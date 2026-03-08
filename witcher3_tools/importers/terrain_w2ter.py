import os
import re
import math
import struct
import zlib
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np

W2TER_BUFFER_RE = re.compile(r"\.w2ter\.(\d+)\.buffer$", re.IGNORECASE)
W2TER_TILE_RE = re.compile(
    r"tile_(?P<y>\d+)_x_(?P<x>\d+)_res(?P<res>\d+)\.w2ter(?:\.(?P<buffer>\d+)\.buffer)?$",
    re.IGNORECASE,
)

BUFFER_LABELS = {
    1: "heightmap",
    2: "texturemap",
}

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
    elif color_type == 3 and bit_depth == 8:
        bpp = 1
    elif color_type == 6 and bit_depth == 8:
        bpp = 4
    else:
        raise ValueError("Unsupported PNG format")

    row_bytes = width * bpp
    if len(data) < row_bytes * height:
        raise ValueError("PNG data too small")

    raw = bytearray()
    for row in range(height):
        start = row * row_bytes
        raw.append(0)
        raw.extend(data[start:start + row_bytes])

    compressed = zlib.compress(bytes(raw))

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


def select_tintmap_buffer_index(tiles: Dict[int, Dict[Tuple[int, int], str]], res: int) -> Optional[int]:
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


