from __future__ import annotations

import copy
import json
import logging
import math
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

SRC_GUID_PROP = "witcher_src_guid"
SRC_GUIDS_PROP = "witcher_src_guids"
SRC_REFCOUNTS_PROP = "witcher_src_refcounts"
ACTIVE_GRAFT_SRC_PROP = "_witcher_active_graft_src_guid"


@dataclass
class GraftResult:
    merged: bool = False
    added_bones: list[str] = field(default_factory=list)
    shared_bones: list[str] = field(default_factory=list)
    reparented_objects: list[Any] = field(default_factory=list)
    retargeted_objects: list[Any] = field(default_factory=list)
    retargeted_constraints: int = 0
    removed_armature: bool = False


def _skeleton_bone_name(bone, fallback: str = "") -> str:
    if isinstance(bone, dict):
        return str(bone.get("name", fallback) or fallback)
    return str(getattr(bone, "name", fallback) or fallback)


def _skeleton_bone_parent_id(bone) -> int:
    if isinstance(bone, dict):
        value = bone.get("parentId", -1)
    else:
        value = getattr(bone, "parentId", -1)
    try:
        return int(value)
    except Exception:
        return -1


def _rebuild_skeleton_compat_fields(skeleton_data):
    if skeleton_data is None:
        return None
    bones = list(getattr(skeleton_data, "bones", []) or [])
    skeleton_data.nbBones = len(bones)
    skeleton_data.names = []
    skeleton_data.parentIdx = []
    skeleton_data.positions = []
    skeleton_data.rotations = []
    skeleton_data.scales = []
    for idx, bone in enumerate(bones):
        name = _skeleton_bone_name(bone, str(idx))
        parent_id = _skeleton_bone_parent_id(bone)
        if isinstance(bone, dict):
            position = bone.get("co")
            rotation = bone.get("ro_quat")
            scale = bone.get("sc")
        else:
            position = getattr(bone, "co", None)
            rotation = getattr(bone, "ro_quat", None)
            scale = getattr(bone, "sc", None)
        skeleton_data.names.append(name)
        skeleton_data.parentIdx.append(parent_id)
        skeleton_data.positions.append(position if position is not None and position is not False else [0.0, 0.0, 0.0])
        skeleton_data.rotations.append(rotation if rotation is not None and rotation is not False else _identity_w3_quat())
        skeleton_data.scales.append(scale if scale is not None and scale is not False else [1.0, 1.0, 1.0])
    return skeleton_data


def _identity_w3_quat():
    from ..CR2W import w3_types
    return w3_types.Quaternion(0.0, 0.0, 0.0, 1.0)


def _copy_skeleton_bone(bone, *, new_id: int, parent_id: int):
    from ..CR2W import w3_types

    name = _skeleton_bone_name(bone, str(new_id))
    if isinstance(bone, dict):
        co = bone.get("co")
        ro = bone.get("ro")
        ro_quat = bone.get("ro_quat")
        sc = bone.get("sc")
    else:
        co = getattr(bone, "co", None)
        ro = getattr(bone, "ro", False)
        ro_quat = getattr(bone, "ro_quat", None)
        sc = getattr(bone, "sc", None)
    return w3_types.W3Bone(
        new_id,
        name,
        copy.deepcopy(co if co is not None and co is not False else [0.0, 0.0, 0.0]),
        parent_id,
        copy.deepcopy(ro),
        copy.deepcopy(ro_quat if ro_quat is not None and ro_quat is not False else _identity_w3_quat()),
        copy.deepcopy(sc if sc is not None and sc is not False else [1.0, 1.0, 1.0]),
    )


def clone_skeleton_data(skeleton_data):
    from ..CR2W import w3_types

    if skeleton_data is None:
        return w3_types.CSkeleton()
    bones = [
        _copy_skeleton_bone(bone, new_id=idx, parent_id=_skeleton_bone_parent_id(bone))
        for idx, bone in enumerate(getattr(skeleton_data, "bones", []) or [])
    ]
    tracks = copy.deepcopy(list(getattr(skeleton_data, "tracks", []) or []))
    return _rebuild_skeleton_compat_fields(w3_types.CSkeleton(bones=bones, tracks=tracks))


def _source_bone_indices_by_name(skeleton_data) -> dict[str, int]:
    out: dict[str, int] = {}
    for idx, bone in enumerate(getattr(skeleton_data, "bones", []) or []):
        name = _skeleton_bone_name(bone)
        if name and name not in out:
            out[name] = idx
    return out


def _rest_repair_names(name: str) -> tuple[str, ...]:
    name = str(name or "")
    aliases = {
        "dyng_l_rein_hand_left_00": "dyng_l_rein_hand_IK",
        "dyng_r_rein_hand_right_00": "dyng_r_rein_hand_IK",
    }
    out = [name] if name else []
    alias = aliases.get(name)
    if alias and alias not in out:
        out.append(alias)
    return tuple(out)


def _bone_local_matrix(bone):
    from mathutils import Matrix, Vector

    pos = Vector(_w3_vec3(getattr(bone, "co", bone.get("co") if isinstance(bone, dict) else None)))
    rot = _w3_quat(getattr(bone, "ro_quat", bone.get("ro_quat") if isinstance(bone, dict) else None))
    return Matrix.Translation(pos) @ rot.to_matrix().to_4x4()


def _set_bone_local_matrix(bone, mat):
    from ..CR2W import w3_types

    pos = mat.to_translation()
    quat = mat.to_quaternion()
    co = [float(pos.x), float(pos.y), float(pos.z), 1.0]
    ro_quat = w3_types.Quaternion(float(quat.x), float(quat.y), float(quat.z), float(quat.w))
    sc = [1.0, 1.0, 1.0]
    if isinstance(bone, dict):
        bone["co"] = co
        bone["ro_quat"] = ro_quat
        bone["sc"] = sc
    else:
        bone.co = co
        bone.ro_quat = ro_quat
        bone.sc = sc


def _matrix_has_useful_rest_transform(mat, *, tolerance=1e-6) -> bool:
    try:
        if mat.to_translation().length > tolerance:
            return True
        quat = mat.to_quaternion()
        return abs(float(quat.x)) > tolerance or abs(float(quat.y)) > tolerance or abs(float(quat.z)) > tolerance or abs(abs(float(quat.w)) - 1.0) > tolerance
    except Exception:
        return False


def _bone_has_default_rest_transform(bone, *, tolerance=1e-6) -> bool:
    pos = _w3_vec3(getattr(bone, "co", bone.get("co") if isinstance(bone, dict) else None))
    quat = _w3_quat(getattr(bone, "ro_quat", bone.get("ro_quat") if isinstance(bone, dict) else None))
    scale = _w3_vec3(getattr(bone, "sc", bone.get("sc") if isinstance(bone, dict) else None), default=(1.0, 1.0, 1.0))
    return (
        all(abs(float(value)) <= tolerance for value in pos)
        and abs(float(quat.x)) <= tolerance
        and abs(float(quat.y)) <= tolerance
        and abs(float(quat.z)) <= tolerance
        and abs(abs(float(quat.w)) - 1.0) <= tolerance
        and all(abs(float(value) - 1.0) <= tolerance for value in scale)
    )


def skeleton_has_placeholder_rest_bones(skeleton_data, *, tolerance=1e-6) -> bool:
    for bone in list(getattr(skeleton_data, "bones", []) or []):
        if _skeleton_bone_name(bone) in {"Root", "Trajectory"}:
            continue
        if _bone_has_default_rest_transform(bone, tolerance=tolerance):
            return True
    return False


def fill_placeholder_skeleton_transforms_from_sources(target_skeleton, source_skeletons, *, tolerance=1e-6) -> int:
    """Repair placeholder local transforms from source skeletons."""
    if target_skeleton is None or not skeleton_has_placeholder_rest_bones(target_skeleton, tolerance=tolerance):
        return 0

    target_bones = list(getattr(target_skeleton, "bones", []) or [])
    if not target_bones:
        return 0

    source_world_by_name = {}
    sources = sorted(
        [source for source in (source_skeletons or []) if source is not None],
        key=lambda source: len(getattr(source, "bones", []) or []),
        reverse=True,
    )
    for source in sources:
        source_world = _skeleton_world_matrices(source)
        for bone in getattr(source, "bones", []) or []:
            name = _skeleton_bone_name(bone)
            if not name:
                continue
            if _bone_has_default_rest_transform(bone, tolerance=tolerance):
                continue
            mat = source_world.get(name)
            if _matrix_has_useful_rest_transform(mat, tolerance=tolerance):
                for repair_name in _rest_repair_names(name):
                    source_world_by_name.setdefault(repair_name, mat.copy())

    if not source_world_by_name:
        return 0

    from mathutils import Matrix

    world_by_idx = {}
    updating = set()
    updated = 0

    def target_world_matrix(idx: int):
        nonlocal updated
        if idx in world_by_idx:
            return world_by_idx[idx]
        if idx in updating or idx < 0 or idx >= len(target_bones):
            return Matrix.Identity(4)
        updating.add(idx)
        bone = target_bones[idx]
        parent_idx = _skeleton_bone_parent_id(bone)
        parent_world = target_world_matrix(parent_idx) if 0 <= parent_idx < len(target_bones) else Matrix.Identity(4)
        name = _skeleton_bone_name(bone)
        source_world = source_world_by_name.get(name)
        if (
            source_world is not None
            and name not in {"Root", "Trajectory"}
            and _bone_has_default_rest_transform(bone, tolerance=tolerance)
        ):
            local_mat = parent_world.inverted() @ source_world
            _set_bone_local_matrix(bone, local_mat)
            world_by_idx[idx] = source_world
            updated += 1
        else:
            world_by_idx[idx] = parent_world @ _bone_local_matrix(bone)
        updating.discard(idx)
        return world_by_idx[idx]

    for idx in range(len(target_bones)):
        target_world_matrix(idx)

    if updated:
        target_skeleton.bones = target_bones
        _rebuild_skeleton_compat_fields(target_skeleton)
    return updated


