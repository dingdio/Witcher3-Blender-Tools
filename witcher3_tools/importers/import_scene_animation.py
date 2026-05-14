import json
import logging
import math

from mathutils import Euler, Quaternion, Vector

from ..action_compat import (
    bind_strip_action_slot,
    iter_action_fcurves,
    new_action_fcurve,
    remove_action_fcurve,
    resolve_action_slot,
)
from .import_scene_fcurves import (
    bone_base_name as _w2scene_bone_base_name,
    collect_pose_transform_curves as _collect_pose_transform_curves,
    ensure_pose_transform_fcurves as _ensure_transform_fcurves,
    evaluate_fcurve_group as _evaluate_fcurve_group,
    fcurve_has_keys as _fcurve_has_keys,
    find_pose_bone_name as _find_w2scene_pose_bone_name,
    keyframe_frames as _keyframe_frames,
    parse_pose_bone_transform_fcurve_path as _parse_pose_bone_transform_fcurve_path,
    pose_bone_data_path as _pose_bone_data_path,
    quat_from_curve_values as _quat_from_curve_values,
    set_fcurve_value_at_frame as _set_fcurve_value_at_frame,
    set_transform_group_values as _set_transform_group_values,
)

log = logging.getLogger(__name__)

W2SCENE_ACTION_ROOT_ORIENTATION_PROP = "w3_scene_root_orientation_applied"
W2SCENE_ACTION_WARNING_PROP = "w3_scene_animation_warnings"
W2SCENE_ACTION_ROOT_DIAGNOSTIC_PROP = "w3_scene_root_trajectory_diagnostic"
W2SCENE_ACTION_SCENE_COPY_PROP = "_w3_scene_action_copy"
W2SCENE_ACTION_SCENE_COPY_SOURCE_PROP = "_w3_scene_action_copy_source"
W2SCENE_ACTION_WEIGHT_PROP = "w3_scene_additive_weight"
W2SCENE_ACTION_WEIGHT_APPLIED_PROP = "w3_scene_additive_weight_applied"
W2SCENE_ACTION_WEIGHT_SOURCE_PROP = "_w3_scene_additive_weight_source_action"
W2SCENE_EVENT_WEIGHT_PROP = "w3_scene_event_weight"
W2SCENE_EVENT_WEIGHT_APPLIED_PROP = "w3_scene_event_weight_applied_to_strip"
W2SCENE_EVENT_WEIGHT_CURVE_BAKED_PROP = "w3_scene_event_weight_curve_baked"
W2SCENE_EVENT_WEIGHT_CURVE_EDITS_PROP = "w3_scene_event_weight_curve_edits"


def _target_name(target):
    return str(getattr(target, "name", "") or "<unnamed>")


def _event_context_text(event=None, section="", track_name="", action=None, armature_obj=None):
    parts = []
    if section:
        parts.append(f"section={section}")
    if event is not None:
        event_class = event.__class__.__name__
        event_name = str(getattr(event, "eventName", "") or "").strip()
        anim_name = str(getattr(event, "animationName", "") or "").strip()
        actor_name = str(getattr(event, "actor", None) or getattr(event, "actorName", "") or "").strip()
        parts.append(f"event={event_class}")
        if event_name:
            parts.append(f"eventName={event_name}")
        if anim_name:
            parts.append(f"animation={anim_name}")
        if actor_name:
            parts.append(f"actor={actor_name}")
    if track_name:
        parts.append(f"track={track_name}")
    if action is not None:
        parts.append(f"action={_target_name(action)}")
    if armature_obj is not None:
        parts.append(f"armature={_target_name(armature_obj)}")
    return " ".join(parts)


def _append_warning_prop(target, message):
    if target is None:
        return
    try:
        current = str(target.get(W2SCENE_ACTION_WARNING_PROP, "") or "")
        entries = [entry for entry in current.split("\n") if entry]
        if message not in entries:
            entries.append(message)
        target[W2SCENE_ACTION_WARNING_PROP] = "\n".join(entries[-12:])
    except Exception:
        pass


