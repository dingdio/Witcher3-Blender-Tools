"""Strings Browser popup + supporting operators.

Style intent: similar to the existing Witcher Asset Browser. A single popup
operator (``witcher.open_strings_browser``) brings up a dialog with the game
selector on top, source selector, search/speaker filters and a paged list of
strings. Closing the popup leaves no state behind in saved blend files
(SKIP_SAVE everywhere).
"""

from __future__ import annotations

import logging
import math
import os
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


PAGE_SIZE_DEFAULT = 200
PAGE_SIZE_MIN = 50
PAGE_SIZE_MAX = 1000

DEFAULT_POPUP_WIDTH = 900
STRINGS_SOUND_PREVIEW_TYPE = "Strings Browser Voice"


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


class WITCH_PG_StringsBrowserItem(bpy.types.PropertyGroup):
    """One row in the browser UIList (kept short — full data lives in the cache)."""

    cache_index: IntProperty(default=-1)
    game: StringProperty(default="")
    source: StringProperty(default="")
    string_id: IntProperty(default=0)
    string_id_str: StringProperty(default="")
    string_key: StringProperty(default="")
    voiceover: StringProperty(default="")
    has_voice: BoolProperty(default=False)
    speaker: StringProperty(default="")
    speaker_display: StringProperty(default="")
    text: StringProperty(default="")
    resource: StringProperty(default="")
    property_name: StringProperty(default="")
    db_path: StringProperty(default="")


def _on_filter_update(self, context):
    scene = getattr(context, "scene", None)
    if scene is None:
        return
    settings = getattr(scene, "witcher_strings_browser", None)
    if settings is None:
        return
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
    if settings.page_index != 0:
        settings.page_index = 0
    settings.speaker_filter = ""
    _refresh_records(context, force_rebuild=True)


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

    page_size: IntProperty(
        name="Rows",
        default=PAGE_SIZE_DEFAULT,
        min=PAGE_SIZE_MIN,
        max=PAGE_SIZE_MAX,
    )
    page_index: IntProperty(default=0)
    last_total: IntProperty(default=0)
    last_filtered: IntProperty(default=0)
    last_built_count: IntProperty(default=0)
    last_error: StringProperty(default="")
    last_db_used: StringProperty(default="")

    items: CollectionProperty(type=WITCH_PG_StringsBrowserItem)
    active_index: IntProperty(default=-1)


# ---------------------------------------------------------------------------
# Cache + rebuild
# ---------------------------------------------------------------------------

# Holds the currently displayed filtered record list so we don't repeatedly
# rebuild PropertyGroups for every redraw. Keyed by (game, source, db_path).
_FILTERED_CACHE = {"records": [], "key": None}


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
    """Whether this record has enough W3 voice metadata to offer preview."""

    if str(rec.get("game", "") or "").upper() != ss.GAME_W3:
        return False
    if not str(rec.get("string_id_str", "") or "").strip():
        return False
    return bool(
        str(rec.get("voiceover", "") or "").strip()
        or str(rec.get("speaker_code", "") or "").strip()
    )


def _voice_preview_item_key(line_id):
    line_id = str(line_id or "").strip()
    return f"strings_browser\\speech\\{line_id}.wem" if line_id else ""


def _is_voice_preview_playing(line_id):
    key = _voice_preview_item_key(line_id)
    if not key:
        return False
    try:
        from ..ui import ui_file_browser

        return ui_file_browser.sound_preview_matches(STRINGS_SOUND_PREVIEW_TYPE, key)
    except Exception:
        return False


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

    records = ss.get_records(game, source, language=language, db_path=db_path, force_reload=force_rebuild)
    filtered = ss.filter_records(
        records,
        search_text=settings.search_text,
        speaker_filter=settings.speaker_filter,
    )

    _FILTERED_CACHE["records"] = filtered
    _FILTERED_CACHE["key"] = (game, source, db_path)

    settings.last_total = len(records)
    settings.last_filtered = len(filtered)
    settings.last_built_count = len(records)
    settings.last_db_used = db_path or ""
    settings.last_error = ss.get_last_error(game, source, db_path=db_path, language=language) or ""

    _refresh_page(context, settings)


