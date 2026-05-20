import csv
import hashlib
import logging
import math
import re
import time
from pathlib import Path

import bpy
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, IntProperty, StringProperty
from bpy_extras.io_utils import ImportHelper

from .. import dialog_language
from ..extension_paths import get_cache_root
from . import apply as lipsync_apply
from . import radish_runner, redkit_project, stt


log = logging.getLogger(__name__)

LIPSYNC_STRIP_SOURCES = {"wav_lipsync", "text_lipsync"}
LIPSYNC_LINES_PROP = "witcher_lipsync_lines"
LIPSYNC_LINE_INDEX_PROP = "witcher_lipsync_line_index"
LIPSYNC_REDKIT_PROJECT_PROP = "witcher_lipsync_redkit_project"
LIPSYNC_TARGET_SPEAKER_HINTS = (
    ("cirilla", "CIRI"),
    ("ciri", "CIRI"),
    ("yennefer", "YENN"),
    ("yenn", "YENN"),
    ("triss", "TRSS"),
    ("tris", "TRSS"),
    ("shani", "SHNI"),
    ("shni", "SHNI"),
    ("annahenrietta", "ANHE"),
    ("henrietta", "ANHE"),
    ("anhe", "ANHE"),
    ("syanna", "SYAN"),
    ("syan", "SYAN"),
    ("geralt", "GRLT"),
    ("grlt", "GRLT"),
    ("player", "GRLT"),
)
_EDITOR_SYNCING = False
_REDKIT_PROJECT_ENUM_CACHE = []


def _scene_tools_path(scene):
    path = str(getattr(scene, "witcher_lipsync_radish_tools_path", "") or "").strip()
    if not path:
        try:
            path = str(scene.get("witcher_lipsync_radish_tools_path", "") or "").strip()
        except Exception:
            path = ""
    return bpy.path.abspath(path) if path else ""


def _addon_preferences(context):
    addons = getattr(getattr(context, "preferences", None), "addons", None)
    if not addons:
        return None

    addon_module = (__package__ or "").rsplit(".", 1)[0]
    for module in (addon_module, "witcher3_tools", "io_import_w2l"):
        if not module:
            continue
        try:
            addon = addons.get(module)
        except Exception:
            addon = None
        if addon is not None:
            return getattr(addon, "preferences", None)
    return None


def _addon_tools_path(context):
    prefs = _addon_preferences(context)
    path = str(getattr(prefs, "radish_tools_path", "") or "").strip()
    return bpy.path.abspath(path) if path else ""


def _radish_tools_path(context):
    prefs_path = _addon_tools_path(context)
    if prefs_path:
        return prefs_path
    legacy_path = _scene_tools_path(context.scene)
    if legacy_path:
        prefs = _addon_preferences(context)
        if prefs is not None and hasattr(prefs, "radish_tools_path"):
            try:
                if not str(getattr(prefs, "radish_tools_path", "") or "").strip():
                    prefs.radish_tools_path = legacy_path
            except Exception:
                pass
    return legacy_path


def _scene_stt_model_path(scene):
    path = str(getattr(scene, "witcher_lipsync_stt_model_path", "") or "").strip()
    return bpy.path.abspath(path) if path else ""


def _scene_redkit_output_path(scene):
    path = str(getattr(scene, "witcher_lipsync_redkit_output_path", "") or "").strip()
    return bpy.path.abspath(path) if path else ""


def _scene_wwise_console_path(scene):
    path = str(getattr(scene, "witcher_lipsync_wwise_console_path", "") or "").strip()
    if not path:
        try:
            path = str(scene.get("witcher_lipsync_wwise_console_path", "") or "").strip()
        except Exception:
            path = ""
    return bpy.path.abspath(path) if path else ""


def _addon_wwise_console_path(context):
    prefs = _addon_preferences(context)
    path = str(getattr(prefs, "wwise_console_path", "") or "").strip()
    return bpy.path.abspath(path) if path else ""


def _wwise_console_path(context):
    prefs_path = _addon_wwise_console_path(context)
    if prefs_path:
        return prefs_path
    legacy_path = _scene_wwise_console_path(context.scene)
    if legacy_path:
        prefs = _addon_preferences(context)
        if prefs is not None and hasattr(prefs, "wwise_console_path"):
            try:
                if not str(getattr(prefs, "wwise_console_path", "") or "").strip():
                    prefs.wwise_console_path = legacy_path
            except Exception:
                pass
    return legacy_path


def _safe_cache_component(value, fallback="project", max_length=72):
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip()).strip("._-")
    name = name[:max_length].rstrip("._-")
    return name or fallback


def _lipsync_project_cache_name(project_path):
    project_path = Path(project_path)
    try:
        project_key = str(project_path.resolve(strict=False))
    except OSError:
        project_key = str(project_path)
    digest = hashlib.sha1(project_key.casefold().encode("utf-8", "replace")).hexdigest()[:8]
    return f"{_safe_cache_component(project_path.name, 'redkit_project')}_{digest}"


def _lipsync_cache_root(create=True, context=None):
    root = Path(get_cache_root(create=create)) / "lipsync"
    if context is not None:
        project_path = redkit_project.get_active_project_path(context)
        if project_path:
            root = root / "redkit_projects" / _lipsync_project_cache_name(project_path)
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def _active_project_index(context):
    prefs = _addon_preferences(context)
    try:
        return int(getattr(prefs, "redkit_projects_index", 0) or 0)
    except Exception:
        return 0


def _redkit_project_enum_items(self, context):
    global _REDKIT_PROJECT_ENUM_CACHE
    items = []
    for index, project_path in redkit_project.iter_project_paths(context):
        name = project_path.name or f"Project {index + 1}"
        items.append((str(index), name, str(project_path), 'FILE_FOLDER', index))
    if not items:
        items.append(("NONE", "No REDkit Project", "Add REDkit projects in Add-on Preferences", 'ERROR', 0))
    _REDKIT_PROJECT_ENUM_CACHE = items
    return _REDKIT_PROJECT_ENUM_CACHE


def _on_redkit_project_update(self, context):
    value = str(getattr(self, LIPSYNC_REDKIT_PROJECT_PROP, "") or "")
    if value == "NONE":
        return
    try:
        redkit_project.set_active_project_index(context, int(value))
    except Exception:
        log.debug("Could not set active REDkit project from lipsync selector", exc_info=True)


def _sync_project_selector_from_preferences(scene, context):
    projects = redkit_project.iter_project_paths(context)
    if not projects:
        try:
            if str(getattr(scene, LIPSYNC_REDKIT_PROJECT_PROP, "") or "") != "NONE":
                setattr(scene, LIPSYNC_REDKIT_PROJECT_PROP, "NONE")
        except Exception:
            pass
        return
    valid_indices = {str(index) for index, _path in projects}
    current = str(_active_project_index(context))
    if current not in valid_indices:
        current = projects[0][0]
        redkit_project.set_active_project_index(context, current)
        current = str(current)
    try:
        if str(getattr(scene, LIPSYNC_REDKIT_PROJECT_PROP, "") or "") != current:
            setattr(scene, LIPSYNC_REDKIT_PROJECT_PROP, current)
    except Exception:
        pass


def _active_project_path(context):
    return redkit_project.get_active_project_path(context)


def _resolve_start_frame(scene):
    return float(scene.frame_current if getattr(scene, "witcher_anim_nla_mode", "REPLACE") == "APPEND_AT_CURSOR" else 0.0)


def _resolve_target_armature(context):
    from ..ui import ui_voice

    return ui_voice._resolve_voice_target_armature(context)


def _speaker_from_text_hint(value):
    normalized = re.sub(r"[^a-z0-9]+", "", str(value or "").lower())
    if not normalized:
        return ""
    for hint, speaker in LIPSYNC_TARGET_SPEAKER_HINTS:
        if hint in normalized:
            return speaker
    return ""


def _target_speaker_sources(armature):
    if armature is None:
        return []

    sources = []
    data = getattr(armature, "data", None)
    rig_settings = getattr(data, "witcherui_RigSettings", None)
    if rig_settings is not None:
        for attr_name in ("entity_name", "repo_path", "name"):
            value = getattr(rig_settings, attr_name, "")
            if value:
                sources.append(value)

    for owner in (armature, data):
        if owner is None:
            continue
        for prop_name in (
            "witcher_entity_name",
            "witcher_entity_path",
            "witcher_name",
            "entity_name",
            "repo_path",
        ):
            try:
                value = owner.get(prop_name, "")
            except Exception:
                value = ""
            if value:
                sources.append(value)
        name = getattr(owner, "name", "")
        if name:
            sources.append(name)
    return sources


def _derive_speaker_from_target(context):
    armature = _resolve_target_armature(context)
    for value in _target_speaker_sources(armature):
        speaker = _speaker_from_text_hint(value)
        if speaker:
            return speaker
    return ""


def _default_lipsync_speaker(context):
    scene = context.scene
    current = str(getattr(scene, "witcher_lipsync_speaker", "") or "").strip()
    derived = _derive_speaker_from_target(context)
    if derived and (not current or current.upper() == "GRLT"):
        return derived
    return current or derived or "GRLT"


def _line_id_from_wav_path(wav_path):
    if not wav_path:
        return ""
    match = re.match(r"^(\d+)", Path(wav_path).name)
    if not match:
        return ""
    return radish_runner.normalize_line_id(match.group(1))


def _resolve_line_id(context, wav_path=None):
    scene = context.scene
    raw_line_id = str(getattr(scene, "witcher_lipsync_line_id", "") or "").strip()
    line_id = radish_runner.normalize_line_id(raw_line_id)
    if raw_line_id and not line_id:
        raise RuntimeError(f"Line ID must be {radish_runner.MAX_RADISH_LINE_ID} or lower.")
    if line_id:
        return line_id
    wav_line_id = _line_id_from_wav_path(wav_path)
    if wav_line_id:
        return wav_line_id

    project_id_info = redkit_project.get_active_project_id_info(context)
    if project_id_info:
        return str(project_id_info.next_line_id)
    return radish_runner.make_line_id()


def _editor_lines(scene):
    return getattr(scene, LIPSYNC_LINES_PROP, None)


def _editor_line_index(scene):
    try:
        return int(getattr(scene, LIPSYNC_LINE_INDEX_PROP, -1))
    except Exception:
        return -1


def _active_editor_line(scene):
    lines = _editor_lines(scene)
    if lines is None:
        return None
    index = _editor_line_index(scene)
    if 0 <= index < len(lines):
        return lines[index]
    for line in lines:
        if _line_matches_lipsync_filters(scene, line):
            return line
    return lines[0] if len(lines) else None


def _line_display_name(line):
    line_id = str(getattr(line, "line_id", "") or "").strip() or "<new>"
    speaker = str(getattr(line, "speaker", "") or "").strip()
    text = str(getattr(line, "text", "") or "").strip()
    wav_name = Path(str(getattr(line, "wav_path", "") or "")).name
    detail = text or wav_name or "Untitled"
    return f"{line_id} [{speaker}] {detail}" if speaker else f"{line_id} {detail}"


def _duration_text(seconds):
    try:
        value = float(seconds or 0.0)
    except (TypeError, ValueError):
        value = 0.0
    if value <= 0.0:
        return ""
    return str(round(value, 2))


def _line_duration_from_wav(path):
    try:
        path = Path(bpy.path.abspath(str(path or "")))
    except Exception:
        path = Path(str(path or ""))
    return _duration_text(redkit_project.wav_duration_seconds(path)) if path.is_file() else ""


def _strip_duration_text(context, strip):
    if strip is None:
        return ""
    fps = 0.0
    try:
        fps = float(context.scene.render.fps) / float(context.scene.render.fps_base or 1.0)
    except Exception:
        fps = 0.0
    if fps <= 0.0:
        return ""
    try:
        frames = float(getattr(strip, "frame_final_duration", 0.0) or 0.0)
    except Exception:
        frames = 0.0
    return _duration_text(frames / fps) if frames > 0.0 else ""


def _line_list_label(scene, line):
    line_id = str(getattr(line, "line_id", "") or "").strip() or "<new>"
    speaker = str(getattr(line, "speaker", "") or "").strip()
    text = str(getattr(line, "text", "") or "").strip()
    wav_name = Path(str(getattr(line, "wav_path", "") or "")).name
    detail = _short_ui_text(text or wav_name or "Untitled", 44)
    prefix = f"[{speaker}] " if speaker else ""
    if not bool(getattr(scene, "witcher_lipsync_show_details", True)):
        return f"{prefix}{detail}"
    label = f"{line_id} {prefix}{detail}"
    duration = str(getattr(line, "duration", "") or "").strip()
    return f"{label} |{duration}" if duration else label


def _line_asset_status(line):
    parts = []
    if bool(getattr(line, "has_project_wav", False)):
        parts.append("Silent WAV" if bool(getattr(line, "project_wav_is_silent", False)) else "WAV")
    if bool(getattr(line, "has_project_wem", False)):
        parts.append("WEM")
    if bool(getattr(line, "has_project_re", False)):
        parts.append(".re")
    return " ".join(parts) if parts else "missing"


def _project_line_audio_source(line):
    if bool(getattr(line, "project_wav_is_silent", False)):
        return "text_lipsync"
    if bool(getattr(line, "has_project_wem", False) or getattr(line, "has_project_wav", False)):
        return "wav_lipsync"
    return ""


def _line_status_icon_tooltip(line):
    if line is None:
        return 'ADD', "New lipsync line"
    if str(getattr(line, "project_path", "") or "").strip():
        has_wem = bool(getattr(line, "has_project_wem", False))
        has_re = bool(getattr(line, "has_project_re", False))
        if has_wem and has_re:
            if bool(getattr(line, "project_wav_is_silent", False)):
                return 'CHECKMARK', "Project silent placeholder audio and .re lipsync exist"
            return 'CHECKMARK', "Project WEM audio and .re lipsync exist"
        missing = []
        if not has_wem:
            missing.append("WEM")
        if not has_re:
            missing.append(".re")
        return 'ERROR', f"Project line is missing {', '.join(missing)}"
    if str(getattr(line, "last_re", "") or "").strip():
        return 'CHECKMARK', "Generated .re lipsync available"
    if str(getattr(line, "last_output", "") or "").strip():
        return 'CHECKMARK', "Generated lipsync output available"
    if str(getattr(line, "wav_path", "") or "").strip():
        return 'SOUND', "WAV is linked; generate to create lipsync"
    return 'TEXT', "Draft line; add text or import WAV"


def _line_audio_source(line):
    if line is None:
        return ""
    source = str(getattr(line, "audio_source", "") or "").strip()
    if source == "text_lipsync" or bool(getattr(line, "project_wav_is_silent", False)):
        return "text_lipsync"
    if source in LIPSYNC_STRIP_SOURCES:
        return source
    project_source = _project_line_audio_source(line)
    if project_source:
        return project_source
    for attr_name in ("wav_path", "project_wav_path"):
        if str(getattr(line, attr_name, "") or "").strip():
            return "wav_lipsync"
    workspace = _existing_path(getattr(line, "last_workspace", ""))
    line_id = str(getattr(line, "line_id", "") or "").strip()
    if workspace and workspace.is_dir() and line_id:
        if (
            (workspace / f"{line_id}_text_only.wav").exists()
            or (workspace / f"{redkit_project.voiceover_name(getattr(line, 'speaker', 'GRLT'), line_id)}.wav").exists()
        ):
            return "text_lipsync"
    return ""


def _line_audio_icon_tooltip(line):
    source = _line_audio_source(line)
    if source == "wav_lipsync":
        return 'FILE_SOUND', "Audio: real WAV/WEM source"
    if source == "text_lipsync":
        return 'TEXT', "Audio: silent placeholder generated from text only"
    return None, ""


def _text_contains_filter(value, needle):
    needle = str(needle or "").strip().lower()
    if not needle:
        return True
    return needle in str(value or "").lower()


def _line_matches_lipsync_filters(scene, line):
    if line is None:
        return False

    any_missing_filter = bool(
        getattr(scene, "witcher_lipsync_filter_missing_wav", False)
        or getattr(scene, "witcher_lipsync_filter_missing_wem", False)
        or getattr(scene, "witcher_lipsync_filter_missing_re", False)
    )
    if bool(getattr(scene, "witcher_lipsync_filter_project_only", False)):
        if not str(getattr(line, "project_path", "") or "").strip():
            return False
    if any_missing_filter and not str(getattr(line, "project_path", "") or "").strip():
        return False

    if bool(getattr(scene, "witcher_lipsync_filter_missing_wav", False)) and bool(getattr(line, "has_project_wav", False)):
        return False
    if bool(getattr(scene, "witcher_lipsync_filter_missing_wem", False)) and bool(getattr(line, "has_project_wem", False)):
        return False
    if bool(getattr(scene, "witcher_lipsync_filter_missing_re", False)) and bool(getattr(line, "has_project_re", False)):
        return False

    speaker_filter = str(getattr(scene, "witcher_lipsync_filter_speaker", "") or "").strip()
    if speaker_filter and not _text_contains_filter(getattr(line, "speaker", ""), speaker_filter):
        return False

    resource_filter = str(getattr(scene, "witcher_lipsync_filter_resource", "") or "").strip()
    if resource_filter:
        resource_text = " ".join((
            str(getattr(line, "resource", "") or ""),
            str(getattr(line, "project_path", "") or ""),
        ))
        if not _text_contains_filter(resource_text, resource_filter):
            return False

    search_text = str(getattr(scene, "witcher_lipsync_filter_text", "") or "").strip()
    if search_text:
        haystack = " ".join((
            str(getattr(line, "line_id", "") or ""),
            str(getattr(line, "original_line_id", "") or ""),
            str(getattr(line, "text", "") or ""),
            str(getattr(line, "speaker", "") or ""),
            str(getattr(line, "duration", "") or ""),
            str(getattr(line, "voiceover", "") or ""),
            str(getattr(line, "resource", "") or ""),
            str(getattr(line, "key", "") or ""),
        ))
        if not _text_contains_filter(haystack, search_text):
            return False

    return True


