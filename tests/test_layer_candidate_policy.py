import ast
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
UI_MAP_PATH = ROOT / "witcher3_tools" / "ui" / "ui_map.py"


def _load_candidate_helpers():
    source = UI_MAP_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(UI_MAP_PATH))
    wanted_functions = {
        "collection_w2layer_path",
        "_manifest_countable_item",
        "_manifest_item_matches_kind_filter",
        "_manifest_item_position",
        "_count_nearby_manifest_items_for_entry",
        "_layer_candidate_vertical_radius",
        "_normalize_level_path",
        "_default_hidden_level_paths",
    }
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name)
            and target.id in {
                "_LAYER_QUERY_FILTER_KINDS",
                "_LAYER_CANDIDATE_VERTICAL_RADIUS",
                "W2LAYER_PATH_PROP",
                "LEGACY_LEVEL_PATH_PROP",
            }
            for target in node.targets
        ):
            nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in wanted_functions:
            nodes.append(node)
    namespace = {}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(UI_MAP_PATH), "exec"), namespace)
    return namespace


HELPERS = _load_candidate_helpers()


class LayerCandidatePolicyTests(unittest.TestCase):
    def test_candidate_layer_needs_a_renderable_anchor_in_the_vertical_band(self):
        entry = {
            "items": [
                {"kind": "mesh", "world_position": [1.0, 0.0, 19.0]},
                {"kind": "mesh", "world_position": [2.0, 0.0, 21.0]},
                {"kind": "point_light", "world_position": [3.0, 4.0, 0.0]},
                {"kind": "entity_empty", "world_position": [0.0, 0.0, 0.0]},
                {"kind": "entity_template", "world_position": [0.0, 0.0, 21.0]},
            ]
        }

        count, nearest_sq = HELPERS["_count_nearby_manifest_items_for_entry"](
            entry,
            (0.0, 0.0, 0.0),
            100.0,
            item_kind_filter=HELPERS["_LAYER_QUERY_FILTER_KINDS"],
            vertical_radius=20.0,
        )

        self.assertEqual(count, 2)
        self.assertEqual(nearest_sq, 1.0)

    def test_entity_templates_can_be_visual_layer_anchors(self):
        self.assertIn("entity_template", HELPERS["_LAYER_QUERY_FILTER_KINDS"])
        self.assertNotIn("entity_empty", HELPERS["_LAYER_QUERY_FILTER_KINDS"])

    def test_vertical_band_only_controls_layer_candidacy(self):
        entry = {"items": [{"kind": "mesh", "world_position": [2.0, 0.0, 21.0]}]}

        count, _nearest_sq = HELPERS["_count_nearby_manifest_items_for_entry"](
            entry,
            (0.0, 0.0, 0.0),
            100.0,
            item_kind_filter=HELPERS["_LAYER_QUERY_FILTER_KINDS"],
        )

        self.assertEqual(count, 1)

    def test_vertical_band_reaches_an_elevated_viewports_target(self):
        area = SimpleNamespace(
            spaces=SimpleNamespace(
                active=SimpleNamespace(
                    region_3d=SimpleNamespace(
                        view_location=SimpleNamespace(z=0.0),
                    )
                )
            )
        )
        original = HELPERS.get("_get_current_view3d_area")
        HELPERS["_get_current_view3d_area"] = lambda _context: area
        try:
            radius = HELPERS["_layer_candidate_vertical_radius"](
                None,
                (0.0, 0.0, 50.0),
            )
        finally:
            if original is None:
                HELPERS.pop("_get_current_view3d_area", None)
            else:
                HELPERS["_get_current_view3d_area"] = original

        self.assertEqual(radius, 51.0)
        count, _nearest_sq = HELPERS["_count_nearby_manifest_items_for_entry"](
            {"items": [{"kind": "mesh", "world_position": [0.0, 0.0, -0.5]}]},
            (0.0, 0.0, 50.0),
            100.0,
            item_kind_filter=HELPERS["_LAYER_QUERY_FILTER_KINDS"],
            vertical_radius=radius,
        )
        self.assertEqual(count, 1)

    def test_default_hidden_ancestry_excludes_descendant_layers(self):
        class Collection(dict):
            def __init__(self, *args, children=(), **kwargs):
                super().__init__(*args, **kwargs)
                self.children = list(children)

        hidden_layer = Collection(group_type="LayerInfo", w2layer_path=r"levels\hidden.w2l")
        visible_layer = Collection(group_type="LayerInfo", w2layer_path=r"levels\visible.w2l")
        hidden_group = Collection(
            group_type="LayerGroup",
            witcher_visible_on_start=False,
            children=(hidden_layer,),
        )
        visible_group = Collection(
            group_type="LayerGroup",
            witcher_visible_on_start=True,
            children=(visible_layer,),
        )
        root = Collection(children=(hidden_group, visible_group))

        hidden_paths = HELPERS["_default_hidden_level_paths"](root)

        self.assertEqual(hidden_paths, {r"levels\hidden.w2l"})


if __name__ == "__main__":
    unittest.main()
