"""Blender runtime glue for breast dangle armatures."""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

import bpy
from bpy.app.handlers import persistent
from mathutils import Matrix, Vector

from . import presets as physics_presets
from . import runtime as physics_runtime
from . import dyng_blender
from .breast import (
    BREAST_BONE_NAMES,
    BREAST_PRESETS,
    CUSTOM_PRESET_NAME,
    BreastSettings,
    BreastSimulator,
    REDKIT_BREAST_PRESETS,
)
from .dyng import matrix_mul

log = logging.getLogger(__name__)

BREAST_ENABLED_PROP = "witcher_breast_sim_enabled"
BREAST_RUNTIME_OPT_IN_PROP = "witcher_breast_runtime_opt_in"
BREAST_LAST_FRAME_PROP = "witcher_breast_last_frame"
BREAST_LAST_STEP_PROP = "witcher_breast_last_step"
BREAST_SIM_STATUS_PROP = "witcher_breast_sim_status"
BREAST_PRESET_PROP = "witcher_breast_preset"
BREAST_SIM_TIME_PROP = "witcher_breast_sim_time"
BREAST_ELLIPSE_PROP = "witcher_breast_ellipse"
BREAST_VEL_DAMP_PROP = "witcher_breast_vel_damp"
BREAST_BOUNCE_DAMP_PROP = "witcher_breast_bounce_damp"
BREAST_IN_ACC_PROP = "witcher_breast_in_acc"
BREAST_INERTIA_SCALER_PROP = "witcher_breast_inertia_scaler"
BREAST_BLACK_HOLE_PROP = "witcher_breast_black_hole"
BREAST_VEL_CLAMP_PROP = "witcher_breast_vel_clamp"
BREAST_GRAVITY_PROP = "witcher_breast_gravity"
BREAST_MOVEMENT_WEIGHT_PROP = "witcher_breast_movement_weight"
BREAST_ROTATION_WEIGHT_PROP = "witcher_breast_rotation_weight"
BREAST_START_OFFSET_PROP = "witcher_breast_start_offset"
BREAST_BLEND_PROP = "witcher_breast_blend"
BREAST_CUSTOM_SETTINGS_PROP = "witcher_breast_custom_settings"

_SETTING_PROPS: Tuple[Tuple[str, str], ...] = (
    (BREAST_PRESET_PROP, "preset"),
    (BREAST_SIM_TIME_PROP, "simTime"),
    (BREAST_ELLIPSE_PROP, "ellipse"),
    (BREAST_VEL_DAMP_PROP, "velDamp"),
    (BREAST_BOUNCE_DAMP_PROP, "bounceDamp"),
    (BREAST_IN_ACC_PROP, "inAcc"),
    (BREAST_INERTIA_SCALER_PROP, "inertiaScaler"),
    (BREAST_BLACK_HOLE_PROP, "blackHole"),
    (BREAST_VEL_CLAMP_PROP, "velClamp"),
    (BREAST_GRAVITY_PROP, "gravity"),
    (BREAST_MOVEMENT_WEIGHT_PROP, "movementBoneWeight"),
    (BREAST_ROTATION_WEIGHT_PROP, "rotationBoneWeight"),
    (BREAST_START_OFFSET_PROP, "startSimPointOffset"),
    (BREAST_BLEND_PROP, "blend"),
)


_LEGACY_CONSTRUCTOR_DEFAULTS = {
    BREAST_PRESET_PROP: "",
    BREAST_SIM_TIME_PROP: 1.0,
    BREAST_ELLIPSE_PROP: (0.0, 0.0, 0.3, 0.3),
    BREAST_VEL_DAMP_PROP: 1.0,
    BREAST_BOUNCE_DAMP_PROP: 1.0,
    BREAST_IN_ACC_PROP: 1.0,
    BREAST_INERTIA_SCALER_PROP: 1.0,
    BREAST_BLACK_HOLE_PROP: 1.0,
    BREAST_VEL_CLAMP_PROP: 4.0,
    BREAST_GRAVITY_PROP: -0.327,
    BREAST_MOVEMENT_WEIGHT_PROP: 1.0,
    BREAST_ROTATION_WEIGHT_PROP: 1.0,
    BREAST_START_OFFSET_PROP: 0.0,
    BREAST_BLEND_PROP: 1.0,
}


@dataclass
class _BreastObjectState:
    simulator: BreastSimulator
    settings_key: Tuple
    startup_remaining: int = 0


_STATES: Dict[str, _BreastObjectState] = {}
_RUNTIME_OBJECT_NAMES: Set[str] = set()
_LAST_FRAMES: Dict[str, int] = {}
_SUPPRESS_FRAME_HANDLER = False
_BREAST_STARTUP_BLEND_FRAMES = 12
_BREAST_MAX_ADVANCE_FRAMES = 4
_BREAST_RESET_FRAME_GAP = 12
_BREAST_SHEAR_TO_ROTATION_FACTOR = 0.5
_ROT90_TO_GAME = Matrix.Rotation(math.radians(90.0), 4, 'Z')
_ROT90_FROM_GAME = Matrix.Rotation(math.radians(-90.0), 4, 'Z')
def _state_key(obj: bpy.types.Object) -> str:
    return physics_runtime.state_key(obj)


