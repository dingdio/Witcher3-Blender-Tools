import logging
import bpy
import bpy.utils.previews
import os
import gzip
import pickle
import json
import hashlib
import shutil
import re
from collections import Counter
from typing import Iterable

log = logging.getLogger(__name__)
from .importers import import_entity
from .CR2W.CR2W_file import CR2W_file
from .CR2W.CR2W_types import CR2W, W_CLASS
from .CR2W.witcher_cache.Bundles import LoadBundleManager
from .CR2W.witcher_cache.Bundles.BundleItem import BundleItem
from .CR2W.witcher_cache.blender_common import get_game_path
from .CR2W.witcher_cache import cache_meta
from . import get_uncook_path, get_all_addon_prefs
from .extension_paths import get_cache_root
from .CR2W.common_blender import (
    repo_file,
    win_safe_path,
    win_path_exists,
    win_path_isfile,
    win_path_getmtime,
    win_path_getsize,
)
from .CR2W.witcher_cache.TextureCache import LoadTextureManager
from .CR2W.witcher_cache.TextureCache.TextureCacheItem import TextureCacheItem

IMAGE_BROWSER_PAGE_PROP = "witcher_image_browser_current_page"
JOURNAL_BROWSER_CACHE_VERSION = 10

_BUILTIN_CHARACTER_ENTITY_MAP_FILE = "journal_entity_overrides.characters.json"
_BUILTIN_BESTIARY_ENTITY_MAP_FILE = "journal_entity_overrides.bestiary.json"
_BUILTIN_ENTITY_MAP_FILE_BY_BROWSER_KEY = {
    "CHARACTERS": _BUILTIN_CHARACTER_ENTITY_MAP_FILE,
    "BESTIARY": _BUILTIN_BESTIARY_ENTITY_MAP_FILE,
}
_ENTITY_RESOLVE_BROWSER_KEYS = frozenset(_BUILTIN_ENTITY_MAP_FILE_BY_BROWSER_KEY.keys())

JOURNAL_BROWSER_CONFIGS = {
    "BESTIARY": {
        "journal_dir": r"gameplay\journal\bestiary",
        "image_dir": r"gameplay\gui_new\textures\journal\bestiary",
    },
    "CHARACTERS": {
        "journal_dir": r"gameplay\journal\characters",
        "image_dir": r"gameplay\gui_new\textures\journal\characters",
    },
}

_JOURNAL_ENTRY_TYPES_BY_BROWSER_KEY = {
    "CHARACTERS": (
        "CJournalCharacter",
    ),
    "BESTIARY": (
        "CJournalCreature",
    ),
}
_JOURNAL_GROUP_TYPE_BY_BROWSER_KEY = {
    "CHARACTERS": "CJournalCharacterGroup",
    "BESTIARY": (
        "CJournalCreatureGroup",
        "CJournalCreatureVirtualGroup",
    ),
}
_GUID_PATTERN = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE)
_NO_GROUP_FILTER_ID = "__NO_GROUP__"
_GROUP_MISSING_FILTER_ID = "__GROUP_MISSING__"

_JOURNAL_DLC_MOUNT_CACHE = {
    "game_path": None,
    "journal_roots": {},
    "image_roots": {},
    "scanned": False,
}
_JOURNAL_METADATA_MEM_CACHE = {}
_JOURNAL_GROUP_OPTIONS_CACHE = {
    "BESTIARY": [],
    "CHARACTERS": [],
}
_JOURNAL_BROWSER_REFRESH_SERIAL = {
    "BESTIARY": 0,
    "CHARACTERS": 0,
}
_BUILTIN_JOURNAL_ENTITY_MAP_CACHE = {}


def _normalize_depot_path(path: str) -> str:
    if not path:
        return ""
    normalized = str(path).replace("/", "\\").strip()
    return normalized.strip("\\")


def _safe_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    try:
        text = str(value)
    except Exception:
        return ""
    return text.strip()


def _truncate_text(text: str, limit: int = 120) -> str:
    text = _safe_text(text)
    if not text:
        return ""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _cache_file_paths(browser_key: str):
    cache_root = get_cache_root(create=True)
    cache_dir = os.path.join(cache_root, "JournalBrowser")
    os.makedirs(cache_dir, exist_ok=True)
    cache_name = f"journal_browser_{browser_key.lower()}.pkl"
    cache_path = os.path.join(cache_dir, cache_name)
    return cache_path, cache_meta.get_meta_path(cache_path)


def _builtin_character_entity_map_path(browser_key: str = "CHARACTERS"):
    browser_key = _safe_text(browser_key).upper() or "CHARACTERS"
    file_name = _BUILTIN_ENTITY_MAP_FILE_BY_BROWSER_KEY.get(browser_key, _BUILTIN_CHARACTER_ENTITY_MAP_FILE)
    return os.path.join(os.path.dirname(__file__), "CR2W", "data", file_name)


def _file_signature_token(path: str):
    if not win_path_exists(path):
        return "missing"
    try:
        return f"{int(win_path_getmtime(path))}:{win_path_getsize(path)}"
    except Exception:
        return "unknown"


def _builtin_character_entity_map_signature_token(browser_key: str = "CHARACTERS"):
    return _file_signature_token(_builtin_character_entity_map_path(browser_key))


def _normalize_mapped_repo_path(path: str) -> str:
    path = _safe_text(path)
    if not path:
        return ""
    if os.path.isabs(path):
        return os.path.normpath(path)
    return _normalize_depot_path(path)


def _load_builtin_character_entity_map(browser_key: str = "CHARACTERS"):
    browser_key = _safe_text(browser_key).upper() or "CHARACTERS"
    path = _builtin_character_entity_map_path(browser_key)
    token = _builtin_character_entity_map_signature_token(browser_key)
    cached = _BUILTIN_JOURNAL_ENTITY_MAP_CACHE.get(browser_key)
    if cached and cached.get("token") == token:
        return dict(cached.get("data") or {})

    mapping = {}
    if win_path_exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
        except Exception:
            log.warning("Failed to read built-in journal entity overrides: %s", path, exc_info=True)
            loaded = {}

        if isinstance(loaded, dict) and isinstance(loaded.get("journals"), dict):
            loaded = loaded.get("journals")

        if isinstance(loaded, dict):
            for journal_path, repo_path in loaded.items():
                if not isinstance(journal_path, str) or not isinstance(repo_path, str):
                    continue
                normalized_journal = _normalize_depot_path(journal_path)
                normalized_repo = _normalize_mapped_repo_path(repo_path)
                if normalized_journal:
                    mapping[normalized_journal] = normalized_repo

    _BUILTIN_JOURNAL_ENTITY_MAP_CACHE[browser_key] = {
        "token": token,
        "data": dict(mapping),
    }
    return mapping


def _create_character_entity_resolver(browser_key: str = "CHARACTERS"):
    browser_key = _safe_text(browser_key).upper() or "CHARACTERS"
    builtin_map = _load_builtin_character_entity_map(browser_key)
    if builtin_map:
        log.info("%s journal entity overrides: %d built-in mappings loaded", browser_key, len(builtin_map))
    return {
        "browser_key": browser_key,
        "builtin_map": builtin_map,
        "stats": Counter(),
    }


def _resolve_character_repo_path_with_overrides(resolver: dict, journal_depot_path: str, _journal_name: str, journal_repo_path: str):
    journal_repo = _normalize_mapped_repo_path(journal_repo_path)
    if journal_repo:
        return journal_repo, "journal"

    if resolver is None:
        return "", "missing"

    journal_key = _normalize_depot_path(journal_depot_path)
    builtin_map = resolver.get("builtin_map") or {}
    if journal_key in builtin_map:
        override_repo = _normalize_mapped_repo_path(builtin_map.get(journal_key))
        if override_repo:
            resolver["stats"]["override_hits"] += 1
            return override_repo, "override"
        resolver["stats"]["override_empty"] += 1
        return "", "missing"

    resolver["stats"]["override_missing"] += 1
    return "", "missing"


def _icon_cache_dir(browser_key: str):
    cache_root = get_cache_root(create=True)
    icon_dir = os.path.join(cache_root, "JournalBrowser", "icons", browser_key.lower())
    os.makedirs(icon_dir, exist_ok=True)
    return icon_dir


def _cache_entry_icon_file(browser_key: str, source_path: str, image_depot_path: str = "", image_file: str = ""):
    source_path = _safe_text(source_path)
    if not source_path or not win_path_exists(source_path):
        return ""

    cache_key = (_safe_text(image_depot_path) or _safe_text(image_file) or os.path.basename(source_path)).lower()
    extension = os.path.splitext(source_path)[1].lower() or ".dds"
    digest = hashlib.sha1(cache_key.encode("utf-8")).hexdigest()[:20]
    cached_path = os.path.join(_icon_cache_dir(browser_key), f"{digest}{extension}")

    try:
        source_size = win_path_getsize(source_path)
        source_mtime = win_path_getmtime(source_path)
        cached_ok = False
        if win_path_exists(cached_path):
            try:
                cached_ok = (
                    win_path_getsize(cached_path) == source_size
                    and win_path_getmtime(cached_path) >= source_mtime
                )
            except Exception:
                cached_ok = False
        if not cached_ok:
            shutil.copy2(win_safe_path(source_path), win_safe_path(cached_path))
        return cached_path
    except Exception:
        log.debug("Failed to copy journal icon into cache: %s", source_path, exc_info=True)
        return source_path


def _clear_journal_browser_caches(browser_key: str | None = None):
    if browser_key:
        browser_key = browser_key.upper()
        _JOURNAL_BROWSER_REFRESH_SERIAL[browser_key] = _JOURNAL_BROWSER_REFRESH_SERIAL.get(browser_key, 0) + 1
        for mem_key in list(_JOURNAL_METADATA_MEM_CACHE.keys()):
            if isinstance(mem_key, tuple) and mem_key and mem_key[0] == browser_key:
                _JOURNAL_METADATA_MEM_CACHE.pop(mem_key, None)
        cache_path, meta_path = _cache_file_paths(browser_key)
        icon_dir = os.path.join(get_cache_root(create=True), "JournalBrowser", "icons", browser_key.lower())
        for path in (cache_path, meta_path):
            try:
                if win_path_exists(path):
                    os.remove(path)
            except Exception:
                log.debug("Failed to remove journal browser cache file: %s", path, exc_info=True)
        try:
            if win_path_exists(icon_dir):
                shutil.rmtree(win_safe_path(icon_dir), ignore_errors=True)
        except Exception:
            log.debug("Failed to remove journal browser icon cache dir: %s", icon_dir, exc_info=True)
    else:
        _JOURNAL_METADATA_MEM_CACHE.clear()
        for key in list(_JOURNAL_GROUP_OPTIONS_CACHE.keys()):
            _JOURNAL_GROUP_OPTIONS_CACHE[key] = []
        for key in JOURNAL_BROWSER_CONFIGS:
            _clear_journal_browser_caches(key)
        return

    _JOURNAL_DLC_MOUNT_CACHE["game_path"] = None
    _JOURNAL_DLC_MOUNT_CACHE["journal_roots"] = {}
    _JOURNAL_DLC_MOUNT_CACHE["image_roots"] = {}
    _JOURNAL_DLC_MOUNT_CACHE["scanned"] = False
    _JOURNAL_GROUP_OPTIONS_CACHE[browser_key] = []


