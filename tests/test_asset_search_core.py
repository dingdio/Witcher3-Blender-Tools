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

from witcher3_tools import asset_search_core as core  # noqa: E402


def _entries(paths):
    return [(core.normalize(path), path) for path in paths]


def _search(paths, query, limit=1000, path_filter=""):
    ranked, total = core.search(_entries(paths), query, limit, path_filter)
    return [path for _rank, path in ranked], total


class AssetSearchCoreTests(unittest.TestCase):
    def test_underscored_name_matches_as_one_phrase(self):
        paths = [r"characters\models\crowd\c_01_wa__body.w2mesh", r"characters\c_02\wa\body_01.w2mesh"]
        self.assertEqual(_search(paths, "c_01_wa__body"), ([paths[0]], 1))

    def test_spaces_still_split_into_tokens(self):
        paths = [r"characters\geralt\body_01.w2mesh", r"characters\ciri\body_01.w2mesh"]
        self.assertEqual(_search(paths, "body geralt"), ([paths[0]], 1))

    def test_forward_slashes_and_leading_separator_match(self):
        paths = [r"characters\models\geralt\body.w2mesh"]
        self.assertEqual(_search(paths, "characters/models/geralt/body.w2mesh")[0], paths)
        self.assertEqual(_search(paths, "/characters/models")[0], paths)

    def test_exact_name_survives_the_limit_and_leads(self):
        paths = [rf"levels\novigrad\occlusion_tiles\novigrad.w2w_0x{i}.w3occlusion" for i in range(50)]
        paths.append(r"levels\novigrad\novigrad.w2w")
        results, total = _search(paths, "novigrad.w2w", limit=5)
        self.assertEqual(results[0], r"levels\novigrad\novigrad.w2w")
        self.assertEqual((len(results), total), (5, 51))

    def test_rank_order(self):
        paths = [
            r"levels\geralt\terrain.w2ter",
            r"items\c_01_mg__geralt.xbm",
            r"anims\geralt_walk.w2anims",
            r"entities\geralt.w2ent",
        ]
        ranked, _total = core.search(_entries(paths), "geralt", 10)
        self.assertEqual(ranked, [(1, paths[3]), (2, paths[2]), (3, paths[1]), (4, paths[0])])
        self.assertEqual(core.search(_entries(paths), "geralt.w2ent", 10), ([(0, paths[3])], 1))
        self.assertEqual(core.search(_entries(paths), r"entities\geralt", 10), ([(1, paths[3])], 1))

    def test_path_filter_applies_before_limit(self):
        paths = [rf"characters\geralt\tex_{i:02d}.xbm" for i in range(20)]
        paths += [rf"characters\geralt\mesh_{i}.w2mesh" for i in range(3)]
        results, total = _search(paths, "geralt", limit=5, path_filter=" .W2MESH ")
        self.assertEqual(total, 3)
        self.assertEqual(sorted(results), sorted(paths[-3:]))

    def test_blank_query_finds_nothing(self):
        self.assertEqual(_search([r"a\b.w2mesh"], "   "), ([], 0))
        self.assertEqual(_search([r"a\b.w2mesh"], "\\"), ([], 0))

    def test_group_by_rank_keeps_order_within_rank(self):
        ranked = [(0, "b"), (0, "a"), (2, "d"), (2, "c")]
        self.assertEqual(core.group_by_rank(ranked, ["d", "c", "b", "a"]), ["b", "a", "d", "c"])


if __name__ == "__main__":
    unittest.main()