def is_breast_armature(obj: Optional[bpy.types.Object]) -> bool:
    if obj is None or getattr(obj, "type", None) != "ARMATURE":
        return False
    return str(obj.get("witcher_type", "")) == "CAnimDangleConstraint_Breast"


def _has_breast_bones(obj: bpy.types.Object) -> bool:
    return all(obj.pose.bones.get(name) is not None for name in BREAST_BONE_NAMES)


def ensure_default_props(obj: bpy.types.Object, *, enabled: Optional[bool] = None) -> None:
    settings = BreastSettings.from_mapping({})
    defaults = settings.to_dict()
    prop_defaults = {
        BREAST_ENABLED_PROP: False,
        BREAST_RUNTIME_OPT_IN_PROP: False,
        BREAST_PRESET_PROP: defaults["preset"],
        BREAST_SIM_TIME_PROP: defaults["simTime"],
        BREAST_ELLIPSE_PROP: defaults["ellipse"],
        BREAST_VEL_DAMP_PROP: defaults["velDamp"],
        BREAST_BOUNCE_DAMP_PROP: defaults["bounceDamp"],
        BREAST_IN_ACC_PROP: defaults["inAcc"],
        BREAST_INERTIA_SCALER_PROP: defaults["inertiaScaler"],
        BREAST_BLACK_HOLE_PROP: defaults["blackHole"],
        BREAST_VEL_CLAMP_PROP: defaults["velClamp"],
        BREAST_GRAVITY_PROP: defaults["gravity"],
        BREAST_MOVEMENT_WEIGHT_PROP: defaults["movementBoneWeight"],
        BREAST_ROTATION_WEIGHT_PROP: defaults["rotationBoneWeight"],
        BREAST_START_OFFSET_PROP: defaults["startSimPointOffset"],
        BREAST_BLEND_PROP: 1.0,
    }
    for key, value in prop_defaults.items():
        if key not in obj:
            obj[key] = value
    if _has_legacy_constructor_defaults(obj):
        _apply_settings_props(obj, settings)
    if _is_custom_preset_value(obj.get(BREAST_PRESET_PROP)) and BREAST_CUSTOM_SETTINGS_PROP not in obj:
        store_custom_preset(obj)
    if enabled is not None:
        obj[BREAST_ENABLED_PROP] = bool(enabled)
        obj[BREAST_RUNTIME_OPT_IN_PROP] = bool(enabled)


def _has_breast_runtime_opt_in_props(obj: Optional[bpy.types.Object]) -> bool:
    if obj is None:
        return False
    return bool(obj.get(BREAST_ENABLED_PROP, False)) and bool(obj.get(BREAST_RUNTIME_OPT_IN_PROP, False))


def is_breast_runtime_enabled(
    obj: Optional[bpy.types.Object],
    scene: Optional[bpy.types.Scene] = None,
) -> bool:
    if not dyng_blender.live_preview_enabled(scene):
        return False
    return _has_breast_runtime_opt_in_props(obj)


def _apply_settings_props(obj: bpy.types.Object, settings: BreastSettings) -> None:
    values = settings.to_dict()
    prop_values = {
        BREAST_PRESET_PROP: values["preset"],
        BREAST_SIM_TIME_PROP: values["simTime"],
        BREAST_ELLIPSE_PROP: values["ellipse"],
        BREAST_VEL_DAMP_PROP: values["velDamp"],
        BREAST_BOUNCE_DAMP_PROP: values["bounceDamp"],
        BREAST_IN_ACC_PROP: values["inAcc"],
        BREAST_INERTIA_SCALER_PROP: values["inertiaScaler"],
        BREAST_BLACK_HOLE_PROP: values["blackHole"],
        BREAST_VEL_CLAMP_PROP: values["velClamp"],
        BREAST_GRAVITY_PROP: values["gravity"],
        BREAST_MOVEMENT_WEIGHT_PROP: values["movementBoneWeight"],
        BREAST_ROTATION_WEIGHT_PROP: values["rotationBoneWeight"],
        BREAST_START_OFFSET_PROP: values["startSimPointOffset"],
        BREAST_BLEND_PROP: values["blend"],
    }
    for key, value in prop_values.items():
        obj[key] = value


def _is_custom_preset_value(value) -> bool:
    return str(value or "").strip().lower() == CUSTOM_PRESET_NAME.lower()


def _reserved_preset_names_lower() -> Set[str]:
    names = {CUSTOM_PRESET_NAME.lower()}
    names.update(name.lower() for name in redkit_preset_names())
    return names


