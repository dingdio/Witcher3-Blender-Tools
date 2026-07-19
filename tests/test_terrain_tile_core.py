import importlib.util
import os
import re
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from pathlib import Path
from unittest import mock

import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
TARGET_PATH = REPO_ROOT / "witcher3_tools" / "importers" / "import_w2w.py"


def _load_import_w2w_with_blender_stubs():
    root_pkg = types.ModuleType("witcher3_tools")
    root_pkg.__path__ = [str(REPO_ROOT / "witcher3_tools")]
    root_pkg.__package__ = "witcher3_tools"
    root_pkg.get_uncook_path = lambda context=None: ""
    root_pkg.get_fbx_uncook_path = lambda context=None: ""
    root_pkg.get_all_addon_prefs = lambda context=None: types.SimpleNamespace()

    importers_pkg = types.ModuleType("witcher3_tools.importers")
    importers_pkg.__path__ = [str(REPO_ROOT / "witcher3_tools" / "importers")]
    importers_pkg.__package__ = "witcher3_tools.importers"

    cr2w_pkg = types.ModuleType("witcher3_tools.CR2W")
    cr2w_pkg.__path__ = [str(REPO_ROOT / "witcher3_tools" / "CR2W")]
    cr2w_pkg.__package__ = "witcher3_tools.CR2W"

    bpy = types.ModuleType("bpy")
    bpy_types = types.ModuleType("bpy.types")
    for name in ("PropertyGroup", "Operator", "UIList", "Panel", "Mesh", "Scene"):
        setattr(bpy_types, name, type(name, (), {}))
    bpy_props = types.ModuleType("bpy.props")

    def prop_stub(*args, **kwargs):
        return None

    for name in (
        "CollectionProperty",
        "IntProperty",
        "BoolProperty",
        "StringProperty",
        "PointerProperty",
    ):
        setattr(bpy_props, name, prop_stub)
    bpy_utils = types.ModuleType("bpy.utils")
    bpy_utils.register_class = prop_stub
    bpy_utils.unregister_class = prop_stub
    bpy.types = bpy_types
    bpy.props = bpy_props
    bpy.utils = bpy_utils
    bpy.context = types.SimpleNamespace()

    import_texarray = types.ModuleType("witcher3_tools.importers.import_texarray")
    import_texarray.insert_color = prop_stub
    import_texarray.get_texture_node = prop_stub
    import_texarray.insert_heightmap_to_disp = prop_stub

    terrain_w2ter = types.ModuleType("witcher3_tools.importers.terrain_w2ter")
    terrain_w2ter.W2TER_BUFFER_RE = re.compile(
        r"\.w2ter\.(\d+)\.buffer$", re.IGNORECASE
    )
    terrain_w2ter.TileInfo = type("TileInfo", (), {})
    importers_pkg.terrain_w2ter = terrain_w2ter

    cr2w_file = types.ModuleType("witcher3_tools.CR2W.CR2W_file")
    cr2w_file.WORLD = type("WORLD", (), {})
    cr2w_file.read_CR2W = prop_stub

    common_blender = types.ModuleType("witcher3_tools.CR2W.common_blender")
    common_blender.repo_file = lambda path: ""
    common_blender.bpy_image_load_safe = prop_stub
    common_blender.redkit_repo_context = prop_stub
    common_blender.win_safe_path = lambda path: path

    import_w2l = types.ModuleType("witcher3_tools.importers.import_w2l")
    importers_pkg.import_w2l = import_w2l

    yaml_module = types.ModuleType("yaml")
    yaml_module.full_load = prop_stub
    third_party = types.ModuleType("witcher3_tools.CR2W.third_party_libs")
    third_party.yaml = yaml_module

    extension_paths = types.ModuleType("witcher3_tools.extension_paths")
    extension_paths.get_dev_override = lambda *args, **kwargs: ""
    extension_paths.get_redkit_working_root = lambda *args, **kwargs: ""

    stubs = {
        "witcher3_tools": root_pkg,
        "witcher3_tools.importers": importers_pkg,
        "witcher3_tools.CR2W": cr2w_pkg,
        "bpy": bpy,
        "bpy.types": bpy_types,
        "bpy.props": bpy_props,
        "bpy.utils": bpy_utils,
        "witcher3_tools.importers.import_texarray": import_texarray,
        "witcher3_tools.importers.terrain_w2ter": terrain_w2ter,
        "witcher3_tools.CR2W.CR2W_file": cr2w_file,
        "witcher3_tools.CR2W.common_blender": common_blender,
        "witcher3_tools.importers.import_w2l": import_w2l,
        "witcher3_tools.CR2W.third_party_libs": third_party,
        "witcher3_tools.extension_paths": extension_paths,
    }
    module_name = "witcher3_tools.importers._terrain_tile_test_target"
    spec = importlib.util.spec_from_file_location(module_name, TARGET_PATH)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, stubs):
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    return module


terrain = _load_import_w2w_with_blender_stubs()


def _novigrad_spec():
    return terrain.TerrainWorldSpec(
        hub_name="novigrad",
        world_name="Novigrad",
        world_path=r"levels\novigrad\novigrad.w2w",
        world_key="novigrad-test",
        terrain_size=2048.0,
        lowest_elevation=-100.0,
        highest_elevation=300.0,
        tile_res=512,
        x_tiles=32,
        y_tiles=32,
        terrain_tiles_dir=r"C:\uncook\levels\novigrad\terrain_tiles",
        terrain_tiles_rel=r"levels\novigrad\terrain_tiles",
        working_tiles_dir=r"C:\work\levels\novigrad\terrain_tiles",
    )


class TestTerrainLodPolicy(unittest.TestCase):
    def test_native_level_and_window_follow_w2w_resolution(self):
        self.assertEqual(terrain.terrain_native_level(256), 8)
        self.assertEqual(terrain.terrain_native_level(512), 9)
        self.assertEqual(terrain._clamp_tile_multires_level(9, 512), 9)

        center = terrain.terrain_view_lod_tiles(46, 46, 23, 23, 3)
        edge = terrain.terrain_view_lod_tiles(46, 46, 0, 0, 3)
        moved = terrain.terrain_view_lod_tiles(46, 46, 24, 23, 3)
        self.assertEqual(len(center), 49)
        self.assertEqual(
            (min(x for x, _y in center), max(x for x, _y in center)),
            (20, 26),
        )
        self.assertEqual(len(edge), 16)
        self.assertEqual((len(moved - center), len(center - moved)), (7, 7))

    def test_novigrad_lod_budget_is_below_three_percent_of_all_native(self):
        native = terrain.terrain_native_level(512)
        overview = min(5, native)
        lod_vertices = 49 * ((1 << native) + 1) ** 2
        lod_vertices += (46 * 46 - 49) * ((1 << overview) + 1) ** 2
        lod_vertices += 49 * 4 * ((1 << native) + 1)
        lod_vertices += (46 * 46 - 49) * 4 * ((1 << overview) + 1)
        all_native = 46 * 46 * ((1 << native) + 1) ** 2
        self.assertEqual(lod_vertices, 15_519_636)
        self.assertLess(lod_vertices / all_native, 0.03)

    def test_w2w_clipmap_declares_grid_without_scanning_tiles(self):
        world = types.SimpleNamespace(tileRes=512, clipmapSize=23552, clipSize=0)
        with tempfile.TemporaryDirectory() as tmp:
            world_path = Path(tmp) / "novigrad.w2w"
            world_path.write_bytes(b"w2w")
            with (
                mock.patch.object(terrain, "get_uncook_path", return_value=""),
                mock.patch.object(terrain, "_configured_redkit_workspace_roots", return_value=[]),
                mock.patch.object(terrain, "_configured_redkit_roots", return_value=[]),
                mock.patch.object(terrain, "_discover_tile_count") as discover,
            ):
                context = terrain._resolve_terrain_context(
                    world, str(world_path), discover_tiles=False)

        self.assertEqual((context["tile_res"], context["n_tiles"]), (512, 46))
        discover.assert_not_called()


class TestSelectedTerrainTileBounds(unittest.TestCase):
    def test_corner_tile_has_centered_half_open_bounds_and_elevation(self):
        bounds = terrain.terrain_tile_bounds(_novigrad_spec(), 0, 0)

        self.assertEqual(bounds.key, terrain.TerrainTileKey(0, 0))
        self.assertEqual(bounds.world_y, 0)
        self.assertEqual(
            (bounds.min_x, bounds.min_y, bounds.max_x, bounds.max_y),
            (-1024.0, -1024.0, -960.0, -960.0),
        )
        self.assertEqual((bounds.min_z, bounds.max_z), (-100.0, 300.0))

    def test_adjacent_tiles_assign_shared_edge_to_positive_tile_only(self):
        spec = _novigrad_spec()
        left = terrain.terrain_tile_bounds(spec, 7, 9)
        right = terrain.terrain_tile_bounds(spec, 8, 9)

        self.assertEqual(left.max_x, right.min_x)
        self.assertFalse(left.contains_xy(left.max_x, left.center_y))
        self.assertTrue(right.contains_xy(left.max_x, left.center_y))

    def test_invalid_tile_fails_before_any_resolution(self):
        with mock.patch.object(terrain, "_resolve_tile_buffer") as resolver:
            with self.assertRaises(ValueError):
                terrain.resolve_world_terrain_tile_source(_novigrad_spec(), 32, 0)
        resolver.assert_not_called()


