"""Equipment catalog loading, XML parsing, and item lookup helpers."""

import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import bpy

from .. import (
    get_all_addon_prefs,
    get_uncook_path,
)
from ..CR2W.witcher_cache import cache_meta
from ..CR2W.witcher_cache.Bundles import BundleManager, LoadBundleManager
from ..extension_paths import get_cache_root, get_dev_override
from ..repo_paths import (
    configured_w2_repo_roots,
    is_under_root,
    normalize_source_game as _normalize_source_game,
)


log = logging.getLogger(__name__)


default_categories = {
    "pants": [("None", "None", ""), ("Body underwear 01", "Body underwear 01", "l_01_mg__body_underwear")],
    "armor": [("None", "None", ""), ("Body torso 01", "Body torso 01", "t_01_mg__body")],
    "gloves": [("None", "None", ""), ("Body palms 01", "Body palms 01", "g_01_mg__body")],
    "boots": [("None", "None", ""), ("Body feet 01", "Body feet 01", "s_01_mg__body")],
    "steelsword": [("None", "None", "")],
    "silversword": [("None", "None", "")],
    "crossbow": [("None", "None", "")],
    "head": [("None", "None", ""), ("head_2", "head_2", "h_02_mg__geralt")],
    "hair": [("None", "None", ""), ("Preview Hair", "Preview Hair", "c_01b_mg__witcher")],
}

category_items = {category: list(items) for category, items in default_categories.items()}
item_attributes = {}
w2_category_items = {}
w2_item_attributes = {}

_CATEGORY_CACHE_FILE = Path(get_cache_root(create=True)) / "equipment_categories.json"
_CATEGORY_CACHE_SCHEMA_VERSION = 2
_CATEGORY_SOURCE_VALIDATION_INTERVAL_SECONDS = 5.0
_CATEGORY_REFRESH_RETRY_INTERVAL_SECONDS = 30.0
_CATALOG_CACHE_FLAGS = {
    "w3": {"schema_version": 0, "icon_path": False, "hold_template": False},
    "w2": {"schema_version": 0, "icon_path": False, "hold_template": False},
}
_W3_CATEGORY_BUNDLE_STATE = {
    "loaded": False,
    "stale": False,
    "signature": {},
    "source_checked_at": 0.0,
    "refresh_after": 0.0,
}
_ITEM_ATTRIBUTE_IDENTIFIER_LOOKUP = {
    "w3": None,
    "w2": None,
}
_LOADED_EQUIPMENT_XML_DIRS = set()
_W2_CATEGORY_CACHE_LOADED = False
_XML_DECL_RE = re.compile(r'^\s*<\?xml[^>]*\?>', re.IGNORECASE)
_XML_DECL_ENCODING_BYTES_RE = re.compile(br'<\?xml[^>]*encoding=["\']([^"\']+)["\']', re.IGNORECASE)
_XML_HASH_COMMENT_LINE_RE = re.compile(r'(?m)^[ \t]*#.*(?:\r?\n|$)')
_GERALT_BODYPART_CATEGORY_BY_FOLDER = {
    "trunk": "armor",
    "legs": "pants",
    "gloves": "gloves",
    "shoes": "boots",
    "coif": "head",
    "heads": "head",
}
_ICON_CACHE_CLEAR_CALLBACK = None
_TEMPLATE_CACHE_CLEAR_CALLBACK = None


def set_icon_cache_clear_callback(callback):
    global _ICON_CACHE_CLEAR_CALLBACK
    _ICON_CACHE_CLEAR_CALLBACK = callback


def set_template_cache_clear_callback(callback):
    global _TEMPLATE_CACHE_CLEAR_CALLBACK
    _TEMPLATE_CACHE_CLEAR_CALLBACK = callback


def _notify_icon_cache_clear():
    if _ICON_CACHE_CLEAR_CALLBACK is None:
        return
    try:
        _ICON_CACHE_CLEAR_CALLBACK()
    except Exception:
        log.debug("Failed to clear equipment item icon cache", exc_info=True)


def _notify_template_cache_clear():
    if _TEMPLATE_CACHE_CLEAR_CALLBACK is None:
        return
    try:
        _TEMPLATE_CACHE_CLEAR_CALLBACK()
    except Exception:
        log.debug("Failed to clear equipment template path cache", exc_info=True)


def _split_tags(raw_text):
    if not raw_text:
        return []
    text = str(raw_text).replace("\n", " ").replace("\t", " ").strip()
    if not text:
        return []
    parts = re.split(r"[,\s]+", text)
    return [p for p in (part.strip() for part in parts) if p]


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


def _norm_root_path(path):
    if not path:
        return ""
    try:
        return os.path.normcase(os.path.normpath(str(path)))
    except Exception:
        return str(path).lower()