def _fill_placeholder_armature_transforms_from_matrices(master_armature, source_matrices_by_name, *,
                                                        context=None, tolerance=1e-6) -> int:
    import bpy
    from mathutils import Vector
    from ..unreal_export.scene_utils import _restore_object_state, _snapshot_object_state

    if master_armature is None or safe_object_type(master_armature) != "ARMATURE":
        return 0
    if not object_still_exists(master_armature):
        return 0
    if not source_matrices_by_name:
        return 0

    existing_bones = _armature_bones_by_name(master_armature)
    wanted_names = [
        name for name, bone in existing_bones.items()
        if (
            name not in {"Root", "Trajectory"}
            and name in source_matrices_by_name
            and getattr(bone, "head_local", None) is not None
            and bone.head_local.length <= tolerance
        )
    ]
    if not wanted_names:
        return 0

    saved_state = _snapshot_object_state(context or bpy.context)
    updated = 0
    updated_names = []
    try:
        if saved_state[2] != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        bpy.ops.object.select_all(action="DESELECT")
        master_armature.select_set(True)
        (context or bpy.context).view_layer.objects.active = master_armature
        bpy.ops.object.mode_set(mode="EDIT")
        edit_bones = master_armature.data.edit_bones
        for name in wanted_names:
            edit_bone = edit_bones.get(name)
            mat = source_matrices_by_name.get(name)
            if edit_bone is None or mat is None:
                continue
            current_length = (edit_bone.tail - edit_bone.head).length
            if current_length <= 0.000001:
                current_length = 0.01
            head = mat.to_translation()
            direction = mat.to_3x3() @ Vector((0.0, 0.01, 0.0))
            if direction.length <= 0.000001:
                direction = Vector((0.0, 0.01, 0.0))
            edit_bone.head = head
            edit_bone.tail = head + direction.normalized() * current_length
            try:
                edit_bone.matrix = mat
            except Exception:
                pass
            updated_names.append(name)
            updated += 1
        for name in updated_names:
            edit_bone = edit_bones.get(name)
            if edit_bone is None or not edit_bone.children:
                continue
            current_direction = edit_bone.tail - edit_bone.head
            if current_direction.length <= 0.000001:
                continue
            current_direction = current_direction.normalized()
            best_child = None
            best_dot = 0.0
            for child in edit_bone.children:
                direction_to_child = child.head - edit_bone.head
                if direction_to_child.length <= 0.000001:
                    continue
                dot = current_direction.dot(direction_to_child.normalized())
                if dot > best_dot:
                    best_dot = dot
                    best_child = child
            if best_child is not None and best_dot > 0.995:
                edit_bone.tail = best_child.head.copy()
        bpy.ops.object.mode_set(mode="OBJECT")
    finally:
        try:
            if bpy.context.object and bpy.context.object.mode != "OBJECT":
                bpy.ops.object.mode_set(mode="OBJECT")
        except Exception:
            pass
        _restore_object_state(context or bpy.context, saved_state)
    return updated


def fill_placeholder_armature_transforms_from_skeletons(master_armature, source_skeletons, *,
                                                        context=None, tolerance=1e-6) -> int:
    sources = sorted(
        [source for source in (source_skeletons or []) if source is not None],
        key=lambda source: len(getattr(source, "bones", []) or []),
        reverse=True,
    )
    if not sources:
        return 0

    source_matrices_by_name = {}
    use_rot90 = _armature_uses_rot90(master_armature)
    for source in sources:
        source_world = _skeleton_world_matrices(source, rotate_90=use_rot90)
        for bone in getattr(source, "bones", []) or []:
            name = _skeleton_bone_name(bone)
            if not name or _bone_has_default_rest_transform(bone, tolerance=tolerance):
                continue
            mat = source_world.get(name)
            if not _matrix_has_useful_rest_transform(mat, tolerance=tolerance):
                continue
            for repair_name in _rest_repair_names(name):
                source_matrices_by_name.setdefault(repair_name, mat.copy())
    return _fill_placeholder_armature_transforms_from_matrices(
        master_armature,
        source_matrices_by_name,
        context=context,
        tolerance=tolerance,
    )


def fill_placeholder_armature_transforms_from_armatures(master_armature, source_armatures, *,
                                                       context=None, tolerance=1e-6) -> int:
    if master_armature is None or safe_object_type(master_armature) != "ARMATURE":
        return 0
    if not object_still_exists(master_armature):
        return 0

    def source_score(source):
        path = str(source.get("witcher_path", "") or "").lower() if hasattr(source, "get") else ""
        is_mesh = 1 if path.endswith(".w2mesh") else 0
        return (is_mesh, len(getattr(getattr(source, "data", None), "bones", []) or []))

    sources = sorted(
        [
            source for source in (source_armatures or [])
            if source is not master_armature
            and safe_object_type(source) == "ARMATURE"
            and object_still_exists(source)
        ],
        key=source_score,
        reverse=True,
    )
    if not sources:
        return 0

    source_matrices_by_name = {}
    target_inv = master_armature.matrix_world.inverted()
    for source in sources:
        source_world = source.matrix_world
        for bone in getattr(getattr(source, "data", None), "bones", []) or []:
            name = str(getattr(bone, "name", "") or "")
            if not name:
                continue
            mat = target_inv @ source_world @ bone.matrix_local
            if mat.to_translation().length <= tolerance:
                continue
            for repair_name in _rest_repair_names(name):
                source_matrices_by_name.setdefault(repair_name, mat.copy())
    return _fill_placeholder_armature_transforms_from_matrices(
        master_armature,
        source_matrices_by_name,
        context=context,
        tolerance=tolerance,
    )


def merge_skeleton_data(target_skeleton, source_skeleton, *,
                        exclude_bone_names=None,
                        exclude_name_contains=None) -> int:
    """Append missing bones from ``source_skeleton`` into ``target_skeleton``.

    This operates on parsed CR2W skeleton data before Blender objects are
    created. Duplicate bone names are treated as shared bones and skipped.
    """
    if target_skeleton is None or source_skeleton is None:
        return 0
    target_bones = list(getattr(target_skeleton, "bones", []) or [])
    source_bones = list(getattr(source_skeleton, "bones", []) or [])
    if not source_bones:
        return 0

    excluded = {str(name or "") for name in (exclude_bone_names or []) if str(name or "")}
    excluded_contains = tuple(str(text or "").lower() for text in (exclude_name_contains or []) if str(text or ""))
    target_name_to_idx = {
        _skeleton_bone_name(bone): idx
        for idx, bone in enumerate(target_bones)
        if _skeleton_bone_name(bone)
    }
    source_name_to_idx = _source_bone_indices_by_name(source_skeleton)
    added = 0
    visiting: set[int] = set()

    def is_excluded(name: str) -> bool:
        lowered = name.lower()
        return name in excluded or any(part in lowered for part in excluded_contains)

    def add_source_index(source_idx: int) -> int:
        nonlocal added
        if source_idx < 0 or source_idx >= len(source_bones):
            return -1
        source_bone = source_bones[source_idx]
        name = _skeleton_bone_name(source_bone)
        if not name or is_excluded(name):
            return -1
        existing_idx = target_name_to_idx.get(name)
        if existing_idx is not None:
            return existing_idx
        if source_idx in visiting:
            return -1
        visiting.add(source_idx)
        parent_target_idx = -1
        parent_source_idx = _skeleton_bone_parent_id(source_bone)
        if 0 <= parent_source_idx < len(source_bones):
            parent_name = _skeleton_bone_name(source_bones[parent_source_idx])
            parent_target_idx = target_name_to_idx.get(parent_name, -1)
            if parent_target_idx == -1 and parent_name in source_name_to_idx:
                parent_target_idx = add_source_index(source_name_to_idx[parent_name])
        new_idx = len(target_bones)
        copied = _copy_skeleton_bone(source_bone, new_id=new_idx, parent_id=parent_target_idx)
        target_bones.append(copied)
        target_name_to_idx[name] = new_idx
        added += 1
        visiting.discard(source_idx)
        return new_idx

    for idx in range(len(source_bones)):
        add_source_index(idx)

    if added:
        target_skeleton.bones = target_bones
        _rebuild_skeleton_compat_fields(target_skeleton)
        target_tracks = list(getattr(target_skeleton, "tracks", []) or [])
        seen_tracks = {str(track) for track in target_tracks}
        for track in getattr(source_skeleton, "tracks", []) or []:
            if str(track) not in seen_tracks:
                target_tracks.append(copy.deepcopy(track))
                seen_tracks.add(str(track))
        target_skeleton.tracks = target_tracks
    return added