def warn_scene_animation_edit(operation, *, action=None, strip=None, armature_obj=None, event=None, section="", track_name="", details=None):
    context = _event_context_text(
        event=event,
        section=section,
        track_name=track_name,
        action=action,
        armature_obj=armature_obj,
    )
    detail_text = ""
    if details:
        detail_text = " " + " ".join(f"{key}={value}" for key, value in details.items())
    message = f"W2SCENE WARNING: scene section importer changed animation: {operation}"
    if context:
        message = f"{message} {context}"
    message = f"{message}{detail_text}"
    log.warning(message)
    _append_warning_prop(action, message)
    _append_warning_prop(strip, message)


def warn_scene_animation_lookup(*, requested="", resolved="", path="", armature_obj=None, event=None, section="", track_name="", frame=None):
    context = _event_context_text(
        event=event,
        section=section,
        track_name=track_name,
        armature_obj=armature_obj,
    )
    parts = [
        "W2SCENE LOOKUP: scene section importer resolved animation",
        context,
        f"requested={requested}",
        f"resolved={resolved}",
        f"path={path}",
    ]
    if frame is not None:
        parts.append(f"frame={frame}")
    log.warning(" ".join(str(part) for part in parts if part))


def warn_scene_animation_skip(reason, *, event=None, section="", track_name="", details=None):
    context = _event_context_text(event=event, section=section, track_name=track_name)
    detail_text = ""
    if details:
        detail_text = " " + " ".join(f"{key}={value}" for key, value in details.items())
    message = f"W2SCENE SKIP: scene section importer skipped animation event: {reason}"
    if context:
        message = f"{message} {context}"
    log.warning(f"{message}{detail_text}")


def warn_scene_animation_debug(message, *, event=None, section="", track_name="", action=None, armature_obj=None, details=None):
    context = _event_context_text(
        event=event,
        section=section,
        track_name=track_name,
        action=action,
        armature_obj=armature_obj,
    )
    detail_text = ""
    if details:
        detail_text = " " + " ".join(f"{key}={value}" for key, value in details.items())
    text = f"W2SCENE DEBUG: {message}"
    if context:
        text = f"{text} {context}"
    log.warning(f"{text}{detail_text}")


def _set_float_idprop_ui(id_block, prop_name, value, soft_min=0.0, soft_max=1.0):
    if id_block is None:
        return
    try:
        id_block[prop_name] = float(value)
        ui = id_block.id_properties_ui(prop_name)
        ui.update(min=float(soft_min), max=float(soft_max), soft_min=float(soft_min), soft_max=float(soft_max))
    except Exception:
        pass


def _quat_world_up_dot(quat):
    if quat is None:
        return -1.0
    try:
        test_quat = quat.copy()
        test_quat.normalize()
        up = test_quat.to_matrix() @ Vector((0.0, 0.0, 1.0))
        if up.length <= 1e-8:
            return -1.0
        return float(up.normalized().dot(Vector((0.0, 0.0, 1.0))))
    except Exception:
        return -1.0


def _read_bone_first_frame_quat(action, armature_obj, bone_name, default=None):
    quat_path = _pose_bone_data_path(armature_obj, bone_name, "rotation_quaternion")
    euler_path = _pose_bone_data_path(armature_obj, bone_name, "rotation_euler")

    first_frame = None
    quat_curves = {}
    euler_curves = {}
    slot = resolve_action_slot(action, target=armature_obj, ensure=True)

    for fcurve in iter_action_fcurves(action, target=armature_obj, slot=slot):
        if fcurve.data_path == quat_path and _fcurve_has_keys(fcurve):
            frame = _keyframe_frames(fcurve)[0]
            if first_frame is None or frame < first_frame:
                first_frame = frame
            quat_curves[int(getattr(fcurve, "array_index", 0) or 0)] = fcurve
        elif fcurve.data_path == euler_path and _fcurve_has_keys(fcurve):
            frame = _keyframe_frames(fcurve)[0]
            if first_frame is None or frame < first_frame:
                first_frame = frame
            euler_curves[int(getattr(fcurve, "array_index", 0) or 0)] = fcurve

    if first_frame is None:
        return default

    if quat_curves:
        quat = Quaternion((
            quat_curves[0].evaluate(first_frame) if 0 in quat_curves else 1.0,
            quat_curves[1].evaluate(first_frame) if 1 in quat_curves else 0.0,
            quat_curves[2].evaluate(first_frame) if 2 in quat_curves else 0.0,
            quat_curves[3].evaluate(first_frame) if 3 in quat_curves else 0.0,
        ))
        if math.sqrt(sum(float(value) * float(value) for value in quat)) <= 1e-8:
            return Quaternion((1.0, 0.0, 0.0, 0.0))
        quat.normalize()
        return quat

    return Euler((
        euler_curves[0].evaluate(first_frame) if 0 in euler_curves else 0.0,
        euler_curves[1].evaluate(first_frame) if 1 in euler_curves else 0.0,
        euler_curves[2].evaluate(first_frame) if 2 in euler_curves else 0.0,
    ), "XYZ").to_quaternion()


