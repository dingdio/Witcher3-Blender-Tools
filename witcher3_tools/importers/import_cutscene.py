import logging
import os
import time
from collections import Counter
from types import SimpleNamespace
from ..CR2W import w3_types
from ..CR2W.prop_utils import read_enum_prop
from ..importers import import_entity
from ..action_compat import iter_action_fcurves, remove_action_fcurve
from ..CR2W.dc_anims import load_bin_cutscene
from ..CR2W.common_blender import repo_file, redkit_repo_context, win_path_isfile
from ..source_game_paths import normalize_source_game, repo_file_for_source, resolve_w2_repo_file_from_source, w2_source_repo_root
from ..duplication import duplicate_character_hierarchy
from .cutscene_appearance_events import (
    body_part_event_has_body_state,
    body_part_event_request_name,
    is_body_part_event,
    resolve_body_part_event_appearance,
)

log = logging.getLogger(__name__)

CUTSCENE_GUID_PROP = "witcher_cutscene_guid"
CUTSCENE_TRACK_NAME = "cutscene_anim"
CUTSCENE_FACE_TRACK_NAME = f"{CUTSCENE_TRACK_NAME}_face"
CUTSCENE_SOURCE_PATH_PROP = "witcher_cutscene_source_path"
CUTSCENE_SOURCE_INDEX_PROP = "witcher_cutscene_source_index"
CUTSCENE_ANIMATION_NAME_PROP = "witcher_cutscene_animation_name"
CUTSCENE_ACTOR_IMPORTED_PROP = "cutscene_actor_imported"
CUTSCENE_ACTOR_SOURCE_GAME_PROP = "cutscene_actor_source_game"
CUTSCENE_APPEARANCE_DATA_PATH = "witcherui_RigSettings.app_list_index"
FACE_MORPHS_APPEARANCE_PROP = "witcher_face_morphs_loaded_for_appearance"
CUTSCENE_BURNED_AUDIO_PROP = "witcher_cutscene_burned_audio"
CUTSCENE_BURNED_AUDIO_EVENT_PROP = "witcher_cutscene_burned_audio_event"
CUTSCENE_BURNED_AUDIO_ITEM_PATH_PROP = "witcher_cutscene_burned_audio_item_path"
CUTSCENE_BURNED_AUDIO_SOURCE_PATH_PROP = "witcher_cutscene_burned_audio_source_path"
CUTSCENE_BURNED_AUDIO_DEFAULT_VOLUME = 0.35
CUTSCENE_DIALOG_AUDIO_PROP = "witcher_cutscene_dialog_audio"
CUTSCENE_DIALOG_LINE_ID_PROP = "witcher_cutscene_dialog_line_id"
CUTSCENE_DIALOG_TEXT_PROP = "witcher_cutscene_dialog_text"
CUTSCENE_DIALOG_SOUND_EVENT_PROP = "witcher_cutscene_dialog_sound_event"
CUTSCENE_DIALOG_ITEM_PATH_PROP = "witcher_cutscene_dialog_item_path"
CUTSCENE_DIALOG_SOURCE_PATH_PROP = "witcher_cutscene_dialog_source_path"
CUTSCENE_DIALOG_SOURCE_GAME_PROP = "witcher_cutscene_dialog_source_game"

def loadCutsceneFile(filename):
    ext = os.path.splitext(filename)[1]
    if ext.lower().endswith('.w2cutscene'):
        return load_bin_cutscene(filename)
    return None


def _is_w2_cutscene_file(filename):
    """True if the cutscene is a Witcher 2 (CR2W version <=115) container.

    W2 cutscene actor templates are W2 .w2ent files that must be resolved
    against the W2 depot/game roots rather than the W3 uncook path.
    """
    from ..CR2W.dc_w2_havok import is_w2_cr2w_version_file

    try:
        return bool(is_w2_cr2w_version_file(filename))
    except Exception:
        return False


def _resolve_cutscene_actor_template_path(template_path, cutscene_filename, is_w2):
    """Resolve a cutscene actor template to an absolute path.

    For W2 cutscenes the template is a W2 .w2ent; resolving it needs the W2
    repo version (115) and the W2 source context (so the game-data root that
    holds the cutscene is searched). import_ent_template then auto-detects W2
    from the file version and resolves nested dependencies the same way.
    """
    if is_w2:
        resolved = resolve_w2_repo_file_from_source(template_path, cutscene_filename, version=115)
        if resolved:
            return resolved
        source_root = w2_source_repo_root(cutscene_filename)
        if source_root:
            source_candidate = os.path.join(source_root, str(template_path or "").replace("/", "\\").lstrip("\\"))
            if win_path_isfile(source_candidate):
                return source_candidate
        with redkit_repo_context(cutscene_filename):
            return repo_file(template_path, version=115)
    return repo_file(template_path)


def _normalize_actor_replacement_source(source_game):
    source_text = str(source_game or "").strip().upper()
    if source_text == "REDKIT":
        return "REDKIT"
    return normalize_source_game(source_text).upper()


def _coerce_source_index(value, default=-1):
    try:
        return int(value)
    except Exception:
        return int(default)


def _append_unique_existing_root(roots, root):
    root = str(root or "").strip()
    if not root:
        return
    root = os.path.normpath(root)
    if not os.path.isdir(root):
        return
    root_key = os.path.normcase(root)
    if all(os.path.normcase(existing) != root_key for existing in roots):
        roots.append(root)


def _redkit_actor_replacement_roots(context=None):
    roots = []
    try:
        from .. import get_all_addon_prefs

        prefs = get_all_addon_prefs(context or bpy.context)
    except Exception:
        prefs = None
    if prefs is None:
        return roots

    try:
        projects = list(getattr(prefs, "redkit_projects", []) or [])
        active_index = int(getattr(prefs, "redkit_projects_index", -1) or -1)
    except Exception:
        projects = []
        active_index = -1
    ordered_projects = []
    if 0 <= active_index < len(projects):
        ordered_projects.append(projects[active_index])
    ordered_projects.extend(project for idx, project in enumerate(projects) if idx != active_index)
    for project in ordered_projects:
        project_path = str(getattr(project, "path", "") or "").strip()
        if project_path:
            _append_unique_existing_root(roots, os.path.join(bpy.path.abspath(project_path), "workspace"))

    for attr_name in ("redkit_depot_path", "redkit_uncooked_path"):
        try:
            _append_unique_existing_root(roots, bpy.path.abspath(getattr(prefs, attr_name, "") or ""))
        except Exception:
            pass
    return roots


def resolve_cutscene_actor_replacement_template_path(template_path, cutscene_filename="", source_game="W3", context=None):
    template_path = _normalize_repo_path(template_path)
    if not template_path:
        return ""
    if os.path.isabs(template_path):
        return template_path

    source_key = _normalize_actor_replacement_source(source_game)
    if source_key == "REDKIT":
        rel_path = template_path.replace("/", "\\").lstrip("\\")
        for root in _redkit_actor_replacement_roots(context):
            candidate = os.path.normpath(os.path.join(root, rel_path))
            if win_path_isfile(candidate):
                return candidate
        return ""
    if source_key == "W2":
        return _resolve_cutscene_actor_template_path(template_path, cutscene_filename, True)
    return repo_file_for_source(template_path, "w3")


def _split_tag_text(value):
    if isinstance(value, (list, tuple)):
        return [str(item or "").strip() for item in value if str(item or "").strip()]
    return [part.strip() for part in str(value or "").split(";") if part.strip()]


def _cutscene_actor_proxy_from_values(
    actor_name="",
    template_path="",
    actor_type="CAT_Actor",
    appearance_name="",
    tag="",
    voice_tag="",
    final_position="",
    kill_me=False,
    use_mimic=False,
    anim_final_pos="",
):
    return SimpleNamespace(
        name=str(actor_name or "").strip(),
        template=_normalize_repo_path(template_path),
        type=_normalize_cutscene_actor_type(actor_type, actor_name=actor_name),
        appearance=str(appearance_name or "").strip(),
        useMimic=bool(use_mimic),
        tag=_split_tag_text(tag),
        voiceTag=str(voice_tag or "").strip(),
        finalPosition=_split_tag_text(final_position),
        killMe=bool(kill_me),
        animationAtFinalPosition=str(anim_final_pos or "").strip(),
    )


def replace_cutscene_actor_template(
    old_actor_obj,
    template_path,
    *,
    source_game="W3",
    cutscene_filename="",
    actor_name="",
    actor_type="CAT_Actor",
    appearance_name="",
    tag="",
    voice_tag="",
    final_position="",
    kill_me=False,
    use_mimic=False,
    anim_final_pos="",
    source_index=-1,
):
    template_path = _normalize_repo_path(template_path)
    if not template_path:
        raise RuntimeError("Replacement actor template path is empty.")

    source_key = _normalize_actor_replacement_source(source_game)
    resolved_template_path = resolve_cutscene_actor_replacement_template_path(
        template_path,
        cutscene_filename=cutscene_filename,
        source_game=source_key,
        context=bpy.context,
    )
    if not resolved_template_path or not win_path_isfile(resolved_template_path):
        raise RuntimeError(f"Replacement actor template was not found for {source_key}: {template_path}")

    actor_name = str(actor_name or "").strip()
    appearance_name = str(appearance_name or "").strip()
    old_actor_names = _collect_cutscene_actor_removal_names(old_actor_obj)
    new_actor_obj = import_entity.import_ent_template(
        resolved_template_path,
        load_face_poses=True,
        import_apperance=1,
        selected_appearance_name=appearance_name,
        entity_namespace=actor_name,
    )
    if new_actor_obj is None:
        raise RuntimeError(f"Failed to import replacement actor: {resolved_template_path}")

    cutscene_guid = _generate_cutscene_guid()
    _tag_cutscene_object_hierarchy(new_actor_obj, cutscene_guid)
    actor_proxy = _cutscene_actor_proxy_from_values(
        actor_name=actor_name,
        template_path=template_path,
        actor_type=actor_type,
        appearance_name=appearance_name,
        tag=tag,
        voice_tag=voice_tag,
        final_position=final_position,
        kill_me=kill_me,
        use_mimic=use_mimic,
        anim_final_pos=anim_final_pos,
    )
    _tag_cutscene_actor(
        new_actor_obj,
        actor_proxy,
        source_index=source_index,
        source_path=cutscene_filename,
        imported_new=True,
        cutscene_guid=cutscene_guid,
    )
    new_actor_obj[CUTSCENE_ACTOR_SOURCE_GAME_PROP] = source_key.lower()
    new_actor_obj["cutscene_actor_replacement_source_game"] = source_key
    new_actor_obj["cutscene_actor_resolved_template"] = resolved_template_path
    try:
        new_actor_obj["witcher_source_game"] = "w2" if source_key == "W2" else "w3"
    except Exception:
        pass

    _ensure_cutscene_actor_appearance(new_actor_obj, appearance_name)
    _ensure_cutscene_face_setup(new_actor_obj)

    removed_old = 0
    if old_actor_obj is not None and old_actor_obj is not new_actor_obj:
        removed_old = remove_cutscene_actor_hierarchy(old_actor_obj, object_names=old_actor_names)

    return {
        "actor_obj": new_actor_obj,
        "actor_name": actor_name,
        "template_path": template_path,
        "resolved_template_path": resolved_template_path,
        "appearance_name": appearance_name,
        "source_game": source_key.lower(),
        "imported_new": True,
        "cutscene_guid": cutscene_guid,
        "source_index": _coerce_source_index(source_index),
        "removed_old": int(removed_old or 0),
    }

