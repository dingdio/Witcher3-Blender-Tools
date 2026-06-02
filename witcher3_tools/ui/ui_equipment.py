import bpy
import os
import json
import time
import uuid
import logging
import re
import hashlib
import shutil
from contextlib import contextmanager
from bpy.app.handlers import persistent
from mathutils import Matrix
from types import SimpleNamespace

log = logging.getLogger(__name__)
from ..CR2W.witcher_cache.CacheController import CacheController
from ..CR2W.witcher_cache.Bundles import LoadBundleManager
from ..CR2W.witcher_cache.Bundles.BundleItem import BundleItem
from ..CR2W.common_blender import repo_file, mod_loading_context
from ..importers import import_entity
from ..importers import import_isolation
from ..importers.import_anims import load_idle_animation_for_armature as _load_idle_anim
from ..CR2W.dc_entity import LoadCEntityTemplateFile  # Import the function as per your setup
from ..extension_paths import get_cache_root
from .. import (
    get_all_addon_prefs,
    get_uncook_path,
    get_do_import_redcloth,
)
from pathlib import Path
from .. import get_rig_rot90_enabled
from .armature_context import (
    get_main_armature_and_rig_settings,
)
from ..source_game_paths import normalize_source_game as _normalize_source_game
from . import equipment_catalog
from .equipment_item_picker import (
    EquipmentItemPickerRow,
    EquipmentPresetPickerRow,
    EQUIPMENT_OT_PickDefaultItem,
    EQUIPMENT_OT_PickInventoryPreset,
    EQUIPMENT_OT_DeleteInventoryPreset,
    EQUIPMENT_OT_ShowInventoryPresetDetails,
    EQUIPMENT_OT_ItemPickerPage,
    EQUIPMENT_OT_PresetPickerPage,
    EQUIPMENT_OT_SearchDefaultItem,
    _on_equipment_item_picker_index_changed,
    _on_equipment_item_picker_filter_changed,
    _on_equipment_preset_picker_filter_changed,
    draw_inventory_preset_picker,
    inventory_preset_picker_width,
)

_UNCOOK_ITEM_ENT_INDEX = {}
_LAST_EQUIPMENT_LOAD_FAILURES = {}
_OPERATOR_ENUM_CACHE = {}
_EQUIPMENT_ITEM_ICON_ID_CACHE = {}
_EQUIPMENT_ITEM_ICON_REQUESTS = []
_EQUIPMENT_ITEM_ICON_PENDING_KEYS = set()
_EQUIPMENT_ITEM_ICON_TIMER_RUNNING = False
# Persistent icon cache: resolved preview images are copied into a cache dir and
# their paths recorded to JSON so icons load once and survive Blender restarts
# (no re-resolution / no UI lag on later sessions).
_EQUIPMENT_ICON_PREVIEWS = None
_EQUIPMENT_ICON_PATH_DISK = None          # stable_key(str) -> persistent image path ("" = unresolvable)
_EQUIPMENT_ICON_PATH_DISK_DIRTY = False
_EQUIPMENT_ICON_PATH_CACHE_FILE = Path(get_cache_root(create=True)) / "equipment_icon_paths.json"
_EQUIPMENT_ICON_PERSIST_DIR = Path(get_cache_root(create=True)) / "equipment_icons"
_ENTITY_APPEARANCE_CACHE = {}
_EQUIPMENT_ENTITY_CACHE = {}
_TEMPLATE_PATH_RESOLVE_CACHE = {}  # (template_key, roots_tuple) -> (repo_path, export_path)
# How many icons the background timer resolves per tick. Resolution happens on
# the main thread (DDS extraction), so keep this small to avoid UI stutter.
# Placeholders are shown instantly, so a low value here only affects how fast
# real thumbnails stream in — not whether the grid looks complete.
_EQUIPMENT_ITEM_ICON_BATCH_SIZE = 2

# Lazily-built neutral placeholder shown at the exact size of a real icon while
# the real one is still resolving (or can't be resolved at all).
_EQUIPMENT_PLACEHOLDER_PREVIEWS = None
_EQUIPMENT_PLACEHOLDER_ICON_ID = 0

def _clear_cache_if_oversized(cache, max_entries=64):
    if len(cache) > max_entries:
        cache.clear()


def _clear_equipment_item_icon_cache():
    _EQUIPMENT_ITEM_ICON_ID_CACHE.clear()
    _EQUIPMENT_ITEM_ICON_REQUESTS.clear()
    _EQUIPMENT_ITEM_ICON_PENDING_KEYS.clear()


def _get_equipment_placeholder_icon_id():
    """Return a stable icon_id for a neutral 'unresolved item' placeholder.

    Reuses the browser dummy-icon generator so the placeholder is a real preview
    image and therefore scales identically to a resolved icon when drawn with
    ``template_icon(scale=...)`` — giving same-sized tiles whether or not the
    real icon has loaded yet.
    """
    global _EQUIPMENT_PLACEHOLDER_PREVIEWS, _EQUIPMENT_PLACEHOLDER_ICON_ID
    if _EQUIPMENT_PLACEHOLDER_ICON_ID:
        return _EQUIPMENT_PLACEHOLDER_ICON_ID
    try:
        from . import asset_previews

        png_path = asset_previews.ensure_dummy_icon_path("ITEM", "item")
        if not png_path:
            return 0
        if _EQUIPMENT_PLACEHOLDER_PREVIEWS is None:
            _EQUIPMENT_PLACEHOLDER_PREVIEWS = bpy.utils.previews.new()
        key = "equipment_item_placeholder"
        icon = _EQUIPMENT_PLACEHOLDER_PREVIEWS.get(key)
        if icon is None:
            icon = _EQUIPMENT_PLACEHOLDER_PREVIEWS.load(key, png_path, 'IMAGE')
        _EQUIPMENT_PLACEHOLDER_ICON_ID = int(getattr(icon, "icon_id", 0) or 0)
    except Exception:
        log.debug("Failed to build equipment placeholder icon", exc_info=True)
        _EQUIPMENT_PLACEHOLDER_ICON_ID = 0
    return _EQUIPMENT_PLACEHOLDER_ICON_ID


def _clear_equipment_placeholder_icon():
    global _EQUIPMENT_PLACEHOLDER_PREVIEWS, _EQUIPMENT_PLACEHOLDER_ICON_ID
    if _EQUIPMENT_PLACEHOLDER_PREVIEWS is not None:
        try:
            bpy.utils.previews.remove(_EQUIPMENT_PLACEHOLDER_PREVIEWS)
        except Exception:
            pass
    _EQUIPMENT_PLACEHOLDER_PREVIEWS = None
    _EQUIPMENT_PLACEHOLDER_ICON_ID = 0


# --- Persistent equipment icon cache (load once, survive restart, no lag) -----

def _equipment_icon_stable_key(cache_key_tuple):
    """Stable, JSON-safe key for the cross-session icon path cache."""
    return hashlib.sha1(repr(cache_key_tuple).encode("utf-8", "ignore")).hexdigest()


def _load_equipment_icon_path_disk():
    global _EQUIPMENT_ICON_PATH_DISK
    if _EQUIPMENT_ICON_PATH_DISK is not None:
        return _EQUIPMENT_ICON_PATH_DISK
    data = {}
    try:
        if _EQUIPMENT_ICON_PATH_CACHE_FILE.exists():
            with open(_EQUIPMENT_ICON_PATH_CACHE_FILE, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                data = {str(k): str(v or "") for k, v in loaded.items()}
    except Exception:
        log.debug("Failed to load equipment icon path cache", exc_info=True)
        data = {}
    _EQUIPMENT_ICON_PATH_DISK = data
    return data


def _mark_equipment_icon_disk_dirty():
    global _EQUIPMENT_ICON_PATH_DISK_DIRTY
    _EQUIPMENT_ICON_PATH_DISK_DIRTY = True


def _save_equipment_icon_path_disk():
    global _EQUIPMENT_ICON_PATH_DISK_DIRTY
    if _EQUIPMENT_ICON_PATH_DISK is None or not _EQUIPMENT_ICON_PATH_DISK_DIRTY:
        return
    try:
        _EQUIPMENT_ICON_PATH_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = str(_EQUIPMENT_ICON_PATH_CACHE_FILE) + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(_EQUIPMENT_ICON_PATH_DISK, handle)
        os.replace(tmp_path, str(_EQUIPMENT_ICON_PATH_CACHE_FILE))
        _EQUIPMENT_ICON_PATH_DISK_DIRTY = False
    except Exception:
        log.debug("Failed to save equipment icon path cache", exc_info=True)


def _persist_equipment_icon_image(src_path, stable_key):
    """Copy a resolved preview image into the persistent cache dir so it survives
    temp-dir cleanup and Blender restarts. Returns the persistent path or ""."""
    try:
        from ..CR2W.common_blender import win_path_exists, win_safe_path

        if not src_path or not win_path_exists(src_path):
            return ""
        ext = os.path.splitext(str(src_path))[1].lower() or ".png"
        _EQUIPMENT_ICON_PERSIST_DIR.mkdir(parents=True, exist_ok=True)
        dst_path = str(_EQUIPMENT_ICON_PERSIST_DIR / f"{stable_key}{ext}")
        if not win_path_exists(dst_path):
            shutil.copyfile(win_safe_path(src_path), dst_path)
        return dst_path
    except Exception:
        log.debug("Failed to persist equipment icon image", exc_info=True)
        return ""


def _equipment_icon_id_from_path(stable_key, image_path):
    """Lazily load a persisted preview image into our previews collection and
    return its icon_id. Loading is cheap (Blender decodes lazily on draw)."""
    global _EQUIPMENT_ICON_PREVIEWS
    try:
        from ..CR2W.common_blender import win_path_exists, win_safe_path

        if not image_path or not win_path_exists(image_path):
            return 0
        if _EQUIPMENT_ICON_PREVIEWS is None:
            _EQUIPMENT_ICON_PREVIEWS = bpy.utils.previews.new()
        pcoll = _EQUIPMENT_ICON_PREVIEWS
        icon = pcoll.get(stable_key)
        if icon is None:
            icon = pcoll.load(stable_key, win_safe_path(image_path), 'IMAGE')
        return int(getattr(icon, "icon_id", 0) or 0)
    except Exception:
        log.debug("Failed to load persisted equipment icon: %s", image_path, exc_info=True)
        return 0


def _clear_equipment_icon_previews():
    global _EQUIPMENT_ICON_PREVIEWS
    _save_equipment_icon_path_disk()
    if _EQUIPMENT_ICON_PREVIEWS is not None:
        try:
            bpy.utils.previews.remove(_EQUIPMENT_ICON_PREVIEWS)
        except Exception:
            pass
    _EQUIPMENT_ICON_PREVIEWS = None


def _clear_template_path_resolve_cache():
    _TEMPLATE_PATH_RESOLVE_CACHE.clear()


equipment_catalog.set_icon_cache_clear_callback(_clear_equipment_item_icon_cache)
equipment_catalog.set_template_cache_clear_callback(_clear_template_path_resolve_cache)


def _set_catalog_cache_flags(source_game="w3", *, schema_version=0, icon_path=False, hold_template=False):
    return equipment_catalog.set_catalog_cache_flags(
        source_game,
        schema_version=schema_version,
        icon_path=icon_path,
        hold_template=hold_template,
    )


def _infer_catalog_cache_flags(item_attributes):
    return equipment_catalog.infer_catalog_cache_flags(item_attributes)


def _catalog_has_browser_icon_fields(source_game="w3"):
    return equipment_catalog.catalog_has_browser_icon_fields(source_game)


def _get_category_cache_file(source_game="w3"):
    return equipment_catalog.get_category_cache_file(source_game)


def _catalog_key_for_source_game(source_game="w3"):
    return equipment_catalog.catalog_key_for_source_game(source_game)


def _get_equipment_catalog(source_game="w3"):
    return equipment_catalog.get_equipment_catalog(source_game)


def _clear_item_attribute_identifier_lookup(source_game=None):
    return equipment_catalog.clear_item_attribute_identifier_lookup(source_game)


def _normalize_item_attribute_identifier(value):
    return equipment_catalog.normalize_item_attribute_identifier(value)


def _iter_item_attribute_identifier_aliases(value):
    return equipment_catalog.iter_item_attribute_identifier_aliases(value)


def _get_item_attribute_identifier_lookup(source_game="w3"):
    return equipment_catalog.get_item_attribute_identifier_lookup(source_game)


def get_equipment_source_game_for_search_roots(search_roots=None):
    return equipment_catalog.get_equipment_source_game_for_search_roots(search_roots)


def get_equipment_catalog_for_search_roots(search_roots=None):
    return equipment_catalog.get_equipment_catalog_for_search_roots(search_roots)


def _get_active_equipment_catalog(context):
    return _get_equipment_catalog(_get_temp_source_game(context))


def _save_category_cache(source_game="w3"):
    return equipment_catalog.save_category_cache(source_game)


def _load_category_cache(source_game="w3"):
    return equipment_catalog.load_category_cache(source_game)


def _candidate_w2_items_dirs(search_roots=None, *, include_configured_roots=True):
    return equipment_catalog.candidate_w2_items_dirs(
        search_roots,
        include_configured_roots=include_configured_roots,
    )


def ensure_equipment_catalog_for_search_roots(search_roots=None):
    return equipment_catalog.ensure_equipment_catalog_for_search_roots(search_roots)


def _request_sync_templates():
    """No-op: automatic sync disabled to avoid Blender UI performance issues."""
    return


def _cache_operator_enum_items(cache_key, items):
    stable_items = []
    for index, item in enumerate(items or [("None", "None", "")]):
        identifier = str(item[0] or "None")
        label = str(item[1] or identifier)
        description = str(item[2] or "")
        if len(item) >= 5:
            icon = item[3]
            try:
                number = int(item[4])
            except Exception:
                number = index + 1
            stable_items.append((identifier, label, description, icon, number))
        else:
            stable_items.append((identifier, label, description))
    _OPERATOR_ENUM_CACHE[cache_key] = stable_items
    _clear_cache_if_oversized(_OPERATOR_ENUM_CACHE, max_entries=128)
    return stable_items


def _inventory_preset_store_path():
    return Path(get_cache_root(create=True)) / "inventory_presets.json"


def _inventory_preset_defaults_path():
    return Path(get_cache_root(create=True)) / "inventory_preset_defaults.json"


def _shipped_inventory_preset_store_path():
    return Path(__file__).resolve().parents[1] / "CR2W" / "data" / "inventory_presets_shipped.json"


def _load_inventory_preset_store():
    path = _inventory_preset_store_path()
    if not path.exists():
        return {"version": 1, "presets": []}
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        log.warning("Failed to read inventory preset store: %s", path, exc_info=True)
        return {"version": 1, "presets": []}
    if not isinstance(data, dict):
        return {"version": 1, "presets": []}
    presets = data.get("presets", [])
    if not isinstance(presets, list):
        presets = []
    return {"version": int(data.get("version", 1) or 1), "presets": presets}


def _save_inventory_preset_store(data):
    path = _inventory_preset_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "presets": list(data.get("presets", []) if isinstance(data, dict) else []),
    }
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=False)


def _load_inventory_preset_defaults():
    path = _inventory_preset_defaults_path()
    if not path.exists():
        return {"version": 1, "defaults": {}}
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        log.warning("Failed to read inventory preset defaults: %s", path, exc_info=True)
        return {"version": 1, "defaults": {}}
    if not isinstance(data, dict):
        return {"version": 1, "defaults": {}}
    defaults = data.get("defaults", {})
    if not isinstance(defaults, dict):
        defaults = {}
    return {"version": int(data.get("version", 1) or 1), "defaults": defaults}


def _save_inventory_preset_defaults(data):
    path = _inventory_preset_defaults_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "defaults": dict(data.get("defaults", {}) if isinstance(data, dict) else {}),
    }
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=False)


def _load_shipped_inventory_preset_store():
    path = _shipped_inventory_preset_store_path()
    if not path.exists():
        return {"version": 1, "presets": []}
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        log.warning("Failed to read shipped inventory presets: %s", path, exc_info=True)
        return {"version": 1, "presets": []}
    if not isinstance(data, dict):
        return {"version": 1, "presets": []}
    presets = data.get("presets", [])
    if not isinstance(presets, list):
        presets = []
    return {"version": int(data.get("version", 1) or 1), "presets": presets}


def _normalize_inventory_preset(preset, source="user", is_shipped=False):
    if not isinstance(preset, dict):
        return None
    preset_id = str(preset.get("id", "") or "").strip()
    name = str(preset.get("name", "") or "").strip()
    entries = preset.get("entries", [])
    if not preset_id or not name or not isinstance(entries, list):
        return None
    source_game = _normalize_source_game(preset.get("source_game", "w3"))
    normalized_entries = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        normalized_entry = dict(entry)
        normalized_entry["source_game"] = _normalize_source_game(normalized_entry.get("source_game", source_game))
        normalized_entries.append(normalized_entry)
    normalized = dict(preset)
    normalized["id"] = preset_id
    normalized["name"] = name
    normalized["entries"] = normalized_entries
    normalized["source"] = str(source or normalized.get("source") or "user")
    normalized["source_game"] = source_game
    normalized["is_shipped"] = bool(is_shipped)
    return normalized


def _load_shipped_inventory_presets():
    presets = []
    for preset in _load_shipped_inventory_preset_store().get("presets", []) or []:
        normalized = _normalize_inventory_preset(preset, source="shipped", is_shipped=True)
        if normalized:
            presets.append(normalized)
    return presets


def _load_user_inventory_presets():
    presets = []
    for preset in _load_inventory_preset_store().get("presets", []) or []:
        normalized = _normalize_inventory_preset(
            preset,
            source=str(preset.get("source", "") or "user") if isinstance(preset, dict) else "user",
            is_shipped=False,
        )
        if normalized:
            presets.append(normalized)
    return presets


def _load_inventory_presets(source_game=None):
    presets = _load_shipped_inventory_presets() + _load_user_inventory_presets()
    if source_game is None:
        return presets
    source_game = _normalize_source_game(source_game)
    return [
        preset for preset in presets
        if _normalize_source_game(preset.get("source_game", "w3")) == source_game
    ]


def _get_inventory_preset(preset_id, source_game=None):
    preset_id = str(preset_id or "").strip()
    if not preset_id:
        return None
    for preset in _load_inventory_presets(source_game=source_game):
        if str(preset.get("id", "") or "") == preset_id:
            return preset
    return None


def _inventory_preset_label(preset_id, fallback="Select Preset", source_game=None):
    preset = _get_inventory_preset(preset_id, source_game=source_game)
    if preset:
        return str(preset.get("name", "") or fallback)
    return fallback


def _normalize_inventory_preset_selection_target(target):
    target = str(target or "INVENTORY").strip().upper()
    if target in {"GERALT", "GERALT_W3", "W3_GERALT"}:
        return "GERALT_W3"
    if target in {"GERALT_W2", "W2_GERALT"}:
        return "GERALT_W2"
    return "INVENTORY"


def _inventory_preset_default_key_for_target(target):
    target = _normalize_inventory_preset_selection_target(target)
    if target == "GERALT_W3":
        return "geralt_w3"
    if target == "GERALT_W2":
        return "geralt_w2"
    return ""


def _inventory_preset_target_label(target):
    target = _normalize_inventory_preset_selection_target(target)
    if target == "GERALT_W3":
        return "Geralt Default"
    if target == "GERALT_W2":
        return "Geralt W2 Default"
    return "Inventory Preset"