def _template_key(value):
    text = str(value or "").replace("/", "\\").strip().strip("\\").lower()
    if text.endswith(".w2ent"):
        text = text[:-6]
    if "\\" in text:
        text = text.rsplit("\\", 1)[-1]
    return text


def _candidate_geralt_bodypart_roots(folder_path):
    try:
        current = Path(folder_path)
    except Exception:
        return []
    candidates = []
    seen = set()
    for parent in [current] + list(current.parents):
        if parent.name.lower() != "gameplay":
            continue
        depot_root = parent.parent
        bodypart_root = depot_root / "items" / "bodyparts" / "geralt_items"
        try:
            norm = os.path.normcase(os.path.normpath(str(bodypart_root)))
        except Exception:
            norm = str(bodypart_root).lower()
        if norm in seen:
            continue
        seen.add(norm)
        if bodypart_root.is_dir():
            candidates.append((depot_root, bodypart_root))
    return candidates


def build_geralt_bodypart_template_index(folder_path):
    """Map bodypart .w2ent basenames to repo-relative geralt item paths."""
    index = {}
    for depot_root, bodypart_root in _candidate_geralt_bodypart_roots(folder_path):
        for file_path in sorted(bodypart_root.rglob("*.w2ent")):
            template_key = _template_key(file_path.stem)
            if not template_key:
                continue
            try:
                rel_path = str(file_path.relative_to(depot_root)).replace("/", "\\")
            except Exception:
                rel_path = str(file_path.name)
            try:
                local_rel = str(file_path.relative_to(bodypart_root)).replace("/", "\\")
            except Exception:
                local_rel = ""
            parts = local_rel.split("\\") if local_rel else []
            bodypart_folder = parts[0] if len(parts) >= 3 else ""
            set_folder = parts[1] if len(parts) >= 3 else ""
            category = _GERALT_BODYPART_CATEGORY_BY_FOLDER.get(bodypart_folder, bodypart_folder)
            index.setdefault(template_key, {
                "path": rel_path,
                "category": category,
                "bodypart_folder": bodypart_folder,
                "set_folder": set_folder,
            })
    return index


def set_catalog_cache_flags(source_game="w3", *, schema_version=0, icon_path=False, hold_template=False):
    source_game = _normalize_source_game(source_game)
    _CATALOG_CACHE_FLAGS[source_game] = {
        "schema_version": int(schema_version or 0),
        "icon_path": bool(icon_path),
        "hold_template": bool(hold_template),
    }


def infer_catalog_cache_flags(attrs_by_item):
    has_icon_path = False
    has_hold_template = False
    for attrs in attrs_by_item.values():
        if not isinstance(attrs, dict):
            continue
        if "icon_path" in attrs:
            has_icon_path = True
        if "hold_template" in attrs:
            has_hold_template = True
        if has_icon_path and has_hold_template:
            break
    return has_icon_path, has_hold_template


def catalog_has_browser_icon_fields(source_game="w3"):
    source_game = _normalize_source_game(source_game)
    flags = _CATALOG_CACHE_FLAGS.get(source_game, {})
    if flags.get("icon_path") and flags.get("hold_template"):
        return True

    _category_items, attrs_by_item = get_equipment_catalog(source_game)
    has_icon_path, has_hold_template = infer_catalog_cache_flags(attrs_by_item)
    if attrs_by_item:
        set_catalog_cache_flags(
            source_game,
            schema_version=flags.get("schema_version", 0),
            icon_path=has_icon_path,
            hold_template=has_hold_template,
        )
    return has_icon_path and has_hold_template


def get_category_cache_file(source_game="w3"):
    source_game = _normalize_source_game(source_game)
    if source_game == "w2":
        return Path(get_cache_root(create=True)) / "equipment_categories_w2.json"
    return _CATEGORY_CACHE_FILE


def build_w3_category_cache_source_signature():
    try:
        signature, source = BundleManager.BuildSourceSignature(False)
    except Exception:
        log.debug("Failed to build equipment category source signature", exc_info=True)
        return {}, {}
    if not signature.get("count"):
        return {}, dict(source or {})
    signature = dict(signature)
    source = dict(source or {})
    source["type"] = "equipment_categories"
    source["category_schema_version"] = _CATEGORY_CACHE_SCHEMA_VERSION
    return signature, source


def _bundle_xml_cache_has_xml():
    root = Path(get_cache_root(create=True)) / "equipment_items_xml_bundle"
    if not root.is_dir():
        return False
    return any(
        name.lower().endswith(".xml")
        for _dirpath, _dirnames, names in os.walk(root)
        for name in names
    )


def _w3_bundle_xml_source_snapshot():
    has_xml = _bundle_xml_cache_has_xml()
    if not has_xml:
        return {}, {}, False
    root = Path(get_cache_root(create=True)) / "equipment_items_xml_bundle"
    meta = cache_meta.load_meta(cache_meta.get_meta_path(str(root)))
    if not isinstance(meta, dict):
        return {}, {}, True
    source = dict(meta.get("source") or {})
    if source.get("pending_removals"):
        return {}, source, True
    return dict(meta.get("signature") or {}), source, True


