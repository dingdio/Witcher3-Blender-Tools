"""Blender integration checks for native CHardAttachment representation.

Run with Blender 4.5+:
  blender --background --factory-startup --python tests/blender_hard_attachment_native.py
"""

import json
import math
import os
import sys
import tempfile
from pathlib import Path

import bpy
from mathutils import Matrix


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import witcher3_tools as addon
from witcher3_tools import constrain_util
from witcher3_tools.CR2W.dc_entity import LoadCEntityTemplateFile
from witcher3_tools.attachment_math import (
    HAF_FREE_POSITION_AXIS_X,
    HAF_FREE_ROTATION,
    coerce_attachment_flags,
)
from witcher3_tools.importers import import_entity
from witcher3_tools.ui import ui_animated_component
from witcher3_tools.ui import ui_anims


def assert_matrix_close(actual, expected, rel_tol=2e-5, abs_tol=2e-5):
    for row in range(4):
        for col in range(4):
            a = float(actual[row][col])
            b = float(expected[row][col])
            assert math.isclose(a, b, rel_tol=rel_tol, abs_tol=abs_tol), (
                row, col, a, b, abs(a - b)
            )


def create_armature(name, bone_specs):
    data = bpy.data.armatures.new(name + "Data")
    obj = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.mode_set(mode='EDIT')
    made = {}
    for bone_name, parent_name, head, tail in bone_specs:
        bone = data.edit_bones.new(bone_name)
        bone.head = head
        bone.tail = tail
        if parent_name:
            bone.parent = made[parent_name]
        made[bone_name] = bone
    bpy.ops.object.mode_set(mode='OBJECT')
    obj.select_set(False)
    data.witcherui_RigSettings.rot90_state = 'OFF'
    return obj


def new_mesh_object(name):
    mesh = bpy.data.meshes.new(name + "Data")
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


