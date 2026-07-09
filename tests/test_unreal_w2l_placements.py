import math
import sys
import types
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


class _BlockDataObjectType:
    Mesh = 0
    RigidBody = 1
    Collision = 2
    PointLight = 3
    SpotLight = 4
    Invalid = 5
    Decal = 6


class _Enums:
    BlockDataObjectType = _BlockDataObjectType


_LOCAL_PARAMS = {"Diffuse": ("handle:ITexture", "tex\\wall_d.xbm"), "Roughness": ("Float", "0.5")}


def _fake_xml_data_from_CR2W(chunk, name):
    el = ET.Element("material")
    el.set("base", getattr(chunk, "_base", ""))
    el.set("local", "true")
    for param_name, (param_type, value) in _LOCAL_PARAMS.items():
        param = ET.SubElement(el, "param")
        param.set("name", param_name)
        param.set("type", param_type)
        param.set("value", value)
    return el


def _install_stubs():
    pkg = sys.modules.get("witcher3_tools")
    if pkg is None or not getattr(pkg, "__path__", None):
        pkg = types.ModuleType("witcher3_tools")
        pkg.__path__ = [str(REPO_ROOT / "witcher3_tools")]
        pkg.__package__ = "witcher3_tools"
        sys.modules["witcher3_tools"] = pkg

    crw = types.ModuleType("witcher3_tools.CR2W")
    crw.__path__ = [str(REPO_ROOT / "witcher3_tools" / "CR2W")]
    crw.__package__ = "witcher3_tools.CR2W"
    helpers = types.ModuleType("witcher3_tools.CR2W.CR2W_helpers")
    helpers.Enums = _Enums
    crw.CR2W_helpers = helpers
    sys.modules["witcher3_tools.CR2W"] = crw
    sys.modules["witcher3_tools.CR2W.CR2W_helpers"] = helpers

    material = types.ModuleType("witcher3_tools.materials.material")
    material.xml_data_from_CR2W = _fake_xml_data_from_CR2W
    sys.modules["witcher3_tools.materials.material"] = material


_install_stubs()

from witcher3_tools.unreal_export import terrain_unreal, w2l_placements
from witcher3_tools.unreal_export import mesh_materials


class _Vec:
    def __init__(self, x, y, z):
        self.x, self.y, self.z = x, y, z


class _Rot:
    def __init__(self, rows):
        (self.ax, self.ay, self.az), (self.bx, self.by, self.bz), (self.cx, self.cy, self.cz) = rows


class _Color:
    def __init__(self, red=255, green=255, blue=255):
        self.Red = red
        self.Green = green
        self.Blue = blue


class _LightData:
    def __init__(self, brightness=2.0, radius=255.0, color=None, inner=0.0, outer=1.0):
        self.brightness = brightness
        self.radius = radius
        self.color = color or _Color()
        self.innerAngle = inner
        self.outerAngle = outer


_IDENT_ROWS = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]


class _Block:
    def __init__(self, block_type, mesh_index=0, pos=(0.0, 0.0, 0.0), rows=None, packed_object=None, flags=None):
        self.packedObjectType = block_type
        self.packedObject = packed_object or types.SimpleNamespace(meshIndex=mesh_index)
        self.position = _Vec(*pos)
        self.rotationMatrix = _Rot(rows or _IDENT_ROWS)
        if flags is not None:
            self.flags = flags


class _Sector:
    def __init__(self, blocks, resources):
        self.BlockData = blocks
        self.Resources = [types.SimpleNamespace(pathHash=p) for p in resources]


class _Level:
    def __init__(self, sector, version=174):
        self.CSectorData = sector
        self.version = version


class _Handle:
    def __init__(self, depot=None, reference=None):
        self.DepotPath = depot
        self.Reference = reference


class _Materials:
    def __init__(self, handles):
        self.Handles = handles
        self.Count = len(handles)


class _MatVar:
    def __init__(self, depot):
        self.Handles = [_Handle(depot=depot)]


