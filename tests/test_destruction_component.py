import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def _install_namespace_stub(qualified_name, package_path):
    if qualified_name in sys.modules:
        return
    module = types.ModuleType(qualified_name)
    module.__path__ = [str(package_path)]
    module.__package__ = qualified_name
    sys.modules[qualified_name] = module


_install_namespace_stub("witcher3_tools", REPO_ROOT / "witcher3_tools")
_install_namespace_stub("witcher3_tools.CR2W", REPO_ROOT / "witcher3_tools" / "CR2W")
_install_namespace_stub("witcher3_tools.importers", REPO_ROOT / "witcher3_tools" / "importers")

from witcher3_tools.CR2W import CR2W_file, CR2W_types, common_blender, fast_cache_scan  # noqa: E402
from witcher3_tools.importers import import_helpers  # noqa: E402


FENCE_BASE = (
    r"environment\architecture\human\skellige\ard_skellig\kaer_trolde"
    r"\thin_wood_fence\apex\fence_straight_b_dst.reddest"
)
FENCE_VISUAL = (
    r"environment\architecture\human\skellige\ard_skellig\kaer_trolde"
    r"\thin_wood_fence\apex\fence_straight_b_px.redapex"
)
FENCE_POSE = {
    "X": {"X": 1.0, "Y": 0.0, "Z": 0.0, "W": 0.0},
    "Y": {"X": 0.0, "Y": 1.0, "Z": 0.0, "W": 0.0},
    "Z": {"X": 0.0, "Y": 0.0, "Z": 1.0, "W": 0.0},
    "W": {"X": 0.0, "Y": 1.1, "Z": 0.0, "W": 1.0},
}
LEGACY_FENCE_TRANSFORM = {
    "X": 0.0,
    "Y": 0.0,
    "Z": 0.0,
    "Pitch": 0.0,
    "Yaw": 0.0,
    "Roll": 180.0,
    "Scale_x": 1.0,
    "Scale_y": 1.0,
    "Scale_z": 1.0,
}


