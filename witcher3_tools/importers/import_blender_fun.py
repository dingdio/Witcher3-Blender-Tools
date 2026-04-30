import logging
from pathlib import Path
import re
from ..CR2W.CR2W_helpers import Enums
from ..CR2W.CR2W_types import Entity_Type_List
import bpy
import os
from ..importers.import_helpers import MatrixToArray, checkLevel, meshPath, set_blender_object_transform, _transform_real
from mathutils import Matrix, Euler
from math import radians
import time

log = logging.getLogger(__name__)

_MESH_IMPORT_TIMING_ENABLED = True
_MESH_IMPORT_WARN_THRESHOLD = 0.25
_LAYER_IMPORT_PROFILE_ENABLED = True
_LAYER_IMPORT_PROFILE_WARN_THRESHOLD = 0.25

from .. import fbx_util
from .. import get_uncook_path
from .. import get_W3_FOLIAGE_PATH
from .. import get_fbx_uncook_path
from .. import get_use_fbx_repo
from .. import get_do_import_redcloth
from ..importers import import_mesh, import_isolation
from ..external_addon_tools import get_srt_addon_status

from bpy_extras.wm_utils.progress_report import (
    ProgressReport,
    ProgressReportSubstep,
)


class lightObject:
    def __init__(self, meshName = "Light Item",
                    translation = False,
                    matrix = False,
                    transform = False,
                    block = False,
                    BlockDataObjectType = Enums.BlockDataObjectType.Mesh):
        self.name = meshName
        self.meshName = meshName
        self.translation = translation
        self.matrix = matrix
        self.transform = transform
        self.type = "Light"
        self.block = block
        self.BlockDataObjectType = BlockDataObjectType

from ..CR2W.common_blender import repo_file
# def repo_file(filepath: str):
#     if filepath.endswith('.fbx'):
#         return os.path.join(bpy.context.preferences.addons['io_import_w2l'].preferences.fbx_uncook_path, filepath)
#     else:
#         return os.path.join(bpy.context.preferences.addons['io_import_w2l'].preferences.uncook_path, filepath)

def _log_layer_import_start(level_file):
    log.info("Importing layer: %s", level_file)


def _log_layer_import_complete(level_file, progress_count, errors):
    if errors:
        log.error("Layer import finished with %d error(s): %s", len(errors), level_file)
        for error in errors:
            log.error(error)
        return
    if progress_count:
        log.info("Finished layer: %s", level_file)
    else:
        log.info("Layer contained no importable items: %s", level_file)


def _layer_load_mode_signature(dev_empty_only=False):
    return f"dev_empty={int(bool(dev_empty_only))}"


def _set_layer_import_state(collection, level_file, state, progress_count=0, error_count=0, filtered_count=0, *, nearby_filter=None, mode_signature=None):
    if collection is None or not hasattr(collection, "__setitem__"):
        return
    try:
        collection["witcher_layer_import_state"] = str(state or "").strip().lower()
        collection["witcher_layer_import_level"] = str(level_file or "")
        collection["witcher_layer_import_count"] = int(progress_count or 0)
        collection["witcher_layer_import_errors"] = int(error_count or 0)
        collection["witcher_layer_import_filtered"] = int(filtered_count or 0)
    except Exception:
        pass
    if nearby_filter is not None:
        try:
            cam = nearby_filter.get("camera_position") or (0.0, 0.0, 0.0)
            collection["witcher_layer_load_camera_x"] = float(cam[0])
            collection["witcher_layer_load_camera_y"] = float(cam[1])
            collection["witcher_layer_load_camera_z"] = float(cam[2])
            collection["witcher_layer_load_radius"] = float(nearby_filter.get("radius", 0.0) or 0.0)
            if mode_signature is not None:
                collection["witcher_layer_load_mode"] = str(mode_signature)
        except Exception:
            pass


class LayerImportCancelled(RuntimeError):
    pass


def _layer_import_cancel_requested(kwargs):
    cancel_check = kwargs.get("_cancel_check")
    if not callable(cancel_check):
        return False
    try:
        return bool(cancel_check())
    except Exception:
        return False


def _raise_if_layer_import_cancelled(kwargs):
    if _layer_import_cancel_requested(kwargs):
        raise LayerImportCancelled("Cancelled by user")


def _log_mesh_import_timing_warning(message, *args):
    if not _MESH_IMPORT_TIMING_ENABLED:
        return
    log.info("[mesh-import-profile] " + str(message), *args)


def _log_layer_import_profile_warning(message, *args):
    if not _LAYER_IMPORT_PROFILE_ENABLED:
        return
    log.info("[layer-import-profile] " + str(message), *args)


def _new_layer_import_profile():
    return {
        "mesh_calls": 0,
        "mesh_total_seconds": 0.0,
        "mesh_import_seconds": 0.0,
        "mesh_finalize_seconds": 0.0,
        "mesh_transform_seconds": 0.0,
        "backend_counts": {},
        "reused_meshes": 0,
        "fresh_meshes": 0,
        "unique_mesh_paths": set(),
        "slowest_mesh": {"path": "", "seconds": 0.0, "backend": ""},
        "entity_calls": 0,
        "entity_imported": 0,
        "slowest_entity": {"name": "", "seconds": 0.0},
    }


def _get_layer_import_profile(kwargs):
    profile = kwargs.get("_layer_import_profile")
    if profile is None:
        profile = _new_layer_import_profile()
        kwargs["_layer_import_profile"] = profile
    return profile


def _record_layer_mesh_profile(
    kwargs,
    mesh,
    backend,
    reused_existing,
    total_seconds,
    import_seconds,
    finalize_seconds,
    transform_seconds,
):
    profile = _get_layer_import_profile(kwargs)
    profile["mesh_calls"] += 1
    profile["mesh_total_seconds"] += float(total_seconds or 0.0)
    profile["mesh_import_seconds"] += float(import_seconds or 0.0)
    profile["mesh_finalize_seconds"] += float(finalize_seconds or 0.0)
    profile["mesh_transform_seconds"] += float(transform_seconds or 0.0)
    mesh_name = str(getattr(mesh, "meshName", "") or "")
    if mesh_name:
        profile["unique_mesh_paths"].add(mesh_name)
    if reused_existing:
        profile["reused_meshes"] += 1
    else:
        profile["fresh_meshes"] += 1
    backend_entry = profile["backend_counts"].setdefault(
        str(backend or "unknown"),
        {"count": 0, "seconds": 0.0},
    )
    backend_entry["count"] += 1
    backend_entry["seconds"] += float(total_seconds or 0.0)
    if total_seconds >= profile["slowest_mesh"]["seconds"]:
        profile["slowest_mesh"] = {
            "path": mesh_name,
            "seconds": float(total_seconds or 0.0),
            "backend": str(backend or "unknown"),
        }


def _record_layer_entity_profile(kwargs, entity_name, total_seconds, imported_any):
    profile = _get_layer_import_profile(kwargs)
    profile["entity_calls"] += 1
    if imported_any:
        profile["entity_imported"] += 1
    if total_seconds >= profile["slowest_entity"]["seconds"]:
        profile["slowest_entity"] = {
            "name": str(entity_name or ""),
            "seconds": float(total_seconds or 0.0),
        }


def _log_layer_import_profile_summary(level_file, kwargs):
    profile = kwargs.get("_layer_import_profile")
    if not profile:
        return
    mesh_total_seconds = float(profile.get("mesh_total_seconds", 0.0) or 0.0)
    entity_calls = int(profile.get("entity_calls", 0) or 0)
    if mesh_total_seconds < _LAYER_IMPORT_PROFILE_WARN_THRESHOLD and entity_calls <= 0:
        return

    backend_bits = []
    for backend_name, backend_entry in sorted(
        profile.get("backend_counts", {}).items(),
        key=lambda item: item[1].get("seconds", 0.0),
        reverse=True,
    ):
        backend_bits.append(
            f"{backend_name} {int(backend_entry.get('count', 0) or 0)}/{float(backend_entry.get('seconds', 0.0) or 0.0):.3f}s"
        )
    backend_summary = ", ".join(backend_bits) if backend_bits else "none"

    slowest_mesh = profile.get("slowest_mesh", {}) or {}
    slowest_entity = profile.get("slowest_entity", {}) or {}
    _log_layer_import_profile_warning(
        "%s meshes %d total %.3fs (import %.3fs, finalize %.3fs, transform %.3fs, fresh %d, reused %d, unique %d, backends %s, slowest mesh %s %.3fs %s, entities %d/%d imported, slowest entity %s %.3fs)",
        level_file,
        int(profile.get("mesh_calls", 0) or 0),
        mesh_total_seconds,
        float(profile.get("mesh_import_seconds", 0.0) or 0.0),
        float(profile.get("mesh_finalize_seconds", 0.0) or 0.0),
        float(profile.get("mesh_transform_seconds", 0.0) or 0.0),
        int(profile.get("fresh_meshes", 0) or 0),
        int(profile.get("reused_meshes", 0) or 0),
        len(profile.get("unique_mesh_paths", set()) or ()),
        backend_summary,
        slowest_mesh.get("path", "") or "<none>",
        float(slowest_mesh.get("seconds", 0.0) or 0.0),
        slowest_mesh.get("backend", "") or "",
        int(profile.get("entity_imported", 0) or 0),
        entity_calls,
        slowest_entity.get("name", "") or "<none>",
        float(slowest_entity.get("seconds", 0.0) or 0.0),
    )


_LAYER_IMPORT_OWNER_PROP = "witcher_layer_owner"
_LAYER_IMPORT_GENERATION_PROP = "witcher_layer_generation"
_LAYER_IMPORT_PLAN_ITEM_PROP = "witcher_layer_plan_item_id"
_LAYER_IMPORT_PLAN_MODE_PROP = "witcher_layer_plan_mode"
_CACHED_REDCLOTH_ITEM_KINDS = frozenset({"cloth"})
_CACHED_FULL_MESH_ITEM_KINDS = frozenset({
    "mesh",
    "component_mesh",
    "foliage",
    "grass",
    "collision",
    "rigid",
    "rigid_body",
})
_CACHED_FULL_LIGHT_ITEM_KINDS = frozenset({
    "point_light",
    "spot_light",
    "component_point_light",
    "component_spot_light",
})
_CACHED_SECTOR_INSTANCER_KINDS = frozenset({"sector_instancer"})
_CACHED_FULL_ITEM_KINDS = (
    _CACHED_FULL_MESH_ITEM_KINDS
    | _CACHED_REDCLOTH_ITEM_KINDS
    | _CACHED_FULL_LIGHT_ITEM_KINDS
    | _CACHED_SECTOR_INSTANCER_KINDS
)
_CACHED_FULL_PARENT_ITEM_KINDS = frozenset({"group", "entity"})
_SECTOR_FLAG_MESH_VISIBLE = 1 << 2
_SECTOR_FLAG_MESH_PART_OF_ENTITY_PROXY = 1 << 10
_SECTOR_FLAG_MESH_ROOT_ENTITY_PROXY = 1 << 11


def _sector_flags_value(flags):
    try:
        return int(flags)
    except Exception:
        return None


def _sector_mesh_visible_from_flags(flags, default=True):
    value = _sector_flags_value(flags)
    if value is None:
        return bool(default)
    return bool(value & _SECTOR_FLAG_MESH_VISIBLE)


def _sector_visibility_key_from_flags(flags, default=True):
    return "visible" if _sector_mesh_visible_from_flags(flags, default=default) else "hidden"


def _drawable_flags_visible_from_value(flags, default=True):
    if flags is None:
        return bool(default)
    if hasattr(flags, "strings"):
        return _drawable_flags_visible_from_value(getattr(flags, "strings", None), default=default)
    if hasattr(flags, "Value"):
        return _drawable_flags_visible_from_value(getattr(flags, "Value", None), default=default)
    if isinstance(flags, (list, tuple, set)):
        names = {str(value or "").strip() for value in flags}
        return "DF_IsVisible" in names or "IsVisible" in names
    if isinstance(flags, str):
        parts = {part for part in re.split(r"[\s,|;]+", flags.strip()) if part}
        return "DF_IsVisible" in parts or "IsVisible" in parts
    try:
        return bool(int(flags) & 1)
    except Exception:
        return bool(default)


def _component_drawable_flags(component):
    prop = None
    try:
        prop = component.GetVariableByName("drawableFlags")
    except Exception:
        prop = None
    if prop is None:
        try:
            prop = getattr(component, "drawableFlags", None)
        except Exception:
            prop = None
    if prop is None:
        return None
    if hasattr(prop, "strings"):
        return list(getattr(prop, "strings", []) or [])
    if hasattr(prop, "Value"):
        return getattr(prop, "Value", None)
    return prop


def _drawable_flags_display_value(flags):
    if flags is None:
        return ""
    if hasattr(flags, "strings"):
        return _drawable_flags_display_value(getattr(flags, "strings", None))
    if hasattr(flags, "Value"):
        return _drawable_flags_display_value(getattr(flags, "Value", None))
    if isinstance(flags, (list, tuple, set)):
        return "|".join(str(value or "").strip() for value in flags if str(value or "").strip())
    return str(flags)


def _tag_entity_empty_engine_visibility_from_children(entity_empty, kwargs=None):
    if entity_empty is None:
        return
    tagged_children = []
    for child in list(getattr(entity_empty, "children_recursive", []) or []):
        try:
            value = child.get("witcher_layer_engine_visible", None)
        except Exception:
            value = None
        if value is None:
            continue
        tagged_children.append((child, bool(value)))
    if not tagged_children:
        return

    hidden_children = [child for child, visible in tagged_children if not visible]
    all_hidden = len(hidden_children) == len(tagged_children)
    try:
        entity_empty["witcher_entity_drawable_components"] = len(tagged_children)
        entity_empty["witcher_entity_engine_hidden_components"] = len(hidden_children)
        entity_empty["witcher_layer_engine_visible"] = not all_hidden
        drawable_values = []
        for child, _visible in tagged_children:
            value = child.get("witcher_redkit_drawableFlags", child.get("witcher_drawableFlags", None))
            if value is not None and str(value) not in drawable_values:
                drawable_values.append(str(value))
        if drawable_values:
            entity_empty["witcher_redkit_drawableFlags"] = ";".join(drawable_values)
    except Exception:
        pass

    if all_hidden and _hide_engine_hidden_meshes_enabled(kwargs):
        try:
            entity_empty.hide_viewport = True
            entity_empty.hide_render = True
        except Exception:
            pass
    elif _hide_engine_hidden_meshes_enabled(kwargs):
        try:
            entity_empty.hide_viewport = False
            entity_empty.hide_render = False
        except Exception:
            pass


def _sector_visibility_key_for_item(kind, item):
    kind = str(kind or "").strip().lower()
    if kind not in {"mesh", "rigid", "rigid_body", "sector_instancer"}:
        return ""
    if not isinstance(item, dict) or "sector_flags" not in item:
        return ""
    return _sector_visibility_key_from_flags(item.get("sector_flags"), default=True)


def _hide_engine_hidden_meshes_enabled(kwargs=None, context=None):
    if kwargs and "hide_engine_hidden_meshes" in kwargs:
        return bool(kwargs.get("hide_engine_hidden_meshes"))
    scene = getattr(context or bpy.context, "scene", None)
    scene_settings = getattr(scene, "witcher_file_browser", None) if scene is not None else None
    return bool(getattr(scene_settings, "terrain_layer_hide_engine_hidden_meshes", True))


def _path_indicates_proxy_mesh(repo_path, name=""):
    text = f"{repo_path or ''}/{name or ''}".replace("\\", "/").lower()
    if not text:
        return False
    parts = [part for part in re.split(r"[\\/]+", text) if part]
    tokens = []
    for part in parts:
        stem = Path(part).stem if "." in part else part
        if stem == "no_proxy" or stem.endswith("_no_proxy") or stem.endswith("-no-proxy"):
            continue
        tokens.extend(token for token in re.split(r"[_\-\s]+", stem) if token)
    return any(token == "proxy" for token in tokens)


def _path_extension(repo_path):
    return Path(str(repo_path or "").replace("\\", "/")).suffix.lower()


def _sector_collision_path_is_visual_mesh(repo_path):
    return _path_extension(repo_path) in {".w2mesh", ".mesh"}


def _sector_proxy_role_from_flags(flags):
    try:
        value = int(flags or 0)
    except Exception:
        value = 0
    if value & _SECTOR_FLAG_MESH_ROOT_ENTITY_PROXY:
        return "root"
    if value & _SECTOR_FLAG_MESH_PART_OF_ENTITY_PROXY:
        return "part"
    return ""


def _cached_plan_item_is_proxy_mesh(item):
    if not isinstance(item, dict):
        return False
    proxy_role = str(item.get("proxy_role", "") or "").strip().lower()
    if proxy_role == "part":
        return False
    if proxy_role == "root":
        return True
    if bool(item.get("is_proxy_mesh", False)):
        return True
    return _path_indicates_proxy_mesh(item.get("repo_path", ""), item.get("name", ""))


def _proxy_mesh_filter_active(kwargs):
    return "do_import_ProxyMesh" in dict(kwargs or {})


def _redcloth_enabled_for_import(kwargs, context=None):
    global_enabled = bool(get_do_import_redcloth(context or bpy.context))
    if "do_import_Redcloth" in dict(kwargs or {}):
        return bool(kwargs.get("do_import_Redcloth", False)) and global_enabled
    return global_enabled


def _redapex_enabled_for_import(kwargs, context=None):
    if "do_import_Redapex" in dict(kwargs or {}):
        return bool(kwargs.get("do_import_Redapex", False))
    return False


def _is_redapex_resource(resource_path):
    return str(resource_path or "").strip().lower().endswith(".redapex")


def _cloth_resource_enabled_for_import(resource_path, kwargs, context=None):
    if _is_redapex_resource(resource_path):
        return _redapex_enabled_for_import(kwargs, context)
    return _redcloth_enabled_for_import(kwargs, context)


def _redapex_import_options(kwargs):
    return {
        "import_chunks": bool(kwargs.get("redapex_import_chunks", False)),
        "import_floor": bool(kwargs.get("redapex_import_floor", False)),
        "collections_as_empties": bool(kwargs.get("redapex_collections_as_empties", True)),
    }


def _chunk_cloth_resource(chunk):
    try:
        resource_var = chunk.GetVariableByName('resource') or chunk.GetVariableByName('m_resource')
        handles = getattr(resource_var, "Handles", None) or []
        if handles:
            return str(getattr(handles[0], "DepotPath", "") or "").strip()
    except Exception:
        pass
    return ""

def _new_mesh_path(
    mesh_name=False,
    translation=False,
    matrix=False,
    *,
    fbx_uncook_path=None,
    uncook_path=None,
    transform=False,
    block_data_object_type=Enums.BlockDataObjectType.Mesh,
):
    return meshPath(
        meshName=mesh_name,
        translation=translation,
        matrix=matrix,
        fbx_uncook_path=fbx_uncook_path if fbx_uncook_path is not None else False,
        transform=transform,
        BlockDataObjectType=block_data_object_type,
        uncook_path=uncook_path,
    )


def get_CSectorData(level, *, mesh_fbx_uncook_path=None, mesh_uncook_path=None):
    if level.CSectorData:
        #import entities hold import data
        static_mesh_list = []
        #meshPath entities hold a transform and componants such as import data
        # THIS_ENTITY = meshPath("CSectorData_Transform", False, False, fbx_uncook_path, BasicEngineQsTransform())
        # THIS_ENTITY.type = "Entity"
        for idx, block in enumerate(level.CSectorData.BlockData):
            #TESTING
            this_type = Enums.BlockDataObjectType.getEnum(block.packedObjectType)
            if hasattr(block, 'resourceIndex') and block.resourceIndex < 12:
                this_resource = level.CSectorData.Resources[block.resourceIndex].pathHash
                log.debug(str(block.resourceIndex)+' '+this_resource)

            if block.packedObjectType == Enums.BlockDataObjectType.Mesh:# or block.packedObjectType == Enums.BlockDataObjectType.Invalid:
                mesh_path = level.CSectorData.Resources[block.packedObject.meshIndex].pathHash
                #obj_pos = level.CSectorData.Objects[idx].position
                mesh_item = _new_mesh_path(
                    mesh_path,
                    block.position,
                    MatrixToArray(block.rotationMatrix),
                    fbx_uncook_path=mesh_fbx_uncook_path,
                    uncook_path=mesh_uncook_path,
                )
                mesh_item.sector_flags = int(getattr(block, "flags", 0) or 0)
                mesh_item.proxy_role = _sector_proxy_role_from_flags(mesh_item.sector_flags)
                mesh_item.is_proxy_mesh = mesh_item.proxy_role == "root" or _path_indicates_proxy_mesh(mesh_path, "")
                static_mesh_list.append(mesh_item)
            if block.packedObjectType == Enums.BlockDataObjectType.RigidBody:
                mesh_path = level.CSectorData.Resources[block.packedObject.meshIndex].pathHash
                mesh_item = _new_mesh_path(
                    mesh_path,
                    block.position,
                    MatrixToArray(block.rotationMatrix),
                    fbx_uncook_path=mesh_fbx_uncook_path,
                    uncook_path=mesh_uncook_path,
                    block_data_object_type=Enums.BlockDataObjectType.RigidBody,
                )
                mesh_item.sector_flags = int(getattr(block, "flags", 0) or 0)
                static_mesh_list.append(mesh_item)
                log.info("found RigidBody in CSectorData")
            if block.packedObjectType == Enums.BlockDataObjectType.Collision:
                mesh_path = level.CSectorData.Resources[block.packedObject.meshIndex].pathHash
                mesh_item = _new_mesh_path(
                    mesh_path,
                    block.position,
                    MatrixToArray(block.rotationMatrix),
                    fbx_uncook_path=mesh_fbx_uncook_path,
                    uncook_path=mesh_uncook_path,
                    block_data_object_type=Enums.BlockDataObjectType.Collision,
                )
                mesh_item.sector_flags = int(getattr(block, "flags", 0) or 0)
                static_mesh_list.append(mesh_item)
                log.info("found Collision in CSectorData")
            if block.packedObjectType == Enums.BlockDataObjectType.PointLight:
                log.info("found point light in CSectorData")
                static_mesh_list.append(lightObject("PointLight", block.position, MatrixToArray(block.rotationMatrix), block = block, BlockDataObjectType = Enums.BlockDataObjectType.PointLight))
            if block.packedObjectType == Enums.BlockDataObjectType.SpotLight:
                static_mesh_list.append(lightObject("SpotLight", block.position, MatrixToArray(block.rotationMatrix), block = block, BlockDataObjectType = Enums.BlockDataObjectType.SpotLight))
                #light_path = level.CSectorData.Resources[block.resourceIndex].pathHash
                log.info("found spot light in CSectorData")
            if block.packedObjectType == Enums.BlockDataObjectType.Invalid:
                log.info("found point Invalid in CSectorData")
            if block.packedObjectType == Enums.BlockDataObjectType.Cloth:
                log.info("found point Cloth in CSectorData")
            if block.packedObjectType == Enums.BlockDataObjectType.Decal:
                log.info("found point Decal in CSectorData")
            if block.packedObjectType == Enums.BlockDataObjectType.Destruction:
                log.info("found point Destruction in CSectorData")
            if block.packedObjectType == Enums.BlockDataObjectType.Dimmer:
                log.info("found point Dimmer in CSectorData")
            if block.packedObjectType == Enums.BlockDataObjectType.Particles:
                log.info("found point Particles in CSectorData")
        return static_mesh_list
    else:
        return False


def recurLayerCollection(layerColl, collName):
    if layerColl is None:
        return None
    target_collection = collName if hasattr(collName, "name") else None
    target_name = target_collection.name if target_collection is not None else collName
    if target_collection is not None and getattr(layerColl, "collection", None) == target_collection:
        return layerColl
    found = None
    if layerColl.name == target_name:
        return layerColl
    for layer in layerColl.children:
        found = recurLayerCollection(layer, collName)
        if found:
            return found
         
import math
def is_within_distance(mesh_translation, reference_vector, distance_threshold):
    # Calculate the Euclidean distance between the two vectors
    distance = math.sqrt((mesh_translation[0] - reference_vector[0])**2 + 
                        (mesh_translation[1] - reference_vector[1])**2 +
                        (mesh_translation[2] - reference_vector[2])**2)
    
    # Check if the distance is within the threshold
    if distance <= distance_threshold:
        return True
    else:
        return False