def _journal_browser_signature(browser_key: str):
    browser_key = _safe_text(browser_key).upper()
    base_path = get_game_path() or ""
    roots = cache_meta.get_content_patch_dirs(base_path)
    roots.extend(cache_meta.get_dlc_dirs(base_path, vanilla_only=False, vanilla_list=[]))

    def _predicate(path: str) -> bool:
        lower = path.lower()
        return lower.endswith(".bundle") or lower.endswith(".cache") or lower.endswith(".reddlc")

    signature = cache_meta.compute_signature(cache_meta.iter_files(roots, _predicate))
    builtin_entity_map_token = ""
    if browser_key in _ENTITY_RESOLVE_BROWSER_KEYS:
        builtin_entity_map_token = _builtin_character_entity_map_signature_token(browser_key)
        mix = f"{signature.get('hash', '')}|builtin_entity_map:{builtin_entity_map_token}"
        signature["hash"] = hashlib.sha1(mix.encode("utf-8", "ignore")).hexdigest()

    source = {
        "type": "journal_browser",
        "browser_key": browser_key,
        "base_path": base_path,
        "uncook_path": _safe_text(get_uncook_path(bpy.context)),
        "roots": roots,
        "builtin_character_entity_map_token": builtin_entity_map_token,
        "version": JOURNAL_BROWSER_CACHE_VERSION,
    }
    return signature, source


def _source_info_from_depot_path(depot_path: str):
    depot_path = _normalize_depot_path(depot_path)
    lower = depot_path.lower()
    if lower.startswith("dlc\\"):
        parts = depot_path.split("\\")
        dlc_name = parts[1] if len(parts) > 1 else "unknown"
        return "DLC", dlc_name, f"DLC: {dlc_name}"
    return "BASE", "", "Base Game"


def _is_exported_depot_path(depot_path: str) -> bool:
    if not depot_path:
        return False
    try:
        return win_path_exists(repo_file(_normalize_depot_path(depot_path)))
    except Exception:
        return False


def _ensure_bundle_item_exported(bundle_item: BundleItem) -> str:
    depot_path = _normalize_depot_path(getattr(bundle_item, "name", ""))
    if not depot_path:
        return ""
    abs_path = repo_file(depot_path)
    if win_path_exists(abs_path):
        return abs_path
    export_path = os.path.join(get_uncook_path(bpy.context), depot_path)
    bundle_item.extract_to_file(export_path)
    return export_path


def _ensure_depot_path_exported(depot_path: str) -> str:
    depot_path = _normalize_depot_path(depot_path)
    if not depot_path:
        return ""
    abs_path = repo_file(depot_path)
    if win_path_exists(abs_path):
        return abs_path

    manager = LoadBundleManager()
    items = manager.find_item_by_path_name(depot_path) if hasattr(manager, "find_item_by_path_name") else None
    if not items:
        items = manager.Items.get(depot_path, None)
    if not items:
        return abs_path

    final_item = items[-1]
    export_path = os.path.join(get_uncook_path(bpy.context), _normalize_depot_path(final_item.name))
    try:
        return final_item.extract_to_file(export_path)
    except Exception:
        log.warning("Failed to extract bundle item for %s", depot_path, exc_info=True)
        return abs_path


def _property_to_string(prop) -> str:
    if not prop:
        return ""

    # Try direct string fields first.
    for attr_name in ("String", "Value", "value"):
        try:
            attr = getattr(prop, attr_name, None)
        except Exception:
            attr = None
        if isinstance(attr, str):
            return attr.strip()
        try:
            nested = getattr(attr, "String", None)
        except Exception:
            nested = None
        if isinstance(nested, str):
            return nested.strip()

    try:
        index = getattr(prop, "Index", None)
    except Exception:
        index = None
    if index is not None:
        if isinstance(index, str):
            return index.strip()
        try:
            idx_str = getattr(index, "String", None)
        except Exception:
            idx_str = None
        if isinstance(idx_str, str):
            return idx_str.strip()
        try:
            to_string = index.ToString()
            if isinstance(to_string, str):
                return to_string.strip()
        except Exception:
            pass

    try:
        to_string = prop.ToString()
        if isinstance(to_string, str):
            return to_string.strip()
    except Exception:
        pass

    strings = set()
    _collect_strings_from_cr2w_value(prop, strings, set())
    candidates = []
    for text in strings:
        text = _safe_text(text)
        if not text:
            continue
        # Prefer non-path, non-type-looking strings for descriptions.
        score = 0
        if "\\" in text or "/" in text:
            score -= 5
        if text.isdigit():
            score -= 3
        score += min(len(text), 120)
        candidates.append((score, text))
    if not candidates:
        return ""
    candidates.sort(reverse=True)
    return candidates[0][1]


def _property_to_int(prop):
    if not prop:
        return None

    for attr_name in ("Value", "value", "Index"):
        try:
            value = getattr(prop, attr_name, None)
        except Exception:
            value = None
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)

    text = _property_to_string(prop)
    if not text:
        return None
    try:
        return int(text, 10)
    except Exception:
        return None


def _guid_from_value(value, seen: set[int] | None = None) -> str:
    if value is None:
        return ""
    if seen is None:
        seen = set()

    if isinstance(value, str):
        match = _GUID_PATTERN.search(value)
        return match.group(0).lower() if match else ""

    if isinstance(value, (int, float, bool)):
        return ""

    obj_id = id(value)
    if obj_id in seen:
        return ""
    seen.add(obj_id)

    for attr_name in ("GuidString", "GUID", "guid", "Value", "value", "Index", "String"):
        try:
            attr = getattr(value, attr_name, None)
        except Exception:
            attr = None
        if attr is None or attr is value:
            continue
        guid = _guid_from_value(attr, seen)
        if guid:
            return guid

    try:
        text = str(value)
    except Exception:
        return ""
    match = _GUID_PATTERN.search(text)
    return match.group(0).lower() if match else ""


def _property_to_guid(prop) -> str:
    return _guid_from_value(prop)


def _entry_kind(entry: dict) -> str:
    kind = _safe_text(entry.get("entry_kind")).lower()
    return kind if kind in {"entry", "group"} else "entry"


def _is_group_entry(entry: dict) -> bool:
    return _entry_kind(entry) == "group"


def _is_leaf_entry(entry: dict) -> bool:
    return _entry_kind(entry) != "group"


def _group_option_id_from_guid(guid: str) -> str:
    guid = _safe_text(guid).lower()
    return f"guid:{guid}" if guid else ""


def _group_option_id_from_group_entry(entry: dict) -> str:
    guid_id = _group_option_id_from_guid(_safe_text(entry.get("guid")))
    if guid_id:
        return guid_id
    fallback_path = _normalize_depot_path(_safe_text(entry.get("journal_path"))).lower()
    return f"path:{fallback_path}" if fallback_path else ""


def _group_types_for_browser(browser_key: str):
    browser_key = _safe_text(browser_key).upper()
    value = _JOURNAL_GROUP_TYPE_BY_BROWSER_KEY.get(browser_key)
    if isinstance(value, (list, tuple, set)):
        return tuple(t for t in (_safe_text(v) for v in value) if t)
    single = _safe_text(value)
    return (single,) if single else ()


def _all_group_types():
    all_types = set()
    for key in _JOURNAL_GROUP_TYPE_BY_BROWSER_KEY:
        all_types.update(_group_types_for_browser(key))
    return all_types


def _entry_group_option_id(entry: dict) -> str:
    option_id = _safe_text(entry.get("group_option_id"))
    if option_id:
        return option_id
    return _group_option_id_from_guid(_safe_text(entry.get("group_guid")))


def _existing_group_option_ids(entries: list[dict]):
    option_ids = set()
    for entry in entries:
        if not _is_group_entry(entry):
            continue
        option_id = _safe_text(entry.get("group_option_id")) or _group_option_id_from_group_entry(entry)
        if option_id:
            option_ids.add(option_id)
    return option_ids


def _collect_group_filter_data(entries: list[dict]):
    existing_group_ids = _existing_group_option_ids(entries)
    counts = Counter()
    ungrouped_count = 0
    missing_group_count = 0
    for entry in entries:
        if not _is_leaf_entry(entry):
            continue
        option_id = _entry_group_option_id(entry)
        if not option_id:
            ungrouped_count += 1
        elif option_id in existing_group_ids:
            counts[option_id] += 1
        else:
            missing_group_count += 1

    by_id = {}
    empty_group_count = 0
    for entry in entries:
        if not _is_group_entry(entry):
            continue
        option_id = _safe_text(entry.get("group_option_id")) or _group_option_id_from_group_entry(entry)
        if not option_id:
            continue
        label = _safe_text(entry.get("group_name")) or _safe_text(entry.get("name"))
        if not label:
            label = os.path.splitext(os.path.basename(_safe_text(entry.get("journal_path"))))[0]
        count = int(counts.get(option_id, 0))
        if count <= 0:
            empty_group_count += 1
            continue
        existing = by_id.get(option_id)
        if existing is None or (not existing["label"] and label):
            by_id[option_id] = {
                "id": option_id,
                "label": label or option_id,
                "count": count,
            }

    options = list(by_id.values())
    options.sort(key=lambda item: (_safe_text(item.get("label")).lower(), _safe_text(item.get("id")).lower()))
    if ungrouped_count > 0:
        options.append({
            "id": _NO_GROUP_FILTER_ID,
            "label": "No Group",
            "count": int(ungrouped_count),
        })
    if missing_group_count > 0:
        options.append({
            "id": _GROUP_MISSING_FILTER_ID,
            "label": "Group Missing",
            "count": int(missing_group_count),
        })
    return {
        "options": options,
        "empty_group_count": int(empty_group_count),
        "ungrouped_count": int(ungrouped_count),
        "missing_group_count": int(missing_group_count),
        "grouped_count": int(sum(counts.values())),
        "existing_group_ids": existing_group_ids,
    }


def _collect_group_filter_options(entries: list[dict]):
    data = _collect_group_filter_data(entries)
    options = data.get("options")
    if isinstance(options, list):
        return options
    return []


def _update_group_filter_options_cache(browser_key: str, entries: list[dict]):
    key = _safe_text(browser_key).upper() or "BESTIARY"
    _JOURNAL_GROUP_OPTIONS_CACHE[key] = _collect_group_filter_options(entries)