def _filtered_lipsync_lines(scene):
    lines = _editor_lines(scene)
    if lines is None:
        return []
    return [line for line in lines if _line_matches_lipsync_filters(scene, line)]


def _selected_lipsync_lines(scene):
    lines = _editor_lines(scene)
    if lines is None:
        return []
    return [line for line in lines if bool(getattr(line, "selected", False))]


def _refresh_line_display_name(line):
    try:
        line.name = _line_display_name(line)
    except Exception:
        pass


def _line_id_exists(scene, line_id):
    line_id = str(line_id or "").strip()
    lines = _editor_lines(scene)
    return bool(line_id and lines is not None and any(str(item.line_id or "").strip() == line_id for item in lines))


def _make_new_editor_line_id(context):
    scene = context.scene
    project_id_info = redkit_project.get_active_project_id_info(context)
    if project_id_info:
        candidate = int(project_id_info.next_line_id)
        while candidate <= radish_runner.MAX_RADISH_LINE_ID:
            line_id = str(candidate)
            if not _line_id_exists(scene, line_id):
                return line_id
            candidate += 1

    for _attempt in range(100):
        line_id = radish_runner.make_line_id()
        if not _line_id_exists(scene, line_id):
            return line_id
    return radish_runner.make_line_id()


def _sync_scene_fields_from_line(scene, line):
    global _EDITOR_SYNCING
    if line is None:
        return
    was_syncing = _EDITOR_SYNCING
    _EDITOR_SYNCING = True
    try:
        scene.witcher_lipsync_text = str(line.text or "")
        scene.witcher_lipsync_speaker = str(line.speaker or "GRLT")
        scene.witcher_lipsync_line_id = str(line.line_id or "")
        if str(line.language or ""):
            try:
                scene.witcher_lipsync_language = str(line.language or "en")
            except Exception:
                pass
    finally:
        _EDITOR_SYNCING = was_syncing


def _sync_line_from_scene(scene, line):
    if line is None:
        return
    line.text = str(getattr(scene, "witcher_lipsync_text", "") or "")
    line.speaker = str(getattr(scene, "witcher_lipsync_speaker", "GRLT") or "GRLT")
    line.line_id = str(getattr(scene, "witcher_lipsync_line_id", "") or "")
    line.language = str(getattr(scene, "witcher_lipsync_language", "en") or "en")
    _refresh_line_display_name(line)


def _apply_project_voice_line(line, project_line):
    assets = project_line.assets
    line.line_id = str(project_line.line_id or "")
    line.original_line_id = str(project_line.line_id or "")
    line.text = str(project_line.text or "")
    line.speaker = str(project_line.speaker or "GRLT")
    line.language = str(project_line.language or "en")
    line.project_path = str(project_line.project_path or "")
    line.project_csv_path = str(project_line.csv_path or "")
    line.resource = str(project_line.resource or "")
    line.property_name = str(project_line.property_name or "")
    line.key = str(project_line.key or "")
    line.voiceover = str(project_line.voiceover or "")
    line.has_project_wav = bool(assets.has_wav)
    line.has_project_wem = bool(assets.has_wem)
    line.has_project_re = bool(assets.has_re)
    line.project_wav_is_silent = bool(getattr(assets, "wav_is_silent", False))
    line.duration = _duration_text(getattr(assets, "wav_duration", 0.0))
    line.project_wav_path = str(assets.wav_path or "")
    line.project_wem_path = str(assets.wem_path or "")
    line.project_re_path = str(assets.re_path or "")
    if assets.wav_path:
        line.wav_path = str(assets.wav_path)
    line.audio_source = _project_line_audio_source(line)
    if assets.re_path:
        line.last_re = str(assets.re_path)
    line.last_status = f"Project assets: {_line_asset_status(line)}"
    _refresh_line_display_name(line)


def _refresh_project_asset_status_for_line(line):
    project_path = str(getattr(line, "project_path", "") or "").strip()
    line_id = str(getattr(line, "line_id", "") or "").strip()
    if not project_path or not line_id:
        return False
    assets = redkit_project.find_project_line_assets(
        project_path,
        getattr(line, "language", "en"),
        line_id,
        voiceover=getattr(line, "voiceover", ""),
        speaker=getattr(line, "speaker", ""),
    )
    line.has_project_wav = bool(assets.has_wav)
    line.has_project_wem = bool(assets.has_wem)
    line.has_project_re = bool(assets.has_re)
    line.project_wav_is_silent = bool(getattr(assets, "wav_is_silent", False))
    if assets.wav_path:
        line.duration = _duration_text(getattr(assets, "wav_duration", 0.0))
    line.project_wav_path = str(assets.wav_path or "")
    line.project_wem_path = str(assets.wem_path or "")
    line.project_re_path = str(assets.re_path or "")
    if assets.wav_path:
        line.wav_path = str(assets.wav_path)
    project_source = _project_line_audio_source(line)
    if project_source:
        line.audio_source = project_source
    elif str(getattr(line, "audio_source", "") or "") != "text_lipsync":
        line.audio_source = ""
    if assets.re_path:
        line.last_re = str(assets.re_path)
    line.last_status = f"Project assets: {_line_asset_status(line)}"
    _refresh_line_display_name(line)
    return True


def _line_has_project(line):
    return bool(str(getattr(line, "project_path", "") or "").strip())


def _add_line_to_active_project(context, line, sync_from_scene=False):
    if line is None:
        raise RuntimeError("No lipsync line selected.")
    if _line_has_project(line):
        return line

    scene = context.scene
    project_path = _active_project_path(context)
    if not project_path:
        raise RuntimeError("No REDkit project selected.")

    if sync_from_scene:
        _sync_line_from_scene(scene, line)
    line_id = radish_runner.normalize_line_id(getattr(line, "line_id", ""))
    if not line_id:
        line_id = str(redkit_project.next_project_line_id(project_path).next_line_id)
        line.line_id = line_id

    text = str(getattr(line, "text", "") or "").strip()
    if not text:
        raise RuntimeError("Enter voiceline text before adding the line to the project.")

    speaker = radish_runner.normalize_speaker(getattr(line, "speaker", "GRLT"))
    language = str(getattr(line, "language", "") or getattr(scene, "witcher_lipsync_language", "en") or "en").lower()

    existing = redkit_project.find_project_voice_line(project_path, line_id, language=language, include_unvoiced=True)
    if existing is None:
        project_line = redkit_project.add_project_line(
            project_path,
            line_id,
            text,
            speaker,
            language=language,
            resource=getattr(line, "resource", ""),
            property_name=getattr(line, "property_name", "") or "Line text",
            key=getattr(line, "key", "") or line_id,
        )
        scene.witcher_lipsync_project_status = f"Added {line_id} to project strings"
        scene.witcher_lipsync_project_log = f"Added to {project_line.csv_path}"
    else:
        project_line = existing
        scene.witcher_lipsync_project_status = f"Linked {line_id} to project strings"
        scene.witcher_lipsync_project_log = f"Linked from {project_line.csv_path}"

    _apply_project_voice_line(line, project_line)
    line.selected = True
    _sync_scene_fields_from_line(scene, line)
    return line


def _on_lipsync_line_index_update(self, context):
    if _EDITOR_SYNCING:
        return
    scene = getattr(context, "scene", None)
    if scene is None:
        return
    line = _active_editor_line(scene)
    _sync_scene_fields_from_line(scene, line)
    if not getattr(scene, "witcher_lipsync_load_on_select", True):
        return
    try:
        _load_editor_line_result(context, line)
    except Exception as exc:
        log.exception("Failed to load selected lipsync line")
        scene.witcher_lipsync_last_status = "Load failed"
        scene.witcher_lipsync_last_log = str(exc)


def _on_lipsync_line_field_update(self, context):
    if _EDITOR_SYNCING:
        return
    scene = getattr(context, "scene", None)
    if scene is not None:
        _sync_line_from_scene(scene, _active_editor_line(scene))


def _set_active_editor_line(scene, line):
    global _EDITOR_SYNCING

    lines = _editor_lines(scene)
    if lines is None or line is None:
        return
    try:
        target_pointer = line.as_pointer()
    except Exception:
        target_pointer = None
    for index, item in enumerate(lines):
        try:
            same_item = item == line or (target_pointer is not None and item.as_pointer() == target_pointer)
        except Exception:
            same_item = item == line
        if same_item:
            was_syncing = _EDITOR_SYNCING
            _EDITOR_SYNCING = True
            try:
                setattr(scene, LIPSYNC_LINE_INDEX_PROP, index)
            finally:
                _EDITOR_SYNCING = was_syncing
            _sync_scene_fields_from_line(scene, item)
            return


def _add_editor_line(
    context,
    *,
    line_id="",
    text="",
    speaker="",
    language="",
    wav_path="",
    strip_name="",
    audio_source="",
    duration="",
):
    scene = context.scene
    lines = _editor_lines(scene)
    if lines is None:
        return None
    line = lines.add()
    line.line_id = str(line_id or _make_new_editor_line_id(context))
    line.text = str(text or "")
    line.speaker = str(speaker or _default_lipsync_speaker(context))
    line.language = str(language or getattr(scene, "witcher_lipsync_language", "en") or "en")
    line.wav_path = str(wav_path or "")
    line.strip_name = str(strip_name or "")
    line.audio_source = str(audio_source or ("wav_lipsync" if wav_path else ""))
    line.duration = str(duration or _line_duration_from_wav(wav_path))
    _refresh_line_display_name(line)
    _set_active_editor_line(scene, line)
    return line


def _find_editor_line_by_id(scene, line_id):
    line_id = str(line_id or "").strip()
    if not line_id:
        return None
    lines = _editor_lines(scene)
    if lines is None:
        return None
    for item in lines:
        if str(item.line_id or "").strip() == line_id:
            return item
    return None


def _upsert_editor_line(
    context,
    *,
    line_id="",
    text="",
    speaker="",
    language="",
    wav_path="",
    strip_name="",
    audio_source="",
    duration="",
):
    scene = context.scene
    line = _find_editor_line_by_id(scene, line_id)
    if line is None:
        line = _add_editor_line(
            context,
            line_id=line_id,
            text=text,
            speaker=speaker,
            language=language,
            wav_path=wav_path,
            strip_name=strip_name,
            audio_source=audio_source,
            duration=duration,
        )
    else:
        if text or not str(line.text or ""):
            line.text = str(text or "")
        if speaker:
            line.speaker = str(speaker)
        if language:
            line.language = str(language)
        if wav_path:
            line.wav_path = str(wav_path)
        if strip_name:
            line.strip_name = str(strip_name)
        if audio_source:
            line.audio_source = str(audio_source)
        if duration:
            line.duration = str(duration)
        elif wav_path:
            line.duration = _line_duration_from_wav(wav_path)
        _refresh_line_display_name(line)
        _set_active_editor_line(scene, line)
    return line


def _line_for_generation(context, line_id):
    scene = context.scene
    active = _active_editor_line(scene)
    active_line_id = str(getattr(active, "line_id", "") or "").strip() if active is not None else ""
    if active is not None and (not active_line_id or active_line_id == str(line_id or "").strip()):
        return active
    return _find_editor_line_by_id(scene, line_id) or _add_editor_line(context, line_id=line_id)


def _update_editor_line_from_result(context, line, job, wav_path, soundstrip, audio_source="", duration=""):
    if line is None:
        line = _line_for_generation(context, job.line_id)
    if line is None:
        return
    scene = context.scene
    line.line_id = str(job.line_id or "")
    line.text = str(job.text or "")
    line.speaker = str(job.speaker or "")
    line.language = str(job.language or "")
    if wav_path:
        line.wav_path = str(wav_path)
    if soundstrip is not None:
        line.strip_name = str(getattr(soundstrip, "name", "") or "")
    if audio_source:
        line.audio_source = str(audio_source)
    if duration:
        line.duration = str(duration)
    elif wav_path:
        line.duration = _line_duration_from_wav(wav_path)
    line.voiceover = redkit_project.voiceover_name(job.speaker, job.line_id)
    line.last_status = str(getattr(scene, "witcher_lipsync_last_status", "") or "")
    line.last_output = str(getattr(scene, "witcher_lipsync_last_output", "") or "")
    line.last_re = str(getattr(scene, "witcher_lipsync_last_re", "") or "")
    line.last_workspace = str(getattr(scene, "witcher_lipsync_last_workspace", "") or "")
    _refresh_project_asset_status_for_line(line)
    _refresh_line_display_name(line)
    _set_active_editor_line(scene, line)


class WitcherLipsyncLineItem(bpy.types.PropertyGroup):
    selected: BoolProperty(name="Batch", default=False)
    line_id: StringProperty(name="Line ID", default="")
    original_line_id: StringProperty(name="Original Line ID", default="")
    text: StringProperty(name="Voiceline", default="")
    speaker: StringProperty(name="Speaker", default="GRLT")
    language: StringProperty(name="Language", default="en")
    duration: StringProperty(name="Duration", default="")
    wav_path: StringProperty(name="WAV", default="", subtype="FILE_PATH")
    audio_source: StringProperty(name="Audio Source", default="")
    strip_name: StringProperty(name="Strip", default="")
    project_path: StringProperty(name="Project", default="", subtype="DIR_PATH")
    project_csv_path: StringProperty(name="Project Strings", default="", subtype="FILE_PATH")
    resource: StringProperty(name="Resource", default="")
    property_name: StringProperty(name="Property", default="")
    key: StringProperty(name="Key", default="")
    voiceover: StringProperty(name="Voiceover", default="")
    has_project_wav: BoolProperty(name="Has WAV", default=False)
    has_project_wem: BoolProperty(name="Has WEM", default=False)
    has_project_re: BoolProperty(name="Has .re", default=False)
    project_wav_is_silent: BoolProperty(name="Silent Project WAV", default=False)
    project_wav_path: StringProperty(name="Project WAV", default="", subtype="FILE_PATH")
    project_wem_path: StringProperty(name="Project WEM", default="", subtype="FILE_PATH")
    project_re_path: StringProperty(name="Project .re", default="", subtype="FILE_PATH")
    last_status: StringProperty(name="Status", default="")
    last_output: StringProperty(name="Output", default="")
    last_re: StringProperty(name=".re", default="")
    last_workspace: StringProperty(name="Workspace", default="")


class WITCH_OT_lipsync_line_status_hint(bpy.types.Operator):
    bl_idname = "witcher.lipsync_line_status_hint"
    bl_label = "Lipsync Line Status"
    bl_options = {"INTERNAL"}

    tooltip: StringProperty(default="", options={"HIDDEN"})

    @classmethod
    def description(cls, context, properties):
        return str(getattr(properties, "tooltip", "") or "Lipsync line status")

    def execute(self, context):
        if self.tooltip:
            self.report({"INFO"}, self.tooltip)
        return {"FINISHED"}


class WITCHER_UL_lipsync_lines(bpy.types.UIList):
    use_filter_show = False
    use_filter_sort_alpha = False

    def _force_unsorted(self):
        for attr_name in ("use_filter_sort_alpha", "use_filter_sort_reverse"):
            try:
                setattr(self, attr_name, False)
            except Exception:
                pass

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        self._force_unsorted()
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            row.prop(item, "selected", text="")
            status_icon, status_tooltip = _line_status_icon_tooltip(item)
            op = row.operator(
                WITCH_OT_lipsync_line_status_hint.bl_idname,
                text="", icon=status_icon, emboss=False,
            )
            op.tooltip = status_tooltip
            audio_icon, audio_tooltip = _line_audio_icon_tooltip(item)
            if audio_icon:
                op = row.operator(
                    WITCH_OT_lipsync_line_status_hint.bl_idname,
                    text="", icon=audio_icon, emboss=False,
                )
                op.tooltip = audio_tooltip
            row.label(text=_line_list_label(context.scene, item))
        else:
            layout.label(text=str(item.name or item.line_id or "Line"))

    def draw_filter(self, context, layout):
        pass

    def filter_items(self, context, data, propname):
        self._force_unsorted()
        scene = getattr(context, "scene", None)
        lines = getattr(data, propname, None)
        if scene is None or lines is None:
            return [], []
        flags = [
            self.bitflag_filter_item if _line_matches_lipsync_filters(scene, item) else 0
            for item in lines
        ]
        return flags, []


