import ast
import fnmatch
import hashlib
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
BROWSER_PATH = ROOT / "witcher3_tools" / "w3_asset_browser.py"
LOCATIONS_PATH = ROOT / "witcher3_tools" / "CR2W" / "data" / "locations.json"


def _load_policy_functions():
    source = BROWSER_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(BROWSER_PATH))
    wanted = {
        "_normalize_depot_path",
        "_location_layer_patterns",
        "_location_layer_relative_path",
        "_location_layer_path_allowed",
        "_location_scope_id",
        "_location_scope_for_full_load",
        "_terrain_tile_from_world_position",
        "_location_anchor_positions_from_items",
        "_location_dense_position",
    }
    nodes = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]
    module = ast.Module(body=nodes, type_ignores=[])
    terrain_spec = importlib.util.spec_from_file_location(
        "terrain_core_for_location_tests",
        ROOT / "witcher3_tools" / "terrain_core.py",
    )
    terrain_core = importlib.util.module_from_spec(terrain_spec)
    sys.modules[terrain_spec.name] = terrain_core
    terrain_spec.loader.exec_module(terrain_core)
    namespace = {
        "fnmatch": fnmatch,
        "hashlib": hashlib,
        "json": json,
        "_grid_tile_from_world_position": terrain_core.terrain_tile_from_world_position,
    }
    exec(compile(module, str(BROWSER_PATH), "exec"), namespace)
    return namespace


POLICY = _load_policy_functions()
is_allowed = POLICY["_location_layer_path_allowed"]
scope_id = POLICY["_location_scope_id"]
scope_for_full_load = POLICY["_location_scope_for_full_load"]
terrain_tile_from_position = POLICY["_terrain_tile_from_world_position"]
location_anchor_positions = POLICY["_location_anchor_positions_from_items"]
location_dense_position = POLICY["_location_dense_position"]


