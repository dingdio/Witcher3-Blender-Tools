"""Extract the W3 per-world terrain texture set for the Unreal blend material.

The terrain's `.w2w` `CClipMap` references a terrain material graph (`.w2mg`)
whose `diffuse`/`normal` `CMaterialParameterTextureArray` params point at two
`.texarray` atlases (one slice per terrain layer). Per-pixel control maps
(overlay/bkgrnd layer indices + a blend weight) come from the assembled
`.w2ter` texturemap.

This module resolves those, splits the atlases into per-layer DDS slices, and
emits the control maps as raw 8-bit index images, so world_bundle can hand UE a
faithful weight-blended landscape material.
"""

from __future__ import annotations

import os
import struct
import zlib
from dataclasses import dataclass, field
from typing import Optional

import numpy as np


@dataclass
class TerrainLayer:
    index: int
    diffuse_dds: str = ""
    normal_dds: str = ""
    blend_sharpness: float = 0.1
    slope_base_dampening: float = 0.0
    slope_normal_dampening: float = 0.5
    falloff: float = 0.0
    specularity: float = 0.0
    specularity_base: float = 0.0
    specularity_scale: float = 0.0


@dataclass
class TerrainMaterialSet:
    material_path: str = ""           # depot path of the terrain .w2mg
    diffuse_texarray: str = ""        # depot path
    normal_texarray: str = ""         # depot path
    layers: list[TerrainLayer] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def layer_count(self) -> int:
        return len(self.layers)


def _handle_depot_path(prop) -> str:
    if prop is None:
        return ""
    for handle in getattr(prop, "Handles", []) or []:
        depot = getattr(handle, "DepotPath", None)
        if depot:
            return str(depot)
    return ""


def _chunk_handle_path(chunk, *names: str) -> str:
    for name in names:
        try:
            prop = chunk.GetVariableByName(name)
        except Exception:
            prop = None
        depot = _handle_depot_path(prop)
        if depot:
            return depot
    for prop in getattr(chunk, "PROPS", []) or []:
        depot = _handle_depot_path(prop)
        if depot:
            return depot
    return ""


def get_terrain_material_path(world) -> str:
    clip = getattr(world, "terrainClipMap", None)
    if clip is None:
        return ""
    try:
        return _handle_depot_path(clip.GetVariableByName("material"))
    except Exception:
        return ""


def _vector_component(vector, name: str, default: float = 0.0) -> float:
    if vector is None:
        return default
    try:
        return float(vector.GetVariableByName(name).Value)
    except Exception:
        return default


def get_terrain_texture_params(world) -> list[dict[str, float]]:
    """terrain texture params from CClipMap.textureParams.

    For slope blending:
      val.X  = horizontal/vertical blend sharpness
      val.Y  = slope base dampening
      val.Z  = slope normal dampening, packed for the shader as Z * 0.5 + 0.5
      val.W  = falloff
      val2.X = specularity
      val2.Y = specularity base
      val2.Z = specularity scale
    """
    clip = getattr(world, "terrainClipMap", None)
    if clip is None:
        return []
    try:
        params = clip.GetVariableByName("textureParams")
    except Exception:
        return []
    elements = getattr(params, "elements", None) or getattr(params, "PROPS", None) or []
    result: list[dict[str, float]] = []
    for element in elements:
        try:
            val = element.GetVariableByName("val")
        except Exception:
            val = None
        try:
            val2 = element.GetVariableByName("val2")
        except Exception:
            val2 = None
        result.append({
            "blend_sharpness": _vector_component(val, "X", 0.1),
            "slope_base_dampening": _vector_component(val, "Y", 0.0),
            "slope_normal_dampening": _vector_component(val, "Z", 0.0) * 0.5 + 0.5,
            "falloff": _vector_component(val, "W", 0.0),
            "specularity": _vector_component(val2, "X", 0.0),
            "specularity_base": _vector_component(val2, "Y", 0.0),
            "specularity_scale": _vector_component(val2, "Z", 0.0),
        })
    return result


