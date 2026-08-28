import logging
from dataclasses import dataclass
from typing import Literal
from ..CR2W.witcher_cache.Bundles import BundleItem, LoadBundleManager
log = logging.getLogger(__name__)

import json
import copy
import os
import re
import time
import types
import bpy
import numpy as np
from pathlib import Path
from bpy.app.handlers import persistent

import addon_utils
from .. import (
    clear_external_import_dependency_alert,
    import_rig,
    get_all_addon_prefs,
    get_uncook_path,
    get_W3_REDCLOTH_PATH,
    get_addon_name,
    get_do_import_redcloth,
    get_import_physics_enabled,
    get_rig_rot90_enabled,
    set_external_import_dependency_alert,
)
from ..external_addon_tools import get_apx_addon_status, resolve_redcloth_apx
#from io_import_w2l import settings
from .. import fbx_util
from ..cloth.importer import import_cloth
from ..cloth.materials import apply_redcloth_materials_to_meshes
from ..rigging import armature_merge
from ..rigging import constraints as constrain_util
from ..CR2W import read_json_w3
from ..CR2W import w3_types
from ..CR2W.dc_entity import load_bin_entity
from ..CR2W.dc_entity import LoadCEntityTemplateFile
from ..CR2W.dc_entity import read_entity_template_appearance_metadata as _read_entity_template_appearance_metadata
from ..CR2W.dc_entity import is_valid_mesh_path
from ..CR2W.dc_entity import _resolve_repo_path, _resolve_repo_paths_from_array
from ..CR2W.CR2W_types import EngineTransform
from ..CR2W.prop_utils import prop_to_string, read_enum_prop
from ..animation.camera_tracks import setup_camera_preview_drivers
from ..rigging.attachment import (
    bone_name_from_slot_index,
    coerce_attachment_flags,
    normalize_engine_transform,
)
from ..importers.import_helpers import set_blender_object_transform
from ..importers import import_isolation
from . import entity_effects
from .entity_light import configure_entity_light, orient_red_spot
from ..duplication import duplicate_object_hierarchy
from ..ui.ui_morphs import witcherui_add_redmorph, create_control_bone, create_morph_and_driver
from ..CR2W.common_blender import repo_file, redkit_repo_context, win_safe_path
from ..CR2W.dc_beh import read_beh_info as _read_beh_info, guess_idle as _beh_guess_idle
from ..repo_paths import (
    W2_REPO_ROOT_MARKERS,
    resolve_w2_repo_file_from_source,
    w2_source_repo_root_if_configured,
)
from .dlc_mounters import (
    append_dlc_entity_template_params,
    append_dlc_external_appearances,
    get_dlc_external_appearance_names_for_entity,
    realize_dlc_external_appearance,
)
from .. import get_do_fix_tail
from ..ui.ui_equipment import (
    generate_guid, tag_new_objects_with_guid, remove_objects_by_guid,
    _build_guid_index,
)
from ..ui.armature_context import (
    get_main_armature_and_rig_settings,
    set_main_armature,
)

from mathutils import Euler, Matrix
from math import radians

# def repo_file(filepath: str):
#     if filepath.endswith('.fbx'):
#         return os.path.join(bpy.context.preferences.addons['io_import_w2l'].preferences.fbx_uncook_path, filepath)
#     else:
#         return os.path.join(bpy.context.preferences.addons['io_import_w2l'].preferences.uncook_path, filepath)
#     #repo = "D:/Witcher_uncooked_clean/raw_ent/"
#     #return settings.get().repopath+filepath
addon_name = get_addon_name()
_ENTITY_RUNTIME_CACHE = {}
_BEH_IDLE_BUDGET_MS = 500.0
_BEH_IDLE_TRANSITION_RE = re.compile(r"_to_idle", re.IGNORECASE)
_BEH_IDLE_DOWNGRADE_RE = re.compile(r"additive|lookat|look_at|combat", re.IGNORECASE)
_REDCLOTH_PROFILE_ENABLED = True
_REDCLOTH_PROFILE_WARN_THRESHOLD = 0.10
_REDCLOTH_CACHE_COLLECTION_NAME = "_WitcherRedclothCache"
_FACE_MORPHS_APPEARANCE_PROP = "witcher_face_morphs_loaded_for_appearance"
_REDCLOTH_CACHE_INDEX = {
    "scene_key": None,
    "collection_key": None,
    "dirty": True,
    "roots": {},
}


@persistent
def _clear_entity_cache_on_load(_filepath=""):
    """Clear the runtime entity cache whenever a new .blend file is loaded.

    Memory addresses (as_pointer) and Python ids from the old session are
    meaningless after a file load; clearing here prevents stale cache hits and
    lets the old Entity objects be garbage-collected.
    """
    _ENTITY_RUNTIME_CACHE.clear()
    _invalidate_redcloth_cache_index()


def _register_entity_cache_handler():
    if _clear_entity_cache_on_load not in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.append(_clear_entity_cache_on_load)


def _unregister_entity_cache_handler():
    if _clear_entity_cache_on_load in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_clear_entity_cache_on_load)


_register_entity_cache_handler()


def _log_redcloth_profile_warning(message, *args):
    if not _REDCLOTH_PROFILE_ENABLED:
        return
    log.info("[redcloth-profile] " + str(message), *args)


def _log_redapex_profile(message, *args):
    log.info("[redapex-profile] " + str(message), *args)


def _norm_redcloth_key_path(value) -> str:
    return str(value or "").replace("/", "\\").lower()


def _id_key(data_block):
    if data_block is None:
        return None
    try:
        return int(data_block.as_pointer())
    except Exception:
        return id(data_block)


def _scene_cache_key(scene):
    return _id_key(scene)


def _invalidate_redcloth_cache_index():
    _REDCLOTH_CACHE_INDEX["dirty"] = True


def _make_redcloth_resource_key(resource_path: str) -> str:
    return _norm_redcloth_key_path(resource_path)


def _make_redcloth_reuse_key(resource_path: str, redcloth_mat_path: str) -> str:
    return f"{_make_redcloth_resource_key(resource_path)}|{_norm_redcloth_key_path(redcloth_mat_path)}"


def _is_cloth_resource_path(resource_path: str) -> bool:
    return str(resource_path or "").strip().lower().endswith((".redcloth", ".redapex"))


def _component_import_option(options, name: str, default: bool = True) -> bool:
    if options is None or name not in options:
        return default
    return bool(options[name])


def _entity_chunk_is_proxy_mesh(chunk) -> bool:
    text = f"{chunk.get('mesh', '')}/{chunk.get('name', '')}".replace("\\", "/").lower()
    tokens = []
    for part in (part for part in text.split("/") if part):
        stem = Path(part).stem if "." in part else part
        if stem == "no_proxy" or stem.endswith(("_no_proxy", "-no-proxy")):
            continue
        tokens.extend(token for token in re.split(r"[_\-\s]+", stem) if token)
    return "proxy" in tokens


def _entity_chunk_mesh_enabled(chunk, component_import_options) -> bool:
    if _entity_chunk_is_proxy_mesh(chunk) and component_import_options is not None and "do_import_ProxyMesh" in component_import_options:
        return _component_import_option(component_import_options, "do_import_ProxyMesh", False)
    return _component_import_option(component_import_options, "do_import_Mesh", True)


def _entity_chunk_cloth_enabled(resource_path, component_import_options, import_redcloth_enabled: bool) -> bool:
    if str(resource_path or "").strip().lower().endswith(".redapex"):
        return _component_import_option(
            component_import_options,
            "do_import_Redapex",
            import_redcloth_enabled,
        )
    return import_redcloth_enabled and _component_import_option(
        component_import_options,
        "do_import_Redcloth",
        True,
    )


def _snapshot_collection_object_ids(collection):
    if collection is None:
        return None
    return {
        _id_key(obj)
        for obj in list(getattr(collection, "all_objects", []) or [])
    }


def _tag_new_collection_objects_with_guid(collection, before_ids, guid, prop_name):
    if collection is None or before_ids is None:
        return None
    from ..ui.ui_equipment import _is_internal_inventory_group_object
    tagged_objects = set()
    for obj in list(getattr(collection, "all_objects", []) or []):
        if _id_key(obj) in before_ids:
            continue
        if _is_internal_inventory_group_object(obj):
            continue
        obj[prop_name] = guid
        tagged_objects.add(obj)
    return tagged_objects


def _get_chunk_component_name(chunk) -> str:
    component_name = str(chunk.get("name", "") or "").strip()
    if component_name:
        return component_name
    resource_path = str(chunk.get("resource", "") or "").strip()
    if _is_cloth_resource_path(resource_path):
        return Path(resource_path.replace("/", "\\")).stem
    return ""


def _mesh_uses_armature(obj, armature_obj) -> bool:
    if obj is None or obj.type != 'MESH' or armature_obj is None:
        return False
    for mod in getattr(obj, "modifiers", []):
        if mod.type == 'ARMATURE' and mod.object == armature_obj:
            return True
    return False


def get_entity_mesh_import_settings(source=None) -> dict:
    settings = {
        "keep_lod_meshes": False,
        "keep_empty_lods": False,
        "keep_proxy_meshes": False,
        "hide_zero_weight_faces": True,
        "build_material_nodes": True,
        "import_morphs": True,
    }
    if source is None:
        return settings

    if isinstance(source, dict):
        getter = lambda name, default=None: source.get(name, default)
    else:
        def getter(name, default=None):
            value = getattr(source, name, None)
            if value is not None:
                return value
            if hasattr(source, "get"):
                try:
                    return source.get(name, default)
                except Exception:
                    return default
            return default

    source_names = {
        "keep_lod_meshes": ("keep_lod_meshes", "do_import_lods"),
        "keep_empty_lods": ("keep_empty_lods",),
        "keep_proxy_meshes": ("keep_proxy_meshes",),
        "hide_zero_weight_faces": ("hide_zero_weight_faces",),
        "build_material_nodes": ("build_material_nodes",),
        "import_morphs": ("import_morphs", "load_morphs"),
    }
    for key, candidate_names in source_names.items():
        for candidate_name in candidate_names:
            value = getter(candidate_name, None)
            if value is not None:
                settings[key] = bool(value)
                break
    return settings


def apply_entity_mesh_import_settings(rig_settings, settings=None) -> dict:
    normalized = get_entity_mesh_import_settings(settings if settings is not None else rig_settings)
    if rig_settings is None:
        return normalized

    rig_settings.do_import_lods = normalized["keep_lod_meshes"]
    rig_settings.keep_empty_lods = normalized["keep_empty_lods"]
    rig_settings.keep_proxy_meshes = normalized["keep_proxy_meshes"]
    rig_settings.hide_zero_weight_faces = normalized["hide_zero_weight_faces"]
    if hasattr(rig_settings, "build_material_nodes"):
        rig_settings.build_material_nodes = normalized["build_material_nodes"]
    else:
        try:
            rig_settings["build_material_nodes"] = normalized["build_material_nodes"]
        except Exception:
            pass
    try:
        rig_settings["import_morphs"] = normalized["import_morphs"]
    except Exception:
        pass
    return normalized


def _iter_tagged_redcloth_meshes_from_carrier(carrier_obj):
    if carrier_obj is None or not hasattr(carrier_obj, "get"):
        return
    seen_names = set()

    def _yield_named_mesh(name):
        mesh_name = str(name or "").strip()
        if not mesh_name or mesh_name in seen_names:
            return
        seen_names.add(mesh_name)
        mesh_obj = bpy.data.objects.get(mesh_name)
        if mesh_obj is not None and mesh_obj.type == 'MESH':
            yield mesh_obj

    mesh_name = carrier_obj.get("witcher_redcloth_mesh_name", "")
    if mesh_name:
        yield from _yield_named_mesh(mesh_name)

    raw_mesh_names = carrier_obj.get("witcher_redcloth_mesh_names", "")
    if not raw_mesh_names:
        return
    try:
        mesh_names = json.loads(raw_mesh_names)
    except Exception:
        mesh_names = [raw_mesh_names]
    if not isinstance(mesh_names, (list, tuple)):
        mesh_names = [mesh_names]
    for name in mesh_names:
        yield from _yield_named_mesh(name)


def _get_redcloth_tag_targets(cloth_armature):
    targets = []
    if cloth_armature is not None:
        targets.append(cloth_armature)
        parent = getattr(cloth_armature, "parent", None)
        if parent is not None:
            targets.append(parent)
    return targets


def _get_tagged_redcloth_meshes(cloth_armature):
    meshes = []
    seen = set()
    for carrier in _get_redcloth_tag_targets(cloth_armature):
        for mesh in _iter_tagged_redcloth_meshes_from_carrier(carrier):
            mesh_id = id(mesh)
            if mesh_id in seen:
                continue
            seen.add(mesh_id)
            meshes.append(mesh)
    return meshes


def _collect_redcloth_meshes(cloth_armature):
    meshes = []
    seen = set()

    def _add_mesh(mesh_obj):
        if mesh_obj is None or mesh_obj.type != 'MESH':
            return
        mesh_id = id(mesh_obj)
        if mesh_id in seen:
            return
        seen.add(mesh_id)
        meshes.append(mesh_obj)

    if getattr(cloth_armature, "type", None) == 'MESH':
        _add_mesh(cloth_armature)

    for obj in _iter_object_descendants(cloth_armature):
        if obj.type == 'MESH':
            _add_mesh(obj)

    for mesh in _get_tagged_redcloth_meshes(cloth_armature):
        _add_mesh(mesh)

    if meshes:
        return meshes

    collections = set(getattr(cloth_armature, "users_collection", []))
    parent = getattr(cloth_armature, "parent", None)
    if parent is not None:
        collections.update(getattr(parent, "users_collection", []))
    for collection in collections:
        for obj in getattr(collection, "all_objects", []):
            if _mesh_uses_armature(obj, cloth_armature):
                _add_mesh(obj)
    return meshes


def build_component_mesh_index_in_hierarchy(root_obj):
    if root_obj is None:
        return {}

    component_mesh_index = {}
    seen_mesh_ids = set()
    stack = [root_obj]
    seen_objects = set()

    def _add_mesh(mesh_obj):
        if mesh_obj is None or mesh_obj.type != 'MESH':
            return
        component_name = str(mesh_obj.get('witcher_name', '') or '').strip()
        if not component_name:
            return
        mesh_id = id(mesh_obj)
        if mesh_id in seen_mesh_ids:
            return
        seen_mesh_ids.add(mesh_id)
        component_mesh_index.setdefault(component_name, []).append(mesh_obj)

    while stack:
        obj = stack.pop()
        if obj is None:
            continue
        obj_id = id(obj)
        if obj_id in seen_objects:
            continue
        seen_objects.add(obj_id)

        _add_mesh(obj)
        for mesh in _iter_tagged_redcloth_meshes_from_carrier(obj):
            _add_mesh(mesh)
        stack.extend(list(getattr(obj, "children", [])))

    return component_mesh_index


def find_component_meshes_in_hierarchy(root_obj, component_name):
    component_name = str(component_name or "").strip()
    if root_obj is None or not component_name:
        return []
    return build_component_mesh_index_in_hierarchy(root_obj).get(component_name, [])


def _iter_object_descendants(root_obj):
    if root_obj is None:
        return
    stack = list(getattr(root_obj, "children", []))
    seen = set()
    while stack:
        obj = stack.pop()
        if obj is None:
            continue
        obj_id = id(obj)
        if obj_id in seen:
            continue
        seen.add(obj_id)
        yield obj
        stack.extend(list(getattr(obj, "children", [])))


def _is_redcloth_collision_helper(obj) -> bool:
    try:
        if obj.get("witcher_apx_collision_proxy", False):
            return True
        object_name = str(getattr(obj, "name", "") or "").lower()
        parent_name = str(getattr(getattr(obj, "parent", None), "name", "") or "").lower()
        return any(
            label in object_name or label in parent_name
            for label in (
                "collision spheres",
                "collision connections",
                "collision capsules",
                "collision proxies",
            )
        )
    except Exception:
        return False


def _set_redcloth_collision_helper_visibility(root_obj, hidden: bool):
    if root_obj is None:
        return
    for obj in (root_obj, *_iter_object_descendants(root_obj)):
        if not _is_redcloth_collision_helper(obj):
            continue
        try:
            # The Outliner eye controls the viewport; helpers never render.
            obj.hide_viewport = False
            obj.hide_set(bool(hidden))
            obj.hide_render = True
        except Exception:
            continue


def _find_reusable_redcloth_armature(owner_armature, reuse_key: str, *, key_prop: str = "witcher_redcloth_reuse_key"):
    if owner_armature is None or not reuse_key:
        return None
    for obj in _iter_object_descendants(owner_armature):
        try:
            if obj.type != 'ARMATURE':
                continue
            if obj.get(key_prop) == reuse_key:
                return obj
        except Exception:
            continue
    return None


def _configure_redcloth_cache_collection(cache_collection):
    if cache_collection is None:
        return
    try:
        # Keep the unlinked cache collection alive across save/reload.
        cache_collection.use_fake_user = True
    except Exception:
        pass
    try:
        cache_collection.hide_render = True
    except Exception:
        pass
    try:
        cache_collection.hide_viewport = True
    except Exception:
        pass
    try:
        cache_collection["witcher_redcloth_cache"] = True
    except Exception:
        pass


def _unlink_redcloth_cache_from_scenes(cache_collection):
    # Migration: older versions linked the cache into the scene, polluting the outliner.
    if cache_collection is None:
        return
    for scene in bpy.data.scenes:
        try:
            scene_children = scene.collection.children
            if cache_collection.name in scene_children.keys():
                scene_children.unlink(cache_collection)
        except Exception:
            continue


def _get_redcloth_cache_collection(scene, create: bool = False):
    if scene is None:
        return None
    cache_collection = bpy.data.collections.get(_REDCLOTH_CACHE_COLLECTION_NAME)
    if cache_collection is None and not create:
        return None
    if cache_collection is None:
        cache_collection = bpy.data.collections.new(_REDCLOTH_CACHE_COLLECTION_NAME)
    _unlink_redcloth_cache_from_scenes(cache_collection)
    _configure_redcloth_cache_collection(cache_collection)
    return cache_collection


def _iter_redcloth_cache_objects(scene):
    cache_collection = _get_redcloth_cache_collection(scene, create=False)
    if cache_collection is not None:
        for obj in getattr(cache_collection, "all_objects", []) or []:
            yield obj
        return


def _find_reusable_redcloth_root(scene, resource_key: str):
    if scene is None or not resource_key:
        return None
    cache_collection = _get_redcloth_cache_collection(scene, create=False)
    if cache_collection is None:
        return None

    scene_key = _scene_cache_key(scene)
    collection_key = _id_key(cache_collection)
    if (
        _REDCLOTH_CACHE_INDEX.get("dirty")
        or _REDCLOTH_CACHE_INDEX.get("scene_key") != scene_key
        or _REDCLOTH_CACHE_INDEX.get("collection_key") != collection_key
    ):
        roots = {}
        for obj in _iter_redcloth_cache_objects(scene):
            try:
                if not obj.get("witcher_redcloth_cache_root", False):
                    continue
                key = str(obj.get("witcher_redcloth_resource_key", "") or "").strip()
                if key:
                    roots[key] = obj
            except Exception:
                continue
        _REDCLOTH_CACHE_INDEX["scene_key"] = scene_key
        _REDCLOTH_CACHE_INDEX["collection_key"] = collection_key
        _REDCLOTH_CACHE_INDEX["dirty"] = False
        _REDCLOTH_CACHE_INDEX["roots"] = roots

    obj = (_REDCLOTH_CACHE_INDEX.get("roots") or {}).get(resource_key)
    if obj is not None:
        try:
            if obj.name in bpy.data.objects and obj.get("witcher_redcloth_cache_root", False):
                return obj
        except Exception:
            pass
        _REDCLOTH_CACHE_INDEX["dirty"] = True
    return None


def _get_reusable_redcloth_root(cloth_armature, resource_key: str):
    if cloth_armature is None:
        return None
    parent = getattr(cloth_armature, "parent", None)
    if parent is not None:
        try:
            if parent.get("witcher_redcloth_resource_key") == resource_key:
                return parent
        except Exception:
            pass
    return cloth_armature


def _resolve_redcloth_armature_from_root(root_obj, resource_key: str):
    if root_obj is None:
        return None, None
    if getattr(root_obj, "type", "") == 'ARMATURE':
        return root_obj, None
    cloth_arma = _find_reusable_redcloth_armature(
        root_obj,
        resource_key,
        key_prop="witcher_redcloth_resource_key",
    )
    if cloth_arma is not None:
        return cloth_arma, root_obj
    return None, root_obj if getattr(root_obj, "type", "") == 'EMPTY' else None


def _clear_redcloth_cache_root_flag(root_obj):
    if root_obj is None:
        return
    try:
        if "witcher_redcloth_cache_root" in root_obj:
            del root_obj["witcher_redcloth_cache_root"]
    except Exception:
        try:
            root_obj["witcher_redcloth_cache_root"] = False
        except Exception:
            pass


def _tag_redcloth_for_reuse(cloth_armature, reuse_key: str, resource_key: str, resource_path: str, redcloth_mat_path: str):
    if cloth_armature is None:
        return
    targets = [cloth_armature]
    parent = getattr(cloth_armature, "parent", None)
    if parent is not None:
        targets.append(parent)
    for obj in targets:
        try:
            obj["witcher_redcloth_reuse_key"] = reuse_key
            obj["witcher_redcloth_resource_key"] = resource_key
            obj["witcher_redcloth_resource"] = resource_path or ""
            obj["witcher_redcloth_material"] = redcloth_mat_path or ""
        except Exception:
            pass


def _tag_redcloth_reuse_root(root_obj, reuse_key: str, resource_key: str, resource_path: str, redcloth_mat_path: str):
    if root_obj is None:
        return
    try:
        root_obj["witcher_redcloth_cache_root"] = True
        root_obj["witcher_redcloth_reuse_key"] = reuse_key
        root_obj["witcher_redcloth_resource_key"] = resource_key
        root_obj["witcher_redcloth_resource"] = resource_path or ""
        root_obj["witcher_redcloth_material"] = redcloth_mat_path or ""
    except Exception:
        pass


def _get_redcloth_stored_material_path(cloth_armature) -> str:
    if cloth_armature is None:
        return ""
    for obj in (cloth_armature, getattr(cloth_armature, "parent", None)):
        if obj is None:
            continue
        try:
            value = str(obj.get("witcher_redcloth_material", "") or "").strip()
        except Exception:
            value = ""
        if value:
            return value
    return ""


def _refresh_reused_redcloth_materials(cloth_armature, redcloth_resource: str, redcloth_mat_path: str) -> float:
    requested_mat = _norm_redcloth_key_path(redcloth_mat_path)
    cached_mat = _norm_redcloth_key_path(_get_redcloth_stored_material_path(cloth_armature))
    if not requested_mat or requested_mat == cached_mat:
        return 0.0
    cloth_meshes = _collect_redcloth_meshes(cloth_armature)
    refresh_stats = apply_redcloth_materials_to_meshes(
        cloth_meshes,
        redcloth_resource,
        redcloth_mat_path,
        context=bpy.context,
        apply_runtime_defaults=True,
    )
    refresh_seconds = float(refresh_stats.get("read_seconds", 0.0) or 0.0) + float(refresh_stats.get("apply_seconds", 0.0) or 0.0)
    if refresh_seconds >= _REDCLOTH_PROFILE_WARN_THRESHOLD:
        _log_redcloth_profile_warning(
            "material refresh %s %.3fs (cached %s, requested %s, meshes %d, slots %d)",
            Path(redcloth_resource.replace("/", "\\")).name or redcloth_resource,
            refresh_seconds,
            Path(cached_mat).name or cached_mat or "<none>",
            Path(requested_mat).name or requested_mat or "<none>",
            len(cloth_meshes),
            int(refresh_stats.get("material_count", 0) or 0),
        )
    return refresh_seconds


def _seed_redcloth_cache_root(scene, source_root, reuse_key: str, resource_key: str, resource_path: str, redcloth_mat_path: str):
    if scene is None or source_root is None or not resource_key:
        return None
    existing_root = _find_reusable_redcloth_root(scene, resource_key)
    if existing_root is not None:
        return existing_root
    cache_collection = _get_redcloth_cache_collection(scene, create=True)
    if cache_collection is None:
        return None
    duplicate_root = duplicate_object_hierarchy(
        bpy.context,
        source_root,
        target_collection=cache_collection,
        link_to_source_collections=False,
    )
    if duplicate_root is None:
        return None
    _clear_redcloth_cache_root_flag(duplicate_root)
    cache_armature, _cache_group = _resolve_redcloth_armature_from_root(duplicate_root, resource_key)
    if cache_armature is not None:
        _tag_redcloth_for_reuse(cache_armature, reuse_key, resource_key, resource_path, redcloth_mat_path)
    cache_root = _get_reusable_redcloth_root(cache_armature or duplicate_root, resource_key) or duplicate_root
    _tag_redcloth_reuse_root(cache_root, reuse_key, resource_key, resource_path, redcloth_mat_path)
    if _REDCLOTH_CACHE_INDEX.get("scene_key") == _scene_cache_key(scene):
        _REDCLOTH_CACHE_INDEX.setdefault("roots", {})[resource_key] = cache_root
        _REDCLOTH_CACHE_INDEX["collection_key"] = _id_key(cache_collection)
        _REDCLOTH_CACHE_INDEX["dirty"] = False
    return cache_root


_REDCLOTH_FAILED_IMPORTS = set()


def clear_redcloth_failure_cache():
    _REDCLOTH_FAILED_IMPORTS.clear()


_ERROR_PLACEHOLDER_MESH = "W3_ERR_placeholder"
_ERROR_PLACEHOLDER_MAT = "W3_ERR_placeholder_mat"


def _get_error_placeholder_mesh():
    mesh = bpy.data.meshes.get(_ERROR_PLACEHOLDER_MESH)
    if mesh is not None:
        return mesh
    mesh = bpy.data.meshes.new(_ERROR_PLACEHOLDER_MESH)
    s = 0.25
    verts = [(-s, -s, 0), (s, -s, 0), (s, s, 0), (-s, s, 0),
             (-s, -s, 2 * s), (s, -s, 2 * s), (s, s, 2 * s), (-s, s, 2 * s)]
    faces = [(0, 1, 2, 3), (7, 6, 5, 4), (0, 4, 5, 1), (1, 5, 6, 2), (2, 6, 7, 3), (3, 7, 4, 0)]
    mesh.from_pydata(verts, [], faces)
    mat = bpy.data.materials.get(_ERROR_PLACEHOLDER_MAT)
    if mat is None:
        mat = bpy.data.materials.new(_ERROR_PLACEHOLDER_MAT)
        mat.diffuse_color = (1.0, 0.0, 1.0, 1.0)
        mat.use_nodes = True
        bsdf = mat.node_tree.nodes.get("Principled BSDF")
        if bsdf is not None:
            bsdf.inputs["Base Color"].default_value = (1.0, 0.0, 1.0, 1.0)
    mesh.materials.append(mat)
    return mesh


def make_import_error_placeholder(resource, target_collection=None):
    label = Path(str(resource or "").replace("/", "\\")).name or "import"
    obj = bpy.data.objects.new(f"ERR_{label}", _get_error_placeholder_mesh())
    obj["witcher_import_error"] = str(resource or "")
    obj.color = (1.0, 0.0, 1.0, 1.0)
    coll = target_collection
    if coll is None:
        coll = getattr(bpy.context, "collection", None) or bpy.context.scene.collection
    try:
        coll.objects.link(obj)
    except Exception:
        pass
    return obj


def import_or_reuse_redcloth(
    owner_armature,
    redcloth_resource: str,
    redcloth_mat_path: str,
    *,
    import_name: str,
    entity_name: str,
    target_collection=None,
    hide_collision_proxies=True,
):
    total_started = time.perf_counter()
    resolve_seconds = 0.0
    import_seconds = 0.0
    activate_seconds = 0.0
    collect_seconds = 0.0
    material_refresh_seconds = 0.0
    reused = False
    imported = False
    redcloth_resource = str(redcloth_resource or "").strip()
    redcloth_mat_path = str(redcloth_mat_path or "").strip()
    if not redcloth_resource or not redcloth_mat_path:
        return None, None, []

    redcloth_resource_key = _make_redcloth_resource_key(redcloth_resource)
    redcloth_reuse_key = _make_redcloth_reuse_key(redcloth_resource, redcloth_mat_path)
    if redcloth_reuse_key in _REDCLOTH_FAILED_IMPORTS:
        log.debug("Skipping redcloth %s: import failed earlier this session", redcloth_resource)
        return make_import_error_placeholder(redcloth_resource, target_collection), None, []
    scene = getattr(bpy.context, "scene", None)
    cloth_arma = _find_reusable_redcloth_armature(owner_armature, redcloth_reuse_key)
    if cloth_arma is not None:
        reused = True
        log.info("Reusing redcloth import for %s", redcloth_resource)
    else:
        cloth_grp = None
        reusable_root = _find_reusable_redcloth_root(scene, redcloth_resource_key)
        if reusable_root is not None:
            duplicate_root = duplicate_object_hierarchy(
                bpy.context,
                reusable_root,
                target_collection=target_collection,
                link_to_source_collections=False,
            )
            _clear_redcloth_cache_root_flag(duplicate_root)
            cloth_arma, cloth_grp = _resolve_redcloth_armature_from_root(duplicate_root, redcloth_resource_key)
            if cloth_arma is not None:
                reused = True
                log.info("Reusing global redcloth import for %s", redcloth_resource)
                material_refresh_seconds = _refresh_reused_redcloth_materials(
                    cloth_arma,
                    redcloth_resource,
                    redcloth_mat_path,
                )
                _tag_redcloth_for_reuse(
                    cloth_arma,
                    redcloth_reuse_key,
                    redcloth_resource_key,
                    redcloth_resource,
                    redcloth_mat_path,
                )
        if cloth_arma is not None:
            imported = False
        else:
            resolve_started = time.perf_counter()
            apx_info = resolve_redcloth_apx(bpy.context, redcloth_resource, loadmods=False)
            resolve_seconds = time.perf_counter() - resolve_started
            apx_path = apx_info.get("apx_path", "")
            if not apx_path or not os.path.isfile(apx_path):
                apx_status = get_apx_addon_status(bpy.context)
                if not apx_status["enabled"]:
                    set_external_import_dependency_alert(
                        "redcloth",
                        source_path=redcloth_resource,
                        status="apx_addon_disabled",
                        reason=apx_info.get("message") or "io_mesh_apx addon is not enabled.",
                    )
                elif not apx_status["sdk_ready"]:
                    set_external_import_dependency_alert(
                        "redcloth",
                        source_path=redcloth_resource,
                        status="apx_sdk_missing",
                        reason=apx_info.get("message") or "APX SDK CLI path is not configured or does not exist.",
                    )
                log.warning(
                    "Skipping redcloth import for %s: %s",
                    redcloth_resource,
                    apx_info.get("message") or apx_info.get("status"),
                )
                cloth_arma = None
            else:
                try:
                    if target_collection is not None:
                        activate_started = time.perf_counter()
                        _activate_target_collection(bpy.context, target_collection)
                        activate_seconds = time.perf_counter() - activate_started
                    import_started = time.perf_counter()
                    cloth_arma = import_cloth(
                        False,
                        apx_path,
                        True,
                        False,
                        True,
                        redcloth_mat_path,
                    )
                    import_seconds = time.perf_counter() - import_started
                    imported = cloth_arma is not None
                    if cloth_arma is None:
                        legacy_exists, legacy_enabled = addon_utils.check("io_scene_apx")
                        apx_status = get_apx_addon_status(bpy.context)
                        if not apx_status["enabled"] and not bool(legacy_enabled):
                            set_external_import_dependency_alert(
                                "redcloth",
                                source_path=redcloth_resource,
                                status="apx_addon_disabled",
                                reason="io_mesh_apx (or legacy io_scene_apx) addon is not enabled.",
                            )
                        log.warning("Redcloth import returned no object for %s", redcloth_resource)
                except Exception as e:
                    import_seconds = time.perf_counter() - import_started
                    apx_status = get_apx_addon_status(bpy.context)
                    if not apx_status["enabled"]:
                        legacy_exists, legacy_enabled = addon_utils.check("io_scene_apx")
                        if not bool(legacy_enabled):
                            set_external_import_dependency_alert(
                                "redcloth",
                                source_path=redcloth_resource,
                                status="apx_addon_disabled",
                                reason="io_mesh_apx (or legacy io_scene_apx) addon is not enabled.",
                            )
                    log.warning("Redcloth import failed for %s: %s", redcloth_resource, e)
                    cloth_arma = None

    if cloth_arma is None:
        _REDCLOTH_FAILED_IMPORTS.add(redcloth_reuse_key)
        total_seconds = time.perf_counter() - total_started
        if total_seconds >= _REDCLOTH_PROFILE_WARN_THRESHOLD:
            _log_redcloth_profile_warning(
                "entity %s total %.3fs (resolve %.3fs, import %.3fs, activate %.3fs, material_refresh %.3fs, collect %.3fs, reused %s, imported %s, ok no)",
                Path(redcloth_resource.replace("/", "\\")).name or redcloth_resource,
                total_seconds,
                resolve_seconds,
                import_seconds,
                activate_seconds,
                material_refresh_seconds,
                collect_seconds,
                "yes" if reused else "no",
                "yes" if imported else "no",
            )
        return make_import_error_placeholder(redcloth_resource, target_collection), None, []

    clear_external_import_dependency_alert("redcloth")
    cloth_grp = None
    if cloth_arma.type == 'EMPTY':
        cloth_grp = cloth_arma
        for child in cloth_arma.children:
            if child.type == 'ARMATURE':
                cloth_arma = child
                break
    _tag_redcloth_for_reuse(
        cloth_arma,
        redcloth_reuse_key,
        redcloth_resource_key,
        redcloth_resource,
        redcloth_mat_path,
    )
    visible_root = _get_reusable_redcloth_root(cloth_arma, redcloth_resource_key)
    _clear_redcloth_cache_root_flag(visible_root)
    if imported:
        # Cache the pristine hierarchy before applying per-use visibility.
        _seed_redcloth_cache_root(
            scene,
            visible_root,
            redcloth_reuse_key,
            redcloth_resource_key,
            redcloth_resource,
            redcloth_mat_path,
        )
    _set_redcloth_collision_helper_visibility(visible_root, hide_collision_proxies)
    collect_started = time.perf_counter()
    cloth_meshes = _collect_redcloth_meshes(cloth_arma)
    collect_seconds = time.perf_counter() - collect_started
    total_seconds = time.perf_counter() - total_started
    if total_seconds >= _REDCLOTH_PROFILE_WARN_THRESHOLD:
        _log_redcloth_profile_warning(
            "entity %s total %.3fs (resolve %.3fs, import %.3fs, activate %.3fs, material_refresh %.3fs, collect %.3fs, reused %s, imported %s, meshes %d, ok yes)",
            Path(redcloth_resource.replace("/", "\\")).name or redcloth_resource,
            total_seconds,
            resolve_seconds,
            import_seconds,
            activate_seconds,
            material_refresh_seconds,
            collect_seconds,
            "yes" if reused else "no",
            "yes" if imported else "no",
            len(cloth_meshes),
        )
    return cloth_arma, cloth_grp, cloth_meshes


