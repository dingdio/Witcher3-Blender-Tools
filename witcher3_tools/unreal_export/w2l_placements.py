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
# SBlockData.flags bit 2 = mesh visible in engine. Cleared = "engine hidden"
_SECTOR_FLAG_MESH_VISIBLE = 1 << 2


def _collision_asset_rel(asset_rel: str) -> str:
    return f"{asset_rel}_collision" if asset_rel else ""


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


def _real(transform, name, default=0.0) -> float:
    if transform is None:
        return float(default)
    value = transform.get(name, default) if isinstance(transform, dict) else getattr(transform, name, default)
    try:
        return float(value)
    except Exception:
        return float(default)


def _engine_transform_local_matrix(transform) -> np.ndarray:
    """Build a 4x4 from a normalized engine-transform dict, matching the Blender
    importer (`_engine_transform_to_local_matrix`): YXZ Euler, then scale."""
    from mathutils import Euler, Matrix

    yaw = _real(transform, "Yaw")
    pitch = _real(transform, "Pitch")
    roll = _real(transform, "Roll")
    if yaw == 0.0 and pitch == 0.0 and roll == 0.0:
        mat = Matrix.Identity(4)
    else:
        mat = Euler((np.radians(yaw), np.radians(pitch), np.radians(roll)), "YXZ").to_matrix().to_4x4()
    mat.translation = (_real(transform, "X"), _real(transform, "Y"), _real(transform, "Z"))
    mat = mat @ Matrix.Diagonal((
        _real(transform, "Scale_x", 1.0),
        _real(transform, "Scale_y", 1.0),
        _real(transform, "Scale_z", 1.0),
        1.0,
    ))
    return np.asarray(mat, dtype=float)


def _plan_item_local_matrix(item) -> np.ndarray:
    transform = item.get("transform")
    if transform:
        return _engine_transform_local_matrix(transform)
    rows = item.get("matrix")
    translation = item.get("translation")
    if rows:
        return block_world_matrix(translation or (0.0, 0.0, 0.0), rows)
    m = np.identity(4, dtype=float)
    if translation:
        try:
            m[0, 3], m[1, 3], m[2, 3] = float(translation[0]), float(translation[1]), float(translation[2])
        except Exception:
            pass
    return m


_ENTITY_ROOT_KINDS = {"entity", "entity_empty"}
_ENTITY_MESH_KINDS = {"component_mesh", "mesh"}


def collect_entity_placements(level, context, warnings, assets, entities, skipped, *, include_proxy=False):
    """Walk the level's placed entities via the headless import planner and emit
    ONE grouped entity per top-level CEntity/CGameplayEntity"""
    def _skip(reason):
        skipped[reason] = skipped.get(reason, 0) + 1

    try:
        from ..importers import import_blender_fun
    except Exception:
        return

    try:
        plan = import_blender_fun.resolve_level_import_plan(
            level,
            context,
            do_import_Mesh=False,
            do_import_Collision=False,
            do_import_RigidBody=False,
            do_import_PointLight=False,
            do_import_SpotLight=False,
            do_import_Entity=True,
            do_import_ProxyMesh=bool(include_proxy),
        )
    except Exception as exc:
        warnings.append(f"entity placement planning failed: {exc}")
        log.warning("Entity placement planning failed", exc_info=True)
        return

    items = plan.get("items", []) or []
    children: dict[str, list] = {}
    for item in items:
        children.setdefault(str(item.get("parent_id", "") or ""), []).append(item)

    identity = np.identity(4, dtype=float)

    def gather_components(item, rel, out):
        # rel = transform of this item relative to the owning entity root.
        if str(item.get("kind", "")) in _ENTITY_MESH_KINDS:
            comp = _entity_component(item, rel, assets, warnings, _skip, include_proxy)
            if comp is not None:
                out.append(comp)
        for child in children.get(str(item.get("id", "") or ""), []):
            gather_components(child, rel @ _plan_item_local_matrix(child), out)

    for root in items:
        if str(root.get("kind", "")) not in _ENTITY_ROOT_KINDS or str(root.get("parent_id", "") or ""):
            continue
        entity_world = _plan_item_local_matrix(root)
        components: list[dict[str, Any]] = []
        for child in children.get(str(root.get("id", "") or ""), []):
            gather_components(child, _plan_item_local_matrix(child), components)
        if not components:
            continue
        if not terrain_unreal.world_matrix_has_valid_basis(entity_world):
            entity_world = identity
        entities.append({
            "name": str(root.get("name", "") or "Entity"),
            "matrix": entity_world,
            "components": components,
        })