def _w3_category_cache_is_current(cache_file, cache_data):
    has_snapshot = "bundle_xml_source_signature" in cache_data
    stored_signature = cache_data.get("bundle_xml_source_signature") or {}
    current_signature, _source = build_w3_category_cache_source_signature()

    # Empty stored signatures are manual caches; empty current ones preserve last-good data.
    if has_snapshot:
        if not stored_signature or not current_signature:
            return True
        return cache_meta.signatures_match(stored_signature, current_signature)

    # Preserve legacy manual caches and last-good data while sources are unavailable.
    if not _bundle_xml_cache_has_xml() or not current_signature:
        return True

    # Trust legacy caches only when newer than matching bundle metadata.
    bundle_root = Path(get_cache_root(create=True)) / "equipment_items_xml_bundle"
    bundle_meta = cache_meta.load_meta(cache_meta.get_meta_path(str(bundle_root)))
    bundle_signature = bundle_meta.get("signature", {}) if isinstance(bundle_meta, dict) else {}
    try:
        cache_mtime = int(cache_file.stat().st_mtime)
        bundle_created_at = int(bundle_meta.get("created_at", 0) or 0)
    except OSError:
        return False
    return bool(
        bundle_created_at
        and cache_mtime >= bundle_created_at
        and cache_meta.signatures_match(bundle_signature, current_signature)
    )


def _w3_loaded_category_cache_is_stale():
    if _W3_CATEGORY_BUNDLE_STATE["stale"]:
        return True
    stored_signature = _W3_CATEGORY_BUNDLE_STATE.get("signature") or {}
    if not _W3_CATEGORY_BUNDLE_STATE["loaded"] or not stored_signature:
        return False
    now = time.monotonic()
    if (
        now - float(_W3_CATEGORY_BUNDLE_STATE.get("source_checked_at", 0.0))
        < _CATEGORY_SOURCE_VALIDATION_INTERVAL_SECONDS
    ):
        return False
    _W3_CATEGORY_BUNDLE_STATE["source_checked_at"] = now
    current_signature, _source = build_w3_category_cache_source_signature()
    if current_signature and not cache_meta.signatures_match(stored_signature, current_signature):
        _W3_CATEGORY_BUNDLE_STATE["stale"] = True
    return _W3_CATEGORY_BUNDLE_STATE["stale"]


def catalog_key_for_source_game(source_game="w3"):
    source_game = _normalize_source_game(source_game)
    if source_game == "w2":
        return ("_W2_CATEGORY_ITEMS", "_W2_ITEM_ATTRIBUTES")
    return ("category_items", "item_attributes")


def get_equipment_catalog(source_game="w3"):
    source_game = _normalize_source_game(source_game)
    if source_game == "w2":
        return w2_category_items, w2_item_attributes
    return category_items, item_attributes


def clear_item_attribute_identifier_lookup(source_game=None):
    _notify_icon_cache_clear()
    if source_game is None:
        for key in _ITEM_ATTRIBUTE_IDENTIFIER_LOOKUP:
            _ITEM_ATTRIBUTE_IDENTIFIER_LOOKUP[key] = None
        return
    _ITEM_ATTRIBUTE_IDENTIFIER_LOOKUP[_normalize_source_game(source_game)] = None


def normalize_item_attribute_identifier(value):
    if value is None:
        return ""
    return str(value).replace("/", "\\").strip().strip("\\").lower()


def iter_item_attribute_identifier_aliases(value):
    normalized = normalize_item_attribute_identifier(value)
    if not normalized:
        return
    seen = set()
    for alias in (
        normalized,
        os.path.basename(normalized),
        os.path.splitext(os.path.basename(normalized))[0],
    ):
        alias = normalize_item_attribute_identifier(alias)
        if not alias or alias in seen:
            continue
        seen.add(alias)
        yield alias


def get_item_attribute_identifier_lookup(source_game="w3"):
    source_game = _normalize_source_game(source_game)
    cached = _ITEM_ATTRIBUTE_IDENTIFIER_LOOKUP.get(source_game)
    if cached is not None:
        return cached

    _category_items, attrs_by_item = get_equipment_catalog(source_game)
    lookup = {}
    for field_name in ("equip_template", "hold_template", "item_name"):
        for item_name, attrs in attrs_by_item.items():
            if not isinstance(attrs, dict):
                continue
            value = attrs.get(field_name, "") if field_name != "item_name" else (item_name or attrs.get("item_name", ""))
            for alias in iter_item_attribute_identifier_aliases(value):
                lookup.setdefault(alias, attrs)
    _ITEM_ATTRIBUTE_IDENTIFIER_LOOKUP[source_game] = lookup
    return lookup