import bpy
from .import_anims import NewW2ANIMSListItem#, set_global_set #!USE NEW METHOD


def _cutscene_progress_begin(window_manager):
    if not window_manager:
        return
    try:
        window_manager.progress_begin(0, 100)
    except Exception:
        pass


def _cutscene_progress_update(window_manager, workspace, percent, message=""):
    clamped = max(0, min(100, int(percent or 0)))
    if window_manager:
        try:
            window_manager.progress_update(clamped)
        except Exception:
            pass
    if workspace:
        try:
            workspace.status_text_set(str(message or ""))
        except Exception:
            pass


def _cutscene_progress_end(window_manager, workspace):
    if window_manager:
        try:
            window_manager.progress_end()
        except Exception:
            pass
    if workspace:
        try:
            workspace.status_text_set(None)
        except Exception:
            pass

def _normalize_repo_path(path):
    return str(path or "").replace("/", "\\").lstrip("\\")


def _normalize_filesystem_path(path):
    text = str(path or "").strip()
    if not text:
        return ""
    try:
        return os.path.normcase(os.path.normpath(text))
    except Exception:
        return text

def split_cutscene_animation_name(anim_name):
    full_name = str(anim_name or "").strip()
    parts = full_name.split(":", 2)
    if len(parts) >= 3:
        actor_name, component_name, display_name = parts
    elif len(parts) == 2:
        actor_name, display_name = parts
        component_name = ""
    else:
        actor_name = ""
        component_name = ""
        display_name = full_name
    return actor_name, component_name, display_name

def _is_face_cutscene_component(component_name):
    return str(component_name or "").strip().lower() == "face"

def _is_face_cutscene_animation(anim_name):
    _actor_name, component_name, _display_name = split_cutscene_animation_name(anim_name)
    if _is_face_cutscene_component(component_name):
        return True
    return ":face" in str(anim_name or "").lower()

def _cutscene_track_name_for_animation(anim_name, base_track=CUTSCENE_TRACK_NAME):
    if _is_face_cutscene_animation(anim_name):
        return CUTSCENE_FACE_TRACK_NAME
    return str(base_track or CUTSCENE_TRACK_NAME)

def _is_cutscene_track_name(track_name, base_track=CUTSCENE_TRACK_NAME):
    track_text = str(track_name or "").strip()
    base_text = str(base_track or CUTSCENE_TRACK_NAME).strip()
    if not track_text or not base_text:
        return False
    return track_text == base_text or track_text.startswith(f"{base_text}_")


def _get_cutscene_burned_audio_property(cutscene_template):
    present_fields = getattr(cutscene_template, "presentPropertyNames", None)
    if present_fields is None:
        has_burned_audio_prop = False
    else:
        has_burned_audio_prop = "burnedAudioTrackName" in set(present_fields)
    burned_audio_name = str(getattr(cutscene_template, "burnedAudioTrackName", "") or "")
    burned_audio_name = burned_audio_name.strip()
    return has_burned_audio_prop, burned_audio_name


def get_cutscene_burned_audio_property(cutscene_template):
    return _get_cutscene_burned_audio_property(cutscene_template)


def _resolve_cutscene_burned_audio_item(cutscene_template, filename="", loadmods=False):
    from ..CR2W.witcher_cache.SoundCache import LoadSoundManager

    def _resolve_paths(soundbanks_info, event_name):
        if soundbanks_info is None or not hasattr(soundbanks_info, "resolve_event_name"):
            return []
        resolved = [str(path or "").replace("/", "\\") for path in (soundbanks_info.resolve_event_name(event_name) or [])]
        return [path for path in resolved if path]

    manager = LoadSoundManager(loadmods=loadmods)
    soundbanks_info = getattr(manager, "soundBanksInfo", None)
    if soundbanks_info is None or not hasattr(soundbanks_info, "resolve_event_name"):
        return None

    has_burned_audio_prop, burned_audio_name = _get_cutscene_burned_audio_property(cutscene_template)
    if not has_burned_audio_prop or not burned_audio_name:
        return None

    resolved_paths = _resolve_paths(soundbanks_info, burned_audio_name)
    if not resolved_paths:
        metadata_path = str(getattr(soundbanks_info, "filename", "") or "").strip()
        if metadata_path and os.path.exists(metadata_path):
            try:
                refreshed_info = soundbanks_info.__class__(metadata_path)
                manager.soundBanksInfo = refreshed_info
                soundbanks_info = refreshed_info
                resolved_paths = _resolve_paths(soundbanks_info, burned_audio_name)
            except Exception:
                log.debug(
                    "Failed to refresh soundbanks metadata before resolving burned audio '%s'.",
                    burned_audio_name,
                    exc_info=True,
                )

    if not resolved_paths:
        log.info(
            "Cutscene burned audio event '%s' did not resolve in soundbanks metadata '%s' for '%s'.",
            burned_audio_name,
            getattr(soundbanks_info, "filename", ""),
            filename,
        )
        return None

    preferred_path = next((path for path in resolved_paths if path.lower().endswith(".wem")), resolved_paths[0])
    return {
        "event_name": burned_audio_name,
        "item_path": preferred_path,
        "candidate_paths": resolved_paths,
    }


def resolve_cutscene_burned_audio_item(cutscene_template, filename="", loadmods=False):
    return _resolve_cutscene_burned_audio_item(cutscene_template, filename=filename, loadmods=loadmods)


def resolve_cutscene_sound_event_item(sound_event_name, loadmods=False):
    from ..CR2W.witcher_cache.SoundCache import LoadSoundManager

    sound_event_name = str(sound_event_name or "").strip()
    if not sound_event_name:
        return None

    manager = LoadSoundManager(loadmods=loadmods)
    soundbanks_info = getattr(manager, "soundBanksInfo", None)
    if soundbanks_info is None or not hasattr(soundbanks_info, "resolve_event_name"):
        return None

    resolved_paths = [
        str(path or "").replace("/", "\\")
        for path in (soundbanks_info.resolve_event_name(sound_event_name) or [])
        if str(path or "").strip()
    ]
    if not resolved_paths:
        metadata_path = str(getattr(soundbanks_info, "filename", "") or "").strip()
        if metadata_path and os.path.exists(metadata_path):
            try:
                refreshed_info = soundbanks_info.__class__(metadata_path)
                manager.soundBanksInfo = refreshed_info
                soundbanks_info = refreshed_info
                resolved_paths = [
                    str(path or "").replace("/", "\\")
                    for path in (soundbanks_info.resolve_event_name(sound_event_name) or [])
                    if str(path or "").strip()
                ]
            except Exception:
                log.debug(
                    "Failed to refresh soundbanks metadata before resolving dialog sound '%s'.",
                    sound_event_name,
                    exc_info=True,
                )

    if not resolved_paths:
        log.info(
            "Dialog sound event '%s' did not resolve in soundbanks metadata '%s'.",
            sound_event_name,
            getattr(soundbanks_info, "filename", ""),
        )
        return None

    preferred_path = next((path for path in resolved_paths if path.lower().endswith(".wem")), resolved_paths[0])
    return {
        "event_name": sound_event_name,
        "item_path": preferred_path,
        "candidate_paths": resolved_paths,
    }


def _iter_cutscene_dialog_audio_strips(scene, source_path="", line_id="", sound_event=""):
    from ..ui.ui_voice import _get_sequence_editor_strips

    strips = _get_sequence_editor_strips(getattr(scene, "sequence_editor", None))
    if strips is None:
        return []

    normalized_source_path = _normalize_filesystem_path(source_path)
    line_id = str(line_id or "").strip()
    sound_event = str(sound_event or "").strip()
    matching = []
    for strip in list(strips):
        if getattr(strip, "type", None) != 'SOUND':
            continue
        try:
            is_dialog_audio = bool(strip.get(CUTSCENE_DIALOG_AUDIO_PROP, False))
        except Exception:
            is_dialog_audio = False
        if not is_dialog_audio:
            continue

        if normalized_source_path:
            strip_source_path = ""
            try:
                strip_source_path = _normalize_filesystem_path(strip.get(CUTSCENE_DIALOG_SOURCE_PATH_PROP, ""))
            except Exception:
                strip_source_path = ""
            if strip_source_path != normalized_source_path:
                continue

        if line_id:
            try:
                strip_line_id = str(strip.get(CUTSCENE_DIALOG_LINE_ID_PROP, "") or "").strip()
            except Exception:
                strip_line_id = ""
            if strip_line_id != line_id:
                continue

        if sound_event:
            try:
                strip_sound_event = str(strip.get(CUTSCENE_DIALOG_SOUND_EVENT_PROP, "") or "").strip()
            except Exception:
                strip_sound_event = ""
            if strip_sound_event != sound_event:
                continue

        matching.append(strip)
    return matching


def remove_cutscene_dialog_audio_strips(scene, source_path="", line_id="", sound_event=""):
    from ..ui.ui_voice import _get_sequence_editor_strips

    strips = _get_sequence_editor_strips(getattr(scene, "sequence_editor", None))
    if strips is None:
        return 0
    strips_to_remove = _iter_cutscene_dialog_audio_strips(
        scene,
        source_path=source_path,
        line_id=line_id,
        sound_event=sound_event,
    )
    for strip in strips_to_remove:
        strips.remove(strip)
    return len(strips_to_remove)


def import_sound_event_to_timeline(context, sound_event_name, frame_start=0.0, source_path="", line_id="",
                                   line_text="", strip_props=None, loadmods=False, volume=None):
    from ..ui.ui_file_browser import ensure_sound_item_extracted, ensure_sound_wav
    from ..ui.ui_voice import _get_next_sound_channel, _get_sequence_editor_strips

    resolved_audio = resolve_cutscene_sound_event_item(sound_event_name, loadmods=loadmods)
    if not resolved_audio:
        return None

    item_path = str(resolved_audio.get("item_path", "") or "").replace("/", "\\").lstrip("\\")
    if not item_path:
        return None

    sound_abs_path = ensure_sound_item_extracted(context, item_path, loadmods=loadmods)
    if not sound_abs_path:
        raise RuntimeError(f"Sound cache item not found: {item_path}")

    wav_path = ensure_sound_wav(context, sound_abs_path, item_path)
    scene = context.scene
    if not scene.sequence_editor:
        scene.sequence_editor_create()
    strips = _get_sequence_editor_strips(scene.sequence_editor)
    if strips is None:
        raise RuntimeError("Blender sequence editor strips API is unavailable")

    line_id = str(line_id or "").strip()
    sound_event_name = str(sound_event_name or "").strip()
    if line_id or sound_event_name:
        remove_cutscene_dialog_audio_strips(
            scene,
            source_path=source_path,
            line_id=line_id,
            sound_event=sound_event_name,
        )

    channel = _get_next_sound_channel(scene)
    strip_name = sound_event_name or os.path.splitext(os.path.basename(item_path))[0] or "cutscene_dialog"
    frame_start_value = float(frame_start or 0.0)
    soundstrip = strips.new_sound(
        strip_name,
        wav_path,
        channel=channel,
        frame_start=int(round(frame_start_value)),
    )
    soundstrip.frame_start = frame_start_value
    if volume is not None:
        try:
            soundstrip.volume = max(0.0, float(volume))
        except Exception:
            pass

    strip_end = int(getattr(soundstrip, "frame_final_end", frame_start_value))
    if strip_end > scene.frame_end:
        scene.frame_end = strip_end

    tag_props = {
        CUTSCENE_DIALOG_AUDIO_PROP: True,
        CUTSCENE_DIALOG_LINE_ID_PROP: line_id,
        CUTSCENE_DIALOG_TEXT_PROP: str(line_text or ""),
        CUTSCENE_DIALOG_SOUND_EVENT_PROP: sound_event_name,
        CUTSCENE_DIALOG_ITEM_PATH_PROP: item_path,
        CUTSCENE_DIALOG_SOURCE_PATH_PROP: str(source_path or ""),
    }
    tag_props.update(strip_props or {})
    for prop_name, prop_value in tag_props.items():
        try:
            soundstrip[prop_name] = prop_value
        except Exception:
            log.debug(
                "Could not tag dialog sound strip %s with %s",
                getattr(soundstrip, "name", ""),
                prop_name,
                exc_info=True,
            )

    return soundstrip


