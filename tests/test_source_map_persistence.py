import ast
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "witcher3_tools" / "CR2W" / "common_blender.py"


def _load_source_map_functions():
    tree = ast.parse(TARGET.read_text(encoding="utf-8"), filename=str(TARGET))
    wanted = {"_get_source_map_path", "_save_source_map", "flush_source_map"}
    nodes = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]
    namespace = {
        "os": os,
        "json": json,
        "log": mock.Mock(),
        "_source_map_cache": {"path": "", "data": {}, "mtime": 0},
        "_source_map_dirty": False,
        "_source_map_flush_scheduled": False,
        "_SOURCE_MAP_FLUSH_DELAY": 2.0,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(TARGET), "exec"), namespace)
    return namespace


class SourceMapPersistenceTests(unittest.TestCase):
    def test_failed_flush_keeps_pending_data_dirty_for_retry(self):
        source_map = _load_source_map_functions()
        source_map["_source_map_cache"] = {
            "path": os.path.join("missing", "_witcher_tools_sources.json"),
            "data": {"a": "Depot"},
            "mtime": 0,
        }
        source_map["_source_map_dirty"] = True
        source_map["_source_map_flush_scheduled"] = True

        with (
            mock.patch.object(os, "makedirs"),
            mock.patch("builtins.open", side_effect=OSError("read only")),
        ):
            retry = source_map["flush_source_map"]()

        self.assertEqual(retry, source_map["_SOURCE_MAP_FLUSH_DELAY"])
        self.assertTrue(source_map["_source_map_dirty"])
        self.assertTrue(source_map["_source_map_flush_scheduled"])

    def test_successful_flush_atomically_persists_and_clears_dirty_flag(self):
        source_map = _load_source_map_functions()
        with tempfile.TemporaryDirectory() as root:
            map_path = os.path.join(root, "_witcher_tools_sources.json")
            source_map["_source_map_cache"] = {
                "path": map_path,
                "data": {"levels\\test.w2l": "REDkit Depot"},
                "mtime": 0,
            }
            source_map["_source_map_dirty"] = True

            self.assertIsNone(source_map["flush_source_map"]())
            self.assertFalse(source_map["_source_map_dirty"])
            self.assertFalse(os.path.exists(map_path + ".tmp"))
            with open(map_path, "r", encoding="utf-8") as handle:
                self.assertEqual(json.load(handle), source_map["_source_map_cache"]["data"])


if __name__ == "__main__":
    unittest.main()
