"""Build terrain-detail atlases and control maps without Blender."""

from __future__ import annotations

import json
import os
import struct
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .terrain_w2ter import (
    _safe_mtime,
    decode_bc1_to_rgba,
    decode_tintmap_file_to_rgba,
    write_png,
)

DETAIL_VERSION = 1

UV_SCALE_LUT = (0.333, 0.166, 0.05, 0.025, 0.0125, 0.0075, 0.00375, 0.0)
SLOPE_THRESHOLD_LUT = (0.0, 0.125, 0.25, 0.375, 0.5, 0.625, 0.75, 0.98)
OVERLAY_UV_SCALE = 0.333
TRIPLANAR_TIGHTEN = 0.576
PARAMS_SHARPNESS_SCALE = 2.0

DEFAULT_LAYER_PARAMS = {
    "blend_sharpness": 0.1,
    "slope_base_dampening": 0.0,
    "slope_normal_dampening": 0.5,
    "falloff": 0.0,
    "specularity": 0.0,
    "specularity_base": 0.0,
    "specularity_scale": 0.0,
}


def _detail_mtime() -> float:
    return _safe_mtime(__file__)


def _max_mtime(paths: Sequence[str]) -> float:
    newest = _detail_mtime()
    for p in paths:
        if p:
            m = _safe_mtime(p)
            if m > newest:
                newest = m
    return newest


def _fresh(out_path: str, src_mtime: float) -> bool:
    return bool(out_path) and os.path.isfile(out_path) and _safe_mtime(out_path) >= src_mtime


def _read_dds_header(data: bytes) -> Optional[Tuple[str, int, int, int]]:
    if len(data) < 128 or data[:4] != b"DDS ":
        return None
    height, width = struct.unpack_from("<II", data, 12)
    fourcc = data[84:88]
    offset = 128
    if fourcc == b"DX10":
        if len(data) < 148:
            return None
        dxgi = struct.unpack_from("<I", data, 128)[0]
        offset = 148
        name = {70: "BC1", 71: "BC1", 72: "BC1", 73: "BC2", 74: "BC2", 75: "BC2",
                76: "BC3", 77: "BC3", 78: "BC3"}.get(dxgi)
        if name is None:
            return None
        return name, width, height, offset
    name = {b"DXT1": "BC1", b"DXT3": "BC2", b"DXT5": "BC3"}.get(fourcc)
    if name is None:
        rgb_bits = struct.unpack_from("<I", data, 88)[0]
        if rgb_bits == 32:
            return "RGBA8", width, height, offset
        return None
    return name, width, height, offset


def _decode_bc4_plane(raw: np.ndarray) -> np.ndarray:
    a0 = raw[:, 0].astype(np.float32)
    a1 = raw[:, 1].astype(np.float32)
    bits = np.zeros(raw.shape[0], dtype=np.uint64)
    for i in range(6):
        bits |= raw[:, 2 + i].astype(np.uint64) << np.uint64(8 * i)
    shifts = (3 * np.arange(16, dtype=np.uint64)).reshape(1, 16)
    codes = ((bits[:, None] >> shifts) & np.uint64(7)).astype(np.intp)

    pal = np.empty((raw.shape[0], 8), dtype=np.float32)
    pal[:, 0] = a0
    pal[:, 1] = a1
    seven = a0 > a1
    for i in range(1, 7):
        pal[:, 1 + i] = np.where(
            seven,
            ((7 - i) * a0 + i * a1) / 7.0,
            ((5 - i) * a0 + i * a1) / 5.0 if i <= 5 else 0.0,
        )
    pal[~seven, 6] = 0.0
    pal[~seven, 7] = 255.0
    out = np.take_along_axis(pal, codes, axis=1)
    return np.clip(out + 0.5, 0, 255).astype(np.uint8)


def _decode_bc3_to_rgba(data: bytes, width: int, height: int) -> Optional[np.ndarray]:
    if width <= 0 or height <= 0 or width % 4 or height % 4:
        return None
    blocks_x, blocks_y = width // 4, height // 4
    n_blocks = blocks_x * blocks_y
    expected = n_blocks * 16
    if len(data) < expected:
        return None
    raw = np.frombuffer(data, dtype=np.uint8, count=expected).reshape(n_blocks, 16)
    # BC3 always uses four-color interpolation.
    rgb = decode_bc1_to_rgba(
        np.ascontiguousarray(raw[:, 8:]).tobytes(),
        width,
        height,
        force_four_color=True,
    )
    if rgb is None:
        return None
    alpha = _decode_bc4_plane(raw[:, :8])
    a_img = (alpha.reshape(blocks_y, blocks_x, 4, 4)
             .transpose(0, 2, 1, 3)
             .reshape(height, width))
    out = rgb.copy()
    out[:, :, 3] = a_img
    return out


