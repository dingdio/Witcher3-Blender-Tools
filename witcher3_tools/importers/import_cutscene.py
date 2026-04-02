import logging
import os
import time
from collections import Counter
from ..CR2W import w3_types
from ..importers import import_entity
from ..action_compat import iter_action_fcurves, remove_action_fcurve
from ..CR2W.dc_anims import load_bin_cutscene
from ..CR2W.common_blender import repo_file
from ..duplication import duplicate_character_hierarchy

log = logging.getLogger(__name__)

CUTSCENE_GUID_PROP = "witcher_cutscene_guid"
CUTSCENE_TRACK_NAME = "cutscene_import"
CUTSCENE_FACE_TRACK_NAME = f"{CUTSCENE_TRACK_NAME}_face"
CUTSCENE_SOURCE_PATH_PROP = "witcher_cutscene_source_path"
CUTSCENE_SOURCE_INDEX_PROP = "witcher_cutscene_source_index"
CUTSCENE_ANIMATION_NAME_PROP = "witcher_cutscene_animation_name"
CUTSCENE_ACTOR_IMPORTED_PROP = "cutscene_actor_imported"
CUTSCENE_APPEARANCE_DATA_PATH = "witcherui_RigSettings.app_list_index"

def loadCutsceneFile(filename):
    ext = os.path.splitext(filename)[1]
    if ext.lower().endswith('.w2cutscene'):
        return load_bin_cutscene(filename)
    return None

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
    actor_obj["cutscene_actor_type"] = str(getattr(actor, "type", "") or "CAT_Actor")
    actor_obj["cutscene_component"] = "Root"
    appearance_name = str(getattr(actor, "appearance", "") or "").strip()
    if appearance_name:
        actor_obj["cutscene_actor_appearance"] = appearance_name
    if source_path:
        actor_obj[CUTSCENE_SOURCE_PATH_PROP] = str(source_path)
    if int(source_index or -1) >= 0:
        actor_obj[CUTSCENE_SOURCE_INDEX_PROP] = int(source_index)
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

def _iter_additional_cutscene_armatures(actor_obj):
    if actor_obj is None:
        return
    mimic_name = str(actor_obj.get("mimicFace", "") or "").strip()
    if mimic_name:
        mimic_obj = bpy.data.objects.get(mimic_name)
        if mimic_obj is not None and getattr(mimic_obj, "type", None) == 'ARMATURE':
            yield mimic_obj

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

def _resolve_cutscene_actor_appearance(entity, preferred_name=""):
    appearances = list(getattr(entity, "appearances", None) or [])
    if not appearances:
        return None, -1, ""

    preferred_name = str(preferred_name or "").strip()
    if preferred_name:
        for idx, appearance in enumerate(appearances):
            appearance_name = str(getattr(appearance, "name", "") or "").strip()
            if appearance_name == preferred_name:
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

def _ensure_cutscene_actor_appearance(actor_obj, preferred_name=""):
    if actor_obj is None or getattr(actor_obj, "type", "") != 'ARMATURE':
        return False, ""

    rig_settings = getattr(getattr(actor_obj, "data", None), "witcherui_RigSettings", None)
    if rig_settings is None:
        return False, ""

    entity, _entity_data = import_entity.get_rig_entity_state(rig_settings)
    if entity is None:
        return False, ""

    selected_appearance, app_idx, resolved_name = _resolve_cutscene_actor_appearance(entity, preferred_name)
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

def _ensure_cutscene_face_setup(actor_obj):
    if actor_obj is None or getattr(actor_obj, "type", None) != 'ARMATURE':
        return False
    if 'mimicFaceFile' not in actor_obj or 'mimicFace' not in actor_obj:
        return False
    try:
        from ..ui.ui_anims_list import ensure_owner_face_animation_setup

        loaded, target_armature = ensure_owner_face_animation_setup(bpy.context, actor_obj)
        if target_armature is not None:
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

