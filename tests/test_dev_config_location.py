import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "witcher3_tools" / "dev" / "dev_config.py"


class DevConfigLocationTests(unittest.TestCase):
    def test_non_blender_tools_keep_using_package_config(self):
        with mock.patch.dict(sys.modules, {"bpy": None}):
            spec = importlib.util.spec_from_file_location("_standalone_dev_config_test", MODULE_PATH)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

        self.assertEqual(
            module.get_config_path(),
            REPO_ROOT / "witcher3_tools" / "dev" / "dev_config.json",
        )

    def test_blender_dev_config_uses_extension_storage_and_migrates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            extension_user_root = Path(temp_dir) / "extensions" / ".user"
            current_extension_dir = extension_user_root / "current_repo" / "witcher3_tools"
            previous_config = (
                extension_user_root
                / "previous_repo"
                / "witcher3_tools"
                / "dev"
                / "dev_config.json"
            )
            previous_config.parent.mkdir(parents=True)
            previous_config.write_text(
                '{"dev_mode_enabled": true, "test_paths": {"migrated": {}}}',
                encoding="utf-8",
            )
            config_path = current_extension_dir / "dev" / "dev_config.json"

            package = ModuleType("_dev_config_test")
            package.__path__ = []
            dev_package = ModuleType("_dev_config_test.dev")
            dev_package.__path__ = []
            extension_paths = ModuleType("_dev_config_test.extension_paths")
            extension_paths.get_extension_user_dir = lambda create=True: str(current_extension_dir)
            bpy = ModuleType("bpy")

            fake_modules = {
                "_dev_config_test": package,
                "_dev_config_test.dev": dev_package,
                "_dev_config_test.extension_paths": extension_paths,
                "bpy": bpy,
            }
            with mock.patch.dict(sys.modules, fake_modules):
                spec = importlib.util.spec_from_file_location(
                    "_dev_config_test.dev.dev_config",
                    MODULE_PATH,
                )
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

            self.assertEqual(module.get_config_path(), config_path)
            self.assertTrue(module.DEV_MODE_ENABLED)
            data = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertTrue(data["dev_mode_enabled"])
            self.assertIn("migrated", data["test_paths"])