def _get_nearby_import_stats(kwargs):
    stats = kwargs.get("_nearby_stats")
    if not isinstance(stats, dict):
        stats = {"filtered": 0}
        kwargs["_nearby_stats"] = stats
    stats["filtered"] = int(stats.get("filtered", 0) or 0)
    return stats


def _note_nearby_filter_skip(nearby_stats):
    nearby_stats["filtered"] = int(nearby_stats.get("filtered", 0) or 0) + 1


def _get_nearby_import_filter(kwargs):
    if "_nearby_filter" in kwargs:
        return kwargs.get("_nearby_filter")

    camera_position = kwargs.get("_nearby_camera_position")
    radius = kwargs.get("_nearby_radius", 0.0)
    nearby_filter = None
    try:
        if camera_position is not None:
            nearby_filter = {
                "camera_position": (
                    float(camera_position[0]),
                    float(camera_position[1]),
                    float(camera_position[2]),
                ),
                "radius": float(radius or 0.0),
            }
            nearby_filter["radius_sq"] = nearby_filter["radius"] * nearby_filter["radius"]
            if nearby_filter["radius"] <= 0.0:
                nearby_filter = None
    except Exception:
        nearby_filter = None

    kwargs["_nearby_filter"] = nearby_filter
    return nearby_filter


def _extract_vector_position(value):
    if value is None:
        return None
    try:
        return float(value.x), float(value.y), float(value.z)
    except Exception:
        pass
    try:
        return float(value[0]), float(value[1]), float(value[2])
    except Exception:
        return None


def _extract_transform_position(transform):
    if transform is None:
        return None
    return (
        _transform_real(transform, "X", 0.0),
        _transform_real(transform, "Y", 0.0),
        _transform_real(transform, "Z", 0.0),
    )


def _compose_world_position(local_position, parent_position=None):
    if local_position is None:
        return parent_position
    if parent_position is None:
        return local_position
    return (
        float(parent_position[0]) + float(local_position[0]),
        float(parent_position[1]) + float(local_position[1]),
        float(parent_position[2]) + float(local_position[2]),
    )


def _position_within_nearby_filter(position, nearby_filter):
    if nearby_filter is None or position is None:
        return True
    camera_position = nearby_filter["camera_position"]
    dx = float(position[0]) - camera_position[0]
    dy = float(position[1]) - camera_position[1]
    return (dx * dx + dy * dy) <= nearby_filter["radius_sq"]


def _mesh_world_position(mesh, parent_position=None):
    translation = _extract_vector_position(getattr(mesh, "translation", None))
    if translation is not None:
        return translation
    return _compose_world_position(
        _extract_transform_position(getattr(mesh, "transform", None)),
        parent_position,
    )


def _entity_world_position(entity, parent_position=None):
    return _compose_world_position(
        _extract_transform_position(getattr(entity, "transform", None)),
        parent_position,
    )


def _chunk_world_position(chunk, parent_position=None):
    if chunk is None or not hasattr(chunk, "GetVariableByName"):
        return parent_position
    try:
        transform_prop = chunk.GetVariableByName("transform")
    except Exception:
        transform_prop = None
    transform = getattr(transform_prop, "EngineTransform", None) if transform_prop else None
    return _compose_world_position(_extract_transform_position(transform), parent_position)


def _copy_engine_transform_dict(transform):
    if transform is None:
        return None
    return {
        "X": _transform_real(transform, "X", 0.0),
        "Y": _transform_real(transform, "Y", 0.0),
        "Z": _transform_real(transform, "Z", 0.0),
        "Yaw": _transform_real(transform, "Yaw", 0.0),
        "Pitch": _transform_real(transform, "Pitch", 0.0),
        "Roll": _transform_real(transform, "Roll", 0.0),
        "Scale_x": _transform_real(transform, "Scale_x", 1.0),
        "Scale_y": _transform_real(transform, "Scale_y", 1.0),
        "Scale_z": _transform_real(transform, "Scale_z", 1.0),
    }


def _copy_matrix_array(matrix_value):
    if matrix_value is None:
        return None
    try:
        rows = []
        for row in matrix_value:
            rows.append(tuple(float(value) for value in row))
        return tuple(rows) if rows else None
    except Exception:
        return None


def _copy_translation_vector(value):
    position = _extract_vector_position(value)
    if position is None:
        return None
    return (
        float(position[0]),
        float(position[1]),
        float(position[2]),
    )


def _copy_world_position(position):
    if position is None:
        return None
    return (
        float(position[0]),
        float(position[1]),
        float(position[2]),
    )


def _new_level_import_plan():
    return {
        "items": [],
        "stats": {
            "total": 0,
            "filtered": 0,
            "by_kind": {},
        },
    }


def _add_level_import_plan_item(
    plan,
    kind,
    name,
    *,
    parent_id="",
    repo_path="",
    transform=None,
    matrix=None,
    translation=None,
    world_position=None,
    is_proxy_mesh=None,
    proxy_role="",
    sector_flags=None,
    sector_transforms=None,
    sector_visibility_key="",
    sector_visible=None,
    source_kind="",
    drawable_flags=None,
    engine_visible=None,
):
    item_kind = str(kind or "unknown").strip() or "unknown"
    item = {
        "id": f"item_{len(plan['items']) + 1}",
        "kind": item_kind,
        "name": str(name or item_kind).strip() or item_kind,
        "parent_id": str(parent_id or "").strip(),
        "repo_path": str(repo_path or "").strip(),
        "transform": _copy_engine_transform_dict(transform),
        "matrix": _copy_matrix_array(matrix),
        "translation": _copy_translation_vector(translation),
        "world_position": _copy_world_position(world_position),
    }
    if is_proxy_mesh is not None:
        item["is_proxy_mesh"] = bool(is_proxy_mesh)
    if proxy_role:
        item["proxy_role"] = str(proxy_role)
    if sector_flags is not None:
        try:
            item["sector_flags"] = int(sector_flags)
        except Exception:
            pass
    if sector_transforms is not None:
        item["sector_transforms"] = list(sector_transforms)
    if sector_visibility_key:
        item["sector_visibility_key"] = str(sector_visibility_key)
    if sector_visible is not None:
        item["sector_visible"] = bool(sector_visible)
    if source_kind:
        item["_source_kind"] = str(source_kind)
    if drawable_flags is not None:
        item["drawable_flags"] = drawable_flags
    if engine_visible is not None:
        item["engine_visible"] = bool(engine_visible)
    plan["items"].append(item)
    plan["stats"]["total"] = len(plan["items"])
    by_kind = plan["stats"]["by_kind"]
    by_kind[item_kind] = int(by_kind.get(item_kind, 0) or 0) + 1
    return item["id"]


def _remove_level_import_plan_item(plan, item_id):
    if not item_id:
        return
    removed_item = None
    for index, item in enumerate(plan["items"]):
        if item.get("id") == item_id:
            removed_item = plan["items"].pop(index)
            break
    if removed_item is None:
        return
    kind = str(removed_item.get("kind", "") or "").strip()
    if kind:
        by_kind = plan["stats"]["by_kind"]
        remaining = int(by_kind.get(kind, 0) or 0) - 1
        if remaining > 0:
            by_kind[kind] = remaining
        else:
            by_kind.pop(kind, None)
    plan["stats"]["total"] = len(plan["items"])


def _create_linked_empty(name, target_collection=None, *, display_size=0.25):
    target_collection = target_collection or _get_active_collection()
    if target_collection is None:
        return None
    obj = bpy.data.objects.new(str(name or "Empty"), None)
    obj.empty_display_type = 'PLAIN_AXES'
    obj.empty_display_size = float(display_size)
    target_collection.objects.link(obj)
    return obj


def _apply_plan_item_transform(obj, item):
    transform = item.get("transform")
    if transform:
        set_blender_object_transform(obj, transform)

    matrix_rows = item.get("matrix")
    if matrix_rows:
        mat = Matrix.Identity(4)
        try:
            for row_index, row in enumerate(matrix_rows):
                if row_index >= 4:
                    break
                for col_index, value in enumerate(row):
                    if col_index >= 4:
                        break
                    mat[row_index][col_index] = float(value)
            obj.matrix_basis = mat
        except Exception:
            pass

    translation = item.get("translation")
    if translation is not None:
        try:
            obj.location[0] = float(translation[0])
            obj.location[1] = float(translation[1])
            obj.location[2] = float(translation[2])
        except Exception:
            pass


def _engine_transform_to_local_matrix(engine_transform, rotate_180=False):
    yaw = _transform_real(engine_transform, "Yaw", 0.0)
    pitch = _transform_real(engine_transform, "Pitch", 0.0)
    roll = _transform_real(engine_transform, "Roll", 0.0)
    loc_x = _transform_real(engine_transform, "X", 0.0)
    loc_y = _transform_real(engine_transform, "Y", 0.0)
    loc_z = _transform_real(engine_transform, "Z", 0.0)

    if yaw == 0.0 and pitch == 0.0 and roll == 0.0:
        mat = Matrix.Identity(4)
    else:
        mat = Euler((radians(yaw), radians(pitch), radians(roll)), 'YXZ').to_matrix().to_4x4()
        if rotate_180:
            mat[0][0], mat[0][1], mat[0][2] = -mat[0][0], -mat[0][1], mat[0][2]
            mat[1][0], mat[1][1], mat[1][2] = -mat[1][0], -mat[1][1], mat[1][2]
            mat[2][0], mat[2][1], mat[2][2] = -mat[2][0], -mat[2][1], mat[2][2]

    mat.translation = (loc_x, loc_y, loc_z)
    return mat


def _apply_engine_transform_local(obj, engine_transform, rotate_180=False):
    if obj is None or engine_transform is None:
        return
    obj.matrix_parent_inverse = Matrix.Identity(4)
    obj.matrix_basis = _engine_transform_to_local_matrix(engine_transform, rotate_180=rotate_180)
    if isinstance(engine_transform, dict) or hasattr(engine_transform, "Scale_x"):
        obj.scale[0] = _transform_real(engine_transform, "Scale_x", 1.0)
        obj.scale[1] = _transform_real(engine_transform, "Scale_y", 1.0)
        obj.scale[2] = _transform_real(engine_transform, "Scale_z", 1.0)


def _apply_plan_item_transform_as_child(obj, item):
    transform = item.get("transform")
    if transform:
        _apply_engine_transform_local(obj, transform)
        return
    _apply_plan_item_transform(obj, item)


def _tag_single_object_for_layer(obj, owner_tag=None, generation_tag=None):
    if obj is None:
        return
    owner_tag = str(owner_tag or "").strip()
    generation_tag = str(generation_tag or "").strip()
    try:
        if owner_tag:
            obj[_LAYER_IMPORT_OWNER_PROP] = owner_tag
        if generation_tag:
            obj[_LAYER_IMPORT_GENERATION_PROP] = generation_tag
    except Exception:
        pass


def _tag_object_tree_for_plan_item(root_obj, item_id, mode_signature=""):
    _tag_object_tree_for_layer_and_plan(
        root_obj,
        item_id=item_id,
        mode_signature=mode_signature,
    )


def _cached_plan_loaded_item_ids(target_collection, mode_signature=""):
    return set(_cached_plan_loaded_item_objects(target_collection, mode_signature).keys())


def _cached_plan_loaded_item_objects(target_collection, mode_signature=""):
    loaded = {}
    mode_signature = str(mode_signature or "").strip()
    if target_collection is None:
        return loaded
    for obj in list(getattr(target_collection, "all_objects", []) or []):
        try:
            item_id = str(obj.get(_LAYER_IMPORT_PLAN_ITEM_PROP, "") or "").strip()
            if not item_id:
                continue
            obj_mode = str(obj.get(_LAYER_IMPORT_PLAN_MODE_PROP, "") or "").strip()
            if mode_signature and obj_mode and obj_mode != mode_signature:
                continue
            loaded.setdefault(item_id, obj)
        except Exception:
            continue
    return loaded


def _cached_plan_filter_for_position(camera_position=None, radius=0.0):
    if camera_position is None:
        return None
    try:
        radius_value = float(radius or 0.0)
        if radius_value <= 0.0:
            return None
        return {
            "camera_position": (
                float(camera_position[0]),
                float(camera_position[1]),
                float(camera_position[2]),
            ),
            "radius": radius_value,
            "radius_sq": radius_value * radius_value,
        }
    except Exception:
        return None


def cached_plan_can_use_full_import(plan_items, camera_position=None, radius=0.0, import_kwargs=None, context=None):
    nearby_filter = _cached_plan_filter_for_position(camera_position, radius)
    nearby_stats = {"filtered": 0}
    source_items = [item for item in plan_items or [] if isinstance(item, dict)]
    if import_kwargs is not None:
        source_items = cached_plan_filter_items_for_import_options(
            source_items,
            import_kwargs or {},
            context=context,
        )
        source_items = _maybe_group_cached_items_into_sector_instancers(source_items, import_kwargs)
        source_items = _ensure_cached_sector_group_hierarchy(source_items)
    filtered_items = _filter_cached_plan_items_by_proximity(
        source_items,
        nearby_filter,
        nearby_stats,
    )
    has_nearby_item = False
    for item in filtered_items:
        has_nearby_item = True
        kind = str(item.get("kind", "") or "").strip().lower()
        if kind in _CACHED_FULL_ITEM_KINDS:
            continue
        if kind in _CACHED_FULL_PARENT_ITEM_KINDS:
            continue
        if kind:
            return False
    return has_nearby_item


def _filter_cached_plan_items_by_kinds(items, item_kinds):
    if not item_kinds:
        return list(items or [])
    wanted_kinds = {str(kind or "").strip().lower() for kind in item_kinds if str(kind or "").strip()}
    if not wanted_kinds:
        return list(items or [])

    by_id = {}
    for item in items or []:
        item_id = str(item.get("id", "") or "").strip() if isinstance(item, dict) else ""
        if item_id:
            by_id[item_id] = item

    keep = set()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id", "") or "").strip()
        kind = str(item.get("kind", "") or "").strip().lower()
        if not item_id or kind not in wanted_kinds:
            continue
        current = item
        while current is not None:
            current_id = str(current.get("id", "") or "").strip()
            if not current_id or current_id in keep:
                break
            keep.add(current_id)
            parent_id = str(current.get("parent_id", "") or "").strip()
            current = by_id.get(parent_id)

    return [item for item in items or [] if isinstance(item, dict) and str(item.get("id", "") or "").strip() in keep]


def _cached_plan_items_by_id(items):
    by_id = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id", "") or "").strip()
        if item_id:
            by_id[item_id] = item
    return by_id


def _cached_plan_parent_chain(item, by_id):
    current = item
    seen = set()
    while isinstance(current, dict):
        parent_id = str(current.get("parent_id", "") or "").strip()
        if not parent_id or parent_id in seen:
            break
        seen.add(parent_id)
        current = by_id.get(parent_id)
        if isinstance(current, dict):
            yield current
        else:
            break


def _cached_plan_nearest_entity_parent(item, by_id):
    for parent in _cached_plan_parent_chain(item, by_id):
        if str(parent.get("kind", "") or "").strip().lower() == "entity":
            return parent
    return None


def _cached_plan_item_matches_regex(item, by_id, regex_pattern):
    if regex_pattern is None:
        return True
    candidates = [item.get("name", ""), item.get("repo_path", "")]
    for value in candidates:
        value = str(value or "")
        if value and regex_pattern.search(value):
            return True
    return False


def _cached_plan_item_enabled_by_import_options(item, by_id, kwargs, *, context=None):
    kind = str(item.get("kind", "") or "").strip().lower()
    if kind in _CACHED_FULL_PARENT_ITEM_KINDS:
        return False
    is_proxy_mesh = kind in {"mesh", "component_mesh"} and _cached_plan_item_is_proxy_mesh(item)
    if is_proxy_mesh and _proxy_mesh_filter_active(kwargs):
        return bool(kwargs.get("do_import_ProxyMesh", False))
    if kind in _CACHED_REDCLOTH_ITEM_KINDS:
        return _cloth_resource_enabled_for_import(item.get("repo_path", ""), kwargs, context)
    if kind in {"mesh", "component_mesh", "foliage", "grass"}:
        return bool(kwargs.get("do_import_Mesh", True))
    if kind == "collision":
        return bool(kwargs.get("do_import_Collision", True))
    if kind in {"rigid", "rigid_body"}:
        return bool(kwargs.get("do_import_RigidBody", True))
    if kind in {"point_light", "component_point_light"}:
        return bool(kwargs.get("do_import_PointLight", True))
    if kind in {"spot_light", "component_spot_light"}:
        return bool(kwargs.get("do_import_SpotLight", True))
    if kind == "entity_template":
        return bool(kwargs.get("do_import_Entity", True))
    return True


def cached_plan_filter_items_for_import_options(items, kwargs=None, *, context=None):
    source_items = [item for item in items or [] if isinstance(item, dict)]
    kwargs = dict(kwargs or {})
    if not source_items:
        return []

    regex_pattern = None
    if bool(kwargs.get("do_enable_name_filter", False)):
        regex_text = str(kwargs.get("do_name_filter_regex", "") or "")
        if regex_text:
            try:
                regex_pattern = re.compile(regex_text)
            except Exception:
                log.warning("Invalid layer import regex filter: %s", regex_text)
                return []

    by_id = _cached_plan_items_by_id(source_items)
    keep = set()
    for item in source_items:
        item_id = str(item.get("id", "") or "").strip()
        if not item_id:
            continue
        if not _cached_plan_item_enabled_by_import_options(item, by_id, kwargs, context=context):
            continue
        if not _cached_plan_item_matches_regex(item, by_id, regex_pattern):
            continue
        current = item
        while isinstance(current, dict):
            current_id = str(current.get("id", "") or "").strip()
            if not current_id or current_id in keep:
                break
            keep.add(current_id)
            parent_id = str(current.get("parent_id", "") or "").strip()
            current = by_id.get(parent_id)

    return [item for item in source_items if str(item.get("id", "") or "").strip() in keep]


def _import_plan_as_dev_empties(plan, target_collection, kwargs):
    if target_collection is None:
        target_collection = _get_active_collection()
    created = {}
    owner_tag = kwargs.get("_layer_import_owner")
    generation_tag = kwargs.get("_layer_import_generation")

    for item in plan.get("items", []):
        _raise_if_layer_import_cancelled(kwargs)
        obj = _create_linked_empty(item.get("name", "Empty"), target_collection)
        if obj is None:
            continue
        parent_obj = created.get(str(item.get("parent_id", "") or "").strip())
        if parent_obj is not None:
            obj.parent = parent_obj
        _apply_plan_item_transform(obj, item)
        _tag_single_object_for_layer(obj, owner_tag, generation_tag)
        try:
            obj["witcher_dev_proxy"] = True
            obj["witcher_dev_kind"] = str(item.get("kind", "") or "")
            repo_path = str(item.get("repo_path", "") or "").strip()
            if repo_path:
                obj["witcher_dev_source_path"] = repo_path
        except Exception:
            pass
        created[item["id"]] = obj
    return len(created)


def _import_cached_plan_redcloth_items(plan, target_collection, kwargs, context=None, loaded_collection=None, errors=None):
    if target_collection is None:
        target_collection = _get_active_collection(context)
    if target_collection is None:
        return 0

    from ..importers import import_entity

    items = [item for item in plan.get("items", []) or [] if isinstance(item, dict)]
    by_id = {
        str(item.get("id", "") or "").strip(): item
        for item in items
        if str(item.get("id", "") or "").strip()
    }
    mode_signature = str(kwargs.get("_layer_import_mode_signature", "") or "").strip()
    if not mode_signature:
        mode_signature = _layer_load_mode_signature(False)
    loaded_item_ids = _cached_plan_loaded_item_ids(loaded_collection or target_collection, mode_signature)
    needed_ids = set()

    for item in items:
        item_id = str(item.get("id", "") or "").strip()
        kind = str(item.get("kind", "") or "").strip().lower()
        repo_path_value = str(item.get("repo_path", "") or "").strip()
        if kind not in _CACHED_REDCLOTH_ITEM_KINDS or not item_id or not repo_path_value:
            continue
        if item_id in loaded_item_ids:
            continue
        current = item
        while current is not None:
            current_id = str(current.get("id", "") or "").strip()
            if not current_id or current_id in needed_ids:
                break
            needed_ids.add(current_id)
            parent_id = str(current.get("parent_id", "") or "").strip()
            current = by_id.get(parent_id)

    created = {}
    owner_tag = kwargs.get("_layer_import_owner")
    generation_tag = kwargs.get("_layer_import_generation")

    def ensure_parent_empty(item_id):
        item_id = str(item_id or "").strip()
        if not item_id or item_id not in needed_ids:
            return None
        existing = created.get(item_id)
        if existing is not None:
            return existing
        item = by_id.get(item_id)
        if item is None:
            return None
        kind = str(item.get("kind", "") or "").strip().lower()
        if kind in _CACHED_REDCLOTH_ITEM_KINDS:
            return None
        parent_obj = ensure_parent_empty(str(item.get("parent_id", "") or "").strip())
        obj = _create_linked_empty(item.get("name", "Entity"), target_collection)
        if obj is None:
            return None
        if parent_obj is not None:
            obj.parent = parent_obj
        _apply_plan_item_transform(obj, item)
        _tag_single_object_for_layer(obj, owner_tag, generation_tag)
        _tag_object_tree_for_plan_item(obj, item_id, mode_signature)
        try:
            obj["witcher_cached_plan_proxy"] = True
            obj["witcher_cached_plan_kind"] = kind
        except Exception:
            pass
        created[item_id] = obj
        return obj

    imported_count = 0
    for item in items:
        _raise_if_layer_import_cancelled(kwargs)
        item_id = str(item.get("id", "") or "").strip()
        if not item_id or item_id not in needed_ids:
            continue
        kind = str(item.get("kind", "") or "").strip().lower()
        if kind not in _CACHED_REDCLOTH_ITEM_KINDS:
            ensure_parent_empty(item_id)
            continue
        resource = str(item.get("repo_path", "") or "").strip()
        if not resource or item_id in loaded_item_ids:
            continue
        if not _cloth_resource_enabled_for_import(resource, kwargs, context):
            continue
        parent_obj = ensure_parent_empty(str(item.get("parent_id", "") or "").strip())
        try:
            if _is_redapex_resource(resource):
                root_obj, _cloth_meshes = import_entity.import_or_reuse_redapex(
                    resource,
                    repo_file(resource),
                    context=context or bpy.context,
                    loadmods=bool(kwargs.get("loadmods", False)),
                    target_collection=target_collection,
                    **_redapex_import_options(kwargs),
                )
                cloth_arma = root_obj
                cloth_grp = None
            else:
                cloth_arma, cloth_grp, _cloth_meshes = import_entity.import_or_reuse_redcloth(
                    parent_obj,
                    resource,
                    repo_file(resource),
                    import_name="CClothComponent",
                    entity_name=str(item.get("name", "") or Path(resource.replace("/", "\\")).stem),
                    target_collection=target_collection,
                )
        except Exception as exc:
            resource_label = "redapex" if _is_redapex_resource(resource) else "redcloth"
            log.warning("Problem with cached %s import %s: %s", resource_label, resource, exc)
            if errors is not None:
                errors.append(f"Problem with cached {resource_label} import {resource}: {exc}")
            continue
        if cloth_arma is None:
            continue
        root_obj = cloth_grp if cloth_grp is not None else cloth_arma
        if parent_obj is not None:
            root_obj.parent = parent_obj
            _apply_plan_item_transform_as_child(root_obj, item)
        else:
            _apply_plan_item_transform(root_obj, item)
        _tag_object_tree_for_layer_and_plan(
            root_obj,
            owner_tag,
            generation_tag,
            item_id,
            mode_signature,
        )
        loaded_item_ids.add(item_id)
        imported_count += 1

    return imported_count


def _cached_plan_mesh_enabled(kind, kwargs, item=None):
    if kind in {"mesh", "component_mesh"} and _cached_plan_item_is_proxy_mesh(item) and _proxy_mesh_filter_active(kwargs):
        return bool(kwargs.get("do_import_ProxyMesh", False))
    if kind in {"mesh", "component_mesh", "foliage", "grass"}:
        return bool(kwargs.get("do_import_Mesh", True))
    if kind == "sector_instancer":
        # Synthetic sector_instancer items are only emitted by the grouping helper after
        # the per-toggle filter, so respecting their original source kind here would just
        # double-filter. Trust upstream filtering.
        source_kind = str((item or {}).get("_source_kind", "") or "").strip().lower()
        if source_kind in {"rigid", "rigid_body"}:
            return bool(kwargs.get("do_import_RigidBody", True))
        if source_kind == "collision":
            return bool(kwargs.get("do_import_Collision", True))
        return bool(kwargs.get("do_import_Mesh", True))
    if kind == "collision":
        return bool(kwargs.get("do_import_Collision", True))
    if kind in {"rigid", "rigid_body"}:
        return bool(kwargs.get("do_import_RigidBody", True))
    return False


