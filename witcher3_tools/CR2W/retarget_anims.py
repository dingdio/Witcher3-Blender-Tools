"""Pure data retargeting helpers for Witcher 2 body animations.

The W2 and W3 body rigs are close in proportions but not in local bone axes.
This module transfers animation as model-space rotation deltas and
then writes ordinary W3 local animation tracks.
"""

from __future__ import annotations

import copy
import logging
import math
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from . import w3_types

log = logging.getLogger(__name__)

Vec3 = Tuple[float, float, float]
Quat = Tuple[float, float, float, float]  # x, y, z, w

DEFAULT_W2_WOMAN_RIG = r"characters\templates\woman\model\woman.w2rig"
DEFAULT_W2_MAN_RIG = r"characters\templates\man\model\man_rig.w2rig"
DEFAULT_W2_WITCHER_RIG = r"characters\templates\witcher\model\witcher_without_ponytail.w2rig"

_EPSILON = 1e-8
_ROOT_POSITION_BONES = {"trajectory", "pelvis"}
_ATTACHMENT_TRANSFER_BONES = {"l_weapon", "r_weapon"}


def _get_attr_or_item(value, names, default=0.0):
    for name in names:
        if isinstance(name, str) and hasattr(value, name):
            return getattr(value, name)
        if isinstance(name, str) and isinstance(value, dict) and name in value:
            return value[name]
        if isinstance(name, int):
            try:
                return value[name]
            except Exception:
                pass
    return default


def _vec3(value, default=(0.0, 0.0, 0.0)) -> Vec3:
    if value is None:
        return tuple(default)
    try:
        return (
            float(_get_attr_or_item(value, ("X", "x", 0), default[0])),
            float(_get_attr_or_item(value, ("Y", "y", 1), default[1])),
            float(_get_attr_or_item(value, ("Z", "z", 2), default[2])),
        )
    except Exception:
        return tuple(default)


def _quat(value, default=(0.0, 0.0, 0.0, 1.0)) -> Quat:
    if value is None:
        return tuple(default)
    try:
        return _quat_normalize((
            float(_get_attr_or_item(value, ("X", "x", 0), default[0])),
            float(_get_attr_or_item(value, ("Y", "y", 1), default[1])),
            float(_get_attr_or_item(value, ("Z", "z", 2), default[2])),
            float(_get_attr_or_item(value, ("W", "w", 3), default[3])),
        ))
    except Exception:
        return tuple(default)


def _quat_normalize(q: Quat) -> Quat:
    x, y, z, w = q
    length = math.sqrt(x * x + y * y + z * z + w * w)
    if length <= _EPSILON:
        return (0.0, 0.0, 0.0, 1.0)
    inv = 1.0 / length
    return (x * inv, y * inv, z * inv, w * inv)


def _quat_mul(a: Quat, b: Quat) -> Quat:
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return _quat_normalize((
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    ))


def _quat_inv(q: Quat) -> Quat:
    x, y, z, w = _quat_normalize(q)
    return (-x, -y, -z, w)


def _quat_dot(a: Quat, b: Quat) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2] + a[3] * b[3]


def _quat_slerp(a: Quat, b: Quat, t: float) -> Quat:
    a = _quat_normalize(a)
    b = _quat_normalize(b)
    dot = _quat_dot(a, b)
    if dot < 0.0:
        b = (-b[0], -b[1], -b[2], -b[3])
        dot = -dot
    if dot > 0.9995:
        return _quat_normalize((
            a[0] + t * (b[0] - a[0]),
            a[1] + t * (b[1] - a[1]),
            a[2] + t * (b[2] - a[2]),
            a[3] + t * (b[3] - a[3]),
        ))
    theta_0 = math.acos(max(-1.0, min(1.0, dot)))
    theta = theta_0 * t
    sin_theta = math.sin(theta)
    sin_theta_0 = math.sin(theta_0)
    s0 = math.cos(theta) - dot * sin_theta / sin_theta_0
    s1 = sin_theta / sin_theta_0
    return _quat_normalize((
        (s0 * a[0]) + (s1 * b[0]),
        (s0 * a[1]) + (s1 * b[1]),
        (s0 * a[2]) + (s1 * b[2]),
        (s0 * a[3]) + (s1 * b[3]),
    ))


