import contextlib
import math
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

def _axis_rot(axis, angle):
    c, s = math.cos(angle), math.sin(angle)
    if axis == 0:
        return np.array([[1, 0, 0], [0, c, -s], [0, s, c]], dtype=float)
    if axis == 1:
        return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]], dtype=float)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], dtype=float)


class _StubMatrix:
    def __init__(self, m4):
        self._m = np.asarray(m4, dtype=float).reshape(4, 4)

    def to_4x4(self):
        return self

    @property
    def translation(self):
        return self._m[:3, 3]

    @translation.setter
    def translation(self, value):
        self._m[:3, 3] = list(value)

    def __array__(self, dtype=None):
        return self._m if dtype is None else self._m.astype(dtype)


class _StubEuler:
    _AXIS = {"X": 0, "Y": 1, "Z": 2}

    def __init__(self, values, order="XYZ"):
        self.values = list(values)
        self.order = order

    def to_matrix(self):
        rot = np.identity(3, dtype=float)
        for letter in self.order:  # compose in order string left-to-right
            axis = self._AXIS[letter]
            rot = rot @ _axis_rot(axis, self.values[axis])
        m4 = np.identity(4, dtype=float)
        m4[:3, :3] = rot
        return _StubMatrix(m4)


_mathutils = types.ModuleType("mathutils")
_mathutils.Euler = _StubEuler
_mathutils.Vector = lambda seq: list(seq)
sys.modules["mathutils"] = _mathutils


if "witcher3_tools" not in sys.modules:
    _pkg = types.ModuleType("witcher3_tools")
    _pkg.__path__ = [str(REPO_ROOT / "witcher3_tools")]
    _pkg.__package__ = "witcher3_tools"
    sys.modules["witcher3_tools"] = _pkg

from witcher3_tools.unreal_export import foliage_bundle, speedtree_bundle, terrain_unreal, world_bundle
from witcher3_tools.unreal_export.manifest import depot_asset_rel


# --- fake .flyr CFoliageResource structures ---------------------------------

class _Inst:
    def __init__(self, x, y, z, yaw=0.0, pitch=0.0, roll=0.0):
        self.X, self.Y, self.Z = x, y, z
        self.Yaw, self.Pitch, self.Roll = yaw, pitch, roll


class SFoliageInstanceData:
    def __init__(self, x, y, z, scale=1.0, angle_degrees=0.0):
        self.X, self.Y, self.Z = x, y, z
        half_angle = math.radians(angle_degrees) * 0.5
        self.Yaw = scale
        self.Pitch = math.sin(half_angle)
        self.Roll = math.cos(half_angle)


class _Buffer:
    def __init__(self, elements):
        self.elements = list(elements)


class _TreeType:
    def __init__(self, depot):
        self.DepotPath = depot


class _TreeData:
    def __init__(self, depot, insts):
        self.TreeType = _TreeType(depot)
        self.TreeCollection = _Buffer(insts)


class _Foliage:
    def __init__(self, trees=(), grasses=None):
        self.Trees = _Buffer(trees)
        if grasses is not None:
            self.Grasses = _Buffer(grasses)


class _Level:
    def __init__(self, foliage):
        self.Foliage = foliage


PINE = r"environment\vegetation\trees\pine\pine.srt"
GRASS = r"environment\vegetation\grass\grass_a.srt"


def _settings(tmp):
    return types.SimpleNamespace(asset_name="", export_folder=str(Path(tmp) / "out"), content_root="")


def _context():
    return types.SimpleNamespace(
        scene=types.SimpleNamespace(witcher_file_browser=types.SimpleNamespace(loadmods=False)),
    )


def _fake_resolve_srt(context, srt_path, depot_path=""):
    return ("C:\\srt\\" + os.path.basename(str(srt_path)), depot_path or srt_path)


def _fake_build_entry(context, settings, abs_srt, resolved_depot, bundle_root, warnings, *, force_import=True):
    entry = {
        "asset_path": depot_asset_rel(resolved_depot),
        "depot_path": resolved_depot,
        "file": "SpeedTrees/" + os.path.basename(str(abs_srt)),
        "texture_files": [],
        "missing_textures": [],
        "force_import": bool(force_import),
        "import_options": {"create_materials": True},
    }
    return entry, {"requested": 2, "staged": 2, "missing": []}


@contextlib.contextmanager
def _noop_redkit(*args, **kwargs):
    yield


