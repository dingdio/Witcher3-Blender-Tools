"""Unit tests for the Witcher world-placement -> Unreal export."""

import math
import sys
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

from witcher3_tools.unreal_export import terrain_unreal
from witcher3_tools.unreal_export.manifest import build_manifest, depot_asset_rel
from witcher3_tools.unreal_export.placements_bundle import (
    _collision_asset_rel,
    _is_volume_mesh,
    _layer_label_and_folder,
    _light_entry_from_object,
    _placement_lod0_meshes,
    _w3_direction_to_unreal,
)

for _name in [n for n in list(sys.modules) if n == "witcher3_tools" or n.startswith("witcher3_tools.")]:
    sys.modules.pop(_name, None)


def _rot_z(deg):
    """Blender-world 4x4 rotation about +Z."""
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    m = np.eye(4)
    m[0, 0], m[0, 1] = c, -s
    m[1, 0], m[1, 1] = s, c
    return m


def _quat_xyzw_close(test, got, expected, places=5):
    got = np.asarray(got, dtype=float)
    expected = np.asarray(expected, dtype=float)
    # quaternions are double-cover: q and -q are the same rotation.
    if np.dot(got, expected) < 0:
        expected = -expected
    for g, e in zip(got, expected):
        test.assertAlmostEqual(g, e, places=places)


class TestPlacementTransform(unittest.TestCase):
    def test_identity_maps_to_identity(self):
        out = terrain_unreal.w3_matrix_to_unreal(np.eye(4))
        self.assertEqual([round(v, 6) for v in out["location"]], [0.0, 0.0, 0.0])
        _quat_xyzw_close(self, out["rotation"], [0.0, 0.0, 0.0, 1.0])
        for s in out["scale"]:
            self.assertAlmostEqual(s, 1.0, places=6)

    def test_translation_uses_canonical_frame(self):
        # (x, y, z) metres -> (100x, -100y, 100z) cm, matching w3_world_to_unreal.
        w = np.eye(4)
        w[0, 3], w[1, 3], w[2, 3] = 3.0, 5.0, 7.0
        out = terrain_unreal.w3_matrix_to_unreal(w)
        self.assertAlmostEqual(out["location"][0], 300.0, places=4)
        self.assertAlmostEqual(out["location"][1], -500.0, places=4)
        self.assertAlmostEqual(out["location"][2], 700.0, places=4)
        self.assertEqual(
            tuple(round(v, 4) for v in out["location"]),
            tuple(round(v, 4) for v in terrain_unreal.w3_world_to_unreal(3.0, 5.0, 7.0)),
        )

    def test_z_rotation_flips_handedness(self):
        # A +90 deg Blender Z rotation becomes -90 deg in the left-handed UE frame
        # (conjugation by diag(1,-1,1)); quaternion [x, y, z, w] = [0, 0, -sin45, cos45].
        out = terrain_unreal.w3_matrix_to_unreal(_rot_z(90.0))
        half = math.sqrt(0.5)
        _quat_xyzw_close(self, out["rotation"], [0.0, 0.0, -half, half])
        self.assertEqual([round(v, 6) for v in out["location"]], [0.0, 0.0, 0.0])

    def test_rotation_stays_proper_and_scale_preserved(self):
        s = np.diag([2.0, 3.0, 4.0, 1.0])
        w = _rot_z(37.0) @ s
        out = terrain_unreal.w3_matrix_to_unreal(w)
        # conjugation preserves det -> still a unit quaternion / proper rotation.
        self.assertAlmostEqual(float(np.linalg.norm(out["rotation"])), 1.0, places=6)
        # diagonal scale is invariant under conjugation by a diagonal sign flip.
        self.assertAlmostEqual(out["scale"][0], 2.0, places=4)
        self.assertAlmostEqual(out["scale"][1], 3.0, places=4)
        self.assertAlmostEqual(out["scale"][2], 4.0, places=4)

    def test_location_independent_of_rotation(self):
        w = _rot_z(123.0)
        w[0, 3], w[1, 3], w[2, 3] = 1.0, 2.0, 3.0
        out = terrain_unreal.w3_matrix_to_unreal(w)
        self.assertAlmostEqual(out["location"][0], 100.0, places=4)
        self.assertAlmostEqual(out["location"][1], -200.0, places=4)
        self.assertAlmostEqual(out["location"][2], 300.0, places=4)

    def test_mirror_scale_keeps_proper_rotation(self):
        # A mirrored placement (negative X scale) must yield a UNIT quaternion
        # (proper rotation) with the mirror pushed into a negative scale axis --
        # otherwise the quaternion extraction garbles the rotation.
        w = _rot_z(30.0) @ np.diag([-1.0, 1.0, 1.0, 1.0])
        out = terrain_unreal.w3_matrix_to_unreal(w)
        self.assertAlmostEqual(float(np.linalg.norm(out["rotation"])), 1.0, places=6)
        negative_axes = [s for s in out["scale"] if s < 0]
        self.assertEqual(len(negative_axes), 1)
        self.assertEqual([round(abs(s), 4) for s in out["scale"]], [1.0, 1.0, 1.0])


