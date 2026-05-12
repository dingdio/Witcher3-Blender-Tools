import logging
import json
import base64
import math
from dataclasses import dataclass, field

from mathutils import Matrix, Quaternion, Vector

from ..action_compat import (
    iter_action_fcurves,
    new_action_fcurve,
    resolve_action_slot,
)
from . import import_scene_animation
from .import_scene_fcurves import (
    collect_pose_transform_curves as _collect_pose_transform_curves,
    find_pose_bone_name as _find_pose_bone_name,
    set_fcurve_value_at_frame as _set_scene_fcurve_value_at_frame,
    transform_matrix_from_curve_groups as _transform_matrix_from_curve_groups,
)

log = logging.getLogger(__name__)

SCENE_MOTION_TRACK_NAME = "SceneMotionExtraction"
SCENE_MOTION_IMPORTER_VERSION = "action_motion_extraction_handoff_v4"

W2SCENE_MOTION_POSE_POLICY_PROP = "w3_scene_motion_pose_policy"
W2SCENE_MOTION_HAS_EXTRACTION_PROP = "w3_scene_motion_extraction_created"
W2SCENE_MOTION_POSE_NEUTRALIZED_PROP = "w3_scene_pose_trajectory_neutralized"
W2SCENE_EVENT_WEIGHT_PROP = "w3_scene_event_weight"
W2SCENE_EVENT_WEIGHT_CURVE_BAKED_PROP = "w3_scene_event_weight_curve_baked"
W2SCENE_ACTION_WEIGHT_PROP = "w3_scene_additive_weight"
W2SCENE_ACTION_WEIGHT_APPLIED_PROP = "w3_scene_additive_weight_applied"

W3_MOTION_EXTRACTION_FINAL_LOCATION_PROP = "w3_motion_extraction_final_location"
W3_MOTION_EXTRACTION_FINAL_YAW_PROP = "w3_motion_extraction_final_yaw"
W3_MOTION_EXTRACTION_FINAL_FRAME_PROP = "w3_motion_extraction_final_frame"
W3_MOTION_EXTRACTION_FLAGS_PROP = "w3_motion_extraction_flags"
W3_MOTION_EXTRACTION_SAMPLE_COUNT_PROP = "w3_motion_extraction_sample_count"
W3_MOTION_EXTRACTION_DATA_PROP = "w3_motion_extraction_data"

_MOTION_EXTRACTION_DATA_CACHE = {}


def _target_name(target):
    return str(getattr(target, "name", "") or "<unnamed>")


def _strip_text(value, default=""):
    text = str(value if value is not None else default).strip()
    return text if text else default


def _as_bool(value, default=False):
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    return bool(default)


def _as_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return float(default)


def _wrap_radians(value):
    return (float(value) + math.pi) % (math.pi * 2.0) - math.pi


def _yaw_from_matrix(matrix):
    try:
        return float(matrix.to_euler("XYZ").z)
    except Exception:
        return 0.0


def _xy_delta_from_matrices(prev_matrix, curr_matrix):
    delta_matrix = prev_matrix.inverted_safe() @ curr_matrix
    delta_loc = delta_matrix.to_translation()
    return Vector((float(delta_loc.x), float(delta_loc.y), float(delta_loc.z)))


def _yaw_delta_from_matrices(prev_matrix, curr_matrix):
    delta_matrix = prev_matrix.inverted_safe() @ curr_matrix
    return _wrap_radians(_yaw_from_matrix(delta_matrix))


def _set_fcurve_value_at_frame(fcurve, frame, value, interpolation='LINEAR'):
    _set_scene_fcurve_value_at_frame(
        fcurve,
        frame,
        value,
        interpolation=interpolation,
        update_existing_interpolation=True,
    )


def _event_uses_motion_extraction(event):
    return _as_bool(getattr(event, "useMotionExtraction", None), False)


def _event_uses_fake_motion(event):
    return _as_bool(getattr(event, "useFakeMotion", None), True)


def _event_weight(event, strip=None, action=None):
    for id_block in (strip, action):
        try:
            value = id_block.get(W2SCENE_EVENT_WEIGHT_PROP, None)
        except Exception:
            value = None
        if value is not None:
            return max(0.0, min(1.0, _as_float(value, 1.0)))
    return max(0.0, min(1.0, _as_float(getattr(event, "weight", None), 1.0)))


def _event_weight_is_baked(strip=None, action=None):
    for id_block in (strip, action):
        try:
            if bool(id_block.get(W2SCENE_EVENT_WEIGHT_CURVE_BAKED_PROP, False)):
                return True
            value = id_block.get(W2SCENE_ACTION_WEIGHT_APPLIED_PROP, None)
            if value is not None and abs(_as_float(value, 0.0) - 1.0) > 1e-6:
                return True
        except Exception:
            pass
    return False


def _strip_action_frame(strip, scene_frame, action_start):
    try:
        strip_start = float(getattr(strip, "frame_start", scene_frame) or scene_frame)
    except Exception:
        strip_start = float(scene_frame)
    try:
        scale = float(getattr(strip, "scale", 1.0) or 1.0)
    except Exception:
        scale = 1.0
    if abs(scale) <= 1e-8:
        scale = 1.0
    return float(action_start) + ((float(scene_frame) - strip_start) / scale)


def _strip_blend_factor(strip, scene_frame):
    try:
        strip_start = float(getattr(strip, "frame_start", scene_frame) or scene_frame)
        strip_end = float(getattr(strip, "frame_end", scene_frame) or scene_frame)
    except Exception:
        return 1.0
    if strip_end <= strip_start:
        return 0.0
    t = max(0.0, min(strip_end - strip_start, float(scene_frame) - strip_start))
    factor = 1.0
    try:
        blend_in = max(0.0, float(getattr(strip, "blend_in", 0.0) or 0.0))
    except Exception:
        blend_in = 0.0
    try:
        blend_out = max(0.0, float(getattr(strip, "blend_out", 0.0) or 0.0))
    except Exception:
        blend_out = 0.0
    if blend_in > 1e-6:
        factor = min(factor, max(0.0, min(1.0, t / blend_in)))
    remaining = max(0.0, strip_end - float(scene_frame))
    if blend_out > 1e-6:
        factor = min(factor, max(0.0, min(1.0, remaining / blend_out)))
    return max(0.0, min(1.0, factor))


