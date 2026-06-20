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
import hashlib
import math
import os
import tempfile
from typing import Any, Optional

from .manifest import (
    build_manifest,
    depot_asset_rel,
    normalize_depot_path,
    relpath_for_manifest,
    safe_asset_name,
)
from . import terrain_unreal
from . import terrain_material
from . import foliage_bundle
from . import speedtree_bundle
from .bundle import _resolve_content_root_setting, default_export_folder, overwrite_policy_from_settings

_WORLD_FOLIAGE_CELL_CACHE_SCHEMA = "witcher_unreal_world_foliage_cell.v2"


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


def _cell_sort_key(cell_key: str):
    parts = str(cell_key or "").replace(",", ".").split("_")
    try:
        return tuple(float(part) for part in parts[:2])
    except Exception:
        return (str(cell_key or ""),)


def _cell_key_from_flyr_path(flyr_path: str) -> str:
    base = os.path.splitext(os.path.basename(str(flyr_path or "")))[0]
    return base[len("foliage_"):] if base.startswith("foliage_") else base


def _scan_disk_flyr_dir(disk_dir: str) -> dict[str, str]:
    cells: dict[str, str] = {}
    if not disk_dir or not os.path.isdir(disk_dir):
        return cells
    with os.scandir(disk_dir) as entries:
        for entry in entries:
            if not entry.is_file() or not entry.name.lower().endswith(".flyr"):
                continue
            cells[_cell_key_from_flyr_path(entry.name)] = entry.path
    return cells


def _discover_world_flyr_cells(context, world_path: str, warnings: list[str]) -> list[tuple[str, str]]:
    """Find every .flyr cell for a world.

    REDkit worlds usually have a real source_foliage folder beside the .w2w.
    Use that directly first; scanning the global bundle index is much slower and
    unnecessary when the source folder exists.
    """
    cells: dict[str, str] = {}
    if world_path and (os.path.isabs(world_path) or os.path.splitdrive(world_path)[0]):
        try:
            cells.update(_scan_disk_flyr_dir(os.path.join(os.path.dirname(world_path), "source_foliage")))
        except Exception as exc:
            warnings.append(f"World foliage folder scan failed: {exc}")
        if cells:
            return sorted(cells.items(), key=lambda item: _cell_sort_key(item[0]))

    try:
        from ..importers import import_foliage
    except Exception as exc:
        warnings.append(f"World foliage export skipped: foliage importer unavailable ({exc}).")
        return []

    foliage_prefix = ""
    try:
        foliage_prefix = import_foliage.get_game_rel_foliage_prefix(world_path, context)
        for key, flyr_path in (import_foliage.find_all_flyr_keys_in_bundles(foliage_prefix) or {}).items():
            cells[str(key)] = str(flyr_path)
    except Exception as exc:
        warnings.append(f"World foliage bundle scan failed ({exc}); checking source_foliage on disk.")

    disk_dirs: list[str] = []
    if world_path and (os.path.isabs(world_path) or os.path.splitdrive(world_path)[0]):
        disk_dirs.append(os.path.join(os.path.dirname(world_path), "source_foliage"))
    if foliage_prefix:
        try:
            from ..CR2W.common_blender import repo_file

            repo_dir = repo_file(foliage_prefix)
            if repo_dir:
                disk_dirs.append(repo_dir)
        except Exception:
            pass

    seen_dirs: set[str] = set()
    for disk_dir in disk_dirs:
        if not disk_dir:
            continue
        norm_dir = os.path.normcase(os.path.normpath(str(disk_dir)))
        if norm_dir in seen_dirs or not os.path.isdir(disk_dir):
            continue
        seen_dirs.add(norm_dir)
        try:
            filenames = os.listdir(disk_dir)
        except Exception as exc:
            warnings.append(f"World foliage folder scan failed for {disk_dir}: {exc}")
            continue
        for fname in filenames:
            if fname.lower().endswith(".flyr"):
                cells.setdefault(str(_cell_key_from_flyr_path(fname)), os.path.join(disk_dir, fname))

    return sorted(cells.items(), key=lambda item: _cell_sort_key(item[0]))


def _resolve_world_flyr_path(flyr_path: str) -> str:
    raw = str(flyr_path or "").strip().strip('"')
    if raw and (os.path.isabs(raw) or os.path.splitdrive(raw)[0]) and os.path.isfile(raw):
        return raw
    try:
        from ..importers import import_foliage

        resolved = import_foliage.resolve_flyr_abs_path(raw)
        if resolved and os.path.isfile(resolved):
            return resolved
    except Exception:
        pass
    try:
        return foliage_bundle._resolve_flyr_abspath(raw)
    except Exception:
        return ""


