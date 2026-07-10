import math

from mathutils import Euler, Matrix, Quaternion, Vector

from ..animation.action_compat import iter_action_fcurves, new_action_fcurve
from ..file_helpers import rm_ns


POSE_TRANSFORM_PROPS = {"location", "rotation_quaternion", "rotation_euler", "scale"}


def pose_bone_data_path(armature_obj, bone_name, prop_name):
    try:
        pose_bone = armature_obj.pose.bones.get(bone_name)
        if pose_bone is not None:
            return pose_bone.path_from_id(prop_name)
    except Exception:
        pass
    safe_bone_name = str(bone_name).replace("\\", "\\\\").replace('"', '\\"')
    return f'pose.bones["{safe_bone_name}"].{prop_name}'


def bone_base_name(name):
    return rm_ns(str(name or "")).strip().lower()


def find_pose_bone_name(armature_obj, *base_names):
    wanted = {bone_base_name(name) for name in base_names}
    pose_bones = getattr(getattr(armature_obj, "pose", None), "bones", None)
    if not pose_bones:
        return None
    for pose_bone in pose_bones:
        if bone_base_name(getattr(pose_bone, "name", "")) in wanted:
            return str(pose_bone.name)
    return None


def parse_pose_bone_transform_fcurve_path(fcurve, prop_names=POSE_TRANSFORM_PROPS):
    data_path = str(getattr(fcurve, "data_path", "") or "")
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
    if prop_name not in prop_names:
        return None
    try:
        array_index = int(getattr(fcurve, "array_index", 0) or 0)
    except Exception:
        array_index = 0
    return bone_name, prop_name, array_index


def new_transform_group(include_euler=True):
    group = {
        "location": {},
        "rotation_quaternion": {},
        "scale": {},
    }
    if include_euler:
        group["rotation_euler"] = {}
    return group


def collect_pose_transform_curves(action, armature_obj, slot, prop_names=POSE_TRANSFORM_PROPS, include_euler=True):
    grouped = {}
    for fcurve in iter_action_fcurves(action, target=armature_obj, slot=slot):
        parsed = parse_pose_bone_transform_fcurve_path(fcurve, prop_names=prop_names)
        if parsed is None:
            continue
        bone_name, prop_name, array_index = parsed
        grouped.setdefault(bone_name, new_transform_group(include_euler=include_euler))[prop_name][array_index] = fcurve
    return grouped


def ensure_pose_transform_fcurves(action, armature_obj, slot, bone_name, prop_name, curves, *, create_if_empty=True, log=None):
    if not curves and not create_if_empty:
        return curves
    component_count = 4 if prop_name in {"rotation_quaternion", "rotation_axis_angle"} else 3
    data_path = pose_bone_data_path(armature_obj, bone_name, prop_name)
    for index in range(component_count):
        if index in curves:
            continue
        try:
            curves[index] = new_action_fcurve(
                action,
                armature_obj,
                data_path=data_path,
                index=index,
                group_name=bone_name,
                slot=slot,
            )
        except Exception:
            if log is not None:
                log.debug("Could not create pose transform fcurve for %s %s[%d]", bone_name, prop_name, index, exc_info=True)
                continue
            raise
    return curves


def keyframe_frames(fcurve):
    try:
        return [float(point.co[0]) for point in fcurve.keyframe_points]
    except Exception:
        return []


def fcurve_has_keys(fcurve):
    try:
        return len(fcurve.keyframe_points) > 0
    except Exception:
        return False


def set_fcurve_value_at_frame(fcurve, frame, value, *, interpolation='LINEAR', update_existing_interpolation=False):
    try:
        for point in fcurve.keyframe_points:
            if abs(float(point.co[0]) - float(frame)) <= 1e-4:
                point.co[1] = float(value)
                if update_existing_interpolation:
                    try:
                        point.interpolation = interpolation
                    except Exception:
                        pass
                return
    except Exception:
        pass
    try:
        point = fcurve.keyframe_points.insert(float(frame), float(value), options={'FAST'})
    except TypeError:
        point = fcurve.keyframe_points.insert(float(frame), float(value))
    try:
        point.interpolation = interpolation
    except Exception:
        pass


def evaluate_fcurve_group(curves, defaults, frame):
    values = list(defaults)
    for index, fcurve in (curves or {}).items():
        if index < 0 or index >= len(values) or not fcurve_has_keys(fcurve):
            continue
        try:
            values[index] = float(fcurve.evaluate(frame))
        except Exception:
            pass
    return tuple(values)


def quat_from_curve_values(values):
    quat = Quaternion(values)
    if math.sqrt(sum(float(value) * float(value) for value in quat)) <= 1e-8:
        return Quaternion((1.0, 0.0, 0.0, 0.0))
    quat.normalize()
    return quat


def transform_matrix_from_curve_groups(curves_by_prop, frame):
    loc = Vector(evaluate_fcurve_group(curves_by_prop.get("location", {}), (0.0, 0.0, 0.0), frame))
    if curves_by_prop.get("rotation_quaternion"):
        rot = quat_from_curve_values(
            evaluate_fcurve_group(curves_by_prop.get("rotation_quaternion", {}), (1.0, 0.0, 0.0, 0.0), frame)
        )
    else:
        rot = Euler(
            evaluate_fcurve_group(curves_by_prop.get("rotation_euler", {}), (0.0, 0.0, 0.0), frame),
            "XYZ",
        ).to_quaternion()
    scale = Vector(evaluate_fcurve_group(curves_by_prop.get("scale", {}), (1.0, 1.0, 1.0), frame))
    return Matrix.Translation(loc) @ rot.to_matrix().to_4x4() @ Matrix.Diagonal((scale.x, scale.y, scale.z, 1.0))


def set_transform_group_values(curves_by_prop, frame, loc=None, quat_rot=None, euler_rot=None, scale=None):
    if loc is not None:
        for index, value in enumerate(loc):
            fcurve = curves_by_prop["location"].get(index)
            if fcurve is not None:
                set_fcurve_value_at_frame(fcurve, frame, float(value))
    if quat_rot is not None:
        for index, value in enumerate((quat_rot.w, quat_rot.x, quat_rot.y, quat_rot.z)):
            fcurve = curves_by_prop["rotation_quaternion"].get(index)
            if fcurve is not None:
                set_fcurve_value_at_frame(fcurve, frame, float(value))
    if euler_rot is not None:
        for index, value in enumerate(euler_rot):
            fcurve = curves_by_prop["rotation_euler"].get(index)
            if fcurve is not None:
                set_fcurve_value_at_frame(fcurve, frame, float(value))
    if scale is not None:
        for index, value in enumerate(scale):
            fcurve = curves_by_prop["scale"].get(index)
            if fcurve is not None:
                set_fcurve_value_at_frame(fcurve, frame, float(value))