def _cached_group_filter_options(browser_key: str):
    key = _safe_text(browser_key).upper() or "BESTIARY"
    cached = _JOURNAL_GROUP_OPTIONS_CACHE.get(key)
    return list(cached) if isinstance(cached, list) else []


def _journal_group_filter_items(self, context):
    items = [("ALL", "All Groups", "Show entries from all detected groups")]
    browser_key = _safe_text(getattr(self, "journal_browser_key", "")).upper() or "BESTIARY"
    return _journal_group_filter_items_for_key(browser_key, self=self, context=context)


def _journal_group_filter_items_for_key(browser_key: str, self=None, context=None):
    items = [("ALL", "All Groups", "Show entries with detected journal groups (excludes No Group and Group Missing)")]
    browser_key = _safe_text(browser_key).upper() or "BESTIARY"
    options = _cached_group_filter_options(browser_key)

    if not options:
        preview_collection = getattr(self, "preview_collection", None)
        entries = list(getattr(preview_collection, "my_previews", [])) if preview_collection is not None else []
        if entries:
            entries = [entry for entry in entries if _safe_text(entry.get("browser_key")).upper() == browser_key]
            options = _collect_group_filter_options(entries)
            _JOURNAL_GROUP_OPTIONS_CACHE[browser_key] = options

    if not options:
        for mem_key, mem_entries in _JOURNAL_METADATA_MEM_CACHE.items():
            if isinstance(mem_key, tuple) and mem_key and _safe_text(mem_key[0]).upper() == browser_key and isinstance(mem_entries, list):
                options = _collect_group_filter_options(mem_entries)
                _JOURNAL_GROUP_OPTIONS_CACHE[browser_key] = options
                break

    for option in options:
        label = _safe_text(option.get("label")) or "Unnamed Group"
        count = int(option.get("count", 0))
        if option.get("id") == _NO_GROUP_FILTER_ID:
            items.append((option["id"], label, f"{label} ({count} entries without a parent group)"))
        elif option.get("id") == _GROUP_MISSING_FILTER_ID:
            items.append((option["id"], label, f"{label} ({count} entries with a missing parent group)"))
        else:
            items.append((option["id"], label, f"{label} ({count} entries)"))
    return items


def _journal_group_filter_items_bestiary(self, context):
    return _journal_group_filter_items_for_key("BESTIARY", self=self, context=context)


def _journal_group_filter_items_characters(self, context):
    return _journal_group_filter_items_for_key("CHARACTERS", self=self, context=context)


def _extract_journal_description(journal: W_CLASS) -> str:
    candidate_names = (
        "description",
        "shortDescription",
        "longDescription",
        "text",
        "bestiaryDescription",
        "bestiaryText",
        "entryDescription",
        "tooltip",
        "fluffDescription",
    )
    for var_name in candidate_names:
        try:
            prop = journal.GetVariableByName(var_name)
        except Exception:
            prop = None
        text = _property_to_string(prop)
        if text:
            return text
    return ""


def _build_entry_tooltip(entry: dict) -> str:
    lines = []
    name = _safe_text(entry.get("name"))
    if name:
        lines.append(name)
    repo_path = _safe_text(entry.get("repo_path"))
    if repo_path:
        lines.append(f"w2ent: {repo_path}")
    else:
        lines.append("w2ent: <not resolved>")
    repo_source = _safe_text(entry.get("repo_source"))
    if repo_source and repo_source != "journal":
        lines.append(f"Entity Source: {repo_source}")
    source_label = _safe_text(entry.get("source_label"))
    if source_label:
        lines.append(f"Source: {source_label}")
    journal_path = _safe_text(entry.get("journal_path"))
    if journal_path:
        lines.append(f"Journal: {journal_path}")
    description = _safe_text(entry.get("description"))
    if description:
        lines.append("")
        lines.append(_truncate_text(description, 500))
    return "\n".join(lines)


def _journal_chunk_kind(browser_key: str, chunk_type: str) -> str:
    browser_key = _safe_text(browser_key).upper()
    chunk_type = _safe_text(chunk_type)
    if chunk_type in _JOURNAL_ENTRY_TYPES_BY_BROWSER_KEY.get(browser_key, ()):
        return "entry"
    if chunk_type in _group_types_for_browser(browser_key):
        return "group"
    return ""


def _find_journal_display_chunk(browser_key: str, cr2w_file: CR2W):
    # Prefer typed chunks for this browser mode; fall back to any chunk that has the journal display fields.
    browser_key = _safe_text(browser_key).upper() or "BESTIARY"
    preferred_types = tuple(_JOURNAL_ENTRY_TYPES_BY_BROWSER_KEY.get(browser_key, ()))
    group_types = _group_types_for_browser(browser_key)
    ordered_types = list(preferred_types)
    ordered_types.extend(group_types)

    fallback_chunk = None
    base_name_only_chunk = None
    group_chunk = None

    for chunk_type in ordered_types:
        chunks = cr2w_file.CHUNKS.GetObjectsOfType(chunk_type)
        if not chunks:
            continue
        kind = _journal_chunk_kind(browser_key, chunk_type) or "entry"
        for chunk in chunks:
            try:
                base_name = chunk.GetVariableByName("baseName")
            except Exception:
                base_name = None
            if not base_name:
                continue

            if kind == "group":
                if group_chunk is None:
                    group_chunk = (chunk, chunk_type, kind)
                continue

            try:
                image = chunk.GetVariableByName("image")
            except Exception:
                image = None
            if image:
                if chunk.GetVariableByName("entityTemplate"):
                    return chunk, chunk_type, kind
                if fallback_chunk is None:
                    fallback_chunk = (chunk, chunk_type, kind)
            elif base_name_only_chunk is None:
                base_name_only_chunk = (chunk, chunk_type, kind)

    if fallback_chunk is not None:
        return fallback_chunk
    if base_name_only_chunk is not None:
        return base_name_only_chunk
    if group_chunk is not None:
        return group_chunk

    for chunk in getattr(cr2w_file.CHUNKS, "CHUNKS", []):
        chunk_type = _safe_text(getattr(chunk, "name", ""))
        kind = _journal_chunk_kind(browser_key, chunk_type) or "entry"
        try:
            base_name = chunk.GetVariableByName("baseName")
        except Exception:
            base_name = None
        if not base_name:
            continue

        if kind == "group":
            if group_chunk is None:
                group_chunk = (chunk, chunk_type, kind)
            continue

        try:
            image = chunk.GetVariableByName("image")
        except Exception:
            image = None

        if image:
            if chunk.GetVariableByName("entityTemplate"):
                return chunk, chunk_type, kind
            if fallback_chunk is None:
                fallback_chunk = (chunk, chunk_type, kind)
        elif base_name_only_chunk is None:
            base_name_only_chunk = (chunk, chunk_type, kind)

    return fallback_chunk or base_name_only_chunk or group_chunk


def _find_journal_entity_template(cr2w_file: CR2W, preferred_chunk: W_CLASS | None = None):
    if preferred_chunk is not None:
        try:
            entity_template = preferred_chunk.GetVariableByName("entityTemplate")
        except Exception:
            entity_template = None
        if entity_template:
            return entity_template

    for chunk in getattr(cr2w_file.CHUNKS, "CHUNKS", []):
        try:
            entity_template = chunk.GetVariableByName("entityTemplate")
        except Exception:
            entity_template = None
        if entity_template:
            return entity_template
    return None


def _apply_journal_group_metadata(entries: list[dict]):
    groups_by_guid = {}
    all_group_types = _all_group_types()
    for entry in entries:
        journal_class = _safe_text(entry.get("journal_class"))
        kind = _safe_text(entry.get("entry_kind")).lower()
        if kind not in {"entry", "group"}:
            kind = "group" if journal_class in all_group_types else "entry"
            entry["entry_kind"] = kind

        guid = _safe_text(entry.get("guid")).lower()
        parent_guid = _safe_text(entry.get("parent_guid")).lower()
        entry["guid"] = guid
        entry["parent_guid"] = parent_guid

        if kind == "group":
            entry["group_guid"] = guid
            entry["group_name"] = _safe_text(entry.get("name"))
            entry["group_option_id"] = _group_option_id_from_group_entry(entry)
            if guid:
                groups_by_guid[guid] = entry

    for entry in entries:
        if _is_group_entry(entry):
            continue
        parent_guid = _safe_text(entry.get("parent_guid")).lower()
        group_entry = groups_by_guid.get(parent_guid)
        if group_entry is not None:
            entry["group_guid"] = _safe_text(group_entry.get("guid")).lower() or parent_guid
            entry["group_name"] = _safe_text(group_entry.get("name"))
            entry["group_option_id"] = _group_option_id_from_group_entry(group_entry)
        else:
            entry["group_guid"] = _safe_text(entry.get("group_guid")).lower() or parent_guid
            entry["group_name"] = _safe_text(entry.get("group_name"))
            entry["group_option_id"] = _safe_text(entry.get("group_option_id")) or _group_option_id_from_guid(entry["group_guid"])


def _entity_template_repo_path(entity_template) -> str:
    if not entity_template:
        return ""
    repo_path = ""
    try:
        repo_path = _normalize_depot_path(str(entity_template.Index))
    except Exception:
        repo_path = ""
    if repo_path in {"", "None", "0"}:
        repo_path = _property_to_string(getattr(entity_template, "Index", None)) or _property_to_string(entity_template)
    repo_path = _normalize_mapped_repo_path(repo_path)
    if not repo_path or repo_path.lower() in {"0", "none"}:
        return ""
    return repo_path


def _iter_top_level_reddlc_files(game_path: str) -> Iterable[str]:
    # .reddlc files are discovered from bundle contents (depot paths) and extracted to uncook/repo paths.
    # They are not typically present in the installed game's runtime DLC "content" folders.
    try:
        bundle_manager = LoadBundleManager()
    except Exception:
        log.warning("Failed to load bundle manager while scanning for .reddlc mounters", exc_info=True)
        return []

    top_level_reddlc_files = []
    nested_reddlc_files = []
    seen_local_paths = set()

    for key, bundle_items in bundle_manager.Items.items():
        depot_path = _normalize_depot_path(key)
        depot_lower = depot_path.lower()
        if not depot_lower.startswith("dlc\\") or not depot_lower.endswith(".reddlc"):
            continue
        if not bundle_items:
            continue

        final_item = bundle_items[-1]
        item_name = _normalize_depot_path(getattr(final_item, "name", depot_path) or depot_path)

        local_path = repo_file(item_name)
        if not win_path_exists(local_path):
            export_path = os.path.join(get_uncook_path(bpy.context), item_name)
            try:
                final_item.extract_to_file(export_path)
                local_path = export_path
            except Exception:
                log.warning("Failed to extract .reddlc from bundle item %s", item_name, exc_info=True)
                continue

        if not win_path_isfile(local_path):
            continue
        if local_path in seen_local_paths:
            continue
        seen_local_paths.add(local_path)

        path_parts = depot_path.split("\\")
        # Top-level in DLC folder is usually: dlc\<name>\<name>.reddlc
        if len(path_parts) == 3:
            top_level_reddlc_files.append(local_path)
        else:
            nested_reddlc_files.append(local_path)

    if top_level_reddlc_files:
        return top_level_reddlc_files
    return nested_reddlc_files