def _entity_component(item, rel, assets, warnings, skip, include_proxy):
    depot = str(item.get("repo_path", "") or "").strip()
    if not depot:
        skip("entity_empty_depot")
        return None
    if is_volume_depot(depot):
        skip("entity_volume")
        return None
    if not include_proxy and (bool(item.get("is_proxy_mesh")) or is_proxy_depot(depot)):
        skip("entity_proxy")
        return None
    if not terrain_unreal.world_matrix_has_valid_basis(rel):
        skip("entity_degenerate_transform")
        return None

    embedded_index = item.get("embedded_cmesh_chunk_index")
    base_asset_rel = depot_asset_rel(depot)
    if not base_asset_rel:
        warnings.append(f"{depot}: could not derive an Unreal asset path; entity component skipped.")
        return None
    asset_rel = base_asset_rel if embedded_index is None else f"{base_asset_rel}_c{int(embedded_index)}"
    assets.setdefault(asset_rel, {
        "depot": depot,
        "kind": "mesh",
        "base_asset_rel": base_asset_rel,
        "embedded_cmesh_chunk_index": (None if embedded_index is None else int(embedded_index)),
        "cr2w_version": item.get("cr2w_version"),
    })
    return {
        "name": str(item.get("component_name", "") or item.get("name", "") or "Mesh"),
        "type": str(item.get("component_type", "") or "CStaticMeshComponent"),
        "asset_rel": asset_rel,
        "rel_matrix": rel,
        "engine_hidden": item.get("engine_visible") is False,
    }


def _color_rgb(color) -> list[float]:
    try:
        return [
            float(getattr(color, "Red")) / 255.0,
            float(getattr(color, "Green")) / 255.0,
            float(getattr(color, "Blue")) / 255.0,
        ]
    except Exception:
        return [1.0, 1.0, 1.0]


def _float_attr(obj, name: str, default: float = 0.0) -> float:
    try:
        value = getattr(obj, name, default)
        if value is None:
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _w3_direction_to_unreal(direction) -> list[float]:
    try:
        return [float(direction[0]), -float(direction[1]), float(direction[2])]
    except Exception:
        return [1.0, 0.0, 0.0]


def _block_engine_hidden(block, is_collision: bool) -> bool:
    if is_collision:
        return False
    try:
        flags = getattr(block, "flags")
    except Exception:
        return False
    try:
        return not bool(int(flags) & _SECTOR_FLAG_MESH_VISIBLE)
    except Exception:
        return False


def _spot_direction_from_world(world: np.ndarray) -> list[float]:
    try:
        return _w3_direction_to_unreal((-float(world[0, 2]), -float(world[1, 2]), -float(world[2, 2])))
    except Exception:
        return [1.0, 0.0, 0.0]