def _quat_rotate_vec(q: Quat, v: Vec3) -> Vec3:
    x, y, z, w = _quat_normalize(q)
    vx, vy, vz = v
    tx = 2.0 * (y * vz - z * vy)
    ty = 2.0 * (z * vx - x * vz)
    tz = 2.0 * (x * vy - y * vx)
    return (
        vx + w * tx + (y * tz - z * ty),
        vy + w * ty + (z * tx - x * tz),
        vz + w * tz + (x * ty - y * tx),
    )


def _vec_lerp(a: Vec3, b: Vec3, t: float) -> Vec3:
    return (
        a[0] + (b[0] - a[0]) * t,
        a[1] + (b[1] - a[1]) * t,
        a[2] + (b[2] - a[2]) * t,
    )


def _vec_add(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _vec_sub(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _frames_all_close(frames: Sequence[Sequence[float]], epsilon=1e-7) -> bool:
    if len(frames) <= 1:
        return True
    first = tuple(float(v) for v in frames[0])
    for frame in frames[1:]:
        if any(abs(float(a) - float(b)) > epsilon for a, b in zip(first, frame)):
            return False
    return True


def _quat_frames_all_close(frames: Sequence[Quat], epsilon=1e-7) -> bool:
    if len(frames) <= 1:
        return True
    first = _quat_normalize(frames[0])
    for frame in frames[1:]:
        q = _quat_normalize(frame)
        if _quat_dot(first, q) < 0.0:
            q = (-q[0], -q[1], -q[2], -q[3])
        if any(abs(a - b) > epsilon for a, b in zip(first, q)):
            return False
    return True


def _sample_vec_track(frames, frame_index: int, base_dt: float, track_dt: float, default: Vec3) -> Vec3:
    frames = list(frames or [])
    if not frames:
        return default
    if len(frames) == 1:
        return _vec3(frames[0], default)
    if track_dt <= 0.0:
        idx = min(frame_index, len(frames) - 1)
        return _vec3(frames[idx], default)
    sample = (float(frame_index) * base_dt) / track_dt
    lo = int(math.floor(sample))
    hi = min(lo + 1, len(frames) - 1)
    lo = max(0, min(lo, len(frames) - 1))
    if hi == lo:
        return _vec3(frames[lo], default)
    return _vec_lerp(_vec3(frames[lo], default), _vec3(frames[hi], default), sample - lo)


def _sample_quat_track(frames, frame_index: int, base_dt: float, track_dt: float, default: Quat) -> Quat:
    frames = list(frames or [])
    if not frames:
        return default
    if len(frames) == 1:
        return _quat(frames[0], default)
    if track_dt <= 0.0:
        idx = min(frame_index, len(frames) - 1)
        return _quat(frames[idx], default)
    sample = (float(frame_index) * base_dt) / track_dt
    lo = int(math.floor(sample))
    hi = min(lo + 1, len(frames) - 1)
    lo = max(0, min(lo, len(frames) - 1))
    if hi == lo:
        return _quat(frames[lo], default)
    return _quat_slerp(_quat(frames[lo], default), _quat(frames[hi], default), sample - lo)


def _skeleton_bones(skeleton) -> List:
    return list(getattr(skeleton, "bones", []) or [])


def _bone_name(bone, fallback="") -> str:
    if isinstance(bone, dict):
        return str(bone.get("name", fallback) or fallback)
    return str(getattr(bone, "name", fallback) or fallback)


def _bone_parent_id(bone) -> int:
    value = bone.get("parentId", -1) if isinstance(bone, dict) else getattr(bone, "parentId", -1)
    try:
        return int(value)
    except Exception:
        return -1


def _bone_rest_pos(bone) -> Vec3:
    value = bone.get("co", None) if isinstance(bone, dict) else getattr(bone, "co", None)
    return _vec3(value)


def _bone_rest_rot(bone) -> Quat:
    value = bone.get("ro_quat", None) if isinstance(bone, dict) else getattr(bone, "ro_quat", None)
    return _quat(value)


def _world_rest_rotations(skeleton) -> List[Quat]:
    bones = _skeleton_bones(skeleton)
    world = []
    for idx, bone in enumerate(bones):
        local = _bone_rest_rot(bone)
        parent_idx = _bone_parent_id(bone)
        if 0 <= parent_idx < idx:
            local = _quat_mul(world[parent_idx], local)
        world.append(local)
    return world


def _animation_frame_count(anim_buffer, bones: Sequence) -> int:
    num_frames = int(getattr(anim_buffer, "numFrames", 0) or 0)
    if num_frames > 0:
        return num_frames
    max_frames = 1
    for bone in bones:
        max_frames = max(
            max_frames,
            len(getattr(bone, "positionFrames", []) or []),
            len(getattr(bone, "rotationFramesQuat", []) or []),
            len(getattr(bone, "rotationFrames", []) or []),
        )
    return max_frames


def _animation_base_dt(animation, anim_buffer, num_frames: int) -> float:
    dt = float(getattr(anim_buffer, "dt", 0.0) or 0.0)
    if dt > 0.0:
        return dt
    duration = float(getattr(animation, "duration", 0.0) or getattr(anim_buffer, "duration", 0.0) or 0.0)
    if duration > 0.0 and num_frames > 1:
        return duration / float(num_frames - 1)
    fps = float(getattr(animation, "framesPerSecond", 30.0) or 30.0)
    return 1.0 / fps if fps > 0.0 else 1.0 / 30.0


def _source_anim_bone_map(anim_buffer) -> Dict[str, object]:
    out = {}
    for bone in list(getattr(anim_buffer, "bones", []) or []):
        name = str(getattr(bone, "BoneName", "") or "").lower()
        if name and name not in out:
            out[name] = bone
    return out


def _sample_source_local(
    anim_bone,
    rest_pos: Vec3,
    rest_rot: Quat,
    frame_index: int,
    base_dt: float,
) -> Tuple[Vec3, Quat]:
    if anim_bone is None:
        return rest_pos, rest_rot
    pos_dt = float(getattr(anim_bone, "position_dt", 0.0) or base_dt)
    rot_dt = float(getattr(anim_bone, "rotation_dt", 0.0) or base_dt)
    rot_frames = getattr(anim_bone, "rotationFramesQuat", None) or getattr(anim_bone, "rotationFrames", None) or []
    return (
        _sample_vec_track(getattr(anim_bone, "positionFrames", []) or [], frame_index, base_dt, pos_dt, rest_pos),
        _sample_quat_track(rot_frames, frame_index, base_dt, rot_dt, rest_rot),
    )


def _default_bone_map(source_skeleton, target_skeleton, overrides=None) -> Dict[str, str]:
    source_names = {_bone_name(bone).lower(): _bone_name(bone) for bone in _skeleton_bones(source_skeleton)}
    out = {}
    for target_bone in _skeleton_bones(target_skeleton):
        target_name = _bone_name(target_bone)
        key = target_name.lower()
        if key in source_names:
            out[target_name] = source_names[key]
    if overrides:
        for target_name, source_name in overrides.items():
            if target_name and source_name:
                out[str(target_name)] = str(source_name)
    return out


def _stable_quat_sequence_append(frames: List[Quat], q: Quat) -> None:
    q = _quat_normalize(q)
    if frames and _quat_dot(frames[-1], q) < 0.0:
        q = (-q[0], -q[1], -q[2], -q[3])
    frames.append(q)


def _apply_in_place_root_motion(
    pos_by_name: Dict[str, List[Vec3]],
    rot_by_name: Dict[str, List[Quat]],
    rest_pos_by_name: Dict[str, Vec3],
    rest_rot_by_name: Dict[str, Quat],
    num_frames: int,
) -> None:
    traj_key = next((name for name in pos_by_name if name.lower() == "trajectory"), None)
    pelvis_key = next((name for name in pos_by_name if name.lower() == "pelvis"), None)
    if not traj_key or not pelvis_key:
        return

    new_traj_pos = rest_pos_by_name.get(traj_key, (0.0, 0.0, 0.0))
    new_traj_rot = rest_rot_by_name.get(traj_key, (0.0, 0.0, 0.0, 1.0))
    new_pelvis_positions = []
    new_pelvis_rotations = []

    for frame_idx in range(num_frames):
        old_traj_pos = pos_by_name[traj_key][frame_idx]
        old_traj_rot = rot_by_name[traj_key][frame_idx]
        old_pelvis_pos = pos_by_name[pelvis_key][frame_idx]
        old_pelvis_rot = rot_by_name[pelvis_key][frame_idx]

        inv_traj_rot = _quat_inv(old_traj_rot)
        pelvis_local_pos = _quat_rotate_vec(inv_traj_rot, _vec_sub(old_pelvis_pos, old_traj_pos))
        pelvis_local_rot = _quat_mul(inv_traj_rot, old_pelvis_rot)

        new_pelvis_positions.append(_vec_add(new_traj_pos, _quat_rotate_vec(new_traj_rot, pelvis_local_pos)))
        _stable_quat_sequence_append(new_pelvis_rotations, _quat_mul(new_traj_rot, pelvis_local_rot))

    pos_by_name[traj_key] = [new_traj_pos for _ in range(num_frames)]
    rot_by_name[traj_key] = [new_traj_rot for _ in range(num_frames)]
    pos_by_name[pelvis_key] = new_pelvis_positions
    rot_by_name[pelvis_key] = new_pelvis_rotations


def _make_anim_bone(
    idx: int,
    name: str,
    pos_frames: List[Vec3],
    rot_frames: List[Quat],
    scale_frames: List[Vec3],
    dt: float,
):
    out_pos = [tuple(float(v) for v in frame) for frame in pos_frames]
    out_rot = [_quat_normalize(frame) for frame in rot_frames]
    out_scale = [tuple(float(v) for v in frame) for frame in scale_frames]
    if _frames_all_close(out_pos):
        out_pos = out_pos[:1]
    if _quat_frames_all_close(out_rot):
        out_rot = out_rot[:1]
    if _frames_all_close(out_scale):
        out_scale = out_scale[:1]
    rot_objs = [w3_types.Quaternion(q[0], q[1], q[2], q[3]) for q in out_rot]
    return w3_types.w2AnimsFrames(
        idx,
        BoneName=name,
        position_dt=dt,
        position_numFrames=len(out_pos),
        positionFrames=[list(frame) for frame in out_pos],
        rotation_dt=dt,
        rotation_numFrames=len(rot_objs),
        rotationFrames=rot_objs,
        scale_dt=dt,
        scale_numFrames=len(out_scale),
        scaleFrames=[list(frame) for frame in out_scale],
        rotationFramesQuat=rot_objs,
    )


def retarget_w2_animation_entry(
    animation_entry,
    source_skeleton,
    target_skeleton,
    *,
    in_place: bool = False,
    bone_map: Optional[Dict[str, str]] = None,
):
    """Return a copy of ``animation_entry`` retargeted onto ``target_skeleton``.

    ``animation_entry`` must contain a decoded W2 animation buffer. The output
    keeps target bone names/order and can be fed into the existing W3 import
    and export paths.
    """
    animation = getattr(animation_entry, "animation", None)
    anim_buffer = getattr(animation, "animBuffer", None)
    if animation is None or anim_buffer is None:
        return animation_entry

    source_bones = _skeleton_bones(source_skeleton)
    target_bones = _skeleton_bones(target_skeleton)
    if not source_bones or not target_bones:
        log.warning("W2 retarget skipped: missing source or target skeleton data.")
        return animation_entry

    source_by_name = {_bone_name(bone).lower(): idx for idx, bone in enumerate(source_bones)}
    target_map = _default_bone_map(source_skeleton, target_skeleton, overrides=bone_map)
    anim_bones_by_name = _source_anim_bone_map(anim_buffer)
    num_frames = _animation_frame_count(anim_buffer, list(anim_bones_by_name.values()))
    base_dt = _animation_base_dt(animation, anim_buffer, num_frames)
    duration = float(getattr(animation, "duration", 0.0) or getattr(anim_buffer, "duration", 0.0) or 0.0)
    if duration <= 0.0 and num_frames > 1:
        duration = base_dt * float(num_frames - 1)

    source_rest_world_rot = _world_rest_rotations(source_skeleton)
    target_rest_world_rot = _world_rest_rotations(target_skeleton)
    source_rest_pos = [_bone_rest_pos(bone) for bone in source_bones]
    source_rest_rot = [_bone_rest_rot(bone) for bone in source_bones]
    target_rest_pos = [_bone_rest_pos(bone) for bone in target_bones]
    target_rest_rot = [_bone_rest_rot(bone) for bone in target_bones]

    yaw_180 = (0.0, 0.0, 1.0, 0.0)
    yaw_180_inv = _quat_inv(yaw_180)

    pos_by_name: Dict[str, List[Vec3]] = {_bone_name(bone): [] for bone in target_bones}
    rot_by_name: Dict[str, List[Quat]] = {_bone_name(bone): [] for bone in target_bones}
    scale_by_name: Dict[str, List[Vec3]] = {_bone_name(bone): [] for bone in target_bones}

    for frame_idx in range(num_frames):
        source_world_rot: List[Quat] = []
        for src_idx, src_bone in enumerate(source_bones):
            src_name = _bone_name(src_bone)
            anim_bone = anim_bones_by_name.get(src_name.lower())
            _pos, local_rot = _sample_source_local(
                anim_bone,
                source_rest_pos[src_idx],
                source_rest_rot[src_idx],
                frame_idx,
                base_dt,
            )
            parent_idx = _bone_parent_id(src_bone)
            if 0 <= parent_idx < src_idx:
                local_rot = _quat_mul(source_world_rot[parent_idx], local_rot)
            source_world_rot.append(local_rot)

        target_world_rot: List[Quat] = []
        for tgt_idx, tgt_bone in enumerate(target_bones):
            target_name = _bone_name(tgt_bone)
            target_key = target_name.lower()
            source_name = target_map.get(target_name, "")
            source_idx = source_by_name.get(str(source_name).lower(), None)
            parent_idx = _bone_parent_id(tgt_bone)
            parent_world_rot = (
                target_world_rot[parent_idx]
                if 0 <= parent_idx < len(target_world_rot)
                else (0.0, 0.0, 0.0, 1.0)
            )

            if target_key == "root":
                local_rot = target_rest_rot[tgt_idx]
                desired_world_rot = _quat_mul(parent_world_rot, local_rot)
            elif target_key in _ATTACHMENT_TRANSFER_BONES and source_idx is not None:
                # W3 weapon attachment bones rest at the wrist; W2 weapon bones
                # are already posed in the hand. Transfer the full W2 hand-space
                # transform instead of only the animation delta from rest.
                desired_world_rot = _quat_mul(_quat_mul(yaw_180, source_world_rot[source_idx]), yaw_180_inv)
                local_rot = _quat_mul(_quat_inv(parent_world_rot), desired_world_rot)
            elif source_idx is not None:
                src_delta_world = _quat_mul(source_world_rot[source_idx], _quat_inv(source_rest_world_rot[source_idx]))
                rotated_delta = _quat_mul(_quat_mul(yaw_180, src_delta_world), yaw_180_inv)
                desired_world_rot = _quat_mul(rotated_delta, target_rest_world_rot[tgt_idx])
                local_rot = _quat_mul(_quat_inv(parent_world_rot), desired_world_rot)
            else:
                local_rot = target_rest_rot[tgt_idx]
                desired_world_rot = _quat_mul(parent_world_rot, local_rot)

            if target_key in _ATTACHMENT_TRANSFER_BONES and source_idx is not None:
                anim_bone = anim_bones_by_name.get(str(source_name).lower())
                source_local_pos, _source_local_rot = _sample_source_local(
                    anim_bone,
                    source_rest_pos[source_idx],
                    source_rest_rot[source_idx],
                    frame_idx,
                    base_dt,
                )
                source_parent_idx = _bone_parent_id(source_bones[source_idx])
                source_parent_world_rot = (
                    source_world_rot[source_parent_idx]
                    if 0 <= source_parent_idx < len(source_world_rot)
                    else (0.0, 0.0, 0.0, 1.0)
                )
                source_offset_world = _quat_rotate_vec(source_parent_world_rot, source_local_pos)
                rotated_offset_world = _quat_rotate_vec(yaw_180, source_offset_world)
                local_pos = _quat_rotate_vec(_quat_inv(parent_world_rot), rotated_offset_world)
            elif target_key in _ROOT_POSITION_BONES and source_idx is not None:
                anim_bone = anim_bones_by_name.get(str(source_name).lower())
                source_local_pos, _source_local_rot = _sample_source_local(
                    anim_bone,
                    source_rest_pos[source_idx],
                    source_rest_rot[source_idx],
                    frame_idx,
                    base_dt,
                )
                local_pos = source_local_pos
            else:
                local_pos = target_rest_pos[tgt_idx]

            target_world_rot.append(desired_world_rot)
            pos_by_name[target_name].append(local_pos)
            _stable_quat_sequence_append(rot_by_name[target_name], local_rot)
            scale_by_name[target_name].append((1.0, 1.0, 1.0))

    rest_pos_by_name = {_bone_name(bone): target_rest_pos[idx] for idx, bone in enumerate(target_bones)}
    rest_rot_by_name = {_bone_name(bone): target_rest_rot[idx] for idx, bone in enumerate(target_bones)}
    if in_place:
        _apply_in_place_root_motion(pos_by_name, rot_by_name, rest_pos_by_name, rest_rot_by_name, num_frames)

    output_bones = []
    for idx, target_bone in enumerate(target_bones):
        name = _bone_name(target_bone, str(idx))
        output_bones.append(
            _make_anim_bone(
                idx,
                name,
                pos_by_name[name],
                rot_by_name[name],
                scale_by_name[name],
                base_dt,
            )
        )

    out_entry = copy.deepcopy(animation_entry)
    out_anim = out_entry.animation
    old_buffer = out_anim.animBuffer
    if getattr(old_buffer, "tracks", None):
        log.debug("Dropping %d W2 float track(s) during body retarget.", len(old_buffer.tracks))
    out_anim.animBuffer = w3_types.CAnimationBufferBitwiseCompressed(
        bones=output_bones,
        tracks=[],
        duration=duration,
        numFrames=num_frames,
        dt=base_dt,
        version=getattr(old_buffer, "version", 0),
    )
    out_anim.duration = duration
    out_anim.framesPerSecond = 1.0 / base_dt if base_dt > 0.0 else float(getattr(out_anim, "framesPerSecond", 30.0) or 30.0)
    out_anim._w2_retargeted_to_w3 = True
    out_anim._w2_retarget_in_place = bool(in_place)
    return out_entry


def infer_w2_source_rig_path(target_rig_path: str, target_name_hint: str = "") -> str:
    """Return a repo-relative W2 body rig candidate for a W3 target rig."""
    text = f"{target_rig_path or ''} {target_name_hint or ''}".lower().replace("/", "\\")
    if (
        "geralt" in text
        or "\\player\\player.w2ent" in text
        or "gameplay\\templates\\characters\\player\\player.w2ent" in text
        or "witcher_without_ponytail" in text
    ):
        return DEFAULT_W2_WITCHER_RIG
    if "woman" in text or "triss" in text or "yennefer" in text or "ciri" in text:
        return DEFAULT_W2_WOMAN_RIG
    if "man" in text or "geralt" in text or "male" in text:
        return DEFAULT_W2_MAN_RIG
    return DEFAULT_W2_WOMAN_RIG