def store_custom_preset(obj: bpy.types.Object, *, overwrite: bool = False) -> bool:
    if not overwrite and BREAST_CUSTOM_SETTINGS_PROP in obj:
        return False
    values = _settings_from_object(obj).to_dict()
    values["preset"] = CUSTOM_PRESET_NAME
    obj[BREAST_CUSTOM_SETTINGS_PROP] = json.dumps(values, separators=(",", ":"), sort_keys=True)
    return True


def _custom_preset_settings(obj: bpy.types.Object) -> Optional[BreastSettings]:
    raw = obj.get(BREAST_CUSTOM_SETTINGS_PROP)
    if not raw:
        return None
    try:
        data = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except Exception:
        return None
    if not isinstance(data, Mapping):
        return None
    values = dict(data)
    values["preset"] = CUSTOM_PRESET_NAME
    return BreastSettings.from_mapping(values)


def redkit_preset_names() -> Tuple[str, ...]:
    return tuple(preset.name for preset in REDKIT_BREAST_PRESETS)


def saved_user_preset_names() -> Tuple[str, ...]:
    return physics_presets.saved_preset_names("breast")


def user_preset_names() -> Tuple[str, ...]:
    return saved_user_preset_names()


def available_preset_names() -> Tuple[str, ...]:
    names = [CUSTOM_PRESET_NAME]
    names.extend(preset.name for preset in BREAST_PRESETS)
    names.extend(name for name in saved_user_preset_names() if name not in names)
    return tuple(names)


def is_user_preset(preset_name: str) -> bool:
    return str(preset_name or "") in user_preset_names()


def apply_user_preset(obj: bpy.types.Object, preset_name: str) -> bool:
    if not is_breast_armature(obj):
        return False
    name = physics_presets.normalize_preset_name(preset_name)
    saved = physics_presets.get_preset("breast", name)
    if saved is None:
        return False
    ensure_default_props(obj)
    values = dict(saved)
    values["preset"] = name
    settings = BreastSettings.from_mapping(values)
    _apply_settings_props(obj, settings)
    clear_state(obj)
    obj[BREAST_SIM_STATUS_PROP] = f"Loaded Breast Blender preset {name}"
    return True


def save_user_preset(obj: bpy.types.Object, preset_name: str) -> str:
    if not is_breast_armature(obj):
        raise ValueError("Object is not a Breast physics armature")
    name = physics_presets.normalize_preset_name(preset_name)
    if not name:
        raise ValueError("Preset name is empty")
    if name.lower() in _reserved_preset_names_lower():
        raise ValueError("Reserved Breast preset names are read-only")
    ensure_default_props(obj)
    values = _settings_from_object(obj).to_dict()
    values["preset"] = name
    saved_name = physics_presets.save_preset("breast", name, values)
    _apply_settings_props(obj, BreastSettings.from_mapping(values))
    clear_state(obj)
    obj[BREAST_SIM_STATUS_PROP] = f"Saved Breast Blender preset {saved_name}"
    return saved_name


def delete_user_preset(preset_name: str) -> bool:
    return physics_presets.delete_preset("breast", preset_name)


def apply_preset(obj: bpy.types.Object, preset_name: str) -> bool:
    if not is_breast_armature(obj):
        return False
    name = physics_presets.normalize_preset_name(preset_name)
    if _is_custom_preset_value(name):
        ensure_default_props(obj)
        settings = _custom_preset_settings(obj) or BreastSettings.from_mapping({})
        _apply_settings_props(obj, settings)
        clear_state(obj)
        obj[BREAST_SIM_STATUS_PROP] = "Loaded Breast preset CUSTOM_PRESET"
        return True
    redkit_names = {preset_name.lower(): preset_name for preset_name in redkit_preset_names()}
    redkit_name = redkit_names.get(name.lower())
    if redkit_name is None:
        return apply_user_preset(obj, name)
    ensure_default_props(obj)
    current_blend = _float_prop(obj, BREAST_BLEND_PROP, BreastSettings.from_mapping({}).blend)
    settings = BreastSettings.from_mapping({"preset": redkit_name, "blend": current_blend})
    _apply_settings_props(obj, settings)
    clear_state(obj)
    obj[BREAST_SIM_STATUS_PROP] = f"Loaded Breast preset {redkit_name}"
    return True


def _prop_matches(value, expected, epsilon: float = 1e-5) -> bool:
    if isinstance(expected, (tuple, list)):
        try:
            values = list(value)
        except TypeError:
            return False
        if len(values) < len(expected):
            return False
        return all(abs(float(values[i]) - float(expected[i])) <= epsilon for i in range(len(expected)))
    if isinstance(expected, str):
        return str(value or "") == expected
    try:
        return abs(float(value) - float(expected)) <= epsilon
    except (TypeError, ValueError):
        return False


def _has_legacy_constructor_defaults(obj: bpy.types.Object) -> bool:
    if str(obj.get("witcher_type", "")) != "CAnimDangleConstraint_Breast":
        return False
    if BREAST_SIM_TIME_PROP not in obj:
        return False
    return all(
        _prop_matches(obj.get(key), expected)
        for key, expected in _LEGACY_CONSTRUCTOR_DEFAULTS.items()
    )


