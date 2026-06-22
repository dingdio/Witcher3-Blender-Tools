"""Apply Unreal animation payloads sent by the Witcher Unreal plugin."""

from __future__ import annotations

import logging
import math
import json
from typing import Any

log = logging.getLogger(__name__)


def _base_bone_name(name: str) -> str:
    return str(name or "").rsplit(":", 1)[-1]


def _vec3(value: Any, default=(0.0, 0.0, 0.0)) -> tuple[float, float, float]:
    if isinstance(value, dict):
        return (
            float(value.get("x", value.get("X", default[0])) or 0.0),
            float(value.get("y", value.get("Y", default[1])) or 0.0),
            float(value.get("z", value.get("Z", default[2])) or 0.0),
        )
    try:
        if len(value) >= 3:
            return (float(value[0]), float(value[1]), float(value[2]))
    except Exception:
        pass
    return (float(default[0]), float(default[1]), float(default[2]))


def _quat_xyzw(value: Any) -> tuple[float, float, float, float]:
    if isinstance(value, dict):
        return (
            float(value.get("x", value.get("X", 0.0)) or 0.0),
            float(value.get("y", value.get("Y", 0.0)) or 0.0),
            float(value.get("z", value.get("Z", 0.0)) or 0.0),
            float(value.get("w", value.get("W", 1.0)) or 0.0),
        )
    try:
        if len(value) >= 4:
            return (float(value[0]), float(value[1]), float(value[2]), float(value[3]))
    except Exception:
        pass
    return (0.0, 0.0, 0.0, 1.0)


def _frame_value(values: Any, frame_index: int, default: Any) -> Any:
    if not isinstance(values, (list, tuple)) or not values:
        return default
    return values[frame_index] if frame_index < len(values) else values[-1]


def _bone_depth(pose_bone) -> int:
    depth = 0
    parent = getattr(pose_bone, "parent", None)
    while parent is not None:
        depth += 1
        parent = getattr(parent, "parent", None)
    return depth


def _rest_local_matrix(pose_bone):
    parent = getattr(pose_bone, "parent", None)
    if parent is not None:
        return parent.bone.matrix_local.inverted() @ pose_bone.bone.matrix_local
    return pose_bone.bone.matrix_local.copy()


def _scene_nla_mode(scene) -> str:
    mode_map = {
        "REPLACE": "replace",
        "APPEND": "append",
        "APPEND_AT_CURSOR": "append_at_cursor",
    }
    return mode_map.get(str(getattr(scene, "witcher_anim_nla_mode", "REPLACE") or "REPLACE"), "replace")


def _quat_is_zero(quat) -> bool:
    return quat.dot(quat) <= 1e-12


def _ue_quat_to_blender(quat_xyzw, axis_flip):
    from mathutils import Quaternion

    x, y, z, w = _quat_xyzw(quat_xyzw)
    quat = Quaternion((w, x, y, z))
    if _quat_is_zero(quat):
        quat = Quaternion((1.0, 0.0, 0.0, 0.0))
    else:
        quat.normalize()
    matrix = axis_flip @ quat.to_matrix().to_4x4() @ axis_flip
    result = matrix.to_quaternion()
    if _quat_is_zero(result):
        return Quaternion((1.0, 0.0, 0.0, 0.0))
    result.normalize()
    return result


def _ue_local_matrix(track: dict[str, Any], frame_index: int, translation_scale: float, axis_flip):
    from mathutils import Matrix, Vector

    pos = _vec3(_frame_value(track.get("positions"), frame_index, (0.0, 0.0, 0.0)))
    loc = Vector((
        pos[0] * translation_scale,
        -pos[1] * translation_scale,
        pos[2] * translation_scale,
    ))
    rot = _ue_quat_to_blender(_frame_value(track.get("rotations"), frame_index, (0.0, 0.0, 0.0, 1.0)), axis_flip)
    return Matrix.Translation(loc) @ rot.to_matrix().to_4x4()


def _ue_pose_row_matrix(pose_row: Any, translation_scale: float, axis_flip):
    from mathutils import Matrix, Vector

    pos = _vec3(pose_row, (0.0, 0.0, 0.0))
    quat_xyzw = (0.0, 0.0, 0.0, 1.0)
    try:
        if len(pose_row) >= 7:
            quat_xyzw = pose_row[3:7]
    except Exception:
        pass
    loc = Vector((
        pos[0] * translation_scale,
        -pos[1] * translation_scale,
        pos[2] * translation_scale,
    ))
    rot = _ue_quat_to_blender(quat_xyzw, axis_flip)
    return Matrix.Translation(loc) @ rot.to_matrix().to_4x4()


