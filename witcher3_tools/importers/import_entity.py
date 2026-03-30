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
    set_external_import_dependency_alert,
)
from ..external_addon_tools import get_apx_addon_status, resolve_redcloth_apx
#from io_import_w2l import settings
from .. import fbx_util
from .. import cloth_util
from .. import constrain_util
from ..CR2W import read_json_w3
from ..CR2W import w3_types
from ..CR2W.dc_entity import load_bin_entity
from ..CR2W.dc_entity import LoadCEntityTemplateFile, clear_template_cache
from ..CR2W.dc_entity import is_valid_mesh_path
from ..CR2W.CR2W_types import EngineTransform
from ..importers.import_helpers import set_blender_object_transform
from ..ui.ui_morphs import witcherui_add_redmorph, create_control_bone, create_morph_and_driver
from ..CR2W.common_blender import repo_file
from ..CR2W.dc_beh import read_beh_info as _read_beh_info, guess_idle as _beh_guess_idle
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


@persistent
def _clear_entity_cache_on_load(_filepath=""):
    """Clear the runtime entity cache whenever a new .blend file is loaded.

    Memory addresses (as_pointer) and Python ids from the old session are
    meaningless after a file load; clearing here prevents stale cache hits and
    lets the old Entity objects be garbage-collected.
    """
    _ENTITY_RUNTIME_CACHE.clear()


def _register_entity_cache_handler():
    if _clear_entity_cache_on_load not in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.append(_clear_entity_cache_on_load)


def _unregister_entity_cache_handler():
    if _clear_entity_cache_on_load in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_clear_entity_cache_on_load)


_register_entity_cache_handler()


def _norm_redcloth_key_path(value) -> str:
    return str(value or "").replace("/", "\\").lower()


def _make_redcloth_reuse_key(resource_path: str, redcloth_mat_path: str) -> str:
    return f"{_norm_redcloth_key_path(resource_path)}|{_norm_redcloth_key_path(redcloth_mat_path)}"


def _get_chunk_component_name(chunk) -> str:
    component_name = str(chunk.get("name", "") or "").strip()
    if component_name:
        return component_name
    resource_path = str(chunk.get("resource", "") or "").strip()
    if resource_path.lower().endswith(".redcloth"):
        return Path(resource_path.replace("/", "\\")).stem
    return ""


def _mesh_uses_armature(obj, armature_obj) -> bool:
    if obj is None or obj.type != 'MESH' or armature_obj is None:
        return False
    for mod in getattr(obj, "modifiers", []):
        if mod.type == 'ARMATURE' and mod.object == armature_obj:
            return True
    return False


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


def _find_reusable_redcloth_armature(owner_armature, reuse_key: str):
    if owner_armature is None or not reuse_key:
        return None
    for obj in _iter_object_descendants(owner_armature):
        try:
            if obj.type != 'ARMATURE':
                continue
            if obj.get("witcher_redcloth_reuse_key") == reuse_key:
                return obj
        except Exception:
            continue
    return None


def _tag_redcloth_for_reuse(cloth_armature, reuse_key: str, resource_path: str, redcloth_mat_path: str):
    if cloth_armature is None:
        return
    targets = [cloth_armature]
    parent = getattr(cloth_armature, "parent", None)
    if parent is not None:
        targets.append(parent)
    for obj in targets:
        try:
            obj["witcher_redcloth_reuse_key"] = reuse_key
            obj["witcher_redcloth_resource"] = resource_path or ""
            obj["witcher_redcloth_material"] = redcloth_mat_path or ""
        except Exception:
            pass


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
                    chunk['resource_apx'] = get_W3_REDCLOTH_PATH(bpy.context)+"\\"+resource.replace(".redcloth", ".apx")
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

def NewAnimsetListItem( treeList, path, name):
    item = treeList.add()
    if path:
        item.path = path
    if name:
        item.name = name
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
    if isinstance(value, (bytes, bytearray)):
        return list(value)
    if isinstance(value, memoryview):
        return list(value.tobytes())
    if isinstance(value, Path):
        return str(value)
    if _depth > _TO_PLAIN_DATA_MAX_DEPTH:
        return None
    if isinstance(value, dict):
        if _visited is None:
            _visited = set()
        return {key: _to_plain_data(item, _visited, _depth + 1) for key, item in value.items()}
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
        return value
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
    imported_objects = [obj for obj in (objects or []) if obj is not None]
    if not imported_objects:
        return []
    imported_ids = {id(obj) for obj in imported_objects}
    roots = [
        obj for obj in imported_objects
        if obj.parent is None or id(obj.parent) not in imported_ids
    ]
    return roots or imported_objects


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


def _apply_chunk_transform_to_import_roots(chunk, *, armatures=None, meshes=None):
    rt = _coerce_engine_transform(chunk.get("transform"))
    if rt is None:
        return

    armatures = [obj for obj in (armatures or []) if obj is not None]
    meshes = [obj for obj in (meshes or []) if obj is not None]
    target_objects = armatures if armatures else meshes
    if not target_objects:
        return

    no_rotation = not bool(armatures)
    for obj in _get_import_root_objects(target_objects):
        set_blender_object_transform(obj, rt, rotate_180=False, no_rotation=no_rotation)


def import_direct_entity_file(filename, load_face_poses=False, import_apperance=0, parent_transform=None):
    _, ext = os.path.splitext(str(filename or ""))
    if ext.lower() == ".json":
        log.info("Importing entity via common importer (JSON): %s", filename)
        return import_ent_template(filename, load_face_poses, import_apperance, parent_transform)

    before_objects = set(bpy.data.objects)
    try:
        log.info("Importing entity via common importer: %s", filename)
        result = import_ent_template(filename, load_face_poses, import_apperance, parent_transform)
    except Exception:
        if set(bpy.data.objects) != before_objects:
            raise
        log.warning("Common importer failed for %s with no imported objects; falling back to legacy importer.", filename, exc_info=True)
        result = None
    else:
        if result is not None:
            return result
        if set(bpy.data.objects) != before_objects:
            log.info("Keeping common-import result for %s despite missing main armature return value.", filename)
            return result
        log.info("Common importer produced no objects for %s; falling back to legacy importer.", filename)

    from ..CR2W import CR2W_reader
    from ..importers import import_w2l

    log.info("Importing entity via legacy importer fallback: %s", filename)
    legacy_entity = CR2W_reader.load_entity(filename)
    import_w2l.btn_import_w2ent(legacy_entity)
    return None


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

