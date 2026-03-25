import bpy
import os
import json
import uuid
import logging
import re
import xml.etree.ElementTree as ET
from mathutils import Matrix
from types import SimpleNamespace

log = logging.getLogger(__name__)
from ..CR2W.witcher_cache.CacheController import CacheController
from ..CR2W.witcher_cache.Bundles import LoadBundleManager
from ..CR2W.witcher_cache.Bundles.BundleItem import BundleItem
from ..CR2W.common_blender import repo_file, mod_loading_context
from ..importers import import_entity
from ..importers.import_anims import load_idle_animation_for_armature as _load_idle_anim
from ..CR2W.dc_entity import LoadCEntityTemplateFile  # Import the function as per your setup
from ..extension_paths import get_cache_root, get_dev_override
from .. import (
    get_all_addon_prefs,
    get_uncook_path,
    get_do_import_redcloth,
    get_w2_unbundle_path,
    get_witcher2_game_path,
)
from pathlib import Path
from .. import get_rig_rot90_enabled
from .armature_context import (
    get_main_armature_and_rig_settings,
)

# Category cache file path (extension-safe user cache)
_CATEGORY_CACHE_FILE = Path(get_cache_root(create=True)) / "equipment_categories.json"
_UNCOOK_ITEM_ENT_INDEX = {}
_LAST_EQUIPMENT_LOAD_FAILURES = {}
_LOADED_EQUIPMENT_XML_DIRS = set()
_OPERATOR_ENUM_CACHE = {}
_W2_CATEGORY_CACHE_LOADED = False
_W2_CATEGORY_ITEMS = {}
_W2_ITEM_ATTRIBUTES = {}
_ENTITY_APPEARANCE_CACHE = {}
_EQUIPMENT_ENTITY_CACHE = {}
_TEMPLATE_PATH_RESOLVE_CACHE = {}  # (template_key, roots_tuple) -> (repo_path, export_path)
_XML_DECL_RE = re.compile(r'^\s*<\?xml[^>]*\?>', re.IGNORECASE)
_XML_DECL_ENCODING_BYTES_RE = re.compile(br'<\?xml[^>]*encoding=["\']([^"\']+)["\']', re.IGNORECASE)


def _clear_cache_if_oversized(cache, max_entries=64):
    if len(cache) > max_entries:
        cache.clear()


def _normalize_source_game(source_game):
    return "w2" if str(source_game or "").strip().lower() == "w2" else "w3"


def _get_category_cache_file(source_game="w3"):
    source_game = _normalize_source_game(source_game)
    if source_game == "w2":
        return Path(get_cache_root(create=True)) / "equipment_categories_w2.json"
    return _CATEGORY_CACHE_FILE


def _catalog_key_for_source_game(source_game="w3"):
    source_game = _normalize_source_game(source_game)
    if source_game == "w2":
        return ("_W2_CATEGORY_ITEMS", "_W2_ITEM_ATTRIBUTES")
    return ("category_items", "item_attributes")


def _get_equipment_catalog(source_game="w3"):
    source_game = _normalize_source_game(source_game)
    if source_game == "w2":
        return (_W2_CATEGORY_ITEMS, _W2_ITEM_ATTRIBUTES)
    return (EquipmentDefinitionEntry.category_items, EquipmentDefinitionEntry.item_attributes)


def get_equipment_source_game_for_search_roots(search_roots=None):
    return "w2" if _is_w2_search(search_roots) else "w3"


def get_equipment_catalog_for_search_roots(search_roots=None):
    return _get_equipment_catalog(get_equipment_source_game_for_search_roots(search_roots))


def _get_active_equipment_catalog(context):
    return _get_equipment_catalog(_get_temp_source_game(context))


def _save_category_cache(source_game="w3"):
    """Save loaded categories to the appropriate cache file."""
    try:
        category_items, item_attributes = _get_equipment_catalog(source_game)
        cache_file = _get_category_cache_file(source_game)
        cache_data = {
            'category_items': category_items,
            'item_attributes': item_attributes,
        }
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_file, 'w', encoding='utf-8') as f:
            json.dump(cache_data, f, indent=2)
        log.debug("Saved %s category cache to %s", _normalize_source_game(source_game), cache_file)
    except Exception as e:
        log.warning("Failed to save category cache: %s", e)


def _load_category_cache(source_game="w3"):
    """Load categories from the appropriate cache file if it exists."""
    source_game = _normalize_source_game(source_game)
    cache_file = _get_category_cache_file(source_game)
    if not cache_file.exists():
        return False
    try:
        with open(cache_file, 'r', encoding='utf-8') as f:
            cache_data = json.load(f)

        category_items = cache_data.get('category_items', {})
        item_attributes = cache_data.get('item_attributes', {})
        target_categories, target_attributes = _get_equipment_catalog(source_game)

        for category, items in category_items.items():
            filtered_items = []
            for item in items:
                item_name = item[0] if item else ""
                attrs = item_attributes.get(item_name, {}) if item_name else {}
                item_source_game = _normalize_source_game(attrs.get("source_game", source_game))
                if item_source_game != source_game:
                    continue
                filtered_items.append(item)
            if not filtered_items:
                continue
            if category not in target_categories:
                target_categories[category] = filtered_items
            else:
                existing_set = set(tuple(item) for item in target_categories[category])
                for item in filtered_items:
                    if tuple(item) not in existing_set:
                        target_categories[category].append(item)
                        existing_set.add(tuple(item))

        for item_name, attrs in item_attributes.items():
            if not isinstance(attrs, dict):
                continue
            item_source_game = _normalize_source_game(attrs.get("source_game", source_game))
            if item_source_game != source_game:
                continue
            target_attributes[item_name] = attrs

        log.debug("Loaded %d %s categories from cache", len(category_items), source_game)
        return True
    except Exception as e:
        log.warning("Failed to load category cache: %s", e)
        return False


def _candidate_w2_items_dirs(search_roots=None):
    candidates = []
    seen = set()
    roots = _normalize_unique_roots(list(search_roots or []) + _get_w2_repo_roots())
    for root in roots:
        try:
            current = Path(root)
        except Exception:
            continue
        for parent in [current] + list(current.parents):
            for candidate in (
                parent,
                parent / "items",
                parent / "data" / "items",
            ):
                try:
                    candidate_path = str(candidate)
                    norm = os.path.normcase(os.path.normpath(candidate_path))
                except Exception:
                    continue
                if norm in seen:
                    continue
                seen.add(norm)
                if os.path.basename(norm).lower() != "items":
                    continue
                if os.path.isdir(candidate_path) and any(
                    name.lower().endswith(".xml") for name in os.listdir(candidate_path)
                ):
                    candidates.append(candidate_path)
    return candidates


def ensure_equipment_catalog_for_search_roots(search_roots=None):
    """Load additional XML catalogs required by the active repo roots."""
    if not _is_w2_search(search_roots):
        return False

    global _W2_CATEGORY_CACHE_LOADED
    if not _W2_CATEGORY_CACHE_LOADED:
        _load_category_cache("w2")
        _W2_CATEGORY_CACHE_LOADED = True

    w2_category_items, w2_item_attributes = _get_equipment_catalog("w2")
    merged_any = False
    for folder_path in _candidate_w2_items_dirs(search_roots):
        try:
            norm = os.path.normcase(os.path.normpath(folder_path))
        except Exception:
            norm = folder_path.lower()
        if norm in _LOADED_EQUIPMENT_XML_DIRS:
            continue

        _, category_items_from_xml, item_attributes_from_xml = extract_categories_from_xml(folder_path)
        if category_items_from_xml or item_attributes_from_xml:
            _merge_equipment_xml_data(
                w2_category_items,
                w2_item_attributes,
                category_items_from_xml,
                item_attributes_from_xml,
            )
            merged_any = True
            log.info(
                "Loaded Witcher 2 equipment XMLs from '%s' (%d cats, %d items)",
                folder_path,
                len(category_items_from_xml),
                len(item_attributes_from_xml),
            )
        _LOADED_EQUIPMENT_XML_DIRS.add(norm)

    if merged_any:
        _save_category_cache("w2")
    return merged_any


def _request_sync_templates():
    """No-op: automatic sync disabled to avoid Blender UI performance issues."""
    return


def _cache_operator_enum_items(cache_key, items):
    stable_items = []
    for item in items or [("None", "None", "")]:
        identifier = str(item[0] or "None")
        label = str(item[1] or identifier)
        description = str(item[2] or "")
        stable_items.append((identifier, label, description))
    _OPERATOR_ENUM_CACHE[cache_key] = stable_items
    return stable_items


def _set_last_equipment_load_failure(armature, slot_index, reason):
    key = (getattr(armature, "name_full", getattr(armature, "name", "")), int(slot_index))
    if reason:
        _LAST_EQUIPMENT_LOAD_FAILURES[key] = str(reason)
    else:
        _LAST_EQUIPMENT_LOAD_FAILURES.pop(key, None)


def _get_last_equipment_load_failure(armature, slot_index):
    key = (getattr(armature, "name_full", getattr(armature, "name", "")), int(slot_index))
    return _LAST_EQUIPMENT_LOAD_FAILURES.get(key, "")


def _w2ent_basename_key(template_name):
    rel_name = str(template_name or "").replace("/", "\\").lstrip("\\")
    if not rel_name:
        return ""
    base_name = rel_name.rsplit("\\", 1)[-1]
    if not base_name.lower().endswith(".w2ent"):
        base_name += ".w2ent"
    return base_name.lower()


def _normalize_unique_roots(roots):
    out = []
    seen = set()
    for root in roots or []:
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


def _source_game_from_xml_path(path_value):
    lowered = str(path_value or "").replace("/", "\\").lower()
    if "\\gameplay\\items" in lowered:
        return "w3"
    if lowered.endswith("\\gameplay\\items"):
        return "w3"
    if "\\items\\" in lowered or lowered.endswith("\\items"):
        return "w2"
    return ""


def _norm_root_path(path):
    if not path:
        return ""
    try:
        return os.path.normcase(os.path.normpath(str(path)))
    except Exception:
        return str(path).lower()


def _get_w2_repo_roots():
    roots = []
    try:
        w2_unbundle = (get_w2_unbundle_path(bpy.context) or "").strip()
    except Exception:
        w2_unbundle = ""
    if w2_unbundle:
        roots.append(w2_unbundle)
    try:
        w2_game = (get_witcher2_game_path(bpy.context) or "").strip()
    except Exception:
        w2_game = ""
    if w2_game:
        roots.append(os.path.join(w2_game, "data"))
        roots.append(w2_game)
    return _normalize_unique_roots(roots)


def _is_w2_search(search_roots):
    norm_search_roots = [_norm_root_path(root) for root in (search_roots or []) if root]
    if not norm_search_roots:
        return False
    for root in _get_w2_repo_roots():
        norm_root = _norm_root_path(root)
        if not norm_root:
            continue
        prefix = norm_root + os.sep
        for candidate in norm_search_roots:
            if candidate == norm_root or candidate.startswith(prefix):
                return True
    return False


def _get_safe_context_armature_and_rig_settings(context):
    if context is None:
        return None, None

    candidates = []
    for attr in ("object", "active_object"):
        obj = getattr(context, attr, None)
        if obj is not None:
            candidates.append(obj)

    scene = getattr(context, "scene", None)
    if scene is not None and hasattr(scene, "witcher_main_armature"):
        armature = getattr(scene, "witcher_main_armature", None)
        if armature is not None:
            candidates.append(armature)

    seen = set()
    for obj in candidates:
        try:
            obj_ptr = obj.as_pointer()
        except Exception:
            obj_ptr = id(obj)
        if obj_ptr in seen:
            continue
        seen.add(obj_ptr)
        try:
            if obj and obj.type == "ARMATURE":
                return obj, getattr(obj.data, "witcherui_RigSettings", None)
        except Exception:
            continue
    return None, None


def _get_active_equipment_source_roots(context):
    armature, rig_settings = _get_safe_context_armature_and_rig_settings(context)

    roots = []
    if armature:
        try:
            roots = import_entity._get_armature_source_roots(armature)
        except Exception:
            roots = []

    if not roots and rig_settings:
        repo_path_hint = getattr(rig_settings, "repo_path", "") or ""
        if repo_path_hint and os.path.isabs(repo_path_hint):
            try:
                roots = import_entity._build_entity_source_roots(repo_path_hint)
            except Exception:
                roots = []
    return _normalize_unique_roots(roots)


def _get_active_equipment_source_game(context):
    roots = _get_active_equipment_source_roots(context)
    if _is_w2_search(roots):
        return "w2"
    return "w3"


def _infer_source_game_from_rig_settings(rig_settings, armature=None):
    if rig_settings is not None:
        sg = str(getattr(rig_settings, "source_game", "") or "").strip().lower()
        if sg in {"w2", "w3"}:
            return sg
    return "w3"


def _get_temp_source_game(context):
    try:
        temp_data = context.window_manager.witcherui_temp_data
        value = str(getattr(temp_data, "equipment_source_game", "") or "").strip().lower()
        if value in {"w2", "w3"}:
            return value
    except Exception:
        pass
    return "w3"


def _get_temp_equipment_data(context):
    try:
        return context.window_manager.witcherui_temp_data
    except Exception:
        return None


def _make_temp_armature_key(armature):
    if armature is None:
        return ""
    try:
        arm_ptr = int(armature.as_pointer())
    except Exception:
        arm_ptr = id(armature)
    arm_name = getattr(armature, "name_full", getattr(armature, "name", ""))
    return f"{arm_name}|{arm_ptr}"


def _make_temp_entity_state_token(rig_settings):
    raw_json = getattr(rig_settings, "jsonData", "") or ""
    return f"{len(raw_json)}:{hash(raw_json)}"


def _set_temp_equipment_auto_apply_suspended(context, suspended):
    temp_data = _get_temp_equipment_data(context)
    if temp_data is None:
        return
    try:
        temp_data.suspend_auto_apply_updates = bool(suspended)
    except Exception:
        pass


def _is_temp_equipment_auto_apply_enabled(context):
    temp_data = _get_temp_equipment_data(context)
    if temp_data is None:
        return False
    if getattr(temp_data, "suspend_auto_apply_updates", False):
        return False
    return bool(getattr(temp_data, "auto_apply_equipment_selection", False))


def _get_catalog_for_rig_settings(rig_settings, armature=None):
    return _get_equipment_catalog(_infer_source_game_from_rig_settings(rig_settings, armature))


def _lookup_item_attributes(item_name, source_game="w3"):
    if not item_name:
        return {}
    _category_items, item_attributes = _get_equipment_catalog(source_game)
    attrs = item_attributes.get(item_name, {})
    if isinstance(attrs, dict):
        return attrs
    return {}


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
    return _normalize_unique_roots(parsed)


def _get_uncook_item_ent_index(uncook_root):
    norm_root = os.path.normcase(os.path.normpath(uncook_root)) if uncook_root else ""
    if not norm_root:
        return {}
    cached = _UNCOOK_ITEM_ENT_INDEX.get(norm_root)
    if cached is not None:
        return cached

    index = {}
    if uncook_root and os.path.isdir(uncook_root):
        for dirpath, dirnames, filenames in os.walk(uncook_root):
            dirnames.sort()
            filenames.sort()
            for filename in filenames:
                if not filename.lower().endswith(".w2ent"):
                    continue
                full_path = os.path.join(dirpath, filename)
                try:
                    rel_path = os.path.relpath(full_path, uncook_root).replace("/", "\\")
                except Exception:
                    continue
                key = filename.lower()
                existing = index.get(key)
                if existing is None:
                    # Prefer the first match; entity names are generally unique.
                    index[key] = rel_path
                    continue
                # If there is a collision, prefer an items path for equipment.
                existing_is_items = existing.lower().startswith("items\\")
                rel_is_items = rel_path.lower().startswith("items\\")
                if rel_is_items and not existing_is_items:
                    index[key] = rel_path

    _UNCOOK_ITEM_ENT_INDEX[norm_root] = index
    _clear_cache_if_oversized(_UNCOOK_ITEM_ENT_INDEX, max_entries=16)
    # A new index means newly extracted files may now be findable via the index
    # path, so invalidate the template path resolve cache.
    _TEMPLATE_PATH_RESOLVE_CACHE.clear()
    return index


def _remember_uncook_item_relpath(uncook_root, rel_path):
    if not uncook_root or not rel_path:
        return
    norm_root = os.path.normcase(os.path.normpath(uncook_root))
    root_index = _UNCOOK_ITEM_ENT_INDEX.get(norm_root)
    if root_index is None:
        return
    rel_name = str(rel_path).replace("/", "\\").lstrip("\\")
    key = _w2ent_basename_key(rel_name)
    if key:
        root_index.setdefault(key, rel_name)


def preserve_armature_focus(operation_func):
    """
    Decorator/context manager to preserve armature focus during operations.
    
    Usage:
        @preserve_armature_focus
        def my_operation(self, context):
            # ... your operation code ...
            return {'FINISHED'}
    
    Args:
        operation_func: Function that takes self and context and returns operator result
    Returns:
        Wrapped function that preserves armature focus
    """
    def wrapper(self, context, *args, **kwargs):
        # Store current selection and active object
        original_selection = list(context.selected_objects)
        original_active = context.active_object
        
        # Check if original active object is an armature
        was_armature_active = original_active and original_active.type == 'ARMATURE'
        
        # Execute the operation
        result = operation_func(self, context, *args, **kwargs)
        
        # Restore focus to armature if it was active before
        if was_armature_active and original_active:
            bpy.context.view_layer.objects.active = original_active
            
            # Clear current selection and restore original armature selection
            for obj in context.selected_objects:
                obj.select_set(False)
            
            original_active.select_set(True)
        
        return result
    
    return wrapper


# =============================================================================
# GUID Utility Functions (shared by Equipment and Template systems)
# =============================================================================

def generate_guid():
    """Generate a new UUID4 string."""
    return str(uuid.uuid4())

def _is_internal_inventory_group_object(obj):
    try:
        return bool(obj and obj.get("witcher_inventory_group"))
    except Exception:
        return False


def _clear_internal_inventory_group_state(obj):
    if obj is None:
        return
    for prop_name in ("witcher_equip_guid", "witcher_bound_parent_guid", "witcher_bound_item_name"):
        try:
            if prop_name in obj:
                del obj[prop_name]
        except Exception:
            pass


def tag_new_objects_with_guid(before_objects, guid, prop_name="witcher_equip_guid"):
    """Find objects added since `before_objects` snapshot and tag them with a GUID."""
    after_objects = set(bpy.data.objects)
    new_objects = after_objects - before_objects
    tagged_objects = set()
    for obj in new_objects:
        if _is_internal_inventory_group_object(obj):
            continue
        obj[prop_name] = guid
        tagged_objects.add(obj)
    return tagged_objects

def find_objects_by_guid(guid, prop_name="witcher_equip_guid"):
    """Find all scene objects with the given GUID."""
    return [
        obj for obj in bpy.data.objects
        if obj.get(prop_name) == guid and not _is_internal_inventory_group_object(obj)
    ]

def _object_parent_depth(obj):
    depth = 0
    current = getattr(obj, "parent", None)
    while current is not None:
        depth += 1
        current = getattr(current, "parent", None)
    return depth

def _clear_guid_metadata(obj, guid, prop_name):
    try:
        if obj.get(prop_name) == guid:
            del obj[prop_name]
    except Exception:
        pass
    if prop_name == "witcher_equip_guid":
        for extra_prop in ("witcher_bound_parent_guid", "witcher_bound_item_name"):
            try:
                if obj.get(extra_prop) and (extra_prop != "witcher_bound_parent_guid" or obj.get(extra_prop) == guid):
                    del obj[extra_prop]
            except Exception:
                pass

def _build_guid_index(prop_name="witcher_equip_guid"):
    """Build a dict mapping GUID -> list of objects by scanning bpy.data.objects once.

    Use this before a loop that would otherwise call find_objects_by_guid many
    times, and look up results via ``index.get(guid, [])`` instead.
    """
    index = {}
    for obj in bpy.data.objects:
        if _is_internal_inventory_group_object(obj):
            continue
        val = obj.get(prop_name)
        if val is not None:
            index.setdefault(val, []).append(obj)
    return index

def remove_objects_by_guid(guid, prop_name="witcher_equip_guid"):
    """Delete GUID-tagged scene objects without breaking external child hierarchies."""
    tagged_objects = set(find_objects_by_guid(guid, prop_name))
    if not tagged_objects:
        return 0

    pending = set(tagged_objects)
    removed = 0
    for obj in sorted(tagged_objects, key=_object_parent_depth, reverse=True):
        if obj not in pending:
            continue

        external_children = [child for child in obj.children if child not in pending]
        if external_children:
            # Preserve shared parents so unloading one slot cannot orphan another.
            _clear_guid_metadata(obj, guid, prop_name)
            pending.remove(obj)
            continue

        try:
            bpy.data.objects.remove(obj, do_unlink=True)
            removed += 1
        except Exception:
            pass
        finally:
            pending.discard(obj)
    return removed


def _collect_mount_roots(objects, ignored_objects=None):
    object_set = {obj for obj in (objects or []) if obj is not None}
    ignored_set = {obj for obj in (ignored_objects or []) if obj is not None}
    roots = []
    for obj in object_set:
        if obj in ignored_set or obj.get("witcher_mount_anchor") or _is_internal_inventory_group_object(obj):
            continue
        parent = getattr(obj, "parent", None)
        if (
            parent in ignored_set
            or _is_internal_inventory_group_object(parent)
            or parent is None
            or parent not in object_set
        ):
            roots.append(obj)
    return roots


def _mount_roots_are_animated(roots):
    return any(obj and obj.type == 'ARMATURE' for obj in (roots or []))


def _find_equipment_mount_anchor(guid, kind="main", bound_item_name=None):
    for obj in find_objects_by_guid(guid, "witcher_equip_guid"):
        if not obj or obj.type != 'EMPTY' or not obj.get("witcher_mount_anchor"):
            continue
        if str(obj.get("witcher_mount_kind", "") or "") != str(kind or ""):
            continue
        if bound_item_name is not None and str(obj.get("witcher_bound_item_name", "") or "") != str(bound_item_name or ""):
            continue
        return obj
    return None


def _ensure_equipment_mount_anchor(guid, kind="main", parent_hint=None, *, bound_parent_guid=None, bound_item_name=None):
    anchor = _find_equipment_mount_anchor(guid, kind=kind, bound_item_name=bound_item_name)
    if anchor is None:
        anchor = bpy.data.objects.new(f"{kind}_mount_anchor", None)
        bpy.context.collection.objects.link(anchor)
        if parent_hint is not None:
            try:
                anchor.matrix_world = parent_hint.matrix_world.copy()
            except Exception:
                pass
        anchor.empty_display_type = 'PLAIN_AXES'
        anchor.empty_display_size = 0.02
        if hasattr(anchor, "show_relationship_lines"):
            anchor.show_relationship_lines = False
    anchor["witcher_mount_anchor"] = True
    anchor["witcher_equip_guid"] = guid
    anchor["witcher_mount_kind"] = kind
    if bound_parent_guid:
        anchor["witcher_bound_parent_guid"] = bound_parent_guid
    if bound_item_name:
        anchor["witcher_bound_item_name"] = bound_item_name
    anchor.hide_set(True)
    anchor.hide_render = True
    return anchor


def _attach_roots_to_anchor_preserving_basis(roots, anchor, parent_hint=None):
    if not anchor:
        return
    reference_world = Matrix.Identity(4)
    if parent_hint is not None:
        try:
            reference_world = parent_hint.matrix_world.copy()
        except Exception:
            reference_world = Matrix.Identity(4)
    try:
        anchor.matrix_world = reference_world
    except Exception:
        pass

    try:
        anchor_world_inv = anchor.matrix_world.inverted()
    except Exception:
        anchor_world_inv = Matrix.Identity(4)

    for root in roots or []:
        if root is None:
            continue
        if parent_hint is not None and root.parent == parent_hint:
            local_basis = root.matrix_local.copy()
        else:
            try:
                local_basis = anchor_world_inv @ root.matrix_world.copy()
            except Exception:
                local_basis = root.matrix_world.copy()

        root.parent = anchor
        root.parent_type = 'OBJECT'
        try:
            root.matrix_parent_inverse = Matrix.Identity(4)
        except Exception:
            pass
        try:
            root.matrix_world = anchor.matrix_world @ local_basis
        except Exception:
            try:
                root.matrix_local = local_basis
            except Exception:
                pass


def _mount_anchor_to_slot(anchor, slot_empty, parent_armature=None):
    if not anchor or not slot_empty:
        return None
    mounted = mount_equipment_to_slot(anchor, slot_empty, parent_armature, snap=True)
    anchor["witcher_mount_target_type"] = "slot"
    anchor["witcher_mount_target_name"] = slot_empty.get("witcher_slot_name") or slot_empty.name
    return mounted


def _mount_anchor_to_bone(anchor, armature, bone_name):
    if not anchor or not armature or not bone_name:
        return None
    mounted = mount_equipment_to_bone(anchor, armature, bone_name, snap=True)
    anchor["witcher_mount_target_type"] = "bone"
    anchor["witcher_mount_target_name"] = bone_name
    return mounted


def _mount_anchor_to_target(anchor, target_info, fallback_armature=None):
    if not anchor or not target_info or not target_info.get("is_valid"):
        return None
    if target_info.get("target_type") == "slot" and target_info.get("slot_empty") is not None:
        return _mount_anchor_to_slot(
            anchor,
            target_info.get("slot_empty"),
            parent_armature=target_info.get("armature") or fallback_armature,
        )
    return _mount_anchor_to_bone(
        anchor,
        target_info.get("armature") or fallback_armature,
        target_info.get("bone_name"),
    )


def _mount_object_to_target(equipment_obj, target_info, fallback_armature=None):
    if not equipment_obj or not target_info or not target_info.get("is_valid"):
        return None
    if target_info.get("target_type") == "slot" and target_info.get("slot_empty") is not None:
        return mount_equipment_to_slot(
            equipment_obj,
            target_info.get("slot_empty"),
            target_info.get("armature") or fallback_armature,
            snap=False,
            preserve_local_offset=True,
        )
    return mount_equipment_to_bone(
        equipment_obj,
        target_info.get("armature") or fallback_armature,
        target_info.get("bone_name"),
        snap=False,
        preserve_local_offset=True,
    )


def _mount_animated_roots_with_anchor(roots, guid, kind, parent_hint, *, slot_empty=None, armature=None,
                                      bone_name=None, bound_parent_guid=None, bound_item_name=None):
    if not roots:
        return None
    anchor = _ensure_equipment_mount_anchor(
        guid,
        kind=kind,
        parent_hint=parent_hint,
        bound_parent_guid=bound_parent_guid,
        bound_item_name=bound_item_name,
    )
    _attach_roots_to_anchor_preserving_basis(roots, anchor, parent_hint=parent_hint)
    if slot_empty is not None:
        _mount_anchor_to_slot(anchor, slot_empty, parent_armature=armature)
    elif armature is not None and bone_name:
        _mount_anchor_to_bone(anchor, armature, bone_name)
    return anchor


def hide_objects_by_guid(guid, prop_name, hidden=True):
    """Toggle viewport visibility for all objects with the given GUID.
    
    Uses hide_set() for temporary UI visibility toggle (doesn't conflict with drivers).
    """
    objects = find_objects_by_guid(guid, prop_name)
    for obj in objects:
        if obj.get("witcher_mount_anchor"):
            obj.hide_set(True)
            continue
        obj.hide_set(hidden)
        # Note: Do NOT set hide_viewport directly - that conflicts with drivers
    return len(objects)

