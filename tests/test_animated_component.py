import unittest
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

if "witcher3_tools" not in sys.modules:
    _pkg = types.ModuleType("witcher3_tools")
    _pkg.__path__ = [str(REPO_ROOT / "witcher3_tools")]
    _pkg.__package__ = "witcher3_tools"
    sys.modules["witcher3_tools"] = _pkg

from witcher3_tools.CR2W import animated_component


class AnimatedComponentBuilderTests(unittest.TestCase):
    def test_trajectory_entity_allows_zero_mesh_attachments(self):
        data = animated_component.build_entity_json([], entity_name="trajectories")

        imports = data["_imports"]
        self.assertIn({
            "_className": "CSkeleton",
            "_depotPath": animated_component.TRAJECTORY_RIG_PATH,
            "_flags": 0,
        }, imports)
        self.assertIn({
            "_className": "CBehaviorGraph",
            "_depotPath": animated_component.CUTSCENE_BEHAVIOR_PATH,
            "_flags": 0,
        }, imports)
        self.assertFalse(any(item["_className"] == "CMesh" for item in imports))

        chunks = data["_chunks"]
        entity_vars = chunks["CEntity #1"]["_vars"]
        component_refs = entity_vars["Components"]["_elements"]
        self.assertEqual(len(component_refs), 1)
        self.assertEqual(
            component_refs[0]["_vars"]["_reference"]["_value"],
            "CAnimatedComponent #2",
        )

        anim_vars = chunks["CAnimatedComponent #2"]["_vars"]
        self.assertEqual(anim_vars["name"]["_value"], animated_component.DEFAULT_COMPONENT_NAME)
        self.assertEqual(anim_vars["AttachmentsChild"]["_elements"], [])
        flat_chunks = chunks["CEntityTemplate #0"]["_vars"]["flatCompiledData"]["_chunks"]
        self.assertEqual(
            anim_vars["guid"]["_value"],
            flat_chunks["flatCompiledData::CAnimatedComponent #1"]["_vars"]["guid"]["_value"],
        )

    def test_trajectory_entity_writes_mesh_hard_attachment_and_slot(self):
        mesh_path = r"items\weapons\swords\nilfgaardian_sword_lv1.w2mesh"
        data = animated_component.build_entity_json([{
            "mesh": mesh_path,
            "slot": "Trajectory01",
            "bone_index": 1,
            "name": "nilfgaardian_sword_lv1",
        }], entity_name="trajectories")

        self.assertIn({
            "_className": "CMesh",
            "_depotPath": mesh_path,
            "_flags": 0,
        }, data["_imports"])

        chunks = data["_chunks"]
        self.assertEqual(chunks["CHardAttachment #3"]["_type"], "CHardAttachment")
        self.assertEqual(chunks["CSkeletonBoneSlot #4"]["_type"], "CSkeletonBoneSlot")
        self.assertEqual(chunks["CMeshComponent #5"]["_type"], "CMeshComponent")

        hard_vars = chunks["CHardAttachment #3"]["_vars"]
        self.assertEqual(hard_vars["parentSlotName"]["_value"], "Trajectory01")
        self.assertEqual(
            hard_vars["child"]["_vars"]["_reference"]["_value"],
            "CMeshComponent #5",
        )

        mesh_vars = chunks["CMeshComponent #5"]["_vars"]
        self.assertEqual(mesh_vars["name"]["_value"], "nilfgaardian_sword_lv1")
        self.assertEqual(mesh_vars["mesh"]["_vars"]["_depotPath"]["_value"], mesh_path)

        slot_vars = chunks["CSkeletonBoneSlot #4"]["_vars"]
        self.assertEqual(slot_vars["boneIndex"]["_value"], 1)

        flat_chunks = chunks["CEntityTemplate #0"]["_vars"]["flatCompiledData"]["_chunks"]
        self.assertEqual(
            mesh_vars["guid"]["_value"],
            flat_chunks["flatCompiledData::CMeshComponent #4"]["_vars"]["guid"]["_value"],
        )

    def test_attachment_flags_and_transforms_round_trip_into_json(self):
        data = animated_component.build_entity_json([{
            "mesh": r"items\props\flagged.w2mesh",
            "slot": "Trajectory03",
            "bone_index": 3,
            "attachment_flags": 9,
            "relative_transform": {"X": 1.5, "Yaw": 30.0},
            "component_transform": {"Scale_x": 2.0, "Scale_y": 2.0, "Scale_z": 2.0},
        }])

        chunks = data["_chunks"]
        hard_vars = chunks["CHardAttachment #3"]["_vars"]
        self.assertEqual(
            hard_vars["attachmentFlags"]["_value"],
            "HAF_FreePositionAxisX|HAF_FreeRotation",
        )
        self.assertEqual(hard_vars["relativeTransform"]["_vars"]["X"]["_value"], 1.5)
        self.assertEqual(hard_vars["relativeTransform"]["_vars"]["Yaw"]["_value"], 30.0)
        mesh_transform = chunks["CMeshComponent #5"]["_vars"]["transform"]["_vars"]
        self.assertEqual(mesh_transform["Scale_x"]["_value"], 2.0)


if __name__ == "__main__":
    unittest.main()
