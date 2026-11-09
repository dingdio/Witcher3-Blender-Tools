import os
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

if "witcher3_tools" not in sys.modules:
    _pkg = types.ModuleType("witcher3_tools")
    _pkg.__path__ = [str(REPO_ROOT / "witcher3_tools")]
    _pkg.__package__ = "witcher3_tools"
    _pkg.get_addon_name = lambda: "witcher3_tools"
    sys.modules["witcher3_tools"] = _pkg

from witcher3_tools.controller import surface  # noqa: E402

UNCOOK_ROOTS = (
    os.environ.get("W3_UNCOOK", ""),
    r"C:\Users\david\AppData\Roaming\Blender Foundation\Blender\5.0\extensions\.user\vscode_development\witcher3_tools\witcher_uncook",
    r"E:\w3.modding\modkit\r4data",
)
TERRAIN_CSV = next((p for p in (os.path.join(r, surface.TERRAIN_TABLE) for r in UNCOOK_ROOTS if r) if os.path.isfile(p)), "")


class FakeObject(dict):
    def __init__(self, name, materials=(), **props):
        super().__init__(props)
        self.name = name
        self.material_slots = [SimpleNamespace(material=SimpleNamespace(name=m) if m else None) for m in materials]
        self.witcherui_MeshSettings = None


class FakeWorld:
    def __init__(self, hit):
        self.hit = hit

    def ground_hit(self, x, y, z):
        return self.hit


class SurfaceTests(unittest.TestCase):
    def test_names_prefer_exact_physical_materials(self):
        self.assertEqual(surface.from_name("Floor_stone_debris_02"), "stone_debris")
        self.assertEqual(surface.from_name("ground_grass_mossy"), "grass_short")
        self.assertEqual(surface.from_name("tavern_floor_boards"), "wood_solid")
        self.assertEqual(surface.from_name("planks_wet"), "wood_solid")
        self.assertEqual(surface.from_name("Plane"), "")

    def test_terrain_table_parses(self):
        table = surface.parse_terrain_table("TextureFileName;PhysicalMaterialName\r\n"
                                            "environment/textures_tileable/soil/grass_default.xbm;grass_short\r\n\r\n")
        self.assertEqual(table, {r"environment\textures_tileable\soil\grass_default.xbm": "grass_short"})

    @unittest.skipUnless(TERRAIN_CSV, "terrain_textures_physical_materials.csv not available")
    def test_game_terrain_table_names_known_materials(self):
        with open(TERRAIN_CSV, encoding="utf-8-sig") as f:
            table = surface.parse_terrain_table(f.read())
        self.assertGreater(len(table), 100)
        self.assertEqual(set(table.values()) - set(surface.PHYSICAL_MATERIALS), set())

    def test_probe_resolution_order(self):
        explicit = FakeObject("floor", ["grass_ground"], witcher_physical_material="stone_solid")
        named = FakeObject("rock_wall", [None, "grass_ground"])
        probe = surface.Probe(FakeWorld((explicit, 0, None)), default="dirt_hard")
        self.assertEqual(probe.at(0, 0, 0), "stone_solid")
        probe.world.hit = (named, 1, None)
        self.assertEqual(probe.at(0, 0, 0), "grass_short")
        probe.world.hit = (named, 0, None)
        self.assertEqual(probe.at(0, 0, 0), "stone_solid")
        probe.world.hit = (FakeObject("Plane"), 0, None)
        self.assertEqual(probe.at(0, 0, 0), "dirt_hard")
        probe.world.hit = None
        self.assertEqual(probe.at(0, 0, 0), "dirt_hard")
        self.assertEqual(surface.Probe(None).at(0, 0, 0), "default")
        probe.world.hit = (FakeObject("river", witcher_physical_material="water_deep_river"), 0, None)
        self.assertEqual(probe.at(0, 0, 0), "water_deep")


if __name__ == "__main__":
    unittest.main()