def configure_imported_breast(obj: bpy.types.Object, *, enabled: bool = True) -> None:
    if not is_breast_armature(obj):
        return
    clear_state(obj)
    ensure_default_props(obj, enabled=enabled)
    store_custom_preset(obj, overwrite=True)
    if enabled and _has_breast_bones(obj):
        dyng_blender.set_live_preview_enabled(enabled=True)
        _RUNTIME_OBJECT_NAMES.add(_state_key(obj))
        ensure_frame_handler()
        obj[BREAST_SIM_STATUS_PROP] = "Breast runtime enabled on import"
    elif _has_breast_bones(obj):
        obj[BREAST_SIM_STATUS_PROP] = "Breast runtime ready"
    else:
        obj[BREAST_SIM_STATUS_PROP] = "Missing l_boob/r_boob"


def _float_prop(obj: bpy.types.Object, key: str, default: float) -> float:
    try:
        return float(obj.get(key, default))
    except (TypeError, ValueError):
        return float(default)


def _settings_from_object(obj: bpy.types.Object) -> BreastSettings:
    mapping = {}
    for prop_name, setting_key in _SETTING_PROPS:
        if prop_name in obj:
            mapping[setting_key] = obj[prop_name]
    return BreastSettings.from_mapping(mapping)


def _settings_key(obj: bpy.types.Object) -> Tuple:
    return _settings_from_object(obj).as_tuple() + (_rig_uses_rot90(obj),)


def _rig_uses_rot90(obj: Optional[bpy.types.Object]) -> bool:
    return physics_runtime.rig_uses_rot90(obj)


def _matrix_to_solver_space(obj: bpy.types.Object, matrix: Matrix) -> Matrix:
    if not _rig_uses_rot90(obj):
        return matrix.copy()
    return matrix @ _ROT90_TO_GAME


def _matrix_from_solver_space(obj: bpy.types.Object, matrix: Matrix) -> Matrix:
    if not _rig_uses_rot90(obj):
        return matrix.copy()
    return matrix @ _ROT90_FROM_GAME


def _transform_from_blender_matrix(matrix: Matrix):
    x_axis = (float(matrix[0][0]), float(matrix[1][0]), float(matrix[2][0]))
    y_axis = (float(matrix[0][1]), float(matrix[1][1]), float(matrix[2][1]))
    z_axis = (float(matrix[0][2]), float(matrix[1][2]), float(matrix[2][2]))
    position = (float(matrix[0][3]), float(matrix[1][3]), float(matrix[2][3]))
    return (x_axis + (0.0,), y_axis + (0.0,), z_axis + (0.0,), position + (1.0,))


def _blender_matrix_from_transform(transform, obj: Optional[bpy.types.Object] = None) -> Matrix:
    x_axis = transform[0]
    y_axis = transform[1]
    z_axis = transform[2]
    position = transform[3]
    matrix = Matrix(
        (
            (x_axis[0], y_axis[0], z_axis[0], position[0]),
            (x_axis[1], y_axis[1], z_axis[1], position[1]),
            (x_axis[2], y_axis[2], z_axis[2], position[2]),
            (0.0, 0.0, 0.0, 1.0),
        )
    )
    if obj is not None:
        return _matrix_from_solver_space(obj, matrix)
    return matrix


def _blend_matrix(target_matrix: Matrix, simulated_matrix: Matrix, weight: float) -> Matrix:
    return dyng_blender._blend_matrix(target_matrix, simulated_matrix, max(0.0, min(1.0, float(weight))))


def _target_parent_matrix(pose_bone: bpy.types.PoseBone) -> Matrix:
    parent = pose_bone.parent
    if parent is None:
        return Matrix.Identity(4)
    if dyng_blender._pose_bone_is_externally_driven(parent):
        matrix = dyng_blender._external_copy_transform_matrix(parent)
        if matrix is not None:
            return matrix
    return parent.matrix.copy()


def _rest_local_matrix(obj: bpy.types.Object, bone_name: str) -> Matrix:
    return dyng_blender._bone_rest_local_matrix(obj, bone_name)


def _solver_rest_local_matrix(obj: bpy.types.Object, bone_name: str) -> Matrix:
    bone = obj.data.bones.get(bone_name)
    if bone is None:
        return Matrix.Identity(4)
    child_matrix = _matrix_to_solver_space(obj, bone.matrix_local.copy())
    if bone.parent is None:
        return child_matrix
    parent_matrix = _matrix_to_solver_space(obj, bone.parent.matrix_local.copy())
    return parent_matrix.inverted_safe() @ child_matrix


def _target_bone_matrix(obj: bpy.types.Object, bone_name: str) -> Optional[Matrix]:
    pose_bone = obj.pose.bones.get(bone_name)
    if pose_bone is None:
        return None
    return _target_parent_matrix(pose_bone) @ _rest_local_matrix(obj, bone_name)


