import csv
import logging
import math
import re
from pathlib import Path

import bpy
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, FloatProperty, IntProperty, StringProperty
from bpy_extras.io_utils import ImportHelper

from .. import dialog_language
from ..extension_paths import get_cache_root
from . import apply as lipsync_apply
from . import phoneme_file, radish_runner, redkit_project, stt


log = logging.getLogger(__name__)

LIPSYNC_STRIP_SOURCES = {"wav_lipsync", "text_lipsync"}
LIPSYNC_LINES_PROP = "witcher_lipsync_lines"
LIPSYNC_LINE_INDEX_PROP = "witcher_lipsync_line_index"
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


def _active_editor_line(scene):
    lines = _editor_lines(scene)
    if lines is None:
        return None
    index = int(getattr(scene, LIPSYNC_LINE_INDEX_PROP, -1) or -1)
    return lines[index] if 0 <= index < len(lines) else None


def _line_display_name(line):
    line_id = str(getattr(line, "line_id", "") or "").strip() or "<new>"
    speaker = str(getattr(line, "speaker", "") or "").strip()
    text = str(getattr(line, "text", "") or "").strip()
    wav_name = Path(str(getattr(line, "wav_path", "") or "")).name
    detail = text or wav_name or "Untitled"
    return f"{line_id} [{speaker}] {detail}" if speaker else f"{line_id} {detail}"


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


def _add_editor_line(context, *, line_id="", text="", speaker="", language="", wav_path="", strip_name=""):
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


def _upsert_editor_line(context, *, line_id="", text="", speaker="", language="", wav_path="", strip_name=""):
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


def _update_editor_line_from_result(context, line, job, wav_path, soundstrip):
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
    line.last_status = str(getattr(scene, "witcher_lipsync_last_status", "") or "")
    line.last_output = str(getattr(scene, "witcher_lipsync_last_output", "") or "")
    line.last_re = str(getattr(scene, "witcher_lipsync_last_re", "") or "")
    line.last_workspace = str(getattr(scene, "witcher_lipsync_last_workspace", "") or "")
    _refresh_line_display_name(line)
    _set_active_editor_line(scene, line)


class WitcherLipsyncLineItem(bpy.types.PropertyGroup):
    line_id: StringProperty(name="Line ID", default="")
    text: StringProperty(name="Voiceline", default="")
    speaker: StringProperty(name="Speaker", default="GRLT")
    language: StringProperty(name="Language", default="en")
    wav_path: StringProperty(name="WAV", default="", subtype="FILE_PATH")
    strip_name: StringProperty(name="Strip", default="")
    last_status: StringProperty(name="Status", default="")
    last_output: StringProperty(name="Output", default="")
    last_re: StringProperty(name=".re", default="")
    last_workspace: StringProperty(name="Workspace", default="")


class WITCHER_UL_lipsync_lines(bpy.types.UIList):
    use_filter_show = False
    use_filter_sort_alpha = False

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            line_id = str(item.line_id or "").strip() or "<new>"
            speaker = str(item.speaker or "").strip()
            text = str(item.text or "").strip()
            wav_name = Path(str(item.wav_path or "")).name
            detail = text or wav_name or "Untitled"
            label = f"{line_id} [{speaker}] {detail}" if speaker else f"{line_id} {detail}"
            layout.label(text=label, icon='SOUND' if item.wav_path else 'TEXT')
        else:
            layout.label(text=str(item.name or item.line_id or "Line"))

    def draw_filter(self, context, layout):
        pass

    def filter_items(self, context, data, propname):
        return [], []


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


def _ensure_face_phonemes(context, armature):
    from ..ui import phoneme_helper, ui_voice

    if armature is None:
        raise RuntimeError("No character target armature found.")

    ui_voice._auto_load_face_morphs(context, armature)
    if not ui_voice._armature_has_face_morphs(armature):
        raise RuntimeError("Load Face Morphs on the character before generating lipsync.")

    pose_bone = armature.pose.bones.get("w3_face_poses")
    _phonemes_data, _morphs_data, phoneme_list, _morph_list = phoneme_helper.read_phoneme_weights()
    missing = [phoneme for phoneme in phoneme_list if phoneme not in pose_bone]
    if missing:
        prev_active = context.view_layer.objects.active
        try:
            context.view_layer.objects.active = armature
            armature.select_set(True)
            bpy.ops.witcher.load_face_phonemes()
        finally:
            if prev_active and prev_active.name in bpy.data.objects:
                context.view_layer.objects.active = prev_active

    missing = [phoneme for phoneme in phoneme_list if phoneme not in pose_bone]
    if missing:
        raise RuntimeError("Create Phonemes on the character before generating lipsync.")

    return pose_bone, phoneme_list


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