def _light_entry_from_block(block, light_type: str) -> Optional[dict[str, Any]]:
    light_data = getattr(block, "packedObject", None)
    if light_data is None:
        return None
    world = block_world_matrix(_block_position(block), _block_rotation_rows(block))
    brightness = _float_attr(light_data, "brightness", 0.0)
    radius = _float_attr(light_data, "radius", 0.0)
    is_spot = light_type == "spot"
    entry: dict[str, Any] = {
        "name": "SpotLight" if is_spot else "PointLight",
        "type": light_type,
        "matrix": world,
        "color": _color_rgb(getattr(light_data, "color", None)),
        "intensity": max(0.0, brightness * (300.0 if is_spot else 1000.0)),
        "attenuation_radius": max(0.0, (radius / 255.0) * terrain_unreal.UE_UNITS_PER_METER),
        "source_radius": max(0.0, (radius / 255.0) * terrain_unreal.UE_UNITS_PER_METER),
    }
    if is_spot:
        outer_angle = _float_attr(light_data, "outerAngle", 0.0) * 57.29577951308232
        inner_angle = _float_attr(light_data, "innerAngle", 0.0) * 57.29577951308232
        if inner_angle <= 0.0:
            inner_angle = outer_angle
        entry["direction"] = _spot_direction_from_world(world)
        entry["outer_cone_angle"] = max(0.0, outer_angle)
        entry["inner_cone_angle"] = max(0.0, min(inner_angle, outer_angle))
    return entry


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
    context=None,
    instancer_threshold: int = DEFAULT_INSTANCER_THRESHOLD,
    include_collision_blocks: bool = False,
    include_point_lights: bool = True,
    include_spot_lights: bool = True,
    include_entities: bool = True,
) -> dict[str, Any]:
    from ..CR2W.CR2W_helpers import Enums

    sector = getattr(level, "CSectorData", None)
    assets: dict[str, dict[str, Any]] = {}
    by_asset: dict[str, dict[str, Any]] = {}
    lights: list[dict[str, Any]] = []
    skipped: dict[str, int] = {}

    def _skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    resources = getattr(sector, "Resources", None) or []
    mesh_types = {Enums.BlockDataObjectType.Mesh, Enums.BlockDataObjectType.RigidBody}
    collision_type = Enums.BlockDataObjectType.Collision

    for block in (getattr(sector, "BlockData", None) or []):
        block_type = getattr(block, "packedObjectType", None)
        is_collision = block_type == collision_type
        if block_type == Enums.BlockDataObjectType.PointLight:
            if include_point_lights:
                entry = _light_entry_from_block(block, "point")
                if entry is not None:
                    lights.append(entry)
                else:
                    _skip("point_light_invalid")
            else:
                _skip("point_light")
            continue
        if block_type == Enums.BlockDataObjectType.SpotLight:
            if include_spot_lights:
                entry = _light_entry_from_block(block, "spot")
                if entry is not None:
                    lights.append(entry)
                else:
                    _skip("spot_light_invalid")
            else:
                _skip("spot_light")
            continue
        if block_type not in mesh_types and not (include_collision_blocks and is_collision):
            if is_collision:
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
        if not is_collision and is_volume_depot(depot):
            _skip("volume")
            continue
        if not is_collision and is_proxy_depot(depot):
            _skip("proxy")
            continue

        world = block_world_matrix(_block_position(block), _block_rotation_rows(block))
        if not terrain_unreal.world_matrix_has_valid_basis(world):
            _skip("degenerate_transform")
            continue

        base_asset_rel = depot_asset_rel(depot)
        asset_rel = _collision_asset_rel(base_asset_rel) if is_collision else base_asset_rel
        if not asset_rel:
            warnings.append(f"{depot}: could not derive an Unreal asset path; skipped.")
            continue
        engine_hidden = _block_engine_hidden(block, is_collision)
        group_key = (asset_rel, "collision" if is_collision else "mesh", bool(engine_hidden))
        entry = by_asset.setdefault(group_key, {
            "depot": depot,
            "kind": "collision" if is_collision else "mesh",
            "base_asset_rel": base_asset_rel,
            "matrices": [],
            "engine_hidden": engine_hidden,
        })
        entry["matrices"].append(world)

    actors: list[dict[str, Any]] = []
    instancers: list[dict[str, Any]] = []
    for (asset_rel, _kind, _hidden), entry in by_asset.items():
        matrices = entry["matrices"]
        is_collision = entry.get("kind") == "collision"
        engine_hidden = bool(entry.get("engine_hidden", False))
        assets[asset_rel] = {
            "depot": entry["depot"],
            "kind": entry.get("kind", "mesh"),
            "base_asset_rel": entry.get("base_asset_rel") or asset_rel,
            "embedded_cmesh_chunk_index": entry.get("embedded_cmesh_chunk_index"),
            "cr2w_version": entry.get("cr2w_version"),
        }
        stem = asset_rel.rsplit("/", 1)[-1]
        group_stem = f"{stem}_EngineHidden" if engine_hidden else stem
        if len(matrices) >= instancer_threshold:
            inst = {"name": group_stem, "asset_rel": asset_rel, "matrices": matrices}
            if is_collision:
                inst["collision_only"] = True
            if engine_hidden:
                inst["engine_hidden"] = True
            instancers.append(inst)
        else:
            single = len(matrices) == 1
            for index, world in enumerate(matrices):
                name = group_stem if single else f"{group_stem}_{index + 1:03d}"
                actor = {"name": name, "asset_rel": asset_rel, "matrix": world}
                if is_collision:
                    actor["collision_only"] = True
                if engine_hidden:
                    actor["engine_hidden"] = True
                actors.append(actor)

    entities: list[dict[str, Any]] = []
    if include_entities:
        collect_entity_placements(level, context, warnings, assets, entities, skipped)

    return {
        "assets": assets,
        "actors": actors,
        "instancers": instancers,
        "lights": lights,
        "entities": entities,
        "skipped": skipped,
    }


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