def get_equipment_source_game_for_search_roots(search_roots=None):
    return "w2" if is_w2_search(search_roots) else "w3"


def get_equipment_catalog_for_search_roots(search_roots=None):
    return get_equipment_catalog(get_equipment_source_game_for_search_roots(search_roots))


def reset_w3_category_cache_runtime():
    category_items.clear()
    category_items.update({category: list(items) for category, items in default_categories.items()})
    item_attributes.clear()
    set_catalog_cache_flags("w3")
    clear_item_attribute_identifier_lookup("w3")
    _W3_CATEGORY_BUNDLE_STATE.update({
        "loaded": False,
        "stale": False,
        "signature": {},
        "source_checked_at": 0.0,
        "refresh_after": 0.0,
    })
    _notify_icon_cache_clear()
    _notify_template_cache_clear()


def save_category_cache(source_game="w3"):
    """Save loaded categories to the appropriate cache file."""
    try:
        source_game = _normalize_source_game(source_game)
        categories, attrs_by_item = get_equipment_catalog(source_game)
        cache_file = get_category_cache_file(source_game)
        has_icon_path, has_hold_template = infer_catalog_cache_flags(attrs_by_item)
        cache_data = {
            "schema_version": _CATEGORY_CACHE_SCHEMA_VERSION,
            "feature_flags": {
                "icon_path": has_icon_path,
                "hold_template": has_hold_template,
            },
            "category_items": categories,
            "item_attributes": attrs_by_item,
        }
        bundle_signature = {}
        bundle_source = {}
        bundle_xml_present = False
        if source_game == "w3":
            bundle_signature, bundle_source, bundle_xml_present = _w3_bundle_xml_source_snapshot()
            # Empty snapshots mark manual caches; omit unknown bundle snapshots so they retry.
            if bundle_signature or not bundle_xml_present:
                cache_data["bundle_xml_source_signature"] = bundle_signature
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(cache_data, f, indent=2)
        if source_game == "w3":
            meta_path = cache_meta.get_meta_path(str(cache_file))
            if bundle_signature:
                meta = cache_meta.make_meta(cache_file.name, str(cache_file), bundle_signature, bundle_source)
                cache_meta.save_meta(meta_path, meta)
            elif os.path.exists(meta_path):
                try:
                    os.remove(meta_path)
                except OSError:
                    log.debug("Failed to remove stale equipment category metadata", exc_info=True)
            _W3_CATEGORY_BUNDLE_STATE.update({
                "loaded": True,
                "stale": bool(bundle_xml_present and not bundle_signature),
                "signature": dict(bundle_signature),
                "source_checked_at": 0.0,
            })
        set_catalog_cache_flags(
            source_game,
            schema_version=_CATEGORY_CACHE_SCHEMA_VERSION,
            icon_path=has_icon_path,
            hold_template=has_hold_template,
        )
        log.debug("Saved %s category cache to %s", _normalize_source_game(source_game), cache_file)
        return True
    except Exception as e:
        log.warning("Failed to save category cache: %s", e)
        return False


def load_category_cache(source_game="w3"):
    """Load categories from the appropriate cache file if it exists."""
    source_game = _normalize_source_game(source_game)
    cache_file = get_category_cache_file(source_game)
    if not cache_file.exists():
        return False
    try:
        with open(cache_file, "r", encoding="utf-8") as f:
            cache_data = json.load(f)

        if source_game == "w3" and not _w3_category_cache_is_current(cache_file, cache_data):
            _W3_CATEGORY_BUNDLE_STATE.update({
                "loaded": False,
                "stale": True,
                "signature": dict(cache_data.get("bundle_xml_source_signature") or {}),
                "source_checked_at": time.monotonic(),
            })
            log.info("Equipment category cache is stale; rebuilding from XML sources")
            return False

        loaded_categories = cache_data.get("category_items", {})
        loaded_attributes = cache_data.get("item_attributes", {})
        feature_flags = cache_data.get("feature_flags", {}) if isinstance(cache_data, dict) else {}
        schema_version = int(cache_data.get("schema_version", 0) or 0) if isinstance(cache_data, dict) else 0
        target_categories, target_attributes = get_equipment_catalog(source_game)

        for category, items in loaded_categories.items():
            filtered_items = []
            for item in items:
                item_name = item[0] if item else ""
                attrs = loaded_attributes.get(item_name, {}) if item_name else {}
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

        for item_name, attrs in loaded_attributes.items():
            if not isinstance(attrs, dict):
                continue
            item_source_game = _normalize_source_game(attrs.get("source_game", source_game))
            if item_source_game != source_game:
                continue
            target_attributes[item_name] = attrs

        inferred_icon_path, inferred_hold_template = infer_catalog_cache_flags(loaded_attributes)
        set_catalog_cache_flags(
            source_game,
            schema_version=schema_version,
            icon_path=feature_flags.get("icon_path", inferred_icon_path),
            hold_template=feature_flags.get("hold_template", inferred_hold_template),
        )
        clear_item_attribute_identifier_lookup(source_game)
        if source_game == "w3":
            bundle_signature = dict(cache_data.get("bundle_xml_source_signature") or {})
            if not bundle_signature and "bundle_xml_source_signature" not in cache_data and _bundle_xml_cache_has_xml():
                bundle_signature, _source = build_w3_category_cache_source_signature()
            _W3_CATEGORY_BUNDLE_STATE.update({
                "loaded": True,
                "stale": False,
                "signature": bundle_signature,
                "source_checked_at": time.monotonic(),
            })
        log.debug("Loaded %d %s categories from cache", len(loaded_categories), source_game)
        return True
    except Exception as e:
        log.warning("Failed to load category cache: %s", e)
        return False


