import ast
import types
import unittest
from pathlib import Path


UI_MAP = Path(__file__).resolve().parents[1] / "witcher3_tools" / "ui" / "ui_map.py"


class TestLayerVisibilityRoot(unittest.TestCase):
    def test_sole_scene_world_is_used_without_selection(self):
        tree = ast.parse(UI_MAP.read_text(encoding="utf-8"))
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_resolve_layer_visibility_root_collection"
        )
        namespace = {
            "_find_world_root_collection": lambda _context: None,
            "_collection_has_imported_layer_objects": lambda _collection: False,
            "_find_parent_collection": lambda _collection: None,
        }
        exec(compile(ast.Module([function], []), str(UI_MAP), "exec"), namespace)

        world = {"world_path": "levels/bob/bob.w2w"}
        context = types.SimpleNamespace(
            active_object=None,
            selected_objects=[],
            collection=None,
            scene=types.SimpleNamespace(
                collection=types.SimpleNamespace(children=[{}, world]),
            ),
        )
        self.assertIs(
            namespace["_resolve_layer_visibility_root_collection"](context),
            world,
        )


if __name__ == "__main__":
    unittest.main()