def _source_skeleton_data(payload: dict[str, Any], translation_scale: float, axis_flip) -> dict[str, Any]:
    source_skeleton = payload.get("source_skeleton")
    if not isinstance(source_skeleton, dict):
        return {}

    names = source_skeleton.get("names") or ()
    parents = source_skeleton.get("parents") or ()
    poses = source_skeleton.get("poses") or ()
    if not isinstance(names, (list, tuple)) or not isinstance(parents, (list, tuple)) or not isinstance(poses, (list, tuple)):
        return {}

    out_names = []
    out_parents = []
    rest_local = []
    name_to_index = {}
    for index, name in enumerate(names):
        if index >= len(poses):
            break
        key = _base_bone_name(str(name or "")).lower()
        if not key:
            continue
        out_names.append(str(name or ""))
        try:
            parent_index = int(float(parents[index]))
        except Exception:
            parent_index = -1
        out_parents.append(parent_index if 0 <= parent_index < index else -1)
        rest_local.append(_ue_pose_row_matrix(poses[index], translation_scale, axis_flip))
        name_to_index[key] = len(out_names) - 1

    return {
        "names": out_names,
        "parents": out_parents,
        "rest_local": rest_local,
        "name_to_index": name_to_index,
    }


def _matrix_quat(matrix):
    quat = matrix.to_quaternion()
    if _quat_is_zero(quat):
        from mathutils import Quaternion
        return Quaternion((1.0, 0.0, 0.0, 0.0))
    quat.normalize()
    return quat


def _pose_basis_from_local(rest_local, local_pos, local_rot):
    from mathutils import Matrix

    local_matrix = Matrix.Translation(local_pos) @ local_rot.to_matrix().to_4x4()
    basis_matrix = rest_local.inverted() @ local_matrix
    basis_rot = _matrix_quat(basis_matrix)
    return basis_matrix.translation.copy(), basis_rot


_POSITION_TRANSFER_BONES = {"root", "trajectory", "pelvis", "l_weapon", "r_weapon"}
_CORE_BONE_KEYS = ("root", "trajectory", "pelvis", "l_weapon", "r_weapon")


def _preview_source_to_target_basis(preview_yaw_degrees: float):
    from mathutils import Matrix

    if abs(float(preview_yaw_degrees or 0.0)) <= 1e-6:
        return Matrix.Identity(4).to_quaternion()
    # Undo preview-facing yaw for target-armature deltas.
    quat = Matrix.Rotation(math.radians(-float(preview_yaw_degrees)), 4, "Z").to_quaternion()
    if _quat_is_zero(quat):
        return Matrix.Identity(4).to_quaternion()
    quat.normalize()
    return quat


def _animation_timing(payload: dict[str, Any], tracks: list[dict[str, Any]]) -> tuple[int, float, float, float]:
    num_frames = int(float(payload.get("num_frames", 0) or 0))
    if num_frames <= 0:
        for track in tracks:
            num_frames = max(num_frames, len(track.get("positions") or ()), len(track.get("rotations") or ()))
    num_frames = max(1, num_frames)

    fps = float(payload.get("fps", 0.0) or 0.0)
    dt = float(payload.get("dt", 0.0) or 0.0)
    duration = float(payload.get("duration", 0.0) or 0.0)

    if fps <= 0.0 and dt > 0.0:
        fps = 1.0 / dt
    if fps <= 0.0:
        fps = 30.0
    if dt <= 0.0:
        dt = 1.0 / fps
    if duration <= 0.0:
        duration = (num_frames - 1) * dt if num_frames > 1 else 0.0

    return num_frames, duration, fps, dt


