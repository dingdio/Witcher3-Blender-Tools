from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import bpy
from mathutils import Matrix, Vector

from .action_compat import assign_action, iter_action_fcurves, remove_action_fcurve
from .w3_armature_constants import human_bone_order
from . import pose_key_tools

log = logging.getLogger(__name__)

CONTROL_PREFIX = "W3IK_"
CONTROL_COLLECTION_NAME = "W3 IK Controls"
HIDDEN_COLLECTION_NAME = "W3 IK Hidden"
WIDGET_COLLECTION_NAME = "W3 IK Widgets"
WIDGET_PREFIX = "W3IK_Widget_"
CONSTRAINT_PREFIX = "W3IK_"
RIG_VERSION = 1

SHARED_WIDGET_NAMES = {
    "arrows": "Arrows",
    "cs_cube": "CS_Cube",
}

ACTION_BAKED_PROP = "w3ik_baked_game_action"
ACTION_SOURCE_PROP = "w3ik_source_action"

IK_PROPS = {
    "l_arm": "w3ik_l_arm",
    "r_arm": "w3ik_r_arm",
    "l_leg": "w3ik_l_leg",
    "r_leg": "w3ik_r_leg",
}

CONTROL_TARGETS = {
    "l_hand": ("W3IK_l_hand", "l_hand"),
    "r_hand": ("W3IK_r_hand", "r_hand"),
    "l_foot": ("W3IK_l_foot", "l_foot"),
    "r_foot": ("W3IK_r_foot", "r_foot"),
    "l_toe": ("W3IK_l_toe", "l_toe"),
    "r_toe": ("W3IK_r_toe", "r_toe"),
}

AIM_CONTROLS = {
    "l_arm": "W3IK_l_upperarmIK",
    "r_arm": "W3IK_r_upperarmIK",
    "l_leg": "W3IK_l_thighIK",
    "r_leg": "W3IK_r_thighIK",
}

CHAIN_CONTROLS = {
    "l_arm": "W3IK_l_forearmIK",
    "r_arm": "W3IK_r_forearmIK",
    "l_leg": "W3IK_l_shinIK",
    "r_leg": "W3IK_r_shinIK",
}

POLE_CONTROLS = {
    "l_arm": "W3IK_l_elbow",
    "r_arm": "W3IK_r_elbow",
    "l_leg": "W3IK_l_knee",
    "r_leg": "W3IK_r_knee",
}

# Hidden chain-root bones. The IK chain is mch -> chain so the pole target pins
# the bend plane on the mch bone, leaving the visible aim control as a pure
# twist dial that the snap code can drive deterministically.
MCH_CONTROLS = {
    "l_arm": "W3IK_l_upperarm_mch",
    "r_arm": "W3IK_r_upperarm_mch",
    "l_leg": "W3IK_l_thigh_mch",
    "r_leg": "W3IK_r_thigh_mch",
}

POLE_DISTANCE_FACTOR = 2.0

# Reverse-foot pivot chain (legs only): foot control -> heel pivot -> toe pivot
# -> hidden ankle. The leg IK / foot copy target is the ankle, so rotating the
# pivots rolls the foot while the toes stay planted.
HEEL_CONTROLS = {
    "l_leg": "W3IK_l_heelRoll",
    "r_leg": "W3IK_r_heelRoll",
}

TOE_ROLL_CONTROLS = {
    "l_leg": "W3IK_l_toeRoll",
    "r_leg": "W3IK_r_toeRoll",
}

ANKLE_CONTROLS = {
    "l_leg": "W3IK_l_ankle",
    "r_leg": "W3IK_r_ankle",
}

LIMB_DEFS = {
    "l_arm": {
        "label": "Left Arm",
        "start": ("l_bicep", "l_shoulder"),
        "mid": ("l_forearm", "l_elbowRoll", "l_bicep"),
        "end": "l_hand",
        "target": "W3IK_l_hand",
        "aim": AIM_CONTROLS["l_arm"],
        "chain": CHAIN_CONTROLS["l_arm"],
        "pole": POLE_CONTROLS["l_arm"],
        "mch": MCH_CONTROLS["l_arm"],
        "prop": IK_PROPS["l_arm"],
    },
    "r_arm": {
        "label": "Right Arm",
        "start": ("r_bicep", "r_shoulder"),
        "mid": ("r_forearm", "r_elbowRoll", "r_bicep"),
        "end": "r_hand",
        "target": "W3IK_r_hand",
        "aim": AIM_CONTROLS["r_arm"],
        "chain": CHAIN_CONTROLS["r_arm"],
        "pole": POLE_CONTROLS["r_arm"],
        "mch": MCH_CONTROLS["r_arm"],
        "prop": IK_PROPS["r_arm"],
    },
    "l_leg": {
        "label": "Left Leg",
        "start": ("l_thigh",),
        "mid": ("l_shin",),
        "end": "l_foot",
        "target": "W3IK_l_foot",
        "aim": AIM_CONTROLS["l_leg"],
        "chain": CHAIN_CONTROLS["l_leg"],
        "pole": POLE_CONTROLS["l_leg"],
        "mch": MCH_CONTROLS["l_leg"],
        "heel": HEEL_CONTROLS["l_leg"],
        "toe_roll": TOE_ROLL_CONTROLS["l_leg"],
        "ankle": ANKLE_CONTROLS["l_leg"],
        "prop": IK_PROPS["l_leg"],
    },
    "r_leg": {
        "label": "Right Leg",
        "start": ("r_thigh",),
        "mid": ("r_shin",),
        "end": "r_foot",
        "target": "W3IK_r_foot",
        "aim": AIM_CONTROLS["r_leg"],
        "chain": CHAIN_CONTROLS["r_leg"],
        "pole": POLE_CONTROLS["r_leg"],
        "mch": MCH_CONTROLS["r_leg"],
        "heel": HEEL_CONTROLS["r_leg"],
        "toe_roll": TOE_ROLL_CONTROLS["r_leg"],
        "ankle": ANKLE_CONTROLS["r_leg"],
        "prop": IK_PROPS["r_leg"],
    },
}

BODY_CONTROL_SHAPES = {
    "Trajectory": "trajectory",
    "pelvis": "cs_cube",
    "torso3": "body_circle",
    "head": "body_circle",
}

REQUIRED_HUMAN_BONES = (
    "Root",
    "Trajectory",
    "Reference",
    "pelvis",
    "torso",
    "torso2",
    "torso3",
    "neck",
    "head",
    "l_thigh",
    "l_shin",
    "l_foot",
    "r_thigh",
    "r_shin",
    "r_foot",
    "l_bicep",
    "l_hand",
    "r_bicep",
    "r_hand",
)

_TRANSFORM_PROPS = ("location", "rotation_quaternion")


@dataclass
class IkRigResult:
    success: bool
    message: str = ""
    changed: List[str] = field(default_factory=list)
    details: Dict[str, object] = field(default_factory=dict)

    def __bool__(self):
        return bool(self.success)


@dataclass
class ControlSpec:
    matrix: Matrix
    source: str = ""
    parent: str = ""
    length: float = 0.0
    connected: bool = False


def _control_names(include_optional=True) -> List[str]:
    names = [name for name, _target in CONTROL_TARGETS.values()]
    names.extend(AIM_CONTROLS.values())
    names.extend(CHAIN_CONTROLS.values())
    names.extend(POLE_CONTROLS.values())
    names.extend(MCH_CONTROLS.values())
    names.extend(HEEL_CONTROLS.values())
    names.extend(TOE_ROLL_CONTROLS.values())
    names.extend(ANKLE_CONTROLS.values())
    if not include_optional:
        names = [name for name in names if name not in {"W3IK_l_toe", "W3IK_r_toe"}]
    return names


def _game_export_bones(armature) -> List[str]:
    rig_settings = getattr(armature.data, "witcherui_RigSettings", None) if armature else None
    if rig_settings and len(getattr(rig_settings, "bone_order_list", [])):
        names = [bone.name for bone in rig_settings.bone_order_list]
    else:
        names = list(human_bone_order)
    return [name for name in names if not name.startswith(CONTROL_PREFIX)]


def _active_mode():
    try:
        return bpy.context.mode
    except Exception:
        return "OBJECT"


def _set_active_armature(context, armature):
    if context is None or armature is None:
        return
    try:
        context.view_layer.objects.active = armature
        armature.select_set(True)
    except Exception:
        pass


def _restore_mode(context, mode):
    if mode in {None, ""}:
        return
    try:
        if mode.startswith("POSE"):
            bpy.ops.object.mode_set(mode="POSE")
        elif mode.startswith("EDIT"):
            bpy.ops.object.mode_set(mode="EDIT")
        else:
            bpy.ops.object.mode_set(mode="OBJECT")
    except Exception:
        pass


def _enter_mode(context, armature, mode):
    _set_active_armature(context, armature)
    try:
        bpy.ops.object.mode_set(mode=mode)
    except Exception:
        pass


def _view_update(context):
    try:
        context.view_layer.update()
    except Exception:
        try:
            bpy.context.view_layer.update()
        except Exception:
            pass


def _has_bone(armature, bone_name: str) -> bool:
    return bool(armature and getattr(armature, "data", None) and armature.data.bones.get(bone_name))


def validate_human_armature(armature) -> IkRigResult:
    if armature is None or getattr(armature, "type", None) != "ARMATURE":
        return IkRigResult(False, "Select a character armature")
    missing = [name for name in REQUIRED_HUMAN_BONES if not _has_bone(armature, name)]
    for side in ("l", "r"):
        if not any(_has_bone(armature, name) for name in (f"{side}_elbowRoll", f"{side}_forearm")):
            missing.append(f"{side}_elbowRoll or {side}_forearm")
    if missing:
        return IkRigResult(False, "Not a canonical Witcher human rig", details={"missing": missing})
    return IkRigResult(True, "Human rig validated")


def _rig_scale(armature) -> float:
    bones = getattr(getattr(armature, "data", None), "bones", None)
    if not bones:
        return 0.12
    try:
        ys = [bone.head_local.z for bone in bones] + [bone.tail_local.z for bone in bones]
        height = max(ys) - min(ys)
        if height > 0.0:
            return max(0.05, min(0.35, height * 0.045))
    except Exception:
        pass
    return 0.12


def _pose_matrix(armature, bone_name: str) -> Optional[Matrix]:
    pose_bone = armature.pose.bones.get(bone_name) if getattr(armature, "pose", None) else None
    if pose_bone is not None:
        try:
            return pose_bone.matrix.copy()
        except Exception:
            pass
    bone = armature.data.bones.get(bone_name) if getattr(armature, "data", None) else None
    if bone is not None:
        return bone.matrix_local.copy()
    return None


def _rest_matrix(armature, bone_name: str) -> Optional[Matrix]:
    bone = armature.data.bones.get(bone_name) if getattr(armature, "data", None) else None
    if bone is None:
        return None
    return bone.matrix_local.copy()


def _first_existing_bone(armature, names: Iterable[str]) -> Optional[str]:
    for name in names:
        if _has_bone(armature, name):
            return name
    return None


# Direction (in mid-bone local space) the elbow/knee vertex protrudes when the
# helper hint pre-bends a perfectly straight limb. Only used as a fallback when
# the chain is too straight to derive the bend plane from the pose itself; the
# pole angle is calibrated against the same fallback, so the sign only affects
# where the pole sits on degenerate frames, never the solve.
_POLE_FALLBACK_AXES = {
    "arm": Vector((0.0, 0.0, -1.0)),
    "leg": Vector((-1.0, 0.0, 0.0)),
}


def _pole_position(start_pos: Vector, mid_pos: Vector, end_pos: Vector, mid_matrix: Matrix, limb_name: str, distance: float) -> Vector:
    """Pole location in the bend plane: out from the start->end line through the elbow/knee."""
    fallback_local = _POLE_FALLBACK_AXES["leg" if limb_name.endswith("_leg") else "arm"]
    fallback = mid_matrix.to_3x3() @ fallback_local
    chain_dir = end_pos - start_pos
    if chain_dir.length <= 1.0e-6:
        direction = fallback
    else:
        axis = chain_dir.normalized()
        direction = (mid_pos - start_pos) - ((mid_pos - start_pos).dot(axis)) * axis
        if direction.length <= 1.0e-5:
            direction = fallback - fallback.dot(axis) * axis
    if direction.length <= 1.0e-6:
        direction = Vector((0.0, -1.0, 0.0))
    return mid_pos + direction.normalized() * max(distance, 1.0e-3)