class TestWorkspaceTerrainContext(unittest.TestCase):
    def test_generated_terrain_image_reloads_when_file_stamp_changes(self):
        class FakeImage(dict):
            def __init__(self):
                super().__init__()
                self.reload_count = 0
                self.colorspace_settings = types.SimpleNamespace(name="")

            def reload(self):
                self.reload_count += 1

        image = FakeImage()
        first = types.SimpleNamespace(st_mtime_ns=10, st_size=20)
        changed = types.SimpleNamespace(st_mtime_ns=11, st_size=20)
        with (
            mock.patch.object(terrain, "bpy_image_load_safe", return_value=image),
            mock.patch.object(terrain.os, "stat", side_effect=[first, first, changed]),
        ):
            terrain._load_or_reload_terrain_image("terrain.png", "Non-Color")
            terrain._load_or_reload_terrain_image("terrain.png", "Non-Color")
            terrain._load_or_reload_terrain_image("terrain.png", "Non-Color")

        self.assertEqual(image.reload_count, 2)
        self.assertEqual(image.colorspace_settings.name, "Non-Color")

    def test_embedded_buffer_fallback_keeps_w2ter_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tiles = root / "terrain_tiles"
            working = root / "working"
            tiles.mkdir()
            source_tile = tiles / "tile_8_x_8_res256.w2ter"
            source_tile.write_bytes(b"tile")
            buffer_name = "tile_8_x_8_res256.w2ter.1.buffer"
            materialized = working / buffer_name

            with mock.patch.object(
                terrain,
                "_materialize_w2ter_embedded_buffers",
                return_value=[str(materialized)],
            ) as extract:
                resolved = terrain._resolve_tile_buffer(
                    str(tiles), None, buffer_name, str(working))

            self.assertEqual(resolved, str(materialized))
            extract.assert_called_once_with(str(source_tile), str(working))

    def test_workspace_container_wins_over_existing_working_buffer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tiles = root / "terrain_tiles"
            working = root / "working"
            tiles.mkdir()
            working.mkdir()
            source_tile = tiles / "tile_8_x_8_res256.w2ter"
            source_tile.write_bytes(b"workspace")
            buffer_name = "tile_8_x_8_res256.w2ter.1.buffer"
            working_buffer = working / buffer_name
            working_buffer.write_bytes(b"stale")

            with mock.patch.object(
                terrain,
                "_materialize_w2ter_embedded_buffers",
                return_value=[str(working_buffer)],
            ) as extract:
                resolved = terrain._resolve_tile_buffer(
                    str(tiles), r"levels\test\terrain_tiles", buffer_name,
                    str(working))

            self.assertEqual(resolved, str(working_buffer))
            extract.assert_called_once_with(str(source_tile), str(working))

    def test_workspace_decode_failure_does_not_fall_back_to_depot_or_stale_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tiles = root / "terrain_tiles"
            working = root / "working"
            tiles.mkdir()
            working.mkdir()
            (tiles / "tile_8_x_8_res256.w2ter").write_bytes(b"workspace")
            buffer_name = "tile_8_x_8_res256.w2ter.1.buffer"
            (working / buffer_name).write_bytes(b"stale")

            with (
                mock.patch.object(
                    terrain, "_materialize_w2ter_embedded_buffers", return_value=[]),
                mock.patch.object(terrain, "repo_file") as repo,
            ):
                resolved = terrain._resolve_tile_buffer(
                    str(tiles), r"levels\test\terrain_tiles", buffer_name,
                    str(working))

            self.assertIsNone(resolved)
            repo.assert_not_called()

    def test_materializer_replaces_same_size_newer_stale_buffer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_tile = root / "tile_8_x_8_res256.w2ter"
            working = root / "working"
            working.mkdir()
            source_tile.write_bytes(b"container")
            source_mtime = os.path.getmtime(source_tile)
            output = working / "tile_8_x_8_res256.w2ter.1.buffer"
            output.write_bytes(b"stale")
            os.utime(output, (source_mtime + 60.0, source_mtime + 60.0))
            cr2w = types.SimpleNamespace(
                BufferData=[b"fresh"],
                CR2WBuffer=[types.SimpleNamespace(index=1)],
            )
            terrain._MATERIALIZED_W2TER_BUFFER_CACHE.clear()

            with mock.patch.object(terrain, "read_CR2W", return_value=cr2w):
                outputs = terrain._materialize_w2ter_embedded_buffers(
                    str(source_tile), str(working))

            self.assertEqual(outputs, [str(output)])
            self.assertEqual(output.read_bytes(), b"fresh")
            self.assertGreater(os.path.getmtime(output), source_mtime)
            terrain._MATERIALIZED_W2TER_BUFFER_CACHE.clear()

    def test_direct_workspace_world_enables_embedded_tile_working_storage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "project" / "workspace"
            world_dir = workspace / "levels" / "test_world"
            world_dir.mkdir(parents=True)
            world_path = world_dir / "test_world.w2w"
            world_path.write_bytes(b"world")
            cache_root = root / "cache"
            world = types.SimpleNamespace(tileRes=256, clipmapSize=4096, clipSize=0)

            with (
                mock.patch.object(terrain, "get_uncook_path", return_value=""),
                mock.patch.object(terrain, "_configured_redkit_roots", return_value=[]),
                mock.patch.object(
                    terrain, "_configured_redkit_workspace_roots", return_value=[]),
                mock.patch.object(
                    terrain, "get_redkit_working_root", return_value=str(cache_root)),
            ):
                context = terrain._resolve_terrain_context(
                    world, str(world_path), discover_tiles=False)

            expected_rel = os.path.join("levels", "test_world", "terrain_tiles")
            self.assertEqual(context["terrain_tiles_rel"], expected_rel)
            workspace_cache = (
                cache_root
                / "workspaces"
                / terrain._workspace_cache_namespace(str(workspace))
            )
            self.assertEqual(
                Path(context["working_tiles_dir"]), workspace_cache / expected_rel)

    def test_different_projects_get_distinct_workspace_cache_namespaces(self):
        first = terrain._workspace_cache_namespace(
            r"C:\projects\first\workspace")
        second = terrain._workspace_cache_namespace(
            r"C:\projects\second\workspace")

        self.assertNotEqual(first, second)

    def test_configured_project_contributes_its_workspace_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            workspace = project / "workspace"
            workspace.mkdir(parents=True)
            prefs = types.SimpleNamespace(
                redkit_projects=[types.SimpleNamespace(path=str(project))])

            with mock.patch.object(
                terrain, "get_all_addon_prefs", return_value=prefs):
                roots = terrain._configured_redkit_workspace_roots()

            self.assertEqual([Path(path) for path in roots], [workspace])