def _world_foliage_cache_root(world_path: str) -> str:
    key = hashlib.sha1(os.path.normcase(os.path.normpath(str(world_path or ""))).encode("utf-8", "ignore")).hexdigest()
    try:
        from ..extension_paths import get_cache_root

        root = get_cache_root(create=True)
    except Exception:
        root = os.path.join(tempfile.gettempdir(), "witcher3_tools", "witcher_cache")
    path = os.path.join(root, "unreal_world_foliage", key[:2], key)
    os.makedirs(path, exist_ok=True)
    return path


def _world_foliage_cell_cache_path(cache_root: str, abs_flyr: str) -> str:
    key = hashlib.sha1(os.path.normcase(os.path.normpath(str(abs_flyr or ""))).encode("utf-8", "ignore")).hexdigest()
    return os.path.join(cache_root, key[:2], f"{key}.json")


def _load_cached_world_foliage_cell(cache_root: str, abs_flyr: str) -> Optional[dict[str, Any]]:
    try:
        stat = os.stat(abs_flyr)
        cache_path = _world_foliage_cell_cache_path(cache_root, abs_flyr)
        with open(cache_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if data.get("schema") != _WORLD_FOLIAGE_CELL_CACHE_SCHEMA:
            return None
        if int(data.get("mtime_ns", -1)) != int(getattr(stat, "st_mtime_ns", 0)):
            return None
        if int(data.get("size", -1)) != int(stat.st_size):
            return None
        cell = data.get("cell")
        return cell if isinstance(cell, dict) else None
    except Exception:
        return None


def _write_cached_world_foliage_cell(cache_root: str, abs_flyr: str, cell: dict[str, Any]) -> None:
    try:
        stat = os.stat(abs_flyr)
        cache_path = _world_foliage_cell_cache_path(cache_root, abs_flyr)
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        payload = {
            "schema": _WORLD_FOLIAGE_CELL_CACHE_SCHEMA,
            "source": os.path.normpath(abs_flyr),
            "mtime_ns": int(getattr(stat, "st_mtime_ns", 0)),
            "size": int(stat.st_size),
            "cell": cell,
        }
        with open(cache_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"))
    except Exception:
        pass


def _unreal_project_asset_file_exists(settings, content_root: str, asset_path: str) -> bool:
    project = str(getattr(settings, "unreal_project", "") or "").strip().strip('"')
    if not project or not project.lower().endswith(".uproject"):
        return False
    project_dir = os.path.dirname(project)
    if not project_dir or not os.path.isdir(project_dir):
        return False

    root = str(content_root or "").replace("\\", "/").strip("/")
    if root == "Game":
        root_rel = ""
    elif root.startswith("Game/"):
        root_rel = root[len("Game/"):]
    else:
        return False
    parts = [part for part in (root_rel + "/" + str(asset_path or "")).split("/") if part]
    if not parts:
        return False
    return os.path.isfile(os.path.join(project_dir, "Content", *parts) + ".uasset")


def _existing_speedtree_entry(asset_path: str, depot_path: str) -> dict[str, Any]:
    return {
        "asset_path": asset_path,
        "depot_path": str(depot_path or "").replace("/", "\\"),
        "force_import": False,
        "existing_project_asset": True,
    }


def _matmul3(a, b):
    return [
        [
            a[row][0] * b[0][col] + a[row][1] * b[1][col] + a[row][2] * b[2][col]
            for col in range(3)
        ]
        for row in range(3)
    ]


def _quat_xyzw_from_rot3(rot) -> list[float]:
    trace = rot[0][0] + rot[1][1] + rot[2][2]
    if trace > 0.0:
        s = 0.5 / math.sqrt(trace + 1.0)
        w = 0.25 / s
        x = (rot[2][1] - rot[1][2]) * s
        y = (rot[0][2] - rot[2][0]) * s
        z = (rot[1][0] - rot[0][1]) * s
    elif rot[0][0] > rot[1][1] and rot[0][0] > rot[2][2]:
        s = 2.0 * math.sqrt(max(0.0, 1.0 + rot[0][0] - rot[1][1] - rot[2][2]))
        w = (rot[2][1] - rot[1][2]) / s if s else 1.0
        x = 0.25 * s
        y = (rot[0][1] + rot[1][0]) / s if s else 0.0
        z = (rot[0][2] + rot[2][0]) / s if s else 0.0
    elif rot[1][1] > rot[2][2]:
        s = 2.0 * math.sqrt(max(0.0, 1.0 + rot[1][1] - rot[0][0] - rot[2][2]))
        w = (rot[0][2] - rot[2][0]) / s if s else 1.0
        x = (rot[0][1] + rot[1][0]) / s if s else 0.0
        y = 0.25 * s
        z = (rot[1][2] + rot[2][1]) / s if s else 0.0
    else:
        s = 2.0 * math.sqrt(max(0.0, 1.0 + rot[2][2] - rot[0][0] - rot[1][1]))
        w = (rot[1][0] - rot[0][1]) / s if s else 1.0
        x = (rot[0][2] + rot[2][0]) / s if s else 0.0
        y = (rot[1][2] + rot[2][1]) / s if s else 0.0
        z = 0.25 * s
    norm = math.sqrt(x * x + y * y + z * z + w * w)
    if norm > 0.0:
        x, y, z, w = x / norm, y / norm, z / norm, w / norm
    if w < 0.0:
        x, y, z, w = -x, -y, -z, -w
    return [float(x), float(y), float(z), float(w)]


def _foliage_transform_to_unreal(transform: Any) -> Optional[dict[str, Any]]:
    if transform is None:
        return None
    try:
        location, w3_rot, scale = foliage_bundle._instance_transform_parts(transform)
    except Exception:
        return None
    if not all(math.isfinite(value) for value in (*location, *scale)):
        return None

    # Same conjugation as terrain_unreal.w3_matrix_to_unreal, but without
    # allocating a 4x4 numpy matrix per foliage instance.
    signs = [1.0, terrain_unreal.UNREAL_Y_SIGN, 1.0]
    ue_rot = [
        [w3_rot[row][col] * signs[row] * signs[col] for col in range(3)]
        for row in range(3)
    ]
    return {
        "location": [
            location[0] * terrain_unreal.UE_UNITS_PER_METER,
            location[1] * terrain_unreal.UE_UNITS_PER_METER * terrain_unreal.UNREAL_Y_SIGN,
            location[2] * terrain_unreal.UE_UNITS_PER_METER,
        ],
        "rotation": _quat_xyzw_from_rot3(ue_rot),
        "scale": [float(scale[0]), float(scale[1]), float(scale[2])],
    }


def _cell_data_from_foliage_items(abs_flyr: str, foliage_items: list[Any]) -> Optional[dict[str, Any]]:
    by_depot: dict[str, dict[str, Any]] = {}
    for item in foliage_items or []:
        depot = str((item or {}).get("repo_path", "") or "").strip()
        transform = (item or {}).get("transform")
        instance = _foliage_transform_to_unreal(transform)
        if not depot or instance is None:
            continue
        entry = by_depot.setdefault(normalize_depot_path(depot), {"depot": depot, "instances": []})
        entry["instances"].append(instance)
    if not by_depot:
        return None

    layer_id = foliage_bundle._layer_id_for_flyr(abs_flyr)
    label, folder = foliage_bundle._layer_label_and_folder(layer_id)
    return {
        "layer_id": layer_id,
        "label": label,
        "folder": folder,
        "types": [
            {"depot": entry["depot"], "instances": entry["instances"]}
            for entry in by_depot.values()
            if entry["instances"]
        ],
    }


def _cell_data_from_foliage_chunk(abs_flyr: str, foliage_chunk: Any) -> Optional[dict[str, Any]]:
    by_depot: dict[str, dict[str, Any]] = {}
    all_types = []
    for attr in ("Trees", "Grasses"):
        coll = getattr(foliage_chunk, attr, None)
        if coll is not None and hasattr(coll, "elements"):
            all_types.extend(list(coll.elements))

    for tree_type in all_types:
        try:
            depot = str(tree_type.TreeType.DepotPath or "").strip()
        except Exception:
            depot = ""
        if not depot:
            continue
        collection = getattr(tree_type, "TreeCollection", None)
        instances_raw = list(getattr(collection, "elements", []) or []) if collection is not None else []
        entry = by_depot.setdefault(normalize_depot_path(depot), {"depot": depot, "instances": []})
        for inst in instances_raw:
            instance = _foliage_transform_to_unreal(inst)
            if instance is not None:
                entry["instances"].append(instance)

    if not by_depot:
        return None

    layer_id = foliage_bundle._layer_id_for_flyr(abs_flyr)
    label, folder = foliage_bundle._layer_label_and_folder(layer_id)
    return {
        "layer_id": layer_id,
        "label": label,
        "folder": folder,
        "types": [
            {"depot": entry["depot"], "instances": entry["instances"]}
            for entry in by_depot.values()
            if entry["instances"]
        ],
    }


def _scan_world_foliage_cell_fast(abs_flyr: str) -> Optional[dict[str, Any]]:
    try:
        from ..CR2W import fast_cache_scan

        scan = fast_cache_scan.scan_dependency_file(abs_flyr)
    except Exception:
        return None
    if not isinstance(scan, dict):
        return None
    return _cell_data_from_foliage_items(abs_flyr, list(scan.get("foliage_items", []) or []))


def _scan_world_foliage_cell_full(abs_flyr: str, load_foliage, warnings: list[str]) -> Optional[dict[str, Any]]:
    try:
        level = load_foliage(abs_flyr)
    except Exception as exc:
        warnings.append(f"World foliage cell skipped: {os.path.basename(abs_flyr)} failed to load ({exc}).")
        return None
    foliage_chunk = getattr(level, "Foliage", None)
    if foliage_chunk is None:
        warnings.append(f"World foliage cell skipped: {os.path.basename(abs_flyr)} has no foliage chunk.")
        return None

    return _cell_data_from_foliage_chunk(abs_flyr, foliage_chunk)


def _build_world_foliage_sections(context, settings, world_path: str, bundle_root: str, content_root: str,
                                  warnings: list[str]) -> tuple[list[dict[str, Any]], Optional[dict[str, Any]], dict[str, int]]:
    """Stage all SpeedTrees referenced by the world's .flyr cells and emit one
    foliage manifest section for the terrain bundle."""
    from ..CR2W.CR2W_reader import load_foliage
    from ..CR2W.common_blender import redkit_repo_context, win_path_exists

    stats = {
        "cells_found": 0,
        "cells_exported": 0,
        "tree_types": 0,
        "instances": 0,
        "speedtrees": 0,
        "missing_srt": 0,
        "textures_requested": 0,
        "textures_staged": 0,
        "textures_missing": 0,
        "cache_hits": 0,
        "cache_misses": 0,
        "fast_scanned": 0,
        "full_scanned": 0,
        "speedtrees_reused": 0,
    }
    speedtrees: list[dict[str, Any]] = []
    foliage_cells: list[dict[str, Any]] = []
    seen_assets: set[str] = set()
    resolved_srt_cache: dict[str, tuple[str, str, bool]] = {}
    warned_missing_srt: set[str] = set()
    overwrite_meshes = bool(overwrite_policy_from_settings(settings).get("meshes", False))

    if not world_path:
        warnings.append("World foliage export skipped: source .w2w path is missing.")
        return speedtrees, None, stats

    cache_root = _world_foliage_cache_root(world_path)
    with redkit_repo_context(world_path):
        flyr_cells = _discover_world_flyr_cells(context, world_path, warnings)
        stats["cells_found"] = len(flyr_cells)
        if not flyr_cells:
            warnings.append("World foliage export skipped: no .flyr cells found in source_foliage.")
            return speedtrees, None, stats

        for _cell_key, flyr_ref in flyr_cells:
            abs_flyr = _resolve_world_flyr_path(flyr_ref)
            if not abs_flyr:
                warnings.append(f"World foliage cell skipped: could not resolve {flyr_ref}")
                continue

            cell_data = _load_cached_world_foliage_cell(cache_root, abs_flyr)
            if cell_data is not None:
                stats["cache_hits"] += 1
            else:
                stats["cache_misses"] += 1
                cell_data = _scan_world_foliage_cell_fast(abs_flyr)
                if cell_data is not None:
                    stats["fast_scanned"] += 1
                else:
                    cell_data = _scan_world_foliage_cell_full(abs_flyr, load_foliage, warnings)
                    if cell_data is not None:
                        stats["full_scanned"] += 1
                if cell_data is not None:
                    _write_cached_world_foliage_cell(cache_root, abs_flyr, cell_data)

            if not cell_data or not cell_data.get("types"):
                continue

            layer_id = str(cell_data.get("layer_id", "") or foliage_bundle._layer_id_for_flyr(abs_flyr))
            label = str(cell_data.get("label", "") or os.path.basename(abs_flyr))
            folder = str(cell_data.get("folder", "") or "")
            foliage_types: list[dict[str, Any]] = []
            bounds_min = [float("inf"), float("inf")]
            bounds_max = [float("-inf"), float("-inf")]

            for entry in list(cell_data.get("types", []) or []):
                depot = str(entry.get("depot", "") or "").strip()
                instances = list(entry.get("instances", []) or [])
                if not depot or not instances:
                    continue

                depot_key = normalize_depot_path(depot)
                resolved = resolved_srt_cache.get(depot_key)
                if resolved is None:
                    abs_srt, resolved_depot = speedtree_bundle._resolve_srt_source(context, depot, depot)
                    exists = bool(abs_srt and win_path_exists(abs_srt))
                    resolved = (abs_srt or "", resolved_depot or depot, exists)
                    resolved_srt_cache[depot_key] = resolved
                abs_srt, resolved_depot, srt_exists = resolved
                if not srt_exists:
                    stats["missing_srt"] += 1
                    if depot_key not in warned_missing_srt:
                        warned_missing_srt.add(depot_key)
                        warnings.append(
                            f"{depot}: SpeedTree .srt not found on disk; foliage instances skipped."
                        )
                    continue

                asset_path = depot_asset_rel(resolved_depot or depot)
                if not asset_path:
                    warnings.append(f"{depot}: could not derive an Unreal asset path; skipped.")
                    continue

                if asset_path not in seen_assets:
                    seen_assets.add(asset_path)
                    if not overwrite_meshes and _unreal_project_asset_file_exists(settings, content_root, asset_path):
                        speedtree_entry, texture_stats = speedtree_bundle.build_speedtree_entry(
                            context, settings, abs_srt, resolved_depot or depot, bundle_root, warnings,
                            force_import=False,
                        )
                        speedtree_entry["existing_project_asset"] = True
                        speedtrees.append(speedtree_entry)
                        stats["speedtrees_reused"] += 1
                        stats["textures_requested"] += int(texture_stats.get("requested", 0) or 0)
                        stats["textures_staged"] += int(texture_stats.get("staged", 0) or 0)
                        stats["textures_missing"] += len(texture_stats.get("missing", []) or [])
                    else:
                        speedtree_entry, texture_stats = speedtree_bundle.build_speedtree_entry(
                            context, settings, abs_srt, resolved_depot or depot, bundle_root, warnings,
                            force_import=False,
                        )
                        speedtrees.append(speedtree_entry)
                        stats["textures_requested"] += int(texture_stats.get("requested", 0) or 0)
                        stats["textures_staged"] += int(texture_stats.get("staged", 0) or 0)
                        stats["textures_missing"] += len(texture_stats.get("missing", []) or [])

                for inst in instances:
                    loc = inst["location"]
                    bounds_min[0], bounds_min[1] = min(bounds_min[0], loc[0]), min(bounds_min[1], loc[1])
                    bounds_max[0], bounds_max[1] = max(bounds_max[0], loc[0]), max(bounds_max[1], loc[1])
                foliage_types.append({
                    "name": asset_path.rsplit("/", 1)[-1],
                    "asset_path": asset_path,
                    "instances": instances,
                })

            if not foliage_types:
                continue
            cell = {"layer_id": layer_id, "label": label, "folder": folder, "types": foliage_types}
            if bounds_min[0] != float("inf"):
                cell["bounds"] = {"min": bounds_min, "max": bounds_max}
            foliage_cells.append(cell)
            stats["cells_exported"] += 1
            stats["tree_types"] += len(foliage_types)
            stats["instances"] += sum(len(item["instances"]) for item in foliage_types)

    stats["speedtrees"] = len(speedtrees)
    if stats["cells_found"] and not foliage_cells:
        warnings.append(
            f"World foliage export skipped: no SpeedTree .srt sources could be resolved for "
            f"{stats['cells_found']} .flyr cell(s)."
        )
    return speedtrees, ({"cells": foliage_cells} if foliage_cells else None), stats


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

    speedtrees: list[dict[str, Any]] = []
    foliage_section: Optional[dict[str, Any]] = None
    foliage_stats: dict[str, int] = {}
    if bool(getattr(settings, "include_world_foliage", True)):
        try:
            speedtrees, foliage_section, foliage_stats = _build_world_foliage_sections(
                context, settings, world_path, bundle_root, content_root, warnings
            )
        except Exception as exc:
            warnings.append(f"World foliage export failed ({exc}); terrain will still be sent.")
    else:
        foliage_stats = {
            "cells_found": 0,
            "cells_exported": 0,
            "tree_types": 0,
            "instances": 0,
            "speedtrees": 0,
            "missing_srt": 0,
            "textures_requested": 0,
            "textures_staged": 0,
            "textures_missing": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "fast_scanned": 0,
            "full_scanned": 0,
            "speedtrees_reused": 0,
        }

    manifest = build_manifest(
        asset_name=asset_name,
        bundle_root=bundle_root,
        source_game=source_game,
        content_root=content_root,
        overwrite=overwrite_policy_from_settings(settings),
        textures=textures,
        speedtrees=speedtrees,
        terrain=terrain_section,
        foliage=foliage_section,
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
        "foliage_stats": foliage_stats,
    }
