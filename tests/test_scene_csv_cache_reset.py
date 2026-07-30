import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CR2W_DIR = REPO_ROOT / "witcher3_tools" / "CR2W"
PACKAGE_NAME = "_scene_csv_cache_test"


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


package = types.ModuleType(PACKAGE_NAME)
package.__path__ = [str(CR2W_DIR)]
sys.modules[PACKAGE_NAME] = package
common_blender = types.ModuleType(f"{PACKAGE_NAME}.common_blender")
common_blender.repo_file = lambda filepath: filepath
sys.modules[common_blender.__name__] = common_blender
scene_csv = _load_module(f"{PACKAGE_NAME}.scene_csv_utils", CR2W_DIR / "scene_csv_utils.py")


class SceneCsvCacheResetTests(unittest.TestCase):
    def test_failed_lookups_retry_after_cache_reset(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            body_path = Path(temp_dir) / "scene_body_animations.csv"
            mimics_path = Path(temp_dir) / "scene_mimics_emotional_states.csv"

            scene_csv.repo_file = lambda filepath: str(
                mimics_path if "mimics" in filepath else body_path
            )
            scene_csv.reset_scene_csv_caches()
            with self.assertLogs(scene_csv.log, level="DEBUG"):
                self.assertEqual(scene_csv._parse_body_anim_csv(), {})
                self.assertEqual(scene_csv._parse_mimics_csv(), {})

            body_path.write_text(
                "status;emotion;pose;unused;animation;type\n"
                "High;Determined;Standing;;idle_anim;Idle\n",
                encoding="utf-8",
            )
            mimics_path.write_text(
                "state;eyes;pose;animation\n"
                "Happy;happy eyes;happy pose;happy animation\n",
                encoding="utf-8",
            )

            self.assertEqual(scene_csv._parse_body_anim_csv(), {})
            self.assertEqual(scene_csv._parse_mimics_csv(), {})
            scene_csv.reset_scene_csv_caches()

            body = scene_csv._parse_body_anim_csv()
            mimics = scene_csv._parse_mimics_csv()
            self.assertEqual(body[("high", "determined", "standing")]["idles"], ["idle_anim"])
            self.assertEqual(mimics["happy"]["pose"], "happy pose")


if __name__ == "__main__":
    unittest.main()