class TestBoundedTerrainTileResolver(unittest.TestCase):
    def test_height_only_request_resolves_exactly_one_named_buffer(self):
        calls = []

        def resolve(_tiles_dir, _tiles_rel, buffer_name, working_tiles_dir=None):
            calls.append((buffer_name, working_tiles_dir))
            return "C:\\resolved\\" + buffer_name

        with mock.patch.object(terrain, "_resolve_tile_buffer", side_effect=resolve):
            source = terrain.resolve_world_terrain_tile_source(
                _novigrad_spec(), 2, 3, include_overlay=False
            )

        expected = "tile_3_x_2_res512.w2ter.1.buffer"
        self.assertEqual(calls, [(expected, r"C:\work\levels\novigrad\terrain_tiles")])
        self.assertEqual(source.key, terrain.TerrainTileKey(2, 3))
        self.assertTrue(source.available)
        self.assertTrue(source.heightmap_buffer.endswith(expected))
        self.assertEqual(source.texture_buffer, "")
        self.assertEqual(source.overlay_path, "")

    def test_overlay_request_is_bounded_to_two_buffers_of_selected_tile(self):
        names = []

        def resolve(_tiles_dir, _tiles_rel, buffer_name, working_tiles_dir=None):
            names.append(buffer_name)
            return "C:\\resolved\\" + buffer_name

        with mock.patch.object(terrain, "_resolve_tile_buffer", side_effect=resolve):
            source = terrain.resolve_world_terrain_tile_source(
                _novigrad_spec(), 17, 21, include_overlay=True
            )

        prefix = "tile_21_x_17_res512.w2ter"
        self.assertEqual(
            names,
            [f"{prefix}.1.buffer", f"{prefix}.2.buffer"],
        )
        self.assertTrue(source.heightmap_buffer.endswith(f"{prefix}.1.buffer"))
        self.assertTrue(source.texture_buffer.endswith(f"{prefix}.2.buffer"))
        self.assertEqual(source.overlay_path, source.texture_buffer + ".overlay.png")

    def test_stitch_request_resolves_only_positive_axis_neighbors(self):
        names = []

        def resolve(_tiles_dir, _tiles_rel, buffer_name, working_tiles_dir=None):
            names.append(buffer_name)
            return "C:\\resolved\\" + buffer_name

        with mock.patch.object(terrain, "_resolve_tile_buffer", side_effect=resolve):
            source = terrain.resolve_world_terrain_tile_source(
                _novigrad_spec(),
                2,
                3,
                include_overlay=False,
                include_stitch_neighbors=True,
            )

        expected = {
            "current": "tile_3_x_2_res512.w2ter.1.buffer",
            "right": "tile_3_x_3_res512.w2ter.1.buffer",
            "up": "tile_4_x_2_res512.w2ter.1.buffer",
            "diagonal": "tile_4_x_3_res512.w2ter.1.buffer",
        }
        self.assertCountEqual(names, expected.values())
        self.assertTrue(source.heightmap_buffer.endswith(expected["current"]))
        self.assertTrue(source.positive_x_buffer_path.endswith(expected["right"]))
        self.assertTrue(source.positive_y_buffer_path.endswith(expected["up"]))
        self.assertTrue(source.positive_xy_buffer_path.endswith(expected["diagonal"]))

    def test_detail_stitch_request_resolves_positive_texture_neighbors(self):
        names = []

        def resolve(_tiles_dir, _tiles_rel, buffer_name, working_tiles_dir=None):
            names.append(buffer_name)
            return "C:\\resolved\\" + buffer_name

        with mock.patch.object(terrain, "_resolve_tile_buffer", side_effect=resolve):
            source = terrain.resolve_world_terrain_tile_source(
                _novigrad_spec(),
                2,
                3,
                include_overlay=True,
                include_stitch_neighbors=True,
            )

        for y, x in ((3, 2), (3, 3), (4, 2), (4, 3)):
            self.assertIn(f"tile_{y}_x_{x}_res512.w2ter.1.buffer", names)
            self.assertIn(f"tile_{y}_x_{x}_res512.w2ter.2.buffer", names)
        self.assertTrue(source.positive_x_texture_buffer_path.endswith(
            "tile_3_x_3_res512.w2ter.2.buffer"))
        self.assertTrue(source.positive_y_texture_buffer_path.endswith(
            "tile_4_x_2_res512.w2ter.2.buffer"))
        self.assertTrue(source.positive_xy_texture_buffer_path.endswith(
            "tile_4_x_3_res512.w2ter.2.buffer"))

    def test_outer_corner_stitch_request_does_not_resolve_out_of_bounds_tiles(self):
        names = []

        def resolve(_tiles_dir, _tiles_rel, buffer_name, working_tiles_dir=None):
            names.append(buffer_name)
            return "C:\\resolved\\" + buffer_name

        with mock.patch.object(terrain, "_resolve_tile_buffer", side_effect=resolve):
            source = terrain.resolve_world_terrain_tile_source(
                _novigrad_spec(),
                31,
                31,
                include_overlay=False,
                include_stitch_neighbors=True,
            )

        self.assertEqual(names, ["tile_31_x_31_res512.w2ter.1.buffer"])
        self.assertEqual(source.positive_x_buffer_path, "")
        self.assertEqual(source.positive_y_buffer_path, "")
        self.assertEqual(source.positive_xy_buffer_path, "")


class TestTerrainGridTopology(unittest.TestCase):
    def test_cached_grid_contains_complete_faces_and_uvs(self):
        loop_vertices, loop_starts, loop_totals, loop_uv = terrain._terrain_grid_topology(3)

        self.assertEqual(loop_vertices.shape, (16,))
        self.assertEqual(loop_starts.tolist(), [0, 4, 8, 12])
        self.assertEqual(loop_totals.tolist(), [4, 4, 4, 4])
        self.assertEqual(loop_uv.shape, (16, 2))
        self.assertIs(
            terrain._terrain_grid_topology(3)[0],
            loop_vertices,
        )

    def test_elevation_range_uses_high_minus_low_for_positive_worlds(self):
        spec = replace(
            _novigrad_spec(),
            lowest_elevation=100.0,
            highest_elevation=300.0,
        )
        self.assertEqual(terrain._terrain_elevation_range(spec), 200.0)

    def test_import_signature_includes_grid_dimensions(self):
        spec = _novigrad_spec()
        bounds = terrain.terrain_tile_bounds(spec, 2, 3)
        source = terrain.TerrainTileSource(
            terrain.TerrainTileSourceRequest(terrain.TerrainTileKey(2, 3)),
            "tile_3_x_2_res512.w2ter",
            r"C:\resolved\tile.buffer",
        )
        signature = terrain._tile_import_signature(spec, source, bounds, 5)
        resized = replace(spec, x_tiles=16, y_tiles=64)
        resized_bounds = terrain.terrain_tile_bounds(resized, 2, 3)
        self.assertNotEqual(
            signature,
            terrain._tile_import_signature(resized, source, resized_bounds, 5),
        )

    def test_import_signature_includes_every_stitch_neighbor(self):
        spec = _novigrad_spec()
        bounds = terrain.terrain_tile_bounds(spec, 2, 3)
        source = terrain.TerrainTileSource(
            terrain.TerrainTileSourceRequest(terrain.TerrainTileKey(2, 3)),
            "tile_3_x_2_res512.w2ter",
            r"C:\resolved\current.buffer",
            positive_x_buffer_path=r"C:\resolved\right.buffer",
            positive_y_buffer_path=r"C:\resolved\up.buffer",
            positive_xy_buffer_path=r"C:\resolved\diagonal.buffer",
        )

        with mock.patch.object(terrain, "_source_file_stamp", side_effect=lambda path: path):
            signature = terrain._tile_import_signature(spec, source, bounds, 5)
            for field_name in (
                "positive_x_buffer_path",
                "positive_y_buffer_path",
                "positive_xy_buffer_path",
            ):
                changed = replace(source, **{field_name: getattr(source, field_name) + ".new"})
                self.assertNotEqual(
                    signature,
                    terrain._tile_import_signature(spec, changed, bounds, 5),
                    field_name,
                )


