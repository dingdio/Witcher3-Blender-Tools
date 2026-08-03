import copy
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from _helpers import exec_functions


REPO_ROOT = Path(__file__).resolve().parents[1]
UI_MAP_PATH = REPO_ROOT / "witcher3_tools" / "ui" / "ui_map.py"
COMMON_BLENDER_PATH = REPO_ROOT / "witcher3_tools" / "CR2W" / "common_blender.py"
DC_ENTITY_PATH = REPO_ROOT / "witcher3_tools" / "CR2W" / "dc_entity.py"


class TemplateDependencyFreshnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.namespace = exec_functions(
            DC_ENTITY_PATH,
            {"_dependencies_current", "_template_file_signature"},
            {"os": os, "time": time, "_dep_stat_memo": {}},
        )

    def test_edited_file_dependency_goes_stale(self):
        namespace = self.namespace
        with tempfile.TemporaryDirectory() as tmp:
            dep_path = os.path.join(tmp, "included.w2ent")
            with open(dep_path, "wb") as handle:
                handle.write(b"one")
            signature = namespace["_template_file_signature"](dep_path)
            self.assertTrue(namespace["_dependencies_current"]((signature,)))
            with open(dep_path, "wb") as handle:
                handle.write(b"one-edited")
            namespace["_dep_stat_memo"].clear()
            self.assertFalse(namespace["_dependencies_current"]((signature,)))


class RepoResolutionContextTests(unittest.TestCase):
    def test_context_tracks_mod_priority_and_overwrite_flags(self):
        namespace = exec_functions(
            COMMON_BLENDER_PATH,
            {"get_repo_resolution_context"},
            {
                "os": os,
                "_repo_override_roots": [],
                "_repo_override_read_only": False,
                "_mod_priority_enabled": False,
                "_mod_priority_high": True,
                "_overwrite_existing": False,
                "_active_redkit_repo_roots": lambda: [],
                "_redkit_roots_for_path": lambda _path: [],
                "_active_w2_repo_context": lambda: None,
                "_w2_repo_context_for_source": lambda _path: None,
                "_get_repo_roots_from_prefs": lambda: ("", "", "", False),
            },
        )
        get_context = namespace["get_repo_resolution_context"]

        contexts = [get_context("entity.w2ent")]
        namespace["_mod_priority_enabled"] = True
        contexts.append(get_context("entity.w2ent"))
        namespace["_mod_priority_high"] = False
        contexts.append(get_context("entity.w2ent"))
        namespace["_overwrite_existing"] = True
        contexts.append(get_context("entity.w2ent"))

        self.assertEqual(len(set(contexts)), len(contexts))
        self.assertEqual(
            [context[-1] for context in contexts],
            [
                (False, True, False),
                (True, True, False),
                (True, False, False),
                (True, False, True),
            ],
        )


class LayerDependencyCopyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.namespace = exec_functions(
            UI_MAP_PATH,
            {
                "_layer_scan_active_set",
                "_load_layer_scan_dependency_shared",
                "_load_layer_scan_dependency",
            },
            {
                "copy": copy,
                "os": os,
                "threading": threading,
            },
        )

    def test_full_cache_hit_returns_deep_copy(self):
        source_path = os.path.abspath("shared_template.w2ent")
        cache_key = os.path.normcase(source_path)
        cached_level = {
            "entityAsset": {
                "appearances": [{"name": "default"}],
            }
        }
        dependency_cache = {
            "levels": {cache_key: cached_level},
            "inflight": {},
            "stats": {"hits": 0},
            "lock": threading.RLock(),
            "thread_local": threading.local(),
        }

        result = self.namespace["_load_layer_scan_dependency"](
            source_path,
            dependency_cache,
        )

        self.assertEqual(result, cached_level)
        self.assertIsNot(result, cached_level)
        self.assertIsNot(result["entityAsset"], cached_level["entityAsset"])
        result["entityAsset"]["appearances"][0]["name"] = "mutated"
        self.assertEqual(
            cached_level["entityAsset"]["appearances"][0]["name"],
            "default",
        )
        self.assertEqual(dependency_cache["stats"]["hits"], 1)


class LayerPlanDependencySignatureTests(unittest.TestCase):
    def test_logical_depot_path_wins_over_duplicate_absolute_path(self):
        namespace = exec_functions(
            UI_MAP_PATH,
            {"_collect_layer_plan_dependency_signatures"},
            {
                "os": os,
                "_LAYER_PLAN_DEPENDENCY_EXTENSIONS": (
                    ".w2ent",
                    ".w2ent.json",
                    ".w3app",
                    ".reddlc",
                    ".w2l",
                ),
            },
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            resolved_path = os.path.join(temp_dir, "shared.w2ent")
            Path(resolved_path).write_bytes(b"entity")
            namespace["_resolve_level_dependency_for_scan"] = (
                lambda _path, _version, _config: resolved_path
            )
            signatures = namespace["_collect_layer_plan_dependency_signatures"](
                [{
                    "repo_path": r"items\shared.w2ent",
                    "entity_data": {
                        "template_dependency_paths": [resolved_path],
                    },
                    "cr2w_version": 159,
                }],
                {},
            )

        self.assertEqual(len(signatures), 1)
        self.assertEqual(signatures[0]["depot_path"], r"items\shared.w2ent")


if __name__ == "__main__":
    unittest.main()