def source_game_from_xml_path(path_value):
    lowered = str(path_value or "").replace("/", "\\").lower()
    if "\\gameplay\\items" in lowered:
        return "w3"
    if lowered.endswith("\\gameplay\\items"):
        return "w3"
    if "\\items\\" in lowered or lowered.endswith("\\items"):
        return "w2"
    return ""


def get_w2_repo_roots():
    return _normalize_unique_roots(configured_w2_repo_roots(bpy.context))


def _is_under_any_root(path_value, roots):
    if not path_value:
        return False
    try:
        path_key = os.path.normcase(os.path.normpath(str(path_value)))
    except Exception:
        return False
    for root in roots or []:
        if not root:
            continue
        try:
            if is_under_root(path_key, root):
                return True
        except Exception:
            continue
    return False


def candidate_w2_items_dirs(search_roots=None, *, include_configured_roots=True):
    candidates = []
    seen = set()
    roots = list(search_roots or [])
    allowed_roots = get_w2_repo_roots()
    if include_configured_roots:
        roots.extend(allowed_roots)
    roots = _normalize_unique_roots(roots)
    for root in roots:
        if not _is_under_any_root(root, allowed_roots):
            continue
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
                if not _is_under_any_root(candidate_path, allowed_roots):
                    continue
                if os.path.basename(norm).lower() != "items":
                    continue
                if os.path.isdir(candidate_path) and any(
                    name.lower().endswith(".xml") for name in os.listdir(candidate_path)
                ):
                    candidates.append(candidate_path)
    return candidates


def is_w2_search(search_roots):
    norm_search_roots = [_norm_root_path(root) for root in (search_roots or []) if root]
    if not norm_search_roots:
        return False
    for root in get_w2_repo_roots():
        norm_root = _norm_root_path(root)
        if not norm_root:
            continue
        prefix = norm_root + os.sep
        for candidate in norm_search_roots:
            if candidate == norm_root or candidate.startswith(prefix):
                return True
    return False


def ensure_w2_category_cache_loaded():
    global _W2_CATEGORY_CACHE_LOADED
    if _W2_CATEGORY_CACHE_LOADED:
        return False
    load_category_cache("w2")
    _W2_CATEGORY_CACHE_LOADED = True
    return True


def ensure_equipment_catalog_for_search_roots(search_roots=None):
    """Load additional XML catalogs required by the active repo roots."""
    if not is_w2_search(search_roots):
        return False

    ensure_w2_category_cache_loaded()

    categories, attrs_by_item = get_equipment_catalog("w2")
    merged_any = False
    for folder_path in candidate_w2_items_dirs(search_roots):
        try:
            norm = os.path.normcase(os.path.normpath(folder_path))
        except Exception:
            norm = folder_path.lower()
        if norm in _LOADED_EQUIPMENT_XML_DIRS:
            continue

        _, categories_from_xml, attributes_from_xml = extract_categories_from_xml(folder_path)
        if categories_from_xml or attributes_from_xml:
            merge_equipment_xml_data(
                categories,
                attrs_by_item,
                categories_from_xml,
                attributes_from_xml,
            )
            merged_any = True
            log.info(
                "Loaded Witcher 2 equipment XMLs from '%s' (%d cats, %d items)",
                folder_path,
                len(categories_from_xml),
                len(attributes_from_xml),
            )
        _LOADED_EQUIPMENT_XML_DIRS.add(norm)

    if merged_any:
        clear_item_attribute_identifier_lookup("w2")
        save_category_cache("w2")
    return merged_any


def lookup_item_attributes(item_name, source_game="w3"):
    if not item_name:
        return {}
    _category_items, attrs_by_item = get_equipment_catalog(source_game)
    attrs = attrs_by_item.get(item_name, {})
    if isinstance(attrs, dict):
        return attrs
    return {}