def test_load_entity(filename) ->  w3_types.Entity:
    # #TODO add this custom json after normal bin file is loaded
    # if filename.endswith("geralt_player.w2ent") or filename.endswith(r"player\player.w2ent"):
    #     RES_DIR = Path(__file__)
    #     RES_DIR = str(Path(RES_DIR).parents[1])
    #     filename = os.path.join(RES_DIR, r"CR2W\data\geralt_CUSTOM.w2ent.json")

    dirpath, file = os.path.split(filename)
    basename, ext = os.path.splitext(file)
    if ext.lower() in ('.json'):
        entity = read_json_w3.readEntFile(filename)
    elif ext.lower().endswith('.w2ent') or ext.lower().endswith('.w3app'):
        entity = load_bin_entity(filename)
    else:
        entity = None
    return entity

def _try_import_armature_from_item_appearances(entity, parent_transform=None, source_game="", target_collection=None):
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
        else:
            tmpl_filename = getattr(tmpl, 'templateFilename', '')
        if not tmpl_filename:
            continue
        try:
            (_, sub_entity) = LoadCEntityTemplateFile(tmpl_filename)
            if sub_entity is None:
                continue
            arm = import_MovingPhysicalAgentComponent(
                sub_entity,
                parent_transform,
                direct_entity_path=tmpl_filename,
                source_game=source_game,
                target_collection=target_collection,
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
    return bool(component_type and component_type != "CMovingPhysicalAgentComponent")


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


def import_ent_template(filename, load_face_poses = False, import_apperance = 0,
                        parent_transform = None, selected_appearance_name = ""):
    clear_template_cache()
    context = bpy.context
    target_collection = _get_import_target_collection(context)
    _activate_target_collection(context, target_collection)
    entity = test_load_entity(filename)
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

    base_animation_skeleton = import_MovingPhysicalAgentComponent(
        entity,
        entity_root,
        direct_entity_path=entity_repo_path,
        source_game=entity_source_game,
        target_collection=target_collection,
    )
    main_arm_obj = base_animation_skeleton

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
            if created_entity_root and not created_entity_root.children:
                bpy.data.objects.remove(created_entity_root, do_unlink=True)
            return None
    elif entity_root and getattr(main_arm_obj, "parent", None) is None:
        main_arm_obj.parent = entity_root
    _focus_main_armature(context, main_arm_obj)
    main_arm_obj["_w3_entity_import_in_progress"] = True
    try:
        rig_settings = initialize_entity_armature_state(
            main_arm_obj,
            entity,
            update_json=True,
            entity_state=entity_state,
        )

        app_idx = -1 if entity_state is None else int(entity_state.get("app_idx", -1))
        if rig_settings and getattr(entity, "appearances", None) and app_idx >= 0:
            item = rig_settings.app_list[app_idx]
            import_from_list_item(context, item)

        # Refresh slot constraints after all components are imported
        try:
            from ..ui.ui_equipment import refresh_slot_constraints
            refresh_slot_constraints(main_arm_obj)
        except Exception:
            pass

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
        "\\game\\",
        "\\gameplay\\",
        "\\items\\",
        "\\characters\\",
        "\\dlc\\",
        "\\quests\\",
        "\\levels\\",
        "\\living_world\\",
        "\\environment\\",
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
                                     context_role="primary", entity_state=None):
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
        for group_name, paths in _collect_armature_animset_groups(entity, armature_obj):
            NewAnimsetListItem(animset_list, f"{group_name}:", group_name)
            for path in paths:
                NewAnimsetListItem(animset_list, path, group_name)

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
                                         context_role="primary"):
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

def create_app_drivers(armobj: bpy.types.Armature, obj_to_hide:bpy.types.Object, appearance_indices=None):
    """Create hide drivers on object and children.
    
    Args:
        armobj: The armature to reference in driver
        obj_to_hide: Object to add drivers to
        appearance_indices: Optional list of appearance indices where object should be visible.
                           If None, uses current app_list_index only.
    """
    # Keep shadowmesh objects hidden regardless of active appearance.
    if _is_shadowmesh_name(getattr(obj_to_hide, "name", "")):
        _force_shadowmesh_hidden(obj_to_hide)
        for obj in obj_to_hide.children:
            create_app_drivers(armobj, obj, appearance_indices)
        return

    rig_settings = armobj.data.witcherui_RigSettings
    
    if appearance_indices is None or len(appearance_indices) <= 1:
        # Single appearance - use simple inequality
        current_app_list_index = rig_settings.app_list_index
        create_on_prop(armobj, current_app_list_index, obj_to_hide, prop_name = "hide_render")
        create_on_prop(armobj, current_app_list_index, obj_to_hide, prop_name = "hide_viewport")
    else:
        # Multiple appearances - create drivers with "not in" expression
        indices_str = ", ".join(str(i) for i in sorted(appearance_indices))
        
        for prop_name in ["hide_render", "hide_viewport"]:
            # Check if driver already exists for this property
            has_driver = False
            if obj_to_hide.animation_data and obj_to_hide.animation_data.drivers:
                for fc in obj_to_hide.animation_data.drivers:
                    if fc.data_path == prop_name:
                        # Update existing driver expression
                        fc.driver.expression = f"idx_on_app_list not in [{indices_str}]"
                        has_driver = True
                        break
            
            if not has_driver:
                # Create new driver
                driver_curve = obj_to_hide.driver_add(prop_name)
                driver = driver_curve.driver
                driver.expression = f"idx_on_app_list not in [{indices_str}]"
                var = driver.variables.new()
                var.type = "SINGLE_PROP"
                var.name = "idx_on_app_list"
                target = var.targets[0]
                target.id_type = "ARMATURE"
                target.data_path = "witcherui_RigSettings.app_list_index"
                target.id = armobj.data
    
    for obj in obj_to_hide.children:
        create_app_drivers(armobj, obj, appearance_indices)