def _cached_plan_mesh_from_item(item, kind, context=None):
    repo_path_value = str(item.get("repo_path", "") or "").strip()
    if not repo_path_value:
        return None
    block_type = Enums.BlockDataObjectType.Mesh
    if kind == "collision":
        block_type = Enums.BlockDataObjectType.Collision
    elif kind in {"rigid", "rigid_body"}:
        block_type = Enums.BlockDataObjectType.RigidBody

    fbx_uncook_path = None
    if kind in {"foliage", "grass"}:
        fbx_uncook_path = get_W3_FOLIAGE_PATH(context or bpy.context)

    mesh = _new_mesh_path(
        repo_path_value,
        item.get("translation") or False,
        item.get("matrix") or False,
        fbx_uncook_path=fbx_uncook_path,
        transform=item.get("transform") or False,
        block_data_object_type=block_type,
    )
    if item.get("sector_flags") is not None:
        try:
            mesh.sector_flags = int(item.get("sector_flags"))
        except Exception:
            pass
    if item.get("engine_visible") is not None:
        try:
            mesh.engine_visible = bool(item.get("engine_visible"))
        except Exception:
            pass
    if item.get("drawable_flags") is not None:
        try:
            mesh.drawable_flags = item.get("drawable_flags")
        except Exception:
            pass
    try:
        mesh.is_proxy_mesh = _cached_plan_item_is_proxy_mesh(item)
        mesh.proxy_role = str(item.get("proxy_role", "") or "")
    except Exception:
        pass
    try:
        mesh.import_name = str(item.get("name", "") or "").strip()
    except Exception:
        pass
    if kind in {"foliage", "grass"}:
        mesh.type = "mesh_foliage"
    return mesh


def _cached_plan_light_enabled(kind, kwargs):
    if kind in {"point_light", "component_point_light"}:
        return bool(kwargs.get("do_import_PointLight", True))
    if kind in {"spot_light", "component_spot_light"}:
        return bool(kwargs.get("do_import_SpotLight", True))
    return False


def _cached_plan_float(item, key, default=0.0):
    try:
        return float(item.get(key, default))
    except Exception:
        return float(default)


def _cached_plan_light_color(item):
    color = item.get("color")
    if isinstance(color, dict):
        try:
            return (
                float(color.get("Red", color.get("red", 255.0))) / 255.0,
                float(color.get("Green", color.get("green", 255.0))) / 255.0,
                float(color.get("Blue", color.get("blue", 255.0))) / 255.0,
            )
        except Exception:
            return (1.0, 1.0, 1.0)
    if isinstance(color, (list, tuple)) and len(color) >= 3:
        try:
            values = [float(color[0]), float(color[1]), float(color[2])]
            if max(values) > 1.0:
                values = [value / 255.0 for value in values]
            return tuple(values)
        except Exception:
            return (1.0, 1.0, 1.0)
    return (1.0, 1.0, 1.0)


def _import_cached_plan_light_item(
    item,
    kind,
    target_collection,
    parent_obj,
    owner_tag,
    generation_tag,
    item_id,
    mode_signature,
):
    light_type = "SPOT" if kind in {"spot_light", "component_spot_light"} else "POINT"
    name = str(item.get("name", "") or light_type.title())
    light_data = bpy.data.lights.new(name, type=light_type)
    brightness = _cached_plan_float(item, "brightness", 1.0)
    default_multiplier = 3.0 if light_type == "SPOT" else 10.0
    light_data.energy = _cached_plan_float(item, "energy", brightness * default_multiplier)
    light_data.color = _cached_plan_light_color(item)

    if item.get("radius") is not None:
        radius_value = _cached_plan_float(item, "radius", 0.0)
        if kind in {"point_light", "spot_light"}:
            radius_value /= 255.0
        light_data.shadow_soft_size = max(0.0, radius_value)

    if light_type == "SPOT":
        light_data.spot_blend = _cached_plan_float(item, "spot_blend", 0.0)
        if item.get("outer_angle") is not None:
            light_data.spot_size = _cached_plan_float(item, "outer_angle", light_data.spot_size)

    light_obj = bpy.data.objects.new(name, light_data)
    target_collection.objects.link(light_obj)
    if parent_obj is not None:
        light_obj.parent = parent_obj
    _apply_plan_item_transform(light_obj, item)
    if light_type == "SPOT":
        light_obj.rotation_euler.x += 1.5708
    _tag_single_object_for_layer(light_obj, owner_tag, generation_tag)
    _tag_object_tree_for_plan_item(light_obj, item_id, mode_signature)
    return light_obj


# ---------------------------------------------------------------------------
# Sector GN instancer helpers
# ---------------------------------------------------------------------------

def _decompose_sector_3x3(mat3_rows):
    """Return (euler_xyz_radians, scale_xyz) from a CR2W 3x3 rotation matrix.

    The engine stores the basis vectors as rows but Blender expects them as columns,
    so the matrix has to be transposed before decomposition. import_single_mesh applies
    the same transpose when placing individual mesh objects.
    """
    try:
        from mathutils import Matrix
        r0 = mat3_rows[0]
        r1 = mat3_rows[1]
        r2 = mat3_rows[2]
        m3 = Matrix((
            (float(r0[0]), float(r1[0]), float(r2[0])),
            (float(r0[1]), float(r1[1]), float(r2[1])),
            (float(r0[2]), float(r1[2]), float(r2[2])),
        ))
        _, rot_quat, scale = m3.to_4x4().decompose()
        e = rot_quat.to_euler('XYZ')
        return (e.x, e.y, e.z), (scale.x, scale.y, scale.z)
    except Exception:
        return (0.0, 0.0, 0.0), (1.0, 1.0, 1.0)


def _rebuild_sector_instancer_mesh(instancer_obj, transforms):
    """
    Rebuild the instancer point-cloud mesh.
    transforms: list of (x, y, z, ex, ey, ez, sx, sy, sz)
    """
    mesh = instancer_obj.data
    n = len(transforms)
    mesh.clear_geometry()
    if n == 0:
        return
    flat_pos, flat_rot, flat_scale = [], [], []
    for (x, y, z, ex, ey, ez, sx, sy, sz) in transforms:
        flat_pos  += [x, y, z]
        flat_rot  += [ex, ey, ez]
        flat_scale += [sx, sy, sz]
    mesh.vertices.add(n)
    mesh.vertices.foreach_set("co", flat_pos)
    for attr_name, flat_data in (("rot", flat_rot), ("scale", flat_scale)):
        existing = mesh.attributes.get(attr_name)
        if existing is not None:
            mesh.attributes.remove(existing)
        attr = mesh.attributes.new(attr_name, 'FLOAT_VECTOR', 'POINT')
        attr.data.foreach_set("vector", flat_data)
    mesh.update()


def _build_sector_instancer_gn_tree(ng, source_obj):
    """
    Geometry Nodes tree:
      Named Attribute "rot"   (FLOAT_VECTOR euler XYZ) → Euler→Rotation ─┐
      Named Attribute "scale" (FLOAT_VECTOR)           ─────────────────────┤ Instance on Points → Output
      Object Info (source_obj) → Geometry              ─────────────────────┘
    """
    nodes = ng.nodes
    links = ng.links
    nodes.clear()

    use_iface = hasattr(ng, "interface") and hasattr(ng.interface, "new_socket")

    def _sock(name, in_out, stype):
        if use_iface:
            ng.interface.new_socket(name=name, in_out=in_out, socket_type=stype)
        else:
            (ng.inputs if in_out == 'INPUT' else ng.outputs).new(stype, name)

    _sock("Geometry", "OUTPUT", "NodeSocketGeometry")
    _sock("Geometry", "INPUT",  "NodeSocketGeometry")

    gin  = nodes.new('NodeGroupInput');  gin.location  = (-800, 0)
    gout = nodes.new('NodeGroupOutput'); gout.location = ( 500, 0)

    # Named attribute: rotation
    na_rot = nodes.new('GeometryNodeInputNamedAttribute')
    na_rot.location = (-600, -100)
    for v in ('FLOAT_VECTOR', 'VECTOR'):
        try: na_rot.data_type = v; break
        except Exception: pass
    try:    na_rot.inputs["Name"].default_value = "rot"
    except Exception: na_rot.inputs[0].default_value = "rot"

    # Named attribute: scale
    na_scale = nodes.new('GeometryNodeInputNamedAttribute')
    na_scale.location = (-600, -250)
    for v in ('FLOAT_VECTOR', 'VECTOR'):
        try: na_scale.data_type = v; break
        except Exception: pass
    try:    na_scale.inputs["Name"].default_value = "scale"
    except Exception: na_scale.inputs[0].default_value = "scale"

    # Euler → Rotation
    e2r = None
    for bl_id in ('FunctionNodeEulerToRotation', 'FunctionNodeRotationFromEuler'):
        try: e2r = nodes.new(bl_id); e2r.location = (-350, -100); break
        except Exception: pass

    # Object Info
    oi = nodes.new('GeometryNodeObjectInfo')
    oi.location = (-350, -300)
    try:    oi.inputs['Object'].default_value = source_obj
    except Exception: pass
    try:    oi.transform_space = 'ORIGINAL'
    except Exception: pass

    # Instance on Points
    iop = nodes.new('GeometryNodeInstanceOnPoints')
    iop.location = (150, 0)

    links.new(gin.outputs['Geometry'], iop.inputs['Points'])
    links.new(oi.outputs['Geometry'],  iop.inputs['Instance'])
    if e2r is not None:
        links.new(na_rot.outputs[0], e2r.inputs[0])
        try:    links.new(e2r.outputs['Rotation'], iop.inputs['Rotation'])
        except Exception: links.new(e2r.outputs[0], iop.inputs['Rotation'])
    else:
        try: links.new(na_rot.outputs[0], iop.inputs['Rotation'])
        except Exception: pass
    try:    links.new(na_scale.outputs[0], iop.inputs['Scale'])
    except Exception: pass
    links.new(iop.outputs['Instances'], gout.inputs['Geometry'])


_SECTOR_SOURCE_MARKER_PROP = "_sector_source_repo"
_SECTOR_SOURCES_COLLECTION_NAME = "__sector_sources__"
_SECTOR_SOURCE_CACHE = {
    "collection_key": None,
    "dirty": True,
    "sources": {},
}
_SECTOR_MISSING_NXS_CACHE = set()


def _id_key(data_block):
    if data_block is None:
        return None
    try:
        return int(data_block.as_pointer())
    except Exception:
        return id(data_block)


def _get_sector_sources_collection(context=None):
    """Return a persistent scene-level collection that holds all hidden source meshes.

    This collection lives as a direct child of the scene's master collection, NOT inside
    any layer collection. That way _capture_previous_layer_object_ids never sees the
    sources and they are never deleted during a layer reload cycle.
    """
    existing = bpy.data.collections.get(_SECTOR_SOURCES_COLLECTION_NAME)
    if existing is not None:
        return existing
    coll = bpy.data.collections.new(_SECTOR_SOURCES_COLLECTION_NAME)
    coll.hide_viewport = True
    coll.hide_render = True
    scene = _get_scene(context)
    if scene is not None:
        try:
            scene.collection.children.link(coll)
        except Exception:
            pass
    return coll


def _get_cached_sector_source(marker):
    marker = str(marker or "").strip()
    if not marker:
        return None
    sources_coll = _get_sector_sources_collection()
    collection_key = _id_key(sources_coll)
    if _SECTOR_SOURCE_CACHE.get("dirty") or _SECTOR_SOURCE_CACHE.get("collection_key") != collection_key:
        sources = {}
        for obj in list(getattr(sources_coll, "all_objects", []) or []):
            try:
                key = str(obj.get(_SECTOR_SOURCE_MARKER_PROP, "") or "").strip()
                if key:
                    sources[key] = obj
            except Exception:
                continue
        _SECTOR_SOURCE_CACHE["collection_key"] = collection_key
        _SECTOR_SOURCE_CACHE["dirty"] = False
        _SECTOR_SOURCE_CACHE["sources"] = sources
    obj = (_SECTOR_SOURCE_CACHE.get("sources") or {}).get(marker)
    if obj is None:
        return None
    try:
        if obj.name in bpy.data.objects:
            return obj
    except Exception:
        pass
    _SECTOR_SOURCE_CACHE["dirty"] = True
    return None


def _remember_sector_source(marker, source):
    marker = str(marker or "").strip()
    if not marker or source is None:
        return
    _SECTOR_SOURCE_CACHE.setdefault("sources", {})[marker] = source
    _SECTOR_SOURCE_CACHE["collection_key"] = _id_key(_get_sector_sources_collection())
    _SECTOR_SOURCE_CACHE["dirty"] = False


def _nxs_missing_key(path):
    return os.path.normcase(os.path.normpath(str(path or "")))


def _is_missing_nxs(path):
    key = _nxs_missing_key(path)
    return bool(key and key in _SECTOR_MISSING_NXS_CACHE)


def _remember_missing_nxs(path):
    key = _nxs_missing_key(path)
    if key:
        _SECTOR_MISSING_NXS_CACHE.add(key)


def _is_missing_file_error(exc):
    if isinstance(exc, FileNotFoundError):
        return True
    try:
        if getattr(exc, "errno", None) == 2:
            return True
    except Exception:
        pass
    text = str(exc or "").lower()
    return "no such file or directory" in text or "[errno 2]" in text


def _note_missing_nxs_once(path, label):
    if _is_missing_nxs(path):
        return
    _remember_missing_nxs(path)
    log.warning("%s missing NXS skipped %s", label, path)


def _apply_sector_collision_transform_and_tags(wrapper, mesh, parent_transform, kwargs):
    if wrapper is None:
        return None
    if parent_transform:
        wrapper.parent = parent_transform

    # Apply rotation (same column-transposed pattern as import_single_mesh).
    mat_src = getattr(mesh, "matrix", None)
    if mat_src:
        try:
            mat = Matrix()
            mat[0][0], mat[0][1], mat[0][2] = mat_src[0][0], mat_src[1][0], mat_src[2][0]
            mat[1][0], mat[1][1], mat[1][2] = mat_src[0][1], mat_src[1][1], mat_src[2][1]
            mat[2][0], mat[2][1], mat[2][2] = mat_src[0][2], mat_src[1][2], mat_src[2][2]
            wrapper.matrix_world = wrapper.matrix_world @ mat
        except Exception:
            pass

    translation = _extract_vector_position(getattr(mesh, "translation", None))
    if translation is not None:
        wrapper.location[0] = translation[0]
        wrapper.location[1] = translation[1]
        wrapper.location[2] = translation[2]

    _tag_object_tree_for_layer_and_plan(
        wrapper,
        kwargs.get("_layer_import_owner"),
        kwargs.get("_layer_import_generation"),
        kwargs.get("_layer_import_plan_item_id"),
        kwargs.get("_layer_import_plan_mode"),
    )
    for obj in [wrapper] + list(getattr(wrapper, "children_recursive", []) or []):
        try:
            obj["witcher_layer_visibility_kind"] = "collision"
        except Exception:
            pass
    return wrapper


def _import_sector_w2mesh_collision(mesh, errors, parent_transform, **kwargs):
    """Import collision-only geometry for a CSectorData collision record backed by .w2mesh."""
    mesh_path = str(getattr(mesh, "meshName", "") or "").strip()
    if not mesh_path or mesh_path == "0":
        return None

    try:
        from .import_nxs import create_from_nxs
        from ..CR2W.common_blender import get_collision_for_mesh_with_poses
    except ImportError as exc:
        log.warning("sector w2mesh collision import: missing module: %s", exc)
        return None

    collision_root_key = f"{mesh_path}#collision"
    existing = check_if_empty_already_in_scene(
        collision_root_key,
        fast_static_clone=bool(kwargs.get("_cached_plan_fast_static_clone", False)),
    )
    if existing:
        return _apply_sector_collision_transform_and_tags(existing, mesh, parent_transform, kwargs)

    collision_path, shape_items = get_collision_for_mesh_with_poses(mesh_path)
    if not collision_path:
        log.debug("No collision cache item found for mesh collision record: %s", mesh_path)
        return None
    if _is_missing_nxs(collision_path):
        return None
    if not os.path.exists(collision_path):
        _note_missing_nxs_once(collision_path, "Mesh collision")
        return None

    try:
        nxs_objects = create_from_nxs(collision_path, shape_items=shape_items)
    except Exception as exc:
        if _is_missing_file_error(exc):
            _note_missing_nxs_once(collision_path, "Mesh collision")
            return None
        log.warning("Mesh collision NXS import failed %s: %s", collision_path, exc)
        if errors is not None:
            errors.append(f"Mesh collision NXS import failed {mesh_path}: {exc}")
        return None

    if not nxs_objects:
        return None

    bpy.ops.object.empty_add(type="PLAIN_AXES", radius=1)
    wrapper = bpy.context.object
    stem = Path(mesh_path.replace("\\", "/")).stem or "Collision"
    wrapper.name = stem
    wrapper["repo_path"] = collision_root_key
    wrapper["witcher_collision_source_repo_path"] = mesh_path
    wrapper["witcher_layer_visibility_kind"] = "collision"
    wrapper.show_in_front = False

    for obj in nxs_objects:
        if obj is not None:
            obj.parent = wrapper
            try:
                obj["witcher_layer_visibility_kind"] = "collision"
                obj.show_in_front = False
            except Exception:
                pass

    _record_duplicate_root(wrapper)
    return _apply_sector_collision_transform_and_tags(wrapper, mesh, parent_transform, kwargs)


def _import_sector_collision_from_cache(mesh, errors, parent_transform, **kwargs):
    """Import sector collision blocks as collision-only geometry."""
    collision_path = str(getattr(mesh, "meshName", "") or "").strip()
    if not collision_path or collision_path == "0":
        return None
    if _sector_collision_path_is_visual_mesh(collision_path):
        return _import_sector_w2mesh_collision(mesh, errors, parent_transform, **kwargs)

    try:
        from .import_nxs import create_from_nxs
        from ..CR2W.witcher_cache.CollisionCache.CollisionManager import CollisionManager
    except ImportError as exc:
        log.warning("sector collision import: missing module: %s", exc)
        return None

    manager = CollisionManager.Get()

    # Prefer exact key match, fall back to case-insensitive
    raw = manager.Items.get(collision_path)
    item = None
    if raw:
        item = raw[0] if isinstance(raw, list) else raw
    if item is None:
        collision_lower = collision_path.lower()
        for key, val in manager.Items.items():
            if key.lower() == collision_lower:
                item = val[0] if isinstance(val, list) else val
                break

    if item is None:
        log.debug("Collision cache item not found for sector block: %s", collision_path)
        return None

    existing = check_if_empty_already_in_scene(
        collision_path,
        fast_static_clone=bool(kwargs.get("_cached_plan_fast_static_clone", False)),
    )
    if existing:
        return _apply_sector_collision_transform_and_tags(existing, mesh, parent_transform, kwargs)

    uncook = get_uncook_path(bpy.context)
    if not uncook:
        log.warning("No uncook path for collision extraction: %s", collision_path)
        return None

    ext = item.Extension or ".nxs"
    out_path = os.path.join(uncook, collision_path.replace("/", "\\"))
    if not out_path.lower().endswith(ext.lower()):
        out_path = os.path.splitext(out_path)[0] + ext

    if not os.path.exists(out_path):
        try:
            item.extract_to_file(out_path)
        except Exception as exc:
            if _is_missing_file_error(exc):
                _note_missing_nxs_once(out_path, "Collision extraction")
                return None
            log.warning("Collision extraction failed %s: %s", collision_path, exc)
            if errors is not None:
                errors.append(f"Collision extraction failed {collision_path}: {exc}")
            return None

    if _is_missing_nxs(out_path):
        return None
    try:
        nxs_objects = create_from_nxs(out_path)
    except Exception as exc:
        if _is_missing_file_error(exc):
            _note_missing_nxs_once(out_path, "Collision")
            return None
        log.warning("NXS import failed %s: %s", out_path, exc)
        if errors is not None:
            errors.append(f"NXS import failed {out_path}: {exc}")
        return None

    if not nxs_objects:
        return None

    bpy.ops.object.empty_add(type="PLAIN_AXES", radius=1)
    wrapper = bpy.context.object
    stem = Path(collision_path.replace("\\", "/")).stem or "Collision"
    wrapper.name = stem
    wrapper["repo_path"] = collision_path
    wrapper["witcher_layer_visibility_kind"] = "collision"
    wrapper.show_in_front = False

    for obj in nxs_objects:
        if obj is not None:
            obj.parent = wrapper
            try:
                obj.show_in_front = False
            except Exception:
                pass

    _record_duplicate_root(wrapper)
    return _apply_sector_collision_transform_and_tags(wrapper, mesh, parent_transform, kwargs)


def _join_nxs_objects_to_single_mesh(nxs_objects, name):
    """Merge all mesh objects returned by create_from_nxs into one mesh object.

    NXS files can contain multiple shapes (convex hulls, tri-meshes). For GN instancing
    all shapes must live in one source object so Object Info emits all of them together.
    Uses bmesh.from_mesh which appends (merges) geometry into an existing BMesh.
    """
    import bmesh as _bmesh
    mesh_objs = [o for o in (nxs_objects or []) if o is not None and o.type == 'MESH']
    if not mesh_objs:
        return None

    if len(mesh_objs) == 1:
        mesh_objs[0].name = name
        return mesh_objs[0]

    combined_mesh = bpy.data.meshes.new(name)
    bm = _bmesh.new()
    try:
        for obj in mesh_objs:
            mesh_copy = obj.data.copy()
            # Embed the object's own local transform so shapes align correctly
            mesh_copy.transform(obj.matrix_local)
            bm.from_mesh(mesh_copy)
            bpy.data.meshes.remove(mesh_copy)
        bm.to_mesh(combined_mesh)
    finally:
        bm.free()

    combined_obj = bpy.data.objects.new(name, combined_mesh)
    combined_obj.show_in_front = False
    # Remove the now-redundant individual objects
    for obj in mesh_objs:
        try:
            bpy.data.objects.remove(obj, do_unlink=True)
        except Exception:
            pass
    return combined_obj