def update_rune_level(self, context):
    """Update rune_normal mapping node X Location based on rune level selection."""
    if not self.is_loaded or not self.equip_guid:
        return
    level_map = {'NONE': 0.0, '1': 0.25, '2': 0.50, '3': 0.75}
    x_loc = level_map.get(self.rune_level, 0.0)
    objects = find_objects_by_guid(self.equip_guid)
    for obj in objects:
        if obj.type != 'MESH' or not obj.data.materials:
            continue
        for mat in obj.data.materials:
            if not mat or not mat.node_tree:
                continue
            rune_node = mat.node_tree.nodes.get('rune_normal')
            if rune_node and rune_node.type == 'TEX_IMAGE' and len(rune_node.inputs[0].links) > 0:
                mapping = rune_node.inputs[0].links[0].from_node
                if mapping.type == 'MAPPING':
                    mapping.inputs[1].default_value[0] = x_loc

def _safe_restore_selection(saved_active, saved_selection):
    """Restore selection/active safely (handles removed objects)."""
    try:
        bpy.ops.object.select_all(action='DESELECT')
    except Exception:
        pass
    for obj in saved_selection:
        try:
            if obj and obj.name in bpy.data.objects:
                obj.select_set(True)
        except ReferenceError:
            continue
        except Exception:
            continue
    try:
        if saved_active and saved_active.name in bpy.data.objects:
            bpy.context.view_layer.objects.active = saved_active
    except ReferenceError:
        pass
    except Exception:
        pass

def _set_pose_all_armatures(root_armature, pose_value):
    """Set pose_position for root armature and any child armatures."""
    if not root_armature or root_armature.type != 'ARMATURE':
        return []
    changed = []
    for obj in [root_armature] + list(root_armature.children_recursive):
        if obj.type == 'ARMATURE':
            action = None
            action_slot = None
            if obj.animation_data:
                action = obj.animation_data.action
                action_slot = getattr(obj.animation_data, "action_slot", None)
                obj.animation_data.action = None
            changed.append((obj, obj.data.pose_position, action, action_slot))
            obj.data.pose_position = pose_value
    try:
        bpy.context.view_layer.update()
    except Exception:
        pass
    return changed

def _restore_pose_all_armatures(changed):
    """Restore pose_position for armatures changed by _set_pose_all_armatures."""
    for obj, prev_pose, action, action_slot in changed:
        if obj and obj.type == 'ARMATURE':
            obj.data.pose_position = prev_pose
            if obj.animation_data is not None:
                obj.animation_data.action = action
                if action is not None and action_slot is not None and hasattr(obj.animation_data, "action_slot"):
                    obj.animation_data.action_slot = action_slot
    try:
        bpy.context.view_layer.update()
    except Exception:
        pass

def _temp_reset_armature_world(root_armature):
    """Temporarily reset armature world transform to identity for clean imports."""
    if not root_armature or root_armature.type != 'ARMATURE':
        return None
    saved = root_armature.matrix_world.copy()
    root_armature.matrix_world = Matrix.Identity(4)
    try:
        bpy.context.view_layer.update()
    except Exception:
        pass
    return saved

def _restore_armature_world(root_armature, saved):
    if not root_armature or saved is None:
        return
    root_armature.matrix_world = saved
    try:
        bpy.context.view_layer.update()
    except Exception:
        pass


# =============================================================================
# Equipment Variant Helpers
# =============================================================================

def _safe_json_list(value):
    if not value:
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []

def _format_variant_summary(variants):
    if not variants:
        return ""
    parts = []
    for v in variants:
        try:
            cat = v.get("category", "")
            tmpl = v.get("equip_template", "")
        except Exception:
            continue
        if cat and tmpl:
            parts.append(f"{cat}->{tmpl}")
        elif cat:
            parts.append(cat)
        elif tmpl:
            parts.append(tmpl)
    return ", ".join(parts)

def _format_bound_items_summary(bound_items):
    if not bound_items:
        return ""
    try:
        return ", ".join([str(b) for b in bound_items if b])
    except Exception:
        return ""

def _split_tags(raw_text):
    if not raw_text:
        return []
    text = raw_text.replace("\n", " ").replace("\t", " ").strip()
    if not text:
        return []
    parts = re.split(r"[,\s]+", text)
    return [p for p in (part.strip() for part in parts) if p]


def _slot_has_active_selection(slot) -> bool:
    if slot is None:
        return False
    if bool(getattr(slot, "is_inventory", False)):
        return True
    if bool(getattr(slot, "is_loaded", False)):
        return True
    item_name = str(getattr(slot, "item_name", "") or "").strip()
    if item_name and item_name.lower() != "none":
        return True
    equip_template = str(getattr(slot, "equip_template", "") or "").strip()
    if equip_template and equip_template.lower() != "none":
        return True
    return False


def _slot_persists_across_appearances(slot) -> bool:
    if slot is None:
        return False
    return bool(
        getattr(slot, "is_inventory", False)
        or getattr(slot, "keep_across_appearances", False)
    )


def _slot_uses_appearance_drivers(slot) -> bool:
    return not _slot_persists_across_appearances(slot)

def _get_tags_for_slot(slot, source_game="w3"):
    tags = []
    try:
        attrs = _lookup_item_attributes(slot.item_name, source_game)
        tags = attrs.get("tags", []) or []
    except Exception:
        tags = []
    if isinstance(tags, str):
        tags = _split_tags(tags)
    return [t.lower() for t in tags if isinstance(t, str)]

def _find_matching_variant(slot, category_has_item):
    variants = _safe_json_list(getattr(slot, "variants_json", ""))
    if not variants:
        return None
    for v in variants:
        try:
            cat = v.get("category", "")
        except Exception:
            continue
        if cat and cat in category_has_item:
            return v
    return None

def refresh_variant_states(rig_settings):
    """Compute variant-active state for all equipment slots."""
    if not rig_settings:
        return 0
    source_game = _infer_source_game_from_rig_settings(rig_settings)
    slots = rig_settings.equipment_slots
    auto_mode = getattr(rig_settings, "variants_auto", True)
    category_has_item = {}
    for slot in slots:
        if slot.is_loaded and slot.category and slot.item_name and str(slot.item_name).lower() not in {"none", ""}:
            tags_lower = _get_tags_for_slot(slot, source_game)
            if "body" in tags_lower:
                continue
            category_has_item[slot.category] = slot

    updated = 0
    for slot in slots:
        was_active = bool(getattr(slot, "variant_active", False))
        match = _find_matching_variant(slot, category_has_item)
        if auto_mode:
            slot.variants_enabled = True if match else False

        variant = match if getattr(slot, "variants_enabled", False) else None
        if variant:
            slot.variant_active = True
            slot.variant_template = variant.get("equip_template", "")
            slot.variant_category = variant.get("category", "")
            slot.variant_equip_slot = variant.get("equip_slot", "")
            slot.variant_hold_slot = variant.get("hold_slot", "")
        else:
            slot.variant_active = False
            slot.variant_template = ""
            slot.variant_category = ""
            slot.variant_equip_slot = ""
            slot.variant_hold_slot = ""
        if was_active != slot.variant_active:
            updated += 1
    return updated

def get_effective_equip_template(slot):
    if getattr(slot, "variant_active", False) and getattr(slot, "variant_template", ""):
        return slot.variant_template
    base = getattr(slot, "base_equip_template", "") or slot.equip_template
    return base

def get_effective_equip_slot(slot):
    if getattr(slot, "variant_active", False) and getattr(slot, "variant_equip_slot", ""):
        return slot.variant_equip_slot
    return slot.equip_slot

def get_effective_hold_slot(slot):
    if getattr(slot, "variant_active", False) and getattr(slot, "variant_hold_slot", ""):
        return slot.variant_hold_slot
    return slot.hold_slot


def _slot_has_explicit_mount_target(slot):
    if slot is None:
        return False
    return bool(
        str(get_effective_equip_slot(slot) or "").strip()
        or str(get_effective_hold_slot(slot) or "").strip()
    )


def _slot_matches_unmounted_visual_hint(slot):
    if slot is None:
        return False
    for value in (
        getattr(slot, "category", ""),
        getattr(slot, "item_name", ""),
        get_effective_equip_template(slot),
    ):
        normalized = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower())
        if "tail" in normalized or "hair" in normalized:
            return True
    return False


def _allow_unmounted_slotless_visual(slot, *, attachment_profile=None, item_entity=None):
    if slot is None or _slot_has_explicit_mount_target(slot):
        return False

    if attachment_profile is None and item_entity is not None:
        try:
            attachment_profile = import_entity.classify_equipment_attachment_profile(item_entity)
        except Exception:
            attachment_profile = None

    if attachment_profile is not None:
        profile_kind = str(getattr(attachment_profile, "kind", "") or "").strip()
        if profile_kind == "inventory_wrapper":
            return False
        if profile_kind == "owner_graph":
            return True
        if bool(getattr(slot, "weapon", False)):
            return False
        if profile_kind in {"slot_visual", "slot_animated"}:
            return True

    if not bool(getattr(slot, "is_inventory", False)):
        return True
    if bool(getattr(slot, "weapon", False)):
        return False
    return _slot_matches_unmounted_visual_hint(slot)


# =============================================================================
# Bound Item Helpers
# =============================================================================

def _select_bundle_item(item, search_pattern):
    """Choose the best matching BundleItem from the search result."""
    final_item = item[-1]
    if isinstance(final_item, list):
        for candidate in item:
            if isinstance(candidate, list):
                for sub in candidate:
                    if hasattr(sub, 'name') and sub.name.endswith(search_pattern):
                        return sub
            elif hasattr(candidate, 'name') and candidate.name.endswith(search_pattern):
                return candidate
        if isinstance(final_item, list) and len(final_item) > 0:
            return final_item[-1]
    return final_item


def _normalize_template_path(template_name):
    return str(template_name or "").replace("/", "\\").strip().lstrip("\\")


def _template_match_keys(template_name):
    rel_name = _normalize_template_path(template_name)
    if not rel_name:
        return set()

    lower_rel = rel_name.lower()
    keys = {lower_rel}

    rel_root, rel_ext = os.path.splitext(lower_rel)
    if rel_ext:
        keys.add(rel_root)
    else:
        keys.add(lower_rel + ".w2ent")

    base_name = lower_rel.rsplit("\\", 1)[-1]
    keys.add(base_name)
    base_root, base_ext = os.path.splitext(base_name)
    if base_ext:
        keys.add(base_root)
    else:
        keys.add(base_name + ".w2ent")
    return {key for key in keys if key}


def _resolve_bundle_item_by_template(template_name, search_roots=None):
    if not template_name:
        return None, None, None
    rel_candidates = []
    rel_name = str(template_name).replace("/", "\\").lstrip("\\")
    is_short_template_id = bool(rel_name) and ("\\" not in rel_name)
    if rel_name:
        if rel_name.lower().endswith(".w2ent"):
            rel_candidates.append(rel_name)
        else:
            rel_candidates.append(rel_name + ".w2ent")
        if not rel_candidates[0].lower().startswith("items\\"):
            rel_candidates.append("items\\" + rel_candidates[0])

    search_roots = list(search_roots or [])
    prefer_w2_repo = _is_w2_search(search_roots)
    if prefer_w2_repo:
        roots_to_search = _normalize_unique_roots(search_roots + _get_w2_repo_roots())
        uncook_root = ""
    else:
        # Prefer already-exported assets from uncook/source roots. Bundles are fallback.
        try:
            uncook_root = get_uncook_path(bpy.context)
        except Exception:
            uncook_root = ""
        roots_to_search = _normalize_unique_roots([uncook_root] + search_roots)

    for rel_path in rel_candidates:
        for root in roots_to_search:
            export_path = os.path.join(root, rel_path)
            if os.path.exists(export_path):
                return SimpleNamespace(name=rel_path), export_path, "\\" + rel_path

    def _lookup_indexed_rel_path():
        key = _w2ent_basename_key(rel_name)
        if not key:
            return None
        for root in roots_to_search:
            indexed_rel_path = _get_uncook_item_ent_index(root).get(key)
            if not indexed_rel_path:
                continue
            export_path = os.path.join(root, indexed_rel_path)
            if os.path.exists(export_path):
                return SimpleNamespace(name=indexed_rel_path), export_path, "\\" + indexed_rel_path
        return None

    if is_short_template_id:
        indexed_match = _lookup_indexed_rel_path()
        if indexed_match:
            return indexed_match

    if not prefer_w2_repo:
        # Try repo_file for candidate relative paths to benefit from bundle/mod
        # extraction and repo override roots.
        for rel_path in rel_candidates:
            try:
                repo_path = repo_file(rel_path)
            except Exception:
                repo_path = ""
            if repo_path and os.path.exists(repo_path):
                return SimpleNamespace(name=rel_path), repo_path, "\\" + rel_path

    # Basename fallback: many equipment templates are referenced by short IDs
    # (e.g. "axe_01") while the file resides under nested folders.
    indexed_match = _lookup_indexed_rel_path()
    if indexed_match:
        return indexed_match

    search_pattern = "\\" + template_name
    if not search_pattern.lower().endswith(".w2ent"):
        search_pattern += ".w2ent"
    search_info = f"{search_pattern}; roots={roots_to_search}"
    if prefer_w2_repo:
        return None, None, search_info
    try:
        bundle_manager = LoadBundleManager()
    except Exception:
        return None, None, search_info
    if is_short_template_id:
        # For short IDs (no path separator in the template name), use basename-only
        # matching so the search is slash-agnostic.  Bundle keys may use either /
        # or \ as separators; os.path.basename() handles both, while endswith()
        # on a pattern that includes a backslash only matches backslash-keyed bundles.
        basename_end = rel_name
        if not basename_end.lower().endswith(".w2ent"):
            basename_end += ".w2ent"
        item = bundle_manager.find_item_by_partial_hash(start="items", end=basename_end)
        if not item:
            item = bundle_manager.find_item_by_partial_hash(start="", end=basename_end)
    else:
        item = bundle_manager.find_item_by_partial_hash(start="items", end=search_pattern)
        # Some equipment/body templates resolve outside items (e.g. characters/...).
        if not item:
            item = bundle_manager.find_item_by_partial_hash(start="", end=search_pattern)
        if not item and rel_name:
            basename_end = rel_name.rsplit("\\", 1)[-1]
            if not basename_end.lower().endswith(".w2ent"):
                basename_end += ".w2ent"
            item = bundle_manager.find_item_by_partial_hash(start="", end=basename_end)
    if not item:
        return None, None, search_info
    final_item = _select_bundle_item(item, search_pattern)
    if not hasattr(final_item, 'name'):
        return None, None, search_info
    export_path = repo_file(final_item.name)
    if not os.path.exists(export_path):
        final_item.extract_to_file(export_path)
    _remember_uncook_item_relpath(uncook_root, final_item.name)
    return final_item, export_path, search_info


def _resolve_bundle_item_by_template_cached(template_name, search_roots=None, prepared_context=None):
    if prepared_context is None:
        return _resolve_bundle_item_by_template(template_name, search_roots=search_roots)

    cache = prepared_context.setdefault("bundle_item_cache", {})
    cache_key = (
        _normalize_template_path(template_name).lower(),
        tuple(_norm_root_path(root) for root in (search_roots or [])),
    )
    if cache_key not in cache:
        cache[cache_key] = _resolve_bundle_item_by_template(template_name, search_roots=search_roots)
    return cache[cache_key]


def _resolve_equipment_paths_for_template(template_name, armature=None, rig_settings=None):
    template_name = str(template_name or "").strip()
    if not template_name or template_name.lower() == "none":
        return "", ""

    source_roots = []
    if armature is not None:
        source_roots = _get_armature_source_roots(armature)
    if not source_roots and rig_settings is not None:
        repo_path_hint = getattr(rig_settings, "repo_path", "") or ""
        if repo_path_hint and os.path.isabs(repo_path_hint):
            try:
                source_roots = import_entity._build_entity_source_roots(repo_path_hint)
            except Exception:
                source_roots = []

    # Use a persistent module-level cache to avoid repeated bundle extraction
    # every time the UI redraws or sync_equipment_slots_to_temp iterates entries.
    cache_key = (
        _normalize_template_path(template_name).lower(),
        tuple(_norm_root_path(r) for r in source_roots),
    )
    cached = _TEMPLATE_PATH_RESOLVE_CACHE.get(cache_key)
    if cached is not None:
        return cached

    final_item, export_path, _search_pattern = _resolve_bundle_item_by_template_cached(
        template_name,
        search_roots=source_roots,
        prepared_context=None,
    )
    repo_path = str(getattr(final_item, "name", "") or "").replace("/", "\\").lstrip("\\")
    if not repo_path and export_path:
        try:
            from ..importers.import_mesh import get_repo_from_abs_path
            repo_path = str(get_repo_from_abs_path(export_path) or "").replace("/", "\\").lstrip("\\")
        except Exception:
            repo_path = ""
    result = (repo_path, export_path or "")
    # Cache misses as well so missing legacy templates do not trigger repeated
    # bundle extraction attempts on every UI redraw.
    _TEMPLATE_PATH_RESOLVE_CACHE[cache_key] = result
    _clear_cache_if_oversized(_TEMPLATE_PATH_RESOLVE_CACHE, max_entries=256)
    return result


def _update_entry_resolved_repo_path(entry, context=None, armature=None, rig_settings=None):
    if entry is None:
        return ""

    if armature is None or rig_settings is None:
        try:
            armature, rig_settings = _get_armature_and_rig_settings(context)
        except Exception:
            armature, rig_settings = None, None

    template_name = str(getattr(entry, "equip_template", "") or "").strip()
    slot_repo_path = ""
    try:
        slot_index = int(getattr(entry, "slot_index", -1))
    except Exception:
        slot_index = -1
    if rig_settings is not None and 0 <= slot_index < len(rig_settings.equipment_slots):
        try:
            slot = rig_settings.equipment_slots[slot_index]
            template_name = get_effective_equip_template(slot) or template_name
            slot_repo_path = str(getattr(slot, "resolved_repo_path", "") or "").strip()
            slot_source_game = str(getattr(slot, "source_game", "") or "").strip()
            if slot_source_game:
                entry.source_game = _normalize_source_game(slot_source_game)
        except Exception:
            pass

    try:
        computed_repo_path, resolved_abs_path = _resolve_equipment_paths_for_template(
            template_name,
            armature=armature,
            rig_settings=rig_settings,
        )
    except Exception:
        computed_repo_path, resolved_abs_path = "", ""

    repo_path = slot_repo_path or computed_repo_path

    try:
        entry.resolved_repo_path = repo_path
        entry.resolved_abs_path = resolved_abs_path
    except Exception:
        pass
    return repo_path


def _get_cached_equipment_item_entity(export_path, prepared_context=None):
    if not export_path or not os.path.exists(export_path):
        return None

    try:
        cache_key = (
            os.path.normcase(os.path.normpath(export_path)),
            os.path.getmtime(export_path),
            os.path.getsize(export_path),
        )
    except Exception:
        cache_key = (os.path.normcase(os.path.normpath(export_path)),)

    if cache_key in _EQUIPMENT_ENTITY_CACHE:
        entity = _EQUIPMENT_ENTITY_CACHE[cache_key]
        if prepared_context is not None:
            prepared_context.setdefault("item_entity_cache", {})[cache_key] = entity
        return entity

    local_cache = prepared_context.setdefault("item_entity_cache", {}) if prepared_context is not None else None
    if local_cache is not None and cache_key in local_cache:
        return local_cache[cache_key]

    try:
        item_entity = import_entity.test_load_entity(export_path)
    except Exception as e:
        log.warning("Failed to parse equipment entity '%s': %s", export_path, e)
        item_entity = None

    _EQUIPMENT_ENTITY_CACHE[cache_key] = item_entity
    _clear_cache_if_oversized(_EQUIPMENT_ENTITY_CACHE, max_entries=128)
    if local_cache is not None:
        local_cache[cache_key] = item_entity
    return item_entity

def _update_slot_coloring_json(slot, item_entity):
    """Populate slot.item_coloring_json from item_entity.coloringEntries for the selected appearance."""
    coloring_entries = getattr(item_entity, 'coloringEntries', None) or []
    selected_app = getattr(slot, 'item_appearance_name', '') or '__default__'
    result = []
    for entry in coloring_entries:
        entry_app = getattr(entry, 'appearance', '') or ''
        if selected_app == '__default__' or not entry_app or entry_app == selected_app:
            cs1 = getattr(entry, 'colorShift1', None)
            cs2 = getattr(entry, 'colorShift2', None)
            result.append({
                'componentName': getattr(entry, 'componentName', ''),
                'hue1': getattr(cs1, 'hue', 0) if cs1 else 0,
                'sat1': getattr(cs1, 'saturation', 0) if cs1 else 0,
                'lum1': getattr(cs1, 'luminance', 0) if cs1 else 0,
                'hue2': getattr(cs2, 'hue', 0) if cs2 else 0,
                'sat2': getattr(cs2, 'saturation', 0) if cs2 else 0,
                'lum2': getattr(cs2, 'luminance', 0) if cs2 else 0,
            })
    slot.item_coloring_json = json.dumps(result)


def _import_item_entity(export_path, final_item_name, entity, armature, appearance, slot_index, empty_transform,
                        use_app_drivers=True, prepared_context=None, item_appearance_name=None,
                        attachment_profile=None, bind_root_chunks_to_entity=None):
    """Import a w2ent item (handles includedTemplates)."""
    from ..importers.import_entity import add_app_template
    ent_namespace = entity.name + ":"

    included_templates = []
    imported_template_keys = set()
    imported_template_keys.update(_template_match_keys(final_item_name))
    selected_item_appearance_name = ""
    selected_item_appearance = None
    try:
        item_entity = _get_cached_equipment_item_entity(export_path, prepared_context=prepared_context)
    except Exception:
        item_entity = None
    if attachment_profile is None:
        attachment_profile = import_entity.classify_equipment_attachment_profile(item_entity)
    if bind_root_chunks_to_entity is None:
        bind_root_chunks_to_entity = bool(
            attachment_profile is None
            or getattr(attachment_profile, "requires_owner_root_binding", False)
        )
    if item_entity and hasattr(item_entity, 'appearances') and item_entity.appearances:
        selected_app = item_entity.appearances[0]
        if item_appearance_name and item_appearance_name != '__default__':
            for app in item_entity.appearances:
                if getattr(app, 'name', '') == item_appearance_name:
                    selected_app = app
                    break
        selected_item_appearance = selected_app
        selected_item_appearance_name = getattr(selected_app, 'name', '') or ""
        if hasattr(selected_app, 'includedTemplates') and selected_app.includedTemplates:
            included_templates = selected_app.includedTemplates

    static_template_data = None
    static_meshes = getattr(item_entity, 'staticMeshes', None) if item_entity else None
    if isinstance(static_meshes, dict) and static_meshes.get('chunks'):
        static_template_data = static_meshes
    elif hasattr(static_meshes, 'chunks') and getattr(static_meshes, 'chunks', None):
        static_template_data = {'chunks': static_meshes.chunks}

    for template in included_templates:
        template_filename = template.get('templateFilename', '') if isinstance(template, dict) else getattr(template, 'templateFilename', '')
        if template_filename:
            imported_template_keys.update(_template_match_keys(template_filename))

    item_import_context = selected_item_appearance
    if item_import_context is None:
        item_import_context = appearance if appearance else type('obj', (), {'includedTemplates': [], 'name': 'equipment'})()

    # Determine whether to import redcloth/apex cloth resources.
    # Mirror the same check used by import_app: user preference + addon availability.
    import addon_utils as _addon_utils
    _equip_import_redcloth = get_do_import_redcloth(bpy.context)
    if _equip_import_redcloth:
        _apx_exist, _apx_enabled = _addon_utils.check("io_mesh_apx")
        if not _apx_enabled:
            _apx_exist, _apx_enabled = _addon_utils.check("io_scene_apx")
        if not _apx_enabled:
            _equip_import_redcloth = False

    def _import_equipment_template(template_source, *, template_data=None):
        if not template_source:
            return
        imported_template_keys.update(_template_match_keys(template_source))
        add_app_template(
            entity,
            armature,
            entity.name,
            ent_namespace,
            _equip_import_redcloth,
            slot_index,
            item_import_context,
            True,
            empty_transform,
            False,
            template_source,
            template_data=template_data,
            appearance_indices=None,
            use_app_drivers=use_app_drivers,
            bind_root_chunks_to_entity=bool(bind_root_chunks_to_entity),
        )

    base_template_source = export_path if export_path and os.path.isabs(export_path) else final_item_name
    if static_template_data:
        _import_equipment_template(base_template_source, template_data=static_template_data)
    elif not included_templates:
        _import_equipment_template(base_template_source)

    for template in included_templates:
        template_filename = template.get('templateFilename', '') if isinstance(template, dict) else getattr(template, 'templateFilename', '')
        if template_filename:
            _import_equipment_template(
                template_filename,
                template_data=template if isinstance(template, dict) else None,
            )
    return {
        "template_keys": imported_template_keys,
        "item_entity": item_entity,
        "attachment_profile": attachment_profile,
        "selected_appearance_name": selected_item_appearance_name,
    }

def _resolve_bound_item_template(bound_item_name, search_roots=None):
    """Resolve a bound item name to an equip_template if possible."""
    try:
        from ..importers import import_entity
        item_lookup, template_lookup = import_entity._build_equipment_lookup(search_roots)
        resolved = import_entity._resolve_inventory_item(bound_item_name, item_lookup, template_lookup)
        if resolved:
            return resolved[2]
        derived = import_entity._derive_template_from_item(bound_item_name)
        return derived if derived else bound_item_name
    except Exception:
        return bound_item_name

def _get_slot_target_armature(slot_empty, fallback_armature):
    if slot_empty:
        for c in slot_empty.constraints:
            if c.type in {'COPY_TRANSFORMS', 'CHILD_OF'} and c.target and c.target.type == 'ARMATURE':
                return c.target
        if slot_empty.parent and slot_empty.parent.type == 'ARMATURE':
            return slot_empty.parent
    return fallback_armature


def _iter_local_armatures(root_armature):
    if root_armature is None:
        return []
    armatures = []
    seen = set()
    candidates = [root_armature]
    try:
        candidates.extend(list(root_armature.children_recursive))
    except Exception:
        pass
    for candidate in candidates:
        if candidate is None or candidate.type != 'ARMATURE':
            continue
        try:
            key = candidate.as_pointer()
        except Exception:
            key = getattr(candidate, "name_full", getattr(candidate, "name", id(candidate)))
        if key in seen:
            continue
        seen.add(key)
        armatures.append(candidate)
    return armatures


def _find_component_target_armature(root_armature, component_name, entity_name=""):
    component_name = str(component_name or "").strip()
    if not component_name:
        return None

    local_armatures = _iter_local_armatures(root_armature)
    for obj in local_armatures:
        if str(obj.get('witcher_name', '') or '').strip() == component_name:
            return obj

    for obj in local_armatures:
        if obj.name == f"{entity_name}:{component_name}" or obj.name == component_name:
            return obj
        if component_name in obj.name and (not entity_name or obj.name.startswith(entity_name)):
            return obj
    return None


def _resolve_slot_target_armature_from_rig(slot_name, armature, rig_settings):
    slot_name = str(slot_name or "").strip()
    if not slot_name or armature is None or rig_settings is None:
        return armature

    entity_name = getattr(rig_settings, "entity_name", "") or ""
    slot_entry = _find_slot_entry_for_mount_slot(slot_name, rig_settings)
    if slot_entry is not None:
        component_name = getattr(slot_entry, "component_name", "") or ""
        target_armature = _find_component_target_armature(armature, component_name, entity_name=entity_name)
        if target_armature is not None:
            return target_armature
    return armature


