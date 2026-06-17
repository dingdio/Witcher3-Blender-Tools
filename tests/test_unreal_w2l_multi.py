"""Blender-free tests for the multi-layer .w2l Unreal bundle builder
(``build_unreal_w2l_bundle_multi``) and the cross-layer placement dedupe."""

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


class _BlockDataObjectType:
    Mesh = 0
    RigidBody = 1
    Collision = 2
    PointLight = 3
    SpotLight = 4
    Invalid = 5
    Decal = 6


class _Enums:
    BlockDataObjectType = _BlockDataObjectType


def _install_pkg_stubs():
    pkg = sys.modules.get("witcher3_tools")
    if pkg is None or not getattr(pkg, "__path__", None):
        pkg = types.ModuleType("witcher3_tools")
        pkg.__path__ = [str(REPO_ROOT / "witcher3_tools")]
        pkg.__package__ = "witcher3_tools"
        sys.modules["witcher3_tools"] = pkg

    crw = types.ModuleType("witcher3_tools.CR2W")
    crw.__path__ = []
    helpers = types.ModuleType("witcher3_tools.CR2W.CR2W_helpers")
    helpers.Enums = _Enums
    crw.CR2W_helpers = helpers
    sys.modules["witcher3_tools.CR2W"] = crw
    sys.modules["witcher3_tools.CR2W.CR2W_helpers"] = helpers

    importers = types.ModuleType("witcher3_tools.importers")
    importers.__path__ = []
    import_mesh = types.ModuleType("witcher3_tools.importers.import_mesh")
    # Force the basename fallback in _layer_id_for_w2l (no depot rel resolution).
    import_mesh.get_repo_from_abs_path = lambda p: ""
    importers.import_mesh = import_mesh
    sys.modules["witcher3_tools.importers"] = importers
    sys.modules["witcher3_tools.importers.import_mesh"] = import_mesh


_install_pkg_stubs()

from witcher3_tools.unreal_export import terrain_unreal, w2l_placements  # noqa: E402


_IDENT_ROWS = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]


class _Vec:
    def __init__(self, x, y, z):
        self.x, self.y, self.z = x, y, z


class _Rot:
    def __init__(self, rows):
        (self.ax, self.ay, self.az), (self.bx, self.by, self.bz), (self.cx, self.cy, self.cz) = rows


class _Block:
    def __init__(self, block_type, mesh_index=0, pos=(0.0, 0.0, 0.0), rows=None):
        self.packedObjectType = block_type
        self.packedObject = types.SimpleNamespace(meshIndex=mesh_index)
        self.position = _Vec(*pos)
        self.rotationMatrix = _Rot(rows or _IDENT_ROWS)


class _Sector:
    def __init__(self, blocks, resources):
        self.BlockData = blocks
        self.Resources = [types.SimpleNamespace(pathHash=p) for p in resources]


class _Level:
    def __init__(self, sector, version=174):
        self.CSectorData = sector
        self.version = version


def _collected(actor_rels, instancer=None):
    """Minimal collect_w2l_placements output for _layer_placement_group tests."""
    actors = [
        {"name": rel.rsplit("/", 1)[-1], "asset_rel": rel, "matrix": np.identity(4)}
        for rel in actor_rels
    ]
    instancers = []
    if instancer is not None:
        rel, count = instancer
        instancers.append({"name": rel.rsplit("/", 1)[-1], "asset_rel": rel,
                           "matrices": [np.identity(4) for _ in range(count)]})
    return {"assets": {}, "actors": actors, "instancers": instancers, "skipped": {}}