def update_driver_for_shared_template(obj, appearance_indices):
    """Update drivers on an object to show it for multiple appearance indices.
    
    Args:
        obj: The Blender object with hide drivers
        appearance_indices: List of appearance indices where this should be visible
    """
    if _is_shadowmesh_name(getattr(obj, "name", "")):
        _force_shadowmesh_hidden(obj)
        for child in obj.children:
            update_driver_for_shared_template(child, appearance_indices)
        return

    if not appearance_indices:
        return
    
    # Build expression like "idx_on_app_list not in [0, 2, 3]" 
    # (hidden when NOT in the list of valid appearances)
    indices_str = ", ".join(str(i) for i in sorted(appearance_indices))
    new_expression = f"idx_on_app_list not in [{indices_str}]"
    
    for prop_name in ["hide_render", "hide_viewport"]:
        if obj.animation_data and obj.animation_data.drivers:
            for driver_curve in obj.animation_data.drivers:
                if driver_curve.data_path == prop_name:
                    driver_curve.driver.expression = new_expression
    
    # Recursively update children
    for child in obj.children:
        update_driver_for_shared_template(child, appearance_indices)


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
        update_driver_for_shared_template(obj, appearance_indices)

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
        return entry.get(key, default)
    return getattr(entry, key, default)


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


def _mesh_skinning_cache_key(mesh_path):
    if not mesh_path:
        return None
    try:
        normalized = os.path.normcase(os.path.normpath(mesh_path))
    except Exception:
        normalized = str(mesh_path)
    try:
        return (
            normalized,
            os.path.getmtime(mesh_path),
            os.path.getsize(mesh_path),
        )
    except Exception:
        return (normalized,)