def _build_animation_entry(target_armature, payload: dict[str, Any]):
    from mathutils import Matrix
    from ..CR2W import w3_types

    tracks = [track for track in (payload.get("tracks") or []) if isinstance(track, dict)]
    if not tracks:
        raise RuntimeError("Unreal animation payload has no bone tracks.")

    num_frames, duration, fps, dt = _animation_timing(payload, tracks)
    translation_scale = float(payload.get("translation_scale", 0.01) or 0.01)
    preview_yaw = float(payload.get("preview_facing_yaw_degrees", 0.0) or 0.0)
    source_to_target_basis = _preview_source_to_target_basis(preview_yaw)
    target_to_source_basis = source_to_target_basis.inverted()

    axis_flip = Matrix((
        (1.0, 0.0, 0.0, 0.0),
        (0.0, -1.0, 0.0, 0.0),
        (0.0, 0.0, 1.0, 0.0),
        (0.0, 0.0, 0.0, 1.0),
    ))
    source_skeleton = _source_skeleton_data(payload, translation_scale, axis_flip)
    source_rest_local = source_skeleton.get("rest_local") or []
    source_parents = source_skeleton.get("parents") or []
    source_name_to_index = source_skeleton.get("name_to_index") or {}
    use_rest_delta = bool(source_rest_local and source_name_to_index)
    legacy_facing_fix = (
        Matrix.Rotation(math.radians(preview_yaw), 4, "Z")
        if not use_rest_delta and abs(preview_yaw) > 1e-6 else None
    )

    tracks_by_name = {
        _base_bone_name(track.get("bone", "")).lower(): track
        for track in tracks
        if str(track.get("bone", "") or "").strip()
    }
    component_tracks = [track for track in (payload.get("component_tracks") or []) if isinstance(track, dict)]
    component_tracks_by_name = {
        _base_bone_name(track.get("bone", "")).lower(): track
        for track in component_tracks
        if str(track.get("bone", "") or "").strip()
    }
    pose_bones = sorted(list(target_armature.pose.bones), key=_bone_depth)
    rest_local = {bone.name: _rest_local_matrix(bone) for bone in pose_bones}
    target_rest_world = {bone.name: bone.bone.matrix_local.copy() for bone in pose_bones}
    target_rest_world_rot = {bone.name: _matrix_quat(target_rest_world[bone.name]) for bone in pose_bones}
    target_rest_world_pos = {bone.name: target_rest_world[bone.name].translation.copy() for bone in pose_bones}
    target_rest_local_pos = {bone.name: rest_local[bone.name].translation.copy() for bone in pose_bones}
    target_rest_local_rot = {bone.name: _matrix_quat(rest_local[bone.name]) for bone in pose_bones}
    use_component_space = bool(use_rest_delta and component_tracks_by_name)

    output_by_bone: dict[str, dict[str, Any]] = {}
    for pose_bone in pose_bones:
        key = _base_bone_name(pose_bone.name).lower()
        if key in tracks_by_name and (not use_rest_delta or key in source_name_to_index):
            output_by_bone[pose_bone.name] = {
                "bone_name": pose_bone.name,
                "positions": [],
                "rotations": [],
                "prev_rot": None,
                "basis_positions": [],
                "basis_rotations": [],
                "prev_basis_rot": None,
            }

    if not output_by_bone:
        raise RuntimeError("No Unreal animation tracks match the active Blender armature.")

    target_keys = {_base_bone_name(bone.name).lower(): bone.name for bone in pose_bones}
    matched_target_keys = {_base_bone_name(name).lower() for name in output_by_bone}
    source_track_keys = set(tracks_by_name)
    component_track_keys = set(component_tracks_by_name)
    core_target_keys = [key for key in _CORE_BONE_KEYS if key in target_keys]
    missing_core_track_keys = [key for key in core_target_keys if key not in source_track_keys]
    missing_core_component_keys = [key for key in core_target_keys if use_component_space and key not in component_track_keys]
    unmatched_target_keys = [
        key for key in target_keys
        if key not in source_track_keys or (use_rest_delta and key not in source_name_to_index)
    ]

    pose_matrices_by_bone: dict[str, list[Any]] = {
        bone_name: [] for bone_name in output_by_bone
    }

    source_rest_world_rot = []
    source_rest_world_pos = []
    for source_index, source_rest in enumerate(source_rest_local):
        parent_index = source_parents[source_index] if source_index < len(source_parents) else -1
        local_rot = _matrix_quat(source_rest)
        if 0 <= parent_index < len(source_rest_world_rot):
            parent_rot = source_rest_world_rot[parent_index]
            world_rot = parent_rot @ local_rot
            world_pos = source_rest_world_pos[parent_index] + (parent_rot @ source_rest.translation)
        else:
            world_rot = local_rot
            world_pos = source_rest.translation.copy()
        if _quat_is_zero(world_rot):
            world_rot = Matrix.Identity(4).to_quaternion()
        else:
            world_rot.normalize()
        source_rest_world_rot.append(world_rot)
        source_rest_world_pos.append(world_pos)

    for frame_index in range(num_frames):
        source_world_rot = []
        source_local_pos = []
        source_world_pos = []
        if use_rest_delta:
            for source_index, source_rest in enumerate(source_rest_local):
                source_name = source_skeleton["names"][source_index]
                source_key = _base_bone_name(source_name).lower()
                if use_component_space:
                    component_track = component_tracks_by_name.get(source_key)
                    if component_track is not None:
                        source_anim_world = _ue_local_matrix(component_track, frame_index, translation_scale, axis_flip)
                        world_rot = _matrix_quat(source_anim_world)
                        world_pos = source_anim_world.translation.copy()
                    else:
                        world_rot = source_rest_world_rot[source_index]
                        world_pos = source_rest_world_pos[source_index].copy()
                    local_track = tracks_by_name.get(source_key)
                    source_anim_local = (
                        _ue_local_matrix(local_track, frame_index, translation_scale, axis_flip)
                        if local_track is not None else source_rest
                    )
                else:
                    track = tracks_by_name.get(source_key)
                    source_anim_local = (
                        _ue_local_matrix(track, frame_index, translation_scale, axis_flip)
                        if track is not None else source_rest
                    )
                    local_rot = _matrix_quat(source_anim_local)
                    parent_index = source_parents[source_index] if source_index < len(source_parents) else -1
                    if 0 <= parent_index < len(source_world_rot):
                        world_rot = source_world_rot[parent_index] @ local_rot
                        world_pos = source_world_pos[parent_index] + (source_world_rot[parent_index] @ source_anim_local.translation)
                    else:
                        world_rot = local_rot
                        world_pos = source_anim_local.translation.copy()
                if _quat_is_zero(world_rot):
                    world_rot = Matrix.Identity(4).to_quaternion()
                else:
                    world_rot.normalize()
                source_world_rot.append(world_rot)
                source_world_pos.append(world_pos)
                source_local_pos.append(source_anim_local.translation.copy())

        target_world_rot = {}
        target_world_pos = {}
        for pose_bone in pose_bones:
            key = _base_bone_name(pose_bone.name).lower()
            source_index = source_name_to_index.get(key) if use_rest_delta else None
            parent = getattr(pose_bone, "parent", None)
            parent_world_rot = (
                target_world_rot[parent.name]
                if parent is not None and parent.name in target_world_rot
                else Matrix.Identity(4).to_quaternion()
            )
            parent_world_pos = (
                target_world_pos[parent.name]
                if parent is not None and parent.name in target_world_pos
                else Matrix.Identity(4).translation
            )

            if source_index is not None and source_index < len(source_world_rot) and source_index < len(source_rest_world_rot):
                source_delta_world = source_world_rot[source_index] @ source_rest_world_rot[source_index].inverted()
                source_delta_world = source_to_target_basis @ source_delta_world @ target_to_source_basis
                if _quat_is_zero(source_delta_world):
                    source_delta_world = Matrix.Identity(4).to_quaternion()
                else:
                    source_delta_world.normalize()
                desired_world_rot = source_delta_world @ target_rest_world_rot[pose_bone.name]
                if _quat_is_zero(desired_world_rot):
                    desired_world_rot = Matrix.Identity(4).to_quaternion()
                else:
                    desired_world_rot.normalize()
                local_rot = parent_world_rot.inverted() @ desired_world_rot
                if _quat_is_zero(local_rot):
                    local_rot = Matrix.Identity(4).to_quaternion()
                else:
                    local_rot.normalize()

                source_rest_pos = source_rest_local[source_index].translation
                source_pos = source_local_pos[source_index]
                target_rest_pos = target_rest_local_pos[pose_bone.name]
                if key in _POSITION_TRANSFER_BONES:
                    if use_component_space and source_index < len(source_world_pos) and source_index < len(source_rest_world_pos):
                        desired_world_pos = target_rest_world_pos[pose_bone.name] + (
                            source_to_target_basis @ (source_world_pos[source_index] - source_rest_world_pos[source_index])
                        )
                        local_pos = parent_world_rot.inverted() @ (desired_world_pos - parent_world_pos)
                    else:
                        local_pos = target_rest_pos + (source_to_target_basis @ (source_pos - source_rest_pos))
                else:
                    local_pos = target_rest_pos.copy()
            else:
                local_rot = target_rest_local_rot[pose_bone.name]
                local_pos = target_rest_local_pos[pose_bone.name].copy()
                if not use_rest_delta:
                    track = tracks_by_name.get(key)
                    if track is not None:
                        local_matrix = _ue_local_matrix(track, frame_index, translation_scale, axis_flip)
                        if legacy_facing_fix is not None and parent is None:
                            local_matrix = legacy_facing_fix @ local_matrix
                        local_rot = _matrix_quat(local_matrix)
                        local_pos = local_matrix.translation.copy()

            desired_world_rot = parent_world_rot @ local_rot
            if _quat_is_zero(desired_world_rot):
                desired_world_rot = Matrix.Identity(4).to_quaternion()
            else:
                desired_world_rot.normalize()
            target_world_rot[pose_bone.name] = desired_world_rot
            target_world_pos[pose_bone.name] = parent_world_pos + (parent_world_rot @ local_pos)

            output = output_by_bone.get(pose_bone.name)
            if output is None:
                continue

            pose_matrices_by_bone[pose_bone.name].append(
                Matrix.Translation(target_world_pos[pose_bone.name]) @ desired_world_rot.to_matrix().to_4x4()
            )

            loc = local_pos
            rot = local_rot
            if _quat_is_zero(rot):
                rot = Matrix.Identity(4).to_quaternion()
            else:
                rot.normalize()
            basis_loc, basis_rot = _pose_basis_from_local(rest_local[pose_bone.name], loc, rot)
            prev_rot = output.get("prev_rot")
            if prev_rot is not None and prev_rot.dot(rot) < 0.0:
                rot = -rot
            output["prev_rot"] = rot.copy()
            prev_basis_rot = output.get("prev_basis_rot")
            if prev_basis_rot is not None and prev_basis_rot.dot(basis_rot) < 0.0:
                basis_rot = -basis_rot
            output["prev_basis_rot"] = basis_rot.copy()
            output["positions"].append([float(loc.x), float(loc.y), float(loc.z)])
            output["rotations"].append(w3_types.Quaternion(float(rot.x), float(rot.y), float(rot.z), float(rot.w)))
            output["basis_positions"].append([float(basis_loc.x), float(basis_loc.y), float(basis_loc.z)])
            output["basis_rotations"].append([float(basis_rot.w), float(basis_rot.x), float(basis_rot.y), float(basis_rot.z)])

    bones = []
    for bone_id, data in enumerate(output_by_bone.values()):
        positions = data["positions"] or [[0.0, 0.0, 0.0]]
        rotations = data["rotations"] or [w3_types.Quaternion(0.0, 0.0, 0.0, 1.0)]
        bones.append(w3_types.w2AnimsFrames(
            bone_id,
            BoneName=data["bone_name"],
            position_dt=dt,
            position_numFrames=len(positions),
            positionFrames=positions,
            rotation_dt=dt,
            rotation_numFrames=len(rotations),
            rotationFrames=rotations,
            scale_dt=dt,
            scale_numFrames=1,
            scaleFrames=[[1.0, 1.0, 1.0]],
            rotationFramesQuat=rotations,
        ))

    anim_buffer = w3_types.CAnimationBufferBitwiseCompressed(
        bones=bones,
        tracks=[],
        duration=duration,
        numFrames=num_frames,
        dt=dt,
        version=0,
    )
    anim_name = str(payload.get("name") or "UnrealAnimation")
    animation = w3_types.CSkeletalAnimation(
        name=anim_name,
        duration=duration,
        framesPerSecond=fps,
        animBuffer=anim_buffer,
        motionExtraction={},
        SkeletalAnimationType="SAT_Normal",
        AdditiveType=None,
    )
    return w3_types.CSkeletalAnimationSetEntry(animation=animation, entries=[]), {
        "source_tracks": len(tracks),
        "matched_tracks": len(bones),
        "num_frames": num_frames,
        "duration": duration,
        "fps": fps,
        "preview_facing_yaw_degrees": preview_yaw,
        "source_to_target_basis_yaw_degrees": -preview_yaw if use_rest_delta else 0.0,
        "retarget_mode": (
            "component_world_delta_to_target"
            if use_component_space else ("world_delta_to_target" if use_rest_delta else "legacy_direct_source_local")
        ),
        "frame_space": "target_track_local",
        "payload_space": str(payload.get("space") or ""),
        "preferred_pose_space": str(payload.get("preferred_pose_space") or ""),
        "pose_space_source": "component_tracks" if use_component_space else "tracks",
        "track_source": str(payload.get("track_source") or ""),
        "asset_path": str(payload.get("asset_path") or ""),
        "skeleton_path": str(payload.get("skeleton_path") or ""),
        "source_skeleton_path": str((payload.get("source_skeleton") or {}).get("path") or "") if isinstance(payload.get("source_skeleton"), dict) else "",
        "source_skeleton_bones": len(source_rest_local),
        "component_tracks": len(component_tracks),
        "component_track_matches": len(matched_target_keys.intersection(component_track_keys)),
        "core_bones_present": [target_keys[key] for key in core_target_keys],
        "core_bones_missing_tracks": [target_keys[key] for key in missing_core_track_keys],
        "core_bones_missing_component_tracks": [target_keys[key] for key in missing_core_component_keys],
        "matched_bone_names_sample": list(output_by_bone.keys())[:24],
        "unmatched_target_bones_sample": [target_keys[key] for key in unmatched_target_keys[:24]],
        "position_transfer_bones": sorted(_POSITION_TRANSFER_BONES),
    }, pose_matrices_by_bone, {
        bone_name: {
            "positions": data["basis_positions"],
            "rotations": data["basis_rotations"],
        }
        for bone_name, data in output_by_bone.items()
    }