def load_skeleton_data(file_path: str):
    file_path = str(file_path or "").strip()
    if not file_path:
        return None
    lowered = file_path.lower()
    if lowered.endswith((".w2rig", ".w3dyng")):
        from ..CR2W.dc_skeleton import load_bin_skeleton
        return load_bin_skeleton(file_path)
    if lowered.endswith((".w2rig.json", ".w3dyng.json")):
        from ..CR2W import read_json_w3
        return read_json_w3.readCSkeleton(file_path)
    return None


def _w3_vec3(value, default=(0.0, 0.0, 0.0)):
    if value is None or value is False:
        return tuple(default)
    if hasattr(value, "x") and hasattr(value, "y") and hasattr(value, "z"):
        return (float(value.x), float(value.y), float(value.z))
    try:
        return (float(value[0]), float(value[1]), float(value[2]))
    except Exception:
        return tuple(default)


def _w3_quat(value):
    from mathutils import Quaternion

    if value is None or value is False:
        return Quaternion((1.0, 0.0, 0.0, 0.0))
    if all(hasattr(value, attr) for attr in ("W", "X", "Y", "Z")):
        return Quaternion((float(value.W), float(value.X), float(value.Y), float(value.Z)))
    if all(hasattr(value, attr) for attr in ("w", "x", "y", "z")):
        return Quaternion((float(value.w), float(value.x), float(value.y), float(value.z)))
    try:
        if len(value) >= 4:
            return Quaternion((float(value[3]), float(value[0]), float(value[1]), float(value[2])))
    except Exception:
        pass
    return Quaternion((1.0, 0.0, 0.0, 0.0))


def _skeleton_world_matrices(skeleton_data, *, rotate_90: bool = False) -> dict[str, Any]:
    from mathutils import Matrix, Vector

    bones = list(getattr(skeleton_data, "bones", []) or [])
    matrices_by_idx: dict[int, Any] = {}
    names_by_idx = [_skeleton_bone_name(bone, str(idx)) for idx, bone in enumerate(bones)]
    display_rotation = Matrix.Rotation(math.radians(-90), 4, 'Z') if rotate_90 else None

    def matrix_for_idx(idx: int):
        if idx in matrices_by_idx:
            return matrices_by_idx[idx]
        bone = bones[idx]
        pos = Vector(_w3_vec3(getattr(bone, "co", bone.get("co") if isinstance(bone, dict) else None)))
        rot = _w3_quat(getattr(bone, "ro_quat", bone.get("ro_quat") if isinstance(bone, dict) else None))
        mat = Matrix.Translation(pos) @ rot.to_matrix().to_4x4()
        parent_idx = _skeleton_bone_parent_id(bone)
        if 0 <= parent_idx < len(bones):
            mat = matrix_for_idx(parent_idx) @ mat
        matrices_by_idx[idx] = mat
        return mat

    out = {}
    for idx, name in enumerate(names_by_idx):
        if name and name not in out:
            mat = matrix_for_idx(idx)
            out[name] = mat @ display_rotation if display_rotation is not None else mat
    return out


def _armature_rest_world_matrices(armature_obj) -> dict[str, Any]:
    if armature_obj is None or safe_object_type(armature_obj) != "ARMATURE":
        return {}
    world = getattr(armature_obj, "matrix_world", None)
    if world is None:
        return {}
    out = {}
    for bone in getattr(getattr(armature_obj, "data", None), "bones", []) or []:
        name = str(getattr(bone, "name", "") or "")
        if not name:
            continue
        out[name] = world @ bone.matrix_local
    return out


def _nearest_source_anchor_index(source_bones, source_idx: int, target_world: dict[str, Any]) -> int:
    idx = source_idx
    while 0 <= idx < len(source_bones):
        name = _skeleton_bone_name(source_bones[idx])
        if name in target_world:
            return idx
        idx = _skeleton_bone_parent_id(source_bones[idx])
    return -1


def _aligned_skeleton_world_matrices(source_skeleton, target_armature, *, source_rot90: bool = False) -> dict[str, Any]:
    source_bones = list(getattr(source_skeleton, "bones", []) or [])
    source_world = _skeleton_world_matrices(source_skeleton, rotate_90=source_rot90)
    target_world = _armature_rest_world_matrices(target_armature)
    if not source_bones or not target_world:
        return source_world

    out = {}
    for idx, bone in enumerate(source_bones):
        name = _skeleton_bone_name(bone)
        if not name:
            continue
        if name in target_world:
            out[name] = target_world[name]
            continue
        source_mat = source_world.get(name)
        if source_mat is None:
            continue
        anchor_idx = _nearest_source_anchor_index(source_bones, idx, target_world)
        if 0 <= anchor_idx < len(source_bones):
            anchor_name = _skeleton_bone_name(source_bones[anchor_idx])
            source_anchor = source_world.get(anchor_name)
            target_anchor = target_world.get(anchor_name)
            if source_anchor is not None and target_anchor is not None:
                out[name] = target_anchor @ source_anchor.inverted() @ source_mat
                continue
        out[name] = source_mat
    return out


def _required_skeleton_bone_names_for_armature(source_skeleton, target_armature,
                                               exclude_bone_names=None,
                                               exclude_name_contains=None) -> list[str]:
    target_names = set(_armature_bones_by_name(target_armature))
    bones = list(getattr(source_skeleton, "bones", []) or [])
    excluded = {str(name or "") for name in (exclude_bone_names or []) if str(name or "")}
    excluded_contains = tuple(str(text or "").lower() for text in (exclude_name_contains or []) if str(text or ""))
    required: set[str] = set()

    def is_excluded(name: str) -> bool:
        lowered = name.lower()
        return name in excluded or any(part in lowered for part in excluded_contains)

    def add_chain(idx: int):
        while 0 <= idx < len(bones):
            bone = bones[idx]
            name = _skeleton_bone_name(bone)
            if not name or name in target_names or name in required or is_excluded(name):
                break
            required.add(name)
            idx = _skeleton_bone_parent_id(bone)

    for idx, bone in enumerate(bones):
        name = _skeleton_bone_name(bone)
        if name and name not in target_names and not is_excluded(name):
            add_chain(idx)

    ordered = []
    visited: set[str] = set()
    source_name_to_idx = _source_bone_indices_by_name(source_skeleton)

    def visit(name: str):
        if name in visited:
            return
        visited.add(name)
        idx = source_name_to_idx.get(name, -1)
        if 0 <= idx < len(bones):
            parent_idx = _skeleton_bone_parent_id(bones[idx])
            if 0 <= parent_idx < len(bones):
                parent_name = _skeleton_bone_name(bones[parent_idx])
                if parent_name in required:
                    visit(parent_name)
        if name in required:
            ordered.append(name)

    for bone in bones:
        name = _skeleton_bone_name(bone)
        if name in required:
            visit(name)
    return ordered


def _armature_uses_rot90(armature_obj) -> bool:
    try:
        from .. import get_rig_rot90_enabled
        return bool(get_rig_rot90_enabled(getattr(armature_obj.data, "witcherui_RigSettings", None), False))
    except Exception:
        return False


