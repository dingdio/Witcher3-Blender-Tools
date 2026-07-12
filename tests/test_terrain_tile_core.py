import importlib.util
import re
import sys
import types
import unittest
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from unittest import mock


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


class TestTerrainTileRebuild(unittest.TestCase):
    def test_rebuild_preserves_material_and_invalidates_import_signature(self):
        material = object()
        old_mesh = types.SimpleNamespace(materials=[material], users=0)

        class MaterialSlots(list):
            pass

        new_mesh = types.SimpleNamespace(materials=MaterialSlots())

        class TileObject(dict):
            def __init__(self):
                super().__init__(
                    tile_buffer_path=r"C:\resolved\tile.buffer",
                    tile_res=256,
                    tile_size=64.0,
                    elev_range=400.0,
                    lowest_elevation=-100.0,
                    terrain_tile_source_signature="old-signature",
                )
                self.name = "tile_0_x_0"
                self.data = old_mesh

        obj = TileObject()
        meshes = types.SimpleNamespace(remove=mock.Mock())
        with (
            mock.patch.object(terrain.os.path, "isfile", return_value=True),
            mock.patch.object(terrain, "_create_tile_mesh", return_value=new_mesh),
            mock.patch.object(
                terrain.bpy,
                "data",
                types.SimpleNamespace(meshes=meshes),
                create=True,
            ),
        ):
            self.assertTrue(terrain.rebuild_tile_mesh(obj, 5))

        self.assertEqual(new_mesh.materials, [material])
        self.assertNotIn("terrain_tile_source_signature", obj)
        self.assertEqual(obj["terrain_multires"], 5)
        meshes.remove.assert_called_once_with(old_mesh)


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
            mock.patch.object(terrain, "_import_resolved_terrain_tile", return_value=imported),
            mock.patch.object(terrain, "_apply_tile_detail_material") as apply_detail,
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
        apply_detail.assert_called_once()
        args = apply_detail.call_args.args
        self.assertIs(args[0], world_file)
        self.assertEqual(args[2].texture_buffer, texture_path)
        self.assertIs(args[4], imported_obj)


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