def _iter_pose_frame_indices(num_frames: int):
    for frame_index in range(max(1, int(num_frames or 1))):
        yield frame_index, float(frame_index)


def _set_pose_to_rest(target_armature):
    from mathutils import Matrix

    for pose_bone in target_armature.pose.bones:
        try:
            pose_bone.matrix_basis = Matrix.Identity(4)
        except Exception:
            pass


def _linearize_action(action, target_armature):
    from ..action_compat import iter_action_fcurves

    for fcurve in iter_action_fcurves(action, target=target_armature):
        for point in getattr(fcurve, "keyframe_points", []) or []:
            point.interpolation = "LINEAR"


def _set_curve_samples(fcurve, samples):
    points = getattr(fcurve, "keyframe_points", None)
    if points is None:
        return
    samples = list(samples or ())
    if not samples:
        return
    start_index = len(points)
    points.add(len(samples))
    for offset, sample in enumerate(samples):
        point = points[start_index + offset]
        frame, value = sample
        point.co = (float(frame), float(value))
        point.interpolation = "LINEAR"
    try:
        fcurve.update()
    except Exception:
        pass


def _set_scene_range_from_target(scene, target_armature, fallback_start: float, fallback_end: float, fps: float):
    if scene is None:
        return None

    start = float(fallback_start)
    end = float(fallback_end)
    anim_data = getattr(target_armature, "animation_data", None)
    if anim_data is not None:
        for track in getattr(anim_data, "nla_tracks", []) or []:
            for strip in getattr(track, "strips", []) or []:
                try:
                    start = min(start, float(getattr(strip, "frame_start", start) or start))
                    end = max(end, float(getattr(strip, "frame_end", end) or end))
                except Exception:
                    pass

    start_i = int(math.floor(start))
    end_i = int(math.ceil(max(end, start)))
    try:
        scene.frame_start = start_i
        scene.frame_end = end_i
    except Exception:
        pass
    try:
        fps_value = float(fps or 0.0)
        if fps_value > 0.0:
            scene.render.fps = max(1, int(round(fps_value)))
            scene.render.fps_base = 1
    except Exception:
        pass
    try:
        if getattr(scene, "rigidbody_world", None) is not None:
            scene.rigidbody_world.point_cache.frame_start = start_i
            scene.rigidbody_world.point_cache.frame_end = end_i
    except Exception:
        pass
    return start_i, end_i