def _inventory_preset_source_game_for_target(context=None, target="INVENTORY"):
    target = _normalize_inventory_preset_selection_target(target)
    if target == "GERALT_W2":
        return "w2"
    if target == "GERALT_W3":
        return "w3"
    temp_data = _get_temp_equipment_data(context) if context is not None else None
    return _normalize_source_game(getattr(temp_data, "equipment_source_game", "") if temp_data is not None else "w3")


def _get_inventory_preset_selection(context=None, target="INVENTORY"):
    target = _normalize_inventory_preset_selection_target(target)
    if target == "INVENTORY":
        temp_data = _get_temp_equipment_data(context) if context is not None else None
        preset_id = str(getattr(temp_data, "inventory_preset_id", "") or "") if temp_data is not None else ""
        if not preset_id:
            return ""
        source_game = _inventory_preset_source_game_for_target(context, target)
        return preset_id if _get_inventory_preset(preset_id, source_game=source_game) is not None else ""

    default_key = _inventory_preset_default_key_for_target(target)
    if not default_key:
        return ""
    preset_id = str(_load_inventory_preset_defaults().get("defaults", {}).get(default_key, "") or "").strip()
    source_game = _inventory_preset_source_game_for_target(context, target)
    return preset_id if _get_inventory_preset(preset_id, source_game=source_game) is not None else ""


def _set_inventory_preset_selection(context, preset_id, target="INVENTORY"):
    target = _normalize_inventory_preset_selection_target(target)
    preset_id = str(preset_id or "").strip()
    if preset_id == "__none__":
        preset_id = ""
    source_game = _inventory_preset_source_game_for_target(context, target)
    if preset_id and _get_inventory_preset(preset_id, source_game=source_game) is None:
        return False

    if target == "INVENTORY":
        temp_data = _get_temp_equipment_data(context)
        if temp_data is None:
            return False
        temp_data.inventory_preset_id = preset_id
    else:
        default_key = _inventory_preset_default_key_for_target(target)
        if not default_key:
            return False
        store = _load_inventory_preset_defaults()
        defaults = dict(store.get("defaults", {}) or {})
        if preset_id:
            defaults[default_key] = preset_id
        else:
            defaults.pop(default_key, None)
        store["defaults"] = defaults
        _save_inventory_preset_defaults(store)

    try:
        if context and context.area:
            context.area.tag_redraw()
    except Exception:
        pass
    return True


def _get_geralt_default_inventory_preset_id(context=None, source_game="w3"):
    target = "GERALT_W2" if _normalize_source_game(source_game) == "w2" else "GERALT_W3"
    return _get_inventory_preset_selection(context, target=target)


def _normal_inventory_item_name(item_name, equip_template=""):
    item_name = str(item_name or "").strip()
    if item_name and item_name.lower() not in {"none", "null"}:
        return item_name
    equip_template = str(equip_template or "").strip()
    if equip_template and equip_template.lower() not in {"none", "null"}:
        return equip_template
    return ""


def _make_inventory_preset_entry(category, item_name, equip_template="", is_mount=True,
                                 quantity=1, quantity_min=0, quantity_max=0,
                                 probability=0.0, is_lootable=False, source_game="w3"):
    category = str(category or "").strip()
    item_name = _normal_inventory_item_name(item_name, equip_template)
    equip_template = str(equip_template or "").strip()
    entry = {
        "category": category,
        "item": item_name,
        "quantity": int(quantity or 1),
        "isMount": bool(is_mount),
        "isLootable": bool(is_lootable),
        "source_game": _normalize_source_game(source_game),
    }
    if quantity_min:
        entry["quantityMin"] = int(quantity_min)
    if quantity_max:
        entry["quantityMax"] = int(quantity_max)
    if probability:
        entry["probability"] = float(probability)
    if item_name:
        entry["initializer"] = {"itemName": item_name}
    if equip_template:
        entry["equip_template"] = equip_template
    return entry


def _inventory_preset_entries_from_equipment_slots(rig_settings):
    entries = []
    if rig_settings is None:
        return entries
    for slot in getattr(rig_settings, "equipment_slots", []):
        category = str(getattr(slot, "category", "") or "").strip()
        item_name = str(getattr(slot, "item_name", "") or "").strip()
        equip_template = str(getattr(slot, "equip_template", "") or "").strip()
        if not category and not item_name and not equip_template:
            continue
        if not _normal_inventory_item_name(item_name, equip_template):
            continue
        entries.append(_make_inventory_preset_entry(
            category,
            item_name,
            equip_template=equip_template,
            is_mount=True,
            source_game=getattr(slot, "source_game", "") or getattr(rig_settings, "source_game", "") or "w3",
        ))
    return entries


def _inventory_preset_entries_from_inventory_rows(temp_data):
    entries = []
    if temp_data is None:
        return entries
    for row in getattr(temp_data, "inventory_entries", []):
        item_name = str(getattr(row, "item_name", "") or getattr(row, "resolved_item_name", "") or "").strip()
        equip_template = str(getattr(row, "equip_template", "") or "").strip()
        if not _normal_inventory_item_name(item_name, equip_template):
            continue
        entries.append(_make_inventory_preset_entry(
            getattr(row, "category", ""),
            item_name,
            equip_template=equip_template,
            is_mount=bool(getattr(row, "is_mount", False)),
            quantity=getattr(row, "quantity", 1) or 1,
            quantity_min=getattr(row, "quantity_min", 0) or 0,
            quantity_max=getattr(row, "quantity_max", 0) or 0,
            probability=getattr(row, "probability", 0.0) or 0.0,
            is_lootable=bool(getattr(row, "is_lootable", False)),
            source_game=getattr(row, "source_game", "") or getattr(temp_data, "equipment_source_game", "") or "w3",
        ))
    return entries


def _make_inventory_preset(name, entries, source="equipment", source_game="w3"):
    source_game = _normalize_source_game(source_game)
    normalized_entries = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        entry = dict(entry)
        entry["source_game"] = _normalize_source_game(entry.get("source_game", source_game))
        normalized_entries.append(entry)
    return {
        "id": f"p_{uuid.uuid4().hex}",
        "name": str(name or "Inventory Preset").strip() or "Inventory Preset",
        "source": str(source or "equipment"),
        "source_game": source_game,
        "created": int(time.time()),
        "entries": normalized_entries,
    }


def _save_new_inventory_preset(name, entries, source="equipment", source_game="w3"):
    preset = _make_inventory_preset(name, entries, source=source, source_game=source_game)
    store = _load_inventory_preset_store()
    presets = list(store.get("presets", []) or [])
    presets.append(preset)
    store["presets"] = presets
    _save_inventory_preset_store(store)
    return preset


def _delete_user_inventory_preset(preset_id, context=None):
    preset_id = str(preset_id or "").strip()
    if not preset_id:
        return None

    store = _load_inventory_preset_store()
    presets = list(store.get("presets", []) or [])
    kept = []
    removed = None
    for preset in presets:
        if isinstance(preset, dict) and str(preset.get("id", "") or "") == preset_id:
            removed = preset
            continue
        kept.append(preset)
    if removed is None:
        return None

    store["presets"] = kept
    _save_inventory_preset_store(store)

    defaults_store = _load_inventory_preset_defaults()
    defaults = dict(defaults_store.get("defaults", {}) or {})
    filtered_defaults = {
        key: value for key, value in defaults.items()
        if str(value or "") != preset_id
    }
    if filtered_defaults != defaults:
        defaults_store["defaults"] = filtered_defaults
        _save_inventory_preset_defaults(defaults_store)

    temp_data = _get_temp_equipment_data(context) if context is not None else None
    if temp_data is not None:
        if str(getattr(temp_data, "inventory_preset_id", "") or "") == preset_id:
            temp_data.inventory_preset_id = ""
        try:
            temp_data.preset_picker_filter_token = ""
            temp_data.preset_picker_rows.clear()
        except Exception:
            pass

    try:
        if context and context.area:
            context.area.tag_redraw()
    except Exception:
        pass
    return removed


def _set_entity_inventory_definitions_for_preset(rig_settings, preset):
    if rig_settings is None or not preset:
        return None
    _entity, entity_data = import_entity.get_rig_entity_state(rig_settings)
    if entity_data is None:
        return None
    entity_data = json.loads(json.dumps(entity_data, sort_keys=False))
    entity_data["inventoryDefinitions"] = [{
        "wasIncluded": False,
        "entries": json.loads(json.dumps(preset.get("entries", []) or [], sort_keys=False)),
    }]
    entity_data["witcher_inventory_preset_id"] = str(preset.get("id", "") or "")
    entity_data["witcher_inventory_preset_name"] = str(preset.get("name", "") or "")
    entity_data["witcher_inventory_preset_source_game"] = _normalize_source_game(preset.get("source_game", "w3"))
    if import_entity.cache_rig_entity_state_from_data(rig_settings, entity_data, update_json=True) is None:
        return None
    try:
        rig_settings.inventory_mount_overrides_json = "{}"
    except Exception:
        pass
    return entity_data


def _remove_inventory_slots_for_preset_apply(rig_settings):
    if rig_settings is None:
        return 0
    removed = 0
    slots = rig_settings.equipment_slots
    for slot_index in reversed(range(len(slots))):
        slot = slots[slot_index]
        if not getattr(slot, "is_inventory", False):
            continue
        try:
            unload_equipment_item(slot)
        except Exception:
            pass
        try:
            slots.remove(slot_index)
            removed += 1
        except Exception:
            pass
    return removed


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
    return equipment_catalog.source_game_from_xml_path(path_value)


def _norm_root_path(path):
    if not path:
        return ""
    try:
        return os.path.normcase(os.path.normpath(str(path)))
    except Exception:
        return str(path).lower()


def _get_w2_repo_roots():
    return equipment_catalog.get_w2_repo_roots()


def _is_w2_search(search_roots):
    return equipment_catalog.is_w2_search(search_roots)


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
    return equipment_catalog.lookup_item_attributes(item_name, source_game)


def ensure_equipment_catalog_ready(source_game="w3", search_roots=None, context=None, require_browser_icon_fields=False):
    return equipment_catalog.ensure_equipment_catalog_ready(
        source_game,
        search_roots=search_roots,
        context=context,
        require_browser_icon_fields=require_browser_icon_fields,
    )


def get_item_attributes_by_identifier(identifier, source_game="w3", strict: bool = False):
    return equipment_catalog.get_item_attributes_by_identifier(
        identifier,
        source_game=source_game,
        strict=strict,
    )


def get_item_icon_path(identifier, source_game="w3", strict: bool = False):
    return equipment_catalog.get_item_icon_path(identifier, source_game=source_game, strict=strict)


def _get_active_equipment_source_path(context):
    armature, rig_settings = _get_safe_context_armature_and_rig_settings(context)
    if armature is not None:
        try:
            source_path = str(armature.get("_w3_entity_source_path", "") or "").strip()
            if source_path:
                return source_path
        except Exception:
            pass
    if rig_settings is not None:
        try:
            repo_path = str(getattr(rig_settings, "repo_path", "") or "").strip()
            if repo_path and os.path.isabs(repo_path):
                return repo_path
        except Exception:
            pass
    return ""


@contextmanager
def _equipment_icon_repo_context(context):
    source_path = _get_active_equipment_source_path(context)
    if not source_path:
        yield
        return
    try:
        from ..CR2W.common_blender import redkit_repo_context
    except Exception:
        yield
        return
    with redkit_repo_context(source_path=source_path):
        yield


def _get_equipment_icon_loadmods(context):
    try:
        return bool(context.scene.witcher_file_browser.loadmods)
    except Exception:
        return False


def _iter_equipment_icon_identifiers(item_name, attrs=None, fallback_template=""):
    attrs = attrs if isinstance(attrs, dict) else {}
    seen = set()
    for value in (
        item_name,
        attrs.get("equip_template", ""),
        attrs.get("hold_template", ""),
        attrs.get("template_name", ""),
        fallback_template,
    ):
        text = str(value or "").strip()
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        yield text


def _get_equipment_item_attrs_for_enum(entry, item_name, source_game="w3"):
    source_game = _normalize_source_game(source_game)
    item_name = str(item_name or "").strip()
    if not item_name or item_name == "None":
        return {}, ""

    category_items, item_attributes = _get_equipment_catalog(source_game)
    attrs = item_attributes.get(item_name, {})
    if isinstance(attrs, dict) and attrs:
        return attrs, str(attrs.get("template_name", "") or attrs.get("equip_template", "") or attrs.get("hold_template", ""))

    category = str(getattr(entry, "category", "") or "None")
    fallback_template = ""
    for name, _display, template_name in category_items.get(category, []):
        if str(name or "") == item_name:
            fallback_template = str(template_name or "")
            break

    if fallback_template:
        attrs = get_item_attributes_by_identifier(fallback_template, source_game=source_game, strict=True)
        if isinstance(attrs, dict) and attrs:
            return attrs, fallback_template

    attrs = get_item_attributes_by_identifier(item_name, source_game=source_game, strict=True)
    return (attrs if isinstance(attrs, dict) else {}), fallback_template


def _resolve_equipment_item_icon_path(item_name, attrs=None, source_game="w3", fallback_template=""):
    source_game = _normalize_source_game(source_game)
    attrs = attrs if isinstance(attrs, dict) else {}
    raw_icon_path = str(attrs.get("icon_path", "") or "").strip()
    if raw_icon_path:
        return raw_icon_path

    for identifier in _iter_equipment_icon_identifiers(item_name, attrs, fallback_template=fallback_template):
        raw_icon_path = str(get_item_icon_path(identifier, source_game=source_game, strict=True) or "").strip()
        if raw_icon_path:
            return raw_icon_path
    return ""


def _iter_equipment_icon_candidate_paths(asset_previews, context, raw_icon_path):
    seen = set()

    def _add(path_value):
        normalized = str(path_value or "").replace("/", "\\").strip("\\")
        if not normalized:
            return
        key = normalized.lower()
        if key in seen:
            return
        seen.add(key)
        yield normalized

    def _add_preview_lookup_paths(path_value):
        lookup_paths = asset_previews.iter_preview_lookup_paths(path_value)
        for lookup_path in lookup_paths:
            for normalized in _add(lookup_path):
                yield normalized

    for candidate in _add_preview_lookup_paths(raw_icon_path):
        yield candidate
    expanded = asset_previews.expand_scaleform_icon_candidates(context, raw_icon_path)
    for candidate in expanded:
        for normalized in _add_preview_lookup_paths(candidate):
            yield normalized


def _get_equipment_icon_cache_type(asset_previews, source_game="w3"):
    if _normalize_source_game(source_game) == "w2":
        return asset_previews.get_witcher2_bundle_cache_type()
    return "Bundle"


def _iter_equipment_icon_template_candidates(item_name, attrs=None, fallback_template=""):
    attrs = attrs if isinstance(attrs, dict) else {}
    seen = set()
    for value in (
        attrs.get("equip_template", ""),
        attrs.get("hold_template", ""),
        attrs.get("template_name", ""),
        fallback_template,
    ):
        text = _normalize_template_path(value)
        key = text.lower()
        if not text or key == "none" or key in seen:
            continue
        seen.add(key)
        yield text

    try:
        derived = _normalize_template_path(import_entity._derive_template_from_item(item_name))
    except Exception:
        derived = ""
    key = derived.lower()
    if derived and key != "none" and key not in seen:
        yield derived


def _iter_equipment_template_browser_paths(template_name):
    rel_template = _normalize_template_path(template_name)
    if not rel_template:
        return

    candidates = []
    if rel_template.lower().endswith(".w2ent"):
        candidates.append(rel_template)
    else:
        candidates.append(rel_template + ".w2ent")

    if "\\" not in rel_template:
        short_path = candidates[0]
        candidates.append(os.path.join("items", short_path).replace("/", "\\"))

    seen = set()
    for candidate in candidates:
        key = candidate.lower()
        if not candidate or key in seen:
            continue
        seen.add(key)
        yield candidate


def _resolve_equipment_item_entity_preview_path(
    context,
    asset_previews,
    source_game,
    item_name,
    attrs=None,
    fallback_template="",
    loadmods=False,
):
    cache_type = _get_equipment_icon_cache_type(asset_previews, source_game)
    armature, rig_settings = _get_safe_context_armature_and_rig_settings(context)
    seen_paths = set()

    for template_name in _iter_equipment_icon_template_candidates(item_name, attrs, fallback_template):
        try:
            repo_path, _resolved_abs_path = _resolve_equipment_paths_for_template(
                template_name,
                armature=armature,
                rig_settings=rig_settings,
            )
        except Exception:
            repo_path = ""

        path_candidates = []
        if repo_path:
            path_candidates.append(repo_path)
        path_candidates.extend(_iter_equipment_template_browser_paths(template_name))

        for item_path in path_candidates:
            normalized = str(item_path or "").replace("/", "\\").strip("\\")
            key = normalized.lower()
            if not normalized or key in seen_paths:
                continue
            seen_paths.add(key)

            icon_info = asset_previews.get_browser_item_icon_info(
                context,
                cache_type,
                normalized,
                loadmods=loadmods,
            )
            if icon_info.get("is_dummy"):
                continue
            preview_path = str(icon_info.get("preview_path", "") or "")
            if preview_path:
                return preview_path
    return ""


def _get_equipment_item_icon_cache_key(context, item_name, attrs=None, source_game="w3", fallback_template=""):
    raw_icon_path = _resolve_equipment_item_icon_path(
        item_name,
        attrs,
        source_game=source_game,
        fallback_template=fallback_template,
    )
    source_game = _normalize_source_game(source_game)
    loadmods = _get_equipment_icon_loadmods(context)
    source_roots = _get_active_equipment_source_roots(context)
    source_path = _get_active_equipment_source_path(context)
    template_keys = tuple(
        template.lower()
        for template in _iter_equipment_icon_template_candidates(item_name, attrs, fallback_template)
    )
    if not raw_icon_path and not template_keys:
        return None, raw_icon_path, template_keys, loadmods
    return (
        source_game,
        int(bool(loadmods)),
        raw_icon_path.replace("/", "\\").strip("\\").lower(),
        template_keys,
        _norm_root_path(source_path),
        tuple(_norm_root_path(root) for root in source_roots),
    ), raw_icon_path, template_keys, loadmods


def _resolve_equipment_item_preview_path(context, item_name, attrs=None, source_game="w3", fallback_template=""):
    """Run the (expensive) icon resolution and return the resolved preview image
    path on disk, or "" if none. This is the slow path run in the background; its
    result is persisted so it only happens once per item, ever."""
    raw_icon_path = _resolve_equipment_item_icon_path(
        item_name,
        attrs,
        source_game=source_game,
        fallback_template=fallback_template,
    )
    preview_path = ""
    try:
        from . import asset_previews

        cache_type = _get_equipment_icon_cache_type(asset_previews, source_game)
        loadmods = _get_equipment_icon_loadmods(context)
        with _equipment_icon_repo_context(context):
            if raw_icon_path:
                for candidate_path in _iter_equipment_icon_candidate_paths(asset_previews, context, raw_icon_path):
                    icon_info = asset_previews.get_browser_item_icon_info(
                        context,
                        cache_type,
                        candidate_path,
                        loadmods=loadmods,
                    )
                    if icon_info.get("is_dummy"):
                        continue
                    preview_path = str(icon_info.get("preview_path", "") or "")
                    if preview_path:
                        break
            if not preview_path:
                preview_path = _resolve_equipment_item_entity_preview_path(
                    context,
                    asset_previews,
                    source_game,
                    item_name,
                    attrs,
                    fallback_template=fallback_template,
                    loadmods=loadmods,
                )
    except Exception:
        log.debug("Failed to resolve equipment item icon for %s", item_name, exc_info=True)
    return preview_path


