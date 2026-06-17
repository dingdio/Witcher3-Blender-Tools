"""Terrain hole -> Unreal landscape visibility mask export."""

import sys
import tempfile
import types
import unittest
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

if "witcher3_tools" not in sys.modules:
    _pkg = types.ModuleType("witcher3_tools")
    _pkg.__path__ = [str(REPO_ROOT / "witcher3_tools")]
    _pkg.__package__ = "witcher3_tools"
    sys.modules["witcher3_tools"] = _pkg

from witcher3_tools.unreal_export import terrain_unreal, world_bundle

for _name in [n for n in list(sys.modules) if n == "witcher3_tools" or n.startswith("witcher3_tools.")]:
    sys.modules.pop(_name, None)


class TestVisibilityHelpers(unittest.TestCase):
    def test_holemap_to_r8_encodes_hole_255_visible_0(self):
        mask = np.array([[0, 1], [1, 0]], dtype=np.uint8)
        raw = terrain_unreal.holemap_to_r8_bytes(mask)
        self.assertEqual(list(raw), [0, 255, 255, 0])

    def test_holemap_honours_any_truthy_value(self):
        mask = np.array([[0, 7]], dtype=np.uint8)
        self.assertEqual(list(terrain_unreal.holemap_to_r8_bytes(mask)), [0, 255])

    def test_resample_nearest_is_identity_when_size_matches(self):
        mask = np.array([[0, 1], [1, 0]], dtype=np.uint8)
        out = terrain_unreal.resample_mask_nearest(mask, 2)
        self.assertTrue(np.array_equal(out, mask))

    def test_resample_nearest_preserves_corners(self):
        mask = np.zeros((2, 2), dtype=np.uint8)
        mask[0, 0] = 1
        out = terrain_unreal.resample_mask_nearest(mask, 4)
        self.assertEqual(out.shape, (4, 4))
        self.assertEqual(out[0, 0], 1)
        self.assertEqual(out[-1, -1], 0)
        self.assertEqual(set(np.unique(out).tolist()), {0, 1})


class TestExportTerrainHoles(unittest.TestCase):
    def _write_control(self, folder, hub, res, overlay, bkgrnd, blend):
        np.asarray(overlay, dtype=np.uint8).tofile(folder / f"combined.{hub}.overlay.data")
        np.asarray(bkgrnd, dtype=np.uint8).tofile(folder / f"combined.{hub}.bkgrnd.data")
        np.asarray(blend, dtype=np.uint8).tofile(folder / f"combined.{hub}.blendcontrol.data")

    def test_no_holes_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            hub = "skellige"
            res = 4
            n = res * res
            self._write_control(folder, hub, res,
                                overlay=np.ones(n), bkgrnd=np.ones(n), blend=np.zeros(n))
            warnings = []
            result = world_bundle._export_terrain_holes(
                str(folder), hub, res, res, str(folder), warnings)
            self.assertIsNone(result)

    def test_holes_emit_visibility_section_and_r8(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            hub = "skellige"
            res = 4
            n = res * res
            overlay = np.ones(n, dtype=np.uint8)
            bkgrnd = np.ones(n, dtype=np.uint8)
            blend = np.zeros(n, dtype=np.uint8)
            overlay[0] = bkgrnd[0] = 0
            overlay[5] = bkgrnd[5] = 0
            self._write_control(folder, hub, res, overlay, bkgrnd, blend)

            warnings = []
            result = world_bundle._export_terrain_holes(
                str(folder), hub, res, res, str(folder), warnings)

            self.assertIsNotNone(result)
            self.assertEqual(result["hole_count"], 2)
            self.assertEqual(result["resolution"], res)
            self.assertEqual(result["hole_value"], 255)
            self.assertEqual(result["layer_name"], "__LandscapeVisibility__")

            r8 = (folder / "Terrain" / f"{hub}.visibility.r8").read_bytes()
            self.assertEqual(len(r8), n)
            arr = np.frombuffer(r8, dtype=np.uint8)
            self.assertEqual(arr[0], 255)
            self.assertEqual(arr[5], 255)
            self.assertEqual(int((arr == 255).sum()), 2)
            self.assertEqual(int((arr == 0).sum()), n - 2)

    def test_only_full_zero_control_is_a_hole(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            hub = "velen"
            res = 4
            n = res * res
            overlay = np.zeros(n, dtype=np.uint8)
            bkgrnd = np.ones(n, dtype=np.uint8)
            blend = np.zeros(n, dtype=np.uint8)
            self._write_control(folder, hub, res, overlay, bkgrnd, blend)
            warnings = []
            result = world_bundle._export_terrain_holes(
                str(folder), hub, res, res, str(folder), warnings)
            self.assertIsNone(result)

    def test_size_mismatch_warns_and_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            hub = "novigrad"
            self._write_control(folder, hub, 4,
                                overlay=np.zeros(9), bkgrnd=np.zeros(9), blend=np.zeros(9))
            warnings = []
            result = world_bundle._export_terrain_holes(
                str(folder), hub, 4, 4, str(folder), warnings)
            self.assertIsNone(result)
            self.assertTrue(any("control map size" in w for w in warnings))


if __name__ == "__main__":
    unittest.main()
