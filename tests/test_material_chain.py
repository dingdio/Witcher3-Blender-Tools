"""Tests for material-chain classification helpers."""

import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree


CHAIN_PATH = (
    Path(__file__).resolve().parents[1]
    / "witcher3_tools"
    / "materials"
    / "chain.py"
)
SPEC = importlib.util.spec_from_file_location("witcher_material_chain", CHAIN_PATH)
chain = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(chain)


class TestImportedParamClassification(unittest.TestCase):
    def test_external_material_params_stay_in_source_frame(self):
        param = ElementTree.Element("param", name="Diffuse")

        marked = chain.mark_imported_params_as_local(
            [param],
            {"Diffuse"},
            material_local=False,
        )

        self.assertEqual(marked, 0)
        self.assertIsNone(param.get("witcher_include"))

    def test_embedded_material_params_are_local(self):
        param = ElementTree.Element("param", name="Diffuse")

        marked = chain.mark_imported_params_as_local(
            [param],
            {"Diffuse"},
            material_local=True,
        )

        self.assertEqual(marked, 1)
        self.assertEqual(param.get("witcher_include"), "true")

    def test_only_current_material_params_are_marked(self):
        current = ElementTree.Element("param", name="Diffuse")
        inherited = ElementTree.Element("param", name="Normal")

        marked = chain.mark_imported_params_as_local(
            [current, inherited],
            {"Diffuse"},
            material_local=True,
        )

        self.assertEqual(marked, 1)
        self.assertEqual(current.get("witcher_include"), "true")
        self.assertIsNone(inherited.get("witcher_include"))

    def test_external_classification_does_not_clear_explicit_marker(self):
        param = ElementTree.Element("param", name="Diffuse")
        param.set("witcher_include", "true")

        marked = chain.mark_imported_params_as_local(
            [param],
            {"Diffuse"},
            material_local=False,
        )

        self.assertEqual(marked, 0)
        self.assertEqual(param.get("witcher_include"), "true")


class TestMaterialLocalModeMatching(unittest.TestCase):
    def test_matching_local_mode_can_be_reused(self):
        material = SimpleNamespace(
            witcher_props=SimpleNamespace(local=False),
        )

        self.assertTrue(chain.material_local_mode_matches(material, False))

    def test_different_local_mode_cannot_be_reused(self):
        material = SimpleNamespace(
            witcher_props=SimpleNamespace(local=True),
        )

        self.assertFalse(chain.material_local_mode_matches(material, False))


if __name__ == "__main__":
    unittest.main()