def append_skeleton_data_to_armature(master_armature, source_skeleton, *,
                                     context=None,
                                     exclude_bone_names=None,
                                     exclude_name_contains=None) -> int:
    """Add missing bones from parsed skeleton data to an existing Blender armature."""
    import bpy
    from mathutils import Vector
    from ..unreal_export.scene_utils import _restore_object_state, _snapshot_object_state

    if master_armature is None or safe_object_type(master_armature) != "ARMATURE" or source_skeleton is None:
        return 0
    if not object_still_exists(master_armature):
        return 0

    required_names = _required_skeleton_bone_names_for_armature(
        source_skeleton,
        master_armature,
        exclude_bone_names=exclude_bone_names,
        exclude_name_contains=exclude_name_contains,
    )
    if not required_names:
        return 0

    use_rot90 = _armature_uses_rot90(master_armature)
    source_world = _aligned_skeleton_world_matrices(source_skeleton, master_armature, source_rot90=use_rot90)
    source_name_to_idx = _source_bone_indices_by_name(source_skeleton)
    source_bones = list(getattr(source_skeleton, "bones", []) or [])
    source_child_names: dict[str, list[str]] = {}
    for child_bone in source_bones:
        child_name = _skeleton_bone_name(child_bone)
        parent_idx = _skeleton_bone_parent_id(child_bone)
        if not child_name or not (0 <= parent_idx < len(source_bones)):
            continue
        parent_name = _skeleton_bone_name(source_bones[parent_idx])
        if parent_name:
            source_child_names.setdefault(parent_name, []).append(child_name)
    root_name = _main_root_bone_name(master_armature)

    saved_state = _snapshot_object_state(context or bpy.context)
    added = 0
    added_names: list[str] = []
    try:
        if saved_state[2] != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        bpy.ops.object.select_all(action="DESELECT")
        master_armature.select_set(True)
        (context or bpy.context).view_layer.objects.active = master_armature
        bpy.ops.object.mode_set(mode="EDIT")
        edit_bones = master_armature.data.edit_bones

        for name in required_names:
            if name in edit_bones:
                continue
            mat = source_world.get(name)
            if mat is None:
                continue
            edit_bone = edit_bones.new(name)
            edit_bone.use_connect = False
            parent_name = ""
            source_idx = source_name_to_idx.get(name, -1)
            if 0 <= source_idx < len(source_bones):
                parent_idx = _skeleton_bone_parent_id(source_bones[source_idx])
                if 0 <= parent_idx < len(source_bones):
                    parent_name = _skeleton_bone_name(source_bones[parent_idx])
            if parent_name and parent_name in edit_bones:
                edit_bone.parent = edit_bones[parent_name]
            elif root_name and root_name in edit_bones and root_name != name:
                edit_bone.parent = edit_bones[root_name]
            head = mat.to_translation()
            direction = mat.to_3x3() @ Vector((0.0, 0.01, 0.0))
            if direction.length < 0.000001:
                direction = Vector((0.0, 0.01, 0.0))
            edit_bone.head = head
            edit_bone.tail = head + direction.normalized() * 0.01
            try:
                edit_bone.matrix = mat
            except Exception:
                pass
            added_names.append(name)
            added += 1

        for name in added_names:
            edit_bone = edit_bones.get(name)
            if edit_bone is None:
                continue
            current_direction = edit_bone.tail - edit_bone.head
            for child_name in source_child_names.get(name, []):
                child_bone = edit_bones.get(child_name)
                if child_bone is None:
                    continue
                direction_to_child = child_bone.head - edit_bone.head
                if direction_to_child.length <= 0.000001:
                    continue
                if (
                    current_direction.length <= 0.000001
                    or current_direction.normalized().dot(direction_to_child.normalized()) > 0.999
                ):
                    edit_bone.tail = child_bone.head.copy()
                    break
        bpy.ops.object.mode_set(mode="OBJECT")
    finally:
        try:
            if bpy.context.object and bpy.context.object.mode != "OBJECT":
                bpy.ops.object.mode_set(mode="OBJECT")
        except Exception:
            pass
        _restore_object_state(context or bpy.context, saved_state)
    return added


def _armature_bones_by_name(armature) -> dict[str, Any]:
    return {
        str(getattr(bone, "name", "") or ""): bone
        for bone in getattr(getattr(armature, "data", None), "bones", []) or []
        if str(getattr(bone, "name", "") or "")
    }


def _missing_armature_bone_names(group_armature, main_armature) -> list[str]:
    main_bones = set(_armature_bones_by_name(main_armature))
    return [name for name in _armature_bones_by_name(group_armature) if name not in main_bones]


def _armature_root_count(armature) -> int:
    return sum(
        1
        for bone in getattr(getattr(armature, "data", None), "bones", []) or []
        if getattr(bone, "parent", None) is None
    )


def _find_attachment_armature_for_missing_bones(group_armature, missing_names: list[str]):
    missing = set(missing_names)
    if not missing:
        return None

    candidates = []
    seen = set()

    def add(candidate):
        if getattr(candidate, "type", "") != "ARMATURE":
            return
        ident = id(candidate)
        if ident in seen:
            return
        seen.add(ident)
        candidates.append(candidate)

    add(getattr(group_armature, "parent", None))

    pose_bones = getattr(getattr(group_armature, "pose", None), "bones", None)
    if pose_bones is not None:
        for bone_name in missing_names:
            try:
                pose_bone = pose_bones.get(bone_name)
            except Exception:
                pose_bone = None
            for constraint in getattr(pose_bone, "constraints", []) or []:
                add(getattr(constraint, "target", None))

    add(group_armature)

    def score(candidate):
        bones = _armature_bones_by_name(candidate)
        contains = len(missing.intersection(bones))
        parented = sum(1 for bone in bones.values() if getattr(bone, "parent", None) is not None)
        single_root = 1 if _armature_root_count(candidate) == 1 else 0
        not_group = 1 if candidate is not group_armature else 0
        covers_all = 1 if contains == len(missing) else 0
        return (covers_all, contains, single_root, parented, not_group)

    best = max(candidates, key=score, default=None)
    if best is None or not missing.intersection(_armature_bones_by_name(best)):
        return None
    return best


def _required_source_bone_names(source_armature, wanted_names: list[str], stop_names: set[str]) -> list[str]:
    source_bones = _armature_bones_by_name(source_armature)
    required: set[str] = set()

    def add_chain(name: str):
        bone = source_bones.get(name)
        while bone is not None:
            bone_name = str(getattr(bone, "name", "") or "")
            if not bone_name or bone_name in stop_names:
                break
            if bone_name in required:
                break
            required.add(bone_name)
            bone = getattr(bone, "parent", None)

    for name in wanted_names:
        add_chain(name)

    ordered = []
    visited: set[str] = set()

    def visit(name: str):
        if name in visited:
            return
        visited.add(name)
        bone = source_bones.get(name)
        parent = getattr(bone, "parent", None) if bone is not None else None
        parent_name = str(getattr(parent, "name", "") or "")
        if parent_name in required:
            visit(parent_name)
        if name in required:
            ordered.append(name)

    for bone in getattr(getattr(source_armature, "data", None), "bones", []) or []:
        name = str(getattr(bone, "name", "") or "")
        if name in required:
            visit(name)
    for name in sorted(required):
        visit(name)
    return ordered


def _main_root_bone_name(armature) -> str:
    bones = list(getattr(getattr(armature, "data", None), "bones", []) or [])
    for bone in bones:
        if getattr(bone, "name", "") == "Root":
            return "Root"
    for bone in bones:
        if getattr(bone, "parent", None) is None:
            return str(getattr(bone, "name", "") or "")
    return ""


def _copy_source_bone_to_edit_bones(edit_bones, bone_name: str, source_bones: dict,
                                    source_armature, target_armature, root_name: str,
                                    *, required_names=None, attachment_parent_bone: str = ""):
    from mathutils import Vector

    if bone_name in edit_bones:
        return None
    source_bone = source_bones.get(bone_name)
    if source_bone is None:
        return None

    edit_bone = edit_bones.new(bone_name)
    edit_bone.use_connect = False
    required = set(required_names or ())
    attachment_parent_bone = str(attachment_parent_bone or "").strip()
    try:
        edit_bone.use_deform = bool(getattr(source_bone, "use_deform", True))
    except Exception:
        pass

    parent = getattr(source_bone, "parent", None)
    parent_name = str(getattr(parent, "name", "") or "")
    reanchor_skipped_source_root = bool(
        attachment_parent_bone
        and parent_name
        and parent_name in edit_bones
        and parent_name not in required
        and attachment_parent_bone in edit_bones
    )
    if parent_name and parent_name in edit_bones and not reanchor_skipped_source_root:
        edit_bone.parent = edit_bones[parent_name]
        source_parent_inv = parent.matrix_local.inverted()
        target_parent_matrix = edit_bones[parent_name].matrix.copy()
        target_matrix = target_parent_matrix @ (source_parent_inv @ source_bone.matrix_local)
        local_head = source_parent_inv @ source_bone.head_local
        local_tail = source_parent_inv @ source_bone.tail_local
        head = target_parent_matrix @ local_head
        tail = target_parent_matrix @ local_tail
    else:
        target_parent_name = attachment_parent_bone if reanchor_skipped_source_root else root_name
        if target_parent_name and target_parent_name in edit_bones:
            edit_bone.parent = edit_bones[target_parent_name]
        target_inv = target_armature.matrix_world.inverted()
        source_world = source_armature.matrix_world
        target_matrix = target_inv @ source_world @ source_bone.matrix_local
        head = target_inv @ (source_world @ source_bone.head_local)
        tail = target_inv @ (source_world @ source_bone.tail_local)

    if (tail - head).length < 0.000001:
        tail = head + Vector((0.0, 0.01, 0.0))
    edit_bone.head = head
    edit_bone.tail = tail
    try:
        edit_bone.matrix = target_matrix
    except Exception:
        pass
    return edit_bone


def _parse_refcounts(value) -> dict[str, int]:
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    out = {}
    for key, count in parsed.items():
        key = str(key or "").strip()
        if not key:
            continue
        try:
            count_int = int(count)
        except Exception:
            count_int = 0
        if count_int > 0:
            out[key] = count_int
    return out


def _encode_refcounts(refcounts: dict[str, int]) -> str:
    clean = {
        str(key): int(count)
        for key, count in sorted((refcounts or {}).items())
        if str(key or "").strip() and int(count) > 0
    }
    return json.dumps(clean, sort_keys=True, separators=(",", ":"))