def _cr2w_stub_modules(level, exists=True):
    reader = types.ModuleType("witcher3_tools.CR2W.CR2W_reader")
    reader.load_foliage = lambda path: level
    common = types.ModuleType("witcher3_tools.CR2W.common_blender")
    common.redkit_repo_context = _noop_redkit
    common.win_path_exists = lambda p: bool(exists)
    common.repo_file = lambda p, *a, **k: p
    importers = types.ModuleType("witcher3_tools.importers")
    importers.__path__ = []
    import_mesh = types.ModuleType("witcher3_tools.importers.import_mesh")
    import_mesh.get_repo_from_abs_path = lambda p: os.path.basename(str(p))
    import_foliage = types.ModuleType("witcher3_tools.importers.import_foliage")
    import_foliage.get_game_rel_foliage_prefix = lambda world_path, context=None: ""
    import_foliage.find_all_flyr_keys_in_bundles = lambda prefix: {}
    import_foliage.resolve_flyr_abs_path = lambda p: p
    import_foliage.cell_key_from_path = lambda p: (
        os.path.splitext(os.path.basename(str(p)))[0].removeprefix("foliage_")
    )
    return {
        "witcher3_tools.CR2W.CR2W_reader": reader,
        "witcher3_tools.CR2W.common_blender": common,
        "witcher3_tools.importers": importers,
        "witcher3_tools.importers.import_mesh": import_mesh,
        "witcher3_tools.importers.import_foliage": import_foliage,
    }


class TestCollectFoliage(unittest.TestCase):
    def test_groups_tree_and_grass_instances_by_depot(self):
        foliage = _Foliage(
            trees=[_TreeData(PINE, [_Inst(1, 2, 3), _Inst(4, 5, 6)])],
            grasses=[_TreeData(GRASS, [_Inst(7, 8, 9)])],
        )
        warnings = []
        by_depot = foliage_bundle.collect_foliage_placements(foliage, warnings)

        self.assertEqual(set(by_depot), {PINE.lower(), GRASS.lower()})
        self.assertEqual(len(by_depot[PINE.lower()]["matrices"]), 2)
        self.assertEqual(len(by_depot[GRASS.lower()]["matrices"]), 1)
        self.assertEqual(by_depot[PINE.lower()]["depot"], PINE)

    def test_same_depot_across_buffers_merges(self):
        foliage = _Foliage(
            trees=[_TreeData(PINE, [_Inst(0, 0, 0)])],
            grasses=[_TreeData(PINE.upper(), [_Inst(1, 1, 1)])],
        )
        by_depot = foliage_bundle.collect_foliage_placements(foliage, [])
        self.assertEqual(len(by_depot), 1)
        self.assertEqual(len(by_depot[PINE.lower()]["matrices"]), 2)

    def test_empty_depot_is_skipped(self):
        foliage = _Foliage(trees=[_TreeData("", [_Inst(0, 0, 0)])])
        self.assertEqual(foliage_bundle.collect_foliage_placements(foliage, []), {})


