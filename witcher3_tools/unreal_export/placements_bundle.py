"""Build an Unreal export bundle for placed world geometry (CSectorData).

Every placement transform goes through `terrain_unreal.w3_matrix_to_unreal`
so it shares the one canonical W3->UE frame the landscape was exported in (see
``terrain_unreal.w3_world_to_unreal``); a known building landing on the right
terrain spot is the definitive alignment test for that frame.

v1 scope: static meshes and imported point/spot lights. Entities
(``.w2ent`` placements) are deferred.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from collections import defaultdict
from contextlib import contextmanager, nullcontext
from typing import Any, Optional

from .manifest import (
    build_manifest,
    depot_asset_dir,
    depot_asset_rel,
    normalize_depot_path,
    relpath_for_manifest,
    safe_asset_name,
)
from . import terrain_unreal
from .bundle import (
    collect_material_infos,
    _object_lod_index,
    _object_source_game,
    _resolve_content_root_setting,
    _unique_bundle_file,
    _unique_fbx_path,
    default_export_folder,
    export_fbx,
    mesh_depot_path,
    overwrite_policy_from_settings,
)
from .material_chain import ChainBuilder
from .scene_utils import _remove_object
from .texture_export import TextureRegistry

log = logging.getLogger(__name__)


# ---- selection / layer identity -------------------------------------------

def _expand_selection(objects) -> list[Any]:
    """Selected objects plus their full child hierarchy (instancers live under
    a layer parent empty, so selecting the parent should pull them in too)."""
    seen: dict[int, Any] = {}

    def add(obj):
        if obj is None:
            return
        key = _object_key(obj)
        if key in seen:
            return
        seen[key] = obj

    for obj in objects or []:
        add(obj)
        for child in getattr(obj, "children_recursive", []) or []:
            add(child)
    return list(seen.values())


def _object_key(obj) -> int:
    try:
        return int(obj.as_pointer())
    except Exception:
        return id(obj)


def _custom_prop(obj, key: str, default=None):
    try:
        return obj.get(key, default)
    except Exception:
        return default


def _depot_rel_path(path: str) -> str:
    raw = str(path or "").strip().strip('"')
    if not raw:
        return ""
    if os.path.isabs(raw) or os.path.splitdrive(raw)[0]:
        try:
            from ..importers.import_mesh import get_repo_from_abs_path

            rel = get_repo_from_abs_path(os.path.normpath(raw)) or ""
        except Exception:
            rel = ""
        if rel and not (os.path.isabs(rel) or os.path.splitdrive(rel)[0]):
            raw = rel
        else:
            raw = os.path.basename(raw)
    return normalize_depot_path(raw)


def _layer_id_for_object(obj) -> str:
    node = obj
    visited = set()
    while node is not None and _object_key(node) not in visited:
        visited.add(_object_key(node))
        for coll in getattr(node, "users_collection", []) or []:
            try:
                level = coll.get("witcher_layer_import_level")
            except Exception:
                level = None
            if level:
                depot = _depot_rel_path(str(level))
                return depot or str(getattr(coll, "name", "") or "placements")
        node = getattr(node, "parent", None)

    for coll in getattr(obj, "users_collection", []) or []:
        name = str(getattr(coll, "name", "") or "").strip()
        if name and name.lower() not in ("scene collection", "master collection"):
            return name
    return "placements"


def _layer_label_and_folder(layer_id: str) -> tuple[str, str]:
    norm = normalize_depot_path(layer_id)
    if not norm:
        return "placements", ""
    segments = [seg for seg in norm.split("\\") if seg]
    label = segments[-1] if segments else "placements"
    folder = "/".join(segments[:-1])
    return label, folder


# ---- placement collection --------------------------------------------------

def _sector_source_object(inst_obj):
    for mod in getattr(inst_obj, "modifiers", []) or []:
        if getattr(mod, "type", "") != "NODES":
            continue
        node_group = getattr(mod, "node_group", None)
        if node_group is None:
            continue
        for node in getattr(node_group, "nodes", []) or []:
            if getattr(node, "bl_idname", "") == "GeometryNodeObjectInfo":
                try:
                    src = node.inputs["Object"].default_value
                except Exception:
                    src = None
                if src is not None and getattr(src, "type", "") == "MESH":
                    return src
    return None


def _sector_point_world_matrices(inst_obj):
    from mathutils import Euler, Matrix, Vector

    mesh = getattr(inst_obj, "data", None)
    verts = getattr(mesh, "vertices", None) if mesh is not None else None
    if not verts:
        return []
    count = len(verts)

    co = [0.0] * (count * 3)
    verts.foreach_get("co", co)

    def _read_vec_attr(name, default):
        attr = mesh.attributes.get(name) if mesh.attributes else None
        if attr is None or len(attr.data) != count:
            return [default] * count
        flat = [0.0] * (count * 3)
        attr.data.foreach_get("vector", flat)
        return [(flat[i * 3], flat[i * 3 + 1], flat[i * 3 + 2]) for i in range(count)]

    rots = _read_vec_attr("rot", (0.0, 0.0, 0.0))
    scales = _read_vec_attr("scale", (1.0, 1.0, 1.0))

    base = inst_obj.matrix_world.copy()
    matrices = []
    for i in range(count):
        pos = Vector((co[i * 3], co[i * 3 + 1], co[i * 3 + 2]))
        rot_m = Euler(rots[i], "XYZ").to_matrix().to_4x4()
        scale_m = Matrix.Diagonal((scales[i][0], scales[i][1], scales[i][2], 1.0))
        matrices.append(base @ Matrix.Translation(pos) @ rot_m @ scale_m)
    return matrices


def _w3_direction_to_unreal(direction) -> list[float]:
    try:
        x = float(direction.x)
        y = float(direction.y)
        z = float(direction.z)
    except Exception:
        try:
            values = list(direction)
        except Exception:
            values = []
        x = float(values[0]) if len(values) > 0 else 0.0
        y = float(values[1]) if len(values) > 1 else 0.0
        z = float(values[2]) if len(values) > 2 else 0.0
    x, y, z = x, y * terrain_unreal.UNREAL_Y_SIGN, z
    length = (x * x + y * y + z * z) ** 0.5
    if length <= 1e-8:
        return [1.0, 0.0, 0.0]
    return [x / length, y / length, z / length]


def _light_world_direction(obj) -> tuple[float, float, float]:
    matrix = getattr(obj, "matrix_world", None)
    if matrix is None:
        return (0.0, 0.0, -1.0)
    # Blender spot lights emit along local -Z.
    try:
        return (-float(matrix[0][2]), -float(matrix[1][2]), -float(matrix[2][2]))
    except Exception:
        return (0.0, 0.0, -1.0)


def _float_attr(obj, name: str, default: float = 0.0) -> float:
    try:
        return float(getattr(obj, name, default))
    except Exception:
        return float(default)


def _light_entry_from_object(obj) -> Optional[dict[str, Any]]:
    data = getattr(obj, "data", None)
    light_type = str(getattr(data, "type", "") or "").strip().upper()
    if light_type not in {"POINT", "SPOT"}:
        return None
    matrix = getattr(obj, "matrix_world", None)
    if matrix is None:
        return None

    color = getattr(data, "color", (1.0, 1.0, 1.0))
    try:
        color_values = [float(color[0]), float(color[1]), float(color[2])]
    except Exception:
        color_values = [1.0, 1.0, 1.0]

    energy = _float_attr(data, "energy", 0.0)
    radius_m = _float_attr(data, "cutoff_distance", 0.0) or _float_attr(data, "shadow_soft_size", 0.0)
    entry: dict[str, Any] = {
        "name": str(getattr(obj, "name", "") or ("SpotLight" if light_type == "SPOT" else "PointLight")),
        "type": "spot" if light_type == "SPOT" else "point",
        "matrix": matrix.copy() if matrix is not None else None,
        "color": color_values,
        # Blender importer stores Witcher brightness as low Blender energy values;
        # UE light intensity needs a larger scale to be visible in level lighting.
        "intensity": max(0.0, energy * 100.0),
        "attenuation_radius": max(0.0, radius_m * terrain_unreal.UE_UNITS_PER_METER),
        "source_radius": max(0.0, _float_attr(data, "shadow_soft_size", 0.0) * terrain_unreal.UE_UNITS_PER_METER),
    }

    if light_type == "SPOT":
        outer_rad = max(0.0, _float_attr(data, "spot_size", 0.0))
        blend = min(1.0, max(0.0, _float_attr(data, "spot_blend", 0.0)))
        outer_deg = outer_rad * 57.29577951308232
        entry["outer_cone_angle"] = outer_deg
        entry["inner_cone_angle"] = outer_deg * (1.0 - blend)
        entry["direction"] = _w3_direction_to_unreal(_light_world_direction(obj))

    return entry

_VOLUME_TOKENS = ("volume", "trigger", "dimmer", "occlusion", "_occ", "blocker", "nointer")


def _is_volume_mesh(obj) -> bool:
    name = str(getattr(obj, "name", "") or "").lower()
    data_name = str(getattr(getattr(obj, "data", None), "name", "") or "").lower()
    depot = str(mesh_depot_path(obj) or _custom_prop(obj, "repo_path", "") or "").lower()
    return any(tok in name or tok in data_name or tok in depot for tok in _VOLUME_TOKENS)


def _placement_mesh_children(wrapper):
    if getattr(wrapper, "type", "") == "MESH":
        return [wrapper] if mesh_depot_path(wrapper) else []
    return [
        child
        for child in getattr(wrapper, "children_recursive", []) or []
        if getattr(child, "type", "") == "MESH" and mesh_depot_path(child)
    ]


def _placement_lod0_meshes(wrapper):
    children = _placement_mesh_children(wrapper)
    if not children:
        return []
    min_lod = min(_object_lod_index(child) for child in children)
    return [child for child in children if _object_lod_index(child) == min_lod]


def collect_placements(objects, warnings: list[str]) -> dict[str, Any]:
    expanded = _expand_selection(objects)
    assets: dict[str, dict[str, Any]] = {}
    layers: dict[str, dict[str, list]] = {}

    def _asset_for(depot: str, wrapper, children) -> Optional[str]:
        asset_rel = depot_asset_rel(depot)
        if not asset_rel:
            return None
        if asset_rel not in assets:
            assets[asset_rel] = {"depot": depot, "wrapper": wrapper, "children": list(children or [])}
        return asset_rel

    def _layer(layer_id: str) -> dict[str, list]:
        return layers.setdefault(layer_id, {"actors": [], "instancers": [], "lights": []})

    for obj in expanded:
        if not bool(_custom_prop(obj, "_is_sector_instancer", False)):
            continue
        kind = str(_custom_prop(obj, "witcher_layer_visibility_kind", "") or "").strip().lower()
        if kind == "collision" or _custom_prop(obj, "witcher_layer_engine_visible", True) is False:
            continue
        if _is_volume_mesh(obj):
            warnings.append(f"{obj.name}: looks like an editor volume; skipped.")
            continue
        depot = str(_custom_prop(obj, "repo_path", "") or "").strip()
        if not depot:
            continue
        src_obj = _sector_source_object(obj)
        if src_obj is None:
            warnings.append(f"{obj.name}: sector instancer source mesh missing; skipped.")
            continue
        if _is_volume_mesh(src_obj):
            warnings.append(f"{obj.name}: sector instancer source looks like an editor volume; skipped.")
            continue
        asset_rel = _asset_for(depot, None, [src_obj])
        if asset_rel is None:
            continue
        _layer(_layer_id_for_object(obj))["instancers"].append({
            "name": str(getattr(obj, "name", "") or "Instancer"),
            "asset_rel": asset_rel,
            "matrices": _sector_point_world_matrices(obj),
        })

    wrappers: dict[int, Any] = {}
    for obj in expanded:
        if bool(_custom_prop(obj, "_is_sector_instancer", False)):
            continue
        obj_type = getattr(obj, "type", "")
        if obj_type == "EMPTY" and _custom_prop(obj, "repo_path"):
            wrappers.setdefault(_object_key(obj), obj)
        elif obj_type == "MESH" and mesh_depot_path(obj):
            parent = getattr(obj, "parent", None)
            if parent is not None and getattr(parent, "type", "") == "EMPTY" and _custom_prop(parent, "repo_path"):
                wrappers.setdefault(_object_key(parent), parent)
            else:
                wrappers.setdefault(_object_key(obj), obj)

    for wrapper in wrappers.values():
        children = _placement_lod0_meshes(wrapper)
        if not children:
            continue
        if _is_volume_mesh(wrapper) or any(_is_volume_mesh(child) for child in children):
            warnings.append(f"{wrapper.name}: looks like an editor volume; skipped.")
            continue
        depot = mesh_depot_path(children[0])
        if not depot:
            continue
        asset_rel = _asset_for(depot, wrapper, children)
        if asset_rel is None:
            continue
        _layer(_layer_id_for_object(wrapper))["actors"].append({
            "name": str(getattr(wrapper, "name", "") or "Placement"),
            "asset_rel": asset_rel,
            "matrix": wrapper.matrix_world.copy(),
        })

    # 3) imported point/spot lights from layers and entity components.
    for obj in expanded:
        if getattr(obj, "type", "") != "LIGHT":
            continue
        if _custom_prop(obj, "witcher_layer_engine_visible", True) is False:
            continue
        entry = _light_entry_from_object(obj)
        if entry is None:
            continue
        _layer(_layer_id_for_object(obj))["lights"].append(entry)

    return {"assets": assets, "layers": layers}


# ---- mesh export (identity local space) -----------------------------------

@contextmanager
def _object_at_identity(obj):
    """Temporarily neutralise an object's world transform so its (and its
    children's) FBX exports in clean local space -- the actor transform carries
    the world placement. Forces a depsgraph refresh so parented children update."""
    import bpy
    from mathutils import Matrix

    saved = obj.matrix_world.copy()

    def _refresh():
        try:
            bpy.context.view_layer.update()
        except Exception:
            pass

    try:
        obj.matrix_world = Matrix.Identity(4)
        _refresh()
        yield
    finally:
        obj.matrix_world = saved
        _refresh()


class _PlacementExportProfile:
    def __init__(self):
        self.started = time.perf_counter()
        self.events: list[dict[str, Any]] = []
        self.counts: defaultdict[str, int] = defaultdict(int)
        self.totals: defaultdict[str, float] = defaultdict(float)

    @contextmanager
    def section(self, name: str, label: str = ""):
        started = time.perf_counter()
        try:
            yield
        finally:
            seconds = time.perf_counter() - started
            self.events.append({"name": name, "label": label, "seconds": seconds})
            self.totals[name] += seconds

    def count(self, name: str, amount: int = 1) -> None:
        self.counts[name] += int(amount)

    def total_seconds(self) -> float:
        return time.perf_counter() - self.started


class _PlacementExportProgress:
    def __init__(self, context, settings, total_steps: int):
        self.context = context
        self.settings = settings
        self.total_steps = max(1, int(total_steps))
        self.current = 0
        self.wm = getattr(context, "window_manager", None)
        if self.wm is not None:
            try:
                self.wm.progress_begin(0, self.total_steps)
            except Exception:
                self.wm = None

    def step(self, label: str, amount: int = 1) -> None:
        self.current = min(self.total_steps, self.current + max(1, int(amount)))
        try:
            self.settings.last_status = f"{label} ({self.current}/{self.total_steps})"
        except Exception:
            pass
        if self.wm is not None:
            try:
                self.wm.progress_update(self.current)
            except Exception:
                pass

    def end(self) -> None:
        if self.wm is not None:
            try:
                self.wm.progress_end()
            except Exception:
                pass


def _write_profile_log(
    profile: _PlacementExportProfile,
    bundle_root: str,
    settings_summary: dict[str, Any],
    warnings: list[str],
) -> Optional[str]:
    path = os.path.join(bundle_root, "witcher_unreal_placements_profile.log")
    try:
        totals = sorted(profile.totals.items(), key=lambda item: item[1], reverse=True)
        slowest = sorted(profile.events, key=lambda event: event["seconds"], reverse=True)[:40]
        lines = [
            "Witcher Unreal placement export profile",
            f"Total: {profile.total_seconds():.3f}s",
            "",
            "Settings:",
        ]
        for key, value in settings_summary.items():
            lines.append(f"- {key}: {value}")
        lines += ["", "Counts:"]
        for key in sorted(profile.counts):
            lines.append(f"- {key}: {profile.counts[key]}")
        lines += ["", "Totals:"]
        for name, seconds in totals:
            lines.append(f"- {name}: {seconds:.3f}s")
        lines += ["", "Slowest operations:"]
        if slowest:
            for event in slowest:
                label = f" {event['label']}" if event.get("label") else ""
                lines.append(f"- {event['name']}{label}: {event['seconds']:.3f}s")
        else:
            lines.append("- none")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        return path
    except Exception as exc:
        warnings.append(f"Could not write placement profile log: {exc}")
        log.warning("Could not write placement profile log", exc_info=True)
        return None


def _collision_asset_rel(asset_rel: str) -> str:
    return f"{asset_rel}_collision" if asset_rel else ""


def _ensure_collision_export_uv(objects) -> None:
    """FBX tangent export expects a UV layer. Collision objects only need one
    dummy layer because they render hidden and exist for collision queries."""
    for obj in objects or []:
        mesh = getattr(obj, "data", None)
        if mesh is None:
            continue
        try:
            if not mesh.uv_layers:
                mesh.uv_layers.new(name="CollisionUV")
        except Exception:
            pass


def _unique_collision_buffer_path(bundle_root: str, asset_rel: str, used_stems: dict[str, str]) -> str:
    return _unique_bundle_file(bundle_root, asset_rel, used_stems, "Meshes", ".w3buf")


def _material_name_for_collision_polygon(mesh, material_index: int) -> str:
    try:
        if 0 <= material_index < len(mesh.materials):
            material = mesh.materials[material_index]
            name = getattr(material, "name", "") if material else ""
            if name:
                return str(name)
    except Exception:
        pass
    return "collision"


def _collision_objects_to_mesh_buffer(objects, mesh_name: str, depot_path: str):
    """Bake RED collision objects into static placement buffers."""
    from .mesh_buffer import MeshBuffer, SubmeshBuffer

    mesh = MeshBuffer(mesh_name, depot_path=depot_path, is_skinned=False)
    submesh_data: dict[str, dict[str, Any]] = {}
    depsgraph = None
    try:
        import bpy

        depsgraph = bpy.context.evaluated_depsgraph_get()
    except Exception:
        depsgraph = None

    for obj in objects or []:
        if getattr(obj, "type", "") != "MESH":
            continue
        eval_obj = None
        eval_mesh = None
        try:
            eval_obj = obj.evaluated_get(depsgraph) if depsgraph is not None else obj
            eval_mesh = eval_obj.to_mesh() if hasattr(eval_obj, "to_mesh") else getattr(obj, "data", None)
            if eval_mesh is None:
                continue
            eval_mesh.calc_loop_triangles()
            world = getattr(eval_obj, "matrix_world", None) or getattr(obj, "matrix_world", None)
            if world is None:
                continue

            for tri in getattr(eval_mesh, "loop_triangles", []) or []:
                poly = eval_mesh.polygons[tri.polygon_index]
                material_name = _material_name_for_collision_polygon(eval_mesh, int(poly.material_index))
                data = submesh_data.get(material_name)
                if data is None:
                    data = {
                        "mat_id": len(submesh_data),
                        "positions": [],
                        "indices": [],
                    }
                    submesh_data[material_name] = data

                positions = data["positions"]
                indices = data["indices"]
                base = len(positions) // 3
                valid_triangle = True
                for vertex_index in tri.vertices:
                    co = world @ eval_mesh.vertices[vertex_index].co
                    xyz = (float(co.x), float(co.y), float(co.z))
                    if not all(math.isfinite(value) for value in xyz):
                        valid_triangle = False
                        break
                    positions.extend(xyz)
                if not valid_triangle:
                    continue
                indices.extend((base, base + 1, base + 2))
        finally:
            if eval_obj is not None and eval_mesh is not None and hasattr(eval_obj, "to_mesh_clear"):
                try:
                    eval_obj.to_mesh_clear()
                except Exception:
                    pass

    for material_name, data in submesh_data.items():
        positions = data["positions"]
        indices = data["indices"]
        if len(positions) < 9 or len(indices) < 3:
            continue
        sm = SubmeshBuffer(lod=0, mat_id=data["mat_id"], material=material_name)
        sm.set_positions(positions)
        sm.set_indices(indices)
        mesh.submeshes.append(sm)
    return mesh


_EMBEDDED_COLLISION_BUILDERS = {
    "CCollisionShapeConvex": ("CCollisionShapeConvex", "createCol"),
    "CCollisionShapeTriMesh": ("CCollisionShapeTriMesh", "createTri"),
    "CCollisionShapeBox": ("CCollisionShapeBox", "createBox"),
    "CCollisionShapeSphere": ("CCollisionShapeSphere", "createSphere"),
    "CCollisionShapeCapsule": ("CCollisionShapeCapsule", "createCapsule"),
}


def _create_embedded_collision_objects(
    depot_path: str,
    warnings: list[str],
    profile: Optional[_PlacementExportProfile] = None,
):
    """Read collision from an UNCOOKED .w2mesh's embedded ``CCollisionMesh`` chunk
    (the REDkit custom format), mirroring the importer's path
    [import_mesh.py:466-535]. Returns ``(found, objects)``: ``found`` is True when
    the mesh carried a ``CCollisionMesh`` chunk, in which case the caller must NOT
    fall back to the cooked ``.nxs`` cache. Relies on the active
    ``redkit_repo_context`` so ``repo_file`` resolves the uncooked mesh."""
    try:
        from ..CR2W.common_blender import repo_file, win_safe_path
        from ..CR2W.dc_mesh import load_bin_mesh
        from ..CR2W import dc_entity
        from ..importers import import_nxs
    except Exception as exc:
        warnings.append(f"{depot_path}: embedded collision importer unavailable ({exc}).")
        return False, []

    source = repo_file(normalize_depot_path(depot_path))
    if not source or not os.path.isfile(win_safe_path(source)):
        return False, []

    try:
        section = profile.section("collision_resolve_embedded", depot_path) if profile else nullcontext()
        with section:
            *_, meshFile = load_bin_mesh(source, keep_lod_meshes=False)
    except Exception as exc:
        warnings.append(f"{depot_path}: could not parse mesh for embedded collision ({exc}).")
        return False, []

    chunks = getattr(getattr(meshFile, "CHUNKS", None), "CHUNKS", None) or []
    collision_chunk = next((c for c in chunks if getattr(c, "name", "") == "CCollisionMesh"), None)
    if collision_chunk is None:
        return False, []  # cooked mesh -> caller uses the .nxs cache

    mesh_stem = os.path.splitext(os.path.basename(source))[0] or "collision"
    objects = []
    try:
        section = profile.section("collision_create_embedded", depot_path) if profile else nullcontext()
        with section:
            shapes = collision_chunk.GetVariableByName("shapes")
            for shape_id in (getattr(shapes, "value", None) or []):
                shape = chunks[shape_id - 1]
                entry = _EMBEDDED_COLLISION_BUILDERS.get(getattr(shape, "Type", ""))
                if entry is None:
                    continue
                wrap = getattr(dc_entity, entry[0])
                build = getattr(import_nxs, entry[1])
                try:
                    obj = build(wrap(shape), mesh_stem)
                except Exception as exc:
                    warnings.append(f"{depot_path}: {getattr(shape, 'Type', 'shape')} collision failed ({exc}).")
                    continue
                if obj is not None:
                    objects.append(obj)
    except Exception as exc:
        warnings.append(f"{depot_path}: could not read embedded collision ({exc}).")
    return True, objects


def _create_collision_objects(
    depot_path: str,
    warnings: list[str],
    profile: Optional[_PlacementExportProfile] = None,
):
    # Uncooked REDkit mesh: build from the embedded CCollisionMesh shapes.
    found_embedded, embedded_objects = _create_embedded_collision_objects(depot_path, warnings, profile=profile)
    if found_embedded:
        if not embedded_objects and profile:
            profile.count("collision_embedded_empty")
        return embedded_objects

    # Cooked mesh: no embedded chunk -> the collision cache .nxs.
    try:
        from ..CR2W.common_blender import get_collision_for_mesh_with_poses
        from ..importers.import_nxs import create_from_nxs
    except Exception as exc:
        warnings.append(f"{depot_path}: collision importer unavailable ({exc}); skipped collision.")
        return []

    try:
        section = profile.section("collision_resolve", depot_path) if profile else nullcontext()
        with section:
            collision_path, shape_items = get_collision_for_mesh_with_poses(depot_path)
    except Exception as exc:
        warnings.append(f"{depot_path}: could not resolve collision data ({exc}); skipped collision.")
        return []
    if not collision_path:
        if profile:
            profile.count("collision_missing")
        return []

    try:
        section = profile.section("collision_create_objects", depot_path) if profile else nullcontext()
        with section:
            objects = list(create_from_nxs(collision_path, shape_items=shape_items) or [])
    except Exception as exc:
        warnings.append(f"{depot_path}: could not import collision '{collision_path}' ({exc}); skipped collision.")
        return []

    return objects


def _export_collision_mesh_for_asset(
    context,
    asset_rel: str,
    depot_path: str,
    bundle_root: str,
    warnings: list[str],
    used_fbx_stems: dict[str, str],
    *,
    reuse_existing_fbx: bool = True,
    profile: Optional[_PlacementExportProfile] = None,
) -> Optional[dict[str, Any]]:
    collision_asset = _collision_asset_rel(asset_rel)
    buffer_path = _unique_collision_buffer_path(bundle_root, collision_asset, used_fbx_stems)
    if reuse_existing_fbx and os.path.exists(buffer_path) and os.path.getsize(buffer_path) > 0:
        if profile:
            profile.count("collision_buffer_reused")
        return {
            "name": collision_asset.rsplit("/", 1)[-1],
            "buffer": relpath_for_manifest(buffer_path, bundle_root),
            "asset_path": collision_asset,
            "kind": "static",
            "collision": True,
            "slots": [],
        }

    created_objects = _create_collision_objects(depot_path, warnings, profile=profile)
    collision_objects = [obj for obj in created_objects if getattr(obj, "type", "") == "MESH"]
    if not collision_objects:
        for obj in created_objects:
            _remove_object(obj)
        return None

    try:
        section = profile.section("collision_buffer_write", asset_rel) if profile else nullcontext()
        with section:
            from .mesh_buffer import write_mesh_buffer

            mesh = _collision_objects_to_mesh_buffer(collision_objects, collision_asset.rsplit("/", 1)[-1], depot_path)
            if not mesh.submeshes:
                return None
            write_mesh_buffer(buffer_path, mesh)
        if profile:
            profile.count("collision_buffer_exported")
    finally:
        for obj in created_objects:
            _remove_object(obj)

    return {
        "name": collision_asset.rsplit("/", 1)[-1],
        "buffer": relpath_for_manifest(buffer_path, bundle_root),
        "asset_path": collision_asset,
        "kind": "static",
        "collision": True,
        "slots": [],
    }


def _export_static_mesh_group(
    context,
    group: dict[str, Any],
    bundle_root: str,
    chain: ChainBuilder,
    warnings: list[str],
    used_fbx_stems: dict[str, str],
    *,
    reuse_existing_fbx: bool = True,
    profile: Optional[_PlacementExportProfile] = None,
) -> dict[str, Any]:
    asset_rel = group["asset_path"]
    objects = list(group.get("objects") or [])
    asset_dir = depot_asset_dir(asset_rel)
    mesh_name = asset_rel.rsplit("/", 1)[-1]
    fbx_path = _unique_fbx_path(bundle_root, asset_rel, used_fbx_stems)

    if reuse_existing_fbx and os.path.exists(fbx_path):
        if profile:
            profile.count("visual_fbx_reused")
    else:
        section = profile.section("visual_fbx_export", asset_rel) if profile else nullcontext()
        with section:
            export_fbx(context, objects, fbx_path, object_types={"MESH"})
        if profile:
            profile.count("visual_fbx_exported")

    slots: list[dict[str, Any]] = []
    seen_slots: set[tuple[int, str]] = set()
    section = profile.section("material_scan", asset_rel) if profile else nullcontext()
    with section:
        for obj in objects:
            for mat_info in collect_material_infos(obj, warnings):
                slot_index = int(mat_info.get("material_slot_index", len(slots)))
                slot_name = str(mat_info.get("name", ""))
                key = (slot_index, slot_name)
                if key in seen_slots:
                    continue
                seen_slots.add(key)
                material_id = chain.add_slot_material(mat_info, asset_dir)
                slots.append({
                    "slot_index": slot_index,
                    "slot_name": slot_name,
                    "material_id": material_id,
                })

    return {
        "name": mesh_name,
        "fbx": relpath_for_manifest(fbx_path, bundle_root),
        "asset_path": asset_rel,
        "kind": "static",
        "slots": slots,
    }


def build_unreal_placements_bundle(context, settings) -> dict[str, Any]:
    selected_objects = list(getattr(context, "selected_objects", []) or [])
    if not selected_objects:
        raise ValueError("Select at least one imported layer collection or placed object first.")

    warnings: list[str] = []
    collected = collect_placements(selected_objects, warnings)
    assets = collected["assets"]
    collected_layers = collected["layers"]
    has_lights = any(data.get("lights") for data in collected_layers.values())
    if not assets and not has_lights:
        raise ValueError(
            "No placed meshes or lights found in the selection. Import layers with 'Load "
            "Layers Around Camera', then select the layer collection(s) to send."
        )

    source_game = _object_source_game(selected_objects[0]) or "w3"
    asset_name = safe_asset_name(getattr(settings, "asset_name", "") or "WitcherPlacements")
    content_root = _resolve_content_root_setting(getattr(settings, "content_root", ""), source_game)

    export_root = str(getattr(settings, "export_folder", "") or default_export_folder())
    bundle_root = os.path.join(export_root, asset_name)
    os.makedirs(bundle_root, exist_ok=True)

    export_collision = bool(getattr(settings, "placement_export_collision", True))
    overwrite = overwrite_policy_from_settings(settings)
    reuse_existing_fbx = not overwrite["meshes"]
    write_profile_log = bool(getattr(settings, "placement_write_profile_log", True))

    profile = _PlacementExportProfile()
    profile.count("unique_assets", len(assets))
    profile.count("layers", len(collected_layers))
    for data in collected_layers.values():
        profile.count("actors", len(data.get("actors", []) or []))
        profile.count("instancers", len(data.get("instancers", []) or []))
        profile.count("lights", len(data.get("lights", []) or []))
        profile.count(
            "instances",
            sum(len(inst.get("matrices", []) or []) for inst in data.get("instancers", []) or []),
        )

    progress_steps = 2 + len(assets) * (2 if export_collision else 1) + len(collected_layers)
    progress = _PlacementExportProgress(context, settings, progress_steps)
    profile_path = None
    try:
        registry = TextureRegistry(bundle_root, parallel=True)
        chain = ChainBuilder(registry.register)

        mesh_entries: list[dict[str, Any]] = []
        used_fbx_stems: dict[str, str] = {}
        asset_paths: dict[str, str] = {}
        collision_asset_paths: dict[str, str] = {}
        for index, (asset_rel, entry) in enumerate(assets.items(), start=1):
            children = list(entry.get("children") or [])
            if not children:
                warnings.append(f"{asset_rel}: no source mesh to export; skipped.")
                progress.step(f"Skipped mesh {index}/{len(assets)}")
                continue
            zero_obj = entry["wrapper"] or children[0]
            mesh_group = {"asset_path": asset_rel, "objects": children}
            with _object_at_identity(zero_obj):
                mesh_entry = _export_static_mesh_group(
                    context,
                    mesh_group,
                    bundle_root,
                    chain,
                    warnings,
                    used_fbx_stems,
                    reuse_existing_fbx=reuse_existing_fbx,
                    profile=profile,
                )
            mesh_entries.append(mesh_entry)
            asset_paths[asset_rel] = mesh_entry["asset_path"]
            progress.step(f"Mesh {index}/{len(assets)}")

            if export_collision:
                collision_entry = _export_collision_mesh_for_asset(
                    context,
                    asset_rel,
                    str(entry.get("depot") or ""),
                    bundle_root,
                    warnings,
                    used_fbx_stems,
                    reuse_existing_fbx=reuse_existing_fbx,
                    profile=profile,
                )
                if collision_entry:
                    mesh_entries.append(collision_entry)
                    collision_asset_paths[asset_rel] = collision_entry["asset_path"]
                progress.step(f"Collision {index}/{len(assets)}")

        layer_groups = []
        with profile.section("placement_manifest_build", "layers"):
            for layer_id, data in collected_layers.items():
                actors = []
                for actor in data["actors"]:
                    asset_path = asset_paths.get(actor["asset_rel"])
                    if not asset_path:
                        continue
                    actor_entry = {
                        "name": actor["name"],
                        "asset_path": asset_path,
                        "transform": terrain_unreal.w3_matrix_to_unreal(actor["matrix"]),
                    }
                    collision_asset_path = collision_asset_paths.get(actor["asset_rel"])
                    if collision_asset_path:
                        actor_entry["collision_asset_path"] = collision_asset_path
                    actors.append(actor_entry)
                instancers = []
                for inst in data["instancers"]:
                    asset_path = asset_paths.get(inst["asset_rel"])
                    if not asset_path or not inst["matrices"]:
                        continue
                    instancer_entry = {
                        "name": inst["name"],
                        "asset_path": asset_path,
                        "instances": [terrain_unreal.w3_matrix_to_unreal(m) for m in inst["matrices"]],
                    }
                    collision_asset_path = collision_asset_paths.get(inst["asset_rel"])
                    if collision_asset_path:
                        instancer_entry["collision_asset_path"] = collision_asset_path
                    instancers.append(instancer_entry)
                lights = []
                for light in data.get("lights", []) or []:
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
                    lights.append(light_entry)

                if actors or instancers or lights:
                    label, folder = _layer_label_and_folder(layer_id)
                    layer_groups.append({
                        "layer_id": layer_id,
                        "label": label,
                        "folder": folder,
                        "actors": actors,
                        "instancers": instancers,
                        "lights": lights,
                    })
                progress.step(f"Layer manifest {len(layer_groups)}/{len(collected_layers)}")

        placements = {"layers": layer_groups} if layer_groups else None

        with profile.section("manifest_write", "witcher_unreal_export.json"):
            manifest = build_manifest(
                asset_name=asset_name,
                bundle_root=bundle_root,
                source_game=source_game,
                content_root=content_root,
                overwrite=overwrite,
                meshes=mesh_entries,
                masters=chain.ordered_masters(),
                materials=chain.ordered_materials(),
                textures=registry.manifest_entries(),
                placements=placements,
                warnings=warnings + chain.warnings + registry.warnings,
            )

            manifest_path = os.path.join(bundle_root, "witcher_unreal_export.json")
            with open(manifest_path, "w", encoding="utf-8") as handle:
                json.dump(manifest, handle, indent=2)
        progress.step("Manifest")

        if write_profile_log:
            profile_path = _write_profile_log(
                profile,
                bundle_root,
                {
                    "collision": export_collision,
                    "reuse_existing_fbx": reuse_existing_fbx,
                    "write_profile_log": write_profile_log,
                },
                warnings,
            )
        progress.step("Profile")

        return {
            "asset_name": asset_name,
            "bundle_root": bundle_root,
            "manifest_path": manifest_path,
            "profile_path": profile_path or "",
            "profile": {
                "counts": dict(profile.counts),
                "totals": dict(profile.totals),
                "total_seconds": profile.total_seconds(),
            },
            "manifest": manifest,
        }
    finally:
        progress.end()