def _iter_cutscene_burned_audio_strips(scene, source_path=""):
    from ..ui.ui_voice import _get_sequence_editor_strips

    strips = _get_sequence_editor_strips(getattr(scene, "sequence_editor", None))
    if strips is None:
        return []

    normalized_source_path = _normalize_filesystem_path(source_path)
    matching = []
    for strip in list(strips):
        if getattr(strip, "type", None) != 'SOUND':
            continue
        try:
            is_burned_audio = bool(strip.get(CUTSCENE_BURNED_AUDIO_PROP, False))
        except Exception:
            is_burned_audio = False
        if not is_burned_audio:
            continue
        strip_source_path = ""
        try:
            strip_source_path = _normalize_filesystem_path(strip.get(CUTSCENE_BURNED_AUDIO_SOURCE_PATH_PROP, ""))
        except Exception:
            strip_source_path = ""
        if normalized_source_path and strip_source_path and strip_source_path != normalized_source_path:
            continue
        matching.append(strip)
    return matching


def find_cutscene_burned_audio_strip(scene, source_path=""):
    strips = _iter_cutscene_burned_audio_strips(scene, source_path=source_path)
    return strips[0] if strips else None


def remove_cutscene_burned_audio_strips(scene, source_path=""):
    from ..ui.ui_voice import _get_sequence_editor_strips

    strips = _get_sequence_editor_strips(getattr(scene, "sequence_editor", None))
    if strips is None:
        return 0

    strips_to_remove = _iter_cutscene_burned_audio_strips(scene, source_path=source_path)
    for strip in strips_to_remove:
        strips.remove(strip)
    return len(strips_to_remove)


def _remove_existing_cutscene_burned_audio_strips(scene, source_path=""):
    return remove_cutscene_burned_audio_strips(scene, source_path=source_path)


def _get_cutscene_burned_audio_default_volume(context):
    scene = getattr(context, "scene", None)
    if scene is None:
        return CUTSCENE_BURNED_AUDIO_DEFAULT_VOLUME
    try:
        return max(
            0.0,
            float(
                getattr(
                    scene,
                    "witcher_cutscene_burned_audio_default_volume",
                    CUTSCENE_BURNED_AUDIO_DEFAULT_VOLUME,
                )
                or 0.0
            ),
        )
    except Exception:
        return CUTSCENE_BURNED_AUDIO_DEFAULT_VOLUME


def _import_cutscene_burned_audio_track(context, cutscene_template, source_path="", volume=None):
    from ..ui.ui_file_browser import ensure_sound_item_extracted, ensure_sound_wav
    from ..ui.ui_voice import _get_next_sound_channel, _get_sequence_editor_strips

    resolved_audio = _resolve_cutscene_burned_audio_item(
        cutscene_template,
        filename=source_path,
        loadmods=False,
    )
    if not resolved_audio:
        return None

    item_path = str(resolved_audio.get("item_path", "") or "").replace("/", "\\").lstrip("\\")
    if not item_path:
        return None

    sound_abs_path = ensure_sound_item_extracted(context, item_path, loadmods=False)
    if not sound_abs_path:
        raise RuntimeError(f"Sound cache item not found: {item_path}")

    wav_path = ensure_sound_wav(context, sound_abs_path, item_path)
    scene = context.scene
    if not scene.sequence_editor:
        scene.sequence_editor_create()
    strips = _get_sequence_editor_strips(scene.sequence_editor)
    if strips is None:
        raise RuntimeError("Blender sequence editor strips API is unavailable")

    removed_count = _remove_existing_cutscene_burned_audio_strips(scene, source_path=source_path)
    channel = _get_next_sound_channel(scene)
    frame_start = 0
    strip_name = (
        str(resolved_audio.get("event_name", "") or "").strip()
        or os.path.splitext(os.path.basename(item_path))[0]
        or "cutscene_burned_audio"
    )

    soundstrip = strips.new_sound(strip_name, wav_path, channel=channel, frame_start=frame_start)
    soundstrip.frame_start = frame_start
    if volume is None:
        volume = _get_cutscene_burned_audio_default_volume(context)
    try:
        soundstrip.volume = max(0.0, float(volume))
    except Exception:
        pass
    strip_end = int(getattr(soundstrip, "frame_final_end", frame_start))
    if strip_end > scene.frame_end:
        scene.frame_end = strip_end

    try:
        soundstrip[CUTSCENE_BURNED_AUDIO_PROP] = True
        soundstrip[CUTSCENE_BURNED_AUDIO_EVENT_PROP] = str(resolved_audio.get("event_name", "") or "")
        soundstrip[CUTSCENE_BURNED_AUDIO_ITEM_PATH_PROP] = item_path
        soundstrip[CUTSCENE_BURNED_AUDIO_SOURCE_PATH_PROP] = str(source_path or "")
    except Exception:
        pass

    return {
        "event_name": str(resolved_audio.get("event_name", "") or ""),
        "item_path": item_path,
        "wav_path": wav_path,
        "strip_name": getattr(soundstrip, "name", strip_name),
        "channel": int(getattr(soundstrip, "channel", channel) or channel),
        "removed_count": int(removed_count),
        "volume": float(volume or 0.0),
    }


def import_cutscene_burned_audio_track(context, filepath="", cutscene_template=None, volume=None):
    source_path = str(filepath or "").strip()
    if cutscene_template is None:
        if not source_path:
            return None
        cutscene_template = loadCutsceneFile(source_path)
    if cutscene_template is None:
        return None
    return _import_cutscene_burned_audio_track(
        context,
        cutscene_template,
        source_path=source_path,
        volume=volume,
    )

def _schedule_cutscene_animation_frame(sequence_state, actor_key, component_name, duration):
    actor_key = str(actor_key or "<unknown>")
    component_name = str(component_name or "").strip()
    duration = max(1, int(duration or 0))

    state = sequence_state.get(actor_key)
    if state is None:
        state = {
            "current_cut_start": 0,
            "next_cut_start": 0,
            "has_timeline_cut": False,
        }
        sequence_state[actor_key] = state

    # Cutscene face clips layer onto the current body/root cut instead of advancing the actor timeline.
    if _is_face_cutscene_component(component_name):
        if state["has_timeline_cut"]:
            return int(state["current_cut_start"])
        return int(state["next_cut_start"])

    at_frame = int(state["next_cut_start"])
    state["current_cut_start"] = at_frame
    state["next_cut_start"] = at_frame + duration
    state["has_timeline_cut"] = True
    return at_frame

def _iter_scene_armatures():
    for obj in bpy.context.scene.objects:
        if obj.type == 'ARMATURE':
            yield obj

def _get_armature_repo_path(obj):
    try:
        rig_settings = getattr(obj.data, "witcherui_RigSettings", None)
    except Exception:
        rig_settings = None
    return _normalize_repo_path(getattr(rig_settings, "repo_path", "") or "")

def _find_cutscene_actor_by_name(actor_name):
    actor_name = str(actor_name or "").strip()
    if not actor_name:
        return None
    for obj in _iter_scene_armatures():
        if str(obj.get("cutscene_actor_name", "") or "").strip() == actor_name:
            return obj
    return None

def _find_actor_by_repo_path(repo_path):
    repo_path = _normalize_repo_path(repo_path)
    if not repo_path:
        return None
    for obj in _iter_scene_armatures():
        if len(obj.name) > 4 and obj.name[-4] == ".":
            continue
        if _get_armature_repo_path(obj) == repo_path:
            return obj
    return None

def find_existing_cutscene_actor(actor_name="", repo_path="", duplicate_count=1):
    actor_obj = _find_cutscene_actor_by_name(actor_name)
    if actor_obj is not None:
        return actor_obj
    if int(duplicate_count or 0) <= 1:
        return _find_actor_by_repo_path(repo_path)
    return None

def check_if_actor_already_in_scene(repo_path):
    return find_existing_cutscene_actor(repo_path=repo_path) or False

def _actor_template_counts(actor_defs):
    return Counter(
        _normalize_repo_path(getattr(actor, "template", "") or "")
        for actor in (actor_defs or [])
        if _normalize_repo_path(getattr(actor, "template", "") or "")
    )

def _tag_cutscene_actor(actor_obj, actor, source_index=-1, source_path="", imported_new=False, cutscene_guid=""):
    if actor_obj is None:
        return
    actor_name = str(getattr(actor, "name", "") or "").strip()
    if actor_name:
        actor_obj["cutscene_actor_name"] = actor_name
    actor_obj["cutscene_actor_template"] = _normalize_repo_path(getattr(actor, "template", "") or "")
    actor_obj["cutscene_actor_type"] = _normalize_cutscene_actor_type(
        _safe_actor_type_str(getattr(actor, "type", None)),
        actor_name=actor_name,
    )
    actor_obj["cutscene_component"] = "Root"
    appearance_name = str(getattr(actor, "appearance", "") or "").strip()
    if appearance_name:
        actor_obj["cutscene_actor_appearance"] = appearance_name
    actor_obj["cutscene_actor_use_mimic"] = bool(getattr(actor, "useMimic", False))

    # Extended actor properties (all fields on SCutsceneActorDef)
    tag_list = getattr(actor, "tag", None)
    actor_obj["cutscene_actor_tag"] = "; ".join(str(t or "").strip() for t in (tag_list or []) if str(t or "").strip())
    voice_tag = str(getattr(actor, "voiceTag", "") or "").strip()
    actor_obj["cutscene_actor_voice_tag"] = voice_tag
    final_pos_list = getattr(actor, "finalPosition", None)
    actor_obj["cutscene_actor_final_position"] = "; ".join(str(t or "").strip() for t in (final_pos_list or []) if str(t or "").strip())
    actor_obj["cutscene_actor_kill_me"] = bool(getattr(actor, "killMe", False) or False)
    actor_obj["cutscene_actor_anim_final_pos"] = str(getattr(actor, "animationAtFinalPosition", "") or "").strip()

    if source_path:
        actor_obj[CUTSCENE_SOURCE_PATH_PROP] = str(source_path)
    source_index_int = _coerce_source_index(source_index)
    if source_index_int >= 0:
        actor_obj[CUTSCENE_SOURCE_INDEX_PROP] = source_index_int
    actor_obj[CUTSCENE_ACTOR_IMPORTED_PROP] = bool(imported_new)
    if cutscene_guid:
        actor_obj[CUTSCENE_GUID_PROP] = str(cutscene_guid)

