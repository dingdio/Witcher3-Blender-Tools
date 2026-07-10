"""Story-scene pose-key creation, editing, preview, and serialization."""

import json
import logging
import math
import os
from xml.sax.saxutils import escape

import bpy
from mathutils import Euler, Matrix, Quaternion, Vector

from .action_compat import (
    bind_strip_action_slot,
    iter_action_fcurves,
    new_action_fcurve,
    remove_action_fcurve,
    resolve_action_slot,
)

log = logging.getLogger(__name__)


POSEKEY_TRACK_NAME = "ScenePoseKey"
POSEKEY_CLASS_NAME = "CStorySceneEventPoseKey"
POSEKEY_MARKER_PROP = "w3_scene_pose_key"
POSEKEY_METADATA_PROP = "w3_scene_pose_key_metadata"
REDKIT_POSE_PRESETS_RELATIVE_PATH = os.path.join("workspace", "gameplay", "resources", "scene.redPresets")

GAME_IK_BONE_MAP = {
    "IK_r_foot": "r_foot",
    "IK_l_foot": "l_foot",
    "IK_pelvis": "pelvis",
    "IK_r_hand": "r_hand",
    "IK_l_hand": "l_hand",
    "IK_torso3": "torso3",
}

HAND_BONE_TOKENS = ("thumb", "index", "middle", "ring", "pinky")
POSE_TRANSFORM_PROPS = {"location", "rotation_quaternion", "rotation_euler", "scale"}
HAND_TRACK_PARTS = ("all", "A", "B", "C", "D", "E", "twist", "handX", "handY", "handZ")
HAND_TRACK_SIDE_INDEX = {"L": 0, "R": 1}
HAND_TRACK_COUNT = len(HAND_TRACK_PARTS) * len(HAND_TRACK_SIDE_INDEX)
HAND_BASE_ROT_ANGLE = 120.0
POSEKEY_GROUP_ORDER = {"FK": 0, "HAND": 1, "IK": 2}


class _PropertyRecord:
    def __init__(self, props):
        self.More = list(props or [])
        self.MoreProps = self.More
        self.PROPS = self.More


def is_hand_bone_name(name):
    lower = str(name or "").lower()
    return any(token in lower for token in HAND_BONE_TOKENS)


def is_game_ik_marker(name):
    return str(name or "") in GAME_IK_BONE_MAP


def fps_from_context(context):
    scene = getattr(context, "scene", None)
    render = getattr(scene, "render", None)
    if render is None:
        return 30.0
    fps_base = float(getattr(render, "fps_base", 1.0) or 1.0)
    fps = float(getattr(render, "fps", 30.0) or 30.0)
    return fps / fps_base if fps_base else fps


def seconds_to_frames(context, seconds, minimum=1.0):
    try:
        frames = float(seconds) * fps_from_context(context)
    except Exception:
        frames = minimum
    return max(float(minimum), frames)


def _pose_key_duration_frames(context, metadata):
    return float(metadata.get("durationFrames") or seconds_to_frames(context, metadata.get("duration", 1.0)))


def _pose_key_preview_duration_frames(context, metadata):
    preview_frames = _float_or_default(metadata.get("previewDurationFrames", None), 0.0)
    if bool(metadata.get("linkToDialogset", False)) and preview_frames > 0.0:
        return max(1.0, preview_frames)
    return _pose_key_duration_frames(context, metadata)


def _float_or_default(value, default=0.0):
    if value is None:
        return float(default)
    try:
        return float(value)
    except Exception:
        return float(default)


def _bool_or_default(value, default=False):
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


def actor_name_for_armature(armature_obj, fallback=""):
    for prop_name in ("cutscene_actor_name", "w3_scene_actor", "witcher_w2scene_actor"):
        try:
            value = str(armature_obj.get(prop_name, "") or "").strip()
        except Exception:
            value = ""
        if value:
            return value
    return str(fallback or getattr(armature_obj, "name", "") or "").strip()


def resolve_armature_object(obj):
    if obj is None:
        return None
    if getattr(obj, "type", None) == 'ARMATURE':
        return obj
    parent = getattr(obj, "parent", None)
    if getattr(parent, "type", None) == 'ARMATURE':
        return parent
    for child in getattr(obj, "children", []) or []:
        if getattr(child, "type", None) == 'ARMATURE':
            return child
    return None


def ordered_pose_bone_names(armature_obj):
    if armature_obj is None or getattr(armature_obj, "pose", None) is None:
        return []

    names = []
    rig_settings = getattr(getattr(armature_obj, "data", None), "witcherui_RigSettings", None)
    bone_order = getattr(rig_settings, "bone_order_list", None) if rig_settings is not None else None
    if bone_order:
        for item in bone_order:
            name = str(getattr(item, "name", "") or "")
            if name and name in armature_obj.pose.bones and name not in names:
                names.append(name)

    for bone in armature_obj.pose.bones:
        if bone.name not in names:
            names.append(bone.name)
    return names


def bone_index_map(armature_obj):
    return {name: idx for idx, name in enumerate(ordered_pose_bone_names(armature_obj))}


def bone_name_by_index(armature_obj):
    return {idx: name for idx, name in enumerate(ordered_pose_bone_names(armature_obj))}


def _bone_data_path(bone_name, prop_name):
    escaped = str(bone_name).replace("\\", "\\\\").replace('"', '\\"')
    return f'pose.bones["{escaped}"].{prop_name}'


def _parse_pose_bone_data_path(data_path):
    data_path = str(data_path or "")
    prefix = 'pose.bones["'
    if not data_path.startswith(prefix):
        return None
    end = data_path.find('"]', len(prefix))
    if end < 0:
        return None
    bone_name = data_path[len(prefix):end]
    prop_name = data_path[end + 2:]
    if prop_name.startswith("."):
        prop_name = prop_name[1:]
    if prop_name not in POSE_TRANSFORM_PROPS:
        return None
    return bone_name, prop_name


def _quat_magnitude(quat):
    magnitude = getattr(quat, "magnitude", None)
    if magnitude is not None:
        try:
            return float(magnitude)
        except (TypeError, ValueError):
            pass
    try:
        return math.sqrt(sum(float(component) * float(component) for component in quat))
    except Exception:
        return math.sqrt(
            float(getattr(quat, "w", 1.0) or 0.0) ** 2
            + float(getattr(quat, "x", 0.0) or 0.0) ** 2
            + float(getattr(quat, "y", 0.0) or 0.0) ** 2
            + float(getattr(quat, "z", 0.0) or 0.0) ** 2
        )


def _normalized_quaternion(value):
    if all(hasattr(value, attr) for attr in ("w", "x", "y", "z")):
        quat = Quaternion((value.w, value.x, value.y, value.z))
    else:
        quat = Quaternion(value)
    if _quat_magnitude(quat) <= 1e-8:
        return Quaternion((1.0, 0.0, 0.0, 0.0))
    try:
        quat.normalize()
    except Exception:
        pass
    return quat


def _normalize_degrees_signed(value):
    try:
        value = float(value)
    except Exception:
        return 0.0
    value = (value + 180.0) % 360.0 - 180.0
    if value <= -180.0:
        value += 360.0
    return value


def _engine_euler_to_quaternion(roll, pitch, yaw):
    """Match REDengine EulerAngles::ToMatrix: Roll=Y, Pitch=X, Yaw=Z, degrees."""
    roll_quat = Quaternion((0.0, 1.0, 0.0), math.radians(float(roll)))
    pitch_quat = Quaternion((1.0, 0.0, 0.0), math.radians(float(pitch)))
    yaw_quat = Quaternion((0.0, 0.0, 1.0), math.radians(float(yaw)))
    return _normalized_quaternion(yaw_quat @ pitch_quat @ roll_quat)


def _engine_rotation_to_ui_xyz(roll, pitch, yaw):
    return (
        _normalize_degrees_signed(yaw),
        _normalize_degrees_signed(pitch),
        _normalize_degrees_signed(roll),
    )


def _ui_xyz_to_engine_rotation(x_value, y_value, z_value):
    return (
        _normalize_degrees_signed(y_value),
        _normalize_degrees_signed(x_value),
        _normalize_degrees_signed(z_value),
    )


def _engine_ui_xyz_to_quaternion(x_value, y_value, z_value):
    roll, pitch, yaw = _ui_xyz_to_engine_rotation(x_value, y_value, z_value)
    return _engine_euler_to_quaternion(roll, pitch, yaw)


def _armature_uses_rot90(armature_obj):
    try:
        from .. import get_rig_rot90_enabled

        rig_settings = getattr(getattr(armature_obj, "data", None), "witcherui_RigSettings", None)
        return bool(get_rig_rot90_enabled(rig_settings, default=False))
    except Exception:
        return False


def _game_transform_to_action_transform(armature_obj, loc, quat, scale):
    loc = Vector(tuple(float(v) for v in (loc or (0.0, 0.0, 0.0))))
    quat = _normalized_quaternion(quat)
    scale = tuple(float(v) for v in (scale or (1.0, 1.0, 1.0)))
    if not _armature_uses_rot90(armature_obj):
        return tuple(loc), (quat.w, quat.x, quat.y, quat.z), scale

    z_pos90 = Quaternion((0.0, 0.0, 1.0), math.radians(90.0))
    z_neg90 = Quaternion((0.0, 0.0, 1.0), math.radians(-90.0))
    loc = Matrix.Rotation(math.radians(90.0), 4, 'Z') @ loc
    quat = z_pos90 @ quat @ z_neg90
    quat.normalize()
    return tuple(float(v) for v in loc), (quat.w, quat.x, quat.y, quat.z), scale


def _transform_values_for_action(armature_obj, entry):
    loc = entry.get("location", (0.0, 0.0, 0.0)) if isinstance(entry, dict) else (0.0, 0.0, 0.0)
    quat = entry.get("rotation", (1.0, 0.0, 0.0, 0.0)) if isinstance(entry, dict) else (1.0, 0.0, 0.0, 0.0)
    scale = entry.get("scale", (1.0, 1.0, 1.0)) if isinstance(entry, dict) else (1.0, 1.0, 1.0)
    if isinstance(entry, dict) and str(entry.get("space", "") or "").lower() == "game":
        source_rotation = entry.get("sourceRotation")
        if source_rotation is not None:
            source_rotation = _float_tuple(source_rotation, 3, (0.0, 0.0, 0.0))
            quat = _engine_ui_xyz_to_quaternion(source_rotation[0], source_rotation[1], source_rotation[2])
        return _game_transform_to_action_transform(armature_obj, loc, quat, scale)
    return tuple(loc), tuple(quat), tuple(scale)


def _insert_constant_curve(action, armature_obj, data_path, index, value, end_frame, group_name):
    fcurve = _find_action_fcurve(action, armature_obj, data_path, index)
    if fcurve is None:
        fcurve = new_action_fcurve(action, armature_obj, data_path, index=index, group_name=group_name)
    return _set_constant_curve_value(fcurve, value, end_frame)