def _find_slot_entry_for_mount_slot(slot_name, rig_settings):
    slot_name = str(slot_name or "").strip()
    if not slot_name or rig_settings is None:
        return None

    exact_match = None
    bone_matches = []
    fuzzy_matches = []
    for slot_entry in getattr(rig_settings, "entity_slots", []):
        entry_slot_name = str(getattr(slot_entry, "slot_name", "") or "").strip()
        entry_bone_name = str(getattr(slot_entry, "bone_name", "") or "").strip()
        if entry_slot_name == slot_name:
            exact_match = slot_entry
            break
        if entry_bone_name == slot_name:
            bone_matches.append(slot_entry)
            continue
        if slot_name and entry_slot_name and slot_name in entry_slot_name:
            fuzzy_matches.append((len(entry_slot_name), slot_entry))

    if exact_match is not None:
        return exact_match
    if len(bone_matches) == 1:
        return bone_matches[0]
    if fuzzy_matches:
        fuzzy_matches.sort(key=lambda pair: pair[0])
        return fuzzy_matches[0][1]
    return None


def _get_root_bone_name(armature):
    if armature is None or armature.type != 'ARMATURE':
        return ""
    try:
        for bone in armature.data.bones:
            if bone.parent is None:
                return bone.name
    except Exception:
        pass
    return ""


def _link_object_to_armature_collection(obj, armature):
    if obj is None:
        return
    target_collection = None
    try:
        if armature is not None and armature.users_collection:
            target_collection = armature.users_collection[0]
    except Exception:
        target_collection = None
    if target_collection is None:
        target_collection = getattr(bpy.context, "collection", None)
    if target_collection is None:
        target_collection = bpy.context.scene.collection
    target_collection.objects.link(obj)


def _find_slots_parent_for_armature(armature, entity_name=""):
    if armature is None:
        return None
    arm_name = getattr(armature, "name_full", getattr(armature, "name", ""))
    candidates = []
    try:
        descendants = list(armature.children_recursive)
    except Exception:
        descendants = []
    for obj in descendants:
        if obj is None or obj.type != 'EMPTY' or not obj.get("witcher_slots_parent"):
            continue
        score = 0
        if obj.parent == armature:
            score += 4
        if obj.get("witcher_owner_armature") == arm_name:
            score += 2
        if entity_name and obj.get("witcher_entity_name") == entity_name:
            score += 1
        candidates.append((score, obj))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return candidates[0][1]


def _ensure_slots_parent_for_armature(armature, entity_name="", rig_settings=None):
    if armature is None:
        return None
    slots_parent = _find_slots_parent_for_armature(armature, entity_name=entity_name)
    if slots_parent is not None:
        return slots_parent

    slots_parent_name = f"{entity_name}_slots" if entity_name else "entity_slots"
    slots_parent = bpy.data.objects.new(slots_parent_name, None)
    _link_object_to_armature_collection(slots_parent, armature)
    slots_parent.empty_display_type = 'PLAIN_AXES'
    slots_parent.empty_display_size = 0.1
    slots_parent["witcher_slots_parent"] = True
    slots_parent["witcher_entity_name"] = entity_name or ""
    slots_parent["witcher_owner_armature"] = getattr(armature, "name_full", getattr(armature, "name", ""))
    slots_parent.parent = armature
    if hasattr(slots_parent, "show_relationship_lines"):
        slots_parent.show_relationship_lines = False
    slots_parent.hide_set(not getattr(rig_settings, "show_entity_slots", False))
    return slots_parent


def _ensure_slot_empty_from_rig(slot_name, armature, rig_settings):
    slot_name = str(slot_name or "").strip()
    if not slot_name or armature is None or armature.type != 'ARMATURE' or rig_settings is None:
        return None

    entity_name = getattr(rig_settings, "entity_name", "") or ""
    existing = find_slot_empty(entity_name, slot_name, armature)
    if existing is not None:
        return existing

    slot_entry = _find_slot_entry_for_mount_slot(slot_name, rig_settings)
    if slot_entry is None:
        return None

    resolved_slot_name = str(getattr(slot_entry, "slot_name", "") or "").strip() or slot_name
    if resolved_slot_name != slot_name:
        existing = find_slot_empty(entity_name, resolved_slot_name, armature)
        if existing is not None:
            return existing

    component_name = getattr(slot_entry, "component_name", "") or ""
    target_armature = _find_component_target_armature(armature, component_name, entity_name=entity_name)
    if target_armature is None:
        target_armature = armature

    bone_name = str(getattr(slot_entry, "bone_name", "") or "").strip()
    if bone_name:
        resolved_armature = _find_armature_with_bone(armature, bone_name, preferred_armature=target_armature)
        if resolved_armature is not None:
            target_armature = resolved_armature
    else:
        target_armature = armature
        bone_name = _get_root_bone_name(armature)

    slots_parent = _ensure_slots_parent_for_armature(armature, entity_name=entity_name, rig_settings=rig_settings)
    if slots_parent is None:
        return None

    empty_name = f"{entity_name}:{resolved_slot_name}" if entity_name else resolved_slot_name
    slot_empty = bpy.data.objects.new(empty_name, None)
    _link_object_to_armature_collection(slot_empty, armature)
    slot_empty.empty_display_type = 'SPHERE'
    slot_empty.empty_display_size = 0.02
    slot_empty["witcher_slot_name"] = resolved_slot_name
    slot_empty["witcher_entity_name"] = entity_name or ""
    slot_empty["witcher_owner_armature"] = getattr(armature, "name_full", getattr(armature, "name", ""))
    slot_empty.parent = slots_parent
    if hasattr(slot_empty, "show_relationship_lines"):
        slot_empty.show_relationship_lines = False

    try:
        transform_data = json.loads(getattr(slot_entry, "transform_json", "") or "") if getattr(slot_entry, "transform_json", "") else None
    except Exception:
        transform_data = None

    use_rot90 = get_rig_rot90_enabled(rig_settings, default=False)
    import_entity.set_empty_bone_offset(
        slot_empty,
        target_armature,
        bone_name,
        transform_data,
        rotate_90=use_rot90,
        rotate_90_dir=1,
    )
    slot_empty.hide_set(not getattr(rig_settings, "show_entity_slots", False))
    return slot_empty


def _find_armature_with_bone(root_armature, bone_name, preferred_armature=None):
    bone_name = str(bone_name or "").strip()
    if not bone_name:
        return None

    preferred = preferred_armature if preferred_armature and preferred_armature.type == 'ARMATURE' else None
    if preferred is not None:
        try:
            if bone_name in preferred.pose.bones:
                return preferred
        except Exception:
            pass

    for candidate in _iter_local_armatures(root_armature):
        if preferred is not None and candidate == preferred:
            continue
        try:
            if bone_name in candidate.pose.bones:
                return candidate
        except Exception:
            continue
    return None


def _resolve_equipment_mount_target(slot_name, armature, rig_settings):
    slot_name = str(slot_name or "").strip()
    target_info = {
        "name": slot_name,
        "is_valid": False,
        "target_type": "",
        "slot_empty": None,
        "armature": None,
        "bone_name": "",
    }
    if not slot_name or armature is None or armature.type != 'ARMATURE':
        return target_info

    entity_name = getattr(rig_settings, "entity_name", "") or ""
    slot_empty = find_slot_empty(entity_name, slot_name, armature)
    if slot_empty is None:
        slot_empty = _ensure_slot_empty_from_rig(slot_name, armature, rig_settings)
    if slot_empty is not None:
        target_info.update({
            "is_valid": True,
            "target_type": "slot",
            "slot_empty": slot_empty,
            "armature": _get_slot_target_armature(slot_empty, armature),
        })
        return target_info

    preferred_armature = _resolve_slot_target_armature_from_rig(slot_name, armature, rig_settings)
    target_armature = _find_armature_with_bone(armature, slot_name, preferred_armature=preferred_armature)
    if target_armature is not None:
        target_info.update({
            "is_valid": True,
            "target_type": "bone",
            "armature": target_armature,
            "bone_name": slot_name,
        })
    return target_info


def _item_entity_is_inventory_wrapper(item_entity):
    if item_entity is None:
        return False
    try:
        return import_entity.classify_equipment_attachment_profile(item_entity).kind == "inventory_wrapper"
    except Exception:
        return False


def _item_entity_is_visual(item_entity, attachment_profile=None):
    if attachment_profile is not None:
        return getattr(attachment_profile, "kind", "") != "inventory_wrapper"
    if item_entity is None:
        return True
    try:
        attachment_profile = import_entity.classify_equipment_attachment_profile(item_entity)
    except Exception:
        return not _item_entity_is_inventory_wrapper(item_entity)
    return getattr(attachment_profile, "kind", "") != "inventory_wrapper"


def _infer_equipment_mount_strategy(attachment_profile, target_info, *, allow_unmounted_visual=False):
    profile_kind = str(getattr(attachment_profile, "kind", "") or "").strip()
    target_valid = bool((target_info or {}).get("is_valid"))
    target_name = str((target_info or {}).get("name", "") or "").strip()
    has_skinned_mesh_payload = bool(getattr(attachment_profile, "has_skinned_mesh_payload", False))

    if profile_kind == "inventory_wrapper":
        return "nonvisual"
    if profile_kind == "owner_graph":
        return "owner_graph_bound"
    if target_valid:
        if profile_kind == "slot_animated":
            return "slot_mount_animated"
        return "slot_mount_static"
    if has_skinned_mesh_payload and not target_name:
        return "owner_graph_bound"
    if allow_unmounted_visual:
        if profile_kind == "slot_animated":
            return "slot_mount_animated"
        return "slot_mount_static"
    return "invalid_target"


def _should_bind_root_chunks_to_entity(attachment_profile, mount_strategy):
    if mount_strategy == "owner_graph_bound":
        return True
    if mount_strategy in {"slot_mount_static", "slot_mount_animated", "invalid_target", "nonvisual"}:
        return False
    return bool(
        attachment_profile is None
        or getattr(attachment_profile, "requires_owner_root_binding", False)
    )


def _resolve_bound_owner_bind_armature(slot, armature, rig_settings, attachment_profile,
                                       mount_strategy, bound_equip_slot, current_target_armature):
    bind_armature = current_target_armature or armature
    if mount_strategy != "owner_graph_bound":
        return bind_armature
    if bound_equip_slot:
        return bind_armature
    if str(getattr(attachment_profile, "kind", "") or "").strip() == "owner_graph":
        return bind_armature
    if not bool(getattr(attachment_profile, "has_skinned_mesh_payload", False)):
        return bind_armature

    parent_equip_target = _resolve_equipment_mount_target(
        get_effective_equip_slot(slot),
        armature,
        rig_settings,
    )
    equip_armature = (parent_equip_target or {}).get("armature")
    if (parent_equip_target or {}).get("is_valid") and equip_armature is not None:
        return equip_armature
    return bind_armature


def _should_use_bound_skinning_bridge(attachment_profile, mount_strategy, bound_equip_slot, target_armature):
    if not target_armature:
        return False
    if str(bound_equip_slot or "").strip():
        return False
    if mount_strategy != "owner_graph_bound":
        return False
    if str(getattr(attachment_profile, "kind", "") or "").strip() == "owner_graph":
        return False
    return bool(getattr(attachment_profile, "has_skinned_mesh_payload", False))


def _maybe_log_legacy_attachment_type_conflict(item_name, attachment_type, attachment_profile):
    legacy_type = str(attachment_type or "").strip().lower()
    if not legacy_type or attachment_profile is None:
        return

    inferred_kind = str(getattr(attachment_profile, "kind", "") or "").strip()
    if legacy_type == "skinning" and inferred_kind not in {"owner_graph", "slot_animated"}:
        log.debug(
            "Ignoring legacy attachment_type='%s' for '%s'; inferred profile is '%s'.",
            legacy_type,
            item_name,
            inferred_kind or "unknown",
        )


def _describe_mount_target(target_info):
    target_info = target_info or {}
    target_type = str(target_info.get("target_type", "") or "").strip()
    if target_type == "slot":
        slot_empty = target_info.get("slot_empty")
        slot_name = getattr(slot_empty, "name", "") if slot_empty is not None else ""
        return f"slot:{slot_name or 'unknown'}"
    if target_type == "bone":
        return f"bone:{str(target_info.get('bone_name', '') or '').strip() or 'unknown'}"
    return target_type or "none"


def _resolve_visual_policy_from_slot_names(equip_slot_name, hold_slot_name, armature, rig_settings, *,
                                           item_entity=None, attachment_profile=None,
                                           allow_unmounted_visual=False):
    if attachment_profile is None and item_entity is not None:
        attachment_profile = import_entity.classify_equipment_attachment_profile(item_entity)
    equip_slot_name = str(equip_slot_name or "").strip()
    hold_slot_name = str(hold_slot_name or "").strip()
    equip_target = _resolve_equipment_mount_target(equip_slot_name, armature, rig_settings)
    hold_target = _resolve_equipment_mount_target(hold_slot_name, armature, rig_settings)
    item_is_visual = _item_entity_is_visual(item_entity, attachment_profile=attachment_profile)

    if not item_is_visual:
        policy = "nonvisual_on_rig"
    elif attachment_profile is not None and getattr(attachment_profile, "kind", "") == "owner_graph":
        policy = "equipable_on_rig"
    elif equip_target["is_valid"]:
        policy = "equipable_on_rig"
    elif hold_target["is_valid"]:
        policy = "hold_only_on_rig"
    elif allow_unmounted_visual:
        policy = "equipable_on_rig"
    else:
        policy = "nonvisual_on_rig"

    return {
        "policy": policy,
        "item_is_visual": item_is_visual,
        "equip_target": equip_target,
        "hold_target": hold_target,
        "equip_valid": bool(equip_target["is_valid"]),
        "hold_valid": bool(hold_target["is_valid"]),
        "attachment_profile": attachment_profile,
    }


def _resolve_slot_visual_policy(slot, armature, rig_settings, *, item_entity=None, attachment_profile=None):
    if slot is None:
        return _resolve_visual_policy_from_slot_names(
            "",
            "",
            armature,
            rig_settings,
            item_entity=item_entity,
            attachment_profile=attachment_profile,
        )
    equip_slot_name = get_effective_equip_slot(slot)
    hold_slot_name = get_effective_hold_slot(slot)
    allow_unmounted_visual = _allow_unmounted_slotless_visual(
        slot,
        attachment_profile=attachment_profile,
        item_entity=item_entity,
    )
    return _resolve_visual_policy_from_slot_names(
        equip_slot_name,
        hold_slot_name,
        armature,
        rig_settings,
        item_entity=item_entity,
        attachment_profile=attachment_profile,
        allow_unmounted_visual=allow_unmounted_visual,
    )

def _bind_objects_to_armature(objects, target_armature):
    if not target_armature:
        return
    for obj in objects:
        if obj.type != 'MESH':
            continue
        if obj.parent and obj.parent.type == 'ARMATURE' and obj.parent != target_armature:
            saved_world = obj.matrix_world.copy()
            obj.parent = target_armature
            obj.parent_type = 'OBJECT'
            obj.matrix_world = saved_world
        arm_mod = None
        for mod in obj.modifiers:
            if mod.type == 'ARMATURE':
                arm_mod = mod
                break
        if arm_mod:
            arm_mod.object = target_armature
        else:
            mod = obj.modifiers.new(name="W2_Skin", type='ARMATURE')
            mod.object = target_armature

def _constrain_bound_armature_to_target(bound_armature, target_armature):
    """Constrain bound armature bones to target armature bones (skinning behavior)."""
    if not bound_armature or not target_armature:
        return
    if bound_armature.type != 'ARMATURE' or target_armature.type != 'ARMATURE':
        return
    _snap_armature_to_target(bound_armature, target_armature)
    _align_bound_armature_pose(bound_armature, target_armature)
    try:
        # Preserve selection/active to avoid UI focus loss
        saved_active = bpy.context.view_layer.objects.active
        saved_selection = [obj for obj in bpy.context.selected_objects]
        bpy.ops.object.select_all(action='DESELECT')
        target_armature.select_set(True)
        bound_armature.select_set(True)
        bpy.context.view_layer.objects.active = target_armature

        from .. import constrain_util
        constrain_util.CreateConstraints2(target_armature, bound_armature)
        _set_child_of_inverse_for_armature(bound_armature)
        try:
            bpy.context.view_layer.update()
        except Exception:
            pass
    except Exception as e:
        log.warning(f"Failed to constrain bound armature: {e}")
    finally:
        try:
            bpy.ops.object.mode_set(mode='OBJECT', toggle=False)
        except Exception:
            pass
        _safe_restore_selection(saved_active, saved_selection)


def _armature_has_external_binding(bound_armature, object_set):
    if not bound_armature or bound_armature.type != 'ARMATURE':
        return False

    parent_obj = getattr(bound_armature, "parent", None)
    if (
        parent_obj is not None
        and parent_obj not in object_set
        and (
            getattr(parent_obj, "type", "") == 'ARMATURE'
            or getattr(bound_armature, "parent_type", "") == 'BONE'
        )
    ):
        return True

    for constraint in getattr(bound_armature, "constraints", []):
        if constraint.type in {'COPY_TRANSFORMS', 'CHILD_OF'} and constraint.target and constraint.target not in object_set:
            return True

    pose_data = getattr(bound_armature, "pose", None)
    pose_bones = getattr(pose_data, "bones", []) if pose_data is not None else []
    for pose_bone in pose_bones:
        for constraint in pose_bone.constraints:
            if constraint.type in {'COPY_TRANSFORMS', 'CHILD_OF'} and constraint.target and constraint.target not in object_set:
                return True

    return False


def _attach_imported_objects_via_skinning(objects, target_armature):
    """Legacy/manual recovery path for binding imported equipment to an armature."""
    if not target_armature:
        return False

    object_set = {obj for obj in (objects or []) if obj is not None}
    candidate_root_armatures = [
        obj for obj in object_set
        if obj.type == 'ARMATURE' and (obj.parent is None or obj.parent not in object_set)
    ]
    root_armatures = [
        arm for arm in candidate_root_armatures
        if not _armature_has_external_binding(arm, object_set)
    ]
    if root_armatures:
        for arm in root_armatures:
            try:
                arm.data.pose_position = target_armature.data.pose_position
            except Exception:
                arm.data.pose_position = "POSE"
            saved_world = arm.matrix_world.copy()
            arm.parent = target_armature
            arm.parent_type = 'OBJECT'
            arm.matrix_world = saved_world
            _snap_armature_to_target(arm, target_armature)
            _constrain_bound_armature_to_target(arm, target_armature)
        return True

    if candidate_root_armatures:
        return True

    _bind_objects_to_armature(object_set, target_armature)
    return bool(object_set)

def _snap_armature_to_target(bound_armature, target_armature):
    """Snap a bound armature object to the target armature's evaluated world matrix."""
    if not bound_armature or not target_armature:
        return
    if bound_armature.type != 'ARMATURE' or target_armature.type != 'ARMATURE':
        return
    try:
        dg = bpy.context.evaluated_depsgraph_get()
        target_eval = target_armature.evaluated_get(dg)
        target_world = target_eval.matrix_world
    except Exception:
        target_world = target_armature.matrix_world
    try:
        bound_armature.matrix_world = target_world
    except Exception:
        pass

def _align_bound_armature_pose(bound_armature, target_armature):
    """Align bound armature pose bones to target armature's current evaluated pose."""
    if not bound_armature or not target_armature:
        return
    if bound_armature.type != 'ARMATURE' or target_armature.type != 'ARMATURE':
        return
    try:
        from .. import file_helpers
    except Exception:
        file_helpers = None

    try:
        saved_active = bpy.context.view_layer.objects.active
        saved_selection = [obj for obj in bpy.context.selected_objects]

        dg = bpy.context.evaluated_depsgraph_get()
        target_eval = target_armature.evaluated_get(dg)
        target_world = target_eval.matrix_world

        # Build name -> pose bone map (namespace-stripped)
        target_map = {}
        for tp in target_eval.pose.bones:
            name = tp.name
            if file_helpers:
                name = file_helpers.rm_ns(name)
            target_map[name] = tp

        bpy.ops.object.select_all(action='DESELECT')
        bound_armature.select_set(True)
        bpy.context.view_layer.objects.active = bound_armature
        bpy.ops.object.mode_set(mode='POSE', toggle=False)

        inv_bound_world = bound_armature.matrix_world.inverted()
        for bp in bound_armature.pose.bones:
            bname = bp.name
            if file_helpers:
                bname = file_helpers.rm_ns(bname)
            tp = target_map.get(bname)
            if not tp:
                continue
            target_world_matrix = target_world @ tp.matrix
            try:
                bp.matrix = inv_bound_world @ target_world_matrix
            except Exception:
                pass

        bpy.ops.object.mode_set(mode='OBJECT', toggle=False)
    except Exception:
        pass
    finally:
        try:
            bpy.ops.object.mode_set(mode='OBJECT', toggle=False)
        except Exception:
            pass
        _safe_restore_selection(saved_active, saved_selection)

def _set_child_of_inverse_for_armature(bound_armature):
    """Set inverse for CHILD_OF constraints on a bound armature (keeps offsets)."""
    if not bound_armature or bound_armature.type != 'ARMATURE':
        return
    try:
        saved_active = bpy.context.view_layer.objects.active
        saved_selection = [obj for obj in bpy.context.selected_objects]
        bpy.ops.object.select_all(action='DESELECT')
        bound_armature.select_set(True)
        bpy.context.view_layer.objects.active = bound_armature
        bpy.ops.object.mode_set(mode='POSE', toggle=False)

        for pb in bound_armature.pose.bones:
            for c in pb.constraints:
                if c.type == 'CHILD_OF':
                    try:
                        bound_armature.data.bones.active = bound_armature.data.bones[pb.name]
                        bpy.ops.constraint.childof_set_inverse(constraint=c.name, owner='BONE')
                    except Exception:
                        pass
        bpy.ops.object.mode_set(mode='OBJECT', toggle=False)
    except Exception:
        pass
    finally:
        try:
            bpy.ops.object.mode_set(mode='OBJECT', toggle=False)
        except Exception:
            pass
        _safe_restore_selection(saved_active, saved_selection)

def _is_guid_hidden(guid, prop_name="witcher_equip_guid"):
    if not guid:
        return False
    objs = find_objects_by_guid(guid, prop_name)
    if not objs:
        return False
    try:
        return all(obj.hide_get() for obj in objs)
    except Exception:
        return all(getattr(obj, "hide_viewport", False) for obj in objs)

def _iter_bound_item_objects(parent_guid, bound_name):
    for obj in bpy.data.objects:
        if obj.get("witcher_bound_parent_guid") == parent_guid and obj.get("witcher_bound_item_name") == bound_name:
            yield obj

def _is_bound_item_hidden(parent_guid, bound_name):
    objs = list(_iter_bound_item_objects(parent_guid, bound_name))
    if not objs:
        return False
    try:
        return all(obj.hide_get() for obj in objs)
    except Exception:
        return all(getattr(obj, "hide_viewport", False) for obj in objs)


_VARIANT_REFRESHING = False

def _refresh_variants_and_reload(context, armature, rig_settings):
    """Refresh variant states and reload any affected equipment slots."""
    global _VARIANT_REFRESHING
    if _VARIANT_REFRESHING:
        return
    _VARIANT_REFRESHING = True
    try:
        slots = rig_settings.equipment_slots
        before_templates = [get_effective_equip_template(slot) for slot in slots]
        before_active = [bool(getattr(slot, "variant_active", False)) for slot in slots]

        refresh_variant_states(rig_settings)

        saved_active = context.view_layer.objects.active
        saved_selection = [obj for obj in context.selected_objects]
        for i, slot in enumerate(slots):
            after_template = get_effective_equip_template(slot)
            after_active = bool(getattr(slot, "variant_active", False))
            if slot.is_loaded and (before_templates[i] != after_template or before_active[i] != after_active):
                load_equipment_item(context, armature, i, rig_settings)
        _safe_restore_selection(saved_active, saved_selection)
    finally:
        _VARIANT_REFRESHING = False