def _read_root_first_frame_quat(action, armature_obj, root_bone_name):
    return _read_bone_first_frame_quat(
        action,
        armature_obj,
        root_bone_name,
        default=Quaternion((1.0, 0.0, 0.0, 0.0)),
    )


def _select_root_orientation_quat(action, armature_obj, root_bone_name):
    root_quat = _read_bone_first_frame_quat(action, armature_obj, root_bone_name, default=None)
    root_up_dot = _quat_world_up_dot(root_quat)
    if root_quat is not None and root_up_dot >= 0.5:
        return root_quat, root_bone_name, root_up_dot

    best_quat = root_quat or Quaternion((1.0, 0.0, 0.0, 0.0))
    best_bone = root_bone_name
    best_up_dot = root_up_dot
    for base_name in ("Trajectory", "Reference"):
        candidate_name = _find_w2scene_pose_bone_name(armature_obj, base_name)
        if not candidate_name:
            continue
        candidate = _read_bone_first_frame_quat(action, armature_obj, candidate_name, default=None)
        candidate_up_dot = _quat_world_up_dot(candidate)
        if candidate is not None and candidate_up_dot > best_up_dot:
            best_quat = candidate
            best_bone = candidate_name
            best_up_dot = candidate_up_dot
        if candidate is not None and candidate_up_dot >= 0.5:
            return candidate, candidate_name, candidate_up_dot

    return best_quat, best_bone, best_up_dot


def _rotation_curves(action, armature_obj, bone_name):
    if not bone_name:
        return {}, {}
    quat_path = _pose_bone_data_path(armature_obj, bone_name, "rotation_quaternion")
    euler_path = _pose_bone_data_path(armature_obj, bone_name, "rotation_euler")
    slot = resolve_action_slot(action, target=armature_obj, ensure=True)
    quat_curves = {}
    euler_curves = {}
    for fcurve in iter_action_fcurves(action, target=armature_obj, slot=slot):
        if not _fcurve_has_keys(fcurve):
            continue
        index = int(getattr(fcurve, "array_index", 0) or 0)
        if str(getattr(fcurve, "data_path", "") or "") == quat_path:
            quat_curves[index] = fcurve
        elif str(getattr(fcurve, "data_path", "") or "") == euler_path:
            euler_curves[index] = fcurve
    return quat_curves, euler_curves


def _wrap_degrees(value):
    return (float(value) + 180.0) % 360.0 - 180.0


def _quat_yaw_degrees(quat):
    try:
        return math.degrees(quat.to_euler("XYZ").z)
    except Exception:
        return 0.0


def _quat_from_curves(curves, frame):
    quat = Quaternion((
        curves[0].evaluate(frame) if 0 in curves else 1.0,
        curves[1].evaluate(frame) if 1 in curves else 0.0,
        curves[2].evaluate(frame) if 2 in curves else 0.0,
        curves[3].evaluate(frame) if 3 in curves else 0.0,
    ))
    if math.sqrt(sum(float(value) * float(value) for value in quat)) <= 1e-8:
        return Quaternion((1.0, 0.0, 0.0, 0.0))
    quat.normalize()
    return quat


def _euler_yaw_from_curves(curves, frame):
    return math.degrees(curves[2].evaluate(frame) if 2 in curves else 0.0)


def _rotation_diagnostic(action, armature_obj, bone_name):
    quat_curves, euler_curves = _rotation_curves(action, armature_obj, bone_name)
    curves = quat_curves or euler_curves
    frames = set()
    for fcurve in curves.values():
        frames.update(_keyframe_frames(fcurve))
    if not frames:
        return {
            "bone": bone_name or "",
            "curve_count": 0,
            "key_count": 0,
            "yaw_delta_deg": 0.0,
            "yaw_span_deg": 0.0,
        }
    sorted_frames = sorted(frames)
    yaws = []
    for frame in sorted_frames:
        if quat_curves:
            yaws.append(_quat_yaw_degrees(_quat_from_curves(quat_curves, frame)))
        else:
            yaws.append(_euler_yaw_from_curves(euler_curves, frame))
    first_yaw = yaws[0]
    relative_yaws = [_wrap_degrees(yaw - first_yaw) for yaw in yaws]
    return {
        "bone": bone_name or "",
        "curve_count": len(curves),
        "key_count": len(sorted_frames),
        "yaw_delta_deg": round(_wrap_degrees(yaws[-1] - first_yaw), 3),
        "yaw_span_deg": round(max(relative_yaws) - min(relative_yaws), 3),
    }


