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
    sys.modules["witcher3_tools"] = _pkg

from witcher3_tools.importers import terrain_w2ter

for _name in [n for n in list(sys.modules) if n == "witcher3_tools" or n.startswith("witcher3_tools.")]:
    sys.modules.pop(_name, None)


class TestTerrainTintmapDetection(unittest.TestCase):
    def _tiles_from_sizes(self, folder: Path, sizes: list[int]) -> dict:
        tiles = {}
        for idx, size in enumerate(sizes, start=1):
            path = folder / f"tile_0_x_0_res512.w2ter.{idx}.buffer"
            with open(path, "wb") as handle:
                if size:
                    handle.seek(size - 1)
                    handle.write(b"\0")
            tiles[idx] = {(0, 0): str(path)}
        return tiles

    def test_skellige_source_raw_rgba_sequence_selects_buffer_7(self):
        # Source tiles append raw RGBA colormaps after height/control mips.
        sizes = [
            524288, 524288,
            131072, 131072,
            32768, 32768, 65536,
            8192, 8192, 16384,
            2048, 2048, 4096,
        ]
        with tempfile.TemporaryDirectory() as tmp:
            tiles = self._tiles_from_sizes(Path(tmp), sizes)

            self.assertEqual(terrain_w2ter.detect_colormap_start_mip(tiles, 512), 2)
            self.assertEqual(terrain_w2ter.select_tintmap_buffer_index(tiles, 512), 7)
            self.assertEqual(terrain_w2ter.get_raw_colormap_tile_res(tiles[7]), 128)

    def test_skellige_cooked_bc1_sequence_selects_buffer_7(self):
        # Sequence detection must beat BC1-size heuristics for cooked terrain.
        sizes = [
            524288, 524288,
            131072, 131072,
            32768, 32768, 8192,
            8192, 8192, 2048,
            2048, 2048, 512,
        ]
        with tempfile.TemporaryDirectory() as tmp:
            tiles = self._tiles_from_sizes(Path(tmp), sizes)

            self.assertEqual(terrain_w2ter.detect_colormap_start_mip(tiles, 512), 2)
            self.assertEqual(terrain_w2ter.select_tintmap_buffer_index(tiles, 512), 7)
            self.assertEqual(terrain_w2ter.get_tintmap_tile_blocks(tiles[7]), 32)

    def test_height_and_control_only_do_not_select_fake_bc1_tint(self):
        with tempfile.TemporaryDirectory() as tmp:
            tiles = self._tiles_from_sizes(Path(tmp), [524288, 524288])

            self.assertIsNone(terrain_w2ter.detect_colormap_start_mip(tiles, 512))
            self.assertIsNone(terrain_w2ter.select_tintmap_buffer_index(tiles, 512))


if __name__ == "__main__":
    unittest.main()