def _clear_cutscene_actor_tags(actor_obj):
    if actor_obj is None:
        return
    for prop_name in (
        "cutscene_actor_name",
        "cutscene_actor_template",
        "cutscene_actor_type",
        "cutscene_component",
        "cutscene_actor_appearance",
        "cutscene_actor_use_mimic",
        "cutscene_actor_tag",
        "cutscene_actor_voice_tag",
        "cutscene_actor_final_position",
        "cutscene_actor_kill_me",
        "cutscene_actor_anim_final_pos",
        CUTSCENE_ACTOR_SOURCE_GAME_PROP,
        CUTSCENE_SOURCE_PATH_PROP,
        CUTSCENE_SOURCE_INDEX_PROP,
        CUTSCENE_ACTOR_IMPORTED_PROP,
        CUTSCENE_GUID_PROP,
    ):
        try:
            if prop_name in actor_obj:
                del actor_obj[prop_name]
        except Exception:
            pass

def _iter_object_descendants(root_obj):
    if root_obj is None:
        return
    pending = list(getattr(root_obj, "children", []) or [])
    while pending:
        child = pending.pop(0)
        pending.extend(getattr(child, "children", []) or [])
        yield child


def _w2_mimic_armature_for_actor(actor_obj):
    if actor_obj is None or getattr(actor_obj, "type", None) != 'ARMATURE':
        return None

    actor_name = str(getattr(actor_obj, "name", "") or "").strip()
    mimic_name = str(actor_obj.get("witcher_w2_mimic_armature", "") or "").strip()
    if mimic_name:
        mimic_obj = bpy.data.objects.get(mimic_name)
        if mimic_obj is not None and getattr(mimic_obj, "type", None) == 'ARMATURE':
            return mimic_obj

    for obj in _iter_object_descendants(actor_obj):
        if getattr(obj, "type", None) != 'ARMATURE':
            continue
        try:
            if str(obj.get("witcher_w2_mimic_armature", "") or "").strip() == getattr(obj, "name", ""):
                return obj
            if (
                actor_name
                and str(obj.get("witcher_w2_mimic_parent_armature", "") or "").strip() == actor_name
                and bool(obj.get("witcher_w2_mimic_mapping_constraint_count", 0))
            ):
                return obj
        except Exception:
            continue
    return None


def _iter_additional_cutscene_armatures(actor_obj):
    if actor_obj is None:
        return
    mimic_name = str(actor_obj.get("mimicFace", "") or "").strip()
    if mimic_name:
        mimic_obj = bpy.data.objects.get(mimic_name)
        if mimic_obj is not None and getattr(mimic_obj, "type", None) == 'ARMATURE':
            yield mimic_obj
    w2_mimic_obj = _w2_mimic_armature_for_actor(actor_obj)
    if w2_mimic_obj is not None:
        yield w2_mimic_obj

    actor_name = str(actor_obj.get("cutscene_actor_name", "") or "").strip()
    if actor_name:
        for obj in bpy.context.scene.objects:
            if obj is actor_obj or getattr(obj, "type", None) != 'ARMATURE':
                continue
            if str(obj.get("cutscene_actor_name", "") or "").strip() == actor_name:
                yield obj

def _iter_cutscene_related_armatures(actor_obj):
    seen = set()

    def _yield_once(obj):
        if obj is None or getattr(obj, "type", None) != 'ARMATURE':
            return
        obj_name = getattr(obj, "name", "")
        if obj_name in seen:
            return
        seen.add(obj_name)
        yield obj

    if actor_obj and getattr(actor_obj, "type", None) == 'ARMATURE':
        yield from _yield_once(actor_obj)
    for child in _iter_object_descendants(actor_obj):
        yield from _yield_once(child)
    for extra_obj in _iter_additional_cutscene_armatures(actor_obj):
        yield from _yield_once(extra_obj)

def _tag_cutscene_object_hierarchy(actor_obj, guid):
    if actor_obj is None or not guid:
        return
    seen = set()

    def _iter_related_objects(root_obj):
        if root_obj is None:
            return
        yield root_obj
        for obj in _iter_object_descendants(root_obj):
            yield obj

    related_roots = [actor_obj, *list(_iter_additional_cutscene_armatures(actor_obj))]
    for root_obj in related_roots:
        for obj in _iter_related_objects(root_obj):
            obj_name = getattr(obj, "name", None)
            if not obj_name or obj_name not in bpy.data.objects or obj_name in seen:
                continue
            seen.add(obj_name)
            try:
                obj[CUTSCENE_GUID_PROP] = str(guid)
            except Exception:
                pass


def _duplicate_cutscene_actor_from_source(source_actor, actor_name="", repo_path="", appearance_name=""):
    if source_actor is None or getattr(source_actor, "type", None) != 'ARMATURE':
        return None, ""
    try:
        duplicate_actor = duplicate_character_hierarchy(bpy.context, source_actor)
    except Exception:
        log.exception(
            "Failed to duplicate cached cutscene actor '%s'",
            getattr(source_actor, "name", "<unknown>"),
        )
        return None, ""

    if duplicate_actor is None:
        return None, ""

    cutscene_guid = _generate_cutscene_guid()
    _tag_cutscene_object_hierarchy(duplicate_actor, cutscene_guid)
    _clear_cutscene_actor_tags(duplicate_actor)
    try:
        duplicate_actor[CUTSCENE_ACTOR_IMPORTED_PROP] = True
        duplicate_actor[CUTSCENE_GUID_PROP] = str(cutscene_guid)
        if actor_name:
            duplicate_actor["cutscene_actor_name"] = str(actor_name)
        if repo_path:
            duplicate_actor["cutscene_actor_template"] = _normalize_repo_path(repo_path)
        if appearance_name:
            duplicate_actor["cutscene_actor_appearance"] = str(appearance_name)
    except Exception:
        pass
    return duplicate_actor, cutscene_guid

def _generate_cutscene_guid():
    from ..ui.ui_equipment import generate_guid

    return generate_guid()

def clear_cutscene_actor_animation_tracks(actor_obj, track_name=None):
    removed_tracks = 0
    removed_actions = []
    for armature_obj in _iter_cutscene_related_armatures(actor_obj):
        anim_data = getattr(armature_obj, "animation_data", None)
        if not anim_data:
            continue
        for track in list(anim_data.nla_tracks):
            current_track_name = str(getattr(track, "name", "") or "")
            if track_name:
                if current_track_name != track_name:
                    continue
            elif not _is_cutscene_track_name(current_track_name):
                continue
            for strip in track.strips:
                action = getattr(strip, "action", None)
                if action and action.name not in removed_actions:
                    removed_actions.append(action.name)
            anim_data.nla_tracks.remove(track)
            removed_tracks += 1

    for action_name in removed_actions:
        action = bpy.data.actions.get(action_name)
        if action and action.users == 0:
            bpy.data.actions.remove(action)
    if not track_name:
        clear_cutscene_actor_appearance_keys(actor_obj)
    return removed_tracks


def _collect_cutscene_actor_removal_names(actor_obj):
    names = set()
    if actor_obj is None:
        return names

    def _add_obj(obj):
        obj_name = getattr(obj, "name", "")
        if obj_name:
            names.add(obj_name)

    _add_obj(actor_obj)
    for obj in _iter_object_descendants(actor_obj):
        _add_obj(obj)
    for armature_obj in _iter_additional_cutscene_armatures(actor_obj):
        _add_obj(armature_obj)
        for obj in _iter_object_descendants(armature_obj):
            _add_obj(obj)

    guid = str(actor_obj.get(CUTSCENE_GUID_PROP, "") or "").strip()
    if guid:
        for obj in bpy.data.objects:
            try:
                if str(obj.get(CUTSCENE_GUID_PROP, "") or "").strip() == guid:
                    _add_obj(obj)
            except Exception:
                continue
    return names


def remove_cutscene_actor_hierarchy(actor_obj, object_names=None):
    if actor_obj is None and not object_names:
        return 0

    try:
        clear_cutscene_actor_animation_tracks(actor_obj)
    except Exception:
        pass

    names = set(object_names or _collect_cutscene_actor_removal_names(actor_obj))

    def _object_depth(name):
        depth = 0
        obj = bpy.data.objects.get(name)
        while obj is not None and getattr(obj, "parent", None) is not None:
            depth += 1
            obj = obj.parent
        return depth

    removed = 0
    for name in sorted(names, key=_object_depth, reverse=True):
        obj = bpy.data.objects.get(name)
        if obj is None:
            continue
        try:
            bpy.data.objects.remove(obj, do_unlink=True)
            removed += 1
        except Exception:
            log.exception("Failed to remove replaced cutscene actor object '%s'", name)
    return removed


def unload_cutscene_actor(actor_obj):
    if actor_obj is None:
        return 0

    clear_cutscene_actor_animation_tracks(actor_obj)

    guid = str(actor_obj.get(CUTSCENE_GUID_PROP, "") or "").strip()
    imported_new = bool(actor_obj.get(CUTSCENE_ACTOR_IMPORTED_PROP, False))
    if guid and imported_new:
        from ..ui.ui_equipment import remove_objects_by_guid

        return int(remove_objects_by_guid(guid, CUTSCENE_GUID_PROP) or 0)

    _clear_cutscene_actor_tags(actor_obj)
    return 0


def _resolve_cutscene_actor_appearance(entity, preferred_name="", event=None):
    appearances = list(getattr(entity, "appearances", None) or [])
    if not appearances:
        return None, -1, ""

    if event is not None:
        event_resolved = resolve_body_part_event_appearance(entity, event)
        if event_resolved is not None:
            return event_resolved
        if body_part_event_has_body_state(event):
            return None, -1, ""
        preferred_name = preferred_name or body_part_event_request_name(event)

    preferred_name = str(preferred_name or "").strip()
    if preferred_name:
        preferred_candidates = [preferred_name]
        if ":" in preferred_name:
            body_part, state = preferred_name.split(":", 1)
            preferred_candidates.extend([body_part.strip(), state.strip()])
        seen_candidates = set()
        for candidate in preferred_candidates:
            candidate = str(candidate or "").strip()
            key = candidate.lower()
            if not candidate or key in seen_candidates:
                continue
            seen_candidates.add(key)
            for idx, appearance in enumerate(appearances):
                appearance_name = str(getattr(appearance, "name", "") or "").strip()
                if appearance_name == candidate or appearance_name.lower() == key:
                    return appearance, idx, appearance_name
                if ":" not in candidate and appearance_name.lower().startswith(f"{key}:"):
                    return appearance, idx, appearance_name
                w2_parts = {
                    str(part or "").strip().lower()
                    for part in (getattr(appearance, "w2_parts", None) or [])
                    if str(part or "").strip()
                }
                if key in w2_parts:
                    return appearance, idx, appearance_name

    first_appearance = appearances[0]
    return first_appearance, 0, str(getattr(first_appearance, "name", "") or "").strip()