def root_trajectory_diagnostic(action, armature_obj):
    root_bone_name = _find_w2scene_pose_bone_name(armature_obj, "Root")
    trajectory_bone_name = _find_w2scene_pose_bone_name(armature_obj, "Trajectory")
    return {
        "root": _rotation_diagnostic(action, armature_obj, root_bone_name),
        "trajectory": _rotation_diagnostic(action, armature_obj, trajectory_bone_name),
    }


def _ensure_bone_transform_group(curves_by_bone, bone_name):
    return curves_by_bone.setdefault(bone_name, {
        "location": {},
        "rotation_quaternion": {},
        "rotation_euler": {},
        "scale": {},
    })


def _slerp_identity_to_quat(quat, weight):
    if quat.w < 0.0:
        quat = Quaternion((-quat.w, -quat.x, -quat.y, -quat.z))
    identity = Quaternion((1.0, 0.0, 0.0, 0.0))
    try:
        weighted = identity.slerp(quat, weight)
    except Exception:
        weighted = Quaternion((
            identity.w + (quat.w - identity.w) * weight,
            identity.x + (quat.x - identity.x) * weight,
            identity.y + (quat.y - identity.y) * weight,
            identity.z + (quat.z - identity.z) * weight,
        ))
    weighted.normalize()
    return weighted


def _action_by_name(name):
    try:
        import bpy
        return bpy.data.actions.get(str(name or ""))
    except Exception:
        return None


def _source_action_for_weight(action):
    if action is None:
        return None
    try:
        source_name = str(action.get(W2SCENE_ACTION_WEIGHT_SOURCE_PROP, "") or "")
    except Exception:
        source_name = ""
    source = _action_by_name(source_name)
    if source is not None:
        return source

    try:
        source = action.copy()
    except Exception:
        log.debug("Could not copy scene weight source action %s", _target_name(action), exc_info=True)
        return None
    try:
        source.name = f"{action.name}_sceneWeightSource"
        source.use_fake_user = True
        source[W2SCENE_ACTION_WEIGHT_SOURCE_PROP] = ""
        source["_w3_scene_weight_source_for"] = action.name
        action[W2SCENE_ACTION_WEIGHT_SOURCE_PROP] = source.name
    except Exception:
        pass
    return source


def _ensure_weight_edit_action(strip, armature_obj, *, event=None, section="", track_name=""):
    action = getattr(strip, "action", None)
    if action is None:
        return None
    try:
        if action.get(W2SCENE_ACTION_WEIGHT_SOURCE_PROP, ""):
            return action
    except Exception:
        pass

    try:
        copied_action = action.copy()
    except Exception:
        log.debug("Could not copy scene action %s for curve weighting", _target_name(action), exc_info=True)
        return action
    try:
        copied_action.name = f"{action.name}_scene_weight"
        copied_action[W2SCENE_ACTION_SCENE_COPY_PROP] = True
        copied_action[W2SCENE_ACTION_SCENE_COPY_SOURCE_PROP] = action.name
        copied_action[W2SCENE_ACTION_WEIGHT_SOURCE_PROP] = action.name
        strip.action = copied_action
        bind_strip_action_slot(strip, resolve_action_slot(copied_action, target=armature_obj, ensure=True))
    except Exception:
        log.debug("Could not assign scene curve-weighted action copy to strip %s", _target_name(strip), exc_info=True)

    warn_scene_animation_edit(
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
            "reason": "scene_weight",
        },
    )
    return copied_action


