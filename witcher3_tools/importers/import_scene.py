import logging
log = logging.getLogger(__name__)
import os
import json
from collections import deque
from pathlib import Path
from .. import get_uncook_path
from ..CR2W import read_json_w3
from ..CR2W import w3_types
from ..CR2W.CR2W_types import EngineTransform
from ..CR2W.common_blender import repo_file, redkit_repo_context, vanilla_only_repo_context
from ..CR2W.scene_csv_utils import (
    _lookup_dialogset_body_anim,
    _resolve_mimic_layer_anim_candidates,
)
from ..CR2W.dc_scene import load_bin_scene
from ..importers import import_entity
from ..importers import import_scene_animation
from ..importers import import_scene_motion
from ..importers.import_helpers import set_blender_object_transform#, set_blender_pose_bone_transform
from ..action_compat import assign_action, bind_strip_action_slot, get_action_channelbag, iter_action_fcurves, new_action_fcurve, resolve_action_slot
from .import_cutscene import (
    check_if_actor_already_in_scene,
    _ensure_cutscene_actor_appearance,
    _ensure_cutscene_face_setup,
)
from ..camera_tracks import (
    CAMERA_CONTROL_BONE,
    CAMERA_EDIT_BONE,
    CAMERA_TRACK_DEFAULTS,
    CAMERA_TRACK_NAMES,
    ensure_camera_track_properties,
    find_camera_preview_object,
    setup_camera_preview_drivers,
)
from .import_scene_fcurves import (
    bone_base_name as _w2scene_bone_base_name,
    collect_pose_transform_curves as _collect_pose_transform_curves,
    ensure_pose_transform_fcurves as _ensure_pose_transform_fcurves,
    find_pose_bone_name as _find_w2scene_pose_bone_name,
    keyframe_frames as _keyframe_frames,
    set_fcurve_value_at_frame as _set_fcurve_value_at_frame,
    set_transform_group_values as _set_w2scene_transform_group_values,
    transform_matrix_from_curve_groups as _transform_matrix_from_curve_groups,
)
from mathutils import Euler, Quaternion
from math import radians
import math
from mathutils import Matrix, Vector
from ..ui.ui_voice import _find_face_meshes, _get_sequence_editor_strips, load_voice_and_lipsync
from ..ui.ui_anims_list import SetupActor, GetAnimationInfoByName, load_anim_into_scene

def loadSceneFile(fileName):
    dirpath, file = os.path.split(fileName)
    basename, ext = os.path.splitext(file)
    if fileName.endswith('.w2scene'):
        w3Data = load_bin_scene(fileName)
        return w3Data
    else:
        pass

import bpy
from .import_anims import (
    NewW2ANIMSListItem,
    apply_mimic_pose_weight_to_strip,
    apply_mimic_scene_weight_to_strip,
)
from .. import get_all_addon_prefs as _get_all_addon_prefs


def _scene_linked_assets_prefer_bundles():
    """Return True when the user has opted to resolve scene-linked assets from
    vanilla bundles rather than the REDkit depot. When active, the scene file
    itself still loads from its original absolute path, but all referenced assets
    (entity templates, meshes, textures …) bypass REDkit priority."""
    try:
        return bool(_get_all_addon_prefs(bpy.context).prefer_bundles_for_linked_assets)
    except Exception:
        return False

W2SCENE_AUDIO_STRIP_PROP = "witcher_w2scene_section_audio"
W2SCENE_AUDIO_SOURCE_PROP = "witcher_w2scene_source"
W2SCENE_AUDIO_SECTION_PROP = "witcher_w2scene_section"
W2SCENE_NLA_STRIP_BLEND_TYPE_PROP = "_w3_scene_blend_type"
W2SCENE_DIALOGSET_IDLE_TRACK_NAME = "SceneDialogsetIdle"
W2SCENE_DIALOGSET_IDLE_CHANGE_TRACK_PREFIX = "SceneDialogsetIdleChange"
W2SCENE_DIALOGSET_MIMICS_TRACK_PREFIX = "SceneDialogsetMimics"
W2SCENE_DIALOGSET_MIMICS_CHANGE_TRACK_PREFIX = "SceneDialogsetMimicsChange"
W2SCENE_SECTION_NLA_TRACK_NAMES = {
    W2SCENE_DIALOGSET_IDLE_TRACK_NAME,
    "SceneMotionExtraction",
    "ScenePlacement",
    "SceneVisibility",
    "voice_import",
    "voice_import_phoneme",
}
W2SCENE_SECTION_NLA_TRACK_PREFIXES = (
    W2SCENE_DIALOGSET_IDLE_CHANGE_TRACK_PREFIX,
    W2SCENE_DIALOGSET_MIMICS_TRACK_PREFIX,
    "cutscene_import_body",
    "cutscene_import_pose",
    "cutscene_import_mimic",
)
W2SCENE_NLA_TRACK_NAME_MAX = 63
W2SCENE_SCENE_FPS = 30.0
W2SCENE_ANIM_CLIP_DEFAULT_BLEND_IN = 0.5
W2SCENE_ANIM_CLIP_DEFAULT_BLEND_OUT = 0.5
W2SCENE_ANIM_CLIP_DEFAULT_CLIP_FRONT = 0.0
W2SCENE_ANIM_CLIP_DEFAULT_CLIP_END = -1.0
W2SCENE_ANIM_CLIP_DEFAULT_STRETCH = 1.0
W2SCENE_ANIM_CLIP_DEFAULT_WEIGHT = 1.0
W2SCENE_ANIM_CLIP_DEFAULT_ADDITIVE_TYPE = "AT_Local"
W2SCENE_ANIM_CLIP_DEFAULT_CONVERT_TO_ADDITIVE = True
W2SCENE_APPLY_MOTION_EXTRACTION_TO_POSE = False
W2SCENE_ACTION_SCENE_COPY_PROP = "_w3_scene_action_copy"
W2SCENE_ACTION_ADDITIVE_CONVERT_PROP = "_w3_scene_additive_converted"
W2SCENE_ACTION_TRAJECTORY_EXTRACTED_PROP = "_w3_scene_trajectory_extracted"
W2SCENE_ACTION_TRAJECTORY_SOURCE_PROP = "w3_scene_trajectory_source"
W2SCENE_ACTION_ADDITIVE_WEIGHT_PROP = "w3_scene_additive_weight"
W2SCENE_ACTION_ADDITIVE_WEIGHT_APPLIED_PROP = "w3_scene_additive_weight_applied"
W2SCENE_ACTION_ADDITIVE_WEIGHT_SOURCE_PROP = "_w3_scene_additive_weight_source_action"
W2SCENE_EVENT_WEIGHT_PROP = "w3_scene_event_weight"
W2SCENE_EVENT_WEIGHT_APPLIED_PROP = "w3_scene_event_weight_applied_to_strip"
W2SCENE_ACTION_BLEND_TYPE_PROP = "_w3_scene_blend_type"
W2SCENE_ACTION_BLEND_IN_PROP = "_w3_scene_blend_in"
W2SCENE_ACTION_BLEND_OUT_PROP = "_w3_scene_blend_out"
W2SCENE_ACTION_ROOT_ORIENTATION_PROP = "w3_scene_root_orientation_applied"
W2SCENE_EVENT_GUID_INDEX_PROP = "w3_scene_event_guid_index"
W2SCENE_CAMERA_ENTITY_PATH = "gameplay\\camera\\scene_camera.w2ent"
W2SCENE_CAMERA_LEGACY_RAW_SHOTS_TRACK_NAME = "CameraRawShots"
W2SCENE_CAMERA_SHOT_TRACK_PREFIX = "CameraShot_"
W2SCENE_CAMERA_LEGACY_RAW_INTERPOLATION_TRACK_PREFIX = "CameraInterpolation_"
W2SCENE_CAMERA_NLA_TRACK_NAMES = {
    "CameraInterpolation",
    "CameraShots",
    "CustomCamera",
    "CustomCameraInstance",
    W2SCENE_CAMERA_LEGACY_RAW_SHOTS_TRACK_NAME,
    "PAUSE",
    "dialogLine",
}
W2SCENE_CAMERA_NLA_TRACK_PREFIXES = (
    W2SCENE_CAMERA_SHOT_TRACK_PREFIX,
    W2SCENE_CAMERA_LEGACY_RAW_INTERPOLATION_TRACK_PREFIX,
)
W2SCENE_DEBUG_COLLECTION_NAME = "W2SCENE_DEBUG"
W2SCENE_DEBUG_EMPTY_PROP = "witcher_w2scene_debug_empty"
W2SCENE_DEBUG_EMPTY_TYPE_PROP = "witcher_w2scene_debug_type"
W2SCENE_DEBUG_SECTION_PROP = "witcher_w2scene_debug_section"


def _strip_get(strip, prop_name, default=None):
    try:
        return strip.get(prop_name, default)
    except Exception:
        return default


def _looks_like_voice_line_strip(strip):
    name = str(getattr(strip, "name", "") or "").split(".", 1)[0]
    return len(name) == 10 and name.isdigit()


def clear_w2scene_section_audio(scene):
    strips = _get_sequence_editor_strips(getattr(scene, "sequence_editor", None))
    if strips is None:
        return 0
    removed = 0
    for strip in list(strips):
        if getattr(strip, "type", None) != 'SOUND':
            continue
        if not bool(_strip_get(strip, W2SCENE_AUDIO_STRIP_PROP, False)) and not _looks_like_voice_line_strip(strip):
            continue
        try:
            strips.remove(strip)
            removed += 1
        except Exception:
            log.debug("Could not remove .w2scene audio strip %s", getattr(strip, "name", ""), exc_info=True)
    return removed


def _nla_track_name_matches(track_name, track_names=(), track_prefixes=()):
    current_name = str(track_name or "")
    if any(current_name == name or current_name.startswith(f"{name}.") for name in track_names):
        return True
    return any(current_name.startswith(prefix) for prefix in track_prefixes)


def _w2scene_track_is_mimic_layer(track_name):
    current_name = str(track_name or "")
    return (
        current_name == "voice_import"
        or current_name.startswith("voice_import.")
        or current_name == "voice_import_phoneme"
        or current_name.startswith("voice_import_phoneme.")
        or current_name.startswith("cutscene_import_mimic")
        or current_name.startswith(W2SCENE_DIALOGSET_MIMICS_TRACK_PREFIX)
    )


def _w2scene_track_is_body_layer(track_name):
    current_name = str(track_name or "")
    base_name = current_name.split(".", 1)[0]
    return (
        base_name == W2SCENE_DIALOGSET_IDLE_TRACK_NAME
        or base_name.startswith(W2SCENE_DIALOGSET_IDLE_CHANGE_TRACK_PREFIX)
        or current_name.startswith("cutscene_import_body")
        or current_name.startswith("cutscene_import_pose")
    )


def _w2scene_event_uses_additive_body_blend(event):
    event_class = event.__class__.__name__ if event is not None else ""
    animation_type = _w2scene_event_animation_type(event)
    return event_class == "CStorySceneEventAdditiveAnimation" or "ADDITIVE" in animation_type.upper()


def _w2scene_event_animation_type(event):
    return _enum_string(getattr(event, "animationType", None), "")