def _tag_equipment_item_icon_redraw():
    try:
        wm = bpy.context.window_manager
        for window in getattr(wm, "windows", []) or []:
            screen = getattr(window, "screen", None)
            if not screen:
                continue
            for area in getattr(screen, "areas", []) or []:
                try:
                    area.tag_redraw()
                except Exception:
                    pass
    except Exception:
        pass


def _equipment_item_icon_timer():
    global _EQUIPMENT_ITEM_ICON_TIMER_RUNNING

    if not _EQUIPMENT_ITEM_ICON_REQUESTS:
        _EQUIPMENT_ITEM_ICON_TIMER_RUNNING = False
        _save_equipment_icon_path_disk()
        return None

    context = bpy.context
    disk = _load_equipment_icon_path_disk()
    resolved_any = False
    for _i in range(min(_EQUIPMENT_ITEM_ICON_BATCH_SIZE, len(_EQUIPMENT_ITEM_ICON_REQUESTS))):
        request = _EQUIPMENT_ITEM_ICON_REQUESTS.pop(0)
        cache_key = request.get("cache_key")
        stable_key = request.get("stable_key")
        try:
            src_path = _resolve_equipment_item_preview_path(
                context,
                request.get("item_name", ""),
                request.get("attrs", {}),
                source_game=request.get("source_game", "w3"),
                fallback_template=request.get("fallback_template", ""),
            )
            # Persist a copy + record the path so this never has to resolve again.
            persistent = _persist_equipment_icon_image(src_path, stable_key) if src_path else ""
            disk[stable_key] = persistent
            _mark_equipment_icon_disk_dirty()
            icon_id = _equipment_icon_id_from_path(stable_key, persistent) if persistent else 0
            if cache_key is not None:
                _EQUIPMENT_ITEM_ICON_ID_CACHE[cache_key] = int(icon_id or 0)
            resolved_any = True
        except Exception:
            if cache_key is not None:
                _EQUIPMENT_ITEM_ICON_ID_CACHE[cache_key] = 0
        finally:
            if cache_key is not None:
                _EQUIPMENT_ITEM_ICON_PENDING_KEYS.discard(cache_key)

    if resolved_any:
        _tag_equipment_item_icon_redraw()

    if _EQUIPMENT_ITEM_ICON_REQUESTS:
        return 0.03

    _EQUIPMENT_ITEM_ICON_TIMER_RUNNING = False
    _save_equipment_icon_path_disk()
    return None


def _ensure_equipment_item_icon_timer():
    global _EQUIPMENT_ITEM_ICON_TIMER_RUNNING
    if _EQUIPMENT_ITEM_ICON_TIMER_RUNNING:
        return
    try:
        bpy.app.timers.register(_equipment_item_icon_timer, first_interval=0.03)
        _EQUIPMENT_ITEM_ICON_TIMER_RUNNING = True
    except Exception:
        _EQUIPMENT_ITEM_ICON_TIMER_RUNNING = False


def _get_cached_or_queue_equipment_item_icon_id(context, item_name, attrs=None, source_game="w3", fallback_template=""):
    cache_key, _raw_icon_path, _template_keys, _loadmods = _get_equipment_item_icon_cache_key(
        context,
        item_name,
        attrs,
        source_game=source_game,
        fallback_template=fallback_template,
    )
    if cache_key is None:
        return 0

    # Fast path: already loaded this session.
    cached = _EQUIPMENT_ITEM_ICON_ID_CACHE.get(cache_key)
    if cached is not None:
        return int(cached or 0)

    # Cross-session path: we resolved this icon in a previous run. Just lazily
    # (re)load the persisted image — cheap, no resolution, no UI lag.
    stable_key = _equipment_icon_stable_key(cache_key)
    disk = _load_equipment_icon_path_disk()
    if stable_key in disk:
        path = disk[stable_key]
        if not path:
            # Known to be unresolvable — don't keep retrying.
            _EQUIPMENT_ITEM_ICON_ID_CACHE[cache_key] = 0
            return 0
        icon_id = _equipment_icon_id_from_path(stable_key, path)
        if icon_id:
            _EQUIPMENT_ITEM_ICON_ID_CACHE[cache_key] = icon_id
            return icon_id
        # Persisted file disappeared (e.g. cache dir wiped) — fall through and
        # re-resolve it in the background.

    if cache_key not in _EQUIPMENT_ITEM_ICON_PENDING_KEYS:
        _EQUIPMENT_ITEM_ICON_PENDING_KEYS.add(cache_key)
        _EQUIPMENT_ITEM_ICON_REQUESTS.append({
            "cache_key": cache_key,
            "stable_key": stable_key,
            "item_name": str(item_name or ""),
            "attrs": dict(attrs) if isinstance(attrs, dict) else {},
            "source_game": _normalize_source_game(source_game),
            "fallback_template": str(fallback_template or ""),
        })
        _ensure_equipment_item_icon_timer()
    return 0


def _get_equipment_entry_item_icon_id(context, entry):
    if entry is None:
        return 0
    source_game = getattr(entry, "source_game", "") or _get_temp_source_game(context)
    item_name = str(getattr(entry, "defaultItemName", "") or "").strip()
    attrs, fallback_template = _get_equipment_item_attrs_for_enum(entry, item_name, source_game)
    if not attrs:
        attrs = get_item_attributes_by_identifier(
            getattr(entry, "equip_template", ""),
            source_game=source_game,
            strict=True,
        )
    return _get_cached_or_queue_equipment_item_icon_id(
        context,
        item_name,
        attrs,
        source_game=source_game,
        fallback_template=fallback_template or getattr(entry, "equip_template", ""),
    )


def _apply_catalog_attributes_to_slot(slot, source_game=None):
    if slot is None:
        return False

    source_game = _normalize_source_game(source_game or getattr(slot, "source_game", "") or "w3")
    item_name = str(getattr(slot, "item_name", "") or "").strip()
    equip_template = str(getattr(slot, "equip_template", "") or "").strip()
    attrs = _lookup_item_attributes(item_name, source_game) if item_name else {}
    if not attrs and equip_template:
        attrs = _lookup_item_attributes(equip_template, source_game)
    if not attrs and item_name:
        try:
            derived_template = import_entity._derive_template_from_item(item_name)
        except Exception:
            derived_template = ""
        if derived_template:
            attrs = _lookup_item_attributes(derived_template, source_game)
    if not attrs:
        return False

    changed = False

    def _assign(prop_name, value):
        nonlocal changed
        if getattr(slot, prop_name, None) != value:
            setattr(slot, prop_name, value)
            changed = True

    _assign("equip_slot", attrs.get("equip_slot", getattr(slot, "equip_slot", "")))
    _assign("hold_slot", attrs.get("hold_slot", getattr(slot, "hold_slot", "")))
    _assign("weapon", bool(attrs.get("weapon", getattr(slot, "weapon", False))))
    _assign("attachment_type", attrs.get("attachment_type", getattr(slot, "attachment_type", "")))
    variants = attrs.get("variants", [])
    bound_items = attrs.get("bound_items", [])
    tags = attrs.get("tags", [])
    try:
        variants_json = json.dumps(variants)
    except Exception:
        variants_json = getattr(slot, "variants_json", "")
    _assign("variants_json", variants_json)
    try:
        bound_items_json = json.dumps(bound_items)
    except Exception:
        bound_items_json = getattr(slot, "bound_items_json", "")
    _assign("bound_items_json", bound_items_json)
    _assign("variants_summary", _format_variant_summary(variants))
    _assign("bound_items_summary", _format_bound_items_summary(bound_items))
    if isinstance(tags, str):
        tags = _split_tags(tags)
    try:
        tags_summary = ", ".join([str(tag) for tag in tags if tag])
    except Exception:
        tags_summary = getattr(slot, "tags_summary", "")
    _assign("tags_summary", tags_summary)
    return changed


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
    if prop_name in {"witcher_equip_guid", "witcher_template_guid"}:
        for extra_prop in (
            "witcher_bound_parent_guid",
            "witcher_bound_item_name",
            "witcher_redcloth_reuse_key",
            "witcher_redcloth_resource",
            "witcher_redcloth_material",
            "witcher_redcloth_mesh_name",
            "witcher_redcloth_mesh_names",
        ):
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
            obj_name = obj.name  # capture before removal
            bpy.data.objects.remove(obj, do_unlink=True)
            removed += 1
        except Exception as e:
            log.warning("Failed to remove GUID-tagged object '%s': %s", obj_name, e)
            _clear_guid_metadata(obj, guid, prop_name)
        finally:
            pending.discard(obj)
    return removed


# ---------------------------------------------------------------------------
# GUID validation & repair
# ---------------------------------------------------------------------------

def _guid_has_objects(guid, prop_name="witcher_equip_guid"):
    """Fast check: does at least one scene object carry this GUID?"""
    if not guid:
        return False
    for obj in bpy.data.objects:
        if obj.get(prop_name) == guid and not _is_internal_inventory_group_object(obj):
            return True
    return False


def _get_slot_item_appearance_names(slot):
    try:
        raw_names = json.loads(getattr(slot, "item_appearances_json", "") or "[]")
    except Exception:
        raw_names = []
    result = []
    for name in raw_names:
        name = str(name or "").strip()
        if name and name != "__default__" and name not in result:
            result.append(name)
    return result


def _slot_supports_item_appearance_ui(slot):
    return len(_get_slot_item_appearance_names(slot)) > 1


def _slot_supports_item_coloring_ui(slot, colorable_meshes=None):
    if slot is None:
        return False
    if not getattr(slot, "is_loaded", False):
        return False
    if not getattr(slot, "equip_guid", ""):
        return False
    if not getattr(slot, "item_coloring_json", ""):
        return False
    if colorable_meshes is not None:
        return bool(colorable_meshes)
    return True


def _slot_supports_item_details_ui(slot, colorable_meshes=None):
    return bool(
        _slot_supports_item_appearance_ui(slot)
        or _slot_supports_item_coloring_ui(slot, colorable_meshes=colorable_meshes)
    )


def _slot_supports_rune_ui(slot):
    return bool(
        slot
        and getattr(slot, "is_loaded", False)
        and getattr(slot, "category", "") in {"steelsword", "silversword"}
    )


def _get_slot_colorable_meshes(slot):
    if not _slot_supports_item_coloring_ui(slot):
        return []
    return [
        obj for obj in find_objects_by_guid(getattr(slot, "equip_guid", ""), "witcher_equip_guid")
        if getattr(obj, "type", "") == 'MESH'
        and any(obj.get(k) is not None for k in ("colorShift1_hue", "colorShift2_hue"))
    ]


def _coerce_slot_inline_ui_state(slot):
    repaired = False
    if slot is None:
        return repaired
    if not _slot_supports_item_appearance_ui(slot) and getattr(slot, "show_item_appearance_ui", False):
        slot.show_item_appearance_ui = False
        repaired = True
    if not _slot_supports_item_details_ui(slot) and getattr(slot, "show_item_coloring_ui", False):
        slot.show_item_coloring_ui = False
        repaired = True
    return repaired


def _get_slot_hold_toggle_state(slot, slot_policy):
    if slot.is_loaded and slot_policy["hold_valid"]:
        is_in_hold = bool(slot.is_in_hold_slot)
        toggle_icon = 'ARMATURE_DATA' if is_in_hold else 'FILE_3D'
        if slot_policy["policy"] == "hold_only_on_rig":
            toggle_text = "Put Away" if is_in_hold else "Hold"
        else:
            toggle_text = "Mount" if is_in_hold else "Hold"
        return True, toggle_text, toggle_icon
    if not slot.is_loaded and slot_policy["policy"] == "hold_only_on_rig" and slot_policy["hold_valid"]:
        return True, "Hold", 'ARMATURE_DATA'
    return False, "", 'ARMATURE_DATA'


def _draw_slot_details_ui(layout, slot, *, has_appearance_ui, has_coloring_ui, colorable_meshes):
    if not getattr(slot, "show_item_coloring_ui", False):
        return
    if has_appearance_ui:
        app_row = layout.row(align=True)
        app_row.prop(slot, "item_appearance_name", text="Appearance")
    if has_coloring_ui:
        try:
            from ..ui.ui_entity import _show_coloring_object_props
            if colorable_meshes:
                col_box = layout.box()
                col_box.label(text="Coloring", icon='MOD_HUE_SATURATION')
                _show_coloring_object_props(col_box, colorable_meshes)
        except Exception as e:
            log.warning("Failed to show coloring entries ui: %s", e)


def validate_slot_loaded_state(slot, prop_name="witcher_equip_guid"):
    """Reconcile *slot.is_loaded* with what actually exists in the scene.

    Returns True if the slot was already consistent, False if it was fixed.
    """
    repaired = False
    if not getattr(slot, "is_loaded", False):
        restored_loaded = False
        guid = getattr(slot, "equip_guid", "")
        if guid and _guid_has_objects(guid, prop_name):
            slot.is_loaded = True
            repaired = True
            restored_loaded = True
        elif guid:
            slot.equip_guid = ""
            repaired = True
        if not restored_loaded and getattr(slot, "is_in_hold_slot", False):
            slot.is_in_hold_slot = False
            repaired = True
        if _coerce_slot_inline_ui_state(slot):
            repaired = True
        return not repaired

    guid = getattr(slot, "equip_guid", "")
    if not guid or not _guid_has_objects(guid, prop_name):
        # Slot claims to be loaded but nothing in the scene matches its GUID.
        slot.is_loaded = False
        slot.equip_guid = ""
        slot.is_in_hold_slot = False
        slot.show_item_appearance_ui = False
        slot.show_item_coloring_ui = False
        log.debug(
            "Repaired stale equipment slot '%s/%s': GUID objects missing from scene.",
            getattr(slot, "category", "?"),
            getattr(slot, "item_name", "?"),
        )
        repaired = True
    if _coerce_slot_inline_ui_state(slot):
        repaired = True
    return not repaired


def validate_all_equipment_slots(rig_settings):
    """Walk every equipment slot and fix stale is_loaded flags.

    Returns the number of slots that were repaired.
    """
    repaired = 0
    for slot in getattr(rig_settings, "equipment_slots", []):
        if not validate_slot_loaded_state(slot):
            repaired += 1
    return repaired


def validate_template_slot_loaded_state(slot):
    repaired = False
    if not getattr(slot, "is_loaded", False):
        guid = getattr(slot, "template_guid", "")
        if guid and _guid_has_objects(guid, "witcher_template_guid"):
            slot.is_loaded = True
            repaired = True
        elif guid:
            slot.template_guid = ""
            repaired = True
        if not getattr(slot, "is_loaded", False) and getattr(slot, "is_hidden", False):
            slot.is_hidden = False
            repaired = True
        return not repaired

    guid = getattr(slot, "template_guid", "")
    if not guid or not _guid_has_objects(guid, "witcher_template_guid"):
        slot.is_loaded = False
        slot.template_guid = ""
        slot.is_hidden = False
        repaired = True
    return not repaired


def validate_all_template_slots(rig_settings):
    repaired = 0
    for slot in getattr(rig_settings, "template_slots", []):
        if not validate_template_slot_loaded_state(slot):
            repaired += 1
    return repaired


def _iter_saved_equipment_armatures():
    seen_data = set()
    for obj in bpy.data.objects:
        if obj is None or getattr(obj, "type", "") != 'ARMATURE':
            continue
        if getattr(getattr(obj, "parent", None), "type", "") == 'ARMATURE':
            continue
        data = getattr(obj, "data", None)
        rig_settings = getattr(data, "witcherui_RigSettings", None) if data is not None else None
        if rig_settings is None:
            continue
        try:
            has_equipment = bool(len(getattr(rig_settings, "equipment_slots", [])))
        except Exception:
            has_equipment = False
        try:
            has_entity_slots = bool(len(getattr(rig_settings, "entity_slots", [])))
        except Exception:
            has_entity_slots = False
        if not has_equipment and not has_entity_slots:
            continue
        try:
            key = data.as_pointer()
        except Exception:
            key = id(data)
        if key in seen_data:
            continue
        seen_data.add(key)
        yield obj, rig_settings



def repair_saved_equipment_state(armature, rig_settings=None):
    if armature is None or getattr(armature, "type", "") != 'ARMATURE':
        return 0, 0
    if rig_settings is None:
        rig_settings = getattr(getattr(armature, "data", None), "witcherui_RigSettings", None)
    if rig_settings is None:
        return 0, 0

    refresh_count = 0
    repaired = 0
    try:
        refresh_count = refresh_slot_constraints(armature)
    except Exception:
        log.warning("Failed to refresh slot constraints for '%s' during saved-state repair", getattr(armature, "name", "?"), exc_info=True)

    repaired += validate_all_equipment_slots(rig_settings)
    repaired += validate_all_template_slots(rig_settings)

    source_game = _infer_source_game_from_rig_settings(rig_settings, armature)
    for slot in getattr(rig_settings, "equipment_slots", []):
        try:
            if _apply_catalog_attributes_to_slot(slot, source_game):
                repaired += 1
        except Exception:
            pass

    return refresh_count, repaired


def _deferred_repair_saved_equipment_state():
    try:
        total_refreshed = 0
        total_repaired = 0
        for armature, rig_settings in _iter_saved_equipment_armatures():
            refreshed, repaired = repair_saved_equipment_state(armature, rig_settings)
            total_refreshed += refreshed
            total_repaired += repaired
        if total_refreshed or total_repaired:
            log.info(
                "Restored saved equipment state: refreshed %d slot constraint(s), repaired %d equipment state(s)",
                total_refreshed,
                total_repaired,
            )
    except Exception:
        log.warning("Failed to restore saved equipment state after file load", exc_info=True)
    return None


def _schedule_deferred_equipment_repair():
    try:
        if hasattr(bpy.app.timers, "is_registered") and bpy.app.timers.is_registered(_deferred_repair_saved_equipment_state):
            return
    except Exception:
        pass
    try:
        bpy.app.timers.register(_deferred_repair_saved_equipment_state, first_interval=0.0)
    except Exception:
        _deferred_repair_saved_equipment_state()


@persistent
def _repair_equipment_state_on_load(_filepath=""):
    _schedule_deferred_equipment_repair()


def _register_equipment_load_handler():
    if _repair_equipment_state_on_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_repair_equipment_state_on_load)


def _unregister_equipment_load_handler():
    if _repair_equipment_state_on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_repair_equipment_state_on_load)


# Throttled validation — avoid scanning every UI redraw frame.
_last_equipment_validation_time = 0.0
_EQUIPMENT_VALIDATION_INTERVAL = 2.0  # seconds


def _maybe_validate_equipment_slots(rig_settings):
    """Run validation at most once every few seconds (called from UI draw)."""
    import time
    global _last_equipment_validation_time
    now = time.monotonic()
    if now - _last_equipment_validation_time < _EQUIPMENT_VALIDATION_INTERVAL:
        return 0
    _last_equipment_validation_time = now
    return validate_all_equipment_slots(rig_settings)


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


def _capture_selection_state(context):
    """Capture the current active object and selection, tolerating partial contexts."""
    try:
        view_layer = getattr(context, "view_layer", None)
        saved_active = view_layer.objects.active if view_layer and getattr(view_layer, "objects", None) else None
    except Exception:
        saved_active = None
    try:
        saved_selection = [obj for obj in getattr(context, "selected_objects", [])]
    except Exception:
        saved_selection = []
    return saved_active, saved_selection


@contextmanager
def _preserve_selection(context=None):
    selection_context = context if context is not None else bpy.context
    saved_active, saved_selection = _capture_selection_state(selection_context)
    try:
        yield
    finally:
        _safe_restore_selection(saved_active, saved_selection)

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


