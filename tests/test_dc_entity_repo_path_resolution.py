import sys
import types
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def _install_namespace_stub(qualified_name: str, package_path: Path) -> None:
    if qualified_name in sys.modules:
        return
    module = types.ModuleType(qualified_name)
    module.__path__ = [str(package_path)]
    module.__package__ = qualified_name
    sys.modules[qualified_name] = module


_install_namespace_stub("witcher3_tools", REPO_ROOT / "witcher3_tools")
_install_namespace_stub("witcher3_tools.CR2W", REPO_ROOT / "witcher3_tools" / "CR2W")

from witcher3_tools.CR2W import dc_entity  # noqa: E402


class _PathLikeIndex:
    def __init__(self, path, numeric_index):
        self.path = path
        self.Index = numeric_index

    def __str__(self):
        return self.path


class _MeshProp:
    theName = "mesh"
    Handles = []
    Value = None

    def __init__(self, index):
        self.Index = index

    def ToString(self):
        return "CStaticMeshComponent"


class _Chunk:
    Type = "CStaticMeshComponent"
    PROPS = []

    def __init__(self, prop, cr2w_file):
        self._prop = prop
        setattr(self, "_W_CLASS__CR2WFILE", cr2w_file)

    def GetVariableByName(self, name):
        return self._prop if name == "mesh" else None


class RepoPathResolutionTests(unittest.TestCase):
    def test_path_like_property_index_wins_over_import_table_number(self):
        roof_path = (
            r"environment\architecture\human\redania\nomans_land"
            r"\thatched_buildings\tawern_tower_part_roof.w2mesh"
        )
        wood_path = (
            r"environment\architecture\human\redania\nomans_land"
            r"\thatched_buildings\tawern_tower_part_wood_roof.w2mesh"
        )
        cr2w_file = types.SimpleNamespace(
            HEADER=types.SimpleNamespace(version=156),
            CR2WImport=[
                types.SimpleNamespace(path=roof_path),
                types.SimpleNamespace(path=wood_path),
            ],
        )
        chunk = _Chunk(_MeshProp(_PathLikeIndex(roof_path, numeric_index=1)), cr2w_file)

        self.assertEqual(dc_entity._resolve_mesh_path(chunk, None), roof_path)


if __name__ == "__main__":
    unittest.main()