class _FakeMeshSettings:
    def __init__(self, repo_path):
        self.item_repo_path = repo_path


class _FakeData:
    def __init__(self, name):
        self.name = name


class _FakeObject:
    def __init__(self, name, data_name="", repo_path="", obj_type="MESH",
                 custom_props=None, children=None):
        self.name = name
        self.data = _FakeData(data_name)
        self.witcherui_MeshSettings = _FakeMeshSettings(repo_path)
        self.type = obj_type
        self._custom_props = dict(custom_props or {})
        self.children_recursive = list(children or [])
        self.parent = None
        for child in self.children_recursive:
            child.parent = self

    def get(self, key, default=None):
        return self._custom_props.get(key, default)

    def as_pointer(self):
        return id(self)


class _FakeLightData:
    def __init__(self, light_type):
        self.name = f"{light_type}_data"
        self.type = light_type
        self.energy = 12.0
        self.color = (0.5, 0.25, 1.0)
        self.shadow_soft_size = 3.0
        self.cutoff_distance = 0.0
        self.spot_size = math.radians(60.0)
        self.spot_blend = 0.25


class _FakeLightObject:
    def __init__(self, name, light_type, matrix=None):
        self.name = name
        self.type = "LIGHT"
        self.data = _FakeLightData(light_type)
        self.matrix_world = np.eye(4) if matrix is None else matrix


class TestVolumeFilter(unittest.TestCase):
    def test_box_volume_is_filtered(self):
        obj = _FakeObject("box_volume_lod0.001", data_name="box_volume_lod0")
        self.assertTrue(_is_volume_mesh(obj))

    def test_trigger_and_occlusion_filtered(self):
        self.assertTrue(_is_volume_mesh(_FakeObject("trigger_area")))
        self.assertTrue(_is_volume_mesh(_FakeObject("door", repo_path="env\\occlusion\\occ_box.w2mesh")))

    def test_real_building_is_kept(self):
        obj = _FakeObject("bridge_wood_01", data_name="bridge_wood_01_lod0",
                          repo_path="environment\\buildings\\bridge_wood_01.w2mesh")
        self.assertFalse(_is_volume_mesh(obj))

    def test_empty_wrapper_repo_path_is_filtered(self):
        obj = _FakeObject(
            "plain_empty",
            obj_type="EMPTY",
            custom_props={"repo_path": "environment\\triggers\\box_marker.w2mesh"},
        )
        self.assertTrue(_is_volume_mesh(obj))


class TestPlacementMeshChildren(unittest.TestCase):
    def test_keeps_all_lowest_lod_mesh_parts(self):
        repo = "environment\\buildings\\bridge_01.w2mesh"
        lod0_a = _FakeObject("bridge_01_lod0_part_a", data_name="part_a", repo_path=repo)
        lod1 = _FakeObject("bridge_01_lod1", data_name="lod1", repo_path=repo)
        lod0_b = _FakeObject("bridge_01_lod0_part_b", data_name="part_b", repo_path=repo)
        wrapper = _FakeObject(
            "bridge_01",
            obj_type="EMPTY",
            custom_props={"repo_path": repo},
            children=[lod0_a, lod1, lod0_b],
        )

        meshes = _placement_lod0_meshes(wrapper)

        self.assertEqual([mesh.name for mesh in meshes], ["bridge_01_lod0_part_a", "bridge_01_lod0_part_b"])