def _rest_pole_angle(armature, limb) -> float:
    """Blender pole_angle calibrated from rest geometry so that a pole placed in
    the rest bend plane reproduces the rest pose exactly."""
    bones = getattr(getattr(armature, "data", None), "bones", None)
    if bones is None:
        return 0.0
    base = bones.get(limb["mch"])
    chain = bones.get(limb["chain"])
    pole = bones.get(limb["pole"])
    if base is None or chain is None or pole is None:
        return 0.0
    base_head = base.head_local.copy()
    base_axis = base.tail_local - base_head
    pole_normal = (chain.tail_local - base_head).cross(pole.head_local - base_head)
    projected_pole_axis = pole_normal.cross(base_axis)
    if base_axis.length <= 1.0e-8 or projected_pole_axis.length <= 1.0e-8:
        return 0.0
    x_axis = base.matrix_local.to_3x3().col[0]
    try:
        angle = x_axis.angle(projected_pole_axis)
    except ValueError:
        return 0.0
    cross = x_axis.cross(projected_pole_axis)
    if cross.length > 1.0e-9 and cross.angle(base_axis) < math.pi * 0.5:
        angle = -angle
    return angle


def _signed_angle_about_axis(v_from: Vector, v_to: Vector, axis: Vector) -> Optional[float]:
    if axis.length <= 1.0e-9:
        return None
    axis = axis.normalized()
    u = v_from - v_from.dot(axis) * axis
    w = v_to - v_to.dot(axis) * axis
    if u.length <= 1.0e-9 or w.length <= 1.0e-9:
        return None
    u.normalize()
    w.normalize()
    return math.atan2(u.cross(w).dot(axis), u.dot(w))


def _limb_ik_constraint(armature, limb):
    chain_pb = armature.pose.bones.get(limb["chain"])
    if chain_pb is None:
        return None
    for constraint in chain_pb.constraints:
        if constraint.type == "IK" and getattr(constraint, "pole_subtarget", "") == limb["pole"]:
            return constraint
    return None


def _wrap_angle(angle: float) -> float:
    while angle > math.pi:
        angle -= math.tau
    while angle < -math.pi:
        angle += math.tau
    return angle


def _helper_hint_constraint(armature, limb):
    chain_pb = armature.pose.bones.get(limb["chain"])
    if chain_pb is None:
        return None
    for constraint in chain_pb.constraints:
        if constraint.type == "LIMIT_ROTATION" and str(getattr(constraint, "name", "")).endswith("_helper_hint"):
            return constraint
    return None


def _flip_helper_hint(armature, limb) -> bool:
    """Negate the hint's pre-bend so the solver folds the helper chain to the
    other side. Needed when bone rolls put the hint's fixed local bend axis on
    the wrong side of the pole plane (pole_angle alone cannot fix the fold side
    because the hint seed rotates along with the chain)."""
    hint = _helper_hint_constraint(armature, limb)
    if hint is None:
        return False
    for axis in ("x", "y", "z"):
        try:
            minimum = -float(getattr(hint, f"min_{axis}"))
            maximum = -float(getattr(hint, f"max_{axis}"))
            if minimum > maximum:
                minimum, maximum = maximum, minimum
            setattr(hint, f"min_{axis}", minimum)
            setattr(hint, f"max_{axis}", maximum)
        except Exception:
            pass
    return True


def _pole_misalignment(context, armature, limb) -> Optional[float]:
    """Signed angle (about the chain axis) from the solved elbow to the pole."""
    _view_update(context)
    mch_pb = armature.pose.bones.get(limb["mch"])
    chain_pb = armature.pose.bones.get(limb["chain"])
    pole_pb = armature.pose.bones.get(limb["pole"])
    target_pb = armature.pose.bones.get(limb["target"])
    if None in (mch_pb, chain_pb, pole_pb, target_pb):
        return None
    root_head = mch_pb.matrix.to_translation()
    axis = target_pb.matrix.to_translation() - root_head
    elbow = chain_pb.matrix.to_translation()
    pole_pos = pole_pb.matrix.to_translation()
    return _signed_angle_about_axis(elbow - root_head, pole_pos - root_head, axis)


def _iterate_pole_alignment(context, armature, limb, ik, tolerance, iterations=3) -> Optional[float]:
    """Walk ik.pole_angle toward zero measured misalignment. Returns the final
    residual, or the stalled residual when the correction stops improving (the
    solver fold is seed-bound and chases the rotation - hint must flip)."""
    for _iteration in range(max(1, int(iterations))):
        phi = _pole_misalignment(context, armature, limb)
        if phi is None:
            return None
        if abs(phi) <= tolerance:
            return phi
        base_angle = float(ik.pole_angle)
        ik.pole_angle = _wrap_angle(base_angle + phi)
        phi_after = _pole_misalignment(context, armature, limb)
        if phi_after is None:
            return None
        if abs(phi_after) >= abs(phi) - 1.0e-6:
            ik.pole_angle = base_angle
            _view_update(context)
            return phi
    return _pole_misalignment(context, armature, limb)


def _calibrate_pole_angle(context, armature, limb, tolerance=1.0e-4) -> bool:
    """Empirically tune ik.pole_angle so the solved elbow lands exactly on the
    pole side of the chain plane. Folds the limb temporarily so the bend
    direction is well defined even on straight rest poses. If the correction
    stalls because the hint folds the chain to the mirror side (happens with
    rolled bones), flip the hint's pre-bend and retune."""
    ik = _limb_ik_constraint(armature, limb)
    target_pb = armature.pose.bones.get(limb["target"])
    mch_pb = armature.pose.bones.get(limb["mch"])
    if ik is None or target_pb is None or mch_pb is None:
        return False
    saved_basis = target_pb.matrix_basis.copy()
    try:
        _view_update(context)
        root_head = mch_pb.matrix.to_translation()
        target_head = target_pb.matrix.to_translation()
        axis = target_head - root_head
        if axis.length <= 1.0e-6:
            return False
        folded = target_pb.matrix.copy()
        folded.translation = root_head + axis * 0.75
        target_pb.matrix = folded

        phi = _iterate_pole_alignment(context, armature, limb, ik, tolerance)
        if phi is not None and abs(phi) <= tolerance:
            return True
        if not _flip_helper_hint(armature, limb):
            return False
        phi = _iterate_pole_alignment(context, armature, limb, ik, tolerance)
        if phi is not None and abs(phi) <= tolerance:
            return True
        _flip_helper_hint(armature, limb)
        _iterate_pole_alignment(context, armature, limb, ik, tolerance)
        return False
    finally:
        target_pb.matrix_basis = saved_basis
        _view_update(context)


def _calibrate_pole_angles(context, armature) -> List[str]:
    calibrated = []
    for limb_name, limb in LIMB_DEFS.items():
        try:
            if _calibrate_pole_angle(context, armature, limb):
                calibrated.append(limb_name)
            else:
                log.debug("Pole angle calibration incomplete for %s", limb_name)
        except Exception:
            log.debug("Pole angle calibration failed for %s", limb_name, exc_info=True)
    return calibrated


def _y_twist_between(current: Matrix, target: Matrix) -> float:
    """Rotation about the bone's own Y axis that takes `current` to `target`."""
    try:
        delta = current.to_3x3().inverted() @ target.to_3x3()
        quat = delta.to_quaternion()
    except Exception:
        return 0.0
    twist = 2.0 * math.atan2(quat.y, quat.w)
    while twist > math.pi:
        twist -= math.tau
    while twist < -math.pi:
        twist += math.tau
    return twist


def _apply_twist_corrections(context, armature, limbs: Sequence[str], fk_matrices: Dict[str, Matrix], iterations=4, tolerance=1.0e-4) -> float:
    """With the pole pinning the bend plane, the only remaining FK/IK mismatch is
    the start bone's roll. Measure the solved bone against the FK snapshot and
    feed the residual into the aim control's Y twist until it converges."""
    pairs = []
    for limb_name in _normalized_limbs(limbs, include_body=False):
        limb = LIMB_DEFS.get(limb_name)
        if not limb:
            continue
        start_name = _first_existing_bone(armature, limb["start"])
        start_pose = armature.pose.bones.get(start_name) if start_name else None
        aim_pose = armature.pose.bones.get(limb["aim"])
        fk_matrix = fk_matrices.get(start_name) if start_name else None
        if start_pose is None or aim_pose is None or fk_matrix is None:
            continue
        pairs.append((aim_pose, start_pose, fk_matrix))
    if not pairs:
        return 0.0

    worst_previous = None
    for _iteration in range(max(1, int(iterations))):
        _view_update(context)
        worst = 0.0
        applied = []
        for aim_pose, start_pose, fk_matrix in pairs:
            twist = _y_twist_between(start_pose.matrix, fk_matrix)
            worst = max(worst, abs(twist))
            if abs(twist) <= tolerance:
                continue
            try:
                aim_pose.rotation_euler[1] += twist
                applied.append((aim_pose, twist))
            except Exception:
                log.debug("Could not apply twist correction via %s", aim_pose.name, exc_info=True)
        if worst <= tolerance:
            break
        if worst_previous is not None and worst >= worst_previous:
            for aim_pose, twist in applied:
                try:
                    aim_pose.rotation_euler[1] -= twist
                except Exception:
                    pass
            break
        worst_previous = worst
    _view_update(context)
    worst = 0.0
    for _aim_pose, start_pose, fk_matrix in pairs:
        worst = max(worst, abs(_y_twist_between(start_pose.matrix, fk_matrix)))
    return worst


def _pose_deviation(armature, fk_matrices: Dict[str, Matrix]) -> Tuple[float, float]:
    """Max positional (units) / rotational (degrees) deviation of the evaluated
    pose from an FK snapshot. Verifies that snapping/baking did not move bones."""
    max_position = 0.0
    max_rotation = 0.0
    for bone_name, fk_matrix in fk_matrices.items():
        pose_bone = armature.pose.bones.get(bone_name)
        if pose_bone is None:
            continue
        current = pose_bone.matrix
        max_position = max(max_position, (current.to_translation() - fk_matrix.to_translation()).length)
        try:
            delta = current.to_3x3().to_quaternion().rotation_difference(fk_matrix.to_3x3().to_quaternion())
            angle = abs(float(delta.angle))
        except Exception:
            angle = 0.0
        if angle > math.pi:
            angle = abs(angle - math.tau)
        max_rotation = max(max_rotation, math.degrees(angle))
    return max_position, max_rotation


def _ensure_armature_prop(armature, prop_name: str, default=0.0):
    if prop_name not in armature:
        armature[prop_name] = float(default)
    try:
        armature.id_properties_ui(prop_name).update(min=0.0, max=1.0, soft_min=0.0, soft_max=1.0)
    except Exception:
        pass


def _set_limb_props(armature, limbs: Sequence[str], value: float, include_body=False):
    for limb in _normalized_limbs(limbs, include_body=include_body):
        prop = IK_PROPS.get(limb)
        if prop:
            _ensure_armature_prop(armature, prop, value)
            armature[prop] = float(value)
    # Custom-prop writes do not tag the depsgraph, so the constraint-influence
    # drivers would keep their stale value through the next view-layer update
    # (e.g. the first sampled frame of a bake started away from frame_start).
    try:
        armature.update_tag()
    except Exception:
        pass


def _key_limb_props(armature, limbs: Sequence[str], frame: float, include_body=False):
    for limb in _normalized_limbs(limbs, include_body=include_body):
        prop = IK_PROPS.get(limb)
        if prop:
            try:
                armature.keyframe_insert(data_path=f'["{prop}"]', frame=frame)
            except Exception:
                log.debug("Could not key IK prop %s", prop, exc_info=True)


def _normalized_limbs(limbs: Optional[Sequence[str]], include_body=True) -> List[str]:
    valid = set(LIMB_DEFS)
    if include_body:
        valid.add("body")
    if not limbs:
        return ["l_arm", "r_arm", "l_leg", "r_leg"]
    result = []
    for limb in limbs:
        limb = str(limb or "").strip()
        if limb in valid and limb not in result:
            result.append(limb)
    return result


def _controlled_game_bones_for_limbs(armature, limbs: Sequence[str]) -> List[str]:
    names = []
    seen = set()
    for limb_name in _normalized_limbs(limbs, include_body=False):
        limb = LIMB_DEFS.get(limb_name)
        if not limb:
            continue
        candidates = [
            _first_existing_bone(armature, limb["start"]),
            _first_existing_bone(armature, limb["mid"]),
            limb["end"],
        ]
        if "foot" in limb["end"]:
            candidates.append(limb["end"].replace("foot", "toe"))
        for bone_name in candidates:
            if bone_name and bone_name not in seen and _has_bone(armature, bone_name):
                names.append(bone_name)
                seen.add(bone_name)
    return names