def _load_bound_items(context, armature, rig_settings, slot_index, slot, parent_objects, parent_empty, slot_empty,
                      target_armature=None, prepared_context=None, imported_template_keys=None):
    bound_items = _safe_json_list(getattr(slot, "bound_items_json", ""))
    if not bound_items:
        return []

    prepared = _prepare_equipment_load_context(armature, rig_settings, prepared_context)
    entity = prepared.get("entity")
    appearance = prepared.get("appearance")
    if entity is None:
        return []

    parent_root = None
    if parent_objects:
        for obj in parent_objects:
            if obj.parent is None or obj.parent == parent_empty:
                parent_root = obj
                break
        if parent_root is None:
            parent_root = list(parent_objects)[0]

    target_armature = _get_slot_target_armature(slot_empty, target_armature or armature)
    source_roots = prepared.get("source_roots", [])
    source_game = get_equipment_source_game_for_search_roots(source_roots)

    loaded = []
    seen_template_keys = set(imported_template_keys or ())
    for bound_name in bound_items:
        template = _resolve_bound_item_template(bound_name, source_roots)
        final_item, export_path, _search_pattern = _resolve_bundle_item_by_template_cached(
            template,
            search_roots=source_roots,
            prepared_context=prepared,
        )
        if not final_item:
            log.warning(f"Bound item not found for template: {template}")
            continue

        current_template_keys = set()
        current_template_keys.update(_template_match_keys(bound_name))
        current_template_keys.update(_template_match_keys(template))
        current_template_keys.update(_template_match_keys(getattr(final_item, "name", "")))
        if seen_template_keys.intersection(current_template_keys):
            continue

        # Bound items may provide an explicit slot target, but their attachment
        # behavior still comes from the parsed entity graph, not attachment_type.
        attrs = {}
        bound_equip_slot = ""
        try:
            attrs = _lookup_item_attributes(bound_name, source_game)
            if not attrs and template and template != bound_name:
                attrs = _lookup_item_attributes(template, source_game)
            bound_equip_slot = attrs.get("equip_slot", "")
        except Exception:
            attrs = {}
            bound_equip_slot = ""

        bound_slot_empty = None
        if bound_equip_slot:
            bound_slot_empty = find_slot_empty(rig_settings.entity_name, bound_equip_slot, armature)
            if not bound_slot_empty:
                bound_slot_empty = _ensure_slot_empty_from_rig(bound_equip_slot, armature, rig_settings)
            if not bound_slot_empty and slot_empty and slot_empty.get("witcher_slot_name") == bound_equip_slot:
                bound_slot_empty = slot_empty
            if not bound_slot_empty:
                log.warning(
                    f"Bound item '{bound_name}' requested slot '{bound_equip_slot}' but no slot empty was found"
                )

        bound_item_entity = _get_cached_equipment_item_entity(
            export_path,
            prepared_context=prepared,
        )
        bound_attachment_profile = import_entity.classify_equipment_attachment_profile(bound_item_entity)
        _maybe_log_legacy_attachment_type_conflict(
            bound_name or template,
            attrs.get("attachment_type", ""),
            bound_attachment_profile,
        )
        if getattr(bound_attachment_profile, "kind", "") == "inventory_wrapper":
            seen_template_keys.update(current_template_keys)
            continue

        bound_target_info = {
            "name": bound_equip_slot,
            "is_valid": bool(bound_slot_empty),
            "target_type": "slot" if bound_slot_empty else "",
            "slot_empty": bound_slot_empty,
            "armature": target_armature,
            "bone_name": "",
        }
        allow_bound_unmounted_visual = (
            not bound_slot_empty
            and getattr(bound_attachment_profile, "kind", "") != "owner_graph"
        )
        bound_mount_strategy = _infer_equipment_mount_strategy(
            bound_attachment_profile,
            bound_target_info,
            allow_unmounted_visual=allow_bound_unmounted_visual,
        )
        bound_bind_armature = _resolve_bound_owner_bind_armature(
            slot,
            armature,
            rig_settings,
            bound_attachment_profile,
            bound_mount_strategy,
            bound_equip_slot,
            target_armature,
        )
        bound_use_skinning_bridge = _should_use_bound_skinning_bridge(
            bound_attachment_profile,
            bound_mount_strategy,
            bound_equip_slot,
            bound_bind_armature,
        )
        log.debug(
            "Bound equipment attachment '%s': profile=%s strategy=%s target=%s bind_armature=%s skinning_bridge=%s",
            bound_name or template,
            getattr(bound_attachment_profile, "kind", "") or "unknown",
            bound_mount_strategy,
            _describe_mount_target(bound_target_info),
            getattr(bound_bind_armature, "name", ""),
            bound_use_skinning_bridge,
        )

        # Create a visible group for non-slot bound visuals that are not owner-bound.
        bound_group = None
        if not bound_slot_empty and bound_mount_strategy != "owner_graph_bound":
            bpy.ops.object.empty_add(type="PLAIN_AXES", radius=0.5)
            bound_group = bpy.context.object
            bound_group.name = f"{bound_name}_bound" if bound_name else "bound_item"
            bound_group["witcher_bound_parent_guid"] = slot.equip_guid
            bound_group["witcher_bound_item_name"] = bound_name
            bound_group["witcher_equip_guid"] = slot.equip_guid

            if parent_root:
                bound_group.parent = parent_root
            elif parent_empty:
                bound_group.parent = parent_empty
            else:
                bound_group.parent = armature

        before = set(bpy.data.objects)
        saved_world = _temp_reset_armature_world(armature)
        changed_poses = _set_pose_all_armatures(armature, "REST")
        try:
            import_info = _import_item_entity(
                export_path,
                final_item.name,
                entity,
                bound_bind_armature,
                appearance,
                slot_index,
                parent_empty,
                use_app_drivers=_slot_uses_appearance_drivers(slot),
                prepared_context=prepared,
                attachment_profile=bound_attachment_profile,
                bind_root_chunks_to_entity=(
                    False if bound_use_skinning_bridge else _should_bind_root_chunks_to_entity(
                        bound_attachment_profile,
                        bound_mount_strategy,
                    )
                ),
            )
        finally:
            _restore_pose_all_armatures(changed_poses)
            _restore_armature_world(armature, saved_world)

        new_objects = set(bpy.data.objects) - before
        if not new_objects:
            continue

        # Tag with parent equipment GUID so unload removes them
        for obj in new_objects:
            obj["witcher_equip_guid"] = slot.equip_guid
            obj["witcher_bound_parent_guid"] = slot.equip_guid
            obj["witcher_bound_item_name"] = bound_name

        try:
            import_entity.initialize_imported_entity_armatures(
                new_objects,
                import_info.get("item_entity"),
                filename=export_path,
                selected_appearance_name=import_info.get("selected_appearance_name", ""),
                update_json=True,
                context_role="auxiliary",
            )
        except Exception as e:
            log.warning("Failed to initialize bound equipment entity state for '%s': %s", bound_name, e)

        # Apply parenting/attachment rules
        roots = _collect_mount_roots(new_objects, ignored_objects={parent_empty, bound_group})
        if bound_use_skinning_bridge:
            _attach_imported_objects_via_skinning(new_objects, bound_bind_armature)
        elif bound_mount_strategy == "slot_mount_animated" and bound_slot_empty:
            bound_anchor = _mount_animated_roots_with_anchor(
                roots,
                slot.equip_guid,
                "bound",
                parent_empty,
                slot_empty=bound_slot_empty,
                armature=armature,
                bound_parent_guid=slot.equip_guid,
                bound_item_name=bound_name,
            )
            if bound_anchor is not None:
                new_objects.add(bound_anchor)
        elif bound_mount_strategy == "slot_mount_static" and bound_slot_empty:
            for root in roots:
                mount_equipment_to_slot(root, bound_slot_empty, armature, snap=True)
        elif bound_group:
            # Parent root objects under the bound group
            for root in roots:
                root.parent = bound_group

        seen_template_keys.update(current_template_keys)
        seen_template_keys.update(import_info.get("template_keys", []))
        loaded.extend(list(new_objects))

    return loaded


# =============================================================================
# Entity Slot Utility Functions
# =============================================================================

def find_slot_empty(entity_name, slot_name, armature=None):
    """Find the Empty object for a given slot name.
    
    Args:
        entity_name: Name of the entity (e.g., 'player')
        slot_name: Name of the slot (e.g., 'silver_sword_back_slot')
        armature: Optional armature to scope the search (recommended for duplicates)
        
    Returns:
        The Empty object for the slot, or None if not found
    """
    if armature:
        # Prefer slots parented under this armature instance.
        arm_name = getattr(armature, "name_full", getattr(armature, "name", ""))
        full_name = f"{entity_name}:{slot_name}" if entity_name else slot_name
        candidates = []
        for obj in armature.children_recursive:
            if obj.type != 'EMPTY':
                continue
            obj_slot_name = str(obj.get("witcher_slot_name") or "").strip()
            name_matches = obj.name == full_name or obj.name.startswith(f"{full_name}.")
            if obj_slot_name != slot_name and not name_matches:
                continue
            score = 0
            if obj_slot_name == slot_name:
                score += 8
            if obj.get("witcher_owner_armature") == arm_name:
                score += 4
            if obj.parent and obj.parent.get("witcher_slots_parent"):
                score += 2
            if entity_name and obj.get("witcher_entity_name") == entity_name:
                score += 1
            candidates.append((score, obj))
        if candidates:
            candidates.sort(key=lambda pair: pair[0], reverse=True)
            return candidates[0][1]

    # Fallback: Slot empties are named like "entity_name:slot_name"
    full_name = f"{entity_name}:{slot_name}"
    return bpy.data.objects.get(full_name)

def find_slot_empty_by_bone(entity_name, bone_name, rig_settings):
    """Find slot Empty that targets a specific bone.
    
    Enhanced to handle complex component hierarchies like scabbards_skeleton.
    
    Args:
        entity_name: Name of the entity
        bone_name: Name of the bone to find slot for
        rig_settings: The rig settings containing entity_slots
        
    Returns:
        The Empty object for a slot targeting that bone, or None
    """
    # Direct match first (for slots like r_weapon on main armature)
    for slot in rig_settings.entity_slots:
        if slot.bone_name == bone_name:
            return find_slot_empty(entity_name, slot.slot_name, bpy.context.object)
    
    # Component-based search for complex hierarchies (scabbards_skeleton, etc.)
    # Check if bone_name is part of a component name or vice versa
    for slot in rig_settings.entity_slots:
        if (slot.component_name and bone_name and 
            (slot.component_name in bone_name or bone_name in slot.component_name)):
            return find_slot_empty(entity_name, slot.slot_name, bpy.context.object)
        
        # Also check bone name patterns (silver_sword_back in silver_sword_back_slot)
        if (slot.slot_name and bone_name and 
            (bone_name in slot.slot_name or slot.slot_name in bone_name)):
            return find_slot_empty(entity_name, slot.slot_name, bpy.context.object)
    
    return None

def _capture_mount_local_offset(equipment_obj):
    if equipment_obj is None:
        return None
    try:
        parent = equipment_obj.parent
    except Exception:
        parent = None
    try:
        world_matrix = equipment_obj.matrix_world.copy()
    except Exception:
        return None
    if parent is None:
        return world_matrix
    try:
        return parent.matrix_world.inverted() @ world_matrix
    except Exception:
        try:
            return equipment_obj.matrix_local.copy()
        except Exception:
            return world_matrix


def mount_equipment_to_bone(equipment_obj, armature, bone_name, snap=True, preserve_local_offset=False):
    """Mount equipment object directly to a bone (no constraint).
    
    This avoids double-transforms and keeps hierarchy under the armature.
    """
    if not equipment_obj or not armature or not bone_name:
        return None
    
    if armature.type != 'ARMATURE':
        return None
    
    if bone_name not in armature.pose.bones:
        return None
    
    # Remove any existing mount constraints
    constraints_to_remove = [c for c in equipment_obj.constraints 
                            if c.name.startswith("Mount_") or c.name.startswith("Equip_")]
    for c in constraints_to_remove:
        equipment_obj.constraints.remove(c)

    local_offset = _capture_mount_local_offset(equipment_obj) if preserve_local_offset else None

    # Parent directly to bone
    equipment_obj.parent = armature
    equipment_obj.parent_type = 'BONE'
    equipment_obj.parent_bone = bone_name
    try:
        dg = bpy.context.evaluated_depsgraph_get()
        arm_eval = armature.evaluated_get(dg)
        equipment_obj.matrix_parent_inverse = arm_eval.matrix_world.inverted()
    except Exception:
        try:
            equipment_obj.matrix_parent_inverse = armature.matrix_world.inverted()
        except Exception:
            pass
    if hasattr(equipment_obj, "show_relationship_lines"):
        equipment_obj.show_relationship_lines = False

    if preserve_local_offset:
        try:
            dg = bpy.context.evaluated_depsgraph_get()
            arm_eval = armature.evaluated_get(dg)
            bone = arm_eval.pose.bones.get(bone_name)
            if bone and local_offset is not None:
                equipment_obj.matrix_world = (arm_eval.matrix_world @ bone.matrix) @ local_offset
        except Exception:
            bone = armature.pose.bones.get(bone_name)
            if bone and local_offset is not None:
                equipment_obj.matrix_world = (armature.matrix_world @ bone.matrix) @ local_offset
    elif snap:
        # Snap to evaluated bone world matrix (handles moved armature)
        try:
            dg = bpy.context.evaluated_depsgraph_get()
            arm_eval = armature.evaluated_get(dg)
            bone = arm_eval.pose.bones.get(bone_name)
            if bone:
                equipment_obj.matrix_world = arm_eval.matrix_world @ bone.matrix
        except Exception:
            bone = armature.pose.bones.get(bone_name)
            if bone:
                equipment_obj.matrix_world = armature.matrix_world @ bone.matrix

    return True


def mount_equipment_to_slot(equipment_obj, slot_empty, parent_armature=None, snap=True, preserve_local_offset=False):
    """Mount equipment object directly under a slot Empty (no constraint)."""
    if not equipment_obj or not slot_empty:
        return None
    
    # Remove any existing mount constraints
    constraints_to_remove = [c for c in equipment_obj.constraints 
                            if c.name.startswith("Mount_") or c.name.startswith("Equip_")]
    for c in constraints_to_remove:
        equipment_obj.constraints.remove(c)

    local_offset = _capture_mount_local_offset(equipment_obj) if preserve_local_offset else None

    # Parent under slot empty
    equipment_obj.parent = slot_empty
    equipment_obj.parent_type = 'OBJECT'
    # Use evaluated slot transform for correct parent inverse
    slot_matrix = slot_empty.matrix_world
    try:
        dg = bpy.context.evaluated_depsgraph_get()
        slot_eval = slot_empty.evaluated_get(dg)
        slot_matrix = slot_eval.matrix_world
    except Exception:
        pass
    try:
        equipment_obj.matrix_parent_inverse = slot_matrix.inverted()
    except Exception:
        pass
    if hasattr(equipment_obj, "show_relationship_lines"):
        equipment_obj.show_relationship_lines = False

    if preserve_local_offset and local_offset is not None:
        equipment_obj.matrix_world = slot_matrix @ local_offset
    elif snap:
        # Snap to evaluated slot world matrix (handles moved armature)
        equipment_obj.matrix_world = slot_matrix

    return True

def refresh_slot_constraints(armature):
    """Refresh slot Empty constraints for sub-component armatures.
    
    Call this after all components (like scabbards_skeleton) are imported
    to update slots that couldn't find their target armature during initial import.
    
    Args:
        armature: The root armature with rig_settings
    """
    import json
    from ..importers.import_entity import set_empty_bone_offset
    
    if not armature or armature.type != 'ARMATURE':
        return 0
    
    rig_settings = armature.data.witcherui_RigSettings
    entity_name = rig_settings.entity_name
    updated_count = 0
    
    def get_root_bone_name(arm_obj):
        if not arm_obj or arm_obj.type != 'ARMATURE':
            return None
        for b in arm_obj.data.bones:
            if b.parent is None:
                return b.name
        return None

    arm_name = getattr(armature, "name_full", getattr(armature, "name", ""))
    local_armatures = [armature]
    for obj in armature.children_recursive:
        if obj.type == 'ARMATURE':
            local_armatures.append(obj)

    for slot in rig_settings.entity_slots:
        slot_empty = find_slot_empty(entity_name, slot.slot_name, armature)
        if not slot_empty:
            continue
        # Ensure slot metadata exists for robust lookup
        slot_empty["witcher_slot_name"] = slot.slot_name or ""
        slot_empty["witcher_entity_name"] = entity_name or ""
        slot_empty["witcher_owner_armature"] = arm_name
        
        component_name = slot.component_name
        bone_name = slot.bone_name
        
        # Find the correct armature for this component
        target_armature = None
        if component_name:
            # Restrict search to this armature hierarchy so repeated imports of
            # the same entity cannot bind slots to an older instance.
            for obj in local_armatures:
                if obj.type == 'ARMATURE' and obj.get('witcher_name') == component_name:
                    target_armature = obj
                    break
            if target_armature is None:
                for obj in local_armatures:
                    if obj.type != 'ARMATURE':
                        continue
                    if obj.name == f"{entity_name}:{component_name}" or obj.name == component_name:
                        target_armature = obj
                        break
                    if component_name in obj.name and obj.name.startswith(entity_name):
                        target_armature = obj
                        break
        
        if target_armature is None:
            target_armature = armature  # Fallback to root

        # If no bone specified, follow root bone of main armature
        if not bone_name:
            root_bone = get_root_bone_name(armature)
            if root_bone:
                target_armature = armature
                bone_name = root_bone
        
        # Check if constraint already points to correct armature
        needs_update = True
        desired_subtarget = bone_name if bone_name and bone_name in target_armature.pose.bones else ''
        slot_constraints = [c for c in slot_empty.constraints if c.type in {'COPY_TRANSFORMS', 'CHILD_OF'}]
        for constraint in slot_constraints:
            if (constraint.type == 'COPY_TRANSFORMS'
                    and constraint.name == "W2_SLOT"
                    and constraint.target == target_armature
                    and (constraint.subtarget or '') == desired_subtarget):
                # If we already have a correct W2_SLOT but there are duplicates, reapply
                if len(slot_constraints) == 1:
                    needs_update = False
                break
        
        if needs_update:
            # Apply new constraint with transform data
            try:
                transform_data = json.loads(slot.transform_json) if slot.transform_json else None
            except Exception:
                transform_data = None
            
            use_rot90 = get_rig_rot90_enabled(rig_settings, default=False)
            rot90_dir = 1
            set_empty_bone_offset(slot_empty, target_armature, bone_name, transform_data,
                                  rotate_90=use_rot90, rotate_90_dir=rot90_dir)
            updated_count += 1
    
    return updated_count

# =============================================================================
# Per-Appearance Visibility Helpers
# =============================================================================

def get_hidden_in_appearance(slot, appearance_name):
    """Get hidden state for a specific appearance. Returns True if hidden, False if visible."""
    try:
        hidden_dict = json.loads(slot.hidden_in_appearances or "{}")
    except json.JSONDecodeError:
        hidden_dict = {}
    return hidden_dict.get(appearance_name, False)


def set_hidden_in_appearance(slot, appearance_name, hidden):
    """Set hidden state for a specific appearance."""
    try:
        hidden_dict = json.loads(slot.hidden_in_appearances or "{}")
    except json.JSONDecodeError:
        hidden_dict = {}
    hidden_dict[appearance_name] = hidden
    slot.hidden_in_appearances = json.dumps(hidden_dict)


def get_current_appearance_name(rig_settings):
    """Get the name of the currently selected appearance."""
    if rig_settings.app_list_index >= 0 and len(rig_settings.app_list) > rig_settings.app_list_index:
        return rig_settings.app_list[rig_settings.app_list_index].name
    return ""


def _get_coloring_entries_for_appearance(entity_data, appearance_name):
    """Return entity-level coloringEntries for the selected appearance."""
    if not appearance_name or entity_data is None:
        return []
    if isinstance(entity_data, dict):
        entries = entity_data.get("coloringEntries", [])
    else:
        entries = getattr(entity_data, "coloringEntries", [])
    if not isinstance(entries, list):
        return []
    filtered = []
    for entry in entries:
        if isinstance(entry, dict):
            entry_appearance = entry.get("appearance", "")
            component_name = entry.get("componentName", "")
        else:
            entry_appearance = getattr(entry, "appearance", "")
            component_name = getattr(entry, "componentName", "")
        if str(entry_appearance) != str(appearance_name):
            continue
        filtered.append(entry)
    filtered.sort(
        key=lambda e: str(
            e.get("componentName", "") if isinstance(e, dict) else getattr(e, "componentName", "")
        ).lower()
    )
    return filtered


def _format_color_shift_summary(shift_data):
    if not shift_data:
        return "None"
    if isinstance(shift_data, dict):
        hue = shift_data.get('hue', 0)
        saturation = shift_data.get('saturation', 0)
        luminance = shift_data.get('luminance', 0)
    else:
        hue = getattr(shift_data, 'hue', 0)
        saturation = getattr(shift_data, 'saturation', 0)
        luminance = getattr(shift_data, 'luminance', 0)
    return (
        f"H:{hue} "
        f"S:{saturation} "
        f"L:{luminance}"
    )


def template_belongs_to_appearance(slot, appearance_name):
    """Check if this template belongs to the given appearance."""
    if not slot.appearance_names:
        return False
    app_names = set(slot.appearance_names.split(','))
    app_names.discard('')
    return appearance_name in app_names

# Updated default categories with equip_template strings
default_categories = {
    "pants": [("None", "None", ""), ("Body underwear 01", "Body underwear 01", "l_01_mg__body_underwear")],
    "armor": [("None", "None", ""), ("Body torso 01", "Body torso 01", "t_01_mg__body")],
    "gloves": [("None", "None", ""), ("Body palms 01", "Body palms 01", "g_01_mg__body")],
    "boots": [("None", "None", ""), ("Body feet 01", "Body feet 01", "s_01_mg__body")],
    "steelsword": [("None", "None", "")],
    "silversword": [("None", "None", "")],
    "crossbow": [("None", "None", "")],
    "head": [("None", "None", ""), ("head_2", "head_2", "h_02_mg__geralt")],
    "hair": [("None", "None", ""), ("Preview Hair", "Preview Hair", "c_01b_mg__witcher")]
}

# Define the EquipmentDefinitionEntry property group
class EquipmentDefinitionEntry(bpy.types.PropertyGroup):
    # Class variable to store all categories and items, including those from XML
    category_items = default_categories.copy()
    item_attributes = {}  # New dictionary to store attributes for each item

    # Use property getters and setters for instance_items
    @property
    def instance_items(self):
        if not hasattr(self, '_instance_items'):
            self._instance_items = {}
        return self._instance_items

    @instance_items.setter
    def instance_items(self, value):
        self._instance_items = value

    # Helper to retrieve available items for the current category.
    # NOT an EnumProperty callback — EnumProperty on CollectionProperty items
    # causes hard segfaults because Blender holds C pointers to the returned
    # tuples which Python garbage-collects between per-row draw calls.
    def get_default_items(self, context):
        try:
            sg = getattr(self, "source_game", "") or "w3"
            cat = str(getattr(self, "category", "") or "None")
            category_items, _item_attributes = _get_equipment_catalog(sg)
            items = category_items.get(cat, [])
            instance_items = self.instance_items.get(cat, [])
            seen = set()
            unique_items = []
            for item in items + instance_items:
                if item[0] not in seen:
                    unique_items.append(item)
                    seen.add(item[0])
            result = [(name, name, "") for name, _display, _tpl in unique_items]
            if not result:
                return [("None", "None", "")]
            if result[0][0] != "None" and "None" not in {r[0] for r in result}:
                result.insert(0, ("None", "None", ""))
            return result
        except Exception:
            return [("None", "None", "")]

    def _sync_to_rig(self, context):
        """Sync current entry values to persistent equipment slot on the armature."""
        temp_data = _get_temp_equipment_data(context)
        if temp_data is not None and getattr(temp_data, "suspend_auto_apply_updates", False):
            return
        try:
            _armature, rig_settings = _get_armature_and_rig_settings(context)
            if rig_settings:
                target_slot = None
                slot_index = int(getattr(self, "slot_index", -1))
                if 0 <= slot_index < len(rig_settings.equipment_slots):
                    target_slot = rig_settings.equipment_slots[slot_index]
                else:
                    for slot in rig_settings.equipment_slots:
                        if slot.category == self.category:
                            target_slot = slot
                            break
                if target_slot:
                    target_slot.source_game = _normalize_source_game(
                        getattr(self, "source_game", "") or getattr(target_slot, "source_game", "w3")
                    )
                    target_slot.category = self.category
                    target_slot.item_name = self.defaultItemName
                    target_slot.equip_template = self.equip_template
                    target_slot.base_equip_template = self.equip_template
                    target_slot.resolved_repo_path = ""
                    if slot_index >= 0 and not getattr(target_slot, "is_inventory", False):
                        target_slot.keep_across_appearances = True
                    target_slot.equip_slot = self.equip_slot
                    target_slot.hold_slot = self.hold_slot
                    target_slot.weapon = self.weapon
                    target_slot.attachment_type = self.attachment_type
                    target_slot.variants_json = self.variants_json
                    target_slot.bound_items_json = self.bound_items_json
                    try:
                        refresh_variant_states(rig_settings)
                    except Exception:
                        pass
        except Exception:
            # Blender may block ID writes in some UI contexts
            pass

    def _sync_template_and_repo(self, context):
        self._sync_to_rig(context)
        _update_entry_resolved_repo_path(self, context)

    def _auto_apply_selection_change(self, context):
        if not _is_temp_equipment_auto_apply_enabled(context):
            return

        try:
            armature, rig_settings = _get_armature_and_rig_settings(context)
        except Exception:
            armature, rig_settings = None, None
        if not armature or not rig_settings:
            return

        try:
            slot_index = int(getattr(self, "slot_index", -1))
        except Exception:
            slot_index = -1
        if slot_index < 0 or slot_index >= len(rig_settings.equipment_slots):
            return

        saved_active = None
        saved_selection = []
        try:
            saved_active = context.view_layer.objects.active
            saved_selection = [obj for obj in context.selected_objects]
        except Exception:
            pass

        slot = rig_settings.equipment_slots[slot_index]
        try:
            effective_template = get_effective_equip_template(slot)
            if not self.defaultItemName or self.defaultItemName == "None" or not effective_template or effective_template == "None":
                unload_equipment_item(slot)
                try:
                    _refresh_variants_and_reload(context, armature, rig_settings)
                except Exception:
                    pass
                return

            try:
                refresh_slot_constraints(armature)
            except Exception:
                pass

            with mod_loading_context(context):
                loaded = load_equipment_item(context, armature, slot_index, rig_settings)
            if not loaded:
                reason = _get_last_equipment_load_failure(armature, slot_index) or "Unknown failure"
                log.warning(
                    "Auto-apply equipment selection failed for slot %d (%s): %s",
                    slot_index,
                    getattr(slot, "item_name", "") or "<no item>",
                    reason,
                )
        except Exception:
            log.warning("Auto-apply equipment selection failed", exc_info=True)
        finally:
            _safe_restore_selection(saved_active, saved_selection)

    # Update the equip_template and other attributes when a new item is selected
    def update_item_attributes(self, context):
        sg = getattr(self, "source_game", "") or "w3"
        category_items, item_attributes = _get_equipment_catalog(sg)
        # Find the selected item in the combined items and update attributes
        items = category_items.get(self.category, [])
        instance_items = self.instance_items.get(self.category, [])
        combined_items = items + instance_items

        for item_name, _, equip_template in combined_items:
            if item_name == self.defaultItemName:
                self.equip_template = equip_template
                # Update additional attributes from item_attributes dictionary
                attributes = item_attributes.get(item_name, {})
                self.equip_slot = attributes.get('equip_slot', '')
                self.hold_slot = attributes.get('hold_slot', '')
                self.weapon = attributes.get('weapon', False)
                self.attachment_type = attributes.get('attachment_type', '')
                variants = attributes.get('variants', [])
                bound_items = attributes.get('bound_items', [])
                tags = attributes.get('tags', [])
                try:
                    self.variants_json = json.dumps(variants, indent=2)
                except Exception:
                    self.variants_json = "[]"
                try:
                    self.bound_items_json = json.dumps(bound_items, indent=2)
                except Exception:
                    self.bound_items_json = "[]"
                self.variants_summary = _format_variant_summary(variants)
                self.bound_items_summary = _format_bound_items_summary(bound_items)
                if isinstance(tags, str):
                    tags = _split_tags(tags)
                try:
                    self.tags_summary = ", ".join([str(t) for t in tags if t])
                except Exception:
                    self.tags_summary = ""
                break

        # Sync to persistent EquipmentSlotEntry on the armature
        self._sync_to_rig(context)
        _update_entry_resolved_repo_path(self, context)
        self._auto_apply_selection_change(context)

    # Helper to retrieve all known categories for the entry's source game.
    def get_category_items(self, context):
        try:
            sg = getattr(self, "source_game", "") or "w3"
            category_items, _item_attributes = _get_equipment_catalog(sg)
            seen = set()
            items = []
            for key in category_items.keys():
                if key not in seen:
                    items.append((key, key, ""))
                    seen.add(key)
            if not items:
                items = [("None", "None", "")]
            return items
        except Exception:
            return [("None", "None", "")]

    def _on_category_changed(self, context):
        """Reset item to 'None' (or the first available item) when the category changes."""
        items = self.get_default_items(context)
        first = items[0][0] if items else "None"
        changed_default = False
        # Only write if it would actually change to avoid recursive update triggers
        try:
            if self.defaultItemName != first:
                self.defaultItemName = first
                changed_default = True
        except Exception:
            pass
        if not changed_default:
            try:
                self.update_item_attributes(context)
            except Exception:
                pass
        try:
            if context and context.area:
                context.area.tag_redraw()
        except Exception:
            pass

    # Which catalog this entry uses — set before category/defaultItemName
    # so that get_category_items / get_default_items pick the right catalog.
    source_game: bpy.props.StringProperty(default="w3")

    # NOTE: These MUST be StringProperty, NOT EnumProperty.
    # EnumProperty with dynamic items on a PropertyGroup inside a
    # CollectionProperty causes hard segfaults in Blender 4.x.  Blender's
    # C/RNA layer holds raw pointers to the Python strings returned by the
    # items callback.  When template_list draws N rows it calls the callback
    # N times, each call overwrites the previous row's items → dangling
    # C pointers → instant crash.  This is a documented, unfixable Blender
    # API limitation.  Use the search-popup operators for dropdown UX.
    category: bpy.props.StringProperty(
        name="Category",
        default="None",
        update=_on_category_changed,
    )

    defaultItemName: bpy.props.StringProperty(
        name="Default Item Name",
        default="None",
        update=update_item_attributes,
    )

    # Store the selected equip_template for the current item
    equip_template: bpy.props.StringProperty(
        name="Equip Template",
        description="Equip template associated with the selected item",
        update=lambda self, context: self._sync_template_and_repo(context)
    )

    resolved_repo_path: bpy.props.StringProperty(
        name="Resolved Game Path",
        description="Resolved game-relative repo path for the currently selected equipment template",
        default=""
    )

    resolved_abs_path: bpy.props.StringProperty(
        name="Resolved Absolute Path",
        description="Resolved absolute file path for the currently selected equipment template",
        default=""
    )

    # Additional attributes
    equip_slot: bpy.props.StringProperty(
        name="Equip Slot",
        description="Equip slot of the item",
        update=lambda self, context: self._sync_to_rig(context)
    )
    hold_slot: bpy.props.StringProperty(
        name="Hold Slot",
        description="Hold slot of the item",
        update=lambda self, context: self._sync_to_rig(context)
    )
    # Add other properties as needed
    # For example:
    weapon: bpy.props.BoolProperty(
        name="Weapon",
        description="Is this item a weapon",
        default=False
    )

    attachment_type: bpy.props.StringProperty(
        name="Attachment Type",
        description="Legacy/debug attachment metadata; runtime attachment is inferred from the item entity graph",
        default=""
    )

    variants_json: bpy.props.StringProperty(
        name="Variants JSON",
        description="Raw variants data from XML (JSON)",
        default="[]"
    )

    bound_items_json: bpy.props.StringProperty(
        name="Bound Items JSON",
        description="Raw bound items data from XML (JSON)",
        default="[]"
    )

    variants_summary: bpy.props.StringProperty(
        name="Variants",
        description="Summary of variant rules",
        default=""
    )

    bound_items_summary: bpy.props.StringProperty(
        name="Bound Items",
        description="Summary of bound items",
        default=""
    )

    tags_summary: bpy.props.StringProperty(
        name="Tags",
        description="Summary of tags",
        default=""
    )

    # Property to store reference to the item in the scene
    item_object: bpy.props.PointerProperty(
        name="Item Object",
        type=bpy.types.Object
    )

    slot_index: bpy.props.IntProperty(
        name="Slot Index",
        default=-1,
        options={'HIDDEN'}
    )

    # Toggle value
    toggle_value: bpy.props.BoolProperty(
        name="Toggle Value",
        description="Toggle value to manipulate the item",
        default=False,
        update=lambda self, context: self.toggle_item(context)
    )

    # Method to manipulate the item when toggle_value changes
    def toggle_item(self, context):
        if self.item_object:
            if self.toggle_value:
                # Perform manipulation, e.g., show the item
                self.item_object.hide_set(False)
            else:
                # Hide the item
                self.item_object.hide_set(True)

