from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

import numpy as np

from . import terrain_unreal
from .manifest import (
    build_manifest,
    depot_asset_dir,
    depot_asset_rel,
    normalize_depot_path,
    relpath_for_manifest,
    safe_asset_name,
)

log = logging.getLogger(__name__)

_VOLUME_TOKENS = ("volume", "trigger", "dimmer", "occlusion", "_occ", "blocker", "nointer")
_PROXY_TOKENS = ("proxy",)
DEFAULT_INSTANCER_THRESHOLD = 8


def _layer_id_for_w2l(w2l_path: str) -> str:
    from ..importers.import_mesh import get_repo_from_abs_path

    raw = str(w2l_path or "").strip().strip('"')
    if os.path.isabs(raw) or os.path.splitdrive(raw)[0]:
        try:
            rel = get_repo_from_abs_path(os.path.normpath(raw)) or ""
        except Exception:
            rel = ""
        if rel:
            return normalize_depot_path(rel)
        return normalize_depot_path(os.path.basename(raw))
    return normalize_depot_path(raw)


def _layer_label_and_folder(layer_id: str) -> tuple[str, str]:
    norm = normalize_depot_path(layer_id)
    if not norm:
        return "placements", ""
    segments = [seg for seg in norm.split("\\") if seg]
    label = segments[-1] if segments else "placements"
    folder = "/".join(segments[:-1])
    return label, folder


def _block_position(block) -> tuple[float, float, float]:
    pos = getattr(block, "position", None)
    if pos is None:
        return (0.0, 0.0, 0.0)
    try:
        return (float(pos.x), float(pos.y), float(pos.z))
    except Exception:
        try:
            return (float(pos[0]), float(pos[1]), float(pos[2]))
        except Exception:
            return (0.0, 0.0, 0.0)