def _get_slot_requested_mount_mode(slot, slot_policy=None):
    if slot is None:
        return "equip"
    if getattr(slot, "is_loaded", False):
        return "hold" if getattr(slot, "is_in_hold_slot", False) else "equip"
    if slot_policy and slot_policy.get("policy") == "hold_only_on_rig" and slot_policy.get("hold_valid"):
        return "hold"
    return "equip"


def _can_load_slot_for_mount_mode(slot_policy, mount_mode):
    mount_mode = str(mount_mode or "").strip().lower()
    if mount_mode == "hold":
        return bool(slot_policy.get("hold_valid"))
    return slot_policy.get("policy") == "equipable_on_rig"


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

    template = get_effective_equip_template(slot)
    if template and str(template).lower().strip().endswith('.w2ent'):
        return True

    if str(getattr(slot, "attachment_type", "")).strip():
        return True

    keywords = {
        "tail", "hair", "armor", "gloves", "pants", "boots",
        "torso", "legs", "arms", "trousers", "head", "mask",
        "cape", "cloak", "beard", "mustache", "helmet", "hood",
        "shirt", "shoes", "amulet", "accessory", "belt", "medal",
        "sword", "weapon", "scabbard", "crossbow",
    }
    for value in (
        getattr(slot, "category", ""),
        getattr(slot, "item_name", ""),
        template,
    ):
        normalized = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower())
        if any(kw in normalized for kw in keywords):
            return True
    return False


def _allow_w2_visual_without_resolved_mount(slot, attachment_profile=None):
    if slot is None:
        return False
    if _normalize_source_game(getattr(slot, "source_game", "")) != "w2":
        return False

    profile_kind = str(getattr(attachment_profile, "kind", "") or "").strip()
    if profile_kind == "inventory_wrapper":
        return False
    if profile_kind in {"owner_graph", "slot_visual", "slot_animated"}:
        return True

    return bool(
        getattr(slot, "weapon", False)
        or _safe_json_list(getattr(slot, "bound_items_json", ""))
        or _slot_matches_unmounted_visual_hint(slot)
    )


def _allow_unmounted_slotless_visual(slot, *, attachment_profile=None, item_entity=None):
    if slot is None:
        return False

    if attachment_profile is None and item_entity is not None:
        try:
            attachment_profile = import_entity.classify_equipment_attachment_profile(item_entity)
        except Exception:
            attachment_profile = None

    if _allow_w2_visual_without_resolved_mount(slot, attachment_profile=attachment_profile):
        return True

    if _slot_has_explicit_mount_target(slot):
        return False

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
    app_names = _get_slot_item_appearance_names(slot)
    selected_app = getattr(slot, 'item_appearance_name', '') or ''
    if not selected_app or selected_app == '__default__' or (app_names and selected_app not in app_names):
        selected_app = app_names[0] if app_names else ''

    result = []
    for entry in coloring_entries:
        entry_app = getattr(entry, 'appearance', '') or ''
        # Exact match required, no generic fallback unless entry_app is explicitly empty 
        if not entry_app or entry_app == selected_app:
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


def _resolve_item_entity_selected_appearance(item_entity, item_appearance_name=None):
    selected_item_appearance = None
    selected_item_appearance_name = ""
    if item_entity and getattr(item_entity, 'appearances', None):
        selected_app = item_entity.appearances[0]
        target_name = str(item_appearance_name or "").strip()
        if target_name and target_name != '__default__':
            for app in item_entity.appearances:
                if str(getattr(app, 'name', '') or '') == target_name:
                    selected_app = app
                    break
        selected_item_appearance = selected_app
        selected_item_appearance_name = str(getattr(selected_app, 'name', '') or '').strip()
    return selected_item_appearance, selected_item_appearance_name


def _get_item_entity_appearance_structure_signature(item_entity, item_appearance_name=None):
    if item_entity is None:
        return ""

    selected_app, _selected_name = _resolve_item_entity_selected_appearance(item_entity, item_appearance_name)
    if selected_app is not None:
        included_templates = getattr(selected_app, 'includedTemplates', None) or []
        if included_templates:
            return "included:" + import_entity._to_json_text(included_templates, default_text="[]", indent=None)

    static_meshes = getattr(item_entity, 'staticMeshes', None)
    if isinstance(static_meshes, dict) and static_meshes.get('chunks'):
        return "static:" + import_entity._to_json_text({'chunks': static_meshes.get('chunks')}, default_text="{}", indent=None)
    if hasattr(static_meshes, 'chunks') and getattr(static_meshes, 'chunks', None):
        return "static:" + import_entity._to_json_text({'chunks': static_meshes.chunks}, default_text="{}", indent=None)
    return "base"


def _get_loaded_slot_item_appearance_name(slot):
    if slot is None:
        return ""
    guid = str(getattr(slot, "equip_guid", "") or "").strip()
    if not guid:
        return ""
    for obj in find_objects_by_guid(guid, "witcher_equip_guid"):
        appearance_name = str(obj.get("witcher_item_appearance", "") or "").strip()
        if appearance_name:
            return appearance_name
    return ""