def _mesh_path_is_skinned(mesh_path, version=999) -> bool:
    mesh_path = str(mesh_path or "").strip()
    if not mesh_path:
        return False

    resolved_path = mesh_path if os.path.isabs(mesh_path) else repo_file(mesh_path, version)
    resolved_path = str(resolved_path or "").strip()
    if not resolved_path or not os.path.exists(resolved_path):
        return False

    cache_key = _mesh_skinning_cache_key(resolved_path)
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

    def _add_group(raw_name, paths):
        animset_paths = _normalize_animset_paths(paths)
        if not animset_paths:
            return
        group_name = str(raw_name or "").strip() or "AnimSets"
        group_paths = group_lookup.get(group_name)
        if group_paths is None:
            group_paths = []
            group_lookup[group_name] = group_paths
            groups.append((group_name, group_paths))
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
        _add_group(component_group_name, _get_entry_attr(component_chunk, "animationSets", []))

    matched_param = False
    saw_scoped_param = False
    animset_params = getattr(entity, "CAnimAnimsetsParam", []) or []
    for animset_param in animset_params:
        param_component_name = str(_get_entry_attr(animset_param, "componentName", "") or "").strip()
        if param_component_name:
            saw_scoped_param = True
        if component_name and param_component_name != component_name:
            continue
        if not component_name and param_component_name:
            continue
        _add_group(_get_entry_attr(animset_param, "name", "AnimSets"), _get_entry_attr(animset_param, "animationSets", []))
        matched_param = True

    if not matched_param and not saw_scoped_param and component_type != "CMimicComponent":
        for animset_param in animset_params:
            _add_group(_get_entry_attr(animset_param, "name", "AnimSets"), _get_entry_attr(animset_param, "animationSets", []))

    if component_type == "CMimicComponent":
        for mimic_set in getattr(entity, "CAnimMimicParam", []) or []:
            _add_group(f"{_get_entry_attr(mimic_set, 'name', 'MimicSets')} (Mimic)", _get_entry_attr(mimic_set, "animationSets", []))

    return [(group_name, paths) for group_name, paths in groups if paths]


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
            load_equipment_items_batch,
            refresh_variant_states,
        )
    except Exception:
        return
    prepared = prepared_context if prepared_context is not None else {}
    prepared.setdefault("source_roots", source_roots)
    prepared = _prepare_equipment_load_context(armature, rig_settings, prepared)
    source_roots = prepared.get("source_roots", source_roots)

    item_lookup, template_lookup = _build_equipment_lookup(source_roots)
    slots = rig_settings.equipment_slots
    slot_by_category = {slot.category: (idx, slot) for idx, slot in enumerate(slots) if slot.category}
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
                item_raw = _get_inventory_item_name(entry)
                item_key = _normalize_key(item_raw)
                if not item_key or item_key in {"none", "random", "null"}:
                    continue
                category_key = _normalize_key(_get_inventory_category(entry))
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
                slot_policy = _resolve_slot_visual_policy(slot, armature, rig_settings)
                if slot_policy["policy"] != "equipable_on_rig":
                    continue
                if not (getattr(slot, "is_loaded", False) and getattr(slot, "equip_guid", "")):
                    existing_loaded = False
                    break
            if desired_mounts and desired_mounts.issubset(existing_mounts) and existing_loaded:
                return
    seen_entries = set()
    equip_category_keywords = ("sword", "weapon", "armor", "boots", "gloves", "pants", "trousers", "crossbow", "head", "hair", "axe", "mace")

    category_items, item_attributes = get_equipment_catalog_for_search_roots(source_roots)
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
        is_mount = _get_entry_attr(entry, "isMount", None)
        if is_mount is None:
            # Some inventory entries omit isMount but are equipment categories
            if category_raw in slot_by_category:
                is_mount = True
            else:
                cat_lower = str(category_raw).lower() if category_raw else ""
                item_lower = str(item_raw).lower() if item_raw else ""
                is_mount = any(token in cat_lower for token in equip_category_keywords) or \
                           any(token in item_lower for token in equip_category_keywords)
        if not is_mount:
            continue
        dedupe_key = (_normalize_key(category_raw), _normalize_key(item_raw))
        if dedupe_key in seen_entries:
            continue
        seen_entries.add(dedupe_key)
        item_key = _normalize_key(item_raw)
        if not item_key or item_key in {"none", "random", "null"}:
            continue

        slot_index = None
        slot = None
        resolved_category = ""
        resolved_item_name = ""
        resolved_template = ""
        resolved = _resolve_inventory_item(item_raw, item_lookup, template_lookup)
        if resolved:
            resolved_category, resolved_item_name, resolved_template = resolved

        # Prefer slot by inventory category, then resolved category, then item/template match
        if category_raw and category_raw in slot_by_category:
            slot_index, slot = slot_by_category[category_raw]
        elif resolved_category and resolved_category in slot_by_category:
            slot_index, slot = slot_by_category[resolved_category]
        else:
            slot_index, slot = _find_slot_by_item_or_template(slot_search_list, item_raw)

        slot_was_created = False
        # If no slot exists, create one for this mounted inventory item
        if slot is None and is_mount:
            new_category = category_raw or resolved_category or _derive_template_from_item(item_raw) or str(item_raw)
            if not new_category:
                new_category = f"inventory_{len(slots)}"
            if new_category in slot_by_category:
                slot_index, slot = slot_by_category[new_category]
            else:
                slot = slots.add()
                slot.source_game = getattr(rig_settings, "source_game", "w3")
                slot.category = new_category
                slot.resolved_repo_path = ""
                slot_index = len(slots) - 1
                slot_by_category[new_category] = (slot_index, slot)
                slot_was_created = True

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
            slot_by_category[fallback] = (slot_index, slot)

        slot.source_game = getattr(rig_settings, "source_game", "w3")
        if shared_inventory:
            slot.is_inventory = True

        # Determine item name / template
        if resolved_item_name:
            item_name = resolved_item_name
        else:
            item_name = _derive_template_from_item(item_raw) or str(item_raw)

        template = resolved_template
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
            if not template:
                template = _derive_template_from_item(item_raw)
        if not template:
            template = item_name

        slot.item_name = item_name
        slot.equip_template = template

        attrs = item_attributes.get(item_name, {})
        if not attrs and item_raw:
            attrs = item_attributes.get(str(item_raw), {})
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
                    existing_slot.category: (idx, existing_slot)
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
                slot_by_category = {existing_slot.category: (idx, existing_slot) for idx, existing_slot in enumerate(slots) if existing_slot.category}
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

    if slots_to_load:
        load_equipment_items_batch(
            context,
            armature,
            slots_to_load,
            rig_settings,
            prepared_context=prepared,
            post_refresh_variants=post_refresh,
            mount_mode="equip",
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
    camera_data:bpy.types.Camera = camera.data
    camera_data.lens_unit = 'FOV' #convert witcher FOV angle to mm, angle cannot be driven it uses mm lens prop
    camera_data.sensor_fit = 'VERTICAL'
    camera_data.sensor_height = 43.266615300557

    driver_curve = camera_data.driver_add("lens")
    driver = driver_curve.driver
    channel = name
    driver.expression = f'43.266615300557 / ( 2 * tan( pi * {channel} / 360.0 ) )' #channel
    var = driver.variables.get(channel)
    if var is None:
        var = driver.variables.new()
    var.type = "SINGLE_PROP"
    var.name = channel
    armobj.pose.bones["Camera_Node"]["%s" % channel] = 35
    target = var.targets[0]
    target.id_type = "OBJECT"
    target.data_path = 'pose.bones["Camera_Node"]["%s"]' % channel #'["%s"]' % channel
    target.id = armobj
    armobj.update_tag()

def do_constraints(constrains, objdict, meshdict, HardAttachments, group_parent=None):
    """
    Process constraints and hard attachments, applying constraints between objects and setting up parenting.

    Parameters:
        constrains (list): List of tuples [(parent_obj_name, child_obj_name), ...]
        objdict (dict): Dictionary mapping object names to Blender objects
        meshdict (dict): Dictionary mapping mesh names to Blender mesh objects
        HardAttachments (list): List of constraints with 'parent_name', 'parentSlotName', 'child_name', 'relativeTransform'
        group_parent (str, optional): Optional parent object name for grouping

    Returns:
        list: List of objects that are parented to the group_parent
    """
    return_objs = process_constraints(constrains, objdict, group_parent)
    process_hard_attachments(HardAttachments, objdict, meshdict)
    return return_objs


def _set_parent_keep_world(obj, parent_obj, *, parent_type='OBJECT', parent_bone=""):
    if obj is None:
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


def process_constraints(constrains, objdict, group_parent=None):
    """
    Process and apply constraints between parent and child objects.

    Parameters:
        constrains (list): List of tuples [(parent_obj_name, child_obj_name), ...]
        objdict (dict): Dictionary mapping object names to Blender objects
        group_parent (str, optional): Optional parent object name for grouping

    Returns:
        list: List of objects that are parented to the group_parent
    """
    return_objs = []
    for parent_obj_name, child_obj_name in constrains:
        if parent_obj_name in objdict and child_obj_name in objdict:
            parent_obj = objdict[parent_obj_name]
            child_obj = objdict[child_obj_name]
            constrain_util.CreateConstraints2(parent_obj, child_obj)

            # If the object is a Cloth group, attach the group to the appearance instead.
            if child_obj.parent and ":_grp" in child_obj.parent.name:
                _set_parent_keep_world(child_obj.parent, parent_obj)
                if group_parent and parent_obj_name == group_parent:
                    return_objs.append(child_obj.parent)
            else:
                _set_parent_keep_world(child_obj, parent_obj)
                if group_parent and parent_obj_name == group_parent:
                    return_objs.append(child_obj)
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
        parent_arm_name = constraint['parent_name']
        p_bone_name = constraint['parentSlotName']
        child_name = constraint['child_name']
        relativeTransform = constraint['relativeTransform']

        special_names = ["CAnimated", "CCameraComponent", "CAnimDangleConstraint"]
        if any(substring in child_name for substring in special_names):
            process_special_attachment(constraint, objdict)
        else:
            process_regular_attachment(constraint, objdict, meshdict)


def process_special_attachment(constraint, objdict):
    """
    Process special attachments like animated components or cameras.

    Parameters:
        constraint (dict): Constraint information
        objdict (dict): Dictionary mapping object names to Blender objects
    """
    parent_arm_name = constraint['parent_name']
    p_bone_name = constraint['parentSlotName']
    child_name = constraint['child_name']
    relativeTransform = constraint['relativeTransform']

    if parent_arm_name in objdict and child_name in objdict:
        parent_arm = objdict[parent_arm_name]
        target_object = objdict[child_name]
        rig_settings = getattr(parent_arm.data, "witcherui_RigSettings", None)
        use_rot90 = get_do_fix_tail(bpy.context)
        if rig_settings is not None:
            if hasattr(rig_settings, "rot90_compensate"):
                use_rot90 = bool(rig_settings.rot90_compensate)
            elif hasattr(rig_settings, "rot90_imported"):
                use_rot90 = bool(rig_settings.rot90_imported)

        # Determine parent bone.
        # Prefer full-rig matching only for clear duplicate skeletons; otherwise
        # only bind when parentSlotName is explicitly set in the CHardAttachment.
        p_bone = None
        if p_bone_name:
            p_bone = parent_arm.pose.bones.get(p_bone_name)

        can_match_full_armature = (
            target_object.type == 'ARMATURE'
            and constrain_util.should_auto_align_armatures(parent_arm, target_object)
        )

        if can_match_full_armature:
            _set_parent_keep_world(target_object, parent_arm)
            target_object["w2_special_attachment"] = True
            target_object["w2_special_parent_arm"] = parent_arm.name
            target_object["w2_special_parent_bone"] = p_bone.name if p_bone else ""
            target_object["w2_special_attachment_mode"] = "matched_armature"
            target_object.parent_type = "OBJECT"
            target_object.parent_bone = ""
            constrain_util.CreateConstraints2(parent_arm, target_object)

        elif p_bone is not None:
            if target_object.type == 'ARMATURE':
                _set_parent_keep_world(target_object, parent_arm)
            else:
                target_object.parent = parent_arm
            target_object["w2_special_attachment"] = True
            target_object["w2_special_parent_arm"] = parent_arm.name
            target_object["w2_special_parent_bone"] = p_bone.name
            target_object["w2_special_attachment_mode"] = "root_copy"
            # Keep one consistent binding mode for special attachment armatures:
            # always object parent + root COPY_TRANSFORMS, regardless of Rot90 state.
            if target_object.pose:
                tgt_child_bone = target_object.pose.bones[0]
                for c in list(tgt_child_bone.constraints):
                    if c.type == 'COPY_TRANSFORMS' and c.target == parent_arm:
                        tgt_child_bone.constraints.remove(c)
                copy_transform = tgt_child_bone.constraints.new('COPY_TRANSFORMS')
                copy_transform.name = f"{p_bone.name} to {tgt_child_bone.name}"
                copy_transform.target = parent_arm
                copy_transform.subtarget = p_bone.name
                target_object.parent_type = "OBJECT"
                target_object.parent_bone = ""
            else:
                # Non-armature objects (e.g. camera object) still use bone-parenting.
                target_object.parent_type = "BONE"
                target_object.parent_bone = p_bone.name

            if "CCameraComponent" in child_name:
                create_camera_drivers(parent_arm, target_object, "hctFOV")

        # Apply relativeTransform if present
        if relativeTransform:
            rt = _coerce_engine_transform(relativeTransform)
            if rt is not None:
                set_blender_object_transform(target_object, rt, rotate_180=False)

        # Camera components need a rot90-facing offset when the rig is rotated
        if "CCameraComponent" in child_name and use_rot90:
            target_object.rotation_euler[2] += math.radians(90)


def process_regular_attachment(constraint, objdict, meshdict):
    """
    Process regular attachments by creating an empty object and setting up parenting.

    Parameters:
        constraint (dict): Constraint information
        objdict (dict): Dictionary mapping object names to Blender objects
        meshdict (dict): Dictionary mapping mesh names to Blender mesh objects
    """
    parent_arm_name = constraint['parent_name']
    p_bone_name = constraint['parentSlotName']
    child_name = constraint['child_name']
    relativeTransform = constraint['relativeTransform']
    rt = _coerce_engine_transform(relativeTransform) if relativeTransform else None

    bpy.ops.object.empty_add(type="PLAIN_AXES", radius=1)
    target_transform = bpy.context.object
    target_transform.name = "CHardAttachment"

    target_name = f"{child_name}_lod0"
    if parent_arm_name in objdict and target_name in meshdict:
        target_mesh_obj = meshdict[target_name]
        target_mesh_obj.parent = target_transform

        parent_arm = objdict[parent_arm_name]
        p_bone = parent_arm.pose.bones.get(p_bone_name)
        if p_bone is not None:
            target_transform.parent = parent_arm
            target_transform.parent_type = "BONE"
            target_transform.parent_bone = p_bone_name

            use_rot90 = get_do_fix_tail(bpy.context)
            rig_settings = getattr(parent_arm.data, "witcherui_RigSettings", None)
            if rig_settings is not None:
                if hasattr(rig_settings, "rot90_compensate"):
                    use_rot90 = bool(rig_settings.rot90_compensate)
                elif hasattr(rig_settings, "rot90_imported"):
                    use_rot90 = bool(rig_settings.rot90_imported)
            if rt is None and use_rot90:
                target_transform.rotation_euler[2] += radians(90)

    # Apply relativeTransform if present
    if rt is not None:
        set_blender_object_transform(target_transform, rt, rotate_180=False)

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
                 target_collection=None):
    if morphs_todo is None:
        morphs_todo = []
    if target_collection is None:
        target_collection = _get_import_target_collection(bpy.context)
    selected_appearance_name = str(_get_entry_attr(selectedAppearance, "name", "") or "")
    coloring_entry_lookup = _build_coloring_entry_lookup(
        getattr(entity, "coloringEntries", None),
        selected_appearance_name,
    )
    direct_entity_path = _normalize_repo_path(direct_entity_path)
    source_game = "w2" if str(source_game or "").strip().lower() == "w2" else ("w3" if source_game else "")
    
    def get_chunk_namespace(chunk):
        return f"{ent_namespace}{chunk['type']}{i}{chunk['chunkIndex']}"
    
    def get_ns_for_chunk(chunk_index, chunks):
        for chunk in chunks:
            if chunk['chunkIndex'] == chunk_index:
                if chunk['type'] == "CAnimDangleComponent":
                    return GetChunkNS(chunk['constraint'], chunks, i)
                return f"{chunk['type']}{i}{chunk_index}"
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
            if source_game:
                obj['witcher_source_game'] = source_game

    has_moving_agent = False
    
    # Handle base constraints first
    if bind_root_chunks_to_entity:
        for chunk in cur_chunks:
            chunk_ns = get_chunk_namespace(chunk)
            if not isChildNode(chunk['chunkIndex'], cur_chunks):
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

        # Import meshes
        if "mesh" in chunk:
            mesh_path = chunk['mesh']
            if not is_valid_mesh_path(mesh_path):
                log.warning(f"Skipping chunk with invalid mesh path ({chunk['type']} #{chunk['chunkIndex']}): {mesh_path}")
            else:
                component_name = _get_chunk_component_name(chunk)
                meshes, armatures = fbx_util.import_model(repo_file(mesh_path, entity.version), 
                                                     f"{chunk['type']}{i}{chunk['chunkIndex']}", 
                                                     entity.name)
             
                if component_name:
                    for mesh in meshes:
                        mesh['witcher_name'] = component_name
                if selected_appearance_name and component_name:
                    _apply_coloring_lookup_to_objects(meshes, coloring_entry_lookup)

                # Store objects directly while adding metadata
                for arm in armatures:
                    add_chunk_metadata(arm, chunk, mesh_path, component_name=component_name)
                    objdict[chunk_ns] = arm
                    
                for mesh in meshes:
                    add_chunk_metadata(mesh, chunk, mesh_path, component_name=component_name)
                    if mesh.name[-5:-1] == "_lod":
                        meshdict[chunk_ns + mesh.name[-5:]] = mesh
                    else:
                        meshdict[chunk_ns] = mesh
                        
                    if hide_shadowmesh:
                        chunk_name = chunk.get('name', '')
                        if any(_is_shadowmesh_name(candidate) for candidate in (mesh.name, chunk_name, mesh_path)):
                            _force_shadowmesh_hidden(mesh)

                _apply_chunk_transform_to_import_roots(chunk, armatures=armatures, meshes=meshes)

        # Handle cloth resources
        if "resource" in chunk and not import_redcloth_enabled:
            redcloth_resource = str(chunk.get("resource", "") or "")
            if redcloth_resource.lower().endswith(".redcloth"):
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

        if "resource" in chunk and import_redcloth_enabled:
            redcloth_resource = chunk["resource"]
            redcloth_mat_path = repo_file(redcloth_resource, entity.version)
            component_name = _get_chunk_component_name(chunk)
            owner_armature = objdict.get(entity.name)
            redcloth_reuse_key = _make_redcloth_reuse_key(redcloth_resource, redcloth_mat_path)
            cloth_arma = _find_reusable_redcloth_armature(owner_armature, redcloth_reuse_key)
            if cloth_arma is not None:
                log.info("Reusing redcloth import for %s", redcloth_resource)
            else:
                apx_info = resolve_redcloth_apx(bpy.context, redcloth_resource, loadmods=False)
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
                        cloth_arma = cloth_util.importCloth(
                            False,
                            apx_path,
                            True,
                            False,
                            True,
                            redcloth_mat_path,
                            f"{chunk['type']}{i}{chunk['chunkIndex']}",
                            entity.name,
                        )
                        if target_collection is not None:
                            _activate_target_collection(bpy.context, target_collection)
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
            if cloth_arma is not None:
                clear_external_import_dependency_alert("redcloth")
                cloth_grp = None
                if cloth_arma.type == 'EMPTY':
                    cloth_grp = cloth_arma
                    for child in cloth_arma.children:
                        if child.type == 'ARMATURE':
                            cloth_arma = child
                            break
                _tag_redcloth_for_reuse(cloth_arma, redcloth_reuse_key, redcloth_resource, redcloth_mat_path)
                add_chunk_metadata(cloth_arma, chunk, chunk['resource'], component_name=component_name)
                objdict[chunk_ns] = cloth_arma
                if cloth_grp is not None:
                    objdict[chunk_ns + ":_grp"] = cloth_grp
                if not any(c[1] == chunk_ns for c in constrains):
                    constrains.append([entity.name, chunk_ns])

                cloth_meshes = _collect_redcloth_meshes(cloth_arma)
                if component_name:
                    for mesh in cloth_meshes:
                        mesh['witcher_name'] = component_name
                for mesh in cloth_meshes:
                    add_chunk_metadata(mesh, chunk, chunk['resource'], component_name=component_name)
                if selected_appearance_name and component_name:
                    _apply_coloring_lookup_to_objects(cloth_meshes, coloring_entry_lookup)

        # Handle morphs
        if "morphComponentId" in chunk:
            morph_source_meshes, morph_source_arms = fbx_util.import_model(
                repo_file(chunk['morphSource'], entity.version), 
                f"{chunk['type']}{i}{chunk['chunkIndex']}", 
                entity.name
            )
            morph_target_meshes, morph_target_arms = fbx_util.import_model(
                repo_file(chunk['morphTarget'], entity.version),
                f"{chunk['type']}{i}{chunk['chunkIndex']}_morphTarget",
                entity.name
            )
            
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
                moving_agent = import_rig.import_w3_rig(
                    repo_file(chunk['skeleton'], entity.version),
                    chunk_ns
                )
                add_chunk_metadata(moving_agent, chunk, chunk['skeleton'])
                objdict[chunk_ns] = moving_agent
                root_skeleton = moving_agent
                has_moving_agent = True
                _apply_chunk_transform_to_import_roots(chunk, armatures=[moving_agent])
        elif "skeleton" in chunk and chunk['skeleton'] is not None:
            root_bone = import_rig.import_w3_rig(
                repo_file(chunk['skeleton'], entity.version),
                chunk_ns
            )
            add_chunk_metadata(root_bone, chunk, chunk['skeleton'])
            objdict[chunk_ns] = root_bone
            if not has_moving_agent:
                root_skeleton = root_bone
            _apply_chunk_transform_to_import_roots(chunk, armatures=[root_bone])

        # Handle dynamic rigs
        if "dyng" in chunk and chunk['dyng'] is not None:
            root_bone = import_rig.import_w3_rig(
                repo_file(chunk['dyng'], entity.version),
                chunk_ns
            )
            add_chunk_metadata(root_bone, chunk, chunk['dyng'])
            objdict[chunk_ns] = root_bone
            _apply_chunk_transform_to_import_roots(chunk, armatures=[root_bone])

        # Handle mimic face
        if "mimicFace" in chunk:
            faceData = import_rig.loadFaceFile(repo_file(chunk['mimicFace'], entity.version))
            if faceData is None:
                log.warning("Failed to load mimic face: %s", chunk['mimicFace'])
            else:
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

        if chunk['type'] in {"CPointLightComponent", "CSpotLightComponent"}:
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
                HardAttachments.append(chunk)

    return constrains, objdict, meshdict, HardAttachments, root_skeleton, morphs_todo