def _start_end_game_bones_for_limbs(armature, limbs: Sequence[str]) -> List[str]:
    """Game bones whose FK keys the bake replaces. Mid bones (forearm/shin) are
    excluded on purpose: their roll has no IK dial and comes from the FK basis
    seeding the damped track, so their keys must survive the bake."""
    names = []
    seen = set()
    for limb_name in _normalized_limbs(limbs, include_body=False):
        limb = LIMB_DEFS.get(limb_name)
        if not limb:
            continue
        for bone_name in (_first_existing_bone(armature, limb["start"]), limb["end"]):
            if bone_name and bone_name not in seen and _has_bone(armature, bone_name):
                names.append(bone_name)
                seen.add(bone_name)
    return names


def _has_limb_ik_controls(armature, limbs: Sequence[str]) -> bool:
    pose_bones = getattr(getattr(armature, "pose", None), "bones", None)
    if pose_bones is None:
        return False
    for limb_name in _normalized_limbs(limbs, include_body=False):
        limb = LIMB_DEFS.get(limb_name)
        if not limb:
            continue
        for control_name in (limb["target"], limb["aim"], limb["chain"]):
            if pose_bones.get(control_name) is None:
                return False
    return True


def _reset_pose_bone_basis(armature, bone_names: Iterable[str]):
    for bone_name in bone_names:
        pose_bone = armature.pose.bones.get(bone_name) if getattr(armature, "pose", None) else None
        if pose_bone is None:
            continue
        try:
            pose_bone.matrix_basis = Matrix.Identity(4)
            continue
        except Exception:
            pass
        try:
            pose_bone.location = (0.0, 0.0, 0.0)
        except Exception:
            pass
        try:
            if str(getattr(pose_bone, "rotation_mode", "") or "") == "AXIS_ANGLE":
                pose_bone.rotation_axis_angle = (0.0, 0.0, 1.0, 0.0)
            elif str(getattr(pose_bone, "rotation_mode", "") or "") == "QUATERNION":
                pose_bone.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
            else:
                pose_bone.rotation_euler = (0.0, 0.0, 0.0)
        except Exception:
            pass
        try:
            pose_bone.scale = (1.0, 1.0, 1.0)
        except Exception:
            pass


def _ensure_bone_collection(armature, name=CONTROL_COLLECTION_NAME, visible=True):
    collections = getattr(armature.data, "collections", None)
    if collections is None:
        return None
    try:
        collection = collections.get(name)
    except Exception:
        collection = None
    if collection is None:
        try:
            collection = collections.new(name)
        except Exception:
            collection = None
    if collection is not None and hasattr(collection, "is_visible"):
        try:
            collection.is_visible = bool(visible)
        except Exception:
            pass
    return collection


def _assign_bone_to_collection(collection, bone):
    if collection is None or bone is None:
        return
    try:
        collection.assign(bone)
    except Exception:
        pass


def _unassign_bone_from_collection(collection, bone):
    if collection is None or bone is None:
        return
    try:
        collection.unassign(bone)
    except Exception:
        pass


def _ensure_widget_collection(context):
    collection = bpy.data.collections.get(WIDGET_COLLECTION_NAME)
    if collection is None:
        collection = bpy.data.collections.new(WIDGET_COLLECTION_NAME)
    scene = getattr(context, "scene", None) if context is not None else bpy.context.scene
    if scene is not None:
        linked = any(child == collection for child in scene.collection.children)
        if not linked:
            try:
                scene.collection.children.link(collection)
            except Exception:
                pass
    try:
        collection.hide_render = True
    except Exception:
        pass
    return collection


def _circle_points(count=32, radius=1.0):
    verts = []
    edges = []
    for index in range(count):
        angle = (math.tau * index) / count
        verts.append((math.cos(angle) * radius, math.sin(angle) * radius, 0.0))
        edges.append((index, (index + 1) % count))
    return verts, edges


def _circle_y_points(count=32, radius=1.0):
    verts = []
    edges = []
    for index in range(count):
        angle = (math.tau * index) / count
        verts.append((math.cos(angle) * radius, 0.0, math.sin(angle) * radius))
        edges.append((index, (index + 1) % count))
    return verts, edges


