import importlib.util
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest


MODULE_PATH = Path(__file__).parents[1] / "witcher3_tools" / "environment_catalog.py"
SPEC = importlib.util.spec_from_file_location("witcher_environment_catalog_under_test", MODULE_PATH)
environment_catalog = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = environment_catalog
SPEC.loader.exec_module(environment_catalog)


class EnvironmentCatalogTests(unittest.TestCase):
    def _touch(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")

    def test_scans_only_env_files_below_native_definition_root(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._touch(root / "environment" / "definitions" / "env_novigrad" / "day.env")
            self._touch(root / "environment" / "definitions" / "interiors" / "night.ENV")
            self._touch(root / "environment" / "definitions" / "ignored.txt")
            self._touch(root / "dlc" / "bob" / "data" / "environment" / "definitions" / "dlc.env")

            items = environment_catalog.scan_environment_definitions([str(root)])

        self.assertEqual([item.label for item in items], [r"env_novigrad\day.env", r"interiors\night.ENV"])
        self.assertEqual(
            [item.depot_path for item in items],
            [r"environment\definitions\env_novigrad\day.env", r"environment\definitions\interiors\night.ENV"],
        )
        self.assertTrue(all(item.identifier.startswith("ENV_") for item in items))

    def test_first_depot_root_wins_for_duplicate_paths(self):
        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            primary = base / "primary"
            fallback = base / "fallback"
            relative = Path("environment", "definitions", "shared.env")
            self._touch(primary / relative)
            self._touch(fallback / relative)

            items = environment_catalog.scan_environment_definitions([str(primary), str(fallback)])

        self.assertEqual(len(items), 1)
        self.assertEqual(Path(items[0].absolute_path), primary / relative)
        self.assertEqual(
            items[0].identifier,
            environment_catalog.environment_identifier(r"environment\definitions\shared.env"),
        )


if __name__ == "__main__":
    unittest.main()