def _set_constant_curve_value(fcurve, value, end_frame):
    points = getattr(fcurve, "keyframe_points", None)
    if points is None:
        return fcurve
    if len(points) == 0:
        for frame in (0.0, float(end_frame)):
            key = points.insert(frame, float(value), options={'FAST'})
            key.interpolation = 'CONSTANT'
    else:
        if len(points) == 1:
            key = points.insert(float(end_frame), float(value), options={'FAST'})
            key.interpolation = 'CONSTANT'
        for key in points:
            key.co[1] = float(value)
            try:
                key.interpolation = 'CONSTANT'
            except Exception:
                pass
    try:
        fcurve.update()
    except Exception:
        pass
    return fcurve


def _find_action_fcurve(action, armature_obj, data_path, index):
    try:
        wanted_index = int(index)
    except Exception:
        wanted_index = 0
    for fcurve in iter_action_fcurves(action, target=armature_obj):
        if str(getattr(fcurve, "data_path", "") or "") != data_path:
            continue
        try:
            curve_index = int(getattr(fcurve, "array_index", 0) or 0)
        except Exception:
            curve_index = 0
        if curve_index == wanted_index:
            return fcurve
    return None


def _first_curve_value(fcurve, default=0.0):
    points = getattr(fcurve, "keyframe_points", None)
    if points:
        try:
            return float(points[0].co[1])
        except Exception:
            pass
    evaluate = getattr(fcurve, "evaluate", None)
    if callable(evaluate):
        try:
            return float(evaluate(0.0))
        except Exception:
            pass
    return float(default)


def _write_bone_transform_curves(action, armature_obj, bone_name, loc, quat, scale, end_frame):
    quat = _normalized_quaternion(quat)

    loc_path = _bone_data_path(bone_name, "location")
    scale_path = _bone_data_path(bone_name, "scale")
    group_name = str(bone_name)

    for idx, value in enumerate(loc):
        _insert_constant_curve(action, armature_obj, loc_path, idx, value, end_frame, group_name)

    pose_bone = armature_obj.pose.bones.get(bone_name) if getattr(armature_obj, "pose", None) else None
    rotation_mode = str(getattr(pose_bone, "rotation_mode", "QUATERNION") or "QUATERNION")
    euler_modes = {'XYZ', 'XZY', 'YXZ', 'YZX', 'ZXY', 'ZYX'}
    if rotation_mode in euler_modes:
        rot_path = _bone_data_path(bone_name, "rotation_euler")
        euler = quat.to_euler(rotation_mode)
        for idx, value in enumerate(euler):
            _insert_constant_curve(action, armature_obj, rot_path, idx, value, end_frame, group_name)
    else:
        if pose_bone is not None and rotation_mode == 'AXIS_ANGLE':
            try:
                pose_bone.rotation_mode = 'QUATERNION'
            except Exception:
                pass
        rot_path = _bone_data_path(bone_name, "rotation_quaternion")
        for idx, value in enumerate((quat.w, quat.x, quat.y, quat.z)):
            _insert_constant_curve(action, armature_obj, rot_path, idx, value, end_frame, group_name)

    for idx, value in enumerate(scale):
        _insert_constant_curve(action, armature_obj, scale_path, idx, value, end_frame, group_name)


def _pose_bone_transform(pose_bone):
    loc = tuple(float(v) for v in pose_bone.location)
    if getattr(pose_bone, "rotation_mode", "") == 'QUATERNION':
        quat = pose_bone.rotation_quaternion.copy()
    elif getattr(pose_bone, "rotation_mode", "") == 'AXIS_ANGLE':
        axis_angle = pose_bone.rotation_axis_angle
        axis = Vector((axis_angle[1], axis_angle[2], axis_angle[3]))
        quat = Quaternion(axis, axis_angle[0]) if axis.length > 1e-8 else Quaternion((1.0, 0.0, 0.0, 0.0))
    else:
        quat = pose_bone.rotation_euler.to_quaternion()
    scale = tuple(float(v) for v in pose_bone.scale)
    return loc, (quat.w, quat.x, quat.y, quat.z), scale


def _pose_bone_quaternion(pose_bone):
    _loc, quat, _scale = _pose_bone_transform(pose_bone)
    return _normalized_quaternion(quat)


def _pose_bone_rotation_mode(armature_obj, bone_name):
    pose_bone = armature_obj.pose.bones.get(bone_name) if getattr(armature_obj, "pose", None) else None
    return str(getattr(pose_bone, "rotation_mode", "QUATERNION") or "QUATERNION")


def _remove_pose_transform_fcurves(action, armature_obj):
    for fcurve in list(iter_action_fcurves(action, target=armature_obj)):
        if _parse_pose_bone_data_path(getattr(fcurve, "data_path", "")) is None:
            continue
        try:
            remove_action_fcurve(action, fcurve, target=armature_obj)
        except Exception:
            log.debug("Could not remove PoseKey fcurve %s", getattr(fcurve, "data_path", ""), exc_info=True)


def _remove_bone_transform_fcurves(action, armature_obj, bone_name, props=None):
    props = set(props or POSE_TRANSFORM_PROPS)
    for fcurve in list(iter_action_fcurves(action, target=armature_obj)):
        parsed = _parse_pose_bone_data_path(getattr(fcurve, "data_path", ""))
        if parsed is None:
            continue
        curve_bone, prop_name = parsed
        if curve_bone != bone_name or prop_name not in props:
            continue
        try:
            remove_action_fcurve(action, fcurve, target=armature_obj)
        except Exception:
            log.debug("Could not remove PoseKey fcurve %s", getattr(fcurve, "data_path", ""), exc_info=True)


def _write_transform_curves(action, armature_obj, transforms, end_frame, indexed, *, replace=False):
    if replace:
        _remove_pose_transform_fcurves(action, armature_obj)
    metadata_bones = []
    for entry in transforms or []:
        bone_name = str(entry.get("name", "") if isinstance(entry, dict) else entry)
        if not bone_name or bone_name not in armature_obj.pose.bones:
            continue
        group = entry.get("group", "FK") if isinstance(entry, dict) else "FK"
        loc, quat, scale = _transform_values_for_action(armature_obj, entry if isinstance(entry, dict) else {})
        _write_bone_transform_curves(action, armature_obj, bone_name, loc, quat, scale, end_frame)
        metadata_bone = {
            "name": bone_name,
            "group": str(group or "FK"),
            "index": indexed.get(bone_name, -1),
        }
        if isinstance(entry, dict):
            source_rotation = entry.get("sourceRotation")
            if source_rotation is not None:
                metadata_bone["sourceSpace"] = str(entry.get("space", "game") or "game")
                metadata_bone["sourceRotation"] = [
                    _normalize_degrees_signed(value)
                    for value in tuple(source_rotation)[:3]
                ]
        metadata_bones.append(metadata_bone)
    return metadata_bones


def _transform_key(item):
    name = str(item.get("name", "") or "") if isinstance(item, dict) else ""
    group = str(item.get("group", "FK") or "FK").upper() if isinstance(item, dict) else "FK"
    return name, group


def _float_tuple(values, size, default):
    if isinstance(default, (int, float)):
        default_values = [float(default)] * size
    else:
        default_values = list(default)[:size]
        while len(default_values) < size:
            default_values.append(0.0)
    try:
        values = list(values)
    except Exception:
        values = []
    result = []
    for index in range(size):
        fallback = float(default_values[index])
        try:
            result.append(float(values[index]))
        except Exception:
            result.append(fallback)
    return tuple(result)


def _serializable_transform_entry(entry):
    if not isinstance(entry, dict):
        return {}
    name = str(entry.get("name", "") or "")
    if not name:
        return {}
    source_rotation = entry.get("sourceRotation")
    if source_rotation is not None and str(entry.get("space", "") or "").lower() == "game":
        values = _float_tuple(source_rotation, 3, (0.0, 0.0, 0.0))
        quat = _engine_ui_xyz_to_quaternion(values[0], values[1], values[2])
    else:
        quat = _normalized_quaternion(entry.get("rotation", (1.0, 0.0, 0.0, 0.0)))
    result = {
        "name": name,
        "group": str(entry.get("group", "FK") or "FK"),
        "location": list(_float_tuple(entry.get("location", (0.0, 0.0, 0.0)), 3, (0.0, 0.0, 0.0))),
        "rotation": [float(quat.w), float(quat.x), float(quat.y), float(quat.z)],
        "scale": list(_float_tuple(entry.get("scale", (1.0, 1.0, 1.0)), 3, (1.0, 1.0, 1.0))),
    }
    if entry.get("space"):
        result["space"] = str(entry.get("space") or "")
    if source_rotation is not None:
        result["sourceSpace"] = str(entry.get("sourceSpace", entry.get("space", "game")) or "game")
        result["sourceRotation"] = [
            _normalize_degrees_signed(value)
            for value in _float_tuple(source_rotation, 3, (0.0, 0.0, 0.0))
        ]
    return result


def _metadata_with_source_transforms(metadata, new_transforms=None, remove_keys=None):
    metadata = dict(metadata or {})
    remove_keys = {
        (str(name), str(group).upper())
        for name, group in (remove_keys or set())
        if str(name)
    }
    existing = {}
    order = []
    for item in metadata.get("sourceTransforms", []) or []:
        item = _serializable_transform_entry(item)
        key = _transform_key(item)
        if not key[0] or key in remove_keys:
            continue
        if key not in existing:
            order.append(key)
        existing[key] = item
    for item in new_transforms or []:
        item = _serializable_transform_entry(item)
        key = _transform_key(item)
        if not key[0]:
            continue
        if key not in existing:
            order.append(key)
        existing[key] = item
    metadata["sourceTransforms"] = [existing[key] for key in order if key in existing]
    return metadata


def _metadata_with_bones(metadata, new_bones):
    metadata = dict(metadata or {})
    existing = {
        _transform_key(item): dict(item)
        for item in (metadata.get("bones", []) or [])
        if str(item.get("name", ""))
    }
    for item in new_bones or []:
        name = str(item.get("name", "") or "")
        if name:
            existing[_transform_key(item)] = dict(item)
    metadata["bones"] = list(existing.values())
    return metadata


def _metadata_without_transform_keys(metadata, keys):
    metadata = dict(metadata or {})
    keys = {(str(name), str(group).upper()) for name, group in (keys or set()) if str(name)}
    metadata["bones"] = [
        dict(item)
        for item in (metadata.get("bones", []) or [])
        if _transform_key(item) not in keys
    ]
    metadata["sourceTransforms"] = [
        _serializable_transform_entry(item)
        for item in (metadata.get("sourceTransforms", []) or [])
        if _transform_key(item) not in keys and str(item.get("name", "") or "")
    ]
    return metadata


def _replace_metadata_bones(metadata, new_bones):
    metadata = dict(metadata or {})
    metadata["bones"] = list(new_bones or [])
    return metadata