class DestructionComponentTests(unittest.TestCase):
    def test_fresh_reddest_extract_includes_cooked_buffer(self):
        base_item = object()
        buffer_item = object()

        def find_item(path):
            if path == FENCE_BASE:
                return [base_item]
            if path == f"{FENCE_BASE}.1.buffer":
                return [buffer_item]
            return []

        manager = SimpleNamespace(Items={}, find_item_by_hash=Mock(side_effect=find_item))

        self.assertEqual(
            common_blender._collect_bundle_extract_items(manager, FENCE_BASE),
            [(FENCE_BASE, [base_item]), (f"{FENCE_BASE}.1.buffer", [buffer_item])],
        )

    def test_reddest_buffer_is_extracted_from_cooked_bundle(self):
        class BundleItem:
            def extract_to_file(self, path):
                Path(path).write_bytes(b"cooked buffer")

        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "environment" / "test.reddest"
            source.parent.mkdir()
            source.write_bytes(b"CR2W")
            manager = SimpleNamespace(find_item_by_hash=Mock(return_value=[BundleItem()]))

            with (
                patch.object(
                    common_blender,
                    "_get_repo_roots_from_prefs",
                    return_value=("", root, "", False),
                ),
                patch.object(common_blender, "LoadBundleManager", return_value=manager, create=True),
            ):
                extracted = common_blender.extract_missing_buffers(str(source), required_index=1)

            relative_buffer = os.path.relpath(source, root) + ".1.buffer"
            manager.find_item_by_hash.assert_called_once_with(relative_buffer)
            self.assertEqual(extracted, {1})
            self.assertEqual(Path(f"{source}.1.buffer").read_bytes(), b"cooked buffer")

    def test_updated_entity_and_destruction_component_reach_full_parser(self):
        chunk = SimpleNamespace(
            Type="W3UpdatedEntity",
            PROPS=[],
            GetVariableByName=lambda name: object() if name == "streamingDataBuffer" else None,
        )

        self.assertTrue(CR2W_types.is_entity_chunk(chunk))
        self.assertIn("CDestructionComponent", CR2W_file._ENTITY_DIRECT_COMPONENT_TYPES)

    def test_outer_layer_cloth_and_destruction_components_are_supported(self):
        for component_type in ("CClothComponent", "CDestructionSystemComponent"):
            with self.subTest(component_type=component_type):
                entity = CR2W_file.CEntity()
                root = SimpleNamespace(name="CGameplayEntity", Components=[])
                component = SimpleNamespace(name=component_type, Type=component_type)

                CR2W_file._append_entity_components(
                    entity,
                    root,
                    [root, component],
                    {1: [(2, component)]},
                    "layer.w2l",
                    1,
                )

                self.assertEqual(entity.Components, [component])
                self.assertEqual(entity.unsupportedComponents, [])

    def test_fast_scanner_routes_native_reddest_and_ignores_physics_pose(self):
        props = {
            "name": "CDestructionSystemComponentfence_straight_b_px",
            "m_baseResource": FENCE_BASE,
            "parameters.m_pose": FENCE_POSE,
        }
        cr2w_file = SimpleNamespace(HEADER=SimpleNamespace(version=164))
        with patch.object(fast_cache_scan, "_scan_selected_props", return_value=props):
            item = fast_cache_scan._scan_component_export(
                cr2w_file,
                object(),
                "CDestructionComponent",
                0,
                as_stream=False,
            )

        self.assertIn("CDestructionComponent", fast_cache_scan._DIRECT_COMPONENT_TYPES)
        self.assertIn("CDestructionComponent", fast_cache_scan._STREAM_COMPONENT_TYPES)
        self.assertEqual(item["kind"], "component_mesh")
        self.assertEqual(item["repo_path"], FENCE_BASE)
        self.assertIsNone(item["matrix"])
        self.assertIsNone(item["transform"])
        self.assertIsNone(item["translation"])
        self.assertIsNone(item["local_position"])
        self.assertEqual(item["component_type"], "CDestructionComponent")
        self.assertNotIn("parameters.m_pose", fast_cache_scan._TARGET_PROP_NAMES)

    def test_fast_scanner_preserves_real_component_transform(self):
        transform = {
            "X": 0.422492,
            "Y": 0.0,
            "Z": -0.227788,
            "Pitch": 0.0,
            "Yaw": 0.0,
            "Roll": 0.0,
            "Scale_x": 1.0,
            "Scale_y": 1.0,
            "Scale_z": 1.0,
        }
        props = {
            "name": "CDestructionSystemComponentfence_straight_b_px",
            "m_baseResource": FENCE_BASE,
            "parameters.m_pose": FENCE_POSE,
            "transform": transform,
        }
        with patch.object(fast_cache_scan, "_scan_selected_props", return_value=props):
            item = fast_cache_scan._scan_component_export(
                SimpleNamespace(HEADER=SimpleNamespace(version=164)),
                object(),
                "CDestructionComponent",
                0,
                as_stream=False,
            )

        self.assertEqual(item["transform"], transform)
        self.assertEqual(item["local_position"], (0.422492, 0.0, -0.227788))
        self.assertIsNone(item["matrix"])

    def test_fast_scanner_preserves_legacy_redapex_component(self):
        props = {
            "name": "CDestructionSystemComponentfence_straight_b_px",
            "m_resource": FENCE_VISUAL,
            "transform": LEGACY_FENCE_TRANSFORM,
        }
        with patch.object(fast_cache_scan, "_scan_selected_props", return_value=props):
            item = fast_cache_scan._scan_component_export(
                SimpleNamespace(HEADER=SimpleNamespace(version=163)),
                object(),
                "CDestructionSystemComponent",
                0,
                as_stream=True,
            )

        self.assertEqual(item["kind"], "cloth")
        self.assertEqual(item["repo_path"], FENCE_VISUAL)
        self.assertEqual(item["transform"], LEGACY_FENCE_TRANSFORM)
        self.assertEqual(item["local_position"], (0.0, 0.0, 0.0))
        self.assertIsNone(item["matrix"])
        self.assertEqual(item["component_type"], "CDestructionSystemComponent")

    def test_static_component_path_uses_native_destruction_resource(self):
        component = SimpleNamespace(name="CDestructionComponent")
        with (
            patch.object(import_helpers, "_w2_embedded_mesh_ref_info", return_value=(None, None)),
            patch.object(
                import_helpers,
                "_resolve_repo_path",
                side_effect=lambda _chunk, prop_name, extension: (
                    FENCE_BASE
                    if (prop_name, extension) == ("m_baseResource", ".reddest")
                    else None
                ),
            ),
        ):
            self.assertEqual(
                import_helpers._resolve_static_mesh_chunk_path(component),
                (FENCE_BASE, None),
            )

    def test_stream_scanner_forwards_dependency_resolution(self):
        resolver = object()
        loader = object()
        nested_scan = Mock(return_value={"sector_items": []})
        with (
            patch.object(fast_cache_scan.CR2W_types, "getCR2W", return_value=object()),
            patch.object(fast_cache_scan, "_supports_fast_scan", return_value=True),
            patch.object(fast_cache_scan, "_scan_cr2w_structure", nested_scan),
        ):
            scan = fast_cache_scan._scan_stream_buffer(
                b"prefixCR2Wpayload",
                "stream",
                dependency_resolver=resolver,
                dependency_loader=loader,
            )
            self.assertEqual((scan or {}).get("sector_items"), [])

        self.assertIs(nested_scan.call_args.kwargs["dependency_resolver"], resolver)
        self.assertIs(nested_scan.call_args.kwargs["dependency_loader"], loader)

    def test_stream_scanner_propagates_rich_graph_requirement(self):
        rich_scan = {
            "sector_items": [],
            "requires_rich_entity": True,
        }
        with (
            patch.object(fast_cache_scan.CR2W_types, "getCR2W", return_value=object()),
            patch.object(fast_cache_scan, "_supports_fast_scan", return_value=True),
            patch.object(fast_cache_scan, "_scan_cr2w_structure", return_value=rich_scan),
        ):
            self.assertTrue(
                fast_cache_scan._scan_stream_buffer(
                    b"prefixCR2Wpayload",
                    "stream",
                )["requires_rich_entity"]
            )

    def test_fast_template_scan_preserves_native_entity_class(self):
        cr2w_file = SimpleNamespace(
            CR2WExport=[
                SimpleNamespace(name="CEntityTemplate"),
                SimpleNamespace(name="CItemEntity"),
            ],
        )
        with (
            patch.object(fast_cache_scan, "_scan_selected_props", return_value={}),
            patch.object(fast_cache_scan, "_scan_embedded_template_data", return_value=None),
        ):
            template_scan = fast_cache_scan._scan_template_export(
                cr2w_file,
                Mock(),
                0,
                "items\\sword.w2ent",
            )

        merged_scan = fast_cache_scan._new_scan_result()
        fast_cache_scan._merge_scan_result(merged_scan, template_scan)

        self.assertEqual(template_scan["entity_class"], "CItemEntity")
        self.assertEqual(merged_scan["entity_class"], "CItemEntity")


if __name__ == "__main__":
    unittest.main()