addon.register()
try:
    parent = create_armature("player:man_base_ARM", [
        ("root", None, (0, 0, 0), (0, 0.1, 0)),
        ("torso3", "root", (0, 0, 1.4), (0, 0.1, 1.4)),
    ])
    parent.matrix_world = Matrix.Translation((1000.125, -725.75, 42.5))
    assert hasattr(parent.data.witcherui_RigSettings, "rot90_state")
    addon.set_rig_rot90_enabled(parent.data.witcherui_RigSettings, True)
    assert parent.data.witcherui_RigSettings.rot90_state == 'ON'
    addon.set_rig_rot90_enabled(parent.data.witcherui_RigSettings, False)
    assert parent.data.witcherui_RigSettings.rot90_state == 'OFF'

    scabbards = create_armature("player:scabbards_skeleton_ARM", [
        ("scabbards_root", None, (0, 0, 0), (0, 0.1, 0)),
        ("steel_sword_scabbard_1", "scabbards_root", (0, 0, 0.3), (0, 0.1, 0.3)),
        ("steel_sword_back", "steel_sword_scabbard_1", (0.2, 0, 0.6), (0.2, 0.1, 0.6)),
    ])

    import_entity.process_special_attachment({
        "parent_name": parent.name,
        "child_name": scabbards.name,
        "parentSlotName": "torso3",
        "relativeTransform": None,
        "attachmentFlags": 0,
    }, {parent.name: parent, scabbards.name: scabbards})

    assert scabbards.parent is parent
    assert scabbards.get("w2_special_attachment_mode") == "root_copy"
    root_constraint = next(
        c for c in scabbards.pose.bones[0].constraints
        if c.type == 'COPY_TRANSFORMS'
    )
    assert root_constraint.target is parent
    assert root_constraint.subtarget == "torso3"

    slot = bpy.data.objects.new("player:steel_sword_back_slot", None)
    bpy.context.scene.collection.objects.link(slot)
    import_entity.set_empty_bone_offset(slot, scabbards, "steel_sword_back", None)
    sword = new_mesh_object("steel_unique_arbitrator_lod0")
    sword.parent = slot
    sword.matrix_parent_inverse = Matrix.Identity(4)
    sword.matrix_basis = Matrix.Identity(4)
    bpy.context.view_layer.update()

    expected_slot = scabbards.matrix_world @ scabbards.pose.bones["steel_sword_back"].matrix
    assert_matrix_close(slot.matrix_world, expected_slot)
    assert_matrix_close(sword.matrix_world, slot.matrix_world)
    before = slot.matrix_world.copy()
    parent.pose.bones["torso3"].location.x += 1.75
    bpy.context.view_layer.update()
    after = slot.matrix_world.copy()
    assert not math.isclose(before.translation.x, after.translation.x, abs_tol=1e-4)
    expected_slot = scabbards.matrix_world @ scabbards.pose.bones["steel_sword_back"].matrix
    assert_matrix_close(after, expected_slot)

    # Duplicate skeletons use full-armature matching.
    matched_parent = create_armature("MatchedParent", [
        ("root", None, (0, 0, 0), (0, 0.1, 0)),
        ("spine", "root", (0, 0, 0.5), (0, 0.1, 0.5)),
    ])
    matched_child = create_armature("MatchedChild", [
        ("root", None, (0, 0, 0), (0, 0.1, 0)),
        ("spine", "root", (0, 0, 0.5), (0, 0.1, 0.5)),
    ])
    assert constrain_util.should_auto_align_armatures(matched_parent, matched_child)
    import_entity.process_special_attachment({
        "parent_name": matched_parent.name,
        "child_name": matched_child.name,
        "parentSlotName": "root",
        "relativeTransform": None,
        "attachmentFlags": 0,
    }, {matched_parent.name: matched_parent, matched_child.name: matched_child})
    assert matched_child.get("w2_special_attachment_mode") == "matched_armature"
    assert any(
        c.target is matched_parent
        for bone in matched_child.pose.bones
        for c in bone.constraints
        if hasattr(c, "target")
    )

    # Rigid attachments use a bone-head constraint and preserve the component transform.
    rigid = new_mesh_object("RigidComponent_lod0")
    rigid.matrix_basis = Matrix.Translation((0.25, -0.5, 0.75))
    original_basis = rigid.matrix_basis.copy()
    anchor = import_entity._link_hard_attachment_anchor(
        parent, rigid, "torso3", {"X": 0.2, "Yaw": 15.0}, 0
    )
    assert anchor.parent is parent
    assert anchor.parent_type == 'OBJECT'
    assert len(anchor.constraints) == 1
    anchor_constraint = anchor.constraints[0]
    assert anchor_constraint.type == 'COPY_TRANSFORMS'
    assert anchor_constraint.target is parent
    assert anchor_constraint.subtarget == "torso3"
    assert anchor_constraint.head_tail == 0.0
    assert anchor_constraint.owner_space == 'LOCAL'
    assert anchor_constraint.target_space == 'POSE'
    assert anchor_constraint.mix_mode in {'BEFORE_FULL', 'BEFORE'}
    assert not bool(anchor.get("witcher_hard_attachment_runtime", False))
    assert_matrix_close(rigid.matrix_basis, original_basis)
    relative_basis = anchor.matrix_basis.copy()
    bpy.context.view_layer.update()
    expected_anchor = parent.matrix_world @ parent.pose.bones["torso3"].matrix @ relative_basis
    assert_matrix_close(anchor.matrix_world, expected_anchor)

    # Bone-head placement is independent of bone length.
    head_rig = create_armature("HeadConstraintRig", [
        ("mount", None, (1.0, 2.0, 3.0), (1.0, 7.0, 3.0)),
    ])
    head_rig.matrix_world = (
        Matrix.Translation((15.0, -4.0, 8.0))
        @ Matrix.Rotation(math.radians(37.0), 4, 'Z')
    )
    head_mesh = new_mesh_object("HeadConstraintMesh")
    head_anchor = import_entity._link_hard_attachment_anchor(
        head_rig, head_mesh, "mount", None, 0
    )
    bpy.context.view_layer.update()
    head_world = head_rig.matrix_world @ head_rig.pose.bones["mount"].matrix
    tail_world = head_rig.matrix_world @ head_rig.pose.bones["mount"].tail
    assert_matrix_close(head_anchor.matrix_world, head_world)
    assert (head_anchor.matrix_world.translation - tail_world).length > 4.99

    # Nonzero flags are preserved and marked unsupported.
    flagged = new_mesh_object("FlaggedRigid_lod0")
    import_entity._WARNED_ATTACHMENT_FLAGS.add((parent.name, flagged.name, 1))
    flagged_anchor = import_entity._link_hard_attachment_anchor(
        parent, flagged, "torso3", None, HAF_FREE_POSITION_AXIS_X
    )
    assert flagged_anchor.get("witcher_attachment_flags") == HAF_FREE_POSITION_AXIS_X
    assert "unsupported" in flagged_anchor.get("witcher_attachment_flags_warning", "").lower()
    assert not bool(flagged_anchor.get("witcher_hard_attachment_runtime", False))

    # Missing special slots fall back to object parenting.
    missing_child = create_armature("MissingSlotChild", [
        ("only_root", None, (0, 0, 0), (0, 0.1, 0)),
    ])
    import_entity.process_special_attachment({
        "parent_name": parent.name,
        "child_name": missing_child.name,
        "parentSlotName": "does_not_exist",
        "relativeTransform": None,
        "attachmentFlags": 0,
    }, {parent.name: parent, missing_child.name: missing_child})
    assert missing_child.parent is parent
    assert missing_child.get("w2_special_attachment_mode") == "object_fallback"

    # Hard attachments register no persistent Python handlers.
    handler_groups = (
        bpy.app.handlers.depsgraph_update_post,
        bpy.app.handlers.frame_change_post,
        bpy.app.handlers.load_post,
    )
    assert not any(
        "hard_attachment" in getattr(handler, "__name__", "")
        for group in handler_groups
        for handler in group
    )

    # Authoring/export preserves the slot, flags, and transforms.
    authored = create_armature("AuthoredComponent", [
        ("Root", None, (0, 0, 0), (0.1, 0, 0)),
        ("Trajectory01", "Root", (0, 0, 0), (0.1, 0, 0)),
    ])
    authored[ui_animated_component.P_TYPE] = ui_animated_component.T_ANIMATED_COMPONENT
    authored_mesh = new_mesh_object("AuthoredMesh")
    authored_anchor = ui_animated_component.add_hard_attachment(
        authored,
        authored_mesh,
        "Trajectory01",
        r"environment\decorations\test.w2mesh",
    )
    authored_anchor[ui_animated_component.P_ATTACHMENT_FLAGS] = 0
    authored_anchor[ui_animated_component.P_ATTACHMENT_RELATIVE] = json.dumps({"X": 0.5})
    authored_mesh["witcher_component_transform"] = json.dumps({"Scale_x": 1.25})
    attachments, skipped = ui_animated_component.collect_hard_attachment_export_data(authored)
    assert not skipped
    assert len(attachments) == 1
    assert attachments[0]["slot"] == "Trajectory01"
    assert attachments[0]["relative_transform"]["X"] == 0.5
    assert attachments[0]["component_transform"]["Scale_x"] == 1.25

    # Export paths stay within the configured project root.
    assert ui_anims._anim_compute_full_export_path(
        r"C:\project\workspace", r"..\outside.w2cutscene"
    ) is None
    assert ui_anims._cutscene_actor_template_full_path(
        r"C:\project", r"C:\outside\actor.w2ent"
    ) is None

    # Parse a flagged binary when one is supplied.
    flagged_entity_path = os.environ.get("W3TB_FLAGGED_ENTITY", "").strip()
    if flagged_entity_path:
        parsed_mesh, parsed_entity = LoadCEntityTemplateFile(flagged_entity_path)
        chunks = list(getattr(parsed_mesh, "chunks", None) or [])
        chunks.extend(getattr(getattr(parsed_entity, "staticMeshes", None), "chunks", None) or [])
        parsed = [chunk for chunk in chunks if getattr(chunk, "type", None) == "CHardAttachment"]
        assert parsed
        assert coerce_attachment_flags(parsed[0].attachmentFlags) == (
            HAF_FREE_POSITION_AXIS_X | HAF_FREE_ROTATION
        )

    # Attachment parenting and constraints survive save/reload.
    blend_path = os.path.join(tempfile.gettempdir(), "w3tb_hard_attachment_native.blend")
    bpy.ops.wm.save_as_mainfile(filepath=blend_path)
    bpy.ops.wm.open_mainfile(filepath=blend_path)
    reloaded_scabbards = bpy.data.objects["player:scabbards_skeleton_ARM"]
    reloaded_slot = bpy.data.objects["player:steel_sword_back_slot"]
    reloaded_root_constraint = next(
        c for c in reloaded_scabbards.pose.bones[0].constraints
        if c.type == 'COPY_TRANSFORMS'
    )
    assert reloaded_root_constraint.subtarget == "torso3"
    assert reloaded_slot.constraints[0].subtarget == "steel_sword_back"
    os.remove(blend_path)

    print("W3TB_HARD_ATTACHMENT_NATIVE_OK")
finally:
    addon.unregister()