def _refresh_page(context, settings):
    items = settings.items
    items.clear()

    filtered = _FILTERED_CACHE.get("records", [])
    if not filtered:
        settings.active_index = -1
        return

    page_size = max(PAGE_SIZE_MIN, min(PAGE_SIZE_MAX, settings.page_size or PAGE_SIZE_DEFAULT))
    total_pages = max(1, int(math.ceil(len(filtered) / page_size)))
    page_index = max(0, min(settings.page_index, total_pages - 1))
    if settings.page_index != page_index:
        settings.page_index = page_index

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
        item.voiceover = rec.get("voiceover", "")
        item.has_voice = _record_has_voice(rec)
        item.speaker = rec.get("speaker", "")
        item.speaker_display = rec.get("speaker_display", "")
        item.text = rec.get("text", "")
        item.resource = rec.get("resource", "")
        item.property_name = rec.get("property", "")
        item.db_path = rec.get("db_path", "")

    if items and (settings.active_index < 0 or settings.active_index >= len(items)):
        settings.active_index = 0


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
    items: CollectionProperty(type=WITCH_PG_StringsBrowserSpeakerItem)
    items_index: IntProperty(default=0)

    def _build_full_speaker_list(self, context):
        """Collect every speaker from the unfiltered source records.

        Pulls the full record list via ``ss.get_records`` (a cache hit when
        the source is loaded) so the speaker picker shows every speaker in
        the active DB / binary table, not just those matching the active
        text-search filter.
        """
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
        records = ss.get_records(game, source, language=language, db_path=db_path) or []

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
        settings = getattr(scene, "witcher_strings_browser", None)
        if settings is None:
            self.report({'ERROR'}, "Strings browser settings not registered.")
            return {'CANCELLED'}

        # Make sure the underlying records exist — the popup builds its list
        # off the in-memory filter cache, which is populated by every refresh.
        if not (_FILTERED_CACHE.get("records") or []):
            _refresh_records(context, force_rebuild=False)

        self.filter_text = ""
        self._rebuild_items(context)
        if not len(self.items):
            self.report({'WARNING'}, "No speakers in the current source.")
            return {'CANCELLED'}

        # Preselect the current filter if any.
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
        info.label(text=f"{total} speaker(s) — click OK to filter the main list", icon='INFO')

    def execute(self, context):
        scene = context.scene
        settings = getattr(scene, "witcher_strings_browser", None)
        if settings is None:
            return {'CANCELLED'}
        if not (0 <= self.items_index < len(self.items)):
            return {'CANCELLED'}
        chosen = self.items[self.items_index]
        # Setting this fires _on_filter_update which re-applies the filter.
        settings.speaker_filter = chosen.code
        return {'FINISHED'}


class WITCHER_UL_strings_browser(bpy.types.UIList):
    use_filter_show = False
    use_filter_sort_alpha = False

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type not in {'DEFAULT', 'COMPACT'}:
            layout.label(text=f"{item.string_id_str} {item.text}")
            return

        row = layout.row(align=True)
        if getattr(item, "has_voice", False):
            op = row.operator(
                "witcher.strings_browser_preview_voice",
                text="",
                icon='CANCEL' if _is_voice_preview_playing(item.string_id_str) else 'PLAY',
            )
            op.line_id = item.string_id_str
            op.text = item.text
            op.speaker = item.speaker_display or item.speaker
            op.voiceover = item.voiceover
        else:
            row.label(text="", icon='BLANK1')
        # ID
        id_split = row.split(factor=0.16, align=True)
        id_split.label(text=item.string_id_str or "-")
        # Speaker
        rest = id_split.split(factor=0.22, align=True)
        rest.label(text=item.speaker or "-", icon='USER' if item.speaker else 'BLANK1')
        # Text (longest column)
        rest.label(text=item.text or "<no text>")

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

        if self.initial_game and settings.active_game != self.initial_game:
            settings.active_game = self.initial_game
        settings.search_text = ""
        settings.speaker_filter = ""
        settings.page_index = 0
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

        # ── Game-specific source/DB picker ────────────────────────
        if settings.active_game == ss.GAME_W3:
            self._draw_w3_source(layout, settings)
        else:
            self._draw_w2_source(layout, settings)

        layout.separator(factor=0.5)

        # ── Filters ───────────────────────────────────────────────
        filter_box = layout.box()
        filter_box.label(text="Filters", icon='FILTER')
        row = filter_box.row(align=True)
        row.prop(settings, "search_text", text="", icon='VIEWZOOM')
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

        speakers_box = filter_box.box()
        speakers_row = speakers_box.row(align=True)
        speakers_row.scale_y = 0.9
        speakers_row.label(text="Top speakers:", icon='USER')
        speakers_grid = speakers_box.grid_flow(columns=4, even_columns=True, align=True)
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

        page_size = max(PAGE_SIZE_MIN, min(PAGE_SIZE_MAX, settings.page_size or PAGE_SIZE_DEFAULT))
        total_pages = max(1, int(math.ceil(settings.last_filtered / page_size)))
        page_row = layout.row(align=True)
        page_row.scale_y = 1.1
        nav = page_row.row(align=True)
        op = nav.operator("witcher.strings_browser_page", text="", icon='REW')
        op.action = "first"
        op = nav.operator("witcher.strings_browser_page", text="", icon='TRIA_LEFT')
        op.action = "prev"
        nav.label(text=f"Page {settings.page_index + 1} / {total_pages}")
        op = nav.operator("witcher.strings_browser_page", text="", icon='TRIA_RIGHT')
        op.action = "next"
        op = nav.operator("witcher.strings_browser_page", text="", icon='FF')
        op.action = "last"

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
        detail_box.label(text="Selected String", icon='OUTLINER_DATA_FONT')
        if 0 <= settings.active_index < len(settings.items):
            item = settings.items[settings.active_index]
            grid = detail_box.column(align=True)
            grid.use_property_split = True
            grid.use_property_decorate = False
            grid.label(text=f"ID: {item.string_id_str}")
            if item.string_key:
                grid.label(text=f"Key: {item.string_key}")
            if item.voiceover:
                grid.label(text=f"Voiceover: {item.voiceover}")
            if item.speaker:
                grid.label(text=f"Speaker: {item.speaker}")
            if item.property_name:
                grid.label(text=f"Property: {item.property_name}")
            if item.resource:
                grid.label(text=f"Resource: {item.resource}")
            text_col = detail_box.column(align=True)
            text_col.label(text="Text:")
            # Multi-line text box: stack labels per visual line.
            text_value = item.text or ""
            if text_value:
                for chunk in _split_text_for_display(text_value, 110):
                    text_col.label(text=chunk)
            else:
                text_col.label(text="<empty>")

            # Copy buttons mirror the asset-browser pattern for path copying.
            copy_row = detail_box.row(align=True)
            if item.has_voice:
                op = copy_row.operator(
                    "witcher.strings_browser_preview_voice",
                    text="Preview Audio",
                    icon='CANCEL' if _is_voice_preview_playing(item.string_id_str) else 'PLAY',
                )
                op.line_id = item.string_id_str
                op.text = item.text
                op.speaker = item.speaker_display or item.speaker
                op.voiceover = item.voiceover
            op = copy_row.operator("witcher.strings_browser_copy", text="Copy ID", icon='COPYDOWN')
            op.field = "id"
            op = copy_row.operator("witcher.strings_browser_copy", text="Copy Key", icon='COPYDOWN')
            op.field = "key"
            op = copy_row.operator("witcher.strings_browser_copy", text="Copy Voiceover", icon='COPYDOWN')
            op.field = "voiceover"
            op = copy_row.operator("witcher.strings_browser_copy", text="Copy Text", icon='COPYDOWN')
            op.field = "text"
        else:
            detail_box.label(text="No row selected.", icon='INFO')

    # ------------------------------------------------------------------
    # Helpers used by draw()
    # ------------------------------------------------------------------

    def _draw_w3_source(self, layout, settings):
        box = layout.box()
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

    def _draw_w2_source(self, layout, settings):
        box = layout.box()
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