def get_terrain_atlases(w2mg_abs_path: str) -> tuple[str, str]:
    """(diffuse_texarray_depot, normal_texarray_depot) from the terrain .w2mg."""
    from ..CR2W import CR2W_file

    if not w2mg_abs_path or not os.path.isfile(w2mg_abs_path):
        return "", ""
    cr2w = CR2W_file.read_CR2W(w2mg_abs_path)
    diffuse = normal = ""
    for chunk in getattr(getattr(cr2w, "CHUNKS", None), "CHUNKS", []) or []:
        if getattr(chunk, "Type", "") != "CMaterialParameterTextureArray":
            continue
        try:
            name_var = chunk.GetVariableByName("parameterName")
            par_name = str(getattr(getattr(name_var, "Index", None), "String", "") or "").lower()
        except Exception:
            par_name = ""
        path = _chunk_handle_path(chunk, "textureArray", "texture", "textures")
        if not path:
            continue
        if "normal" in par_name and not normal:
            normal = path
        elif ("diffuse" in par_name or "color" in par_name or "albedo" in par_name) and not diffuse:
            diffuse = path
        elif not diffuse:
            diffuse = path
    return diffuse, normal


def get_texture_array_bitmap_paths(texarray_abs_path: str) -> list[str]:
    """Return source texture paths from a REDkit/source CTextureArray."""
    from ..CR2W import CR2W_file

    if not texarray_abs_path or not os.path.isfile(texarray_abs_path):
        return []
    try:
        cr2w = CR2W_file.read_CR2W(texarray_abs_path)
    except Exception:
        return []

    paths: list[str] = []
    for chunk in getattr(getattr(cr2w, "CHUNKS", None), "CHUNKS", []) or []:
        if getattr(chunk, "Type", "") != "CTextureArray":
            continue
        try:
            bitmaps = chunk.GetVariableByName("bitmaps")
        except Exception:
            bitmaps = None
        for element in getattr(bitmaps, "More", []) or []:
            for prop in getattr(element, "MoreProps", []) or []:
                if getattr(prop, "theName", "") != "m_texture":
                    continue
                index = getattr(prop, "Index", None)
                path = str(getattr(index, "Path", "") or "").strip()
                if path:
                    paths.append(path)
                    break
    return paths


def _resolved_existing_repo_files(repo_paths: list[str]) -> list[str]:
    from ..CR2W.common_blender import repo_file, win_safe_path

    resolved: list[str] = []
    for path in repo_paths:
        candidate = repo_file(path)
        try:
            if candidate and os.path.isfile(win_safe_path(candidate)):
                resolved.append(candidate)
        except Exception:
            if candidate and os.path.isfile(candidate):
                resolved.append(candidate)
    return resolved


def extract_terrain_material_set(world) -> TerrainMaterialSet:
    """Resolve the terrain material set and split both atlases into DDS slices."""
    from ..CR2W.common_blender import repo_file
    from ..CR2W.texture_converters import convert_texarray_to_dds

    result = TerrainMaterialSet()
    result.material_path = get_terrain_material_path(world)
    if not result.material_path:
        result.warnings.append("No terrain material graph (.w2mg) referenced by the clipmap.")
        return result

    w2mg_abs = repo_file(result.material_path)
    if not w2mg_abs or not os.path.isfile(w2mg_abs):
        result.warnings.append(f"Terrain material graph not found on disk: {result.material_path}")
        return result

    result.diffuse_texarray, result.normal_texarray = get_terrain_atlases(w2mg_abs)
    if not result.diffuse_texarray:
        result.warnings.append("Terrain material graph has no diffuse texture array.")
        return result

    diffuse_abs = repo_file(result.diffuse_texarray)
    normal_abs = repo_file(result.normal_texarray) if result.normal_texarray else ""

    diffuse_slices = convert_texarray_to_dds(diffuse_abs) or []
    if not diffuse_slices:
        diffuse_slices = _resolved_existing_repo_files(get_texture_array_bitmap_paths(diffuse_abs))
    normal_slices = []
    if result.normal_texarray:
        normal_slices = convert_texarray_to_dds(normal_abs) or []
        if not normal_slices:
            normal_slices = _resolved_existing_repo_files(get_texture_array_bitmap_paths(normal_abs))

    texture_params = get_terrain_texture_params(world)
    count = len(diffuse_slices)
    for i in range(count):
        layer = TerrainLayer(index=i)
        layer.diffuse_dds = diffuse_slices[i]
        if i < len(normal_slices):
            layer.normal_dds = normal_slices[i]
        if i < len(texture_params):
            for key, value in texture_params[i].items():
                setattr(layer, key, value)
        result.layers.append(layer)

    if not result.layers:
        result.warnings.append("Diffuse texture array produced no slices.")
    return result