class TestPlacementLights(unittest.TestCase):
    def test_point_light_exports_common_light_properties(self):
        light = _FakeLightObject("torch_point", "POINT")

        entry = _light_entry_from_object(light)

        self.assertEqual(entry["type"], "point")
        self.assertEqual(entry["name"], "torch_point")
        self.assertEqual(entry["color"], [0.5, 0.25, 1.0])
        self.assertAlmostEqual(entry["intensity"], 1200.0)
        self.assertAlmostEqual(entry["attenuation_radius"], 300.0)

    def test_spot_light_exports_unreal_direction_and_cones(self):
        light = _FakeLightObject("torch_spot", "SPOT")

        entry = _light_entry_from_object(light)

        self.assertEqual(entry["type"], "spot")
        self.assertEqual([round(v, 6) for v in entry["direction"]], [0.0, -0.0, -1.0])
        self.assertAlmostEqual(entry["outer_cone_angle"], 60.0)
        self.assertAlmostEqual(entry["inner_cone_angle"], 45.0)

    def test_direction_conversion_flips_y_for_unreal(self):
        self.assertEqual(
            [round(v, 6) for v in _w3_direction_to_unreal((0.0, 1.0, 0.0))],
            [0.0, -1.0, 0.0],
        )


class TestLayerLabelFolder(unittest.TestCase):
    def test_depot_layer_id_splits_into_label_and_nested_folder(self):
        label, folder = _layer_label_and_folder(
            "levels\\prolog_village\\surroundings\\architecture.w2l"
        )
        self.assertEqual(label, "architecture.w2l")
        self.assertEqual(folder, "levels/prolog_village/surroundings")

    def test_top_level_layer_has_no_folder(self):
        label, folder = _layer_label_and_folder("architecture.w2l")
        self.assertEqual(label, "architecture.w2l")
        self.assertEqual(folder, "")


class TestPlacementManifest(unittest.TestCase):
    def test_collision_asset_rel_uses_visual_asset_sibling(self):
        self.assertEqual(
            _collision_asset_rel("environment/architecture/guard_tower_small"),
            "environment/architecture/guard_tower_small_collision",
        )

    def test_build_manifest_attaches_placements(self):
        placements = {
            "layers": [
                {
                    "layer_id": "levels\\novigrad\\novigrad.w2l",
                    "label": "novigrad.w2l",
                    "folder": "levels/novigrad",
                    "actors": [
                        {
                            "name": "house_01",
                            "asset_path": "environment/buildings/house_01",
                            "transform": {"location": [0, 0, 0], "rotation": [0, 0, 0, 1], "scale": [1, 1, 1]},
                        }
                    ],
                    "instancers": [],
                    "lights": [
                        {
                            "name": "torch_01",
                            "type": "point",
                            "transform": {"location": [1, 2, 3], "rotation": [0, 0, 0, 1], "scale": [1, 1, 1]},
                            "color": [1, 0.8, 0.6],
                            "intensity": 1000,
                        }
                    ],
                }
            ]
        }
        manifest = build_manifest(
            asset_name="WitcherPlacements",
            bundle_root="/tmp/bundle",
            placements=placements,
        )
        self.assertIn("placements", manifest)
        self.assertEqual(len(manifest["placements"]["layers"]), 1)
        self.assertEqual(manifest["placements"]["layers"][0]["actors"][0]["name"], "house_01")
        self.assertEqual(manifest["placements"]["layers"][0]["lights"][0]["name"], "torch_01")

    def test_build_manifest_omits_placements_when_absent(self):
        manifest = build_manifest(asset_name="x", bundle_root="/tmp/bundle")
        self.assertNotIn("placements", manifest)

    def test_same_depot_dedupes_to_one_asset_path(self):
        a = depot_asset_rel("environment\\buildings\\house_01.w2mesh")
        b = depot_asset_rel("environment/buildings/house_01.w2mesh")
        self.assertEqual(a, b)
        self.assertNotEqual(a, depot_asset_rel("environment\\buildings\\house_02.w2mesh"))


if __name__ == "__main__":
    unittest.main()
