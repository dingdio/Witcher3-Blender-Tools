import hashlib
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from _helpers import exec_functions


IMPORTER_PATH = (
    Path(__file__).resolve().parents[1]
    / "witcher3_tools"
    / "importers"
    / "import_blender_fun.py"
)


def _load_plan_helpers():
    return exec_functions(
        IMPORTER_PATH,
        {
            "_new_level_import_plan",
            "_add_level_import_plan_item",
            "_remove_level_import_plan_item",
            "_compose_world_position",
            "entity_import_plan_hash",
            "_entity_import_plan_preflight",
            "_entity_import_plan_preflight_errors",
            "_entity_plan_skip_ids",
            "_entity_asset_clone_key",
            "_embedded_entity_data_has_complete_appearance_templates",
            "_add_embedded_entity_plan_components",
        },
        {
            "json": json,
            "hashlib": hashlib,
            "Path": Path,
            "_copy_engine_transform_dict": lambda value: value,
            "_copy_matrix_array": lambda value: value,
            "_copy_translation_vector": lambda value: value,
            "_copy_world_position": lambda value: value,
            "_json_safe_plan_value": lambda value: value,
            "_hash_plan_entity_data": lambda _entity_data: "",
            "_mesh_cr2w_version": lambda _mesh, version: int(version),
            "_normalize_embedded_cmesh_chunk_index": lambda value: value,
            "_CACHED_ENTITY_ASSET_ITEM_KINDS": {"entity_asset"},
            "_CACHED_FULL_ITEM_KINDS": {"entity_asset", "mesh"},
            "_CACHED_FULL_PARENT_ITEM_KINDS": {"entity", "entity_empty", "group"},
            "_CACHED_FULL_MESH_ITEM_KINDS": {"mesh"},
            "_CACHED_REDCLOTH_ITEM_KINDS": set(),
            "_CACHED_SECTOR_INSTANCER_KINDS": set(),
            "_embedded_entity_data_validation_error": lambda _data, _overrides=None, deep=True: "",
            "_entity_instance_override_shape_error": lambda _overrides: "",
            "_drawable_flags_visible_from_value": lambda _value, default=True: default,
        },
    )


def _load_materializable_helper(preflight):
    namespace = exec_functions(
        IMPORTER_PATH,
        {"entity_import_plan_can_materialize"},
        {
            "_cached_plan_filter_for_position": lambda position, radius: bool(position and radius),
            "cached_plan_filter_items_for_import_options": (
                lambda items, options, context=None: [] if options.get("disable_all") else list(items)
            ),
            "_maybe_group_cached_items_into_sector_instancers": lambda items, _options: items,
            "_ensure_cached_sector_group_hierarchy": lambda items: items,
            "_filter_cached_plan_items_by_proximity": (
                lambda items, active, _stats: [item for item in items if not active or item.get("near", True)]
            ),
            "_entity_import_plan_preflight": preflight,
            "_CACHED_FULL_ITEM_KINDS": {"mesh", "entity_asset"},
            "_CACHED_FULL_PARENT_ITEM_KINDS": {"entity", "entity_empty", "group"},
        },
    )
    return namespace["entity_import_plan_can_materialize"]


class EntityPlanCoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helpers = _load_plan_helpers()

    def test_item_ids_remain_unique_after_removal_and_round_trip(self):
        new_plan = self.helpers["_new_level_import_plan"]
        add_item = self.helpers["_add_level_import_plan_item"]
        remove_item = self.helpers["_remove_level_import_plan_item"]
        plan_hash = self.helpers["entity_import_plan_hash"]

        plan = new_plan()
        first = add_item(plan, "entity", "one")
        second = add_item(plan, "entity", "two")
        third = add_item(plan, "mesh", "three", parent_id=second, repo_path="a.w2mesh")
        remove_item(plan, second)
        fourth = add_item(plan, "entity_empty", "four")

        self.assertEqual((first, second, third, fourth), ("item_1", "item_2", "item_3", "item_4"))
        self.assertEqual(len({item["id"] for item in plan["items"]}), len(plan["items"]))
        restored = json.loads(json.dumps(plan))
        self.assertEqual(plan_hash(plan), plan_hash(restored))

    def test_deterministic_builds_have_stable_ids_and_hashes(self):
        new_plan = self.helpers["_new_level_import_plan"]
        add_item = self.helpers["_add_level_import_plan_item"]
        plan_hash = self.helpers["entity_import_plan_hash"]

        plans = []
        for _ in range(2):
            plan = new_plan("parsed_layer")
            parent = add_item(plan, "entity", "door", entity_class="CDoor")
            add_item(plan, "mesh", "door_mesh", parent_id=parent, repo_path="door.w2mesh")
            plans.append(plan)

        self.assertEqual(plans[0]["items"], plans[1]["items"])
        self.assertEqual(plan_hash(plans[0]), plan_hash(plans[1]))
        self.assertEqual(
            self.helpers["_compose_world_position"]((1.0, 2.0, 3.0), (10.0, 20.0, 30.0)),
            (11.0, 22.0, 33.0),
        )

    def test_preflight_flags_incomplete_assets_but_keeps_unsupported_notes_advisory(self):
        preflight = self.helpers["_entity_import_plan_preflight"]
        plan = {
            "mode": "cached_layer",
            "unsupported": ["CUnknownVisualComponent"],
            "items": [
                {
                    "id": "item_1",
                    "kind": "entity_asset",
                    "parent_id": "",
                    "source_path": "entity.w2ent",
                }
            ],
        }

        fatal, item_errors = preflight(plan)
        self.assertEqual(fatal, [])
        self.assertTrue(
            any("requires embedded entity_data" in error for error in item_errors.get("item_1", []))
        )
        flattened = self.helpers["_entity_import_plan_preflight_errors"](plan)
        self.assertNotIn("CUnknownVisualComponent", flattened)
        self.assertIn("item_1", self.helpers["_entity_plan_skip_ids"](plan["items"], item_errors))

    def test_preflight_rejects_lazy_dlc_appearance_before_materialization(self):
        preflight = self.helpers["_entity_import_plan_preflight_errors"]
        plan = {
            "mode": "cached_layer",
            "items": [{
                "id": "item_1",
                "kind": "entity_asset",
                "parent_id": "",
                "repo_path": "entity.w2ent",
                "entity_data": {
                    "name": "entity",
                    "appearances": [{
                        "name": "dlc",
                        "_dlc_mounter_lazy": True,
                        "includedTemplates": [],
                    }],
                },
            }],
        }

        errors = preflight(plan)

        self.assertTrue(any("incomplete embedded appearance templates" in error for error in errors))

    def test_preflight_rejects_broken_entity_overlay_links(self):
        preflight = self.helpers["_entity_import_plan_preflight_errors"]

        def make_plan(items):
            return {"mode": "cached_layer", "items": items}

        def overlay_item(**extra):
            return {
                "id": "overlay",
                "kind": "entity_asset",
                "parent_id": "",
                "entity_data": {"name": "overlay"},
                "entity_asset_overlay": True,
                "shared_armature_item_id": "template",
                **extra,
            }

        template_item = {
            "id": "template",
            "kind": "entity_asset",
            "parent_id": "",
            "entity_data": {"name": "template"},
        }

        errors = preflight(make_plan([overlay_item(), dict(template_item)]))
        self.assertTrue(any("must follow" in error for error in errors))
        errors = preflight(make_plan([
            overlay_item(shared_armature_item_id="missing"),
            dict(template_item),
        ]))
        self.assertTrue(any("invalid shared entity asset" in error for error in errors))
        errors = preflight(make_plan([{
            "id": "different_entity",
            "kind": "entity",
            "parent_id": "",
        }, dict(template_item), overlay_item(parent_id="different_entity")]))
        self.assertTrue(any("across entity parents" in error for error in errors))

    def test_valid_cached_plan_remains_materializable_when_current_view_is_empty(self):
        can_materialize = _load_materializable_helper(
            self.helpers["_entity_import_plan_preflight"]
        )
        far_plan = {
            "mode": "cached_layer",
            "items": [{
                "id": "item_1",
                "kind": "mesh",
                "parent_id": "",
                "repo_path": "far.w2mesh",
                "near": False,
            }],
        }

        self.assertTrue(
            can_materialize(far_plan, camera_position=(0.0, 0.0, 0.0), radius=10.0)
        )
        self.assertTrue(can_materialize(far_plan, import_kwargs={"disable_all": True}))
        self.assertTrue(can_materialize({**far_plan, "items": []}))

    def test_invalid_far_cached_item_is_skipped_not_fatal(self):
        can_materialize = _load_materializable_helper(
            self.helpers["_entity_import_plan_preflight"]
        )
        invalid_plan = {
            "mode": "cached_layer",
            "items": [{
                "id": "item_1",
                "kind": "unknown_component",
                "parent_id": "",
                "near": False,
            }],
        }

        self.assertTrue(
            can_materialize(invalid_plan, camera_position=(0.0, 0.0, 0.0), radius=10.0)
        )
        fatal, item_errors = self.helpers["_entity_import_plan_preflight"](invalid_plan)
        self.assertEqual(fatal, [])
        self.assertIn(
            "item_1",
            self.helpers["_entity_plan_skip_ids"](invalid_plan["items"], item_errors),
        )

    def test_entity_item_rollback_removes_only_new_objects(self):
        old_object = SimpleNamespace(name="old")
        new_objects = [SimpleNamespace(name="new_a"), SimpleNamespace(name="new_b")]

        class ObjectStore:
            def __init__(self):
                self.removed = []

            def remove(self, obj, do_unlink=False):
                self.removed.append((obj, do_unlink))

        store = ObjectStore()
        namespace = exec_functions(
            IMPORTER_PATH,
            {"_rollback_blender_objects_created_after"},
            {
                "_snapshot_blender_objects": lambda: {
                    "old": old_object,
                    "new_a": new_objects[0],
                    "new_b": new_objects[1],
                },
                "bpy": SimpleNamespace(data=SimpleNamespace(objects=store)),
                "log": SimpleNamespace(warning=lambda *_args, **_kwargs: None),
            },
        )

        removed = namespace["_rollback_blender_objects_created_after"]({"old": old_object})

        self.assertEqual(removed, 2)
        self.assertEqual([obj for obj, _unlink in store.removed], list(reversed(new_objects)))
        self.assertTrue(all(unlink for _obj, unlink in store.removed))

    def test_entity_asset_clone_key_shares_only_identical_placements(self):
        clone_key = self.helpers["_entity_asset_clone_key"]
        base = {
            "id": "item_1",
            "kind": "entity_asset",
            "entity_data_hash": "abc123",
            "selected_appearance": "default",
        }

        self.assertTrue(clone_key(base))
        self.assertEqual(clone_key(base), clone_key({**base, "id": "item_2"}))
        self.assertNotEqual(clone_key(base), clone_key({**base, "entity_data_hash": "other"}))
        self.assertNotEqual(clone_key(base), clone_key({**base, "selected_appearance": "alt"}))
        self.assertNotEqual(
            clone_key(base),
            clone_key({**base, "instance_overrides": {"BufferV2": [{"component_name": "a", "variables": []}]}}),
        )
        self.assertEqual(clone_key({**base, "entity_data_hash": ""}), "")
        self.assertEqual(clone_key({**base, "entity_asset_overlay": True, "shared_armature_item_id": "x"}), "")
        self.assertEqual(clone_key({**base, "shared_armature_item_id": "x"}), "")

    def test_embedded_destruction_components_become_regular_plan_items(self):
        plan = self.helpers["_new_level_import_plan"]("direct")
        parent_id = self.helpers["_add_level_import_plan_item"](
            plan,
            "entity",
            "fence",
        )

        added = self.helpers["_add_embedded_entity_plan_components"](
            plan,
            [{
                "kind": "component_mesh",
                "name": "fence destruction",
                "repo_path": r"environment\fence.reddest",
                "component_type": "CDestructionComponent",
                "component_name": "destruction",
            }, {
                "kind": "cloth",
                "name": "fence apex",
                "repo_path": r"environment\fence.redapex",
                "component_type": "CDestructionSystemComponent",
            }],
            parent_id=parent_id,
        )

        self.assertEqual(added, 2)
        self.assertEqual(
            [item["kind"] for item in plan["items"]],
            ["entity", "component_mesh", "cloth"],
        )
        self.assertTrue(all(item["parent_id"] == parent_id for item in plan["items"][1:]))


if __name__ == "__main__":
    unittest.main()