def import_or_reuse_redapex(
    redapex_resource: str,
    redapex_mat_path: str,
    *,
    target_collection=None,
    context=None,
    loadmods=False,
    import_chunks=False,
    import_floor=False,
    collections_as_empties=True,
):
    total_started = time.perf_counter()
    cache_lookup_seconds = 0.0
    duplicate_seconds = 0.0
    import_seconds = 0.0
    reused = False
    imported = False
    redapex_resource = str(redapex_resource or "").strip()
    redapex_mat_path = str(redapex_mat_path or "").strip()
    if not redapex_resource or not redapex_mat_path:
        return None, []

    ctx = context or bpy.context
    scene = getattr(ctx, "scene", None) or getattr(bpy.context, "scene", None)
    resource_key = _make_redcloth_resource_key(redapex_resource)
    reuse_key = _make_redcloth_reuse_key(redapex_resource, redapex_mat_path)
    root_obj = None

    cache_started = time.perf_counter()
    reusable_root = _find_reusable_redcloth_root(scene, resource_key)
    cache_lookup_seconds = time.perf_counter() - cache_started
    if reusable_root is not None:
        duplicate_started = time.perf_counter()
        root_obj = duplicate_object_hierarchy(
            ctx,
            reusable_root,
            target_collection=target_collection,
            link_to_source_collections=False,
        )
        duplicate_seconds = time.perf_counter() - duplicate_started
        _clear_redcloth_cache_root_flag(root_obj)
        reused = root_obj is not None
        if reused:
            log.info("Reusing global redapex import for %s", redapex_resource)

    if root_obj is None:
        if target_collection is not None:
            _activate_target_collection(ctx, target_collection)
        import_started = time.perf_counter()
        from ..ui.ui_mesh import import_redapex_resource
        root_obj = import_redapex_resource(
            ctx,
            redapex_mat_path,
            repo_path=redapex_resource,
            loadmods=loadmods,
            target_collection=target_collection,
            import_chunks=import_chunks,
            import_floor=import_floor,
            collections_as_empties=collections_as_empties,
        )
        import_seconds = time.perf_counter() - import_started
        imported = root_obj is not None

    if root_obj is None:
        return None, []

    _tag_redcloth_for_reuse(
        root_obj,
        reuse_key,
        resource_key,
        redapex_resource,
        redapex_mat_path,
    )
    _clear_redcloth_cache_root_flag(root_obj)
    try:
        root_obj["witcher_redcloth_resource_type"] = "redapex"
        root_obj["witcher_layer_visibility_kind"] = "redapex"
        root_obj["witcher_cached_plan_kind"] = "redapex"
        root_obj["repo_path"] = redapex_resource
    except Exception:
        pass

    if imported:
        _seed_redcloth_cache_root(
            scene,
            root_obj,
            reuse_key,
            resource_key,
            redapex_resource,
            redapex_mat_path,
        )

    meshes = _collect_redcloth_meshes(root_obj)
    total_seconds = time.perf_counter() - total_started
    if total_seconds >= _REDCLOTH_PROFILE_WARN_THRESHOLD:
        _log_redapex_profile(
            "reuse %s total %.3fs (cache %.3fs, duplicate %.3fs, import %.3fs, reused %s, imported %s, meshes %d, ok yes)",
            Path(redapex_resource.replace("/", "\\")).name or redapex_resource,
            total_seconds,
            cache_lookup_seconds,
            duplicate_seconds,
            import_seconds,
            "yes" if reused else "no",
            "yes" if imported else "no",
            len(meshes),
        )
    return root_obj, meshes


def _build_coloring_entry_lookup(coloring_entries, appearance_name):
    if not coloring_entries:
        return {}

    lookup = {}
    for entry in coloring_entries:
        try:
            entry_app = str(entry['appearance'] or "")
            # Entries with a specific appearance must match; entries with no
            # appearance (empty string) apply to all appearances.
            if entry_app and (not appearance_name or entry_app != appearance_name):
                continue
            component_name = str(entry['componentName'] or "")
            if component_name:
                lookup[component_name] = entry
        except Exception:
            continue
    return lookup


def _set_idprop_value(obj, key, value) -> bool:
    current_value = obj.get(key)
    if value is None:
        if current_value is None:
            return False
        obj.pop(key, None)
        return True
    if current_value == value:
        return False
    obj[key] = value
    return True


def _apply_coloring_entry_to_object(obj, entry):
    changed = False
    cs1 = entry.get('colorShift1') if entry is not None else None
    cs2 = entry.get('colorShift2') if entry is not None else None

    changed |= _set_idprop_value(obj, 'colorShift1_hue', cs1['hue'] if cs1 is not None else None)
    changed |= _set_idprop_value(obj, 'colorShift1_saturation', cs1['saturation'] if cs1 is not None else None)
    changed |= _set_idprop_value(obj, 'colorShift1_luminance', cs1['luminance'] if cs1 is not None else None)
    changed |= _set_idprop_value(obj, 'colorShift2_hue', cs2['hue'] if cs2 is not None else None)
    changed |= _set_idprop_value(obj, 'colorShift2_saturation', cs2['saturation'] if cs2 is not None else None)
    changed |= _set_idprop_value(obj, 'colorShift2_luminance', cs2['luminance'] if cs2 is not None else None)

    if changed:
        obj.update_tag()


def _apply_coloring_lookup_to_objects(objects, coloring_lookup):
    if not objects:
        return
    for obj in objects:
        if obj is None or obj.type != 'MESH':
            continue
        component_name = obj.get('witcher_name', '')
        if not component_name:
            continue
        _apply_coloring_entry_to_object(obj, coloring_lookup.get(component_name))

def fixed_chunk_paths(entity, version = 999):
    use_fbx = False
    ext = ".fbx" if use_fbx else ".w2mesh"
    suffix ="" #"_CONVERT_"
    entity.MovingPhysicalAgentComponent.skeleton = repo_file(entity.MovingPhysicalAgentComponent.skeleton, version)#+".json";

    for appearance in entity.appearances:
        for template in appearance.includedTemplates:
            for chunk in template['chunks']:
                if "mesh" in chunk:
                    chunk['mesh'] = repo_file(chunk['mesh'].replace(".w2mesh", suffix+ext), version)
                if chunk['type'] == "CClothComponent":
                    resource = chunk['resource']
                    chunk['resource'] = repo_file(resource, version)
                    chunk['resource_apx'] = os.path.join(
                        get_W3_REDCLOTH_PATH(bpy.context),
                        os.path.splitext(resource)[0] + ".apx",
                    )
                if "morphSource" in chunk:
                    chunk['morphSource'] = repo_file(chunk['morphSource'].replace(".w2mesh", suffix+ext), version)
                if "morphTarget" in chunk:
                    chunk['morphTarget'] = repo_file(chunk['morphTarget'].replace(".w2mesh", suffix+ext), version)
                if "skeleton" in chunk and chunk['skeleton'] != None:
                    chunk['skeleton'] = repo_file(chunk['skeleton'], version)#+".json"
                if "dyng" in chunk and chunk['dyng'] != None:
                    chunk['dyng'] = repo_file(chunk['dyng'], version)#+".json"
                if "mimicFace" in chunk:
                    chunk['mimicFace'] = repo_file(chunk['mimicFace'], version)#+".json"
    if entity.staticMeshes:
        for chunk in entity.staticMeshes.get('chunks', []):
            if "mesh" in chunk:
                chunk['mesh'] = repo_file(chunk['mesh'].replace(".w2mesh", suffix+ext), version)
            if "skeleton" in chunk and chunk['skeleton'] != None:
                chunk['skeleton'] = repo_file(chunk['skeleton'], version)#+".json"
            if "dyng" in chunk and chunk['dyng'] != None:
                chunk['dyng'] = repo_file(chunk['dyng'], version)#+".json"
            if chunk['type'] == 'CHardAttachment':
                pass
    return entity

def isChildNode(chunkIndex, templateChunks):
    for chunk in templateChunks:
        if "child" in chunk and chunk['child'] == chunkIndex:
            return True
    return False

def GetChunkNS(chunkIndex, templateChunks, index):
    for chunk in templateChunks:
        if chunk['chunkIndex'] == chunkIndex:
            return chunk['type']+str(index)+str(chunk['chunkIndex'])

#global GLOBAL_appearances
def NewListItem( treeList, node):
    item = treeList.add()
    item.name = node.name
    return item

def NewAnimsetListItem( treeList, path, name, component_name="", source_game="w3"):
    item = treeList.add()
    if path:
        item.path = path
    if name:
        item.name = name
    if component_name and hasattr(item, "component_name"):
        item.component_name = component_name
    if hasattr(item, "source_game"):
        item.source_game = "w2" if str(source_game or "").strip().lower() == "w2" else "w3"
    return item


def _rig_settings_cache_key(rig_settings):
    """Return a stable cache key for rig_settings.

    Uses the owning armature data-block name as the primary key so the key
    survives undo steps and file reloads (which recreate C pointers).  The
    pointer is appended as a session-local tiebreaker for the rare case of
    two data-blocks sharing a name within one session.
    """
    if rig_settings is None:
        return None
    try:
        owner_name = rig_settings.id_data.name
    except Exception:
        owner_name = ""
    try:
        ptr = int(rig_settings.as_pointer())
    except Exception:
        ptr = id(rig_settings)
    return (owner_name, ptr)


def _json_token(text):
    raw_text = text or ""
    return (len(raw_text), hash(raw_text))


_TO_PLAIN_DATA_MAX_DEPTH = 64


