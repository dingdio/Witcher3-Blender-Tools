import ast
import importlib.util
import json
import logging
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "witcher3_tools" / "ui" / "equipment_catalog.py"
UI_PATH = ROOT / "witcher3_tools" / "ui" / "ui_equipment.py"
META_PATH = ROOT / "witcher3_tools" / "CR2W" / "witcher_cache" / "cache_meta.py"

META_SPEC = importlib.util.spec_from_file_location("equipment_cache_meta_under_test", META_PATH)
cache_meta = importlib.util.module_from_spec(META_SPEC)
sys.modules[META_SPEC.name] = cache_meta
META_SPEC.loader.exec_module(cache_meta)


def _load_catalog_functions(cache_root):
    tree = ast.parse(CATALOG_PATH.read_text(encoding="utf-8"), filename=str(CATALOG_PATH))
    wanted = {
        "flatten_bundle_item_candidates",
        "select_final_bundle_item",
        "get_category_cache_file",
        "build_w3_category_cache_source_signature",
        "_bundle_xml_cache_has_xml",
        "_bundle_xml_cache_is_complete",
        "_w3_bundle_xml_source_snapshot",
        "_w3_bundle_xml_extraction_recovers",
        "_w3_category_cache_is_current",
        "_w3_loaded_category_cache_is_stale",
        "reset_w3_category_cache_runtime",
        "save_category_cache",
        "load_category_cache",
        "ensure_equipment_catalog_ready",
        "get_equipment_xml_bundle_cache_root",
        "extract_equipment_xmls_from_bundles",
        "merge_equipment_xml_data",
        "refresh_w3_catalog_from_xml",
    }
    nodes = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]

    def is_under_root(path_value, root):
        try:
            path_key = os.path.normcase(os.path.abspath(os.path.normpath(str(path_value))))
            root_key = os.path.normcase(os.path.abspath(os.path.normpath(str(root))))
            return os.path.commonpath([path_key, root_key]) == root_key
        except Exception:
            return False

    namespace = {
        "Path": Path,
        "json": json,
        "os": os,
        "time": time,
        "log": logging.getLogger("equipment_bundle_xml_cache_test"),
        "cache_meta": cache_meta,
        "get_cache_root": lambda create=True: str(cache_root),
        "is_under_root": is_under_root,
        "_normalize_source_game": lambda value="w3": str(value or "w3").lower(),
        "_CATEGORY_CACHE_FILE": Path(cache_root) / "equipment_categories.json",
        "_CATEGORY_CACHE_SCHEMA_VERSION": 2,
        "_CATEGORY_SOURCE_VALIDATION_INTERVAL_SECONDS": 5.0,
        "_CATEGORY_REFRESH_RETRY_INTERVAL_SECONDS": 30.0,
        "_BUNDLE_XML_RETRY_INTERVAL_SECONDS": 300.0,
        "_BUNDLE_XML_RECOVERY_STATE": {"checked_at": None},
        "_W3_CATEGORY_BUNDLE_STATE": {
            "loaded": False,
            "stale": False,
            "signature": {},
            "source_checked_at": 0.0,
            "refresh_after": 0.0,
        },
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(CATALOG_PATH), "exec"), namespace)
    return namespace


class _BundleItem:
    def __init__(self, name, contents):
        self.name = name
        self.contents = contents

    def extract_to_file(self, file_name):
        path = Path(file_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.contents, encoding="utf-8")


class _FailingBundleItem:
    def __init__(self, name):
        self.name = name

    def extract_to_file(self, file_name):
        # Failed extraction can leave empty directories.
        Path(file_name).parent.mkdir(parents=True, exist_ok=True)
        raise OSError("simulated extraction failure")


class EquipmentBundleXmlCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.cache_root = Path(self.temp_dir.name)
        self.namespace = _load_catalog_functions(self.cache_root)
        self.signature = {"hash": "bundle-signature", "count": 1}
        self.source = {"type": "bundle", "base_path": "game"}
        self.items = {}
        self.reset_calls = []
        self.categories = {}
        self.attributes = {}
        self.namespace["BundleManager"] = SimpleNamespace(
            BuildSourceSignature=lambda _loadmods: (dict(self.signature), dict(self.source))
        )
        self.namespace["LoadBundleManager"] = self._load_bundle_manager
        self.namespace.update({
            "get_equipment_catalog": lambda _source_game="w3": (self.categories, self.attributes),
            "infer_catalog_cache_flags": lambda attrs: (
                any("icon_path" in value for value in attrs.values()),
                any("hold_template" in value for value in attrs.values()),
            ),
            "set_catalog_cache_flags": lambda *_args, **_kwargs: None,
            "clear_item_attribute_identifier_lookup": lambda *_args: None,
            "ensure_equipment_catalog_for_search_roots": lambda _roots: None,
            "catalog_has_browser_icon_fields": lambda _source_game: True,
        })

    def tearDown(self):
        self.temp_dir.cleanup()

    def _load_bundle_manager(self, reset_cache=False):
        self.reset_calls.append(reset_cache)
        return SimpleNamespace(Items=self.items)

    def _xml_root(self):
        return Path(self.namespace["get_equipment_xml_bundle_cache_root"]())

    def _write_meta(self, extracted_files, pending_removals=None):
        root = self._xml_root()
        source = dict(self.source)
        source["extracted_files"] = list(extracted_files)
        if pending_removals is not None:
            source["pending_removals"] = list(pending_removals)
        cache_meta.save_meta(
            cache_meta.get_meta_path(str(root)),
            cache_meta.make_meta(
                "equipment_items_xml_bundle",
                str(root),
                dict(self.signature),
                source,
            ),
        )

    def _write_category_cache(self, *, signature_marker=True):
        data = {
            "schema_version": 2,
            "category_items": {"armor": [["armor", "Armor", "armor.w2ent"]]},
            "item_attributes": {"armor": {"item_name": "armor"}},
        }
        if signature_marker is not None:
            data["bundle_xml_source_signature"] = signature_marker
        cache_file = Path(self.namespace["get_category_cache_file"]("w3"))
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(data), encoding="utf-8")

    def test_legacy_cache_is_rebuilt_with_changed_and_new_xml(self):
        root = self._xml_root()
        existing = root / "gameplay" / "items" / "defs.xml"
        unrelated = root / "user_notes.xml"
        existing.parent.mkdir(parents=True, exist_ok=True)
        existing.write_text("old", encoding="utf-8")
        unrelated.write_text("keep", encoding="utf-8")
        self.items = {
            r"gameplay\items\defs.xml": _BundleItem("defs", "new"),
            r"gameplay\items\extra_defs.xml": _BundleItem("extra", "extra"),
        }

        result = self.namespace["extract_equipment_xmls_from_bundles"]()

        self.assertEqual(Path(result), root)
        self.assertEqual(existing.read_text(encoding="utf-8"), "new")
        self.assertEqual(
            (root / "gameplay" / "items" / "extra_defs.xml").read_text(encoding="utf-8"),
            "extra",
        )
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep")
        self.assertEqual(self.reset_calls, [True])
        meta = cache_meta.load_meta(cache_meta.get_meta_path(str(root)))
        self.assertEqual(
            meta["source"]["extracted_files"],
            ["gameplay/items/defs.xml", "gameplay/items/extra_defs.xml"],
        )

    def test_matching_signature_keeps_fast_cached_path(self):
        root = self._xml_root()
        cached = root / "gameplay" / "items" / "defs.xml"
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_text("cached", encoding="utf-8")
        self._write_meta(["gameplay/items/defs.xml"])

        result = self.namespace["extract_equipment_xmls_from_bundles"]()

        self.assertEqual(Path(result), root)
        self.assertEqual(cached.read_text(encoding="utf-8"), "cached")
        self.assertEqual(self.reset_calls, [])

    def test_forced_refresh_replaces_and_removes_only_tracked_files(self):
        root = self._xml_root()
        current = root / "gameplay" / "items" / "defs.xml"
        removed = root / "gameplay" / "items" / "removed.xml"
        unrelated = root / "custom.xml"
        current.parent.mkdir(parents=True, exist_ok=True)
        current.write_text("old", encoding="utf-8")
        removed.write_text("removed", encoding="utf-8")
        unrelated.write_text("keep", encoding="utf-8")
        self._write_meta([
            "gameplay/items/defs.xml",
            "gameplay/items/removed.xml",
        ])
        self.items = {
            r"gameplay\items\defs.xml": _BundleItem("defs", "new"),
        }

        self.namespace["extract_equipment_xmls_from_bundles"](force_refresh=True)

        self.assertEqual(current.read_text(encoding="utf-8"), "new")
        self.assertFalse(removed.exists())
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep")
        self.assertEqual(self.reset_calls, [True])

    def test_failed_stale_xml_removal_is_tracked_and_retried(self):
        root = self._xml_root()
        current = root / "gameplay" / "items" / "defs.xml"
        removed = root / "gameplay" / "items" / "removed.xml"
        current.parent.mkdir(parents=True, exist_ok=True)
        current.write_text("old", encoding="utf-8")
        removed.write_text("removed", encoding="utf-8")
        self._write_meta([
            "gameplay/items/defs.xml",
            "gameplay/items/removed.xml",
        ])
        self.items = {
            r"gameplay\items\defs.xml": _BundleItem("defs", "new"),
        }
        real_remove = os.remove

        def fail_removed(path):
            if os.path.normcase(str(path)) == os.path.normcase(str(removed)):
                raise PermissionError("locked")
            return real_remove(path)

        with mock.patch.object(os, "remove", side_effect=fail_removed):
            self.namespace["extract_equipment_xmls_from_bundles"](force_refresh=True)

        meta = cache_meta.load_meta(cache_meta.get_meta_path(str(root)))
        self.assertEqual(meta["source"]["pending_removals"], ["gameplay/items/removed.xml"])
        self.assertIn("gameplay/items/removed.xml", meta["source"]["extracted_files"])
        self.assertTrue(removed.exists())

        self.namespace["extract_equipment_xmls_from_bundles"]()

        meta = cache_meta.load_meta(cache_meta.get_meta_path(str(root)))
        self.assertEqual(meta["source"]["pending_removals"], [])
        self.assertEqual(meta["source"]["extracted_files"], ["gameplay/items/defs.xml"])
        self.assertFalse(removed.exists())
        self.assertEqual(self.reset_calls, [True, True])

    def test_pending_xml_cleanup_cannot_mark_category_cache_current(self):
        xml_path = self._xml_root() / "gameplay" / "items" / "defs.xml"
        xml_path.parent.mkdir(parents=True, exist_ok=True)
        xml_path.write_text("cached", encoding="utf-8")
        self._write_meta(
            ["gameplay/items/defs.xml", "gameplay/items/removed.xml"],
            pending_removals=["gameplay/items/removed.xml"],
        )
        self.categories["armor"] = [("armor", "Armor", "armor.w2ent")]
        self.attributes["armor"] = {"item_name": "armor"}

        signature, source, present = self.namespace["_w3_bundle_xml_source_snapshot"]()
        self.namespace["save_category_cache"]("w3")

        cache_file = Path(self.namespace["get_category_cache_file"]("w3"))
        cache_data = json.loads(cache_file.read_text(encoding="utf-8"))
        self.assertEqual(signature, {})
        self.assertEqual(source["pending_removals"], ["gameplay/items/removed.xml"])
        self.assertTrue(present)
        self.assertNotIn("bundle_xml_source_signature", cache_data)
        self.assertTrue(self.namespace["_W3_CATEGORY_BUNDLE_STATE"]["stale"])
        self.assertFalse(Path(cache_meta.get_meta_path(str(cache_file))).exists())

    def test_runtime_reset_restores_only_built_in_w3_categories(self):
        category_items = {"old": [("old", "Old", "old.w2ent")]}
        item_attributes = {"old": {"item_name": "old"}}
        calls = []
        self.namespace.update({
            "category_items": category_items,
            "item_attributes": item_attributes,
            "default_categories": {"built_in": [("base", "Base", "base.w2ent")]},
            "set_catalog_cache_flags": lambda source_game: calls.append(("flags", source_game)),
            "clear_item_attribute_identifier_lookup": lambda source_game: calls.append(("lookup", source_game)),
            "_notify_icon_cache_clear": lambda: calls.append(("icons", None)),
            "_notify_template_cache_clear": lambda: calls.append(("templates", None)),
        })
        self.namespace["_W3_CATEGORY_BUNDLE_STATE"].update({
            "loaded": True,
            "stale": True,
            "signature": {"hash": "old"},
        })

        self.namespace["reset_w3_category_cache_runtime"]()

        self.assertEqual(category_items, self.namespace["default_categories"])
        self.assertEqual(item_attributes, {})
        self.assertEqual(
            self.namespace["_W3_CATEGORY_BUNDLE_STATE"],
            {
                "loaded": False,
                "stale": False,
                "signature": {},
                "source_checked_at": 0.0,
                "refresh_after": 0.0,
            },
        )
        self.assertEqual(
            calls,
            [("flags", "w3"), ("lookup", "w3"), ("icons", None), ("templates", None)],
        )

    def test_changed_bundle_signature_rebuilds_stale_category_cache(self):
        self._write_category_cache(signature_marker={"hash": "old", "count": 1})

        def refresh(_context):
            self.categories.clear()
            self.categories["armor"] = [("new", "New", "new.w2ent")]
            self.attributes.clear()
            self.attributes["new"] = {"item_name": "new"}
            return True

        self.namespace["refresh_w3_catalog_from_xml"] = refresh

        loaded = self.namespace["ensure_equipment_catalog_ready"]("w3", context=object())

        self.assertTrue(loaded)
        self.assertNotIn("armor", self.attributes)
        self.assertIn("new", self.attributes)

    def test_legacy_manual_category_cache_remains_valid_without_bundle_xml(self):
        self._write_category_cache(signature_marker=None)

        loaded = self.namespace["load_category_cache"]("w3")

        self.assertTrue(loaded)
        self.assertIn("armor", self.categories)
        self.assertIn("armor", self.attributes)

    def test_current_legacy_bundle_category_cache_remains_valid(self):
        xml_path = self._xml_root() / "gameplay" / "items" / "defs.xml"
        xml_path.parent.mkdir(parents=True, exist_ok=True)
        xml_path.write_text("cached", encoding="utf-8")
        self._write_meta(["gameplay/items/defs.xml"])
        self._write_category_cache(signature_marker=None)

        loaded = self.namespace["load_category_cache"]("w3")

        self.assertTrue(loaded)
        self.assertIn("armor", self.attributes)

    def test_saved_category_cache_records_bundle_signature_and_sidecar(self):
        xml_path = self._xml_root() / "gameplay" / "items" / "defs.xml"
        xml_path.parent.mkdir(parents=True, exist_ok=True)
        xml_path.write_text("cached", encoding="utf-8")
        self._write_meta(["gameplay/items/defs.xml"])
        self.categories["armor"] = [("armor", "Armor", "armor.w2ent")]
        self.attributes["armor"] = {"item_name": "armor"}

        self.namespace["save_category_cache"]("w3")

        cache_file = Path(self.namespace["get_category_cache_file"]("w3"))
        cache_data = json.loads(cache_file.read_text(encoding="utf-8"))
        meta = cache_meta.load_meta(cache_meta.get_meta_path(str(cache_file)))
        self.assertEqual(cache_data["bundle_xml_source_signature"], self.signature)
        self.assertEqual(meta["signature"], self.signature)

    def test_category_save_uses_xml_snapshot_not_newer_game_signature(self):
        xml_path = self._xml_root() / "gameplay" / "items" / "defs.xml"
        xml_path.parent.mkdir(parents=True, exist_ok=True)
        xml_path.write_text("old cached XML", encoding="utf-8")
        self.signature = {"hash": "old-bundle-signature", "count": 1}
        self._write_meta(["gameplay/items/defs.xml"])
        self.signature = {"hash": "changed-source-signature", "count": 1}
        self.categories["armor"] = [("armor", "Armor", "armor.w2ent")]
        self.attributes["armor"] = {"item_name": "armor"}

        self.namespace["save_category_cache"]("w3")

        cache_file = Path(self.namespace["get_category_cache_file"]("w3"))
        cache_data = json.loads(cache_file.read_text(encoding="utf-8"))
        self.assertEqual(
            cache_data["bundle_xml_source_signature"]["hash"],
            "old-bundle-signature",
        )
        self.assertTrue(self.namespace["_w3_loaded_category_cache_is_stale"]())

    def test_manual_category_save_has_explicit_empty_bundle_signature(self):
        self.categories["armor"] = [("armor", "Armor", "armor.w2ent")]
        self.attributes["armor"] = {"item_name": "armor"}

        self.namespace["save_category_cache"]("w3")

        cache_file = Path(self.namespace["get_category_cache_file"]("w3"))
        cache_data = json.loads(cache_file.read_text(encoding="utf-8"))
        self.assertEqual(cache_data["bundle_xml_source_signature"], {})
        self.assertFalse(Path(cache_meta.get_meta_path(str(cache_file))).exists())

    def test_catalog_refresh_replaces_old_in_memory_catalog(self):
        categories = {"old": [("old", "old", "old.w2ent")]}
        attributes = {"old": {"item_name": "old"}}
        force_values = []
        self.namespace.update({
            "get_all_addon_prefs": lambda _context: SimpleNamespace(),
            "get_equipment_xml_sources": lambda _context, _prefs, force_bundle_refresh=False: (
                force_values.append(force_bundle_refresh) or [("Bundles", "bundle", True)]
            ),
            "extract_categories_from_xml": lambda _path: (
                ["new"],
                {"new": [("new", "new", "new.w2ent")]},
                {"new": {"item_name": "new"}},
            ),
            "get_equipment_catalog": lambda _source_game: (categories, attributes),
            "clear_item_attribute_identifier_lookup": lambda _source_game: None,
            "save_category_cache": lambda _source_game: True,
            "_notify_template_cache_clear": lambda: None,
        })

        result = self.namespace["refresh_w3_catalog_from_xml"](
            SimpleNamespace(),
            force_bundle_refresh=True,
        )

        self.assertTrue(result)
        self.assertEqual(force_values, [True])
        self.assertNotIn("old", categories)
        self.assertNotIn("old", attributes)
        self.assertIn("new", categories)
        self.assertIn("new", attributes)

    def test_loaded_category_signature_check_is_throttled(self):
        checks = []
        clock = SimpleNamespace(value=100.0)
        self.namespace["time"] = SimpleNamespace(monotonic=lambda: clock.value)
        self.namespace["build_w3_category_cache_source_signature"] = lambda: (
            checks.append(True) or dict(self.signature),
            dict(self.source),
        )
        self.namespace["_W3_CATEGORY_BUNDLE_STATE"].update({
            "loaded": True,
            "stale": False,
            "signature": dict(self.signature),
            "source_checked_at": 0.0,
        })

        self.assertFalse(self.namespace["_w3_loaded_category_cache_is_stale"]())
        self.assertFalse(self.namespace["_w3_loaded_category_cache_is_stale"]())
        self.assertEqual(checks, [True])

        clock.value += self.namespace["_CATEGORY_SOURCE_VALIDATION_INTERVAL_SECONDS"]
        self.assertFalse(self.namespace["_w3_loaded_category_cache_is_stale"]())
        self.assertEqual(checks, [True, True])

    def test_failed_catalog_refresh_is_not_retried_per_browser_row(self):
        refreshes = []
        clock = SimpleNamespace(value=100.0)
        self.namespace["time"] = SimpleNamespace(monotonic=lambda: clock.value)
        self.attributes["armor"] = {"item_name": "armor"}
        self.namespace["_W3_CATEGORY_BUNDLE_STATE"].update({
            "loaded": True,
            "stale": True,
            "refresh_after": 0.0,
        })
        self.namespace["refresh_w3_catalog_from_xml"] = lambda _context: (
            refreshes.append(True) and False
        )

        self.namespace["ensure_equipment_catalog_ready"]("w3", context=object())
        self.namespace["ensure_equipment_catalog_ready"]("w3", context=object())
        self.assertEqual(refreshes, [True])

        clock.value += self.namespace["_CATEGORY_REFRESH_RETRY_INTERVAL_SECONDS"]
        self.namespace["ensure_equipment_catalog_ready"]("w3", context=object())
        self.assertEqual(refreshes, [True, True])

    def test_empty_stale_catalog_is_not_revalidated_per_browser_row(self):
        checks = []
        refreshes = []
        clock = SimpleNamespace(value=100.0)
        self._write_category_cache(signature_marker={"hash": "old", "count": 1})
        self.namespace["time"] = SimpleNamespace(monotonic=lambda: clock.value)
        self.namespace["build_w3_category_cache_source_signature"] = lambda: (
            checks.append(True) or dict(self.signature),
            dict(self.source),
        )
        self.namespace["refresh_w3_catalog_from_xml"] = lambda _context: (
            refreshes.append(True) and False
        )

        self.namespace["ensure_equipment_catalog_ready"]("w3", context=object())
        self.namespace["ensure_equipment_catalog_ready"]("w3", context=object())

        self.assertEqual(checks, [True])
        self.assertEqual(refreshes, [True])
        self.assertEqual(self.attributes, {})

    def test_failed_extraction_is_not_reported_as_source(self):
        self.items = {
            r"gameplay\items\defs.xml": _FailingBundleItem("defs"),
            r"dlc\dlc10\data\gameplay\items\dlc10_wolf_swords.xml": _FailingBundleItem("wolf"),
        }

        result = self.namespace["extract_equipment_xmls_from_bundles"]()

        self.assertEqual(result, "")
        self.assertFalse(self.namespace["_bundle_xml_cache_has_xml"]())

    def test_partial_extraction_is_incomplete_and_retried(self):
        wolf = r"dlc\dlc10\data\gameplay\items\dlc10_wolf_swords.xml"
        self.items = {
            r"gameplay\items\defs.xml": _BundleItem("defs", "ok"),
            wolf: _FailingBundleItem("wolf"),
        }

        result = self.namespace["extract_equipment_xmls_from_bundles"]()

        self.assertEqual(Path(result), self._xml_root())
        self.assertTrue(self.namespace["_bundle_xml_cache_has_xml"]())
        self.assertFalse(self.namespace["_bundle_xml_cache_is_complete"]())
        _signature, _source, present = self.namespace["_w3_bundle_xml_source_snapshot"]()
        self.assertFalse(present)

        self.items[wolf] = _BundleItem("wolf", "ok")
        self.assertTrue(self.namespace["_w3_bundle_xml_extraction_recovers"]())
        self.assertTrue(self.namespace["_bundle_xml_cache_is_complete"]())
        self.assertEqual(self.reset_calls, [True, True])

    def test_catalog_built_without_bundle_xml_rebuilds_once_extraction_recovers(self):
        self._write_category_cache(signature_marker={})
        self.items = {r"gameplay\items\defs.xml": _BundleItem("defs", "recovered")}

        loaded = self.namespace["load_category_cache"]("w3")

        self.assertFalse(loaded)
        self.assertTrue(self.namespace["_W3_CATEGORY_BUNDLE_STATE"]["stale"])
        self.assertTrue(self.namespace["_bundle_xml_cache_has_xml"]())

    def test_catalog_built_without_bundle_xml_rebuilds_when_xml_completed_earlier(self):
        # Extraction succeeded through another path before the catalog was reloaded;
        # an already-complete XML cache must count as recovered, without re-extracting.
        self._write_category_cache(signature_marker={})
        self.items = {r"gameplay\items\defs.xml": _BundleItem("defs", "ok")}
        self.namespace["extract_equipment_xmls_from_bundles"]()
        self.assertTrue(self.namespace["_bundle_xml_cache_is_complete"]())
        self.reset_calls.clear()

        loaded = self.namespace["load_category_cache"]("w3")

        self.assertFalse(loaded)
        self.assertTrue(self.namespace["_W3_CATEGORY_BUNDLE_STATE"]["stale"])
        self.assertEqual(self.reset_calls, [])

    def test_legacy_cache_without_bundle_xml_rebuilds_once_extraction_recovers(self):
        self._write_category_cache(signature_marker=None)
        self.items = {r"gameplay\items\defs.xml": _BundleItem("defs", "recovered")}

        loaded = self.namespace["load_category_cache"]("w3")

        self.assertFalse(loaded)
        self.assertTrue(self.namespace["_W3_CATEGORY_BUNDLE_STATE"]["stale"])

    def test_catalog_without_bundle_xml_stays_valid_while_extraction_fails(self):
        self._write_category_cache(signature_marker={})
        self.items = {r"gameplay\items\defs.xml": _FailingBundleItem("defs")}

        loaded = self.namespace["load_category_cache"]("w3")

        self.assertTrue(loaded)
        self.assertIn("armor", self.attributes)

    def test_extraction_recovery_probe_is_throttled(self):
        self.items = {r"gameplay\items\defs.xml": _FailingBundleItem("defs")}

        self.assertFalse(self.namespace["_w3_bundle_xml_extraction_recovers"]())
        self.assertFalse(self.namespace["_w3_bundle_xml_extraction_recovers"]())

        self.assertEqual(self.reset_calls, [True])

    def test_loaded_catalog_with_empty_signature_goes_stale_on_recovery(self):
        self.namespace["_W3_CATEGORY_BUNDLE_STATE"].update({
            "loaded": True,
            "stale": False,
            "signature": {},
            "source_checked_at": 0.0,
        })
        self.items = {r"gameplay\items\defs.xml": _BundleItem("defs", "recovered")}

        self.assertTrue(self.namespace["_w3_loaded_category_cache_is_stale"]())

    def test_refresh_operator_requests_forced_bundle_refresh(self):
        tree = ast.parse(UI_PATH.read_text(encoding="utf-8"), filename=str(UI_PATH))
        operator = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "EQUIPMENT_OT_RefreshCategories"
        )
        refresh_call = next(
            node for node in ast.walk(operator)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "refresh_w3_catalog_from_xml"
        )
        keyword = next(item for item in refresh_call.keywords if item.arg == "force_bundle_refresh")
        self.assertIsInstance(keyword.value, ast.Constant)
        self.assertIs(keyword.value.value, True)


if __name__ == "__main__":
    unittest.main()
