import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / "witcher3_tools" / "blender_manifest.toml"


def _declared_wheel_names():
    manifest_text = MANIFEST.read_text(encoding="utf-8")
    return re.findall(r'"wheels/([^"/]+\.whl)"', manifest_text)


class ExtensionManifestWheelTests(unittest.TestCase):
    def test_manifest_does_not_override_blender_runtime_packages(self):
        """Shared extension wheels precede Blender's site-packages globally."""
        blender_packages = {
            "certifi",
            "charset_normalizer",
            "idna",
            "numpy",
            "requests",
            "urllib3",
        }
        declared_packages = {name.partition("-")[0] for name in _declared_wheel_names()}

        self.assertTrue(declared_packages.isdisjoint(blender_packages))