def _extract_speech_wem_for_preview(context, line_id):
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


class WITCHER_OT_strings_browser_preview_voice(bpy.types.Operator):
    """Play/stop preview audio for a voiced string."""

    bl_idname = "witcher.strings_browser_preview_voice"
    bl_label = "Preview Voice"
    bl_options = {'INTERNAL'}

    line_id: StringProperty(default="")
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
        self.line_id = item.string_id_str
        self.text = item.text
        self.speaker = item.speaker_display or item.speaker
        self.voiceover = item.voiceover

    def execute(self, context):
        self._resolve_from_selection(context)
        line_id = str(self.line_id or "").strip()
        if not line_id:
            self.report({'WARNING'}, "No voiced string selected.")
            return {'CANCELLED'}

        preview_key = _voice_preview_item_key(line_id)
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
            wem_path, language = _extract_speech_wem_for_preview(context, line_id)
            wav_path = ui_file_browser.play_sound_file_preview(
                context,
                wem_path,
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
        page_size = max(PAGE_SIZE_MIN, min(PAGE_SIZE_MAX, settings.page_size or PAGE_SIZE_DEFAULT))
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
        settings.page_index = target
        _refresh_page(context, settings)
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


class WITCHER_OT_strings_browser_clear_filters(bpy.types.Operator):
    """Clear search text and speaker filter"""

    bl_idname = "witcher.strings_browser_clear_filters"
    bl_label = "Clear Filters"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        scene = context.scene
        settings = getattr(scene, "witcher_strings_browser", None)
        if settings is None:
            return {'CANCELLED'}
        settings.search_text = ""
        settings.speaker_filter = ""
        settings.page_index = 0
        return {'FINISHED'}


class WITCHER_OT_strings_browser_rebuild(bpy.types.Operator):
    """Rebuild the strings list from the current source"""

    bl_idname = "witcher.strings_browser_rebuild"
    bl_label = "Rebuild Strings List"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        ss.cache_clear()
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
            "key": "Copy the string key to the clipboard",
            "voiceover": "Copy the voiceover name to the clipboard",
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
            "key": item.string_key,
            "voiceover": item.voiceover,
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
    WITCH_PG_StringsBrowserItem,
    WITCH_PG_StringsBrowserSettings,
    WITCHER_UL_strings_browser_speakers,
    WITCHER_UL_strings_browser,
    WITCHER_OT_strings_browser_open,
    WITCHER_OT_strings_browser_preview_voice,
    WITCHER_OT_strings_browser_page,
    WITCHER_OT_strings_browser_set_speaker,
    WITCHER_OT_strings_browser_search_speakers,
    WITCHER_OT_strings_browser_clear_filters,
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
