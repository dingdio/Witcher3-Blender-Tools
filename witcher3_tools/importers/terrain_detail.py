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

DETAIL_VERSION = 5

UV_SCALE_LUT = (0.333, 0.166, 0.05, 0.025, 0.0125, 0.0075, 0.00375, 0.0)
SLOPE_THRESHOLD_LUT = (0.0, 0.125, 0.25, 0.375, 0.5, 0.625, 0.75, 0.98)
OVERLAY_UV_SCALE = 0.333
TRIPLANAR_TIGHTEN = 0.576
PARAMS_SHARPNESS_SCALE = 2.0
TERRAIN_GAMMA = 2.2
SPECULAR_IOR_F0_SCALE = 0.08

DEFAULT_LAYER_PARAMS = {
    "blend_sharpness": 0.0,
    "slope_base_dampening": 0.0,
    "slope_normal_dampening": 0.5,
    "falloff": 0.0,
    "specularity": 0.0,
    "specularity_base": 0.0,
    "specularity_scale": 0.0,
}

_CONTROL_CORNERS = (
    ("SW", 0, 0),
    ("SE", 1, 0),
    ("NW", 0, 1),
    ("NE", 1, 1),
)


def _terrain_layer_name(path: str, layer_id: int) -> str:
    filename = str(path or "").replace("\\", "/").rsplit("/", 1)[-1]
    stem = os.path.splitext(filename)[0]
    return stem or f"Layer {int(layer_id)}"


def build_terrain_layer_metadata(layers) -> List[Dict]:
    """Return compact, serializable names/paths/params for atlas inspection."""
    metadata: List[Dict] = []
    for atlas_index, layer in enumerate(layers or []):
        layer_id = atlas_index + 1  # W2TER control IDs are one-based.
        diffuse_source = str(getattr(layer, "diffuse_source", "") or "")
        normal_source = str(getattr(layer, "normal_source", "") or "")
        diffuse_dds = str(getattr(layer, "diffuse_dds", "") or "")
        normal_dds = str(getattr(layer, "normal_dds", "") or "")
        display_path = diffuse_source or diffuse_dds
        row = {
            "id": layer_id,
            "atlas_index": atlas_index,
            "name": _terrain_layer_name(display_path, layer_id),
            "diffuse_source": diffuse_source,
            "normal_source": normal_source,
            "diffuse_dds": diffuse_dds,
            "normal_dds": normal_dds,
        }
        for field, default in DEFAULT_LAYER_PARAMS.items():
            try:
                row[field] = float(getattr(layer, field, default))
            except (TypeError, ValueError):
                row[field] = float(default)
        metadata.append(row)
    return metadata


def decode_terrain_control(control_value: int) -> Dict[str, object]:
    """Decode one terrain W2TER control word."""
    value = int(control_value) & 0xFFFF
    slope_index = (value >> 10) & 0x07
    scale_index = (value >> 13) & 0x07
    return {
        "control": value,
        "horizontal_id": value & 0x1F,
        "vertical_id": (value >> 5) & 0x1F,
        "slope_index": slope_index,
        "slope_threshold": float(SLOPE_THRESHOLD_LUT[slope_index]),
        "scale_index": scale_index,
        "vertical_uv_scale": float(UV_SCALE_LUT[scale_index]),
        "hole": value == 0,
    }


def load_tile_control_lattice(
    texture_buffer: str,
    res: int,
    *,
    positive_x_texture_buffer: str = "",
    positive_y_texture_buffer: str = "",
    positive_xy_texture_buffer: str = "",
) -> Optional[np.ndarray]:
    """Load a tile's control words with the same positive-edge stitching as its shader."""
    current = _read_square_u16(texture_buffer, int(res))
    if current is None:
        return None
    return _stitch_positive_edges(
        current,
        _read_square_u16(positive_x_texture_buffer, int(res)),
        _read_square_u16(positive_y_texture_buffer, int(res)),
        _read_square_u16(positive_xy_texture_buffer, int(res)),
    )