def _sample_scene_frames(strip, section_start_frame, section_end_frame):
    try:
        strip_start = float(getattr(strip, "frame_start", section_start_frame) or section_start_frame)
        strip_end = float(getattr(strip, "frame_end", strip_start) or strip_start)
    except Exception:
        return []
    start = max(float(section_start_frame), strip_start)
    end = min(float(section_end_frame), strip_end)
    if end <= start:
        return []
    frames = {start, end}
    first_int = int(math.floor(start))
    last_int = int(math.ceil(end))
    for frame in range(first_int, last_int + 1):
        if start < float(frame) < end:
            frames.add(float(frame))
    return sorted(frames)


def _read_action_motion_extraction_terminal(action):
    if action is None:
        return None
    try:
        raw_location = action.get(W3_MOTION_EXTRACTION_FINAL_LOCATION_PROP, None)
    except Exception:
        raw_location = None
    if raw_location is None:
        return None
    if isinstance(raw_location, str):
        try:
            raw_location = json.loads(raw_location)
        except Exception:
            return None
    if not isinstance(raw_location, (list, tuple)) or len(raw_location) < 3:
        return None
    try:
        location = Vector((
            float(raw_location[0]),
            float(raw_location[1]),
            float(raw_location[2]),
        ))
    except Exception:
        return None
    try:
        yaw = _as_float(action.get(W3_MOTION_EXTRACTION_FINAL_YAW_PROP, 0.0), 0.0)
    except Exception:
        yaw = 0.0
    try:
        flags = int(action.get(W3_MOTION_EXTRACTION_FLAGS_PROP, 0) or 0)
    except Exception:
        flags = 0
    try:
        final_frame = _as_float(action.get(W3_MOTION_EXTRACTION_FINAL_FRAME_PROP, 0.0), 0.0)
    except Exception:
        final_frame = 0.0
    try:
        sample_count = int(action.get(W3_MOTION_EXTRACTION_SAMPLE_COUNT_PROP, 0) or 0)
    except Exception:
        sample_count = 0
    return {
        "location": location,
        "yaw": float(yaw),
        "flags": flags,
        "finalFrame": float(final_frame),
        "sampleCount": sample_count,
    }


def _motion_extraction_delta_times(value):
    if value is None:
        return []
    if isinstance(value, str):
        try:
            return [int(item) for item in base64.b64decode(value)]
        except Exception:
            return []
    if isinstance(value, (bytes, bytearray)):
        return [int(item) for item in value]
    try:
        return [int(item) for item in value]
    except Exception:
        return []


def _normalize_motion_extraction_data(raw_data):
    if isinstance(raw_data, str):
        try:
            raw_data = json.loads(raw_data)
        except Exception:
            return None
    if not isinstance(raw_data, dict):
        return None
    try:
        flags = int(raw_data.get("flags", 0) or 0)
        frames = [float(value) for value in (raw_data.get("frames") or [])]
        delta_times = _motion_extraction_delta_times(raw_data.get("deltaTimes", None))
        duration = _as_float(raw_data.get("duration", 0.0), 0.0)
    except Exception:
        return None
    axis_count = sum(1 for bit in (1, 2, 4, 8) if flags & bit)
    if flags == 0 or axis_count <= 0 or len(frames) < axis_count:
        return None
    sample_count = len(frames) // axis_count
    if sample_count <= 0:
        return None
    return {
        "duration": float(duration),
        "deltaTimes": delta_times[:max(0, sample_count - 1)],
        "frames": frames[:sample_count * axis_count],
        "flags": flags,
        "sampleCount": sample_count,
        "axisCount": axis_count,
    }


def _motion_extraction_terminal_from_data(motion_data):
    keyframes = _motion_extraction_keyframes(motion_data)
    if not keyframes:
        return None
    final_frame, final_loc, final_yaw = keyframes[-1]
    return {
        "location": final_loc,
        "yaw": float(final_yaw),
        "flags": int(motion_data.get("flags", 0) or 0),
        "finalFrame": float(final_frame),
        "sampleCount": int(motion_data.get("sampleCount", 0) or 0),
    }


def _write_action_motion_extraction_data(action, motion_data):
    if action is None or not motion_data:
        return
    try:
        action[W3_MOTION_EXTRACTION_DATA_PROP] = json.dumps({
            "duration": float(motion_data.get("duration", 0.0) or 0.0),
            "deltaTimes": [int(value) for value in (motion_data.get("deltaTimes") or [])],
            "frames": [float(value) for value in (motion_data.get("frames") or [])],
            "flags": int(motion_data.get("flags", 0) or 0),
        })
        terminal = _motion_extraction_terminal_from_data(motion_data)
        if terminal:
            loc = terminal["location"]
            action[W3_MOTION_EXTRACTION_FLAGS_PROP] = int(terminal["flags"])
            action[W3_MOTION_EXTRACTION_FINAL_LOCATION_PROP] = json.dumps([
                float(loc.x),
                float(loc.y),
                float(loc.z),
            ])
            action[W3_MOTION_EXTRACTION_FINAL_YAW_PROP] = float(terminal["yaw"])
            action[W3_MOTION_EXTRACTION_FINAL_FRAME_PROP] = float(terminal["finalFrame"])
            action[W3_MOTION_EXTRACTION_SAMPLE_COUNT_PROP] = int(terminal["sampleCount"])
    except Exception:
        pass


def _action_scene_motion_source(action):
    if action is None:
        return "", ""
    source_path = ""
    anim_name = ""
    for prop_name in ("w3_anim_source_file", "w3_scene_resolved_path"):
        try:
            source_path = str(action.get(prop_name, "") or "").strip()
        except Exception:
            source_path = ""
        if source_path:
            break
    for prop_name in ("w3_scene_resolved_animation", "w3_scene_requested_animation"):
        try:
            anim_name = str(action.get(prop_name, "") or "").strip()
        except Exception:
            anim_name = ""
        if anim_name:
            break
    if not anim_name:
        action_name = _target_name(action)
        base_name, dot, suffix = action_name.rpartition(".")
        anim_name = base_name if dot and suffix.isdigit() else action_name
    return source_path, anim_name