def _has_loaded_appearance_group(actor_obj, appearance_name):
    appearance_name = str(appearance_name or "").strip()
    if actor_obj is None or not appearance_name:
        return False
    for child in getattr(actor_obj, "children", []):
        if getattr(child, "type", "") != 'EMPTY':
            continue
        child_app_name = str(child.get("witcher_app_name", "") or child.name or "").strip()
        if child_app_name == appearance_name:
            return True
    return False

def _ensure_cutscene_actor_appearance(actor_obj, preferred_name="", event=None):
    if actor_obj is None or getattr(actor_obj, "type", "") != 'ARMATURE':
        return False, ""

    rig_settings = getattr(getattr(actor_obj, "data", None), "witcherui_RigSettings", None)
    if rig_settings is None:
        return False, ""

    entity, _entity_data = import_entity.get_rig_entity_state(rig_settings)
    if entity is None:
        return False, ""

    selected_appearance, app_idx, resolved_name = _resolve_cutscene_actor_appearance(
        entity,
        preferred_name,
        event=event,
    )
    if selected_appearance is None or app_idx < 0:
        return False, ""

    try:
        actor_obj["_w3_entity_import_in_progress"] = True
        rig_settings.app_list_index = app_idx
    except Exception:
        pass
    finally:
        try:
            del actor_obj["_w3_entity_import_in_progress"]
        except Exception:
            pass

    if _has_loaded_appearance_group(actor_obj, resolved_name):
        return True, resolved_name

    try:
        import_entity.import_app(bpy.context, selected_appearance, entity, actor_obj)
        try:
            import_entity._focus_main_armature(bpy.context, actor_obj)
        except Exception:
            pass
        return True, resolved_name
    except Exception:
        log.exception(
            "Failed to apply cutscene appearance '%s' on actor '%s'",
            resolved_name,
            getattr(actor_obj, "name", "<unknown>"),
        )
        return False, resolved_name


def _current_actor_appearance_name(actor_obj):
    rig_settings = getattr(getattr(actor_obj, "data", None), "witcherui_RigSettings", None)
    if rig_settings is None:
        return ""
    try:
        app_idx = int(getattr(rig_settings, "app_list_index", -1))
    except Exception:
        app_idx = -1
    app_list = getattr(rig_settings, "app_list", None)
    if app_idx < 0 or app_list is None or app_idx >= len(app_list):
        return ""
    try:
        return str(getattr(app_list[app_idx], "name", "") or "").strip()
    except Exception:
        return ""


def _ensure_cutscene_w2_face_setup(actor_obj, force=False):
    if _w2_mimic_armature_for_actor(actor_obj) is None:
        return False
    try:
        from ..ui.ui_anims_list import ensure_face_animation_setup, _resolve_face_animation_targets

        loaded, _target_armature = ensure_face_animation_setup(
            bpy.context,
            actor_obj,
            _resolve_face_animation_targets(actor_obj),
            force=force,
        )
        return bool(loaded)
    except Exception:
        log.warning(
            "Failed to prepare Witcher 2 face setup for cutscene actor '%s'.",
            getattr(actor_obj, "name", "<unknown>"),
            exc_info=True,
        )
    return False


def _ensure_cutscene_face_setup(actor_obj, force=False):
    if actor_obj is None or getattr(actor_obj, "type", None) != 'ARMATURE':
        return False
    if _w2_mimic_armature_for_actor(actor_obj) is not None:
        return _ensure_cutscene_w2_face_setup(actor_obj, force=force)
    if 'mimicFaceFile' not in actor_obj or 'mimicFace' not in actor_obj:
        return False
    current_appearance = _current_actor_appearance_name(actor_obj)
    last_face_appearance = str(actor_obj.get(FACE_MORPHS_APPEARANCE_PROP, "") or "").strip()
    force_reload = bool(force or (current_appearance and current_appearance != last_face_appearance))
    try:
        from ..ui.ui_anims_list import ensure_owner_face_animation_setup

        loaded, target_armature = ensure_owner_face_animation_setup(
            bpy.context,
            actor_obj,
            force=force_reload,
        )
        if target_armature is not None:
            if loaded and current_appearance:
                try:
                    target_armature[FACE_MORPHS_APPEARANCE_PROP] = current_appearance
                except Exception:
                    pass
            return bool(loaded)
    except Exception:
        log.warning(
            "Failed to prepare face morph setup for cutscene actor '%s'.",
            getattr(actor_obj, "name", "<unknown>"),
            exc_info=True,
        )
    return False

def _estimate_animation_frame_count(node):
    animation = getattr(node, "animation", None)
    frame_count = int(getattr(getattr(animation, "animBuffer", None), "numFrames", 0) or 0)
    if frame_count > 0:
        return frame_count

    duration = float(getattr(animation, "duration", 0.0) or 0.0)
    fps = float(getattr(animation, "framesPerSecond", 30.0) or 30.0)
    estimated = int(round(duration * fps))
    return max(1, estimated)

def _tag_cutscene_animation_actions(target_armatures, track_name, anim_name, source_path, source_index, at_frame,
                                    duration_frames=None):
    start_frame = float(at_frame or 0.0)
    duration_frames = max(1.0, float(duration_frames or 0.0))
    end_frame = start_frame + duration_frames - 0.001
    for armature_obj in target_armatures or []:
        anim_data = getattr(armature_obj, "animation_data", None)
        if not anim_data:
            continue
        track = anim_data.nla_tracks.get(track_name)
        if track is None:
            continue
        for strip in track.strips:
            strip_frame_start = float(getattr(strip, "frame_start", 0.0) or 0.0)
            if strip_frame_start < start_frame - 0.001 or strip_frame_start > end_frame:
                continue
            action = getattr(strip, "action", None)
            if action is None:
                continue
            action[CUTSCENE_SOURCE_PATH_PROP] = str(source_path or "")
            action[CUTSCENE_SOURCE_INDEX_PROP] = _coerce_source_index(source_index)
            action[CUTSCENE_ANIMATION_NAME_PROP] = str(anim_name or "")

def is_cutscene_animation_loaded(actor_obj, animation_name, source_path, source_index, track_name=None):
    animation_name = str(animation_name or "").strip()
    source_path = str(source_path or "").strip()
    try:
        source_index = int(source_index)
    except Exception:
        source_index = -1

    for armature_obj in _iter_cutscene_related_armatures(actor_obj):
        anim_data = getattr(armature_obj, "animation_data", None)
        if not anim_data:
            continue
        for track in anim_data.nla_tracks:
            current_track_name = str(getattr(track, "name", "") or "")
            if track_name:
                if current_track_name != track_name:
                    continue
            elif not _is_cutscene_track_name(current_track_name):
                continue
            for strip in track.strips:
                action = getattr(strip, "action", None)
                if action is None:
                    continue
                if (
                    str(action.get(CUTSCENE_SOURCE_PATH_PROP, "") or "") == source_path
                    and _coerce_source_index(action.get(CUTSCENE_SOURCE_INDEX_PROP, -1)) == source_index
                ):
                    return True
                action_name = str(getattr(action, "name", "") or "")
                strip_name = str(getattr(strip, "name", "") or "")
                if animation_name and (action_name == animation_name or strip_name == animation_name):
                    return True
    return False

def load_cutscene_actor(filename, actor_index, cutscene_template=None, actor_cache=None):
    cutscene_template = cutscene_template if cutscene_template is not None else loadCutsceneFile(filename)
    if cutscene_template is None:
        return {}

    actor_defs = list(getattr(cutscene_template, "SCutsceneActorDefs", None) or [])
    try:
        actor_index = int(actor_index)
    except Exception:
        actor_index = -1
    if actor_index < 0 or actor_index >= len(actor_defs):
        return {}

    actor = actor_defs[actor_index]
    template_counts = _actor_template_counts(actor_defs)
    actor_name = str(getattr(actor, "name", "") or "").strip()
    template_path = _normalize_repo_path(getattr(actor, "template", "") or "")
    preferred_appearance_name = str(getattr(actor, "appearance", "") or "").strip()
    duplicate_count = template_counts.get(template_path, 0)
    actor_source_game = "w2" if _is_w2_cutscene_file(filename) else "w3"

    actor_obj = find_existing_cutscene_actor(
        actor_name=actor_name,
        repo_path=template_path,
        duplicate_count=duplicate_count,
    )
    imported_new = bool(getattr(actor_obj, "get", lambda *_args, **_kwargs: False)(CUTSCENE_ACTOR_IMPORTED_PROP, False)) if actor_obj else False
    cutscene_guid = str(getattr(actor_obj, "get", lambda *_args, **_kwargs: "")(CUTSCENE_GUID_PROP, "") or "").strip() if actor_obj else ""
    if not actor_obj and template_path and int(duplicate_count or 0) > 1 and actor_cache is not None:
        cached_actor = actor_cache.get(template_path)
        if cached_actor is not None and getattr(cached_actor, "name", None) in bpy.data.objects:
            actor_obj, cutscene_guid = _duplicate_cutscene_actor_from_source(
                cached_actor,
                actor_name=actor_name,
                repo_path=template_path,
                appearance_name=preferred_appearance_name,
            )
            imported_new = actor_obj is not None
    if not actor_obj and template_path:
        try:
            is_w2 = _is_w2_cutscene_file(filename)
            resolved_template_path = _resolve_cutscene_actor_template_path(template_path, filename, is_w2)
            log.info(
                "Cutscene actor '%s' template resolved (%s): %s -> %s",
                actor_name or actor_index,
                "w2" if is_w2 else "w3",
                template_path,
                resolved_template_path,
            )
            actor_obj = import_entity.import_ent_template(
                resolved_template_path,
                load_face_poses=True,
                import_apperance=1,
                selected_appearance_name=preferred_appearance_name,
                entity_namespace=actor_name,
            )
        except Exception:
            log.exception("Failed to import cutscene actor '%s' from '%s'", actor_name or actor_index, template_path)
            actor_obj = None
        if actor_obj is not None:
            imported_new = True
            cutscene_guid = _generate_cutscene_guid()
            _tag_cutscene_object_hierarchy(actor_obj, cutscene_guid)

    if actor_obj is None:
        return {}

    if actor_cache is not None and template_path and template_path not in actor_cache:
        actor_cache[template_path] = actor_obj

    _ensure_cutscene_actor_appearance(actor_obj, preferred_appearance_name)
    _ensure_cutscene_face_setup(actor_obj)
    if imported_new and cutscene_guid:
        _tag_cutscene_object_hierarchy(actor_obj, cutscene_guid)
    _tag_cutscene_actor(
        actor_obj,
        actor,
        source_index=actor_index,
        source_path=filename,
        imported_new=imported_new,
        cutscene_guid=cutscene_guid,
    )
    actor_obj[CUTSCENE_ACTOR_SOURCE_GAME_PROP] = actor_source_game
    return {
        "actor_obj": actor_obj,
        "actor_name": actor_name,
        "template_path": template_path,
        "appearance_name": preferred_appearance_name,
        "source_game": actor_source_game,
        "imported_new": bool(imported_new),
        "cutscene_guid": cutscene_guid,
        "source_index": actor_index,
    }