def _action_bone_quaternion(action, armature_obj, bone_name):
    rotation_mode = _pose_bone_rotation_mode(armature_obj, bone_name)
    euler_modes = {'XYZ', 'XZY', 'YXZ', 'YZX', 'ZXY', 'ZYX'}

    quat_path = _bone_data_path(bone_name, "rotation_quaternion")
    quat_curves = {
        index: _find_action_fcurve(action, armature_obj, quat_path, index)
        for index in range(4)
    }
    if any(curve is not None for curve in quat_curves.values()):
        return _normalized_quaternion((
            _first_curve_value(quat_curves.get(0), 1.0),
            _first_curve_value(quat_curves.get(1), 0.0),
            _first_curve_value(quat_curves.get(2), 0.0),
            _first_curve_value(quat_curves.get(3), 0.0),
        ))

    euler_path = _bone_data_path(bone_name, "rotation_euler")
    euler_curves = {
        index: _find_action_fcurve(action, armature_obj, euler_path, index)
        for index in range(3)
    }
    if any(curve is not None for curve in euler_curves.values()):
        euler_order = rotation_mode if rotation_mode in euler_modes else 'XYZ'
        return Euler((
            _first_curve_value(euler_curves.get(0), 0.0),
            _first_curve_value(euler_curves.get(1), 0.0),
            _first_curve_value(euler_curves.get(2), 0.0),
        ), euler_order).to_quaternion()

    return Quaternion((1.0, 0.0, 0.0, 0.0))


def _ensure_metadata_source_transforms(metadata, action, armature_obj):
    metadata = dict(metadata or {})
    if metadata.get("sourceTransforms"):
        metadata["sourceTransforms"] = [
            item
            for item in (_serializable_transform_entry(item) for item in metadata.get("sourceTransforms", []) or [])
            if item.get("name")
        ]
        return metadata

    source_transforms = []
    for item in metadata.get("bones", []) or []:
        bone_name = str(item.get("name", "") or "")
        if not bone_name or bone_name not in getattr(getattr(armature_obj, "pose", None), "bones", {}):
            continue
        loc, scale = _action_bone_loc_scale(action, armature_obj, bone_name)
        source_rotation = item.get("sourceRotation")
        if source_rotation is not None:
            source_rotation = [
                _normalize_degrees_signed(value)
                for value in _float_tuple(source_rotation, 3, (0.0, 0.0, 0.0))
            ]
            quat = _engine_ui_xyz_to_quaternion(source_rotation[0], source_rotation[1], source_rotation[2])
            source_transforms.append({
                "name": bone_name,
                "group": str(item.get("group", "FK") or "FK"),
                "location": loc,
                "rotation": (quat.w, quat.x, quat.y, quat.z),
                "scale": scale,
                "space": str(item.get("sourceSpace", "game") or "game"),
                "sourceRotation": source_rotation,
            })
        else:
            quat = _action_bone_quaternion(action, armature_obj, bone_name)
            source_transforms.append({
                "name": bone_name,
                "group": str(item.get("group", "FK") or "FK"),
                "location": loc,
                "rotation": (quat.w, quat.x, quat.y, quat.z),
                "scale": scale,
                "space": "blender",
            })
    metadata["sourceTransforms"] = [
        item
        for item in (_serializable_transform_entry(item) for item in source_transforms)
        if item.get("name")
    ]
    return metadata


def _compose_preview_transform_entries(armature_obj, source_transforms):
    grouped = {}
    for order, item in enumerate(source_transforms or []):
        item = _serializable_transform_entry(item)
        bone_name = str(item.get("name", "") or "")
        if not bone_name or bone_name not in armature_obj.pose.bones:
            continue
        group = str(item.get("group", "FK") or "FK").upper()
        grouped.setdefault(bone_name, []).append((POSEKEY_GROUP_ORDER.get(group, 99), order, item))

    preview = []
    for bone_name, items in grouped.items():
        matrix = Matrix.Identity(4)
        for _group_order, _order, item in sorted(items, key=lambda data: (data[0], data[1])):
            loc, quat, scale = _transform_values_for_action(armature_obj, item)
            component = Matrix.LocRotScale(
                Vector(loc),
                _normalized_quaternion(quat),
                Vector(scale),
            )
            matrix = matrix @ component
        loc, quat, scale = matrix.decompose()
        preview.append({
            "name": bone_name,
            "group": "PREVIEW",
            "location": tuple(float(value) for value in loc),
            "rotation": (quat.w, quat.x, quat.y, quat.z),
            "scale": tuple(float(value) for value in scale),
            "space": "blender",
        })
    return preview


def _write_pose_key_preview_curves_from_metadata(context, armature_obj, action, metadata):
    if action is None or armature_obj is None or "sourceTransforms" not in (metadata or {}):
        return False
    end_frame = float(metadata.get("durationFrames") or seconds_to_frames(context, metadata.get("duration", 1.0)))
    _remove_pose_transform_fcurves(action, armature_obj)
    for entry in _compose_preview_transform_entries(armature_obj, metadata.get("sourceTransforms", []) or []):
        loc, quat, scale = _transform_values_for_action(armature_obj, entry)
        _write_bone_transform_curves(action, armature_obj, entry["name"], loc, quat, scale, end_frame)
    return True


def _refresh_scene_frame(context):
    scene = getattr(context, "scene", None)
    if scene is None:
        return
    is_playing = bool(getattr(getattr(context, "screen", None), "is_animation_playing", False))
    if is_playing:
        try:
            context.view_layer.update()
        except Exception:
            pass
        return
    try:
        scene.frame_set(scene.frame_current)
    except Exception:
        try:
            context.view_layer.update()
        except Exception:
            pass


def _metadata_json(metadata):
    return json.dumps(metadata or {}, sort_keys=True)


def _set_custom_prop(target, key, value):
    if target is None:
        return False
    try:
        target[key] = value
        return True
    except (AttributeError, RuntimeError, TypeError):
        return False


def _get_custom_prop(target, key, default=None):
    if target is None:
        return default
    getter = getattr(target, "get", None)
    if callable(getter):
        try:
            return getter(key, default)
        except Exception:
            pass
    try:
        return target[key]
    except Exception:
        return default


def _has_pose_key_metadata(target):
    if target is None:
        return False
    if bool(_get_custom_prop(target, POSEKEY_MARKER_PROP, False)):
        return True
    return bool(_get_custom_prop(target, POSEKEY_METADATA_PROP, ""))


def _set_pose_key_metadata(target, metadata):
    if target is None:
        return
    if not _set_custom_prop(target, POSEKEY_MARKER_PROP, True):
        return
    _set_custom_prop(target, POSEKEY_METADATA_PROP, _metadata_json(metadata))
    for key, value in (metadata or {}).items():
        if key in {"bones", "sourceTransforms"}:
            continue
        if isinstance(value, (str, int, float, bool)):
            _set_custom_prop(target, f"w3_pose_key_{key}", value)
    if metadata.get("actor"):
        _set_custom_prop(target, "w3_scene_actor", str(metadata.get("actor")))
    _set_custom_prop(target, "w3_scene_event_class", POSEKEY_CLASS_NAME)
    if metadata.get("eventName"):
        _set_custom_prop(target, "w3_scene_event_name", str(metadata.get("eventName")))
    if metadata.get("section"):
        _set_custom_prop(target, "w3_scene_section", str(metadata.get("section")))
    if metadata.get("guid"):
        _set_custom_prop(target, "w3_scene_event_guid", str(metadata.get("guid")))


def pose_key_metadata_from_target(target):
    raw = str(_get_custom_prop(target, POSEKEY_METADATA_PROP, "") or "")
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def create_pose_key_action(
    context,
    armature_obj,
    bone_entries,
    *,
    actor="",
    duration=1.0,
    blend_in=0.0,
    blend_out=0.0,
    weight=1.0,
    link_to_dialogset=True,
    preset_name="None",
    preset_version=0,
    event_name="Pose key",
    source="created",
    transforms=None,
):
    if armature_obj is None or getattr(armature_obj, "type", None) != 'ARMATURE':
        armature_obj = resolve_armature_object(armature_obj)
    if armature_obj is None or getattr(armature_obj, "type", None) != 'ARMATURE':
        raise ValueError("Select a character armature")

    end_frame = seconds_to_frames(context, duration, minimum=1.0)
    actor = str(actor or actor_name_for_armature(armature_obj)).strip()
    safe_actor = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in (actor or armature_obj.name))
    action_name = f"PoseKey_{safe_actor}_{int(getattr(context.scene, 'frame_current', 0) or 0)}"
    action = bpy.data.actions.new(action_name)
    slot = resolve_action_slot(action, target=armature_obj, ensure=True)

    indexed = bone_index_map(armature_obj)
    transforms_by_key = {
        _transform_key(entry): entry
        for entry in (transforms or [])
        if isinstance(entry, dict) and entry.get("name")
    }
    transforms_by_name = {
        str(entry.get("name", "")): entry
        for entry in (transforms or [])
        if isinstance(entry, dict) and entry.get("name")
    }
    action_transforms = []

    for entry in bone_entries:
        bone_name = entry["name"] if isinstance(entry, dict) else str(entry)
        if bone_name not in armature_obj.pose.bones:
            continue
        group = entry.get("group", "FK") if isinstance(entry, dict) else "FK"
        source_transform = transforms_by_key.get((bone_name, str(group or "FK").upper())) or transforms_by_name.get(bone_name)
        if source_transform:
            loc = source_transform.get("location", (0.0, 0.0, 0.0))
            quat = source_transform.get("rotation", (1.0, 0.0, 0.0, 0.0))
            scale = source_transform.get("scale", (1.0, 1.0, 1.0))
        else:
            loc, quat, scale = _pose_bone_transform(armature_obj.pose.bones[bone_name])
        action_entry = {
            "name": bone_name,
            "group": str(group or "FK"),
            "location": loc,
            "rotation": quat,
            "scale": scale,
        }
        if source_transform:
            if source_transform.get("space"):
                action_entry["space"] = source_transform.get("space")
            if source_transform.get("sourceRotation") is not None:
                action_entry["sourceRotation"] = source_transform.get("sourceRotation")
        action_transforms.append(action_entry)
    metadata_bones = _write_transform_curves(action, armature_obj, action_transforms, end_frame, indexed)

    if not metadata_bones:
        bpy.data.actions.remove(action)
        raise ValueError("No matching pose bones were found")

    metadata = {
        "class": POSEKEY_CLASS_NAME,
        "source": source,
        "actor": actor,
        "eventName": event_name,
        "duration": float(duration),
        "durationFrames": float(end_frame),
        "blendIn": float(blend_in),
        "blendOut": float(blend_out),
        "weightBlendType": "IT_Bezier",
        "weight": float(weight),
        "useWeightCurve": False,
        "linkToDialogset": bool(link_to_dialogset),
        "presetName": str(preset_name or "None"),
        "presetVersion": int(preset_version or 0),
        "bones": metadata_bones,
        "sourceTransforms": [
            item
            for item in (_serializable_transform_entry(entry) for entry in action_transforms)
            if item.get("name")
        ],
    }
    _write_pose_key_preview_curves_from_metadata(context, armature_obj, action, metadata)
    _set_pose_key_metadata(action, metadata)
    return action, metadata, slot


