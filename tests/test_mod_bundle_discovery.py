import importlib
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def _install_namespace_stub(qualified_name: str, package_path: Path) -> None:
    if qualified_name in sys.modules:
        return
    module = types.ModuleType(qualified_name)
    module.__path__ = [str(package_path)]
    module.__package__ = qualified_name
    sys.modules[qualified_name] = module


class _FakeBundleItem:
    def __init__(self, bundle):
        self.bundle = bundle
        self.name = r"dlc\modSample\modSample.reddlc"
        self.size = 1
        self.zsize = 1
        self.timestamp = 0
        self.crc = 0


class _FakeBundle:
    def __init__(self, filename):
        if os.path.basename(filename).lower().startswith("locked"):
            raise PermissionError(filename)
        self.ArchiveAbsolutePath = filename
        self.Items = {r"dlc\modSample\modSample.reddlc": _FakeBundleItem(self)}


_install_namespace_stub("witcher3_tools", REPO_ROOT / "witcher3_tools")
_install_namespace_stub("witcher3_tools.CR2W", REPO_ROOT / "witcher3_tools" / "CR2W")
_install_namespace_stub(
    "witcher3_tools.CR2W.witcher_cache",
    REPO_ROOT / "witcher3_tools" / "CR2W" / "witcher_cache",
)
_install_namespace_stub(
    "witcher3_tools.CR2W.witcher_cache.Bundles",
    REPO_ROOT / "witcher3_tools" / "CR2W" / "witcher_cache" / "Bundles",
)

bundle_stub = types.ModuleType("witcher3_tools.CR2W.witcher_cache.Bundles.Bundle")
bundle_stub.Bundle = _FakeBundle
sys.modules[bundle_stub.__name__] = bundle_stub

bundle_manager_module = importlib.import_module(
    "witcher3_tools.CR2W.witcher_cache.Bundles.BundleManager"
)
dlc_manager_module = importlib.import_module(
    "witcher3_tools.CR2W.witcher_cache.DLC.DLCManager"
)
archive_config_module = importlib.import_module(
    "witcher3_tools.CR2W.witcher_cache.common_cache.WitcherArchiveManager"
)

BundleManager = bundle_manager_module.BundleManager
Configuration = archive_config_module.Configuration


def _touch(path: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(b"bundle")
    return path


class ModBundleDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self._old_executable_path = Configuration.ExecutablePath
        self._old_game_mod_dir = Configuration.GameModDir
        self._old_game_dlc_dir = Configuration.GameDlcDir

    def tearDown(self):
        Configuration.ExecutablePath = self._old_executable_path
        Configuration.GameModDir = self._old_game_mod_dir
        Configuration.GameDlcDir = self._old_game_dlc_dir

    def _set_game_root(self, root: str):
        Configuration.ExecutablePath = root
        Configuration.GameModDir = os.path.join(root, "mods")
        Configuration.GameDlcDir = os.path.join(root, "dlc")

    def test_mod_dlc_bundles_load_without_mods_folder(self):
        with tempfile.TemporaryDirectory() as root:
            self._set_game_root(root)
            _touch(os.path.join(root, "dlc", "modSample", "content", "bundles", "blob.bundle"))

            manager = BundleManager()
            manager.LoadModsBundles(Configuration.GameModDir, Configuration.GameDlcDir)

        self.assertIn(r"modSample\dlc\modSample\modSample.reddlc", manager.Items)

    def test_singular_mod_root_is_scanned(self):
        with tempfile.TemporaryDirectory() as root:
            self._set_game_root(root)
            _touch(os.path.join(root, "mod", "modSample", "content", "bundles", "blob.bundle"))

            manager = BundleManager()
            manager.LoadModsBundles(Configuration.GameModDir, Configuration.GameDlcDir)

        self.assertIn(r"modSample\dlc\modSample\modSample.reddlc", manager.Items)

    def test_locked_mod_bundle_is_skipped_without_aborting_scan(self):
        with tempfile.TemporaryDirectory() as root:
            self._set_game_root(root)
            _touch(os.path.join(root, "mods", "modLocked", "content", "bundles", "locked.bundle"))
            _touch(os.path.join(root, "mods", "modSample", "content", "bundles", "blob.bundle"))

            manager = BundleManager()
            manager.LoadModsBundles(Configuration.GameModDir, Configuration.GameDlcDir)

        self.assertEqual(len(manager.locked_bundle_files), 1)
        self.assertIn(r"modSample\dlc\modSample\modSample.reddlc", manager.Items)

    def test_assets_mod_reddlc_path_accepts_extra_depot_prefix(self):
        virtual_path = r"modSample\content\content0\dlc\modSample\modSample.reddlc"

        self.assertEqual(
            dlc_manager_module._virtual_assets_mod_reddlc_path(virtual_path),
            r"dlc\modSample\modSample.reddlc",
        )


if __name__ == "__main__":
    unittest.main()