def _load_action_motion_extraction_data(action):
    source_path, anim_name = _action_scene_motion_source(action)
    if not source_path or not anim_name:
        return None
    cache_key = (source_path.lower(), anim_name.lower())
    if cache_key in _MOTION_EXTRACTION_DATA_CACHE:
        return _MOTION_EXTRACTION_DATA_CACHE[cache_key]
    motion_data = None
    try:
        from ..CR2W.dc_anims import load_bin_anims_single
        anim_set = load_bin_anims_single(source_path, anim_name, rigPath=None)
        entry = (anim_set.animations[0] if anim_set and anim_set.animations else None)
        animation = getattr(entry, "animation", None)
        motion_data = _normalize_motion_extraction_data(getattr(animation, "motionExtraction", None))
    except Exception:
        log.debug("Could not lazily load motion extraction for %s from %s", anim_name, source_path, exc_info=True)
        motion_data = None
    _MOTION_EXTRACTION_DATA_CACHE[cache_key] = motion_data
    if motion_data:
        _write_action_motion_extraction_data(action, motion_data)
        log.info(
            "Loaded scene motion extraction metadata on demand: action=%s animation=%s samples=%s flags=%s",
            _target_name(action),
            anim_name,
            motion_data.get("sampleCount", 0),
            motion_data.get("flags", 0),
        )
    return motion_data


def _read_action_motion_extraction_data(action):
    if action is None:
        return None
    try:
        raw_data = action.get(W3_MOTION_EXTRACTION_DATA_PROP, None)
    except Exception:
        raw_data = None
    motion_data = _normalize_motion_extraction_data(raw_data) if raw_data is not None else None
    if motion_data is not None:
        return motion_data
    return _load_action_motion_extraction_data(action)


def _motion_extraction_axis_order(flags):
    axis_order = []
    if flags & 1:
        axis_order.append("x")
    if flags & 2:
        axis_order.append("y")
    if flags & 4:
        axis_order.append("z")
    if flags & 8:
        axis_order.append("yaw")
    return axis_order


def _motion_extraction_keyframes(motion_data):
    if not motion_data:
        return []
    flags = int(motion_data.get("flags", 0) or 0)
    axis_order = _motion_extraction_axis_order(flags)
    axis_count = len(axis_order)
    frames = list(motion_data.get("frames") or [])
    if axis_count <= 0 or not frames:
        return []
    sample_count = int(motion_data.get("sampleCount", 0) or 0)
    if sample_count <= 0:
        sample_count = len(frames) // axis_count
    delta_times = list(motion_data.get("deltaTimes") or [])
    keyframes = []
    action_frame = 0.0
    for sample_index in range(sample_count):
        sample = frames[sample_index * axis_count:(sample_index + 1) * axis_count]
        values = {"x": 0.0, "y": 0.0, "z": 0.0, "yaw": 0.0}
        for axis, value in zip(axis_order, sample):
            values[axis] = float(value)
        keyframes.append((
            float(action_frame),
            Vector((values["x"], values["y"], values["z"])),
            float(values["yaw"]),
        ))
        if sample_index < len(delta_times):
            action_frame += float(delta_times[sample_index])
    return keyframes


def _motion_extraction_value_at_frame(keyframes, action_frame):
    if not keyframes:
        return Vector((0.0, 0.0, 0.0)), 0.0
    frame = float(action_frame)
    first_frame, first_loc, first_yaw = keyframes[0]
    if frame <= float(first_frame):
        return first_loc.copy(), float(first_yaw)
    last_frame, last_loc, last_yaw = keyframes[-1]
    if frame >= float(last_frame):
        return last_loc.copy(), float(last_yaw)
    for left, right in zip(keyframes[:-1], keyframes[1:]):
        left_frame, left_loc, left_yaw = left
        right_frame, right_loc, right_yaw = right
        if float(left_frame) <= frame <= float(right_frame):
            span = max(1e-6, float(right_frame) - float(left_frame))
            factor = max(0.0, min(1.0, (frame - float(left_frame)) / span))
            loc = left_loc.lerp(right_loc, factor)
            yaw = float(left_yaw) + ((float(right_yaw) - float(left_yaw)) * factor)
            return loc, yaw
    return last_loc.copy(), float(last_yaw)


def _motion_extraction_matrix_at_frame(keyframes, action_frame):
    loc, yaw = _motion_extraction_value_at_frame(keyframes, action_frame)
    return Matrix.Translation(loc) @ Matrix.Rotation(float(yaw), 4, 'Z')


def _collect_visible_root_motion_curves(action, armature_obj):
    if action is None or armature_obj is None:
        return None
    try:
        slot = resolve_action_slot(action, target=armature_obj, ensure=True)
        curves_by_bone = _collect_pose_transform_curves(action, armature_obj, slot)
    except Exception:
        log.debug("Could not collect pose root motion curves from %s", _target_name(action), exc_info=True)
        return None

    source_name = None
    for wanted_name in ("Trajectory", "Root", "Reference"):
        bone_name = _find_pose_bone_name(armature_obj, wanted_name)
        if bone_name and bone_name in curves_by_bone:
            source_name = bone_name
            break
    if not source_name:
        return None
    return source_name, curves_by_bone.get(source_name) or {}


@dataclass
class SceneMotionInterval:
    start_frame: float
    end_frame: float
    dx: float
    dy: float
    dz: float
    dyaw: float
    weight: float


@dataclass
class SceneMotionEvent:
    actor_obj: object
    armature_obj: object
    event: object
    action_name: str
    track_name: str
    start_frame: float
    end_frame: float
    event_weight: float
    motion_weight: float
    weight_baked: bool
    trajectory_bone: str
    intervals: list = field(default_factory=list)
    diagnostic: dict = field(default_factory=dict)


def _strip_transfer_frame(strip, section_end_frame=0.0):
    try:
        end_frame = float(getattr(strip, "frame_end", 0.0) or 0.0)
    except Exception:
        end_frame = 0.0
    return end_frame