class TestInstanceMatrix(unittest.TestCase):
    def test_translation_is_instance_position(self):
        inst = _Inst(10.0, -20.0, 5.0, yaw=30.0, pitch=10.0, roll=-15.0)
        mat = foliage_bundle._instance_world_matrix(inst)
        np.testing.assert_allclose(mat[:3, 3], [10.0, -20.0, 5.0], atol=1e-6)

    def test_zero_rotation_is_identity_basis(self):
        mat = foliage_bundle._instance_world_matrix(_Inst(1.0, 2.0, 3.0))
        np.testing.assert_allclose(mat[:3, :3], np.identity(3), atol=1e-6)

    def test_rotation_basis_is_orthonormal(self):
        mat = foliage_bundle._instance_world_matrix(_Inst(0, 0, 0, yaw=33.0, pitch=12.0, roll=-7.0))
        rot = mat[:3, :3]
        np.testing.assert_allclose(rot @ rot.T, np.identity(3), atol=1e-6)
        self.assertAlmostEqual(float(np.linalg.det(rot)), 1.0, places=5)

    def test_fast_world_transform_matches_flyr_matrix_path(self):
        inst = _Inst(10.0, -20.0, 5.0, yaw=33.0, pitch=12.0, roll=-7.0)
        expected = terrain_unreal.w3_matrix_to_unreal(foliage_bundle._instance_world_matrix(inst))
        actual = world_bundle._foliage_transform_to_unreal({
            "X": inst.X,
            "Y": inst.Y,
            "Z": inst.Z,
            "Yaw": inst.Yaw,
            "Pitch": inst.Pitch,
            "Roll": inst.Roll,
        })
        np.testing.assert_allclose(actual["location"], expected["location"], atol=1e-6)
        np.testing.assert_allclose(actual["rotation"], expected["rotation"], atol=1e-6)
        np.testing.assert_allclose(actual["scale"], expected["scale"], atol=1e-6)

    def test_packed_red_foliage_instance_preserves_scale(self):
        inst = SFoliageInstanceData(10.0, -20.0, 5.0, scale=1.35, angle_degrees=90.0)
        xform = terrain_unreal.w3_matrix_to_unreal(foliage_bundle._instance_world_matrix(inst))

        np.testing.assert_allclose(xform["scale"], [1.35, 1.35, 1.35], atol=1e-6)
        self.assertAlmostEqual(float(np.linalg.det(foliage_bundle._instance_world_matrix(inst)[:3, :3])), 1.35 ** 3, places=5)

    def test_fast_packed_transform_matches_matrix_path(self):
        inst = SFoliageInstanceData(10.0, -20.0, 5.0, scale=1.35, angle_degrees=90.0)
        expected = terrain_unreal.w3_matrix_to_unreal(foliage_bundle._instance_world_matrix(inst))
        actual = world_bundle._foliage_transform_to_unreal({
            "X": inst.X,
            "Y": inst.Y,
            "Z": inst.Z,
            "Yaw": 90.0,
            "Pitch": 0.0,
            "Roll": 0.0,
            "Scale_x": inst.Yaw,
            "Scale_y": inst.Yaw,
            "Scale_z": inst.Yaw,
            "Quat_z": inst.Pitch,
            "Quat_w": inst.Roll,
        })

        np.testing.assert_allclose(actual["location"], expected["location"], atol=1e-6)
        np.testing.assert_allclose(actual["rotation"], expected["rotation"], atol=1e-6)
        np.testing.assert_allclose(actual["scale"], expected["scale"], atol=1e-6)


class TestBuildFlyrBundle(unittest.TestCase):
    def _build(self, level, tmp, exists=True):
        with mock.patch.dict(sys.modules, _cr2w_stub_modules(level, exists=exists)), \
             mock.patch.object(speedtree_bundle, "_resolve_srt_source", _fake_resolve_srt), \
             mock.patch.object(speedtree_bundle, "build_speedtree_entry", _fake_build_entry):
            flyr = Path(tmp) / "foliage_320.00_192.00.flyr"
            flyr.write_bytes(b"CR2W_fake_flyr")
            return foliage_bundle.build_unreal_flyr_bundle(_context(), _settings(tmp), str(flyr))

    def test_emits_speedtrees_and_one_foliage_type_per_tree(self):
        level = _Level(_Foliage(
            trees=[_TreeData(PINE, [_Inst(1, 2, 3), _Inst(4, 5, 6, yaw=90.0)])],
            grasses=[_TreeData(GRASS, [_Inst(7, 8, 9)])],
        ))
        with tempfile.TemporaryDirectory() as tmp:
            result = self._build(level, tmp)

        manifest = result["manifest"]
        self.assertEqual(len(manifest["speedtrees"]), 2)
        # Foliage goes through the `foliage` section, NOT `placements`.
        self.assertNotIn("placements", manifest)
        cells = manifest["foliage"]["cells"]
        self.assertEqual(len(cells), 1)
        types = cells[0]["types"]
        self.assertEqual(len(types), 2)

        asset_paths = {t["asset_path"] for t in types}
        self.assertIn(depot_asset_rel(PINE), asset_paths)
        self.assertIn(depot_asset_rel(GRASS), asset_paths)
        # Every speedtree asset_path is referenced by a foliage type (FoliageType wraps it).
        self.assertEqual({s["asset_path"] for s in manifest["speedtrees"]}, asset_paths)
        self.assertTrue(all(not s["force_import"] for s in manifest["speedtrees"]))

        pine = next(t for t in types if t["asset_path"] == depot_asset_rel(PINE))
        self.assertEqual(len(pine["instances"]), 2)
        for inst in pine["instances"]:
            self.assertIn("location", inst)
            self.assertIn("rotation", inst)
            self.assertIn("scale", inst)
        self.assertEqual(result["counts"]["instances"], 3)
        self.assertEqual(result["counts"]["tree_types"], 2)

    def test_cell_carries_xy_bounds_for_resend_dedup(self):
        level = _Level(_Foliage(trees=[_TreeData(PINE, [_Inst(10, 20, 0), _Inst(-5, 8, 0)])]))
        with tempfile.TemporaryDirectory() as tmp:
            result = self._build(level, tmp)
        bounds = result["manifest"]["foliage"]["cells"][0]["bounds"]
        self.assertEqual(len(bounds["min"]), 2)
        self.assertEqual(len(bounds["max"]), 2)
        self.assertLessEqual(bounds["min"][0], bounds["max"][0])
        self.assertLessEqual(bounds["min"][1], bounds["max"][1])

    def test_repeated_type_dedupes_speedtree_but_keeps_all_instances(self):
        # The same .srt appears in two tree-type buffers (as it would across cells).
        level = _Level(_Foliage(
            trees=[_TreeData(PINE, [_Inst(0, 0, 0)]), _TreeData(PINE, [_Inst(1, 1, 1)])],
        ))
        with tempfile.TemporaryDirectory() as tmp:
            result = self._build(level, tmp)
        # collect_foliage_placements already merges by depot -> one type, one speedtree.
        self.assertEqual(len(result["manifest"]["speedtrees"]), 1)
        types = result["manifest"]["foliage"]["cells"][0]["types"]
        self.assertEqual(len(types), 1)
        self.assertEqual(len(types[0]["instances"]), 2)

    def test_missing_srt_warns_and_is_skipped(self):
        level = _Level(_Foliage(trees=[_TreeData(PINE, [_Inst(0, 0, 0)])]))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                self._build(level, tmp, exists=False)

    def test_no_instances_raises(self):
        level = _Level(_Foliage(trees=[]))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                self._build(level, tmp)

    def test_writes_manifest_file(self):
        level = _Level(_Foliage(trees=[_TreeData(PINE, [_Inst(0, 0, 0)])]))
        with tempfile.TemporaryDirectory() as tmp:
            result = self._build(level, tmp)
            self.assertTrue(os.path.isfile(result["manifest_path"]))