def _get_or_import_sector_nxs_source_mesh(repo_path, kwargs, errors, mode_signature, item_id):
    """Return a single mesh object suitable as GN source for an NXS collision instancer.

    For standalone collision-cache paths, look up repo_path directly in the
    CollisionManager. For CSectorData collision records that point at .w2mesh,
    resolve the mesh's collision cache/NXS entry first. The returned object is a
    single joined mesh stored in the __sector_sources__ collection.
    """
    repo_path = str(repo_path or "").strip()
    if not repo_path:
        return None
    is_mesh_collision = _sector_collision_path_is_visual_mesh(repo_path)
    marker = f"{repo_path}#collision" if is_mesh_collision else repo_path

    cached_source = _get_cached_sector_source(marker)
    if cached_source is not None:
        return cached_source

    try:
        from .import_nxs import create_from_nxs
        if is_mesh_collision:
            from ..CR2W.common_blender import get_collision_for_mesh_with_poses
        else:
            from ..CR2W.witcher_cache.CollisionCache.CollisionManager import CollisionManager
    except ImportError as exc:
        log.warning("sector nxs source: missing module: %s", exc)
        return None

    shape_items = None
    if is_mesh_collision:
        out_path, shape_items = get_collision_for_mesh_with_poses(repo_path)
        if not out_path:
            log.debug("NXS source not found for mesh collision: %s", repo_path)
            return None
        if _is_missing_nxs(out_path):
            return None
        if not os.path.exists(out_path):
            _note_missing_nxs_once(out_path, "NXS source")
            return None
    else:
        manager = CollisionManager.Get()
        raw = manager.Items.get(repo_path)
        item = None
        if raw:
            item = raw[0] if isinstance(raw, list) else raw
        if item is None:
            path_lower = repo_path.lower()
            for key, val in manager.Items.items():
                if key.lower() == path_lower:
                    item = val[0] if isinstance(val, list) else val
                    break

        if item is None:
            log.debug("NXS source not found in collision cache: %s", repo_path)
            return None

        uncook = get_uncook_path(bpy.context)
        if not uncook:
            return None

        ext = item.Extension or ".nxs"
        out_path = os.path.join(uncook, repo_path.replace("/", "\\"))
        if not out_path.lower().endswith(ext.lower()):
            out_path = os.path.splitext(out_path)[0] + ext

        if not os.path.exists(out_path):
            try:
                item.extract_to_file(out_path)
            except Exception as exc:
                if _is_missing_file_error(exc):
                    _note_missing_nxs_once(out_path, "NXS extraction")
                    return None
                log.warning("NXS extraction failed %s: %s", repo_path, exc)
                if errors is not None:
                    errors.append(f"NXS extraction failed {repo_path}: {exc}")
                return None

    if _is_missing_nxs(out_path):
        return None
    try:
        nxs_objects = create_from_nxs(out_path, shape_items=shape_items)
    except Exception as exc:
        if _is_missing_file_error(exc):
            _note_missing_nxs_once(out_path, "NXS source")
            return None
        log.warning("NXS import failed %s: %s", out_path, exc)
        if errors is not None:
            errors.append(f"NXS import failed {out_path}: {exc}")
        return None

    if not nxs_objects:
        return None

    stem = Path(repo_path.replace("\\", "/")).stem or "ColSrc"
    source = _join_nxs_objects_to_single_mesh(nxs_objects, f"si_{stem}_src")
    if source is None:
        return None

    source[_SECTOR_SOURCE_MARKER_PROP] = marker
    source["_is_sector_source"] = True
    source["repo_path"] = marker
    if is_mesh_collision:
        source["witcher_collision_source_repo_path"] = repo_path
    source.hide_viewport = True
    source.hide_render = True
    source.show_in_front = False

    for c in list(source.users_collection):
        try:
            c.objects.unlink(source)
        except Exception:
            pass
    sources_coll = _get_sector_sources_collection()
    try:
        sources_coll.objects.link(source)
    except Exception:
        pass
    _remember_sector_source(marker, source)
    return source


def _pick_best_sector_source_mesh(new_objects, parent_root):
    """From a freshly-imported mesh hierarchy, keep only the highest-poly mesh as the source.

    parent_root is the empty wrapper from import_single_mesh; it is removed too because
    the GN Object Info node needs a real mesh, not an empty parent. Returns the kept mesh
    object or None if no mesh was found.
    """
    candidates = [o for o in new_objects if o is not None and o.type == 'MESH' and getattr(o, "data", None)]
    if not candidates:
        for o in new_objects:
            if o is None:
                continue
            try:
                bpy.data.objects.remove(o, do_unlink=True)
            except Exception:
                pass
        return None

    best = max(candidates, key=lambda o: len(o.data.polygons))
    for o in list(new_objects):
        if o is None or o is best:
            continue
        try:
            data = o.data if getattr(o, "type", "") == 'MESH' else None
        except Exception:
            data = None
        try:
            bpy.data.objects.remove(o, do_unlink=True)
        except Exception:
            continue
        if data is not None and data.users == 0:
            try:
                bpy.data.meshes.remove(data)
            except Exception:
                pass

    best.parent = None
    try:
        best.matrix_world = Matrix.Identity(4)
        best.matrix_local = Matrix.Identity(4)
        best.matrix_basis = Matrix.Identity(4)
    except Exception:
        pass
    best.location = (0.0, 0.0, 0.0)
    best.rotation_euler = (0.0, 0.0, 0.0)
    best.scale = (1.0, 1.0, 1.0)
    return best


def _get_or_import_sector_source_mesh(repo_path, target_collection, kwargs, errors, mode_signature, item_id):
    """Return the single LOD0 mesh that the GN instancer references for repo_path.

    Reuses an already-imported source within the current scene so multiple instancers
    of the same mesh share one source mesh object. The source is stored in the dedicated
    __sector_sources__ scene-level collection so it is invisible in the viewport and
    immune to layer-reload cleanup (target_collection is unused but kept for API compat).
    """
    marker = str(repo_path or "").strip()
    if not marker:
        return None

    cached_source = _get_cached_sector_source(marker)
    if cached_source is not None:
        return cached_source

    src_mesh_data = _new_mesh_path(
        repo_path,
        False, False,
        uncook_path=kwargs.get("mesh_uncook_path"),
        fbx_uncook_path=kwargs.get("mesh_fbx_uncook_path"),
    )
    mesh_kwargs = dict(kwargs)
    mesh_kwargs.pop("_cached_plan_fast_static_clone", None)
    # Remove positional-equivalent kwargs so they don't collide with the explicit args below
    mesh_kwargs.pop("keep_lod_meshes", None)
    mesh_kwargs.pop("keep_empty_lods", None)
    mesh_kwargs["_layer_import_plan_item_id"] = (item_id or "") + "_src"
    mesh_kwargs["_layer_import_plan_mode"] = mode_signature

    active_collection = _get_active_collection()
    before_ids = {
        _id_key(o)
        for o in list(getattr(active_collection, "all_objects", []) or [])
    }
    try:
        wrapper = import_single_mesh(
            src_mesh_data,
            errors,
            None,
            keep_lod_meshes=False,
            **mesh_kwargs,
        )
    except Exception as exc:
        log.warning("sector_instancer source import failed %s: %s", repo_path, exc)
        if errors is not None:
            errors.append(f"sector_instancer source failed {repo_path}: {exc}")
        return None

    new_objects = [
        o
        for o in list(getattr(active_collection, "all_objects", []) or [])
        if _id_key(o) not in before_ids
    ]
    if wrapper is not None and wrapper not in new_objects:
        new_objects.append(wrapper)

    source = _pick_best_sector_source_mesh(new_objects, wrapper)
    if source is None:
        return None

    stem = Path(repo_path.replace("\\", "/")).stem or "Source"
    source.name = f"si_{stem}_src"
    source[_SECTOR_SOURCE_MARKER_PROP] = marker
    source["_is_sector_source"] = True
    source["repo_path"] = repo_path
    source.hide_viewport = True
    source.hide_render = True
    source.show_in_front = False

    for c in list(source.users_collection):
        try:
            c.objects.unlink(source)
        except Exception:
            pass
    # Link source to the dedicated scene-level sources collection, NOT to the
    # per-layer target_collection. This keeps sources out of layer cleanup sweeps
    # so they survive reloads and are shared across all layers using the same mesh.
    sources_coll = _get_sector_sources_collection()
    try:
        sources_coll.objects.link(source)
    except Exception:
        pass
    _remember_sector_source(marker, source)
    return source


def _import_cached_plan_sector_instancer_item(
    item,
    target_collection,
    parent_obj,
    owner_tag,
    generation_tag,
    item_id,
    mode_signature,
    kwargs,
    errors,
    context=None,
):
    """Import a sector_instancer plan item: one hidden source mesh + one GN instancer per mesh type.

    The source must be a real MESH object (not an empty wrapper) because the GN Object Info
    node reads geometry directly from the referenced object — empties have no geometry and
    produce no instances.
    """
    repo_path = str(item.get("repo_path", "") or "").strip()
    if not repo_path:
        return None
    sector_transforms = item.get("sector_transforms") or []
    if not sector_transforms:
        return None

    stem = Path(repo_path.replace("\\", "/")).stem or "Mesh"
    source_kind = str(item.get("_source_kind", "") or "mesh").strip().lower()

    if source_kind == "collision":
        src_root = _get_or_import_sector_nxs_source_mesh(
            repo_path, kwargs, errors, mode_signature, item_id,
        )
    else:
        src_root = _get_or_import_sector_source_mesh(
            repo_path,
            target_collection,
            kwargs,
            errors,
            mode_signature,
            item_id,
        )
    if src_root is None:
        return None
    # Do NOT tag src_root with owner/generation: sources live in __sector_sources__
    # (a separate scene-level collection) and are shared across all layers that use
    # the same mesh. Tagging them would cause them to be captured and deleted by
    # _finalize_layer_reload_cleanup on subsequent around-camera reloads.

    all_t = []
    for t_entry in sector_transforms:
        if not isinstance(t_entry, dict):
            continue
        tx = t_entry.get("t") or [0.0, 0.0, 0.0]
        mx = t_entry.get("m")
        (ex, ey, ez), (sx, sy, sz) = _decompose_sector_3x3(mx) if mx else ((0.0, 0.0, 0.0), (1.0, 1.0, 1.0))
        try:
            all_t.append((float(tx[0]), float(tx[1]), float(tx[2]), ex, ey, ez, sx, sy, sz))
        except Exception:
            continue
    if not all_t:
        return None

    safe_name = f"si_{stem}"
    inst_mesh = bpy.data.meshes.new(safe_name)
    inst_obj  = bpy.data.objects.new(safe_name, inst_mesh)
    inst_obj.show_in_front = False
    if target_collection is not None:
        target_collection.objects.link(inst_obj)
    if parent_obj is not None:
        inst_obj.parent = parent_obj
    inst_obj["_is_sector_instancer"] = True
    inst_obj["repo_path"] = repo_path
    inst_obj["witcher_layer_visibility_kind"] = source_kind
    if item.get("sector_visible") is not None:
        inst_obj["witcher_layer_engine_visible"] = bool(item.get("sector_visible"))
    elif item.get("sector_visibility_key"):
        inst_obj["witcher_layer_engine_visible"] = str(item.get("sector_visibility_key")).lower() != "hidden"
    if (
        not bool(inst_obj.get("witcher_layer_engine_visible", True))
        and _hide_engine_hidden_meshes_enabled(kwargs, context)
    ):
        inst_obj.hide_viewport = True
        inst_obj.hide_render = True
    if item.get("sector_flags") is not None:
        try:
            inst_obj["witcher_sector_flags"] = int(item.get("sector_flags"))
        except Exception:
            pass
    _tag_single_object_for_layer(inst_obj, owner_tag, generation_tag)
    _tag_object_tree_for_plan_item(inst_obj, item_id, mode_signature)

    _rebuild_sector_instancer_mesh(inst_obj, all_t)

    mod = inst_obj.modifiers.new("SectorInstancer", 'NODES')
    ng  = bpy.data.node_groups.new(f"SectorInstancer_{stem}", 'GeometryNodeTree')
    mod.node_group = ng
    _build_sector_instancer_gn_tree(ng, src_root)

    log.info(
        "sector_instancer: %s -> %d placements (source=%s)",
        stem, len(all_t), src_root.name,
    )
    return inst_obj


def _import_cached_plan_full_items(plan, target_collection, kwargs, context=None, loaded_collection=None, errors=None, level_file=""):
    total_started = time.perf_counter()
    if target_collection is None:
        target_collection = _get_active_collection(context)
    if target_collection is None:
        return 0

    from ..importers import import_entity

    items = [item for item in plan.get("items", []) or [] if isinstance(item, dict)]
    by_id = {
        str(item.get("id", "") or "").strip(): item
        for item in items
        if str(item.get("id", "") or "").strip()
    }
    mode_signature = str(kwargs.get("_layer_import_mode_signature", "") or "").strip()
    if not mode_signature:
        mode_signature = _layer_load_mode_signature(False)
    loaded_by_id = _cached_plan_loaded_item_objects(loaded_collection or target_collection, mode_signature)
    needed_ids = set()
    skipped_loaded = 0
    select_started = time.perf_counter()

    for item in items:
        item_id = str(item.get("id", "") or "").strip()
        kind = str(item.get("kind", "") or "").strip().lower()
        repo_path_value = str(item.get("repo_path", "") or "").strip()
        if not item_id or item_id in loaded_by_id:
            if item_id:
                skipped_loaded += 1
            continue
        if kind in _CACHED_FULL_MESH_ITEM_KINDS:
            if not repo_path_value or not _cached_plan_mesh_enabled(kind, kwargs, item):
                continue
        elif kind in _CACHED_SECTOR_INSTANCER_KINDS:
            if not repo_path_value or not _cached_plan_mesh_enabled(kind, kwargs, item):
                continue
        elif kind in _CACHED_REDCLOTH_ITEM_KINDS:
            if (
                not repo_path_value
                or not _cloth_resource_enabled_for_import(repo_path_value, kwargs, context)
            ):
                continue
        elif kind in _CACHED_FULL_LIGHT_ITEM_KINDS:
            if not _cached_plan_light_enabled(kind, kwargs):
                continue
        else:
            continue

        current = item
        while current is not None:
            current_id = str(current.get("id", "") or "").strip()
            if not current_id or current_id in needed_ids:
                break
            needed_ids.add(current_id)
            parent_id = str(current.get("parent_id", "") or "").strip()
            current = by_id.get(parent_id)
    select_seconds = time.perf_counter() - select_started

    created = dict(loaded_by_id)
    owner_tag = kwargs.get("_layer_import_owner")
    generation_tag = kwargs.get("_layer_import_generation")
    parent_seconds = 0.0
    mesh_seconds = 0.0
    cloth_seconds = 0.0
    light_seconds = 0.0
    parent_count = 0
    mesh_count = 0
    cloth_count = 0
    light_count = 0

    def ensure_parent_empty(item_id):
        nonlocal parent_seconds, parent_count
        item_id = str(item_id or "").strip()
        if not item_id or item_id not in needed_ids:
            return None
        existing = created.get(item_id)
        if existing is not None:
            return existing
        item = by_id.get(item_id)
        if item is None:
            return None
        kind = str(item.get("kind", "") or "").strip().lower()
        if kind in _CACHED_FULL_ITEM_KINDS:
            return None
        parent_started = time.perf_counter()
        parent_obj = ensure_parent_empty(str(item.get("parent_id", "") or "").strip())
        obj = _create_linked_empty(item.get("name", "Entity"), target_collection)
        if obj is None:
            return None
        if parent_obj is not None:
            obj.parent = parent_obj
        _apply_plan_item_transform(obj, item)
        _tag_single_object_for_layer(obj, owner_tag, generation_tag)
        _tag_object_tree_for_plan_item(obj, item_id, mode_signature)
        try:
            obj["witcher_cached_plan_proxy"] = True
            obj["witcher_cached_plan_kind"] = kind
        except Exception:
            pass
        created[item_id] = obj
        parent_count += 1
        parent_seconds += time.perf_counter() - parent_started
        return obj

    if errors is None:
        errors = []
    keep_lod_meshes = bool(kwargs.get("keep_lod_meshes", False))
    keep_proxy_meshes = bool(kwargs.get("keep_proxy_meshes", True))
    imported_count = 0
    for item in items:
        _raise_if_layer_import_cancelled(kwargs)
        item_id = str(item.get("id", "") or "").strip()
        if not item_id or item_id not in needed_ids or item_id in loaded_by_id:
            continue
        kind = str(item.get("kind", "") or "").strip().lower()
        parent_obj = ensure_parent_empty(str(item.get("parent_id", "") or "").strip())

        if kind in _CACHED_SECTOR_INSTANCER_KINDS:
            mesh_started = time.perf_counter()
            root_obj = _import_cached_plan_sector_instancer_item(
                item,
                target_collection,
                parent_obj,
                owner_tag,
                generation_tag,
                item_id,
                mode_signature,
                kwargs,
                errors,
                context=context,
            )
            mesh_seconds += time.perf_counter() - mesh_started
            if root_obj is None:
                continue
            loaded_by_id[item_id] = root_obj
            created[item_id] = root_obj
            _tag_entity_empty_engine_visibility_from_children(parent_obj, kwargs)
            imported_count += 1
            mesh_count += 1
            continue

        if kind in _CACHED_FULL_MESH_ITEM_KINDS:
            mesh = _cached_plan_mesh_from_item(item, kind, context=context)
            if mesh is None:
                continue
            try:
                mesh_started = time.perf_counter()
                mesh_kwargs = dict(kwargs)
                mesh_kwargs.pop("keep_lod_meshes", None)
                mesh_kwargs["_layer_import_plan_item_id"] = item_id
                mesh_kwargs["_layer_import_plan_mode"] = mode_signature
                mesh_kwargs["_cached_plan_fast_static_clone"] = True
                if kind == "collision":
                    root_obj = _import_sector_collision_from_cache(
                        mesh, errors, parent_obj, **mesh_kwargs,
                    )
                else:
                    root_obj = import_single_mesh(
                        mesh,
                        errors,
                        parent_obj,
                        keep_lod_meshes=keep_lod_meshes or (keep_proxy_meshes and _cached_plan_item_is_proxy_mesh(item)),
                        **mesh_kwargs,
                    )
                mesh_seconds += time.perf_counter() - mesh_started
            except Exception as exc:
                log.warning("Problem with cached mesh import %s: %s", mesh.meshName, exc)
                errors.append(f"Problem with cached mesh import {mesh.meshName}: {exc}")
                continue
            if root_obj is None:
                continue
            loaded_by_id[item_id] = root_obj
            created[item_id] = root_obj
            _tag_entity_empty_engine_visibility_from_children(parent_obj, kwargs)
            imported_count += 1
            mesh_count += 1
            continue

        if kind in _CACHED_FULL_LIGHT_ITEM_KINDS:
            if not _cached_plan_light_enabled(kind, kwargs):
                continue
            light_started = time.perf_counter()
            root_obj = _import_cached_plan_light_item(
                item,
                kind,
                target_collection,
                parent_obj,
                owner_tag,
                generation_tag,
                item_id,
                mode_signature,
            )
            light_seconds += time.perf_counter() - light_started
            loaded_by_id[item_id] = root_obj
            created[item_id] = root_obj
            imported_count += 1
            light_count += 1
            continue

        if kind in _CACHED_REDCLOTH_ITEM_KINDS:
            resource = str(item.get("repo_path", "") or "").strip()
            if (
                not resource
                or not _cloth_resource_enabled_for_import(resource, kwargs, context)
            ):
                continue
            try:
                cloth_started = time.perf_counter()
                if _is_redapex_resource(resource):
                    root_obj, _cloth_meshes = import_entity.import_or_reuse_redapex(
                        resource,
                        repo_file(resource),
                        context=context or bpy.context,
                        loadmods=bool(kwargs.get("loadmods", False)),
                        target_collection=target_collection,
                        **_redapex_import_options(kwargs),
                    )
                    cloth_arma = root_obj
                    cloth_grp = None
                else:
                    cloth_arma, cloth_grp, _cloth_meshes = import_entity.import_or_reuse_redcloth(
                        parent_obj,
                        resource,
                        repo_file(resource),
                        import_name="CClothComponent",
                        entity_name=str(item.get("name", "") or Path(resource.replace("/", "\\")).stem),
                        target_collection=target_collection,
                    )
                cloth_seconds += time.perf_counter() - cloth_started
            except Exception as exc:
                resource_label = "redapex" if _is_redapex_resource(resource) else "redcloth"
                log.warning("Problem with cached %s import %s: %s", resource_label, resource, exc)
                errors.append(f"Problem with cached {resource_label} import {resource}: {exc}")
                continue
            if cloth_arma is None:
                continue
            root_obj = cloth_grp if cloth_grp is not None else cloth_arma
            if parent_obj is not None:
                root_obj.parent = parent_obj
                _apply_plan_item_transform_as_child(root_obj, item)
            else:
                _apply_plan_item_transform(root_obj, item)
            _tag_object_tree_for_layer_and_plan(
                root_obj,
                owner_tag,
                generation_tag,
                item_id,
                mode_signature,
            )
            loaded_by_id[item_id] = root_obj
            created[item_id] = root_obj
            imported_count += 1
            cloth_count += 1

    for item_id, obj in list(created.items()):
        item = by_id.get(str(item_id or ""))
        if not isinstance(item, dict):
            continue
        if str(item.get("kind", "") or "").strip().lower() == "entity":
            _tag_entity_empty_engine_visibility_from_children(obj, kwargs)

    total_seconds = time.perf_counter() - total_started
    if total_seconds >= _LAYER_IMPORT_PROFILE_WARN_THRESHOLD:
        _log_layer_import_profile_warning(
            "cached plan full %s total %.3fs (select %.3fs, parents %.3fs/%d, mesh dispatch %.3fs/%d, cloth dispatch %.3fs/%d, light dispatch %.3fs/%d, imported %d, loaded skips %d, source items %d, needed ids %d)",
            level_file or "<cached-plan>",
            total_seconds,
            select_seconds,
            parent_seconds,
            parent_count,
            mesh_seconds,
            mesh_count,
            cloth_seconds,
            cloth_count,
            light_seconds,
            light_count,
            imported_count,
            skipped_loaded,
            len(items),
            len(needed_ids),
        )
    return imported_count


def _filter_cached_plan_items_by_proximity(items, nearby_filter, nearby_stats, item_kinds=None):
    """Cull cached plan items whose world_position is outside the radius.

    Group items and items without world_position are kept only if a
    descendant survives the cull, so parent chains stay connected.

    Sector instancer items are kept whole if any of their packed sector_transforms
    falls within the radius (per-placement culling is intentionally avoided so
    the instancer's identity stays stable across reloads as the camera moves).
    """
    if nearby_filter is None:
        return list(items)

    target_kinds = None
    if item_kinds:
        target_kinds = {str(kind or "").strip().lower() for kind in item_kinds if str(kind or "").strip()}

    by_id = {}
    for item in items:
        item_id = str(item.get("id", "") or "")
        if item_id:
            by_id[item_id] = item

    keep = set()
    filtered_count = 0
    for item in items:
        kind = str(item.get("kind", "") or "").strip().lower()
        if kind == "group":
            continue
        if target_kinds is not None and kind not in target_kinds:
            continue
        if kind == "sector_instancer":
            transforms = item.get("sector_transforms") or []
            any_nearby = False
            for tr in transforms:
                t_pos = tr.get("t") if isinstance(tr, dict) else None
                if t_pos and _position_within_nearby_filter(t_pos, nearby_filter):
                    any_nearby = True
                    break
            if any_nearby:
                keep.add(str(item.get("id", "") or ""))
            elif transforms:
                filtered_count += 1
            continue
        position = item.get("world_position")
        if position is None:
            continue
        if _position_within_nearby_filter(position, nearby_filter):
            keep.add(str(item.get("id", "") or ""))
        else:
            filtered_count += 1

    full_keep = set(keep)
    for kept_id in list(keep):
        current = by_id.get(kept_id)
        while current is not None:
            parent_id = str(current.get("parent_id", "") or "")
            if not parent_id or parent_id in full_keep:
                break
            full_keep.add(parent_id)
            current = by_id.get(parent_id)

    nearby_stats["filtered"] = int(nearby_stats.get("filtered", 0) or 0) + filtered_count
    return [item for item in items if str(item.get("id", "") or "") in full_keep]


def _stable_sector_instancer_id(repo_path, kind, visibility_key=""):
    """Build a deterministic id from (kind, repo_path) so reloads can find the same instancer."""
    import hashlib
    key = f"{str(kind or '').lower()}|{str(repo_path or '').lower().replace('/', chr(92))}|{str(visibility_key or '').lower()}"
    return "_si_" + hashlib.md5(key.encode("utf-8")).hexdigest()[:16]


def _maybe_group_cached_items_into_sector_instancers(items, kwargs):
    """If instanced_sector=True, replace top-level non-proxy mesh and rigid_body items with
    synthetic sector_instancer items grouped by repo_path. Each instancer holds every
    placement for that repo_path so subsequent camera-radius reloads of the same layer
    re-attach to the same instancer instead of duplicating it.
    """
    if not bool(kwargs and kwargs.get("instanced_sector", False)):
        return list(items or [])

    items = list(items or [])
    if not items:
        return items

    grouped = {}  # (kind, repo_path, visibility_key) -> list of items
    survivors = []

    for item in items:
        if not isinstance(item, dict):
            survivors.append(item)
            continue
        kind = str(item.get("kind", "") or "").strip().lower()
        parent_id = str(item.get("parent_id", "") or "").strip()
        repo_path = str(item.get("repo_path", "") or "").strip()
        if parent_id or not repo_path:
            survivors.append(item)
            continue
        is_proxy = _cached_plan_item_is_proxy_mesh(item) if kind in {"mesh", "component_mesh"} else False
        if (kind == "mesh" and not is_proxy) or kind in {"rigid", "rigid_body", "collision"}:
            visibility_key = _sector_visibility_key_for_item(kind, item)
            grouped.setdefault((kind, repo_path, visibility_key), []).append(item)
        else:
            survivors.append(item)

    if not grouped:
        return items

    new_items = list(survivors)
    for (kind, repo_path, visibility_key), grouped_items in grouped.items():
        sector_transforms = []
        for it in grouped_items:
            t_src = it.get("translation") or it.get("local_position") or it.get("world_position") or [0.0, 0.0, 0.0]
            try:
                t = [float(t_src[0]), float(t_src[1]), float(t_src[2])]
            except Exception:
                t = [0.0, 0.0, 0.0]
            m = it.get("matrix")
            if m:
                try:
                    m = [list(r) for r in m]
                except Exception:
                    m = None
            sector_transforms.append({"t": t, "m": m})
        synth = {
            "id": _stable_sector_instancer_id(repo_path, kind, visibility_key),
            "kind": "sector_instancer",
            "name": Path(repo_path.replace("\\", "/")).stem or "Instancer",
            "parent_id": "",
            "repo_path": repo_path,
            "sector_transforms": sector_transforms,
            "_source_kind": kind,
        }
        if visibility_key:
            synth["sector_visibility_key"] = visibility_key
            synth["sector_visible"] = visibility_key != "hidden"
            first_flags = grouped_items[0].get("sector_flags") if grouped_items else None
            if first_flags is not None:
                synth["sector_flags"] = first_flags
        new_items.append(synth)

    return new_items


