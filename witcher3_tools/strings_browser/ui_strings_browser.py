"""Strings Browser popup + supporting operators.

Style intent: similar to the existing Witcher Asset Browser. A single popup
operator (``witcher.open_strings_browser``) brings up a dialog with the game
selector on top, source selector, search/speaker filters and a paged list of
strings. Closing the popup leaves no state behind in saved blend files
(SKIP_SAVE everywhere).
"""

from __future__ import annotations

import logging
import html
import math
import os
import re
from pathlib import Path

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)

from . import strings_sources as ss

log = logging.getLogger(__name__)


PAGE_SIZE_DEFAULT = 1000
PAGE_SIZE_MIN = 50
PAGE_SIZE_SOFT_MAX = 100000

DEFAULT_POPUP_WIDTH = 900
STRINGS_SOUND_PREVIEW_TYPE = "Strings Browser Voice"
STRINGS_ROW_ID_WIDTH = 10
_STRINGS_PAGE_SYNCING = False
_STRINGS_STATE_SYNCING = False


# ---------------------------------------------------------------------------
# Scene-level state
# ---------------------------------------------------------------------------

class WITCH_PG_StringsBrowserSpeakerItem(bpy.types.PropertyGroup):
    """Row in the Search Speakers popup.

    ``code`` is the **filter key** — matches ``WITCH_PG_StringsBrowserItem.speaker``
    (which is the resolved display name uppercased). Picking an entry writes
    this into ``settings.speaker_filter`` so the main list re-filters.

    ``raw_code`` is the 4-letter abbreviation as it appeared in the source
    (e.g. ``GRLT``). Shown next to the display name so the user can see
    which characters mapped which way.

    ``display`` is the resolved full name. ``count`` is how many records
    have this speaker in the loaded source.
    """

    code: StringProperty(default="")
    raw_code: StringProperty(default="")
    display: StringProperty(default="")
    count: IntProperty(default=0)


class WITCH_PG_StringsAssociatedPathItem(bpy.types.PropertyGroup):
    kind: StringProperty(default="")
    game: StringProperty(default=ss.GAME_W3)
    source: StringProperty(default="")
    repo_path: StringProperty(
        name="Repo Path",
        default="",
        description="Game-relative path associated with the selected string",
    )
    appearance: StringProperty(
        name="Appearance",
        default="",
        description="Entity appearance associated with this voice tag",
    )
    resolved_path: StringProperty(default="")


class WITCH_PG_StringsBrowserItem(bpy.types.PropertyGroup):
    """One row in the browser UIList (kept short — full data lives in the cache)."""

    cache_index: IntProperty(default=-1)
    game: StringProperty(default="")
    source: StringProperty(default="")
    string_id: IntProperty(default=0)
    string_id_str: StringProperty(default="")
    string_key: StringProperty(default="")
    voice_line_id: StringProperty(default="")
    voiceover: StringProperty(default="")
    has_voice: BoolProperty(default=False)
    speaker: StringProperty(default="")
    speaker_display: StringProperty(default="")
    text: StringProperty(default="")
    resource: StringProperty(default="")
    property_name: StringProperty(default="")
    db_path: StringProperty(default="")
    scene_path: StringProperty(
        name="Scene Path",
        default="",
        description="Associated game-relative .w2scene path",
    )
    entity_path: StringProperty(
        name="Template Path",
        default="",
        description="Associated game-relative character template path",
    )


def _on_filter_update(self, context):
    if _STRINGS_STATE_SYNCING:
        return
    scene = getattr(context, "scene", None)
    if scene is None:
        return
    settings = getattr(scene, "witcher_strings_browser", None)
    if settings is None:
        return
    _set_strings_filter_anchor(settings)
    # Resetting to page 0 keeps the UI consistent when the result set changes.
    if settings.page_index != 0:
        settings.page_index = 0
    _refresh_records(context)


def _on_game_or_source_update(self, context):
    """Game/source/DB-override change: discard cached records and re-read.

    Without ``force_rebuild=True`` here, an earlier failed build under the same
    cache key (game, source, db_path, language) could be re-served from the
    cache after the underlying fix was in place. Forcing a rebuild guarantees
    the user sees fresh results every time they touch the source picker.
    """
    scene = getattr(context, "scene", None)
    if scene is None:
        return
    settings = getattr(scene, "witcher_strings_browser", None)
    if settings is None:
        return
    if _STRINGS_STATE_SYNCING:
        return
    previous_game = str(getattr(settings, "last_active_game", "") or settings.active_game or "").upper()
    current_game = str(settings.active_game or "").upper()
    if previous_game and previous_game != current_game:
        _save_strings_browser_state(settings, game=previous_game)
        _restore_strings_browser_state(settings, game=current_game)
    settings.last_active_game = current_game
    if hasattr(settings, "previous_selected_string_id"):
        settings.previous_selected_string_id = ""
    if hasattr(settings, "filter_anchor_string_id"):
        settings.filter_anchor_string_id = ""
    _refresh_records(context, force_rebuild=True)


def _state_prefix_for_game(game):
    game = str(game or "").upper()
    return "w2" if game == ss.GAME_W2 else "w3"


def _selected_string_state_id(item):
    if item is None:
        return ""
    for attr in ("string_id_str", "string_key", "voice_line_id"):
        value = str(getattr(item, attr, "") or "").strip()
        if value:
            return value
    return ""


def _string_record_state_id(rec):
    if not isinstance(rec, dict):
        return ""
    for key in ("string_id_str", "string_key", "voice_line_id"):
        value = str(rec.get(key, "") or "").strip()
        if value:
            return value
    return ""


def _normalize_scene_filter_path(value):
    return str(value or "").strip().replace("/", "\\").lstrip("\\").lower()


def _record_matches_scene_filter(rec, scene_filter):
    scene_filter = _normalize_scene_filter_path(scene_filter)
    if not scene_filter:
        return True
    paths = []
    for value in rec.get("source_scenes", []) or []:
        norm = _normalize_scene_filter_path(value)
        if norm and norm not in paths:
            paths.append(norm)
    primary = _normalize_scene_filter_path(rec.get("scene_path", ""))
    if primary and primary not in paths:
        paths.insert(0, primary)
    return scene_filter in paths


def _record_has_text(rec):
    if not isinstance(rec, dict):
        return False
    return bool(str(rec.get("text", "") or "").strip())


def _normalize_resource_filter_value(value):
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.search(r'"([^"]+)"', text)
    if match:
        text = match.group(1).strip()
    text = text.replace("/", "\\").lstrip("\\").lower()
    return re.sub(r"\s+", " ", text)


def _record_matches_resource_filter(rec, resource_filter):
    resource_filter = _normalize_resource_filter_value(resource_filter)
    if not resource_filter:
        return True
    candidates = [
        rec.get("resource", ""),
        rec.get("scene_path", ""),
    ]
    for value in rec.get("source_scenes", []) or []:
        candidates.append(value)
    for value in candidates:
        norm = _normalize_resource_filter_value(value)
        if norm and (norm == resource_filter or resource_filter in norm):
            return True
    return False


def _record_has_voice_tag(rec):
    if not isinstance(rec, dict):
        return False
    value = (
        str(rec.get("speaker", "") or "").strip()
        or str(rec.get("speaker_display", "") or "").strip()
    )
    if not value:
        return False
    return value.strip().upper() not in {"UNKN", "UNKNOWN", "VO"}


def _saved_selected_string_id(settings, game=None):
    prefix = _state_prefix_for_game(game or getattr(settings, "active_game", ss.GAME_W3))
    return str(getattr(settings, f"{prefix}_selected_string_id", "") or "").strip()


def _set_saved_selected_string_id(settings, value, game=None):
    prefix = _state_prefix_for_game(game or getattr(settings, "active_game", ss.GAME_W3))
    setattr(settings, f"{prefix}_selected_string_id", str(value or "").strip())


def _set_strings_filter_anchor(settings, value=None):
    if settings is None or not hasattr(settings, "filter_anchor_string_id"):
        return ""
    if value is None:
        selected = None
        if 0 <= int(getattr(settings, "active_index", -1) or -1) < len(settings.items):
            selected = settings.items[settings.active_index]
        value = _selected_string_state_id(selected)
    value = str(value or "").strip()
    if value:
        settings.filter_anchor_string_id = value
    return value


def _clear_strings_filter_anchor(settings, value=""):
    if settings is None or not hasattr(settings, "filter_anchor_string_id"):
        return
    if not value or str(getattr(settings, "filter_anchor_string_id", "") or "") == str(value or ""):
        settings.filter_anchor_string_id = ""


def _save_strings_browser_state(settings, game=None):
    if settings is None:
        return
    game = str(game or getattr(settings, "active_game", ss.GAME_W3) or ss.GAME_W3).upper()
    prefix = _state_prefix_for_game(game)
    try:
        setattr(settings, f"{prefix}_page_index", int(settings.page_index or 0))
    except Exception:
        setattr(settings, f"{prefix}_page_index", 0)
    setattr(settings, f"{prefix}_search_text", str(getattr(settings, "search_text", "") or ""))
    setattr(settings, f"{prefix}_speaker_filter", str(getattr(settings, "speaker_filter", "") or ""))
    setattr(settings, f"{prefix}_scene_filter", str(getattr(settings, "scene_filter", "") or ""))
    setattr(settings, f"{prefix}_resource_filter", str(getattr(settings, "resource_filter", "") or ""))
    setattr(settings, f"{prefix}_only_voicetagged", bool(getattr(settings, "only_voicetagged", False)))
    setattr(settings, f"{prefix}_only_with_text", bool(getattr(settings, "only_with_text", True)))

    selected = None
    if 0 <= int(getattr(settings, "active_index", -1) or -1) < len(settings.items):
        selected = settings.items[settings.active_index]
    selected_id = _selected_string_state_id(selected)
    if selected_id:
        _set_saved_selected_string_id(settings, selected_id, game=game)


def _restore_strings_browser_state(settings, game=None):
    global _STRINGS_STATE_SYNCING
    if settings is None:
        return
    game = str(game or getattr(settings, "active_game", ss.GAME_W3) or ss.GAME_W3).upper()
    prefix = _state_prefix_for_game(game)

    _STRINGS_STATE_SYNCING = True
    try:
        settings.search_text = str(getattr(settings, f"{prefix}_search_text", "") or "")
        settings.speaker_filter = str(getattr(settings, f"{prefix}_speaker_filter", "") or "")
        settings.scene_filter = str(getattr(settings, f"{prefix}_scene_filter", "") or "")
        settings.resource_filter = str(getattr(settings, f"{prefix}_resource_filter", "") or "")
        settings.only_voicetagged = bool(getattr(settings, f"{prefix}_only_voicetagged", False))
        settings.only_with_text = bool(getattr(settings, f"{prefix}_only_with_text", True))
        settings.page_index = max(0, int(getattr(settings, f"{prefix}_page_index", 0) or 0))
        _sync_strings_page_number(settings)
    finally:
        _STRINGS_STATE_SYNCING = False


def _on_active_index_update(self, context):
    selected = None
    if 0 <= int(getattr(self, "active_index", -1) or -1) < len(self.items):
        selected = self.items[self.active_index]
    selected_id = _selected_string_state_id(selected)
    previous_id = _saved_selected_string_id(self)
    if previous_id and selected_id and previous_id != selected_id and hasattr(self, "previous_selected_string_id"):
        self.previous_selected_string_id = previous_id
    _save_strings_browser_state(self)
    if hasattr(self, "show_all_voicetag_entities"):
        self.show_all_voicetag_entities = False
    _refresh_selected_string_associated_paths(context)


def _strings_total_pages(settings):
    if settings is None:
        return 1
    page_size = max(PAGE_SIZE_MIN, settings.page_size or PAGE_SIZE_DEFAULT)
    return max(1, int(math.ceil(settings.last_filtered / page_size)))