def apply_scene_weight_to_action(action, armature_obj, weight, *, strip=None, event=None, section="", track_name=""):
    if action is None or armature_obj is None or getattr(armature_obj, "type", None) != "ARMATURE":
        return {"changed": 0, "bones": 0, "action": action}
    try:
        weight = max(0.0, min(1.0, float(weight)))
    except Exception:
        return {"changed": 0, "bones": 0, "action": action}

    _set_float_idprop_ui(action, W2SCENE_EVENT_WEIGHT_PROP, weight)
    _set_float_idprop_ui(action, W2SCENE_ACTION_WEIGHT_PROP, weight)

    source_action = _source_action_for_weight(action)
    if source_action is None:
        return {"changed": 0, "bones": 0, "action": action}

    slot = resolve_action_slot(action, target=armature_obj, ensure=True)
    source_slot = resolve_action_slot(source_action, target=armature_obj, ensure=True)
    source_curves_by_bone = _collect_pose_transform_curves(source_action, armature_obj, source_slot)
    dest_curves_by_bone = _collect_pose_transform_curves(action, armature_obj, slot)
    changed = 0
    changed_bones = set()
    weighted_rotation_frames = 0

    for bone_name, source_curves_by_prop in source_curves_by_bone.items():
        has_location = bool(source_curves_by_prop.get("location"))
        has_quat_rotation = bool(source_curves_by_prop.get("rotation_quaternion"))
        has_euler_rotation = bool(source_curves_by_prop.get("rotation_euler"))
        has_scale = bool(source_curves_by_prop.get("scale"))
        if not (has_location or has_quat_rotation or has_euler_rotation or has_scale):
            continue

        dest_curves_by_prop = _ensure_bone_transform_group(dest_curves_by_bone, bone_name)
        if has_location:
            _ensure_transform_fcurves(action, armature_obj, slot, bone_name, "location", dest_curves_by_prop["location"], create_if_empty=True)
        if has_quat_rotation:
            _ensure_transform_fcurves(action, armature_obj, slot, bone_name, "rotation_quaternion", dest_curves_by_prop["rotation_quaternion"], create_if_empty=True)
        if has_euler_rotation:
            _ensure_transform_fcurves(action, armature_obj, slot, bone_name, "rotation_euler", dest_curves_by_prop["rotation_euler"], create_if_empty=True)
        if has_scale:
            _ensure_transform_fcurves(action, armature_obj, slot, bone_name, "scale", dest_curves_by_prop["scale"], create_if_empty=True)

        frames = set()
        for curves in source_curves_by_prop.values():
            for fcurve in curves.values():
                frames.update(_keyframe_frames(fcurve))
        if not frames:
            continue

        for frame in sorted(frames):
            loc = None
            quat_rot = None
            euler_rot = None
            scale = None
            if has_location:
                loc = Vector(_evaluate_fcurve_group(source_curves_by_prop.get("location", {}), (0.0, 0.0, 0.0), frame)) * weight
            if has_quat_rotation:
                quat_rot = _slerp_identity_to_quat(
                    _quat_from_curve_values(
                        _evaluate_fcurve_group(source_curves_by_prop.get("rotation_quaternion", {}), (1.0, 0.0, 0.0, 0.0), frame)
                    ),
                    weight,
                )
                weighted_rotation_frames += 1
            if has_euler_rotation:
                euler_rot = tuple(
                    float(value) * weight
                    for value in _evaluate_fcurve_group(source_curves_by_prop.get("rotation_euler", {}), (0.0, 0.0, 0.0), frame)
                )
                weighted_rotation_frames += 1
            if has_scale:
                source_scale = Vector(_evaluate_fcurve_group(source_curves_by_prop.get("scale", {}), (1.0, 1.0, 1.0), frame))
                scale = Vector((
                    1.0 + (source_scale.x - 1.0) * weight,
                    1.0 + (source_scale.y - 1.0) * weight,
                    1.0 + (source_scale.z - 1.0) * weight,
                ))
            _set_transform_group_values(dest_curves_by_prop, frame, loc, quat_rot=quat_rot, euler_rot=euler_rot, scale=scale)
            changed += 1
            changed_bones.add(bone_name)

    for fcurve in iter_action_fcurves(action, target=armature_obj, slot=slot):
        try:
            fcurve.update()
        except Exception:
            pass
    try:
        action[W2SCENE_ACTION_WEIGHT_PROP] = weight
        action[W2SCENE_ACTION_WEIGHT_APPLIED_PROP] = weight
        action[W2SCENE_EVENT_WEIGHT_PROP] = weight
        action[W2SCENE_EVENT_WEIGHT_CURVE_BAKED_PROP] = changed > 0
        action[W2SCENE_EVENT_WEIGHT_CURVE_EDITS_PROP] = changed
        action.update_tag()
    except Exception:
        pass

    if strip is not None:
        try:
            strip.influence = 1.0 if changed else weight
            strip[W2SCENE_EVENT_WEIGHT_PROP] = weight
            strip[W2SCENE_EVENT_WEIGHT_APPLIED_PROP] = True
            strip[W2SCENE_EVENT_WEIGHT_CURVE_BAKED_PROP] = changed > 0
            strip[W2SCENE_EVENT_WEIGHT_CURVE_EDITS_PROP] = changed
        except Exception:
            pass

    if changed:
        warn_scene_animation_edit(
            "baked scene animation weight into action curves",
            action=action,
            strip=strip,
            armature_obj=armature_obj,
            event=event,
            section=section,
            track_name=track_name,
            details={
                "weight": round(float(weight), 6),
                "frameEdits": changed,
                "bones": len(changed_bones),
                "rotationFrames": weighted_rotation_frames,
                "sourceAction": _target_name(source_action),
                "stripInfluence": round(float(getattr(strip, "influence", 1.0) or 0.0), 6) if strip is not None else "",
            },
        )
    else:
        warn_scene_animation_edit(
            "scene animation weight had no editable pose transform curves",
            action=action,
            strip=strip,
            armature_obj=armature_obj,
            event=event,
            section=section,
            track_name=track_name,
            details={
                "weight": round(float(weight), 6),
                "sourceAction": _target_name(source_action),
            },
        )
    return {"changed": changed, "bones": len(changed_bones), "action": action, "source": source_action}