def _local_transforms(obj: bpy.types.Object) -> Mapping[str, Tuple[Tuple[float, ...], ...]]:
    return {
        name: _transform_from_blender_matrix(_solver_rest_local_matrix(obj, name))
        for name in BREAST_BONE_NAMES
    }


def _parent_transforms(obj: bpy.types.Object) -> Mapping[str, Tuple[Tuple[float, ...], ...]]:
    transforms = {}
    for name in BREAST_BONE_NAMES:
        pose_bone = obj.pose.bones.get(name)
        if pose_bone is None:
            continue
        transforms[name] = _transform_from_blender_matrix(_matrix_to_solver_space(obj, _target_parent_matrix(pose_bone)))
    return transforms


def _get_state(
    obj: bpy.types.Object,
    *,
    reset: bool = False,
    parent_transforms: Optional[Mapping[str, Tuple[Tuple[float, ...], ...]]] = None,
) -> _BreastObjectState:
    key = _state_key(obj)
    settings = _settings_from_object(obj)
    settings_key = _settings_key(obj)
    state = _STATES.get(key)
    if reset or state is None or state.settings_key != settings_key:
        simulator = BreastSimulator(_local_transforms(obj), settings)
        state = _BreastObjectState(
            simulator=simulator,
            settings_key=settings_key,
            startup_remaining=_BREAST_STARTUP_BLEND_FRAMES,
        )
        _STATES[key] = state
        if parent_transforms is not None:
            state.simulator.reset(parent_transforms)
    else:
        state.simulator.set_settings(settings)
    return state


def _write_external_copy_targets(
    obj: bpy.types.Object,
    pose_bone: bpy.types.PoseBone,
    desired_matrix: Matrix,
) -> int:
    desired_world = obj.matrix_world @ desired_matrix
    updated = 0
    for constraint in dyng_blender._external_copy_transform_constraints(pose_bone):
        target = getattr(constraint, "target", None)
        subtarget = str(getattr(constraint, "subtarget", "") or "")
        if target is None or getattr(target, "type", "") != "ARMATURE" or not subtarget:
            continue
        target_bone = target.pose.bones.get(subtarget)
        if target_bone is None:
            continue
        target_matrix = target.matrix_world.inverted_safe() @ desired_world
        parent_matrix = target_bone.parent.matrix.copy() if target_bone.parent is not None else None
        dyng_blender._set_pose_bone_matrix_basis(target, target_bone, target_matrix, parent_matrix)
        target.update_tag()
        updated += 1
    return updated


def _without_blender_shear(target_matrix: Matrix, simulated_matrix: Matrix) -> Matrix:
    try:
        loc = simulated_matrix.translation.copy()
        scale = target_matrix.to_scale()

        y_axis = Vector((simulated_matrix[0][1], simulated_matrix[1][1], simulated_matrix[2][1]))
        if y_axis.length <= 1e-8:
            y_axis = Vector((target_matrix[0][1], target_matrix[1][1], target_matrix[2][1]))
        y_axis.normalize()

        ref_z = Vector((target_matrix[0][2], target_matrix[1][2], target_matrix[2][2]))
        if ref_z.length <= 1e-8:
            ref_z = Vector((0.0, 0.0, 1.0))
        ref_z.normalize()

        x_axis = y_axis.cross(ref_z)
        if x_axis.length <= 1e-8:
            ref_x = Vector((target_matrix[0][0], target_matrix[1][0], target_matrix[2][0]))
            if ref_x.length <= 1e-8:
                ref_x = Vector((1.0, 0.0, 0.0))
            ref_x.normalize()
            z_axis = ref_x.cross(y_axis)
            if z_axis.length <= 1e-8:
                return Matrix.LocRotScale(loc, target_matrix.to_quaternion(), scale)
            z_axis.normalize()
            x_axis = y_axis.cross(z_axis)
        x_axis.normalize()

        z_axis = x_axis.cross(y_axis)
        if z_axis.length <= 1e-8:
            return Matrix.LocRotScale(loc, target_matrix.to_quaternion(), scale)
        z_axis.normalize()

        full_swing = Matrix(
            (
                (x_axis.x * scale.x, y_axis.x * scale.y, z_axis.x * scale.z, loc.x),
                (x_axis.y * scale.x, y_axis.y * scale.y, z_axis.y * scale.z, loc.y),
                (x_axis.z * scale.x, y_axis.z * scale.y, z_axis.z * scale.z, loc.z),
                (0.0, 0.0, 0.0, 1.0),
            )
        )
        rotation = target_matrix.to_quaternion().slerp(
            full_swing.to_quaternion(),
            _BREAST_SHEAR_TO_ROTATION_FACTOR,
        )
        return Matrix.LocRotScale(loc, rotation, scale)
    except Exception:
        result = target_matrix.copy()
        result.translation = simulated_matrix.translation
        return result


