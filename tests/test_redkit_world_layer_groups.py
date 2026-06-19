import sys
import tempfile
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

cr2w_pkg = sys.modules.get("witcher3_tools.CR2W")
if cr2w_pkg is None or not getattr(cr2w_pkg, "__path__", None):
    cr2w_pkg = types.ModuleType("witcher3_tools.CR2W")
    cr2w_pkg.__path__ = [str(REPO_ROOT / "witcher3_tools" / "CR2W")]
    cr2w_pkg.__package__ = "witcher3_tools.CR2W"
    sys.modules["witcher3_tools.CR2W"] = cr2w_pkg

from witcher3_tools.CR2W.CR2W_file import _build_world_groups_from_disk  # noqa: E402


def _child(group, name):
    for child in group.ChildrenGroups:
        if child.name == name:
            return child
    raise AssertionError(f"missing child group: {name}")


class TestRedkitWorldLayerGroups(unittest.TestCase):
    def test_disk_world_marks_lg_csv_paths_not_visible_on_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            world_path = root / "prolog_village.w2w"
            world_path.write_bytes(b"")
            (root / "village" / "barricade").mkdir(parents=True)
            (root / "village" / "barricade" / "meshes.w2l").write_bytes(b"")
            (root / "village" / "tavern").mkdir(parents=True)
            (root / "village" / "tavern" / "inn.w2l").write_bytes(b"")
            (root / "prolog_village_lg.csv").write_text(
                "LayerGroupPath\nworld\\village\\barricade\n",
                encoding="utf-8",
            )

            groups = _build_world_groups_from_disk("prolog_village", str(world_path))

            village = _child(groups, "village")
            barricade = _child(village, "barricade")
            tavern = _child(village, "tavern")
            self.assertTrue(groups.isVisibleOnStart)
            self.assertTrue(village.isVisibleOnStart)
            self.assertFalse(barricade.isVisibleOnStart)
            self.assertTrue(tavern.isVisibleOnStart)


if __name__ == "__main__":
    unittest.main()