def apply_scene_weight_to_strip(strip, armature_obj, weight, *, event=None, section="", track_name=""):
    if strip is None:
        return {"changed": 0, "bones": 0, "action": None}
    try:
        weight = max(0.0, min(1.0, float(weight)))
    except Exception:
        weight = 1.0
    action = getattr(strip, "action", None)
    has_weight_source = False
    try:
        has_weight_source = bool(action is not None and action.get(W2SCENE_ACTION_WEIGHT_SOURCE_PROP, ""))
    except Exception:
        has_weight_source = False
    if action is not None and not has_weight_source and abs(weight - 1.0) <= 0.000001:
        _set_float_idprop_ui(action, W2SCENE_EVENT_WEIGHT_PROP, weight)
        _set_float_idprop_ui(action, W2SCENE_ACTION_WEIGHT_PROP, weight)
        try:
            strip.influence = 1.0
            strip[W2SCENE_EVENT_WEIGHT_PROP] = weight
            strip[W2SCENE_EVENT_WEIGHT_APPLIED_PROP] = True
            strip[W2SCENE_EVENT_WEIGHT_CURVE_BAKED_PROP] = False
            strip[W2SCENE_EVENT_WEIGHT_CURVE_EDITS_PROP] = 0
            action[W2SCENE_EVENT_WEIGHT_APPLIED_PROP] = True
            action[W2SCENE_EVENT_WEIGHT_CURVE_BAKED_PROP] = False
            action[W2SCENE_EVENT_WEIGHT_CURVE_EDITS_PROP] = 0
        except Exception:
            pass
        return {"changed": 0, "bones": 0, "action": action}
    action = _ensure_weight_edit_action(strip, armature_obj, event=event, section=section, track_name=track_name)
    return apply_scene_weight_to_action(
        action,
        armature_obj,
        weight,
        strip=strip,
        event=event,
        section=section,
        track_name=track_name,
    )