def _addon_preferences(context):
    try:
        package_root = __package__.split(".")[0]
        return context.preferences.addons[package_root].preferences
    except Exception:
        return None


def _strings_page_size_pref(context):
    prefs = _addon_preferences(context)
    try:
        value = int(getattr(prefs, "strings_browser_page_size", 0) or 0)
    except Exception:
        value = 0
    return max(PAGE_SIZE_MIN, value or PAGE_SIZE_DEFAULT)


def _save_strings_page_size_pref(context, page_size):
    prefs = _addon_preferences(context)
    if prefs is None or not hasattr(prefs, "strings_browser_page_size"):
        return
    try:
        value = max(PAGE_SIZE_MIN, int(page_size or PAGE_SIZE_DEFAULT))
    except Exception:
        value = PAGE_SIZE_DEFAULT
    try:
        if int(getattr(prefs, "strings_browser_page_size", 0) or 0) != value:
            prefs.strings_browser_page_size = value
    except Exception:
        pass


def _sync_strings_page_size_from_pref(context, settings):
    if settings is None:
        return
    preferred = _strings_page_size_pref(context)
    if int(getattr(settings, "page_size", 0) or 0) != preferred:
        settings.page_size = preferred


def _sync_strings_page_number(settings, total_pages=None):
    global _STRINGS_PAGE_SYNCING
    if settings is None or not hasattr(settings, "page_number"):
        return
    if total_pages is None:
        total_pages = _strings_total_pages(settings)
    page_number = max(1, min(int(settings.page_index or 0) + 1, int(total_pages or 1)))
    if int(getattr(settings, "page_number", 1) or 1) == page_number:
        return
    _STRINGS_PAGE_SYNCING = True
    try:
        settings.page_number = page_number
    finally:
        _STRINGS_PAGE_SYNCING = False


def _on_page_number_update(self, context):
    global _STRINGS_PAGE_SYNCING
    if _STRINGS_PAGE_SYNCING:
        return
    settings = self
    total_pages = _strings_total_pages(settings)
    try:
        page_number = int(settings.page_number or 1)
    except Exception:
        page_number = 1
    page_number = max(1, min(page_number, total_pages))
    if settings.page_number != page_number:
        _STRINGS_PAGE_SYNCING = True
        try:
            settings.page_number = page_number
        finally:
            _STRINGS_PAGE_SYNCING = False
    target_index = page_number - 1
    if settings.page_index != target_index:
        settings.page_index = target_index
        _refresh_page(context, settings, select_first=True)


def _on_page_size_update(self, context):
    settings = self
    clamped = max(PAGE_SIZE_MIN, settings.page_size or PAGE_SIZE_DEFAULT)
    if settings.page_size != clamped:
        settings.page_size = clamped
        return
    _save_strings_page_size_pref(context, clamped)
    _refresh_page(
        context,
        settings,
        selected_id=_saved_selected_string_id(settings),
        jump_to_selected=True,
    )


class WITCH_PG_StringsBrowserSettings(bpy.types.PropertyGroup):
    """Per-scene browser state (active game, filters, paging)."""

    active_game: EnumProperty(
        name="Game",
        description="Which game's strings to browse",
        items=(
            (ss.GAME_W3, "Witcher 3", "Browse Witcher 3 strings"),
            (ss.GAME_W2, "Witcher 2", "Browse Witcher 2 strings"),
        ),
        default=ss.GAME_W3,
        update=_on_game_or_source_update,
    )

    w3_source: EnumProperty(
        name="Witcher 3 Source",
        description="Which Witcher 3 string source to display",
        items=(
            (ss.SOURCE_W3STRINGS, "Binary (w3strings)", "Use the cached game .w3strings binary tables"),
            (ss.SOURCE_SQLITE, "REDkit DB (SQLite)", "Use a REDkit LocalEditorStringDataBaseW3_UTF8(_mod).db file"),
        ),
        default=ss.SOURCE_W3STRINGS,
        update=_on_game_or_source_update,
    )

    w2_source: EnumProperty(
        name="Witcher 2 Source",
        description="Which Witcher 2 string source to display",
        items=(
            (ss.SOURCE_W2STRINGS, "Cooked (w2strings)", "Use the shipped CookedPC/<lang>0.w2strings binary table"),
            (ss.SOURCE_SQLITE, "REDkit DB (SQLite)", "Use a REDkit base.sqlite / user.sqlite file from the W2 bin folder"),
        ),
        default=ss.SOURCE_W2STRINGS,
        update=_on_game_or_source_update,
    )

    w3_db_path: StringProperty(
        name="W3 DB Override",
        description=(
            "Optional path to a Witcher 3 REDkit .db file or its folder "
            "(e.g. the REDkit install root, or its r4data subfolder). "
            "Leave blank to auto-detect from the configured paths"
        ),
        default="",
        subtype='FILE_PATH',
        update=_on_game_or_source_update,
    )
    w2_db_path: StringProperty(
        name="W2 DB Override",
        description=(
            "Optional path to a Witcher 2 base.sqlite / user.sqlite file or "
            "its parent folder (e.g. the W2 game install or its bin subfolder). "
            "Leave blank to auto-detect from the configured paths"
        ),
        default="",
        subtype='FILE_PATH',
        update=_on_game_or_source_update,
    )

    search_text: StringProperty(
        name="Search",
        description="Filter the list by ID, key, voiceover, speaker or text (substring AND)",
        default="",
        update=_on_filter_update,
    )
    speaker_filter: StringProperty(
        name="Speaker",
        description="Show only lines spoken by this speaker",
        default="",
        update=_on_filter_update,
    )
    scene_filter: StringProperty(
        name="Scene",
        description="Show only strings associated with this .w2scene repo path",
        default="",
        update=_on_filter_update,
    )
    resource_filter: StringProperty(
        name="Resource",
        description="Show only strings whose REDkit resource matches this value",
        default="",
        update=_on_filter_update,
    )
    only_voicetagged: BoolProperty(
        name="Only VoiceTagged",
        description="Show only strings with a resolved speaker voice tag",
        default=False,
        update=_on_filter_update,
    )
    only_with_text: BoolProperty(
        name="Only With Text",
        description="Hide string records whose localized text is blank",
        default=True,
        update=_on_filter_update,
    )
    format_selected_text: BoolProperty(
        name="Format Text",
        description="Render simple string markup such as <br> and hide inline style tags in the selected string preview",
        default=True,
        options={'SKIP_SAVE'},
    )

    page_size: IntProperty(
        name="Rows",
        default=PAGE_SIZE_DEFAULT,
        min=PAGE_SIZE_MIN,
        soft_max=PAGE_SIZE_SOFT_MAX,
        update=_on_page_size_update,
    )
    page_index: IntProperty(default=0)
    page_number: IntProperty(
        name="Page",
        default=1,
        min=1,
        soft_min=1,
        update=_on_page_number_update,
    )
    last_active_game: StringProperty(default=ss.GAME_W3)
    w3_page_index: IntProperty(default=0)
    w2_page_index: IntProperty(default=0)
    w3_selected_string_id: StringProperty(default="")
    w2_selected_string_id: StringProperty(default="")
    w3_search_text: StringProperty(default="")
    w2_search_text: StringProperty(default="")
    w3_speaker_filter: StringProperty(default="")
    w2_speaker_filter: StringProperty(default="")
    w3_scene_filter: StringProperty(default="")
    w2_scene_filter: StringProperty(default="")
    w3_resource_filter: StringProperty(default="")
    w2_resource_filter: StringProperty(default="")
    w3_only_voicetagged: BoolProperty(default=False)
    w2_only_voicetagged: BoolProperty(default=False)
    w3_only_with_text: BoolProperty(default=True)
    w2_only_with_text: BoolProperty(default=True)
    previous_selected_string_id: StringProperty(default="", options={'SKIP_SAVE'})
    filter_anchor_string_id: StringProperty(default="", options={'SKIP_SAVE'})
    last_total: IntProperty(default=0)
    last_filtered: IntProperty(default=0)
    last_built_count: IntProperty(default=0)
    last_error: StringProperty(default="")
    last_db_used: StringProperty(default="")

    items: CollectionProperty(type=WITCH_PG_StringsBrowserItem)
    associated_paths: CollectionProperty(
        type=WITCH_PG_StringsAssociatedPathItem,
        options={'SKIP_SAVE'},
    )
    associated_paths_key: StringProperty(default="", options={'SKIP_SAVE'})
    show_all_voicetag_entities: BoolProperty(
        name="Show VoiceTag Entities",
        default=False,
        options={'SKIP_SAVE'},
    )
    active_index: IntProperty(default=-1, update=_on_active_index_update)


# ---------------------------------------------------------------------------
# Cache + rebuild
# ---------------------------------------------------------------------------

# Holds the currently displayed filtered record list so we don't repeatedly
# rebuild PropertyGroups for every redraw. Keyed by (game, source, db_path).
_FILTERED_CACHE = {"records": [], "key": None}
_SCENE_DIALOG_METADATA_CACHE = {}
_SCENE_DIALOG_LINE_CACHE = {}
_SCENE_DIALOG_FULL_LINE_CACHE = {}
_SCENE_DIALOG_SUMMARY_CACHE = {}
_SCENE_AUGMENTED_RECORDS_CACHE = {"key": None, "records": None}
_VO_LINE_ID_RE = re.compile(r"\bVO(?:ICE)?(?:_?ID)?_?0*([0-9]+)\b", re.IGNORECASE)


def _resolve_source_and_db(settings):
    game = settings.active_game
    if game == ss.GAME_W3:
        source = settings.w3_source
        db_override = settings.w3_db_path
    else:
        source = settings.w2_source
        db_override = settings.w2_db_path

    db_path = ""
    if source == ss.SOURCE_SQLITE:
        db_paths = ss.find_string_db_paths(game, override_path=db_override)
        if db_paths:
            db_path = str(db_paths[0])
    elif source == ss.SOURCE_W2STRINGS:
        # The W2 binary path uses the override field as a hint and falls back
        # to the configured game roots. Pass through so the cache key stays
        # stable across runs.
        db_path = db_override or ""
    return game, source, db_path


def _record_has_voice(rec):
    """Whether this record has enough voice metadata to offer preview."""

    if not _record_has_text(rec):
        return False
    game = str(rec.get("game", "") or "").upper()
    if game == ss.GAME_W2:
        return bool(str(rec.get("voice_line_id", "") or "").strip() or _voice_line_candidates_for_record(rec))
    if game != ss.GAME_W3:
        return False
    if not str(rec.get("string_id_str", "") or "").strip():
        return False
    return bool(
        str(rec.get("voiceover", "") or "").strip()
        or str(rec.get("speaker_code", "") or "").strip()
    )


def _scene_dialog_metadata(game):
    game = str(game or "").strip().upper()
    if game not in {ss.GAME_W2, ss.GAME_W3}:
        return None
    if game in _SCENE_DIALOG_METADATA_CACHE:
        return _SCENE_DIALOG_METADATA_CACHE.get(game)
    metadata = None
    try:
        from ..CR2W.witcher_cache.SceneDialog.scene_dialog_index import LoadSceneDialogIndexMetadata

        metadata = LoadSceneDialogIndexMetadata(game)
    except Exception:
        log.debug("Scene dialogue index is unavailable for %s strings.", game, exc_info=True)
    _SCENE_DIALOG_METADATA_CACHE[game] = metadata
    return metadata


def _normalize_scene_dialog_line_id(game, line_id):
    game = str(game or "").strip().upper()
    line_id = str(line_id or "").strip()
    if game == ss.GAME_W2 and line_id.upper().startswith("VO_ID"):
        line_id = line_id[5:]
    if game in {ss.GAME_W2, ss.GAME_W3} and line_id.isdigit():
        return str(int(line_id))
    return line_id