def _gather_layer_assets(
    collected,
    *,
    context,
    bundle_root,
    registry,
    chain,
    used_stems,
    used_fbx_stems,
    gathered_assets,
    failed_assets,
    collision_asset_paths,
    mesh_entries,
    warnings,
    phase,
    skip_materials,
    export_visual_collision,
    reuse_existing_collision_fbx,
) -> None:
    """Gather a single layer's unique placed meshes into the shared bundle.

    ``gathered_assets``/``failed_assets``/``mesh_entries``/``used_stems`` are
    shared across every layer in a multi-layer bundle so a mesh referenced by
    several layers is decoded, materialised, and emitted exactly once.
    """
    from ..CR2W.common_blender import repo_file, win_safe_path
    from .gather import gather_placement_mesh
    from .mesh_buffer import write_mesh_buffer

    for asset_rel, entry in collected["assets"].items():
        if asset_rel in gathered_assets or asset_rel in failed_assets:
            continue
        depot = entry["depot"]
        asset_kind = str(entry.get("kind", "mesh") or "mesh").lower()
        if asset_kind == "collision":
            _g0 = time.perf_counter()
            base_asset_rel = str(entry.get("base_asset_rel") or "").strip()
            if not base_asset_rel:
                warnings.append(f"{depot}: could not derive a collision asset path; skipped.")
                failed_assets.add(asset_rel)
                continue
            collision_entry = _export_collision_mesh_for_asset(
                context,
                base_asset_rel,
                depot,
                bundle_root,
                warnings,
                used_fbx_stems,
                reuse_existing_fbx=reuse_existing_collision_fbx,
            )
            phase["gather"] += time.perf_counter() - _g0
            if not collision_entry:
                failed_assets.add(asset_rel)
                continue
            exported_rel = str(collision_entry.get("asset_path", "") or "")
            if exported_rel != asset_rel:
                warnings.append(
                    f"{depot}: collision asset path mismatch ({exported_rel or '<empty>'} != {asset_rel}); skipped."
                )
                failed_assets.add(asset_rel)
                continue
            mesh_entries.append(collision_entry)
            gathered_assets.add(asset_rel)
            continue

        source = repo_file(normalize_depot_path(depot))
        if not source or not os.path.isfile(win_safe_path(source)):
            warnings.append(f"{depot}: source .w2mesh not found on disk; placement skipped.")
            failed_assets.add(asset_rel)
            continue
        embedded_index = entry.get("embedded_cmesh_chunk_index")
        buffer_path = _unique_buffer_path(bundle_root, asset_rel, used_stems)
        _g0 = time.perf_counter()
        slot_infos: list[dict[str, Any]] = []
        if skip_materials and _buffer_cache_is_fresh(buffer_path, source):
            pass
        else:
            try:
                mesh, slot_infos = gather_placement_mesh(
                    source,
                    version=entry.get("cr2w_version"),
                    warnings=warnings,
                    embedded_cmesh_chunk_index=embedded_index,
                )
            except Exception as exc:
                warnings.append(f"{depot}: mesh gather failed ({exc}); placement skipped.")
                log.warning("Placement gather failed for %s", depot, exc_info=True)
                failed_assets.add(asset_rel)
                continue
            if not mesh.submeshes:
                warnings.append(f"{depot}: mesh has no geometry; placement skipped.")
                failed_assets.add(asset_rel)
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

        if export_visual_collision:
            _c0 = time.perf_counter()
            collision_entry = _export_collision_mesh_for_asset(
                context,
                asset_rel,
                depot,
                bundle_root,
                warnings,
                used_fbx_stems,
                reuse_existing_fbx=reuse_existing_collision_fbx,
            )
            phase["gather"] += time.perf_counter() - _c0
            if collision_entry:
                collision_rel = str(collision_entry.get("asset_path", "") or "")
                if collision_rel and collision_rel not in gathered_assets:
                    mesh_entries.append(collision_entry)
                    gathered_assets.add(collision_rel)
                if collision_rel:
                    collision_asset_paths[asset_rel] = collision_rel


