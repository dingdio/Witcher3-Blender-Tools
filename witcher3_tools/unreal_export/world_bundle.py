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


def _tint_dds_path(heightmap_png: str, hub: str) -> str:
    folder = os.path.dirname(str(heightmap_png))
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
        "srgb": True,
        "compression": "default",
    }


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

    # Landscape asset mirrors the depot path of the world file.
    asset_rel = depot_asset_rel(world_path) if world_path else f"levels/{safe_asset_name(hub)}/{safe_asset_name(hub)}"
    if not asset_rel:
        asset_rel = f"levels/{safe_asset_name(hub)}/{safe_asset_name(hub)}"

    textures: list[dict[str, Any]] = []
    base_color_depot = ""
    tint_entry = _export_tint_texture(
        _tint_dds_path(heightmap_png, hub), bundle_root, f"{asset_rel}_tint", warnings
    )
    if tint_entry is not None:
        textures.append(tint_entry)
        base_color_depot = tint_entry["depot_path"]
    else:
        warnings.append("No terrain tint texture; landscape will use a neutral base colour.")

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