def inspect_terrain_control_lattice(
    control_lattice: np.ndarray,
    u: float,
    v: float,
    layer_metadata=None,
) -> Dict[str, object]:
    """Inspect the exact four shader control taps at a terrain UV position."""
    lattice = np.asarray(control_lattice)
    if lattice.ndim != 2 or lattice.shape[0] != lattice.shape[1] or lattice.shape[0] < 2:
        raise ValueError("Terrain control lattice must be a square (resolution + 1) array")

    res = lattice.shape[0] - 1
    grid_x = float(np.clip(float(u), 0.0, 1.0)) * res
    grid_y = float(np.clip(float(v), 0.0, 1.0)) * res
    cell_x = min(int(np.floor(grid_x)), res - 1)
    cell_y = min(int(np.floor(grid_y)), res - 1)
    frac_x = float(np.clip(grid_x - cell_x, 0.0, 1.0))
    frac_y = float(np.clip(grid_y - cell_y, 0.0, 1.0))
    weights = (
        (1.0 - frac_x) * (1.0 - frac_y),
        frac_x * (1.0 - frac_y),
        (1.0 - frac_x) * frac_y,
        frac_x * frac_y,
    )

    by_id = {}
    for row in layer_metadata or []:
        try:
            by_id[int(row.get("id", 0))] = dict(row)
        except (AttributeError, TypeError, ValueError):
            continue

    def layer_row(layer_id: int) -> Dict[str, object]:
        if layer_id <= 0:
            return {
                "id": 0,
                "atlas_index": -1,
                "name": "None / Hole",
                "diffuse_source": "",
                "normal_source": "",
                "diffuse_dds": "",
                "normal_dds": "",
                **DEFAULT_LAYER_PARAMS,
            }
        row = dict(by_id.get(layer_id) or {})
        row.setdefault("id", layer_id)
        row.setdefault("atlas_index", layer_id - 1)
        row.setdefault("name", f"Layer {layer_id}")
        for path_field in ("diffuse_source", "normal_source", "diffuse_dds", "normal_dds"):
            row.setdefault(path_field, "")
        for field, default in DEFAULT_LAYER_PARAMS.items():
            row.setdefault(field, default)
        return row

    taps = []
    for (corner, dx, dy), weight in zip(_CONTROL_CORNERS, weights):
        decoded = decode_terrain_control(lattice[cell_y + dy, cell_x + dx])
        decoded.update({
            "corner": corner,
            "x": cell_x + dx,
            "y": cell_y + dy,
            "weight": float(weight),
            "horizontal": layer_row(int(decoded["horizontal_id"])),
            "vertical": layer_row(int(decoded["vertical_id"])),
        })
        taps.append(decoded)

    active_taps = [tap for tap in taps if float(tap["weight"]) > 1e-8]

    def aggregate(branch: str) -> List[Dict[str, object]]:
        accumulated: Dict[int, Dict[str, object]] = {}
        for tap in active_taps:
            layer = tap[branch]
            layer_id = int(layer["id"])
            entry = accumulated.setdefault(layer_id, {"layer": layer, "weight": 0.0})
            entry["weight"] += float(tap["weight"])
        return sorted(accumulated.values(), key=lambda item: (-item["weight"], item["layer"]["id"]))

    def weighted_param(branch: str, field: str) -> float:
        return float(sum(
            float(tap["weight"]) * float(tap[branch].get(field, DEFAULT_LAYER_PARAMS[field]))
            for tap in active_taps
        ))

    effective = {
        "slope_threshold": float(sum(
            float(tap["weight"]) * float(tap["slope_threshold"])
            for tap in active_taps
        )),
        "blend_sharpness": weighted_param("horizontal", "blend_sharpness"),
        "slope_base_dampening": weighted_param("vertical", "slope_base_dampening"),
        "slope_normal_dampening": weighted_param("vertical", "slope_normal_dampening"),
        "hole_weight": float(sum(
            float(tap["weight"]) for tap in active_taps if tap["hole"]
        )),
    }
    return {
        "resolution": res,
        "grid": (grid_x, grid_y),
        "cell": (cell_x, cell_y),
        "fraction": (frac_x, frac_y),
        "taps": taps,
        "horizontal_layers": aggregate("horizontal"),
        "vertical_layers": aggregate("vertical"),
        "effective": effective,
    }


