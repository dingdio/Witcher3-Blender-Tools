import ast
import importlib.util
import logging
import os
import pickle
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANAGER_PATH = ROOT / "witcher3_tools" / "CR2W" / "witcher_cache" / "W3Strings" / "W3StringManager.py"
CACHE_META_PATH = ROOT / "witcher3_tools" / "CR2W" / "witcher_cache" / "cache_meta.py"
_ENV = {"game": "", "cache": ""}


def _load_cache_meta():
    spec = importlib.util.spec_from_file_location("_w3_string_cache_meta_test", CACHE_META_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_manager_module():
    module_name = "_w3_string_cache_manager_test"
    module = types.ModuleType(module_name)
    module.__file__ = str(MANAGER_PATH)
    module.__dict__.update(
        {
            "os": os,
            "time": time,
            "pickle": pickle,
            "Path": Path,
            "get_game_path": lambda: _ENV["game"],
            "normalize_game_path": lambda path: os.path.normpath(os.fspath(path)) if path else "",
            "has_game_content_or_dlc_root": lambda path: any(
                (Path(path) / name).is_dir() for name in ("content", "dlc")
            ) if path else False,
            "cache_meta": _load_cache_meta(),
            "get_cache_root": lambda create=True: _cache_root(create),
            "logging": logging,
        }
    )
    sys.modules[module_name] = module
    tree = ast.parse(MANAGER_PATH.read_text(encoding="utf-8-sig"))
    tree.body = [node for node in tree.body if not isinstance(node, (ast.Import, ast.ImportFrom))]
    exec(compile(tree, str(MANAGER_PATH), "exec"), module.__dict__)
    return module


def _cache_root(create=True):
    path = Path(_ENV["cache"])
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return str(path)


manager_module = _load_manager_module()


class W3StringCacheManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.game_root = root / "game"
        self.cache_root = root / "cache"
        (self.game_root / "content" / "base").mkdir(parents=True)
        _ENV.update(game=str(self.game_root), cache=str(self.cache_root))
        manager_module.Configuration.TextLanguage = "en"
        manager_module.W3StringManager.InstanceManager = None
        self.builds = []
        self.original_load = manager_module.W3StringManager.Load

        def fake_load(manager, language, path, onlyIfLanguageChanged=False):
            self.builds.append(language)
            manager.Language = language
            manager.base_path = os.path.normpath(path)
            manager.Lines = {1: f"{language} text"}
            manager.Keys = {}
            manager.KeyToId = {}
            manager.decoder_cache_version = manager_module._decoder_cache_version(language)
            manager.string_cache_format_version = manager_module._STRING_CACHE_FORMAT_VERSION

        manager_module.W3StringManager.Load = fake_load

    def tearDown(self):
        manager_module.W3StringManager.Load = self.original_load
        manager_module.W3StringManager.InstanceManager = None
        self.temp_dir.cleanup()

    def _source(self, language, contents=b"strings"):
        path = self.game_root / "content" / "base" / f"{language}.w3strings"
        path.write_bytes(contents)
        return path

    def test_get_rebuilds_only_after_same_path_sources_change(self):
        source = self._source("en")

        first = manager_module.W3StringManager.Get()
        self.assertIs(first, manager_module.W3StringManager.Get())
        self.assertEqual(self.builds, ["en"])

        source.write_bytes(b"updated strings with a different size")
        first._source_checked_at -= manager_module._SOURCE_VALIDATION_INTERVAL_SECONDS
        changed = manager_module.W3StringManager.Get()
        self.assertIsNot(changed, first)
        self.assertEqual(self.builds, ["en", "en"])

        self.assertIs(changed, manager_module.W3StringManager.Get())
        self.assertEqual(self.builds, ["en", "en"])

    def test_hot_get_reuses_recent_source_validation(self):
        self._source("en")
        manager_class = manager_module.W3StringManager
        original_builder = manager_class.BuildSourceSignature
        checks = []

        def counted_builder(*args, **kwargs):
            checks.append(True)
            return original_builder(*args, **kwargs)

        manager_class.BuildSourceSignature = staticmethod(counted_builder)
        try:
            manager = manager_class.Get()
            checks_after_build = len(checks)
            for _ in range(10):
                self.assertIs(manager, manager_class.Get())
            self.assertEqual(len(checks), checks_after_build)

            manager._source_checked_at -= manager_module._SOURCE_VALIDATION_INTERVAL_SECONDS
            self.assertIs(manager, manager_class.Get())
            self.assertEqual(len(checks), checks_after_build + 1)
        finally:
            manager_class.BuildSourceSignature = staticmethod(original_builder)

    def test_temporary_source_loss_preserves_last_good_manager(self):
        self._source("en")
        manager_class = manager_module.W3StringManager
        manager = manager_class.Get()
        manager._source_checked_at -= manager_module._SOURCE_VALIDATION_INTERVAL_SECONDS
        original_has_root = manager_module._has_string_source_root
        manager_module._has_string_source_root = lambda _path: False
        try:
            self.assertIs(manager_class.Get(), manager)
            self.assertIs(manager_class.Get(do_reload=True), manager)
            self.assertEqual(manager.Lines, {1: "en text"})
            self.assertEqual(self.builds, ["en"])
        finally:
            manager_module._has_string_source_root = original_has_root

    def test_forced_reload_preserves_manager_when_language_files_disappear(self):
        source = self._source("en")
        manager_class = manager_module.W3StringManager
        manager = manager_class.Get()
        cache_path = Path(manager_class.GetCachePath("en", create=False))
        cached_bytes = cache_path.read_bytes()
        source.unlink()

        self.assertIs(manager_class.Get(do_reload=True), manager)
        self.assertEqual(manager.Lines, {1: "en text"})
        self.assertEqual(self.builds, ["en"])
        self.assertEqual(cache_path.read_bytes(), cached_bytes)

    def test_language_specific_cache_is_reused_when_switching_back(self):
        self._source("en")
        self._source("zz")

        english = manager_module.W3StringManager.Get()
        manager_module.Configuration.TextLanguage = "zz"
        secondary = manager_module.W3StringManager.Get()
        manager_module.Configuration.TextLanguage = "en"
        english_again = manager_module.W3StringManager.Get()

        self.assertEqual(self.builds, ["en", "zz"])
        self.assertEqual(secondary.Language, "zz")
        self.assertEqual(english_again.Language, "en")
        self.assertEqual(english_again.Lines, english.Lines)

    def test_active_language_cache_path_and_signature_helpers(self):
        self._source("zz")
        manager_module.Configuration.TextLanguage = "zz"

        cache_path = manager_module.W3StringManager.GetCachePath(create=False)
        signature, source = manager_module.W3StringManager.BuildSourceSignature()

        self.assertEqual(Path(cache_path).name, "string_cache_zz.pkl")
        self.assertEqual(source["language"], "zz")
        self.assertEqual(signature["count"], 1)
        self.assertEqual(
            Path(manager_module.W3StringManager.GetCachePath("esMX", create=False)).name,
            "string_cache_esMX.pkl",
        )


if __name__ == "__main__":
    unittest.main()