def _layer_placement_group(layer_id, label, folder, collected, gathered_assets, collision_asset_paths=None, default_hidden=False) -> Optional[dict]:
    """Build the manifest placement group for one layer, keeping only meshes
    that were successfully gathered somewhere in the (possibly multi-layer)
    bundle. ``default_hidden`` marks the whole layer as a RED "Default Hidden"
    group (its parent LayerGroup has visible_on_start == 0)."""
    collision_asset_paths = collision_asset_paths or {}
    actors_out: list[dict[str, Any]] = []
    for actor in collected["actors"]:
        if actor["asset_rel"] not in gathered_assets:
            continue
        actor_entry = {
            "name": actor["name"],
            "asset_path": actor["asset_rel"],
            "transform": terrain_unreal.w3_matrix_to_unreal(actor["matrix"]),
        }
        if actor.get("collision_only"):
            actor_entry["collision_only"] = True
        else:
            collision_asset_path = collision_asset_paths.get(actor["asset_rel"])
            if collision_asset_path:
                actor_entry["collision_asset_path"] = collision_asset_path
        if actor.get("engine_hidden"):
            actor_entry["engine_hidden"] = True
        if default_hidden:
            actor_entry["default_hidden"] = True
        actors_out.append(actor_entry)

    instancers_out: list[dict[str, Any]] = []
    for inst in collected["instancers"]:
        if inst["asset_rel"] not in gathered_assets or not inst["matrices"]:
            continue
        inst_entry = {
            "name": inst["name"],
            "asset_path": inst["asset_rel"],
            "instances": [terrain_unreal.w3_matrix_to_unreal(m) for m in inst["matrices"]],
        }
        if inst.get("collision_only"):
            inst_entry["collision_only"] = True
        else:
            collision_asset_path = collision_asset_paths.get(inst["asset_rel"])
            if collision_asset_path:
                inst_entry["collision_asset_path"] = collision_asset_path
        if inst.get("engine_hidden"):
            inst_entry["engine_hidden"] = True
        if default_hidden:
            inst_entry["default_hidden"] = True
        instancers_out.append(inst_entry)

    lights_out: list[dict[str, Any]] = []
    for light in collected.get("lights", []) or []:
        matrix = light.get("matrix")
        if matrix is None:
            continue
        light_entry = {
            "name": light.get("name", "Light"),
            "type": light.get("type", "point"),
            "transform": terrain_unreal.w3_matrix_to_unreal(matrix),
            "color": light.get("color", [1.0, 1.0, 1.0]),
            "intensity": light.get("intensity", 0.0),
            "attenuation_radius": light.get("attenuation_radius", 0.0),
            "source_radius": light.get("source_radius", 0.0),
        }
        if light_entry["type"] == "spot":
            light_entry["direction"] = light.get("direction", [1.0, 0.0, 0.0])
            light_entry["inner_cone_angle"] = light.get("inner_cone_angle", 0.0)
            light_entry["outer_cone_angle"] = light.get("outer_cone_angle", 0.0)
        if default_hidden:
            light_entry["default_hidden"] = True
        lights_out.append(light_entry)

    entities_out: list[dict[str, Any]] = []
    for entity in collected.get("entities", []) or []:
        comps_out = []
        for comp in entity.get("components", []):
            if comp["asset_rel"] not in gathered_assets:
                continue
            comp_entry = {
                "name": comp.get("name", "Mesh"),
                "type": comp.get("type", "CStaticMeshComponent"),
                "asset_path": comp["asset_rel"],
                "transform": terrain_unreal.w3_matrix_to_unreal(comp["rel_matrix"]),
            }
            if comp.get("engine_hidden"):
                comp_entry["engine_hidden"] = True
            collision_asset_path = collision_asset_paths.get(comp["asset_rel"])
            if collision_asset_path:
                comp_entry["collision_asset_path"] = collision_asset_path
            comps_out.append(comp_entry)
        if not comps_out:
            continue
        entity_entry = {
            "name": entity.get("name", "Entity"),
            "transform": terrain_unreal.w3_matrix_to_unreal(entity["matrix"]),
            "components": comps_out,
        }
        if default_hidden:
            entity_entry["default_hidden"] = True
        entities_out.append(entity_entry)

    if not actors_out and not instancers_out and not lights_out and not entities_out:
        return None
    return {
        "layer_id": layer_id,
        "label": label,
        "folder": folder,
        "actors": actors_out,
        "instancers": instancers_out,
        "lights": lights_out,
        "entities": entities_out,
    }