class WITCH_MT_lipsync_line_list_actions(bpy.types.Menu):
    bl_idname = "WITCH_MT_lipsync_line_list_actions"
    bl_label = "Lipsync Line Actions"

    def draw(self, context):
        layout = self.layout
        layout.operator(
            WITCH_OT_load_selected_lipsync_voiceline.bl_idname,
            text="Add Line From Selected Sequencer Audio",
            icon='SEQUENCE',
        )
        layout.operator(
            WITCH_OT_add_lipsync_line_from_voice_browser.bl_idname,
            text="Add From Dialog Browser",
            icon='TEXT',
        )
        layout.separator()
        layout.operator(
            WITCH_OT_load_active_lipsync_editor_line.bl_idname,
            text="Load Generated Result",
            icon='PLAY',
        )
        layout.operator(
            WITCH_OT_apply_lipsync_voiceline_to_selected.bl_idname,
            text="Apply Line To Selected Sequencer Audio",
            icon='FILE_TICK',
        )


def _read_project_line_metadata(context, line_id, language):
    project_path = redkit_project.get_active_project_path(context)
    if not project_path:
        return "", ""

    csv_path = Path(project_path) / redkit_project.PROJECT_STRINGS_CSV
    if not csv_path.is_file():
        return "", ""

    lang_column = str(language or "en").upper()
    try:
        with open(csv_path, "r", encoding="utf-8", errors="replace", newline="") as handle:
            reader = csv.DictReader(handle, delimiter=";")
            for row in reader:
                if str(row.get("ID", "") or "").strip() != str(line_id):
                    continue
                text = str(row.get(lang_column, "") or row.get("EN", "") or "").strip()
                voiceover = str(row.get("VOICEOVER", "") or "").strip()
                suffix = f"_{line_id}"
                speaker = voiceover[:-len(suffix)] if voiceover.upper().endswith(suffix.upper()) else voiceover
                return text, speaker
    except Exception:
        log.debug("Could not read REDkit project strings for lipsync metadata", exc_info=True)
    return "", ""


def _ensure_face_morphs(context, armature):
    from ..ui import ui_voice

    if armature is None:
        raise RuntimeError("No character target armature found.")

    ui_voice._auto_load_face_morphs(context, armature)
    if not ui_voice._armature_has_face_morphs(armature):
        raise RuntimeError("Load Face Morphs on the character before generating lipsync.")

    pose_bone = armature.pose.bones.get("w3_face_poses") if armature.pose else None
    if pose_bone is None:
        raise RuntimeError("The target armature is missing the w3_face_poses pose bone.")
    return pose_bone


def _strip_get(strip, prop_name, default=""):
    try:
        return strip.get(prop_name, default)
    except Exception:
        return default


def _strip_set(strip, prop_name, value):
    try:
        strip[prop_name] = value
        return True
    except Exception:
        log.debug("Could not set sound strip property %s", prop_name, exc_info=True)
        return False


def _strip_has_prop(strip, prop_name):
    try:
        return prop_name in strip.keys()
    except Exception:
        return False


def _get_sequence_editor_strips(scene):
    try:
        from ..ui import ui_voice

        return ui_voice._get_sequence_editor_strips(getattr(scene, "sequence_editor", None))
    except Exception:
        log.debug("Could not access sequence editor strips", exc_info=True)
    return None


def _iter_selected_sequence_strips(context):
    scene = getattr(context, "scene", None)
    sequence_editor = getattr(scene, "sequence_editor", None) if scene is not None else None
    strips = _get_sequence_editor_strips(scene) if scene is not None else None

    seen = set()

    def add_candidate(strip):
        if strip is None:
            return
        key = getattr(strip, "name", None) or id(strip)
        if key in seen:
            return
        seen.add(key)
        candidates.append(strip)

    candidates = []
    add_candidate(getattr(sequence_editor, "active_strip", None))
    for strip in list(getattr(context, "selected_sequences", []) or []):
        add_candidate(strip)
    if strips is not None:
        for strip in list(strips):
            if bool(getattr(strip, "select", False)):
                add_candidate(strip)
    return candidates


def _strip_dialog_text(strip):
    for prop_name in (
        dialog_language.DIALOG_SUBTITLE_TEXT_PROP,
        "witcher_cutscene_dialog_text",
        "witcher_w2scene_dialog_text",
    ):
        text = str(_strip_get(strip, prop_name, "") or "").strip()
        if text:
            return text
    return ""


def _strip_dialog_line_id(strip):
    for prop_name in (
        dialog_language.DIALOG_SUBTITLE_LINE_ID_PROP,
        "witcher_cutscene_dialog_line_id",
        "witcher_w2scene_dialog_line_id",
    ):
        line_id = str(_strip_get(strip, prop_name, "") or "").strip()
        if line_id:
            return line_id
    return ""


def _is_dialog_sound_strip(strip):
    if getattr(strip, "type", None) != "SOUND":
        return False
    return bool(
        str(_strip_get(strip, dialog_language.DIALOG_SUBTITLE_SOURCE_PROP, "") or "").strip()
        or _strip_dialog_text(strip)
        or _strip_dialog_line_id(strip)
        or str(_strip_get(strip, dialog_language.DIALOG_SUBTITLE_SPEAKER_PROP, "") or "").strip()
    )


def _selected_sound_strip(context, require_dialog=False):
    fallback = None
    for strip in _iter_selected_sequence_strips(context):
        if getattr(strip, "type", None) != "SOUND":
            continue
        if fallback is None:
            fallback = strip
        if not require_dialog or _is_dialog_sound_strip(strip):
            return strip
    return None if require_dialog else fallback


def _apply_dialog_metadata_to_strip(context, strip, text, line_id, speaker, language):
    text_language = dialog_language.get_active_text_language(context)
    source = str(_strip_get(strip, dialog_language.DIALOG_SUBTITLE_SOURCE_PROP, "") or "").strip() or "wav_lipsync"
    props = {
        dialog_language.DIALOG_SUBTITLE_TEXT_PROP: text,
        dialog_language.DIALOG_SUBTITLE_LINE_ID_PROP: line_id,
        dialog_language.DIALOG_SUBTITLE_SPEAKER_PROP: speaker,
        dialog_language.DIALOG_SUBTITLE_SOURCE_PROP: source,
        dialog_language.DIALOG_SUBTITLE_LANGUAGE_PROP: text_language,
        dialog_language.DIALOG_AUDIO_LANGUAGE_PROP: language,
    }
    for prop_name, value in props.items():
        _strip_set(strip, prop_name, value)

    for legacy_prop in ("witcher_cutscene_dialog_text", "witcher_w2scene_dialog_text"):
        if _strip_has_prop(strip, legacy_prop):
            _strip_set(strip, legacy_prop, text)
    for legacy_prop in ("witcher_cutscene_dialog_line_id", "witcher_w2scene_dialog_line_id"):
        if _strip_has_prop(strip, legacy_prop):
            _strip_set(strip, legacy_prop, line_id)


def _is_lipsync_sound_strip(strip):
    if getattr(strip, "type", None) != "SOUND":
        return False
    source = str(_strip_get(strip, dialog_language.DIALOG_SUBTITLE_SOURCE_PROP, "") or "")
    return source in LIPSYNC_STRIP_SOURCES


def _strip_starts_at(strip, start_frame):
    try:
        return int(round(float(getattr(strip, "frame_start", 0.0)))) == int(round(float(start_frame or 0.0)))
    except Exception:
        return False


def _remove_existing_lipsync_strips(strips, line_id, start_frame=None):
    line_id = str(line_id or "").strip()
    for strip in list(strips):
        if not _is_lipsync_sound_strip(strip):
            continue
        strip_line_id = str(_strip_get(strip, dialog_language.DIALOG_SUBTITLE_LINE_ID_PROP, "") or "")
        same_line = bool(line_id and strip_line_id == line_id)
        same_start = start_frame is not None and _strip_starts_at(strip, start_frame)
        if same_line or same_start:
            try:
                strips.remove(strip)
            except Exception:
                log.debug("Could not remove previous lipsync sound strip", exc_info=True)


def _import_audio_strip(context, wav_path, start_frame, line_id, text, speaker, language, source):
    from ..ui import ui_voice

    scene = context.scene
    if not scene.sequence_editor:
        scene.sequence_editor_create()

    strips = ui_voice._get_sequence_editor_strips(scene.sequence_editor)
    if strips is None:
        raise RuntimeError("Blender sequence editor strips API is unavailable.")

    replace_audio = bool(getattr(scene, "witcher_lipsync_replace_audio", False))
    if replace_audio:
        for strip in [strip for strip in strips if strip.type == "SOUND"]:
            strips.remove(strip)
    else:
        _remove_existing_lipsync_strips(strips, line_id, start_frame=start_frame)

    wav_path = Path(wav_path)
    channel = 1 if replace_audio else ui_voice._get_next_sound_channel(scene)
    soundstrip = strips.new_sound(
        wav_path.stem,
        str(wav_path),
        channel=channel,
        frame_start=math.ceil(start_frame) + 1,
    )
    soundstrip.frame_start = start_frame

    strip_props = {
        dialog_language.DIALOG_SUBTITLE_TEXT_PROP: text,
        dialog_language.DIALOG_SUBTITLE_LINE_ID_PROP: line_id,
        dialog_language.DIALOG_SUBTITLE_SPEAKER_PROP: speaker,
        dialog_language.DIALOG_SUBTITLE_SOURCE_PROP: source,
        dialog_language.DIALOG_SUBTITLE_SOURCE_PATH_PROP: str(wav_path),
        dialog_language.DIALOG_SUBTITLE_LANGUAGE_PROP: dialog_language.get_active_text_language(context),
        dialog_language.DIALOG_AUDIO_LANGUAGE_PROP: language,
    }
    for prop_name, prop_value in strip_props.items():
        try:
            soundstrip[prop_name] = prop_value
        except Exception:
            log.debug("Could not tag sound strip with %s", prop_name, exc_info=True)
    try:
        soundstrip.select = True
        scene.sequence_editor.active_strip = soundstrip
    except Exception:
        pass

    strip_end = int(math.ceil(soundstrip.frame_final_end))
    if strip_end > scene.frame_end:
        scene.frame_end = strip_end
    return soundstrip


def _existing_path(path_value):
    path_value = str(path_value or "").strip()
    if not path_value:
        return None
    try:
        path = Path(bpy.path.abspath(path_value))
    except Exception:
        path = Path(path_value)
    return path if path.exists() else None


def _line_audio_path(line):
    source_hint = _line_audio_source(line)
    workspace = _existing_path(getattr(line, "last_workspace", ""))
    line_id = str(getattr(line, "line_id", "") or "").strip()
    if source_hint == "text_lipsync" and workspace and workspace.is_dir() and line_id:
        for silent_wav in (
            workspace / f"{line_id}_text_only.wav",
            workspace / f"{redkit_project.voiceover_name(getattr(line, 'speaker', 'GRLT'), line_id)}.wav",
        ):
            if silent_wav.exists():
                return silent_wav, "text_lipsync"

    for attr_name in ("wav_path", "project_wav_path"):
        wav_path = _existing_path(getattr(line, attr_name, ""))
        if wav_path:
            return wav_path, "text_lipsync" if source_hint == "text_lipsync" else "wav_lipsync"

    if workspace and workspace.is_dir() and line_id:
        for silent_wav in (
            workspace / f"{line_id}_text_only.wav",
            workspace / f"{redkit_project.voiceover_name(getattr(line, 'speaker', 'GRLT'), line_id)}.wav",
        ):
            if silent_wav.exists():
                return silent_wav, "text_lipsync"
    return None, "text_lipsync"


def _project_voiceover_stem(line):
    voiceover = str(getattr(line, "voiceover", "") or "").strip()
    if voiceover:
        return voiceover
    return redkit_project.voiceover_name(
        getattr(line, "speaker", "GRLT"),
        getattr(line, "line_id", ""),
    )


def _expected_project_output_path(line, folder_name, suffix):
    project_path = str(getattr(line, "project_path", "") or "").strip()
    line_id = str(getattr(line, "line_id", "") or "").strip()
    if not project_path or not line_id:
        return None
    language = str(getattr(line, "language", "en") or "en").strip().lower() or "en"
    return Path(project_path) / "speech" / language / folder_name / f"{_project_voiceover_stem(line)}{suffix}"


def _existing_or_expected_project_output(line, attr_name, folder_name, suffix):
    path = _existing_path(getattr(line, attr_name, ""))
    if path:
        return path
    expected = _expected_project_output_path(line, folder_name, suffix)
    if expected and expected.is_file():
        return expected
    return None


def _project_generation_overwrite_paths(line):
    paths = []
    for path in (
        _existing_or_expected_project_output(line, "project_re_path", "lipsync", ".re"),
        _existing_or_expected_project_output(line, "project_wem_path", "audio", ".wem"),
    ):
        if path and path not in paths:
            paths.append(path)
    return paths


def _project_generation_missing_outputs(line, require_wem=False, require_re=False):
    missing = []
    if require_wem and not bool(getattr(line, "has_project_wem", False)):
        expected = _expected_project_output_path(line, "audio", ".wem")
        missing.append(f"WEM missing: {expected}" if expected else "WEM missing")
    if require_re and not bool(getattr(line, "has_project_re", False)):
        expected = _expected_project_output_path(line, "lipsync", ".re")
        missing.append(f".re missing: {expected}" if expected else ".re missing")
    return missing


def _resolve_project_output_path(line, path):
    project_path = Path(str(getattr(line, "project_path", "") or ""))
    try:
        project_root = project_path.resolve(strict=False)
    except OSError:
        project_root = project_path.absolute()
    path = Path(path)
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        resolved = path.absolute()
    if resolved == project_root or project_root not in resolved.parents:
        raise RuntimeError(f"Refusing to overwrite outside project: {path}")
    return project_root, path, resolved


def _stash_project_generation_outputs(line, paths):
    moved = []
    if not paths:
        return moved
    backup_root = None
    for path in paths:
        project_root, path, resolved = _resolve_project_output_path(line, path)
        if not path.is_file():
            continue
        if backup_root is None:
            backup_root = project_root / redkit_project.PROJECT_BACKUP_DIR / "lipsync_overwrite" / str(time.time_ns())
        backup_path = backup_root / resolved.relative_to(project_root)
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        path.replace(backup_path)
        moved.append((backup_path, path))
    return moved


def _restore_project_generation_outputs(moved):
    for backup_path, target_path in reversed(moved):
        if not backup_path.is_file():
            continue
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if target_path.exists():
            target_path.unlink()
        backup_path.replace(target_path)


def _restore_missing_project_generation_outputs(moved):
    for backup_path, target_path in reversed(moved):
        if backup_path.is_file() and not target_path.exists():
            target_path.parent.mkdir(parents=True, exist_ok=True)
            backup_path.replace(target_path)


def _line_lipsync_re_path(line):
    for attr_name in ("project_re_path", "last_re"):
        re_path = _existing_path(getattr(line, attr_name, ""))
        if re_path and re_path.suffix.lower() == ".re":
            return re_path
    return None


def _is_hdf_re_file(path):
    try:
        with open(path, "rb") as handle:
            return handle.read(8) == b"\x89HDF\r\n\x1a\n"
    except OSError:
        return False


def _scene_import_nla_mode(scene):
    mode_map = {'REPLACE': 'replace', 'APPEND': 'append', 'APPEND_AT_CURSOR': 'append_at_cursor'}
    return mode_map.get(getattr(scene, 'witcher_anim_nla_mode', 'REPLACE'), 'replace')


def _voice_import_curve_count(armature, track_name="voice_import"):
    anim_data = getattr(armature, "animation_data", None)
    track = anim_data.nla_tracks.get(track_name) if anim_data else None
    if track is None:
        return 0, 0
    strips = list(getattr(track, "strips", []) or [])
    curve_count = 0
    for strip in strips:
        action = getattr(strip, "action", None)
        if action is None:
            continue
        try:
            curve_count += len(getattr(action, "fcurves", []) or [])
        except Exception:
            pass
    return len(strips), curve_count