def _scene_dialog_summaries(game):
    game = str(game or "").strip().upper()
    if game in _SCENE_DIALOG_SUMMARY_CACHE:
        return _SCENE_DIALOG_SUMMARY_CACHE.get(game) or {}
    metadata = _scene_dialog_metadata(game)
    summaries = {}
    if metadata is not None and hasattr(metadata, "preload_line_summaries"):
        try:
            summaries = metadata.preload_line_summaries() or {}
        except Exception:
            log.debug("Failed to preload %s scene dialogue summaries for strings.", game, exc_info=True)
            summaries = {}
    _SCENE_DIALOG_SUMMARY_CACHE[game] = summaries
    return summaries


def _coerce_scene_dialog_info(info):
    if not isinstance(info, dict):
        info = {}
    source_scenes = [
        str(path or "").strip().replace("/", "\\")
        for path in (info.get("source_scenes", []) or [])
        if str(path or "").strip()
    ]
    scene_path = str(info.get("scene_path", "") or "").strip().replace("/", "\\")
    if scene_path and scene_path not in source_scenes:
        source_scenes.insert(0, scene_path)
    entity_paths = []
    for entity in (info.get("entity_paths", []) or []):
        if isinstance(entity, dict):
            path = str(entity.get("path", "") or "").strip().replace("/", "\\")
            if not path:
                continue
            item = {"path": path}
            appearance = str(entity.get("appearance", "") or "").strip()
            source = str(entity.get("source", "") or "").strip()
            if appearance:
                item["appearance"] = appearance
            if source:
                item["source"] = source
            entity_paths.append(item)
        else:
            path = str(entity or "").strip().replace("/", "\\")
            if path:
                entity_paths.append({"path": path})
    return {
        "speaker": str(info.get("speaker", "") or "").strip().upper(),
        "scene_path": scene_path,
        "source_scenes": source_scenes,
        "entity_path": str(info.get("entity_path", "") or "").strip().replace("/", "\\"),
        "entity_paths": entity_paths,
    }


def _scene_dialog_line_info(game, line_id, summaries=None):
    game = str(game or "").strip().upper()
    line_id = _normalize_scene_dialog_line_id(game, line_id)
    if not game or not line_id:
        return {}

    cache_key = (game, line_id)
    cached = _SCENE_DIALOG_LINE_CACHE.get(cache_key)
    if cached is not None:
        return cached

    if summaries is not None:
        out = _coerce_scene_dialog_info(summaries.get(line_id) or {})
        _SCENE_DIALOG_LINE_CACHE[cache_key] = out
        return out

    metadata = _scene_dialog_metadata(game)
    info = {}
    if metadata is not None:
        try:
            info = metadata.get_line(line_id) or {}
        except Exception:
            log.debug("Failed to read %s scene dialogue info for line %s.", game, line_id, exc_info=True)
            info = {}
    if not isinstance(info, dict):
        info = {}

    out = _coerce_scene_dialog_info(info)
    _SCENE_DIALOG_LINE_CACHE[cache_key] = out
    return out


def _scene_dialog_full_line_info(game, line_id):
    game = str(game or "").strip().upper()
    line_id = _normalize_scene_dialog_line_id(game, line_id)
    if not game or not line_id:
        return {}
    cache_key = (game, line_id)
    cached = _SCENE_DIALOG_FULL_LINE_CACHE.get(cache_key)
    if cached is not None:
        return cached
    metadata = _scene_dialog_metadata(game)
    info = {}
    if metadata is not None:
        try:
            info = metadata.get_line(line_id) or {}
        except Exception:
            log.debug("Failed to read full %s scene dialogue info for line %s.", game, line_id, exc_info=True)
            info = {}
    out = _coerce_scene_dialog_info(info)
    _SCENE_DIALOG_FULL_LINE_CACHE[cache_key] = out
    return out


def _dialog_line_candidates_for_record(rec):
    game = str(rec.get("game", "") or "").upper()
    candidates = []
    seen = set()

    def add(value):
        value = _normalize_scene_dialog_line_id(game, value)
        if not value or value in seen:
            return
        seen.add(value)
        candidates.append(value)

    for field in ("string_key", "voiceover"):
        text = str(rec.get(field, "") or "")
        for match in _VO_LINE_ID_RE.finditer(text):
            add(match.group(1))

    add(rec.get("string_id_str", ""))
    add(rec.get("string_id", ""))
    return candidates


def _voice_line_candidates_for_record(rec):
    game = str(rec.get("game", "") or "").upper()
    if game != ss.GAME_W2:
        return _dialog_line_candidates_for_record(rec)

    candidates = []
    seen = set()

    def add(value):
        value = _normalize_scene_dialog_line_id(game, value)
        if not value or value in seen:
            return
        seen.add(value)
        candidates.append(value)

    for field in ("string_key", "voiceover"):
        text = str(rec.get(field, "") or "")
        for match in _VO_LINE_ID_RE.finditer(text):
            add(match.group(1))
    return candidates


def _record_with_scene_dialog_info(rec, summaries=None):
    game = str(rec.get("game", "") or "").upper()
    candidates = _dialog_line_candidates_for_record(rec)
    voice_candidates = _voice_line_candidates_for_record(rec)
    if voice_candidates and not str(rec.get("voice_line_id", "") or "").strip():
        rec = dict(rec)
        rec["voice_line_id"] = voice_candidates[0]

    for line_id in candidates:
        info = _scene_dialog_line_info(game, line_id, summaries=summaries)
        if not any(info.values()):
            continue

        out = dict(rec)
        if not str(out.get("voice_line_id", "") or "").strip():
            out["voice_line_id"] = line_id
        if info.get("speaker"):
            out["speaker"] = info["speaker"]
            out["speaker_display"] = info["speaker"]
        if info.get("scene_path"):
            out["scene_path"] = info["scene_path"]
        if info.get("source_scenes"):
            out["source_scenes"] = list(info.get("source_scenes", []) or [])
        if info.get("entity_path"):
            out["entity_path"] = info["entity_path"]

        search_blob = str(out.get("search_blob", "") or "")
        extra_blob = " ".join(
            value.lower()
            for value in (
                out.get("speaker", ""),
                out.get("scene_path", ""),
                out.get("entity_path", ""),
                " ".join(out.get("source_scenes", []) or []),
            )
            if value
        )
        out["search_blob"] = f"{search_blob} {extra_blob}".strip()
        return out
    return rec


def _records_with_scene_dialog_info(records, cache_key=None):
    if cache_key is not None and _SCENE_AUGMENTED_RECORDS_CACHE.get("key") == cache_key:
        cached = _SCENE_AUGMENTED_RECORDS_CACHE.get("records")
        if cached is not None:
            return cached

    by_game = {}
    for rec in records or []:
        by_game.setdefault(str(rec.get("game", "") or "").upper(), None)
    summaries_by_game = {game: _scene_dialog_summaries(game) for game in by_game if game in {ss.GAME_W2, ss.GAME_W3}}
    enriched = [
        _record_with_scene_dialog_info(rec, summaries=summaries_by_game.get(str(rec.get("game", "") or "").upper()))
        for rec in (records or [])
    ]
    if cache_key is not None:
        _SCENE_AUGMENTED_RECORDS_CACHE["key"] = cache_key
        _SCENE_AUGMENTED_RECORDS_CACHE["records"] = enriched
    return enriched


def _add_strings_associated_path(entries, seen, *, kind, game, repo_path, appearance="", source=""):
    repo_path = str(repo_path or "").strip().replace("/", "\\")
    if not repo_path:
        return
    appearance = str(appearance or "").strip()
    key = (kind, repo_path.lower(), appearance.lower())
    if key in seen:
        return
    seen.add(key)
    entries.append({
        "kind": kind,
        "game": str(game or ss.GAME_W3).upper(),
        "repo_path": repo_path,
        "appearance": appearance,
        "source": str(source or ""),
    })


def _strings_append_entity_path(entities, seen, value):
    if isinstance(value, dict):
        repo_path = str(value.get("path", "") or "").strip().replace("/", "\\")
        appearance = str(value.get("appearance", "") or "").strip()
        source = str(value.get("source", "") or "").strip()
    else:
        repo_path = str(value or "").strip().replace("/", "\\")
        appearance = ""
        source = ""
    if not repo_path:
        return
    key = (repo_path.lower(), appearance.lower())
    if key in seen:
        return
    seen.add(key)
    item = {"path": repo_path, "appearance": appearance}
    if source:
        item["source"] = source
    entities.append(item)


def _strings_entity_path(entity):
    return str(entity.get("path", "") if isinstance(entity, dict) else entity or "").strip().replace("/", "\\")


def _strings_entity_appearance(entity):
    return str(entity.get("appearance", "") if isinstance(entity, dict) else "").strip()


def _strings_voicetag_entity_paths(line_info):
    rows = []
    seen = set()
    for entity in (line_info.get("voice_tag_entity_paths", []) or []):
        path = _strings_entity_path(entity)
        appearance = _strings_entity_appearance(entity)
        if not path:
            continue
        key = (path.lower(), appearance.lower())
        if key in seen:
            continue
        seen.add(key)
        rows.append({"path": path, "appearance": appearance, "source": "entity_voice_tag"})
    for entity in (line_info.get("entity_paths", []) or []):
        if not isinstance(entity, dict):
            continue
        source = str(entity.get("source", "") or "")
        appearance = _strings_entity_appearance(entity)
        if source and source != "entity_voice_tag":
            continue
        if source != "entity_voice_tag" and not appearance:
            continue
        path = _strings_entity_path(entity)
        if not path:
            continue
        key = (path.lower(), appearance.lower())
        if key in seen:
            continue
        seen.add(key)
        rows.append({"path": path, "appearance": appearance, "source": "entity_voice_tag"})
    return rows


def _strings_scene_actor_entities(line_info, item):
    entity_path = str(line_info.get("entity_path", "") or getattr(item, "entity_path", "") or "").strip().replace("/", "\\")
    rows = []
    seen = set()
    for entity in (line_info.get("entity_paths", []) or []):
        if not isinstance(entity, dict):
            continue
        if str(entity.get("source", "") or "") != "scene_actor":
            continue
        path = _strings_entity_path(entity)
        if not path:
            continue
        appearance = _strings_entity_appearance(entity)
        key = (path.lower(), appearance.lower())
        if key in seen:
            continue
        seen.add(key)
        row = {"path": path, "source": "scene_actor"}
        if appearance:
            row["appearance"] = appearance
        rows.append(row)
    if rows:
        return rows
    if not entity_path:
        return []
    matches = [
        entity for entity in _strings_voicetag_entity_paths(line_info)
        if _strings_entity_path(entity).lower() == entity_path.lower() and _strings_entity_appearance(entity)
    ]
    result = {"path": entity_path, "source": "scene_actor"}
    if len(matches) == 1:
        result["appearance"] = _strings_entity_appearance(matches[0])
    return [result]


def _strings_scene_actor_entity(line_info, item):
    rows = _strings_scene_actor_entities(line_info, item)
    return rows[0] if rows else {}


def _selected_string_record(item):
    if item is None:
        return {}
    return {
        "game": str(getattr(item, "game", "") or ss.GAME_W3).upper(),
        "string_id_str": str(getattr(item, "string_id_str", "") or ""),
        "string_id": str(getattr(item, "string_id", "") or ""),
        "string_key": str(getattr(item, "string_key", "") or ""),
        "voice_line_id": str(getattr(item, "voice_line_id", "") or ""),
        "voiceover": str(getattr(item, "voiceover", "") or ""),
    }


