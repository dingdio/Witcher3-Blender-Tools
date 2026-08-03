import copy
import json
import types
import unittest
from pathlib import Path

from _helpers import exec_functions


IMPORTER_PATH = (
    Path(__file__).resolve().parents[1]
    / "witcher3_tools"
    / "importers"
    / "import_blender_fun.py"
)


def _load_override_helpers():
    def json_safe(value):
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        if isinstance(value, bytes):
            return {"encoding": "hex", "data": value.hex()}
        if isinstance(value, dict):
            return {str(key): json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [json_safe(item) for item in value]
        return json_safe(vars(value))

    return exec_functions(
        IMPORTER_PATH,
        {
            "_instance_entry_value",
            "_normalize_entity_buffer_v2",
            "_entity_instance_overrides",
            "_entity_instance_override_shape_error",
            "_iter_entity_override_components",
            "_apply_entity_instance_overrides",
            "_merge_unique_entity_metadata",
            "_merge_entity_appearance_metadata",
            "_merge_instance_entity_metadata",
            "_entity_data_override_component_names",
            "_partition_entity_instance_overrides",
        },
        {
            "copy": copy,
            "json": json,
            "_json_safe_plan_value": json_safe,
            "read_prop_value": lambda prop, _chunks: getattr(prop, "Value", None),
        },
    )


class EntityInstanceOverrideTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helpers = _load_override_helpers()

    def test_decoded_buffer_v2_is_normalized_and_applied(self):
        prop = types.SimpleNamespace(theName="isEnabled", theType="Bool", Value=False)
        entry = types.SimpleNamespace(
            componentName=types.SimpleNamespace(value="torch"),
            variables=types.SimpleNamespace(elements=[types.SimpleNamespace(PROP=prop)]),
        )
        layer_entity = types.SimpleNamespace(BufferV1=[], BufferV2=[entry])

        overrides = self.helpers["_entity_instance_overrides"](layer_entity)
        target = {"name": "torch", "isEnabled": True}
        entity = types.SimpleNamespace(
            staticMeshes={"chunks": [target]},
            MovingPhysicalAgentComponent=None,
            appearances=[],
        )

        self.assertEqual(overrides, {"BufferV2": [{
            "component_name": "torch",
            "variables": [{"name": "isEnabled", "type": "Bool", "value": False}],
        }]})
        self.assertEqual(self.helpers["_apply_entity_instance_overrides"](entity, overrides), "")
        self.assertFalse(target["isEnabled"])

    def test_opaque_or_unmatchable_overrides_fail_explicitly(self):
        shape_error = self.helpers["_entity_instance_override_shape_error"]
        apply_overrides = self.helpers["_apply_entity_instance_overrides"]
        entity = types.SimpleNamespace(
            staticMeshes={"chunks": [{"name": "body"}]},
            MovingPhysicalAgentComponent=None,
            appearances=[],
        )

        self.assertIn("BufferV1", shape_error({"BufferV1": [{"Buffer": "opaque"}]}))
        self.assertIn(
            "not present",
            apply_overrides(entity, {"BufferV2": [{
                "component_name": "missing",
                "variables": [{"name": "isEnabled", "type": "Bool", "value": True}],
            }]}),
        )

    def test_instance_metadata_moves_to_per_instance_template_copy(self):
        merge = self.helpers["_merge_instance_entity_metadata"]
        template = {
            "name": "template",
            "staticMeshes": {"chunks": [{"name": "body"}]},
            "slots": [{"name": "hand", "componentName": "body", "boneName": "r_hand"}],
            "coloringEntries": [],
            "CAnimAnimsetsParam": [{"path": "base.w2anims"}],
            "appearances": [{"name": "default", "includedTemplates": [{"name": "base"}]}],
        }
        instance = {
            "name": "instance",
            "staticMeshes": {"chunks": [{"name": "torch"}]},
            "slots": [{"name": "flame", "componentName": "torch", "boneName": ""}],
            "coloringEntries": [{"appearance": "default", "componentName": "torch"}],
            "CAnimAnimsetsParam": [{"path": "placed.w2anims"}],
            "appearances": [{"name": "default", "includedTemplates": [{"name": "placed"}]}],
        }

        merged_template, remaining_instance = merge(template, instance)

        self.assertEqual(len(merged_template["coloringEntries"]), 1)
        self.assertEqual(len(merged_template["CAnimAnimsetsParam"]), 2)
        self.assertEqual(
            [entry["name"] for entry in merged_template["appearances"][0]["includedTemplates"]],
            ["base", "placed"],
        )
        self.assertEqual([slot["name"] for slot in merged_template["slots"]], ["hand", "flame"])
        self.assertEqual([slot["name"] for slot in remaining_instance["slots"]], ["flame"])
        self.assertEqual(len(remaining_instance["coloringEntries"]), 1)
        self.assertEqual(remaining_instance["appearances"], [])
        self.assertEqual(remaining_instance["staticMeshes"]["chunks"][0]["name"], "torch")
        self.assertEqual(template["slots"][0]["name"], "hand")

        _merged, metadata_only = merge(
            template,
            {"staticMeshes": {"chunks": []}, "slots": instance["slots"]},
        )
        self.assertEqual(metadata_only["slots"], [])

    def test_buffer_v2_routes_to_the_graph_that_owns_each_component(self):
        partition = self.helpers["_partition_entity_instance_overrides"]
        template = {"staticMeshes": {"chunks": [{"name": "body"}]}}
        instance = {"staticMeshes": {"chunks": [{"name": "torch"}]}}
        entry = lambda name: {
            "component_name": name,
            "variables": [{"name": "visible", "type": "Bool", "value": False}],
        }

        partitions, errors = partition(
            {"BufferV2": [entry("body"), entry("torch")]},
            template,
            instance,
        )

        self.assertEqual(errors, [])
        self.assertEqual(partitions[0]["BufferV2"][0]["component_name"], "body")
        self.assertEqual(partitions[1]["BufferV2"][0]["component_name"], "torch")

        _partitions, missing_errors = partition(
            {"BufferV2": [entry("missing")]}, template, instance
        )
        self.assertIn("absent", missing_errors[0])
        _partitions, ambiguous_errors = partition(
            {"BufferV2": [entry("body")]}, template, template
        )
        self.assertIn("ambiguous", ambiguous_errors[0])


if __name__ == "__main__":
    unittest.main()
