import logging
import math
from dataclasses import dataclass, field

from mathutils import Matrix, Quaternion, Vector

from ..action_compat import (
    bind_strip_action_slot,
    iter_action_fcurves,
    new_action_fcurve,
    resolve_action_slot,
)
from . import import_scene_animation
from .import_scene_fcurves import (
    collect_pose_transform_curves as _collect_pose_transform_curves,
    ensure_pose_transform_fcurves as _ensure_pose_transform_fcurves,
    find_pose_bone_name as _find_pose_bone_name,
    keyframe_frames as _keyframe_frames,
    set_fcurve_value_at_frame as _set_scene_fcurve_value_at_frame,
    transform_matrix_from_curve_groups as _transform_matrix_from_curve_groups,
)

log = logging.getLogger(__name__)

SCENE_MOTION_TRACK_NAME = "SceneMotionExtraction"

W2SCENE_ACTION_SCENE_COPY_PROP = "_w3_scene_action_copy"
W2SCENE_ACTION_SCENE_COPY_SOURCE_PROP = "_w3_scene_action_copy_source"
W2SCENE_MOTION_SOURCE_ACTION_PROP = "_w3_scene_motion_source_action"
W2SCENE_MOTION_POSE_POLICY_PROP = "w3_scene_motion_pose_policy"
W2SCENE_MOTION_HAS_EXTRACTION_PROP = "w3_scene_motion_extraction_created"
W2SCENE_MOTION_POSE_NEUTRALIZED_PROP = "w3_scene_pose_trajectory_neutralized"
W2SCENE_EVENT_WEIGHT_PROP = "w3_scene_event_weight"
W2SCENE_EVENT_WEIGHT_CURVE_BAKED_PROP = "w3_scene_event_weight_curve_baked"
W2SCENE_ACTION_WEIGHT_PROP = "w3_scene_additive_weight"
W2SCENE_ACTION_WEIGHT_APPLIED_PROP = "w3_scene_additive_weight_applied"


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


def _wrap_degrees(value):
    return (float(value) + 180.0) % 360.0 - 180.0


def _set_fcurve_value_at_frame(fcurve, frame, value, interpolation='LINEAR'):
    _set_scene_fcurve_value_at_frame(
        fcurve,
        frame,
        value,
        interpolation=interpolation,
        update_existing_interpolation=True,
    )


def _yaw_from_matrix(matrix):
    try:
        return float(matrix.to_euler("XYZ").z)
    except Exception:
        return 0.0


def _xy_delta_from_matrices(prev_matrix, curr_matrix):
    delta_matrix = prev_matrix.inverted_safe() @ curr_matrix
    delta_loc = delta_matrix.to_translation()
    return Vector((float(delta_loc.x), float(delta_loc.y), 0.0))


def _yaw_delta_from_matrices(prev_matrix, curr_matrix):
    delta_matrix = prev_matrix.inverted_safe() @ curr_matrix
    return _wrap_radians(_yaw_from_matrix(delta_matrix))


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


def _ensure_motion_edit_action(strip, armature_obj, *, event=None, section="", track_name="", reason="motion"):
    action = getattr(strip, "action", None) if strip is not None else None
    if action is None:
        return None
    try:
        if bool(action.get(W2SCENE_ACTION_SCENE_COPY_PROP, False)):
            return action
    except Exception:
        pass
    try:
        copied_action = action.copy()
    except Exception:
        log.debug("Could not copy scene action %s for %s", _target_name(action), reason, exc_info=True)
        return action
    try:
        copied_action.name = f"{action.name}_{reason}"
        copied_action[W2SCENE_ACTION_SCENE_COPY_PROP] = True
        copied_action[W2SCENE_ACTION_SCENE_COPY_SOURCE_PROP] = action.name
        copied_action[W2SCENE_MOTION_SOURCE_ACTION_PROP] = action.name
        strip.action = copied_action
        bind_strip_action_slot(strip, resolve_action_slot(copied_action, target=armature_obj, ensure=True))
    except Exception:
        log.debug("Could not assign scene motion action copy to strip %s", _target_name(strip), exc_info=True)
    import_scene_animation.warn_scene_animation_edit(
        "copied action for scene-only preprocessing",
        action=copied_action,
        strip=strip,
        armature_obj=armature_obj,
        event=event,
        section=section,
        track_name=track_name,
        details={
            "sourceAction": _target_name(action),
            "newAction": _target_name(copied_action),
            "reason": reason,
        },
    )
    return copied_action


