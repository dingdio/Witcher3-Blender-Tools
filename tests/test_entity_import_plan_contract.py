import ast
import json
import unittest
from pathlib import Path

from _helpers import exec_functions


IMPORTER_PATH = (
    Path(__file__).resolve().parents[1]
    / "witcher3_tools"
    / "importers"
    / "import_blender_fun.py"
)
IMPORT_ENTITY_PATH = IMPORTER_PATH.with_name("import_entity.py")


class EntityImportPlanContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(IMPORTER_PATH.read_text(encoding="utf-8"))

    def _function_source(self, name):
        return ast.unparse(next(
            node for node in self.tree.body
            if isinstance(node, ast.FunctionDef) and node.name == name
        ))

    def test_direct_and_layer_imports_share_public_materializer(self):
        self.assertIn("entity_asset", self._function_source("build_entity_import_plan"))
        self.assertIn(
            "materialize_entity_import_plan(",
            self._function_source("loadLevel"),
        )
        self.assertIn(
            "materialize_entity_import_plan(",
            self._function_source("loadLevelFromCachedPlan"),
        )

    def test_parsed_layer_failure_does_not_fall_back_to_legacy_import(self):
        loader = self._function_source("loadLevel")

        self.assertIn("Entity plan is not materializable", loader)
        self.assertNotIn("using legacy import", loader)

    def test_cloth_metadata_round_trips_and_follows_selected_appearance(self):
        namespace = exec_functions(
            IMPORT_ENTITY_PATH,
            {
                "_dedupe_entity_appearance_names",
                "normalize_entity_appearance_metadata",
                "entity_appearance_has_cloth",
            },
        )
        normalize = namespace["normalize_entity_appearance_metadata"]
        has_cloth = namespace["entity_appearance_has_cloth"]

        metadata = normalize({
            "all_names": ["ciri_player", "CIRI_PLAYER", "winter"],
            "component_metadata_known": True,
            "base_has_cloth_components": False,
            "cloth_appearance_names": ["Ciri_Player", "ciri_player"],
        })
        round_tripped = normalize(json.loads(json.dumps(metadata)))

        self.assertFalse(round_tripped["base_has_cloth_components"])
        self.assertEqual(round_tripped["cloth_appearance_names"], ["Ciri_Player"])
        self.assertTrue(round_tripped["has_cloth_components"])
        self.assertTrue(has_cloth(round_tripped, "ciri_player"))
        self.assertTrue(has_cloth(round_tripped, "CIRI_PLAYER"))
        self.assertFalse(has_cloth(round_tripped, "winter"))
        self.assertFalse(has_cloth(round_tripped, "__base__"))

        legacy = {"has_cloth_components": True}
        self.assertTrue(has_cloth(legacy, "any appearance"))
        self.assertTrue(has_cloth(normalize(legacy), "any appearance"))


if __name__ == "__main__":
    unittest.main()