class TestTerrainTileHeightmapStitching(unittest.TestCase):
    def test_full_resolution_sampling_is_inclusive_without_duplicates(self):
        indices = terrain._terrain_tile_sample_indices(256, 257)

        self.assertEqual(indices[0], 0)
        self.assertEqual(indices[128], 128)
        self.assertEqual(indices[-1], 256)
        self.assertEqual(indices.size, 257)
        self.assertEqual(np.unique(indices).size, 257)

    def test_preview_sampling_includes_both_stitched_boundaries(self):
        np.testing.assert_array_equal(
            terrain._terrain_tile_sample_indices(256, 3),
            np.array([0, 128, 256], dtype=np.intp),
        )

    def test_positive_edges_come_from_right_up_and_diagonal_neighbors(self):
        current = np.array([[10, 11], [12, 13]], dtype=np.uint16)
        right = np.array([[20, 21], [22, 23]], dtype=np.uint16)
        up = np.array([[30, 31], [32, 33]], dtype=np.uint16)
        diagonal = np.array([[40, 41], [42, 43]], dtype=np.uint16)

        stitched = terrain._stitch_tile_heightmap(current, right, up, diagonal)

        np.testing.assert_array_equal(
            stitched,
            np.array(
                [
                    [10, 11, 20],
                    [12, 13, 22],
                    [30, 31, 40],
                ],
                dtype=np.uint16,
            ),
        )

    def test_missing_neighbors_clamp_to_current_positive_edges(self):
        current = np.array([[10, 11], [12, 13]], dtype=np.uint16)

        stitched = terrain._stitch_tile_heightmap(current)

        np.testing.assert_array_equal(
            stitched,
            np.array(
                [
                    [10, 11, 11],
                    [12, 13, 13],
                    [12, 13, 13],
                ],
                dtype=np.uint16,
            ),
        )

    def test_adjacent_stitched_tiles_have_identical_shared_edge(self):
        left = np.array([[1, 2], [3, 4]], dtype=np.uint16)
        right = np.array([[5, 6], [7, 8]], dtype=np.uint16)
        left_up = np.array([[9, 10], [11, 12]], dtype=np.uint16)
        right_up = np.array([[13, 14], [15, 16]], dtype=np.uint16)

        stitched_left = terrain._stitch_tile_heightmap(
            left,
            right=right,
            up=left_up,
            diagonal=right_up,
        )
        stitched_right = terrain._stitch_tile_heightmap(
            right,
            up=right_up,
        )

        np.testing.assert_array_equal(stitched_left[:, -1], stitched_right[:, 0])

    def test_mesh_creation_samples_the_stitched_grid_not_the_unextended_tile(self):
        class BulkData:
            def __init__(self):
                self.values = {}

            def add(self, count):
                self.count = count

            def foreach_set(self, name, values):
                self.values[name] = np.asarray(values).copy()

        class UVLayers:
            def __init__(self):
                self.active = None

            def new(self, name):
                layer = types.SimpleNamespace(name=name, uv=BulkData())
                self.active = layer
                return layer

        class FakeMesh:
            def __init__(self):
                self.vertices = BulkData()
                self.loops = BulkData()
                self.polygons = BulkData()
                self.uv_layers = UVLayers()

            def update(self, **_kwargs):
                pass

        buffers = {
            "current": np.array([[1, 2], [3, 4]], dtype=np.uint16),
            "right": np.array([[5, 6], [7, 8]], dtype=np.uint16),
            "up": np.array([[9, 10], [11, 12]], dtype=np.uint16),
            "diagonal": np.array([[13, 14], [15, 16]], dtype=np.uint16),
        }
        mesh = FakeMesh()

        def read_buffer(path, dtype):
            self.assertEqual(dtype, "<u2")
            return buffers[path].ravel()

        with (
            mock.patch.object(terrain.np, "fromfile", side_effect=read_buffer),
            mock.patch.object(
                terrain.bpy,
                "data",
                types.SimpleNamespace(
                    meshes=types.SimpleNamespace(new=mock.Mock(return_value=mesh))
                ),
                create=True,
            ),
        ):
            result = terrain._create_tile_mesh(
                "tile",
                "current",
                2,
                3,
                64.0,
                1.0,
                positive_x_buffer_path="right",
                positive_y_buffer_path="up",
                positive_xy_buffer_path="diagonal",
                skirt_depth=10.0,
            )

        self.assertIs(result, mesh)
        coordinates = mesh.vertices.values["co"].reshape((-1, 3))
        expected_samples = np.array(
            [1, 2, 5, 3, 4, 7, 9, 10, 13], dtype=np.float32
        )
        # The elevation scale is deliberately factored out: this regression is
        # about selecting all inclusive source samples, including neighbor edges.
        np.testing.assert_allclose(
            coordinates[:9, 2] / coordinates[0, 2],
            expected_samples,
            rtol=1e-6,
        )
        edge_vertices = np.array(
            [0, 1, 2, 2, 5, 8, 8, 7, 6, 6, 3, 0], dtype=np.intp
        )
        self.assertEqual((mesh.vertices.count, mesh.polygons.count), (21, 12))
        np.testing.assert_allclose(
            coordinates[9:, :2], coordinates[edge_vertices, :2]
        )
        np.testing.assert_allclose(
            coordinates[9:, 2], coordinates[edge_vertices, 2] - 10.0
        )
        np.testing.assert_array_equal(
            mesh.loops.values["vertex_index"][16:20],
            np.array([0, 9, 10, 1], dtype=np.int32),
        )


class TestTerrainTileRebuild(unittest.TestCase):
    def test_rebuild_preserves_material_and_invalidates_import_signature(self):
        material = object()
        old_mesh = types.SimpleNamespace(materials=[material], users=0)

        class MaterialSlots(list):
            pass

        class NewMesh(dict):
            def __init__(self):
                super().__init__()
                self.materials = MaterialSlots()

        new_mesh = NewMesh()

        class TileObject(dict):
            def __init__(self):
                super().__init__(
                    tile_buffer_path=r"C:\resolved\tile.buffer",
                    tile_res=256,
                    tile_size=64.0,
                    elev_range=400.0,
                    lowest_elevation=-100.0,
                    positive_x_buffer_path=r"C:\resolved\right.buffer",
                    positive_y_buffer_path=r"C:\resolved\up.buffer",
                    positive_xy_buffer_path=r"C:\resolved\diagonal.buffer",
                    terrain_lod_skirt_depth=400.0,
                    terrain_tile_source_signature="old-signature",
                )
                self.name = "tile_0_x_0"
                self.data = old_mesh

        obj = TileObject()
        create_mesh = mock.Mock(return_value=new_mesh)
        with (
            mock.patch.object(terrain.os.path, "isfile", return_value=True),
            mock.patch.object(terrain, "_create_tile_mesh", create_mesh),
            mock.patch.object(terrain, "_take_cached_tile_mesh", return_value=None),
            mock.patch.object(terrain, "_store_cached_tile_mesh") as store_mesh,
        ):
            self.assertTrue(terrain.rebuild_tile_mesh(obj, 5))

        self.assertEqual(new_mesh.materials, [material])
        self.assertNotIn("terrain_tile_source_signature", obj)
        self.assertEqual(obj["terrain_multires"], 5)
        store_mesh.assert_called_once_with(old_mesh)
        create_mesh.assert_called_once_with(
            obj.name,
            obj["tile_buffer_path"],
            256,
            33,
            64.0,
            400.0,
            positive_x_buffer_path=obj["positive_x_buffer_path"],
            positive_y_buffer_path=obj["positive_y_buffer_path"],
            positive_xy_buffer_path=obj["positive_xy_buffer_path"],
            heightmap_cache=terrain._REBUILD_HEIGHTMAP_CACHE,
            skirt_depth=400.0,
        )


class TestTerrainTileUnload(unittest.TestCase):
    def test_unload_removes_only_matching_world_and_coordinate(self):
        spec = _novigrad_spec()

        class FakeObject(dict):
            type = "MESH"

            def __init__(self, world_key, x, y):
                super().__init__(
                    witcher_terrain_tile=True,
                    witcher_terrain_world_key=world_key,
                    tile_x=x,
                    tile_y=y,
                )
                self.data = types.SimpleNamespace(users=0)

        target = FakeObject(spec.world_key, 2, 3)
        neighbor = FakeObject(spec.world_key, 3, 3)
        objects = [target, neighbor]
        removed_meshes = []

        class ObjectStore(list):
            def remove(self, obj, do_unlink=False):
                self.remove_called = (obj, do_unlink)
                super().remove(obj)

        store = ObjectStore(objects)
        fake_data = types.SimpleNamespace(
            objects=store,
            meshes=types.SimpleNamespace(remove=lambda mesh: removed_meshes.append(mesh)),
        )
        root = types.SimpleNamespace(children=[target, neighbor])
        with mock.patch.object(terrain.bpy, "data", fake_data, create=True):
            self.assertTrue(terrain.unload_world_terrain_tile(spec, 2, 3, root=root))

        self.assertNotIn(target, store)
        self.assertIn(neighbor, store)
        self.assertEqual(store.remove_called, (target, True))
        self.assertEqual(removed_meshes, [target.data])


class TestAllTilesHeightmapStitching(unittest.TestCase):
    def test_all_tiles_assign_positive_neighbor_buffers_to_each_source(self):
        spec = replace(
            _novigrad_spec(),
            terrain_size=128.0,
            tile_res=2,
            x_tiles=2,
            y_tiles=2,
        )
        class Root(dict):
            def __init__(self):
                super().__init__()
                self.children = []

        root = Root()
        buffers = {
            (0, 0): "tile_0_0.buffer",
            (1, 0): "tile_1_0.buffer",
            (0, 1): "tile_0_1.buffer",
            (1, 1): "tile_1_1.buffer",
        }
        textures = {key: value + ".texture" for key, value in buffers.items()}
        sources = {}

        def import_tile(_spec, source, bounds, **_kwargs):
            sources[(source.key.x, source.key.y)] = source
            return types.SimpleNamespace(ok=True, bounds=bounds, obj=object())

        with (
            mock.patch.object(terrain, "ensure_world_terrain_collection", return_value=object()),
            mock.patch.object(terrain, "_ensure_terrain_root", return_value=root),
            mock.patch.object(terrain, "_import_resolved_terrain_tile", side_effect=import_tile),
        ):
            _root, count = terrain.do_import_terrain_tiles(
                tile_heightmap_buffers=buffers,
                tile_texture_buffers=textures,
                tile_overlays={},
                x_tiles=spec.x_tiles,
                y_tiles=spec.y_tiles,
                tile_res=spec.tile_res,
                terrain_size=spec.terrain_size,
                lowest_elevation=spec.lowest_elevation,
                highest_elevation=spec.highest_elevation,
                multires_level=1,
                hub_name=spec.hub_name,
                terrain_spec=spec,
                detail_material=False,
            )

        self.assertIs(_root, root)
        self.assertEqual(count, 4)
        self.assertEqual(
            (
                sources[(0, 0)].positive_x_buffer_path,
                sources[(0, 0)].positive_y_buffer_path,
                sources[(0, 0)].positive_xy_buffer_path,
            ),
            (buffers[(1, 0)], buffers[(0, 1)], buffers[(1, 1)]),
        )
        self.assertEqual(sources[(1, 0)].positive_x_buffer_path, "")
        self.assertEqual(sources[(1, 0)].positive_y_buffer_path, buffers[(1, 1)])
        self.assertEqual(sources[(1, 0)].positive_xy_buffer_path, "")
        self.assertEqual(sources[(0, 1)].positive_x_buffer_path, buffers[(1, 1)])
        self.assertEqual(sources[(0, 1)].positive_y_buffer_path, "")
        self.assertEqual(sources[(0, 1)].positive_xy_buffer_path, "")
        self.assertEqual(sources[(1, 1)].positive_x_buffer_path, "")
        self.assertEqual(sources[(1, 1)].positive_y_buffer_path, "")
        self.assertEqual(sources[(1, 1)].positive_xy_buffer_path, "")
        self.assertEqual(
            (
                sources[(0, 0)].positive_x_texture_buffer_path,
                sources[(0, 0)].positive_y_texture_buffer_path,
                sources[(0, 0)].positive_xy_texture_buffer_path,
            ),
            (textures[(1, 0)], textures[(0, 1)], textures[(1, 1)]),
        )
        self.assertNotIn("terrain_import_seconds", root)
        self.assertNotIn("terrain_import_timing", root)