class LocationPresetPolicyTests(unittest.TestCase):
    @staticmethod
    def _terrain_spec():
        return SimpleNamespace(
            terrain_size=2048.0,
            x_tiles=32,
            y_tiles=32,
        )

    def test_location_density_selects_tile_with_most_entity_anchors(self):
        spec = self._terrain_spec()
        dense = [
            (101.0, 202.0, 4.0),
            (103.0, 204.0, 6.0),
            (105.0, 206.0, 8.0),
            (107.0, 208.0, 10.0),
        ]
        outliers = [(-900.0, -900.0, 0.0), (900.0, 900.0, 0.0)]

        position = location_dense_position(dense + outliers, spec)

        self.assertEqual(
            terrain_tile_from_position(spec, position),
            terrain_tile_from_position(spec, dense[0]),
        )
        self.assertEqual(position, (104.0, 205.0, 7.0))

    def test_every_curated_location_has_a_provisional_position(self):
        data = json.loads(LOCATIONS_PATH.read_text(encoding="utf-8"))
        locations = data["locations"]
        missing = [item["name"] for item in locations if len(item.get("position") or ()) < 3]
        self.assertEqual(missing, [])
        for item in locations:
            with self.subTest(location=item["name"]):
                self.assertTrue(all(
                    isinstance(value, (int, float))
                    for value in item["position"][:3]
                ))

    def test_location_density_ignores_entity_component_multiplication(self):
        items = [
            {
                "id": "entity_1",
                "kind": "entity",
                "parent_id": "",
                "world_position": [100.0, 200.0, 5.0],
            },
            {
                "id": "mesh_1",
                "kind": "mesh",
                "parent_id": "entity_1",
                "world_position": [100.0, 200.0, 5.0],
            },
            {
                "id": "mesh_2",
                "kind": "mesh",
                "parent_id": "entity_1",
                "world_position": [100.0, 200.0, 5.0],
            },
            {
                "id": "nested_entity",
                "kind": "entity",
                "parent_id": "entity_1",
                "world_position": [100.0, 200.0, 5.0],
            },
            {
                "id": "sector_1",
                "kind": "mesh",
                "parent_id": "",
                "world_position": [110.0, 210.0, 5.0],
            },
        ]

        self.assertEqual(
            location_anchor_positions(items),
            [(100.0, 200.0, 5.0), (110.0, 210.0, 5.0)],
        )

    def test_full_load_never_reuses_filtered_viewer_scope(self):
        directory = r"levels\novigrad\location"
        viewer = {
            "witcher_location_scope": True,
            "witcher_location_scope_id": scope_id(directory, (), ["volume.w2l"]),
            "witcher_location_layer_dir": directory,
        }
        full = {
            "witcher_location_scope": True,
            "witcher_location_scope_id": scope_id(directory),
            "witcher_location_layer_dir": directory,
        }

        class Root:
            children = [viewer]

        self.assertIsNone(scope_for_full_load(Root(), directory))
        Root.children = [viewer, full]
        self.assertIs(scope_for_full_load(Root(), directory), full)

    def test_keira_viewer_policy_denies_only_pocket_interior(self):
        data = json.loads(LOCATIONS_PATH.read_text(encoding="utf-8"))
        keira = next(item for item in data["locations"] if item["name"] == "Keira Metz's Hut")
        self.assertNotIn("layer_allow", keira)
        self.assertEqual(keira["layer_deny"], ["secret_room\\*", "volume.w2l"])
        directory = keira["layer_dir"]
        deny = keira["layer_deny"]
        for visible in (
            r"hut.w2l",
            r"cellar.w2l",
            r"environment.w2l",
            r"decoration\deco.w2l",
            r"decoration\deco_ns.w2l",
            r"path_to_keira\assets.w2l",
        ):
            self.assertTrue(is_allowed(directory + "\\" + visible, directory, layer_deny=deny))
        for hidden in (r"secret_room\secret_room.w2l", r"secret_room\skydome.w2l", r"volume.w2l"):
            self.assertFalse(is_allowed(directory + "\\" + hidden, directory, layer_deny=deny))

    def test_allow_patterns_are_relative_to_location_directory(self):
        directory = r"levels\novigrad\nml_villages\keira_metz_house"
        allow = [r"hut.w2l", r"path_to_keira\assets.w2l"]
        self.assertTrue(is_allowed(directory + r"\hut.w2l", directory, layer_allow=allow))
        self.assertTrue(is_allowed(directory + r"\path_to_keira\assets.w2l", directory, layer_allow=allow))
        self.assertFalse(is_allowed(directory + r"\secret_room\secret_room.w2l", directory, layer_allow=allow))

    def test_deny_patterns_override_allow_patterns(self):
        directory = r"levels\novigrad\location"
        self.assertFalse(
            is_allowed(
                directory + r"\secret_room\terrain.w2l",
                directory,
                layer_allow=[r"*.w2l"],
                layer_deny=[r"secret_room\*"],
            )
        )

    def test_empty_policy_preserves_full_layer_behavior(self):
        self.assertTrue(
            is_allowed(
                r"levels\novigrad\location\anything.w2l",
                r"levels\novigrad\location",
            )
        )

    def test_scope_cache_identity_changes_with_policy(self):
        directory = r"levels\novigrad\location"
        viewer = scope_id(directory, ["hut.w2l"], [])
        full = scope_id(directory, [], [])
        self.assertNotEqual(viewer, full)
        self.assertEqual(viewer, scope_id(directory.upper(), ["HUT.W2L"], []))

    def test_scope_cache_identity_includes_shared_layers(self):
        directory = r"levels\novigrad\novigrad\passiflora"
        base = scope_id(directory)
        with_doors = scope_id(
            directory,
            layer_extra=[r"levels\novigrad\doors.w2l"],
        )
        self.assertNotEqual(base, with_doors)

    def test_passiflora_includes_global_interactive_door_layer(self):
        data = json.loads(LOCATIONS_PATH.read_text(encoding="utf-8"))
        passiflora = next(item for item in data["locations"] if item["name"] == "Passiflora")
        self.assertEqual(passiflora["layer_extra"], [r"levels\novigrad\doors.w2l"])

    def test_background_import_exits_before_viewport_access(self):
        source = BROWSER_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(BROWSER_PATH))
        function = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_start_location_stream"
        )
        background_guard = next(
            node for node in function.body
            if isinstance(node, ast.If)
            and "bpy.app" in ast.unparse(node.test)
            and "background" in ast.unparse(node.test)
        )
        self.assertTrue(
            any(
                isinstance(node, ast.Return)
                and isinstance(node.value, ast.Constant)
                and node.value.value is False
                for node in background_guard.body
            )
        )
        viewport_access = next(
            node for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_find_view3d_area"
        )
        self.assertLess(background_guard.lineno, viewport_access.lineno)

    def test_busy_job_cancels_before_location_world_mutation(self):
        source = BROWSER_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(BROWSER_PATH))
        function = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_open_location"
        )
        busy_guard = next(
            node for node in function.body
            if isinstance(node, ast.If)
            and "layer_stream_job_running" in ast.unparse(node.test)
            and "foliage_busy" in ast.unparse(node.test)
        )
        self.assertTrue(
            any(
                isinstance(node, ast.Return)
                and "CANCELLED" in ast.unparse(node)
                for node in busy_guard.body
            )
        )
        first_world_mutation = next(
            node for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "ensure_world_terrain_collection"
        )
        self.assertLess(busy_guard.lineno, first_world_mutation.lineno)

    def test_location_loads_parent_world_water_and_environment(self):
        source = BROWSER_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(BROWSER_PATH))
        function = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_open_location"
        )
        calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)]

        load_world = next(
            node for node in calls
            if isinstance(node.func, ast.Attribute) and node.func.attr == "load_w2w"
        )
        ensure_water = next(
            node for node in calls
            if isinstance(node.func, ast.Attribute)
            and node.func.attr == "_ensure_world_water_plane"
        )
        sync_environment = next(
            node for node in calls
            if isinstance(node.func, ast.Attribute)
            and node.func.attr == "sync_world_import"
        )
        import_tile = next(
            node for node in calls
            if isinstance(node.func, ast.Attribute)
            and node.func.attr == "import_world_terrain_tile"
        )

        self.assertLess(load_world.lineno, ensure_water.lineno)
        self.assertLess(ensure_water.lineno, sync_environment.lineno)
        self.assertLess(sync_environment.lineno, import_tile.lineno)
        self.assertIn("world_key", {keyword.arg for keyword in ensure_water.keywords})

    def test_stream_start_race_does_not_request_sync_fallback(self):
        source = BROWSER_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(BROWSER_PATH))
        function = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_start_location_stream"
        )
        busy_guard = next(
            node for node in function.body
            if isinstance(node, ast.If)
            and "layer_stream_job_running" in ast.unparse(node.test)
            and "foliage_busy" in ast.unparse(node.test)
        )
        self.assertTrue(
            any(
                isinstance(node, ast.Return)
                and (
                    node.value is None
                    or (
                        isinstance(node.value, ast.Constant)
                        and node.value.value is None
                    )
                )
                for node in busy_guard.body
            )
        )

    def test_location_refresh_invalidates_discovered_layer_paths(self):
        source = BROWSER_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(BROWSER_PATH))
        function = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_load_location_entries_cached"
        )
        refresh_guard = next(
            node for node in function.body
            if isinstance(node, ast.If)
            and "force_refresh" in ast.unparse(node.test)
        )
        self.assertTrue(
            any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "_LOCATION_LAYER_PATH_CACHE"
                and node.func.attr == "clear"
                for node in ast.walk(refresh_guard)
            )
        )

    def test_full_load_checks_for_an_existing_full_scope(self):
        source = BROWSER_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(BROWSER_PATH))
        function = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_open_location"
        )
        calls = [
            node for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_location_scope_for_full_load"
        ]
        self.assertEqual(len(calls), 1)
        self.assertIn("load_full_layers", ast.unparse(function))

    def test_synchronous_fallback_receives_shared_layers(self):
        source = BROWSER_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(BROWSER_PATH))
        sync_function = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_import_location_layers_sync"
        )
        self.assertIn("layer_extra", [arg.arg for arg in sync_function.args.kwonlyargs])
        self.assertIn("extra_paths", ast.unparse(sync_function))

        open_function = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_open_location"
        )
        fallback_calls = [
            node for node in ast.walk(open_function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_import_location_layers_sync"
        ]
        self.assertEqual(len(fallback_calls), 1)
        self.assertIn("layer_extra", {keyword.arg for keyword in fallback_calls[0].keywords})


if __name__ == "__main__":
    unittest.main()
