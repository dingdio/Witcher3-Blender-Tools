from dataclasses import replace
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

    def test_scans_env_files_below_vanilla_and_dlc_definition_roots(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._touch(root / "environment" / "definitions" / "env_novigrad" / "day.env")
            self._touch(root / "environment" / "definitions" / "interiors" / "night.ENV")
            self._touch(root / "environment" / "definitions" / "ignored.txt")
            self._touch(root / "dlc" / "bob" / "data" / "environment" / "definitions" / "sunny" / "dlc.env")
            self._touch(root / "dlc" / "bob" / "data" / "stray.env")

            items = environment_catalog.scan_environment_definitions([str(root)])
            self.assertTrue(all(Path(item.absolute_path).is_file() for item in items))

        self.assertEqual(
            [item.label for item in items],
            [r"dlc\bob\sunny\dlc.env", r"env_novigrad\day.env", r"interiors\night.ENV"],
        )
        self.assertEqual(
            [item.depot_path for item in items],
            [
                r"dlc\bob\data\environment\definitions\sunny\dlc.env",
                r"environment\definitions\env_novigrad\day.env",
                r"environment\definitions\interiors\night.ENV",
            ],
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

    def test_catalog_from_depot_paths_keeps_only_definitions(self):
        items = environment_catalog.catalog_from_depot_paths(
            [
                "environment/definitions/env_skellige/storm.env",
                r"dlc\ep1\data\environment\definitions\leshy_forest.ENV",
                r"environment\definitions\balancemap\map.xbm",
                r"environment\weather_conditions\novigrad.env",
                r"dlc\bob\data\other.env",
                r"environment\definitions\env_skellige\storm.env",
            ]
        )

        self.assertEqual(
            [item.depot_path for item in items],
            [
                r"dlc\ep1\data\environment\definitions\leshy_forest.ENV",
                r"environment\definitions\env_skellige\storm.env",
            ],
        )
        self.assertEqual([item.label for item in items], [r"dlc\ep1\leshy_forest.ENV", r"env_skellige\storm.env"])
        self.assertEqual([item.absolute_path for item in items], ["", ""])
        self.assertEqual(
            items[1].identifier,
            environment_catalog.environment_identifier(r"environment\definitions\env_skellige\storm.env"),
        )

    def test_merge_catalogs_prefers_earlier_sources(self):
        disk = environment_catalog.catalog_from_depot_paths([r"environment\definitions\a.env"])
        disk = (replace(disk[0], absolute_path=r"C:\uncook\a.env"),)
        bundles = environment_catalog.catalog_from_depot_paths(
            [r"environment\definitions\A.ENV", r"environment\definitions\b.env"]
        )

        merged = environment_catalog.merge_catalogs(disk, bundles)

        self.assertEqual(
            [item.depot_path for item in merged],
            [r"environment\definitions\a.env", r"environment\definitions\b.env"],
        )
        self.assertEqual(merged[0].absolute_path, r"C:\uncook\a.env")
        self.assertEqual(merged[1].absolute_path, "")


if __name__ == "__main__":
    unittest.main()