class TestLayerPlacementGroup(unittest.TestCase):
    def test_filters_actors_by_gathered_assets(self):
        collected = _collected(["a/wall", "a/missing"])
        group = w2l_placements._layer_placement_group("l1", "l1", "lev", collected, {"a/wall"})
        self.assertIsNotNone(group)
        self.assertEqual([a["asset_path"] for a in group["actors"]], ["a/wall"])
        self.assertEqual(group["layer_id"], "l1")
        # transform was applied (dict with location/rotation/scale), not raw matrix
        self.assertIn("location", group["actors"][0]["transform"])

    def test_instancer_dropped_when_not_gathered(self):
        collected = _collected([], instancer=("a/barrel", 9))
        self.assertIsNone(w2l_placements._layer_placement_group("l", "l", "", collected, set()))
        group = w2l_placements._layer_placement_group("l", "l", "", collected, {"a/barrel"})
        self.assertEqual(len(group["instancers"]), 1)
        self.assertEqual(len(group["instancers"][0]["instances"]), 9)

    def test_returns_none_when_nothing_gathered(self):
        collected = _collected(["a/wall", "a/door"])
        self.assertIsNone(w2l_placements._layer_placement_group("l", "l", "", collected, set()))


# ---------------------------------------------------------------------------
# Full orchestration with stubbed heavy deps (gather / buffers / materials).
# ---------------------------------------------------------------------------

class _FakeMesh:
    submeshes = [object()]


class _ChainBuilder:
    def __init__(self, register):
        self.warnings = []
        self._n = 0

    def add_slot_material(self, mat_info, asset_dir):
        self._n += 1
        return f"mat{self._n}"

    def ordered_masters(self):
        return []

    def ordered_materials(self):
        return []


class _TextureRegistry:
    def __init__(self, bundle_root, parallel=True):
        self.warnings = []

    def register(self, *a, **k):
        return ""

    def manifest_entries(self):
        return []


def _make_settings(export_folder):
    return types.SimpleNamespace(
        asset_name="",
        content_root="/Game/Witcher3",
        export_folder=export_folder,
        placement_skip_materials=False,
    )