def ensure_equipment_catalog_ready(source_game="w3", search_roots=None, context=None, require_browser_icon_fields=False):
    """Ensure the equipment catalog is loaded for the requested source game."""
    source_game = _normalize_source_game(source_game)
    _category_items, attrs_by_item = get_equipment_catalog(source_game)
    if source_game == "w2":
        ensure_equipment_catalog_for_search_roots(search_roots)
        _category_items, attrs_by_item = get_equipment_catalog(source_game)
        return bool(attrs_by_item)
    retry_wait_active = time.monotonic() < float(_W3_CATEGORY_BUNDLE_STATE.get("refresh_after", 0.0))
    if not attrs_by_item and not retry_wait_active:
        load_category_cache(source_game)
        _category_items, attrs_by_item = get_equipment_catalog(source_game)
    if context is not None:
        needs_w3_refresh = not attrs_by_item or _w3_loaded_category_cache_is_stale()
        if require_browser_icon_fields and not catalog_has_browser_icon_fields(source_game):
            needs_w3_refresh = True
        if needs_w3_refresh:
            now = time.monotonic()
            if now >= float(_W3_CATEGORY_BUNDLE_STATE.get("refresh_after", 0.0)):
                _W3_CATEGORY_BUNDLE_STATE["refresh_after"] = (
                    now + _CATEGORY_REFRESH_RETRY_INTERVAL_SECONDS
                )
                if refresh_w3_catalog_from_xml(context):
                    _category_items, attrs_by_item = get_equipment_catalog(source_game)
    return bool(attrs_by_item)


def get_item_attributes_by_identifier(identifier, source_game="w3", strict: bool = False):
    """Resolve equipment catalog attributes by item name or template identifier."""
    _ = strict  # Kept for API compatibility; lookups are exact aliases only.
    source_game = _normalize_source_game(source_game)
    raw_value = str(identifier or "").strip()
    if not raw_value:
        return {}

    direct = lookup_item_attributes(raw_value, source_game)
    if direct:
        return direct

    lookup = get_item_attribute_identifier_lookup(source_game)
    for alias in iter_item_attribute_identifier_aliases(raw_value):
        attrs = lookup.get(alias)
        if attrs:
            return attrs
    return {}


def get_item_icon_path(identifier, source_game="w3", strict: bool = False):
    attrs = get_item_attributes_by_identifier(identifier, source_game=source_game, strict=strict)
    return str(attrs.get("icon_path", "") or "")


def extract_categories_from_xml(folder_path):
    categories = {}
    attrs_by_item = {}
    bodypart_index = build_geralt_bodypart_template_index(folder_path)

    if not folder_path or not os.path.isdir(folder_path):
        return [], categories, attrs_by_item

    for dirpath, dirnames, file_names in os.walk(folder_path):
        dirnames.sort()
        for file_name in sorted(file_names):
            if not file_name.lower().endswith(".xml"):
                continue
            file_path = os.path.join(dirpath, file_name)
            if not is_plaintext_xml_candidate(file_path):
                log.debug("Skipping non-text XML file %s", file_path)
                continue
            source_game = source_game_from_xml_path(file_path)
            try:
                root = parse_xml_root_with_fallbacks(file_path)

                for item in root.findall(".//item"):
                    category = item.get("category")
                    name = item.get("name")
                    equip_template = item.get("equip_template", "")
                    hold_template = item.get("hold_template", "")
                    template_name = equip_template or hold_template
                    icon_path = item.get("icon_path", "") or ""
                    if not icon_path:
                        icon_node = item.find("icon_path")
                        if icon_node is not None and icon_node.text:
                            icon_path = icon_node.text.strip()

                    equip_slot = item.get("equip_slot", "")
                    hold_slot = item.get("hold_slot", "")
                    weapon = item.get("weapon", "false").lower() == "true"
                    attachment_type = item.get("attachment_type", "")
                    attachment_prefix = item.get("attachment_prefix", "")
                    tags_text = ""
                    tags_node = item.find("tags")
                    if tags_node is not None and tags_node.text:
                        tags_text = tags_node.text
                    tags = _split_tags(tags_text)

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
                                    "hold_slot": v_hold_slot,
                                })

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

                    if category and name and template_name:
                        if category not in categories:
                            categories[category] = [("None", "None", "")]

                        item_tuple = (name, name, template_name)
                        if item_tuple not in categories[category]:
                            categories[category].append(item_tuple)

                        bodypart_info = bodypart_index.get(_template_key(template_name))
                        variant_bodyparts = []
                        for variant in variants:
                            variant_template = variant.get("equip_template", "")
                            variant_info = bodypart_index.get(_template_key(variant_template))
                            if not variant_info:
                                continue
                            variant_bodyparts.append({
                                "equip_template": variant_template,
                                "category": variant.get("category", "") or variant_info.get("category", ""),
                                "path": variant_info.get("path", ""),
                                "set_folder": variant_info.get("set_folder", ""),
                            })

                        attrs = {
                            "item_name": name,
                            "category": category,
                            "equip_template": equip_template,
                            "hold_template": hold_template,
                            "template_name": template_name,
                            "icon_path": icon_path,
                            "equip_slot": equip_slot,
                            "hold_slot": hold_slot,
                            "weapon": weapon,
                            "attachment_type": attachment_type,
                            "attachment_prefix": attachment_prefix,
                            "variants": variants,
                            "bound_items": bound_items,
                            "tags": tags,
                            "source_game": source_game,
                        }
                        if bodypart_info:
                            attrs["bodypart_entity_path"] = bodypart_info.get("path", "")
                            attrs["bodypart_set_folder"] = bodypart_info.get("set_folder", "")
                        if variant_bodyparts:
                            attrs["variant_bodypart_entity_paths"] = variant_bodyparts
                        attrs_by_item[name] = attrs

            except (ET.ParseError, ValueError, UnicodeError) as e:
                if source_game == "w2":
                    log.debug("Skipping malformed Witcher 2 XML %s (%s).", file_path, e)
                else:
                    log.warning("Error parsing XML %s (%s). Skipping file.", file_path, e)
                continue
            except Exception as e:
                if source_game == "w2":
                    log.debug("Skipping Witcher 2 XML %s after unexpected parse error: %s", file_path, e)
                else:
                    log.warning("Unexpected error parsing XML %s (%s). Skipping file.", file_path, e)
                continue

    return sorted(categories.keys()), categories, attrs_by_item


