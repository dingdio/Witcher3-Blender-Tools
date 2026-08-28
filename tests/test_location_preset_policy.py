import ast
import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
BROWSER_PATH = ROOT / "witcher3_tools" / "w3_asset_browser.py"
UI_MAP_PATH = ROOT / "witcher3_tools" / "ui" / "ui_map.py"
IMPORT_BLENDER_FUN_PATH = ROOT / "witcher3_tools" / "importers" / "import_blender_fun.py"
LOCATIONS_PATH = ROOT / "witcher3_tools" / "CR2W" / "data" / "locations.json"


def _load_location_helpers():
    source = BROWSER_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(BROWSER_PATH))
    wanted = {
        "_collection_pointer",
        "_iter_descendant_collections",
        "_world_root_has_complete_layer_tree",
        "_terrain_tiles_within_radius",
        "_locations_data_path",
        "_user_locations_data_path",
        "_read_locations_file",
        "_location_identity",
        "_merge_locations",
        "_save_user_location",
        "_location_image_path_from_root",
        "_user_location_image_path",
        "_bundled_location_image_path",
        "_location_entry_image_path",
        "_location_preview_relative_path",
        "_location_browser_signature",
    }
    nodes = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]
    terrain_spec = importlib.util.spec_from_file_location(
        "terrain_core_for_location_tests",
        ROOT / "witcher3_tools" / "terrain_core.py",
    )
    terrain_core = importlib.util.module_from_spec(terrain_spec)
    sys.modules[terrain_spec.name] = terrain_core
    terrain_spec.loader.exec_module(terrain_core)
    namespace = {
        "hashlib": hashlib,
        "json": json,
        "os": os,
        "re": re,
        "log": SimpleNamespace(warning=lambda *_args, **_kwargs: None),
        "bpy": SimpleNamespace(context=None),
        "__file__": str(BROWSER_PATH),
        "_LOCATIONS_DATA_FILE": "locations.json",
        "_LOCATION_PREVIEW_DIR": "location_previews",
        "_USER_LOCATIONS_DATA_FILE": "user_locations.json",
        "_safe_text": lambda value: str(value or "").strip(),
        "get_extension_user_dir": lambda create=True: tempfile.gettempdir(),
        "get_game_path": lambda: "",
        "get_uncook_path": lambda _context: "",
        "win_path_exists": os.path.exists,
        "_file_signature_token": lambda _path: "same",
        "JOURNAL_BROWSER_CACHE_VERSION": 1,
        "_grid_tile_bounds": terrain_core.terrain_tile_bounds,
        "_grid_tile_from_world_position": terrain_core.terrain_tile_from_world_position,
    }
    module = ast.Module(body=nodes, type_ignores=[])
    exec(compile(module, str(BROWSER_PATH), "exec"), namespace)
    return namespace


HELPERS = _load_location_helpers()


