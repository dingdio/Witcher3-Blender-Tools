import os
import re
import math
import struct
import zlib
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np

from ..CR2W.texture_dds import DDSMetadata, EFormat, write_dds_payload

W2TER_BUFFER_RE = re.compile(r"\.w2ter\.(\d+)\.buffer$", re.IGNORECASE)
W2TER_TILE_RE = re.compile(
    r"tile_(?P<y>\d+)_x_(?P<x>\d+)_res(?P<res>\d+)\.w2ter(?:\.(?P<buffer>\d+)\.buffer)?$",
    re.IGNORECASE,
)

BUFFER_LABELS = {
    1: "heightmap",
    2: "texturemap",
}


def _safe_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _generator_mtime() -> float:
    return _safe_mtime(__file__)


def _max_source_mtime(paths) -> float:
    newest = _generator_mtime()
    for p in paths:
        m = _safe_mtime(p)
        if m > newest:
            newest = m
    return newest


def _is_fresh(out_path: str, src_mtime: float) -> bool:
    if not out_path or not os.path.isfile(out_path):
        return False
    return _safe_mtime(out_path) >= src_mtime

# palette from bevy plugin (32 colors, RGB)
TEXTURING_PALETTE = [
    0, 0, 0,        75, 87, 66,     68, 82, 61,
    102, 88, 75,    81, 73, 62,     74, 92, 59,
    81, 70, 57,     70, 62, 54,     85, 73, 64,
    70, 68, 54,     66, 58, 51,     110, 99, 84,
    121, 113, 102,  105, 90, 75,    92, 112, 75,
    81, 102, 66,    90, 70, 59,     53, 62, 40,
    115, 92, 72,    90, 78, 64,     113, 104, 90,
    114, 115, 117,  105, 101, 97,   145, 143, 139,
    105, 97, 87,    151, 146, 132,  185, 172, 152,
    171, 164, 148,  182, 179, 175,  60, 79, 53,
    104, 105, 103,  36, 30, 22,
]