def add_pose_key_action_to_nla(
    context,
    armature_obj,
    action,
    metadata,
    *,
    start_frame=None,
    track_name=POSEKEY_TRACK_NAME,
    nla_blend_type='COMBINE',
):
    if armature_obj.animation_data is None:
        armature_obj.animation_data_create()

    duration_frames = _pose_key_preview_duration_frames(context, metadata)
    if start_frame is None:
        start_frame = float(getattr(context.scene, "frame_current", 0.0) or 0.0)

    base_track_name = str(track_name or POSEKEY_TRACK_NAME)
    track = armature_obj.animation_data.nla_tracks.get(base_track_name)
    if track is None:
        track = armature_obj.animation_data.nla_tracks.new()
        track.name = base_track_name

    try:
        strip = track.strips.new(action.name, int(start_frame), action)
    except Exception:
        track = armature_obj.animation_data.nla_tracks.new()
        track.name = base_track_name
        strip = track.strips.new(action.name, int(start_frame), action)

    strip.frame_start = float(start_frame)
    strip.frame_end = float(start_frame) + max(1.0, duration_frames)
    try:
        strip.extrapolation = 'NOTHING'
    except Exception:
        pass
    strip.blend_in = seconds_to_frames(context, metadata.get("blendIn", 0.0), minimum=0.0)
    strip.blend_out = seconds_to_frames(context, metadata.get("blendOut", 0.0), minimum=0.0)
    strip.influence = max(0.0, min(1.0, float(metadata.get("weight", 1.0))))
    if nla_blend_type in {'REPLACE', 'COMBINE', 'ADD', 'SUBTRACT', 'MULTIPLY'}:
        strip.blend_type = nla_blend_type
    bind_strip_action_slot(strip, resolve_action_slot(action, target=armature_obj, ensure=True))
    _set_pose_key_metadata(strip, metadata)
    return strip


def create_empty_pose_key(
    context,
    armature_obj,
    *,
    actor="",
    duration=1.0,
    blend_in=0.0,
    blend_out=0.0,
    weight=1.0,
    link_to_dialogset=True,
    preset_name="None",
    preset_version=0,
    event_name="Pose key",
    track_name=POSEKEY_TRACK_NAME,
    nla_blend_type='COMBINE',
):
    armature_obj = resolve_armature_object(armature_obj)
    if armature_obj is None or getattr(armature_obj, "type", None) != 'ARMATURE':
        raise ValueError("Select a character armature")

    duration_frames = seconds_to_frames(context, duration, minimum=1.0)
    actor = str(actor or actor_name_for_armature(armature_obj)).strip()
    safe_actor = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in (actor or armature_obj.name))
    action_name = f"PoseKey_{safe_actor}_{int(getattr(context.scene, 'frame_current', 0) or 0)}"
    action = bpy.data.actions.new(action_name)
    resolve_action_slot(action, target=armature_obj, ensure=True)

    metadata = {
        "class": POSEKEY_CLASS_NAME,
        "source": "created",
        "actor": actor,
        "eventName": str(event_name or "Pose key"),
        "duration": float(duration),
        "durationFrames": float(duration_frames),
        "blendIn": float(blend_in),
        "blendOut": float(blend_out),
        "weightBlendType": "IT_Bezier",
        "weight": float(weight),
        "useWeightCurve": False,
        "linkToDialogset": bool(link_to_dialogset),
        "presetName": str(preset_name or "None"),
        "presetVersion": int(preset_version or 0),
        "bones": [],
        "sourceTransforms": [],
    }
    _set_pose_key_metadata(action, metadata)
    strip = add_pose_key_action_to_nla(
        context,
        armature_obj,
        action,
        metadata,
        track_name=track_name,
        nla_blend_type=nla_blend_type,
    )
    return strip, action, metadata


def create_pose_key_from_current_pose(
    context,
    armature_obj,
    bone_entries,
    *,
    actor="",
    duration=1.0,
    blend_in=0.0,
    blend_out=0.0,
    weight=1.0,
    link_to_dialogset=True,
    preset_name="None",
    preset_version=0,
    event_name="Pose key",
    track_name=POSEKEY_TRACK_NAME,
    nla_blend_type='COMBINE',
):
    armature_obj = resolve_armature_object(armature_obj)
    action, metadata, _slot = create_pose_key_action(
        context,
        armature_obj,
        bone_entries,
        actor=actor,
        duration=duration,
        blend_in=blend_in,
        blend_out=blend_out,
        weight=weight,
        link_to_dialogset=link_to_dialogset,
        preset_name=preset_name,
        preset_version=preset_version,
        event_name=event_name,
        source="created",
    )
    strip = add_pose_key_action_to_nla(
        context,
        armature_obj,
        action,
        metadata,
        track_name=track_name,
        nla_blend_type=nla_blend_type,
    )
    return strip, action, metadata


def sync_game_ik_marker_pose(armature_obj):
    if armature_obj is None or getattr(armature_obj, "pose", None) is None:
        return 0
    count = 0
    for ik_name, source_name in GAME_IK_BONE_MAP.items():
        ik_bone = armature_obj.pose.bones.get(ik_name)
        source_bone = armature_obj.pose.bones.get(source_name)
        if ik_bone is None or source_bone is None:
            continue
        try:
            ik_bone.matrix = source_bone.matrix.copy()
            count += 1
        except Exception:
            log.debug("Could not sync IK marker %s from %s", ik_name, source_name, exc_info=True)
    return count


def _value_from_prop(prop, default=None):
    if prop is None:
        return default
    if isinstance(prop, (str, int, float, bool)):
        return prop
    if getattr(prop, "theType", None) == "CName":
        index = getattr(prop, "Index", None)
        if index is not None:
            value = getattr(index, "String", None)
            if value not in (None, ""):
                return value
            if hasattr(index, "ToString"):
                try:
                    return index.ToString()
                except Exception:
                    pass
    string_obj = getattr(prop, "String", None)
    if string_obj is not None:
        value = getattr(string_obj, "String", None)
        if value not in (None, ""):
            return value
        to_string = getattr(string_obj, "ToString", None)
        if callable(to_string):
            try:
                value = to_string()
                if value not in (None, ""):
                    return value
            except Exception:
                pass
    if hasattr(prop, "Value"):
        return getattr(prop, "Value")
    if hasattr(prop, "value"):
        return getattr(prop, "value")
    index = getattr(prop, "Index", None)
    if index is not None:
        for attr in ("String", "DepotPath", "Path"):
            value = getattr(index, attr, None)
            if value not in (None, ""):
                return value
        if hasattr(index, "ToString"):
            try:
                return index.ToString()
            except Exception:
                pass
    return default