def build_sampled_root_motion_carry_event_from_strip(
    actor_obj,
    armature_obj,
    strip,
    *,
    event=None,
    section="",
    track_name="",
    section_start_frame=0.0,
    section_end_frame=0.0,
):
    action = getattr(strip, "action", None) if strip is not None else None
    if actor_obj is None or armature_obj is None or strip is None or action is None:
        return None

    source_data = _collect_visible_root_motion_curves(action, armature_obj)
    if source_data is None:
        import_scene_animation.warn_scene_animation_debug(
            "scene motion extraction skipped; action has no visible root motion curves",
            action=action,
            strip=strip,
            armature_obj=armature_obj,
            event=event,
            section=section,
            track_name=track_name,
            details={
                "useMotionExtraction": _event_uses_motion_extraction(event),
                "useFakeMotion": _event_uses_fake_motion(event),
            },
        )
        return None

    source_name, curves = source_data
    scene_frames = _sample_scene_frames(strip, section_start_frame, section_end_frame)
    if len(scene_frames) < 2:
        return None

    try:
        action_frame_start = float(getattr(strip, "action_frame_start", action.frame_range[0]) or action.frame_range[0])
    except Exception:
        action_frame_start = 0.0

    event_weight = _event_weight(event, strip=strip, action=action)
    weight_baked = _event_weight_is_baked(strip=strip, action=action)
    motion_weight = 1.0 if weight_baked else event_weight

    action_frames = [_strip_action_frame(strip, frame, action_frame_start) for frame in scene_frames]
    intervals = []
    for prev_scene_frame, curr_scene_frame, prev_action_frame, curr_action_frame in zip(
        scene_frames[:-1],
        scene_frames[1:],
        action_frames[:-1],
        action_frames[1:],
    ):
        prev_matrix = _transform_matrix_from_curve_groups(curves, prev_action_frame)
        curr_matrix = _transform_matrix_from_curve_groups(curves, curr_action_frame)
        delta_loc = _xy_delta_from_matrices(prev_matrix, curr_matrix)
        dyaw = _yaw_delta_from_matrices(prev_matrix, curr_matrix)
        blend_factor = _strip_blend_factor(strip, curr_scene_frame)
        interval_weight = max(0.0, min(1.0, motion_weight * blend_factor))
        if interval_weight <= 0.0:
            continue
        intervals.append(SceneMotionInterval(
            start_frame=float(prev_scene_frame),
            end_frame=float(curr_scene_frame),
            dx=float(delta_loc.x),
            dy=float(delta_loc.y),
            dz=float(delta_loc.z),
            dyaw=float(dyaw),
            weight=float(interval_weight),
        ))

    if not intervals:
        return None

    start_frame = scene_frames[0]
    end_frame = scene_frames[-1]
    first_matrix = _transform_matrix_from_curve_groups(curves, action_frames[0])
    last_matrix = _transform_matrix_from_curve_groups(curves, action_frames[-1])
    total_delta = first_matrix.inverted_safe() @ last_matrix
    total_loc = total_delta.to_translation()
    transfer_frame = _strip_transfer_frame(strip, section_end_frame=section_end_frame)
    return SceneMotionEvent(
        actor_obj=actor_obj,
        armature_obj=armature_obj,
        event=event,
        action_name=_target_name(action),
        track_name=track_name,
        start_frame=start_frame,
        end_frame=end_frame,
        event_weight=event_weight,
        motion_weight=motion_weight,
        weight_baked=weight_baked,
        trajectory_bone=source_name,
        intervals=intervals,
        diagnostic={
            "source": "visible_pose_root_sampled",
            "sourceBone": source_name,
            "objectPlayback": "carry",
            "xyDelta": round(math.sqrt((total_loc.x ** 2) + (total_loc.y ** 2)), 6),
            "zDelta": round(float(total_loc.z), 6),
            "yawDeltaDeg": round(math.degrees(_wrap_radians(_yaw_from_matrix(total_delta))), 3),
            "transferFrame": float(transfer_frame),
            "actionStart": round(float(action_frames[0]), 3),
            "actionEnd": round(float(action_frames[-1]), 3),
            "sampleCount": len(intervals) + 1,
        },
    )


def build_sampled_motion_extraction_event_from_strip(
    actor_obj,
    armature_obj,
    strip,
    *,
    event=None,
    section="",
    track_name="",
    section_start_frame=0.0,
    section_end_frame=0.0,
):
    action = getattr(strip, "action", None) if strip is not None else None
    if actor_obj is None or armature_obj is None or strip is None or action is None:
        return None

    motion_data = _read_action_motion_extraction_data(action)
    if motion_data is None:
        return None
    motion_keyframes = _motion_extraction_keyframes(motion_data)
    if len(motion_keyframes) < 2:
        return None

    scene_frames = _sample_scene_frames(strip, section_start_frame, section_end_frame)
    if len(scene_frames) < 2:
        return None

    try:
        action_frame_start = float(getattr(strip, "action_frame_start", action.frame_range[0]) or action.frame_range[0])
    except Exception:
        action_frame_start = 0.0

    event_weight = _event_weight(event, strip=strip, action=action)
    weight_baked = _event_weight_is_baked(strip=strip, action=action)
    motion_weight = 1.0 if weight_baked else event_weight

    action_frames = [_strip_action_frame(strip, frame, action_frame_start) for frame in scene_frames]
    intervals = []
    for prev_scene_frame, curr_scene_frame, prev_action_frame, curr_action_frame in zip(
        scene_frames[:-1],
        scene_frames[1:],
        action_frames[:-1],
        action_frames[1:],
    ):
        prev_matrix = _motion_extraction_matrix_at_frame(motion_keyframes, prev_action_frame)
        curr_matrix = _motion_extraction_matrix_at_frame(motion_keyframes, curr_action_frame)
        delta_loc = _xy_delta_from_matrices(prev_matrix, curr_matrix)
        dyaw = _yaw_delta_from_matrices(prev_matrix, curr_matrix)
        blend_factor = _strip_blend_factor(strip, curr_scene_frame)
        interval_weight = max(0.0, min(1.0, motion_weight * blend_factor))
        if interval_weight <= 0.0:
            continue
        intervals.append(SceneMotionInterval(
            start_frame=float(prev_scene_frame),
            end_frame=float(curr_scene_frame),
            dx=float(delta_loc.x),
            dy=float(delta_loc.y),
            dz=float(delta_loc.z),
            dyaw=float(dyaw),
            weight=float(interval_weight),
        ))

    if not intervals:
        return None

    first_matrix = _motion_extraction_matrix_at_frame(motion_keyframes, action_frames[0])
    last_matrix = _motion_extraction_matrix_at_frame(motion_keyframes, action_frames[-1])
    total_delta = first_matrix.inverted_safe() @ last_matrix
    total_loc = total_delta.to_translation()
    transfer_frame = _strip_transfer_frame(strip, section_end_frame=section_end_frame)
    return SceneMotionEvent(
        actor_obj=actor_obj,
        armature_obj=armature_obj,
        event=event,
        action_name=_target_name(action),
        track_name=track_name,
        start_frame=scene_frames[0],
        end_frame=scene_frames[-1],
        event_weight=event_weight,
        motion_weight=motion_weight,
        weight_baked=weight_baked,
        trajectory_bone="",
        intervals=intervals,
        diagnostic={
            "source": "action_motion_extraction_sampled",
            "sourceBone": "",
            "objectPlayback": "carry",
            "xyDelta": round(math.sqrt((total_loc.x ** 2) + (total_loc.y ** 2)), 6),
            "zDelta": round(float(total_loc.z), 6),
            "yawDeltaDeg": round(math.degrees(_wrap_radians(_yaw_from_matrix(total_delta))), 3),
            "transferFrame": float(transfer_frame),
            "actionStart": round(float(action_frames[0]), 3),
            "actionEnd": round(float(action_frames[-1]), 3),
            "flags": int(motion_data.get("flags", 0) or 0),
            "sourceMotionSamples": int(motion_data.get("sampleCount", 0) or 0),
            "sampleCount": len(intervals) + 1,
        },
    )


