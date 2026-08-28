import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers import REPO_ROOT, exec_functions, install_namespace_stub

IMPORT_ENTITY = REPO_ROOT / "witcher3_tools" / "importers" / "import_entity.py"


def _install_catalog_stub(alias_attrs):
    install_namespace_stub("witcher3_tools", REPO_ROOT / "witcher3_tools")
    install_namespace_stub("witcher3_tools.ui", REPO_ROOT / "witcher3_tools" / "ui")
    module = types.ModuleType("witcher3_tools.ui.equipment_catalog")
    module.get_item_attributes_by_identifier = (
        lambda identifier, source_game="w3", strict=False: alias_attrs.get(str(identifier), {})
    )
    sys.modules["witcher3_tools.ui.equipment_catalog"] = module


class LookupItemAttrsTests(unittest.TestCase):
    def setUp(self):
        self.alias_attrs = {}
        _install_catalog_stub(self.alias_attrs)
        namespace = exec_functions(
            IMPORT_ENTITY,
            ["_lookup_item_attrs"],
            {"__package__": "witcher3_tools.importers"},
        )
        self.lookup = namespace["_lookup_item_attrs"]

    def test_exact_key_wins(self):
        attrs = {"Wolf School steel sword 1": {"bound_items": ["scabbard_steel_wolf_01"]}}
        result = self.lookup(attrs, "w3", "Wolf School steel sword 1")
        self.assertEqual(result["bound_items"], ["scabbard_steel_wolf_01"])

    def test_alias_fallback_recovers_bound_items(self):
        self.alias_attrs["wolf_school_steel_sword_1"] = {"bound_items": ["scabbard_steel_wolf_01"]}
        result = self.lookup({}, "w3", "wolf_school_steel_sword_1")
        self.assertEqual(result["bound_items"], ["scabbard_steel_wolf_01"])

    def test_later_identifiers_are_tried(self):
        self.alias_attrs["witcher_steel_wolf_sword_lvl2"] = {"equip_slot": "steel_sword_back_slot"}
        result = self.lookup({}, "w3", "unknown_item", "", "witcher_steel_wolf_sword_lvl2")
        self.assertEqual(result["equip_slot"], "steel_sword_back_slot")

    def test_total_miss_returns_empty(self):
        self.assertEqual(self.lookup({}, "w3", "nope", None, ""), {})


if __name__ == "__main__":
    unittest.main()