def flatten_bundle_item_candidates(items):
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


def select_final_bundle_item(items):
    flat = flatten_bundle_item_candidates(items)
    for candidate in reversed(flat):
        if hasattr(candidate, "name"):
            return candidate
    return None


def get_equipment_xml_bundle_cache_root():
    cache_root = Path(get_cache_root(create=True)) / "equipment_items_xml_bundle"
    cache_root.mkdir(parents=True, exist_ok=True)
    return str(cache_root)


def extract_equipment_xmls_from_bundles(force_refresh=False):
    out_root = get_equipment_xml_bundle_cache_root()
    meta_path = cache_meta.get_meta_path(out_root)
    old_meta = cache_meta.load_meta(meta_path)
    if not isinstance(old_meta, dict):
        old_meta = {}
    old_source = old_meta.get("source") or {}
    pending_removals = set(old_source.get("pending_removals") or [])
    has_cached_xml = _bundle_xml_cache_has_xml()

    signature = {}
    source = {}
    try:
        signature, source = BundleManager.BuildSourceSignature(False)
    except Exception:
        log.debug("Failed to build equipment bundle XML source signature", exc_info=True)

    cache_is_current = bool(
        has_cached_xml
        and signature
        and not pending_removals
        and cache_meta.signatures_match(old_meta.get("signature", {}), signature)
    )
    if cache_is_current and not force_refresh:
        return out_root
    if has_cached_xml and not signature and not force_refresh:
        return out_root

    try:
        bundle_manager = LoadBundleManager(reset_cache=True)
    except Exception as e:
        log.warning("Failed to load bundle manager for equipment XMLs: %s", e)
        return out_root if has_cached_xml else ""

    items = getattr(bundle_manager, "Items", None) or {}
    if not items:
        return out_root if has_cached_xml else ""
    found = 0
    failed = False
    extracted_files = set()

    for key, value in items.items():
        rel_key = str(key or "").replace("/", "\\").lstrip("\\")
        rel_lower = rel_key.lower()
        if not rel_lower.endswith(".xml"):
            continue
        if "\\gameplay\\items\\" not in ("\\" + rel_lower):
            continue

        final_item = select_final_bundle_item(value)
        if not final_item or not hasattr(final_item, "extract_to_file"):
            continue

        export_path = os.path.join(out_root, *rel_key.split("\\"))
        if not is_under_root(export_path, out_root):
            log.warning("Skipping unsafe equipment XML bundle path '%s'", rel_key)
            continue

        found += 1
        export_dir = os.path.dirname(export_path)
        if export_dir:
            os.makedirs(export_dir, exist_ok=True)
        try:
            final_item.extract_to_file(export_path)
            extracted_files.add(rel_key.replace("\\", "/"))
        except Exception as e:
            failed = True
            log.warning("Failed to extract equipment XML '%s': %s", rel_key, e)

    if found == 0:
        return out_root if has_cached_xml else ""

    if not failed and signature:
        previous_files = set(old_source.get("extracted_files") or [])
        retry_removals = set()
        for relative_path in previous_files - extracted_files:
            stale_path = os.path.join(out_root, relative_path.replace("/", os.sep).replace("\\", os.sep))
            if (
                stale_path.lower().endswith(".xml")
                and is_under_root(stale_path, out_root)
                and os.path.isfile(stale_path)
            ):
                try:
                    os.remove(stale_path)
                except OSError:
                    retry_removals.add(relative_path)
                    log.debug("Failed to remove stale equipment XML %s", stale_path, exc_info=True)

        source = dict(source or {})
        source["extracted_files"] = sorted(extracted_files | retry_removals)
        source["pending_removals"] = sorted(retry_removals)
        meta = cache_meta.make_meta("equipment_items_xml_bundle", out_root, signature, source)
        cache_meta.save_meta(meta_path, meta)
    return out_root