def _selected_string_associated_paths(item, *, max_scenes=5, max_entities=64, include_voicetag_entities=False):
    if item is None:
        return []
    game = str(getattr(item, "game", "") or ss.GAME_W3).upper()
    line_ids = []
    seen_line_ids = set()

    def add_line_id(value):
        value = _normalize_scene_dialog_line_id(game, value)
        if value and value not in seen_line_ids:
            seen_line_ids.add(value)
            line_ids.append(value)

    add_line_id(getattr(item, "voice_line_id", ""))
    for candidate in _dialog_line_candidates_for_record(_selected_string_record(item)):
        add_line_id(candidate)

    line_info = {}
    for line_id in line_ids:
        line_info = _scene_dialog_full_line_info(game, line_id)
        if any(line_info.values()):
            break

    entries = []
    seen = set()
    source_scenes = list(line_info.get("source_scenes", []) or [])
    item_scene = str(getattr(item, "scene_path", "") or "").strip().replace("/", "\\")
    if item_scene and item_scene not in source_scenes:
        source_scenes.insert(0, item_scene)
    for scene_path in source_scenes[:max_scenes]:
        _add_strings_associated_path(entries, seen, kind="scene", game=game, repo_path=scene_path)

    entity_paths = []
    entity_seen = set()
    for scene_actor in _strings_scene_actor_entities(line_info, item):
        _strings_append_entity_path(entity_paths, entity_seen, scene_actor)
    if include_voicetag_entities:
        for entity in _strings_voicetag_entity_paths(line_info):
            _strings_append_entity_path(entity_paths, entity_seen, entity)

    for entity in entity_paths[:max_entities]:
        _add_strings_associated_path(
            entries,
            seen,
            kind="entity",
            game=game,
            repo_path=entity.get("path", ""),
            appearance=entity.get("appearance", ""),
            source=entity.get("source", ""),
        )
    return entries


def _selected_string_voicetag_entity_count(item):
    if item is None:
        return 0
    game = str(getattr(item, "game", "") or ss.GAME_W3).upper()
    line_info = {}
    line_ids = []
    seen_line_ids = set()

    def add_line_id(value):
        value = _normalize_scene_dialog_line_id(game, value)
        if value and value not in seen_line_ids:
            seen_line_ids.add(value)
            line_ids.append(value)

    add_line_id(getattr(item, "voice_line_id", ""))
    for candidate in _dialog_line_candidates_for_record(_selected_string_record(item)):
        add_line_id(candidate)
    for line_id in line_ids:
        line_info = _scene_dialog_full_line_info(game, line_id)
        if any(line_info.values()):
            break
    scene_keys = {
        (
            _strings_entity_path(scene_actor).lower(),
            _strings_entity_appearance(scene_actor).lower(),
        )
        for scene_actor in _strings_scene_actor_entities(line_info, item)
        if scene_actor
    }
    count = 0
    for entity in _strings_voicetag_entity_paths(line_info):
        key = (_strings_entity_path(entity).lower(), _strings_entity_appearance(entity).lower())
        if key in scene_keys:
            continue
        count += 1
    return count


def _selected_string_associated_paths_key(item, show_all=False):
    if item is None:
        return ""
    return "|".join((
        str(getattr(item, "game", "") or ""),
        str(getattr(item, "string_id_str", "") or ""),
        str(getattr(item, "voice_line_id", "") or ""),
        str(getattr(item, "scene_path", "") or ""),
        str(getattr(item, "entity_path", "") or ""),
        "all" if show_all else "scene",
    ))


def _refresh_selected_string_associated_paths(context, *, force=False):
    settings = getattr(getattr(context, "scene", None), "witcher_strings_browser", None) if context is not None else None
    if settings is None or not hasattr(settings, "associated_paths"):
        return
    item = settings.items[settings.active_index] if 0 <= settings.active_index < len(settings.items) else None
    show_all = bool(getattr(settings, "show_all_voicetag_entities", False))
    key = _selected_string_associated_paths_key(item, show_all=show_all)
    if not force and getattr(settings, "associated_paths_key", "") == key:
        return
    settings.associated_paths.clear()
    if not key:
        settings.associated_paths_key = ""
        return
    include_all = bool(getattr(settings, "show_all_voicetag_entities", False))
    for entry in _selected_string_associated_paths(item, include_voicetag_entities=include_all):
        assoc = settings.associated_paths.add()
        assoc.kind = entry.get("kind", "")
        assoc.game = entry.get("game", ss.GAME_W3)
        assoc.source = entry.get("source", "")
        assoc.repo_path = entry.get("repo_path", "")
        assoc.appearance = entry.get("appearance", "")
        assoc.resolved_path = ""
    settings.associated_paths_key = key


def _draw_strings_associated_path(layout, assoc):
    repo_path = str(getattr(assoc, "repo_path", "") or "").strip()
    if not repo_path:
        return

    kind = str(getattr(assoc, "kind", "") or "")
    game = str(getattr(assoc, "game", "") or ss.GAME_W3).upper()
    appearance = str(getattr(assoc, "appearance", "") or "")
    source = str(getattr(assoc, "source", "") or "")
    icon = 'OUTLINER_OB_ARMATURE' if kind == "entity" else 'SCENE_DATA'

    row = layout.row(align=True)
    row.label(text="", icon=icon)
    if kind == "entity":
        row.label(text="", icon='USER' if source == "scene_actor" else 'COMMUNITY')
    row.prop(assoc, "repo_path", text="")
    if appearance:
        row.prop(assoc, "appearance", text="")
    if kind == "scene":
        filter_op = row.operator("witcher.strings_browser_filter_scene", text="", icon='FILTER')
        filter_op.scene_path = repo_path
        filter_op.clear_other_filters = True
    copy_op = row.operator("witcher.quick_voice_copy_associated_path", text="", icon='COPYDOWN')
    copy_op.path = repo_path
    open_op = row.operator("witcher.quick_voice_open_associated_path", text="", icon='FILEBROWSER')
    open_op.repo_path = repo_path
    open_op.game = game
    if kind == "entity":
        import_op = row.operator("witcher.quick_voice_import_associated_entity", text="", icon='IMPORT')
        import_op.repo_path = repo_path
        import_op.game = game
        import_op.appearance = appearance
    elif kind == "scene" and game == ss.GAME_W3:
        import_op = row.operator("witcher.quick_voice_import_associated_scene", text="", icon='IMPORT')
        import_op.repo_path = repo_path
        import_op.game = game


def _strings_copy_button(row, field):
    op = row.operator("witcher.strings_browser_copy", text="", icon='COPYDOWN')
    op.field = field
    return op


def _draw_selected_string_value(
    layout,
    label,
    value,
    copy_field,
    *,
    filter_operator="",
    filter_attr="",
    filter_value="",
):
    value = str(value or "")
    if not value:
        return
    row = layout.row(align=True)
    row.alignment = 'LEFT'
    row.label(text=f"{label}: {value}")
    if filter_operator and filter_attr:
        op = row.operator(filter_operator, text="", icon='FILTER')
        setattr(op, filter_attr, str(filter_value or value))
        if hasattr(op, "clear_other_filters"):
            op.clear_other_filters = True
    _strings_copy_button(row, copy_field)


def _strings_browser_row_label(item):
    string_id = str(getattr(item, "string_id_str", "") or "").strip() or "-"
    if len(string_id) < STRINGS_ROW_ID_WIDTH:
        string_id = string_id.ljust(STRINGS_ROW_ID_WIDTH)

    voice_tag = (
        str(getattr(item, "speaker", "") or "").strip()
        or str(getattr(item, "speaker_display", "") or "").strip()
    )
    text = str(getattr(item, "text", "") or "<no text>")
    if voice_tag:
        return f"{string_id} [{voice_tag.upper()}] {text}"
    return f"{string_id} {text}"


def _voice_preview_item_key(game, line_id):
    game = str(game or ss.GAME_W3).strip().upper()
    line_id = str(line_id or "").strip()
    return f"strings_browser\\{game.lower()}\\speech\\{line_id}" if line_id else ""


def _is_voice_preview_playing(game, line_id):
    key = _voice_preview_item_key(game, line_id)
    if not key:
        return False
    try:
        from ..ui import ui_file_browser

        return ui_file_browser.sound_preview_matches(STRINGS_SOUND_PREVIEW_TYPE, key)
    except Exception:
        return False


def _draw_strings_language_selector(layout, context, *, boxed=True):
    scene = getattr(context, "scene", None) if context is not None else None
    if scene is None:
        return False

    try:
        from .. import dialog_language
        from ..ui import ui_dialog_language
    except Exception:
        log.debug("Strings Browser language selector is unavailable.", exc_info=True)
        return False

    has_language_props = (
        hasattr(scene, dialog_language.DIALOG_TEXT_LANGUAGE_PROP)
        or hasattr(scene, dialog_language.DIALOG_VOICE_LANGUAGE_PROP)
        or hasattr(scene, dialog_language.DIALOG_LEGACY_LANGUAGE_PROP)
    )
    if not has_language_props:
        return False

    box = layout.box() if boxed else layout
    return ui_dialog_language.draw_dialog_language_selector(
        box,
        context,
        heading="Languages",
        icon='WORLD',
    )


def _refresh_records(context, force_rebuild=False):
    scene = getattr(context, "scene", None)
    settings = getattr(scene, "witcher_strings_browser", None) if scene else None
    if settings is None:
        return

    game, source, db_path = _resolve_source_and_db(settings)
    language = "en"
    try:
        from .. import dialog_language

        language = dialog_language.get_active_text_language(context) or "en"
    except Exception:
        language = "en"

    only_with_text = bool(getattr(settings, "only_with_text", True))
    records = ss.get_records(
        game,
        source,
        language=language,
        db_path=db_path,
        force_reload=force_rebuild,
        only_with_text=only_with_text,
    )
    scene_cache_key = (game, source, db_path, language, only_with_text, id(records))
    records = _records_with_scene_dialog_info(records, cache_key=scene_cache_key)
    filtered = ss.filter_records(
        records,
        search_text=settings.search_text,
        speaker_filter=settings.speaker_filter,
    )
    if only_with_text:
        filtered = [rec for rec in filtered if _record_has_text(rec)]
    scene_filter = str(getattr(settings, "scene_filter", "") or "").strip()
    if scene_filter:
        filtered = [rec for rec in filtered if _record_matches_scene_filter(rec, scene_filter)]
    resource_filter = str(getattr(settings, "resource_filter", "") or "").strip()
    if resource_filter:
        filtered = [rec for rec in filtered if _record_matches_resource_filter(rec, resource_filter)]
    if bool(getattr(settings, "only_voicetagged", False)):
        filtered = [rec for rec in filtered if _record_has_voice_tag(rec)]

    _FILTERED_CACHE["records"] = filtered
    _FILTERED_CACHE["key"] = (game, source, db_path, language)

    settings.last_total = len(records)
    settings.last_filtered = len(filtered)
    settings.last_built_count = len(records)
    settings.last_db_used = db_path or ""
    settings.last_error = ss.get_last_error(
        game,
        source,
        db_path=db_path,
        language=language,
        only_with_text=only_with_text,
    ) or ""

    selected_id = str(getattr(settings, "filter_anchor_string_id", "") or "").strip()
    if 0 <= int(getattr(settings, "active_index", -1) or -1) < len(settings.items):
        selected_id = selected_id or _selected_string_state_id(settings.items[settings.active_index])
    selected_id = selected_id or _saved_selected_string_id(settings)
    if _refresh_page(context, settings, selected_id=selected_id, jump_to_selected=True):
        _clear_strings_filter_anchor(settings, selected_id)


