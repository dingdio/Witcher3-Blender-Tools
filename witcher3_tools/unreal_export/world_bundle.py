"""Build an Unreal export bundle for a Witcher world's terrain.

Operates on a full-map terrain object produced by the world (.w2w) import
(``terrain_mode == "full_map"``). That object already carries the resolved
terrain params and the combined 16-bit heightmap that Blender displaced the
terrain from -- the same data layer geometry (buildings) was placed against --
so the Unreal landscape lines up with later layer imports by construction.

Emits a manifest with a ``terrain`` section (heightmap R16 + landscape layout +
actor transform + water) plus an optional tint texture for the base colour.
Phase 4 will add the weight-blended terrain material; this is geometry + water +
flat colour.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

from .manifest import (
    build_manifest,
    depot_asset_rel,
    relpath_for_manifest,
    safe_asset_name,
)
from . import terrain_unreal
from . import terrain_material
from .bundle import _resolve_content_root_setting, default_export_folder


def _find_terrain_object(selected_objects, active_object=None):
    """The full-map terrain object to export: active first, then selection."""
    def is_terrain(obj):
        try:
            return obj is not None and obj.get("terrain_mode") == "full_map"
        except Exception:
            return False

    if is_terrain(active_object):
        return active_object
    for obj in selected_objects or []:
        if is_terrain(obj):
            return obj
    return None


def _object_source_game(obj) -> str:
    for key in ("witcher_source_game", "source_game"):
        try:
            value = str(obj.get(key, "") or "").strip()
        except Exception:
            value = ""
        if value:
            return value
    return "w3"


def _tint_source_path(heightmap_png: str, hub: str) -> str:
    """Prefer the flipud'd tint PNG over the combined DDS.

    The combine step applies np.flipud to the heightmap + control .data buffers
    AND to ``{hub}.tint.png``, but writes ``combined.{hub}.dds`` un-flipped. Using
    the DDS leaves the tint vertically mirrored relative to the terrain; the PNG
    shares the heightmap/control orientation.
    """
    folder = os.path.dirname(str(heightmap_png))
    png = os.path.join(folder, f"{hub}.tint.png")
    if os.path.isfile(png):
        return png
    return os.path.join(folder, f"combined.{hub}.dds")


def _export_tint_texture(tint_dds: str, bundle_root: str, depot_rel: str,
                         warnings: list[str]) -> Optional[dict[str, Any]]:
    """Convert the BC1 tint DDS to a PNG in the bundle and return a manifest
    texture entry, or None when the tint is unavailable."""
    if not tint_dds or not os.path.isfile(tint_dds):
        return None
    from .texture_export import convert_texture_for_unreal

    textures_dir = os.path.join(bundle_root, "Textures")
    stem = depot_rel.rsplit("/", 1)[-1]
    try:
        png_path = convert_texture_for_unreal(tint_dds, textures_dir, stem)
    except Exception as exc:
        warnings.append(f"terrain tint texture conversion failed: {exc}")
        return None
    return {
        "depot_path": depot_rel,
        "file": relpath_for_manifest(png_path, bundle_root),
        # import it linear.
        "srgb": False,
        "compression": "default",
    }


def _export_terrain_blend_layers(world, hub: str, heightmap_dir: str, height_res: int,
                                 bundle_root: str, asset_rel: str,
                                 warnings: list[str]) -> tuple[list[dict], str, list[dict]]:
    """Extract the W3 terrain layer atlases + packed control map into the bundle.

    Returns (layers_manifest, control_depot, texture_entries). Best-effort:
    on any failure the caller falls back to the flat tint material.
    """
    from .texture_export import convert_texture_for_unreal

    textures_dir = os.path.join(bundle_root, "Textures")
    layers_manifest: list[dict] = []
    control_depot: str = ""
    texture_entries: list[dict] = []

    mat_set = terrain_material.extract_terrain_material_set(world)
    warnings.extend(mat_set.warnings)
    if not mat_set.layers:
        return [], "", []

    def _add_texture(dds_path: str, depot_rel: str, srgb: bool, compression: str) -> str:
        if not dds_path or not os.path.isfile(dds_path):
            return ""
        stem = depot_rel.rsplit("/", 1)[-1]
        try:
            png_path = convert_texture_for_unreal(dds_path, textures_dir, stem)
        except Exception as exc:
            warnings.append(f"terrain texture '{depot_rel}' conversion failed: {exc}")
            return ""
        texture_entries.append({
            "depot_path": depot_rel,
            "file": relpath_for_manifest(png_path, bundle_root),
            "srgb": srgb,
            "compression": compression,
        })
        return depot_rel

    for layer in mat_set.layers:
        diffuse_depot = _add_texture(
            layer.diffuse_dds, f"{asset_rel}_layers/diffuse_{layer.index}", True, "default")
        normal_depot = _add_texture(
            layer.normal_dds, f"{asset_rel}_layers/normal_{layer.index}", False, "normalmap")
        layers_manifest.append({
            "index": layer.index,
            "diffuse": diffuse_depot,
            "normal": normal_depot,
            "blend_sharpness": layer.blend_sharpness,
            "slope_base_dampening": layer.slope_base_dampening,
            "slope_normal_dampening": layer.slope_normal_dampening,
            "falloff": layer.falloff,
            "specularity": layer.specularity,
            "specularity_base": layer.specularity_base,
            "specularity_scale": layer.specularity_scale,
        })

    # Packed control map (RGBA8: overlay/bkgrnd/slope/uvScale) as one
    # uncompressed, point-sampled texture.
    control_dir = os.path.join(bundle_root, "Terrain")
    control_path = terrain_material.write_control_map(
        heightmap_dir, hub, (height_res, height_res), control_dir)
    if control_path:
        control_depot = f"{asset_rel}_control"
        texture_entries.append({
            "depot_path": control_depot,
            "file": relpath_for_manifest(control_path, bundle_root),
            "srgb": False,
            "compression": "controlmap",
        })

    return layers_manifest, control_depot, texture_entries


def build_unreal_world_bundle(context, settings) -> dict[str, Any]:
    selected_objects = list(getattr(context, "selected_objects", []) or [])
    active_object = getattr(getattr(context, "view_layer", None), "objects", None)
    active_object = getattr(active_object, "active", None) if active_object else None
    terrain_obj = _find_terrain_object(selected_objects, active_object)
    if terrain_obj is None:
        raise ValueError(
            "Select an imported full-map terrain object first "
            "(import the world's .w2w terrain in Full Map mode, then select it)."
        )

    def prop(name, default=None):
        try:
            value = terrain_obj.get(name)
        except Exception:
            value = None
        return default if value is None else value

    heightmap_png = str(prop("terrain_heightmap_path", "") or "")
    if not heightmap_png or not os.path.isfile(heightmap_png):
        raise ValueError(
            f"Terrain heightmap not found: '{heightmap_png}'. Re-import the world terrain."
        )

    hub = str(prop("terrain_hub", "") or "terrain")
    world_path = str(prop("world_path", "") or "")
    terrain_size = float(prop("terrainSize", 0.0) or 0.0)
    lowest = float(prop("lowestElevation", 0.0) or 0.0)
    highest = float(prop("highestElevation", 0.0) or 0.0)
    if terrain_size <= 0.0:
        raise ValueError("Terrain object has no positive terrainSize; cannot place landscape.")

    source_game = _object_source_game(terrain_obj)
    asset_name = safe_asset_name(str(getattr(settings, "asset_name", "") or "") or hub)
    content_root = _resolve_content_root_setting(getattr(settings, "content_root", ""), source_game)

    export_root = str(getattr(settings, "export_folder", "") or default_export_folder())
    bundle_root = os.path.join(export_root, asset_name)
    os.makedirs(os.path.join(bundle_root, "Terrain"), exist_ok=True)

    warnings: list[str] = []

    # Heightmap -> UE-valid R16 + landscape layout + transform.
    r16_path = os.path.join(bundle_root, "Terrain", f"{hub}.r16")
    result = terrain_unreal.build_terrain_r16(
        heightmap_png_path=heightmap_png,
        out_r16_path=r16_path,
        terrain_size_m=terrain_size,
        lowest_elevation_m=lowest,
        highest_elevation_m=highest,
    )
    layout = result.layout
    transform = result.transform

    # Landscape asset mirrors the DEPOT path of the world file. world_path is an
    # absolute filesystem path, so convert it to depot-relative first (otherwise
    # the whole C:\Users\... path becomes the Unreal asset path).
    depot_world = ""
    if world_path:
        if os.path.isabs(world_path) or os.path.splitdrive(world_path)[0]:
            try:
                from ..importers.import_mesh import get_repo_from_abs_path

                depot_world = get_repo_from_abs_path(os.path.normpath(world_path)) or ""
            except Exception:
                depot_world = ""
        else:
            depot_world = world_path
    asset_rel = depot_asset_rel(depot_world) if depot_world else ""
    if not asset_rel:
        asset_rel = f"levels/{safe_asset_name(hub)}/{safe_asset_name(hub)}"

    textures: list[dict[str, Any]] = []
    base_color_depot = ""
    tint_entry = _export_tint_texture(
        _tint_source_path(heightmap_png, hub), bundle_root, f"{asset_rel}_tint", warnings
    )
    if tint_entry is not None:
        textures.append(tint_entry)
        base_color_depot = tint_entry["depot_path"]
    else:
        warnings.append("No terrain tint texture; landscape will use a neutral base colour.")

    # Faithful weight-blended terrain layers (Phase 4): diffuse/normal atlases +
    # overlay/bkgrnd/blend control maps. Best-effort; falls back to tint.
    terrain_layers: list[dict] = []
    terrain_control: str = ""
    heightmap_dir = os.path.dirname(heightmap_png)
    try:
        from ..CR2W import CR2W_reader

        world = CR2W_reader.load_w2w(world_path) if world_path else None
        if world is not None:
            terrain_layers, terrain_control, layer_textures = _export_terrain_blend_layers(
                world, hub, heightmap_dir, result.source_resolution,
                bundle_root, asset_rel, warnings,
            )
            textures.extend(layer_textures)
    except Exception as exc:
        warnings.append(f"Terrain blend-layer export failed ({exc}); using flat tint.")

    terrain_section: dict[str, Any] = {
        "name": safe_asset_name(hub),
        "asset_path": asset_rel,
        "heightmap_r16": relpath_for_manifest(r16_path, bundle_root),
        "resolution": layout.resolution,
        "source_resolution": result.source_resolution,
        "subsection_size_quads": layout.subsection_size_quads,
        "num_subsections": layout.num_subsections,
        "component_count_per_axis": layout.component_count_per_axis,
        "min_x": 0,
        "min_y": 0,
        "max_x": layout.resolution - 1,
        "max_y": layout.resolution - 1,
        "transform": transform.as_dict(),
        "terrain_size": terrain_size,
        "elevation": {"lowest": lowest, "highest": highest},
        "water": {"z": terrain_unreal.water_plane_z_cm(0.0), "size_cm": terrain_size * terrain_unreal.UE_UNITS_PER_METER},
        "base_color_texture": base_color_depot,
        "world_path": world_path,
    }
    if terrain_layers:
        terrain_section["layers"] = terrain_layers
        terrain_section["layer_count"] = len(terrain_layers)
        terrain_section["control"] = terrain_control

    manifest = build_manifest(
        asset_name=asset_name,
        bundle_root=bundle_root,
        source_game=source_game,
        content_root=content_root,
        textures=textures,
        terrain=terrain_section,
        warnings=warnings,
    )

    manifest_path = os.path.join(bundle_root, "witcher_unreal_export.json")
    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)

    return {
        "asset_name": asset_name,
        "bundle_root": bundle_root,
        "manifest_path": manifest_path,
        "manifest": manifest,
    }