def _write_bone_matrices(obj: bpy.types.Object, matrices: Mapping[str, Matrix], *, blend: float) -> int:
    applied: Dict[str, Matrix] = {}
    updated = 0
    for name in BREAST_BONE_NAMES:
        pose_bone = obj.pose.bones.get(name)
        desired = matrices.get(name)
        if pose_bone is None or desired is None:
            continue
        target = _target_bone_matrix(obj, name)
        if target is not None:
            desired = _without_blender_shear(target, desired)
            desired = _blend_matrix(target, desired, blend)
        dyng_blender._mute_external_copy_transforms(pose_bone)
        parent_matrix = None
        if pose_bone.parent is not None:
            parent_matrix = applied.get(pose_bone.parent.name, pose_bone.parent.matrix.copy())
        dyng_blender._set_pose_bone_matrix_basis(obj, pose_bone, desired, parent_matrix)
        _write_external_copy_targets(obj, pose_bone, desired)
        applied[name] = desired.copy()
        updated += 1
    if updated:
        obj.update_tag()
    return updated


def _target_bone_matrices(obj: bpy.types.Object) -> Dict[str, Matrix]:
    targets = {}
    for name in BREAST_BONE_NAMES:
        target = _target_bone_matrix(obj, name)
        if target is not None:
            targets[name] = target
    return targets


def ellipse_preview_guides(
    obj: bpy.types.Object,
    *,
    segments: int = 64,
    display_offset: float = 0.08,
) -> Dict[str, Dict[str, object]]:
    """Return viewport-only elA guide points for each Breast simulation bone."""

    if not is_breast_armature(obj) or not _has_breast_bones(obj):
        return {}
    settings = _settings_from_object(obj)
    try:
        center_x, center_y, radius_x, radius_y = (float(value) for value in settings.ellipse)
    except (TypeError, ValueError):
        return {}
    if abs(radius_x) <= 1e-8 or abs(radius_y) <= 1e-8:
        return {}

    parent_transforms = _parent_transforms(obj)
    count = max(12, int(segments or 64))
    offset = float(display_offset or 0.0)
    result: Dict[str, Dict[str, object]] = {}

    def local_preview_point(point_x: float, point_y: float) -> Vector:
        return Vector((point_y, offset, point_x))

    for name in BREAST_BONE_NAMES:
        pose_bone = obj.pose.bones.get(name)
        parent_transform = parent_transforms.get(name)
        if pose_bone is None or parent_transform is None:
            continue
        local_transform = _transform_from_blender_matrix(_solver_rest_local_matrix(obj, name))
        preview_matrix = _blender_matrix_from_transform(matrix_mul(local_transform, parent_transform), obj)
        world_matrix = obj.matrix_world @ preview_matrix
        points: List[Tuple[float, float, float]] = []
        for index in range(count + 1):
            angle = (math.tau * index) / count
            local_point = Vector(
                (
                    center_y + math.sin(angle) * radius_y,
                    offset,
                    center_x + math.cos(angle) * radius_x,
                )
            )
            point = world_matrix @ local_point
            points.append((float(point.x), float(point.y), float(point.z)))
        center = world_matrix @ local_preview_point(center_x, center_y)
        start = world_matrix @ local_preview_point(center_x, center_y - settings.start_sim_point_offset)
        bone = obj.matrix_world @ pose_bone.head
        result[name] = {
            "ellipse": points,
            "center": (float(center.x), float(center.y), float(center.z)),
            "start": (float(start.x), float(start.y), float(start.z)),
            "bone": (float(bone.x), float(bone.y), float(bone.z)),
            "distance": float((center - bone).length),
        }
    return result


def ellipse_preview_lines(
    obj: bpy.types.Object,
    *,
    segments: int = 64,
    display_offset: float = 0.08,
) -> Dict[str, List[Tuple[float, float, float]]]:
    """Return viewport-only elA guide ellipse points for each Breast simulation bone."""

    guides = ellipse_preview_guides(obj, segments=segments, display_offset=display_offset)
    return {
        name: list(data.get("ellipse", []))
        for name, data in guides.items()
        if isinstance(data.get("ellipse"), list)
    }


def _prime_object(obj: bpy.types.Object, *, status: str, update_status: bool = True) -> bool:
    ensure_default_props(obj)
    parents = _parent_transforms(obj)
    state = _get_state(obj, reset=True, parent_transforms=parents)
    state.startup_remaining = _BREAST_STARTUP_BLEND_FRAMES
    updated = _write_bone_matrices(obj, _target_bone_matrices(obj), blend=0.0)
    if update_status:
        obj[BREAST_LAST_STEP_PROP] = 0.0
        obj[BREAST_SIM_STATUS_PROP] = status
    return updated > 0


def clear_state(obj: Optional[bpy.types.Object] = None) -> None:
    if obj is None:
        _STATES.clear()
        _LAST_FRAMES.clear()
    else:
        key = _state_key(obj)
        _STATES.pop(key, None)
        _LAST_FRAMES.pop(key, None)


def find_breast_armature(context: bpy.types.Context) -> Optional[bpy.types.Object]:
    return physics_runtime.find_armature(context, is_breast_armature)