def _tag_cutscene_animation_actions(target_armatures, track_name, anim_name, source_path, source_index, at_frame):
    for armature_obj in target_armatures or []:
        anim_data = getattr(armature_obj, "animation_data", None)
        if not anim_data:
            continue
        track = anim_data.nla_tracks.get(track_name)
        if track is None:
            continue
        for strip in track.strips:
            if abs(float(getattr(strip, "frame_start", 0.0)) - float(at_frame)) > 0.001:
                continue
            action = getattr(strip, "action", None)
            if action is None:
                continue
            action[CUTSCENE_SOURCE_PATH_PROP] = str(source_path or "")
            action[CUTSCENE_SOURCE_INDEX_PROP] = int(source_index)
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
                    and int(action.get(CUTSCENE_SOURCE_INDEX_PROP, -1) or -1) == source_index
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
            actor_obj = import_entity.import_ent_template(
                repo_file(template_path),
                load_face_poses=True,
                import_apperance=1,
                selected_appearance_name=preferred_appearance_name,
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
    _tag_cutscene_actor(
        actor_obj,
        actor,
        source_index=actor_index,
        source_path=filename,
        imported_new=imported_new,
        cutscene_guid=cutscene_guid,
    )
    return {
        "actor_obj": actor_obj,
        "actor_name": actor_name,
        "template_path": template_path,
        "appearance_name": preferred_appearance_name,
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
            if str(_cutscene_event_value(event, "type_name", "") or "") != "CExtAnimCutsceneBodyPartEvent":
                continue

            appearance = str(_cutscene_event_value(event, "appearance", "") or "").strip()
            if not appearance:
                continue

            start_time = float(_cutscene_event_value(event, "start_time", 0.0) or 0.0)
            fps = float(context.get("frames_per_second", 0.0) or fallback_fps or 30.0)
            body_part_events.append({
                "appearance": appearance,
                "frame": float(context.get("at_frame", 0.0) or 0.0) + (start_time * fps),
                "order": order,
            })
            order += 1

    for event in getattr(cutscene_template, "animevents", None) or []:
        if str(_cutscene_event_value(event, "type_name", "") or "") != "CExtAnimCutsceneBodyPartEvent":
            continue

        appearance = str(_cutscene_event_value(event, "appearance", "") or "").strip()
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

            success, resolved_name = _ensure_cutscene_actor_appearance(actor_obj, requested_appearance)
            if not success:
                log.warning(
                    "Cutscene appearance event '%s' could not be resolved for actor '%s'.",
                    requested_appearance,
                    getattr(actor_obj, "name", "<unknown>"),
                )
                continue

            _selected_appearance, app_idx, _resolved_name = _resolve_cutscene_actor_appearance(entity, resolved_name)
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

    applied_indices = set()
    error_messages = {}
    for context in animation_contexts:
        idx = int(context.get("source_index", -1))
        anim_name = str(context.get("anim_name", "") or "")
        component_name = str(context.get("component_name", "") or "")
        at_frame = float(context.get("at_frame", 0.0) or 0.0)
        animation_track_name = _cutscene_track_name_for_animation(anim_name, base_track=track_name)

        try:
            if _is_face_cutscene_animation(anim_name):
                _ensure_cutscene_face_setup(actor_obj)
            target_armatures = load_anim_into_scene(
                bpy.context,
                anim_name,
                filename,
                actor_obj,
                NLA_track=animation_track_name,
                at_frame=at_frame,
                face_target_mode="owner",
            )
            _tag_cutscene_animation_actions(
                target_armatures,
                animation_track_name,
                anim_name,
                filename,
                idx,
                at_frame,
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
        actor_items.append({
            "source_index": idx,
            "label": display_name,
            "actor_name": actor_name,
            "voice_tag": str(getattr(actor, "voiceTag", "") or "").strip(),
            "template_path": template_path,
            "appearance_name": appearance_name,
            "actor_type": _safe_actor_type_str(getattr(actor, "type", None)),
            "use_mimic": bool(getattr(actor, "useMimic", False)),
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


def load_cutscene_dialog_items(cutscene_filepath):
    """Do the reverse lookup from the linked .w2scene list.

    Returns a list of dicts {actor, voice_file, sound_event, line_index, scene_path}.
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
        try:
            scene_abs = repo_file(depot_path)
        except Exception:
            scene_abs = None
        if not scene_abs or not os.path.isfile(scene_abs):
            log.debug("Could not resolve scene file for dialog lookup: %s", depot_path)
            continue

        try:
            lines = get_cutscene_dialog_lines(scene_abs, cutscene_filepath)
        except Exception:
            log.exception("Dialog lookup failed for %s in %s", cutscene_filepath, scene_abs)
            continue

        if not lines and idx == 0:
            continue

        for line in lines:
            dialog_items.append({
                "actor":       str(line.get("actor", "") or ""),
                "voice_file":  str(line.get("voice_file", "") or ""),
                "sound_event": str(line.get("sound_event", "") or ""),
                "line_index":  int(line.get("line_index", 0) or 0),
                "scene_path":  depot_path,
            })

        if dialog_items and idx == 0:
            break

    return dialog_items


def import_w3_cutscene(filename, selected_actor_indices=None, selected_animation_indices=None,
                       auto_apply_selected_animations=False):
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
        return CCutsceneTemplate
    finally:
        _cutscene_progress_end(window_manager, workspace)