def _load_editor_line_re_result(context, line, re_file, operator=None):
    from ..importers import import_anims

    scene = context.scene
    line_id = str(getattr(line, "line_id", "") or "").strip()
    text = str(getattr(line, "text", "") or getattr(scene, "witcher_lipsync_text", "") or "")
    speaker = radish_runner.normalize_speaker(getattr(line, "speaker", "") or getattr(scene, "witcher_lipsync_speaker", "GRLT"))
    language = str(getattr(line, "language", "") or getattr(scene, "witcher_lipsync_language", "en") or "en")

    armature = _resolve_target_armature(context)
    _ensure_face_morphs(context, armature)
    start_frame = _resolve_start_frame(scene)

    audio_path, source = _line_audio_path(line)

    nla_mode = _scene_import_nla_mode(scene)
    import_label = ""
    load_notes = []
    if _is_hdf_re_file(re_file):
        from ..ui import ui_re_anims

        stats = ui_re_anims.import_re_mimic_file(
            context,
            str(re_file),
            armature,
            nla_track_name="voice_import",
            start_frame=start_frame if nla_mode == "append_at_cursor" else 0,
            nla_mode=nla_mode,
        )
        import_label = f"{stats['morph_count']} morphs, {stats['frame_count']} frames"
        if int(stats.get("nonzero_key_count", 0) or 0) <= 0:
            import_label += ", silent"
            load_notes.append("Project .re contains only zero morph values; loaded as silent/empty animation.")
    else:
        before_strips, before_curves = _voice_import_curve_count(armature)
        import_anims.import_lipsync(
            context,
            str(re_file),
            use_NLA=True,
            NLA_track="voice_import",
            override_select=armature,
            at_frame=start_frame if nla_mode == "append_at_cursor" else 0,
            nla_mode=nla_mode,
        )
        after_strips, after_curves = _voice_import_curve_count(armature)
        added_curves = max(0, after_curves - before_curves)
        if after_strips <= before_strips and added_curves <= 0:
            raise RuntimeError(f"No lipsync curves were imported from {re_file}")
        import_label = f"{added_curves or after_curves} curves"
    if armature.animation_data:
        armature.animation_data.use_nla = True
    soundstrip = None
    if audio_path:
        soundstrip = _import_audio_strip(
            context,
            audio_path,
            start_frame,
            line_id,
            text,
            speaker,
            language,
            source=source,
        )
    if soundstrip is not None:
        line.strip_name = str(getattr(soundstrip, "name", "") or "")
    line.last_re = str(re_file)
    if not audio_path:
        load_notes.append("No source WAV found; loaded animation only.")
    line.last_status = f"Loaded project .re: {import_label}"
    scene.witcher_lipsync_last_status = f"Loaded {line_id}: project .re ({import_label})"
    scene.witcher_lipsync_last_re = str(re_file)
    scene.witcher_lipsync_last_log = "\n".join(load_notes)
    if operator is not None:
        operator.report({"WARNING"} if load_notes else {"INFO"}, scene.witcher_lipsync_last_status)
    return True


def _load_editor_line_result(context, line, operator=None):
    if line is None:
        if operator is not None:
            operator.report({"WARNING"}, "No lipsync line selected.")
        return False

    line_id = str(getattr(line, "line_id", "") or "").strip()
    if not line_id:
        if operator is not None:
            operator.report({"WARNING"}, "The active lipsync line has no line ID.")
        return False

    lipsyncanim_file = _existing_path(getattr(line, "last_output", ""))
    if not lipsyncanim_file or lipsyncanim_file.suffix.lower() != ".csv":
        re_file = _line_lipsync_re_path(line)
        if re_file:
            return _load_editor_line_re_result(context, line, re_file, operator=operator)
        if operator is not None:
            operator.report({"WARNING"}, "The active lipsync line has no generated CSV or project .re to load.")
        return False

    scene = context.scene
    text = str(getattr(line, "text", "") or getattr(scene, "witcher_lipsync_text", "") or "")
    speaker = radish_runner.normalize_speaker(getattr(line, "speaker", "") or getattr(scene, "witcher_lipsync_speaker", "GRLT"))
    language = str(getattr(line, "language", "") or getattr(scene, "witcher_lipsync_language", "en") or "en")

    armature = _resolve_target_armature(context)
    _ensure_face_morphs(context, armature)
    start_frame = _resolve_start_frame(scene)

    audio_path, source = _line_audio_path(line)
    soundstrip = None
    if audio_path:
        soundstrip = _import_audio_strip(
            context,
            audio_path,
            start_frame,
            line_id,
            text,
            speaker,
            language,
            source=source,
        )

    stats = lipsync_apply.apply_lipsyncanim_csv_to_armature(
        context,
        armature,
        lipsyncanim_file,
        action_name=f"{line_id}_{source}_morphs",
        start_frame=start_frame,
    )
    if soundstrip is not None:
        line.strip_name = str(getattr(soundstrip, "name", "") or "")
    line.last_status = f"Loaded: {stats['morph_count']} morphs"
    scene.witcher_lipsync_last_status = (
        f"Loaded {line_id}: {stats['morph_count']} morphs, frames {stats['start_frame']}-{stats['end_frame']}"
    )
    scene.witcher_lipsync_last_output = str(lipsyncanim_file)
    scene.witcher_lipsync_last_log = ""
    return True


def _csv_duration_seconds(csv_path, scene):
    _metadata, morph_values = lipsync_apply.read_lipsyncanim_csv(csv_path)
    frame_count = max(len(values) for values in morph_values.values())
    fps = lipsync_apply.scene_fps(scene)
    return frame_count / fps if fps else frame_count / 30.0


def _transcribe_wav_into_scene(context, wav_path):
    scene = context.scene
    text = stt.transcribe_wav(
        wav_path,
        model_path=_scene_stt_model_path(scene),
        n_threads=getattr(scene, "witcher_lipsync_stt_threads", 0),
    )
    scene.witcher_lipsync_text = text
    scene.witcher_lipsync_last_transcript = text
    return text


def _run_lipsync_generation(
    context,
    operator,
    wav_path=None,
    text_only=False,
    force_transcribe=False,
    editor_line=None,
    apply_to_scene=True,
    report_result=True,
    require_wem=False,
):
    scene = context.scene
    language = str(getattr(scene, "witcher_lipsync_language", "en") or "en").lower()
    editor_line_id = str(getattr(editor_line, "line_id", "") or "").strip() if editor_line is not None else ""
    line_id = radish_runner.normalize_line_id(editor_line_id) if editor_line_id else ""
    if not line_id:
        line_id = _resolve_line_id(context, wav_path=None if text_only else wav_path)
    if editor_line is None:
        active_line = _active_editor_line(scene)
        active_line_id = str(getattr(active_line, "line_id", "") or "").strip() if active_line is not None else ""
        if active_line is not None and (not active_line_id or active_line_id == line_id):
            editor_line = active_line

    text = str(getattr(scene, "witcher_lipsync_text", "") or "").strip()
    speaker_raw = str(getattr(scene, "witcher_lipsync_speaker", "GRLT") or "").strip()
    project_text, project_speaker = _read_project_line_metadata(context, line_id, language)
    if project_text and not text:
        scene.witcher_lipsync_text = project_text
        text = project_text
    if project_speaker and (not speaker_raw or speaker_raw.upper() == "GRLT"):
        scene.witcher_lipsync_speaker = project_speaker
        speaker_raw = project_speaker
    if apply_to_scene and not project_speaker and (not speaker_raw or speaker_raw.upper() == "GRLT"):
        target_speaker = _derive_speaker_from_target(context)
        if target_speaker:
            scene.witcher_lipsync_speaker = target_speaker
            speaker_raw = target_speaker

    should_transcribe = (
        not text_only
        and (force_transcribe or (not project_text and not text and getattr(scene, "witcher_lipsync_auto_transcribe", False)))
    )
    if should_transcribe:
        previous_text = text
        try:
            text = _transcribe_wav_into_scene(context, wav_path)
        except Exception:
            if force_transcribe or not previous_text:
                raise
            operator.report({"WARNING"}, "Speech-to-text failed; using existing voiceline text.")
            log.warning("Speech-to-text failed; using existing voiceline text", exc_info=True)

    speaker = radish_runner.normalize_speaker(speaker_raw)
    generate_re = bool(getattr(scene, "witcher_lipsync_generate_re", True))

    armature = None
    if apply_to_scene:
        armature = _resolve_target_armature(context)
        _ensure_face_morphs(context, armature)

    tools_dir = radish_runner.find_full_tools_dir(_radish_tools_path(context), include_converter=generate_re)
    project_path = _active_project_path(context)
    work_root = _lipsync_cache_root(create=True, context=context)
    stable_workspace = bool(project_path)
    if text_only:
        job = radish_runner.create_text_job(
            text=text,
            speaker=speaker,
            line_id=line_id,
            language=language,
            work_root=work_root,
            stable_workspace=stable_workspace,
        )
        result = radish_runner.run_text_phoneme_generator(job, tools_dir)
    else:
        job = radish_runner.create_job(
            wav_path,
            text=text,
            speaker=speaker,
            line_id=line_id,
            language=language,
            work_root=work_root,
            stable_workspace=stable_workspace,
        )
        result = radish_runner.run_phoneme_extractor(job, tools_dir)

    start_frame = _resolve_start_frame(scene) if apply_to_scene else 0.0
    re_file = None
    lipsyncanim_file = None
    soundstrip = None
    creator_result = radish_runner.run_lipsync_creator(
        job,
        result.phoneme_file,
        tools_dir,
        text_only=text_only,
    )
    lipsyncanim_file = creator_result.lipsyncanim_file
    generated_duration = _duration_text(_csv_duration_seconds(lipsyncanim_file, scene))
    if text_only:
        voiceover_name = radish_runner.voiceover_name(job.speaker, job.line_id)
        voiceover_wav = radish_runner.write_silent_wav(
            job.workspace / f"{voiceover_name}.wav",
            _csv_duration_seconds(lipsyncanim_file, scene) + 0.25,
        )
        audio_path = radish_runner.write_silent_wav(
            job.workspace / f"{job.line_id}_text_only.wav",
            _csv_duration_seconds(lipsyncanim_file, scene) + 0.25,
        )
        if not audio_path.exists():
            audio_path = voiceover_wav
        source = "text_lipsync"
    else:
        audio_path = radish_runner.find_job_audio_file(job)
        source = "wav_lipsync"
        generated_duration = _line_duration_from_wav(audio_path) or generated_duration

    if apply_to_scene:
        soundstrip = _import_audio_strip(
            context,
            audio_path,
            start_frame,
            job.line_id,
            job.text,
            job.speaker,
            job.language,
            source=source,
        )
        stats = lipsync_apply.apply_lipsyncanim_csv_to_armature(
            context,
            armature,
            lipsyncanim_file,
            action_name=f"{job.line_id}_{source}_morphs",
            start_frame=start_frame,
        )
    else:
        _metadata, morph_values = lipsync_apply.read_lipsyncanim_csv(lipsyncanim_file)
        frame_count = max((len(values) for values in morph_values.values()), default=0)
        stats = {
            "morph_count": len(morph_values),
            "start_frame": 0,
            "end_frame": max(frame_count - 1, 0),
        }

    wwise_log = ""
    converter_log = ""
    if generate_re:
        output_path = _scene_redkit_output_path(scene)
        if not output_path and editor_line is not None:
            output_path = str(getattr(editor_line, "project_path", "") or "").strip()
        should_generate_wem = bool(require_wem or output_path)
        if should_generate_wem:
            wwise_console = radish_runner.find_wwise_console(
                _wwise_console_path(context),
                tools_dir=tools_dir,
            )
            wwise_result = radish_runner.run_wwise_conversion(
                job,
                wwise_console,
                source_audio_dir=job.workspace,
                output_dir=lipsyncanim_file.parent,
            )
            wwise_log = wwise_result.compact_log
        converter_result = radish_runner.run_converter(
            job,
            lipsyncanim_file.parent,
            tools_dir,
            output_dir=output_path or None,
        )
        re_file = converter_result.redkit_lipsync_file
        converter_log = converter_result.compact_log

    combined_log = "\n".join(
        part for part in (result.compact_log, creator_result.compact_log, wwise_log, converter_log) if part
    )
    count_label = f"{stats['morph_count']} morphs"

    if apply_to_scene and hasattr(scene, "witcher_cutscene_show_dialog_subtitles"):
        scene.witcher_cutscene_show_dialog_subtitles = True

    mode_label = "Text-only" if text_only else "WAV"
    scene.witcher_lipsync_line_id = job.line_id
    scene.witcher_lipsync_text = job.text
    scene.witcher_lipsync_speaker = job.speaker
    scene.witcher_lipsync_last_workspace = str(job.workspace)
    scene.witcher_lipsync_last_output = str(lipsyncanim_file or result.phoneme_file)
    scene.witcher_lipsync_last_re = str(re_file or "")
    scene.witcher_lipsync_last_log = combined_log[-4000:] if len(combined_log) > 4000 else combined_log
    scene.witcher_lipsync_last_status = (
        f"{mode_label}: {count_label}, frames {stats['start_frame']}-{stats['end_frame']}"
    )
    _update_editor_line_from_result(
        context,
        editor_line,
        job,
        wav_path if not text_only else "",
        soundstrip,
        audio_source=source,
        duration=generated_duration,
    )
    if report_result:
        operator.report({"INFO"}, f"Generated {mode_label.lower()} lipsync for line {job.line_id}.")
    return {"FINISHED"}


class WITCH_OT_import_wav_lipsync(bpy.types.Operator, ImportHelper):
    bl_idname = "witcher.import_wav_lipsync"
    bl_label = "Import WAV + Generate Lipsync"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".wav"
    filter_glob: StringProperty(default="*.wav", options={"HIDDEN"})

    def execute(self, context):
        scene = context.scene
        try:
            return _run_lipsync_generation(
                context,
                self,
                wav_path=self.filepath,
                text_only=False,
                force_transcribe=False,
            )
        except Exception as exc:
            message = str(exc).strip() or exc.__class__.__name__
            scene.witcher_lipsync_last_log = message
            scene.witcher_lipsync_last_status = "Failed"
            self.report({"ERROR"}, message.splitlines()[0][:240])
            log.exception("WAV lipsync import failed")
            return {"CANCELLED"}


class WITCH_OT_generate_text_lipsync(bpy.types.Operator):
    bl_idname = "witcher.generate_text_lipsync"
    bl_label = "Generate Lipsync From Text"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        try:
            line = _active_editor_line(scene)
            if line is not None and not _line_has_project(line) and _active_project_path(context):
                _add_line_to_active_project(context, line, sync_from_scene=True)
            return _run_lipsync_generation(context, self, text_only=True, editor_line=line)
        except Exception as exc:
            message = str(exc).strip() or exc.__class__.__name__
            scene.witcher_lipsync_last_log = message
            scene.witcher_lipsync_last_status = "Failed"
            self.report({"ERROR"}, message.splitlines()[0][:240])
            log.exception("Text-only lipsync generation failed")
            return {"CANCELLED"}


class WITCH_OT_new_lipsync_editor_line(bpy.types.Operator):
    """Create a new editable lipsync line."""

    bl_idname = "witcher.new_lipsync_editor_line"
    bl_label = "New Lipsync Line"
    bl_description = "Create a new editable lipsync line with a new line ID"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        line = _add_editor_line(
            context,
            line_id=_make_new_editor_line_id(context),
            speaker=_default_lipsync_speaker(context),
            language=getattr(context.scene, "witcher_lipsync_language", "en"),
        )
        if line is None:
            self.report({"ERROR"}, "Could not create a lipsync line.")
            return {"CANCELLED"}
        line.selected = True
        context.scene.witcher_lipsync_last_status = "Created new lipsync line"
        return {"FINISHED"}


class WITCH_OT_remove_lipsync_editor_line(bpy.types.Operator):
    """Remove the active editable lipsync line from the editor list."""

    bl_idname = "witcher.remove_lipsync_editor_line"
    bl_label = "Remove Lipsync Line"
    bl_description = "Remove the active lipsync line from the editor list; generated strips are not deleted"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        lines = _editor_lines(scene)
        index = _editor_line_index(scene)
        if lines is None or index < 0 or index >= len(lines):
            self.report({"WARNING"}, "No lipsync line selected.")
            return {"CANCELLED"}
        lines.remove(index)
        scene.witcher_lipsync_line_index = min(index, len(lines) - 1) if len(lines) else -1
        if len(lines):
            _sync_scene_fields_from_line(scene, lines[scene.witcher_lipsync_line_index])
        else:
            scene.witcher_lipsync_text = ""
            scene.witcher_lipsync_line_id = ""
        scene.witcher_lipsync_last_status = "Removed lipsync line from editor"
        return {"FINISHED"}


class WITCH_OT_move_lipsync_editor_line(bpy.types.Operator):
    """Move the active lipsync line in the editor list."""

    bl_idname = "witcher.move_lipsync_editor_line"
    bl_label = "Move Lipsync Line"
    bl_description = "Move the active lipsync line up or down in the list"
    bl_options = {"REGISTER", "UNDO"}

    direction: EnumProperty(
        items=[
            ("UP", "Up", "Move the active line up"),
            ("DOWN", "Down", "Move the active line down"),
        ],
        default="UP",
    )

    def execute(self, context):
        global _EDITOR_SYNCING

        scene = context.scene
        lines = _editor_lines(scene)
        index = _editor_line_index(scene)
        if lines is None or index < 0 or index >= len(lines):
            self.report({"WARNING"}, "No lipsync line selected.")
            return {"CANCELLED"}

        new_index = index - 1 if self.direction == "UP" else index + 1
        if new_index < 0 or new_index >= len(lines):
            return {"CANCELLED"}

        lines.move(index, new_index)
        was_syncing = _EDITOR_SYNCING
        _EDITOR_SYNCING = True
        try:
            scene.witcher_lipsync_line_index = new_index
        finally:
            _EDITOR_SYNCING = was_syncing
        _sync_scene_fields_from_line(scene, lines[new_index])
        scene.witcher_lipsync_last_status = "Moved lipsync line"
        return {"FINISHED"}


class WITCH_OT_lipsync_use_target_speaker(bpy.types.Operator):
    """Set the active lipsync speaker from the currently loaded target armature."""

    bl_idname = "witcher.lipsync_use_target_speaker"
    bl_label = "Use Target Speaker"
    bl_description = "Set Speaker from the active imported character when it can be recognized"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        speaker = _derive_speaker_from_target(context)
        if not speaker:
            self.report({"WARNING"}, "Could not derive a speaker from the target character.")
            return {"CANCELLED"}

        scene.witcher_lipsync_speaker = speaker
        line = _active_editor_line(scene)
        if line is not None:
            line.speaker = speaker
            _refresh_line_display_name(line)
        scene.witcher_lipsync_last_status = f"Speaker set to {speaker}"
        return {"FINISHED"}