class TestAllTilesDetailMaterial(unittest.TestCase):
    def test_all_tiles_passes_control_buffer_to_detail_builder(self):
        spec = _novigrad_spec()
        root = types.SimpleNamespace(children=[])
        imported_obj = types.SimpleNamespace(name="tile_3_x_2")
        imported = types.SimpleNamespace(
            ok=True,
            bounds=terrain.terrain_tile_bounds(spec, 2, 3),
            obj=imported_obj,
        )
        world_file = object()
        height_path = str(Path("test_data") / "tile.1.buffer")
        texture_path = str(Path("test_data") / "tile.2.buffer")
        overlay_path = str(Path("test_data") / "tile.overlay.png")

        with (
            mock.patch.object(terrain, "ensure_world_terrain_collection", return_value=object()),
            mock.patch.object(terrain, "_ensure_terrain_root", return_value=root),
            mock.patch.object(
                terrain, "_import_resolved_terrain_tile", return_value=imported
            ) as import_tile,
            mock.patch.object(terrain, "_apply_tile_detail_material") as apply_detail,
            mock.patch.object(terrain, "_purge_unused_terrain_detail_groups") as purge_detail,
            mock.patch.object(terrain, "redkit_repo_context", side_effect=lambda _path: nullcontext()),
        ):
            _root, count = terrain.do_import_terrain_tiles(
                tile_heightmap_buffers={(2, 3): height_path},
                tile_texture_buffers={(2, 3): texture_path},
                tile_overlays={(2, 3): overlay_path},
                x_tiles=spec.x_tiles,
                y_tiles=spec.y_tiles,
                tile_res=spec.tile_res,
                terrain_size=spec.terrain_size,
                lowest_elevation=spec.lowest_elevation,
                highest_elevation=spec.highest_elevation,
                multires_level=6,
                hub_name=spec.hub_name,
                world_path=spec.world_path,
                world_file=world_file,
                terrain_spec=spec,
                detail_material=True,
            )

        self.assertEqual(count, 1)
        self.assertIs(_root, root)
        self.assertFalse(import_tile.call_args.kwargs["apply_overlay"])
        apply_detail.assert_called_once()
        args = apply_detail.call_args.args
        self.assertIs(args[0], world_file)
        self.assertEqual(args[2].texture_buffer, texture_path)
        self.assertIs(args[4], imported_obj)
        self.assertFalse(apply_detail.call_args.kwargs["purge_unused"])
        purge_detail.assert_called_once_with()


