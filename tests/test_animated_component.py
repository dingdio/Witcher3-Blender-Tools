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

    def _parse(self, build):
        import os
        import tempfile
        from witcher3_tools.CR2W.CR2W_file import read_CR2W

        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "entity.w2ent")
            build(out)
            return read_CR2W(out)

    def test_static_entity_has_no_animated_component(self):
        parsed = self._parse(lambda out: animated_component.generate_entity(
            [], out, skeleton_path=None, behavior_path=None, entity_name="crate",
            static_meshes=[
                {"mesh": r"environment\a.w2mesh", "name": "a"},
                {"mesh": r"environment\b.w2mesh", "transform": {"X": 1.5, "Yaw": 90.0, "Scale_x": 2.0}},
            ]))
        exports = parsed.CR2WExport
        self.assertEqual([e.name for e in exports],
                         ["CEntityTemplate", "CEntity", "CStaticMeshComponent", "CStaticMeshComponent"])
        self.assertEqual([e.parentID for e in exports], [0, 1, 2, 2])
        first, second = parsed.CHUNKS.CHUNKS[2], parsed.CHUNKS.CHUNKS[3]
        self.assertEqual([p.theName for p in first.PROPS], ["guid", "name", "mesh"])
        self.assertEqual([p.theName for p in second.PROPS], ["transform", "guid", "name", "mesh"])
        transform = next(p for p in second.PROPS if p.theName == "transform").EngineTransform
        self.assertAlmostEqual(transform.X, 1.5)
        self.assertAlmostEqual(transform.Yaw, 90.0)
        self.assertAlmostEqual(transform.Scale_x, 2.0)
        self.assertAlmostEqual(transform.Scale_y, 1.0)

    def test_mixed_entity_lists_all_components(self):
        parsed = self._parse(lambda out: animated_component.generate_entity(
            [{"mesh": r"items\a.w2mesh", "slot": "Trajectory01", "bone_index": 1}], out,
            entity_name="mixed", static_meshes=[{"mesh": r"environment\b.w2mesh"}]))
        exports = parsed.CR2WExport
        self.assertEqual([e.name for e in exports],
                         ["CEntityTemplate", "CEntity", "CAnimatedComponent", "CHardAttachment",
                          "CSkeletonBoneSlot", "CMeshComponent", "CStaticMeshComponent"])
        self.assertEqual([e.parentID for e in exports], [0, 1, 2, 3, 3, 2, 2])

    def test_native_attachment_keeps_transforms_and_flags(self):
        from witcher3_tools.rigging.attachment import attachment_flag_names

        parsed = self._parse(lambda out: animated_component.generate_entity([{
            "mesh": r"items\a.w2mesh", "slot": "Trajectory01", "bone_index": 1,
            "relative_transform": {"X": 1.5, "Yaw": 30.0},
            "attachment_flags": 9,
            "component_transform": {"Scale_x": 2.0},
        }], out, entity_name="offset"))
        hard, mesh = parsed.CHUNKS.CHUNKS[3], parsed.CHUNKS.CHUNKS[5]
        self.assertEqual([p.theName for p in hard.PROPS][-2:], ["relativeTransform", "attachmentFlags"])
        relative = next(p for p in hard.PROPS if p.theName == "relativeTransform").EngineTransform
        self.assertAlmostEqual(relative.X, 1.5)
        self.assertAlmostEqual(relative.Yaw, 30.0)
        flags = next(p for p in hard.PROPS if p.theName == "attachmentFlags")
        self.assertEqual(attachment_flag_names(flags), ["HAF_FreePositionAxisX", "HAF_FreeRotation"])
        transform = next(p for p in mesh.PROPS if p.theName == "transform").EngineTransform
        self.assertAlmostEqual(transform.Scale_x, 2.0)

    def test_hard_attachments_require_a_skeleton(self):
        with self.assertRaises(ValueError):
            animated_component.build_entity_cr2w(
                [{"mesh": r"items\a.w2mesh", "slot": "Trajectory01", "bone_index": 1}],
                skeleton_path=None)

    def test_static_mesh_requires_a_path(self):
        with self.assertRaises(ValueError):
            animated_component.build_entity_cr2w([], skeleton_path=None, static_meshes=[{"mesh": ""}])

    def test_depot_paths_must_be_game_relative(self):
        # Absolute, UNC and traversing paths must never reach the CR2W import table.
        att = [{"mesh": r"items\a.w2mesh", "slot": "Trajectory01", "bone_index": 1}]
        bad = [r"E:\abs\x", "\\\\server\\share\\x", r"..\x", r"a\..\x", r"a\.\x"]
        for path in bad:
            with self.assertRaises(ValueError, msg=path):
                animated_component.build_entity_cr2w(att, skeleton_path=path + ".w2rig")
            with self.assertRaises(ValueError, msg=path):
                animated_component.build_entity_cr2w(att, behavior_path=path + ".w2beh")
            with self.assertRaises(ValueError, msg=path):
                animated_component.build_entity_cr2w(
                    [{"mesh": path + ".w2mesh", "slot": "Trajectory01", "bone_index": 1}])
            with self.assertRaises(ValueError, msg=path):
                animated_component.build_entity_cr2w(
                    [], skeleton_path=None, static_meshes=[{"mesh": path + ".w2mesh"}])
        # Rooted and forward-slash paths are normalised, not rejected.
        self.assertEqual(animated_component._depot_path("/items/a.w2mesh", "t"), r"items\a.w2mesh")
        self.assertEqual(animated_component._depot_path("\\items\\a.w2mesh", "t"), r"items\a.w2mesh")

    def test_import_table_holds_normalised_paths(self):
        att = [{"mesh": "items/a.w2mesh", "slot": "Trajectory01", "bone_index": 1}]
        data = animated_component.build_entity_json(att, skeleton_path="/characters/a.w2rig",
                                                    behavior_path="foo/bar.w2beh")
        self.assertEqual({(i["_className"], i["_depotPath"]) for i in data["_imports"]}, {
            ("CSkeleton", r"characters\a.w2rig"), ("CBehaviorGraph", r"foo\bar.w2beh"), ("CMesh", r"items\a.w2mesh")})

        parsed = self._parse(lambda out: animated_component.generate_entity(
            att, out, skeleton_path="/characters/a.w2rig", behavior_path="foo/bar.w2beh"))
        depot_paths = {str(getattr(i, "path", None) or getattr(i, "depotPath", None) or "") for i in parsed.CR2WImport}
        self.assertEqual(depot_paths, {r"characters\a.w2rig", r"foo\bar.w2beh", r"items\a.w2mesh"})

    def test_handle_extensions_are_enforced(self):
        att = [{"mesh": r"items\a.w2mesh", "slot": "Trajectory01", "bone_index": 1}]
        with self.assertRaises(ValueError):
            animated_component.build_entity_cr2w(att, skeleton_path=r"items\a.w2mesh")
        with self.assertRaises(ValueError):
            animated_component.build_entity_cr2w(att, behavior_path=r"gameplay\x.txt")
        with self.assertRaises(ValueError):
            animated_component.build_entity_cr2w(
                [{"mesh": r"characters\a.w2rig", "slot": "Trajectory01", "bone_index": 1}])
        with self.assertRaises(ValueError):
            animated_component.build_entity_cr2w(
                [], skeleton_path=None, static_meshes=[{"mesh": r"characters\a.w2rig"}])


if __name__ == "__main__":
    unittest.main()