class WITCH_OT_load_active_lipsync_editor_line(bpy.types.Operator):
    """Load the active generated lipsync line onto the target armature."""

    bl_idname = "witcher.load_active_lipsync_editor_line"
    bl_label = "Load Active Lipsync Line"
    bl_description = "Load the active line's generated audio and lipsync animation onto the target character"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        line = _active_editor_line(scene)
        if line is not None:
            _sync_scene_fields_from_line(scene, line)
        try:
            loaded = _load_editor_line_result(context, line, operator=self)
        except Exception as exc:
            message = str(exc).strip() or exc.__class__.__name__
            scene.witcher_lipsync_last_status = "Load failed"
            scene.witcher_lipsync_last_log = message
            self.report({"ERROR"}, message.splitlines()[0][:240])
            log.exception("Active lipsync line load failed")
            return {"CANCELLED"}
        return {"FINISHED"} if loaded else {"CANCELLED"}


class WITCH_OT_add_lipsync_line_from_voice_browser(bpy.types.Operator):
    """Add the selected Dialog Browser entry to the lipsync editor."""

    bl_idname = "witcher.add_lipsync_line_from_voice_browser"
    bl_label = "Add Dialog Browser Line"
    bl_description = "Add the selected Dialog Browser line to the lipsync editor"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        voice_items = getattr(scene, "witcher_voice_list", None)
        try:
            index = int(getattr(scene, "witcher_voice_list_index", -1))
        except Exception:
            index = -1
        if voice_items is None or index < 0 or index >= len(voice_items):
            self.report({"WARNING"}, "Select a line in the Dialog Browser first.")
            return {"CANCELLED"}

        voice_item = voice_items[index]
        line_id = str(getattr(voice_item, "voiceLineId", "") or getattr(voice_item, "line_id", "") or "").strip()
        if not line_id:
            self.report({"WARNING"}, "The selected Dialog Browser line has no line ID.")
            return {"CANCELLED"}

        line = _upsert_editor_line(
            context,
            line_id=line_id,
            text=getattr(voice_item, "text", ""),
            speaker=getattr(voice_item, "speaker", ""),
            language=dialog_language.get_active_voice_language(context),
        )
        _sync_scene_fields_from_line(scene, line)
        scene.witcher_lipsync_last_status = "Added Dialog Browser line"
        return {"FINISHED"}


class WITCH_OT_load_redkit_project_lipsync_lines(bpy.types.Operator):
    """Load voiced lines from the active REDkit project strings CSV."""

    bl_idname = "witcher.load_redkit_project_lipsync_lines"
    bl_label = "Load Project Strings"
    bl_description = "Load all voiced REDkit project strings into the lipsync line list"
    bl_options = {"REGISTER", "UNDO"}

    clear_existing: BoolProperty(
        name="Replace Current List",
        default=True,
        description="Clear the current lipsync line list before loading project strings",
    )
    include_unvoiced: BoolProperty(
        name="Include Unvoiced",
        default=False,
        description="Also include string rows without a VOICEOVER value",
    )

    def execute(self, context):
        scene = context.scene
        project_path = _active_project_path(context)
        if not project_path:
            self.report({"WARNING"}, "No REDkit project selected.")
            return {"CANCELLED"}

        try:
            project_lines = redkit_project.read_project_voice_lines(
                project_path,
                language=getattr(scene, "witcher_lipsync_language", "en"),
                include_unvoiced=self.include_unvoiced,
            )
        except Exception as exc:
            message = str(exc).strip() or exc.__class__.__name__
            scene.witcher_lipsync_project_status = "Project load failed"
            scene.witcher_lipsync_project_log = message
            self.report({"ERROR"}, message.splitlines()[0][:240])
            log.exception("Failed to load REDkit project lipsync lines")
            return {"CANCELLED"}

        lines = _editor_lines(scene)
        if lines is None:
            self.report({"ERROR"}, "Lipsync line list is unavailable.")
            return {"CANCELLED"}
        if self.clear_existing:
            while len(lines):
                lines.remove(0)

        for project_line in project_lines:
            line = lines.add()
            _apply_project_voice_line(line, project_line)

        if len(lines):
            _set_active_editor_line(scene, lines[0])
        else:
            scene.witcher_lipsync_line_index = -1

        wav_count = sum(1 for item in project_lines if item.assets.has_wav)
        wem_count = sum(1 for item in project_lines if item.assets.has_wem)
        re_count = sum(1 for item in project_lines if item.assets.has_re)
        scene.witcher_lipsync_project_status = (
            f"{len(project_lines)} strings; WAV {wav_count}, WEM {wem_count}, .re {re_count}"
        )
        scene.witcher_lipsync_project_log = f"Loaded from {project_path}"
        scene.witcher_lipsync_last_status = "Loaded REDkit project strings"
        self.report({"INFO"}, scene.witcher_lipsync_project_status)
        return {"FINISHED"}


class WITCH_OT_refresh_redkit_project_lipsync_assets(bpy.types.Operator):
    """Refresh project speech asset status for loaded project lines."""

    bl_idname = "witcher.refresh_redkit_project_lipsync_assets"
    bl_label = "Refresh Project Assets"
    bl_description = "Refresh WAV, WEM, and .re status for loaded REDkit project strings"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        lines = _editor_lines(scene)
        if lines is None or not len(lines):
            self.report({"WARNING"}, "No lipsync lines loaded.")
            return {"CANCELLED"}

        refreshed = 0
        wav_count = 0
        wem_count = 0
        re_count = 0
        for line in lines:
            if _refresh_project_asset_status_for_line(line):
                refreshed += 1
                wav_count += 1 if line.has_project_wav else 0
                wem_count += 1 if line.has_project_wem else 0
                re_count += 1 if line.has_project_re else 0

        scene.witcher_lipsync_project_status = (
            f"{refreshed} project lines; WAV {wav_count}, WEM {wem_count}, .re {re_count}"
        )
        scene.witcher_lipsync_last_status = "Refreshed REDkit project asset status"
        self.report({"INFO"}, scene.witcher_lipsync_project_status)
        return {"FINISHED"}


class WITCH_OT_select_lipsync_project_lines(bpy.types.Operator):
    """Select or clear lipsync lines for batch project operations."""

    bl_idname = "witcher.select_lipsync_project_lines"
    bl_label = "Select Lipsync Lines"
    bl_description = "Select visible filtered lipsync lines or clear the batch selection"
    bl_options = {"REGISTER", "UNDO"}

    action: EnumProperty(
        name="Action",
        items=[
            ("FILTERED", "Select Filtered", "Select rows currently matching the filters"),
            ("CLEAR", "Clear", "Clear all selected rows"),
        ],
        default="FILTERED",
    )

    def execute(self, context):
        scene = context.scene
        lines = _editor_lines(scene)
        if lines is None:
            return {"CANCELLED"}

        if self.action == "CLEAR":
            for line in lines:
                line.selected = False
            scene.witcher_lipsync_project_status = "Cleared batch selection"
            return {"FINISHED"}

        selected = 0
        for line in lines:
            if _line_matches_lipsync_filters(scene, line):
                line.selected = True
                selected += 1
        scene.witcher_lipsync_project_status = f"Selected {selected} filtered lines"
        return {"FINISHED"}


class WITCH_OT_validate_redkit_project_lipsync_lines(bpy.types.Operator):
    """Validate active REDkit project string IDs and voiceover names."""

    bl_idname = "witcher.validate_redkit_project_lipsync_lines"
    bl_label = "Validate Project Strings"
    bl_description = "Check the active REDkit project string CSV for duplicate IDs and voiceovers"
    bl_options = {"REGISTER"}

    def execute(self, context):
        scene = context.scene
        project_path = _active_project_path(context)
        if not project_path:
            self.report({"WARNING"}, "No REDkit project selected.")
            return {"CANCELLED"}

        try:
            result = redkit_project.validate_project_voice_lines(project_path)
        except Exception as exc:
            message = str(exc).strip() or exc.__class__.__name__
            scene.witcher_lipsync_project_status = "Project validation failed"
            scene.witcher_lipsync_project_log = message
            self.report({"ERROR"}, message.splitlines()[0][:240])
            return {"CANCELLED"}

        details = []
        details.extend(result.duplicate_ids)
        details.extend(result.duplicate_voiceovers)
        details.extend(result.invalid_ids)
        if result.empty_voiceover_lines:
            details.append("Rows without VOICEOVER:")
            details.extend(result.empty_voiceover_lines)
        scene.witcher_lipsync_project_status = result.compact_message()
        scene.witcher_lipsync_project_log = "\n".join(details)

        if not result.is_valid:
            self.report({"ERROR"}, result.compact_message())
            return {"CANCELLED"}
        self.report({"INFO"}, result.compact_message())
        return {"FINISHED"}


class WITCH_OT_generate_missing_project_lipsync_lines(bpy.types.Operator):
    """Generate missing project speech assets for selected or filtered rows."""

    bl_idname = "witcher.generate_missing_project_lipsync_lines"
    bl_label = "Generate Missing Project Assets"
    bl_description = "Generate missing REDkit project lipsync/audio assets for selected or filtered project rows"
    bl_options = {"REGISTER", "UNDO"}

    target: EnumProperty(
        name="Rows",
        items=[
            ("SELECTED", "Selected", "Use checked rows"),
            ("FILTERED", "Filtered", "Use rows currently matching the filters"),
        ],
        default="SELECTED",
    )
    missing_wem: BoolProperty(
        name="Missing WEM",
        default=True,
        description="Process rows whose project WEM audio is missing",
    )
    missing_re: BoolProperty(
        name="Missing .re",
        default=True,
        description="Process rows whose project .re lipsync animation is missing",
    )
    allow_text_only: BoolProperty(
        name="Allow Text Only",
        default=False,
        description="For rows without WAV, generate text-only lipsync with silent placeholder audio",
    )
    overwrite_existing: BoolProperty(
        name="Overwrite Existing .re/WEM",
        default=False,
        description="Allow batch generation to replace existing project .re or WEM files",
    )

    def invoke(self, context, event):
        self.target = "SELECTED" if _selected_lipsync_lines(context.scene) else "FILTERED"
        return context.window_manager.invoke_props_dialog(self, width=560)

    def draw(self, context):
        col = self.layout.column(align=True)
        col.label(text="Batch Project Generation", icon='PLAY')
        col.prop(self, "target")
        row = col.row(align=True)
        row.prop(self, "missing_wem")
        row.prop(self, "missing_re")
        col.prop(self, "allow_text_only")
        col.prop(self, "overwrite_existing")
        if self.missing_wem:
            col.label(text="WEM generation can rebuild existing .re files.", icon='INFO')

    def _candidate_lines(self, scene):
        if self.target == "SELECTED":
            return _selected_lipsync_lines(scene)
        return _filtered_lipsync_lines(scene)

    def execute(self, context):
        scene = context.scene
        if not bool(getattr(scene, "witcher_lipsync_generate_re", True)):
            self.report({"ERROR"}, "Enable Generate .re before batch project generation.")
            return {"CANCELLED"}

        active_line = _active_editor_line(scene)
        try:
            active_pointer = active_line.as_pointer() if active_line is not None else None
        except Exception:
            active_pointer = None

        candidates = []
        attach_failures = []
        for line in self._candidate_lines(scene):
            if _line_has_project(line):
                candidates.append(line)
                continue
            try:
                line_pointer = line.as_pointer()
            except Exception:
                line_pointer = None
            is_active = active_line is not None and (line == active_line or (active_pointer is not None and line_pointer == active_pointer))
            if bool(getattr(line, "selected", False)) or is_active:
                try:
                    _add_line_to_active_project(context, line, sync_from_scene=is_active)
                    candidates.append(line)
                except Exception as exc:
                    attach_failures.append(f"{getattr(line, 'line_id', '')}: {exc}")

        if not candidates:
            if attach_failures:
                scene.witcher_lipsync_project_status = f"Generated 0; skipped 0; failed {len(attach_failures)}"
                scene.witcher_lipsync_project_log = "\n".join(attach_failures)
                self.report({"ERROR"}, scene.witcher_lipsync_project_status)
                return {"CANCELLED"}
            self.report({"WARNING"}, "No project lines match the batch target.")
            return {"CANCELLED"}

        projects = {str(getattr(line, "project_path", "") or "") for line in candidates}
        try:
            for project_path in projects:
                validation = redkit_project.validate_project_voice_lines(project_path)
                if not validation.is_valid:
                    raise RuntimeError(f"{Path(project_path).name}: {validation.compact_message()}")
        except Exception as exc:
            message = str(exc).strip() or exc.__class__.__name__
            scene.witcher_lipsync_project_status = "Project validation failed"
            scene.witcher_lipsync_project_log = message
            self.report({"ERROR"}, message.splitlines()[0][:240])
            return {"CANCELLED"}

        old_output_path = str(getattr(scene, "witcher_lipsync_redkit_output_path", "") or "")
        generated = 0
        skipped = 0
        failures = list(attach_failures)
        try:
            scene.witcher_lipsync_redkit_output_path = ""
            for line in candidates:
                _refresh_project_asset_status_for_line(line)
                needs_generation = (
                    (self.missing_wem and not bool(getattr(line, "has_project_wem", False)))
                    or (self.missing_re and not bool(getattr(line, "has_project_re", False)))
                )
                if not needs_generation:
                    skipped += 1
                    continue

                overwrite_paths = _project_generation_overwrite_paths(line)
                if overwrite_paths and not self.overwrite_existing:
                    skipped += 1
                    names = ", ".join(path.name for path in overwrite_paths)
                    failures.append(
                        f"{getattr(line, 'line_id', '')}: existing {names}; enable Overwrite Existing .re/WEM"
                    )
                    continue

                wav_path = str(getattr(line, "project_wav_path", "") or getattr(line, "wav_path", "") or "").strip()
                wav_exists = bool(wav_path and Path(bpy.path.abspath(wav_path)).is_file())
                placeholder_wav = bool(getattr(line, "project_wav_is_silent", False) and wav_exists)
                text_only = placeholder_wav or not wav_exists
                if text_only and not (self.allow_text_only or placeholder_wav):
                    skipped += 1
                    failures.append(f"{getattr(line, 'line_id', '')}: missing WAV")
                    continue

                moved_outputs = []
                try:
                    if overwrite_paths:
                        moved_outputs = _stash_project_generation_outputs(line, overwrite_paths)
                    _sync_scene_fields_from_line(scene, line)
                    _run_lipsync_generation(
                        context,
                        self,
                        wav_path=wav_path if wav_exists and not placeholder_wav else None,
                        text_only=text_only,
                        force_transcribe=False,
                        editor_line=line,
                        apply_to_scene=False,
                        report_result=False,
                        require_wem=bool(self.missing_wem),
                    )
                    _restore_missing_project_generation_outputs(moved_outputs)
                    _refresh_project_asset_status_for_line(line)
                    missing_outputs = _project_generation_missing_outputs(
                        line,
                        require_wem=bool(self.missing_wem),
                        require_re=bool(self.missing_re),
                    )
                    if missing_outputs:
                        log_tail = str(getattr(scene, "witcher_lipsync_last_log", "") or "").strip()
                        detail = "; ".join(missing_outputs)
                        if log_tail:
                            detail = f"{detail}; converter log: {_short_ui_text(log_tail, 600)}"
                        raise RuntimeError(detail)
                    generated += 1
                except Exception as exc:
                    _restore_project_generation_outputs(moved_outputs)
                    failures.append(f"{getattr(line, 'line_id', '')}: {exc}")
        finally:
            scene.witcher_lipsync_redkit_output_path = old_output_path

        scene.witcher_lipsync_project_status = (
            f"Generated {generated}; skipped {skipped}; failed {len(failures)}"
        )
        scene.witcher_lipsync_project_log = "\n".join(failures)
        scene.witcher_lipsync_last_status = "Batch project generation finished"
        if failures and not generated:
            self.report({"ERROR"}, scene.witcher_lipsync_project_status)
            return {"CANCELLED"}
        self.report({"INFO"}, scene.witcher_lipsync_project_status)
        return {"FINISHED"}