def _block_rotation_rows(block) -> list[list[float]]:
    rm = getattr(block, "rotationMatrix", None)
    if rm is None:
        return [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    try:
        return [
            [float(rm.ax), float(rm.ay), float(rm.az)],
            [float(rm.bx), float(rm.by), float(rm.bz)],
            [float(rm.cx), float(rm.cy), float(rm.cz)],
        ]
    except Exception:
        return [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]


def block_world_matrix(position, rotation_rows) -> np.ndarray:
    m = np.identity(4, dtype=float)
    m[:3, :3] = np.asarray(rotation_rows, dtype=float).reshape(3, 3).T
    m[0, 3], m[1, 3], m[2, 3] = (float(position[0]), float(position[1]), float(position[2]))
    return m


def _depot_has_token(depot: str, tokens) -> bool:
    lowered = str(depot or "").lower()
    return any(tok in lowered for tok in tokens)


def is_volume_depot(depot: str) -> bool:
    return _depot_has_token(depot, _VOLUME_TOKENS)


def is_proxy_depot(depot: str) -> bool:
    return _depot_has_token(depot, _PROXY_TOKENS)


def collect_w2l_placements(
    level,
    warnings: list[str],
    *,
    instancer_threshold: int = DEFAULT_INSTANCER_THRESHOLD,
) -> dict[str, Any]:
    from ..CR2W.CR2W_helpers import Enums

    sector = getattr(level, "CSectorData", None)
    assets: dict[str, dict[str, Any]] = {}
    by_depot: dict[str, list[np.ndarray]] = {}
    skipped: dict[str, int] = {}

    def _skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    if not sector or not getattr(sector, "BlockData", None):
        return {"assets": assets, "actors": [], "instancers": [], "skipped": skipped}

    resources = getattr(sector, "Resources", None) or []
    mesh_types = {Enums.BlockDataObjectType.Mesh, Enums.BlockDataObjectType.RigidBody}

    for block in sector.BlockData:
        block_type = getattr(block, "packedObjectType", None)
        if block_type not in mesh_types:
            if block_type == Enums.BlockDataObjectType.PointLight:
                _skip("point_light")
            elif block_type == Enums.BlockDataObjectType.SpotLight:
                _skip("spot_light")
            elif block_type == Enums.BlockDataObjectType.Collision:
                _skip("collision")
            else:
                _skip("other")
            continue

        try:
            mesh_index = block.packedObject.meshIndex
            depot = str(resources[mesh_index].pathHash or "")
        except Exception:
            _skip("unresolved_mesh")
            continue
        if not depot:
            _skip("empty_depot")
            continue
        if is_volume_depot(depot):
            _skip("volume")
            continue
        if is_proxy_depot(depot):
            _skip("proxy")
            continue

        world = block_world_matrix(_block_position(block), _block_rotation_rows(block))
        if not terrain_unreal.world_matrix_has_valid_basis(world):
            _skip("degenerate_transform")
            continue
        by_depot.setdefault(depot, []).append(world)

    actors: list[dict[str, Any]] = []
    instancers: list[dict[str, Any]] = []
    for depot, matrices in by_depot.items():
        asset_rel = depot_asset_rel(depot)
        if not asset_rel:
            warnings.append(f"{depot}: could not derive an Unreal asset path; skipped.")
            continue
        assets[asset_rel] = {"depot": depot}
        stem = asset_rel.rsplit("/", 1)[-1]
        if len(matrices) >= instancer_threshold:
            instancers.append({"name": stem, "asset_rel": asset_rel, "matrices": matrices})
        else:
            single = len(matrices) == 1
            for index, world in enumerate(matrices):
                name = stem if single else f"{stem}_{index + 1:03d}"
                actors.append({"name": name, "asset_rel": asset_rel, "matrix": world})

    return {"assets": assets, "actors": actors, "instancers": instancers, "skipped": skipped}


def _resolve_w2l_abspath(w2l_path: str) -> str:
    raw = str(w2l_path or "").strip().strip('"')
    if not raw:
        raise ValueError("No .w2l path given.")
    if os.path.isfile(raw):
        return raw
    from ..CR2W.common_blender import repo_file

    resolved = repo_file(normalize_depot_path(raw))
    if not resolved or not os.path.isfile(resolved):
        raise ValueError(f"Could not resolve .w2l on disk: {w2l_path}")
    return resolved


def build_unreal_w2l_bundle(context, settings, w2l_path: str) -> dict[str, Any]:
    from ..CR2W.CR2W_reader import load_w2l
    from ..CR2W.common_blender import repo_file
    from .bundle import (
        _resolve_content_root_setting,
        default_export_folder,
        overwrite_policy_from_settings,
    )
    from .gather import gather_placement_mesh
    from .material_chain import ChainBuilder
    from .mesh_buffer import write_mesh_buffer
    from .texture_export import TextureRegistry

    started = time.perf_counter()
    phase = {"parse": 0.0, "gather": 0.0, "materials": 0.0, "textures": 0.0, "manifest": 0.0}
    abs_w2l = _resolve_w2l_abspath(w2l_path)
    layer_id = _layer_id_for_w2l(abs_w2l)
    label, folder = _layer_label_and_folder(layer_id)

    warnings: list[str] = []
    _parse0 = time.perf_counter()
    level = load_w2l(abs_w2l)
    collected = collect_w2l_placements(level, warnings)
    phase["parse"] = time.perf_counter() - _parse0
    assets = collected["assets"]
    if not assets:
        raise ValueError(
            f"No placed meshes found in '{label}'. (Skipped: {collected['skipped'] or 'nothing'}.)"
        )

    level_version = getattr(level, "version", None)
    source_game = "w2" if (level_version is not None and int(level_version) <= 115) else "w3"
    asset_name = safe_asset_name(getattr(settings, "asset_name", "") or label or "WitcherLayer")
    content_root = _resolve_content_root_setting(getattr(settings, "content_root", ""), source_game)

    export_root = str(getattr(settings, "export_folder", "") or default_export_folder())
    bundle_root = os.path.join(export_root, asset_name)
    os.makedirs(bundle_root, exist_ok=True)

    overwrite = overwrite_policy_from_settings(settings)
    skip_materials = bool(getattr(settings, "placement_skip_materials", False))

    registry = TextureRegistry(bundle_root, parallel=True)
    chain = ChainBuilder(registry.register)

    mesh_entries: list[dict[str, Any]] = []
    used_stems: dict[str, str] = {}
    gathered_assets: set[str] = set()

    for asset_rel, entry in assets.items():
        depot = entry["depot"]
        source = repo_file(normalize_depot_path(depot))
        if not source or not os.path.isfile(source):
            warnings.append(f"{depot}: source .w2mesh not found on disk; placement skipped.")
            continue
        buffer_path = _unique_buffer_path(bundle_root, asset_rel, used_stems)
        _g0 = time.perf_counter()
        slot_infos: list[dict[str, Any]] = []
        if skip_materials and _buffer_cache_is_fresh(buffer_path, source):
            pass
        else:
            try:
                mesh, slot_infos = gather_placement_mesh(source, version=None, warnings=warnings)
            except Exception as exc:
                warnings.append(f"{depot}: mesh gather failed ({exc}); placement skipped.")
                log.warning("Placement gather failed for %s", depot, exc_info=True)
                continue
            if not mesh.submeshes:
                warnings.append(f"{depot}: mesh has no geometry; placement skipped.")
                continue
            write_mesh_buffer(buffer_path, mesh)
        phase["gather"] += time.perf_counter() - _g0

        slots: list[dict[str, Any]] = []
        if not skip_materials:
            _m0 = time.perf_counter()
            asset_dir = depot_asset_dir(asset_rel)
            for mat_info in slot_infos:
                material_id = chain.add_slot_material(mat_info, asset_dir)
                slots.append({
                    "slot_index": int(mat_info.get("material_slot_index", len(slots))),
                    "slot_name": str(mat_info.get("name", "")),
                    "material_id": material_id,
                })
            phase["materials"] += time.perf_counter() - _m0

        mesh_entries.append({
            "name": asset_rel.rsplit("/", 1)[-1],
            "asset_path": asset_rel,
            "kind": "static",
            "slots": slots,
            "buffer": relpath_for_manifest(buffer_path, bundle_root),
        })
        gathered_assets.add(asset_rel)

    actors_out: list[dict[str, Any]] = []
    for actor in collected["actors"]:
        if actor["asset_rel"] not in gathered_assets:
            continue
        actors_out.append({
            "name": actor["name"],
            "asset_path": actor["asset_rel"],
            "transform": terrain_unreal.w3_matrix_to_unreal(actor["matrix"]),
        })

    instancers_out: list[dict[str, Any]] = []
    for inst in collected["instancers"]:
        if inst["asset_rel"] not in gathered_assets or not inst["matrices"]:
            continue
        instancers_out.append({
            "name": inst["name"],
            "asset_path": inst["asset_rel"],
            "instances": [terrain_unreal.w3_matrix_to_unreal(m) for m in inst["matrices"]],
        })

    placements = None
    if actors_out or instancers_out:
        placements = {
            "layers": [{
                "layer_id": layer_id,
                "label": label,
                "folder": folder,
                "actors": actors_out,
                "instancers": instancers_out,
                "lights": [],
            }]
        }

    _tex0 = time.perf_counter()
    texture_entries = [] if skip_materials else registry.manifest_entries()
    masters = [] if skip_materials else chain.ordered_masters()
    materials = [] if skip_materials else chain.ordered_materials()
    chain_warnings = [] if skip_materials else (chain.warnings + registry.warnings)
    phase["textures"] = time.perf_counter() - _tex0

    _man0 = time.perf_counter()
    manifest = build_manifest(
        asset_name=asset_name,
        bundle_root=bundle_root,
        source_game=source_game,
        content_root=content_root,
        overwrite=overwrite,
        meshes=mesh_entries,
        masters=masters,
        materials=materials,
        textures=texture_entries,
        placements=placements,
        warnings=warnings + chain_warnings,
    )

    manifest_path = os.path.join(bundle_root, "witcher_unreal_export.json")
    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)
    phase["manifest"] = time.perf_counter() - _man0

    return {
        "asset_name": asset_name,
        "bundle_root": bundle_root,
        "manifest_path": manifest_path,
        "layer_id": layer_id,
        "skip_materials": skip_materials,
        "build_timings": phase,
        "counts": {
            "unique_meshes": len(gathered_assets),
            "actors": len(actors_out),
            "instancers": len(instancers_out),
            "instances": sum(len(i["instances"]) for i in instancers_out),
            "materials": len(materials),
            "textures": len(texture_entries),
            "skipped": collected["skipped"],
        },
        "elapsed_seconds": time.perf_counter() - started,
        "manifest": manifest,
    }


def _unique_buffer_path(bundle_root: str, asset_rel: str, used_stems: dict[str, str]) -> str:
    from .bundle import _unique_bundle_file

    return _unique_bundle_file(bundle_root, asset_rel, used_stems, "Meshes", ".w3buf")


def _buffer_cache_is_fresh(buffer_path: str, source_path: str) -> bool:
    try:
        if not os.path.isfile(buffer_path) or os.path.getsize(buffer_path) <= 0:
            return False
        if os.path.isfile(source_path) and os.path.getmtime(buffer_path) < os.path.getmtime(source_path):
            return False
        return True
    except OSError:
        return False