def _refresh_page(context, settings, *, selected_id="", jump_to_selected=False, select_first=False):
    items = settings.items
    items.clear()

    filtered = _FILTERED_CACHE.get("records", [])
    if not filtered:
        settings.active_index = -1
        _sync_strings_page_number(settings, 1)
        _refresh_selected_string_associated_paths(context, force=True)
        return False

    page_size = max(PAGE_SIZE_MIN, settings.page_size or PAGE_SIZE_DEFAULT)
    total_pages = max(1, int(math.ceil(len(filtered) / page_size)))
    page_index = max(0, min(settings.page_index, total_pages - 1))
    if jump_to_selected and selected_id:
        for rec_idx, rec in enumerate(filtered):
            if _string_record_state_id(rec) == selected_id:
                page_index = rec_idx // page_size
                break
    if settings.page_index != page_index:
        settings.page_index = page_index
    _sync_strings_page_number(settings, total_pages)

    start = page_index * page_size
    end = min(start + page_size, len(filtered))
    page = filtered[start:end]

    for cache_idx, rec in enumerate(page, start=start):
        item = items.add()
        item.cache_index = cache_idx
        item.game = rec.get("game", "")
        item.source = rec.get("source", "")
        try:
            item.string_id = int(rec.get("string_id", 0) or 0)
        except (TypeError, ValueError):
            item.string_id = 0
        item.string_id_str = rec.get("string_id_str", "")
        item.string_key = rec.get("string_key", "")
        item.voice_line_id = rec.get("voice_line_id", "")
        item.voiceover = rec.get("voiceover", "")
        item.has_voice = _record_has_voice(rec)
        item.speaker = rec.get("speaker", "")
        item.speaker_display = rec.get("speaker_display", "")
        item.scene_path = rec.get("scene_path", "")
        item.entity_path = rec.get("entity_path", "")
        if not (item.scene_path or item.entity_path):
            rec = _record_with_scene_dialog_info(rec, summaries=_scene_dialog_summaries(item.game))
            scene_speaker = str(rec.get("speaker", "") or "").strip().upper()
            if scene_speaker:
                item.speaker = scene_speaker
                item.speaker_display = scene_speaker
            item.voice_line_id = rec.get("voice_line_id", "")
            item.scene_path = rec.get("scene_path", "")
            item.entity_path = rec.get("entity_path", "")
        item.text = rec.get("text", "")
        item.resource = rec.get("resource", "")
        item.property_name = rec.get("property", "")
        item.db_path = rec.get("db_path", "")

    saved_id = "" if select_first else (str(selected_id or "").strip() or _saved_selected_string_id(settings))
    restored_index = -1
    if saved_id:
        for idx, item in enumerate(items):
            if _selected_string_state_id(item) == saved_id:
                restored_index = idx
                break

    if select_first and items:
        settings.active_index = 0
    elif restored_index >= 0:
        settings.active_index = restored_index
    elif items and (settings.active_index < 0 or settings.active_index >= len(items)):
        settings.active_index = 0
    _save_strings_browser_state(settings)
    _refresh_selected_string_associated_paths(context)
    return restored_index >= 0


def refresh_strings_browser_dialog_language(context, refresh_audio=False):
    """Refresh visible strings when the shared dialog text language changes."""

    if refresh_audio:
        return
    settings = getattr(getattr(context, "scene", None), "witcher_strings_browser", None) if context is not None else None
    if settings is None:
        return
    if not (_FILTERED_CACHE.get("records") or len(getattr(settings, "items", [])) or int(getattr(settings, "last_built_count", 0) or 0)):
        return
    _refresh_records(context, force_rebuild=False)


# ---------------------------------------------------------------------------
# UIList
# ---------------------------------------------------------------------------

class WITCHER_UL_strings_browser_speakers(bpy.types.UIList):
    """List rendering for the Search Speakers popup."""
    use_filter_show = False
    use_filter_sort_alpha = False

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type not in {'DEFAULT', 'COMPACT'}:
            layout.label(text=f"{item.display}  ({item.count})")
            return
        row = layout.row(align=True)
        # Display name (resolved) takes the main column. Show the raw 4-letter
        # abbreviation only when it differs from the display — for resolved
        # characters that's useful diagnostic info; for unresolved ones it's
        # redundant.
        split = row.split(factor=0.55, align=True)
        split.label(text=item.display or "-", icon='USER' if item.display else 'BLANK1')
        right = split.split(factor=0.55, align=True)
        raw = (item.raw_code or "").strip()
        if raw and raw.upper() != (item.display or "").upper():
            right.label(text=f"({raw})")
        else:
            right.label(text="")
        right.label(text=f"{item.count:,}")

    def draw_filter(self, context, layout):
        # Operator owns its own search box.
        return

    def filter_items(self, context, data, propname):
        return [], []


class WITCHER_OT_strings_browser_search_speakers(bpy.types.Operator):
    """Pick a speaker from the full list of speakers in the active source.

    Mirrors the pattern of ``WITCH_OT_search_base_material_path``: a popup
    dialog with a search box and a paginated list. Selecting an entry sets
    ``settings.speaker_filter`` to the underlying 4-letter code so the main
    strings list re-filters to just that character.
    """
    bl_idname = "witcher.strings_browser_search_speakers"
    bl_label = "Search Speakers"
    bl_description = "Search and pick a speaker to filter the strings list"
    bl_options = {'REGISTER', 'INTERNAL'}

    filter_text: StringProperty(name="Search", default="")
    target: EnumProperty(
        name="Target",
        items=(
            ("strings", "Strings", "Filter the Strings Browser"),
            ("dialogue", "Dialogue", "Filter the Dialogue Browser"),
        ),
        default="strings",
        options={'HIDDEN', 'SKIP_SAVE'},
    )
    items: CollectionProperty(type=WITCH_PG_StringsBrowserSpeakerItem)
    items_index: IntProperty(default=0)

    def _build_dialogue_speaker_list(self, context):
        try:
            from ..ui import ui_voice
        except Exception:
            log.warning("Dialogue Browser speaker picker could not import ui_voice.", exc_info=True)
            return []

        ui_voice.ensure_voice_cache(context)
        nodes = getattr(ui_voice, "_voice_node_cache", []) or []
        counts = {}
        for node in nodes:
            speaker = str(node.get("speaker", "") if isinstance(node, dict) else getattr(node, "speaker", "")).strip().upper()
            if not speaker:
                continue
            entry = counts.get(speaker)
            if entry is None:
                counts[speaker] = {
                    "code": speaker,
                    "raw_code": "",
                    "display": speaker.title() if speaker != "UNKN" else "Unknown",
                    "count": 1,
                }
            else:
                entry["count"] += 1
        return sorted(counts.values(), key=lambda d: (-d["count"], d["display"].lower()))

    def _build_full_speaker_list(self, context):
        """Collect every speaker from the unfiltered source records.

        Pulls the full record list via ``ss.get_records`` (a cache hit when
        the source is loaded) so the speaker picker shows every speaker in
        the active DB / binary table, not just those matching the active
        text-search filter.
        """
        if self.target == "dialogue":
            return self._build_dialogue_speaker_list(context)

        scene = getattr(context, "scene", None)
        settings = getattr(scene, "witcher_strings_browser", None) if scene else None
        if settings is None:
            return []
        game, source, db_path = _resolve_source_and_db(settings)
        language = "en"
        try:
            from .. import dialog_language

            language = dialog_language.get_active_text_language(context) or "en"
        except Exception:
            language = "en"
        only_with_text = bool(getattr(settings, "only_with_text", True))
        records = ss.get_records(
            game,
            source,
            language=language,
            db_path=db_path,
            only_with_text=only_with_text,
        ) or []
        scene_cache_key = (game, source, db_path, language, only_with_text, id(records))
        records = _records_with_scene_dialog_info(records, cache_key=scene_cache_key)

        counts = {}  # filter_key -> {code, raw_code, display, count}
        for rec in records:
            filter_key = (rec.get("speaker") or "").strip()
            if not filter_key:
                continue
            entry = counts.get(filter_key)
            if entry is None:
                display = (rec.get("speaker_display") or "").strip() or filter_key
                raw_code = (rec.get("speaker_code") or "").strip()
                counts[filter_key] = {
                    "code": filter_key,
                    "raw_code": raw_code,
                    "display": display,
                    "count": 1,
                }
            else:
                entry["count"] += 1
                # If we didn't have a raw_code yet but this record does, keep it.
                if not entry["raw_code"]:
                    entry["raw_code"] = (rec.get("speaker_code") or "").strip()
        speakers = sorted(counts.values(), key=lambda d: (-d["count"], d["display"].lower()))
        return speakers

    def _rebuild_items(self, context):
        speakers = self._build_full_speaker_list(context)
        query = (self.filter_text or "").strip().lower()
        self.items.clear()
        for sp in speakers:
            if query:
                # Search across both resolved display and raw abbreviation so
                # typing ``grlt`` finds Geralt, and ``ger`` does too.
                hay = " ".join((sp["code"], sp.get("raw_code", ""), sp["display"])).lower()
                if query not in hay:
                    continue
            item = self.items.add()
            item.code = sp["code"]
            item.raw_code = sp.get("raw_code", "")
            item.display = sp["display"]
            item.count = int(sp["count"])
        if len(self.items):
            self.items_index = min(max(int(self.items_index), 0), len(self.items) - 1)
        else:
            self.items_index = -1

    def invoke(self, context, event):
        scene = context.scene
        settings = getattr(scene, "witcher_strings_browser", None) if self.target != "dialogue" else None
        if self.target != "dialogue" and settings is None:
            self.report({'ERROR'}, "Strings browser settings not registered.")
            return {'CANCELLED'}

        # Make sure the underlying records exist — the popup builds its list
        # off the in-memory filter cache, which is populated by every refresh.
        if self.target != "dialogue" and not (_FILTERED_CACHE.get("records") or []):
            _refresh_records(context, force_rebuild=False)

        self.filter_text = ""
        self._rebuild_items(context)
        if not len(self.items):
            self.report({'WARNING'}, "No speakers in the current source.")
            return {'CANCELLED'}

        # Preselect the current filter if any.
        if self.target == "dialogue":
            current = str(getattr(scene, "witcher_voice_speaker_filter", "") or "").strip().upper()
        else:
            current = (settings.speaker_filter or "").strip().upper()
        if current:
            for idx, item in enumerate(self.items):
                if item.code == current:
                    self.items_index = idx
                    break

        return context.window_manager.invoke_props_dialog(self, width=520)

    def check(self, context):
        self._rebuild_items(context)
        return True

    def draw(self, context):
        layout = self.layout
        row = layout.row(align=True)
        row.prop(self, "filter_text", text="", icon='VIEWZOOM')

        total = len(self.items)
        if total == 0:
            layout.label(text="No matching speakers.", icon='INFO')
            return

        list_box = layout.box()
        list_box.template_list(
            "WITCHER_UL_strings_browser_speakers",
            "",
            self,
            "items",
            self,
            "items_index",
            rows=20,
        )
        info = layout.row(align=True)
        info.scale_y = 0.9
        target_label = "dialogue list" if self.target == "dialogue" else "main list"
        info.label(text=f"{total} speaker(s) — click OK to filter the {target_label}", icon='INFO')

    def execute(self, context):
        scene = context.scene
        if not (0 <= self.items_index < len(self.items)):
            return {'CANCELLED'}
        chosen = self.items[self.items_index]
        if self.target == "dialogue":
            try:
                from ..ui import ui_voice

                ui_voice._set_speaker_filter(scene, context, chosen.code)
            except Exception:
                log.warning("Dialogue Browser speaker picker failed to apply filter.", exc_info=True)
                return {'CANCELLED'}
            return {'FINISHED'}

        settings = getattr(scene, "witcher_strings_browser", None)
        if settings is None:
            return {'CANCELLED'}
        # Setting this fires _on_filter_update which re-applies the filter.
        settings.speaker_filter = chosen.code
        return {'FINISHED'}


class WITCHER_UL_strings_browser(bpy.types.UIList):
    use_filter_show = False
    use_filter_sort_alpha = False

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type not in {'DEFAULT', 'COMPACT'}:
            layout.label(text=_strings_browser_row_label(item))
            return

        row = layout.row(align=True)
        if getattr(item, "has_voice", False):
            op = row.operator(
                "witcher.strings_browser_preview_voice",
                text="",
                icon='CANCEL' if _is_voice_preview_playing(item.game, item.voice_line_id or item.string_id_str) else 'PLAY',
            )
            op.game = item.game
            op.line_id = item.voice_line_id or item.string_id_str
            op.text = item.text
            op.speaker = item.speaker_display or item.speaker
            op.voiceover = item.voiceover
        else:
            row.label(text="", icon='BLANK1')
        row.label(text=_strings_browser_row_label(item))

    def draw_filter(self, context, layout):
        # Suppress Blender's default filter bar — we have our own search UI.
        return

    def filter_items(self, context, data, propname):
        return [], []


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