class WITCH_OT_lipsync_project_line_info(bpy.types.Operator):
    """Show copyable REDkit project line details."""

    bl_idname = "witcher.lipsync_project_line_info"
    bl_label = "Project Line Info"
    bl_description = "Show copyable REDkit project string and speech asset paths"

    project_path: StringProperty(default="", subtype="DIR_PATH", options={'SKIP_SAVE'})
    csv_path: StringProperty(default="", subtype="FILE_PATH", options={'SKIP_SAVE'})
    resource: StringProperty(default="", options={'SKIP_SAVE'})
    line_id: StringProperty(default="", options={'SKIP_SAVE'})
    original_line_id: StringProperty(default="", options={'SKIP_SAVE'})
    voiceover: StringProperty(default="", options={'SKIP_SAVE'})
    wav_path: StringProperty(default="", subtype="FILE_PATH", options={'SKIP_SAVE'})
    wem_path: StringProperty(default="", subtype="FILE_PATH", options={'SKIP_SAVE'})
    re_path: StringProperty(default="", subtype="FILE_PATH", options={'SKIP_SAVE'})
    status_text: StringProperty(default="", options={'SKIP_SAVE'})

    def invoke(self, context, event):
        line = _active_editor_line(context.scene)
        if line is None:
            self.report({"WARNING"}, "No lipsync line selected.")
            return {"CANCELLED"}
        self.project_path = str(getattr(line, "project_path", "") or "")
        self.csv_path = str(getattr(line, "project_csv_path", "") or "")
        self.resource = str(getattr(line, "resource", "") or "")
        self.line_id = str(getattr(line, "line_id", "") or "")
        self.original_line_id = str(getattr(line, "original_line_id", "") or "")
        self.voiceover = str(getattr(line, "voiceover", "") or "")
        self.wav_path = str(getattr(line, "project_wav_path", "") or getattr(line, "wav_path", "") or "")
        self.wem_path = str(getattr(line, "project_wem_path", "") or "")
        self.re_path = str(getattr(line, "project_re_path", "") or getattr(line, "last_re", "") or "")
        self.status_text = _line_asset_status(line)
        return context.window_manager.invoke_props_dialog(self, width=780)

    def draw(self, context):
        col = self.layout.column(align=True)
        col.label(text="REDkit Project Line", icon='INFO')
        col.prop(self, "project_path", text="Project")
        col.prop(self, "csv_path", text="Strings CSV")
        col.prop(self, "resource", text="Resource")
        col.prop(self, "line_id", text="Line ID")
        if self.original_line_id and self.original_line_id != self.line_id:
            col.prop(self, "original_line_id", text="Original ID")
        col.prop(self, "voiceover", text="Voiceover")
        col.prop(self, "status_text", text="Assets")
        if self.wav_path:
            col.prop(self, "wav_path", text="WAV")
        if self.wem_path:
            col.prop(self, "wem_path", text="WEM")
        if self.re_path:
            col.prop(self, "re_path", text=".re")

    def execute(self, context):
        return {"FINISHED"}


class WITCH_OT_lipsync_project_status_info(bpy.types.Operator):
    """Show copyable REDkit project batch status and log."""

    bl_idname = "witcher.lipsync_project_status_info"
    bl_label = "Project Lipsync Status"
    bl_description = "Show copyable REDkit project lipsync status and batch log"

    status_text: StringProperty(default="", options={'SKIP_SAVE'})
    log_text: StringProperty(default="", options={'SKIP_SAVE'})

    def invoke(self, context, event):
        scene = context.scene
        self.status_text = str(getattr(scene, "witcher_lipsync_project_status", "") or "")
        self.log_text = str(getattr(scene, "witcher_lipsync_project_log", "") or "")
        return context.window_manager.invoke_props_dialog(self, width=780)

    def draw(self, context):
        col = self.layout.column(align=True)
        col.label(text="REDkit Project Lipsync", icon='INFO')
        col.prop(self, "status_text", text="Status")
        if self.log_text:
            col.prop(self, "log_text", text="Log")

    def execute(self, context):
        return {"FINISHED"}


class WITCH_OT_add_lipsync_line_to_redkit_project(bpy.types.Operator):
    """Append the active editable line to the selected REDkit project CSV."""

    bl_idname = "witcher.add_lipsync_line_to_redkit_project"
    bl_label = "Add Line To Project"
    bl_description = "Add the active lipsync line to the selected REDkit project's string CSV"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        line = _active_editor_line(scene)
        try:
            _add_line_to_active_project(context, line, sync_from_scene=True)
        except Exception as exc:
            message = str(exc).strip() or exc.__class__.__name__
            scene.witcher_lipsync_project_status = "Project add failed"
            scene.witcher_lipsync_project_log = message
            self.report({"ERROR"}, message.splitlines()[0][:240])
            log.exception("Failed to add lipsync line to REDkit project")
            return {"CANCELLED"}

        _refresh_project_asset_status_for_line(line)
        scene.witcher_lipsync_last_status = "Added lipsync line to REDkit project"
        self.report({"INFO"}, scene.witcher_lipsync_project_status)
        return {"FINISHED"}


class WITCH_OT_apply_redkit_project_lipsync_line(bpy.types.Operator):
    """Write active line edits back to the REDkit project CSV and scenes."""

    bl_idname = "witcher.apply_redkit_project_lipsync_line"
    bl_label = "Apply Line To Project"
    bl_description = "Update the project string CSV and optionally update scene IDs through WolvenKit"
    bl_options = {"REGISTER", "UNDO"}

    update_scenes: BoolProperty(
        name="Update .w2scene Files",
        default=True,
        description="Use WolvenKit CLI to update matching LocalizedString, voice, audio, and lipsync IDs in project scenes",
    )
    rename_assets: BoolProperty(
        name="Rename Speech Assets",
        default=True,
        description="Rename matching project speech audio and .re files when the line ID changes",
    )
    project_path: StringProperty(default="", subtype="DIR_PATH", options={'SKIP_SAVE'})
    old_line_id: StringProperty(default="", options={'SKIP_SAVE'})
    new_line_id: StringProperty(default="", options={'SKIP_SAVE'})
    old_voiceover: StringProperty(default="", options={'SKIP_SAVE'})
    new_voiceover: StringProperty(default="", options={'SKIP_SAVE'})

    def _read_active_values(self, context):
        scene = context.scene
        line = _active_editor_line(scene)
        if line is None:
            raise RuntimeError("No lipsync line selected.")
        _sync_line_from_scene(scene, line)
        project_path = str(getattr(line, "project_path", "") or _active_project_path(context) or "").strip()
        if not project_path:
            raise RuntimeError("The selected line is not linked to a REDkit project.")

        old_id = str(getattr(line, "original_line_id", "") or getattr(line, "line_id", "") or "").strip()
        new_id = radish_runner.normalize_line_id(getattr(line, "line_id", ""))
        if not new_id:
            raise RuntimeError(f"Line ID must be {radish_runner.MAX_RADISH_LINE_ID} or lower.")
        speaker = radish_runner.normalize_speaker(getattr(line, "speaker", "GRLT"))
        old_voiceover = str(getattr(line, "voiceover", "") or redkit_project.voiceover_name(speaker, old_id) or "").strip()
        new_voiceover = redkit_project.voiceover_name(speaker, new_id)
        return line, Path(project_path), old_id, new_id, speaker, old_voiceover, new_voiceover

    def invoke(self, context, event):
        try:
            _line, project_path, old_id, new_id, speaker, old_voiceover, new_voiceover = self._read_active_values(context)
        except Exception as exc:
            self.report({"ERROR"}, str(exc).splitlines()[0][:240])
            return {"CANCELLED"}
        self.project_path = str(project_path)
        self.old_line_id = old_id
        self.new_line_id = new_id
        self.old_voiceover = old_voiceover
        self.new_voiceover = new_voiceover
        self.update_scenes = old_id != new_id or old_voiceover != new_voiceover
        self.rename_assets = self.update_scenes
        return context.window_manager.invoke_props_dialog(self, width=760)

    def draw(self, context):
        col = self.layout.column(align=True)
        col.label(text="Apply REDkit Project Edits", icon='FILE_TICK')
        col.prop(self, "project_path", text="Project")
        col.prop(self, "old_line_id", text="Old ID")
        col.prop(self, "new_line_id", text="New ID")
        col.prop(self, "old_voiceover", text="Old Voice")
        col.prop(self, "new_voiceover", text="New Voice")
        col.prop(self, "rename_assets")
        col.prop(self, "update_scenes")

    def execute(self, context):
        scene = context.scene
        try:
            line, project_path, old_id, new_id, speaker, old_voiceover, new_voiceover = self._read_active_values(context)
            needs_scene_update = bool(self.update_scenes and (old_id != new_id or old_voiceover != new_voiceover))
            prefs = _addon_preferences(context)
            wolvenkit_path = str(getattr(prefs, "wolvenkit", "") or "").strip()
            if wolvenkit_path:
                wolvenkit_path = bpy.path.abspath(wolvenkit_path)
            if needs_scene_update and not Path(wolvenkit_path).is_file():
                raise RuntimeError("Set WolvenKit CLI in Add-on Preferences before updating .w2scene files.")

            result = redkit_project.update_project_line(
                project_path,
                old_id,
                new_id,
                text=getattr(line, "text", ""),
                speaker=speaker,
                language=getattr(line, "language", "en"),
                old_voiceover=old_voiceover,
                new_voiceover=new_voiceover,
                wolvenkit_path=wolvenkit_path,
                update_scenes=needs_scene_update,
                rename_assets=bool(self.rename_assets),
            )
        except Exception as exc:
            message = str(exc).strip() or exc.__class__.__name__
            scene.witcher_lipsync_project_status = "Project update failed"
            scene.witcher_lipsync_project_log = message
            self.report({"ERROR"}, message.splitlines()[0][:240])
            log.exception("Failed to update REDkit project lipsync line")
            return {"CANCELLED"}

        line.original_line_id = new_id
        line.voiceover = new_voiceover
        _refresh_project_asset_status_for_line(line)
        _sync_scene_fields_from_line(scene, line)
        skipped = len(result.skipped_files)
        scene.witcher_lipsync_project_status = (
            f"CSV updated; scenes {result.scenes_changed}/{result.scenes_scanned}; assets {result.assets_renamed}"
        )
        if skipped:
            scene.witcher_lipsync_project_status += f"; skipped {skipped}"
        scene.witcher_lipsync_project_log = "\n".join((
            f"Backup: {result.backup_dir}",
            *result.skipped_files,
        ))
        scene.witcher_lipsync_last_status = "Applied REDkit project line edits"
        self.report({"INFO"}, scene.witcher_lipsync_project_status)
        return {"FINISHED"}


class WITCH_OT_import_wav_to_active_lipsync_line(bpy.types.Operator, ImportHelper):
    """Import or replace WAV audio for the active lipsync editor line."""

    bl_idname = "witcher.import_wav_to_active_lipsync_line"
    bl_label = "Import/Replace WAV"
    bl_description = "Generate lipsync from a WAV and attach it to the active editor line"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".wav"
    filter_glob: StringProperty(default="*.wav", options={"HIDDEN"})

    def execute(self, context):
        scene = context.scene
        line = _active_editor_line(scene)
        if line is None:
            line = _add_editor_line(
                context,
                line_id=_make_new_editor_line_id(context),
                speaker=_default_lipsync_speaker(context),
                language=getattr(scene, "witcher_lipsync_language", "en"),
            )
        if line is None:
            self.report({"ERROR"}, "Could not create or select a lipsync line.")
            return {"CANCELLED"}
        if not str(line.line_id or "").strip():
            line.line_id = _make_new_editor_line_id(context)
        _refresh_line_display_name(line)
        _sync_scene_fields_from_line(scene, line)

        try:
            return _run_lipsync_generation(
                context,
                self,
                wav_path=self.filepath,
                text_only=False,
                force_transcribe=False,
                editor_line=line,
            )
        except Exception as exc:
            message = str(exc).strip() or exc.__class__.__name__
            scene.witcher_lipsync_last_log = message
            scene.witcher_lipsync_last_status = "Failed"
            self.report({"ERROR"}, message.splitlines()[0][:240])
            log.exception("Active lipsync line WAV import failed")
            return {"CANCELLED"}


class WITCH_OT_transcribe_wav_text(bpy.types.Operator, ImportHelper):
    bl_idname = "witcher.transcribe_wav_lipsync_text"
    bl_label = "Transcribe WAV Text"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".wav"
    filter_glob: StringProperty(default="*.wav", options={"HIDDEN"})

    def execute(self, context):
        scene = context.scene
        try:
            text = _transcribe_wav_into_scene(context, self.filepath)
            scene.witcher_lipsync_last_status = "Transcribed WAV text"
            self.report({"INFO"}, f"Transcribed: {text[:120]}")
            return {"FINISHED"}
        except Exception as exc:
            message = str(exc).strip() or exc.__class__.__name__
            scene.witcher_lipsync_last_log = message
            scene.witcher_lipsync_last_status = "Transcription failed"
            self.report({"ERROR"}, message.splitlines()[0][:240])
            log.exception("WAV transcription failed")
            return {"CANCELLED"}


class WITCH_OT_load_selected_lipsync_voiceline(bpy.types.Operator):
    """Add a line from selected Sequencer audio metadata."""

    bl_idname = "witcher.load_selected_lipsync_voiceline"
    bl_label = "Add Line From Selected Sequencer Audio"
    bl_description = "Add or update a lipsync line from the selected dialog audio strip in the Video Sequencer"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        strip = _selected_sound_strip(context, require_dialog=True)
        if strip is None:
            self.report({"WARNING"}, "Select a dialog sound strip in the Sequencer.")
            return {"CANCELLED"}

        text = _strip_dialog_text(strip)
        line_id = _strip_dialog_line_id(strip)
        speaker = str(_strip_get(strip, dialog_language.DIALOG_SUBTITLE_SPEAKER_PROP, "") or "").strip()
        wav_path = str(_strip_get(strip, dialog_language.DIALOG_SUBTITLE_SOURCE_PATH_PROP, "") or "")
        audio_source = str(_strip_get(strip, dialog_language.DIALOG_SUBTITLE_SOURCE_PROP, "") or "").strip()
        if audio_source not in LIPSYNC_STRIP_SOURCES:
            audio_source = "wav_lipsync" if wav_path else ""
        language = str(
            _strip_get(strip, dialog_language.DIALOG_AUDIO_LANGUAGE_PROP, "")
            or _strip_get(strip, dialog_language.DIALOG_SUBTITLE_LANGUAGE_PROP, "")
            or getattr(scene, "witcher_lipsync_language", "en")
            or "en"
        ).strip()

        line = _upsert_editor_line(
            context,
            line_id=line_id,
            text=text,
            speaker=speaker or getattr(scene, "witcher_lipsync_speaker", "GRLT"),
            language=language,
            wav_path=wav_path,
            strip_name=str(getattr(strip, "name", "") or ""),
            audio_source=audio_source,
            duration=_strip_duration_text(context, strip),
        )
        _sync_scene_fields_from_line(scene, line)
        scene.witcher_lipsync_last_status = "Added line from selected sequencer audio"
        scene.witcher_lipsync_last_log = f"Loaded from strip: {getattr(strip, 'name', '')}"
        self.report({"INFO"}, "Added line from selected sequencer audio.")
        return {"FINISHED"}


class WITCH_OT_apply_lipsync_voiceline_to_selected(bpy.types.Operator):
    """Apply lipsync metadata to the selected Sequencer audio."""

    bl_idname = "witcher.apply_lipsync_voiceline_to_selected"
    bl_label = "Apply Line To Selected Sequencer Audio"
    bl_description = "Apply the active lipsync text, speaker, and line ID to the selected audio strip in the Video Sequencer"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        strip = _selected_sound_strip(context, require_dialog=False)
        if strip is None:
            self.report({"WARNING"}, "Select a sound strip in the Sequencer.")
            return {"CANCELLED"}

        raw_line_id = str(getattr(scene, "witcher_lipsync_line_id", "") or "").strip()
        line_id = radish_runner.normalize_line_id(raw_line_id)
        if raw_line_id and not line_id:
            self.report({"ERROR"}, f"Line ID must be {radish_runner.MAX_RADISH_LINE_ID} or lower.")
            return {"CANCELLED"}

        text = str(getattr(scene, "witcher_lipsync_text", "") or "").strip()
        speaker = radish_runner.normalize_speaker(getattr(scene, "witcher_lipsync_speaker", "GRLT"))
        language = str(getattr(scene, "witcher_lipsync_language", "en") or "en").lower()
        _apply_dialog_metadata_to_strip(context, strip, text, line_id, speaker, language)
        audio_source = str(_strip_get(strip, dialog_language.DIALOG_SUBTITLE_SOURCE_PROP, "") or "")
        if audio_source not in LIPSYNC_STRIP_SOURCES:
            audio_source = "wav_lipsync"
        _upsert_editor_line(
            context,
            line_id=line_id,
            text=text,
            speaker=speaker,
            language=language,
            wav_path=str(_strip_get(strip, dialog_language.DIALOG_SUBTITLE_SOURCE_PATH_PROP, "") or ""),
            strip_name=str(getattr(strip, "name", "") or ""),
            audio_source=audio_source,
            duration=_strip_duration_text(context, strip),
        )

        scene.witcher_lipsync_last_status = "Updated selected voiceline"
        scene.witcher_lipsync_last_log = f"Updated strip: {getattr(strip, 'name', '')}"
        self.report({"INFO"}, "Updated selected voiceline.")
        return {"FINISHED"}