# Custom JSON encoder
class CustomJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        # Skip specific types
        if hasattr(obj, 'theType') and obj.theType in ['CGUID', 'EPathEngineCollision']:
            return None
        # Serialize objects using their __dict__ attribute
        try:
            return obj.__dict__
        except AttributeError:
            return super().default(obj)

# New property group for Included Templates
class IncludedTemplateEntry(bpy.types.PropertyGroup):
    # Store the entire included template data as a JSON string
    data: bpy.props.StringProperty(
        name="Included Template Data",
        description="JSON data of the included template"
    )

    # Expose templateFilename for UI editing
    templateFilename: bpy.props.StringProperty(
        name="Template Filename",
        description="Filename of the included template"
    )

    # Expose ns for UI (optional)
    ns: bpy.props.StringProperty(
        name="Namespace",
        description="Namespace of the included template"
    )

    # Method to update the included template data when requested
    def update_template_data(self, context):
        if self.templateFilename:
            try:
                # Call LoadCEntityTemplateFile to get the template data
                (template_data, entity) = LoadCEntityTemplateFile(self.templateFilename)
                # Serialize the data using the custom JSON encoder
                self.data = json.dumps(template_data, indent=2, cls=CustomJSONEncoder, sort_keys=False)
                # Optionally set ns from template_data
                if hasattr(template_data, 'ns'):
                    self.ns = template_data.ns
            except Exception as e:
                self.data = json.dumps({'templateFilename': self.templateFilename}, indent=2)
        else:
            # Clear the data if templateFilename is empty
            self.data = ''

# Temporary data storage in WindowManager
class WitcherUITempData(bpy.types.PropertyGroup):
    equipment_entries: bpy.props.CollectionProperty(type=EquipmentDefinitionEntry)
    equipment_entries_index: bpy.props.IntProperty()
    last_app_list_index: bpy.props.IntProperty(default=-1)
    last_armature_name: bpy.props.StringProperty(default="")
    last_entity_state_token: bpy.props.StringProperty(default="")
    equipment_source_game: bpy.props.StringProperty(default="w3")
    auto_apply_equipment_selection: bpy.props.BoolProperty(
        name="Auto Apply Dropdown Changes",
        description="Immediately unload and reload the edited equipment slot when you change its category or item selection. This affects the current scene only",
        default=False,
    )
    suspend_auto_apply_updates: bpy.props.BoolProperty(default=False, options={'HIDDEN'})

    included_template_entries: bpy.props.CollectionProperty(type=IncludedTemplateEntry)
    included_template_entries_index: bpy.props.IntProperty()