def build_terminal_motion_extraction_event_from_strip(
    actor_obj,
    armature_obj,
    strip,
    *,
    event=None,
    section="",
    track_name="",
    section_end_frame=0.0,
):
    action = getattr(strip, "action", None) if strip is not None else None
    if actor_obj is None or armature_obj is None or strip is None or action is None:
        return None

    terminal = _read_action_motion_extraction_terminal(action)
    if terminal is None:
        return None

    try:
        start_frame = float(getattr(strip, "frame_start", 0.0) or 0.0)
        end_frame = float(getattr(strip, "frame_end", start_frame) or start_frame)
    except Exception:
        start_frame = 0.0
        end_frame = 0.0
    if end_frame <= start_frame:
        return None

    event_weight = _event_weight(event, strip=strip, action=action)
    weight_baked = _event_weight_is_baked(strip=strip, action=action)
    motion_weight = 1.0 if weight_baked else event_weight
    location = terminal["location"]
    yaw = float(terminal["yaw"])
    return SceneMotionEvent(
        actor_obj=actor_obj,
        armature_obj=armature_obj,
        event=event,
        action_name=_target_name(action),
        track_name=track_name,
        start_frame=start_frame,
        end_frame=end_frame,
        event_weight=event_weight,
        motion_weight=motion_weight,
        weight_baked=weight_baked,
        trajectory_bone="",
        intervals=[
            SceneMotionInterval(
                start_frame=float(start_frame),
                end_frame=float(end_frame),
                dx=float(location.x),
                dy=float(location.y),
                dz=float(location.z),
                dyaw=float(yaw),
                weight=float(motion_weight),
            )
        ],
        diagnostic={
            "source": "action_motion_extraction_terminal",
            "sourceBone": "",
            "objectPlayback": "carry",
            "xyDelta": round(math.sqrt((location.x ** 2) + (location.y ** 2)), 6),
            "zDelta": round(float(location.z), 6),
            "yawDeltaDeg": round(math.degrees(_wrap_radians(yaw)), 3),
            "transferFrame": float(end_frame),
            "flags": int(terminal.get("flags", 0) or 0),
            "sampleCount": int(terminal.get("sampleCount", 0) or 0),
            "motionFinalFrame": round(float(terminal.get("finalFrame", 0.0) or 0.0), 3),
        },
    )


def build_terminal_root_motion_carry_event_from_strip(*args, **kwargs):
    return build_terminal_motion_extraction_event_from_strip(*args, **kwargs)