def apply_scene_root_orientation_to_action(action, armature_obj, *, event=None, section="", track_name="", strip=None):
    if action is None or armature_obj is None or getattr(armature_obj, "type", None) != "ARMATURE":
        return False

    root_bone_name = _find_w2scene_pose_bone_name(armature_obj, "Root")
    if not root_bone_name:
        return False

    try:
        if action.get("root_orientation_applied", False):
            action[W2SCENE_ACTION_ROOT_ORIENTATION_PROP] = True
            return True
    except Exception:
        pass

    slot = resolve_action_slot(action, target=armature_obj, ensure=True)
    diagnostic = root_trajectory_diagnostic(action, armature_obj)
    try:
        action[W2SCENE_ACTION_ROOT_DIAGNOSTIC_PROP] = json.dumps(diagnostic, sort_keys=True)
    except Exception:
        pass

    root_diag = diagnostic.get("root") or {}
    traj_diag = diagnostic.get("trajectory") or {}
    root_yaw = abs(float(root_diag.get("yaw_span_deg", 0.0) or 0.0))
    traj_yaw = abs(float(traj_diag.get("yaw_span_deg", 0.0) or 0.0))
    if root_yaw > 1.0 and traj_yaw > 1.0:
        warn_scene_animation_edit(
            "detected animated Root and Trajectory yaw before cleanup",
            action=action,
            strip=strip,
            armature_obj=armature_obj,
            event=event,
            section=section,
            track_name=track_name,
            details={
                "rootYawSpanDeg": round(root_yaw, 3),
                "trajectoryYawSpanDeg": round(traj_yaw, 3),
            },
        )

    initial_quat, source_bone_name, source_up_dot = _select_root_orientation_quat(action, armature_obj, root_bone_name)
    root_data_paths = {
        _pose_bone_data_path(armature_obj, root_bone_name, "location"),
        _pose_bone_data_path(armature_obj, root_bone_name, "rotation_quaternion"),
        _pose_bone_data_path(armature_obj, root_bone_name, "rotation_euler"),
        _pose_bone_data_path(armature_obj, root_bone_name, "scale"),
    }
    root_key = _w2scene_bone_base_name(root_bone_name)
    fcurves_to_remove = []
    for fcurve in iter_action_fcurves(action, target=armature_obj, slot=slot):
        data_path = str(getattr(fcurve, "data_path", "") or "")
        remove_curve = data_path in root_data_paths
        if not remove_curve:
            parsed = _parse_pose_bone_transform_fcurve_path(fcurve)
            remove_curve = bool(
                parsed
                and _w2scene_bone_base_name(parsed[0]) == root_key
                and parsed[1] in {"location", "rotation_quaternion", "rotation_euler", "scale"}
            )
        if remove_curve:
            fcurves_to_remove.append(fcurve)

    for fcurve in fcurves_to_remove:
        remove_action_fcurve(action, fcurve, target=armature_obj, slot=slot)

    try:
        pose_bone = armature_obj.pose.bones.get(root_bone_name)
        if pose_bone is not None:
            pose_bone.rotation_mode = "QUATERNION"
    except Exception:
        pass

    quat_path = _pose_bone_data_path(armature_obj, root_bone_name, "rotation_quaternion")
    loc_path = _pose_bone_data_path(armature_obj, root_bone_name, "location")
    for index, value in enumerate((initial_quat.w, initial_quat.x, initial_quat.y, initial_quat.z)):
        fcurve = new_action_fcurve(action, armature_obj, data_path=quat_path, index=index, group_name=root_bone_name, slot=slot)
        _set_fcurve_value_at_frame(fcurve, 1.0, float(value))
    for index in range(3):
        fcurve = new_action_fcurve(action, armature_obj, data_path=loc_path, index=index, group_name=root_bone_name, slot=slot)
        _set_fcurve_value_at_frame(fcurve, 1.0, 0.0)

    for fcurve in iter_action_fcurves(action, target=armature_obj, slot=slot):
        try:
            fcurve.update()
        except Exception:
            pass
    try:
        action["root_orientation_applied"] = True
        action[W2SCENE_ACTION_ROOT_ORIENTATION_PROP] = True
        action["w3_scene_root_orientation_removed_fcurves"] = len(fcurves_to_remove)
        action["w3_scene_root_orientation_source_bone"] = source_bone_name
        action["w3_scene_root_orientation_source_up_dot"] = float(source_up_dot)
    except Exception:
        pass

    warn_scene_animation_edit(
        "removed animated Root transform and keyed static Root orientation",
        action=action,
        strip=strip,
        armature_obj=armature_obj,
        event=event,
        section=section,
        track_name=track_name,
        details={
            "removedFcurves": len(fcurves_to_remove),
            "rootBone": root_bone_name,
            "sourceBone": source_bone_name,
            "sourceUpDot": round(float(source_up_dot), 4),
        },
    )
    return True


def _w2scene_quat_key_frames(quat_curves):
    frames = set()
    for fcurve in (quat_curves or {}).values():
        if _fcurve_has_keys(fcurve):
            frames.update(_keyframe_frames(fcurve))
    return sorted(frames)