def _apply_action_to_nla(target_armature, action, nla_mode: str, at_frame: float, length: float, track_name="anim_import"):
    from ..action_compat import bind_strip_action_slot, resolve_action_slot

    target_armature.animation_data_create()
    anim_data = target_armature.animation_data
    track = anim_data.nla_tracks.get(track_name)
    if track is None:
        track = anim_data.nla_tracks.new()
        track.name = track_name
    try:
        track.mute = False
    except Exception:
        pass

    strip_length = max(1.0, float(length or 1.0))
    if nla_mode == "append":
        start_frame = float(track.strips[-1].frame_end) if len(track.strips) else 0.0
    elif nla_mode == "append_at_cursor":
        start_frame = float(at_frame or 0.0)
        for strip in sorted(list(track.strips), key=lambda item: float(getattr(item, "frame_start", 0.0) or 0.0), reverse=True):
            if float(getattr(strip, "frame_start", 0.0) or 0.0) >= start_frame:
                strip.frame_start = float(strip.frame_start) + strip_length
                strip.frame_end = float(strip.frame_end) + strip_length
    else:
        start_frame = 0.0
        for strip in list(track.strips):
            track.strips.remove(strip)

    try:
        strip = track.strips.new(action.name, int(round(start_frame)), action)
    except Exception:
        fallback = int(round((track.strips[-1].frame_end if len(track.strips) else start_frame) + 1.0))
        strip = track.strips.new(action.name, fallback, action)
    strip.frame_start = start_frame
    strip.frame_end = start_frame + strip_length
    if hasattr(strip, "action_frame_start"):
        strip.action_frame_start = 0.0
    if hasattr(strip, "action_frame_end"):
        strip.action_frame_end = strip_length
    strip.blend_type = "REPLACE"
    try:
        strip.influence = 1.0
        if hasattr(strip, "use_animated_influence"):
            strip.use_animated_influence = False
    except Exception:
        pass
    bind_strip_action_slot(strip, resolve_action_slot(action, target=target_armature, ensure=True))
    anim_data.action = None
    return strip