def terrain_specular_f0(specularity, roughness, specularity_base, falloff):
    """Evaluate the terrain material's direct dielectric Fresnel F0."""
    specularity = np.clip(np.asarray(specularity, dtype=np.float64), 0.0, 1.0)
    roughness = np.clip(np.asarray(roughness, dtype=np.float64), 0.0, 1.0)
    factor = ((1.0 - roughness) * np.asarray(falloff, dtype=np.float64)
              + 3.0 * (np.asarray(specularity_base, dtype=np.float64) - 0.5))
    return np.clip(np.power(specularity, TERRAIN_GAMMA) * factor, 0.0, 1.0)


def f0_to_ior(f0, max_ior=1000.0):
    """Convert direct F0 to Principled IOR while keeping the F0=1 limit finite."""
    max_ior = max(1.0, float(max_ior))
    f0 = np.clip(np.asarray(f0, dtype=np.float64), 0.0, 1.0)
    root = np.sqrt(f0)
    denominator = np.maximum(1.0 - root, 2.0 / (max_ior + 1.0))
    return np.minimum((1.0 + root) / denominator, max_ior)


def legacy_specular_ior_level(specularity, roughness, specularity_base, falloff):
    """Legacy 0.08-F0 Principled multiplier retained for older callers."""
    f0 = terrain_specular_f0(specularity, roughness, specularity_base, falloff)
    return np.clip(f0 / SPECULAR_IOR_F0_SCALE, 0.0, 1.0)


def overlay_gamma(diffuse, tint):
    """Apply component-wise Overlay in stored gamma space."""
    diffuse = np.asarray(diffuse, dtype=np.float64)
    tint = np.asarray(tint, dtype=np.float64)
    dark = 2.0 * diffuse * tint
    light = 1.0 - 2.0 * (1.0 - diffuse) * (1.0 - tint)
    return np.where(tint < 0.5, dark, light)


def gamma_to_linear(value):
    """Convert stored terrain color channels to linear values."""
    return np.power(np.clip(np.asarray(value, dtype=np.float64), 0.0, 1.0), TERRAIN_GAMMA)


def combine_tangent_normal(vertex_normal, texture_normal):
    """Transform a tangent-space normal into the vertex-normal frame."""
    vertex = np.asarray(vertex_normal, dtype=np.float64)
    texture = np.asarray(texture_normal, dtype=np.float64)
    vertex = vertex / np.maximum(np.linalg.norm(vertex, axis=-1, keepdims=True), 1e-12)
    tangent = np.zeros_like(vertex)
    tangent[..., 0] = 1.0
    tangent = tangent - vertex * vertex[..., 0, None]
    tangent = tangent / np.maximum(np.linalg.norm(tangent, axis=-1, keepdims=True), 1e-12)
    binormal = np.cross(vertex, tangent)
    return (texture[..., 0, None] * tangent
            + texture[..., 1, None] * binormal
            + texture[..., 2, None] * vertex)


def compute_slope_blend(normal, low_threshold, blend_sharpness):
    """Calculate a bounded slope blend from the surface normal."""
    normal = np.asarray(normal, dtype=np.float64)
    horizontal = np.linalg.norm(normal[..., :2], axis=-1)
    slope = np.clip(horizontal / np.maximum(normal[..., 2], 1e-12), 0.0, 1.0)
    low = np.asarray(low_threshold, dtype=np.float64)
    high = np.clip(low + np.asarray(blend_sharpness, dtype=np.float64), 0.0, 1.0)
    denominator = high - low
    with np.errstate(divide="ignore", invalid="ignore"):
        result = np.divide(slope - low, denominator)
    return np.clip(np.where(denominator > 0.0, result,
                            np.where(slope > low, 1.0, 0.0)), 0.0, 1.0)