class WITCHER_OT_strings_browser_open(bpy.types.Operator):
    """Open the Witcher Strings Browser popup"""

    bl_idname = "witcher.open_strings_browser"
    bl_label = "Witcher Strings Browser"
    bl_options = {'REGISTER', 'INTERNAL'}

    initial_game: EnumProperty(
        name="Initial Game",
        items=(
            (ss.GAME_W3, "Witcher 3", ""),
            (ss.GAME_W2, "Witcher 2", ""),
        ),
        default=ss.GAME_W3,
        options={'HIDDEN', 'SKIP_SAVE'},
    )

    def invoke(self, context, event):
        scene = context.scene
        settings = getattr(scene, "witcher_strings_browser", None)
        if settings is None:
            self.report({'ERROR'}, "Strings browser settings not registered.")
            return {'CANCELLED'}

        _sync_strings_page_size_from_pref(context, settings)
        if self.initial_game and settings.active_game != self.initial_game:
            settings.active_game = self.initial_game
        else:
            settings.last_active_game = str(settings.active_game or ss.GAME_W3).upper()
            _restore_strings_browser_state(settings, game=settings.active_game)
            _refresh_records(context, force_rebuild=False)

        width = DEFAULT_POPUP_WIDTH
        try:
            prefs = context.preferences.addons[__package__.split(".")[0]].preferences
            override = int(getattr(prefs, "browser_popup_width", 0) or 0)
            if override > 0:
                width = override
        except Exception:
            pass
        return context.window_manager.invoke_props_dialog(self, width=width)

    def execute(self, context):
        return {'FINISHED'}

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        settings = getattr(scene, "witcher_strings_browser", None)
        if settings is None:
            layout.label(text="Settings unavailable.", icon='ERROR')
            return

        # ── Game tabs ─────────────────────────────────────────────
        header = layout.row()
        header.scale_y = 1.4
        header.prop_enum(settings, "active_game", ss.GAME_W3)
        header.prop_enum(settings, "active_game", ss.GAME_W2)

        layout.separator(factor=0.5)
        setup_box = layout.box()
        setup_header, setup_body = setup_box.panel("witcher_strings_browser_language_source", default_closed=False)
        setup_header.label(text="Language and Source", icon='SETTINGS')
        if setup_body:
            _draw_strings_language_selector(setup_body, context, boxed=False)
            setup_body.separator(factor=0.5)
            if settings.active_game == ss.GAME_W3:
                self._draw_w3_source(setup_body, settings, boxed=False)
            else:
                self._draw_w2_source(setup_body, settings, boxed=False)

        # ── Game-specific source/DB picker ────────────────────────
        layout.separator(factor=0.5)

        # ── Filters ───────────────────────────────────────────────
        filter_box = layout.box()
        filter_box.label(text="Filters", icon='FILTER')
        row = filter_box.row(align=True)
        row.prop(settings, "search_text", text="", icon='VIEWZOOM')
        back_btn = row.row(align=True)
        back_btn.enabled = bool(
            getattr(settings, "previous_selected_string_id", "")
            or getattr(settings, "filter_anchor_string_id", "")
        )
        back_btn.operator("witcher.strings_browser_back_select", text="", icon='BACK')
        row.operator("witcher.strings_browser_clear_filters", text="", icon='X')
        row.operator("witcher.strings_browser_rebuild", text="", icon='FILE_REFRESH')

        speaker_row = filter_box.row(align=True)
        speaker_row.prop(settings, "speaker_filter", text="Speaker")
        # Browse-from-full-list button (mirrors the base-material path picker).
        speaker_row.operator(
            "witcher.strings_browser_search_speakers",
            text="",
            icon='VIEWZOOM',
        )
        if settings.speaker_filter:
            op = speaker_row.operator("witcher.strings_browser_set_speaker", text="", icon='X')
            op.speaker = ""

        tag_row = filter_box.row(align=True)
        tag_row.prop(settings, "only_with_text", text="Only With Text")
        tag_row.prop(settings, "only_voicetagged", text="Only VoiceTagged")

        if settings.scene_filter:
            scene_row = filter_box.row(align=True)
            scene_row.prop(settings, "scene_filter", text="Scene")
            clear_scene = scene_row.operator("witcher.strings_browser_filter_scene", text="", icon='X')
            clear_scene.scene_path = ""
            clear_scene.clear_other_filters = False

        if settings.resource_filter:
            resource_row = filter_box.row(align=True)
            resource_row.prop(settings, "resource_filter", text="Resource")
            clear_resource = resource_row.operator("witcher.strings_browser_filter_resource", text="", icon='X')
            clear_resource.resource = ""
            clear_resource.clear_other_filters = False

        speakers_header, speakers_body = filter_box.panel("witcher_strings_browser_top_speakers", default_closed=True)
        speakers_header.label(text="Top Speakers", icon='USER')
        if speakers_body:
            speakers_grid = speakers_body.grid_flow(columns=4, even_columns=True, align=True)
            for speaker, count in ss.collect_speakers(_FILTERED_CACHE.get("records", []), top=12):
                sub = speakers_grid.row(align=True)
                sub.scale_y = 0.85
                op = sub.operator(
                    "witcher.strings_browser_set_speaker",
                    text=f"{speaker.title()} ({count})",
                )
                op.speaker = speaker

        # ── Counts + page nav ─────────────────────────────────────
        info_row = layout.row(align=True)
        info_row.scale_y = 0.95
        info_row.label(
            text=(
                f"Showing {settings.last_filtered:,} of {settings.last_total:,} strings"
            ),
            icon='INFO',
        )
        info_row.prop(settings, "page_size", text="Rows/page")

        # Show the actual DB / file in use so the user can confirm auto-detect
        # picked the right one.
        if settings.last_db_used:
            using_row = layout.row()
            using_row.scale_y = 0.85
            using_row.label(text=f"Using: {settings.last_db_used}", icon='FILE')

        # Surface read errors here. When a SQLite source returns 0 rows because
        # of a connection / schema problem, the user otherwise has no way to
        # see why.
        if settings.last_error:
            err_box = layout.box()
            err_box.alert = True
            err_box.label(text="Read error:", icon='ERROR')
            err_box.label(text=settings.last_error)

        total_pages = _strings_total_pages(settings)
        page_row = layout.row(align=True)
        page_row.scale_y = 1.1
        nav = page_row.row(align=True)
        op = nav.operator("witcher.strings_browser_page", text="", icon='TRIA_LEFT')
        op.action = "prev"
        nav.prop(settings, "page_number", text="Page")
        nav.label(text=f"/ {total_pages}")
        op = nav.operator("witcher.strings_browser_page", text="", icon='TRIA_RIGHT')
        op.action = "next"

        # ── List ──────────────────────────────────────────────────
        list_row = layout.row()
        list_row.template_list(
            "WITCHER_UL_strings_browser",
            "",
            settings,
            "items",
            settings,
            "active_index",
            rows=18,
        )

        # ── Detail panel for the selected entry ───────────────────
        detail_box = layout.box()
        detail_head = detail_box.row(align=True)
        detail_head.label(text="Selected String", icon='OUTLINER_DATA_FONT')
        detail_head.prop(settings, "format_selected_text", text="Format")
        if 0 <= settings.active_index < len(settings.items):
            item = settings.items[settings.active_index]
            grid = detail_box.column(align=True)
            _draw_selected_string_value(grid, "ID", item.string_id_str, "id")
            if item.voice_line_id and item.voice_line_id != item.string_id_str:
                _draw_selected_string_value(grid, "Voice ID", item.voice_line_id, "voice_id")
            _draw_selected_string_value(grid, "Key", item.string_key, "key")
            _draw_selected_string_value(grid, "Voiceover", item.voiceover, "voiceover")
            _draw_selected_string_value(grid, "Speaker", item.speaker, "speaker")
            _draw_selected_string_value(grid, "Property", item.property_name, "property")
            _draw_selected_string_value(
                grid,
                "Resource",
                item.resource,
                "resource",
                filter_operator="witcher.strings_browser_filter_resource",
                filter_attr="resource",
            )
            text_col = detail_box.column(align=True)
            text_value = item.text or ""
            text_chunks = _string_text_display_lines(
                text_value,
                width=110,
                formatted=bool(getattr(settings, "format_selected_text", True)),
            )
            text_header = text_col.row(align=True)
            text_header.alignment = 'LEFT'
            first_chunk = text_chunks[0] if text_chunks else "<empty>"
            text_header.label(text=f"Text: {first_chunk}" if first_chunk else "Text:")
            if item.has_voice:
                op = text_header.operator(
                    "witcher.strings_browser_preview_voice",
                    text="",
                    icon='CANCEL' if _is_voice_preview_playing(item.game, item.voice_line_id or item.string_id_str) else 'PLAY',
                )
                op.game = item.game
                op.line_id = item.voice_line_id or item.string_id_str
                op.text = item.text
                op.speaker = item.speaker_display or item.speaker
                op.voiceover = item.voiceover
            _strings_copy_button(text_header, "text")
            # Multi-line text box: stack labels per visual line.
            for chunk in text_chunks[1:]:
                text_col.label(text=chunk or " ")

            associated_paths = getattr(settings, "associated_paths", None)
            if associated_paths is not None and len(associated_paths):
                assoc_header, assoc_body = detail_box.panel("witcher_strings_associated_files", default_closed=False)
                assoc_header.label(text=f"Associated Files ({len(associated_paths)})", icon='FILE_FOLDER')
                if assoc_body:
                    voicetag_entity_count = _selected_string_voicetag_entity_count(item)
                    if voicetag_entity_count:
                        show_all = bool(getattr(settings, "show_all_voicetag_entities", False))
                        assoc_body.operator(
                            "witcher.strings_browser_toggle_voicetag_entities",
                            text=(
                                "Hide VoiceTag Entities"
                                if show_all
                                else f"Show All VoiceTag Entities ({voicetag_entity_count})"
                            ),
                            icon='COMMUNITY',
                        )
                    for assoc in associated_paths:
                        _draw_strings_associated_path(assoc_body, assoc)
        else:
            detail_box.label(text="No row selected.", icon='INFO')

    # ------------------------------------------------------------------
    # Helpers used by draw()
    # ------------------------------------------------------------------

    def _draw_w3_source(self, layout, settings, *, boxed=True):
        box = layout.box() if boxed else layout.column(align=True)
        head = box.row(align=True)
        head.label(text="Witcher 3 Strings", icon='WORDWRAP_ON')
        head.prop(settings, "w3_source", text="")

        if settings.w3_source == ss.SOURCE_W3STRINGS:
            info = box.row()
            info.scale_y = 0.9
            info.label(text="Reads cached *.w3strings files from the Witcher 3 game folder.", icon='INFO')
            return

        # SQLite source — show the override + auto-detect diagnostics
        row = box.row(align=True)
        row.prop(settings, "w3_db_path", text="DB / Folder")
        db_paths = ss.find_string_db_paths(ss.GAME_W3, override_path=settings.w3_db_path)
        _draw_db_diagnostics(box, db_paths, expected="LocalEditorStringDataBaseW3_UTF8.db",
                              hint="Point at the REDkit install folder, its r4data subfolder, or a specific .db file.")

    def _draw_w2_source(self, layout, settings, *, boxed=True):
        box = layout.box() if boxed else layout.column(align=True)
        head = box.row(align=True)
        head.label(text="Witcher 2 Strings", icon='WORDWRAP_ON')
        head.prop(settings, "w2_source", text="")

        if settings.w2_source == ss.SOURCE_W2STRINGS:
            row = box.row(align=True)
            row.prop(settings, "w2_db_path", text="Folder")
            files = ss.find_w2_strings_files(settings.w2_db_path)
            info_row = box.row()
            info_row.scale_y = 0.9
            if files:
                info_row.label(
                    text=f"Found {len(files)} .w2strings file(s) (showing language matching scene 'Text/Subtitles').",
                    icon='CHECKMARK',
                )
            else:
                info_row.alert = True
                info_row.label(text="No CookedPC/<lang>0.w2strings found", icon='ERROR')
                hint = box.row()
                hint.scale_y = 0.85
                hint.label(text="Set Witcher 2 path in addon prefs, or paste the CookedPC folder above.")
            return

        # SQLite source — show the override + auto-detect diagnostics
        row = box.row(align=True)
        row.prop(settings, "w2_db_path", text="DB / Folder")
        db_paths = ss.find_string_db_paths(ss.GAME_W2, override_path=settings.w2_db_path)
        _draw_db_diagnostics(box, db_paths, expected="base.sqlite / user.sqlite",
                              hint="Point at the Witcher 2 install folder, its bin subfolder, or a specific .sqlite file.")