class SceneMotionAccumulator:
    def __init__(self, *, section="", section_start_frame=0.0, section_end_frame=0.0):
        self.section = section
        self.section_start_frame = float(section_start_frame)
        self.section_end_frame = float(section_end_frame)
        self._events_by_actor = {}

    def add_event(self, motion_event):
        if motion_event is None:
            return False
        key = getattr(motion_event.actor_obj, "name", "") or str(id(motion_event.actor_obj))
        self._events_by_actor.setdefault(key, {
            "object": motion_event.actor_obj,
            "events": [],
        })["events"].append(motion_event)
        return True

    def actor_count(self):
        return len(self._events_by_actor)

    def event_count(self):
        return sum(len(entry.get("events", [])) for entry in self._events_by_actor.values())

    def placement_defer_windows_by_actor(self):
        windows_by_actor = {}
        for key, entry in self._events_by_actor.items():
            actor_obj = entry.get("object")
            actor_key = getattr(actor_obj, "name", "") or key or str(id(actor_obj))
            windows = []
            for motion_event in entry.get("events", []) or []:
                if motion_event is None:
                    continue
                diagnostic = motion_event.diagnostic or {}
                source = str(diagnostic.get("source", "") or "")
                object_playback = str(diagnostic.get("objectPlayback", "") or "")
                if source not in {
                    "visible_pose_root_sampled",
                    "action_motion_extraction_sampled",
                    "action_motion_extraction_terminal",
                }:
                    continue
                if object_playback not in {"carry", "animated"}:
                    continue
                try:
                    start_frame = float(motion_event.start_frame)
                    end_frame = float(motion_event.end_frame)
                    transfer_frame = float((motion_event.diagnostic or {}).get("transferFrame", end_frame))
                except Exception:
                    continue
                if end_frame <= start_frame or transfer_frame <= start_frame:
                    continue
                windows.append({
                    "startFrame": start_frame,
                    "endFrame": end_frame,
                    "transferFrame": transfer_frame,
                    "action": str(motion_event.action_name or ""),
                    "source": source,
                })
            if not windows:
                continue
            windows.sort(key=lambda item: (item["startFrame"], item["endFrame"], item["transferFrame"]))
            windows_by_actor[actor_key] = windows
            if actor_obj is not None:
                windows_by_actor[str(id(actor_obj))] = windows
        return windows_by_actor

    def _actor_keyframes(self, entry, reset_frames=None):
        events = list(entry.get("events", []) or [])
        reset_frames = sorted(
            {
                float(frame)
                for frame in (reset_frames or [])
                if self.section_start_frame < float(frame) <= self.section_end_frame
            }
        )
        frames = {self.section_start_frame, self.section_end_frame}
        frames.update(reset_frames)
        for motion_event in events:
            for interval in motion_event.intervals:
                frames.add(float(interval.start_frame))
                frames.add(float(interval.end_frame))
        sorted_frames = sorted(frame for frame in frames if self.section_start_frame <= frame <= self.section_end_frame)
        if len(sorted_frames) < 2:
            return []

        loc = Vector((0.0, 0.0, 0.0))
        yaw = 0.0
        keyframes = [(sorted_frames[0], loc.copy(), yaw)]
        reset_lookup = {round(float(frame), 4) for frame in reset_frames}
        for prev_frame, curr_frame in zip(sorted_frames[:-1], sorted_frames[1:]):
            active = []
            for motion_event in events:
                for interval in motion_event.intervals:
                    overlap_start = max(float(prev_frame), float(interval.start_frame))
                    overlap_end = min(float(curr_frame), float(interval.end_frame))
                    if overlap_end <= overlap_start:
                        continue
                    interval_duration = max(1e-6, float(interval.end_frame) - float(interval.start_frame))
                    coverage = max(0.0, min(1.0, (overlap_end - overlap_start) / interval_duration))
                    if coverage <= 0.0:
                        continue
                    active.append((interval, coverage))
            if active:
                denom = max(1.0, sum(float(interval.weight) * float(coverage) for interval, coverage in active))
                dx = sum(float(interval.dx) * float(interval.weight) * float(coverage) for interval, coverage in active) / denom
                dy = sum(float(interval.dy) * float(interval.weight) * float(coverage) for interval, coverage in active) / denom
                dz = sum(float(interval.dz) * float(interval.weight) * float(coverage) for interval, coverage in active) / denom
                dyaw = sum(float(interval.dyaw) * float(interval.weight) * float(coverage) for interval, coverage in active) / denom
                yaw_rot = Matrix.Rotation(yaw, 4, 'Z')
                loc += yaw_rot @ Vector((dx, dy, dz))
                yaw = _wrap_radians(yaw + dyaw)
            if round(float(curr_frame), 4) in reset_lookup:
                before_reset = max(float(prev_frame), float(curr_frame) - 0.001)
                if before_reset < float(curr_frame) - 1e-6 and (
                    loc.length > 1e-7 or abs(float(yaw)) > 1e-7
                ):
                    keyframes.append((before_reset, loc.copy(), float(yaw)))
                loc = Vector((0.0, 0.0, 0.0))
                yaw = 0.0
            keyframes.append((float(curr_frame), loc.copy(), float(yaw)))
        return keyframes

    def _actor_reset_frames(self, actor_obj, actor_name, reset_frames_by_actor):
        if not reset_frames_by_actor:
            return []
        raw_frames = reset_frames_by_actor.get(actor_name)
        if raw_frames is None:
            raw_frames = reset_frames_by_actor.get(str(id(actor_obj)))
        if raw_frames is None:
            return []
        if isinstance(raw_frames, (list, tuple, set)):
            values = raw_frames
        else:
            values = [raw_frames]
        frames = []
        for raw_frame in values:
            try:
                frame = float(raw_frame)
            except Exception:
                continue
            if self.section_start_frame < frame <= self.section_end_frame + 1e-4:
                frames.append(frame)
        return sorted(set(frames))

    def _actor_placement_yaw_keys(self, actor_obj, actor_name, placement_yaws_by_actor):
        if not placement_yaws_by_actor:
            return []
        raw_keys = placement_yaws_by_actor.get(actor_name)
        if raw_keys is None:
            raw_keys = placement_yaws_by_actor.get(str(id(actor_obj)))
        if raw_keys is None:
            return []
        keys = []
        for item in raw_keys:
            try:
                frame, yaw = item
                keys.append((float(frame), float(yaw)))
            except Exception:
                continue
        return sorted(keys)

    def build_actor_motion_actions(
        self,
        *,
        action_name_factory=None,
        playback_mode="carry",
        reset_frames_by_actor=None,
        placement_yaws_by_actor=None,
    ):
        actions = []
        try:
            import bpy
        except Exception:
            return actions

        reset_frames_by_actor = reset_frames_by_actor or {}
        placement_yaws_by_actor = placement_yaws_by_actor or {}
        default_playback_mode = _strip_text(playback_mode, "carry")
        for entry in self._events_by_actor.values():
            actor_obj = entry.get("object")
            actor_name = getattr(actor_obj, "name", "") or str(id(actor_obj))
            reset_frames = self._actor_reset_frames(actor_obj, actor_name, reset_frames_by_actor)
            placement_yaw_keys = self._actor_placement_yaw_keys(actor_obj, actor_name, placement_yaws_by_actor)
            sampled_keyframes = self._actor_keyframes(entry, reset_frames=reset_frames)
            if actor_obj is None or len(sampled_keyframes) < 2:
                continue
            events = list(entry.get("events", []) or [])
            has_animated_object_motion = any(
                str((motion_event.diagnostic or {}).get("objectPlayback", "") or "") == "animated"
                for motion_event in events
            )
            actor_playback_mode = default_playback_mode
            if actor_playback_mode == "animated":
                keyframes = [(float(frame), loc.copy(), float(yaw)) for frame, loc, yaw in sampled_keyframes]
                interpolation = 'LINEAR'
            else:
                state_frames = {self.section_start_frame, self.section_end_frame}
                state_frames.update(reset_frames)
                if has_animated_object_motion:
                    for reset_frame_value in reset_frames:
                        before_reset = max(self.section_start_frame, float(reset_frame_value) - 0.001)
                        if before_reset < float(reset_frame_value) - 1e-6:
                            state_frames.add(before_reset)
                for motion_event in events:
                    object_playback = str((motion_event.diagnostic or {}).get("objectPlayback", "") or "")
                    if object_playback == "animated":
                        for interval in motion_event.intervals:
                            state_frames.add(float(interval.start_frame))
                            state_frames.add(float(interval.end_frame))
                    else:
                        transfer_frame = motion_event.diagnostic.get("transferFrame", motion_event.end_frame)
                        state_frames.add(float(transfer_frame))
                keyframes = []
                for state_frame in sorted(frame for frame in state_frames if self.section_start_frame <= frame <= self.section_end_frame):
                    candidates = [item for item in sampled_keyframes if float(item[0]) <= float(state_frame) + 1e-4]
                    chosen = candidates[-1] if candidates else sampled_keyframes[0]
                    keyframes.append((float(state_frame), chosen[1].copy(), float(chosen[2])))
                interpolation = 'LINEAR' if has_animated_object_motion else 'CONSTANT'
            reset_frame = reset_frames[0] if reset_frames else None
            reset_reason = "explicitScenePlacement" if reset_frames else ""
            if len(keyframes) < 2:
                continue
            motion_sources = sorted({
                str((motion_event.diagnostic or {}).get("source", "") or "")
                for motion_event in events
                if str((motion_event.diagnostic or {}).get("source", "") or "")
            })
            object_playbacks = sorted({
                str((motion_event.diagnostic or {}).get("objectPlayback", "") or "")
                for motion_event in events
                if str((motion_event.diagnostic or {}).get("objectPlayback", "") or "")
            })
            selection_reasons = sorted({
                str((motion_event.diagnostic or {}).get("selectionReason", "") or "")
                for motion_event in events
                if str((motion_event.diagnostic or {}).get("selectionReason", "") or "")
            })
            action_name = (
                action_name_factory(actor_obj)
                if action_name_factory is not None
                else f"{SCENE_MOTION_TRACK_NAME}_{_target_name(actor_obj)}"
            )
            action = bpy.data.actions.new(name=action_name)
            loc_curves = [
                new_action_fcurve(action, actor_obj, data_path='location', index=axis_i, group_name="MotionExtraction")
                for axis_i in range(3)
            ]
            rotation_mode = str(getattr(actor_obj, "rotation_mode", "XYZ") or "XYZ")
            if rotation_mode == "QUATERNION":
                rot_path = "rotation_quaternion"
                rot_len = 4
            elif rotation_mode == "AXIS_ANGLE":
                rot_path = "rotation_axis_angle"
                rot_len = 4
            else:
                rot_path = "rotation_euler"
                rot_len = 3
            rot_curves = [
                new_action_fcurve(action, actor_obj, data_path=rot_path, index=axis_i, group_name="MotionExtraction")
                for axis_i in range(rot_len)
            ]
            try:
                placement_yaw = float(actor_obj.matrix_world.to_euler("XYZ").z)
            except Exception:
                try:
                    placement_yaw = float(actor_obj.rotation_euler.z)
                except Exception:
                    placement_yaw = 0.0

            def placement_yaw_at(frame):
                yaw_value = placement_yaw
                for key_frame, key_yaw in placement_yaw_keys:
                    if float(key_frame) <= float(frame) + 1e-4:
                        yaw_value = float(key_yaw)
                    else:
                        break
                return yaw_value

            for frame, loc, yaw in keyframes:
                # Engine scene motion is accumulated in actor-local space, then
                # multiplied by the actor placement. Object location F-curves are
                # keyed in parent/scene axes, so rotate the carried offset by the
                # current placement yaw before adding it to ScenePlacement.
                placement_rot = Matrix.Rotation(placement_yaw_at(frame), 4, 'Z')
                keyed_loc = placement_rot @ loc
                for axis_i, value in enumerate((keyed_loc.x, keyed_loc.y, keyed_loc.z)):
                    _set_fcurve_value_at_frame(loc_curves[axis_i], frame, value, interpolation=interpolation)
                if rot_path == "rotation_quaternion":
                    yaw_quat = Quaternion((0.0, 0.0, 1.0), yaw)
                    rot_values = (yaw_quat.w, yaw_quat.x, yaw_quat.y, yaw_quat.z)
                elif rot_path == "rotation_axis_angle":
                    rot_values = (yaw, 0.0, 0.0, 1.0)
                else:
                    rot_values = (0.0, 0.0, yaw)
                for axis_i, value in enumerate(rot_values):
                    _set_fcurve_value_at_frame(rot_curves[axis_i], frame, value, interpolation=interpolation)
            for fcurve in iter_action_fcurves(action, target=actor_obj):
                try:
                    fcurve.update()
                except Exception:
                    pass
            total_xy = 0.0
            span_xy = 0.0
            total_yaw = 0.0
            if sampled_keyframes:
                total_xy = math.sqrt(sampled_keyframes[-1][1].x ** 2 + sampled_keyframes[-1][1].y ** 2)
                span_xy = max(
                    math.sqrt(item[1].x ** 2 + item[1].y ** 2)
                    for item in sampled_keyframes
                )
                total_yaw = math.degrees(_wrap_radians(sampled_keyframes[-1][2] - sampled_keyframes[0][2]))
            try:
                action[W2SCENE_MOTION_HAS_EXTRACTION_PROP] = True
                action["w3_scene_motion_event_count"] = len(entry.get("events", []) or [])
                action["w3_scene_motion_key_count"] = len(keyframes)
                action["w3_scene_motion_sample_key_count"] = len(sampled_keyframes)
                action["w3_scene_motion_xy_delta"] = float(total_xy)
                action["w3_scene_motion_xy_span"] = float(span_xy)
                action["w3_scene_motion_yaw_delta_deg"] = float(total_yaw)
                action["w3_scene_motion_rotation_path"] = rot_path
                action["w3_scene_motion_location_space"] = "blender_scene_axes"
                action["w3_scene_motion_placement_yaw_deg"] = math.degrees(placement_yaw)
                action["w3_scene_motion_has_animated_object_motion"] = bool(has_animated_object_motion)
                action["w3_scene_motion_sources"] = ",".join(motion_sources)
                action["w3_scene_motion_object_playbacks"] = ",".join(object_playbacks)
                action["w3_scene_motion_selection_reasons"] = ",".join(selection_reasons)
                action["w3_scene_motion_placement_yaw_keys"] = ",".join(
                    f"{round(float(frame), 3)}:{round(math.degrees(float(yaw)), 3)}"
                    for frame, yaw in placement_yaw_keys[:8]
                )
                action["w3_scene_motion_playback_mode"] = actor_playback_mode
                action["w3_scene_motion_reset_count"] = len(reset_frames)
                if reset_frame is not None:
                    action["w3_scene_motion_reset_frame"] = float(reset_frame)
                    action["w3_scene_motion_reset_reason"] = reset_reason
                    action["w3_scene_motion_reset_frames"] = ",".join(str(round(float(frame), 3)) for frame in reset_frames)
            except Exception:
                pass
            actions.append({
                "actor_obj": actor_obj,
                "action": action,
                "keyframes": keyframes,
                "sample_keyframes": sampled_keyframes,
                "event_count": len(entry.get("events", []) or []),
                "xy_delta": total_xy,
                "xy_span": span_xy,
                "yaw_delta_deg": total_yaw,
                "rotation_path": rot_path,
                "location_space": "blender_scene_axes",
                "placement_yaw_deg": math.degrees(placement_yaw),
                "playback_mode": actor_playback_mode,
                "has_animated_object_motion": bool(has_animated_object_motion),
                "motion_sources": motion_sources,
                "object_playbacks": object_playbacks,
                "selection_reasons": selection_reasons,
                "reset_frame": reset_frame,
                "reset_frames": list(reset_frames),
                "reset_reason": reset_reason,
            })
        return actions