def _max_mtime(paths: Sequence[str]) -> float:
    newest = 0.0
    for p in paths:
        if p:
            m = _safe_mtime(p)
            if m > newest:
                newest = m
    return newest


def _fresh(out_path: str, src_mtime: float) -> bool:
    return bool(out_path) and os.path.isfile(out_path) and _safe_mtime(out_path) >= src_mtime


def _read_square_u16(path: str, res: int) -> Optional[np.ndarray]:
    if not path:
        return None
    try:
        data = np.fromfile(path, dtype="<u2")
    except OSError:
        return None
    if data.size != int(res) * int(res):
        return None
    return data.reshape(int(res), int(res))


def _stitch_positive_edges(current, right=None, up=None, diagonal=None) -> np.ndarray:
    current = np.asarray(current)
    if current.ndim < 2 or current.shape[0] != current.shape[1]:
        raise ValueError("Terrain detail map must be square")

    shape = current.shape
    for label, neighbor in (("right", right), ("up", up), ("diagonal", diagonal)):
        if neighbor is not None and np.asarray(neighbor).shape != shape:
            raise ValueError(f"Terrain {label} detail map shape does not match the current tile")

    res = current.shape[0]
    stitched = np.empty((res + 1, res + 1) + current.shape[2:], dtype=current.dtype)
    stitched[:res, :res] = current
    stitched[:res, res] = np.asarray(right)[:, 0] if right is not None else current[:, -1]
    stitched[res, :res] = np.asarray(up)[0, :] if up is not None else current[-1, :]
    stitched[res, res] = (
        np.asarray(diagonal)[0, 0] if diagonal is not None else current[-1, -1]
    )
    return stitched