def _build_unreal_w2l_bundle_core(
    context,
    settings,
    w2l_paths,
    *,
    include_collision_blocks=None,
    include_point_lights=True,
    include_spot_lights=True,
    default_hidden_paths=None,
) -> dict[str, Any]:
    from ..CR2W.CR2W_reader import load_w2l
    from ..CR2W.common_blender import redkit_repo_context

    def _norm_abs(path):
        return os.path.normcase(os.path.normpath(str(path or "")))

    default_hidden_set = {_norm_abs(p) for p in (default_hidden_paths or [])}
    from .bundle import (
        _resolve_content_root_setting,
        default_export_folder,
        overwrite_policy_from_settings,
    )
    from .material_chain import ChainBuilder
    from .texture_export import TextureRegistry

    if not w2l_paths:
        raise ValueError("No .w2l layers given.")

    started = time.perf_counter()
    phase = {"parse": 0.0, "gather": 0.0, "materials": 0.0, "textures": 0.0, "manifest": 0.0}
    skip_materials = bool(getattr(settings, "placement_skip_materials", False))
    export_visual_collision = bool(getattr(settings, "placement_export_collision", False))
    if include_collision_blocks is None:
        include_collision_blocks = export_visual_collision
    include_collision_blocks = bool(include_collision_blocks)

    # First pass: parse every layer and gather its placements. We defer bundle
    # setup until the first parse so source_game is derived from real data.
    parsed: list[dict[str, Any]] = []
    warnings: list[str] = []
    source_game: Optional[str] = None
    skipped_totals: dict[str, int] = {}

    bundle_state: dict[str, Any] = {}

    def _ensure_bundle(level) -> None:
        nonlocal source_game
        if bundle_state:
            return
        level_version = getattr(level, "version", None)
        source_game = "w2" if (level_version is not None and int(level_version) <= 115) else "w3"
        first_label = _layer_label_and_folder(_layer_id_for_w2l(w2l_paths[0]))[0]
        default_name = first_label if len(w2l_paths) == 1 else "WitcherLayers"
        asset_name = safe_asset_name(getattr(settings, "asset_name", "") or default_name or "WitcherLayer")
        content_root = _resolve_content_root_setting(getattr(settings, "content_root", ""), source_game)
        export_root = str(getattr(settings, "export_folder", "") or default_export_folder())
        bundle_root = os.path.join(export_root, asset_name)
        os.makedirs(bundle_root, exist_ok=True)
        registry = TextureRegistry(bundle_root, parallel=True)
        overwrite = overwrite_policy_from_settings(settings)
        bundle_state.update({
            "asset_name": asset_name,
            "content_root": content_root,
            "bundle_root": bundle_root,
            "registry": registry,
            "chain": ChainBuilder(registry.register),
            "overwrite": overwrite,
            "used_stems": {},
            "used_fbx_stems": {},
            "mesh_entries": [],
            "gathered_assets": set(),
            "failed_assets": set(),
            "collision_asset_paths": {},
            "reuse_existing_collision_fbx": not bool(overwrite.get("meshes", False)),
        })

    for raw_path in w2l_paths:
        abs_w2l = _resolve_w2l_abspath(raw_path)
        layer_id = _layer_id_for_w2l(abs_w2l)
        label, folder = _layer_label_and_folder(layer_id)
        # Resolve placed meshes/entities against the REDkit uncooked depot first
        # (same dual-depot lookup the normal .w2l import uses) so we read the
        # uncooked .w2mesh -- which carries embedded CCollisionMesh -- instead of
        # the cooked uncook copy.
        with redkit_repo_context(abs_w2l):
            _parse0 = time.perf_counter()
            level = load_w2l(abs_w2l)
            collected = collect_w2l_placements(
                level,
                warnings,
                context=context,
                include_collision_blocks=include_collision_blocks,
                include_point_lights=include_point_lights,
                include_spot_lights=include_spot_lights,
            )
            phase["parse"] += time.perf_counter() - _parse0
            for reason, count in (collected.get("skipped") or {}).items():
                skipped_totals[reason] = skipped_totals.get(reason, 0) + int(count or 0)
            if not collected["assets"] and not collected.get("lights"):
                continue
            _ensure_bundle(level)
            if collected["assets"]:
                _gather_layer_assets(
                    collected,
                    context=context,
                    bundle_root=bundle_state["bundle_root"],
                    registry=bundle_state["registry"],
                    chain=bundle_state["chain"],
                    used_stems=bundle_state["used_stems"],
                    used_fbx_stems=bundle_state["used_fbx_stems"],
                    gathered_assets=bundle_state["gathered_assets"],
                    failed_assets=bundle_state["failed_assets"],
                    collision_asset_paths=bundle_state["collision_asset_paths"],
                    mesh_entries=bundle_state["mesh_entries"],
                    warnings=warnings,
                    phase=phase,
                    skip_materials=skip_materials,
                    export_visual_collision=export_visual_collision,
                    reuse_existing_collision_fbx=bundle_state["reuse_existing_collision_fbx"],
                )
            parsed.append({
                "layer_id": layer_id,
                "label": label,
                "folder": folder,
                "collected": collected,
                "default_hidden": _norm_abs(abs_w2l) in default_hidden_set,
            })

    if not bundle_state:
        labels = ", ".join(sorted({_layer_label_and_folder(_layer_id_for_w2l(_resolve_w2l_abspath(p)))[0] for p in w2l_paths}))
        raise ValueError(
            f"No placed meshes found in {labels or 'the selected layers'}. "
            f"(Skipped: {skipped_totals or 'nothing'}.)"
        )

    gathered_assets = bundle_state["gathered_assets"]
    layers_out: list[dict[str, Any]] = []
    layer_ids: list[str] = []
    for item in parsed:
        group = _layer_placement_group(
            item["layer_id"],
            item["label"],
            item["folder"],
            item["collected"],
            gathered_assets,
            bundle_state["collision_asset_paths"],
            default_hidden=item.get("default_hidden", False),
        )
        if group is not None:
            layers_out.append(group)
            layer_ids.append(item["layer_id"])

    placements = {"layers": layers_out} if layers_out else None

    registry = bundle_state["registry"]
    chain = bundle_state["chain"]
    _tex0 = time.perf_counter()
    texture_entries = [] if skip_materials else registry.manifest_entries()
    masters = [] if skip_materials else chain.ordered_masters()
    materials = [] if skip_materials else chain.ordered_materials()
    chain_warnings = [] if skip_materials else (chain.warnings + registry.warnings)
    phase["textures"] = time.perf_counter() - _tex0

    _man0 = time.perf_counter()
    manifest = build_manifest(
        asset_name=bundle_state["asset_name"],
        bundle_root=bundle_state["bundle_root"],
        source_game=source_game,
        content_root=bundle_state["content_root"],
        overwrite=bundle_state["overwrite"],
        meshes=bundle_state["mesh_entries"],
        masters=masters,
        materials=materials,
        textures=texture_entries,
        placements=placements,
        warnings=warnings + chain_warnings,
    )

    manifest_path = os.path.join(bundle_state["bundle_root"], "witcher_unreal_export.json")
    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)
    phase["manifest"] = time.perf_counter() - _man0

    total_actors = sum(len(g["actors"]) for g in layers_out)
    total_instancers = sum(len(g["instancers"]) for g in layers_out)
    total_instances = sum(len(i["instances"]) for g in layers_out for i in g["instancers"])
    total_lights = sum(len(g["lights"]) for g in layers_out)
    total_entities = sum(len(g.get("entities", [])) for g in layers_out)
    total_entity_components = sum(len(e.get("components", [])) for g in layers_out for e in g.get("entities", []))

    return {
        "asset_name": bundle_state["asset_name"],
        "bundle_root": bundle_state["bundle_root"],
        "manifest_path": manifest_path,
        "layer_ids": layer_ids,
        "skip_materials": skip_materials,
        "build_timings": phase,
        "counts": {
            "unique_meshes": len(gathered_assets),
            "actors": total_actors,
            "instancers": total_instancers,
            "instances": total_instances,
            "lights": total_lights,
            "entities": total_entities,
            "entity_components": total_entity_components,
            "materials": len(materials),
            "textures": len(texture_entries),
            "layers": len(w2l_paths),
            "layers_with_placements": len(layers_out),
            "skipped": skipped_totals,
        },
        "elapsed_seconds": time.perf_counter() - started,
        "manifest": manifest,
    }