def _draw_db_diagnostics(box, db_paths, *, expected, hint):
    """Show the auto-detect result for SQLite sources.

    Renders the absolute path of the DB being used (so the user can verify
    auto-detect picked the right one) plus the file size, and lists any other
    candidates that were found alongside it.
    """
    count_row = box.row()
    count_row.scale_y = 0.9
    if db_paths:
        active_path = str(db_paths[0])
        size_text = _human_size(active_path)
        count_row.label(
            text=f"Using: {active_path}  ({size_text})",
            icon='FILE',
        )
        if len(db_paths) > 1:
            extras = box.column(align=True)
            extras.scale_y = 0.85
            extras.label(text=f"Other candidates ({len(db_paths) - 1}):", icon='INFO')
            for path in db_paths[1:5]:
                path_str = str(path)
                extras.label(text=f"  {path_str}  ({_human_size(path_str)})")
    else:
        count_row.alert = True
        count_row.label(text=f"No {expected} found", icon='ERROR')
        hint_row = box.row()
        hint_row.scale_y = 0.85
        hint_row.label(text=hint)


def _human_size(path):
    try:
        size = os.path.getsize(path)
    except OSError:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit == "B":
                return f"{size} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


_STRING_BREAK_TAG_RE = re.compile(r"<\s*br\s*/?\s*>", re.IGNORECASE)
_STRING_PARAGRAPH_END_RE = re.compile(r"</\s*p\s*>", re.IGNORECASE)
_STRING_PARAGRAPH_START_RE = re.compile(r"<\s*p\b[^>]*>", re.IGNORECASE)
_STRING_BLOCK_TAG_RE = re.compile(r"</?\s*(?:div|center|left|right|h[1-6])\b[^>]*>", re.IGNORECASE)
_STRING_INLINE_TAG_RE = re.compile(
    r"</?\s*(?:i|b|strong|em|u|font|span|small|big|sup|sub|a|c|color)\b[^>]*>",
    re.IGNORECASE,
)
_STRING_ANY_WORD_TAG_RE = re.compile(r"</?\s*[A-Za-z][A-Za-z0-9_:.-]*(?:\s+[^<>]*)?\s*/?>")


def _format_string_markup_for_display(text):
    """Render common localized-string markup as readable Blender labels."""

    value = html.unescape(str(text or ""))
    value = value.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    value = _STRING_BREAK_TAG_RE.sub("\n", value)
    value = _STRING_PARAGRAPH_END_RE.sub("\n\n", value)
    value = _STRING_PARAGRAPH_START_RE.sub("", value)
    value = _STRING_BLOCK_TAG_RE.sub("\n", value)
    value = _STRING_INLINE_TAG_RE.sub("", value)
    value = _STRING_ANY_WORD_TAG_RE.sub("", value)

    lines = []
    previous_blank = False
    for raw_line in value.split("\n"):
        line = re.sub(r"[ \t\f\v]+", " ", raw_line).strip()
        if not line:
            if lines and not previous_blank:
                lines.append("")
            previous_blank = True
            continue
        lines.append(line)
        previous_blank = False

    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def _string_text_display_lines(text, *, width, formatted):
    value = _format_string_markup_for_display(text) if formatted else str(text or "")
    if not value:
        return ["<empty>"]

    lines = []
    for raw_line in value.splitlines():
        if not raw_line.strip():
            lines.append("")
            continue
        lines.extend(_split_text_for_display(raw_line, width))
    return lines or ["<empty>"]


def _split_text_for_display(text, width):
    """Split a long string into ~width-char chunks for label stacking.

    Blender's `layout.label` does not word-wrap, so we manually break long
    text on whitespace boundaries.
    """

    words = text.split(" ")
    lines = []
    current = ""
    for word in words:
        if not current:
            current = word
            continue
        if len(current) + 1 + len(word) <= width:
            current = current + " " + word
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [text]


def _extract_w3_speech_wem_for_preview(context, line_id):
    """Extract the W3 speech WEM for a string id and return (path, language)."""

    line_id = str(line_id or "").strip()
    if not line_id:
        raise RuntimeError("No string ID is available for this row.")

    try:
        from .. import dialog_language
        from ..CR2W.witcher_cache.Speech import LoadSpeechManager
        from ..extension_paths import get_cache_root
    except Exception as exc:
        raise RuntimeError(f"Speech preview support is unavailable: {exc}") from exc

    requested_language = dialog_language.normalize_dialog_language(
        dialog_language.get_active_voice_language(context)
    )
    languages = [requested_language or "en"]
    if "en" not in languages:
        languages.append("en")

    for language in languages:
        manager = LoadSpeechManager(language=language)
        matches = manager.find_item_by_hash(line_id) or []
        if not matches:
            continue
        item = matches[0]
        out_dir = Path(get_cache_root(create=True)) / "StringsBrowser" / "speech_preview" / language
        out_dir.mkdir(parents=True, exist_ok=True)
        try:
            cached_wem = out_dir / f"{int(item.id):010}.wem"
        except Exception:
            cached_wem = out_dir / f"{item.id}.wem"
        if cached_wem.is_file():
            return str(cached_wem), language
        wem_path = item.extract_to_file(str(item.id), output_dir=str(out_dir))
        if wem_path and os.path.isfile(wem_path):
            return wem_path, language

    raise FileNotFoundError(f"Voice line {line_id} was not found in installed W3 speech resources.")


def _extract_w2_speech_for_preview(context, line_id):
    """Extract the W2 speech MP2/DAT pair for a dialogue id and return (path, language)."""

    line_id = str(line_id or "").strip()
    if not line_id:
        raise RuntimeError("No Witcher 2 voice line ID is available for this row.")

    try:
        from .. import dialog_language
        from ..ui import ui_file_browser, ui_speech
    except Exception as exc:
        raise RuntimeError(f"Witcher 2 speech preview support is unavailable: {exc}") from exc

    extracted_path = ui_file_browser.ensure_witcher2_speech_item_extracted(context, line_id, overwrite=False)
    if not extracted_path:
        raise FileNotFoundError(f"Voice line {line_id} was not found in installed W2 speech resources.")

    try:
        _dat_path, mp2_path = ui_speech._resolve_w2_voice_pair(extracted_path, context)
    except Exception:
        mp2_path = ""

    sound_path = mp2_path or extracted_path
    language = ""
    try:
        language = ui_speech._w2_voice_language_from_path(sound_path)
    except Exception:
        language = ""
    if not language:
        try:
            language = dialog_language.normalize_dialog_language(dialog_language.get_active_voice_language(context))
        except Exception:
            language = "en"
    return sound_path, language or "en"


def _extract_speech_for_preview(context, game, line_id):
    game = str(game or ss.GAME_W3).upper()
    if game == ss.GAME_W2:
        return _extract_w2_speech_for_preview(context, line_id)
    return _extract_w3_speech_wem_for_preview(context, line_id)


class WITCHER_OT_strings_browser_preview_voice(bpy.types.Operator):
    """Play/stop preview audio for a voiced string."""

    bl_idname = "witcher.strings_browser_preview_voice"
    bl_label = "Preview Voice"
    bl_options = {'INTERNAL'}

    line_id: StringProperty(default="")
    game: StringProperty(default=ss.GAME_W3)
    text: StringProperty(default="")
    speaker: StringProperty(default="")
    voiceover: StringProperty(default="")

    @classmethod
    def description(cls, context, properties):
        voiceover = str(getattr(properties, "voiceover", "") or "").strip()
        line_id = str(getattr(properties, "line_id", "") or "").strip()
        if voiceover:
            return f"Play or stop preview audio for {voiceover}"
        if line_id:
            return f"Play or stop preview audio for string {line_id}"
        return "Play or stop preview audio for the selected string"

    def _resolve_from_selection(self, context):
        if self.line_id:
            return
        settings = getattr(getattr(context, "scene", None), "witcher_strings_browser", None)
        if settings is None or not (0 <= settings.active_index < len(settings.items)):
            return
        item = settings.items[settings.active_index]
        self.game = item.game or ss.GAME_W3
        self.line_id = item.voice_line_id or item.string_id_str
        self.text = item.text
        self.speaker = item.speaker_display or item.speaker
        self.voiceover = item.voiceover

    def execute(self, context):
        self._resolve_from_selection(context)
        game = str(self.game or ss.GAME_W3).upper()
        line_id = str(self.line_id or "").strip()
        if not line_id:
            self.report({'WARNING'}, "No voiced string selected.")
            return {'CANCELLED'}

        preview_key = _voice_preview_item_key(game, line_id)
        try:
            from ..ui import ui_file_browser
        except Exception as exc:
            self.report({'ERROR'}, f"Sound preview support is unavailable: {exc}")
            return {'CANCELLED'}

        if ui_file_browser.sound_preview_matches(STRINGS_SOUND_PREVIEW_TYPE, preview_key):
            ui_file_browser.clear_sound_preview(context)
            self.report({'INFO'}, f"Stopped preview: {line_id}")
            return {'FINISHED'}

        try:
            speech_path, language = _extract_speech_for_preview(context, game, line_id)
            wav_path = ui_file_browser.play_sound_file_preview(
                context,
                speech_path,
                preview_key,
                cache_type=STRINGS_SOUND_PREVIEW_TYPE,
            )
        except Exception as exc:
            self.report({'ERROR'}, f"Voice preview failed: {exc}")
            return {'CANCELLED'}

        try:
            from .. import dialog_language

            requested = dialog_language.normalize_dialog_language(dialog_language.get_active_voice_language(context))
        except Exception:
            requested = language
        if language and requested and language != requested:
            self.report({'WARNING'}, f"Playing {language.upper()} audio; {requested.upper()} was not found for {line_id}.")
        else:
            self.report({'INFO'}, f"Playing preview: {os.path.basename(wav_path)}")
        return {'FINISHED'}


class WITCHER_OT_strings_browser_page(bpy.types.Operator):
    """Navigate the Strings Browser between pages"""

    bl_idname = "witcher.strings_browser_page"
    bl_label = "Strings Browser Page"
    bl_options = {'INTERNAL'}

    action: StringProperty(default="next")

    def execute(self, context):
        scene = context.scene
        settings = getattr(scene, "witcher_strings_browser", None)
        if settings is None:
            return {'CANCELLED'}
        page_size = max(PAGE_SIZE_MIN, settings.page_size or PAGE_SIZE_DEFAULT)
        total_pages = max(1, int(math.ceil(settings.last_filtered / page_size)))
        current = settings.page_index
        if self.action == "first":
            target = 0
        elif self.action == "prev":
            target = max(0, current - 1)
        elif self.action == "next":
            target = min(total_pages - 1, current + 1)
        elif self.action == "last":
            target = total_pages - 1
        else:
            return {'CANCELLED'}
        if target != current:
            settings.page_index = target
            _refresh_page(context, settings, select_first=True)
        return {'FINISHED'}