def try_update_loaded_equipment_appearance_in_place(context, armature, slot_index, rig_settings=None, prepared_context=None):
    """Recolor a loaded item in place when the selected appearance does not change import structure."""
    if armature is None:
        return False
    if rig_settings is None:
        rig_settings = getattr(getattr(armature, "data", None), "witcherui_RigSettings", None)
    if rig_settings is None:
        return False
    if slot_index < 0 or slot_index >= len(rig_settings.equipment_slots):
        return False

    slot = rig_settings.equipment_slots[slot_index]
    if not getattr(slot, "is_loaded", False) or not getattr(slot, "equip_guid", ""):
        return False

    prepared = _prepare_equipment_load_context(armature, rig_settings, prepared_context)
    source_roots = prepared.get("source_roots", [])
    effective_template = get_effective_equip_template(slot)
    final_item, export_path, _search_pattern = _resolve_bundle_item_by_template_cached(
        effective_template,
        search_roots=source_roots,
        prepared_context=prepared,
    )
    if not final_item or not export_path or not os.path.exists(export_path):
        return False

    item_entity = _get_cached_equipment_item_entity(export_path, prepared_context=prepared)
    if item_entity is None:
        return False

    current_loaded_appearance = _get_loaded_slot_item_appearance_name(slot)
    requested_appearance = getattr(slot, "item_appearance_name", "") or ""
    current_signature = _get_item_entity_appearance_structure_signature(item_entity, current_loaded_appearance)
    requested_signature = _get_item_entity_appearance_structure_signature(item_entity, requested_appearance)
    if not current_signature or current_signature != requested_signature:
        return False

    objects = find_objects_by_guid(slot.equip_guid, "witcher_equip_guid")
    if not objects:
        return False

    selected_app, resolved_appearance_name = _resolve_item_entity_selected_appearance(item_entity, requested_appearance)
    item_coloring_entries = getattr(item_entity, 'coloringEntries', None) or []
    try:
        import_entity._apply_coloring_entries_to_objects(
            objects,
            item_coloring_entries,
            resolved_appearance_name,
        )
    except Exception as e:
        log.warning("Failed to recolor '%s' in place for appearance '%s': %s", slot.item_name, requested_appearance, e)
        return False

    try:
        stamp_appearance = resolved_appearance_name if resolved_appearance_name != "__default__" else ""
        import_entity.stamp_import_origin(
            objects,
            origin="equipment_slot",
            entity_path=slot.resolved_repo_path,
            source_game=slot.source_game,
            item_category=slot.category,
            item_name=slot.item_name,
            equip_template=effective_template or slot.equip_template,
            item_appearance=stamp_appearance,
            owner_entity_path=getattr(rig_settings, "repo_path", ""),
        )
    except Exception as e:
        log.warning("Failed to restamp in-place appearance metadata for '%s': %s", slot.item_name, e)

    try:
        import_entity.initialize_imported_entity_armatures(
            objects,
            item_entity,
            filename=export_path,
            selected_appearance_name=resolved_appearance_name,
            update_json=True,
            context_role="auxiliary",
        )
    except Exception as e:
        log.warning("Failed to sync in-place appearance armature state for '%s': %s", slot.item_name, e)

    try:
        _update_slot_coloring_json(slot, item_entity)
    except Exception:
        pass

    try:
        bpy.context.view_layer.update()
    except Exception:
        pass
    return True


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
    if item_entity is not None:
        selected_item_appearance, selected_item_appearance_name = _resolve_item_entity_selected_appearance(
            item_entity,
            item_appearance_name,
        )
        if selected_item_appearance is not None and hasattr(selected_item_appearance, 'includedTemplates') and selected_item_appearance.includedTemplates:
            included_templates = selected_item_appearance.includedTemplates

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
            template_payload = template if (isinstance(template, dict) or hasattr(template, 'chunks')) else None
            _import_equipment_template(
                template_filename,
                template_data=template_payload,
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


# Witcher 2 catalog attachment_type values that skin an item directly onto the
# main body skeleton. Animated prefixed items keep their own rig and use a
# prefix-aware owner-armature bridge instead.
_W2_BODY_BOUND_ATTACHMENT_TYPES = {"skinning"}


def _is_w2_source_game(source_game):
    return str(source_game or "").strip().lower() == "w2"


def _w2_attachment_binds_to_body(source_game, attachment_type):
    """Whether a W2 item's mesh should be name-skinned to the body armature."""
    if not _is_w2_source_game(source_game):
        return False
    return str(attachment_type or "").strip().lower() in _W2_BODY_BOUND_ATTACHMENT_TYPES


def _w2_prefixed_animated_attachment_binds_to_owner(source_game, attachment_type, attachment_prefix):
    if not _is_w2_source_game(source_game):
        return False
    if str(attachment_type or "").strip().lower() != "animated":
        return False
    return bool(str(attachment_prefix or "").strip())


def _value_mentions_scabbard(*values):
    for value in values:
        if "scabbard" in str(value or "").replace("/", "\\").lower():
            return True
    return False


def _w2_bound_entity_binds_animated_to_owner(source_game, bound_name, template, export_path, attrs, attachment_profile):
    if not _is_w2_source_game(source_game):
        return False
    if _w2_prefixed_animated_attachment_binds_to_owner(
        source_game,
        (attrs or {}).get("attachment_type", ""),
        (attrs or {}).get("attachment_prefix", ""),
    ):
        return True

    category = str((attrs or {}).get("category", "") or "").strip().lower()
    if category in {"steel_scabbard", "silver_scabbard"}:
        return True

    if not _value_mentions_scabbard(bound_name, template, export_path):
        return False
    profile_kind = str(getattr(attachment_profile, "kind", "") or "").strip()
    root_type = str(getattr(attachment_profile, "root_component_type", "") or "").strip()
    return profile_kind == "slot_animated" and root_type == "CAnimatedComponent"


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

    reason = ""
    if not item_is_visual:
        policy = "nonvisual_on_rig"
        reason = "Item has no visual mesh or is an inventory wrapper."
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
        reason = f"No valid rig mount found for slots: {equip_slot_name} / {hold_slot_name}."

    return {
        "policy": policy,
        "reason": reason,
        "item_is_visual": item_is_visual,
        "equip_target": equip_target,
        "hold_target": hold_target,
        "equip_valid": bool(equip_target["is_valid"]),
        "hold_valid": bool(hold_target["is_valid"]),
        "attachment_profile": attachment_profile,
    }


def _slot_name_exists_on_rig(slot_name, armature, rig_settings):
    """True if slot_name corresponds to a real EntitySlot (empty or entry) on this rig."""
    if not slot_name or armature is None or rig_settings is None:
        return False
    entity_name = getattr(rig_settings, "entity_name", "") or ""
    if find_slot_empty(entity_name, slot_name, armature) is not None:
        return True
    return _find_slot_entry_for_mount_slot(slot_name, rig_settings) is not None


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
    # If hold_slot doesn't correspond to a real EntitySlot on this rig treat it as undefined
    if hold_slot_name and not _slot_name_exists_on_rig(hold_slot_name, armature, rig_settings):
        hold_slot_name = ""
    allow_unmounted_visual = _allow_unmounted_slotless_visual(
        slot,
        attachment_profile=attachment_profile,
        item_entity=item_entity,
    )
    # If hold_slot was cleared and equip_slot is also empty, the item is effectively slotless
    if not allow_unmounted_visual and not equip_slot_name and not hold_slot_name:
        allow_unmounted_visual = True
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


def _set_armature_pose_position(armature, pose_position="POSE"):
    if not armature or armature.type != 'ARMATURE':
        return
    try:
        armature.data.pose_position = pose_position
    except Exception:
        pass


def _constrain_bound_armature_to_target(bound_armature, target_armature):
    """Constrain bound armature bones to target armature bones (skinning behavior)."""
    if not bound_armature or not target_armature:
        return
    if bound_armature.type != 'ARMATURE' or target_armature.type != 'ARMATURE':
        return
    _snap_armature_to_target(bound_armature, target_armature)
    _align_bound_armature_pose(bound_armature, target_armature)
    with _preserve_selection():
        try:
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
    for arm in candidate_root_armatures:
        _set_armature_pose_position(arm, "POSE")
    root_armatures = [
        arm for arm in candidate_root_armatures
        if not _armature_has_external_binding(arm, object_set)
    ]
    if root_armatures:
        for arm in root_armatures:
            _set_armature_pose_position(arm, "POSE")
            saved_world = arm.matrix_world.copy()
            arm.parent = target_armature
            arm.parent_type = 'OBJECT'
            arm.matrix_world = saved_world
            _snap_armature_to_target(arm, target_armature)
            _constrain_bound_armature_to_target(arm, target_armature)
            _set_armature_pose_position(arm, "POSE")
        return True

    if candidate_root_armatures:
        return True

    _bind_objects_to_armature(object_set, target_armature)
    return bool(object_set)


def _attach_imported_armatures_to_owner(objects, target_armature):
    """Attach imported animated bound rigs to the owner armature."""
    if not target_armature:
        return False

    object_set = {obj for obj in (objects or []) if obj is not None}
    root_armatures = [
        obj for obj in object_set
        if obj.type == 'ARMATURE' and (obj.parent is None or obj.parent not in object_set)
    ]
    if not root_armatures:
        return False

    for arm in root_armatures:
        _set_armature_pose_position(arm, "POSE")
        try:
            saved_world = arm.matrix_world.copy()
            arm.parent = target_armature
            arm.parent_type = 'OBJECT'
            arm.matrix_world = saved_world
        except Exception:
            pass
        _constrain_bound_armature_to_target(arm, target_armature)
        _set_armature_pose_position(arm, "POSE")
    return True


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

    with _preserve_selection():
        try:
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

def _set_child_of_inverse_for_armature(bound_armature):
    """Set inverse for CHILD_OF constraints on a bound armature (keeps offsets)."""
    if not bound_armature or bound_armature.type != 'ARMATURE':
        return
    with _preserve_selection():
        try:
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

        with _preserve_selection(context):
            for i, slot in enumerate(slots):
                after_template = get_effective_equip_template(slot)
                after_active = bool(getattr(slot, "variant_active", False))
                if slot.is_loaded and (before_templates[i] != after_template or before_active[i] != after_active):
                    load_equipment_item(context, armature, i, rig_settings)
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
    source_game = _normalize_source_game(
        getattr(slot, "source_game", "")
        or get_equipment_source_game_for_search_roots(source_roots)
    )
    if source_game != "w2":
        source_candidates = {
            get_equipment_source_game_for_search_roots(source_roots),
            _infer_source_game_from_rig_settings(rig_settings, armature),
        }
        if "w2" in source_candidates:
            source_game = "w2"

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
        bound_source_game = source_game
        if bound_source_game != "w2" and _is_w2_search([export_path]):
            bound_source_game = "w2"

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
            attrs = get_item_attributes_by_identifier(bound_name, bound_source_game)
            if not attrs and template and template != bound_name:
                attrs = get_item_attributes_by_identifier(template, bound_source_game)
            bound_equip_slot = attrs.get("equip_slot", "")
        except Exception:
            attrs = {}
            bound_equip_slot = ""

        bound_slot_empty = None

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
            "is_valid": False,
            "target_type": "",
            "slot_empty": None,
            "armature": target_armature,
            "bone_name": "",
        }
        if bound_equip_slot:
            bound_target_info = _resolve_equipment_mount_target(bound_equip_slot, armature, rig_settings)
            bound_slot_empty = bound_target_info.get("slot_empty")
            if bound_target_info.get("armature") is not None:
                target_armature = bound_target_info.get("armature")
            if not bound_target_info.get("is_valid"):
                log_fn = log.debug if bound_source_game == "w2" else log.warning
                log_fn(
                    "Bound item '%s' requested target '%s' but no slot or bone was found",
                    bound_name,
                    bound_equip_slot,
                )
        allow_bound_unmounted_visual = (
            not bound_target_info.get("is_valid")
            and getattr(bound_attachment_profile, "kind", "") != "owner_graph"
        )
        bound_mount_strategy = _infer_equipment_mount_strategy(
            bound_attachment_profile,
            bound_target_info,
            allow_unmounted_visual=allow_bound_unmounted_visual,
        )
        attachment_type = str(attrs.get("attachment_type", "") or "").strip()
        attachment_prefix = str(attrs.get("attachment_prefix", "") or "").strip()
        w2_body_bound = bool(armature and _w2_attachment_binds_to_body(bound_source_game, attachment_type))
        w2_owner_animated_bound = bool(
            armature
            and _w2_bound_entity_binds_animated_to_owner(
                bound_source_game,
                bound_name,
                template,
                export_path,
                attrs,
                bound_attachment_profile,
            )
        )
        prefixed_animated_binding = (
            attachment_type.lower() == "animated"
            and bool(attachment_prefix)
            and getattr(bound_attachment_profile, "kind", "") == "slot_animated"
        )
        if prefixed_animated_binding or w2_owner_animated_bound:
            bound_mount_strategy = "owner_graph_bound"
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
        bound_use_owner_animated_bridge = False
        if w2_body_bound:
            bound_mount_strategy = "owner_graph_bound"
            bound_bind_armature = armature
            bound_use_skinning_bridge = True
        elif w2_owner_animated_bound:
            bound_bind_armature = armature
            bound_use_owner_animated_bridge = True
        log.debug(
            "Bound equipment attachment '%s': profile=%s strategy=%s target=%s bind_armature=%s skinning_bridge=%s owner_animated_bridge=%s",
            bound_name or template,
            getattr(bound_attachment_profile, "kind", "") or "unknown",
            bound_mount_strategy,
            _describe_mount_target(bound_target_info),
            getattr(bound_bind_armature, "name", ""),
            bound_use_skinning_bridge,
            bound_use_owner_animated_bridge,
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
        elif bound_use_owner_animated_bridge:
            if not _attach_imported_armatures_to_owner(new_objects, bound_bind_armature):
                log.warning(
                    "W2 animated bound item '%s' imported without an armature to bind to the owner",
                    bound_name or template,
                )
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

        # When a caller scopes lookup to an armature, do not fall back to the
        # global object table. Multiple imported entities can share entity names
        # such as "player", and a global "player:slot" lookup can bind equipment
        # to a different character instance.
        return None

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

default_categories = equipment_catalog.default_categories

# Define the EquipmentDefinitionEntry property group
class EquipmentDefinitionEntry(bpy.types.PropertyGroup):
    # Class aliases retained for existing UI code; catalog owns the storage.
    category_items = equipment_catalog.category_items
    item_attributes = equipment_catalog.item_attributes

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

        slot = rig_settings.equipment_slots[slot_index]
        with _preserve_selection(context):
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
        name="Item Name",
        default="None",
        update=update_item_attributes,
    )

    # The default item this category had when the entity was imported. The
    # picker's "Default" button restores this so users can revert experiments.
    import_default_item: bpy.props.StringProperty(
        name="Imported Default Item",
        default="None",
        options={'HIDDEN'},
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


class InventoryDefinitionEntry(bpy.types.PropertyGroup):
    source_game: bpy.props.StringProperty(name="Source Game", default="w3")
    entry_key: bpy.props.StringProperty(name="Entry Key", default="")
    source_label: bpy.props.StringProperty(name="Source", default="")
    category: bpy.props.StringProperty(name="Category", default="")
    item_name: bpy.props.StringProperty(name="Item", default="")
    resolved_item_name: bpy.props.StringProperty(name="Resolved Item", default="")
    equip_template: bpy.props.StringProperty(name="Equip Template", default="")
    source_is_mount: bpy.props.BoolProperty(
        name="Source Is Mount",
        description="Mount flag stored on the inventory definition",
        default=False,
    )
    is_mount: bpy.props.BoolProperty(
        name="Mounted",
        description="Current scene override for this inventory item",
        default=False,
    )
    is_loaded: bpy.props.BoolProperty(name="Loaded", default=False)
    is_lootable: bpy.props.BoolProperty(name="Lootable", default=False)
    quantity: bpy.props.IntProperty(name="Quantity", default=0)
    quantity_min: bpy.props.IntProperty(name="Quantity Min", default=0)
    quantity_max: bpy.props.IntProperty(name="Quantity Max", default=0)
    probability: bpy.props.FloatProperty(name="Probability", default=0.0)
    slot_index: bpy.props.IntProperty(name="Slot Index", default=-1, options={'HIDDEN'})


# Temporary data storage in WindowManager
class WitcherUITempData(bpy.types.PropertyGroup):
    equipment_entries: bpy.props.CollectionProperty(type=EquipmentDefinitionEntry)
    equipment_entries_index: bpy.props.IntProperty()
    last_app_list_index: bpy.props.IntProperty(default=-1)
    last_armature_name: bpy.props.StringProperty(default="")
    last_entity_state_token: bpy.props.StringProperty(default="")
    equipment_source_game: bpy.props.StringProperty(default="w3")
    auto_apply_equipment_selection: bpy.props.BoolProperty(
        name="Auto Toggle",
        description="Automatically unload and reload the changed equipment slot when you select a new asset.",
        default=True,
    )
    suspend_auto_apply_updates: bpy.props.BoolProperty(default=False, options={'HIDDEN'})

    included_template_entries: bpy.props.CollectionProperty(type=IncludedTemplateEntry)
    included_template_entries_index: bpy.props.IntProperty()

    inventory_entries: bpy.props.CollectionProperty(type=InventoryDefinitionEntry)
    inventory_entries_index: bpy.props.IntProperty()
    inventory_preset_id: bpy.props.StringProperty(
        name="Inventory Preset",
        default="",
        options={'HIDDEN'},
    )

    item_picker_rows: bpy.props.CollectionProperty(type=EquipmentItemPickerRow)
    item_picker_index: bpy.props.IntProperty(default=-1, update=_on_equipment_item_picker_index_changed)
    item_picker_entry_index: bpy.props.IntProperty(default=-1, options={'HIDDEN'})
    item_picker_source_game: bpy.props.StringProperty(default="w3", options={'HIDDEN'})
    item_picker_filter_token: bpy.props.StringProperty(default="", options={'HIDDEN'})
    item_picker_match_count: bpy.props.IntProperty(default=0, options={'HIDDEN'})
    item_picker_all_count: bpy.props.IntProperty(default=0, options={'HIDDEN'})
    item_picker_suppress_select: bpy.props.BoolProperty(default=False, options={'HIDDEN'})
    # Live picker state. Stored here (not on the operator) so it survives the
    # close+reopen the "close on select" popup mode uses to navigate pages.
    item_picker_search: bpy.props.StringProperty(
        name="Search",
        default="",
        options={'TEXTEDIT_UPDATE'},
        update=_on_equipment_item_picker_filter_changed,
    )
    item_picker_view: bpy.props.EnumProperty(
        name="View",
        items=[
            ('LIST', "List", "Show a list with large thumbnails", 'SHORTDISPLAY', 0),
            ('GRID', "Grid", "Show a thumbnail grid", 'IMGDISPLAY', 1),
        ],
        default='LIST',
        update=_on_equipment_item_picker_filter_changed,
    )
    item_picker_sort: bpy.props.EnumProperty(
        name="Sort",
        items=[
            ('RECENT', "Recent", "Recently picked items first", 'TIME', 0),
            ('NAME_ASC', "Name A-Z", "Sort by name ascending", 'SORTALPHA', 1),
            ('NAME_DESC', "Name Z-A", "Sort by name descending", 'SORTALPHA', 2),
        ],
        default='NAME_ASC',
        update=_on_equipment_item_picker_filter_changed,
    )
    item_picker_grid_size: bpy.props.EnumProperty(
        name="Tile Size",
        description="Thumbnail size in grid view",
        items=[
            ('S', "Small", "Small thumbnails (more per row)", 'NODE_TEXTURE', 0),
            ('M', "Medium", "Medium thumbnails", 'TEXTURE', 1),
            ('L', "Large", "Large thumbnails (asset-browser size)", 'IMAGE_DATA', 2),
        ],
        default='L',
        update=_on_equipment_item_picker_filter_changed,
    )
    item_picker_page: bpy.props.IntProperty(default=0, min=0, options={'HIDDEN'})

    preset_picker_rows: bpy.props.CollectionProperty(type=EquipmentPresetPickerRow)
    preset_picker_index: bpy.props.IntProperty(default=-1, options={'HIDDEN'})
    preset_picker_target: bpy.props.StringProperty(default="INVENTORY", options={'HIDDEN'})
    preset_picker_filter_token: bpy.props.StringProperty(default="", options={'HIDDEN'})
    preset_picker_match_count: bpy.props.IntProperty(default=0, options={'HIDDEN'})
    preset_picker_all_count: bpy.props.IntProperty(default=0, options={'HIDDEN'})
    preset_picker_search: bpy.props.StringProperty(
        name="Search",
        default="",
        options={'TEXTEDIT_UPDATE'},
        update=_on_equipment_preset_picker_filter_changed,
    )
    preset_picker_view: bpy.props.EnumProperty(
        name="View",
        items=[
            ('LIST', "List", "Show set item icons", 'SHORTDISPLAY', 0),
            ('GRID', "Grid", "Show torso thumbnails", 'IMGDISPLAY', 1),
        ],
        default='LIST',
        update=_on_equipment_preset_picker_filter_changed,
    )
    preset_picker_sort: bpy.props.EnumProperty(
        name="Sort",
        items=[
            ('ORDER', "Preset Order", "Keep bundled presets in shipped order", 'SORTSIZE', 0),
            ('NAME_ASC', "Name A-Z", "Sort by name ascending", 'SORTALPHA', 1),
            ('NAME_DESC', "Name Z-A", "Sort by name descending", 'SORTALPHA', 2),
        ],
        default='ORDER',
        update=_on_equipment_preset_picker_filter_changed,
    )
    preset_picker_grid_size: bpy.props.EnumProperty(
        name="Tile Size",
        description="Thumbnail size in grid view",
        items=[
            ('S', "Small", "Small thumbnails (more per row)", 'NODE_TEXTURE', 0),
            ('M', "Medium", "Medium thumbnails", 'TEXTURE', 1),
            ('L', "Large", "Large thumbnails (asset-browser size)", 'IMAGE_DATA', 2),
        ],
        default='L',
        update=_on_equipment_preset_picker_filter_changed,
    )
    preset_picker_page: bpy.props.IntProperty(default=0, min=0, options={'HIDDEN'})


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


class EQUIPMENT_UL_InventoryList(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        toggle_icon = 'CHECKMARK' if getattr(item, "is_mount", False) else 'RADIOBUT_OFF'
        op = row.operator("witcher.equipment_toggle_inventory_mount", text="", icon=toggle_icon)
        op.entry_index = index
        label = f"{getattr(item, 'category', '') or 'None'}: {getattr(item, 'item_name', '') or 'None'}"
        row.label(text=label)


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


# Equipment catalog XML wrappers. The implementation lives in equipment_catalog;
# these names remain for older callers and operators in this module.
def extract_categories_from_xml(folder_path):
    return equipment_catalog.extract_categories_from_xml(folder_path)


def _flatten_bundle_item_candidates(items):
    return equipment_catalog.flatten_bundle_item_candidates(items)


def _select_final_bundle_item(items):
    return equipment_catalog.select_final_bundle_item(items)


def _get_equipment_xml_bundle_cache_root():
    return equipment_catalog.get_equipment_xml_bundle_cache_root()


def _extract_equipment_xmls_from_bundles():
    return equipment_catalog.extract_equipment_xmls_from_bundles()


def _get_equipment_xml_sources(context, addon_prefs):
    return equipment_catalog.get_equipment_xml_sources(context, addon_prefs)


def _refresh_w3_catalog_from_xml(context):
    return equipment_catalog.refresh_w3_catalog_from_xml(context)


def _merge_equipment_xml_data(target_categories, target_attributes, source_categories, source_attributes):
    return equipment_catalog.merge_equipment_xml_data(
        target_categories,
        target_attributes,
        source_categories,
        source_attributes,
    )


def _strip_duplicate_xml_attributes(xml_text):
    return equipment_catalog.strip_duplicate_xml_attributes(xml_text)


def _is_plaintext_xml_candidate(file_path):
    return equipment_catalog.is_plaintext_xml_candidate(file_path)


def _parse_xml_text_with_game_fallbacks(text):
    return equipment_catalog.parse_xml_text_with_game_fallbacks(text)


def _parse_xml_root_with_fallbacks(file_path):
    return equipment_catalog.parse_xml_root_with_fallbacks(file_path)

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
        _clear_item_attribute_identifier_lookup("w3")

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
        _refresh_variants_and_reload(context, ob, rig_settings)

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
            equipment_catalog.ensure_w2_category_cache_loaded()
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
            temp_data.inventory_entries.clear()

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
                    source_roots = _get_inventory_source_roots_for_rig(main_arm_obj, rig_settings)
                    try:
                        item_lookup, template_lookup = import_entity._build_equipment_lookup(source_roots)
                    except Exception:
                        item_lookup, template_lookup = {}, {}
                    _set_temp_equipment_auto_apply_suspended(context, True)
                    try:
                        # First load: populate temp entries from appearance JSON
                        for entry_data in equipment_entries_data:
                            category_val = import_entity._get_entry_attr(entry_data, 'category', '') or 'None'
                            default_item_name = import_entity.get_equipment_entry_item_name(entry_data, rig_settings) or 'None'
                            category_val, default_item_name, equip_template, _attrs = _resolve_appearance_equipment_item_for_ui(
                                entry_data,
                                rig_settings,
                                category_val,
                                source_roots,
                                active_category_items,
                                active_item_attributes,
                                item_lookup=item_lookup,
                                template_lookup=template_lookup,
                            )
                            # Pre-populate catalog so update callbacks can find the item
                            if category_val not in active_category_items:
                                active_category_items[category_val] = [("None", "None", "")]
                            if default_item_name != 'None':
                                item_names = [it[0] for it in active_category_items[category_val]]
                                if default_item_name not in item_names:
                                    active_category_items[category_val].append((default_item_name, default_item_name, equip_template or ""))
                            entry = temp_data.equipment_entries.add()
                            entry.slot_index = -1
                            entry.source_game = temp_data.equipment_source_game
                            entry.category = category_val
                            entry.defaultItemName = default_item_name
                            entry.import_default_item = default_item_name
                            entry.update_item_attributes(context)

                        # Also create persistent equipment_slots from appearance data
                        for entry_data in equipment_entries_data:
                            category_val = import_entity._get_entry_attr(entry_data, 'category', '') or ''
                            category_val, item_name, equip_template, attrs = _resolve_appearance_equipment_item_for_ui(
                                entry_data,
                                rig_settings,
                                category_val,
                                source_roots,
                                active_category_items,
                                active_item_attributes,
                                item_lookup=item_lookup,
                                template_lookup=template_lookup,
                            )
                            slot = rig_settings.equipment_slots.add()
                            slot.source_game = temp_data.equipment_source_game
                            slot.category = category_val
                            slot.item_name = item_name if item_name and item_name != "None" else ''
                            slot.equip_template = equip_template or ''
                            slot.base_equip_template = slot.equip_template
                            slot.resolved_repo_path = ""
                            slot.keep_across_appearances = False
                            try:
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

        # prop_enum tab buttons can lose their captions when property split is enabled.
        prev_split = layout.use_property_split
        layout.use_property_split = False
        tab_row = layout.row(align=True)
        tab_row.prop_enum(rig_settings, "equipment_ui_tab", 'EQUIPMENT')
        tab_row.prop_enum(rig_settings, "equipment_ui_tab", 'INVENTORY')
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

        if tab == "INVENTORY":
            _maybe_validate_equipment_slots(rig_settings)
            sync_inventory_entries_to_temp(context, rig_settings, entity_data=entity_data)

            box = layout.box()
            box.label(text=f"Inventory Items ({len(temp_data.inventory_entries)})", icon='PACKAGE')

            active_inventory_preset_id = _get_inventory_preset_selection(context, target="INVENTORY")
            preset_row = box.row(align=True)
            preset_row.label(text="Preset:")
            preset_row.operator(
                "witcher.equipment_select_inventory_preset",
                text=_inventory_preset_label(
                    active_inventory_preset_id,
                    source_game=_inventory_preset_source_game_for_target(context, "INVENTORY"),
                ),
                icon='DOWNARROW_HLT',
            )
            apply_row = preset_row.row(align=True)
            apply_row.enabled = bool(_get_inventory_preset(
                active_inventory_preset_id,
                source_game=_inventory_preset_source_game_for_target(context, "INVENTORY"),
            ))
            apply_row.operator("witcher.equipment_apply_inventory_preset", text="", icon='IMPORT')
            save_op = preset_row.operator("witcher.equipment_save_inventory_preset", text="", icon='FILE_TICK')
            save_op.source = 'INVENTORY'

            if len(temp_data.inventory_entries) == 0:
                box.label(text="No inventory entries for this appearance.", icon='INFO')
                box.operator("witcher.equipment_refresh_inventory", text="Refresh", icon='FILE_REFRESH')
                return

            row = box.row()
            row.template_list("EQUIPMENT_UL_InventoryList", "", temp_data, "inventory_entries", temp_data, "inventory_entries_index")
            col = row.column(align=True)
            col.operator("witcher.equipment_refresh_inventory", icon="FILE_REFRESH", text="")

            index = temp_data.inventory_entries_index
            if index >= 0 and index < len(temp_data.inventory_entries):
                entry = temp_data.inventory_entries[index]
                action_row = box.row(align=True)
                op = action_row.operator(
                    "witcher.equipment_toggle_inventory_mount",
                    text="Unmount" if entry.is_mount else "Mount",
                    icon='CHECKMARK' if entry.is_mount else 'IMPORT',
                )
                op.entry_index = index
                action_row.label(
                    text="Loaded" if entry.is_loaded else ("Mounted" if entry.is_mount else "Inventory"),
                    icon='CHECKMARK' if entry.is_loaded else 'RADIOBUT_OFF',
                )

                details = box.box()
                values = details.column()
                values.enabled = False
                values.prop(entry, "source_label")
                values.prop(entry, "category")
                values.prop(entry, "item_name")
                if entry.resolved_item_name and entry.resolved_item_name != entry.item_name:
                    values.prop(entry, "resolved_item_name")
                values.prop(entry, "equip_template")
                values.prop(entry, "source_is_mount")
                values.prop(entry, "is_loaded")
                if entry.quantity or entry.quantity_min or entry.quantity_max:
                    values.prop(entry, "quantity")
                    values.prop(entry, "quantity_min")
                    values.prop(entry, "quantity_max")
                if entry.probability:
                    values.prop(entry, "probability")
                if entry.is_lootable:
                    values.prop(entry, "is_lootable")
            return

        if tab == "EQUIPMENT":
            # =============================================================
            # EQUIPMENT SECTION (persistent, GUID-tracked)
            # =============================================================
            # Periodically reconcile is_loaded flags with actual scene state
            # so the UI never shows stale "loaded" indicators after the user
            # deletes objects manually.
            _maybe_validate_equipment_slots(rig_settings)

            box = layout.box()
            box.label(text="Currently Equipped", icon='COMMUNITY')

            # Master appearance dropdown
            mast_row = box.row(align=True)
            mast_row.prop(rig_settings, "master_equipment_appearance", text="")
            mast_row.operator("witcher.equipment_set_master_appearance", text="Apply to All", icon='IMPORT')

            row = box.row(align=True)
            row.label(text=f"Variants: {'Auto' if rig_settings.variants_auto else 'Manual'}")
            row.operator("witcher.equipment_toggle_variant_mode", text="Switch")

            # Show persistent equipment slots with status.
            # Every slot with a category OR an active selection is shown,
            # even inventory items without a mount target - they get a
            # disabled load button instead of being hidden entirely.
            if len(rig_settings.equipment_slots) > 0:
                try:
                    refresh_variant_states(rig_settings)
                except Exception:
                    pass
                visible_slots = []
                for i, slot in enumerate(rig_settings.equipment_slots):
                    # Show any slot that has a category OR is loaded/active.
                    if not slot.category and not _slot_has_active_selection(slot):
                        continue
                    slot_policy = _resolve_slot_visual_policy(slot, main_arm_obj, rig_settings)
                    # Hide nonvisual inventory items only when they have no
                    # assigned category AND no item_name — those are noise entries with no
                    # visual representation. Slots *with* a category or item_name came
                    # from the entity's equipment data and must stay visible
                    # even after being unloaded (is_loaded == False), so the
                    # user can re-load them.
                    item_name_str = str(getattr(slot, "item_name", "") or "").strip().lower()
                    has_item = bool(item_name_str and item_name_str != "none")
                    if (
                        getattr(slot, "is_inventory", False)
                        and slot_policy["policy"] == "nonvisual_on_rig"
                        and not getattr(slot, "is_loaded", False)
                        and not slot.category
                        and not has_item
                    ):
                        continue
                    visible_slots.append((i, slot, slot_policy))
                for i, slot, slot_policy in visible_slots:
                    has_variants = bool(_safe_json_list(getattr(slot, "variants_json", "")))
                    has_appearance_ui = _slot_supports_item_appearance_ui(slot)
                    colorable_meshes = _get_slot_colorable_meshes(slot)
                    has_coloring_ui = _slot_supports_item_coloring_ui(slot, colorable_meshes=colorable_meshes)
                    has_details_ui = _slot_supports_item_details_ui(slot, colorable_meshes=colorable_meshes)
                    has_rune_ui = _slot_supports_rune_ui(slot)
                    requested_mount_mode = _get_slot_requested_mount_mode(slot, slot_policy)
                    can_load = _can_load_slot_for_mount_mode(slot_policy, requested_mount_mode)

                    # --- Main slot row (compact, single-line by default) ---
                    split = box.split(factor=0.50, align=True)
                    row = split.row(align=True)
                    icon = 'CHECKMARK' if slot.is_loaded else 'RADIOBUT_OFF'
                    label_text = f"{slot.category}: {slot.item_name or 'None'}"
                    row.label(text=label_text, icon=icon)
                    if has_rune_ui:
                        rune_inline = row.row(align=True)
                        rune_inline.prop(slot, "rune_level", text="Rune")

                    controls = split.row(align=True)

                    var_sub = controls.row(align=True)
                    var_sub.enabled = has_variants and not rig_settings.variants_auto
                    var_sub.prop(slot, "variants_enabled", text="", icon='SHAPEKEY_DATA', toggle=True)

                    color_sub = controls.row(align=True)
                    color_sub.enabled = has_details_ui
                    color_sub.prop(slot, "show_item_coloring_ui", text="", icon='MOD_HUE_SATURATION', toggle=True)

                    show_toggle, toggle_text, toggle_icon = _get_slot_hold_toggle_state(slot, slot_policy)

                    tog_sub = controls.row(align=True)
                    tog_sub.enabled = show_toggle
                    op = tog_sub.operator("witcher.equipment_toggle_item", text=toggle_text, icon=toggle_icon)
                    op.slot_index = i if show_toggle else -1

                    vis_sub = controls.row(align=True)
                    vis_sub.enabled = slot.is_loaded
                    is_hidden = bool(slot.is_loaded and _is_guid_hidden(slot.equip_guid, "witcher_equip_guid"))
                    vis_icon = 'HIDE_OFF' if is_hidden else 'HIDE_ON'
                    vis_op_name = "witcher.equipment_show_equipment" if is_hidden else "witcher.equipment_hide_equipment"
                    op = vis_sub.operator(vis_op_name, text="", icon=vis_icon)
                    op.slot_index = i if slot.is_loaded else -1

                    if slot.is_loaded:
                        op = controls.operator("witcher.equipment_unload_equipment", text="", icon='X')
                        op.slot_index = i
                    else:
                        btn = controls.row(align=True)
                        btn.enabled = can_load
                        if can_load:
                            op = btn.operator("witcher.equipment_load_equipment", text="", icon='IMPORT')
                            op.slot_index = i
                            op.mount_mode = requested_mount_mode
                        else:
                            op = btn.operator("witcher.equipment_load_disabled", text="", icon='IMPORT')
                            op.reason = slot_policy.get("reason", "Incompatible item/rig.")

                    # --- Secondary rows (only when relevant) ---

                    _draw_slot_details_ui(
                        box,
                        slot,
                        has_appearance_ui=has_appearance_ui,
                        has_coloring_ui=has_coloring_ui,
                        colorable_meshes=colorable_meshes,
                    )

                    # Bound items
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
            row.operator("witcher.equipment_validate", text="", icon='FILE_REFRESH')

            preset_row = box.row(align=True)
            preset_op = preset_row.operator(
                "witcher.equipment_save_inventory_preset",
                text="Save as Inventory Preset",
                icon='FILE_TICK',
            )
            preset_op.source = 'EQUIPMENT'

            if temp_data.equipment_source_game != "w3":
                info = box.box()
                info.label(text="Witcher 2 categories can be edited from the list below.", icon='INFO')
                info.label(text="Changes apply to the current loaded entity only.")

            settings_row = box.row(align=True)
            settings_row.prop(temp_data, "auto_apply_equipment_selection")
            init_icon = 'CHECKBOX_HLT' if getattr(rig_settings, "use_equipment_initializers", True) else 'CHECKBOX_DEHLT'
            settings_row.operator("witcher.equipment_toggle_initializers", text="Initializers", icon=init_icon)

            # Temp data equipment list (for dropdown editing)
            row = box.row()
            row.template_list("EQUIPMENT_UL_CategoryList", "", temp_data, "equipment_entries", temp_data, "equipment_entries_index")
            if temp_data.equipment_source_game == "w3":
                col = row.column(align=True)
                col.operator("witcher.equipment_add_category", icon="ADD", text="")
                col.operator("witcher.equipment_remove_category", icon="REMOVE", text="")
                col.separator(factor=0.5)
                col.operator("witcher.equipment_move_category", icon="TRIA_UP", text="").direction = 'UP'
                col.operator("witcher.equipment_move_category", icon="TRIA_DOWN", text="").direction = 'DOWN'
            else:
                col = row.column(align=True)
                col.operator("witcher.equipment_add_category", icon="ADD", text="")
                col.operator("witcher.equipment_remove_category", icon="REMOVE", text="")
                col.separator(factor=0.5)
                col.operator("witcher.equipment_move_category", icon="TRIA_UP", text="").direction = 'UP'
                col.operator("witcher.equipment_move_category", icon="TRIA_DOWN", text="").direction = 'DOWN'

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
                row.scale_y = 2.2
                row.label(text="Item:")
                op = row.operator(
                    "witcher.equipment_search_default_item",
                    text=entry.defaultItemName or "None",
                    icon='DOWNARROW_HLT',
                )
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


class EQUIPMENT_OT_ValidateEquipment(bpy.types.Operator):
    """Scan equipment and template slots and repair stale loaded/runtime states.

    Fixes slots that claim to be loaded but whose GUID-tagged objects no
    longer exist in the scene (e.g. after manual deletion) and refreshes
    post-load mount wiring for saved equipment.
    """
    bl_idname = "witcher.equipment_validate"
    bl_label = "Validate Equipment"
    bl_description = "Detect and fix stale equipment/template states caused by manual object deletion"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        armature, rig_settings = _get_armature_and_rig_settings(context)
        if not rig_settings:
            self.report({'WARNING'}, "No valid armature selected.")
            return {'CANCELLED'}

        refreshed, repaired = repair_saved_equipment_state(armature, rig_settings)

        if refreshed or repaired:
            msg = f"Repaired {repaired} equipment state(s)"
            if refreshed:
                msg += f", refreshed {refreshed} slot constraint(s)"
            self.report({'INFO'}, msg + ".")
        else:
            self.report({'INFO'}, "All equipment/template slots are consistent.")
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

        _set_temp_equipment_auto_apply_suspended(context, True)
        try:
            # Determine the initial category BEFORE creating the slot so
            # both entry and slot get the same value atomically.
            source_game = _get_temp_source_game(context)
            category_items, _item_attrs = _get_equipment_catalog(source_game)
            initial_category = ""
            if category_items:
                initial_category = next(iter(category_items), "")

            slot = rig_settings.equipment_slots.add()
            slot.source_game = source_game
            slot.category = initial_category
            slot.item_name = "None"
            slot.equip_template = ""
            slot.base_equip_template = ""
            slot.resolved_repo_path = ""
            slot.keep_across_appearances = True

            entry = temp_data.equipment_entries.add()
            entry.slot_index = len(rig_settings.equipment_slots) - 1
            entry.source_game = source_game
            entry.category = initial_category
            # defaultItemName resets automatically via _on_category_changed, but
            # set it explicitly here in case the update didn't fire yet
            try:
                item_items = entry.get_default_items(context)
                entry.defaultItemName = item_items[0][0] if item_items else "None"
            except Exception:
                entry.defaultItemName = "None"
            entry.import_default_item = entry.defaultItemName
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


class EQUIPMENT_OT_MoveCategory(bpy.types.Operator):
    bl_idname = "witcher.equipment_move_category"
    bl_label = "Move Category"
    bl_description = "Move the selected equipment category up or down"
    bl_options = {'REGISTER', 'UNDO'}

    direction: bpy.props.EnumProperty(
        name="Direction",
        items=[
            ('UP', "Up", "Move category up"),
            ('DOWN', "Down", "Move category down"),
        ],
        default='UP',
        options={'HIDDEN'},
    )

    def execute(self, context):
        armature, rig_settings = _get_armature_and_rig_settings(context)
        temp_data = getattr(context.window_manager, "witcherui_temp_data", None)
        if rig_settings is None or temp_data is None:
            self.report({'WARNING'}, "No valid armature selected.")
            return {'CANCELLED'}
        index = int(getattr(temp_data, "equipment_entries_index", -1))
        if index < 0 or index >= len(temp_data.equipment_entries):
            self.report({'WARNING'}, "No equipment category selected.")
            return {'CANCELLED'}

        entry = temp_data.equipment_entries[index]
        slot_index = int(getattr(entry, "slot_index", -1))
        if slot_index < 0 or slot_index >= len(rig_settings.equipment_slots):
            slot_index = index if index < len(rig_settings.equipment_slots) else -1
        if slot_index < 0:
            self.report({'WARNING'}, "Selected category has no editable slot.")
            return {'CANCELLED'}

        delta = -1 if self.direction == 'UP' else 1
        target_index = slot_index + delta
        if target_index < 0 or target_index >= len(rig_settings.equipment_slots):
            return {'CANCELLED'}

        try:
            rig_settings.equipment_slots.move(slot_index, target_index)
        except Exception:
            self.report({'WARNING'}, "Could not move equipment category.")
            return {'CANCELLED'}

        sync_equipment_slots_to_temp(context, rig_settings)
        try:
            for temp_index, temp_entry in enumerate(temp_data.equipment_entries):
                if int(getattr(temp_entry, "slot_index", -1)) == target_index:
                    temp_data.equipment_entries_index = temp_index
                    break
        except Exception:
            pass
        if context.area:
            context.area.tag_redraw()
        return {'FINISHED'}


class EQUIPMENT_OT_ToggleInventoryMount(bpy.types.Operator):
    bl_idname = "witcher.equipment_toggle_inventory_mount"
    bl_label = "Toggle Inventory Mount"
    bl_description = "Mount or unmount this inventory item over the selected appearance"
    bl_options = {'REGISTER', 'UNDO'}

    entry_index: bpy.props.IntProperty(default=-1, options={'HIDDEN'})

    def execute(self, context):
        armature, rig_settings = _get_armature_and_rig_settings(context)
        if not armature or not rig_settings:
            self.report({'WARNING'}, "No valid armature selected.")
            return {'CANCELLED'}

        temp_data = getattr(context.window_manager, "witcherui_temp_data", None)
        if temp_data is None:
            return {'CANCELLED'}
        index = self.entry_index
        if index < 0:
            index = temp_data.inventory_entries_index
        if index < 0 or index >= len(temp_data.inventory_entries):
            self.report({'WARNING'}, "No inventory item selected.")
            return {'CANCELLED'}

        entry = temp_data.inventory_entries[index]
        category = getattr(entry, "category", "") or ""
        item_name = getattr(entry, "item_name", "") or ""
        desired_mount = not bool(getattr(entry, "is_mount", False))
        source_mount = bool(getattr(entry, "source_is_mount", False))

        _set_inventory_mount_override(rig_settings, category, item_name, desired_mount, source_mount)

        with _preserve_selection(context):
            if desired_mount:
                try:
                    _apply_inventory_mount_overrides(context, armature, rig_settings)
                except Exception:
                    log.warning("Failed to mount inventory item %s:%s", category, item_name, exc_info=True)
                    self.report({'WARNING'}, "Inventory mount failed.")
                    return {'CANCELLED'}
            else:
                _unmount_inventory_entry(context, armature, rig_settings, entry)
                try:
                    _refresh_variants_and_reload(context, armature, rig_settings)
                except Exception:
                    pass

        sync_equipment_slots_to_temp(context, rig_settings)
        _entity, entity_data = import_entity.get_rig_entity_state(rig_settings)
        sync_inventory_entries_to_temp(context, rig_settings, entity_data=entity_data)
        if context.area:
            context.area.tag_redraw()
        self.report({'INFO'}, f"{'Mounted' if desired_mount else 'Unmounted'} inventory item: {category}:{item_name}")
        return {'FINISHED'}


class EQUIPMENT_OT_ToggleEquipmentInitializers(bpy.types.Operator):
    bl_idname = "witcher.equipment_toggle_initializers"
    bl_label = "Toggle Equipment Initializers"
    bl_description = "Switch appearance equipment between initializer items and default items"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        armature, rig_settings = _get_armature_and_rig_settings(context)
        if not armature or not rig_settings:
            self.report({'WARNING'}, "No valid armature selected.")
            return {'CANCELLED'}

        desired = not bool(getattr(rig_settings, "use_equipment_initializers", True))
        rig_settings.use_equipment_initializers = desired

        with _preserve_selection(context):
            try:
                loaded = _rebuild_appearance_equipment_slots(context, armature, rig_settings)
            except Exception:
                log.warning("Failed to switch equipment initializer mode", exc_info=True)
                self.report({'WARNING'}, "Equipment reload failed.")
                return {'CANCELLED'}

        sync_equipment_slots_to_temp(context, rig_settings)
        _entity, entity_data = import_entity.get_rig_entity_state(rig_settings)
        sync_inventory_entries_to_temp(context, rig_settings, entity_data=entity_data)
        if context.area:
            context.area.tag_redraw()

        mode = "Initializers" if desired else "Defaults"
        self.report({'INFO'}, f"Equipment mode set to {mode}; loaded {loaded} item(s).")
        return {'FINISHED'}


class EQUIPMENT_OT_RefreshInventoryEntries(bpy.types.Operator):
    bl_idname = "witcher.equipment_refresh_inventory"
    bl_label = "Refresh Inventory"
    bl_description = "Refresh the inventory item list for the selected appearance"

    def execute(self, context):
        armature, rig_settings = _get_armature_and_rig_settings(context)
        if not rig_settings:
            self.report({'WARNING'}, "No valid armature selected.")
            return {'CANCELLED'}
        _entity, entity_data = import_entity.get_rig_entity_state(rig_settings)
        sync_inventory_entries_to_temp(context, rig_settings, entity_data=entity_data)
        if context.area:
            context.area.tag_redraw()
        self.report({'INFO'}, "Inventory list refreshed.")
        return {'FINISHED'}


class EQUIPMENT_OT_SaveInventoryPreset(bpy.types.Operator):
    bl_idname = "witcher.equipment_save_inventory_preset"
    bl_label = "Save Inventory Preset"
    bl_description = "Save the current equipment or inventory rows as an inventory preset"
    bl_options = {'REGISTER', 'UNDO'}

    source: bpy.props.EnumProperty(
        name="Source",
        items=[
            ('EQUIPMENT', "Equipment", "Save current equipped slots as mounted inventory entries"),
            ('INVENTORY', "Inventory", "Save current inventory rows and mount states"),
        ],
        default='EQUIPMENT',
        options={'HIDDEN'},
    )
    preset_name: bpy.props.StringProperty(name="Preset Name", default="")

    def invoke(self, context, event):
        _armature, rig_settings = _get_armature_and_rig_settings(context)
        if rig_settings and not self.preset_name:
            app_name = get_current_appearance_name(rig_settings) or ""
            base_name = app_name or getattr(rig_settings, "entity_name", "") or "Inventory Preset"
            self.preset_name = str(base_name).strip() or "Inventory Preset"
        return context.window_manager.invoke_props_dialog(self, width=360)

    def draw(self, context):
        self.layout.prop(self, "preset_name")

    def execute(self, context):
        _armature, rig_settings = _get_armature_and_rig_settings(context)
        if rig_settings is None:
            self.report({'WARNING'}, "No valid armature selected.")
            return {'CANCELLED'}
        temp_data = getattr(context.window_manager, "witcherui_temp_data", None)
        source_game = _normalize_source_game(
            getattr(rig_settings, "source_game", "")
            or getattr(temp_data, "equipment_source_game", "")
            or "w3"
        )
        if self.source == 'INVENTORY':
            try:
                _entity, entity_data = import_entity.get_rig_entity_state(rig_settings)
                sync_inventory_entries_to_temp(context, rig_settings, entity_data=entity_data)
            except Exception:
                pass
            entries = _inventory_preset_entries_from_inventory_rows(temp_data)
            source_label = "inventory"
        else:
            entries = _inventory_preset_entries_from_equipment_slots(rig_settings)
            source_label = "equipment"

        if not entries:
            self.report({'WARNING'}, "No inventory preset items to save.")
            return {'CANCELLED'}

        preset = _save_new_inventory_preset(
            self.preset_name,
            entries,
            source=source_label,
            source_game=source_game,
        )
        if temp_data is not None:
            temp_data.inventory_preset_id = str(preset.get("id", "") or "")
        if context.area:
            context.area.tag_redraw()
        self.report({'INFO'}, f"Saved inventory preset: {preset.get('name', '')}")
        return {'FINISHED'}


class EQUIPMENT_OT_SelectInventoryPreset(bpy.types.Operator):
    bl_idname = "witcher.equipment_select_inventory_preset"
    bl_label = "Select Inventory Preset"
    bl_description = "Select an inventory preset"

    target: bpy.props.EnumProperty(
        name="Target",
        items=[
            ('INVENTORY', "Inventory", "Select the current Inventory tab preset"),
            ('GERALT_W3', "Geralt", "Select the preset used by the Geralt quick import"),
            ('GERALT_W2', "Geralt W2", "Select the preset used by the Geralt W2 quick import"),
        ],
        default='INVENTORY',
        options={'HIDDEN'},
    )
    tooltip: bpy.props.StringProperty(default="", options={'HIDDEN', 'SKIP_SAVE'})

    @classmethod
    def description(cls, context, properties):
        tip = str(getattr(properties, "tooltip", "") or "").strip()
        if tip:
            return tip
        return cls.bl_description

    def invoke(self, context, event):
        temp_data = _get_temp_equipment_data(context)
        if temp_data is None:
            return {'CANCELLED'}
        target = _normalize_inventory_preset_selection_target(self.target)
        try:
            temp_data.preset_picker_target = target
            temp_data.preset_picker_search = ""
            temp_data.preset_picker_page = 0
            temp_data.preset_picker_filter_token = ""
        except Exception:
            pass
        _get_equipment_placeholder_icon_id()
        try:
            context.window_manager.invoke_props_dialog(
                self,
                width=inventory_preset_picker_width(),
                confirm_text="Done",
            )
        except TypeError:
            context.window_manager.invoke_props_dialog(self, width=inventory_preset_picker_width())
        return {'RUNNING_MODAL'}

    def draw(self, context):
        draw_inventory_preset_picker(context, self.layout)

    def execute(self, context):
        return {'FINISHED'}


class EQUIPMENT_OT_ClearInventoryPreset(bpy.types.Operator):
    bl_idname = "witcher.equipment_clear_inventory_preset"
    bl_label = "Disable Inventory Preset"
    bl_description = "Do not apply an inventory preset for this target"
    bl_options = {'INTERNAL'}

    target: bpy.props.EnumProperty(
        name="Target",
        items=[
            ('INVENTORY', "Inventory", "Clear the current Inventory tab preset"),
            ('GERALT_W3', "Geralt", "Disable the Geralt quick import preset"),
            ('GERALT_W2', "Geralt W2", "Disable the Geralt W2 quick import preset"),
        ],
        default='INVENTORY',
        options={'HIDDEN'},
    )

    @classmethod
    def description(cls, context, properties):
        target = _normalize_inventory_preset_selection_target(getattr(properties, "target", "INVENTORY"))
        if target == "GERALT_W2":
            return "Import Geralt W2 without applying an inventory preset"
        if target == "GERALT_W3":
            return "Import Geralt without applying an inventory preset"
        return cls.bl_description

    def execute(self, context):
        if not _set_inventory_preset_selection(context, "", target=self.target):
            return {'CANCELLED'}
        self.report({'INFO'}, "Inventory preset disabled.")
        return {'FINISHED'}


class EQUIPMENT_OT_ApplyInventoryPreset(bpy.types.Operator):
    bl_idname = "witcher.equipment_apply_inventory_preset"
    bl_label = "Apply Inventory Preset"
    bl_description = "Apply the selected inventory preset to the current character"
    bl_options = {'REGISTER', 'UNDO'}

    preset_id: bpy.props.StringProperty(default="", options={'HIDDEN'})

    def execute(self, context):
        armature, rig_settings = _get_armature_and_rig_settings(context)
        if not armature or rig_settings is None:
            self.report({'WARNING'}, "No valid armature selected.")
            return {'CANCELLED'}

        temp_data = getattr(context.window_manager, "witcherui_temp_data", None)
        source_game = _infer_source_game_from_rig_settings(rig_settings, armature)
        preset_id = str(self.preset_id or _get_inventory_preset_selection(context, target="INVENTORY") or "").strip()
        preset = _get_inventory_preset(preset_id, source_game=source_game)
        if not preset:
            self.report({'WARNING'}, "No inventory preset selected.")
            return {'CANCELLED'}

        entity_data = _set_entity_inventory_definitions_for_preset(rig_settings, preset)
        if entity_data is None:
            self.report({'WARNING'}, "Could not update character inventory state.")
            return {'CANCELLED'}

        source_roots = _get_inventory_source_roots_for_rig(armature, rig_settings)
        prepared_context = {"source_roots": source_roots} if source_roots else None
        with _preserve_selection(context):
            _remove_inventory_slots_for_preset_apply(rig_settings)
            try:
                import_entity._apply_inventory_mounts(
                    context,
                    armature,
                    None,
                    rig_settings,
                    entity=entity_data,
                    shared_inventory=True,
                    prepared_context=prepared_context,
                )
            except Exception:
                log.warning("Failed to apply inventory preset", exc_info=True)
                self.report({'WARNING'}, "Inventory preset apply failed.")
                return {'CANCELLED'}

        sync_equipment_slots_to_temp(context, rig_settings)
        sync_inventory_entries_to_temp(context, rig_settings, entity_data=entity_data)
        if temp_data is not None:
            temp_data.inventory_preset_id = str(preset.get("id", "") or "")
        if context.area:
            context.area.tag_redraw()
        self.report({'INFO'}, f"Applied inventory preset: {preset.get('name', '')}")
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
            entry.import_default_item = item_name
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


def _get_selected_appearance_data(entity_data, rig_settings):
    if entity_data is None or rig_settings is None:
        return None
    try:
        app_index = int(getattr(rig_settings, "app_list_index", -1))
    except Exception:
        app_index = -1
    if app_index < 0:
        return None
    appearances = entity_data.get("appearances", []) if isinstance(entity_data, dict) else getattr(entity_data, "appearances", [])
    if 0 <= app_index < len(appearances):
        return appearances[app_index]
    return None


def _get_inventory_defs(source):
    if not source:
        return []
    try:
        return import_entity._get_entry_attr(source, "inventoryDefinitions", []) or []
    except Exception:
        if isinstance(source, dict):
            return source.get("inventoryDefinitions", []) or []
        return getattr(source, "inventoryDefinitions", []) or []


def _get_inventory_def_entries(inv_def):
    if not inv_def:
        return []
    try:
        return import_entity._get_entry_attr(inv_def, "entries", []) or []
    except Exception:
        if isinstance(inv_def, dict):
            return inv_def.get("entries", []) or []
        return getattr(inv_def, "entries", []) or []


def _iter_inventory_entries_for_ui(selected_appearance, entity_data):
    def _source_name(source, fallback):
        try:
            name = import_entity._get_entry_attr(source, "name", "") or ""
        except Exception:
            name = ""
        return name or fallback

    for source, label in (
        (selected_appearance, _source_name(selected_appearance, "Appearance")),
        (entity_data, "Entity"),
    ):
        for inv_def in _get_inventory_defs(source):
            for entry in _get_inventory_def_entries(inv_def):
                yield label, entry


def _inventory_item_match_keys(*values):
    keys = set()
    for value in values:
        if not value:
            continue
        try:
            for key in import_entity._candidate_item_keys(value):
                if key:
                    keys.add(key)
        except Exception:
            pass
        try:
            key = import_entity._normalize_key(value)
        except Exception:
            key = str(value).strip().lower()
        if key:
            keys.add(key)
    return keys


def _resolve_inventory_item_for_ui(
    category,
    item_name,
    source_roots=None,
    item_lookup=None,
    template_lookup=None,
    category_items=None,
):
    resolved_category = ""
    resolved_item_name = ""
    resolved_template = ""
    try:
        if item_lookup is None or template_lookup is None:
            item_lookup, template_lookup = import_entity._build_equipment_lookup(source_roots)
        resolved = import_entity._resolve_inventory_item(item_name, item_lookup, template_lookup)
    except Exception:
        resolved = None
    if resolved:
        resolved_category, resolved_item_name, resolved_template = resolved

    if not resolved_template and category:
        if category_items is None:
            try:
                category_items, _item_attributes = get_equipment_catalog_for_search_roots(source_roots)
            except Exception:
                category_items = {}
        wanted = import_entity._normalize_key(item_name)
        for name, _display, tmpl in category_items.get(category, []):
            if import_entity._normalize_key(name) == wanted:
                resolved_category = resolved_category or category
                resolved_item_name = resolved_item_name or name
                resolved_template = tmpl or ""
                break

    if not resolved_template:
        try:
            resolved_template = import_entity._derive_template_from_item(item_name)
        except Exception:
            resolved_template = ""

    return resolved_category, resolved_item_name, resolved_template


def _get_inventory_source_roots_for_rig(armature, rig_settings):
    try:
        source_roots = import_entity._get_armature_source_roots(armature) if armature else []
    except Exception:
        source_roots = []
    if not source_roots and rig_settings is not None:
        repo_path_hint = getattr(rig_settings, "repo_path", "") or ""
        if repo_path_hint and os.path.isabs(repo_path_hint):
            try:
                source_roots = import_entity._build_entity_source_roots(repo_path_hint)
            except Exception:
                source_roots = []
    return source_roots


def _equipment_slot_has_persistent_override(slot):
    if slot is None or not getattr(slot, "keep_across_appearances", False):
        return False
    item_name = str(getattr(slot, "item_name", "") or "").strip().lower()
    equip_template = str(getattr(slot, "equip_template", "") or "").strip().lower()
    return (bool(item_name) and item_name != "none") or (bool(equip_template) and equip_template != "none")


def _resolve_appearance_equipment_item_for_ui(
    entry_data,
    rig_settings,
    category_val,
    source_roots,
    category_items,
    item_attributes,
    item_lookup=None,
    template_lookup=None,
):
    item_name = import_entity.get_equipment_entry_item_name(entry_data, rig_settings) or "None"
    equip_template = import_entity._get_entry_attr(entry_data, "equip_template", "") or ""
    resolved_item_name = ""

    if item_name and item_name != "None":
        try:
            if item_lookup is None or template_lookup is None:
                item_lookup, template_lookup = import_entity._build_equipment_lookup(source_roots)
            resolved = import_entity._resolve_inventory_item(item_name, item_lookup, template_lookup)
        except Exception:
            resolved = None
        if resolved:
            resolved_category, resolved_item_name, resolved_template = resolved
            if resolved_category and (not category_val or category_val == "None"):
                category_val = resolved_category
            if resolved_item_name:
                item_name = resolved_item_name
            if not equip_template:
                equip_template = resolved_template or ""

    if not equip_template and item_name and item_name != "None":
        item_key = import_entity._normalize_key(item_name)
        for name, _display, tmpl in category_items.get(category_val, []):
            if import_entity._normalize_key(name) == item_key:
                resolved_item_name = resolved_item_name or name
                equip_template = tmpl or ""
                break

    if not equip_template and item_name and item_name != "None":
        _resolved_category, resolved_item, resolved_template = _resolve_inventory_item_for_ui(
            category_val,
            item_name,
            source_roots,
            item_lookup=item_lookup,
            template_lookup=template_lookup,
            category_items=category_items,
        )
        if resolved_item:
            item_name = resolved_item
        equip_template = resolved_template or ""

    if not equip_template and item_name and item_name != "None":
        equip_template = item_name

    attrs = {}
    if item_name and item_name != "None":
        attrs = item_attributes.get(item_name, {}) or item_attributes.get(resolved_item_name, {}) or {}
    return category_val, item_name, equip_template, attrs


def _apply_attrs_to_equipment_slot(slot, attrs):
    if not attrs:
        return
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


def _rebuild_appearance_equipment_slots(context, armature, rig_settings):
    if rig_settings is None:
        return 0
    _entity, entity_data = import_entity.get_rig_entity_state(rig_settings)
    selected_appearance = _get_selected_appearance_data(entity_data, rig_settings)
    if selected_appearance is None:
        return 0

    try:
        appearance_params = import_entity._get_entry_attr(selected_appearance, "appearanceParams", []) or []
        first_param = appearance_params[0] if appearance_params else None
        equipment_entries_data = import_entity._get_entry_attr(first_param, "entries", []) or []
    except Exception:
        equipment_entries_data = []
    if not equipment_entries_data:
        return 0

    for i in reversed(range(len(rig_settings.equipment_slots))):
        slot = rig_settings.equipment_slots[i]
        if getattr(slot, "is_inventory", False) or _equipment_slot_has_persistent_override(slot):
            continue
        try:
            unload_equipment_item(slot)
        except Exception:
            pass
        rig_settings.equipment_slots.remove(i)

    source_roots = _get_inventory_source_roots_for_rig(armature, rig_settings)
    try:
        source_game = _infer_source_game_from_rig_settings(rig_settings, armature)
    except Exception:
        source_game = "w3"
    try:
        item_lookup, template_lookup = import_entity._build_equipment_lookup(source_roots)
    except Exception:
        item_lookup, template_lookup = {}, {}
    try:
        category_items, item_attributes = get_equipment_catalog_for_search_roots(source_roots)
    except Exception:
        category_items, item_attributes = _get_equipment_catalog(source_game)

    protected_categories = {
        slot.category for slot in rig_settings.equipment_slots
        if slot.category and (
            getattr(slot, "is_inventory", False)
            or _equipment_slot_has_persistent_override(slot)
        )
    }
    slots_to_load = []
    for entry_data in equipment_entries_data:
        category_val = import_entity._get_entry_attr(entry_data, "category", "") or ""
        if category_val and category_val in protected_categories:
            continue

        category_val, item_name, equip_template, attrs = _resolve_appearance_equipment_item_for_ui(
            entry_data,
            rig_settings,
            category_val,
            source_roots,
            category_items,
            item_attributes,
            item_lookup=item_lookup,
            template_lookup=template_lookup,
        )

        slot = rig_settings.equipment_slots.add()
        slot_index = len(rig_settings.equipment_slots) - 1
        slot.source_game = source_game
        slot.category = category_val
        slot.item_name = item_name if item_name != "None" else ""
        slot.equip_template = equip_template or ""
        slot.base_equip_template = equip_template or ""
        slot.resolved_repo_path = ""
        slot.is_inventory = False
        slot.keep_across_appearances = False
        _apply_attrs_to_equipment_slot(slot, attrs)

        if not slot.category:
            fallback = import_entity._derive_template_from_item(item_name) or item_name or f"slot_{slot_index}"
            slot.category = fallback

        if equip_template and equip_template != "None":
            slots_to_load.append(slot_index)

    entity_obj, appearance_obj = _get_entity_and_appearance(rig_settings)
    prepared_context = {
        "entity": entity_obj or _entity or entity_data,
        "appearance": appearance_obj or selected_appearance,
        "source_roots": source_roots,
    }

    import_entity._apply_inventory_mounts(
        context,
        armature,
        appearance_obj or selected_appearance,
        rig_settings,
        entity=entity_obj or _entity or entity_data,
        shared_inventory=True,
        prepared_context=prepared_context,
        post_refresh=not slots_to_load,
    )

    loaded = 0
    if slots_to_load and armature:
        try:
            refresh_slot_constraints(armature)
        except Exception:
            pass
        loaded = load_equipment_items_batch(
            context,
            armature,
            slots_to_load,
            rig_settings,
            prepared_context=prepared_context,
            post_refresh_variants=True,
            mount_mode=None,
        )

    try:
        refresh_variant_states(rig_settings)
    except Exception:
        pass
    return loaded


def _find_inventory_slot_index(
    rig_settings,
    category,
    item_name,
    resolved_category="",
    resolved_item_name="",
    equip_template="",
    allow_category_fallback=False,
):
    if rig_settings is None:
        return -1
    category_keys = _inventory_item_match_keys(category, resolved_category)
    item_keys = _inventory_item_match_keys(item_name, resolved_item_name, equip_template)
    best_category_match = -1
    for idx, slot in enumerate(rig_settings.equipment_slots):
        if not getattr(slot, "is_inventory", False):
            continue
        slot_category_key = import_entity._normalize_key(getattr(slot, "category", ""))
        slot_item_keys = _inventory_item_match_keys(
            getattr(slot, "item_name", ""),
            getattr(slot, "equip_template", ""),
            getattr(slot, "base_equip_template", ""),
        )
        category_matches = not category_keys or slot_category_key in category_keys
        item_matches = bool(item_keys and slot_item_keys.intersection(item_keys))
        if category_matches and item_matches:
            return idx
        if best_category_match < 0 and category_matches:
            best_category_match = idx
        if item_matches and not category_keys:
            return idx
    return best_category_match if (allow_category_fallback or not item_keys) else -1


def _get_inventory_mount_overrides_for_write(rig_settings):
    try:
        overrides = import_entity._get_inventory_mount_overrides(rig_settings)
    except Exception:
        overrides = {}
    return dict(overrides) if isinstance(overrides, dict) else {}


def _set_inventory_mount_override(rig_settings, category, item_name, mounted, source_mounted=False):
    if rig_settings is None:
        return
    try:
        key = import_entity._inventory_mount_override_key(category, item_name)
    except Exception:
        key = ""
    if not key:
        return
    overrides = _get_inventory_mount_overrides_for_write(rig_settings)
    if bool(mounted) == bool(source_mounted):
        overrides.pop(key, None)
    else:
        overrides[key] = bool(mounted)
    try:
        rig_settings.inventory_mount_overrides_json = json.dumps(overrides, sort_keys=True)
    except Exception:
        rig_settings.inventory_mount_overrides_json = "{}"


def sync_inventory_entries_to_temp(context, rig_settings, entity_data=None):
    try:
        temp_data = context.window_manager.witcherui_temp_data
    except Exception:
        return
    if rig_settings is None:
        temp_data.inventory_entries.clear()
        temp_data.inventory_entries_index = -1
        return

    if entity_data is None:
        _entity, entity_data = import_entity.get_rig_entity_state(rig_settings)
    selected_appearance = _get_selected_appearance_data(entity_data, rig_settings)
    try:
        armature, _active_rig_settings = _get_armature_and_rig_settings(context)
    except Exception:
        armature = None
    source_roots = _get_inventory_source_roots_for_rig(armature, rig_settings)
    try:
        source_game = _infer_source_game_from_rig_settings(rig_settings, armature)
    except Exception:
        source_game = "w3"
    try:
        item_lookup, template_lookup = import_entity._build_equipment_lookup(source_roots)
    except Exception:
        item_lookup, template_lookup = {}, {}
    try:
        category_items, _item_attributes = get_equipment_catalog_for_search_roots(source_roots)
    except Exception:
        category_items, _item_attributes = _get_equipment_catalog(source_game)

    old_key = ""
    try:
        if 0 <= temp_data.inventory_entries_index < len(temp_data.inventory_entries):
            old_key = temp_data.inventory_entries[temp_data.inventory_entries_index].entry_key
    except Exception:
        old_key = ""

    temp_data.inventory_entries.clear()
    occurrence_counts = {}
    selected_index = -1

    for source_label, inv_entry in _iter_inventory_entries_for_ui(selected_appearance, entity_data):
        try:
            category = import_entity._get_inventory_category(inv_entry)
            item_name = import_entity._get_inventory_item_name(inv_entry)
        except Exception:
            category = ""
            item_name = ""
        base_key = import_entity._inventory_mount_override_key(category, item_name)
        occurrence = occurrence_counts.get(base_key, 0) + 1
        occurrence_counts[base_key] = occurrence
        entry_key = f"{base_key}#{occurrence}"

        resolved_category, resolved_item_name, equip_template = _resolve_inventory_item_for_ui(
            category,
            item_name,
            source_roots,
            item_lookup=item_lookup,
            template_lookup=template_lookup,
            category_items=category_items,
        )
        slot_index = _find_inventory_slot_index(
            rig_settings,
            category,
            item_name,
            resolved_category=resolved_category,
            resolved_item_name=resolved_item_name,
            equip_template=equip_template,
        )
        source_is_mount = import_entity._inventory_entry_is_mount(inv_entry)
        override = import_entity._get_inventory_mount_override(rig_settings, category, item_name)
        if override is None:
            is_mount = bool(source_is_mount or slot_index >= 0)
        else:
            is_mount = bool(override)

        row = temp_data.inventory_entries.add()
        row.source_game = source_game
        row.entry_key = entry_key
        row.source_label = source_label
        row.category = category or ""
        row.item_name = item_name or ""
        row.resolved_item_name = resolved_item_name or ""
        row.equip_template = equip_template or ""
        row.source_is_mount = bool(source_is_mount)
        row.is_mount = bool(is_mount)
        row.slot_index = int(slot_index)
        row.is_loaded = bool(
            0 <= slot_index < len(rig_settings.equipment_slots)
            and getattr(rig_settings.equipment_slots[slot_index], "is_loaded", False)
        )
        try:
            row.quantity = int(import_entity._get_entry_attr(inv_entry, "quantity", 0) or 0)
            row.quantity_min = int(import_entity._get_entry_attr(inv_entry, "quantityMin", 0) or 0)
            row.quantity_max = int(import_entity._get_entry_attr(inv_entry, "quantityMax", 0) or 0)
            row.probability = float(import_entity._get_entry_attr(inv_entry, "probability", 0.0) or 0.0)
            row.is_lootable = bool(import_entity._get_entry_attr(inv_entry, "isLootable", False))
        except Exception:
            pass
        if entry_key == old_key:
            selected_index = len(temp_data.inventory_entries) - 1

    if len(temp_data.inventory_entries) == 0:
        temp_data.inventory_entries_index = -1
    elif selected_index >= 0:
        temp_data.inventory_entries_index = selected_index
    else:
        temp_data.inventory_entries_index = min(max(0, temp_data.inventory_entries_index), len(temp_data.inventory_entries) - 1)


def _get_appearance_equipment_entry_for_category(entity_data, rig_settings, category):
    selected_appearance = _get_selected_appearance_data(entity_data, rig_settings)
    if selected_appearance is None:
        return None
    try:
        appearance_params = import_entity._get_entry_attr(selected_appearance, "appearanceParams", []) or []
    except Exception:
        appearance_params = []
    if not appearance_params:
        return None
    try:
        entries = import_entity._get_entry_attr(appearance_params[0], "entries", []) or []
    except Exception:
        entries = []
    category_key = import_entity._normalize_key(category)
    for entry in entries:
        try:
            entry_category = import_entity._get_entry_attr(entry, "category", "") or ""
        except Exception:
            entry_category = ""
        if import_entity._normalize_key(entry_category) == category_key:
            return entry
    return None


def _restore_appearance_equipment_slot_for_category(context, armature, rig_settings, category):
    if rig_settings is None or not category:
        return False
    _entity, entity_data = import_entity.get_rig_entity_state(rig_settings)
    entry_data = _get_appearance_equipment_entry_for_category(entity_data, rig_settings, category)
    if entry_data is None:
        return False

    category_val = import_entity._get_entry_attr(entry_data, "category", "") or category
    item_name = import_entity.get_equipment_entry_item_name(entry_data, rig_settings) or "None"
    category_key = import_entity._normalize_key(category_val)
    for slot in rig_settings.equipment_slots:
        if getattr(slot, "is_inventory", False):
            continue
        if import_entity._normalize_key(getattr(slot, "category", "")) == category_key:
            return False

    source_roots = _get_inventory_source_roots_for_rig(armature, rig_settings)
    try:
        source_game = _infer_source_game_from_rig_settings(rig_settings, armature)
    except Exception:
        source_game = "w3"
    try:
        category_items, item_attributes = get_equipment_catalog_for_search_roots(source_roots)
    except Exception:
        category_items, item_attributes = _get_equipment_catalog(source_game)

    equip_template = import_entity._get_entry_attr(entry_data, "equip_template", "") or ""
    if not equip_template and item_name and item_name != "None":
        item_key = import_entity._normalize_key(item_name)
        for name, _display, tmpl in category_items.get(category_val, []):
            if import_entity._normalize_key(name) == item_key:
                equip_template = tmpl or ""
                break
    if not equip_template and item_name and item_name != "None":
        _resolved_category, resolved_item_name, resolved_template = _resolve_inventory_item_for_ui(category_val, item_name, source_roots)
        if resolved_item_name:
            item_name = resolved_item_name
        equip_template = resolved_template or ""
    if not equip_template and item_name and item_name != "None":
        equip_template = item_name

    slot = rig_settings.equipment_slots.add()
    slot_index = len(rig_settings.equipment_slots) - 1
    slot.source_game = source_game
    slot.category = category_val
    slot.item_name = item_name if item_name != "None" else ""
    slot.equip_template = equip_template or ""
    slot.base_equip_template = equip_template or ""
    slot.resolved_repo_path = ""
    slot.is_inventory = False
    slot.keep_across_appearances = False
    try:
        attrs = item_attributes.get(item_name, {})
        if attrs:
            slot.equip_slot = attrs.get('equip_slot', slot.equip_slot)
            slot.hold_slot = attrs.get('hold_slot', slot.hold_slot)
            slot.weapon = attrs.get('weapon', slot.weapon)
            slot.attachment_type = attrs.get('attachment_type', '')
            slot.variants_json = json.dumps(attrs.get('variants', []))
            slot.bound_items_json = json.dumps(attrs.get('bound_items', []))
    except Exception:
        pass

    if armature and slot.equip_template and slot.equip_template != "None":
        try:
            load_equipment_item(context, armature, slot_index, rig_settings)
        except Exception:
            log.warning("Failed to restore appearance equipment for %s", category_val, exc_info=True)
    return True


def _unmount_inventory_entry(context, armature, rig_settings, entry):
    if rig_settings is None or entry is None:
        return False
    slot_index = _find_inventory_slot_index(
        rig_settings,
        getattr(entry, "category", ""),
        getattr(entry, "item_name", ""),
        resolved_item_name=getattr(entry, "resolved_item_name", ""),
        equip_template=getattr(entry, "equip_template", ""),
        allow_category_fallback=True,
    )
    if slot_index < 0 or slot_index >= len(rig_settings.equipment_slots):
        return False
    slot = rig_settings.equipment_slots[slot_index]
    try:
        unload_equipment_item(slot)
    except Exception:
        pass
    try:
        rig_settings.equipment_slots.remove(slot_index)
    except Exception:
        return False
    return _restore_appearance_equipment_slot_for_category(context, armature, rig_settings, getattr(entry, "category", ""))


def _apply_inventory_mount_overrides(context, armature, rig_settings):
    if rig_settings is None:
        return
    _entity, entity_data = import_entity.get_rig_entity_state(rig_settings)
    selected_appearance = _get_selected_appearance_data(entity_data, rig_settings)
    source_roots = _get_inventory_source_roots_for_rig(armature, rig_settings)
    prepared_context = {"source_roots": source_roots} if source_roots else None
    import_entity._apply_inventory_mounts(
        context,
        armature,
        selected_appearance,
        rig_settings,
        entity=entity_data,
        shared_inventory=True,
        prepared_context=prepared_context,
    )


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
    _core_started = time.perf_counter()
    _resolve_started = time.perf_counter()
    final_item, export_path, _search_pattern = _resolve_bundle_item_by_template_cached(
        effective_template,
        search_roots=source_roots,
        prepared_context=prepared,
    )
    _resolve_seconds = time.perf_counter() - _resolve_started
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
    _entityparse_started = time.perf_counter()
    item_entity_for_apps = _get_cached_equipment_item_entity(export_path, prepared_context=prepared)
    _entityparse_seconds = time.perf_counter() - _entityparse_started
    if item_entity_for_apps and getattr(item_entity_for_apps, 'appearances', None):
        app_names = [getattr(a, 'name', '') for a in item_entity_for_apps.appearances]
        slot.item_appearances_json = json.dumps([n for n in app_names if n])
        _update_slot_coloring_json(slot, item_entity_for_apps)
    else:
        slot.item_appearances_json = ""
        slot.item_coloring_json = ""
    _coerce_slot_inline_ui_state(slot)

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
    _import_started = time.perf_counter()
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
            # Equipment items must go through _import_item_entity only.
            # Do NOT fall back to import_direct_entity_file — that creates a
            # standalone entity (armature + rig_settings + appearance groups)
            # which is the wrong structure for equipment and causes hard-to-
            # debug issues when world-layer import logic runs inside the
            # equipment system.
            log.warning(
                "Equipment import produced no objects for '%s'. "
                "No fallback — the item entity route is the only supported "
                "path for equipment items.",
                export_path,
            )
    except Exception as e:
        reason = f"Import failed for '{getattr(final_item, 'name', effective_template)}': {e}"
        _set_last_equipment_load_failure(armature, slot_index, reason)
        raise
    finally:
        _restore_pose_all_armatures(changed_poses)
        _restore_armature_world(armature, saved_world)
    _import_seconds = time.perf_counter() - _import_started

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
    # Use the appearance name that was actually used during import (from import_info)
    # rather than re-reading the slot EnumProperty, which may be stale for newly
    # created slots (e.g. from inventory import) before Blender updates its RNA cache.
    try:
        item_coloring_entries = getattr(item_entity_for_apps, 'coloringEntries', None) or []
        selected_app_name = str(import_info.get("selected_appearance_name", "") or "").strip()
        if not selected_app_name or selected_app_name == '__default__':
            selected_app_name = getattr(slot, 'item_appearance_name', '') or ''
        if not selected_app_name or selected_app_name == '__default__':
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

    _bound_started = time.perf_counter()
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
    _bound_seconds = time.perf_counter() - _bound_started

    if post_refresh_variants:
        try:
            _refresh_variants_and_reload(context, armature, rig_settings)
        except Exception:
            pass

    log.info(
        "[equip-profile] item '%s' core %.3fs (resolve %.3fs, entity_parse %.3fs, "
        "import %.3fs, bound_items %.3fs)",
        getattr(slot, "item_name", "") or effective_template,
        time.perf_counter() - _core_started,
        _resolve_seconds, _entityparse_seconds, _import_seconds, _bound_seconds,
    )
    _set_last_equipment_load_failure(armature, slot_index, None)
    return True


def load_equipment_item(context, armature, slot_index, rig_settings=None, mount_mode=None):
    """Load a single equipment item into the scene, tagged with GUID."""
    base_context = context or bpy.context
    # Keep isolation at the outer equipment API.  The core loader continues to
    # run through the legacy code path and does not need isolation parameters.
    if import_isolation.needs_isolation_session(base_context):
        target_collection = import_entity._get_import_target_collection(base_context)
        visible_objects = import_isolation.collect_related_hierarchy_objects(armature)
        with _preserve_selection(base_context):
            with import_isolation.isolated_import_session(
                base_context,
                target_collection,
                label=f"{getattr(armature, 'name', 'Character')}_Equipment",
                visible_objects=visible_objects,
            ) as session:
                return load_equipment_item(
                    session.context,
                    armature,
                    slot_index,
                    rig_settings=rig_settings,
                    mount_mode=mount_mode,
                )

    saved_active, saved_selection = _capture_selection_state(context)
    try:
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
    finally:
        _safe_restore_selection(saved_active, saved_selection)


def load_equipment_items_batch(context, armature, slot_indices, rig_settings=None, prepared_context=None,
                               reload_loaded=False, post_refresh_variants=True, mount_mode="auto"):
    base_context = context or bpy.context
    if import_isolation.needs_isolation_session(base_context):
        target_collection = import_entity._get_import_target_collection(base_context)
        visible_objects = import_isolation.collect_related_hierarchy_objects(armature)
        with _preserve_selection(base_context):
            with import_isolation.isolated_import_session(
                base_context,
                target_collection,
                label=f"{getattr(armature, 'name', 'Character')}_EquipmentBatch",
                visible_objects=visible_objects,
            ) as session:
                return load_equipment_items_batch(
                    session.context,
                    armature,
                    slot_indices,
                    rig_settings=rig_settings,
                    prepared_context=prepared_context,
                    reload_loaded=reload_loaded,
                    post_refresh_variants=post_refresh_variants,
                    mount_mode=mount_mode,
                )

    if rig_settings is None:
        rig_settings = armature.data.witcherui_RigSettings
    saved_active, saved_selection = _capture_selection_state(context)
    try:
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

        _batch_started = time.perf_counter()
        _prep_started = time.perf_counter()
        try:
            refresh_variant_states(rig_settings)
        except Exception:
            pass

        prepared = _prepare_equipment_load_context(armature, rig_settings, prepared_context)
        _prep_seconds = time.perf_counter() - _prep_started
        loaded = 0
        for idx in unique_indices:
            slot = slots[idx]
            if slot.is_loaded and slot.equip_guid and not reload_loaded:
                continue
            slot_policy = _resolve_slot_visual_policy(slot, armature, rig_settings)
            requested_mount_mode = _get_slot_requested_mount_mode(slot, slot_policy)
            mount_mode_resolved = str(mount_mode or "").strip().lower()
            if mount_mode_resolved not in {"equip", "hold"}:
                mount_mode_resolved = requested_mount_mode
            if not _can_load_slot_for_mount_mode(slot_policy, mount_mode_resolved):
                continue
            if _load_equipment_item_core(
                context,
                armature,
                idx,
                rig_settings=rig_settings,
                prepared_context=prepared,
                refresh_variants_before_load=False,
                post_refresh_variants=False,
                mount_mode=mount_mode_resolved,
            ):
                loaded += 1

        _reload_started = time.perf_counter()
        if post_refresh_variants:
            try:
                _refresh_variants_and_reload(context, armature, rig_settings)
            except Exception:
                pass
        _reload_seconds = time.perf_counter() - _reload_started
        log.info(
            "[equip-profile] load_equipment_items_batch total %.3fs (prep %.3fs, post_reload %.3fs, "
            "requested %d, loaded %d)",
            time.perf_counter() - _batch_started, _prep_seconds, _reload_seconds,
            len(unique_indices), loaded,
        )
        return loaded
    finally:
        _safe_restore_selection(saved_active, saved_selection)


def unload_equipment_item(slot):
    """Unload a single equipment item by removing its GUID-tagged objects.

    Handles edge cases:
    - Slot claims loaded but GUID is empty → just clear the flag.
    - GUID set but no matching objects in scene → clear stale state.
    - Normal case → remove objects and clear state.
    """
    if not getattr(slot, "is_loaded", False):
        return 0

    guid = getattr(slot, "equip_guid", "")
    count = 0
    if guid:
        count = remove_objects_by_guid(guid, "witcher_equip_guid")
        if count == 0:
            log.debug(
                "unload_equipment_item: no objects found for GUID '%s' (%s/%s) — clearing stale state.",
                guid,
                getattr(slot, "category", "?"),
                getattr(slot, "item_name", "?"),
            )

    slot.equip_guid = ""
    slot.is_loaded = False
    slot.is_in_hold_slot = False
    slot.show_item_appearance_ui = False
    slot.show_item_coloring_ui = False
    return count


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
    base_context = context or bpy.context
    if import_isolation.needs_isolation_session(base_context):
        target_collection = import_entity._get_import_target_collection(base_context)
        visible_objects = import_isolation.collect_related_hierarchy_objects(armature)
        with _preserve_selection(base_context):
            with import_isolation.isolated_import_session(
                base_context,
                target_collection,
                label=f"{getattr(armature, 'name', 'Character')}_Template",
                visible_objects=visible_objects,
            ) as session:
                return load_template_item(session.context, armature, slot_index, rig_settings=rig_settings)

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
    if not getattr(slot, "is_loaded", False):
        return 0

    guid = getattr(slot, "template_guid", "")
    count = 0
    if guid:
        count = remove_objects_by_guid(guid, "witcher_template_guid")

    slot.template_guid = ""
    slot.is_loaded = False
    slot.is_hidden = False
    return count


# =============================================================================
# Equipment Load/Unload Operators
# =============================================================================

class EQUIPMENT_OT_LoadDisabled(bpy.types.Operator):
    bl_idname = "witcher.equipment_load_disabled"
    bl_label = "Load Equipment (Unavailable)"
    bl_description = "Item cannot be loaded"
    bl_options = {'INTERNAL'}

    reason: bpy.props.StringProperty(name="Reason", default="")

    @classmethod
    def description(cls, context, properties):
        return properties.reason if getattr(properties, "reason", "") else "Cannot load equipment."
    
    @classmethod
    def poll(cls, context):
        return False

    def execute(self, context):
        return {'CANCELLED'}

class EQUIPMENT_OT_LoadEquipment(bpy.types.Operator):
    """Load equipment item(s) from bundles and attach to armature"""
    bl_idname = "witcher.equipment_load_equipment"
    bl_label = "Load Equipment"
    bl_options = {'REGISTER', 'UNDO'}

    slot_index: bpy.props.IntProperty(default=-1, description="Slot index (-1 = all)")
    mount_mode: bpy.props.StringProperty(default="auto", description="Mount mode override: auto, equip, or hold")

    def execute(self, context):
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
        with _preserve_selection(context):
            with mod_loading_context(context):
                if self.slot_index == -1:
                    for i in range(len(slots)):
                        slot_policy = _resolve_slot_visual_policy(slots[i], armature, rig_settings)
                        mount_mode_resolved = str(self.mount_mode or "").strip().lower()
                        if mount_mode_resolved not in {"equip", "hold"}:
                            mount_mode_resolved = _get_slot_requested_mount_mode(slots[i], slot_policy)
                        if not _can_load_slot_for_mount_mode(slot_policy, mount_mode_resolved):
                            continue
                        if load_equipment_item(context, armature, i, rig_settings, mount_mode=mount_mode_resolved):
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
                        slot = slots[self.slot_index]
                        slot_policy = _resolve_slot_visual_policy(slot, armature, rig_settings)
                        mount_mode_resolved = str(self.mount_mode or "").strip().lower()
                        if mount_mode_resolved not in {"equip", "hold"}:
                            mount_mode_resolved = _get_slot_requested_mount_mode(slot, slot_policy)
                        if load_equipment_item(context, armature, self.slot_index, rig_settings, mount_mode=mount_mode_resolved):
                            loaded += 1
                        else:
                            failed += 1
                            reason = _get_last_equipment_load_failure(armature, self.slot_index) or "Unknown failure"
                            failed_details.append(
                                f"[{self.slot_index}] {getattr(slot, 'item_name', '') or '<no item>'} | "
                                f"{get_effective_equip_template(slot) or getattr(slot, 'equip_template', '') or '<no template>'}: {reason}"
                            )

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
        armature, rig_settings = _get_armature_and_rig_settings(context)
        if not armature:
            self.report({'WARNING'}, "No valid armature selected.")
            return {'CANCELLED'}

        slots = rig_settings.equipment_slots
        removed = 0
        with _preserve_selection(context):
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


class EQUIPMENT_OT_SetMasterAppearance(bpy.types.Operator):
    """Apply the selected master appearance to all capable equipment slots"""
    bl_idname = "witcher.equipment_set_master_appearance"
    bl_label = "Set Master Appearance"

    def execute(self, context):
        armature, rig_settings = _get_armature_and_rig_settings(context)
        if not armature:
            return {'CANCELLED'}

        app_name = rig_settings.master_equipment_appearance
        if app_name == "NONE" or not app_name:
            return {'FINISHED'}

        changed_slots = []
        _set_temp_equipment_auto_apply_suspended(context, True)
        try:
            for i, slot in enumerate(rig_settings.equipment_slots):
                app_names = _get_slot_item_appearance_names(slot)
                if app_name != "__default__" and app_name not in app_names:
                    continue
                if slot.item_appearance_name != app_name:
                    slot.item_appearance_name = app_name
                    if slot.is_loaded and app_names:
                        changed_slots.append(i)
        finally:
            _set_temp_equipment_auto_apply_suspended(context, False)

        if changed_slots:
            with _preserve_selection(context):
                prepared = _prepare_equipment_load_context(armature, rig_settings, None)
                reload_slots = []
                for slot_index in changed_slots:
                    updated_in_place = try_update_loaded_equipment_appearance_in_place(
                        context,
                        armature,
                        slot_index,
                        rig_settings,
                        prepared_context=prepared,
                    )
                    if not updated_in_place:
                        reload_slots.append(slot_index)
                if reload_slots:
                    load_equipment_items_batch(
                        context,
                        armature,
                        reload_slots,
                        rig_settings,
                        prepared_context=prepared,
                        reload_loaded=True,
                        mount_mode="auto",
                    )

        rig_settings.master_equipment_appearance = "NONE"
        self.report({'INFO'}, f"Master Appearance set to: {app_name}")
        return {'FINISHED'}


classes = [
    EquipmentDefinitionEntry,
    IncludedTemplateEntry,
    InventoryDefinitionEntry,
    EquipmentItemPickerRow,
    EquipmentPresetPickerRow,
    WitcherUITempData,
    EQUIPMENT_UL_CategoryList,
    EQUIPMENT_UL_InventoryList,
    EQUIPMENT_UL_IncludedTemplateList,
    EQUIPMENT_OT_SearchCategory,
    EQUIPMENT_OT_PickDefaultItem,
    EQUIPMENT_OT_PickInventoryPreset,
    EQUIPMENT_OT_DeleteInventoryPreset,
    EQUIPMENT_OT_ShowInventoryPresetDetails,
    EQUIPMENT_OT_ItemPickerPage,
    EQUIPMENT_OT_PresetPickerPage,
    EQUIPMENT_OT_SearchDefaultItem,
    EQUIPMENT_OT_AddCategory,
    EQUIPMENT_OT_RemoveCategory,
    EQUIPMENT_OT_MoveCategory,
    EQUIPMENT_OT_ToggleInventoryMount,
    EQUIPMENT_OT_ToggleEquipmentInitializers,
    EQUIPMENT_OT_RefreshInventoryEntries,
    EQUIPMENT_OT_SaveInventoryPreset,
    EQUIPMENT_OT_SelectInventoryPreset,
    EQUIPMENT_OT_ClearInventoryPreset,
    EQUIPMENT_OT_ApplyInventoryPreset,
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
    EQUIPMENT_OT_LoadDisabled,
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
    EQUIPMENT_OT_ValidateEquipment,
    EQUIPMENT_OT_SetMasterAppearance,
    EQUIPMENT_PT_MainPanel,
]

# Register classes and properties
def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.WindowManager.witcherui_temp_data = bpy.props.PointerProperty(type=WitcherUITempData)

    # Load cached categories on startup
    _load_category_cache()
    _register_equipment_load_handler()
    _schedule_deferred_equipment_repair()

def unregister():
    _unregister_equipment_load_handler()
    _clear_equipment_item_icon_cache()
    _clear_equipment_placeholder_icon()
    _clear_equipment_icon_previews()
    if hasattr(bpy.types.WindowManager, "witcherui_temp_data"):
        del bpy.types.WindowManager.witcherui_temp_data
    for c in reversed(classes):
        bpy.utils.unregister_class(c)

if __name__ == "__main__":
    register()