def process_body_strip_motion(
    accumulator,
    actor_obj,
    armature_obj,
    strip,
    *,
    event=None,
    section="",
    track_name="",
    section_start_frame=0.0,
    section_end_frame=0.0,
):
    action = getattr(strip, "action", None) if strip is not None else None
    if action is None:
        return {
            "motionEventAdded": False,
            "posePolicy": "no_action",
            "poseNeutralized": False,
        }

    use_motion_extraction = _event_uses_motion_extraction(event)
    use_fake_motion = _event_uses_fake_motion(event)
    pose_policy = "keep_fake_motion" if use_fake_motion else "keep_pose_motion_record_actor_state"

    motion_event = None
    if use_motion_extraction:
        extracted_motion_event = build_sampled_motion_extraction_event_from_strip(
            actor_obj,
            armature_obj,
            strip,
            event=event,
            section=section,
            track_name=track_name,
            section_start_frame=section_start_frame,
            section_end_frame=section_end_frame,
        )
        terminal_motion_event = build_terminal_motion_extraction_event_from_strip(
            actor_obj,
            armature_obj,
            strip,
            event=event,
            section=section,
            track_name=track_name,
            section_end_frame=section_end_frame,
        )
        visible_motion_event = None
        if extracted_motion_event is not None:
            motion_event = extracted_motion_event
            try:
                motion_event.diagnostic["selectionReason"] = "action_motion_extraction_handoff"
            except Exception:
                pass
        elif terminal_motion_event is not None:
            motion_event = terminal_motion_event
            try:
                motion_event.diagnostic["selectionReason"] = "terminal_motion_extraction_handoff_fallback"
            except Exception:
                pass
        else:
            visible_motion_event = build_sampled_root_motion_carry_event_from_strip(
                actor_obj,
                armature_obj,
                strip,
                event=event,
                section=section,
                track_name=track_name,
                section_start_frame=section_start_frame,
                section_end_frame=section_end_frame,
            )
            motion_event = visible_motion_event
            try:
                if motion_event is not None:
                    motion_event.diagnostic["selectionReason"] = "visible_root_fallback_no_action_motion_extraction"
            except Exception:
                pass
    motion_event_added = False
    if use_motion_extraction and motion_event is not None and accumulator is not None:
        motion_event_added = accumulator.add_event(motion_event)
    pose_neutralized = False
    if use_motion_extraction:
        if motion_event_added:
            object_playback = str((motion_event.diagnostic or {}).get("objectPlayback", "") or "") if motion_event is not None else ""
            pose_policy = (
                "animate_actor_motion_extraction_keep_pose"
                if object_playback == "animated"
                else "keep_nla_root_motion_carry_actor_state"
            )
        else:
            pose_policy = "keep_nla_root_motion_no_actor_carry"

    final_action = getattr(strip, "action", action) if strip is not None else action
    for id_block in (final_action, strip):
        if id_block is None:
            continue
        try:
            id_block[W2SCENE_MOTION_POSE_POLICY_PROP] = pose_policy
            id_block[W2SCENE_MOTION_HAS_EXTRACTION_PROP] = bool(motion_event_added)
            id_block[W2SCENE_MOTION_POSE_NEUTRALIZED_PROP] = bool(pose_neutralized)
        except Exception:
            pass

    details = {
        "useMotionExtraction": bool(use_motion_extraction),
        "useFakeMotion": bool(use_fake_motion),
        "posePolicy": pose_policy,
        "actorMotion": bool(motion_event_added),
        "poseNeutralized": bool(pose_neutralized),
        "trajectoryBone": motion_event.trajectory_bone if motion_event is not None else "",
    }
    if motion_event is not None:
        details.update({
            "eventWeight": round(float(motion_event.event_weight), 6),
            "motionWeight": round(float(motion_event.motion_weight), 6),
            "weightBaked": bool(motion_event.weight_baked),
            "intervals": len(motion_event.intervals),
            "motionSource": motion_event.diagnostic.get("source", ""),
            "motionSourceBone": motion_event.diagnostic.get("sourceBone", ""),
            "objectPlayback": motion_event.diagnostic.get("objectPlayback", ""),
            "selectionReason": motion_event.diagnostic.get("selectionReason", ""),
            "visibleRootXYDelta": motion_event.diagnostic.get("visibleRootXYDelta", 0.0),
            "extractedXYDelta": motion_event.diagnostic.get("extractedXYDelta", 0.0),
            "terminalXYDelta": motion_event.diagnostic.get("xyDelta", 0.0),
            "terminalZDelta": motion_event.diagnostic.get("zDelta", 0.0),
            "terminalYawDeltaDeg": motion_event.diagnostic.get("yawDeltaDeg", 0.0),
            "transferFrame": motion_event.diagnostic.get("transferFrame", 0.0),
            "actionStart": motion_event.diagnostic.get("actionStart", 0.0),
            "actionEnd": motion_event.diagnostic.get("actionEnd", 0.0),
            "motionFlags": motion_event.diagnostic.get("flags", 0),
            "motionSamples": motion_event.diagnostic.get("sampleCount", 0),
            "sourceMotionSamples": motion_event.diagnostic.get("sourceMotionSamples", 0),
            "motionFinalFrame": motion_event.diagnostic.get("motionFinalFrame", 0.0),
        })
    if use_motion_extraction:
        action_motion_data = _read_action_motion_extraction_data(action)
        action_motion_terminal = _read_action_motion_extraction_terminal(action)
        details.update({
            "hasActionMotionExtractionData": bool(action_motion_data),
            "hasActionMotionExtractionTerminal": bool(action_motion_terminal),
            "actionMotionExtractionSamples": int((action_motion_data or {}).get("sampleCount", 0) or 0),
        })
    if use_motion_extraction or not use_fake_motion or motion_event is not None:
        try:
            import_scene_animation.warn_scene_animation_debug(
                "scene motion policy",
                action=final_action,
                strip=strip,
                armature_obj=armature_obj,
                event=event,
                section=section,
                track_name=track_name,
                details=details,
            )
        except Exception:
            log.debug("Could not log scene motion policy for %s", _target_name(final_action), exc_info=True)

    return {
        "motionEventAdded": bool(motion_event_added),
        "posePolicy": pose_policy,
        "poseNeutralized": bool(pose_neutralized),
        "motionEvent": motion_event,
    }