def counteract_pose_trajectory_with_root(strip, armature_obj, *, event=None, section="", track_name=""):
    action = _ensure_motion_edit_action(
        strip,
        armature_obj,
        event=event,
        section=section,
        track_name=track_name,
        reason="scene_motion_root_counter",
    )
    if action is None or armature_obj is None:
        return {"changed": 0, "rootBone": "", "trajectoryBone": "", "frames": 0}

    root_name = _find_pose_bone_name(armature_obj, "Root")
    trajectory_name = _find_pose_bone_name(armature_obj, "Trajectory")
    if not root_name or not trajectory_name:
        return {"changed": 0, "rootBone": root_name or "", "trajectoryBone": trajectory_name or "", "frames": 0}

    slot = resolve_action_slot(action, target=armature_obj, ensure=True)
    curves_by_bone = _collect_pose_transform_curves(action, armature_obj, slot)
    trajectory_curves = curves_by_bone.get(trajectory_name)
    if not trajectory_curves:
        return {"changed": 0, "rootBone": root_name, "trajectoryBone": trajectory_name, "frames": 0}

    root_curves = curves_by_bone.setdefault(root_name, {
        "location": {},
        "rotation_quaternion": {},
        "rotation_euler": {},
        "scale": {},
    })
    _ensure_pose_transform_fcurves(action, armature_obj, slot, root_name, "location", root_curves["location"], create_if_empty=True)
    _ensure_pose_transform_fcurves(action, armature_obj, slot, root_name, "rotation_quaternion", root_curves["rotation_quaternion"], create_if_empty=True)
    _ensure_pose_transform_fcurves(action, armature_obj, slot, root_name, "scale", root_curves["scale"], create_if_empty=True)

    frames = set()
    for curves in trajectory_curves.values():
        for fcurve in curves.values():
            frames.update(_keyframe_frames(fcurve))
    if not frames:
        return {"changed": 0, "rootBone": root_name, "trajectoryBone": trajectory_name, "frames": 0}

    sorted_frames = sorted(frames)
    root_static = _transform_matrix_from_curve_groups(root_curves, sorted_frames[0])
    changed = 0
    yaw_values = []
    for frame in sorted_frames:
        trajectory_matrix = _transform_matrix_from_curve_groups(trajectory_curves, frame)
        counter_matrix = root_static @ trajectory_matrix.inverted_safe()
        loc, rot, scale = counter_matrix.decompose()
        rot.normalize()
        yaw_values.append(math.degrees(_yaw_from_matrix(trajectory_matrix)))
        for axis_i, value in enumerate((loc.x, loc.y, loc.z)):
            _set_fcurve_value_at_frame(root_curves["location"][axis_i], frame, value)
            changed += 1
        for axis_i, value in enumerate((rot.w, rot.x, rot.y, rot.z)):
            _set_fcurve_value_at_frame(root_curves["rotation_quaternion"][axis_i], frame, value)
            changed += 1
        for axis_i, value in enumerate((scale.x, scale.y, scale.z)):
            _set_fcurve_value_at_frame(root_curves["scale"][axis_i], frame, value)
            changed += 1

    try:
        pose_bone = armature_obj.pose.bones.get(root_name)
        if pose_bone is not None:
            pose_bone.rotation_mode = "QUATERNION"
    except Exception:
        pass
    for fcurve in iter_action_fcurves(action, target=armature_obj, slot=slot):
        try:
            fcurve.update()
        except Exception:
            pass
    try:
        action[W2SCENE_MOTION_POSE_POLICY_PROP] = "counteract_pose_trajectory_with_root"
        action[W2SCENE_MOTION_POSE_NEUTRALIZED_PROP] = True
        action["w3_scene_pose_trajectory_counteracted"] = True
        action["w3_scene_pose_trajectory_counter_frames"] = len(sorted_frames)
        action["w3_scene_pose_trajectory_counter_root_bone"] = root_name
        action["w3_scene_pose_trajectory_counter_trajectory_bone"] = trajectory_name
        action.update_tag()
    except Exception:
        pass
    if strip is not None:
        try:
            strip[W2SCENE_MOTION_POSE_POLICY_PROP] = "counteract_pose_trajectory_with_root"
            strip[W2SCENE_MOTION_POSE_NEUTRALIZED_PROP] = True
            strip["w3_scene_pose_trajectory_counteracted"] = True
        except Exception:
            pass

    details = {
        "reason": "useMotionExtraction_true_useFakeMotion_false",
        "rootBone": root_name,
        "trajectoryBone": trajectory_name,
        "frames": len(sorted_frames),
        "curveEdits": changed,
    }
    if yaw_values:
        first_yaw = yaw_values[0]
        rel_yaws = [_wrap_degrees(yaw - first_yaw) for yaw in yaw_values]
        details.update({
            "trajectoryYawDeltaDeg": round(_wrap_degrees(yaw_values[-1] - first_yaw), 3),
            "trajectoryYawSpanDeg": round(max(rel_yaws) - min(rel_yaws), 3),
        })
    import_scene_animation.warn_scene_animation_edit(
        "counteracted pose Trajectory motion with Root inverse for scene motion extraction",
        action=action,
        strip=strip,
        armature_obj=armature_obj,
        event=event,
        section=section,
        track_name=track_name,
        details=details,
    )
    return {
        "changed": changed,
        "rootBone": root_name,
        "trajectoryBone": trajectory_name,
        "frames": len(sorted_frames),
    }


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