def _iter_array_items(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    for attr in ("value", "More", "MoreProps"):
        items = getattr(value, attr, None)
        if isinstance(items, (list, tuple)):
            return list(items)
    return []


def _iter_property_records(value, first_field_names):
    items = _iter_array_items(value)
    first_field_names = {str(name) for name in first_field_names}
    if not items or not any(str(getattr(item, "theName", "") or "") in first_field_names for item in items):
        return items

    records = []
    current = []
    for item in items:
        field_name = str(getattr(item, "theName", "") or "")
        if field_name in first_field_names and current:
            records.append(_PropertyRecord(current))
            current = []
        current.append(item)
    if current:
        records.append(_PropertyRecord(current))
    return records


def _prop_count(value):
    return len(_iter_array_items(value))


def _float_array_from_prop(value):
    result = []
    for item in _iter_array_items(value):
        try:
            result.append(float(_value_from_prop(item, item) or 0.0))
        except Exception:
            result.append(0.0)
    return result


def _child_prop(item, name):
    if item is None:
        return None
    if isinstance(item, dict):
        return item.get(name)
    if hasattr(item, name):
        return getattr(item, name)
    if hasattr(item, "GetVariableByName"):
        try:
            found = item.GetVariableByName(name)
            if found is not None:
                return found
        except Exception:
            pass
    for attr in ("MoreProps", "More", "PROPS"):
        for prop in getattr(item, attr, []) or []:
            if getattr(prop, "theName", None) == name:
                return prop
    return None


def normalize_hand_tracks(values):
    try:
        values = list(values or [])
    except Exception:
        values = []
    result = []
    for index in range(HAND_TRACK_COUNT):
        try:
            value = float(values[index])
        except Exception:
            value = 0.0
        result.append(max(-1.0, min(1.0, value)))
    return result


def hand_track_index(side, part):
    side = str(side or "").upper()
    part = str(part or "")
    if side not in HAND_TRACK_SIDE_INDEX:
        raise ValueError(f"Unknown hand side: {side}")
    if part not in HAND_TRACK_PARTS:
        raise ValueError(f"Unknown hand track: {part}")
    return HAND_TRACK_SIDE_INDEX[side] * len(HAND_TRACK_PARTS) + HAND_TRACK_PARTS.index(part)


def _hand_track_specs_for_side(side):
    prefix = f"{str(side).lower()}_"
    data = {part: [] for part in HAND_TRACK_PARTS}

    def add_bone(part, bone_name, ratio, axis="Z"):
        data[part].append((bone_name, float(ratio), axis))

    def add_finger(part, base_name, twist_ratio, all_ratio=1.0, base_ratio=1.0):
        ratios = (0.3 * base_ratio, 1.0 * base_ratio, 0.6 * base_ratio)
        for offset, ratio in enumerate(ratios):
            bone_name = f"{prefix}{base_name}{3 - offset}"
            add_bone(part, bone_name, ratio)
            add_bone("all", bone_name, all_ratio * ratio)
            add_bone("twist", bone_name, twist_ratio * ratio)

    add_finger("A", "pinky", 1.0)
    add_finger("B", "ring", 0.9)
    add_finger("C", "middle", 0.8)
    add_finger("D", "index", 0.7)
    add_finger("E", "thumb", 0.1, all_ratio=0.1)
    add_bone("handX", f"{prefix}hand", 1.0, "X")
    add_bone("handY", f"{prefix}hand", 1.0, "Y")
    add_bone("handZ", f"{prefix}hand", 1.0, "Z")
    return data


def hand_track_controlled_bones():
    names = set()
    for side in HAND_TRACK_SIDE_INDEX:
        for specs in _hand_track_specs_for_side(side).values():
            for bone_name, _ratio, _axis in specs:
                names.add(bone_name)
    return names


def hand_track_transforms(track_values, armature_obj=None):
    track_values = normalize_hand_tracks(track_values)
    rotations = {}
    for side in HAND_TRACK_SIDE_INDEX:
        side_specs = _hand_track_specs_for_side(side)
        for part in HAND_TRACK_PARTS:
            value = track_values[hand_track_index(side, part)]
            if abs(value) <= 1e-8:
                continue
            for bone_name, ratio, axis in side_specs.get(part, []):
                current = rotations.setdefault(bone_name, [0.0, 0.0, 0.0])
                axis_index = {"X": 0, "Y": 1, "Z": 2}.get(axis, 2)
                current[axis_index] += value * ratio * HAND_BASE_ROT_ANGLE

    pose_bones = getattr(getattr(armature_obj, "pose", None), "bones", None) if armature_obj is not None else None
    results = []
    for bone_name, values in rotations.items():
        if pose_bones is not None and bone_name not in pose_bones:
            continue
        x_value, y_value, z_value = (_normalize_degrees_signed(value) for value in values)
        if (x_value * x_value + y_value * y_value + z_value * z_value) < 0.0001:
            continue
        # Hand sliders already accumulate in the editable pose-key XYZ convention:
        # X -> pitch, Y -> roll, Z -> yaw. Remapping them through EngineTransform
        # display order here turns the finger Z bend into a different axis.
        source_rotation = (x_value, y_value, z_value)
        quat = _engine_ui_xyz_to_quaternion(source_rotation[0], source_rotation[1], source_rotation[2])
        results.append({
            "name": bone_name,
            "group": "HAND",
            "location": (0.0, 0.0, 0.0),
            "rotation": (quat.w, quat.x, quat.y, quat.z),
            "scale": (1.0, 1.0, 1.0),
            "space": "game",
            "sourceRotation": source_rotation,
        })
    return results


def _engine_qs_transform_dict(transform):
    loc, quat, scale = _engine_qs_transform_values(transform)
    return {
        "location": loc,
        "rotation": quat,
        "scale": scale,
        "space": "game",
    }


def _engine_transform_values(transform):
    transform = getattr(transform, "EngineTransform", None) or transform
    loc = (
        float(_value_from_prop(_child_prop(transform, "X"), getattr(transform, "X", 0.0)) or 0.0),
        float(_value_from_prop(_child_prop(transform, "Y"), getattr(transform, "Y", 0.0)) or 0.0),
        float(_value_from_prop(_child_prop(transform, "Z"), getattr(transform, "Z", 0.0)) or 0.0),
    )
    pitch = float(_value_from_prop(_child_prop(transform, "Pitch"), getattr(transform, "Pitch", 0.0)) or 0.0)
    yaw = float(_value_from_prop(_child_prop(transform, "Yaw"), getattr(transform, "Yaw", 0.0)) or 0.0)
    roll = float(_value_from_prop(_child_prop(transform, "Roll"), getattr(transform, "Roll", 0.0)) or 0.0)
    source_rotation = _engine_rotation_to_ui_xyz(roll, pitch, yaw)
    quat = _engine_ui_xyz_to_quaternion(source_rotation[0], source_rotation[1], source_rotation[2])
    scale = (
        float(_value_from_prop(_child_prop(transform, "Scale_x"), getattr(transform, "Scale_x", 1.0)) or 1.0),
        float(_value_from_prop(_child_prop(transform, "Scale_y"), getattr(transform, "Scale_y", 1.0)) or 1.0),
        float(_value_from_prop(_child_prop(transform, "Scale_z"), getattr(transform, "Scale_z", 1.0)) or 1.0),
    )
    return loc, (quat.w, quat.x, quat.y, quat.z), scale, source_rotation


def _engine_qs_transform_values(transform):
    loc = (
        float(getattr(transform, "x", 0.0) or 0.0),
        float(getattr(transform, "y", 0.0) or 0.0),
        float(getattr(transform, "z", 0.0) or 0.0),
    )
    quat = Quaternion((
        float(getattr(transform, "w", 1.0) or 1.0),
        float(getattr(transform, "pitch", 0.0) or 0.0),
        float(getattr(transform, "yaw", 0.0) or 0.0),
        float(getattr(transform, "roll", 0.0) or 0.0),
    ))
    quat = _normalized_quaternion(quat)
    sx = float(getattr(transform, "scale_x", 1.0) or 1.0)
    sy = float(getattr(transform, "scale_y", 1.0) or 1.0)
    sz = float(getattr(transform, "scale_z", 1.0) or 1.0)
    return loc, (quat.w, quat.x, quat.y, quat.z), (sx, sy, sz)


def _extract_sss_bone_transforms(event, prop_name, group):
    results = []
    source = getattr(event, prop_name, None)
    if source is None:
        source = _child_prop(event, prop_name)
    for item in _iter_array_items(source):
        bone_name = str(_value_from_prop(_child_prop(item, "bone"), "") or "").strip()
        transform = _child_prop(item, "transform")
        if not bone_name or transform is None:
            continue
        loc, quat, scale, source_rotation = _engine_transform_values(transform)
        results.append({
            "name": bone_name,
            "group": group,
            "location": loc,
            "rotation": quat,
            "scale": scale,
            "space": "game",
            "sourceRotation": source_rotation,
        })
    return results


def _extract_cached_ik_transforms(event, armature_obj):
    indices = [int(_value_from_prop(item, item) or 0) for item in _iter_array_items(getattr(event, "cachedBonesIK", None))]
    transforms = _iter_array_items(getattr(event, "cachedTransformsIK", None))
    names_by_index = bone_name_by_index(armature_obj)
    results = []
    for index, transform in zip(indices, transforms):
        bone_name = names_by_index.get(index)
        if not bone_name:
            continue
        loc, quat, scale = _engine_qs_transform_values(transform)
        results.append({
            "name": bone_name,
            "group": "IK",
            "location": loc,
            "rotation": quat,
            "scale": scale,
            "space": "game",
        })
    return results


def redkit_pose_presets_path(project_path):
    project_path = os.path.normpath(str(project_path or "").strip())
    if not project_path:
        return ""
    return os.path.normpath(os.path.join(project_path, REDKIT_POSE_PRESETS_RELATIVE_PATH))


def _preset_transform_dicts(preset_element, prop_name, group):
    return _extract_sss_bone_transforms(preset_element, prop_name, group)


def _preset_index_array(preset_element, prop_name):
    return [
        int(_value_from_prop(item, item) or 0)
        for item in _iter_array_items(_child_prop(preset_element, prop_name))
    ]


def _preset_qs_transform_array(preset_element, prop_name):
    return [
        _engine_qs_transform_dict(item)
        for item in _iter_array_items(_child_prop(preset_element, prop_name))
    ]


def _find_chunk(cr2w_file, chunk_type):
    for chunk in getattr(getattr(cr2w_file, "CHUNKS", None), "CHUNKS", []) or []:
        if str(getattr(chunk, "name", "") or getattr(chunk, "Type", "") or "") == chunk_type:
            return chunk
    return None


def read_redkit_pose_presets(presets_path):
    presets_path = os.path.normpath(str(presets_path or "").strip())
    if not presets_path:
        raise ValueError("No REDkit preset file path was provided")
    if not os.path.isfile(presets_path):
        raise FileNotFoundError(presets_path)

    from ..CR2W.CR2W_types import getCR2W

    with open(presets_path, "rb") as handle:
        cr2w_file = getCR2W(handle)

    scene_presets = _find_chunk(cr2w_file, "CStoryScenePresets")
    pose_presets_prop = scene_presets.GetVariableByName("posePresets") if scene_presets is not None else None
    results = []
    for preset_element in _iter_property_records(pose_presets_prop, ("presetName",)):
        preset_name = str(
            _value_from_prop(_child_prop(preset_element, "presetName"), "")
            or _value_from_prop(_child_prop(preset_element, "presetCName"), "")
            or getattr(preset_element, "elementName", "")
            or ""
        ).strip()
        if not preset_name:
            continue

        version = int(_value_from_prop(_child_prop(preset_element, "presetVersion"), 0) or 0)
        fk_transforms = _preset_transform_dicts(preset_element, "bones", "FK")
        hand_transforms = _preset_transform_dicts(preset_element, "bonesHands", "HAND")
        hand_tracks = _float_array_from_prop(_child_prop(preset_element, "editorCachedHandTracks"))
        ik_indices = _preset_index_array(preset_element, "cachedBonesIK")
        ik_transforms = _preset_qs_transform_array(preset_element, "cachedTransformsIK")
        tracks_prop = _child_prop(preset_element, "tracks")

        results.append({
            "name": preset_name,
            "version": version,
            "sourcePath": presets_path,
            "transforms": fk_transforms + hand_transforms,
            "handTracks": normalize_hand_tracks(hand_tracks) if hand_tracks else [],
            "cachedBonesIK": ik_indices,
            "cachedTransformsIK": ik_transforms,
            "boneCount": len(fk_transforms),
            "handCount": len(hand_transforms),
            "ikCount": min(len(ik_indices), len(ik_transforms)),
            "trackCount": _prop_count(tracks_prop),
        })
    return results


def transforms_for_preset(preset, armature_obj):
    armature_obj = resolve_armature_object(armature_obj)
    transforms = [dict(item) for item in (preset.get("transforms", []) or []) if item.get("name")]
    if preset.get("handTracks") and not any(str(item.get("group", "") or "").upper() == "HAND" for item in transforms):
        transforms.extend(hand_track_transforms(preset.get("handTracks"), armature_obj))
    names_by_index = bone_name_by_index(armature_obj) if armature_obj is not None else {}
    ik_indices = list(preset.get("cachedBonesIK", []) or [])
    ik_transforms = list(preset.get("cachedTransformsIK", []) or [])
    for index, transform in zip(ik_indices, ik_transforms):
        bone_name = names_by_index.get(int(index))
        if not bone_name:
            continue
        entry = dict(transform)
        entry["name"] = bone_name
        entry["group"] = "IK"
        transforms.append(entry)
    return transforms


def create_pose_key_from_preset(
    context,
    armature_obj,
    preset,
    *,
    actor="",
    duration=1.0,
    blend_in=0.0,
    blend_out=0.0,
    weight=1.0,
    link_to_dialogset=True,
    event_name="Pose key",
    track_name=POSEKEY_TRACK_NAME,
    nla_blend_type='COMBINE',
):
    armature_obj = resolve_armature_object(armature_obj)
    transforms = transforms_for_preset(preset, armature_obj)
    if not transforms:
        raise ValueError("The selected preset has no transforms for this armature")

    preset_name = str(preset.get("name", "None") or "None")
    preset_version = int(preset.get("version", 0) or 0)
    action, metadata, _slot = create_pose_key_action(
        context,
        armature_obj,
        [{"name": t["name"], "group": t.get("group", "FK")} for t in transforms],
        actor=actor,
        duration=duration,
        blend_in=blend_in,
        blend_out=blend_out,
        weight=weight,
        link_to_dialogset=link_to_dialogset,
        preset_name=preset_name,
        preset_version=preset_version,
        event_name=event_name or preset_name,
        source="redPresets",
        transforms=transforms,
    )
    metadata["sourcePath"] = str(preset.get("sourcePath", "") or "")
    if preset.get("handTracks"):
        metadata["handTracks"] = normalize_hand_tracks(preset.get("handTracks"))
    _set_pose_key_metadata(action, metadata)
    strip = add_pose_key_action_to_nla(
        context,
        armature_obj,
        action,
        metadata,
        track_name=track_name,
        nla_blend_type=nla_blend_type,
    )
    return strip, action, metadata


def import_pose_key_event_to_nla(context, event, armature_obj, start_frame, *, track_name="", section_name=""):
    armature_obj = resolve_armature_object(armature_obj)
    if armature_obj is None:
        return None

    transforms = []
    transforms.extend(_extract_sss_bone_transforms(event, "bones", "FK"))
    transforms.extend(_extract_sss_bone_transforms(event, "bonesHands", "HAND"))
    transforms.extend(_extract_cached_ik_transforms(event, armature_obj))

    if not transforms:
        return None

    actor = str(getattr(event, "actor", "") or actor_name_for_armature(armature_obj)).strip()
    source_duration = max(0.0, _float_or_default(getattr(event, "duration", None), 0.0))
    duration = source_duration
    blend_in = max(0.0, _float_or_default(getattr(event, "blendIn", None), 0.0))
    blend_out = max(0.0, _float_or_default(getattr(event, "blendOut", None), 0.0))
    weight = _float_or_default(getattr(event, "weight", None), 1.0)
    link_to_dialogset = _bool_or_default(getattr(event, "linkToDialogset", None), True)
    preset_name = str(getattr(event, "presetName", "None") or "None")
    preset_version = int(getattr(event, "presetVersion", 0) or 0)
    event_name = str(getattr(event, "eventName", "") or "Pose key")

    action, metadata, _slot = create_pose_key_action(
        context,
        armature_obj,
        [{"name": t["name"], "group": t.get("group", "FK")} for t in transforms],
        actor=actor,
        duration=duration,
        blend_in=blend_in,
        blend_out=blend_out,
        weight=weight,
        link_to_dialogset=link_to_dialogset,
        preset_name=preset_name,
        preset_version=preset_version,
        event_name=event_name,
        source="w2scene",
        transforms=transforms,
    )

    guid = str(getattr(event, "GUID", "") or "")
    metadata["guid"] = guid
    metadata["section"] = str(section_name or "")
    metadata["sourceDuration"] = float(source_duration)
    hand_tracks = _float_array_from_prop(getattr(event, "editorCachedHandTracks", None) or _child_prop(event, "editorCachedHandTracks"))
    if hand_tracks:
        metadata["handTracks"] = normalize_hand_tracks(hand_tracks)
    _set_pose_key_metadata(action, metadata)
    strip = add_pose_key_action_to_nla(
        context,
        armature_obj,
        action,
        metadata,
        start_frame=float(start_frame),
        track_name=track_name or POSEKEY_TRACK_NAME,
        nla_blend_type='COMBINE',
    )
    return strip


def set_pose_key_preview_hold(context, strip, hold_until_frame=None):
    if strip is None:
        return {}
    action = getattr(strip, "action", None)
    metadata = dict(pose_key_metadata_from_strip(strip) or {})
    metadata.setdefault("class", POSEKEY_CLASS_NAME)

    frame_start = float(getattr(strip, "frame_start", 0.0) or 0.0)
    nominal_duration = max(1.0, _pose_key_duration_frames(context, metadata))
    nominal_end = frame_start + nominal_duration
    link_to_dialogset = bool(metadata.get("linkToDialogset", False))

    if link_to_dialogset and hold_until_frame is not None:
        try:
            preview_end = max(nominal_end, float(hold_until_frame))
        except Exception:
            preview_end = nominal_end
        metadata["previewHoldUntilFrame"] = float(preview_end)
        metadata["previewDurationFrames"] = max(1.0, preview_end - frame_start)
        metadata["previewMode"] = "dialogsetHold"
    else:
        preview_end = nominal_end
        metadata.pop("previewHoldUntilFrame", None)
        metadata.pop("previewDurationFrames", None)
        metadata.pop("previewMode", None)

    try:
        strip.frame_end = max(frame_start + 1.0, preview_end)
        strip.extrapolation = 'NOTHING'
    except Exception:
        pass
    if action is not None:
        _set_pose_key_metadata(action, metadata)
    _set_pose_key_metadata(strip, metadata)
    return metadata


def iter_pose_key_strips(armature_obj):
    armature_obj = resolve_armature_object(armature_obj)
    if armature_obj is None or getattr(armature_obj, "animation_data", None) is None:
        return []
    items = []
    for track in armature_obj.animation_data.nla_tracks:
        for strip in track.strips:
            action = getattr(strip, "action", None)
            if not (_has_pose_key_metadata(strip) or _has_pose_key_metadata(action)):
                continue
            metadata = pose_key_metadata_from_strip(strip)
            items.append({
                "strip": strip,
                "action": action,
                "track": track,
                "trackName": str(getattr(track, "name", "") or ""),
                "name": str(getattr(strip, "name", "") or ""),
                "frameStart": float(getattr(strip, "frame_start", 0.0) or 0.0),
                "frameEnd": float(getattr(strip, "frame_end", 0.0) or 0.0),
                "durationFrames": max(0.0, float(getattr(strip, "frame_end", 0.0) or 0.0) - float(getattr(strip, "frame_start", 0.0) or 0.0)),
                "metadata": metadata,
            })
    items.sort(key=lambda item: (item["frameStart"], item["trackName"], item["name"]))
    return items


def find_pose_key_strip_by_name(armature_obj, strip_name):
    strip_name = str(strip_name or "")
    if not strip_name:
        return None
    for item in iter_pose_key_strips(armature_obj):
        strip = item.get("strip")
        if str(getattr(strip, "name", "") or "") == strip_name:
            return strip
    return None


def set_active_pose_key_strip(context, armature_obj, strip_name):
    strip = find_pose_key_strip_by_name(armature_obj, strip_name)
    if strip is None:
        return None
    scene = getattr(context, "scene", None)
    metadata = pose_key_metadata_from_strip(strip)
    if scene is not None:
        scene.witcher_rig_last_pose_key_strip = str(getattr(strip, "name", "") or "")
        scene.witcher_rig_last_pose_key_metadata = json.dumps(metadata, sort_keys=True)
    return strip


def remove_pose_key_strip(context, armature_obj, strip_name, *, remove_action=True):
    strip_name = str(strip_name or "")
    if not strip_name:
        return False
    armature_obj = resolve_armature_object(armature_obj)
    if armature_obj is None or getattr(armature_obj, "animation_data", None) is None:
        return False
    for track in armature_obj.animation_data.nla_tracks:
        for strip in list(track.strips):
            if str(getattr(strip, "name", "") or "") != strip_name:
                continue
            action = getattr(strip, "action", None)
            track.strips.remove(strip)
            if remove_action and action is not None and getattr(action, "users", 0) == 0:
                try:
                    bpy.data.actions.remove(action)
                except Exception:
                    pass
            scene = getattr(context, "scene", None)
            if scene is not None and str(getattr(scene, "witcher_rig_last_pose_key_strip", "") or "") == strip_name:
                scene.witcher_rig_last_pose_key_strip = ""
                scene.witcher_rig_last_pose_key_metadata = ""
            return True
    return False


def update_pose_key_strip_settings(
    context,
    armature_obj,
    strip,
    *,
    actor=None,
    event_name=None,
    start_frame=None,
    duration=None,
    blend_in=None,
    blend_out=None,
    weight=None,
    link_to_dialogset=None,
    preset_name=None,
    preset_version=None,
    nla_blend_type=None,
):
    armature_obj = resolve_armature_object(armature_obj)
    if armature_obj is None or strip is None:
        return {}
    action = getattr(strip, "action", None)
    metadata = dict(pose_key_metadata_from_strip(strip) or {})
    metadata.setdefault("class", POSEKEY_CLASS_NAME)

    if actor is not None:
        metadata["actor"] = str(actor or actor_name_for_armature(armature_obj)).strip()
    if event_name is not None:
        metadata["eventName"] = str(event_name or "Pose key")
    if duration is not None:
        metadata["duration"] = float(duration)
        metadata["durationFrames"] = float(seconds_to_frames(context, duration, minimum=1.0))
    if blend_in is not None:
        metadata["blendIn"] = float(blend_in)
    if blend_out is not None:
        metadata["blendOut"] = float(blend_out)
    if weight is not None:
        metadata["weight"] = max(0.0, min(1.0, float(weight)))
    if link_to_dialogset is not None:
        metadata["linkToDialogset"] = bool(link_to_dialogset)
        if not metadata["linkToDialogset"]:
            metadata.pop("previewHoldUntilFrame", None)
            metadata.pop("previewDurationFrames", None)
            metadata.pop("previewMode", None)
    if preset_name is not None:
        metadata["presetName"] = str(preset_name or "None")
    if preset_version is not None:
        metadata["presetVersion"] = int(preset_version or 0)

    current_start = float(getattr(strip, "frame_start", 0.0) or 0.0)
    current_duration = max(
        1.0,
        float(getattr(strip, "frame_end", current_start + 1.0) or current_start + 1.0) - current_start,
    )
    target_start = float(start_frame) if start_frame is not None else current_start
    target_duration = _pose_key_preview_duration_frames(context, metadata) if metadata.get("durationFrames") is not None else current_duration
    target_end = target_start + max(1.0, target_duration)
    try:
        if target_start >= float(getattr(strip, "frame_end", current_start + current_duration) or current_start + current_duration):
            strip.frame_end = target_end
            strip.frame_start = target_start
        else:
            strip.frame_start = target_start
            strip.frame_end = target_end
    except Exception:
        pass

    try:
        if blend_in is not None:
            strip.blend_in = seconds_to_frames(context, blend_in, minimum=0.0)
        if blend_out is not None:
            strip.blend_out = seconds_to_frames(context, blend_out, minimum=0.0)
        if weight is not None:
            strip.influence = metadata["weight"]
        if nla_blend_type in {'REPLACE', 'COMBINE', 'ADD', 'SUBTRACT', 'MULTIPLY'}:
            strip.blend_type = nla_blend_type
    except Exception:
        pass

    if action is not None:
        _set_pose_key_metadata(action, metadata)
    _set_pose_key_metadata(strip, metadata)
    _refresh_scene_frame(context)
    return metadata


def find_pose_key_strip(context, armature_obj=None):
    if armature_obj is None:
        armature_obj = getattr(context, "object", None)
    armature_obj = resolve_armature_object(armature_obj)
    if armature_obj is None or getattr(armature_obj, "animation_data", None) is None:
        return None
    scene = getattr(context, "scene", None)
    last_strip = str(getattr(scene, "witcher_rig_last_pose_key_strip", "") or "") if scene is not None else ""
    strip = find_pose_key_strip_by_name(armature_obj, last_strip)
    if strip is not None:
        return strip

    active_strip = getattr(context, "active_nla_strip", None)
    if active_strip is not None:
        if _has_pose_key_metadata(active_strip) or _has_pose_key_metadata(getattr(active_strip, "action", None)):
            return active_strip

    if bool(getattr(getattr(context, "screen", None), "is_animation_playing", False)):
        return None

    frame = float(getattr(getattr(context, "scene", None), "frame_current", 0.0) or 0.0)
    for item in iter_pose_key_strips(armature_obj):
        strip = item.get("strip")
        if strip is not None:
            if float(strip.frame_start) <= frame <= float(strip.frame_end):
                return strip
    return None


def pose_key_metadata_from_strip(strip):
    metadata = pose_key_metadata_from_target(strip)
    if metadata:
        return metadata
    return pose_key_metadata_from_target(getattr(strip, "action", None))


def active_pose_key_action(context, armature_obj=None):
    strip = find_pose_key_strip(context, armature_obj)
    return strip, getattr(strip, "action", None) if strip is not None else None


def pose_key_hand_tracks(context, armature_obj=None):
    strip, _action = active_pose_key_action(context, armature_obj)
    metadata = pose_key_metadata_from_strip(strip)
    values = metadata.get("handTracks")
    if values is None:
        values = metadata.get("editorCachedHandTracks")
    return normalize_hand_tracks(values or [])


def set_pose_key_hand_tracks(context, armature_obj, track_values):
    armature_obj = resolve_armature_object(armature_obj)
    strip, action = active_pose_key_action(context, armature_obj)
    if action is None:
        raise ValueError("No active PoseKey strip")
    if armature_obj is None or getattr(armature_obj, "pose", None) is None:
        raise ValueError("Select a character armature")

    track_values = normalize_hand_tracks(track_values)
    metadata = _ensure_metadata_source_transforms(pose_key_metadata_from_strip(strip), action, armature_obj)
    metadata["handTracks"] = list(track_values)
    metadata["editorCachedHandTracks"] = list(track_values)

    controlled = {
        (bone_name, "HAND")
        for bone_name in hand_track_controlled_bones()
    }
    hand_transforms = hand_track_transforms(track_values, armature_obj)
    metadata = _metadata_without_transform_keys(metadata, controlled)
    indexed = bone_index_map(armature_obj)
    hand_bones = [
        {
            "name": str(item.get("name", "") or ""),
            "group": "HAND",
            "index": indexed.get(str(item.get("name", "") or ""), -1),
            "sourceSpace": str(item.get("space", "game") or "game"),
            "sourceRotation": [
                _normalize_degrees_signed(value)
                for value in _float_tuple(item.get("sourceRotation", (0.0, 0.0, 0.0)), 3, (0.0, 0.0, 0.0))
            ],
        }
        for item in hand_transforms
        if str(item.get("name", "") or "")
    ]
    metadata = _metadata_with_bones(metadata, hand_bones)
    metadata = _metadata_with_source_transforms(metadata, hand_transforms)
    _write_pose_key_preview_curves_from_metadata(context, armature_obj, action, metadata)
    _set_pose_key_metadata(action, metadata)
    _set_pose_key_metadata(strip, metadata)
    _refresh_scene_frame(context)
    return metadata


def pose_key_bone_entries(context, armature_obj=None, mode=None):
    strip, action = active_pose_key_action(context, armature_obj)
    metadata = pose_key_metadata_from_strip(strip)
    entries = []
    for item in metadata.get("bones", []) or []:
        name = str(item.get("name", "") or "")
        group = str(item.get("group", "FK") or "FK").upper()
        if not name:
            continue
        if mode and group != str(mode).upper():
            continue
        entries.append({"name": name, "group": group, "index": int(item.get("index", -1) or -1)})

    if entries or action is None:
        return entries

    seen = set()
    for fcurve in iter_action_fcurves(action, target=armature_obj):
        parsed = _parse_pose_bone_data_path(getattr(fcurve, "data_path", ""))
        if parsed is None:
            continue
        bone_name, _prop_name = parsed
        if bone_name in seen:
            continue
        group = "IK" if is_game_ik_marker(bone_name) else ("HAND" if is_hand_bone_name(bone_name) else "FK")
        if mode and group != str(mode).upper():
            continue
        seen.add(bone_name)
        entries.append({"name": bone_name, "group": group, "index": -1})
    return entries


def pose_key_bone_rotation_euler(context, armature_obj, bone_name, group=None):
    strip, action = active_pose_key_action(context, armature_obj)
    if action is None or not bone_name:
        return None
    metadata = pose_key_metadata_from_strip(strip)
    group_upper = str(group or "").upper()
    source_transforms = metadata.get("sourceTransforms", []) or []
    candidates = [
        item for item in source_transforms
        if str(item.get("name", "") or "") == bone_name
    ]
    if group_upper:
        candidates = [
            item for item in candidates
            if str(item.get("group", "FK") or "FK").upper() == group_upper
        ] or candidates
    for item in candidates:
        source_rotation = item.get("sourceRotation")
        if source_rotation is not None:
            values = list(source_rotation)[:3]
            while len(values) < 3:
                values.append(0.0)
            return tuple(math.radians(_normalize_degrees_signed(value)) for value in values)
        try:
            _loc, quat, _scale = _transform_values_for_action(armature_obj, item)
            rotation_mode = _pose_bone_rotation_mode(armature_obj, bone_name)
            euler_order = rotation_mode if rotation_mode in {'XYZ', 'XZY', 'YXZ', 'YZX', 'ZXY', 'ZYX'} else 'XYZ'
            return tuple(float(value) for value in _normalized_quaternion(quat).to_euler(euler_order))
        except Exception:
            pass

    metadata_bones = [
        item for item in metadata.get("bones", []) or []
        if str(item.get("name", "") or "") == bone_name
    ]
    if group_upper:
        metadata_bones = [
            item for item in metadata_bones
            if str(item.get("group", "FK") or "FK").upper() == group_upper
        ] or metadata_bones
    for item in metadata_bones:
        source_rotation = item.get("sourceRotation")
        if source_rotation is not None:
            values = list(source_rotation)[:3]
            while len(values) < 3:
                values.append(0.0)
            return tuple(math.radians(_normalize_degrees_signed(value)) for value in values)

    euler_modes = {'XYZ', 'XZY', 'YXZ', 'YZX', 'ZXY', 'ZYX'}
    rotation_mode = _pose_bone_rotation_mode(armature_obj, bone_name)
    euler_order = rotation_mode if rotation_mode in euler_modes else 'XYZ'

    quat_path = _bone_data_path(bone_name, "rotation_quaternion")
    quat_curves = {
        index: _find_action_fcurve(action, armature_obj, quat_path, index)
        for index in range(4)
    }
    if any(curve is not None for curve in quat_curves.values()):
        quat = Quaternion((
            _first_curve_value(quat_curves.get(0), 1.0),
            _first_curve_value(quat_curves.get(1), 0.0),
            _first_curve_value(quat_curves.get(2), 0.0),
            _first_curve_value(quat_curves.get(3), 0.0),
        ))
        quat = _normalized_quaternion(quat)
        return tuple(float(value) for value in quat.to_euler(euler_order))

    euler_path = _bone_data_path(bone_name, "rotation_euler")
    euler_curves = {
        index: _find_action_fcurve(action, armature_obj, euler_path, index)
        for index in range(3)
    }
    if any(curve is not None for curve in euler_curves.values()):
        return (
            _first_curve_value(euler_curves.get(0), 0.0),
            _first_curve_value(euler_curves.get(1), 0.0),
            _first_curve_value(euler_curves.get(2), 0.0),
        )
    return (0.0, 0.0, 0.0)


def _action_bone_loc_scale(action, armature_obj, bone_name):
    pose_bone = armature_obj.pose.bones.get(bone_name) if getattr(armature_obj, "pose", None) else None
    current_loc = tuple(float(v) for v in getattr(pose_bone, "location", (0.0, 0.0, 0.0)))
    current_scale = tuple(float(v) for v in getattr(pose_bone, "scale", (1.0, 1.0, 1.0)))
    loc_path = _bone_data_path(bone_name, "location")
    scale_path = _bone_data_path(bone_name, "scale")
    loc = tuple(
        _first_curve_value(_find_action_fcurve(action, armature_obj, loc_path, index), current_loc[index])
        for index in range(3)
    )
    scale = tuple(
        _first_curve_value(_find_action_fcurve(action, armature_obj, scale_path, index), current_scale[index])
        for index in range(3)
    )
    return loc, scale


def _quat_angular_error(a, b):
    try:
        diff = _normalized_quaternion(a).rotation_difference(_normalized_quaternion(b))
        return abs(float(getattr(diff, "angle", 0.0) or 0.0))
    except Exception:
        try:
            diff = _normalized_quaternion(a).inverted() @ _normalized_quaternion(b)
            return abs(float(getattr(diff, "angle", 0.0) or 0.0))
        except Exception:
            return float("inf")


def _write_single_bone_rotation_curves(action, armature_obj, bone_name, loc, quat, scale, end_frame, indexed, group):
    _remove_bone_transform_fcurves(action, armature_obj, bone_name)
    return _write_transform_curves(action, armature_obj, [{
        "name": bone_name,
        "group": str(group or "FK"),
        "location": loc,
        "rotation": (quat.w, quat.x, quat.y, quat.z),
        "scale": scale,
        "space": "blender",
    }], end_frame, indexed)


def set_pose_key_bone_rotation_euler(context, armature_obj, bone_name, euler_values, *, group="FK"):
    strip, action = active_pose_key_action(context, armature_obj)
    if action is None:
        raise ValueError("No active PoseKey strip at the current frame")
    if bone_name not in armature_obj.pose.bones:
        raise ValueError(f"Bone not found: {bone_name}")

    metadata = _ensure_metadata_source_transforms(pose_key_metadata_from_strip(strip), action, armature_obj)
    end_frame = float(metadata.get("durationFrames") or seconds_to_frames(context, metadata.get("duration", 1.0)))
    indexed = bone_index_map(armature_obj)
    group = str(group or "FK")
    group_upper = group.upper()
    metadata_bone = None
    for item in metadata.get("bones", []) or []:
        if (
            str(item.get("name", "") or "") == bone_name
            and str(item.get("group", "FK") or "FK").upper() == group_upper
        ):
            metadata_bone = item
            break
    if metadata_bone is not None and metadata_bone.get("sourceRotation") is not None:
        loc, scale = _action_bone_loc_scale(action, armature_obj, bone_name)
        source_rotation = [
            _normalize_degrees_signed(math.degrees(float(value)))
            for value in tuple(euler_values)[:3]
        ]
        while len(source_rotation) < 3:
            source_rotation.append(0.0)
        quat = _engine_ui_xyz_to_quaternion(source_rotation[0], source_rotation[1], source_rotation[2])
        _remove_bone_transform_fcurves(action, armature_obj, bone_name)
        transform_entry = {
            "name": bone_name,
            "group": str(group or metadata_bone.get("group", "FK") or "FK"),
            "location": loc,
            "rotation": (quat.w, quat.x, quat.y, quat.z),
            "scale": scale,
            "space": "game",
            "sourceRotation": source_rotation,
        }
        new_bones = _write_transform_curves(action, armature_obj, [transform_entry], end_frame, indexed)
        metadata = _metadata_with_bones(metadata, new_bones)
        metadata = _metadata_with_source_transforms(metadata, [transform_entry])
        _write_pose_key_preview_curves_from_metadata(context, armature_obj, action, metadata)
        _set_pose_key_metadata(action, metadata)
        _set_pose_key_metadata(strip, metadata)
        _refresh_scene_frame(context)
        return metadata

    rotation_mode = _pose_bone_rotation_mode(armature_obj, bone_name)
    if rotation_mode in {'XYZ', 'XZY', 'YXZ', 'YZX', 'ZXY', 'ZYX'}:
        data_path = _bone_data_path(bone_name, "rotation_euler")
        for index, value in enumerate(tuple(euler_values)[:3]):
            _insert_constant_curve(action, armature_obj, data_path, index, float(value), end_frame, bone_name)
    else:
        quat = Euler(tuple(euler_values)[:3], 'XYZ').to_quaternion()
        data_path = _bone_data_path(bone_name, "rotation_quaternion")
        for index, value in enumerate((quat.w, quat.x, quat.y, quat.z)):
            _insert_constant_curve(action, armature_obj, data_path, index, float(value), end_frame, bone_name)

    if _find_action_fcurve(action, armature_obj, _bone_data_path(bone_name, "location"), 0) is None:
        loc, _quat, scale = _pose_bone_transform(armature_obj.pose.bones[bone_name])
        for index, value in enumerate(loc):
            _insert_constant_curve(action, armature_obj, _bone_data_path(bone_name, "location"), index, value, end_frame, bone_name)
        for index, value in enumerate(scale):
            _insert_constant_curve(action, armature_obj, _bone_data_path(bone_name, "scale"), index, value, end_frame, bone_name)

    loc, scale = _action_bone_loc_scale(action, armature_obj, bone_name)
    if rotation_mode in {'XYZ', 'XZY', 'YXZ', 'YZX', 'ZXY', 'ZYX'}:
        quat = Euler(tuple(euler_values)[:3], rotation_mode).to_quaternion()
    else:
        quat = Euler(tuple(euler_values)[:3], 'XYZ').to_quaternion()
    transform_entry = {
        "name": bone_name,
        "group": group,
        "location": loc,
        "rotation": (quat.w, quat.x, quat.y, quat.z),
        "scale": scale,
        "space": "blender",
    }
    new_bone = {"name": bone_name, "group": group, "index": indexed.get(bone_name, -1)}
    metadata = _metadata_with_bones(metadata, [new_bone])
    metadata = _metadata_with_source_transforms(metadata, [transform_entry])
    _write_pose_key_preview_curves_from_metadata(context, armature_obj, action, metadata)
    _set_pose_key_metadata(action, metadata)
    _set_pose_key_metadata(strip, metadata)
    _refresh_scene_frame(context)
    return metadata


def set_pose_key_bone_rotation_from_current_pose(context, armature_obj, bone_name, *, group="FK"):
    strip, action = active_pose_key_action(context, armature_obj)
    if action is None:
        raise ValueError("No active PoseKey strip")
    if bone_name not in armature_obj.pose.bones:
        raise ValueError(f"Bone not found: {bone_name}")

    pose_bone = armature_obj.pose.bones[bone_name]
    target_quat = _pose_bone_quaternion(pose_bone)
    target_loc = tuple(float(v) for v in getattr(pose_bone, "location", (0.0, 0.0, 0.0)))
    target_scale = tuple(float(v) for v in getattr(pose_bone, "scale", (1.0, 1.0, 1.0)))

    metadata = pose_key_metadata_from_strip(strip)
    end_frame = float(metadata.get("durationFrames") or seconds_to_frames(context, metadata.get("duration", 1.0)))
    loc, scale = _action_bone_loc_scale(action, armature_obj, bone_name)
    if loc == target_loc and scale == target_scale:
        loc, scale = target_loc, target_scale

    # The viewport target already includes this PoseKey bone's old value.
    # Clear only this bone before solving the replacement, so capture is not cumulative.
    _remove_bone_transform_fcurves(action, armature_obj, bone_name)
    _refresh_scene_frame(context)

    base_quat = None
    blend_type = str(getattr(strip, "blend_type", "") or "").upper()
    if blend_type == "COMBINE":
        base_quat = _pose_bone_quaternion(armature_obj.pose.bones[bone_name])

    candidates = [("target", target_quat)]
    if base_quat is not None:
        candidates = [
            ("base_inverse_target", base_quat.inverted() @ target_quat),
            ("target_base_inverse", target_quat @ base_quat.inverted()),
            ("target", target_quat),
        ]
    for _name, candidate in candidates:
        candidate.normalize()

    indexed = bone_index_map(armature_obj)
    best_quat = candidates[0][1]
    best_error = float("inf")
    for _name, candidate in candidates:
        _write_single_bone_rotation_curves(action, armature_obj, bone_name, loc, candidate, scale, end_frame, indexed, group)
        _refresh_scene_frame(context)
        evaluated_quat = _pose_bone_quaternion(armature_obj.pose.bones[bone_name])
        error = _quat_angular_error(evaluated_quat, target_quat)
        if error < best_error:
            best_error = error
            best_quat = candidate.copy()

    new_bones = _write_single_bone_rotation_curves(action, armature_obj, bone_name, loc, best_quat, scale, end_frame, indexed, group)
    metadata = _metadata_with_bones(metadata, new_bones)
    group = str(group or "FK")
    metadata = _metadata_with_source_transforms(metadata, [{
        "name": bone_name,
        "group": group,
        "location": loc,
        "rotation": (best_quat.w, best_quat.x, best_quat.y, best_quat.z),
        "scale": scale,
        "space": "blender",
    }])
    for item in metadata.get("bones", []) or []:
        if (
            str(item.get("name", "") or "") != bone_name
            or str(item.get("group", "FK") or "FK").upper() != group.upper()
        ):
            continue
        item.pop("sourceRotation", None)
        item.pop("sourceSpace", None)
    _write_pose_key_preview_curves_from_metadata(context, armature_obj, action, metadata)
    _set_pose_key_metadata(action, metadata)
    _set_pose_key_metadata(strip, metadata)
    _refresh_scene_frame(context)
    return metadata


def apply_pose_key_preset_to_strip(
    context,
    armature_obj,
    strip,
    preset,
    *,
    actor="",
    duration=1.0,
    blend_in=0.0,
    blend_out=0.0,
    weight=1.0,
    link_to_dialogset=True,
    event_name="Pose key",
    nla_blend_type='COMBINE',
):
    action = getattr(strip, "action", None)
    if action is None:
        raise ValueError("The active PoseKey strip has no action")
    transforms = transforms_for_preset(preset, armature_obj)
    if not transforms:
        raise ValueError("The selected preset has no transforms for this armature")

    duration_frames = float(getattr(strip, "frame_end", 1.0) - getattr(strip, "frame_start", 0.0))
    if duration_frames <= 0.0:
        duration_frames = seconds_to_frames(context, duration, minimum=1.0)
    indexed = bone_index_map(armature_obj)
    metadata_bones = _write_transform_curves(action, armature_obj, transforms, duration_frames, indexed, replace=True)
    preset_name = str(preset.get("name", "None") or "None")
    metadata = pose_key_metadata_from_strip(strip)
    metadata.update({
        "class": POSEKEY_CLASS_NAME,
        "source": "redPresets",
        "actor": str(actor or metadata.get("actor", "") or actor_name_for_armature(armature_obj)).strip(),
        "eventName": event_name or metadata.get("eventName") or preset_name,
        "duration": float(duration),
        "durationFrames": float(duration_frames),
        "blendIn": float(blend_in),
        "blendOut": float(blend_out),
        "weightBlendType": "IT_Bezier",
        "weight": float(weight),
        "useWeightCurve": False,
        "linkToDialogset": bool(link_to_dialogset),
        "presetName": preset_name,
        "presetVersion": int(preset.get("version", 0) or 0),
        "sourcePath": str(preset.get("sourcePath", "") or ""),
    })
    metadata = _replace_metadata_bones(metadata, metadata_bones)
    metadata["sourceTransforms"] = [
        item
        for item in (_serializable_transform_entry(entry) for entry in transforms)
        if item.get("name")
    ]
    if preset.get("handTracks"):
        metadata["handTracks"] = normalize_hand_tracks(preset.get("handTracks"))
    _write_pose_key_preview_curves_from_metadata(context, armature_obj, action, metadata)
    _set_pose_key_metadata(action, metadata)
    _set_pose_key_metadata(strip, metadata)
    try:
        strip.blend_in = seconds_to_frames(context, blend_in, minimum=0.0)
        strip.blend_out = seconds_to_frames(context, blend_out, minimum=0.0)
        strip.influence = max(0.0, min(1.0, float(weight)))
        if nla_blend_type in {'REPLACE', 'COMBINE', 'ADD', 'SUBTRACT', 'MULTIPLY'}:
            strip.blend_type = nla_blend_type
    except Exception:
        pass
    _refresh_scene_frame(context)
    return strip, action, metadata


def pose_key_xml_from_metadata(metadata):
    bones = list(metadata.get("bones", []) or [])
    fk_count = sum(1 for b in bones if str(b.get("group", "FK")).upper() == "FK")
    hand_count = sum(1 for b in bones if str(b.get("group", "")).upper() == "HAND")
    ik_count = sum(1 for b in bones if str(b.get("group", "")).upper() == "IK")
    cached_count = fk_count + hand_count + ik_count
    raw_hand_tracks = metadata.get("handTracks", metadata.get("editorCachedHandTracks", []))
    hand_track_count = len(list(raw_hand_tracks or []))

    def prop(name, value):
        return f'        <property name="{escape(str(name))}" value="{escape(str(value))}" />'

    serialized_duration = (
        metadata.get("sourceDuration")
        if metadata.get("sourceDuration") is not None
        else metadata.get("duration", 1.0)
    )
    lines = [
        '<?xml version="1.0" encoding="UTF-16"?>',
        f'<CopyToClipboard type="{POSEKEY_CLASS_NAME}">',
        f'    <group name="{POSEKEY_CLASS_NAME}">',
        prop("actor", metadata.get("actor", "")),
        prop("duration", round(float(serialized_duration or 0.0), 6)),
        prop("blendIn", round(float(metadata.get("blendIn", 0.0) or 0.0), 6)),
        prop("blendOut", round(float(metadata.get("blendOut", 0.0) or 0.0), 6)),
        prop("weightBlendType", metadata.get("weightBlendType", "IT_Bezier")),
        prop("weight", round(float(metadata.get("weight", 1.0) or 1.0), 6)),
        prop("useWeightCurve", str(bool(metadata.get("useWeightCurve", False))).lower()),
        prop("weightCurve", "[Empty Array]; CVT_Float; CT_Segmented; false"),
        prop("linkToDialogset", str(bool(metadata.get("linkToDialogset", True))).lower()),
        prop("bones", f"[Array of {fk_count} elements]" if fk_count else "[Empty Array]"),
        prop("bonesHands", f"[Array of {hand_count} elements]" if hand_count else "[Empty Array]"),
        prop("cachedBonesIK", f"[Array of {ik_count} elements]" if ik_count else "[Empty Array]"),
        prop("cachedTransformsIK", f"[Array of {ik_count} elements]" if ik_count else "[Empty Array]"),
        prop("presetName", metadata.get("presetName", "None")),
        prop("presetVersion", int(metadata.get("presetVersion", 0) or 0)),
        prop("cachedBones", f"[Array of {cached_count} elements]" if cached_count else "[Empty Array]"),
        prop("cachedTransforms", f"[Array of {cached_count} elements]" if cached_count else "[Empty Array]"),
        prop("editorCachedHandTracks", f"[Array of {hand_track_count} elements]" if hand_track_count else "[Empty Array]"),
        prop("editorCachedIkEffectorsID", "[Empty Array]"),
        prop("editorCachedIkEffectorsPos", "[Empty Array]"),
        prop("editorCachedIkEffectorsWeight", "[Empty Array]"),
        prop("tracks", "[Empty Array]"),
        prop("cachedTracks", "[Empty Array]"),
        prop("cachedTracksValues", "[Empty Array]"),
        prop("editorCachedMimicSliders", "[Empty Array]"),
        "    </group>",
        "</CopyToClipboard>",
    ]
    return "\n".join(lines)
