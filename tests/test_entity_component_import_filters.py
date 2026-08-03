import re
import unittest
from pathlib import Path

from _helpers import exec_functions


SOURCE = Path(__file__).resolve().parents[1] / "witcher3_tools" / "importers" / "import_entity.py"


class EntityComponentImportFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helpers = exec_functions(
            SOURCE,
            {
                "_component_import_option",
                "_entity_chunk_is_proxy_mesh",
                "_entity_chunk_mesh_enabled",
                "_entity_chunk_cloth_enabled",
            },
            {"Path": Path, "re": re},
        )

    def test_standalone_defaults_preserve_existing_component_imports(self):
        mesh_enabled = self.helpers["_entity_chunk_mesh_enabled"]
        cloth_enabled = self.helpers["_entity_chunk_cloth_enabled"]

        self.assertTrue(mesh_enabled({"mesh": "items/sword.w2mesh"}, None))
        self.assertTrue(mesh_enabled({"mesh": "items/sword_proxy.w2mesh"}, None))
        self.assertTrue(cloth_enabled("cloth/cape.redcloth", None, True))
        self.assertTrue(cloth_enabled("cloth/cape.redapex", None, True))

    def test_layer_options_filter_mesh_proxy_and_cloth_independently(self):
        mesh_enabled = self.helpers["_entity_chunk_mesh_enabled"]
        cloth_enabled = self.helpers["_entity_chunk_cloth_enabled"]
        options = {
            "do_import_Mesh": False,
            "do_import_ProxyMesh": True,
            "do_import_Redcloth": False,
            "do_import_Redapex": True,
        }

        self.assertFalse(mesh_enabled({"mesh": "items/sword.w2mesh"}, options))
        self.assertTrue(mesh_enabled({"mesh": "items/sword_proxy.w2mesh"}, options))
        self.assertFalse(cloth_enabled("cloth/cape.redcloth", options, True))
        self.assertTrue(cloth_enabled("cloth/cape.redapex", options, False))


if __name__ == "__main__":
    unittest.main()