# Define the UI list to display equipment categories
class EQUIPMENT_UL_CategoryList(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        # Small dot at the far-left gives a click-target for row selection that
        # doesn't accidentally activate either text field.
        split = layout.split(factor=0.44, align=True)
        left = split.row(align=True)
        left.label(text="", icon='LAYER_USED')
        op = left.operator("witcher.equipment_search_category",
                           text=getattr(item, "category", "") or "None")
        op.entry_index = index
        right = split.row(align=True)
        op = right.operator("witcher.equipment_search_default_item",
                            text=getattr(item, "defaultItemName", "") or "None")
        op.entry_index = index


class EQUIPMENT_OT_SearchCategory(bpy.types.Operator):
    bl_idname = "witcher.equipment_search_category"
    bl_label = "Search Category"
    bl_description = "Search and pick an equipment category"
    bl_property = "category"

    entry_index: bpy.props.IntProperty(default=-1, options={'HIDDEN'})

    def _enum_categories(self, context):
        temp_data = getattr(context.window_manager, "witcherui_temp_data", None)
        if temp_data and 0 <= self.entry_index < len(temp_data.equipment_entries):
            entry = temp_data.equipment_entries[self.entry_index]
            try:
                items = entry.get_category_items(context)
                if items:
                    cache_key = (
                        "category",
                        int(self.entry_index),
                        _get_temp_source_game(context),
                    )
                    return _cache_operator_enum_items(cache_key, items)
            except Exception:
                pass
        return [("None", "None", "")]

    category: bpy.props.EnumProperty(name="Category", items=_enum_categories)

    def invoke(self, context, event):
        temp_data = getattr(context.window_manager, "witcherui_temp_data", None)
        if temp_data and 0 <= self.entry_index < len(temp_data.equipment_entries):
            current = temp_data.equipment_entries[self.entry_index].category
            if current:
                try:
                    self.category = current
                except Exception:
                    pass
        context.window_manager.invoke_search_popup(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        temp_data = getattr(context.window_manager, "witcherui_temp_data", None)
        if not temp_data or not (0 <= self.entry_index < len(temp_data.equipment_entries)):
            return {'CANCELLED'}
        entry = temp_data.equipment_entries[self.entry_index]
        try:
            entry.category = self.category
        except Exception:
            return {'CANCELLED'}
        return {'FINISHED'}


class EQUIPMENT_OT_SearchDefaultItem(bpy.types.Operator):
    bl_idname = "witcher.equipment_search_default_item"
    bl_label = "Search Default Item"
    bl_description = "Search and pick a default item for the selected category"
    bl_property = "item_name"

    entry_index: bpy.props.IntProperty(default=-1, options={'HIDDEN'})

    def _enum_default_items(self, context):
        temp_data = getattr(context.window_manager, "witcherui_temp_data", None)
        if temp_data and 0 <= self.entry_index < len(temp_data.equipment_entries):
            entry = temp_data.equipment_entries[self.entry_index]
            try:
                items = entry.get_default_items(context)
                if items:
                    cache_key = (
                        "default_item",
                        int(self.entry_index),
                        str(getattr(entry, "category", "") or "None"),
                        _get_temp_source_game(context),
                    )
                    return _cache_operator_enum_items(cache_key, items)
            except Exception:
                pass
        return [("None", "None", "")]

    item_name: bpy.props.EnumProperty(name="Default Item", items=_enum_default_items)

    def invoke(self, context, event):
        temp_data = getattr(context.window_manager, "witcherui_temp_data", None)
        if temp_data and 0 <= self.entry_index < len(temp_data.equipment_entries):
            current = temp_data.equipment_entries[self.entry_index].defaultItemName
            if current:
                try:
                    self.item_name = current
                except Exception:
                    pass
        context.window_manager.invoke_search_popup(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        temp_data = getattr(context.window_manager, "witcherui_temp_data", None)
        if not temp_data or not (0 <= self.entry_index < len(temp_data.equipment_entries)):
            return {'CANCELLED'}
        entry = temp_data.equipment_entries[self.entry_index]
        try:
            entry.defaultItemName = self.item_name
        except Exception:
            return {'CANCELLED'}
        return {'FINISHED'}

# Function to extract categories and items from XML files, including additional attributes
def extract_categories_from_xml(folder_path):
    category_items = {}
    item_attributes = {}

    if not folder_path or not os.path.isdir(folder_path):
        return [], category_items, item_attributes

    for dirpath, dirnames, file_names in os.walk(folder_path):
        dirnames.sort()
        for file_name in sorted(file_names):
            if not file_name.lower().endswith(".xml"):
                continue
            file_path = os.path.join(dirpath, file_name)
            try:
                root = _parse_xml_root_with_fallbacks(file_path)
                source_game = _source_game_from_xml_path(file_path)

                for item in root.findall(".//item"):
                    category = item.get("category")
                    name = item.get("name")
                    equip_template = item.get("equip_template", "")
                    # Extract additional attributes
                    equip_slot = item.get("equip_slot", "")
                    hold_slot = item.get("hold_slot", "")
                    weapon = item.get("weapon", "false").lower() == "true"
                    attachment_type = item.get("attachment_type", "")
                    tags_text = ""
                    tags_node = item.find("tags")
                    if tags_node is not None and tags_node.text:
                        tags_text = tags_node.text
                    tags = _split_tags(tags_text)

                    # Parse variants
                    variants = []
                    variants_node = item.find("variants")
                    if variants_node is not None:
                        for var in variants_node.findall("variant"):
                            v_template = var.get("equip_template", "")
                            v_category = var.get("category", "")
                            v_equip_slot = var.get("equip_slot", "")
                            v_hold_slot = var.get("hold_slot", "")
                            if v_template or v_category:
                                variants.append({
                                    "equip_template": v_template,
                                    "category": v_category,
                                    "equip_slot": v_equip_slot,
                                    "hold_slot": v_hold_slot
                                })

                    # Parse bound items
                    bound_items = []

                    def _collect_bound_items(bound_items_node):
                        if bound_items_node is None:
                            return
                        for bi in bound_items_node.findall("item"):
                            if not bi.text:
                                continue
                            bi_name = bi.text.strip()
                            if bi_name and bi_name not in bound_items:
                                bound_items.append(bi_name)

                    _collect_bound_items(item.find("bound_items"))

                    player_override = item.find("player_override")
                    if player_override is not None:
                        _collect_bound_items(player_override.find("bound_items"))
                    # ... extract other attributes as needed

                    if category and name and equip_template:
                        # Initialize the category if it doesn't exist
                        if category not in category_items:
                            category_items[category] = [("None", "None", "")]

                        # Add item as a tuple without modifying names or adding suffixes
                        item_tuple = (name, name, equip_template)

                        # Only add if item is not already in the list for this category
                        if item_tuple not in category_items[category]:
                            category_items[category].append(item_tuple)

                        # Store attributes
                        item_attributes[name] = {
                            'item_name': name,
                            'category': category,
                            'equip_template': equip_template,
                            'equip_slot': equip_slot,
                            'hold_slot': hold_slot,
                            'weapon': weapon,
                            'attachment_type': attachment_type,
                            'variants': variants,
                            'bound_items': bound_items,
                            'tags': tags,
                            'source_game': source_game,
                            # ... store other attributes
                        }

            except (ET.ParseError, ValueError, UnicodeError) as e:
                log.warning("Error parsing XML %s (%s). Skipping file.", file_path, e)
                continue
            except Exception as e:
                log.warning("Unexpected error parsing XML %s (%s). Skipping file.", file_path, e)
                continue

    return sorted(category_items.keys()), category_items, item_attributes


def _flatten_bundle_item_candidates(items):
    if items is None:
        return []
    if not isinstance(items, list):
        return [items]
    flat = []
    stack = list(items)
    while stack:
        value = stack.pop(0)
        if isinstance(value, list):
            stack = list(value) + stack
            continue
        flat.append(value)
    return flat


def _select_final_bundle_item(items):
    flat = _flatten_bundle_item_candidates(items)
    for candidate in reversed(flat):
        if hasattr(candidate, "name"):
            return candidate
    return None


def _get_equipment_xml_bundle_cache_root():
    cache_root = Path(get_cache_root(create=True)) / "equipment_items_xml_bundle"
    cache_root.mkdir(parents=True, exist_ok=True)
    return str(cache_root)


def _extract_equipment_xmls_from_bundles():
    out_root = _get_equipment_xml_bundle_cache_root()

    # Early exit: if XML files are already extracted to the cache directory,
    # skip loading BundleManager entirely (expensive on every refresh).
    if os.path.isdir(out_root):
        for _dp, _dns, fnames in os.walk(out_root):
            if any(f.lower().endswith(".xml") for f in fnames):
                return out_root

    try:
        bundle_manager = LoadBundleManager()
    except Exception as e:
        log.warning("Failed to load bundle manager for equipment XMLs: %s", e)
        return ""

    items = getattr(bundle_manager, "Items", None) or {}
    if not items:
        return ""
    found = 0

    for key, value in items.items():
        rel_key = str(key or "").replace("/", "\\").lstrip("\\")
        rel_lower = rel_key.lower()
        if not rel_lower.endswith(".xml"):
            continue
        if "\\gameplay\\items\\" not in ("\\" + rel_lower):
            continue

        final_item = _select_final_bundle_item(value)
        if not final_item or not hasattr(final_item, "extract_to_file"):
            continue

        found += 1
        export_path = os.path.join(out_root, rel_key)
        export_dir = os.path.dirname(export_path)
        if export_dir:
            os.makedirs(export_dir, exist_ok=True)
        if not os.path.exists(export_path):
            try:
                final_item.extract_to_file(export_path)
            except Exception as e:
                log.warning("Failed to extract equipment XML '%s': %s", rel_key, e)

    if found == 0:
        return ""
    return out_root


def _get_equipment_xml_sources(context, addon_prefs):
    prefer_redkit = bool(getattr(addon_prefs, "prefer_redkit_equipment_xml", False))

    try:
        uncook_root = get_uncook_path(context)
    except Exception:
        uncook_root = ""
    uncook_items = os.path.join(uncook_root, "gameplay", "items") if uncook_root else ""

    bundle_items_root = _extract_equipment_xmls_from_bundles()

    redkit_depot = getattr(addon_prefs, "redkit_depot_path", "") or ""
    redkit_items = os.path.join(redkit_depot, "gameplay", "items") if redkit_depot else ""

    dev_override = get_dev_override("equipment_items_xml_root", "")

    ordered = []
    if prefer_redkit:
        ordered.extend([
            ("REDkit r4data", redkit_items),
            ("Uncook", uncook_items),
            ("Bundles", bundle_items_root),
        ])
    else:
        ordered.extend([
            ("Uncook", uncook_items),
            ("Bundles", bundle_items_root),
            ("REDkit r4data", redkit_items),
        ])

    if dev_override:
        ordered.append(("Dev Override", dev_override))

    # Deduplicate by normalized path while preserving order.
    result = []
    seen = set()
    for label, path in ordered:
        if not path:
            result.append((label, path, False))
            continue
        try:
            norm = os.path.normcase(os.path.normpath(path))
        except Exception:
            norm = path.lower()
        if norm in seen:
            continue
        seen.add(norm)
        result.append((label, path, os.path.isdir(path)))
    return result


def _merge_equipment_xml_data(target_categories, target_attributes, source_categories, source_attributes):
    for category, items in source_categories.items():
        if category not in target_categories:
            target_categories[category] = list(items)
            continue
        existing_items_set = set(tuple(item) for item in target_categories[category])
        for item in items:
            item_tuple = tuple(item)
            if item_tuple not in existing_items_set:
                target_categories[category].append(item)
                existing_items_set.add(item_tuple)

    # Preserve earlier sources as higher priority.
    for item_name, attrs in source_attributes.items():
        target_attributes.setdefault(item_name, attrs)


def _strip_duplicate_xml_attributes(xml_text):
    """Pre-process XML text to remove duplicate attributes, keeping the first occurrence.

    Some Witcher 3 gameplay XMLs contain duplicate attributes on a single element
    (e.g. ``player_level_min`` appearing twice on a ``<loot>`` tag), which is
    technically invalid XML that Python's ElementTree refuses to parse.  This
    function strips all but the first occurrence of any repeated attribute name so
    the file can be parsed normally.
    """
    import re as _re

    def _fix_tag(match):
        seen = set()

        def _keep_attr(m):
            name = m.group(1).lower()
            if name in seen:
                return ""
            seen.add(name)
            return m.group(0)

        # Match name="value" or name='value' pairs inside the tag
        return _re.sub(
            r'([\w\-\.:]+)\s*=\s*(?:"[^"]*"|\'[^\']*\')',
            _keep_attr,
            match.group(0),
        )

    return _re.sub(r'<[\w][^>]*>', _fix_tag, xml_text, flags=_re.DOTALL)


def _parse_xml_root_with_fallbacks(file_path):
    try:
        return ET.parse(file_path).getroot()
    except (ET.ParseError, ValueError) as first_exc:
        last_exc = first_exc

    try:
        with open(file_path, "rb") as f:
            raw = f.read()
    except Exception:
        raise last_exc

    if not raw:
        raise last_exc

    encodings = []
    decl_match = _XML_DECL_ENCODING_BYTES_RE.search(raw[:256])
    if decl_match:
        try:
            declared = decl_match.group(1).decode("ascii", errors="ignore").strip()
        except Exception:
            declared = ""
        if declared:
            encodings.extend([declared, declared.lower()])

    # Common encodings found in game/editor XML exports.
    encodings.extend([
        "utf-8-sig",
        "utf-8",
        "utf-16",
        "utf-16-le",
        "utf-16-be",
        "cp1250",
        "cp1252",
        "latin-1",
    ])

    tried = set()
    for encoding in encodings:
        key = encoding.lower()
        if key in tried:
            continue
        tried.add(key)
        try:
            text = raw.decode(encoding)
            # ElementTree rejects unicode strings with an XML encoding declaration.
            text = _XML_DECL_RE.sub("", text, count=1).lstrip("\ufeff")
            return ET.fromstring(text)
        except Exception as exc:
            last_exc = exc
            continue

    # Final fallback: strip duplicate attributes (invalid but present in some game XMLs)
    try:
        text = raw.decode("utf-8", errors="replace")
        text = _XML_DECL_RE.sub("", text, count=1).lstrip("\ufeff")
        text = _strip_duplicate_xml_attributes(text)
        return ET.fromstring(text)
    except Exception as exc:
        last_exc = exc

    raise last_exc

# Operator to refresh backend categories and items from XML files
class EQUIPMENT_OT_RefreshCategories(bpy.types.Operator):
    bl_idname = "witcher.equipment_refresh_categories"
    bl_label = "Refresh Categories"

    def execute(self, context):
        if _get_temp_source_game(context) == "w2":
            roots = _get_active_equipment_source_roots(context)
            loaded = ensure_equipment_catalog_for_search_roots(roots)
            if context.area:
                context.area.tag_redraw()
            if loaded:
                self.report({'INFO'}, "Loaded Witcher 2 equipment XMLs")
                return {'FINISHED'}
            self.report({'WARNING'}, "No Witcher 2 item XMLs found for the active source roots")
            return {'CANCELLED'}

        addon_prefs = get_all_addon_prefs(context)
        sources = _get_equipment_xml_sources(context, addon_prefs)
        valid_sources = [(label, path) for (label, path, is_valid) in sources if is_valid]
        if not valid_sources:
            searched = ", ".join(
                f"{label}={'<unset>' if not path else path}"
                for (label, path, _is_valid) in sources
            )
            self.report({'WARNING'}, "No valid gameplay/items XML source found")
            if searched:
                self.report({'INFO'}, f"Searched: {searched}")
            return {'CANCELLED'}

        merged_category_items = {}
        merged_item_attributes = {}
        source_summaries = []
        for label, folder_path in valid_sources:
            _, category_items_from_xml, item_attributes_from_xml = extract_categories_from_xml(folder_path)
            _merge_equipment_xml_data(
                merged_category_items,
                merged_item_attributes,
                category_items_from_xml,
                item_attributes_from_xml,
            )
            source_summaries.append(
                f"{label} ({len(category_items_from_xml)} cats, {len(item_attributes_from_xml)} items)"
            )

        # Update the class-level category_items and item_attributes
        for category, items in merged_category_items.items():
            if category not in EquipmentDefinitionEntry.category_items:
                # If the category doesn't exist, add it with items from XML
                EquipmentDefinitionEntry.category_items[category] = items
            else:
                # If category exists, update the items
                existing_items_set = set(tuple(item) for item in EquipmentDefinitionEntry.category_items[category])
                for item in items:
                    if tuple(item) not in existing_items_set:
                        EquipmentDefinitionEntry.category_items[category].append(item)
                        existing_items_set.add(tuple(item))

        # Update item_attributes (source priority already resolved in merged_item_attributes)
        EquipmentDefinitionEntry.item_attributes.update(merged_item_attributes)

        # Save to cache for persistence across reloads
        _save_category_cache()
        # Clear template path cache so any newly uncoooked files are picked up.
        _TEMPLATE_PATH_RESOLVE_CACHE.clear()

        if context.area:
            context.area.tag_redraw()  # Refresh the UI to reflect changes if necessary
        if source_summaries:
            self.report({'INFO'}, "Equipment XML sources: " + " | ".join(source_summaries[:3]))
        return {'FINISHED'}

# Operator to toggle item manipulation (switch between mount slot and hold slot)
class EQUIPMENT_OT_ToggleItem(bpy.types.Operator):
    """Toggle equipment between mount slot (scabbard) and hold slot (hand)"""
    bl_idname = "witcher.equipment_toggle_item"
    bl_label = "Toggle Item Manipulation"
    bl_options = {'REGISTER', 'UNDO'}
    
    slot_index: bpy.props.IntProperty(default=-1, description="Equipment slot index to toggle")

    def execute(self, context):
        ob, rig_settings = _get_armature_and_rig_settings(context)
        if not ob or not rig_settings:
            self.report({'WARNING'}, "No valid armature selected")
            return {'CANCELLED'}

        # Ensure slot constraints are up-to-date before toggling
        try:
            refresh_slot_constraints(ob)
        except Exception:
            pass
        
        # Get the slot to toggle
        if self.slot_index < 0 or self.slot_index >= len(rig_settings.equipment_slots):
            self.report({'WARNING'}, "Invalid slot index")
            return {'CANCELLED'}
            
        slot = rig_settings.equipment_slots[self.slot_index]
        slot_policy = _resolve_slot_visual_policy(slot, ob, rig_settings)

        if not slot.is_loaded or not slot.equip_guid:
            if not slot_policy["hold_valid"]:
                self.report({'INFO'}, f"No valid hold slot defined for '{slot.item_name}'")
                return {'CANCELLED'}
            if load_equipment_item(context, ob, self.slot_index, rig_settings, mount_mode="hold"):
                self.report({'INFO'}, f"'{slot.item_name}' loaded into hold slot")
                return {'FINISHED'}
            reason = _get_last_equipment_load_failure(ob, self.slot_index) or "Unknown failure"
            self.report({'WARNING'}, reason)
            return {'CANCELLED'}

        is_in_hold = bool(slot.is_in_hold_slot)
        target_info = slot_policy["equip_target"] if is_in_hold else slot_policy["hold_target"]

        if is_in_hold and not slot_policy["equip_valid"]:
            unload_equipment_item(slot)
            self.report({'INFO'}, f"'{slot.item_name}' put away")
            return {'FINISHED'}

        if not target_info["is_valid"]:
            target_label = "equip" if is_in_hold else "hold"
            self.report({'WARNING'}, f"No valid {target_label} target on current rig for '{slot.item_name}'")
            return {'CANCELLED'}

        equipment_objects = [
            obj for obj in find_objects_by_guid(slot.equip_guid, "witcher_equip_guid")
            if not obj.get("witcher_bound_parent_guid")
        ]
        if not equipment_objects:
            self.report({'WARNING'}, "Equipment objects not found")
            return {'CANCELLED'}

        mount_anchor = _find_equipment_mount_anchor(slot.equip_guid, kind="main")
        if mount_anchor:
            _mount_anchor_to_target(mount_anchor, target_info, fallback_armature=ob)
        else:
            for obj in _collect_mount_roots(equipment_objects):
                constraints_to_remove = [
                    c for c in obj.constraints
                    if c.name.startswith("Mount_") or c.name.startswith("Equip_")
                ]
                for c in constraints_to_remove:
                    obj.constraints.remove(c)
                _mount_object_to_target(obj, target_info, fallback_armature=ob)

        slot.is_in_hold_slot = not is_in_hold
        state_name = "mount" if is_in_hold else "hold"
        self.report({'INFO'}, f"'{slot.item_name}' moved to {state_name} slot")
        return {'FINISHED'}

# Operator to toggle variants auto/manual mode
class EQUIPMENT_OT_ToggleVariantMode(bpy.types.Operator):
    """Toggle variant mode between Auto and Manual"""
    bl_idname = "witcher.equipment_toggle_variant_mode"
    bl_label = "Toggle Variant Mode"

    def execute(self, context):
        ob, rig_settings = _get_armature_and_rig_settings(context)
        if not ob or not rig_settings:
            self.report({'WARNING'}, "No valid armature selected")
            return {'CANCELLED'}

        rig_settings.variants_auto = not rig_settings.variants_auto

        # Refresh variant states and reload changed slots
        slots = rig_settings.equipment_slots
        before_templates = []
        before_active = []
        for slot in slots:
            before_templates.append(get_effective_equip_template(slot))
            before_active.append(bool(getattr(slot, "variant_active", False)))

        saved_active = context.view_layer.objects.active
        saved_selection = [obj for obj in context.selected_objects]
        refresh_variant_states(rig_settings)

        for i, slot in enumerate(slots):
            after_template = get_effective_equip_template(slot)
            after_active = bool(getattr(slot, "variant_active", False))
            if slot.is_loaded and (before_templates[i] != after_template or before_active[i] != after_active):
                load_equipment_item(context, ob, i, rig_settings)

        _safe_restore_selection(saved_active, saved_selection)

        mode = "Auto" if rig_settings.variants_auto else "Manual"
        self.report({'INFO'}, f"Variant mode set to {mode}")
        return {'FINISHED'}

# Operators to hide/show equipment by GUID
class EQUIPMENT_OT_HideEquipment(bpy.types.Operator):
    bl_idname = "witcher.equipment_hide_equipment"
    bl_label = "Hide Equipment"

    slot_index: bpy.props.IntProperty(default=-1)

    def execute(self, context):
        _ob, rig_settings = _get_armature_and_rig_settings(context)
        if not rig_settings:
            self.report({'WARNING'}, "No valid armature selected")
            return {'CANCELLED'}

        if self.slot_index < 0 or self.slot_index >= len(rig_settings.equipment_slots):
            return {'CANCELLED'}
        slot = rig_settings.equipment_slots[self.slot_index]
        if slot.equip_guid:
            hide_objects_by_guid(slot.equip_guid, "witcher_equip_guid", hidden=True)
        return {'FINISHED'}

class EQUIPMENT_OT_ShowEquipment(bpy.types.Operator):
    bl_idname = "witcher.equipment_show_equipment"
    bl_label = "Show Equipment"

    slot_index: bpy.props.IntProperty(default=-1)

    def execute(self, context):
        _ob, rig_settings = _get_armature_and_rig_settings(context)
        if not rig_settings:
            self.report({'WARNING'}, "No valid armature selected")
            return {'CANCELLED'}

        if self.slot_index < 0 or self.slot_index >= len(rig_settings.equipment_slots):
            return {'CANCELLED'}
        slot = rig_settings.equipment_slots[self.slot_index]
        if slot.equip_guid:
            hide_objects_by_guid(slot.equip_guid, "witcher_equip_guid", hidden=False)
        return {'FINISHED'}

# Operators to hide/show bound items
class EQUIPMENT_OT_HideBoundItem(bpy.types.Operator):
    bl_idname = "witcher.equipment_hide_bound_item"
    bl_label = "Hide Bound Item"

    slot_index: bpy.props.IntProperty(default=-1)
    bound_name: bpy.props.StringProperty(default="")

    def execute(self, context):
        _ob, rig_settings = _get_armature_and_rig_settings(context)
        if not rig_settings:
            self.report({'WARNING'}, "No valid armature selected")
            return {'CANCELLED'}

        if self.slot_index < 0 or self.slot_index >= len(rig_settings.equipment_slots):
            return {'CANCELLED'}
        slot = rig_settings.equipment_slots[self.slot_index]
        for obj in _iter_bound_item_objects(slot.equip_guid, self.bound_name):
            obj.hide_set(True)
        return {'FINISHED'}

class EQUIPMENT_OT_ShowBoundItem(bpy.types.Operator):
    bl_idname = "witcher.equipment_show_bound_item"
    bl_label = "Show Bound Item"

    slot_index: bpy.props.IntProperty(default=-1)
    bound_name: bpy.props.StringProperty(default="")

    def execute(self, context):
        _ob, rig_settings = _get_armature_and_rig_settings(context)
        if not rig_settings:
            self.report({'WARNING'}, "No valid armature selected")
            return {'CANCELLED'}

        if self.slot_index < 0 or self.slot_index >= len(rig_settings.equipment_slots):
            return {'CANCELLED'}
        slot = rig_settings.equipment_slots[self.slot_index]
        for obj in _iter_bound_item_objects(slot.equip_guid, self.bound_name):
            obj.hide_set(False)
        return {'FINISHED'}


class EQUIPMENT_OT_CopyResolvedGamePath(bpy.types.Operator):
    bl_idname = "witcher.equipment_copy_resolved_game_path"
    bl_label = "Copy Resolved Game Path"
    bl_description = "Copy the selected equipment entry's resolved game-relative path to the clipboard"

    entry_index: bpy.props.IntProperty(default=-1)

    def execute(self, context):
        temp_data = _get_temp_equipment_data(context)
        if not temp_data or self.entry_index < 0 or self.entry_index >= len(temp_data.equipment_entries):
            self.report({'WARNING'}, "No equipment entry selected")
            return {'CANCELLED'}

        entry = temp_data.equipment_entries[self.entry_index]
        game_path = str(getattr(entry, "resolved_repo_path", "") or "").strip()
        if not game_path:
            _update_entry_resolved_repo_path(entry, context)
            game_path = str(getattr(entry, "resolved_repo_path", "") or "").strip()
        if not game_path:
            self.report({'WARNING'}, "Resolved game path not available")
            return {'CANCELLED'}

        context.window_manager.clipboard = game_path
        self.report({'INFO'}, "Copied resolved game path")
        return {'FINISHED'}


class EQUIPMENT_OT_OpenResolvedPathFolder(bpy.types.Operator):
    bl_idname = "witcher.equipment_open_resolved_path_folder"
    bl_label = "Open Resolved Path Folder"
    bl_description = "Open the folder containing the resolved equipment file in Windows Explorer"

    entry_index: bpy.props.IntProperty(default=-1)

    def execute(self, context):
        temp_data = _get_temp_equipment_data(context)
        if not temp_data or self.entry_index < 0 or self.entry_index >= len(temp_data.equipment_entries):
            self.report({'WARNING'}, "No equipment entry selected")
            return {'CANCELLED'}

        entry = temp_data.equipment_entries[self.entry_index]
        abs_path = str(getattr(entry, "resolved_abs_path", "") or "").strip()
        if not abs_path:
            _update_entry_resolved_repo_path(entry, context)
            abs_path = str(getattr(entry, "resolved_abs_path", "") or "").strip()
        if not abs_path:
            self.report({'WARNING'}, "Resolved absolute path not available")
            return {'CANCELLED'}

        folder = os.path.dirname(abs_path) if os.path.isfile(abs_path) else abs_path
        if not os.path.isdir(folder):
            self.report({'WARNING'}, f"Folder not found: {folder}")
            return {'CANCELLED'}

        import subprocess
        try:
            if os.path.isfile(abs_path):
                subprocess.Popen(f'explorer /select,"{abs_path}"')
            else:
                subprocess.Popen(f'explorer "{folder}"')
        except Exception as e:
            self.report({'ERROR'}, f"Could not open folder: {e}")
            return {'CANCELLED'}
        return {'FINISHED'}

# Define the UI list to display included templates
class EQUIPMENT_UL_IncludedTemplateList(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        # Display templateFilename
        layout.prop(item, "templateFilename", text="", emboss=False)

# Operator to add an included template
class EQUIPMENT_OT_AddIncludedTemplate(bpy.types.Operator):
    bl_idname = "witcher.equipment_add_included_template"
    bl_label = "Add Included Template"

    def execute(self, context):
        wm = context.window_manager
        temp_data = wm.witcherui_temp_data
        new_entry = temp_data.included_template_entries.add()
        temp_data.included_template_entries_index = len(temp_data.included_template_entries) - 1
        return {'FINISHED'}

# Operator to remove the selected included template
class EQUIPMENT_OT_RemoveIncludedTemplate(bpy.types.Operator):
    bl_idname = "witcher.equipment_remove_included_template"
    bl_label = "Remove Included Template"

    def execute(self, context):
        wm = context.window_manager
        temp_data = wm.witcherui_temp_data
        entries = temp_data.included_template_entries
        index = temp_data.included_template_entries_index
        if len(entries) > 0 and 0 <= index < len(entries):
            entries.remove(index)
            temp_data.included_template_entries_index = min(max(0, index - 1), len(entries) - 1)
        return {'FINISHED'}

# Operator to manually load included template data
class EQUIPMENT_OT_LoadIncludedTemplateData(bpy.types.Operator):
    bl_idname = "witcher.equipment_load_included_template_data"
    bl_label = "Load Template Data"

    def execute(self, context):
        wm = context.window_manager
        temp_data = wm.witcherui_temp_data
        index = temp_data.included_template_entries_index
        if index >= 0 and index < len(temp_data.included_template_entries):
            entry = temp_data.included_template_entries[index]
            entry.update_template_data(context)
            self.report({'INFO'}, f"Template data loaded for '{entry.templateFilename}'")
            return {'FINISHED'}
        else:
            self.report({'WARNING'}, "No included template selected.")
            return {'CANCELLED'}

# Operator to save equipment entries back to jsonData
class EQUIPMENT_OT_SaveEquipmentEntries(bpy.types.Operator):
    bl_idname = "witcher.equipment_save_equipment_entries"
    bl_label = "Save Equipment Entries"
    
    def execute(self, context):
        wm = context.window_manager
        temp_data = wm.witcherui_temp_data
        equipment_entries = temp_data.equipment_entries

        # Get the armature object
        _armature, rig_settings = _get_armature_and_rig_settings(context)
        if not rig_settings:
            self.report({'WARNING'}, "No valid armature selected.")
            return {'CANCELLED'}
        
        app_list = rig_settings.app_list
        app_list_index = rig_settings.app_list_index
        
        _entity, entity_data = import_entity.get_rig_entity_state(rig_settings)
        if entity_data is None:
            self.report({'ERROR'}, "Failed to load cached entity data.")
            return {'CANCELLED'}
        
        appearances = entity_data.get('appearances', [])
        if app_list_index >= 0 and app_list_index < len(appearances):
            selected_appearance = appearances[app_list_index]
            # Update equipment entries
            equipment_entries_data = []
            for entry in equipment_entries:
                default_item_name = entry.defaultItemName
                if default_item_name == 'None':
                    default_item_name = None
                equipment_entries_data.append({
                    'category': entry.category,
                    'defaultItemName': default_item_name,
                    'initializer': None  # Keeping initializer as per your JSON structure
                })
            # Assuming 'appearanceParams' is a list with at least one element containing 'entries'
            if 'appearanceParams' in selected_appearance and len(selected_appearance['appearanceParams']) > 0:
                selected_appearance['appearanceParams'][0]['entries'] = equipment_entries_data
            else:
                selected_appearance['appearanceParams'] = [{'entries': equipment_entries_data}]

            # Update includedTemplates
            included_templates_data = []
            for entry in temp_data.included_template_entries:
                # Load the data from the JSON string
                if entry.data:
                    template_data = json.loads(entry.data)
                    # Update the templateFilename if it was edited
                    template_data['templateFilename'] = entry.templateFilename
                    included_templates_data.append(template_data)
                else:
                    # If no data is present, create a minimal structure
                    included_templates_data.append({'templateFilename': entry.templateFilename})

            selected_appearance['includedTemplates'] = included_templates_data

            if import_entity.cache_rig_entity_state_from_data(rig_settings, entity_data, update_json=True) is None:
                self.report({'ERROR'}, "Failed to rebuild entity state after editing equipment entries.")
                return {'CANCELLED'}
            self.report({'INFO'}, "Equipment entries saved.")
            return {'FINISHED'}
        else:
            self.report({'WARNING'}, "No appearance item selected.")
            return {'CANCELLED'}


from ..ui.ui_utils import WITCH_PT_Base
# Define the main panel
class EQUIPMENT_PT_MainPanel(WITCH_PT_Base, bpy.types.Panel):
    bl_label = "Equipment"
    bl_idname = "EQUIPMENT_PT_main_panel"
    # Embedded into Character panel's Equipment tab — hidden as standalone sub-panel.
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return False  # Content embedded via Character panel tabs

    def draw_header(self, context):
        self.layout.label(text="Equipment", icon='PACKAGE')

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        wm = context.window_manager

        # Get the armature object
        main_arm_obj, rig_settings = _get_armature_and_rig_settings(context)
        if not main_arm_obj or not rig_settings:
            layout.label(text="No valid armature selected.")
            return

        app_list = rig_settings.app_list
        app_list_index = rig_settings.app_list_index

        temp_data = wm.witcherui_temp_data
        try:
            temp_data.equipment_source_game = _infer_source_game_from_rig_settings(rig_settings, main_arm_obj)
        except Exception:
            temp_data.equipment_source_game = "w3"
        if temp_data.equipment_source_game == "w2":
            # Auto-restore W2 catalog from persistent cache on first draw so the
            # category/item search popups work without a manual "Refresh Categories".
            global _W2_CATEGORY_CACHE_LOADED
            if not _W2_CATEGORY_CACHE_LOADED:
                _load_category_cache("w2")
                _W2_CATEGORY_CACHE_LOADED = True
            try:
                ensure_equipment_catalog_for_search_roots(import_entity._get_armature_source_roots(main_arm_obj))
            except Exception:
                pass

        _entity, entity_data = import_entity.get_rig_entity_state(rig_settings)
        if entity_data is None:
            layout.label(text="Failed to load cached entity data.")
            return

        appearances = entity_data.get('appearances', [])

        # Check if selected appearance, armature instance, or entity state has changed.
        arm_name = _make_temp_armature_key(main_arm_obj)
        entity_state_token = _make_temp_entity_state_token(rig_settings)
        if (
            temp_data.last_app_list_index != app_list_index
            or temp_data.last_armature_name != arm_name
            or temp_data.last_entity_state_token != entity_state_token
        ):
            temp_data.last_app_list_index = app_list_index
            temp_data.last_armature_name = arm_name
            temp_data.last_entity_state_token = entity_state_token
            temp_data.equipment_entries.clear()
            temp_data.included_template_entries.clear()

            if app_list_index >= 0 and app_list_index < len(appearances):
                selected_appearance = appearances[app_list_index]

                # Load includedTemplates into temp data
                included_templates_data = selected_appearance.get('includedTemplates', [])
                for template_data in included_templates_data:
                    entry = temp_data.included_template_entries.add()
                    entry.data = json.dumps(template_data, indent=2)
                    entry.templateFilename = template_data.get('templateFilename', '')
                    entry.ns = template_data.get('ns', '')

                # Parse equipment entries
                appearance_params = selected_appearance.get('appearanceParams', [])
                if appearance_params and 'entries' in appearance_params[0]:
                    equipment_entries_data = appearance_params[0]['entries']
                else:
                    equipment_entries_data = []

                # If persistent equipment_slots already exist, use THOSE as the
                # source of truth (they may have been modified by inventory import).
                # Only fall back to appearance JSON on first load.
                if len(rig_settings.equipment_slots) > 0:
                    sync_equipment_slots_to_temp(context, rig_settings)
                else:
                    active_category_items, active_item_attributes = _get_equipment_catalog(temp_data.equipment_source_game)
                    _set_temp_equipment_auto_apply_suspended(context, True)
                    try:
                        # First load: populate temp entries from appearance JSON
                        for entry_data in equipment_entries_data:
                            category_val = entry_data.get('category', '') or 'None'
                            default_item_name = entry_data.get('defaultItemName', '') or 'None'
                            # Pre-populate catalog so update callbacks can find the item
                            if category_val not in active_category_items:
                                active_category_items[category_val] = [("None", "None", "")]
                            if default_item_name != 'None':
                                item_names = [it[0] for it in active_category_items[category_val]]
                                if default_item_name not in item_names:
                                    active_category_items[category_val].append((default_item_name, default_item_name, ""))
                            entry = temp_data.equipment_entries.add()
                            entry.slot_index = -1
                            entry.source_game = temp_data.equipment_source_game
                            entry.category = category_val
                            entry.defaultItemName = default_item_name
                            entry.update_item_attributes(context)

                        # Also create persistent equipment_slots from appearance data
                        for entry_data in equipment_entries_data:
                            slot = rig_settings.equipment_slots.add()
                            slot.source_game = temp_data.equipment_source_game
                            slot.category = entry_data.get('category', '')
                            item_name = entry_data.get('defaultItemName', '')
                            slot.item_name = item_name if item_name else ''
                            slot.equip_template = entry_data.get('equip_template', '')
                            slot.base_equip_template = slot.equip_template
                            slot.resolved_repo_path = ""
                            slot.keep_across_appearances = False
                            try:
                                attrs = active_item_attributes.get(item_name, {})
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
                    finally:
                        _set_temp_equipment_auto_apply_suspended(context, False)
                    sync_equipment_slots_to_temp(context, rig_settings)

                # Sync persistent template_slots to current appearance (deferred)
                _request_sync_templates()
            else:
                selected_appearance = None

        tab = getattr(rig_settings, "equipment_ui_tab", "EQUIPMENT")
        if tab == "APPEARANCE":
            # Appearance controls now live in Character Appearances to avoid duplicate flows.
            tab = "EQUIPMENT"
            try:
                rig_settings.equipment_ui_tab = 'EQUIPMENT'
            except Exception:
                pass

        info_box = layout.box()
        info_box.label(text="Character appearance loading moved to parent panel.", icon='INFO')
        info_box.label(text="Use this panel for templates, equipment items, and entity slots.")

        # prop_enum tab buttons can lose their captions when property split is enabled.
        prev_split = layout.use_property_split
        layout.use_property_split = False
        tab_row = layout.row(align=True)
        tab_row.prop_enum(rig_settings, "equipment_ui_tab", 'EQUIPMENT')
        tab_row.prop_enum(rig_settings, "equipment_ui_tab", 'TEMPLATES')
        tab_row.prop_enum(rig_settings, "equipment_ui_tab", 'SLOTS')
        layout.use_property_split = prev_split
        layout.separator(factor=0.5)

        if tab == "APPEARANCE":
            box = layout.box()
            box.label(text="Appearance controls are in Character Appearances", icon='INFO')
            box.operator("witcher.list_loadapp", text="Load Selected Appearance", icon='IMPORT').action = "load"
            return

        if tab == "TEMPLATES":
            # =============================================================
            # TEMPLATES SECTION (persistent, GUID-tracked)
            # =============================================================
            box = layout.box()
            current_app_name = get_current_appearance_name(rig_settings)
            box.label(text=f"Included Templates ({current_app_name}):", icon='FILE_3D')

            # Show persistent template slots - FILTERED by current appearance
            templates_shown = 0
            if len(rig_settings.template_slots) > 0:
                for i, slot in enumerate(rig_settings.template_slots):
                    # Bug 2 fix: Only show templates belonging to current appearance
                    if not template_belongs_to_appearance(slot, current_app_name):
                        continue

                    templates_shown += 1
                    row = box.row(align=True)

                    # Status icon - use per-appearance hidden state
                    is_hidden_for_app = get_hidden_in_appearance(slot, current_app_name)
                    if not slot.is_loaded:
                        icon = 'RADIOBUT_OFF'
                    elif is_hidden_for_app:
                        icon = 'HIDE_ON'
                    else:
                        icon = 'CHECKMARK'

                    row.label(text=slot.template_filename, icon=icon)

                    # Visibility toggle (eye icon) - only if loaded
                    if slot.is_loaded:
                        if is_hidden_for_app:
                            op = row.operator("witcher.equipment_show_template", text="", icon='HIDE_OFF')
                        else:
                            op = row.operator("witcher.equipment_hide_template", text="", icon='HIDE_ON')
                        op.slot_index = i

                    # Load/Unload button
                    if slot.is_loaded:
                        op = row.operator("witcher.equipment_unload_template", text="", icon='X')
                        op.slot_index = i
                    else:
                        op = row.operator("witcher.equipment_load_template", text="", icon='IMPORT')
                        op.slot_index = i

            if templates_shown == 0:
                box.label(text="No templates for this appearance.")

            # Template bulk actions
            row = box.row(align=True)
            row.operator("witcher.equipment_load_template", text="Load All Templates", icon='IMPORT').slot_index = -1
            row.operator("witcher.equipment_unload_template", text="Unload All", icon='X').slot_index = -1
            box.operator("witcher.equipment_sync_templates_to_appearance", text="Sync Templates", icon='FILE_REFRESH')
            box.operator("witcher.equipment_refresh_template_data", text="Refresh Template Data", icon='FILE_REFRESH')

            # Temp data template list (for editing/adding/removing)
            row = box.row()
            row.template_list("EQUIPMENT_UL_IncludedTemplateList", "", temp_data, "included_template_entries", temp_data, "included_template_entries_index")
            col = row.column(align=True)
            col.operator("witcher.equipment_add_included_template", icon="ADD", text="")
            col.operator("witcher.equipment_remove_included_template", icon="REMOVE", text="")

            index = temp_data.included_template_entries_index
            if index >= 0 and index < len(temp_data.included_template_entries):
                entry = temp_data.included_template_entries[index]
                box.prop(entry, "templateFilename")
                box.prop(entry, "ns")
                box.operator("witcher.equipment_load_included_template_data", text="Load Template Data")
            return

        if tab == "EQUIPMENT":
            # =============================================================
            # EQUIPMENT SECTION (persistent, GUID-tracked)
            # =============================================================
            box = layout.box()
            box.label(text="Equipment Entries:", icon='ARMATURE_DATA')
            row = box.row(align=True)
            row.label(text=f"Variants: {'Auto' if rig_settings.variants_auto else 'Manual'}")
            row.operator("witcher.equipment_toggle_variant_mode", text="Switch")

            # Show persistent equipment slots with status
            if len(rig_settings.equipment_slots) > 0:
                try:
                    refresh_variant_states(rig_settings)
                except Exception:
                    pass
                visible_slots = []
                for i, slot in enumerate(rig_settings.equipment_slots):
                    if not _slot_has_active_selection(slot):
                        continue
                    slot_policy = _resolve_slot_visual_policy(slot, main_arm_obj, rig_settings)
                    if getattr(slot, "is_inventory", False) and slot_policy["policy"] == "nonvisual_on_rig":
                        continue
                    visible_slots.append((i, slot, slot_policy))
                for i, slot, slot_policy in visible_slots:
                    row = box.row(align=True)
                    icon = 'CHECKMARK' if slot.is_loaded else 'RADIOBUT_OFF'
                    label_text = f"{slot.category}: {slot.item_name or 'None'}"
                    if getattr(slot, "variant_active", False):
                        label_text += " [VAR]"
                    if slot_policy["policy"] == "hold_only_on_rig":
                        label_text += " [Hold Only]"
                    row.label(text=label_text, icon=icon)

                    if _safe_json_list(getattr(slot, "variants_json", "")):
                        toggle = row.row(align=True)
                        toggle.enabled = not rig_settings.variants_auto
                        toggle.prop(slot, "variants_enabled", text="Var")
                        if getattr(slot, "variant_active", False):
                            row.label(text="", icon='CHECKMARK')

                    show_toggle = False
                    toggle_text = ""
                    toggle_icon = 'ARMATURE_DATA'
                    if slot.is_loaded and slot_policy["hold_valid"]:
                        is_in_hold = bool(slot.is_in_hold_slot)
                        toggle_icon = 'ARMATURE_DATA' if is_in_hold else 'FILE_3D'
                        if slot_policy["policy"] == "hold_only_on_rig":
                            toggle_text = "Put Away" if is_in_hold else "Hold"
                        else:
                            toggle_text = "->Mount" if is_in_hold else "->Hold"
                        show_toggle = True
                    elif not slot.is_loaded and slot_policy["policy"] == "hold_only_on_rig" and slot_policy["hold_valid"]:
                        toggle_text = "Hold"
                        toggle_icon = 'ARMATURE_DATA'
                        show_toggle = True

                    if show_toggle:
                        op = row.operator("witcher.equipment_toggle_item", text=toggle_text, icon=toggle_icon)
                        op.slot_index = i

                    if slot.is_loaded:
                        is_hidden = _is_guid_hidden(slot.equip_guid, "witcher_equip_guid")
                        if is_hidden:
                            op = row.operator("witcher.equipment_show_equipment", text="", icon='HIDE_OFF')
                        else:
                            op = row.operator("witcher.equipment_hide_equipment", text="", icon='HIDE_ON')
                        op.slot_index = i

                        if slot.category in ('steelsword', 'silversword'):
                            row.prop(slot, "rune_level", text="Rune")

                        op = row.operator("witcher.equipment_unload_equipment", text="", icon='X')
                        op.slot_index = i
                    else:
                        # Always show a load/hold button in a consistent position.
                        # Disabled when the item cannot be loaded on this rig.
                        btn = row.row(align=True)
                        if slot_policy["policy"] == "equipable_on_rig":
                            btn.enabled = True
                            op = btn.operator("witcher.equipment_load_equipment", text="", icon='IMPORT')
                            op.slot_index = i
                        elif slot_policy["policy"] == "hold_only_on_rig" and slot_policy["hold_valid"]:
                            # hold_only items handled by show_toggle above; skip duplicate
                            pass
                        else:
                            btn.enabled = False
                            op = btn.operator("witcher.equipment_load_equipment", text="", icon='IMPORT')
                            op.slot_index = i

                    # Appearance dropdown for items with multiple dye/appearance variants
                    item_app_names = _safe_json_list(getattr(slot, 'item_appearances_json', ''))
                    if len(item_app_names) > 1:
                        app_row = box.row(align=True)
                        app_row.label(text="  Appearance:")
                        app_row.prop(slot, "item_appearance_name", text="")
                        try:
                            coloring = json.loads(slot.item_coloring_json or '[]')
                        except Exception:
                            coloring = []
                        for col_entry in coloring:
                            comp = col_entry.get('componentName', '')
                            h1 = col_entry.get('hue1', 0)
                            s1 = col_entry.get('sat1', 0)
                            l1 = col_entry.get('lum1', 0)
                            col_row = box.row(align=True)
                            col_row.label(text=f"    {comp}: H{h1:+.0f} S{s1:+.0f} L{l1:+.0f}", icon='COLORSET_01_VEC')

                    bound_items = _safe_json_list(getattr(slot, "bound_items_json", ""))
                    if bound_items:
                        for bound_name in bound_items:
                            bound_row = box.row(align=True)
                            bound_row.label(text=f"  Bound: {bound_name}", icon='LINKED')
                            if slot.is_loaded:
                                hidden = _is_bound_item_hidden(slot.equip_guid, bound_name)
                                if hidden:
                                    op = bound_row.operator("witcher.equipment_show_bound_item", text="", icon='HIDE_OFF')
                                else:
                                    op = bound_row.operator("witcher.equipment_hide_bound_item", text="", icon='HIDE_ON')
                                op.slot_index = i
                                op.bound_name = bound_name
                if not visible_slots:
                    box.label(text="No active equipment categories on this character.", icon='INFO')

            # Equipment bulk actions
            row = box.row(align=True)
            row.operator("witcher.equipment_load_equipment", text="Load All Equipment", icon='IMPORT').slot_index = -1
            row.operator("witcher.equipment_unload_equipment", text="Unload All", icon='X').slot_index = -1

            if temp_data.equipment_source_game != "w3":
                info = box.box()
                info.label(text="Witcher 2 categories can be edited from the list below.", icon='INFO')
                info.label(text="Changes apply to the current loaded entity only.")

            box.prop(temp_data, "auto_apply_equipment_selection")

            # Temp data equipment list (for dropdown editing)
            row = box.row()
            row.template_list("EQUIPMENT_UL_CategoryList", "", temp_data, "equipment_entries", temp_data, "equipment_entries_index")
            if temp_data.equipment_source_game == "w3":
                col = row.column(align=True)
                col.operator("witcher.equipment_add_category", icon="ADD", text="")
                col.operator("witcher.equipment_remove_category", icon="REMOVE", text="")
            else:
                col = row.column(align=True)
                col.operator("witcher.equipment_add_category", icon="ADD", text="")
                col.operator("witcher.equipment_remove_category", icon="REMOVE", text="")

            # Display attributes of the selected equipment entry
            index = temp_data.equipment_entries_index
            if index >= 0 and index < len(temp_data.equipment_entries):
                entry = temp_data.equipment_entries[index]
                try:
                    _update_entry_resolved_repo_path(entry, context, armature=main_arm_obj, rig_settings=rig_settings)
                except Exception:
                    pass
                row = box.row(align=True)
                row.label(text="Category:")
                op = row.operator("witcher.equipment_search_category",
                                  text=entry.category or "None", icon='DOWNARROW_HLT')
                op.entry_index = index
                row = box.row(align=True)
                row.label(text="Default Item:")
                op = row.operator("witcher.equipment_search_default_item",
                                  text=entry.defaultItemName or "None", icon='DOWNARROW_HLT')
                op.entry_index = index
                box.prop(entry, "equip_template")
                box.prop(entry, "equip_slot")
                box.prop(entry, "hold_slot")
                box.prop(entry, "weapon")
                box.prop(entry, "attachment_type")
                box.label(text=f"Variants: {entry.variants_summary or 'None'}")
                box.label(text=f"Bound Items: {entry.bound_items_summary or 'None'}")
                box.label(text=f"Tags: {entry.tags_summary or 'None'}")
                repo_row = box.row(align=True)
                repo_value = repo_row.row()
                repo_value.enabled = False
                repo_value.prop(entry, "resolved_repo_path", text="Resolved Game Path")
                repo_actions = repo_row.row(align=True)
                repo_actions.enabled = bool(
                    str(getattr(entry, "resolved_repo_path", "") or "").strip()
                    or str(getattr(entry, "resolved_abs_path", "") or "").strip()
                )
                op = repo_actions.operator("witcher.equipment_copy_resolved_game_path", text="", icon='COPYDOWN')
                op.entry_index = index
                op = repo_actions.operator("witcher.equipment_open_resolved_path_folder", text="", icon='FILE_FOLDER')
                op.entry_index = index

                if entry.hold_slot:
                    box.operator("witcher.equipment_toggle_item", text="Toggle Item Manipulation")
                    box.prop(entry, "toggle_value", text="Manipulation Active")

            # Bottom actions
            row = layout.row(align=True)
            row.operator("witcher.equipment_refresh_categories", icon="FILE_REFRESH")
            if temp_data.equipment_source_game == "w3":
                row.operator("witcher.equipment_insert_default_categories", icon="IMPORT")
                layout.operator("witcher.equipment_save_equipment_entries", icon="FILE_TICK")
            return

        if tab == "SLOTS":
            # =============================================================
            # Entity Slots Section (mounting points from EntitySlot data)
            # =============================================================
            box = layout.box()
            row = box.row()
            row.label(text=f"Entity Slots ({len(rig_settings.entity_slots)}):", icon='EMPTY_AXIS')
            row.operator("witcher.equipment_toggle_entity_slots", 
                         text="Show" if not rig_settings.show_entity_slots else "Hide",
                         icon='HIDE_OFF' if rig_settings.show_entity_slots else 'HIDE_ON')

            row = box.row(align=True)
            row.label(
                text=f"Rot90 Display Fix: {'Applied' if get_rig_rot90_enabled(rig_settings, default=False) else 'Not Applied'}",
                icon='BONE_DATA'
            )

            if len(rig_settings.entity_slots) > 0:
                # Show slot details
                for slot in rig_settings.entity_slots:
                    row = box.row(align=True)
                    row.label(text=slot.slot_name, icon='DOT')
                    row.label(text=f"{slot.component_name}:{slot.bone_name}" if slot.component_name else slot.bone_name or "(no bone)")

            # Refresh button for sub-component slots
            box.operator("witcher.equipment_refresh_slot_constraints", text="Refresh Sub-Component Slots", icon='FILE_REFRESH')
            return

# Operator to toggle entity slot empty visibility
class EQUIPMENT_OT_ToggleEntitySlots(bpy.types.Operator):
    bl_idname = "witcher.equipment_toggle_entity_slots"
    bl_label = "Toggle Entity Slot Visibility"
    bl_description = "Show/hide entity slot empty objects in viewport"
    
    def execute(self, context):
        ob, rig_settings = _get_armature_and_rig_settings(context)
        if not ob or not rig_settings:
            self.report({'WARNING'}, "No valid armature selected")
            return {'CANCELLED'}

        rig_settings.show_entity_slots = not rig_settings.show_entity_slots

        hidden = not rig_settings.show_entity_slots
        # Scope strictly to this armature instance's slot hierarchy.
        for obj in ob.children_recursive:
            if obj.type != 'EMPTY':
                continue
            if obj.get("witcher_slots_parent") or obj.get("witcher_slot_name"):
                obj.hide_set(hidden)

        return {'FINISHED'}

# Operator to refresh slot constraints for sub-component armatures
class EQUIPMENT_OT_RefreshSlotConstraints(bpy.types.Operator):
    """Refresh slot constraints for sub-components like scabbards_skeleton"""
    bl_idname = "witcher.equipment_refresh_slot_constraints"
    bl_label = "Refresh Slot Constraints"
    bl_description = "Update slot Empty constraints after all components are imported"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        ob, rig_settings = _get_armature_and_rig_settings(context)
        if not ob or not rig_settings:
            self.report({'WARNING'}, "No valid armature selected")
            return {'CANCELLED'}
        
        updated = refresh_slot_constraints(ob)
        self.report({'INFO'}, f"Updated {updated} slot constraint(s)")
        return {'FINISHED'}

# Operator to add a new category
class EQUIPMENT_OT_AddCategory(bpy.types.Operator):
    bl_idname = "witcher.equipment_add_category"
    bl_label = "Add Category"

    def execute(self, context):
        wm = context.window_manager
        temp_data = wm.witcherui_temp_data
        armature, rig_settings = _get_armature_and_rig_settings(context)
        if not rig_settings:
            self.report({'WARNING'}, "No valid armature selected.")
            return {'CANCELLED'}

        slot = rig_settings.equipment_slots.add()
        slot.source_game = _get_temp_source_game(context)
        slot.item_name = "None"
        slot.equip_template = ""
        slot.base_equip_template = ""
        slot.resolved_repo_path = ""
        slot.keep_across_appearances = True

        _set_temp_equipment_auto_apply_suspended(context, True)
        try:
            entry = temp_data.equipment_entries.add()
            entry.slot_index = len(rig_settings.equipment_slots) - 1
            entry.source_game = _get_temp_source_game(context)
            # Ensure a valid category is always selected on creation
            try:
                cat_items = entry.get_category_items(context)
                if cat_items:
                    entry.category = cat_items[0][0]
            except Exception:
                pass
            # defaultItemName resets automatically via _on_category_changed, but
            # set it explicitly here in case the update didn't fire yet
            try:
                item_items = entry.get_default_items(context)
                entry.defaultItemName = item_items[0][0] if item_items else "None"
            except Exception:
                pass
        finally:
            _set_temp_equipment_auto_apply_suspended(context, False)
        temp_data.equipment_entries_index = len(temp_data.equipment_entries) - 1
        if context.area:
            context.area.tag_redraw()
        return {'FINISHED'}

# Operator to remove the selected category
class EQUIPMENT_OT_RemoveCategory(bpy.types.Operator):
    bl_idname = "witcher.equipment_remove_category"
    bl_label = "Remove Category"
    
    def execute(self, context):
        wm = context.window_manager
        temp_data = wm.witcherui_temp_data
        entries = temp_data.equipment_entries
        index = temp_data.equipment_entries_index
        if len(entries) > 0 and 0 <= index < len(entries):
            entry = entries[index]
            armature, rig_settings = _get_armature_and_rig_settings(context)
            slot_index = int(getattr(entry, "slot_index", -1))
            if rig_settings and 0 <= slot_index < len(rig_settings.equipment_slots):
                slot = rig_settings.equipment_slots[slot_index]
                if getattr(slot, "is_loaded", False) and getattr(slot, "equip_guid", ""):
                    remove_objects_by_guid(slot.equip_guid, "witcher_equip_guid")
                rig_settings.equipment_slots.remove(slot_index)
            entries.remove(index)
            temp_data.equipment_entries_index = min(max(0, index - 1), len(entries) - 1)
            if rig_settings:
                sync_equipment_slots_to_temp(context, rig_settings)
            if context.area:
                context.area.tag_redraw()
        return {'FINISHED'}

# Operator to insert all default categories into the equipment entries list
class EQUIPMENT_OT_InsertDefaultCategories(bpy.types.Operator):
    bl_idname = "witcher.equipment_insert_default_categories"
    bl_label = "Insert Default Categories"
    
    def execute(self, context):
        wm = context.window_manager
        temp_data = wm.witcherui_temp_data
        source_game = _get_temp_source_game(context)
        active_category_items, active_item_attributes = _get_equipment_catalog(source_game)

        armature, rig_settings = _get_armature_and_rig_settings(context)
        if not rig_settings:
            self.report({'WARNING'}, "No valid armature selected.")
            return {'CANCELLED'}

        # Collect categories already present in existing slots
        existing_categories = {slot.category for slot in rig_settings.equipment_slots}

        _set_temp_equipment_auto_apply_suspended(context, True)
        try:
            if source_game == "w2":
                category_source = active_category_items
            else:
                category_source = default_categories
            for category, items in category_source.items():
                if category in existing_categories:
                    continue  # preserve existing slot (keeps is_loaded, equip_guid, item_name)

                default_item_name = items[0][0] if len(items) <= 1 else items[1][0]
                equip_template = "" if len(items) <= 1 else items[1][2]

                slot = rig_settings.equipment_slots.add()
                slot.source_game = source_game
                slot.category = category
                slot.item_name = default_item_name if default_item_name and default_item_name != "None" else ""
                slot.equip_template = equip_template
                slot.base_equip_template = equip_template
                slot.resolved_repo_path = ""
                slot.keep_across_appearances = False
                try:
                    attrs = active_item_attributes.get(default_item_name, {})
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
        finally:
            _set_temp_equipment_auto_apply_suspended(context, False)

        sync_equipment_slots_to_temp(context, rig_settings)
        if context.area:
            context.area.tag_redraw()
        return {'FINISHED'}

# =============================================================================
# Core Load/Unload Functions (used by operators and import_app)
# =============================================================================

def _get_armature_and_rig_settings(context):
    """Get the active armature and its rig settings. Returns (armature, rig_settings) or (None, None)."""
    armature, rig_settings = get_main_armature_and_rig_settings(
        context,
        prefer_active=True,
        remember=True,
        fallback=True,
    )
    if armature and rig_settings:
        return armature, rig_settings
    return None, None

def sync_equipment_slots_to_temp(context, rig_settings):
    """Sync persistent equipment_slots back to temp UI equipment_entries.

    Call this after programmatic changes to equipment_slots (e.g. inventory import)
    so the category dropdowns and item selections stay in sync.
    """
    try:
        wm = context.window_manager
        temp_data = wm.witcherui_temp_data
    except Exception:
        return

    try:
        armature, _active_rig_settings = _get_armature_and_rig_settings(context)
    except Exception:
        armature = None
    armature_key = _make_temp_armature_key(armature)
    entity_state_token = _make_temp_entity_state_token(rig_settings)
    try:
        temp_data.equipment_source_game = _infer_source_game_from_rig_settings(rig_settings, armature)
    except Exception:
        temp_data.equipment_source_game = "w3"
    category_items, item_attributes = _get_equipment_catalog(temp_data.equipment_source_game)
    _set_temp_equipment_auto_apply_suspended(context, True)
    try:
        temp_data.equipment_entries.clear()

        for slot_index, slot in enumerate(rig_settings.equipment_slots):
            if not _slot_has_active_selection(slot) and not slot.category:
                continue

            # Pre-populate catalog so update callbacks can find the item
            item_name = slot.item_name or 'None'
            if slot.category not in category_items:
                category_items[slot.category] = [("None", "None", "")]
            if item_name != 'None':
                items = category_items[slot.category]
                item_names = [it[0] for it in items]
                if item_name not in item_names:
                    tmpl = slot.equip_template or ""
                    category_items[slot.category].append(
                        (item_name, item_name, tmpl)
                    )

            entry = temp_data.equipment_entries.add()
            entry.slot_index = slot_index
            entry.source_game = _normalize_source_game(getattr(slot, "source_game", "") or temp_data.equipment_source_game)
            entry.category = slot.category
            entry.defaultItemName = item_name
            entry.equip_template = slot.equip_template or ""
            entry.resolved_repo_path = slot.resolved_repo_path or ""
            entry.equip_slot = slot.equip_slot or ""
            entry.hold_slot = slot.hold_slot or ""
            entry.weapon = slot.weapon
            entry.attachment_type = slot.attachment_type or ""
            entry.variants_json = slot.variants_json or "[]"
            entry.bound_items_json = slot.bound_items_json or "[]"

            # Populate display summaries
            try:
                variants = json.loads(slot.variants_json) if slot.variants_json else []
                entry.variants_summary = _format_variant_summary(variants)
            except Exception:
                entry.variants_summary = ""
            try:
                bound_items = json.loads(slot.bound_items_json) if slot.bound_items_json else []
                entry.bound_items_summary = _format_bound_items_summary(bound_items)
            except Exception:
                entry.bound_items_summary = ""
            # Tags summary from item_attributes
            attrs = item_attributes.get(item_name, {})
            tags = attrs.get('tags', [])
            if isinstance(tags, str):
                tags = _split_tags(tags)
            try:
                entry.tags_summary = ", ".join([str(t) for t in tags if t])
            except Exception:
                entry.tags_summary = ""
            _update_entry_resolved_repo_path(entry, context, armature=armature, rig_settings=rig_settings)
    finally:
        _set_temp_equipment_auto_apply_suspended(context, False)

    # Force the draw sync to recognize current state
    try:
        temp_data.last_app_list_index = rig_settings.app_list_index
        temp_data.last_armature_name = armature_key
        temp_data.last_entity_state_token = entity_state_token
    except Exception:
        pass
    if len(temp_data.equipment_entries) == 0:
        temp_data.equipment_entries_index = -1
    else:
        temp_data.equipment_entries_index = min(max(0, temp_data.equipment_entries_index), len(temp_data.equipment_entries) - 1)


def _get_entity_and_appearance(rig_settings):
    """Load entity and current appearance from runtime cache. Returns (entity, appearance) or (None, None)."""
    app_index = int(getattr(rig_settings, "app_list_index", -1))
    try:
        rig_key = rig_settings.as_pointer()
    except Exception:
        rig_key = id(rig_settings)
    entity, _entity_data = import_entity.get_rig_entity_state(rig_settings)
    cache_key = (rig_key, id(entity), app_index)
    cached = _ENTITY_APPEARANCE_CACHE.get(cache_key)
    if cached is not None:
        return cached

    if entity is None:
        return None, None

    appearances = getattr(entity, 'appearances', [])
    if app_index >= 0 and app_index < len(appearances):
        result = (entity, appearances[app_index])
    else:
        result = (entity, None)

    _ENTITY_APPEARANCE_CACHE[cache_key] = result
    _clear_cache_if_oversized(_ENTITY_APPEARANCE_CACHE, max_entries=32)
    return result


def _prepare_equipment_load_context(armature, rig_settings, prepared_context=None):
    prepared = prepared_context if prepared_context is not None else {}
    prepared.setdefault("rig_settings", rig_settings)

    source_roots = prepared.get("source_roots")
    if source_roots is None:
        source_roots = _get_armature_source_roots(armature)
        if not source_roots:
            repo_path_hint = getattr(rig_settings, "repo_path", "") or ""
            if repo_path_hint and os.path.isabs(repo_path_hint):
                try:
                    source_roots = _normalize_unique_roots([
                        import_entity._derive_repo_root_hint(repo_path_hint),
                        os.path.dirname(os.path.normpath(repo_path_hint)),
                    ])
                except Exception:
                    source_roots = []
        prepared["source_roots"] = source_roots

    if "entity" not in prepared or "appearance" not in prepared:
        entity, appearance = _get_entity_and_appearance(rig_settings)
        prepared.setdefault("entity", entity)
        prepared.setdefault("appearance", appearance)

    prepared.setdefault("bundle_item_cache", {})
    prepared.setdefault("item_entity_cache", {})
    return prepared

def _get_shared_equipment_group(armature, rig_settings, *, suffix, marker_name, hidden):
    """Find or create a shared equipment empty under the armature."""
    group_name = f"{rig_settings.entity_name}_{suffix}"
    owner_armature_name = getattr(armature, "name_full", getattr(armature, "name", ""))
    inv_group = None
    for child in armature.children:
        if child.type != 'EMPTY':
            continue
        if child.name == group_name or child.get(marker_name):
            inv_group = child
            break
    if inv_group is None:
        inv_group = bpy.data.objects.new(group_name, None)
        linked = False
        for collection in getattr(armature, "users_collection", []) or []:
            try:
                collection.objects.link(inv_group)
                linked = True
                break
            except Exception:
                continue
        if not linked:
            try:
                bpy.context.collection.objects.link(inv_group)
            except Exception:
                bpy.context.scene.collection.objects.link(inv_group)
    inv_group.name = group_name
    inv_group.parent = armature
    inv_group.parent_type = 'OBJECT'
    try:
        inv_group.matrix_parent_inverse = Matrix.Identity(4)
        inv_group.matrix_local = Matrix.Identity(4)
    except Exception:
        pass
    inv_group.empty_display_type = 'PLAIN_AXES'
    inv_group.empty_display_size = 0.02
    inv_group[marker_name] = True
    inv_group["witcher_owner_armature"] = owner_armature_name
    _clear_internal_inventory_group_state(inv_group)
    if hasattr(inv_group, "show_relationship_lines"):
        inv_group.show_relationship_lines = False
    inv_group.hide_set(bool(hidden))
    inv_group.hide_render = bool(hidden)
    return inv_group


def _get_inventory_group(armature, rig_settings):
    """Find or create a shared inventory empty under the armature."""
    return _get_shared_equipment_group(
        armature,
        rig_settings,
        suffix="inventory",
        marker_name="witcher_inventory_group",
        hidden=True,
    )


def _get_persistent_equipment_group(armature, rig_settings):
    """Find or create a shared manual-equipment empty under the armature."""
    return _get_shared_equipment_group(
        armature,
        rig_settings,
        suffix="equipment",
        marker_name="witcher_persistent_equipment_group",
        hidden=False,
    )

def _load_equipment_item_core(context, armature, slot_index, rig_settings=None, prepared_context=None,
                              refresh_variants_before_load=True, post_refresh_variants=True,
                              mount_mode=None):
    if rig_settings is None:
        rig_settings = armature.data.witcherui_RigSettings

    _set_last_equipment_load_failure(armature, slot_index, None)
    slot = rig_settings.equipment_slots[slot_index]

    if not getattr(slot, "base_equip_template", ""):
        slot.base_equip_template = slot.equip_template

    if refresh_variants_before_load:
        try:
            refresh_variant_states(rig_settings)
        except Exception:
            pass

    requested_mount_mode = str(mount_mode or "").strip().lower()
    if requested_mount_mode not in {"equip", "hold"}:
        requested_mount_mode = "hold" if (slot.is_loaded and slot.is_in_hold_slot) else "equip"
    allow_unmounted_visual_load = False

    effective_template = get_effective_equip_template(slot)
    if not effective_template or effective_template == "None":
        _set_last_equipment_load_failure(
            armature, slot_index,
            f"No effective template (item='{getattr(slot, 'item_name', '')}', base='{getattr(slot, 'equip_template', '')}')"
        )
        return False

    prepared = _prepare_equipment_load_context(armature, rig_settings, prepared_context)
    target_key = "hold_target" if requested_mount_mode == "hold" else "equip_target"
    target_label = "hold" if requested_mount_mode == "hold" else "equip"

    source_roots = prepared.get("source_roots", [])
    final_item, export_path, _search_pattern = _resolve_bundle_item_by_template_cached(
        effective_template,
        search_roots=source_roots,
        prepared_context=prepared,
    )
    if not final_item:
        reason = f"Template not resolved: '{effective_template}' (search={_search_pattern})"
        _set_last_equipment_load_failure(armature, slot_index, reason)
        log.warning(reason)
        return False

    log.info(f"Exporting to: {export_path}")
    if not export_path or not os.path.exists(export_path):
        reason = f"Resolved template has no exported file: '{getattr(final_item, 'name', effective_template)}' -> '{export_path}'"
        _set_last_equipment_load_failure(armature, slot_index, reason)
        log.warning(reason)
        return False

    resolved_repo_path = str(getattr(final_item, "name", "") or "").replace("/", "\\").lstrip("\\")
    if not resolved_repo_path:
        try:
            from ..importers.import_mesh import get_repo_from_abs_path
            resolved_repo_path = str(get_repo_from_abs_path(export_path) or "").replace("/", "\\").lstrip("\\")
        except Exception:
            resolved_repo_path = ""
    slot.source_game = _normalize_source_game(get_equipment_source_game_for_search_roots(source_roots))
    slot.resolved_repo_path = resolved_repo_path

    # Populate item appearance list from entity (runs even if already loaded)
    item_entity_for_apps = _get_cached_equipment_item_entity(export_path, prepared_context=prepared)
    if item_entity_for_apps and getattr(item_entity_for_apps, 'appearances', None):
        app_names = [getattr(a, 'name', '') for a in item_entity_for_apps.appearances]
        slot.item_appearances_json = json.dumps([n for n in app_names if n])
        _update_slot_coloring_json(slot, item_entity_for_apps)
    else:
        slot.item_appearances_json = ""
        slot.item_coloring_json = ""

    attachment_profile = import_entity.classify_equipment_attachment_profile(item_entity_for_apps)
    allow_unmounted_visual_load = (
        requested_mount_mode == "equip"
        and _allow_unmounted_slotless_visual(
            slot,
            attachment_profile=attachment_profile,
            item_entity=item_entity_for_apps,
        )
    )
    _maybe_log_legacy_attachment_type_conflict(
        getattr(slot, "item_name", "") or effective_template,
        getattr(slot, "attachment_type", ""),
        attachment_profile,
    )
    slot_policy = _resolve_slot_visual_policy(
        slot,
        armature,
        rig_settings,
        item_entity=item_entity_for_apps,
        attachment_profile=attachment_profile,
    )
    target_info = slot_policy[target_key]
    target_armature = target_info.get("armature") or armature
    mount_strategy = _infer_equipment_mount_strategy(
        attachment_profile,
        target_info,
        allow_unmounted_visual=allow_unmounted_visual_load,
    )
    log.info(
        "Equipment attachment '%s': profile=%s strategy=%s target=%s",
        getattr(slot, "item_name", "") or effective_template,
        getattr(attachment_profile, "kind", "") or "unknown",
        mount_strategy,
        _describe_mount_target(target_info),
    )
    if mount_strategy == "nonvisual":
        reason = f"Template '{effective_template}' resolves to inventory data only; no visual entity to load"
        _set_last_equipment_load_failure(armature, slot_index, reason)
        return False
    if mount_strategy == "invalid_target":
        if requested_mount_mode == "equip" and slot_policy["hold_valid"]:
            reason = f"No valid equip slot on current rig for '{slot.item_name}'; use Hold instead"
        else:
            reason = f"No valid {target_label} target on current rig for '{slot.item_name}'"
        _set_last_equipment_load_failure(armature, slot_index, reason)
        return False

    entity = prepared.get("entity")
    appearance = prepared.get("appearance")
    if entity is None:
        if allow_unmounted_visual_load:
            try:
                from ..CR2W import w3_types
                entity = w3_types.Entity()
                entity.name = armature.name
                entity.appearances = []
                entity.slots = []
                entity.coloringEntries = []
                log.debug(
                    "Entity state unavailable for '%s'; using minimal fallback for unmounted visual import",
                    armature.name,
                )
            except Exception:
                reason = "Could not parse entity/appearance from rig settings JSON (fallback also failed)"
                _set_last_equipment_load_failure(armature, slot_index, reason)
                log.warning(reason)
                return False
        else:
            reason = "Could not parse entity/appearance from rig settings JSON"
            _set_last_equipment_load_failure(armature, slot_index, reason)
            log.warning(reason)
            return False

    empty_transform = None
    if getattr(slot, "is_inventory", False):
        if allow_unmounted_visual_load and not target_info.get("is_valid"):
            empty_transform = _get_persistent_equipment_group(armature, rig_settings)
        else:
            empty_transform = _get_inventory_group(armature, rig_settings)
    elif getattr(slot, "keep_across_appearances", False):
        empty_transform = _get_persistent_equipment_group(armature, rig_settings)
    else:
        if appearance:
            for child in armature.children:
                if child.type == 'EMPTY' and child.name == appearance.name:
                    empty_transform = child
                    break
        if empty_transform is None:
            bpy.ops.object.empty_add(type="PLAIN_AXES", radius=1)
            empty_transform = bpy.context.object
            empty_transform.name = "equipment_group"
            empty_transform.parent = armature

    if slot.is_loaded and slot.equip_guid:
        remove_objects_by_guid(slot.equip_guid, "witcher_equip_guid")
        slot.equip_guid = ""
        slot.is_loaded = False
        slot.is_in_hold_slot = False

    guid = generate_guid()
    before = set(bpy.data.objects)
    import_info = {
        "template_keys": set(),
        "item_entity": item_entity_for_apps,
        "attachment_profile": attachment_profile,
        "selected_appearance_name": "",
    }

    saved_world = _temp_reset_armature_world(armature)
    changed_poses = _set_pose_all_armatures(armature, "REST")
    try:
        import_info = _import_item_entity(
            export_path,
            final_item.name,
            entity,
            armature,
            appearance,
            slot_index,
            empty_transform,
            use_app_drivers=_slot_uses_appearance_drivers(slot),
            prepared_context=prepared,
            item_appearance_name=getattr(slot, 'item_appearance_name', None) or None,
            attachment_profile=attachment_profile,
            bind_root_chunks_to_entity=_should_bind_root_chunks_to_entity(
                attachment_profile,
                mount_strategy,
            ),
        )
        if not (set(bpy.data.objects) - before):
            log.info(
                "Equipment common import produced no objects for '%s'; falling back to direct entity import.",
                export_path,
            )
            import_entity.import_direct_entity_file(export_path, parent_transform=empty_transform)
            # Bind any armatures produced by the fallback import to the parent armature
            fallback_new = set(bpy.data.objects) - before
            for fb_obj in fallback_new:
                if fb_obj and fb_obj.type == 'ARMATURE' and fb_obj != armature:
                    try:
                        _constrain_bound_armature_to_target(fb_obj, armature)
                    except Exception as _fb_e:
                        log.warning("Failed to bind fallback armature '%s' to '%s': %s", fb_obj.name, armature.name, _fb_e)
    except Exception as e:
        reason = f"Import failed for '{getattr(final_item, 'name', effective_template)}': {e}"
        _set_last_equipment_load_failure(armature, slot_index, reason)
        raise
    finally:
        _restore_pose_all_armatures(changed_poses)
        _restore_armature_world(armature, saved_world)

    new_objects = tag_new_objects_with_guid(before, guid, "witcher_equip_guid")
    if not new_objects:
        reason = f"Import produced no objects for '{getattr(final_item, 'name', effective_template)}'"
        _set_last_equipment_load_failure(armature, slot_index, reason)
        log.warning(reason)
        return False
    slot.equip_guid = guid
    slot.is_loaded = True

    try:
        selected_item_appearance = str(import_info.get("selected_appearance_name", "") or "").strip()
        if selected_item_appearance == "__default__":
            selected_item_appearance = ""
        import_entity.stamp_import_origin(
            new_objects,
            origin="equipment_slot",
            entity_path=slot.resolved_repo_path,
            source_game=slot.source_game,
            item_category=slot.category,
            item_name=slot.item_name,
            equip_template=effective_template or slot.equip_template,
            item_appearance=selected_item_appearance,
            owner_entity_path=getattr(rig_settings, "repo_path", ""),
        )
    except Exception as e:
        log.warning("Failed to stamp equipment import origin for '%s': %s", slot.item_name, e)

    try:
        import_entity.initialize_imported_entity_armatures(
            new_objects,
            import_info.get("item_entity") or item_entity_for_apps,
            filename=export_path,
            selected_appearance_name=import_info.get("selected_appearance_name", ""),
            update_json=True,
            context_role="auxiliary",
        )
    except Exception as e:
        log.warning("Failed to initialize equipment entity state for '%s': %s", slot.item_name, e)

    # Apply coloring entries from the item entity to newly imported mesh objects.
    # The character entity's coloringEntries don't cover equipment items, so we
    # apply the item entity's own coloring here using witcher_name for matching.
    try:
        item_coloring_entries = getattr(item_entity_for_apps, 'coloringEntries', None) or []
        selected_app_name = getattr(slot, 'item_appearance_name', '') or ''
        if not selected_app_name or selected_app_name == '__default__':
            # Use first appearance name as fallback
            app_names = json.loads(slot.item_appearances_json or '[]')
            selected_app_name = app_names[0] if app_names else ''
        if item_coloring_entries and selected_app_name:
            from ..importers.import_entity import _apply_coloring_entries_to_objects
            _apply_coloring_entries_to_objects(new_objects, item_coloring_entries, selected_app_name)
    except Exception as e:
        log.warning(f"Failed to apply coloring entries for '{slot.item_name}': {e}")

    try:
        bpy.context.view_layer.update()
    except Exception:
        pass

    slot_empty = None
    main_mount_anchor = None
    if target_info.get("target_type") == "slot":
        slot_empty = target_info.get("slot_empty")
    if new_objects:
        mount_roots = _collect_mount_roots(new_objects, ignored_objects={empty_transform})
        if mount_strategy == "slot_mount_animated" and target_info["is_valid"]:
            main_mount_anchor = _mount_animated_roots_with_anchor(
                mount_roots,
                slot.equip_guid,
                "main",
                empty_transform,
                slot_empty=slot_empty if target_info.get("target_type") == "slot" else None,
                armature=target_armature,
                bone_name=target_info.get("bone_name") if target_info.get("target_type") == "bone" else None,
            )
            if main_mount_anchor is not None:
                new_objects.add(main_mount_anchor)
            # Apply idle animation to any equipment armature that has one recorded
            if get_all_addon_prefs(context).import_idle_animation:
                for obj in new_objects:
                    if obj and obj.type == 'ARMATURE':
                        _load_idle_anim(context, obj)
        elif mount_strategy == "slot_mount_static" and target_info["is_valid"]:
            for root in mount_roots:
                _mount_object_to_target(root, target_info, fallback_armature=armature)

    slot.is_in_hold_slot = requested_mount_mode == "hold"

    try:
        _load_bound_items(
            context,
            armature,
            rig_settings,
            slot_index,
            slot,
            new_objects,
            empty_transform,
            slot_empty,
            target_armature=target_armature,
            prepared_context=prepared,
            imported_template_keys=import_info.get("template_keys", []),
        )
    except Exception as e:
        log.warning(f"Failed to load bound items for '{slot.item_name}': {e}")

    if post_refresh_variants:
        try:
            _refresh_variants_and_reload(context, armature, rig_settings)
        except Exception:
            pass

    _set_last_equipment_load_failure(armature, slot_index, None)
    return True


def load_equipment_item(context, armature, slot_index, rig_settings=None, mount_mode=None):
    """Load a single equipment item into the scene, tagged with GUID."""
    return _load_equipment_item_core(
        context,
        armature,
        slot_index,
        rig_settings=rig_settings,
        prepared_context=None,
        refresh_variants_before_load=True,
        post_refresh_variants=True,
        mount_mode=mount_mode,
    )


def load_equipment_items_batch(context, armature, slot_indices, rig_settings=None, prepared_context=None,
                               reload_loaded=False, post_refresh_variants=True, mount_mode="equip"):
    if rig_settings is None:
        rig_settings = armature.data.witcherui_RigSettings

    slots = rig_settings.equipment_slots
    unique_indices = []
    seen = set()
    for slot_index in slot_indices or []:
        try:
            idx = int(slot_index)
        except Exception:
            continue
        if idx < 0 or idx >= len(slots) or idx in seen:
            continue
        seen.add(idx)
        unique_indices.append(idx)

    if not unique_indices:
        return 0

    try:
        refresh_variant_states(rig_settings)
    except Exception:
        pass

    prepared = _prepare_equipment_load_context(armature, rig_settings, prepared_context)
    loaded = 0
    for idx in unique_indices:
        slot = slots[idx]
        if slot.is_loaded and slot.equip_guid and not reload_loaded:
            continue
        slot_policy = _resolve_slot_visual_policy(slot, armature, rig_settings)
        if mount_mode == "equip" and slot_policy["policy"] != "equipable_on_rig":
            continue
        if _load_equipment_item_core(
            context,
            armature,
            idx,
            rig_settings=rig_settings,
            prepared_context=prepared,
            refresh_variants_before_load=False,
            post_refresh_variants=False,
            mount_mode=mount_mode,
        ):
            loaded += 1

    if post_refresh_variants:
        try:
            _refresh_variants_and_reload(context, armature, rig_settings)
        except Exception:
            pass
    return loaded


def unload_equipment_item(slot):
    """Unload a single equipment item by removing its GUID-tagged objects."""
    if slot.is_loaded and slot.equip_guid:
        count = remove_objects_by_guid(slot.equip_guid, "witcher_equip_guid")
        slot.equip_guid = ""
        slot.is_loaded = False
        slot.is_in_hold_slot = False
        return count
    return 0


def load_template_item(context, armature, slot_index, rig_settings=None):
    """Load a single includedTemplate into the scene, tagged with GUID.

    Args:
        context: Blender context
        armature: The armature object
        slot_index: Index into rig_settings.template_slots
        rig_settings: Optional, will be fetched from armature if not provided

    Returns:
        True if template was loaded successfully, False otherwise
    """
    if rig_settings is None:
        rig_settings = armature.data.witcherui_RigSettings

    slot = rig_settings.template_slots[slot_index]

    if not slot.template_filename:
        return False

    # Unload existing if loaded
    if slot.is_loaded and slot.template_guid:
        remove_objects_by_guid(slot.template_guid, "witcher_template_guid")
        slot.template_guid = ""
        slot.is_loaded = False

    entity, appearance = _get_entity_and_appearance(rig_settings)
    if entity is None or appearance is None:
        return False

    guid = generate_guid()
    before = set(bpy.data.objects)

    from ..importers.import_entity import add_app_template, build_template_appearance_map
    ent_namespace = entity.name + ":"

    # Get ALL appearances that use this template from entity data
    try:
        template_map = build_template_appearance_map(entity)
        template_appearances = template_map.get(slot.template_filename, {}).get('indices', [])
    except Exception:
        template_appearances = []

    # Find the appearance empty group
    empty_transform = None
    for child in armature.children:
        if child.type == 'EMPTY' and child.name == appearance.name:
            empty_transform = child
            break
    if empty_transform is None:
        bpy.ops.object.empty_add(type="PLAIN_AXES", radius=1)
        empty_transform = bpy.context.object
        empty_transform.name = appearance.name
        empty_transform.parent = armature

    # Pass appearance indices so drivers are correct for all appearances
    saved_world = _temp_reset_armature_world(armature)
    changed_poses = _set_pose_all_armatures(armature, "REST")
    try:
        template_data = None
        if getattr(slot, "data_json", ""):
            try:
                template_data = json.loads(slot.data_json)
            except Exception:
                template_data = None
        add_app_template(entity, armature, entity.name, ent_namespace,
                         get_do_import_redcloth(context), slot_index, appearance,
                         True, empty_transform, False, slot.template_filename,
                         template_data=template_data,
                         appearance_indices=template_appearances)
    finally:
        _restore_pose_all_armatures(changed_poses)
        _restore_armature_world(armature, saved_world)

    new_objects = tag_new_objects_with_guid(before, guid, "witcher_template_guid")
    slot.template_guid = guid
    slot.is_loaded = True

    try:
        import_entity.stamp_import_origin(
            new_objects,
            origin="template_slot",
            entity_path=slot.template_filename,
            source_game=getattr(rig_settings, "source_game", "w3"),
            owner_entity_path=getattr(rig_settings, "repo_path", ""),
        )
    except Exception as e:
        log.warning("Failed to stamp template import origin for '%s': %s", slot.template_filename, e)
    
    # Restore armature as active object (Bug 2 fix)
    bpy.ops.object.select_all(action='DESELECT')
    armature.select_set(True)
    bpy.context.view_layer.objects.active = armature
    
    return True


def unload_template_item(slot):
    """Unload a single template item by removing its GUID-tagged objects."""
    if slot.is_loaded and slot.template_guid:
        count = remove_objects_by_guid(slot.template_guid, "witcher_template_guid")
        slot.template_guid = ""
        slot.is_loaded = False
        return count
    return 0


# =============================================================================
# Equipment Load/Unload Operators
# =============================================================================

class EQUIPMENT_OT_LoadEquipment(bpy.types.Operator):
    """Load equipment item(s) from bundles and attach to armature"""
    bl_idname = "witcher.equipment_load_equipment"
    bl_label = "Load Equipment"
    bl_options = {'REGISTER', 'UNDO'}

    slot_index: bpy.props.IntProperty(default=-1, description="Slot index (-1 = all)")

    def execute(self, context):
        # Save selection state
        saved_active = context.view_layer.objects.active
        saved_selection = [obj for obj in context.selected_objects]
        
        armature, rig_settings = _get_armature_and_rig_settings(context)
        if not armature:
            self.report({'WARNING'}, "No valid armature selected.")
            return {'CANCELLED'}

        slots = rig_settings.equipment_slots
        if len(slots) == 0:
            self.report({'WARNING'}, "No equipment slots defined.")
            return {'CANCELLED'}

        loaded = 0
        failed = 0
        failed_details = []
        with mod_loading_context(context):
            if self.slot_index == -1:
                for i in range(len(slots)):
                    slot_policy = _resolve_slot_visual_policy(slots[i], armature, rig_settings)
                    if slot_policy["policy"] != "equipable_on_rig":
                        continue
                    if load_equipment_item(context, armature, i, rig_settings):
                        loaded += 1
                    else:
                        if slots[i].equip_template and slots[i].equip_template != "None":
                            failed += 1
                            reason = _get_last_equipment_load_failure(armature, i) or "Unknown failure"
                            failed_details.append(
                                f"[{i}] {getattr(slots[i], 'item_name', '') or '<no item>'} | "
                                f"{get_effective_equip_template(slots[i]) or getattr(slots[i], 'equip_template', '') or '<no template>'}: {reason}"
                            )
            else:
                if self.slot_index < len(slots):
                    if load_equipment_item(context, armature, self.slot_index, rig_settings):
                        loaded += 1
                    else:
                        failed += 1
                        slot = slots[self.slot_index]
                        reason = _get_last_equipment_load_failure(armature, self.slot_index) or "Unknown failure"
                        failed_details.append(
                            f"[{self.slot_index}] {getattr(slot, 'item_name', '') or '<no item>'} | "
                            f"{get_effective_equip_template(slot) or getattr(slot, 'equip_template', '') or '<no template>'}: {reason}"
                        )

        # Restore selection state
        _safe_restore_selection(saved_active, saved_selection)

        msg = f"Loaded {loaded} equipment item(s)"
        if failed:
            msg += f", {failed} failed"
        self.report({'INFO'}, msg)
        if failed_details:
            preview = " | ".join(failed_details[:2])
            self.report({'WARNING'}, f"Equipment load failure details: {preview}")
            for detail in failed_details:
                log.warning("Equipment load failed: %s", detail)
        return {'FINISHED'}


class EQUIPMENT_OT_UnloadEquipment(bpy.types.Operator):
    """Unload equipment item(s) by removing GUID-tagged objects"""
    bl_idname = "witcher.equipment_unload_equipment"
    bl_label = "Unload Equipment"
    bl_options = {'REGISTER', 'UNDO'}

    slot_index: bpy.props.IntProperty(default=-1, description="Slot index (-1 = all)")

    def execute(self, context):
        # Save selection state
        saved_active = context.view_layer.objects.active
        saved_selection = [obj for obj in context.selected_objects]
        
        armature, rig_settings = _get_armature_and_rig_settings(context)
        if not armature:
            self.report({'WARNING'}, "No valid armature selected.")
            return {'CANCELLED'}

        slots = rig_settings.equipment_slots
        removed = 0
        if self.slot_index == -1:
            for slot in slots:
                removed += unload_equipment_item(slot)
        else:
            if self.slot_index < len(slots):
                removed += unload_equipment_item(slots[self.slot_index])

        try:
            _refresh_variants_and_reload(context, armature, rig_settings)
        except Exception:
            pass

        # Restore selection state
        _safe_restore_selection(saved_active, saved_selection)

        self.report({'INFO'}, f"Removed {removed} object(s)")
        return {'FINISHED'}


# =============================================================================
# Template Load/Unload/Refresh Operators
# =============================================================================

class EQUIPMENT_OT_LoadTemplate(bpy.types.Operator):
    """Load template item(s) and attach to armature"""
    bl_idname = "witcher.equipment_load_template"
    bl_label = "Load Template"
    bl_options = {'REGISTER', 'UNDO'}

    slot_index: bpy.props.IntProperty(default=-1, description="Slot index (-1 = all for current appearance)")

    def execute(self, context):
        armature, rig_settings = _get_armature_and_rig_settings(context)
        if not armature:
            self.report({'WARNING'}, "No valid armature selected.")
            return {'CANCELLED'}

        slots = rig_settings.template_slots
        if len(slots) == 0:
            self.report({'WARNING'}, "No template slots defined.")
            return {'CANCELLED'}

        current_app = get_current_appearance_name(rig_settings)
        
        loaded = 0
        skipped = 0
        failed = 0
        if self.slot_index == -1:
            # Load All: only load templates for current appearance
            for i, slot in enumerate(slots):
                if not template_belongs_to_appearance(slot, current_app):
                    continue  # Skip templates not in current appearance
                if slot.is_loaded:
                    skipped += 1
                    continue  # Skip already loaded (Bug 5 efficiency fix)
                if load_template_item(context, armature, i, rig_settings):
                    loaded += 1
                elif slot.template_filename:
                    failed += 1
        else:
            # Single item: load regardless of appearance
            if self.slot_index < len(slots):
                if load_template_item(context, armature, self.slot_index, rig_settings):
                    loaded += 1
                else:
                    failed += 1

        msg = f"Loaded {loaded} template(s)"
        if skipped:
            msg += f", {skipped} already loaded"
        if failed:
            msg += f", {failed} failed"
        self.report({'INFO'}, msg)
        return {'FINISHED'}


class EQUIPMENT_OT_LoadAllAppearances(bpy.types.Operator):
    """Load all templates from all appearances in the entity"""
    bl_idname = "witcher.equipment_load_all_appearances"
    bl_label = "Load All Appearances"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        armature, rig_settings = _get_armature_and_rig_settings(context)
        if not armature:
            self.report({'WARNING'}, "No valid armature selected.")
            return {'CANCELLED'}

        entity, _ = _get_entity_and_appearance(rig_settings)
        if entity is None:
            self.report({'ERROR'}, "Failed to load entity data.")
            return {'CANCELLED'}

        _entity, entity_data = import_entity.get_rig_entity_state(rig_settings)
        if entity_data is None:
            self.report({'ERROR'}, "Failed to load cached entity data.")
            return {'CANCELLED'}

        appearances = entity_data.get('appearances', [])
        if not appearances:
            self.report({'WARNING'}, "No appearances found.")
            return {'CANCELLED'}

        from ..importers.import_entity import import_app
        from ..CR2W import w3_types

        loaded_appearances = 0
        original_index = rig_settings.app_list_index

        for app_index, appearance_data in enumerate(appearances):
            # Create appearance object from data
            appearance = w3_types.CAppearance()
            appearance.name = appearance_data.get('name', f'appearance_{app_index}')
            appearance.includedTemplates = appearance_data.get('includedTemplates', [])
            
            # Set the index so import_app knows which appearance we're loading
            rig_settings.app_list_index = app_index
            
            try:
                import_app(context,
                          appearance,
                          entity,
                          armature)
                loaded_appearances += 1
            except Exception as e:
                log.error("Failed to load appearance %s: %s", app_index, e)

        # Restore original index
        rig_settings.app_list_index = original_index

        # Refresh slot constraints for sub-component armatures (like scabbards_skeleton)
        # These armatures may not exist during initial entity import
        refresh_count = refresh_slot_constraints(armature)
        
        msg = f"Loaded {loaded_appearances} appearance(s)"
        if refresh_count > 0:
            msg += f", updated {refresh_count} slot constraint(s)"
        self.report({'INFO'}, msg)
        return {'FINISHED'}


class EQUIPMENT_OT_UnloadTemplate(bpy.types.Operator):
    """Unload template item(s) by removing GUID-tagged objects"""
    bl_idname = "witcher.equipment_unload_template"
    bl_label = "Unload Template"
    bl_options = {'REGISTER', 'UNDO'}

    slot_index: bpy.props.IntProperty(default=-1, description="Slot index (-1 = all for current appearance)")

    def execute(self, context):
        armature, rig_settings = _get_armature_and_rig_settings(context)
        if not armature:
            self.report({'WARNING'}, "No valid armature selected.")
            return {'CANCELLED'}

        current_app = get_current_appearance_name(rig_settings)
        slots = rig_settings.template_slots
        
        # Get template->appearances map to check for shared templates
        try:
            entity, entity_data = import_entity.get_rig_entity_state(rig_settings)
            from ..importers.import_entity import build_template_appearance_map
            template_source = entity if entity is not None else entity_data
            template_map = build_template_appearance_map(template_source) if template_source else {}
        except Exception:
            template_map = {}
        
        removed = 0
        skipped_shared = 0
        
        if self.slot_index == -1:
            # Unload All: only unload templates EXCLUSIVE to current appearance
            for slot in slots:
                if template_belongs_to_appearance(slot, current_app):
                    # Check if template is used by other appearances
                    template_apps = template_map.get(slot.template_filename, {}).get('indices', [])
                    if len(template_apps) > 1:
                        # Shared template - skip unloading
                        skipped_shared += 1
                        continue
                    removed += unload_template_item(slot)
        else:
            # Single item: unload regardless (user explicitly requested)
            if self.slot_index < len(slots):
                removed += unload_template_item(slots[self.slot_index])

        msg = f"Removed {removed} object(s)"
        if skipped_shared:
            msg += f", skipped {skipped_shared} shared template(s)"
        self.report({'INFO'}, msg)
        return {'FINISHED'}


class EQUIPMENT_OT_RefreshTemplateData(bpy.types.Operator):
    """Re-read entity JSON and update the template slots list (does not load meshes)"""
    bl_idname = "witcher.equipment_refresh_template_data"
    bl_label = "Refresh Template Data"

    def execute(self, context):
        armature, rig_settings = _get_armature_and_rig_settings(context)
        if not armature:
            self.report({'WARNING'}, "No valid armature selected.")
            return {'CANCELLED'}

        _entity, entity_data = import_entity.get_rig_entity_state(rig_settings)
        if entity_data is None:
            self.report({'ERROR'}, "Failed to load cached entity data.")
            return {'CANCELLED'}

        appearances = entity_data.get('appearances', [])
        app_index = rig_settings.app_list_index
        if app_index < 0 or app_index >= len(appearances):
            self.report({'WARNING'}, "No valid appearance selected.")
            return {'CANCELLED'}

        selected_appearance = appearances[app_index]
        included_templates = selected_appearance.get('includedTemplates', [])

        # Build set of existing template filenames for tracking
        existing_filenames = {slot.template_filename for slot in rig_settings.template_slots}
        new_filenames = {t.get('templateFilename', '') for t in included_templates}

        # Remove stale slots (unload their objects first)
        indices_to_remove = []
        for i, slot in enumerate(rig_settings.template_slots):
            if slot.template_filename not in new_filenames:
                unload_template_item(slot)
                indices_to_remove.append(i)
        for i in reversed(indices_to_remove):
            rig_settings.template_slots.remove(i)

        # Add new slots
        for template_data in included_templates:
            filename = template_data.get('templateFilename', '')
            if filename and filename not in existing_filenames:
                slot = rig_settings.template_slots.add()
                slot.template_filename = filename
                slot.ns = template_data.get('ns', '')
                slot.data_json = json.dumps(template_data, indent=2)
                slot.is_loaded = False

        self.report({'INFO'}, f"Refreshed: {len(rig_settings.template_slots)} template(s)")
        return {'FINISHED'}


class EQUIPMENT_OT_HideTemplate(bpy.types.Operator):
    """Hide template objects in viewport without unloading (per-appearance)"""
    bl_idname = "witcher.equipment_hide_template"
    bl_label = "Hide Template"
    bl_options = {'REGISTER', 'UNDO'}

    slot_index: bpy.props.IntProperty(default=-1, description="Slot index (-1 = all)")

    def execute(self, context):
        armature, rig_settings = _get_armature_and_rig_settings(context)
        if not armature:
            self.report({'WARNING'}, "No valid armature selected.")
            return {'CANCELLED'}

        current_app = get_current_appearance_name(rig_settings)
        if not current_app:
            self.report({'WARNING'}, "No appearance selected.")
            return {'CANCELLED'}

        slots = rig_settings.template_slots
        hidden = 0
        if self.slot_index == -1:
            for slot in slots:
                if slot.is_loaded and template_belongs_to_appearance(slot, current_app):
                    is_hidden = get_hidden_in_appearance(slot, current_app)
                    if not is_hidden:
                        hide_objects_by_guid(slot.template_guid, "witcher_template_guid", hidden=True)
                        set_hidden_in_appearance(slot, current_app, True)
                        slot.is_hidden = True
                        hidden += 1
        else:
            if self.slot_index < len(slots):
                slot = slots[self.slot_index]
                if slot.is_loaded:
                    hide_objects_by_guid(slot.template_guid, "witcher_template_guid", hidden=True)
                    set_hidden_in_appearance(slot, current_app, True)
                    slot.is_hidden = True
                    hidden += 1

        self.report({'INFO'}, f"Hidden {hidden} template(s)")
        return {'FINISHED'}


class EQUIPMENT_OT_ShowTemplate(bpy.types.Operator):
    """Show hidden template objects in viewport (per-appearance)"""
    bl_idname = "witcher.equipment_show_template"
    bl_label = "Show Template"
    bl_options = {'REGISTER', 'UNDO'}

    slot_index: bpy.props.IntProperty(default=-1, description="Slot index (-1 = all)")

    def execute(self, context):
        armature, rig_settings = _get_armature_and_rig_settings(context)
        if not armature:
            self.report({'WARNING'}, "No valid armature selected.")
            return {'CANCELLED'}

        current_app = get_current_appearance_name(rig_settings)
        if not current_app:
            self.report({'WARNING'}, "No appearance selected.")
            return {'CANCELLED'}

        slots = rig_settings.template_slots
        shown = 0
        if self.slot_index == -1:
            for slot in slots:
                if slot.is_loaded and template_belongs_to_appearance(slot, current_app):
                    is_hidden = get_hidden_in_appearance(slot, current_app)
                    if is_hidden:
                        hide_objects_by_guid(slot.template_guid, "witcher_template_guid", hidden=False)
                        set_hidden_in_appearance(slot, current_app, False)
                        slot.is_hidden = False
                        shown += 1
        else:
            if self.slot_index < len(slots):
                slot = slots[self.slot_index]
                if slot.is_loaded:
                    hide_objects_by_guid(slot.template_guid, "witcher_template_guid", hidden=False)
                    set_hidden_in_appearance(slot, current_app, False)
                    slot.is_hidden = False
                    shown += 1

        self.report({'INFO'}, f"Shown {shown} template(s)")
        return {'FINISHED'}


class EQUIPMENT_OT_SyncTemplatesToAppearance(bpy.types.Operator):
    """Sync template visibility to the currently selected appearance"""
    bl_idname = "witcher.equipment_sync_templates_to_appearance"
    bl_label = "Sync Templates to Appearance"

    def execute(self, context):
        armature, rig_settings = _get_armature_and_rig_settings(context)
        if not armature:
            self.report({'WARNING'}, "No valid armature selected.")
            return {'CANCELLED'}

        _entity, entity_data = import_entity.get_rig_entity_state(rig_settings)
        if entity_data is None:
            self.report({'ERROR'}, "Failed to load cached entity data.")
            return {'CANCELLED'}

        appearances = entity_data.get('appearances', [])
        app_index = rig_settings.app_list_index
        if app_index < 0 or app_index >= len(appearances):
            self.report({'WARNING'}, "No valid appearance selected.")
            return {'CANCELLED'}

        selected_appearance = appearances[app_index]
        new_filenames = set()
        for t in selected_appearance.get('includedTemplates', []):
            fn = t.get('templateFilename', '')
            if fn:
                new_filenames.add(fn)

        app_name = selected_appearance.get('name', str(app_index))
        existing_filenames = {slot.template_filename for slot in rig_settings.template_slots}

        # Update appearance tracking and drivers for all templates
        # Drivers handle visibility automatically - no manual hide/show needed
        from ..importers.import_entity import update_template_drivers_for_appearances
        
        for slot in rig_settings.template_slots:
            if slot.template_filename in new_filenames:
                # Template is used by this appearance - track it
                app_names_set = set(slot.appearance_names.split(',')) if slot.appearance_names else set()
                app_names_set.discard('')
                app_names_set.add(app_name)
                slot.appearance_names = ','.join(app_names_set)
                
                # Update drivers to show for all appearances that use this template
                if slot.is_loaded and slot.template_guid:
                    update_template_drivers_for_appearances(slot.template_guid, rig_settings)
                    
                    # Apply per-appearance hidden state
                    is_hidden_for_this_app = get_hidden_in_appearance(slot, app_name)
                    hide_objects_by_guid(slot.template_guid, "witcher_template_guid", hidden=is_hidden_for_this_app)
                    slot.is_hidden = is_hidden_for_this_app

        # Add new template slots for templates not yet in the list
        for template_data in selected_appearance.get('includedTemplates', []):
            filename = template_data.get('templateFilename', '')
            if filename and filename not in existing_filenames:
                slot = rig_settings.template_slots.add()
                slot.template_filename = filename
                slot.ns = template_data.get('ns', '')
                slot.data_json = json.dumps(template_data, indent=2)
                slot.is_loaded = False
                slot.is_hidden = False
                slot.appearance_names = app_name

        self.report({'INFO'}, f"Synced templates to appearance '{app_name}'")
        return {'FINISHED'}


classes = [
    EquipmentDefinitionEntry,
    IncludedTemplateEntry,
    WitcherUITempData,
    EQUIPMENT_UL_CategoryList,
    EQUIPMENT_UL_IncludedTemplateList,
    EQUIPMENT_OT_SearchCategory,
    EQUIPMENT_OT_SearchDefaultItem,
    EQUIPMENT_OT_AddCategory,
    EQUIPMENT_OT_RemoveCategory,
    EQUIPMENT_OT_AddIncludedTemplate,
    EQUIPMENT_OT_RemoveIncludedTemplate,
    EQUIPMENT_OT_LoadIncludedTemplateData,
    EQUIPMENT_OT_RefreshCategories,
    EQUIPMENT_OT_InsertDefaultCategories,
    EQUIPMENT_OT_SaveEquipmentEntries,
    EQUIPMENT_OT_ToggleItem,
    EQUIPMENT_OT_ToggleVariantMode,
    EQUIPMENT_OT_HideEquipment,
    EQUIPMENT_OT_ShowEquipment,
    EQUIPMENT_OT_HideBoundItem,
    EQUIPMENT_OT_ShowBoundItem,
    EQUIPMENT_OT_CopyResolvedGamePath,
    EQUIPMENT_OT_OpenResolvedPathFolder,
    EQUIPMENT_OT_LoadEquipment,
    EQUIPMENT_OT_UnloadEquipment,
    EQUIPMENT_OT_LoadTemplate,
    EQUIPMENT_OT_LoadAllAppearances,
    EQUIPMENT_OT_UnloadTemplate,
    EQUIPMENT_OT_RefreshTemplateData,
    EQUIPMENT_OT_HideTemplate,
    EQUIPMENT_OT_ShowTemplate,
    EQUIPMENT_OT_SyncTemplatesToAppearance,
    EQUIPMENT_OT_ToggleEntitySlots,
    EQUIPMENT_OT_RefreshSlotConstraints,
    EQUIPMENT_PT_MainPanel,
]

# Register classes and properties
def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.WindowManager.witcherui_temp_data = bpy.props.PointerProperty(type=WitcherUITempData)

    # Load cached categories on startup
    _load_category_cache()

def unregister():
    if hasattr(bpy.types.WindowManager, "witcherui_temp_data"):
        del bpy.types.WindowManager.witcherui_temp_data
    for c in reversed(classes):
        bpy.utils.unregister_class(c)

if __name__ == "__main__":
    register()