def _widget_geometry(kind):
    if kind == "circle":
        return _circle_points(32, 1.0)
    if kind == "body_circle":
        return _circle_y_points(40, 1.0)
    if kind == "square":
        verts = [(-1.0, -1.0, 0.0), (1.0, -1.0, 0.0), (1.0, 1.0, 0.0), (-1.0, 1.0, 0.0)]
        return verts, [(0, 1), (1, 2), (2, 3), (3, 0)]
    if kind == "body_square":
        verts = [(-1.0, 0.0, -1.0), (1.0, 0.0, -1.0), (1.0, 0.0, 1.0), (-1.0, 0.0, 1.0)]
        return verts, [(0, 1), (1, 2), (2, 3), (3, 0)]
    if kind == "diamond":
        verts = [(0.0, -1.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (-1.0, 0.0, 0.0)]
        return verts, [(0, 1), (1, 2), (2, 3), (3, 0)]
    if kind == "foot":
        verts = [
            (-0.65, -0.85, 0.0),
            (0.65, -0.85, 0.0),
            (0.65, 0.42, 0.0),
            (0.34, 0.85, 0.0),
            (-0.34, 0.85, 0.0),
            (-0.65, 0.42, 0.0),
        ]
        return verts, [(0, 1), (1, 2), (2, 3), (3, 4), (4, 5), (5, 0)]
    if kind == "trajectory":
        verts = [
            (-1.0, -1.0, 0.0),
            (1.0, -1.0, 0.0),
            (1.0, 0.55, 0.0),
            (0.45, 0.55, 0.0),
            (0.0, 1.15, 0.0),
            (-0.45, 0.55, 0.0),
            (-1.0, 0.55, 0.0),
            (-1.0, -1.0, 0.0),
            (-0.55, 0.0, 0.0),
            (0.55, 0.0, 0.0),
            (0.0, -0.55, 0.0),
            (0.0, 0.55, 0.0),
        ]
        return verts, [(0, 1), (1, 2), (2, 3), (3, 4), (4, 5), (5, 6), (6, 7), (8, 9), (10, 11)]
    if kind == "cube":
        verts = [
            (-1, -1, -1), (1, -1, -1), (1, 1, -1), (-1, 1, -1),
            (-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1),
        ]
        return verts, [
            (0, 1), (1, 2), (2, 3), (3, 0),
            (4, 5), (5, 6), (6, 7), (7, 4),
            (0, 4), (1, 5), (2, 6), (3, 7),
        ]
    if kind == "arrows":
        verts = [
            (0.055, 0.00, -0.16),
            (0.055, 0.38, -0.16),
            (-0.055, 0.00, -0.16),
            (-0.055, 0.38, -0.16),
            (0.13, 0.38, -0.16),
            (0.00, 0.56, -0.16),
            (-0.13, 0.38, -0.16),
            (0.055, 0.00, 0.16),
            (0.055, 0.38, 0.16),
            (-0.055, 0.00, 0.16),
            (-0.055, 0.38, 0.16),
            (0.13, 0.38, 0.16),
            (0.00, 0.56, 0.16),
            (-0.13, 0.38, 0.16),
        ]
        return verts, [
            (0, 1), (2, 3), (1, 4), (4, 5), (3, 6), (5, 6), (0, 2),
            (7, 8), (9, 10), (8, 11), (11, 12), (10, 13), (12, 13), (7, 9),
        ]
    if kind == "sphere":
        verts = []
        edges = []
        for plane in range(3):
            start = len(verts)
            for index in range(24):
                angle = (math.tau * index) / 24
                c = math.cos(angle)
                s = math.sin(angle)
                if plane == 0:
                    verts.append((c, s, 0.0))
                elif plane == 1:
                    verts.append((c, 0.0, s))
                else:
                    verts.append((0.0, c, s))
                edges.append((start + index, start + ((index + 1) % 24)))
        return verts, edges
    return _circle_points(24, 1.0)


def _widget_object_name(kind):
    return SHARED_WIDGET_NAMES.get(kind, f"{WIDGET_PREFIX}{kind}")


def _ensure_widget_object(context, kind):
    name = _widget_object_name(kind)
    obj = bpy.data.objects.get(name)
    if obj is not None:
        if kind == "cs_cube":
            try:
                obj.empty_display_type = "CUBE"
            except Exception:
                pass
        else:
            _refresh_widget_geometry(obj, kind)
        return obj
    if kind == "cs_cube":
        obj = bpy.data.objects.new(name, None)
        obj.empty_display_type = "CUBE"
    else:
        verts, edges = _widget_geometry(kind)
        mesh = bpy.data.meshes.new(f"{name}Mesh")
        mesh.from_pydata(verts, edges, [])
        mesh.update()
        obj = bpy.data.objects.new(name, mesh)
    collection = _ensure_widget_collection(context)
    try:
        collection.objects.link(obj)
    except RuntimeError:
        pass
    obj.hide_viewport = True
    obj.hide_render = True
    obj.hide_select = True
    try:
        obj.display_type = "WIRE"
    except Exception:
        pass
    return obj


def _refresh_widget_geometry(obj, kind):
    if obj is None or kind == "cs_cube":
        return
    try:
        verts, edges = _widget_geometry(kind)
    except Exception:
        return
    mesh = getattr(obj, "data", None)
    if mesh is None or getattr(obj, "type", "") != "MESH":
        return
    try:
        mesh.clear_geometry()
        mesh.from_pydata(verts, edges, [])
        mesh.update()
    except Exception:
        log.debug("Could not refresh IK widget %s", getattr(obj, "name", kind), exc_info=True)


def _control_shape_kind(control_name):
    if control_name in set(MCH_CONTROLS.values()) | set(ANKLE_CONTROLS.values()):
        return "none"
    if control_name in set(POLE_CONTROLS.values()):
        return "sphere"
    if control_name in set(HEEL_CONTROLS.values()) | set(TOE_ROLL_CONTROLS.values()):
        return "square"
    if control_name in {"W3IK_l_upperarmIK", "W3IK_r_upperarmIK", "W3IK_l_thighIK", "W3IK_r_thighIK"}:
        return "arrows"
    if control_name in {"W3IK_l_forearmIK", "W3IK_r_forearmIK", "W3IK_l_shinIK", "W3IK_r_shinIK"}:
        return "none"
    if "foot" in control_name:
        return "cs_cube"
    if "toe" in control_name:
        return "diamond"
    if "hand" in control_name:
        return "cs_cube"
    if "trajectory" in control_name:
        return "trajectory"
    if "root" in control_name:
        return "cube"
    return "sphere"


def _control_shape_scale(control_name, base_scale):
    if control_name in set(POLE_CONTROLS.values()):
        return base_scale * 0.3
    if control_name in set(HEEL_CONTROLS.values()) | set(TOE_ROLL_CONTROLS.values()):
        return base_scale * 0.35
    if "upperarmIK" in control_name or "thighIK" in control_name:
        return base_scale * 1.35
    if "forearmIK" in control_name or "shinIK" in control_name:
        return base_scale * 0.65
    if "hand" in control_name:
        return (base_scale * 0.20, base_scale * 0.55, base_scale * 0.32)
    if "foot" in control_name:
        return (base_scale * 0.45, base_scale * 0.70, base_scale * 0.18)
    if "toe" in control_name:
        return base_scale * 0.35
    return base_scale


def _control_shape_scale_for_bone(control_name, base_scale, pose_bone):
    length = getattr(getattr(pose_bone, "bone", None), "length", 0.0) if pose_bone is not None else 0.0
    if length <= 1.0e-6:
        return _control_shape_scale(control_name, base_scale)
    if "upperarmIK" in control_name or "thighIK" in control_name:
        return max(base_scale * 1.8, length / 0.75)
    if "hand" in control_name:
        return (length * 0.20, length * 0.50, length * 0.35)
    if "foot" in control_name:
        return (length * 0.25, length * 0.50, length * 0.10)
    return _control_shape_scale(control_name, base_scale)


def _shape_scale_xyz(scale):
    if isinstance(scale, (tuple, list, Vector)):
        if len(scale) >= 3:
            return (float(scale[0]), float(scale[1]), float(scale[2]))
        if len(scale) == 2:
            return (float(scale[0]), float(scale[1]), float(scale[1]))
        if len(scale) == 1:
            value = float(scale[0])
            return (value, value, value)
    value = float(scale)
    return (value, value, value)


def _shape_scale_scalar(scale):
    sx, sy, sz = _shape_scale_xyz(scale)
    return (sx + sy + sz) / 3.0


def _control_shape_translation(control_name, scale):
    if "hand" in control_name or "foot" in control_name:
        return (0.0, _shape_scale_xyz(scale)[1], 0.0)
    return None


def _control_shape_translation_for_bone(control_name, scale, pose_bone):
    if "hand" in control_name or "foot" in control_name:
        length = getattr(getattr(pose_bone, "bone", None), "length", 0.0) if pose_bone is not None else 0.0
        if length > 1.0e-6:
            return (0.0, length * 0.5, 0.0)
    return _control_shape_translation(control_name, scale)


def _flat_cube_rotation(armature, pose_bone):
    if pose_bone is None:
        return (0.0, 0.0, 0.0)
    try:
        bone_space = pose_bone.matrix.to_3x3().inverted()
        up_local = bone_space @ Vector((0.0, 0.0, 1.0))
    except Exception:
        return (0.0, 0.0, 0.0)
    if up_local.length <= 1.0e-6:
        return (0.0, 0.0, 0.0)
    z_axis = up_local.normalized()
    y_axis = Vector((0.0, 1.0, 0.0))
    y_axis = y_axis - z_axis * y_axis.dot(z_axis)
    if y_axis.length <= 1.0e-6:
        y_axis = Vector((1.0, 0.0, 0.0)) - z_axis * z_axis.x
    if y_axis.length <= 1.0e-6:
        return (0.0, 0.0, 0.0)
    y_axis.normalize()
    x_axis = y_axis.cross(z_axis)
    if x_axis.length <= 1.0e-6:
        return (0.0, 0.0, 0.0)
    x_axis.normalize()
    matrix = Matrix((
        (x_axis.x, y_axis.x, z_axis.x),
        (x_axis.y, y_axis.y, z_axis.y),
        (x_axis.z, y_axis.z, z_axis.z),
    ))
    try:
        return tuple(matrix.to_euler())
    except Exception:
        return (0.0, 0.0, 0.0)


def _control_shape_rotation(armature, control_name, kind, pose_bone, rot90_z):
    if kind == "arrows":
        return (0.0, 0.0, 0.0)
    if "foot" in control_name and kind == "cs_cube":
        return _flat_cube_rotation(armature, pose_bone)
    if kind in {"foot", "square", "diamond", "circle"}:
        return (0.0, 0.0, rot90_z)
    return (0.0, 0.0, 0.0)


def _body_shape_scale(bone_name, base_scale):
    if bone_name == "Trajectory":
        return base_scale * 4.2
    if bone_name == "pelvis":
        return base_scale * 2.0
    if bone_name == "torso3":
        return base_scale * 2.8
    if bone_name == "head":
        return base_scale * 1.4
    return base_scale


def _set_pose_bone_color(pose_bone, palette):
    color = getattr(pose_bone, "color", None)
    if color is None:
        return
    try:
        color.palette = palette
    except Exception:
        pass


def _rig_rot90_enabled(armature):
    rig_settings = getattr(getattr(armature, "data", None), "witcherui_RigSettings", None)
    if rig_settings is None:
        return False
    return bool(
        getattr(rig_settings, "rot90_state", "") == "ON"
        or getattr(rig_settings, "rot90_imported", False)
        or getattr(rig_settings, "rot90_compensate", False)
    )


def _set_custom_shape_rotation(pose_bone, rotation):
    if not hasattr(pose_bone, "custom_shape_rotation_euler"):
        return
    try:
        pose_bone.custom_shape_rotation_euler = rotation
    except Exception:
        pass


def _apply_custom_shape(context, pose_bone, kind, scale, rotation=(0.0, 0.0, 0.0), translation=None):
    if pose_bone is None:
        return False
    if kind == "none":
        pose_bone.custom_shape = None
        return True
    shape = _ensure_widget_object(context, kind)
    pose_bone.custom_shape = shape
    try:
        pose_bone.use_custom_shape_bone_size = False
    except Exception:
        pass
    if hasattr(pose_bone, "custom_shape_scale_xyz"):
        pose_bone.custom_shape_scale_xyz = _shape_scale_xyz(scale)
    elif hasattr(pose_bone, "custom_shape_scale"):
        pose_bone.custom_shape_scale = _shape_scale_scalar(scale)
    _set_custom_shape_rotation(pose_bone, rotation)
    if hasattr(pose_bone, "custom_shape_translation"):
        try:
            pose_bone.custom_shape_translation = translation or (0.0, 0.0, 0.0)
        except Exception:
            pass
    try:
        pose_bone.bone.show_wire = True
    except Exception:
        pass
    return True


def _assign_control_shapes(context, armature):
    base_scale = _rig_scale(armature)
    rot90_z = math.radians(90.0) if _rig_rot90_enabled(armature) else 0.0
    assigned = []
    for control_name in _control_names():
        pose_bone = armature.pose.bones.get(control_name) if getattr(armature, "pose", None) else None
        if pose_bone is None:
            continue
        kind = _control_shape_kind(control_name)
        scale = _control_shape_scale_for_bone(control_name, base_scale, pose_bone)
        rotation = _control_shape_rotation(armature, control_name, kind, pose_bone, rot90_z)
        translation = _control_shape_translation_for_bone(control_name, scale, pose_bone)
        _apply_custom_shape(context, pose_bone, kind, scale, rotation, translation)
        if "foot" in control_name or "toe" in control_name or "thighIK" in control_name or "_knee" in control_name or "heel" in control_name:
            _set_pose_bone_color(pose_bone, "THEME04")
        elif "hand" in control_name or "upperarmIK" in control_name or "_elbow" in control_name:
            _set_pose_bone_color(pose_bone, "THEME03")
        else:
            _set_pose_bone_color(pose_bone, "THEME14")
        assigned.append(control_name)
    for bone_name, kind in BODY_CONTROL_SHAPES.items():
        pose_bone = armature.pose.bones.get(bone_name) if getattr(armature, "pose", None) else None
        if pose_bone is None:
            continue
        rotation = (0.0, 0.0, rot90_z) if kind == "trajectory" else (0.0, 0.0, 0.0)
        if _apply_custom_shape(context, pose_bone, kind, _body_shape_scale(bone_name, base_scale), rotation):
            _set_pose_bone_color(pose_bone, "THEME14")
            assigned.append(bone_name)
    return assigned


def _set_pose_lock(pose_bone, attr_name, values):
    if not hasattr(pose_bone, attr_name):
        return
    try:
        setattr(pose_bone, attr_name, values)
    except Exception:
        pass


def _set_pose_ik_lock(pose_bone, axis_name, value):
    attr_name = f"lock_ik_{axis_name}"
    if not hasattr(pose_bone, attr_name):
        return
    try:
        setattr(pose_bone, attr_name, bool(value))
    except Exception:
        pass


def _configure_control_pose_bone(control_name, pose_bone):
    if pose_bone is None:
        return
    is_aim = control_name in set(AIM_CONTROLS.values())
    is_chain = control_name in set(CHAIN_CONTROLS.values())
    is_pole = control_name in set(POLE_CONTROLS.values())
    is_mch = control_name in set(MCH_CONTROLS.values()) | set(ANKLE_CONTROLS.values())
    is_roll = control_name in set(HEEL_CONTROLS.values()) | set(TOE_ROLL_CONTROLS.values())
    is_toe_target = control_name in {"W3IK_l_toe", "W3IK_r_toe"}
    if is_aim or is_chain or is_pole or is_mch or is_roll:
        try:
            pose_bone.rotation_mode = "XYZ"
        except Exception:
            pass
    elif str(getattr(pose_bone, "rotation_mode", "") or "") == "":
        try:
            pose_bone.rotation_mode = "QUATERNION"
        except Exception:
            pass

    _set_pose_ik_lock(pose_bone, "x", False)
    _set_pose_ik_lock(pose_bone, "y", False)
    _set_pose_ik_lock(pose_bone, "z", False)

    if is_mch:
        _set_pose_lock(pose_bone, "lock_location", (True, True, True))
        _set_pose_lock(pose_bone, "lock_rotation", (True, True, True))
        _set_pose_lock(pose_bone, "lock_scale", (True, True, True))
    elif is_roll or is_toe_target:
        # Rotation-only controls: the toe game bone copies rotation from the
        # toe control, never location (snap still writes the basis location
        # via the API to keep the widget glued to the FK toe).
        _set_pose_lock(pose_bone, "lock_location", (True, True, True))
        _set_pose_lock(pose_bone, "lock_rotation", (False, False, False))
        _set_pose_lock(pose_bone, "lock_scale", (True, True, True))
    elif is_pole:
        _set_pose_lock(pose_bone, "lock_location", (False, False, False))
        _set_pose_lock(pose_bone, "lock_rotation", (True, True, True))
        _set_pose_lock(pose_bone, "lock_scale", (True, True, True))
    elif is_aim:
        _set_pose_lock(pose_bone, "lock_location", (True, True, True))
        _set_pose_lock(pose_bone, "lock_rotation", (True, False, True))
        _set_pose_lock(pose_bone, "lock_scale", (True, True, True))
    elif is_chain:
        _set_pose_lock(pose_bone, "lock_location", (True, True, True))
        _set_pose_lock(pose_bone, "lock_rotation", (False, False, False))
        _set_pose_lock(pose_bone, "lock_scale", (True, True, True))
        # Hinge: twist (Y) locked, both bend axes free. Locking a bend axis
        # would keep the solver from settling exactly into the pole plane.
        _set_pose_ik_lock(pose_bone, "y", True)
    else:
        _set_pose_lock(pose_bone, "lock_location", (False, False, False))
        _set_pose_lock(pose_bone, "lock_rotation", (False, False, False))
        _set_pose_lock(pose_bone, "lock_scale", (True, True, True))


def _configure_control_pose_bones(armature, bone_names: Optional[Iterable[str]] = None):
    if armature is None or getattr(armature, "pose", None) is None:
        return
    names = list(bone_names) if bone_names is not None else _control_names()
    for control_name in names:
        _configure_control_pose_bone(control_name, armature.pose.bones.get(control_name))


def _clear_body_control_shapes(armature):
    cleared = []
    pose_bones = getattr(getattr(armature, "pose", None), "bones", None)
    if pose_bones is None:
        return cleared
    for bone_name in BODY_CONTROL_SHAPES:
        pose_bone = pose_bones.get(bone_name)
        shape = getattr(pose_bone, "custom_shape", None) if pose_bone is not None else None
        shape_name = str(getattr(shape, "name", "") or "") if shape is not None else ""
        if shape is None or not (shape_name.startswith(WIDGET_PREFIX) or shape_name in set(SHARED_WIDGET_NAMES.values())):
            continue
        pose_bone.custom_shape = None
        cleared.append(bone_name)
    return cleared


def _bone_parent_name(armature, bone_name: str) -> str:
    bone = armature.data.bones.get(bone_name) if getattr(armature, "data", None) else None
    parent = getattr(bone, "parent", None)
    return getattr(parent, "name", "") or ""


def _bone_length(armature, bone_name: str, fallback=0.0) -> float:
    bone = armature.data.bones.get(bone_name) if getattr(armature, "data", None) else None
    if bone is None:
        return fallback
    try:
        return float(bone.length)
    except Exception:
        return fallback


def _child_extent_length(armature, bone_name: str, fallback=0.0) -> float:
    bone = armature.data.bones.get(bone_name) if getattr(armature, "data", None) else None
    if bone is None:
        return fallback
    head = bone.head_local.copy()
    distances = []
    for child in getattr(bone, "children", []) or []:
        if child.name.startswith(CONTROL_PREFIX):
            continue
        try:
            distances.append((child.head_local - head).length)
            distances.append((child.tail_local - head).length)
        except Exception:
            pass
    distances = [value for value in distances if value > 1.0e-5]
    return max(distances) if distances else fallback


def _control_bone_length(armature, control_name: str, source_name: str, base_scale: float) -> float:
    source_length = _bone_length(armature, source_name, base_scale)
    if "hand" in control_name:
        hand_extent = _child_extent_length(armature, source_name, 0.0)
        parent_name = _bone_parent_name(armature, source_name)
        parent_length = _bone_length(armature, parent_name, 0.0) if parent_name else 0.0
        practical_length = max(source_length, hand_extent, parent_length * 0.35, base_scale * 1.1)
        return min(max(practical_length, base_scale * 0.8), base_scale * 2.2)
    if "foot" in control_name:
        return source_length
    if "toe" in control_name:
        return max(base_scale * 0.18, min(source_length * 0.25, base_scale * 0.30))
    return source_length


def _is_hidden_control_name(bone_name: str) -> bool:
    return bone_name in set(CHAIN_CONTROLS.values()) | set(MCH_CONTROLS.values()) | set(ANKLE_CONTROLS.values())


def _is_target_control_name(bone_name: str) -> bool:
    return bone_name in {name for name, _target in CONTROL_TARGETS.values()}


def _root_control_parent(armature) -> str:
    if _has_bone(armature, "Root"):
        return "Root"
    if _has_bone(armature, "Trajectory"):
        return "Trajectory"
    return ""


def _target_control_matrix(control_name: str, source_matrix: Matrix) -> Matrix:
    if "foot" in control_name:
        return Matrix.Translation(source_matrix.to_translation())
    return source_matrix


def _aim_control_parent(armature, limb_name: str, start_name: str) -> str:
    if limb_name.endswith("_arm"):
        side = limb_name[0]
        shoulder = f"{side}_shoulder"
        if _has_bone(armature, shoulder):
            return shoulder
    return _bone_parent_name(armature, start_name)


def _control_specs(armature) -> Dict[str, ControlSpec]:
    # Specs are derived from rest geometry (not the current pose) so that rig
    # creation is idempotent and the calibrated pole angle stays valid no matter
    # which frame the rig was created on.
    specs = {}
    base_scale = _rig_scale(armature)
    for _key, (control_name, target_name) in CONTROL_TARGETS.items():
        if not _has_bone(armature, target_name):
            continue
        matrix = _rest_matrix(armature, target_name)
        if matrix is not None:
            parent = _root_control_parent(armature)
            if control_name in {"W3IK_l_toe", "W3IK_r_toe"}:
                # Under the heel pivot so toes stay planted while the foot
                # rolls over the toe pivot.
                parent = control_name.replace("toe", "heelRoll")
            specs[control_name] = ControlSpec(
                matrix=_target_control_matrix(control_name, matrix),
                source=target_name,
                parent=parent,
                length=_control_bone_length(armature, control_name, target_name, base_scale),
            )

    for _limb_name, limb_def in LIMB_DEFS.items():
        start = _first_existing_bone(armature, limb_def["start"])
        mid = _first_existing_bone(armature, limb_def["mid"])
        start_matrix = _rest_matrix(armature, start) if start else None
        if start and start_matrix is not None:
            aim_parent = _aim_control_parent(armature, _limb_name, start)
            specs[limb_def["aim"]] = ControlSpec(
                matrix=start_matrix,
                source=start,
                parent=aim_parent,
                length=_bone_length(armature, start),
            )
            specs[limb_def["mch"]] = ControlSpec(
                matrix=start_matrix.copy(),
                source=start,
                parent=aim_parent,
                length=_bone_length(armature, start),
            )
        mid_matrix = _rest_matrix(armature, mid) if mid else None
        if mid and mid_matrix is not None:
            specs[limb_def["chain"]] = ControlSpec(
                matrix=mid_matrix,
                source=mid,
                parent=limb_def["mch"],
                length=_bone_length(armature, mid),
                connected=True,
            )
        end_matrix = _rest_matrix(armature, limb_def["end"])
        if start and start_matrix is not None and mid_matrix is not None and end_matrix is not None:
            distance = POLE_DISTANCE_FACTOR * _bone_length(armature, start, base_scale)
            pole_pos = _pole_position(
                start_matrix.to_translation(),
                mid_matrix.to_translation(),
                end_matrix.to_translation(),
                mid_matrix,
                _limb_name,
                distance,
            )
            specs[limb_def["pole"]] = ControlSpec(
                matrix=Matrix.Translation(pole_pos),
                source="",
                parent=_root_control_parent(armature),
                length=max(base_scale * 0.5, 0.05),
            )
        if limb_def.get("ankle") and end_matrix is not None:
            foot_head = end_matrix.to_translation()
            foot_length = _bone_length(armature, limb_def["end"], base_scale)
            foot_tail = end_matrix @ Vector((0.0, foot_length, 0.0))
            toe_bone_name = limb_def["end"].replace("foot", "toe")
            toe_matrix = _rest_matrix(armature, toe_bone_name)
            if toe_matrix is not None:
                ground_z = toe_matrix.to_translation().z
                toe_tip = toe_matrix @ Vector((0.0, _bone_length(armature, toe_bone_name, base_scale * 0.3), 0.0))
            else:
                ground_z = foot_tail.z
                toe_tip = foot_tail
            heel_pos = Vector((foot_head.x, foot_head.y, ground_z))
            roll_length = max(base_scale * 0.3, 0.03)
            specs[limb_def["heel"]] = ControlSpec(
                matrix=Matrix.Translation(heel_pos),
                source="",
                parent=limb_def["target"],
                length=roll_length,
            )
            specs[limb_def["toe_roll"]] = ControlSpec(
                matrix=Matrix.Translation(toe_tip),
                source="",
                parent=limb_def["heel"],
                length=roll_length,
            )
            specs[limb_def["ankle"]] = ControlSpec(
                matrix=Matrix.Translation(foot_head),
                source="",
                parent=limb_def["toe_roll"],
                length=roll_length,
            )
    return specs


def _create_edit_control_bones(context, armature, rebuild=False) -> List[str]:
    specs = _control_specs(armature)
    scale = _rig_scale(armature)
    changed = []
    previous_mode = _active_mode()
    _enter_mode(context, armature, "EDIT")
    try:
        edit_bones = armature.data.edit_bones
        if rebuild:
            for bone_name in list(edit_bones.keys()):
                if bone_name.startswith(CONTROL_PREFIX):
                    edit_bones.remove(edit_bones[bone_name])
        else:
            current_controls = set(specs)
            removed = True
            while removed:
                removed = False
                for bone_name in list(edit_bones.keys()):
                    if not bone_name.startswith(CONTROL_PREFIX) or bone_name in current_controls:
                        continue
                    bone = edit_bones.get(bone_name)
                    if bone is None:
                        continue
                    edit_bones.remove(bone)
                    changed.append(bone_name)
                    removed = True
        for bone_name, spec in specs.items():
            bone = edit_bones.get(bone_name)
            if bone is None:
                bone = edit_bones.new(bone_name)
                changed.append(bone_name)
            matrix = spec.matrix
            head = matrix.to_translation()
            length = spec.length if spec.length > 1.0e-6 else scale
            tail = head + (matrix.to_3x3() @ Vector((0.0, length, 0.0)))
            if (tail - head).length <= 1.0e-6:
                tail = head + Vector((0.0, scale, 0.0))
            bone.head = head
            bone.tail = tail
            # Derive the roll from the spec matrix so the control's rest equals
            # the spec rotation exactly. Copying the source bone's roll is wrong
            # for controls built along a different axis: e.g. foot controls are
            # +Y world with identity rotation, and a Witcher foot's ~90deg roll
            # twisted the ankle chain (and the foot) by -90deg per rebuild.
            try:
                bone.align_roll(matrix.to_3x3() @ Vector((0.0, 0.0, 1.0)))
            except Exception:
                source_bone = edit_bones.get(spec.source) if spec.source else None
                bone.roll = source_bone.roll if source_bone is not None else 0.0
            bone.use_deform = False
            bone.parent = None
            bone.use_connect = False
        for bone_name, spec in specs.items():
            bone = edit_bones.get(bone_name)
            parent = edit_bones.get(spec.parent) if spec.parent else None
            if bone is None:
                continue
            bone.parent = parent
            if parent is not None:
                bone.use_connect = bool(spec.connected)
    finally:
        _restore_mode(context, previous_mode)

    collection = _ensure_bone_collection(armature, CONTROL_COLLECTION_NAME, visible=True)
    hidden_collection = _ensure_bone_collection(armature, HIDDEN_COLLECTION_NAME, visible=False)
    for bone_name in specs:
        bone = armature.data.bones.get(bone_name)
        if bone is not None:
            try:
                bone.use_deform = False
            except Exception:
                pass
            hidden = _is_hidden_control_name(bone_name)
            try:
                bone.hide = bool(hidden)
            except Exception:
                pass
            try:
                bone.hide_select = bool(hidden)
            except Exception:
                pass
            if _is_target_control_name(bone_name):
                # World-style grab (False) for hands/feet; toes keep the default
                # local interpretation (True) - they are rotation-only and the
                # snap glue math writes their basis location in local convention.
                try:
                    bone.use_local_location = bone_name in {"W3IK_l_toe", "W3IK_r_toe"}
                except Exception:
                    pass
            if hidden:
                _assign_bone_to_collection(hidden_collection, bone)
                _unassign_bone_from_collection(collection, bone)
            else:
                _assign_bone_to_collection(collection, bone)
                _unassign_bone_from_collection(hidden_collection, bone)
    try:
        armature.show_in_front = True
    except Exception:
        pass
    return changed


def _remove_w3ik_constraints(armature):
    if armature is None or getattr(armature, "pose", None) is None:
        return 0
    count = 0
    for pose_bone in armature.pose.bones:
        for constraint in list(pose_bone.constraints):
            if str(getattr(constraint, "name", "") or "").startswith(CONSTRAINT_PREFIX):
                pose_bone.constraints.remove(constraint)
                count += 1
    return count


def _add_constraint_driver(armature, constraint, prop_name: str):
    _ensure_armature_prop(armature, prop_name, 0.0)
    try:
        fcurve = constraint.driver_add("influence")
    except TypeError:
        try:
            constraint.driver_remove("influence")
            fcurve = constraint.driver_add("influence")
        except Exception:
            return
    except Exception:
        return
    driver = fcurve.driver
    driver.type = "SCRIPTED"
    driver.expression = "v"
    while driver.variables:
        driver.variables.remove(driver.variables[0])
    var = driver.variables.new()
    var.name = "v"
    var.type = "SINGLE_PROP"
    var.targets[0].id = armature
    var.targets[0].data_path = f'["{prop_name}"]'


def _set_constraint_spaces(constraint, space="WORLD"):
    for attr_name in ("target_space", "owner_space"):
        if hasattr(constraint, attr_name):
            try:
                setattr(constraint, attr_name, space)
            except Exception:
                pass


def _set_owner_constraint_space(constraint, space):
    if not hasattr(constraint, "owner_space"):
        return
    try:
        constraint.owner_space = space
    except Exception:
        pass


def _new_limit_rotation(owner, name: str, prop_owner, prop_name: str):
    if owner is None:
        return None
    constraint = owner.constraints.new(type="LIMIT_ROTATION")
    constraint.name = name
    _set_owner_constraint_space(constraint, "LOCAL")
    if hasattr(constraint, "euler_order"):
        try:
            constraint.euler_order = owner.rotation_mode
        except Exception:
            pass
    if hasattr(constraint, "use_transform_limit"):
        try:
            constraint.use_transform_limit = True
        except Exception:
            pass
    _add_constraint_driver(prop_owner, constraint, prop_name)
    return constraint


def _new_helper_hint(owner, limb_name: str, prop_owner, prop_name: str):
    """Small pre-bend hint so Blender's IK solver chooses a stable fold side."""
    if owner is None:
        return None
    constraint = owner.constraints.new(type="LIMIT_ROTATION")
    constraint.name = f"{CONSTRAINT_PREFIX}{limb_name}_helper_hint"
    _set_owner_constraint_space(constraint, "LOCAL")
    if hasattr(constraint, "euler_order"):
        try:
            constraint.euler_order = owner.rotation_mode
        except Exception:
            pass
    try:
        if limb_name.endswith("_leg"):
            constraint.min_x = 0.0
            constraint.max_x = 0.0
            constraint.min_y = 0.0
            constraint.max_y = 0.0
            # Witcher imported leg roll is opposite the arm helper's bend side.
            constraint.min_z = -math.radians(18.0)
            constraint.max_z = -math.radians(18.0)
        else:
            constraint.min_x = math.radians(18.0)
            constraint.max_x = math.radians(18.0)
            constraint.min_y = 0.0
            constraint.max_y = 0.0
            constraint.min_z = 0.0
            constraint.max_z = 0.0
        constraint.use_limit_x = True
        constraint.use_limit_y = True
        constraint.use_limit_z = True
        constraint.use_transform_limit = True
    except Exception:
        pass
    _add_constraint_driver(prop_owner, constraint, prop_name)
    return constraint


def _new_ik_constraint(owner, name: str, target_armature, subtarget: str, prop_name: str, pole_subtarget: str = "", pole_angle: float = 0.0):
    if owner is None or target_armature.pose.bones.get(subtarget) is None:
        return None
    ik = owner.constraints.new(type="IK")
    ik.name = name
    ik.target = target_armature
    ik.subtarget = subtarget
    if pole_subtarget and target_armature.pose.bones.get(pole_subtarget) is not None:
        ik.pole_target = target_armature
        ik.pole_subtarget = pole_subtarget
        ik.pole_angle = float(pole_angle)
    else:
        ik.pole_target = None
        ik.pole_subtarget = ""
    ik.chain_count = 2
    if hasattr(ik, "use_tail"):
        try:
            ik.use_tail = True
        except Exception:
            pass
    if hasattr(ik, "use_location"):
        try:
            ik.use_location = True
        except Exception:
            pass
    if hasattr(ik, "use_rotation"):
        try:
            ik.use_rotation = False
        except Exception:
            pass
    if hasattr(ik, "use_stretch"):
        try:
            ik.use_stretch = False
        except Exception:
            pass
    _add_constraint_driver(target_armature, ik, prop_name)
    return ik


def _add_limb_constraints(armature, limb_name: str):
    """Witcher limb constraints. Same pattern for arms and legs:

    - Hidden helper chain (mch -> chain) gets a hint + IK constraint targeting
      the end control (hand/foot), with the pole control (elbow/knee) pinning
      the bend plane. The pole angle is calibrated from rest geometry so the
      solve is deterministic and snappable.
    - Real start bone (thigh/bicep) damped-tracks the chain helper to point at
      the knee/elbow, then copy-rotates the aim control's local Y to inherit
      the user-controlled twist along the bone axis. The aim control sits
      outside the IK chain, so its twist is never overridden by the solver.
    - Real mid bones track the end target. Helpers choose the bend, while the
      game bones stay connected and aim down the evaluated chain.
    - End bone (hand/foot) copies location and rotation from the end target.
    """
    limb = LIMB_DEFS[limb_name]
    start_name = _first_existing_bone(armature, limb["start"])
    mid_name = _first_existing_bone(armature, limb["mid"])
    start = armature.pose.bones.get(start_name) if start_name else None
    mid = armature.pose.bones.get(mid_name) if mid_name else None
    end = armature.pose.bones.get(limb["end"])
    chain = armature.pose.bones.get(limb["chain"])
    aim = armature.pose.bones.get(limb["aim"])
    # Legs aim at the hidden ankle (end of the reverse-foot pivot chain) so the
    # heel/toe roll controls move the IK goal; arms aim at the hand control.
    ankle_name = limb.get("ankle")
    ik_target_name = ankle_name if ankle_name and armature.pose.bones.get(ankle_name) is not None else limb["target"]

    _new_helper_hint(chain, limb_name, armature, limb["prop"])
    _new_ik_constraint(
        chain,
        f"{CONSTRAINT_PREFIX}{limb_name}_helper_solver",
        armature,
        ik_target_name,
        limb["prop"],
        pole_subtarget=limb["pole"],
        pole_angle=_rest_pole_angle(armature, limb),
    )

    if start is not None and chain is not None:
        _new_limit_rotation(start, f"{CONSTRAINT_PREFIX}{limb_name}_start_limit", armature, limb["prop"])
        track = start.constraints.new(type="DAMPED_TRACK")
        track.name = f"{CONSTRAINT_PREFIX}{limb_name}_start_track"
        track.target = armature
        track.subtarget = limb["chain"]
        try:
            track.track_axis = "TRACK_Y"
        except Exception:
            pass
        _add_constraint_driver(armature, track, limb["prop"])

        if aim is not None:
            twist = start.constraints.new(type="COPY_ROTATION")
            twist.name = f"{CONSTRAINT_PREFIX}{limb_name}_start_twist"
            twist.target = armature
            twist.subtarget = limb["aim"]
            try:
                twist.use_x = False
                twist.use_y = True
                twist.use_z = False
            except Exception:
                pass
            # AFTER composes the aim's pure Y rotation onto the damped-track
            # result (a true roll about the bone axis). REPLACE would swap a
            # single euler channel, which changes the aim direction instead.
            if hasattr(twist, "mix_mode"):
                try:
                    twist.mix_mode = "AFTER"
                except Exception:
                    pass
            try:
                twist.target_space = "LOCAL"
                twist.owner_space = "LOCAL"
            except Exception:
                pass
            if hasattr(twist, "euler_order"):
                try:
                    twist.euler_order = "XYZ"
                except Exception:
                    pass
            _add_constraint_driver(armature, twist, limb["prop"])

    if mid is not None:
        _new_limit_rotation(mid, f"{CONSTRAINT_PREFIX}{limb_name}_mid_limit", armature, limb["prop"])
        mid_track = mid.constraints.new(type="DAMPED_TRACK")
        mid_track.name = f"{CONSTRAINT_PREFIX}{limb_name}_mid_track"
        mid_track.target = armature
        mid_track.subtarget = ik_target_name
        try:
            mid_track.track_axis = "TRACK_Y"
        except Exception:
            pass
        _add_constraint_driver(armature, mid_track, limb["prop"])

    for pose_name in (start_name, mid_name):
        pose_bone = armature.pose.bones.get(pose_name) if pose_name else None
        if pose_bone is not None and hasattr(pose_bone, "ik_stretch"):
            try:
                pose_bone.ik_stretch = 0.0
            except Exception:
                pass
    if end is not None and armature.pose.bones.get(limb["target"]) is not None:
        end_limit = end.constraints.new(type="LIMIT_ROTATION")
        end_limit.name = f"{CONSTRAINT_PREFIX}{limb_name}_end_limit"
        _set_owner_constraint_space(end_limit, "LOCAL")
        if hasattr(end_limit, "use_transform_limit"):
            try:
                end_limit.use_transform_limit = True
            except Exception:
                pass
        _add_constraint_driver(armature, end_limit, limb["prop"])

        end_rot = end.constraints.new(type="COPY_ROTATION")
        end_rot.name = f"{CONSTRAINT_PREFIX}{limb_name}_end_rot"
        end_rot.target = armature
        end_rot.subtarget = ik_target_name
        _set_constraint_spaces(end_rot, "POSE")
        if hasattr(end_rot, "mix_mode"):
            try:
                end_rot.mix_mode = "REPLACE"
            except Exception:
                pass
        _add_constraint_driver(armature, end_rot, limb["prop"])

    # Live toe follow. Safe now that control rests come from rest geometry
    # (the old per-pose helper rests made toe copy-rotation curl the feet).
    if "foot" in limb["end"]:
        toe_name = limb["end"].replace("foot", "toe")
        toe_control = limb["target"].replace("foot", "toe")
        toe = armature.pose.bones.get(toe_name)
        if toe is not None and armature.pose.bones.get(toe_control) is not None:
            toe_rot = toe.constraints.new(type="COPY_ROTATION")
            toe_rot.name = f"{CONSTRAINT_PREFIX}{limb_name}_toe_rot"
            toe_rot.target = armature
            toe_rot.subtarget = toe_control
            _set_constraint_spaces(toe_rot, "POSE")
            if hasattr(toe_rot, "mix_mode"):
                try:
                    toe_rot.mix_mode = "REPLACE"
                except Exception:
                    pass
            _add_constraint_driver(armature, toe_rot, limb["prop"])


def _add_constraints(armature):
    _remove_w3ik_constraints(armature)
    for limb_name in LIMB_DEFS:
        _add_limb_constraints(armature, limb_name)


def _set_controls_from_current_pose(armature, limbs: Sequence[str], include_body=True):
    changed = []
    limbs = _normalized_limbs(limbs, include_body=include_body)
    for limb_name in limbs:
        if limb_name == "body":
            continue
        limb = LIMB_DEFS.get(limb_name)
        if not limb:
            continue
        target = armature.pose.bones.get(limb["target"])
        end = armature.pose.bones.get(limb["end"])
        if target is not None and end is not None:
            target.rotation_mode = "QUATERNION"
            target.matrix = end.matrix.copy()
            _configure_control_pose_bone(limb["target"], target)
            changed.append(limb["target"])

        start = _first_existing_bone(armature, limb["start"])
        start_pose = armature.pose.bones.get(start) if start else None
        aim = armature.pose.bones.get(limb["aim"])
        if start_pose is not None and aim is not None:
            # The aim control is a pure twist dial: seed it with the Y-twist
            # component of the FK basis (aim and start share rest + parent).
            aim.rotation_mode = "XYZ"
            aim.rotation_euler = (0.0, _y_twist_between(Matrix.Identity(4), start_pose.matrix_basis), 0.0)
            _configure_control_pose_bone(limb["aim"], aim)
            changed.append(limb["aim"])

        mid = _first_existing_bone(armature, limb["mid"])
        mid_pose = armature.pose.bones.get(mid) if mid else None
        chain = armature.pose.bones.get(limb["chain"])
        if mid_pose is not None and chain is not None:
            chain.rotation_mode = "XYZ"
            chain.matrix = mid_pose.matrix.copy()
            _configure_control_pose_bone(limb["chain"], chain)
            changed.append(limb["chain"])

        pole = armature.pose.bones.get(limb["pole"])
        if pole is not None and start_pose is not None and mid_pose is not None and end is not None:
            distance = POLE_DISTANCE_FACTOR * _bone_length(armature, start, _rig_scale(armature))
            pole_pos = _pole_position(
                start_pose.matrix.to_translation(),
                mid_pose.matrix.to_translation(),
                end.matrix.to_translation(),
                mid_pose.matrix,
                limb_name,
                distance,
            )
            matrix = pole.matrix.copy()
            matrix.translation = pole_pos
            pole.matrix = matrix
            _configure_control_pose_bone(limb["pole"], pole)
            changed.append(limb["pole"])

        if "foot" in limb["end"]:
            toe = armature.pose.bones.get(limb["end"].replace("foot", "toe"))
            _snap_foot_chain_controls(
                armature,
                limb,
                end.matrix.copy() if end is not None else None,
                toe.matrix.copy() if toe is not None else None,
                changed,
            )

    return changed


def _snap_foot_chain_controls(armature, limb, end_world: Optional[Matrix], toe_world: Optional[Matrix], changed: List[str]):
    """Zero the heel/toe roll pivots and place the toe control on the FK toe.
    The toe basis is computed analytically: its parents (foot control + zeroed
    pivots) were assigned this update, so their depsgraph caches are stale."""
    if "foot" not in limb["end"]:
        return
    for key in ("heel", "toe_roll"):
        roll_name = limb.get(key)
        roll_pb = armature.pose.bones.get(roll_name) if roll_name else None
        if roll_pb is None:
            continue
        try:
            roll_pb.rotation_mode = "XYZ"
            roll_pb.rotation_euler = (0.0, 0.0, 0.0)
            roll_pb.location = (0.0, 0.0, 0.0)
        except Exception:
            continue
        _configure_control_pose_bone(roll_name, roll_pb)
        changed.append(roll_name)

    toe_control = limb["target"].replace("foot", "toe")
    toe_target = armature.pose.bones.get(toe_control)
    if toe_target is None or toe_world is None:
        return
    toe_target.rotation_mode = "QUATERNION"
    target_pose = armature.pose.bones.get(limb["target"])
    heel_pb = armature.pose.bones.get(limb.get("heel") or "")
    toe_parent = getattr(toe_target, "parent", None)
    if (
        end_world is not None
        and target_pose is not None
        and heel_pb is not None
        and toe_parent is not None
        and toe_parent.name == heel_pb.name
    ):
        ctrl_rest = target_pose.bone.matrix_local
        heel_world = end_world @ _matrix_inverted_safe(ctrl_rest) @ heel_pb.bone.matrix_local
        toe_target.matrix_basis = _pose_basis_from_matrix(armature, toe_target, toe_world, {heel_pb.name: heel_world})
    else:
        try:
            toe_target.matrix = toe_world.copy()
        except Exception:
            return
    _configure_control_pose_bone(toe_control, toe_target)
    changed.append(toe_control)


def _matrix_from_pose_snapshot(armature, matrices: Dict[str, Matrix], bone_name: str) -> Optional[Matrix]:
    matrix = matrices.get(bone_name) if matrices else None
    if matrix is not None:
        return matrix.copy()
    return _pose_matrix(armature, bone_name)


def _set_controls_from_pose_snapshot(armature, limbs: Sequence[str], matrices: Dict[str, Matrix], include_body=True):
    changed = []
    limbs = _normalized_limbs(limbs, include_body=include_body)
    for limb_name in limbs:
        if limb_name == "body":
            continue
        limb = LIMB_DEFS.get(limb_name)
        if not limb:
            continue
        target = armature.pose.bones.get(limb["target"])
        end_matrix = _matrix_from_pose_snapshot(armature, matrices, limb["end"])
        if target is not None and end_matrix is not None:
            target.rotation_mode = "QUATERNION"
            target.matrix = end_matrix.copy()
            _configure_control_pose_bone(limb["target"], target)
            changed.append(limb["target"])

        start = _first_existing_bone(armature, limb["start"])
        start_matrix = _matrix_from_pose_snapshot(armature, matrices, start) if start else None
        start_pose = armature.pose.bones.get(start) if start else None
        aim = armature.pose.bones.get(limb["aim"])
        if aim is not None and start_pose is not None:
            aim.rotation_mode = "XYZ"
            aim.rotation_euler = (0.0, _y_twist_between(Matrix.Identity(4), start_pose.matrix_basis), 0.0)
            _configure_control_pose_bone(limb["aim"], aim)
            changed.append(limb["aim"])

        mid = _first_existing_bone(armature, limb["mid"])
        mid_matrix = _matrix_from_pose_snapshot(armature, matrices, mid) if mid else None
        chain = armature.pose.bones.get(limb["chain"])
        if chain is not None and mid_matrix is not None:
            chain.rotation_mode = "XYZ"
            chain.matrix = mid_matrix.copy()
            _configure_control_pose_bone(limb["chain"], chain)
            changed.append(limb["chain"])

        pole = armature.pose.bones.get(limb["pole"])
        if pole is not None and start_matrix is not None and mid_matrix is not None and end_matrix is not None:
            distance = POLE_DISTANCE_FACTOR * _bone_length(armature, start, _rig_scale(armature))
            pole_pos = _pole_position(
                start_matrix.to_translation(),
                mid_matrix.to_translation(),
                end_matrix.to_translation(),
                mid_matrix,
                limb_name,
                distance,
            )
            matrix = pole.matrix.copy()
            matrix.translation = pole_pos
            pole.matrix = matrix
            _configure_control_pose_bone(limb["pole"], pole)
            changed.append(limb["pole"])

        if "foot" in limb["end"]:
            toe_matrix = _matrix_from_pose_snapshot(armature, matrices, limb["end"].replace("foot", "toe"))
            _snap_foot_chain_controls(armature, limb, end_matrix, toe_matrix, changed)
    return changed


def _pose_matrices_for_bones(armature, bone_names: Iterable[str]) -> Dict[str, Matrix]:
    matrices = {}
    for bone_name in bone_names:
        pose_bone = armature.pose.bones.get(bone_name) if getattr(armature, "pose", None) else None
        if pose_bone is not None:
            matrices[bone_name] = pose_bone.matrix.copy()
    return matrices


def _pose_bases_for_bones(armature, bone_names: Iterable[str]) -> Dict[str, Matrix]:
    bases = {}
    for bone_name in bone_names:
        pose_bone = armature.pose.bones.get(bone_name)
        if pose_bone is not None:
            bases[bone_name] = pose_bone.matrix_basis.copy()
    return bases


def _apply_control_pose_bases(armature, bases: Dict[str, Matrix]):
    # Basis assignment needs no parent depsgraph state, unlike pose_bone.matrix,
    # so applying a whole control chain in one go is safe.
    changed = []
    for bone_name, basis in bases.items():
        pose_bone = armature.pose.bones.get(bone_name)
        if pose_bone is None:
            continue
        try:
            pose_bone.matrix_basis = basis.copy()
        except Exception:
            log.debug("Could not apply control basis to %s", bone_name, exc_info=True)
            continue
        _configure_control_pose_bone(bone_name, pose_bone)
        changed.append(bone_name)
    return changed


def _matrix_inverted_safe(matrix: Matrix) -> Matrix:
    try:
        return matrix.inverted_safe()
    except Exception:
        return matrix.inverted()


def _pose_bone_depth(armature, bone_name: str) -> int:
    bone = armature.data.bones.get(bone_name) if getattr(armature, "data", None) else None
    depth = 0
    parent = getattr(bone, "parent", None)
    while parent is not None and depth < 128:
        depth += 1
        parent = getattr(parent, "parent", None)
    return depth


def _ordered_matrix_bone_names(armature, matrices: Dict[str, Matrix]) -> List[str]:
    order = {name: index for index, name in enumerate(_game_export_bones(armature))}
    return sorted(
        matrices.keys(),
        key=lambda name: (_pose_bone_depth(armature, name), order.get(name, len(order))),
    )


def _pose_basis_from_matrix(armature, pose_bone, pose_matrix: Matrix, matrices: Dict[str, Matrix]) -> Matrix:
    rest_matrix = pose_bone.bone.matrix_local
    parent = getattr(pose_bone, "parent", None)
    if parent is None:
        return _matrix_inverted_safe(rest_matrix) @ pose_matrix
    parent_pose_matrix = matrices.get(parent.name)
    if parent_pose_matrix is None:
        parent_pose_matrix = parent.matrix.copy()
    parent_rest_matrix = parent.bone.matrix_local
    return (
        _matrix_inverted_safe(rest_matrix)
        @ parent_rest_matrix
        @ _matrix_inverted_safe(parent_pose_matrix)
        @ pose_matrix
    )


def _quaternion_dot(a, b) -> float:
    try:
        return float(sum(float(a[index]) * float(b[index]) for index in range(4)))
    except Exception:
        return 1.0


def _align_pose_quaternion_to_previous(pose_bone, previous_quaternions: Optional[Dict[str, object]]):
    if previous_quaternions is None:
        return
    if str(getattr(pose_bone, "rotation_mode", "") or "") != "QUATERNION":
        return
    try:
        current = pose_bone.rotation_quaternion.copy()
    except Exception:
        return
    previous = previous_quaternions.get(pose_bone.name)
    if previous is not None and _quaternion_dot(current, previous) < 0.0:
        try:
            current.negate()
            pose_bone.rotation_quaternion = current
        except Exception:
            pass
    try:
        previous_quaternions[pose_bone.name] = pose_bone.rotation_quaternion.copy()
    except Exception:
        previous_quaternions[pose_bone.name] = current


def _pose_bone_rotation_data_path(pose_bone):
    mode = str(getattr(pose_bone, "rotation_mode", "") or "")
    if mode == "AXIS_ANGLE":
        return "rotation_axis_angle"
    if mode and mode != "QUATERNION":
        return "rotation_euler"
    return "rotation_quaternion"


def _key_pose_bones(armature, bone_names: Iterable[str], frame: float, previous_quaternions: Optional[Dict[str, object]] = None):
    keyed = []
    for bone_name in bone_names:
        pose_bone = armature.pose.bones.get(bone_name)
        if pose_bone is None:
            continue
        _align_pose_quaternion_to_previous(pose_bone, previous_quaternions)
        for data_path in ("location", _pose_bone_rotation_data_path(pose_bone)):
            try:
                pose_bone.keyframe_insert(data_path=data_path, frame=frame)
            except Exception:
                log.debug("Could not key %s.%s", bone_name, data_path, exc_info=True)
        keyed.append(bone_name)
    return keyed


def _clear_matching_action_fcurves(action, armature, prefixes: Sequence[str], exact_bones: Sequence[str] = ()):
    if action is None:
        return 0
    exact = {f'pose.bones["{name}"].' for name in exact_bones}
    count = 0
    for fcurve in list(iter_action_fcurves(action, target=armature)):
        data_path = str(getattr(fcurve, "data_path", "") or "")
        if any(data_path.startswith(prefix) for prefix in prefixes) or any(data_path.startswith(prefix) for prefix in exact):
            try:
                remove_action_fcurve(action, fcurve, target=armature)
                count += 1
            except Exception:
                log.debug("Could not remove fcurve %s", data_path, exc_info=True)
    return count


def _control_fcurve_prefixes():
    prefixes = [f'pose.bones["{CONTROL_PREFIX}']
    prefixes.extend(f'["{IK_PROPS[limb]}"]' for limb in LIMB_DEFS)
    return prefixes


def _set_action_interpolation(action, armature=None):
    if action is None:
        return
    for fcurve in iter_action_fcurves(action, target=armature):
        for point in getattr(fcurve, "keyframe_points", []) or []:
            try:
                point.interpolation = "LINEAR"
            except Exception:
                pass


def _frame_range(frame_start, frame_end) -> Tuple[int, int]:
    start = int(round(float(frame_start)))
    end = int(round(float(frame_end)))
    if end < start:
        start, end = end, start
    return start, end


def _active_action(armature):
    anim = getattr(armature, "animation_data", None)
    return getattr(anim, "action", None) if anim else None


def _store_pose_matrices(armature, sync_markers=True) -> Dict[str, Matrix]:
    matrices = {}
    for bone_name in _game_export_bones(armature):
        pose_bone = armature.pose.bones.get(bone_name)
        if pose_bone is not None:
            matrices[bone_name] = pose_bone.matrix.copy()
    if sync_markers:
        for ik_name, source_name in pose_key_tools.GAME_IK_BONE_MAP.items():
            if source_name in matrices and armature.pose.bones.get(ik_name) is not None:
                matrices[ik_name] = matrices[source_name].copy()
    return matrices


def _apply_pose_matrices_unkeyed(armature, matrices: Dict[str, Matrix]):
    applied = []
    for bone_name in _ordered_matrix_bone_names(armature, matrices):
        if bone_name.startswith(CONTROL_PREFIX):
            continue
        pose_bone = armature.pose.bones.get(bone_name)
        matrix = matrices.get(bone_name)
        if pose_bone is None or matrix is None:
            continue
        try:
            pose_bone.matrix_basis = _pose_basis_from_matrix(armature, pose_bone, matrix, matrices)
        except Exception:
            log.debug("Could not restore matrix to %s", bone_name, exc_info=True)
            continue
        applied.append(bone_name)
    return applied


def _apply_pose_matrices(armature, matrices: Dict[str, Matrix], frame: float, previous_quaternions: Optional[Dict[str, object]] = None):
    keyed = []
    for bone_name in _ordered_matrix_bone_names(armature, matrices):
        matrix = matrices.get(bone_name)
        pose_bone = armature.pose.bones.get(bone_name)
        if pose_bone is None or matrix is None:
            continue
        try:
            pose_bone.rotation_mode = "QUATERNION"
        except Exception:
            pass
        try:
            pose_bone.matrix_basis = _pose_basis_from_matrix(armature, pose_bone, matrix, matrices)
        except Exception:
            log.debug("Could not apply matrix to %s", bone_name, exc_info=True)
            continue
        _align_pose_quaternion_to_previous(pose_bone, previous_quaternions)
        for data_path in _TRANSFORM_PROPS:
            try:
                pose_bone.keyframe_insert(data_path=data_path, frame=frame)
            except Exception:
                log.debug("Could not key %s.%s", bone_name, data_path, exc_info=True)
        keyed.append(bone_name)
    return keyed


def create_ik_rig(context, armature, options=None) -> IkRigResult:
    options = options or {}
    validation = validate_human_armature(armature)
    if not validation:
        return validation
    rebuild = bool(options.get("rebuild", False))
    previous_mode = _active_mode()
    changed = []
    try:
        _enter_mode(context, armature, "POSE")
        _view_update(context)
        pose_snapshot = _store_pose_matrices(armature, sync_markers=False)
        for prop in IK_PROPS.values():
            _ensure_armature_prop(armature, prop, 0.0)
            armature[prop] = 0.0
        _remove_w3ik_constraints(armature)
        _view_update(context)
        _apply_pose_matrices_unkeyed(armature, pose_snapshot)
        _view_update(context)
        changed.extend(_create_edit_control_bones(context, armature, rebuild=rebuild))
        _enter_mode(context, armature, "POSE")
        for bone_name in _control_names():
            pose_bone = armature.pose.bones.get(bone_name)
            if pose_bone is not None:
                pose_bone.rotation_mode = "QUATERNION"
        _assign_control_shapes(context, armature)
        _set_controls_from_pose_snapshot(armature, ("l_arm", "r_arm", "l_leg", "r_leg"), pose_snapshot, include_body=False)
        _configure_control_pose_bones(armature)
        _add_constraints(armature)
        _view_update(context)
        _set_limb_props(armature, ("l_arm", "r_arm", "l_leg", "r_leg"), 1.0, include_body=False)
        _calibrate_pole_angles(context, armature)
        armature["w3ik_rig_version"] = RIG_VERSION
        armature["w3ik_rig_enabled"] = True
        armature["w3ik_export_requires_bake"] = True
        _view_update(context)
    finally:
        _restore_mode(context, previous_mode)
    return IkRigResult(True, f"IK controls ready and enabled ({len(changed)} changed)", changed=changed)


def remove_ik_rig(context, armature) -> IkRigResult:
    if armature is None or getattr(armature, "type", None) != "ARMATURE":
        return IkRigResult(False, "Select a character armature")
    previous_mode = _active_mode()
    removed_constraints = _remove_w3ik_constraints(armature)
    cleared_shapes = _clear_body_control_shapes(armature)
    removed_bones = []
    try:
        _enter_mode(context, armature, "EDIT")
        edit_bones = armature.data.edit_bones
        for bone_name in list(edit_bones.keys()):
            if bone_name.startswith(CONTROL_PREFIX):
                edit_bones.remove(edit_bones[bone_name])
                removed_bones.append(bone_name)
    finally:
        _restore_mode(context, previous_mode)
    for prop_name in list(IK_PROPS.values()) + ["w3ik_rig_version", "w3ik_rig_enabled", "w3ik_export_requires_bake"]:
        if prop_name in armature:
            try:
                del armature[prop_name]
            except Exception:
                pass
    return IkRigResult(
        True,
        f"Removed {len(removed_bones)} controls",
        changed=removed_bones,
        details={"constraints": removed_constraints, "body_shapes": cleared_shapes},
    )


def recalibrate_pole_angles(context, armature) -> IkRigResult:
    """Re-run the empirical pole-angle calibration on an existing rig (e.g.
    after bone edits, or to upgrade rigs created before pole targets)."""
    validation = validate_human_armature(armature)
    if not validation:
        return validation
    if not _has_limb_ik_controls(armature, tuple(LIMB_DEFS)):
        return IkRigResult(False, "Create IK controls first")
    previous_mode = _active_mode()
    previous_props = {prop: float(armature.get(prop, 0.0)) for prop in IK_PROPS.values() if prop in armature}
    try:
        _enter_mode(context, armature, "POSE")
        _set_limb_props(armature, tuple(LIMB_DEFS), 1.0, include_body=False)
        _view_update(context)
        calibrated = _calibrate_pole_angles(context, armature)
    finally:
        for prop, value in previous_props.items():
            try:
                armature[prop] = value
            except Exception:
                pass
        try:
            armature.update_tag()
        except Exception:
            pass
        _view_update(context)
        _restore_mode(context, previous_mode)
    return IkRigResult(
        True,
        f"Calibrated pole angles for {len(calibrated)}/{len(LIMB_DEFS)} limbs",
        changed=calibrated,
    )


def snap_fk_to_ik(context, armature, limbs, frame, key=False) -> IkRigResult:
    validation = validate_human_armature(armature)
    if not validation:
        return validation
    limbs = _normalized_limbs(limbs, include_body=True)
    required_controls = []
    for limb_def in LIMB_DEFS.values():
        required_controls.extend(
            (limb_def["target"], limb_def["aim"], limb_def["chain"], limb_def["pole"], limb_def["mch"])
        )
        for key in ("heel", "toe_roll", "ankle"):
            name = limb_def.get(key)
            if name:
                required_controls.append(name)
    if not all(armature.pose.bones.get(name) for name in required_controls):
        created = create_ik_rig(context, armature, options={})
        if not created:
            return created
    current_frame = context.scene.frame_current
    target_frame = int(round(frame))
    previous_mode = _active_mode()
    max_position = max_rotation = 0.0
    try:
        context.scene.frame_set(target_frame)
        _enter_mode(context, armature, "POSE")
        _set_limb_props(armature, limbs, 0.0, include_body=True)
        _view_update(context)
        fk_matrices = _pose_matrices_for_bones(armature, _controlled_game_bones_for_limbs(armature, limbs))
        changed = _set_controls_from_current_pose(armature, limbs, include_body=True)
        _set_limb_props(armature, limbs, 1.0, include_body=True)
        _apply_twist_corrections(context, armature, limbs, fk_matrices)
        max_position, max_rotation = _pose_deviation(armature, fk_matrices)
        if key:
            _key_pose_bones(armature, changed, frame)
            _key_limb_props(armature, limbs, frame, include_body=True)
            _set_action_interpolation(_active_action(armature), armature)
        _view_update(context)
    finally:
        if int(current_frame) != target_frame:
            context.scene.frame_set(current_frame)
        _restore_mode(context, previous_mode)
    return IkRigResult(
        True,
        f"Snapped FK to IK ({len(changed)} controls, max deviation {max_position * 1000.0:.2f} mm / {max_rotation:.2f} deg)",
        changed=changed,
        details={"max_position_error": max_position, "max_rotation_error_deg": max_rotation},
    )


def snap_ik_to_fk(context, armature, limbs, frame, key=False) -> IkRigResult:
    validation = validate_human_armature(armature)
    if not validation:
        return validation
    limbs = _normalized_limbs(limbs, include_body=True)
    current_frame = context.scene.frame_current
    target_frame = int(round(frame))
    previous_mode = _active_mode()
    try:
        context.scene.frame_set(target_frame)
        _enter_mode(context, armature, "POSE")
        _set_limb_props(armature, limbs, 1.0, include_body=True)
        _view_update(context)
        matrices = _store_pose_matrices(armature, sync_markers=True)
        _set_limb_props(armature, limbs, 0.0, include_body=True)
        _view_update(context)
        keyed = _apply_pose_matrices(armature, matrices, frame) if key else []
        if not key:
            for bone_name, matrix in matrices.items():
                pose_bone = armature.pose.bones.get(bone_name)
                if pose_bone is not None:
                    pose_bone.matrix = matrix.copy()
        if key:
            _key_limb_props(armature, limbs, frame, include_body=True)
            _set_action_interpolation(_active_action(armature), armature)
        _view_update(context)
    finally:
        if int(current_frame) != target_frame:
            context.scene.frame_set(current_frame)
        _restore_mode(context, previous_mode)
    count = len(keyed) if key else len(matrices)
    return IkRigResult(True, f"Snapped IK to FK ({count} bones)", changed=keyed)


def bake_fk_to_ik(context, armature, frame_start, frame_end, limbs, clear_keys=True) -> IkRigResult:
    validation = validate_human_armature(armature)
    if not validation:
        return validation
    created = create_ik_rig(context, armature, options={})
    if not created:
        return created
    limbs = _normalized_limbs(limbs, include_body=True)
    controlled_game_bones = _controlled_game_bones_for_limbs(armature, limbs)
    frame_start, frame_end = _frame_range(frame_start, frame_end)
    current_frame = context.scene.frame_current
    previous_mode = _active_mode()
    action = _active_action(armature)
    cleared = 0
    cleared_fk = 0
    if clear_keys and action is not None:
        cleared = _clear_matching_action_fcurves(action, armature, _control_fcurve_prefixes())

    samples = []
    keyed_controls = set()
    max_position = max_rotation = 0.0
    worst_frame = frame_start
    try:
        _enter_mode(context, armature, "POSE")
        for frame in range(frame_start, frame_end + 1):
            context.scene.frame_set(frame)
            _set_limb_props(armature, limbs, 0.0, include_body=True)
            _view_update(context)
            fk_matrices = _pose_matrices_for_bones(armature, controlled_game_bones)
            changed = _set_controls_from_current_pose(armature, limbs, include_body=True)
            samples.append((frame, _pose_bases_for_bones(armature, changed), fk_matrices))
            keyed_controls.update(changed)

        action = _active_action(armature)
        if clear_keys and action is not None:
            cleared += _clear_matching_action_fcurves(action, armature, _control_fcurve_prefixes())
        cleared_bones = _start_end_game_bones_for_limbs(armature, limbs)
        cleared_fk = _clear_matching_action_fcurves(_active_action(armature), armature, (), exact_bones=cleared_bones)
        _reset_pose_bone_basis(armature, cleared_bones)

        control_quaternions = {}
        for frame, control_bases, fk_matrices in samples:
            context.scene.frame_set(frame)
            _set_limb_props(armature, limbs, 0.0, include_body=True)
            _view_update(context)
            changed = _apply_control_pose_bases(armature, control_bases)
            _set_limb_props(armature, limbs, 1.0, include_body=True)
            _apply_twist_corrections(context, armature, limbs, fk_matrices)
            _key_pose_bones(armature, changed, frame, previous_quaternions=control_quaternions)
            _key_limb_props(armature, limbs, frame, include_body=True)
            frame_position, frame_rotation = _pose_deviation(armature, fk_matrices)
            if frame_position > max_position or frame_rotation > max_rotation:
                worst_frame = frame
            max_position = max(max_position, frame_position)
            max_rotation = max(max_rotation, frame_rotation)
        _set_action_interpolation(_active_action(armature), armature)
        _view_update(context)
    finally:
        context.scene.frame_set(current_frame)
        _restore_mode(context, previous_mode)
    return IkRigResult(
        True,
        f"Baked FK to IK ({frame_end - frame_start + 1} frames, max deviation "
        f"{max_position * 1000.0:.2f} mm / {max_rotation:.2f} deg at frame {worst_frame})",
        changed=sorted(keyed_controls),
        details={
            "cleared": cleared,
            "cleared_fk": cleared_fk,
            "frame_start": frame_start,
            "frame_end": frame_end,
            "max_position_error": max_position,
            "max_rotation_error_deg": max_rotation,
            "worst_frame": worst_frame,
        },
    )


def bake_ik_to_fk(
    context,
    armature,
    frame_start,
    frame_end,
    limbs,
    new_action=True,
) -> IkRigResult:
    validation = validate_human_armature(armature)
    if not validation:
        return validation
    limbs = _normalized_limbs(limbs, include_body=True)
    if not _has_limb_ik_controls(armature, limbs):
        return IkRigResult(False, "Create IK controls first")
    frame_start, frame_end = _frame_range(frame_start, frame_end)
    current_frame = context.scene.frame_current
    previous_mode = _active_mode()
    source_action = _active_action(armature)
    samples = []
    try:
        _enter_mode(context, armature, "POSE")
        for frame in range(frame_start, frame_end + 1):
            context.scene.frame_set(frame)
            _set_limb_props(armature, limbs, 1.0, include_body=True)
            _view_update(context)
            samples.append((frame, _store_pose_matrices(armature, sync_markers=True)))

        if new_action or source_action is None:
            source_name = getattr(source_action, "name", armature.name)
            target_action = bpy.data.actions.new(f"{source_name}_W3IK_Baked")
            assign_action(armature, target_action)
        else:
            target_action = source_action
            prefixes = _control_fcurve_prefixes()
            _clear_matching_action_fcurves(target_action, armature, prefixes, exact_bones=_game_export_bones(armature))

        _set_limb_props(armature, tuple(LIMB_DEFS), 0.0, include_body=False)
        _view_update(context)
        keyed = set()
        game_quaternions = {}
        for frame, matrices in samples:
            context.scene.frame_set(frame)
            _set_limb_props(armature, tuple(LIMB_DEFS), 0.0, include_body=False)
            _view_update(context)
            keyed.update(_apply_pose_matrices(armature, matrices, frame, previous_quaternions=game_quaternions))
        _set_limb_props(armature, tuple(LIMB_DEFS), 0.0, include_body=False)
        for limb in LIMB_DEFS:
            prop = IK_PROPS[limb]
            try:
                armature.keyframe_insert(data_path=f'["{prop}"]', frame=frame_start)
                armature.keyframe_insert(data_path=f'["{prop}"]', frame=frame_end)
            except Exception:
                pass
        target_action[ACTION_BAKED_PROP] = True
        if source_action is not None:
            target_action[ACTION_SOURCE_PROP] = source_action.name
        _set_action_interpolation(target_action, armature)
        _view_update(context)
    finally:
        context.scene.frame_set(current_frame)
        _restore_mode(context, previous_mode)
    return IkRigResult(
        True,
        f"Baked IK to game action ({frame_end - frame_start + 1} frames)",
        changed=sorted(keyed),
        details={
            "action": getattr(_active_action(armature), "name", ""),
            "source_action": getattr(source_action, "name", ""),
            "frame_start": frame_start,
            "frame_end": frame_end,
        },
    )


def has_unbaked_ik_controls(armature, action) -> bool:
    if action is None:
        return False
    try:
        if bool(action.get(ACTION_BAKED_PROP, False)):
            return False
    except Exception:
        pass
    for fcurve in iter_action_fcurves(action, target=armature):
        data_path = str(getattr(fcurve, "data_path", "") or "")
        if f'pose.bones["{CONTROL_PREFIX}' in data_path:
            return True
        if any(data_path.startswith(f'["{IK_PROPS[limb]}"]') for limb in LIMB_DEFS):
            return True
    return False


def action_frame_range(action, scene=None) -> Tuple[int, int]:
    if action is not None:
        try:
            return _frame_range(action.frame_range[0], action.frame_range[1])
        except Exception:
            pass
    if scene is not None:
        return int(scene.frame_start), int(scene.frame_end)
    return 1, 1