def apply_cutscene_animation_sequence(filename, animation_indices, actor_obj, actor_name="", track_name=CUTSCENE_TRACK_NAME,
                                      return_errors=False):
    if actor_obj is None:
        return (set(), {}) if return_errors else set()

    cutscene_template = loadCutsceneFile(filename)
    if cutscene_template is None:
        return (set(), {}) if return_errors else set()

    try:
        selected_animation_indices = {int(idx) for idx in (animation_indices or [])}
    except Exception:
        selected_animation_indices = set()
    if not selected_animation_indices:
        return (set(), {}) if return_errors else set()
    return _apply_cutscene_animation_sequence_template(
        cutscene_template,
        filename,
        selected_animation_indices,
        actor_obj,
        actor_name=actor_name,
        track_name=track_name,
        return_errors=return_errors,
    )

def _auto_apply_cutscene_animations(filename, cutscene_template, actor_objects_by_name,
                                     selected_animation_indices=None, actor_repo_paths_by_name=None,
                                     progress_callback=None):
    selected_animation_indices = None if selected_animation_indices is None else {int(idx) for idx in selected_animation_indices}
    actor_repo_paths_by_name = dict(actor_repo_paths_by_name or {})
    actor_animation_indices = {}

    for idx, node in enumerate(getattr(cutscene_template, "animations", None) or []):
        if selected_animation_indices is not None and idx not in selected_animation_indices:
            continue

        anim_name = str(getattr(getattr(node, "animation", None), "name", "") or "")
        actor_name, _component_name, _display_name = split_cutscene_animation_name(anim_name)

        actor_obj = None
        if actor_name:
            actor_obj = (
                actor_objects_by_name.get(actor_name)
                or _find_cutscene_actor_by_name(actor_name)
                or _find_actor_by_repo_path(actor_repo_paths_by_name.get(actor_name, ""))
            )
        elif len(actor_objects_by_name) == 1:
            actor_obj = next(iter(actor_objects_by_name.values()))

        if actor_obj is None:
            log.info("Skipping cutscene animation '%s': no matching actor found in scene.", anim_name or idx)
            continue

        actor_animation_indices.setdefault(actor_obj.name, {
            "actor_obj": actor_obj,
            "actor_name": actor_name,
            "indices": [],
        })
        actor_animation_indices[actor_obj.name]["indices"].append(idx)

    applied_indices = set()
    total_actor_groups = len(actor_animation_indices)
    for actor_group_index, actor_info in enumerate(actor_animation_indices.values(), start=1):
        actor_obj = actor_info["actor_obj"]
        actor_name = actor_info["actor_name"]
        try:
            actor_applied, _actor_errors = _apply_cutscene_animation_sequence_template(
                cutscene_template,
                filename,
                actor_info["indices"],
                actor_obj,
                actor_name=actor_name,
                return_errors=True,
            )
            applied_indices.update(actor_applied)
        except Exception:
            log.exception(
                "Failed to auto-apply cutscene animations on actor '%s'.",
                getattr(actor_obj, "name", "<unknown>"),
            )
        if progress_callback is not None:
            try:
                progress_callback(
                    actor_group_index,
                    total_actor_groups,
                    str(getattr(actor_obj, "name", "") or actor_name or ""),
                )
            except Exception:
                pass

    return len(applied_indices), applied_indices

def _safe_actor_type_str(type_value):
    """Safely extract a clean string from an ECutsceneActorType value."""
    if type_value is None:
        return ""
    if isinstance(type_value, str):
        return type_value
    enum_value = str(read_enum_prop(type_value) or "").strip()
    if enum_value:
        return enum_value
    # Binary PROPERTY object: try .Value then .String.String
    val = getattr(type_value, "Value", None)
    if isinstance(val, str):
        return val
    s = getattr(type_value, "String", None)
    if s is not None:
        ss = getattr(s, "String", None)
        if isinstance(ss, str):
            return ss
    result = str(type_value)
    return "" if result.startswith("<") else result


def _normalize_cutscene_actor_type(type_value, actor_name=""):
    text = str(type_value or "").strip()
    for candidate in ("CAT_None", "CAT_Actor", "CAT_Prop", "CAT_Camera"):
        if text == candidate or candidate in text:
            return candidate
    if str(actor_name or "").strip().lower() == "camera":
        return "CAT_Camera"
    return "CAT_Actor"


def _cutscene_event_value(event, field_name, default=None):
    if event is None:
        return default
    return getattr(event, field_name, default)


def _append_cutscene_preview_event(event_items, ev, event_scope, source_index=-1):
    event_items.append({
        "event_type": str(_cutscene_event_value(ev, "type_name", "") or ""),
        "event_name": str(_cutscene_event_value(ev, "event_name", "") or ""),
        "start_time": float(_cutscene_event_value(ev, "start_time", 0.0) or 0.0),
        "duration": float(_cutscene_event_value(ev, "duration", 0.0) or 0.0),
        "animation_name": str(_cutscene_event_value(ev, "animation_name", "") or ""),
        "track_name": str(_cutscene_event_value(ev, "track_name", "") or ""),
        "effect_name": str(_cutscene_event_value(ev, "effect_name", "") or ""),
        "appearance": str(_cutscene_event_value(ev, "appearance", "") or ""),
        "event_scope": event_scope,
        "source_index": int(source_index),
    })


def _append_unique_existing_dir(roots, root):
    root = str(root or "").strip()
    if not root:
        return
    root = os.path.normpath(root)
    if not os.path.isdir(root):
        return
    root_key = os.path.normcase(root)
    if all(os.path.normcase(existing) != root_key for existing in roots):
        roots.append(root)


def _cutscene_source_root_candidates(cutscene_filepath):
    roots = []
    source_path = str(cutscene_filepath or "").strip().replace("/", "\\")
    if not source_path or not os.path.isabs(source_path):
        return roots

    normalized = os.path.normpath(source_path)
    lowered = normalized.lower()
    for marker in ("\\r4data\\", "\\workspace\\", "\\content\\content0\\"):
        marker_idx = lowered.find(marker)
        if marker_idx >= 0:
            _append_unique_existing_dir(roots, normalized[:marker_idx + len(marker) - 1])

    parent = os.path.dirname(normalized)
    previous = ""
    while parent and parent != previous:
        _append_unique_existing_dir(roots, parent)
        previous = parent
        parent = os.path.dirname(parent)

    return roots


def _resolve_cutscene_linked_scene_file(depot_path, cutscene_filepath):
    raw_path = str(depot_path or "").strip().replace("/", "\\")
    if not raw_path:
        return ""
    if os.path.isabs(raw_path):
        return raw_path if win_path_isfile(raw_path) else ""

    rel_path = raw_path.lstrip("\\")
    for root in _cutscene_source_root_candidates(cutscene_filepath):
        candidate = os.path.normpath(os.path.join(root, rel_path))
        if win_path_isfile(candidate):
            return candidate

    try:
        with redkit_repo_context(cutscene_filepath):
            candidate = repo_file(rel_path)
        if win_path_isfile(candidate):
            return candidate
    except Exception:
        log.debug("repo_file failed while resolving linked cutscene scene: %s", depot_path, exc_info=True)

    return ""


def resolve_cutscene_linked_scene_file(depot_path, cutscene_filepath):
    return _resolve_cutscene_linked_scene_file(depot_path, cutscene_filepath)


def _build_cutscene_animation_contexts(cutscene_template, animation_indices, actor_name="", actor_key=""):
    try:
        selected_animation_indices = {int(idx) for idx in (animation_indices or [])}
    except Exception:
        selected_animation_indices = set()
    if not selected_animation_indices:
        return []

    actor_name = str(actor_name or "").strip()
    actor_key = str(actor_key or actor_name or "<unknown>")
    sequence_state = {}
    contexts = []

    for idx, node in enumerate(getattr(cutscene_template, "animations", None) or []):
        if idx not in selected_animation_indices:
            continue

        anim_name = str(getattr(getattr(node, "animation", None), "name", "") or "")
        node_actor_name, component_name, _display_name = split_cutscene_animation_name(anim_name)
        if actor_name and node_actor_name and node_actor_name != actor_name:
            continue

        duration = _estimate_animation_frame_count(node)
        at_frame = _schedule_cutscene_animation_frame(sequence_state, actor_key, component_name, duration)
        contexts.append({
            "source_index": idx,
            "node": node,
            "anim_name": anim_name,
            "actor_name": node_actor_name,
            "component_name": component_name,
            "duration_frames": duration,
            "at_frame": float(at_frame),
            "frames_per_second": float(getattr(getattr(node, "animation", None), "framesPerSecond", 0.0) or 0.0),
        })

    return contexts


def _find_animation_context_by_name(animation_contexts, anim_name):
    anim_name = str(anim_name or "").strip()
    if not anim_name:
        return None
    for context in animation_contexts or []:
        if str(context.get("anim_name", "") or "").strip() == anim_name:
            return context
    return None


def _resolve_cutscene_event_fps(event, animation_contexts, fallback_fps):
    animation_name = str(_cutscene_event_value(event, "animation_name", "") or "").strip()
    context = _find_animation_context_by_name(animation_contexts, animation_name)
    if context is not None:
        fps = float(context.get("frames_per_second", 0.0) or 0.0)
        if fps > 0.0:
            return fps
    return float(fallback_fps or 30.0)


def _cutscene_body_part_event_appearance(event):
    return body_part_event_request_name(event)


def _iter_cutscene_body_part_events(cutscene_template, animation_contexts, actor_name=""):
    actor_name = str(actor_name or "").strip()
    animation_contexts = list(animation_contexts or [])
    if not animation_contexts:
        return []

    _render = getattr(getattr(bpy.context, "scene", None), "render", None)
    fallback_fps = next(
        (float(ctx.get("frames_per_second", 0.0) or 0.0) for ctx in animation_contexts
         if float(ctx.get("frames_per_second", 0.0) or 0.0) > 0.0),
        float(_render.fps if _render else 30.0),
    )

    body_part_events = []
    order = 0

    for context in animation_contexts:
        for event in getattr(context.get("node"), "entries", None) or []:
            if not is_body_part_event(event):
                continue

            appearance = _cutscene_body_part_event_appearance(event)
            if not appearance:
                continue

            start_time = float(_cutscene_event_value(event, "start_time", 0.0) or 0.0)
            fps = float(context.get("frames_per_second", 0.0) or fallback_fps or 30.0)
            body_part_events.append({
                "event": event,
                "appearance": appearance,
                "frame": float(context.get("at_frame", 0.0) or 0.0) + (start_time * fps),
                "order": order,
            })
            order += 1

    for event in getattr(cutscene_template, "animevents", None) or []:
        if not is_body_part_event(event):
            continue

        appearance = _cutscene_body_part_event_appearance(event)
        if not appearance:
            continue

        animation_name = str(_cutscene_event_value(event, "animation_name", "") or "").strip()
        event_actor_name, _component_name, _display_name = split_cutscene_animation_name(animation_name)
        if actor_name and event_actor_name and event_actor_name != actor_name:
            continue
        if actor_name and not event_actor_name:
            continue

        fps = _resolve_cutscene_event_fps(event, animation_contexts, fallback_fps)
        start_time = float(_cutscene_event_value(event, "start_time", 0.0) or 0.0)
        body_part_events.append({
            "event": event,
            "appearance": appearance,
            "frame": start_time * fps,
            "order": order,
        })
        order += 1

    return body_part_events