def _remove_existing_lipsync_strips(strips, line_id):
    line_id = str(line_id or "").strip()
    for strip in list(strips):
        if getattr(strip, "type", None) != "SOUND":
            continue
        source = str(_strip_get(strip, dialog_language.DIALOG_SUBTITLE_SOURCE_PROP, "") or "")
        strip_line_id = str(_strip_get(strip, dialog_language.DIALOG_SUBTITLE_LINE_ID_PROP, "") or "")
        if source in LIPSYNC_STRIP_SOURCES and strip_line_id == line_id:
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
        _remove_existing_lipsync_strips(strips, line_id)

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
    wav_path = _existing_path(getattr(line, "wav_path", ""))
    if wav_path:
        return wav_path, "wav_lipsync"

    workspace = _existing_path(getattr(line, "last_workspace", ""))
    line_id = str(getattr(line, "line_id", "") or "").strip()
    if workspace and workspace.is_dir() and line_id:
        silent_wav = workspace / f"{line_id}_text_only.wav"
        if silent_wav.exists():
            return silent_wav, "text_lipsync"
    return None, "text_lipsync"


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
        if operator is not None:
            operator.report({"WARNING"}, "The active lipsync line has no generated Radish CSV to load.")
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


def _segments_duration_seconds(segments):
    if not segments:
        return 0.1
    return max(segment.end_ms for segment in segments) / 1000.0


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


def _run_lipsync_generation(context, operator, wav_path=None, text_only=False, force_transcribe=False, editor_line=None):
    scene = context.scene
    language = str(getattr(scene, "witcher_lipsync_language", "en") or "en").lower()
    editor_line_id = str(getattr(editor_line, "line_id", "") or "").strip() if editor_line is not None else ""
    line_id = radish_runner.normalize_line_id(editor_line_id) if editor_line_id else ""
    if not line_id:
        line_id = _resolve_line_id(context, wav_path=None if text_only else wav_path)

    text = str(getattr(scene, "witcher_lipsync_text", "") or "").strip()
    speaker_raw = str(getattr(scene, "witcher_lipsync_speaker", "GRLT") or "").strip()
    project_text, project_speaker = _read_project_line_metadata(context, line_id, language)
    if project_text and not text:
        scene.witcher_lipsync_text = project_text
        text = project_text
    if project_speaker and (not speaker_raw or speaker_raw.upper() == "GRLT"):
        scene.witcher_lipsync_speaker = project_speaker
        speaker_raw = project_speaker
    if not project_speaker and (not speaker_raw or speaker_raw.upper() == "GRLT"):
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
    use_creator = bool(getattr(scene, "witcher_lipsync_use_radish_creator", True))
    generate_re = bool(getattr(scene, "witcher_lipsync_generate_re", True))

    armature = _resolve_target_armature(context)
    if use_creator:
        _ensure_face_morphs(context, armature)
    else:
        _pose_bone, phoneme_list = _ensure_face_phonemes(context, armature)

    tools_dir = (
        radish_runner.find_full_tools_dir(_radish_tools_path(context), include_converter=generate_re)
        if use_creator
        else radish_runner.find_tools_dir(_radish_tools_path(context))
    )
    work_root = Path(get_cache_root(create=True)) / "lipsync"
    if text_only:
        job = radish_runner.create_text_job(
            text=text,
            speaker=speaker,
            line_id=line_id,
            language=language,
            work_root=work_root,
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
        )
        result = radish_runner.run_phoneme_extractor(job, tools_dir)

    start_frame = _resolve_start_frame(scene)
    re_file = None
    lipsyncanim_file = None
    soundstrip = None
    if use_creator:
        creator_result = radish_runner.run_lipsync_creator(
            job,
            result.phoneme_file,
            tools_dir,
            text_only=text_only,
        )
        lipsyncanim_file = creator_result.lipsyncanim_file
        if text_only:
            audio_path = radish_runner.write_silent_wav(
                job.workspace / f"{job.line_id}_text_only.wav",
                _csv_duration_seconds(lipsyncanim_file, scene) + 0.25,
            )
            source = "text_lipsync"
        else:
            audio_path = radish_runner.find_job_audio_file(job)
            source = "wav_lipsync"

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

        converter_log = ""
        if generate_re:
            output_path = _scene_redkit_output_path(scene)
            converter_result = radish_runner.run_converter(
                job,
                lipsyncanim_file.parent,
                tools_dir,
                output_dir=output_path or None,
            )
            re_file = converter_result.redkit_lipsync_file
            converter_log = converter_result.compact_log

        combined_log = "\n".join(
            part for part in (result.compact_log, creator_result.compact_log, converter_log) if part
        )
        count_label = f"{stats['morph_count']} morphs"
    else:
        ipa_map = phoneme_file.read_ipa_similarity_map(
            Path(tools_dir) / "data" / f"{job.language}.phoneme.similarity.csv",
            allowed_phonemes=phoneme_list,
        )
        segments = phoneme_file.parse_phoneme_file(
            result.phoneme_file,
            allowed_phonemes=phoneme_list,
            ipa_map=ipa_map,
        )
        if not segments:
            raise RuntimeError(f"No usable phoneme segments were found in {result.phoneme_file}.")

        if text_only:
            audio_path = radish_runner.write_silent_wav(
                job.workspace / f"{job.line_id}_text_only.wav",
                _segments_duration_seconds(segments) + 0.25,
            )
            source = "text_lipsync"
        else:
            audio_path = radish_runner.find_job_audio_file(job)
            source = "wav_lipsync"

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
        stats = lipsync_apply.apply_segments_to_armature(
            context,
            armature,
            segments,
            action_name=f"{job.line_id}_{source}_phonemes",
            start_frame=start_frame,
            strength=getattr(scene, "witcher_lipsync_strength", 1.0),
        )
        combined_log = result.compact_log
        count_label = f"{len(segments)} phonemes"

    if hasattr(scene, "witcher_cutscene_show_dialog_subtitles"):
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
    _update_editor_line_from_result(context, editor_line, job, wav_path if not text_only else "", soundstrip)
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
                force_transcribe=getattr(scene, "witcher_lipsync_auto_transcribe", False),
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
            return _run_lipsync_generation(context, self, text_only=True)
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
        index = int(getattr(scene, LIPSYNC_LINE_INDEX_PROP, -1) or -1)
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
        index = int(getattr(scene, "witcher_voice_list_index", -1) or -1)
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


