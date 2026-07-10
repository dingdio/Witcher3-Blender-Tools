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

from ..rigging.armature_merge import (
    _armature_bones_by_name,
    _copy_source_bone_to_edit_bones,
    _find_attachment_armature_for_missing_bones,
    _main_root_bone_name,
    _missing_armature_bone_names,
    _required_source_bone_names,
)
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


def _object_prop(obj, name):
    try:
        return obj.get(name)
    except Exception:
        return None


def _is_hard_attachment_anchor(obj) -> bool:
    if obj is None:
        return False
    if str(_object_prop(obj, "witcher_type") or "") == "CHardAttachment":
        return True
    return str(getattr(obj, "name", "") or "").startswith("CHardAttachment")


def _hard_attachment_constraint_socket(anchor, main_armature, main_bones) -> Optional[str]:
    if not _is_hard_attachment_anchor(anchor):
        return None
    for constraint in getattr(anchor, "constraints", []) or []:
        if getattr(constraint, "type", "") not in {"COPY_TRANSFORMS", "COPY_LOCATION", "COPY_ROTATION"}:
            continue
        if getattr(constraint, "target", None) is not main_armature:
            continue
        bone = str(getattr(constraint, "subtarget", "") or "")
        if bone and bone in main_bones:
            return bone
    bone = str(_object_prop(anchor, "witcher_parent_slot_name") or "")
    if bone and bone in main_bones:
        return bone
    return None


def _special_attachment_socket(armature) -> Optional[str]:
    """Return the main-rig socket recorded for a separate attachment skeleton."""
    if armature is None:
        return None
    if not _object_prop(armature, "w2_special_attachment"):
        return None
    bone = str(_object_prop(armature, "w2_special_parent_bone") or "").strip()
    return bone or None


def _hard_attachment_socket(group_objects, main_armature) -> Optional[str]:
    """Return the main-rig socket used by a rigid attachment mesh.

    Rigid attachments have no armature modifier, so the socket is read from a
    CHardAttachment ancestor's constraint or bone parent.
    """
    if main_armature is None:
        return None
    main_bones = _armature_bones_by_name(main_armature)
    for obj in group_objects:
        node = obj
        depth = 0
        while node is not None and depth < 16:
            constraint_bone = _hard_attachment_constraint_socket(node, main_armature, main_bones)
            if constraint_bone:
                return constraint_bone
            if (
                _is_hard_attachment_anchor(node)
                and
                getattr(node, "parent_type", "") == "BONE"
                and getattr(node, "parent", None) is main_armature
            ):
                bone = str(getattr(node, "parent_bone", "") or "")
                if bone and bone in main_bones:
                    return bone
            node = getattr(node, "parent", None)
            depth += 1
    return None