from mathutils import Euler, Matrix
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


def _light_color_channel(color_value, channel_name, default=255.0):
    if color_value is None:
        return default
    if isinstance(color_value, dict):
        return _coerce_real(color_value.get(channel_name), default)
    if hasattr(color_value, "get"):
        try:
            return _coerce_real(color_value.get(channel_name), default)
        except Exception:
            pass
    return _coerce_real(getattr(color_value, channel_name, None), default)


def _import_light_component(chunk):
    chunk_type = str(chunk.get("type", "") or "").strip()
    if chunk_type not in {"CPointLightComponent", "CSpotLightComponent"}:
        return None

    light_kind = 'SPOT' if chunk_type == "CSpotLightComponent" else 'POINT'
    bpy.ops.object.light_add(type=light_kind, radius=1, align='WORLD', location=(0, 0, 0), scale=(1, 1, 1))
    light_obj = bpy.context.selected_objects[:][0]
    light_name = str(chunk.get("name", "") or "").strip()
    if light_name:
        light_obj.name = light_name
        light_obj.data.name = light_name

    brightness = _coerce_real(chunk.get("brightness"), 0.0)
    light_obj.data.energy = brightness * (3.0 if light_kind == 'SPOT' else 10.0)

    color_value = chunk.get("color")
    light_obj.data.color[0] = _light_color_channel(color_value, "Red") / 255.0
    light_obj.data.color[1] = _light_color_channel(color_value, "Green") / 255.0
    light_obj.data.color[2] = _light_color_channel(color_value, "Blue") / 255.0

    radius = _coerce_real(chunk.get("radius"), 0.0)
    if radius > 0.0:
        light_obj.data.shadow_soft_size = radius

    rt = _coerce_engine_transform(chunk.get("transform"))
    if rt is not None:
        set_blender_object_transform(light_obj, rt, rotate_180=False)

    if light_kind == 'SPOT':
        light_obj.rotation_euler.x += 1.5708
        light_obj.data.spot_blend = 0.0
        outer_angle = _coerce_real(chunk.get("outerAngle"), 0.0)
        if outer_angle > 0.0:
            light_obj.data.spot_size = outer_angle

    return light_obj