def blendcontrol_palette() -> bytes:
    palette = [0] * (64 * 3)
    for i in range(64):
        scale = i % 8
        slope = (i // 8) % 8
        palette[i * 3] = 32 + (255 // 8) * scale
        palette[i * 3 + 1] = 55 + scale * slope * 4
        palette[i * 3 + 2] = 32 + (255 // 8) * slope
    return bytes(palette)


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    length = struct.pack(">I", len(data))
    crc = struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return length + tag + data + crc


def write_png(
    output_path: str,
    width: int,
    height: int,
    color_type: int,
    bit_depth: int,
    data: bytes,
    palette: Optional[bytes] = None,
) -> None:
    if width <= 0 or height <= 0:
        return
    if color_type == 0 and bit_depth == 16:
        bpp = 2
    elif color_type == 2 and bit_depth == 16:
        bpp = 6
    elif color_type == 3 and bit_depth == 8:
        bpp = 1
    elif color_type == 6 and bit_depth == 8:
        bpp = 4
    else:
        raise ValueError("Unsupported PNG format")

    row_bytes = width * bpp
    if len(data) < row_bytes * height:
        raise ValueError("PNG data too small")

    pixels = np.frombuffer(data, dtype=np.uint8, count=row_bytes * height).reshape(height, row_bytes)
    raw = np.empty((height, row_bytes + 1), dtype=np.uint8)
    raw[:, 0] = 0
    raw[:, 1:] = pixels

    compressed = zlib.compress(raw.tobytes())

    ihdr = struct.pack(">IIBBBBB", width, height, bit_depth, color_type, 0, 0, 0)
    chunks = [_png_chunk(b"IHDR", ihdr)]
    if palette is not None:
        chunks.append(_png_chunk(b"PLTE", palette))
    chunks.append(_png_chunk(b"IDAT", compressed))
    chunks.append(_png_chunk(b"IEND", b""))

    with open(output_path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        for chunk in chunks:
            f.write(chunk)


def bake_terrain_lod_overview(
    tile_paths: Dict[Tuple[int, int], str],
    res: int,
    x_tiles: int,
    y_tiles: int,
    output_path: str,
    *,
    out_res: int = 2048,
    skip_existing: bool = True,
) -> Optional[str]:
    if not tile_paths or res <= 0 or x_tiles <= 0 or y_tiles <= 0:
        return None
    src_mtime = _max_source_mtime(tile_paths.values())
    if skip_existing and _is_fresh(output_path, src_mtime):
        return output_path

    tile_px = max(1, min(int(res), int(out_res) // max(int(x_tiles), int(y_tiles))))
    sample_axis = np.rint(np.linspace(0, int(res) - 1, tile_px)).astype(np.intp)
    palette = np.asarray(TEXTURING_PALETTE, dtype=np.uint8).reshape(-1, 3)
    rgb = np.zeros((int(y_tiles) * tile_px, int(x_tiles) * tile_px, 3), dtype=np.uint8)
    alpha = np.zeros(rgb.shape[:2], dtype=np.uint8)

    for (x, y), path in tile_paths.items():
        if not 0 <= int(x) < int(x_tiles) or not 0 <= int(y) < int(y_tiles):
            continue
        try:
            tile = np.memmap(path, dtype="<u2", mode="r", shape=(int(res), int(res)))
            sampled = tile[np.ix_(sample_axis, sample_axis)]
            overlay = sampled & 0x1F
            background = (sampled >> 5) & 0x1F
            layer = np.where(overlay > 0, overlay, background).astype(np.intp)
            block = palette[np.clip(layer, 0, len(palette) - 1)]
            block_alpha = np.where(sampled == 0, 0, 255).astype(np.uint8)
        except (OSError, ValueError):
            continue
        y0 = int(y) * tile_px
        x0 = int(x) * tile_px
        rgb[y0:y0 + tile_px, x0:x0 + tile_px] = block
        alpha[y0:y0 + tile_px, x0:x0 + tile_px] = block_alpha

    rgb = np.flipud(rgb)
    alpha = np.flipud(alpha)
    rgba = np.empty((*rgb.shape[:2], 4), dtype=np.uint8)
    rgba[..., :3] = rgb
    rgba[..., 3] = alpha
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    write_png(output_path, rgba.shape[1], rgba.shape[0], 6, 8, rgba.tobytes())
    return output_path


@dataclass(frozen=True)
class TileInfo:
    x: int
    y: int
    res: int
    buffer_index: Optional[int]


def is_w2ter_buffer_name(name: str) -> bool:
    return bool(W2TER_BUFFER_RE.search(name))


def get_w2ter_buffer_index(name: str) -> Optional[int]:
    match = W2TER_BUFFER_RE.search(name)
    if not match:
        return None
    try:
        return int(match.group(1))
    except Exception:
        return None


def get_w2ter_buffer_label(index: Optional[int]) -> str:
    if index is None:
        return ""
    if index in BUFFER_LABELS:
        return BUFFER_LABELS[index]
    if index >= 3:
        # Buffer 1/2 are known. Higher buffers vary by hub and mip usage.
        return f"buffer{index}"
    return f"buffer{index}"


def parse_tile_filename(name: str) -> Optional[TileInfo]:
    match = W2TER_TILE_RE.search(os.path.basename(name))
    if not match:
        return None
    buffer_raw = match.group("buffer")
    buffer_index = int(buffer_raw) if buffer_raw is not None else None
    return TileInfo(
        x=int(match.group("x")),
        y=int(match.group("y")),
        res=int(match.group("res")),
        buffer_index=buffer_index,
    )


def is_w2ter_tile_name(name: str) -> bool:
    return parse_tile_filename(name) is not None


def collect_tile_buffers(paths: List[str]) -> Dict[str, object]:
    tiles_by_buffer: Dict[int, Dict[Tuple[int, int], str]] = {}
    res: Optional[int] = None
    max_x = -1
    max_y = -1
    skipped: List[str] = []

    for path in paths:
        info = parse_tile_filename(path)
        if not info or info.buffer_index is None:
            continue
        if res is None:
            res = info.res
        elif info.res != res:
            skipped.append(path)
            continue
        max_x = max(max_x, info.x)
        max_y = max(max_y, info.y)
        tiles_by_buffer.setdefault(info.buffer_index, {})[(info.x, info.y)] = path

    return {
        "res": res,
        "x_tiles": max_x + 1 if max_x >= 0 else 0,
        "y_tiles": max_y + 1 if max_y >= 0 else 0,
        "tiles": tiles_by_buffer,
        "skipped": skipped,
    }


def assemble_heightmap(tile_paths: Dict[Tuple[int, int], str], res: int, x_tiles: int, y_tiles: int) -> bytes:
    result = np.zeros((y_tiles * res, x_tiles * res), dtype=np.uint16)
    for (x, y), path in tile_paths.items():
        data = np.fromfile(path, dtype="<u2")
        if data.size != res * res:
            continue
        tile = data.reshape((res, res))
        dest_y = y * res
        result[dest_y:dest_y + res, x * res:(x + 1) * res] = tile

    # match bevy: flip vertically after assembling
    result = np.flipud(result)

    # border fix (bevy workaround)
    if result.shape[0] > 1:
        result[0, :] = result[1, :]
    if result.shape[1] > 1:
        result[:, -1] = result[:, -2]

    return result.byteswap().tobytes()


def assemble_texture_maps(
    tile_paths: Dict[Tuple[int, int], str],
    res: int,
    x_tiles: int,
    y_tiles: int,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    bkgrnd = np.zeros((y_tiles * res, x_tiles * res), dtype=np.uint8)
    overlay = np.zeros((y_tiles * res, x_tiles * res), dtype=np.uint8)
    blend = np.zeros((y_tiles * res, x_tiles * res), dtype=np.uint8)

    for (x, y), path in tile_paths.items():
        data = np.fromfile(path, dtype="<u2")
        if data.size != res * res:
            continue
        tile = data.reshape((res, res))
        dest_y = y * res
        sl_y = slice(dest_y, dest_y + res)
        sl_x = slice(x * res, (x + 1) * res)
        overlay[sl_y, sl_x] = (tile & 0x1F).astype(np.uint8)
        bkgrnd[sl_y, sl_x] = ((tile >> 5) & 0x1F).astype(np.uint8)
        blend[sl_y, sl_x] = ((tile >> 10) & 0x3F).astype(np.uint8)

    # match bevy: flip vertically after assembling
    bkgrnd = np.flipud(bkgrnd)
    overlay = np.flipud(overlay)
    blend = np.flipud(blend)

    return bkgrnd, overlay, blend


def _tintmap_blocks_from_size(byte_size: int) -> Optional[int]:
    if byte_size <= 0 or byte_size % 8 != 0:
        return None
    blocks = int(math.isqrt(byte_size // 8))
    if blocks * blocks * 8 != byte_size:
        return None
    return blocks


def get_tintmap_blocks_from_file(path: str) -> Optional[int]:
    try:
        size = os.path.getsize(path)
    except Exception:
        return None
    return _tintmap_blocks_from_size(size)


def get_tintmap_tile_blocks(tile_paths: Dict[Tuple[int, int], str]) -> Optional[int]:
    for path in tile_paths.values():
        blocks = get_tintmap_blocks_from_file(path)
        if blocks:
            return blocks
    return None


def _raw_colormap_res_from_size(byte_size: int) -> Optional[int]:
    if byte_size <= 0 or byte_size % 4 != 0:
        return None
    res = int(math.isqrt(byte_size // 4))
    if res * res * 4 != byte_size:
        return None
    return res


def get_raw_colormap_res_from_file(path: str) -> Optional[int]:
    try:
        size = os.path.getsize(path)
    except Exception:
        return None
    return _raw_colormap_res_from_size(size)


def get_raw_colormap_tile_res(tile_paths: Dict[Tuple[int, int], str]) -> Optional[int]:
    for path in tile_paths.values():
        res = get_raw_colormap_res_from_file(path)
        if res:
            return res
    return None


def _infer_colormap_mip(res: int, tile_blocks: int) -> Optional[int]:
    tile_res_px = tile_blocks * 4
    if tile_res_px <= 0 or res % tile_res_px != 0:
        return None
    ratio = res // tile_res_px
    if ratio <= 0:
        return None
    mip = int(round(math.log2(ratio)))
    if (1 << mip) * tile_res_px != res:
        return None
    return mip


def _infer_raw_colormap_mip(res: int, raw_res: int) -> Optional[int]:
    if raw_res <= 0 or res % raw_res != 0:
        return None
    ratio = res // raw_res
    if ratio <= 0:
        return None
    mip = int(round(math.log2(ratio)))
    if (1 << mip) * raw_res != res:
        return None
    return mip


def _representative_buffer_size(tile_paths: Dict[Tuple[int, int], str]) -> Optional[int]:
    counts: Dict[int, int] = {}
    for path in tile_paths.values():
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        counts[size] = counts.get(size, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


def _bc1_size(width: int, height: int) -> int:
    return max(1, (width + 3) // 4) * max(1, (height + 3) // 4) * 8


def _expected_buffer_sizes(
    res: int,
    num_mips: int,
    colormap_start_mip: int,
    colormap_encoding: str,
) -> List[int]:
    seq: List[int] = []
    for mip in range(num_mips):
        r = res >> mip
        if r <= 0:
            return []
        u16 = r * r * 2
        seq.append(u16)
        seq.append(u16)
        if mip >= colormap_start_mip:
            if colormap_encoding == "raw_rgba":
                seq.append(r * r * 4)
            elif colormap_encoding == "bc1":
                seq.append(_bc1_size(r, r))
            else:
                return []
    return seq


def _detect_colormap_layout(
    tiles: Dict[int, Dict[Tuple[int, int], str]],
    res: int,
) -> Optional[Tuple[int, str]]:
    if not res or res <= 0:
        return None
    indices = sorted(tiles.keys())
    if not indices:
        return None
    if indices[0] != 1 or indices != list(range(1, indices[-1] + 1)):
        return None
    sizes: List[int] = []
    for idx in indices:
        size = _representative_buffer_size(tiles[idx])
        if size is None:
            return None
        sizes.append(size)

    total = len(indices)
    for start_mip in range(0, res.bit_length()):
        if (total + start_mip) % 3 != 0:
            continue
        num_mips = (total + start_mip) // 3
        if not 0 <= start_mip < num_mips:
            continue
        for encoding in ("raw_rgba", "bc1"):
            if _expected_buffer_sizes(res, num_mips, start_mip, encoding) == sizes:
                return start_mip, encoding
    return None


def detect_colormap_start_mip(
    tiles: Dict[int, Dict[Tuple[int, int], str]],
    res: int,
) -> Optional[int]:
    layout = _detect_colormap_layout(tiles, res)
    return layout[0] if layout else None


def select_tintmap_buffer_index(
    tiles: Dict[int, Dict[Tuple[int, int], str]],
    res: int,
) -> Optional[int]:
    layout = _detect_colormap_layout(tiles, res)
    if layout is not None:
        start_mip, encoding = layout
        idx = start_mip * 2 + 3
        if idx in tiles:
            if encoding == "raw_rgba" and get_raw_colormap_tile_res(tiles[idx]):
                return idx
            if encoding == "bc1" and get_tintmap_tile_blocks(tiles[idx]):
                return idx

    raw_candidates = []
    for idx, tile_paths in tiles.items():
        raw_res = get_raw_colormap_tile_res(tile_paths)
        if not raw_res:
            continue
        if raw_res == res:
            return idx
        mip = _infer_raw_colormap_mip(res, raw_res)
        if mip is None:
            continue
        start_mip = 3 * mip + 3 - idx
        if 0 <= start_mip <= mip:
            raw_candidates.append((idx, raw_res))
    if raw_candidates:
        # REDkit/editor tiles store uncompressed RGBA colormaps per mip. Prefer
        # the largest available mip so it matches the full terrain resolution.
        return sorted(raw_candidates, key=lambda it: (-it[1], it[0]))[0][0]

    start_mip = detect_colormap_start_mip(tiles, res)
    if start_mip is not None:
        idx = start_mip * 2 + 3
        if idx in tiles and get_tintmap_tile_blocks(tiles[idx]):
            return idx
    candidates = []
    for idx, tile_paths in tiles.items():
        if idx < 3:
            continue
        blocks = get_tintmap_tile_blocks(tile_paths)
        if not blocks:
            continue
        mip = _infer_colormap_mip(res, blocks)
        if mip is None:
            continue
        expected = mip * 2 + 3
        if idx == expected:
            return idx
        candidates.append((idx, expected))
    if candidates:
        # fallback: pick the smallest index (closest to base mip)
        return sorted(candidates, key=lambda it: it[0])[0][0]
    return None


def assemble_tintmap(tile_paths: Dict[Tuple[int, int], str], tile_blocks: int, x_tiles: int, y_tiles: int) -> bytes:
    row_bytes = tile_blocks * 8
    target_row_bytes = x_tiles * row_bytes
    result = bytearray(target_row_bytes * (y_tiles * tile_blocks))

    for (x, y), path in tile_paths.items():
        with open(path, "rb") as file:
            data = file.read()
        expected = tile_blocks * tile_blocks * 8
        if len(data) < expected:
            continue
        data = data[:expected]
        for line in range(tile_blocks):
            src_start = line * row_bytes
            src_end = src_start + row_bytes
            dest_row = y * tile_blocks + line
            dest_start = dest_row * target_row_bytes + x * row_bytes
            result[dest_start:dest_start + row_bytes] = data[src_start:src_end]

    return bytes(result)


def assemble_raw_colormap(
    tile_paths: Dict[Tuple[int, int], str],
    tile_res_px: int,
    x_tiles: int,
    y_tiles: int,
) -> Optional[np.ndarray]:
    if tile_res_px <= 0:
        return None
    result = np.zeros((y_tiles * tile_res_px, x_tiles * tile_res_px, 4), dtype=np.uint8)
    expected = tile_res_px * tile_res_px * 4
    for (x, y), path in tile_paths.items():
        try:
            data = np.fromfile(path, dtype=np.uint8, count=expected)
        except Exception:
            continue
        if data.size != expected:
            continue
        tile = data.reshape((tile_res_px, tile_res_px, 4))
        dest_y = y * tile_res_px
        result[dest_y:dest_y + tile_res_px, x * tile_res_px:(x + 1) * tile_res_px] = tile
    return np.ascontiguousarray(np.flipud(result))


def write_dds_dxt1(output_path: str, width: int, height: int, data: bytes) -> None:
    metadata = DDSMetadata(width=width, height=height, mipscount=0, format=EFormat.BC1_UNORM)
    write_dds_payload(output_path, metadata, data)


def _decode_rgb565(value: int) -> Tuple[int, int, int]:
    r = ((value >> 11) & 0x1F) * 255 // 31
    g = ((value >> 5) & 0x3F) * 255 // 63
    b = (value & 0x1F) * 255 // 31
    return r, g, b


def decode_bc1_to_rgba(
    data: bytes,
    width: int,
    height: int,
    *,
    force_four_color: bool = False,
) -> Optional[np.ndarray]:
    if width <= 0 or height <= 0 or width % 4 != 0 or height % 4 != 0:
        return None
    blocks_x = width // 4
    blocks_y = height // 4
    n_blocks = blocks_x * blocks_y
    expected = n_blocks * 8
    if len(data) < expected:
        return None

    raw = np.frombuffer(data, dtype=np.uint8, count=expected).reshape(n_blocks, 8)
    c0 = raw[:, 0].astype(np.uint16) | (raw[:, 1].astype(np.uint16) << 8)
    c1 = raw[:, 2].astype(np.uint16) | (raw[:, 3].astype(np.uint16) << 8)
    bits = (raw[:, 4].astype(np.uint32) | (raw[:, 5].astype(np.uint32) << 8)
            | (raw[:, 6].astype(np.uint32) << 16) | (raw[:, 7].astype(np.uint32) << 24))

    def _rgb565(v):
        r = (((v >> 11) & 0x1F).astype(np.uint16) * 255 // 31).astype(np.uint8)
        g = (((v >> 5) & 0x3F).astype(np.uint16) * 255 // 63).astype(np.uint8)
        b = ((v & 0x1F).astype(np.uint16) * 255 // 31).astype(np.uint8)
        return r, g, b

    r0, g0, b0 = _rgb565(c0)
    r1, g1, b1 = _rgb565(c1)
    R0, G0, B0 = r0.astype(np.int32), g0.astype(np.int32), b0.astype(np.int32)
    R1, G1, B1 = r1.astype(np.int32), g1.astype(np.int32), b1.astype(np.int32)
    full = np.full(n_blocks, 255, np.int32)

    pal = np.zeros((n_blocks, 4, 4), dtype=np.uint8)
    pal[:, 0] = np.stack([r0, g0, b0, np.full_like(r0, 255)], axis=1)
    pal[:, 1] = np.stack([r1, g1, b1, np.full_like(r1, 255)], axis=1)

    opaque = ((c0 > c1) | bool(force_four_color))[:, None]
    c2_op = np.stack([(2 * R0 + R1) // 3, (2 * G0 + G1) // 3, (2 * B0 + B1) // 3, full], axis=1).astype(np.uint8)
    c3_op = np.stack([(R0 + 2 * R1) // 3, (G0 + 2 * G1) // 3, (B0 + 2 * B1) // 3, full], axis=1).astype(np.uint8)
    c2_tr = np.stack([(R0 + R1) // 2, (G0 + G1) // 2, (B0 + B1) // 2, full], axis=1).astype(np.uint8)
    c3_tr = np.zeros((n_blocks, 4), dtype=np.uint8)
    pal[:, 2] = np.where(opaque, c2_op, c2_tr)
    pal[:, 3] = np.where(opaque, c3_op, c3_tr)

    shifts = (2 * np.arange(16, dtype=np.uint32)).reshape(1, 16)
    idx = ((bits[:, None] >> shifts) & 0x03).astype(np.intp)
    block_pixels = np.take_along_axis(pal, idx[:, :, None], axis=1)

    rgba = (block_pixels.reshape(blocks_y, blocks_x, 4, 4, 4)
            .transpose(0, 2, 1, 3, 4)
            .reshape(blocks_y * 4, blocks_x * 4, 4))
    return np.ascontiguousarray(rgba)


def decode_tintmap_buffer_to_rgba(
    data: bytes,
    tile_res_px: int,
    target_res_px: Optional[int] = None,
) -> Optional[np.ndarray]:
    rgba = decode_bc1_to_rgba(data, tile_res_px, tile_res_px)
    if rgba is None:
        return None
    rgba = np.flipud(rgba)
    if target_res_px and target_res_px > tile_res_px and target_res_px % tile_res_px == 0:
        scale = target_res_px // tile_res_px
        rgba = np.repeat(np.repeat(rgba, scale, axis=0), scale, axis=1)
    return rgba


def decode_tintmap_file_to_rgba(path: str, target_res_px: Optional[int] = None) -> Optional[np.ndarray]:
    raw_res = get_raw_colormap_res_from_file(path)
    if raw_res:
        expected = raw_res * raw_res * 4
        data = np.fromfile(path, dtype=np.uint8, count=expected)
        if data.size != expected:
            return None
        rgba = np.ascontiguousarray(np.flipud(data.reshape((raw_res, raw_res, 4))))
        if target_res_px and target_res_px > raw_res and target_res_px % raw_res == 0:
            scale = target_res_px // raw_res
            rgba = np.repeat(np.repeat(rgba, scale, axis=0), scale, axis=1)
        return rgba
    blocks = get_tintmap_blocks_from_file(path)
    if not blocks:
        return None
    with open(path, "rb") as f:
        data = f.read()
    expected = blocks * blocks * 8
    if len(data) < expected:
        return None
    tile_res_px = blocks * 4
    return decode_tintmap_buffer_to_rgba(data[:expected], tile_res_px, target_res_px)


# Full-map diffuse bake: resolve the RED terrain blend to one EEVEE-safe texture.
TERRAIN_SLOPE_LIMITS = tuple(i / 8.0 for i in range(7)) + (0.98,)


def _assemble_height_u16(tile_paths: Dict[Tuple[int, int], str], res: int, x_tiles: int, y_tiles: int) -> np.ndarray:
    """Raw uint16 heightmap matching assemble_texture_maps orientation."""
    result = np.zeros((y_tiles * res, x_tiles * res), dtype=np.uint16)
    for (x, y), path in tile_paths.items():
        data = np.fromfile(path, dtype="<u2")
        if data.size != res * res:
            continue
        result[y * res:(y + 1) * res, x * res:(x + 1) * res] = data.reshape((res, res))
    return np.flipud(result)


def _decimate_nearest(arr: np.ndarray, out: int) -> np.ndarray:
    h, w = arr.shape[:2]
    th, tw = min(h, out), min(w, out)
    if th == h and tw == w:
        return arr
    ys = np.linspace(0, h - 1, th).astype(np.intp)
    xs = np.linspace(0, w - 1, tw).astype(np.intp)
    return arr[ys][:, xs]


def _resample_avg(arr: np.ndarray, out: int) -> np.ndarray:
    h, w = arr.shape[:2]
    th, tw = min(h, out), min(w, out)
    if th == h and tw == w:
        return arr.astype(np.float32)
    if h % th == 0 and w % tw == 0:
        fy, fx = h // th, w // tw
        a = arr.astype(np.float32)
        if arr.ndim == 2:
            return a.reshape(th, fy, tw, fx).mean(axis=(1, 3))
        return a.reshape(th, fy, tw, fx, arr.shape[2]).mean(axis=(1, 3))
    return _decimate_nearest(arr, out).astype(np.float32)


def bake_terrain_fullmap_diffuse(
    overlay_idx: np.ndarray,
    bkgrnd_idx: np.ndarray,
    slope_band_idx: Optional[np.ndarray],
    height_u16: Optional[np.ndarray],
    tint_rgb: Optional[np.ndarray],
    layer_colors,
    terrain_size: float,
    elev_range: float,
    out_res: int = 8192,
    use_slope: bool = True,
    slope_sharpness: float = 0.5,
    hole_color: Tuple[float, float, float] = (0.25, 0.22, 0.18),
) -> Optional[np.ndarray]:
    """Resolve terrain layer indices, slope, and tint into one uint8 RGB image."""
    lut = np.asarray(layer_colors, dtype=np.float32)
    if lut.ndim != 2 or lut.shape[1] != 3 or lut.shape[0] == 0:
        return None
    n = lut.shape[0]

    work = max(overlay_idx.shape[0], overlay_idx.shape[1]) if out_res <= 0 else out_res
    overlay_idx = _decimate_nearest(overlay_idx, work)
    bkgrnd_idx = _decimate_nearest(bkgrnd_idx, work)

    def lookup(idx_map: np.ndarray) -> np.ndarray:
        idx = idx_map.astype(np.intp)
        col = lut[np.clip(idx - 1, 0, n - 1)]
        hole = idx <= 0
        if hole.any():
            col = col.copy()
            col[hole] = np.asarray(hole_color, np.float32)
        return col

    base = lookup(overlay_idx)

    if use_slope and height_u16 is not None and slope_band_idx is not None:
        bk = lookup(bkgrnd_idx)
        h_m = _resample_avg(height_u16, work) * (float(elev_range) / 65535.0)
        spacing = float(terrain_size) / max(h_m.shape[1], 1)
        gy, gx = np.gradient(h_m, spacing if spacing > 0 else 1.0)
        slope = np.sqrt(gx * gx + gy * gy)
        del gx, gy, h_m
        limits = np.asarray(TERRAIN_SLOPE_LIMITS, np.float32)[
            np.clip(_decimate_nearest(slope_band_idx, work).astype(np.intp), 0, 7)
        ]
        sb = np.clip((slope - limits) / max(slope_sharpness, 1e-4), 0.0, 1.0)[..., None]
        base = base * (1.0 - sb) + bk * sb
        del bk, sb, slope, limits

    if tint_rgb is not None:
        t = np.clip(_resample_avg(tint_rgb, work), 0.0, 1.0)
        if t.shape[:2] == base.shape[:2] and t.shape[2] >= 3:
            t = t[..., :3]
            base = np.where(t < 0.5, 2.0 * t * base, 1.0 - 2.0 * (1.0 - t) * (1.0 - base))
            del t

    return (np.clip(base, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)


def bake_terrain_fullmap_from_tiles(
    tiles: Dict[int, Dict[Tuple[int, int], str]],
    res: int,
    x_tiles: int,
    y_tiles: int,
    layer_colors,
    output_path: str,
    out_res: int = 8192,
    use_slope: bool = True,
    terrain_size: float = 0.0,
    lowest_elevation: float = 0.0,
    highest_elevation: float = 0.0,
    skip_existing: bool = False,
    src_mtime: float = 0.0,
) -> Optional[str]:
    if 2 not in tiles:
        return None
    if skip_existing and _is_fresh(output_path, src_mtime):
        return output_path

    bkgrnd, overlay, blend = assemble_texture_maps(tiles[2], res, x_tiles, y_tiles)
    slope_band = (blend & 0x07).astype(np.uint8)

    height = None
    if use_slope and 1 in tiles:
        height = _assemble_height_u16(tiles[1], res, x_tiles, y_tiles)

    tint = None
    tint_idx = select_tintmap_buffer_index(tiles, res)
    if tint_idx is not None:
        raw_res = get_raw_colormap_tile_res(tiles[tint_idx])
        if raw_res:
            rgba = assemble_raw_colormap(tiles[tint_idx], raw_res, x_tiles, y_tiles)
            if rgba is not None:
                if res > raw_res and res % raw_res == 0:
                    s = res // raw_res
                    rgba = np.repeat(np.repeat(rgba, s, axis=0), s, axis=1)
                tint = rgba[..., :3].astype(np.float32) / 255.0
        else:
            tile_blocks = get_tintmap_tile_blocks(tiles[tint_idx])
            if tile_blocks:
                tintmap = assemble_tintmap(tiles[tint_idx], tile_blocks, x_tiles, y_tiles)
                tw, th = tile_blocks * 4 * x_tiles, tile_blocks * 4 * y_tiles
                rgba = decode_bc1_to_rgba(tintmap, tw, th)
                if rgba is not None:
                    rgba = np.flipud(rgba)
                    if res > tile_blocks * 4 and res % (tile_blocks * 4) == 0:
                        s = res // (tile_blocks * 4)
                        rgba = np.repeat(np.repeat(rgba, s, axis=0), s, axis=1)
                    tint = rgba[..., :3].astype(np.float32) / 255.0

    elev_range = abs(float(lowest_elevation)) + abs(float(highest_elevation))
    rgb = bake_terrain_fullmap_diffuse(
        overlay, bkgrnd, slope_band, height, tint, layer_colors,
        terrain_size, elev_range, out_res=out_res, use_slope=use_slope,
    )
    if rgb is None:
        return None

    rgba_out = np.empty((rgb.shape[0], rgb.shape[1], 4), dtype=np.uint8)
    rgba_out[..., :3] = rgb
    rgba_out[..., 3] = 255
    write_png(output_path, rgb.shape[1], rgb.shape[0], 6, 8, rgba_out.tobytes())
    return output_path


def _tile_heightmap_png(path: str, info: TileInfo) -> Optional[str]:
    data = np.fromfile(path, dtype="<u2")
    if data.size != info.res * info.res:
        return None
    tile = data.reshape((info.res, info.res))
    tile = np.flipud(tile)
    be_data = tile.byteswap().tobytes()
    out_path = path + ".heightmap.png"
    write_png(out_path, info.res, info.res, 0, 16, be_data)
    return out_path


def _tile_texture_pngs(
    path: str,
    info: TileInfo,
    which: Optional[Tuple[str, ...]] = None,
    skip_existing: bool = False,
) -> List[str]:
    if which is None:
        which = ("bkgrnd", "overlay", "blendcontrol")
    wanted = set(which)
    outputs: List[str] = []
    base = path
    channels = (
        ("bkgrnd", ".bkgrnd.png", 5, 0x1F, None),
        ("overlay", ".overlay.png", 0, 0x1F, None),
        ("blendcontrol", ".blendcontrol.png", 10, 0x3F, None),
    )

    src_mtime = _max_source_mtime([path])
    todo = []
    for name, suffix, shift, mask, _pal in channels:
        if name not in wanted:
            continue
        out_path = base + suffix
        if skip_existing and _is_fresh(out_path, src_mtime):
            outputs.append(out_path)
            continue
        todo.append((name, out_path, shift, mask))

    if not todo:
        return outputs

    data = np.fromfile(path, dtype="<u2")
    if data.size != info.res * info.res:
        return outputs
    tile = np.flipud(data.reshape((info.res, info.res)))

    texturing_palette = bytes(TEXTURING_PALETTE)
    blend_palette = blendcontrol_palette()
    for name, out_path, shift, mask in todo:
        channel = ((tile >> shift) & mask).astype(np.uint8)
        palette = blend_palette if name == "blendcontrol" else texturing_palette
        write_png(out_path, info.res, info.res, 3, 8, channel.tobytes(), palette)
        outputs.append(out_path)
    return outputs


def _tile_tintmap_dds(path: str, info: TileInfo) -> Optional[str]:
    blocks = get_tintmap_blocks_from_file(path)
    if not blocks:
        return None
    with open(path, "rb") as f:
        data = f.read()
    tile_res_px = blocks * 4
    out_path = path + ".tintmap.dds"
    write_dds_dxt1(out_path, tile_res_px, tile_res_px, data)
    return out_path


def _tile_tintmap_png(path: str, info: TileInfo) -> Optional[str]:
    rgba = decode_tintmap_file_to_rgba(path, target_res_px=info.res)
    if rgba is None:
        return None
    out_path = path + ".tint.png"
    write_png(out_path, rgba.shape[1], rgba.shape[0], 6, 8, rgba.tobytes())
    return out_path


def export_tile_images(buffer_paths: List[str]) -> List[str]:
    outputs: List[str] = []
    for path in buffer_paths:
        info = parse_tile_filename(path)
        if not info or info.buffer_index is None:
            continue
        if not os.path.exists(path):
            continue
        try:
            if info.buffer_index == 1:
                out = _tile_heightmap_png(path, info)
                if out:
                    outputs.append(out)
            elif info.buffer_index == 2:
                outputs.extend(_tile_texture_pngs(path, info))
            elif info.buffer_index >= 3:
                out = _tile_tintmap_png(path, info)
                if out:
                    outputs.append(out)
                out = _tile_tintmap_dds(path, info)
                if out:
                    outputs.append(out)
        except Exception:
            continue
    return outputs


def _select_override_tiles(override: Optional[int], detected: int) -> int:
    if override and override > 0:
        if detected <= 0:
            return override
        return max(override, detected)
    return detected


COMBINE_TARGETS_ALL = ("per_tile", "heightmap", "overlay", "bkgrnd", "blend", "tint")


def combine_w2ter_tiles(
    buffer_paths: List[str],
    output_dir: str,
    hub_name: str,
    res_override: Optional[int] = None,
    x_tiles_override: Optional[int] = None,
    y_tiles_override: Optional[int] = None,
    targets: Optional[Tuple[str, ...]] = None,
    skip_existing: bool = False,
) -> Dict[str, object]:
    """Assemble selected per-tile .w2ter buffers into combined terrain maps."""
    want = set(COMBINE_TARGETS_ALL if targets is None else targets)
    info = collect_tile_buffers(buffer_paths)
    res_detected = info.get("res")
    res = res_override or res_detected
    if res_override and res_detected and res_override != res_detected:
        info["res_override"] = res_override
        info["res_override_mismatch"] = True
        res = res_detected
    if not res:
        return {"outputs": [], "info": info}

    x_tiles = _select_override_tiles(x_tiles_override, info["x_tiles"])
    y_tiles = _select_override_tiles(y_tiles_override, info["y_tiles"])
    tiles = info["tiles"]

    os.makedirs(output_dir, exist_ok=True)
    outputs: List[str] = []
    src_mtime = _max_source_mtime(buffer_paths) if skip_existing else 0.0

    def _needs(out_path: str) -> bool:
        return not (skip_existing and _is_fresh(out_path, src_mtime))

    if "per_tile" in want:
        outputs.extend(export_tile_images(buffer_paths))

    if "heightmap" in want and 1 in tiles:
        data_path = os.path.join(output_dir, f"combined.{hub_name}.data")
        png_path = os.path.join(output_dir, f"{hub_name}.heightmap.png")
        if _needs(data_path) or _needs(png_path):
            heightmap = assemble_heightmap(tiles[1], res, x_tiles, y_tiles)
            if _needs(data_path):
                with open(data_path, "wb") as target:
                    target.write(heightmap)
            if _needs(png_path):
                try:
                    write_png(png_path, res * x_tiles, res * y_tiles, 0, 16, heightmap)
                except Exception:
                    pass
        for p in (data_path, png_path):
            if os.path.isfile(p):
                outputs.append(p)

    texture_channels = {"overlay", "bkgrnd", "blend"} & want
    if texture_channels and 2 in tiles:
        palette = bytes(TEXTURING_PALETTE)
        specs = {
            "bkgrnd": (f"combined.{hub_name}.bkgrnd.data", f"{hub_name}.bkgrnd.png", palette),
            "overlay": (f"combined.{hub_name}.overlay.data", f"{hub_name}.overlay.png", palette),
            "blend": (f"combined.{hub_name}.blendcontrol.data", f"{hub_name}.blendcontrol.png", blendcontrol_palette()),
        }
        plan = []
        for name in ("bkgrnd", "overlay", "blend"):
            if name not in texture_channels:
                continue
            data_name, png_name, pal = specs[name]
            plan.append((name, os.path.join(output_dir, data_name), os.path.join(output_dir, png_name), pal))

        if any(_needs(d) or _needs(p) for (_n, d, p, _pal) in plan):
            bkgrnd, overlay, blend = assemble_texture_maps(tiles[2], res, x_tiles, y_tiles)
            channel_arrays = {"bkgrnd": bkgrnd, "overlay": overlay, "blend": blend}
            for name, data_path, png_path, pal in plan:
                arr = channel_arrays[name]
                if _needs(data_path):
                    arr.tofile(data_path)
                if _needs(png_path):
                    try:
                        write_png(png_path, res * x_tiles, res * y_tiles, 3, 8, arr.tobytes(), pal)
                    except Exception:
                        pass
        for _name, data_path, png_path, _pal in plan:
            for p in (data_path, png_path):
                if os.path.isfile(p):
                    outputs.append(p)

    if "tint" in want:
        tint_idx = select_tintmap_buffer_index(tiles, res)
        if tint_idx is not None:
            raw_res = get_raw_colormap_tile_res(tiles[tint_idx])
            if raw_res:
                width = raw_res * x_tiles
                height = raw_res * y_tiles
                out_png = os.path.join(output_dir, f"{hub_name}.tint.png")
                if _needs(out_png):
                    rgba = assemble_raw_colormap(tiles[tint_idx], raw_res, x_tiles, y_tiles)
                    if rgba is not None:
                        if res > raw_res and res % raw_res == 0:
                            scale = res // raw_res
                            rgba = np.repeat(np.repeat(rgba, scale, axis=0), scale, axis=1)
                            width = rgba.shape[1]
                            height = rgba.shape[0]
                        try:
                            write_png(out_png, width, height, 6, 8, rgba.tobytes())
                        except Exception:
                            pass
                if os.path.isfile(out_png):
                    outputs.append(out_png)
            else:
                tile_blocks = get_tintmap_tile_blocks(tiles[tint_idx])
                if tile_blocks:
                    width = tile_blocks * 4 * x_tiles
                    height = tile_blocks * 4 * y_tiles
                    out_dds = os.path.join(output_dir, f"combined.{hub_name}.dds")
                    out_png = os.path.join(output_dir, f"{hub_name}.tint.png")
                    if _needs(out_dds) or _needs(out_png):
                        tintmap = assemble_tintmap(tiles[tint_idx], tile_blocks, x_tiles, y_tiles)
                        if _needs(out_dds):
                            write_dds_dxt1(out_dds, width, height, tintmap)
                        if _needs(out_png):
                            try:
                                rgba = decode_bc1_to_rgba(tintmap, width, height)
                                if rgba is not None:
                                    rgba = np.flipud(rgba)
                                    if res > tile_blocks * 4 and res % (tile_blocks * 4) == 0:
                                        scale = res // (tile_blocks * 4)
                                        rgba = np.repeat(np.repeat(rgba, scale, axis=0), scale, axis=1)
                                    write_png(out_png, rgba.shape[1], rgba.shape[0], 6, 8, rgba.tobytes())
                            except Exception:
                                pass
                    for p in (out_dds, out_png):
                        if os.path.isfile(p):
                            outputs.append(p)

    return {"outputs": outputs, "info": info}