def _owners_from_refcounts(refcounts: dict[str, int]) -> list[str]:
    return [key for key, count in sorted((refcounts or {}).items()) if int(count) > 0]


def _add_source_owner_to_bone(bone, src_id: str) -> bool:
    src_id = str(src_id or "").strip()
    if bone is None or not src_id:
        return False
    try:
        refcounts = _parse_refcounts(bone.get(SRC_REFCOUNTS_PROP, ""))
    except Exception:
        refcounts = {}
    refcounts[src_id] = int(refcounts.get(src_id, 0)) + 1
    owners = _owners_from_refcounts(refcounts)
    try:
        bone[SRC_REFCOUNTS_PROP] = _encode_refcounts(refcounts)
        bone[SRC_GUIDS_PROP] = json.dumps(owners, separators=(",", ":"))
        bone[SRC_GUID_PROP] = owners[0] if owners else ""
    except Exception:
        return False
    return True


def _remove_source_owner_from_bone(bone, src_id: str) -> bool:
    src_id = str(src_id or "").strip()
    if bone is None or not src_id:
        return False
    try:
        refcounts = _parse_refcounts(bone.get(SRC_REFCOUNTS_PROP, ""))
    except Exception:
        refcounts = {}
    if src_id not in refcounts:
        return False
    refcounts[src_id] -= 1
    if refcounts[src_id] <= 0:
        del refcounts[src_id]
    owners = _owners_from_refcounts(refcounts)
    try:
        if owners:
            bone[SRC_REFCOUNTS_PROP] = _encode_refcounts(refcounts)
            bone[SRC_GUIDS_PROP] = json.dumps(owners, separators=(",", ":"))
            bone[SRC_GUID_PROP] = owners[0]
        else:
            for prop_name in (SRC_REFCOUNTS_PROP, SRC_GUIDS_PROP, SRC_GUID_PROP):
                if prop_name in bone:
                    del bone[prop_name]
    except Exception:
        pass
    return not owners


def should_unify_character_armature(context=None) -> bool:
    try:
        if context is None:
            import bpy
            context = bpy.context
        from .. import get_unify_character_armature
        return bool(get_unify_character_armature(context))
    except Exception:
        return False


def graft_source_id(master_armature=None, child_armature=None, fallback: str = "") -> str:
    for obj in (master_armature, child_armature):
        if obj is None:
            continue
        for prop_name in (ACTIVE_GRAFT_SRC_PROP, "witcher_equip_guid", "witcher_template_guid"):
            try:
                value = str(obj.get(prop_name, "") or "").strip()
            except Exception:
                value = ""
            if value:
                return value
    return str(fallback or safe_object_name(child_armature) or "").strip()


def is_own_skeleton_attachment(armature) -> bool:
    if armature is None or getattr(armature, "type", "") != "ARMATURE":
        return False
    try:
        is_special = bool(armature.get("w2_special_attachment", False))
        parent_bone = str(armature.get("w2_special_parent_bone", "") or "").strip()
    except Exception:
        return False
    return bool(is_special and parent_bone)


def armature_is_in_merged_character_hierarchy(armature) -> bool:
    if armature is None:
        return False
    try:
        import bpy
    except Exception:
        bpy = None

    pending = [armature]
    seen = set()
    while pending:
        obj = pending.pop()
        if obj is None or id(obj) in seen:
            continue
        seen.add(id(obj))
        try:
            if obj.get("witcher_merged_character_armature", False):
                return True
        except Exception:
            pass
        try:
            parent = getattr(obj, "parent", None)
            if parent is not None:
                pending.append(parent)
        except Exception:
            pass
        try:
            special_parent_name = str(obj.get("w2_special_parent_arm", "") or "")
        except Exception:
            special_parent_name = ""
        if special_parent_name and bpy is not None:
            try:
                special_parent = bpy.data.objects.get(special_parent_name)
            except Exception:
                special_parent = None
            if special_parent is not None:
                pending.append(special_parent)
        try:
            constraints = list(getattr(obj, "constraints", []) or [])
        except Exception:
            constraints = []
        for constraint in constraints:
            target = getattr(constraint, "target", None)
            if target is not None:
                pending.append(target)
    return False


def _is_dangle_constraint_armature(obj) -> bool:
    if obj is None or safe_object_type(obj) != "ARMATURE":
        return False
    try:
        return str(obj.get("witcher_type", "") or "").startswith("CAnimDangleConstraint_")
    except Exception:
        return False


def _is_dangle_buffer_armature(obj) -> bool:
    if obj is None or safe_object_type(obj) != "ARMATURE":
        return False
    try:
        return str(obj.get("witcher_type", "") or "") == "CAnimDangleBufferComponent"
    except Exception:
        return False


def _iter_bpy_objects():
    import bpy
    try:
        return list(bpy.data.objects)
    except Exception:
        return []


def safe_object_name(obj, fallback: str = "") -> str:
    try:
        return str(getattr(obj, "name", "") or fallback)
    except ReferenceError:
        return str(fallback or "")
    except Exception:
        return str(fallback or "")


def safe_object_type(obj, fallback: str = "") -> str:
    try:
        return str(getattr(obj, "type", "") or fallback)
    except ReferenceError:
        return str(fallback or "")
    except Exception:
        return str(fallback or "")


def object_still_exists(obj) -> bool:
    if obj is None:
        return False
    try:
        name = getattr(obj, "name", "")
        getattr(obj, "type", "")
    except ReferenceError:
        return False
    except Exception:
        return False
    try:
        import bpy
        objects = getattr(getattr(bpy, "data", None), "objects", None)
        if objects is None or not name:
            return True
        try:
            return objects.get(name) is obj
        except Exception:
            return name in objects
    except Exception:
        return True


def _set_parent_keep_world(obj, parent_obj, *, parent_type="OBJECT", parent_bone=""):
    if obj is None or obj == parent_obj:
        return
    try:
        world = obj.matrix_world.copy()
    except Exception:
        world = None
    obj.parent = parent_obj
    obj.parent_type = parent_type
    obj.parent_bone = parent_bone
    if world is not None:
        try:
            obj.matrix_world = world
        except Exception:
            pass


def _graft_reparent_target(master_armature, child_armature, parent_bone: str = ""):
    if parent_bone and getattr(getattr(master_armature, "data", None), "bones", {}).get(parent_bone):
        return master_armature, "BONE", parent_bone

    visual_parent = getattr(child_armature, "parent", None)
    visual_parent_type = str(getattr(child_armature, "parent_type", "OBJECT") or "OBJECT")
    if (
        visual_parent is not None
        and visual_parent is not master_armature
        and visual_parent is not child_armature
        and visual_parent_type == "OBJECT"
        and bool(safe_object_name(visual_parent))
    ):
        return visual_parent, "OBJECT", ""

    return master_armature, "OBJECT", ""


def _iter_constraints_for_object(obj):
    for constraint in getattr(obj, "constraints", []) or []:
        yield constraint
    pose = getattr(obj, "pose", None)
    for pose_bone in getattr(pose, "bones", []) or []:
        for constraint in getattr(pose_bone, "constraints", []) or []:
            yield constraint


def _retarget_constraints_to_armature(source_armature, target_armature, *, objects=None) -> int:
    if source_armature is None or target_armature is None or source_armature is target_armature:
        return 0
    target_bones = _armature_bones_by_name(target_armature)
    object_iter = list(objects) if objects is not None else _iter_bpy_objects()
    changed = 0
    for obj in object_iter:
        if obj is None:
            continue
        for constraint in _iter_constraints_for_object(obj):
            try:
                if getattr(constraint, "target", None) is not source_armature:
                    continue
                subtarget = str(getattr(constraint, "subtarget", "") or "").strip()
                if subtarget and subtarget not in target_bones:
                    continue
                constraint.target = target_armature
                changed += 1
            except Exception:
                continue
    return changed


def _pose_bones(obj):
    pose = getattr(obj, "pose", None)
    return getattr(pose, "bones", []) or []


def _pose_bone_named(obj, name: str):
    bones = _pose_bones(obj)
    getter = getattr(bones, "get", None)
    if callable(getter):
        try:
            return getter(name)
        except Exception:
            pass
    for bone in bones:
        if str(getattr(bone, "name", "") or "") == name:
            return bone
    return None


def _is_dangle_driver_constraint(constraint) -> bool:
    return (
        getattr(constraint, "type", "") == "COPY_TRANSFORMS"
        and _is_dangle_constraint_armature(getattr(constraint, "target", None))
    )


def _has_matching_pose_constraint(pose_bone, source_constraint) -> bool:
    for constraint in getattr(pose_bone, "constraints", []) or []:
        if (
            getattr(constraint, "type", "") == getattr(source_constraint, "type", "")
            and getattr(constraint, "target", None) is getattr(source_constraint, "target", None)
            and str(getattr(constraint, "subtarget", "") or "") == str(getattr(source_constraint, "subtarget", "") or "")
        ):
            return True
    return False