def decode_dds_rgba(path: str) -> Optional[np.ndarray]:
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    header = _read_dds_header(data)
    if header is None:
        return None
    fmt, width, height, offset = header
    payload = data[offset:]
    if fmt == "BC1":
        return decode_bc1_to_rgba(payload, width, height)
    if fmt == "BC3":
        return _decode_bc3_to_rgba(payload, width, height)
    if fmt == "RGBA8":
        expected = width * height * 4
        if len(payload) < expected:
            return None
        return np.frombuffer(payload, dtype=np.uint8, count=expected).reshape(height, width, 4).copy()
    return None


def _box_downscale(rgba: np.ndarray, out_px: int) -> np.ndarray:
    h, w = rgba.shape[:2]
    if h == out_px and w == out_px:
        return rgba
    if h < out_px or w < out_px or h % out_px or w % out_px:
        ys = (np.arange(out_px) * h // out_px).astype(np.intp)
        xs = (np.arange(out_px) * w // out_px).astype(np.intp)
        return rgba[np.ix_(ys, xs)]
    f = h // out_px
    acc = rgba.reshape(out_px, f, out_px, f, rgba.shape[2]).astype(np.float32).mean(axis=(1, 3))
    return np.clip(acc + 0.5, 0, 255).astype(np.uint8)


def _dx_normal_to_opengl(rgba: np.ndarray) -> np.ndarray:
    out = rgba.copy()
    nx = rgba[:, :, 0].astype(np.float32) / 255.0 * 2.0 - 1.0
    ny = (1.0 - rgba[:, :, 1].astype(np.float32) / 255.0) * 2.0 - 1.0
    nz = np.sqrt(np.clip(1.0 - nx * nx - ny * ny, 0.0, 1.0))
    out[:, :, 1] = np.clip((ny * 0.5 + 0.5) * 255.0 + 0.5, 0, 255).astype(np.uint8)
    out[:, :, 2] = np.clip((nz * 0.5 + 0.5) * 255.0 + 0.5, 0, 255).astype(np.uint8)
    return out


def compute_atlas_layout(n_slices: int, slice_px: int, gutter_px: Optional[int] = None) -> Dict:
    if gutter_px is None:
        gutter_px = max(8, slice_px // 32)
    cols = int(np.ceil(np.sqrt(max(1, n_slices))))
    rows = int(np.ceil(n_slices / cols))
    cell_px = slice_px + 2 * gutter_px
    return {
        "version": DETAIL_VERSION,
        "n_slices": int(n_slices),
        "slice_px": int(slice_px),
        "gutter_px": int(gutter_px),
        "cell_px": int(cell_px),
        "cols": int(cols),
        "rows": int(rows),
        "atlas_w": int(cols * cell_px),
        "atlas_h": int(rows * cell_px),
    }


def _pack_atlas(slice_paths: Sequence[str], layout: Dict, normal_mode: bool) -> Optional[np.ndarray]:
    cell = layout["cell_px"]
    g = layout["gutter_px"]
    slice_px = layout["slice_px"]
    atlas = np.zeros((layout["atlas_h"], layout["atlas_w"], 4), dtype=np.uint8)
    got_any = False
    for s, path in enumerate(slice_paths):
        rgba = decode_dds_rgba(path) if path else None
        if rgba is None:
            continue
        rgba = _box_downscale(rgba, slice_px)
        if normal_mode:
            rgba = _dx_normal_to_opengl(rgba)
        else:
            rgba = rgba.copy()
            rgba[:, :, 3] = 255
        padded = np.pad(rgba, ((g, g), (g, g), (0, 0)), mode="wrap")
        row, col = divmod(s, layout["cols"])
        atlas[row * cell:(row + 1) * cell, col * cell:(col + 1) * cell] = padded
        got_any = True
    return atlas if got_any else None


def pack_world_detail_atlases(
    hub_name: str,
    layers,
    out_dir: str,
    slice_px: int = 1024,
    skip_existing: bool = True,
) -> Optional[Dict]:
    """Pack terrain-layer DDS files into diffuse and normal atlases."""
    diffuse_paths = [getattr(l, "diffuse_dds", "") or "" for l in layers]
    normal_paths = [getattr(l, "normal_dds", "") or "" for l in layers]
    n = len(diffuse_paths)
    if n == 0 or not any(diffuse_paths):
        return None

    layout = compute_atlas_layout(n, slice_px)
    layout["has_normals"] = bool(any(normal_paths))
    os.makedirs(out_dir, exist_ok=True)
    d_path = os.path.join(out_dir, f"{hub_name}.detail_atlas_d{slice_px}.png")
    n_path = os.path.join(out_dir, f"{hub_name}.detail_atlas_n{slice_px}.png") if layout["has_normals"] else ""
    j_path = os.path.join(out_dir, f"{hub_name}.detail_atlas{slice_px}.json")

    src_mtime = _max_mtime(diffuse_paths + normal_paths)
    outputs_fresh = _fresh(d_path, src_mtime) and (not n_path or _fresh(n_path, src_mtime))
    if skip_existing and outputs_fresh and os.path.isfile(j_path):
        try:
            with open(j_path, "r", encoding="utf-8") as f:
                cached = json.load(f)
            if cached.get("version") == DETAIL_VERSION and cached.get("n_slices") == n:
                return {"diffuse": d_path, "normal": n_path, "json": j_path, "layout": cached}
        except Exception:
            pass

    atlas_d = _pack_atlas(diffuse_paths, layout, normal_mode=False)
    if atlas_d is None:
        return None
    write_png(d_path, layout["atlas_w"], layout["atlas_h"], 6, 8, atlas_d.tobytes())
    del atlas_d

    if n_path:
        atlas_n = _pack_atlas(normal_paths, layout, normal_mode=True)
        if atlas_n is None:
            layout["has_normals"] = False
            n_path = ""
        else:
            write_png(n_path, layout["atlas_w"], layout["atlas_h"], 6, 8, atlas_n.tobytes())
            del atlas_n

    with open(j_path, "w", encoding="utf-8") as f:
        json.dump(layout, f, indent=1)
    return {"diffuse": d_path, "normal": n_path, "json": j_path, "layout": layout}


def normalize_layer_params(params_rows, layer_count: int) -> List[Dict[str, float]]:
    rows: List[Dict[str, float]] = []
    for i in range(layer_count):
        row = dict(DEFAULT_LAYER_PARAMS)
        if params_rows and i < len(params_rows) and isinstance(params_rows[i], dict):
            for key in row:
                try:
                    row[key] = float(params_rows[i].get(key, row[key]))
                except (TypeError, ValueError):
                    pass
        rows.append(row)
    return rows


def build_tile_detail_maps(
    texture_buffer: str,
    heightmap_buffer: str,
    res: int,
    tile_size: float,
    elev_range: float,
    params_rows,
    layer_count: int = 32,
    tint_buffer: str = "",
    skip_existing: bool = True,
) -> Optional[Dict]:
    """Write control, parameter, normal, and tint maps for one tile."""
    res = int(res)
    if res <= 0 or not texture_buffer or not os.path.isfile(texture_buffer):
        return None

    control_path = texture_buffer + ".detail_control.png"
    params_path = texture_buffer + ".detail_params.png"
    params2_path = texture_buffer + ".detail_params2.png"
    normal_path = (heightmap_buffer + ".detail_normal.png") if heightmap_buffer else ""
    tint_path = (texture_buffer + ".detail_tint.png") if tint_buffer else ""

    src_mtime = _max_mtime([texture_buffer, heightmap_buffer, tint_buffer])
    result: Dict = {
        "control": control_path,
        "params": params_path,
        "params2": params2_path,
        "normal": normal_path,
        "tint": tint_path,
        "res": res,
    }

    control = np.fromfile(texture_buffer, dtype="<u2")
    if control.size != res * res:
        return None
    control = control.reshape(res, res)
    overlay = (control & 0x1F).astype(np.uint8)
    bkgrnd = ((control >> 5) & 0x1F).astype(np.uint8)
    slope_idx = ((control >> 10) & 0x07).astype(np.uint8)
    scale_idx = ((control >> 13) & 0x07).astype(np.uint8)
    hole = control == 0
    result["has_holes"] = bool(hole.any())
    result["hole_count"] = int(hole.sum())

    if not (skip_existing and _fresh(control_path, src_mtime)):
        rgba = np.empty((res, res, 4), dtype=np.uint8)
        rgba[:, :, 0] = overlay
        rgba[:, :, 1] = bkgrnd
        rgba[:, :, 2] = scale_idx
        rgba[:, :, 3] = np.where(hole, 0, 255).astype(np.uint8)
        write_png(control_path, res, res, 6, 8, np.flipud(rgba).tobytes())

    rows = normalize_layer_params(params_rows, max(1, int(layer_count)))
    if not (skip_existing and _fresh(params_path, src_mtime) and _fresh(params2_path, src_mtime)):
        def lut(field):
            return np.asarray([r[field] for r in rows], dtype=np.float32)

        ov_i = np.clip(overlay.astype(np.intp) - 1, 0, len(rows) - 1)
        bg_i = np.clip(bkgrnd.astype(np.intp) - 1, 0, len(rows) - 1)
        thr = np.asarray(SLOPE_THRESHOLD_LUT, np.float32)[slope_idx]
        sharp = np.clip(lut("blend_sharpness")[ov_i] / PARAMS_SHARPNESS_SCALE, 0.0, 1.0)
        base_damp = np.clip(lut("slope_base_dampening")[bg_i], 0.0, 1.0)
        norm_damp = np.clip(lut("slope_normal_dampening")[bg_i], 0.0, 1.0)
        params = np.stack([thr, sharp, base_damp, norm_damp], axis=-1)
        params8 = np.clip(params * 255.0 + 0.5, 0, 255).astype(np.uint8)
        write_png(params_path, res, res, 6, 8, np.flipud(params8).tobytes())

        spec_ov = np.clip(lut("specularity")[ov_i], 0.0, 1.0)
        spec_bg = np.clip(lut("specularity")[bg_i], 0.0, 1.0)
        falloff = np.clip(lut("falloff")[ov_i], 0.0, 1.0)
        params2 = np.stack([spec_ov, spec_bg, falloff, np.ones_like(falloff)], axis=-1)
        params2_8 = np.clip(params2 * 255.0 + 0.5, 0, 255).astype(np.uint8)
        write_png(params2_path, res, res, 6, 8, np.flipud(params2_8).tobytes())

    if normal_path and not (skip_existing and _fresh(normal_path, src_mtime)):
        try:
            height = np.fromfile(heightmap_buffer, dtype="<u2")
        except OSError:
            height = np.zeros(0)
        if height.size == res * res:
            h_m = height.reshape(res, res).astype(np.float32) * (float(elev_range) / 65535.0)
            spacing = float(tile_size) / max(res - 1, 1)
            dz_dy, dz_dx = np.gradient(h_m, spacing if spacing > 0 else 1.0)
            nz = np.ones_like(h_m)
            inv_len = 1.0 / np.sqrt(dz_dx * dz_dx + dz_dy * dz_dy + 1.0)
            normal = np.stack([-dz_dx * inv_len, -dz_dy * inv_len, nz * inv_len], axis=-1)
            enc = np.clip((normal * 0.5 + 0.5) * 65535.0 + 0.5, 0, 65535).astype(">u2")
            write_png(normal_path, res, res, 2, 16, np.flipud(enc).tobytes())
        else:
            result["normal"] = ""
    elif not normal_path:
        result["normal"] = ""

    if tint_path and not (skip_existing and _fresh(tint_path, src_mtime)):
        rgba = decode_tintmap_file_to_rgba(tint_buffer, target_res_px=None)
        if rgba is not None:
            write_png(tint_path, rgba.shape[1], rgba.shape[0], 6, 8,
                      np.ascontiguousarray(rgba).tobytes())
        else:
            result["tint"] = ""
    elif tint_path and not os.path.isfile(tint_path):
        result["tint"] = ""

    return result


def build_fullmap_detail_maps(
    texture_buffers,
    source_res: int,
    x_tiles: int,
    y_tiles: int,
    params_rows,
    out_dir: str,
    hub_name: str,
    *,
    layer_count: int = 32,
    target_res: int = 8192,
    tint_path: str = "",
    skip_existing: bool = True,
) -> Optional[Dict]:
    """Build capped, tile-aligned control maps for the full-map mesh."""
    source_res = int(source_res)
    x_tiles = int(x_tiles)
    y_tiles = int(y_tiles)
    if source_res <= 0 or x_tiles <= 0 or y_tiles <= 0 or x_tiles != y_tiles:
        return None

    valid = {
        (int(x), int(y)): str(path)
        for (x, y), path in (texture_buffers or {}).items()
        if path and os.path.isfile(path)
    }
    if not valid:
        return None

    tile_px = min(source_res, max(1, int(target_res) // x_tiles))
    map_res = tile_px * x_tiles
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.join(out_dir, f"{hub_name}.detail_full_{map_res}")
    control_path = stem + "_control.png"
    params_path = stem + "_params.png"
    params2_path = stem + "_params2.png"
    source_paths = list(valid.values())
    src_mtime = _max_mtime(source_paths)
    rows = normalize_layer_params(params_rows, max(1, int(layer_count)))
    luts = {
        field: np.asarray([row[field] for row in rows], dtype=np.float32)
        for field in DEFAULT_LAYER_PARAMS
    }
    sample_idx = (np.arange(tile_px, dtype=np.intp) * source_res // tile_px)
    hole_count = 0

    def read_control(path):
        data = np.fromfile(path, dtype="<u2")
        if data.size != source_res * source_res:
            return None
        data = data.reshape(source_res, source_res)
        holes = data == 0
        if tile_px != source_res:
            sampled = data[np.ix_(sample_idx, sample_idx)]
            if source_res % tile_px == 0:
                factor = source_res // tile_px
                holes = holes.reshape(tile_px, factor, tile_px, factor).any(axis=(1, 3))
            else:
                holes = holes[np.ix_(sample_idx, sample_idx)]
            data = sampled
        return data, holes

    for path in source_paths:
        raw = np.fromfile(path, dtype="<u2")
        if raw.size == source_res * source_res:
            hole_count += int((raw == 0).sum())
    missing_tiles = max(0, x_tiles * y_tiles - len(valid))
    has_holes = hole_count > 0 or missing_tiles > 0

    def build_image(kind):
        out = np.zeros((map_res, map_res, 4), dtype=np.uint8)
        for (x, y), path in valid.items():
            if not (0 <= x < x_tiles and 0 <= y < y_tiles):
                continue
            loaded = read_control(path)
            if loaded is None:
                continue
            control, holes = loaded
            overlay = (control & 0x1F).astype(np.uint8)
            bkgrnd = ((control >> 5) & 0x1F).astype(np.uint8)
            slope_idx = ((control >> 10) & 0x07).astype(np.uint8)
            target = np.empty((tile_px, tile_px, 4), dtype=np.uint8)
            if kind == "control":
                target[..., 0] = overlay
                target[..., 1] = bkgrnd
                target[..., 2] = ((control >> 13) & 0x07).astype(np.uint8)
                target[..., 3] = np.where(holes, 0, 255).astype(np.uint8)
            else:
                ov_i = np.clip(overlay.astype(np.intp) - 1, 0, len(rows) - 1)
                bg_i = np.clip(bkgrnd.astype(np.intp) - 1, 0, len(rows) - 1)
                if kind == "params":
                    values = (
                        np.asarray(SLOPE_THRESHOLD_LUT, np.float32)[slope_idx],
                        np.clip(luts["blend_sharpness"][ov_i]
                                / PARAMS_SHARPNESS_SCALE, 0.0, 1.0),
                        np.clip(luts["slope_base_dampening"][bg_i], 0.0, 1.0),
                        np.clip(luts["slope_normal_dampening"][bg_i], 0.0, 1.0),
                    )
                else:
                    values = (
                        np.clip(luts["specularity"][ov_i], 0.0, 1.0),
                        np.clip(luts["specularity"][bg_i], 0.0, 1.0),
                        np.clip(luts["falloff"][ov_i], 0.0, 1.0),
                        np.ones_like(overlay, dtype=np.float32),
                    )
                target[...] = np.clip(
                    np.stack(values, axis=-1) * 255.0 + 0.5, 0, 255
                ).astype(np.uint8)
            sy = slice(y * tile_px, (y + 1) * tile_px)
            sx = slice(x * tile_px, (x + 1) * tile_px)
            out[sy, sx] = target
        return out

    for kind, path in (
        ("control", control_path),
        ("params", params_path),
        ("params2", params2_path),
    ):
        if skip_existing and _fresh(path, src_mtime):
            continue
        pixels = build_image(kind)
        write_png(path, map_res, map_res, 6, 8, np.flipud(pixels).tobytes())
        del pixels

    return {
        "control": control_path,
        "params": params_path,
        "params2": params2_path,
        "normal": "",
        "tint": tint_path if tint_path and os.path.isfile(tint_path) else "",
        "res": map_res,
        "has_holes": has_holes,
        "hole_count": hole_count,
    }
