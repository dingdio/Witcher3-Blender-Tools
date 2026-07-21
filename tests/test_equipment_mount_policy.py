import ast
import unittest
from pathlib import Path
from types import SimpleNamespace


TARGET = Path(__file__).resolve().parents[1] / "witcher3_tools" / "ui" / "ui_equipment.py"


def _load_mount_policy():
    tree = ast.parse(TARGET.read_text(encoding="utf-8"), filename=str(TARGET))
    wanted = {
        "_get_slot_requested_mount_mode",
        "_can_load_slot_for_mount_mode",
        "_get_loaded_slot_mount_strategy",
        "_get_slot_hold_toggle_state",
        "_infer_equipment_mount_strategy",
        "_is_bound_item_hidden",
        "_is_guid_hidden",
        "_objects_in_current_view_layer",
        "_should_bind_root_chunks_to_entity",
        "_resolve_visual_policy_from_slot_names",
        "hide_objects_by_guid",
    }
    nodes = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]

    def resolve_target(name, _armature, valid_targets):
        name = str(name or "").strip()
        return {"name": name, "is_valid": bool(name and name in valid_targets)}

    namespace = {
        "bpy": SimpleNamespace(context=SimpleNamespace(view_layer=None)),
        "find_objects_by_guid": lambda _guid, _property: [],
        "import_entity": SimpleNamespace(
            _is_redcloth_collision_helper=lambda obj: bool(obj.get("witcher_apx_collision_proxy", False))
        ),
        "_resolve_equipment_mount_target": resolve_target,
        "_item_entity_is_visual": lambda _entity, attachment_profile=None: (
            getattr(attachment_profile, "kind", "") != "inventory_wrapper"
        ),
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(TARGET), "exec"), namespace)
    return namespace


POLICY = _load_mount_policy()


class EquipmentMountPolicyTests(unittest.TestCase):
    def test_visibility_skips_objects_outside_the_active_view_layer(self):
        class ImportedObject(dict):
            def __init__(self, name):
                super().__init__()
                self.name = name
                self.hidden = False
                self.hide_calls = []

            def hide_set(self, hidden):
                self.hidden = bool(hidden)
                self.hide_calls.append(self.hidden)

            def hide_get(self):
                return self.hidden

        inside = ImportedObject("inside")
        outside = ImportedObject("outside")
        collision = ImportedObject("collision")
        collision["witcher_apx_collision_proxy"] = True
        POLICY["bpy"].context.view_layer = SimpleNamespace(objects={"inside", "collision"})
        POLICY["find_objects_by_guid"] = lambda _guid, _property: [inside, outside, collision]

        self.assertEqual(POLICY["hide_objects_by_guid"]("guid", "prop"), 1)
        self.assertEqual(inside.hide_calls, [True])
        self.assertEqual(outside.hide_calls, [])
        self.assertEqual(collision.hide_calls, [])
        self.assertTrue(POLICY["_is_guid_hidden"]("guid", "prop"))
        POLICY["_iter_bound_item_objects"] = lambda _guid, _name: iter([inside, outside, collision])
        self.assertTrue(POLICY["_is_bound_item_hidden"]("guid", "bound"))

    def test_owner_skinned_item_cannot_be_moved_to_hold_in_place(self):
        class ImportedObject(dict):
            type = "MESH"

        imported = ImportedObject(witcher_mount_strategy="owner_graph_bound")
        POLICY["find_objects_by_guid"] = lambda _guid, _property: [imported]
        slot = SimpleNamespace(is_loaded=True, is_in_hold_slot=False, equip_guid="guid")
        slot_policy = {"hold_valid": True, "policy": "hold_only_on_rig"}

        self.assertEqual(
            POLICY["_get_slot_hold_toggle_state"](slot, slot_policy),
            (False, "", "ARMATURE_DATA"),
        )
        slot.is_in_hold_slot = True
        self.assertEqual(
            POLICY["_get_slot_hold_toggle_state"](slot, slot_policy),
            (True, "Mount", "FILE_3D"),
        )

        tree = ast.parse(TARGET.read_text(encoding="utf-8"), filename=str(TARGET))
        toggle = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "EQUIPMENT_OT_ToggleItem"
        )
        equip_reload = next(
            call for call in ast.walk(toggle)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "load_equipment_item"
            and any(
                keyword.arg == "mount_mode"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value == "equip"
                for keyword in call.keywords
            )
        )
        unload = next(
            call for call in ast.walk(toggle)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "unload_equipment_item"
        )
        self.assertLess(equip_reload.lineno, unload.lineno)

    def test_slotless_skinned_equipment_uses_owner_skinning(self):
        slot = SimpleNamespace(is_loaded=False, is_in_hold_slot=False)
        skinned = SimpleNamespace(kind="slot_animated", has_skinned_mesh_payload=True)
        policy = POLICY["_resolve_visual_policy_from_slot_names"](
            "",
            "head",
            None,
            {"head"},
            attachment_profile=skinned,
        )

        mount_mode = POLICY["_get_slot_requested_mount_mode"](slot, policy)
        strategy = POLICY["_infer_equipment_mount_strategy"](
            skinned,
            policy["equip_target"],
        )

        self.assertEqual(policy["policy"], "equipable_on_rig")
        self.assertEqual(mount_mode, "equip")
        self.assertTrue(POLICY["_can_load_slot_for_mount_mode"](policy, mount_mode))
        self.assertEqual(strategy, "owner_graph_bound")
        self.assertTrue(POLICY["_should_bind_root_chunks_to_entity"](skinned, strategy))

        self.assertEqual(
            POLICY["_infer_equipment_mount_strategy"](skinned, policy["hold_target"]),
            "slot_mount_animated",
        )
        equipped = POLICY["_resolve_visual_policy_from_slot_names"](
            "back",
            "hand",
            None,
            {"back", "hand"},
            attachment_profile=skinned,
        )
        self.assertEqual(
            POLICY["_infer_equipment_mount_strategy"](skinned, equipped["equip_target"]),
            "slot_mount_animated",
        )

    def test_non_skinned_hold_only_item_keeps_its_auto_hold(self):
        slot = SimpleNamespace(is_loaded=False, is_in_hold_slot=False)
        static = SimpleNamespace(kind="slot_visual", has_skinned_mesh_payload=False)
        policy = POLICY["_resolve_visual_policy_from_slot_names"](
            "",
            "hand",
            None,
            {"hand"},
            attachment_profile=static,
        )

        self.assertEqual(policy["policy"], "hold_only_on_rig")
        self.assertEqual(POLICY["_get_slot_requested_mount_mode"](slot, policy), "hold")
        self.assertEqual(
            POLICY["_infer_equipment_mount_strategy"](static, policy["hold_target"]),
            "slot_mount_static",
        )

    def test_auto_mode_is_resolved_after_the_item_profile_is_known(self):
        tree = ast.parse(TARGET.read_text(encoding="utf-8"), filename=str(TARGET))
        core = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_load_equipment_item_core"
        )
        calls = [node for node in ast.walk(core) if isinstance(node, ast.Call)]
        profile_call = next(
            call for call in calls
            if isinstance(call.func, ast.Attribute)
            and call.func.attr == "classify_equipment_attachment_profile"
        )
        auto_mode_call = next(
            call for call in calls
            if isinstance(call.func, ast.Name)
            and call.func.id == "_get_slot_requested_mount_mode"
        )

        self.assertLess(profile_call.lineno, auto_mode_call.lineno)


if __name__ == "__main__":
    unittest.main()
