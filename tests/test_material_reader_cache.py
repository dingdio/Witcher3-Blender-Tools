"""Tests for the session-level material root chunk cache."""

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
    _pkg.get_addon_name = lambda: "witcher3_tools"
    sys.modules["witcher3_tools"] = _pkg

from witcher3_tools.materials import reader

for _name in [n for n in list(sys.modules) if n == "witcher3_tools" or n.startswith("witcher3_tools.")]:
    sys.modules.pop(_name, None)


class _FakeChunk:
    def __init__(self, chunk_type="CMaterialInstance"):
        self.Type = chunk_type

    def GetVariableByName(self, name):
        return None


class TestMaterialRootChunkCache(unittest.TestCase):
    def setUp(self):
        reader._material_root_chunk_cache.clear()
        self._orig_repo_file = reader.repo_file
        self._orig_exists = reader.os.path.exists
        self._orig_load_material = reader.CR2W_reader.load_material
        self.load_calls = []

        reader.repo_file = lambda path, version=999: "resolved\\" + path
        reader.os.path.exists = lambda path: not path.endswith("missing.w2mi")

        def fake_load_material(full_path):
            self.load_calls.append(full_path)
            return [_FakeChunk()]

        reader.CR2W_reader.load_material = fake_load_material

    def tearDown(self):
        reader.repo_file = self._orig_repo_file
        reader.os.path.exists = self._orig_exists
        reader.CR2W_reader.load_material = self._orig_load_material
        reader._material_root_chunk_cache.clear()

    def test_repeat_load_parses_once(self):
        first = reader._load_material_root_chunk(r"engine\materials\graphs\pbr_skin.w2mi")
        second = reader._load_material_root_chunk(r"engine\materials\graphs\pbr_skin.w2mi")
        self.assertIs(first, second)
        self.assertEqual(len(self.load_calls), 1)

    def test_cache_key_normalizes_path(self):
        first = reader._load_material_root_chunk(r"engine\materials\graphs\pbr_skin.w2mi")
        second = reader._load_material_root_chunk("Engine/Materials/Graphs/PBR_Skin.w2mi")
        self.assertIs(first, second)
        self.assertEqual(len(self.load_calls), 1)

    def test_versions_cached_separately(self):
        w3_chunk = reader._load_material_root_chunk(r"shaders\cloth.w2mi", version=999)
        w2_chunk = reader._load_material_root_chunk(r"shaders\cloth.w2mi", version=115)
        self.assertIsNot(w3_chunk, w2_chunk)
        self.assertEqual(len(self.load_calls), 2)

    def test_missing_file_not_cached(self):
        self.assertIsNone(reader._load_material_root_chunk(r"shaders\missing.w2mi"))
        self.assertNotIn(
            (999, reader.normalize_depot_path(r"shaders\missing.w2mi")),
            reader._material_root_chunk_cache,
        )
        # If the file shows up later (e.g. extracted mid-session), it loads.
        reader.os.path.exists = lambda path: True
        self.assertIsNotNone(reader._load_material_root_chunk(r"shaders\missing.w2mi"))


if __name__ == "__main__":
    unittest.main()