def clear_cutscene_actor_appearance_keys(actor_obj):
    if actor_obj is None or getattr(actor_obj, "type", None) != 'ARMATURE':
        return 0

    armature_data = getattr(actor_obj, "data", None)
    anim_data = getattr(armature_data, "animation_data", None)
    action = getattr(anim_data, "action", None)
    if action is None:
        return 0

    removed = 0
    for fcurve in list(iter_action_fcurves(action, target=armature_data)):
        if str(getattr(fcurve, "data_path", "") or "") != CUTSCENE_APPEARANCE_DATA_PATH:
            continue
        remove_action_fcurve(action, fcurve, target=armature_data)
        removed += 1

    remaining_fcurves = tuple(iter_action_fcurves(action, target=armature_data))
    if removed and not remaining_fcurves and action.users == 0:
        bpy.data.actions.remove(action)
    return removed


def _set_cutscene_actor_appearance_key_interpolation(actor_obj):
    if actor_obj is None or getattr(actor_obj, "type", None) != 'ARMATURE':
        return
    armature_data = getattr(actor_obj, "data", None)
    action = getattr(getattr(armature_data, "animation_data", None), "action", None)
    if action is None:
        return
    for fcurve in iter_action_fcurves(action, target=armature_data):
        if str(getattr(fcurve, "data_path", "") or "") != CUTSCENE_APPEARANCE_DATA_PATH:
            continue
        for keyframe in fcurve.keyframe_points:
            keyframe.interpolation = 'CONSTANT'
        try:
            fcurve.update()
        except Exception:
            pass


def _bake_cutscene_body_part_events(cutscene_template, animation_contexts, actor_obj, actor_name=""):
    if actor_obj is None or getattr(actor_obj, "type", None) != 'ARMATURE':
        return 0

    animation_contexts = list(animation_contexts or [])
    clear_cutscene_actor_appearance_keys(actor_obj)
    if not animation_contexts:
        return 0

    body_part_events = _iter_cutscene_body_part_events(cutscene_template, animation_contexts, actor_name=actor_name)
    if not body_part_events:
        return 0

    rig_settings = getattr(getattr(actor_obj, "data", None), "witcherui_RigSettings", None)
    if rig_settings is None:
        return 0

    entity, _entity_data = import_entity.get_rig_entity_state(rig_settings)
    if entity is None:
        return 0

    default_app_idx = int(getattr(rig_settings, "app_list_index", -1) or -1)
    if default_app_idx < 0:
        _selected_appearance, default_app_idx, _resolved_name = _resolve_cutscene_actor_appearance(
            entity,
            str(actor_obj.get("cutscene_actor_appearance", "") or "").strip(),
        )
    if default_app_idx < 0:
        return 0

    scene = getattr(bpy.context, "scene", None)
    restore_auto_load = None
    if scene is not None and hasattr(scene, "witcher_load_app_on_select"):
        restore_auto_load = bool(getattr(scene, "witcher_load_app_on_select", False))
        try:
            scene.witcher_load_app_on_select = False
        except Exception:
            restore_auto_load = None

    baked_events = []
    try:
        for event in body_part_events:
            requested_appearance = str(event.get("appearance", "") or "").strip()
            if not requested_appearance:
                continue

            source_event = event.get("event")
            success, resolved_name = _ensure_cutscene_actor_appearance(
                actor_obj,
                requested_appearance,
                event=source_event,
            )
            if not success:
                log.warning(
                    "Cutscene appearance event '%s' could not be resolved for actor '%s'.",
                    requested_appearance,
                    getattr(actor_obj, "name", "<unknown>"),
                )
                continue

            _selected_appearance, app_idx, _resolved_name = _resolve_cutscene_actor_appearance(
                entity,
                resolved_name,
                event=source_event,
            )
            if app_idx < 0:
                log.warning(
                    "Cutscene appearance event '%s' resolved to '%s' but no app index was found.",
                    requested_appearance,
                    resolved_name,
                )
                continue

            baked_events.append({
                "appearance": resolved_name,
                "app_idx": int(app_idx),
                "frame": float(event.get("frame", 0.0) or 0.0),
                "order": int(event.get("order", 0) or 0),
            })

        rig_settings.app_list_index = int(default_app_idx)
        if not baked_events:
            return 0

        if (
            _w2_mimic_armature_for_actor(actor_obj) is not None
            and getattr(getattr(actor_obj, "pose", None), "bones", None) is not None
            and actor_obj.pose.bones.get("w2_face_poses") is not None
        ):
            _ensure_cutscene_face_setup(actor_obj, force=True)

        actor_obj.data.keyframe_insert(data_path=CUTSCENE_APPEARANCE_DATA_PATH, frame=0.0)
        for event in sorted(baked_events, key=lambda item: (float(item["frame"]), int(item["order"]))):
            rig_settings.app_list_index = int(event["app_idx"])
            actor_obj.data.keyframe_insert(
                data_path=CUTSCENE_APPEARANCE_DATA_PATH,
                frame=float(event["frame"]),
            )

        _set_cutscene_actor_appearance_key_interpolation(actor_obj)
        return len(baked_events)
    finally:
        try:
            rig_settings.app_list_index = int(default_app_idx)
        except Exception:
            pass
        if baked_events and scene is not None:
            try:
                scene.frame_set(scene.frame_current)
                bpy.context.view_layer.update()
            except Exception:
                pass
        if restore_auto_load is not None and scene is not None:
            try:
                scene.witcher_load_app_on_select = bool(restore_auto_load)
            except Exception:
                pass


def _apply_cutscene_animation_sequence_template(cutscene_template, filename, animation_indices, actor_obj, actor_name="",
                                                track_name=CUTSCENE_TRACK_NAME, return_errors=False):
    if cutscene_template is None or actor_obj is None:
        return (set(), {}) if return_errors else set()

    actor_name = str(actor_name or "").strip()
    actor_key = actor_name or str(getattr(actor_obj, "name", "") or "<unknown>")
    animation_contexts = _build_cutscene_animation_contexts(
        cutscene_template,
        animation_indices,
        actor_name=actor_name,
        actor_key=actor_key,
    )
    if not animation_contexts:
        return (set(), {}) if return_errors else set()

    from ..ui.ui_anims_list import load_anim_into_scene

    cutscene_source_game = "w2" if _is_w2_cutscene_file(filename) else ""
    applied_indices = set()
    error_messages = {}
    for context in animation_contexts:
        idx = int(context.get("source_index", -1))
        anim_name = str(context.get("anim_name", "") or "")
        component_name = str(context.get("component_name", "") or "")
        at_frame = float(context.get("at_frame", 0.0) or 0.0)
        animation_track_name = _cutscene_track_name_for_animation(anim_name, base_track=track_name)

        try:
            face_target_mode = "owner"
            is_face_animation = _is_face_cutscene_animation(anim_name)
            target_component = component_name
            if is_face_animation:
                _ensure_cutscene_face_setup(actor_obj)
                if _w2_mimic_armature_for_actor(actor_obj) is not None:
                    face_target_mode = "auto"
            target_armatures = load_anim_into_scene(
                bpy.context,
                anim_name,
                filename,
                actor_obj,
                NLA_track=animation_track_name,
                at_frame=at_frame,
                face_target_mode=face_target_mode,
                target_component=target_component,
                source_game=cutscene_source_game,
            )
            _tag_cutscene_animation_actions(
                target_armatures,
                animation_track_name,
                anim_name,
                filename,
                idx,
                at_frame,
                duration_frames=context.get("duration_frames", 0),
            )
            applied_indices.add(idx)
        except Exception as exc:
            error_text = str(exc or "").strip() or exc.__class__.__name__
            error_messages[idx] = error_text
            log.exception(
                "Failed to apply cutscene animation '%s' on actor '%s'",
                anim_name or idx,
                getattr(actor_obj, "name", "<unknown>"),
            )

    if applied_indices:
        try:
            loaded_contexts = [ctx for ctx in animation_contexts if int(ctx.get("source_index", -1)) in applied_indices]
            _bake_cutscene_body_part_events(cutscene_template, loaded_contexts, actor_obj, actor_name=actor_name)
        except Exception:
            log.exception(
                "Failed to bake cutscene body-part appearance events on actor '%s'.",
                getattr(actor_obj, "name", "<unknown>"),
            )

    if return_errors:
        return applied_indices, error_messages
    return applied_indices


def collect_cutscene_preview(filename, cutscene_template=None):
    cutscene = cutscene_template if cutscene_template is not None else loadCutsceneFile(filename)
    if cutscene is None:
        return None, [], [], []

    actor_defs = list(getattr(cutscene, "SCutsceneActorDefs", None) or [])
    template_counts = _actor_template_counts(actor_defs)
    actor_source_game = "w2" if _is_w2_cutscene_file(filename) else "w3"
    actor_items = []
    for idx, actor in enumerate(actor_defs):
        actor_name = str(getattr(actor, "name", "") or "").strip()
        template_path = _normalize_repo_path(getattr(actor, "template", "") or "")
        appearance_name = str(getattr(actor, "appearance", "") or "").strip()
        display_name = actor_name or os.path.splitext(os.path.basename(template_path))[0] or f"Actor {idx + 1}"
        existing = find_existing_cutscene_actor(
            actor_name=actor_name,
            repo_path=template_path,
            duplicate_count=template_counts.get(template_path, 0),
        )
        tag_list = getattr(actor, "tag", None)
        final_pos_list = getattr(actor, "finalPosition", None)
        actor_items.append({
            "source_index": idx,
            "label": display_name,
            "actor_name": actor_name,
            "tag": "; ".join(str(t or "").strip() for t in (tag_list or []) if str(t or "").strip()),
            "voice_tag": str(getattr(actor, "voiceTag", "") or "").strip(),
            "template_path": template_path,
            "source_game": actor_source_game,
            "appearance_name": appearance_name,
            "actor_type": _normalize_cutscene_actor_type(
                _safe_actor_type_str(getattr(actor, "type", None)),
                actor_name=actor_name,
            ),
            "final_position": "; ".join(str(t or "").strip() for t in (final_pos_list or []) if str(t or "").strip()),
            "kill_me": bool(getattr(actor, "killMe", False) or False),
            "use_mimic": bool(getattr(actor, "useMimic", False)),
            "anim_final_pos": str(getattr(actor, "animationAtFinalPosition", "") or "").strip(),
            "already_in_scene": bool(existing),
        })

    animation_items = []
    for idx, node in enumerate(getattr(cutscene, "animations", None) or []):
        animation = getattr(node, "animation", None)
        full_name = str(getattr(animation, "name", "") or f"Animation {idx + 1}")
        actor_name, component_name, display_name = split_cutscene_animation_name(full_name)
        animation_items.append({
            "source_index": idx,
            "full_name": full_name,
            "display_name": full_name or display_name,
            "actor_name": actor_name,
            "component_name": component_name,
            "frames_per_second": float(getattr(animation, "framesPerSecond", 0.0) or 0.0),
            "num_frames": int(getattr(getattr(animation, "animBuffer", None), "numFrames", 0) or 0),
            "duration": float(getattr(animation, "duration", 0.0) or 0.0),
        })

    event_items = []
    for ev in getattr(cutscene, "animevents", None) or []:
        _append_cutscene_preview_event(event_items, ev, "ROOT")

    for idx, node in enumerate(getattr(cutscene, "animations", None) or []):
        for ev in getattr(node, "entries", None) or []:
            _append_cutscene_preview_event(event_items, ev, "ENTRY", source_index=idx)

    return cutscene, actor_items, animation_items, event_items