def _collect_strings_from_cr2w_value(value, out_strings: set[str], seen: set[int]):
    if value is None:
        return

    if isinstance(value, str):
        text = value.strip()
        if text:
            out_strings.add(text)
        return

    obj_id = id(value)
    if obj_id in seen:
        return

    if isinstance(value, (list, tuple, set)):
        seen.add(obj_id)
        for item in value:
            _collect_strings_from_cr2w_value(item, out_strings, seen)
        return

    if isinstance(value, dict):
        seen.add(obj_id)
        for item in value.values():
            _collect_strings_from_cr2w_value(item, out_strings, seen)
        return

    seen.add(obj_id)

    if hasattr(value, "ToString"):
        try:
            text = value.ToString()
            if isinstance(text, str) and text.strip():
                out_strings.add(text.strip())
        except Exception:
            pass

    for attr_name in ("String", "Value", "value", "DepotPath"):
        try:
            attr = getattr(value, attr_name, None)
        except Exception:
            attr = None
        if attr is not None and attr is not value:
            _collect_strings_from_cr2w_value(attr, out_strings, seen)

    for attr_name in ("PROPS", "More", "elements", "Handles"):
        try:
            attr = getattr(value, attr_name, None)
        except Exception:
            attr = None
        if attr:
            _collect_strings_from_cr2w_value(attr, out_strings, seen)

    try:
        index_attr = getattr(value, "Index", None)
    except Exception:
        index_attr = None
    if index_attr is not None and index_attr is not value:
        _collect_strings_from_cr2w_value(index_attr, out_strings, seen)


def _extract_mounter_search_roots_from_reddlc(reddlc_path: str):
    journal_roots = set()
    image_roots = set()
    try:
        cr2w_file: CR2W = CR2W_file.read_CR2W(reddlc_path)
    except Exception:
        log.warning("Failed to read DLC mounter file: %s", reddlc_path, exc_info=True)
        return journal_roots, image_roots

    journal_chunks = cr2w_file.CHUNKS.GetObjectsOfType("CR4JournalDLCMounter")
    scaleform_chunks = cr2w_file.CHUNKS.GetObjectsOfType("CR4ScaleformContentDLCMounter")

    journal_roots_from_strings = set()
    image_roots_from_strings = set()

    for mounter_type, chunks in (
        ("CR4JournalDLCMounter", journal_chunks),
        ("CR4ScaleformContentDLCMounter", scaleform_chunks),
    ):
        for chunk in chunks:
            strings = set()
            _collect_strings_from_cr2w_value(chunk, strings, set())
            for raw in strings:
                path = _normalize_depot_path(raw)
                if not path:
                    continue

                lower_path = path.lower()
                if (
                    "gameplay\\journal\\bestiary" in lower_path
                    or "gameplay\\journal\\characters" in lower_path
                    or lower_path.endswith("\\journal\\bestiary")
                    or lower_path.endswith("\\journal\\characters")
                ):
                    journal_roots.add(path)
                    journal_roots_from_strings.add(path)
                if (
                    "gameplay\\gui_new\\textures\\journal\\bestiary" in lower_path
                    or "gameplay\\gui_new\\textures\\journal\\characters" in lower_path
                    or lower_path.endswith("\\textures\\journal\\bestiary")
                    or lower_path.endswith("\\textures\\journal\\characters")
                ):
                    image_roots.add(path)
                    image_roots_from_strings.add(path)

                # Some mounters expose a root path, e.g. "dlc\\bob\\journal\\", not the category paths.
                normalized_for_join = path
                if mounter_type == "CR4JournalDLCMounter" and (
                    lower_path.endswith("\\journal") or lower_path.endswith("\\journal\\")
                ):
                    for category in ("bestiary", "characters"):
                        expanded = _normalize_depot_path(os.path.join(normalized_for_join, category))
                        journal_roots.add(expanded)
                        journal_roots_from_strings.add(expanded)

                if mounter_type == "CR4ScaleformContentDLCMounter" and (
                    lower_path.endswith("\\textures\\journal") or lower_path.endswith("\\textures\\journal\\")
                ):
                    for category in ("bestiary", "characters"):
                        expanded = _normalize_depot_path(os.path.join(normalized_for_join, category))
                        image_roots.add(expanded)
                        image_roots_from_strings.add(expanded)

    # Fallback: if the mounters exist but do not expose explicit category paths in easily
    # readable string fields, synthesize the mounted DLC data roots from the .reddlc location.
    dlc_name = os.path.basename(os.path.dirname(reddlc_path))
    if dlc_name:
        dlc_data_prefix = _normalize_depot_path(os.path.join("dlc", dlc_name, "data"))
        if journal_chunks and not journal_roots_from_strings:
            journal_roots.add(_normalize_depot_path(os.path.join(dlc_data_prefix, r"gameplay\journal\bestiary")))
            journal_roots.add(_normalize_depot_path(os.path.join(dlc_data_prefix, r"gameplay\journal\characters")))
        if scaleform_chunks and not image_roots_from_strings:
            image_roots.add(_normalize_depot_path(os.path.join(dlc_data_prefix, r"gameplay\gui_new\textures\journal\bestiary")))
            image_roots.add(_normalize_depot_path(os.path.join(dlc_data_prefix, r"gameplay\gui_new\textures\journal\characters")))

    return journal_roots, image_roots


def _get_dlc_mounter_search_roots():
    game_path = get_game_path() or ""
    cached_game_path = _JOURNAL_DLC_MOUNT_CACHE.get("game_path")
    cached_journal_roots = _JOURNAL_DLC_MOUNT_CACHE.get("journal_roots") or {}
    cached_image_roots = _JOURNAL_DLC_MOUNT_CACHE.get("image_roots") or {}
    cached_has_any_roots = any(cached_journal_roots.get(key) for key in ("BESTIARY", "CHARACTERS")) or any(
        cached_image_roots.get(key) for key in ("BESTIARY", "CHARACTERS")
    )
    if cached_game_path == game_path and _JOURNAL_DLC_MOUNT_CACHE.get("scanned") and cached_has_any_roots:
        return cached_journal_roots, cached_image_roots

    journal_roots = {"BESTIARY": set(), "CHARACTERS": set()}
    image_roots = {"BESTIARY": set(), "CHARACTERS": set()}
    reddlc_files = list(_iter_top_level_reddlc_files(game_path))
    log.info(
        "Journal browser: scanning %d .reddlc files from bundle exports (game path: %s)",
        len(reddlc_files),
        game_path or "<unset>",
    )

    for reddlc_path in reddlc_files:
        extra_journal_roots, extra_image_roots = _extract_mounter_search_roots_from_reddlc(reddlc_path)
        for path in extra_journal_roots:
            lower = path.lower()
            if "gameplay\\journal\\bestiary" in lower or lower.endswith("\\journal\\bestiary"):
                journal_roots["BESTIARY"].add(path)
            if "gameplay\\journal\\characters" in lower or lower.endswith("\\journal\\characters"):
                journal_roots["CHARACTERS"].add(path)
        for path in extra_image_roots:
            lower = path.lower()
            if "gameplay\\gui_new\\textures\\journal\\bestiary" in lower or lower.endswith("\\textures\\journal\\bestiary"):
                image_roots["BESTIARY"].add(path)
            if "gameplay\\gui_new\\textures\\journal\\characters" in lower or lower.endswith("\\textures\\journal\\characters"):
                image_roots["CHARACTERS"].add(path)

    _JOURNAL_DLC_MOUNT_CACHE["game_path"] = game_path
    _JOURNAL_DLC_MOUNT_CACHE["journal_roots"] = journal_roots
    _JOURNAL_DLC_MOUNT_CACHE["image_roots"] = image_roots
    _JOURNAL_DLC_MOUNT_CACHE["scanned"] = True
    log.info(
        "Journal browser DLC mounter roots: %d bestiary journals, %d character journals, %d bestiary images, %d character images",
        len(journal_roots["BESTIARY"]),
        len(journal_roots["CHARACTERS"]),
        len(image_roots["BESTIARY"]),
        len(image_roots["CHARACTERS"]),
    )
    return journal_roots, image_roots


def _get_browser_search_roots(browser_key: str):
    config = JOURNAL_BROWSER_CONFIGS[browser_key]
    journal_roots = [config["journal_dir"]]
    image_roots = [config["image_dir"]]

    dlc_journal_roots, dlc_image_roots = _get_dlc_mounter_search_roots()
    journal_roots.extend(sorted(dlc_journal_roots.get(browser_key, ())))
    image_roots.extend(sorted(dlc_image_roots.get(browser_key, ())))

    # Keep order stable while removing duplicates.
    journal_roots = list(dict.fromkeys(_normalize_depot_path(p) for p in journal_roots if p))
    image_roots = list(dict.fromkeys(_normalize_depot_path(p) for p in image_roots if p))
    return journal_roots, image_roots


def _iter_manager_values_for_prefixes(manager_items, prefixes: list[str]):
    for key, value in manager_items.items():
        normalized_key = _normalize_depot_path(key).lower()
        if any(normalized_key.startswith(prefix.lower()) for prefix in prefixes):
            yield value


def _resolve_image_path_from_roots(image_roots: list[str], image_file: str):
    normalized_image = _normalize_depot_path(image_file)
    if "\\" in normalized_image:
        direct_path = repo_file(normalized_image)
        if win_path_exists(direct_path):
            return direct_path, normalized_image

    for image_root in image_roots:
        image_directory = repo_file(image_root)
        filepath = os.path.join(image_directory, image_file)
        if win_path_exists(filepath):
            return filepath, _normalize_depot_path(os.path.join(image_root, image_file))
    return None, ""


def _ensure_texture_roots_exported(image_dirs: list[str]):
    # Ensure journal textures from all mounted roots are exported once before resolving icon files.
    texture_manager = LoadTextureManager()
    texture_values = _iter_manager_values_for_prefixes(texture_manager.Items, image_dirs)
    for tex_items in texture_values:
        if not tex_items:
            continue
        final_item: TextureCacheItem = tex_items[-1]
        export_path = os.path.join(get_uncook_path(bpy.context), final_item.name)
        if not win_path_exists(export_path) and not win_path_exists(export_path.rsplit('.', 1)[0] + '.dds'):
            final_item.extract_to_file(export_path)