def _nla_track_states(anim_data):
    states = []
    for track in getattr(anim_data, "nla_tracks", []) or []:
        state = {"track": track}
        for attr in ("mute", "is_solo"):
            if hasattr(track, attr):
                try:
                    state[attr] = getattr(track, attr)
                except Exception:
                    pass
        states.append(state)
    return states


def _restore_nla_track_states(states):
    for state in states:
        track = state.get("track")
        if track is None:
            continue
        for attr in ("mute", "is_solo"):
            if attr in state and hasattr(track, attr):
                try:
                    setattr(track, attr, state[attr])
                except Exception:
                    pass


def _mute_nla_tracks(anim_data):
    for track in getattr(anim_data, "nla_tracks", []) or []:
        try:
            track.mute = True
        except Exception:
            pass


def _apply_basis_tracks_as_action(context, target_armature, basis_tracks_by_bone, payload, stats, nla_mode, at_frame):
    import bpy
    from ..action_compat import assign_action, new_action_fcurve

    name = str(payload.get("name") or "UnrealAnimation")
    action = bpy.data.actions.new(name)
    target_armature.animation_data_create()
    anim_data = target_armature.animation_data
    previous_action = getattr(anim_data, "action", None)
    previous_slot = getattr(anim_data, "action_slot", None) if hasattr(anim_data, "action_slot") else None
    track_states = _nla_track_states(anim_data)
    _mute_nla_tracks(anim_data)
    assign_action(target_armature, action)

    pose_bones = sorted(list(target_armature.pose.bones), key=_bone_depth)
    keyed_bone_names = [bone.name for bone in pose_bones if bone.name in basis_tracks_by_bone]
    num_frames = int(stats.get("num_frames", 0) or 0)
    previous_frame = getattr(getattr(context, "scene", None), "frame_current", None)
    frame_samples = list(_iter_pose_frame_indices(num_frames))

    for pose_bone in target_armature.pose.bones:
        pose_bone.rotation_mode = "QUATERNION"

    try:
        for bone_name in keyed_bone_names:
            pose_bone = target_armature.pose.bones.get(bone_name)
            track = basis_tracks_by_bone.get(bone_name) or {}
            positions = list(track.get("positions") or ())
            rotations = list(track.get("rotations") or ())
            if pose_bone is None or not positions or not rotations:
                continue

            loc_curves = [
                new_action_fcurve(
                    action,
                    target_armature,
                    data_path=f'pose.bones["{bone_name}"].location',
                    index=axis,
                    group_name=bone_name,
                )
                for axis in range(3)
            ]
            rot_curves = [
                new_action_fcurve(
                    action,
                    target_armature,
                    data_path=f'pose.bones["{bone_name}"].rotation_quaternion',
                    index=axis,
                    group_name=bone_name,
                )
                for axis in range(4)
            ]

            for axis in range(3):
                _set_curve_samples(
                    loc_curves[axis],
                    (
                        (frame, (positions[frame_index] if frame_index < len(positions) else positions[-1])[axis])
                        for frame_index, frame in frame_samples
                    ),
                )
            for axis in range(4):
                _set_curve_samples(
                    rot_curves[axis],
                    (
                        (frame, (rotations[frame_index] if frame_index < len(rotations) else rotations[-1])[axis])
                        for frame_index, frame in frame_samples
                    ),
                )
    finally:
        if not nla_mode:
            try:
                anim_data.action = action
            except Exception:
                pass
        elif previous_action is not None:
            try:
                anim_data.action = previous_action
                if previous_slot is not None and hasattr(anim_data, "action_slot"):
                    anim_data.action_slot = previous_slot
            except Exception:
                pass
        else:
            try:
                anim_data.action = None
            except Exception:
                pass

    _linearize_action(action, target_armature)
    stats["action_name"] = action.name
    stats["keyed_bones"] = len(keyed_bone_names)
    stats["apply_method"] = "direct_fcurve_nla"
    stats["action_curve_space"] = "pose_basis"
    strip = None

    if nla_mode:
        strip = _apply_action_to_nla(
            target_armature,
            action,
            nla_mode,
            at_frame,
            max(1.0, float(max(1, num_frames) - 1)),
            track_name="anim_import",
        )
        _restore_nla_track_states(track_states)
        new_track = target_armature.animation_data.nla_tracks.get("anim_import") if target_armature.animation_data else None
        if new_track is not None:
            try:
                new_track.mute = False
            except Exception:
                pass
        stats["nla_strip_start"] = float(getattr(strip, "frame_start", 0.0) or 0.0) if strip is not None else 0.0
        stats["nla_strip_end"] = float(getattr(strip, "frame_end", 0.0) or 0.0) if strip is not None else 0.0

    range_result = _set_scene_range_from_target(
        getattr(context, "scene", None),
        target_armature,
        float(getattr(strip, "frame_start", 0.0) or 0.0) if strip is not None else float(action.frame_range[0]),
        float(getattr(strip, "frame_end", 0.0) or 0.0) if strip is not None else float(action.frame_range[1]),
        float(stats.get("fps", 0.0) or 0.0),
    )
    if range_result is not None:
        stats["scene_frame_start"], stats["scene_frame_end"] = range_result

    if previous_frame is not None and nla_mode != "replace":
        try:
            context.scene.frame_set(int(previous_frame))
        except Exception:
            pass
    elif getattr(context, "scene", None) is not None:
        try:
            context.scene.frame_set(int(getattr(context.scene, "frame_start", 0) or 0))
        except Exception:
            pass

    return action