def set_empty_bone_offset(empty_obj, armature_obj, bone_name, transform, rotate_180=False, rotate_90=False, rotate_90_dir=1):
    """
    Sets the relative position of an empty object based on the EngineTransform,
    offsetting it from the target bone if boneName is provided.
    If transform is None, the empty is constrained to the bone or component with no offset.
    If bone_name is None, the empty is constrained to the armature object.
    
    Updated to use COPY_TRANSFORMS for consistency with equipment mounting system.
    Rot90 compensation must preserve the slot's world placement when the bone
    basis is rotated for Blender display.
    """
    # Check if bone exists
    has_bone = bone_name and bone_name in armature_obj.pose.bones

    # Remove existing slot constraints to avoid duplicates
    for c in list(empty_obj.constraints):
        if c.type in {'COPY_TRANSFORMS', 'CHILD_OF'}:
            empty_obj.constraints.remove(c)

    # Use COPY_TRANSFORMS for consistency with equipment mounting
    constraint = empty_obj.constraints.new(type='COPY_TRANSFORMS')
    constraint.name = "W2_SLOT"
    constraint.target = armature_obj
    constraint.subtarget = bone_name if has_bone else ''
    # Keep one consistent slot binding mode regardless of Rot90 state so
    # toggling Rot90 only changes local orientation compensation, not placement.
    constraint.owner_space = 'LOCAL'
    constraint.target_space = 'POSE'
    constraint.mix_mode = 'BEFORE'
    
    # Now set the empty's local transform for offset
    if transform is not None:
        # Create rotation matrix based on yaw, pitch, roll from transform
        x = radians(_coerce_real(transform.get('Yaw', 0.0), 0.0))
        y = radians(_coerce_real(transform.get('Pitch', 0.0), 0.0))
        z = radians(_coerce_real(transform.get('Roll', 0.0), 0.0))
        rotation_matrix = Euler((x, y, z), 'YXZ').to_matrix().to_4x4()

        # Adjust for 180-degree rotation if specified
        if rotate_180:
            rotation_matrix[0][0], rotation_matrix[0][1], rotation_matrix[0][2] = -rotation_matrix[0][0], -rotation_matrix[0][1], rotation_matrix[0][2]
            rotation_matrix[1][0], rotation_matrix[1][1], rotation_matrix[1][2] = -rotation_matrix[1][0], -rotation_matrix[1][1], rotation_matrix[1][2]
            rotation_matrix[2][0], rotation_matrix[2][1], rotation_matrix[2][2] = -rotation_matrix[2][0], -rotation_matrix[2][1], rotation_matrix[2][2]

        # Apply position based on transform data
        location = Matrix.Translation((
            _coerce_real(transform.get('X', 0.0), 0.0),
            _coerce_real(transform.get('Y', 0.0), 0.0),
            _coerce_real(transform.get('Z', 0.0), 0.0),
        ))

        # Apply scale based on transform data
        scale_x = _coerce_real(transform.get('Scale_x', 1.0), 1.0)
        scale_y = _coerce_real(transform.get('Scale_y', 1.0), 1.0)
        scale_z = _coerce_real(transform.get('Scale_z', 1.0), 1.0)
        scale_matrix = Matrix.Scale(scale_x, 4, (1, 0, 0)) @ \
                       Matrix.Scale(scale_y, 4, (0, 1, 0)) @ \
                       Matrix.Scale(scale_z, 4, (0, 0, 1))

        # Combine and set as local transform
        transform_matrix = location @ rotation_matrix @ scale_matrix

        # Convert the authored slot transform into the rotated bone basis.
        # Applying the correction on the left preserves world placement for
        # translated slots when Rot90 changes the bone's local axes.
        if rotate_90:
            rot90 = Matrix.Rotation(radians(90 * rotate_90_dir), 4, 'Z')
            transform_matrix = rot90 @ transform_matrix

        empty_obj.matrix_local = transform_matrix
    else:
        # No offset - place at origin (constraint will position it)
        if rotate_90:
            empty_obj.matrix_local = Matrix.Rotation(radians(90 * rotate_90_dir), 4, 'Z')
        else:
            empty_obj.matrix_local = Matrix.Identity(4)