@contextmanager
def _rigid_attachment_skinning(context, group_objects, main_armature, slot_bone: str,
                               asset_rel: str, warnings: list[str]):
    """Temporarily skin a hard-attachment mesh group rigidly to ``slot_bone``.

    Re-binds each mesh so the FBX exporter writes it as a skeletal mesh weighted
    100% to one rig bone, against the shared rig: the bone-parent (which the FBX
    exporter cannot represent) is swapped for an object parent + full-weight
    vertex group + armature modifier, with world position preserved. Unreal then
    imports it on the shared skeleton and the blueprint drives it as an ordinary
    leader-pose follower, so it tracks the slot bone exactly the way RED's hard
    attachment does -- without grafting bones onto the base skeleton. Everything
    is restored afterwards.
    """
    saved = []
    bound = 0
    try:
        for obj in group_objects:
            if getattr(obj, "type", "") != "MESH":
                continue
            mesh = getattr(obj, "data", None)
            vertices = getattr(mesh, "vertices", None)
            if vertices is None:
                continue
            world = obj.matrix_world.copy()
            vertex_indices = list(range(len(vertices)))

            vgroup = obj.vertex_groups.get(slot_bone)
            vgroup_weights = None
            if vgroup is not None:
                vgroup_weights = []
                vgroup_index = vgroup.index
                for vertex in vertices:
                    weight = None
                    for group in getattr(vertex, "groups", []) or []:
                        if getattr(group, "group", None) == vgroup_index:
                            weight = float(getattr(group, "weight", 0.0))
                            break
                    vgroup_weights.append(weight)

            modifier = next(
                (m for m in obj.modifiers if getattr(m, "type", "") == "ARMATURE"), None
            )
            record = {
                "obj": obj,
                "parent": obj.parent,
                "parent_type": obj.parent_type,
                "parent_bone": obj.parent_bone,
                "matrix_parent_inverse": obj.matrix_parent_inverse.copy(),
                "world": world,
                "created_vgroup": False,
                "vgroup_weights": vgroup_weights,
                "modifier": modifier,
                "modifier_object": getattr(modifier, "object", None) if modifier is not None else None,
                "created_modifier": None,
            }
            saved.append(record)

            obj.parent = main_armature
            obj.parent_type = "OBJECT"
            obj.parent_bone = ""
            try:
                obj.matrix_parent_inverse = main_armature.matrix_world.inverted()
            except Exception:
                pass
            obj.matrix_world = world

            if vgroup is None:
                vgroup = obj.vertex_groups.new(name=slot_bone)
                record["created_vgroup"] = True
            vgroup.add(vertex_indices, 1.0, "REPLACE")

            if modifier is None:
                modifier = obj.modifiers.new("Armature", "ARMATURE")
                record["created_modifier"] = modifier
            modifier.object = main_armature

            bound += 1

        if bound:
            warnings.append(
                f"{asset_rel}: exported as a rigid attachment skinned to bone "
                f"'{slot_bone}' (Unreal leader-pose follower)"
            )
        yield bound > 0
    finally:
        for record in reversed(saved):
            obj = record["obj"]
            try:
                if record["created_modifier"] is not None:
                    obj.modifiers.remove(record["created_modifier"])
                elif record["modifier"] is not None:
                    record["modifier"].object = record["modifier_object"]
            except Exception:
                pass
            try:
                if record["created_vgroup"]:
                    existing = obj.vertex_groups.get(slot_bone)
                    if existing is not None:
                        obj.vertex_groups.remove(existing)
                elif record["vgroup_weights"] is not None:
                    existing = obj.vertex_groups.get(slot_bone)
                    if existing is not None:
                        for index, weight in enumerate(record["vgroup_weights"]):
                            try:
                                if weight is None:
                                    existing.remove([index])
                                else:
                                    existing.add([index], weight, "REPLACE")
                            except Exception:
                                pass
            except Exception:
                pass
            try:
                obj.parent = record["parent"]
                obj.parent_type = record["parent_type"]
                obj.parent_bone = record["parent_bone"]
                obj.matrix_parent_inverse = record["matrix_parent_inverse"]
                obj.matrix_world = record["world"]
            except Exception:
                pass


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


@contextmanager
def _combined_export_armature_for_mesh_groups(context, group_armatures, main_armature, asset_rel: str,
                                              warnings: list[str],
                                              force_full_source_bone_names: set[str] | None = None,
                                              exclude_bone_names: set[str] | None = None):
    """Build one temporary armature with the union skeleton needed by preview meshes."""
    if main_armature is None:
        yield None
        return

    force_full_source_bone_names = set(force_full_source_bone_names or ())
    exclude_bone_names = set(exclude_bone_names or ())

    import bpy

    temp_data = main_armature.data.copy()
    temp_obj = bpy.data.objects.new(
        _unique_temp_object_name("__witcher_unreal_export_preview_armature", _iter_bpy_objects()),
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

        def current_names() -> set[str]:
            return {str(getattr(bone, "name", "") or "") for bone in edit_bones}

        for group_armature in group_armatures or []:
            if group_armature is None or group_armature is main_armature:
                continue
            missing = [
                name for name in _armature_bones_by_name(group_armature)
                if name not in current_names() and name not in exclude_bone_names
            ]
            if not missing:
                continue
            source_armature = (
                _find_attachment_armature_for_missing_bones(group_armature, missing)
                or group_armature
            )
            source_bones = _armature_bones_by_name(source_armature)
            if force_full_source_bone_names.intersection(source_bones):
                wanted = [
                    name for name in source_bones
                    if name not in current_names() and name not in exclude_bone_names
                ]
            else:
                wanted = [name for name in missing if name in source_bones and name not in exclude_bone_names]
            if not wanted:
                continue
            required_names = [
                name for name in _required_source_bone_names(source_armature, wanted, current_names())
                if name not in exclude_bone_names
            ]

            for bone_name in required_names:
                _copy_source_bone_to_edit_bones(
                    edit_bones,
                    bone_name,
                    source_bones,
                    source_armature,
                    temp_obj,
                    root_name,
                )

        bpy.ops.object.mode_set(mode="OBJECT")
        yield temp_obj
    except Exception:
        try:
            if bpy.context.object and bpy.context.object.mode != "OBJECT":
                bpy.ops.object.mode_set(mode="OBJECT")
        except Exception:
            pass
        raise
    finally:
        _restore_object_state(context, saved_state)
        _remove_object(temp_obj)


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

        for bone_name in required_names:
            _copy_source_bone_to_edit_bones(
                edit_bones,
                bone_name,
                source_bones,
                source_armature,
                temp_obj,
                root_name,
            )

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
