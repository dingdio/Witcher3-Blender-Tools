"""Build Unreal foliage bundles from Witcher .flyr layers.

Stages referenced SpeedTree .srt files and emits 'foliage.cells' with Unreal
transforms. Nothing is imported into Blender.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import numpy as np

from . import speedtree_bundle
from . import terrain_unreal
from .bundle import (
    _resolve_content_root_setting,
    default_export_folder,
    overwrite_policy_from_settings,
)
from .manifest import build_manifest, normalize_depot_path, safe_asset_name
from ..foliage_core import decode_foliage_instance_transform

log = logging.getLogger(__name__)


def _resolve_flyr_abspath(flyr_path: str) -> str:
    raw = str(flyr_path or "").strip().strip('"')
    if not raw:
        raise ValueError("No .flyr path given.")
    if os.path.isfile(raw):
        return raw
    from ..CR2W.common_blender import repo_file

    resolved = repo_file(normalize_depot_path(raw))
    if not resolved or not os.path.isfile(resolved):
        raise ValueError(f"Could not resolve .flyr on disk: {flyr_path}")
    return resolved


def _layer_id_for_flyr(flyr_path: str) -> str:
    from ..importers.import_mesh import get_repo_from_abs_path

    raw = str(flyr_path or "").strip().strip('"')
    if os.path.isabs(raw) or os.path.splitdrive(raw)[0]:
        try:
            rel = get_repo_from_abs_path(os.path.normpath(raw)) or ""
        except Exception:
            rel = ""
        return normalize_depot_path(rel or os.path.basename(raw))
    return normalize_depot_path(raw)


def _layer_label_and_folder(layer_id: str) -> tuple[str, str]:
    norm = normalize_depot_path(layer_id)
    if not norm:
        return "foliage", ""
    segments = [seg for seg in norm.split("\\") if seg]
    label = segments[-1] if segments else "foliage"
    folder = "/".join(segments[:-1])
    return label, folder


def _instance_transform_parts(inst) -> tuple[list[float], np.ndarray, list[float]]:
    from mathutils import Euler

    decoded = decode_foliage_instance_transform(inst)
    rot = np.asarray(Euler(decoded.rotation_xyz, "XYZ").to_matrix(), dtype=float)
    # Use only the rotation basis from 4×4 adapters.
    rot = rot[:3, :3]
    return list(decoded.location), rot, list(decoded.scale)


def _instance_world_matrix(inst) -> np.ndarray:
    """Blender-frame world 4x4 for one foliage instance, matching the .flyr
    importer. Current RED foliage instances are position + uniform scale +
    yaw-only quaternion Z/W; older/generic transforms may still be Euler."""
    location, rot, scale = _instance_transform_parts(inst)
    mat = np.identity(4, dtype=float)
    mat[:3, :3] = rot @ np.diag(scale)
    mat[:3, 3] = location
    return mat


def collect_foliage_placements(foliage_chunk, warnings: list[str]) -> dict[str, dict[str, Any]]:
    """Group every tree+grass instance by its SpeedTree depot path.

    Returns ``{normalized_srt_depot: {"depot": original, "matrices": [4x4...]}}``.
    """
    by_depot: dict[str, dict[str, Any]] = {}

    all_types = []
    for attr in ("Trees", "Grasses"):
        coll = getattr(foliage_chunk, attr, None)
        if coll is not None and hasattr(coll, "elements"):
            all_types += list(coll.elements)

    for tree_type in all_types:
        try:
            depot = str(tree_type.TreeType.DepotPath or "").strip()
        except Exception:
            depot = ""
        if not depot:
            continue
        instances = []
        collection = getattr(tree_type, "TreeCollection", None)
        if collection is not None and hasattr(collection, "elements"):
            instances = collection.elements

        entry = by_depot.setdefault(normalize_depot_path(depot), {"depot": depot, "matrices": []})
        for inst in instances:
            matrix = _instance_world_matrix(inst)
            if terrain_unreal.world_matrix_has_valid_basis(matrix):
                entry["matrices"].append(matrix)
    return by_depot


def build_unreal_flyr_bundle(context, settings, flyr_path: str) -> dict[str, Any]:
    from ..CR2W.CR2W_reader import load_foliage
    from ..CR2W.common_blender import redkit_repo_context, win_path_exists

    abs_flyr = _resolve_flyr_abspath(flyr_path)
    started = time.perf_counter()
    warnings: list[str] = []

    # Resolve the placed .srt trees against the REDkit uncooked depot first
    # (same dual-depot lookup the normal foliage import uses).
    with redkit_repo_context(abs_flyr):
        level = load_foliage(abs_flyr)
        foliage_chunk = getattr(level, "Foliage", None)
        if foliage_chunk is None:
            raise ValueError(f"No CFoliageResource chunk found in {os.path.basename(abs_flyr)}.")

        by_depot = {k: v for k, v in collect_foliage_placements(foliage_chunk, warnings).items() if v["matrices"]}
        if not by_depot:
            raise ValueError(f"No foliage instances found in {os.path.basename(abs_flyr)}.")

        source_game = "w3"
        layer_id = _layer_id_for_flyr(abs_flyr)
        label, folder = _layer_label_and_folder(layer_id)
        asset_name = safe_asset_name(getattr(settings, "asset_name", "") or label or "WitcherFoliage")
        content_root = _resolve_content_root_setting(getattr(settings, "content_root", ""), source_game)
        export_root = str(getattr(settings, "export_folder", "") or default_export_folder())
        bundle_root = os.path.join(export_root, asset_name)
        os.makedirs(speedtree_bundle._safe_path(bundle_root), exist_ok=True)
        overwrite = overwrite_policy_from_settings(settings)

        speedtrees: list[dict[str, Any]] = []
        foliage_types: list[dict[str, Any]] = []
        seen_assets: set[str] = set()
        texture_totals = {"requested": 0, "staged": 0, "missing": 0}
        missing_srt = 0
        bounds_min = [float("inf"), float("inf")]
        bounds_max = [float("-inf"), float("-inf")]

        for entry in by_depot.values():
            depot = entry["depot"]
            abs_srt, resolved_depot = speedtree_bundle._resolve_srt_source(context, depot, depot)
            if not abs_srt or not win_path_exists(abs_srt):
                warnings.append(
                    f"{depot}: SpeedTree .srt not found on disk; {len(entry['matrices'])} instance(s) skipped."
                )
                missing_srt += 1
                continue

            speedtree_entry, texture_stats = speedtree_bundle.build_speedtree_entry(
                context, settings, abs_srt, resolved_depot or depot, bundle_root, warnings,
                force_import=False,
            )
            asset_path = speedtree_entry["asset_path"]
            if not asset_path:
                warnings.append(f"{depot}: could not derive an Unreal asset path; skipped.")
                continue

            if asset_path not in seen_assets:
                seen_assets.add(asset_path)
                speedtrees.append(speedtree_entry)
                texture_totals["requested"] += int(texture_stats.get("requested", 0) or 0)
                texture_totals["staged"] += int(texture_stats.get("staged", 0) or 0)
                texture_totals["missing"] += len(texture_stats.get("missing", []) or [])

            instances = [terrain_unreal.w3_matrix_to_unreal(m) for m in entry["matrices"]]
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
        raise ValueError(
            f"No SpeedTree .srt sources could be resolved for {os.path.basename(abs_flyr)}; nothing to send."
        )

    # Bounds let re-sends replace only this cell's instances of each type.
    cell = {"layer_id": layer_id, "label": label, "folder": folder, "types": foliage_types}
    if bounds_min[0] != float("inf"):
        cell["bounds"] = {"min": bounds_min, "max": bounds_max}
    foliage = {"cells": [cell]}

    manifest = build_manifest(
        asset_name=asset_name,
        bundle_root=bundle_root,
        source_game=source_game,
        content_root=content_root,
        overwrite=overwrite,
        speedtrees=speedtrees,
        foliage=foliage,
        warnings=warnings,
    )

    manifest_path = os.path.join(bundle_root, "witcher_unreal_export.json")
    with open(speedtree_bundle._safe_path(manifest_path), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)

    total_instances = sum(len(t["instances"]) for t in foliage_types)
    return {
        "asset_name": asset_name,
        "bundle_root": bundle_root,
        "manifest_path": manifest_path,
        "flyr_path": abs_flyr,
        "layer_id": layer_id,
        "texture_stats": texture_totals,
        "counts": {
            "tree_types": len(foliage_types),
            "instances": total_instances,
            "missing_srt": missing_srt,
        },
        "elapsed_seconds": time.perf_counter() - started,
        "manifest": manifest,
    }
