import ast
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


UI_VOICE_PATH = Path(__file__).resolve().parents[1] / "witcher3_tools" / "ui" / "ui_voice.py"


def _load_function(name, namespace):
    tree = ast.parse(UI_VOICE_PATH.read_text(encoding="utf-8-sig"))
    node = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(UI_VOICE_PATH), "exec"), namespace)
    return namespace[name]


class VoiceCacheSignatureTests(unittest.TestCase):
    def test_w3_source_signature_covers_stable_inputs(self):
        namespace = {
            "VOICE_GAME_W3": "W3",
            "_voice_cache_identity": lambda _context=None: ("W3", "en", "fr"),
            "SpeechManager": SimpleNamespace(
                BuildSourceSignature=lambda _language: (
                    {"count": 2, "hash": "speech-hash"},
                    {"base_path": "game"},
                )
            ),
            "cache_meta": SimpleNamespace(
                signature_w3strings=lambda _base_path, _language: (
                    {"count": 1, "hash": "strings-hash"},
                    {},
                )
            ),
            "_voice_node_input_signature": lambda: {"hash": "node-input-hash"},
            "log": SimpleNamespace(debug=lambda *_args, **_kwargs: None),
        }
        source_signature = _load_function("_voice_source_signature", namespace)

        self.assertEqual(
            source_signature(),
            {
                "speech": "speech-hash",
                "strings": "strings-hash",
                "node_inputs": "node-input-hash",
            },
        )

        namespace["_voice_cache_identity"] = lambda _context=None: ("W2", "en", "fr")
        self.assertEqual(source_signature(), {})

    def test_node_input_signature_covers_metadata_name_maps_and_overrides(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            captured = []

            def compute_signature(paths):
                captured.extend(paths)
                return {"hash": "inputs"}

            voice_names_override = Path(temp_dir) / "voice_names_override.json"
            voice_tags_override = Path(temp_dir) / "voice_tags_override.json"
            namespace = {
                "__file__": str(UI_VOICE_PATH),
                "Path": Path,
                "get_cache_root": lambda create=False: temp_dir,
                "get_dev_override": lambda key, default="": str(voice_names_override) if key == "voice_names_json" else default,
                "get_dev_override_list": lambda key, default=None: [str(voice_tags_override)] if key == "scene_voice_tags_paths" else (default or []),
                "cache_meta": SimpleNamespace(compute_signature=compute_signature),
            }
            node_input_signature = _load_function("_voice_node_input_signature", namespace)

            self.assertEqual(node_input_signature(), {"hash": "inputs"})
            normalized = {str(path).replace("\\", "/") for path in captured}
            expected_suffixes = {
                "/CR2W/data/voice_names.json",
                "/CR2W/data/actor_voicelines.csv",
                "/CR2W/data/speaker_codes.json",
                "/CR2W/data/dialogue/w3/scene_voice_tags.json",
                "/CR2W/data/dialogue/w3/voice_tag_entities.json",
                "/CR2W/data/dialogue/w3/scene_dialog_index_v2.sqlite3",
                "/SceneDialog/w3/user_scene_dialog_index_v2.sqlite3",
                "/SceneDialog/W3/w3_scene_dialog_metadata.json",
            }
            for suffix in expected_suffixes:
                self.assertTrue(any(path.endswith(suffix) for path in normalized), suffix)
            self.assertIn(str(voice_names_override).replace("\\", "/"), normalized)
            self.assertIn(str(voice_tags_override).replace("\\", "/"), normalized)

    def test_rejects_changed_sources_and_accepts_matching_sources(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "voice.json"
            current_signature = {
                "speech": "new",
                "strings": "new",
                "node_inputs": "new",
            }
            namespace = {
                "os": os,
                "json": json,
                "VOICE_CACHE_VERSION": 16,
                "VOICE_GAME_W3": "W3",
                "_voice_node_cache": [],
                "_voice_cache_loaded": False,
                "_voice_filtered_indices": [],
                "_voice_cache_identity_loaded": None,
                "_voice_cache_source_revision": 4,
                "_voice_cache_source_revision_loaded": None,
                "_voice_cache_identity": lambda _context=None: ("W3", "en", "en"),
                "_voice_cache_path": lambda _context=None: str(cache_path),
                "_voice_source_signature": lambda _context=None: current_signature,
                "_refresh_speaker_stats": lambda _nodes: None,
                "log": type("Log", (), {"info": staticmethod(lambda *_args: None), "error": staticmethod(lambda *_args: None)})(),
            }
            load_cache = _load_function("_load_voice_cache", namespace)
            payload = {
                "version": 16,
                "game": "W3",
                "source_signature": {
                    "speech": "old",
                    "strings": "old",
                    "node_inputs": "old",
                },
                "nodes": [{"voiceLineId": "1"}],
            }

            cache_path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertFalse(load_cache())

            payload["source_signature"] = current_signature
            cache_path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertTrue(load_cache())
            self.assertEqual(namespace["_voice_cache_source_revision_loaded"], 4)

            namespace["_voice_cache_identity"] = lambda _context=None: ("W2", "en", "en")
            namespace["_voice_source_signature"] = lambda _context=None: {}
            namespace["_voice_node_cache"] = []
            namespace["_voice_cache_loaded"] = False
            payload["game"] = "W2"
            payload.pop("source_signature")
            cache_path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertTrue(load_cache())

    def test_runtime_invalidation_revalidates_once_without_scanning_hot_path(self):
        rebuilds = []
        loads = []
        namespace = {
            "VOICE_GAME_W3": "W3",
            "_voice_node_cache": [{"voiceLineId": "1"}],
            "_voice_cache_loaded": True,
            "_voice_filtered_indices": [0],
            "_voice_cache_identity_loaded": ("W3", "en", "en"),
            "_voice_cache_source_revision": 0,
            "_voice_cache_source_revision_loaded": 0,
            "_voice_cache_identity": lambda _context=None: ("W3", "en", "en"),
            "_load_voice_cache": lambda _context=None: loads.append(True) or False,
            "SetupNodeData": lambda do_reload_strings=False: rebuilds.append(do_reload_strings),
        }
        invalidate = _load_function("invalidate_voice_cache_sources", namespace)
        ensure = _load_function("ensure_voice_cache", namespace)

        ensure()
        self.assertEqual(loads, [])
        self.assertEqual(rebuilds, [])

        invalidate()
        ensure()
        self.assertEqual(loads, [True])
        self.assertEqual(rebuilds, [False])

    def test_runtime_invalidation_does_not_change_w2_behavior(self):
        namespace = {
            "VOICE_GAME_W3": "W3",
            "_voice_node_cache": [{"voiceLineId": "1"}],
            "_voice_cache_loaded": True,
            "_voice_filtered_indices": [0],
            "_voice_cache_identity_loaded": ("W2", "en", "en"),
            "_voice_cache_source_revision": 2,
            "_voice_cache_source_revision_loaded": 1,
            "_voice_cache_identity": lambda _context=None: ("W2", "en", "en"),
            "_load_voice_cache": lambda _context=None: self.fail("W2 cache should stay loaded"),
            "SetupNodeData": lambda do_reload_strings=False: self.fail("W2 cache should not rebuild"),
        }
        ensure = _load_function("ensure_voice_cache", namespace)

        ensure()
        self.assertTrue(namespace["_voice_cache_loaded"])


if __name__ == "__main__":
    unittest.main()
