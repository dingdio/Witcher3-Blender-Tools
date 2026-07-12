import math
import sys
import types
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

pkg = sys.modules.get("witcher3_tools")
if pkg is None or not getattr(pkg, "__path__", None):
    pkg = types.ModuleType("witcher3_tools")
    pkg.__path__ = [str(REPO_ROOT / "witcher3_tools")]
    pkg.__package__ = "witcher3_tools"
    sys.modules["witcher3_tools"] = pkg

from witcher3_tools.foliage_core import (  # noqa: E402
    Bounds2D,
    decode_foliage_instance_transform,
    foliage_cells_for_bounds,
    point_in_bounds,
    terrain_tile_bounds,
)


class SFoliageInstanceData:
    def __init__(self, x, y, z, scale, qz, qw):
        self.X = x
        self.Y = y
        self.Z = z
        self.Yaw = scale
        self.Pitch = qz
        self.Roll = qw


class TestTerrainTileBounds(unittest.TestCase):
    def test_centered_tile_bounds_share_exact_half_open_edges(self):
        left = terrain_tile_bounds(0, 1, 4, 4, 256.0)
        right = terrain_tile_bounds(1, 1, 4, 4, 256.0)

        self.assertEqual(left, Bounds2D(-128.0, -64.0, -64.0, 0.0))
        self.assertEqual(right, Bounds2D(-64.0, -64.0, 0.0, 0.0))
        self.assertEqual(left.max_x, right.min_x)
        self.assertFalse(point_in_bounds(left.max_x, -32.0, left))
        self.assertTrue(point_in_bounds(left.max_x, -32.0, right))

    def test_invert_y_maps_source_top_row_to_positive_world_edge(self):
        normal = terrain_tile_bounds(2, 0, 4, 4, 256.0)
        inverted = terrain_tile_bounds(2, 0, 4, 4, 256.0, invert_y=True)

        self.assertEqual(normal, Bounds2D(0.0, -128.0, 64.0, -64.0))
        self.assertEqual(inverted, Bounds2D(0.0, 64.0, 64.0, 128.0))

    def test_out_of_range_tile_is_rejected(self):
        for x, y in ((-1, 0), (0, -1), (4, 0), (0, 4)):
            with self.subTest(tile=(x, y)):
                with self.assertRaises(ValueError):
                    terrain_tile_bounds(x, y, 4, 4, 256.0)


class TestFoliageCellMapping(unittest.TestCase):
    def test_exact_positive_cell_maximum_does_not_add_neighbor(self):
        cells = list(foliage_cells_for_bounds(Bounds2D(0.0, 0.0, 64.0, 64.0)))
        self.assertEqual(cells, [(0.0, 0.0)])

    def test_exact_negative_boundaries_keep_half_open_ownership(self):
        cells = list(foliage_cells_for_bounds(Bounds2D(-128.0, -64.0, -64.0, 0.0)))
        self.assertEqual(cells, [(-128.0, -64.0)])

    def test_small_range_crossing_negative_boundary_selects_four_cells(self):
        cells = list(foliage_cells_for_bounds(Bounds2D(-65.0, -65.0, -63.0, -63.0)))
        self.assertEqual(
            cells,
            [
                (-128.0, -128.0),
                (-128.0, -64.0),
                (-64.0, -128.0),
                (-64.0, -64.0),
            ],
        )

    def test_tile_aligned_to_cells_returns_only_owned_cells(self):
        tile = terrain_tile_bounds(0, 0, 2, 2, 256.0)
        self.assertEqual(
            list(foliage_cells_for_bounds(tile)),
            [
                (-128.0, -128.0),
                (-128.0, -64.0),
                (-64.0, -128.0),
                (-64.0, -64.0),
            ],
        )


class TestPackedFoliageTransform(unittest.TestCase):
    def test_cooked_packed_fields_decode_scale_and_yaw_quaternion(self):
        half_angle = math.radians(90.0) * 0.5
        inst = SFoliageInstanceData(
            10.0,
            -20.0,
            5.0,
            1.35,
            math.sin(half_angle),
            math.cos(half_angle),
        )

        decoded = decode_foliage_instance_transform(inst)

        self.assertTrue(decoded.packed)
        self.assertEqual(decoded.location, (10.0, -20.0, 5.0))
        self.assertEqual(decoded.scale, (1.35, 1.35, 1.35))
        self.assertAlmostEqual(decoded.rotation_xyz[0], 0.0)
        self.assertAlmostEqual(decoded.rotation_xyz[1], 0.0)
        self.assertAlmostEqual(decoded.rotation_xyz[2], math.pi / 2.0)

    def test_explicit_packed_dict_normalizes_quaternion_terms(self):
        decoded = decode_foliage_instance_transform(
            {
                "X": -1.0,
                "Y": 2.0,
                "Z": 3.0,
                "Scale_x": 2.0,
                "Quat_z": 2.0,
                "Quat_w": 2.0,
            }
        )

        self.assertTrue(decoded.packed)
        self.assertEqual(decoded.scale, (2.0, 2.0, 2.0))
        self.assertAlmostEqual(decoded.rotation_xyz[2], math.pi / 2.0)

    def test_engine_transform_remains_non_packed_and_preserves_axis_scale(self):
        decoded = decode_foliage_instance_transform(
            {
                "X": 1.0,
                "Y": 2.0,
                "Z": 3.0,
                "Yaw": 0.0,
                "Pitch": 0.0,
                "Roll": 0.0,
                "Scale_x": 2.0,
                "Scale_y": 3.0,
                "Scale_z": 4.0,
            }
        )

        self.assertFalse(decoded.packed)
        self.assertEqual(decoded.rotation_xyz, (0.0, 0.0, 0.0))
        self.assertEqual(decoded.scale, (2.0, 3.0, 4.0))

    def test_engine_transform_matches_blender_yxz_conversion(self):
        decoded = decode_foliage_instance_transform(
            {
                "Yaw": math.degrees(0.2),
                "Pitch": math.degrees(0.3),
                "Roll": math.degrees(0.4),
                "Scale_x": 1.0,
                "Scale_y": 1.0,
                "Scale_z": 1.0,
            }
        )

        expected = (0.2090859491, 0.2938396931, 0.4613784328)
        for actual, wanted in zip(decoded.rotation_xyz, expected):
            self.assertAlmostEqual(actual, wanted, places=7)

    def test_engine_transform_gimbal_lock_stays_matrix_equivalent(self):
        decoded = decode_foliage_instance_transform(
            {
                "Yaw": 90.0,
                "Pitch": math.degrees(0.4),
                "Roll": math.degrees(-0.8),
                "Scale_x": 1.0,
                "Scale_y": 1.0,
                "Scale_z": 1.0,
            }
        )

        self.assertAlmostEqual(decoded.rotation_xyz[0], math.pi / 2.0, places=7)
        self.assertAlmostEqual(decoded.rotation_xyz[1], 0.0, places=7)
        self.assertAlmostEqual(decoded.rotation_xyz[2], -0.4, places=7)


if __name__ == "__main__":
    unittest.main()
