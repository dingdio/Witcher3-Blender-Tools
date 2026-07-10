import json
import sys
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

if "witcher3_tools" not in sys.modules:
    _pkg = types.ModuleType("witcher3_tools")
    _pkg.__path__ = [str(REPO_ROOT / "witcher3_tools")]
    _pkg.__package__ = "witcher3_tools"
    sys.modules["witcher3_tools"] = _pkg

from witcher3_tools.rigging import armature_merge
from witcher3_tools.CR2W import w3_types


class FakeBone(dict):
    def __init__(self, name, parent=None):
        super().__init__()
        self.name = name
        self.parent = parent
        self.children = []
        if parent is not None:
            parent.children.append(self)


class FakeObject(dict):
    type = "ARMATURE"

    def __init__(self, name):
        super().__init__()
        self.name = name


class RemovedObject:
    def __getattribute__(self, name):
        if name.startswith("__"):
            return object.__getattribute__(self, name)
        raise ReferenceError("StructRNA of type Object has been removed")


def _armature(name, bones):
    return types.SimpleNamespace(
        name=name,
        type="ARMATURE",
        data=types.SimpleNamespace(bones=bones),
    )


class TestArmatureMergeProvenance(unittest.TestCase):
    def test_refcounts_survive_shared_owners_until_last_remove(self):
        bone = FakeBone("dyng_01")

        self.assertTrue(armature_merge._add_source_owner_to_bone(bone, "guid_a"))
        self.assertTrue(armature_merge._add_source_owner_to_bone(bone, "guid_a"))
        self.assertTrue(armature_merge._add_source_owner_to_bone(bone, "guid_b"))

        counts = json.loads(bone[armature_merge.SRC_REFCOUNTS_PROP])
        self.assertEqual(counts, {"guid_a": 2, "guid_b": 1})

        self.assertFalse(armature_merge._remove_source_owner_from_bone(bone, "guid_a"))
        counts = json.loads(bone[armature_merge.SRC_REFCOUNTS_PROP])
        self.assertEqual(counts, {"guid_a": 1, "guid_b": 1})

        self.assertFalse(armature_merge._remove_source_owner_from_bone(bone, "guid_b"))
        self.assertTrue(armature_merge._remove_source_owner_from_bone(bone, "guid_a"))
        self.assertNotIn(armature_merge.SRC_REFCOUNTS_PROP, bone)
        self.assertNotIn(armature_merge.SRC_GUID_PROP, bone)

    def test_existing_grafted_chain_adds_owner_to_collision_parents(self):
        target_root = FakeBone("Root")
        target_group = FakeBone("dyng_group", target_root)
        target_leaf = FakeBone("dyng_01", target_group)
        armature_merge._add_source_owner_to_bone(target_group, "old_guid")
        armature_merge._add_source_owner_to_bone(target_leaf, "old_guid")

        source_root = FakeBone("Root")
        source_group = FakeBone("dyng_group", source_root)
        source_leaf = FakeBone("dyng_01", source_group)

        target = _armature("target", [target_root, target_group, target_leaf])
        source = _armature("source", [source_root, source_group, source_leaf])

        chain = armature_merge._existing_grafted_chain_names(target, source, "dyng_01")

        self.assertEqual(chain, ["dyng_01", "dyng_group"])

    def test_graft_source_id_prefers_active_guid(self):
        master = FakeObject("master")
        child = FakeObject("child")
        master[armature_merge.ACTIVE_GRAFT_SRC_PROP] = "active_guid"
        child["witcher_equip_guid"] = "equip_guid"

        self.assertEqual(armature_merge.graft_source_id(master, child), "active_guid")

    def test_own_skeleton_attachment_requires_special_parent_bone(self):
        special = FakeObject("scabbard")
        special["w2_special_attachment"] = True
        special["w2_special_parent_bone"] = "torso3"
        matched_without_socket = FakeObject("duplicate")
        matched_without_socket["w2_special_attachment"] = True

        self.assertTrue(armature_merge.is_own_skeleton_attachment(special))
        self.assertFalse(armature_merge.is_own_skeleton_attachment(matched_without_socket))

    def test_merged_character_hierarchy_follows_parent_chain(self):
        master = FakeObject("master")
        master["witcher_merged_character_armature"] = True
        scabbard = FakeObject("scabbard")
        scabbard.parent = master
        loose = FakeObject("loose")

        self.assertTrue(armature_merge.armature_is_in_merged_character_hierarchy(scabbard))
        self.assertFalse(armature_merge.armature_is_in_merged_character_hierarchy(loose))

    def test_removed_rna_object_is_skipped_before_graft(self):
        removed = RemovedObject()
        live = FakeObject("live")

        self.assertFalse(armature_merge.object_still_exists(removed))
        self.assertEqual(armature_merge.safe_object_name(removed), "")

        result = armature_merge.graft_armature_into_master(live, removed)

        self.assertFalse(result.merged)


class FakeConstraint:
    def __init__(self, ctype, target, subtarget=""):
        self.type = ctype
        self.target = target
        self.subtarget = subtarget


class FakePoseBone:
    def __init__(self, constraints):
        self.constraints = constraints