class WITCH_OT_download_lipsync_whisper_model(bpy.types.Operator):
    """Download the recommended Whisper model."""

    bl_idname = "witcher.download_lipsync_whisper_model"
    bl_label = "Download Whisper Model"
    bl_description = f"Download {stt.RECOMMENDED_MODEL_FILE} from the whisper.cpp Hugging Face repository"
    bl_options = {"REGISTER"}

    source_url: StringProperty(default="", options={'SKIP_SAVE'})
    target_path: StringProperty(default="", subtype="FILE_PATH", options={'SKIP_SAVE'})
    checksum_sha1: StringProperty(default="", options={'SKIP_SAVE'})
    size_text: StringProperty(default="", options={'SKIP_SAVE'})

    def invoke(self, context, event):
        self.source_url = stt.RECOMMENDED_MODEL_URL
        self.target_path = str(stt.user_model_path(create=False))
        self.checksum_sha1 = stt.RECOMMENDED_MODEL_SHA1
        self.size_text = stt.RECOMMENDED_MODEL_SIZE
        return context.window_manager.invoke_props_dialog(self, width=780)

    def draw(self, context):
        col = self.layout.column(align=True)
        col.label(text="Download Recommended Whisper Model", icon='IMPORT')
        col.prop(self, "target_path", text="Target")
        col.prop(self, "source_url", text="Source")
        col.prop(self, "checksum_sha1", text="SHA1")
        col.prop(self, "size_text", text="Size")

    def execute(self, context):
        scene = context.scene
        wm = context.window_manager

        def progress(downloaded, total):
            if not total:
                return
            try:
                wm.progress_update(int((downloaded / total) * 100))
            except Exception:
                pass

        try:
            try:
                wm.progress_begin(0, 100)
            except Exception:
                pass
            model_path = stt.download_recommended_model(self.target_path, progress_callback=progress)
            try:
                wm.progress_end()
            except Exception:
                pass
            scene.witcher_lipsync_stt_model_path = str(model_path)
            scene.witcher_lipsync_last_status = "Downloaded Whisper model"
            scene.witcher_lipsync_last_log = f"Downloaded {model_path}\nSource: {stt.RECOMMENDED_MODEL_URL}"
            self.report({"INFO"}, f"Downloaded Whisper model: {model_path.name}")
            return {"FINISHED"}
        except Exception as exc:
            try:
                wm.progress_end()
            except Exception:
                pass
            message = str(exc).strip() or exc.__class__.__name__
            scene.witcher_lipsync_last_log = message
            scene.witcher_lipsync_last_status = "Whisper model download failed"
            self.report({"ERROR"}, message.splitlines()[0][:240])
            log.exception("Whisper model download failed")
            return {"CANCELLED"}


class WITCH_OT_lipsync_whisper_info(bpy.types.Operator):
    """Show copyable Whisper setup details."""

    bl_idname = "witcher.lipsync_whisper_info"
    bl_label = "Whisper Model Info"
    bl_description = "Show copyable Whisper model setup and status details"

    current_model_path: StringProperty(default="", options={'SKIP_SAVE'})
    resolved_model_path: StringProperty(default="", options={'SKIP_SAVE'})
    user_model_path: StringProperty(default="", options={'SKIP_SAVE'})
    bundled_model_path: StringProperty(default="", options={'SKIP_SAVE'})
    recommended_model: StringProperty(default=stt.RECOMMENDED_MODEL_FILE, options={'SKIP_SAVE'})
    download_url: StringProperty(default="", options={'SKIP_SAVE'})
    checksum_sha1: StringProperty(default="", options={'SKIP_SAVE'})
    status_text: StringProperty(default="", options={'SKIP_SAVE'})
    detail_text: StringProperty(default="", options={'SKIP_SAVE'})

    def invoke(self, context, event):
        scene = context.scene
        configured_path = str(getattr(scene, "witcher_lipsync_stt_model_path", "") or "").strip()
        self.current_model_path = _scene_stt_model_path(scene) if configured_path else ""
        self.resolved_model_path = ""
        try:
            self.resolved_model_path = str(stt.resolve_model_path(_scene_stt_model_path(scene)))
        except Exception:
            pass
        self.user_model_path = str(stt.user_model_path(create=False))
        self.bundled_model_path = str(stt.bundled_model_path())
        self.recommended_model = stt.RECOMMENDED_MODEL_FILE
        self.download_url = stt.RECOMMENDED_MODEL_URL
        self.checksum_sha1 = stt.RECOMMENDED_MODEL_SHA1

        stt_ok, stt_message = stt.is_available(_scene_stt_model_path(scene))
        self.status_text = "Ready" if stt_ok else "Not ready"
        self.detail_text = stt_message or "Whisper transcription is available."
        return context.window_manager.invoke_props_dialog(self, width=780)

    def draw(self, context):
        col = self.layout.column(align=True)
        col.label(text="Whisper Model", icon='QUESTION')
        col.prop(self, "current_model_path", text="Configured")
        if self.resolved_model_path:
            col.prop(self, "resolved_model_path", text="Active")
        col.prop(self, "user_model_path", text="User Cache")
        col.prop(self, "bundled_model_path", text="Bundled")
        col.prop(self, "recommended_model", text="Recommended")
        col.prop(self, "download_url", text="Download")
        col.prop(self, "checksum_sha1", text="SHA1")
        col.prop(self, "status_text", text="Status")
        col.prop(self, "detail_text", text="Details")

    def execute(self, context):
        return {"FINISHED"}


class WITCH_OT_lipsync_radish_tools_info(bpy.types.Operator):
    """Show copyable Radish Lipsync 4 REDkit setup details."""

    bl_idname = "witcher.lipsync_radish_tools_info"
    bl_label = "Radish Lipsync 4 REDkit Info"
    bl_description = "Show copyable Radish Lipsync 4 REDkit setup and status details"

    configured_path: StringProperty(default="", options={'SKIP_SAVE'})
    resolved_path: StringProperty(default="", options={'SKIP_SAVE'})
    mode_text: StringProperty(default="", options={'SKIP_SAVE'})
    status_text: StringProperty(default="", options={'SKIP_SAVE'})
    missing_text: StringProperty(default="", options={'SKIP_SAVE'})

    def invoke(self, context, event):
        scene = context.scene
        self.configured_path = _radish_tools_path(context)

        generate_re = getattr(scene, "witcher_lipsync_generate_re", True)
        tools_dir, missing = radish_runner.get_full_tool_status(
            _radish_tools_path(context),
            include_converter=generate_re,
        )
        self.mode_text = "Full REDkit lipsync" if generate_re else "Lipsync animation only"

        self.resolved_path = str(tools_dir) if tools_dir else ""
        self.status_text = "Ready" if tools_dir else "Missing external tools"
        self.missing_text = ", ".join(missing)
        return context.window_manager.invoke_props_dialog(self, width=780)

    def draw(self, context):
        col = self.layout.column(align=True)
        col.label(text="Radish Lipsync 4 REDkit", icon='QUESTION')
        col.prop(self, "configured_path", text="Configured")
        if self.resolved_path:
            col.prop(self, "resolved_path", text="Resolved")
        col.prop(self, "mode_text", text="Mode")
        col.prop(self, "status_text", text="Status")
        if self.missing_text:
            col.prop(self, "missing_text", text="Missing")

    def execute(self, context):
        return {"FINISHED"}


class WITCH_OT_open_lipsync_work_folder(bpy.types.Operator):
    """Open the lipsync cache or last Radish workspace folder."""

    bl_idname = "witcher.open_lipsync_work_folder"
    bl_label = "Open Lipsync Work Folder"
    bl_description = "Open the addon lipsync cache or the last generated Radish workspace"

    target: EnumProperty(
        name="Folder",
        items=(
            ("LAST", "Last Workspace", "Open the last generated Radish workspace"),
            ("CACHE", "Cache Root", "Open the addon lipsync cache root"),
        ),
        default="LAST",
        options={'SKIP_SAVE'},
    )

    def execute(self, context):
        scene = context.scene
        path = None
        if self.target == "LAST":
            path = _existing_path(getattr(scene, "witcher_lipsync_last_workspace", ""))
        if path is None:
            path = _lipsync_cache_root(create=True, context=context)

        path = Path(path)
        if path.suffix and path.is_file():
            path = path.parent
        path.mkdir(parents=True, exist_ok=True)
        try:
            bpy.ops.wm.path_open(filepath=str(path))
        except Exception as exc:
            self.report({"ERROR"}, f"Could not open folder: {exc}")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Opened {path}")
        return {"FINISHED"}


class WITCH_OT_lipsync_last_result_info(bpy.types.Operator):
    """Show copyable paths and logs from the last lipsync operation."""

    bl_idname = "witcher.lipsync_last_result_info"
    bl_label = "Lipsync Result Info"
    bl_description = "Show copyable paths and logs from the last lipsync operation"

    status_text: StringProperty(default="", options={'SKIP_SAVE'})
    output_path: StringProperty(default="", options={'SKIP_SAVE'})
    re_path: StringProperty(default="", options={'SKIP_SAVE'})
    workspace_path: StringProperty(default="", options={'SKIP_SAVE'})
    cache_path: StringProperty(default="", options={'SKIP_SAVE'})
    transcript_text: StringProperty(default="", options={'SKIP_SAVE'})
    log_text: StringProperty(default="", options={'SKIP_SAVE'})

    def invoke(self, context, event):
        scene = context.scene
        self.status_text = str(getattr(scene, "witcher_lipsync_last_status", "") or "")
        self.output_path = str(getattr(scene, "witcher_lipsync_last_output", "") or "")
        self.re_path = str(getattr(scene, "witcher_lipsync_last_re", "") or "")
        self.workspace_path = str(getattr(scene, "witcher_lipsync_last_workspace", "") or "")
        self.cache_path = str(_lipsync_cache_root(create=True, context=context))
        self.transcript_text = str(getattr(scene, "witcher_lipsync_last_transcript", "") or "")
        self.log_text = str(getattr(scene, "witcher_lipsync_last_log", "") or "")
        return context.window_manager.invoke_props_dialog(self, width=780)

    def draw(self, context):
        col = self.layout.column(align=True)
        col.label(text="Last Lipsync Result", icon='INFO')
        col.prop(self, "status_text", text="Status")
        if self.output_path:
            col.prop(self, "output_path", text="Output")
        if self.re_path:
            col.prop(self, "re_path", text=".re")
        if self.workspace_path:
            col.prop(self, "workspace_path", text="Workspace")
        if self.cache_path:
            col.prop(self, "cache_path", text="Cache Root")
        if self.transcript_text:
            col.prop(self, "transcript_text", text="Transcript")
        if self.log_text:
            col.prop(self, "log_text", text="Log")

    def execute(self, context):
        return {"FINISHED"}


def _section_header(layout, scene, expand_prop, title, icon='NONE', boxed=True):
    """Draw a collapsible section header with the title as the click target."""
    expanded = bool(getattr(scene, expand_prop, True))
    container = layout.box() if boxed else layout
    header = container.row(align=True)
    header.prop(
        scene,
        expand_prop,
        icon='TRIA_DOWN' if expanded else 'TRIA_RIGHT',
        text=title,
        emboss=False,
    )
    if icon != 'NONE':
        header.label(text="", icon=icon)
    body = container.column(align=True) if expanded else None
    return body, header


def _draw_setup_banner(layout, scene, context):
    """Show only setup blockers that affect the main WAV workflow."""
    generate_re = bool(getattr(scene, "witcher_lipsync_generate_re", True))
    tools_dir, _missing = radish_runner.get_full_tool_status(
        _radish_tools_path(context), include_converter=generate_re,
    )
    stt_ok, stt_message = stt.is_available(_scene_stt_model_path(scene))
    if tools_dir and stt_ok:
        return

    box = layout.box()
    box.label(text="Setup", icon='ERROR')
    if not tools_dir:
        row = box.row(align=True)
        row.alert = True
        row.label(text="Radish Lipsync 4 REDkit missing", icon='ERROR')
        row.operator("witcher.open_addon_preferences", text="", icon='PREFERENCES')
        row.operator(WITCH_OT_lipsync_radish_tools_info.bl_idname, text="", icon='QUESTION')
    if not stt_ok:
        stt_label, stt_icon = _short_stt_status(stt_ok, stt_message)
        row = box.row(align=True)
        row.alert = True
        row.label(text=stt_label, icon=stt_icon)
        if not _has_whisper_model_file(scene):
            row.operator(WITCH_OT_download_lipsync_whisper_model.bl_idname, text="Download", icon='IMPORT')
        row.operator(WITCH_OT_lipsync_whisper_info.bl_idname, text="", icon='QUESTION')


def _short_stt_status(stt_ok, stt_message):
    if stt_ok:
        return "Whisper ready", 'CHECKMARK'

    message = str(stt_message or "")
    if "No Whisper model configured" in message:
        return "Model not configured", 'ERROR'
    if "Whisper model file does not exist" in message:
        return "Model file missing", 'ERROR'
    if "pywhispercpp" in message or "No module named" in message:
        return "Whisper package unavailable", 'ERROR'
    return "Whisper not ready", 'ERROR'


def _has_whisper_model_file(scene):
    model_path = _scene_stt_model_path(scene)
    if model_path:
        return Path(model_path).is_file()
    return stt.user_model_path(create=False).is_file() or stt.bundled_model_path().is_file()


def _short_ui_text(value, limit=96):
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[:max(0, limit - 3)].rstrip() + "..."