def _ensure_cached_sector_group_hierarchy(items):
    """Add CSectorData child groups to flat cached sector items.

    Fast scan cache entries record sector meshes/lights as top-level items. Direct
    W2L import and slow resolved plans already expose them under CSectorData, so
    normalize the fast path before creating Blender objects.
    """
    source_items = [item for item in items or [] if isinstance(item, dict)]
    if not source_items:
        return []
    for item in source_items:
        if (
            str(item.get("kind", "") or "").strip().lower() == "group"
            and str(item.get("name", "") or "").strip() == "CSectorData"
        ):
            return list(source_items)

    sector_kind_to_group = {
        "mesh": "_sector_mesh",
        "sector_instancer": "_sector_mesh",
        "rigid": "_sector_rigid",
        "rigid_body": "_sector_rigid",
        "collision": "_sector_collision",
        "point_light": "_sector_point_light",
        "spot_light": "_sector_spot_light",
    }
    group_names = {
        "_sector_collision": "Collision",
        "_sector_rigid": "Rigid",
        "_sector_mesh": "Mesh",
        "_sector_point_light": "PointLight",
        "_sector_spot_light": "SpotLight",
    }
    needed_groups = set()
    normalized = []
    for item in source_items:
        copied = dict(item)
        kind = str(copied.get("kind", "") or "").strip().lower()
        source_kind = str(copied.get("_source_kind", "") or "").strip().lower()
        parent_id = str(copied.get("parent_id", "") or "").strip()
        group_id = sector_kind_to_group.get(source_kind if kind == "sector_instancer" else kind)
        if group_id and not parent_id:
            copied["parent_id"] = group_id
            needed_groups.add(group_id)
        normalized.append(copied)

    if not needed_groups:
        return normalized

    groups = [
        {
            "id": "_sector_root",
            "kind": "group",
            "name": "CSectorData",
            "parent_id": "",
            "repo_path": "",
            "transform": None,
            "matrix": None,
            "translation": None,
            "world_position": None,
        }
    ]
    for group_id in ("_sector_collision", "_sector_rigid", "_sector_mesh", "_sector_point_light", "_sector_spot_light"):
        if group_id not in needed_groups:
            continue
        groups.append(
            {
                "id": group_id,
                "kind": "group",
                "name": group_names[group_id],
                "parent_id": "_sector_root",
                "repo_path": "",
                "transform": None,
                "matrix": None,
                "translation": None,
                "world_position": None,
            }
        )
    return groups + normalized


def _resolve_component_import_plan(
    plan,
    component,
    parent_id,
    parent_position=None,
    *,
    mesh_fbx_uncook_path=None,
    mesh_uncook_path=None,
):
    component_name = getattr(component, "name", getattr(component, "Type", "Component"))
    world_position = _chunk_world_position(component, parent_position)
    transform_prop = None
    try:
        transform_prop = component.GetVariableByName('transform')
    except Exception:
        transform_prop = None
    transform = getattr(transform_prop, "EngineTransform", None) if transform_prop else None

    if component_name in {"CMeshComponent", "CStaticMeshComponent"}:
        try:
            mesh = _new_mesh_path(
                fbx_uncook_path=mesh_fbx_uncook_path,
                uncook_path=mesh_uncook_path,
            ).static_from_chunk(component)
        except Exception:
            log.exception("Problem resolving mesh component %s", component_name)
            return None
        mesh_label = str(getattr(mesh, "name", "") or "").strip()
        if not mesh_label or mesh_label == "Mesh Item":
            mesh_label = Path(mesh.meshName).stem or component_name
        drawable_flags = _component_drawable_flags(component)
        engine_visible = _drawable_flags_visible_from_value(drawable_flags, default=True)
        return _add_level_import_plan_item(
            plan,
            "component_mesh",
            f"{component_name} {mesh_label}",
            parent_id=parent_id,
            repo_path=mesh.meshName,
            transform=getattr(mesh, "transform", None),
            matrix=getattr(mesh, "matrix", None),
            translation=getattr(mesh, "translation", None),
            world_position=_mesh_world_position(mesh, parent_position),
            is_proxy_mesh=_path_indicates_proxy_mesh(mesh.meshName, component_name),
            drawable_flags=drawable_flags,
            engine_visible=engine_visible,
        )

    if component_name == "CPointLightComponent":
        return _add_level_import_plan_item(
            plan,
            "component_point_light",
            "PointLightComponent",
            parent_id=parent_id,
            transform=transform,
            world_position=world_position,
        )

    if component_name == "CSpotLightComponent":
        return _add_level_import_plan_item(
            plan,
            "component_spot_light",
            "SpotLightComponent",
            parent_id=parent_id,
            transform=transform,
            world_position=world_position,
        )

    return None


def _resolve_gameplay_entity_import_plan(
    plan,
    ENTITY_OBJECT,
    *,
    parent_id="",
    parent_position=None,
    keep_lod_meshes=False,
    mesh_fbx_uncook_path=None,
    mesh_uncook_path=None,
    **kwargs,
):
    try:
        mesh_list, cloth_list = getDataBufferMesh(
            ENTITY_OBJECT,
            mesh_fbx_uncook_path=mesh_fbx_uncook_path,
            mesh_uncook_path=mesh_uncook_path,
        )
    except Exception as exc:
        raise exc

    nearby_filter = _get_nearby_import_filter(kwargs)
    nearby_stats = _get_nearby_import_stats(kwargs)
    entity_world_position = _entity_world_position(ENTITY_OBJECT, parent_position)
    anchor_position = entity_world_position or parent_position

    supported_component_names = {
        "CMeshComponent",
        "CStaticMeshComponent",
        "CPointLightComponent",
        "CSpotLightComponent",
    }

    filtered_mesh_list = []
    for mesh in mesh_list:
        if not _position_within_nearby_filter(_mesh_world_position(mesh, anchor_position), nearby_filter):
            _note_nearby_filter_skip(nearby_stats)
            continue
        filtered_mesh_list.append(mesh)
    mesh_list = filtered_mesh_list

    filtered_cloth_list = []
    for chunk in cloth_list:
        if not _position_within_nearby_filter(_chunk_world_position(chunk, anchor_position), nearby_filter):
            _note_nearby_filter_skip(nearby_stats)
            continue
        filtered_cloth_list.append(chunk)
    cloth_list = filtered_cloth_list

    eligible_components = []
    for component in (getattr(ENTITY_OBJECT, "Components", None) or []):
        component_name = getattr(component, "name", getattr(component, "Type", ""))
        if component_name not in supported_component_names:
            continue
        if not _position_within_nearby_filter(_chunk_world_position(component, anchor_position), nearby_filter):
            _note_nearby_filter_skip(nearby_stats)
            continue
        eligible_components.append(component)

    template = getattr(ENTITY_OBJECT, "template", None)
    has_template_content = bool(
        template is not None
        and (getattr(template, "includes", None) or getattr(template, "Entities", None))
    )
    if not mesh_list and not cloth_list and not eligible_components and not has_template_content:
        return None

    entity_id = _add_level_import_plan_item(
        plan,
        "entity",
        getattr(ENTITY_OBJECT, "name", "") or getattr(ENTITY_OBJECT, "type", "") or "Entity",
        parent_id=parent_id,
        repo_path=getattr(ENTITY_OBJECT, "templatePath", "") if getattr(ENTITY_OBJECT, "isCreatedFromTemplate", False) else "",
        transform=getattr(ENTITY_OBJECT, "transform", None),
        world_position=entity_world_position,
    )
    items_before_children = len(plan["items"])

    for mesh in mesh_list:
        is_proxy_mesh = _path_indicates_proxy_mesh(mesh.meshName, "")
        _add_level_import_plan_item(
            plan,
            "mesh",
            Path(mesh.meshName).stem or "Mesh",
            parent_id=entity_id,
            repo_path=mesh.meshName,
            transform=getattr(mesh, "transform", None),
            matrix=getattr(mesh, "matrix", None),
            translation=getattr(mesh, "translation", None),
            world_position=_mesh_world_position(mesh, anchor_position),
            is_proxy_mesh=is_proxy_mesh,
        )

    for chunk in cloth_list:
        cloth_name = getattr(ENTITY_OBJECT, "name", "") or "Cloth"
        cloth_resource = ""
        try:
            name_var = chunk.GetVariableByName('name')
            cloth_name = str(getattr(getattr(name_var, "String", None), "String", "") or "").strip() or cloth_name
        except Exception:
            pass
        try:
            resource_var = chunk.GetVariableByName('resource')
            handles = getattr(resource_var, "Handles", None) or []
            if handles:
                cloth_resource = str(getattr(handles[0], "DepotPath", "") or "").strip()
        except Exception:
            cloth_resource = ""
        transform_prop = None
        try:
            transform_prop = chunk.GetVariableByName('transform')
        except Exception:
            transform_prop = None
        _add_level_import_plan_item(
            plan,
            "cloth",
            Path(cloth_resource).stem or cloth_name or "Cloth",
            parent_id=entity_id,
            repo_path=cloth_resource,
            transform=getattr(transform_prop, "EngineTransform", None) if transform_prop else None,
            world_position=_chunk_world_position(chunk, anchor_position),
        )

    for component in eligible_components:
        _resolve_component_import_plan(
            plan,
            component,
            entity_id,
            anchor_position,
            mesh_fbx_uncook_path=mesh_fbx_uncook_path,
            mesh_uncook_path=mesh_uncook_path,
        )

    if getattr(ENTITY_OBJECT, "isCreatedFromTemplate", False):
        if '(CDoor)' in getattr(ENTITY_OBJECT, "name", ""):
            if _position_within_nearby_filter(entity_world_position, nearby_filter):
                template_path = getattr(getattr(ENTITY_OBJECT, "template", None), "layerNode", "")
                _add_level_import_plan_item(
                    plan,
                    "entity_template",
                    Path(template_path).stem or "Template",
                    parent_id=entity_id,
                    repo_path=template_path,
                    world_position=entity_world_position,
                )
            else:
                _note_nearby_filter_skip(nearby_stats)
        else:
            include_root_id = ""
            include_items_before = len(plan["items"])
            if template and getattr(template, "includes", None):
                include_root_id = _add_level_import_plan_item(
                    plan,
                    "group",
                    "INCLUDES",
                    parent_id=entity_id,
                    world_position=anchor_position,
                )
                for INCLUDE_OBJECT in template.includes:
                    for inc_entity in getattr(INCLUDE_OBJECT, "Entities", []) or []:
                        if inc_entity.type in Entity_Type_List:
                            _resolve_gameplay_entity_import_plan(
                                plan,
                                inc_entity,
                                parent_id=include_root_id,
                                parent_position=anchor_position,
                                keep_lod_meshes=keep_lod_meshes,
                                mesh_fbx_uncook_path=mesh_fbx_uncook_path,
                                mesh_uncook_path=mesh_uncook_path,
                                **kwargs,
                            )
                if len(plan["items"]) == include_items_before + 1:
                    _remove_level_import_plan_item(plan, include_root_id)
            for entity in getattr(template, "Entities", []) or []:
                _resolve_gameplay_entity_import_plan(
                    plan,
                    entity,
                    parent_id=entity_id,
                    parent_position=anchor_position,
                    keep_lod_meshes=keep_lod_meshes,
                    mesh_fbx_uncook_path=mesh_fbx_uncook_path,
                    mesh_uncook_path=mesh_uncook_path,
                    **kwargs,
                )

    if len(plan["items"]) == items_before_children:
        _remove_level_import_plan_item(plan, entity_id)
        return None
    return entity_id


def resolve_level_import_plan(levelData, context = None, keep_lod_meshes:bool = False, **kwargs):
    do_import_Mesh = kwargs.get('do_import_Mesh', True)
    do_import_Collision = kwargs.get('do_import_Collision', True)
    do_import_RigidBody = kwargs.get('do_import_RigidBody', True)
    do_import_PointLight = kwargs.get('do_import_PointLight', True)
    do_import_SpotLight = kwargs.get('do_import_SpotLight', True)
    do_import_Entity = kwargs.get('do_import_Entity', True)
    do_import_ProxyMesh = kwargs.get('do_import_ProxyMesh', False)
    proxy_filter_active = _proxy_mesh_filter_active(kwargs)
    do_enable_name_filter = kwargs.get('do_enable_name_filter', False)
    do_name_filter_regex = kwargs.get('do_name_filter_regex', '')

    nearby_filter = _get_nearby_import_filter(kwargs)
    nearby_stats = _get_nearby_import_stats(kwargs)
    nearby_stats["filtered"] = 0
    plan = _new_level_import_plan()
    mesh_fbx_uncook_path = kwargs.get("_mesh_fbx_uncook_path")
    mesh_uncook_path = kwargs.get("_mesh_uncook_path")

    if levelData.Foliage and do_import_Mesh:
        for treeCollection in (levelData.Foliage.Trees.elements if hasattr(levelData.Foliage, 'Trees') else []):
            treeFilePath = treeCollection.TreeType.DepotPath
            for treeTransform in treeCollection.TreeCollection.elements:
                if not _position_within_nearby_filter(
                    _extract_transform_position(treeTransform),
                    nearby_filter,
                ):
                    _note_nearby_filter_skip(nearby_stats)
                    continue
                _add_level_import_plan_item(
                    plan,
                    "foliage",
                    Path(treeFilePath).stem or "Foliage",
                    repo_path=treeFilePath,
                    transform=treeTransform,
                    world_position=_extract_transform_position(treeTransform),
                )
        for treeCollection in (levelData.Foliage.Grasses.elements if hasattr(levelData.Foliage, 'Grasses') else []):
            treeFilePath = treeCollection.TreeType.DepotPath
            for treeTransform in treeCollection.TreeCollection.elements:
                if not _position_within_nearby_filter(
                    _extract_transform_position(treeTransform),
                    nearby_filter,
                ):
                    _note_nearby_filter_skip(nearby_stats)
                    continue
                _add_level_import_plan_item(
                    plan,
                    "grass",
                    Path(treeFilePath).stem or "Grass",
                    repo_path=treeFilePath,
                    transform=treeTransform,
                    world_position=_extract_transform_position(treeTransform),
                )

    mesh_list = get_CSectorData(
        levelData,
        mesh_fbx_uncook_path=mesh_fbx_uncook_path,
        mesh_uncook_path=mesh_uncook_path,
    )
    if mesh_list:
        mesh_candidates = []
        for mesh in mesh_list:
            if not _position_within_nearby_filter(_mesh_world_position(mesh), nearby_filter):
                _note_nearby_filter_skip(nearby_stats)
                continue
            if not (re.search(do_name_filter_regex, mesh.fileName()) if do_enable_name_filter else True):
                continue
            mesh_candidates.append(mesh)

        if mesh_candidates:
            sector_root_id = _add_level_import_plan_item(plan, "group", "CSectorData")
            collision_root_id = _add_level_import_plan_item(plan, "group", "Collision", parent_id=sector_root_id)
            rigid_root_id = _add_level_import_plan_item(plan, "group", "Rigid", parent_id=sector_root_id)
            mesh_root_id = _add_level_import_plan_item(plan, "group", "Mesh", parent_id=sector_root_id)
            point_light_root_id = _add_level_import_plan_item(plan, "group", "PointLight", parent_id=sector_root_id)
            spot_light_root_id = _add_level_import_plan_item(plan, "group", "SpotLight", parent_id=sector_root_id)

            instanced_sector = bool(kwargs.get("instanced_sector", False))
            # Per-repo_path/visibility transform accumulator for GN instancing.
            _sector_instancer_groups: dict = {}  # (kind, repo_path, visibility_key) -> [{"t": [...], "m": [...]}, ...]

            for mesh in mesh_candidates:
                is_proxy_mesh = bool(getattr(mesh, "is_proxy_mesh", False)) or _path_indicates_proxy_mesh(getattr(mesh, "meshName", ""), "")
                if mesh.BlockDataObjectType == Enums.BlockDataObjectType.Mesh and (
                    (is_proxy_mesh and proxy_filter_active and do_import_ProxyMesh)
                    or ((not is_proxy_mesh or not proxy_filter_active) and do_import_Mesh)
                ):
                    if instanced_sector and not is_proxy_mesh:
                        # Accumulate for GN instancer instead of individual object
                        rp = str(mesh.meshName or "").strip()
                        if rp:
                            tv = _extract_vector_position(getattr(mesh, "translation", None))
                            t = list(tv) if tv else [0.0, 0.0, 0.0]
                            m = _copy_matrix_array(getattr(mesh, "matrix", None))
                            m = [list(r) for r in m] if m else None
                            vis_key = _sector_visibility_key_from_flags(getattr(mesh, "sector_flags", None), default=True)
                            _sector_instancer_groups.setdefault(("mesh", rp, vis_key), []).append({"t": t, "m": m})
                    else:
                        _add_level_import_plan_item(
                            plan,
                            "mesh",
                            Path(mesh.meshName).stem or "Mesh",
                            parent_id=mesh_root_id,
                            repo_path=mesh.meshName,
                            transform=getattr(mesh, "transform", None),
                            matrix=getattr(mesh, "matrix", None),
                            translation=getattr(mesh, "translation", None),
                            world_position=_mesh_world_position(mesh),
                            is_proxy_mesh=is_proxy_mesh,
                            proxy_role=getattr(mesh, "proxy_role", ""),
                            sector_flags=getattr(mesh, "sector_flags", None),
                        )
                elif mesh.BlockDataObjectType == Enums.BlockDataObjectType.Collision and do_import_Collision:
                    if instanced_sector:
                        rp = str(mesh.meshName or "").strip()
                        if rp:
                            tv = _extract_vector_position(getattr(mesh, "translation", None))
                            t = list(tv) if tv else [0.0, 0.0, 0.0]
                            m = _copy_matrix_array(getattr(mesh, "matrix", None))
                            m = [list(r) for r in m] if m else None
                            _sector_instancer_groups.setdefault(("collision", rp, ""), []).append({"t": t, "m": m})
                    else:
                        _add_level_import_plan_item(
                            plan,
                            "collision",
                            Path(mesh.meshName).stem or "Collision",
                            parent_id=collision_root_id,
                            repo_path=mesh.meshName,
                            transform=getattr(mesh, "transform", None),
                            matrix=getattr(mesh, "matrix", None),
                            translation=getattr(mesh, "translation", None),
                            world_position=_mesh_world_position(mesh),
                        )
                elif mesh.BlockDataObjectType == Enums.BlockDataObjectType.RigidBody and do_import_RigidBody:
                    if instanced_sector:
                        rp = str(mesh.meshName or "").strip()
                        if rp:
                            tv = _extract_vector_position(getattr(mesh, "translation", None))
                            t = list(tv) if tv else [0.0, 0.0, 0.0]
                            m = _copy_matrix_array(getattr(mesh, "matrix", None))
                            m = [list(r) for r in m] if m else None
                            vis_key = _sector_visibility_key_from_flags(getattr(mesh, "sector_flags", None), default=True)
                            _sector_instancer_groups.setdefault(("rigid", rp, vis_key), []).append({"t": t, "m": m})
                    else:
                        _add_level_import_plan_item(
                            plan,
                            "rigid_body",
                            Path(mesh.meshName).stem or "RigidBody",
                            parent_id=rigid_root_id,
                            repo_path=mesh.meshName,
                            transform=getattr(mesh, "transform", None),
                            matrix=getattr(mesh, "matrix", None),
                            translation=getattr(mesh, "translation", None),
                            world_position=_mesh_world_position(mesh),
                            sector_flags=getattr(mesh, "sector_flags", None),
                        )
                elif mesh.BlockDataObjectType == Enums.BlockDataObjectType.PointLight and do_import_PointLight:
                    _add_level_import_plan_item(
                        plan,
                        "point_light",
                        "PointLight",
                        parent_id=point_light_root_id,
                        transform=getattr(mesh, "transform", None),
                        matrix=getattr(mesh, "matrix", None),
                        translation=getattr(mesh, "translation", None),
                        world_position=_mesh_world_position(mesh),
                    )
                elif mesh.BlockDataObjectType == Enums.BlockDataObjectType.SpotLight and do_import_SpotLight:
                    _add_level_import_plan_item(
                        plan,
                        "spot_light",
                        "SpotLight",
                        parent_id=spot_light_root_id,
                        transform=getattr(mesh, "transform", None),
                        matrix=getattr(mesh, "matrix", None),
                        translation=getattr(mesh, "translation", None),
                        world_position=_mesh_world_position(mesh),
                    )

            # Emit one sector_instancer item per unique repo_path
            instancer_parent_by_kind = {
                "mesh": mesh_root_id,
                "rigid": rigid_root_id,
                "rigid_body": rigid_root_id,
                "collision": collision_root_id,
            }
            for (si_kind, rp, vis_key), transforms in _sector_instancer_groups.items():
                _add_level_import_plan_item(
                    plan,
                    "sector_instancer",
                    Path(rp.replace("\\", "/")).stem or "Instancer",
                    parent_id=instancer_parent_by_kind.get(si_kind, mesh_root_id),
                    repo_path=rp,
                    sector_transforms=transforms,
                    sector_visibility_key=vis_key,
                    sector_visible=(vis_key != "hidden"),
                    source_kind=si_kind,
                )

    if do_import_Entity:
        for INCLUDE_OBJECT in levelData.includes:
            for ENTITY_OBJECT in INCLUDE_OBJECT.Entities:
                if ENTITY_OBJECT.type in Entity_Type_List:
                    _resolve_gameplay_entity_import_plan(
                        plan,
                        ENTITY_OBJECT,
                        keep_lod_meshes=keep_lod_meshes,
                        mesh_fbx_uncook_path=mesh_fbx_uncook_path,
                        mesh_uncook_path=mesh_uncook_path,
                        **kwargs,
                    )

        for ENTITY_OBJECT in levelData.Entities:
            if re.search(do_name_filter_regex, ENTITY_OBJECT.name) if do_enable_name_filter else True:
                if ENTITY_OBJECT.type in Entity_Type_List:
                    _resolve_gameplay_entity_import_plan(
                        plan,
                        ENTITY_OBJECT,
                        keep_lod_meshes=keep_lod_meshes,
                        mesh_fbx_uncook_path=mesh_fbx_uncook_path,
                        mesh_uncook_path=mesh_uncook_path,
                        **kwargs,
                    )

    plan["stats"]["filtered"] = int(nearby_stats.get("filtered", 0) or 0)
    return plan


_REPO_DUPLICATE_CACHE = {
    "scene_key": None,
    "object_count": -1,
    "roots": {},
}


def _invalidate_duplicate_root_index():
    _REPO_DUPLICATE_CACHE["scene_key"] = None
    _REPO_DUPLICATE_CACHE["object_count"] = -1
    _REPO_DUPLICATE_CACHE["roots"] = {}


def _scene_identity(scene):
    if scene is None:
        return None
    try:
        return int(scene.as_pointer())
    except Exception:
        return id(scene)


def _object_identity(obj):
    if obj is None:
        return None
    try:
        return int(obj.as_pointer())
    except Exception:
        return id(obj)


def _is_live_blender_object(obj):
    if obj is None:
        return False
    try:
        return bool(obj.name)
    except ReferenceError:
        return False
    except Exception:
        return False