def _cutscene_template_default_fps(cutscene_template):
    for node in getattr(cutscene_template, "animations", None) or []:
        fps = float(getattr(getattr(node, "animation", None), "framesPerSecond", 0.0) or 0.0)
        if fps > 0.0:
            return fps
    return 30.0


def _cutscene_animation_fps_by_name(cutscene_template):
    by_name = {}
    for node in getattr(cutscene_template, "animations", None) or []:
        animation = getattr(node, "animation", None)
        name = str(getattr(animation, "name", "") or "").strip()
        fps = float(getattr(animation, "framesPerSecond", 0.0) or 0.0)
        if name and fps > 0.0:
            by_name[name] = fps
    return by_name


def _append_cutscene_dialog_event_frame(out, event, event_scope, fps, source_index=-1, frame_offset=0.0):
    if "DialogEvent" not in str(_cutscene_event_value(event, "type_name", "") or ""):
        return
    start_time = float(_cutscene_event_value(event, "start_time", 0.0) or 0.0)
    duration = float(_cutscene_event_value(event, "duration", 0.0) or 0.0)
    out.append({
        "frame": int(round(float(frame_offset or 0.0) + (start_time * fps))),
        "duration_frames": max(0, int(round(duration * fps))),
        "source_index": int(source_index),
        "event_scope": event_scope,
    })


def collect_cutscene_dialog_event_frames(cutscene_filepath, cutscene_template=None):
    cutscene = cutscene_template if cutscene_template is not None else loadCutsceneFile(cutscene_filepath)
    if cutscene is None:
        return []

    fallback_fps = _cutscene_template_default_fps(cutscene)
    animation_fps_by_name = _cutscene_animation_fps_by_name(cutscene)
    dialog_events = []

    for event in getattr(cutscene, "animevents", None) or []:
        animation_name = str(_cutscene_event_value(event, "animation_name", "") or "").strip()
        fps = animation_fps_by_name.get(animation_name, fallback_fps)
        _append_cutscene_dialog_event_frame(dialog_events, event, "ROOT", fps)

    for idx, node in enumerate(getattr(cutscene, "animations", None) or []):
        animation = getattr(node, "animation", None)
        fps = float(getattr(animation, "framesPerSecond", 0.0) or fallback_fps or 30.0)
        for event in getattr(node, "entries", None) or []:
            _append_cutscene_dialog_event_frame(dialog_events, event, "ENTRY", fps, source_index=idx)

    dialog_events.sort(key=lambda item: (int(item["frame"]), int(item["source_index"])))
    return dialog_events


def load_cutscene_dialog_items(cutscene_filepath):
    """Do the reverse lookup from the linked .w2scene list.

    Returns a list of dicts {actor, voice_file, sound_event, line_id,
    line_index, line_text, scene_path}.
    """
    from ..CR2W.dc_scene import get_cutscene_dialog_lines

    cutscene = loadCutsceneFile(cutscene_filepath)
    if cutscene is None:
        return []

    used_in_files = [
        str(depot_path or "").strip()
        for depot_path in (getattr(cutscene, 'usedInFiles', None) or [])
        if str(depot_path or "").strip()
    ]
    if not used_in_files:
        return []

    # The first linked scene is the primary one for cutscene dialog lookups.
    ordered_scene_paths = [used_in_files[0], *used_in_files[1:]]
    dialog_items = []

    for idx, depot_path in enumerate(ordered_scene_paths):
        scene_abs = _resolve_cutscene_linked_scene_file(depot_path, cutscene_filepath)
        if not scene_abs:
            log.debug("Could not resolve scene file for dialog lookup: %s", depot_path)
            continue

        try:
            with redkit_repo_context(cutscene_filepath):
                lines = get_cutscene_dialog_lines(scene_abs, cutscene_filepath)
        except Exception:
            log.exception("Dialog lookup failed for %s in %s", cutscene_filepath, scene_abs)
            continue

        if not lines and idx == 0:
            continue

        for line in lines:
            try:
                line_index = int(line.get("line_index", 0) or 0)
            except (TypeError, ValueError):
                line_index = 0
            dialog_items.append({
                "actor":       str(line.get("actor", "") or ""),
                "voice_file":  str(line.get("voice_file", "") or ""),
                "sound_event": str(line.get("sound_event", "") or ""),
                "line_id":     str(line.get("line_id", "") or ""),
                "line_index":  line_index,
                "line_text":   str(line.get("line_text", "") or ""),
                "scene_path":  depot_path,
                "source_game": str(line.get("source_game", "") or ""),
            })

        if dialog_items and idx == 0:
            break

    return dialog_items


def import_w3_cutscene(filename, selected_actor_indices=None, selected_animation_indices=None,
                       auto_apply_selected_animations=False, import_burned_audio=True):
    context = bpy.context
    scene = context.scene
    window_manager = getattr(context, "window_manager", None)
    workspace = getattr(context, "workspace", None)
    import_started_at = time.perf_counter()

    def _set_progress(percent, message):
        _cutscene_progress_update(window_manager, workspace, percent, message)

    _cutscene_progress_begin(window_manager)
    try:
        _set_progress(2, "Loading cutscene...")
        CCutsceneTemplate = loadCutsceneFile(filename)
        if CCutsceneTemplate is None:
            return None

        treeList = scene.witcher_w2cutscene_list
        treeList.clear()
        scene.witcher_loaded_w2cutscene_path = filename

        selected_actor_indices = None if selected_actor_indices is None else {int(idx) for idx in selected_actor_indices}
        selected_animation_indices = None if selected_animation_indices is None else {int(idx) for idx in selected_animation_indices}
        actor_defs = list(getattr(CCutsceneTemplate, "SCutsceneActorDefs", None) or [])
        actor_objects_by_name = {}
        actor_repo_paths_by_name = {
            str(getattr(actor, "name", "") or "").strip(): _normalize_repo_path(getattr(actor, "template", "") or "")
            for actor in actor_defs
            if str(getattr(actor, "name", "") or "").strip()
        }
        actor_cache_by_template = {}
        loaded_actor_object_names_by_index = {}
        loaded_actor_imported_flags_by_index = {}
        loaded_actor_guid_by_index = {}

        for idx, node in enumerate(getattr(CCutsceneTemplate, "animations", None) or []):
            if selected_animation_indices is not None and idx not in selected_animation_indices:
                continue
            NewW2ANIMSListItem(treeList, node)
        scene.witcher_w2cutscene_list_index = 0 if len(treeList) else -1
        _set_progress(15, "Preparing cutscene import...")

        actor_indices_to_load = [
            idx for idx, _actor in enumerate(actor_defs)
            if selected_actor_indices is None or idx in selected_actor_indices
        ]
        actor_total = len(actor_indices_to_load)
        for actor_step_index, idx in enumerate(actor_indices_to_load, start=1):
            actor_info = load_cutscene_actor(
                filename,
                idx,
                cutscene_template=CCutsceneTemplate,
                actor_cache=actor_cache_by_template,
            )
            actor_obj = actor_info.get("actor_obj")
            actor_name = str(actor_info.get("actor_name", "") or "").strip()
            if actor_obj:
                if actor_name:
                    actor_objects_by_name[actor_name] = actor_obj
                loaded_actor_object_names_by_index[idx] = str(getattr(actor_obj, "name", "") or "")
                loaded_actor_imported_flags_by_index[idx] = bool(actor_info.get("imported_new", False))
                loaded_actor_guid_by_index[idx] = str(actor_info.get("cutscene_guid", "") or "")
            if actor_total:
                progress = 15 + int(round((actor_step_index / actor_total) * 45))
                _set_progress(progress, f"Importing cutscene actors... {actor_step_index}/{actor_total}")
        if not actor_total:
            _set_progress(60, "No cutscene actors selected for import.")

        auto_applied_animation_count = 0
        applied_animation_indices = set()
        burned_audio_info = None
        if auto_apply_selected_animations:
            def _auto_apply_progress(done_count, total_count, actor_label):
                total_count = max(1, int(total_count or 0))
                progress = 60 + int(round((done_count / total_count) * 35))
                label = str(actor_label or "").strip()
                suffix = f" ({label})" if label else ""
                _set_progress(
                    progress,
                    f"Auto-applying cutscene animations... {done_count}/{total_count}{suffix}",
                )

            auto_applied_animation_count, applied_animation_indices = _auto_apply_cutscene_animations(
                filename,
                CCutsceneTemplate,
                actor_objects_by_name,
                selected_animation_indices=selected_animation_indices,
                actor_repo_paths_by_name=actor_repo_paths_by_name,
                progress_callback=_auto_apply_progress,
            )
            _set_progress(95, "Finishing cutscene import...")
        else:
            _set_progress(95, "Finishing cutscene import...")

        has_burned_audio_prop, burned_audio_name = _get_cutscene_burned_audio_property(CCutsceneTemplate)
        if import_burned_audio and has_burned_audio_prop and burned_audio_name:
            _set_progress(97, "Importing burned audio track...")
            try:
                burned_audio_info = _import_cutscene_burned_audio_track(context, CCutsceneTemplate, source_path=filename)
            except Exception as exc:
                log.warning(
                    "Failed to import burned audio track '%s' for cutscene %s: %s",
                    burned_audio_name,
                    os.path.basename(filename),
                    exc,
                )

        import_duration_seconds = time.perf_counter() - import_started_at

        try:
            CCutsceneTemplate.auto_applied_animation_count = int(auto_applied_animation_count)
            CCutsceneTemplate.import_duration_seconds = float(import_duration_seconds)
        except Exception:
            pass
        try:
            CCutsceneTemplate.loaded_actor_object_names_by_index = dict(loaded_actor_object_names_by_index)
            CCutsceneTemplate.loaded_actor_imported_flags_by_index = dict(loaded_actor_imported_flags_by_index)
            CCutsceneTemplate.loaded_actor_guid_by_index = dict(loaded_actor_guid_by_index)
            CCutsceneTemplate.applied_animation_indices = sorted(applied_animation_indices)
            CCutsceneTemplate.burned_audio_info = dict(burned_audio_info or {})
        except Exception:
            pass
        if hasattr(scene, "witcher_cutscene_last_import_seconds"):
            scene.witcher_cutscene_last_import_seconds = float(import_duration_seconds)

        _set_progress(100, f"Cutscene import finished in {import_duration_seconds:.2f}s")
        log.info(
            "Imported cutscene '%s' in %.2fs (%d actor(s), %d animation(s), auto-applied %d).",
            os.path.basename(filename),
            import_duration_seconds,
            len(actor_indices_to_load),
            len(treeList),
            auto_applied_animation_count,
        )
        if burned_audio_info:
            log.info(
                "Imported burned audio '%s' for cutscene '%s' from %s.",
                burned_audio_info.get("event_name", ""),
                os.path.basename(filename),
                burned_audio_info.get("item_path", ""),
            )
        return CCutsceneTemplate
    finally:
        _cutscene_progress_end(window_manager, workspace)