class _Chunk:
    def __init__(self, base):
        self._base = base

    def GetVariableByName(self, name):
        return _MatVar(self._base) if name == "baseMaterial" else None


class _MeshFile:
    def __init__(self, chunks):
        self.CHUNKS = types.SimpleNamespace(CHUNKS=chunks)


def _rot_z_rows(deg):
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    blender_R = np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])
    return blender_R.T.tolist()


class TestBlockTransform(unittest.TestCase):
    def test_identity_block(self):
        m = w2l_placements.block_world_matrix((1.0, 2.0, 3.0), _IDENT_ROWS)
        self.assertTrue(np.allclose(m[:3, :3], np.eye(3)))
        self.assertEqual(list(m[:3, 3]), [1.0, 2.0, 3.0])

    def test_transpose_recovers_blender_rotation(self):
        c, s = math.cos(math.radians(30)), math.sin(math.radians(30))
        blender_R = np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])
        m = w2l_placements.block_world_matrix((0, 0, 0), blender_R.T.tolist())
        self.assertTrue(np.allclose(m[:3, :3], blender_R))

    def test_through_unreal_frame_location(self):
        m = w2l_placements.block_world_matrix((1.0, 2.0, 3.0), _IDENT_ROWS)
        out = terrain_unreal.w3_matrix_to_unreal(m)
        self.assertEqual([round(v, 6) for v in out["location"]], [100.0, -200.0, 300.0])
        for sc in out["scale"]:
            self.assertAlmostEqual(sc, 1.0, places=6)

    def test_z_rotation_handedness_flip(self):
        m = w2l_placements.block_world_matrix((0, 0, 0), _rot_z_rows(90.0))
        out = terrain_unreal.w3_matrix_to_unreal(m)
        expected = [0.0, 0.0, math.sin(math.radians(-45.0)), math.cos(math.radians(-45.0))]
        got = np.asarray(out["rotation"], dtype=float)
        exp = np.asarray(expected, dtype=float)
        if np.dot(got, exp) < 0:
            exp = -exp
        for g, e in zip(got, exp):
            self.assertAlmostEqual(g, e, places=5)