def _w2scene_set_keyframe_value(fcurve, frame, value, *, epsilon=1e-10):
    if fcurve is None:
        return False
    frame = float(frame)
    value = float(value)
    try:
        keyframes = getattr(fcurve, "keyframe_points", []) or []
    except Exception:
        keyframes = []

    for keyframe in keyframes:
        try:
            if abs(float(keyframe.co.x) - frame) > 1e-4:
                continue
            old_value = float(keyframe.co.y)
            if abs(old_value - value) <= epsilon:
                return False
            delta = value - old_value
            keyframe.co.y = value
            try:
                keyframe.handle_left.y = float(keyframe.handle_left.y) + delta
                keyframe.handle_right.y = float(keyframe.handle_right.y) + delta
            except Exception:
                pass
            return True
        except Exception:
            continue

    _set_fcurve_value_at_frame(fcurve, frame, value)
    return True


def _w2scene_set_quat_key(quat_curves, frame, quat):
    changed = False
    for index, value in enumerate((quat.w, quat.x, quat.y, quat.z)):
        fcurve = (quat_curves or {}).get(index)
        if fcurve is not None and _w2scene_set_keyframe_value(fcurve, frame, value):
            changed = True
    return changed


def make_action_quaternion_keys_continuous(
    action,
    armature_obj,
    *,
    strip=None,
    event=None,
    section="",
    track_name="",
):
    result = {"changed_bones": 0, "flipped_bones": 0, "flipped_keys": 0, "normalized_keys": 0, "bones": []}
    if action is None or armature_obj is None or getattr(armature_obj, "type", None) != "ARMATURE":
        return result
    try:
        slot = resolve_action_slot(action, target=armature_obj, ensure=True)
    except Exception:
        return result

    curves_by_bone = _collect_pose_transform_curves(action, armature_obj, slot, include_euler=False)
    changed_bones = []
    flipped_bone_names = []
    flipped_keys = 0
    normalized_keys = 0
    for bone_name, curves_by_prop in curves_by_bone.items():
        quat_curves = curves_by_prop.get("rotation_quaternion") or {}
        frames = _w2scene_quat_key_frames(quat_curves)
        if not frames:
            continue

        prev_quat = None
        bone_changed = False
        bone_flipped = False
        for frame in frames:
            try:
                raw_values = _evaluate_fcurve_group(quat_curves, (1.0, 0.0, 0.0, 0.0), frame)
                raw_norm_sq = sum(float(value) * float(value) for value in raw_values)
                quat = _quat_from_curve_values(raw_values)
            except Exception:
                continue

            if raw_norm_sq <= 1e-12 or abs(raw_norm_sq - 1.0) > 1e-6:
                normalized_keys += 1
            if prev_quat is not None and prev_quat.dot(quat) < 0.0:
                quat = -quat
                flipped_keys += 1
                bone_flipped = True
            if _w2scene_set_quat_key(quat_curves, frame, quat):
                bone_changed = True
            prev_quat = quat.copy()

        if bone_changed:
            changed_bones.append(bone_name)
        if bone_flipped:
            flipped_bone_names.append(bone_name)

    if changed_bones:
        for fcurve in iter_action_fcurves(action, target=armature_obj, slot=slot):
            try:
                fcurve.update()
            except Exception:
                pass
        warn_scene_animation_edit(
            "made quaternion keys continuous per bone track",
            action=action,
            strip=strip,
            armature_obj=armature_obj,
            event=event,
            section=section,
            track_name=track_name,
            details={
                "changedBones": len(changed_bones),
                "flippedBones": len(flipped_bone_names),
                "flippedKeys": flipped_keys,
                "normalizedKeys": normalized_keys,
                "bones": ",".join(changed_bones[:32]),
            },
        )

    result["changed_bones"] = len(changed_bones)
    result["flipped_bones"] = len(flipped_bone_names)
    result["flipped_keys"] = flipped_keys
    result["normalized_keys"] = normalized_keys
    result["bones"] = changed_bones
    return result


def make_strip_quaternion_keys_continuous(strip, armature_obj, *, event=None, section="", track_name=""):
    return make_action_quaternion_keys_continuous(
        getattr(strip, "action", None),
        armature_obj,
        strip=strip,
        event=event,
        section=section,
        track_name=track_name,
    )
