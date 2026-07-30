import importlib.util
import sys
import types
import unittest
from pathlib import Path


TOOLS_DIR = Path(__file__).resolve().parents[1] / "witcher3_tools"
PACKAGE_NAME = "_dialog_language_dynamic_test"

package = types.ModuleType(PACKAGE_NAME)
package.__path__ = [str(TOOLS_DIR)]
sys.modules[PACKAGE_NAME] = package
spec = importlib.util.spec_from_file_location(
    f"{PACKAGE_NAME}.dialog_language", TOOLS_DIR / "dialog_language.py"
)
dialog_language = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = dialog_language
spec.loader.exec_module(dialog_language)


class DynamicDialogLanguageTests(unittest.TestCase):
    def test_installed_unknown_text_language_is_discovered_without_voice(self):
        original = dialog_language.installed_text_languages
        dialog_language.installed_text_languages = lambda: ("zz",)
        try:
            self.assertIn("zz", dialog_language.supported_dialog_languages())
            self.assertNotIn("zz", dialog_language.supported_voice_languages())
            self.assertEqual(dialog_language.language_label("zz"), "zz")
            self.assertIn(
                "zz",
                {item[0] for item in dialog_language.dialog_text_language_enum_items()},
            )
        finally:
            dialog_language.installed_text_languages = original


if __name__ == "__main__":
    unittest.main()