def apply_unreal_animation_payload(context, payload: dict[str, Any]) -> dict[str, Any]:
    from ..ui.armature_context import get_main_armature, set_main_armature

    scene = getattr(context, "scene", None)
    target_armature = get_main_armature(
        context,
        prefer_active=True,
        remember=True,
        fallback=True,
        allow_auxiliary_active=True,
    )
    if target_armature is None or getattr(target_armature, "type", None) != "ARMATURE":
        raise RuntimeError("Select or set a Witcher armature in Blender before sending an Unreal animation.")

    try:
        set_main_armature(scene, target_armature)
    except Exception:
        pass

    _entry, stats, _pose_matrices_by_bone, basis_tracks_by_bone = _build_animation_entry(target_armature, payload)
    nla_mode = _scene_nla_mode(scene)
    at_frame = float(getattr(scene, "frame_current", 0.0) or 0.0) if nla_mode == "append_at_cursor" else 0.0
    source_path = str(payload.get("asset_path") or "unreal_animation")

    _apply_basis_tracks_as_action(
        context,
        target_armature,
        basis_tracks_by_bone,
        payload,
        stats,
        nla_mode,
        at_frame,
    )

    should_auto_orient = (
        bool(getattr(scene, "witcher_auto_orient_root", True))
        and stats.get("apply_method") not in {"direct_pose_nla", "direct_fcurve_nla"}
    )
    stats["auto_orient_root_applied"] = False
    stats["auto_orient_root_skipped"] = str(stats.get("apply_method") or "direct") if not should_auto_orient else ""

    if should_auto_orient:
        try:
            from ..ui.ui_anims import apply_root_orientation
            stats["auto_orient_root_applied"] = bool(apply_root_orientation(target_armature))
            stats["auto_orient_root_skipped"] = "" if stats["auto_orient_root_applied"] else "not_applied"
        except Exception as exc:
            stats["auto_orient_root_skipped"] = f"error: {exc}"
            log.warning("Unreal animation auto orient failed: %s", exc)

    stats["target"] = target_armature.name
    stats["name"] = str(payload.get("name") or "UnrealAnimation")

    if scene is not None:
        try:
            scene["_w3_last_anim_name"] = str(payload.get("name") or "")
            scene["_w3_last_anim_path"] = source_path
            scene["_w3_last_anim_source_game"] = "unreal"
            scene["_w3_last_anim_fps"] = float(stats["fps"])
            scene["_w3_last_anim_duration"] = float(stats["duration"])
            scene["_w3_last_unreal_anim_stats"] = json.dumps(stats, sort_keys=True)
        except Exception:
            pass
    return stats