def _copy_pose_constraint(pose_bone, source_constraint):
    constraints = getattr(pose_bone, "constraints", None)
    new_constraint = getattr(constraints, "new", None)
    if not callable(new_constraint):
        return None
    constraint_type = getattr(source_constraint, "type", "COPY_TRANSFORMS")
    try:
        copied = new_constraint(type=constraint_type)
    except TypeError:
        copied = new_constraint(constraint_type)
    for attr in (
        "name",
        "target",
        "subtarget",
        "influence",
        "mute",
        "owner_space",
        "target_space",
        "mix_mode",
        "head_tail",
        "use_offset",
    ):
        if not hasattr(source_constraint, attr):
            continue
        try:
            setattr(copied, attr, getattr(source_constraint, attr))
        except Exception:
            pass
    return copied


def _copy_dangle_driver_constraints_to_master(source_armature, target_armature) -> int:
    if source_armature is None or target_armature is None or source_armature is target_armature:
        return 0
    changed = 0
    for source_bone in _pose_bones(source_armature):
        bone_name = str(getattr(source_bone, "name", "") or "")
        if not bone_name:
            continue
        target_bone = _pose_bone_named(target_armature, bone_name)
        if target_bone is None:
            continue
        for constraint in getattr(source_bone, "constraints", []) or []:
            if not _is_dangle_driver_constraint(constraint):
                continue
            if _has_matching_pose_constraint(target_bone, constraint):
                continue
            if _copy_pose_constraint(target_bone, constraint) is not None:
                changed += 1
    return changed


_DANGLE_BREAST_BONES = {"l_boob", "r_boob"}


def _is_dangle_dynamic_bone_name(bone_name: str) -> bool:
    return str(bone_name or "").startswith("dyng_") or str(bone_name or "") in _DANGLE_BREAST_BONES


def _dangle_driver_can_drive_bone(driver_armature, bone_name: str) -> bool:
    driver_type = ""
    try:
        driver_type = str(driver_armature.get("witcher_type", "") or "")
    except Exception:
        driver_type = ""
    if driver_type == "CAnimDangleConstraint_Dyng":
        return str(bone_name or "").startswith("dyng_")
    if driver_type == "CAnimDangleConstraint_Breast":
        return str(bone_name or "") in _DANGLE_BREAST_BONES
    return False


def copy_dangle_driver_constraints_to_armature(target_armature, driver_armature) -> int:
    if target_armature is None or not _is_dangle_constraint_armature(driver_armature):
        return 0
    if safe_object_type(target_armature) != "ARMATURE":
        return 0
    changed = 0
    for driver_bone in _pose_bones(driver_armature):
        bone_name = str(getattr(driver_bone, "name", "") or "")
        if not bone_name or not _dangle_driver_can_drive_bone(driver_armature, bone_name):
            continue
        target_bone = _pose_bone_named(target_armature, bone_name)
        if target_bone is None:
            continue
        spec = type("_ConstraintSpec", (), {})()
        spec.type = "COPY_TRANSFORMS"
        spec.name = f"W3_DANGLE_{bone_name}"
        spec.target = driver_armature
        spec.subtarget = bone_name
        spec.influence = 1.0
        spec.mute = False
        if _has_matching_pose_constraint(target_bone, spec):
            continue
        if _copy_pose_constraint(target_bone, spec) is not None:
            changed += 1
    return changed


def copy_dangle_anchor_constraints_to_armature(target_armature, source_armature) -> int:
    if target_armature is None or source_armature is None or target_armature is source_armature:
        return 0
    if safe_object_type(target_armature) != "ARMATURE" or safe_object_type(source_armature) != "ARMATURE":
        return 0
    changed = 0
    for target_bone in _pose_bones(target_armature):
        bone_name = str(getattr(target_bone, "name", "") or "")
        if not bone_name or _is_dangle_dynamic_bone_name(bone_name):
            continue
        if _pose_bone_named(source_armature, bone_name) is None:
            continue
        spec = type("_ConstraintSpec", (), {})()
        spec.type = "COPY_TRANSFORMS"
        spec.name = f"W3_DANGLE_ANCHOR_{bone_name}"
        spec.target = source_armature
        spec.subtarget = bone_name
        spec.influence = 1.0
        spec.mute = False
        if _has_matching_pose_constraint(target_bone, spec):
            continue
        if _copy_pose_constraint(target_bone, spec) is not None:
            changed += 1
    return changed


def copy_dangle_anchor_constraints_to_driver(driver_armature, target_armature) -> int:
    if not _is_dangle_constraint_armature(driver_armature):
        return 0
    if target_armature is None or safe_object_type(target_armature) != "ARMATURE":
        return 0
    changed = 0
    for driver_bone in _pose_bones(driver_armature):
        bone_name = str(getattr(driver_bone, "name", "") or "")
        if not bone_name or _dangle_driver_can_drive_bone(driver_armature, bone_name):
            continue
        target_bone = _pose_bone_named(target_armature, bone_name)
        if target_bone is None:
            continue
        spec = type("_ConstraintSpec", (), {})()
        spec.type = "COPY_TRANSFORMS"
        spec.name = f"W3_DANGLE_ANCHOR_{bone_name}"
        spec.target = target_armature
        spec.subtarget = bone_name
        spec.influence = 1.0
        spec.mute = False
        if _has_matching_pose_constraint(driver_bone, spec):
            continue
        if _copy_pose_constraint(driver_bone, spec) is not None:
            changed += 1
    return changed


def _objects_bound_to_armature(source_armature, source_objects=None) -> tuple[list[Any], list[Any]]:
    objects = [obj for obj in (source_objects or []) if object_still_exists(obj)]
    seen = {id(obj) for obj in objects if obj is not None}
    for obj in _iter_bpy_objects():
        if obj is None or id(obj) in seen:
            continue
        if getattr(obj, "parent", None) is source_armature:
            objects.append(obj)
            seen.add(id(obj))
            continue
        if getattr(obj, "type", "") != "MESH":
            continue
        for modifier in getattr(obj, "modifiers", []) or []:
            if getattr(modifier, "type", "") == "ARMATURE" and getattr(modifier, "object", None) is source_armature:
                objects.append(obj)
                seen.add(id(obj))
                break

    reparented = []
    retargeted = []
    for obj in objects:
        if getattr(obj, "parent", None) is source_armature and obj not in reparented:
            reparented.append(obj)
        if getattr(obj, "type", "") == "MESH":
            for modifier in getattr(obj, "modifiers", []) or []:
                if getattr(modifier, "type", "") == "ARMATURE" and getattr(modifier, "object", None) is source_armature:
                    if obj not in retargeted:
                        retargeted.append(obj)
                    break
    return reparented, retargeted


def _retarget_armature_modifiers(mesh_objects, source_armature, target_armature):
    changed = []
    for obj in mesh_objects or []:
        for modifier in getattr(obj, "modifiers", []) or []:
            if getattr(modifier, "type", "") != "ARMATURE":
                continue
            if getattr(modifier, "object", None) is source_armature:
                modifier.object = target_armature
                if obj not in changed:
                    changed.append(obj)
    return changed


def _armature_deform_matrix_for_mesh(mesh_obj, armature_obj, pose_bone):
    try:
        mesh_world = mesh_obj.matrix_world.copy()
        arm_world = armature_obj.matrix_world.copy()
        bone_rest_inv = pose_bone.bone.matrix_local.inverted()
        return mesh_world.inverted() @ arm_world @ pose_bone.matrix @ bone_rest_inv @ arm_world.inverted() @ mesh_world
    except Exception:
        return None


def _blend_armature_matrices(weighted_matrices):
    if not weighted_matrices:
        return None
    from mathutils import Matrix

    blended = Matrix(((0.0, 0.0, 0.0, 0.0),
                      (0.0, 0.0, 0.0, 0.0),
                      (0.0, 0.0, 0.0, 0.0),
                      (0.0, 0.0, 0.0, 0.0)))
    total = sum(weight for _matrix, weight in weighted_matrices)
    if total <= 0.000001:
        return None
    for matrix, weight in weighted_matrices:
        factor = weight / total
        for row in range(4):
            for col in range(4):
                blended[row][col] += matrix[row][col] * factor
    return blended