def _build_journal_entry_from_bundle_item(
    browser_key: str,
    final_bundle_item: BundleItem,
    image_dirs: list[str],
    entity_resolver: dict | None,
    stats: Counter,
):
    journal_depot_path = _normalize_depot_path(getattr(final_bundle_item, "name", ""))
    if not journal_depot_path:
        stats["invalid_journal_path"] += 1
        return None

    try:
        item_abs_path = _ensure_bundle_item_exported(final_bundle_item)
    except Exception:
        stats["journal_extract_fail"] += 1
        log.warning("Failed to extract journal entry %s", journal_depot_path, exc_info=True)
        return None

    try:
        cr2w_file: CR2W = CR2W_file.read_CR2W(item_abs_path)
        display_info = _find_journal_display_chunk(browser_key, cr2w_file)
        if not display_info:
            stats["no_display_chunk"] += 1
            return None

        journal, journal_class, journal_kind = display_info
        name = _property_to_string(journal.GetVariableByName("baseName")) or os.path.splitext(os.path.basename(journal_depot_path))[0]

        guid = _property_to_guid(journal.GetVariableByName("guid"))
        parent_guid = _property_to_guid(journal.GetVariableByName("parentGuid"))
        journal_order = _property_to_int(journal.GetVariableByName("order"))

        is_group = journal_kind == "group"
        raw_image = _property_to_string(journal.GetVariableByName("image")) if not is_group else ""
        image = (os.path.splitext(raw_image)[0] + ".dds") if raw_image else ""

        filepath = ""
        image_depot_path = ""
        repo_path = ""
        repo_source = "missing"

        if not is_group:
            entity_template = _find_journal_entity_template(cr2w_file, journal)
            if not entity_template:
                stats["missing_entity_template"] += 1

            if image and (image.lower().endswith(".dds") or image.lower().endswith(".png")):
                filepath, image_depot_path = _resolve_image_path_from_roots(image_dirs, image)
                if not filepath:
                    stats["missing_icon_file"] += 1
                    # keep entry anyway; render with placeholder icon
                else:
                    filepath = _cache_entry_icon_file(browser_key, filepath, image_depot_path, image)
            else:
                stats["missing_image_prop"] += 1

            repo_path = _entity_template_repo_path(entity_template)
            repo_source = "journal" if repo_path else "missing"
            if not repo_path:
                stats["missing_repo_path"] += 1

            if browser_key in _ENTITY_RESOLVE_BROWSER_KEYS:
                repo_path, repo_source = _resolve_character_repo_path_with_overrides(
                    entity_resolver,
                    journal_depot_path,
                    name,
                    repo_path,
                )
                if repo_source == "override":
                    stats["map_override_fallback"] += 1

        source_kind, dlc_name, source_label = _source_info_from_depot_path(journal_depot_path)
        description = _extract_journal_description(journal) if not is_group else ""

        stats["entries_added"] += 1
        if is_group:
            stats["groups_added"] += 1
        else:
            stats["leaf_entries_added"] += 1
        return {
            "name": name,
            "repo_path": repo_path,
            "journal_path": journal_depot_path,
            "image_path": filepath,
            "image_depot_path": image_depot_path,
            "image_file": image,
            "description": description,
            "description_short": _truncate_text(description, 96),
            "source_kind": source_kind,
            "dlc_name": dlc_name,
            "source_label": source_label,
            "browser_key": browser_key,
            "repo_source": repo_source,
            "can_import": bool(repo_path),
            "entry_kind": "group" if is_group else "entry",
            "journal_class": _safe_text(journal_class),
            "guid": guid,
            "parent_guid": parent_guid,
            "journal_order": journal_order,
            "group_guid": "",
            "group_name": "",
            "group_option_id": "",
        }
    except Exception:
        stats["cr2w_parse_fail"] += 1
        log.warning("Couldn't load asset browser preview from %s", item_abs_path, exc_info=True)
        return None


def _build_journal_entries(browser_key: str, journal_dirs: list[str], image_dirs: list[str]):
    browser_key = _safe_text(browser_key).upper()
    entries = []
    seen_entries = set()
    stats = Counter()
    entity_resolver = _create_character_entity_resolver(browser_key) if browser_key in _ENTITY_RESOLVE_BROWSER_KEYS else None

    _ensure_texture_roots_exported(image_dirs)

    bundle_manager = LoadBundleManager()
    bundle_values = _iter_manager_values_for_prefixes(bundle_manager.Items, journal_dirs)
    for bundle_items in bundle_values:
        stats["bundle_items_seen"] += 1
        if not bundle_items:
            stats["empty_bundle_items"] += 1
            continue
        final_bundle_item: BundleItem = bundle_items[-1]
        entry = _build_journal_entry_from_bundle_item(
            browser_key,
            final_bundle_item,
            image_dirs,
            entity_resolver,
            stats,
        )
        if not entry:
            continue
        entry_key = (
            browser_key,
            _safe_text(entry.get("name")),
            _safe_text(entry.get("repo_path")),
            _safe_text(entry.get("journal_path")),
        )
        if entry_key in seen_entries:
            stats["duplicate_entry"] += 1
            continue
        seen_entries.add(entry_key)
        entries.append(entry)

    _apply_journal_group_metadata(entries)

    entries.sort(key=lambda e: (_safe_text(e.get("name")).lower(), _safe_text(e.get("repo_path")).lower()))
    log.info(
        "Journal browser build [%s]: entries=%d leaf=%d groups=%d bundles=%d no_chunk=%d no_repo=%d map_override=%d missing_icon=%d parse_fail=%d",
        browser_key,
        stats.get("entries_added", 0),
        stats.get("leaf_entries_added", 0),
        stats.get("groups_added", 0),
        stats.get("bundle_items_seen", 0),
        stats.get("no_display_chunk", 0),
        stats.get("missing_repo_path", 0),
        stats.get("map_override_fallback", 0),
        stats.get("missing_icon_file", 0),
        stats.get("cr2w_parse_fail", 0),
    )
    return entries


def _repair_cached_entry_image_paths(browser_key: str, entries: list[dict]):
    missing_icon_entries = [
        entry for entry in entries
        if _safe_text(entry.get("image_file"))
        and not win_path_exists(_safe_text(entry.get("image_path")))
    ]
    if not missing_icon_entries:
        return 0

    try:
        _journal_dirs, image_dirs = _get_browser_search_roots(browser_key)
        _ensure_texture_roots_exported(image_dirs)
    except Exception:
        log.debug("Failed to re-export journal icon roots while repairing cache [%s]", browser_key, exc_info=True)
        return 0

    repaired = 0
    for entry in missing_icon_entries:
        image_path = ""
        image_depot_path = _safe_text(entry.get("image_depot_path"))
        if image_depot_path:
            candidate = repo_file(image_depot_path)
            if win_path_exists(candidate):
                image_path = candidate

        if not image_path:
            image_path, resolved_depot = _resolve_image_path_from_roots(image_dirs, _safe_text(entry.get("image_file")))
            if resolved_depot:
                entry["image_depot_path"] = resolved_depot

        if image_path and win_path_exists(image_path):
            entry["image_path"] = _cache_entry_icon_file(
                browser_key,
                image_path,
                _safe_text(entry.get("image_depot_path")),
                _safe_text(entry.get("image_file")),
            )
            repaired += 1

    if repaired:
        log.info(
            "Journal browser cache [%s]: repaired %d/%d missing icon paths after uncook cleanup",
            browser_key,
            repaired,
            len(missing_icon_entries),
        )
    return repaired


def _load_journal_entries_from_disk_payload(browser_key: str):
    cache_path, _meta_path = _cache_file_paths(browser_key)
    if not win_path_exists(cache_path):
        return None
    try:
        with gzip.open(cache_path, "rb") as f:
            payload = pickle.load(f)
    except Exception:
        log.warning("Failed to read journal browser cache %s", cache_path, exc_info=True)
        return None

    if not isinstance(payload, dict):
        return None
    if payload.get("version") != JOURNAL_BROWSER_CACHE_VERSION:
        return None
    entries = payload.get("entries")
    if not isinstance(entries, list):
        return None
    _apply_journal_group_metadata(entries)
    return entries


def _store_journal_entries_cache(browser_key: str, entries: list[dict], cache_label: str = "rebuilt"):
    _apply_journal_group_metadata(entries)
    cache_path, meta_path = _cache_file_paths(browser_key)
    payload = {"version": JOURNAL_BROWSER_CACHE_VERSION, "entries": entries}
    signature, source = _journal_browser_signature(browser_key)
    try:
        with gzip.open(cache_path, "wb") as f:
            pickle.dump(payload, f)
        meta = cache_meta.make_meta(os.path.basename(cache_path), cache_path, signature, source)
        cache_meta.save_meta(meta_path, meta)
    except Exception:
        log.warning("Failed to write journal browser cache %s", cache_path, exc_info=True)

    browser_key = _safe_text(browser_key).upper()
    base_path = _safe_text(source.get("base_path"))
    uncook_path = _safe_text(source.get("uncook_path"))
    for mem_key in list(_JOURNAL_METADATA_MEM_CACHE.keys()):
        if isinstance(mem_key, tuple) and len(mem_key) >= 1 and mem_key[0] == browser_key:
            _JOURNAL_METADATA_MEM_CACHE.pop(mem_key, None)
    mem_key = (browser_key, base_path, uncook_path)
    _JOURNAL_METADATA_MEM_CACHE[mem_key] = entries
    _update_group_filter_options_cache(browser_key, entries)
    return {
        "cache": cache_label,
        "signature": signature,
    }


def _current_journal_bundle_items(browser_key: str, journal_dirs: list[str]):
    browser_key = _safe_text(browser_key).upper()
    bundle_manager = LoadBundleManager()
    current = {}
    bundle_values = _iter_manager_values_for_prefixes(bundle_manager.Items, journal_dirs)
    for bundle_items in bundle_values:
        if not bundle_items:
            continue
        final_bundle_item: BundleItem = bundle_items[-1]
        journal_path = _normalize_depot_path(getattr(final_bundle_item, "name", ""))
        if not journal_path:
            continue
        current[journal_path] = final_bundle_item
    return current


