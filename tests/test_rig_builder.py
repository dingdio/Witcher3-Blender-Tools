import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

if "witcher3_tools" not in sys.modules:
    _pkg = types.ModuleType("witcher3_tools")
    _pkg.__path__ = [str(REPO_ROOT / "witcher3_tools")]
    _pkg.__package__ = "witcher3_tools"
    _pkg.get_addon_name = lambda: "witcher3_tools"
    sys.modules["witcher3_tools"] = _pkg

from witcher3_tools.CR2W import cr2w_writer, dc_skeleton, rig_builder

VANILLA_RIG = "characters/base_entities/man_base/man_base.w2rig"
UNCOOK_ROOTS = (
    os.environ.get("W3_UNCOOK", ""),
    r"C:\Users\david\AppData\Roaming\Blender Foundation\Blender\5.0\extensions\.user\vscode_development\witcher3_tools\witcher_uncook",
    r"E:\w3.modding\modkit\r4data",
)


def _parse(path):
    cr2w = dc_skeleton.getCR2W(dc_skeleton.open_cr2w_read_stream(str(path)))
    return cr2w, dc_skeleton.create_Skeleton(cr2w)


def _spec_from(skel):
    return [{
        "name": n, "parent": p,
        "pos": (pos.x, pos.y, pos.z), "rot": (rot.x, rot.y, rot.z, rot.w), "scale": (s.x, s.y, s.z),
    } for n, p, pos, rot, s in zip(skel.names, skel.parentIdx, skel.positions, skel.rotations, skel.scales)]


def _vanilla_rig():
    for root in UNCOOK_ROOTS:
        if root and (Path(root) / VANILLA_RIG).is_file():
            return Path(root) / VANILLA_RIG
    return None


class RigBuilderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _roundtrip(self, bones, **kwargs):
        out = self.tmp / "out.w2rig"
        cr2w_writer.write_w2rig(rig_builder.build_w2rig(bones, **kwargs), str(out))
        return out, *_parse(out)

    def test_string_ansi_counts_nul(self):
        self.assertEqual(cr2w_writer._encode_string_ansi("Root"), b"\x05Root\x00")
        self.assertEqual(cr2w_writer._encode_string_ansi(""), b"\x00")

    def test_synthetic_roundtrip(self):
        bones = [
            {"name": "Root", "parent": -1, "pos": (0, 0, 0), "rot": (0, 0, 0, 1), "scale": (1, 1, 1)},
            {"name": "Trajectory", "parent": 0, "pos": (0.1, -0.2, 0.9), "rot": (0.5, 0.5, 0.5, 0.5), "scale": (1, 1, 1)},
            {"name": "head", "parent": 1, "pos": (0, 0, 1.5), "rot": (0, 0.70710677, 0, 0.70710677), "scale": (1, 1, 1)},
        ]
        out, cr2w, skel = self._roundtrip(bones, tracks=["hctFOV", "dofIntensity"])
        self.assertEqual(skel.names, ["Root", "Trajectory", "head"])
        self.assertEqual(skel.parentIdx, [-1, 0, 1])
        self.assertEqual(skel.tracks, ["hctFOV", "dofIntensity"])
        for b, pos, rot in zip(bones, skel.positions, skel.rotations):
            for axis, expected in zip("xyz", b["pos"]):
                self.assertAlmostEqual(getattr(pos, axis), expected, places=6)
            for axis, expected in zip("xyzw", b["rot"]):
                self.assertAlmostEqual(getattr(rot, axis), expected, places=6)
        self.assertEqual([p.w for p in skel.positions], [0.0, 1.0, 1.0])
        self.assertIn(b"\x0bTrajectory\x00", out.read_bytes())

    def test_rejects_bad_hierarchy(self):
        root = {"name": "Root", "parent": -1, "pos": (0, 0, 0), "rot": (0, 0, 0, 1), "scale": (1, 1, 1)}
        with self.assertRaises(ValueError):
            rig_builder.build_w2rig([root, dict(root, name="root")])
        with self.assertRaises(ValueError):
            rig_builder.build_w2rig([dict(root, parent=1), dict(root, name="child", parent=-1)])

    def test_vanilla_carry_over_remaps_indices(self):
        rig = _vanilla_rig()
        if rig is None:
            self.skipTest("vanilla man_base.w2rig not available")
        src_cr2w, src = _parse(rig)
        bones = _spec_from(src)
        # Insert a bone right after Root: every vanilla index >= 1 shifts by one.
        for b in bones:
            if b["parent"] >= 1:
                b["parent"] += 1
        bones.insert(1, {"name": "extra", "parent": 0, "pos": (0, 0, 1), "rot": (0, 0, 0, 1), "scale": (1, 1, 1)})

        out, cr2w, skel = self._roundtrip(bones, source_cr2w=src_cr2w)
        self.assertEqual(skel.names, [b["name"] for b in bones])
        self.assertEqual(skel.parentIdx, [b["parent"] for b in bones])
        self.assertAlmostEqual(skel.rotations[4].x, src.rotations[3].x, places=6)

        chunks = {c.name: c for c in cr2w.CHUNKS.CHUNKS}
        self.assertEqual(set(chunks), {"CSkeleton", "TCrDefinition", "TCrPropertySet", "CControlRigSettings", "CTeleportDetectorData"})
        for export in cr2w.CR2WExport[1:]:
            self.assertEqual((export.parentID, export.objectFlags), (1, rig_builder.OF_INLINED))
        self.assertEqual(chunks["TCrDefinition"].GetVariableByName("indexPelvis").Value, 10)
        self.assertEqual(chunks["CControlRigSettings"].GetVariableByName("fkBones_l1").value[:3], [0, 10, 11])
        self.assertEqual(len(chunks["CTeleportDetectorData"].GetVariableByName("teleportedBones").More), 7)
        self.assertEqual(chunks["CSkeleton"].GetVariableByName("lodBoneNum_1").Value, 40)


if __name__ == "__main__":
    unittest.main()