def _to_plain_data(value, _visited=None, _depth=0):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, np.generic):
        return _to_plain_data(value.item(), _visited, _depth + 1)
    if isinstance(value, np.ndarray):
        return _to_plain_data(value.tolist(), _visited, _depth + 1)
    if isinstance(value, (bytes, bytearray)):
        return list(value)
    if isinstance(value, memoryview):
        return list(value.tobytes())
    if isinstance(value, Path):
        return str(value)
    if _depth > _TO_PLAIN_DATA_MAX_DEPTH:
        return None
    if isinstance(value, type):
        return getattr(value, "__name__", str(value))
    if isinstance(
        value,
        (
            types.BuiltinFunctionType,
            types.BuiltinMethodType,
            types.FunctionType,
            types.MethodType,
            types.ModuleType,
            types.GetSetDescriptorType,
            types.MemberDescriptorType,
            types.MappingProxyType,
        ),
    ):
        return None
    if isinstance(value, dict):
        if _visited is None:
            _visited = set()
        return {
            str(_to_plain_data(key, _visited, _depth + 1)): _to_plain_data(item, _visited, _depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        if _visited is None:
            _visited = set()
        return [_to_plain_data(item, _visited, _depth + 1) for item in value]

    # item() handler: only attempt if depth budget remains; use current _visited (init if needed)
    if _visited is None:
        _visited = set()
    item_getter = getattr(value, "item", None)
    if callable(item_getter):
        try:
            inner = item_getter()
            # Only recurse if the result is a different type (avoids wrapper-producing loops)
            if type(inner) is not type(value):
                return _to_plain_data(inner, _visited, _depth + 1)
        except Exception:
            pass

    obj_id = id(value)
    if obj_id in _visited:
        return None
    _visited.add(obj_id)
    try:
        if hasattr(value, "__json_serializable__"):
            return _to_plain_data(value.__json_serializable__(), _visited, _depth + 1)
        if hasattr(value, "__dict__"):
            return {
                key: _to_plain_data(item, _visited, _depth + 1)
                for key, item in vars(value).items()
            }
        return str(value)
    finally:
        _visited.discard(obj_id)


def _to_json_text(value, default_text="{}", indent=None):
    if value is None:
        return default_text
    plain_value = _to_plain_data(value)
    if plain_value is None:
        return default_text
    return json.dumps(plain_value, indent=indent, sort_keys=False)


def _coerce_engine_transform(value):
    if not value:
        return None
    if isinstance(value, EngineTransform):
        return value
    plain_value = value if isinstance(value, dict) else _to_plain_data(value)
    if not isinstance(plain_value, dict):
        return None
    try:
        return EngineTransform.from_json(**plain_value)
    except Exception:
        return None


def _get_entity_static_mesh_chunks(entity):
    static_meshes = getattr(entity, "staticMeshes", None)
    if static_meshes is None:
        return []
    if isinstance(static_meshes, dict):
        return static_meshes.get("chunks", []) or []
    return getattr(static_meshes, "chunks", []) or []


def _get_import_root_objects(objects):
    imported_objects = [
        obj for obj in (objects or [])
        if obj is not None and armature_merge.object_still_exists(obj)
    ]
    if not imported_objects:
        return []
    imported_ids = {id(obj) for obj in imported_objects}
    roots = []
    for obj in imported_objects:
        try:
            parent = obj.parent
        except ReferenceError:
            continue
        except Exception:
            parent = None
        if parent is None or not armature_merge.object_still_exists(parent) or id(parent) not in imported_ids:
            roots.append(obj)
    return roots or imported_objects


def _new_merged_armature_context(context=None, root_armature=None, *, force=False):
    if not force and not armature_merge.should_unify_character_armature(context):
        return None
    return {
        "enabled": True,
        "skeleton": None,
        "root_armature": root_armature,
        "root_skeleton_path": "",
        "root_chunk_key": "",
        "mimic_face_file": "",
    }


def _merged_context_enabled(merged_armature_context) -> bool:
    return bool(merged_armature_context and merged_armature_context.get("enabled"))


def _merged_context_target(merged_armature_context):
    if not _merged_context_enabled(merged_armature_context):
        return None
    target = merged_armature_context.get("root_armature")
    if target is not None and getattr(target, "type", None) == 'ARMATURE':
        return target
    return None


def _merged_chunk_keeps_own_skeleton(chunk) -> bool:
    """Return true for own-skeleton attachments that must remain separate."""
    chunk_type = str(_get_entry_attr(chunk, "type", "") or "")
    return bool(chunk_type == "CAnimatedComponent" and _get_entry_attr(chunk, "skeleton", None))


def _chunk_is_dangle_constraint(chunk) -> bool:
    return str(_get_entry_attr(chunk, "type", "") or "").startswith("CAnimDangleConstraint_")


def _chunk_is_dangle_buffer(chunk) -> bool:
    return str(_get_entry_attr(chunk, "type", "") or "") == "CAnimDangleBufferComponent"


def _merged_chunk_imports_own_skeleton(chunk) -> bool:
    return _merged_chunk_keeps_own_skeleton(chunk) or _chunk_is_dangle_buffer(chunk) or _chunk_is_dangle_constraint(chunk)


def _apply_merged_mimic_metadata(armature_obj, mimic_face_file=""):
    if armature_obj is None or getattr(armature_obj, "type", None) != 'ARMATURE':
        return
    mimic_face_file = str(mimic_face_file or "").strip()
    if not mimic_face_file:
        return
    armature_obj["mimicFace"] = armature_obj.name
    armature_obj["mimicFaceFile"] = mimic_face_file
    rig_settings = getattr(getattr(armature_obj, "data", None), "witcherui_RigSettings", None)
    if rig_settings is not None:
        try:
            rig_settings.main_face_skeleton = mimic_face_file
        except Exception:
            pass


def _merge_skeleton_data_into_context(merged_armature_context, skeleton_data, *, target_armature=None,
                                      exclude_bone_names=None, exclude_name_contains=None) -> int:
    if not _merged_context_enabled(merged_armature_context) or skeleton_data is None:
        return 0
    target_armature = target_armature or _merged_context_target(merged_armature_context)
    if target_armature is not None:
        repaired = armature_merge.fill_placeholder_armature_transforms_from_skeletons(
            target_armature,
            [skeleton_data],
            context=bpy.context,
        )
        added = armature_merge.append_skeleton_data_to_armature(
            target_armature,
            skeleton_data,
            context=bpy.context,
            exclude_bone_names=exclude_bone_names,
            exclude_name_contains=exclude_name_contains,
        )
        return repaired + added
    if merged_armature_context.get("skeleton") is None:
        merged_armature_context["skeleton"] = armature_merge.clone_skeleton_data(skeleton_data)
        return len(getattr(merged_armature_context["skeleton"], "bones", []) or [])
    return armature_merge.merge_skeleton_data(
        merged_armature_context["skeleton"],
        skeleton_data,
        exclude_bone_names=exclude_bone_names,
        exclude_name_contains=exclude_name_contains,
    )


def _normalize_repo_path(value) -> str:
    text = str(value or "").strip().replace("/", "\\")
    if not text:
        return ""
    if os.path.isabs(text):
        try:
            from ..importers.import_mesh import get_repo_from_abs_path
            text = str(get_repo_from_abs_path(text) or "").strip()
        except Exception:
            text = ""
    return text.replace("/", "\\").lstrip("\\") if text else ""


def _object_parent_depth(obj):
    depth = 0
    current = getattr(obj, "parent", None)
    while current is not None:
        depth += 1
        current = getattr(current, "parent", None)
    return depth


def _source_game_from_version(entity) -> str:
    version = _coerce_version(getattr(entity, "version", None), 999)
    return "w2" if version <= 115 else "w3"


def stamp_import_origin(objects, *, origin="", entity_path="",
                        source_game="", item_category="", item_name="",
                        equip_template="", item_appearance="",
                        owner_entity_path=""):
    """Stamp import metadata as custom properties on imported Blender objects."""
    props = {
        "witcher_import_origin": str(origin or "").strip(),
        "witcher_source_game": str(source_game or "").strip() or "w3",
        "witcher_entity_path": _normalize_repo_path(entity_path),
        "witcher_item_category": str(item_category or "").strip(),
        "witcher_item_name": str(item_name or "").strip(),
        "witcher_equip_template": _normalize_repo_path(equip_template),
        "witcher_item_appearance": str(item_appearance or "").strip(),
        "witcher_owner_entity_path": _normalize_repo_path(owner_entity_path),
    }
    for obj in (objects or []):
        if obj is None:
            continue
        for key, value in props.items():
            if value:
                obj[key] = value


_SRT_SOURCE_PATH_PROP = "witcher_srt_source"
_SRT_MESH_NAMES = {}


def _find_srt_source_mesh(srt_key):
    name = _SRT_MESH_NAMES.get(srt_key)
    if name:
        mesh = bpy.data.meshes.get(name)
        if mesh is not None and mesh.get(_SRT_SOURCE_PATH_PROP) == srt_key:
            return mesh
        _SRT_MESH_NAMES.pop(srt_key, None)
    for existing in bpy.data.meshes:
        if existing.get(_SRT_SOURCE_PATH_PROP) == srt_key:
            _SRT_MESH_NAMES[srt_key] = existing.name
            return existing
    return None


def _import_srt_chunk_object(srt_depot_path, object_name):
    srt_key = str(srt_depot_path or "").replace("/", "\\").lower()
    if not srt_key:
        return None
    existing = _find_srt_source_mesh(srt_key)
    if existing is not None:
        obj = bpy.data.objects.new(object_name, existing)
        bpy.context.collection.objects.link(obj)
        return obj

    from .import_helpers import meshPath
    from .import_blender_fun import _import_foliage_mesh
    from .. import get_W3_FOLIAGE_PATH

    mp = meshPath(meshName=srt_depot_path, fbx_uncook_path=get_W3_FOLIAGE_PATH(bpy.context))
    mp.type = "mesh_foliage"
    before = {o.as_pointer() for o in bpy.data.objects}
    _import_foliage_mesh(mp)
    new_objects = [o for o in bpy.data.objects if o.as_pointer() not in before]
    new_meshes = [o for o in new_objects if o.type == 'MESH' and o.data]
    best = max(new_meshes, key=lambda o: len(o.data.polygons), default=None)
    for obj in new_objects:
        if obj is best:
            continue
        data = obj.data if obj.type == 'MESH' else None
        bpy.data.objects.remove(obj, do_unlink=True)
        if data is not None and data.users == 0:
            bpy.data.meshes.remove(data)
    if best is None or len(best.data.vertices) == 0:
        if best is not None:
            data = best.data
            bpy.data.objects.remove(best, do_unlink=True)
            if data.users == 0:
                bpy.data.meshes.remove(data)
        return None
    best.data[_SRT_SOURCE_PATH_PROP] = srt_key
    _SRT_MESH_NAMES[srt_key] = best.data.name
    best.name = object_name
    return best


def _apply_chunk_transform_to_import_roots(chunk, *, armatures=None, meshes=None):
    rt = _coerce_engine_transform(chunk.get("transform"))
    if rt is None:
        return

    armatures = [obj for obj in (armatures or []) if obj is not None]
    meshes = [obj for obj in (meshes or []) if obj is not None]
    target_objects = armatures if armatures else meshes
    if not target_objects:
        return

    for obj in _get_import_root_objects(target_objects):
        set_blender_object_transform(obj, rt, rotate_180=False)


def import_direct_entity_file(filename, load_face_poses=False, import_apperance=0,
                              parent_transform=None, selected_appearance_name="",
                              mesh_import_settings=None):
    return import_entity_file(
        filename,
        load_face_poses,
        import_apperance,
        parent_transform,
        selected_appearance_name,
        mesh_import_settings=mesh_import_settings,
    ).main_object


def _dedupe_entity_appearance_names(values) -> list[str]:
    names = []
    seen = set()
    for value in values or []:
        name = str(value or "").strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names


def normalize_entity_appearance_metadata(metadata: dict | None) -> dict:
    metadata = dict(metadata or {})
    all_names = _dedupe_entity_appearance_names(metadata.get("all_names", []))
    selectable_keys = {name.lower() for name in all_names}
    used_names = [
        name
        for name in _dedupe_entity_appearance_names(metadata.get("used_names", []))
        if name.lower() in selectable_keys
    ]
    default_name = str(metadata.get("default_name", "") or "").strip()
    if default_name.lower() not in selectable_keys:
        default_name = used_names[0] if used_names else (all_names[0] if all_names else "")
    entity_class = str(
        metadata.get("entity_class")
        or metadata.get("type")
        or metadata.get("entityClass")
        or ""
    ).strip()
    has_scoped_cloth_metadata = (
        "base_has_cloth_components" in metadata
        or "cloth_appearance_names" in metadata
    )
    cloth_appearance_names = _dedupe_entity_appearance_names(
        metadata.get("cloth_appearance_names", [])
    )
    base_has_cloth_components = bool(metadata.get(
        "base_has_cloth_components",
        metadata.get("has_cloth_components", False) if not has_scoped_cloth_metadata else False,
    ))
    has_cloth_components = bool(
        metadata.get("has_cloth_components", False)
        or base_has_cloth_components
        or cloth_appearance_names
    )
    return {
        "all_names": all_names,
        "used_names": used_names,
        "default_name": default_name,
        "entity_class": entity_class,
        "component_metadata_known": bool(metadata.get("component_metadata_known", False)),
        "has_armature_root": bool(metadata.get("has_armature_root", False)),
        "has_mesh_components": bool(metadata.get("has_mesh_components", False)),
        "has_cloth_components": has_cloth_components,
        "base_has_cloth_components": base_has_cloth_components,
        "cloth_appearance_names": cloth_appearance_names,
        "has_inventory_entries": bool(metadata.get("has_inventory_entries", False)),
    }


def entity_appearance_has_cloth(metadata: dict | None, selected_appearance_name: str = "") -> bool:
    metadata = dict(metadata or {})
    if (
        "base_has_cloth_components" not in metadata
        and "cloth_appearance_names" not in metadata
    ):
        return bool(metadata.get("has_cloth_components", False))
    if metadata.get("base_has_cloth_components", False):
        return True
    selected_key = str(selected_appearance_name or "").strip().lower()
    return bool(selected_key) and selected_key in {
        str(name or "").strip().lower()
        for name in metadata.get("cloth_appearance_names", []) or []
        if str(name or "").strip()
    }


def _entity_json_component_metadata(data):
    flags = {
        "component_metadata_known": True,
        "has_armature_root": False,
        "has_mesh_components": False,
        "has_cloth_components": False,
        "has_inventory_entries": False,
    }
    mesh_types = {
        "CMeshComponent",
        "CStaticMeshComponent",
        "CRigidMeshComponent",
        "CRagdollMeshComponent",
        "CDressMeshComponent",
        "CFurComponent",
        "CMorphedMeshComponent",
    }
    armature_types = {
        "CMovingPhysicalAgentComponent",
        "CAnimatedComponent",
        "CAnimDangleBufferComponent",
        "CMimicComponent",
    }
    stack = [data]
    while stack:
        value = stack.pop()
        if isinstance(value, list):
            stack.extend(value)
            continue
        if not isinstance(value, dict):
            continue

        component_type = str(value.get("type") or value.get("component_type") or "").strip()
        if component_type in mesh_types or bool(value.get("mesh")):
            flags["has_mesh_components"] = True
        resource = str(value.get("resource") or "").strip().lower()
        if component_type == "CClothComponent" or resource.endswith((".redcloth", ".redapex", ".apx")):
            flags["has_cloth_components"] = True
        skeleton = str(value.get("skeleton") or value.get("mimicFace") or "").strip().lower()
        if component_type in armature_types and skeleton not in {"", "none"}:
            flags["has_armature_root"] = True
        moving_agent = value.get("MovingPhysicalAgentComponent")
        if isinstance(moving_agent, dict):
            agent_skeleton = str(moving_agent.get("skeleton") or "").strip().lower()
            if agent_skeleton not in {"", "none"}:
                flags["has_armature_root"] = True
        if value.get("inventoryDefinitions"):
            flags["has_inventory_entries"] = True
        stack.extend(value.values())
    return flags


def get_entity_appearance_metadata(filename: str) -> dict:
    empty_result = normalize_entity_appearance_metadata(None)
    filename = str(filename or "").strip()
    if not filename:
        return empty_result

    _, ext = os.path.splitext(filename)
    if ext.lower() == ".json":
        try:
            with open(filename, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            log.debug("Failed to read JSON entity appearance metadata for %s", filename, exc_info=True)
            return empty_result

        metadata = normalize_entity_appearance_metadata({
            "all_names": [
                str((appearance or {}).get("name", "") or "").strip()
                for appearance in data.get("appearances", []) or []
            ],
            "used_names": data.get("usedAppearances", []) or [],
            "entity_class": data.get("entity_class") or data.get("type") or data.get("entityClass") or "",
            **_entity_json_component_metadata(data),
        })
        dlc_names = get_dlc_external_appearance_names_for_entity(filename)
        if dlc_names:
            metadata["all_names"] = _dedupe_entity_appearance_names(metadata.get("all_names", []) + dlc_names)
            metadata["component_metadata_known"] = False
        return normalize_entity_appearance_metadata(metadata)

    metadata = normalize_entity_appearance_metadata(_read_entity_template_appearance_metadata(filename))
    dlc_names = get_dlc_external_appearance_names_for_entity(filename)
    if dlc_names:
        metadata["all_names"] = _dedupe_entity_appearance_names(metadata.get("all_names", []) + dlc_names)
        metadata["component_metadata_known"] = False
    return normalize_entity_appearance_metadata(metadata)


def _load_entity_state_from_json(rig_settings):
    raw_json = getattr(rig_settings, "jsonData", "") or ""
    if not raw_json:
        return None, None
    try:
        entity_data = json.loads(raw_json)
    except Exception:
        return None, None

    try:
        entity = w3_types.Entity.from_json(copy.deepcopy(entity_data))
    except Exception:
        entity = None
    return entity, entity_data


def cache_rig_entity_state(rig_settings, entity, entity_data=None, update_json=False):
    cache_key = _rig_settings_cache_key(rig_settings)
    if cache_key is None or entity is None:
        return None
    if entity_data is None:
        entity_data = _to_plain_data(entity)
    else:
        entity_data = _to_plain_data(entity_data)
    _ENTITY_RUNTIME_CACHE[cache_key] = {
        "entity": entity,
        "entity_data": entity_data,
        "json_token": _json_token(getattr(rig_settings, "jsonData", "") or ""),
    }
    if update_json:
        rig_settings.jsonData = json.dumps(entity_data, sort_keys=False)
        _ENTITY_RUNTIME_CACHE[cache_key]["json_token"] = _json_token(getattr(rig_settings, "jsonData", "") or "")
    return entity_data


def cache_rig_entity_state_from_data(rig_settings, entity_data, update_json=False):
    if entity_data is None:
        return None
    try:
        entity = w3_types.Entity.from_json(copy.deepcopy(entity_data))
    except Exception:
        return None
    cache_rig_entity_state(rig_settings, entity, entity_data=entity_data, update_json=update_json)
    return entity


def get_rig_entity_state(rig_settings, allow_json_fallback=True):
    cache_key = _rig_settings_cache_key(rig_settings)
    if cache_key is None:
        return None, None

    cached = _ENTITY_RUNTIME_CACHE.get(cache_key)
    current_json_token = _json_token(getattr(rig_settings, "jsonData", "") or "")
    if cached is not None and cached.get("json_token") == current_json_token:
        return cached.get("entity"), cached.get("entity_data")

    if not allow_json_fallback:
        return None, None

    entity, entity_data = _load_entity_state_from_json(rig_settings)
    if entity is None and entity_data is None:
        return None, None

    _ENTITY_RUNTIME_CACHE[cache_key] = {
        "entity": entity,
        "entity_data": entity_data,
        "json_token": current_json_token,
    }
    return entity, entity_data


def _coerce_version(value, default=999):
    if value is None:
        return default
    if isinstance(value, int):
        return value
    try:
        text = str(value).strip()
        if not text:
            return default
        return int(float(text))
    except Exception:
        return default


def test_load_entity(filename, append_dlc_appearances=True, load_dlc_appearances=False) ->  w3_types.Entity:
    # #TODO add this custom json after normal bin file is loaded
    # if filename.endswith("geralt_player.w2ent") or filename.endswith(r"player\player.w2ent"):
    #     RES_DIR = Path(__file__)
    #     RES_DIR = str(Path(RES_DIR).parents[1])
    #     filename = os.path.join(RES_DIR, r"CR2W\data\geralt_CUSTOM.w2ent.json")

    ext = os.path.splitext(str(filename or ""))[1]
    if ext.lower() in ('.json'):
        entity = read_json_w3.readEntFile(filename)
    elif ext.lower().endswith('.w2ent') or ext.lower().endswith('.w3app'):
        entity = load_bin_entity(filename)
    else:
        entity = None
    if entity is not None and append_dlc_appearances:
        append_dlc_external_appearances(entity, filename, load_appearances=load_dlc_appearances)
        append_dlc_entity_template_params(entity, filename)
    return entity

def _try_import_armature_from_item_appearances(entity, parent_transform=None, source_game="", target_collection=None,
                                               mesh_import_settings=None, component_import_options=None):
    """For CItemEntity (no MovingPhysicalAgentComponent), try to find a skeleton
    inside the first appearance's included templates.  Returns an armature object
    if one is found, otherwise None."""
    appearances = getattr(entity, 'appearances', None) or []
    if not appearances:
        return None
    first_app = appearances[0]
    templates = getattr(first_app, 'includedTemplates', None) or []
    for tmpl in templates:
        if isinstance(tmpl, dict):
            tmpl_filename = tmpl.get('templateFilename', '')
            template_chunks = tmpl.get('chunks') or []
            template_plan_complete = bool(tmpl.get('plan_complete', False))
        else:
            tmpl_filename = getattr(tmpl, 'templateFilename', '')
            template_chunks = getattr(tmpl, 'chunks', None) or []
            template_plan_complete = bool(getattr(tmpl, 'plan_complete', False))
        if not tmpl_filename:
            continue
        try:
            if template_chunks or template_plan_complete:
                sub_entity = w3_types.Entity(
                    name=Path(tmpl_filename).stem,
                    staticMeshes={"chunks": copy.deepcopy(template_chunks)},
                    version=getattr(entity, "version", 999),
                    type=getattr(entity, "type", None),
                )
            else:
                (_, sub_entity) = LoadCEntityTemplateFile(tmpl_filename, getattr(entity, "version", None))
            if sub_entity is None:
                continue
            arm = import_MovingPhysicalAgentComponent(
                sub_entity,
                parent_transform,
                direct_entity_path=tmpl_filename,
                source_game=source_game,
                target_collection=target_collection,
                mesh_import_settings=mesh_import_settings,
                component_import_options=component_import_options,
            )
            if arm:
                return arm
        except Exception:
            continue
    return None


def _entity_primary_component_type(entity) -> str:
    return inspect_entity_import_profile(entity).get("root_component_type", "")


def _should_create_direct_entity_root(entity, parent_transform=None) -> bool:
    if parent_transform is not None or entity is None:
        return False
    component_type = _entity_primary_component_type(entity)
    if component_type:
        return component_type != "CMovingPhysicalAgentComponent"
    return True


def _find_layer_collection_for_collection(layer_collection, target_collection):
    if layer_collection is None or target_collection is None:
        return None
    if getattr(layer_collection, "collection", None) == target_collection:
        return layer_collection
    for child in getattr(layer_collection, "children", []):
        found = _find_layer_collection_for_collection(child, target_collection)
        if found is not None:
            return found
    return None


def _get_import_target_collection(context=None):
    ctx = context or bpy.context
    view_layer = getattr(ctx, "view_layer", None)
    active_layer_collection = getattr(view_layer, "active_layer_collection", None) if view_layer else None
    target_collection = getattr(active_layer_collection, "collection", None)
    if target_collection is not None:
        return target_collection
    target_collection = getattr(ctx, "collection", None)
    if target_collection is not None:
        return target_collection
    scene = getattr(ctx, "scene", None)
    return getattr(scene, "collection", None)


def _activate_target_collection(context, target_collection) -> bool:
    if target_collection is None:
        return False
    ctx = context or bpy.context
    view_layer = getattr(ctx, "view_layer", None)
    if view_layer is None:
        return False
    active_layer_collection = getattr(view_layer, "active_layer_collection", None)
    if getattr(active_layer_collection, "collection", None) == target_collection:
        return True
    target_layer_collection = _find_layer_collection_for_collection(
        getattr(view_layer, "layer_collection", None),
        target_collection,
    )
    if target_layer_collection is None:
        return False
    view_layer.active_layer_collection = target_layer_collection
    return True


def _link_object_to_collection(obj, target_collection):
    if obj is None:
        return
    if target_collection is None:
        target_collection = _get_import_target_collection(bpy.context)
    if target_collection is None:
        return
    if obj.name not in target_collection.objects:
        target_collection.objects.link(obj)


def _create_entity_root_object(name: str, target_collection=None):
    root_name = str(name or "").strip() or "Entity"
    root_obj = bpy.data.objects.new(root_name, None)
    _link_object_to_collection(root_obj, target_collection or _get_import_target_collection(bpy.context))
    root_obj.empty_display_type = 'PLAIN_AXES'
    root_obj.empty_display_size = 0.1
    root_obj["witcher_entity_root"] = True
    return root_obj


def _focus_main_armature(context, armature_obj):
    if context is None or armature_obj is None or getattr(armature_obj, "type", "") != "ARMATURE":
        return
    try:
        set_main_armature(context.scene, armature_obj)
    except Exception:
        pass
    try:
        armature_obj.select_set(True)
    except Exception:
        pass
    try:
        view_layer = getattr(context, "view_layer", None)
        if view_layer is not None and getattr(view_layer, "objects", None) is not None:
            view_layer.objects.active = armature_obj
    except Exception:
        pass


def _ensure_imported_entity_face_morphs_loaded(context, armature_obj):
    if armature_obj is None or getattr(armature_obj, "type", None) != 'ARMATURE':
        return False
    try:
        w2_faces_high_chunk = int(armature_obj.get("witcher_w2_mimic_faces_high_embedded_chunk_index", -1))
    except Exception:
        w2_faces_high_chunk = -1
    has_w3_mimic = 'mimicFaceFile' in armature_obj and 'mimicFace' in armature_obj
    has_w2_mimic = bool(armature_obj.get("witcher_w2_mimic_support", False)) and (
        bool(str(armature_obj.get("witcher_w2_mimic_faces_high", "") or "").strip())
        or bool(str(armature_obj.get("witcher_w2_mimic_faces", "") or "").strip())
        or (
            bool(str(armature_obj.get("witcher_w2_mimic_faces_high_embedded_source", "") or "").strip())
            and w2_faces_high_chunk >= 0
        )
    )
    if not has_w3_mimic and not has_w2_mimic:
        return False
    try:
        from ..ui.ui_anims_list import ensure_owner_face_animation_setup

        loaded, target_armature = ensure_owner_face_animation_setup(
            context or bpy.context,
            armature_obj,
        )
        if loaded and target_armature is not None:
            rig_settings = getattr(getattr(armature_obj, "data", None), "witcherui_RigSettings", None)
            app_idx = -1
            try:
                app_idx = int(getattr(rig_settings, "app_list_index", -1)) if rig_settings else -1
            except Exception:
                app_idx = -1
            app_list = getattr(rig_settings, "app_list", None) if rig_settings else None
            if app_list is not None and 0 <= app_idx < len(app_list):
                try:
                    current_appearance = str(getattr(app_list[app_idx], "name", "") or "").strip()
                    if current_appearance:
                        target_armature[_FACE_MORPHS_APPEARANCE_PROP] = current_appearance
                except Exception:
                    pass
        return bool(loaded)
    except Exception:
        log.warning(
            "Failed to load face morphs during entity import for '%s'.",
            getattr(armature_obj, "name", "<unknown>"),
            exc_info=True,
        )
    return False


def _mesh_has_armature_modifier(mesh_obj) -> bool:
    for modifier in getattr(mesh_obj, "modifiers", []) or []:
        if modifier.type == 'ARMATURE' and getattr(modifier, "object", None) is not None:
            return True
    return False


def _mesh_source_is_skinned(mesh_obj) -> bool:
    if mesh_obj is None or getattr(mesh_obj, "type", None) != 'MESH':
        return False
    try:
        if str(mesh_obj.witcherui_MeshSettings['source_is_skinned']).lower() == "true":
            return True
    except Exception:
        pass
    return len(getattr(mesh_obj, "vertex_groups", []) or []) > 0


def _bind_unbound_skinned_meshes_to_merged_armature(objects, target_armature) -> int:
    if target_armature is None or getattr(target_armature, "type", None) != 'ARMATURE':
        return 0
    try:
        from . import import_mesh as _import_mesh_module
    except Exception:
        log.debug("Could not import mesh helper for merged armature binding validation.", exc_info=True)
        return 0

    seen = set()
    bound = 0
    for obj in objects or []:
        if obj is None or id(obj) in seen:
            continue
        seen.add(id(obj))
        if getattr(obj, "type", None) != 'MESH':
            continue
        if not _mesh_source_is_skinned(obj) or _mesh_has_armature_modifier(obj):
            continue
        try:
            compatible = _import_mesh_module.mesh_object_target_rest_compatibility(obj, target_armature)
        except Exception:
            log.debug(
                "Could not validate unbound skinned mesh '%s' against merged armature '%s'.",
                getattr(obj, "name", ""),
                getattr(target_armature, "name", ""),
                exc_info=True,
            )
            continue
        if compatible is not True:
            continue
        saved_parent = getattr(obj, "parent", None)
        saved_world = obj.matrix_world.copy()
        try:
            _import_mesh_module._ensure_armature_binding(obj, target_armature)
            if saved_parent is not None and obj.parent is not saved_parent:
                obj.parent = saved_parent
                obj.parent_type = 'OBJECT'
                obj.matrix_world = saved_world
            bound += 1
        except Exception:
            log.debug(
                "Failed to bind unbound skinned mesh '%s' to merged armature '%s'.",
                getattr(obj, "name", ""),
                getattr(target_armature, "name", ""),
                exc_info=True,
            )
    if bound:
        log.info(
            "Bound %d unbound skinned mesh(es) to merged armature '%s' after import validation.",
            bound,
            getattr(target_armature, "name", ""),
        )
    return bound


_foliage_dedupe_keys = None


def _with_foliage_dedupe_scope(func):
    def wrapper(*args, **kwargs):
        global _foliage_dedupe_keys
        owner = _foliage_dedupe_keys is None
        if owner:
            _foliage_dedupe_keys = set()
        try:
            return func(*args, **kwargs)
        finally:
            if owner:
                _foliage_dedupe_keys = None
    return wrapper


@_with_foliage_dedupe_scope
def _materialize_entity_asset(filename, load_face_poses = False, import_apperance = 0,
                              parent_transform = None, selected_appearance_name = "",
                              mesh_import_settings = None, entity_namespace = "",
                              entity_override = None, component_import_options = None,
                              load_appearance_equipment = False,
                              existing_root_skeleton = None, entity_overlay = False):
    base_context = bpy.context
    mesh_import_settings = get_entity_mesh_import_settings(mesh_import_settings)
    if not getattr(_materialize_entity_asset, "_repo_context_active", False):
        _materialize_entity_asset._repo_context_active = True
        try:
            with redkit_repo_context(filename):
                return _materialize_entity_asset(
                    filename,
                    load_face_poses,
                    import_apperance,
                    parent_transform,
                    selected_appearance_name,
                    mesh_import_settings=mesh_import_settings,
                    entity_namespace=entity_namespace,
                    entity_override=entity_override,
                    component_import_options=component_import_options,
                    load_appearance_equipment=load_appearance_equipment,
                    existing_root_skeleton=existing_root_skeleton,
                    entity_overlay=entity_overlay,
                )
        finally:
            _materialize_entity_asset._repo_context_active = False
    # Keep isolation at the public entry point.  The existing entity import
    # implementation below should stay unaware of the temporary session.
    if import_isolation.needs_isolation_session(base_context):
        target_collection = _get_import_target_collection(base_context)
        with import_isolation.isolated_import_session(
            base_context,
            target_collection,
            label=Path(filename).stem,
        ):
            result = _materialize_entity_asset(
                filename,
                load_face_poses,
                import_apperance,
                parent_transform,
                selected_appearance_name,
                mesh_import_settings=mesh_import_settings,
                entity_namespace=entity_namespace,
                entity_override=entity_override,
                component_import_options=component_import_options,
                load_appearance_equipment=load_appearance_equipment,
                existing_root_skeleton=existing_root_skeleton,
                entity_overlay=entity_overlay,
            )
        if result is not None:
            _focus_main_armature(base_context, result)
        return result

    context = bpy.context
    target_collection = _get_import_target_collection(context)
    _activate_target_collection(context, target_collection)
    entity = copy.deepcopy(entity_override) if entity_override is not None else test_load_entity(filename)
    if entity is None:
        raise ValueError(f"Could not parse entity asset: {filename}")
    namespace_override = str(entity_namespace or "").strip().rstrip(":")
    if namespace_override:
        entity.name = namespace_override
    entity_state = _build_entity_armature_state(
        entity,
        filename=filename,
        import_apperance=import_apperance,
        selected_appearance_name=selected_appearance_name,
    )
    entity_repo_path = entity_state.get("repo_path", "") if entity_state else ""
    entity_source_game = _source_game_from_version(entity)
    #entity = fixed_chunk_paths(entity, entity.version)
    entity_root = parent_transform
    created_entity_root = None
    if _should_create_direct_entity_root(entity, parent_transform):
        entity_root = _create_entity_root_object(
            getattr(entity, "name", "") or Path(filename).stem,
            target_collection=target_collection,
        )
        created_entity_root = entity_root
        entity_class = str(getattr(entity, "type", "") or "CEntity")
        entity_root["witcher_type"] = entity_class
        entity_root["witcher_redkit_class"] = entity_class

    merged_armature_context = _new_merged_armature_context(
        context,
        root_armature=existing_root_skeleton,
        force=existing_root_skeleton is not None,
    )
    base_animation_skeleton = import_MovingPhysicalAgentComponent(
        entity,
        entity_root,
        direct_entity_path=entity_repo_path,
        source_entity_path=filename if filename and os.path.isabs(str(filename)) else "",
        source_game=entity_source_game,
        target_collection=target_collection,
        mesh_import_settings=mesh_import_settings,
        merged_armature_context=merged_armature_context,
        component_import_options=component_import_options,
        existing_root_skeleton=existing_root_skeleton,
        append_entity_slots=bool(entity_overlay and existing_root_skeleton is not None),
    )
    main_arm_obj = existing_root_skeleton or base_animation_skeleton

    if not main_arm_obj:
        # Only handle entities that actually have appearance variants (e.g. CItemEntity dye
        # variants).  Static items / weapons without appearances keep returning None.
        if getattr(entity, 'appearances', None):
            # Try to find a skeleton inside the first appearance's included templates.
            # Skeletal equipment (armour, capes …) embed their rig inside the mesh template.
            arm_from_tmpl = _try_import_armature_from_item_appearances(
                entity,
                entity_root,
                source_game=entity_source_game,
                target_collection=target_collection,
                mesh_import_settings=mesh_import_settings,
                component_import_options=component_import_options,
            )
            if arm_from_tmpl:
                main_arm_obj = arm_from_tmpl
            else:
                # No skeleton anywhere — create a minimal empty armature as a scene anchor
                # so the appearance list and mesh imports still work.
                bpy.ops.object.armature_add(enter_editmode=True)
                main_arm_obj = bpy.context.object
                main_arm_obj.name = Path(filename).stem
                for bone in main_arm_obj.data.edit_bones:
                    main_arm_obj.data.edit_bones.remove(bone)
                bpy.ops.object.mode_set(mode='OBJECT')
                if entity_root and getattr(main_arm_obj, "parent", None) is None:
                    main_arm_obj.parent = entity_root
        else:
            return created_entity_root
    elif entity_root and getattr(main_arm_obj, "parent", None) is None:
        main_arm_obj.parent = entity_root
    if entity_overlay and existing_root_skeleton is not None:
        try:
            from ..ui.ui_equipment import refresh_slot_constraints
            refresh_slot_constraints(existing_root_skeleton)
        except Exception:
            pass
        try:
            armature_merge.unify_character_armatures(existing_root_skeleton, context=context)
        except Exception:
            log.warning(
                "Failed to unify overlay armatures for '%s'.",
                getattr(existing_root_skeleton, "name", ""),
                exc_info=True,
            )
        _focus_main_armature(context, existing_root_skeleton)
        return existing_root_skeleton
    entity_class = str(getattr(entity, "type", "") or "CEntity")
    main_arm_obj["witcher_type"] = entity_class
    main_arm_obj["witcher_redkit_class"] = entity_class
    _focus_main_armature(context, main_arm_obj)
    main_arm_obj["_w3_entity_import_in_progress"] = True
    try:
        rig_settings = initialize_entity_armature_state(
            main_arm_obj,
            entity,
            update_json=True,
            entity_state=entity_state,
            mesh_import_settings=mesh_import_settings,
        )

        app_idx = -1 if entity_state is None else int(entity_state.get("app_idx", -1))
        if rig_settings and getattr(entity, "appearances", None) and app_idx >= 0:
            item = rig_settings.app_list[app_idx]
            import_from_list_item(
                context,
                item,
                component_import_options=component_import_options,
                load_appearance_equipment=load_appearance_equipment,
            )

        # Refresh slot constraints after all components are imported
        try:
            from ..ui.ui_equipment import refresh_slot_constraints
            refresh_slot_constraints(main_arm_obj)
        except Exception:
            pass

        # Collapse the per-mesh/cloth/hair rigs bound to the main skeleton into one
        # armature (pref-gated, off by default). Done once here, after every
        # component/slot is bound, so the importer's earlier passes never see a
        # half-merged scene.
        try:
            armature_merge.unify_character_armatures(main_arm_obj, context=context)
        except Exception:
            log.warning("Failed to unify character armatures for '%s'.", getattr(main_arm_obj, "name", ""), exc_info=True)

        if load_face_poses:
            _ensure_imported_entity_face_morphs_loaded(context, main_arm_obj)

        try:
            stamp_import_origin(
                [main_arm_obj],
                origin="direct_entity",
                entity_path=(getattr(rig_settings, "repo_path", "") if rig_settings else "") or entity_state.get("repo_path", ""),
                source_game=entity_source_game,
            )
        except Exception:
            log.debug("Failed to stamp direct import origin for %s", filename, exc_info=True)

        _focus_main_armature(context, main_arm_obj)
        return main_arm_obj
    finally:
        try:
            if "_w3_entity_import_in_progress" in main_arm_obj:
                del main_arm_obj["_w3_entity_import_in_progress"]
        except Exception:
            pass


def import_entity_file(filename, load_face_poses=False, import_apperance=0,
                       parent_transform=None, selected_appearance_name="",
                       mesh_import_settings=None, entity_namespace="",
                       load_appearance_equipment=False):
    from . import import_blender_fun

    entity = test_load_entity(filename, load_dlc_appearances=True)
    if entity is None:
        raise ValueError(f"Could not parse entity file: {filename}")
    plan = import_blender_fun.build_entity_import_plan(
        entity,
        source_path=filename,
        selected_appearance=selected_appearance_name,
    )
    return import_blender_fun.materialize_entity_import_plan(
        plan,
        context=bpy.context,
        target_collection=_get_import_target_collection(bpy.context),
        options={
            "load_face_poses": bool(load_face_poses),
            "import_apperance": int(import_apperance),
            "parent_transform": parent_transform,
            "mesh_import_settings": mesh_import_settings,
            "entity_namespace": entity_namespace,
            "load_appearance_equipment": bool(load_appearance_equipment),
            "_entity_import_parse_cache_ready": True,
        },
    )


def entity_import_result_errors(result):
    errors = [str(error) for error in (getattr(result, "errors", None) or []) if str(error)]
    ok = bool(getattr(result, "main_object", None) or getattr(result, "created_objects", None))
    if not ok and not errors:
        errors = ["Entity import produced no objects."]
    return ok, errors


def inList(name, mylist):
    for el in mylist:
        if el in name:
            return True
    return False


def _derive_repo_root_hint(path: str) -> str:
    """Best-effort repo root from an absolute game-relative file path."""
    if not path or not os.path.isabs(path):
        return ""
    norm_path = os.path.normpath(path)
    lower_path = norm_path.lower()
    markers = (
        *W2_REPO_ROOT_MARKERS,
        "\\game\\",
        "\\gameplay\\",
        "\\items\\",
        "\\characters\\",
        "\\dlc\\",
        "\\quests\\",
        "\\levels\\",
        "\\living_world\\",
        "\\environment\\",
        "\\environment_levels\\",
        "\\templates\\",
        "\\cutscenes\\",
        "\\engine\\",
        "\\animations\\",
        "\\fx\\",
        "\\globals\\",
        "\\gui\\",
        "\\ui\\",
    )
    hits = [lower_path.find(marker) for marker in markers if lower_path.find(marker) > 2]
    if hits:
        return norm_path[: min(hits)]
    return os.path.dirname(norm_path)


def _build_entity_source_roots(filename: str):
    roots = []
    if filename and os.path.isabs(filename):
        root_hint = _derive_repo_root_hint(filename)
        if root_hint:
            roots.append(root_hint)
        parent_dir = os.path.dirname(os.path.normpath(filename))
        if parent_dir:
            roots.append(parent_dir)
    # Dedupe while preserving order.
    out = []
    seen = set()
    for root in roots:
        try:
            norm = os.path.normcase(os.path.normpath(root))
        except Exception:
            norm = str(root).lower()
        if norm in seen:
            continue
        seen.add(norm)
        out.append(root)
    return out


def _build_entity_armature_state(entity, *, filename="", import_apperance=0,
                                 selected_appearance_name="", context=None, source_roots=None):
    if entity is None:
        return None

    filename = str(filename or "").strip()
    appearances = list(getattr(entity, "appearances", None) or [])
    source_roots = list(source_roots or _build_entity_source_roots(filename))
    selected_appearance_name = str(selected_appearance_name or "").strip()
    import_profile = inspect_entity_import_profile(entity)

    repo_path = ""
    if filename:
        if not os.path.isabs(filename):
            repo_path = filename.replace("/", "\\").lstrip("\\")
        else:
            repo_path = _normalize_repo_path(filename)

    main_entity_skeleton = ""
    if import_profile.get("root_path_key") == "skeleton":
        main_entity_skeleton = str(import_profile.get("root_path", "") or "").strip()
    if not main_entity_skeleton:
        for chunk in _get_entity_static_mesh_chunks(entity):
            candidate = str(_get_entry_attr(chunk, "skeleton", "") or "").strip()
            if candidate:
                main_entity_skeleton = candidate
                break

    main_face_skeleton = ""
    for appearance in appearances:
        for template in getattr(appearance, "includedTemplates", None) or []:
            for chunk in _get_entry_attr(template, "chunks", []) or []:
                if str(_get_entry_attr(chunk, "type", "") or "").strip() != "CMimicComponent":
                    continue
                candidate = str(_get_entry_attr(chunk, "mimicFace", "") or "").strip()
                if candidate:
                    main_face_skeleton = candidate
                    break
            if main_face_skeleton:
                break
        if main_face_skeleton:
            break
    if not main_face_skeleton:
        if import_profile.get("root_path_key") == "mimicFace":
            main_face_skeleton = str(import_profile.get("root_path", "") or "").strip()
        else:
            for chunk in _get_entity_static_mesh_chunks(entity):
                candidate = str(_get_entry_attr(chunk, "mimicFace", "") or "").strip()
                if candidate:
                    main_face_skeleton = candidate
                    break

    app_idx = -1
    if appearances:
        if selected_appearance_name and selected_appearance_name != "__default__":
            for idx, appearance in enumerate(appearances):
                if str(getattr(appearance, "name", "") or "") == selected_appearance_name:
                    app_idx = idx
                    break

        if app_idx == -1:
            app_idx = int(import_apperance or 0) - 1
            if app_idx >= len(appearances):
                app_idx = len(appearances) - 1
                log.warning(
                    f"Requested appearance index out of range; clamped to {app_idx + 1} "
                    f"(available: {len(appearances)})"
                )

        base_mesh_count = sum(1 for chunk in _get_entity_static_mesh_chunks(entity) if _get_entry_attr(chunk, "mesh"))
        if app_idx == -1 and base_mesh_count == 0:
            app_idx = 0
            if not selected_appearance_name:
                log.info("[Witcher Tools] No base mesh chunks found; auto-importing first appearance (index 1).")

        if 0 <= app_idx < len(appearances):
            selected_appearance_name = str(getattr(appearances[app_idx], "name", "") or "")

    return {
        "source_roots": source_roots,
        "source_path": filename if filename and os.path.isabs(filename) else "",
        "entity_name": str(getattr(entity, "name", "") or "").strip() or Path(str(filename or "")).stem,
        "repo_path": repo_path,
        "main_entity_skeleton": main_entity_skeleton,
        "main_face_skeleton": main_face_skeleton,
        "appearances": appearances,
        "app_idx": app_idx,
        "selected_appearance_name": selected_appearance_name,
    }


def initialize_entity_armature_state(armature_obj, entity, *, filename="", import_apperance=0,
                                     selected_appearance_name="", update_json=True,
                                     context_role="primary", entity_state=None,
                                     mesh_import_settings=None):
    if armature_obj is None or entity is None:
        return None
    if getattr(armature_obj, "type", "") != "ARMATURE":
        return None

    if entity_state is None:
        entity_state = _build_entity_armature_state(
            entity,
            filename=filename,
            import_apperance=import_apperance,
            selected_appearance_name=selected_appearance_name,
        )
    if entity_state is None:
        return None

    source_roots = list(entity_state.get("source_roots", []) or [])
    try:
        armature_obj["_w3_source_roots_json"] = json.dumps(source_roots or [])
    except Exception:
        pass
    source_path = str(entity_state.get("source_path", "") or "").strip()
    if source_path:
        try:
            armature_obj["_w3_entity_source_path"] = source_path
        except Exception:
            pass
    try:
        armature_obj["_w3_entity_context_role"] = str(context_role or "primary")
    except Exception:
        pass

    rig_settings = getattr(armature_obj.data, "witcherui_RigSettings", None)
    if rig_settings is None:
        return None
    existing_main_entity_skeleton = str(getattr(rig_settings, "main_entity_skeleton", "") or "").strip()
    existing_main_face_skeleton = str(getattr(rig_settings, "main_face_skeleton", "") or "").strip()

    added_import_guard = False
    if not armature_obj.get("_w3_entity_import_in_progress", False):
        armature_obj["_w3_entity_import_in_progress"] = True
        added_import_guard = True

    try:
        cache_rig_entity_state(rig_settings, entity, update_json=update_json)

        rig_settings.entity_name = entity_state.get("entity_name") or Path(str(filename or armature_obj.name)).stem
        rig_settings.repo_path = entity_state.get("repo_path", "")
        version = _coerce_version(getattr(entity, "version", None), 999)
        rig_settings.source_game = "w2" if version <= 115 else "w3"
        if version <= 115:
            _store_entity_w2_mimic_metadata(armature_obj, entity)
        if mesh_import_settings is not None:
            apply_entity_mesh_import_settings(rig_settings, mesh_import_settings)
        rig_settings.main_entity_skeleton = entity_state.get("main_entity_skeleton", "") or existing_main_entity_skeleton
        rig_settings.main_face_skeleton = entity_state.get("main_face_skeleton", "") or existing_main_face_skeleton
        if not rig_settings.main_entity_skeleton:
            armature_path = _normalize_repo_path(armature_obj.get("witcher_path", ""))
            if armature_path.lower().endswith((".w2rig", ".w3dyng")):
                rig_settings.main_entity_skeleton = armature_path
        if not rig_settings.main_face_skeleton:
            rig_settings.main_face_skeleton = str(armature_obj.get("mimicFaceFile", "") or "").strip()

        app_idx = int(entity_state.get("app_idx", -1))
        appearances = entity_state.get("appearances", []) or []

        tree_list = rig_settings.app_list
        tree_list.clear()
        for node in appearances:
            NewListItem(tree_list, node)
        if tree_list:
            rig_settings.app_list_index = 0 if app_idx == -1 else app_idx
        else:
            rig_settings.app_list_index = -1

        animset_list = rig_settings.animset_list
        animset_list.clear()
        groups = list(_collect_armature_animset_groups(entity, armature_obj))
        for group_name, paths, component_name in groups:
            NewAnimsetListItem(
                animset_list,
                f"{group_name}:",
                group_name,
                component_name=component_name,
                source_game=rig_settings.source_game,
            )
            for path in paths:
                NewAnimsetListItem(
                    animset_list,
                    path,
                    group_name,
                    component_name=component_name,
                    source_game=rig_settings.source_game,
                )

        # Populate idle animation
        _populate_idle_animation(rig_settings, entity)

        return rig_settings
    finally:
        if added_import_guard:
            try:
                del armature_obj["_w3_entity_import_in_progress"]
            except Exception:
                pass


def initialize_imported_entity_armatures(objects, entity, *, filename="", import_apperance=0,
                                         selected_appearance_name="", update_json=True, root_only=True,
                                         context_role="primary", mesh_import_settings=None):
    imported_objects = [obj for obj in (objects or []) if obj is not None]
    if not imported_objects or entity is None:
        return []

    source_objects = _get_import_root_objects(imported_objects) if root_only else imported_objects
    armatures = [obj for obj in source_objects if getattr(obj, "type", "") == "ARMATURE"]
    if not armatures and root_only:
        armatures = [obj for obj in imported_objects if getattr(obj, "type", "") == "ARMATURE"]

    entity_state = _build_entity_armature_state(
        entity,
        filename=filename,
        import_apperance=import_apperance,
        selected_appearance_name=selected_appearance_name,
    )
    initialized = []
    for armature_obj in armatures:
        rig_settings = initialize_entity_armature_state(
            armature_obj,
            entity,
            update_json=update_json,
            context_role=context_role,
            entity_state=entity_state,
            mesh_import_settings=mesh_import_settings,
        )
        if rig_settings is not None:
            initialized.append(armature_obj)
    return initialized


def _get_armature_source_roots(armature):
    if not armature:
        return []
    raw_value = None
    try:
        raw_value = armature.get("_w3_source_roots_json")
    except Exception:
        raw_value = None
    if not raw_value:
        return []
    try:
        parsed = json.loads(raw_value)
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    out = []
    seen = set()
    for root in parsed:
        if not root:
            continue
        try:
            norm = os.path.normcase(os.path.normpath(str(root)))
        except Exception:
            norm = str(root).lower()
        if norm in seen:
            continue
        seen.add(norm)
        out.append(str(root))
    return out


def _repo_context_source_for_armature(armature) -> str:
    try:
        source_path = str(armature.get("_w3_entity_source_path", "") or "").strip()
    except Exception:
        source_path = ""
    if source_path and os.path.isabs(source_path):
        return source_path
    for root in _get_armature_source_roots(armature):
        if root and os.path.isabs(str(root)):
            return str(root)
    rig_settings = getattr(getattr(armature, "data", None), "witcherui_RigSettings", None) if armature else None
    repo_path_hint = str(getattr(rig_settings, "repo_path", "") or "").strip() if rig_settings else ""
    return repo_path_hint if repo_path_hint and os.path.isabs(repo_path_hint) else ""


def _is_shadowmesh_name(name: str) -> bool:
    lower_name = str(name or "").lower()
    compact_name = re.sub(r"[\s_\-]+", "", lower_name)
    return "shadowmesh" in compact_name


def _remove_hide_drivers(obj):
    if not obj or not getattr(obj, "animation_data", None):
        return
    drivers = getattr(obj.animation_data, "drivers", None)
    if not drivers:
        return
    for driver_curve in list(drivers):
        if driver_curve.data_path in {"hide_render", "hide_viewport"}:
            try:
                obj.driver_remove(driver_curve.data_path)
            except Exception:
                pass


def _force_shadowmesh_hidden(obj):
    if not obj:
        return
    _remove_hide_drivers(obj)
    obj.hide_render = True
    obj.hide_viewport = True

def create_on_prop(armobj: bpy.types.Armature,
                   current_app_list_index:int,
                   obj_to_hide:bpy.types.Object,
                   prop_name:str):
    driver_curve = obj_to_hide.driver_add(prop_name)
    driver = driver_curve.driver
    channel = "idx_on_app_list"
    driver.expression = "idx_on_app_list != "+str(current_app_list_index)
    var = driver.variables.get(channel)
    if var is None:
        var = driver.variables.new()
    var.type = "SINGLE_PROP"
    var.name = channel
    target = var.targets[0]
    target.id_type = "ARMATURE"
    target.data_path = "witcherui_RigSettings.app_list_index"
    target.id = armobj.data


def _ensure_app_visibility_driver(armobj, obj_to_hide, prop_name: str, expression: str):
    if armobj is None or obj_to_hide is None:
        return
    has_driver = False
    if obj_to_hide.animation_data and obj_to_hide.animation_data.drivers:
        for driver_curve in obj_to_hide.animation_data.drivers:
            if driver_curve.data_path != prop_name:
                continue
            driver_curve.driver.expression = expression
            var = driver_curve.driver.variables.get("idx_on_app_list")
            if var is None:
                var = driver_curve.driver.variables.new()
            var.type = "SINGLE_PROP"
            var.name = "idx_on_app_list"
            target = var.targets[0]
            target.id_type = "ARMATURE"
            target.data_path = "witcherui_RigSettings.app_list_index"
            target.id = armobj.data
            has_driver = True
    if has_driver:
        return

    driver_curve = obj_to_hide.driver_add(prop_name)
    driver = driver_curve.driver
    driver.expression = expression
    var = driver.variables.new()
    var.type = "SINGLE_PROP"
    var.name = "idx_on_app_list"
    target = var.targets[0]
    target.id_type = "ARMATURE"
    target.data_path = "witcherui_RigSettings.app_list_index"
    target.id = armobj.data


def _get_object_redcloth_resource_key(obj) -> str:
    current = obj
    seen = set()
    while current is not None:
        obj_id = id(current)
        if obj_id in seen:
            break
        seen.add(obj_id)
        try:
            resource_key = str(current.get("witcher_redcloth_resource_key", "") or "").strip()
        except Exception:
            resource_key = ""
        if resource_key:
            return resource_key
        current = getattr(current, "parent", None)
    return ""


def _get_visibility_appearance_indices_for_object(armobj, obj_to_hide, fallback_indices=None):
    appearance_indices = fallback_indices
    resource_key = _get_object_redcloth_resource_key(obj_to_hide)
    if resource_key and armobj is not None and getattr(armobj, "type", "") == 'ARMATURE':
        rig_settings = getattr(armobj.data, "witcherui_RigSettings", None)
        redcloth_indices = get_redcloth_resource_appearances_from_entity(rig_settings, resource_key)
        if redcloth_indices:
            appearance_indices = redcloth_indices
    if appearance_indices is None:
        return None
    out = []
    seen = set()
    for idx in appearance_indices:
        try:
            idx = int(idx)
        except Exception:
            continue
        if idx in seen:
            continue
        seen.add(idx)
        out.append(idx)
    return out


def create_app_drivers(armobj: bpy.types.Armature, obj_to_hide:bpy.types.Object, appearance_indices=None):
    """Create hide drivers on object and children.
    
    Args:
        armobj: The armature to reference in driver
        obj_to_hide: Object to add drivers to
        appearance_indices: Optional list of appearance indices where object should be visible.
                           If None, uses current app_list_index only.
    """
    if _is_redcloth_collision_helper(obj_to_hide):
        _remove_hide_drivers(obj_to_hide)
        obj_to_hide.hide_viewport = False
        obj_to_hide.hide_render = True
        for obj in obj_to_hide.children:
            create_app_drivers(armobj, obj, appearance_indices)
        return

    # Keep shadowmesh objects hidden regardless of active appearance.
    if _is_shadowmesh_name(getattr(obj_to_hide, "name", "")):
        _force_shadowmesh_hidden(obj_to_hide)
        for obj in obj_to_hide.children:
            create_app_drivers(armobj, obj, appearance_indices)
        return

    rig_settings = armobj.data.witcherui_RigSettings
    effective_indices = _get_visibility_appearance_indices_for_object(
        armobj,
        obj_to_hide,
        appearance_indices,
    )
    
    if effective_indices is None or len(effective_indices) <= 1:
        current_app_list_index = (
            effective_indices[0]
            if effective_indices else
            rig_settings.app_list_index
        )
        expression = f"idx_on_app_list != {int(current_app_list_index)}"
        _ensure_app_visibility_driver(armobj, obj_to_hide, "hide_render", expression)
        _ensure_app_visibility_driver(armobj, obj_to_hide, "hide_viewport", expression)
    else:
        indices_str = ", ".join(str(i) for i in sorted(effective_indices))
        expression = f"idx_on_app_list not in [{indices_str}]"
        _ensure_app_visibility_driver(armobj, obj_to_hide, "hide_render", expression)
        _ensure_app_visibility_driver(armobj, obj_to_hide, "hide_viewport", expression)
    
    for obj in obj_to_hide.children:
        create_app_drivers(armobj, obj, appearance_indices)


def update_driver_for_shared_template(obj, appearance_indices, rig_settings=None):
    """Update drivers on an object to show it for multiple appearance indices.
    
    Args:
        obj: The Blender object with hide drivers
        appearance_indices: List of appearance indices where this should be visible
    """
    if _is_shadowmesh_name(getattr(obj, "name", "")):
        _force_shadowmesh_hidden(obj)
        for child in obj.children:
            update_driver_for_shared_template(child, appearance_indices, rig_settings)
        return

    effective_indices = appearance_indices
    resource_key = _get_object_redcloth_resource_key(obj)
    if resource_key and rig_settings is not None:
        redcloth_indices = get_redcloth_resource_appearances_from_entity(rig_settings, resource_key)
        if redcloth_indices:
            effective_indices = redcloth_indices

    if not effective_indices:
        return
    
    # Build expression like "idx_on_app_list not in [0, 2, 3]"
    # (hidden when NOT in the list of valid appearances)
    unique_indices = []
    seen_indices = set()
    for idx in effective_indices:
        try:
            idx = int(idx)
        except Exception:
            continue
        if idx in seen_indices:
            continue
        seen_indices.add(idx)
        unique_indices.append(idx)
    if not unique_indices:
        return
    unique_indices.sort()
    if len(unique_indices) == 1:
        new_expression = f"idx_on_app_list != {unique_indices[0]}"
    else:
        indices_str = ", ".join(str(i) for i in unique_indices)
        new_expression = f"idx_on_app_list not in [{indices_str}]"
    
    for prop_name in ["hide_render", "hide_viewport"]:
        if obj.animation_data and obj.animation_data.drivers:
            for driver_curve in obj.animation_data.drivers:
                if driver_curve.data_path == prop_name:
                    driver_curve.driver.expression = new_expression
    
    # Recursively update children
    for child in obj.children:
        update_driver_for_shared_template(child, appearance_indices, rig_settings)


def update_template_drivers_for_appearances(guid, rig_settings, prop_name="witcher_template_guid"):
    """Update all objects with the given GUID to be visible for all appearances that use this template."""
    from ..ui.ui_equipment import find_objects_by_guid
    
    # Find the slot to get template filename
    slot = None
    for s in rig_settings.template_slots:
        if s.template_guid == guid:
            slot = s
            break
    
    if not slot:
        return
    
    # Get ALL appearance indices from entity data (not just visited ones)
    appearance_indices = get_template_appearances_from_entity(rig_settings, slot.template_filename)
    
    if not appearance_indices:
        return
    
    # Update all objects with this GUID
    objects = find_objects_by_guid(guid, prop_name)
    for obj in objects:
        update_driver_for_shared_template(obj, appearance_indices, rig_settings)

def _iter_inventory_entries(selected_appearance, entity=None):
    """Yield inventory entries from an appearance and optional entity (object or dict)."""
    def _yield_from(source):
        if not source:
            return
        inv_defs = []
        if hasattr(source, 'inventoryDefinitions'):
            inv_defs = source.inventoryDefinitions or []
        elif isinstance(source, dict):
            inv_defs = source.get('inventoryDefinitions', []) or []

        for inv_def in inv_defs:
            entries = []
            if isinstance(inv_def, dict):
                entries = inv_def.get('entries', []) or []
            elif hasattr(inv_def, 'entries'):
                entries = inv_def.entries or []
            for entry in entries:
                yield entry

    if selected_appearance is not None:
        yield from _yield_from(selected_appearance)
    if entity is not None:
        yield from _yield_from(entity)


def entity_has_inventory_entries(entity) -> bool:
    if entity is None:
        return False

    if next(_iter_inventory_entries(None, entity), None) is not None:
        return True

    appearances = _get_entry_attr(entity, "appearances", []) or []
    for appearance in appearances:
        if next(_iter_inventory_entries(appearance, None), None) is not None:
            return True
    return False


def entity_has_main_skeleton(entity) -> bool:
    return bool(inspect_entity_import_profile(entity).get("has_armature_root"))


def can_apply_inventory_to_selected_character(context) -> bool:
    context = context or bpy.context
    armature, rig_settings = get_main_armature_and_rig_settings(
        context,
        prefer_active=True,
        remember=True,
        fallback=True,
    )
    if armature is None or rig_settings is None:
        return False

    entity, entity_data = get_rig_entity_state(rig_settings)
    return bool(entity is not None or entity_data is not None)


def try_apply_inventory_file_to_selected_character(context, filename, import_mode='MOUNTS') -> bool:
    context = context or bpy.context
    if not filename:
        return False
    try:
        if not can_apply_inventory_to_selected_character(context):
            return False
    except Exception as exc:
        log.debug("Inventory applicability check failed for %s: %s", filename, exc)
        return False

    try:
        entity = test_load_entity(filename)
    except Exception as exc:
        log.debug("Inventory probe failed for %s: %s", filename, exc)
        return False

    if entity is None:
        return False
    if entity_has_main_skeleton(entity):
        return False
    if not entity_has_inventory_entries(entity):
        return False

    try:
        result = bpy.ops.witcher.import_w2ent_inventory(
            'EXEC_DEFAULT',
            filepath=filename,
            import_mode=import_mode,
        )
    except Exception:
        log.warning("Inventory apply failed for %s", filename, exc_info=True)
        return False

    finished = isinstance(result, set) and 'FINISHED' in result
    if finished:
        log.info("Applied inventory from %s to selected character", filename)
    return finished

def _get_entry_attr(entry, key, default=None):
    if isinstance(entry, dict):
        if key in entry:
            value = entry.get(key, default)
            if value is not None:
                return value
        native_key = f"m_{key}"
        if native_key in entry:
            return entry.get(native_key, default)
        return entry.get(key, default)
    value = getattr(entry, key, None)
    if value is not None:
        return value
    native_key = f"m_{key}"
    if hasattr(entry, native_key):
        return getattr(entry, native_key, default)
    return value if hasattr(entry, key) else default


def _coerce_equipment_item_name(value):
    item_name = str(value or "").strip()
    if not item_name or item_name.lower() in {"none", "null"}:
        return ""
    return item_name


def get_equipment_entry_default_item_name(entry):
    return _coerce_equipment_item_name(_get_entry_attr(entry, "defaultItemName", ""))


def get_equipment_entry_initializer_item_name(entry):
    initializer = _get_entry_attr(entry, "initializer", None)
    if initializer is None:
        return ""
    item_name = (
        _get_entry_attr(initializer, "itemName", None)
        or _get_entry_attr(initializer, "item", None)
    )
    return _coerce_equipment_item_name(item_name)


def should_use_equipment_initializers(rig_settings=None) -> bool:
    if rig_settings is None:
        return True
    return bool(getattr(rig_settings, "use_equipment_initializers", True))


def get_equipment_entry_item_name(entry, rig_settings=None, use_initializers=None):
    default_item = get_equipment_entry_default_item_name(entry)
    if use_initializers is None:
        use_initializers = should_use_equipment_initializers(rig_settings)
    if use_initializers:
        initializer_item = get_equipment_entry_initializer_item_name(entry)
        if initializer_item:
            return initializer_item
    return default_item


def _coerce_engine_bool(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _inventory_entry_is_mount(entry) -> bool:
    # Native CInventoryDefinitionEntry defaults m_isMount to false. Missing
    # mount data must not be inferred from category or item names.
    return _coerce_engine_bool(_get_entry_attr(entry, "isMount", None))


def _inventory_mount_override_key(category, item_name) -> str:
    category_key = _normalize_key(category)
    item_key = _normalize_key(item_name)
    if not category_key and not item_key:
        return ""
    return f"{category_key}::{item_key}"


def _get_inventory_mount_overrides(rig_settings):
    if rig_settings is None:
        return {}
    raw_value = getattr(rig_settings, "inventory_mount_overrides_json", "") or ""
    if not raw_value:
        return {}
    try:
        parsed = json.loads(raw_value)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _get_inventory_mount_override(rig_settings, category, item_name):
    key = _inventory_mount_override_key(category, item_name)
    if not key:
        return None
    overrides = _get_inventory_mount_overrides(rig_settings)
    if key not in overrides:
        return None
    value = overrides.get(key)
    if value is None:
        return None
    return _coerce_engine_bool(value)


# ============================================================
# Witcher 2 mimic support
# ============================================================
# W2 heads use CHeadDefinifion + CMimicFaces (.w2faces). Keep these
# properties separate from the W3 mimicFace/mimicFaceFile/.w3fac path.

def _is_w2_mimic_support_chunk(chunk) -> bool:
    return bool(_get_entry_attr(chunk, "w2_mimic_support", False))


def _is_w2_head_base_chunk(chunk) -> bool:
    return bool(_get_entry_attr(chunk, "w2_head_support", False)) and (
        str(_get_entry_attr(chunk, "w2_head_mesh_role", "") or "").strip().lower() == "base"
    )


def _store_w2_head_metadata(obj, chunk):
    if obj is None or not _get_entry_attr(chunk, "w2_head_support", False):
        return
    obj["witcher_w2_head_support"] = True
    for src_key, prop_key in (
        ("w2_head_name", "witcher_w2_head_name"),
        ("w2_head_mesh_role", "witcher_w2_head_mesh_role"),
        ("w2_head_parent_slot_name", "witcher_w2_head_parent_slot_name"),
    ):
        value = str(_get_entry_attr(chunk, src_key, "") or "").strip()
        if value:
            obj[prop_key] = value
    try:
        obj["witcher_w2_head_hide_when_mimic_available"] = bool(_get_entry_attr(chunk, "w2_head_hide_when_mimic_available", False))
    except Exception:
        pass
    try:
        obj["witcher_w2_head_dist_for_default_head"] = float(_get_entry_attr(chunk, "w2_head_dist_for_default_head", 0.0) or 0.0)
    except Exception:
        pass


def _hide_w2_base_head_objects_for_mimic(head_name, objdict, meshdict):
    head_key = str(head_name or "").strip().lower()
    if not head_key:
        return 0
    hidden = 0
    seen = set()
    for obj in list(objdict.values()) + list(meshdict.values()):
        if obj is None or id(obj) in seen:
            continue
        seen.add(id(obj))
        if str(obj.get("witcher_w2_head_mesh_role", "") or "").strip().lower() != "base":
            continue
        if str(obj.get("witcher_w2_head_name", "") or "").strip().lower() != head_key:
            continue
        if not bool(obj.get("witcher_w2_head_hide_when_mimic_available", False)):
            continue
        try:
            obj.hide_set(True)
        except Exception:
            pass
        try:
            obj.hide_viewport = True
            obj.hide_render = True
        except Exception:
            pass
        obj["witcher_w2_hidden_by_mimic_head"] = True
        hidden += 1
    return hidden


def _hide_w2_mimic_fallback_objects(objects):
    hidden = 0
    seen = set()
    for obj in objects or []:
        if obj is None or id(obj) in seen:
            continue
        seen.add(id(obj))
        try:
            obj.hide_set(True)
        except Exception:
            pass
        try:
            obj.hide_viewport = True
            obj.hide_render = True
        except Exception:
            pass
        obj["witcher_w2_mimic_hidden_without_face_rig"] = True
        hidden += 1
    return hidden


def _w2_mimic_metadata_from_chunk(chunk):
    if not _is_w2_mimic_support_chunk(chunk):
        return {}
    metadata = {
        "head_name": str(_get_entry_attr(chunk, "w2_mimic_head_name", "") or ""),
        "mesh_role": str(_get_entry_attr(chunk, "w2_mimic_mesh_role", "") or ""),
        "mesh": str(_get_entry_attr(chunk, "w2_mimic_mesh", "") or _get_entry_attr(chunk, "mesh", "") or ""),
        "mesh_high": str(_get_entry_attr(chunk, "w2_mimic_mesh_high", "") or ""),
        "mesh_low": str(_get_entry_attr(chunk, "w2_mimic_mesh_low", "") or ""),
        "skeleton": str(_get_entry_attr(chunk, "w2_mimic_skeleton", "") or _get_entry_attr(chunk, "skeleton", "") or ""),
        "pose_skeleton": str(_get_entry_attr(chunk, "w2_mimic_pose_skeleton", "") or ""),
        "parent_skeleton": str(_get_entry_attr(chunk, "w2_mimic_parent_skeleton", "") or ""),
        "float_track_skeleton": str(_get_entry_attr(chunk, "w2_mimic_float_track_skeleton", "") or ""),
        "skeleton_embedded_source": str(_get_entry_attr(chunk, "w2_mimic_skeleton_embedded_source", "") or ""),
        "skeleton_embedded_chunk_index": _get_entry_attr(chunk, "w2_mimic_skeleton_embedded_chunk_index", -1),
        "embedded_skeleton_data": _get_entry_attr(chunk, "w2_mimic_embedded_skeleton_data", None),
        "faces": str(_get_entry_attr(chunk, "w2_mimic_faces", "") or ""),
        "faces_high": str(_get_entry_attr(chunk, "w2_mimic_faces_high", "") or ""),
        "faces_low": str(_get_entry_attr(chunk, "w2_mimic_faces_low", "") or ""),
        "faces_high_embedded_source": str(_get_entry_attr(chunk, "w2_mimic_faces_high_embedded_source", "") or ""),
        "faces_high_embedded_chunk_index": _get_entry_attr(chunk, "w2_mimic_faces_high_embedded_chunk_index", -1),
        "faces_low_embedded_source": str(_get_entry_attr(chunk, "w2_mimic_faces_low_embedded_source", "") or ""),
        "faces_low_embedded_chunk_index": _get_entry_attr(chunk, "w2_mimic_faces_low_embedded_chunk_index", -1),
        "bone_mapping": _get_entry_attr(chunk, "w2_mimic_bone_mapping", []) or [],
        "bone_mapping_low": _get_entry_attr(chunk, "w2_mimic_bone_mapping_low", []) or [],
        "parent_slot_name": str(
            _get_entry_attr(chunk, "w2_mimic_parent_slot_name", "")
            or _get_entry_attr(chunk, "w2_head_parent_slot_name", "")
            or ""
        ),
        "dist_for_default_head": (
            _get_entry_attr(chunk, "w2_mimic_dist_for_default_head", None)
            if _get_entry_attr(chunk, "w2_mimic_dist_for_default_head", None) is not None
            else _get_entry_attr(chunk, "w2_head_dist_for_default_head", None)
        ),
    }
    return {key: value for key, value in metadata.items() if value not in ("", None)}


def _collect_w2_mimic_metadata_from_entity(entity):
    out = []
    seen = set()
    for appearance in _get_entry_attr(entity, "appearances", []) or []:
        for template in _get_entry_attr(appearance, "includedTemplates", []) or []:
            for chunk in _get_entry_attr(template, "chunks", []) or []:
                metadata = _w2_mimic_metadata_from_chunk(chunk)
                if not metadata:
                    continue
                key = (
                    metadata.get("head_name", "").lower(),
                    metadata.get("mesh", "").lower().replace("/", "\\"),
                    metadata.get("skeleton", "").lower().replace("/", "\\"),
                    metadata.get("faces_high", "").lower().replace("/", "\\"),
                    metadata.get("faces_low", "").lower().replace("/", "\\"),
                )
                if key in seen:
                    continue
                seen.add(key)
                out.append(metadata)
    return out


def _store_w2_mimic_metadata(obj, metadata, *, armature_name="", mesh_object_name=""):
    if obj is None or not metadata:
        return
    obj["witcher_w2_mimic_support"] = True
    obj["witcher_source_game"] = "w2"
    rig_settings = getattr(getattr(obj, "data", None), "witcherui_RigSettings", None)
    if rig_settings is not None:
        try:
            rig_settings.source_game = "w2"
        except Exception:
            pass
        float_track_skeleton = str(metadata.get("float_track_skeleton", "") or "").strip()
        if float_track_skeleton and not str(getattr(rig_settings, "main_face_skeleton", "") or "").strip():
            try:
                rig_settings.main_face_skeleton = float_track_skeleton
            except Exception:
                pass
    for src_key, prop_key in (
        ("head_name", "witcher_w2_mimic_head_name"),
        ("mesh_role", "witcher_w2_mimic_mesh_role"),
        ("mesh", "witcher_w2_mimic_mesh"),
        ("mesh_high", "witcher_w2_mimic_mesh_high"),
        ("mesh_low", "witcher_w2_mimic_mesh_low"),
        ("skeleton", "witcher_w2_mimic_skeleton"),
        ("pose_skeleton", "witcher_w2_mimic_pose_skeleton"),
        ("parent_skeleton", "witcher_w2_mimic_parent_skeleton"),
        ("float_track_skeleton", "witcher_w2_mimic_float_track_skeleton"),
        ("skeleton_embedded_source", "witcher_w2_mimic_skeleton_embedded_source"),
        ("faces", "witcher_w2_mimic_faces"),
        ("faces_high", "witcher_w2_mimic_faces_high"),
        ("faces_low", "witcher_w2_mimic_faces_low"),
        ("faces_high_embedded_source", "witcher_w2_mimic_faces_high_embedded_source"),
        ("faces_low_embedded_source", "witcher_w2_mimic_faces_low_embedded_source"),
        ("parent_slot_name", "witcher_w2_mimic_parent_slot_name"),
    ):
        value = str(metadata.get(src_key, "") or "").strip()
        if value:
            obj[prop_key] = value
    for src_key, prop_key in (
        ("skeleton_embedded_chunk_index", "witcher_w2_mimic_skeleton_embedded_chunk_index"),
        ("faces_high_embedded_chunk_index", "witcher_w2_mimic_faces_high_embedded_chunk_index"),
        ("faces_low_embedded_chunk_index", "witcher_w2_mimic_faces_low_embedded_chunk_index"),
    ):
        try:
            value = int(metadata.get(src_key, -1))
        except Exception:
            value = -1
        if value >= 0:
            obj[prop_key] = value
    if armature_name:
        obj["witcher_w2_mimic_armature"] = armature_name
    if mesh_object_name:
        obj["witcher_w2_mimic_mesh_object"] = mesh_object_name
    try:
        obj["witcher_w2_mimic_metadata_json"] = json.dumps(metadata, sort_keys=True)
    except Exception:
        pass
    for src_key, prop_key in (
        ("bone_mapping", "witcher_w2_mimic_bone_mapping_json"),
        ("bone_mapping_low", "witcher_w2_mimic_bone_mapping_low_json"),
    ):
        value = metadata.get(src_key)
        if value:
            try:
                obj[prop_key] = json.dumps(value, sort_keys=True)
            except Exception:
                pass


def _store_entity_w2_mimic_metadata(armature_obj, entity):
    metadata_items = _collect_w2_mimic_metadata_from_entity(entity)
    if not metadata_items or armature_obj is None:
        return
    first = metadata_items[0]
    _store_w2_mimic_metadata(armature_obj, first)
    try:
        armature_obj["witcher_w2_mimic_heads_json"] = json.dumps(metadata_items, sort_keys=True)
    except Exception:
        pass


def _store_armature_bone_order_from_skeleton(armature_obj, skeleton_data):
    if armature_obj is None or skeleton_data is None:
        return
    rig_settings = getattr(getattr(armature_obj, "data", None), "witcherui_RigSettings", None)
    if rig_settings is None:
        return
    bone_order = getattr(rig_settings, "bone_order_list", None)
    if bone_order is None:
        return
    try:
        bone_order.clear()
    except Exception:
        pass
    for bone_data in getattr(skeleton_data, "bones", []) or []:
        name = str(getattr(bone_data, "name", "") or "").strip()
        if not name:
            continue
        try:
            item = bone_order.add()
            item.name = name
        except Exception:
            break


def _load_w2_embedded_skeleton_data(source_path, chunk_index):
    source_path = str(source_path or "").strip()
    if not source_path or not os.path.exists(win_safe_path(source_path)):
        return None
    try:
        chunk_index = int(chunk_index)
    except Exception:
        return None
    if chunk_index < 0:
        return None
    try:
        from ..CR2W.CR2W_file import read_CR2W
        from ..CR2W.dc_skeleton import _read_w2_mimic_skeleton, create_Skeleton_w2, read_skelly

        cr2w_file = read_CR2W(source_path)
        with open(source_path, "rb") as f:
            raw_data = f.read()
        chunks = list(getattr(getattr(cr2w_file, "CHUNKS", None), "CHUNKS", None) or [])
        candidate_indices = [chunk_index]
        if chunk_index > 0:
            candidate_indices.append(chunk_index - 1)
        for candidate_index in candidate_indices:
            if not (0 <= candidate_index < len(chunks)):
                continue
            chunk = chunks[candidate_index]
            if getattr(chunk, "Type", None) != "CSkeleton":
                continue
            rig = _read_w2_mimic_skeleton(cr2w_file, raw_data, candidate_index)
            if getattr(getattr(cr2w_file, "HEADER", None), "version", 999) <= 115:
                if rig is None or not getattr(rig, "names", None):
                    class _ChunkList:
                        CHUNKS = [chunk]

                    class _SingleChunkFile:
                        CHUNKS = _ChunkList()

                    with open(source_path, "rb") as f:
                        rig = create_Skeleton_w2(f, _SingleChunkFile())
            if rig is None or not getattr(rig, "names", None):
                rig = read_skelly(chunk)
            if not getattr(rig, "names", None):
                continue
            skeleton_data = read_json_w3.readCSkeletonData(rig)
            bone_names = {
                str(getattr(bone, "name", "") or "")
                for bone in getattr(skeleton_data, "bones", []) or []
            }
            if not {"Rootface", "head_face"}.intersection(bone_names):
                log.debug(
                    "Rejected embedded Witcher 2 mimic skeleton with incomplete face bone names: %s #%s",
                    source_path,
                    chunk_index,
                )
                continue
            return skeleton_data
    except Exception:
        log.debug(
            "Failed to read embedded Witcher 2 mimic skeleton %s #%s",
            source_path,
            chunk_index,
            exc_info=True,
        )
    return None


def _coerce_w2_bone_mapping(value):
    if not value:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return []
    out = []
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        try:
            child_index = int(item[0])
            parent_index = int(item[1])
        except Exception:
            continue
        if child_index < 0 or parent_index < 0:
            continue
        out.append((child_index, parent_index))
    return out


def _bone_name_by_order_index(armature_obj, bone_index):
    if armature_obj is None or getattr(armature_obj, "type", "") != 'ARMATURE':
        return ""
    try:
        bone_index = int(bone_index)
    except Exception:
        return ""
    if bone_index < 0:
        return ""

    rig_settings = getattr(getattr(armature_obj, "data", None), "witcherui_RigSettings", None)
    ordered = getattr(rig_settings, "bone_order_list", None) if rig_settings is not None else None
    try:
        if ordered is not None and 0 <= bone_index < len(ordered):
            name = str(getattr(ordered[bone_index], "name", "") or "").strip()
            if name:
                return name
    except Exception:
        pass

    try:
        bones = list(getattr(getattr(armature_obj, "pose", None), "bones", []) or [])
        if 0 <= bone_index < len(bones):
            return str(getattr(bones[bone_index], "name", "") or "").strip()
    except Exception:
        pass
    return ""


def _remove_w2_mimic_mapping_constraints(pose_bone):
    if pose_bone is None:
        return
    for constraint in list(getattr(pose_bone, "constraints", []) or []):
        if str(getattr(constraint, "name", "") or "").startswith("W2_MIMIC_MAP_"):
            pose_bone.constraints.remove(constraint)


def _apply_w2_mimic_bone_mapping_constraints(parent_armature, child_armature, metadata):
    if (
        parent_armature is None
        or child_armature is None
        or getattr(parent_armature, "type", "") != 'ARMATURE'
        or getattr(child_armature, "type", "") != 'ARMATURE'
    ):
        return 0

    mapping = _coerce_w2_bone_mapping(metadata.get("bone_mapping"))
    if not mapping:
        mapping = _coerce_w2_bone_mapping(metadata.get("bone_mapping_low"))
    if not mapping:
        return 0

    changed = 0
    for child_index, parent_index in mapping:
        child_bone_name = _bone_name_by_order_index(child_armature, child_index)
        parent_bone_name = _bone_name_by_order_index(parent_armature, parent_index)
        if not child_bone_name or not parent_bone_name:
            continue
        child_pose_bone = child_armature.pose.bones.get(child_bone_name)
        if child_pose_bone is None or parent_armature.pose.bones.get(parent_bone_name) is None:
            continue
        _remove_w2_mimic_mapping_constraints(child_pose_bone)
        copy_transform = child_pose_bone.constraints.new('COPY_TRANSFORMS')
        copy_transform.name = f"W2_MIMIC_MAP_{parent_bone_name}_to_{child_bone_name}"
        copy_transform.target = parent_armature
        copy_transform.subtarget = parent_bone_name
        try:
            copy_transform.owner_space = 'WORLD'
            copy_transform.target_space = 'WORLD'
        except Exception:
            pass
        try:
            copy_transform.mix_mode = 'REPLACE'
        except Exception:
            pass
        changed += 1

    if changed:
        child_armature["witcher_w2_mimic_parent_armature"] = parent_armature.name
        child_armature["witcher_w2_mimic_mapping_constraint_count"] = changed
    return changed


def _w2_armature_slot_bone(parent_armature, slot_name):
    if parent_armature is None or getattr(parent_armature, "type", "") != 'ARMATURE':
        return None
    pose_bones = getattr(getattr(parent_armature, "pose", None), "bones", None)
    if not pose_bones:
        return None

    slot_name = str(slot_name or "").strip()
    if not slot_name:
        return None
    bone = pose_bones.get(slot_name)
    if bone is not None:
        return bone

    lowered = {str(getattr(bone, "name", "") or "").lower(): bone for bone in pose_bones}
    return lowered.get(slot_name.lower())


def _remove_w2_head_slot_constraints(pose_bone):
    if pose_bone is None:
        return
    for constraint in list(getattr(pose_bone, "constraints", []) or []):
        if str(getattr(constraint, "name", "") or "").startswith("W2_HEAD_SLOT_"):
            pose_bone.constraints.remove(constraint)


def _attach_w2_head_objects_to_parent_slot(parent_armature, objects, slot_name):
    p_bone = _w2_armature_slot_bone(parent_armature, slot_name)
    if p_bone is None:
        slot_name = str(slot_name or "").strip()
        if slot_name and parent_armature is not None:
            log.warning(
                "Witcher 2 head parent slot '%s' was not found on '%s'",
                slot_name,
                getattr(parent_armature, "name", "<unknown>"),
            )
        return 0

    attached = 0
    for obj in _get_import_root_objects(objects or []):
        if obj is None or obj is parent_armature:
            continue
        if getattr(obj, "type", "") == 'ARMATURE' and getattr(obj, "pose", None):
            _set_parent_keep_world(obj, parent_armature)
            obj.parent_type = "OBJECT"
            obj.parent_bone = ""
            child_bones = list(getattr(obj.pose, "bones", []) or [])
            if child_bones:
                child_root = child_bones[0]
                _remove_w2_head_slot_constraints(child_root)
                copy_transform = child_root.constraints.new('COPY_TRANSFORMS')
                copy_transform.name = f"W2_HEAD_SLOT_{p_bone.name}_to_{child_root.name}"
                copy_transform.target = parent_armature
                copy_transform.subtarget = p_bone.name
                try:
                    copy_transform.owner_space = 'WORLD'
                    copy_transform.target_space = 'WORLD'
                except Exception:
                    pass
                try:
                    copy_transform.mix_mode = 'REPLACE'
                except Exception:
                    pass
        else:
            _set_parent_keep_world(
                obj,
                parent_armature,
                parent_type='BONE',
                parent_bone=p_bone.name,
            )

        obj["witcher_w2_head_parent_armature"] = parent_armature.name
        obj["witcher_w2_head_parent_slot"] = p_bone.name
        attached += 1
    return attached


def _is_w2_head_slot_attached_object(obj):
    if obj is None:
        return False
    try:
        if obj.get("witcher_w2_head_parent_armature") and obj.get("witcher_w2_head_parent_slot"):
            return True
        return bool(obj.get("witcher_w2_mimic_parent_armature") and obj.get("witcher_w2_mimic_mapping_constraint_count"))
    except Exception:
        return False


def _attach_w2_mimic_meshes_to_mimic_rig(mimic_armature, mesh_armatures, meshes):
    if mimic_armature is None or getattr(mimic_armature, "type", "") != 'ARMATURE':
        return 0

    attached = 0
    mesh_armatures = list(mesh_armatures or [])
    meshes = list(meshes or [])

    for mesh_armature in mesh_armatures:
        if mesh_armature is None or mesh_armature is mimic_armature or getattr(mesh_armature, "type", "") != 'ARMATURE':
            continue
        try:
            constrain_util.CreateConstraints2(mimic_armature, mesh_armature)
            mesh_armature["witcher_w2_mimic_mesh_constrained_to_rig"] = True
        except Exception:
            log.debug(
                "Failed to constrain W2 mimic mesh armature '%s' to mimic rig '%s'.",
                getattr(mesh_armature, "name", ""),
                getattr(mimic_armature, "name", ""),
                exc_info=True,
            )
        _set_parent_keep_world(mesh_armature, mimic_armature)
        mesh_armature["witcher_w2_mimic_mesh_parent_rig"] = mimic_armature.name
        attached += 1

    mesh_armature_ids = {id(obj) for obj in mesh_armatures if obj is not None}
    for mesh in meshes:
        if mesh is None:
            continue
        if getattr(mesh, "parent", None) is not None and id(mesh.parent) in mesh_armature_ids:
            continue
        _set_parent_keep_world(mesh, mimic_armature)
        mesh["witcher_w2_mimic_mesh_parent_rig"] = mimic_armature.name
        attached += 1

    return attached


def _iter_entity_mimic_animset_params(entity):
    raw_params = getattr(entity, "CAnimMimicParam", []) or []
    if isinstance(raw_params, dict):
        raw_params = [raw_params]
    for mimic_param in raw_params:
        if isinstance(mimic_param, (list, tuple, set)):
            for nested_param in mimic_param:
                if nested_param:
                    yield nested_param
        elif mimic_param:
            yield mimic_param


def _get_entry_component_type(entry, fallback="") -> str:
    component_type = str(_get_entry_attr(entry, "type", "") or "").strip()
    if component_type:
        return component_type
    if entry is not None and not isinstance(entry, dict):
        component_type = str(type(entry).__name__ or "").strip()
        if component_type and component_type != "dict":
            return component_type
    return str(fallback or "").strip()


@dataclass(frozen=True)
class EquipmentAttachmentProfile:
    kind: Literal["owner_graph", "slot_visual", "slot_animated", "inventory_wrapper"] = "slot_visual"
    has_internal_attachment_graph: bool = False
    has_armature_root: bool = False
    has_streamed_components_only: bool = False
    has_component_attachments: bool = False
    has_owner_component_attachments: bool = False
    has_dangle_graph: bool = False
    has_skinned_mesh_payload: bool = False
    has_visual_components: bool = False
    requires_owner_root_binding: bool = False
    requires_slot_mount: bool = True
    root_component_type: str = ""


_EQUIPMENT_OWNER_ATTACHMENT_TYPES = {
    "CAnimatedAttachment",
    "CHardAttachment",
}
_EQUIPMENT_COMPONENT_ATTACHMENT_TYPES = {
    "CMeshSkinningAttachment",
} | _EQUIPMENT_OWNER_ATTACHMENT_TYPES
_EQUIPMENT_INTERNAL_ATTACHMENT_TYPES = _EQUIPMENT_COMPONENT_ATTACHMENT_TYPES | {
    "CAnimDangleComponent",
}
_ROOT_CONSTRAINT_SKIP_CHUNK_TYPES = _EQUIPMENT_COMPONENT_ATTACHMENT_TYPES | {
    "CSkeletonBoneSlot",
}
_EQUIPMENT_VISUAL_COMPONENT_TYPES = {
    "CMeshComponent",
    "CStaticMeshComponent",
    "CFurComponent",
    "CClothComponent",
    "CMeshClothComponent",
}
_SKINNED_MESH_PROFILE_CACHE = {}


def _iter_equipment_profile_chunks(entity):
    for chunk in _get_entity_static_mesh_chunks(entity):
        if chunk is not None:
            yield chunk

    for appearance in _get_entry_attr(entity, "appearances", []) or []:
        for template in _get_entry_attr(appearance, "includedTemplates", []) or []:
            for chunk in _get_entry_attr(template, "chunks", []) or []:
                if chunk is not None:
                    yield chunk


def _chunk_has_visual_payload(chunk) -> bool:
    if chunk is None:
        return False
    if _get_entry_attr(chunk, "mesh", None):
        return True
    if _get_entry_attr(chunk, "resource", None):
        return True
    return _get_entry_component_type(chunk) in _EQUIPMENT_VISUAL_COMPONENT_TYPES


def _mesh_skinning_cache_key(mesh_path, embedded_cmesh_chunk_index=None):
    if not mesh_path:
        return None
    try:
        normalized = os.path.normcase(os.path.normpath(mesh_path))
    except Exception:
        normalized = str(mesh_path)
    try:
        selected_index = int(embedded_cmesh_chunk_index) if embedded_cmesh_chunk_index is not None else None
    except Exception:
        selected_index = str(embedded_cmesh_chunk_index)
    try:
        return (
            normalized,
            selected_index,
            os.path.getmtime(mesh_path),
            os.path.getsize(mesh_path),
        )
    except Exception:
        return (normalized, selected_index)


def _mesh_path_is_skinned(mesh_path, version=999, embedded_cmesh_chunk_index=None) -> bool:
    mesh_path = str(mesh_path or "").strip()
    if not mesh_path:
        return False

    resolved_path = mesh_path if os.path.isabs(mesh_path) else repo_file(mesh_path, version)
    resolved_path = str(resolved_path or "").strip()
    safe_resolved_path = win_safe_path(resolved_path) if resolved_path else ""
    if not safe_resolved_path or not os.path.exists(safe_resolved_path):
        return False

    selected_cmesh_chunk_index = None
    if embedded_cmesh_chunk_index is not None:
        selected_cmesh_chunk_index = int(embedded_cmesh_chunk_index)

    cache_key = _mesh_skinning_cache_key(safe_resolved_path, selected_cmesh_chunk_index)
    if cache_key in _SKINNED_MESH_PROFILE_CACHE:
        return _SKINNED_MESH_PROFILE_CACHE[cache_key]

    is_skinned = False
    try:
        from ..CR2W import dc_mesh
        from ..CR2W.Types.SBufferInfos import EMeshVertexType

        CData, _buffer_infos, _material_names, _materials, _mesh_name, _mesh_file = dc_mesh.load_bin_mesh(
            resolved_path,
            False,
            False,
            embedded_cmesh_chunk_index=selected_cmesh_chunk_index,
        )
        mesh_infos = getattr(CData, "meshInfos", None) or []
        is_skinned = any(
            getattr(mesh_info, "vertexType", None) == EMeshVertexType.EMVT_SKINNED
            for mesh_info in mesh_infos
        )
        if not is_skinned:
            bone_data = getattr(CData, "boneData", None)
            bone_count = int(getattr(bone_data, "nbBones", 0) or 0)
            is_skinned = bone_count > 0 and any(
                int(getattr(mesh_info, "numBonesPerVertex", 0) or 0) > 0
                for mesh_info in mesh_infos
            )
    except Exception as exc:
        log.debug("Mesh skinning probe failed for '%s': %s", resolved_path, exc)
        is_skinned = False

    _SKINNED_MESH_PROFILE_CACHE[cache_key] = bool(is_skinned)
    return bool(is_skinned)


def _entity_has_skinned_mesh_payload(profile_chunks, version=999) -> bool:
    for chunk in profile_chunks or []:
        mesh_path = _get_entry_attr(chunk, "mesh", None)
        if not mesh_path:
            continue
        if _mesh_path_is_skinned(mesh_path, version):
            return True
        embedded_source_path = str(_get_entry_attr(chunk, "_embedded_source_path", "") or "").strip()
        embedded_cmesh_chunk_index = _get_entry_attr(
            chunk,
            "_embedded_cmesh_chunk_index",
            _get_entry_attr(chunk, "_embedded_mesh_chunk_index", None),
        )
        if embedded_source_path and embedded_cmesh_chunk_index is not None:
            if _mesh_path_is_skinned(
                embedded_source_path,
                version,
                embedded_cmesh_chunk_index=embedded_cmesh_chunk_index,
            ):
                return True
    return False


def _build_equipment_attachment_profile(*, root_component_type, has_armature_root,
                                        has_internal_attachment_graph, has_component_attachments,
                                        has_owner_component_attachments,
                                        has_dangle_graph,
                                        has_skinned_mesh_payload,
                                        has_streamed_components_only, has_visual_components,
                                        has_inventory_entries):
    if (
        has_inventory_entries
        and not has_armature_root
        and not has_internal_attachment_graph
        and not has_visual_components
    ):
        kind = "inventory_wrapper"
    elif has_owner_component_attachments or has_dangle_graph or root_component_type in {
        "CMovingPhysicalAgentComponent",
        "CMimicComponent",
        "CAnimDangleComponent",
    }:
        kind = "owner_graph"
    # Skinned mesh-only item entities often import an armature from the mesh data
    # even when the entity template itself has no explicit skeleton property.
    elif has_armature_root or has_skinned_mesh_payload:
        kind = "slot_animated"
    else:
        kind = "slot_visual"

    requires_owner_root_binding = kind == "owner_graph"
    requires_slot_mount = kind in {"slot_visual", "slot_animated"}
    if kind == "inventory_wrapper":
        requires_slot_mount = False

    return EquipmentAttachmentProfile(
        kind=kind,
        has_internal_attachment_graph=has_internal_attachment_graph,
        has_armature_root=has_armature_root,
        has_streamed_components_only=has_streamed_components_only,
        has_component_attachments=has_component_attachments,
        has_owner_component_attachments=has_owner_component_attachments,
        has_dangle_graph=has_dangle_graph,
        has_skinned_mesh_payload=has_skinned_mesh_payload,
        has_visual_components=has_visual_components,
        requires_owner_root_binding=requires_owner_root_binding,
        requires_slot_mount=requires_slot_mount,
        root_component_type=str(root_component_type or "").strip(),
    )


def classify_equipment_attachment_profile(entity) -> EquipmentAttachmentProfile:
    if entity is None:
        return EquipmentAttachmentProfile(
            kind="slot_visual",
            requires_owner_root_binding=False,
            requires_slot_mount=True,
        )

    import_profile = inspect_entity_import_profile(entity)
    return _build_equipment_attachment_profile(
        root_component_type=import_profile.get("root_component_type", ""),
        has_armature_root=bool(import_profile.get("has_armature_root")),
        has_internal_attachment_graph=bool(import_profile.get("has_internal_attachment_graph")),
        has_component_attachments=bool(import_profile.get("has_component_attachments")),
        has_owner_component_attachments=bool(import_profile.get("has_owner_component_attachments")),
        has_dangle_graph=bool(import_profile.get("has_dangle_graph")),
        has_skinned_mesh_payload=bool(import_profile.get("has_skinned_mesh_payload")),
        has_streamed_components_only=bool(import_profile.get("has_streamed_components_only")),
        has_visual_components=bool(import_profile.get("has_visual_components")),
        has_inventory_entries=entity_has_inventory_entries(entity),
    )


def inspect_entity_import_profile(entity):
    """Describe an entity using the same root signals the importer can actually consume."""
    profile_chunks = list(_iter_equipment_profile_chunks(entity))
    appearances = list(_get_entry_attr(entity, "appearances", []) or [])
    entity_version = _coerce_version(getattr(entity, "version", None), 999)
    has_visual_components = any(
        _chunk_has_visual_payload(chunk)
        for chunk in profile_chunks
    )
    base_mesh_count = sum(1 for chunk in profile_chunks if _chunk_has_visual_payload(chunk))

    root_entry = None
    root_component_type = ""
    root_path_key = ""
    root_path = ""

    moving_component = _get_entry_attr(entity, "MovingPhysicalAgentComponent", None)
    moving_skeleton = str(_get_entry_attr(moving_component, "skeleton", "") or "").strip()
    if moving_skeleton:
        root_entry = moving_component
        root_component_type = _get_entry_component_type(moving_component, fallback="CMovingPhysicalAgentComponent")
        root_path_key = "skeleton"
        root_path = moving_skeleton
    else:
        for chunk in profile_chunks:
            skeleton_path = str(_get_entry_attr(chunk, "skeleton", "") or "").strip()
            if not skeleton_path:
                continue
            root_entry = chunk
            root_component_type = _get_entry_component_type(chunk, fallback="CAnimatedComponent")
            root_path_key = "skeleton"
            root_path = skeleton_path
            break
        if root_entry is None:
            for chunk in profile_chunks:
                mimic_face = str(_get_entry_attr(chunk, "mimicFace", "") or "").strip()
                if not mimic_face:
                    continue
                root_entry = chunk
                root_component_type = _get_entry_component_type(chunk, fallback="CMimicComponent")
                root_path_key = "mimicFace"
                root_path = mimic_face
                break

    chunk_types = {
        _get_entry_component_type(chunk)
        for chunk in profile_chunks
        if chunk is not None
    }
    has_component_attachments = any(
        chunk_type in _EQUIPMENT_COMPONENT_ATTACHMENT_TYPES
        for chunk_type in chunk_types
    )
    has_owner_component_attachments = any(
        chunk_type in _EQUIPMENT_OWNER_ATTACHMENT_TYPES
        for chunk_type in chunk_types
    )
    has_dangle_graph = any(
        chunk_type == "CAnimDangleComponent" or str(chunk_type).startswith("CAnimDangleConstraint")
        for chunk_type in chunk_types
    )
    has_skinned_mesh_payload = _entity_has_skinned_mesh_payload(profile_chunks, version=entity_version)
    has_internal_attachment_graph = any(
        chunk_type in _EQUIPMENT_INTERNAL_ATTACHMENT_TYPES
        for chunk_type in chunk_types
    )
    has_streamed_components_only = bool(profile_chunks) and has_visual_components and not bool(root_entry) and not has_internal_attachment_graph and all(
        _chunk_has_visual_payload(chunk)
        for chunk in profile_chunks
        if chunk is not None
    )

    preferred_import_mode = "template"
    if root_entry is not None or (appearances and base_mesh_count == 0):
        preferred_import_mode = "direct"

    attachment_profile = _build_equipment_attachment_profile(
        root_component_type=root_component_type,
        has_armature_root=root_entry is not None,
        has_internal_attachment_graph=has_internal_attachment_graph,
        has_component_attachments=has_component_attachments,
        has_owner_component_attachments=has_owner_component_attachments,
        has_dangle_graph=has_dangle_graph,
        has_skinned_mesh_payload=has_skinned_mesh_payload,
        has_streamed_components_only=has_streamed_components_only,
        has_visual_components=has_visual_components,
        has_inventory_entries=entity_has_inventory_entries(entity),
    )

    return {
        "root_entry": root_entry,
        "root_component_type": root_component_type,
        "root_path_key": root_path_key,
        "root_path": root_path,
        "has_armature_root": root_entry is not None,
        "base_mesh_count": base_mesh_count,
        "has_appearances": bool(appearances),
        "has_component_attachments": has_component_attachments,
        "has_owner_component_attachments": has_owner_component_attachments,
        "has_dangle_graph": has_dangle_graph,
        "has_skinned_mesh_payload": has_skinned_mesh_payload,
        "has_internal_attachment_graph": has_internal_attachment_graph,
        "has_streamed_components_only": has_streamed_components_only,
        "has_visual_components": has_visual_components,
        "preferred_import_mode": preferred_import_mode,
        "attachment_profile": attachment_profile,
        "attachment_kind": attachment_profile.kind,
    }


def _normalize_animset_paths(value):
    if value is None:
        return []
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        values = [value]

    out = []
    seen = set()
    for candidate in values:
        path = ""
        if isinstance(candidate, str):
            path = candidate
        elif isinstance(candidate, dict):
            path = candidate.get("path") or candidate.get("DepotPath") or candidate.get("depotPath") or candidate.get("_depotPath") or candidate.get("_value") or ""
        else:
            path = getattr(candidate, "path", None) or getattr(candidate, "DepotPath", None) or getattr(candidate, "depotPath", None) or ""
        path = str(path or "").strip().replace("/", "\\")
        if not path:
            continue
        if path.lower().endswith(".json"):
            path = path[:-5]
        if not path.lower().endswith(".w2anims"):
            continue
        key = path.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def _iter_entity_anim_components(entity):
    moving_component = _get_entry_attr(entity, "MovingPhysicalAgentComponent", None)
    if moving_component is not None:
        yield moving_component

    for chunk in _get_entity_static_mesh_chunks(entity):
        if chunk is not None:
            yield chunk

    for appearance in _get_entry_attr(entity, "appearances", []) or []:
        for template in _get_entry_attr(appearance, "includedTemplates", []) or []:
            for chunk in _get_entry_attr(template, "chunks", []) or []:
                if chunk is not None:
                    yield chunk


def _find_anim_component_for_armature(entity, armature_obj):
    component_name = str(getattr(armature_obj, "get", lambda *_args, **_kwargs: "")("witcher_name", "") or "").strip()
    component_type = str(getattr(armature_obj, "get", lambda *_args, **_kwargs: "")("witcher_type", "") or "").strip()
    candidates = []
    for chunk in _iter_entity_anim_components(entity):
        chunk_type = str(_get_entry_attr(chunk, "type", "") or "").strip()
        if chunk_type not in {"CMovingPhysicalAgentComponent", "CAnimatedComponent", "CAnimDangleBufferComponent", "CMimicComponent"}:
            continue
        candidates.append(chunk)

    if component_name:
        for chunk in candidates:
            if str(_get_entry_attr(chunk, "name", "") or "").strip() == component_name:
                return chunk

    if component_type:
        for chunk in candidates:
            if str(_get_entry_attr(chunk, "type", "") or "").strip() == component_type:
                return chunk

    moving_component = _get_entry_attr(entity, "MovingPhysicalAgentComponent", None)
    if moving_component is not None:
        return moving_component

    if len(candidates) == 1:
        return candidates[0]
    return None


def _collect_armature_animset_groups(entity, armature_obj):
    groups = []
    group_lookup = {}
    try:
        component_name = str(armature_obj.get("witcher_name", "") or "").strip()
    except Exception:
        component_name = ""
    try:
        component_type = str(armature_obj.get("witcher_type", "") or "").strip()
    except Exception:
        component_type = ""

    def _add_group(raw_name, paths, component_name=""):
        animset_paths = _normalize_animset_paths(paths)
        if not animset_paths:
            return
        group_name = str(raw_name or "").strip() or "AnimSets"
        component_name = str(component_name or "").strip()
        group_key = (group_name, component_name)
        group_paths = group_lookup.get(group_key)
        if group_paths is None:
            group_paths = []
            group_lookup[group_key] = group_paths
            groups.append((group_name, group_paths, component_name))
        seen = {path.lower() for path in group_paths}
        for path in animset_paths:
            key = path.lower()
            if key in seen:
                continue
            seen.add(key)
            group_paths.append(path)

    component_chunk = _find_anim_component_for_armature(entity, armature_obj)
    if component_chunk is not None and component_type != "CMimicComponent":
        component_group_name = str(_get_entry_attr(component_chunk, "name", "") or "").strip()
        if not component_group_name:
            chunk_type = str(_get_entry_attr(component_chunk, "type", "") or "").strip()
            component_group_name = "Main" if chunk_type == "CMovingPhysicalAgentComponent" else (chunk_type or "AnimSets")
        _add_group(component_group_name, _get_entry_attr(component_chunk, "animationSets", []), _get_entry_attr(component_chunk, "name", ""))

    animset_params = getattr(entity, "CAnimAnimsetsParam", []) or []
    for animset_param in animset_params:
        param_component_name = str(_get_entry_attr(animset_param, "componentName", "") or "").strip()
        _add_group(
            _get_entry_attr(animset_param, "name", "AnimSets"),
            _get_entry_attr(animset_param, "animationSets", []),
            param_component_name,
        )

    include_mimic_sets = component_type == "CMimicComponent" or bool(
        str(getattr(armature_obj, "get", lambda *_args, **_kwargs: "")("mimicFaceFile", "") or "").strip()
    )
    if not include_mimic_sets and component_type in {"", "CMovingPhysicalAgentComponent", "CAnimatedComponent"}:
        include_mimic_sets = any(_iter_entity_mimic_animset_params(entity))
    if include_mimic_sets:
        for mimic_set in _iter_entity_mimic_animset_params(entity):
            _add_group(
                f"{_get_entry_attr(mimic_set, 'name', 'MimicSets')} (Mimic)",
                _get_entry_attr(mimic_set, "animationSets", []),
                _get_entry_attr(mimic_set, "componentName", ""),
            )

    return [(group_name, paths, component_name) for group_name, paths, component_name in groups if paths]


def _populate_idle_animation(rig_settings, entity):
    """Read all .w2beh files on the entity and store the best idle animation name."""
    try:
        should_import_idle = bool(get_all_addon_prefs(bpy.context).import_idle_animation)
    except Exception:
        should_import_idle = False
    if not should_import_idle:
        rig_settings.idle_animation_name = ""
        return

    beh_paths = getattr(entity, "beh_paths", None) or []
    if not beh_paths:
        return

    def _idle_rank(anim_name):
        lo = str(anim_name or "").lower()
        if not lo:
            return 99
        if "locomotion" in lo and "idle" in lo and not _BEH_IDLE_TRANSITION_RE.search(lo) and not _BEH_IDLE_DOWNGRADE_RE.search(lo):
            return 1
        if "standing" in lo and "idle" in lo and not _BEH_IDLE_TRANSITION_RE.search(lo) and not _BEH_IDLE_DOWNGRADE_RE.search(lo):
            return 2
        if "idle" in lo and not _BEH_IDLE_TRANSITION_RE.search(lo) and not _BEH_IDLE_DOWNGRADE_RE.search(lo):
            return 3
        if "locomotion" in lo and "idle" in lo and not _BEH_IDLE_TRANSITION_RE.search(lo):
            return 4
        if "idle" in lo and not _BEH_IDLE_TRANSITION_RE.search(lo):
            return 5
        if "idle" in lo:
            return 6
        return 7

    def _beh_path_priority(depot_path, abs_path):
        base = os.path.basename(str(depot_path or "")).lower()
        priority = 100
        if "overlay" in base:
            priority -= 60
        if "locomotion" in base or "idle" in base:
            priority -= 20
        if "main" in base:
            priority -= 10
        if "gameplay" in base:
            priority += 40
        if "swimming" in base:
            priority += 60
        if "constraint" in base:
            priority += 80
        try:
            size = os.path.getsize(abs_path)
        except Exception:
            size = 1 << 60
        return (priority, size, base)

    deadline = time.perf_counter() + (_BEH_IDLE_BUDGET_MS / 1000.0)
    resolved_entries = []
    candidates = []  # [(anim_name, depot_path)]
    for depot_path in beh_paths:
        try:
            abs_path = repo_file(depot_path)
        except Exception:
            continue
        if not abs_path or not os.path.isfile(abs_path):
            continue
        resolved_entries.append((depot_path, abs_path))

    resolved_entries.sort(key=lambda item: _beh_path_priority(item[0], item[1]))

    best_rank = 99
    for item_index, (depot_path, abs_path) in enumerate(resolved_entries):
        if candidates and best_rank <= 1:
            break
        if item_index > 0 and time.perf_counter() >= deadline:
            break
        try:
            info = _read_beh_info(abs_path)
        except Exception:
            continue
        if info.idle_animation:
            candidates.append((info.idle_animation, depot_path))
            best_rank = min(best_rank, _idle_rank(info.idle_animation))
            log.debug("beh idle candidate: %s  (from %s)", info.idle_animation, depot_path)

    if not candidates:
        return

    # Apply the same heuristic used within a single beh to choose across beh files.
    # This ensures locomotion_idle beats combat_locomotion_*_idle or man_carry_crate_idle
    # even when they come from different beh files.
    chosen_name = _beh_guess_idle([name for name, _ in candidates])
    chosen_path = next((path for name, path in candidates if name == chosen_name), candidates[0][1])

    rig_settings.idle_animation_name = chosen_name
    log.debug("beh idle animation: %s  (from %s)", chosen_name, chosen_path)


def _get_inventory_item_name(entry):
    item_raw = _get_entry_attr(entry, "item", "") or ""
    initializer = _get_entry_attr(entry, "initializer", None)
    if initializer is not None:
        init_item = _get_entry_attr(initializer, "itemName", None) or _get_entry_attr(initializer, "item", None)
        if init_item:
            item_raw = init_item
    return item_raw

def _get_inventory_category(entry):
    return _get_entry_attr(entry, "category", "") or ""

def _get_inventory_equip_template(entry):
    return (
        _get_entry_attr(entry, "equip_template", "")
        or _get_entry_attr(entry, "template", "")
        or _get_entry_attr(entry, "templateName", "")
        or ""
    )

def _normalize_key(value):
    if value is None:
        return ""
    return str(value).strip().lower()

def _canonical_key(value):
    """Loose key used to match names with different separators (space/_/-)."""
    return re.sub(r"[^a-z0-9]+", "", _normalize_key(value))

def _candidate_item_keys(item_raw):
    if not item_raw:
        return []
    raw = str(item_raw).strip()
    if not raw:
        return []

    keys = [raw]
    pathish = raw.replace("\\", "/")
    base = os.path.basename(pathish)
    if base and base != raw:
        keys.append(base)
    root, ext = os.path.splitext(base)
    if root and root != base:
        keys.append(root)
    # Normalize and dedupe
    seen = set()
    out = []
    for k in keys:
        nk = _normalize_key(k)
        ck = _canonical_key(k)
        if nk and nk not in seen:
            seen.add(nk)
            out.append(nk)
        if ck and ck not in seen:
            seen.add(ck)
            out.append(ck)
    return out

def _derive_template_from_item(item_raw):
    if not item_raw:
        return ""
    raw = str(item_raw).strip()
    if not raw:
        return ""
    pathish = raw.replace("\\", "/")
    base = os.path.basename(pathish)
    root, ext = os.path.splitext(base)
    if ext.lower() == ".w2ent":
        return root
    if base != raw and root:
        return root
    # Fallback for display labels like "Zireael Sword" -> "zireael_sword".
    slug = re.sub(r"[^0-9A-Za-z]+", "_", base or raw).strip("_")
    if slug and slug.lower() != (base or raw).lower():
        return slug.lower()
    return ""

def _ensure_equipment_catalog_loaded(search_roots=None):
    """Best-effort load of equipment XML definitions before inventory matching."""
    try:
        from ..ui.ui_equipment import (
            EquipmentDefinitionEntry,
            ensure_equipment_catalog_for_search_roots,
            get_equipment_catalog_for_search_roots,
            get_equipment_source_game_for_search_roots,
        )
    except Exception:
        return
    source_game = get_equipment_source_game_for_search_roots(search_roots)
    try:
        if source_game == "w2":
            ensure_equipment_catalog_for_search_roots(search_roots)
    except Exception:
        pass
    _category_items, item_attributes = get_equipment_catalog_for_search_roots(search_roots)
    if item_attributes:
        return
    if source_game == "w2":
        return
    try:
        result = bpy.ops.witcher.equipment_refresh_categories()
        if isinstance(result, set) and "CANCELLED" in result:
            log.warning("Equipment XML refresh was cancelled; inventory item lookup may be incomplete.")
    except Exception:
        # Missing XML source is non-fatal; keep fallback matching behavior.
        pass

def _add_lookup_aliases(lookup, key, value):
    def _should_replace(existing_value, new_value):
        try:
            existing_template = existing_value[2] if isinstance(existing_value, tuple) and len(existing_value) >= 3 else ""
            new_template = new_value[2] if isinstance(new_value, tuple) and len(new_value) >= 3 else ""
        except Exception:
            return False
        return (not existing_template) and bool(new_template)

    nk = _normalize_key(key)
    if nk and (nk not in lookup or _should_replace(lookup.get(nk), value)):
        lookup[nk] = value
    ck = _canonical_key(key)
    if ck and (ck not in lookup or _should_replace(lookup.get(ck), value)):
        lookup[ck] = value

def _build_equipment_lookup(search_roots=None):
    """Build lookup tables from EquipmentDefinitionEntry for fast inventory matching."""
    _ensure_equipment_catalog_loaded(search_roots)
    try:
        from ..ui.ui_equipment import get_equipment_catalog_for_search_roots
    except Exception:
        return {}, {}
    category_items, item_attributes = get_equipment_catalog_for_search_roots(search_roots)

    item_lookup = {}
    template_lookup = {}
    for category, items in category_items.items():
        for item_name, _display, template in items:
            if item_name:
                _add_lookup_aliases(item_lookup, item_name, (category, item_name, template))
            if template:
                _add_lookup_aliases(template_lookup, template, (category, item_name, template))
                # Also allow template without extension if present
                root, ext = os.path.splitext(template)
                if root and ext:
                    _add_lookup_aliases(template_lookup, root, (category, item_name, template))

    # Some XML merges may leave richer data in item_attributes than in category_items.
    # Backfill lookups from item_attributes so exact item IDs (e.g. Q1_axe1h)
    # still resolve to their equip_template.
    for item_name, attrs in item_attributes.items():
        if not item_name or not isinstance(attrs, dict):
            continue
        attr_category = attrs.get("category", "")
        attr_template = attrs.get("equip_template", "")
        lookup_value = (attr_category, item_name, attr_template)
        _add_lookup_aliases(item_lookup, item_name, lookup_value)
        if attr_template:
            _add_lookup_aliases(template_lookup, attr_template, lookup_value)
            root, ext = os.path.splitext(attr_template)
            if root and ext:
                _add_lookup_aliases(template_lookup, root, lookup_value)
    return item_lookup, template_lookup

def _resolve_inventory_item(item_raw, item_lookup, template_lookup):
    for key in _candidate_item_keys(item_raw):
        if key in item_lookup:
            return item_lookup[key]
        if key in template_lookup:
            return template_lookup[key]
    return None


def _lookup_item_attrs(item_attributes, source_game, *identifiers):
    """Catalog attrs by exact key, then alias lookup; a miss silently loses equip_slot/bound_items."""
    for identifier in identifiers:
        if not identifier:
            continue
        attrs = item_attributes.get(str(identifier))
        if attrs:
            return attrs
    try:
        from ..ui.equipment_catalog import get_item_attributes_by_identifier
    except Exception:
        return {}
    for identifier in identifiers:
        if not identifier:
            continue
        attrs = get_item_attributes_by_identifier(str(identifier), source_game)
        if attrs:
            return attrs
    return {}

def _find_slot_by_item_or_template(slots, item_raw):
    keys = set(_candidate_item_keys(item_raw))
    if not keys:
        return None, None
    for idx, slot in enumerate(slots):
        if _normalize_key(slot.item_name) in keys or _normalize_key(slot.equip_template) in keys:
            return idx, slot
    return None, None

def _apply_inventory_mounts(context, armature, selected_appearance, rig_settings, entity=None, shared_inventory=False,
                            prepared_context=None, post_refresh=True):
    """Apply mounted inventory items to equipment slots and load them."""
    _mounts_started = time.perf_counter()
    inv_entries = list(_iter_inventory_entries(selected_appearance, entity))
    if not inv_entries:
        return

    source_roots = list((prepared_context or {}).get("source_roots") or [])
    if not source_roots:
        source_roots = _get_armature_source_roots(armature)
    if not source_roots:
        repo_path_hint = getattr(rig_settings, "repo_path", "") or ""
        if repo_path_hint and os.path.isabs(repo_path_hint):
            try:
                source_roots = _build_entity_source_roots(repo_path_hint)
            except Exception:
                source_roots = []

    try:
        from ..ui.ui_equipment import (
            _get_cached_equipment_item_entity,
            _prepare_equipment_load_context,
            _resolve_bundle_item_by_template_cached,
            _resolve_slot_visual_policy,
            get_effective_equip_template,
            get_equipment_catalog_for_search_roots,
            get_equipment_source_game_for_search_roots,
            load_equipment_items_batch,
            refresh_variant_states,
        )
    except Exception:
        return
    prepared = prepared_context if prepared_context is not None else {}
    prepared.setdefault("source_roots", source_roots)
    prepared = _prepare_equipment_load_context(armature, rig_settings, prepared)
    source_roots = prepared.get("source_roots", source_roots)

    _lookup_started = time.perf_counter()
    item_lookup, template_lookup = _build_equipment_lookup(source_roots)
    _lookup_seconds = time.perf_counter() - _lookup_started
    log.info(
        "[mounts-profile] _build_equipment_lookup %.3fs (%d inv entries, items=%d templates=%d)",
        _lookup_seconds, len(inv_entries), len(item_lookup), len(template_lookup),
    )
    _rig_source_game_raw = str(getattr(rig_settings, "source_game", "") or "").strip().lower()
    source_game = _rig_source_game_raw
    if source_game not in {"w2", "w3"}:
        source_game = get_equipment_source_game_for_search_roots(source_roots)
    slots = rig_settings.equipment_slots
    # IMPORTANT: store ONLY indices, not slot PropertyGroup references.
    # bpy_prop_collection.add() can reallocate the underlying RNA array,
    # invalidating any cached PropertyGroup refs. Re-fetch via slots[idx]
    # on each use to stay safe across additions.
    slot_by_category = {slot.category: idx for idx, slot in enumerate(slots) if slot.category}
    slot_search_list = slots

    try:
        refresh_variant_states(rig_settings)
    except Exception:
        pass

    # Keep shared inventory stable across appearance switches, but only skip
    # work when current slots already represent the same mounted entries.
    if shared_inventory:
        existing_inventory_slots = [slot for slot in slots if getattr(slot, "is_inventory", False)]
        if existing_inventory_slots:
            desired_mounts = set()
            for entry in inv_entries:
                category_raw = _get_inventory_category(entry)
                item_raw = _get_inventory_item_name(entry)
                entry_template = _get_inventory_equip_template(entry)
                override = _get_inventory_mount_override(rig_settings, category_raw, item_raw)
                if override is False:
                    continue
                if override is not True and not _inventory_entry_is_mount(entry):
                    continue
                item_key = _normalize_key(item_raw) or _normalize_key(entry_template)
                if not item_key or item_key in {"none", "random", "null"}:
                    continue
                category_key = _normalize_key(category_raw)
                desired_mounts.add((category_key, item_key))

            existing_mounts = {
                (
                    _normalize_key(getattr(slot, "category", "")),
                    _normalize_key(getattr(slot, "item_name", "")),
                )
                for slot in existing_inventory_slots
            }
            existing_loaded = True
            for slot in existing_inventory_slots:
                if not (getattr(slot, "is_loaded", False) and getattr(slot, "equip_guid", "")):
                    existing_loaded = False
                    break
            if desired_mounts and desired_mounts.issubset(existing_mounts) and existing_loaded:
                return
    seen_entries = set()

    _catalog_started = time.perf_counter()
    category_items, item_attributes = get_equipment_catalog_for_search_roots(source_roots)
    _catalog_seconds = time.perf_counter() - _catalog_started
    _resolveloop_started = time.perf_counter()
    slots_to_load = []

    def _category_items(category_name):
        if not category_name:
            return []
        cat_items = category_items.get(category_name, [])
        if cat_items:
            return cat_items
        wanted = _normalize_key(category_name)
        for cat_key, cat_vals in category_items.items():
            if _normalize_key(cat_key) == wanted:
                return cat_vals
        return []

    def _first_template_for_category(category_name):
        for _name, _display, tmpl in _category_items(category_name):
            if tmpl and str(tmpl).lower() != "none":
                return tmpl
        return ""

    for entry in inv_entries:
        category_raw = _get_inventory_category(entry)
        item_raw = _get_inventory_item_name(entry)
        entry_template = _get_inventory_equip_template(entry)
        if source_game == "w2":
            entry_w2_path = str(_get_entry_attr(entry, "w2_entity_path", "") or "").strip()
            if entry_w2_path:
                entry_template = entry_w2_path
        override = _get_inventory_mount_override(rig_settings, category_raw, item_raw)
        if override is False:
            continue
        if override is not True and not _inventory_entry_is_mount(entry):
            continue
        is_mount = True
        dedupe_key = (_normalize_key(category_raw), _normalize_key(item_raw) or _normalize_key(entry_template))
        if dedupe_key in seen_entries:
            continue
        seen_entries.add(dedupe_key)
        item_key = _normalize_key(item_raw) or _normalize_key(entry_template)
        if not item_key or item_key in {"none", "random", "null"}:
            continue

        slot_index = None
        slot = None
        resolved_category = ""
        resolved_item_name = ""
        resolved_template = ""
        resolved = _resolve_inventory_item(item_raw, item_lookup, template_lookup)
        if not resolved and entry_template:
            resolved = _resolve_inventory_item(entry_template, item_lookup, template_lookup)
        if resolved:
            resolved_category, resolved_item_name, resolved_template = resolved

        # Prefer slot by inventory category, then resolved category, then item/template match.
        # NOTE: slot_by_category stores INDICES only (not refs) — see init comment.
        # Always re-fetch slot via slots[idx] because slots.add() may have invalidated
        # any older PropertyGroup ref via RNA array realloc.
        if category_raw and category_raw in slot_by_category:
            slot_index = slot_by_category[category_raw]
            slot = slots[slot_index] if 0 <= slot_index < len(slots) else None
        elif resolved_category and resolved_category in slot_by_category:
            slot_index = slot_by_category[resolved_category]
            slot = slots[slot_index] if 0 <= slot_index < len(slots) else None
        else:
            slot_index, _stale_slot = _find_slot_by_item_or_template(slot_search_list, item_raw)
            if slot_index is None and entry_template:
                slot_index, _stale_slot = _find_slot_by_item_or_template(slot_search_list, entry_template)
            slot = slots[slot_index] if (slot_index is not None and 0 <= slot_index < len(slots)) else None

        slot_was_created = False
        # If no slot exists, create one for this mounted inventory item
        if slot is None and is_mount:
            new_category = category_raw or resolved_category or _derive_template_from_item(item_raw) or str(item_raw)
            if not new_category:
                new_category = f"inventory_{len(slots)}"
            if new_category in slot_by_category:
                slot_index = slot_by_category[new_category]
                slot = slots[slot_index] if 0 <= slot_index < len(slots) else None
            else:
                slot = slots.add()
                slot.source_game = source_game
                slot.category = new_category
                slot.resolved_repo_path = ""
                slot_index = len(slots) - 1
                slot_by_category[new_category] = slot_index
                slot_was_created = True
                # Re-fetch by index after add() in case the local ref got
                # invalidated by the same realloc that may invalidate older refs.
                slot = slots[slot_index]

        if slot is None:
            continue

        if not slot.category:
            fallback = category_raw or resolved_category or _derive_template_from_item(item_raw) or str(item_raw)
            if not fallback:
                fallback = f"inventory_{slot_index}"
            base_fallback = fallback
            counter = 2
            while fallback in slot_by_category:
                fallback = f"{base_fallback}_{counter}"
                counter += 1
            slot.category = fallback
            slot_by_category[fallback] = slot_index

        slot.source_game = source_game
        if shared_inventory:
            slot.is_inventory = True

        # Determine item name / template
        if resolved_item_name:
            item_name = resolved_item_name
        else:
            item_name = _derive_template_from_item(item_raw) or str(item_raw or entry_template)

        template = entry_template or resolved_template
        if not template:
            # Try category-specific lookup for this item name.
            for name, _display, tmpl in _category_items(category_raw):
                if _normalize_key(name) == _normalize_key(item_name):
                    template = tmpl
                    break
            if not template and resolved_category:
                for name, _display, tmpl in _category_items(resolved_category):
                    if _normalize_key(name) == _normalize_key(item_name):
                        template = tmpl
                        break
            if not template:
                # If item ID is abstract (e.g. Q1_axe1h), fall back to first
                # concrete template from the category.
                template = _first_template_for_category(category_raw) or _first_template_for_category(resolved_category)
            if not template and entry_template:
                template = entry_template
            if not template:
                template = _derive_template_from_item(item_raw)
        if not template:
            template = item_name

        slot.item_name = item_name
        slot.equip_template = template

        attrs = _lookup_item_attrs(item_attributes, source_game, item_name, item_raw, template)
        if attrs:
            slot.equip_slot = attrs.get('equip_slot', slot.equip_slot)
            slot.hold_slot = attrs.get('hold_slot', slot.hold_slot)
            slot.weapon = attrs.get('weapon', slot.weapon)
            slot.attachment_type = attrs.get('attachment_type', '')
        # Always assign: a reused slot must not keep another item's variants/bound items.
        try:
            slot.variants_json = json.dumps(attrs.get('variants', []))
        except Exception:
            slot.variants_json = ""
        try:
            slot.bound_items_json = json.dumps(attrs.get('bound_items', []))
        except Exception:
            slot.bound_items_json = ""
        slot.base_equip_template = template

        if slot is None or slot_index is None:
            continue
        if not slot.equip_template or slot.equip_template == "None":
            continue

        try:
            refresh_variant_states(rig_settings)
        except Exception:
            pass

        export_path = ""
        item_entity = None
        effective_template = get_effective_equip_template(slot)
        if effective_template and effective_template != "None":
            _resolved_item, export_path, _search_pattern = _resolve_bundle_item_by_template_cached(
                effective_template,
                search_roots=source_roots,
                prepared_context=prepared,
            )
            if export_path:
                item_entity = _get_cached_equipment_item_entity(export_path, prepared_context=prepared)

        if effective_template and effective_template != "None" and not export_path and bool(getattr(slot, "weapon", False)):
            log.info(
                "Skipping nonvisual inventory weapon '%s': template '%s' does not resolve to an entity file",
                item_name,
                effective_template,
            )
            if slot.is_loaded and slot.equip_guid:
                remove_objects_by_guid(slot.equip_guid, "witcher_equip_guid")
                slot.equip_guid = ""
                slot.is_loaded = False
                slot.is_in_hold_slot = False
            if slot_was_created or getattr(slot, "is_inventory", False):
                try:
                    slots.remove(slot_index)
                except Exception:
                    pass
                slot_by_category = {
                    existing_slot.category: idx
                    for idx, existing_slot in enumerate(slots)
                    if existing_slot.category
                }
                slot_search_list = slots
            continue

        slot_policy = _resolve_slot_visual_policy(slot, armature, rig_settings, item_entity=item_entity)

        if slot.is_loaded and slot.equip_guid and slot_policy["policy"] != "equipable_on_rig":
            remove_objects_by_guid(slot.equip_guid, "witcher_equip_guid")
            slot.equip_guid = ""
            slot.is_loaded = False
            slot.is_in_hold_slot = False

        if slot_policy["policy"] == "nonvisual_on_rig":
            if slot_was_created:
                try:
                    slots.remove(slot_index)
                except Exception:
                    pass
                slot_by_category = {existing_slot.category: idx for idx, existing_slot in enumerate(slots) if existing_slot.category}
                slot_search_list = slots
            continue

        if slot_policy["policy"] != "equipable_on_rig":
            slot.is_in_hold_slot = False
            continue

        if slot.is_loaded and slot.equip_guid:
            remove_objects_by_guid(slot.equip_guid, "witcher_equip_guid")
            slot.equip_guid = ""
            slot.is_loaded = False
            slot.is_in_hold_slot = False

        slots_to_load.append(slot_index)

    _resolveloop_seconds = time.perf_counter() - _resolveloop_started
    _batch_started = time.perf_counter()
    if slots_to_load:
        try:
            load_equipment_items_batch(
                context,
                armature,
                slots_to_load,
                rig_settings,
                prepared_context=prepared,
                post_refresh_variants=post_refresh,
                mount_mode="equip",
            )
        except Exception as exc:
            log.warning(
                "Auto inventory equipment import failed; continuing character import: %s",
                exc,
                exc_info=True,
            )
    _batch_seconds = time.perf_counter() - _batch_started

    log.info(
        "[mounts-profile] _apply_inventory_mounts total %.3fs (lookup %.3fs, catalog %.3fs, "
        "resolve_loop %.3fs, batch_load %.3fs; %d inv entries -> %d slots to load)",
        time.perf_counter() - _mounts_started,
        _lookup_seconds, _catalog_seconds, _resolveloop_seconds, _batch_seconds,
        len(inv_entries), len(slots_to_load),
    )

    # Update variant state after all mounts applied
    try:
        from ..ui.ui_equipment import refresh_variant_states
        refresh_variant_states(rig_settings)
    except Exception:
        pass


def build_template_appearance_map(entity_source):
    """Build a mapping of template filename -> list of appearance indices.
    
    Scans all appearances in the entity and identifies which appearances use each template.
    Returns dict: {template_filename: [app_index_0, app_index_2, ...]}
    """
    template_map = {}
    
    appearances = _get_entry_attr(entity_source, 'appearances', []) or []
    for app_index, appearance in enumerate(appearances):
        app_name = _get_entry_attr(appearance, 'name', str(app_index))
        included_templates = _get_entry_attr(appearance, 'includedTemplates', []) or []
        
        for template in included_templates:
            filename = _get_entry_attr(template, 'templateFilename', '')
            if filename:
                if filename not in template_map:
                    template_map[filename] = {'indices': [], 'names': []}
                if app_index not in template_map[filename]['indices']:
                    template_map[filename]['indices'].append(app_index)
                    template_map[filename]['names'].append(app_name)
    
    return template_map


def build_redcloth_resource_appearance_map(entity_source):
    """Build a mapping of cloth resource key -> appearance indices."""
    resource_map = {}
    appearances = _get_entry_attr(entity_source, 'appearances', []) or []
    for app_index, appearance in enumerate(appearances):
        app_name = _get_entry_attr(appearance, 'name', str(app_index))
        included_templates = _get_entry_attr(appearance, 'includedTemplates', []) or []
        for template in included_templates:
            chunks = _get_entry_attr(template, 'chunks', []) or []
            for chunk in chunks:
                resource = str(_get_entry_attr(chunk, 'resource', '') or '').strip()
                if not _is_cloth_resource_path(resource):
                    continue
                resource_key = _make_redcloth_resource_key(resource)
                if not resource_key:
                    continue
                if resource_key not in resource_map:
                    resource_map[resource_key] = {'indices': [], 'names': []}
                if app_index not in resource_map[resource_key]['indices']:
                    resource_map[resource_key]['indices'].append(app_index)
                    resource_map[resource_key]['names'].append(app_name)
    return resource_map


def get_template_appearances_from_entity(rig_settings, template_filename):
    """Get list of appearance indices that use this template (from entity data)."""
    entity, entity_data = get_rig_entity_state(rig_settings)
    entity_source = entity if entity is not None else entity_data
    if not entity_source:
        return []
    
    template_map = build_template_appearance_map(entity_source)
    if template_filename in template_map:
        return template_map[template_filename]['indices']
    return []


def get_redcloth_resource_appearances_from_entity(rig_settings, resource_key):
    """Get appearance indices that reference a reused redcloth/redapex resource."""
    if rig_settings is None:
        return []
    resource_key = _make_redcloth_resource_key(resource_key)
    if not resource_key:
        return []
    entity, entity_data = get_rig_entity_state(rig_settings)
    entity_source = entity if entity is not None else entity_data
    if not entity_source:
        return []

    resource_map = build_redcloth_resource_appearance_map(entity_source)
    if resource_key in resource_map:
        return resource_map[resource_key]['indices']
    return []


import math

def fov_to_length( fov:float ):
    x = 43.266615300557 # Diagonal measurement for a 'normal' 35mm lens
    if ( fov < 1 or fov > 179 ):
        return None
    return ( x / ( 2 * math.tan( math.pi * fov / 360.0 ) ) )


def length_to_fov( length:float, crop:float = 1.0 ):
    x = 43.266615300557
    if ( length < 1 ):
        return None
    length *= crop
    return (2 * math.tan(x / ( 2.0 * length ) ) * 180.0 / math.pi)


def create_camera_drivers(armobj, camera, name):
    setup_camera_preview_drivers(armobj, camera)

def do_constraints(constrains, objdict, meshdict, HardAttachments, group_parent=None, entity_name=""):
    """
    Process constraints and hard attachments, applying constraints between objects and setting up parenting.

    Parameters:
        constrains (list): List of tuples [(parent_obj_name, child_obj_name), ...]
        objdict (dict): Dictionary mapping object names to Blender objects
        meshdict (dict): Dictionary mapping mesh names to Blender mesh objects
        HardAttachments (list): List of constraints with 'parent_name', 'parentSlotName', 'child_name', 'relativeTransform'
        group_parent (str, optional): Optional parent object name for grouping
        entity_name (str, optional): Owning entity name for skeleton-less root pairs

    Returns:
        list: List of objects that are parented to the group_parent
    """
    return_objs = process_constraints(constrains, objdict, group_parent, entity_name=entity_name)
    process_hard_attachments(HardAttachments, objdict, meshdict)
    return return_objs


def _set_parent_keep_world(obj, parent_obj, *, parent_type='OBJECT', parent_bone=""):
    if obj is None:
        return
    if obj == parent_obj:
        return
    try:
        saved_world = obj.matrix_world.copy()
    except Exception:
        saved_world = None

    obj.parent = parent_obj
    obj.parent_type = parent_type
    obj.parent_bone = parent_bone

    if saved_world is not None:
        try:
            obj.matrix_world = saved_world
        except Exception:
            pass


def _has_copy_transform_to(pose_bone, target_obj, subtarget):
    for constraint in pose_bone.constraints:
        if (
            constraint.type == 'COPY_TRANSFORMS'
            and getattr(constraint, "target", None) == target_obj
            and getattr(constraint, "subtarget", "") == subtarget
        ):
            return True
    return False


_BREAST_DANGLE_BONES = {"l_boob", "r_boob"}


def _dangle_constraint_drives_bone(candidate_obj, bone_name):
    constraint_type = str(candidate_obj.get("witcher_type", "") or "")
    if constraint_type == "CAnimDangleConstraint_Dyng":
        return str(bone_name or "").startswith("dyng_")
    if constraint_type == "CAnimDangleConstraint_Breast":
        return bone_name in _BREAST_DANGLE_BONES
    return False


def _is_dangle_constraint_armature(obj):
    return (
        obj is not None
        and getattr(obj, "type", None) == 'ARMATURE'
        and str(obj.get("witcher_type", "") or "").startswith("CAnimDangleConstraint_")
    )


def _is_dangle_buffer_armature(obj):
    return (
        obj is not None
        and getattr(obj, "type", None) == 'ARMATURE'
        and str(obj.get("witcher_type", "") or "") == "CAnimDangleBufferComponent"
    )


def _remove_copy_transform_to(pose_bone, target_obj, subtarget):
    removed = 0
    for constraint in list(pose_bone.constraints):
        if (
            constraint.type == 'COPY_TRANSFORMS'
            and getattr(constraint, "target", None) == target_obj
            and getattr(constraint, "subtarget", "") == subtarget
        ):
            pose_bone.constraints.remove(constraint)
            removed += 1
    return removed


def _build_dangle_driver_map(constrains, objdict):
    drivers_by_parent = {}
    for parent_obj_name, child_obj_name in constrains:
        parent_obj = objdict.get(parent_obj_name)
        child_obj = objdict.get(child_obj_name)
        parent_type = ""
        parent_is_merged = False
        if parent_obj is not None:
            try:
                parent_type = str(parent_obj.get("witcher_type", "") or "")
                parent_is_merged = bool(parent_obj.get("witcher_merged_character_armature", False))
            except Exception:
                pass
        if (
            parent_obj is None
            or child_obj is None
            or getattr(parent_obj, "type", None) != 'ARMATURE'
            or (parent_type != "CAnimDangleBufferComponent" and not parent_is_merged)
            or not _is_dangle_constraint_armature(child_obj)
        ):
            continue

        child_bones = getattr(getattr(child_obj, "pose", None), "bones", None)
        if not child_bones:
            continue

        bone_drivers = drivers_by_parent.setdefault(id(parent_obj), {})
        for pose_bone in child_bones:
            if _dangle_constraint_drives_bone(child_obj, pose_bone.name):
                bone_drivers[pose_bone.name] = child_obj
    return drivers_by_parent


def _retarget_buffer_child_dangle_bones(parent_obj, child_obj, bone_drivers):
    if (
        parent_obj is None
        or child_obj is None
        or getattr(parent_obj, "type", None) != 'ARMATURE'
        or getattr(child_obj, "type", None) != 'ARMATURE'
        or str(parent_obj.get("witcher_type", "")) != "CAnimDangleBufferComponent"
        or _is_dangle_constraint_armature(child_obj)
        or not bone_drivers
    ):
        return 0

    changed = 0
    child_bones = getattr(getattr(child_obj, "pose", None), "bones", None)
    if not child_bones:
        return 0

    for child_bone in child_bones:
        dangle_target = bone_drivers.get(child_bone.name)
        if dangle_target is None:
            continue
        dangle_bones = getattr(getattr(dangle_target, "pose", None), "bones", None)
        if dangle_bones is None or dangle_bones.get(child_bone.name) is None:
            continue

        changed += _remove_copy_transform_to(child_bone, parent_obj, child_bone.name)
        if not _has_copy_transform_to(child_bone, dangle_target, child_bone.name):
            copy_transform = child_bone.constraints.new('COPY_TRANSFORMS')
            copy_transform.name = f"W3_DANGLE_{child_bone.name}"
            copy_transform.target = dangle_target
            copy_transform.subtarget = child_bone.name
            changed += 1
    return changed


def _retarget_merged_dangle_bones(target_obj, bone_drivers):
    if (
        target_obj is None
        or getattr(target_obj, "type", None) != 'ARMATURE'
        or not bone_drivers
    ):
        return 0
    changed = 0
    seen = set()
    for dangle_target in bone_drivers.values():
        if dangle_target is None or id(dangle_target) in seen:
            continue
        seen.add(id(dangle_target))
        changed += armature_merge.copy_dangle_driver_constraints_to_armature(target_obj, dangle_target)
    return changed


def _is_merged_character_armature(obj):
    if obj is None or getattr(obj, "type", None) != 'ARMATURE':
        return False
    try:
        return bool(obj.get("witcher_merged_character_armature", False))
    except Exception:
        return False


def _merged_parent_armature(obj):
    parent = getattr(obj, "parent", None)
    if _is_merged_character_armature(parent):
        return parent
    for pose_bone in getattr(getattr(obj, "pose", None), "bones", []) or []:
        for constraint in getattr(pose_bone, "constraints", []) or []:
            target = getattr(constraint, "target", None)
            if _is_merged_character_armature(target):
                return target
    return None


def process_constraints(constrains, objdict, group_parent=None, entity_name=""):
    """
    Process and apply constraints between parent and child objects.

    Parameters:
        constrains (list): List of tuples [(parent_obj_name, child_obj_name), ...]
        objdict (dict): Dictionary mapping object names to Blender objects
        group_parent (str, optional): Optional parent object name for grouping
        entity_name (str, optional): Owning entity name; root pairs targeting it
            resolve via parent_transform when the entity has no skeleton object

    Returns:
        list: List of objects that are parented to the group_parent
    """
    return_objs = []
    dangle_drivers_by_parent = _build_dangle_driver_map(constrains, objdict)
    for parent_obj_name, child_obj_name in constrains:
        if parent_obj_name in objdict and child_obj_name in objdict:
            parent_obj = objdict[parent_obj_name]
            child_obj = objdict[child_obj_name]
            if not armature_merge.object_still_exists(parent_obj) or not armature_merge.object_still_exists(child_obj):
                log.debug("Skipping stale constraint pair %s -> %s", child_obj_name, parent_obj_name)
                continue
            if parent_obj == child_obj:
                _retarget_merged_dangle_bones(
                    parent_obj,
                    dangle_drivers_by_parent.get(id(parent_obj), {}),
                )
                continue
            if child_obj.get("witcher_import_error"):
                _set_parent_keep_world(child_obj, parent_obj)
                if group_parent and parent_obj_name == group_parent:
                    return_objs.append(child_obj)
                continue
            parent_is_merged = _is_merged_character_armature(parent_obj)
            child_is_merged = _is_merged_character_armature(child_obj)
            if parent_is_merged and _is_dangle_buffer_armature(child_obj):
                armature_merge.copy_dangle_anchor_constraints_to_armature(child_obj, parent_obj)
                _set_parent_keep_world(child_obj, parent_obj)
                continue
            if _is_dangle_buffer_armature(parent_obj) and child_is_merged:
                continue
            if parent_is_merged and _is_dangle_constraint_armature(child_obj):
                armature_merge.copy_dangle_anchor_constraints_to_driver(child_obj, parent_obj)
                _set_parent_keep_world(child_obj, parent_obj)
                continue
            if _is_dangle_constraint_armature(parent_obj) and child_is_merged:
                armature_merge.copy_dangle_anchor_constraints_to_driver(parent_obj, child_obj)
                armature_merge.copy_dangle_driver_constraints_to_armature(child_obj, parent_obj)
                continue
            # Bind with constraints during import; merging into one armature is a
            # single post-import pass (unify_character_armatures) so the importer's
            # later passes never trip over an armature that was deleted mid-flight.
            constrain_util.CreateConstraints2(parent_obj, child_obj)
            if _is_dangle_constraint_armature(parent_obj):
                armature_merge.copy_dangle_driver_constraints_to_armature(child_obj, parent_obj)
            if _is_dangle_buffer_armature(parent_obj) and _is_dangle_constraint_armature(child_obj):
                merged_parent = _merged_parent_armature(parent_obj)
                if merged_parent is not None:
                    armature_merge.copy_dangle_driver_constraints_to_armature(merged_parent, child_obj)
            _retarget_buffer_child_dangle_bones(
                parent_obj,
                child_obj,
                dangle_drivers_by_parent.get(id(parent_obj), {}),
            )

            # If the object is a Cloth group, attach the group to the appearance instead.
            if child_obj.parent and ":_grp" in child_obj.parent.name:
                _set_parent_keep_world(child_obj.parent, parent_obj)
                if group_parent and parent_obj_name == group_parent:
                    return_objs.append(child_obj.parent)
            else:
                _set_parent_keep_world(child_obj, parent_obj)
                if group_parent and parent_obj_name == group_parent:
                    return_objs.append(child_obj)
        elif parent_obj_name == entity_name and parent_obj_name not in objdict:
            log.debug('Root component %s binds via parent_transform (no root skeleton object)', child_obj_name)
        else:
            log.info(f'Failed to constrain {child_obj_name} to {parent_obj_name}')
    return return_objs


def process_hard_attachments(HardAttachments, objdict, meshdict):
    """
    Process hard attachments, setting up parenting and applying relative transformations.

    Parameters:
        HardAttachments (list): List of constraints with 'parent_name', 'parentSlotName', 'child_name', 'relativeTransform'
        objdict (dict): Dictionary mapping object names to Blender objects
        meshdict (dict): Dictionary mapping mesh names to Blender mesh objects
    """
    for constraint in HardAttachments:
        parent_arm_name = _get_entry_attr(constraint, 'parent_name', '')
        child_name = _get_entry_attr(constraint, 'child_name', '')

        special_names = ["CAnimated", "CCameraComponent", "CAnimDangleConstraint"]
        if any(substring in child_name for substring in special_names):
            process_special_attachment(constraint, objdict)
        else:
            process_regular_attachment(constraint, objdict, meshdict)


def _pose_bone_names(armature_obj):
    pose_bones = getattr(getattr(armature_obj, "pose", None), "bones", None)
    if not pose_bones:
        return []
    return [str(getattr(bone, "name", "") or "") for bone in pose_bones]


def _resolve_hard_attachment_parent_slot_name(constraint, parent_arm):
    slot_name = str(_get_entry_attr(constraint, "parentSlotName", "") or "").strip()
    if slot_name and slot_name.lower() not in {"none", "null"}:
        return slot_name
    bone_index = _get_entry_attr(constraint, "parentSlotBoneIndex", None)
    return bone_name_from_slot_index(_pose_bone_names(parent_arm), bone_index, "")


def _constraint_attachment_flags(constraint):
    return coerce_attachment_flags(_get_entry_attr(constraint, "attachmentFlags", 0))


_ATTACHMENT_RELATIVE_PROP = "witcher_hard_attachment_relative_transform"
_ATTACHMENT_FLAGS_WARNING_PROP = "witcher_attachment_flags_warning"
_WARNED_ATTACHMENT_FLAGS = set()


def _warn_unsupported_attachment_flags(flags, parent_name, child_name, target_obj=None):
    flags = coerce_attachment_flags(flags)
    if not flags:
        return
    message = (
        f"CHardAttachment {parent_name} -> {child_name} uses unsupported "
        f"attachmentFlags 0x{flags:02X}; imported as a normal full-transform attachment."
    )
    log.warning(message)
    if target_obj is not None:
        try:
            target_obj["witcher_attachment_flags"] = flags
            target_obj[_ATTACHMENT_FLAGS_WARNING_PROP] = message
        except Exception:
            pass

    warning_key = (str(parent_name), str(child_name), int(flags))
    if warning_key in _WARNED_ATTACHMENT_FLAGS:
        return
    _WARNED_ATTACHMENT_FLAGS.add(warning_key)
    if bool(getattr(bpy.app, "background", False)):
        return
    try:
        def draw_warning(menu, _context):
            menu.layout.label(text=f"Unsupported attachmentFlags: 0x{flags:02X}", icon='ERROR')
            menu.layout.label(text="Imported using normal full-transform attachment behavior.")
            menu.layout.label(text=f"See log/custom properties for {child_name}.")

        bpy.context.window_manager.popup_menu(
            draw_warning,
            title="CHardAttachment Warning",
            icon='ERROR',
        )
    except Exception:
        pass


def _configure_hard_attachment_head_constraint(anchor, parent_obj, bone_name):
    """Bind a parented anchor to a bone transform at the bone head.

    LOCAL/POSE with BEFORE_FULL evaluates the anchor locally as
    ``bone_pose @ relative_local``; normal object parenting then applies the
    armature world matrix exactly once.
    """
    constraint = anchor.constraints.new(type='COPY_TRANSFORMS')
    constraint.name = "W3_HARD_ATTACHMENT"
    constraint.target = parent_obj
    constraint.subtarget = str(bone_name or "")
    try:
        constraint.head_tail = 0.0
    except Exception:
        pass
    try:
        constraint.owner_space = 'LOCAL'
        constraint.target_space = 'POSE'
    except Exception:
        pass
    for mix_mode in ('BEFORE_FULL', 'BEFORE'):
        try:
            constraint.mix_mode = mix_mode
            break
        except Exception:
            continue
    return constraint


def _link_hard_attachment_anchor(parent_obj, child_obj, bone_name, relative_transform, attachment_flags):
    """Create an armature-parented anchor for a rigid mesh attachment."""
    anchor = bpy.data.objects.new("CHardAttachment", None)
    anchor.empty_display_type = 'PLAIN_AXES'
    anchor.empty_display_size = 0.3
    collection = next(iter(getattr(child_obj, "users_collection", []) or []), None)
    _link_object_to_collection(anchor, collection or _get_import_target_collection(bpy.context))
    anchor["witcher_type"] = "CHardAttachment"
    anchor["witcher_child_name"] = str(getattr(child_obj, "name", "") or "")
    anchor["witcher_parent_name"] = str(getattr(parent_obj, "name", "") or "")
    flags = coerce_attachment_flags(attachment_flags)
    anchor["witcher_attachment_flags"] = flags
    anchor["witcher_parent_slot_name"] = str(bone_name or "")
    anchor[_ATTACHMENT_RELATIVE_PROP] = json.dumps(
        normalize_engine_transform(relative_transform),
        separators=(",", ":"),
    )

    # Component transforms belong below CHardAttachment. Preserve the existing
    # local component matrix while replacing its transform parent with the anchor.
    component_matrix = child_obj.matrix_basis.copy()
    child_obj.parent = anchor
    child_obj.parent_type = 'OBJECT'
    child_obj.parent_bone = ""
    try:
        child_obj.matrix_parent_inverse = Matrix.Identity(4)
    except Exception:
        pass
    child_obj.matrix_basis = component_matrix

    has_bone = bool(
        bone_name
        and getattr(parent_obj, "type", None) == 'ARMATURE'
        and getattr(parent_obj, "pose", None)
        and parent_obj.pose.bones.get(bone_name) is not None
    )
    anchor.parent = parent_obj
    anchor.parent_type = 'OBJECT'
    anchor.parent_bone = ""
    try:
        anchor.matrix_parent_inverse = Matrix.Identity(4)
    except Exception:
        pass

    rt = _coerce_engine_transform(relative_transform) if relative_transform else None
    if rt is not None:
        set_blender_object_transform(anchor, rt, rotate_180=False)
    else:
        anchor.matrix_basis = Matrix.Identity(4)

    if has_bone:
        rig_settings = getattr(getattr(parent_obj, "data", None), "witcherui_RigSettings", None)
        if get_rig_rot90_enabled(rig_settings, default=False):
            anchor.matrix_basis = Matrix.Rotation(radians(90), 4, 'Z') @ anchor.matrix_basis

    if has_bone:
        _configure_hard_attachment_head_constraint(anchor, parent_obj, bone_name)

    _warn_unsupported_attachment_flags(
        flags,
        getattr(parent_obj, "name", "parent"),
        getattr(child_obj, "name", "child"),
        anchor,
    )
    return anchor


def process_special_attachment(constraint, objdict):
    """
    Process special attachments like animated components or cameras.

    Parameters:
        constraint (dict): Constraint information
        objdict (dict): Dictionary mapping object names to Blender objects
    """
    parent_arm_name = _get_entry_attr(constraint, 'parent_name', '')
    child_name = _get_entry_attr(constraint, 'child_name', '')
    relativeTransform = _get_entry_attr(constraint, 'relativeTransform', None)
    attachment_flags = _constraint_attachment_flags(constraint)

    if parent_arm_name in objdict and child_name in objdict:
        parent_arm = objdict[parent_arm_name]
        target_object = objdict[child_name]
        if parent_arm is target_object:
            return
        rig_settings = getattr(getattr(parent_arm, "data", None), "witcherui_RigSettings", None)
        try:
            default_rot90 = get_do_fix_tail(bpy.context)
        except Exception:
            default_rot90 = False
        use_rot90 = get_rig_rot90_enabled(rig_settings, default=default_rot90)

        p_bone_name = _resolve_hard_attachment_parent_slot_name(constraint, parent_arm)
        p_bone = None
        if p_bone_name and getattr(parent_arm, "pose", None):
            p_bone = parent_arm.pose.bones.get(p_bone_name)
        if p_bone_name and p_bone is None:
            log.warning(
                "CHardAttachment '%s' references missing slot bone '%s' on '%s'; binding to parent object.",
                child_name,
                p_bone_name,
                parent_arm_name,
            )

        can_match_full_armature = (
            target_object.type == 'ARMATURE'
            and constrain_util.should_auto_align_armatures(parent_arm, target_object)
        )

        if can_match_full_armature:
            _set_parent_keep_world(target_object, parent_arm)
            target_object["w2_special_attachment_mode"] = "matched_armature"
            target_object.parent_type = "OBJECT"
            target_object.parent_bone = ""
            constrain_util.CreateConstraints2(parent_arm, target_object)
        elif p_bone is not None:
            if target_object.type == 'ARMATURE':
                _set_parent_keep_world(target_object, parent_arm)
            else:
                target_object.parent = parent_arm
            target_object["w2_special_attachment_mode"] = "root_copy"
            if getattr(target_object, "pose", None):
                target_root = target_object.pose.bones[0]
                for existing in list(target_root.constraints):
                    if existing.type == 'COPY_TRANSFORMS' and existing.target == parent_arm:
                        target_root.constraints.remove(existing)
                copy_transform = target_root.constraints.new('COPY_TRANSFORMS')
                copy_transform.name = f"{p_bone.name} to {target_root.name}"
                copy_transform.target = parent_arm
                copy_transform.subtarget = p_bone.name
                target_object.parent_type = "OBJECT"
                target_object.parent_bone = ""
            else:
                target_object.parent_type = "BONE"
                target_object.parent_bone = p_bone.name
        else:
            _set_parent_keep_world(target_object, parent_arm)
            target_object["w2_special_attachment_mode"] = "object_fallback"

        target_object["w2_special_attachment"] = True
        target_object["w2_special_parent_arm"] = parent_arm.name
        target_object["w2_special_parent_bone"] = p_bone.name if p_bone else ""
        target_object["witcher_attachment_flags"] = attachment_flags
        target_object[_ATTACHMENT_RELATIVE_PROP] = json.dumps(
            normalize_engine_transform(relativeTransform),
            separators=(",", ":"),
        )

        _warn_unsupported_attachment_flags(
            attachment_flags,
            parent_arm_name,
            child_name,
            target_object,
        )

        if relativeTransform:
            rt = _coerce_engine_transform(relativeTransform)
            if rt is not None:
                set_blender_object_transform(target_object, rt, rotate_180=False)

        if "CCameraComponent" in child_name:
            create_camera_drivers(parent_arm, target_object, "hctFOV")
            if use_rot90:
                target_object.rotation_euler[2] += math.radians(90)
    else:
        log.error("Failed to create special CHardAttachment %s -> %s", child_name, parent_arm_name)


def process_regular_attachment(constraint, objdict, meshdict):
    """
    Process regular attachments by creating an empty object and setting up parenting.

    Parameters:
        constraint (dict): Constraint information
        objdict (dict): Dictionary mapping object names to Blender objects
        meshdict (dict): Dictionary mapping mesh names to Blender mesh objects
    """
    parent_arm_name = _get_entry_attr(constraint, 'parent_name', '')
    child_name = _get_entry_attr(constraint, 'child_name', '')
    relativeTransform = _get_entry_attr(constraint, 'relativeTransform', None)
    attachment_flags = _constraint_attachment_flags(constraint)

    target_name = f"{child_name}_lod0"
    target_mesh_obj = meshdict.get(target_name) or meshdict.get(child_name)
    if parent_arm_name in objdict and target_mesh_obj is not None:
        parent_arm = objdict[parent_arm_name]
        p_bone_name = _resolve_hard_attachment_parent_slot_name(constraint, parent_arm)
        p_bone = parent_arm.pose.bones.get(p_bone_name) if p_bone_name and getattr(parent_arm, "pose", None) else None
        if p_bone_name and p_bone is None:
            log.warning(
                "CHardAttachment '%s' references missing slot bone '%s' on '%s'; binding to parent object.",
                child_name,
                p_bone_name,
                parent_arm_name,
            )
            p_bone_name = ""
        _link_hard_attachment_anchor(
            parent_arm,
            target_mesh_obj,
            p_bone_name,
            relativeTransform,
            attachment_flags,
        )
    else:
        log.error("Failed to create CHardAttachment %s -> %s", child_name, parent_arm_name)

def join_as_shape_keys(source_meshes, target_meshes, morphComponentId):
    for source, target in zip(source_meshes, target_meshes):
        source_obj = bpy.data.objects[source.name]
        target_obj = bpy.data.objects[target.name]
        if source_obj.data.shape_keys is None:
            source_obj.shape_key_add(name='Basis')
        bpy.context.view_layer.objects.active = source_obj
        source_obj.select_set(True)
        target_obj.select_set(True)
        bpy.ops.object.join_shapes()
        target_obj.select_set(False)
        if source_obj.data.shape_keys:
            keys = source_obj.data.shape_keys.key_blocks
            last_key = keys[len(keys) - 1]
            last_key.name = morphComponentId

def import_chunks(entity, ent_namespace, cur_chunks, constrains, objdict, meshdict,
                 HardAttachments, hide_shadowmesh, root_skeleton, i,
                 selectedAppearance=None, import_redcloth_enabled=True, morphs_todo=None,
                 bind_root_chunks_to_entity=True, direct_entity_path="", source_game="",
                 source_entity_path="", target_collection=None, mesh_import_settings=None,
                 merged_armature_context=None, component_import_options=None):
    if morphs_todo is None:
        morphs_todo = []
    if target_collection is None:
        target_collection = _get_import_target_collection(bpy.context)
    mesh_import_settings = get_entity_mesh_import_settings(mesh_import_settings)
    selected_appearance_name = str(_get_entry_attr(selectedAppearance, "name", "") or "")
    coloring_entry_lookup = _build_coloring_entry_lookup(
        getattr(entity, "coloringEntries", None),
        selected_appearance_name,
    )
    direct_entity_path = _normalize_repo_path(direct_entity_path)
    source_entity_path = str(source_entity_path or "").strip()
    source_game = "w2" if str(source_game or "").strip().lower() == "w2" else ("w3" if source_game else "")
    resource_version = 115 if source_game == "w2" else _coerce_version(getattr(entity, "version", None), 999)
    
    def get_chunk_namespace(chunk):
        return f"{ent_namespace}{chunk['type']}{i}{chunk['chunkIndex']}"

    def _resolved_path_key(path):
        path = str(path or "").strip()
        if not path:
            return ""
        try:
            resolved = path if os.path.isabs(path) else repo_file(path, resource_version)
        except Exception:
            resolved = path
        try:
            return os.path.normcase(os.path.normpath(str(resolved or path)))
        except Exception:
            return str(resolved or path).replace("/", "\\").lower()

    def _resolve_required_chunk_resource(chunk, field_name, label):
        repo_path = str(chunk.get(field_name) or "").strip()
        if not repo_path:
            raise ValueError(
                f"Empty {field_name} path for {label} on "
                f"{chunk.get('type')} #{chunk.get('chunkIndex')}"
            )
        source_expected_path = ""
        if source_game == "w2" and source_entity_path and os.path.isabs(source_entity_path):
            try:
                resolved_from_source = resolve_w2_repo_file_from_source(
                    repo_path,
                    source_entity_path,
                    version=resource_version,
                )
            except Exception:
                resolved_from_source = ""
            if resolved_from_source and os.path.exists(win_safe_path(resolved_from_source)):
                return resolved_from_source
            source_root = w2_source_repo_root_if_configured(source_entity_path)
            if source_root:
                source_expected_path = os.path.join(source_root, repo_path.replace("/", "\\").lstrip("\\"))
        try:
            resolved_path = repo_file(repo_path, resource_version)
        except Exception as exc:
            raise FileNotFoundError(
                f"Failed to resolve {label} for {chunk.get('type')} "
                f"#{chunk.get('chunkIndex')}: {repo_path} (version={resource_version}, source_game={source_game or '<unset>'})"
            ) from exc
        if not resolved_path or not os.path.exists(win_safe_path(resolved_path)):
            detail = f"{repo_path} -> {resolved_path}"
            if source_expected_path and os.path.normcase(os.path.normpath(source_expected_path)) != os.path.normcase(os.path.normpath(str(resolved_path or ""))):
                detail = f"{detail} (source root candidate: {source_expected_path})"
            detail = f"{detail} (version={resource_version}, source_game={source_game or '<unset>'})"
            raise FileNotFoundError(
                f"Missing {label} for {chunk.get('type')} "
                f"#{chunk.get('chunkIndex')}: {detail}"
            )
        return resolved_path

    def _try_reuse_owner_moving_agent(chunk, chunk_ns):
        if chunk.get('type') != "CMovingPhysicalAgentComponent" or not chunk.get('skeleton'):
            return None
        chunk_skeleton = str(chunk.get('skeleton') or "").strip()
        chunk_skeleton_key = _resolved_path_key(chunk_skeleton)
        candidate_armatures = []
        for owner_armature in (objdict.get(entity.name), root_skeleton):
            if owner_armature is not None and owner_armature not in candidate_armatures:
                candidate_armatures.append(owner_armature)
        try:
            for obj in bpy.data.objects:
                if obj in candidate_armatures or getattr(obj, "type", "") != 'ARMATURE':
                    continue
                if not obj.get("_w3_entity_import_in_progress", False):
                    continue
                rig_settings = getattr(getattr(obj, "data", None), "witcherui_RigSettings", None)
                if str(getattr(rig_settings, "entity_name", "") or "").strip() != str(entity.name or "").strip():
                    continue
                candidate_armatures.append(obj)
        except Exception:
            pass
        for owner_armature in candidate_armatures:
            if owner_armature is None or getattr(owner_armature, "type", "") != 'ARMATURE':
                continue
            rig_settings = getattr(getattr(owner_armature, "data", None), "witcherui_RigSettings", None)
            owner_skeleton = str(getattr(rig_settings, "main_entity_skeleton", "") or "").strip() if rig_settings else ""
            if not owner_skeleton or _resolved_path_key(owner_skeleton) != chunk_skeleton_key:
                continue
            add_chunk_metadata(owner_armature, chunk, chunk_skeleton)
            objdict[chunk_ns] = owner_armature
            objdict.setdefault(entity.name, owner_armature)
            return owner_armature
        return None
    
    def get_ns_for_chunk(chunk_index, chunks):
        for chunk in chunks:
            if chunk['chunkIndex'] == chunk_index:
                if chunk['type'] == "CAnimDangleComponent":
                    return GetChunkNS(chunk['constraint'], chunks, i)
                return f"{chunk['type']}{i}{chunk_index}"
        return None

    def get_chunk_for_index(chunk_index, chunks):
        for chunk in chunks:
            if chunk['chunkIndex'] == chunk_index:
                return chunk
        return None
    
    def add_chunk_metadata(obj, chunk, path=None, component_name=None):
        """Add metadata as custom properties to the Blender object"""
        if hasattr(obj, 'bl_rna'):  # Verify it's a Blender object
            obj['witcher_type'] = chunk['type']
            resolved_component_name = str(component_name or "").strip()
            if resolved_component_name:
                obj['witcher_name'] = resolved_component_name
            elif 'name' in chunk and chunk['name']:
                obj['witcher_name'] = chunk['name']
            if path:
                obj['witcher_path'] = path
            if direct_entity_path:
                obj['witcher_entity_path'] = direct_entity_path
            if source_entity_path and os.path.isabs(source_entity_path):
                obj['witcher_source_entity_path'] = source_entity_path
            if source_game:
                obj['witcher_source_game'] = source_game
            component_transform = _coerce_engine_transform(chunk.get("transform"))
            if component_transform is not None:
                obj["witcher_component_transform"] = json.dumps(
                    normalize_engine_transform(component_transform),
                    separators=(",", ":"),
                )
            if chunk.get('type') == "CAnimDangleConstraint_Dyng":
                dyng_prop_map = {
                    'dampening': 'witcher_dyng_dampening',
                    'gravity': 'witcher_dyng_gravity',
                    'speed': 'witcher_dyng_speed',
                    'wind': 'witcher_dyng_wind',
                    'shake': 'witcher_dyng_shake',
                    'useOffsets': 'witcher_dyng_use_offsets',
                    'planeCollision': 'witcher_dyng_plane_collision',
                    'maxLinksIterations': 'witcher_dyng_link_iterations',
                }
                for chunk_key, prop_name in dyng_prop_map.items():
                    value = chunk.get(chunk_key, None)
                    if value is not None:
                        obj[prop_name] = value
                try:
                    from ..physics import dyng_blender

                    dyng_blender.configure_imported_dyng(obj, enabled=get_import_physics_enabled(bpy.context))
                except Exception:
                    log.debug("Failed to configure imported Dyng dangle: %s", getattr(obj, "name", obj), exc_info=True)
            if chunk.get('type') == "CAnimDangleConstraint_Breast":
                breast_prop_map = {
                    'preset': 'witcher_breast_preset',
                    'simTime': 'witcher_breast_sim_time',
                    'ellipse': 'witcher_breast_ellipse',
                    'velDamp': 'witcher_breast_vel_damp',
                    'bounceDamp': 'witcher_breast_bounce_damp',
                    'inAcc': 'witcher_breast_in_acc',
                    'inertiaScaler': 'witcher_breast_inertia_scaler',
                    'blackHole': 'witcher_breast_black_hole',
                    'velClamp': 'witcher_breast_vel_clamp',
                    'gravity': 'witcher_breast_gravity',
                    'movementBoneWeight': 'witcher_breast_movement_weight',
                    'rotationBoneWeight': 'witcher_breast_rotation_weight',
                    'startSimPointOffset': 'witcher_breast_start_offset',
                }
                for chunk_key, prop_name in breast_prop_map.items():
                    value = chunk.get(chunk_key, None)
                    if value is not None:
                        obj[prop_name] = value
                try:
                    from ..physics import breast_blender

                    breast_blender.configure_imported_breast(obj, enabled=get_import_physics_enabled(bpy.context))
                except Exception:
                    log.debug("Failed to configure imported Breast dangle: %s", getattr(obj, "name", obj), exc_info=True)

    def import_w2_mimic_support_chunk(chunk, chunk_ns):
        """Import W2 CHeadDefinifion mimic resources without touching W3 .w3fac state."""
        metadata = _w2_mimic_metadata_from_chunk(chunk)
        if not metadata:
            return root_skeleton

        component_name = (
            _get_chunk_component_name(chunk)
            or metadata.get("head_name")
            or "w2_mimic_head"
        )
        imported_armature = None
        imported_meshes = []
        imported_mesh_armatures = []
        deferred_skeleton_warning = ""

        skeleton_repo = str(metadata.get("skeleton", "") or "").strip()
        skeleton_embedded_source = str(metadata.get("skeleton_embedded_source", "") or "").strip()
        skeleton_embedded_chunk_index = metadata.get("skeleton_embedded_chunk_index", -1)
        if skeleton_repo or skeleton_embedded_source:
            try:
                skeleton_path = _resolve_required_chunk_resource(chunk, 'w2_mimic_skeleton', 'Witcher 2 mimic skeleton')
                imported_armature = import_rig.import_w3_rig(
                    skeleton_path,
                    f"{chunk_ns}_rig",
                )
                add_chunk_metadata(imported_armature, chunk, skeleton_repo, component_name=component_name)
                _store_w2_mimic_metadata(imported_armature, metadata, armature_name=imported_armature.name)
                objdict[f"{chunk_ns}:rig"] = imported_armature
            except Exception:
                embedded_skeleton = metadata.get("embedded_skeleton_data")
                if isinstance(embedded_skeleton, dict):
                    embedded_skeleton = w3_types.CSkeleton(
                        bones=embedded_skeleton.get("bones", []) or [],
                        tracks=embedded_skeleton.get("tracks", []) or [],
                    )
                if embedded_skeleton is None:
                    embedded_skeleton = _load_w2_embedded_skeleton_data(
                        skeleton_embedded_source,
                        skeleton_embedded_chunk_index,
                    )
                if embedded_skeleton is not None:
                    try:
                        imported_armature = import_rig.create_armature(
                            embedded_skeleton,
                            f"{chunk_ns}_rig",
                            fileName=skeleton_embedded_source,
                        )
                        _store_armature_bone_order_from_skeleton(imported_armature, embedded_skeleton)
                        add_chunk_metadata(
                            imported_armature,
                            chunk,
                            skeleton_repo or skeleton_embedded_source,
                            component_name=component_name,
                        )
                        _store_w2_mimic_metadata(imported_armature, metadata, armature_name=imported_armature.name)
                        objdict[f"{chunk_ns}:rig"] = imported_armature
                    except Exception:
                        log.warning(
                            "Failed to import embedded Witcher 2 mimic skeleton: %s #%s",
                            skeleton_embedded_source,
                            skeleton_embedded_chunk_index,
                            exc_info=True,
                        )
                else:
                    deferred_skeleton_warning = (
                        skeleton_repo
                        or f"{skeleton_embedded_source} #{skeleton_embedded_chunk_index}"
                    )
                    log.debug(
                        "Failed to import Witcher 2 mimic skeleton: %s",
                        deferred_skeleton_warning,
                        exc_info=True,
                    )

        mesh_repo = str(metadata.get("mesh", "") or chunk.get("mesh", "") or "").strip()
        if mesh_repo and _entity_chunk_mesh_enabled(chunk, component_import_options):
            embedded_source_path = str(_get_entry_attr(chunk, "_embedded_source_path", "") or "")
            embedded_cmesh_chunk_index = _get_entry_attr(
                chunk,
                "_embedded_cmesh_chunk_index",
                _get_entry_attr(chunk, "_embedded_mesh_chunk_index", None),
            )
            selected_cmesh_chunk_index = None
            try:
                if embedded_source_path and embedded_cmesh_chunk_index is not None:
                    if not os.path.exists(win_safe_path(embedded_source_path)):
                        raise FileNotFoundError(f"{mesh_repo} -> {embedded_source_path}")
                    resolved_mesh_path = embedded_source_path
                    selected_cmesh_chunk_index = int(embedded_cmesh_chunk_index)
                else:
                    resolved_mesh_path = _resolve_required_chunk_resource(chunk, 'mesh', 'mesh')
                    if not resolved_mesh_path or not os.path.exists(win_safe_path(resolved_mesh_path)):
                        raise FileNotFoundError(f"{mesh_repo} -> {resolved_mesh_path}")

                imported_meshes, imported_mesh_armatures = fbx_util.import_model(
                    resolved_mesh_path,
                    f"{chunk['type']}{i}{chunk['chunkIndex']}",
                    entity.name,
                    keep_lod_meshes=mesh_import_settings["keep_lod_meshes"],
                    keep_empty_lods=mesh_import_settings["keep_empty_lods"],
                    keep_proxy_meshes=mesh_import_settings["keep_proxy_meshes"],
                    hide_zero_weight_faces=mesh_import_settings["hide_zero_weight_faces"],
                    build_material_nodes=mesh_import_settings["build_material_nodes"],
                    embedded_cmesh_chunk_index=selected_cmesh_chunk_index,
                )
            except Exception:
                log.warning("Failed to import Witcher 2 mimic mesh: %s", mesh_repo, exc_info=True)

        armature_name = getattr(imported_armature, "name", "") if imported_armature is not None else ""
        mesh_object_name = imported_meshes[0].name if imported_meshes else ""

        for arm in imported_mesh_armatures:
            add_chunk_metadata(arm, chunk, mesh_repo, component_name=component_name)
            _store_w2_mimic_metadata(arm, metadata, armature_name=armature_name or arm.name, mesh_object_name=mesh_object_name)
            objdict[f"{chunk_ns}:mesh_armature:{arm.name}"] = arm

        for mesh in imported_meshes:
            add_chunk_metadata(mesh, chunk, mesh_repo, component_name=component_name)
            _store_w2_mimic_metadata(mesh, metadata, armature_name=armature_name, mesh_object_name=mesh.name)
            if mesh.name[-5:-1] == "_lod":
                meshdict[chunk_ns + mesh.name[-5:]] = mesh
            else:
                meshdict[chunk_ns] = mesh

        if imported_armature is not None:
            _apply_chunk_transform_to_import_roots(chunk, armatures=[imported_armature])
        if imported_meshes or imported_mesh_armatures:
            _apply_chunk_transform_to_import_roots(
                chunk,
                armatures=imported_mesh_armatures,
                meshes=imported_meshes,
            )
        if deferred_skeleton_warning and not imported_mesh_armatures:
            log.warning(
                "Failed to import Witcher 2 mimic skeleton and no mesh armature fallback was available: %s",
                deferred_skeleton_warning,
            )

        metadata_targets = []
        for target_obj in (
            imported_armature,
            objdict.get(entity.name),
            root_skeleton,
            *(imported_mesh_armatures or []),
        ):
            if target_obj is None or target_obj in metadata_targets:
                continue
            metadata_targets.append(target_obj)
        for target_obj in metadata_targets:
            _store_w2_mimic_metadata(
                target_obj,
                metadata,
                armature_name=armature_name or getattr(imported_armature, "name", ""),
                mesh_object_name=mesh_object_name,
            )

        parent_armature = root_skeleton if getattr(root_skeleton, "type", "") == 'ARMATURE' else objdict.get(entity.name)
        constrained = 0
        slot_attached = 0
        if getattr(parent_armature, "type", "") == 'ARMATURE':
            parent_slot_name = str(metadata.get("parent_slot_name", "") or "")
            if imported_armature is not None and getattr(imported_armature, "type", "") == 'ARMATURE':
                constrained += _apply_w2_mimic_bone_mapping_constraints(parent_armature, imported_armature, metadata)
            if constrained:
                for target_obj in metadata_targets:
                    if target_obj is not None:
                        target_obj["witcher_w2_mimic_parent_armature"] = parent_armature.name
                        target_obj["witcher_w2_mimic_mapping_constraint_count"] = constrained

            if imported_armature is not None and imported_armature is not parent_armature:
                _set_parent_keep_world(imported_armature, parent_armature)
                if not constrained:
                    slot_attached = _attach_w2_head_objects_to_parent_slot(
                        parent_armature,
                        [imported_armature],
                        parent_slot_name,
                    )
            attached_meshes = _attach_w2_mimic_meshes_to_mimic_rig(
                imported_armature,
                imported_mesh_armatures,
                imported_meshes,
            )
            if attached_meshes:
                for target_obj in metadata_targets:
                    if target_obj is not None:
                        target_obj["witcher_w2_mimic_mesh_attachment_count"] = attached_meshes

            if imported_armature is None:
                slot_attached = _attach_w2_head_objects_to_parent_slot(
                    parent_armature,
                    list(imported_mesh_armatures or []) + list(imported_meshes or []),
                    parent_slot_name,
                )
                if not slot_attached:
                    for target_obj in _get_import_root_objects(list(imported_mesh_armatures or []) + list(imported_meshes or [])):
                        if target_obj is not None and target_obj is not parent_armature:
                            _set_parent_keep_world(target_obj, parent_armature)
            if slot_attached:
                for target_obj in metadata_targets:
                    if target_obj is not None:
                        target_obj["witcher_w2_head_slot_attachment_count"] = slot_attached

        use_visible_mimic_head = bool(
            imported_armature is not None
            and constrained
            and (imported_meshes or imported_mesh_armatures)
        )
        hidden_fallback_mimics = 0
        if not use_visible_mimic_head:
            hidden_fallback_mimics = _hide_w2_mimic_fallback_objects(
                list(imported_mesh_armatures or []) + list(imported_meshes or [])
            )
            if imported_armature is not None and not constrained:
                hidden_fallback_mimics += _hide_w2_mimic_fallback_objects([imported_armature])

        hidden_base_heads = 0
        if use_visible_mimic_head:
            hidden_base_heads = _hide_w2_base_head_objects_for_mimic(
                metadata.get("head_name", ""),
                objdict,
                meshdict,
            )
        if hidden_base_heads:
            for target_obj in metadata_targets:
                if target_obj is not None:
                    target_obj["witcher_w2_base_heads_hidden_for_mimic"] = hidden_base_heads
        if hidden_fallback_mimics:
            for target_obj in metadata_targets:
                if target_obj is not None:
                    target_obj["witcher_w2_mimic_hidden_without_face_rig_count"] = hidden_fallback_mimics

        if imported_armature is not None:
            objdict[chunk_ns] = imported_armature
            return root_skeleton or imported_armature
        return root_skeleton

    has_moving_agent = False

    def _merge_chunk_skeleton_sources(target_armature=None):
        if not _merged_context_enabled(merged_armature_context):
            return 0
        merged_count = 0
        mergeable_chunks = [
            source_chunk for source_chunk in cur_chunks
            if not _merged_chunk_keeps_own_skeleton(source_chunk)
        ]

        for source_chunk in mergeable_chunks:
            if _get_entry_attr(source_chunk, "skeleton", None):
                try:
                    skeleton_path = _resolve_required_chunk_resource(source_chunk, 'skeleton', 'skeleton')
                    skeleton_data = _load_skeleton_data_with_repaired_rest(skeleton_path, include_mesh_rest=True)
                    merged_count += _merge_skeleton_data_into_context(
                        merged_armature_context,
                        skeleton_data,
                        target_armature=target_armature,
                    )
                except Exception:
                    log.debug(
                        "Skipping skeleton pre-merge for %s #%s",
                        _get_entry_attr(source_chunk, "type", ""),
                        _get_entry_attr(source_chunk, "chunkIndex", ""),
                        exc_info=True,
                    )

        for source_chunk in mergeable_chunks:
            if _get_entry_attr(source_chunk, "dyng", None):
                try:
                    dyng_path = _resolve_required_chunk_resource(source_chunk, 'dyng', 'dynamic rig')
                    dyng_data = _load_skeleton_data_with_repaired_rest(dyng_path)
                    merged_count += _merge_skeleton_data_into_context(
                        merged_armature_context,
                        dyng_data,
                        target_armature=target_armature,
                    )
                except Exception:
                    log.debug(
                        "Skipping dynamic-rig pre-merge for %s #%s",
                        _get_entry_attr(source_chunk, "type", ""),
                        _get_entry_attr(source_chunk, "chunkIndex", ""),
                        exc_info=True,
                    )

        for source_chunk in mergeable_chunks:
            if _get_entry_attr(source_chunk, "mimicFace", None):
                try:
                    mimic_face_path = _resolve_required_chunk_resource(source_chunk, 'mimicFace', 'mimic face')
                    face_data = import_rig.loadFaceFile(mimic_face_path)
                    if face_data is not None:
                        merged_count += _merge_skeleton_data_into_context(
                            merged_armature_context,
                            getattr(face_data, "mimicSkeleton", None),
                            target_armature=target_armature,
                        )
                        merged_armature_context["mimic_face_file"] = str(_get_entry_attr(source_chunk, "mimicFace", "") or "")
                        if target_armature is not None:
                            _apply_merged_mimic_metadata(target_armature, merged_armature_context["mimic_face_file"])
                except Exception:
                    log.debug(
                        "Skipping mimic pre-merge for %s #%s",
                        _get_entry_attr(source_chunk, "type", ""),
                        _get_entry_attr(source_chunk, "chunkIndex", ""),
                        exc_info=True,
                    )

        for source_chunk in mergeable_chunks:
            if _get_entry_attr(source_chunk, "mesh", None):
                try:
                    mesh_path = _resolve_required_chunk_resource(source_chunk, 'mesh', 'mesh')
                    from . import import_mesh as _import_mesh_module
                    mesh_skeleton = _import_mesh_module.read_mesh_skeleton_data(
                        mesh_path,
                        keep_proxy_meshes=mesh_import_settings["keep_proxy_meshes"],
                    )
                    merged_count += _merge_skeleton_data_into_context(
                        merged_armature_context,
                        mesh_skeleton,
                        target_armature=target_armature,
                    )
                except Exception:
                    log.debug(
                        "Skipping mesh-bone pre-merge for %s #%s",
                        _get_entry_attr(source_chunk, "type", ""),
                        _get_entry_attr(source_chunk, "chunkIndex", ""),
                        exc_info=True,
                    )
        return merged_count

    def _mesh_skeleton_sources_for_repair():
        sources = []
        for source_chunk in cur_chunks:
            if _merged_chunk_keeps_own_skeleton(source_chunk):
                continue
            if not _get_entry_attr(source_chunk, "mesh", None):
                continue
            try:
                mesh_path = _resolve_required_chunk_resource(source_chunk, 'mesh', 'mesh')
                from . import import_mesh as _import_mesh_module
                mesh_skeleton = _import_mesh_module.read_mesh_skeleton_data(
                    mesh_path,
                    keep_proxy_meshes=mesh_import_settings["keep_proxy_meshes"],
                )
                if mesh_skeleton is not None:
                    sources.append(mesh_skeleton)
            except Exception:
                log.debug(
                    "Skipping mesh-bone skeleton repair source for %s #%s",
                    _get_entry_attr(source_chunk, "type", ""),
                    _get_entry_attr(source_chunk, "chunkIndex", ""),
                    exc_info=True,
                )
        return sources

    def _load_skeleton_data_with_repaired_rest(skeleton_path, *, include_mesh_rest=False):
        skeleton_data = armature_merge.load_skeleton_data(skeleton_path)
        if skeleton_data is None:
            return None

        has_placeholder_rest = armature_merge.skeleton_has_placeholder_rest_bones(skeleton_data)
        if not has_placeholder_rest:
            return skeleton_data

        repair_sources = _mesh_skeleton_sources_for_repair() if include_mesh_rest else []
        if has_placeholder_rest and not repair_sources:
            return skeleton_data

        repaired_skeleton = armature_merge.clone_skeleton_data(skeleton_data)
        repaired_count = armature_merge.fill_placeholder_skeleton_transforms_from_sources(
            repaired_skeleton,
            repair_sources,
        )
        if repaired_count:
            log.info(
                "Repaired skeleton rest data for %s (%d transforms filled)",
                skeleton_path,
                repaired_count,
            )
            return repaired_skeleton
        return skeleton_data

    def _import_skeleton_armature_with_repaired_rest(skeleton_path, chunk_ns, *, include_mesh_rest=False):
        skeleton_data = _load_skeleton_data_with_repaired_rest(
            skeleton_path,
            include_mesh_rest=include_mesh_rest,
        )
        if skeleton_data is not None:
            return import_rig.create_armature_from_skeleton_data(
                skeleton_data,
                skeleton_path,
                chunk_ns,
            )
        return import_rig.import_w3_rig(
            skeleton_path,
            chunk_ns
        )

    def _ensure_merged_root_armature():
        nonlocal root_skeleton, has_moving_agent
        if not _merged_context_enabled(merged_armature_context):
            return root_skeleton

        existing_root = _merged_context_target(merged_armature_context)
        if existing_root is not None:
            root_skeleton = existing_root
            _merge_chunk_skeleton_sources(target_armature=existing_root)
            return root_skeleton

        moving_chunk = None
        for source_chunk in cur_chunks:
            if (
                _get_entry_attr(source_chunk, "type", "") == "CMovingPhysicalAgentComponent"
                and _get_entry_attr(source_chunk, "skeleton", None)
            ):
                moving_chunk = source_chunk
                break
        if moving_chunk is None:
            return root_skeleton

        chunk_ns = get_chunk_namespace(moving_chunk)
        try:
            skeleton_path = _resolve_required_chunk_resource(moving_chunk, 'skeleton', 'skeleton')
            base_skeleton = _load_skeleton_data_with_repaired_rest(
                skeleton_path,
                include_mesh_rest=True,
            )
            if base_skeleton is None:
                return root_skeleton
            merged_armature_context["skeleton"] = armature_merge.clone_skeleton_data(base_skeleton)
            merged_armature_context["root_skeleton_path"] = skeleton_path
            merged_armature_context["root_chunk_key"] = chunk_ns
            moving_agent = import_rig.create_armature(
                base_skeleton,
                chunk_ns,
                fileName=skeleton_path,
            )
            moving_agent["witcher_merged_character_armature"] = True
            moving_agent["witcher_merged_preimport"] = True
            add_chunk_metadata(moving_agent, moving_chunk, _get_entry_attr(moving_chunk, "skeleton", ""))
            objdict[chunk_ns] = moving_agent
            objdict.setdefault(entity.name, moving_agent)
            root_skeleton = moving_agent
            has_moving_agent = True
            merged_armature_context["root_armature"] = moving_agent
            _merge_chunk_skeleton_sources(target_armature=moving_agent)
            _store_armature_bone_order_from_skeleton(moving_agent, moving_agent.data)
            if merged_armature_context.get("mimic_face_file"):
                _apply_merged_mimic_metadata(moving_agent, merged_armature_context.get("mimic_face_file", ""))
            _apply_chunk_transform_to_import_roots(moving_chunk, armatures=[moving_agent])
            return moving_agent
        except Exception:
            log.warning(
                "Failed to create pre-merged character armature for '%s'; falling back to normal import.",
                getattr(entity, "name", ""),
                exc_info=True,
            )
            if merged_armature_context is not None:
                merged_armature_context["enabled"] = False
            return root_skeleton

    _ensure_merged_root_armature()

    # Handle base constraints first
    if bind_root_chunks_to_entity:
        for chunk in cur_chunks:
            chunk_ns = get_chunk_namespace(chunk)
            if not isChildNode(chunk['chunkIndex'], cur_chunks):
                if _is_w2_mimic_support_chunk(chunk):
                    continue
                if chunk['type'] in _ROOT_CONSTRAINT_SKIP_CHUNK_TYPES:
                    continue
                # CAnimatedComponent sub-skeletons must NOT be bone-name-matched to the parent entity via CreateConstraints2. Cause problems with crossbows etc.
                if chunk['type'] == 'CAnimatedComponent' and chunk.get('skeleton'):
                    continue
                constrains.append([entity.name, chunk_ns])

    for chunk in cur_chunks:
        chunk_ns = get_chunk_namespace(chunk)
        if target_collection is not None:
            _activate_target_collection(bpy.context, target_collection)

        # Handle attachments
        if chunk['type'] in ["CMeshSkinningAttachment", "CAnimatedAttachment"]:
            parent_ns = get_ns_for_chunk(chunk['parent'], cur_chunks)
            child_ns = get_ns_for_chunk(chunk['child'], cur_chunks)
            if parent_ns and child_ns:
                constrains.append([f"{ent_namespace}{parent_ns}", f"{ent_namespace}{child_ns}"])

        if _is_w2_mimic_support_chunk(chunk):
            root_skeleton = import_w2_mimic_support_chunk(chunk, chunk_ns)
            continue

        # Import meshes
        if "mesh" in chunk and _entity_chunk_mesh_enabled(chunk, component_import_options):
            mesh_path = chunk['mesh']
            embedded_source_path = str(_get_entry_attr(chunk, "_embedded_source_path", "") or "")
            embedded_cmesh_chunk_index = _get_entry_attr(
                chunk,
                "_embedded_cmesh_chunk_index",
                _get_entry_attr(chunk, "_embedded_mesh_chunk_index", None),
            )
            has_embedded_mesh = bool(embedded_source_path and embedded_cmesh_chunk_index is not None)
            if not is_valid_mesh_path(mesh_path) and not has_embedded_mesh:
                raise ValueError(
                    f"Invalid mesh path for chunk {chunk['type']} #{chunk['chunkIndex']}: {mesh_path}"
                )
            else:
                selected_cmesh_chunk_index = None
                if has_embedded_mesh:
                    if os.path.exists(win_safe_path(embedded_source_path)):
                        resolved_mesh_path = embedded_source_path
                        selected_cmesh_chunk_index = int(embedded_cmesh_chunk_index)
                    else:
                        raise FileNotFoundError(
                            f"Missing embedded mesh source for chunk {chunk.get('type')} "
                            f"#{chunk.get('chunkIndex')}: {mesh_path} -> {embedded_source_path}"
                        )
                else:
                    resolved_mesh_path = _resolve_required_chunk_resource(chunk, 'mesh', 'mesh')
                if selected_cmesh_chunk_index is not None:
                    log.debug(
                        "Importing embedded Witcher 2 CMesh chunk %s for %s from %s",
                        selected_cmesh_chunk_index,
                        mesh_path,
                        resolved_mesh_path,
                    )
                component_name = _get_chunk_component_name(chunk)
                merged_mesh_target = None
                if not _merged_chunk_keeps_own_skeleton(chunk):
                    merged_mesh_target = _merged_context_target(merged_armature_context)
                try:
                    meshes, armatures = fbx_util.import_model(
                        resolved_mesh_path,
                        f"{chunk['type']}{i}{chunk['chunkIndex']}",
                        entity.name,
                        keep_lod_meshes=mesh_import_settings["keep_lod_meshes"],
                        keep_empty_lods=mesh_import_settings["keep_empty_lods"],
                        keep_proxy_meshes=mesh_import_settings["keep_proxy_meshes"],
                        hide_zero_weight_faces=mesh_import_settings["hide_zero_weight_faces"],
                        build_material_nodes=mesh_import_settings["build_material_nodes"],
                        embedded_cmesh_chunk_index=selected_cmesh_chunk_index,
                        target_armature=merged_mesh_target,
                    )
                except Exception as mesh_err:
                    raise RuntimeError(
                        f"Failed to import mesh for chunk {chunk.get('type')} "
                        f"#{chunk.get('chunkIndex')} ({resolved_mesh_path}"
                        f"{f' [embedded CMesh chunk {selected_cmesh_chunk_index}]' if selected_cmesh_chunk_index is not None else ''}): "
                        f"{mesh_err}"
                    ) from mesh_err
                if component_name:
                    for mesh in meshes:
                        mesh['witcher_name'] = component_name
                if selected_appearance_name and component_name:
                    _apply_coloring_lookup_to_objects(meshes, coloring_entry_lookup)

                # Store objects directly while adding metadata
                for arm in armatures:
                    add_chunk_metadata(arm, chunk, mesh_path, component_name=component_name)
                    _store_w2_head_metadata(arm, chunk)
                    # Witcher 2 CRagdollMeshComponent (hair/cloth) ships its dangle
                    # bones flat in the mesh.  Rebuild the parent hierarchy (W2
                    # analog of the W3 .w3dyng) and stash the ragdoll physics
                    # metadata for later simulation.  The shared anchor bones
                    # (head/neck/...) are constrained to the main skeleton later
                    # by the standard CreateConstraints2 name-matching pass.
                    if chunk.get('type') == "CRagdollMeshComponent":
                        try:
                            from . import ragdoll_hierarchy
                            ragdoll_hierarchy.apply_ragdoll_hierarchy(
                                arm, anchor_bone=str(chunk.get('baseBodyName') or ""),
                            )
                            ragdoll_hierarchy.store_ragdoll_metadata(arm, chunk.get('ragdoll_meta'))
                        except Exception:
                            log.warning(
                                "Failed to apply Witcher 2 ragdoll hierarchy for %s #%s",
                                chunk.get('type'), chunk.get('chunkIndex'), exc_info=True,
                            )
                    objdict[chunk_ns] = arm
                if merged_mesh_target is not None and not armatures:
                    objdict[chunk_ns] = merged_mesh_target

                for mesh in meshes:
                    add_chunk_metadata(mesh, chunk, mesh_path, component_name=component_name)
                    _apply_drawable_shadow_flags(
                        mesh,
                        chunk.get("drawableFlags"),
                        chunk.get("type", ""),
                    )
                    _store_w2_head_metadata(mesh, chunk)
                    if mesh.name[-5:-1] == "_lod":
                        meshdict[chunk_ns + mesh.name[-5:]] = mesh
                    else:
                        meshdict[chunk_ns] = mesh

                    if hide_shadowmesh:
                        chunk_name = chunk.get('name', '')
                        if any(_is_shadowmesh_name(candidate) for candidate in (mesh.name, chunk_name, mesh_path)):
                            _force_shadowmesh_hidden(mesh)

                _apply_chunk_transform_to_import_roots(chunk, armatures=armatures, meshes=meshes)
                if _is_w2_head_base_chunk(chunk):
                    parent_armature = root_skeleton if getattr(root_skeleton, "type", "") == 'ARMATURE' else objdict.get(entity.name)
                    head_slot_attached = _attach_w2_head_objects_to_parent_slot(
                        parent_armature,
                        list(armatures or []) + list(meshes or []),
                        _get_entry_attr(chunk, "w2_head_parent_slot_name", ""),
                    )
                    if head_slot_attached:
                        for target_obj in list(armatures or []) + list(meshes or []):
                            if target_obj is not None:
                                target_obj["witcher_w2_head_slot_attachment_count"] = head_slot_attached

        if chunk.get('srt') and _entity_chunk_mesh_enabled(chunk, component_import_options):
            srt_key = str(chunk['srt']).replace("/", "\\").lower()
            chunk_transform = _coerce_engine_transform(chunk.get("transform"))
            transform_sig = (
                json.dumps(normalize_engine_transform(chunk_transform), separators=(",", ":"))
                if chunk_transform is not None else None
            )
            dedupe_key = (srt_key, transform_sig)
            if _foliage_dedupe_keys is not None and dedupe_key in _foliage_dedupe_keys:
                log.debug(
                    "Skipping duplicate foliage component %s #%s (%s)",
                    chunk.get('type'), chunk.get('chunkIndex'), chunk.get('srt'),
                )
                continue
            component_name = _get_chunk_component_name(chunk)
            foliage_obj = None
            try:
                foliage_obj = _import_srt_chunk_object(chunk['srt'], chunk_ns)
            except Exception:
                log.warning(
                    "Failed to import foliage SRT %s for %s #%s",
                    chunk.get('srt'), chunk.get('type'), chunk.get('chunkIndex'), exc_info=True,
                )
            if foliage_obj is None:
                log.warning(
                    "Skipping %s #%s: no mesh from SRT %s",
                    chunk.get('type'), chunk.get('chunkIndex'), chunk.get('srt'),
                )
            else:
                add_chunk_metadata(foliage_obj, chunk, chunk['srt'], component_name=component_name)
                if chunk.get('srt_entry'):
                    foliage_obj['witcher_foliage_entry'] = chunk['srt_entry']
                    foliage_obj['witcher_foliage_entries'] = json.dumps(chunk.get('srt_entries') or {})
                meshdict[chunk_ns] = foliage_obj
                _apply_chunk_transform_to_import_roots(chunk, meshes=[foliage_obj])
                if _foliage_dedupe_keys is not None:
                    _foliage_dedupe_keys.add(dedupe_key)

        # Handle cloth resources
        if "resource" in chunk and not import_redcloth_enabled:
            redcloth_resource = str(chunk.get("resource", "") or "")
            if _is_cloth_resource_path(redcloth_resource):
                # Only notify if the user wants redcloth import enabled and it was auto-disabled by missing addons.
                wants_redcloth = True
                try:
                    wants_redcloth = bool(get_do_import_redcloth(bpy.context))
                except Exception:
                    wants_redcloth = True

                if wants_redcloth:
                    apx_status = get_apx_addon_status(bpy.context)
                    try:
                        _legacy_exists, legacy_enabled = addon_utils.check("io_scene_apx")
                    except Exception:
                        legacy_enabled = False
                    if not apx_status["enabled"] and not bool(legacy_enabled):
                        set_external_import_dependency_alert(
                            "redcloth",
                            source_path=redcloth_resource,
                            status="apx_addon_disabled",
                            reason="io_mesh_apx (or legacy io_scene_apx) addon is not enabled.",
                        )
                        log.warning(
                            "Skipping redcloth import for %s: io_mesh_apx (or legacy io_scene_apx) addon is not enabled.",
                            redcloth_resource,
                        )

        if "resource" in chunk and _entity_chunk_cloth_enabled(
            chunk.get("resource", ""),
            component_import_options,
            import_redcloth_enabled,
        ):
            redcloth_resource = chunk["resource"]
            redcloth_mat_path = repo_file(redcloth_resource, resource_version)
            component_name = _get_chunk_component_name(chunk)
            owner_armature = objdict.get(entity.name)
            cloth_arma, cloth_grp, cloth_meshes = import_or_reuse_redcloth(
                owner_armature,
                redcloth_resource,
                redcloth_mat_path,
                import_name=f"{chunk['type']}{i}{chunk['chunkIndex']}",
                entity_name=entity.name,
                target_collection=target_collection,
            )
            if cloth_arma is not None:
                add_chunk_metadata(cloth_arma, chunk, chunk['resource'], component_name=component_name)
                objdict[chunk_ns] = cloth_arma
                if cloth_grp is not None:
                    objdict[chunk_ns + ":_grp"] = cloth_grp
                if not any(c[1] == chunk_ns for c in constrains):
                    constrains.append([entity.name, chunk_ns])

                if component_name:
                    for mesh in cloth_meshes:
                        mesh['witcher_name'] = component_name
                for mesh in cloth_meshes:
                    add_chunk_metadata(mesh, chunk, chunk['resource'], component_name=component_name)
                if selected_appearance_name and component_name:
                    _apply_coloring_lookup_to_objects(cloth_meshes, coloring_entry_lookup)

        # Handle morphs
        if (
            mesh_import_settings.get("import_morphs", True)
            and _component_import_option(component_import_options, "do_import_Mesh", True)
            and "morphComponentId" in chunk
        ):
            morph_source_path = repo_file(chunk['morphSource'], resource_version)
            morph_target_path = repo_file(chunk['morphTarget'], resource_version)
            if (
                not morph_source_path
                or not morph_target_path
                or not os.path.exists(win_safe_path(morph_source_path))
                or not os.path.exists(win_safe_path(morph_target_path))
            ):
                raise FileNotFoundError(
                    "Morph source/target mesh is missing for "
                    f"{chunk.get('morphComponentId')}: "
                    f"{chunk.get('morphSource')} -> {morph_source_path}, "
                    f"{chunk.get('morphTarget')} -> {morph_target_path}"
                )
            try:
                merged_morph_target = None
                if not _merged_chunk_keeps_own_skeleton(chunk):
                    merged_morph_target = _merged_context_target(merged_armature_context)
                morph_source_meshes, morph_source_arms = fbx_util.import_model(
                    morph_source_path,
                    f"{chunk['type']}{i}{chunk['chunkIndex']}",
                    entity.name,
                    keep_lod_meshes=mesh_import_settings["keep_lod_meshes"],
                    keep_empty_lods=mesh_import_settings["keep_empty_lods"],
                    keep_proxy_meshes=mesh_import_settings["keep_proxy_meshes"],
                    hide_zero_weight_faces=mesh_import_settings["hide_zero_weight_faces"],
                    build_material_nodes=mesh_import_settings["build_material_nodes"],
                    target_armature=merged_morph_target,
                )
                morph_target_meshes, morph_target_arms = fbx_util.import_model(
                    morph_target_path,
                    f"{chunk['type']}{i}{chunk['chunkIndex']}_morphTarget",
                    entity.name,
                    keep_lod_meshes=mesh_import_settings["keep_lod_meshes"],
                    keep_empty_lods=mesh_import_settings["keep_empty_lods"],
                    keep_proxy_meshes=mesh_import_settings["keep_proxy_meshes"],
                    hide_zero_weight_faces=mesh_import_settings["hide_zero_weight_faces"],
                    build_material_nodes=mesh_import_settings["build_material_nodes"],
                    target_armature=merged_morph_target,
                )
            except Exception as morph_err:
                raise RuntimeError(
                    f"Failed to import morph meshes for {chunk.get('type')} "
                    f"#{chunk.get('chunkIndex')} "
                    f"(source={morph_source_path}, target={morph_target_path}): {morph_err}"
                ) from morph_err
            
            morphs_todo.append([chunk['morphComponentId'], (morph_source_meshes, morph_source_arms)])
            join_as_shape_keys(morph_source_meshes, morph_target_meshes, chunk['morphComponentId'])
            
            for obj in morph_target_meshes + morph_target_arms:
                bpy.data.objects.remove(bpy.data.objects[obj.name], do_unlink=True)
                
            for arm in morph_source_arms:
                add_chunk_metadata(arm, chunk, chunk['morphSource'])
                objdict[chunk_ns] = arm
            for mesh in morph_source_meshes:
                add_chunk_metadata(mesh, chunk, chunk['morphSource'])
                if mesh.name[-5:-1] == "_lod":
                    meshdict[chunk_ns + mesh.name[-5:]] = mesh
                else:
                    meshdict[chunk_ns] = mesh

        # Handle skeletons
        if chunk['type'] == "CMovingPhysicalAgentComponent":
            if 'skeleton' in chunk:
                merged_target = _merged_context_target(merged_armature_context)
                if merged_target is not None:
                    objdict[chunk_ns] = merged_target
                    objdict.setdefault(entity.name, merged_target)
                    root_skeleton = merged_target
                    has_moving_agent = True
                    continue
                reused_agent = _try_reuse_owner_moving_agent(chunk, chunk_ns)
                if reused_agent is not None:
                    root_skeleton = reused_agent
                    has_moving_agent = True
                    continue
                skeleton_path = _resolve_required_chunk_resource(chunk, 'skeleton', 'skeleton')
                moving_agent = _import_skeleton_armature_with_repaired_rest(
                    skeleton_path,
                    chunk_ns,
                    include_mesh_rest=True,
                )
                add_chunk_metadata(moving_agent, chunk, chunk['skeleton'])
                objdict[chunk_ns] = moving_agent
                objdict.setdefault(entity.name, moving_agent)
                root_skeleton = moving_agent
                has_moving_agent = True
                _apply_chunk_transform_to_import_roots(chunk, armatures=[moving_agent])
        elif "skeleton" in chunk and chunk['skeleton'] is not None:
            merged_target = None if _merged_chunk_imports_own_skeleton(chunk) else _merged_context_target(merged_armature_context)
            if merged_target is not None:
                objdict[chunk_ns] = merged_target
                if not has_moving_agent:
                    root_skeleton = merged_target
                continue
            try:
                skeleton_path = _resolve_required_chunk_resource(chunk, 'skeleton', 'skeleton')
            except FileNotFoundError as exc:
                if source_game == "w2" and str(chunk.get("type", "") or "") != "CMovingPhysicalAgentComponent":
                    log.warning("Skipping optional Witcher 2 skeleton component: %s", exc)
                    continue
                raise
            root_bone = _import_skeleton_armature_with_repaired_rest(
                skeleton_path,
                chunk_ns,
                include_mesh_rest=True,
            )
            add_chunk_metadata(root_bone, chunk, chunk['skeleton'])
            objdict[chunk_ns] = root_bone
            if not has_moving_agent:
                root_skeleton = root_bone
            _apply_chunk_transform_to_import_roots(chunk, armatures=[root_bone])

        # Handle dynamic rigs
        if "dyng" in chunk and chunk['dyng'] is not None:
            merged_target = None if _merged_chunk_imports_own_skeleton(chunk) else _merged_context_target(merged_armature_context)
            if merged_target is not None:
                objdict[chunk_ns] = merged_target
                continue
            dyng_path = _resolve_required_chunk_resource(chunk, 'dyng', 'dynamic rig')
            root_bone = _import_skeleton_armature_with_repaired_rest(
                dyng_path,
                chunk_ns,
            )
            add_chunk_metadata(root_bone, chunk, chunk['dyng'])
            objdict[chunk_ns] = root_bone
            _apply_chunk_transform_to_import_roots(chunk, armatures=[root_bone])

        # Handle mimic face
        if "mimicFace" in chunk:
            mimic_face_path = _resolve_required_chunk_resource(chunk, 'mimicFace', 'mimic face')
            faceData = import_rig.loadFaceFile(mimic_face_path)
            if faceData is None:
                log.warning("Failed to load mimic face: %s", chunk['mimicFace'])
            else:
                merged_target = None if _merged_chunk_imports_own_skeleton(chunk) else _merged_context_target(merged_armature_context)
                if merged_target is not None:
                    _merge_skeleton_data_into_context(
                        merged_armature_context,
                        getattr(faceData, "mimicSkeleton", None),
                        target_armature=merged_target,
                    )
                    _apply_merged_mimic_metadata(merged_target, chunk['mimicFace'])
                    objdict[chunk_ns] = merged_target
                    continue
                try:
                    bpy.ops.object.mode_set(mode='OBJECT', toggle=False)
                except Exception:
                    pass
                try:
                    bpy.ops.object.select_all(action='DESELECT')
                except Exception:
                    pass
                root_bone = import_rig.create_armature(faceData.mimicSkeleton, chunk_ns)
                mimic_rig_bl = root_bone
                mimic_rig_bl['mimicFace'] = root_bone.name
                mimic_rig_bl['mimicFaceFile'] = chunk['mimicFace']
                add_chunk_metadata(root_bone, chunk, chunk['mimicFace'])
                objdict.update({chunk_ns: root_bone})
                if not root_skeleton:
                    root_skeleton = root_bone
                metadata_targets = []
                for target_obj in (root_bone, objdict.get(entity.name), root_skeleton):
                    if target_obj is None or getattr(target_obj, "type", "") != 'ARMATURE':
                        continue
                    if target_obj in metadata_targets:
                        continue
                    metadata_targets.append(target_obj)
                for target_obj in metadata_targets:
                    target_obj['mimicFace'] = root_bone.name
                    target_obj['mimicFaceFile'] = chunk['mimicFace']
                _apply_chunk_transform_to_import_roots(chunk, armatures=[root_bone])

        # Handle camera
        if chunk['type'] == "CCameraComponent":
            camera_data = bpy.data.cameras.new(name='Camera')
            camera_object = bpy.data.objects.new('Camera', camera_data)
            _link_object_to_collection(camera_object, target_collection)
            camera_object.rotation_euler[0] = np.pi/2
            add_chunk_metadata(camera_object, chunk)
            objdict[chunk_ns] = camera_object

        if chunk['type'] in {"CPointLightComponent", "CSpotLightComponent"} and _component_import_option(
            component_import_options,
            "do_import_SpotLight" if chunk['type'] == "CSpotLightComponent" else "do_import_PointLight",
            True,
        ):
            light_obj = _import_light_component(chunk)
            if light_obj is not None:
                add_chunk_metadata(light_obj, chunk)
                meshdict[chunk_ns] = light_obj

        # Handle hard attachments
        if chunk['type'] == "CHardAttachment":
            parent_ns = get_ns_for_chunk(chunk['parent'], cur_chunks)
            child_ns = get_ns_for_chunk(chunk['child'], cur_chunks)
            if parent_ns and child_ns:
                chunk['parent_name'] = f"{ent_namespace}{parent_ns}"
                chunk['child_name'] = f"{ent_namespace}{child_ns}"
                slot_chunk = get_chunk_for_index(chunk.get('parentSlot'), cur_chunks)
                if slot_chunk is not None and slot_chunk.get('type') == "CSkeletonBoneSlot":
                    chunk['parentSlotBoneIndex'] = slot_chunk.get('boneIndex')
                HardAttachments.append(chunk)

    return constrains, objdict, meshdict, HardAttachments, root_skeleton, morphs_todo

from mathutils import Euler, Matrix
def _drawable_flags_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple, set)):
        return "|".join(str(item).strip() for item in value if str(item).strip())
    return str(read_enum_prop(value) or prop_to_string(value, default="") or "").strip()


def _apply_drawable_shadow_flags(obj, value, component_type=""):
    flags = _drawable_flags_text(value)
    default = str(component_type or "") in {
        "CDestructionComponent",
        "CDestructionSystemComponent",
    }
    raw_value = getattr(value, "Value", value)
    if isinstance(raw_value, (int, float)) and not isinstance(raw_value, bool):
        raw_flags = int(raw_value)
        local_only = bool(raw_flags & 1024)
        casts_shadows = bool(raw_flags & (2 | 1024))
    elif value is None:
        local_only = False
        casts_shadows = default
    else:
        flag_names = {
            part for part in re.split(r"[\s,|;]+", flags) if part
        }
        local_only = "DF_CastShadowsFromLocalLightsOnly" in flag_names
        casts_shadows = "DF_CastShadows" in flag_names or local_only
    obj.visible_shadow = casts_shadows
    obj["witcher_redkit_drawableFlags"] = flags or "Unset"
    obj["witcher_drawableFlags_has_DF_CastShadows"] = casts_shadows
    obj["witcher_drawableFlags_local_lights_only"] = local_only


def _coerce_real(value, default=0.0):
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    try:
        text = str(value).strip()
        if not text:
            return default
        return float(text)
    except Exception:
        return default


def _import_light_component(chunk):
    chunk_type = str(chunk.get("type", "") or "").strip()
    if chunk_type not in {"CPointLightComponent", "CSpotLightComponent"}:
        return None

    bpy.ops.object.light_add(type='POINT', radius=1, align='WORLD', location=(0, 0, 0), scale=(1, 1, 1))
    light_obj = bpy.context.selected_objects[:][0]
    light_name = str(chunk.get("name", "") or "").strip()
    if light_name:
        light_obj.name = light_name
        light_obj.data.name = light_name

    configure_entity_light(light_obj, chunk, chunk_type, scene=bpy.context.scene)

    rt = _coerce_engine_transform(chunk.get("transform"))
    if rt is not None:
        set_blender_object_transform(light_obj, rt, rotate_180=False)

    if chunk_type == "CSpotLightComponent":
        orient_red_spot(light_obj)

    return light_obj


def _set_constraint_space(constraint, *, owner_space=None, target_space=None):
    if owner_space:
        try:
            constraint.owner_space = owner_space
        except Exception:
            pass
    if target_space:
        try:
            constraint.target_space = target_space
        except Exception:
            pass


def _set_constraint_mix_mode(constraint, *modes):
    for mode in modes:
        try:
            constraint.mix_mode = mode
            return mode
        except Exception:
            continue
    return ""


def _set_constraint_head_tail(constraint, value=0.0):
    try:
        constraint.head_tail = float(value)
    except Exception:
        pass


def _configure_slot_copy_transforms_constraint(empty_obj, armature_obj, bone_name, has_bone, constraint_name):
    constraint = empty_obj.constraints.new(type='COPY_TRANSFORMS')
    constraint.name = constraint_name
    constraint.target = armature_obj
    constraint.subtarget = bone_name if has_bone else ''
    _set_constraint_head_tail(constraint, 0.0)
    if has_bone:
        _set_constraint_space(constraint, owner_space='LOCAL', target_space='POSE')
    else:
        _set_constraint_space(constraint, owner_space='LOCAL', target_space='LOCAL')
    _set_constraint_mix_mode(constraint, 'BEFORE', 'BEFORE_FULL')
    return [constraint]


def set_empty_bone_offset(
        empty_obj,
        armature_obj,
        bone_name,
        transform,
        rotate_180=False,
        rotate_90=False,
        rotate_90_dir=1,
        attachment_flags=0,
        constraint_name="W2_SLOT"):
    """Apply an EngineTransform offset and constrain an empty to a bone or armature.

    Rot90 compensation preserves world placement in the rotated bone basis.
    """
    flags = coerce_attachment_flags(attachment_flags)
    if flags:
        _warn_unsupported_attachment_flags(
            flags,
            getattr(armature_obj, "name", "parent"),
            getattr(empty_obj, "name", "attachment"),
            empty_obj,
        )

    has_bone = bone_name and bone_name in armature_obj.pose.bones

    # Remove existing slot constraints to avoid duplicates
    for c in list(empty_obj.constraints):
        if c.type in {'COPY_TRANSFORMS', 'CHILD_OF', 'COPY_LOCATION', 'COPY_ROTATION', 'COPY_SCALE'}:
            empty_obj.constraints.remove(c)

    _configure_slot_copy_transforms_constraint(empty_obj, armature_obj, bone_name, has_bone, constraint_name)

    try:
        empty_obj["witcher_attachment_flags"] = flags
    except Exception:
        pass
    
    if transform is not None:
        x = radians(_coerce_real(transform.get('Yaw', 0.0), 0.0))
        y = radians(_coerce_real(transform.get('Pitch', 0.0), 0.0))
        z = radians(_coerce_real(transform.get('Roll', 0.0), 0.0))
        rotation_matrix = Euler((x, y, z), 'YXZ').to_matrix().to_4x4()

        if rotate_180:
            rotation_matrix[0][0], rotation_matrix[0][1], rotation_matrix[0][2] = -rotation_matrix[0][0], -rotation_matrix[0][1], rotation_matrix[0][2]
            rotation_matrix[1][0], rotation_matrix[1][1], rotation_matrix[1][2] = -rotation_matrix[1][0], -rotation_matrix[1][1], rotation_matrix[1][2]
            rotation_matrix[2][0], rotation_matrix[2][1], rotation_matrix[2][2] = -rotation_matrix[2][0], -rotation_matrix[2][1], rotation_matrix[2][2]

        location = Matrix.Translation((
            _coerce_real(transform.get('X', 0.0), 0.0),
            _coerce_real(transform.get('Y', 0.0), 0.0),
            _coerce_real(transform.get('Z', 0.0), 0.0),
        ))

        scale_x = _coerce_real(transform.get('Scale_x', 1.0), 1.0)
        scale_y = _coerce_real(transform.get('Scale_y', 1.0), 1.0)
        scale_z = _coerce_real(transform.get('Scale_z', 1.0), 1.0)
        scale_matrix = Matrix.Scale(scale_x, 4, (1, 0, 0)) @ \
                       Matrix.Scale(scale_y, 4, (0, 1, 0)) @ \
                       Matrix.Scale(scale_z, 4, (0, 0, 1))

        transform_matrix = location @ rotation_matrix @ scale_matrix

        # Convert the authored slot transform into the rotated bone basis.
        # Applying the correction on the left preserves world placement for
        # translated slots when Rot90 changes the bone's local axes.
        if rotate_90:
            rot90 = Matrix.Rotation(radians(90 * rotate_90_dir), 4, 'Z')
            transform_matrix = rot90 @ transform_matrix

        empty_obj.matrix_local = transform_matrix
    else:
        if rotate_90:
            empty_obj.matrix_local = Matrix.Rotation(radians(90 * rotate_90_dir), 4, 'Z')
        else:
            empty_obj.matrix_local = Matrix.Identity(4)



def import_MovingPhysicalAgentComponent(entity, parent_transform = None, direct_entity_path="", source_game="", source_entity_path="",
                                        target_collection=None, mesh_import_settings=None,
                                        merged_armature_context=None, component_import_options=None,
                                        existing_root_skeleton=None, append_entity_slots=False):
    #entity = fixed_chunk_paths(entity, entity.version)
    ent_namespace = entity.name+":"
    if target_collection is None:
        target_collection = _get_import_target_collection(bpy.context)
    _activate_target_collection(bpy.context, target_collection)
    mesh_import_settings = get_entity_mesh_import_settings(mesh_import_settings)
    import_redcloth_enabled = bool(get_do_import_redcloth(bpy.context))

    #OPTIONS
    hide_shadowmesh = True
    mimic_namespace = False
    root_skeleton = existing_root_skeleton or False
    faceData = False

    #CONTRAINT ARRAYS
    constrains = []
    morphs_todo = []
    HardAttachments = []

    #DICTS
    objdict = {}
    meshdict = {}
    
    
    if entity.staticMeshes is not None:
        cur_chunks = entity.staticMeshes.get('chunks', [])
        (constrains, objdict, meshdict, HardAttachments, root_skeleton, morphs_todo) = import_chunks(
            entity,
            ent_namespace,
            cur_chunks,
            constrains,
            objdict,
            meshdict,
            HardAttachments,
            hide_shadowmesh,
            root_skeleton,
            i='',
            import_redcloth_enabled=import_redcloth_enabled,
            direct_entity_path=direct_entity_path,
            source_game=source_game,
            source_entity_path=source_entity_path,
            target_collection=target_collection,
            mesh_import_settings=mesh_import_settings,
            merged_armature_context=merged_armature_context,
            component_import_options=component_import_options,
        )

    if not root_skeleton:
        candidate_armatures = [
            obj for obj in objdict.values()
            if obj is not None and getattr(obj, "type", "") == 'ARMATURE'
        ]
        if candidate_armatures:
            root_skeleton = min(candidate_armatures, key=_object_parent_depth)

    if root_skeleton is not None and getattr(root_skeleton, "type", "") == 'ARMATURE':
        if not str(root_skeleton.get("mimicFaceFile", "") or "").strip():
            for obj in objdict.values():
                if obj is None or getattr(obj, "type", "") != 'ARMATURE':
                    continue
                mimic_face_file = str(obj.get("mimicFaceFile", "") or "").strip()
                if not mimic_face_file:
                    continue
                root_skeleton["mimicFaceFile"] = mimic_face_file
                root_skeleton["mimicFace"] = str(obj.get("mimicFace", "") or obj.name)
                break
    
    # Process and import EntitySlots from the entity
    if entity.slots and root_skeleton and root_skeleton.type == 'ARMATURE':
        import json
        rig_settings = root_skeleton.data.witcherui_RigSettings
        if not append_entity_slots:
            rig_settings.entity_slots.clear()
        existing_slot_keys = {
            (
                str(getattr(slot, "slot_name", "") or "").strip().lower(),
                str(getattr(slot, "component_name", "") or "").strip().lower(),
                str(getattr(slot, "bone_name", "") or "").strip().lower(),
            )
            for slot in rig_settings.entity_slots
        }
        
        slots_parent = None

        # Process each slot
        for slot in entity.slots:
            this_slot = slot if isinstance(slot, w3_types.EntitySlot) else w3_types.EntitySlot(True, slot)
            componentName = this_slot.componentName
            slot_key = (
                str(this_slot.name or "").strip().lower(),
                str(componentName or "").strip().lower(),
                str(this_slot.boneName or "").strip().lower(),
            )
            if slot_key in existing_slot_keys:
                continue
            existing_slot_keys.add(slot_key)
            if slots_parent is None:
                slots_parent_name = f"{entity.name}_slots" if entity.name else "entity_slots"
                slots_parent = bpy.data.objects.new(slots_parent_name, None)
                _link_object_to_collection(slots_parent, target_collection)
                slots_parent.empty_display_type = 'PLAIN_AXES'
                slots_parent.empty_display_size = 0.1
                slots_parent["witcher_slots_parent"] = True
                slots_parent["witcher_entity_name"] = entity.name or ""
                slots_parent["witcher_owner_armature"] = getattr(
                    root_skeleton, "name_full", root_skeleton.name
                )
                slots_parent.parent = root_skeleton
                slots_parent.hide_set(True)

            # Store slot data in rig_settings for persistence
            slot_entry = rig_settings.entity_slots.add()
            slot_entry.slot_name = this_slot.name or ""
            slot_entry.component_name = componentName or ""
            slot_entry.bone_name = this_slot.boneName or ""
            slot_entry.transform_json = _to_json_text(this_slot.transform)
            slot_entry.free_position_x = this_slot.freePositionAxisX or False
            slot_entry.free_position_y = this_slot.freePositionAxisY or False
            slot_entry.free_position_z = this_slot.freePositionAxisZ or False
            slot_entry.free_rotation = this_slot.freeRotation or False

            # Find the armature object for this component
            name = entity.name + ':' + this_slot.name
            transform = this_slot.transform
            bone_name = this_slot.boneName

            def get_root_bone_name(arm_obj):
                if not arm_obj or arm_obj.type != 'ARMATURE':
                    return None
                for b in arm_obj.data.bones:
                    if b.parent is None:
                        return b.name
                return None

            armature_obj = None
            if componentName:
                # Prefer matching by component "witcher_name" metadata
                for obj in objdict.values():
                    if obj and obj.type == 'ARMATURE' and obj.get('witcher_name') == componentName:
                        armature_obj = obj
                        break
                if armature_obj is None:
                    # Fallback to name matches
                    for obj in objdict.values():
                        if obj and obj.type == 'ARMATURE' and (obj.name == componentName or obj.name == f"{entity.name}:{componentName}"):
                            armature_obj = obj
                            break

            # Fallback to entity itself if no component specified
            if armature_obj is None and not componentName:
                if entity.name in objdict and objdict[entity.name].type == 'ARMATURE':
                    armature_obj = objdict[entity.name]
            
            # Use root_skeleton as fallback
            if armature_obj is None:
                armature_obj = root_skeleton

            # If no bone is specified, bind to the root bone of the main armature
            if not bone_name:
                main_arm = root_skeleton if root_skeleton and root_skeleton.type == 'ARMATURE' else armature_obj
                root_bone = get_root_bone_name(main_arm)
                if root_bone:
                    armature_obj = main_arm
                    bone_name = root_bone

            # Create an empty object for this slot
            empty_obj = bpy.data.objects.new(name, None)
            _link_object_to_collection(empty_obj, target_collection)
            empty_obj.empty_display_type = 'SPHERE'
            empty_obj.empty_display_size = 0.02
            empty_obj["witcher_slot_name"] = this_slot.name or ""
            empty_obj["witcher_entity_name"] = entity.name or ""
            empty_obj["witcher_owner_armature"] = getattr(root_skeleton, "name_full", root_skeleton.name)

            # Parent the empty under the slots parent object
            empty_obj.parent = slots_parent

            # Set the empty's position and constrain it with offset
            use_rot90 = False
            rot90_dir = 1
            if root_skeleton and root_skeleton.type == 'ARMATURE':
                rig_settings = root_skeleton.data.witcherui_RigSettings
                use_rot90 = get_rig_rot90_enabled(rig_settings, default=False)
                rot90_dir = 1
            set_empty_bone_offset(empty_obj, armature_obj, bone_name, transform,
                                  rotate_90=use_rot90, rotate_90_dir=rot90_dir)

            # Hide by default
            empty_obj.hide_set(True)

    #objdict.update({entity.name:root_skeleton}) # TODO this shouldn't be required if it reads the entity constraints full

    do_constraints(constrains, objdict, meshdict, HardAttachments, entity_name=str(getattr(entity, "name", "") or ""))

    if source_game == "w2" and root_skeleton is not None and getattr(root_skeleton, "type", "") == 'ARMATURE':
        imported_roots = _get_import_root_objects(
            [
                obj
                for obj in list(objdict.values()) + list(meshdict.values())
                if obj is not None and obj is not root_skeleton
            ]
        )
        for obj in imported_roots:
            if obj is not None and obj.parent is None:
                _set_parent_keep_world(obj, root_skeleton)
                obj["witcher_w2_parented_to_main_entity"] = True

    if parent_transform:
        if root_skeleton:
            root_skeleton.parent = parent_transform
        for mesh in list(objdict.values()) + list(meshdict.values()):
            if mesh and getattr(mesh, "parent", None) is None:
                mesh.parent = parent_transform
    effect_owner = root_skeleton or parent_transform
    if effect_owner is not None:
        try:
            entity_effects.import_entity_effect_previews(
                entity,
                effect_owner,
                imported_objects=list(objdict.values()) + list(meshdict.values()),
                target_collection=target_collection,
            )
        except Exception:
            log.warning(
                "Failed to create cooked effect previews for '%s'.",
                getattr(entity, "name", "<entity>"),
                exc_info=True,
            )
    return root_skeleton

def reset_transforms(new_obj):
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


def add_app_template(   entity,
                                base_animation_skeleton,
                                group_parent,
                                ent_namespace,
                                import_redcloth_enabled,
                                i,
                                selectedAppearance,
                                hide_shadowmesh,
                                empty_transform,
                                root_skeleton,
                                templateFilename,
                                template_data=None,
                                appearance_indices=None,
                                 use_app_drivers=True,
                                 morphs_todo_accum=None,
                                 bind_root_chunks_to_entity=True,
                                 target_collection=None,
                                 merged_armature_context=None,
                                 component_import_options=None):
    source_context_path = templateFilename if templateFilename and os.path.isabs(str(templateFilename)) else _repo_context_source_for_armature(base_animation_skeleton)
    if source_context_path and not getattr(add_app_template, "_repo_context_active", False):
        add_app_template._repo_context_active = True
        try:
            with redkit_repo_context(source_context_path):
                return add_app_template(
                    entity,
                    base_animation_skeleton,
                    group_parent,
                    ent_namespace,
                    import_redcloth_enabled,
                    i,
                    selectedAppearance,
                    hide_shadowmesh,
                    empty_transform,
                    root_skeleton,
                    templateFilename,
                    template_data=template_data,
                    appearance_indices=appearance_indices,
                    use_app_drivers=use_app_drivers,
                    morphs_todo_accum=morphs_todo_accum,
                    bind_root_chunks_to_entity=bind_root_chunks_to_entity,
                    target_collection=target_collection,
                    merged_armature_context=merged_armature_context,
                    component_import_options=component_import_options,
                )
        finally:
            add_app_template._repo_context_active = False
    constrains = []
    HardAttachments = []

    #DICTS
    objdict = {}
    objdict.update({entity.name:base_animation_skeleton})
    meshdict = {}
    
    #TODO check the scene to see if this template is already loaded, if loaded just adjust the drivers so it shows
    #TODO IMPORT 'chunks' dynamically from file
    templateMesh = None
    entity_back = None
    template_chunks = None
    if isinstance(template_data, dict):
        template_chunks = template_data.get('chunks')
        template_plan_complete = bool(template_data.get('plan_complete', False))
    elif hasattr(template_data, 'chunks'):
        template_chunks = getattr(template_data, 'chunks', None)
        template_plan_complete = bool(getattr(template_data, 'plan_complete', False))
    else:
        template_plan_complete = False
    if template_chunks or template_plan_complete or not str(templateFilename or "").strip():
        templateMesh = {'chunks': list(template_chunks or [])}
    else:
        (templateMesh, entity_back) = LoadCEntityTemplateFile(templateFilename, getattr(entity, "version", None))
    
    cur_chunks = templateMesh['chunks']
    _arm_rig = getattr(getattr(base_animation_skeleton, "data", None), "witcherui_RigSettings", None)
    template_source_game = getattr(_arm_rig, "source_game", "w3") if _arm_rig else "w3"
    mesh_import_settings = get_entity_mesh_import_settings(_arm_rig)
    
    local_morphs_todo = []
    (constrains, objdict, meshdict, HardAttachments, root_skeleton, local_morphs_todo) = import_chunks(
        entity,
        ent_namespace,
        cur_chunks,
        constrains,
        objdict,
        meshdict,
        HardAttachments,
        hide_shadowmesh,
        root_skeleton,
        i,
        selectedAppearance,
        import_redcloth_enabled,
        morphs_todo=local_morphs_todo,
        bind_root_chunks_to_entity=bind_root_chunks_to_entity,
        direct_entity_path=templateFilename,
        source_game=template_source_game,
        source_entity_path=source_context_path,
        target_collection=target_collection,
        mesh_import_settings=mesh_import_settings,
        merged_armature_context=merged_armature_context,
        component_import_options=component_import_options,
    )
    if morphs_todo_accum is not None and local_morphs_todo:
        morphs_todo_accum.extend(local_morphs_todo)
    source_armatures = []
    seen_source_armatures = set()
    for obj in objdict.values():
        if getattr(obj, "type", None) != 'ARMATURE':
            continue
        ident = id(obj)
        if ident in seen_source_armatures:
            continue
        seen_source_armatures.add(ident)
        source_armatures.append(obj)
    for target_armature in (base_animation_skeleton, root_skeleton):
        if getattr(target_armature, "type", None) != 'ARMATURE':
            continue
        repaired = armature_merge.fill_placeholder_armature_transforms_from_armatures(
            target_armature,
            source_armatures,
            context=bpy.context,
        )
        if repaired:
            log.info("Repaired %d placeholder rest transforms on %s", repaired, target_armature.name)
    #TODO do_constraints after each chunk not all together
    apperance_level_objects = do_constraints(
        constrains,
        objdict,
        meshdict,
        HardAttachments,
        group_parent,
        entity_name=str(getattr(entity, "name", "") or ""),
    )

    # Propagate face skeleton from equipment template if not already set
    if 'mimicFaceFile' in base_animation_skeleton:
        rig_settings = base_animation_skeleton.data.witcherui_RigSettings
        if not (getattr(rig_settings, "main_face_skeleton", "") or "").strip():
            rig_settings.main_face_skeleton = base_animation_skeleton['mimicFaceFile']

    imported_objects = [
        obj for key, obj in objdict.items()
        if obj is not None and key != entity.name and obj is not base_animation_skeleton
    ]
    imported_objects.extend([
        obj for obj in meshdict.values()
        if obj is not None and obj is not base_animation_skeleton
    ])
    grouped_root_objects = _get_import_root_objects(imported_objects)

    #if grouping the entire appreance together
    if group_parent:
        if bind_root_chunks_to_entity:
            group_objects = apperance_level_objects
            if template_source_game == "w2" or _merged_context_enabled(merged_armature_context):
                seen = {id(o) for o in group_objects}
                for obj in grouped_root_objects:
                    if obj is not None and id(obj) not in seen:
                        group_objects.append(obj)
                        seen.add(id(obj))
        else:
            group_objects = list(grouped_root_objects)
            seen = {id(o) for o in group_objects}
            for obj in apperance_level_objects:
                if id(obj) not in seen:
                    group_objects.append(obj)
        for obj in group_objects:
            if not _is_w2_head_slot_attached_object(obj):
                _set_parent_keep_world(obj, empty_transform)
        if use_app_drivers:
            # Drive only the imported template roots. Driving the shared appearance
            # empty causes later equipment/template imports to overwrite the empty's
            # visibility and hide reused shared templates (for example, same-head
            # appearance switches after loading equipment).
            for obj in group_objects:
                if obj is not None:
                    create_app_drivers(base_animation_skeleton, obj, appearance_indices)

    if _merged_context_enabled(merged_armature_context):
        _bind_unbound_skinned_meshes_to_merged_armature(imported_objects, base_animation_skeleton)
    

def _apply_coloring_entries_to_objects(objects, coloring_entries, appearance_name):
    """Apply coloringEntry custom properties to Blender mesh objects.

    Works with both SEntityTemplateColoringEntry objects (base_w3 supports dict-style
    access via __getitem__/get) and plain dicts.
    Matches each object's 'witcher_name' custom property against componentName.
    """
    if not objects:
        return
    coloring_lookup = _build_coloring_entry_lookup(coloring_entries, appearance_name)
    _apply_coloring_lookup_to_objects(objects, coloring_lookup)


def import_app(context,
               selectedAppearance,
               entity,
               base_animation_skeleton,
               component_import_options=None,
               load_appearance_equipment=True):
    base_context = context or bpy.context
    source_context_path = _repo_context_source_for_armature(base_animation_skeleton)
    if source_context_path and not getattr(import_app, "_repo_context_active", False):
        import_app._repo_context_active = True
        try:
            with redkit_repo_context(source_context_path):
                return import_app(
                    context,
                    selectedAppearance,
                    entity,
                    base_animation_skeleton,
                    component_import_options=component_import_options,
                    load_appearance_equipment=load_appearance_equipment,
                )
        finally:
            import_app._repo_context_active = False
    # Appearance import is another public boundary.  Wrap once here instead of
    # threading isolation-specific parameters down through template/chunk code.
    if import_isolation.needs_isolation_session(base_context):
        target_collection = _get_import_target_collection(base_context)
        visible_objects = import_isolation.collect_related_hierarchy_objects(base_animation_skeleton)
        with import_isolation.isolated_import_session(
            base_context,
            target_collection,
            label=f"{getattr(base_animation_skeleton, 'name', 'Character')}_{getattr(selectedAppearance, 'name', 'Appearance')}",
            visible_objects=visible_objects,
        ) as session:
            result = import_app(
                session.context,
                selectedAppearance,
                entity,
                base_animation_skeleton,
                component_import_options=component_import_options,
                load_appearance_equipment=load_appearance_equipment,
            )
        _focus_main_armature(base_context, base_animation_skeleton)
        return result

    target_collection = _get_import_target_collection(context)
    _activate_target_collection(context, target_collection)
    import_redcloth_enabled = get_do_import_redcloth(context)
    (exist, enabled) = addon_utils.check("io_mesh_apx")
    if not enabled:
        (exist, enabled) = addon_utils.check("io_scene_apx")
    if not enabled:
        import_redcloth_enabled = False

    save_world = base_animation_skeleton.matrix_world.copy()
    save_local = base_animation_skeleton.matrix_local.copy()
    save_basis = base_animation_skeleton.matrix_basis.copy()
    save_location = base_animation_skeleton.location.copy()
    save_scale = base_animation_skeleton.scale.copy()
    reset_transforms(base_animation_skeleton)
    current_pose_position = base_animation_skeleton.data.pose_position
    base_animation_skeleton.data.pose_position = "REST"

    ent_namespace = entity.name+":"

    #OPTIONS
    hide_shadowmesh = True
    mimic_namespace = False
    root_skeleton = base_animation_skeleton
    faceData = False
    group_parent = True #None

    if group_parent:
        group_parent = entity.name
        appearance_names = {
            str(_get_entry_attr(app, "name", "") or "").strip()
            for app in (_get_entry_attr(entity, "appearances", []) or [])
            if str(_get_entry_attr(app, "name", "") or "").strip()
        }
        for child in base_animation_skeleton.children:
            if getattr(child, "type", "") != 'EMPTY':
                continue
            child_app_name = str(child.get("witcher_app_name", "") or child.name or "").strip()
            if child_app_name not in appearance_names:
                continue
            _remove_hide_drivers(child)
            child.hide_render = False
            child.hide_viewport = False
        # Check if appearance group empty already exists (prevents duplicates on re-load)
        # Use custom property 'witcher_app_name' to match regardless of Blender-renamed object names
        empty_transform = None
        for child in base_animation_skeleton.children:
            if child.type == 'EMPTY' and child.get("witcher_app_name") == selectedAppearance.name:
                empty_transform = child
                break
        if empty_transform is None:
            # Fallback: name match for empties created before this fix
            for child in base_animation_skeleton.children:
                if child.type == 'EMPTY' and child.name == selectedAppearance.name:
                    empty_transform = child
                    break

        if empty_transform is None:
            # Create new group for this appearance
            bpy.ops.object.empty_add(type="PLAIN_AXES", radius=1)
            empty_transform = bpy.context.object
            empty_transform.name = selectedAppearance.name
            empty_transform["witcher_app_name"] = selectedAppearance.name
            empty_transform.parent = base_animation_skeleton

    morphs_todo = []

    rig_settings = base_animation_skeleton.data.witcherui_RigSettings
    merged_armature_context = _new_merged_armature_context(
        context,
        root_armature=base_animation_skeleton,
    )

    # =====================================================
    # TEMPLATE LOADING (shared-aware, GUID-tracked)
    # =====================================================
    from ..ui.ui_equipment import hide_objects_by_guid, find_objects_by_guid
    app_name = selectedAppearance.name

    # Build template->appearances map from entity data for correct driver expressions
    template_map = build_template_appearance_map(entity)

    # Build lookup of already-loaded templates by filename
    loaded_templates = {slot.template_filename: slot for slot in rig_settings.template_slots}
    new_template_filenames = set()
    # Build a GUID index once to avoid repeated O(N) scans of bpy.data.objects
    guid_index = _build_guid_index("witcher_template_guid")

    for i in range(len(selectedAppearance.includedTemplates)):
        templateFilename = selectedAppearance.includedTemplates[i]['templateFilename']
        new_template_filenames.add(templateFilename)
        
        # Get ALL appearances that use this template (from entity data)
        template_appearances = template_map.get(templateFilename, {}).get('indices', [])

        if templateFilename in loaded_templates:
            # Template already loaded - reuse it, just update appearance tracking
            slot = loaded_templates[templateFilename]
            app_names = set(slot.appearance_names.split(',')) if slot.appearance_names else set()
            app_names.discard('')
            app_names.add(app_name)
            slot.appearance_names = ','.join(app_names)

            # Check if this template still has objects in the scene
            slot_has_objects = False
            if slot.template_guid:
                slot_has_objects = len(guid_index.get(slot.template_guid, [])) > 0

            # If already loaded and objects exist, just update drivers/visibility
            if slot.is_loaded and slot_has_objects:
                # Unhide if hidden
                if slot.is_hidden:
                    for obj in guid_index.get(slot.template_guid, []):
                        obj.hide_set(False)
                    slot.is_hidden = False
                # Re-apply coloring entries for this appearance (appearance may have changed)
                if getattr(entity, 'coloringEntries', None):
                    _apply_coloring_entries_to_objects(
                        guid_index.get(slot.template_guid, []),
                        entity.coloringEntries,
                        app_name,
                    )
                continue  # Skip re-importing - preserves morphs and shape keys

            # Template slot exists but is missing in the scene or unloaded - reimport
            if not slot_has_objects:
                slot.template_guid = ""
            slot.is_loaded = False
            template_data = selectedAppearance.includedTemplates[i]
            slot.ns = _get_entry_attr(template_data, 'ns', '')
            slot.data_json = _to_json_text(template_data, indent=2)

            guid = generate_guid()
            before = _snapshot_collection_object_ids(target_collection)
            before_all = set(bpy.data.objects) if before is None else None

            # Pass ALL appearance indices for this template so drivers are correct from the start
            add_app_template(entity,
                             base_animation_skeleton,
                             group_parent,
                             ent_namespace,
                             import_redcloth_enabled,
                             i,
                             selectedAppearance,
                             hide_shadowmesh,
                             empty_transform,
                             root_skeleton,
                             templateFilename,
                             selectedAppearance.includedTemplates[i],
                             template_appearances,
                             morphs_todo_accum=morphs_todo,
                             target_collection=target_collection,
                             merged_armature_context=merged_armature_context,
                             component_import_options=component_import_options)

            new_objects = _tag_new_collection_objects_with_guid(
                target_collection,
                before,
                guid,
                "witcher_template_guid",
            )
            if new_objects is None:
                new_objects = tag_new_objects_with_guid(before_all, guid, "witcher_template_guid")
            guid_index[guid] = list(new_objects)  # Update index with new objects
            slot.template_guid = guid
            slot.is_loaded = True

            # Unhide if hidden
            if slot.is_hidden:
                for obj in guid_index.get(slot.template_guid, []):
                    obj.hide_set(False)
                slot.is_hidden = False
            continue

        # New template — create slot and import
        slot = rig_settings.template_slots.add()
        slot.template_filename = templateFilename
        template_data = selectedAppearance.includedTemplates[i]
        slot.ns = _get_entry_attr(template_data, 'ns', '')
        slot.data_json = _to_json_text(template_data, indent=2)
        slot.appearance_names = app_name

        guid = generate_guid()
        before = _snapshot_collection_object_ids(target_collection)
        before_all = set(bpy.data.objects) if before is None else None

        # Pass ALL appearance indices for this template so drivers are correct from the start
        add_app_template(entity,
                         base_animation_skeleton,
                         group_parent,
                         ent_namespace,
                         import_redcloth_enabled,
                         i,
                         selectedAppearance,
                         hide_shadowmesh,
                         empty_transform,
                         root_skeleton,
                         templateFilename,
                         selectedAppearance.includedTemplates[i],
                         template_appearances,
                         morphs_todo_accum=morphs_todo,
                         target_collection=target_collection,
                         merged_armature_context=merged_armature_context,
                         component_import_options=component_import_options)

        new_objects = _tag_new_collection_objects_with_guid(
            target_collection,
            before,
            guid,
            "witcher_template_guid",
        )
        if new_objects is None:
            new_objects = tag_new_objects_with_guid(before_all, guid, "witcher_template_guid")
        guid_index[guid] = list(new_objects)  # Update index with new objects
        slot.template_guid = guid
        slot.is_loaded = True

    # Keep template driver expressions authoritative on every appearance load.
    # This repairs stale state from older scenes where another import path may
    # have overwritten shared-template visibility drivers.
    for slot in rig_settings.template_slots:
        if slot.is_loaded and slot.template_guid:
            update_template_drivers_for_appearances(slot.template_guid, rig_settings)

    # =====================================================
    # EQUIPMENT LOADING (GUID-tracked, persistent)
    # =====================================================
    # Preserve inventory and user-pinned slots across appearances.
    for i in reversed(range(len(rig_settings.equipment_slots))):
        slot = rig_settings.equipment_slots[i]
        if not getattr(slot, "is_inventory", False) and not getattr(slot, "keep_across_appearances", False):
            if getattr(slot, "is_loaded", False) and getattr(slot, "equip_guid", ""):
                try:
                    remove_objects_by_guid(slot.equip_guid, "witcher_equip_guid")
                except Exception:
                    pass
            rig_settings.equipment_slots.remove(i)

    # Get equipment entries from appearance data
    appearance_params = []
    if hasattr(selectedAppearance, 'appearanceParams'):
        appearance_params = selectedAppearance.appearanceParams
    elif isinstance(selectedAppearance, dict):
        appearance_params = selectedAppearance.get('appearanceParams', [])

    equipment_entries_data = []
    if appearance_params and len(appearance_params) > 0:
        first_param = appearance_params[0]
        if isinstance(first_param, dict) and 'entries' in first_param:
            equipment_entries_data = first_param['entries']
        elif hasattr(first_param, 'entries'):
            equipment_entries_data = first_param.entries

    source_roots = _get_armature_source_roots(base_animation_skeleton)
    if not source_roots:
        repo_path_hint = getattr(rig_settings, "repo_path", "") or ""
        if repo_path_hint and os.path.isabs(repo_path_hint):
            try:
                source_roots = _build_entity_source_roots(repo_path_hint)
            except Exception:
                source_roots = []
    source_game = str(getattr(rig_settings, "source_game", "") or "").strip().lower()
    if source_game not in {"w2", "w3"}:
        source_game = _source_game_from_version(entity)
    item_lookup, template_lookup = _build_equipment_lookup(source_roots)
    try:
        from ..ui.ui_equipment import get_equipment_catalog_for_search_roots
        category_items, item_attributes = get_equipment_catalog_for_search_roots(source_roots)
    except Exception:
        category_items, item_attributes = {}, {}
    equipment_load_context = {
        "entity": entity,
        "appearance": selectedAppearance,
        "source_roots": source_roots,
        "source_game": source_game,
    }
    def _slot_has_persistent_override(slot):
        if slot is None or not getattr(slot, "keep_across_appearances", False):
            return False
        item_name = str(getattr(slot, "item_name", "") or "").strip().lower()
        equip_template = str(getattr(slot, "equip_template", "") or "").strip().lower()
        return (bool(item_name) and item_name != "none") or (bool(equip_template) and equip_template != "none")

    protected_categories = {
        slot.category for slot in rig_settings.equipment_slots
        if slot.category and (
            getattr(slot, "is_inventory", False)
            or _slot_has_persistent_override(slot)
        )
    }
    persistent_slot_indices = [
        idx for idx, slot in enumerate(rig_settings.equipment_slots)
        if _slot_has_persistent_override(slot)
    ]
    deferred_default_slot_indices = []

    for i, entry_data in enumerate(equipment_entries_data):
        category = entry_data.get('category', '') if isinstance(entry_data, dict) else getattr(entry_data, 'category', '')
        if category and category in protected_categories:
            continue
        default_item = get_equipment_entry_item_name(entry_data, rig_settings) or ''

        # Create persistent equipment slot
        slot = rig_settings.equipment_slots.add()
        slot_index = len(rig_settings.equipment_slots) - 1
        slot.source_game = source_game
        slot.category = category
        slot.item_name = default_item
        slot.resolved_repo_path = ""
        slot.keep_across_appearances = False

        # Find the equip_template for this item
        equip_template = ''
        if source_game == "w2":
            entry_w2_path = str(_get_entry_attr(entry_data, "w2_entity_path", "") or "").strip()
            if entry_w2_path:
                equip_template = entry_w2_path
        if not equip_template and default_item and default_item != 'None':
            resolved_item = _resolve_inventory_item(default_item, item_lookup, template_lookup)
            if resolved_item:
                resolved_category, resolved_item_name, resolved_template = resolved_item
                if resolved_category and not slot.category:
                    slot.category = resolved_category
                if resolved_item_name:
                    slot.item_name = resolved_item_name
                equip_template = resolved_template
            if not equip_template:
                # Try category-specific lookup for this item from loaded XML data.
                cat_items = category_items.get(category, [])
                for item_name, _, tmpl in cat_items:
                    if item_name == default_item:
                        equip_template = tmpl
                        break
            if not equip_template:
                equip_template = default_item  # Fallback: use item name as template

        if not slot.category:
            fallback = _derive_template_from_item(default_item) or default_item or f"slot_{slot_index}"
            slot.category = fallback

        slot.equip_template = equip_template
        slot.base_equip_template = equip_template

        # Populate extra attributes if available
        try:
            attrs = _lookup_item_attrs(
                item_attributes, source_game, slot.item_name, default_item, equip_template
            )
            if attrs:
                slot.equip_slot = attrs.get('equip_slot', slot.equip_slot)
                slot.hold_slot = attrs.get('hold_slot', slot.hold_slot)
                slot.weapon = attrs.get('weapon', slot.weapon)
                slot.attachment_type = attrs.get('attachment_type', '')
                try:
                    slot.variants_json = json.dumps(attrs.get('variants', []))
                except Exception:
                    slot.variants_json = ""
                try:
                    slot.bound_items_json = json.dumps(attrs.get('bound_items', []))
                except Exception:
                    slot.bound_items_json = ""
        except Exception:
            pass

        if equip_template and equip_template != "None":
            # Defaults go through the shared loader so slot mounting,
            # bound items and attachment type handling work consistently.
            deferred_default_slot_indices.append(slot_index)
            continue

    if load_appearance_equipment:
        _apply_inventory_mounts(
            context,
            base_animation_skeleton,
            selectedAppearance,
            rig_settings,
            entity,
            shared_inventory=True,
            prepared_context=equipment_load_context,
            post_refresh=not deferred_default_slot_indices,
        )

    # Defaults must be loaded through the shared equipment loader so
    # they get mounted to their equip_slot immediately on import.
    slots_to_reload = list(dict.fromkeys(deferred_default_slot_indices + persistent_slot_indices))
    if load_appearance_equipment and slots_to_reload:
        try:
            from ..ui.ui_equipment import refresh_slot_constraints, load_equipment_items_batch
            refresh_slot_constraints(base_animation_skeleton)
            try:
                bpy.context.view_layer.update()
            except Exception:
                pass
            load_equipment_items_batch(
                context,
                base_animation_skeleton,
                slots_to_reload,
                rig_settings,
                prepared_context=equipment_load_context,
                reload_loaded=bool(persistent_slot_indices),
                post_refresh_variants=True,
                mount_mode=None,
            )
        except Exception as e:
            log.warning("Failed to load deferred equipment: %s", e)

    # Refresh variant state after equipment slots populated
    try:
        from ..ui.ui_equipment import refresh_variant_states
        refresh_variant_states(rig_settings)
    except Exception:
        pass

    # Sync persistent slots → temp UI entries so the equipment panel
    # reflects the newly-created slots immediately after import.
    try:
        from ..ui.ui_equipment import sync_equipment_slots_to_temp
        sync_equipment_slots_to_temp(context, rig_settings)
    except Exception:
        pass


    # Full face-morph loading is handled by import_ent_template while the entity
    # import session is still isolated.  This appearance-level code only keeps
    # component morph targets wired below.
    #if grouping the entire appreance together
    # if group_parent:
    #     for obj in apperance_level_objects:
    #         obj.parent = empty_transform
    #     create_app_drivers(base_animation_skeleton, empty_transform)
    rig_settings = base_animation_skeleton.data.witcherui_RigSettings
    main_obj = base_animation_skeleton
    rig_settings.model_armature_object = main_obj

    for morph in morphs_todo:
        morphComponentId = morph[0]
        (morphSourceMeshes, morphSourceArmatures) = morph[1]
        control_bone_name = 'w3_face_poses'
        pose_name = morphComponentId
        
        #ADD THE BONE AND THE MORPH PROP TO BONE
        create_control_bone(main_obj, control_bone_name)
        bl_ctrl_bone_pose = main_obj.pose.bones[control_bone_name]
        bl_ctrl_bone_pose[pose_name] = 0.0
        property_manager = bl_ctrl_bone_pose.id_properties_ui(pose_name)
        property_manager.update(min = 0., max = 1)
        witcherui_add_redmorph(rig_settings.witcher_morphs_list, [pose_name, pose_name, 3])
        #!GET MESH OBJECTS FOR THIS AND APPLY SHAPE KEYS

        for the_mesh in morphSourceMeshes:
            create_morph_and_driver(context, main_obj, the_mesh, pose_name)
            if the_mesh.data.shape_keys and the_mesh.data.shape_keys.animation_data is not None:
                for oDrv in the_mesh.data.shape_keys.animation_data.drivers:
                    driver = oDrv.driver
                    driver.expression += " "
                    driver.expression = driver.expression[:-1]
    
    #! RETURN MAIN OBJECT
    bpy.context.view_layer.objects.active = main_obj
    #go trough all morphs again to make sure drivers are set
    for morph in morphs_todo:
        morphComponentId = morph[0]
        (morphSourceMeshes, morphSourceArmatures) = morph[1]
        for the_mesh in morphSourceMeshes:
            if the_mesh.data.shape_keys and the_mesh.data.shape_keys.animation_data is not None:
                for oDrv in the_mesh.data.shape_keys.animation_data.drivers:
                    driver = oDrv.driver
                    driver.expression += " "
                    driver.expression = driver.expression[:-1]

    base_animation_skeleton.matrix_world = save_world
    base_animation_skeleton.matrix_local = save_local
    base_animation_skeleton.matrix_basis = save_basis
    base_animation_skeleton.location = save_location
    base_animation_skeleton.scale = save_scale
    base_animation_skeleton.data.pose_position = current_pose_position

def import_from_list_item(context, item, component_import_options=None,
                          load_appearance_equipment=True):
    base_animation_skeleton, rig_settings = get_main_armature_and_rig_settings(
        context,
        prefer_active=True,
        remember=True,
        fallback=True,
    )
    if base_animation_skeleton and rig_settings:
        entity, _entity_data = get_rig_entity_state(rig_settings)
        if entity is None:
            log.warning("import_from_list_item: no cached entity state for armature '%s'.", base_animation_skeleton.name)
            return

        for app in entity.appearances:
            if app.name == item.name:
                was_lazy_dlc_app = bool(getattr(app, "_dlc_mounter_lazy", False))
                if not realize_dlc_external_appearance(entity, app, context):
                    log.warning("import_from_list_item: failed to load DLC appearance '%s'.", item.name)
                    return
                if was_lazy_dlc_app:
                    cache_rig_entity_state(rig_settings, entity, update_json=True)
                import_app(
                    context,
                    app,
                    entity,
                    base_animation_skeleton,
                    component_import_options=component_import_options,
                    load_appearance_equipment=load_appearance_equipment,
                )
                _focus_main_armature(context, base_animation_skeleton)
                #bpy.ops.witcher.load_face_morphs()
                return
    else:
        log.warning("import_from_list_item: no target armature selected.")
