import builtins
import ctypes
import importlib.util
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    REPO_ROOT
    / "witcher3_tools"
    / "CR2W"
    / "witcher_cache"
    / "Bundles"
    / "BundleItem.py"
)


class BundleItemNativeLoadingTests(unittest.TestCase):
    def test_native_compression_libraries_are_deferred(self):
        real_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name == "cramjam" or name.startswith("cramjam."):
                raise AssertionError("cramjam was imported during module loading")
            return real_import(name, *args, **kwargs)

        def guarded_cdll(*args, **kwargs):
            raise AssertionError("Doboz.dll was loaded during module loading")

        with (
            mock.patch.object(builtins, "__import__", side_effect=guarded_import),
            mock.patch.object(ctypes, "CDLL", side_effect=guarded_cdll),
        ):
            spec = importlib.util.spec_from_file_location("_bundle_item_lazy_test", MODULE_PATH)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

        self.assertIsNone(module.cramjam_lz4)
        self.assertIsNone(module.cramjam_snappy)
        self.assertIsNone(module.doboz_lib)