class LocationSystemTests(unittest.TestCase):
    def test_only_a_real_global_layer_group_counts_as_a_loaded_world_tree(self):
        class Collection(dict):
            def __init__(self, *args, children=(), **kwargs):
                super().__init__(*args, **kwargs)
                self.children = list(children)

            def as_pointer(self):
                return id(self)

        layer = Collection(group_type="LayerInfo")
        global_group = Collection(
            group_type="LayerGroup",
            witcher_visible_on_start=True,
            children=(layer,),
        )
        wrapper = Collection(children=(global_group,))
        unmarked_partial_group = Collection(group_type="LayerGroup", children=(layer,))

        self.assertTrue(HELPERS["_world_root_has_complete_layer_tree"](wrapper))
        self.assertFalse(
            HELPERS["_world_root_has_complete_layer_tree"](
                Collection(children=(unmarked_partial_group,))
            )
        )

    def test_bundled_locations_use_world_position_radius_schema(self):
        locations = json.loads(LOCATIONS_PATH.read_text(encoding="utf-8"))["locations"]
        policy_keys = {"layer_dir", "layer_allow", "layer_deny", "layer_extra"}
        for location in locations:
            with self.subTest(location=location.get("name")):
                self.assertFalse(policy_keys.intersection(location))
                self.assertTrue(location.get("world_path"))
                self.assertEqual(len(location.get("position") or ()), 3)
                self.assertTrue(all(
                    isinstance(value, (int, float))
                    for value in location["position"]
                ))
                image_file = location.get("image_file")
                self.assertTrue(image_file)
                self.assertTrue((LOCATIONS_PATH.parent / image_file).is_file())

        palace = next(item for item in locations if item["name"] == "Beauclair Palace")
        self.assertEqual(palace["position"], [-696.63, -1207.23, 167.106])
        self.assertEqual(palace["radius"], 100)

    def test_location_radius_selects_every_intersected_terrain_tile(self):
        spec = SimpleNamespace(terrain_size=40.0, x_tiles=4, y_tiles=4)
        tiles = HELPERS["_terrain_tiles_within_radius"](spec, (5.0, 5.0, 0.0), 6.0)

        self.assertEqual(tiles[0], (2, 2))
        self.assertEqual(
            set(tiles),
            {(2, 2), (1, 2), (3, 2), (2, 1), (2, 3)},
        )
        self.assertEqual(
            HELPERS["_terrain_tiles_within_radius"](spec, (5.0, 5.0), 0.0),
            ((2, 2),),
        )
        self.assertEqual(
            HELPERS["_terrain_tiles_within_radius"](spec, (100.0, 100.0), 1.0),
            (),
        )
        with self.assertRaises(ValueError):
            HELPERS["_terrain_tiles_within_radius"](spec, (5.0, 5.0), -1.0)

    def test_user_overlay_updates_a_builtin_without_repeating_world_data(self):
        builtin = [{
            "name": "Palace",
            "map": "Toussaint",
            "world_path": r"dlc\bob\bob.w2w",
            "position": [1, 2, 3],
            "radius": 100,
            "image_file": "location_previews/builtin-palace.jpg",
        }]
        user = [{
            "name": "palace",
            "map": "TOUSSAINT",
            "position": [4, 5, 6],
            "image_file": "location_previews/palace.png",
        }]

        merged = HELPERS["_merge_locations"](builtin, user)

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["world_path"], builtin[0]["world_path"])
        self.assertEqual(merged[0]["radius"], 100)
        self.assertEqual(merged[0]["position"], [4, 5, 6])
        self.assertEqual(
            merged[0]["_builtin_image_file"],
            "location_previews/builtin-palace.jpg",
        )
        self.assertTrue(merged[0]["_user_location"])

    def test_user_preview_path_cannot_escape_extension_storage(self):
        original = HELPERS["get_extension_user_dir"]
        with tempfile.TemporaryDirectory() as root:
            HELPERS["get_extension_user_dir"] = lambda create=True: root
            try:
                preview = Path(root, "location_previews", "valid.png")
                preview.parent.mkdir()
                preview.write_bytes(b"png")
                self.assertEqual(
                    HELPERS["_user_location_image_path"]("location_previews/valid.png"),
                    str(preview),
                )
                self.assertEqual(HELPERS["_user_location_image_path"]("../escape.png"), "")
                self.assertEqual(HELPERS["_user_location_image_path"](str(preview)), "")
            finally:
                HELPERS["get_extension_user_dir"] = original

    def test_bundled_preview_path_cannot_escape_package_data(self):
        image_file = "location_previews/passiflora.jpg"
        self.assertEqual(
            HELPERS["_bundled_location_image_path"](image_file),
            str(LOCATIONS_PATH.parent / image_file),
        )
        self.assertEqual(HELPERS["_bundled_location_image_path"]("../escape.jpg"), "")
        self.assertEqual(
            HELPERS["_bundled_location_image_path"](str(LOCATIONS_PATH)),
            "",
        )

    def test_missing_user_preview_falls_back_only_to_its_builtin_location(self):
        original_user_dir = HELPERS["get_extension_user_dir"]
        original_locations_path = HELPERS["_locations_data_path"]
        with tempfile.TemporaryDirectory() as root:
            package_data = Path(root, "package", "CR2W", "data")
            user_data = Path(root, "user")
            builtin_preview = package_data / "location_previews" / "builtin.jpg"
            user_preview = user_data / "location_previews" / "custom.jpg"
            builtin_preview.parent.mkdir(parents=True)
            user_preview.parent.mkdir(parents=True)
            builtin_preview.write_bytes(b"builtin")
            user_preview.write_bytes(b"user")
            HELPERS["get_extension_user_dir"] = lambda create=True: str(user_data)
            HELPERS["_locations_data_path"] = lambda: str(package_data / "locations.json")
            try:
                user_overlay = {
                    "name": "Palace",
                    "map": "Toussaint",
                    "image_file": "location_previews/custom.jpg",
                }
                merged = HELPERS["_merge_locations"](
                    [{
                        "name": "Palace",
                        "map": "Toussaint",
                        "image_file": "location_previews/builtin.jpg",
                    }],
                    [user_overlay, dict(user_overlay)],
                )
                self.assertEqual(
                    HELPERS["_location_entry_image_path"](merged[0]),
                    str(user_preview),
                )
                user_preview.unlink()
                self.assertEqual(
                    HELPERS["_location_entry_image_path"](merged[0]),
                    str(builtin_preview),
                )

                standalone_item = {
                    "name": "Standalone",
                    "map": "Toussaint",
                    "image_file": "location_previews/builtin.jpg",
                    "_builtin_image_file": "location_previews/builtin.jpg",
                }
                standalone = HELPERS["_merge_locations"](
                    [],
                    [standalone_item, dict(standalone_item)],
                )[0]
                self.assertNotIn("_builtin_image_file", standalone)
                self.assertEqual(HELPERS["_location_entry_image_path"](standalone), "")
            finally:
                HELPERS["get_extension_user_dir"] = original_user_dir
                HELPERS["_locations_data_path"] = original_locations_path

    def test_location_cache_signature_tracks_package_data_root(self):
        original_locations_path = HELPERS["_locations_data_path"]
        try:
            HELPERS["_locations_data_path"] = lambda: os.path.join("first", "locations.json")
            first, _source = HELPERS["_location_browser_signature"]()
            HELPERS["_locations_data_path"] = lambda: os.path.join("second", "locations.json")
            second, _source = HELPERS["_location_browser_signature"]()
            self.assertNotEqual(first, second)
        finally:
            HELPERS["_locations_data_path"] = original_locations_path

    def test_location_grid_uses_square_thumbnails_and_asset_preview_popup(self):
        tree = ast.parse(BROWSER_PATH.read_text(encoding="utf-8"), filename=str(BROWSER_PATH))
        ensure_icon = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_ensure_entry_icon"
        )
        browser_mixin = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "_JournalBrowserMixin"
        )
        draw = next(
            node for node in browser_mixin.body
            if isinstance(node, ast.FunctionDef) and node.name == "draw"
        )
        ensure_source = ast.unparse(ensure_icon)
        draw_source = ast.unparse(draw)

        self.assertIn("_location_square_thumbnail_path(image_path) or image_path", ensure_source)
        self.assertIn("row.alignment = 'CENTER'", draw_source)
        self.assertIn("witcher.texture_preview", draw_source)
        self.assertIn("preview.file_path = image_path", draw_source)
        self.assertIn("preview.cache_type = 'Workspace'", draw_source)

    def test_malformed_user_file_is_not_overwritten(self):
        original = HELPERS["get_extension_user_dir"]
        with tempfile.TemporaryDirectory() as root:
            HELPERS["get_extension_user_dir"] = lambda create=True: root
            path = Path(root, "user_locations.json")
            path.write_text("{broken", encoding="utf-8")
            try:
                with self.assertRaises(ValueError):
                    HELPERS["_save_user_location"]({
                        "name": "View",
                        "map": "Toussaint",
                        "world_path": r"dlc\bob\bob.w2w",
                        "position": [1, 2, 3],
                    })
                self.assertEqual(path.read_text(encoding="utf-8"), "{broken")
            finally:
                HELPERS["get_extension_user_dir"] = original

    def test_user_location_store_upserts_by_map_and_name(self):
        original = HELPERS["get_extension_user_dir"]
        with tempfile.TemporaryDirectory() as root:
            HELPERS["get_extension_user_dir"] = lambda create=True: root
            try:
                first = {
                    "name": "View",
                    "map": "Toussaint",
                    "world_path": r"dlc\bob\bob.w2w",
                    "position": [1, 2, 3],
                }
                HELPERS["_save_user_location"](first)
                HELPERS["_save_user_location"]({**first, "position": [4, 5, 6]})

                path = Path(root, "user_locations.json")
                data = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(len(data["locations"]), 1)
                self.assertEqual(data["locations"][0]["position"], [4, 5, 6])
                self.assertFalse(Path(str(path) + ".tmp").exists())
            finally:
                HELPERS["get_extension_user_dir"] = original

    def test_open_location_has_exactly_one_global_layer_path(self):
        source = BROWSER_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(BROWSER_PATH))
        function = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_open_location"
        )
        function_source = ast.unparse(function)

        self.assertIn("AddCLayerGroup", function_source)
        self.assertIn("_terrain_tiles_within_radius", function_source)
        self.assertIn("_start_location_stream", function_source)
        self.assertIn("preview_enabled = True", function_source)
        for forbidden in (
            "layer_dir",
            "layer_allow",
            "layer_deny",
            "layer_extra",
            "location_scope",
            "location_viewer",
            "_import_location_layers_sync",
        ):
            self.assertNotIn(forbidden, function_source)

    def test_full_plan_uses_shared_redcloth_and_redapex_resource_resolver(self):
        tree = ast.parse(
            IMPORT_BLENDER_FUN_PATH.read_text(encoding="utf-8"),
            filename=str(IMPORT_BLENDER_FUN_PATH),
        )
        function = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_resolve_gameplay_entity_import_plan"
        )

        self.assertIn(
            "cloth_resource = _chunk_cloth_resource(chunk)",
            ast.unparse(function),
        )

    def test_redapex_resource_resolver_falls_back_to_v164_property(self):
        tree = ast.parse(
            IMPORT_BLENDER_FUN_PATH.read_text(encoding="utf-8"),
            filename=str(IMPORT_BLENDER_FUN_PATH),
        )
        function = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_chunk_cloth_resource"
        )
        namespace = {}
        exec(
            compile(ast.Module(body=[function], type_ignores=[]), str(IMPORT_BLENDER_FUN_PATH), "exec"),
            namespace,
        )
        properties = {
            "resource": SimpleNamespace(Handles=[]),
            "m_resource": SimpleNamespace(
                Handles=[SimpleNamespace(DepotPath=r"environment\decorations\bucket.redapex")]
            ),
        }

        self.assertEqual(
            namespace["_chunk_cloth_resource"](
                SimpleNamespace(GetVariableByName=properties.get)
            ),
            r"environment\decorations\bucket.redapex",
        )

    def test_viewport_and_busy_checks_happen_before_world_mutation(self):
        source = BROWSER_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(BROWSER_PATH))
        function = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_open_location"
        )
        busy_check = next(
            node for node in ast.walk(function)
            if isinstance(node, ast.If)
            and "layer_stream_job_running" in ast.unparse(node.test)
        )
        viewport_check = next(
            node for node in ast.walk(function)
            if isinstance(node, ast.If)
            and "_find_view3d_area" in ast.unparse(node.test)
        )
        mutation = next(
            node for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "ensure_world_terrain_collection"
        )
        self.assertLess(busy_check.lineno, mutation.lineno)
        self.assertLess(viewport_check.lineno, mutation.lineno)

    def test_each_intersected_tile_gets_terrain_and_foliage(self):
        source = BROWSER_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(BROWSER_PATH))
        function = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_open_location"
        )
        terrain_loop = next(
            node for node in ast.walk(function)
            if isinstance(node, ast.For)
            and "terrain_tiles" in ast.unparse(node.iter)
            and "import_world_terrain_tile" in ast.unparse(node)
        )
        foliage_call = next(
            node for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_schedule_location_foliage"
        )
        self.assertIn("terrain_tiles", ast.unparse(foliage_call))
        self.assertLess(terrain_loop.lineno, foliage_call.lineno)

    def test_stream_invokes_the_normal_load_around_camera_operator(self):
        source = BROWSER_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(BROWSER_PATH))
        function = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_start_location_stream"
        )
        call = next(
            node for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "load_layers_around_camera"
        )
        self.assertEqual(call.keywords, [])
        self.assertNotIn("location_viewer", UI_MAP_PATH.read_text(encoding="utf-8"))

    def test_location_save_captures_preview_then_writes_user_json(self):
        source = BROWSER_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(BROWSER_PATH))
        operator = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "MyLocationSaveOperator"
        )
        execute = next(
            node for node in operator.body
            if isinstance(node, ast.FunctionDef) and node.name == "execute"
        )
        calls = {
            node.func.id: node.lineno
            for node in ast.walk(execute)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in {"_capture_location_preview", "_save_user_location"}
        }
        self.assertLess(calls["_capture_location_preview"], calls["_save_user_location"])
        self.assertNotIn("layer_dir", ast.unparse(operator))


if __name__ == "__main__":
    unittest.main()