class TestWorldFoliageSections(unittest.TestCase):
    def test_discovers_source_foliage_cells_and_dedupes_speedtrees(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            world = root / "levels" / "prolog_village" / "prolog_village.w2w"
            source_foliage = world.parent / "source_foliage"
            source_foliage.mkdir(parents=True)
            world.write_bytes(b"CR2W_fake_world")
            cell_a = source_foliage / "foliage_0.00_0.00.flyr"
            cell_b = source_foliage / "foliage_64.00_0.00.flyr"
            cell_a.write_bytes(b"CR2W_fake_flyr_a")
            cell_b.write_bytes(b"CR2W_fake_flyr_b")

            levels = {
                str(cell_a): _Level(_Foliage(trees=[
                    _TreeData(PINE, [_Inst(1, 2, 3), _Inst(4, 5, 6)]),
                ])),
                str(cell_b): _Level(_Foliage(trees=[
                    _TreeData(PINE, [_Inst(7, 8, 9)]),
                    _TreeData(GRASS, [_Inst(10, 11, 12)]),
                ])),
            }

            modules = _cr2w_stub_modules(None, exists=True)
            modules["witcher3_tools.CR2W.CR2W_reader"].load_foliage = lambda path: levels[str(Path(path))]
            modules["witcher3_tools.importers.import_mesh"].get_repo_from_abs_path = (
                lambda p: os.path.relpath(str(p), str(root)).replace(os.sep, "\\")
            )
            build_calls = []

            def fake_build_entry(context, settings, abs_srt, resolved_depot, bundle_root, warnings, *, force_import=True):
                build_calls.append(resolved_depot)
                return _fake_build_entry(
                    context, settings, abs_srt, resolved_depot, bundle_root, warnings,
                    force_import=force_import,
                )

            warnings = []
            with mock.patch.dict(sys.modules, modules), \
                 mock.patch.object(speedtree_bundle, "_resolve_srt_source", _fake_resolve_srt), \
                 mock.patch.object(speedtree_bundle, "build_speedtree_entry", fake_build_entry):
                speedtrees, foliage, stats = world_bundle._build_world_foliage_sections(
                    _context(),
                    _settings(tmp),
                    str(world),
                    str(root / "out" / "prolog_village"),
                    "/Game/Witcher3",
                    warnings,
                )

        self.assertIsNotNone(foliage)
        self.assertEqual(stats["cells_found"], 2)
        self.assertEqual(stats["cells_exported"], 2)
        self.assertEqual(stats["instances"], 4)
        self.assertEqual(stats["tree_types"], 3)
        self.assertEqual(stats["speedtrees"], 2)
        self.assertEqual(len(speedtrees), 2)
        self.assertTrue(all(not entry["force_import"] for entry in speedtrees))
        self.assertEqual(len(foliage["cells"]), 2)
        self.assertEqual({entry["asset_path"] for entry in speedtrees}, {depot_asset_rel(PINE), depot_asset_rel(GRASS)})
        # Repeated PINE across cells should stage/inspect the .srt once.
        self.assertEqual(len(build_calls), 2)
        self.assertFalse(warnings)

    def test_cached_cells_skip_second_cr2w_parse(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            world = root / "levels" / "prolog_village" / "prolog_village.w2w"
            source_foliage = world.parent / "source_foliage"
            source_foliage.mkdir(parents=True)
            world.write_bytes(b"CR2W_fake_world")
            cell = source_foliage / "foliage_0.00_0.00.flyr"
            cell.write_bytes(b"CR2W_fake_flyr")
            level = _Level(_Foliage(trees=[_TreeData(PINE, [_Inst(1, 2, 3)])]))

            modules = _cr2w_stub_modules(None, exists=True)
            load_count = {"value": 0}

            def load_once(path):
                load_count["value"] += 1
                return level

            modules["witcher3_tools.CR2W.CR2W_reader"].load_foliage = load_once
            modules["witcher3_tools.importers.import_mesh"].get_repo_from_abs_path = (
                lambda p: os.path.relpath(str(p), str(root)).replace(os.sep, "\\")
            )

            with mock.patch.dict(sys.modules, modules), \
                 mock.patch.object(speedtree_bundle, "_resolve_srt_source", _fake_resolve_srt), \
                 mock.patch.object(speedtree_bundle, "build_speedtree_entry", _fake_build_entry):
                first_warnings = []
                _speedtrees, _foliage, first_stats = world_bundle._build_world_foliage_sections(
                    _context(), _settings(tmp), str(world), str(root / "out"), "/Game/Witcher3", first_warnings)

                modules["witcher3_tools.CR2W.CR2W_reader"].load_foliage = mock.Mock(
                    side_effect=AssertionError("cache miss")
                )
                second_warnings = []
                _speedtrees, _foliage, second_stats = world_bundle._build_world_foliage_sections(
                    _context(), _settings(tmp), str(world), str(root / "out"), "/Game/Witcher3", second_warnings)

        self.assertEqual(load_count["value"], 1)
        self.assertEqual(first_stats["cache_hits"], 0)
        self.assertEqual(second_stats["cache_hits"], 1)
        self.assertEqual(second_stats["cache_misses"], 0)
        self.assertFalse(second_warnings)

    def test_existing_unreal_speedtree_asset_stages_for_scale_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            world = root / "levels" / "prolog_village" / "prolog_village.w2w"
            source_foliage = world.parent / "source_foliage"
            source_foliage.mkdir(parents=True)
            world.write_bytes(b"CR2W_fake_world")
            cell = source_foliage / "foliage_0.00_0.00.flyr"
            cell.write_bytes(b"CR2W_fake_flyr")

            project = root / "TestProject" / "TestProject.uproject"
            asset_file = project.parent / "Content" / "Witcher3" / Path(depot_asset_rel(PINE) + ".uasset")
            asset_file.parent.mkdir(parents=True)
            project.write_text("{}", encoding="utf-8")
            asset_file.write_bytes(b"fake_uasset")

            settings = _settings(tmp)
            settings.unreal_project = str(project)
            modules = _cr2w_stub_modules(
                _Level(_Foliage(trees=[_TreeData(PINE, [_Inst(1, 2, 3)])])),
                exists=True,
            )
            modules["witcher3_tools.importers.import_mesh"].get_repo_from_abs_path = (
                lambda p: os.path.relpath(str(p), str(root)).replace(os.sep, "\\")
            )

            with mock.patch.dict(sys.modules, modules), \
                 mock.patch.object(speedtree_bundle, "_resolve_srt_source", _fake_resolve_srt), \
                 mock.patch.object(speedtree_bundle, "build_speedtree_entry", _fake_build_entry):
                warnings = []
                speedtrees, foliage, stats = world_bundle._build_world_foliage_sections(
                    _context(), settings, str(world), str(root / "out"), "/Game/Witcher3", warnings)

        self.assertIsNotNone(foliage)
        self.assertEqual(len(speedtrees), 1)
        self.assertTrue(speedtrees[0]["existing_project_asset"])
        self.assertFalse(speedtrees[0]["force_import"])
        self.assertIn("file", speedtrees[0])
        self.assertEqual(stats["speedtrees_reused"], 1)
        self.assertEqual(stats["textures_requested"], 2)
        self.assertEqual(stats["textures_staged"], 2)
        self.assertFalse(warnings)


if __name__ == "__main__":
    unittest.main()