def build_unreal_w2l_bundle(
    context,
    settings,
    w2l_path: str,
    *,
    include_collision_blocks=None,
    include_point_lights=True,
    include_spot_lights=True,
) -> dict[str, Any]:
    result = _build_unreal_w2l_bundle_core(
        context,
        settings,
        [w2l_path],
        include_collision_blocks=include_collision_blocks,
        include_point_lights=include_point_lights,
        include_spot_lights=include_spot_lights,
    )
    # Preserve the single-layer return contract used by the existing operator.
    layer_ids = result.get("layer_ids", [])
    result["layer_id"] = layer_ids[0] if layer_ids else _layer_id_for_w2l(_resolve_w2l_abspath(w2l_path))
    return result


def build_unreal_w2l_bundle_multi(
    context,
    settings,
    w2l_paths: list[str],
    *,
    include_collision_blocks=None,
    include_point_lights=True,
    include_spot_lights=True,
    default_hidden_paths=None,
) -> dict[str, Any]:
    return _build_unreal_w2l_bundle_core(
        context,
        settings,
        list(w2l_paths),
        include_collision_blocks=include_collision_blocks,
        include_point_lights=include_point_lights,
        include_spot_lights=include_spot_lights,
        default_hidden_paths=default_hidden_paths,
    )


def _export_collision_mesh_for_asset(*args, **kwargs):
    from .placements_bundle import _export_collision_mesh_for_asset as export_collision

    return export_collision(*args, **kwargs)


def _unique_buffer_path(bundle_root: str, asset_rel: str, used_stems: dict[str, str]) -> str:
    from .bundle import _unique_bundle_file

    return _unique_bundle_file(bundle_root, asset_rel, used_stems, "Meshes", ".w3buf")


def _buffer_cache_is_fresh(buffer_path: str, source_path: str) -> bool:
    from ..CR2W.common_blender import win_safe_path

    try:
        if not os.path.isfile(buffer_path) or os.path.getsize(buffer_path) <= 0:
            return False
        safe_source = win_safe_path(source_path)
        if os.path.isfile(safe_source) and os.path.getmtime(buffer_path) < os.path.getmtime(safe_source):
            return False
        return True
    except OSError:
        return False