def build_dds_header_dxt1(width: int, height: int) -> bytes:
    dds_magic = b"DDS "
    dds_header_size = 124
    # CAPS | HEIGHT | WIDTH | PIXELFORMAT | LINEARSIZE
    dds_flags = 0x00081007
    dds_depth = 0
    dds_pixelformat_size = 32
    dds_pixelflags = 0x00000004
    dds_fourcc = b"DXT1"
    dds_caps = 0x1000

    linear_size = ((width + 3) // 4) * ((height + 3) // 4) * 8
    fourcc_value = struct.unpack("<I", dds_fourcc)[0]

    header = struct.pack("<4sI", dds_magic, dds_header_size)
    header += struct.pack(
        "<IIIIII",
        dds_flags,
        height,
        width,
        linear_size,
        dds_depth,
        0,
    )
    # 11 reserved DWORDs
    header += b"\x00" * 44
    header += struct.pack(
        "<IIIIIIII",
        dds_pixelformat_size,
        dds_pixelflags,
        fourcc_value,
        0,
        0,
        0,
        0,
        0,
    )
    header += struct.pack("<IIII", dds_caps, 0, 0, 0)
    header += struct.pack("<I", 0)
    return header


def write_dds_dxt1(output_path: str, width: int, height: int, data: bytes) -> None:
    header = build_dds_header_dxt1(width, height)
    with open(output_path, "wb") as dds_file:
        dds_file.write(header)
        dds_file.write(data)


def _decode_rgb565(value: int) -> Tuple[int, int, int]:
    r = ((value >> 11) & 0x1F) * 255 // 31
    g = ((value >> 5) & 0x3F) * 255 // 63
    b = (value & 0x1F) * 255 // 31
    return r, g, b


def decode_bc1_to_rgba(data: bytes, width: int, height: int) -> Optional[np.ndarray]:
    if width <= 0 or height <= 0 or width % 4 != 0 or height % 4 != 0:
        return None
    blocks_x = width // 4
    blocks_y = height // 4
    expected = blocks_x * blocks_y * 8
    if len(data) < expected:
        return None

    rgba = np.zeros((height, width, 4), dtype=np.uint8)
    offset = 0
    for by in range(blocks_y):
        for bx in range(blocks_x):
            c0, c1 = struct.unpack_from("<HH", data, offset)
            bits = struct.unpack_from("<I", data, offset + 4)[0]
            offset += 8

            c0_rgb = _decode_rgb565(c0)
            c1_rgb = _decode_rgb565(c1)
            if c0 > c1:
                colors = [
                    (c0_rgb[0], c0_rgb[1], c0_rgb[2], 255),
                    (c1_rgb[0], c1_rgb[1], c1_rgb[2], 255),
                    (
                        (2 * c0_rgb[0] + c1_rgb[0]) // 3,
                        (2 * c0_rgb[1] + c1_rgb[1]) // 3,
                        (2 * c0_rgb[2] + c1_rgb[2]) // 3,
                        255,
                    ),
                    (
                        (c0_rgb[0] + 2 * c1_rgb[0]) // 3,
                        (c0_rgb[1] + 2 * c1_rgb[1]) // 3,
                        (c0_rgb[2] + 2 * c1_rgb[2]) // 3,
                        255,
                    ),
                ]
            else:
                colors = [
                    (c0_rgb[0], c0_rgb[1], c0_rgb[2], 255),
                    (c1_rgb[0], c1_rgb[1], c1_rgb[2], 255),
                    (
                        (c0_rgb[0] + c1_rgb[0]) // 2,
                        (c0_rgb[1] + c1_rgb[1]) // 2,
                        (c0_rgb[2] + c1_rgb[2]) // 2,
                        255,
                    ),
                    (0, 0, 0, 0),
                ]

            block_x = bx * 4
            block_y = by * 4
            for py in range(4):
                for px in range(4):
                    idx = (bits >> (2 * (py * 4 + px))) & 0x03
                    rgba[block_y + py, block_x + px] = colors[idx]

    return rgba


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


def _tile_texture_pngs(path: str, info: TileInfo) -> List[str]:
    outputs: List[str] = []
    data = np.fromfile(path, dtype="<u2")
    if data.size != info.res * info.res:
        return outputs
    tile = data.reshape((info.res, info.res))
    tile = np.flipud(tile)

    overlay = (tile & 0x1F).astype(np.uint8)
    bkgrnd = ((tile >> 5) & 0x1F).astype(np.uint8)
    blend = ((tile >> 10) & 0x3F).astype(np.uint8)

    base = path
    palette = bytes(TEXTURING_PALETTE)
    bk_path = base + ".bkgrnd.png"
    ov_path = base + ".overlay.png"
    bl_path = base + ".blendcontrol.png"
    write_png(bk_path, info.res, info.res, 3, 8, bkgrnd.tobytes(), palette)
    write_png(ov_path, info.res, info.res, 3, 8, overlay.tobytes(), palette)
    write_png(bl_path, info.res, info.res, 3, 8, blend.tobytes(), blendcontrol_palette())
    outputs.extend([bk_path, ov_path, bl_path])
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


def combine_w2ter_tiles(
    buffer_paths: List[str],
    output_dir: str,
    hub_name: str,
    res_override: Optional[int] = None,
    x_tiles_override: Optional[int] = None,
    y_tiles_override: Optional[int] = None,
) -> Dict[str, object]:
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

    # Individual per-tile images are exported next to source .w2ter buffers.
    tile_image_outputs = export_tile_images(buffer_paths)
    outputs.extend(tile_image_outputs)

    if 1 in tiles:
        heightmap = assemble_heightmap(tiles[1], res, x_tiles, y_tiles)
        out_path = os.path.join(output_dir, f"combined.{hub_name}.data")
        with open(out_path, "wb") as target:
            target.write(heightmap)
        outputs.append(out_path)
        try:
            png_path = os.path.join(output_dir, f"{hub_name}.heightmap.png")
            write_png(png_path, res * x_tiles, res * y_tiles, 0, 16, heightmap)
            outputs.append(png_path)
        except Exception:
            pass

    if 2 in tiles:
        bkgrnd, overlay, blend = assemble_texture_maps(tiles[2], res, x_tiles, y_tiles)
        out_bk = os.path.join(output_dir, f"combined.{hub_name}.bkgrnd.data")
        out_ov = os.path.join(output_dir, f"combined.{hub_name}.overlay.data")
        out_bl = os.path.join(output_dir, f"combined.{hub_name}.blendcontrol.data")
        bkgrnd.tofile(out_bk)
        overlay.tofile(out_ov)
        blend.tofile(out_bl)
        outputs.extend([out_bk, out_ov, out_bl])
        try:
            palette = bytes(TEXTURING_PALETTE)
            bk_png = os.path.join(output_dir, f"{hub_name}.bkgrnd.png")
            ov_png = os.path.join(output_dir, f"{hub_name}.overlay.png")
            bl_png = os.path.join(output_dir, f"{hub_name}.blendcontrol.png")
            write_png(bk_png, res * x_tiles, res * y_tiles, 3, 8, bkgrnd.tobytes(), palette)
            write_png(ov_png, res * x_tiles, res * y_tiles, 3, 8, overlay.tobytes(), palette)
            write_png(bl_png, res * x_tiles, res * y_tiles, 3, 8, blend.tobytes(), blendcontrol_palette())
            outputs.extend([bk_png, ov_png, bl_png])
        except Exception:
            pass

    tint_idx = select_tintmap_buffer_index(tiles, res)
    if tint_idx is not None:
        tile_blocks = get_tintmap_tile_blocks(tiles[tint_idx])
        if tile_blocks:
            tintmap = assemble_tintmap(tiles[tint_idx], tile_blocks, x_tiles, y_tiles)
            width = tile_blocks * 4 * x_tiles
            height = tile_blocks * 4 * y_tiles
            out_dds = os.path.join(output_dir, f"combined.{hub_name}.dds")
            write_dds_dxt1(out_dds, width, height, tintmap)
            outputs.append(out_dds)
            try:
                rgba = decode_bc1_to_rgba(tintmap, width, height)
                if rgba is not None:
                    rgba = np.flipud(rgba)
                    if res > tile_blocks * 4 and res % (tile_blocks * 4) == 0:
                        scale = res // (tile_blocks * 4)
                        rgba = np.repeat(np.repeat(rgba, scale, axis=0), scale, axis=1)
                    out_png = os.path.join(output_dir, f"{hub_name}.tint.png")
                    write_png(out_png, rgba.shape[1], rgba.shape[0], 6, 8, rgba.tobytes())
                    outputs.append(out_png)
            except Exception:
                pass

    return {"outputs": outputs, "info": info}