def _get_scene(context=None):
    ctx = context or bpy.context
    return getattr(ctx, "scene", None)


def _get_active_collection(context=None):
    ctx = context or bpy.context
    collection = getattr(ctx, "collection", None)
    if collection is not None:
        return collection
    view_layer = getattr(ctx, "view_layer", None)
    active_layer_collection = getattr(view_layer, "active_layer_collection", None) if view_layer else None
    collection = getattr(active_layer_collection, "collection", None)
    if collection is not None:
        return collection
    scene = _get_scene(ctx)
    return getattr(scene, "collection", None)


def _normalize_repo_path(path_value):
    return str(path_value or "").replace("/", "\\").strip()


def _normalize_level_repo_path(level_path, context=None):
    norm_path = _normalize_repo_path(level_path)
    uncook_root = _normalize_repo_path(get_uncook_path(context)).rstrip("\\")
    if uncook_root:
        prefix = uncook_root + "\\"
        if norm_path.lower().startswith(prefix.lower()):
            return norm_path[len(prefix):]
    return norm_path


def _get_layer_import_owner_tag(level_path, context=None):
    return _normalize_level_repo_path(level_path, context) or _normalize_repo_path(level_path)


def _iter_object_tree(root_obj):
    if root_obj is None:
        return
    stack = [root_obj]
    seen = set()
    while stack:
        obj = stack.pop()
        obj_id = _object_identity(obj)
        if obj_id in seen:
            continue
        seen.add(obj_id)
        yield obj
        stack.extend(list(getattr(obj, "children", []) or []))


def _tag_object_tree_for_layer_and_plan(
    root_obj,
    owner_tag=None,
    generation_tag=None,
    item_id="",
    mode_signature="",
):
    owner_tag = str(owner_tag or "").strip()
    generation_tag = str(generation_tag or "").strip()
    item_id = str(item_id or "").strip()
    mode_signature = str(mode_signature or "").strip()
    if root_obj is None or (not owner_tag and not generation_tag and not item_id):
        return
    for obj in _iter_object_tree(root_obj):
        try:
            if owner_tag:
                obj[_LAYER_IMPORT_OWNER_PROP] = owner_tag
            if generation_tag:
                obj[_LAYER_IMPORT_GENERATION_PROP] = generation_tag
            if item_id:
                obj[_LAYER_IMPORT_PLAN_ITEM_PROP] = item_id
                if mode_signature:
                    obj[_LAYER_IMPORT_PLAN_MODE_PROP] = mode_signature
        except Exception:
            continue


def _tag_object_tree_for_layer(root_obj, owner_tag=None, generation_tag=None):
    _tag_object_tree_for_layer_and_plan(root_obj, owner_tag, generation_tag)


def _tag_object_tree_as_proxy_mesh(root_obj):
    if root_obj is None:
        return
    for obj in _iter_object_tree(root_obj):
        try:
            obj["witcher_layer_proxy_mesh"] = True
            obj["witcher_layer_visibility_kind"] = "proxy_mesh"
        except Exception:
            continue


def _clear_object_tree_proxy_mesh_tags(root_obj):
    if root_obj is None:
        return
    for obj in _iter_object_tree(root_obj):
        try:
            if "witcher_layer_proxy_mesh" in obj:
                del obj["witcher_layer_proxy_mesh"]
            if str(obj.get("witcher_layer_visibility_kind", "") or "").strip().lower() == "proxy_mesh":
                del obj["witcher_layer_visibility_kind"]
            if "witcher_sector_flags" in obj:
                del obj["witcher_sector_flags"]
            if "witcher_layer_engine_visible" in obj:
                del obj["witcher_layer_engine_visible"]
            obj.hide_viewport = False
            obj.hide_render = False
        except Exception:
            continue


def _tag_object_tree_engine_visibility(root_obj, visible, kwargs=None, *, drawable_flags=None, sector_flags=None):
    if root_obj is None:
        return
    visible = bool(visible)
    drawable_flags_text = _drawable_flags_display_value(drawable_flags) if drawable_flags is not None else None
    for obj in _iter_object_tree(root_obj):
        try:
            obj["witcher_layer_engine_visible"] = visible
            if sector_flags is not None:
                obj["witcher_sector_flags"] = int(sector_flags)
            if drawable_flags_text is not None:
                obj["witcher_drawableFlags"] = drawable_flags_text
                obj["witcher_redkit_drawableFlags"] = drawable_flags_text
                obj["witcher_drawableFlags_has_DF_IsVisible"] = visible
            if not visible and _hide_engine_hidden_meshes_enabled(kwargs):
                obj.hide_viewport = True
                obj.hide_render = True
            elif visible and _hide_engine_hidden_meshes_enabled(kwargs):
                obj.hide_viewport = False
                obj.hide_render = False
        except Exception:
            continue


def _capture_previous_layer_object_ids(collection, owner_tag, fallback_to_all=False):
    if collection is None:
        return set()
    owner_tag = str(owner_tag or "").strip()
    tagged_ids = set()
    all_ids = set()
    for obj in list(getattr(collection, "all_objects", []) or []):
        obj_id = _object_identity(obj)
        if obj_id is None:
            continue
        all_ids.add(obj_id)
        try:
            if owner_tag and str(obj.get(_LAYER_IMPORT_OWNER_PROP, "") or "").strip() == owner_tag:
                tagged_ids.add(obj_id)
        except Exception:
            continue
    if tagged_ids:
        return tagged_ids
    if fallback_to_all:
        return all_ids
    return set()


def _object_parent_depth(obj):
    depth = 0
    current = getattr(obj, "parent", None)
    seen = set()
    while current is not None:
        current_id = _object_identity(current)
        if current_id in seen:
            break
        seen.add(current_id)
        depth += 1
        current = getattr(current, "parent", None)
    return depth


def _cleanup_captured_layer_objects(collection, object_ids):
    if collection is None or not object_ids:
        return 0
    captured_ids = {int(obj_id) for obj_id in object_ids if obj_id is not None}
    objects_to_remove = []
    for obj in list(getattr(collection, "all_objects", []) or []):
        obj_id = _object_identity(obj)
        if obj_id in captured_ids:
            objects_to_remove.append(obj)
    objects_to_remove.sort(key=_object_parent_depth, reverse=True)
    removed_count = 0
    for obj in objects_to_remove:
        if obj is None or getattr(obj, "name", None) not in bpy.data.objects:
            continue
        try:
            bpy.data.objects.remove(obj, do_unlink=True)
            removed_count += 1
        except Exception:
            continue
    return removed_count


def _ensure_layer_reload_tracking(collection, level_file, context, nearby_filter, kwargs):
    owner_tag = str(kwargs.get("_layer_import_owner") or _get_layer_import_owner_tag(level_file, context)).strip()
    if owner_tag:
        kwargs["_layer_import_owner"] = owner_tag
    if "_layer_import_generation" not in kwargs:
        kwargs["_layer_import_generation"] = f"{owner_tag}|{time.time_ns()}"
    if "_layer_import_previous_ids" in kwargs:
        return
    state = str(collection.get("witcher_layer_import_state", "") or "").strip().lower() if collection is not None else ""
    cleanup_existing = (nearby_filter is not None or state.startswith("proxy_")) and not bool(kwargs.get("_layer_import_incremental"))
    if not cleanup_existing or collection is None:
        kwargs["_layer_import_previous_ids"] = set()
        return
    fallback_to_all = state in {"partial", "failed", "proxy_partial", "proxy_failed"}
    kwargs["_layer_import_previous_ids"] = _capture_previous_layer_object_ids(
        collection,
        owner_tag,
        fallback_to_all=fallback_to_all,
    )


def _finalize_layer_reload_cleanup(collection, kwargs):
    previous_ids = kwargs.get("_layer_import_previous_ids")
    if not previous_ids:
        return 0
    removed_count = _cleanup_captured_layer_objects(collection, previous_ids)
    if removed_count:
        _invalidate_duplicate_root_index()
    kwargs["_layer_import_previous_ids"] = set()
    return removed_count


def _find_level_collection(level_path, context=None):
    level_repo_path = _normalize_level_repo_path(level_path, context)
    level_abs_path = _normalize_repo_path(level_path)
    for collection in bpy.data.collections:
        stored_repo_path = _normalize_level_repo_path(collection.get("level_path", ""), context)
        stored_abs_path = _normalize_repo_path(collection.get("level_abs_path", ""))
        if stored_abs_path and stored_abs_path.lower() == level_abs_path.lower():
            return collection
        if stored_repo_path and stored_repo_path.lower() == level_repo_path.lower():
            return collection
    return None


def _ensure_level_collection(level_path, context=None):
    collection = _find_level_collection(level_path, context)
    level_repo_path = _normalize_level_repo_path(level_path, context)
    level_abs_path = _normalize_repo_path(level_path)
    if collection is None:
        level_name = os.path.basename(level_repo_path or level_abs_path) or "Level"
        collection = bpy.data.collections.new(level_name)
        scene = _get_scene(context)
        if scene is not None:
            scene.collection.children.link(collection)
    collection["level_path"] = level_repo_path
    collection["level_abs_path"] = level_abs_path
    return collection


def _activate_collection(context, collection):
    if collection is None:
        return False
    ctx = context or bpy.context
    view_layer = getattr(ctx, "view_layer", None)
    if view_layer is None:
        return False
    active_layer_collection = getattr(view_layer, "active_layer_collection", None)
    if getattr(active_layer_collection, "collection", None) == collection:
        return True
    layer_collection = recurLayerCollection(getattr(view_layer, "layer_collection", None), collection)
    if layer_collection is None:
        return False
    view_layer.active_layer_collection = layer_collection
    return True

def import_light(mesh, parent_transform = False):
    block = mesh.block
    light_data = block.packedObject
    if block.packedObjectType == Enums.BlockDataObjectType.PointLight:
        bpy.ops.object.light_add(type='POINT', radius=1, align='WORLD', location=(0, 0, 0), scale=(1, 1, 1))
        light_obj = bpy.context.selected_objects[:][0]
        light_obj.data.energy = light_data.brightness * 10
        light_obj.data.color[0] = light_data.color.Red/255
        light_obj.data.color[1] = light_data.color.Green/255
        light_obj.data.color[2] = light_data.color.Blue/255
        # do some custom val? #light_obj.data.color[3] = color.Value/255
        light_obj.data.shadow_soft_size = light_data.radius/255
        #set_blender_object_transform(light_obj, component.GetVariableByName('transform').EngineTransform)
        
    elif block.packedObjectType == Enums.BlockDataObjectType.SpotLight:
        bpy.ops.object.light_add(type='SPOT', radius=1, align='WORLD', location=(0, 0, 0), scale=(1, 1, 1))
        light_obj = bpy.context.selected_objects[:][0]
        light_obj.data.energy = light_data.brightness * 3
        light_obj.data.color[0] = light_data.color.Red/255
        light_obj.data.color[1] = light_data.color.Green/255
        light_obj.data.color[2] = light_data.color.Blue/255
        light_obj.data.shadow_soft_size = light_data.radius/255

        #light_obj.data.spot_blend = component.GetVariableByName('innerAngle').Value
        light_obj.data.spot_blend = 0
        light_obj.data.spot_size = light_data.outerAngle
        #light_obj.data.spot_size = component.GetVariableByName('softness').Value



    obj = light_obj
    if parent_transform:
        obj.parent = parent_transform

    if mesh.transform:
        obj.rotation_euler = (0,0,0)
        x, y, z = (
            radians(_transform_real(mesh.transform, "Yaw", 0.0)),
            radians(_transform_real(mesh.transform, "Pitch", 0.0)),
            radians(_transform_real(mesh.transform, "Roll", 0.0)),
        )
        orders =  ['XYZ', 'XZY', 'YXZ', 'YZX', 'ZXY', 'ZYX']
        mat = Euler((x, y, z), orders[2]).to_matrix().to_4x4()

        obj.matrix_world @= mat
        obj.location[0] = _transform_real(mesh.transform, "X", 0.0)
        obj.location[1] = _transform_real(mesh.transform, "Y", 0.0)
        obj.location[2] = _transform_real(mesh.transform, "Z", 0.0)

        if isinstance(mesh.transform, dict) or hasattr(mesh.transform, "Scale_x"):
            obj.scale[0] = _transform_real(mesh.transform, "Scale_x", 1.0)
            obj.scale[1] = _transform_real(mesh.transform, "Scale_y", 1.0)
            obj.scale[2] = _transform_real(mesh.transform, "Scale_z", 1.0)

    if mesh.matrix:
        try:
            log.info(obj.name)
            mat = Matrix()
            #log.info(mat)
            obj.matrix_world = obj.matrix_world @ mat
        except Exception:
            error_message = "ERROR MESH IMPORTER: Can't import: " + mesh.fbxPath()
            log.info(error_message)
    if mesh.translation:
        translation = _extract_vector_position(mesh.translation)
        if translation is not None:
            obj.location[0] = translation[0]
            obj.location[1] = translation[1]
            obj.location[2] = translation[2]
        
    if block.packedObjectType == Enums.BlockDataObjectType.SpotLight:
        # 90 to X in every spotlight
        rotation_euler = light_obj.rotation_euler
        rotation_euler.x += 1.5708  # 90 degrees in radians
        light_obj.rotation_euler = rotation_euler

#global repo_lookup_list

# import cProfile
# import pstats

def loadLevel(levelData, context = None, keep_lod_meshes:bool = False, **kwargs):
    #! profiler = cProfile.Profile()
    #! profiler.enable()

    target_collection = kwargs.pop("_level_target_collection", None)
    #keep_empty_lods = kwargs.get('keep_empty_lods', False)
    #keep_proxy_meshes = kwargs.get('keep_proxy_meshes', False)

    do_import_Mesh = kwargs.get('do_import_Mesh', True)
    do_import_Collision = kwargs.get('do_import_Collision', True)
    do_import_RigidBody = kwargs.get('do_import_RigidBody', True)
    do_import_PointLight = kwargs.get('do_import_PointLight', True)
    do_import_SpotLight = kwargs.get('do_import_SpotLight', True)
    do_import_Entity = kwargs.get('do_import_Entity', True)
    do_import_ProxyMesh = kwargs.get('do_import_ProxyMesh', False)
    proxy_filter_active = _proxy_mesh_filter_active(kwargs)
    do_enable_name_filter = kwargs.get('do_enable_name_filter', False)
    do_name_filter_regex = kwargs.get('do_name_filter_regex', '')
    dev_empty_only = bool(kwargs.get("_dev_empty_only", False))
    kwargs["_layer_import_profile"] = _new_layer_import_profile()

    if context == None:
        context = bpy.context
    nearby_filter = _get_nearby_import_filter(kwargs)
    nearby_stats = _get_nearby_import_stats(kwargs)
    # global repo_lookup_list
    # repo_lookup_list = defaultdict(list)
    # scene = bpy.context.scene
    # for o in scene.objects:
    #     if o.type != 'EMPTY':
    #         continue
    #     if len(o.name) > 4 and o.name[-4] != "." and 'repo_path' in o:
    #         repo_lookup_list[o['repo_path']].append(o)
    levelFile = levelData.layerNode
    if target_collection is None:
        target_collection = _ensure_level_collection(levelFile, context)
    kwargs["_level_target_collection"] = target_collection
    _ensure_layer_reload_tracking(target_collection, levelFile, context, nearby_filter, kwargs)

    if import_isolation.needs_isolation_session(context):
        with import_isolation.isolated_import_session(
            context,
            target_collection,
            label=Path(_normalize_level_repo_path(levelFile, context)).stem or Path(levelFile).stem or "Level",
        ) as session:
            kwargs["_level_target_collection"] = target_collection
            result = loadLevel(levelData, session.context, keep_lod_meshes, **kwargs)
        _finalize_layer_reload_cleanup(target_collection, kwargs)
        _activate_collection(context, target_collection)
        return result

    errors = []
    progress_count = 0
    _log_layer_import_start(levelFile)
    _set_layer_import_state(target_collection, levelFile, "in_progress")
    _raise_if_layer_import_cancelled(kwargs)

    ready_to_import = True#checkLevel(levelData)

    #create collection lfor this level
    if ready_to_import:
        collection = target_collection
        if not import_isolation.is_isolated_import_context(context):
            _activate_collection(context, collection)
        if not dev_empty_only and (do_import_Mesh or do_import_Collision or do_import_RigidBody):
            _get_duplicate_root_index(_get_scene(context))

    #start level import
    try:
        if ready_to_import:
            if dev_empty_only:
                _raise_if_layer_import_cancelled(kwargs)
                resolve_started = time.time()
                plan_kwargs = dict(kwargs)
                resolved_plan = resolve_level_import_plan(levelData, context, keep_lod_meshes, **plan_kwargs)
                log.info(
                    "Resolved layer plan for %s: %d items in %.3f seconds",
                    levelFile,
                    int(resolved_plan.get("stats", {}).get("total", 0) or 0),
                    time.time() - resolve_started,
                )
                dev_target_collection = (
                    _get_active_collection(context)
                    if import_isolation.is_isolated_import_context(context)
                    else collection
                )
                progress_count = _import_plan_as_dev_empties(resolved_plan, dev_target_collection, kwargs)
            else:
                if levelData.Foliage:
                    for treeCollection in (levelData.Foliage.Trees.elements if hasattr(levelData.Foliage, 'Trees') else []):
                        _raise_if_layer_import_cancelled(kwargs)
                        treeFilePath = treeCollection.TreeType.DepotPath
                        for treeTransform in treeCollection.TreeCollection.elements:
                            _raise_if_layer_import_cancelled(kwargs)
                            if not _position_within_nearby_filter(
                                _extract_transform_position(treeTransform),
                                nearby_filter,
                            ):
                                _note_nearby_filter_skip(nearby_stats)
                                continue
                            tree_mesh = meshPath(fbx_uncook_path = get_W3_FOLIAGE_PATH(bpy.context))
                            tree_mesh.meshName = treeFilePath
                            tree_mesh.transform = treeTransform
                            tree_mesh.type = "mesh_foliage"
                            import_single_mesh(tree_mesh, errors, keep_lod_meshes = keep_lod_meshes, **kwargs)
                            progress_count += 1
                    for treeCollection in (levelData.Foliage.Grasses.elements if hasattr(levelData.Foliage, 'Grasses') else []):
                        _raise_if_layer_import_cancelled(kwargs)
                        treeFilePath = treeCollection.TreeType.DepotPath
                        for treeTransform in treeCollection.TreeCollection.elements:
                            _raise_if_layer_import_cancelled(kwargs)
                            if not _position_within_nearby_filter(
                                _extract_transform_position(treeTransform),
                                nearby_filter,
                            ):
                                _note_nearby_filter_skip(nearby_stats)
                                continue
                            tree_mesh = meshPath(fbx_uncook_path = get_W3_FOLIAGE_PATH(bpy.context))
                            tree_mesh.meshName = treeFilePath
                            tree_mesh.transform = treeTransform
                            tree_mesh.type = "mesh_foliage"
                            import_single_mesh(tree_mesh, errors, keep_lod_meshes = keep_lod_meshes, **kwargs)
                            progress_count += 1

                mesh_list = get_CSectorData(levelData)
                if mesh_list:
                    mesh_candidates = []
                    for mesh in mesh_list:
                        _raise_if_layer_import_cancelled(kwargs)
                        if not _position_within_nearby_filter(_mesh_world_position(mesh), nearby_filter):
                            _note_nearby_filter_skip(nearby_stats)
                            continue
                        if not (re.search(do_name_filter_regex, mesh.fileName()) if do_enable_name_filter else True):
                            continue
                        mesh_candidates.append(mesh)

                    if mesh_candidates:
                        bpy.ops.object.empty_add(type="PLAIN_AXES", radius=1)
                        empty_transform = bpy.context.object
                        empty_transform.name = "CSectorData"

                        bpy.ops.object.empty_add(type="PLAIN_AXES", radius=1)
                        Collision_transform = bpy.context.object
                        Collision_transform.name = "Collision"
                        Collision_transform.parent = empty_transform
                        bpy.ops.object.empty_add(type="PLAIN_AXES", radius=1)
                        Rigid_transform = bpy.context.object
                        Rigid_transform.name = "Rigid"
                        Rigid_transform.parent = empty_transform
                        bpy.ops.object.empty_add(type="PLAIN_AXES", radius=1)
                        Mesh_transform = bpy.context.object
                        Mesh_transform.name = "Mesh"
                        Mesh_transform.parent = empty_transform
                        bpy.ops.object.empty_add(type="PLAIN_AXES", radius=1)
                        PointLight_transform = bpy.context.object
                        PointLight_transform.name = "PointLight"
                        PointLight_transform.parent = empty_transform
                        bpy.ops.object.empty_add(type="PLAIN_AXES", radius=1)
                        SpotLight_transform = bpy.context.object
                        SpotLight_transform.name = "SpotLight"
                        SpotLight_transform.parent = empty_transform

                        total_loops = len(mesh_candidates)
                        instanced_sector = bool(kwargs.get("instanced_sector", False))

                        if instanced_sector:
                            # Group Mesh/RigidBody placements by repo_path, build one instancer per type.
                            # Lights and proxy meshes are still imported individually.
                            # Key: (kind_str, repo_path) → list of transforms + source_kind tag.
                            instancer_groups = {}
                            mesh: meshPath
                            for idx, mesh in enumerate(mesh_candidates):
                                _raise_if_layer_import_cancelled(kwargs)
                                is_proxy_mesh = bool(getattr(mesh, "is_proxy_mesh", False)) or _path_indicates_proxy_mesh(getattr(mesh, "meshName", ""), "")
                                if (
                                    mesh.BlockDataObjectType == Enums.BlockDataObjectType.Mesh
                                    and not is_proxy_mesh and do_import_Mesh
                                ):
                                    rp = str(mesh.meshName or "").strip()
                                    if rp:
                                        tv = _extract_vector_position(getattr(mesh, "translation", None))
                                        t = list(tv) if tv else [0.0, 0.0, 0.0]
                                        m = _copy_matrix_array(getattr(mesh, "matrix", None))
                                        m = [list(r) for r in m] if m else None
                                        vis_key = _sector_visibility_key_from_flags(getattr(mesh, "sector_flags", None), default=True)
                                        instancer_groups.setdefault(("mesh", rp, vis_key), []).append({"t": t, "m": m})
                                elif (
                                    mesh.BlockDataObjectType == Enums.BlockDataObjectType.Mesh
                                    and is_proxy_mesh and proxy_filter_active and do_import_ProxyMesh
                                ):
                                    import_single_mesh(
                                        mesh, errors, Mesh_transform,
                                        keep_lod_meshes=keep_lod_meshes or bool(kwargs.get("keep_proxy_meshes", True)),
                                        **kwargs,
                                    )
                                elif mesh.BlockDataObjectType == Enums.BlockDataObjectType.RigidBody and do_import_RigidBody:
                                    rp = str(mesh.meshName or "").strip()
                                    if rp:
                                        tv = _extract_vector_position(getattr(mesh, "translation", None))
                                        t = list(tv) if tv else [0.0, 0.0, 0.0]
                                        m = _copy_matrix_array(getattr(mesh, "matrix", None))
                                        m = [list(r) for r in m] if m else None
                                        vis_key = _sector_visibility_key_from_flags(getattr(mesh, "sector_flags", None), default=True)
                                        instancer_groups.setdefault(("rigid", rp, vis_key), []).append({"t": t, "m": m})
                                elif mesh.BlockDataObjectType == Enums.BlockDataObjectType.Collision and do_import_Collision:
                                    rp = str(mesh.meshName or "").strip()
                                    if rp:
                                        tv = _extract_vector_position(getattr(mesh, "translation", None))
                                        t = list(tv) if tv else [0.0, 0.0, 0.0]
                                        m = _copy_matrix_array(getattr(mesh, "matrix", None))
                                        m = [list(r) for r in m] if m else None
                                        instancer_groups.setdefault(("collision", rp, ""), []).append({"t": t, "m": m})
                                elif mesh.BlockDataObjectType == Enums.BlockDataObjectType.PointLight and do_import_PointLight:
                                    import_light(mesh, PointLight_transform)
                                elif mesh.BlockDataObjectType == Enums.BlockDataObjectType.SpotLight and do_import_SpotLight:
                                    import_light(mesh, SpotLight_transform)
                                progress_count += 1

                            active_coll = getattr(bpy.context, "collection", None) or collection
                            owner_tag = kwargs.get("_layer_import_owner")
                            gen_tag = kwargs.get("_layer_import_generation")
                            mode_sig = str(kwargs.get("_layer_import_mode_signature", "") or "")
                            instancer_parent_by_kind = {
                                "mesh": Mesh_transform,
                                "rigid": Rigid_transform,
                                "rigid_body": Rigid_transform,
                                "collision": Collision_transform,
                            }
                            for si_idx, ((si_kind, rp, vis_key), transforms) in enumerate(instancer_groups.items()):
                                _raise_if_layer_import_cancelled(kwargs)
                                synthetic_item = {
                                    "id": f"si_{si_idx}",
                                    "repo_path": rp,
                                    "sector_transforms": transforms,
                                    "_source_kind": si_kind,
                                    "sector_visibility_key": vis_key,
                                    "sector_visible": vis_key != "hidden",
                                }
                                _import_cached_plan_sector_instancer_item(
                                    synthetic_item,
                                    active_coll,
                                    instancer_parent_by_kind.get(si_kind, Mesh_transform),
                                    owner_tag, gen_tag,
                                    f"si_{si_idx}", mode_sig,
                                    kwargs, errors, context=context,
                                )
                        else:
                            mesh: meshPath
                            for idx, mesh in enumerate(mesh_candidates):
                                _raise_if_layer_import_cancelled(kwargs)
                                progress_msg = f"{idx+1}/{total_loops} - {os.path.basename(mesh.meshName)}"
                                is_proxy_mesh = bool(getattr(mesh, "is_proxy_mesh", False)) or _path_indicates_proxy_mesh(getattr(mesh, "meshName", ""), "")
                                if (
                                    mesh.BlockDataObjectType == Enums.BlockDataObjectType.Mesh
                                    and ((is_proxy_mesh and proxy_filter_active and do_import_ProxyMesh) or ((not is_proxy_mesh or not proxy_filter_active) and do_import_Mesh))
                                ):
                                    import_single_mesh(
                                        mesh,
                                        errors,
                                        Mesh_transform,
                                        keep_lod_meshes=keep_lod_meshes or (is_proxy_mesh and bool(kwargs.get("keep_proxy_meshes", True))),
                                        **kwargs,
                                    )
                                elif mesh.BlockDataObjectType == Enums.BlockDataObjectType.Collision and do_import_Collision:
                                    _import_sector_collision_from_cache(mesh, errors, Collision_transform, **kwargs)
                                elif mesh.BlockDataObjectType == Enums.BlockDataObjectType.RigidBody and do_import_RigidBody:
                                    import_single_mesh(mesh, errors, Rigid_transform, keep_lod_meshes = keep_lod_meshes, **kwargs)
                                elif mesh.BlockDataObjectType == Enums.BlockDataObjectType.PointLight and do_import_PointLight:
                                    import_light(mesh, PointLight_transform)
                                elif mesh.BlockDataObjectType == Enums.BlockDataObjectType.SpotLight and do_import_SpotLight:
                                    import_light(mesh, SpotLight_transform)
                                progress_count += 1
                                progress_msg += " " * (80 - len(progress_msg))
                                log.info(progress_msg)

                        _tag_object_tree_for_layer(
                            empty_transform,
                            kwargs.get("_layer_import_owner"),
                            kwargs.get("_layer_import_generation"),
                        )

                if do_import_Entity:
                    for INCLUDE_OBJECT in levelData.includes:
                        _raise_if_layer_import_cancelled(kwargs)
                        for ENTITY_OBJECT in INCLUDE_OBJECT.Entities:
                            _raise_if_layer_import_cancelled(kwargs)
                            if ENTITY_OBJECT.type in Entity_Type_List:
                                imported_entity = import_gameplay_entity(
                                    ENTITY_OBJECT,
                                    errors,
                                    keep_lod_meshes = keep_lod_meshes,
                                    **kwargs,
                                )
                                if imported_entity is not None:
                                    progress_count += 1

                    total_loops = len(levelData.Entities)
                    for idx, ENTITY_OBJECT in enumerate(levelData.Entities):
                        _raise_if_layer_import_cancelled(kwargs)
                        if re.search(do_name_filter_regex, ENTITY_OBJECT.name) if do_enable_name_filter else True:
                            progress_msg = f"{idx+1}/{total_loops} - {ENTITY_OBJECT.name}"
                            if ENTITY_OBJECT.type in Entity_Type_List:
                                imported_entity = import_gameplay_entity(
                                    ENTITY_OBJECT,
                                    errors,
                                    keep_lod_meshes = keep_lod_meshes,
                                    **kwargs,
                                )
                                if imported_entity is not None:
                                    progress_count += 1
                                    progress_msg += " " * (80 - len(progress_msg))
                                    log.info(progress_msg)
        _finalize_layer_reload_cleanup(target_collection, kwargs)
        filtered_count = int(nearby_stats.get("filtered", 0) or 0)
        _log_layer_import_complete(levelFile, progress_count, errors)
        if not dev_empty_only:
            _log_layer_import_profile_summary(levelFile, kwargs)
        mode_signature = str(kwargs.get("_layer_import_mode_signature", "") or "").strip()
        if not mode_signature:
            mode_signature = _layer_load_mode_signature(dev_empty_only)
        _set_layer_import_state(
            target_collection,
            levelFile,
            (
                "proxy_complete" if dev_empty_only and not errors and filtered_count <= 0
                else "proxy_partial" if dev_empty_only
                else "complete" if not errors and filtered_count <= 0
                else "partial"
            ),
            progress_count,
            len(errors),
            filtered_count,
            nearby_filter=nearby_filter,
            mode_signature=mode_signature,
        )
    except LayerImportCancelled:
        filtered_count = int(nearby_stats.get("filtered", 0) or 0)
        _set_layer_import_state(
            target_collection,
            levelFile,
            "proxy_partial" if dev_empty_only else "partial",
            progress_count,
            len(errors),
            filtered_count,
        )
        raise
    except Exception:
        filtered_count = int(nearby_stats.get("filtered", 0) or 0)
        _set_layer_import_state(
            target_collection,
            levelFile,
            "proxy_failed" if dev_empty_only else "failed",
            progress_count,
            max(1, len(errors)),
            filtered_count,
        )
        raise
        # for idx, ENTITY_OBJECT in enumerate(levelData.meshes):
        #     if ENTITY_OBJECT.type == "Mesh": #A SINGLE MESH WITH NO COMPONENTS
        #         import_single_mesh(ENTITY_OBJECT, errors, **kwargs)
        #         #log.info(idx, ENTITY_OBJECT.translation.x,ENTITY_OBJECT.translation.y,ENTITY_OBJECT.translation.z)
        #     if ENTITY_OBJECT.type == "CGameplayEntity" or ENTITY_OBJECT.type == "CSectorData": #A ENTITY WITH A TRANSFORM AND LIST OF MESH/LIGHTS
        #         import_gameplay_entity(ENTITY_OBJECT, errors)
        #     if ENTITY_OBJECT.type == "CEntity": # A MESH WITH COMPONENTS
        #         bpy.ops.object.empty_add(type="PLAIN_AXES", radius=1)
        #         Entity_transform = bpy.context.object
        #         Entity_transform.name = ENTITY_OBJECT.meshName #"CGameplayEntity_empty_transform"
        #         for comp in ENTITY_OBJECT.components:
        #             import_gameplay_entity(comp, errors, Entity_transform)
        #         set_blender_object_transform(Entity_transform, ENTITY_OBJECT.transform)
    
        
    #! #################
    #!     #PROFILER
    #! #################
    #! profiler.disable()
    
    #! # Dump profiling data to file
    #! with open('profile_results.log', 'w') as f:
    #!     profiler.dump_stats(f.name)

    #! # Read profiling data from file and print to log file
    #! with open('log_file.txt', 'w') as log_file:
    #!     stats = pstats.Stats('profile_results.log', stream=log_file)
    #!     stats.sort_stats('cumulative')
    #!     stats.print_stats()
    
    return {'FINISHED'}