def _smart_refresh_journal_cache(browser_key: str):
    browser_key = _safe_text(browser_key).upper()
    if browser_key not in JOURNAL_BROWSER_CONFIGS:
        return {"added": 0, "removed": 0, "updated": 0, "total": 0}

    try:
        LoadTextureManager()
    except Exception:
        log.debug("Texture cache load failed during smart journal refresh", exc_info=True)
    try:
        LoadBundleManager()
    except Exception:
        log.debug("Bundle cache load failed during smart journal refresh", exc_info=True)

    journal_dirs, image_dirs = _get_browser_search_roots(browser_key)
    _ensure_texture_roots_exported(image_dirs)

    existing_entries = _load_journal_entries_from_disk_payload(browser_key) or []
    existing_by_path = {
        _normalize_depot_path(_safe_text(entry.get("journal_path"))): entry
        for entry in existing_entries
        if _safe_text(entry.get("journal_path"))
    }

    current_by_path = _current_journal_bundle_items(browser_key, journal_dirs)
    existing_paths = set(existing_by_path.keys())
    current_paths = set(current_by_path.keys())

    added_paths = sorted(current_paths - existing_paths)
    removed_paths = sorted(existing_paths - current_paths)

    stats = Counter()
    entity_resolver = _create_character_entity_resolver(browser_key) if browser_key in _ENTITY_RESOLVE_BROWSER_KEYS else None

    merged_entries = []
    for journal_path in sorted(existing_paths & current_paths):
        entry = dict(existing_by_path[journal_path])
        if browser_key in _ENTITY_RESOLVE_BROWSER_KEYS and not _is_group_entry(entry):
            seed_journal_repo = _safe_text(entry.get("repo_path")) if _safe_text(entry.get("repo_source")) == "journal" else ""
            resolved_repo, resolved_source = _resolve_character_repo_path_with_overrides(
                entity_resolver,
                journal_path,
                "",
                seed_journal_repo,
            )
            entry["repo_path"] = resolved_repo
            entry["repo_source"] = resolved_source
            entry["can_import"] = bool(resolved_repo)
        merged_entries.append(entry)
    for journal_path in added_paths:
        entry = _build_journal_entry_from_bundle_item(
            browser_key,
            current_by_path[journal_path],
            image_dirs,
            entity_resolver,
            stats,
        )
        if entry:
            merged_entries.append(entry)

    _apply_journal_group_metadata(merged_entries)
    _repair_cached_entry_image_paths(browser_key, merged_entries)
    merged_entries.sort(key=lambda e: (_safe_text(e.get("name")).lower(), _safe_text(e.get("repo_path")).lower()))
    _store_journal_entries_cache(browser_key, merged_entries, cache_label="smart-refresh")

    return {
        "added": len(added_paths),
        "removed": len(removed_paths),
        "updated": stats.get("entries_added", 0),
        "total": len(merged_entries),
    }


def _load_journal_entries_cached(browser_key: str, force_refresh: bool = False):
    browser_key = browser_key.upper()
    base_path = _safe_text(get_game_path() or "")
    uncook_path = _safe_text(get_uncook_path(bpy.context))
    mem_key = (browser_key, base_path, uncook_path)

    if force_refresh:
        refresh_stats = _smart_refresh_journal_cache(browser_key)
        entries = _load_journal_entries_from_disk_payload(browser_key) or []
        _apply_journal_group_metadata(entries)
        _JOURNAL_METADATA_MEM_CACHE[mem_key] = entries
        return entries, {
            "cache": "smart-refresh",
            "refresh": refresh_stats,
        }

    if mem_key in _JOURNAL_METADATA_MEM_CACHE:
        entries = _JOURNAL_METADATA_MEM_CACHE[mem_key]
        _apply_journal_group_metadata(entries)
        _repair_cached_entry_image_paths(browser_key, entries)
        return entries, {
            "cache": "memory",
        }

    entries = _load_journal_entries_from_disk_payload(browser_key)
    if entries is not None:
        _apply_journal_group_metadata(entries)
        _repair_cached_entry_image_paths(browser_key, entries)
        _JOURNAL_METADATA_MEM_CACHE[mem_key] = entries
        return entries, {
            "cache": "disk",
        }

    log.info("Journal browser [%s]: no cache found, performing first-time build", browser_key)
    wm = getattr(bpy.context, "window_manager", None)
    if wm:
        try:
            wm.progress_begin(0, 100)
            wm.progress_update(5)
        except Exception:
            wm = None

    try:
        try:
            LoadTextureManager(do_reload=True)
        except Exception:
            log.debug("Texture cache reload failed while rebuilding journal browser cache", exc_info=True)
        if wm:
            try:
                wm.progress_update(25)
            except Exception:
                pass
        try:
            LoadBundleManager(reset_cache=True)
        except Exception:
            log.debug("Bundle cache reload failed while rebuilding journal browser cache", exc_info=True)
        if wm:
            try:
                wm.progress_update(45)
            except Exception:
                pass

        _JOURNAL_DLC_MOUNT_CACHE["game_path"] = None
        _JOURNAL_DLC_MOUNT_CACHE["journal_roots"] = {}
        _JOURNAL_DLC_MOUNT_CACHE["image_roots"] = {}
        _JOURNAL_DLC_MOUNT_CACHE["scanned"] = False

        journal_dirs, image_dirs = _get_browser_search_roots(browser_key)
        if wm:
            try:
                wm.progress_update(65)
            except Exception:
                pass
        entries = _build_journal_entries(browser_key, journal_dirs, image_dirs)
        _apply_journal_group_metadata(entries)
        if wm:
            try:
                wm.progress_update(90)
            except Exception:
                pass
        cache_info = _store_journal_entries_cache(browser_key, entries, cache_label="rebuilt")
    finally:
        if wm:
            try:
                wm.progress_end()
            except Exception:
                pass

    _JOURNAL_METADATA_MEM_CACHE[mem_key] = entries
    return entries, cache_info


def _entry_counts(entries: list[dict]):
    base_count = 0
    dlc_counts = Counter()
    for entry in entries:
        if _is_group_entry(entry):
            continue
        if entry.get("source_kind") == "DLC":
            dlc_name = _safe_text(entry.get("dlc_name")) or "unknown"
            dlc_counts[dlc_name] += 1
        else:
            base_count += 1
    return base_count, dlc_counts


def _dlc_breakdown_lines(dlc_counts: Counter, max_line_len: int = 100):
    if not dlc_counts:
        return []
    parts = [f"{name}: {count}" for name, count in sorted(dlc_counts.items(), key=lambda kv: (kv[0].lower(), kv[1]))]
    lines = []
    current = ""
    for part in parts:
        candidate = part if not current else f"{current} | {part}"
        if len(candidate) > max_line_len and current:
            lines.append(current)
            current = part
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _build_journal_browser_info_tooltip(
    browser_key: str,
    selected_group_label: str,
    shown_count: int,
    total_count: int,
    current_page: int,
    total_pages: int,
    base_count: int,
    dlc_counts: Counter,
    group_data: dict,
    cache_info: dict,
) -> str:
    lines = []
    browser_label = _safe_text(browser_key).title() or "Journal"
    lines.append(f"{browser_label} Browser")
    lines.append(f"Page: {int(current_page) + 1}/{int(total_pages)}")
    lines.append(f"Shown: {int(shown_count)}/{int(total_count)}")
    lines.append(f"Selected Group: {_safe_text(selected_group_label) or 'All Groups'}")
    lines.append(f"Base Entries: {int(base_count)}")
    dlc_total = int(sum(dlc_counts.values()))
    lines.append(f"DLC Entries: {dlc_total}")
    for dlc_name, count in sorted(dlc_counts.items(), key=lambda kv: (kv[0].lower(), kv[1])):
        lines.append(f"DLC {dlc_name}: {int(count)}")

    ungrouped_count = int((group_data or {}).get("ungrouped_count", 0))
    missing_group_count = int((group_data or {}).get("missing_group_count", 0))
    empty_group_count = int((group_data or {}).get("empty_group_count", 0))
    grouped_count = int((group_data or {}).get("grouped_count", 0))
    lines.append(
        "Grouping: "
        f"grouped={grouped_count}, no_group={ungrouped_count}, "
        f"group_missing={missing_group_count}, empty_groups_hidden={empty_group_count}"
    )

    cache_label = _safe_text((cache_info or {}).get("cache")) or "rebuilt"
    lines.append(f"Cache: {cache_label}")
    refresh_info = (cache_info or {}).get("refresh")
    if isinstance(refresh_info, dict):
        lines.append(
            "Last Refresh: "
            f"+{int(refresh_info.get('added', 0))} new, "
            f"-{int(refresh_info.get('removed', 0))} removed, "
            f"total {int(refresh_info.get('total', total_count))}"
        )
    return "\n".join(lines)


def _placeholder_icon_path():
    placeholder_path = os.path.join(
        os.path.dirname(__file__),
        "ui",
        "icons",
        "journal_placeholder_icon.png",
    )
    return placeholder_path if win_path_exists(placeholder_path) else ""


def _ensure_entry_icon(preview_collection, entry: dict, fallback_icon: str = "QUESTION"):
    image_path = _safe_text(entry.get("image_path"))
    using_placeholder = False
    if not image_path or not win_path_exists(image_path):
        image_path = _placeholder_icon_path()
        using_placeholder = True
        if not image_path:
            return 0

    preview_key = entry.get("_preview_key")
    if not preview_key:
        if using_placeholder:
            preview_key = "__journal_placeholder_icon__"
        else:
            preview_key = f"{_safe_text(entry.get('repo_path'))}|{image_path}"
        entry["_preview_key"] = preview_key

    try:
        icon = preview_collection.get(preview_key)
    except Exception:
        icon = None
    if icon is None:
        try:
            icon = preview_collection.load(preview_key, win_safe_path(image_path), 'IMAGE')
        except Exception:
            log.debug("Failed to load preview icon for %s", image_path, exc_info=True)
            return 0

    try:
        entry["_icon_id"] = icon.icon_id
        return icon.icon_id
    except Exception:
        return 0