def breast_objects_for_character(root: Optional[bpy.types.Object]) -> List[bpy.types.Object]:
    return physics_runtime.objects_for_character(
        root,
        bpy.data.objects,
        is_runtime_kind=is_breast_armature,
        is_descendant_of=dyng_blender._is_descendant_of,
    )


def breast_objects_for_context(context: bpy.types.Context) -> List[bpy.types.Object]:
    return physics_runtime.objects_for_context(
        context,
        find_root=dyng_blender.find_character_root,
        find_armature_fn=find_breast_armature,
        objects_for_character_fn=breast_objects_for_character,
    )


def enable_breast_object(obj: bpy.types.Object, enabled: bool) -> bool:
    if not is_breast_armature(obj):
        return False
    if enabled and not dyng_blender.live_preview_enabled(getattr(bpy.context, "scene", None)):
        ensure_default_props(obj, enabled=False)
        _RUNTIME_OBJECT_NAMES.discard(_state_key(obj))
        remove_frame_handler()
        obj[BREAST_SIM_STATUS_PROP] = "Live preview is off"
        return False
    ensure_default_props(obj, enabled=enabled)
    if not _has_breast_bones(obj):
        obj[BREAST_SIM_STATUS_PROP] = "Missing l_boob/r_boob"
        return False
    clear_state(obj)
    if enabled:
        _RUNTIME_OBJECT_NAMES.add(_state_key(obj))
        ensure_frame_handler()
        obj[BREAST_SIM_STATUS_PROP] = "Breast runtime enabled"
    else:
        _RUNTIME_OBJECT_NAMES.discard(_state_key(obj))
        dyng_blender._restore_external_constraints(obj)
        obj[BREAST_SIM_STATUS_PROP] = "Breast runtime disabled"
        if not _RUNTIME_OBJECT_NAMES and not enabled_breast_objects(bpy.context.scene):
            remove_frame_handler()
    return True


def enable_breast_objects(objects: Sequence[bpy.types.Object], enabled: bool) -> int:
    return physics_runtime.enable_objects(objects, enabled, enable_breast_object)


def restore_import_default_runtime(scene: Optional[bpy.types.Scene] = None) -> int:
    """Re-enable breast physics armatures imported while runtime defaults were temporarily off."""

    restored = 0
    for obj in getattr(bpy.data, "objects", []) or []:
        if not is_breast_armature(obj) or not _has_breast_bones(obj):
            continue
        status = str(obj.get(BREAST_SIM_STATUS_PROP, "") or "")
        missing_flags = BREAST_ENABLED_PROP not in obj or BREAST_RUNTIME_OPT_IN_PROP not in obj
        temporarily_ready = status in {"Breast runtime ready", "Live preview is off"}
        if not missing_flags and not temporarily_ready:
            continue
        ensure_default_props(obj, enabled=True)
        obj[BREAST_SIM_STATUS_PROP] = "Breast runtime enabled"
        _RUNTIME_OBJECT_NAMES.add(_state_key(obj))
        restored += 1
    if restored:
        dyng_blender.set_live_preview_enabled(scene, True)
        ensure_frame_handler()
    return restored


def step_object(
    obj: bpy.types.Object,
    dt: float,
    *,
    reset: bool = False,
    relaxed: bool = False,
    update_status: bool = True,
) -> bool:
    if not is_breast_armature(obj) or not _has_breast_bones(obj):
        return False
    ensure_default_props(obj)
    parents = _parent_transforms(obj)
    if reset:
        return _prime_object(obj, status="Breast simulation primed", update_status=update_status)
    state = _get_state(obj, parent_transforms=parents)
    outputs = state.simulator.step(parents, dt, reset=False, relaxed=relaxed)
    desired = {name: _blender_matrix_from_transform(transform, obj) for name, transform in outputs.items()}
    blend = _float_prop(obj, BREAST_BLEND_PROP, 1.0)
    if state.startup_remaining > 0:
        total = max(1, _BREAST_STARTUP_BLEND_FRAMES)
        blend *= (total - state.startup_remaining + 1) / total
        state.startup_remaining -= 1
    updated = _write_bone_matrices(obj, desired, blend=blend)
    if update_status:
        obj[BREAST_LAST_STEP_PROP] = float(dt)
        obj[BREAST_SIM_STATUS_PROP] = f"Updated {updated} breast bones"
    return updated > 0


def reset_object(obj: bpy.types.Object, *, update_status: bool = True) -> bool:
    if not is_breast_armature(obj) or not _has_breast_bones(obj):
        return False
    return _prime_object(obj, status="Breast reset", update_status=update_status)


def enabled_breast_objects(scene: bpy.types.Scene) -> List[bpy.types.Object]:
    return physics_runtime.enabled_runtime_objects(
        scene,
        getattr(bpy.data, "objects", []),
        live_preview_enabled=dyng_blender.live_preview_enabled,
        is_runtime_kind=is_breast_armature,
        is_runtime_enabled=is_breast_runtime_enabled,
    )


