import re
import sys
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch

from _helpers import REPO_ROOT, exec_functions, install_cr2w_stubs


IMPORTER_PATH = REPO_ROOT / "witcher3_tools" / "importers" / "import_blender_fun.py"
sys.path.insert(0, str(REPO_ROOT))
install_cr2w_stubs()

from witcher3_tools.CR2W import fast_cache_scan  # noqa: E402


def _load_cached_filter_helpers():
    return exec_functions(
        IMPORTER_PATH,
        {
            "_cached_plan_items_by_id",
            "_cached_plan_parent_chain",
            "_cached_plan_nearest_entity_parent",
            "_cached_plan_item_matches_regex",
            "_cached_plan_item_enabled_by_import_options",
            "cached_plan_filter_items_for_import_options",
        },
        {
            "re": re,
            "log": SimpleNamespace(warning=lambda *_args, **_kwargs: None),
            "_CACHED_FULL_PARENT_ITEM_KINDS": frozenset({"group", "entity"}),
            "_CACHED_REDCLOTH_ITEM_KINDS": frozenset({"cloth"}),
            "_CACHED_FULL_EMPTY_ITEM_KINDS": frozenset({"component_empty", "entity_empty"}),
            "_cached_plan_item_is_proxy_mesh": lambda _item: False,
            "_proxy_mesh_filter_active": lambda _kwargs: False,
            "_cloth_resource_enabled_for_import": lambda *_args, **_kwargs: True,
        },
    )


def _empty_entity_scan(**overrides):
    entity = {
        "name": "Placed Entity",
        "type": "CGameplayEntity",
        "transform": None,
        "template_path": "",
        "template": None,
        "stream_items": [],
        "component_indices": [],
        "components": [],
        "action_name": "",
        "guid": "",
        "entity_id": "",
        "engine_visible": None,
        "template_dependency_paths": [],
        "streaming_distance": 0.0,
        "unresolved_dependencies": [],
        "requires_rich_entity": False,
    }
    entity.update(overrides)
    return entity


def _structure_scan(exports, *, path="layer.w2l", stream_only=False, patches=None):
    cr2w_file = SimpleNamespace(
        CR2WExport=[SimpleNamespace(name=name, parentID=parent) for name, parent in exports],
    )
    with ExitStack() as stack:
        for name, value in (patches or {}).items():
            stack.enter_context(patch.object(fast_cache_scan, name, return_value=value))
        return fast_cache_scan._scan_cr2w_structure(
            cr2w_file,
            Mock(),
            path,
            stream_only=stream_only,
        )