def import_MovingPhysicalAgentComponent(entity, parent_transform = None, direct_entity_path="", source_game="", target_collection=None):
    #entity = fixed_chunk_paths(entity, entity.version)
    ent_namespace = entity.name+":"
    if target_collection is None:
        target_collection = _get_import_target_collection(bpy.context)
    _activate_target_collection(bpy.context, target_collection)

    #OPTIONS
    hide_shadowmesh = True
    mimic_namespace = False
    root_skeleton = False
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
            direct_entity_path=direct_entity_path,
            source_game=source_game,
            target_collection=target_collection,
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
        rig_settings.entity_slots.clear()
        
        # Always create a per-import slot container so repeated imports of the
        # same entity stay isolated and never reuse another instance's slots.
        slots_parent_name = f"{entity.name}_slots" if entity.name else "entity_slots"
        slots_parent = bpy.data.objects.new(slots_parent_name, None)
        _link_object_to_collection(slots_parent, target_collection)
        slots_parent.empty_display_type = 'PLAIN_AXES'
        slots_parent.empty_display_size = 0.1
        slots_parent["witcher_slots_parent"] = True
        slots_parent["witcher_entity_name"] = entity.name or ""
        slots_parent["witcher_owner_armature"] = getattr(root_skeleton, "name_full", root_skeleton.name)
        
        # Parent slots container to root skeleton
        slots_parent.parent = root_skeleton
        slots_parent.hide_set(True)  # Hidden by default

        # Process each slot
        for slot in entity.slots:
            this_slot = slot if isinstance(slot, w3_types.EntitySlot) else w3_types.EntitySlot(True, slot)
            componentName = this_slot.componentName

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
                use_rot90 = getattr(rig_settings, "rot90_compensate", False)
                rot90_dir = 1
            set_empty_bone_offset(empty_obj, armature_obj, bone_name, transform,
                                  rotate_90=use_rot90, rotate_90_dir=rot90_dir)

            # Hide by default
            empty_obj.hide_set(True)

    #objdict.update({entity.name:root_skeleton}) # TODO this shouldn't be required if it reads the entity constraints full
    
    do_constraints(constrains, objdict, meshdict, HardAttachments)

    if parent_transform:
        if root_skeleton:
            root_skeleton.parent = parent_transform
        for mesh in list(objdict.values()) + list(meshdict.values()):
            if mesh and getattr(mesh, "parent", None) is None:
                mesh.parent = parent_transform
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
                                target_collection=None):
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
    elif hasattr(template_data, 'chunks'):
        template_chunks = getattr(template_data, 'chunks', None)
    if template_chunks:
        templateMesh = {'chunks': template_chunks}
    else:
        (templateMesh, entity_back) = LoadCEntityTemplateFile(templateFilename)
    
    cur_chunks = templateMesh['chunks']
    _arm_rig = getattr(getattr(base_animation_skeleton, "data", None), "witcherui_RigSettings", None)
    template_source_game = getattr(_arm_rig, "source_game", "w3") if _arm_rig else "w3"
    
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
        target_collection=target_collection,
    )
    if morphs_todo_accum is not None and local_morphs_todo:
        morphs_todo_accum.extend(local_morphs_todo)
    #TODO do_constraints after each chunk not all together
    apperance_level_objects = do_constraints(constrains, objdict, meshdict, HardAttachments, group_parent)

    # Propagate face skeleton from equipment template if not already set
    if 'mimicFaceFile' in base_animation_skeleton:
        rig_settings = base_animation_skeleton.data.witcherui_RigSettings
        if not (getattr(rig_settings, "main_face_skeleton", "") or "").strip():
            rig_settings.main_face_skeleton = base_animation_skeleton['mimicFaceFile']

    imported_objects = [
        obj for key, obj in objdict.items()
        if obj is not None and key != entity.name
    ]
    imported_objects.extend([obj for obj in meshdict.values() if obj is not None])
    grouped_root_objects = _get_import_root_objects(imported_objects)

    #if grouping the entire appreance together
    if group_parent:
        if bind_root_chunks_to_entity:
            group_objects = apperance_level_objects
        else:
            group_objects = list(grouped_root_objects)
            seen = {id(o) for o in group_objects}
            for obj in apperance_level_objects:
                if id(obj) not in seen:
                    group_objects.append(obj)
        for obj in group_objects:
            _set_parent_keep_world(obj, empty_transform)
        if use_app_drivers:
            # Drive only the imported template roots. Driving the shared appearance
            # empty causes later equipment/template imports to overwrite the empty's
            # visibility and hide reused shared templates (for example, same-head
            # appearance switches after loading equipment).
            for obj in group_objects:
                if obj is not None:
                    create_app_drivers(base_animation_skeleton, obj, appearance_indices)
    

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
               base_animation_skeleton):
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
    root_skeleton = False
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

    log.debug(selectedAppearance.name)
    rig_settings = base_animation_skeleton.data.witcherui_RigSettings

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
            before = set(bpy.data.objects)

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
                             target_collection=target_collection)

            new_objects = tag_new_objects_with_guid(before, guid, "witcher_template_guid")
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
        before = set(bpy.data.objects)

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
                         target_collection=target_collection)

        new_objects = tag_new_objects_with_guid(before, guid, "witcher_template_guid")
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
    source_game = getattr(rig_settings, "source_game", "w3")
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
        default_item = entry_data.get('defaultItemName', '') if isinstance(entry_data, dict) else getattr(entry_data, 'defaultItemName', '')
        if default_item is None:
            default_item = ''

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
        if default_item and default_item != 'None':
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
            attrs = item_attributes.get(default_item, {})
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
            # All equipment (W2 and W3) goes through the shared loader so
            # slot mounting, bound items (belt, scabbards) and attachment
            # type handling work consistently.
            deferred_default_slot_indices.append(slot_index)
            continue

    # Apply inventory-mounted items (overrides defaults when present).
    # Witcher 2 entities can also express equipped gear through inventory
    # definitions, so keep this shared path active for both games.
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

    # Witcher 3 defaults must be loaded through the shared equipment loader so
    # they get mounted to their equip_slot immediately on import.
    slots_to_reload = list(dict.fromkeys(deferred_default_slot_indices + persistent_slot_indices))
    if slots_to_reload:
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
            log.warning("Failed to load deferred Witcher 3 equipment: %s", e)

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


    # TODO ###############################################
    # TODO ############ FACE POSES #######################
    # TODO ###############################################
    #if grouping the entire appreance together
    # if group_parent:
    #     for obj in apperance_level_objects:
    #         obj.parent = empty_transform
    #     create_app_drivers(base_animation_skeleton, empty_transform)
    load_face_poses = False
    if load_face_poses:
        mimicPoses = import_rig.import_w3_mimicPoses(faceData.mimicPoses, faceData.mimicSkeleton, actor=entity.name, mimic_namespace=mimic_namespace)


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

def import_from_list_item(context, item):
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
                import_app(context, app, entity, base_animation_skeleton)
                _focus_main_armature(context, base_animation_skeleton)
                #bpy.ops.witcher.load_face_morphs()
    else:
        log.warning("import_from_list_item: no target armature selected.")
