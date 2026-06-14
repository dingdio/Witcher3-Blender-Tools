"""Export-time armature resolution for Unreal mesh bundles.

Entity import binds each mesh to a flat per-mesh armature built from the
mesh's boneNames/boneMatrices (no hierarchy, no Root/Trajectory) and drives it
from the entity rig with constraints - RED's CMeshSkinningAttachment recreated
in Blender. Unreal needs the real rig hierarchy in every skeletal FBX so the
meshes share one skeleton, so this module decides which armature a mesh group's
FBX should carry and, when a mesh has dangle/hair bones the main rig lacks,
builds a temporary merged armature for the export.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Optional

from .scene_utils import (
    _iter_bpy_objects,
    _remove_object,
    _restore_object_state,
    _snapshot_object_state,
    _unique_temp_object_name,
)


def _group_armature(group_objects) -> Optional[Any]:
    for obj in group_objects:
        for modifier in getattr(obj, "modifiers", []) or []:
            if getattr(modifier, "type", "") == "ARMATURE" and getattr(modifier, "object", None):
                return modifier.object
        parent = getattr(obj, "parent", None)
        if parent is not None and getattr(parent, "type", "") == "ARMATURE":
            return parent
    return None


def _resolve_export_armature(group_armature, main_armature, asset_rel: str,
                             warnings: list[str]):
    """Pick the armature a skeletal mesh FBX should carry.

    Export against the main armature whenever the mesh bones all exist on it
    (the bind poses match because both come from the same skeleton); otherwise
    keep the mesh's own armature so its extra bones survive.
    """
    if group_armature is None or main_armature is None or group_armature is main_armature:
        return group_armature
    missing = _missing_armature_bone_names(group_armature, main_armature)
    if missing:
        warnings.append(
            f"{asset_rel}: merging {len(missing)} extra mesh bone(s) with "
            f"'{getattr(main_armature, 'name', '')}' for Unreal export "
            f"(e.g. {missing[0]})"
        )
        return group_armature
    return main_armature


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
        return (contains, single_root, parented, not_group)

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


@contextmanager
def _export_armature_for_mesh_group(context, group_armature, main_armature, asset_rel: str,
                                    warnings: list[str]):
    armature = _resolve_export_armature(group_armature, main_armature, asset_rel, warnings)
    temp_armature = None
    try:
        missing = _missing_armature_bone_names(group_armature, main_armature)
        if (
            missing
            and group_armature is not None
            and main_armature is not None
            and armature is group_armature
        ):
            source_armature = (
                _find_attachment_armature_for_missing_bones(group_armature, missing)
                or group_armature
            )
            try:
                temp_armature = _create_merged_export_armature(
                    context,
                    main_armature,
                    source_armature,
                    missing,
                    asset_rel,
                    warnings,
                )
            except Exception as exc:
                temp_armature = None
                warnings.append(
                    f"{asset_rel}: failed to build merged export armature; "
                    f"falling back to '{getattr(group_armature, 'name', '')}': {exc}"
                )
            if temp_armature is not None:
                armature = temp_armature
        yield armature
    finally:
        if temp_armature is not None:
            _remove_object(temp_armature)


def _create_merged_export_armature(context, main_armature, source_armature, wanted_names: list[str],
                                   asset_rel: str, warnings: list[str]):
    import bpy
    from mathutils import Vector

    main_bones = _armature_bones_by_name(main_armature)
    source_bones = _armature_bones_by_name(source_armature)
    wanted = [name for name in wanted_names if name in source_bones]
    unresolved = [name for name in wanted_names if name not in source_bones]
    if unresolved:
        warnings.append(
            f"{asset_rel}: {len(unresolved)} extra bone(s) were not found on "
            f"'{getattr(source_armature, 'name', '')}' (e.g. {unresolved[0]})"
        )
    if not wanted:
        return None

    required_names = _required_source_bone_names(source_armature, wanted, set(main_bones))
    if not required_names:
        return None

    temp_data = main_armature.data.copy()
    temp_obj = bpy.data.objects.new(
        _unique_temp_object_name("__witcher_unreal_export_merged_armature", _iter_bpy_objects()),
        temp_data,
    )
    try:
        temp_obj.matrix_world = main_armature.matrix_world.copy()
    except Exception:
        pass

    collection = getattr(context, "collection", None)
    if collection is None:
        collection = getattr(getattr(context, "scene", None), "collection", None)
    if collection is None:
        collection = bpy.context.scene.collection
    collection.objects.link(temp_obj)

    saved_state = _snapshot_object_state(context)

    try:
        if saved_state[2] != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        bpy.ops.object.select_all(action="DESELECT")
        temp_obj.select_set(True)
        context.view_layer.objects.active = temp_obj
        bpy.ops.object.mode_set(mode="EDIT")

        edit_bones = temp_data.edit_bones
        root_name = _main_root_bone_name(main_armature)
        temp_inv = temp_obj.matrix_world.inverted()
        source_world = source_armature.matrix_world

        for bone_name in required_names:
            if bone_name in edit_bones:
                continue
            source_bone = source_bones.get(bone_name)
            if source_bone is None:
                continue

            edit_bone = edit_bones.new(bone_name)
            source_head = source_world @ source_bone.head_local
            source_tail = source_world @ source_bone.tail_local
            head = temp_inv @ source_head
            tail = temp_inv @ source_tail
            if (tail - head).length < 0.000001:
                tail = head + Vector((0.0, 0.01, 0.0))
            edit_bone.head = head
            edit_bone.tail = tail
            try:
                edit_bone.matrix = temp_inv @ source_world @ source_bone.matrix_local
            except Exception:
                pass
            edit_bone.use_connect = False
            try:
                edit_bone.use_deform = bool(getattr(source_bone, "use_deform", True))
            except Exception:
                pass

            parent = getattr(source_bone, "parent", None)
            parent_name = str(getattr(parent, "name", "") or "")
            if parent_name and parent_name in edit_bones:
                edit_bone.parent = edit_bones[parent_name]
            elif root_name and root_name in edit_bones:
                edit_bone.parent = edit_bones[root_name]

        bpy.ops.object.mode_set(mode="OBJECT")
    except Exception:
        try:
            if bpy.context.object and bpy.context.object.mode != "OBJECT":
                bpy.ops.object.mode_set(mode="OBJECT")
        except Exception:
            pass
        _remove_object(temp_obj)
        raise
    finally:
        _restore_object_state(context, saved_state)

    return temp_obj


@contextmanager
def _retargeted_armature_modifiers(mesh_objects, armature):
    """Temporarily point the meshes' armature modifiers at ``armature`` so the
    FBX exporter writes their skinning against it (vertex groups map by bone
    name). Restores the original objects afterwards."""
    originals = []
    try:
        for obj in mesh_objects:
            for modifier in getattr(obj, "modifiers", []) or []:
                if getattr(modifier, "type", "") != "ARMATURE":
                    continue
                original = getattr(modifier, "object", None)
                if original is not None and original is not armature:
                    originals.append((modifier, original))
                    modifier.object = armature
        yield
    finally:
        for modifier, original in reversed(originals):
            try:
                modifier.object = original
            except Exception:
                pass
