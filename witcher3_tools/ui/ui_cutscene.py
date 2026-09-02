import logging
import math
import os
import shlex
import subprocess
import time
from collections import Counter

import bpy
import blf
from bpy.types import Panel, Operator, UIList, PropertyGroup
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy_extras.io_utils import ImportHelper

from .. import dialog_language
from .. import get_tts_command, get_uncook_path
from ..CR2W import w3_types
from ..importers import import_cutscene
from ..exporters import export_anims, export_cutscene
from ..animation import cutscene_bake
from ..animation.camera_tracks import CAMERA_CONTROL_BONE, CAMERA_TRACK_LABELS, CAMERA_TRACK_NAMES
from .ui_cr2w_fields import (
    _draw_imported_class_sections,
    _format_imported_field_value,
    _get_imported_field_type,
    _get_imported_field_schema,
    _get_imported_field_value,
    _get_present_imported_fields,
)
from ..ui.ui_utils import WITCH_PT_Base

log = logging.getLogger(__name__)

_CUTSCENE_SYNC_DEFERRED = set()
_SCRATCH_CUTSCENE_TRACK_NAME = "cutscene_anim"
AUTHORED_CLIP_ID_BASE = 1_000_000
AUTHORED_CLIP_SEQUENCE_PROP = "witcher_cutscene_authored_clip_sequence"
CLIP_IDENTITY_SCHEMA_PROP = "witcher_cutscene_clip_identity_schema"
CLIP_IDENTITY_SCHEMA_VERSION = 1
_CUTSCENE_IMPORT_NLA_TRACK_COMPONENTS = {
    "anim_import": "Root",
    "mimic_import": "face",
}
_CUTSCENE_BROWSE_RESULT_LIMIT = 100
_CUTSCENE_DIALOG_VOICE_RESULT_LIMIT = 100
_AUTHORED_DIALOG_SOURCE_PATH = "witcher_cutscene_authored_dialogue"
_AUTHORED_DIALOG_NLA_PREFIX = "cutscene_import_dialog_voice_"
_CUTSCENE_BROWSE_CATEGORY_ITEMS = [("ALL", "All", "All categories")]
_CUTSCENE_BROWSE_CATEGORY_ITEM_HISTORY = [_CUTSCENE_BROWSE_CATEGORY_ITEMS]
_subtitle_draw_handle = None


def _scene_fps(scene):
    render = getattr(scene, "render", None)
    fps = float(getattr(render, "fps", 30.0) or 30.0)
    fps_base = float(getattr(render, "fps_base", 1.0) or 1.0)
    return fps / fps_base if fps_base > 0.0 else fps


def _coerce_cutscene_index(value, default=-1):
    try:
        return int(value)
    except Exception:
        return int(default)


def _strip_get(strip, prop_name, default=""):
    try:
        return strip.get(prop_name, default)
    except Exception:
        return default


def _subtitle_text_from_strip(strip):
    for prop_name in (
        dialog_language.DIALOG_SUBTITLE_TEXT_PROP,
        import_cutscene.CUTSCENE_DIALOG_TEXT_PROP,
        "witcher_w2scene_dialog_text",
    ):
        text = str(_strip_get(strip, prop_name, "") or "").strip()
        if text:
            return text
    return ""


def _active_subtitle_from_sound_strips(scene, frame):
    try:
        from .ui_voice import _get_sequence_editor_strips
    except Exception:
        return None

    strips = _get_sequence_editor_strips(getattr(scene, "sequence_editor", None))
    if strips is None:
        return None

    frame = float(frame)
    for strip in list(strips):
        if getattr(strip, "type", None) != 'SOUND':
            continue
        text = _subtitle_text_from_strip(strip)
        if not text:
            continue
        start = float(getattr(strip, "frame_final_start", getattr(strip, "frame_start", 0.0)) or 0.0)
        end = float(getattr(strip, "frame_final_end", start) or start)
        if end <= start:
            end = start + 1.0
        if start <= frame < end:
            return text
    return None


def _active_subtitle_from_cutscene_items(scene, frame):
    for item in getattr(scene, "witcher_cutscene_dialog_items", []) or []:
        text = str(getattr(item, "line_text", "") or "").strip()
        if not text:
            continue
        try:
            start_frame = int(getattr(item, "start_frame", 0) or 0)
            end_frame = int(getattr(item, "end_frame", 0) or 0)
        except Exception:
            continue
        if start_frame <= int(frame) < end_frame:
            return text
    return None


def _active_subtitle_from_w2scene_elements(scene, frame):
    try:
        active_section_index = int(getattr(scene, "witcher_w2scene_active_subtitle_section_index", -1))
    except Exception:
        active_section_index = -1
    if active_section_index < 0:
        return None
    fps = _scene_fps(scene)
    frame_offset = float(getattr(scene, "witcher_w2scene_section_subtitle_frame_offset", 0.0) or 0.0)
    frame = float(frame)
    for item in getattr(scene, "witcher_w2scene_section_element_items", []) or []:
        try:
            if int(getattr(item, "section_index", -1)) != active_section_index:
                continue
        except Exception:
            continue
        if str(getattr(item, "element_type", "") or "") != "CStorySceneLine":
            continue
        text = str(getattr(item, "line_text", "") or "").strip()
        if not text:
            continue
        try:
            start_frame = frame_offset + (float(getattr(item, "start_time", 0.0) or 0.0) * fps)
            duration_frames = max(1.0, float(getattr(item, "duration", 0.0) or 0.0) * fps)
        except Exception:
            continue
        if start_frame <= frame < (start_frame + duration_frames):
            return text
    return None


def _cutscene_get_active_subtitle(scene, frame):
    if scene is None or not bool(getattr(scene, "witcher_cutscene_show_dialog_subtitles", True)):
        return None
    for getter in (
        _active_subtitle_from_cutscene_items,
        _active_subtitle_from_sound_strips,
        _active_subtitle_from_w2scene_elements,
    ):
        text = getter(scene, frame)
        if text:
            return text
    return None


def _active_w2scene_choices(scene, frame):
    if scene is None or not bool(getattr(scene, "witcher_cutscene_show_dialog_subtitles", True)):
        return []
    try:
        active_section_index = int(getattr(scene, "witcher_w2scene_active_subtitle_section_index", -1))
    except Exception:
        active_section_index = -1
    if active_section_index < 0:
        return []

    fps = _scene_fps(scene)
    frame_offset = float(getattr(scene, "witcher_w2scene_section_subtitle_frame_offset", 0.0) or 0.0)
    frame = float(frame)
    choices = []
    for item in getattr(scene, "witcher_w2scene_choice_items", []) or []:
        try:
            if int(getattr(item, "section_index", -1)) != active_section_index:
                continue
        except Exception:
            continue
        try:
            start_frame = frame_offset + (float(getattr(item, "start_time", 0.0) or 0.0) * fps)
            duration_frames = max(1.0, float(getattr(item, "duration", 0.0) or 0.0) * fps)
        except Exception:
            continue
        if not (start_frame <= frame <= (start_frame + duration_frames)):
            continue
        text = str(getattr(item, "choice_text", "") or "").strip()
        if not text:
            continue
        choices.append({
            "index": int(getattr(item, "choice_index", len(choices)) or 0),
            "text": text,
            "emphasis": bool(getattr(item, "is_emphasized", False) or False),
        })
    choices.sort(key=lambda item: item["index"])
    return choices


def _set_blf_size(font_id, font_size):
    try:
        blf.size(font_id, font_size)
    except TypeError:
        blf.size(font_id, font_size, 72)


def _wrap_subtitle_text(font_id, text, max_width):
    words = str(text or "").split()
    if not words:
        return []

    lines = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        width, _height = blf.dimensions(font_id, candidate)
        if current and width > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _draw_blf_text_with_shadow(font_id, text, x, y, color=(1.0, 1.0, 1.0, 1.0)):
    try:
        blf.color(font_id, 0.0, 0.0, 0.0, 0.85)
    except Exception:
        pass
    blf.position(font_id, x + 2.0, y - 2.0, 0)
    blf.draw(font_id, text)

    try:
        blf.color(font_id, *color)
    except Exception:
        pass
    blf.position(font_id, x, y, 0)
    blf.draw(font_id, text)


def draw_cutscene_subtitle():
    context = bpy.context
    scene = getattr(context, "scene", None)
    region = getattr(context, "region", None)
    if scene is None or region is None:
        return

    current_frame = getattr(scene, "frame_current", 0)
    text = _cutscene_get_active_subtitle(scene, current_frame)
    choices = _active_w2scene_choices(scene, current_frame)
    if not text and not choices:
        return

    font_id = 0
    font_size = int(getattr(scene, "witcher_cutscene_subtitle_font_size", 28) or 28)
    font_size = max(12, min(72, font_size))
    _set_blf_size(font_id, font_size)

    max_width = max(120.0, float(getattr(region, "width", 0) or 0) * 0.82)
    lines = _wrap_subtitle_text(font_id, text, max_width) if text else []
    line_height = font_size * 1.25
    base_y = 60.0

    for idx, line in enumerate(reversed(lines)):
        text_width, _text_height = blf.dimensions(font_id, line)
        x = (float(region.width) - text_width) / 2.0
        y = base_y + (idx * line_height)
        _draw_blf_text_with_shadow(font_id, line, x, y)

    if not choices:
        return

    choice_font_size = font_size
    _set_blf_size(font_id, choice_font_size)
    region_width = float(getattr(region, "width", 0) or 0)
    region_height = float(getattr(region, "height", 0) or 0)
    choice_max_width = max(120.0, region_width * 0.62)
    choice_line_height = choice_font_size * 1.22
    choice_lines = []
    for display_index, choice in enumerate(choices, start=1):
        wrapped = _wrap_subtitle_text(font_id, f"{display_index}. {choice['text']}", choice_max_width)
        if not wrapped:
            continue
        for line in wrapped:
            choice_lines.append((line, bool(choice.get("emphasis", False))))
    if not choice_lines:
        return

    choice_block_width = min(
        choice_max_width,
        max(blf.dimensions(font_id, line)[0] for line, _emphasized in choice_lines),
    )
    choice_left = max(24.0, (region_width - choice_block_width) / 2.0)
    subtitle_top_y = base_y + ((len(lines) - 1) * line_height if lines else 0.0)
    choice_bottom_y = subtitle_top_y + (line_height + 16.0 if lines else 0.0)
    choice_top_y = choice_bottom_y + ((len(choice_lines) - 1) * choice_line_height)
    max_top_y = max(40.0, region_height - 48.0)
    if choice_top_y > max_top_y:
        choice_top_y = max_top_y

    for idx, (line, emphasized) in enumerate(choice_lines):
        y = choice_top_y - (idx * choice_line_height)
        color = (1.0, 0.88, 0.42, 1.0) if emphasized else (1.0, 1.0, 1.0, 1.0)
        _draw_blf_text_with_shadow(font_id, line, choice_left, y, color=color)


def enable_cutscene_subtitles():
    global _subtitle_draw_handle
    if _subtitle_draw_handle is not None:
        return
    try:
        _subtitle_draw_handle = bpy.types.SpaceView3D.draw_handler_add(
            draw_cutscene_subtitle,
            (),
            'WINDOW',
            'POST_PIXEL',
        )
    except Exception:
        _subtitle_draw_handle = None
        log.debug("Could not register cutscene subtitle draw handler.", exc_info=True)


def disable_cutscene_subtitles():
    global _subtitle_draw_handle
    if _subtitle_draw_handle is None:
        return
    try:
        bpy.types.SpaceView3D.draw_handler_remove(_subtitle_draw_handle, 'WINDOW')
    except Exception:
        log.debug("Could not remove cutscene subtitle draw handler.", exc_info=True)
    _subtitle_draw_handle = None


def _cs_text(value):
    return str(value or "").strip()


def _cs_yield_once(obj, seen):
    if obj is None or getattr(obj, "type", None) != 'ARMATURE':
        return
    obj_name = _cs_text(getattr(obj, "name", ""))
    if not obj_name or obj_name in seen:
        return
    seen.add(obj_name)
    yield obj


def _cs_mimic_face_armature(actor_obj):
    if actor_obj is None:
        return None
    mimic_name = _cs_text(actor_obj.get("mimicFace", ""))
    if mimic_name:
        mimic_obj = bpy.data.objects.get(mimic_name)
        if mimic_obj is not None and getattr(mimic_obj, "type", None) == 'ARMATURE':
            return mimic_obj
    return None


def _cs_iter_import_clip_armatures(actor_obj, scene):
    seen = set()
    try:
        related = export_cutscene._iter_cutscene_related_armatures(actor_obj, scene)
    except Exception:
        related = (actor_obj,)
    for obj in related:
        yield from _cs_yield_once(obj, seen)
    yield from _cs_yield_once(_cs_mimic_face_armature(actor_obj), seen)


def _collect_cutscene_import_nla_candidates(scene):
    candidates = []
    try:
        actor_roots = export_cutscene._collect_cutscene_actor_roots(scene)
    except Exception:
        actor_roots = []
    for actor_obj in actor_roots:
        actor_name = _cs_text(actor_obj.get("cutscene_actor_name", ""))
        if not actor_name:
            continue
        for source_obj in _cs_iter_import_clip_armatures(actor_obj, scene):
            anim_data = getattr(source_obj, "animation_data", None)
            if anim_data is None:
                continue
            for track in getattr(anim_data, "nla_tracks", []) or []:
                track_name = _cs_text(getattr(track, "name", ""))
                component = _CUTSCENE_IMPORT_NLA_TRACK_COMPONENTS.get(track_name)
                if component is None:
                    continue
                for strip in getattr(track, "strips", []) or []:
                    if getattr(strip, "mute", False):
                        continue
                    action = getattr(strip, "action", None)
                    if action is None:
                        continue
                    candidates.append({
                        "actor_name": actor_name,
                        "actor_object_name": getattr(actor_obj, "name", ""),
                        "source_object_name": getattr(source_obj, "name", ""),
                        "track_name": track_name,
                        "strip_name": _cs_text(getattr(strip, "name", "")),
                        "action_name": _cs_text(getattr(action, "name", "")),
                        "component": component,
                        "frame_start": float(getattr(strip, "frame_start", 0.0) or 0.0),
                        "frame_end": float(getattr(strip, "frame_end", 0.0) or 0.0),
                    })
    candidates.sort(key=lambda item: (
        item["actor_name"].lower(),
        item["frame_start"],
        item["component"].lower(),
        item["source_object_name"].lower(),
        item["strip_name"].lower(),
    ))
    return candidates


def _cs_find_camera_armature(context):
    scene = getattr(context, "scene", None)
    if scene is None:
        return None
    for obj in scene.objects:
        if getattr(obj, "type", None) == 'ARMATURE':
            if import_cutscene._is_cutscene_camera_actor_metadata(actor_obj=obj):
                return obj
    return None

def _browse_animation_category_items(_state, _context):
    return _CUTSCENE_BROWSE_CATEGORY_ITEMS


def _clear_browse_animation_selection(state):
    state.selected_animation_id = ""
    state.selected_caption = ""
    state.selected_repo_path = ""
    state.selected_frames = 0
    state.selected_duration = 0.0
    state.selected_component = ""
    state.selected_source_game = ""


def select_cutscene_browse_animation_result(state, index):
    state.refreshing = True
    try:
        _clear_browse_animation_selection(state)
        index = int(index)
        if index < 0 or index >= len(state.results):
            return False
        row = state.results[index]
        state.selected_animation_id = str(row.animation_id or "")
        state.selected_caption = str(row.caption or "")
        state.selected_repo_path = str(row.repo_path or "")
        state.selected_frames = int(row.frames)
        state.selected_duration = float(row.duration)
        component = str(row.component or "BODY").strip().upper()
        if component not in {"BODY", "FACE"}:
            component = "BODY"
        state.selected_component = component
        state.component = component
        state.selected_source_game = str(row.source_game or "")
        return True
    finally:
        state.refreshing = False


def refresh_cutscene_browse_animation_results(state, context=None):
    context = context or bpy.context
    actor_obj = bpy.data.objects.get(str(state.actor_object_name or ""))
    state.refreshing = True
    try:
        state.results.clear()
        state.result_index = -1
        _clear_browse_animation_selection(state)
        if actor_obj is None or getattr(actor_obj, "type", None) != 'ARMATURE':
            state.match_status = "Target actor is unavailable"
            return 0

        from .ui_anims_list import build_animation_catalog_records, filter_animation_catalog_records

        records = build_animation_catalog_records(
            context,
            actor_obj,
            source_game=str(state.source or "AUTO"),
            compatible_only=str(state.compatibility or "COMPATIBLE") == "COMPATIBLE",
        )
        categories = sorted({str(row.get("category", "") or "") for row in records if row.get("category")})
        category_items = [("ALL", "All", "All categories")] + [
            (category, category, f"Show {category}") for category in categories
        ]
        global _CUTSCENE_BROWSE_CATEGORY_ITEMS
        if category_items != _CUTSCENE_BROWSE_CATEGORY_ITEMS:
            _CUTSCENE_BROWSE_CATEGORY_ITEMS = category_items
            _CUTSCENE_BROWSE_CATEGORY_ITEM_HISTORY.append(_CUTSCENE_BROWSE_CATEGORY_ITEMS)
        category = str(state.category or "ALL")
        if category not in {item[0] for item in _CUTSCENE_BROWSE_CATEGORY_ITEMS}:
            category = "ALL"
            state.category = "ALL"
        filtered = filter_animation_catalog_records(
            records,
            query=str(state.query or ""),
            category=category,
            component="",
        )
        total = len(filtered)
        for record in filtered[:_CUTSCENE_BROWSE_RESULT_LIMIT]:
            row = state.results.add()
            row.animation_id = str(record.get("animation_id", "") or "")
            row.caption = str(record.get("caption", "") or "")
            row.category = str(record.get("category", "") or "")
            row.repo_path = str(record.get("repo_path", "") or "")
            row.frames = int(record.get("frames", 0) or 0)
            row.duration = float(record.get("duration", row.frames / 30.0) or 0.0)
            row.component = str(record.get("component", "BODY") or "BODY")
            row.source_game = str(record.get("source_game", "w3") or "w3")
            row.compatible = bool(record.get("compatible", False))
        visible = len(state.results)
        state.match_status = (
            f"Showing {visible} of {total} — refine the search"
            if total > visible else f"{total} animation{'s' if total != 1 else ''}"
        )
        return visible
    except Exception as exc:
        state.match_status = "Catalog failed — see console"
        log.exception("cutscene animation catalog failed: %s", exc)
        return 0
    finally:
        state.refreshing = False


def reset_cutscene_browse_animation_dialog(operator, context):
    state = context.window_manager.witcher_cutscene_browse_animation
    actor_name = str(getattr(operator, "actor_object_name", "") or "")
    if not actor_name:
        actor = _active_cutscene_actor_armature(context.scene)
        actor_name = str(getattr(actor, "name", "") or "")
        operator.actor_object_name = actor_name
    state.refreshing = True
    try:
        state.actor_object_name = actor_name
        state.query = ""
        state.source = "AUTO"
        state.compatibility = "COMPATIBLE"
        state.category = "ALL"
        state.component = "BODY"
        state.placement = "CURRENT"
        state.results.clear()
        state.result_index = -1
        state.match_status = ""
        _clear_browse_animation_selection(state)
    finally:
        state.refreshing = False
    refresh_cutscene_browse_animation_results(state, context)
    return state


def _browse_animation_filter_updated(state, context):
    if state.refreshing:
        return
    refresh_cutscene_browse_animation_results(state, context)


def _browse_animation_result_index_updated(state, _context):
    if not state.refreshing:
        select_cutscene_browse_animation_result(state, state.result_index)


def _clear_cutscene_dialog_voice_selection(state):
    state.selected_voice_line_id = ""
    state.selected_line_id = ""
    state.selected_speaker = ""
    state.selected_text = ""
    state.selected_duration = ""
    state.selected_source_path = ""


def select_cutscene_dialog_voice_result(state, index):
    state.refreshing = True
    try:
        _clear_cutscene_dialog_voice_selection(state)
        index = int(index)
        if index < 0 or index >= len(state.results):
            return False
        row = state.results[index]
        state.selected_voice_line_id = str(row.voice_line_id or "")
        state.selected_line_id = str(row.line_id or row.voice_line_id or "")
        state.selected_speaker = str(row.speaker or "")
        state.selected_text = str(row.text or "")
        state.selected_duration = str(row.duration or "")
        state.selected_source_path = str(row.source_path or "")
        return True
    finally:
        state.refreshing = False


def refresh_cutscene_dialog_voice_results(state, context=None):
    context = context or bpy.context
    state.refreshing = True
    try:
        state.results.clear()
        state.result_index = -1
        _clear_cutscene_dialog_voice_selection(state)

        from . import ui_voice

        voice_nodes = ui_voice.get_voice_nodes_for_game(context, ui_voice.VOICE_GAME_W3)
        search_tokens, speaker_from_query = ui_voice._parse_search_tokens(str(state.query or ""))
        speaker_filter = str(speaker_from_query or state.speaker or "").strip().upper()
        # Resolve engine voicetags to cache speaker aliases.
        speaker_aliases = ui_voice.voice_speaker_aliases(speaker_filter)
        matches = []
        for node in voice_nodes:
            if str(node.get("game", ui_voice.VOICE_GAME_W3) or "").upper() != ui_voice.VOICE_GAME_W3:
                continue
            if speaker_aliases and str(node.get("speaker", "") or "").upper() not in speaker_aliases and not any(
                str(candidate or "").upper() in speaker_aliases
                for candidate in node.get("speaker_candidates") or []
            ):
                continue
            if not ui_voice._matches_voice_filter_fast(
                str(node.get("search_blob", "") or ""),
                str(node.get("speaker", "") or ""),
                search_tokens,
                "",
            ):
                continue
            matches.append(node)

        for node in matches[:_CUTSCENE_DIALOG_VOICE_RESULT_LIMIT]:
            row = state.results.add()
            row.voice_line_id = str(node.get("voiceLineId", "") or "")
            row.line_id = str(node.get("line_id", "") or row.voice_line_id)
            row.speaker = str(node.get("speaker", "") or "")
            row.text = str(node.get("text", "") or "")
            row.duration = str(node.get("duration", "") or "")
            row.display = str(node.get("display_compact", "") or node.get("name", "") or row.voice_line_id)
            row.source_path = str(node.get("source_path", "") or "")
        visible = len(state.results)
        total = len(matches)
        state.match_status = (
            f"Showing {visible} of {total}; refine the search"
            if total > visible else f"{total} voice line{'s' if total != 1 else ''}"
        )
        return visible
    except Exception as exc:
        state.match_status = "Voice cache failed - see console"
        log.exception("cutscene dialogue voice search failed: %s", exc)
        return 0
    finally:
        state.refreshing = False


def reset_cutscene_dialog_voice_dialog(operator, context):
    from . import ui_voice

    scene = context.scene
    state = context.window_manager.witcher_cutscene_dialog_voice
    lines = scene.witcher_cutscene_dialog_lines
    was_initialized = bool(state.initialized)
    cache_key = ui_voice.get_voice_cache_key_for_game(context, ui_voice.VOICE_GAME_W3)
    previous_index = _coerce_cutscene_index(getattr(state, "line_index", -1))
    index = _coerce_cutscene_index(getattr(operator, "line_index", -1))
    if not 0 <= index < len(lines):
        index = int(getattr(scene, "witcher_cutscene_dialog_line_index", 0) or 0)
    if not 0 <= index < len(lines):
        return None
    if not state.initialized:
        line = lines[index]
        state.refreshing = True
        try:
            state.query = ""
            state.speaker = str(getattr(line, "speaker", "") or "").strip().upper()
            state.results.clear()
            state.result_index = -1
            state.match_status = ""
            _clear_cutscene_dialog_voice_selection(state)
            state.initialized = True
            state.cache_key = cache_key
        finally:
            state.refreshing = False
        refresh_cutscene_dialog_voice_results(state, context)
    elif state.cache_key != cache_key:
        selected_voice_id = str(getattr(state, "selected_voice_line_id", "") or "")
        state.cache_key = cache_key
        refresh_cutscene_dialog_voice_results(state, context)
        for result_index, result in enumerate(state.results):
            if str(getattr(result, "voice_line_id", "") or "") == selected_voice_id:
                state.result_index = result_index
                break
    if was_initialized and previous_index != index:
        state.refreshing = True
        try:
            state.speaker = str(getattr(lines[index], "speaker", "") or "").strip().upper()
            state.result_index = -1
            _clear_cutscene_dialog_voice_selection(state)
        finally:
            state.refreshing = False
        refresh_cutscene_dialog_voice_results(state, context)
    state.line_index = index
    operator.line_index = index
    return state


def _cutscene_dialog_voice_filter_updated(state, context):
    if not state.refreshing:
        refresh_cutscene_dialog_voice_results(state, context)


def _cutscene_dialog_voice_result_index_updated(state, _context):
    if not state.refreshing:
        select_cutscene_dialog_voice_result(state, state.result_index)


class CutsceneBrowseAnimationItem(PropertyGroup):
    animation_id: StringProperty(default="")
    caption: StringProperty(default="")
    category: StringProperty(default="")
    repo_path: StringProperty(default="")
    frames: IntProperty(default=0)
    duration: FloatProperty(default=0.0)
    component: StringProperty(default="BODY")
    source_game: StringProperty(default="w3")
    compatible: BoolProperty(default=False)


class CutsceneBrowseAnimationState(PropertyGroup):
    actor_object_name: StringProperty(default="", options={'SKIP_SAVE'})
    query: StringProperty(
        name="Search",
        options={'SKIP_SAVE', 'TEXTEDIT_UPDATE'},
        update=_browse_animation_filter_updated,
    )
    source: EnumProperty(
        name="Source",
        items=[
            ("AUTO", "Auto", "Follow the target actor"),
            ("W3", "W3", "Witcher 3 catalog"),
            ("W2", "W2", "Witcher 2 catalog"),
        ],
        default="AUTO",
        options={'SKIP_SAVE'},
        update=_browse_animation_filter_updated,
    )
    compatibility: EnumProperty(
        name="Compatibility",
        items=[
            ("COMPATIBLE", "Compatible", "Only the target actor's animation sets"),
            ("ALL", "Show All", "Show the full source catalog"),
        ],
        default="COMPATIBLE",
        options={'SKIP_SAVE'},
        update=_browse_animation_filter_updated,
    )
    category: EnumProperty(
        name="Category",
        items=_browse_animation_category_items,
        options={'SKIP_SAVE'},
        update=_browse_animation_filter_updated,
    )
    component: EnumProperty(
        name="Component",
        items=[
            ("BODY", "Body", "Add to the body cutscene track"),
            ("FACE", "Face", "Add to the face cutscene track"),
        ],
        default="BODY",
        options={'SKIP_SAVE'},
    )
    placement: EnumProperty(
        name="Placement",
        items=[
            ("CURRENT", "At Current Frame", "Place without shifting existing strips"),
            ("AFTER_LAST", "After Last Clip", "Start at the actual end of the relevant cutscene tracks"),
        ],
        default="CURRENT",
        options={'SKIP_SAVE'},
    )
    results: CollectionProperty(type=CutsceneBrowseAnimationItem, options={'SKIP_SAVE'})
    result_index: IntProperty(default=-1, min=-1, options={'SKIP_SAVE'}, update=_browse_animation_result_index_updated)
    selected_animation_id: StringProperty(name="Animation", default="", options={'SKIP_SAVE'})
    selected_caption: StringProperty(name="Caption", default="", options={'SKIP_SAVE'})
    selected_repo_path: StringProperty(name="File", default="", options={'SKIP_SAVE'})
    selected_frames: IntProperty(name="Frames", default=0, options={'SKIP_SAVE'})
    selected_duration: FloatProperty(name="Duration", default=0.0, options={'SKIP_SAVE'})
    selected_component: StringProperty(default="", options={'SKIP_SAVE'})
    selected_source_game: StringProperty(default="", options={'SKIP_SAVE'})
    match_status: StringProperty(default="", options={'SKIP_SAVE'})
    refreshing: BoolProperty(default=False, options={'SKIP_SAVE'})


class CutsceneDialogVoiceResult(PropertyGroup):
    voice_line_id: StringProperty(default="")
    line_id: StringProperty(default="")
    speaker: StringProperty(default="")
    text: StringProperty(default="")
    duration: StringProperty(default="")
    display: StringProperty(default="")
    source_path: StringProperty(default="")


class CutsceneDialogVoiceState(PropertyGroup):
    initialized: BoolProperty(default=False, options={'SKIP_SAVE'})
    cache_key: StringProperty(default="", options={'SKIP_SAVE'})
    line_index: IntProperty(default=-1, min=-1, options={'SKIP_SAVE'})
    query: StringProperty(
        name="Search",
        description="Filter Witcher 3 voice lines by ID or words from the subtitle",
        options={'SKIP_SAVE', 'TEXTEDIT_UPDATE'},
        update=_cutscene_dialog_voice_filter_updated,
    )
    speaker: StringProperty(
        name="Speaker",
        description="Limit results to this Witcher 3 voice tag; clear it to search every speaker",
        options={'SKIP_SAVE', 'TEXTEDIT_UPDATE'},
        update=_cutscene_dialog_voice_filter_updated,
    )
    results: CollectionProperty(type=CutsceneDialogVoiceResult, options={'SKIP_SAVE'})
    result_index: IntProperty(
        default=-1,
        min=-1,
        options={'SKIP_SAVE'},
        update=_cutscene_dialog_voice_result_index_updated,
    )
    selected_voice_line_id: StringProperty(name="voiceFileName", default="", options={'SKIP_SAVE'})
    selected_line_id: StringProperty(name="dialogLine", default="", options={'SKIP_SAVE'})
    selected_speaker: StringProperty(name="Speaker", default="", options={'SKIP_SAVE'})
    selected_text: StringProperty(name="Text", default="", options={'SKIP_SAVE'})
    selected_duration: StringProperty(name="Duration", default="", options={'SKIP_SAVE'})
    selected_source_path: StringProperty(name="Source", default="", options={'SKIP_SAVE'})
    match_status: StringProperty(default="", options={'SKIP_SAVE'})
    refreshing: BoolProperty(default=False, options={'SKIP_SAVE'})

class CutsceneActorPreviewItem(PropertyGroup):
    source_index: IntProperty(default=-1)
    label: StringProperty(default="")
    actor_name: StringProperty(default="")
    template_path: StringProperty(default="")
    source_game: StringProperty(default="")
    appearance_name: StringProperty(default="")
    actor_type: StringProperty(default="")
    use_mimic: BoolProperty(default=False)
    already_in_scene: BoolProperty(default=False)
    selected: BoolProperty(name="Import", default=True)

class CutsceneAnimationPreviewItem(PropertyGroup):
    source_index: IntProperty(default=-1)
    full_name: StringProperty(default="")
    display_name: StringProperty(default="")
    actor_name: StringProperty(default="")
    component_name: StringProperty(default="")
    frames_per_second: FloatProperty(default=0.0)
    num_frames: IntProperty(default=0)
    duration: FloatProperty(default=0.0)
    selected: BoolProperty(name="Import", default=True)


class CutsceneCastCandidateItem(PropertyGroup):
    label: StringProperty(default="")
    template_path: StringProperty(default="")
    category: StringProperty(default="")
    rig_summary: StringProperty(default="")
    indexed: BoolProperty(default=False)


class CutsceneLoadedActorItem(PropertyGroup):
    source_index: IntProperty(default=-1)
    label: StringProperty(default="")
    actor_name: StringProperty(default="")
    tag: StringProperty(default="")
    voice_tag: StringProperty(default="")
    template_path: StringProperty(default="")
    source_game: StringProperty(default="")
    appearance_name: StringProperty(default="")
    actor_type: StringProperty(default="")
    final_position: StringProperty(default="")
    kill_me: BoolProperty(default=False)
    use_mimic: BoolProperty(default=False)
    anim_final_pos: StringProperty(default="")
    object_name: StringProperty(default="")
    cutscene_guid: StringProperty(default="")
    is_loaded: BoolProperty(default=False)
    imported_by_cutscene: BoolProperty(default=False)

class CutsceneLoadedAnimationItem(PropertyGroup):
    source_index: IntProperty(default=-1)
    file_backed: BoolProperty(default=False)
    full_name: StringProperty(default="")
    display_name: StringProperty(default="")
    actor_name: StringProperty(default="")
    component_name: StringProperty(default="")
    frames_per_second: FloatProperty(default=0.0)
    num_frames: IntProperty(default=0)
    duration: FloatProperty(default=0.0)
    is_loaded: BoolProperty(default=False)
    muted: BoolProperty(default=False)
    track_muted: BoolProperty(default=False)
    has_prebake: BoolProperty(default=False)


class CutsceneEventItem(PropertyGroup):
    event_type: StringProperty(default="")       # e.g. "CExtAnimCutsceneDialogEvent"
    event_name: StringProperty(name="Event Name", default="")
    start_time: FloatProperty(name="Start Time", default=0.0)
    duration: FloatProperty(name="Duration", default=0.0)
    animation_name: StringProperty(name="Animation", default="")
    track_name: StringProperty(name="Track", default="")
    effect_name: StringProperty(name="Effect", default="")
    appearance: StringProperty(name="Appearance", default="")
    always_fires_end: BoolProperty(name="Always Fires End", default=False)
    event_scope: StringProperty(default="ROOT")
    source_index: IntProperty(default=-1)

class CutsceneEffectItem(PropertyGroup):
    name: StringProperty(default="")

class CutsceneValidationIssue(PropertyGroup):
    severity: StringProperty(default="WARN")
    message: StringProperty(default="")
    tab: StringProperty(default="")
    object_name: StringProperty(default="")
    frame: IntProperty(default=-1)
    line: IntProperty(default=-1)

class CutsceneTemplateFieldItem(PropertyGroup):
    class_name: StringProperty(default="")
    field_name: StringProperty(default="")
    type_text: StringProperty(default="")
    value_text: StringProperty(default="")
    is_set: BoolProperty(default=False)
    show_unset: BoolProperty(name="Show Unset", default=False)


def _cutscene_dialog_speaker_search(self, context, edit_text):
    scene = getattr(context, "scene", None) if context is not None else None
    return _cutscene_dialog_speaker_candidates(scene, edit_text)


def _authored_cutscene_dialog_scene(self, context):
    scene = getattr(self, "id_data", None)
    if scene is None or not hasattr(scene, "witcher_cutscene_dialog_lines"):
        scene = getattr(context, "scene", None) if context is not None else None
    return scene


def _update_authored_cutscene_dialog_line(self, context):
    scene = _authored_cutscene_dialog_scene(self, context)
    if scene is not None and hasattr(scene, "witcher_cutscene_dialog_lines"):
        sync_authored_cutscene_dialog_items(scene)
        _push_authored_cutscene_dialog_to_lipsync(scene, self)


def _update_authored_cutscene_dialog_start(self, context):
    scene = _authored_cutscene_dialog_scene(self, context)
    if scene is not None:
        # Drop the preview at its old start.
        _remove_authored_cutscene_dialog_preview(scene, _authored_cutscene_dialog_line_id(self))
    _update_authored_cutscene_dialog_line(self, context)


def _update_authored_cutscene_dialog_tier(self, context):
    scene = _authored_cutscene_dialog_scene(self, context)
    if scene is not None:
        tier = str(getattr(self, "tier", "SUBTITLE") or "SUBTITLE")
        if tier != 'GAME':
            _remove_authored_cutscene_dialog_preview(scene, getattr(self, "game_line_id", ""))
        if tier != 'WAV':
            _remove_authored_cutscene_dialog_preview(
                scene,
                getattr(self, "lipsync_ref", "") or getattr(self, "allocated_line_id", ""),
            )
    _update_authored_cutscene_dialog_line(self, context)


class CutsceneDialogItem(PropertyGroup):
    actor: StringProperty(name="voicetag", default="")
    voice_file: StringProperty(name="voiceFileName", default="")
    sound_event: StringProperty(name="soundEventName", default="")
    line_id: StringProperty(name="dialogLine", default="")
    line_index: IntProperty(name="dialogLine Int32", default=0)
    line_text: StringProperty(name="LocalizedString.text", default="")
    scene_path: StringProperty(name="source .w2scene", default="")
    source_game: StringProperty(name="source game", default="")
    start_frame: IntProperty(name="computed start frame", default=0)
    end_frame: IntProperty(name="computed end frame", default=0)
    imported_sound: BoolProperty(name="imported audio strip", default=False)
    has_explicit_duration: BoolProperty(default=False, options={'HIDDEN'})


class CutsceneAuthoredDialogLine(PropertyGroup):
    speaker: StringProperty(
        name="Speaker",
        default="",
        description="Character voice tag written to this line; choose a cast actor or type a custom tag",
        search=_cutscene_dialog_speaker_search,
        update=_update_authored_cutscene_dialog_line,
    )
    text: StringProperty(
        name="Text",
        default="",
        description="Subtitle text shown in Blender and associated with the exported line ID",
        update=_update_authored_cutscene_dialog_line,
    )
    start_frame: IntProperty(
        name="Start",
        default=0,
        description="First timeline frame on which this line and subtitle are active; reload the preview after moving it",
        update=_update_authored_cutscene_dialog_start,
    )
    end_frame: IntProperty(
        name="End",
        default=1,
        description="First frame after this line; exported duration is End minus Start",
        update=_update_authored_cutscene_dialog_line,
    )
    tier: EnumProperty(
        name="Voice",
        description="Choose text only, a reusable vanilla game line, or a custom WAV workflow",
        items=[
            ('SUBTITLE', "Subtitle only", "Text-only line", 'FONT_DATA', 0),
            ('GAME', "Game line", "Reuse a Witcher 3 voice line", 'SOUND', 1),
            ('WAV', "Custom WAV", "Prepare custom voice assets in the Lipsync editor", 'FILE', 2),
        ],
        default='SUBTITLE',
        update=_update_authored_cutscene_dialog_tier,
    )
    game_line_id: StringProperty(
        name="Game Line",
        default="",
        description="Numeric vanilla dialogLine string ID written to the companion scene",
        update=_update_authored_cutscene_dialog_line,
    )
    game_voice_file_name: StringProperty(
        name="voiceFileName",
        default="",
        description="Vanilla voiceFileName used to load the matching audio and lipsync",
        update=_update_authored_cutscene_dialog_line,
    )
    wav_path: StringProperty(
        name="WAV",
        default="",
        description="Custom WAV handed to the Lipsync editor for this line",
        subtype='FILE_PATH',
    )
    allocated_line_id: StringProperty(
        name="Allocated Line",
        default="",
        description="Project or fallback string ID used by Subtitle and Custom WAV lines",
        update=_update_authored_cutscene_dialog_line,
    )
    lipsync_ref: StringProperty(
        name="Lipsync Line",
        default="",
        description="Line ID linking this row to its Lipsync editor entry",
    )

def _set_cutscene_burned_audio_scene_state(scene, event_name="", item_path=""):
    if hasattr(scene, "witcher_cutscene_burned_audio_event"):
        scene.witcher_cutscene_burned_audio_event = str(event_name or "")
    if hasattr(scene, "witcher_cutscene_burned_audio_item_path"):
        scene.witcher_cutscene_burned_audio_item_path = str(item_path or "")


def _join_cutscene_metadata_list(values):
    items = []
    for value in values or []:
        item_text = str(value or "").strip()
        if item_text:
            items.append(item_text)
    return "; ".join(items)


def _cutscene_fixed_row(layout, ui_units_x):
    row = layout.row(align=True)
    row.ui_units_x = max(1.0, float(ui_units_x or 1.0))
    return row


def _cutscene_label_units(text, icon=False, minimum=3.0, maximum=12.0):
    base = 2.0 if icon else 1.2
    units = base + (len(str(text or "")) * 0.45)
    return max(float(minimum), min(float(maximum), units))


def _cutscene_fixed_label(
    layout,
    text="",
    icon=None,
    units=None,
    minimum=3.0,
    maximum=12.0,
    enabled=True,
    alignment=None,
):
    if units is None:
        units = _cutscene_label_units(text, icon=bool(icon), minimum=minimum, maximum=maximum)
    row = _cutscene_fixed_row(layout, units)
    row.enabled = enabled
    if alignment:
        row.alignment = alignment
    if icon:
        row.label(text=text, icon=icon)
    else:
        row.label(text=text)
    return row


def _set_cutscene_export_metadata_scene_state(scene, point_tags=(), last_level_loaded="", used_in_files=(), synced=False):
    if hasattr(scene, export_cutscene.CUTSCENE_POINT_TAGS_PROP):
        scene.witcher_cutscene_point_tags = _join_cutscene_metadata_list(point_tags)
    if hasattr(scene, export_cutscene.CUTSCENE_LAST_LEVEL_LOADED_PROP):
        scene.witcher_cutscene_last_level_loaded = str(last_level_loaded or "")
    if hasattr(scene, export_cutscene.CUTSCENE_USED_IN_FILES_PROP):
        scene.witcher_cutscene_used_in_files = _join_cutscene_metadata_list(used_in_files)
    if hasattr(scene, export_cutscene.CUTSCENE_EXPORT_METADATA_SYNCED_PROP):
        scene.witcher_cutscene_export_metadata_synced = bool(synced)


def _sync_cutscene_export_metadata_state(scene, cutscene):
    if cutscene is None:
        _set_cutscene_export_metadata_scene_state(scene, synced=False)
        return

    point_tags = getattr(cutscene, "point", None) or []
    last_level_loaded = getattr(cutscene, "lastLevelLoaded", "")
    used_in_files = getattr(cutscene, "usedInFiles", None) or []
    _set_cutscene_export_metadata_scene_state(
        scene,
        point_tags=point_tags,
        last_level_loaded=last_level_loaded,
        used_in_files=used_in_files,
        synced=True,
    )

def _sync_cutscene_template_fields(scene, cutscene):
    scene.witcher_cutscene_template_fields.clear()
    if cutscene is None:
        return

    schema = _get_imported_field_schema(cutscene, fallback_schema=w3_types.CUTSCENE_CLASS_FIELD_SCHEMA)
    present_fields = _get_present_imported_fields(cutscene)
    for class_name, fields in schema:
        for field_name, default_value in fields:
            item = scene.witcher_cutscene_template_fields.add()
            item.class_name = class_name
            item.field_name = field_name
            item.type_text = _get_imported_field_type(default_value)
            item.is_set = field_name in present_fields
            if item.is_set:
                value = _get_imported_field_value(cutscene, field_name)
                item.type_text = _get_imported_field_type(value)
                item.value_text = _format_imported_field_value(value)
            else:
                item.value_text = "<unset>"


def _sync_cutscene_burned_audio_state(scene, filepath, cutscene, cutscene_data=None):
    event_name = ""
    item_path = ""
    if cutscene is not None:
        has_burned_audio_prop, burned_audio_name = import_cutscene.get_cutscene_burned_audio_property(cutscene)
        if has_burned_audio_prop:
            event_name = str(burned_audio_name or "").strip()

    burned_audio_info = dict(getattr(cutscene_data, "burned_audio_info", {}) or {})
    item_path = str(burned_audio_info.get("item_path", "") or "").strip()
    if event_name and not item_path:
        try:
            resolved_audio = import_cutscene.resolve_cutscene_burned_audio_item(cutscene, filename=filepath)
        except Exception:
            resolved_audio = None
        if resolved_audio:
            item_path = str(resolved_audio.get("item_path", "") or "").strip()

    _set_cutscene_burned_audio_scene_state(scene, event_name=event_name, item_path=item_path)

class WITCH_UL_CutsceneActorPreview(UIList):
    bl_idname = "WITCH_UL_CutsceneActorPreview"
    layout_type = "DEFAULT"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index, flt_flag):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            _cutscene_fixed_row(row, 1.4).prop(item, "selected", text="")
            row.label(text=item.label or item.actor_name or "Actor", icon='ARMATURE_DATA')
            if item.already_in_scene:
                _cutscene_fixed_label(row, "IN SCENE", icon='CHECKMARK', units=6.8)
            if item.appearance_name:
                _cutscene_fixed_label(
                    row,
                    item.appearance_name,
                    icon='MATERIAL_DATA',
                    minimum=7.0,
                    maximum=12.0,
                )
        elif self.layout_type in {'GRID'}:
            layout.alignment = 'CENTER'
            layout.label(text="")


class WITCH_UL_CastActorCandidates(UIList):
    bl_idname = "WITCH_UL_CastActorCandidates"
    layout_type = "DEFAULT"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index, flt_flag):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            row.label(text="", icon='CHECKMARK' if item.indexed else 'FILE')
            row.label(text=item.label or "Actor")
        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text="", icon='ARMATURE_DATA')


class WITCH_UL_CutsceneBrowseAnimations(UIList):
    bl_idname = "WITCH_UL_CutsceneBrowseAnimations"
    layout_type = "DEFAULT"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index, flt_flag):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            row.label(text="", icon='CHECKMARK' if item.compatible else 'INFO')
            label = item.caption or item.animation_id or "Animation"
            if item.frames:
                label = f"{label} · {item.duration:.2f}s"
            row.label(text=label)
        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text="", icon='ACTION')


class WITCH_UL_CutsceneDialogVoiceResults(UIList):
    bl_idname = "WITCH_UL_CutsceneDialogVoiceResults"
    layout_type = "DEFAULT"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index, flt_flag):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            row.label(text="", icon='SOUND')
            label = item.display or f"[{item.speaker}] {item.text}" or item.voice_line_id
            if item.duration:
                label = f"{label} · {item.duration}s"
            row.label(text=label, translate=False)
        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text="", icon='SOUND')


class WITCH_UL_CutsceneSpeechPick(UIList):
    bl_idname = "WITCH_UL_CutsceneSpeechPick"
    layout_type = "DEFAULT"
    use_filter_show = True

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index, flt_flag):
        from ..lipsync import ui_lipsync

        row = layout.row(align=True)
        row.prop(item, "selected", text="")
        linked = str(getattr(item, "line_id", "") or "").strip() in _cutscene_dialog_lipsync_refs(context.scene)
        row.label(text="", icon='LINKED' if linked else 'BLANK1')
        row.label(text=ui_lipsync._line_display_name(item), translate=False)


class WITCH_UL_CutsceneAnimationPreview(UIList):
    bl_idname = "WITCH_UL_CutsceneAnimationPreview"
    layout_type = "DEFAULT"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index, flt_flag):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            _cutscene_fixed_row(row, 1.4).prop(item, "selected", text="")
            row.label(text=_get_cutscene_animation_label(item), icon='ACTION')
            if item.component_name:
                _cutscene_fixed_label(
                    row,
                    item.component_name,
                    icon='BONE_DATA',
                    minimum=4.4,
                    maximum=8.0,
                )
        elif self.layout_type in {'GRID'}:
            layout.alignment = 'CENTER'
            layout.label(text="")


def _event_type_icon(event_type):
    if 'BodyPart' in event_type or 'Appearance' in event_type:
        return 'MATERIAL_DATA'
    if 'Dialog' in event_type or 'Lookat' in event_type or 'LookAt' in event_type:
        return 'OUTLINER_OB_SPEAKER'
    if 'Effect' in event_type or 'Fx' in event_type:
        return 'SHADERFX'
    if 'Sound' in event_type:
        return 'SOUND'
    if 'Fade' in event_type:
        return 'IMAGE_ALPHA'
    if 'Slow' in event_type or 'Wind' in event_type or 'Environment' in event_type:
        return 'WORLD'
    if 'Break' in event_type:
        return 'CANCEL'
    return 'KEYFRAME'


def _get_cutscene_event_label(item):
    event_type = str(getattr(item, "event_type", "") or "")
    appearance = str(getattr(item, "appearance", "") or "").strip()
    if appearance and "BodyPartEvent" in event_type:
        return appearance

    effect_name = str(getattr(item, "effect_name", "") or "").strip()
    if effect_name and ("Effect" in event_type or "Fx" in event_type):
        return effect_name

    event_name = str(getattr(item, "event_name", "") or "").strip()
    return event_name or event_type or "Event"


# ── Event class schema ─────────────────────────────────────────────────────
# (class_name, base_class, scope, own_props)
# scope: "ROOT" = cutscene-level (CCutsceneTemplate.animevents)
#        "ENTRY" = animation-level (CSkeletalAnimationSetEntry.entries)
# own_props: fields added by this class beyond its base (name, type pairs)
_CUTSCENE_EVENT_SCHEMA = [
    # ── ROOT: cutscene-level events ─────────────────────────────────────────
    ("CExtAnimCutsceneFadeEvent",             "CExtAnimEvent",                 "ROOT",
        [("in","Bool"), ("duration","Float"), ("color","Color")]),
    ("CExtAnimCutsceneSlowMoEvent",           "CExtAnimCutsceneDurationEvent", "ROOT",
        [("enabled","Bool"), ("factor","Float"), ("useWeightCurve","Bool"), ("weightCurve","SCurveData")]),
    ("CExtAnimCutsceneWindEvent",             "CExtAnimCutsceneDurationEvent", "ROOT",
        [("enabled","Bool"), ("factor","Float"), ("useWeightCurve","Bool"), ("weightCurve","SCurveData")]),
    ("CExtAnimCutsceneEnvironmentEvent",      "CExtAnimEvent",                 "ROOT",
        [("environmentName","String"), ("environmentActivate","Bool"),
         ("stabilizeBlending","Bool"), ("instantEyeAdaptation","Bool"),
         ("instantDissolve","Bool"), ("forceNoOtherEnvironments","Bool"),
         ("forceSetupLocalEnvironments","Bool"), ("forceSetupGlobalEnvironments","Bool")]),
    ("CExtAnimCutsceneLightEvent",            "CExtAnimEvent",                 "ROOT",
        [("tag","TagList"), ("isEnabled","Bool"), ("radius","Float"), ("brightness","Float"), ("color","Color")]),
    ("CExtAnimCutsceneSoundEvent",            "CExtAnimEvent",                 "ROOT",
        [("soundEventName","StringAnsi"), ("bone","CName"), ("useMaterialInfo","Bool")]),
    ("CExtAnimCutsceneEffectEvent",           "CExtAnimDurationEvent",         "ROOT",
        [("effect","CName"), ("tag","TagList"), ("template","soft:CEntityTemplate"),
         ("spawnPosMS","Vector"), ("spawnRotMS","EulerAngles")]),
    ("CExtAnimCutsceneActorEffect",           "CExtAnimDurationEvent",         "ROOT",
        [("effectName","CName")]),
    ("CExtAnimCutsceneHideEntityEvent",       "CExtAnimCutsceneEvent",         "ROOT",
        [("entTohideTag","CName")]),
    ("CExtAnimCutsceneHideTerrainEvent",      "CExtAnimCutsceneDurationEvent", "ROOT", []),
    ("CExtAnimCutsceneSurfaceEffect",         "CExtAnimCutsceneEvent",         "ROOT",
        [("type","ESceneEventSurfacePostFXType"), ("worldPos","Bool"), ("position","Vector"),
         ("radius","Float"), ("fadeInTime","Float"), ("fadeOutTime","Float"), ("durationTime","Float")]),
    ("CExtAnimCutsceneDisableClothEvent",     "CExtAnimEvent",                 "ROOT",
        [("weight","Float"), ("blendTime","Float")]),
    ("CExtAnimCutsceneDisableDangleEvent",    "CExtAnimEvent",                 "ROOT",
        [("weight","Float")]),
    ("CExtAnimCutsceneResetClothAndDangleEvent","CExtAnimEvent",               "ROOT",
        [("forceRelaxedState","Bool")]),
    ("CExtAnimCutsceneSetClippingPlanesEvent","CExtAnimEvent",                 "ROOT",
        [("nearPlaneDistance","ENearPlaneDistance"), ("farPlaneDistance","EFarPlaneDistance")]),
    ("CExtAnimCutsceneBokehDofEvent",         "CExtAnimEvent",                 "ROOT",
        [("bokehDofParams","SBokehDofParams")]),
    ("CExtAnimCutsceneBokehDofBlendEvent",    "CExtAnimDurationEvent",         "ROOT",
        [("bokehDofParamsStart","SBokehDofParams"), ("bokehDofParamsEnd","SBokehDofParams")]),
    ("CExtAnimCutsceneQuestEvent",            "CExtAnimEvent",                 "ROOT",
        [("cutsceneName","String")]),
    ("CExtAnimCutsceneBreakEvent",            "CExtAnimEvent",                 "ROOT", []),
    # ── ENTRY: animation-level events ───────────────────────────────────────
    ("CExtAnimCutsceneBodyPartEvent",         "CExtAnimEvent",                 "ENTRY",
        [("appearance","CName")]),
    ("CExtAnimCutsceneDialogEvent",           "CExtAnimEvent",                 "ENTRY", []),
    ("CExtAnimDialogKeyPoseMarker",           "CExtAnimEvent",                 "ENTRY", []),
    ("CExtAnimDialogKeyPoseDuration",         "CExtAnimDurationEvent",         "ENTRY",
        [("transition","Bool"), ("keyPose","Bool")]),
    ("CExtAnimDisableDialogLookatEvent",      "CExtAnimDurationEvent",         "ENTRY",
        [("speed","Float")]),
    ("CExtAnimScriptEvent",                   "CExtAnimEvent",                 "ENTRY", []),
    ("CExtAnimScriptDurationEvent",           "CExtAnimDurationEvent",         "ENTRY", []),
    ("CExtAnimRaiseEventEvent",               "CExtAnimEvent",                 "ENTRY",
        [("eventToBeRaisedName","CName"), ("forceRaiseEvent","Bool")]),
    ("CExtAnimEffectEvent",                   "CExtAnimEvent",                 "ENTRY",
        [("effectName","CName"), ("action","EAnimEffectAction")]),
    ("CExtAnimEffectDurationEvent",           "CExtAnimDurationEvent",         "ENTRY",
        [("effectName","CName")]),
    ("CExtAnimMorphEvent",                    "CExtAnimDurationEvent",         "ENTRY",
        [("morphComponentId","CName"), ("invertWeight","Bool"), ("useCurve","Bool")]),
    ("CExtAnimItemEvent",                     "CExtAnimEvent",                 "ENTRY",
        [("category","CName"), ("itemName_optional","CName"), ("action","EItemAction"), ("itemGetting","EGettingItem")]),
    ("CExtAnimItemEffectEvent",               "CExtAnimEvent",                 "ENTRY",
        [("effectName","CName"), ("itemSlot","CName"), ("action","EItemEffectAction")]),
    ("CExtAnimItemEffectDurationEvent",       "CExtAnimDurationEvent",         "ENTRY",
        [("effectName","CName"), ("itemSlot","CName")]),
    ("CExtAnimItemSyncEvent",                 "CExtAnimEvent",                 "ENTRY",
        [("equipSlot","CName"), ("holdSlot","CName"), ("action","EItemLatentAction")]),
    ("CExtAnimItemSyncDurationEvent",         "CExtAnimDurationEvent",         "ENTRY",
        [("equipSlot","CName"), ("holdSlot","CName"), ("action","EItemLatentAction")]),
    ("CExtAnimItemSyncWithCorrectionEvent",   "CExtAnimDurationEvent",         "ENTRY",
        [("equipSlot","CName"), ("holdSlot","CName"), ("action","EItemLatentAction"), ("correctionBone","CName")]),
    ("CExtAnimItemAnimationEvent",            "CExtAnimEvent",                 "ENTRY",
        [("itemCategory","CName"), ("itemAnimationName","CName")]),
    ("CExtAnimItemBehaviorEvent",             "CExtAnimEvent",                 "ENTRY",
        [("itemCategory","CName"), ("event","CName")]),
    ("CExtAnimDropItemEvent",                 "CExtAnimEvent",                 "ENTRY",
        [("action","EDropAction")]),
    ("CExtAnimReattachItemEvent",             "CExtAnimDurationEvent",         "ENTRY",
        [("item","CName"), ("targetSlot","CName")]),
    ("CExtAnimAttackEvent",                   "CExtAnimEvent",                 "ENTRY",
        [("soundAttackType","CName")]),
    ("CExtAnimHitEvent",                      "CExtAnimEvent",                 "ENTRY",
        [("hitLevel","Uint32")]),
    ("CExtAnimSoundEvent",                    "CExtAnimEvent",                 "ENTRY",
        [("soundEventName","StringAnsi"), ("maxDistance","Float"), ("bone","CName"),
         ("filter","Bool"), ("filterCooldown","Float"), ("useDistanceParameter","Bool")]),
    ("CExtAnimFootstepEvent",                 "CExtAnimSoundEvent",            "ENTRY",
        [("fx","Bool"), ("customFxName","CName")]),
    ("CExtAnimLookAtEvent",                   "CExtAnimDurationEvent",         "ENTRY",
        [("level","ELookAtLevel")]),
    ("CExtAnimMaterialBasedFxEvent",          "CExtAnimEvent",                 "ENTRY",
        [("bone","CName"), ("vfxKickup","Bool"), ("vfxFootstep","Bool")]),
    ("CExtAnimProjectileEvent",               "CExtAnimEvent",                 "ENTRY",
        [("castPosition","EProjectileCastPosition"), ("boneName","CName")]),
    ("CExtAnimGameplayMimicEvent",            "CExtAnimDurationEvent",         "ENTRY",
        [("animation","CName")]),
    ("CExtAnimOnSlopeEvent",                  "CExtAnimDurationEvent",         "ENTRY",
        [("slopeAngle","Float")]),
    ("CExtAnimExplorationEvent",              "CExtAnimDurationEvent",         "ENTRY", []),
    ("CExtAnimComboEvent",                    "CExtAnimDurationEvent",         "ENTRY", []),
    ("CExtAnimLocationAdjustmentEvent",       "CExtAnimDurationEvent",         "ENTRY",
        [("locationAdjustmentVar","CName"), ("adjustmentActiveVar","CName")]),
    ("CExtAnimRotationAdjustmentEvent",       "CExtAnimDurationEvent",         "ENTRY",
        [("rotationAdjustmentVar","CName")]),
    ("CExtAnimRotationAdjustmentLocationBasedEvent","CExtAnimDurationEvent",   "ENTRY",
        [("locationAdjustmentVar","CName"), ("targetLocationVar","CName"), ("adjustmentActiveVar","CName")]),
]

_EVENT_SCHEMA_BY_CLASS = {s[0]: s for s in _CUTSCENE_EVENT_SCHEMA}

# EnumProperty items lists (module-level so Blender can cache them)
_ANIM_EVENT_ENUM_ITEMS_ROOT  = [(c, c, f"↑ {b}") for c,b,s,_ in _CUTSCENE_EVENT_SCHEMA if s=="ROOT"]
_ANIM_EVENT_ENUM_ITEMS_ENTRY = [(c, c, f"↑ {b}") for c,b,s,_ in _CUTSCENE_EVENT_SCHEMA if s=="ENTRY"]
_ANIM_EVENT_ENUM_ITEMS_ALL   = _ANIM_EVENT_ENUM_ITEMS_ROOT + _ANIM_EVENT_ENUM_ITEMS_ENTRY


def _event_schema_has_duration(class_name):
    entry = _EVENT_SCHEMA_BY_CLASS.get(class_name)
    if not entry:
        return "Duration" in class_name
    base = entry[1]
    return "Duration" in class_name or "Duration" in base


class WITCH_UL_CutsceneDialogList(UIList):
    bl_idname = "WITCH_UL_CutsceneDialogList"
    layout_type = "DEFAULT"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index, flt_flag):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            actor_split = row.split(factor=0.16, align=True)
            actor_split.label(text=item.actor or "?", icon='OUTLINER_OB_SPEAKER')

            text_split = actor_split.split(factor=0.86, align=True)
            text_split.label(text=item.line_text or "<text not resolved>", icon='FONT_DATA', translate=False)

            frame_row = text_split.row(align=True)
            frame_row.alignment = 'RIGHT'
            if item.start_frame or item.end_frame:
                frame_row.label(text=f"{item.start_frame}-{item.end_frame}")
            if item.imported_sound:
                frame_row.label(text="", icon='SOUND')
        elif self.layout_type in {'GRID'}:
            layout.alignment = 'CENTER'
            layout.label(text="")


class WITCH_UL_CutsceneAuthoredDialogList(UIList):
    bl_idname = "WITCH_UL_CutsceneAuthoredDialogList"
    layout_type = "DEFAULT"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index, flt_flag):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            tier_icon = {'GAME': 'SOUND', 'WAV': 'FILE'}.get(str(item.tier), 'FONT_DATA')
            row.label(text="", icon=tier_icon)
            row.label(text=f"{item.speaker or '?'} · {item.text or '<empty>'}", translate=False)
        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text="")


class WITCH_UL_LoadedActorList(UIList):
    bl_idname = "WITCH_UL_LoadedActorList"
    layout_type = "DEFAULT"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index, flt_flag):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            state = _get_cutscene_actor_display_state(item)
            _cutscene_fixed_label(row, "", icon='CHECKMARK' if state["is_loaded"] else 'RADIOBUT_OFF', units=1.4)
            label = item.label or item.actor_name or f"Actor {item.source_index + 1}"
            row.label(text=label)
        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text="")


class WITCH_UL_LoadedAnimList(UIList):
    bl_idname = "WITCH_UL_LoadedAnimList"
    layout_type = "DEFAULT"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index, flt_flag):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            icon = 'CHECKMARK' if item.is_loaded and not item.muted else ('HIDE_ON' if item.is_loaded else 'RADIOBUT_OFF')
            _cutscene_fixed_label(row, "", icon=icon, units=1.4)
            row.label(text=_get_cutscene_animation_label(item))
        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text="")


class WITCH_UL_CutsceneValidationIssues(UIList):
    bl_idname = "WITCH_UL_CutsceneValidationIssues"
    layout_type = "DEFAULT"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index, flt_flag):
        if self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text="")
            return
        row = layout.row(align=True)
        row.label(text=item.message, icon='ERROR' if item.severity == "ERROR" else 'INFO')
        details = row.operator("witcher.cutscene_exact_value_details", text="", icon='COPYDOWN', emboss=False)
        details.field_label = "Issue"
        details.value = item.message
        target = row.row(align=True)
        target.enabled = bool(item.tab or item.object_name or item.frame >= 0 or item.line >= 0)
        go = target.operator("witcher.cutscene_validation_goto", text="", icon='FORWARD', emboss=False)
        go.tab, go.object_name, go.frame, go.line = item.tab, item.object_name, item.frame, item.line


def _find_actor_obj_by_voicetag(scene, voicetag):
    tag_lower = str(voicetag or "").lower().strip()
    if not tag_lower:
        return None
    for actor_item in getattr(scene, "witcher_cutscene_actor_items", []):
        # Primary: match against the stored voiceTag
        if str(getattr(actor_item, "voice_tag", "") or "").lower().strip() == tag_lower:
            return _get_loaded_cutscene_actor_object(actor_item)
        # Fallback: actor_name (cutscene slot name) often equals voiceTag case-insensitively
        if str(getattr(actor_item, "actor_name", "") or "").lower().strip() == tag_lower:
            return _get_loaded_cutscene_actor_object(actor_item)
    # Try registry aliases such as CIRI/CIRILLA.
    from . import ui_voice

    aliases = ui_voice.voice_speaker_aliases(tag_lower)
    for actor_item in getattr(scene, "witcher_cutscene_actor_items", []):
        for actor_tag in (getattr(actor_item, "voice_tag", ""), getattr(actor_item, "actor_name", "")):
            if actor_tag and aliases & ui_voice.voice_speaker_aliases(actor_tag):
                return _get_loaded_cutscene_actor_object(actor_item)
    return None


def _dialog_default_duration_frames(line_text, fps):
    text_len = len(str(line_text or "").strip())
    if text_len <= 0:
        return int(round(max(1.0, fps * 2.5)))
    seconds = max(1.5, min(8.0, text_len / 14.0))
    return int(round(max(1.0, seconds * fps)))


def _cutscene_dialog_actor_speaker(actor):
    actor_type = str(getattr(actor, "actor_type", "") or "")
    if actor_type and actor_type != "CAT_Actor":
        return ""
    return str(getattr(actor, "voice_tag", "") or getattr(actor, "actor_name", "") or "").strip()


def _cutscene_dialog_speaker_candidates(scene, query=""):
    query = str(query or "").strip().lower()
    speakers = []
    seen = set()
    for actor in getattr(scene, "witcher_cutscene_actor_items", []) if scene is not None else []:
        speaker = _cutscene_dialog_actor_speaker(actor)
        key = speaker.lower()
        if speaker and key not in seen and (not query or query in key):
            speakers.append(speaker)
            seen.add(key)
    return speakers


def _active_cutscene_dialog_speaker(scene):
    actors = list(getattr(scene, "witcher_cutscene_actor_items", []) or [])
    index = int(getattr(scene, "witcher_cutscene_loaded_actor_index", 0) or 0)
    if 0 <= index < len(actors):
        speaker = _cutscene_dialog_actor_speaker(actors[index])
        if speaker:
            return speaker
    candidates = _cutscene_dialog_speaker_candidates(scene)
    return candidates[0] if candidates else ""


def _authored_cutscene_dialog_line_id(line):
    if str(getattr(line, "tier", "SUBTITLE") or "SUBTITLE") == 'GAME':
        return str(getattr(line, "game_line_id", "") or "").strip()
    return str(getattr(line, "allocated_line_id", "") or "").strip()


def _cutscene_dialog_wav_line_id(context, line_index):
    scene = context.scene
    line = scene.witcher_cutscene_dialog_lines[line_index]
    raw_id = str(getattr(line, "allocated_line_id", "") or "").strip()
    if raw_id and not raw_id.isdigit():
        raise RuntimeError("The allocated line ID must be numeric.")

    from ..lipsync import redkit_project

    used_ids = {
        int(value)
        for index, other in enumerate(scene.witcher_cutscene_dialog_lines)
        if index != line_index
        and str(getattr(other, "tier", "SUBTITLE") or "SUBTITLE") != 'GAME'
        and (value := str(getattr(other, "allocated_line_id", "") or "").strip()).isdigit()
    }
    if raw_id and int(raw_id) in used_ids:
        raise RuntimeError(f"Dialogue line ID {raw_id} is already in use")
    project_path = redkit_project.get_active_project_path(context)
    if project_path:
        if raw_id:
            if int(raw_id) > redkit_project.MAX_RADISH_LINE_ID:
                raise RuntimeError(f"Line ID must be {redkit_project.MAX_RADISH_LINE_ID} or lower.")
            return raw_id
        id_info = redkit_project.next_project_line_id(project_path)
        if id_info is None:
            raise RuntimeError(f"REDkit project has no usable idSpace metadata: {project_path}")
        candidate = int(id_info.next_line_id)
        while candidate in used_ids and candidate <= redkit_project.MAX_RADISH_LINE_ID:
            candidate += 1
        if candidate > redkit_project.MAX_RADISH_LINE_ID:
            raise RuntimeError("The REDkit project line-ID range is full.")
        return str(candidate)

    id_space, first_id, last_id = export_cutscene._cutscene_dialog_id_space_bounds(
        getattr(scene, "witcher_cutscene_dialog_id_space", -1)
    )
    if raw_id:
        if not first_id <= int(raw_id) <= last_id:
            raise RuntimeError(f"Line ID must be in {first_id}-{last_id} for id-space {id_space}.")
        return raw_id
    candidate = first_id
    while candidate in used_ids and candidate <= last_id:
        candidate += 1
    if candidate > last_id:
        raise RuntimeError(f"Dialogue id-space {id_space} is full ({first_id}-{last_id}).")
    return str(candidate)


def adopt_cutscene_dialog_lipsync_line(scene, lipsync_line, previous_line_id=""):
    line_id = str(getattr(lipsync_line, "line_id", "") or "").strip()
    if not line_id:
        return 0
    references = {line_id, str(previous_line_id or "").strip()}
    references.discard("")
    wav_path = str(getattr(lipsync_line, "wav_path", "") or "").strip()
    strip_name = str(getattr(lipsync_line, "strip_name", "") or "").strip()
    adopted = 0
    text = str(getattr(lipsync_line, "text", "") or "")
    for line in getattr(scene, "witcher_cutscene_dialog_lines", []) or []:
        if str(getattr(line, "tier", "SUBTITLE") or "SUBTITLE") == 'GAME':
            continue
        if str(getattr(line, "lipsync_ref", "") or "").strip() not in references:
            continue
        line.lipsync_ref = line_id
        line.allocated_line_id = line_id
        if wav_path:
            line.wav_path = wav_path
        if text and line.text != text:
            line.text = text
        speaker = _cutscene_dialog_speaker_for_voice(scene, line.speaker, getattr(lipsync_line, "speaker", ""))
        if speaker != line.speaker:
            line.speaker = speaker
        adopted += 1

        strips = getattr(getattr(scene, "sequence_editor", None), "sequences_all", None)
        if strips is None:
            strips = getattr(getattr(scene, "sequence_editor", None), "strips", None)
        for strip in list(strips or []):
            strip_id = str(strip.get(import_cutscene.CUTSCENE_DIALOG_LINE_ID_PROP, "") or "").strip()
            if getattr(strip, "type", None) != 'SOUND' or not (strip_name == strip.name or strip_id in references):
                continue
            strip.frame_start = float(getattr(line, "start_frame", 0) or 0)
            for prop_name, value in (
                (import_cutscene.CUTSCENE_DIALOG_AUDIO_PROP, True),
                (import_cutscene.CUTSCENE_DIALOG_LINE_ID_PROP, line_id),
                (import_cutscene.CUTSCENE_DIALOG_TEXT_PROP, str(getattr(line, "text", "") or "")),
                (import_cutscene.CUTSCENE_DIALOG_SOURCE_PATH_PROP, _AUTHORED_DIALOG_SOURCE_PATH),
                (dialog_language.DIALOG_SUBTITLE_LINE_ID_PROP, line_id),
                (dialog_language.DIALOG_SUBTITLE_TEXT_PROP, str(getattr(line, "text", "") or "")),
                (dialog_language.DIALOG_SUBTITLE_SPEAKER_PROP, str(getattr(line, "speaker", "") or "")),
            ):
                strip[prop_name] = value
    if adopted:
        sync_authored_cutscene_dialog_items(scene)
    return adopted


def _cutscene_dialog_tts_argv(command, text, output_path):
    command = str(command or "").strip()
    missing = [placeholder for placeholder in ("{text}", "{out}") if placeholder not in command]
    if missing:
        raise RuntimeError(f"TTS command is missing {' and '.join(missing)}")
    try:
        argv = shlex.split(command, posix=os.name != 'nt')
    except ValueError as exc:
        raise RuntimeError(f"Invalid TTS command: {exc}") from exc
    if os.name == 'nt':
        argv = [
            arg[1:-1] if len(arg) >= 2 and arg[0] == arg[-1] and arg[0] in "\"'" else arg
            for arg in argv
        ]
    if not argv:
        raise RuntimeError("Configure TTS Command in the add-on preferences")
    return [arg.replace("{out}", output_path).replace("{text}", text) for arg in argv]


def _cutscene_dialog_tts_output_path(line, line_index):
    wav_path = str(getattr(line, "wav_path", "") or "").strip()
    if wav_path:
        output_path = bpy.path.abspath(wav_path)
    else:
        blend_path = str(getattr(bpy.data, "filepath", "") or "").strip()
        if not blend_path:
            raise RuntimeError("Save the blend or set a WAV path before generating audio")
        blend_stem = os.path.splitext(os.path.basename(blend_path))[0] or "cutscene"
        output_path = os.path.join(
            os.path.dirname(blend_path),
            f"{blend_stem}_dialogue_{line_index + 1:03d}.wav",
        )
    output_path = os.path.abspath(output_path)
    if not output_path.lower().endswith(".wav"):
        raise RuntimeError("The TTS output path must end in .wav")
    return output_path


def generate_cutscene_dialog_wav(context, line_index=None, run_command=subprocess.run):
    scene = context.scene
    lines = scene.witcher_cutscene_dialog_lines
    if line_index is None:
        line_index = int(getattr(scene, "witcher_cutscene_dialog_line_index", 0) or 0)
    line_index = int(line_index)
    if not 0 <= line_index < len(lines):
        raise RuntimeError("Dialogue line is unavailable")
    line = lines[line_index]
    if str(getattr(line, "tier", "SUBTITLE") or "SUBTITLE") != 'WAV':
        raise RuntimeError("Selected dialogue line is not a custom WAV line")
    text = str(getattr(line, "text", "") or "").strip()
    if not text:
        raise RuntimeError("Enter dialogue text before generating audio")
    if not str(getattr(line, "speaker", "") or "").strip():
        raise RuntimeError("Enter a speaker before generating audio")

    command = str(get_tts_command(context) or "").strip()
    if not command:
        raise RuntimeError("Configure TTS Command in the add-on preferences")
    output_path = _cutscene_dialog_tts_output_path(line, line_index)
    argv = _cutscene_dialog_tts_argv(command, text, output_path)
    output_dir = os.path.dirname(output_path)
    os.makedirs(output_dir, exist_ok=True)
    try:
        before = os.stat(output_path)
        before = (before.st_mtime_ns, before.st_size)
    except OSError:
        before = None

    try:
        completed = run_command(
            argv,
            cwd=output_dir,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("TTS command timed out after 300 seconds") from exc
    except OSError as exc:
        raise RuntimeError(f"TTS command could not start: {exc}") from exc
    if int(getattr(completed, "returncode", 0) or 0):
        detail = str(getattr(completed, "stderr", "") or getattr(completed, "stdout", "") or "").strip()
        detail = detail.splitlines()[-1][:300] if detail else "no command output"
        raise RuntimeError(f"TTS command failed ({completed.returncode}): {detail}")
    if not os.path.isfile(output_path):
        raise RuntimeError(f"TTS command did not create a WAV: {output_path}")
    after = os.stat(output_path)
    if before == (after.st_mtime_ns, after.st_size):
        raise RuntimeError(f"TTS command did not update the WAV: {output_path}")

    line = scene.witcher_cutscene_dialog_lines[line_index]
    line.wav_path = output_path
    editor_line, soundstrip = prepare_cutscene_dialog_wav_line(context, line_index=line_index)
    return output_path, editor_line, soundstrip, completed


def prepare_cutscene_dialog_wav_line(context, line_index=None):
    scene = context.scene
    lines = scene.witcher_cutscene_dialog_lines
    if line_index is None:
        line_index = int(getattr(scene, "witcher_cutscene_dialog_line_index", 0) or 0)
    line_index = int(line_index)
    if not 0 <= line_index < len(lines):
        raise RuntimeError("Dialogue line is unavailable")
    line = lines[line_index]
    if str(getattr(line, "tier", "SUBTITLE") or "SUBTITLE") != 'WAV':
        raise RuntimeError("Selected dialogue line is not a custom WAV line")
    if not str(getattr(line, "speaker", "") or "").strip():
        raise RuntimeError("Enter a speaker before preparing voice assets")
    if not str(getattr(line, "text", "") or "").strip():
        raise RuntimeError("Enter dialogue text before preparing voice assets")

    wav_path = str(getattr(line, "wav_path", "") or "").strip()
    resolved_wav = bpy.path.abspath(wav_path) if wav_path else ""
    if resolved_wav and (not resolved_wav.lower().endswith(".wav") or not os.path.isfile(resolved_wav)):
        raise RuntimeError(f"WAV file does not exist: {resolved_wav}")

    from ..lipsync import ui_lipsync
    from . import ui_voice

    line_id = _cutscene_dialog_wav_line_id(context, line_index)
    old_ref = str(getattr(line, "lipsync_ref", "") or "").strip()
    editor_line = ui_lipsync._find_editor_line_by_id(scene, old_ref or line_id)
    id_owner = ui_lipsync._find_editor_line_by_id(scene, line_id)
    if editor_line is not None and id_owner is not None and editor_line != id_owner:
        raise RuntimeError(f"Lipsync line ID {line_id} is already in use")
    created = editor_line is None
    if created:
        if id_owner is None:
            ui_lipsync._add_editor_line(context, line_id=line_id)
        editor_line = ui_lipsync._find_editor_line_by_id(scene, line_id)
    if editor_line is None:
        raise RuntimeError("Could not create a Lipsync editor line")

    if old_ref and old_ref != line_id:
        _remove_authored_cutscene_dialog_preview(scene, old_ref)
    editor_line.line_id = line_id
    editor_line.text = str(getattr(line, "text", "") or "")
    speaker = str(getattr(line, "speaker", "") or "")
    editor_speaker = str(editor_line.speaker or "").strip().upper()
    # Preserve an existing alias such as CIRI for CIRILLA.
    if created or (editor_speaker != speaker.upper() and editor_speaker not in ui_voice.voice_speaker_aliases(speaker)):
        editor_line.speaker = speaker
    editor_line.language = str(getattr(scene, "witcher_lipsync_language", "en") or "en")
    editor_line.wav_path = resolved_wav
    editor_line.audio_source = "wav_lipsync" if resolved_wav else ""
    editor_line.duration = ui_lipsync._line_duration_from_wav(resolved_wav)
    ui_lipsync._refresh_line_display_name(editor_line)
    ui_lipsync._set_active_editor_line(scene, editor_line)

    line = scene.witcher_cutscene_dialog_lines[line_index]
    line.lipsync_ref = line_id
    line.allocated_line_id = line_id
    soundstrip = None
    if resolved_wav:
        replace_audio = bool(getattr(scene, "witcher_lipsync_replace_audio", False))
        try:
            scene.witcher_lipsync_replace_audio = False
            soundstrip = ui_lipsync._import_audio_strip(
                context,
                resolved_wav,
                float(getattr(line, "start_frame", 0) or 0),
                line_id,
                str(getattr(line, "text", "") or ""),
                str(getattr(line, "speaker", "") or ""),
                str(getattr(editor_line, "language", "en") or "en"),
                source="wav_lipsync",
                replace_at_start=False,
            )
        finally:
            scene.witcher_lipsync_replace_audio = replace_audio
        editor_line = ui_lipsync._find_editor_line_by_id(scene, line_id)
        editor_line.strip_name = str(getattr(soundstrip, "name", "") or "")
        for prop_name, value in (
            (import_cutscene.CUTSCENE_DIALOG_AUDIO_PROP, True),
            (import_cutscene.CUTSCENE_DIALOG_LINE_ID_PROP, line_id),
            (import_cutscene.CUTSCENE_DIALOG_TEXT_PROP, str(getattr(line, "text", "") or "")),
            (import_cutscene.CUTSCENE_DIALOG_SOURCE_PATH_PROP, _AUTHORED_DIALOG_SOURCE_PATH),
        ):
            soundstrip[prop_name] = value
    ui_lipsync._sync_scene_fields_from_line(scene, editor_line)
    adopt_cutscene_dialog_lipsync_line(scene, editor_line, previous_line_id=old_ref)
    scene.witcher_cutscene_dialog_line_index = line_index
    return editor_line, soundstrip


def sync_authored_cutscene_dialog_items(scene):
    preview = scene.witcher_cutscene_dialog_items
    preview.clear()
    authored = list(getattr(scene, "witcher_cutscene_dialog_lines", []) or [])
    for line in authored:
        line_id = _authored_cutscene_dialog_line_id(line)
        item = preview.add()
        item.actor = str(getattr(line, "speaker", "") or "")
        item.line_text = str(getattr(line, "text", "") or "")
        item.line_id = line_id
        item.line_index = _cutscene_dialog_int32(line_id)
        item.source_game = "W3"
        item.start_frame = int(getattr(line, "start_frame", 0) or 0)
        item.end_frame = max(item.start_frame, int(getattr(line, "end_frame", 0) or 0))
        item.imported_sound = False
    if hasattr(scene, "witcher_cutscene_dialog_index"):
        scene.witcher_cutscene_dialog_index = min(
            max(0, int(getattr(scene, "witcher_cutscene_dialog_line_index", 0) or 0)),
            max(0, len(preview) - 1),
        )
    return len(preview)


def add_cutscene_dialog_line(scene, text=""):
    lines = scene.witcher_cutscene_dialog_lines
    index = len(lines)
    lines.add()
    line = lines[index]
    line.speaker = _active_cutscene_dialog_speaker(scene)
    line.text = str(text or "")
    line.start_frame = int(getattr(scene, "frame_current", 0) or 0)
    line.end_frame = line.start_frame + _dialog_default_duration_frames(line.text, _scene_fps(scene))
    scene.witcher_cutscene_dialog_line_index = index
    sync_authored_cutscene_dialog_items(scene)
    return index


def _cutscene_dialog_lipsync_refs(scene):
    return {
        str(getattr(line, "lipsync_ref", "") or "").strip()
        for line in getattr(scene, "witcher_cutscene_dialog_lines", []) or []
        if str(getattr(line, "tier", "SUBTITLE") or "SUBTITLE") != 'GAME'
    } - {""}


def _push_authored_cutscene_dialog_to_lipsync(scene, line):
    """Sync edits without replacing a linked speaker alias."""
    ref = str(getattr(line, "lipsync_ref", "") or "").strip()
    if not ref or str(getattr(line, "tier", "SUBTITLE") or "SUBTITLE") == 'GAME':
        return
    from ..lipsync import ui_lipsync
    from . import ui_voice

    editor = ui_lipsync._find_editor_line_by_id(scene, ref)
    if editor is None:
        return
    changed = False
    text = str(getattr(line, "text", "") or "")
    if editor.text != text:
        editor.text = text
        changed = True
    speaker = str(getattr(line, "speaker", "") or "").strip()
    editor_speaker = str(editor.speaker or "").strip().upper()
    if speaker and editor_speaker != speaker.upper() and editor_speaker not in ui_voice.voice_speaker_aliases(speaker):
        editor.speaker = speaker
        changed = True
    if changed:
        ui_lipsync._refresh_line_display_name(editor)
        active = ui_lipsync._active_editor_line(scene)
        if active is not None and active.as_pointer() == editor.as_pointer():
            ui_lipsync._sync_scene_fields_from_line(scene, editor)


def add_cutscene_dialog_line_from_lipsync(scene, editor_line, start_frame=None):
    line_id = str(getattr(editor_line, "line_id", "") or "").strip()
    if not line_id:
        raise RuntimeError("Speech line has no line ID")
    fps = _scene_fps(scene)
    lines = scene.witcher_cutscene_dialog_lines
    index = len(lines)
    lines.add()
    line = lines[index]
    line.speaker = _cutscene_dialog_speaker_for_voice(scene, "", getattr(editor_line, "speaker", ""))
    line.text = str(getattr(editor_line, "text", "") or "")
    line.start_frame = int(getattr(scene, "frame_current", 0) if start_frame is None else start_frame)
    try:
        duration = float(getattr(editor_line, "duration", "") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    frames = max(1, int(round(duration * fps))) if duration > 0.0 else _dialog_default_duration_frames(line.text, fps)
    line.end_frame = line.start_frame + frames
    line.tier = 'WAV'
    line.wav_path = str(getattr(editor_line, "wav_path", "") or getattr(editor_line, "project_wav_path", "") or "")
    line.allocated_line_id = line_id
    line.lipsync_ref = line_id
    scene.witcher_cutscene_dialog_line_index = index
    sync_authored_cutscene_dialog_items(scene)
    return index


def add_cutscene_dialog_lines_from_lipsync(context, line_ids, start_frame=None):
    """Append linked Speech lines back-to-back; return (indices, warnings)."""
    from ..lipsync import ui_lipsync

    scene = context.scene
    frame = int(getattr(scene, "frame_current", 0) if start_frame is None else start_frame)
    indices, warnings = [], []
    for line_id in line_ids:
        editor_line = ui_lipsync._find_editor_line_by_id(scene, line_id)
        if editor_line is None:
            warnings.append(f"Speech line {line_id} is not loaded")
            continue
        index = add_cutscene_dialog_line_from_lipsync(scene, editor_line, start_frame=frame)
        line = scene.witcher_cutscene_dialog_lines[index]
        frame = int(line.end_frame)
        indices.append(index)
        if line.wav_path and line.speaker and line.text:
            try:
                prepare_cutscene_dialog_wav_line(context, line_index=index)
            except Exception as exc:
                warnings.append(f"Speech line {line_id}: audio strip not prepared ({exc})")
    if indices:
        scene.witcher_cutscene_dialog_line_index = indices[-1]
    return indices, warnings


def add_picked_lipsync_lines_to_cutscene(operator, context):
    from ..lipsync import ui_lipsync

    scene = context.scene
    picked = [
        line for line in (ui_lipsync._selected_lipsync_lines(scene) or [ui_lipsync._active_editor_line(scene)])
        if line is not None
    ]
    line_ids = [line_id for line_id in (str(getattr(line, "line_id", "") or "").strip() for line in picked) if line_id]
    if not line_ids:
        operator.report({'WARNING'}, "Select a Speech line with a line ID first")
        return {'CANCELLED'}
    for line in picked:
        line.selected = False
    indices, warnings = add_cutscene_dialog_lines_from_lipsync(context, line_ids)
    for message in warnings:
        operator.report({'WARNING'}, message)
    operator.report({'INFO'}, f"Added {len(indices)} dialogue line(s) from Speech")
    return {'FINISHED'} if indices else {'CANCELLED'}


def _authored_cutscene_dialog_preview_id(line):
    return str(
        getattr(line, "game_voice_file_name", "")
        or getattr(line, "game_line_id", "")
        or ""
    ).strip()


def _authored_cutscene_dialog_nla_track(line_id):
    return _AUTHORED_DIALOG_NLA_PREFIX + str(line_id or "").strip()


def _remove_authored_cutscene_dialog_preview(scene, line_id):
    line_id = str(line_id or "").strip()
    if not line_id:
        return 0
    removed = import_cutscene.remove_cutscene_dialog_audio_strips(
        scene,
        source_path=_AUTHORED_DIALOG_SOURCE_PATH,
        line_id=line_id,
    )
    try:
        from . import ui_voice

        removed += ui_voice.remove_voice_lipsync_tracks(
            scene, _authored_cutscene_dialog_nla_track(line_id),
        )
    except Exception:
        log.debug("Could not remove dialogue lipsync preview %s", line_id, exc_info=True)
    return removed


def _authored_cutscene_dialog_strip_props(context, line):
    line_id = str(getattr(line, "game_line_id", "") or "").strip()
    text = str(getattr(line, "text", "") or "")
    speaker = str(getattr(line, "speaker", "") or "")
    text_language = dialog_language.get_active_text_language(context)
    return {
        dialog_language.DIALOG_SUBTITLE_TEXT_PROP: text,
        dialog_language.DIALOG_SUBTITLE_LINE_ID_PROP: line_id,
        dialog_language.DIALOG_SUBTITLE_SPEAKER_PROP: speaker,
        dialog_language.DIALOG_SUBTITLE_SOURCE_PROP: "cutscene_authored",
        dialog_language.DIALOG_SUBTITLE_SOURCE_PATH_PROP: _AUTHORED_DIALOG_SOURCE_PATH,
        dialog_language.DIALOG_SUBTITLE_LANGUAGE_PROP: text_language,
        import_cutscene.CUTSCENE_DIALOG_AUDIO_PROP: True,
        import_cutscene.CUTSCENE_DIALOG_LINE_ID_PROP: line_id,
        import_cutscene.CUTSCENE_DIALOG_TEXT_PROP: text,
        import_cutscene.CUTSCENE_DIALOG_SOURCE_PATH_PROP: _AUTHORED_DIALOG_SOURCE_PATH,
        import_cutscene.CUTSCENE_DIALOG_SOURCE_GAME_PROP: "W3",
    }


def load_authored_cutscene_game_line(context, line_index=None, cleanup=True):
    scene = context.scene
    lines = scene.witcher_cutscene_dialog_lines
    if line_index is None:
        line_index = int(getattr(scene, "witcher_cutscene_dialog_line_index", 0) or 0)
    line_index = int(line_index)
    if not 0 <= line_index < len(lines):
        raise RuntimeError("Dialogue line is unavailable")
    line = lines[line_index]
    if str(getattr(line, "tier", "SUBTITLE") or "SUBTITLE") != 'GAME':
        raise RuntimeError("Selected dialogue line is not a game line")
    voice_line_id = _authored_cutscene_dialog_preview_id(line)
    if not voice_line_id:
        raise RuntimeError("Choose a game voice line first")
    preview_line_id = str(getattr(line, "game_line_id", "") or voice_line_id).strip()
    if cleanup:
        _remove_authored_cutscene_dialog_preview(scene, preview_line_id)
    actor = _find_actor_obj_by_voicetag(scene, getattr(line, "speaker", ""))
    strip_props = _authored_cutscene_dialog_strip_props(context, line)

    from .ui_voice import load_voice_and_lipsync

    frame_current = int(getattr(scene, "frame_current", 0) or 0)
    frame_subframe = float(getattr(scene, "frame_subframe", 0.0) or 0.0)
    try:
        return load_voice_and_lipsync(
            voice_line_id,
            actor=actor,
            context=context,
            at_frame=float(getattr(line, "start_frame", 0) or 0),
            strip_props=strip_props,
            nla_mode="replace",
            allow_context_actor=False,
            nla_track=_authored_cutscene_dialog_nla_track(preview_line_id),
        )
    finally:
        scene.frame_set(frame_current, subframe=frame_subframe)


def _cutscene_dialog_speaker_for_voice(scene, current_speaker, voice_speaker):
    """Prefer an existing cast alias for the selected voice."""
    from . import ui_voice

    voice_speaker = str(voice_speaker or "").strip().upper()
    current_speaker = str(current_speaker or "").strip()
    if not voice_speaker or voice_speaker == "UNKN":
        return current_speaker
    if current_speaker and voice_speaker in ui_voice.voice_speaker_aliases(current_speaker):
        return current_speaker
    for candidate in _cutscene_dialog_speaker_candidates(scene):
        if voice_speaker in ui_voice.voice_speaker_aliases(candidate):
            return candidate
    return ui_voice.voice_speaker_voicetag(voice_speaker) or voice_speaker


def apply_cutscene_dialog_voice_result(context, state):
    scene = context.scene
    line_index = _coerce_cutscene_index(getattr(state, "line_index", -1))
    lines = scene.witcher_cutscene_dialog_lines
    if not 0 <= line_index < len(lines):
        raise RuntimeError("Dialogue line is unavailable")
    voice_line_id = str(getattr(state, "selected_voice_line_id", "") or "").strip()
    line_id = str(getattr(state, "selected_line_id", "") or voice_line_id).strip()
    if not voice_line_id or not line_id:
        raise RuntimeError("Select a voice line first")

    old_line_id = str(getattr(lines[line_index], "game_line_id", "") or "").strip()
    _remove_authored_cutscene_dialog_preview(scene, old_line_id)
    line = lines[line_index]
    line.tier = 'GAME'
    line.game_line_id = line_id
    line.game_voice_file_name = voice_line_id
    selected_speaker = str(getattr(state, "selected_speaker", "") or "").strip()
    selected_text = str(getattr(state, "selected_text", "") or "").strip()
    speaker = _cutscene_dialog_speaker_for_voice(scene, line.speaker, selected_speaker)
    if speaker != line.speaker:
        line.speaker = speaker
    line.text = selected_text
    try:
        selected_duration = float(str(getattr(state, "selected_duration", "") or "").strip())
    except (TypeError, ValueError):
        selected_duration = 0.0
    if math.isfinite(selected_duration) and selected_duration > 0.0:
        line.end_frame = line.start_frame + max(1, int(round(selected_duration * _scene_fps(scene))))
    scene.witcher_cutscene_dialog_line_index = line_index
    sync_authored_cutscene_dialog_items(scene)
    return scene.witcher_cutscene_dialog_lines[line_index]


def remove_cutscene_dialog_line(scene, context=None):
    lines = scene.witcher_cutscene_dialog_lines
    index = int(getattr(scene, "witcher_cutscene_dialog_line_index", 0) or 0)
    if not 0 <= index < len(lines):
        return False
    line_id = _authored_cutscene_dialog_line_id(lines[index])
    if line_id:
        _remove_authored_cutscene_dialog_preview(scene, line_id)
    lines.remove(index)
    scene.witcher_cutscene_dialog_line_index = min(index, max(0, len(lines) - 1))
    sync_authored_cutscene_dialog_items(scene)
    return True


def move_cutscene_dialog_line(scene, direction):
    lines = scene.witcher_cutscene_dialog_lines
    index = int(getattr(scene, "witcher_cutscene_dialog_line_index", 0) or 0)
    target = index + int(direction)
    if not 0 <= index < len(lines) or not 0 <= target < len(lines):
        return index
    lines.move(index, target)
    scene.witcher_cutscene_dialog_line_index = target
    sync_authored_cutscene_dialog_items(scene)
    return target


def set_cutscene_dialog_line_from_playhead(scene):
    lines = scene.witcher_cutscene_dialog_lines
    index = int(getattr(scene, "witcher_cutscene_dialog_line_index", 0) or 0)
    if not 0 <= index < len(lines):
        return False
    line = lines[index]
    preview_id = _authored_cutscene_dialog_line_id(line)
    if preview_id:
        _remove_authored_cutscene_dialog_preview(scene, preview_id)
    duration = max(1, int(line.end_frame) - int(line.start_frame))
    line.start_frame = int(getattr(scene, "frame_current", 0) or 0)
    line.end_frame = line.start_frame + duration
    sync_authored_cutscene_dialog_items(scene)
    return True


def copy_cutscene_preview_to_authored(scene):
    rows = [
        {
            "speaker": str(getattr(item, "actor", "") or ""),
            "text": str(getattr(item, "line_text", "") or ""),
            "line_id": str(getattr(item, "line_id", "") or ""),
            "voice_file": str(getattr(item, "voice_file", "") or ""),
            "scene_path": str(getattr(item, "scene_path", "") or ""),
            "start": int(getattr(item, "start_frame", 0) or 0),
            "end": int(getattr(item, "end_frame", 0) or 0),
        }
        for item in getattr(scene, "witcher_cutscene_dialog_items", []) or []
    ]
    if not rows:
        return 0
    project_lines = {}
    try:
        from ..lipsync import redkit_project

        project_path = redkit_project.get_active_project_path(bpy.context)
        if project_path:
            project_lines = {
                str(project_line.line_id): project_line
                for project_line in redkit_project.read_project_voice_lines(
                    project_path,
                    language=dialog_language.get_active_text_language(bpy.context),
                    include_unvoiced=True,
                )
            }
    except Exception:
        log.debug("Could not inspect project dialogue IDs while copying cutscene preview", exc_info=True)
    authored = scene.witcher_cutscene_dialog_lines
    authored.clear()
    for row in rows:
        index = len(authored)
        authored.add()
        line = authored[index]
        line.speaker = row["speaker"]
        line.text = row["text"]
        line.start_frame = row["start"]
        line.end_frame = max(
            row["start"] + 1,
            row["end"] or row["start"] + _dialog_default_duration_frames(row["text"], _scene_fps(scene)),
        )
        if row["line_id"]:
            project_line = project_lines.get(row["line_id"])
            if project_line is not None and row["scene_path"] and project_line.resource:
                scene_path = row["scene_path"].replace("/", "\\").lower()
                if f'"{scene_path}"' not in project_line.resource.replace("/", "\\").lower():
                    project_line = None
            try:
                allocated_id = (
                    int(row["line_id"]) >= export_cutscene._RADISH_STRING_ID_BASE
                    or project_line is not None
                )
            except (TypeError, ValueError):
                allocated_id = False
            if allocated_id and not row["voice_file"]:
                line.allocated_line_id = row["line_id"]
                assets = getattr(project_line, "assets", None)
                if assets is not None and any(
                    bool(getattr(assets, name, False)) for name in ("has_wav", "has_wem", "has_re")
                ):
                    line.tier = 'WAV'
                    line.lipsync_ref = row["line_id"]
                    if getattr(assets, "wav_path", None):
                        line.wav_path = str(assets.wav_path)
                else:
                    line.tier = 'SUBTITLE'
            else:
                line.tier = 'GAME'
                line.game_line_id = row["line_id"]
                line.game_voice_file_name = row["voice_file"]
    scene.witcher_cutscene_dialog_line_index = 0
    sync_authored_cutscene_dialog_items(scene)
    return len(rows)


def _cutscene_dialog_line_id(dialog_data):
    line_id = str(dialog_data.get("line_id", "") or "").strip()
    if line_id:
        return line_id
    try:
        line_index = int(dialog_data.get("line_index", 0) or 0)
    except (TypeError, ValueError):
        line_index = 0
    return str(line_index) if line_index else ""


def _cutscene_dialog_int32(value):
    try:
        int_value = int(value or 0)
    except (TypeError, ValueError):
        return 0
    if int_value < -2147483648 or int_value > 2147483647:
        return 0
    return int_value


def _numeric_voice_candidate(value):
    value = str(value or "").strip().replace("\\", "/")
    if not value:
        return ""
    stem = os.path.splitext(os.path.basename(value))[0]
    return stem if stem.isdigit() else ""


def _cutscene_dialog_voice_id_candidates(dialog_data):
    candidates = []
    for value in (
        _numeric_voice_candidate(dialog_data.get("voice_file", "")),
        _cutscene_dialog_line_id(dialog_data),
    ):
        value = str(value or "").strip()
        if value and value.isdigit() and value not in candidates:
            candidates.append(value)
    return candidates


def _cutscene_dialog_strip_props(filepath, dialog_data, item=None):
    line_id = str(getattr(item, "line_id", "") or _cutscene_dialog_line_id(dialog_data))
    line_text = str(getattr(item, "line_text", "") or dialog_data.get("line_text", "") or "")
    sound_event = str(getattr(item, "sound_event", "") or dialog_data.get("sound_event", "") or "")
    source_game = str(getattr(item, "source_game", "") or dialog_data.get("source_game", "") or "")
    text_language = dialog_language.get_active_text_language()
    return {
        dialog_language.DIALOG_SUBTITLE_TEXT_PROP: line_text,
        dialog_language.DIALOG_SUBTITLE_LINE_ID_PROP: line_id,
        dialog_language.DIALOG_SUBTITLE_SOURCE_PROP: "w2cutscene",
        dialog_language.DIALOG_SUBTITLE_SOURCE_PATH_PROP: str(filepath or ""),
        dialog_language.DIALOG_SUBTITLE_LANGUAGE_PROP: text_language,
        import_cutscene.CUTSCENE_DIALOG_AUDIO_PROP: True,
        import_cutscene.CUTSCENE_DIALOG_LINE_ID_PROP: line_id,
        import_cutscene.CUTSCENE_DIALOG_TEXT_PROP: line_text,
        import_cutscene.CUTSCENE_DIALOG_SOUND_EVENT_PROP: sound_event,
        import_cutscene.CUTSCENE_DIALOG_SOURCE_PATH_PROP: str(filepath or ""),
        import_cutscene.CUTSCENE_DIALOG_SOURCE_GAME_PROP: source_game,
    }


def _find_cutscene_animation_strip_start(scene, animation_entry):
    if animation_entry is None:
        return None
    source_index = _coerce_cutscene_index(getattr(animation_entry, "source_index", -1))
    group = _clip_groups(scene).get(source_index, {})
    starts = [float(getattr(strip, "frame_start", 0.0) or 0.0)
              for _track, strip in group.get("strips", []) or []]
    return min(starts) if starts else None


def _collect_cutscene_dialog_event_frames(scene):
    fps = _scene_fps(scene)
    dialog_events = []
    for event in list(getattr(scene, "witcher_cutscene_event_items", [])):
        if "DialogEvent" not in str(getattr(event, "event_type", "") or ""):
            continue

        event_scope = str(getattr(event, "event_scope", "") or "").upper()
        event_fps = fps
        start_frame = float(getattr(event, "start_time", 0.0) or 0.0) * fps
        source_index = _coerce_cutscene_index(getattr(event, "source_index", -1))
        if event_scope == "ENTRY" and source_index >= 0:
            animation_entry = _find_loaded_cutscene_animation_entry(scene, source_index)
            if animation_entry is not None:
                anim_fps = float(getattr(animation_entry, "frames_per_second", 0.0) or 0.0)
                if anim_fps > 0.0:
                    event_fps = anim_fps
                strip_start = _find_cutscene_animation_strip_start(scene, animation_entry)
                start_frame = float(strip_start or 0.0) + (float(getattr(event, "start_time", 0.0) or 0.0) * event_fps)

        duration_frames = int(round(float(getattr(event, "duration", 0.0) or 0.0) * event_fps))
        dialog_events.append({
            "frame": int(round(start_frame)),
            "duration_frames": max(0, duration_frames),
            "source_index": source_index,
            "event": event,
        })

    if not dialog_events:
        filepath = str(getattr(scene, "witcher_loaded_w2cutscene_path", "") or "").strip()
        if filepath:
            try:
                dialog_events = import_cutscene.collect_cutscene_dialog_event_frames(filepath)
            except Exception:
                log.debug("Could not collect dialog event frames directly from %s.", filepath, exc_info=True)
    return dialog_events


def _finalize_cutscene_dialog_item_ranges(scene):
    fps = _scene_fps(scene)
    items = sorted(
        list(getattr(scene, "witcher_cutscene_dialog_items", [])),
        key=lambda item: int(getattr(item, "start_frame", 0) or 0),
    )
    for idx, item in enumerate(items):
        start_frame = int(getattr(item, "start_frame", 0) or 0)
        end_frame = int(getattr(item, "end_frame", 0) or 0)
        default_end = start_frame + _dialog_default_duration_frames(getattr(item, "line_text", ""), fps)
        if end_frame <= start_frame:
            end_frame = default_end

        if (not bool(getattr(item, "imported_sound", False))
                and not bool(getattr(item, "has_explicit_duration", False)) and idx + 1 < len(items)):
            next_start = int(getattr(items[idx + 1], "start_frame", 0) or 0)
            if next_start > start_frame:
                end_frame = min(end_frame, next_start)

        item.end_frame = max(start_frame, int(end_frame))


def _populate_cutscene_dialog_items(scene, dialog_items, dialog_events):
    scene.witcher_cutscene_dialog_items.clear()
    fps = _scene_fps(scene)
    for idx, dialog_data in enumerate(dialog_items):
        event_info = dialog_events[idx] if idx < len(dialog_events) else None
        start_frame = int(event_info["frame"]) if event_info is not None else 0
        duration_frames = int(event_info["duration_frames"]) if event_info is not None else 0
        has_explicit_duration = duration_frames > 0
        line_text = str(dialog_data.get("line_text", "") or "")
        if duration_frames <= 0:
            try:
                approved_duration = float(dialog_data.get("approved_duration", 0.0) or 0.0)
            except (TypeError, ValueError):
                approved_duration = 0.0
            if approved_duration > 0.0:
                duration_frames = int(round(approved_duration * fps))
                has_explicit_duration = True
        if duration_frames <= 0:
            duration_frames = _dialog_default_duration_frames(line_text, fps)

        item = scene.witcher_cutscene_dialog_items.add()
        item.actor = str(dialog_data.get("actor", "") or "")
        item.voice_file = str(dialog_data.get("voice_file", "") or "")
        item.sound_event = str(dialog_data.get("sound_event", "") or "")
        item.line_id = _cutscene_dialog_line_id(dialog_data)
        item.line_index = _cutscene_dialog_int32(dialog_data.get("line_index", 0))
        item.line_text = line_text
        item.scene_path = str(dialog_data.get("scene_path", "") or "")
        item.source_game = str(dialog_data.get("source_game", "") or "")
        item.start_frame = start_frame
        item.end_frame = start_frame + max(1, duration_frames)
        item.imported_sound = False
        item.has_explicit_duration = has_explicit_duration

    _finalize_cutscene_dialog_item_ranges(scene)


def _resolve_cutscene_dialog_item_text(scene, item, language=""):
    line_id = str(getattr(item, "line_id", "") or getattr(item, "line_index", "") or "").strip()
    if not line_id:
        return ""

    cutscene_filepath = str(getattr(scene, "witcher_loaded_w2cutscene_path", "") or "").strip()
    source_scene_path = ""
    linked_scene_path = str(getattr(item, "scene_path", "") or "").strip()
    if linked_scene_path:
        try:
            source_scene_path = import_cutscene.resolve_cutscene_linked_scene_file(linked_scene_path, cutscene_filepath)
        except Exception:
            log.debug("Could not resolve linked scene path for dialog text: %s", linked_scene_path, exc_info=True)
    return dialog_language.resolve_localized_text(
        line_id,
        source_scene_path or cutscene_filepath,
        language=language,
        source_game=str(getattr(item, "source_game", "") or ""),
    )


def refresh_cutscene_dialog_language(context, refresh_audio=False):
    scene = getattr(context, "scene", None) if context is not None else None
    if scene is None:
        return 0

    if len(getattr(scene, "witcher_cutscene_dialog_lines", []) or []):
        return sync_authored_cutscene_dialog_items(scene)

    language = dialog_language.get_active_text_language(context)
    updated = 0
    for item in getattr(scene, "witcher_cutscene_dialog_items", []) or []:
        text = _resolve_cutscene_dialog_item_text(scene, item, language=language)
        if str(getattr(item, "line_text", "") or "") != text:
            item.line_text = text
            updated += 1

        line_id = str(getattr(item, "line_id", "") or "").strip()
        if line_id:
            try:
                for strip in import_cutscene._iter_cutscene_dialog_audio_strips(
                    scene,
                    source_path=str(getattr(scene, "witcher_loaded_w2cutscene_path", "") or ""),
                    line_id=line_id,
                ):
                    strip[dialog_language.DIALOG_SUBTITLE_TEXT_PROP] = text
                    strip[dialog_language.DIALOG_SUBTITLE_LANGUAGE_PROP] = language
                    strip[import_cutscene.CUTSCENE_DIALOG_TEXT_PROP] = text
            except Exception:
                log.debug("Could not update dialog strip text for line %s.", line_id, exc_info=True)

    if refresh_audio and str(getattr(scene, "witcher_loaded_w2cutscene_path", "") or "").strip():
        try:
            result = _load_cutscene_dialogs_into_scene(context)
            updated += int(result.get("loaded", 0) or 0) + int(result.get("sound_loaded", 0) or 0)
        except Exception:
            log.warning("Could not refresh cutscene dialog audio/lipsync for the new language.", exc_info=True)

    return updated


def _load_cutscene_dialogs_into_scene(context):
    from ..ui.ui_voice import load_voice_and_lipsync, load_w2_voice_and_lipsync

    scene = context.scene
    filepath = str(getattr(scene, "witcher_loaded_w2cutscene_path", "") or "").strip()
    if not filepath:
        raise RuntimeError("No cutscene loaded.")

    dialog_items = import_cutscene.load_cutscene_dialog_items(filepath)
    dialog_events = _collect_cutscene_dialog_event_frames(scene)
    _populate_cutscene_dialog_items(scene, dialog_items, dialog_events)

    if not dialog_items:
        return {"loaded": 0, "skipped": 0, "total": 0, "sound_loaded": 0}

    loaded = 0
    skipped = 0
    sound_loaded = 0
    for idx, dialog_data in enumerate(dialog_items):
        item = scene.witcher_cutscene_dialog_items[idx] if idx < len(scene.witcher_cutscene_dialog_items) else None
        line_id = str(getattr(item, "line_id", "") or _cutscene_dialog_line_id(dialog_data)).strip()

        voicetag = str(dialog_data.get("actor", "") or "")
        actor_obj = _find_actor_obj_by_voicetag(scene, voicetag)
        at_frame = float(getattr(item, "start_frame", 0) if item is not None else 0)
        strip_props = _cutscene_dialog_strip_props(filepath, dialog_data, item=item)

        imported_line = False
        soundstrip = None
        try:
            if line_id:
                import_cutscene.remove_cutscene_dialog_audio_strips(
                    scene,
                    source_path=filepath,
                    line_id=line_id,
                )
        except Exception:
            log.debug("Could not remove existing dialog audio for line %s", line_id, exc_info=True)

        for voice_id in _cutscene_dialog_voice_id_candidates(dialog_data):
            try:
                load_voice = (
                    load_w2_voice_and_lipsync
                    if str(dialog_data.get("source_game", "") or "").upper() == "W2"
                    else load_voice_and_lipsync
                )
                soundstrip = load_voice(
                    str(voice_id),
                    actor=actor_obj,
                    context=context,
                    at_frame=at_frame,
                    strip_props=strip_props,
                )
                imported_line = True
                break
            except Exception as exc:
                log.warning("Failed to load voice line %s for actor %s: %s", voice_id, voicetag, exc)

        if soundstrip is None and str(dialog_data.get("sound_event", "") or "").strip():
            try:
                soundstrip = import_cutscene.import_sound_event_to_timeline(
                    context,
                    str(dialog_data.get("sound_event", "") or ""),
                    frame_start=at_frame,
                    source_path=filepath,
                    line_id=line_id,
                    line_text=str(dialog_data.get("line_text", "") or ""),
                    strip_props=strip_props,
                )
                imported_line = imported_line or soundstrip is not None
            except Exception as exc:
                log.warning(
                    "Failed to load dialog sound event %s for actor %s: %s",
                    dialog_data.get("sound_event", ""),
                    voicetag,
                    exc,
                )

        if soundstrip is not None and item is not None:
            item.imported_sound = True
            sound_loaded += 1
            try:
                item.end_frame = max(
                    int(getattr(item, "end_frame", 0) or 0),
                    int(math.ceil(float(getattr(soundstrip, "frame_final_end", at_frame) or at_frame))),
                )
            except Exception:
                pass

        if imported_line:
            loaded += 1
        else:
            skipped += 1

    _finalize_cutscene_dialog_item_ranges(scene)
    return {"loaded": loaded, "skipped": skipped, "total": len(dialog_items), "sound_loaded": sound_loaded}


def _cutscene_dialog_data_from_item(item):
    return {
        "actor": str(getattr(item, "actor", "") or ""),
        "voice_file": str(getattr(item, "voice_file", "") or ""),
        "sound_event": str(getattr(item, "sound_event", "") or ""),
        "line_id": str(getattr(item, "line_id", "") or ""),
        "line_index": int(getattr(item, "line_index", 0) or 0),
        "line_text": str(getattr(item, "line_text", "") or ""),
        "scene_path": str(getattr(item, "scene_path", "") or ""),
        "source_game": str(getattr(item, "source_game", "") or ""),
    }


def _dialog_item_matches_actor_entry(item, actor_entry):
    dialog_actor = str(getattr(item, "actor", "") or "").strip().lower()
    if not dialog_actor or actor_entry is None:
        return False
    actor_names = set()
    for value in (
        getattr(actor_entry, "actor_name", ""),
        getattr(actor_entry, "voice_tag", ""),
    ):
        value = str(value or "").strip().lower()
        if value:
            actor_names.add(value)
    tag_value = str(getattr(actor_entry, "tag", "") or "").replace(",", ";")
    for value in tag_value.split(";"):
        value = value.strip().lower()
        if value:
            actor_names.add(value)
    return dialog_actor in actor_names


def _restore_cutscene_actor_dialog_lipsync(context, actor_entry, actor_obj):
    from ..ui.ui_voice import load_voice_and_lipsync, load_w2_voice_and_lipsync

    scene = getattr(context, "scene", None) if context is not None else None
    if scene is None or actor_entry is None or actor_obj is None:
        return {"loaded": 0, "skipped": 0, "sound_loaded": 0}

    filepath = str(getattr(scene, "witcher_loaded_w2cutscene_path", "") or "").strip()
    dialog_items = [
        item for item in getattr(scene, "witcher_cutscene_dialog_items", []) or []
        if _dialog_item_matches_actor_entry(item, actor_entry)
    ]
    if not filepath or not dialog_items:
        return {"loaded": 0, "skipped": 0, "sound_loaded": 0}

    loaded = 0
    skipped = 0
    sound_loaded = 0
    for item in dialog_items:
        dialog_data = _cutscene_dialog_data_from_item(item)
        line_id = str(getattr(item, "line_id", "") or _cutscene_dialog_line_id(dialog_data)).strip()
        at_frame = float(getattr(item, "start_frame", 0) or 0)
        strip_props = _cutscene_dialog_strip_props(filepath, dialog_data, item=item)
        soundstrip = None
        imported_line = False

        try:
            if line_id or dialog_data["sound_event"]:
                import_cutscene.remove_cutscene_dialog_audio_strips(
                    scene,
                    source_path=filepath,
                    line_id=line_id,
                    sound_event=dialog_data["sound_event"],
                )
        except Exception:
            log.debug("Could not remove existing dialog audio for line %s", line_id, exc_info=True)

        for voice_id in _cutscene_dialog_voice_id_candidates(dialog_data):
            try:
                load_voice = (
                    load_w2_voice_and_lipsync
                    if str(dialog_data.get("source_game", "") or "").upper() == "W2"
                    else load_voice_and_lipsync
                )
                soundstrip = load_voice(
                    str(voice_id),
                    actor=actor_obj,
                    context=context,
                    at_frame=at_frame,
                    strip_props=strip_props,
                    nla_mode="replace",
                )
                imported_line = True
                break
            except Exception as exc:
                log.warning(
                    "Failed to restore dialog lipsync line %s for actor %s: %s",
                    voice_id,
                    getattr(actor_entry, "actor_name", ""),
                    exc,
                )

        if soundstrip is None and dialog_data["sound_event"]:
            try:
                soundstrip = import_cutscene.import_sound_event_to_timeline(
                    context,
                    dialog_data["sound_event"],
                    frame_start=at_frame,
                    source_path=filepath,
                    line_id=line_id,
                    line_text=dialog_data["line_text"],
                    strip_props=strip_props,
                )
                imported_line = imported_line or soundstrip is not None
            except Exception as exc:
                log.warning(
                    "Failed to restore dialog sound event %s for actor %s: %s",
                    dialog_data["sound_event"],
                    getattr(actor_entry, "actor_name", ""),
                    exc,
                )

        if soundstrip is not None:
            item.imported_sound = True
            sound_loaded += 1
            try:
                item.end_frame = max(
                    int(getattr(item, "end_frame", 0) or 0),
                    int(math.ceil(float(getattr(soundstrip, "frame_final_end", at_frame) or at_frame))),
                )
            except Exception:
                pass

        if imported_line:
            loaded += 1
        else:
            skipped += 1

    _finalize_cutscene_dialog_item_ranges(scene)
    return {"loaded": loaded, "skipped": skipped, "sound_loaded": sound_loaded}


class WITCH_OT_CutsceneDialogAddLine(Operator):
    bl_idname = "witcher.cutscene_dialog_add_line"
    bl_label = "Add Dialogue Line"
    bl_description = "Add an editable subtitle line at the current timeline frame"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        add_cutscene_dialog_line(context.scene)
        return {'FINISHED'}


class WITCH_OT_CutsceneDialogRemoveLine(Operator):
    bl_idname = "witcher.cutscene_dialog_remove_line"
    bl_label = "Remove Dialogue Line"
    bl_description = "Remove the selected authored line and its loaded audio preview"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        return {'FINISHED'} if remove_cutscene_dialog_line(context.scene, context=context) else {'CANCELLED'}


class WITCH_OT_CutsceneDialogVoiceSearchClear(Operator):
    bl_idname = "witcher.cutscene_dialog_voice_search_clear"
    bl_label = "Clear Voice Search"
    bl_description = "Clear the voice-line text search while keeping the speaker filter"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        state = context.window_manager.witcher_cutscene_dialog_voice
        if state.query:
            state.query = ""
        else:
            refresh_cutscene_dialog_voice_results(state, context)
        return {'FINISHED'}


class WITCH_OT_CutsceneDialogPickGameVoice(Operator):
    bl_idname = "witcher.cutscene_dialog_pick_game_voice"
    bl_label = "Pick Game Voice Line"
    bl_description = "Choose a vanilla line and fill its speaker, subtitle, duration, IDs, audio, and lipsync preview"
    bl_options = {'REGISTER', 'UNDO'}

    line_index: IntProperty(default=-1, min=-1, options={'SKIP_SAVE'})

    def invoke(self, context, event):
        state = reset_cutscene_dialog_voice_dialog(self, context)
        if state is None:
            self.report({'WARNING'}, "Select a dialogue line first")
            return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self, width=760, confirm_text="Use Voice Line")

    def draw(self, context):
        layout = self.layout
        state = context.window_manager.witcher_cutscene_dialog_voice
        filters = layout.row(align=True)
        filters.prop(state, "query")
        filters.operator(WITCH_OT_CutsceneDialogVoiceSearchClear.bl_idname, text="", icon='X')
        layout.prop(state, "speaker")
        if state.match_status:
            layout.label(text=state.match_status, icon='INFO')
        layout.template_list(
            WITCH_UL_CutsceneDialogVoiceResults.bl_idname,
            "cutscene_dialog_voice_results",
            state,
            "results",
            state,
            "result_index",
            rows=10,
        )

        selected = layout.box()
        selected.label(text="Selected", icon='SOUND')
        if state.selected_voice_line_id:
            for prop_name, label in (
                ("selected_line_id", "dialogLine"),
                ("selected_voice_line_id", "voiceFileName"),
                ("selected_speaker", "Speaker"),
                ("selected_text", "Text"),
                ("selected_duration", "Duration"),
                ("selected_source_path", "Source"),
            ):
                value = str(getattr(state, prop_name, "") or "")
                if value:
                    row = selected.row(align=True)
                    _draw_cutscene_exact_value(row, state, prop_name, text=label, value=value)
        else:
            selected.label(text="Select a voice line", icon='INFO')

    def execute(self, context):
        state = context.window_manager.witcher_cutscene_dialog_voice
        if self.line_index >= 0:
            state.line_index = self.line_index
        try:
            line = apply_cutscene_dialog_voice_result(context, state)
        except Exception as exc:
            log.exception("Could not apply cutscene dialogue voice line")
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        try:
            load_authored_cutscene_game_line(context, line_index=state.line_index, cleanup=True)
        except Exception as exc:
            log.warning("Game voice line %s was selected but its preview could not load: %s", line.game_line_id, exc)
            self.report({'WARNING'}, f"Voice line selected; preview failed: {exc}")
            return {'FINISHED'}
        self.report({'INFO'}, f"Loaded game voice line {line.game_line_id}")
        return {'FINISHED'}


class WITCH_OT_CutsceneDialogPreviewGameLine(Operator):
    bl_idname = "witcher.cutscene_dialog_preview_game_line"
    bl_label = "Load Game Voice Preview"
    bl_description = "Reload this line's audio and lipsync at Start; use timeline playback to hear and see it"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        try:
            load_authored_cutscene_game_line(context)
        except Exception as exc:
            log.exception("Could not preview cutscene dialogue voice line")
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class WITCH_OT_CutsceneDialogGenerateWav(Operator):
    bl_idname = "witcher.cutscene_dialog_generate_wav"
    bl_label = "Generate WAV with External TTS"
    bl_description = "Run the configured external TTS command, then prepare the resulting WAV for this line"
    bl_options = {'REGISTER', 'UNDO'}

    line_index: IntProperty(default=-1, min=-1, options={'SKIP_SAVE'})

    def execute(self, context):
        try:
            output_path, _line, _soundstrip, _completed = generate_cutscene_dialog_wav(
                context,
                line_index=self.line_index if self.line_index >= 0 else None,
            )
        except Exception as exc:
            log.exception("Could not generate cutscene dialogue WAV")
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, f"Generated {os.path.basename(output_path)}")
        return {'FINISHED'}


class WITCH_OT_CutsceneDialogPrepareWav(Operator):
    bl_idname = "witcher.cutscene_dialog_prepare_wav"
    bl_label = "Prepare Custom Voice Assets"
    bl_description = "Create or update the linked Lipsync editor line and load its WAV at Start"
    bl_options = {'REGISTER', 'UNDO'}

    line_index: IntProperty(default=-1, min=-1, options={'SKIP_SAVE'})

    def execute(self, context):
        try:
            line, soundstrip = prepare_cutscene_dialog_wav_line(
                context,
                line_index=self.line_index if self.line_index >= 0 else None,
            )
        except Exception as exc:
            log.exception("Could not prepare cutscene dialogue WAV line")
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        suffix = " and previewed WAV" if soundstrip is not None else ""
        self.report({'INFO'}, f"Prepared Lipsync line {line.line_id}{suffix}")
        return {'FINISHED'}


def _cutscene_dialog_operator_line_index(operator, scene):
    index = int(getattr(operator, "line_index", -1))
    if index < 0:
        index = int(getattr(scene, "witcher_cutscene_dialog_line_index", 0) or 0)
    return index if 0 <= index < len(scene.witcher_cutscene_dialog_lines) else -1


class WITCH_OT_CutsceneDialogAddFromSpeech(Operator):
    bl_idname = "witcher.cutscene_dialog_add_from_speech"
    bl_label = "Add From Speech"
    bl_description = (
        "Add Speech tab lines back to back from the current frame; tick several for a batch. "
        "Linked lines follow the Speech line's text, ID and audio"
    )
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        from ..lipsync import redkit_project, ui_lipsync

        scene = context.scene
        lines = ui_lipsync._editor_lines(scene)
        if lines is not None and not len(lines) and redkit_project.get_active_project_path(context):
            try:
                ui_lipsync.load_project_lines_into_editor(context, clear_existing=False)
            except Exception as exc:
                log.warning("Could not load REDkit project strings for the Speech picker: %s", exc)
        if lines is None or not len(lines):
            self.report({'WARNING'}, "No Speech lines yet: add them in Animation › Speech or select a REDkit project")
            return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self, width=640, confirm_text="Add Lines")

    def draw(self, context):
        layout = self.layout
        layout.label(text="Tick lines to add in order, or add the highlighted line", icon='INFO')
        layout.template_list(
            WITCH_UL_CutsceneSpeechPick.bl_idname, "cutscene_speech_pick",
            context.scene, "witcher_lipsync_lines",
            context.scene, "witcher_lipsync_line_index",
            rows=10,
        )

    def execute(self, context):
        return add_picked_lipsync_lines_to_cutscene(self, context)


class WITCH_OT_CutsceneDialogSendToSpeech(Operator):
    bl_idname = "witcher.cutscene_dialog_send_to_speech"
    bl_label = "Send to Speech"
    bl_description = "Create or update this line in Animation › Speech and keep both linked by line ID"
    bl_options = {'REGISTER', 'UNDO'}

    line_index: IntProperty(default=-1, min=-1, options={'SKIP_SAVE'})

    def execute(self, context):
        scene = context.scene
        index = _cutscene_dialog_operator_line_index(self, scene)
        if index < 0:
            self.report({'WARNING'}, "Select a dialogue line first")
            return {'CANCELLED'}
        line = scene.witcher_cutscene_dialog_lines[index]
        if line.tier == 'GAME':
            self.report({'WARNING'}, "Game lines come from the voice cache, not the Speech tab")
            return {'CANCELLED'}
        previous_tier = line.tier
        line.tier = 'WAV'
        try:
            prepare_cutscene_dialog_wav_line(context, line_index=index)
        except Exception as exc:
            scene.witcher_cutscene_dialog_lines[index].tier = previous_tier
            log.exception("Could not send dialogue line to the Speech tab")
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, f"Linked to Speech line {scene.witcher_cutscene_dialog_lines[index].lipsync_ref}")
        return {'FINISHED'}


class WITCH_OT_CutsceneDialogOpenSpeech(Operator):
    """Open the linked line in Speech."""
    bl_idname = "witcher.cutscene_dialog_open_speech"
    bl_label = "Open in Speech"
    bl_options = {'INTERNAL'}

    line_index: IntProperty(default=-1, min=-1, options={'SKIP_SAVE'})

    def execute(self, context):
        from ..lipsync import ui_lipsync

        scene = context.scene
        index = _cutscene_dialog_operator_line_index(self, scene)
        if index < 0:
            self.report({'WARNING'}, "Select a dialogue line first")
            return {'CANCELLED'}
        ref = str(scene.witcher_cutscene_dialog_lines[index].lipsync_ref or "").strip()
        editor = ui_lipsync._find_editor_line_by_id(scene, ref)
        if editor is None:
            self.report({'WARNING'}, f"Speech line {ref or '?'} is not loaded; use Send to Speech")
            return {'CANCELLED'}
        ui_lipsync._set_active_editor_line(scene, editor)
        if hasattr(scene, "witcher_anim_tab"):
            scene.witcher_anim_tab = 'SPEECH'
        return {'FINISHED'}


class WITCH_OT_CutsceneDialogUnlinkSpeech(Operator):
    """Unlink the Speech line without deleting its data."""
    bl_idname = "witcher.cutscene_dialog_unlink_speech"
    bl_label = "Unlink from Speech"
    bl_options = {'REGISTER', 'UNDO'}

    line_index: IntProperty(default=-1, min=-1, options={'SKIP_SAVE'})

    def execute(self, context):
        scene = context.scene
        index = _cutscene_dialog_operator_line_index(self, scene)
        if index < 0:
            self.report({'WARNING'}, "Select a dialogue line first")
            return {'CANCELLED'}
        scene.witcher_cutscene_dialog_lines[index].lipsync_ref = ""
        sync_authored_cutscene_dialog_items(scene)
        return {'FINISHED'}


class WITCH_OT_CutsceneDialogMoveLine(Operator):
    bl_idname = "witcher.cutscene_dialog_move_line"
    bl_label = "Move Dialogue Line"
    bl_description = "Move the selected line up or down in exported dialogue order"
    bl_options = {'REGISTER', 'UNDO'}

    direction: EnumProperty(
        items=[('UP', "Up", "Move line up"), ('DOWN', "Down", "Move line down")],
        options={'SKIP_SAVE'},
    )

    def execute(self, context):
        move_cutscene_dialog_line(context.scene, -1 if self.direction == 'UP' else 1)
        return {'FINISHED'}


class WITCH_OT_CutsceneDialogFromPlayhead(Operator):
    bl_idname = "witcher.cutscene_dialog_from_playhead"
    bl_label = "Start Dialogue at Current Frame"
    bl_description = (
        "Set Start to the current timeline frame and shift End by the same amount, preserving duration; "
        "reload any audio/lipsync preview afterward"
    )
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        return {'FINISHED'} if set_cutscene_dialog_line_from_playhead(context.scene) else {'CANCELLED'}


class WITCH_OT_CutsceneDialogCopyPreview(Operator):
    bl_idname = "witcher.cutscene_dialog_copy_preview"
    bl_label = "Copy Preview to Editable Lines"
    bl_description = "Copy imported read-only lines into the authored companion-scene output"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        count = copy_cutscene_preview_to_authored(context.scene)
        if not count:
            self.report({'WARNING'}, "No preview lines to copy")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Copied {count} dialogue line(s)")
        return {'FINISHED'}


class WITCH_OT_LoadCutsceneDialogs(Operator):
    bl_idname = "witcher.load_cutscene_dialogs"
    bl_label = "Load Dialogs"
    bl_description = (
        "Read CStorySceneLine fields from the linked .w2scene, then load each "
        "voiceFileName/dialogLine + lipsync onto the matching voicetag at the time given by the cutscene's "
        "CExtAnimCutsceneDialogEvent markers"
    )

    def execute(self, context):
        scene = context.scene
        filepath = str(getattr(scene, "witcher_loaded_w2cutscene_path", "") or "").strip()
        if not filepath:
            self.report({'WARNING'}, "No cutscene loaded.")
            return {'CANCELLED'}

        try:
            stats = _load_cutscene_dialogs_into_scene(context)
        except Exception as exc:
            log.exception("Failed to load dialog items for %s", filepath)
            self.report({'ERROR'}, f"Dialog load failed: {exc}")
            return {'CANCELLED'}

        loaded = int(stats.get("loaded", 0) or 0)
        skipped = int(stats.get("skipped", 0) or 0)
        total = int(stats.get("total", 0) or 0)
        sound_loaded = int(stats.get("sound_loaded", 0) or 0)
        if total <= 0:
            self.report({'INFO'}, "No dialog lines found in linked .w2scene.")
            return {'FINISHED'}

        msg = f"Loaded {loaded} voice line(s)"
        if sound_loaded:
            msg += f", {sound_loaded} sound strip(s)"
        if skipped:
            msg += f" ({skipped} skipped)"
        self.report({'INFO'}, msg)
        return {'FINISHED'}


def _get_cutscene_animation_label(animation_entry):
    if animation_entry is None:
        return "Animation"
    full_name = str(getattr(animation_entry, "full_name", "") or "").strip()
    if full_name:
        return full_name
    display_name = str(getattr(animation_entry, "display_name", "") or "").strip()
    actor_name = str(getattr(animation_entry, "actor_name", "") or "").strip()
    if actor_name and display_name:
        return f"{actor_name}: {display_name}"
    return display_name or "Animation"

def _clear_cutscene_preview(operator):
    operator.cutscene_actor_items.clear()
    operator.cutscene_animation_items.clear()
    operator.cutscene_actor_index = 0
    operator.cutscene_animation_index = 0

def _cutscene_actor_preview_key(item):
    return (
        int(getattr(item, "source_index", -1)),
        str(getattr(item, "actor_name", "") or ""),
        str(getattr(item, "template_path", "") or ""),
        str(getattr(item, "appearance_name", "") or ""),
    )

def _cutscene_animation_preview_key(item):
    return (
        int(getattr(item, "source_index", -1)),
        str(getattr(item, "full_name", "") or ""),
        str(getattr(item, "actor_name", "") or ""),
        str(getattr(item, "component_name", "") or ""),
    )

def _update_cutscene_preview(operator):
    filepath = str(getattr(operator, "filepath", "") or "").strip()
    if not filepath or os.path.isdir(filepath):
        _clear_cutscene_preview(operator)
        operator.cutscene_preview_path = filepath
        operator.cutscene_preview_mtime = 0.0
        operator.cutscene_preview_status = "Select a .w2cutscene file"
        return True

    lowered = filepath.lower()
    if not lowered.endswith(".w2cutscene"):
        _clear_cutscene_preview(operator)
        operator.cutscene_preview_path = filepath
        operator.cutscene_preview_mtime = 0.0
        operator.cutscene_preview_status = "Unsupported file type"
        return True

    try:
        mtime = os.path.getmtime(filepath)
    except OSError:
        _clear_cutscene_preview(operator)
        operator.cutscene_preview_path = filepath
        operator.cutscene_preview_mtime = 0.0
        operator.cutscene_preview_status = "File not found"
        return True

    if (
        operator.cutscene_preview_path == filepath
        and abs(operator.cutscene_preview_mtime - mtime) < 0.0001
        and (operator.cutscene_actor_items or operator.cutscene_animation_items)
    ):
        return False

    old_actor_selection = {
        _cutscene_actor_preview_key(item): bool(item.selected)
        for item in operator.cutscene_actor_items
    }
    old_animation_selection = {
        _cutscene_animation_preview_key(item): bool(item.selected)
        for item in operator.cutscene_animation_items
    }
    old_actor_index = int(getattr(operator, "cutscene_actor_index", 0) or 0)
    old_animation_index = int(getattr(operator, "cutscene_animation_index", 0) or 0)

    _clear_cutscene_preview(operator)
    operator.cutscene_preview_path = filepath
    operator.cutscene_preview_mtime = mtime

    try:
        _cutscene, actor_items, animation_items, event_items = import_cutscene.collect_cutscene_preview(filepath)
    except Exception as exc:
        log.exception("Failed to build cutscene preview for %s", filepath)
        operator.cutscene_preview_status = f"Preview error: {exc}"
        return True

    if not actor_items and not animation_items:
        operator.cutscene_preview_status = "No actors or animations found in file"
        return True

    for actor_data in actor_items:
        item = operator.cutscene_actor_items.add()
        item.source_index = int(actor_data["source_index"])
        item.label = str(actor_data["label"])
        item.actor_name = str(actor_data["actor_name"])
        item.template_path = str(actor_data["template_path"])
        item.source_game = str(actor_data.get("source_game", "") or "")
        item.appearance_name = str(actor_data["appearance_name"])
        item.actor_type = str(actor_data["actor_type"])
        item.use_mimic = bool(actor_data["use_mimic"])
        item.already_in_scene = bool(actor_data["already_in_scene"])
        actor_key = _cutscene_actor_preview_key(item)
        item.selected = old_actor_selection.get(actor_key, True)

    for animation_data in animation_items:
        item = operator.cutscene_animation_items.add()
        item.source_index = int(animation_data["source_index"])
        item.full_name = str(animation_data["full_name"])
        item.display_name = str(animation_data["display_name"])
        item.actor_name = str(animation_data["actor_name"])
        item.component_name = str(animation_data["component_name"])
        item.frames_per_second = float(animation_data["frames_per_second"])
        item.num_frames = int(animation_data["num_frames"])
        item.duration = float(animation_data["duration"])
        animation_key = _cutscene_animation_preview_key(item)
        item.selected = old_animation_selection.get(animation_key, True)

    if operator.cutscene_actor_items:
        operator.cutscene_actor_index = min(max(0, old_actor_index), len(operator.cutscene_actor_items) - 1)
    else:
        operator.cutscene_actor_index = 0

    if operator.cutscene_animation_items:
        operator.cutscene_animation_index = min(max(0, old_animation_index), len(operator.cutscene_animation_items) - 1)
    else:
        operator.cutscene_animation_index = 0

    event_suffix = f", {len(event_items)} event(s)" if event_items else ""
    operator.cutscene_preview_status = (
        f"{len(actor_items)} actor(s), {len(animation_items)} animation(s){event_suffix} found"
    )
    return True

def _clear_loaded_cutscene_state(scene):
    if hasattr(bpy.types.Scene, "witcher_cs_event_target"):
        scene.witcher_cs_event_target = "ROOT"
    scene.witcher_cutscene_actor_items.clear()
    scene.witcher_cutscene_animation_items.clear()
    scene.witcher_cutscene_event_items.clear()
    scene.witcher_cutscene_template_fields.clear()
    scene.witcher_cutscene_effect_items.clear()
    scene.witcher_cutscene_dialog_items.clear()
    if hasattr(scene, "witcher_cutscene_dialog_lines"):
        scene.witcher_cutscene_dialog_lines.clear()
        scene.witcher_cutscene_dialog_line_index = 0
    scene.witcher_loaded_cutscene_name = ""
    _set_cutscene_burned_audio_scene_state(scene, event_name="", item_path="")
    _set_cutscene_export_metadata_scene_state(scene, synced=False)
    if hasattr(scene, "witcher_cutscene_last_import_seconds"):
        scene.witcher_cutscene_last_import_seconds = 0.0
    if hasattr(scene, "witcher_loaded_w2cutscene_path"):
        scene.witcher_loaded_w2cutscene_path = ""
    if hasattr(scene, "witcher_w2scene_active_cutscene_path"):
        scene.witcher_w2scene_active_cutscene_path = ""


def _cutscene_active_collection(context=None):
    context = context or bpy.context
    collection = getattr(context, "collection", None)
    if collection is not None:
        return collection
    active_layer = getattr(getattr(context, "view_layer", None), "active_layer_collection", None)
    collection = getattr(active_layer, "collection", None)
    if collection is not None:
        return collection
    return bpy.context.scene.collection


def _cutscene_actor_template_path(cutscene_repo_path, suffix):
    cutscene_repo_path = str(cutscene_repo_path or "").strip().replace("/", "\\")
    folder, filename = os.path.split(cutscene_repo_path)
    stem, _ext = os.path.splitext(filename)
    if not stem:
        stem = "new_cutscene"
    actor_filename = f"{stem}_{suffix}.w2ent"
    return os.path.join(folder, actor_filename).replace("/", "\\") if folder else actor_filename


def _find_cutscene_actor_armature(scene, actor_name):
    actor_key = str(actor_name or "").strip().lower()
    if not actor_key:
        return None
    for obj in getattr(scene, "objects", []) or []:
        if getattr(obj, "type", None) != 'ARMATURE':
            continue
        if str(obj.get("cutscene_actor_name", "") or "").strip().lower() == actor_key:
            return obj
    prefix = f"{actor_key}:"
    for obj in getattr(scene, "objects", []) or []:
        if getattr(obj, "type", None) == 'ARMATURE' and str(getattr(obj, "name", "") or "").lower().startswith(prefix):
            return obj
    return None


def _set_cutscene_actor_armature_name(armature_obj, actor_name):
    if armature_obj is None:
        return
    target_name = f"{actor_name}:CAnimatedComponent2_ARM"
    try:
        armature_obj.name = target_name
    except Exception:
        pass
    data = getattr(armature_obj, "data", None)
    if data is not None:
        try:
            data.name = f"{target_name}_DATA"
        except Exception:
            pass


def _ensure_default_cutscene_camera(context, camera_armature=None):
    scene = getattr(context, "scene", None)
    if scene is None:
        return None
    try:
        from . import ui_anims
        camera_obj = ui_anims.find_camera_preview_object(camera_armature) if camera_armature is not None else None
    except Exception:
        camera_obj = None
    if camera_obj is None:
        camera_obj = getattr(scene, "camera", None)
    if camera_obj is None or getattr(camera_obj, "type", None) != 'CAMERA':
        cam_data = bpy.data.cameras.new("Camera")
        camera_obj = bpy.data.objects.new("Camera", cam_data)
        _cutscene_active_collection(context).objects.link(camera_obj)
        camera_obj.location = (0.0, -4.0, 1.6)
        camera_obj.rotation_euler = (math.radians(68.0), 0.0, 0.0)
    scene.camera = camera_obj
    return camera_obj


def _ensure_default_cutscene_trajectories_actor(context, cutscene_repo_path):
    scene = getattr(context, "scene", None)
    existing = _find_cutscene_actor_armature(scene, "trajectories")
    template_path = _cutscene_actor_template_path(cutscene_repo_path, "trajectories")
    if existing is not None:
        _set_cutscene_actor_armature_name(existing, "trajectories")
        existing["cutscene_actor_name"] = "trajectories"
        existing["cutscene_actor_template"] = template_path
        existing["cutscene_generated_actor_template"] = "trajectories"
        existing["cutscene_actor_type"] = "CAT_Actor"
        existing["cutscene_component"] = "Root"
        existing["cutscene_actor_appearance"] = ""
        existing["cutscene_actor_use_mimic"] = False
        _ensure_actor_custom_props(existing)
        return existing, False
    from . import ui_animated_component
    from ..CR2W import animated_component as ac
    armature = ui_animated_component.create_animated_component(
        "trajectories",
        ac.TRAJECTORY_RIG_PATH,
        ac.trajectory_bone_names(ac.TRAJECTORY_BONE_COUNT),
        template_path,
        "",
        target_collection=_cutscene_active_collection(context),
    )
    armature["cutscene_actor_name"] = "trajectories"
    armature["cutscene_actor_template"] = template_path
    armature["cutscene_generated_actor_template"] = "trajectories"
    armature["cutscene_actor_type"] = "CAT_Actor"
    armature["cutscene_component"] = "Root"
    armature["cutscene_actor_appearance"] = ""
    armature["cutscene_actor_use_mimic"] = False
    armature[import_cutscene.CUTSCENE_ACTOR_IMPORTED_PROP] = False
    _ensure_actor_custom_props(armature)
    return armature, True


def _ensure_default_cutscene_camera_actor(context):
    scene = getattr(context, "scene", None)
    existing = _find_cutscene_actor_armature(scene, "camera")
    if existing is not None:
        try:
            from . import ui_anims
            _set_cutscene_actor_armature_name(existing, "camera")
            ui_anims._tag_scratch_cutscene_actor(
                existing,
                actor_name="camera",
                template_path=getattr(scene, "witcher_cutscene_scratch_camera_repo_path", "") or ui_anims.SCRATCH_CAMERA_DEFAULT_REPO_PATH,
                actor_type="CAT_Camera",
                appearance="",
                use_mimic=False,
                imported_new=bool(existing.get(import_cutscene.CUTSCENE_ACTOR_IMPORTED_PROP, False)),
            )
        except Exception:
            pass
        return existing, False
    from . import ui_anims
    camera_armature = ui_anims._import_cutscene_camera_rig(context)
    if camera_armature is None:
        return None, False
    _set_cutscene_actor_armature_name(camera_armature, "camera")
    ui_anims._tag_scratch_cutscene_actor(
        camera_armature,
        actor_name="camera",
        template_path=getattr(scene, "witcher_cutscene_scratch_camera_repo_path", "") or ui_anims.SCRATCH_CAMERA_DEFAULT_REPO_PATH,
        actor_type="CAT_Camera",
        appearance="",
        use_mimic=False,
        imported_new=True,
    )
    return camera_armature, True


def _setup_new_cutscene_defaults(context, cutscene_repo_path):
    scene = getattr(context, "scene", None)
    created = []
    if scene is None:
        return created
    trajectories_armature, created_trajectories = _ensure_default_cutscene_trajectories_actor(context, cutscene_repo_path)
    if trajectories_armature is not None and created_trajectories:
        created.append("trajectories")
    camera_armature, created_camera = _ensure_default_cutscene_camera_actor(context)
    if camera_armature is not None and created_camera:
        created.append("camera")
    _ensure_default_cutscene_camera(context, camera_armature)
    _sync_actor_items_with_scene(scene)
    return created


def _schedule_deferred_cutscene_state_sync(scene, filepath):
    scene_name = str(getattr(scene, "name", "") or "").strip()
    filepath = str(filepath or "").strip()
    if not scene_name or not filepath:
        return
    key = (scene_name, filepath)
    if key in _CUTSCENE_SYNC_DEFERRED:
        return
    _CUTSCENE_SYNC_DEFERRED.add(key)

    def _do_sync():
        _CUTSCENE_SYNC_DEFERRED.discard(key)
        target_scene = bpy.data.scenes.get(scene_name)
        if target_scene is None:
            return None
        try:
            _sync_loaded_cutscene_state(target_scene, filepath)
        except Exception:
            log.exception("Failed deferred cutscene state sync for %s", filepath)
        return None

    try:
        bpy.app.timers.register(_do_sync, first_interval=0.0)
    except Exception:
        _CUTSCENE_SYNC_DEFERRED.discard(key)

def _get_loaded_cutscene_name(filepath):
    filepath = str(filepath or "").strip()
    if not filepath:
        return ""
    return os.path.basename(filepath)


def _get_loaded_cutscene_burned_audio_strip(scene):
    filepath = str(getattr(scene, "witcher_loaded_w2cutscene_path", "") or "").strip()
    if not filepath:
        return None
    return import_cutscene.find_cutscene_burned_audio_strip(scene, source_path=filepath)

def _find_loaded_cutscene_animation_entry(scene, source_index):
    try:
        source_index = int(source_index)
    except Exception:
        source_index = -1
    for item in getattr(scene, "witcher_cutscene_animation_items", []):
        if int(getattr(item, "source_index", -1)) == source_index:
            return item
    return None

def _get_loaded_cutscene_actor_object(actor_entry):
    if actor_entry is None:
        return None
    object_name = str(getattr(actor_entry, "object_name", "") or "").strip()
    if not object_name:
        return None
    obj = bpy.data.objects.get(object_name)
    if obj is None or getattr(obj, "type", None) != 'ARMATURE':
        return None
    return obj


def _loaded_cutscene_actor_source_game(actor_obj, fallback=""):
    if actor_obj is None:
        return str(fallback or "")
    for prop_name in (
        import_cutscene.CUTSCENE_ACTOR_SOURCE_GAME_PROP,
        "cutscene_actor_replacement_source_game",
        "witcher_source_game",
    ):
        try:
            value = str(actor_obj.get(prop_name, "") or "").strip()
        except Exception:
            value = ""
        if value:
            value_l = value.lower()
            if value_l == "w2":
                return "w2"
            if value_l == "redkit":
                return "redkit"
            return "w3"
    rig_settings = getattr(getattr(actor_obj, "data", None), "witcherui_RigSettings", None)
    value = str(getattr(rig_settings, "source_game", "") or "").strip() if rig_settings else ""
    if value:
        return "w2" if value.lower() == "w2" else "w3"
    return str(fallback or "")


def _find_loaded_cutscene_actor_entry_for_object(scene, actor_obj):
    if scene is None or actor_obj is None:
        return None
    object_name = str(getattr(actor_obj, "name", "") or "")
    try:
        source_index = _coerce_cutscene_index(actor_obj.get(import_cutscene.CUTSCENE_SOURCE_INDEX_PROP, -1))
    except Exception:
        source_index = -1
    actor_name = str(actor_obj.get("cutscene_actor_name", "") or "").strip()
    for item in getattr(scene, "witcher_cutscene_actor_items", []):
        if str(getattr(item, "object_name", "") or "") == object_name:
            return item
        if source_index >= 0 and _coerce_cutscene_index(getattr(item, "source_index", -2), default=-2) == source_index:
            return item
        if actor_name and str(getattr(item, "actor_name", "") or "").strip() == actor_name:
            return item
    return None


def _same_filesystem_path(path_a, path_b):
    path_a = str(path_a or "").strip()
    path_b = str(path_b or "").strip()
    if not path_a or not path_b:
        return False
    try:
        return os.path.normcase(os.path.normpath(path_a)) == os.path.normcase(os.path.normpath(path_b))
    except Exception:
        return path_a == path_b

def _animation_matches_actor_entry(scene, animation_entry, actor_entry):
    if animation_entry is None or actor_entry is None:
        return False
    actor_name = str(getattr(actor_entry, "actor_name", "") or "").strip()
    animation_actor_name = str(getattr(animation_entry, "actor_name", "") or "").strip()
    if actor_name and animation_actor_name:
        return actor_name == animation_actor_name
    if animation_actor_name and not actor_name:
        return False
    actor_entries = list(getattr(scene, "witcher_cutscene_actor_items", []))
    if len(actor_entries) == 1:
        return actor_entries[0].source_index == actor_entry.source_index
    return False

def _find_actor_entry_for_animation(scene, animation_entry):
    if animation_entry is None:
        return None
    animation_actor_name = str(getattr(animation_entry, "actor_name", "") or "").strip()
    if animation_actor_name:
        for actor_entry in getattr(scene, "witcher_cutscene_actor_items", []):
            if str(getattr(actor_entry, "actor_name", "") or "").strip() == animation_actor_name:
                return actor_entry
    actor_entries = list(getattr(scene, "witcher_cutscene_actor_items", []))
    if len(actor_entries) == 1:
        return actor_entries[0]
    return None

def _validate_loaded_cutscene_state(scene):
    filepath = str(getattr(scene, "witcher_loaded_w2cutscene_path", "") or "").strip()
    for actor_entry in getattr(scene, "witcher_cutscene_actor_items", []):
        actor_obj = _get_loaded_cutscene_actor_object(actor_entry)
        if actor_obj is None:
            actor_entry.is_loaded = False
            actor_entry.object_name = ""
            actor_entry.cutscene_guid = ""
            actor_entry.imported_by_cutscene = False
        else:
            actor_entry.is_loaded = True
            if not actor_entry.cutscene_guid:
                actor_entry.cutscene_guid = str(actor_obj.get(import_cutscene.CUTSCENE_GUID_PROP, "") or "")
            actor_entry.imported_by_cutscene = bool(actor_obj.get(import_cutscene.CUTSCENE_ACTOR_IMPORTED_PROP, False))
            actor_entry.source_game = _loaded_cutscene_actor_source_game(actor_obj, fallback=getattr(actor_entry, "source_game", ""))

    if not filepath:
        return

    for animation_entry in getattr(scene, "witcher_cutscene_animation_items", []):
        if not bool(getattr(animation_entry, "file_backed", False)):
            continue
        if not animation_entry.is_loaded:
            continue
        actor_entry = _find_actor_entry_for_animation(scene, animation_entry)
        actor_obj = _get_loaded_cutscene_actor_object(actor_entry)
        if actor_obj is None:
            animation_entry.is_loaded = False
            continue
        if not import_cutscene.is_cutscene_animation_loaded(
            actor_obj,
            animation_entry.full_name,
            filepath,
            animation_entry.source_index,
        ):
            animation_entry.is_loaded = False

def _get_cutscene_actor_display_state(actor_entry):
    actor_obj = _get_loaded_cutscene_actor_object(actor_entry)
    is_loaded = actor_obj is not None
    imported_by_cutscene = bool(getattr(actor_entry, "imported_by_cutscene", False))
    cutscene_guid = str(getattr(actor_entry, "cutscene_guid", "") or "")
    if actor_obj is not None:
        imported_by_cutscene = bool(actor_obj.get(import_cutscene.CUTSCENE_ACTOR_IMPORTED_PROP, False))
        cutscene_guid = str(actor_obj.get(import_cutscene.CUTSCENE_GUID_PROP, "") or cutscene_guid)
    return {
        "actor_obj": actor_obj,
        "is_loaded": is_loaded,
        "imported_by_cutscene": imported_by_cutscene,
        "cutscene_guid": cutscene_guid,
    }

def _get_cutscene_animation_display_state(scene, animation_entry):
    actor_entry = _find_actor_entry_for_animation(scene, animation_entry)
    actor_state = _get_cutscene_actor_display_state(actor_entry)
    actor_obj = actor_state["actor_obj"]
    is_loaded = False
    if actor_obj is not None and bool(getattr(animation_entry, "file_backed", False)):
        is_loaded = import_cutscene.is_cutscene_animation_loaded(
            actor_obj,
            animation_entry.full_name,
            getattr(scene, "witcher_loaded_w2cutscene_path", ""),
            animation_entry.source_index,
        )
    elif not bool(getattr(animation_entry, "file_backed", False)):
        is_loaded = _coerce_cutscene_index(getattr(animation_entry, "source_index", -1)) in _clip_groups(scene)
    elif bool(getattr(animation_entry, "is_loaded", False)):
        is_loaded = False
    return {
        "actor_entry": actor_entry,
        "actor_state": actor_state,
        "is_loaded": is_loaded,
    }

_ANIMATION_ROW_FIELDS = (
    "source_index", "file_backed", "full_name", "display_name", "actor_name", "component_name",
    "frames_per_second", "num_frames", "duration", "is_loaded", "muted", "track_muted", "has_prebake",
)
_EVENT_ROW_FIELDS = (
    "event_type", "event_name", "start_time", "duration", "animation_name", "track_name",
    "effect_name", "appearance", "always_fires_end", "event_scope", "source_index",
)
_DIALOGUE_ROW_FIELDS = (
    "speaker", "text", "start_frame", "end_frame", "tier", "game_line_id",
    "game_voice_file_name", "wav_path", "allocated_line_id", "lipsync_ref",
)


def _snapshot_property_group(item, fields):
    return {field: getattr(item, field) for field in fields}


def _restore_property_group(item, data):
    for field, value in data.items():
        setattr(item, field, value)


def _sync_loaded_cutscene_state(scene, filepath, cutscene_data=None):
    filepath = str(filepath or "").strip()
    if not filepath:
        _clear_loaded_cutscene_state(scene)
        return
    if cutscene_data is not None and hasattr(scene, "witcher_cutscene_last_import_seconds"):
        try:
            scene.witcher_cutscene_last_import_seconds = float(getattr(cutscene_data, "import_duration_seconds", 0.0) or 0.0)
            log.info("Cutscene import took %.2fs", scene.witcher_cutscene_last_import_seconds)
        except Exception:
            pass

    migrate_cutscene_clip_identity(scene)
    authored_rows = [
        _snapshot_property_group(item, _ANIMATION_ROW_FIELDS)
        for item in getattr(scene, "witcher_cutscene_animation_items", [])
        if not bool(getattr(item, "file_backed", False))
        and _coerce_cutscene_index(getattr(item, "source_index", -1)) >= AUTHORED_CLIP_ID_BASE
    ]
    authored_ids = {int(item["source_index"]) for item in authored_rows}
    authored_events = [
        _snapshot_property_group(item, _EVENT_ROW_FIELDS)
        for item in getattr(scene, "witcher_cutscene_event_items", [])
        if str(getattr(item, "event_scope", "") or "").upper() == "ENTRY"
        and _coerce_cutscene_index(getattr(item, "source_index", -1)) in authored_ids
    ]

    prior_path = str(
        getattr(cutscene_data, "previous_loaded_cutscene_path", "")
        or getattr(scene, "witcher_loaded_w2cutscene_path", "")
        or ""
    )
    same_path = os.path.normcase(os.path.normpath(prior_path)) == os.path.normcase(os.path.normpath(filepath))
    authored_dialog_rows = [
        _snapshot_property_group(item, _DIALOGUE_ROW_FIELDS)
        for item in getattr(scene, "witcher_cutscene_dialog_lines", [])
    ] if same_path else []
    authored_dialog_index = int(getattr(scene, "witcher_cutscene_dialog_line_index", 0) or 0)
    if prior_path and not same_path:
        seen_armatures = set()
        for actor_obj in cutscene_bake.iter_cutscene_actor_armatures(scene):
            pointer = actor_obj.as_pointer()
            if pointer in seen_armatures:
                continue
            seen_armatures.add(pointer)
            import_cutscene.clear_cutscene_actor_animation_tracks(actor_obj, source_path=prior_path)
    old_actor_state = {}
    old_animation_state = {}
    if same_path:
        old_file_mute_state = _file_clip_mute_state(scene, prior_path)
        old_actor_state = {
            int(item.source_index): {
                "object_name": str(item.object_name or ""),
                "cutscene_guid": str(item.cutscene_guid or ""),
                "is_loaded": bool(item.is_loaded),
                "imported_by_cutscene": bool(item.imported_by_cutscene),
                "source_game": str(getattr(item, "source_game", "") or ""),
            }
            for item in getattr(scene, "witcher_cutscene_actor_items", [])
        }
        old_animation_state = {
            int(item.source_index): {
                "is_loaded": bool(item.is_loaded),
                "muted": bool(old_file_mute_state.get(int(item.source_index), item.muted)),
            }
            for item in getattr(scene, "witcher_cutscene_animation_items", [])
            if bool(getattr(item, "file_backed", False))
        }

    _cutscene, actor_items, animation_items, event_items = import_cutscene.collect_cutscene_preview(
        filepath,
        cutscene_template=cutscene_data,
    )

    _sync_cutscene_template_fields(scene, _cutscene)
    _sync_cutscene_burned_audio_state(scene, filepath, _cutscene, cutscene_data=cutscene_data)
    _sync_cutscene_export_metadata_state(scene, _cutscene)
    scene.witcher_cutscene_effect_items.clear()
    if _cutscene is not None:
        for eff in (getattr(_cutscene, "effects", None) or []):
            ei = scene.witcher_cutscene_effect_items.add()
            if isinstance(eff, dict):
                ei.name = str(eff.get("name") or eff.get("Name") or eff.get("$type", "CFXDefinition"))
            elif hasattr(eff, "name") and eff.name:
                ei.name = str(eff.name)
            else:
                ei.name = "CFXDefinition"

    loaded_actor_object_names = dict(getattr(cutscene_data, "loaded_actor_object_names_by_index", {}) or {})
    loaded_actor_imported_flags = dict(getattr(cutscene_data, "loaded_actor_imported_flags_by_index", {}) or {})
    loaded_actor_guid_by_index = dict(getattr(cutscene_data, "loaded_actor_guid_by_index", {}) or {})
    applied_animation_indices = {
        int(idx)
        for idx in (getattr(cutscene_data, "applied_animation_indices", []) or [])
    }

    if hasattr(bpy.types.Scene, "witcher_cs_event_target"):
        scene.witcher_cs_event_target = "ROOT"
    scene.witcher_cutscene_actor_items.clear()
    scene.witcher_cutscene_animation_items.clear()
    scene.witcher_cutscene_event_items.clear()
    scene.witcher_cutscene_dialog_items.clear()
    if hasattr(scene, "witcher_cutscene_dialog_lines"):
        scene.witcher_cutscene_dialog_lines.clear()
        scene.witcher_cutscene_dialog_line_index = 0
    scene.witcher_loaded_cutscene_name = _get_loaded_cutscene_name(filepath)
    scene.witcher_loaded_w2cutscene_path = filepath

    for actor_data in actor_items:
        source_index = int(actor_data["source_index"])
        state = dict(old_actor_state.get(source_index, {}))
        item = scene.witcher_cutscene_actor_items.add()
        item.source_index = source_index
        item.label = str(actor_data["label"])
        item.actor_name = str(actor_data["actor_name"])
        item.tag = str(actor_data.get("tag", "") or "")
        item.voice_tag = str(actor_data.get("voice_tag", "") or "")
        item.template_path = str(actor_data["template_path"])
        item.source_game = str(state.get("source_game", "") or actor_data.get("source_game", "") or "")
        item.appearance_name = str(actor_data["appearance_name"])
        item.actor_type = str(actor_data["actor_type"])
        item.final_position = str(actor_data.get("final_position", "") or "")
        item.kill_me = bool(actor_data.get("kill_me", False))
        item.use_mimic = bool(actor_data["use_mimic"])
        item.anim_final_pos = str(actor_data.get("anim_final_pos", "") or "")
        item.object_name = str(state.get("object_name", "") or "")
        item.cutscene_guid = str(state.get("cutscene_guid", "") or "")
        item.is_loaded = bool(state.get("is_loaded", False))
        item.imported_by_cutscene = bool(state.get("imported_by_cutscene", False))
        if source_index in loaded_actor_object_names:
            item.object_name = str(loaded_actor_object_names[source_index] or "")
            item.cutscene_guid = str(loaded_actor_guid_by_index.get(source_index, "") or "")
            item.is_loaded = bool(item.object_name)
            item.imported_by_cutscene = bool(loaded_actor_imported_flags.get(source_index, False))

    for animation_data in animation_items:
        source_index = int(animation_data["source_index"])
        state = dict(old_animation_state.get(source_index, {}))
        item = scene.witcher_cutscene_animation_items.add()
        item.source_index = source_index
        item.file_backed = True
        item.full_name = str(animation_data["full_name"])
        item.display_name = str(animation_data["display_name"])
        item.actor_name = str(animation_data["actor_name"])
        item.component_name = str(animation_data["component_name"])
        item.frames_per_second = float(animation_data["frames_per_second"])
        item.num_frames = int(animation_data["num_frames"])
        item.duration = float(animation_data["duration"])
        item.is_loaded = bool(state.get("is_loaded", False))
        item.muted = bool(state.get("muted", False))
        if source_index in applied_animation_indices:
            item.is_loaded = True

    for event_data in event_items:
        item = scene.witcher_cutscene_event_items.add()
        item.event_type = str(event_data["event_type"])
        item.event_name = str(event_data["event_name"])
        item.start_time = float(event_data["start_time"])
        item.duration = float(event_data["duration"])
        item.animation_name = str(event_data["animation_name"])
        item.track_name = str(event_data["track_name"])
        item.effect_name = str(event_data["effect_name"])
        item.appearance = str(event_data.get("appearance", "") or "")
        item.event_scope = str(event_data.get("event_scope", "ROOT"))
        item.source_index = int(event_data.get("source_index", -1))

    for row_data in authored_rows:
        item = scene.witcher_cutscene_animation_items.add()
        _restore_property_group(item, row_data)
    for event_data in authored_events:
        item = scene.witcher_cutscene_event_items.add()
        _restore_property_group(item, event_data)
    for row_data in authored_dialog_rows:
        item = scene.witcher_cutscene_dialog_lines.add()
        _restore_property_group(item, row_data)
    if authored_dialog_rows:
        scene.witcher_cutscene_dialog_line_index = min(
            authored_dialog_index, len(authored_dialog_rows) - 1,
        )
        sync_authored_cutscene_dialog_items(scene)

    if same_path:
        _apply_file_clip_mute_state(scene, filepath, {
            source_index: bool(state.get("muted", False))
            for source_index, state in old_animation_state.items()
        })
    _validate_loaded_cutscene_state(scene)
    sync_animation_items_from_scene(scene)

def _update_loaded_actor_entry_from_result(actor_entry, actor_info):
    if actor_entry is None or not actor_info:
        return
    actor_obj = actor_info.get("actor_obj")
    actor_entry.object_name = str(getattr(actor_obj, "name", "") or "")
    actor_entry.cutscene_guid = str(actor_info.get("cutscene_guid", "") or "")
    actor_entry.is_loaded = bool(actor_obj)
    actor_entry.imported_by_cutscene = bool(actor_info.get("imported_new", False))
    actor_entry.template_path = str(actor_info.get("template_path", "") or getattr(actor_entry, "template_path", ""))
    actor_entry.appearance_name = str(actor_info.get("appearance_name", "") or getattr(actor_entry, "appearance_name", ""))
    actor_entry.source_game = str(actor_info.get("source_game", "") or _loaded_cutscene_actor_source_game(actor_obj, fallback=getattr(actor_entry, "source_game", "")))

def _load_cutscene_actor_entry(scene, actor_entry):
    filepath = str(getattr(scene, "witcher_loaded_w2cutscene_path", "") or "").strip()
    if not filepath or actor_entry is None:
        return None
    actor_info = import_cutscene.load_cutscene_actor(filepath, actor_entry.source_index)
    _update_loaded_actor_entry_from_result(actor_entry, actor_info)
    return actor_info.get("actor_obj") if actor_info else None

def _rebuild_cutscene_actor_animations(scene, actor_entry):
    if actor_entry is None:
        return set(), {}
    actor_obj = _get_loaded_cutscene_actor_object(actor_entry)
    if actor_obj is None:
        for animation_entry in getattr(scene, "witcher_cutscene_animation_items", []):
            if _animation_matches_actor_entry(scene, animation_entry, actor_entry):
                animation_entry.is_loaded = False
        return set(), {}

    filepath = str(getattr(scene, "witcher_loaded_w2cutscene_path", "") or "").strip()
    if not filepath:
        return set(), {}
    actor_name = str(getattr(actor_entry, "actor_name", "") or "")
    mute_state = _file_clip_mute_state(scene, filepath, actor_name)

    animation_indices = [
        int(animation_entry.source_index)
        for animation_entry in getattr(scene, "witcher_cutscene_animation_items", [])
        if bool(getattr(animation_entry, "file_backed", False))
        and bool(getattr(animation_entry, "is_loaded", False))
        and _animation_matches_actor_entry(scene, animation_entry, actor_entry)
    ]

    import_cutscene.clear_cutscene_actor_animation_tracks(actor_obj, source_path=filepath)
    if not animation_indices:
        sync_animation_items_from_scene(scene)
        return set(), {}

    applied_indices, error_messages = import_cutscene.apply_cutscene_animation_sequence(
        filepath,
        animation_indices,
        actor_obj,
        actor_name=actor_name,
        track_name=import_cutscene.CUTSCENE_FILE_TRACK_NAME,
        return_errors=True,
    )
    for animation_entry in getattr(scene, "witcher_cutscene_animation_items", []):
        if bool(getattr(animation_entry, "file_backed", False)) and _animation_matches_actor_entry(scene, animation_entry, actor_entry):
            animation_entry.is_loaded = int(animation_entry.source_index) in applied_indices
    _apply_file_clip_mute_state(scene, filepath, mute_state)
    sync_animation_items_from_scene(scene)
    return applied_indices, error_messages


def _find_loaded_actor_entry_index(scene, source_index=-1, object_name="", actor_name=""):
    object_name = str(object_name or "").strip()
    actor_name = str(actor_name or "").strip()
    try:
        source_index = int(source_index)
    except Exception:
        source_index = -1
    entries = list(getattr(scene, "witcher_cutscene_actor_items", []) or [])
    if object_name:
        for idx, entry in enumerate(entries):
            if str(getattr(entry, "object_name", "") or "").strip() == object_name:
                return idx
    if source_index >= 0:
        for idx, entry in enumerate(entries):
            if int(getattr(entry, "source_index", -1)) == source_index:
                return idx
    if actor_name:
        for idx, entry in enumerate(entries):
            if str(getattr(entry, "actor_name", "") or "").strip() == actor_name:
                return idx
    return -1


def _find_loaded_actor_entry(scene, source_index=-1, object_name="", actor_name=""):
    items = getattr(scene, "witcher_cutscene_actor_items", None)
    idx = _find_loaded_actor_entry_index(scene, source_index, object_name, actor_name)
    if items is not None and idx >= 0:
        return items[idx]
    return None


def _clear_loaded_actor_animation_flags(scene, actor_entry):
    if actor_entry is None:
        return
    for animation_entry in getattr(scene, "witcher_cutscene_animation_items", []):
        if _animation_matches_actor_entry(scene, animation_entry, actor_entry):
            animation_entry.is_loaded = False


def _is_cutscene_face_animation_entry(animation_entry):
    component_name = str(getattr(animation_entry, "component_name", "") or "").strip().lower()
    if component_name == "face":
        return True
    full_name = str(getattr(animation_entry, "full_name", "") or "")
    return import_cutscene._is_face_cutscene_animation(full_name)


def _actor_animation_entries_for_layer(scene, actor_entry, layer="ALL"):
    layer = str(layer or "ALL").upper()
    entries = []
    for animation_entry in getattr(scene, "witcher_cutscene_animation_items", []):
        if not bool(getattr(animation_entry, "file_backed", False)):
            continue
        if not _animation_matches_actor_entry(scene, animation_entry, actor_entry):
            continue
        is_face = _is_cutscene_face_animation_entry(animation_entry)
        if layer == "FACE" and not is_face:
            continue
        if layer == "ROOT" and is_face:
            continue
        entries.append(animation_entry)
    return entries


def _actor_animation_layer_state(scene, actor_entry, layer="ALL"):
    entries = _actor_animation_entries_for_layer(scene, actor_entry, layer=layer)
    loaded_count = 0
    for animation_entry in entries:
        if _get_cutscene_animation_display_state(scene, animation_entry)["is_loaded"]:
            loaded_count += 1
    return loaded_count, len(entries)


def _actor_metadata_from_object(obj):
    def _get(name, default=""):
        try:
            return str(obj.get(name, default) or "").strip()
        except Exception:
            return str(default)

    return {
        "actor_name": _get("cutscene_actor_name"),
        "tag": _get("cutscene_actor_tag"),
        "voice_tag": _get("cutscene_actor_voice_tag"),
        "template_path": _get("cutscene_actor_template"),
        "appearance_name": _get("cutscene_actor_appearance"),
        "actor_type": _get("cutscene_actor_type", "CAT_Actor"),
        "final_position": _get("cutscene_actor_final_position"),
        "anim_final_pos": _get("cutscene_actor_anim_final_pos"),
        "source_game": _loaded_cutscene_actor_source_game(obj, fallback=""),
        "kill_me": bool(obj.get("cutscene_actor_kill_me", False)),
        "use_mimic": bool(obj.get("cutscene_actor_use_mimic", False)),
    }


def _sync_actor_items_with_scene(scene):
    items = getattr(scene, "witcher_cutscene_actor_items", None)
    if items is None:
        return 0
    existing_object_names = {
        str(getattr(item, "object_name", "") or "").strip()
        for item in items
        if str(getattr(item, "object_name", "") or "").strip()
    }
    existing_actor_names = {
        str(getattr(item, "actor_name", "") or "").strip().lower()
        for item in items
        if str(getattr(item, "actor_name", "") or "").strip()
    }
    added = 0
    for obj in scene.objects:
        if getattr(obj, "type", None) != 'ARMATURE':
            continue
        obj_name = str(getattr(obj, "name", "") or "")
        actor_name = str(obj.get("cutscene_actor_name", "") or "").strip()
        if not actor_name:
            continue
        _ensure_actor_custom_props(obj)
        if obj_name in existing_object_names:
            continue
        # An unloaded imported-file entry may already hold this actor name; don't duplicate.
        if actor_name.lower() in existing_actor_names:
            entry = _find_loaded_actor_entry(scene, actor_name=actor_name)
            if entry is not None:
                entry.object_name = obj_name
                entry.is_loaded = True
                entry.cutscene_guid = str(obj.get(import_cutscene.CUTSCENE_GUID_PROP, "") or "")
                entry.imported_by_cutscene = bool(obj.get(import_cutscene.CUTSCENE_ACTOR_IMPORTED_PROP, False))
            continue
        meta = _actor_metadata_from_object(obj)
        item = items.add()
        item.source_index = -1
        item.label = meta["actor_name"]
        item.actor_name = meta["actor_name"]
        item.tag = meta["tag"]
        item.voice_tag = meta["voice_tag"]
        item.template_path = meta["template_path"]
        item.source_game = meta["source_game"]
        item.appearance_name = meta["appearance_name"]
        item.actor_type = meta["actor_type"]
        item.final_position = meta["final_position"]
        item.kill_me = meta["kill_me"]
        item.use_mimic = meta["use_mimic"]
        item.anim_final_pos = meta["anim_final_pos"]
        item.object_name = obj_name
        item.cutscene_guid = str(obj.get(import_cutscene.CUTSCENE_GUID_PROP, "") or "")
        item.is_loaded = True
        item.imported_by_cutscene = bool(obj.get(import_cutscene.CUTSCENE_ACTOR_IMPORTED_PROP, False))
        existing_object_names.add(obj_name)
        existing_actor_names.add(actor_name.lower())
        added += 1
    for i in range(len(items) - 1, -1, -1):
        obj_name = str(getattr(items[i], "object_name", "") or "").strip()
        if not obj_name or bpy.data.objects.get(obj_name) is not None:
            continue
        if int(getattr(items[i], "source_index", -1)) >= 0:
            items[i].object_name = ""  # restorable from the file: keep the row as "not loaded"
            items[i].is_loaded = False
        else:
            items.remove(i)
    if hasattr(scene, "witcher_cutscene_loaded_actor_index") and scene.witcher_cutscene_loaded_actor_index >= len(items):
        scene.witcher_cutscene_loaded_actor_index = max(0, len(items) - 1)
    return added


_ACTOR_SYNC_DEFERRED = set()


def _scene_needs_actor_sync(scene):
    items = getattr(scene, "witcher_cutscene_actor_items", None)
    if items is None:
        return False
    known_objects = {
        str(getattr(i, "object_name", "") or "")
        for i in items if str(getattr(i, "object_name", "") or "")
    }
    known_names = {
        str(getattr(i, "actor_name", "") or "").strip().lower()
        for i in items if str(getattr(i, "actor_name", "") or "").strip()
    }
    for obj in scene.objects:
        if getattr(obj, "type", None) != 'ARMATURE':
            continue
        actor_name = str(obj.get("cutscene_actor_name", "") or "").strip()
        if not actor_name:
            continue
        if obj.name in known_objects or actor_name.lower() in known_names:
            continue
        return True
    return any(name and bpy.data.objects.get(name) is None for name in known_objects)


def _schedule_actor_items_sync(scene):
    """Run _sync_actor_items_with_scene on a timer so it never mutates during draw."""
    scene_name = str(getattr(scene, "name", "") or "")
    if not scene_name or scene_name in _ACTOR_SYNC_DEFERRED:
        return
    _ACTOR_SYNC_DEFERRED.add(scene_name)

    def _do_sync():
        _ACTOR_SYNC_DEFERRED.discard(scene_name)
        target = bpy.data.scenes.get(scene_name)
        if target is None:
            return
        try:
            _sync_actor_items_with_scene(target)
        except Exception:
            log.debug("Deferred actor-item sync failed.", exc_info=True)

    try:
        bpy.app.timers.register(_do_sync, first_interval=0.0)
    except Exception:
        _ACTOR_SYNC_DEFERRED.discard(scene_name)


def _set_authored_clip_sequence_floor(scene, source_index):
    source_index = _coerce_cutscene_index(source_index)
    if scene is None or source_index < AUTHORED_CLIP_ID_BASE:
        return
    sequence = source_index - AUTHORED_CLIP_ID_BASE
    current = _coerce_cutscene_index(scene.get(AUTHORED_CLIP_SEQUENCE_PROP, 0), default=0)
    if sequence > current:
        scene[AUTHORED_CLIP_SEQUENCE_PROP] = sequence


def allocate_authored_clip_id(scene):
    """Allocate a persistent, scene-local clip id that can never alias a file array index."""
    if scene is None:
        raise ValueError("A Scene is required to allocate an authored clip id")
    floor = max(0, _coerce_cutscene_index(scene.get(AUTHORED_CLIP_SEQUENCE_PROP, 0), default=0))
    for item in getattr(scene, "witcher_cutscene_animation_items", []) or []:
        value = _coerce_cutscene_index(getattr(item, "source_index", -1))
        if value >= AUTHORED_CLIP_ID_BASE:
            floor = max(floor, value - AUTHORED_CLIP_ID_BASE)
    sequence = floor + 1
    scene[AUTHORED_CLIP_SEQUENCE_PROP] = sequence
    return AUTHORED_CLIP_ID_BASE + sequence


def _clip_groups(scene):
    filepath = str(getattr(scene, "witcher_loaded_w2cutscene_path", "") or "").strip()
    identity_migrated = (
        _coerce_cutscene_index(scene.get(CLIP_IDENTITY_SCHEMA_PROP, 0), default=0)
        >= CLIP_IDENTITY_SCHEMA_VERSION
    )
    rows_by_id = {
        _coerce_cutscene_index(getattr(item, "source_index", -1)): item
        for item in getattr(scene, "witcher_cutscene_animation_items", []) or []
        if _coerce_cutscene_index(getattr(item, "source_index", -1)) >= 0
    }
    groups = {}
    for arm in cutscene_bake.iter_cutscene_actor_armatures(scene):
        if arm.get(cutscene_bake.PROP_RIG_TAG):
            continue
        actor_name = str(arm.get("cutscene_actor_name", "") or "").strip()
        for holder in import_cutscene._iter_cutscene_related_armatures(arm):
            ad = holder.animation_data
            for track in (ad.nla_tracks if ad else []):
                if not export_cutscene._is_cutscene_track_name(track.name):
                    continue
                prebake = cutscene_bake.BAKE_BACKUP_SUFFIX in track.name
                base = track.name.replace(cutscene_bake.BAKE_BACKUP_SUFFIX, "")
                component = "face" if base.startswith(export_cutscene.CUTSCENE_FACE_TRACK_NAME) else "Root"
                for strip in track.strips:
                    action = strip.action
                    if action is None or action.get(cutscene_bake.BAKED_ACTION_TAG):
                        continue
                    source_index = _coerce_cutscene_index(
                        action.get(export_cutscene.CUTSCENE_SOURCE_INDEX_PROP, -1)
                    )
                    full_name = str(action.get(export_cutscene.CUTSCENE_ANIMATION_NAME_PROP, "") or "").strip()
                    row = rows_by_id.get(source_index)
                    if not full_name and row is not None:
                        full_name = str(getattr(row, "full_name", "") or "").strip()
                    if not full_name:
                        full_name = export_anims._compose_cutscene_animation_name(actor_name, component, strip.name)
                    source_path = str(action.get(export_cutscene.CUTSCENE_SOURCE_PATH_PROP, "") or "").strip()
                    if (
                        prebake
                        and filepath
                        and source_path
                        and not _same_filesystem_path(source_path, filepath)
                    ):
                        continue
                    if (
                        prebake
                        and identity_migrated
                        and filepath
                        and source_path
                        and _same_filesystem_path(source_path, filepath)
                        and (
                            row is None
                            or (
                                str(getattr(row, "full_name", "") or "")
                                and full_name != str(getattr(row, "full_name", "") or "")
                            )
                        )
                    ):
                        continue
                    key = source_index if source_index >= 0 else ("legacy", full_name)
                    group = groups.setdefault(key, {
                        "source_index": source_index,
                        "full_name": full_name,
                        "source_path": source_path,
                        "file_backed": False,
                        "actor": actor_name,
                        "component": component,
                        "strips": [],
                        "muted": True,
                        "track_muted": False,
                        "has_prebake": False,
                    })
                    if not group["source_path"] and source_path:
                        group["source_path"] = source_path
                    if filepath and source_path and _same_filesystem_path(source_path, filepath):
                        group["file_backed"] = True
                    group["strips"].append((track, strip))
                    group["track_muted"] = group["track_muted"] or bool(track.mute)
                    group["has_prebake"] = group["has_prebake"] or prebake
    for group in groups.values():
        live_strips = [
            strip for track, strip in group["strips"]
            if cutscene_bake.BAKE_BACKUP_SUFFIX not in str(getattr(track, "name", "") or "")
        ]
        group["muted"] = all(bool(strip.mute) for strip in (live_strips or [s for _t, s in group["strips"]]))
    return groups


def _file_clip_mute_state(scene, filepath, actor_name=""):
    if not _same_filesystem_path(getattr(scene, "witcher_loaded_w2cutscene_path", ""), filepath):
        return {}
    groups = _clip_groups(scene)
    state = {}
    for item in getattr(scene, "witcher_cutscene_animation_items", []) or []:
        if not bool(getattr(item, "file_backed", False)):
            continue
        if actor_name and str(getattr(item, "actor_name", "") or "") != actor_name:
            continue
        source_index = _coerce_cutscene_index(getattr(item, "source_index", -1))
        if source_index < 0:
            continue
        group = groups.get(source_index)
        live_strips = [
            strip
            for track, strip in list((group or {}).get("strips", []) or [])
            if cutscene_bake.BAKE_BACKUP_SUFFIX not in str(getattr(track, "name", "") or "")
            and getattr(strip, "action", None) is not None
            and _same_filesystem_path(
                strip.action.get(export_cutscene.CUTSCENE_SOURCE_PATH_PROP, ""),
                filepath,
            )
        ]
        state[source_index] = (
            all(bool(strip.mute) for strip in live_strips)
            if live_strips
            else bool(getattr(item, "muted", False))
        )
    return state


def _apply_file_clip_mute_state(scene, filepath, state):
    if not filepath or not state:
        return
    groups = _clip_groups(scene)
    for source_index, muted in state.items():
        group = groups.get(int(source_index))
        for track, strip in list((group or {}).get("strips", []) or []):
            action = getattr(strip, "action", None)
            if (
                cutscene_bake.BAKE_BACKUP_SUFFIX in str(getattr(track, "name", "") or "")
                or action is None
                or not _same_filesystem_path(
                    action.get(export_cutscene.CUTSCENE_SOURCE_PATH_PROP, ""),
                    filepath,
                )
            ):
                continue
            strip.mute = bool(muted)


def _retag_clip_group(group, source_index, *, source_path=""):
    seen = set()
    for _track, strip in list(group.get("strips", []) or []):
        action = getattr(strip, "action", None)
        if action is None or action.as_pointer() in seen:
            continue
        seen.add(action.as_pointer())
        action[export_cutscene.CUTSCENE_SOURCE_INDEX_PROP] = int(source_index)
        action[export_cutscene.CUTSCENE_SOURCE_PATH_PROP] = str(source_path or "")
        if group.get("full_name"):
            action[export_cutscene.CUTSCENE_ANIMATION_NAME_PROP] = str(group["full_name"])
    group["source_index"] = int(source_index)
    group["source_path"] = str(source_path or "")
    group["file_backed"] = bool(source_path)


def _file_animation_names_by_index(scene):
    filepath = str(getattr(scene, "witcher_loaded_w2cutscene_path", "") or "").strip()
    if not filepath or not os.path.isfile(filepath):
        return None
    try:
        _cutscene, _actors, animations, _events = import_cutscene.collect_cutscene_preview(filepath)
    except Exception:
        log.debug("Could not inspect loaded cutscene for clip migration: %s", filepath, exc_info=True)
        return None
    return {
        int(item.get("source_index", -1)): str(item.get("full_name", "") or "")
        for item in animations
        if int(item.get("source_index", -1)) >= 0
    }


def _row_matches_file_preview(item, file_names):
    if file_names is None:
        return False
    source_index = _coerce_cutscene_index(getattr(item, "source_index", -1))
    if source_index not in file_names:
        return False
    row_name = str(getattr(item, "full_name", "") or "")
    return not row_name or row_name == file_names[source_index]


def _group_matches_file_preview(group, file_names):
    if file_names is None:
        return False
    source_index = _coerce_cutscene_index(group.get("source_index", -1))
    if source_index not in file_names:
        return False
    group_name = str(group.get("full_name", "") or "")
    return not group_name or group_name == file_names[source_index]


def _prune_stale_file_animation_strips(scene, file_names):
    filepath = str(getattr(scene, "witcher_loaded_w2cutscene_path", "") or "").strip()
    if not filepath or file_names is None:
        return 0
    removed = 0
    seen_holders = set()
    for actor_obj in cutscene_bake.iter_cutscene_actor_armatures(scene):
        for holder in import_cutscene._iter_cutscene_related_armatures(actor_obj):
            pointer = holder.as_pointer()
            if pointer in seen_holders:
                continue
            seen_holders.add(pointer)
            anim_data = getattr(holder, "animation_data", None)
            if anim_data is None:
                continue
            for track in list(anim_data.nla_tracks):
                track_name = str(getattr(track, "name", "") or "")
                if not export_cutscene._is_cutscene_track_name(track_name) or cutscene_bake.BAKE_BACKUP_SUFFIX in track_name:
                    continue
                for strip in list(track.strips):
                    action = getattr(strip, "action", None)
                    if action is None or not _same_filesystem_path(
                        action.get(export_cutscene.CUTSCENE_SOURCE_PATH_PROP, ""),
                        filepath,
                    ):
                        continue
                    source_index = _coerce_cutscene_index(
                        action.get(export_cutscene.CUTSCENE_SOURCE_INDEX_PROP, -1)
                    )
                    action_name = str(
                        action.get(export_cutscene.CUTSCENE_ANIMATION_NAME_PROP, "") or ""
                    )
                    expected_name = file_names.get(source_index)
                    if expected_name is not None and (not action_name or action_name == expected_name):
                        continue
                    track.strips.remove(strip)
                    removed += 1
                if len(track.strips) == 0:
                    anim_data.nla_tracks.remove(track)
    return removed


def _rewrite_entry_event_source(scene, old_source_index, new_source_index):
    old_source_index = _coerce_cutscene_index(old_source_index)
    if old_source_index < 0:
        return 0
    changed = 0
    for event in getattr(scene, "witcher_cutscene_event_items", []) or []:
        if str(getattr(event, "event_scope", "") or "").upper() != "ENTRY":
            continue
        if _coerce_cutscene_index(getattr(event, "source_index", -1)) != old_source_index:
            continue
        event.source_index = int(new_source_index)
        changed += 1
    return changed


def _remove_cutscene_animation_entry(scene, source_index, *, remove_strips=True):
    source_index = _coerce_cutscene_index(source_index)
    if source_index < 0:
        return {"rows": 0, "strips": 0, "events": 0}
    target_removed = _event_target_index(scene) == source_index
    if target_removed:
        scene.witcher_cs_event_target = "ROOT"
    strips_removed = 0
    if remove_strips:
        group = _clip_groups(scene).get(source_index, {})
        for track, strip in list(group.get("strips", []) or []):
            try:
                track.strips.remove(strip)
                strips_removed += 1
            except Exception:
                log.debug("Could not remove strip for clip id %s", source_index, exc_info=True)
    items = getattr(scene, "witcher_cutscene_animation_items", None)
    rows_removed = 0
    if items is not None:
        for index in range(len(items) - 1, -1, -1):
            if _coerce_cutscene_index(getattr(items[index], "source_index", -1)) == source_index:
                items.remove(index)
                rows_removed += 1
    events = getattr(scene, "witcher_cutscene_event_items", None)
    events_removed = 0
    if events is not None:
        for index in range(len(events) - 1, -1, -1):
            event = events[index]
            if (
                str(getattr(event, "event_scope", "") or "").upper() == "ENTRY"
                and _coerce_cutscene_index(getattr(event, "source_index", -1)) == source_index
            ):
                events.remove(index)
                events_removed += 1
    if hasattr(scene, "witcher_cutscene_loaded_anim_index"):
        scene.witcher_cutscene_loaded_anim_index = min(
            max(0, int(scene.witcher_cutscene_loaded_anim_index)),
            max(0, len(items) - 1) if items is not None else 0,
        )
    if hasattr(scene, "witcher_cs_entry_event_idx"):
        scene.witcher_cs_entry_event_idx = min(
            max(0, int(scene.witcher_cs_entry_event_idx)),
            max(0, len(events) - 1) if events is not None else 0,
        )
    return {"rows": rows_removed, "strips": strips_removed, "events": events_removed}


def _update_animation_item_from_group(item, group, fps):
    item.source_index = int(group["source_index"])
    item.file_backed = bool(group.get("file_backed", False))
    item.full_name = str(group.get("full_name", "") or "")
    item.display_name = item.full_name.rsplit(":", 1)[-1]
    item.actor_name = str(group.get("actor", "") or "")
    item.component_name = str(group.get("component", "") or "")
    item.is_loaded = True
    item.muted = bool(group.get("muted", False))
    item.track_muted = bool(group.get("track_muted", False))
    item.has_prebake = bool(group.get("has_prebake", False))
    strips = list(group.get("strips", []) or [])
    if strips:
        start = min(float(strip.frame_start) for _track, strip in strips)
        end = max(float(strip.frame_end) for _track, strip in strips)
        item.num_frames = max(1, int(round(end - start)))
    item.frames_per_second = float(fps or 30.0)
    item.duration = item.num_frames / item.frames_per_second if item.frames_per_second else 0.0


def migrate_cutscene_clip_identity(scene):
    """One-shot V1 row/action migration; called by plain sync, never by panel draw."""
    if scene is None or _coerce_cutscene_index(scene.get(CLIP_IDENTITY_SCHEMA_PROP, 0), default=0) >= CLIP_IDENTITY_SCHEMA_VERSION:
        return False
    items = getattr(scene, "witcher_cutscene_animation_items", None)
    if items is None:
        return False
    file_names = _file_animation_names_by_index(scene)
    groups = _clip_groups(scene)
    claimed_rows = set()
    fps = _scene_fps(scene)

    for group in list(groups.values()):
        source_index = _coerce_cutscene_index(group.get("source_index", -1))
        source_path = str(group.get("source_path", "") or "").strip()
        is_file = bool(group.get("file_backed", False)) and 0 <= source_index < AUTHORED_CLIP_ID_BASE
        row_index = -1
        for index in range(len(items)):
            row = items[index]
            if index in claimed_rows:
                continue
            row_id = _coerce_cutscene_index(getattr(row, "source_index", -1))
            if source_index >= 0 and row_id == source_index:
                row_index = index
                break
        if row_index < 0:
            full_name = str(group.get("full_name", "") or "")
            for index in range(len(items)):
                row = items[index]
                if index in claimed_rows or _row_matches_file_preview(row, file_names):
                    continue
                if str(getattr(row, "full_name", "") or "") == full_name:
                    row_index = index
                    break
        if row_index >= 0 and _row_matches_file_preview(items[row_index], file_names):
            is_file = True
        if source_path and _same_filesystem_path(
            source_path,
            getattr(scene, "witcher_loaded_w2cutscene_path", ""),
        ) and 0 <= source_index < AUTHORED_CLIP_ID_BASE:
            is_file = True

        if is_file:
            file_source_index = (
                _coerce_cutscene_index(getattr(items[row_index], "source_index", -1))
                if row_index >= 0 else source_index
            )
            if file_source_index >= 0:
                _retag_clip_group(
                    group,
                    file_source_index,
                    source_path=getattr(scene, "witcher_loaded_w2cutscene_path", ""),
                )
            if row_index >= 0:
                items[row_index].file_backed = True
                claimed_rows.add(row_index)
            continue

        old_row_id = (
            _coerce_cutscene_index(getattr(items[row_index], "source_index", -1))
            if row_index >= 0 else source_index
        )
        if source_index >= AUTHORED_CLIP_ID_BASE and not source_path:
            authored_id = source_index
        else:
            authored_id = allocate_authored_clip_id(scene)
            _retag_clip_group(group, authored_id, source_path="")
        _set_authored_clip_sequence_floor(scene, authored_id)
        retarget_events = old_row_id >= 0 and _event_target_index(scene) == old_row_id
        if retarget_events:
            scene.witcher_cs_event_target = "ROOT"
        if row_index < 0:
            row_index = len(items)
            items.add()
        _update_animation_item_from_group(items[row_index], group, fps)
        items[row_index].source_index = authored_id
        items[row_index].file_backed = False
        claimed_rows.add(row_index)
        _rewrite_entry_event_source(scene, old_row_id, authored_id)
        if retarget_events:
            scene.witcher_cs_event_target = str(authored_id)

    orphan_ids = []
    for index in range(len(items)):
        if index in claimed_rows:
            continue
        row = items[index]
        if _row_matches_file_preview(row, file_names):
            row.file_backed = True
            continue
        orphan_ids.append(_coerce_cutscene_index(getattr(row, "source_index", -1)))
    for source_index in dict.fromkeys(orphan_ids):
        _remove_cutscene_animation_entry(scene, source_index, remove_strips=False)

    scene[CLIP_IDENTITY_SCHEMA_PROP] = CLIP_IDENTITY_SCHEMA_VERSION
    return True


def _clip_signature(scene, groups=None):
    if groups is None:
        groups = _clip_groups(scene)

    def strip_signature(track, strip):
        action = getattr(strip, "action", None)
        return (
            track.as_pointer(), str(track.name), bool(track.mute),
            strip.as_pointer(), str(strip.name), bool(strip.mute),
            float(strip.frame_start), float(strip.frame_end),
            action.as_pointer() if action is not None else 0,
            str(getattr(action, "name", "") or ""),
            str(action.get(export_cutscene.CUTSCENE_ANIMATION_NAME_PROP, "") or "") if action is not None else "",
            str(action.get(export_cutscene.CUTSCENE_SOURCE_PATH_PROP, "") or "") if action is not None else "",
        )

    return (
        _scene_fps(scene),
        tuple(sorted(
            (
                repr(key),
                _coerce_cutscene_index(group.get("source_index", -1)),
                str(group.get("full_name", "") or ""),
                str(group.get("source_path", "") or ""),
                bool(group.get("file_backed", False)),
                str(group.get("actor", "") or ""),
                str(group.get("component", "") or ""),
                bool(group.get("muted", False)),
                bool(group.get("track_muted", False)),
                bool(group.get("has_prebake", False)),
                tuple(strip_signature(track, strip) for track, strip in group.get("strips", [])),
            )
            for key, group in groups.items()
        ))
    )


_ANIM_SYNC_SIGNATURES = {}
_ANIM_SYNC_DEFERRED = set()


def sync_animation_items_from_scene(scene):
    """Sync stable clip rows; only verified file rows may remain without strips."""
    items = getattr(scene, "witcher_cutscene_animation_items", None)
    if items is None:
        return
    migrate_cutscene_clip_identity(scene)
    file_names = _file_animation_names_by_index(scene)
    _prune_stale_file_animation_strips(scene, file_names)
    groups = _clip_groups(scene)
    filepath = str(getattr(scene, "witcher_loaded_w2cutscene_path", "") or "").strip()
    if file_names is not None and filepath:
        groups = {
            key: group for key, group in groups.items()
            if not (
                0 <= _coerce_cutscene_index(group.get("source_index", -1)) < AUTHORED_CLIP_ID_BASE
                and _same_filesystem_path(group.get("source_path", ""), filepath)
                and not _group_matches_file_preview(group, file_names)
            )
        }
    orphan_ids = []
    fps = _scene_fps(scene)
    for i in range(len(items) - 1, -1, -1):
        if int(items[i].source_index) == -1:
            items.remove(i)  # legacy "Cutscene" sentinel row
            continue
        source_index = _coerce_cutscene_index(items[i].source_index)
        group = groups.pop(source_index, None)
        if group is not None:
            file_backed = bool(group.get("file_backed", False)) or (
                bool(getattr(items[i], "file_backed", False))
                and _row_matches_file_preview(items[i], file_names)
            )
            if not file_backed and source_index < AUTHORED_CLIP_ID_BASE:
                authored_id = allocate_authored_clip_id(scene)
                _retag_clip_group(group, authored_id, source_path="")
                previous_source_index = source_index
                source_index = authored_id
            else:
                previous_source_index = source_index
            group["source_index"] = source_index
            group["file_backed"] = file_backed
            retarget_events = _event_target_index(scene) == previous_source_index
            if retarget_events and previous_source_index != source_index:
                scene.witcher_cs_event_target = "ROOT"
            _update_animation_item_from_group(items[i], group, fps)
            if previous_source_index != source_index:
                _rewrite_entry_event_source(scene, previous_source_index, source_index)
                if retarget_events:
                    scene.witcher_cs_event_target = str(source_index)
            _set_authored_clip_sequence_floor(scene, source_index)
        elif bool(getattr(items[i], "file_backed", False)) and _row_matches_file_preview(items[i], file_names):
            items[i].is_loaded = False
            items[i].track_muted = False
            items[i].has_prebake = False
        else:
            orphan_ids.append(source_index)
    for source_index in dict.fromkeys(orphan_ids):
        _remove_cutscene_animation_entry(scene, source_index, remove_strips=False)

    for group in groups.values():
        source_index = _coerce_cutscene_index(group.get("source_index", -1))
        source_path = str(group.get("source_path", "") or "").strip()
        file_backed = bool(group.get("file_backed", False)) and source_index >= 0
        if not file_backed and (source_index < AUTHORED_CLIP_ID_BASE or source_path):
            source_index = allocate_authored_clip_id(scene)
            _retag_clip_group(group, source_index, source_path="")
        group["source_index"] = source_index
        group["file_backed"] = file_backed
        item = items.add()
        _update_animation_item_from_group(item, group, fps)
        _set_authored_clip_sequence_floor(scene, source_index)
    if hasattr(scene, "witcher_cutscene_loaded_anim_index") and scene.witcher_cutscene_loaded_anim_index >= len(items):
        scene.witcher_cutscene_loaded_anim_index = max(0, len(items) - 1)
    _ANIM_SYNC_SIGNATURES[str(scene.name)] = _clip_signature(scene)


def _schedule_animation_items_sync(scene):
    scene_name = str(getattr(scene, "name", "") or "")
    if not scene_name or scene_name in _ANIM_SYNC_DEFERRED:
        return
    _ANIM_SYNC_DEFERRED.add(scene_name)

    def _do_sync():
        _ANIM_SYNC_DEFERRED.discard(scene_name)
        target = bpy.data.scenes.get(scene_name)
        if target is None:
            return
        try:
            sync_animation_items_from_scene(target)
        except Exception:
            log.debug("Deferred clip sync failed.", exc_info=True)

    try:
        bpy.app.timers.register(_do_sync, first_interval=0.0)
    except Exception:
        _ANIM_SYNC_DEFERRED.discard(scene_name)


def _actor_has_resolveable_template(entry, scene):
    template_path = str(getattr(entry, "template_path", "") or "").strip()
    if not template_path:
        return False
    try:
        resolved = import_cutscene.resolve_cutscene_actor_replacement_template_path(
            template_path,
            cutscene_filename=str(getattr(scene, "witcher_loaded_w2cutscene_path", "") or ""),
            source_game=str(getattr(entry, "source_game", "") or "W3").upper() or "W3",
            context=bpy.context,
        )
    except Exception:
        resolved = ""
    return bool(resolved) and import_cutscene.win_path_isfile(resolved)


def _restore_actor_rest_pose(actor_obj):
    if actor_obj is None:
        return
    try:
        from .ui_morphs import _clear_pose_bones
    except Exception:
        log.debug("Could not import _clear_pose_bones for rest-pose restore.", exc_info=True)
        return
    for arm_obj in import_cutscene._iter_cutscene_related_armatures(actor_obj):
        try:
            _clear_pose_bones(getattr(getattr(arm_obj, "pose", None), "bones", None))
        except Exception:
            log.debug("Failed to clear pose on %s", getattr(arm_obj, "name", ""), exc_info=True)


class ButtonOperatorImportW2cutscene(Operator, ImportHelper):
    """Import a .w2cutscene."""
    bl_idname = "witcher.import_w2_cutscene"
    bl_label = "Cutscene (.w2cutscene)"
    filename_ext = ".w2cutscene"
    bl_options = {'REGISTER', 'UNDO'}

    filter_glob: StringProperty(default='*.w2cutscene', options={'HIDDEN'})
    auto_apply_animations: BoolProperty(
        name="Auto Apply Animations",
        default=True,
        description="Load selected cutscene animations onto their matching actors after import",
    )
    auto_apply_dialog: BoolProperty(
        name="Auto Apply Dialog",
        default=True,
        description="Load linked dialog voice strips and lipsync onto matching actors after import",
    )
    import_burned_audio: BoolProperty(
        name="Import Burned Track",
        default=True,
        description="Import the cutscene burned-track audio strip into the Blender sequencer",
    )
    retarget_w2_to_w3: BoolProperty(
        name="W2 to W3 Retarget",
        default=False,
        description="Replace W2 cutscene camera/actors with W3 camera, Geralt and Ciri during import",
    )
    retarget_replace_camera: BoolProperty(
        name="Camera",
        default=True,
        description="Use the Witcher 3 cutscene camera entity while keeping the imported camera animation",
    )
    retarget_replace_actors: BoolProperty(
        name="Actors",
        default=True,
        description="Replace W2 male/female body actors with W3 Geralt/Ciri and retarget body animation",
    )
    retarget_w3_camera_template: StringProperty(
        name="W3 Camera",
        default=import_cutscene.W3_CUTSCENE_CAMERA_TEMPLATE,
        description="Witcher 3 camera entity template",
    )
    retarget_w3_male_template: StringProperty(
        name="W3 Male",
        default=import_cutscene.W3_CUTSCENE_GERALT_TEMPLATE,
        description="Witcher 3 template used for W2 male skeleton actors",
    )
    retarget_w3_female_template: StringProperty(
        name="W3 Female",
        default=import_cutscene.W3_CUTSCENE_CIRI_TEMPLATE,
        description="Witcher 3 template used for W2 female skeleton actors",
    )

    cutscene_actor_items: CollectionProperty(type=CutsceneActorPreviewItem)
    cutscene_actor_index: IntProperty(default=0)
    cutscene_animation_items: CollectionProperty(type=CutsceneAnimationPreviewItem)
    cutscene_animation_index: IntProperty(default=0)
    cutscene_preview_status: StringProperty(default="Select a .w2cutscene file")
    cutscene_preview_path: StringProperty(default="")
    cutscene_preview_mtime: FloatProperty(default=0.0)

    def draw(self, context):
        layout = self.layout

        settings_box = layout.box()
        settings_box.label(text="Import Settings")
        auto_box = settings_box.box()
        auto_box.label(text="Auto Apply", icon='PREFERENCES')
        auto_box.prop(self, "auto_apply_animations")
        auto_box.prop(self, "auto_apply_dialog")
        auto_box.prop(self, "import_burned_audio")
        retarget_box = settings_box.box()
        retarget_box.prop(self, "retarget_w2_to_w3")
        if self.retarget_w2_to_w3:
            replace_row = retarget_box.row(align=True)
            replace_row.prop(self, "retarget_replace_camera")
            replace_row.prop(self, "retarget_replace_actors")
            retarget_box.prop(self, "retarget_w3_camera_template", text="Camera")
            retarget_box.prop(self, "retarget_w3_male_template", text="Male")
            retarget_box.prop(self, "retarget_w3_female_template", text="Female")

        status_box = layout.box()
        status_box.label(text="Cutscene Preview")
        status_box.label(text=self.cutscene_preview_status)

        actor_box = layout.box()
        actor_box.label(text="Entities", icon='OUTLINER_OB_ARMATURE')
        if self.cutscene_actor_items:
            actor_box.template_list(
                "WITCH_UL_CutsceneActorPreview",
                "",
                self,
                "cutscene_actor_items",
                self,
                "cutscene_actor_index",
                rows=6,
            )
            selected_actor_count = sum(1 for item in self.cutscene_actor_items if item.selected)
            actor_box.label(text=f"Will import/reuse: {selected_actor_count}/{len(self.cutscene_actor_items)} entities")
            idx = self.cutscene_actor_index
            if 0 <= idx < len(self.cutscene_actor_items):
                actor = self.cutscene_actor_items[idx]
                details = actor_box.column(align=True)
                if actor.template_path:
                    details.label(text=f"Template: {actor.template_path}")
                if actor.actor_type:
                    details.label(text=f"Type: {actor.actor_type}")
                if actor.appearance_name:
                    details.label(text=f"Appearance: {actor.appearance_name}")
                if actor.use_mimic:
                    details.label(text="Uses mimic data")

        anim_box = layout.box()
        anim_box.label(text="Animations", icon='ACTION')
        if self.cutscene_animation_items:
            anim_box.template_list(
                "WITCH_UL_CutsceneAnimationPreview",
                "",
                self,
                "cutscene_animation_items",
                self,
                "cutscene_animation_index",
                rows=8,
            )
            selected_animation_count = sum(1 for item in self.cutscene_animation_items if item.selected)
            anim_box.label(text=f"Will import: {selected_animation_count}/{len(self.cutscene_animation_items)} animations")
            if self.auto_apply_animations:
                anim_box.label(text="Auto-apply uses matching actors already in scene or being loaded.", icon='INFO')
            idx = self.cutscene_animation_index
            if 0 <= idx < len(self.cutscene_animation_items):
                anim = self.cutscene_animation_items[idx]
                details = anim_box.column(align=True)
                details.label(text=f"Name: {anim.full_name}")
                if anim.component_name:
                    details.label(text=f"Component: {anim.component_name}")
                if anim.frames_per_second:
                    details.label(text=f"FPS: {anim.frames_per_second:.2f}")
                if anim.num_frames:
                    details.label(text=f"Frames: {anim.num_frames}")
                if anim.duration:
                    details.label(text=f"Duration: {anim.duration:.3f}s")

    def check(self, context):
        return _update_cutscene_preview(self)

    def execute(self, context):
        operation_started = time.perf_counter()
        if os.path.isdir(self.filepath):
            self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
            return {'CANCELLED'}

        lowered = self.filepath.lower()
        if not lowered.endswith(".w2cutscene"):
            self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
            return {'CANCELLED'}

        if not self.cutscene_actor_items and not self.cutscene_animation_items:
            _update_cutscene_preview(self)

        selected_actor_indices = {
            item.source_index
            for item in self.cutscene_actor_items
            if item.selected
        }
        selected_animation_indices = {
            item.source_index
            for item in self.cutscene_animation_items
            if item.selected
        }

        if not selected_actor_indices and not selected_animation_indices:
            self.report({'INFO'}, "Nothing auto-selected; loading cutscene list only.")

        # Classify/tag any V1 authored strips before the importer changes the loaded path.
        sync_animation_items_from_scene(context.scene)
        try:
            if self.retarget_w2_to_w3 and hasattr(context.scene, "witcher_w2_retarget_to_w3"):
                context.scene.witcher_w2_retarget_to_w3 = True
            cutscene_data = import_cutscene.import_w3_cutscene(
                self.filepath,
                selected_actor_indices=selected_actor_indices if self.cutscene_actor_items else None,
                selected_animation_indices=selected_animation_indices if self.cutscene_animation_items else None,
                auto_apply_selected_animations=self.auto_apply_animations,
                import_burned_audio=self.import_burned_audio,
                retarget_options={
                    "enabled": self.retarget_w2_to_w3,
                    "replace_camera": self.retarget_replace_camera,
                    "replace_actors": self.retarget_replace_actors,
                    "camera_template": self.retarget_w3_camera_template,
                    "male_template": self.retarget_w3_male_template,
                    "female_template": self.retarget_w3_female_template,
                },
            )
        except Exception as exc:
            log.exception("Failed to import cutscene %s", self.filepath)
            self.report({'ERROR'}, f"Failed to import cutscene: {exc}")
            return {'CANCELLED'}
        if cutscene_data is None:
            self.report({'ERROR'}, "Failed to load cutscene file.")
            return {'CANCELLED'}

        auto_loaded_count = int(getattr(cutscene_data, "auto_applied_animation_count", 0) or 0)
        _sync_loaded_cutscene_state(context.scene, self.filepath, cutscene_data=cutscene_data)
        if hasattr(context.scene, "witcher_w2scene_active_cutscene_path"):
            context.scene.witcher_w2scene_active_cutscene_path = ""
        dialog_loaded_count = 0
        dialog_skipped_count = 0
        dialog_total_count = 0
        dialog_sound_loaded_count = 0
        if self.auto_apply_dialog:
            try:
                dialog_stats = _load_cutscene_dialogs_into_scene(context)
                dialog_loaded_count = int(dialog_stats.get("loaded", 0) or 0)
                dialog_skipped_count = int(dialog_stats.get("skipped", 0) or 0)
                dialog_total_count = int(dialog_stats.get("total", 0) or 0)
                dialog_sound_loaded_count = int(dialog_stats.get("sound_loaded", 0) or 0)
            except Exception as exc:
                log.exception("Failed to auto-apply cutscene dialog for %s", self.filepath)
                self.report({'WARNING'}, f"Cutscene imported, but dialog auto-apply failed: {exc}")
        burned_audio_info = dict(getattr(cutscene_data, "burned_audio_info", {}) or {})
        status_parts = []
        if self.auto_apply_dialog:
            if dialog_total_count > 0:
                dialog_text = f"loaded {dialog_loaded_count} dialog line(s)"
                if dialog_sound_loaded_count:
                    dialog_text += f", {dialog_sound_loaded_count} sound strip(s)"
                if dialog_skipped_count:
                    dialog_text += f" ({dialog_skipped_count} skipped)"
                status_parts.append(dialog_text)
            else:
                status_parts.append("no dialog lines found")
        if self.import_burned_audio and burned_audio_info:
            status_parts.append("burned track imported")
        retarget_counts = dict(getattr(cutscene_data, "w2w3_retarget_counts", {}) or {})
        retarget_total = sum(int(value or 0) for value in retarget_counts.values())
        if self.retarget_w2_to_w3 and retarget_total:
            status_parts.append(f"W2->W3 replaced {retarget_total}")
        total_elapsed = time.perf_counter() - operation_started
        log.info("Imported .w2cutscene '%s' in %.2fs.", os.path.basename(self.filepath), total_elapsed)
        self.report(
            {'INFO'},
            (
                f"Imported {len(selected_actor_indices)} actor(s) and auto-loaded "
                f"{auto_loaded_count}/{len(selected_animation_indices)} animation(s) in {total_elapsed:.2f}s."
                if self.auto_apply_animations
                else (
                    f"Imported {len(selected_actor_indices)} actor(s) and listed "
                    f"{len(selected_animation_indices)} animation(s) from cutscene in {total_elapsed:.2f}s."
                )
            ) + (f" {'; '.join(status_parts)}." if status_parts else ""),
        )
        return {'FINISHED'}
    def invoke(self, context, event):
        UNCOOK_PATH = os.path.join(get_uncook_path(context),"animations\\")
        if os.path.exists(UNCOOK_PATH):
            self.filepath = UNCOOK_PATH if self.filepath == '' else self.filepath
        return ImportHelper.invoke(self, context, event)

class WITCH_OT_ReopenCutsceneImportDialog(Operator):
    bl_idname = "witcher.reopen_cutscene_import_dialog"
    bl_label = "Open Import Dialog"
    bl_description = "Open the cutscene import dialog for the current cutscene"

    def execute(self, context):
        filepath = str(getattr(context.scene, "witcher_loaded_w2cutscene_path", "") or "").strip()
        if filepath:
            bpy.ops.witcher.import_w2_cutscene('INVOKE_DEFAULT', filepath=filepath)
        else:
            bpy.ops.witcher.import_w2_cutscene('INVOKE_DEFAULT')
        return {'FINISHED'}


class WITCH_OT_ImportCutsceneBurnedAudio(Operator):
    bl_idname = "witcher.import_cutscene_burned_audio"
    bl_label = "Import Burned Track"
    bl_description = "Import the cutscene burned-track audio strip into the Blender sequencer"

    def execute(self, context):
        scene = context.scene
        filepath = str(getattr(scene, "witcher_loaded_w2cutscene_path", "") or "").strip()
        if not filepath:
            self.report({'WARNING'}, "No cutscene loaded.")
            return {'CANCELLED'}

        try:
            burned_audio_info = import_cutscene.import_cutscene_burned_audio_track(context, filepath=filepath)
        except Exception as exc:
            log.exception("Failed to import cutscene burned audio for %s", filepath)
            self.report({'ERROR'}, f"Failed to import burned track: {exc}")
            return {'CANCELLED'}

        if not burned_audio_info:
            self.report({'WARNING'}, "No burned track is defined for this cutscene.")
            return {'CANCELLED'}

        _set_cutscene_burned_audio_scene_state(
            scene,
            event_name=burned_audio_info.get("event_name", ""),
            item_path=burned_audio_info.get("item_path", ""),
        )
        self.report({'INFO'}, f"Imported burned track '{burned_audio_info.get('event_name', '')}'.")
        return {'FINISHED'}


class WITCH_OT_RemoveCutsceneBurnedAudio(Operator):
    bl_idname = "witcher.remove_cutscene_burned_audio"
    bl_label = "Remove Burned Track"
    bl_description = "Remove the imported cutscene burned-track audio strip from the Blender sequencer"

    def execute(self, context):
        scene = context.scene
        filepath = str(getattr(scene, "witcher_loaded_w2cutscene_path", "") or "").strip()
        if not filepath:
            self.report({'WARNING'}, "No cutscene loaded.")
            return {'CANCELLED'}

        removed_count = int(import_cutscene.remove_cutscene_burned_audio_strips(scene, source_path=filepath) or 0)
        if removed_count <= 0:
            self.report({'WARNING'}, "No imported burned track found for this cutscene.")
            return {'CANCELLED'}

        self.report({'INFO'}, f"Removed {removed_count} burned track strip(s).")
        return {'FINISHED'}

class WITCH_OT_SetCutsceneAnimationLoaded(Operator):
    bl_idname = "witcher.set_cutscene_animation_loaded"
    bl_label = "Toggle Cutscene Animation"
    bl_description = "Load or unload a cutscene animation (mutes/unmutes NLA strips)"
    bl_options = {'REGISTER', 'UNDO'}

    source_index: IntProperty(default=-1)
    load: BoolProperty(default=True)

    def execute(self, context):
        scene = context.scene
        animation_entry = _find_loaded_cutscene_animation_entry(scene, self.source_index)
        if animation_entry is None:
            self.report({'ERROR'}, "Cutscene animation entry not found.")
            return {'CANCELLED'}
        if not bool(getattr(animation_entry, "file_backed", False)):
            self.report({'ERROR'}, "Only clips from the loaded file can be loaded or unloaded.")
            return {'CANCELLED'}
        source_index = _coerce_cutscene_index(getattr(animation_entry, "source_index", -1))
        animation_label = _get_cutscene_animation_label(animation_entry)
        if bool((_clip_groups(scene).get(source_index) or {}).get("has_prebake", False)):
            self.report({'WARNING'}, "Pre-bake source clips are read-only")
            return {'CANCELLED'}

        actor_entry = _find_actor_entry_for_animation(scene, animation_entry)
        if actor_entry is None:
            self.report({'ERROR'}, "No matching cutscene actor found for this animation.")
            return {'CANCELLED'}
        actor_state = _get_cutscene_actor_display_state(actor_entry)

        # Actors and animations are decoupled: loading an animation never implicitly
        # imports its actor. The anim row offers an explicit "Import Actor" button instead.
        if self.load and not actor_state["is_loaded"]:
            self.report({'ERROR'}, "Import this animation's actor first.")
            return {'CANCELLED'}

        animation_entry.is_loaded = bool(self.load)
        applied_indices, error_messages = _rebuild_cutscene_actor_animations(scene, actor_entry)
        if self.load and source_index not in applied_indices:
            animation_entry = _find_loaded_cutscene_animation_entry(scene, source_index)
            if animation_entry is not None:
                animation_entry.is_loaded = False
            error_text = str(error_messages.get(source_index, "") or "").strip()
            message = f"Failed to load cutscene animation '{animation_label}'"
            if error_text:
                message = f"{message}: {error_text}"
            self.report({'ERROR'}, message)
            return {'CANCELLED'}

        if not self.load:
            actor_obj = actor_state["actor_obj"]
            if actor_obj is not None and not _loaded_actor_active_animation_indices(scene, actor_entry):
                _restore_actor_rest_pose(actor_obj)

        if self.load:
            self.report({'INFO'}, f"Loaded animation '{animation_label}'.")
        else:
            self.report({'INFO'}, f"Unloaded animation '{animation_label}'.")
        return {'FINISHED'}


class WITCH_OT_SetCutsceneActorAnimationLayer(Operator):
    bl_idname = "witcher.set_cutscene_actor_animation_layer"
    bl_label = "Toggle Actor Animations"
    bl_description = "Load or unload this actor's root/body or face cutscene animations"
    bl_options = {'REGISTER', 'UNDO'}

    source_index: IntProperty(default=-1)
    object_name: StringProperty(default="")
    actor_name: StringProperty(default="")
    layer: EnumProperty(
        items=[
            ('ROOT', "Root", "Root/body animations for this actor"),
            ('FACE', "Face", "Face/mimic animations for this actor"),
            ('ALL', "All", "All animations for this actor"),
        ],
        default='ROOT',
    )
    load: BoolProperty(default=True)

    def execute(self, context):
        scene = context.scene
        actor_entry = _find_loaded_actor_entry(scene, self.source_index, self.object_name, self.actor_name)
        if actor_entry is None:
            self.report({'ERROR'}, "Cutscene actor entry not found.")
            return {'CANCELLED'}

        actor_state = _get_cutscene_actor_display_state(actor_entry)
        if self.load and not actor_state["is_loaded"]:
            self.report({'ERROR'}, "Load this actor first.")
            return {'CANCELLED'}

        entries = _actor_animation_entries_for_layer(scene, actor_entry, self.layer)
        if not entries:
            self.report({'WARNING'}, f"No {self.layer.lower()} animations for this actor.")
            return {'CANCELLED'}

        requested_indices = {_coerce_cutscene_index(getattr(entry, "source_index", -1)) for entry in entries}
        groups = _clip_groups(scene)
        if any(bool((groups.get(source_index) or {}).get("has_prebake", False)) for source_index in requested_indices):
            self.report({'WARNING'}, "Pre-bake source clips are read-only")
            return {'CANCELLED'}
        for source_index in requested_indices:
            entry = _find_loaded_cutscene_animation_entry(scene, source_index)
            if entry is not None:
                entry.is_loaded = bool(self.load)

        applied_indices, error_messages = _rebuild_cutscene_actor_animations(scene, actor_entry)
        layer_label = self.layer.capitalize()
        if self.load:
            failed_indices = sorted(idx for idx in requested_indices if idx not in applied_indices)
            if failed_indices:
                first_error = str(error_messages.get(failed_indices[0], "") or "").strip()
                if len(failed_indices) == len(requested_indices):
                    message = f"Failed to load {layer_label} animations"
                    if first_error:
                        message = f"{message}: {first_error}"
                    self.report({'ERROR'}, message)
                    return {'CANCELLED'}
                message = f"Loaded {len(requested_indices) - len(failed_indices)}/{len(requested_indices)} {layer_label} animations"
                if first_error:
                    message = f"{message}: {first_error}"
                self.report({'WARNING'}, message)
                return {'FINISHED'}
            self.report({'INFO'}, f"Loaded {len(requested_indices)} {layer_label} animation(s).")
            return {'FINISHED'}

        actor_obj = actor_state["actor_obj"]
        if actor_obj is not None and not _loaded_actor_active_animation_indices(scene, actor_entry):
            _restore_actor_rest_pose(actor_obj)
        self.report({'INFO'}, f"Unloaded {len(requested_indices)} {layer_label} animation(s).")
        return {'FINISHED'}


class WITCH_OT_SetCutsceneActorLoaded(Operator):
    bl_idname = "witcher.set_cutscene_actor_loaded"
    bl_label = "Load / Unload Entity"
    bl_description = (
        "Load imports this actor's entity from the cutscene file; Unload deletes its objects. The actor stays in the list"
    )
    bl_options = {'REGISTER', 'UNDO'}

    source_index: IntProperty(default=-1)
    object_name: StringProperty(default="")
    actor_name: StringProperty(default="")
    load: BoolProperty(default=True)

    def execute(self, context):
        scene = context.scene
        entry = _find_loaded_actor_entry(scene, self.source_index, self.object_name, self.actor_name)
        if entry is None:
            self.report({'ERROR'}, "Cutscene actor entry not found.")
            return {'CANCELLED'}
        label = str(getattr(entry, "label", "") or getattr(entry, "actor_name", "") or "actor")

        if self.load:
            filepath = str(getattr(scene, "witcher_loaded_w2cutscene_path", "") or "").strip()
            if int(getattr(entry, "source_index", -1)) < 0 or not filepath:
                self.report({'ERROR'}, "This actor can't be re-spawned here; use 'Assign Selected Armature' to add it again.")
                return {'CANCELLED'}
            actor_obj = _load_cutscene_actor_entry(scene, entry)
            if actor_obj is None:
                self.report({'ERROR'}, f"Failed to import actor '{label}'.")
                return {'CANCELLED'}
            _sync_actor_items_with_scene(scene)
            try:
                dialog_stats = _restore_cutscene_actor_dialog_lipsync(context, entry, actor_obj)
            except Exception as exc:
                log.warning("Failed to restore dialog lipsync for actor %s: %s", label, exc)
                self.report({'WARNING'}, f"Imported actor '{label}', but dialog lipsync restore failed: {exc}")
                return {'FINISHED'}
            dialog_count = int(dialog_stats.get("loaded", 0) or 0)
            message = f"Imported actor '{label}'."
            if dialog_count:
                message = f"{message} Restored {dialog_count} dialog line(s)."
            self.report({'INFO'}, message)
            return {'FINISHED'}

        actor_obj = _get_loaded_cutscene_actor_object(entry)
        obj_name = str(getattr(actor_obj, "name", "") or getattr(entry, "object_name", "") or "")
        if actor_obj is not None:
            import_cutscene.unload_cutscene_actor(actor_obj, force_remove=True)
        _clear_loaded_actor_animation_flags(scene, entry)
        entry.object_name = ""
        entry.cutscene_guid = ""
        entry.is_loaded = False
        entry.imported_by_cutscene = False
        self.report({'INFO'}, f"Removed entity for '{label}' (actor kept in list).")
        return {'FINISHED'}


class WITCH_OT_CutsceneRemoveActorFull(Operator):
    bl_idname = "witcher.cutscene_remove_actor_full"
    bl_label = "Remove Cutscene Actor"
    bl_description = "Remove this actor from the cutscene entirely: delete its entity and drop it from the list"
    bl_options = {'REGISTER', 'UNDO'}

    source_index: IntProperty(default=-1)
    object_name: StringProperty(default="")
    actor_name: StringProperty(default="")

    def invoke(self, context, event):
        entry = _find_loaded_actor_entry(context.scene, self.source_index, self.object_name, self.actor_name)
        obj_name = str(getattr(entry, "object_name", "") or "") if entry else str(self.object_name or "")
        if obj_name and bpy.data.objects.get(obj_name) is not None:
            return context.window_manager.invoke_confirm(self, event)
        return self.execute(context)

    def execute(self, context):
        scene = context.scene
        entry = _find_loaded_actor_entry(scene, self.source_index, self.object_name, self.actor_name)
        label = str(getattr(entry, "label", "") or getattr(entry, "actor_name", "") or self.actor_name or "actor") if entry else (self.actor_name or "actor")
        obj_name = str(getattr(entry, "object_name", "") or "") if entry else str(self.object_name or "")
        actor_obj = bpy.data.objects.get(obj_name) if obj_name else None
        if actor_obj is not None:
            import_cutscene.unload_cutscene_actor(actor_obj, force_remove=True)
        _clear_loaded_actor_animation_flags(scene, entry)
        if entry is not None:
            items = getattr(scene, "witcher_cutscene_actor_items", None)
            if items is not None:
                idx = _find_loaded_actor_entry_index(scene, self.source_index, self.object_name, self.actor_name)
                if idx >= 0:
                    items.remove(idx)
        self.report({'INFO'}, f"Removed actor '{label}' from cutscene.")
        return {'FINISHED'}


class WITCH_OT_CutsceneImportActorForAnim(Operator):
    bl_idname = "witcher.cutscene_import_actor_for_anim"
    bl_label = "Import Actor"
    bl_description = "Import the actor required by this animation into the scene"
    bl_options = {'REGISTER', 'UNDO'}

    source_index: IntProperty(default=-1)

    def execute(self, context):
        scene = context.scene
        animation_entry = _find_loaded_cutscene_animation_entry(scene, self.source_index)
        if animation_entry is None:
            self.report({'ERROR'}, "Cutscene animation entry not found.")
            return {'CANCELLED'}
        if not bool(getattr(animation_entry, "file_backed", False)):
            self.report({'ERROR'}, "This authored clip has no file actor to import.")
            return {'CANCELLED'}
        actor_entry = _find_actor_entry_for_animation(scene, animation_entry)
        if actor_entry is None:
            self.report({'ERROR'}, "No matching cutscene actor for this animation.")
            return {'CANCELLED'}
        actor_obj = _load_cutscene_actor_entry(scene, actor_entry)
        if actor_obj is None:
            self.report({'ERROR'}, "Failed to import the actor.")
            return {'CANCELLED'}
        _sync_actor_items_with_scene(scene)
        label = getattr(actor_entry, 'actor_name', '') or getattr(actor_entry, 'label', '')
        try:
            dialog_stats = _restore_cutscene_actor_dialog_lipsync(context, actor_entry, actor_obj)
        except Exception as exc:
            log.warning("Failed to restore dialog lipsync for actor %s: %s", label, exc)
            self.report({'WARNING'}, f"Imported actor '{label}', but dialog lipsync restore failed: {exc}")
            return {'FINISHED'}
        dialog_count = int(dialog_stats.get("loaded", 0) or 0)
        message = f"Imported actor '{label}'."
        if dialog_count:
            message = f"{message} Restored {dialog_count} dialog line(s)."
        self.report({'INFO'}, message)
        return {'FINISHED'}


def _draw_event_list_item(self, layout, item):
    if self.layout_type in {'DEFAULT', 'COMPACT'}:
        row = layout.row(align=True)
        if partial_event_fields(item.event_type):
            row.operator("witcher.cutscene_event_partial_info", text="", icon='ERROR', emboss=False).event_type = item.event_type
        else:
            _cutscene_fixed_label(row, "", icon='CHECKMARK', units=1.4)
        row.label(text=_get_cutscene_event_label(item))
    elif self.layout_type == 'GRID':
        layout.alignment = 'CENTER'
        layout.label(text="")


_EVENT_TARGET_ITEMS = []
_EVENT_TARGET_ITEM_HISTORY = [_EVENT_TARGET_ITEMS]


def _event_target_items(self, context):
    global _EVENT_TARGET_ITEMS
    scene = getattr(context, "scene", None)
    items = [("ROOT", "Cutscene (root)", "Cutscene-level events (CCutsceneTemplate.animevents)", 'NONE', 0)]
    for anim in (getattr(scene, "witcher_cutscene_animation_items", []) if scene is not None else []):
        source_index = int(anim.source_index)
        if source_index >= 0:
            items.append((
                str(source_index),
                _get_cutscene_animation_label(anim),
                "Events on this animation entry",
                'NONE',
                source_index + 1,
            ))
    if items != _EVENT_TARGET_ITEMS:
        _EVENT_TARGET_ITEMS = items
        _EVENT_TARGET_ITEM_HISTORY.append(_EVENT_TARGET_ITEMS)
    return _EVENT_TARGET_ITEMS


def _event_target_index(scene):
    try:
        return int(str(getattr(scene, "witcher_cs_event_target", "ROOT") or "ROOT"))
    except ValueError:
        return -1


class WITCH_OT_CutsceneExactValueDetails(Operator):
    """Show and copy an exact cutscene value."""
    bl_idname = "witcher.cutscene_exact_value_details"
    bl_label = "Exact Value"
    bl_options = {'INTERNAL'}

    field_label: StringProperty(default="Value", options={'SKIP_SAVE'})
    value: StringProperty(default="", options={'SKIP_SAVE'})

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=520, confirm_text="Copy")

    def draw(self, context):
        self.layout.prop(self, "value", text=self.field_label or "Value")

    def execute(self, context):
        context.window_manager.clipboard = self.value
        self.report({'INFO'}, f"{self.field_label or 'Value'} copied to clipboard")
        return {'FINISHED'}


def _draw_cutscene_exact_value(layout, data, prop_name, *, text="", field_label="", value=None, icon='NONE'):
    field = layout.row(align=True)
    field.enabled = False
    field.prop(data, prop_name, text=text, icon=icon)
    details = layout.operator(WITCH_OT_CutsceneExactValueDetails.bl_idname, text="", icon='INFO')
    details.field_label = field_label or text or "Value"
    if value is None:
        value = getattr(data, prop_name, "")
    details.value = "" if value is None else str(value)


class WITCH_OT_CutsceneEventPartialInfo(Operator):
    """Show event fields omitted from export."""
    bl_idname = "witcher.cutscene_event_partial_info"
    bl_label = "Partial Export"
    bl_options = {'INTERNAL'}

    event_type: StringProperty(default="")

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=420)

    def draw(self, context):
        self.layout.label(text=self.event_type, icon=_event_type_icon(self.event_type))
        self.layout.label(text="Not exported: " + ", ".join(partial_event_fields(self.event_type)), icon='ERROR')
        self.layout.prop(self, "event_type", text="")

    def execute(self, context):
        return {'FINISHED'}


class WITCH_UL_RootEventList(UIList):
    bl_idname = "WITCH_UL_RootEventList"
    layout_type = "DEFAULT"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index, flt_flag):
        _draw_event_list_item(self, layout, item)

    def filter_items(self, context, data, propname):
        items = getattr(data, propname, [])
        flags = [self.bitflag_filter_item if str(getattr(i, "event_scope", "") or "").upper() == "ROOT" else 0
                 for i in items]
        return flags, []


class WITCH_UL_EntryEventList(UIList):
    bl_idname = "WITCH_UL_EntryEventList"
    layout_type = "DEFAULT"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index, flt_flag):
        _draw_event_list_item(self, layout, item)

    def filter_items(self, context, data, propname):
        items = getattr(data, propname, [])
        anim_src_idx = _event_target_index(context.scene)
        flags = [self.bitflag_filter_item
                 if (str(getattr(i, "event_scope", "") or "").upper() == "ENTRY"
                     and int(getattr(i, "source_index", -1)) == anim_src_idx)
                 else 0
                 for i in items]
        return flags, []


_STORED_EVENT_FIELDS = frozenset({"eventName","startTime","animationName","duration","alwaysFiresEnd"})
_EXPORTED_EVENT_FIELDS = _STORED_EVENT_FIELDS | {"appearance", "effectName", "effect", "trackName"}


def partial_event_fields(event_type):
    entry = _EVENT_SCHEMA_BY_CLASS.get(str(event_type or ""))
    return [name for name, _type in (entry[3] if entry else []) if name not in _EXPORTED_EVENT_FIELDS]

def _draw_event_detail(layout, ev):
    detail_box = layout.box()
    event_type = str(getattr(ev, "event_type", "") or "")
    schema_entry = _EVENT_SCHEMA_BY_CLASS.get(event_type)
    hdr = detail_box.row(align=True)
    hdr.label(text=event_type or "Event", icon=_event_type_icon(event_type))
    if schema_entry:
        hdr.label(text=f"↑ {schema_entry[1]}", icon='BLANK1')
    if partial_event_fields(event_type):
        hdr.operator(WITCH_OT_CutsceneEventPartialInfo.bl_idname, text="Partial export", icon='ERROR').event_type = event_type

    col = detail_box.column(align=True)
    col.use_property_split = True
    col.prop(ev, "event_name")
    col.prop(ev, "start_time")
    col.prop(ev, "animation_name")
    if ev.duration > 0.0 or _event_schema_has_duration(event_type):
        col.prop(ev, "duration")
        col.prop(ev, "always_fires_end")
    col.prop(ev, "track_name")
    if ev.effect_name or "Effect" in event_type:
        col.prop(ev, "effect_name")
    if ev.appearance or 'BodyPart' in event_type or 'Appearance' in event_type:
        col.prop(ev, "appearance")


_ensure_actor_custom_props = import_cutscene.ensure_actor_custom_props


def _draw_actor_key(layout, obj, key, text):
    if key in obj:
        layout.prop(obj, f'["{key}"]', text=text)
    else:
        layout.label(text=f"{text}: (unset)", icon='BLANK1')


class WITCH_OT_CutsceneRemoveActor(Operator):
    """Remove this armature from the cutscene (clears its cutscene tags)"""
    bl_idname = "witcher.cutscene_remove_actor"
    bl_label = "Remove Actor"
    bl_options = {'UNDO'}
    object_name: StringProperty(default="")

    def execute(self, context):
        scene = context.scene
        obj = bpy.data.objects.get(self.object_name)
        if obj is None:
            return {'CANCELLED'}
        import_cutscene._clear_cutscene_actor_tags(obj)
        for pn in ("cutscene_actor_tag", "cutscene_actor_voice_tag",
                   "cutscene_actor_final_position", "cutscene_actor_kill_me",
                   "cutscene_actor_anim_final_pos"):
            try:
                if pn in obj:
                    del obj[pn]
            except Exception:
                pass
        return {'FINISHED'}


def _scene_cutscene_w2w3_retarget_options(scene):
    return import_cutscene.normalize_cutscene_w2w3_retarget_options({
        "enabled": True,
        "replace_camera": True,
        "replace_actors": True,
        "camera_template": getattr(scene, "witcher_cutscene_retarget_camera_template", ""),
        "male_template": getattr(scene, "witcher_cutscene_retarget_male_template", ""),
        "female_template": getattr(scene, "witcher_cutscene_retarget_female_template", ""),
    })


def _loaded_actor_active_animation_indices(scene, actor_entry):
    return [
        int(animation_entry.source_index)
        for animation_entry in getattr(scene, "witcher_cutscene_animation_items", [])
        if bool(getattr(animation_entry, "file_backed", False))
        and bool(getattr(animation_entry, "is_loaded", False))
        and _animation_matches_actor_entry(scene, animation_entry, actor_entry)
    ]


def _replace_loaded_cutscene_actor_template(scene, actor_entry, old_obj, template_path, *,
                                            retarget_kind="", retarget_source_rig=""):
    filepath = str(getattr(scene, "witcher_loaded_w2cutscene_path", "") or "").strip()
    if not filepath:
        raise RuntimeError("No cutscene loaded.")
    _ensure_actor_custom_props(old_obj)
    source_index = _coerce_cutscene_index(
        old_obj.get(import_cutscene.CUTSCENE_SOURCE_INDEX_PROP, getattr(actor_entry, "source_index", -1)),
        default=getattr(actor_entry, "source_index", -1),
    )
    actor_info = import_cutscene.replace_cutscene_actor_template(
        old_obj,
        template_path,
        source_game="W3",
        cutscene_filename=filepath,
        actor_name=str(old_obj.get("cutscene_actor_name", "") or getattr(actor_entry, "actor_name", "")),
        actor_type=str(old_obj.get("cutscene_actor_type", "") or getattr(actor_entry, "actor_type", "CAT_Actor")),
        appearance_name="",
        tag=str(old_obj.get("cutscene_actor_tag", "") or getattr(actor_entry, "tag", "")),
        voice_tag=str(old_obj.get("cutscene_actor_voice_tag", "") or getattr(actor_entry, "voice_tag", "")),
        final_position=str(old_obj.get("cutscene_actor_final_position", "") or getattr(actor_entry, "final_position", "")),
        kill_me=bool(old_obj.get("cutscene_actor_kill_me", getattr(actor_entry, "kill_me", False))),
        use_mimic=bool(old_obj.get("cutscene_actor_use_mimic", getattr(actor_entry, "use_mimic", False))),
        anim_final_pos=str(old_obj.get("cutscene_actor_anim_final_pos", "") or getattr(actor_entry, "anim_final_pos", "")),
        source_index=source_index,
        retarget_kind=retarget_kind,
        retarget_source_rig=retarget_source_rig,
        original_template_path=str(old_obj.get("cutscene_actor_template", "") or getattr(actor_entry, "template_path", "")),
    )
    _update_loaded_actor_entry_from_result(actor_entry, actor_info)
    actor_entry.template_path = str(actor_info.get("template_path", "") or template_path)
    actor_entry.appearance_name = ""
    actor_entry.source_game = "w3"
    return actor_info


class WITCH_OT_CutsceneReplaceActor(Operator):
    bl_idname = "witcher.cutscene_replace_actor"
    bl_label = "Replace Actor"
    bl_description = "Import the selected actor's template path from the chosen source and reload active cutscene animations"
    bl_options = {'REGISTER', 'UNDO'}

    object_name: StringProperty(default="")

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=480)

    def draw(self, context):
        obj = bpy.data.objects.get(self.object_name)
        if obj is not None and "cutscene_actor_template" in obj:
            self.layout.prop(obj, '["cutscene_actor_template"]', text="Template")
        self.layout.prop(context.scene, "witcher_cutscene_actor_replace_source", text="Source")

    def execute(self, context):
        scene = context.scene
        old_obj = bpy.data.objects.get(self.object_name)
        if old_obj is None or getattr(old_obj, "type", None) != 'ARMATURE':
            self.report({'ERROR'}, "Selected cutscene actor was not found.")
            return {'CANCELLED'}

        _ensure_actor_custom_props(old_obj)
        actor_entry = _find_loaded_cutscene_actor_entry_for_object(scene, old_obj)
        if actor_entry is None:
            self.report({'ERROR'}, "Loaded cutscene actor entry was not found.")
            return {'CANCELLED'}

        template_path = str(old_obj.get("cutscene_actor_template", "") or "").strip()
        if not template_path:
            self.report({'ERROR'}, "Replacement template path is empty.")
            return {'CANCELLED'}

        filepath = str(getattr(scene, "witcher_loaded_w2cutscene_path", "") or "").strip()
        source_game = str(getattr(scene, "witcher_cutscene_actor_replace_source", "W3") or "W3")
        try:
            source_index = _coerce_cutscene_index(
                old_obj.get(import_cutscene.CUTSCENE_SOURCE_INDEX_PROP, actor_entry.source_index),
                default=actor_entry.source_index,
            )
        except Exception:
            source_index = _coerce_cutscene_index(getattr(actor_entry, "source_index", -1))

        requested_animation_indices = [
            int(animation_entry.source_index)
            for animation_entry in getattr(scene, "witcher_cutscene_animation_items", [])
            if bool(getattr(animation_entry, "is_loaded", False))
            and _animation_matches_actor_entry(scene, animation_entry, actor_entry)
        ]

        try:
            actor_info = import_cutscene.replace_cutscene_actor_template(
                old_obj,
                template_path,
                source_game=source_game,
                cutscene_filename=filepath,
                actor_name=str(old_obj.get("cutscene_actor_name", "") or getattr(actor_entry, "actor_name", "")),
                actor_type=str(old_obj.get("cutscene_actor_type", "") or getattr(actor_entry, "actor_type", "CAT_Actor")),
                appearance_name=str(old_obj.get("cutscene_actor_appearance", "") or getattr(actor_entry, "appearance_name", "")),
                tag=str(old_obj.get("cutscene_actor_tag", "") or getattr(actor_entry, "tag", "")),
                voice_tag=str(old_obj.get("cutscene_actor_voice_tag", "") or getattr(actor_entry, "voice_tag", "")),
                final_position=str(old_obj.get("cutscene_actor_final_position", "") or getattr(actor_entry, "final_position", "")),
                kill_me=bool(old_obj.get("cutscene_actor_kill_me", getattr(actor_entry, "kill_me", False))),
                use_mimic=bool(old_obj.get("cutscene_actor_use_mimic", getattr(actor_entry, "use_mimic", False))),
                anim_final_pos=str(old_obj.get("cutscene_actor_anim_final_pos", "") or getattr(actor_entry, "anim_final_pos", "")),
                source_index=source_index,
            )
        except Exception as exc:
            log.exception("Failed to replace cutscene actor %s", getattr(old_obj, "name", "<unknown>"))
            self.report({'ERROR'}, f"Actor replace failed: {exc}")
            return {'CANCELLED'}

        _update_loaded_actor_entry_from_result(actor_entry, actor_info)
        actor_entry.template_path = template_path
        actor_entry.source_game = str(actor_info.get("source_game", "") or "").lower()

        applied_indices, error_messages = _rebuild_cutscene_actor_animations(scene, actor_entry)
        failed_indices = [idx for idx in requested_animation_indices if idx not in applied_indices]
        if failed_indices:
            first_error = str(error_messages.get(failed_indices[0], "") or "").strip()
            message = f"Replaced actor, but {len(failed_indices)} animation(s) failed to reload"
            if first_error:
                message = f"{message}: {first_error}"
            self.report({'WARNING'}, message)
        else:
            self.report({'INFO'}, f"Replaced actor with {source_game} template.")
        return {'FINISHED'}


class WITCH_OT_CutsceneRetargetW2ToW3(Operator):
    bl_idname = "witcher.cutscene_retarget_w2_to_w3"
    bl_label = "Replace W2 Actors"
    bl_description = "Replace loaded W2 cutscene camera/actors with the configured W3 camera, Geralt and Ciri templates"
    bl_options = {'REGISTER', 'UNDO'}

    replace_camera: BoolProperty(
        name="Camera",
        default=True,
        description="Replace the W2 cutscene camera entity with the configured W3 camera entity",
    )
    replace_actors: BoolProperty(
        name="Actors",
        default=True,
        description="Replace W2 male/female actors with configured W3 player templates",
    )

    def execute(self, context):
        scene = context.scene
        filepath = str(getattr(scene, "witcher_loaded_w2cutscene_path", "") or "").strip()
        if not filepath:
            self.report({'WARNING'}, "No cutscene loaded.")
            return {'CANCELLED'}
        if self.replace_actors and hasattr(scene, "witcher_w2_retarget_to_w3"):
            scene.witcher_w2_retarget_to_w3 = True

        retarget = _scene_cutscene_w2w3_retarget_options(scene)
        replaced = Counter()
        skipped = 0
        failed = 0
        failed_animation_count = 0

        actor_entries = list(getattr(scene, "witcher_cutscene_actor_items", []))
        for actor_entry in actor_entries:
            old_obj = _get_loaded_cutscene_actor_object(actor_entry)
            if old_obj is None:
                skipped += 1
                continue
            source_game = _loaded_cutscene_actor_source_game(old_obj, fallback=getattr(actor_entry, "source_game", ""))
            if source_game and source_game != "w2":
                skipped += 1
                continue
            kind = import_cutscene.infer_w2_cutscene_actor_retarget_kind(old_obj)
            if kind == "camera" and not self.replace_camera:
                skipped += 1
                continue
            if kind in {"male", "female"} and not self.replace_actors:
                skipped += 1
                continue
            if kind not in {"camera", "male", "female"}:
                skipped += 1
                continue

            template_path = import_cutscene.cutscene_w2w3_template_for_kind(kind, retarget)
            if not template_path:
                skipped += 1
                continue
            retarget_source_rig = (
                import_cutscene.get_cutscene_actor_w2_retarget_source_rig(old_obj, kind)
                if kind in {"male", "female"}
                else ""
            )
            requested_animation_indices = _loaded_actor_active_animation_indices(scene, actor_entry)
            try:
                actor_info = _replace_loaded_cutscene_actor_template(
                    scene,
                    actor_entry,
                    old_obj,
                    template_path,
                    retarget_kind=kind,
                    retarget_source_rig=retarget_source_rig,
                )
                applied_indices, error_messages = _rebuild_cutscene_actor_animations(scene, actor_entry)
                failed_indices = [idx for idx in requested_animation_indices if idx not in applied_indices]
                failed_animation_count += len(failed_indices)
                if failed_indices:
                    first_error = str(error_messages.get(failed_indices[0], "") or "").strip()
                    log.warning(
                        "Retargeted cutscene actor '%s', but %d animation(s) failed to reload%s",
                        getattr(actor_entry, "actor_name", ""),
                        len(failed_indices),
                        f": {first_error}" if first_error else "",
                    )
                replaced[kind] += 1
            except Exception:
                failed += 1
                log.exception("Failed to retarget cutscene actor '%s' to W3.", getattr(actor_entry, "actor_name", ""))

        if not replaced and failed <= 0:
            self.report({'WARNING'}, "No loaded W2 camera or male/female actors found.")
            return {'CANCELLED'}

        parts = []
        for key, label in (("camera", "camera"), ("male", "male"), ("female", "female")):
            count = int(replaced.get(key, 0) or 0)
            if count:
                parts.append(f"{count} {label}")
        message = "Replaced " + ", ".join(parts) if parts else "No actors replaced"
        if failed_animation_count:
            message += f"; {failed_animation_count} animation reload(s) failed"
        if failed:
            message += f"; {failed} actor(s) failed"
        if skipped and not parts:
            message += f"; {skipped} skipped"
        self.report({'WARNING' if failed or failed_animation_count else 'INFO'}, message)
        return {'FINISHED'}


class WITCH_OT_CutsceneRemoveAnimation(Operator):
    """Remove this clip and its NLA strips."""
    bl_idname = "witcher.cutscene_remove_animation"
    bl_label = "Remove Clip"
    bl_options = {'UNDO'}
    source_index: IntProperty(default=-1)

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        scene = context.scene
        source_index = _coerce_cutscene_index(self.source_index)
        if _find_loaded_cutscene_animation_entry(scene, source_index) is None:
            self.report({'WARNING'}, "Clip not found")
            return {'CANCELLED'}
        if bool((_clip_groups(scene).get(source_index) or {}).get("has_prebake", False)):
            self.report({'WARNING'}, "Pre-bake source clips are read-only")
            return {'CANCELLED'}
        _remove_cutscene_animation_entry(scene, source_index, remove_strips=True)
        sync_animation_items_from_scene(scene)
        return {'FINISHED'}


class WITCH_OT_CutsceneSetClipMuted(Operator):
    """Mute or unmute this clip for bake and export."""
    bl_idname = "witcher.cutscene_set_clip_muted"
    bl_label = "Mute / Unmute Clip"
    bl_options = {'REGISTER', 'UNDO'}

    source_index: IntProperty(default=-1)
    full_name: StringProperty(default="")
    mute: BoolProperty(default=True)

    def execute(self, context):
        scene = context.scene
        source_index = _coerce_cutscene_index(self.source_index)
        if source_index < 0 and self.full_name:
            matches = [
                _coerce_cutscene_index(getattr(item, "source_index", -1))
                for item in getattr(scene, "witcher_cutscene_animation_items", [])
                if str(getattr(item, "full_name", "") or "") == self.full_name
            ]
            if len(matches) == 1:
                source_index = matches[0]
        group = _clip_groups(scene).get(source_index) or {}
        strips = list(group.get("strips") or [])
        if not strips:
            self.report({'WARNING'}, "This clip has no strips in the scene")
            return {'CANCELLED'}
        if bool(group.get("has_prebake", False)):
            self.report({'WARNING'}, "Pre-bake source clips are read-only")
            return {'CANCELLED'}
        track_muted = bool(group.get("track_muted", False))
        for _track, strip in strips:
            strip.mute = self.mute
        sync_animation_items_from_scene(scene)
        if track_muted:
            verb = "muted" if self.mute else "unmuted"
            self.report({'WARNING'}, f"Clip strips {verb}; containing track is also muted")
        return {'FINISHED'}


class WITCH_OT_CutsceneBrowseAnimationClear(Operator):
    bl_idname = "witcher.cutscene_browse_animation_clear"
    bl_label = "Clear Animation Search"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        state = context.window_manager.witcher_cutscene_browse_animation
        if state.query:
            state.query = ""
        else:
            refresh_cutscene_browse_animation_results(state, context)
        return {'FINISHED'}


class WITCH_OT_CutsceneBrowseAddAnimation(Operator):
    """Add an authored clip from the game catalog."""
    bl_idname = "witcher.cutscene_browse_add_animation"
    bl_label = "Browse & Add Animation"
    bl_options = {'REGISTER', 'UNDO'}

    actor_object_name: StringProperty(name="Target", default="", options={'SKIP_SAVE'})
    animation_id: StringProperty(name="Animation", default="", options={'SKIP_SAVE'})
    source_path: StringProperty(name="File", default="", options={'SKIP_SAVE'})
    source_game: EnumProperty(
        name="Source",
        items=[("w3", "W3", "Witcher 3"), ("w2", "W2", "Witcher 2")],
        default="w3",
        options={'SKIP_SAVE'},
    )
    component: EnumProperty(
        name="Component",
        items=[("BODY", "Body", "Body animation"), ("FACE", "Face", "Face animation")],
        default="BODY",
        options={'SKIP_SAVE'},
    )
    placement: EnumProperty(
        name="Placement",
        items=[
            ("CURRENT", "At Current Frame", "Place at the current frame"),
            ("AFTER_LAST", "After Last Clip", "Place after the relevant cutscene tracks"),
        ],
        default="CURRENT",
        options={'SKIP_SAVE'},
    )

    def invoke(self, context, event):
        actor = _active_cutscene_actor_armature(context.scene)
        if actor is None:
            self.report({'WARNING'}, "Select an actor in the Actors tab first")
            return {'CANCELLED'}
        self.actor_object_name = actor.name
        reset_cutscene_browse_animation_dialog(self, context)
        return context.window_manager.invoke_props_dialog(self, width=680, confirm_text="Add to Cutscene")

    def draw(self, context):
        layout = self.layout
        state = context.window_manager.witcher_cutscene_browse_animation
        target = layout.row(align=True)
        _draw_cutscene_exact_value(
            target,
            self,
            "actor_object_name",
            text="Target",
            value=self.actor_object_name,
        )
        layout.prop(state, "component", expand=True)

        search = layout.row(align=True)
        search.prop(state, "query")
        search.operator(WITCH_OT_CutsceneBrowseAnimationClear.bl_idname, text="", icon='X')
        filters = layout.row(align=True)
        filters.prop(state, "source")
        filters.prop(state, "compatibility")
        filters.prop(state, "category")
        if state.match_status:
            layout.label(text=state.match_status, icon='INFO')
        layout.template_list(
            WITCH_UL_CutsceneBrowseAnimations.bl_idname,
            "",
            state,
            "results",
            state,
            "result_index",
            rows=10,
        )

        selected = layout.box()
        selected.label(text="Selected", icon='ACTION')
        if state.selected_animation_id:
            for prop_name, label in (
                ("selected_animation_id", "Animation"),
                ("selected_caption", "Caption"),
                ("selected_repo_path", "File"),
            ):
                row = selected.row(align=True)
                _draw_cutscene_exact_value(
                    row,
                    state,
                    prop_name,
                    text=label,
                    value=getattr(state, prop_name, ""),
                )
            selected.label(
                text=f"{state.selected_frames} frames · {state.selected_duration:.2f}s · {state.selected_component.title()}",
                icon='TIME',
            )
        else:
            selected.label(text="Select an animation", icon='INFO')
        layout.prop(state, "placement", expand=True)

    def execute(self, context):
        state = context.window_manager.witcher_cutscene_browse_animation
        if not self.animation_id:
            self.actor_object_name = str(state.actor_object_name or self.actor_object_name)
            self.animation_id = str(state.selected_animation_id or "")
            self.source_path = str(state.selected_repo_path or "")
            self.source_game = str(state.selected_source_game or "w3").lower()
            self.component = str(state.component or "BODY")
            self.placement = str(state.placement or "CURRENT")
        actor = bpy.data.objects.get(str(self.actor_object_name or ""))
        if actor is None or getattr(actor, "type", None) != 'ARMATURE':
            self.report({'ERROR'}, "Target actor is unavailable")
            return {'CANCELLED'}
        if not self.animation_id or not self.source_path:
            self.report({'ERROR'}, "Select an animation first")
            return {'CANCELLED'}
        try:
            from . import ui_anims
            result = ui_anims.add_catalog_animation_to_cutscene(
                context,
                actor,
                self.animation_id,
                self.source_path,
                source_game=self.source_game,
                component=self.component,
                placement=self.placement,
            )
            row = context.scene.witcher_cutscene_animation_items[int(result["row_index"])]
            row.frames_per_second = _scene_fps(context.scene)
            row.duration = row.num_frames / row.frames_per_second if row.frames_per_second else 0.0
        except Exception as exc:
            log.exception("Browse & Add animation failed")
            self.report({'ERROR'}, f"Could not add '{self.animation_id}': {exc}")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Added '{self.animation_id}' as clip {result['source_index']}")
        return {'FINISHED'}


def _add_event_class_items(self, context):
    scope = str(getattr(self, "event_scope", "") or "")
    if scope == "ROOT":
        return _ANIM_EVENT_ENUM_ITEMS_ROOT
    if scope == "ENTRY":
        return _ANIM_EVENT_ENUM_ITEMS_ENTRY
    return _ANIM_EVENT_ENUM_ITEMS_ALL


class WITCH_OT_CutsceneAddEvent(Operator):
    """Add a new event to the cutscene or animation event list"""
    bl_idname = "witcher.cutscene_add_event"
    bl_label = "Add Event"
    bl_options = {'UNDO'}

    event_scope: EnumProperty(
        name="Scope",
        items=[("ROOT", "Cutscene (ROOT)", "Attached to the cutscene template"),
               ("ENTRY", "Animation (ENTRY)", "Attached to a specific animation entry")],
        default="ENTRY",
    )
    event_class: EnumProperty(name="Event Class", items=_add_event_class_items)
    source_index: IntProperty(name="Animation Source Index", default=-1)

    # CExtAnimEvent base fields
    event_name: StringProperty(name="eventName", default="")
    start_time: FloatProperty(name="startTime", default=0.0, min=0.0)
    animation_name: StringProperty(name="animationName", default="")
    report_to_script: BoolProperty(name="reportToScript", default=False)
    report_min_weight: FloatProperty(name="reportToScriptMinWeight", default=0.0, min=0.0, max=1.0)

    # CExtAnimDurationEvent
    duration: FloatProperty(name="duration", default=0.0, min=0.0)
    always_fires_end: BoolProperty(name="alwaysFiresEnd", default=False)

    # Own fields (stored in CutsceneEventItem)
    appearance: StringProperty(name="appearance (CName)", default="")
    effect_name: StringProperty(name="effectName / effect (CName)", default="")

    # Extra informational fields (displayed but not stored beyond event_type)
    extra_str1: StringProperty(name="", default="")
    extra_str2: StringProperty(name="", default="")

    def invoke(self, context, event):
        scene = context.scene
        # Pre-fill scope and source_index from context
        if self.event_scope == "ENTRY" and self.source_index < 0:
            target = _event_target_index(scene)
            if target >= 0:
                self.source_index = target
                anim = _find_loaded_cutscene_animation_entry(scene, target)
                if anim is not None and not self.animation_name:
                    self.animation_name = str(anim.display_name or "")
        return context.window_manager.invoke_props_dialog(self, width=460)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True

        layout.prop(self, "event_scope")
        layout.prop(self, "event_class")

        schema_entry = _EVENT_SCHEMA_BY_CLASS.get(self.event_class)
        if schema_entry:
            base_row = layout.row()
            base_row.enabled = False
            base_row.label(text=f"↑ {schema_entry[1]}", icon='BLANK1')

        layout.separator()
        layout.label(text="Base fields  (CExtAnimEvent):", icon='PROPERTIES')
        layout.prop(self, "event_name")
        layout.prop(self, "start_time")
        layout.prop(self, "animation_name")
        layout.prop(self, "report_to_script")
        if self.report_to_script:
            layout.prop(self, "report_min_weight")

        if _event_schema_has_duration(self.event_class):
            layout.separator()
            layout.label(text="Duration fields  (CExtAnimDurationEvent):", icon='PROPERTIES')
            layout.prop(self, "duration")
            layout.prop(self, "always_fires_end")

        if schema_entry:
            own_props = schema_entry[3]
            if own_props:
                layout.separator()
                layout.label(text=f"Own fields  ({self.event_class}):", icon='PROPERTIES')
                for prop_name, prop_type in own_props:
                    if prop_name in ("duration", "alwaysFiresEnd"):
                        continue
                    if prop_name == "appearance":
                        layout.prop(self, "appearance")
                    elif prop_name in ("effectName", "effect"):
                        layout.prop(self, "effect_name")
                    else:
                        # Field not stored — show label only
                        row = layout.row()
                        row.enabled = False
                        row.label(text=f"{prop_name} ({prop_type}) — edit in game files", icon='INFO')

        if self.event_scope == "ENTRY":
            layout.separator()
            info = layout.row()
            info.enabled = False
            info.label(text=f"Animation source index: {self.source_index}", icon='INFO')

    def execute(self, context):
        scene = context.scene
        if self.event_scope == "ENTRY":
            sync_animation_items_from_scene(scene)
            if _find_loaded_cutscene_animation_entry(scene, self.source_index) is None:
                self.report({'WARNING'}, "Choose a valid clip for this ENTRY event")
                return {'CANCELLED'}
        new_ev = scene.witcher_cutscene_event_items.add()
        new_ev.event_type = self.event_class
        new_ev.event_name = self.event_name
        new_ev.start_time = self.start_time
        new_ev.duration = self.duration
        new_ev.animation_name = self.animation_name
        new_ev.appearance = self.appearance
        new_ev.effect_name = self.effect_name
        new_ev.always_fires_end = self.always_fires_end
        new_ev.event_scope = self.event_scope
        new_ev.source_index = self.source_index
        return {'FINISHED'}


_ECutsceneActorType_ITEMS = [
    ("CAT_Actor",  "CAT_Actor",  "Animated actor"),
    ("CAT_Prop",   "CAT_Prop",   "Animated prop"),
    ("CAT_Camera", "CAT_Camera", "Cutscene camera"),
    ("CAT_None",   "CAT_None",   "No type"),
]
_ECutsceneActorType_VALUES = [item[0] for item in _ECutsceneActorType_ITEMS]
_ECutsceneActorType_INDEX = {v: i for i, v in enumerate(_ECutsceneActorType_VALUES)}
_CUTSCENE_ACTOR_REPLACE_SOURCE_ITEMS = [
    ("W3", "W3", "Resolve replacement actor templates from the Witcher 3 repo"),
    ("W2", "W2", "Resolve replacement actor templates from the Witcher 2 repo"),
    ("REDKIT", "REDkit", "Resolve replacement actor templates from the active REDkit project/depot roots"),
]


def _actor_type_get(self):
    val = str(self.get("cutscene_actor_type", "CAT_Actor") or "CAT_Actor")
    return _ECutsceneActorType_INDEX.get(val, 0)


def _actor_type_set(self, value):
    if 0 <= value < len(_ECutsceneActorType_VALUES):
        self["cutscene_actor_type"] = _ECutsceneActorType_VALUES[value]


def _draw_cutscene_burned_track(layout, scene):
    header = layout.row(align=True)
    burned_strip = _get_loaded_cutscene_burned_audio_strip(scene)
    burned_event = str(getattr(scene, "witcher_cutscene_burned_audio_event", "") or "").strip()
    burned_item_path = str(getattr(scene, "witcher_cutscene_burned_audio_item_path", "") or "").strip()
    if burned_strip is not None:
        header.label(text="Loaded", icon='CHECKMARK')
        header.operator(WITCH_OT_ImportCutsceneBurnedAudio.bl_idname, text="", icon='FILE_REFRESH')
        header.operator(WITCH_OT_RemoveCutsceneBurnedAudio.bl_idname, text="", icon='X')
    elif burned_event:
        header.label(text="Not Loaded", icon='INFO')
        header.operator(WITCH_OT_ImportCutsceneBurnedAudio.bl_idname, text="Import", icon='IMPORT')
    else:
        header.label(text="None", icon='INFO')
    layout.prop(scene, "witcher_cutscene_burned_audio_default_volume", text="Default Import Volume", slider=True)
    if burned_event:
        event_row = layout.row(align=True)
        _draw_cutscene_exact_value(
            event_row,
            scene,
            "witcher_cutscene_burned_audio_event",
            text="Event",
            value=burned_event,
        )
    else:
        layout.label(text="No burned track defined in this cutscene.", icon='INFO')
    if burned_item_path:
        item_row = layout.row(align=True)
        _draw_cutscene_exact_value(
            item_row,
            scene,
            "witcher_cutscene_burned_audio_item_path",
            text="Item",
            value=burned_item_path,
        )
    if burned_strip is not None and hasattr(burned_strip, "volume"):
        layout.prop(burned_strip, "volume", text="Sequencer Volume", slider=True)


def _validation_report_summary(report):
    lines = str(report or "").strip().splitlines()
    errors = sum(line.startswith("ERROR") for line in lines)
    warnings = sum(line.startswith(("WARN", "WARNING")) for line in lines)
    if errors:
        text = f"{errors} error{'s' if errors != 1 else ''}"
        if warnings:
            text += f" · {warnings} warning{'s' if warnings != 1 else ''}"
        return text, 'ERROR'
    if warnings:
        return f"{warnings} warning{'s' if warnings != 1 else ''}", 'INFO'
    return ("Validation passed", 'CHECKMARK') if lines else ("", 'NONE')


def _draw_cutscene_export_tab(layout, scene, context):
    layout.use_property_split = False
    layout.use_property_decorate = False
    layout.prop(scene, "witcher_cutscene_export_repo_path", text="Game path")
    if str(getattr(scene, "witcher_loaded_w2cutscene_path", "") or "").strip():
        loaded_path = str(getattr(scene, "witcher_loaded_w2cutscene_path", "") or "")
        loaded_row = layout.row(align=True)
        _draw_cutscene_exact_value(
            loaded_row,
            scene,
            "witcher_loaded_w2cutscene_path",
            text="Loaded File",
            value=loaded_path,
        )
        seconds = float(getattr(scene, "witcher_cutscene_last_import_seconds", 0.0) or 0.0)
        layout.label(text=f"Imported in {seconds:.2f}s", icon='INFO')

    state = cutscene_bake.bake_state(scene)
    prop_items = cutscene_bake.iter_prop_objects(scene)
    can_bake = bool(state["target_count"] or prop_items)
    steps = layout.box()
    bake_row = steps.row(align=True)
    bake_action = bake_row.row(align=True)
    bake_action.enabled = can_bake
    bake_action.operator(WITCH_OT_CutsceneBakeAll.bl_idname, text="1  Bake actors", icon='REC')
    bake_row.operator(WITCH_OT_CutsceneSetSceneRange.bl_idname, text="", icon='PREVIEW_RANGE')
    if not can_bake:
        bake_row.label(text="No actors or props to bake", icon='INFO')
    start, end = state["range"]
    bake_text = f"baked {state['baked_count']}/{state['target_count']} · frames {start}-{end}"
    if state["stale"]:
        bake_text += " · stale"
    steps.label(text=bake_text, icon='CHECKMARK' if state["baked"] and not state["stale"] else 'BLANK1')
    steps.operator(WITCH_OT_CutsceneValidate.bl_idname, text="2  Validate", icon='CHECKMARK')
    summary, summary_icon = _validation_report_summary(
        getattr(scene, "witcher_cutscene_validation_report", "")
    )
    if summary:
        report_row = steps.row(align=True)
        report_row.label(text=summary, icon=summary_icon)
        report_row.operator(WITCH_OT_CutsceneValidationReport.bl_idname, text="Report", icon='COPYDOWN')
        issues = getattr(scene, "witcher_cutscene_validation_issues", None)
        if issues is not None and len(issues):
            steps.template_list(
                "WITCH_UL_CutsceneValidationIssues", "", scene, "witcher_cutscene_validation_issues",
                scene, "witcher_cutscene_validation_issue_index", rows=min(max(len(issues), 2), 6),
            )
    steps.operator("witcher.export_w2_cutscene", text="3  Export .w2cutscene", icon='EXPORT')
    props_row = steps.row(align=True)
    props_action = props_row.row(align=True)
    props_action.enabled = bool(prop_items)
    props_action.operator(WITCH_OT_CutsceneGeneratePropsEntity.bl_idname, text="4  Write props .w2ent", icon='OBJECT_DATA')
    if not prop_items:
        props_row.label(text="No props assigned (Actors tab › Props)", icon='INFO')
    steps.operator(WITCH_OT_CutsceneExportSceneWrapper.bl_idname, text="5  Write REDkit .w2scene", icon='SCENE_DATA')

    def section(section_id, label, icon):
        header, body = layout.box().panel(section_id, default_closed=True)
        header.label(text=label, icon=icon)
        return body

    body = section("witcher_cs_export_metadata", "Metadata", 'TOOL_SETTINGS')
    if body:
        body.prop(scene, "witcher_cutscene_point_tags", text="Point Tags")
        body.prop(scene, "witcher_cutscene_last_level_loaded", text="Last Level Loaded")
        body.prop(scene, "witcher_cutscene_used_in_files", text="Used In Files")
        body.prop(scene, "witcher_cutscene_burned_audio_event", text="Burned Audio Event")
        body.prop(scene, "witcher_cutscene_dialog_id_space", text="Fallback ID Space")
        strings_path = str(getattr(scene, "witcher_cutscene_dialog_strings_path", "") or "").strip()
        if strings_path:
            _draw_cutscene_exact_value(
                body,
                scene,
                "witcher_cutscene_dialog_strings_path",
                text="Strings CSV",
                value=strings_path,
            )
        body.label(text="Use ';' to separate multiple tags or depot paths.", icon='INFO')
    body = section("witcher_cs_export_burned", "Burned track", 'SOUND')
    if body:
        _draw_cutscene_burned_track(body, scene)
    body = section("witcher_cs_export_template_fields", "Template fields (raw)", 'PROPERTIES')
    if body:
        _draw_imported_class_sections(
            body,
            list(getattr(scene, "witcher_cutscene_template_fields", [])),
            w3_types.CUTSCENE_CLASS_FIELD_SCHEMA,
            False,
            "No set imported values.",
            per_class_show_unset=True,
        )
        effects = list(getattr(scene, "witcher_cutscene_effect_items", []))
        if effects:
            body.label(text=f"Effects ({len(effects)})", icon='PARTICLES')
            for eff in effects[:12]:
                body.prop(eff, "name", text="", emboss=False, icon='DOT')
            if len(effects) > 12:
                body.label(text=f"{len(effects) - 12} more not shown.", icon='INFO')


def _selection_has_armature(context):
    for obj in [getattr(context, "active_object", None)] + list(getattr(context, "selected_objects", None) or []):
        while obj is not None:
            if getattr(obj, "type", None) == 'ARMATURE':
                return True
            obj = obj.parent
    return False


def _draw_cutscene_actors_tab(layout, scene, context=None):
    if _scene_needs_actor_sync(scene):
        _schedule_actor_items_sync(scene)

    add_box = layout.box()
    add_box.label(text="Add actor", icon='ADD')
    add_box.operator("witcher.cast_actor", text="Cast Actor…", icon='OUTLINER_OB_ARMATURE')
    header, body = add_box.panel("witcher_cs_actors_tag_form", default_closed=True)
    header.label(text="Tag selected armature instead", icon='ARMATURE_DATA')
    if body:
        body.prop(scene, "witcher_cutscene_scratch_actor_name", text="Name")
        body.prop(scene, "witcher_cutscene_scratch_actor_type", text="Type")
        body.prop(scene, "witcher_cutscene_scratch_actor_template", text="Template")
        body.prop(scene, "witcher_cutscene_scratch_actor_appearance", text="Appearance")
        body.prop(scene, "witcher_cutscene_scratch_use_mimic", text="Face data (mimic)")
        can_assign = context is not None and _selection_has_armature(context)
        assign_row = body.row(align=True)
        assign_action = assign_row.row(align=True)
        assign_action.enabled = can_assign
        assign_action.operator("witcher.cutscene_scratch_assign_actor", text="Assign Selected", icon='CHECKMARK')
        if not can_assign:
            assign_row.label(text="Select an armature in the viewport", icon='INFO')

    items = list(getattr(scene, "witcher_cutscene_actor_items", []) or [])
    idx = int(getattr(scene, "witcher_cutscene_loaded_actor_index", 0) or 0)
    if idx < 0 or idx >= len(items):
        idx = max(0, len(items) - 1)
    active_entry = items[idx] if items else None
    if items:
        layout.template_list(
            "WITCH_UL_LoadedActorList", "",
            scene, "witcher_cutscene_actor_items",
            scene, "witcher_cutscene_loaded_actor_index",
            rows=min(len(items), 8),
        )
    else:
        layout.label(text="No actors in cutscene.", icon='INFO')

    selected_obj = _get_loaded_cutscene_actor_object(active_entry) if active_entry else None
    if active_entry is not None:
        is_loaded = selected_obj is not None
        src = int(getattr(active_entry, "source_index", -1))
        obj_name = str(getattr(active_entry, "object_name", "") or "")
        name = str(getattr(active_entry, "actor_name", "") or "")
        action_row = layout.row(align=True)
        if src >= 0:
            toggle_op = action_row.operator(
                WITCH_OT_SetCutsceneActorLoaded.bl_idname,
                text="Unload entity" if is_loaded else "Load entity",
                icon='HIDE_ON' if is_loaded else 'HIDE_OFF',
            )
            toggle_op.load = not is_loaded
            toggle_op.source_index, toggle_op.object_name, toggle_op.actor_name = src, obj_name, name
        if selected_obj is not None:
            replace_op = action_row.operator("witcher.cutscene_replace_actor", text="Replace…", icon='FILE_REFRESH')
            replace_op.object_name = selected_obj.name
        rm_op = action_row.operator(WITCH_OT_CutsceneRemoveActorFull.bl_idname, text="Remove…", icon='X')
        rm_op.source_index, rm_op.object_name, rm_op.actor_name = src, obj_name, name
        if is_loaded:
            root_loaded, root_total = _actor_animation_layer_state(scene, active_entry, "ROOT")
            face_loaded, face_total = _actor_animation_layer_state(scene, active_entry, "FACE")
            if root_total or face_total:
                anim_row = layout.row(align=True)
                anim_row.label(text="Clips", icon='ACTION')
                for layer, label, loaded_count, total_count in (
                    ("ROOT", "Root", root_loaded, root_total),
                    ("FACE", "Face", face_loaded, face_total),
                ):
                    if not total_count:
                        continue
                    layer_op = anim_row.operator(
                        WITCH_OT_SetCutsceneActorAnimationLayer.bl_idname,
                        text=f"{label} {loaded_count}/{total_count}",
                        icon='HIDE_ON' if loaded_count else 'HIDE_OFF',
                    )
                    layer_op.source_index, layer_op.object_name, layer_op.actor_name = src, obj_name, name
                    layer_op.layer = layer
                    layer_op.load = not bool(loaded_count)
            header, body = layout.box().panel("witcher_cs_actor_properties", default_closed=True)
            header.label(text="Properties", icon='PROPERTIES')
            if body:
                col = body.column(align=True)
                _draw_actor_key(col, selected_obj, "cutscene_actor_name", "Name")
                col.prop(selected_obj, "witcher_cutscene_actor_type", text="Type")
                _draw_actor_key(col, selected_obj, "cutscene_actor_template", "Template")
                _draw_actor_key(col, selected_obj, "cutscene_actor_appearance", "Appearance")
                _draw_actor_key(col, selected_obj, "cutscene_actor_use_mimic", "Face Data (Mimic)")
                _draw_actor_key(col, selected_obj, "cutscene_actor_tag", "Tag")
                _draw_actor_key(col, selected_obj, "cutscene_actor_voice_tag", "Voice Tag")
                _draw_actor_key(col, selected_obj, "cutscene_actor_final_position", "Final Position")
                _draw_actor_key(col, selected_obj, "cutscene_actor_anim_final_pos", "Anim At Final Position")
                _draw_actor_key(col, selected_obj, "cutscene_actor_kill_me", "Kill Me")

    prop_items = cutscene_bake.iter_prop_objects(scene)
    prop_arm = cutscene_bake.find_prop_actor(scene)
    props_box = layout.box()
    props_header = props_box.row(align=True)
    props_header.label(text=f"Props ({len(prop_items)})", icon='OBJECT_DATA')
    mesh_selected = context is not None and any(
        getattr(o, "type", None) == 'MESH' for o in getattr(context, "selected_objects", None) or [])
    add_row = props_box.row(align=True)
    add_action = add_row.row(align=True)
    add_action.enabled = mesh_selected
    add_action.operator(WITCH_OT_CutsceneAddProps.bl_idname, text="Add Selected", icon='ADD')
    if not mesh_selected:
        add_row.label(text="Select a mesh in the viewport", icon='INFO')
    grip_row = props_box.row(align=True)
    grip_action = grip_row.row(align=True)
    grip_action.enabled = bool(mesh_selected and selected_obj is not None)
    grip_action.operator(WITCH_OT_CutsceneGripProp.bl_idname, text="Grip in Hand…", icon='VIEW_PAN')
    if mesh_selected and selected_obj is None:
        grip_row.label(text="Select an actor above", icon='INFO')
    for prop_obj, slot in prop_items:
        row = props_box.row(align=True)
        _draw_cutscene_exact_value(
            row,
            prop_obj,
            f'["{cutscene_bake.TRAJECTORY_SLOT_PROP}"]',
            field_label="Slot",
            value=slot,
            text="",
            icon='BONE_DATA',
        )
        _draw_cutscene_exact_value(
            row,
            prop_obj,
            "name",
            field_label="Object",
            value=prop_obj.name,
            text="",
            icon='MESH_DATA',
        )
        rm = row.operator(WITCH_OT_CutsceneRemoveProp.bl_idname, text="", icon='X')
        rm.object_name = prop_obj.name
    if prop_items:
        if prop_arm is not None and "cutscene_actor_template" in prop_arm:
            props_box.prop(prop_arm, '["cutscene_actor_template"]', text="Entity")
        props_box.label(text="Write the props .w2ent in Export step 4", icon='INFO')

    if any(str(getattr(it, "source_game", "") or "").lower() == "w2"
           or _loaded_cutscene_actor_source_game(_get_loaded_cutscene_actor_object(it)) == "w2" for it in items):
        header, body = layout.box().panel("witcher_cs_actors_w2_retarget", default_closed=True)
        header.label(text="Witcher 2 retarget", icon='ARMATURE_DATA')
        if body:
            body.prop(scene, "witcher_cutscene_retarget_camera_template", text="Camera")
            body.prop(scene, "witcher_cutscene_retarget_male_template", text="Male")
            body.prop(scene, "witcher_cutscene_retarget_female_template", text="Female")
            row = body.row(align=True)
            actor_op = row.operator(WITCH_OT_CutsceneRetargetW2ToW3.bl_idname, text="Retarget actors", icon='FILE_REFRESH')
            actor_op.replace_camera = False
            actor_op.replace_actors = True
            camera_op = row.operator(WITCH_OT_CutsceneRetargetW2ToW3.bl_idname, text="Retarget camera", icon='CAMERA_DATA')
            camera_op.replace_camera = True
            camera_op.replace_actors = False


def _draw_cutscene_anims_tab(layout, scene, groups=None, state=None):
    if groups is None:
        groups = _clip_groups(scene)
    if state is None:
        state = cutscene_bake.bake_state(scene)
    if _clip_signature(scene, groups) != _ANIM_SYNC_SIGNATURES.get(str(scene.name)):
        _schedule_animation_items_sync(scene)
    anims = list(getattr(scene, "witcher_cutscene_animation_items", []))
    idx = int(getattr(scene, "witcher_cutscene_loaded_anim_index", 0) or 0)
    real_anims = [a for a in anims if a.source_index != -1]
    filepath = str(getattr(scene, "witcher_loaded_w2cutscene_path", "") or "").strip()

    inputs = cutscene_bake.bake_inputs(scene)
    if inputs["strips"]:
        layout.label(
            text=f"Bake folds {inputs['strips']} unmuted strip(s) on {len(inputs['lanes'])} cutscene_anim track(s) into one action per actor",
            icon='REC',
        )
    elif state["baked"]:
        layout.label(
            text=("Baked · stale, re-bake" if state["stale"] else "Baked") + " · sources kept muted as *_prebake",
            icon='CHECKMARK',
        )
    for label, track_name, _object_name in inputs["foreign"]:
        layout.label(text=f"{label}: track '{track_name}' is unmuted, Bake folds it in too", icon='ERROR')

    if anims:
        layout.template_list(
            "WITCH_UL_LoadedAnimList", "",
            scene, "witcher_cutscene_animation_items",
            scene, "witcher_cutscene_loaded_anim_index",
            rows=min(len(anims), 8),
        )
        if 0 <= idx < len(anims) and anims[idx].source_index != -1:
            anim = anims[idx]
            g = groups.get(int(anim.source_index))
            actor_entry = _find_actor_entry_for_animation(scene, anim)
            actor_obj = _get_loaded_cutscene_actor_object(actor_entry)
            has_prebake = bool(g and g.get("has_prebake", False))
            detail_box = layout.box()
            detail_row = detail_box.row(align=True)
            detail_row.label(text=_get_cutscene_animation_label(anim), icon='ACTION')
            if g is None:
                if actor_obj is None and filepath and actor_entry is not None and int(getattr(actor_entry, "source_index", -1)) >= 0:
                    imp_op = detail_row.operator(WITCH_OT_CutsceneImportActorForAnim.bl_idname, text="Import Actor", icon='IMPORT')
                    imp_op.source_index = anim.source_index
                elif actor_obj is None:
                    detail_row.label(text="Actor missing", icon='ERROR')
                elif filepath and bool(getattr(anim, "file_backed", False)):
                    op = detail_row.operator(WITCH_OT_SetCutsceneAnimationLoaded.bl_idname, text="Load clip", icon='IMPORT')
                    op.source_index = anim.source_index
                    op.load = True
                else:
                    detail_row.label(text="No strips in scene", icon='INFO')
            elif has_prebake:
                detail_row.label(text="Pre-bake source · read-only", icon='CHECKMARK')
            else:
                op = detail_row.operator(WITCH_OT_CutsceneSetClipMuted.bl_idname,
                                         text="Unmute" if g["muted"] else "Mute",
                                         icon='HIDE_OFF' if g["muted"] else 'HIDE_ON')
                op.source_index = int(anim.source_index)
                op.mute = not bool(g["muted"])
            if not has_prebake:
                rm_op = detail_row.operator("witcher.cutscene_remove_animation", text="", icon='X')
                rm_op.source_index = anim.source_index
            col = detail_box.column(align=True)
            if anim.actor_name:
                actor_field = col.row(align=True)
                _draw_cutscene_exact_value(
                    actor_field,
                    anim,
                    "actor_name",
                    text="Actor",
                    value=anim.actor_name,
                    icon='ARMATURE_DATA',
                )
            if anim.component_name:
                component_field = col.row(align=True)
                _draw_cutscene_exact_value(
                    component_field,
                    anim,
                    "component_name",
                    text="Component",
                    value=anim.component_name,
                    icon='BONE_DATA',
                )
            if g:
                start = min(float(s.frame_start) for _t, s in g["strips"])
                end = max(float(s.frame_end) for _t, s in g["strips"])
                tracks = sorted({str(t.name).replace(cutscene_bake.BAKE_BACKUP_SUFFIX, "") for t, _s in g["strips"]})
                col.label(text=f"Frames {int(start)}-{int(end)} · {len(g['strips'])} strip(s) on {', '.join(tracks)}", icon='NLA')
                if has_prebake:
                    col.label(text="Baked · stale, re-bake" if state["stale"] else f"Baked into {cutscene_bake.BAKE_TRACK_NAME}", icon='CHECKMARK')
                elif g["muted"] or g["track_muted"]:
                    col.label(text="Muted · Bake skips it", icon='HIDE_ON')
                else:
                    col.label(text="Goes into the next Bake", icon='REC')
                if g["track_muted"]:
                    col.label(text="Track muted", icon='HIDE_ON')
            if anim.frames_per_second:
                col.label(text=f"FPS: {anim.frames_per_second:.1f}   Frames: {anim.num_frames}")
            if anim.duration:
                col.label(text=f"Duration: {anim.duration:.3f}s")
    else:
        layout.label(text="No clips yet.", icon='INFO')
    if real_anims:
        layout.label(text=f"Unmuted: {sum(1 for a in real_anims if a.is_loaded and not a.muted)}/{len(real_anims)}")

    layout.separator(factor=0.5)
    target_arm = _active_cutscene_actor_armature(scene)
    add_box = layout.box()
    actor_label = str(target_arm.get("cutscene_actor_name", target_arm.name)) if target_arm is not None else ""
    add_box.label(text=f"Add clip for {actor_label}" if actor_label else "Add clip", icon='NLA')
    browse_row = add_box.row(align=True)
    browse_action = browse_row.row(align=True)
    browse_action.enabled = target_arm is not None
    browse_op = browse_action.operator(
        WITCH_OT_CutsceneBrowseAddAnimation.bl_idname,
        text="Browse & Add Animation…",
        icon='VIEWZOOM',
    )
    browse_op.actor_object_name = str(getattr(target_arm, "name", "") or "")
    if target_arm is None:
        browse_row.label(text="Select an actor in the Actors tab", icon='INFO')
    add_box.prop_search(scene, "witcher_cutscene_scratch_action_name", bpy.data, "actions", text="Action")
    add_box.prop(scene, "witcher_cutscene_scratch_component", text="Component")
    opts_row = add_box.row(align=True)
    opts_row.prop(scene, "witcher_cutscene_scratch_strip_length", text="Length (0 = action)")
    opts_row.prop(scene, "witcher_cutscene_scratch_add_after_last", text="After last strip")
    action_name = str(getattr(scene, "witcher_cutscene_scratch_action_name", "") or "").strip()
    has_action = bool(action_name and bpy.data.actions.get(action_name))
    if target_arm is not None and not has_action:
        has_action = any(
            getattr(getattr(holder, "animation_data", None), "action", None) is not None
            for holder in import_cutscene._iter_cutscene_related_armatures(target_arm)
        )
    manual_row = add_box.row(align=True)
    manual_action = manual_row.row(align=True)
    manual_action.enabled = bool(target_arm is not None and has_action)
    manual_action.operator("witcher.cutscene_scratch_add_action", text="Add to Cutscene", icon='ADD')
    if target_arm is not None and not has_action:
        manual_row.label(text="Choose an action", icon='INFO')

    fallback_header, qb_box = layout.panel("witcher_cutscene_loaded_strip_fallback", default_closed=True)
    fallback_header.label(text="Use loaded strips (fallback)", icon='NLA')
    if qb_box is None:
        return
    import_candidates = _collect_cutscene_import_nla_candidates(scene)
    for cand in import_candidates[:8]:
        row = qb_box.row(align=True)
        label_name = cand["action_name"] or cand["strip_name"] or cand["track_name"]
        row.label(
            text=f"{cand['actor_name']} {cand['component']}: {label_name}  [{int(round(cand['frame_start']))}-{int(round(cand['frame_end']))}]",
            icon='ACTION',
        )
        op = row.operator("witcher.cutscene_use_import_nla_strip", text="Use", icon='CHECKMARK')
        op.actor_object_name = cand["actor_object_name"]
        op.source_object_name = cand["source_object_name"]
        op.track_name = cand["track_name"]
        op.strip_name = cand["strip_name"]
        op.source_frame_start = cand["frame_start"]
        op.component = cand["component"]
        op.mute_source = True
    if len(import_candidates) > 8:
        qb_box.label(text=f"{len(import_candidates) - 8} more loaded clips not shown.", icon='INFO')
    elif not import_candidates:
        qb_box.label(text="No loaded clips on cutscene actors yet.", icon='BLANK1')


def _draw_camera_track_controls(layout, camera_arm):
    camera_bone = camera_arm.pose.bones.get(CAMERA_CONTROL_BONE)
    if camera_bone is None:
        layout.label(text="Camera rig is missing Camera_Node track properties.", icon='ERROR')
        return
    tracks_box = layout.box()
    tracks_box.label(text="Camera Tracks", icon='ANIM')
    tracks_box.operator("witcher.camera_set_dof_from_selected", text="DOF From Selected", icon='CAMERA_DATA')
    for track_name in CAMERA_TRACK_NAMES:
        if track_name in camera_bone:
            tracks_box.prop(camera_bone, f'["{track_name}"]', text=CAMERA_TRACK_LABELS.get(track_name, track_name))


def _draw_cutscene_camera_tab(layout, scene, context):
    from . import ui_anims  # ui_anims -> ui_scene -> ui_cutscene at module level; keep this side lazy

    camera_arm = _cs_find_camera_armature(context)
    cuts = ui_anims._iter_camera_cut_strips(camera_arm) if camera_arm is not None else []
    shots = cutscene_bake.shot_ranges(scene)
    frame = int(getattr(scene, "frame_current", 0))

    shots_box = layout.box()
    shots_hdr = shots_box.row(align=True)
    shots_hdr.label(text="Shots", icon='CAMERA_DATA')
    shots_hdr.operator("witcher.cutscene_new_shot", text="New Shot", icon='ADD')
    shots_hdr.operator("witcher.cutscene_use_selected_camera_as_shot", text="Use Selected", icon='CHECKMARK')
    for shot_idx, cam, start, end in shots:
        skipped = end <= start
        row = shots_box.row(align=True)
        row.alert = skipped
        row.label(text="", icon='ERROR' if skipped else ('RADIOBUT_ON' if start <= frame <= end else 'RADIOBUT_OFF'))
        _draw_cutscene_exact_value(row, cam, "name", field_label="Shot", value=cam.name, text="")
        _cutscene_fixed_label(row, f"{start} skipped" if skipped else f"{start}–{end}", units=4.5)
        row.operator("witcher.cutscene_jump_to_shot", text="", icon='VIEW_CAMERA').shot_index = shot_idx
        row.operator("witcher.cutscene_remove_shot", text="", icon='X').shot_index = shot_idx
    if not shots:
        shots_box.label(text="No shots yet — press New Shot", icon='INFO')
    bridge_row = shots_box.row(align=True)
    bridge_row.operator("witcher.camera_apply_blender_cameras_to_rig", text="Shots → Rig", icon='NLA_PUSHDOWN')
    bridge_row.operator("witcher.camera_convert_cuts_to_blender_cameras", text="Cuts → Shots", icon='CAMERA_DATA')
    stale = cutscene_bake.shots_stale(scene)
    status = f"{len(cuts)} cut{'s' if len(cuts) != 1 else ''} on rig · {len(shots)} shot{'s' if len(shots) != 1 else ''}"
    if stale:
        status += " · changed, run Shots → Rig"
    shots_box.label(text=status, icon='ERROR' if stale else ('CHECKMARK' if cuts else 'BLANK1'))

    if camera_arm is None:
        rig_box = layout.box()
        rig_box.label(text="No camera rig in scene", icon='ARMATURE_DATA')
        rig_box.prop(scene, "witcher_cutscene_scratch_camera_repo_path", text="Entity")
        rig_box.operator("witcher.cutscene_scratch_import_camera", text="Load Rig", icon='IMPORT')
        return

    header, body = layout.box().panel("witcher_cs_camera_rig_tools", default_closed=True)
    header.label(text="Camera rig tools", icon='ARMATURE_DATA')
    if not body:
        return
    cut_idx = ui_anims._current_camera_cut_index(context, camera_arm, cuts)
    current_strip = cuts[cut_idx][1] if cut_idx >= 0 else None
    hdr = body.row(align=True)
    _draw_cutscene_exact_value(
        hdr,
        camera_arm,
        "name",
        field_label="Rig",
        value=camera_arm.name,
        text="",
        icon='ARMATURE_DATA',
    )
    if current_strip is not None:
        hdr.label(text=f"Cut {cut_idx + 1}/{len(cuts)}  {int(current_strip.frame_start)}–{int(current_strip.frame_end)}", icon='SEQUENCE')
    else:
        hdr.label(text="No cuts", icon='SEQUENCE')

    preview_row = body.row(align=True)
    preview_row.operator("witcher.camera_setup_preview", text="Setup Preview", icon='CAMERA_DATA')
    preview_row.operator("witcher.camera_set_scene_camera", text="Set Scene Camera", icon='VIEW_CAMERA')

    key_row = body.row(align=True)
    key_row.operator("witcher.camera_key_rig_from_scene_camera", text="Key Rig + DOF", icon='KEY_HLT')
    key_row.operator("witcher.camera_bake_cut_from_scene_camera", text="Bake Cut + DOF", icon='REC')
    body.operator("witcher.cutscene_scratch_bake_selected_camera_range", text="Bake Selected Range", icon='REC')

    nav_row = body.row(align=True)
    nav_row.operator("witcher.camera_cut_jump", text="", icon='TRIA_LEFT').direction = 'PREV'
    nav_row.operator("witcher.camera_cut_jump", text="Current Cut", icon='PREVIEW_RANGE').direction = 'CURRENT'
    nav_row.operator("witcher.camera_cut_jump", text="", icon='TRIA_RIGHT').direction = 'NEXT'

    edit_row = body.row(align=True)
    edit_row.operator("witcher.camera_cut_split", text="Cut", icon='MOD_BOOLEAN')
    edit_row.operator("witcher.camera_cut_combine", text="Combine", icon='NLA')
    edit_row.operator("witcher.cutscene_scratch_create_camera_cut", text="", icon='SEQUENCE')

    resize_row = body.row(align=True)
    resize_row.operator("witcher.camera_cut_resize", text="-5", icon='REMOVE').delta = -5
    resize_row.operator("witcher.camera_cut_resize", text="-1", icon='REMOVE').delta = -1
    resize_row.operator("witcher.camera_cut_resize", text="+1", icon='ADD').delta = 1
    resize_row.operator("witcher.camera_cut_resize", text="+5", icon='ADD').delta = 5

    marker_row = body.row(align=True)
    marker_row.operator("witcher.camera_cut_sync_markers", text="Sync Markers", icon='MARKER')
    marker_row.operator("witcher.camera_cut_apply_markers", text="Apply Markers", icon='CHECKMARK')

    _draw_camera_track_controls(body, camera_arm)


def _draw_event_schema_browser(layout, scene, cs_selected):
    """Collapsible schema browser listing all event types grouped ROOT / ENTRY."""
    scope_filter = "ROOT" if cs_selected else "ENTRY"

    for scope_label, scope_key, icon in [
        ("Cutscene ROOT events", "ROOT", 'SCENE_DATA'),
        ("Animation ENTRY events", "ENTRY", 'ACTION'),
    ]:
        if scope_key != scope_filter:
            continue
        sect = layout.box()
        sect.label(text=scope_label, icon=icon)
        for cls_name, base_cls, scope, own_props in _CUTSCENE_EVENT_SCHEMA:
            if scope != scope_key:
                continue
            row = sect.row(align=True)
            row.scale_y = 0.85
            label_col = row.column()
            label_col.scale_x = 1.0
            # Class name + base
            name_row = label_col.row(align=True)
            name_row.label(text=cls_name, icon=_event_type_icon(cls_name))
            base_col = name_row.row()
            base_col.enabled = False
            base_col.label(text=f"↑ {base_cls}")
            # Own props list
            if own_props:
                props_row = label_col.row()
                props_row.enabled = False
                props_row.label(text="  " + "  ·  ".join(f"{n} ({t})" for n, t in own_props[:5]))
            # Add button
            add_op = row.operator("witcher.cutscene_add_event", text="", icon='ADD')
            add_op.event_scope = scope_key
            add_op.event_class = cls_name


def _draw_cutscene_events_tab(layout, scene):
    layout.prop(scene, "witcher_cs_event_target", text="Show events for")
    target = _event_target_index(scene)
    all_events = list(getattr(scene, "witcher_cutscene_event_items", []))
    if target < 0:
        event_indices = [i for i, e in enumerate(all_events) if str(getattr(e, "event_scope", "") or "").upper() == "ROOT"]
        layout.label(text=f"Cutscene events ({len(event_indices)})", icon='SCENE_DATA')
        list_id, index_prop = "WITCH_UL_RootEventList", "witcher_cutscene_event_index"
    else:
        event_indices = [i for i, e in enumerate(all_events)
                         if str(getattr(e, "event_scope", "") or "").upper() == "ENTRY"
                         and int(getattr(e, "source_index", -1)) == target]
        anim = _find_loaded_cutscene_animation_entry(scene, target)
        layout.label(text=f"{_get_cutscene_animation_label(anim)} events ({len(event_indices)})", icon='ACTION')
        list_id, index_prop = "WITCH_UL_EntryEventList", "witcher_cs_entry_event_idx"
    if event_indices:
        layout.template_list(list_id, "", scene, "witcher_cutscene_event_items", scene, index_prop,
                             rows=min(len(event_indices), 6))
        ev_idx = int(getattr(scene, index_prop, 0) or 0)
        if ev_idx in event_indices:
            _draw_event_detail(layout, all_events[ev_idx])
    else:
        layout.label(text="No events here yet.", icon='INFO')

    layout.separator(factor=0.5)
    add_box = layout.box()
    add_box.label(text="Add event", icon='ADD')
    quick_row = add_box.row(align=True)
    op_cs = quick_row.operator("witcher.cutscene_add_event", text="Cutscene", icon='SCENE_DATA')
    op_cs.event_scope = "ROOT"
    op_cs.source_index = -1
    anim_action = quick_row.row(align=True)
    anim_action.enabled = target >= 0
    op_anim = anim_action.operator("witcher.cutscene_add_event", text="Animation", icon='ACTION')
    op_anim.event_scope = "ENTRY"
    op_anim.source_index = target
    if target < 0:
        quick_row.label(text="Pick an animation above to add animation events", icon='INFO')
    header, body = add_box.panel("witcher_cs_events_schema", default_closed=True)
    header.label(text="Advanced: all event classes", icon='PROPERTIES')
    if body:
        _draw_event_schema_browser(body, scene, target < 0)


def _draw_cutscene_dialog_id_source(layout, scene, context):
    from ..lipsync import redkit_project, ui_lipsync

    ui_lipsync._sync_project_selector_from_preferences(scene, context)
    row = layout.row(align=True)
    row.prop(scene, ui_lipsync.LIPSYNC_REDKIT_PROJECT_PROP, text="Project")
    row.operator("witcher.open_addon_preferences", text="", icon='PREFERENCES')
    project_path = redkit_project.get_active_project_path(context)
    if not project_path:
        layout.prop(scene, "witcher_cutscene_dialog_id_space", text="Fallback ID Space")
        try:
            _space, first_id, last_id = export_cutscene._cutscene_dialog_id_space_bounds(
                scene.witcher_cutscene_dialog_id_space
            )
            layout.label(text=f"Radish IDs {first_id}-{last_id}", icon='LINENUMBERS_ON')
        except ValueError:
            layout.label(text="No project and no fallback space: IDs cannot be allocated", icon='ERROR')
        return
    info = redkit_project.next_project_line_id(project_path)
    if info is None:
        layout.label(text=f"{project_path.name}: no idSpace in its .w3edit", icon='ERROR')
        return
    row = layout.row(align=True)
    row.label(text=f"idSpace {info.id_space} — {info.metadata_path.name}", icon='LINENUMBERS_ON')
    details = row.operator(WITCH_OT_CutsceneExactValueDetails.bl_idname, text="", icon='INFO')
    details.field_label = f"{project_path.name} string IDs"
    csv_time = time.strftime("%Y-%m-%d %H:%M", time.localtime(info.csv_mtime)) if info.csv_mtime else "no CSV yet"
    details.value = (
        f"idSpace {info.id_space} from {info.metadata_path} | "
        f"{info.used_count} IDs used in {redkit_project.PROJECT_STRINGS_CSV} (exported {csv_time}) | "
        f"next free {info.next_line_id}"
    )
    layout.label(text=f"{info.used_count} strings used · next free {info.next_line_id}", icon='BLANK1')


def _draw_cutscene_dialog_speech_link(layout, scene, line, line_index):
    from ..lipsync import ui_lipsync

    ref = str(getattr(line, "lipsync_ref", "") or "").strip()
    row = layout.row(align=True)
    if not ref:
        row.operator(WITCH_OT_CutsceneDialogSendToSpeech.bl_idname, text="Send to Speech tab", icon='LINKED').line_index = line_index
        return
    loaded = ui_lipsync._find_editor_line_by_id(scene, ref) is not None
    row.label(text=f"Speech line {ref}" if loaded else f"Speech line {ref} not loaded", icon='LINKED' if loaded else 'UNLINKED')
    open_row = row.row(align=True)
    open_row.enabled = loaded
    open_row.operator(WITCH_OT_CutsceneDialogOpenSpeech.bl_idname, text="", icon='FORWARD').line_index = line_index
    row.operator(WITCH_OT_CutsceneDialogUnlinkSpeech.bl_idname, text="", icon='X').line_index = line_index


def _draw_cutscene_dialog_line_id(layout, line, context, line_index):
    layout.prop(line, "allocated_line_id", text="Line ID")
    state, text = export_cutscene.authored_dialog_line_id_status(context, line_index)
    layout.label(text=text[:1].upper() + text[1:], icon={'OK': 'CHECKMARK', 'ERROR': 'ERROR'}.get(state, 'INFO'))


def _draw_cutscene_authored_dialogs(layout, scene, lines, context):
    layout.label(text="Dialogue", icon='OUTLINER_OB_SPEAKER')
    scene_path = export_cutscene._companion_scene_depot_path(scene)
    path_row = layout.row(align=True)
    path_row.label(
        text="Writes to companion .w2scene" if scene_path else "Set Export › Game path",
        icon='FILE',
    )
    if scene_path:
        details = path_row.operator(WITCH_OT_CutsceneExactValueDetails.bl_idname, text="", icon='INFO')
        details.field_label = "Companion .w2scene"
        details.value = scene_path
    _draw_cutscene_dialog_id_source(layout, scene, context)

    header = layout.row(align=True)
    header.operator(WITCH_OT_CutsceneDialogAddLine.bl_idname, text="Add Line", icon='ADD')
    header.operator(WITCH_OT_CutsceneDialogAddFromSpeech.bl_idname, text="From Speech…", icon='LINKED')
    if hasattr(scene, dialog_language.DIALOG_TEXT_LANGUAGE_PROP):
        header.prop(scene, dialog_language.DIALOG_TEXT_LANGUAGE_PROP, text="Text")
    if hasattr(scene, dialog_language.DIALOG_VOICE_LANGUAGE_PROP):
        header.prop(scene, dialog_language.DIALOG_VOICE_LANGUAGE_PROP, text="Voice")

    list_row = layout.row()
    list_row.template_list(
        "WITCH_UL_CutsceneAuthoredDialogList", "",
        scene, "witcher_cutscene_dialog_lines",
        scene, "witcher_cutscene_dialog_line_index",
        rows=min(max(3, len(lines)), 6),
    )
    buttons = list_row.column(align=True)
    remove = buttons.row(align=True)
    remove.enabled = bool(lines)
    remove.operator(WITCH_OT_CutsceneDialogRemoveLine.bl_idname, text="", icon='REMOVE')
    index = int(getattr(scene, "witcher_cutscene_dialog_line_index", 0) or 0)
    up = buttons.row(align=True)
    up.enabled = 0 < index < len(lines)
    up.operator(WITCH_OT_CutsceneDialogMoveLine.bl_idname, text="", icon='TRIA_UP').direction = 'UP'
    down = buttons.row(align=True)
    down.enabled = 0 <= index < len(lines) - 1
    down.operator(WITCH_OT_CutsceneDialogMoveLine.bl_idname, text="", icon='TRIA_DOWN').direction = 'DOWN'

    if 0 <= index < len(lines):
        line = lines[index]
        detail = layout.box()
        detail.label(text="Selected line", icon='OUTLINER_OB_SPEAKER')
        detail.prop(line, "speaker")
        detail.prop(line, "text")
        frames = detail.row(align=True)
        frames.prop(line, "start_frame")
        frames.prop(line, "end_frame")
        detail.operator(
            WITCH_OT_CutsceneDialogFromPlayhead.bl_idname,
            text="Start at Current Frame",
            icon='TIME',
        )
        detail.prop(line, "tier", expand=True)
        if line.tier == 'GAME':
            game = detail.box()
            actions = game.row(align=True)
            actions.operator(
                WITCH_OT_CutsceneDialogPickGameVoice.bl_idname,
                text="Pick Voice Line…",
                icon='VIEWZOOM',
            ).line_index = index
            preview = actions.row(align=True)
            preview.enabled = bool(_authored_cutscene_dialog_preview_id(line))
            preview.operator(WITCH_OT_CutsceneDialogPreviewGameLine.bl_idname, text="Load Preview", icon='PLAY')
            game.prop(line, "game_line_id")
            game.prop(line, "game_voice_file_name")
        elif line.tier == 'WAV':
            wav = detail.box()
            wav.prop(line, "wav_path")
            _draw_cutscene_dialog_line_id(wav, line, context, index)
            if str(get_tts_command(context) or "").strip():
                wav.operator(
                    WITCH_OT_CutsceneDialogGenerateWav.bl_idname,
                    text="Generate WAV",
                    icon='SOUND',
                ).line_index = index
            wav.operator(
                WITCH_OT_CutsceneDialogPrepareWav.bl_idname,
                text="Prepare Voice Assets",
                icon='SOUND',
            ).line_index = index
            if str(line.lipsync_ref or "").strip():
                _draw_cutscene_dialog_speech_link(wav, scene, line, index)
        else:
            detail.label(text="Text only — no audio setup", icon='FONT_DATA')
            _draw_cutscene_dialog_line_id(detail, line, context, index)
            _draw_cutscene_dialog_speech_link(detail, scene, line, index)
    else:
        layout.label(text="Add a line to begin.", icon='INFO')

    display_row = layout.row(align=True)
    display_row.prop(scene, "witcher_cutscene_show_dialog_subtitles", text="Viewport Subtitles", toggle=True, icon='FONT_DATA')
    display_row.prop(scene, "witcher_cutscene_subtitle_font_size", text="Size")


def _draw_cutscene_dialogs_tab(layout, scene, context):
    authored_lines = list(getattr(scene, "witcher_cutscene_dialog_lines", []) or [])
    preview_lines = list(getattr(scene, "witcher_cutscene_dialog_items", []) or [])
    has_file = bool(str(getattr(scene, "witcher_loaded_w2cutscene_path", "") or "").strip())
    has_scene_link = bool(str(getattr(scene, "witcher_cutscene_used_in_files", "") or "").strip())
    if authored_lines or not (has_file and has_scene_link and preview_lines):
        _draw_cutscene_authored_dialogs(layout, scene, authored_lines, context)
        return

    layout.label(text="Dialogue (preview)", icon='OUTLINER_OB_SPEAKER')
    layout.label(text="Read-only — lines live in the .w2scene (Story Scene panel)", icon='INFO')
    has_dialog_events = any("Dialog" in str(getattr(e, "event_type", "") or "")
                            for e in getattr(scene, "witcher_cutscene_event_items", []))
    prereq = layout.column(align=True)
    for ok, text in (
        (has_file, "Imported .w2cutscene"),
        (has_scene_link, "Used In Files names the .w2scene (Export → Metadata)"),
        (has_dialog_events, "Dialog events in the cutscene (Events tab)"),
    ):
        prereq.label(text=text, icon='CHECKMARK' if ok else 'RADIOBUT_OFF')

    header = layout.row(align=True)
    if hasattr(scene, dialog_language.DIALOG_TEXT_LANGUAGE_PROP):
        header.prop(scene, dialog_language.DIALOG_TEXT_LANGUAGE_PROP, text="Text")
    if hasattr(scene, dialog_language.DIALOG_VOICE_LANGUAGE_PROP):
        header.prop(scene, dialog_language.DIALOG_VOICE_LANGUAGE_PROP, text="Voice")
    header.operator(WITCH_OT_LoadCutsceneDialogs.bl_idname, text="Import/Refresh", icon='SOUND')

    display_row = layout.row(align=True)
    display_row.prop(scene, "witcher_cutscene_show_dialog_subtitles", text="Viewport Subtitles", toggle=True, icon='FONT_DATA')
    display_row.prop(scene, "witcher_cutscene_subtitle_font_size", text="Size")

    dialog_items = list(getattr(scene, "witcher_cutscene_dialog_items", []))
    copy_row = layout.row(align=True)
    copy_row.enabled = bool(dialog_items)
    copy_row.operator(WITCH_OT_CutsceneDialogCopyPreview.bl_idname, text="Copy to editable lines", icon='DUPLICATE')
    if dialog_items:
        layout.template_list(
            "WITCH_UL_CutsceneDialogList", "",
            scene, "witcher_cutscene_dialog_items",
            scene, "witcher_cutscene_dialog_index",
            rows=min(len(dialog_items), 4),
        )
        sel_idx = getattr(scene, "witcher_cutscene_dialog_index", 0)
        if 0 <= sel_idx < len(dialog_items):
            sel = dialog_items[sel_idx]
            detail = layout.box()
            detail.label(text="CStorySceneLine", icon='OUTLINER_OB_SPEAKER')
            col = detail.column(align=True)
            col.enabled = False
            col.prop(sel, "line_text", text="Text")
            col.prop(sel, "actor", text="Voice Tag")
            col.prop(sel, "line_id", text="Line")
            col.prop(sel, "voice_file", text="Voice File")
            col.prop(sel, "sound_event", text="Sound Event")
            if sel.scene_path:
                col.prop(sel, "scene_path", text="Source")
            col.label(text=f"Frames {sel.start_frame}-{sel.end_frame}", icon='TIME')
    elif has_file and has_scene_link and has_dialog_events:
        layout.label(text="Press Import/Refresh to read the lines.", icon='INFO')
    else:
        layout.label(text="Nothing to preview until the prerequisites above are met.", icon='INFO')


class WITCH_OT_CutsceneCreateNew(Operator):
    """Create a fresh cutscene."""
    bl_idname = "witcher.cutscene_create_new"
    bl_label = "Create New Cutscene"
    bl_options = {'REGISTER', 'UNDO'}

    cutscene_name: StringProperty(name="Name", default="new_cutscene",
                                  description="File name under animations\\cutscenes\\blender_tools")
    length: IntProperty(name="Frames", default=300, min=1,
                        description="Cutscene length in frames; the scene range becomes 0 to Frames-1")
    fps: IntProperty(name="FPS", default=30, min=1, description="Scene frame rate; game cutscenes run at 30 fps")

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        scene = context.scene
        if scene is None:
            return {'CANCELLED'}
        name = "".join(c if c.isalnum() or c in "_-" else "_" for c in str(self.cutscene_name or "").strip()).strip("_")
        name = name or "new_cutscene"
        base = f"animations\\cutscenes\\blender_tools\\{name}"
        current = str(getattr(scene, "witcher_cutscene_export_repo_path", "") or "").replace("/", "\\").lower()
        candidate = f"{base}.w2cutscene"
        if name == "new_cutscene" or candidate.lower() == current:
            for i in range(1, 100):
                candidate = f"{base}_{i:02d}.w2cutscene"
                if candidate.lower() != current:
                    break
        _clear_loaded_cutscene_state(scene)
        scene.witcher_cutscene_export_repo_path = candidate
        created = _setup_new_cutscene_defaults(context, candidate)
        scene.frame_start, scene.frame_end = 0, max(0, int(self.length) - 1)
        scene.render.fps, scene.render.fps_base = int(self.fps), 1.0
        scene.frame_set(0)
        if hasattr(scene, "witcher_cs_tab"):
            scene.witcher_cs_tab = 'ACTORS'
        actor_text = ", ".join(created) if created else "defaults"
        if _find_cutscene_actor_armature(scene, "camera") is None:
            self.report({'WARNING'}, f"New cutscene: {candidate}; camera actor rig was not loaded.")
            return {'FINISHED'}
        self.report({'INFO'}, f"New cutscene: {candidate} ({actor_text}).")
        return {'FINISHED'}

_CAST_RESULT_LIMIT = 100
_CAST_DEFAULT_APPEARANCE = "__DEFAULT__"
_CAST_APPEARANCE_ITEMS = [
    (_CAST_DEFAULT_APPEARANCE, "Default", "Use the casting index default appearance"),
]
_CAST_APPEARANCE_ITEM_HISTORY = [_CAST_APPEARANCE_ITEMS]


def _cast_appearance_items(self, context):
    return _CAST_APPEARANCE_ITEMS


def _cast_appearance_names(record):
    names = []
    seen = set()
    for value in list((record or {}).get("usedAppearances") or []) + list((record or {}).get("appearances") or []):
        name = str(value or "").strip()
        key = name.lower()
        if name and key not in seen:
            names.append(name)
            seen.add(key)
    return names


def _set_cast_appearance_items(record=None):
    global _CAST_APPEARANCE_ITEMS
    items = [
        (_CAST_DEFAULT_APPEARANCE, "Default", "Use the casting index default appearance"),
        *((name, name, f"Cast with appearance {name}") for name in _cast_appearance_names(record)),
    ]
    if items != _CAST_APPEARANCE_ITEMS:
        _CAST_APPEARANCE_ITEMS = items
        _CAST_APPEARANCE_ITEM_HISTORY.append(_CAST_APPEARANCE_ITEMS)


def _clear_cast_actor_selection(operator):
    operator.template_path = ""
    operator.selected_category = ""
    operator.selected_rigs = ""
    operator.selected_indexed = False
    operator.appearance = ""
    _set_cast_appearance_items()
    operator.appearance_choice = _CAST_DEFAULT_APPEARANCE


def reset_cast_actor_dialog(operator):
    operator.refreshing = True
    try:
        operator.query = ""
        operator.candidates.clear()
        operator.candidate_index = -1
        operator.match_status = ""
        _clear_cast_actor_selection(operator)
    finally:
        operator.refreshing = False


def refresh_cast_actor_candidates(operator, context=None):
    operator.refreshing = True
    try:
        operator.candidates.clear()
        operator.candidate_index = -1
        _clear_cast_actor_selection(operator)
        query = str(getattr(operator, "query", "") or "").strip()
        if not query:
            operator.match_status = ""
            return 0

        from ..repo_paths.casting import casting_record, resolve_cast

        if query.lower().endswith(".w2ent"):
            path = query.replace("/", "\\")
            record = casting_record(path)
            hits = [{"path": path, "alias": "", "record": record}]
            truncated = False
        else:
            hits = resolve_cast(query, limit=_CAST_RESULT_LIMIT + 1)
            truncated = len(hits) > _CAST_RESULT_LIMIT

        for hit in hits[:_CAST_RESULT_LIMIT]:
            path = str(hit.get("path", "") or "").replace("/", "\\")
            record = hit.get("record") or None
            alias = str(hit.get("alias", "") or "").strip()
            fallback = path.rsplit("\\", 1)[-1].rsplit(".", 1)[0]
            friendly = fallback
            for value in (
                (record or {}).get("displayName"),
                (record or {}).get("caption"),
                *((record or {}).get("aliases") or []),
                alias,
                fallback,
            ):
                candidate = str(value or "").strip()
                if candidate and not candidate.isdecimal():
                    friendly = candidate
                    break
            row = operator.candidates.add()
            row.label = f"{path} [{friendly}]"
            row.template_path = path
            row.category = str((record or {}).get("category", "") or "")
            row.rig_summary = "; ".join(str(value) for value in ((record or {}).get("rigs") or []))
            row.indexed = record is not None
        count = len(operator.candidates)
        operator.match_status = (
            f"{_CAST_RESULT_LIMIT}+ matches — refine the search"
            if truncated else f"{count} match{'es' if count != 1 else ''}"
        )
        return count
    except Exception as exc:
        operator.match_status = "Search failed — see console"
        log.exception("cast candidate lookup failed: %s", exc)
        return 0
    finally:
        operator.refreshing = False


def select_cast_actor_candidate(operator, index):
    operator.refreshing = True
    try:
        _clear_cast_actor_selection(operator)
        index = int(index)
        if index < 0 or index >= len(operator.candidates):
            return False
        row = operator.candidates[index]
        operator.template_path = str(row.template_path or "")
        operator.selected_category = str(row.category or "")
        operator.selected_rigs = str(row.rig_summary or "")
        operator.selected_indexed = bool(row.indexed)
        if row.indexed:
            from ..repo_paths.casting import casting_record
            _set_cast_appearance_items(casting_record(operator.template_path))
        operator.appearance_choice = _CAST_DEFAULT_APPEARANCE
        return True
    finally:
        operator.refreshing = False


def _cast_query_updated(operator, context):
    if not operator.refreshing:
        refresh_cast_actor_candidates(operator, context)


def _cast_candidate_index_updated(operator, context):
    if not operator.refreshing:
        select_cast_actor_candidate(operator, operator.candidate_index)


def _cast_appearance_updated(operator, context):
    if not operator.refreshing:
        value = str(operator.appearance_choice or "")
        operator.appearance = "" if value == _CAST_DEFAULT_APPEARANCE else value


def _remove_new_cast_objects(existing_pointers):
    for obj in list(bpy.data.objects):
        if obj.as_pointer() not in existing_pointers:
            data = getattr(obj, "data", None)
            name = str(getattr(obj, "name", "?"))
            try:
                bpy.data.objects.remove(obj, do_unlink=True)
            except Exception:
                log.debug("Failed to remove partial cast object %s", name, exc_info=True)
                continue
            if data is not None and data.users == 0:
                try:
                    bpy.data.batch_remove([data])
                except Exception:
                    log.debug("Failed to remove partial cast data for %s", name, exc_info=True)


class WITCH_OT_CastActor(Operator):
    """Import and tag an indexed actor."""
    bl_idname = "witcher.cast_actor"
    bl_label = "Cast Actor"
    bl_options = {'REGISTER', 'UNDO'}

    query: StringProperty(
        name="Search",
        description="Colloquial name (ciri, drowner) or a repo .w2ent path",
        options={'SKIP_SAVE', 'TEXTEDIT_UPDATE'},
        update=_cast_query_updated,
    )
    candidates: CollectionProperty(type=CutsceneCastCandidateItem, options={'SKIP_SAVE'})
    candidate_index: IntProperty(default=-1, min=-1, options={'SKIP_SAVE'}, update=_cast_candidate_index_updated)
    template_path: StringProperty(name="Template", options={'SKIP_SAVE'})
    selected_category: StringProperty(name="Category", options={'SKIP_SAVE'})
    selected_rigs: StringProperty(name="Rig", options={'SKIP_SAVE'})
    selected_indexed: BoolProperty(default=False, options={'HIDDEN', 'SKIP_SAVE'})
    match_status: StringProperty(default="", options={'HIDDEN', 'SKIP_SAVE'})
    appearance_choice: EnumProperty(
        name="Appearance",
        items=_cast_appearance_items,
        options={'SKIP_SAVE'},
        update=_cast_appearance_updated,
    )
    appearance: StringProperty(
        name="Appearance",
        description="Appearance name for raw paths or EXEC_DEFAULT; empty uses the casting index default",
        options={'SKIP_SAVE'},
    )
    refreshing: BoolProperty(default=False, options={'HIDDEN', 'SKIP_SAVE'})

    def invoke(self, context, event):
        reset_cast_actor_dialog(self)
        return context.window_manager.invoke_props_dialog(self, width=900, confirm_text="Cast")

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "query")
        if self.match_status:
            layout.label(text=self.match_status, icon='INFO')
        layout.template_list(
            WITCH_UL_CastActorCandidates.bl_idname,
            "",
            self,
            "candidates",
            self,
            "candidate_index",
            rows=8,
        )
        selected = layout.box()
        selected.label(text="Selected", icon='ARMATURE_DATA')
        if self.template_path:
            row = selected.row(align=True)
            _draw_cutscene_exact_value(
                row,
                self,
                "template_path",
                text="Template",
                value=self.template_path,
            )
            if self.selected_category:
                row = selected.row(align=True)
                _draw_cutscene_exact_value(
                    row,
                    self,
                    "selected_category",
                    text="Category",
                    value=self.selected_category,
                )
            if self.selected_rigs:
                row = selected.row(align=True)
                _draw_cutscene_exact_value(
                    row,
                    self,
                    "selected_rigs",
                    text="Rig",
                    value=self.selected_rigs,
                )
            if self.selected_indexed:
                selected.prop(self, "appearance_choice")
            else:
                selected.prop(self, "appearance")
        else:
            selected.label(text="Select a candidate to cast", icon='INFO')

    def execute(self, context):
        from ..w3_casting import cast_actor
        target = str(self.template_path or "").strip()
        if not target:
            self.report({'ERROR'}, "Select an actor template first")
            return {'CANCELLED'}
        scene = context.scene
        existing_pointers = {obj.as_pointer() for obj in bpy.data.objects}
        try:
            actor, info = cast_actor(target, appearance=self.appearance)
            _sync_actor_items_with_scene(scene)
            actor_name = str(info.get("label", "") or "")
            index = _find_loaded_actor_entry_index(
                scene,
                object_name=str(getattr(actor, "name", "") or ""),
                actor_name=actor_name,
            )
            if index < 0:
                raise RuntimeError("Imported actor was not added to the cutscene Actors list")
            scene.witcher_cutscene_loaded_actor_index = index
            appearance_text = str(info.get("appearance", "") or "") or "default"
            label = str(info.get("label", "") or target)
            template = str(info.get("template", "") or target)
        except Exception as exc:
            _remove_new_cast_objects(existing_pointers)
            try:
                _sync_actor_items_with_scene(scene)
            except Exception:
                log.debug("Failed to repair actor rows after casting failure", exc_info=True)
            log.exception("cast_actor failed")
            self.report({'ERROR'}, f"Casting failed: {exc}")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Cast '{label}' from {template} ({appearance_text})")
        return {'FINISHED'}


class WITCH_OT_CutsceneValidate(Operator):
    """Validate actors, clips, camera, and export tracks."""
    bl_idname = "witcher.cutscene_validate"
    bl_label = "Validate Cutscene"

    def execute(self, context):
        from ..animation import cutscene_validate
        lines, errors, warnings = cutscene_validate.store_report(
            context.scene, cutscene_validate.collect_issues(context)
        )
        for line in lines:
            if line.startswith("ERROR"):
                log.error("cutscene validate: %s", line)
            elif line.startswith(("WARN", "WARNING")):
                log.warning("cutscene validate: %s", line)
            else:
                log.info("cutscene validate: %s", line)
        if errors:
            self.report({'ERROR'}, f"{len(errors)} validation error(s) — see the Export tab")
        elif warnings:
            self.report({'WARNING'}, f"Export-ready with {len(warnings)} warning(s)")
        else:
            self.report({'INFO'}, "Cutscene is export-ready")
        return {'FINISHED'}


class WITCH_OT_CutsceneValidationReport(Operator):
    """Show or copy the validation report."""
    bl_idname = "witcher.cutscene_validation_report"
    bl_label = "Validation Report"
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=560, confirm_text="Copy")

    def draw(self, context):
        from . import ui_anims

        layout = self.layout
        report = str(getattr(context.scene, "witcher_cutscene_validation_report", "") or "")
        summary, icon = _validation_report_summary(report)
        if summary:
            layout.label(text=summary, icon=icon)
        col = layout.column(align=True)
        for line in report.splitlines():
            severity, _sep, message = line.partition(" ")
            icon = {"ERROR": 'ERROR', "WARN": 'INFO', "WARNING": 'INFO', "OK": 'CHECKMARK'}.get(severity, 'BLANK1')
            for i, chunk in enumerate(ui_anims._split_ui_label_text(message, width=88)):
                col.label(text=chunk, icon=icon if i == 0 else 'BLANK1')

    def execute(self, context):
        context.window_manager.clipboard = str(getattr(context.scene, "witcher_cutscene_validation_report", "") or "")
        self.report({'INFO'}, "Validation report copied to clipboard")
        return {'FINISHED'}


class WITCH_OT_CutsceneValidationGoto(Operator):
    """Open the referenced issue location."""
    bl_idname = "witcher.cutscene_validation_goto"
    bl_label = "Go To Issue"
    bl_options = {'INTERNAL', 'UNDO'}

    tab: StringProperty(default="", options={'SKIP_SAVE'})
    object_name: StringProperty(default="", options={'SKIP_SAVE'})
    frame: IntProperty(default=-1, options={'SKIP_SAVE'})
    line: IntProperty(default=-1, options={'SKIP_SAVE'})

    def execute(self, context):
        scene = context.scene
        if self.tab:
            try:
                scene.witcher_cs_tab = self.tab
            except TypeError:
                pass
        obj = bpy.data.objects.get(self.object_name) if self.object_name else None
        if obj is not None:
            try:
                for other in context.selected_objects:
                    other.select_set(False)
                obj.select_set(True)
                context.view_layer.objects.active = obj
            except RuntimeError:
                pass
        if self.frame >= 0:
            scene.frame_set(self.frame)
        if self.line >= 0 and self.tab == 'DIALOGS':
            scene.witcher_cutscene_dialog_line_index = min(self.line, max(0, len(scene.witcher_cutscene_dialog_lines) - 1))
        return {'FINISHED'}


class WITCH_OT_CutsceneSetSceneRange(Operator):
    """Fit the scene range to cutscene strips."""
    bl_idname = "witcher.cutscene_set_scene_range"
    bl_label = "Set Scene Range"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        start, end = cutscene_bake.effective_frame_range(context.scene)
        context.scene.frame_start, context.scene.frame_end = start, end
        self.report({'INFO'}, f"Scene range set to {start}-{end}")
        return {'FINISHED'}


class WITCH_OT_CutsceneBakeAll(Operator):
    """Bake cutscene actors to full-length tracks."""
    bl_idname = "witcher.cutscene_bake"
    bl_label = "Bake for Cutscene"
    bl_description = (
        "Sample every cast actor over the cutscene range and write one flat action per actor onto a "
        f"{cutscene_bake.BAKE_TRACK_NAME} track. Folds in every unmuted strip on the cutscene_anim tracks plus "
        "constraints, drivers and object transforms; muted clips are skipped. Source tracks stay muted as "
        "*_prebake and are restored on re-bake"
    )
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        from . import ui_anims

        # Always re-apply: the stale fingerprint is only a hint and cannot see every input (drivers, key edits inside target strips).
        if cutscene_bake.iter_shot_markers(context.scene) and ui_anims.apply_shots_to_rig(context, self.report) != {'FINISHED'}:
            self.report({'ERROR'}, "Shots → Rig failed; bake cancelled")
            return {'CANCELLED'}
        transaction = None
        try:
            transaction = cutscene_bake.begin_bake_transaction(context.scene)
            baked = cutscene_bake.bake_cutscene_actors(context)
        except Exception as exc:
            if transaction is not None:
                transaction.rollback()
            log.exception("cutscene bake failed")
            self.report({'ERROR'}, f"Bake failed: {exc}")
            return {'CANCELLED'}
        if not baked:
            transaction.rollback()
            self.report({'WARNING'}, "Nothing to bake (no animated actors or props)")
            return {'CANCELLED'}
        transaction.commit()
        names = ", ".join(str(arm.get("cutscene_actor_name", arm.name)) for arm, _action in baked)
        self.report({'INFO'}, f"Baked {len(baked)} actor(s): {names}")
        return {'FINISHED'}


class WITCH_OT_CutsceneAddProps(Operator):
    """Assign selected meshes to trajectory prop slots."""
    bl_idname = "witcher.cutscene_add_props"
    bl_label = "Add Selected as Props"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return any(getattr(o, "type", None) == 'MESH' for o in context.selected_objects or [])

    def execute(self, context):
        from ..animation import cutscene_bake
        meshes = [o for o in context.selected_objects if getattr(o, "type", None) == 'MESH']
        try:
            assigned = cutscene_bake.assign_prop_slots(context.scene, meshes)
            cutscene_bake.ensure_prop_actor(context)
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, "; ".join(f"{obj.name} -> {slot}" for obj, slot in assigned))
        return {'FINISHED'}


class WITCH_OT_CutsceneRemoveProp(Operator):
    """Remove an object's trajectory prop slot."""
    bl_idname = "witcher.cutscene_remove_prop"
    bl_label = "Remove Prop"
    bl_options = {'REGISTER', 'UNDO'}

    object_name: StringProperty(default="")

    def execute(self, context):
        from ..animation import cutscene_bake
        obj = bpy.data.objects.get(self.object_name)
        if obj is None:
            return {'CANCELLED'}
        cutscene_bake.clear_prop_slot(obj)
        if not cutscene_bake.iter_prop_objects(context.scene):
            cutscene_bake.remove_prop_actor(context.scene)
        return {'FINISHED'}


def _active_cutscene_actor_armature(scene):
    items = list(getattr(scene, "witcher_cutscene_actor_items", []) or [])
    idx = int(getattr(scene, "witcher_cutscene_loaded_actor_index", 0) or 0)
    obj = _get_loaded_cutscene_actor_object(items[idx]) if 0 <= idx < len(items) else None
    if obj is None:
        active = getattr(bpy.context, "active_object", None)
        if active is not None and str(active.get("cutscene_actor_name", "") or "").strip():
            obj = active
    if obj is None:
        return None
    if getattr(obj, "type", None) == 'ARMATURE':
        return obj
    stack = list(getattr(obj, "children", []) or [])
    while stack:
        child = stack.pop()
        if getattr(child, "type", None) == 'ARMATURE':
            return child
        stack.extend(getattr(child, "children", []) or [])
    return None


class WITCH_OT_CutsceneGripProp(Operator):
    """Constrain selected props to an actor's weapon bone."""
    bl_idname = "witcher.cutscene_grip_prop"
    bl_label = "Grip in Hand"
    bl_options = {'REGISTER', 'UNDO'}

    bone: EnumProperty(
        name="Hand",
        items=[
            ("r_weapon", "Right Hand (r_weapon)", "Main-hand weapon bone"),
            ("l_weapon", "Left Hand (l_weapon)", "Off-hand weapon bone"),
        ],
        default="r_weapon",
    )

    @classmethod
    def poll(cls, context):
        return any(getattr(o, "type", None) == 'MESH' for o in context.selected_objects or [])

    def execute(self, context):
        from mathutils import Matrix

        armature = _active_cutscene_actor_armature(context.scene)
        if armature is None:
            self.report({'ERROR'}, "Select a cutscene actor in the Actors tab first")
            return {'CANCELLED'}
        if self.bone not in armature.pose.bones:
            self.report({'ERROR'}, f"Actor has no '{self.bone}' bone")
            return {'CANCELLED'}
        gripped = 0
        for obj in context.selected_objects:
            if getattr(obj, "type", None) != 'MESH':
                continue
            for con in [c for c in obj.constraints if c.name.startswith("cutscene_grip")]:
                obj.constraints.remove(con)
            con = obj.constraints.new('CHILD_OF')
            con.name = "cutscene_grip"
            con.target = armature
            con.subtarget = self.bone
            con.inverse_matrix = Matrix.Identity(4)
            gripped += 1
        self.report({'INFO'}, f"Gripped {gripped} prop(s) in {self.bone}")
        return {'FINISHED'}


class WITCH_OT_CutsceneExportSceneWrapper(Operator):
    """Write the REDkit-editable companion .w2scene."""
    bl_idname = "witcher.cutscene_export_scene_wrapper"
    bl_label = "Export Scene Wrapper"

    def execute(self, context):
        from ..exporters.export_cutscene import (
            _collect_authored_cutscene_dialogue,
            _companion_scene_depot_path,
            prepare_authored_cutscene_dialogue_strings,
        )
        from ..CR2W import scene_builder
        from ..animation import cutscene_bake
        from .ui_animated_component import _resolve_export_dir, _safe_repo_output_path

        scene = context.scene
        cutscene_repo = str(getattr(scene, "witcher_cutscene_export_repo_path", "") or "").replace("/", "\\").lower()
        scene_repo = _companion_scene_depot_path(scene)
        if not scene_repo:
            self.report({'ERROR'}, "Set the Game path (….w2cutscene) in the Export tab first")
            return {'CANCELLED'}
        export_dir = _resolve_export_dir(context)
        if not export_dir:
            self.report({'ERROR'}, "No REDkit project or uncook path configured")
            return {'CANCELLED'}
        try:
            out_path = _safe_repo_output_path(export_dir, scene_repo, suffix=".w2scene")
        except ValueError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

        frame_start, frame_end = cutscene_bake.effective_frame_range(scene)
        render = scene.render
        fps = float(render.fps) / float(render.fps_base or 1.0)
        duration = max(1, int(frame_end) - int(frame_start)) / (fps or 30.0)
        point_tags = [
            tag.strip()
            for tag in str(getattr(scene, "witcher_cutscene_point_tags", "") or "").replace("\n", ";").split(";")
            if tag.strip()
        ]
        try:
            _collect_authored_cutscene_dialogue(scene)
        except Exception as exc:
            self.report({'ERROR'}, f"Dialogue invalid: {exc}")
            return {'CANCELLED'}
        try:
            strings_result = prepare_authored_cutscene_dialogue_strings(
                context,
                out_path,
                scene_repo=scene_repo,
            )
        except Exception as exc:
            log.exception("cutscene dialogue strings preparation failed")
            self.report({'ERROR'}, f"Strings failed: {exc}")
            return {'CANCELLED'}
        scene.witcher_cutscene_dialog_strings_path = strings_result["path"]
        try:
            lines, _dialog_events = _collect_authored_cutscene_dialogue(scene)
            scene_builder.save_cutscene_wrapper_scene(
                out_path, cutscene_repo, duration=duration,
                point_tag=point_tags[0] if point_tags else "",
                lines=lines,
            )
        except Exception as exc:
            log.exception("scene wrapper export failed")
            self.report({'ERROR'}, f"Scene wrapper failed: {exc}")
            return {'CANCELLED'}
        if strings_result["mode"] == "csv":
            detail = f"strings: {strings_result['path']} (id-space {strings_result['id_space']})"
        elif strings_result["mode"] == "redkit":
            detail = f"prepared {strings_result['line_count']} string(s) in {strings_result['path']}"
        else:
            detail = "no new strings"
        self.report({'INFO'}, f"Wrote {out_path} ({duration:.2f}s); {detail}")
        return {'FINISHED'}


class WITCH_OT_CutsceneGeneratePropsEntity(Operator):
    """Write the per-cutscene props .w2ent."""
    bl_idname = "witcher.cutscene_generate_props_entity"
    bl_label = "Generate Props Entity"

    def execute(self, context):
        from ..animation import cutscene_bake
        try:
            out_path = cutscene_bake.generate_props_entity(context)
        except Exception as exc:
            log.exception("props entity generation failed")
            self.report({'ERROR'}, f"Props entity failed: {exc}")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Wrote {out_path}")
        return {'FINISHED'}


def _is_cutscene_cast_actor(arm):
    actor_name = str(arm.get("cutscene_actor_name", "") or "").strip().lower()
    return bool(
        actor_name
        and not arm.get(cutscene_bake.PROP_RIG_TAG)
        and actor_name not in cutscene_bake.SCAFFOLD_ACTORS
        and not import_cutscene._is_cutscene_camera_actor_metadata(actor_obj=arm)
    )


def _cutscene_cast_actors(scene):
    return [
        arm for arm in cutscene_bake.iter_cutscene_actor_armatures(scene)
        if _is_cutscene_cast_actor(arm)
    ]


def _cutscene_authored_clip_groups(scene, groups=None, cast_actors=None):
    groups = _clip_groups(scene) if groups is None else groups
    cast_actors = _cutscene_cast_actors(scene) if cast_actors is None else cast_actors
    cast_names = {
        str(arm.get("cutscene_actor_name", "") or "").strip().lower()
        for arm in cast_actors
    }
    return {
        key: group for key, group in groups.items()
        if str(group.get("actor", "") or "").strip().lower() in cast_names
    }


def _cutscene_clip_strips(scene, groups=None, cast_actors=None):
    return [
        strip
        for group in _cutscene_authored_clip_groups(scene, groups, cast_actors).values()
        for _track, strip in group.get("strips", [])
    ]


def _cutscene_status_text(scene, state, groups=None, cast_actors=None):
    cast_actors = _cutscene_cast_actors(scene) if cast_actors is None else cast_actors
    actors = len(cast_actors)
    clips = len(_cutscene_authored_clip_groups(scene, groups, cast_actors))
    baked = "stale" if state["baked"] and (state["stale"] or state.get("shots_stale")) else ("baked" if state["baked"] else "not baked")
    return f"{actors} actor{'s' if actors != 1 else ''} · {clips} clip{'s' if clips != 1 else ''} · {baked}"


def _cutscene_next_hint(scene, state, groups=None, cast_actors=None):
    cast_actors = _cutscene_cast_actors(scene) if cast_actors is None else cast_actors
    has_path = str(getattr(scene, "witcher_cutscene_export_repo_path", "") or "").strip()
    has_file = str(getattr(scene, "witcher_loaded_w2cutscene_path", "") or "").strip()
    if not has_path and not has_file:
        return "Next: press New or Import"
    actor_rows = list(getattr(scene, "witcher_cutscene_actor_items", []) or [])
    has_file_rows = any(int(getattr(item, "source_index", -1)) >= 0 for item in actor_rows)
    if has_file_rows and not cast_actors:
        return "Next: load an actor entity (Actors tab)"
    if not cast_actors:
        return "Next: add an actor (Actors tab)"
    if not _cutscene_clip_strips(scene, groups, cast_actors):
        return "Next: add a clip (Clips tab)"
    if not state["baked"]:
        return "Next: Bake actors (Export tab)"
    if state.get("shots_stale"):
        return "Next: Shots → Rig (Camera tab); Bake and Export do it too"
    if state["stale"]:
        return "Next: Export re-bakes when enabled"
    report = str(getattr(scene, "witcher_cutscene_validation_report", "") or "")
    errors = sum(1 for line in report.splitlines() if line.startswith("ERROR"))
    if errors:
        return f"Next: fix {errors} validation error(s) (Export tab)"
    return "Ready to export (Export tab)"


class WITCHER_PT_cutscene_panel(WITCH_PT_Base, Panel):
    bl_idname = "WITCHER_PT_cutscene_panel"
    bl_label = "Cutscene"
    bl_description = ""
    bl_options = {'DEFAULT_CLOSED'}

    def draw_header(self, context):
        self.layout.label(text="", icon='ACTION')

    def draw(self, context):
        scene = context.scene
        if scene is None:
            return

        layout = self.layout
        loaded_cutscene_path = str(getattr(scene, "witcher_loaded_w2cutscene_path", "") or "").strip()
        if loaded_cutscene_path and not scene.witcher_cutscene_actor_items and not scene.witcher_cutscene_animation_items:
            _schedule_deferred_cutscene_state_sync(scene, loaded_cutscene_path)

        state = cutscene_bake.bake_state(scene)
        clip_groups = _clip_groups(scene)
        cast_actors = _cutscene_cast_actors(scene)
        layout.label(text=_cutscene_status_text(scene, state, clip_groups, cast_actors), icon='ACTION')
        layout.label(text=_cutscene_next_hint(scene, state, clip_groups, cast_actors), icon='INFO')
        action_row = layout.row(align=True)
        action_row.operator(WITCH_OT_CutsceneCreateNew.bl_idname, text="New", icon='FILE_NEW')
        action_row.operator(ButtonOperatorImportW2cutscene.bl_idname, text="Import…", icon='IMPORT')
        if loaded_cutscene_path:
            action_row.operator(WITCH_OT_ReopenCutsceneImportDialog.bl_idname, text="Re-import", icon='FILE_REFRESH')

        prev_split = layout.use_property_split
        layout.use_property_split = False
        for tab_items in (
            (("ACTORS", "Actors", 'ARMATURE_DATA'), ("ANIMS", "Clips", 'NLA'), ("CAMERA", "Camera", 'CAMERA_DATA')),
            (("EVENTS", "Events", 'SEQUENCE'), ("DIALOGS", "Dialogue", 'OUTLINER_OB_SPEAKER'), ("TEMPLATE", "Export", 'EXPORT')),
        ):
            tab_row = layout.row(align=True)
            tab_row.scale_y = 1.2
            for tab_id, text, icon in tab_items:
                tab_row.prop_enum(scene, "witcher_cs_tab", tab_id, text=text, icon=icon)
        layout.use_property_split = prev_split
        layout.separator(factor=0.5)

        tab = str(getattr(scene, "witcher_cs_tab", "ACTORS") or "ACTORS")
        if tab == 'TEMPLATE':
            _draw_cutscene_export_tab(layout, scene, context)
        elif tab == 'ACTORS':
            _draw_cutscene_actors_tab(layout, scene, context)
        elif tab == 'ANIMS':
            _draw_cutscene_anims_tab(layout, scene, clip_groups, state)
        elif tab == 'CAMERA':
            _draw_cutscene_camera_tab(layout, scene, context)
        elif tab == 'EVENTS':
            _draw_cutscene_events_tab(layout, scene)
        elif tab == 'DIALOGS':
            _draw_cutscene_dialogs_tab(layout, scene, context)


classes = [
    CutsceneBrowseAnimationItem,
    CutsceneBrowseAnimationState,
    CutsceneDialogVoiceResult,
    CutsceneDialogVoiceState,
    CutsceneActorPreviewItem,
    CutsceneAnimationPreviewItem,
    CutsceneCastCandidateItem,
    CutsceneLoadedActorItem,
    CutsceneLoadedAnimationItem,
    CutsceneEventItem,
    CutsceneEffectItem,
    CutsceneValidationIssue,
    CutsceneTemplateFieldItem,
    CutsceneDialogItem,
    CutsceneAuthoredDialogLine,
    WITCH_UL_CutsceneActorPreview,
    WITCH_UL_CastActorCandidates,
    WITCH_UL_CutsceneBrowseAnimations,
    WITCH_UL_CutsceneDialogVoiceResults,
    WITCH_UL_CutsceneSpeechPick,
    WITCH_UL_CutsceneAnimationPreview,
    WITCH_UL_CutsceneDialogList,
    WITCH_UL_CutsceneAuthoredDialogList,
    WITCH_UL_LoadedActorList,
    WITCH_UL_LoadedAnimList,
    WITCH_UL_CutsceneValidationIssues,
    WITCH_UL_RootEventList,
    WITCH_UL_EntryEventList,
    WITCH_OT_CutsceneRemoveActor,
    WITCH_OT_CutsceneReplaceActor,
    WITCH_OT_CutsceneRetargetW2ToW3,
    WITCH_OT_CutsceneRemoveAnimation,
    WITCH_OT_CutsceneAddEvent,
    ButtonOperatorImportW2cutscene,
    WITCH_OT_CutsceneCreateNew,
    WITCH_OT_ReopenCutsceneImportDialog,
    WITCH_OT_ImportCutsceneBurnedAudio,
    WITCH_OT_RemoveCutsceneBurnedAudio,
    WITCH_OT_SetCutsceneAnimationLoaded,
    WITCH_OT_SetCutsceneActorAnimationLayer,
    WITCH_OT_SetCutsceneActorLoaded,
    WITCH_OT_CutsceneRemoveActorFull,
    WITCH_OT_CutsceneImportActorForAnim,
    WITCH_OT_CutsceneDialogAddLine,
    WITCH_OT_CutsceneDialogRemoveLine,
    WITCH_OT_CutsceneDialogVoiceSearchClear,
    WITCH_OT_CutsceneDialogPickGameVoice,
    WITCH_OT_CutsceneDialogPreviewGameLine,
    WITCH_OT_CutsceneDialogGenerateWav,
    WITCH_OT_CutsceneDialogPrepareWav,
    WITCH_OT_CutsceneDialogAddFromSpeech,
    WITCH_OT_CutsceneDialogSendToSpeech,
    WITCH_OT_CutsceneDialogOpenSpeech,
    WITCH_OT_CutsceneDialogUnlinkSpeech,
    WITCH_OT_CutsceneDialogMoveLine,
    WITCH_OT_CutsceneDialogFromPlayhead,
    WITCH_OT_CutsceneDialogCopyPreview,
    WITCH_OT_LoadCutsceneDialogs,
    WITCH_OT_CastActor,
    WITCH_OT_CutsceneValidate,
    WITCH_OT_CutsceneValidationReport,
    WITCH_OT_CutsceneValidationGoto,
    WITCH_OT_CutsceneSetSceneRange,
    WITCH_OT_CutsceneSetClipMuted,
    WITCH_OT_CutsceneBrowseAnimationClear,
    WITCH_OT_CutsceneBrowseAddAnimation,
    WITCH_OT_CutsceneExactValueDetails,
    WITCH_OT_CutsceneEventPartialInfo,
    WITCH_OT_CutsceneBakeAll,
    WITCH_OT_CutsceneAddProps,
    WITCH_OT_CutsceneRemoveProp,
    WITCH_OT_CutsceneGripProp,
    WITCH_OT_CutsceneExportSceneWrapper,
    WITCH_OT_CutsceneGeneratePropsEntity,
    WITCHER_PT_cutscene_panel,
]


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.WindowManager.witcher_cutscene_browse_animation = PointerProperty(type=CutsceneBrowseAnimationState)
    bpy.types.WindowManager.witcher_cutscene_dialog_voice = PointerProperty(type=CutsceneDialogVoiceState)
    bpy.types.Scene.witcher_loaded_cutscene_name = StringProperty(default="")
    bpy.types.Scene.witcher_cutscene_last_import_seconds = FloatProperty(default=0.0)
    bpy.types.Scene.witcher_cutscene_actor_items = CollectionProperty(type=CutsceneLoadedActorItem)
    bpy.types.Scene.witcher_cutscene_animation_items = CollectionProperty(type=CutsceneLoadedAnimationItem)
    bpy.types.Scene.witcher_cutscene_event_items = CollectionProperty(type=CutsceneEventItem)
    bpy.types.Scene.witcher_cutscene_event_index = IntProperty(default=0)
    bpy.types.Scene.witcher_cutscene_validation_issues = CollectionProperty(
        type=CutsceneValidationIssue, options={'SKIP_SAVE'}
    )
    bpy.types.Scene.witcher_cutscene_validation_issue_index = IntProperty(default=0, options={'SKIP_SAVE'})
    bpy.types.Scene.witcher_cs_entry_event_idx = IntProperty(default=0)
    bpy.types.Scene.witcher_cs_event_target = EnumProperty(
        name="Show events for",
        description="Cutscene-level events, or the events of one animation entry",
        items=_event_target_items,
    )
    bpy.types.Scene.witcher_cs_tab = EnumProperty(
        name="Cutscene Tab",
        # Pin values so scenes saved before the reorder keep their tab.
        items=[
            ('ACTORS', 'Actors', 'Cast, tag and edit cutscene actors', 'ARMATURE_DATA', 1),
            ('ANIMS', 'Clips', 'Animation clips on the actors\' cutscene_anim tracks', 'NLA', 2),
            ('CAMERA', 'Camera', 'Shots, camera cuts and camera rig tools', 'CAMERA_DATA', 3),
            ('EVENTS', 'Events', 'Cutscene and animation events', 'SEQUENCE', 4),
            ('DIALOGS', 'Dialogue', 'Author or preview cutscene dialogue lines', 'OUTLINER_OB_SPEAKER', 5),
            ('TEMPLATE', 'Export', 'Bake, validate, export and template metadata', 'EXPORT', 0),
        ],
        default='ACTORS',
    )
    bpy.types.Scene.witcher_cutscene_loaded_actor_index = IntProperty(default=0)
    bpy.types.Scene.witcher_cutscene_loaded_anim_index = IntProperty(default=0)
    bpy.types.Scene.witcher_cutscene_template_fields = CollectionProperty(type=CutsceneTemplateFieldItem)
    bpy.types.Scene.witcher_cutscene_burned_audio_event = StringProperty(default="")
    bpy.types.Scene.witcher_cutscene_burned_audio_item_path = StringProperty(default="")
    bpy.types.Scene.witcher_cutscene_burned_audio_default_volume = FloatProperty(
        name="Burned Track Default Volume",
        default=import_cutscene.CUTSCENE_BURNED_AUDIO_DEFAULT_VOLUME,
        min=0.0,
        soft_max=2.0,
        description="Default sequencer volume for imported cutscene burned-track strips",
    )
    bpy.types.Scene.witcher_cutscene_point_tags = StringProperty(
        name="Point Tags",
        default="",
        description="Semicolon-separated TagList value for exported cutscenes",
    )
    bpy.types.Scene.witcher_cutscene_last_level_loaded = StringProperty(
        name="Last Level Loaded",
        default="",
        description="Value written to CCutsceneTemplate.lastLevelLoaded on export",
    )
    bpy.types.Scene.witcher_cutscene_used_in_files = StringProperty(
        name="Used In Files",
        default="",
        description="Semicolon-separated depot paths for CCutsceneTemplate.usedInFiles on export",
    )
    bpy.types.Scene.witcher_cutscene_export_metadata_synced = BoolProperty(default=False)
    bpy.types.Scene.witcher_cutscene_effect_items = CollectionProperty(type=CutsceneEffectItem)
    bpy.types.Scene.witcher_cutscene_dialog_items = CollectionProperty(type=CutsceneDialogItem)
    bpy.types.Scene.witcher_cutscene_dialog_index = IntProperty(default=0)
    bpy.types.Scene.witcher_cutscene_dialog_lines = CollectionProperty(type=CutsceneAuthoredDialogLine)
    bpy.types.Scene.witcher_cutscene_dialog_line_index = IntProperty(default=0)
    bpy.types.Scene.witcher_cutscene_dialog_id_space = IntProperty(
        name="Dialogue ID Space",
        default=export_cutscene.CUTSCENE_DIALOG_ID_SPACE_DEFAULT,
        min=-1,
        max=9999,
        description="Radish strings id-space for fallback allocation; 9999 is the documented example and -1 disables fallback",
    )
    bpy.types.Scene.witcher_cutscene_dialog_strings_path = StringProperty(
        name="Dialogue Strings CSV",
        default="",
        subtype='FILE_PATH',
        description="Last REDkit project strings database or standalone Radish strings CSV prepared by Step 5",
    )
    bpy.types.Scene.witcher_cutscene_show_dialog_subtitles = BoolProperty(
        name="Viewport Subtitles",
        default=True,
        description="Show active dialog text in the 3D Viewport",
    )
    bpy.types.Scene.witcher_cutscene_subtitle_font_size = IntProperty(
        name="Subtitle Size",
        default=28,
        min=12,
        max=72,
        description="3D Viewport subtitle font size",
    )
    bpy.types.Scene.witcher_cutscene_actor_replace_source = EnumProperty(
        name="Actor Template Source",
        description="Repo source used when replacing the selected cutscene actor",
        items=_CUTSCENE_ACTOR_REPLACE_SOURCE_ITEMS,
        default="W3",
    )
    bpy.types.Scene.witcher_cutscene_retarget_camera_template = StringProperty(
        name="W3 Camera",
        description="Witcher 3 camera entity template used by W2 to W3 cutscene retarget",
        default=import_cutscene.W3_CUTSCENE_CAMERA_TEMPLATE,
    )
    bpy.types.Scene.witcher_cutscene_retarget_male_template = StringProperty(
        name="W3 Male",
        description="Witcher 3 entity template used for W2 male skeleton actors",
        default=import_cutscene.W3_CUTSCENE_GERALT_TEMPLATE,
    )
    bpy.types.Scene.witcher_cutscene_retarget_female_template = StringProperty(
        name="W3 Female",
        description="Witcher 3 entity template used for W2 female skeleton actors",
        default=import_cutscene.W3_CUTSCENE_CIRI_TEMPLATE,
    )
    bpy.types.Object.witcher_cutscene_actor_type = EnumProperty(
        name="type",
        description="ECutsceneActorType for the cutscene actor",
        items=_ECutsceneActorType_ITEMS,
        get=_actor_type_get,
        set=_actor_type_set,
    )
    enable_cutscene_subtitles()


def unregister():
    disable_cutscene_subtitles()
    if hasattr(bpy.types.WindowManager, "witcher_cutscene_browse_animation"):
        del bpy.types.WindowManager.witcher_cutscene_browse_animation
    if hasattr(bpy.types.WindowManager, "witcher_cutscene_dialog_voice"):
        del bpy.types.WindowManager.witcher_cutscene_dialog_voice
    for prop in (
        "witcher_loaded_cutscene_name",
        "witcher_cutscene_last_import_seconds",
        "witcher_cutscene_actor_items",
        "witcher_cutscene_animation_items",
        "witcher_cutscene_event_items",
        "witcher_cutscene_event_index",
        "witcher_cutscene_validation_issues",
        "witcher_cutscene_validation_issue_index",
        "witcher_cs_tab",
        "witcher_cutscene_loaded_actor_index",
        "witcher_cutscene_loaded_anim_index",
        "witcher_cutscene_burned_audio_event",
        "witcher_cutscene_burned_audio_item_path",
        "witcher_cutscene_burned_audio_default_volume",
        "witcher_cutscene_point_tags",
        "witcher_cutscene_last_level_loaded",
        "witcher_cutscene_used_in_files",
        "witcher_cutscene_export_metadata_synced",
        "witcher_cs_entry_event_idx",
        "witcher_cs_event_target",
        "witcher_cs_fade_before",
        "witcher_cs_fade_after",
        "witcher_cs_cam_blend_in",
        "witcher_cs_cam_blend_out",
        "witcher_cs_blackscreen",
        "witcher_cs_check_actors_pos",
        "witcher_cs_reverb_name",
        "witcher_cs_audio_track",
        "witcher_cs_ent_to_hide_tags",
        "witcher_cutscene_info_tab",
        "witcher_cutscene_event_scope_tab",
        "witcher_cutscene_events_tab",
        "witcher_cs_events_anim_idx",
        "witcher_cs_event_view",
        "witcher_cutscene_actor_replace_source",
        "witcher_cutscene_retarget_camera_template",
        "witcher_cutscene_retarget_male_template",
        "witcher_cutscene_retarget_female_template",
        "witcher_cutscene_template_fields",
        "witcher_cutscene_effect_items",
        "witcher_cutscene_dialog_items",
        "witcher_cutscene_dialog_index",
        "witcher_cutscene_dialog_lines",
        "witcher_cutscene_dialog_line_index",
        "witcher_cutscene_dialog_id_space",
        "witcher_cutscene_dialog_strings_path",
        "witcher_cutscene_show_dialog_subtitles",
        "witcher_cutscene_subtitle_font_size",
    ):
        if hasattr(bpy.types.Scene, prop):
            delattr(bpy.types.Scene, prop)
    if hasattr(bpy.types.Object, "witcher_cutscene_actor_type"):
        del bpy.types.Object.witcher_cutscene_actor_type
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