def _compensate_meshes_for_same_name_bone_retarget(mesh_objects, source_armature, target_armature) -> int:
    if source_armature is None or target_armature is None:
        return 0
    if safe_object_type(source_armature) != "ARMATURE" or safe_object_type(target_armature) != "ARMATURE":
        return 0

    target_pose_bones = getattr(getattr(target_armature, "pose", None), "bones", None)
    source_bones = {bone.name for bone in getattr(getattr(source_armature, "data", None), "bones", []) or []}
    target_bones = {bone.name for bone in getattr(getattr(target_armature, "data", None), "bones", []) or []}
    if not (source_bones & target_bones) or target_pose_bones is None:
        return 0

    try:
        import bpy
        bpy.context.view_layer.update()
    except Exception:
        bpy = None

    compensated_count = 0
    for mesh_obj in mesh_objects or []:
        if not object_still_exists(mesh_obj) or safe_object_type(mesh_obj) != "MESH":
            continue
        armature_mod = None
        for mod in getattr(mesh_obj, "modifiers", []) or []:
            if getattr(mod, "type", "") == "ARMATURE" and getattr(mod, "object", None) is source_armature:
                armature_mod = mod
                break
        if armature_mod is None:
            continue

        group_names = {
            group.name
            for group in getattr(mesh_obj, "vertex_groups", []) or []
            if group.name in source_bones
        }
        if not group_names or any(name not in target_bones for name in group_names):
            continue

        bone_matrices = {}
        for bone_name in group_names:
            pose_bone = target_pose_bones.get(bone_name)
            if pose_bone is None:
                bone_matrices = {}
                break
            matrix = _armature_deform_matrix_for_mesh(mesh_obj, target_armature, pose_bone)
            if matrix is None:
                bone_matrices = {}
                break
            bone_matrices[bone_name] = matrix
        if not bone_matrices:
            continue

        if bpy is None:
            continue
        dg = bpy.context.evaluated_depsgraph_get()
        eval_obj = mesh_obj.evaluated_get(dg)
        eval_mesh = None
        try:
            eval_mesh = eval_obj.to_mesh()
            if eval_mesh is None or len(eval_mesh.vertices) != len(mesh_obj.data.vertices):
                continue
            desired_coords = [vertex.co.copy() for vertex in eval_mesh.vertices]
        finally:
            if eval_mesh is not None:
                try:
                    eval_obj.to_mesh_clear()
                except Exception:
                    pass

        if getattr(mesh_obj.data, "users", 1) > 1:
            mesh_obj.data = mesh_obj.data.copy()
        index_to_name = {group.index: group.name for group in mesh_obj.vertex_groups}
        changed = False
        for vertex in mesh_obj.data.vertices:
            weighted_matrices = []
            for membership in vertex.groups:
                bone_name = index_to_name.get(membership.group)
                matrix = bone_matrices.get(bone_name)
                if matrix is not None and membership.weight > 0.000001:
                    weighted_matrices.append((matrix, float(membership.weight)))
            blended = _blend_armature_matrices(weighted_matrices)
            if blended is None:
                continue
            desired = desired_coords[vertex.index].to_4d()
            desired.w = 1.0
            try:
                compensated_coord = blended.inverted_safe() @ desired
            except Exception:
                continue
            if abs(compensated_coord.w) > 0.000001:
                vertex.co = compensated_coord.to_3d() / compensated_coord.w
            else:
                vertex.co = compensated_coord.to_3d()
            changed = True
        if changed:
            try:
                mesh_obj.data.update()
            except Exception:
                pass
            compensated_count += 1
    return compensated_count


def _iter_animation_data_owners():
    import bpy
    owners = []
    for obj in getattr(bpy.data, "objects", []) or []:
        owners.append(obj)
        data = getattr(obj, "data", None)
        shape_keys = getattr(data, "shape_keys", None)
        if shape_keys is not None:
            owners.append(shape_keys)
    for datablock_attr in ("armatures", "meshes", "materials", "cameras", "lights"):
        for datablock in getattr(bpy.data, datablock_attr, []) or []:
            owners.append(datablock)
    return owners


def retarget_driver_targets(source_armature, target_armature) -> int:
    if source_armature is None or target_armature is None:
        return 0
    source_data = getattr(source_armature, "data", None)
    target_data = getattr(target_armature, "data", None)
    changed = 0
    for owner in _iter_animation_data_owners():
        animation_data = getattr(owner, "animation_data", None)
        drivers = getattr(animation_data, "drivers", None) if animation_data is not None else None
        if not drivers:
            continue
        for fcurve in drivers:
            driver = getattr(fcurve, "driver", None)
            for variable in getattr(driver, "variables", []) or []:
                for target in getattr(variable, "targets", []) or []:
                    try:
                        if getattr(target, "id", None) is source_armature:
                            target.id = target_armature
                            changed += 1
                        elif source_data is not None and getattr(target, "id", None) is source_data:
                            if target_data is not None:
                                target.id_type = "ARMATURE"
                                target.id = target_data
                                changed += 1
                    except Exception:
                        continue
    return changed


def _update_deleted_armature_references(source_armature, target_armature):
    source_name = str(getattr(source_armature, "name", "") or "")
    target_name = str(getattr(target_armature, "name", "") or "")
    if not source_name or not target_name:
        return 0
    props = (
        "mimicFace",
        "witcher_w2_mimic_armature",
        "witcher_w2_mimic_parent_armature",
        "witcher_w2_mimic_mesh_parent_rig",
        "witcher_w2_head_parent_armature",
    )
    changed = 0
    for obj in _iter_bpy_objects():
        for prop_name in props:
            try:
                if str(obj.get(prop_name, "") or "") == source_name:
                    obj[prop_name] = target_name
                    changed += 1
            except Exception:
                continue
    return changed


def _copy_bone_collections(target_armature, source_armature, bone_names: list[str]):
    target_data = getattr(target_armature, "data", None)
    source_bones = _armature_bones_by_name(source_armature)
    if not hasattr(target_data, "collections"):
        return
    for bone_name in bone_names:
        source_bone = source_bones.get(bone_name)
        target_bone = target_data.bones.get(bone_name) if target_data is not None else None
        if source_bone is None or target_bone is None:
            continue
        for source_collection in getattr(source_bone, "collections", []) or []:
            coll_name = str(getattr(source_collection, "name", "") or "")
            if not coll_name:
                continue
            coll = target_data.collections.get(coll_name)
            if coll is None:
                coll = target_data.collections.new(coll_name)
            try:
                coll.assign(target_bone)
            except Exception:
                pass


def _existing_grafted_chain_names(target_armature, source_armature, source_name: str) -> list[str]:
    target_bones = _armature_bones_by_name(target_armature)
    source_bones = _armature_bones_by_name(source_armature)
    names = []
    bone = source_bones.get(source_name)
    while bone is not None:
        bone_name = str(getattr(bone, "name", "") or "")
        target_bone = target_bones.get(bone_name)
        if target_bone is None:
            break
        try:
            has_provenance = bool(target_bone.get(SRC_REFCOUNTS_PROP, ""))
        except Exception:
            has_provenance = False
        if not has_provenance:
            break
        names.append(bone_name)
        bone = getattr(bone, "parent", None)
    return names


