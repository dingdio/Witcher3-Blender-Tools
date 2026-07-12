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

    def test_viewer_priority_is_bounded_and_uses_instance_count(self):
        transforms = {
            "owner:a": {
                **{f"grass_{index}.srt": [()] * (100 - index) for index in range(10)},
                **{f"trees\\oak_{index}.srt": [()] * (50 - index) for index in range(8)},
            },
            "owner:b": {
                "grass_9.srt": [()] * 200,
                "trees\\oak_7.srt": [()] * 200,
            },
        }
        selected = foliage._viewer_source_priority(transforms)
        ground = [path for path in selected if "grass" in path]
        trees = [path for path in selected if "trees" in path]
        self.assertEqual(len(ground), 8)
        self.assertEqual(len(trees), 6)
        self.assertIn("grass_9.srt", selected)
        self.assertIn(r"trees\oak_7.srt", selected)
        self.assertNotIn("grass_8.srt", selected)
        self.assertNotIn(r"trees\oak_6.srt", selected)


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

    def test_viewer_budget_switches_every_non_priority_instancer_to_one_placeholder(self):
        transforms = {
            "owner": {
                **{f"grass_{index}.srt": [()] * (100 - index) for index in range(10)},
                **{f"trees\\oak_{index}.srt": [()] * (50 - index) for index in range(8)},
            }
        }

        class FakeInstancer(dict):
            pass

        objects = []
        for depot_path in transforms["owner"]:
            objects.append(FakeInstancer(_is_foliage_instancer=True, _depot_path="_inst_" + depot_path))
        root = types.SimpleNamespace(objects=objects)
        placeholder = object()
        switched = []
        hydration = foliage.FoliageHydrationResult((), (), (), ())
        with (
            mock.patch.object(foliage, "_get_root_transform_bucket", return_value=transforms),
            mock.patch.object(foliage, "_get_or_create_proxy_source", return_value=placeholder) as proxy,
            mock.patch.object(
                foliage,
                "_set_instancer_source",
                side_effect=lambda obj, source: switched.append((obj, source)),
            ),
            mock.patch.object(
                foliage,
                "hydrate_missing_foliage_sources",
                return_value=hydration,
            ) as hydrate,
        ):
            self.assertIs(foliage.apply_viewer_source_budget(root), hydration)

        requested = set(hydrate.call_args.kwargs["depot_paths"])
        self.assertEqual(len(requested), 14)
        self.assertEqual(len(switched), 4)
        self.assertTrue(all(obj["_depot_path"][6:] not in requested for obj, _source in switched))
        self.assertTrue(all(source is placeholder for _obj, source in switched))
        proxy.assert_called_once_with(root)

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
                    source_mode=foliage.FOLIAGE_SOURCE_MODE_FULL,
                )

        self.assertEqual(root[foliage._OWNER_KEYS_PROP], '["owner:old"]')
        self.assertIs(rebuild.call_args_list[1].args[1], previous)
        self.assertEqual(
            rebuild.call_args_list[1].kwargs["source_mode"],
            foliage.FOLIAGE_SOURCE_MODE_PROXY,
        )


if __name__ == "__main__":
    unittest.main()
