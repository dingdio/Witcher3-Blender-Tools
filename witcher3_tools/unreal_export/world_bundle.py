"""Build an Unreal export bundle for a Witcher world's terrain.

Operates on a full-map terrain object produced by the world (.w2w) import
(``terrain_mode == "full_map"``). That object already carries the resolved
terrain params and the combined 16-bit heightmap that Blender displaced the
terrain from -- the same data layer geometry (buildings) was placed against --
so the Unreal landscape lines up with later layer imports by construction.

Emits a manifest with a ``terrain`` section (heightmap R16 + landscape layout +
actor transform + water) plus the terrain tint, layer textures, and packed
control map needed by the Unreal blend material.
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
from .bundle import _resolve_content_root_setting, default_export_folder, overwrite_policy_from_settings


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


def _ensure_terrain_control_sources(world, world_path: str, hub: str, heightmap_dir: str,
                                    warnings: list[str]) -> None:
    """Make sure combined overlay/bkgrnd/blend sidecars exist for UE terrain."""
    required = (
        os.path.join(heightmap_dir, f"combined.{hub}.overlay.data"),
        os.path.join(heightmap_dir, f"combined.{hub}.bkgrnd.data"),
        os.path.join(heightmap_dir, f"combined.{hub}.blendcontrol.data"),
    )
    if all(os.path.isfile(path) for path in required):
        return
    if not world_path:
        warnings.append("Terrain control map export skipped: source .w2w path is missing.")
        return

    try:
        from ..importers import import_w2w
        from ..importers import terrain_w2ter

        ctx = import_w2w._resolve_terrain_context(world, world_path)
        buffer_paths = import_w2w._collect_tile_buffer_paths_for_combine(
            ctx["terrain_tiles_dir"],
            ctx["terrain_tiles_rel"],
            ctx["n_tiles"],
            ctx["tile_res"],
            working_tiles_dir=ctx["working_tiles_dir"],
        )
        if not buffer_paths:
            warnings.append("Terrain control map export skipped: no .w2ter buffer sidecars were found.")
            return

        terrain_w2ter.combine_w2ter_tiles(
            buffer_paths,
            heightmap_dir,
            hub,
            res_override=ctx["tile_res"],
            x_tiles_override=ctx["n_tiles"],
            y_tiles_override=ctx["n_tiles"],
            targets=("overlay", "bkgrnd", "blend"),
            skip_existing=True,
        )
    except Exception as exc:
        warnings.append(f"Terrain control map export failed ({exc}); using flat tint.")


def _export_terrain_blend_layers(world, hub: str, heightmap_dir: str, height_res: int,
                                 bundle_root: str, asset_rel: str,
                                 warnings: list[str]) -> tuple[list[dict], str, list[dict]]:
    """Extract the W3 terrain layer atlases + packed control map into the bundle.

    Returns (layers_manifest, control_depot, texture_entries). The caller
    rejects tint-only terrain when a source world is available.
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
            converted_path = convert_texture_for_unreal(dds_path, textures_dir, stem)
        except Exception as exc:
            warnings.append(f"terrain texture '{depot_rel}' conversion failed: {exc}")
            return ""
        texture_entries.append({
            "depot_path": depot_rel,
            "file": relpath_for_manifest(converted_path, bundle_root),
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
    else:
        warnings.append("Terrain control map missing; Unreal will use the flat terrain tint material.")

    return layers_manifest, control_depot, texture_entries


def _export_terrain_holes(heightmap_dir: str, hub: str, source_res: int,
                          target_res: int, bundle_root: str,
                          warnings: list[str]) -> Optional[dict[str, Any]]:
    """Extract W3 terrain holes and emit an Unreal landscape visibility map.

    W3 treats a terrain texel as a hole when all combined control channels are 0.
    """
    import numpy as np

    overlay_src = os.path.join(heightmap_dir, f"combined.{hub}.overlay.data")
    bkgrnd_src = os.path.join(heightmap_dir, f"combined.{hub}.bkgrnd.data")
    blend_src = os.path.join(heightmap_dir, f"combined.{hub}.blendcontrol.data")
    if not (os.path.isfile(overlay_src) and os.path.isfile(bkgrnd_src)
            and os.path.isfile(blend_src)):
        return None

    overlay = np.fromfile(overlay_src, dtype=np.uint8)
    bkgrnd = np.fromfile(bkgrnd_src, dtype=np.uint8)
    blend = np.fromfile(blend_src, dtype=np.uint8)
    n = source_res * source_res
    if overlay.size != n or bkgrnd.size != n or blend.size != n:
        warnings.append(
            "Terrain hole export skipped: control map size does not match the "
            "heightmap (non-square terrain not supported)."
        )
        return None

    hole = (overlay == 0) & (bkgrnd == 0) & (blend == 0)
    hole_count = int(hole.sum())
    if hole_count == 0:
        return None

    hole = hole.reshape((source_res, source_res))
    hole = terrain_unreal.resample_mask_nearest(hole, target_res)

    os.makedirs(os.path.join(bundle_root, "Terrain"), exist_ok=True)
    r8_path = os.path.join(bundle_root, "Terrain", f"{hub}.visibility.r8")
    terrain_unreal.write_visibility_r8(r8_path, hole)

    return {
        "file": relpath_for_manifest(r8_path, bundle_root),
        "resolution": int(target_res),
        "layer_name": terrain_unreal.LANDSCAPE_VISIBILITY_LAYER,
        "hole_value": terrain_unreal.LANDSCAPE_HOLE_VALUE,
        "hole_count": hole_count,
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

    # Faithful weight-blended terrain layers: diffuse/normal atlases plus
    # overlay/bkgrnd/blend control maps. Terrain sends must not silently degrade
    # to tint-only when the source world is available.
    terrain_layers: list[dict] = []
    terrain_control: str = ""
    heightmap_dir = os.path.dirname(heightmap_png)
    terrain_material_errors: list[str] = []
    try:
        from ..CR2W import CR2W_reader
        from ..CR2W.common_blender import redkit_repo_context

        with redkit_repo_context(world_path):
            world = CR2W_reader.load_w2w(world_path) if world_path else None
        if world is not None:
            warning_start = len(warnings)
            with redkit_repo_context(world_path):
                _ensure_terrain_control_sources(world, world_path, hub, heightmap_dir, warnings)
                terrain_layers, terrain_control, layer_textures = _export_terrain_blend_layers(
                    world, hub, heightmap_dir, result.source_resolution,
                    bundle_root, asset_rel, warnings,
                )
            textures.extend(layer_textures)
            if not terrain_layers:
                detail = "; ".join(warnings[warning_start:]) or "no terrain layer textures were exported"
                terrain_material_errors.append(f"Terrain layer texture export failed: {detail}")
            elif not terrain_control:
                terrain_material_errors.append("Terrain control map was not exported.")
            else:
                missing_diffuse = sum(1 for layer in terrain_layers if not layer.get("diffuse"))
                normal_count = sum(1 for layer in terrain_layers if layer.get("normal"))
                if missing_diffuse:
                    terrain_material_errors.append(
                        f"Terrain layer texture export failed: {missing_diffuse} diffuse layer texture(s) missing."
                    )
                if 0 < normal_count < len(terrain_layers):
                    terrain_material_errors.append(
                        "Terrain layer texture export failed: "
                        f"{len(terrain_layers) - normal_count} normal layer texture(s) missing."
                    )
        elif world_path:
            terrain_material_errors.append(f"Could not load source world for terrain materials: {world_path}")
    except Exception as exc:
        terrain_material_errors.append(f"Terrain blend-layer export failed: {exc}")

    if terrain_material_errors:
        raise ValueError(
            "Terrain material export failed; refusing to send tint-only Unreal terrain. "
            + " ".join(terrain_material_errors)
        )

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

    try:
        visibility = _export_terrain_holes(
            heightmap_dir, hub, result.source_resolution, layout.resolution,
            bundle_root, warnings,
        )
        if visibility is not None:
            terrain_section["visibility"] = visibility
    except Exception as exc:
        warnings.append(f"Terrain hole export failed ({exc}); landscape will be solid.")

    manifest = build_manifest(
        asset_name=asset_name,
        bundle_root=bundle_root,
        source_game=source_game,
        content_root=content_root,
        overwrite=overwrite_policy_from_settings(settings),
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