class _JournalBrowserMixin:
    bl_options = {'REGISTER', 'UNDO'}

    items_per_page: bpy.props.IntProperty(name="Items Per Page", default=16, min=1)
    filter_text: bpy.props.StringProperty(name="Filter", default="")
    group_filter: bpy.props.EnumProperty(name="Group", items=_journal_group_filter_items)
    open_import_dialog: bpy.props.BoolProperty(
        name="Open Dialog",
        default=False,
        description="Open the matching import dialog instead of importing immediately",
    )
    sort_mode: bpy.props.EnumProperty(
        name="Sort",
        items=(
            ("NAME_ASC", "Name A-Z", "Sort by baseName ascending"),
            ("NAME_DESC", "Name Z-A", "Sort by baseName descending"),
            ("ORDER_ASC", "Order 0-9", "Sort by journal order ascending"),
            ("ORDER_DESC", "Order 9-0", "Sort by journal order descending"),
        ),
        default="NAME_ASC",
    )

    journal_browser_key = "BESTIARY"

    def _free_previews(self):
        preview_collection = getattr(self, "preview_collection", None)
        if preview_collection is not None:
            try:
                bpy.utils.previews.remove(preview_collection)
            except Exception:
                log.debug("Failed to free preview collection", exc_info=True)
            finally:
                self.preview_collection = None

    def execute(self, context):
        self._free_previews()
        return {'FINISHED'}

    def cancel(self, context):
        self._free_previews()

    def invoke(self, context, event):
        # Avoid opening to an empty grid because a stale filter from a prior session/operator
        # instance is still applied.
        self.filter_text = ""
        self.group_filter = "ALL"
        self.sort_mode = "NAME_ASC"
        self._last_filter_state = None
        has_mem_cache = any(
            isinstance(mem_key, tuple)
            and len(mem_key) >= 1
            and mem_key[0] == self.journal_browser_key
            for mem_key in _JOURNAL_METADATA_MEM_CACHE.keys()
        )
        if not has_mem_cache and _load_journal_entries_from_disk_payload(self.journal_browser_key) is None:
            self.report({'INFO'}, f"Building {self.journal_browser_key.title()} browser cache for the first time. Please wait...")
        self.load_previews()
        return context.window_manager.invoke_props_dialog(self, width=900)

    def _group_filter_lookup(self):
        preview_collection = getattr(self, "preview_collection", None)
        entries = list(getattr(preview_collection, "my_previews", [])) if preview_collection is not None else []
        options = _collect_group_filter_options(entries)
        return {option["id"]: option for option in options}

    def _ensure_valid_group_filter(self):
        valid_ids = {"ALL"}
        valid_ids.update(self._group_filter_lookup().keys())
        if _safe_text(getattr(self, "group_filter", "ALL")) not in valid_ids:
            self.group_filter = "ALL"

    def _entry_order_value(self, entry: dict):
        value = entry.get("journal_order")
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if value is None:
            return None
        try:
            return int(str(value), 10)
        except Exception:
            return None

    def _sort_entries(self, entries: list[dict]):
        sort_mode = _safe_text(getattr(self, "sort_mode", "NAME_ASC")).upper() or "NAME_ASC"
        if sort_mode == "NAME_DESC":
            return sorted(entries, key=lambda e: _safe_text(e.get("name")).lower(), reverse=True)
        if sort_mode == "ORDER_ASC":
            def _key_asc(entry: dict):
                order = self._entry_order_value(entry)
                return (
                    order is None,
                    order if order is not None else 0,
                    _safe_text(entry.get("name")).lower(),
                )

            return sorted(entries, key=_key_asc)
        if sort_mode == "ORDER_DESC":
            def _key_desc(entry: dict):
                order = self._entry_order_value(entry)
                return (
                    order is None,
                    -order if order is not None else 0,
                    _safe_text(entry.get("name")).lower(),
                )

            return sorted(
                entries,
                key=_key_desc,
            )
        return sorted(entries, key=lambda e: _safe_text(e.get("name")).lower())

    def _current_filter_state(self):
        return (
            _safe_text(self.filter_text).lower(),
            _safe_text(getattr(self, "group_filter", "ALL")),
            _safe_text(getattr(self, "sort_mode", "NAME_ASC")),
            int(getattr(self, "items_per_page", 16)),
        )

    def _sync_page_for_filter_changes(self, context):
        state = self._current_filter_state()
        if getattr(self, "_last_filter_state", None) != state:
            try:
                setattr(context.scene, IMAGE_BROWSER_PAGE_PROP, 0)
            except Exception:
                pass
            self._last_filter_state = state

    def _get_filtered_previews(self):
        if getattr(self, "preview_collection", None) is None:
            return []
        self._ensure_valid_group_filter()
        group_filter = _safe_text(getattr(self, "group_filter", "ALL"))
        filter_text = _safe_text(self.filter_text).lower()

        all_entries = list(getattr(self.preview_collection, "my_previews", []))
        group_data = _collect_group_filter_data(all_entries)
        existing_group_ids = set(group_data.get("existing_group_ids", set()))
        entries = [item for item in all_entries if _is_leaf_entry(item)]
        if group_filter == "ALL":
            entries = [
                item for item in entries
                if _entry_group_option_id(item) and _entry_group_option_id(item) in existing_group_ids
            ]
        elif group_filter == _NO_GROUP_FILTER_ID:
            entries = [item for item in entries if not _entry_group_option_id(item)]
        elif group_filter == _GROUP_MISSING_FILTER_ID:
            entries = [
                item for item in entries
                if _entry_group_option_id(item) and _entry_group_option_id(item) not in existing_group_ids
            ]
        elif group_filter:
            entries = [item for item in entries if _entry_group_option_id(item) == group_filter]
        if filter_text:
            entries = [
                item for item in entries
                if filter_text in _safe_text(item.get("name")).lower()
                or filter_text in _safe_text(item.get("repo_path")).lower()
                or filter_text in _safe_text(item.get("description")).lower()
                or filter_text in _safe_text(item.get("dlc_name")).lower()
                or filter_text in _safe_text(item.get("group_name")).lower()
            ]
        return self._sort_entries(entries)

    def _clamp_page_for_count(self, context, item_count: int, commit: bool = False):
        total_pages = max(1, (item_count + self.items_per_page - 1) // self.items_per_page)
        current_page = getattr(context.scene, IMAGE_BROWSER_PAGE_PROP, 0)
        clamped_page = min(max(current_page, 0), total_pages - 1)
        if commit and clamped_page != current_page:
            try:
                setattr(context.scene, IMAGE_BROWSER_PAGE_PROP, clamped_page)
            except Exception:
                # Blender can disallow Scene writes during draw; callers can request a read-only clamp.
                pass
        return clamped_page, total_pages

    def load_previews(self):
        # Initialize preview collection for this dialog instance.
        self._free_previews()
        self.preview_collection = bpy.utils.previews.new()
        self.preview_collection.my_previews = []
        force_refresh = bool(getattr(self, "_force_refresh_once", False))
        self._force_refresh_once = False
        entries, cache_info = _load_journal_entries_cached(self.journal_browser_key, force_refresh=force_refresh)
        self.preview_collection.my_previews = entries
        _update_group_filter_options_cache(self.journal_browser_key, entries)
        self.cache_info = cache_info
        self._refresh_serial_seen = _JOURNAL_BROWSER_REFRESH_SERIAL.get(self.journal_browser_key, 0)
        self._ensure_valid_group_filter()
        self._last_filter_state = None
        self._clamp_page_for_count(bpy.context, len(self._get_filtered_previews()), commit=True)

    def draw(self, context):
        current_refresh_serial = _JOURNAL_BROWSER_REFRESH_SERIAL.get(self.journal_browser_key, 0)
        if getattr(self, "_refresh_serial_seen", -1) != current_refresh_serial:
            self.load_previews()

        layout = self.layout
        col = layout.column(align=True)

        self._ensure_valid_group_filter()
        self._sync_page_for_filter_changes(context)

        filtered_previews = self._get_filtered_previews()
        current_page, total_pages = self._clamp_page_for_count(context, len(filtered_previews), commit=True)

        all_entries = [entry for entry in getattr(self.preview_collection, "my_previews", []) if _is_leaf_entry(entry)]
        group_data = _collect_group_filter_data(getattr(self.preview_collection, "my_previews", []))
        ungrouped_count = int(group_data.get("ungrouped_count", 0))
        missing_group_count = int(group_data.get("missing_group_count", 0))
        empty_group_count = int(group_data.get("empty_group_count", 0))
        base_count, dlc_counts = _entry_counts(all_entries)

        # Fixed header line 1 (filters)
        row = col.row(align=True)
        row.prop(self, "filter_text", text="", icon="VIEWZOOM")
        row.prop(self, "group_filter", text="Group")
        row.prop(self, "sort_mode", text="Sort")

        # Fixed header line 2 (pagination + counts)
        row = col.row(align=True)
        max_page = max(0, total_pages - 1)
        prev_op = row.operator(MyPageOperator.bl_idname, text="<")
        prev_op.direction = 'BACK'
        prev_op.max_page = max_page
        row.label(text=f"Page {current_page + 1}/{total_pages}")
        next_op = row.operator(MyPageOperator.bl_idname, text=">")
        next_op.direction = 'FORWARD'
        next_op.max_page = max_page
        refresh_op = row.operator(MyJournalBrowserRefreshOperator.bl_idname, text="", icon='FILE_REFRESH')
        refresh_op.browser_key = self.journal_browser_key
        row.prop(self, "open_import_dialog", text="Open Dialog")
        row.label(text=f"Shown {len(filtered_previews)}/{len(all_entries)} | Base {base_count} | DLC {sum(dlc_counts.values())}")

        # Fixed header line 3 (group/cache status)
        group_lookup = self._group_filter_lookup()
        selected_group_id = _safe_text(getattr(self, "group_filter", "ALL"))
        selected_group = group_lookup.get(selected_group_id)
        cache_info = getattr(self, "cache_info", {}) or {}
        cache_label = _safe_text(cache_info.get("cache")) or "rebuilt"
        selected_label = "All Groups"
        if selected_group_id == _NO_GROUP_FILTER_ID:
            selected_label = "No Group"
        elif selected_group_id == _GROUP_MISSING_FILTER_ID:
            selected_label = "Group Missing"
        elif selected_group_id != "ALL" and selected_group is not None:
            selected_label = _safe_text(selected_group.get("label")) or "All Groups"
        elif selected_group_id != "ALL":
            selected_label = selected_group_id or "All Groups"
        status_row = col.row()
        status_row.label(
            text=(
                f"Group: {selected_label} | No Group: {ungrouped_count}"
                f" | Group Missing: {missing_group_count}"
                f" | Empty Groups Hidden: {empty_group_count} | Cache: {cache_label}"
            ),
            icon='INFO',
        )
        info_op = status_row.operator(MyJournalBrowserInfoOperator.bl_idname, text="", icon='QUESTION')
        info_op.tooltip_text = _build_journal_browser_info_tooltip(
            self.journal_browser_key,
            selected_label,
            len(filtered_previews),
            len(all_entries),
            current_page,
            total_pages,
            base_count,
            dlc_counts,
            group_data,
            cache_info,
        )

        grid = col.grid_flow(columns=4, even_columns=True, even_rows=True, align=True)
        start = current_page * self.items_per_page
        end = start + self.items_per_page
        if not filtered_previews:
            empty_row = col.row()
            all_count = len(all_entries)
            if all_count > 0:
                empty_row.label(text=f"No matches for current filters. Entries loaded: {all_count}", icon='INFO')
            else:
                empty_row.label(text="No journal entries found for this browser.", icon='INFO')
            return

        for entry in filtered_previews[start:end]:
            name = _safe_text(entry.get("name"))
            repo_path = _safe_text(entry.get("repo_path"))
            can_import = bool(repo_path)
            icon_id = _ensure_entry_icon(self.preview_collection, entry)
            exported = _is_exported_depot_path(repo_path)
            box = grid.box()
            row = box.row()
            if icon_id:
                row.template_icon(icon_value=icon_id, scale=8.0)
            else:
                row.label(text="", icon='QUESTION')

            action_row = box.row(align=True)
            op = action_row.operator(MyImageActionOperator.bl_idname, text=name)
            op.image_name = name
            op.repo_path = repo_path
            op.tooltip_text = _build_entry_tooltip(entry)
            op.open_import_dialog = bool(getattr(self, "open_import_dialog", False))

            if can_import:
                aux = action_row.operator(MyJournalEntryFileOperator.bl_idname, text="", icon='FILE_FOLDER' if exported else 'IMPORT')
                aux.repo_path = repo_path
                aux.action = "OPEN_FOLDER" if exported else "UNBUNDLE"
            else:
                info_row = action_row.row(align=True)
                info_row.enabled = False
                info_row.label(text="", icon='INFO')

            source_kind = _safe_text(entry.get("source_kind"))
            dlc_name = _safe_text(entry.get("dlc_name"))
            source_tag = (dlc_name or "DLC")[:10] if source_kind == "DLC" else ""
            action_row.label(text=source_tag)

            desc_text = _safe_text(entry.get("description_short"))
            if desc_text:
                desc_row = box.row()
                desc_row.label(text=desc_text, icon='INFO')


class MyImageOperator(_JournalBrowserMixin, bpy.types.Operator):
    """Browse bestiary journal entries"""
    bl_idname = "witcher.image_browser"
    bl_label = "Bestiary"
    journal_browser_key = "BESTIARY"
    group_filter: bpy.props.EnumProperty(name="Group", items=_journal_group_filter_items_bestiary)


class MyCharacterImageOperator(_JournalBrowserMixin, bpy.types.Operator):
    """Browse character journal entries"""
    bl_idname = "witcher.character_image_browser"
    bl_label = "Characters"
    journal_browser_key = "CHARACTERS"
    group_filter: bpy.props.EnumProperty(name="Group", items=_journal_group_filter_items_characters)


    # def draw(self, context):
    #     layout = self.layout
    #     col = layout.column(align=True)
    #     grid = col.grid_flow(columns=4, even_columns=True, even_rows=True, align=True)

    #     for (name, icon_id, repo_path) in self.preview_collection.my_previews:
    #         box = grid.box()
    #         row = box.row()
    #         row.template_icon(icon_value=icon_id, scale=6.0)
    #         row = box.row()
    #         # Pass both name and repo_path to the operator
    #         op = row.operator(MyImageActionOperator.bl_idname, text=name)
    #         op.image_name = name
    #         op.repo_path = repo_path


class MyJournalBrowserRefreshOperator(bpy.types.Operator):
    """Refresh the journal browser cache in place (smart incremental)"""
    bl_idname = "witcher.journal_browser_refresh"
    bl_label = "Refresh Journal Browser"

    browser_key: bpy.props.StringProperty(default="")

    @classmethod
    def description(cls, context, properties):
        key = _safe_text(getattr(properties, "browser_key", "")).title() or "Journal Browser"
        return f"Smart refresh {key} browser (updates new/removed journals)"

    def execute(self, context):
        key = _safe_text(self.browser_key).upper()
        if key and key not in JOURNAL_BROWSER_CONFIGS:
            self.report({'WARNING'}, f"Unknown journal browser key: {key}")
            return {'CANCELLED'}
        key = key or "BESTIARY"
        stats = _smart_refresh_journal_cache(key)
        _JOURNAL_BROWSER_REFRESH_SERIAL[key] = _JOURNAL_BROWSER_REFRESH_SERIAL.get(key, 0) + 1
        setattr(context.scene, IMAGE_BROWSER_PAGE_PROP, 0)
        self.report(
            {'INFO'},
            f"{key.title()} refreshed: +{int(stats.get('added', 0))} new, -{int(stats.get('removed', 0))} removed, total {int(stats.get('total', 0))}",
        )
        return {'FINISHED'}


class MyJournalEntryFileOperator(bpy.types.Operator):
    """Open exported folder or unbundle the entry template"""
    bl_idname = "witcher.journal_browser_entry_file"
    bl_label = "Journal Entry File Action"

    action: bpy.props.StringProperty(default="OPEN_FOLDER")
    repo_path: bpy.props.StringProperty(default="")

    @classmethod
    def description(cls, context, properties):
        action = _safe_text(getattr(properties, "action", ""))
        repo_path = _safe_text(getattr(properties, "repo_path", ""))
        if action == "UNBUNDLE":
            return f"Unbundle/export entity template\n{repo_path}"
        return f"Open exported folder\n{repo_path}"

    def execute(self, context):
        repo_path = _normalize_depot_path(self.repo_path)
        if not repo_path:
            self.report({'INFO'}, "This journal entry has no resolved entity path.")
            return {'CANCELLED'}

        if self.action == "UNBUNDLE":
            abs_path = _ensure_depot_path_exported(repo_path)
            if abs_path and win_path_exists(abs_path):
                self.report({'INFO'}, f"Exported: {repo_path}")
                return {'FINISHED'}
            self.report({'WARNING'}, f"Could not export: {repo_path}")
            return {'CANCELLED'}

        abs_path = repo_file(repo_path)
        if not abs_path or not win_path_exists(abs_path):
            self.report({'WARNING'}, "File not exported yet")
            return {'CANCELLED'}
        folder = os.path.dirname(abs_path)
        if not folder or not win_path_exists(folder):
            self.report({'WARNING'}, "Export folder not found")
            return {'CANCELLED'}

        try:
            result = bpy.ops.wm.path_open(filepath=folder)
            if isinstance(result, set) and 'FINISHED' in result:
                return {'FINISHED'}
        except Exception as e:
            self.report({'ERROR'}, f"Failed to open folder: {e}")
            return {'CANCELLED'}
        return {'CANCELLED'}


class MyImageActionOperator(bpy.types.Operator):
    """Perform an Action on Image"""
    bl_idname = "witcher.image_browser_action"
    bl_label = "Image Action"
    image_name: bpy.props.StringProperty()
    repo_path: bpy.props.StringProperty()  # Repository path property
    tooltip_text: bpy.props.StringProperty(default="")
    open_import_dialog: bpy.props.BoolProperty(default=False)

    @classmethod
    def description(cls, context, properties):
        tooltip = _safe_text(getattr(properties, "tooltip_text", ""))
        if tooltip:
            return tooltip
        repo_path = _safe_text(getattr(properties, "repo_path", ""))
        if repo_path:
            return f"Import entity template\n{repo_path}"
        return cls.bl_label

    def execute(self, context):
        # Now also prints the repo path
        logging.info(f"Selected image: {self.image_name}, Repo Path: {self.repo_path}")
        if not _normalize_depot_path(self.repo_path):
            self.report({'INFO'}, "This journal entry has no resolved entity path.")
            return {'CANCELLED'}
        abs_path = _ensure_depot_path_exported(self.repo_path)
        if not abs_path or not win_path_exists(abs_path):
            self.report({'WARNING'}, f"Could not find/export: {self.repo_path}")
            return {'CANCELLED'}
        if self.open_import_dialog:
            try:
                entity = import_entity.test_load_entity(abs_path)
            except Exception:
                entity = None
            if (
                entity is not None
                and not import_entity.entity_has_main_skeleton(entity)
                and import_entity.entity_has_inventory_entries(entity)
                and import_entity.can_apply_inventory_to_selected_character(context)
            ):
                return bpy.ops.witcher.import_w2ent_inventory(
                    'INVOKE_DEFAULT',
                    filepath=abs_path,
                    import_mode='MOUNTS',
                )
            return bpy.ops.witcher.import_w2ent_character(
                'INVOKE_DEFAULT',
                filepath=abs_path,
                import_apperance=1,
            )
        if not import_entity.try_apply_inventory_file_to_selected_character(context, abs_path):
            arm_obj = import_entity.import_ent_template(abs_path, False, 1)
            if arm_obj and get_all_addon_prefs(context).import_idle_animation:
                from .importers.import_anims import load_idle_animation_for_armature as _load_idle_anim
                _load_idle_anim(context, arm_obj)
        return {'FINISHED'}


class MyJournalBrowserInfoOperator(bpy.types.Operator):
    """Show journal browser details in tooltip"""
    bl_idname = "witcher.journal_browser_info"
    bl_label = "Journal Browser Info"

    tooltip_text: bpy.props.StringProperty(default="")

    @classmethod
    def description(cls, context, properties):
        tooltip = _safe_text(getattr(properties, "tooltip_text", ""))
        return tooltip or cls.bl_label

    def execute(self, context):
        return {'FINISHED'}


class MyPageOperator(bpy.types.Operator):
    bl_idname = "witcher.image_browser_page"
    bl_label = "Page Operator"

    direction: bpy.props.StringProperty()
    max_page: bpy.props.IntProperty(default=-1)

    def execute(self, context):
        current_page = int(getattr(context.scene, IMAGE_BROWSER_PAGE_PROP, 0))
        max_page = int(getattr(self, "max_page", -1))
        if max_page >= 0:
            current_page = min(max(current_page, 0), max_page)

        if self.direction == 'FORWARD':
            next_page = current_page + 1
            if max_page >= 0:
                next_page = min(next_page, max_page)
            setattr(context.scene, IMAGE_BROWSER_PAGE_PROP, next_page)
        elif self.direction == 'BACK':
            setattr(context.scene, IMAGE_BROWSER_PAGE_PROP, max(current_page - 1, 0))
        elif max_page >= 0:
            setattr(context.scene, IMAGE_BROWSER_PAGE_PROP, min(max(current_page, 0), max_page))

        return {'FINISHED'}

def update_image_previews(self, context):
    return

def register():
    bpy.types.Scene.witcher_image_browser_current_page = bpy.props.IntProperty(
        name="Current Page",
        default=0,
        update=update_image_previews,
    )
    bpy.utils.register_class(MyImageOperator)
    bpy.utils.register_class(MyCharacterImageOperator)
    bpy.utils.register_class(MyJournalBrowserRefreshOperator)
    bpy.utils.register_class(MyJournalEntryFileOperator)
    bpy.utils.register_class(MyImageActionOperator)
    bpy.utils.register_class(MyJournalBrowserInfoOperator)
    bpy.utils.register_class(MyPageOperator)

def unregister():
    if hasattr(bpy.types.Scene, "witcher_image_browser_current_page"):
        del bpy.types.Scene.witcher_image_browser_current_page
    bpy.utils.unregister_class(MyPageOperator)
    bpy.utils.unregister_class(MyJournalBrowserInfoOperator)
    bpy.utils.unregister_class(MyImageActionOperator)
    bpy.utils.unregister_class(MyJournalEntryFileOperator)
    bpy.utils.unregister_class(MyJournalBrowserRefreshOperator)
    bpy.utils.unregister_class(MyCharacterImageOperator)
    bpy.utils.unregister_class(MyImageOperator)

# if __name__ == "__main__":
#     register()