class EntityFastScanRegressionTests(unittest.TestCase):
    def test_instance_metadata_reaches_plan_and_buffer_v2_forces_rich_fallback(self):
        props = {
            "name": "placed_torch",
            "template": r"environment\templates\torch.w2ent",
            "guid": "01234567-89ab-cdef-0123-456789abcdef",
            "id": "entity-42",
            "visible": False,
        }
        template_scan = fast_cache_scan._new_scan_result()
        template_scan["entity_class"] = "CGameplayEntity"
        cr2w_file = SimpleNamespace(
            HEADER=SimpleNamespace(version=159),
            CR2WExport=[SimpleNamespace(name="CGameplayEntity", parentID=0)],
        )
        handle = Mock()
        handle.tell.return_value = 0

        with (
            patch.object(fast_cache_scan, "_open_export", return_value=(0, 128)),
            patch.object(fast_cache_scan, "_scan_selected_props", return_value=props),
            patch.object(fast_cache_scan.CR2W_types, "_entity_payload_start", return_value=0),
            patch.object(fast_cache_scan.CR2W_types, "_read_entity_buffer_v1_safe", return_value=[]),
            patch.object(
                fast_cache_scan.CR2W_types,
                "_read_entity_buffer_v2_safe",
                return_value=[SimpleNamespace(componentName="torch")],
            ) as read_buffer_v2,
        ):
            scan = fast_cache_scan._scan_cr2w_structure(
                cr2w_file,
                handle,
                "layer.w2l",
                dependency_resolver=lambda path, *_args: path,
                dependency_loader=lambda _path: template_scan,
            )

        entity = scan["entities"][0]
        read_buffer_v2.assert_called_once()
        self.assertTrue(scan["requires_rich_entity"])
        self.assertTrue(entity["requires_rich_entity"])
        self.assertEqual(entity["guid"], props["guid"])
        self.assertEqual(entity["entity_id"], props["id"])
        self.assertIs(entity["engine_visible"], False)

        entry = fast_cache_scan._build_cache_entry("layer.w2l", "layer.w2l", 1.0, 100, scan)
        self.assertTrue(entry["requires_rich_entity"])
        plan_item = next(item for item in entry["items"] if item["kind"] == "entity_empty")

        self.assertEqual(plan_item["entity_guid"], props["guid"])
        self.assertEqual(plan_item["entity_id"], props["id"])
        self.assertEqual(plan_item["instance_id"], props["guid"])
        self.assertIs(plan_item["engine_visible"], False)

    def test_simple_template_includes_stay_on_compact_path(self):
        cr2w_file = SimpleNamespace(
            HEADER=SimpleNamespace(version=159),
            CR2WExport=[SimpleNamespace(name="CEntityTemplate", parentID=0)],
        )
        handle = Mock()
        handle.tell.return_value = 0
        child_scan = fast_cache_scan._new_scan_result()

        with (
            patch.object(
                fast_cache_scan,
                "_scan_selected_props",
                return_value={
                    "includes": [r"templates\child.w2ent"],
                    fast_cache_scan._RICH_ENTITY_PROP_MARKER: ["templateParams"],
                },
            ),
            patch.object(fast_cache_scan, "_scan_embedded_template_data", return_value=None),
            patch.object(
                fast_cache_scan,
                "_resolve_dependency_path",
                return_value=r"C:\templates\child.w2ent",
            ),
        ):
            scan = fast_cache_scan._scan_template_export(
                cr2w_file,
                handle,
                0,
                "root.w2ent",
                dependency_loader=lambda _path: child_scan,
            )

        self.assertFalse(scan["requires_rich_entity"])
        self.assertEqual(scan["includes"], [child_scan])
        self.assertEqual(
            scan["dependency_paths"],
            [r"templates\child.w2ent"],
        )

    def test_template_appearance_data_still_forces_rich_fallback(self):
        cr2w_file = SimpleNamespace(
            HEADER=SimpleNamespace(version=159),
            CR2WExport=[SimpleNamespace(name="CEntityTemplate", parentID=0)],
        )
        with (
            patch.object(
                fast_cache_scan,
                "_scan_selected_props",
                return_value={
                    fast_cache_scan._RICH_ENTITY_PROP_MARKER: ["appearances"],
                },
            ),
            patch.object(fast_cache_scan, "_scan_embedded_template_data", return_value=None),
        ):
            scan = fast_cache_scan._scan_template_export(
                cr2w_file,
                Mock(),
                0,
                "character.w2ent",
            )

        self.assertTrue(scan["requires_rich_entity"])

    def test_structure_scan_forces_rich_fallback_only_for_unknown_graphs(self):
        open_export = {"_open_export": (0, 0)}
        cases = [
            (
                "inventory and loot exports stay compact",
                [
                    ("CEntityTemplate", 0),
                    ("CGameplayEntity", 1),
                    ("CInventoryComponent", 2),
                    ("CR4LootParam", 1),
                    ("CInventoryInitializerUniform", 1),
                ],
                {
                    **open_export,
                    "_scan_template_export": fast_cache_scan._new_scan_result(),
                    "_scan_entity_export": _empty_entity_scan(),
                    "_scan_selected_props": {},
                },
                {"path": "container.w2ent"},
                False,
                (),
            ),
            (
                "animated template export",
                [("CEntityTemplate", 0), ("CAnimatedComponent", 1)],
                {
                    **open_export,
                    "_scan_template_export": fast_cache_scan._new_scan_result(),
                    "_scan_selected_props": {},
                },
                {"path": "animated.w2ent"},
                True,
                (),
            ),
            (
                "unknown entity-owned direct child",
                [("CLayer", 0), ("CGameplayEntity", 1), ("CUnknownVisualComponent", 2)],
                {**open_export, "_scan_entity_export": _empty_entity_scan()},
                {},
                True,
                (),
            ),
            (
                "unknown nested entity component",
                [
                    ("CLayer", 0),
                    ("CGameplayEntity", 1),
                    ("CStaticMeshComponent", 2),
                    ("CUnknownVisualComponent", 3),
                ],
                {
                    **open_export,
                    "_scan_entity_export": _empty_entity_scan(),
                    "_scan_component_export": {"kind": "component_mesh", "name": "mesh"},
                },
                {},
                True,
                (),
            ),
            (
                "unknown direct layer entity",
                [("CLayer", 0), ("CModCustomEntity", 1)],
                dict(open_export),
                {"path": "custom_layer.w2l"},
                True,
                ("entities",),
            ),
            (
                "unknown standalone w2ent root",
                [("CModCustomEntity", 0)],
                {},
                {"path": r"C:\mods\custom.w2ent"},
                True,
                ("entities",),
            ),
            (
                "unknown stream component",
                [("CMimicComponent", 0)],
                dict(open_export),
                {"path": "stream", "stream_only": True},
                True,
                (),
            ),
            (
                "recognized stream component without plan data",
                [("CStaticMeshComponent", 0)],
                {**open_export, "_scan_component_export": None},
                {"path": "stream", "stream_only": True},
                True,
                (),
            ),
            (
                "recognized direct component without plan data",
                [("CStaticMeshComponent", 0)],
                {**open_export, "_scan_component_export": None},
                {"path": "component.w2mesh"},
                True,
                (),
            ),
        ] + [
            (
                f"{component_type} stream component",
                [(component_type, 0)],
                dict(open_export),
                {"path": "stream", "stream_only": True},
                True,
                ("sector_items",),
            )
            for component_type in (
                "CRigidMeshComponent",
                "CDressMeshComponent",
                "CFurComponent",
                "CMorphedMeshComponent",
                "CWindowComponent",
            )
        ]

        for label, exports, patches, kwargs, requires_rich, empty_keys in cases:
            with self.subTest(label):
                scan = _structure_scan(exports, patches=patches, **kwargs)
                self.assertEqual(bool(scan["requires_rich_entity"]), requires_rich)
                for key in empty_keys:
                    self.assertEqual(scan[key], [])

    def test_cached_regex_matches_placed_entity_parent_only(self):
        filter_items = _load_cached_filter_helpers()[
            "cached_plan_filter_items_for_import_options"
        ]
        items = [
            {
                "id": "entity-1",
                "kind": "entity",
                "name": "Placed Torch 42",
                "repo_path": r"levels\novigrad\placed_torch",
                "parent_id": "",
            },
            {
                "id": "asset-1",
                "kind": "entity_asset",
                "name": "Generic Torch Template",
                "repo_path": r"environment\templates\generic_torch.w2ent",
                "parent_id": "entity-1",
            },
        ]

        matching = filter_items(
            items,
            {
                "do_import_Entity": True,
                "do_enable_name_filter": True,
                "do_name_filter_regex": r"Placed Torch 42",
            },
        )
        self.assertEqual([item["id"] for item in matching], ["entity-1", "asset-1"])

        for child_only_pattern in (r"Generic Torch Template", r"generic_torch\.w2ent"):
            with self.subTest(pattern=child_only_pattern):
                filtered = filter_items(
                    items,
                    {
                        "do_import_Entity": True,
                        "do_enable_name_filter": True,
                        "do_name_filter_regex": child_only_pattern,
                    },
                )
                self.assertEqual(filtered, [])


if __name__ == "__main__":
    unittest.main()