def _height_normals(height, spacing: float) -> np.ndarray:
    height = np.asarray(height, dtype=np.float32)
    step = float(spacing) if float(spacing) > 0 else 1.0
    if min(height.shape[:2]) > 1:
        dz_dy, dz_dx = np.gradient(height, step)
    else:
        dz_dx = np.zeros_like(height)
        dz_dy = np.zeros_like(height)
    inv_len = 1.0 / np.sqrt(dz_dx * dz_dx + dz_dy * dz_dy + 1.0)
    return np.stack([-dz_dx * inv_len, -dz_dy * inv_len, inv_len], axis=-1)


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
    out[:, :, 1] = 255 - rgba[:, :, 1]
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
    slice_px: int = 2048,
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
    positive_x_texture_buffer: str = "",
    positive_y_texture_buffer: str = "",
    positive_xy_texture_buffer: str = "",
    positive_x_heightmap_buffer: str = "",
    positive_y_heightmap_buffer: str = "",
    positive_xy_heightmap_buffer: str = "",
    positive_x_tint_buffer: str = "",
    positive_y_tint_buffer: str = "",
    positive_xy_tint_buffer: str = "",
) -> Optional[Dict]:
    """Write neighbor-padded control, parameter, normal, and tint maps for one tile."""
    res = int(res)
    if res <= 0 or not texture_buffer or not os.path.isfile(texture_buffer):
        return None

    detail_stem = f".detail_v{DETAIL_VERSION}"
    control_path = texture_buffer + detail_stem + "_control.png"
    params_path = texture_buffer + detail_stem + "_params.png"
    params2_path = texture_buffer + detail_stem + "_params2.png"
    params3_path = texture_buffer + detail_stem + "_params3.png"
    normal_path = (
        heightmap_buffer + detail_stem + "_normal.png" if heightmap_buffer else ""
    )
    tint_path = texture_buffer + detail_stem + "_tint.png" if tint_buffer else ""

    neighbor_paths = [
        positive_x_texture_buffer,
        positive_y_texture_buffer,
        positive_xy_texture_buffer,
        positive_x_heightmap_buffer,
        positive_y_heightmap_buffer,
        positive_xy_heightmap_buffer,
        positive_x_tint_buffer,
        positive_y_tint_buffer,
        positive_xy_tint_buffer,
    ]
    src_mtime = _max_mtime([texture_buffer, heightmap_buffer, tint_buffer, *neighbor_paths])
    result: Dict = {
        "control": control_path,
        "params": params_path,
        "params2": params2_path,
        "params3": params3_path,
        "normal": normal_path,
        "tint": tint_path,
        "res": res,
        "map_res": res + 1,
    }

    current_control = _read_square_u16(texture_buffer, res)
    if current_control is None:
        return None
    control = _stitch_positive_edges(
        current_control,
        _read_square_u16(positive_x_texture_buffer, res),
        _read_square_u16(positive_y_texture_buffer, res),
        _read_square_u16(positive_xy_texture_buffer, res),
    )
    map_res = res + 1
    overlay = (control & 0x1F).astype(np.uint8)
    bkgrnd = ((control >> 5) & 0x1F).astype(np.uint8)
    slope_idx = ((control >> 10) & 0x07).astype(np.uint8)
    scale_idx = ((control >> 13) & 0x07).astype(np.uint8)
    hole = control == 0
    result["has_holes"] = bool(hole.any())
    result["hole_count"] = int((current_control == 0).sum())

    if not (skip_existing and _fresh(control_path, src_mtime)):
        rgba = np.empty((map_res, map_res, 4), dtype=np.uint8)
        rgba[:, :, 0] = overlay
        rgba[:, :, 1] = bkgrnd
        rgba[:, :, 2] = scale_idx
        rgba[:, :, 3] = np.where(hole, 0, 255).astype(np.uint8)
        write_png(control_path, map_res, map_res, 6, 8, np.flipud(rgba).tobytes())

    rows = normalize_layer_params(params_rows, max(1, int(layer_count)))
    if not (skip_existing
            and _fresh(params_path, src_mtime)
            and _fresh(params2_path, src_mtime)
            and _fresh(params3_path, src_mtime)):
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
        write_png(params_path, map_res, map_res, 6, 8, np.flipud(params8).tobytes())

        spec_ov = np.clip(lut("specularity")[ov_i], 0.0, 1.0)
        spec_bg = np.clip(lut("specularity")[bg_i], 0.0, 1.0)
        base_ov = np.clip(lut("specularity_base")[ov_i], 0.0, 1.0)
        base_bg = np.clip(lut("specularity_base")[bg_i], 0.0, 1.0)
        params2 = np.stack([spec_ov, spec_bg, base_ov, base_bg], axis=-1)
        params2_8 = np.clip(params2 * 255.0 + 0.5, 0, 255).astype(np.uint8)
        write_png(params2_path, map_res, map_res, 6, 8, np.flipud(params2_8).tobytes())

        falloff_ov = np.clip(lut("falloff")[ov_i], 0.0, 1.0)
        falloff_bg = np.clip(lut("falloff")[bg_i], 0.0, 1.0)
        scale_ov = np.clip(lut("specularity_scale")[ov_i], 0.0, 1.0)
        scale_bg = np.clip(lut("specularity_scale")[bg_i], 0.0, 1.0)
        params3 = np.stack([falloff_ov, falloff_bg, scale_ov, scale_bg], axis=-1)
        params3_8 = np.clip(params3 * 255.0 + 0.5, 0, 255).astype(np.uint8)
        write_png(params3_path, map_res, map_res, 6, 8, np.flipud(params3_8).tobytes())

    if normal_path and not (skip_existing and _fresh(normal_path, src_mtime)):
        try:
            height = _read_square_u16(heightmap_buffer, res)
        except (OSError, ValueError):
            height = None
        if height is not None:
            height_scale = float(elev_range) / 65535.0
            h_m = height.astype(np.float32) * height_scale
            right_h = _read_square_u16(positive_x_heightmap_buffer, res)
            up_h = _read_square_u16(positive_y_heightmap_buffer, res)
            diagonal_h = _read_square_u16(positive_xy_heightmap_buffer, res)
            right_h = right_h.astype(np.float32) * height_scale if right_h is not None else None
            up_h = up_h.astype(np.float32) * height_scale if up_h is not None else None
            diagonal_h = (
                diagonal_h.astype(np.float32) * height_scale if diagonal_h is not None else None
            )
            spacing = float(tile_size) / max(res, 1)
            normal = _stitch_positive_edges(
                _height_normals(h_m, spacing),
                _height_normals(right_h, spacing) if right_h is not None else None,
                _height_normals(up_h, spacing) if up_h is not None else None,
                _height_normals(diagonal_h, spacing) if diagonal_h is not None else None,
            )
            enc = np.clip((normal * 0.5 + 0.5) * 65535.0 + 0.5, 0, 65535).astype(">u2")
            write_png(normal_path, map_res, map_res, 2, 16, np.flipud(enc).tobytes())
        else:
            result["normal"] = ""
    elif not normal_path:
        result["normal"] = ""

    if tint_path and not (skip_existing and _fresh(tint_path, src_mtime)):
        rgba = decode_tintmap_file_to_rgba(tint_buffer, target_res_px=None)
        if rgba is not None:
            tint_res = int(rgba.shape[0])
            if rgba.ndim != 3 or rgba.shape[1] != tint_res:
                rgba = None
            else:
                def read_tint(path):
                    neighbor = decode_tintmap_file_to_rgba(path, target_res_px=None) if path else None
                    return neighbor if neighbor is not None and neighbor.shape == rgba.shape else None

                right_tint = read_tint(positive_x_tint_buffer)
                up_tint = read_tint(positive_y_tint_buffer)
                diagonal_tint = read_tint(positive_xy_tint_buffer)
                rgba = np.flipud(_stitch_positive_edges(
                    np.flipud(rgba),
                    np.flipud(right_tint) if right_tint is not None else None,
                    np.flipud(up_tint) if up_tint is not None else None,
                    np.flipud(diagonal_tint) if diagonal_tint is not None else None,
                ))
                result["tint_res"] = tint_res
                result["tint_map_res"] = tint_res + 1
                write_png(tint_path, tint_res + 1, tint_res + 1, 6, 8,
                          np.ascontiguousarray(rgba).tobytes())
        if rgba is None:
            result["tint"] = ""
    elif tint_path and not os.path.isfile(tint_path):
        result["tint"] = ""
    elif tint_path:
        rgba = decode_tintmap_file_to_rgba(tint_buffer, target_res_px=None)
        if rgba is not None and rgba.ndim == 3 and rgba.shape[0] == rgba.shape[1]:
            result["tint_res"] = int(rgba.shape[0])
            result["tint_map_res"] = int(rgba.shape[0]) + 1

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
    params3_path = stem + "_params3.png"
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
                elif kind == "params2":
                    values = (
                        np.clip(luts["specularity"][ov_i], 0.0, 1.0),
                        np.clip(luts["specularity"][bg_i], 0.0, 1.0),
                        np.clip(luts["specularity_base"][ov_i], 0.0, 1.0),
                        np.clip(luts["specularity_base"][bg_i], 0.0, 1.0),
                    )
                else:
                    values = (
                        np.clip(luts["falloff"][ov_i], 0.0, 1.0),
                        np.clip(luts["falloff"][bg_i], 0.0, 1.0),
                        np.clip(luts["specularity_scale"][ov_i], 0.0, 1.0),
                        np.clip(luts["specularity_scale"][bg_i], 0.0, 1.0),
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
        ("params3", params3_path),
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
        "params3": params3_path,
        "normal": "",
        "tint": tint_path if tint_path and os.path.isfile(tint_path) else "",
        "res": map_res,
        "has_holes": has_holes,
        "hole_count": hole_count,
    }