def _armature_with_constraints(name, target_specs):
    """target_specs: list of (constraint_type, target_object)."""
    bones = [FakePoseBone([FakeConstraint(t, tgt) for (t, tgt) in target_specs])]
    return types.SimpleNamespace(
        name=name,
        type="ARMATURE",
        pose=types.SimpleNamespace(bones=bones),
    )


class TestUnifyHelpers(unittest.TestCase):
    def test_constraint_target_names_only_binding_constraints_to_armatures(self):
        master = FakeObject("master")
        mesh = types.SimpleNamespace(name="not_an_arm", type="MESH")
        arm = _armature_with_constraints(
            "child",
            [("COPY_TRANSFORMS", master), ("CHILD_OF", master), ("LIMIT_SCALE", master), ("COPY_LOCATION", mesh)],
        )
        names = armature_merge._constraint_target_armature_names(arm)
        self.assertEqual(names, {"master"})

    def test_constraint_target_names_empty_when_unbound(self):
        arm = _armature_with_constraints("loose", [])
        self.assertEqual(armature_merge._constraint_target_armature_names(arm), set())

    def test_graft_reparent_target_preserves_visual_parent(self):
        master = types.SimpleNamespace(name="master", data=types.SimpleNamespace(bones={}))
        appearance = FakeObject("appearance")
        child = FakeObject("child")
        child.parent = appearance
        child.parent_type = "OBJECT"

        parent, parent_type, parent_bone = armature_merge._graft_reparent_target(master, child)

        self.assertIs(parent, appearance)
        self.assertEqual(parent_type, "OBJECT")
        self.assertEqual(parent_bone, "")

    def test_graft_reparent_target_keeps_bone_parenting_on_master(self):
        master = types.SimpleNamespace(name="master", data=types.SimpleNamespace(bones={"torso3": object()}))
        appearance = FakeObject("appearance")
        child = FakeObject("child")
        child.parent = appearance
        child.parent_type = "OBJECT"

        parent, parent_type, parent_bone = armature_merge._graft_reparent_target(master, child, "torso3")

        self.assertIs(parent, master)
        self.assertEqual(parent_type, "BONE")
        self.assertEqual(parent_bone, "torso3")

    def test_retarget_constraints_moves_scabbard_slots_to_master_bone(self):
        master = _armature("master", [FakeBone("Root"), FakeBone("sword_back")])
        scabbard = _armature("scabbard", [FakeBone("Root"), FakeBone("sword_back")])
        slot_constraint = FakeConstraint("COPY_TRANSFORMS", scabbard, "sword_back")
        slot_empty = types.SimpleNamespace(name="slot", constraints=[slot_constraint])

        changed = armature_merge._retarget_constraints_to_armature(
            scabbard,
            master,
            objects=[slot_empty],
        )

        self.assertEqual(changed, 1)
        self.assertIs(slot_constraint.target, master)
        self.assertEqual(slot_constraint.subtarget, "sword_back")

    def test_retarget_constraints_skips_missing_subtarget(self):
        master = _armature("master", [FakeBone("Root")])
        scabbard = _armature("scabbard", [FakeBone("Root"), FakeBone("sword_back")])
        slot_constraint = FakeConstraint("COPY_TRANSFORMS", scabbard, "sword_back")
        slot_empty = types.SimpleNamespace(name="slot", constraints=[slot_constraint])

        changed = armature_merge._retarget_constraints_to_armature(
            scabbard,
            master,
            objects=[slot_empty],
        )

        self.assertEqual(changed, 0)
        self.assertIs(slot_constraint.target, scabbard)

    def test_merge_skeleton_data_adds_missing_parent_chain_before_child(self):
        base = w3_types.CSkeleton(bones=[
            w3_types.W3Bone(0, "Root", [0.0, 0.0, 0.0], -1, False, w3_types.Quaternion(0.0, 0.0, 0.0, 1.0), [1.0, 1.0, 1.0]),
            w3_types.W3Bone(1, "head", [0.0, 0.0, 1.0], 0, False, w3_types.Quaternion(0.0, 0.0, 0.0, 1.0), [1.0, 1.0, 1.0]),
        ])
        mimic = w3_types.CSkeleton(bones=[
            w3_types.W3Bone(0, "Root", [0.0, 0.0, 0.0], -1, False, w3_types.Quaternion(0.0, 0.0, 0.0, 1.0), [1.0, 1.0, 1.0]),
            w3_types.W3Bone(1, "head", [0.0, 0.0, 1.0], 0, False, w3_types.Quaternion(0.0, 0.0, 0.0, 1.0), [1.0, 1.0, 1.0]),
            w3_types.W3Bone(2, "face_root", [0.0, 0.0, 0.1], 1, False, w3_types.Quaternion(0.0, 0.0, 0.0, 1.0), [1.0, 1.0, 1.0]),
            w3_types.W3Bone(3, "jaw_face", [0.0, 0.0, 0.1], 2, False, w3_types.Quaternion(0.0, 0.0, 0.0, 1.0), [1.0, 1.0, 1.0]),
        ])

        added = armature_merge.merge_skeleton_data(base, mimic)

        self.assertEqual(added, 2)
        self.assertEqual(base.names, ["Root", "head", "face_root", "jaw_face"])
        self.assertEqual(base.parentIdx, [-1, 0, 1, 2])


if __name__ == "__main__":
    unittest.main()