class TestViewLodRouting(unittest.TestCase):
    def test_view_lod_mode_dispatches_only_to_view_lod_import(self):
        marker = object()
        with (
            mock.patch.object(
                terrain, "_get_scene_terrain_import_mode",
                return_value=terrain.TERRAIN_IMPORT_VIEW_LOD,
            ),
            mock.patch.object(
                terrain, "_do_import_map_terrain_view_lod", return_value=marker
            ) as view_lod,
            mock.patch.object(terrain, "_do_import_map_terrain_tiles") as all_tiles,
            mock.patch.object(terrain, "_do_import_map_terrain_full_map") as full_map,
        ):
            result = terrain.do_import_map_terrain(
                "world", "novigrad.w2w", world_root_collection="collection")

        self.assertIs(result, marker)
        view_lod.assert_called_once_with("world", "novigrad.w2w", "collection")
        all_tiles.assert_not_called()
        full_map.assert_not_called()

    def test_only_focus_window_gets_native_geometry_and_detail_materials(self):
        spec = replace(_novigrad_spec(), x_tiles=46, y_tiles=46)
        focus = terrain.terrain_view_lod_tiles(46, 46, 23, 23, 3)
        buffers = {(x, y): f"height_{x}_{y}" for y in range(46) for x in range(46)}
        textures = {(x, y): f"control_{x}_{y}" for y in range(46) for x in range(46)}
        levels = {key: 9 for key in focus}
        imported_levels = {}
        overlay_flags = {}
        skirt_depths = {}

        class Root(dict):
            children = []

        class TileObject(dict):
            pass

        root = Root()

        def import_tile(_spec, source, bounds, **kwargs):
            key = (source.key.x, source.key.y)
            imported_levels[key] = kwargs["multires_level"]
            overlay_flags[key] = kwargs["apply_overlay"]
            skirt_depths[key] = kwargs["skirt_depth"]
            return types.SimpleNamespace(ok=True, bounds=bounds, obj=TileObject())

        def apply_overview(obj, _material, _spec, _key):
            obj["terrain_lod_role"] = "overview"

        with (
            mock.patch.object(terrain, "ensure_world_terrain_collection", return_value=object()),
            mock.patch.object(terrain, "_ensure_terrain_root", return_value=root),
            mock.patch.object(terrain, "_import_resolved_terrain_tile", side_effect=import_tile),
            mock.patch.object(terrain, "_apply_tile_detail_material", return_value=object()) as detail,
            mock.patch.object(
                terrain, "_apply_terrain_lod_overview_material", side_effect=apply_overview
            ) as overview,
            mock.patch.object(terrain, "_purge_unused_terrain_detail_groups"),
            mock.patch.object(terrain, "_purge_unused_terrain_detail_data"),
            mock.patch.object(terrain, "redkit_repo_context", side_effect=lambda _path: nullcontext()),
        ):
            _root, count = terrain.do_import_terrain_tiles(
                tile_heightmap_buffers=buffers,
                tile_texture_buffers=textures,
                tile_overlays={},
                x_tiles=46,
                y_tiles=46,
                tile_res=512,
                terrain_size=spec.terrain_size,
                lowest_elevation=spec.lowest_elevation,
                highest_elevation=spec.highest_elevation,
                multires_level=5,
                hub_name=spec.hub_name,
                world_path=spec.world_path,
                world_file=object(),
                terrain_spec=spec,
                detail_material=True,
                tile_levels=levels,
                detail_tiles=focus,
                overview_material=object(),
            )

        self.assertIs(_root, root)
        self.assertEqual(count, 2116)
        self.assertEqual(
            {key for key, level in imported_levels.items() if level == 9},
            set(focus),
        )
        self.assertEqual(
            {key for key, level in imported_levels.items() if level == 5},
            set(buffers) - set(focus),
        )
        self.assertFalse(any(overlay_flags.values()))
        self.assertEqual(
            set(skirt_depths.values()), {terrain.TERRAIN_VIEW_LOD_SKIRT_AUTO})
        self.assertEqual(detail.call_count, 49)
        self.assertEqual(overview.call_count, 2067)
        self.assertEqual(
            {(call.args[2].key.x, call.args[2].key.y) for call in detail.call_args_list},
            set(focus),
        )
        self.assertEqual(
            {(call.args[3].x, call.args[3].y) for call in overview.call_args_list},
            set(buffers) - set(focus),
        )

    def test_update_moves_only_set_difference_and_same_center_is_idempotent(self):
        spec = replace(_novigrad_spec(), x_tiles=46, y_tiles=46)
        old_focus = terrain.terrain_view_lod_tiles(46, 46, 23, 23, 3)
        new_focus = terrain.terrain_view_lod_tiles(46, 46, 24, 23, 3)
        leaving = set(old_focus - new_focus)
        entering = set(new_focus - old_focus)

        class Root(dict):
            pass

        class TileObject(dict):
            type = "MESH"

        tiles = {}
        for key in old_focus | new_focus:
            role = "native" if key in old_focus else "overview"
            level = 9 if role == "native" else 5
            tiles[key] = TileObject(
                tile_x=key[0],
                tile_y=key[1],
                terrain_lod_role=role,
                terrain_multires=level,
            )
        root = Root(
            witcher_terrain_root=True,
            witcher_terrain_world_key=spec.world_key,
            terrain_import_mode=terrain.TERRAIN_IMPORT_VIEW_LOD,
            terrain_lod_radius=3,
            terrain_lod_detail_material=True,
            terrain_lod_detail_texture_res=2048,
            terrain_lod_overview_material="overview",
        )
        root.children = list(tiles.values())
        world_collection = types.SimpleNamespace(all_objects=[root])
        overview_material = types.SimpleNamespace(name="overview")

        def resolve_source(_spec, x, y, **kwargs):
            request = terrain.terrain_tile_source_request(
                x,
                y,
                include_overlay=kwargs.get("include_overlay", True),
                include_stitch_neighbors=kwargs.get("include_stitch_neighbors", False),
            )
            return terrain.TerrainTileSource(
                request=request,
                tile_name=f"tile_{y}_x_{x}",
                heightmap_buffer=f"height_{x}_{y}",
                texture_buffer=f"control_{x}_{y}" if request.include_overlay else "",
            )

        def rebuild(obj, level, **kwargs):
            obj["terrain_multires"] = level
            return True

        def apply_overview(obj, _material, _spec, _key):
            obj["terrain_lod_role"] = "overview"

        target = terrain.terrain_tile_bounds(spec, 24, 23)
        position = (target.center_x, target.center_y, 0.0)
        with (
            mock.patch.object(terrain, "inspect_world_terrain", return_value=spec),
            mock.patch.object(
                terrain.bpy,
                "data",
                types.SimpleNamespace(
                    materials=types.SimpleNamespace(get=lambda _name: overview_material)
                ),
                create=True,
            ),
            mock.patch.object(
                terrain, "resolve_world_terrain_tile_source", side_effect=resolve_source
            ) as resolve,
            mock.patch.object(terrain, "rebuild_tile_mesh", side_effect=rebuild) as rebuild_mesh,
            mock.patch.object(
                terrain, "_apply_tile_detail_material", return_value=object()
            ) as detail,
            mock.patch.object(
                terrain, "_apply_terrain_lod_overview_material", side_effect=apply_overview
            ) as overview,
            mock.patch.object(terrain, "_tile_import_signature", return_value="signature"),
            mock.patch.object(terrain, "_get_scene_terrain_detail_res", return_value=2048),
            mock.patch.object(terrain, "_purge_unused_terrain_detail_data") as purge,
            mock.patch.object(
                terrain, "redkit_repo_context", side_effect=lambda _path: nullcontext()
            ),
        ):
            result = terrain.update_terrain_view_lod(
                object(),
                spec.world_path,
                position,
                radius=3,
                world_root_collection=world_collection,
                detail_material=True,
            )

            self.assertEqual((result["promoted"], result["lowered"]), (7, 7))
            self.assertEqual(rebuild_mesh.call_count, 14)
            self.assertEqual(
                {(call.args[1], call.args[2]) for call in resolve.call_args_list},
                entering | leaving,
            )
            self.assertEqual(
                {(call.args[2].key.x, call.args[2].key.y) for call in detail.call_args_list},
                entering,
            )
            self.assertEqual(
                {(call.args[3].x, call.args[3].y) for call in overview.call_args_list},
                leaving,
            )
            purge.assert_called_once_with()

            resolve.reset_mock()
            rebuild_mesh.reset_mock()
            detail.reset_mock()
            overview.reset_mock()
            purge.reset_mock()
            repeated = terrain.update_terrain_view_lod(
                object(),
                spec.world_path,
                position,
                radius=3,
                world_root_collection=world_collection,
                detail_material=True,
            )

        self.assertEqual(
            (repeated["promoted"], repeated["lowered"], repeated["refreshed"]),
            (0, 0, 0),
        )
        resolve.assert_not_called()
        rebuild_mesh.assert_not_called()
        detail.assert_not_called()
        overview.assert_not_called()
        purge.assert_called_once_with()

    def test_auto_skirt_depth_tracks_border_deviation_not_elevation_range(self):
        tile_res = 512
        stitched = np.zeros((tile_res + 1, tile_res + 1), dtype=np.uint16)
        stitched[0, 8] = 6553
        depth = terrain._auto_skirt_depth(stitched, tile_res, 400.0)
        self.assertAlmostEqual(depth, 6553 * 400.0 / 65535.0 + 1.0, places=4)
        self.assertLess(depth, 45.0)

        flat = np.zeros((tile_res + 1, tile_res + 1), dtype=np.uint16)
        self.assertAlmostEqual(
            terrain._auto_skirt_depth(flat, tile_res, 400.0), 1.0)

        anchored = np.zeros((17, 17), dtype=np.uint16)
        anchored[0, :] = np.arange(17, dtype=np.uint16) * 1000
        self.assertAlmostEqual(terrain._auto_skirt_depth(anchored, 16, 400.0), 1.0)

    def test_update_streams_with_max_tiles_demotes_first_and_reports_pending(self):
        spec = replace(_novigrad_spec(), x_tiles=46, y_tiles=46)
        old_focus = terrain.terrain_view_lod_tiles(46, 46, 23, 23, 3)
        new_focus = terrain.terrain_view_lod_tiles(46, 46, 24, 23, 3)

        class Root(dict):
            pass

        class TileObject(dict):
            type = "MESH"

        tiles = {}
        for key in old_focus | new_focus:
            role = "native" if key in old_focus else "overview"
            tiles[key] = TileObject(
                tile_x=key[0],
                tile_y=key[1],
                terrain_lod_role=role,
                terrain_multires=9 if role == "native" else 5,
            )
        root = Root(
            witcher_terrain_root=True,
            witcher_terrain_world_key=spec.world_key,
            terrain_import_mode=terrain.TERRAIN_IMPORT_VIEW_LOD,
            terrain_lod_radius=3,
            terrain_lod_detail_material=True,
            terrain_lod_detail_texture_res=2048,
            terrain_lod_overview_material="overview",
        )
        root.children = list(tiles.values())
        world_collection = types.SimpleNamespace(all_objects=[root])
        overview_material = types.SimpleNamespace(name="overview")

        def resolve_source(_spec, x, y, **kwargs):
            request = terrain.terrain_tile_source_request(
                x,
                y,
                include_overlay=kwargs.get("include_overlay", True),
                include_stitch_neighbors=kwargs.get("include_stitch_neighbors", False),
            )
            return terrain.TerrainTileSource(
                request=request,
                tile_name=f"tile_{y}_x_{x}",
                heightmap_buffer=f"height_{x}_{y}",
                texture_buffer=f"control_{x}_{y}" if request.include_overlay else "",
            )

        def rebuild(obj, level, **kwargs):
            obj["terrain_multires"] = level
            return True

        def apply_overview(obj, _material, _spec, _key):
            obj["terrain_lod_role"] = "overview"

        target = terrain.terrain_tile_bounds(spec, 24, 23)
        position = (target.center_x, target.center_y, 0.0)
        with (
            mock.patch.object(terrain, "inspect_world_terrain", return_value=spec),
            mock.patch.object(
                terrain.bpy,
                "data",
                types.SimpleNamespace(
                    materials=types.SimpleNamespace(get=lambda _name: overview_material)
                ),
                create=True,
            ),
            mock.patch.object(
                terrain, "resolve_world_terrain_tile_source", side_effect=resolve_source
            ),
            mock.patch.object(terrain, "rebuild_tile_mesh", side_effect=rebuild),
            mock.patch.object(terrain, "_apply_tile_detail_material", return_value=object()),
            mock.patch.object(
                terrain, "_apply_terrain_lod_overview_material", side_effect=apply_overview
            ),
            mock.patch.object(terrain, "_tile_import_signature", return_value="signature"),
            mock.patch.object(terrain, "_get_scene_terrain_detail_res", return_value=2048),
            mock.patch.object(terrain, "_purge_unused_terrain_detail_data") as purge,
            mock.patch.object(
                terrain, "redkit_repo_context", side_effect=lambda _path: nullcontext()
            ),
        ):
            first = terrain.update_terrain_view_lod(
                object(),
                spec.world_path,
                position,
                radius=3,
                world_root_collection=world_collection,
                detail_material=True,
                max_tiles=3,
            )
            self.assertEqual(
                (first["lowered"], first["promoted"], first["pending"]), (3, 0, 11))
            purge.assert_not_called()

            lowered = first["lowered"]
            promoted = first["promoted"]
            calls = 1
            while True:
                step = terrain.update_terrain_view_lod(
                    object(),
                    spec.world_path,
                    position,
                    radius=3,
                    world_root_collection=world_collection,
                    detail_material=True,
                    max_tiles=3,
                )
                lowered += step["lowered"]
                promoted += step["promoted"]
                calls += 1
                if not step["pending"]:
                    break

        self.assertEqual((lowered, promoted), (7, 7))
        self.assertEqual(calls, 5)
        purge.assert_called_once_with()

    def test_promote_reuses_cached_detail_material_and_demote_retains_it(self):
        terrain._DETAIL_MATERIAL_LRU.clear()
        spec = replace(_novigrad_spec(), x_tiles=46, y_tiles=46)
        old_focus = terrain.terrain_view_lod_tiles(46, 46, 23, 23, 3)
        new_focus = terrain.terrain_view_lod_tiles(46, 46, 26, 23, 3)
        leaving = set(old_focus - new_focus)
        entering = set(new_focus - old_focus)

        class Root(dict):
            pass

        class FakeMat(dict):
            def __init__(self, name, **kwargs):
                super().__init__(**kwargs)
                self.name = name
                self.use_fake_user = False

        class TileObject(dict):
            type = "MESH"

        def mat_name(key):
            return terrain._tile_material_name(
                spec, types.SimpleNamespace(x=key[0], y=key[1]))

        tiles = {}
        for key in old_focus | new_focus:
            role = "native" if key in old_focus else "overview"
            obj = TileObject(
                tile_x=key[0],
                tile_y=key[1],
                terrain_lod_role=role,
                terrain_multires=9 if role == "native" else 5,
            )
            obj.data = types.SimpleNamespace(materials=[])
            tiles[key] = obj

        cached_mats = {}
        for key in entering:
            mat = FakeMat(
                mat_name(key),
                witcher_terrain_detail=True,
                witcher_terrain_lod_sig="signature:2048",
            )
            mat.use_fake_user = True
            cached_mats[mat.name] = mat
            terrain._DETAIL_MATERIAL_LRU[mat.name] = None
        live_mats = {}
        for key in leaving:
            mat = FakeMat(mat_name(key), witcher_terrain_detail=True)
            tiles[key].data.materials.append(mat)
            live_mats[key] = mat

        root = Root(
            witcher_terrain_root=True,
            witcher_terrain_world_key=spec.world_key,
            terrain_import_mode=terrain.TERRAIN_IMPORT_VIEW_LOD,
            terrain_lod_radius=3,
            terrain_lod_detail_material=True,
            terrain_lod_detail_texture_res=2048,
            terrain_lod_overview_material="overview",
        )
        root.children = list(tiles.values())
        world_collection = types.SimpleNamespace(all_objects=[root])
        overview_material = types.SimpleNamespace(name="overview")

        def resolve_source(_spec, x, y, **kwargs):
            request = terrain.terrain_tile_source_request(
                x,
                y,
                include_overlay=kwargs.get("include_overlay", True),
                include_stitch_neighbors=kwargs.get("include_stitch_neighbors", False),
            )
            return terrain.TerrainTileSource(
                request=request,
                tile_name=f"tile_{y}_x_{x}",
                heightmap_buffer=f"height_{x}_{y}",
                texture_buffer=f"control_{x}_{y}" if request.include_overlay else "",
            )

        def rebuild(obj, level, **kwargs):
            obj["terrain_multires"] = level
            return True

        def apply_overview(obj, _material, _spec, _key):
            obj["terrain_lod_role"] = "overview"

        target = terrain.terrain_tile_bounds(spec, 26, 23)
        position = (target.center_x, target.center_y, 0.0)
        with (
            mock.patch.object(terrain, "inspect_world_terrain", return_value=spec),
            mock.patch.object(
                terrain.bpy,
                "data",
                types.SimpleNamespace(
                    materials=types.SimpleNamespace(
                        get=lambda name: cached_mats.get(name, overview_material))
                ),
                create=True,
            ),
            mock.patch.object(
                terrain, "resolve_world_terrain_tile_source", side_effect=resolve_source
            ),
            mock.patch.object(terrain, "rebuild_tile_mesh", side_effect=rebuild),
            mock.patch.object(terrain, "_apply_tile_detail_material") as detail,
            mock.patch.object(
                terrain, "_apply_terrain_lod_overview_material", side_effect=apply_overview
            ),
            mock.patch.object(terrain, "_tile_import_signature", return_value="signature"),
            mock.patch.object(terrain, "_get_scene_terrain_detail_res", return_value=2048),
            mock.patch.object(terrain, "_purge_unused_terrain_detail_data"),
            mock.patch.object(
                terrain, "redkit_repo_context", side_effect=lambda _path: nullcontext()
            ),
        ):
            result = terrain.update_terrain_view_lod(
                object(),
                spec.world_path,
                position,
                radius=3,
                world_root_collection=world_collection,
                detail_material=True,
            )

        self.assertEqual((result["promoted"], result["lowered"]), (21, 21))
        detail.assert_not_called()
        for key in entering:
            mat = cached_mats[mat_name(key)]
            self.assertEqual(tiles[key].data.materials, [mat])
            self.assertFalse(mat.use_fake_user)
            self.assertNotIn(mat.name, terrain._DETAIL_MATERIAL_LRU)
        for key in leaving:
            self.assertTrue(live_mats[key].use_fake_user)
            self.assertIn(live_mats[key].name, terrain._DETAIL_MATERIAL_LRU)
        terrain._DETAIL_MATERIAL_LRU.clear()