def graft_armature_into_master(master_armature, child_armature, *, src_id: str = "",
                               source_objects=None, attachment_parent_bone: str = "",
                               context=None, remove_child: bool = True,
                               include_own_skeleton_attachments: bool = False,
                               reparent_retargeted_meshes: bool = False,
                               parent_added_roots_to_master_root: bool = True) -> GraftResult:
    result = GraftResult()
    if master_armature is None or child_armature is None or master_armature is child_armature:
        return result
    if not object_still_exists(master_armature) or not object_still_exists(child_armature):
        return result
    if safe_object_type(master_armature) != "ARMATURE" or safe_object_type(child_armature) != "ARMATURE":
        return result
    if is_own_skeleton_attachment(child_armature) and not include_own_skeleton_attachments:
        return result

    import bpy
    from ..unreal_export.scene_utils import _restore_object_state, _snapshot_object_state

    src_id = graft_source_id(master_armature, child_armature, src_id)
    source_bones = _armature_bones_by_name(child_armature)
    target_bones = _armature_bones_by_name(master_armature)
    wanted = [name for name in source_bones if name not in target_bones]
    required_names = _required_source_bone_names(child_armature, wanted, set(target_bones))
    attachment_parent_bone = str(attachment_parent_bone or "").strip()
    if include_own_skeleton_attachments and not attachment_parent_bone:
        try:
            attachment_parent_bone = str(child_armature.get("w2_special_parent_bone", "") or "").strip()
        except Exception:
            attachment_parent_bone = ""

    reparented, retargeted = _objects_bound_to_armature(child_armature, source_objects)
    reparent_targets = list(reparented)
    if reparent_retargeted_meshes:
        for obj in retargeted:
            if obj not in reparent_targets:
                reparent_targets.append(obj)
    reparent_cache = []
    for obj in reparent_targets:
        try:
            world = obj.matrix_world.copy()
        except Exception:
            world = None
        reparent_cache.append({
            "obj": obj,
            "world": world,
            "parent_type": getattr(obj, "parent_type", "OBJECT"),
            "parent_bone": getattr(obj, "parent_bone", ""),
            "force_parent_to_master": obj not in reparented,
        })

    saved_state = _snapshot_object_state(context or bpy.context)
    added_bones = []
    try:
        if saved_state[2] != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        bpy.ops.object.select_all(action="DESELECT")
        master_armature.select_set(True)
        (context or bpy.context).view_layer.objects.active = master_armature
        bpy.ops.object.mode_set(mode="EDIT")

        edit_bones = master_armature.data.edit_bones
        root_name = str(attachment_parent_bone or "").strip()
        if not root_name and parent_added_roots_to_master_root:
            root_name = _main_root_bone_name(master_armature)
        for bone_name in required_names:
            edit_bone = _copy_source_bone_to_edit_bones(
                edit_bones,
                bone_name,
                source_bones,
                child_armature,
                master_armature,
                root_name,
                required_names=required_names,
                attachment_parent_bone=attachment_parent_bone,
            )
            if edit_bone is not None:
                added_bones.append(bone_name)
        bpy.ops.object.mode_set(mode="OBJECT")
    except Exception:
        try:
            if bpy.context.object and bpy.context.object.mode != "OBJECT":
                bpy.ops.object.mode_set(mode="OBJECT")
        except Exception:
            pass
        raise
    finally:
        _restore_object_state(context or bpy.context, saved_state)

    for bone_name in added_bones:
        bone = master_armature.data.bones.get(bone_name)
        if bone is not None:
            _add_source_owner_to_bone(bone, src_id)

    shared_names = []
    if src_id:
        for source_name in source_bones:
            if source_name in target_bones and source_name not in added_bones:
                for chain_name in _existing_grafted_chain_names(master_armature, child_armature, source_name):
                    bone = master_armature.data.bones.get(chain_name)
                    if bone is not None and _add_source_owner_to_bone(bone, src_id):
                        shared_names.append(chain_name)

    _copy_bone_collections(master_armature, child_armature, added_bones)
    _compensate_meshes_for_same_name_bone_retarget(retargeted, child_armature, master_armature)
    changed_mods = _retarget_armature_modifiers(retargeted, child_armature, master_armature)
    changed_constraints = _retarget_constraints_to_armature(child_armature, master_armature)
    changed_constraints += _copy_dangle_driver_constraints_to_master(child_armature, master_armature)

    for child_data in reparent_cache:
        obj = child_data["obj"]
        if obj is None:
            continue
        parent_bone = str(child_data.get("parent_bone", "") or "")
        if child_data.get("force_parent_to_master", False):
            target_parent, target_parent_type, target_parent_bone = master_armature, "OBJECT", ""
        else:
            target_parent, target_parent_type, target_parent_bone = _graft_reparent_target(
                master_armature,
                child_armature,
                parent_bone,
            )
        _set_parent_keep_world(
            obj,
            target_parent,
            parent_type=target_parent_type,
            parent_bone=target_parent_bone,
        )
        if child_data["world"] is not None:
            try:
                obj.matrix_world = child_data["world"]
            except Exception:
                pass

    retarget_driver_targets(child_armature, master_armature)
    _update_deleted_armature_references(child_armature, master_armature)

    removed = False
    if remove_child:
        source_data = child_armature.data
        try:
            bpy.data.objects.remove(child_armature, do_unlink=True)
            removed = True
        except Exception:
            removed = False
        if removed and source_data and source_data.users == 0:
            try:
                bpy.data.armatures.remove(source_data)
            except Exception:
                pass

    result.merged = True
    result.added_bones = added_bones
    result.shared_bones = sorted(set(shared_names))
    result.reparented_objects = reparent_targets
    result.retargeted_objects = changed_mods
    result.retargeted_constraints = changed_constraints
    result.removed_armature = removed
    return result


def _constraint_target_armature_names(arm) -> set[str]:
    names: set[str] = set()
    pose = getattr(arm, "pose", None)
    for pose_bone in getattr(pose, "bones", []) or []:
        for constraint in getattr(pose_bone, "constraints", []) or []:
            if getattr(constraint, "type", "") not in {"COPY_TRANSFORMS", "CHILD_OF"}:
                continue
            target = getattr(constraint, "target", None)
            if target is not None and safe_object_type(target) == "ARMATURE":
                name = safe_object_name(target)
                if name:
                    names.add(name)
    return names


def unify_character_armatures(master_armature, *, context=None, src_id: str = "", force: bool = False,
                              include_own_skeleton_attachments: bool = False) -> int:
    """Graft every mesh-binding child armature bound to ``master_armature`` into it.

    Single post-import pass: collect the transitive closure of armatures bound to
    the master with name-matching constraints (the per-mesh/cloth/hair rigs the
    importer creates), then graft each one in and delete it. Own-skeleton
    attachments (scabbards, animated sub-skeletons) stay separate unless
    explicitly included.
    """
    import bpy

    if master_armature is None or safe_object_type(master_armature) != "ARMATURE":
        return 0
    if not force and not should_unify_character_armature(context):
        return 0

    master_name = safe_object_name(master_armature)

    pool = []
    for obj in list(bpy.data.objects):
        if obj is master_armature or safe_object_type(obj) != "ARMATURE":
            continue
        name = safe_object_name(obj)
        if not name:
            continue
        if is_own_skeleton_attachment(obj) and not include_own_skeleton_attachments:
            continue
        if _is_dangle_constraint_armature(obj):
            continue
        if _is_dangle_buffer_armature(obj):
            continue
        pool.append(obj)

    reached = {master_name}
    ordered: list[str] = []
    changed = True
    while changed:
        changed = False
        for obj in list(pool):
            if _constraint_target_armature_names(obj) & reached:
                name = safe_object_name(obj)
                ordered.append(name)
                reached.add(name)
                pool.remove(obj)
                changed = True

    merged = 0
    for name in ordered:
        child = bpy.data.objects.get(name)
        if child is None or safe_object_type(child) != "ARMATURE":
            continue
        try:
            result = graft_armature_into_master(
                master_armature,
                child,
                src_id=src_id or graft_source_id(master_armature, child),
                attachment_parent_bone=(
                    str(child.get("w2_special_parent_bone", "") or "").strip()
                    if include_own_skeleton_attachments else ""
                ),
                context=context,
                include_own_skeleton_attachments=include_own_skeleton_attachments,
            )
            if result.merged:
                merged += 1
        except Exception:
            log.warning("Failed to unify armature '%s' into '%s'.", name, master_name, exc_info=True)
    if merged:
        log.info("Unified %d armature(s) into '%s'.", merged, master_name)
    return merged


def _bone_depth(bone) -> int:
    depth = 0
    parent = getattr(bone, "parent", None)
    while parent is not None:
        depth += 1
        parent = getattr(parent, "parent", None)
    return depth


def remove_grafted_source(master_armature, src_id: str, *, context=None) -> int:
    import bpy
    from ..unreal_export.scene_utils import _restore_object_state, _snapshot_object_state

    src_id = str(src_id or "").strip()
    if not src_id or master_armature is None or getattr(master_armature, "type", "") != "ARMATURE":
        return 0

    delete_names = set()
    for bone in list(getattr(master_armature.data, "bones", []) or []):
        if _remove_source_owner_from_bone(bone, src_id):
            delete_names.add(str(getattr(bone, "name", "") or ""))
    if not delete_names:
        return 0

    for bone in list(getattr(master_armature.data, "bones", []) or []):
        bone_name = str(getattr(bone, "name", "") or "")
        if bone_name not in delete_names:
            continue
        for child in getattr(bone, "children", []) or []:
            child_name = str(getattr(child, "name", "") or "")
            if child_name and child_name not in delete_names:
                delete_names.discard(bone_name)
                break
    if not delete_names:
        return 0

    bones_by_name = _armature_bones_by_name(master_armature)
    ordered = sorted(
        [bones_by_name[name] for name in delete_names if name in bones_by_name],
        key=_bone_depth,
        reverse=True,
    )
    if not ordered:
        return 0

    saved_state = _snapshot_object_state(context or bpy.context)
    removed = 0
    try:
        if saved_state[2] != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        bpy.ops.object.select_all(action="DESELECT")
        master_armature.select_set(True)
        (context or bpy.context).view_layer.objects.active = master_armature
        bpy.ops.object.mode_set(mode="EDIT")
        edit_bones = master_armature.data.edit_bones
        for bone in ordered:
            edit_bone = edit_bones.get(getattr(bone, "name", ""))
            if edit_bone is None:
                continue
            try:
                edit_bones.remove(edit_bone)
                removed += 1
            except Exception:
                pass
        bpy.ops.object.mode_set(mode="OBJECT")
    finally:
        try:
            if bpy.context.object and bpy.context.object.mode != "OBJECT":
                bpy.ops.object.mode_set(mode="OBJECT")
        except Exception:
            pass
        _restore_object_state(context or bpy.context, saved_state)
    return removed


def _armature_has_source(master_armature, src_id: str) -> bool:
    for bone in getattr(getattr(master_armature, "data", None), "bones", []) or []:
        try:
            if src_id in _parse_refcounts(bone.get(SRC_REFCOUNTS_PROP, "")):
                return True
        except Exception:
            continue
    return False


def remove_grafted_source_from_scene(src_id: str, *, context=None) -> int:
    src_id = str(src_id or "").strip()
    if not src_id:
        return 0
    removed = 0
    for obj in _iter_bpy_objects():
        if getattr(obj, "type", "") != "ARMATURE":
            continue
        if not _armature_has_source(obj, src_id):
            continue
        try:
            removed += remove_grafted_source(obj, src_id, context=context)
        except Exception:
            log.warning("Failed to remove grafted bones for source '%s' on '%s'.", src_id, getattr(obj, "name", ""), exc_info=True)
    return removed