class WITCHER_OT_strings_browser_set_speaker(bpy.types.Operator):
    """Filter the list to a specific speaker"""

    bl_idname = "witcher.strings_browser_set_speaker"
    bl_label = "Filter to Speaker"
    bl_options = {'INTERNAL'}

    speaker: StringProperty(default="")

    def execute(self, context):
        scene = context.scene
        settings = getattr(scene, "witcher_strings_browser", None)
        if settings is None:
            return {'CANCELLED'}
        settings.speaker_filter = self.speaker
        return {'FINISHED'}


class WITCHER_OT_strings_browser_filter_scene(bpy.types.Operator):
    """Filter the strings list to a specific scene"""

    bl_idname = "witcher.strings_browser_filter_scene"
    bl_label = "Filter to Scene"
    bl_options = {'INTERNAL'}

    scene_path: StringProperty(default="")
    clear_other_filters: BoolProperty(default=False)

    @classmethod
    def description(cls, context, properties):
        scene_path = str(getattr(properties, "scene_path", "") or "")
        return f"Show strings associated with {scene_path}" if scene_path else "Clear the strings scene filter"

    def execute(self, context):
        global _STRINGS_STATE_SYNCING
        scene = context.scene
        settings = getattr(scene, "witcher_strings_browser", None)
        if settings is None:
            return {'CANCELLED'}
        scene_path = str(self.scene_path or "").strip().replace("/", "\\")
        _set_strings_filter_anchor(settings)
        _STRINGS_STATE_SYNCING = True
        try:
            if self.clear_other_filters and scene_path:
                settings.search_text = ""
                settings.speaker_filter = ""
                settings.resource_filter = ""
                settings.only_voicetagged = False
            settings.scene_filter = scene_path
        finally:
            _STRINGS_STATE_SYNCING = False
        _refresh_records(context)
        if scene_path:
            self.report({'INFO'}, f"Filtered scene: {scene_path}")
        return {'FINISHED'}


class WITCHER_OT_strings_browser_filter_resource(bpy.types.Operator):
    """Filter the strings list to a specific REDkit resource"""

    bl_idname = "witcher.strings_browser_filter_resource"
    bl_label = "Filter to Resource"
    bl_options = {'INTERNAL'}

    resource: StringProperty(default="")
    clear_other_filters: BoolProperty(default=False)

    @classmethod
    def description(cls, context, properties):
        resource = str(getattr(properties, "resource", "") or "")
        return f"Show strings with resource {resource}" if resource else "Clear the strings resource filter"

    def execute(self, context):
        global _STRINGS_STATE_SYNCING
        scene = context.scene
        settings = getattr(scene, "witcher_strings_browser", None)
        if settings is None:
            return {'CANCELLED'}
        resource = str(self.resource or "").strip()
        _set_strings_filter_anchor(settings)
        _STRINGS_STATE_SYNCING = True
        try:
            if self.clear_other_filters and resource:
                settings.search_text = ""
                settings.speaker_filter = ""
                settings.scene_filter = ""
                settings.only_voicetagged = False
            settings.resource_filter = resource
        finally:
            _STRINGS_STATE_SYNCING = False
        _refresh_records(context)
        if resource:
            self.report({'INFO'}, f"Filtered resource: {resource}")
        return {'FINISHED'}


class WITCHER_OT_strings_browser_clear_filters(bpy.types.Operator):
    """Clear search text, speaker, scene, resource, and tag filters"""

    bl_idname = "witcher.strings_browser_clear_filters"
    bl_label = "Clear Filters"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        global _STRINGS_STATE_SYNCING
        scene = context.scene
        settings = getattr(scene, "witcher_strings_browser", None)
        if settings is None:
            return {'CANCELLED'}
        _set_strings_filter_anchor(settings)
        _STRINGS_STATE_SYNCING = True
        try:
            settings.search_text = ""
            settings.speaker_filter = ""
            settings.scene_filter = ""
            settings.resource_filter = ""
            settings.only_voicetagged = False
            settings.only_with_text = True
        finally:
            _STRINGS_STATE_SYNCING = False
        _refresh_records(context)
        return {'FINISHED'}


class WITCHER_OT_strings_browser_back_select(bpy.types.Operator):
    """Return to the string selected before the last filter/list jump"""

    bl_idname = "witcher.strings_browser_back_select"
    bl_label = "Back Select"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        global _STRINGS_STATE_SYNCING
        settings = getattr(getattr(context, "scene", None), "witcher_strings_browser", None)
        if settings is None:
            return {'CANCELLED'}
        target = (
            str(getattr(settings, "previous_selected_string_id", "") or "").strip()
            or str(getattr(settings, "filter_anchor_string_id", "") or "").strip()
        )
        if not target:
            self.report({'WARNING'}, "No previous string selection.")
            return {'CANCELLED'}

        filtered = _FILTERED_CACHE.get("records", []) or []
        if not any(_string_record_state_id(rec) == target for rec in filtered):
            _STRINGS_STATE_SYNCING = True
            try:
                settings.search_text = ""
                settings.speaker_filter = ""
                settings.scene_filter = ""
                settings.resource_filter = ""
                settings.only_voicetagged = False
                settings.page_index = 0
                _set_strings_filter_anchor(settings, target)
            finally:
                _STRINGS_STATE_SYNCING = False
            _refresh_records(context)
        else:
            _set_strings_filter_anchor(settings, target)
            if _refresh_page(context, settings, selected_id=target, jump_to_selected=True):
                _clear_strings_filter_anchor(settings, target)

        filtered = _FILTERED_CACHE.get("records", []) or []
        if not any(_string_record_state_id(rec) == target for rec in filtered):
            self.report({'WARNING'}, f"Previous string {target} is not available.")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Selected string {target}.")
        return {'FINISHED'}


class WITCHER_OT_strings_browser_toggle_voicetag_entities(bpy.types.Operator):
    """Show/hide all templates that use the selected string's voice tag"""

    bl_idname = "witcher.strings_browser_toggle_voicetag_entities"
    bl_label = "Show VoiceTag Entities"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        settings = getattr(getattr(context, "scene", None), "witcher_strings_browser", None)
        if settings is None:
            return {'CANCELLED'}
        settings.show_all_voicetag_entities = not bool(getattr(settings, "show_all_voicetag_entities", False))
        _refresh_selected_string_associated_paths(context, force=True)
        return {'FINISHED'}


class WITCHER_OT_strings_browser_rebuild(bpy.types.Operator):
    """Rebuild the strings list from the current source"""

    bl_idname = "witcher.strings_browser_rebuild"
    bl_label = "Rebuild Strings List"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        ss.cache_clear()
        _SCENE_DIALOG_METADATA_CACHE.clear()
        _SCENE_DIALOG_LINE_CACHE.clear()
        _SCENE_DIALOG_FULL_LINE_CACHE.clear()
        _SCENE_DIALOG_SUMMARY_CACHE.clear()
        _SCENE_AUGMENTED_RECORDS_CACHE["key"] = None
        _SCENE_AUGMENTED_RECORDS_CACHE["records"] = None
        _refresh_records(context, force_rebuild=True)
        scene = context.scene
        settings = getattr(scene, "witcher_strings_browser", None)
        if settings is not None:
            self.report(
                {'INFO'},
                f"Loaded {settings.last_total:,} strings; {settings.last_filtered:,} match filters.",
            )
        return {'FINISHED'}


class WITCHER_OT_strings_browser_copy(bpy.types.Operator):
    """Copy a field from the selected string to the clipboard"""

    bl_idname = "witcher.strings_browser_copy"
    bl_label = "Copy"
    bl_options = {'INTERNAL'}

    field: StringProperty(default="text")

    @classmethod
    def description(cls, context, properties):
        return {
            "id": "Copy the string ID to the clipboard",
            "voice_id": "Copy the voice line ID to the clipboard",
            "key": "Copy the string key to the clipboard",
            "voiceover": "Copy the voiceover name to the clipboard",
            "speaker": "Copy the speaker name to the clipboard",
            "property": "Copy the string property name to the clipboard",
            "resource": "Copy the string resource to the clipboard",
            "text": "Copy the displayed string text to the clipboard",
        }.get(properties.field, "Copy to clipboard")

    def execute(self, context):
        scene = context.scene
        settings = getattr(scene, "witcher_strings_browser", None)
        if settings is None or settings.active_index < 0 or settings.active_index >= len(settings.items):
            self.report({'WARNING'}, "No string selected.")
            return {'CANCELLED'}
        item = settings.items[settings.active_index]
        value = {
            "id": item.string_id_str,
            "voice_id": item.voice_line_id,
            "key": item.string_key,
            "voiceover": item.voiceover,
            "speaker": item.speaker,
            "property": item.property_name,
            "resource": item.resource,
            "text": item.text,
        }.get(self.field, "")
        if not value:
            self.report({'WARNING'}, f"Selected string has no {self.field}.")
            return {'CANCELLED'}
        context.window_manager.clipboard = value
        self.report({'INFO'}, f"Copied {self.field} to clipboard.")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Asset Browser launcher row (drawn by ui_file_browser via draw_launcher)
# ---------------------------------------------------------------------------

def draw_launcher(layout):
    """Draw the Strings Browser launcher box (W2/W3 buttons).

    Caller passes its parent layout; we render a self-contained box so the
    asset browser panel only needs one line.
    """

    box = layout.box()
    box.label(text="Browse Strings", icon='OUTLINER_DATA_FONT')
    col = box.column(align=True)
    col.scale_y = 1.4
    op = col.operator("witcher.open_strings_browser", text="Witcher 3 Strings", icon='OUTLINER_DATA_FONT')
    op.initial_game = ss.GAME_W3
    op = col.operator("witcher.open_strings_browser", text="Witcher 2 Strings", icon='OUTLINER_DATA_FONT')
    op.initial_game = ss.GAME_W2


classes = (
    WITCH_PG_StringsBrowserSpeakerItem,
    WITCH_PG_StringsAssociatedPathItem,
    WITCH_PG_StringsBrowserItem,
    WITCH_PG_StringsBrowserSettings,
    WITCHER_UL_strings_browser_speakers,
    WITCHER_UL_strings_browser,
    WITCHER_OT_strings_browser_open,
    WITCHER_OT_strings_browser_preview_voice,
    WITCHER_OT_strings_browser_page,
    WITCHER_OT_strings_browser_set_speaker,
    WITCHER_OT_strings_browser_filter_scene,
    WITCHER_OT_strings_browser_filter_resource,
    WITCHER_OT_strings_browser_search_speakers,
    WITCHER_OT_strings_browser_clear_filters,
    WITCHER_OT_strings_browser_back_select,
    WITCHER_OT_strings_browser_toggle_voicetag_entities,
    WITCHER_OT_strings_browser_rebuild,
    WITCHER_OT_strings_browser_copy,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.witcher_strings_browser = PointerProperty(
        type=WITCH_PG_StringsBrowserSettings,
        options={'SKIP_SAVE'},
    )


def unregister():
    if hasattr(bpy.types.Scene, "witcher_strings_browser"):
        delattr(bpy.types.Scene, "witcher_strings_browser")
    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            log.debug("Failed to unregister %s", cls, exc_info=True)
    ss.cache_clear()
    _FILTERED_CACHE["records"] = []
    _FILTERED_CACHE["key"] = None
    _SCENE_DIALOG_METADATA_CACHE.clear()
    _SCENE_DIALOG_LINE_CACHE.clear()
    _SCENE_DIALOG_FULL_LINE_CACHE.clear()
    _SCENE_DIALOG_SUMMARY_CACHE.clear()
    _SCENE_AUGMENTED_RECORDS_CACHE["key"] = None
    _SCENE_AUGMENTED_RECORDS_CACHE["records"] = None