def _trajectory_diagnostic(curves_by_prop, frames):
    if not curves_by_prop or not frames:
        return {
            "xyDelta": 0.0,
            "yawDeltaDeg": 0.0,
            "yawSpanDeg": 0.0,
            "samples": 0,
        }
    sorted_frames = sorted(frames)
    first_matrix = _transform_matrix_from_curve_groups(curves_by_prop, sorted_frames[0])
    first_loc = first_matrix.to_translation()
    first_yaw = _yaw_from_matrix(first_matrix)
    last_matrix = _transform_matrix_from_curve_groups(curves_by_prop, sorted_frames[-1])
    last_loc = last_matrix.to_translation()
    yaws = [_wrap_radians(_yaw_from_matrix(_transform_matrix_from_curve_groups(curves_by_prop, frame)) - first_yaw) for frame in sorted_frames]
    return {
        "xyDelta": round(math.sqrt((last_loc.x - first_loc.x) ** 2 + (last_loc.y - first_loc.y) ** 2), 6),
        "yawDeltaDeg": round(math.degrees(_wrap_radians(_yaw_from_matrix(last_matrix) - first_yaw)), 3),
        "yawSpanDeg": round(math.degrees(max(yaws) - min(yaws)), 3) if yaws else 0.0,
        "samples": len(sorted_frames),
    }