class WITCH_OT_import_wav_to_active_lipsync_line(bpy.types.Operator, ImportHelper):
    """Import or replace WAV audio for the active lipsync editor line."""

    bl_idname = "witcher.import_wav_to_active_lipsync_line"
    bl_label = "Import/Replace WAV"
    bl_description = "Transcribe the WAV, generate lipsync, and attach it to the active editor line"
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
                force_transcribe=True,
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
    """Load selected sound strip metadata into the lipsync panel."""

    bl_idname = "witcher.load_selected_lipsync_voiceline"
    bl_label = "Load Selected Voiceline"
    bl_description = "Load the selected dialog sound strip text, speaker, and line ID into the lipsync fields"
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
            wav_path=str(_strip_get(strip, dialog_language.DIALOG_SUBTITLE_SOURCE_PATH_PROP, "") or ""),
            strip_name=str(getattr(strip, "name", "") or ""),
        )
        _sync_scene_fields_from_line(scene, line)
        scene.witcher_lipsync_last_status = "Loaded selected voiceline"
        scene.witcher_lipsync_last_log = f"Loaded from strip: {getattr(strip, 'name', '')}"
        self.report({"INFO"}, "Loaded selected voiceline.")
        return {"FINISHED"}


class WITCH_OT_apply_lipsync_voiceline_to_selected(bpy.types.Operator):
    """Apply lipsync panel metadata to the selected sound strip."""

    bl_idname = "witcher.apply_lipsync_voiceline_to_selected"
    bl_label = "Apply Voiceline to Selected"
    bl_description = "Apply the lipsync text, speaker, and line ID to the selected sound strip without regenerating lipsync"
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
        _upsert_editor_line(
            context,
            line_id=line_id,
            text=text,
            speaker=speaker,
            language=language,
            wav_path=str(_strip_get(strip, dialog_language.DIALOG_SUBTITLE_SOURCE_PATH_PROP, "") or ""),
            strip_name=str(getattr(strip, "name", "") or ""),
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
    """Show copyable Radish tool setup details."""

    bl_idname = "witcher.lipsync_radish_tools_info"
    bl_label = "Radish Tools Info"
    bl_description = "Show copyable Radish lipsync tool setup and status details"

    configured_path: StringProperty(default="", options={'SKIP_SAVE'})
    resolved_path: StringProperty(default="", options={'SKIP_SAVE'})
    mode_text: StringProperty(default="", options={'SKIP_SAVE'})
    status_text: StringProperty(default="", options={'SKIP_SAVE'})
    missing_text: StringProperty(default="", options={'SKIP_SAVE'})

    def invoke(self, context, event):
        scene = context.scene
        self.configured_path = _radish_tools_path(context)

        use_creator = getattr(scene, "witcher_lipsync_use_radish_creator", True)
        generate_re = getattr(scene, "witcher_lipsync_generate_re", True)
        if use_creator:
            tools_dir, missing = radish_runner.get_full_tool_status(
                _radish_tools_path(context),
                include_converter=generate_re,
            )
            self.mode_text = "Full Radish creator" if generate_re else "Radish creator"
        else:
            tools_dir, missing = radish_runner.get_tool_status(_radish_tools_path(context))
            self.mode_text = "Phoneme extractor"

        self.resolved_path = str(tools_dir) if tools_dir else ""
        self.status_text = "Ready" if tools_dir else "Missing external tools"
        self.missing_text = ", ".join(missing)
        return context.window_manager.invoke_props_dialog(self, width=780)

    def draw(self, context):
        col = self.layout.column(align=True)
        col.label(text="Radish Tools", icon='QUESTION')
        col.prop(self, "configured_path", text="Configured")
        if self.resolved_path:
            col.prop(self, "resolved_path", text="Resolved")
        col.prop(self, "mode_text", text="Mode")
        col.prop(self, "status_text", text="Status")
        if self.missing_text:
            col.prop(self, "missing_text", text="Missing")

    def execute(self, context):
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
    transcript_text: StringProperty(default="", options={'SKIP_SAVE'})
    log_text: StringProperty(default="", options={'SKIP_SAVE'})

    def invoke(self, context, event):
        scene = context.scene
        self.status_text = str(getattr(scene, "witcher_lipsync_last_status", "") or "")
        self.output_path = str(getattr(scene, "witcher_lipsync_last_output", "") or "")
        self.re_path = str(getattr(scene, "witcher_lipsync_last_re", "") or "")
        self.workspace_path = str(getattr(scene, "witcher_lipsync_last_workspace", "") or "")
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
        if self.transcript_text:
            col.prop(self, "transcript_text", text="Transcript")
        if self.log_text:
            col.prop(self, "log_text", text="Log")

    def execute(self, context):
        return {"FINISHED"}


def _draw_lipsync_section(layout, title, icon='NONE'):
    box = layout.box()
    box.label(text=title, icon=icon)
    return box.column(align=True)


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


def draw_panel(layout, context):
    scene = context.scene
    col = layout.column(align=True)

    lines_box = _draw_lipsync_section(col, "Lipsync Lines", 'SOUND')
    lines_row = lines_box.row()
    lines_row.template_list(
        "WITCHER_UL_lipsync_lines",
        "",
        scene,
        LIPSYNC_LINES_PROP,
        scene,
        LIPSYNC_LINE_INDEX_PROP,
        rows=4,
    )
    line_buttons = lines_row.column(align=True)
    line_buttons.operator(WITCH_OT_new_lipsync_editor_line.bl_idname, text="", icon='ADD')
    line_buttons.operator(WITCH_OT_remove_lipsync_editor_line.bl_idname, text="", icon='REMOVE')
    import_row = lines_box.row(align=True)
    import_row.operator(WITCH_OT_load_selected_lipsync_voiceline.bl_idname, text="From Strip", icon='IMPORT')
    import_row.operator(WITCH_OT_add_lipsync_line_from_voice_browser.bl_idname, text="From Browser", icon='TEXT')
    import_row.operator(WITCH_OT_load_active_lipsync_editor_line.bl_idname, text="Load", icon='PLAY')
    options_row = lines_box.row(align=True)
    options_row.prop(scene, "witcher_lipsync_load_on_select", text="Load on Select", toggle=True)
    options_row.prop(scene, "witcher_lipsync_replace_audio", text="Replace Audio", toggle=True)

    line_box = _draw_lipsync_section(col, "Active Line", 'TEXT')
    line_box.prop(scene, "witcher_lipsync_text", text="Voiceline")

    speaker_row = line_box.row(align=True)
    speaker_row.prop(scene, "witcher_lipsync_speaker", text="Speaker")
    speaker_row.operator(WITCH_OT_lipsync_use_target_speaker.bl_idname, text="Target", icon='ARMATURE_DATA')
    line_box.prop(scene, "witcher_lipsync_language", text="Language")

    row = line_box.row(align=True)
    row.prop(scene, "witcher_lipsync_line_id", text="Line ID")
    row.prop(scene, "witcher_lipsync_strength", text="Strength", slider=True)

    project_id_info = redkit_project.get_active_project_id_info(context)
    if project_id_info:
        line_box.label(
            text=f"REDkit ID space {project_id_info.id_space}; next {project_id_info.next_line_id}",
            icon="FILE_FOLDER",
        )
    else:
        line_box.label(text="No REDkit ID space found; using fallback IDs.", icon="INFO")

    if hasattr(scene, "witcher_anim_nla_mode"):
        line_box.prop(scene, "witcher_anim_nla_mode", text="NLA Load Mode")

    line_box.operator(WITCH_OT_import_wav_to_active_lipsync_line.bl_idname, text="Import/Replace WAV", icon='SOUND')
    line_box.operator(WITCH_OT_generate_text_lipsync.bl_idname, text="Generate Active From Text", icon='TEXT')

    edit_box = _draw_lipsync_section(col, "Selected Strip Metadata", 'SEQUENCE')
    selected_strip = _selected_sound_strip(context, require_dialog=False)
    if selected_strip is not None:
        icon = "CHECKMARK" if _is_dialog_sound_strip(selected_strip) else "SOUND"
        label = "Dialog strip selected" if _is_dialog_sound_strip(selected_strip) else "Sound strip selected"
        edit_box.label(text=label, icon=icon)
    else:
        edit_box.label(text="Select a sound strip in the Sequencer.", icon="INFO")
    edit_box.operator(WITCH_OT_load_selected_lipsync_voiceline.bl_idname, text="Load Selected", icon='IMPORT')
    edit_box.operator(WITCH_OT_apply_lipsync_voiceline_to_selected.bl_idname, text="Apply to Selected", icon='FILE_TICK')

    stt_box = _draw_lipsync_section(col, "Transcribe WAV", 'FILE_SOUND')
    stt_box.prop(scene, "witcher_lipsync_auto_transcribe", text="Auto Transcribe Imported WAV")
    stt_box.prop(scene, "witcher_lipsync_stt_model_path", text="Whisper Model")
    stt_ok, stt_message = stt.is_available(_scene_stt_model_path(scene))
    stt_label, stt_icon = _short_stt_status(stt_ok, stt_message)
    stt_status = stt_box.row(align=True)
    stt_status.alert = not stt_ok
    stt_status.label(text=stt_label, icon=stt_icon)
    if not _has_whisper_model_file(scene):
        stt_status.operator(WITCH_OT_download_lipsync_whisper_model.bl_idname, text="Download", icon='IMPORT')
    stt_status.operator(WITCH_OT_lipsync_whisper_info.bl_idname, text="Setup", icon='QUESTION')
    stt_row = stt_box.row(align=True)
    stt_row.prop(scene, "witcher_lipsync_stt_threads", text="Threads")
    stt_row.operator(WITCH_OT_transcribe_wav_text.bl_idname, text="Transcribe WAV", icon="FILE_SOUND")

    generation_box = _draw_lipsync_section(col, "Lipsync Generation", 'SETTINGS')
    generation_box.prop(scene, "witcher_lipsync_use_radish_creator", text="Use Radish Creator")
    if getattr(scene, "witcher_lipsync_use_radish_creator", True):
        generation_box.prop(scene, "witcher_lipsync_generate_re", text="Generate .re")
        if getattr(scene, "witcher_lipsync_generate_re", True):
            generation_box.prop(scene, "witcher_lipsync_redkit_output_path", text=".re Output")

    if getattr(scene, "witcher_lipsync_use_radish_creator", True):
        tools_dir, missing = radish_runner.get_full_tool_status(
            _radish_tools_path(context),
            include_converter=getattr(scene, "witcher_lipsync_generate_re", True),
        )
    else:
        tools_dir, missing = radish_runner.get_tool_status(_radish_tools_path(context))
    if not tools_dir:
        tools_row = generation_box.row(align=True)
        tools_row.alert = True
        tools_row.label(text="Radish tools missing", icon="ERROR")
        tools_row.operator("witcher.open_addon_preferences", text="", icon='PREFERENCES')
        tools_row.operator(WITCH_OT_lipsync_radish_tools_info.bl_idname, text="Details", icon='QUESTION')
    else:
        tool_label = "Full Radish" if getattr(scene, "witcher_lipsync_use_radish_creator", True) else "Extractor"
        tools_row = generation_box.row(align=True)
        tools_row.label(text=f"{tool_label}: {tools_dir.name}", icon="CHECKMARK")
        tools_row.operator(WITCH_OT_lipsync_radish_tools_info.bl_idname, text="Details", icon='QUESTION')

    output_box = _draw_lipsync_section(col, "Output", 'EXPORT')
    output_box.operator("witcher.export_re_mimic", text="Export Edited .re", icon="EXPORT")

    status = str(getattr(scene, "witcher_lipsync_last_status", "") or "").strip()
    last_output = str(getattr(scene, "witcher_lipsync_last_output", "") or "").strip()
    last_re = str(getattr(scene, "witcher_lipsync_last_re", "") or "").strip()
    if status or last_output or last_re:
        result_box = _draw_lipsync_section(col, "Last Result", 'INFO')
        result_row = result_box.row(align=True)
        result_row.label(text=status or "Generated output available", icon="INFO")
        result_row.operator(WITCH_OT_lipsync_last_result_info.bl_idname, text="Details", icon='QUESTION')
        if last_output:
            result_box.label(text=f"Output: {Path(last_output).name}", icon="FILE")
        if last_re:
            result_box.label(text=f".re: {Path(last_re).name}", icon="FILE_TICK")


classes = (
    WitcherLipsyncLineItem,
    WITCHER_UL_lipsync_lines,
    WITCH_OT_new_lipsync_editor_line,
    WITCH_OT_remove_lipsync_editor_line,
    WITCH_OT_lipsync_use_target_speaker,
    WITCH_OT_load_active_lipsync_editor_line,
    WITCH_OT_add_lipsync_line_from_voice_browser,
    WITCH_OT_import_wav_to_active_lipsync_line,
    WITCH_OT_load_selected_lipsync_voiceline,
    WITCH_OT_apply_lipsync_voiceline_to_selected,
    WITCH_OT_download_lipsync_whisper_model,
    WITCH_OT_lipsync_whisper_info,
    WITCH_OT_lipsync_radish_tools_info,
    WITCH_OT_lipsync_last_result_info,
    WITCH_OT_generate_text_lipsync,
    WITCH_OT_transcribe_wav_text,
    WITCH_OT_import_wav_lipsync,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

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
    bpy.types.Scene.witcher_lipsync_strength = FloatProperty(
        name="Strength",
        default=1.0,
        min=0.0,
        max=2.0,
        soft_min=0.0,
        soft_max=1.5,
        description="Scale generated phoneme property values",
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
    bpy.types.Scene.witcher_lipsync_use_radish_creator = BoolProperty(
        name="Use Radish Creator",
        default=True,
        description="Generate and import Radish .lipsyncanim.csv morph curves instead of approximate phoneme curves",
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
        "witcher_lipsync_last_log",
        "witcher_lipsync_last_workspace",
        "witcher_lipsync_last_re",
        "witcher_lipsync_last_transcript",
        "witcher_lipsync_last_output",
        "witcher_lipsync_last_status",
        "witcher_lipsync_redkit_output_path",
        "witcher_lipsync_generate_re",
        "witcher_lipsync_use_radish_creator",
        "witcher_lipsync_stt_threads",
        "witcher_lipsync_stt_model_path",
        "witcher_lipsync_auto_transcribe",
        "witcher_lipsync_replace_audio",
        "witcher_lipsync_load_on_select",
        "witcher_lipsync_strength",
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