def _refresh_runtime_object_names(scene: Optional[bpy.types.Scene] = None) -> None:
    physics_runtime.refresh_runtime_object_names(
        scene,
        _RUNTIME_OBJECT_NAMES,
        getattr(bpy.data, "objects", []),
        live_preview_enabled=dyng_blender.live_preview_enabled,
        is_runtime_kind=is_breast_armature,
        has_runtime_opt_in=_has_breast_runtime_opt_in_props,
        key_fn=_state_key,
    )


def _object_by_runtime_name(name: str) -> Optional[bpy.types.Object]:
    return physics_runtime.object_by_runtime_name(
        name,
        getattr(bpy.data, "objects", []),
        key_fn=_state_key,
    )


def _runtime_breast_objects(scene: bpy.types.Scene) -> List[bpy.types.Object]:
    return physics_runtime.runtime_objects(
        scene,
        _RUNTIME_OBJECT_NAMES,
        getattr(bpy.data, "objects", []),
        live_preview_enabled=dyng_blender.live_preview_enabled,
        is_runtime_kind=is_breast_armature,
        has_runtime_opt_in=_has_breast_runtime_opt_in_props,
        is_runtime_enabled=is_breast_runtime_enabled,
        key_fn=_state_key,
    )


def frame_dt(scene: bpy.types.Scene, obj: bpy.types.Object) -> Tuple[float, bool]:
    return physics_runtime.frame_dt(
        scene,
        obj,
        _LAST_FRAMES,
        reset_frame_gap=_BREAST_RESET_FRAME_GAP,
        max_advance_frames=_BREAST_MAX_ADVANCE_FRAMES,
        key_fn=_state_key,
    )


def _is_rest_frame(scene: bpy.types.Scene) -> bool:
    return physics_runtime.is_rest_frame(scene)


@persistent
def breast_frame_change_post(scene: bpy.types.Scene) -> None:
    if _SUPPRESS_FRAME_HANDLER:
        return
    objects = _runtime_breast_objects(scene)
    if not objects:
        remove_frame_handler()
        return
    for obj in objects:
        try:
            current = int(scene.frame_current)
            if _is_rest_frame(scene):
                reset_object(obj, update_status=False)
                _LAST_FRAMES[_state_key(obj)] = current
                continue
            dt, reset = frame_dt(scene, obj)
            step_object(obj, dt, reset=reset, update_status=False)
        except Exception:
            obj[BREAST_SIM_STATUS_PROP] = "Breast update failed"
            log.warning("Breast frame update failed for %s", obj.name, exc_info=True)


def ensure_frame_handler() -> None:
    physics_runtime.ensure_frame_handler(
        bpy.app.handlers.frame_change_post,
        breast_frame_change_post,
        handler_name="breast_frame_change_post",
        module_suffix="breast_blender",
        runtime_names=_RUNTIME_OBJECT_NAMES,
        live_preview_enabled=dyng_blender.live_preview_enabled,
        refresh_names=_refresh_runtime_object_names,
        scene=getattr(bpy.context, "scene", None),
    )


def remove_frame_handler() -> None:
    physics_runtime.remove_frame_handler(
        bpy.app.handlers.frame_change_post,
        "breast_frame_change_post",
        "breast_blender",
    )


def bake_object(context: bpy.types.Context, obj: bpy.types.Object, frame_start: int, frame_end: int) -> int:
    frame_start, frame_end = physics_runtime.normalized_frame_range(frame_start, frame_end)
    if not is_breast_armature(obj) or not _has_breast_bones(obj):
        return 0
    clear_state(obj)
    baked = 0
    scene = context.scene
    original_frame = int(scene.frame_current)
    dt = physics_runtime.scene_frame_dt(scene)
    global _SUPPRESS_FRAME_HANDLER
    was_suppressed = _SUPPRESS_FRAME_HANDLER
    _SUPPRESS_FRAME_HANDLER = True
    try:
        for frame in range(frame_start, frame_end + 1):
            scene.frame_set(frame)
            if frame == frame_start or frame <= 0:
                reset_object(obj, update_status=False)
            else:
                step_object(obj, dt, reset=False, update_status=False)
            for name in BREAST_BONE_NAMES:
                pose_bone = obj.pose.bones.get(name)
                if pose_bone is None:
                    continue
                physics_runtime.keyframe_pose_bone_transform(pose_bone, frame)
                baked += 1
    finally:
        try:
            scene.frame_set(original_frame)
        finally:
            _SUPPRESS_FRAME_HANDLER = was_suppressed
    obj[BREAST_SIM_STATUS_PROP] = f"Baked {frame_start}-{frame_end}"
    return baked


def summarize_object(obj: bpy.types.Object) -> str:
    if not _has_breast_bones(obj):
        return "Missing l_boob/r_boob"
    settings = _settings_from_object(obj)
    preset = settings.preset or "Custom"
    return f"{preset}, blend {settings.blend:.2f}"