def build_motion_event_from_strip(
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

    slot = resolve_action_slot(action, target=armature_obj, ensure=True)
    curves_by_bone = _collect_pose_transform_curves(action, armature_obj, slot)
    trajectory_name = _find_pose_bone_name(armature_obj, "Trajectory")
    if not trajectory_name or trajectory_name not in curves_by_bone:
        import_scene_animation.warn_scene_animation_debug(
            "scene motion extraction skipped; action has no Trajectory curves",
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

    trajectory_curves = curves_by_bone.get(trajectory_name) or {}
    action_frames = [_strip_action_frame(strip, frame, action_frame_start) for frame in scene_frames]
    diagnostic = _trajectory_diagnostic(trajectory_curves, action_frames)
    intervals = []
    for prev_scene_frame, curr_scene_frame, prev_action_frame, curr_action_frame in zip(
        scene_frames[:-1],
        scene_frames[1:],
        action_frames[:-1],
        action_frames[1:],
    ):
        prev_matrix = _transform_matrix_from_curve_groups(trajectory_curves, prev_action_frame)
        curr_matrix = _transform_matrix_from_curve_groups(trajectory_curves, curr_action_frame)
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
            dz=0.0,
            dyaw=float(dyaw),
            weight=float(interval_weight),
        ))

    if not intervals:
        return None

    try:
        start_frame = float(getattr(strip, "frame_start", scene_frames[0]) or scene_frames[0])
        end_frame = float(getattr(strip, "frame_end", scene_frames[-1]) or scene_frames[-1])
    except Exception:
        start_frame = scene_frames[0]
        end_frame = scene_frames[-1]

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
        trajectory_bone=trajectory_name,
        intervals=intervals,
        diagnostic=diagnostic,
    )


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

    def build_actor_motion_actions(self, *, action_name_factory=None, playback_mode="carry", reset_frames_by_actor=None):
        actions = []
        try:
            import bpy
        except Exception:
            return actions

        reset_frames_by_actor = reset_frames_by_actor or {}
        default_playback_mode = _strip_text(playback_mode, "carry")
        for entry in self._events_by_actor.values():
            actor_obj = entry.get("object")
            actor_name = getattr(actor_obj, "name", "") or str(id(actor_obj))
            reset_frames = self._actor_reset_frames(actor_obj, actor_name, reset_frames_by_actor)
            sampled_keyframes = self._actor_keyframes(entry, reset_frames=reset_frames)
            if actor_obj is None or len(sampled_keyframes) < 2:
                continue
            actor_playback_mode = default_playback_mode
            if actor_playback_mode == "animated":
                keyframes = [(float(frame), loc.copy(), float(yaw)) for frame, loc, yaw in sampled_keyframes]
                interpolation = 'LINEAR'
            else:
                state_frames = {self.section_start_frame, self.section_end_frame}
                state_frames.update(reset_frames)
                for motion_event in entry.get("events", []) or []:
                    state_frames.add(float(motion_event.end_frame))
                keyframes = []
                for state_frame in sorted(frame for frame in state_frames if self.section_start_frame <= frame <= self.section_end_frame):
                    candidates = [item for item in sampled_keyframes if float(item[0]) <= float(state_frame) + 1e-4]
                    chosen = candidates[-1] if candidates else sampled_keyframes[0]
                    keyframes.append((float(state_frame), chosen[1].copy(), float(chosen[2])))
                interpolation = 'CONSTANT'
            reset_frame = reset_frames[0] if reset_frames else None
            reset_reason = "explicitScenePlacement" if reset_frames else ""
            if len(keyframes) < 2:
                continue
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
            for frame, loc, yaw in keyframes:
                # Trajectory curves have already gone through the animation import
                # rot90 axis conversion. Key the extracted object offset in those
                # same Blender scene axes; rotating by actor placement yaw turns a
                # forward scene +Y step into actor-local sideways motion.
                keyed_loc = loc
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
            total_yaw = 0.0
            if sampled_keyframes:
                total_xy = math.sqrt(sampled_keyframes[-1][1].x ** 2 + sampled_keyframes[-1][1].y ** 2)
                total_yaw = math.degrees(_wrap_radians(sampled_keyframes[-1][2] - sampled_keyframes[0][2]))
            try:
                action[W2SCENE_MOTION_HAS_EXTRACTION_PROP] = True
                action["w3_scene_motion_event_count"] = len(entry.get("events", []) or [])
                action["w3_scene_motion_key_count"] = len(keyframes)
                action["w3_scene_motion_sample_key_count"] = len(sampled_keyframes)
                action["w3_scene_motion_xy_delta"] = float(total_xy)
                action["w3_scene_motion_yaw_delta_deg"] = float(total_yaw)
                action["w3_scene_motion_rotation_path"] = rot_path
                action["w3_scene_motion_location_space"] = "blender_scene_axes"
                action["w3_scene_motion_placement_yaw_deg"] = math.degrees(placement_yaw)
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
                "yaw_delta_deg": total_yaw,
                "rotation_path": rot_path,
                "location_space": "blender_scene_axes",
                "placement_yaw_deg": math.degrees(placement_yaw),
                "playback_mode": actor_playback_mode,
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
    if use_motion_extraction or not use_fake_motion:
        motion_event = build_motion_event_from_strip(
            actor_obj,
            armature_obj,
            strip,
            event=event,
            section=section,
            track_name=track_name,
            section_start_frame=section_start_frame,
            section_end_frame=section_end_frame,
        )
    motion_event_added = False
    if use_motion_extraction and motion_event is not None and accumulator is not None:
        motion_event_added = accumulator.add_event(motion_event)

    neutralize_result = {"changed": 0, "bone": ""}
    if use_motion_extraction and not use_fake_motion and motion_event is not None:
        neutralize_result = counteract_pose_trajectory_with_root(
            strip,
            armature_obj,
            event=event,
            section=section,
            track_name=track_name,
        )
        if int(neutralize_result.get("changed", 0) or 0) > 0:
            pose_policy = "actor_motion_counteract_pose_trajectory"
        else:
            pose_policy = "actor_motion_no_pose_trajectory"
    if use_motion_extraction and use_fake_motion:
        pose_policy = "keep_fake_motion_record_actor_state"

    final_action = getattr(strip, "action", action) if strip is not None else action
    for id_block in (final_action, strip):
        if id_block is None:
            continue
        try:
            id_block[W2SCENE_MOTION_POSE_POLICY_PROP] = pose_policy
            id_block[W2SCENE_MOTION_HAS_EXTRACTION_PROP] = bool(motion_event_added)
            id_block[W2SCENE_MOTION_POSE_NEUTRALIZED_PROP] = bool(neutralize_result.get("changed", 0))
        except Exception:
            pass

    details = {
        "useMotionExtraction": bool(use_motion_extraction),
        "useFakeMotion": bool(use_fake_motion),
        "posePolicy": pose_policy,
        "actorMotion": bool(motion_event_added),
        "poseNeutralized": bool(neutralize_result.get("changed", 0)),
        "trajectoryBone": (
            motion_event.trajectory_bone
            if motion_event is not None
            else neutralize_result.get("trajectoryBone", neutralize_result.get("bone", ""))
        ),
    }
    if motion_event is not None:
        details.update({
            "eventWeight": round(float(motion_event.event_weight), 6),
            "motionWeight": round(float(motion_event.motion_weight), 6),
            "weightBaked": bool(motion_event.weight_baked),
            "intervals": len(motion_event.intervals),
            "trajectoryXYDelta": motion_event.diagnostic.get("xyDelta", 0.0),
            "trajectoryYawDeltaDeg": motion_event.diagnostic.get("yawDeltaDeg", 0.0),
            "trajectoryYawSpanDeg": motion_event.diagnostic.get("yawSpanDeg", 0.0),
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
        "poseNeutralized": bool(neutralize_result.get("changed", 0)),
        "motionEvent": motion_event,
    }