def loadLevelFromCachedPlan(level_file, plan_items, context=None, **kwargs):
    """Fast path for cached plan layer loads.

    Skips parsing the .w2l binary and re-resolving the import plan; instead
    consumes the plan items captured at scan-time (entry["items"] in the
    world layer cache). Supports dev-empty proxy loads and full loads for
    cached mesh/foliage/collision/rigid/cloth/light items.
    """
    target_collection = kwargs.pop("_level_target_collection", None)
    dev_empty_only = bool(kwargs.get("_dev_empty_only", False))

    if context is None:
        context = bpy.context

    nearby_filter = _get_nearby_import_filter(kwargs)
    nearby_stats = _get_nearby_import_stats(kwargs)

    if target_collection is None:
        target_collection = _ensure_level_collection(level_file, context)
    kwargs["_level_target_collection"] = target_collection
    mode_signature = str(kwargs.get("_layer_import_mode_signature", "") or "").strip()
    if not mode_signature:
        mode_signature = _layer_load_mode_signature(dev_empty_only)
    if (
        not dev_empty_only
        and _cached_plan_loaded_item_ids(target_collection, mode_signature)
    ):
        kwargs["_layer_import_incremental"] = True
    _ensure_layer_reload_tracking(target_collection, level_file, context, nearby_filter, kwargs)

    if import_isolation.needs_isolation_session(context):
        with import_isolation.isolated_import_session(
            context,
            target_collection,
            label=Path(_normalize_level_repo_path(level_file, context)).stem or Path(level_file).stem or "Level",
        ) as session:
            kwargs["_level_target_collection"] = target_collection
            result = loadLevelFromCachedPlan(level_file, plan_items, session.context, **kwargs)
        _finalize_layer_reload_cleanup(target_collection, kwargs)
        _activate_collection(context, target_collection)
        return result

    errors = []
    progress_count = 0
    if not dev_empty_only:
        kwargs["_layer_import_profile"] = _new_layer_import_profile()
    _log_layer_import_start(level_file)
    _set_layer_import_state(target_collection, level_file, "in_progress")
    _raise_if_layer_import_cancelled(kwargs)

    if not import_isolation.is_isolated_import_context(context):
        _activate_collection(context, target_collection)

    try:
        _raise_if_layer_import_cancelled(kwargs)
        source_items = list(plan_items or [])
        if not dev_empty_only:
            source_items = cached_plan_filter_items_for_import_options(
                source_items,
                kwargs,
                context=context,
            )
            source_items = _maybe_group_cached_items_into_sector_instancers(source_items, kwargs)
            source_items = _ensure_cached_sector_group_hierarchy(source_items)
        filtered_items = _filter_cached_plan_items_by_proximity(
            source_items,
            nearby_filter,
            nearby_stats,
        )
        plan = {
            "items": filtered_items,
            "stats": {"total": len(filtered_items), "filtered": 0, "by_kind": {}},
        }
        import_target_collection = (
            _get_active_collection(context)
            if import_isolation.is_isolated_import_context(context)
            else target_collection
        )
        if not dev_empty_only:
            progress_count = _import_cached_plan_full_items(
                plan,
                import_target_collection,
                kwargs,
                context=context,
                loaded_collection=target_collection,
                errors=errors,
                level_file=level_file,
            )
        else:
            progress_count = _import_plan_as_dev_empties(plan, import_target_collection, kwargs)

        _finalize_layer_reload_cleanup(target_collection, kwargs)
        filtered_count = int(nearby_stats.get("filtered", 0) or 0)
        _log_layer_import_complete(level_file, progress_count, errors)
        if not dev_empty_only:
            _log_layer_import_profile_summary(level_file, kwargs)
        complete_state = "proxy_complete" if dev_empty_only else "complete"
        partial_state = "proxy_partial" if dev_empty_only else "partial"
        _set_layer_import_state(
            target_collection,
            level_file,
            complete_state if not errors and filtered_count <= 0 else partial_state,
            progress_count,
            len(errors),
            filtered_count,
            nearby_filter=nearby_filter,
            mode_signature=mode_signature,
        )
    except LayerImportCancelled:
        filtered_count = int(nearby_stats.get("filtered", 0) or 0)
        _set_layer_import_state(
            target_collection,
            level_file,
            "proxy_partial" if dev_empty_only else "partial",
            progress_count,
            len(errors),
            filtered_count,
        )
        raise
    except Exception:
        filtered_count = int(nearby_stats.get("filtered", 0) or 0)
        _set_layer_import_state(
            target_collection,
            level_file,
            "proxy_failed" if dev_empty_only else "failed",
            progress_count,
            max(1, len(errors)),
            filtered_count,
        )
        raise

    return {'FINISHED'}


from bpy.types import Object, Mesh

def repo_in_scene(dct, path):
    if path in dct.keys():
        return True
    else:
        return False

def _has_blender_numeric_suffix(name: str) -> bool:
    return bool(name) and len(name) > 4 and name[-4] == "." and name[-3:].isdigit()


def _is_duplicate_root_candidate(obj, repo_path=None):
    if not _is_live_blender_object(obj):
        return False
    try:
        obj_type = getattr(obj, "type", "")
        obj_name = getattr(obj, "name", "")
        obj_repo_path = str(obj.get("repo_path", "") or "").strip()
    except ReferenceError:
        return False
    except Exception:
        return False
    if obj_type != 'EMPTY':
        return False
    if not obj_name:
        return False
    if not obj_repo_path:
        return False
    if repo_path is not None and obj_repo_path != repo_path:
        return False
    return True


def _prefer_duplicate_root(current_obj, candidate_obj):
    if not _is_duplicate_root_candidate(candidate_obj):
        return current_obj
    if not _is_duplicate_root_candidate(current_obj):
        return candidate_obj
    try:
        current_primary = not _has_blender_numeric_suffix(getattr(current_obj, "name", ""))
        candidate_primary = not _has_blender_numeric_suffix(getattr(candidate_obj, "name", ""))
    except ReferenceError:
        return candidate_obj
    if candidate_primary and not current_primary:
        return candidate_obj
    return current_obj


def _rebuild_duplicate_root_index(scene=None):
    scene = scene or _get_scene()
    roots = {}
    if scene is not None:
        for obj in scene.objects:
            if not _is_duplicate_root_candidate(obj):
                continue
            repo_path = str(obj.get("repo_path", "") or "").strip()
            roots[repo_path] = _prefer_duplicate_root(roots.get(repo_path), obj)
    _REPO_DUPLICATE_CACHE["scene_key"] = _scene_identity(scene)
    _REPO_DUPLICATE_CACHE["object_count"] = len(scene.objects) if scene is not None else -1
    _REPO_DUPLICATE_CACHE["roots"] = roots
    return roots


def _get_duplicate_root_index(scene=None):
    scene = scene or _get_scene()
    scene_key = _scene_identity(scene)
    object_count = len(scene.objects) if scene is not None else -1
    if _REPO_DUPLICATE_CACHE["scene_key"] != scene_key:
        return _rebuild_duplicate_root_index(scene)
    cached_object_count = int(_REPO_DUPLICATE_CACHE.get("object_count", -1))
    if object_count >= 0 and cached_object_count >= 0 and object_count < cached_object_count:
        return _rebuild_duplicate_root_index(scene)
    roots = _REPO_DUPLICATE_CACHE.get("roots") or {}
    _REPO_DUPLICATE_CACHE["object_count"] = max(cached_object_count, object_count)
    return roots


def _touch_duplicate_root_index(scene=None):
    scene = scene or _get_scene()
    if _REPO_DUPLICATE_CACHE["scene_key"] == _scene_identity(scene):
        object_count = len(scene.objects) if scene is not None else -1
        cached_object_count = int(_REPO_DUPLICATE_CACHE.get("object_count", -1))
        _REPO_DUPLICATE_CACHE["object_count"] = max(cached_object_count, object_count)


def _record_duplicate_root(obj, scene=None):
    scene = scene or _get_scene()
    if scene is None:
        return
    if _REPO_DUPLICATE_CACHE["scene_key"] != _scene_identity(scene):
        _rebuild_duplicate_root_index(scene)
        return
    object_count = len(scene.objects)
    cached_object_count = int(_REPO_DUPLICATE_CACHE.get("object_count", -1))
    _REPO_DUPLICATE_CACHE["object_count"] = max(cached_object_count, object_count)
    if not _is_duplicate_root_candidate(obj):
        return
    repo_path = str(obj.get("repo_path", "") or "").strip()
    current_obj = _REPO_DUPLICATE_CACHE["roots"].get(repo_path)
    _REPO_DUPLICATE_CACHE["roots"][repo_path] = _prefer_duplicate_root(current_obj, obj)


def _remap_object_reference(owner, attr_name, clone_by_id):
    if owner is None or not hasattr(owner, attr_name):
        return
    try:
        current_value = getattr(owner, attr_name)
    except Exception:
        return
    clone_value = clone_by_id.get(_object_identity(current_value))
    if clone_value is None:
        return
    try:
        setattr(owner, attr_name, clone_value)
    except Exception:
        return


def _clone_duplicate_hierarchy(source_root, target_collection=None, *, remap_links=True):
    if not _is_duplicate_root_candidate(source_root):
        return None
    target_collection = target_collection or _get_active_collection()
    if target_collection is None:
        return None

    clone_pairs = []
    clone_by_id = {}
    try:
        source_objects = [source_root] + list(getattr(source_root, "children_recursive", []) or [])
    except ReferenceError:
        return None
    for source_obj in source_objects:
        clone_obj = source_obj.copy()
        target_collection.objects.link(clone_obj)
        clone_pairs.append((source_obj, clone_obj))
        clone_by_id[_object_identity(source_obj)] = clone_obj

    for source_obj, clone_obj in clone_pairs:
        clone_parent = clone_by_id.get(_object_identity(getattr(source_obj, "parent", None)))
        clone_obj.parent = clone_parent

    if remap_links:
        for _source_obj, clone_obj in clone_pairs:
            for modifier in getattr(clone_obj, "modifiers", []):
                for attr_name in ("object", "mirror_object", "offset_object"):
                    _remap_object_reference(modifier, attr_name, clone_by_id)
            for constraint in getattr(clone_obj, "constraints", []):
                for attr_name in ("target", "space_object"):
                    _remap_object_reference(constraint, attr_name, clone_by_id)

    new_root = clone_by_id.get(_object_identity(source_root))
    if new_root is None:
        return None

    identity = Matrix.Identity(4)
    new_root.parent = None
    new_root.matrix_world = identity.copy()
    new_root.matrix_local = identity.copy()
    new_root.matrix_basis = identity.copy()
    new_root.location[0] = 0
    new_root.location[1] = 0
    new_root.location[2] = 0
    new_root.scale[0] = 1
    new_root.scale[1] = 1
    new_root.scale[2] = 1
    return new_root


def check_if_empty_already_in_scene(repo_path, *, fast_static_clone=False):
    scene = _get_scene()
    repo_path = str(repo_path or "").strip()
    if not repo_path:
        return False

    start_time1 = time.time()
    root_index = _get_duplicate_root_index(scene)
    source_root = root_index.get(repo_path)
    # Cache misses are expected while importing new assets. Rebuild only when a
    # cached hit looks stale.
    if source_root is not None and not _is_duplicate_root_candidate(source_root, repo_path):
        source_root = _rebuild_duplicate_root_index(scene).get(repo_path)
    if source_root is None:
        return False

    log.info('Check Mesh found in %f seconds.', time.time() - start_time1)
    start_time2 = time.time()
    new_obj = _clone_duplicate_hierarchy(
        source_root,
        _get_active_collection(),
        remap_links=not bool(fast_static_clone),
    )
    if new_obj is None:
        return False
    _touch_duplicate_root_index(scene)
    log.info('Check Mesh Finished importing in %f seconds.', time.time() - start_time2)
    return new_obj

def check_if_mesh_already_in_scene(repo_path):

    start_time1 = time.time()
    # name = Path(repo_path).stem+"_Mesh_lod0"
    # try:
    #     o = bpy.context.scene.objects[name]
    # except Exception as e:
    #     try:
    #         name = Path(repo_path).stem+"_Mesh"
    #         o = bpy.context.scene.objects[name]
    #     except Exception as e:
    #         return False
    # #else:
    for o in bpy.context.scene.objects:
        if o.type != 'MESH':
            continue
        if o.name[-4] != "." and 'repo_path' in o and o['repo_path'] == repo_path:
            log.info('Check Mesh found in %f seconds.', time.time() - start_time1)
            start_time2 = time.time()
            #log.info("COPYING", o['repo_path'])
            new_obj = o.copy()
            #new_obj.data = o.data.copy()
            #new_obj.animation_data_clear()
            bpy.context.collection.objects.link(new_obj)

            # new_obj.rotation_euler[0] = 0
            # new_obj.rotation_euler[1] = 0
            # new_obj.rotation_euler[2] = 0
            # new_obj.rotation_euler = (0,0,0)
            x, y, z = (radians(0), radians(0), radians(0))
            mat = Euler((x, y, z)).to_matrix().to_4x4()
            new_obj.matrix_world = mat
            new_obj.matrix_local = mat
            new_obj.matrix_basis = mat

            new_obj.location[0] = 0
            new_obj.location[1] = 0
            new_obj.location[2] = 0
            new_obj.scale[0] = 1
            new_obj.scale[1] = 1
            new_obj.scale[2] = 1
            new_obj.parent = None
            log.info('Check Mesh Finished importing in %f seconds.', time.time() - start_time2)
            return new_obj
    return False

def _import_foliage_mesh(mesh: meshPath):
    """Import a foliage tree mesh using SRT (SpeedTree) if available, FBX as fallback."""
    srt_status = get_srt_addon_status()
    if srt_status["enabled"]:
        try:
            srt_path = repo_file(mesh.meshName)
            if srt_path and os.path.exists(srt_path):
                from ..ui.ui_file_browser import (
                    _export_srt_textures_for_import,
                    _prepare_srt_lod0_json,
                    _snapshot_srt_import_state,
                    _flatten_srt_import_collections,
                )
                from .. import get_all_addon_prefs
                context = bpy.context
                prefs = get_all_addon_prefs(context)
                use_custom_grouping = bool(getattr(prefs, "ab_srt_custom_grouping", True))
                lod0_only = bool(getattr(prefs, "ab_srt_lod0_only", True))

                srt_snapshot = _snapshot_srt_import_state(context) if use_custom_grouping else {}
                tex_stats = _export_srt_textures_for_import(
                    context, srt_path, mesh.meshName, loadmods=False,
                )
                import_path = tex_stats.get("import_path") or srt_path
                if lod0_only:
                    import_path = _prepare_srt_lod0_json(import_path)
                result = getattr(bpy.ops, "import").srt_json(filepath=import_path)
                if 'FINISHED' in result:
                    if use_custom_grouping:
                        _flatten_srt_import_collections(context, import_path, srt_snapshot)
                    return
                log.warning("SRT import failed for %s, falling back to FBX", mesh.meshName)
            else:
                log.warning("SRT file not found: %s, falling back to FBX", mesh.meshName)
        except Exception as e:
            log.warning("SRT import error for %s: %s, falling back to FBX", mesh.meshName, e)
    # Fallback to FBX
    bpy.ops.import_scene.fbx(filepath=mesh.fbxPath())