# --- raw 8-bit control-map images (overlay/bkgrnd indices + blend weight) -----

def _write_gray8_png(path: str, arr: np.ndarray) -> None:
    """Minimal 8-bit grayscale PNG (filter 0 every row), matching the decoder in
    terrain_unreal.read_height_png_u16's family."""
    height, width = arr.shape
    data = np.ascontiguousarray(arr, dtype=np.uint8)
    raw = bytearray()
    for row in range(height):
        raw.append(0)
        raw.extend(data[row].tobytes())

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)  # 8-bit grayscale
    with open(path, "wb") as handle:
        handle.write(b"\x89PNG\r\n\x1a\n")
        handle.write(chunk(b"IHDR", ihdr))
        handle.write(chunk(b"IDAT", zlib.compress(bytes(raw))))
        handle.write(chunk(b"IEND", b""))


def write_control_map(heightmap_dir: str, hub: str, height_shape: tuple[int, int],
                      out_dir: str) -> Optional[str]:
    """Pack the W3 terrain control map into one RGBA8 PNG for the UE shader.

    The 16-bit control texel decodes to:
      R = overlay  = control & 0x1F          (1-based; flat-ground detail tex)
      G = bkgrnd   = (control>>5) & 0x1F      (1-based; triplanar/slope tex)
      B = slopeThr = (control>>10) & 0x07     (slope blend threshold idx)
      A = uvScale  = (control>>13) & 0x07     (background UV-scale idx)
    Channels carry the raw small integers (recovered in the shader by *255);
    the texture imports uncompressed + point-sampled. Same pixel orientation as
    the heightmap (shared assembly). Returns the PNG path, or None.
    """
    from ..importers.terrain_w2ter import write_png

    height, width = height_shape
    overlay_src = os.path.join(heightmap_dir, f"combined.{hub}.overlay.data")
    bkgrnd_src = os.path.join(heightmap_dir, f"combined.{hub}.bkgrnd.data")
    blend_src = os.path.join(heightmap_dir, f"combined.{hub}.blendcontrol.data")
    if not (os.path.isfile(overlay_src) and os.path.isfile(bkgrnd_src) and os.path.isfile(blend_src)):
        return None

    overlay = np.fromfile(overlay_src, dtype=np.uint8)
    bkgrnd = np.fromfile(bkgrnd_src, dtype=np.uint8)
    blend = np.fromfile(blend_src, dtype=np.uint8)  # = (control>>10) & 0x3F
    if overlay.size != width * height or bkgrnd.size != width * height or blend.size != width * height:
        return None

    rgba = np.empty((height, width, 4), dtype=np.uint8)
    rgba[:, :, 0] = overlay.reshape((height, width))
    rgba[:, :, 1] = bkgrnd.reshape((height, width))
    rgba[:, :, 2] = (blend & 0x07).reshape((height, width))          # slope threshold
    rgba[:, :, 3] = ((blend >> 3) & 0x07).reshape((height, width))   # bkgrnd UV scale

    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{hub}.control.png")
    write_png(out_path, width, height, 6, 8, rgba.tobytes())  # color_type 6 = RGBA8
    return out_path
