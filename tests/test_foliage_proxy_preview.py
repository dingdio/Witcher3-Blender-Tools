import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_import_foliage_without_blender():
    package_name = "_w3_foliage_preview_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(REPO_ROOT / "witcher3_tools")]
    importers = types.ModuleType(package_name + ".importers")
    importers.__path__ = [str(REPO_ROOT / "witcher3_tools" / "importers")]
    fake_bpy = types.ModuleType("bpy")

    core_name = package_name + ".foliage_core"
    core_spec = importlib.util.spec_from_file_location(
        core_name,
        REPO_ROOT / "witcher3_tools" / "foliage_core.py",
    )
    core = importlib.util.module_from_spec(core_spec)

    module_name = package_name + ".importers.import_foliage"
    module_spec = importlib.util.spec_from_file_location(
        module_name,
        REPO_ROOT / "witcher3_tools" / "importers" / "import_foliage.py",
    )
    module = importlib.util.module_from_spec(module_spec)

    modules = {
        package_name: package,
        package_name + ".importers": importers,
        core_name: core,
        module_name: module,
        "bpy": fake_bpy,
    }
    with mock.patch.dict(sys.modules, modules):
        core_spec.loader.exec_module(core)
        module_spec.loader.exec_module(module)
    return module


foliage = _load_import_foliage_without_blender()


class TestFoliageProxyClassification(unittest.TestCase):
    def test_common_depot_paths_choose_visual_class(self):
        cases = {
            r"environment\vegetation\grass\grass_a.srt": "grass",
            r"environment\vegetation\flowers\poppy_01.srt": "flower",
            r"environment\vegetation\water\cattail_02.srt": "reed",
            r"environment\vegetation\bushes\bramble_01.srt": "shrub",
            r"environment\vegetation\trees\pine\pine_01.srt": "conifer",
            r"environment\vegetation\trees\birch\birch_01.srt": "tree",
        }
        for depot_path, expected in cases.items():
            with self.subTest(depot_path=depot_path):
                self.assertEqual(foliage._foliage_proxy_kind(depot_path), expected)

    def test_unknown_foliage_is_a_shrub_not_a_diagnostic_marker(self):
        self.assertEqual(foliage._foliage_proxy_kind(r"custom\mystery.srt"), "shrub")

    def test_trees_import_before_ground_cover(self):
        paths = ["grass_a.srt", "trees\\oak.srt", "flowers\\poppy.srt", "trees\\pine.srt"]
        ordered = sorted(paths, key=foliage._source_import_order)
        self.assertEqual(ordered[:2], ["trees\\oak.srt", "trees\\pine.srt"])

    def test_viewport_density_applies_only_to_generic_grass(self):
        self.assertTrue(foliage._is_generic_grass("grass.srt"))
        self.assertFalse(foliage._is_generic_grass("flowers\\poppy.srt"))
        self.assertFalse(foliage._is_generic_grass("bushes\\bramble.srt"))
        self.assertFalse(foliage._is_generic_grass("trees\\oak.srt"))
        self.assertFalse(foliage._is_generic_grass("trees\\pine.srt"))

    def test_viewport_density_threshold_is_explicit_percentage(self):
        self.assertEqual(foliage._viewport_density_threshold(0.01), 0.5)
        self.assertEqual(foliage._viewport_density_threshold(0.25), 24.5)
        self.assertEqual(foliage._viewport_density_threshold(1.0), 99.5)


class TestFoliageProxyVisibility(unittest.TestCase):
    def test_technical_source_is_disabled_in_viewport_and_render(self):
        class FakeSource:
            hide_viewport = False
            hide_render = False
            hide_select = False
            hidden = False

            def hide_set(self, value):
                self.hidden = value

        source = FakeSource()
        foliage._hide_foliage_source(source)
        self.assertTrue(source.hide_viewport)
        self.assertTrue(source.hide_render)
        self.assertTrue(source.hide_select)
        self.assertTrue(source.hidden)

    def test_only_real_source_instancers_are_visible(self):
        class FakeInstancer(dict):
            hide_viewport = False
            hide_render = False
            hide_select = False
            hidden = False

            def hide_set(self, value):
                self.hidden = value

        fallback = {foliage._SOURCE_KIND_PROP: foliage.FOLIAGE_SOURCE_MODE_PROXY}
        hydrated = {
            foliage._SOURCE_KIND_PROP: foliage.FOLIAGE_SOURCE_MODE_FULL,
            foliage._SOURCE_READY_PROP: True,
        }
        instancer = FakeInstancer()
        self.assertTrue(foliage._sync_instancer_source_visibility(instancer, fallback))
        self.assertTrue(instancer.hide_viewport)
        self.assertTrue(instancer.hide_render)
        self.assertTrue(instancer.hidden)

        self.assertFalse(foliage._sync_instancer_source_visibility(instancer, hydrated))
        self.assertFalse(instancer.hide_viewport)
        self.assertFalse(instancer.hide_render)
        self.assertFalse(instancer.hidden)

    def test_failed_rebuild_restores_previous_geometry_and_owner_metadata(self):
        root = {foliage._OWNER_KEYS_PROP: '["owner:old"]'}
        previous = {"owner:old": {"grass.srt": [(1.0,)]}}
        candidate = {"owner:new": {"grass.srt": [(2.0,)]}}

        def rebuild_side_effect(_root, transforms, *_args, **_kwargs):
            _root[foliage._OWNER_KEYS_PROP] = '["mutated"]'
            if transforms is candidate:
                raise RuntimeError("candidate rebuild failed")

        with mock.patch.object(
            foliage,
            "_rebuild_depot_types",
            side_effect=rebuild_side_effect,
        ) as rebuild:
            with self.assertRaisesRegex(RuntimeError, "candidate rebuild failed"):
                foliage._rebuild_depot_types_transactionally(
                    root,
                    previous,
                    candidate,
                    {"grass.srt"},
                )

        self.assertEqual(root[foliage._OWNER_KEYS_PROP], '["owner:old"]')
        self.assertIs(rebuild.call_args_list[1].args[1], previous)
        self.assertFalse(rebuild.call_args_list[1].kwargs["import_sources"])


class TestFoliageViewportDefaults(unittest.TestCase):
    def test_viewport_defaults_favor_viewport_performance(self):
        scene = types.SimpleNamespace(witcher_file_browser=None)
        settings = foliage._foliage_viewport_settings(scene)
        self.assertTrue(settings["cull_enabled"])
        self.assertTrue(settings["ground_density_enabled"])


if __name__ == "__main__":
    unittest.main()