def draw_panel(layout, context):
    scene = context.scene
    _sync_project_selector_from_preferences(scene, context)

    _draw_setup_banner(layout, scene, context)

    lines = _editor_lines(scene)
    total_lines = len(lines) if lines is not None else 0
    filtered_lines = _filtered_lipsync_lines(scene)
    filtered_count = len(filtered_lines)
    active_line = _active_editor_line(scene)
    active_index = _editor_line_index(scene)

    lines_box = layout.box()
    lines_header = lines_box.row(align=True)
    lines_header.label(text="Voicelines", icon='SOUND')
    if total_lines:
        count_text = f"{filtered_count}/{total_lines}" if filtered_count != total_lines else str(total_lines)
        lines_header.label(text=count_text, icon='FILTER' if filtered_count != total_lines else 'NONE')

    project_row = lines_box.row(align=True)
    project_row.prop(scene, LIPSYNC_REDKIT_PROJECT_PROP, text="Project")
    project_row.operator("witcher.open_addon_preferences", text="", icon='PREFERENCES')

    load_row = lines_box.row(align=True)
    load_row.operator(WITCH_OT_load_redkit_project_lipsync_lines.bl_idname, text="Load Lines", icon='IMPORT')
    load_row.operator(WITCH_OT_refresh_redkit_project_lipsync_assets.bl_idname, text="Refresh", icon='FILE_REFRESH')

    project_status = str(getattr(scene, "witcher_lipsync_project_status", "") or "").strip()
    if project_status:
        status_row = lines_box.row(align=True)
        status_row.label(text=_short_ui_text(project_status, 72), icon='INFO')
        status_row.operator(WITCH_OT_lipsync_project_status_info.bl_idname, text="", icon='QUESTION')

    search_row = lines_box.row(align=True)
    search_row.prop(scene, "witcher_lipsync_filter_text", text="Search")
    search_row.prop(scene, "witcher_lipsync_show_details", text="Show ID/duration")

    filter_body, _filter_header = _section_header(
        lines_box, scene, "witcher_lipsync_show_filters", "More Filters", 'FILTER', boxed=False,
    )
    if filter_body is not None:
        f_row = filter_body.row(align=True)
        f_row.prop(scene, "witcher_lipsync_filter_speaker", text="Speaker")
        f_row.prop(scene, "witcher_lipsync_filter_resource", text="Scene")
        m_row = filter_body.row(align=True)
        m_row.prop(scene, "witcher_lipsync_filter_project_only", text="Project", toggle=True)
        m_row.prop(scene, "witcher_lipsync_filter_missing_wav", text="No WAV", toggle=True)
        m_row.prop(scene, "witcher_lipsync_filter_missing_wem", text="No WEM", toggle=True)
        m_row.prop(scene, "witcher_lipsync_filter_missing_re", text="No .re", toggle=True)

    lines_row = lines_box.row()
    lines_row.template_list(
        "WITCHER_UL_lipsync_lines", "",
        scene, LIPSYNC_LINES_PROP,
        scene, LIPSYNC_LINE_INDEX_PROP,
        rows=5,
    )
    line_buttons = lines_row.column(align=True)
    line_buttons.operator(WITCH_OT_new_lipsync_editor_line.bl_idname, text="", icon='ADD')
    remove_row = line_buttons.row(align=True)
    remove_row.enabled = total_lines > 0
    remove_row.operator(WITCH_OT_remove_lipsync_editor_line.bl_idname, text="", icon='REMOVE')
    line_buttons.menu(WITCH_MT_lipsync_line_list_actions.bl_idname, text="", icon='DOWNARROW_HLT')
    line_buttons.separator()
    up_row = line_buttons.row(align=True)
    up_row.enabled = active_index > 0
    op = up_row.operator(WITCH_OT_move_lipsync_editor_line.bl_idname, text="", icon='TRIA_UP')
    op.direction = "UP"
    down_row = line_buttons.row(align=True)
    down_row.enabled = 0 <= active_index < total_lines - 1
    op = down_row.operator(WITCH_OT_move_lipsync_editor_line.bl_idname, text="", icon='TRIA_DOWN')
    op.direction = "DOWN"

    batch_body, _batch_header = _section_header(
        lines_box, scene, "witcher_lipsync_show_batch", "Batch Project Tools", 'FILE_TICK', boxed=False,
    )
    if batch_body is not None:
        select_row = batch_body.row(align=True)
        op = select_row.operator(
            WITCH_OT_select_lipsync_project_lines.bl_idname,
            text="Select Filtered", icon='CHECKBOX_HLT',
        )
        op.action = "FILTERED"
        op = select_row.operator(
            WITCH_OT_select_lipsync_project_lines.bl_idname,
            text="Clear", icon='CHECKBOX_DEHLT',
        )
        op.action = "CLEAR"
        run_row = batch_body.row(align=True)
        run_row.operator(WITCH_OT_validate_redkit_project_lipsync_lines.bl_idname, text="Validate", icon='CHECKMARK')
        run_row.operator(WITCH_OT_generate_missing_project_lipsync_lines.bl_idname, text="Generate Missing", icon='PLAY')

    strip_body, strip_header = _section_header(
        lines_box, scene, "witcher_lipsync_show_strip_tools",
        "Sequencer Strip Tools", 'SEQUENCE', boxed=False,
    )
    selected_strip = _selected_sound_strip(context, require_dialog=False)
    if selected_strip is not None:
        badge = strip_header.row()
        badge.alignment = 'RIGHT'
        badge.label(
            text="dialog" if _is_dialog_sound_strip(selected_strip) else "sound",
            icon='CHECKMARK' if _is_dialog_sound_strip(selected_strip) else 'SOUND',
        )
    if strip_body is not None:
        if selected_strip is None:
            strip_body.label(text="No sound strip selected.", icon='INFO')
        strip_body.operator(
            WITCH_OT_load_selected_lipsync_voiceline.bl_idname,
            text="Add Line From Selected Sequencer Audio", icon='IMPORT',
        )
        strip_body.operator(
            WITCH_OT_apply_lipsync_voiceline_to_selected.bl_idname,
            text="Apply Line To Selected Sequencer Audio", icon='FILE_TICK',
        )

    edit_box = layout.box()
    edit_header = edit_box.row(align=True)
    edit_header.label(text="Selected Line", icon='REC')
    line_status_icon, line_status_tooltip = _line_status_icon_tooltip(active_line)
    op = edit_header.operator(
        WITCH_OT_lipsync_line_status_hint.bl_idname,
        text="", icon=line_status_icon, emboss=False,
    )
    op.tooltip = line_status_tooltip
    audio_icon, audio_tooltip = _line_audio_icon_tooltip(active_line)
    if audio_icon:
        op = edit_header.operator(
            WITCH_OT_lipsync_line_status_hint.bl_idname,
            text="", icon=audio_icon, emboss=False,
        )
        op.tooltip = audio_tooltip

    edit_box.prop(scene, "witcher_lipsync_text", text="Text")

    speaker_row = edit_box.row(align=True)
    speaker_row.prop(scene, "witcher_lipsync_speaker", text="Speaker")
    speaker_row.operator(WITCH_OT_lipsync_use_target_speaker.bl_idname, text="", icon='ARMATURE_DATA')

    id_row = edit_box.row(align=True)
    id_row.prop(scene, "witcher_lipsync_line_id", text="Line ID")
    id_row.prop(scene, "witcher_lipsync_language", text="Lang")

    if active_line is not None and str(getattr(active_line, "project_path", "") or "").strip():
        project_actions = edit_box.row(align=True)
        project_actions.operator(
            WITCH_OT_apply_redkit_project_lipsync_line.bl_idname,
            text="Save To Project", icon='FILE_TICK',
        )
        project_actions.operator(WITCH_OT_lipsync_project_line_info.bl_idname, text="", icon='QUESTION')
    elif active_line is not None and _active_project_path(context):
        edit_box.operator(
            WITCH_OT_add_lipsync_line_to_redkit_project.bl_idname,
            text="Add To Project", icon='FILE_TICK',
        )

    edit_box.separator()
    primary = edit_box.column(align=True)
    primary.scale_y = 1.35
    primary.operator(
        WITCH_OT_import_wav_to_active_lipsync_line.bl_idname,
        text="Import WAV + Generate", icon='SOUND',
    )
    secondary = edit_box.row(align=True)
    secondary.operator(WITCH_OT_load_active_lipsync_editor_line.bl_idname, text="Load Result", icon='PLAY')
    secondary.operator(WITCH_OT_generate_text_lipsync.bl_idname, text="Text Only", icon='TEXT')

    settings_body, _settings_header = _section_header(
        edit_box, scene, "witcher_lipsync_show_settings", "Settings", 'PREFERENCES', boxed=False,
    )
    if settings_body is not None:
        settings_body.prop(scene, "witcher_lipsync_generate_re", text="Generate .re")
        if getattr(scene, "witcher_lipsync_generate_re", True):
            settings_body.prop(scene, "witcher_lipsync_redkit_output_path", text=".re Output")
            try:
                tools_hint = _radish_tools_path(context)
                wwise_console, _missing = radish_runner.get_wwise_status(
                    _wwise_console_path(context),
                    tools_dir=tools_hint,
                )
            except Exception:
                wwise_console = None
            wwise_row = settings_body.row(align=True)
            wwise_row.alert = not bool(wwise_console)
            if wwise_console:
                wwise_row.label(text=f"Wwise: {Path(wwise_console).parent.name}", icon='CHECKMARK')
            else:
                wwise_row.label(text="Wwise missing; WEM generation will fail", icon='ERROR')
            wwise_row.operator("witcher.open_addon_preferences", text="", icon='PREFERENCES')

        option_row = settings_body.row(align=True)
        option_row.prop(scene, "witcher_lipsync_load_on_select", text="Auto Load")
        option_row.prop(scene, "witcher_lipsync_replace_audio", text="Replace Audio")
        if hasattr(scene, "witcher_anim_nla_mode"):
            settings_body.prop(scene, "witcher_anim_nla_mode", text="NLA Mode")

        stt_ok, stt_message = stt.is_available(_scene_stt_model_path(scene))
        stt_label, stt_icon = _short_stt_status(stt_ok, stt_message)
        stt_row = settings_body.row(align=True)
        stt_row.alert = not stt_ok
        stt_row.label(text=stt_label, icon=stt_icon)
        if not _has_whisper_model_file(scene):
            stt_row.operator(WITCH_OT_download_lipsync_whisper_model.bl_idname, text="Download", icon='IMPORT')
        stt_row.operator(WITCH_OT_lipsync_whisper_info.bl_idname, text="", icon='QUESTION')
        settings_body.prop(scene, "witcher_lipsync_auto_transcribe", text="Auto Transcribe")
        settings_body.prop(scene, "witcher_lipsync_stt_model_path", text="Whisper Model")
        transcribe_row = settings_body.row(align=True)
        transcribe_row.prop(scene, "witcher_lipsync_stt_threads", text="Threads")
        transcribe_row.operator(WITCH_OT_transcribe_wav_text.bl_idname, text="Transcribe", icon='FILE_SOUND')

        output_row = settings_body.row(align=True)
        output_row.operator("witcher.export_re_mimic", text="Export .re", icon='EXPORT')
        op = output_row.operator(WITCH_OT_open_lipsync_work_folder.bl_idname, text="Open Cache", icon='FILE_FOLDER')
        op.target = "CACHE"

    status = str(getattr(scene, "witcher_lipsync_last_status", "") or "").strip()
    last_output = str(getattr(scene, "witcher_lipsync_last_output", "") or "").strip()
    last_re = str(getattr(scene, "witcher_lipsync_last_re", "") or "").strip()
    if status or last_output or last_re:
        result_box = layout.box()
        result_row = result_box.row(align=True)
        result_row.label(text=_short_ui_text(status or "Generated output available", 72), icon='INFO')
        result_row.operator(
            WITCH_OT_lipsync_last_result_info.bl_idname,
            text="", icon='QUESTION',
        )
        op = result_row.operator(
            WITCH_OT_open_lipsync_work_folder.bl_idname,
            text="", icon='FILE_FOLDER',
        )
        op.target = "LAST"


classes = (
    WitcherLipsyncLineItem,
    WITCH_OT_lipsync_line_status_hint,
    WITCHER_UL_lipsync_lines,
    WITCH_OT_new_lipsync_editor_line,
    WITCH_OT_remove_lipsync_editor_line,
    WITCH_OT_move_lipsync_editor_line,
    WITCH_OT_lipsync_use_target_speaker,
    WITCH_OT_load_active_lipsync_editor_line,
    WITCH_OT_add_lipsync_line_from_voice_browser,
    WITCH_OT_load_redkit_project_lipsync_lines,
    WITCH_OT_refresh_redkit_project_lipsync_assets,
    WITCH_OT_select_lipsync_project_lines,
    WITCH_OT_validate_redkit_project_lipsync_lines,
    WITCH_OT_generate_missing_project_lipsync_lines,
    WITCH_OT_lipsync_project_line_info,
    WITCH_OT_lipsync_project_status_info,
    WITCH_OT_add_lipsync_line_to_redkit_project,
    WITCH_OT_apply_redkit_project_lipsync_line,
    WITCH_OT_import_wav_to_active_lipsync_line,
    WITCH_OT_load_selected_lipsync_voiceline,
    WITCH_OT_apply_lipsync_voiceline_to_selected,
    WITCH_MT_lipsync_line_list_actions,
    WITCH_OT_download_lipsync_whisper_model,
    WITCH_OT_lipsync_whisper_info,
    WITCH_OT_lipsync_radish_tools_info,
    WITCH_OT_open_lipsync_work_folder,
    WITCH_OT_lipsync_last_result_info,
    WITCH_OT_generate_text_lipsync,
    WITCH_OT_transcribe_wav_text,
    WITCH_OT_import_wav_lipsync,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.Scene.witcher_lipsync_show_filters = BoolProperty(
        name="Show Filters",
        default=False,
        description="Expand the project line filters",
    )
    bpy.types.Scene.witcher_lipsync_show_settings = BoolProperty(
        name="Show Lipsync Settings",
        default=False,
        description="Expand generation, transcription, and output settings",
    )
    bpy.types.Scene.witcher_lipsync_show_batch = BoolProperty(
        name="Show Batch Tools",
        default=False,
        description="Expand project validation and batch generation tools",
    )
    bpy.types.Scene.witcher_lipsync_show_strip_tools = BoolProperty(
        name="Show Strip Tools",
        default=False,
        description="Expand the Sequencer strip helpers",
    )
    bpy.types.Scene.witcher_lipsync_redkit_project = EnumProperty(
        name="REDkit Project",
        items=_redkit_project_enum_items,
        description="Active REDkit project used for project string and speech asset workflows",
        update=_on_redkit_project_update,
    )
    bpy.types.Scene.witcher_lipsync_project_status = StringProperty(name="REDkit Project Status", default="")
    bpy.types.Scene.witcher_lipsync_project_log = StringProperty(name="REDkit Project Log", default="")
    bpy.types.Scene.witcher_lipsync_filter_text = StringProperty(
        name="Lipsync Search",
        default="",
        description="Filter lipsync lines by ID, text, voiceover, scene/resource, or key",
    )
    bpy.types.Scene.witcher_lipsync_filter_speaker = StringProperty(
        name="Speaker Filter",
        default="",
        description="Filter lipsync lines by speaker",
    )
    bpy.types.Scene.witcher_lipsync_filter_resource = StringProperty(
        name="Scene Filter",
        default="",
        description="Filter lipsync lines by REDkit scene/resource path",
    )
    bpy.types.Scene.witcher_lipsync_filter_project_only = BoolProperty(
        name="Project Lines",
        default=False,
        description="Show only REDkit project-backed lipsync lines",
    )
    bpy.types.Scene.witcher_lipsync_filter_missing_wav = BoolProperty(
        name="Missing WAV",
        default=False,
        description="Show only project lines without source WAV audio",
    )
    bpy.types.Scene.witcher_lipsync_filter_missing_wem = BoolProperty(
        name="Missing WEM",
        default=False,
        description="Show only project lines without project WEM audio",
    )
    bpy.types.Scene.witcher_lipsync_filter_missing_re = BoolProperty(
        name="Missing .re",
        default=False,
        description="Show only project lines without project .re lipsync animation",
    )
    bpy.types.Scene.witcher_lipsync_show_details = BoolProperty(
        name="Show IDs/duration",
        default=True,
        description="Show line IDs and duration in the lipsync voiceline list",
    )
    bpy.types.Scene.witcher_lipsync_lines = CollectionProperty(type=WitcherLipsyncLineItem)
    bpy.types.Scene.witcher_lipsync_line_index = IntProperty(default=-1, update=_on_lipsync_line_index_update)
    bpy.types.Scene.witcher_lipsync_load_on_select = BoolProperty(
        name="Load on Select",
        default=True,
        description="Load generated audio and lipsync animation when a lipsync line is selected",
    )
    bpy.types.Scene.witcher_lipsync_replace_audio = BoolProperty(
        name="Replace Audio",
        default=False,
        description="Remove existing sound strips before importing generated lipsync audio",
    )
    bpy.types.Scene.witcher_lipsync_text = StringProperty(
        name="Voiceline",
        default="",
        description="Subtitle text used by the generated lipsync line",
        update=_on_lipsync_line_field_update,
    )
    bpy.types.Scene.witcher_lipsync_speaker = StringProperty(
        name="Speaker",
        default="GRLT",
        description="Witcher actor code used for generated line metadata",
        update=_on_lipsync_line_field_update,
    )
    bpy.types.Scene.witcher_lipsync_language = EnumProperty(
        name="Language",
        items=[
            ("en", "English", "Generate English phoneme timings"),
        ],
        default="en",
        update=_on_lipsync_line_field_update,
    )
    bpy.types.Scene.witcher_lipsync_line_id = StringProperty(
        name="Line ID",
        default="",
        description="Optional numeric line ID; generated automatically when empty",
        update=_on_lipsync_line_field_update,
    )
    bpy.types.Scene.witcher_lipsync_auto_transcribe = BoolProperty(
        name="Auto Transcribe WAV",
        default=False,
        description="When no voiceline text is set, transcribe imported WAV audio before generating lipsync",
    )
    bpy.types.Scene.witcher_lipsync_stt_model_path = StringProperty(
        name="Whisper Model",
        default="",
        subtype="FILE_PATH",
        description=f"Path to a ggml Whisper model file; recommended CPU model is {stt.RECOMMENDED_MODEL_FILE}",
    )
    bpy.types.Scene.witcher_lipsync_stt_threads = IntProperty(
        name="STT Threads",
        default=0,
        min=0,
        max=64,
        description="Whisper transcription thread count; 0 uses pywhispercpp default",
    )
    bpy.types.Scene.witcher_lipsync_generate_re = BoolProperty(
        name="Generate .re",
        default=True,
        description="Run Radish converter after generating morph curves and store the REDkit .re output",
    )
    bpy.types.Scene.witcher_lipsync_redkit_output_path = StringProperty(
        name=".re Output",
        default="",
        subtype="DIR_PATH",
        description="Optional converter output folder; empty stores generated REDkit files in the lipsync job cache",
    )
    bpy.types.Scene.witcher_lipsync_last_status = StringProperty(name="Last Lipsync Status", default="")
    bpy.types.Scene.witcher_lipsync_last_output = StringProperty(name="Last Lipsync Output", default="")
    bpy.types.Scene.witcher_lipsync_last_re = StringProperty(name="Last Lipsync .re", default="")
    bpy.types.Scene.witcher_lipsync_last_workspace = StringProperty(name="Last Lipsync Workspace", default="")
    bpy.types.Scene.witcher_lipsync_last_log = StringProperty(name="Last Lipsync Log", default="")
    bpy.types.Scene.witcher_lipsync_last_transcript = StringProperty(name="Last Lipsync Transcript", default="")


def unregister():
    for prop_name in (
        "witcher_lipsync_show_strip_tools",
        "witcher_lipsync_show_batch",
        "witcher_lipsync_show_settings",
        "witcher_lipsync_show_transcribe",
        "witcher_lipsync_show_filters",
        "witcher_lipsync_show_project",
        "witcher_lipsync_show_active",
        "witcher_lipsync_project_log",
        "witcher_lipsync_project_status",
        "witcher_lipsync_redkit_project",
        "witcher_lipsync_filter_missing_re",
        "witcher_lipsync_filter_missing_wem",
        "witcher_lipsync_filter_missing_wav",
        "witcher_lipsync_filter_project_only",
        "witcher_lipsync_filter_resource",
        "witcher_lipsync_filter_speaker",
        "witcher_lipsync_filter_text",
        "witcher_lipsync_show_details",
        "witcher_lipsync_last_log",
        "witcher_lipsync_last_workspace",
        "witcher_lipsync_last_re",
        "witcher_lipsync_last_transcript",
        "witcher_lipsync_last_output",
        "witcher_lipsync_last_status",
        "witcher_lipsync_redkit_output_path",
        "witcher_lipsync_generate_re",
        "witcher_lipsync_stt_threads",
        "witcher_lipsync_stt_model_path",
        "witcher_lipsync_auto_transcribe",
        "witcher_lipsync_replace_audio",
        "witcher_lipsync_load_on_select",
        "witcher_lipsync_line_index",
        "witcher_lipsync_lines",
        "witcher_lipsync_line_id",
        "witcher_lipsync_language",
        "witcher_lipsync_speaker",
        "witcher_lipsync_text",
    ):
        if hasattr(bpy.types.Scene, prop_name):
            delattr(bpy.types.Scene, prop_name)

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