class TestCollect(unittest.TestCase):
    def setUp(self):
        _install_stubs()

    def _level(self, blocks, resources, version=174):
        return _Level(_Sector(blocks, resources), version=version)

    def test_unique_meshes_become_named_actors(self):
        level = self._level(
            [_Block(_BlockDataObjectType.Mesh, 0), _Block(_BlockDataObjectType.Mesh, 1)],
            ["a\\wall.w2mesh", "a\\door.w2mesh"],
        )
        out = w2l_placements.collect_w2l_placements(level, [])
        self.assertEqual(len(out["assets"]), 2)
        self.assertEqual(len(out["actors"]), 2)
        self.assertEqual(out["instancers"], [])
        self.assertEqual({a["name"] for a in out["actors"]}, {"wall", "door"})

    def test_repeated_mesh_above_threshold_becomes_instancer(self):
        blocks = [_Block(_BlockDataObjectType.Mesh, 0) for _ in range(9)]
        level = self._level(blocks, ["a\\barrel.w2mesh"])
        out = w2l_placements.collect_w2l_placements(level, [], instancer_threshold=8)
        self.assertEqual(out["actors"], [])
        self.assertEqual(len(out["instancers"]), 1)
        self.assertEqual(len(out["instancers"][0]["matrices"]), 9)

    def test_repeated_mesh_below_threshold_indexed_actors(self):
        blocks = [_Block(_BlockDataObjectType.Mesh, 0) for _ in range(3)]
        level = self._level(blocks, ["a\\crate.w2mesh"])
        out = w2l_placements.collect_w2l_placements(level, [], instancer_threshold=8)
        self.assertEqual([a["name"] for a in out["actors"]], ["crate_001", "crate_002", "crate_003"])

    def test_missing_sector_flags_default_to_visible(self):
        level = self._level([_Block(_BlockDataObjectType.Mesh, 0)], ["a\\wall.w2mesh"])
        out = w2l_placements.collect_w2l_placements(level, [])
        self.assertFalse(out["actors"][0].get("engine_hidden", False))

    def test_engine_hidden_is_per_sector_placement(self):
        visible = _Block(_BlockDataObjectType.Mesh, 0, flags=1 << 2)
        hidden = _Block(_BlockDataObjectType.Mesh, 0, flags=0)
        level = self._level([visible, hidden], ["a\\wall.w2mesh"])

        out = w2l_placements.collect_w2l_placements(level, [], instancer_threshold=8)

        self.assertEqual(len(out["assets"]), 1)
        visible_actors = [actor for actor in out["actors"] if not actor.get("engine_hidden")]
        hidden_actors = [actor for actor in out["actors"] if actor.get("engine_hidden")]
        self.assertEqual([actor["name"] for actor in visible_actors], ["wall"])
        self.assertEqual([actor["name"] for actor in hidden_actors], ["wall_EngineHidden"])

    def test_engine_hidden_instancers_split_from_visible_instancers(self):
        blocks = (
            [_Block(_BlockDataObjectType.Mesh, 0, flags=1 << 2) for _ in range(8)]
            + [_Block(_BlockDataObjectType.Mesh, 0, flags=0) for _ in range(8)]
        )
        level = self._level(blocks, ["a\\barrel.w2mesh"])

        out = w2l_placements.collect_w2l_placements(level, [], instancer_threshold=8)

        self.assertEqual(out["actors"], [])
        self.assertEqual(len(out["instancers"]), 2)
        by_name = {inst["name"]: inst for inst in out["instancers"]}
        self.assertEqual(len(by_name["barrel"]["matrices"]), 8)
        self.assertFalse(by_name["barrel"].get("engine_hidden", False))
        self.assertEqual(len(by_name["barrel_EngineHidden"]["matrices"]), 8)
        self.assertTrue(by_name["barrel_EngineHidden"].get("engine_hidden", False))

    def test_volume_and_proxy_filtered(self):
        level = self._level(
            [
                _Block(_BlockDataObjectType.Mesh, 0),
                _Block(_BlockDataObjectType.Mesh, 1),
                _Block(_BlockDataObjectType.Mesh, 2),
            ],
            ["a\\wall.w2mesh", "a\\trigger_volume.w2mesh", "a\\house_proxy.w2mesh"],
        )
        warnings = []
        out = w2l_placements.collect_w2l_placements(level, warnings)
        self.assertEqual(len(out["actors"]), 1)
        self.assertEqual(out["skipped"].get("volume"), 1)
        self.assertEqual(out["skipped"].get("proxy"), 1)

    def test_rigid_body_included_lights_counted(self):
        level = self._level(
            [
                _Block(_BlockDataObjectType.Mesh, 0),
                _Block(_BlockDataObjectType.RigidBody, 1),
                _Block(_BlockDataObjectType.PointLight, 2),
                _Block(_BlockDataObjectType.Collision, 3),
            ],
            ["a\\wall.w2mesh", "a\\barrel.w2mesh", "a\\light", "a\\wall.w2mesh"],
        )
        out = w2l_placements.collect_w2l_placements(level, [])
        self.assertEqual(len(out["actors"]), 2)
        self.assertEqual(len(out["lights"]), 1)
        self.assertIsNone(out["skipped"].get("point_light"))
        self.assertEqual(out["skipped"].get("collision"), 1)

    def test_sector_point_light_exports_manifest_light_data(self):
        level = self._level(
            [_Block(
                _BlockDataObjectType.PointLight,
                pos=(1.0, 2.0, 3.0),
                packed_object=_LightData(brightness=2.5, radius=510.0, color=_Color(128, 64, 255)),
            )],
            [],
        )

        out = w2l_placements.collect_w2l_placements(level, [])

        self.assertEqual(out["assets"], {})
        light = out["lights"][0]
        self.assertEqual(light["type"], "point")
        self.assertEqual(light["color"], [128 / 255, 64 / 255, 1.0])
        self.assertAlmostEqual(light["intensity"], 2500.0)
        self.assertAlmostEqual(light["attenuation_radius"], 200.0)
        self.assertEqual(list(light["matrix"][:3, 3]), [1.0, 2.0, 3.0])

    def test_sector_spot_light_exports_direction_and_cones(self):
        level = self._level(
            [_Block(
                _BlockDataObjectType.SpotLight,
                rows=_rot_z_rows(90.0),
                packed_object=_LightData(brightness=2.0, radius=255.0, inner=0.25, outer=0.5),
            )],
            [],
        )

        out = w2l_placements.collect_w2l_placements(level, [])

        light = out["lights"][0]
        self.assertEqual(light["type"], "spot")
        self.assertAlmostEqual(light["intensity"], 600.0)
        self.assertAlmostEqual(light["inner_cone_angle"], 0.25 * 57.29577951308232)
        self.assertAlmostEqual(light["outer_cone_angle"], 0.5 * 57.29577951308232)
        self.assertEqual([round(v, 6) for v in light["direction"]], [0.0, -0.0, -1.0])

    def test_collision_blocks_can_be_included_as_collision_only_placements(self):
        level = self._level(
            [_Block(_BlockDataObjectType.Collision, 0)],
            ["a\\wall.w2mesh"],
        )
        out = w2l_placements.collect_w2l_placements(
            level,
            [],
            include_collision_blocks=True,
        )
        self.assertEqual(list(out["assets"]), ["a/wall_collision"])
        self.assertEqual(out["assets"]["a/wall_collision"]["kind"], "collision")
        self.assertEqual(len(out["actors"]), 1)
        self.assertTrue(out["actors"][0]["collision_only"])
        self.assertIsNone(out["skipped"].get("collision"))

    def test_degenerate_transform_skipped(self):
        level = self._level(
            [_Block(_BlockDataObjectType.Mesh, 0, rows=[[0.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])],
            ["a\\collapsed.w2mesh"],
        )
        out = w2l_placements.collect_w2l_placements(level, [])
        self.assertEqual(out["actors"], [])
        self.assertEqual(out["assets"], {})
        self.assertEqual(out["skipped"].get("degenerate_transform"), 1)

    def test_no_sector_data(self):
        out = w2l_placements.collect_w2l_placements(_Level(None), [])
        self.assertEqual(out["assets"], {})
        self.assertEqual(out["actors"], [])


class TestMaterialSlots(unittest.TestCase):
    def setUp(self):
        _install_stubs()

    def test_external_handle(self):
        materials = _Materials([_Handle(depot="materials\\wall.w2mi")])
        warnings = []
        slots = mesh_materials.material_slots_from_mesh(["wall"], materials, _MeshFile([]), 174, warnings)
        self.assertEqual(len(slots), 1)
        props = slots[0]["witcher_props"]
        self.assertEqual(props["base_custom"], "materials\\wall.w2mi")
        self.assertFalse(props["local"])
        self.assertEqual(props["input_props"], [])
        self.assertEqual(props["material_version"], "")

    def test_embedded_handle_reads_base_and_params(self):
        chunk = _Chunk("engine\\materials\\graphs\\pbr_std.w2mg")
        materials = _Materials([_Handle(reference=0)])
        warnings = []
        slots = mesh_materials.material_slots_from_mesh(["local"], materials, _MeshFile([chunk]), 115, warnings)
        props = slots[0]["witcher_props"]
        self.assertEqual(props["base_custom"], "engine\\materials\\graphs\\pbr_std.w2mg")
        self.assertTrue(props["local"])
        self.assertEqual(props["material_version"], "witcher2")
        names = {p["name"] for p in props["input_props"]}
        self.assertEqual(names, {"Diffuse", "Roughness"})

    def test_empty_handle_warns(self):
        materials = _Materials([_Handle(depot="")])
        warnings = []
        slots = mesh_materials.material_slots_from_mesh(["x"], materials, _MeshFile([]), 174, warnings)
        self.assertEqual(slots[0]["witcher_props"]["base_custom"], "")
        self.assertTrue(any("fallback master" in w for w in warnings))


if __name__ == "__main__":
    unittest.main()