class TestTerrainDetailWorldAssets(unittest.TestCase):
    def test_source_world_supplies_texture_pack_values_for_uncook_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uncook = root / "addon_uncook"
            redkit_depot = root / "redkit" / "r4data"
            rel_path = Path("levels") / "test_world" / "test.w2w"
            uncook_world = uncook / rel_path
            source_world_path = redkit_depot / rel_path
            uncook_world.parent.mkdir(parents=True)
            source_world_path.parent.mkdir(parents=True)
            uncook_world.write_bytes(b"stale uncook world")
            source_world_path.write_bytes(b"fresh project world")

            selected_world = object()
            source_world = object()
            spec = replace(
                _novigrad_spec(),
                world_path=str(uncook_world),
                world_key="source-world-texture-params",
            )
            reader = types.SimpleNamespace(load_w2w=mock.Mock(
                return_value=source_world))
            with (
                mock.patch.object(terrain, "get_uncook_path", return_value=str(uncook)),
                mock.patch.object(
                    terrain, "_configured_redkit_workspace_roots", return_value=[]),
                mock.patch.object(
                    terrain, "_configured_redkit_roots",
                    return_value=[str(redkit_depot)]),
                mock.patch.object(terrain.CR2W, "CR2W_reader", reader, create=True),
            ):
                resolved_world, resolved_path = terrain._terrain_detail_parameter_world(
                    selected_world, spec)

            self.assertIs(resolved_world, source_world)
            self.assertEqual(Path(resolved_path), source_world_path)
            reader.load_w2w.assert_called_once_with(
                str(source_world_path), include_groups=False)

    def test_configured_source_graph_wins_for_external_world(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            redkit_depot = root / "redkit" / "r4data"
            redkit_uncooked = root / "redkit" / "uncooked"
            addon_uncook = root / "addon_uncook"
            graph_rel = Path("levels") / "test_world" / "terrain_material.w2mg"
            source_graph = redkit_depot / graph_rel
            fallback_graph = addon_uncook / graph_rel
            source_graph.parent.mkdir(parents=True)
            fallback_graph.parent.mkdir(parents=True)
            source_graph.write_text("16.0", encoding="utf-8")
            fallback_graph.write_text("2.0", encoding="utf-8")

            context_state = {"roots": [], "source": ""}
            selected = {"path": ""}

            @contextmanager
            def fake_redkit_context(source_path=None, roots=None):
                previous = dict(context_state)
                context_state["source"] = str(source_path or "")
                context_state["roots"] = list(roots or [])
                try:
                    yield
                finally:
                    context_state.update(previous)

            def extract_material_set(_world):
                candidates = [Path(value) / graph_rel for value in context_state["roots"]]
                candidates.append(fallback_graph)
                graph = next(path for path in candidates if path.is_file())
                selected["path"] = str(graph)
                layer = types.SimpleNamespace(
                    blend_sharpness=0.1,
                    slope_base_dampening=0.0,
                    slope_normal_dampening=0.5,
                    falloff=0.0,
                    specularity=0.0,
                    specularity_base=0.0,
                    specularity_scale=0.0,
                )
                return types.SimpleNamespace(
                    layers=[layer],
                    warnings=[],
                    fresnel_power=float(graph.read_text(encoding="utf-8")),
                )

            terrain_material = types.ModuleType(
                "witcher3_tools.unreal_export.terrain_material")
            terrain_material.extract_terrain_material_set = extract_material_set
            unreal_export = types.ModuleType("witcher3_tools.unreal_export")
            unreal_export.__path__ = []
            unreal_export.terrain_material = terrain_material
            terrain_detail = types.ModuleType(
                "witcher3_tools.importers.terrain_detail")
            terrain_detail.pack_world_detail_atlases = (
                lambda *_args, **_kwargs: {"layout": {}, "diffuse": "atlas.png"}
            )
            terrain_detail.build_terrain_layer_metadata = lambda layers: [
                {"id": i + 1, "name": f"Layer {i + 1}"}
                for i, _layer in enumerate(layers)
            ]
            root_package = types.ModuleType("witcher3_tools")
            root_package.__path__ = [str(REPO_ROOT / "witcher3_tools")]
            importers_package = types.ModuleType("witcher3_tools.importers")
            importers_package.__path__ = [
                str(REPO_ROOT / "witcher3_tools" / "importers")]
            importers_package.terrain_detail = terrain_detail
            root_package.importers = importers_package
            root_package.unreal_export = unreal_export

            spec = replace(
                _novigrad_spec(),
                world_path=str(addon_uncook / "levels" / "test_world" / "test.w2w"),
                world_key="external-world-redkit-material",
                working_tiles_dir=str(root / "work" / "terrain_tiles"),
            )
            terrain._TERRAIN_DETAIL_WORLD_CACHE.clear()
            with (
                mock.patch.dict(
                    sys.modules,
                    {
                        "witcher3_tools": root_package,
                        "witcher3_tools.importers": importers_package,
                        "witcher3_tools.unreal_export": unreal_export,
                        "witcher3_tools.unreal_export.terrain_material": terrain_material,
                        "witcher3_tools.importers.terrain_detail": terrain_detail,
                    },
                ),
                mock.patch.object(
                    terrain,
                    "_configured_redkit_roots",
                    return_value=[str(redkit_depot), str(redkit_uncooked)],
                ),
                mock.patch.object(
                    terrain,
                    "redkit_repo_context",
                    side_effect=fake_redkit_context,
                ) as context_mock,
            ):
                assets = terrain._terrain_detail_world_assets(object(), spec, 256)

            self.assertEqual(Path(selected["path"]), source_graph)
            self.assertEqual(assets["fresnel_power"], 16.0)
            context_mock.assert_called_once_with(
                spec.world_path,
                roots=[str(redkit_depot), str(redkit_uncooked)],
            )
            terrain._TERRAIN_DETAIL_WORLD_CACHE.clear()

    def test_changed_world_or_deleted_atlas_invalidates_world_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out_dir = root / "terrain_tiles"
            out_dir.mkdir()
            builds = []

            layer = types.SimpleNamespace(
                blend_sharpness=0.1,
                slope_base_dampening=0.0,
                slope_normal_dampening=0.5,
                falloff=0.0,
                specularity=0.0,
                specularity_base=0.0,
                specularity_scale=0.0,
            )
            terrain_material = types.ModuleType(
                "witcher3_tools.unreal_export.terrain_material")
            terrain_material.extract_terrain_material_set = lambda _world: types.SimpleNamespace(
                layers=[layer], warnings=[], fresnel_power=2.0)

            def pack_atlases(_hub, _layers, target_dir, slice_px):
                builds.append(int(slice_px))
                diffuse = Path(target_dir) / f"atlas_{len(builds)}.png"
                metadata = Path(target_dir) / f"atlas_{len(builds)}.json"
                diffuse.write_bytes(b"png")
                metadata.write_text("{}", encoding="utf-8")
                return {
                    "diffuse": str(diffuse),
                    "normal": "",
                    "json": str(metadata),
                    "layout": {},
                }

            terrain_detail = types.ModuleType(
                "witcher3_tools.importers.terrain_detail")
            terrain_detail.pack_world_detail_atlases = pack_atlases
            terrain_detail.build_terrain_layer_metadata = lambda layers: [
                {"id": i + 1, "name": f"Layer {i + 1}"}
                for i, _layer in enumerate(layers)
            ]
            unreal_export = types.ModuleType("witcher3_tools.unreal_export")
            unreal_export.__path__ = []
            unreal_export.terrain_material = terrain_material
            importers_package = types.ModuleType("witcher3_tools.importers")
            importers_package.__path__ = []
            importers_package.terrain_detail = terrain_detail
            root_package = types.ModuleType("witcher3_tools")
            root_package.__path__ = []
            root_package.importers = importers_package
            root_package.unreal_export = unreal_export

            spec = replace(
                _novigrad_spec(),
                world_path=str(root / "test.w2w"),
                world_key="deleted-atlas-cache",
                working_tiles_dir=str(out_dir),
            )
            Path(spec.world_path).write_text("source revision one", encoding="utf-8")
            terrain._TERRAIN_DETAIL_WORLD_CACHE.clear()
            try:
                with (
                    mock.patch.dict(
                        sys.modules,
                        {
                            "witcher3_tools": root_package,
                            "witcher3_tools.importers": importers_package,
                            "witcher3_tools.unreal_export": unreal_export,
                            "witcher3_tools.unreal_export.terrain_material": terrain_material,
                            "witcher3_tools.importers.terrain_detail": terrain_detail,
                        },
                    ),
                    mock.patch.object(
                        terrain, "_configured_redkit_roots", return_value=[]),
                    mock.patch.object(
                        terrain,
                        "redkit_repo_context",
                        side_effect=lambda *_args, **_kwargs: nullcontext(),
                    ),
                ):
                    first = terrain._terrain_detail_world_assets(object(), spec, 2048)
                    cached = terrain._terrain_detail_world_assets(object(), spec, 2048)
                    Path(spec.world_path).write_text(
                        "source revision two with new values", encoding="utf-8")
                    source_rebuilt = terrain._terrain_detail_world_assets(
                        object(), spec, 2048)
                    Path(source_rebuilt["atlas"]["diffuse"]).unlink()
                    rebuilt = terrain._terrain_detail_world_assets(object(), spec, 2048)

                self.assertIs(first, cached)
                self.assertIsNot(first, source_rebuilt)
                self.assertEqual(builds, [2048, 2048, 2048])
                self.assertNotEqual(
                    source_rebuilt["atlas"]["diffuse"], rebuilt["atlas"]["diffuse"])
            finally:
                terrain._TERRAIN_DETAIL_WORLD_CACHE.clear()


class TestFullMapDetailMaterial(unittest.TestCase):
    def test_full_map_applies_combined_detail_material(self):
        spec = _novigrad_spec()
        world = types.SimpleNamespace(
            terrainSize=spec.terrain_size,
            lowestElevation=spec.lowest_elevation,
            highestElevation=spec.highest_elevation,
            worldName=spec.world_name,
        )
        output_dir = str(Path("test_output") / "terrain")
        texture_path = str(Path("test_data") / "tile.2.buffer")
        ctx = {
            "hub_name": spec.hub_name,
            "n_tiles": spec.x_tiles,
            "tile_res": spec.tile_res,
            "terrain_tiles_dir": spec.terrain_tiles_dir,
            "terrain_tiles_rel": spec.terrain_tiles_rel,
            "working_tiles_dir": spec.working_tiles_dir,
            "output_dir": output_dir,
        }
        texture_tiles = {(0, 0): texture_path}
        combined = {"info": {"tiles": {2: texture_tiles}}}
        obj = types.SimpleNamespace(name="Novigrad")

        with (
            mock.patch.object(terrain, "_resolve_terrain_context", return_value=ctx),
            mock.patch.object(terrain, "inspect_world_terrain", return_value=spec),
            mock.patch.object(terrain, "_get_scene_terrain_detail_enabled", return_value=True),
            mock.patch.object(terrain, "_ensure_world_water_plane"),
            mock.patch.object(terrain, "_get_scene_terrain_multires_level", return_value=6),
            mock.patch.object(terrain, "_collect_tile_buffer_paths_for_combine", return_value=["buffer"]),
            mock.patch.object(terrain.terrain_w2ter, "combine_w2ter_tiles", return_value=combined, create=True),
            mock.patch.object(
                terrain,
                "_bake_fullmap_diffuse",
                return_value=str(Path(output_dir) / "baked.png"),
            ),
            mock.patch.object(terrain.os.path, "isfile", return_value=True),
            mock.patch.object(terrain, "import_combined_terrain_full_map", return_value=obj),
            mock.patch.object(terrain, "_apply_fullmap_detail_material") as apply_detail,
        ):
            result = terrain._do_import_map_terrain_full_map(
                world, spec.world_path, world_root_collection="world-root")

        self.assertIs(result, obj)
        apply_detail.assert_called_once_with(
            world, spec, obj, texture_tiles, ctx["output_dir"])


if __name__ == "__main__":
    unittest.main()