class TestMultiLayerBundle(unittest.TestCase):
    def setUp(self):
        _install_pkg_stubs()
        self.tmp = tempfile.mkdtemp()
        self.gather_calls = []
        self.depot_sources = {}

        # Map .w2l basenames -> parsed level. Two layers share "a\\wall.w2mesh".
        self.levels = {
            "layer1.w2l": _Level(_Sector(
                [_Block(_BlockDataObjectType.Mesh, 0), _Block(_BlockDataObjectType.Mesh, 1)],
                ["a\\wall.w2mesh", "a\\door.w2mesh"],
            )),
            "layer2.w2l": _Level(_Sector(
                [_Block(_BlockDataObjectType.Mesh, 0), _Block(_BlockDataObjectType.Mesh, 1)],
                ["a\\wall.w2mesh", "a\\tower.w2mesh"],
            )),
        }
        self.w2l_paths = []
        for name in ("layer1.w2l", "layer2.w2l"):
            p = os.path.join(self.tmp, name)
            with open(p, "wb") as fh:
                fh.write(b"w2l")
            self.w2l_paths.append(p)

        self._install_dep_stubs()

    def _install_dep_stubs(self):
        test = self

        reader = types.ModuleType("witcher3_tools.CR2W.CR2W_reader")
        reader.load_w2l = lambda path: test.levels[os.path.basename(path)]
        sys.modules["witcher3_tools.CR2W.CR2W_reader"] = reader

        common = types.ModuleType("witcher3_tools.CR2W.common_blender")

        def _repo_file(depot):
            src = test.depot_sources.get(depot)
            if src is None:
                src = os.path.join(test.tmp, "src_" + str(len(test.depot_sources)) + ".w2mesh")
                with open(src, "wb") as fh:
                    fh.write(b"mesh")
                test.depot_sources[depot] = src
            return src

        common.repo_file = _repo_file
        sys.modules["witcher3_tools.CR2W.common_blender"] = common

        gather = types.ModuleType("witcher3_tools.unreal_export.gather")

        def _gather(source, version=None, warnings=None):
            test.gather_calls.append(source)
            return _FakeMesh(), [{"name": "m", "material_slot_index": 0}]

        gather.gather_placement_mesh = _gather
        sys.modules["witcher3_tools.unreal_export.gather"] = gather

        mesh_buffer = types.ModuleType("witcher3_tools.unreal_export.mesh_buffer")

        def _write(path, mesh):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(b"W3BUF")

        mesh_buffer.write_mesh_buffer = _write
        sys.modules["witcher3_tools.unreal_export.mesh_buffer"] = mesh_buffer

        mc = types.ModuleType("witcher3_tools.unreal_export.material_chain")
        mc.ChainBuilder = _ChainBuilder
        sys.modules["witcher3_tools.unreal_export.material_chain"] = mc

        tex = types.ModuleType("witcher3_tools.unreal_export.texture_export")
        tex.TextureRegistry = _TextureRegistry
        sys.modules["witcher3_tools.unreal_export.texture_export"] = tex

        bundle = types.ModuleType("witcher3_tools.unreal_export.bundle")
        bundle.default_export_folder = lambda: test.tmp
        bundle._resolve_content_root_setting = lambda root, game: root or "/Game/Witcher3"
        bundle.overwrite_policy_from_settings = lambda settings: {}

        def _unique_bundle_file(bundle_root, asset_rel, used_stems, subdir, ext):
            base = asset_rel.rsplit("/", 1)[-1]
            stem = base
            counter = 2
            while used_stems.get(stem, asset_rel) != asset_rel:
                stem = f"{base}_{counter}"
                counter += 1
            used_stems[stem] = asset_rel
            out_dir = os.path.join(bundle_root, subdir)
            os.makedirs(out_dir, exist_ok=True)
            return os.path.join(out_dir, f"{stem}{ext}")

        bundle._unique_bundle_file = _unique_bundle_file
        sys.modules["witcher3_tools.unreal_export.bundle"] = bundle

    def test_shared_mesh_gathered_once_across_layers(self):
        result = w2l_placements.build_unreal_w2l_bundle_multi(None, _make_settings(self.tmp), self.w2l_paths)
        counts = result["counts"]
        self.assertEqual(counts["layers"], 2)
        self.assertEqual(counts["layers_with_placements"], 2)
        # wall + door + tower
        self.assertEqual(counts["unique_meshes"], 3)
        # wall appears in both layers but is gathered exactly once
        self.assertEqual(len(self.gather_calls), 3)
        self.assertEqual(len(set(self.gather_calls)), 3)
        # wall(2) + door + tower = 4 actors across the two layer groups
        self.assertEqual(counts["actors"], 4)

    def test_manifest_shape(self):
        result = w2l_placements.build_unreal_w2l_bundle_multi(None, _make_settings(self.tmp), self.w2l_paths)
        manifest = result["manifest"]
        self.assertEqual(len(manifest["meshes"]), 3)
        self.assertTrue(os.path.isfile(result["manifest_path"]))
        layers = manifest["placements"]["layers"]
        self.assertEqual(len(layers), 2)
        # each mesh entry carries a decoded buffer, not an FBX
        for mesh in manifest["meshes"]:
            self.assertIn("buffer", mesh)
            self.assertEqual(mesh["kind"], "static")
        self.assertEqual(sorted(result["layer_ids"]), ["layer1.w2l", "layer2.w2l"])

    def test_single_builder_preserves_layer_id(self):
        result = w2l_placements.build_unreal_w2l_bundle(None, _make_settings(self.tmp), self.w2l_paths[0])
        self.assertEqual(result["layer_id"], "layer1.w2l")
        self.assertEqual(result["counts"]["unique_meshes"], 2)

    def test_no_placements_raises(self):
        empty = os.path.join(self.tmp, "empty.w2l")
        with open(empty, "wb") as fh:
            fh.write(b"w2l")
        self.levels["empty.w2l"] = _Level(_Sector([], []))
        with self.assertRaises(ValueError):
            w2l_placements.build_unreal_w2l_bundle_multi(None, _make_settings(self.tmp), [empty])


if __name__ == "__main__":
    unittest.main()