def _as_bool_scene_prop(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    return default


def _normalise_w2scene_additive_type(value):
    text = _enum_string(value, "") or str(value or W2SCENE_ANIM_CLIP_DEFAULT_ADDITIVE_TYPE).strip()
    key = text.replace("-", "_").replace(" ", "_").upper()
    if key in {"AT_LOCAL", "ATLOCAL", "LOCAL"}:
        return "AT_Local"
    if key in {"AT_REF", "ATREF", "REF", "REFERENCE"}:
        return "AT_Ref"
    return text


def _w2scene_event_additive_type(event):
    event_class = event.__class__.__name__ if event is not None else ""
    if event_class == "CStorySceneEventAdditiveAnimation":
        return _normalise_w2scene_additive_type(getattr(event, "additiveType", None))
    return _normalise_w2scene_additive_type(getattr(event, "addAdditiveType", None))


def _w2scene_event_convert_to_additive(event):
    event_class = event.__class__.__name__ if event is not None else ""
    if event_class == "CStorySceneEventAdditiveAnimation":
        return _as_bool_scene_prop(
            getattr(event, "convertToAdditive", None),
            W2SCENE_ANIM_CLIP_DEFAULT_CONVERT_TO_ADDITIVE,
        )
    # CStorySceneEventAnimation entries with animationType=AAST_Additive are authored as scene
    # overlays. The raw imported Blender action is still an absolute pose, so the section
    # importer must convert it to a local additive delta before COMBINE layering.
    raw = getattr(event, "addConvertToAdditive", None)
    if raw is None:
        anim_type = _w2scene_event_animation_type(event)
        default = True if "ADDITIVE" in anim_type.upper() else W2SCENE_ANIM_CLIP_DEFAULT_CONVERT_TO_ADDITIVE
    else:
        default = W2SCENE_ANIM_CLIP_DEFAULT_CONVERT_TO_ADDITIVE
    return _as_bool_scene_prop(raw, default)


def _w2scene_event_needs_local_additive_conversion(event):
    return (
        _w2scene_event_uses_additive_body_blend(event)
        and _w2scene_event_convert_to_additive(event)
        and _w2scene_event_additive_type(event) == "AT_Local"
    )


def _w2scene_event_uses_motion_extraction(event):
    return _as_bool_scene_prop(getattr(event, "useMotionExtraction", None), False)


def _w2scene_event_uses_fake_motion(event):
    return _as_bool_scene_prop(getattr(event, "useFakeMotion", None), True)


def _w2scene_event_needs_additive_trajectory_extraction(event):
    return (
        W2SCENE_APPLY_MOTION_EXTRACTION_TO_POSE and
        _w2scene_event_needs_local_additive_conversion(event)
        and (
            _w2scene_event_uses_motion_extraction(event)
            or not _w2scene_event_uses_fake_motion(event)
        )
    )


def _w2scene_strip_blend_type(track_name, event=None):
    if _w2scene_track_is_mimic_layer(track_name):
        return 'COMBINE'
    if _w2scene_track_is_body_layer(track_name):
        if _w2scene_event_uses_additive_body_blend(event):
            return 'COMBINE'
        return 'REPLACE'
    return None


def _w2scene_track_uses_anim_clip_blends(track_name):
    current_name = str(track_name or "")
    return current_name.startswith((
        "cutscene_import_body",
        "cutscene_import_pose",
        "cutscene_import_mimic",
    ))


def _w2scene_strip_duration_frames(strip):
    try:
        return max(
            0.0,
            float(getattr(strip, "frame_end", 0.0) or 0.0)
            - float(getattr(strip, "frame_start", 0.0) or 0.0),
        )
    except Exception:
        return 0.0


def _w2scene_clamp01(value, default=1.0):
    try:
        value = float(value)
    except Exception:
        value = float(default)
    if not math.isfinite(value):
        value = float(default)
    return max(0.0, min(1.0, value))


def _w2scene_bezier_interpolation(value):
    value = _w2scene_clamp01(value, 0.0)
    return _w2scene_clamp01((-2.0 * value * value * value) + (3.0 * value * value), 0.0)


def _w2scene_anim_clip_blend_factor(local_frame, duration_frames, blend_in_frames, blend_out_frames):
    duration_frames = max(1e-6, float(duration_frames or 0.0))
    local_frame = max(0.0, min(float(local_frame or 0.0), duration_frames))
    blend_in_frames = max(0.0, float(blend_in_frames or 0.0))
    blend_out_frames = max(0.0, float(blend_out_frames or 0.0))
    blend_weight = 1.0
    if blend_in_frames > 1e-6 and local_frame <= blend_in_frames:
        blend_weight = local_frame / blend_in_frames
    elif blend_out_frames > 1e-6 and local_frame >= duration_frames - blend_out_frames:
        blend_weight = (duration_frames - local_frame) / blend_out_frames
    return _w2scene_bezier_interpolation(blend_weight)


def _w2scene_mimic_float_anim_weight_fn(base_weight, action_frame_start, action_frame_len, blend_in_frames, blend_out_frames):
    has_default_blend = float(blend_in_frames or 0.0) > 1e-6 or float(blend_out_frames or 0.0) > 1e-6
    if not has_default_blend:
        return None
    base_weight = _w2scene_clamp01(base_weight, 1.0)
    action_frame_start = float(action_frame_start or 0.0)
    action_frame_len = max(1e-6, float(action_frame_len or 0.0))

    def weight_at_action_frame(action_frame):
        local_frame = float(action_frame or 0.0) - action_frame_start
        blend_weight = _w2scene_anim_clip_blend_factor(
            local_frame,
            action_frame_len,
            blend_in_frames,
            blend_out_frames,
        )
        return base_weight * blend_weight

    return weight_at_action_frame


def _w2scene_action_float_prop(action, prop_name, default):
    if action is None:
        return float(default)
    try:
        value = action.get(prop_name, None)
    except Exception:
        value = None
    return float(_as_float(value, default))


def _snapshot_w2scene_strip(strip):
    data = {
        "name": str(getattr(strip, "name", "") or ""),
        "action": getattr(strip, "action", None),
        "action_slot": getattr(strip, "action_slot", None),
        "frame_start": float(getattr(strip, "frame_start", 0.0) or 0.0),
        "frame_end": float(getattr(strip, "frame_end", 0.0) or 0.0),
        "settings": {},
        "custom_props": {},
    }
    for attr in (
        "blend_type",
        "extrapolation",
        "influence",
        "mute",
        "use_auto_blend",
        "blend_in",
        "blend_out",
        "use_animated_influence",
        "repeat",
        "scale",
        "action_frame_start",
        "action_frame_end",
    ):
        if not hasattr(strip, attr):
            continue
        try:
            data["settings"][attr] = getattr(strip, attr)
        except Exception:
            pass
    try:
        for key in list(strip.keys()):
            data["custom_props"][key] = strip[key]
    except Exception:
        pass
    return data


def _restore_w2scene_strip_settings(snapshot, target_strip):
    for attr, value in (snapshot.get("settings") or {}).items():
        if not hasattr(target_strip, attr):
            continue
        try:
            setattr(target_strip, attr, value)
        except Exception:
            pass
    for key, value in (snapshot.get("custom_props") or {}).items():
        try:
            target_strip[key] = value
        except Exception:
            pass


def _apply_w2scene_strip_stack_settings(track_name, strip):
    try:
        strip.extrapolation = 'NOTHING'
    except Exception:
        pass
    action = None
    try:
        action = getattr(strip, "action", None)
    except Exception:
        action = None
    blend_type = None
    try:
        blend_type = strip.get(W2SCENE_NLA_STRIP_BLEND_TYPE_PROP, None)
    except Exception:
        blend_type = None
    if not blend_type:
        try:
            blend_type = action.get(W2SCENE_ACTION_BLEND_TYPE_PROP, None) if action is not None else None
        except Exception:
            blend_type = None
    if not blend_type:
        blend_type = _w2scene_strip_blend_type(track_name)
    if blend_type:
        try:
            strip.blend_type = blend_type
        except Exception:
            pass
    if _w2scene_track_uses_anim_clip_blends(track_name):
        blend_in_frames = _w2scene_action_float_prop(
            action,
            W2SCENE_ACTION_BLEND_IN_PROP,
            W2SCENE_ANIM_CLIP_DEFAULT_BLEND_IN * W2SCENE_SCENE_FPS,
        )
        blend_out_frames = _w2scene_action_float_prop(
            action,
            W2SCENE_ACTION_BLEND_OUT_PROP,
            W2SCENE_ANIM_CLIP_DEFAULT_BLEND_OUT * W2SCENE_SCENE_FPS,
        )
        strip_duration_frames = _w2scene_strip_duration_frames(strip)
        if hasattr(strip, 'use_auto_blend'):
            try:
                strip.use_auto_blend = False
            except Exception:
                pass
        try:
            strip.blend_in = (
                min(max(0.0, blend_in_frames), strip_duration_frames)
                if strip_duration_frames > 0.0
                else max(0.0, blend_in_frames)
            )
        except Exception:
            pass
        try:
            strip.blend_out = (
                min(max(0.0, blend_out_frames), strip_duration_frames)
                if strip_duration_frames > 0.0
                else max(0.0, blend_out_frames)
            )
        except Exception:
            pass


_W2SCENE_POSE_TRANSFORM_PROPS = {"location", "rotation_quaternion", "scale"}


def _ensure_w2scene_transform_fcurves(action, armature_obj, slot, bone_name, prop_name, curves, create_if_empty=False):
    return _ensure_pose_transform_fcurves(
        action,
        armature_obj,
        slot,
        bone_name,
        prop_name,
        curves,
        create_if_empty=create_if_empty,
        log=log,
    )


def _collect_w2scene_pose_bone_transform_curves(action, armature_obj, slot):
    return _collect_pose_transform_curves(
        action,
        armature_obj,
        slot,
        prop_names=_W2SCENE_POSE_TRANSFORM_PROPS,
        include_euler=False,
    )


def _w2scene_action_has_pose_transform_curves(action, armature_obj, slot=None):
    if action is None or armature_obj is None:
        return False
    try:
        if slot is None:
            slot = resolve_action_slot(action, target=armature_obj, ensure=True)
        curves_by_bone = _collect_w2scene_pose_bone_transform_curves(action, armature_obj, slot)
    except Exception:
        return False
    for curves_by_prop in curves_by_bone.values():
        for prop_name in _W2SCENE_POSE_TRANSFORM_PROPS:
            if curves_by_prop.get(prop_name):
                return True
    return False


def _apply_w2scene_root_orientation_to_action(action, armature_obj, event=None, section="", track_name="", strip=None):
    return import_scene_animation.apply_scene_root_orientation_to_action(
        action,
        armature_obj,
        event=event,
        section=section,
        track_name=track_name,
        strip=strip,
    )


def _w2scene_root_child_bone_names(armature_obj, root_bone_name):
    pose_bones = getattr(getattr(armature_obj, "pose", None), "bones", None)
    if not pose_bones or not root_bone_name:
        return []
    root_key = _w2scene_bone_base_name(root_bone_name)
    child_names = []
    for pose_bone in pose_bones:
        parent = getattr(pose_bone, "parent", None)
        if parent is not None and _w2scene_bone_base_name(getattr(parent, "name", "")) == root_key:
            child_names.append(str(pose_bone.name))
    return child_names


def _ensure_w2scene_bone_transform_group(curves_by_bone, bone_name):
    return curves_by_bone.setdefault(bone_name, {
        "location": {},
        "rotation_quaternion": {},
        "scale": {},
    })


def _ensure_w2scene_full_transform_group(action, armature_obj, slot, curves_by_bone, bone_name):
    curves_by_prop = _ensure_w2scene_bone_transform_group(curves_by_bone, bone_name)
    for prop_name in ("location", "rotation_quaternion", "scale"):
        _ensure_w2scene_transform_fcurves(
            action,
            armature_obj,
            slot,
            bone_name,
            prop_name,
            curves_by_prop[prop_name],
            create_if_empty=True,
        )
    return curves_by_prop


def _set_w2scene_action_scene_copy_marker(action):
    if action is None:
        return
    try:
        action[W2SCENE_ACTION_SCENE_COPY_PROP] = True
    except Exception:
        pass


def _copy_w2scene_strip_action(strip, armature_obj, suffix, event=None, section="", track_name=""):
    source_action = getattr(strip, "action", None)
    if source_action is None:
        return None
    try:
        action = source_action.copy()
    except Exception:
        log.debug("Could not copy scene strip action %s", getattr(source_action, "name", "<action>"), exc_info=True)
        return None
    try:
        action.name = f"{source_action.name}_{suffix}"
    except Exception:
        pass
    _set_w2scene_action_scene_copy_marker(action)
    try:
        strip.action = action
    except Exception:
        log.debug("Could not assign scene action copy to strip %s", getattr(strip, "name", "<strip>"), exc_info=True)
        return None
    try:
        bind_strip_action_slot(strip, resolve_action_slot(action, target=armature_obj, ensure=True))
    except Exception:
        pass
    import_scene_animation.warn_scene_animation_edit(
        "copied action for scene-only preprocessing",
        action=action,
        strip=strip,
        armature_obj=armature_obj,
        event=event,
        section=section,
        track_name=track_name,
        details={
            "sourceAction": getattr(source_action, "name", "<action>"),
            "newAction": getattr(action, "name", "<action>"),
            "reason": suffix,
        },
    )
    return action


def _set_w2scene_float_prop_ui(id_block, prop_name, default, soft_min=0.0, soft_max=1.0):
    if id_block is None:
        return
    try:
        if prop_name not in id_block:
            id_block[prop_name] = float(default)
        ui = id_block.id_properties_ui(prop_name)
        ui.update(min=float(soft_min), max=float(soft_max), soft_min=float(soft_min), soft_max=float(soft_max))
    except Exception:
        pass


def _set_w2scene_idprop(id_block, prop_name, value):
    if id_block is None or value is None:
        return
    try:
        if isinstance(value, bool):
            id_block[prop_name] = bool(value)
        elif isinstance(value, (int, float)):
            id_block[prop_name] = float(value)
        else:
            id_block[prop_name] = str(value)
    except Exception:
        pass


def _set_w2scene_import_metadata(id_blocks, metadata):
    metadata = metadata or {}
    for id_block in id_blocks or ():
        if id_block is None:
            continue
        for key, value in metadata.items():
            _set_w2scene_idprop(id_block, key, value)
        if W2SCENE_EVENT_WEIGHT_PROP in metadata:
            _set_w2scene_float_prop_ui(id_block, W2SCENE_EVENT_WEIGHT_PROP, metadata.get(W2SCENE_EVENT_WEIGHT_PROP, 1.0))
        if W2SCENE_ACTION_ADDITIVE_WEIGHT_PROP in metadata:
            _set_w2scene_float_prop_ui(id_block, W2SCENE_ACTION_ADDITIVE_WEIGHT_PROP, metadata.get(W2SCENE_ACTION_ADDITIVE_WEIGHT_PROP, 1.0))


def _remember_w2scene_event_guid_metadata(scene, metadata):
    if scene is None or not metadata:
        return
    guid = str(metadata.get("w3_scene_event_guid", "") or "").strip().lower()
    if not guid:
        return
    try:
        index = json.loads(str(scene.get(W2SCENE_EVENT_GUID_INDEX_PROP, "{}") or "{}"))
        if not isinstance(index, dict):
            index = {}
    except Exception:
        index = {}
    entry = {
        key: value
        for key, value in metadata.items()
        if key.startswith("w3_scene_") and isinstance(value, (str, int, float, bool))
    }
    index[guid] = entry
    index[guid[:8]] = entry
    try:
        scene[W2SCENE_EVENT_GUID_INDEX_PROP] = json.dumps(index, sort_keys=True)
    except Exception:
        pass


def _w2scene_event_strip_name(event):
    event_name = str(getattr(event, "eventName", "") or "").strip()
    anim_name = str(getattr(event, "animationName", "") or "").strip()
    guid = (get_w2scene_event_guid_string(event) or "")[:8]
    parts = [part for part in (event_name, anim_name, guid) if part]
    return _safe_nla_track_name("scene_event", *parts)[:96]


def _extract_w2scene_trajectory_from_action_pose(action, armature_obj, event=None, section="", track_name="", strip=None):
    slot = resolve_action_slot(action, target=armature_obj, ensure=True)
    curves_by_bone = _collect_w2scene_pose_bone_transform_curves(action, armature_obj, slot)
    trajectory_name = _find_w2scene_pose_bone_name(armature_obj, "Trajectory")
    root_name = _find_w2scene_pose_bone_name(armature_obj, "Root")
    trajectory_source_name = trajectory_name if trajectory_name in curves_by_bone else None
    if trajectory_source_name is None and root_name in curves_by_bone:
        # Some imported dialog anims have REDengine motion extraction copied onto
        # Root as a Blender fallback, while Trajectory itself has no keyed curves.
        trajectory_source_name = root_name
    if not trajectory_source_name:
        return False
    try:
        action[W2SCENE_ACTION_TRAJECTORY_SOURCE_PROP] = trajectory_source_name
    except Exception:
        pass

    root_child_names = _w2scene_root_child_bone_names(armature_obj, root_name)
    reference_name = _find_w2scene_pose_bone_name(armature_obj, "Reference")
    if reference_name and not root_child_names:
        root_child_names.append(reference_name)
    if trajectory_name and trajectory_name != root_name and trajectory_name not in root_child_names:
        root_child_names.append(trajectory_name)

    trajectory_curves = _ensure_w2scene_full_transform_group(
        action,
        armature_obj,
        slot,
        curves_by_bone,
        trajectory_source_name,
    )
    root_curves = None
    if root_name and root_name in curves_by_bone:
        root_curves = _ensure_w2scene_full_transform_group(
            action,
            armature_obj,
            slot,
            curves_by_bone,
            root_name,
        )
    child_curves = {
        bone_name: _ensure_w2scene_full_transform_group(action, armature_obj, slot, curves_by_bone, bone_name)
        for bone_name in root_child_names
    }

    frames = {0.0}
    for bone_name in [trajectory_source_name, root_name] + root_child_names:
        if not bone_name:
            continue
        for curves in (curves_by_bone.get(bone_name) or {}).values():
            for fcurve in curves.values():
                frames.update(_keyframe_frames(fcurve))
    if not frames:
        return False

    extracted_frames = []
    for frame in sorted(frames):
        trajectory_matrix = _transform_matrix_from_curve_groups(trajectory_curves, frame)
        trajectory_inverse = trajectory_matrix.inverted_safe()
        child_values = {}
        for bone_name, curves_by_prop in child_curves.items():
            extracted_matrix = trajectory_inverse @ _transform_matrix_from_curve_groups(curves_by_prop, frame)
            loc, rot, scale = extracted_matrix.decompose()
            rot.normalize()
            child_values[bone_name] = (loc.copy(), rot.copy(), scale.copy())
        extracted_frames.append((frame, child_values))

    identity_loc = Vector((0.0, 0.0, 0.0))
    identity_rot = Quaternion((1.0, 0.0, 0.0, 0.0))
    identity_scale = Vector((1.0, 1.0, 1.0))
    for frame, child_values in extracted_frames:
        if root_curves is not None:
            _set_w2scene_transform_group_values(root_curves, frame, identity_loc, quat_rot=identity_rot, scale=identity_scale)
        for bone_name, (loc, rot, scale) in child_values.items():
            _set_w2scene_transform_group_values(child_curves[bone_name], frame, loc, quat_rot=rot, scale=scale)

    for fcurve in iter_action_fcurves(action, target=armature_obj, slot=slot):
        try:
            fcurve.update()
        except Exception:
            pass
    import_scene_animation.warn_scene_animation_edit(
        "extracted trajectory motion into child bones",
        action=action,
        strip=strip,
        armature_obj=armature_obj,
        event=event,
        section=section,
        track_name=track_name,
        details={
            "trajectorySource": trajectory_source_name,
            "frames": len(extracted_frames),
            "children": ",".join(root_child_names),
        },
    )
    return True


def _convert_action_to_w2scene_local_additive(action, armature_obj, event=None, section="", track_name="", strip=None, reference_frame=0.0):
    slot = resolve_action_slot(action, target=armature_obj, ensure=True)
    curves_by_bone = _collect_w2scene_pose_bone_transform_curves(action, armature_obj, slot)
    changed = False
    changed_bones = set()
    try:
        reference_frame = float(reference_frame)
    except Exception:
        reference_frame = 0.0

    for bone_name, curves_by_prop in curves_by_bone.items():
        has_location = bool(curves_by_prop.get("location"))
        has_rotation = bool(curves_by_prop.get("rotation_quaternion"))
        has_scale = bool(curves_by_prop.get("scale"))
        if not (has_location or has_rotation or has_scale):
            continue

        frames = {reference_frame}
        for curves in curves_by_prop.values():
            for fcurve in curves.values():
                frames.update(_keyframe_frames(fcurve))
        if not frames:
            continue

        for prop_name in ("location", "rotation_quaternion", "scale"):
            _ensure_w2scene_transform_fcurves(action, armature_obj, slot, bone_name, prop_name, curves_by_prop[prop_name])

        reference_matrix = _transform_matrix_from_curve_groups(curves_by_prop, reference_frame)
        reference_inverse = reference_matrix.inverted_safe()
        frame_deltas = []

        for frame in sorted(frames):
            delta_matrix = reference_inverse @ _transform_matrix_from_curve_groups(curves_by_prop, frame)
            delta_loc, delta_rot, delta_scale = delta_matrix.decompose()
            delta_rot.normalize()
            frame_deltas.append((frame, delta_loc.copy(), delta_rot.copy(), delta_scale.copy()))

        for frame, delta_loc, delta_rot, delta_scale in frame_deltas:
            if has_location:
                for index, value in enumerate(delta_loc):
                    fcurve = curves_by_prop["location"].get(index)
                    if fcurve is not None:
                        _set_fcurve_value_at_frame(fcurve, frame, float(value))
            if has_rotation:
                for index, value in enumerate(delta_rot):
                    fcurve = curves_by_prop["rotation_quaternion"].get(index)
                    if fcurve is not None:
                        _set_fcurve_value_at_frame(fcurve, frame, float(value))
            if has_scale:
                for index, value in enumerate(delta_scale):
                    fcurve = curves_by_prop["scale"].get(index)
                    if fcurve is not None:
                        _set_fcurve_value_at_frame(fcurve, frame, float(value))
            changed = True
            changed_bones.add(bone_name)

    if changed:
        for fcurve in iter_action_fcurves(action, target=armature_obj, slot=slot):
            try:
                fcurve.update()
            except Exception:
                pass
        import_scene_animation.warn_scene_animation_edit(
            "converted action to local additive curves",
            action=action,
            strip=strip,
            armature_obj=armature_obj,
            event=event,
            section=section,
            track_name=track_name,
            details={
                "bones": len(changed_bones),
                "referenceFrame": round(float(reference_frame), 3),
            },
        )
    return changed


def _prepare_w2scene_local_additive_strip_action(strip, armature_obj, event, section="", track_name=""):
    if not _w2scene_event_needs_local_additive_conversion(event):
        return
    source_action = getattr(strip, "action", None)
    if source_action is None:
        return
    try:
        if source_action.get(W2SCENE_ACTION_ADDITIVE_CONVERT_PROP, False):
            return
    except Exception:
        pass

    action = _copy_w2scene_strip_action(strip, armature_obj, "local_additive", event=event, section=section, track_name=track_name)
    if action is None:
        return

    trajectory_extracted = False
    if _w2scene_event_needs_additive_trajectory_extraction(event):
        trajectory_extracted = _extract_w2scene_trajectory_from_action_pose(
            action,
            armature_obj,
            event=event,
            section=section,
            track_name=track_name,
            strip=strip,
        )
        try:
            action[W2SCENE_ACTION_TRAJECTORY_EXTRACTED_PROP] = bool(trajectory_extracted)
        except Exception:
            pass

    if _convert_action_to_w2scene_local_additive(
        action,
        armature_obj,
        event=event,
        section=section,
        track_name=track_name,
        strip=strip,
    ):
        try:
            action[W2SCENE_ACTION_ADDITIVE_CONVERT_PROP] = True
        except Exception:
            pass
    elif trajectory_extracted:
        try:
            action[W2SCENE_ACTION_ADDITIVE_CONVERT_PROP] = True
        except Exception:
            pass


def _prepare_w2scene_mimic_overlay_strip_action(strip, armature_obj, event=None, section="", track_name="", reference_frame=0.0):
    source_action = getattr(strip, "action", None)
    if source_action is None:
        return False
    try:
        if source_action.get(W2SCENE_ACTION_ADDITIVE_CONVERT_PROP, False):
            return False
    except Exception:
        pass
    if not _w2scene_action_has_pose_transform_curves(source_action, armature_obj, getattr(strip, "action_slot", None)):
        return False

    action = _copy_w2scene_strip_action(
        strip,
        armature_obj,
        "mimic_local_additive",
        event=event,
        section=section,
        track_name=track_name,
    )
    if action is None:
        return False

    changed = _convert_action_to_w2scene_local_additive(
        action,
        armature_obj,
        event=event,
        section=section,
        track_name=track_name,
        strip=strip,
        reference_frame=reference_frame,
    )
    if changed:
        try:
            action[W2SCENE_ACTION_ADDITIVE_CONVERT_PROP] = True
            action["w3_scene_mimic_local_additive"] = True
            action["w3_scene_mimic_local_additive_reference_frame"] = float(reference_frame)
        except Exception:
            pass
    return changed


def _w2scene_section_track_rank(track_name, strip_entries=None):
    base_name = str(track_name or "").split(".", 1)[0]
    if base_name == "ScenePlacement":
        return 0.0
    if base_name == W2SCENE_DIALOGSET_IDLE_TRACK_NAME:
        return 1.0
    if base_name.startswith(W2SCENE_DIALOGSET_IDLE_CHANGE_TRACK_PREFIX):
        return 1.1
    if _w2scene_track_is_body_layer(track_name):
        for strip_entry in strip_entries or []:
            blend_type = (strip_entry.get("settings") or {}).get("blend_type")
            if blend_type in {'ADD', 'COMBINE'}:
                return 2.0
        return 1.2
    if _w2scene_track_is_mimic_layer(track_name):
        return 3.0
    if base_name in {"SceneMotionExtraction", "SceneVisibility"}:
        return 4.0
    return 5.0


def _sort_w2scene_nla_tracks_for_actor(target_obj):
    anim_data = getattr(target_obj, "animation_data", None)
    tracks = getattr(anim_data, "nla_tracks", None)
    if tracks is None:
        return 0
    section_tracks = [
        track
        for track in list(tracks)
        if _nla_track_name_matches(
            getattr(track, "name", ""),
            track_names=W2SCENE_SECTION_NLA_TRACK_NAMES,
            track_prefixes=W2SCENE_SECTION_NLA_TRACK_PREFIXES,
        )
    ]
    for track in section_tracks:
        track_name = str(getattr(track, "name", "") or "")
        for strip in list(getattr(track, "strips", []) or []):
            _apply_w2scene_strip_stack_settings(track_name, strip)
    if len(section_tracks) < 2:
        return 0

    original_index = {id(track): index for index, track in enumerate(list(tracks))}
    track_entries = []
    for track in section_tracks:
        track_name = str(getattr(track, "name", "") or "")
        track_settings = {}
        for attr in ("mute", "lock", "select", "is_solo"):
            if not hasattr(track, attr):
                continue
            try:
                track_settings[attr] = getattr(track, attr)
            except Exception:
                pass
        strip_entries = []
        for strip in list(getattr(track, "strips", []) or []):
            action = getattr(strip, "action", None)
            if action is None:
                continue
            snapshot = _snapshot_w2scene_strip(strip)
            strip_entries.append(snapshot)
        track_entries.append((track_name, track, track_settings, strip_entries))
    track_entries.sort(
        key=lambda item: (
            _w2scene_section_track_rank(item[0], item[3]),
            original_index.get(id(item[1]), 0),
        )
    )

    try:
        for _track_name, track, _track_settings, _strip_entries in track_entries:
            tracks.remove(track)

        previous_track = None
        rebuilt = 0
        for track_name, _old_track, track_settings, strip_entries in track_entries:
            new_track = tracks.new(prev=previous_track) if previous_track is not None else tracks.new()
            new_track.name = track_name
            for attr, value in track_settings.items():
                if not hasattr(new_track, attr):
                    continue
                try:
                    setattr(new_track, attr, value)
                except Exception:
                    pass
            for snapshot in sorted(strip_entries, key=lambda item: float(item.get("frame_start", 0.0) or 0.0)):
                action = snapshot.get("action")
                if action is None:
                    continue
                frame_start = float(snapshot.get("frame_start", 0.0) or 0.0)
                frame_end = float(snapshot.get("frame_end", frame_start + 1.0) or (frame_start + 1.0))
                strip_name = str(snapshot.get("name") or getattr(action, "name", "") or track_name)
                new_strip = new_track.strips.new(strip_name, int(round(frame_start)), action)
                new_strip.frame_start = frame_start
                new_strip.frame_end = max(frame_start + 1.0, frame_end)
                _restore_w2scene_strip_settings(snapshot, new_strip)
                _apply_w2scene_strip_stack_settings(track_name, new_strip)
                try:
                    action_slot = snapshot.get("action_slot")
                    if action_slot is not None:
                        bind_strip_action_slot(new_strip, action_slot)
                    else:
                        bind_strip_action_slot(new_strip, resolve_action_slot(action, target=target_obj, ensure=True))
                except Exception:
                    pass
            previous_track = new_track
            rebuilt += 1
        return rebuilt
    except Exception:
        log.debug("Could not rebuild .w2scene NLA tracks for %s", getattr(target_obj, "name", "<unknown>"), exc_info=True)
    return 0


def _remove_orphan_action(action):
    if action is not None and getattr(action, "users", 0) == 0:
        try:
            bpy.data.actions.remove(action)
        except Exception:
            pass


def clear_nla_tracks(target_obj, track_names=(), track_prefixes=()):
    anim_data = getattr(target_obj, "animation_data", None) if target_obj is not None else None
    if anim_data is None:
        return 0
    removed = 0
    for track in list(anim_data.nla_tracks):
        if not _nla_track_name_matches(getattr(track, "name", ""), track_names=track_names, track_prefixes=track_prefixes):
            continue
        actions = []
        for strip in list(track.strips):
            action = getattr(strip, "action", None)
            if action is not None:
                actions.append(action)
            try:
                track.strips.remove(strip)
                removed += 1
            except Exception:
                log.debug("Could not remove strip %s from %s", getattr(strip, "name", ""), getattr(track, "name", ""), exc_info=True)
        try:
            anim_data.nla_tracks.remove(track)
        except Exception:
            pass
        for action in actions:
            _remove_orphan_action(action)
    return removed


def clear_w2scene_actor_section_nla(context, actor_obj):
    removed = clear_nla_tracks(
        actor_obj,
        track_names=W2SCENE_SECTION_NLA_TRACK_NAMES,
        track_prefixes=W2SCENE_SECTION_NLA_TRACK_PREFIXES,
    )
    if actor_obj is not None and getattr(actor_obj, "type", None) == 'ARMATURE':
        removed += clear_actor_lookat_constraints(actor_obj)
    mimic_name = str(actor_obj.get("mimicFace", "") or "") if actor_obj is not None else ""
    mimic_armature = bpy.data.objects.get(mimic_name) if mimic_name else None
    if mimic_armature is not None and mimic_armature is not actor_obj:
        removed += clear_nla_tracks(
            mimic_armature,
            track_names=W2SCENE_SECTION_NLA_TRACK_NAMES,
            track_prefixes=W2SCENE_SECTION_NLA_TRACK_PREFIXES,
        )
    try:
        face_meshes = _find_face_meshes(context, actor_obj)
    except Exception:
        face_meshes = []
    for mesh_obj in face_meshes:
        shape_keys = getattr(getattr(mesh_obj, "data", None), "shape_keys", None)
        removed += clear_nla_tracks(
            shape_keys,
            track_names=W2SCENE_SECTION_NLA_TRACK_NAMES,
            track_prefixes=W2SCENE_SECTION_NLA_TRACK_PREFIXES,
        )
    return removed


def clear_w2scene_prop_section_nla(prop_obj):
    return clear_nla_tracks(prop_obj, track_names=("ScenePlacement", "SceneVisibility"))


def reset_w2scene_prop_visibility(prop_obj):
    if prop_obj is None:
        return
    prop_obj.hide_viewport = False
    prop_obj.hide_render = False


def clear_w2scene_story_scene_actor_nla(context, story_scene, reset_actors=False):
    if story_scene is None:
        return 0
    removed = clear_lookat_static_empties()
    for actor_ref in getattr(getattr(story_scene, "sceneTemplates", None), "value", []) or []:
        try:
            actor_template = story_scene.chunksRef[actor_ref - 1]
            actor = w3_types.CStorySceneActor(actor_template)
            actor_obj = check_if_actor_already_in_scene(actor.entityTemplate)
        except Exception:
            actor_obj = None
        if actor_obj:
            removed += clear_w2scene_actor_section_nla(context, actor_obj)
            if reset_actors:
                try:
                    reset_transforms(actor_obj)
                except Exception:
                    log.debug("Could not reset .w2scene actor %s", getattr(actor_obj, "name", ""), exc_info=True)
    return removed


def clear_w2scene_story_scene_prop_nla(story_scene, reset_props=False):
    if story_scene is None:
        return 0
    removed = 0
    for prop_ref in getattr(getattr(story_scene, "sceneProps", None), "value", []) or []:
        try:
            prop_chunk = story_scene.chunksRef[prop_ref - 1]
            prop = w3_types.CStorySceneProp(prop_chunk)
            prop_obj = _find_scene_prop_object(getattr(prop, "id", ""))
        except Exception:
            prop_obj = None
        if prop_obj:
            removed += clear_w2scene_prop_section_nla(prop_obj)
            if reset_props:
                try:
                    reset_transforms(prop_obj)
                    reset_w2scene_prop_visibility(prop_obj)
                except Exception:
                    log.debug("Could not reset .w2scene prop %s", getattr(prop_obj, "name", ""), exc_info=True)
    return removed


def clear_w2scene_camera_runtime(context, scene_cam_obj=None, clear_markers=True):
    scene = getattr(context, "scene", None)
    removed = 0
    if scene_cam_obj is None:
        scene_cam_obj = check_if_actor_already_in_scene(W2SCENE_CAMERA_ENTITY_PATH)
    if scene_cam_obj:
        removed += clear_nla_tracks(
            scene_cam_obj,
            track_names=W2SCENE_CAMERA_NLA_TRACK_NAMES,
            track_prefixes=W2SCENE_CAMERA_NLA_TRACK_PREFIXES,
        )
    if clear_markers and scene is not None:
        for marker in list(scene.timeline_markers):
            try:
                scene.timeline_markers.remove(marker)
                removed += 1
            except Exception:
                pass
    return removed


def clear_w2scene_runtime_state(context, story_scene=None, reset_actors=False):
    scene = getattr(context, "scene", None)
    removed = {
        "audio": 0,
        "actor_nla": 0,
        "prop_nla": 0,
        "camera": 0,
        "debug_empties": 0,
    }
    if scene is not None:
        removed["audio"] = clear_w2scene_section_audio(scene)
    removed["debug_empties"] = clear_w2scene_debug_empties(context)
    removed["actor_nla"] = clear_w2scene_story_scene_actor_nla(context, story_scene, reset_actors=reset_actors)
    removed["prop_nla"] = clear_w2scene_story_scene_prop_nla(story_scene, reset_props=reset_actors)
    removed["camera"] = clear_w2scene_camera_runtime(context)
    return removed


def reset_transforms(new_obj):
    x, y, z = (radians(0), radians(0), radians(0))
    mat = Euler((x, y, z)).to_matrix().to_4x4()
    new_obj.matrix_world = mat
    new_obj.matrix_local = mat
    new_obj.matrix_basis = mat

    new_obj.location[0] = 0
    new_obj.location[1] = 0
    new_obj.location[2] = 0
    new_obj.scale[0] = 1
    new_obj.scale[1] = 1
    new_obj.scale[2] = 1


def _w2scene_object_transform_values(obj):
    prop_rot_map = {'QUATERNION': 'rotation_quaternion', 'AXIS_ANGLE': 'rotation_axis_angle'}
    data_path_rot = prop_rot_map.get(obj.rotation_mode, 'rotation_euler')
    return (
        tuple(float(value) for value in obj.location),
        tuple(float(value) for value in getattr(obj, data_path_rot)),
        tuple(float(value) for value in obj.scale),
        data_path_rot,
    )


class BlankEngineTransform:
    def __init__(self):
        self.X = 0.0
        self.Y = 0.0
        self.Z = 0.0
        self.Pitch = 0.0
        self.Yaw = 0.0
        self.Roll = 0.0
        self.Scale_x = 1.0
        self.Scale_y = 1.0
        self.Scale_z = 1.0
import time


def _safe_w2scene_debug_name(prefix, *parts, max_len=96):
    raw_parts = [str(part or "").strip() for part in parts if str(part or "").strip()]
    raw = "_".join(raw_parts)
    safe = "".join(char if char.isalnum() or char in "._-" else "_" for char in raw)
    safe = "_".join(part for part in safe.split("_") if part)
    if not safe:
        safe = "marker"
    max_safe_len = max(1, int(max_len) - len(prefix) - 1)
    if len(safe) > max_safe_len:
        tail = safe[-10:] if max_safe_len > 14 else ""
        if tail:
            head_len = max_safe_len - len(tail) - 1
            safe = f"{safe[:head_len].rstrip('_')}_{tail}"
        else:
            safe = safe[:max_safe_len]
    return f"{prefix}_{safe}"


def _ensure_w2scene_debug_collection(context):
    scene = getattr(context, "scene", None) or bpy.context.scene
    collection = bpy.data.collections.get(W2SCENE_DEBUG_COLLECTION_NAME)
    if collection is None:
        collection = bpy.data.collections.new(W2SCENE_DEBUG_COLLECTION_NAME)
    linked = False
    try:
        linked = scene.collection.children.get(collection.name) is not None
    except Exception:
        linked = False
    if not linked:
        try:
            scene.collection.children.link(collection)
        except Exception:
            pass
    return collection


def _w2scene_debug_markers_enabled(context=None):
    try:
        scene = getattr(context, "scene", None) if context is not None else bpy.context.scene
        return bool(getattr(scene, "witcher_w2scene_create_debug_markers", True))
    except Exception:
        return True


def clear_w2scene_debug_empties(context=None):
    removed = 0
    for obj in list(bpy.data.objects):
        try:
            is_debug = bool(obj.get(W2SCENE_DEBUG_EMPTY_PROP, False))
        except Exception:
            is_debug = False
        if not is_debug:
            continue
        try:
            bpy.data.objects.remove(obj, do_unlink=True)
            removed += 1
        except Exception:
            log.debug("Could not remove .w2scene debug empty %s", getattr(obj, "name", ""), exc_info=True)
    collection = bpy.data.collections.get(W2SCENE_DEBUG_COLLECTION_NAME)
    if collection is not None and len(collection.objects) == 0:
        try:
            scene = getattr(context, "scene", None) if context is not None else bpy.context.scene
            if scene is not None and scene.collection.children.get(collection.name) is not None:
                scene.collection.children.unlink(collection)
        except Exception:
            pass
        try:
            bpy.data.collections.remove(collection)
        except Exception:
            pass
    return removed


def _create_w2scene_debug_empty(
    context,
    name,
    engine_transform,
    *,
    from_object=None,
    display_type='ARROWS',
    display_size=0.35,
    color=(1.0, 0.55, 0.08, 1.0),
    metadata=None,
):
    if not _w2scene_debug_markers_enabled(context):
        return None
    collection = _ensure_w2scene_debug_collection(context)
    empty = bpy.data.objects.new(name, None)
    try:
        empty.empty_display_type = display_type
    except Exception:
        empty.empty_display_type = 'PLAIN_AXES'
    empty.empty_display_size = float(display_size)
    empty.show_name = True
    empty.show_in_front = True
    try:
        empty.color = color
    except Exception:
        pass
    collection.objects.link(empty)
    try:
        set_blender_object_transform(empty, engine_transform or BlankEngineTransform(), from_this_object=from_object)
    except Exception:
        log.debug("Could not set transform on .w2scene debug empty %s", name, exc_info=True)
    try:
        empty[W2SCENE_DEBUG_EMPTY_PROP] = True
        for key, value in (metadata or {}).items():
            empty[key] = value
    except Exception:
        pass
    return empty


def _find_scene_prop_object(prop_id):
    prop_id = str(prop_id or "").strip()
    if not prop_id:
        return None
    for obj in bpy.context.scene.objects:
        if str(obj.get("witcher_w2scene_prop_id", "") or "").strip() == prop_id:
            return obj
    return None


def _resolve_w2scene_template_path(template_path, scene_filepath=""):
    template_path = str(template_path or "").strip().replace("/", "\\")
    if not template_path:
        return ""
    if os.path.isabs(template_path):
        return template_path

    if _scene_linked_assets_prefer_bundles():
        # User has opted out of REDkit priority for linked assets: skip the
        # scene-adjacent directory walk (which would resolve to REDkit paths)
        # and resolve straight from vanilla bundles.
        with vanilla_only_repo_context():
            return repo_file(template_path)

    source_path = Path(str(scene_filepath or ""))
    if source_path:
        for parent in source_path.parents:
            candidate = parent / template_path
            if candidate.exists():
                return str(candidate)
    with redkit_repo_context(scene_filepath):
        return repo_file(template_path)


def _create_scene_prop_empty(prop_id, template_path, context):
    name = str(prop_id or "").strip() or Path(str(template_path or "SceneProp")).stem
    prop_obj = bpy.data.objects.new(name, None)
    prop_obj.empty_display_type = 'PLAIN_AXES'
    prop_obj.empty_display_size = 0.25
    collection = getattr(context, "collection", None) or getattr(getattr(context, "scene", None), "collection", None)
    if collection is not None:
        collection.objects.link(prop_obj)
    else:
        bpy.context.scene.collection.objects.link(prop_obj)
    return prop_obj


def _import_scene_prop_object(prop, context, scene_filepath=""):
    prop_id = str(getattr(prop, "id", "") or "").strip()
    template_path = str(getattr(prop, "entityTemplate", "") or "").strip()
    prop_obj = _find_scene_prop_object(prop_id)
    if prop_obj is not None:
        return prop_obj
    if not template_path:
        log.warning("Skipping scene prop %s; no entity template", prop_id or "<unnamed>")
        return None

    before_ids = {id(obj) for obj in bpy.data.objects}
    resolved_path = _resolve_w2scene_template_path(template_path, scene_filepath)
    try:
        prop_obj = import_entity.import_ent_template(resolved_path)
    except Exception:
        log.warning("Failed to import scene prop %s from %s", prop_id or "<unnamed>", resolved_path or template_path, exc_info=True)
        prop_obj = None

    if prop_obj is None:
        new_objects = [obj for obj in bpy.data.objects if id(obj) not in before_ids]
        new_ids = {id(obj) for obj in new_objects}
        root_candidates = [
            obj for obj in new_objects
            if obj.parent is None or id(obj.parent) not in new_ids
        ]
        prop_obj = (root_candidates or new_objects or [None])[0]

    if prop_obj is None:
        log.warning(
            "Scene prop %s produced no importable object from %s; creating placement empty",
            prop_id or "<unnamed>",
            resolved_path or template_path,
        )
        prop_obj = _create_scene_prop_empty(prop_id, template_path, context)

    prop_obj["witcher_w2scene_prop_id"] = prop_id
    prop_obj["witcher_w2scene_prop_template"] = template_path
    prop_obj["witcher_scene_item_type"] = "PROP"
    try:
        child_objects = list(prop_obj.children_recursive)
    except Exception:
        child_objects = []
    for child in child_objects:
        child["witcher_w2scene_prop_id"] = prop_id
        child["witcher_w2scene_prop_template"] = template_path
        child["witcher_scene_item_type"] = "PROP"
    return prop_obj

def _cname_index_to_string(index_obj):
    if index_obj is None:
        return ""
    value = getattr(index_obj, "String", None)
    if value:
        return str(value)
    if hasattr(index_obj, "ToString"):
        try:
            return str(index_obj.ToString() or "")
        except Exception:
            return ""
    return ""


def _cname_array_values(prop):
    if prop is None:
        return []
    if isinstance(prop, (list, tuple)):
        return [str(value or "").strip() for value in prop if str(value or "").strip()]
    values = []
    for index_obj in getattr(prop, "Index", []) or []:
        value = _cname_index_to_string(index_obj).strip()
        if value:
            values.append(value)
    return values


def _scene_actor_preferred_appearance(actor):
    values = _cname_array_values(getattr(actor, "appearanceFilter", None))
    return values[0] if values else ""


def _transform_real(transform, name, default=0.0):
    if transform is None:
        return default
    value = transform.get(name, default) if isinstance(transform, dict) else getattr(transform, name, default)
    try:
        return float(value)
    except Exception:
        return default


def _engine_transform_matrix(engine_transform, from_this_object=None):
    base_matrix = from_this_object.matrix_world.copy() if from_this_object else Matrix.Identity(4)
    yaw = _transform_real(engine_transform, "Yaw", 0.0)
    pitch = _transform_real(engine_transform, "Pitch", 0.0)
    roll = _transform_real(engine_transform, "Roll", 0.0)
    loc_x = _transform_real(engine_transform, "X", 0.0)
    loc_y = _transform_real(engine_transform, "Y", 0.0)
    loc_z = _transform_real(engine_transform, "Z", 0.0)
    if yaw == 0.0 and pitch == 0.0 and roll == 0.0:
        local_matrix = Matrix.Identity(4)
    else:
        local_matrix = Euler((radians(yaw), radians(pitch), radians(roll)), 'YXZ').to_matrix().to_4x4()
    local_matrix.translation = (loc_x, loc_y, loc_z)
    return base_matrix @ local_matrix


def _pose_bone_world_matrix(armature_obj, bone_name):
    pose = getattr(armature_obj, "pose", None) if armature_obj is not None else None
    pose_bones = getattr(pose, "bones", None) if pose is not None else None
    pose_bone = pose_bones.get(bone_name) if pose_bones is not None else None
    if pose_bone is None:
        return None
    return armature_obj.matrix_world @ pose_bone.matrix


def _camera_preview_offset_matrix(camera_armature):
    edit_world = _pose_bone_world_matrix(camera_armature, CAMERA_EDIT_BONE)
    preview_camera = find_camera_preview_object(camera_armature)
    if edit_world is None or preview_camera is None:
        return Matrix.Identity(4)
    return edit_world.inverted() @ preview_camera.matrix_world


def _camera_matrix_to_edit_bone_matrix(camera_armature, camera_matrix, preview_offset):
    desired_edit_world = camera_matrix @ preview_offset.inverted()
    return camera_armature.matrix_world.inverted() @ desired_edit_world


# CStorySceneEventLookAt support: live constraints layered on top of body NLA.
# The Damped Track constraint runs after pose evaluation each frame, so the
# head bone tracks its target naturally during scrubbing/playback.
LOOKAT_CONSTRAINT_PREFIX = "w3_lookat_"
LOOKAT_STATIC_EMPTY_PREFIX = "w3_lookat_static_"
LOOKAT_ANCHOR_PREFIX = "w3_lookat_anchor_"
# W3 head bone local frame: face direction is the bone's -X axis on this rig.
LOOKAT_TRACK_AXIS = 'TRACK_NEGATIVE_X'
LOOKAT_LEVEL_BONE = {
    "LL_Body": "head",
    "LL_Head": "head",
    "LL_Eyes": "head",
    "LL_Null": "head",
}
LOOKAT_DEFAULT_BONE = "head"
LOOKAT_TARGET_SUBTARGET = "head"
LOOKAT_LIMIT_PITCH_DEG = 10.0 #setting to 10 seems to match what it looks like in editor
LOOKAT_LIMIT_YAW_DEG = 90.0
LOOKAT_LIMIT_ROLL_DEG = 30.0
LOOKAT_LIMIT_CONSTRAINT_SUFFIX = "_limit"


def _lookat_actor_bone(level):
    return LOOKAT_LEVEL_BONE.get(str(level or ""), LOOKAT_DEFAULT_BONE)


def _enum_string(value, default=""):
    """Extract a string from a CR2W enum property.

    Enum-typed properties come through as PROPERTY objects whose .Value is a
    CEnum with a .String attr. Plain strings (from primitives) pass through.
    """
    if value is None:
        return default
    if isinstance(value, str):
        return value
    inner = getattr(value, "Value", None)
    if inner is not None:
        s = getattr(inner, "String", None)
        if s:
            return str(s)
        if hasattr(inner, "ToString"):
            try:
                return str(inner.ToString())
            except Exception:
                pass
    s = getattr(value, "String", None)
    if s:
        return str(s)
    if hasattr(value, "ToString"):
        try:
            return str(value.ToString())
        except Exception:
            pass
    return default


def _cname_text(value):
    text = _enum_string(value, "") if value is not None else ""
    if not text and value is not None:
        text = str(value or "")
    text = str(text or "").strip()
    if text.upper() in {"NONE", "ZERO", "CNAME::NONE"}:
        return ""
    return text


def _force_fcurve_interp(armature_obj, data_path, frame, interp):
    anim = getattr(armature_obj, "animation_data", None)
    action = getattr(anim, "action", None) if anim is not None else None
    if action is None:
        return
    for fc in iter_action_fcurves(action, target=armature_obj):
        if fc.data_path != data_path:
            continue
        for kp in fc.keyframe_points:
            if abs(kp.co[0] - frame) < 0.5:
                kp.interpolation = interp
                kp.handle_left_type = 'VECTOR'
                kp.handle_right_type = 'VECTOR'


def _keyframe_constraint_influence(armature_obj, bone_name, constraint_name, frame, value, interp='CONSTANT'):
    pose_bone = armature_obj.pose.bones.get(bone_name) if getattr(armature_obj, "pose", None) else None
    if pose_bone is None:
        return False
    constraint = pose_bone.constraints.get(constraint_name)
    if constraint is None:
        return False
    constraint.influence = float(value)
    data_path = f'pose.bones["{bone_name}"].constraints["{constraint_name}"].influence'
    armature_obj.keyframe_insert(data_path=data_path, frame=int(frame))
    _force_fcurve_interp(armature_obj, data_path, int(frame), interp)
    return True


def _get_or_create_lookat_anchor(target_armature, target_bone_name):
    if target_armature is None or not target_bone_name:
        return None
    pose = getattr(target_armature, "pose", None)
    pose_bone = pose.bones.get(target_bone_name) if pose else None
    if pose_bone is None or pose_bone.parent is None:
        return None
    parent_bone_name = pose_bone.parent.name
    anchor_name = f"{LOOKAT_ANCHOR_PREFIX}{target_armature.name}_{target_bone_name}"
    anchor = bpy.data.objects.get(anchor_name)
    if anchor is None:
        anchor = bpy.data.objects.new(anchor_name, None)
        anchor.empty_display_type = 'PLAIN_AXES'
        anchor.empty_display_size = 0.05
        try:
            bpy.context.collection.objects.link(anchor)
        except Exception:
            bpy.context.scene.collection.objects.link(anchor)
        anchor.hide_viewport = True
    anchor["witcher_w2scene_lookat_anchor"] = True
    anchor["witcher_w2scene_lookat_anchor_armature"] = target_armature.name
    anchor["witcher_w2scene_lookat_anchor_bone"] = target_bone_name
    if (anchor.parent is not target_armature
            or anchor.parent_type != 'BONE'
            or anchor.parent_bone != parent_bone_name):
        anchor.parent = target_armature
        anchor.parent_type = 'BONE'
        anchor.parent_bone = parent_bone_name
        anchor.matrix_parent_inverse = Matrix.Identity(4)
        anchor.matrix_basis = Matrix.Identity(4)
    return anchor


def _add_lookat_damped_track(actor_obj, bone_name, target_obj, subtarget, constraint_name):
    pose = getattr(actor_obj, "pose", None)
    pose_bone = pose.bones.get(bone_name) if pose else None
    if pose_bone is None:
        log.warning("LookAt: actor '%s' has no '%s' bone; skipping", getattr(actor_obj, "name", "?"), bone_name)
        return None

    # Avoid mutual head-to-head Damped Track cycles by retargeting onto an
    # anchor empty parented to the target bone's parent (see
    # _get_or_create_lookat_anchor).  Self-look (same armature) does not need
    # this - a bone tracking another bone on the same rig is a normal forward
    # dependency.  Static-point lookats already use a parented empty.
    if (
        getattr(target_obj, "type", None) == 'ARMATURE'
        and subtarget
        and target_obj is not actor_obj
    ):
        anchor = _get_or_create_lookat_anchor(target_obj, subtarget)
        if anchor is not None:
            target_obj = anchor
            subtarget = ""

    existing = pose_bone.constraints.get(constraint_name)
    if existing is not None:
        return existing
    constraint = pose_bone.constraints.new('DAMPED_TRACK')
    constraint.name = constraint_name
    constraint.target = target_obj
    constraint.subtarget = subtarget or ""
    constraint.track_axis = LOOKAT_TRACK_AXIS
    constraint.influence = 0.0

    # Companion Limit Rotation clamps the head bone to a realistic neck range.
    limit_name = constraint_name + LOOKAT_LIMIT_CONSTRAINT_SUFFIX
    if pose_bone.constraints.get(limit_name) is None:
        limit = pose_bone.constraints.new('LIMIT_ROTATION')
        limit.name = limit_name
        limit.use_limit_x = True
        limit.use_limit_y = True
        limit.use_limit_z = True
        # Track axis is -X (face forward), so the head bone's local frame:
        #   Y = up the skull  -> rotation around Y = yaw (left/right)
        #   Z = side of head  -> rotation around Z = pitch (up/down)
        #   X = forward axis  -> rotation around X = roll (head tilt)
        limit.min_x = math.radians(-LOOKAT_LIMIT_ROLL_DEG)
        limit.max_x = math.radians(LOOKAT_LIMIT_ROLL_DEG)
        limit.min_y = math.radians(-LOOKAT_LIMIT_YAW_DEG)
        limit.max_y = math.radians(LOOKAT_LIMIT_YAW_DEG)
        limit.min_z = math.radians(-LOOKAT_LIMIT_PITCH_DEG)
        limit.max_z = math.radians(LOOKAT_LIMIT_PITCH_DEG)
        limit.owner_space = 'LOCAL'
        limit.influence = 1.0
    return constraint


def _create_lookat_static_empty(name, local_pos, parent_obj=None, event_guid=None):
    empty = bpy.data.objects.get(name)
    if empty is None:
        empty = bpy.data.objects.new(name, None)
        empty.empty_display_type = 'SPHERE'
        empty.empty_display_size = 0.1
        bpy.context.collection.objects.link(empty)
    empty["witcher_w2scene_lookat_static"] = True
    if event_guid:
        empty["witcher_w2scene_lookat_guid"] = str(event_guid)
    if parent_obj is not None:
        empty.parent = parent_obj
        empty.matrix_parent_inverse = Matrix.Identity(4)
    empty.location = (float(local_pos[0]), float(local_pos[1]), float(local_pos[2]))
    return empty


def clear_actor_lookat_constraints(actor_obj):
    pose = getattr(actor_obj, "pose", None)
    if pose is None:
        return 0
    removed = 0
    action = None
    anim = getattr(actor_obj, "animation_data", None)
    if anim is not None:
        action = getattr(anim, "action", None)
    channelbag = None
    if action is not None:
        try:
            channelbag = get_action_channelbag(action, target=actor_obj)
        except Exception:
            channelbag = None
    fcurves_owner = channelbag if channelbag is not None else action
    for pose_bone in pose.bones:
        for constraint in list(pose_bone.constraints):
            if not constraint.name.startswith(LOOKAT_CONSTRAINT_PREFIX):
                continue
            # Drop any influence f-curves that animate this constraint
            data_path = f'pose.bones["{pose_bone.name}"].constraints["{constraint.name}"].influence'
            if action is not None:
                for fc in list(iter_action_fcurves(action, target=actor_obj)):
                    if fc.data_path != data_path:
                        continue
                    try:
                        fcurves_owner.fcurves.remove(fc)
                    except Exception:
                        log.debug("Could not remove lookat influence fcurve %s", data_path, exc_info=True)
            try:
                pose_bone.constraints.remove(constraint)
                removed += 1
            except Exception:
                log.debug("Could not remove lookat constraint %s", constraint.name, exc_info=True)
    return removed


def clear_lookat_static_empties():
    removed = 0
    for obj in list(bpy.data.objects):
        is_static = obj.name.startswith(LOOKAT_STATIC_EMPTY_PREFIX) and obj.get("witcher_w2scene_lookat_static")
        is_anchor = obj.name.startswith(LOOKAT_ANCHOR_PREFIX) and obj.get("witcher_w2scene_lookat_anchor")
        if not (is_static or is_anchor):
            continue
        try:
            bpy.data.objects.remove(obj, do_unlink=True)
            removed += 1
        except Exception:
            log.debug("Could not remove lookat helper empty %s", obj.name, exc_info=True)
    return removed


_W2SCENE_CAMERA_TRACK_DEFAULTS = {
    "hctFOV": 50.0,
    "overrideFactor": 1.0,
    "dofFocusDistFar": 0.0,
    "dofBlurDistFar": 0.0,
    "dofIntensity": 0.0,
    "dofFocusDistNear": 0.0,
    "dofBlurDistNear": 0.0,
    "blenderDofFocusDistance": 0.0,
    "blenderDofFocusDistanceWeight": 0.0,
}


_W2SCENE_CAMERA_TRACK_ATTRS = {
    "hctFOV": "cameraFov",
    "dofFocusDistFar": "dofFocusDistFar",
    "dofBlurDistFar": "dofBlurDistFar",
    "dofIntensity": "dofIntensity",
    "dofFocusDistNear": "dofFocusDistNear",
    "dofBlurDistNear": "dofBlurDistNear",
}


_BLENDER_DOF_FOCUS_DISTANCE_TRACK = "blenderDofFocusDistance"
_BLENDER_DOF_FOCUS_DISTANCE_WEIGHT_TRACK = "blenderDofFocusDistanceWeight"
_W2SCENE_DOF_FOCUS_BONE_NAMES = ("head", "Head", "neck", "Neck")


_APERTURE_DOF_DEFAULT_APERTURE = 5
_APERTURE_DOF_CIRCLE_OF_CONFUSION = 0.00003


def _prop_children(prop):
    if prop is None:
        return []
    for attr in ("PROPS", "MoreProps", "More"):
        values = getattr(prop, attr, None)
        if values is not None:
            return list(values)
    return []


def _prop_child_value(prop, name, default=None):
    for child in _prop_children(prop):
        if getattr(child, "theName", None) != name:
            continue
        if hasattr(child, "Value"):
            return child.Value
        if hasattr(child, "value"):
            return child.value
        index_obj = getattr(child, "Index", None)
        if index_obj is not None:
            return getattr(index_obj, "String", None) or getattr(index_obj, "Path", None) or default
        return default
    return default


def _prop_child_value_any(prop, names, default=None):
    for name in names:
        value = _prop_child_value(prop, name, None)
        if value is not None:
            return value
    return default


def _prop_child_bool(prop, name, default=False):
    value = _prop_child_value(prop, name, default)
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes"}
    return bool(value)


def _aperture_enum_index(value):
    if value is None:
        return _APERTURE_DOF_DEFAULT_APERTURE
    if isinstance(value, str):
        aperture_names = {
            "APERTURE_1_0": 0,
            "APERTURE_1_4": 1,
            "APERTURE_2_0": 2,
            "APERTURE_2_8": 3,
            "APERTURE_4_0": 4,
            "APERTURE_5_6": 5,
            "APERTURE_8_0": 6,
            "APERTURE_11_0": 7,
            "APERTURE_16_0": 8,
            "APERTURE_22_0": 9,
            "APERTURE_32_0": 10,
        }
        if value in aperture_names:
            return aperture_names[value]
    return _as_int(value, _APERTURE_DOF_DEFAULT_APERTURE)


def _aperture_dof_to_engine_planes(dof_prop):
    if not (
        _prop_child_bool(dof_prop, "enabled", False)
        or _prop_child_bool(dof_prop, "m_enabled", False)
    ):
        return None
    focal_length = _as_float(
        _prop_child_value_any(dof_prop, ("focalLength", "m_focalLength"), 0.0),
        0.0,
    )
    focus_distance = _as_float(
        _prop_child_value_any(dof_prop, ("distance", "m_distance"), 0.0),
        0.0,
    )
    if focal_length <= 0.0 or focus_distance <= 0.0:
        return None

    focal_meters = focal_length * 0.001
    aperture = math.pow(
        1.4142,
        _aperture_enum_index(_prop_child_value_any(dof_prop, ("aperture", "m_aperture"), None)),
    )
    hyperfocal_distance = (focal_meters * focal_meters) / (aperture * _APERTURE_DOF_CIRCLE_OF_CONFUSION)
    focus_near = (hyperfocal_distance * focus_distance) / (hyperfocal_distance + focus_distance)
    if focus_distance < hyperfocal_distance:
        focus_far = (hyperfocal_distance * focus_distance) / (hyperfocal_distance - focus_distance)
    else:
        focus_far = 1000.0 * focal_meters

    return {
        "dofFocusDistNear": focus_near,
        "dofFocusDistFar": focus_far,
        "dofBlurDistNear": 0.0,
        "dofBlurDistFar": focus_far + focus_near,
        "dofIntensity": 1.0,
    }


def _story_scene_camera_track_values(camera_definition):
    values = {}
    for track_name in CAMERA_TRACK_NAMES:
        default = _W2SCENE_CAMERA_TRACK_DEFAULTS.get(track_name, CAMERA_TRACK_DEFAULTS.get(track_name, 0.0))
        attr_name = _W2SCENE_CAMERA_TRACK_ATTRS.get(track_name)
        if attr_name:
            values[track_name] = _as_float(getattr(camera_definition, attr_name, None), default)
        else:
            values[track_name] = default
    aperture_planes = _aperture_dof_to_engine_planes(getattr(camera_definition, "dof", None))
    if aperture_planes is not None:
        values.update(aperture_planes)
    return values


def _camera_actor_focus_distance(camera_matrix, focus_targets):
    if camera_matrix is None or not focus_targets:
        return None
    camera_loc = camera_matrix.translation
    all_candidates = []
    front_candidates = []
    try:
        camera_forward = camera_matrix.to_quaternion() @ Vector((0.0, 0.0, -1.0))
    except Exception:
        camera_forward = None

    for target_obj in focus_targets:
        if target_obj is None:
            continue
        try:
            target_loc = _focus_target_location(target_obj)
            if target_loc is None:
                continue
            direction = target_loc - camera_loc
            distance = float(direction.length)
        except Exception:
            continue
        if distance <= 0.001:
            continue
        all_candidates.append((distance, 0.0))
        if camera_forward is not None:
            try:
                dot = float(direction.normalized().dot(camera_forward))
                projected_distance = float(direction.dot(camera_forward))
            except Exception:
                dot = 0.0
                projected_distance = 0.0
            if dot > 0.0 and projected_distance > 0.001:
                front_candidates.append((projected_distance, dot, distance))

    if front_candidates:
        front_candidates.sort(key=lambda item: (-item[1], item[0]))
        return front_candidates[0][0]
    if all_candidates:
        return min(distance for distance, _dot in all_candidates)
    return None


def _focus_target_location(target_obj):
    candidate_objects = []
    try:
        mimic_name = str(target_obj.get("mimicFace", "") or "")
    except Exception:
        mimic_name = ""
    mimic_obj = bpy.data.objects.get(mimic_name) if mimic_name else None
    if mimic_obj is not None and mimic_obj is not target_obj:
        candidate_objects.append(mimic_obj)
    candidate_objects.append(target_obj)

    for candidate_obj in candidate_objects:
        pose_bones = getattr(getattr(candidate_obj, "pose", None), "bones", None)
        if pose_bones is None:
            continue
        focus_bone = None
        for bone_name in _W2SCENE_DOF_FOCUS_BONE_NAMES:
            focus_bone = pose_bones.get(bone_name)
            if focus_bone is not None:
                break
        if focus_bone is None:
            for pose_bone in pose_bones:
                if str(getattr(pose_bone, "name", "") or "").lower().endswith("head"):
                    focus_bone = pose_bone
                    break
        if focus_bone is not None:
            try:
                return (candidate_obj.matrix_world @ focus_bone.matrix).translation
            except Exception:
                pass

    bound_box = getattr(target_obj, "bound_box", None)
    if bound_box:
        try:
            points = [target_obj.matrix_world @ Vector(corner) for corner in bound_box]
            if points:
                center = Vector((0.0, 0.0, 0.0))
                for point in points:
                    center += point
                return center / len(points)
        except Exception:
            pass

    try:
        return target_obj.matrix_world.translation
    except Exception:
        return None


def _camera_tracks_with_blender_focus_distance(camera_tracks, camera_matrix, focus_targets):
    tracks = dict(camera_tracks or {})
    focus_distance = _camera_actor_focus_distance(camera_matrix, focus_targets)
    if focus_distance is not None:
        tracks[_BLENDER_DOF_FOCUS_DISTANCE_TRACK] = focus_distance
        tracks[_BLENDER_DOF_FOCUS_DISTANCE_WEIGHT_TRACK] = 1.0
    return tracks


def _existing_w2scene_actor_focus_targets(story_scene):
    focus_targets = []
    for actor_ref in getattr(getattr(story_scene, "sceneTemplates", None), "value", []) or []:
        try:
            actor_template = story_scene.chunksRef[actor_ref - 1]
            actor = w3_types.CStorySceneActor(actor_template)
            actor_obj = check_if_actor_already_in_scene(actor.entityTemplate)
        except Exception:
            actor_obj = None
        if actor_obj is not None:
            focus_targets.append(actor_obj)
    return focus_targets


def _custom_camera_event_transform(event):
    translation = getattr(event, "cameraTranslation", None)
    rotation = getattr(event, "cameraRotation", None)
    if translation is None or rotation is None:
        return None
    transform = BlankEngineTransform()
    transform.X = _transform_real(translation, "X", 0.0)
    transform.Y = _transform_real(translation, "Y", 0.0)
    transform.Z = _transform_real(translation, "Z", 0.0)
    transform.Pitch = _transform_real(rotation, "Pitch", 0.0)
    transform.Yaw = _transform_real(rotation, "Yaw", 0.0)
    transform.Roll = _transform_real(rotation, "Roll", 0.0)
    return transform


def _prepare_w2scene_camera_rig(context, camera_armature, repo_path):
    if camera_armature is None or getattr(camera_armature, "type", None) != 'ARMATURE':
        return None, None
    camera_armature["cutscene_actor_name"] = "Camera"
    camera_armature["cutscene_actor_template"] = str(repo_path or "")
    camera_armature["cutscene_actor_type"] = "CAT_Camera"
    camera_armature["witcher_w2scene_camera"] = True
    camera_bone = ensure_camera_track_properties(camera_armature, track_names=CAMERA_TRACK_NAMES)
    preview_camera = find_camera_preview_object(camera_armature)
    if preview_camera is not None:
        setup_camera_preview_drivers(camera_armature, preview_camera)
        if getattr(context, "scene", None) is not None:
            context.scene.camera = preview_camera
    return camera_bone, preview_camera


def _w2scene_guid_string(guid):
    if guid is None:
        return ""
    if isinstance(guid, str):
        return guid
    guid = getattr(guid, "GUID", guid)
    guid_string = getattr(guid, "GuidString", None)
    if guid_string:
        return str(guid_string)
    guid_value = getattr(guid, "_value", None)
    if guid_value:
        return str(guid_value)
    if isinstance(guid, str):
        return guid
    return ""


def get_w2scene_event_guid_string(event):
    return _w2scene_guid_string(getattr(event, "GUID", None))


def get_w2scene_event_camera_name(event):
    camera_name = getattr(event, "customCameraName", None)
    if camera_name:
        return camera_name
    camera_definition = getattr(event, "cameraDefinition", None)
    if camera_definition:
        try:
            camera_definition = w3_types.StorySceneCameraDefinition(camera_definition)
            return camera_definition.cameraName
        except Exception:
            log.debug("Could not read cameraDefinition from %s", event.__class__.__name__, exc_info=True)
    return None


def _w2scene_camera_definition_matrix(camera_definition, place_object=None):
    transform = getattr(getattr(camera_definition, "cameraTransform", None), "EngineTransform", None)
    if transform is None:
        return None
    camera_matrix = _engine_transform_matrix(transform, from_this_object=place_object)
    return camera_matrix @ Matrix.Rotation(math.radians(90.0), 4, 'X')


def build_w2scene_camera_definitions(story_scene, place_object=None):
    camera_definitions = {}
    for camera_def in _iter_prop_values(getattr(story_scene, "cameraDefinitions", None)):
        try:
            camera_class = w3_types.StorySceneCameraDefinition(camera_def)
        except Exception:
            log.debug("Could not parse story scene camera definition", exc_info=True)
            continue
        camera_name = str(getattr(camera_class, "cameraName", "") or "")
        if not camera_name:
            continue
        camera_definitions[camera_name] = (
            _w2scene_camera_definition_matrix(camera_class, place_object=place_object),
            _story_scene_camera_track_values(camera_class),
            camera_class,
        )
    return camera_definitions


def resolve_w2scene_event_camera_pose(event, camera_definitions, place_object=None, focus_targets=None):
    camera_definition = getattr(event, "cameraDefinition", None)
    if camera_definition:
        try:
            camera_definition = w3_types.StorySceneCameraDefinition(camera_definition)
            camera_matrix = _w2scene_camera_definition_matrix(camera_definition, place_object=place_object)
            if camera_matrix is not None:
                camera_tracks = _camera_tracks_with_blender_focus_distance(
                    _story_scene_camera_track_values(camera_definition),
                    camera_matrix,
                    focus_targets,
                )
                return camera_matrix, camera_tracks, camera_definition.cameraName
        except Exception:
            log.debug("Could not resolve embedded camera pose from %s", event.__class__.__name__, exc_info=True)

    transform = _custom_camera_event_transform(event)
    if transform is not None:
        camera_matrix = _engine_transform_matrix(transform, from_this_object=place_object)
        camera_matrix = camera_matrix @ Matrix.Rotation(math.radians(90.0), 4, 'X')
        camera_tracks = _camera_tracks_with_blender_focus_distance(
            _story_scene_camera_track_values(event),
            camera_matrix,
            focus_targets,
        )
        return (
            camera_matrix,
            camera_tracks,
            get_w2scene_event_camera_name(event) or getattr(event, "eventName", None),
        )

    camera_name = get_w2scene_event_camera_name(event)
    camera_data = camera_definitions.get(camera_name)
    if not camera_data:
        return None, None, camera_name
    camera_matrix, camera_tracks, _camera_definition = camera_data
    if camera_matrix is None:
        return None, None, camera_name
    camera_matrix = camera_matrix.copy()
    camera_tracks = _camera_tracks_with_blender_focus_distance(camera_tracks, camera_matrix, focus_targets)
    return camera_matrix, camera_tracks, camera_name


def _as_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def _as_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def _dialog_script_duration(dialogscript):
    return _as_float(
        getattr(dialogscript, "_w3_scene_duration_seconds", None)
        or getattr(dialogscript, "approvedDuration", None)
        or getattr(dialogscript, "duration", None),
        0.0,
    )


def _iter_prop_values(prop):
    if prop is None:
        return []
    if isinstance(prop, (list, tuple)):
        return list(prop)
    for attr in ("value", "More", "elements", "_elements"):
        values = getattr(prop, attr, None)
        if values is not None:
            return list(values)
    return []


def _w2scene_chunk_type(chunk):
    return str(getattr(chunk, "Type", None) or getattr(chunk, "name", "") or "")


def _w2scene_chunk_prop(chunk, prop_name):
    if chunk is None:
        return None
    try:
        return chunk.GetVariableByName(prop_name)
    except Exception:
        return None


def _w2scene_chunk_ptr(chunk, prop_name):
    prop = _w2scene_chunk_prop(chunk, prop_name)
    value = getattr(prop, "Value", None)
    return _as_int(value, 0) if value else 0


def _w2scene_chunk_array(chunk, prop_name):
    return _iter_prop_values(_w2scene_chunk_prop(chunk, prop_name))


def _w2scene_chunk_cname(chunk, prop_name):
    prop = _w2scene_chunk_prop(chunk, prop_name)
    if prop is None:
        return ""
    index = getattr(prop, "Index", None)
    text = getattr(index, "String", None) if index is not None else None
    if text:
        return _cname_text(text)
    return _cname_text(getattr(prop, "Value", None))


def _w2scene_add_valid_ptr(result, chunks, ptr):
    ptr = _as_int(ptr, 0)
    if ptr <= 0 or ptr > len(chunks) or ptr in result:
        return
    result.append(ptr)


def _w2scene_flow_outgoing_indices(chunks, chunk_index):
    try:
        chunk = chunks[int(chunk_index) - 1]
    except Exception:
        return []

    result = []
    chunk_type = _w2scene_chunk_type(chunk)
    _w2scene_add_valid_ptr(result, chunks, _w2scene_chunk_ptr(chunk, "nextLinkElement"))

    if chunk_type == "CStorySceneFlowCondition":
        _w2scene_add_valid_ptr(result, chunks, _w2scene_chunk_ptr(chunk, "trueLink"))
        _w2scene_add_valid_ptr(result, chunks, _w2scene_chunk_ptr(chunk, "falseLink"))
    elif chunk_type == "CStorySceneFlowSwitch":
        for case_ptr in _w2scene_chunk_array(chunk, "cases"):
            try:
                case_chunk = chunks[int(case_ptr) - 1]
            except Exception:
                continue
            _w2scene_add_valid_ptr(result, chunks, _w2scene_chunk_ptr(case_chunk, "thenLink"))
        _w2scene_add_valid_ptr(result, chunks, _w2scene_chunk_ptr(chunk, "defaultLink"))
    elif chunk_type == "CStorySceneScript":
        for link_ptr in _w2scene_chunk_array(chunk, "links"):
            _w2scene_add_valid_ptr(result, chunks, link_ptr)
    elif chunk_type == "CStorySceneSection":
        choice_ptr = _w2scene_chunk_ptr(chunk, "choice")
        if choice_ptr:
            try:
                choice_chunk = chunks[int(choice_ptr) - 1]
            except Exception:
                choice_chunk = None
            for line_ptr in _w2scene_chunk_array(choice_chunk, "choiceLines"):
                try:
                    line_chunk = chunks[int(line_ptr) - 1]
                except Exception:
                    continue
                _w2scene_add_valid_ptr(result, chunks, _w2scene_chunk_ptr(line_chunk, "nextLinkElement"))

    return result


def _resolve_w2scene_dialogset_name_for_section(story_scene, target_section_idx):
    target_section_idx = _as_int(target_section_idx, 0)
    chunks = getattr(story_scene, "chunksRef", None) or []
    if target_section_idx <= 0 or target_section_idx > len(chunks):
        return ""

    input_indices = [
        _as_int(idx, 0)
        for idx in _iter_prop_values(getattr(story_scene, "controlParts", None))
        if 0 < _as_int(idx, 0) <= len(chunks)
        and _w2scene_chunk_type(chunks[_as_int(idx, 0) - 1]) == "CStorySceneInput"
    ]
    if not input_indices:
        return ""

    resolved_names = []
    resolved_name_keys = set()
    for input_idx in input_indices:
        queue = deque([(input_idx, "")])
        visited = set()
        while queue:
            current_idx, current_dialogset = queue.popleft()
            state_key = (current_idx, current_dialogset.lower())
            if state_key in visited:
                continue
            visited.add(state_key)
            if len(visited) > 10000:
                log.debug("Stopping dialogset flow walk after 10000 states for section index %s", target_section_idx)
                break

            try:
                chunk = chunks[int(current_idx) - 1]
            except Exception:
                continue
            chunk_type = _w2scene_chunk_type(chunk)
            dialogset_name = current_dialogset

            if chunk_type == "CStorySceneInput":
                input_dialogset = _w2scene_chunk_cname(chunk, "dialogsetInstanceName")
                if input_dialogset:
                    dialogset_name = input_dialogset
            elif chunk_type == "CStorySceneSection":
                section_dialogset = _w2scene_chunk_cname(chunk, "dialogsetChangeTo")
                if section_dialogset:
                    dialogset_name = section_dialogset

            if current_idx == target_section_idx:
                dialogset_key = dialogset_name.lower()
                if dialogset_name and dialogset_key not in resolved_name_keys:
                    resolved_names.append(dialogset_name)
                    resolved_name_keys.add(dialogset_key)
                continue

            for next_idx in _w2scene_flow_outgoing_indices(chunks, current_idx):
                queue.append((next_idx, dialogset_name))

    if len(resolved_names) > 1:
        log.info(
            "Multiple dialogsets can reach section index %s via scene flow; using %s from %s",
            target_section_idx,
            resolved_names[0],
            ", ".join(resolved_names),
        )
    return resolved_names[0] if resolved_names else ""


def _safe_nla_track_name(prefix, *parts):
    raw = "_".join(str(part or "").strip() for part in parts if str(part or "").strip())
    safe = "".join(char if char.isalnum() or char in "._-" else "_" for char in raw)
    safe = "_".join(part for part in safe.split("_") if part)
    if not safe:
        return prefix
    max_safe_len = max(1, W2SCENE_NLA_TRACK_NAME_MAX - len(prefix) - 1)
    if len(safe) > max_safe_len:
        tail = safe[-8:] if max_safe_len > 12 else ""
        if tail:
            head_len = max_safe_len - len(tail) - 1
            safe = f"{safe[:head_len].rstrip('_')}_{tail}"
        else:
            safe = safe[:max_safe_len]
    return f"{prefix}_{safe}"

class HasAnimationData:
    animation_data: bpy.types.AnimData

from typing import Union
class SceneImporter():
    def __init__(self):
        self._CStoryScene = None
        self.__use_NLA = True
        self.__NLA_track = 'CAMERA_BLEND'
        self.__frame_margin = 0
        self.__frame_current = 0
        self.scene_element_dict = []
        self.scene_sections = []
        self._section_scene_event_elements = []
        self._section_event_variant_by_guid = {}
        self._section_active_variant_id = None
        self._scene_filepath = ""
        self._section_name = ""
        self._section_dialogset_name = ""
        self._section_chunk_index = 0

    def __assign_action(self, target: Union[bpy.types.ID, HasAnimationData], action: bpy.types.Action, track_name:str = None, at_frame = False):
        if target.animation_data is None:
            target.animation_data_create()
        track_name = track_name if track_name else self.__NLA_track

        if not self.__use_NLA:
            assign_action(target, action)
        else:
            #frame_current = bpy.context.scene.frame_current
            if track_name:
                target_track: bpy.types.NlaTrack = target.animation_data.nla_tracks.get(track_name)
                if target_track is None:
                    target_track: bpy.types.NlaTrack = target.animation_data.nla_tracks.new()
                    target_track.name = track_name #action.name
                strip_frame = at_frame if at_frame is not False else self.__frame_current
                if strip_frame !=0:
                    pass # adding multiple strips
                else:
                    for strip in target_track.strips:
                        target_track.strips.remove(strip)
            else:
                target_track: bpy.types.NlaTrack = target.animation_data.nla_tracks.new()
                target_track.name = action.name
            
            strip_frame = at_frame if at_frame is not False else self.__frame_current
            test_strips = []
            for st in target_track.strips:
                test_strips.append(st)
            if target_track.strips:
                last_strip = target_track.strips[-1]
                strip_start = last_strip.frame_end
            # else:
            try:
                target_strip = target_track.strips.new(action.name, strip_frame, action)
            except Exception as e:
                target_strip = target_track.strips.new(action.name, int(strip_frame + 1), action)
                target_strip.frame_start = strip_frame
                start_frame, end_frame = action.frame_range
                length = end_frame - start_frame
                target_strip.frame_end = strip_frame + length
            bind_strip_action_slot(target_strip, resolve_action_slot(action, target=target, ensure=True))
            target_strip.blend_type = 'REPLACE'
            return target_strip

    def loadSceneFile(self, filePath):
        self._scene_filepath = str(filePath or "")
        self._CStoryScene:w3_types.CStoryScene = loadSceneFile(filePath)

    def _section_variant_duration_overrides(self, section):
        duration_by_element_id = {}
        for variant_ptr in _iter_prop_values(getattr(section, "variants", None)):
            try:
                variant_chunk = self._CStoryScene.chunksRef[variant_ptr - 1]
                variant = w3_types.CStorySceneSectionVariant(variant_chunk)
            except Exception:
                log.debug("Could not parse story scene section variant", exc_info=True)
                continue

            for element_info_prop in _iter_prop_values(getattr(variant, "elementInfo", None)):
                try:
                    element_info = w3_types.CStorySceneSectionVariantElementInfo(element_info_prop)
                except Exception:
                    log.debug("Could not parse section variant element info", exc_info=True)
                    continue
                element_id = str(getattr(element_info, "elementId", "") or "")
                approved_duration = getattr(element_info, "approvedDuration", None)
                if element_id and approved_duration is not None:
                    duration_by_element_id[element_id] = _as_float(approved_duration)

            if duration_by_element_id:
                break
        return duration_by_element_id

    def _section_default_variant_id(self, section):
        default_variant_id = getattr(section, "defaultVariantId", None)
        if default_variant_id is not None:
            return _as_int(default_variant_id, 0)
        for variant_ptr in _iter_prop_values(getattr(section, "variants", None)):
            try:
                variant_chunk = self._CStoryScene.chunksRef[variant_ptr - 1]
                variant = w3_types.CStorySceneSectionVariant(variant_chunk)
                return _as_int(getattr(variant, "id", 0), 0)
            except Exception:
                log.debug("Could not parse story scene section variant id", exc_info=True)
        return None

    def _section_event_variant_map(self, section):
        event_variant_by_guid = {}
        for event_info_ptr in _iter_prop_values(getattr(section, "eventsInfo", None)):
            try:
                event_info_chunk = self._CStoryScene.chunksRef[event_info_ptr - 1]
                event_info = w3_types.CStorySceneEventInfo(event_info_chunk)
            except Exception:
                log.debug("Could not parse story scene event info", exc_info=True)
                continue
            guid_string = _w2scene_guid_string(getattr(event_info, "eventGuid", None))
            if guid_string:
                event_variant_by_guid[guid_string] = _as_int(getattr(event_info, "sectionVariantId", 0), 0)
        return event_variant_by_guid

    def _section_event_is_active(self, event, include_muted=False):
        if not include_muted and bool(getattr(event, "isMuted", False) or False):
            return False
        guid_string = get_w2scene_event_guid_string(event)
        active_variant_id = getattr(self, "_section_active_variant_id", None)
        event_variant_by_guid = getattr(self, "_section_event_variant_by_guid", {}) or {}
        if active_variant_id is not None and guid_string in event_variant_by_guid:
            return event_variant_by_guid[guid_string] == active_variant_id
        return True

    def _event_start_frame(self, event, fps=30.0, fallback_dialogscript=None):
        scene_element = getattr(event, "sceneElement", None)
        scene_element_id = getattr(scene_element, "Value", None)
        element_start = self._section_element_start_seconds.get(
            scene_element_id,
            getattr(fallback_dialogscript, "_w3_scene_start_seconds", 0.0),
        )
        element_duration = self._section_element_duration_seconds.get(
            scene_element_id,
            _dialog_script_duration(fallback_dialogscript),
        )
        start_position = _as_float(getattr(event, "startPosition", None), 0.0)
        return (element_start + (element_duration * start_position)) * fps

    def load_section(self, section):
        self.scene_element_dict = {}
        self._section_name = str(getattr(section, "sectionName", "") or "")
        self._section_chunk_index = _as_int(getattr(section, "_w3_chunk_index", 0), 0)
        explicit_dialogset_name = _cname_text(getattr(section, "dialogsetChangeTo", None))
        self._section_dialogset_name = explicit_dialogset_name
        if not self._section_dialogset_name:
            self._section_dialogset_name = _resolve_w2scene_dialogset_name_for_section(
                self._CStoryScene,
                self._section_chunk_index,
            )
            if self._section_dialogset_name:
                log.info(
                    "Resolved inherited dialogset '%s' for section %s.",
                    self._section_dialogset_name,
                    self._section_name or self._section_chunk_index,
                )
        self._section_scene_event_elements = list(getattr(section, "sceneEventElements", []) or [])
        self._section_active_variant_id = self._section_default_variant_id(section)
        self._section_event_variant_by_guid = self._section_event_variant_map(section)
        self._section_element_start_seconds = {}
        self._section_element_duration_seconds = {}
        self._section_duration_seconds = 0.0
        duration_overrides = self._section_variant_duration_overrides(section)

        section_time_seconds = 0.0
        for el in getattr(getattr(section, "sceneElements", None), "value", []) or []:
            chunk = self._CStoryScene.chunksRef[el-1]
            sceneElement = w3_types.str_to_class(chunk.Type)(chunk)
            element_id = str(getattr(sceneElement, "elementID", "") or "")
            duration_seconds = duration_overrides.get(element_id, _dialog_script_duration(sceneElement))
            setattr(sceneElement, "_w3_scene_duration_seconds", duration_seconds)
            setattr(sceneElement, "_w3_scene_start_seconds", section_time_seconds)
            self._section_element_start_seconds[el] = section_time_seconds
            self._section_element_duration_seconds[el] = duration_seconds

            shot_dict = {}
            shot_dict['dialogscript'] = sceneElement
            shot_dict['CUE'] = []
            shot_dict['start_seconds'] = section_time_seconds
            shot_dict['duration_seconds'] = duration_seconds
            self.scene_element_dict[el] = shot_dict
            section_time_seconds += duration_seconds
            #each sceneelement contains dialoge and "CUE" shot that
            #contains all the events (sceneEventElements)
            #shot_1
            #shot_2
            #shot_3
        self._section_duration_seconds = section_time_seconds

        # Sentinel entry (key=None) collects events whose sceneElement ptr is null
        # or points outside this section's elements. They use section-spanning timing:
        # at_frame = startPosition * section_total_duration.
        class _SectionSpan:
            pass
        _span_script = _SectionSpan()
        _span_script._w3_scene_start_seconds = 0.0
        _span_script._w3_scene_duration_seconds = section_time_seconds
        self._section_element_start_seconds[None] = 0.0
        self._section_element_duration_seconds[None] = section_time_seconds
        self.scene_element_dict[None] = {
            'dialogscript': _span_script,
            'CUE': [],
            'start_seconds': 0.0,
            'duration_seconds': section_time_seconds,
        }

        for sceneEventElement in self._section_scene_event_elements:
            el_type =  sceneEventElement.__class__.__name__
            if hasattr(sceneEventElement, 'theType'):
                raise Exception('Missing Event Class')
            else:
                scene_element = getattr(sceneEventElement, "sceneElement", None)
                scene_element_id = getattr(scene_element, "Value", None)
                if scene_element_id in self.scene_element_dict:
                    self.scene_element_dict[scene_element_id]['CUE'].append(sceneEventElement)
                else:
                    self.scene_element_dict[None]['CUE'].append(sceneEventElement)

    def load_sections(self):
        chunks = self._CStoryScene.chunksRef
        sections_prop = self._CStoryScene.sections
        if sections_prop is None:
            return
        all_section_indices = list(getattr(sections_prop, 'value', None) or [])
        if not all_section_indices:
            return

        section_index_set = set(all_section_indices)
        section_orig_order = {idx: i for i, idx in enumerate(all_section_indices)}

        def _get_ptr(chunk, prop_name):
            prop = chunk.GetVariableByName(prop_name)
            if prop is None:
                return None
            v = getattr(prop, 'Value', None)
            return v if v else None

        def _get_array(chunk, prop_name):
            prop = chunk.GetVariableByName(prop_name)
            if prop is None:
                return []
            return getattr(prop, 'value', None) or []

        def _outgoing_links(chunk):
            links = []
            v = _get_ptr(chunk, 'nextLinkElement')
            if v:
                links.append(v)
            name = chunk.name
            if name == 'CStorySceneFlowCondition':
                for pn in ('trueLink', 'falseLink'):
                    v = _get_ptr(chunk, pn)
                    if v:
                        links.append(v)
            elif name == 'CStorySceneFlowSwitch':
                for ci in _get_array(chunk, 'cases'):
                    v = _get_ptr(chunks[ci - 1], 'thenLink')
                    if v:
                        links.append(v)
                v = _get_ptr(chunk, 'defaultLink')
                if v:
                    links.append(v)
            return links

        def _sections_reachable_from(start_idx):
            reached = []
            visited = set()
            q = deque([start_idx])
            while q:
                idx = q.popleft()
                if idx in section_index_set:
                    if idx not in visited:
                        visited.add(idx)
                        reached.append(idx)
                    continue
                if idx in visited:
                    continue
                visited.add(idx)
                q.extend(_outgoing_links(chunks[idx - 1]))
            return reached

        def _section_successors(section_idx):
            chunk = chunks[section_idx - 1]
            result = []
            seen = set()
            def _add(targets):
                for t in targets:
                    if t not in seen:
                        seen.add(t)
                        result.append(t)
            v = _get_ptr(chunk, 'nextLinkElement')
            if v:
                _add(_sections_reachable_from(v))
            choice_idx = _get_ptr(chunk, 'choice')
            if choice_idx:
                for line_idx in _get_array(chunks[choice_idx - 1], 'choiceLines'):
                    v = _get_ptr(chunks[line_idx - 1], 'nextLinkElement')
                    if v:
                        _add(_sections_reachable_from(v))
            return result

        succ = {}
        in_degree = {idx: 0 for idx in all_section_indices}
        for idx in all_section_indices:
            s = _section_successors(idx)
            succ[idx] = s
            for t in s:
                if t in in_degree:
                    in_degree[t] += 1

        entry_sections = set()
        control_parts_prop = self._CStoryScene.controlParts
        if control_parts_prop is not None:
            for cp_idx in (getattr(control_parts_prop, 'value', None) or []):
                cp_chunk = chunks[cp_idx - 1]
                if cp_chunk.name == 'CStorySceneInput':
                    v = _get_ptr(cp_chunk, 'nextLinkElement')
                    if v:
                        entry_sections.update(_sections_reachable_from(v))

        zero_degree = sorted(
            [idx for idx in all_section_indices if in_degree[idx] == 0],
            key=lambda idx: (0 if idx in entry_sections else 1, section_orig_order.get(idx, 0))
        )
        q = deque(zero_degree)
        ordered = []
        while q:
            idx = q.popleft()
            ordered.append(idx)
            for t in succ.get(idx, []):
                if t in in_degree:
                    in_degree[t] -= 1
                    if in_degree[t] == 0:
                        q.append(t)

        ordered_set = set(ordered)
        for idx in all_section_indices:
            if idx not in ordered_set:
                ordered.append(idx)

        for idx in ordered:
            chunk = chunks[idx - 1]
            section_cls = w3_types.str_to_class(chunk.Type)
            section = section_cls(chunk)
            try:
                section._w3_chunk_index = idx
            except Exception:
                pass
            self.scene_sections.append(section)

    def preview_camera_event(self, context, event, fps=30.0):
        if event is None:
            raise ValueError("No story scene event selected")
        placeCube = bpy.data.objects.get('SCENE_POINT')
        if not placeCube:
            bpy.ops.object.empty_add(type="PLAIN_AXES", radius=0.1)
            placeCube = bpy.context.object
            placeCube.name = "SCENE_POINT"

        scene_camera_entity_path = W2SCENE_CAMERA_ENTITY_PATH
        scene_cam_obj = check_if_actor_already_in_scene(scene_camera_entity_path)
        if not scene_cam_obj:
            scene_cam_obj = import_entity.import_ent_template(str(Path(get_uncook_path(context)) / scene_camera_entity_path))
        if scene_cam_obj is None:
            raise RuntimeError("Could not load gameplay\\camera\\scene_camera.w2ent")

        event_frame = self._event_start_frame(event, fps=fps)
        if getattr(context, "scene", None) is not None:
            context.scene.frame_set(max(0, int(round(event_frame))))
        context.view_layer.update()

        camera_bone, preview_camera = _prepare_w2scene_camera_rig(context, scene_cam_obj, scene_camera_entity_path)
        if camera_bone is None:
            raise RuntimeError(f"Scene camera rig is missing {CAMERA_CONTROL_BONE}")
        edit_bone = scene_cam_obj.pose.bones.get(CAMERA_EDIT_BONE)
        if edit_bone is None:
            raise RuntimeError(f"Scene camera rig is missing {CAMERA_EDIT_BONE}")

        camera_definitions = build_w2scene_camera_definitions(self._CStoryScene, place_object=placeCube)
        camera_focus_targets = _existing_w2scene_actor_focus_targets(self._CStoryScene)
        camera_matrix, camera_tracks, camera_name = resolve_w2scene_event_camera_pose(
            event,
            camera_definitions,
            place_object=placeCube,
            focus_targets=camera_focus_targets,
        )
        if camera_matrix is None:
            raise RuntimeError(f"Could not resolve camera pose for {event.__class__.__name__}")

        preview_offset = _camera_preview_offset_matrix(scene_cam_obj)
        edit_bone.matrix = _camera_matrix_to_edit_bone_matrix(scene_cam_obj, camera_matrix, preview_offset)
        for track_name in CAMERA_TRACK_NAMES:
            camera_bone[track_name] = _as_float(
                (camera_tracks or {}).get(track_name),
                _W2SCENE_CAMERA_TRACK_DEFAULTS.get(track_name, CAMERA_TRACK_DEFAULTS.get(track_name, 0.0)),
            )
        if preview_camera is not None:
            context.scene.camera = preview_camera
        context.view_layer.update()
        return camera_name or event.__class__.__name__, event_frame

    def execute(self, frame_offset=0, keep_existing_nla=False):
        if not getattr(self, "_redkit_repo_context_active", False):
            if _scene_linked_assets_prefer_bundles():
                # Preference active: suppress REDkit path priority for all sub-asset
                # loads (entities, meshes, textures). The scene file itself was already
                # loaded from its absolute path; linked assets resolve via bundles only.
                ctx = vanilla_only_repo_context()
            else:
                ctx = redkit_repo_context(self._scene_filepath)
            with ctx:
                self._redkit_repo_context_active = True
                try:
                    return self.execute(frame_offset=frame_offset, keep_existing_nla=keep_existing_nla)
                finally:
                    self._redkit_repo_context_active = False

        s = time.time()
        _CStoryScene = self._CStoryScene
        context = bpy.context
        placeCube = bpy.data.objects.get('SCENE_POINT')
        if not placeCube:
            bpy.ops.object.empty_add(type="PLAIN_AXES", radius=0.1)
            placeCube = bpy.context.object
            placeCube.name = "SCENE_POINT"

        removed_debug = clear_w2scene_debug_empties(context)
        if removed_debug:
            log.info("Removed %d previous .w2scene debug marker(s).", removed_debug)
        if not keep_existing_nla:
            removed_audio = clear_w2scene_section_audio(context.scene)
            if removed_audio:
                log.info("Removed %d previous .w2scene audio strip(s).", removed_audio)

        scene_camera_entity_path = W2SCENE_CAMERA_ENTITY_PATH

        scene_cam_obj = check_if_actor_already_in_scene(scene_camera_entity_path)
        if not scene_cam_obj:
            scene_cam_obj = import_entity.import_ent_template(str(Path(get_uncook_path(context)) / scene_camera_entity_path))
        context.view_layer.update()
        _scene_camera_bone, scene_camera_preview_obj = _prepare_w2scene_camera_rig(context, scene_cam_obj, scene_camera_entity_path)
        scene_camera_preview_offset = _camera_preview_offset_matrix(scene_cam_obj)

        camera_definitions = {}
        actors_dict= {}
        props_dict = {}
        dialogset_idle_animations = []
        dialogset_mimic_animations = []  # [(actor_obj, layer_value, layer_column, track_name, pose_weight), ...]
        dialogset_body_state_by_actor = {}
        dialogset_mimic_state_by_actor = {}
        dialogset_idle_change_counts_by_actor = {}
        dialogset_mimic_change_counts_by_actor = {}
        dialogset_initial_placement_by_actor = {}

        for actor in getattr(getattr(_CStoryScene, "sceneTemplates", None), "value", []) or []: #<array:2,0,ptr:CStorySceneActor>
            actor_template = _CStoryScene.chunksRef[actor-1]
            actor = w3_types.CStorySceneActor(actor_template)
            preferred_appearance = _scene_actor_preferred_appearance(actor)

            actor_obj = check_if_actor_already_in_scene(actor.entityTemplate)
            if not actor_obj:
                actor_obj = import_entity.import_ent_template(
                    repo_file(actor.entityTemplate),
                    load_face_poses=True,
                    import_apperance=1,
                    selected_appearance_name=preferred_appearance,
                )
            else:
                _ensure_cutscene_actor_appearance(actor_obj, preferred_appearance)
            if actor_obj is None:
                log.warning("Skipping scene actor %s; failed to import %s", actor.id, actor.entityTemplate)
                continue
            _ensure_cutscene_face_setup(actor_obj)
            if not keep_existing_nla:
                removed_tracks = clear_w2scene_actor_section_nla(context, actor_obj)
                if removed_tracks:
                    log.debug("Removed %d stale .w2scene NLA strip(s) from %s", removed_tracks, actor_obj.name)
            actors_dict[actor.id] = (actor_obj, actor)

        for prop_ref in getattr(getattr(_CStoryScene, "sceneProps", None), "value", []) or []:
            try:
                prop_chunk = _CStoryScene.chunksRef[prop_ref - 1]
                prop = w3_types.CStorySceneProp(prop_chunk)
            except Exception:
                log.debug("Could not parse scene prop definition %s", prop_ref, exc_info=True)
                continue
            prop_obj = _import_scene_prop_object(prop, context, self._scene_filepath)
            if prop_obj is None:
                continue
            if not keep_existing_nla:
                removed_tracks = clear_w2scene_prop_section_nla(prop_obj)
                if removed_tracks:
                    log.debug("Removed %d stale .w2scene prop NLA strip(s) from %s", removed_tracks, prop_obj.name)
            props_dict[str(getattr(prop, "id", "") or "")] = (prop_obj, prop)

        if not keep_existing_nla:
            for actor_obj, _actor in actors_dict.values():
                reset_transforms(actor_obj)
            for prop_obj, _prop in props_dict.values():
                reset_transforms(prop_obj)
                reset_w2scene_prop_visibility(prop_obj)
        context.view_layer.update()

        _target_dialogset = self._section_dialogset_name
        _dialogset_ptrs = list(_iter_prop_values(getattr(_CStoryScene, "dialogsetInstances", None)))
        if not _target_dialogset and len(_dialogset_ptrs) > 1:
            log.info(
                "Section %s has no explicit or inherited dialogset; using the first of %d dialogsets.",
                self._section_name or "<unnamed>",
                len(_dialogset_ptrs),
            )
        debug_slot_empty_count = 0
        debug_placement_empty_count = 0
        debug_actor_placement_index = {}
        debug_prop_placement_index = {}
        selected_dialogset_count = 0
        for di in _dialogset_ptrs: #<array:2,0,ptr:CStorySceneActor>
            chunk = _CStoryScene.chunksRef[di-1]
            _di = w3_types.CStorySceneDialogsetInstance(chunk)
            _di_name = str(getattr(_di, "name", "") or "").strip()
            if _target_dialogset:
                if _di_name.lower() != _target_dialogset.lower():
                    log.debug("Skipping dialogset '%s' (section wants '%s')", _di_name, _target_dialogset)
                    continue
            elif selected_dialogset_count > 0:
                log.debug("Skipping dialogset '%s' because no section dialogset was resolved and the first dialogset was already applied", _di_name)
                continue
            selected_dialogset_count += 1
            placementTag = _di.placementTag[0] if _di.placementTag else None # find placement tag, use for relative transforms

            #place cube should not change if using actor as base. Need to create temp transform
            #placeCube = False
            if placementTag:
                for key, actor in actors_dict.items():
                    if placementTag in (actor[1].actorTags or []):
                        placeCube = actor[0]
                        break

            for dss in getattr(getattr(_di, "slots", None), "value", []) or []: #<array:2,0,ptr:CStorySceneActor>
                chunk = _CStoryScene.chunksRef[dss-1]
                _dss = w3_types.CStorySceneDialogsetSlot(chunk)
                slot_transform = _dss.slotPlacement.EngineTransform if _dss.slotPlacement else BlankEngineTransform()
                slot_empty_name = _safe_w2scene_debug_name(
                    "w2scene_slot",
                    self._section_name,
                    _di_name or "dialogset",
                    getattr(_dss, "slotName", ""),
                    getattr(_dss, "actorName", ""),
                )
                if _create_w2scene_debug_empty(
                    context,
                    slot_empty_name,
                    slot_transform,
                    from_object=placeCube,
                    display_type='CUBE',
                    display_size=0.32,
                    color=(0.18, 0.48, 1.0, 1.0),
                    metadata={
                        W2SCENE_DEBUG_EMPTY_TYPE_PROP: "dialogset_slot",
                        W2SCENE_DEBUG_SECTION_PROP: self._section_name,
                        "witcher_w2scene_dialogset": _di_name,
                        "witcher_w2scene_slot_name": str(getattr(_dss, "slotName", "") or ""),
                        "witcher_w2scene_actor": str(getattr(_dss, "actorName", "") or ""),
                    },
                ) is not None:
                    debug_slot_empty_count += 1
                actor_entry = actor_entry_by_str(_dss.actorName, actors_dict)
                if not _dss.actorName or not actor_entry:
                    log.debug("Skipping dialogset slot %s with actor %s", _dss.slotName, _dss.actorName)
                    continue
                actor_obj = actor_entry[0]
                reset_transforms(actor_obj)
                set_blender_object_transform(actor_obj, slot_transform, from_this_object = placeCube)
                # Body NLA actions can contain object transform curves; keep the slot as a keyed placement layer.
                actor_key = getattr(actor_obj, "name", str(id(actor_obj)))
                dialogset_initial_placement_by_actor[actor_key] = {
                    "object": actor_obj,
                    "values": _w2scene_object_transform_values(actor_obj),
                    "dialogset": _di_name,
                    "slot": str(getattr(_dss, "slotName", "") or ""),
                    "actorName": str(getattr(_dss, "actorName", "") or ""),
                }
                # Body idle: forceBodyIdleAnimation takes priority; otherwise resolve from CSV
                force_anim = _cname_text(getattr(_dss, "forceBodyIdleAnimation", None))
                if force_anim:
                    body_anim = force_anim
                else:
                    body_anim = _lookup_dialogset_body_anim(
                        getattr(_dss, "actorStatus", None),
                        getattr(_dss, "actorEmotionalState", None),
                        getattr(_dss, "actorPoseName", None),
                    )
                dialogset_body_state_by_actor[getattr(actor_obj, "name", str(_dss.actorName))] = {
                    "status": _cname_text(getattr(_dss, "actorStatus", None)),
                    "emotional_state": _cname_text(getattr(_dss, "actorEmotionalState", None)),
                    "pose_name": _cname_text(getattr(_dss, "actorPoseName", None)),
                }
                if body_anim:
                    dialogset_idle_animations.append((actor_obj, body_anim, _dss.actorName, _dss.slotName))
                else:
                    log.debug(
                        "No body idle resolved for slot %s (actor=%s, status=%s, emotional=%s, pose=%s)",
                        _dss.slotName, _dss.actorName,
                        getattr(_dss, "actorStatus", None),
                        getattr(_dss, "actorEmotionalState", None),
                        getattr(_dss, "actorPoseName", None),
                    )

                # Mimics: each per-layer field stores an emotional-state CName; if a layer
                # isn't set, fall back to actorMimicsEmotionalState. The actual catalog id
                # is resolved at load time by trying candidate variants.
                mimics_state = str(getattr(_dss, "actorMimicsEmotionalState", "") or "").strip()
                mimic_state = {
                    "emotional_state": _cname_text(getattr(_dss, "actorMimicsEmotionalState", None)),
                    "eyes": "",
                    "pose": "",
                    "animation": "",
                    "pose_weight": _as_float(getattr(_dss, "actorMimicsLayer_Pose_Weight", None), 1.0),
                }
                for _attr, _col, _track in (
                    ("actorMimicsLayer_Eyes",      "eyes",      "SceneDialogsetMimicsEyes"),
                    ("actorMimicsLayer_Pose",      "pose",      "SceneDialogsetMimicsPose"),
                    ("actorMimicsLayer_Animation", "animation", "SceneDialogsetMimicsAnim"),
                ):
                    _val = str(getattr(_dss, _attr, "") or "").strip()
                    if not _val or _val.upper() == "NONE":
                        _val = mimics_state
                    if not _val or _val.upper() == "NONE":
                        continue
                    # Store the (state, column) so loader can try candidates.
                    _pose_weight = _as_float(getattr(_dss, "actorMimicsLayer_Pose_Weight", None), 1.0) if _col == "pose" else 1.0
                    mimic_state[_col] = _val
                    dialogset_mimic_animations.append((actor_obj, _val, _col, _track, _pose_weight))
                dialogset_mimic_state_by_actor[getattr(actor_obj, "name", str(_dss.actorName))] = mimic_state

        if _target_dialogset and selected_dialogset_count == 0:
            log.warning(
                "Section %s resolved dialogset '%s', but no matching dialogset instance exists in this scene.",
                self._section_name or "<unnamed>",
                _target_dialogset,
            )

        camera_focus_targets = [actor_entry[0] for actor_entry in actors_dict.values() if actor_entry and actor_entry[0]]
        camera_definitions = build_w2scene_camera_definitions(_CStoryScene, place_object=placeCube)

        #shot = scene_element_dict[8]
        self.__frame_current = float(frame_offset)
        __fps = W2SCENE_SCENE_FPS
        section_nla_targets = []
        section_nla_target_names = set()
        scene_animation_debug_by_track = {}
        section_body_strip_entries = []

        def remember_section_nla_targets(target_armatures):
            for arm_obj in target_armatures or []:
                if arm_obj is None:
                    continue
                obj_name = str(getattr(arm_obj, "name", "") or "")
                target_key = obj_name or str(id(arm_obj))
                if target_key in section_nla_target_names:
                    continue
                section_nla_target_names.add(target_key)
                section_nla_targets.append(arm_obj)

        def _scene_bool_idprop(id_block, prop_name, default=False):
            if id_block is None:
                return default
            try:
                return bool(id_block.get(prop_name, default))
            except Exception:
                return default

        def _remember_body_strip_debug(arm_obj, track_name, strip, action, event, additive_body_blend, weight_curve_baked, weight_curve_edits, motion_result=None):
            if arm_obj is None or strip is None or not _w2scene_track_is_body_layer(track_name):
                return
            motion_result = motion_result or {}
            entry = {
                "armature": getattr(arm_obj, "name", ""),
                "track": track_name,
                "strip": getattr(strip, "name", ""),
                "action": getattr(action, "name", ""),
                "event": event.__class__.__name__ if event is not None else "",
                "eventName": getattr(event, "eventName", "") if event is not None else "",
                "animation": getattr(event, "animationName", "") if event is not None else "",
                "frameStart": float(getattr(strip, "frame_start", 0.0) or 0.0),
                "frameEnd": float(getattr(strip, "frame_end", 0.0) or 0.0),
                "blendType": str(getattr(strip, "blend_type", "") or ""),
                "influence": float(getattr(strip, "influence", 0.0) or 0.0),
                "additiveBlend": bool(additive_body_blend),
                "localAdditive": _scene_bool_idprop(action, W2SCENE_ACTION_ADDITIVE_CONVERT_PROP, False),
                "rootClean": _scene_bool_idprop(action, W2SCENE_ACTION_ROOT_ORIENTATION_PROP, False),
                "weightCurveBaked": bool(weight_curve_baked),
                "weightCurveEdits": int(weight_curve_edits or 0),
                "motionExtraction": _w2scene_event_uses_motion_extraction(event),
                "fakeMotion": _w2scene_event_uses_fake_motion(event),
                "sceneMotionActor": bool(motion_result.get("motionEventAdded", False)),
                "sceneMotionPosePolicy": str(motion_result.get("posePolicy", "") or ""),
                "sceneMotionPoseNeutralized": bool(motion_result.get("poseNeutralized", False)),
                "animationType": _w2scene_event_animation_type(event),
                "convertToAdditive": _w2scene_event_convert_to_additive(event) if additive_body_blend else False,
                "additiveType": _w2scene_event_additive_type(event) if additive_body_blend else "",
            }
            section_body_strip_entries.append(entry)

        def _log_body_strip_overlap_debug():
            entries = sorted(
                section_body_strip_entries,
                key=lambda item: (str(item.get("armature", "")), float(item.get("frameStart", 0.0)), float(item.get("frameEnd", 0.0))),
            )
            import_scene_animation.warn_scene_animation_debug(
                "section body strip summary",
                section=self._section_name,
                details={
                    "bodyStrips": len(entries),
                    "targets": len({str(item.get("armature", "")) for item in entries}),
                },
            )
            for entry in entries:
                import_scene_animation.warn_scene_animation_debug(
                    "body strip",
                    section=self._section_name,
                    track_name=entry.get("track", ""),
                    details={
                        "actor": entry.get("armature", ""),
                        "event": entry.get("eventName", "") or entry.get("event", ""),
                        "animation": entry.get("animation", ""),
                        "frames": f"{round(entry.get('frameStart', 0.0), 3)}-{round(entry.get('frameEnd', 0.0), 3)}",
                        "blend": entry.get("blendType", ""),
                        "influence": round(float(entry.get("influence", 0.0) or 0.0), 6),
                        "animationType": entry.get("animationType", ""),
                        "additive": bool(entry.get("additiveBlend", False)),
                        "additiveType": entry.get("additiveType", ""),
                        "convertToAdditive": bool(entry.get("convertToAdditive", False)),
                        "localAdditive": bool(entry.get("localAdditive", False)),
                        "rootClean": bool(entry.get("rootClean", False)),
                        "weightBaked": bool(entry.get("weightCurveBaked", False)),
                        "weightEdits": int(entry.get("weightCurveEdits", 0) or 0),
                        "motionExtraction": bool(entry.get("motionExtraction", False)),
                        "fakeMotion": bool(entry.get("fakeMotion", False)),
                        "actorMotion": bool(entry.get("sceneMotionActor", False)),
                        "posePolicy": entry.get("sceneMotionPosePolicy", ""),
                        "poseNeutralized": bool(entry.get("sceneMotionPoseNeutralized", False)),
                    },
                )
            for index, left in enumerate(entries):
                for right in entries[index + 1:]:
                    if left.get("armature", "") != right.get("armature", ""):
                        continue
                    overlap_start = max(float(left.get("frameStart", 0.0)), float(right.get("frameStart", 0.0)))
                    overlap_end = min(float(left.get("frameEnd", 0.0)), float(right.get("frameEnd", 0.0)))
                    if overlap_end <= overlap_start:
                        continue
                    both_combine = str(left.get("blendType", "")).upper() == "COMBINE" and str(right.get("blendType", "")).upper() == "COMBINE"
                    missing_local = (
                        (bool(left.get("additiveBlend", False)) and not bool(left.get("localAdditive", False)))
                        or (bool(right.get("additiveBlend", False)) and not bool(right.get("localAdditive", False)))
                    )
                    import_scene_animation.warn_scene_animation_debug(
                        "body strip overlap",
                        section=self._section_name,
                        details={
                            "actor": left.get("armature", ""),
                            "overlapFrames": round(overlap_end - overlap_start, 3),
                            "range": f"{round(overlap_start, 3)}-{round(overlap_end, 3)}",
                            "left": left.get("eventName", "") or left.get("animation", ""),
                            "leftAction": left.get("action", ""),
                            "leftBlend": left.get("blendType", ""),
                            "leftLocalAdditive": bool(left.get("localAdditive", False)),
                            "right": right.get("eventName", "") or right.get("animation", ""),
                            "rightAction": right.get("action", ""),
                            "rightBlend": right.get("blendType", ""),
                            "rightLocalAdditive": bool(right.get("localAdditive", False)),
                            "bothCombine": both_combine,
                            "missingLocalAdditive": missing_local,
                        },
                    )

        def ensure_scene_animation_track(target_obj, track_name):
            if target_obj is None or not track_name:
                return
            try:
                anim_data = target_obj.animation_data_create()
                tracks = anim_data.nla_tracks
                if tracks.get(track_name) is not None:
                    return
                track = tracks.new()
                track.name = track_name
            except Exception:
                log.debug("Could not pre-create .w2scene NLA track %s", track_name, exc_info=True)

        def get_event_camera_pose(event):
            return resolve_w2scene_event_camera_pose(
                event,
                camera_definitions,
                place_object=placeCube,
                focus_targets=camera_focus_targets,
            )

        def get_event_start_frame(shot, event):
            return float(frame_offset) + self._event_start_frame(event, fps=__fps, fallback_dialogscript=shot.get('dialogscript'))

        def extend_nla_track_to_frame(target_obj, track_name, end_frame, cycle=False):
            if target_obj is None or target_obj.animation_data is None:
                return
            target_track = target_obj.animation_data.nla_tracks.get(track_name)
            if target_track is None or not target_track.strips:
                return
            target_strip = max(target_track.strips, key=lambda strip: float(getattr(strip, "frame_start", 0.0) or 0.0))
            strip_start = float(getattr(target_strip, "frame_start", 0.0) or 0.0)
            if end_frame <= strip_start:
                return
            action = getattr(target_strip, "action", None)
            action_length = 0.0
            if action is not None:
                try:
                    action_start, action_end = action.frame_range
                    action_length = float(action_end) - float(action_start)
                except Exception:
                    action_length = 0.0
            if cycle and action_length > 0.0 and hasattr(target_strip, "repeat"):
                try:
                    if hasattr(target_strip, "action_frame_start"):
                        target_strip.action_frame_start = float(action_start)
                    if hasattr(target_strip, "action_frame_end"):
                        target_strip.action_frame_end = float(action_end)
                    if hasattr(target_strip, "scale"):
                        target_strip.scale = 1.0
                    target_strip.repeat = max(
                        float(getattr(target_strip, "repeat", 1.0) or 1.0),
                        (float(end_frame) - strip_start) / action_length,
                    )
                except Exception:
                    pass
            try:
                target_strip.extrapolation = 'NOTHING'
            except Exception:
                pass
            try:
                target_strip.frame_end = end_frame
            except Exception:
                pass
            import_scene_animation.warn_scene_animation_edit(
                "extended NLA strip to section duration",
                action=action,
                strip=target_strip,
                armature_obj=target_obj,
                section=self._section_name,
                track_name=track_name,
                details={
                    "frameStart": round(strip_start, 3),
                    "frameEnd": round(float(end_frame), 3),
                    "cycle": bool(cycle),
                    "repeat": round(float(getattr(target_strip, "repeat", 1.0) or 1.0), 6),
                },
            )

        def scene_debug_track_names(target_obj):
            try:
                tracks = getattr(getattr(target_obj, "animation_data", None), "nla_tracks", None)
                if tracks is None:
                    return ""
                names = [
                    str(getattr(track, "name", "") or "")
                    for track in list(tracks)
                    if _nla_track_name_matches(
                        getattr(track, "name", ""),
                        track_names=W2SCENE_SECTION_NLA_TRACK_NAMES,
                        track_prefixes=W2SCENE_SECTION_NLA_TRACK_PREFIXES,
                    )
                ]
                return ",".join(names[:12])
            except Exception:
                return ""

        def annotate_loaded_scene_animation(target_armatures, track_name, at_frame, metadata, event=None):
            annotated_count = 0
            for arm_obj in list(target_armatures or []):
                if arm_obj is None or getattr(arm_obj, "animation_data", None) is None:
                    continue
                track = arm_obj.animation_data.nla_tracks.get(track_name)
                if track is None or not track.strips:
                    continue
                strip = min(track.strips, key=lambda s: abs(float(getattr(s, 'frame_start', 0.0) or 0.0) - float(at_frame)))
                action = getattr(strip, "action", None)
                annotated_count += 1
                _set_w2scene_import_metadata((action, strip), metadata)
                _remember_w2scene_event_guid_metadata(getattr(bpy.context, "scene", None), metadata)
                if action is not None and _w2scene_track_is_body_layer(track_name):
                    try:
                        _apply_w2scene_root_orientation_to_action(
                            action,
                            arm_obj,
                            event=event,
                            section=self._section_name,
                            track_name=track_name,
                            strip=strip,
                        )
                    except Exception:
                        log.debug(
                            "Could not apply scene root orientation cleanup to loaded scene animation %s",
                            metadata.get("w3_scene_requested_animation", ""),
                            exc_info=True,
                        )
            return annotated_count

        def load_scene_animation_by_name(anim_name, actor_obj, track_name, at_frame, face_target_mode="auto", show_all=False, extend_to_frame=None, compatible_only=True, cycle=False, event=None):
            if actor_obj is None or not anim_name:
                return []
            try:
                SetupActor(actor_obj, show_all=show_all)
                prefer_mimic_lookup = face_target_mode == "owner"
                resolved_anim_name, fdir = GetAnimationInfoByName(
                    anim_name,
                    actor_obj,
                    show_all=show_all,
                    prefer_mimic=prefer_mimic_lookup,
                    compatible_only=compatible_only,
                )
                if not resolved_anim_name or not fdir:
                    import_scene_animation.warn_scene_animation_skip(
                        "animation lookup failed",
                        event=event,
                        section=self._section_name,
                        track_name=track_name,
                        details={
                            "requested": anim_name,
                            "actorObject": getattr(actor_obj, "name", ""),
                            "compatibleOnly": bool(compatible_only),
                            "showAll": bool(show_all),
                            "preferMimic": bool(prefer_mimic_lookup),
                        },
                    )
                    log.warning("Skipping scene animation '%s'; animation was not found", anim_name)
                    return []
                lookup_metadata = {
                    "w3_scene_file": self._scene_filepath,
                    "w3_scene_section": self._section_name,
                    "w3_scene_requested_animation": anim_name,
                    "w3_scene_resolved_animation": resolved_anim_name,
                    "w3_scene_resolved_path": fdir,
                    "w3_scene_actor_object": getattr(actor_obj, "name", ""),
                    "w3_scene_track_name": track_name,
                    "w3_scene_event_class": event.__class__.__name__ if event is not None else "",
                    "w3_scene_event_guid": get_w2scene_event_guid_string(event) if event is not None else "",
                    "w3_scene_event_name": getattr(event, "eventName", "") if event is not None else "",
                    "w3_scene_actor": getattr(event, "actor", None) or getattr(event, "actorName", None) or "",
                }
                scene_animation_debug_by_track[track_name] = lookup_metadata
                import_scene_animation.warn_scene_animation_lookup(
                    requested=anim_name,
                    resolved=resolved_anim_name,
                    path=fdir,
                    armature_obj=actor_obj,
                    event=event,
                    section=self._section_name,
                    track_name=track_name,
                    frame=round(float(at_frame), 3),
                )
                if _w2scene_track_is_body_layer(track_name):
                    ensure_scene_animation_track(actor_obj, track_name)
                target_armatures = load_anim_into_scene(
                    bpy.context,
                    resolved_anim_name,
                    fdir,
                    actor_obj,
                    track_name,
                    at_frame=at_frame,
                    face_target_mode=face_target_mode,
                )
                effective_armatures = list(target_armatures or [actor_obj])
                remember_section_nla_targets(effective_armatures)
                annotated_count = annotate_loaded_scene_animation(effective_armatures, track_name, at_frame, lookup_metadata, event=event)
                if annotated_count <= 0:
                    import_scene_animation.warn_scene_animation_skip(
                        "animation loaded but expected NLA strip was not found",
                        event=event,
                        section=self._section_name,
                        track_name=track_name,
                        details={
                            "requested": anim_name,
                            "resolved": resolved_anim_name,
                            "actorObject": getattr(actor_obj, "name", ""),
                            "returnedTargets": len(effective_armatures),
                            "sceneTracks": scene_debug_track_names(actor_obj),
                        },
                    )
                if extend_to_frame is not None:
                    for target_armature in effective_armatures:
                        extend_nla_track_to_frame(target_armature, track_name, extend_to_frame, cycle=cycle)
                return effective_armatures
            except Exception:
                log.warning(
                    "Failed to load scene animation '%s' on '%s'",
                    anim_name,
                    getattr(actor_obj, "name", "<unknown>"),
                    exc_info=True,
                )
            return []

        def apply_mimic_layer_strip_weights(
            target_armatures,
            track_name,
            at_frame,
            scene_weight=1.0,
            pose_weight=None,
            event=None,
            blend_in_frames=0.0,
            blend_out_frames=0.0,
            convert_transform_to_additive=False,
            additive_reference_frame=0.0,
            metadata=None,
        ):
            try:
                scene_weight = max(0.0, min(1.0, float(scene_weight)))
            except Exception:
                scene_weight = 1.0
            if pose_weight is not None:
                try:
                    pose_weight = max(0.0, min(1.0, float(pose_weight)))
                except Exception:
                    pose_weight = None
            changed = 0
            for arm_obj in target_armatures or []:
                if arm_obj is None or getattr(arm_obj, "animation_data", None) is None:
                    continue
                track = arm_obj.animation_data.nla_tracks.get(track_name)
                if track is None or not track.strips:
                    continue
                strip = min(track.strips, key=lambda s: abs(float(getattr(s, "frame_start", 0.0) or 0.0) - float(at_frame)))
                action = getattr(strip, "action", None)
                transform_edits = 0
                transform_additive = 0
                if convert_transform_to_additive:
                    try:
                        transform_additive = int(bool(_prepare_w2scene_mimic_overlay_strip_action(
                            strip,
                            arm_obj,
                            event=event,
                            section=self._section_name,
                            track_name=track_name,
                            reference_frame=additive_reference_frame,
                        )))
                    except Exception:
                        log.debug("Could not convert mimic layer transform curves to local additive for %s", track_name, exc_info=True)
                    action = getattr(strip, "action", action)
                if _w2scene_action_has_pose_transform_curves(action, arm_obj, getattr(strip, "action_slot", None)):
                    try:
                        result = import_scene_animation.apply_scene_weight_to_strip(
                            strip,
                            arm_obj,
                            scene_weight,
                            event=event,
                            section=self._section_name,
                            track_name=track_name,
                        )
                        transform_edits = int((result or {}).get("changed", 0) or 0)
                    except Exception:
                        log.debug("Could not bake mimic layer transform weight for %s", track_name, exc_info=True)
                try:
                    action_start, action_end = (0.0, 0.0)
                    if action is not None:
                        action_start, action_end = action.frame_range
                    action_frame_start = float(getattr(strip, "action_frame_start", action_start) or action_start)
                    action_frame_end = float(getattr(strip, "action_frame_end", action_end) or action_end)
                    mimic_weight_fn = _w2scene_mimic_float_anim_weight_fn(
                        scene_weight,
                        action_frame_start,
                        max(1e-6, action_frame_end - action_frame_start),
                        blend_in_frames,
                        blend_out_frames,
                    )
                    mimic_edits = apply_mimic_scene_weight_to_strip(strip, scene_weight, weight_fn=mimic_weight_fn, target=arm_obj)
                except Exception:
                    log.debug("Could not bake mimic layer float weight for %s", track_name, exc_info=True)
                    mimic_edits = 0
                try:
                    pose_edits = apply_mimic_pose_weight_to_strip(strip, pose_weight) if pose_weight is not None else 0
                except Exception:
                    log.debug("Could not bake mimic layer pose weight for %s", track_name, exc_info=True)
                    pose_edits = 0
                action = getattr(strip, "action", action)
                total_edits = int(transform_edits or 0) + int(mimic_edits or 0) + int(pose_edits or 0)
                changed += total_edits
                strip_duration = max(0.0, float(getattr(strip, "frame_end", at_frame) or at_frame) - float(getattr(strip, "frame_start", at_frame) or at_frame))
                try:
                    strip.extrapolation = 'NOTHING'
                    strip.blend_type = 'COMBINE'
                    strip.influence = 1.0 if total_edits else scene_weight
                    if hasattr(strip, 'use_auto_blend'):
                        strip.use_auto_blend = False
                    strip.blend_in = min(max(0.0, float(blend_in_frames)), strip_duration) if strip_duration else max(0.0, float(blend_in_frames))
                    strip.blend_out = min(max(0.0, float(blend_out_frames)), strip_duration) if strip_duration else max(0.0, float(blend_out_frames))
                    strip[W2SCENE_NLA_STRIP_BLEND_TYPE_PROP] = 'COMBINE'
                    strip[W2SCENE_EVENT_WEIGHT_PROP] = float(scene_weight)
                    strip[W2SCENE_EVENT_WEIGHT_APPLIED_PROP] = True
                    strip["w3_scene_mimic_pose_weight"] = float(pose_weight) if pose_weight is not None else 1.0
                    strip["w3_scene_mimic_weight_curve_edits"] = int(total_edits)
                    if action is not None:
                        action[W2SCENE_ACTION_BLEND_TYPE_PROP] = 'COMBINE'
                except Exception:
                    log.debug("Could not configure mimic layer strip %s", track_name, exc_info=True)
                strip_metadata = dict(metadata or {})
                strip_metadata.update({
                    "w3_scene_blend_type": "COMBINE",
                    W2SCENE_EVENT_WEIGHT_PROP: float(scene_weight),
                    "w3_scene_mimic_pose_weight": float(pose_weight) if pose_weight is not None else 1.0,
                    "w3_scene_mimic_weight_curve_edits": int(total_edits),
                    "w3_scene_mimic_local_additive": bool(transform_additive),
                })
                _set_w2scene_import_metadata((action, strip), strip_metadata)
            return changed

        def load_scene_mimic_layer(
            actor_obj,
            layer_value,
            layer_column,
            track_name,
            at_frame,
            scene_weight=1.0,
            pose_weight=None,
            event=None,
            extend_to_frame=None,
            blend_in_frames=0.0,
            blend_out_frames=0.0,
            exact_animation=False,
            show_all=False,
            convert_transform_to_additive=False,
            additive_reference_frame=0.0,
            source="",
        ):
            layer_value = _cname_text(layer_value)
            if actor_obj is None or not layer_value:
                return []
            SetupActor(actor_obj, show_all=show_all)
            candidates = [layer_value] if exact_animation else _resolve_mimic_layer_anim_candidates(layer_value, layer_column)
            resolved_cand = None
            for cand in candidates:
                _name, _fdir = GetAnimationInfoByName(
                    cand,
                    actor_obj,
                    show_all=show_all,
                    prefer_mimic=True,
                    compatible_only=False,
                    quiet=True,
                )
                if _name and _fdir:
                    resolved_cand = cand
                    break
            if not resolved_cand:
                import_scene_animation.warn_scene_animation_skip(
                    "mimic layer animation lookup failed",
                    event=event,
                    section=self._section_name,
                    track_name=track_name,
                    details={
                        "layer": layer_column,
                        "value": layer_value,
                        "actorObject": getattr(actor_obj, "name", ""),
                        "source": source,
                        "candidates": ",".join(candidates[:8]),
                    },
                )
                log.warning(
                    "No mimic animation found for layer %s='%s' on actor '%s' (tried: %s)",
                    layer_column, layer_value, getattr(actor_obj, "name", "?"), ", ".join(candidates),
                )
                return []
            target_armatures = load_scene_animation_by_name(
                resolved_cand,
                actor_obj,
                track_name,
                float(at_frame),
                face_target_mode="owner",
                show_all=show_all,
                extend_to_frame=extend_to_frame,
                compatible_only=False,
                event=event,
            )
            if target_armatures:
                metadata = {
                    "w3_scene_mimic_layer": layer_column,
                    "w3_scene_mimic_layer_value": layer_value,
                    "w3_scene_mimic_layer_source": source,
                    "w3_scene_mimic_layer_resolved": resolved_cand,
                }
                apply_mimic_layer_strip_weights(
                    target_armatures,
                    track_name,
                    float(at_frame),
                    scene_weight=scene_weight,
                    pose_weight=pose_weight,
                    event=event,
                    blend_in_frames=blend_in_frames,
                    blend_out_frames=blend_out_frames,
                    convert_transform_to_additive=convert_transform_to_additive,
                    additive_reference_frame=additive_reference_frame,
                    metadata=metadata,
                )
            return target_armatures

        def apply_anim_clip_event_strip_props(target_armatures, track_name, at_frame, event):
            weight = _as_float(getattr(event, 'weight', None), W2SCENE_ANIM_CLIP_DEFAULT_WEIGHT)
            weight = max(0.0, min(1.0, weight))
            blend_in_frames = max(
                0.0,
                _as_float(getattr(event, 'blendIn', None), W2SCENE_ANIM_CLIP_DEFAULT_BLEND_IN) * __fps,
            )
            blend_out_frames = max(
                0.0,
                _as_float(getattr(event, 'blendOut', None), W2SCENE_ANIM_CLIP_DEFAULT_BLEND_OUT) * __fps,
            )
            blend_type = _w2scene_strip_blend_type(track_name, event)
            clip_front_frames = max(
                0.0,
                _as_float(getattr(event, 'clipFront', None), W2SCENE_ANIM_CLIP_DEFAULT_CLIP_FRONT) * __fps,
            )
            clip_end_seconds = _as_float(getattr(event, 'clipEnd', None), W2SCENE_ANIM_CLIP_DEFAULT_CLIP_END)
            clip_end_frames = clip_end_seconds * __fps if clip_end_seconds >= 0.0 else None
            stretch = _as_float(getattr(event, 'stretch', None), W2SCENE_ANIM_CLIP_DEFAULT_STRETCH)
            if stretch <= 0.0:
                stretch = W2SCENE_ANIM_CLIP_DEFAULT_STRETCH
            event_duration_frames = max(0.0, _as_float(getattr(event, 'duration', None), 0.0) * __fps)

            for arm_obj in (target_armatures or []):
                if arm_obj is None or arm_obj.animation_data is None:
                    continue
                track = arm_obj.animation_data.nla_tracks.get(track_name)
                if track is None or not track.strips:
                    import_scene_animation.warn_scene_animation_skip(
                        "cannot configure animation event because expected NLA strip is missing",
                        event=event,
                        section=self._section_name,
                        track_name=track_name,
                        details={
                            "actorObject": getattr(arm_obj, "name", ""),
                            "sceneTracks": scene_debug_track_names(arm_obj),
                        },
                    )
                    continue
                strip = min(track.strips, key=lambda s: abs(float(getattr(s, 'frame_start', 0.0) or 0.0) - float(at_frame)))
                try:
                    action = getattr(strip, 'action', None)
                    additive_body_blend = _w2scene_event_uses_additive_body_blend(event)
                    mimic_blend = _w2scene_track_is_mimic_layer(track_name)
                    try:
                        strip.name = _w2scene_event_strip_name(event)
                    except Exception:
                        pass
                    early_metadata = dict(scene_animation_debug_by_track.get(track_name, {}) or {})
                    early_metadata.update({
                        "w3_scene_file": self._scene_filepath,
                        "w3_scene_section": self._section_name,
                        "w3_scene_event_class": event.__class__.__name__,
                        "w3_scene_event_guid": get_w2scene_event_guid_string(event),
                        "w3_scene_event_name": getattr(event, "eventName", ""),
                        "w3_scene_actor": getattr(event, "actor", None) or getattr(event, "actorName", None) or "",
                        "w3_scene_animation_type": _w2scene_event_animation_type(event),
                        "w3_scene_event_start_frame": float(at_frame),
                        W2SCENE_EVENT_WEIGHT_PROP: float(weight),
                        "w3_scene_event_weight_source": "m_weight",
                        "w3_scene_blend_type": blend_type,
                    })
                    _set_w2scene_import_metadata((action, strip), early_metadata)
                    _remember_w2scene_event_guid_metadata(getattr(bpy.context, "scene", None), early_metadata)
                    if action is not None and _w2scene_track_is_body_layer(track_name):
                        try:
                            _apply_w2scene_root_orientation_to_action(
                                action,
                                arm_obj,
                                event=event,
                                section=self._section_name,
                                track_name=track_name,
                                strip=strip,
                            )
                        except Exception:
                            log.debug(
                                "Could not apply scene root orientation cleanup to %s",
                                getattr(event, "animationName", None),
                                exc_info=True,
                            )
                    if action is not None:
                        action_start, action_end = action.frame_range
                    else:
                        action_start, action_end = 0.0, 0.0
                    action_start = float(action_start)
                    action_end = float(action_end)
                    motion_result = None
                    action_frame_start = action_start + clip_front_frames
                    if clip_end_frames is not None:
                        # REDengine clipEnd is an absolute animation end time in seconds,
                        # not a number of seconds to trim from the strip tail.
                        action_frame_end = action_start + clip_end_frames
                    else:
                        action_frame_end = float(getattr(strip, 'action_frame_end', action_end) or action_end)
                    if action_frame_end <= action_frame_start:
                        if event_duration_frames > 0.0:
                            action_frame_end = action_frame_start + (event_duration_frames / stretch)
                        else:
                            strip_len = (
                                float(getattr(strip, 'frame_end', at_frame) or at_frame)
                                - float(getattr(strip, 'frame_start', at_frame) or at_frame)
                            )
                            action_frame_end = action_frame_start + max(1.0, strip_len)
                    action_frame_len = max(1e-6, action_frame_end - action_frame_start)
                    if event_duration_frames > 0.0:
                        strip_duration_frames = event_duration_frames
                    else:
                        strip_duration_frames = action_frame_len * stretch
                    strip_duration_frames = max(1.0, strip_duration_frames)
                    weight_curve_result = None
                    mimic_weight_curve_edits = 0
                    mimic_local_additive = False
                    if additive_body_blend:
                        try:
                            _prepare_w2scene_local_additive_strip_action(
                                strip,
                                arm_obj,
                                event,
                                section=self._section_name,
                                track_name=track_name,
                            )
                        except Exception:
                            log.debug(
                                "Could not preprocess additive scene animation %s",
                                getattr(event, "animationName", None),
                                exc_info=True,
                            )
                        try:
                            weight_curve_result = import_scene_animation.apply_scene_weight_to_strip(
                                strip,
                                arm_obj,
                                weight,
                                event=event,
                                section=self._section_name,
                                track_name=track_name,
                            )
                        except Exception:
                            log.debug(
                                "Could not bake scene animation weight %s into %s",
                                weight,
                                getattr(event, "animationName", None),
                                exc_info=True,
                            )
                    elif mimic_blend and _w2scene_action_has_pose_transform_curves(action, arm_obj, getattr(strip, "action_slot", None)):
                        try:
                            mimic_local_additive = bool(_prepare_w2scene_mimic_overlay_strip_action(
                                strip,
                                arm_obj,
                                event=event,
                                section=self._section_name,
                                track_name=track_name,
                                reference_frame=action_frame_start,
                            ))
                        except Exception:
                            log.debug(
                                "Could not preprocess mimic overlay animation %s",
                                getattr(event, "animationName", None),
                                exc_info=True,
                            )
                        action = getattr(strip, 'action', action)
                        try:
                            weight_curve_result = import_scene_animation.apply_scene_weight_to_strip(
                                strip,
                                arm_obj,
                                weight,
                                event=event,
                                section=self._section_name,
                                track_name=track_name,
                            )
                        except Exception:
                            log.debug(
                                "Could not bake mimic transform scene weight %s into %s",
                                weight,
                                getattr(event, "animationName", None),
                                exc_info=True,
                            )
                    if mimic_blend:
                        try:
                            mimic_weight_fn = _w2scene_mimic_float_anim_weight_fn(
                                weight,
                                action_frame_start,
                                action_frame_len,
                                blend_in_frames,
                                blend_out_frames,
                            )
                            mimic_weight_curve_edits = apply_mimic_scene_weight_to_strip(strip, weight, weight_fn=mimic_weight_fn, target=arm_obj)
                        except Exception:
                            log.debug(
                                "Could not bake mimic float scene weight %s into %s",
                                weight,
                                getattr(event, "animationName", None),
                                exc_info=True,
                            )
                    action = getattr(strip, 'action', None)
                    weight_curve_edits = int((weight_curve_result or {}).get("changed", 0) or 0) + int(mimic_weight_curve_edits or 0)
                    weight_curve_baked = weight_curve_edits > 0
                    mimic_local_additive_applied = bool(mimic_local_additive)
                    if action is not None and not mimic_local_additive_applied:
                        try:
                            mimic_local_additive_applied = bool(action.get("w3_scene_mimic_local_additive", False))
                        except Exception:
                            pass
                    if action is not None:
                        try:
                            action[W2SCENE_ACTION_BLEND_IN_PROP] = float(blend_in_frames)
                            action[W2SCENE_ACTION_BLEND_OUT_PROP] = float(blend_out_frames)
                        except Exception:
                            pass
                    metadata = dict(scene_animation_debug_by_track.get(track_name, {}) or {})
                    metadata.update({
                        "w3_scene_file": self._scene_filepath,
                        "w3_scene_section": self._section_name,
                        "w3_scene_event_class": event.__class__.__name__,
                        "w3_scene_event_guid": get_w2scene_event_guid_string(event),
                        "w3_scene_event_name": getattr(event, "eventName", ""),
                        "w3_scene_actor": getattr(event, "actor", None) or getattr(event, "actorName", None) or "",
                        "w3_scene_animation_type": _w2scene_event_animation_type(event),
                        "w3_scene_additive_type": _w2scene_event_additive_type(event) if additive_body_blend else "",
                        "w3_scene_convert_to_additive": _w2scene_event_convert_to_additive(event) if additive_body_blend else False,
                        "w3_scene_use_motion_extraction": _w2scene_event_uses_motion_extraction(event),
                        "w3_scene_use_fake_motion": _w2scene_event_uses_fake_motion(event),
                        "w3_scene_motion_extraction_applied_to_pose": False,
                        "w3_scene_event_start_frame": float(at_frame),
                        "w3_scene_event_duration_frames": float(strip_duration_frames),
                        W2SCENE_EVENT_WEIGHT_PROP: float(weight),
                        "w3_scene_event_weight_source": "m_weight",
                        "w3_scene_event_weight_curve_baked": bool(weight_curve_baked),
                        "w3_scene_event_weight_curve_edits": int(weight_curve_edits),
                        "w3_scene_mimic_weight_curve_edits": int(mimic_weight_curve_edits or 0),
                        "w3_scene_mimic_local_additive": bool(mimic_local_additive_applied),
                        "w3_scene_blend_type": blend_type,
                        "w3_scene_blend_in_frames": float(blend_in_frames),
                        "w3_scene_blend_out_frames": float(blend_out_frames),
                        "w3_scene_clip_front_frames": float(clip_front_frames),
                        "w3_scene_clip_end_seconds": float(clip_end_seconds),
                        "w3_scene_stretch": float(stretch),
                        "w3_scene_trajectory_extracted": bool(action.get(W2SCENE_ACTION_TRAJECTORY_EXTRACTED_PROP, False)) if action is not None else False,
                        W2SCENE_ACTION_TRAJECTORY_SOURCE_PROP: action.get(W2SCENE_ACTION_TRAJECTORY_SOURCE_PROP, "") if action is not None else "",
                        "w3_scene_additive_converted": bool(action.get(W2SCENE_ACTION_ADDITIVE_CONVERT_PROP, False)) if action is not None else False,
                        W2SCENE_ACTION_ROOT_ORIENTATION_PROP: bool(action.get(W2SCENE_ACTION_ROOT_ORIENTATION_PROP, False)) if action is not None else False,
                    })
                    _set_w2scene_import_metadata((action, strip), metadata)
                    _remember_w2scene_event_guid_metadata(getattr(bpy.context, "scene", None), metadata)

                    # Set weight on strip first so it persists even if frame assignments below throw.
                    try:
                        strip.influence = 1.0 if weight_curve_baked else max(0.0, min(1.0, float(weight)))
                        log.debug("Scene anim weight set: event=%r anim=%r weight=%.4f",
                                  getattr(event, "eventName", ""), getattr(event, "animationName", ""), weight)
                        # Mark applied only after successful influence assignment.
                        for _wb in (strip, action):
                            if _wb is not None:
                                try:
                                    _wb[W2SCENE_EVENT_WEIGHT_APPLIED_PROP] = True
                                except Exception:
                                    pass
                    except Exception:
                        log.debug("Could not set strip.influence for %s", getattr(event, "animationName", None), exc_info=True)
                    # NOTHING: strip only contributes during its frame range.
                    # HOLD (default) causes every strip to bleed into all other frames,
                    # making stacked tracks fight each other constantly.
                    strip.extrapolation = 'NOTHING'
                    if hasattr(strip, 'use_auto_blend'):
                        strip.use_auto_blend = False
                    strip.frame_start = float(at_frame)
                    strip.frame_end = float(at_frame) + strip_duration_frames
                    if hasattr(strip, 'action_frame_start'):
                        strip.action_frame_start = action_frame_start
                    if hasattr(strip, 'action_frame_end'):
                        strip.action_frame_end = action_frame_end
                    if hasattr(strip, 'scale'):
                        strip.scale = strip_duration_frames / action_frame_len
                    if blend_type:
                        strip.blend_type = blend_type
                        if action is not None:
                            try:
                                action[W2SCENE_ACTION_BLEND_TYPE_PROP] = blend_type
                            except Exception:
                                pass
                    strip.blend_in = min(blend_in_frames, strip_duration_frames)
                    strip.blend_out = min(blend_out_frames, strip_duration_frames)
                    if action is not None and _w2scene_track_is_body_layer(track_name):
                        try:
                            motion_result = import_scene_motion.process_body_strip_motion(
                                scene_motion_accumulator,
                                arm_obj,
                                arm_obj,
                                strip,
                                event=event,
                                section=self._section_name,
                                track_name=track_name,
                                section_start_frame=float(frame_offset),
                                section_end_frame=section_end_frame,
                            )
                            action = getattr(strip, 'action', action)
                            if action is not None and motion_result is not None:
                                _set_w2scene_import_metadata((action, strip), {
                                    "w3_scene_motion_extraction_applied_to_pose": bool(motion_result.get("poseNeutralized", False)),
                                    "w3_scene_motion_pose_policy": str(motion_result.get("posePolicy", "") or ""),
                                })
                        except Exception:
                            log.debug(
                                "Could not process scene motion policy for %s",
                                getattr(event, "animationName", None),
                                exc_info=True,
                            )
                    import_scene_animation.warn_scene_animation_edit(
                        "configured NLA strip playback from scene animation event",
                        action=action,
                        strip=strip,
                        armature_obj=arm_obj,
                        event=event,
                        section=self._section_name,
                        track_name=track_name,
                        details={
                            "frameStart": round(float(getattr(strip, "frame_start", 0.0) or 0.0), 3),
                            "frameEnd": round(float(getattr(strip, "frame_end", 0.0) or 0.0), 3),
                            "actionStart": round(float(action_frame_start), 3),
                            "actionEnd": round(float(action_frame_end), 3),
                            "scale": round(float(getattr(strip, "scale", 1.0) or 1.0), 6),
                            "influence": round(float(getattr(strip, "influence", 1.0) or 0.0), 6),
                            "blendType": blend_type,
                            "blendIn": round(float(getattr(strip, "blend_in", 0.0) or 0.0), 3),
                            "blendOut": round(float(getattr(strip, "blend_out", 0.0) or 0.0), 3),
                        },
                    )
                    _remember_body_strip_debug(
                        arm_obj,
                        track_name,
                        strip,
                        action,
                        event,
                        additive_body_blend,
                        weight_curve_baked,
                        weight_curve_edits,
                        motion_result=motion_result,
                    )
                except Exception:
                    log.debug("Could not apply strip props from scene event %s", event.__class__.__name__, exc_info=True)

        def _dialogset_body_state_key(actor_obj):
            return getattr(actor_obj, "name", str(id(actor_obj)))

        def _update_dialogset_body_state_from_change_pose(actor_obj, event):
            state_key = _dialogset_body_state_key(actor_obj)
            state = dict(dialogset_body_state_by_actor.get(state_key) or {})
            for attr_name, state_name in (
                ("status", "status"),
                ("emotionalState", "emotional_state"),
                ("poseName", "pose_name"),
            ):
                value = _cname_text(getattr(event, attr_name, None))
                if value:
                    state[state_name] = value
            dialogset_body_state_by_actor[state_key] = state
            return state

        def _next_dialogset_idle_change_track_name(actor_obj):
            state_key = _dialogset_body_state_key(actor_obj)
            index = int(dialogset_idle_change_counts_by_actor.get(state_key, 0) or 0) + 1
            dialogset_idle_change_counts_by_actor[state_key] = index
            return f"{W2SCENE_DIALOGSET_IDLE_CHANGE_TRACK_PREFIX}{index:02d}"

        def _change_pose_idle_start_frame(event, event_start_frame, transition_armatures=None, transition_track_name=""):
            event_start_frame = float(event_start_frame)
            fallback_frame = event_start_frame + max(
                0.0,
                _as_float(getattr(event, "duration", None), 0.0) * __fps,
            )
            if transition_track_name:
                for arm_obj in list(transition_armatures or []):
                    if arm_obj is None or getattr(arm_obj, "animation_data", None) is None:
                        continue
                    track = arm_obj.animation_data.nla_tracks.get(transition_track_name)
                    if track is None or not track.strips:
                        continue
                    try:
                        strip = min(
                            track.strips,
                            key=lambda s: abs(float(getattr(s, "frame_start", 0.0) or 0.0) - event_start_frame),
                        )
                        return max(event_start_frame, float(getattr(strip, "frame_end", fallback_frame) or fallback_frame))
                    except Exception:
                        log.debug("Could not read change-pose transition strip end frame.", exc_info=True)
            return fallback_frame

        def load_change_pose_dialogset_idle(event, actor_obj, at_frame):
            at_frame = float(at_frame)
            if at_frame >= float(section_end_frame) - 1e-4:
                return []
            state = _update_dialogset_body_state_from_change_pose(actor_obj, event)
            force_idle = _cname_text(getattr(event, "forceBodyIdleAnimation", None))
            if force_idle:
                idle_name = force_idle
                idle_source = "forceBodyIdleAnimation"
            else:
                idle_name = _lookup_dialogset_body_anim(
                    state.get("status", ""),
                    state.get("emotional_state", ""),
                    state.get("pose_name", ""),
                )
                idle_source = "status/emotionalState/poseName"
            if not idle_name:
                import_scene_animation.warn_scene_animation_skip(
                    "change-pose event resolved no follow-up dialogset idle",
                    event=event,
                    section=self._section_name,
                    details={
                        "status": state.get("status", ""),
                        "emotionalState": state.get("emotional_state", ""),
                        "poseName": state.get("pose_name", ""),
                        "forceBodyIdleAnimation": force_idle,
                    },
                )
                return []

            track_name = _next_dialogset_idle_change_track_name(actor_obj)
            loaded = load_scene_animation_by_name(
                idle_name,
                actor_obj,
                track_name,
                at_frame,
                extend_to_frame=section_end_frame,
                compatible_only=True,
                cycle=True,
                event=event,
            )
            metadata = {
                "w3_scene_change_pose_idle_source": idle_source,
                "w3_scene_change_pose_status": state.get("status", ""),
                "w3_scene_change_pose_emotional_state": state.get("emotional_state", ""),
                "w3_scene_change_pose_pose_name": state.get("pose_name", ""),
                "w3_scene_change_pose_idle_start_frame": float(at_frame),
            }
            for arm_obj in list(loaded or []):
                if arm_obj is None or getattr(arm_obj, "animation_data", None) is None:
                    continue
                track = arm_obj.animation_data.nla_tracks.get(track_name)
                if track is None or not track.strips:
                    continue
                strip = min(track.strips, key=lambda s: abs(float(getattr(s, "frame_start", 0.0) or 0.0) - float(at_frame)))
                action = getattr(strip, "action", None)
                _set_w2scene_import_metadata((action, strip), metadata)
                try:
                    strip.influence = 1.0
                    strip.extrapolation = 'NOTHING'
                    strip.blend_type = 'REPLACE'
                    if hasattr(strip, 'use_auto_blend'):
                        strip.use_auto_blend = False
                    if hasattr(strip, 'blend_in'):
                        strip.blend_in = 0.0
                    if hasattr(strip, 'blend_out'):
                        strip.blend_out = 0.0
                except Exception:
                    log.debug("Could not configure change-pose idle strip %s", track_name, exc_info=True)
            return loaded

        def _dialogset_mimic_state_key(actor_obj):
            return getattr(actor_obj, "name", str(id(actor_obj)))

        def _next_dialogset_mimic_change_track_name(actor_obj, layer_column, event):
            state_key = _dialogset_mimic_state_key(actor_obj)
            count_key = (state_key, str(layer_column or "mimic"))
            index = int(dialogset_mimic_change_counts_by_actor.get(count_key, 0) or 0) + 1
            dialogset_mimic_change_counts_by_actor[count_key] = index
            return _safe_nla_track_name(
                W2SCENE_DIALOGSET_MIMICS_CHANGE_TRACK_PREFIX,
                layer_column,
                f"{index:02d}",
                (get_w2scene_event_guid_string(event) or "")[:8],
            )

        def _event_mimic_layer_value(event, layer_attr, force_attr):
            forced = _cname_text(getattr(event, force_attr, None))
            if forced:
                return forced, force_attr, True
            value = _cname_text(getattr(event, layer_attr, None))
            if value:
                return value, layer_attr, False
            state = _cname_text(getattr(event, "mimicsEmotionalState", None))
            if state:
                return state, "mimicsEmotionalState", False
            return "", "", False

        def load_mimics_state_event(event, actor_obj, at_frame):
            if actor_obj is None:
                return []
            at_frame = float(at_frame)
            if at_frame >= float(section_end_frame) - 1e-4:
                return []
            scene_weight = max(
                0.0,
                min(1.0, _as_float(getattr(event, "weight", None), W2SCENE_ANIM_CLIP_DEFAULT_WEIGHT)),
            )
            pose_weight = max(0.0, min(1.0, _as_float(getattr(event, "mimicsPoseWeight", None), 1.0)))
            blend_in_frames = max(0.0, _as_float(getattr(event, "blendIn", None), 0.0) * __fps)
            loaded_armatures = []

            transition_anim = _cname_text(getattr(event, "transitionAnimation", None))
            if transition_anim:
                transition_track = _safe_nla_track_name(
                    "cutscene_import_mimic",
                    getattr(event, "actor", None),
                    transition_anim,
                    (get_w2scene_event_guid_string(event) or "")[:8],
                )
                loaded = load_scene_animation_by_name(
                    transition_anim,
                    actor_obj,
                    transition_track,
                    at_frame,
                    face_target_mode="owner",
                    show_all=True,
                    compatible_only=False,
                    event=event,
                )
                if loaded:
                    apply_anim_clip_event_strip_props(loaded, transition_track, at_frame, event)
                    loaded_armatures.extend(loaded)

            state_key = _dialogset_mimic_state_key(actor_obj)
            state = dict(dialogset_mimic_state_by_actor.get(state_key) or {})
            if _cname_text(getattr(event, "mimicsEmotionalState", None)):
                state["emotional_state"] = _cname_text(getattr(event, "mimicsEmotionalState", None))
            state["pose_weight"] = pose_weight

            for layer_attr, force_attr, layer_column in (
                ("mimicsLayer_Eyes", "forceMimicsIdleAnimation_Eyes", "eyes"),
                ("mimicsLayer_Pose", "forceMimicsIdleAnimation_Pose", "pose"),
                ("mimicsLayer_Animation", "forceMimicsIdleAnimation_Animation", "animation"),
            ):
                layer_value, source, exact = _event_mimic_layer_value(event, layer_attr, force_attr)
                if not layer_value:
                    continue
                track_name = _next_dialogset_mimic_change_track_name(actor_obj, layer_column, event)
                loaded = load_scene_mimic_layer(
                    actor_obj,
                    layer_value,
                    layer_column,
                    track_name,
                    at_frame,
                    scene_weight=scene_weight,
                    pose_weight=pose_weight if layer_column == "pose" else None,
                    event=event,
                    extend_to_frame=section_end_frame,
                    blend_in_frames=blend_in_frames,
                    blend_out_frames=0.0,
                    exact_animation=exact,
                    convert_transform_to_additive=True,
                    additive_reference_frame=0.0,
                    source=source,
                )
                if loaded:
                    state[layer_column] = layer_value
                    loaded_armatures.extend(loaded)

            dialogset_mimic_state_by_actor[state_key] = state
            if not loaded_armatures and not transition_anim:
                import_scene_animation.warn_scene_animation_skip(
                    "mimics state event resolved no mimic layer animation",
                    event=event,
                    section=self._section_name,
                    details={
                        "actorObject": getattr(actor_obj, "name", ""),
                        "mimicsEmotionalState": _cname_text(getattr(event, "mimicsEmotionalState", None)),
                        "mimicsLayerEyes": _cname_text(getattr(event, "mimicsLayer_Eyes", None)),
                        "mimicsLayerPose": _cname_text(getattr(event, "mimicsLayer_Pose", None)),
                        "mimicsLayerAnimation": _cname_text(getattr(event, "mimicsLayer_Animation", None)),
                    },
                )
            return loaded_armatures

        CustomCameraInstances = {}
        camera_interpolation_key_guids = set()
        camera_event_debug_counts = {
            "custom": 0,
            "customInstance": 0,
            "interpolation": 0,
            "interpolationKeyGuids": 0,
        }
        for section_event in getattr(self, "_section_scene_event_elements", []) or []:
            if not self._section_event_is_active(section_event, include_muted=True):
                continue
            if section_event.__class__.__name__ in {"CStorySceneEventCustomCamera", "CStorySceneEventCustomCameraInstance"}:
                guid_string = get_w2scene_event_guid_string(section_event)
                if guid_string:
                    CustomCameraInstances[guid_string] = section_event
                if section_event.__class__.__name__ == "CStorySceneEventCustomCameraInstance":
                    camera_event_debug_counts["customInstance"] += 1
                else:
                    camera_event_debug_counts["custom"] += 1
            if section_event.__class__.__name__ == "CStorySceneEventCameraInterpolation" and self._section_event_is_active(section_event):
                camera_event_debug_counts["interpolation"] += 1
                for guid in _iter_prop_values(getattr(section_event, "keyGuids", None)):
                    guid_string = _w2scene_guid_string(guid)
                    if guid_string:
                        camera_interpolation_key_guids.add(guid_string)
                        camera_event_debug_counts["interpolationKeyGuids"] += 1
        if any(camera_event_debug_counts.values()):
            import_scene_animation.warn_scene_animation_debug(
                "camera event prescan",
                section=self._section_name,
                armature_obj=scene_cam_obj,
                details={
                    "definitions": len(camera_definitions),
                    "custom": camera_event_debug_counts["custom"],
                    "customInstance": camera_event_debug_counts["customInstance"],
                    "interpolation": camera_event_debug_counts["interpolation"],
                    "interpolationKeys": camera_event_debug_counts["interpolationKeyGuids"],
                    "registeredCameraEvents": len(CustomCameraInstances),
                },
            )

        section_end_frame = float(frame_offset) + self._section_duration_seconds * __fps
        scene_motion_accumulator = import_scene_motion.SceneMotionAccumulator(
            section=self._section_name,
            section_start_frame=float(frame_offset),
            section_end_frame=section_end_frame,
        )
        for actor_obj, idle_name, actor_name, slot_name in dialogset_idle_animations:
            load_scene_animation_by_name(
                idle_name,
                actor_obj,
                W2SCENE_DIALOGSET_IDLE_TRACK_NAME,
                float(frame_offset),
                extend_to_frame=section_end_frame,
                compatible_only=True,
                cycle=True,
            )
        for actor_obj, layer_value, layer_column, mimic_track_name, pose_weight in dialogset_mimic_animations:
            load_scene_mimic_layer(
                actor_obj,
                layer_value,
                layer_column,
                mimic_track_name,
                float(frame_offset),
                scene_weight=1.0,
                pose_weight=pose_weight if layer_column == "pose" else None,
                extend_to_frame=section_end_frame,
                source="dialogset",
            )

        ###################
        #   RESET SCENE   #
        ###################
        #TODO delete all markers in the scene
        
        def remove_strips_from_track(scene_cam_obj, trackname):
            if scene_cam_obj.animation_data is None:
                log.debug("No animation data yet.")
                return
            if trackname in scene_cam_obj.animation_data.nla_tracks:
                track = scene_cam_obj.animation_data.nla_tracks[trackname]
                for strip in track.strips:
                    track.strips.remove(strip)
            else:
                log.debug("Track '%s' not found in the NLA Editor.", trackname)
        
        def reset_scene(scene_cam_obj):
            scene = bpy.context.scene
            for marker in scene.timeline_markers:
                scene.timeline_markers.remove(marker)
            if scene_cam_obj is not None and scene_cam_obj.animation_data is not None:
                try:
                    scene_cam_obj.animation_data.action = None
                except Exception:
                    pass
            clear_nla_tracks(
                scene_cam_obj,
                track_names=W2SCENE_CAMERA_NLA_TRACK_NAMES,
                track_prefixes=W2SCENE_CAMERA_NLA_TRACK_PREFIXES,
            )
        reset_scene(scene_cam_obj)

        class _Dummy: pass
        dummy_keyframe_points = iter(lambda: _Dummy, None)

        def create_scene_camera_action(action_name, key_data, strip_start_frame, interpolation='LINEAR'):
            if not key_data:
                return None
            bl_bone = scene_cam_obj.pose.bones.get(CAMERA_EDIT_BONE)
            camera_bone = ensure_camera_track_properties(scene_cam_obj, track_names=CAMERA_TRACK_NAMES)
            if bl_bone is None or camera_bone is None:
                log.warning("Skipping %s; scene camera rig is missing %s or %s", action_name, CAMERA_EDIT_BONE, CAMERA_CONTROL_BONE)
                return None

            action = bpy.data.actions.new(name=action_name)
            action_slot = resolve_action_slot(action, target=scene_cam_obj, ensure=True)
            pos_curves = [dummy_keyframe_points] * 3
            rot_curves = [dummy_keyframe_points] * 4

            prop_rot_map = {'QUATERNION':'rotation_quaternion', 'AXIS_ANGLE':'rotation_axis_angle'}
            data_path_rot = prop_rot_map.get(bl_bone.rotation_mode, 'rotation_quaternion')
            bone_rotation = getattr(bl_bone, data_path_rot)
            data_path = 'pose.bones["%s"].location'%bl_bone.name
            for axis_i in range(3):
                pos_curves[axis_i] = new_action_fcurve(action, scene_cam_obj, data_path=data_path, index=axis_i, group_name=bl_bone.name, slot=action_slot)
            data_path = 'pose.bones["%s"].%s'%(bl_bone.name, data_path_rot)
            for axis_i in range(len(bone_rotation)):
                rot_curves[axis_i] = new_action_fcurve(action, scene_cam_obj, data_path=data_path, index=axis_i, group_name=bl_bone.name, slot=action_slot)

            track_curves = {
                track_name: new_action_fcurve(
                    action,
                    scene_cam_obj,
                    data_path=f"pose.bones[\"{CAMERA_CONTROL_BONE}\"][\"{track_name}\"]",
                    slot=action_slot,
                )
                for track_name in CAMERA_TRACK_NAMES
            }

            previous_quaternion_values = None
            quaternion_sign_flips = 0
            for key_item in key_data:
                key_frame, camera_matrix, camera_tracks, _event = key_item[:4]
                key_interpolation = interpolation
                if len(key_item) > 4 and key_item[4]:
                    key_interpolation = key_item[4]
                if not key_interpolation:
                    key_interpolation = 'CONSTANT'
                bl_bone.matrix = _camera_matrix_to_edit_bone_matrix(scene_cam_obj, camera_matrix, scene_camera_preview_offset)
                for track_name in CAMERA_TRACK_NAMES:
                    camera_bone[track_name] = _as_float(
                        (camera_tracks or {}).get(track_name),
                        _W2SCENE_CAMERA_TRACK_DEFAULTS.get(track_name, CAMERA_TRACK_DEFAULTS.get(track_name, 0.0)),
                    )
                interFrame = key_frame - strip_start_frame

                for i in range(3):
                    pos_curves[i].keyframe_points.add(1)
                    pos_curves[i].keyframe_points[-1].co = (interFrame, bl_bone.location[i])
                    pos_curves[i].keyframe_points[-1].interpolation = key_interpolation
                rotation_values = [float(value) for value in getattr(bl_bone, data_path_rot)]
                if data_path_rot == 'rotation_quaternion' and len(rotation_values) >= 4:
                    if previous_quaternion_values is not None:
                        dot = sum(
                            float(previous_quaternion_values[i]) * float(rotation_values[i])
                            for i in range(4)
                        )
                        if dot < 0.0:
                            rotation_values = [-float(value) for value in rotation_values]
                            quaternion_sign_flips += 1
                    previous_quaternion_values = list(rotation_values[:4])
                for i in range(len(rotation_values)):
                    rot_curves[i].keyframe_points.add(1)
                    rot_curves[i].keyframe_points[-1].co = (interFrame, rotation_values[i])
                    rot_curves[i].keyframe_points[-1].interpolation = key_interpolation
                for track_name, track_curve in track_curves.items():
                    point = track_curve.keyframe_points.insert(interFrame, float(camera_bone[track_name]))
                    point.interpolation = key_interpolation
            for fcurve in list(pos_curves) + list(rot_curves) + list(track_curves.values()):
                try:
                    fcurve.update()
                except Exception:
                    pass
            first_key = key_data[0]
            try:
                bl_bone.matrix = _camera_matrix_to_edit_bone_matrix(scene_cam_obj, first_key[1], scene_camera_preview_offset)
                for track_name in CAMERA_TRACK_NAMES:
                    camera_bone[track_name] = _as_float(
                        (first_key[2] or {}).get(track_name),
                        _W2SCENE_CAMERA_TRACK_DEFAULTS.get(track_name, CAMERA_TRACK_DEFAULTS.get(track_name, 0.0)),
                    )
            except Exception:
                log.debug("Could not restore scene camera rig to first camera key", exc_info=True)
            import_scene_animation.warn_scene_animation_edit(
                "created scene camera action",
                action=action,
                armature_obj=scene_cam_obj,
                section=self._section_name,
                track_name=action_name,
                details={
                    "keyCount": len(key_data),
                    "stripStart": round(float(strip_start_frame), 3),
                    "frameStart": round(float(key_data[0][0]), 3),
                    "frameEnd": round(float(key_data[-1][0]), 3),
                    "keyFrames": ",".join(str(round(float(item[0]), 3)) for item in key_data[:8]),
                    "interpolation": interpolation or "per-key",
                    "keyInterpolation": ",".join(
                        str(item[4] if len(item) > 4 and item[4] else interpolation or "CONSTANT")
                        for item in key_data[:8]
                    ),
                    "cameras": ",".join(
                        str(get_w2scene_event_camera_name(item[3]) or getattr(item[3], "eventName", "") or item[3].__class__.__name__)
                        for item in key_data[:6]
                    ),
                    "eventGuids": ",".join(
                        str(get_w2scene_event_guid_string(item[3]) or "")[:8]
                        for item in key_data[:8]
                    ),
                    "quaternionSignFlips": quaternion_sign_flips,
                },
            )
            return action

        def _find_scene_camera_nla_track_for_strip(strip):
            if strip is None or scene_cam_obj is None or scene_cam_obj.animation_data is None:
                return None
            for track in list(scene_cam_obj.animation_data.nla_tracks):
                try:
                    if strip in list(track.strips):
                        return track
                except Exception:
                    continue
            return None

        def _configure_scene_camera_strip(strip, frame_start, frame_end, action_start=None, action_end=None, muted=False):
            if strip is None:
                return None
            strip_start = float(frame_start)
            strip_end = max(strip_start + 1.0, float(frame_end))
            action_start = float(action_start) if action_start is not None else 0.0
            action_end = float(action_end) if action_end is not None else max(action_start + 1.0, strip_end - strip_start)
            try:
                strip.frame_start = strip_start
                strip.frame_end = strip_end
                strip.extrapolation = 'NOTHING'
                strip.blend_type = 'REPLACE'
                strip.influence = 1.0
                if hasattr(strip, 'use_auto_blend'):
                    strip.use_auto_blend = False
                if hasattr(strip, 'blend_in'):
                    strip.blend_in = 0.0
                if hasattr(strip, 'blend_out'):
                    strip.blend_out = 0.0
                if hasattr(strip, "action_frame_start"):
                    strip.action_frame_start = action_start
                if hasattr(strip, "action_frame_end"):
                    strip.action_frame_end = action_end
                if hasattr(strip, "scale"):
                    strip.scale = (strip_end - strip_start) / max(1e-6, action_end - action_start)
            except Exception:
                log.debug("Could not configure .w2scene camera strip playback", exc_info=True)
            track = _find_scene_camera_nla_track_for_strip(strip)
            if track is not None:
                try:
                    track.mute = bool(muted)
                except Exception:
                    pass
            return track

        def object_transform_values(obj):
            return _w2scene_object_transform_values(obj)

        def engine_transform_to_object_values(obj, engine_transform, from_object):
            saved_matrix_world = obj.matrix_world.copy()
            saved_location = obj.location.copy()
            saved_rotation_euler = obj.rotation_euler.copy()
            saved_rotation_quaternion = obj.rotation_quaternion.copy()
            saved_rotation_axis_angle = tuple(float(value) for value in obj.rotation_axis_angle)
            saved_scale = obj.scale.copy()
            try:
                set_blender_object_transform(obj, engine_transform, from_this_object=from_object)
                return object_transform_values(obj)
            finally:
                obj.matrix_world = saved_matrix_world
                obj.location = saved_location
                obj.rotation_euler = saved_rotation_euler
                obj.rotation_quaternion = saved_rotation_quaternion
                for axis_i, value in enumerate(saved_rotation_axis_angle):
                    obj.rotation_axis_angle[axis_i] = value
                obj.scale = saved_scale

        def create_object_transform_action(action_name, target_obj, key_data, interpolation='CONSTANT'):
            if target_obj is None or not key_data:
                return None
            data_path_rot = key_data[0][1][3]
            rotation_values = getattr(target_obj, data_path_rot)
            action = bpy.data.actions.new(name=action_name)
            loc_curves = [
                new_action_fcurve(action, target_obj, data_path='location', index=axis_i, group_name="Placement")
                for axis_i in range(3)
            ]
            rot_curves = [
                new_action_fcurve(action, target_obj, data_path=data_path_rot, index=axis_i, group_name="Placement")
                for axis_i in range(len(rotation_values))
            ]
            scale_curves = [
                new_action_fcurve(action, target_obj, data_path='scale', index=axis_i, group_name="Placement")
                for axis_i in range(3)
            ]

            for frame, values, _event in key_data:
                location_values, rotation_values, scale_values, _rot_path = values
                for axis_i, curve in enumerate(loc_curves):
                    point = curve.keyframe_points.insert(frame, float(location_values[axis_i]))
                    point.interpolation = interpolation
                for axis_i, curve in enumerate(rot_curves):
                    point = curve.keyframe_points.insert(frame, float(rotation_values[axis_i]))
                    point.interpolation = interpolation
                for axis_i, curve in enumerate(scale_curves):
                    point = curve.keyframe_points.insert(frame, float(scale_values[axis_i]))
                    point.interpolation = interpolation
            rotation_yaws = []
            for _frame, values, _event in key_data:
                _location_values, key_rotation_values, _scale_values, key_rot_path = values
                yaw_degrees = None
                try:
                    if key_rot_path == "rotation_quaternion" and len(key_rotation_values) >= 4:
                        yaw_degrees = Quaternion(key_rotation_values[:4]).to_euler().z * 180.0 / math.pi
                    elif key_rot_path == "rotation_axis_angle" and len(key_rotation_values) >= 4:
                        axis = Vector((key_rotation_values[1], key_rotation_values[2], key_rotation_values[3]))
                        if axis.length > 0.0:
                            yaw_degrees = Quaternion(axis.normalized(), float(key_rotation_values[0])).to_euler().z * 180.0 / math.pi
                    elif len(key_rotation_values) >= 3:
                        euler_order = getattr(target_obj, "rotation_mode", "XYZ")
                        if euler_order in {"QUATERNION", "AXIS_ANGLE"}:
                            euler_order = "XYZ"
                        yaw_degrees = Euler(key_rotation_values[:3], euler_order).z * 180.0 / math.pi
                except Exception:
                    yaw_degrees = None
                if yaw_degrees is not None:
                    rotation_yaws.append(float(yaw_degrees))
            placement_details = {
                "target": getattr(target_obj, "name", ""),
                "keyCount": len(key_data),
                "frameStart": round(float(key_data[0][0]), 3),
                "frameEnd": round(float(key_data[-1][0]), 3),
                "keyFrames": ",".join(str(round(float(item[0]), 3)) for item in key_data[:8]),
                "rotationPath": data_path_rot,
            }
            if rotation_yaws:
                relative_yaws = [((yaw - rotation_yaws[0] + 180.0) % 360.0) - 180.0 for yaw in rotation_yaws]
                placement_details.update({
                    "yawStartDeg": round(rotation_yaws[0], 3),
                    "yawEndDeg": round(rotation_yaws[-1], 3),
                    "yawDeltaDeg": round(((rotation_yaws[-1] - rotation_yaws[0] + 180.0) % 360.0) - 180.0, 3),
                    "yawSpanDeg": round(max(relative_yaws) - min(relative_yaws), 3),
                })
            import_scene_animation.warn_scene_animation_edit(
                "created scene placement transform action",
                action=action,
                armature_obj=target_obj,
                section=self._section_name,
                track_name="ScenePlacement",
                details=placement_details,
            )
            return action

        def create_object_visibility_action(action_name, target_obj, key_data, interpolation='CONSTANT'):
            if target_obj is None or not key_data:
                return None
            action = bpy.data.actions.new(name=action_name)
            hide_viewport_curve = new_action_fcurve(action, target_obj, data_path='hide_viewport', group_name="Visibility")
            hide_render_curve = new_action_fcurve(action, target_obj, data_path='hide_render', group_name="Visibility")
            for frame, is_visible, _event in key_data:
                hidden_value = 0.0 if bool(is_visible) else 1.0
                for curve in (hide_viewport_curve, hide_render_curve):
                    point = curve.keyframe_points.insert(frame, hidden_value)
                    point.interpolation = interpolation
            return action

        camera_shot_key_data = []
        camera_interpolation_intervals = []
        camera_event_order = 0
        camera_priority_eps = 0.001
        camera_priority_tolerance = 1e-4
        placement_key_data_by_actor = {}
        placement_key_data_by_prop = {}
        visibility_key_data_by_prop = {}
        lookat_state_by_actor = {}
        lookat_event_entries = []
        camera_marker_keys = set()

        def apply_lookat_event(event, trigger_frame):
            actor_name = getattr(event, "actor", None)
            actor_obj = set_cur_actor_by_str(actor_name, actors_dict) if actor_name else None
            if actor_obj is None:
                return False

            trigger_frame = int(trigger_frame)
            guid_full = get_w2scene_event_guid_string(event) or ""
            guid_short = "".join(c if c.isalnum() else "_" for c in guid_full[:12])
            if not guid_short:
                guid_short = f"f{trigger_frame:05d}"
            enabled = getattr(event, "enabled", None)
            enabled = True if enabled is None else bool(enabled)
            instant = bool(getattr(event, "instant", False) or False)
            speed = float(getattr(event, "speed", 0.0) or 0.0)
            level = _enum_string(getattr(event, "level", None), default="LL_Head") or "LL_Head"
            lookat_type = _enum_string(getattr(event, "type", None), default="DLT_Dynamic") or "DLT_Dynamic"
            bone_name = _lookat_actor_bone(level)
            log.info(
                "LookAt: actor=%s target=%s type=%s level=%s instant=%s speed=%.3f frame=%d",
                actor_name, getattr(event, "target", None), lookat_type, level, instant, speed, trigger_frame
            )

            actor_key = getattr(actor_obj, "name", str(actor_name))
            prev_constraint_name = lookat_state_by_actor.get(actor_key)

            if instant or speed <= 0.0:
                ramp_frames = 0
            else:
                ramp_frames = max(1, int(round(__fps / max(speed, 0.01))))

            if not enabled:
                if prev_constraint_name:
                    _keyframe_constraint_influence(actor_obj, bone_name, prev_constraint_name, max(trigger_frame - 1, 0), 1.0, 'CONSTANT' if ramp_frames == 0 else 'LINEAR')
                    _keyframe_constraint_influence(actor_obj, bone_name, prev_constraint_name, trigger_frame + ramp_frames, 0.0, 'CONSTANT')
                lookat_state_by_actor[actor_key] = None
                return True

            target_obj_for_constraint = None
            subtarget = ""
            if lookat_type == "DLT_StaticPoint":
                sp = getattr(event, "staticPoint", None)
                # staticPoint is a Vector struct PROPERTY whose X/Y/Z
                # live as named child PROPERTYs in .More, not direct attrs.
                sx = float(_prop_child_value(sp, "X", 0.0) or 0.0)
                sy = float(_prop_child_value(sp, "Y", 0.0) or 0.0)
                sz = float(_prop_child_value(sp, "Z", 0.0) or 0.0)
                local_pos = (sx, sy, sz)
                log.info("LookAt: staticPoint actor-local = (%.3f, %.3f, %.3f)", sx, sy, sz)
                empty_name = f"{LOOKAT_STATIC_EMPTY_PREFIX}{actor_name}_{guid_short}"
                target_obj_for_constraint = _create_lookat_static_empty(
                    empty_name, local_pos, parent_obj=actor_obj, event_guid=guid_full
                )
                subtarget = ""
            else:
                target_name = getattr(event, "target", None)
                target_entry = actor_entry_by_str(target_name, actors_dict) if target_name else None
                target_armature = target_entry[0] if target_entry else None
                if target_armature is None:
                    log.warning("LookAt target '%s' not found for actor '%s'; skipping event", target_name, actor_name)
                    return False
                target_obj_for_constraint = target_armature
                subtarget = LOOKAT_TARGET_SUBTARGET

            constraint_name = f"{LOOKAT_CONSTRAINT_PREFIX}{guid_short}"
            new_constraint = _add_lookat_damped_track(actor_obj, bone_name, target_obj_for_constraint, subtarget, constraint_name)
            if new_constraint is None:
                log.warning("LookAt: failed to create constraint on %s.%s", actor_key, bone_name)
                return False
            log.info(
                "LookAt: created %s on %s.%s -> target=%s subtarget=%s ramp_frames=%d",
                constraint_name, actor_key, bone_name,
                getattr(target_obj_for_constraint, "name", "?"), subtarget, ramp_frames,
            )

            pre_frame = max(trigger_frame - 1, 0)
            _keyframe_constraint_influence(actor_obj, bone_name, constraint_name, 0, 0.0, 'CONSTANT')
            if ramp_frames == 0:
                _keyframe_constraint_influence(actor_obj, bone_name, constraint_name, pre_frame, 0.0, 'CONSTANT')
                _keyframe_constraint_influence(actor_obj, bone_name, constraint_name, trigger_frame, 1.0, 'CONSTANT')
            else:
                _keyframe_constraint_influence(actor_obj, bone_name, constraint_name, trigger_frame, 0.0, 'LINEAR')
                _keyframe_constraint_influence(actor_obj, bone_name, constraint_name, trigger_frame + ramp_frames, 1.0, 'CONSTANT')

            if prev_constraint_name and prev_constraint_name != constraint_name:
                if ramp_frames == 0:
                    _keyframe_constraint_influence(actor_obj, bone_name, prev_constraint_name, pre_frame, 1.0, 'CONSTANT')
                    _keyframe_constraint_influence(actor_obj, bone_name, prev_constraint_name, trigger_frame, 0.0, 'CONSTANT')
                else:
                    _keyframe_constraint_influence(actor_obj, bone_name, prev_constraint_name, trigger_frame, 1.0, 'LINEAR')
                    _keyframe_constraint_influence(actor_obj, bone_name, prev_constraint_name, trigger_frame + ramp_frames, 0.0, 'CONSTANT')

            lookat_state_by_actor[actor_key] = constraint_name
            return True

        def camera_frame_is_covered_by_queued_interpolation(frame_value):
            frame_value = float(frame_value)
            sample_frame = frame_value + (camera_priority_eps * 0.5)
            for interval in camera_interpolation_intervals or []:
                start = float(interval.get("start", 0.0) or 0.0)
                end = float(interval.get("end", start) or start)
                if end < start:
                    start, end = end, start
                if start - camera_priority_tolerance <= sample_frame <= end + camera_priority_tolerance:
                    return True
            return False

        def add_scene_camera_marker(marker_frame, camera_name, fallback_name="CustomCamera"):
            marker_label = str(camera_name or fallback_name or "CustomCamera")
            try:
                marker_key = (round(float(marker_frame), 3), marker_label)
            except Exception:
                marker_key = (float(marker_frame or 0.0), marker_label)
            if marker_key in camera_marker_keys:
                return None
            camera_marker_keys.add(marker_key)
            marker = bpy.context.scene.timeline_markers.new(marker_label, frame=int(marker_frame))
            if scene_camera_preview_obj is not None:
                marker.camera = scene_camera_preview_obj
            return marker

        for key, shot in self.scene_element_dict.items():
            # if key != 7:
            #     break
            # if key == 7:
            #     continue
            curr_actor = None
            
            ###################
            #   SHOT SCRIPT   #
            ###################
            if shot['dialogscript'].__class__.__name__ == 'CStoryScenePauseElement':
                action = bpy.data.actions.new(name="PAUSE")
                pos_curves = [dummy_keyframe_points] * 3
                dialogframe = self.__frame_current
                duration_frames = _dialog_script_duration(shot['dialogscript']) * __fps
                for axis_i in range(3):
                    pos_curves[axis_i] = new_action_fcurve(action, scene_cam_obj, data_path='location', index=axis_i, group_name="PAUSE")
                #PAUSE BEGIN
                for i in range(3):
                    pos_curves[i].keyframe_points.add(1)
                    pos_curves[i].keyframe_points[-1].co = (0.0, scene_cam_obj.location[i])
                    pos_curves[i].keyframe_points[-1].interpolation = 'LINEAR'

                #PAUSE END
                dialogframe=self.__frame_current + duration_frames
                for i in range(3):
                    pos_curves[i].keyframe_points.add(1)
                    pos_curves[i].keyframe_points[-1].co = (duration_frames, scene_cam_obj.location[i])
                    pos_curves[i].keyframe_points[-1].interpolation = 'LINEAR'

                self.__assign_action(scene_cam_obj, action, track_name = "PAUSE")
            elif shot['dialogscript'].__class__.__name__ == 'CStorySceneLine':
                curr_actor = set_cur_actor_by_str(shot['dialogscript'].voicetag, actors_dict)
                if curr_actor is not None:
                    _ensure_cutscene_face_setup(curr_actor)
                    load_voice_and_lipsync(
                        shot['dialogscript'].dialogLine.String.val,
                        curr_actor,
                        context=context,
                        at_frame=self.__frame_current,
                        nla_mode='replace',
                        strip_props={
                            W2SCENE_AUDIO_STRIP_PROP: True,
                            W2SCENE_AUDIO_SOURCE_PROP: self._scene_filepath,
                            W2SCENE_AUDIO_SECTION_PROP: self._section_name,
                        },
                    )
                    remember_section_nla_targets([curr_actor])
                action = bpy.data.actions.new(name="dialogLine")
                pos_curves = [dummy_keyframe_points] * 3
                dialogframe = self.__frame_current
                duration_frames = _dialog_script_duration(shot['dialogscript']) * __fps
                for axis_i in range(3):
                    pos_curves[axis_i] = new_action_fcurve(action, scene_cam_obj, data_path='location', index=axis_i, group_name="dialogLine")
                #PAUSE BEGIN
                for i in range(3):
                    pos_curves[i].keyframe_points.add(1)
                    pos_curves[i].keyframe_points[-1].co = (0.0, scene_cam_obj.location[i])
                    pos_curves[i].keyframe_points[-1].interpolation = 'LINEAR'

                #PAUSE END
                dialogframe=self.__frame_current + duration_frames
                for i in range(3):
                    pos_curves[i].keyframe_points.add(1)
                    pos_curves[i].keyframe_points[-1].co = (duration_frames, scene_cam_obj.location[i])
                    pos_curves[i].keyframe_points[-1].interpolation = 'LINEAR'

                self.__assign_action(scene_cam_obj, action, track_name = "dialogLine")
            else:
                dialogframe = self.__frame_current
                log.debug("Unhandled dialog script type: %s", shot['dialogscript'].__class__.__name__)
                # "CStorySceneDialogset",
                # "CStorySceneDialogsetInstance",
                # "CStorySceneDialogsetSlot",
            
            ###################
            #  SHOT ELEMENTS  #
            ###################

            #shot['CUE'] = []
            for event in shot['CUE']:
                if not self._section_event_is_active(event):
                    event_class = event.__class__.__name__
                    log.debug("Skipping muted or inactive scene event: %s", event_class)
                    if event_class in {
                        "CStorySceneEventAnimation",
                        "CStorySceneEventAdditiveAnimation",
                        "CStorySceneEventOverrideAnimation",
                        "CStorySceneEventChangePose",
                        "CStorySceneEventMimics",
                        "CStorySceneEventMimicsAnim",
                    }:
                        import_scene_animation.warn_scene_animation_skip(
                            "inactive section variant or muted event",
                            event=event,
                            section=self._section_name,
                            details={
                                "requested": getattr(event, "animationName", None) or getattr(event, "transitionAnimation", None) or getattr(event, "forceBodyIdleAnimation", None) or "",
                                "activeVariant": getattr(self, "_section_active_variant_id", None),
                                "eventVariant": (getattr(self, "_section_event_variant_by_guid", {}) or {}).get(get_w2scene_event_guid_string(event), ""),
                                "muted": bool(getattr(event, "isMuted", False) or False),
                            },
                        )
                    continue
                camera_event_order += 1
                current_camera_event_order = camera_event_order
                if event.__class__.__name__ in {
                    "CStorySceneEventAnimation",
                    "CStorySceneEventAdditiveAnimation",
                    "CStorySceneEventOverrideAnimation",
                }:
                    #event: w3_types.CStorySceneEventAnimation
                    actor_name = getattr(event, "actor", None) or getattr(event, "actorName", None)
                    event_actor = set_cur_actor_by_str(actor_name, actors_dict) if actor_name else curr_actor
                    if event_actor is None:
                        import_scene_animation.warn_scene_animation_skip(
                            "missing actor for animation event",
                            event=event,
                            section=self._section_name,
                            details={"requested": getattr(event, "animationName", "")},
                        )
                        continue
                    guid_suffix = (get_w2scene_event_guid_string(event) or "")[:8]
                    _anim_track = _safe_nla_track_name("cutscene_import_body", actor_name, event.animationName, guid_suffix)
                    _anim_frame = get_event_start_frame(shot, event)
                    _loaded = load_scene_animation_by_name(event.animationName, event_actor, _anim_track, _anim_frame, event=event)
                    if _loaded:
                        apply_anim_clip_event_strip_props(_loaded, _anim_track, _anim_frame, event)
                elif  event.__class__.__name__ == "CStorySceneEventChangePose": #type(event) == w3_types.CStorySceneEventChangePose:
                    event: w3_types.CStorySceneEventChangePose
                    curr_actor = set_cur_actor_by_str(event.actor, actors_dict)
                    if curr_actor is None:
                        import_scene_animation.warn_scene_animation_skip(
                            "missing actor for change-pose event",
                            event=event,
                            section=self._section_name,
                            details={"requested": getattr(event, "transitionAnimation", "") or getattr(event, "forceBodyIdleAnimation", "")},
                        )
                        continue
                    anim_name = _cname_text(getattr(event, "transitionAnimation", None))
                    if not anim_name:
                        import_scene_animation.warn_scene_animation_skip(
                            "change-pose event has no transition animation",
                            event=event,
                            section=self._section_name,
                            details={"forceBodyIdleAnimation": getattr(event, "forceBodyIdleAnimation", "")},
                        )
                    _pose_frame = get_event_start_frame(shot, event)
                    _pose_track = ""
                    _loaded = []
                    if anim_name:
                        _pose_track = _safe_nla_track_name("cutscene_import_pose", event.actor, anim_name, (get_w2scene_event_guid_string(event) or "")[:8])
                        _loaded = load_scene_animation_by_name(anim_name, curr_actor, _pose_track, _pose_frame, event=event)
                        if _loaded:
                            apply_anim_clip_event_strip_props(_loaded, _pose_track, _pose_frame, event)
                    _idle_frame = _change_pose_idle_start_frame(event, _pose_frame, _loaded, _pose_track)
                    load_change_pose_dialogset_idle(event, curr_actor, _idle_frame)
                elif event.__class__.__name__ == "CStorySceneEventMimics":
                    event: w3_types.CStorySceneEventMimics
                    actor_name = getattr(event, "actor", None)
                    actor_obj = set_cur_actor_by_str(actor_name, actors_dict) if actor_name else curr_actor
                    if actor_obj is None:
                        import_scene_animation.warn_scene_animation_skip(
                            "missing actor for mimic state event",
                            event=event,
                            section=self._section_name,
                            details={"requested": getattr(event, "mimicsEmotionalState", "") or getattr(event, "transitionAnimation", "")},
                        )
                        continue
                    _ensure_cutscene_face_setup(actor_obj)
                    _mimic_frame = get_event_start_frame(shot, event)
                    load_mimics_state_event(event, actor_obj, _mimic_frame)
                elif event.__class__.__name__ == "CStorySceneEventMimicsAnim":
                    event: w3_types.CStorySceneEventMimicsAnim
                    actor_obj = set_cur_actor_by_str(event.actor, actors_dict)
                    if actor_obj is None:
                        import_scene_animation.warn_scene_animation_skip(
                            "missing actor for mimic animation event",
                            event=event,
                            section=self._section_name,
                            details={"requested": getattr(event, "animationName", "")},
                        )
                        continue
                    _ensure_cutscene_face_setup(actor_obj)
                    _mimic_track = _safe_nla_track_name("cutscene_import_mimic", event.actor, event.animationName, (get_w2scene_event_guid_string(event) or "")[:8])
                    _mimic_frame = get_event_start_frame(shot, event)
                    _loaded = load_scene_animation_by_name(event.animationName, actor_obj, _mimic_track, _mimic_frame, face_target_mode="owner", show_all=True, event=event)
                    if _loaded:
                        apply_anim_clip_event_strip_props(_loaded, _mimic_track, _mimic_frame, event)
                elif event.__class__.__name__ == "CStorySceneEventOverridePlacement":
                    event: w3_types.CStorySceneEventOverridePlacement
                    actor_obj = set_cur_actor_by_str(event.actorName, actors_dict)
                    if actor_obj is None:
                        continue
                    engine_transform = event.placement.EngineTransform if event.placement else EngineTransform()
                    placement_frame = get_event_start_frame(shot, event)
                    placement_guid = get_w2scene_event_guid_string(event) or ""
                    actor_marker_key = str(getattr(event, "actorName", "") or "actor").strip() or "actor"
                    debug_actor_placement_index[actor_marker_key] = debug_actor_placement_index.get(actor_marker_key, 0) + 1
                    placement_empty_name = _safe_w2scene_debug_name(
                        "place",
                        actor_marker_key,
                        f"{debug_actor_placement_index[actor_marker_key]:02d}",
                        max_len=48,
                    )
                    if _create_w2scene_debug_empty(
                        context,
                        placement_empty_name,
                        engine_transform,
                        from_object=placeCube,
                        display_type='ARROWS',
                        display_size=0.45,
                        color=(1.0, 0.55, 0.08, 1.0),
                        metadata={
                            W2SCENE_DEBUG_EMPTY_TYPE_PROP: "actor_placement_event",
                            W2SCENE_DEBUG_SECTION_PROP: self._section_name,
                            "witcher_w2scene_actor": str(getattr(event, "actorName", "") or ""),
                            "witcher_w2scene_event": event.__class__.__name__,
                            "witcher_w2scene_event_name": str(getattr(event, "eventName", "") or ""),
                            "witcher_w2scene_event_guid": placement_guid,
                            "witcher_w2scene_frame": float(placement_frame),
                        },
                    ) is not None:
                        debug_placement_empty_count += 1
                    placement_values = engine_transform_to_object_values(actor_obj, engine_transform, placeCube)
                    actor_key = getattr(actor_obj, "name", str(id(actor_obj)))
                    placement_entry = placement_key_data_by_actor.setdefault(
                        actor_key,
                        {"object": actor_obj, "keys": []},
                    )
                    placement_entry["keys"].append((placement_frame, placement_values, event))
                elif event.__class__.__name__ == "CStorySceneEventScenePropPlacement":
                    prop_id = str(getattr(event, "propId", "") or "").strip()
                    prop_entry = props_dict.get(prop_id)
                    if prop_entry is None:
                        log.warning("Skipping scene prop placement for unknown prop '%s'", prop_id)
                        continue
                    prop_obj = prop_entry[0]
                    engine_transform = event.placement.EngineTransform if event.placement else EngineTransform()
                    placement_frame = get_event_start_frame(shot, event)
                    placement_guid = get_w2scene_event_guid_string(event) or ""
                    prop_marker_key = str(prop_id or getattr(prop_obj, "name", "") or "prop").strip() or "prop"
                    debug_prop_placement_index[prop_marker_key] = debug_prop_placement_index.get(prop_marker_key, 0) + 1
                    placement_empty_name = _safe_w2scene_debug_name(
                        "prop_place",
                        prop_marker_key,
                        f"{debug_prop_placement_index[prop_marker_key]:02d}",
                        max_len=56,
                    )
                    if _create_w2scene_debug_empty(
                        context,
                        placement_empty_name,
                        engine_transform,
                        from_object=placeCube,
                        display_type='SINGLE_ARROW',
                        display_size=0.38,
                        color=(0.1, 0.85, 0.38, 1.0),
                        metadata={
                            W2SCENE_DEBUG_EMPTY_TYPE_PROP: "prop_placement_event",
                            W2SCENE_DEBUG_SECTION_PROP: self._section_name,
                            "witcher_w2scene_prop": prop_id,
                            "witcher_w2scene_event": event.__class__.__name__,
                            "witcher_w2scene_event_name": str(getattr(event, "eventName", "") or ""),
                            "witcher_w2scene_event_guid": placement_guid,
                            "witcher_w2scene_frame": float(placement_frame),
                        },
                    ) is not None:
                        debug_placement_empty_count += 1
                    placement_values = engine_transform_to_object_values(prop_obj, engine_transform, placeCube)
                    prop_key = prop_id or getattr(prop_obj, "name", str(id(prop_obj)))
                    placement_entry = placement_key_data_by_prop.setdefault(
                        prop_key,
                        {"object": prop_obj, "keys": []},
                    )
                    placement_entry["keys"].append((placement_frame, placement_values, event))

                    is_visible = True if getattr(event, "showHide", None) is None else bool(getattr(event, "showHide", True))
                    visibility_entry = visibility_key_data_by_prop.setdefault(
                        prop_key,
                        {"object": prop_obj, "keys": []},
                    )
                    visibility_entry["keys"].append((placement_frame, is_visible, event))
                elif  event.__class__.__name__ ==  "CStorySceneEventCameraInterpolation":
                    keyGuidsObjs = []
                    key_guid_strings = []
                    resolved_key_frames = []
                    resolved_key_names = []
                    for guid in _iter_prop_values(getattr(event, "keyGuids", None)):
                        guid_string = _w2scene_guid_string(guid)
                        key_guid_strings.append(guid_string or "<empty>")
                        cam_event = CustomCameraInstances.get(guid_string)
                        if not cam_event:
                            log.warning("Skipping camera interpolation key %s with no matching custom camera event", guid_string)
                            continue
                        camera_matrix, camera_tracks, camera_name = get_event_camera_pose(cam_event)
                        if camera_matrix is None:
                            log.warning("Skipping camera interpolation key %s with unresolved camera %s", guid_string, camera_name)
                            continue
                        key_frame = get_event_start_frame(shot, cam_event)
                        keyGuidsObjs.append((key_frame, camera_matrix, camera_tracks, cam_event))
                        resolved_key_frames.append(round(float(key_frame), 3))
                        resolved_key_names.append(str(camera_name or getattr(cam_event, "eventName", "") or cam_event.__class__.__name__))
                        add_scene_camera_marker(key_frame, camera_name, "CameraInterpolation")
                    import_scene_animation.warn_scene_animation_debug(
                        "camera interpolation event",
                        event=event,
                        section=self._section_name,
                        track_name="CameraInterpolation",
                        details={
                            "guid": get_w2scene_event_guid_string(event),
                            "keyGuidCount": len(key_guid_strings),
                            "resolvedKeys": len(keyGuidsObjs),
                            "keyGuids": ",".join(key_guid_strings[:6]),
                            "keyFrames": ",".join(str(frame) for frame in resolved_key_frames[:6]),
                            "cameras": ",".join(resolved_key_names[:6]),
                            "easeIn": _enum_string(getattr(event, "easeInStyle", None), ""),
                            "easeOut": _enum_string(getattr(event, "easeOutStyle", None), ""),
                        },
                    )

                    if not keyGuidsObjs:
                        log.warning("Skipping camera interpolation event with no resolved camera keys")
                        continue
                    keyGuidsObjs.sort(key=lambda item: float(item[0]))
                    camera_interpolation_intervals.append({
                        "event": event,
                        "keys": keyGuidsObjs,
                        "start": float(keyGuidsObjs[0][0]),
                        "end": float(keyGuidsObjs[-1][0]),
                        "order": current_camera_event_order,
                    })
                    import_scene_animation.warn_scene_animation_debug(
                        "camera interpolation queued for section timeline",
                        event=event,
                        section=self._section_name,
                        track_name="CustomCamera",
                        details={
                            "keysQueued": len(keyGuidsObjs),
                            "frames": ",".join(str(round(float(item[0]), 3)) for item in keyGuidsObjs[:6]),
                            "interpolation": "LINEAR",
                            "interval": f"{round(float(keyGuidsObjs[0][0]), 3)}-{round(float(keyGuidsObjs[-1][0]), 3)}",
                        },
                    )
                
                elif  event.__class__.__name__ ==  "CStorySceneEventCustomCamera":
                    guid_string = get_w2scene_event_guid_string(event)
                    if guid_string:
                        CustomCameraInstances[guid_string] = event
                    if guid_string and guid_string in camera_interpolation_key_guids:
                        import_scene_animation.warn_scene_animation_debug(
                            "custom camera deferred to interpolation",
                            event=event,
                            section=self._section_name,
                            track_name="CameraInterpolation",
                            details={
                                "guid": guid_string,
                                "camera": get_w2scene_event_camera_name(event) or "",
                            },
                        )
                        continue
                    camera_matrix, camera_tracks, camera_name = get_event_camera_pose(event)
                    if camera_matrix is None:
                        log.warning("Skipping custom camera event with unresolved camera %s", camera_name)
                        continue
                    marker_frame = get_event_start_frame(shot, event)
                    if camera_frame_is_covered_by_queued_interpolation(marker_frame):
                        import_scene_animation.warn_scene_animation_debug(
                            "custom camera overridden by interpolation",
                            event=event,
                            section=self._section_name,
                            track_name="CustomCamera",
                            details={
                                "guid": guid_string,
                                "frame": round(float(marker_frame), 3),
                                "camera": camera_name or "",
                            },
                        )
                        continue
                    add_scene_camera_marker(marker_frame, camera_name, "CustomCamera")
                    camera_shot_key_data.append((marker_frame, camera_matrix, camera_tracks, event, 'CONSTANT', current_camera_event_order))
                    import_scene_animation.warn_scene_animation_debug(
                        "custom camera shot event",
                        event=event,
                        section=self._section_name,
                        track_name="CustomCamera",
                        details={
                            "guid": guid_string,
                            "frame": round(float(marker_frame), 3),
                            "camera": camera_name or "",
                        },
                    )
                
                elif  event.__class__.__name__ ==  "CStorySceneEventCustomCameraInstance":
                    guid_string = get_w2scene_event_guid_string(event)
                    if guid_string:
                        CustomCameraInstances[guid_string] = event
                    if guid_string and guid_string in camera_interpolation_key_guids:
                        import_scene_animation.warn_scene_animation_debug(
                            "custom camera instance deferred to interpolation",
                            event=event,
                            section=self._section_name,
                            track_name="CameraInterpolation",
                            details={
                                "guid": guid_string,
                                "camera": get_w2scene_event_camera_name(event) or "",
                            },
                        )
                        continue
                    camera_matrix, camera_tracks, camera_name = get_event_camera_pose(event)
                    if camera_matrix is None:
                        log.warning("Skipping custom camera instance with unresolved camera %s", camera_name)
                        continue
                    marker_frame = get_event_start_frame(shot, event)
                    if camera_frame_is_covered_by_queued_interpolation(marker_frame):
                        import_scene_animation.warn_scene_animation_debug(
                            "custom camera instance overridden by interpolation",
                            event=event,
                            section=self._section_name,
                            track_name="CustomCamera",
                            details={
                                "guid": guid_string,
                                "frame": round(float(marker_frame), 3),
                                "camera": camera_name or "",
                            },
                        )
                        continue
                    add_scene_camera_marker(marker_frame, camera_name, "CustomCameraInstance")
                    camera_shot_key_data.append((marker_frame, camera_matrix, camera_tracks, event, 'CONSTANT', current_camera_event_order))
                    import_scene_animation.warn_scene_animation_debug(
                        "custom camera instance shot event",
                        event=event,
                        section=self._section_name,
                        track_name="CustomCamera",
                        details={
                            "guid": guid_string,
                            "frame": round(float(marker_frame), 3),
                            "camera": camera_name or "",
                        },
                    )

                elif "Camera" in event.__class__.__name__:
                    import_scene_animation.warn_scene_animation_debug(
                        "unhandled camera event",
                        event=event,
                        section=self._section_name,
                        track_name="Camera",
                        details={
                            "guid": get_w2scene_event_guid_string(event),
                            "frame": round(float(get_event_start_frame(shot, event)), 3),
                            "class": event.__class__.__name__,
                        },
                    )

                elif event.__class__.__name__ == "CStorySceneEventLookAt":
                    lookat_frame = get_event_start_frame(shot, event)
                    lookat_event_entries.append((float(lookat_frame), current_camera_event_order, event))

                else:
                    log.debug("Unhandled event type: %s", event.__class__.__name__)

            ###################
            #  SHOT ENDING    #
            ###################
            self.__frame_current = dialogframe #! THE FRAME THIS SHOT ENDS ON

        if lookat_event_entries:
            lookat_event_entries.sort(key=lambda item: (float(item[0]), int(item[1] or 0)))
            log.info(
                "LookAt: applying %d event(s) in timeline order for section %s",
                len(lookat_event_entries),
                self._section_name or "<unnamed>",
            )
            for lookat_frame, _event_order, lookat_event in lookat_event_entries:
                apply_lookat_event(lookat_event, lookat_frame)

        if debug_slot_empty_count or debug_placement_empty_count:
            log.info(
                "Created %d .w2scene dialogset slot debug marker(s) and %d placement debug marker(s) for section %s.",
                debug_slot_empty_count,
                debug_placement_empty_count,
                self._section_name or "<unnamed>",
            )

        CAMERA_TIMELINE_EPS = 0.001
        CAMERA_TIMELINE_TOLERANCE = 1e-4

        def _camera_interval_priority(interval):
            return (float(interval.get("start", 0.0) or 0.0), int(interval.get("order", 0) or 0))

        def _active_camera_interval_at(frame_value, intervals):
            active = []
            frame_value = float(frame_value)
            for interval in intervals or []:
                start = float(interval.get("start", 0.0) or 0.0)
                end = float(interval.get("end", start) or start)
                if end < start:
                    start, end = end, start
                if start - CAMERA_TIMELINE_TOLERANCE <= frame_value <= end + CAMERA_TIMELINE_TOLERANCE:
                    active.append(interval)
            if not active:
                return None
            return max(active, key=_camera_interval_priority)

        def _compose_camera_sample_matrix(location, rotation, scale):
            return (
                Matrix.Translation(location)
                @ rotation.to_matrix().to_4x4()
                @ Matrix.Diagonal((scale.x, scale.y, scale.z, 1.0))
            )

        def _interpolate_camera_matrix(matrix_a, matrix_b, alpha):
            alpha = max(0.0, min(1.0, float(alpha)))
            if alpha <= 0.0:
                return matrix_a.copy()
            if alpha >= 1.0:
                return matrix_b.copy()
            loc_a, rot_a, scale_a = matrix_a.decompose()
            loc_b, rot_b, scale_b = matrix_b.decompose()
            try:
                rot_b_work = rot_b.copy()
                if rot_a.dot(rot_b_work) < 0.0:
                    rot_b_work.negate()
                rotation = rot_a.slerp(rot_b_work, alpha)
            except Exception:
                rotation = rot_a.slerp(rot_b, alpha)
            return _compose_camera_sample_matrix(
                loc_a.lerp(loc_b, alpha),
                rotation,
                scale_a.lerp(scale_b, alpha),
            )

        def _interpolate_camera_tracks(tracks_a, tracks_b, alpha):
            alpha = max(0.0, min(1.0, float(alpha)))
            result = {}
            for track_name in CAMERA_TRACK_NAMES:
                default_value = _W2SCENE_CAMERA_TRACK_DEFAULTS.get(
                    track_name,
                    CAMERA_TRACK_DEFAULTS.get(track_name, 0.0),
                )
                value_a = _as_float((tracks_a or {}).get(track_name), default_value)
                value_b = _as_float((tracks_b or {}).get(track_name), default_value)
                result[track_name] = (value_a * (1.0 - alpha)) + (value_b * alpha)
            return result

        def _sample_camera_interpolation_interval(interval, frame_value):
            keys = list(interval.get("keys") or [])
            if not keys:
                return None, None, interval.get("event")
            frame_value = float(frame_value)
            if frame_value <= float(keys[0][0]) + CAMERA_TIMELINE_TOLERANCE:
                return keys[0][1].copy(), dict(keys[0][2] or {}), keys[0][3]
            if frame_value >= float(keys[-1][0]) - CAMERA_TIMELINE_TOLERANCE:
                return keys[-1][1].copy(), dict(keys[-1][2] or {}), keys[-1][3]
            for key_index in range(1, len(keys)):
                prev_key = keys[key_index - 1]
                next_key = keys[key_index]
                prev_frame = float(prev_key[0])
                next_frame = float(next_key[0])
                if next_frame + CAMERA_TIMELINE_TOLERANCE < frame_value:
                    continue
                span = max(CAMERA_TIMELINE_TOLERANCE, next_frame - prev_frame)
                alpha = max(0.0, min(1.0, (frame_value - prev_frame) / span))
                matrix = _interpolate_camera_matrix(prev_key[1], next_key[1], alpha)
                tracks = _interpolate_camera_tracks(prev_key[2], next_key[2], alpha)
                source_event = prev_key[3] if alpha < 0.5 else next_key[3]
                return matrix, tracks, source_event
            return keys[-1][1].copy(), dict(keys[-1][2] or {}), keys[-1][3]

        def _camera_timeline_key(frame_value, matrix, tracks, event, interpolation, order=0, source=""):
            return (
                float(frame_value),
                matrix.copy() if hasattr(matrix, "copy") else matrix,
                dict(tracks or {}),
                event,
                interpolation or 'CONSTANT',
                int(order or 0),
                str(source or ""),
            )

        def _camera_key_order(key_item, fallback_order):
            if len(key_item) > 5:
                try:
                    return int(key_item[5] or 0)
                except Exception:
                    pass
            return int(fallback_order or 0)

        def _camera_key_source_priority(key_item):
            source = str(key_item[6] if len(key_item) > 6 else "")
            if source == "interpolation":
                return 2
            if source == "shot":
                return 1
            return 0

        def _has_persistent_camera_shot_at(frame_value, intervals, shot_key_data):
            for shot_key in shot_key_data or []:
                if abs(float(shot_key[0]) - float(frame_value)) > CAMERA_TIMELINE_TOLERANCE:
                    continue
                if _active_camera_interval_at(float(frame_value) + (CAMERA_TIMELINE_EPS * 0.5), intervals) is None:
                    return True
            return False

        def _camera_interval_forced_end_key(interval, frame_value):
            keys = list(interval.get("keys") or [])
            if not keys:
                return None
            frame_value = float(frame_value)
            for key in keys:
                if float(key[0]) >= frame_value - CAMERA_TIMELINE_TOLERANCE:
                    return key
            return keys[-1]

        def _append_camera_interval_segment(timeline_keys, interval, start_frame, end_frame, force_end_key=False):
            start_frame = float(start_frame)
            end_frame = float(end_frame)
            if end_frame < start_frame:
                start_frame, end_frame = end_frame, start_frame
            segment_frames = [start_frame]
            for key_frame, _matrix, _tracks, _event in interval.get("keys", []) or []:
                key_frame = float(key_frame)
                if start_frame + CAMERA_TIMELINE_TOLERANCE < key_frame < end_frame - CAMERA_TIMELINE_TOLERANCE:
                    segment_frames.append(key_frame)
            if end_frame > start_frame + CAMERA_TIMELINE_TOLERANCE:
                segment_frames.append(end_frame)
            segment_frames = sorted(set(round(frame, 6) for frame in segment_frames))
            for frame_index, sample_frame in enumerate(segment_frames):
                if force_end_key and frame_index == len(segment_frames) - 1 and interval.get("keys"):
                    end_key = _camera_interval_forced_end_key(interval, sample_frame)
                    matrix = end_key[1].copy()
                    tracks = dict(end_key[2] or {})
                    source_event = end_key[3]
                else:
                    matrix, tracks, source_event = _sample_camera_interpolation_interval(interval, sample_frame)
                if matrix is None:
                    continue
                interpolation = 'LINEAR' if frame_index < len(segment_frames) - 1 else 'CONSTANT'
                timeline_keys.append(_camera_timeline_key(
                    sample_frame,
                    matrix,
                    tracks,
                    source_event or interval.get("event"),
                    interpolation,
                    order=interval.get("order", 0),
                    source="interpolation",
                ))

        def _build_camera_timeline_keys(shot_key_data, interpolation_intervals):
            intervals = [
                interval
                for interval in (interpolation_intervals or [])
                if interval.get("keys") and float(interval.get("end", 0.0) or 0.0) >= float(interval.get("start", 0.0) or 0.0)
            ]
            timeline_keys = []
            stats = {
                "rawKeys": len(shot_key_data or []) + sum(len(interval.get("keys") or []) for interval in intervals),
                "shots": len(shot_key_data or []),
                "intervals": len(intervals),
                "suppressedShots": 0,
                "overlapSplits": 0,
                "sameFrameSplits": 0,
                "sameFrameReplacements": 0,
                "splitFrames": [],
                "splitPairs": [],
                "forcedIntervalEndKeys": 0,
                "suppressedShotFrames": [],
            }

            for interval in intervals:
                start = float(interval.get("start", 0.0) or 0.0)
                end = float(interval.get("end", start) or start)
                if end < start:
                    start, end = end, start
                boundaries = {round(start, 6), round(end, 6)}
                for other in intervals:
                    if other is interval or _camera_interval_priority(other) <= _camera_interval_priority(interval):
                        continue
                    other_start = float(other.get("start", 0.0) or 0.0)
                    other_end = float(other.get("end", other_start) or other_start)
                    if other_end < other_start:
                        other_start, other_end = other_end, other_start
                    if start + CAMERA_TIMELINE_TOLERANCE < other_start < end - CAMERA_TIMELINE_TOLERANCE:
                        boundaries.add(round(other_start, 6))
                    if start + CAMERA_TIMELINE_TOLERANCE < other_end < end - CAMERA_TIMELINE_TOLERANCE:
                        boundaries.add(round(other_end, 6))
                ordered_boundaries = sorted(boundaries)
                for boundary_index in range(1, len(ordered_boundaries)):
                    seg_start = float(ordered_boundaries[boundary_index - 1])
                    seg_end = float(ordered_boundaries[boundary_index])
                    if seg_end <= seg_start + CAMERA_TIMELINE_TOLERANCE:
                        continue
                    mid_frame = seg_start + ((seg_end - seg_start) * 0.5)
                    if _active_camera_interval_at(mid_frame, intervals) is not interval:
                        continue
                    active_after = _active_camera_interval_at(seg_end + (CAMERA_TIMELINE_EPS * 0.5), intervals)
                    persistent_shot_at_end = _has_persistent_camera_shot_at(seg_end, intervals, shot_key_data)
                    output_end = seg_end
                    if (active_after is not None and active_after is not interval) or persistent_shot_at_end:
                        output_end = seg_end - CAMERA_TIMELINE_EPS
                        force_end_key = True
                    else:
                        force_end_key = False
                    if output_end <= seg_start + CAMERA_TIMELINE_TOLERANCE:
                        output_end = seg_start + ((seg_end - seg_start) * 0.5)
                    if output_end < seg_end - CAMERA_TIMELINE_TOLERANCE:
                        stats["overlapSplits"] += 1
                        stats["splitFrames"].append(seg_end)
                    if force_end_key:
                        stats["forcedIntervalEndKeys"] += 1
                    _append_camera_interval_segment(timeline_keys, interval, seg_start, output_end, force_end_key=force_end_key)

            for shot_key in shot_key_data or []:
                shot_frame = float(shot_key[0])
                active_after = _active_camera_interval_at(shot_frame + (CAMERA_TIMELINE_EPS * 0.5), intervals)
                active_now = _active_camera_interval_at(shot_frame, intervals)
                if active_after is not None or (
                    active_now is not None
                    and float(active_now.get("end", shot_frame) or shot_frame) > shot_frame + CAMERA_TIMELINE_TOLERANCE
                ):
                    stats["suppressedShots"] += 1
                    stats["suppressedShotFrames"].append(shot_frame)
                    continue
                timeline_keys.append(_camera_timeline_key(
                    shot_frame,
                    shot_key[1],
                    shot_key[2],
                    shot_key[3],
                    shot_key[4] if len(shot_key) > 4 and shot_key[4] else 'CONSTANT',
                    order=_camera_key_order(shot_key, 0),
                    source="shot",
                ))

            return _merged_camera_timeline_keys(timeline_keys, stats)

        def _merged_camera_timeline_keys(key_data, stats=None, add_section_hold=True):
            if stats is None:
                stats = {
                    "sameFrameSplits": 0,
                    "sameFrameReplacements": 0,
                    "splitFrames": [],
                    "splitPairs": [],
                }
            ordered_keys = sorted(
                enumerate(key_data or []),
                key=lambda item: (float(item[1][0]), _camera_key_order(item[1], item[0]), item[0]),
            )
            merged_keys = []
            merged_meta = []
            for source_order, key_item in ordered_keys:
                key_frame = float(key_item[0])
                normalized_key = (
                    key_frame,
                    key_item[1],
                    key_item[2],
                    key_item[3],
                    key_item[4] if len(key_item) > 4 and key_item[4] else 'CONSTANT',
                )
                if merged_keys and abs(float(merged_keys[-1][0]) - key_frame) <= 1e-4:
                    previous_meta = merged_meta[-1] if merged_meta else (0, 0, 0)
                    new_meta = (
                        _camera_key_source_priority(key_item),
                        _camera_key_order(key_item, source_order),
                        source_order,
                    )
                    if new_meta >= previous_meta:
                        merged_keys[-1] = normalized_key
                        merged_meta[-1] = new_meta
                        stats["sameFrameReplacements"] += 1
                else:
                    merged_keys.append(normalized_key)
                    merged_meta.append((
                        _camera_key_source_priority(key_item),
                        _camera_key_order(key_item, source_order),
                        source_order,
                    ))
            if merged_keys and add_section_hold:
                last_frame, last_matrix, last_tracks, last_event, _last_interp = merged_keys[-1]
                if section_end_frame > float(last_frame):
                    merged_keys.append((
                        float(section_end_frame),
                        last_matrix.copy(),
                        dict(last_tracks or {}),
                        last_event,
                        'CONSTANT',
                    ))
            return merged_keys, stats

        def _camera_shot_is_overridden_by_interpolation(shot_frame, intervals):
            shot_frame = float(shot_frame)
            active_after = _active_camera_interval_at(shot_frame + (CAMERA_TIMELINE_EPS * 0.5), intervals)
            active_now = _active_camera_interval_at(shot_frame, intervals)
            return active_after is not None or (
                active_now is not None
                and float(active_now.get("end", shot_frame) or shot_frame) > shot_frame + CAMERA_TIMELINE_TOLERANCE
            )

        def _camera_layered_strip_end(start_frame, nominal_end_frame, takeover_frames):
            start_frame = float(start_frame)
            nominal_end_frame = max(start_frame, float(nominal_end_frame))
            future_frames = [
                float(frame)
                for frame in takeover_frames
                if float(frame) > nominal_end_frame + CAMERA_TIMELINE_TOLERANCE
            ]
            strip_end = min(future_frames) if future_frames else float(section_end_frame)
            if strip_end > nominal_end_frame + CAMERA_TIMELINE_TOLERANCE:
                strip_end -= CAMERA_TIMELINE_EPS
            else:
                strip_end = nominal_end_frame
            return max(start_frame, strip_end)

        def _copy_camera_key_for_frame(frame_value, key_item, interpolation='CONSTANT'):
            return (
                float(frame_value),
                key_item[1].copy() if hasattr(key_item[1], "copy") else key_item[1],
                dict(key_item[2] or {}),
                key_item[3],
                interpolation,
                _camera_key_order(key_item, 0),
            )

        def _set_scene_camera_preview_from_key(key_item):
            if not key_item:
                return
            bl_bone = scene_cam_obj.pose.bones.get(CAMERA_EDIT_BONE) if scene_cam_obj is not None else None
            camera_bone = ensure_camera_track_properties(scene_cam_obj, track_names=CAMERA_TRACK_NAMES) if scene_cam_obj is not None else None
            if bl_bone is None or camera_bone is None:
                return
            try:
                bl_bone.matrix = _camera_matrix_to_edit_bone_matrix(scene_cam_obj, key_item[1], scene_camera_preview_offset)
                for track_name in CAMERA_TRACK_NAMES:
                    camera_bone[track_name] = _as_float(
                        (key_item[2] or {}).get(track_name),
                        _W2SCENE_CAMERA_TRACK_DEFAULTS.get(track_name, CAMERA_TRACK_DEFAULTS.get(track_name, 0.0)),
                    )
            except Exception:
                log.debug("Could not set scene camera preview pose from layered camera key", exc_info=True)

        def create_layered_scene_camera_playback():
            intervals = [
                interval
                for interval in (camera_interpolation_intervals or [])
                if interval.get("keys") and float(interval.get("end", 0.0) or 0.0) >= float(interval.get("start", 0.0) or 0.0)
            ]
            intervals.sort(key=_camera_interval_priority)
            visible_shots = [
                shot_key
                for shot_key in sorted(camera_shot_key_data or [], key=lambda item: (float(item[0]), _camera_key_order(item, 0)))
                if not _camera_shot_is_overridden_by_interpolation(float(shot_key[0]), intervals)
            ]
            takeover_frames = sorted(set(
                [round(float(shot_key[0]), 6) for shot_key in visible_shots]
                + [round(float(interval.get("start", 0.0) or 0.0), 6) for interval in intervals]
            ))
            created_actions = 0
            created_strips = 0
            raw_shot_strips = 0
            interpolation_strips = 0
            strip_ranges = []
            first_preview_key = None

            for shot_index, shot_key in enumerate(visible_shots):
                shot_frame = float(shot_key[0])
                strip_end = _camera_layered_strip_end(shot_frame, shot_frame, takeover_frames)
                action_keys = [_copy_camera_key_for_frame(shot_frame, shot_key, 'CONSTANT')]
                if strip_end > shot_frame + CAMERA_TIMELINE_TOLERANCE:
                    action_keys.append(_copy_camera_key_for_frame(strip_end, shot_key, 'CONSTANT'))
                action_name = _safe_nla_track_name(
                    "CameraShot",
                    f"{shot_index:03d}",
                    (get_w2scene_event_guid_string(shot_key[3]) or "")[:8],
                )
                action = create_scene_camera_action(action_name, action_keys, shot_frame, interpolation=None)
                if action is None:
                    continue
                strip = self.__assign_action(
                    scene_cam_obj,
                    action,
                    track_name=action_name,
                    at_frame=shot_frame,
                )
                _configure_scene_camera_strip(
                    strip,
                    shot_frame,
                    strip_end,
                    action_start=0.0,
                    action_end=max(CAMERA_TIMELINE_EPS, strip_end - shot_frame),
                    muted=False,
                )
                try:
                    strip.name = action_name
                except Exception:
                    pass
                created_actions += 1
                created_strips += 1
                raw_shot_strips += 1
                strip_ranges.append(f"{action_name}:{round(shot_frame, 3)}-{round(strip_end, 3)}")
                if first_preview_key is None or shot_frame < float(first_preview_key[0]):
                    first_preview_key = action_keys[0]

            for interval_index, interval in enumerate(intervals):
                keys = sorted(list(interval.get("keys") or []), key=lambda item: float(item[0]))
                if not keys:
                    continue
                start_frame = float(interval.get("start", keys[0][0]) or keys[0][0])
                nominal_end = float(interval.get("end", keys[-1][0]) or keys[-1][0])
                strip_end = _camera_layered_strip_end(start_frame, nominal_end, takeover_frames)
                action_keys = []
                for key_index, key_item in enumerate(keys):
                    key_interpolation = 'LINEAR' if key_index < len(keys) - 1 else 'CONSTANT'
                    action_keys.append(_copy_camera_key_for_frame(float(key_item[0]), key_item, key_interpolation))
                if action_keys and strip_end > float(action_keys[-1][0]) + CAMERA_TIMELINE_TOLERANCE:
                    action_keys.append(_copy_camera_key_for_frame(strip_end, action_keys[-1], 'CONSTANT'))
                track_name = f"{W2SCENE_CAMERA_LEGACY_RAW_INTERPOLATION_TRACK_PREFIX}{interval_index:03d}"
                action_name = _safe_nla_track_name(
                    track_name,
                    (get_w2scene_event_guid_string(interval.get("event")) or "")[:8],
                )
                action = create_scene_camera_action(action_name, action_keys, start_frame, interpolation=None)
                if action is None:
                    continue
                strip = self.__assign_action(scene_cam_obj, action, track_name=track_name, at_frame=start_frame)
                _configure_scene_camera_strip(
                    strip,
                    start_frame,
                    strip_end,
                    action_start=0.0,
                    action_end=max(CAMERA_TIMELINE_EPS, strip_end - start_frame),
                    muted=False,
                )
                try:
                    strip.name = action_name
                except Exception:
                    pass
                created_actions += 1
                created_strips += 1
                interpolation_strips += 1
                strip_ranges.append(f"{track_name}:{round(start_frame, 3)}-{round(strip_end, 3)}")
                if first_preview_key is None or start_frame < float(first_preview_key[0]):
                    first_preview_key = action_keys[0]

            try:
                if scene_cam_obj is not None and scene_cam_obj.animation_data is not None:
                    scene_cam_obj.animation_data.action = None
            except Exception:
                pass
            _set_scene_camera_preview_from_key(first_preview_key)

            import_scene_animation.warn_scene_animation_edit(
                "configured layered scene camera playback",
                armature_obj=scene_cam_obj,
                section=self._section_name,
                track_name="CameraLayers",
                details={
                    "actions": created_actions,
                    "strips": created_strips,
                    "rawShotStrips": raw_shot_strips,
                    "interpolationStrips": interpolation_strips,
                    "directAction": False,
                    "takeoverFrames": ",".join(str(round(float(frame), 3)) for frame in takeover_frames[:12]),
                    "stripRanges": ";".join(strip_ranges[:12]),
                },
            )
            return created_strips

        camera_timeline_keys, camera_timeline_stats = _build_camera_timeline_keys(
            camera_shot_key_data,
            camera_interpolation_intervals,
        )
        if camera_timeline_keys:
            import_scene_animation.warn_scene_animation_debug(
                "camera section timeline summary",
                section=self._section_name,
                track_name="CustomCamera",
                armature_obj=scene_cam_obj,
                details={
                    "rawKeys": camera_timeline_stats.get("rawKeys", 0),
                    "mergedKeys": len(camera_timeline_keys),
                    "shots": camera_timeline_stats.get("shots", 0),
                    "intervals": camera_timeline_stats.get("intervals", 0),
                    "suppressedShots": camera_timeline_stats.get("suppressedShots", 0),
                    "overlapSplits": camera_timeline_stats.get("overlapSplits", 0),
                    "forcedIntervalEndKeys": camera_timeline_stats.get("forcedIntervalEndKeys", 0),
                    "frames": ",".join(str(round(float(item[0]), 3)) for item in camera_timeline_keys[:16]),
                    "interpolation": ",".join(str(item[4]) for item in camera_timeline_keys[:16]),
                    "sameFrameSplits": camera_timeline_stats.get("sameFrameSplits", 0),
                    "sameFrameReplacements": camera_timeline_stats.get("sameFrameReplacements", 0),
                    "splitFrames": ",".join(
                        str(round(float(frame), 3))
                        for frame in camera_timeline_stats.get("splitFrames", [])[:8]
                    ),
                    "splitPairs": ";".join(camera_timeline_stats.get("splitPairs", [])[:8]),
                    "suppressedShotFrames": ",".join(
                        str(round(float(frame), 3))
                        for frame in camera_timeline_stats.get("suppressedShotFrames", [])[:8]
                    ),
                },
            )
        create_layered_scene_camera_playback()
        track_order = []
        try:
            track_order = [
                str(getattr(track, "name", "") or "")
                for track in list(scene_cam_obj.animation_data.nla_tracks)
                if _nla_track_name_matches(
                    getattr(track, "name", ""),
                    track_names=W2SCENE_CAMERA_NLA_TRACK_NAMES,
                    track_prefixes=W2SCENE_CAMERA_NLA_TRACK_PREFIXES,
                )
            ]
        except Exception:
            track_order = []
        import_scene_animation.warn_scene_animation_debug(
            "camera NLA track order",
            section=self._section_name,
            armature_obj=scene_cam_obj,
            details={
                "rebuilt": 0,
                "order": ">".join(track_order),
                "rank": "created-order",
            },
        )

        for actor_key, initial_entry in dialogset_initial_placement_by_actor.items():
            actor_obj = initial_entry.get("object")
            initial_values = initial_entry.get("values")
            if actor_obj is None or initial_values is None:
                continue
            placement_entry = placement_key_data_by_actor.setdefault(
                actor_key,
                {"object": actor_obj, "keys": []},
            )
            placement_keys = placement_entry.setdefault("keys", [])
            has_frame_start_key = any(
                abs(float(key[0]) - float(frame_offset)) <= 1e-4
                for key in placement_keys
            )
            if not has_frame_start_key:
                placement_keys.append((float(frame_offset), initial_values, None))

        for placement_entry in placement_key_data_by_actor.values():
            actor_obj = placement_entry["object"]
            placement_keys = list(placement_entry["keys"])
            if actor_obj is None or not placement_keys:
                continue
            placement_keys.sort(key=lambda item: item[0])
            action_keys = []
            if placement_keys[0][0] > float(frame_offset):
                action_keys.append((float(frame_offset), object_transform_values(actor_obj), None))
            action_keys.extend(placement_keys)
            last_frame, last_values, last_event = action_keys[-1]
            if section_end_frame > last_frame:
                action_keys.append((section_end_frame, last_values, last_event))
            action_name = _safe_nla_track_name("ScenePlacement", getattr(actor_obj, "name", "Actor"))
            placement_action = create_object_transform_action(action_name, actor_obj, action_keys, interpolation='CONSTANT')
            if placement_action is not None:
                self.__assign_action(actor_obj, placement_action, track_name="ScenePlacement", at_frame=float(frame_offset))
                extend_nla_track_to_frame(actor_obj, "ScenePlacement", section_end_frame)

        placement_reset_frames_by_actor = {}
        for placement_entry in placement_key_data_by_actor.values():
            actor_obj = placement_entry.get("object")
            if actor_obj is None:
                continue
            placement_frames = []
            for key in placement_entry.get("keys", []) or []:
                try:
                    key_frame = float(key[0])
                except Exception:
                    continue
                if key_frame > float(frame_offset) + 1e-4:
                    placement_frames.append(key_frame)
            if placement_frames:
                placement_reset_frames_by_actor[getattr(actor_obj, "name", str(id(actor_obj)))] = sorted(set(placement_frames))
        for motion_entry in scene_motion_accumulator.build_actor_motion_actions(
            action_name_factory=lambda actor_obj: _safe_nla_track_name("SceneMotionExtraction", getattr(actor_obj, "name", "Actor")),
            playback_mode="animated",
            reset_frames_by_actor=placement_reset_frames_by_actor,
        ):
            actor_obj = motion_entry.get("actor_obj")
            motion_action = motion_entry.get("action")
            if actor_obj is None or motion_action is None:
                continue
            self.__assign_action(
                actor_obj,
                motion_action,
                track_name=import_scene_motion.SCENE_MOTION_TRACK_NAME,
                at_frame=float(frame_offset),
            )
            remember_section_nla_targets([actor_obj])
            motion_strip = None
            try:
                track = actor_obj.animation_data.nla_tracks.get(import_scene_motion.SCENE_MOTION_TRACK_NAME)
                if track is not None and track.strips:
                    motion_strip = max(track.strips, key=lambda strip: float(getattr(strip, "frame_start", 0.0) or 0.0))
                    motion_strip.name = getattr(motion_action, "name", import_scene_motion.SCENE_MOTION_TRACK_NAME)
                    motion_strip.frame_start = float(frame_offset)
                    motion_strip.frame_end = max(float(frame_offset) + 1.0, float(section_end_frame))
                    motion_strip.extrapolation = 'NOTHING'
                    motion_strip.blend_type = 'ADD'
                    motion_strip.influence = 1.0
                    if hasattr(motion_strip, 'use_auto_blend'):
                        motion_strip.use_auto_blend = False
                    if hasattr(motion_strip, 'blend_in'):
                        motion_strip.blend_in = 0.0
                    if hasattr(motion_strip, 'blend_out'):
                        motion_strip.blend_out = 0.0
                    try:
                        motion_strip[W2SCENE_NLA_STRIP_BLEND_TYPE_PROP] = 'ADD'
                    except Exception:
                        pass
            except Exception:
                log.debug("Could not configure scene motion extraction strip for %s", getattr(actor_obj, "name", "<actor>"), exc_info=True)
            try:
                motion_action[W2SCENE_ACTION_BLEND_TYPE_PROP] = 'ADD'
            except Exception:
                pass
            import_scene_animation.warn_scene_animation_edit(
                "created scene motion extraction transform action",
                action=motion_action,
                strip=motion_strip,
                armature_obj=actor_obj,
                section=self._section_name,
                track_name=import_scene_motion.SCENE_MOTION_TRACK_NAME,
                details={
                    "eventCount": int(motion_entry.get("event_count", 0) or 0),
                    "keyCount": len(motion_entry.get("keyframes", []) or []),
                    "sampleKeyCount": len(motion_entry.get("sample_keyframes", []) or []),
                    "frameStart": round(float(frame_offset), 3),
                    "frameEnd": round(float(section_end_frame), 3),
                    "xyDelta": round(float(motion_entry.get("xy_delta", 0.0) or 0.0), 6),
                    "yawDeltaDeg": round(float(motion_entry.get("yaw_delta_deg", 0.0) or 0.0), 3),
                    "rotationPath": motion_entry.get("rotation_path", ""),
                    "locationSpace": motion_entry.get("location_space", ""),
                    "placementYawDeg": round(float(motion_entry.get("placement_yaw_deg", 0.0) or 0.0), 3),
                    "playbackMode": motion_entry.get("playback_mode", ""),
                    "resetFrame": round(float(motion_entry.get("reset_frame")), 3) if motion_entry.get("reset_frame") is not None else "",
                    "resetFrames": ",".join(str(round(float(frame), 3)) for frame in (motion_entry.get("reset_frames", []) or [])[:8]),
                    "resetReason": motion_entry.get("reset_reason", ""),
                    "blendType": "ADD",
                },
            )

        for placement_entry in placement_key_data_by_prop.values():
            prop_obj = placement_entry["object"]
            placement_keys = list(placement_entry["keys"])
            if prop_obj is None or not placement_keys:
                continue
            placement_keys.sort(key=lambda item: item[0])
            action_keys = []
            if placement_keys[0][0] > float(frame_offset):
                action_keys.append((float(frame_offset), object_transform_values(prop_obj), None))
            action_keys.extend(placement_keys)
            last_frame, last_values, last_event = action_keys[-1]
            if section_end_frame > last_frame:
                action_keys.append((section_end_frame, last_values, last_event))
            action_name = _safe_nla_track_name("ScenePlacement", getattr(prop_obj, "name", "Prop"))
            placement_action = create_object_transform_action(action_name, prop_obj, action_keys, interpolation='CONSTANT')
            if placement_action is not None:
                self.__assign_action(prop_obj, placement_action, track_name="ScenePlacement", at_frame=float(frame_offset))
                extend_nla_track_to_frame(prop_obj, "ScenePlacement", section_end_frame)

        for visibility_entry in visibility_key_data_by_prop.values():
            prop_obj = visibility_entry["object"]
            visibility_keys = list(visibility_entry["keys"])
            if prop_obj is None or not visibility_keys:
                continue
            visibility_keys.sort(key=lambda item: item[0])
            action_keys = []
            if visibility_keys[0][0] > float(frame_offset):
                action_keys.append((float(frame_offset), not bool(getattr(prop_obj, "hide_viewport", False)), None))
            action_keys.extend(visibility_keys)
            last_frame, last_visible, last_event = action_keys[-1]
            if section_end_frame > last_frame:
                action_keys.append((section_end_frame, last_visible, last_event))
            action_name = _safe_nla_track_name("SceneVisibility", getattr(prop_obj, "name", "Prop"))
            visibility_action = create_object_visibility_action(action_name, prop_obj, action_keys, interpolation='CONSTANT')
            if visibility_action is not None:
                self.__assign_action(prop_obj, visibility_action, track_name="SceneVisibility", at_frame=float(frame_offset))
                extend_nla_track_to_frame(prop_obj, "SceneVisibility", section_end_frame)

        _log_body_strip_overlap_debug()

        for target_obj in section_nla_targets:
            _sort_w2scene_nla_tracks_for_actor(target_obj)

        log.info(f'Loaded scene in {time.time() - s} seconds.')

def import_w3_scene(filePath):
    sceneImporter = SceneImporter()
    sceneImporter.loadSceneFile(filePath)
    return sceneImporter

def actor_entry_by_str(actor_tag_str, actors_dict):
    actor_entry = actors_dict.get(actor_tag_str)
    if actor_entry is None:
        needle = str(actor_tag_str or "").strip().lower()
        for _actor_id, candidate in actors_dict.items():
            actor = candidate[1]
            actor_tags = [str(tag or "").strip().lower() for tag in (getattr(actor, "actorTags", None) or [])]
            alias = str(getattr(actor, "alias", "") or "").strip().lower()
            if needle and (needle == alias or needle in actor_tags):
                actor_entry = candidate
                break
    return actor_entry

def set_cur_actor_by_str(actor_tag_str, actors_dict):
    actor_entry = actor_entry_by_str(actor_tag_str, actors_dict)
    if actor_entry is None or actor_entry[0] is None:
        log.warning("Scene actor '%s' is not loaded; skipping actor-bound event", actor_tag_str)
        return None

    curr_actor = actor_entry[0]
    bpy.ops.object.select_all(action='DESELECT')
    curr_actor.select_set(True)
    bpy.context.view_layer.objects.active = curr_actor
    return curr_actor