def get_equipment_xml_sources(context, addon_prefs, force_bundle_refresh=False):
    prefer_redkit = bool(getattr(addon_prefs, "prefer_redkit_equipment_xml", False))

    try:
        uncook_root = get_uncook_path(context)
    except Exception:
        uncook_root = ""
    uncook_items = os.path.join(uncook_root, "gameplay", "items") if uncook_root else ""

    bundle_items_root = extract_equipment_xmls_from_bundles(force_refresh=force_bundle_refresh)

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


def refresh_w3_catalog_from_xml(context, force_bundle_refresh=False):
    try:
        addon_prefs = get_all_addon_prefs(context)
        sources = get_equipment_xml_sources(
            context,
            addon_prefs,
            force_bundle_refresh=force_bundle_refresh,
        )
    except Exception:
        log.debug("Failed to resolve equipment XML sources for catalog refresh", exc_info=True)
        return False

    valid_sources = [(label, path) for (label, path, is_valid) in sources if is_valid]
    if not valid_sources:
        return False

    merged_categories = {}
    merged_attributes = {}
    for _label, folder_path in valid_sources:
        _all_categories, categories_from_xml, attributes_from_xml = extract_categories_from_xml(folder_path)
        merge_equipment_xml_data(
            merged_categories,
            merged_attributes,
            categories_from_xml,
            attributes_from_xml,
        )

    if not merged_attributes:
        return False

    target_categories, target_attributes = get_equipment_catalog("w3")
    target_categories.clear()
    target_attributes.clear()
    merge_equipment_xml_data(
        target_categories,
        target_attributes,
        merged_categories,
        merged_attributes,
    )
    clear_item_attribute_identifier_lookup("w3")
    if not save_category_cache("w3"):
        return False
    _notify_template_cache_clear()
    return True


def merge_equipment_xml_data(target_categories, target_attributes, source_categories, source_attributes):
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


def strip_duplicate_xml_attributes(xml_text):
    """Pre-process XML text to remove duplicate attributes, keeping the first occurrence."""

    def _fix_tag(match):
        seen = set()

        def _keep_attr(m):
            name = m.group(1).lower()
            if name in seen:
                return ""
            seen.add(name)
            return m.group(0)

        return re.sub(
            r'([\w\-\.:]+)\s*=\s*(?:"[^"]*"|\'[^\']*\')',
            _keep_attr,
            match.group(0),
        )

    return re.sub(r'<[\w][^>]*>', _fix_tag, xml_text, flags=re.DOTALL)


def is_plaintext_xml_candidate(file_path):
    """Return False for packed/binary files that use an .xml extension."""
    try:
        with open(file_path, "rb") as f:
            raw = f.read(512)
    except Exception:
        return True
    if not raw:
        return False
    for bom in (b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff"):
        if raw.startswith(bom):
            raw = raw[len(bom):]
            break
    stripped = raw.lstrip(b" \t\r\n\x00")
    return stripped.startswith((b"<", b"#"))


def parse_xml_text_with_game_fallbacks(text):
    # ElementTree rejects unicode strings with an XML encoding declaration.
    text = _XML_DECL_RE.sub("", text, count=1).lstrip("\ufeff")
    last_exc = None
    candidates = [text]
    cleaned = _XML_HASH_COMMENT_LINE_RE.sub("", text)
    if cleaned != text:
        candidates.append(cleaned)

    for candidate in candidates:
        try:
            return ET.fromstring(candidate)
        except ET.ParseError as exc:
            last_exc = exc

        deduped = strip_duplicate_xml_attributes(candidate)
        if deduped == candidate:
            continue
        try:
            return ET.fromstring(deduped)
        except ET.ParseError as exc:
            last_exc = exc

    if last_exc is not None:
        raise last_exc
    raise ET.ParseError("empty XML text")


def parse_xml_root_with_fallbacks(file_path):
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
            return parse_xml_text_with_game_fallbacks(text)
        except Exception as exc:
            last_exc = exc
            continue

    try:
        text = raw.decode("utf-8", errors="replace")
        text = strip_duplicate_xml_attributes(text)
        return parse_xml_text_with_game_fallbacks(text)
    except Exception as exc:
        last_exc = exc

    raise last_exc