def import_single_mesh(mesh:meshPath, errors, parent_transform = False, keep_lod_meshes = False, version = 999, **kwargs):
    _raise_if_layer_import_cancelled(kwargs)
    mesh_started = time.perf_counter()
    use_fbx = get_use_fbx_repo(bpy.context)
    import_seconds = 0.0
    finalize_seconds = 0.0
    transform_seconds = 0.0
    backend = "reuse"
    reused_existing = False

    obj = check_if_empty_already_in_scene(
        mesh.meshName,
        fast_static_clone=bool(kwargs.get("_cached_plan_fast_static_clone", False)),
    )
    # if keep_lod_meshes:
    #     obj = check_if_empty_already_in_scene(mesh.meshName)
    # else:
    #     obj = check_if_mesh_already_in_scene(mesh.meshName)
    #obj = False
    if not obj:
        # if keep_lod_meshes:
        #     bpy.ops.object.empty_add(type="PLAIN_AXES", radius=1)
        #     obj = bpy.context.object
        pre_selected_ids = {obj.as_pointer() for obj in bpy.context.selected_objects[:]}
        bpy.ops.object.empty_add(type="PLAIN_AXES", radius=1)
        obj = bpy.context.object
        try:
            import_started = time.perf_counter()
            if mesh.type == "mesh_foliage":
                backend = "foliage"
                _import_foliage_mesh(mesh)
            else:
                if use_fbx and os.path.exists(mesh.fbxPath()):
                    backend = "fbx"
                    fbx_util.importFbx(mesh.fbxPath(),mesh.fileName(),mesh.fileName(), keep_lod_meshes=keep_lod_meshes)
                elif not use_fbx:
                    backend = "cr2w"
                    import_mesh.import_mesh(repo_file(mesh.meshName, version), keep_lod_meshes = keep_lod_meshes, keep_empty_lods = kwargs.get('keep_empty_lods', False), keep_proxy_meshes = kwargs.get('keep_proxy_meshes', False))
                else:
                    backend = "fallback_cube"
                    log.warning("Can't find FBX file %s", mesh.fbxPath())
                    bpy.ops.mesh.primitive_cube_add()
                    objs = bpy.context.selected_objects[:]
                    objs[0].color = (0,0,1,1)
                    objs[0].name = "ERROR_CUBE"
                    err_mat = bpy.data.materials.new("ERROR_CUBE_MAT")
                    err_mat.use_nodes = True
                    principled = err_mat.node_tree.nodes['Principled BSDF']
                    principled.inputs['Base Color'].default_value = (0,0,1,1)
                    objs[0].data.materials.append(err_mat)
            import_seconds = time.perf_counter() - import_started

        except Exception:
            log.exception("Problem importing mesh %s", mesh.meshName)
            raise
        try:
            finalize_started = time.perf_counter()

            objs = [
                subobj
                for subobj in bpy.context.selected_objects[:]
                if subobj.as_pointer() not in pre_selected_ids
            ]
            if obj not in objs:
                objs.append(obj)
            #if keep_lod_meshes:
            import_name = str(getattr(mesh, "import_name", "") or "").strip()
            obj.name = import_name or Path(mesh.meshName).stem
            obj['repo_path'] = mesh.meshName
            for subobj in objs:
                if subobj == obj:
                    continue
                subobj.parent = obj
            # else:
            #     obj = objs[0]
            #     obj['repo_path'] = mesh.meshName
            #apply scale
            bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
            _record_duplicate_root(obj)
            finalize_seconds = time.perf_counter() - finalize_started
        except Exception:
            #usually tried to do something with materials and failed
            log.exception("Problem finalizing imported mesh %s", mesh.meshName)
            return
    else:
        reused_existing = True
    if parent_transform:
        obj.parent = parent_transform

    transform_started = time.perf_counter()
    if mesh.transform:
        obj.rotation_euler = (0,0,0)
        #THIS WORKS?
        x, y, z = (
            radians(_transform_real(mesh.transform, "Yaw", 0.0)),
            radians(_transform_real(mesh.transform, "Pitch", 0.0)),
            radians(_transform_real(mesh.transform, "Roll", 0.0)),
        )
        orders =  ['XYZ', 'XZY', 'YXZ', 'YZX', 'ZXY', 'ZYX']
        mat = Euler((x, y, z), orders[2]).to_matrix().to_4x4()

        rotate_180 = False
        if rotate_180:
            mat[0][0], mat[0][1], mat[0][2] = -mat[0][0], -mat[0][1], mat[0][2]
            mat[1][0], mat[1][1], mat[1][2] = -mat[1][0], -mat[1][1], mat[1][2]
            mat[2][0], mat[2][1], mat[2][2] = -mat[2][0], -mat[2][1], mat[2][2]
        else:
            mat[0][0], mat[0][1], mat[0][2] = mat[0][0], mat[0][1], mat[0][2]
            mat[1][0], mat[1][1], mat[1][2] = mat[1][0], mat[1][1], mat[1][2]
            mat[2][0], mat[2][1], mat[2][2] = mat[2][0], mat[2][1], mat[2][2]

        obj.matrix_world @= mat
        # obj.rotation_euler[0] = mesh.transform.Pitch
        # obj.rotation_euler[1] = mesh.transform.Yaw
        # obj.rotation_euler[2] = mesh.transform.Roll
        obj.location[0] = _transform_real(mesh.transform, "X", 0.0)
        obj.location[1] = _transform_real(mesh.transform, "Y", 0.0)
        obj.location[2] = _transform_real(mesh.transform, "Z", 0.0)

        #foliage transforms don't have scale
        if isinstance(mesh.transform, dict) or hasattr(mesh.transform, "Scale_x"):
            obj.scale[0] = _transform_real(mesh.transform, "Scale_x", 1.0)
            obj.scale[1] = _transform_real(mesh.transform, "Scale_y", 1.0)
            obj.scale[2] = _transform_real(mesh.transform, "Scale_z", 1.0)
        # else:
        #     obj.scale[0] =0.01
        #     obj.scale[1] =0.01
        #     obj.scale[2] =0.01
    if mesh.matrix:
        try:
            #obj = bpy.context.selected_objects[:][0]
            #MATRIX PART
            log.info(obj.name)
            mat = Matrix()

            rotate_180 = False
            if rotate_180:
                mat[0][0], mat[0][1], mat[0][2] = -mesh.matrix[0][0], -mesh.matrix[1][0], mesh.matrix[2][0]
                mat[1][0], mat[1][1], mat[1][2] = -mesh.matrix[0][1], -mesh.matrix[1][1], mesh.matrix[2][1]
                mat[2][0], mat[2][1], mat[2][2] = -mesh.matrix[0][2], -mesh.matrix[1][2], mesh.matrix[2][2]
            else:
                mat[0][0], mat[0][1], mat[0][2] = mesh.matrix[0][0], mesh.matrix[1][0], mesh.matrix[2][0]
                mat[1][0], mat[1][1], mat[1][2] = mesh.matrix[0][1], mesh.matrix[1][1], mesh.matrix[2][1]
                mat[2][0], mat[2][1], mat[2][2] = mesh.matrix[0][2], mesh.matrix[1][2], mesh.matrix[2][2]
            #log.info(mat)
            obj.matrix_world = obj.matrix_world @ mat
        except Exception:
            error_message = "ERROR MESH IMPORTER: Can't import: " + mesh.fbxPath()
            log.info(error_message)
            errors.append(error_message)
    if mesh.translation:
        translation = _extract_vector_position(mesh.translation)
        if translation is not None:
            obj.location[0] = translation[0]
            obj.location[1] = translation[1]
            obj.location[2] = translation[2]
    _tag_object_tree_for_layer_and_plan(
        obj,
        kwargs.get("_layer_import_owner"),
        kwargs.get("_layer_import_generation"),
        kwargs.get("_layer_import_plan_item_id"),
        kwargs.get("_layer_import_plan_mode"),
    )
    is_proxy_mesh = bool(getattr(mesh, "is_proxy_mesh", False)) or _path_indicates_proxy_mesh(getattr(mesh, "meshName", ""), "")
    if is_proxy_mesh:
        _tag_object_tree_as_proxy_mesh(obj)
    else:
        _clear_object_tree_proxy_mesh_tags(obj)
    sector_flags = getattr(mesh, "sector_flags", None)
    if sector_flags is not None:
        try:
            _tag_object_tree_engine_visibility(
                obj,
                _sector_mesh_visible_from_flags(sector_flags, default=True),
                kwargs,
                sector_flags=sector_flags,
            )
        except Exception:
            pass
    elif hasattr(mesh, "engine_visible"):
        try:
            _tag_object_tree_engine_visibility(
                obj,
                bool(getattr(mesh, "engine_visible")),
                kwargs,
                drawable_flags=getattr(mesh, "drawable_flags", None) if hasattr(mesh, "drawable_flags") else None,
            )
        except Exception:
            pass
    transform_seconds = time.perf_counter() - transform_started
    total_seconds = time.perf_counter() - mesh_started
    _record_layer_mesh_profile(
        kwargs,
        mesh,
        backend,
        reused_existing,
        total_seconds,
        import_seconds,
        finalize_seconds,
        transform_seconds,
    )
    if total_seconds >= _MESH_IMPORT_WARN_THRESHOLD:
        _log_mesh_import_timing_warning(
            "single mesh %s total %.3fs (backend %s, import %.3fs, finalize %.3fs, transform %.3fs, reused %s, kind %s)",
            mesh.meshName,
            total_seconds,
            backend,
            import_seconds,
            finalize_seconds,
            transform_seconds,
            "yes" if reused_existing else "no",
            mesh.type,
        )
    return obj

MeshComponent_Type_List = ['CStaticMeshComponent',
                            'CMeshComponent',
                            'CRigidMeshComponent',
                            "CBgMeshComponent",
                            "CBgNpcItemComponent",
                            "CBoatBodyComponent",
                            "CDressMeshComponent",
                            "CFurComponent",
                            "CImpostorMeshComponent",
                            "CMergedMeshComponent",
                            "CMergedShadowMeshComponent",
                            "CMorphedMeshComponent",
                            "CNavmeshComponent",
                            "CRigidMeshComponentCooked",
                            "CScriptedDestroyableComponent",
                            "CWindowComponent"]

def getDataBufferMesh(entity, *, mesh_fbx_uncook_path=None, mesh_uncook_path=None):
    mesh_list = []
    cloth_list = []
    if hasattr(entity, "streamingDataBuffer") and entity.streamingDataBuffer:
        for chunk in entity.streamingDataBuffer.CHUNKS.CHUNKS:
            if chunk.name in Entity_Type_List:
                log.info("Found an entity in data buffer??")
            if chunk.name in MeshComponent_Type_List:
                mesh = _new_mesh_path(
                    fbx_uncook_path=mesh_fbx_uncook_path,
                    uncook_path=mesh_uncook_path,
                ).static_from_chunk(chunk)
                drawable_flags = _component_drawable_flags(chunk)
                mesh.drawable_flags = drawable_flags
                mesh.engine_visible = _drawable_flags_visible_from_value(drawable_flags, default=True)
                mesh_list.append(mesh)
            
            if chunk.name in {"CClothComponent", "CDestructionSystemComponent"}:
                cloth_list.append(chunk)

    return (mesh_list, cloth_list)

from .. import get_witcher2_game_path

def import_single_component(component, parent_obj, keep_lod_meshes = False, **kwargs):
    if component.name == "CMeshComponent" or component.name == "CStaticMeshComponent":
        try:
            mesh = meshPath(fbx_uncook_path = get_fbx_uncook_path(bpy.context)).static_from_chunk(component)
            is_proxy_mesh = _path_indicates_proxy_mesh(getattr(mesh, "meshName", ""), "")
            if is_proxy_mesh and _proxy_mesh_filter_active(kwargs):
                if not bool(kwargs.get("do_import_ProxyMesh", False)):
                    return
            elif not bool(kwargs.get("do_import_Mesh", True)):
                return
            # if component.get_CR2W_version() <= 115:
            #     mesh.uncook_path = get_witcher2_game_path(bpy.context) + '\\data'
            mesh_label = str(getattr(mesh, "name", "") or "").strip()
            if not mesh_label or mesh_label == "Mesh Item":
                mesh_label = Path(mesh.meshName).stem or component.name
            drawable_flags = _component_drawable_flags(component)
            mesh.drawable_flags = drawable_flags
            mesh.engine_visible = _drawable_flags_visible_from_value(drawable_flags, default=True)
            mesh.import_name = f"{component.name} {mesh_label}"
            import_single_mesh(
                mesh,
                [],
                parent_obj,
                keep_lod_meshes=keep_lod_meshes or (is_proxy_mesh and bool(kwargs.get("keep_proxy_meshes", True))),
                version=component.get_CR2W_version(),
                **kwargs,
            )
        except Exception as e:
            log.critical('import_single_component mesh fail') #w2 has embedded here??
    elif component.name == "CPointLightComponent":
        if not bool(kwargs.get("do_import_PointLight", True)):
            return
        bpy.ops.object.light_add(type='POINT', radius=1, align='WORLD', location=(0, 0, 0), scale=(1, 1, 1))
        light_obj = bpy.context.selected_objects[:][0]
        light_obj.parent = parent_obj
        if component.GetVariableByName('brightness'):
            light_obj.data.energy = component.GetVariableByName('brightness').Value * 10

        
        COLOR = component.GetVariableByName('color')
        if COLOR:
            for color in COLOR.More:
                if color.theName == "Red":
                    light_obj.data.color[0] = color.Value/255
                elif color.theName == "Green":
                    light_obj.data.color[1] = color.Value/255
                elif color.theName == "Blue":
                    light_obj.data.color[2] = color.Value/255
                elif color.theName == "Alpha":
                    pass # do some custom val?
                    #light_obj.data.color[3] = color.Value/255
        RADIUS = component.GetVariableByName('radius')
        if RADIUS:
            light_obj.data.shadow_soft_size = RADIUS.Value
        if component.GetVariableByName('transform'):
            set_blender_object_transform(light_obj, component.GetVariableByName('transform').EngineTransform)
    
    elif component.name == "CSpotLightComponent":
        if not bool(kwargs.get("do_import_SpotLight", True)):
            return
        bpy.ops.object.light_add(type='SPOT', radius=1, align='WORLD', location=(0, 0, 0), scale=(1, 1, 1))
        light_obj = bpy.context.selected_objects[:][0]
        light_obj.parent = parent_obj
        light_obj.data.energy = component.GetVariableByName('brightness').Value * 3

        COLOR = component.GetVariableByName('color')
        if COLOR:
            for color in COLOR.More:
                if color.theName == "Red":
                    light_obj.data.color[0] = color.Value/255
                elif color.theName == "Green":
                    light_obj.data.color[1] = color.Value/255
                elif color.theName == "Blue":
                    light_obj.data.color[2] = color.Value/255
                elif color.theName == "Alpha":
                    pass # do some custom val?
                    #light_obj.data.color[3] = color.Value/255
        RADIUS = component.GetVariableByName('radius')
        if RADIUS:
            light_obj.data.shadow_soft_size = RADIUS.Value
        if component.GetVariableByName('transform'):
            set_blender_object_transform(light_obj, component.GetVariableByName('transform').EngineTransform)
            #TODO should add 90 to X in every spotlight so it matches engine
            rotation_euler = light_obj.rotation_euler
            rotation_euler.x += 1.5708  # 90 degrees in radians
            light_obj.rotation_euler = rotation_euler

        #light_obj.data.spot_blend = component.GetVariableByName('innerAngle').Value
        light_obj.data.spot_blend = 0
        light_obj.data.spot_size = component.GetVariableByName('outerAngle').Value
        #light_obj.data.spot_size = component.GetVariableByName('softness').Value

def import_gameplay_entity(ENTITY_OBJECT, errors, parent_obj = False, keep_lod_meshes = False, **kwargs):
    _raise_if_layer_import_cancelled(kwargs)
    entity_started = time.perf_counter()
    try:
        (mesh_list, cloth_list) = getDataBufferMesh(ENTITY_OBJECT)
    except Exception as e:
        raise e
    nearby_filter = _get_nearby_import_filter(kwargs)
    nearby_stats = _get_nearby_import_stats(kwargs)
    do_import_mesh = bool(kwargs.get("do_import_Mesh", True))
    proxy_filter_active = _proxy_mesh_filter_active(kwargs)
    do_import_proxy_mesh = bool(kwargs.get("do_import_ProxyMesh", False))
    do_import_redcloth = _redcloth_enabled_for_import(kwargs, bpy.context)
    do_import_redapex = _redapex_enabled_for_import(kwargs, bpy.context)
    parent_world_position = kwargs.get("_nearby_parent_position")
    entity_world_position = _entity_world_position(ENTITY_OBJECT, parent_world_position)
    supported_component_names = {
        "CMeshComponent",
        "CStaticMeshComponent",
        "CPointLightComponent",
        "CSpotLightComponent",
    }
    anchor_position = entity_world_position or parent_world_position

    filtered_mesh_list = []
    for mesh in mesh_list:
        _raise_if_layer_import_cancelled(kwargs)
        try:
            mesh_file_name = mesh.fileName() if callable(getattr(mesh, "fileName", None)) else ""
        except Exception:
            mesh_file_name = ""
        is_proxy_mesh = _path_indicates_proxy_mesh(getattr(mesh, "meshName", ""), mesh_file_name)
        if is_proxy_mesh and proxy_filter_active:
            if not do_import_proxy_mesh:
                continue
        elif not do_import_mesh:
            continue
        if not _position_within_nearby_filter(_mesh_world_position(mesh, anchor_position), nearby_filter):
            _note_nearby_filter_skip(nearby_stats)
            continue
        filtered_mesh_list.append(mesh)
    mesh_list = filtered_mesh_list

    filtered_cloth_list = []
    for chunk in cloth_list:
        _raise_if_layer_import_cancelled(kwargs)
        cloth_resource = _chunk_cloth_resource(chunk)
        if _is_redapex_resource(cloth_resource):
            if not do_import_redapex:
                continue
        elif not do_import_redcloth:
            continue
        if not _position_within_nearby_filter(_chunk_world_position(chunk, anchor_position), nearby_filter):
            _note_nearby_filter_skip(nearby_stats)
            continue
        filtered_cloth_list.append(chunk)
    cloth_list = filtered_cloth_list

    eligible_components = []
    for component in (getattr(ENTITY_OBJECT, "Components", None) or []):
        _raise_if_layer_import_cancelled(kwargs)
        component_name = getattr(component, "name", getattr(component, "Type", ""))
        if component_name not in supported_component_names:
            continue
        if not _position_within_nearby_filter(_chunk_world_position(component, anchor_position), nearby_filter):
            _note_nearby_filter_skip(nearby_stats)
            continue
        eligible_components.append(component)
    has_supported_components = bool(eligible_components)
    template = getattr(ENTITY_OBJECT, "template", None)
    has_template_content = bool(
        template is not None
        and (getattr(template, "includes", None) or getattr(template, "Entities", None))
    )
    if not mesh_list and not cloth_list and not has_supported_components and not has_template_content:
        _record_layer_entity_profile(
            kwargs,
            getattr(ENTITY_OBJECT, "name", ""),
            time.perf_counter() - entity_started,
            False,
        )
        return None

    entity_target_collection = kwargs.get("_level_target_collection")
    if import_isolation.is_isolated_import_context(bpy.context):
        entity_target_collection = _get_active_collection(bpy.context) or entity_target_collection
    if entity_target_collection is not None:
        _activate_collection(bpy.context, entity_target_collection)

    #TRANSFORM FOR THIS ENTITY
    bpy.ops.object.empty_add(type="PLAIN_AXES", radius=1)
    empty_transform = bpy.context.object

    if parent_obj:
        empty_transform.name = ENTITY_OBJECT.name+"_SUB" # "CGameplayEntity_empty_transform"
        empty_transform.parent = parent_obj
    else:
        empty_transform.name = ENTITY_OBJECT.name

    imported_any = False
    if mesh_list:
        for mesh in mesh_list:
            _raise_if_layer_import_cancelled(kwargs)
            import_single_mesh(mesh, errors, empty_transform, keep_lod_meshes = keep_lod_meshes, **kwargs)
            imported_any = True
    if cloth_list:
        from ..importers import import_entity
        target_collection = entity_target_collection or _get_active_collection()
        for chunk in cloth_list:
            _raise_if_layer_import_cancelled(kwargs)
            try:
                resource = _chunk_cloth_resource(chunk)
                if _is_redapex_resource(resource):
                    if not do_import_redapex:
                        continue
                elif not do_import_redcloth:
                    continue
                cloth_name = chunk.GetVariableByName('name').String.String
                if _is_redapex_resource(resource):
                    root_obj, _cloth_meshes = import_entity.import_or_reuse_redapex(
                        resource,
                        repo_file(resource),
                        context=bpy.context,
                        loadmods=bool(kwargs.get("loadmods", False)),
                        target_collection=target_collection,
                        **_redapex_import_options(kwargs),
                    )
                    cloth_arma = root_obj
                    cloth_grp = None
                else:
                    cloth_arma, cloth_grp, _cloth_meshes = import_entity.import_or_reuse_redcloth(
                        empty_transform,
                        resource,
                        repo_file(resource),
                        import_name="CClothComponent",
                        entity_name=cloth_name,
                        target_collection=target_collection,
                    )
                    root_obj = cloth_grp if cloth_grp is not None else cloth_arma
                if target_collection is not None:
                    _activate_collection(bpy.context, target_collection)
                if cloth_arma:
                    if root_obj is not None:
                        root_obj.parent = empty_transform
                        try:
                            transform_prop = chunk.GetVariableByName('transform')
                        except Exception:
                            transform_prop = None
                        if transform_prop is not None:
                            try:
                                _apply_engine_transform_local(root_obj, getattr(transform_prop, "EngineTransform", None))
                            except Exception:
                                pass
                    imported_any = True
            except Exception as e:
                resource_label = "redapex" if _is_redapex_resource(resource if 'resource' in locals() else "") else "cloth"
                log.warning("Problem with %s import: %s", resource_label, e)
    
    for component in eligible_components:
        _raise_if_layer_import_cancelled(kwargs)
        import_single_component(component, empty_transform, keep_lod_meshes = keep_lod_meshes, **kwargs)
        imported_any = True
    _tag_entity_empty_engine_visibility_from_children(empty_transform, kwargs)
    #MESH THIS ENTITY HAS
    # for mesh in ENTITY_OBJECT.static_mesh_list:
    #     import_single_mesh(mesh, errors, empty_transform, **kwargs)
    if ENTITY_OBJECT.isCreatedFromTemplate:
        empty_transform['entity_type'] = ENTITY_OBJECT.type
        empty_transform['template'] = ENTITY_OBJECT.templatePath

    
        #TODO work for all animated objects
        if '(CDoor)' in ENTITY_OBJECT.name:
            if _position_within_nearby_filter(entity_world_position, nearby_filter):
                from ..importers import import_entity
                ent_template = import_entity.import_ent_template(ENTITY_OBJECT.template.layerNode, False, 0, empty_transform)
                ent_template.parent = empty_transform
                imported_any = True
            else:
                _note_nearby_filter_skip(nearby_stats)
            pass
        else:
            child_kwargs = dict(kwargs)
            child_kwargs["_nearby_parent_position"] = anchor_position
            child_kwargs["_nearby_filter"] = nearby_filter
            child_kwargs["_nearby_stats"] = nearby_stats
            if ENTITY_OBJECT.template.includes:
                bpy.ops.object.empty_add(type="PLAIN_AXES", radius=1)
                include_transform = bpy.context.object
                include_transform.name = "INCLUDES"
                include_transform.parent = empty_transform
                imported_include_any = False
                for INCLUDE_OBJECT in ENTITY_OBJECT.template.includes:
                    _raise_if_layer_import_cancelled(kwargs)
                    for inc_entity in INCLUDE_OBJECT.Entities:
                        _raise_if_layer_import_cancelled(kwargs)
                        if inc_entity.type in Entity_Type_List:
                            imported_child = import_gameplay_entity(
                                inc_entity,
                                errors,
                                include_transform,
                                keep_lod_meshes = keep_lod_meshes,
                                **child_kwargs,
                            )
                            if imported_child is not None:
                                imported_any = True
                                imported_include_any = True
                if not imported_include_any:
                    try:
                        bpy.data.objects.remove(include_transform, do_unlink=True)
                    except Exception:
                        pass
            for entity in ENTITY_OBJECT.template.Entities:
                _raise_if_layer_import_cancelled(kwargs)
                imported_child = import_gameplay_entity(
                    entity,
                    errors,
                    empty_transform,
                    keep_lod_meshes = keep_lod_meshes,
                    **child_kwargs,
                )
                if imported_child is not None:
                    imported_any = True
                # mesh_list = getDataBufferMesh(entity)
                # for mesh in mesh_list:
                #     import_single_mesh(mesh, errors, empty_transform, **kwargs)
                # for component in entity.Components:
                #     import_single_component(component, empty_transform, **kwargs)

    if ENTITY_OBJECT.transform:
        set_blender_object_transform(empty_transform, ENTITY_OBJECT.transform)

    if not imported_any:
        try:
            bpy.data.objects.remove(empty_transform, do_unlink=True)
        except Exception:
            pass
        _record_layer_entity_profile(
            kwargs,
            getattr(ENTITY_OBJECT, "name", ""),
            time.perf_counter() - entity_started,
            False,
        )
        return None

    _tag_object_tree_for_layer(
        empty_transform,
        kwargs.get("_layer_import_owner"),
        kwargs.get("_layer_import_generation"),
    )
    _record_layer_entity_profile(
        kwargs,
        getattr(ENTITY_OBJECT, "name", ""),
        time.perf_counter() - entity_started,
        True,
    )
    return empty_transform


