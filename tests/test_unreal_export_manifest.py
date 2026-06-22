import json
import sys
import tempfile
import types
import unittest
from unittest import mock
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

if "witcher3_tools" not in sys.modules:
    _pkg = types.ModuleType("witcher3_tools")
    _pkg.__path__ = [str(REPO_ROOT / "witcher3_tools")]
    _pkg.__package__ = "witcher3_tools"
    sys.modules["witcher3_tools"] = _pkg

from witcher3_tools.unreal_export import bundle, gather, speedtree_bundle, texture_export, unreal_project
from witcher3_tools.unreal_export.manifest import (
    SCHEMA,
    build_manifest,
    convert_witcher_param,
    depot_asset_rel,
    texture_compression_for_param,
    texture_srgb_for_param,
)
from witcher3_tools.unreal_export.material_chain import ChainBuilder
from witcher3_tools.unreal_export.plugin_install import (
    PLUGIN_DESCRIPTOR,
    PLUGIN_NAME,
    default_plugin_source,
    install_or_update_plugin,
    plugin_target_dir,
)
from witcher3_tools.unreal_export.socket_client import encode_message, import_bundle_request, send_import_request

for _name in [n for n in list(sys.modules) if n == "witcher3_tools" or n.startswith("witcher3_tools.")]:
    sys.modules.pop(_name, None)


def _stub_register_texture(raw_value, param_name):
    return {"depot": depot_asset_rel(raw_value)}


class TestFbxExportOptions(unittest.TestCase):
    def test_export_fbx_emits_unreal_friendly_skeleton_and_smoothing_options(self):
        captured = {}

        class FakeMesh:
            type = "MESH"
            name = "t_01_mg__body_hires"

            def select_set(self, value):
                self.selected = value

        def fake_fbx_export(**kwargs):
            captured.update(kwargs)

        previous_bpy = sys.modules.get("bpy")
        fake_bpy = types.SimpleNamespace(
            ops=types.SimpleNamespace(
                object=types.SimpleNamespace(
                    mode_set=lambda **kwargs: None,
                    select_all=lambda **kwargs: None,
                ),
                export_scene=types.SimpleNamespace(fbx=fake_fbx_export),
            ),
            data=types.SimpleNamespace(objects={}),
        )
        sys.modules["bpy"] = fake_bpy
        try:
            mesh = FakeMesh()
            context = types.SimpleNamespace(
                selected_objects=[],
                object=None,
                view_layer=types.SimpleNamespace(objects=types.SimpleNamespace(active=None)),
            )

            bundle.export_fbx(context, [mesh], r"F:\tmp\t_01_mg__body_hires.fbx")
        finally:
            if previous_bpy is None:
                sys.modules.pop("bpy", None)
            else:
                sys.modules["bpy"] = previous_bpy

        self.assertEqual(captured["mesh_smooth_type"], "FACE")
        self.assertFalse(captured["use_armature_deform_only"])
        self.assertFalse(captured["add_leaf_bones"])
        self.assertEqual(captured["armature_nodetype"], "NULL")
        self.assertFalse(captured["bake_anim"])
        self.assertEqual(captured["axis_forward"], "-Z")
        self.assertEqual(captured["axis_up"], "Y")
        # The m->cm factor rides the Armature root null; the UE plugin
        # compensates on animation imports with ImportUniformScale=100.
        self.assertEqual(captured["apply_scale_options"], "FBX_SCALE_NONE")
        self.assertFalse(captured["bake_space_transform"])

    def test_export_fbx_can_emit_animation_only_fbx_options(self):
        captured = {}

        class FakeArmature:
            type = "ARMATURE"
            name = "geralt_rig"

            def select_set(self, value):
                self.selected = value

        def fake_fbx_export(**kwargs):
            captured.update(kwargs)

        previous_bpy = sys.modules.get("bpy")
        fake_bpy = types.SimpleNamespace(
            ops=types.SimpleNamespace(
                object=types.SimpleNamespace(
                    mode_set=lambda **kwargs: None,
                    select_all=lambda **kwargs: None,
                ),
                export_scene=types.SimpleNamespace(fbx=fake_fbx_export),
            ),
            data=types.SimpleNamespace(objects={}),
        )
        sys.modules["bpy"] = fake_bpy
        try:
            armature = FakeArmature()
            context = types.SimpleNamespace(
                selected_objects=[],
                object=None,
                view_layer=types.SimpleNamespace(objects=types.SimpleNamespace(active=None)),
            )

            bundle.export_fbx(
                context,
                [armature],
                r"F:\tmp\attack_fast_l_01.fbx",
                object_types={"ARMATURE"},
                bake_anim=True,
            )
        finally:
            if previous_bpy is None:
                sys.modules.pop("bpy", None)
            else:
                sys.modules["bpy"] = previous_bpy

        self.assertEqual(captured["object_types"], {"ARMATURE"})
        self.assertTrue(captured["bake_anim"])
        self.assertFalse(captured["bake_anim_use_all_actions"])
        self.assertFalse(captured["bake_anim_use_nla_strips"])
        self.assertEqual(captured["bake_anim_simplify_factor"], 0.0)
        self.assertEqual(captured["armature_nodetype"], "NULL")
        self.assertEqual(captured["axis_forward"], "-Z")
        self.assertEqual(captured["axis_up"], "Y")
        self.assertEqual(captured["apply_scale_options"], "FBX_SCALE_NONE")

    def test_export_fbx_temporarily_uses_unreal_armature_wrapper_name(self):
        captured = {}

        class FakeObject:
            def __init__(self, name, obj_type):
                self.name = name
                self.type = obj_type
                self.selected = False

            def select_set(self, value):
                self.selected = value

        armature = FakeObject("geralt_player:CMovingPhysicalAgentComponent2_ARM", "ARMATURE")
        existing_armature_name = FakeObject("Armature", "EMPTY")

        def fake_fbx_export(**kwargs):
            captured["export_armature_name"] = armature.name
            captured["collision_name"] = existing_armature_name.name
            captured.update(kwargs)

        previous_bpy = sys.modules.get("bpy")
        fake_bpy = types.SimpleNamespace(
            ops=types.SimpleNamespace(
                object=types.SimpleNamespace(
                    mode_set=lambda **kwargs: None,
                    select_all=lambda **kwargs: None,
                ),
                export_scene=types.SimpleNamespace(fbx=fake_fbx_export),
            ),
            data=types.SimpleNamespace(objects=[existing_armature_name, armature]),
        )
        sys.modules["bpy"] = fake_bpy
        try:
            context = types.SimpleNamespace(
                selected_objects=[],
                object=None,
                view_layer=types.SimpleNamespace(objects=types.SimpleNamespace(active=None)),
            )

            bundle.export_fbx(
                context,
                [armature],
                r"F:\tmp\attack_fast_l_01.fbx",
                object_types={"ARMATURE"},
                bake_anim=True,
            )
        finally:
            if previous_bpy is None:
                sys.modules.pop("bpy", None)
            else:
                sys.modules["bpy"] = previous_bpy

        self.assertEqual(captured["export_armature_name"], "Armature")
        self.assertNotEqual(captured["collision_name"], "Armature")
        self.assertEqual(armature.name, "geralt_player:CMovingPhysicalAgentComponent2_ARM")
        self.assertEqual(existing_armature_name.name, "Armature")

    def test_buffer_skeleton_pose_uses_same_y_mirror_as_buffer_import(self):
        class FakeBone:
            def __init__(self, name, matrix_local, parent=None):
                self.name = name
                self.matrix_local = matrix_local
                self.parent = parent

        class FakeVector:
            def __init__(self, x, y, z):
                self.x = float(x)
                self.y = float(y)
                self.z = float(z)

            def __imul__(self, scalar):
                self.x *= scalar
                self.y *= scalar
                self.z *= scalar
                return self

        class FakeQuat:
            x = 0.0
            y = 0.0
            z = 0.0
            w = 1.0

        class FakeMatrix:
            def __init__(self, rows):
                self.rows = [[float(value) for value in row] for row in rows]

            @classmethod
            def Translation(cls, vec):
                x, y, z = vec
                return cls(((1, 0, 0, x), (0, 1, 0, y), (0, 0, 1, z), (0, 0, 0, 1)))

            @classmethod
            def Diagonal(cls, values):
                vals = list(values)
                return cls(tuple(tuple(vals[row] if row == col else 0.0 for col in range(4)) for row in range(4)))

            def inverted(self):
                # Enough for this test's rigid transforms.
                r = [[self.rows[row][col] for col in range(3)] for row in range(3)]
                t = [self.rows[row][3] for row in range(3)]
                rt = [[r[col][row] for col in range(3)] for row in range(3)]
                inv_t = [-sum(rt[row][col] * t[col] for col in range(3)) for row in range(3)]
                return FakeMatrix((
                    (rt[0][0], rt[0][1], rt[0][2], inv_t[0]),
                    (rt[1][0], rt[1][1], rt[1][2], inv_t[1]),
                    (rt[2][0], rt[2][1], rt[2][2], inv_t[2]),
                    (0, 0, 0, 1),
                ))

            def __matmul__(self, other):
                return FakeMatrix(
                    tuple(
                        tuple(sum(self.rows[row][k] * other.rows[k][col] for k in range(4)) for col in range(4))
                        for row in range(4)
                    )
                )

            def decompose(self):
                return (
                    FakeVector(self.rows[0][3], self.rows[1][3], self.rows[2][3]),
                    FakeQuat(),
                    FakeVector(1.0, 1.0, 1.0),
                )

        previous_mathutils = sys.modules.get("mathutils")
        sys.modules["mathutils"] = types.SimpleNamespace(Matrix=FakeMatrix)
        try:
            root = FakeBone("Root", FakeMatrix.Translation((0.0, 0.0, 0.0)))
            child = FakeBone("child", FakeMatrix.Translation((0.0, -1.0, 0.0)), root)
            armature = types.SimpleNamespace(data=types.SimpleNamespace(bones=[root, child]))

            skeleton = gather.extract_armature_skeleton(armature)
        finally:
            if previous_mathutils is None:
                sys.modules.pop("mathutils", None)
            else:
                sys.modules["mathutils"] = previous_mathutils

        # Match the .w3buf Blender/RED -> Unreal basis conversion.
        pose = skeleton["poses"][1]
        self.assertAlmostEqual(pose[0], 0.0)
        self.assertAlmostEqual(pose[1], 1.0)
        self.assertAlmostEqual(pose[2], 0.0)
        self.assertEqual(skeleton["poses"][0][7:10], [100.0, 100.0, 100.0])


class TestAnimationAssetPaths(unittest.TestCase):
    def test_animation_asset_path_mirrors_w2anims_folder(self):
        used = set()
        self.assertEqual(
            bundle._unique_animation_asset_rel(
                r"animations\man\combat\man_geralt_sword.w2anims",
                "attack fast l 01",
                "geralt",
                used,
            ),
            "animations/man/combat/man_geralt_sword/attack_fast_l_01",
        )

    def test_animation_asset_path_falls_back_to_custom_folder(self):
        used = set()
        self.assertEqual(
            bundle._unique_animation_asset_rel("", "attack fast l 01", "geralt", used),
            "custom/geralt/animations/attack_fast_l_01",
        )

    def test_w2anims_json_source_maps_to_w2anims_folder(self):
        self.assertEqual(
            bundle._normalize_animset_depot_path(r"animations\man\combat\man_geralt_sword.w2anims.json"),
            r"animations\man\combat\man_geralt_sword.w2anims",
        )


class TestExportActionCollection(unittest.TestCase):
    def _context_without_export_set(self):
        return types.SimpleNamespace(scene=types.SimpleNamespace(witcher_anim_export_set=[]))

    def test_falls_back_to_action_currently_applied_to_armature(self):
        action = types.SimpleNamespace(name="nekker_idle")
        armature = types.SimpleNamespace(
            animation_data=types.SimpleNamespace(action=action)
        )
        warnings = []

        actions = bundle._collect_export_actions(
            self._context_without_export_set(), armature, warnings
        )

        self.assertEqual(actions, [action])
        self.assertEqual(warnings, [])

    def test_no_export_set_and_no_applied_action_exports_nothing(self):
        armature = types.SimpleNamespace(animation_data=None)
        warnings = []

        actions = bundle._collect_export_actions(
            self._context_without_export_set(), armature, warnings
        )

        self.assertEqual(actions, [])
        self.assertEqual(warnings, [])

    def test_falls_back_to_nla_strip_under_playhead(self):
        # Clips loaded from the addon's anim list play from the 'anim_import'
        # NLA track with animation_data.action left unset.
        action = types.SimpleNamespace(name="c_idle.002")
        strip = types.SimpleNamespace(action=action, mute=False, frame_start=0.0, frame_end=75.0)
        track = types.SimpleNamespace(name="anim_import", mute=False, is_solo=False, strips=[strip])
        armature = types.SimpleNamespace(
            animation_data=types.SimpleNamespace(action=None, nla_tracks=[track])
        )
        context = types.SimpleNamespace(
            scene=types.SimpleNamespace(witcher_anim_export_set=[], frame_current=10)
        )

        actions = bundle._collect_export_actions(context, armature, [])

        self.assertEqual(actions, [action])

    def test_nla_fallback_skips_muted_tracks_and_strips(self):
        muted_action = types.SimpleNamespace(name="muted_clip")
        playing_action = types.SimpleNamespace(name="playing_clip")
        muted_track = types.SimpleNamespace(
            name="anim_import", mute=True, is_solo=False,
            strips=[types.SimpleNamespace(action=muted_action, mute=False, frame_start=0.0, frame_end=50.0)],
        )
        live_track = types.SimpleNamespace(
            name="other", mute=False, is_solo=False,
            strips=[types.SimpleNamespace(action=playing_action, mute=False, frame_start=0.0, frame_end=50.0)],
        )
        armature = types.SimpleNamespace(
            animation_data=types.SimpleNamespace(action=None, nla_tracks=[muted_track, live_track])
        )

        self.assertEqual(bundle._current_armature_action(armature, 10), playing_action)

    def test_export_set_entries_win_over_applied_action(self):
        set_action = types.SimpleNamespace(name="attack_fast_l_01")
        applied_action = types.SimpleNamespace(name="nekker_idle")
        entry = types.SimpleNamespace(enabled=True, action_name="attack_fast_l_01")
        context = types.SimpleNamespace(
            scene=types.SimpleNamespace(witcher_anim_export_set=[entry])
        )
        armature = types.SimpleNamespace(
            animation_data=types.SimpleNamespace(action=applied_action)
        )

        previous_bpy = sys.modules.get("bpy")
        sys.modules["bpy"] = types.SimpleNamespace(
            data=types.SimpleNamespace(actions={"attack_fast_l_01": set_action})
        )
        try:
            warnings = []
            actions = bundle._collect_export_actions(context, armature, warnings)
        finally:
            if previous_bpy is None:
                sys.modules.pop("bpy", None)
            else:
                sys.modules["bpy"] = previous_bpy

        self.assertEqual(actions, [set_action])
        self.assertEqual(warnings, [])


class TestExportObjectCollection(unittest.TestCase):
    class FakeObject:
        def __init__(self, name, obj_type, children=None, hidden=False, visible=True, depot="", source_game=""):
            self.name = name
            self.name_full = name
            self.type = obj_type
            self.children = list(children or [])
            self.children_recursive = list(self.children)
            self.hide_viewport = hidden
            self.hide_render = hidden
            self._hidden = hidden
            self._visible = visible
            self.modifiers = []
            self.parent = None
            self.witcherui_MeshSettings = types.SimpleNamespace(item_repo_path=depot)
            self.selected = False
            self._props = {}
            if source_game:
                self._props["witcher_source_game"] = source_game
            for child in self.children:
                child.parent = self

        def get(self, key, default=None):
            return self._props.get(key, default)

        def select_set(self, value):
            self.selected = bool(value)

        def hide_get(self):
            return self._hidden

        def visible_get(self):
            return self._visible

    def test_selected_armature_exports_visible_character_template_slots(self):
        neck_transition = self.FakeObject(
            "h_wa__neck_transition",
            "MESH",
            depot=r"characters\models\common\woman_average\body\model\h_wa__neck_transition.w2mesh",
        )
        body = self.FakeObject(
            "t_01_wa__body",
            "MESH",
            depot=r"dlc\ep1\data\characters\models\secondary_npc\shani\model\t_01_wa__body.w2mesh",
        )
        dress = self.FakeObject(
            "shani_dress",
            "MESH",
            depot=r"dlc\ep1\data\characters\models\secondary_npc\shani\model\shani_dress.w2mesh",
        )
        old_body = self.FakeObject(
            "h_wa__old_body",
            "MESH",
            hidden=True,
            depot=r"characters\models\common\woman_average\body\model\h_wa__old_body.w2mesh",
        )
        unloaded_body = self.FakeObject(
            "h_wa__unloaded_body",
            "MESH",
            depot=r"characters\models\common\woman_average\body\model\h_wa__unloaded_body.w2mesh",
        )
        collision_proxy = self.FakeObject("b_01_wa__shani_px:Collision Proxy", "MESH")

        rig_settings = types.SimpleNamespace(
            template_slots=[
                types.SimpleNamespace(is_loaded=True, template_guid="template-visible"),
                types.SimpleNamespace(is_loaded=True, template_guid="template-hidden"),
                types.SimpleNamespace(is_loaded=False, template_guid="template-unloaded"),
            ],
            equipment_slots=[
                types.SimpleNamespace(is_loaded=True, equip_guid="equip-visible"),
            ],
        )
        armature = self.FakeObject(
            "shani_CMovingPhysicalAgentComponent_ARM",
            "ARMATURE",
            children=[neck_transition, collision_proxy],
        )
        armature.data = types.SimpleNamespace(witcherui_RigSettings=rig_settings)

        guid_map = {
            ("template-visible", "witcher_template_guid"): [body, collision_proxy],
            ("template-hidden", "witcher_template_guid"): [old_body],
            ("template-unloaded", "witcher_template_guid"): [unloaded_body],
            ("equip-visible", "witcher_equip_guid"): [dress],
        }
        fake_equipment = types.ModuleType("witcher3_tools.ui.ui_equipment")
        fake_equipment.find_objects_by_guid = (
            lambda guid, prop_name="witcher_equip_guid": guid_map.get((guid, prop_name), [])
        )

        previous_modules = {
            name: sys.modules.get(name)
            for name in (
                "witcher3_tools",
                "witcher3_tools.ui",
                "witcher3_tools.ui.ui_equipment",
            )
        }
        fake_pkg = types.ModuleType("witcher3_tools")
        fake_pkg.__path__ = [str(REPO_ROOT / "witcher3_tools")]
        fake_ui = types.ModuleType("witcher3_tools.ui")
        fake_ui.__path__ = []
        sys.modules["witcher3_tools"] = fake_pkg
        sys.modules["witcher3_tools.ui"] = fake_ui
        sys.modules["witcher3_tools.ui.ui_equipment"] = fake_equipment
        try:
            objects = bundle.collect_export_objects([armature])
        finally:
            for name, previous in previous_modules.items():
                if previous is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = previous

        names = [obj.name for obj in objects]
        self.assertIn("h_wa__neck_transition", names)
        self.assertIn("t_01_wa__body", names)
        self.assertIn("shani_dress", names)
        self.assertNotIn("h_wa__old_body", names)
        self.assertNotIn("h_wa__unloaded_body", names)
        self.assertNotIn("b_01_wa__shani_px:Collision Proxy", names)

    def test_character_export_skips_px_proxy_meshes(self):
        body = self.FakeObject(
            "body_01_wa__ciri",
            "MESH",
            depot=r"characters\models\main_npc\ciri\model\body_01_wa__ciri.w2mesh",
        )
        redcloth_proxy = self.FakeObject("arm_01_wa__ciri_px:sphere_r_bicep_1", "MESH")
        rig_settings = types.SimpleNamespace(
            template_slots=[types.SimpleNamespace(is_loaded=True, template_guid="template-visible")],
            equipment_slots=[],
        )
        armature = self.FakeObject("ciri_player_ARM", "ARMATURE")
        armature.data = types.SimpleNamespace(witcherui_RigSettings=rig_settings)

        fake_equipment = types.ModuleType("witcher3_tools.ui.ui_equipment")
        fake_equipment.find_objects_by_guid = (
            lambda guid, prop_name="witcher_equip_guid": [body, redcloth_proxy]
        )

        previous_modules = {
            name: sys.modules.get(name)
            for name in (
                "witcher3_tools",
                "witcher3_tools.ui",
                "witcher3_tools.ui.ui_equipment",
            )
        }
        fake_pkg = types.ModuleType("witcher3_tools")
        fake_pkg.__path__ = [str(REPO_ROOT / "witcher3_tools")]
        fake_ui = types.ModuleType("witcher3_tools.ui")
        fake_ui.__path__ = []
        sys.modules["witcher3_tools"] = fake_pkg
        sys.modules["witcher3_tools.ui"] = fake_ui
        sys.modules["witcher3_tools.ui.ui_equipment"] = fake_equipment
        try:
            objects = bundle.collect_export_objects([armature, redcloth_proxy])
        finally:
            for name, previous in previous_modules.items():
                if previous is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = previous

        names = [obj.name for obj in objects]
        self.assertIn("body_01_wa__ciri", names)
        self.assertNotIn("arm_01_wa__ciri_px:sphere_r_bicep_1", names)

    def test_unreal_export_preloads_current_appearance_when_slots_are_empty(self):
        item = types.SimpleNamespace(name="shani")
        rig_settings = types.SimpleNamespace(
            app_list=[item],
            app_list_index=0,
            template_slots=[],
            equipment_slots=[],
        )
        armature = self.FakeObject("shani_CMovingPhysicalAgentComponent_ARM", "ARMATURE")
        armature.data = types.SimpleNamespace(witcherui_RigSettings=rig_settings)
        old_active = self.FakeObject("old_active", "EMPTY")
        context = types.SimpleNamespace(
            selected_objects=[armature],
            object=old_active,
            view_layer=types.SimpleNamespace(objects=types.SimpleNamespace(active=old_active)),
        )

        calls = []
        fake_import_entity = types.ModuleType("witcher3_tools.importers.import_entity")
        fake_import_entity.import_from_list_item = lambda ctx, app_item: calls.append(app_item.name)

        previous_modules = {
            name: sys.modules.get(name)
            for name in (
                "witcher3_tools",
                "witcher3_tools.importers",
                "witcher3_tools.importers.import_entity",
                "witcher3_tools.ui",
                "witcher3_tools.ui.ui_equipment",
            )
        }
        fake_pkg = types.ModuleType("witcher3_tools")
        fake_pkg.__path__ = [str(REPO_ROOT / "witcher3_tools")]
        fake_importers = types.ModuleType("witcher3_tools.importers")
        fake_importers.__path__ = []
        fake_ui = types.ModuleType("witcher3_tools.ui")
        fake_ui.__path__ = []
        fake_equipment = types.ModuleType("witcher3_tools.ui.ui_equipment")
        fake_equipment.find_objects_by_guid = lambda guid, prop_name="witcher_equip_guid": []
        sys.modules["witcher3_tools"] = fake_pkg
        sys.modules["witcher3_tools.importers"] = fake_importers
        sys.modules["witcher3_tools.importers.import_entity"] = fake_import_entity
        sys.modules["witcher3_tools.ui"] = fake_ui
        sys.modules["witcher3_tools.ui.ui_equipment"] = fake_equipment
        try:
            warnings = bundle._ensure_selected_character_appearances_loaded(context, [armature])
        finally:
            for name, previous in previous_modules.items():
                if previous is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = previous

        self.assertEqual(warnings, [])
        self.assertEqual(calls, ["shani"])
        self.assertIs(context.view_layer.objects.active, old_active)

    def test_unreal_export_source_game_follows_selected_armature(self):
        rig_settings = types.SimpleNamespace(source_game="w2")
        armature = self.FakeObject("geralt_w2_ARM", "ARMATURE")
        armature.data = types.SimpleNamespace(witcherui_RigSettings=rig_settings)

        source_game, warnings = bundle._infer_export_source_game([armature], armature)

        self.assertEqual(source_game, "w2")
        self.assertEqual(warnings, [])
        self.assertEqual(bundle._resolve_content_root_setting("/Game/Witcher3", source_game), "/Game/Witcher2")
        self.assertEqual(bundle._resolve_content_root_setting("/Game/ImportedFbx", source_game), "/Game/Witcher2")
        self.assertEqual(bundle._resolve_content_root_setting("/Game/CustomRED", source_game), "/Game/CustomRED")

    def test_unreal_export_warns_on_mixed_source_games(self):
        rig_settings = types.SimpleNamespace(source_game="w2")
        armature = self.FakeObject("geralt_w2_ARM", "ARMATURE")
        armature.data = types.SimpleNamespace(witcherui_RigSettings=rig_settings)
        mesh = self.FakeObject("w3_sword", "MESH", source_game="w3")

        source_game, warnings = bundle._infer_export_source_game([armature, mesh], armature)

        self.assertEqual(source_game, "w2")
        self.assertEqual(len(warnings), 1)
        self.assertIn("Mixed W2/W3", warnings[0])


class TestExportArmatureSelection(unittest.TestCase):
    @staticmethod
    def _armature(name, bone_names, parents=None):
        parents = parents or {}
        bones_by_name = {
            bone_name: types.SimpleNamespace(name=bone_name, parent=None)
            for bone_name in bone_names
        }
        for bone_name, parent_name in parents.items():
            if bone_name in bones_by_name and parent_name in bones_by_name:
                bones_by_name[bone_name].parent = bones_by_name[parent_name]
        return types.SimpleNamespace(
            name=name,
            type="ARMATURE",
            data=types.SimpleNamespace(bones=list(bones_by_name.values())),
        )

    def test_flat_mesh_armature_is_replaced_by_entity_rig(self):
        # import_mesh builds per-mesh armatures from boneNames/boneMatrices
        # (flat, no Root); the FBX must carry the entity rig instead or UE
        # cannot merge the bone tree with the shared skeleton.
        mesh_arm = self._armature("t_01__nekker_ARM", ["k_pelvis_g", "k_torso_g"])
        main_arm = self._armature(
            "nekker_lvl1_ARM", ["Root", "Trajectory", "k_pelvis_g", "k_torso_g"]
        )
        warnings = []

        chosen = bundle._resolve_export_armature(mesh_arm, main_arm, "nekker/model/t_01__nekker", warnings)

        self.assertIs(chosen, main_arm)
        self.assertEqual(warnings, [])

    def test_mesh_armature_with_extra_bones_is_kept_with_warning(self):
        mesh_arm = self._armature("hair_ARM", ["k_pelvis_g", "hair_01"])
        main_arm = self._armature("nekker_lvl1_ARM", ["Root", "k_pelvis_g"])
        warnings = []

        chosen = bundle._resolve_export_armature(mesh_arm, main_arm, "nekker/hair", warnings)

        self.assertIs(chosen, mesh_arm)
        self.assertEqual(len(warnings), 1)
        self.assertIn("hair_01", warnings[0])

    def test_missing_bone_export_uses_attachment_parent_chain(self):
        source_arm = self._armature(
            "shani:CAnimDangleBufferComponent03_ARM",
            ["Root", "pelvis", "l_shin", "l_dyng_group_01", "l_01_01", "l_01_02"],
            {
                "pelvis": "Root",
                "l_shin": "pelvis",
                "l_dyng_group_01": "l_shin",
                "l_01_01": "l_dyng_group_01",
                "l_01_02": "l_01_01",
            },
        )

        required = bundle._required_source_bone_names(
            source_arm,
            ["l_01_02"],
            {"Root", "pelvis", "l_shin"},
        )

        self.assertEqual(required, ["l_dyng_group_01", "l_01_01", "l_01_02"])

    def test_attachment_armature_is_preferred_over_flat_mesh_armature(self):
        group_arm = self._armature("b_01_wa__shani_ARM", ["head", "l_01_02"])
        attachment_arm = self._armature(
            "shani:CAnimDangleBufferComponent03_ARM",
            ["Root", "l_dyng_group_01", "l_01_01", "l_01_02"],
            {
                "l_dyng_group_01": "Root",
                "l_01_01": "l_dyng_group_01",
                "l_01_02": "l_01_01",
            },
        )
        group_arm.parent = attachment_arm

        source = bundle._find_attachment_armature_for_missing_bones(group_arm, ["l_01_02"])

        self.assertIs(source, attachment_arm)

    def test_partial_attachment_armature_does_not_beat_complete_mesh_armature(self):
        group_arm = self._armature("flat_mesh_ARM", ["extra_a", "extra_b", "extra_c"])
        partial_attachment_arm = self._armature(
            "partial_attachment_ARM",
            ["Root", "extra_a"],
            {"extra_a": "Root"},
        )
        group_arm.parent = partial_attachment_arm

        source = bundle._find_attachment_armature_for_missing_bones(
            group_arm, ["extra_a", "extra_b", "extra_c"]
        )

        self.assertIs(source, group_arm)

    def test_retargeted_modifiers_restore_after_export(self):
        mesh_arm = object()
        main_arm = object()
        modifier = types.SimpleNamespace(type="ARMATURE", object=mesh_arm)
        mesh = types.SimpleNamespace(modifiers=[modifier])

        with bundle._retargeted_armature_modifiers([mesh], main_arm):
            self.assertIs(modifier.object, main_arm)
        self.assertIs(modifier.object, mesh_arm)


class TestBlueprintEntry(unittest.TestCase):
    def test_blueprint_uses_rig_asset_as_base_mesh_driver(self):
        rig_settings = types.SimpleNamespace(
            repo_path=r"characters\npc_entities\main_npc\geralt.w2ent",
            entity_name="geralt",
        )
        armature = types.SimpleNamespace(
            data=types.SimpleNamespace(witcherui_RigSettings=rig_settings)
        )

        entry = bundle._build_blueprint_entry(
            armature,
            "geralt",
            [{"kind": "skeletal", "asset_path": "characters/models/geralt/body/model/t_01_mg__body_hires"}],
            {"asset_path": "characters/base_entities/man_base/man_base"},
        )

        self.assertEqual(entry["asset_path"], "characters/npc_entities/main_npc/geralt")
        self.assertEqual(entry["base_mesh_asset_path"], "characters/base_entities/man_base/man_base")
        self.assertEqual(
            entry["mesh_asset_paths"],
            ["characters/models/geralt/body/model/t_01_mg__body_hires"],
        )
        self.assertNotIn("animation_asset_path", entry)

    def test_blueprint_keeps_ciri_body_as_visual_part(self):
        rig_settings = types.SimpleNamespace(
            repo_path=r"gameplay\templates\characters\player\ciri_player.w2ent",
            entity_name="ciri_player",
        )
        armature = types.SimpleNamespace(
            data=types.SimpleNamespace(witcherui_RigSettings=rig_settings)
        )

        entry = bundle._build_blueprint_entry(
            armature,
            "ciri_player",
            [
                {"kind": "skeletal", "asset_path": "characters/models/main_npc/ciri/model/item_07_wa__ciri"},
                {"kind": "skeletal", "asset_path": "characters/models/common/woman_average/body/model/h_wa__neck_transition"},
                {"kind": "skeletal", "asset_path": "characters/models/main_npc/ciri/model/body_01_wa__ciri"},
            ],
            {"asset_path": "characters/base_entities/woman_base/woman_base"},
        )

        self.assertEqual(entry["base_mesh_asset_path"], "characters/base_entities/woman_base/woman_base")
        self.assertEqual(
            entry["mesh_asset_paths"],
            [
                "characters/models/main_npc/ciri/model/item_07_wa__ciri",
                "characters/models/common/woman_average/body/model/h_wa__neck_transition",
                "characters/models/main_npc/ciri/model/body_01_wa__ciri",
            ],
        )

    def test_woman_base_blueprint_requests_retarget_setup(self):
        blueprint = {
            "base_mesh_asset_path": "characters/base_entities/woman_base/woman_base",
        }
        setup = bundle._build_retarget_setup(
            {"asset_path": "characters/base_entities/woman_base/woman_base"},
            blueprint,
        )

        self.assertEqual(setup["target_profile"], "woman_base")
        self.assertNotIn("template_ik_rig_asset_path", setup)
        self.assertEqual(setup["output_folder"], "/Game/RETARGET_")

    def test_man_base_blueprint_requests_retarget_setup(self):
        blueprint = {
            "base_mesh_asset_path": "characters/base_entities/man_base/man_base",
        }
        setup = bundle._build_retarget_setup(
            {"asset_path": "characters/base_entities/man_base/man_base"},
            blueprint,
        )

        self.assertEqual(setup["target_profile"], "man_base")
        self.assertEqual(setup["target_mesh_asset_path"], "characters/base_entities/man_base/man_base")
        self.assertEqual(setup["target_skeleton_asset_path"], "characters/base_entities/man_base/man_base_Skeleton")
        self.assertEqual(setup["output_folder"], "/Game/RETARGET_")

    def test_retarget_preview_asset_path_mirrors_blueprint_folder(self):
        self.assertEqual(
            bundle._preview_mesh_asset_rel_for_blueprint("gameplay/templates/characters/player/ciri_player"),
            "gameplay/templates/characters/player/SKM_ciri_player_RetargetPreview",
        )

    def test_blueprint_carries_first_exported_animation(self):
        rig_settings = types.SimpleNamespace(
            repo_path=r"characters\npc_entities\monsters\nekker_lvl1.w2ent",
            entity_name="nekker_lvl1",
        )
        armature = types.SimpleNamespace(
            data=types.SimpleNamespace(witcherui_RigSettings=rig_settings)
        )

        entry = bundle._build_blueprint_entry(
            armature,
            "nekker_lvl1",
            [{"kind": "skeletal", "asset_path": "characters/models/monsters/nekker/model/t_01__nekker"}],
            {"asset_path": "characters/base_entities/nekker_base/nekker_base"},
            [{"asset_path": "animations/monsters/monster_nekker/nekker_idle"},
             {"asset_path": "animations/monsters/monster_nekker/nekker_walk"}],
        )

        self.assertEqual(entry["asset_path"], "characters/npc_entities/monsters/nekker_lvl1")
        self.assertEqual(
            entry["animation_asset_path"],
            "animations/monsters/monster_nekker/nekker_idle",
        )


class TestDepotPaths(unittest.TestCase):
    def test_mesh_depot_path_maps_to_asset_rel(self):
        self.assertEqual(
            depot_asset_rel(r"characters\models\geralt\body\model\t_01_mg__body_hires.w2mesh"),
            "characters/models/geralt/body/model/t_01_mg__body_hires",
        )

    def test_master_graph_path_keeps_engine_layout(self):
        self.assertEqual(
            depot_asset_rel(r"engine\materials\graphs\pbr_std.w2mg"),
            "engine/materials/graphs/pbr_std",
        )

    def test_segments_are_sanitized_and_slashes_normalized(self):
        self.assertEqual(
            depot_asset_rel("characters/models/bad name/3start.xbm"),
            "characters/models/bad_name/_3start",
        )

    def test_texture_color_space_heuristics(self):
        self.assertTrue(texture_srgb_for_param("Diffuse"))
        self.assertFalse(texture_srgb_for_param("Normal"))
        self.assertEqual(texture_compression_for_param("Normal"), "normal_rgba")
        self.assertEqual(texture_compression_for_param("DetailNormal"), "normal_rgba")
        self.assertEqual(texture_compression_for_param("TintMask"), "masks")
        self.assertEqual(texture_compression_for_param("SpecularTexture"), "default")

    def test_explicit_rough_params_are_masks_not_normal_rgba(self):
        self.assertEqual(texture_compression_for_param("NormalRough"), "masks")
        self.assertEqual(texture_compression_for_param("DetailNormalRough"), "masks")
        self.assertEqual(texture_compression_for_param("RoughnessTexture"), "masks")
        self.assertFalse(texture_srgb_for_param("NormalRough"))


class TestParamConversion(unittest.TestCase):
    def test_texture_param_resolves_to_depot_reference(self):
        entries, warnings = convert_witcher_param(
            "Diffuse",
            "handle:ITexture",
            r"characters\models\geralt\body\model\t_01_mg__body_hires_d01.xbm",
            _stub_register_texture,
        )
        self.assertEqual(warnings, [])
        self.assertEqual(entries, [{
            "name": "Diffuse",
            "kind": "texture",
            "depot": "characters/models/geralt/body/model/t_01_mg__body_hires_d01",
        }])

    def test_cr2w_color_params_convert_from_255_range(self):
        entries, _ = convert_witcher_param("SpecularColor", "Color", "255; 128; 0; 255", None)
        self.assertEqual(entries[0]["kind"], "vector")
        self.assertAlmostEqual(entries[0]["value"][0], 1.0)
        self.assertAlmostEqual(entries[0]["value"][1], 128 / 255.0)
        self.assertAlmostEqual(entries[0]["value"][2], 0.0)

    def test_blender_rgb_params_stay_linear(self):
        entries, _ = convert_witcher_param("SpecularColor", "RGB", "0.25 ; 0.5 ; 0.75 ; 1.0", None)
        self.assertEqual(entries[0]["value"], [0.25, 0.5, 0.75, 1.0])

    def test_vector_params_are_not_rescaled(self):
        entries, _ = convert_witcher_param("UVTiling", "Vector", "4.0; 4.0; 0.0; 1.0", None)
        self.assertEqual(entries[0]["value"], [4.0, 4.0, 0.0, 1.0])

    def test_texture_arrays_defer_with_warning(self):
        entries, warnings = convert_witcher_param("Pattern", "handle:CTextureArray", "foo.texarray", None)
        self.assertEqual(entries, [])
        self.assertTrue(any("deferred" in warning for warning in warnings))

    def test_normal_param_does_not_create_roughness_sidecar(self):
        entries, _ = convert_witcher_param("Normal", "handle:ITexture", r"chars\n01.xbm", _stub_register_texture)
        self.assertEqual(entries, [{"name": "Normal", "kind": "texture", "depot": "chars/n01"}])


class TestChainBuilder(unittest.TestCase):
    """Simulates a Geralt body chain: local -> body material .w2mi -> base .w2mi -> pbr_std.w2mg."""

    BODY_MI = r"characters\models\geralt\body\materials\t_01_mg__body_hires.w2mi"
    BASE_MI = r"characters\models\geralt\body\materials\geralt_body_base.w2mi"
    GRAPH = r"engine\materials\graphs\pbr_std.w2mg"

    def _chain_reader(self, material_path, version):
        self.assertEqual(depot_asset_rel(material_path), depot_asset_rel(self.BODY_MI))
        return {
            "resolved_graph": self.GRAPH,
            "errors": [],
            "chain": [
                {"path": self.BODY_MI, "chunk_type": "CMaterialInstance", "_material_bin": "body_bin"},
                {"path": self.BASE_MI, "chunk_type": "CMaterialInstance", "_material_bin": "base_bin"},
                {"path": self.GRAPH, "chunk_type": "CMaterialGraph", "_material_bin": "graph_bin"},
            ],
        }

    def _params_reader(self, material_bin):
        return {
            "body_bin": {"Diffuse": ("handle:ITexture", r"characters\models\geralt\body\model\t_01_mg__body_hires_d01.xbm")},
            "base_bin": {"SpecularColor": ("Color", "255; 255; 255; 255")},
            "graph_bin": {"Roughness": ("Float", "0.7")},
        }.get(material_bin, {})

    def _make_builder(self):
        return ChainBuilder(
            _stub_register_texture,
            chain_reader=self._chain_reader,
            params_reader=self._params_reader,
            enable_mask_reader=lambda material_bin: False,
        )

    def _geralt_local_info(self):
        return {
            "name": "t_01_mg__body_hires_Material0",
            "material_slot_index": 0,
            "witcher_props": {
                "local": True,
                "base_custom": self.BODY_MI,
                "enableMask": False,
                "input_props": [
                    {"name": "Diffuse", "type": "TEX_IMAGE",
                     "value": r"characters\models\geralt\body\model\t_01_mg__body_hires_d01.xbm"},
                ],
            },
        }

    def test_chain_levels_emit_parent_first_at_depot_paths(self):
        builder = self._make_builder()
        local_id = builder.add_slot_material(
            self._geralt_local_info(), "characters/models/geralt/body/model"
        )

        materials = builder.ordered_materials()
        self.assertEqual(
            [m["asset_path"] for m in materials],
            [
                "characters/models/geralt/body/materials/geralt_body_base",
                "characters/models/geralt/body/materials/t_01_mg__body_hires",
                "characters/models/geralt/body/model/t_01_mg__body_hires_Material0",
            ],
        )
        self.assertEqual(local_id, "characters/models/geralt/body/model/t_01_mg__body_hires_Material0")

        base_body, body_material, local = materials
        self.assertEqual(base_body["parent_master"], "engine/materials/graphs/pbr_std")
        self.assertEqual(body_material["parent_material"], base_body["id"])
        self.assertEqual(local["parent_material"], body_material["id"])
        self.assertEqual(local["params"][0]["depot"],
                         "characters/models/geralt/body/model/t_01_mg__body_hires_d01")

    def test_master_declares_graph_defaults_and_instance_params(self):
        builder = self._make_builder()
        builder.add_slot_material(self._geralt_local_info(), "characters/models/geralt/body/model")

        masters = builder.ordered_masters()
        self.assertEqual(len(masters), 1)
        params_by_name = {p["name"]: p for p in masters[0]["params"]}
        self.assertIn("Roughness", params_by_name)
        self.assertEqual(params_by_name["Roughness"]["value"], 0.7)
        self.assertIn("Diffuse", params_by_name)
        self.assertIn("SpecularColor", params_by_name)

    def test_external_material_uses_chain_head_without_local_instance(self):
        builder = self._make_builder()
        slot_id = builder.add_slot_material(
            {
                "name": "t_01_mg__body_hires",
                "witcher_props": {
                    "local": False,
                    "base_custom": self.BODY_MI,
                    "input_props": [],
                },
            },
            "characters/models/geralt/body/model",
        )
        self.assertEqual(slot_id, "characters/models/geralt/body/materials/t_01_mg__body_hires")
        self.assertEqual(len(builder.ordered_materials()), 2)

    def test_second_slot_reuses_chain_entries(self):
        builder = self._make_builder()
        builder.add_slot_material(self._geralt_local_info(), "characters/models/geralt/body/model")
        other = self._geralt_local_info()
        other["name"] = "t_01_mg__body_hires_Material1"
        builder.add_slot_material(other, "characters/models/geralt/body/model")

        asset_paths = [m["asset_path"] for m in builder.ordered_materials()]
        self.assertEqual(len(asset_paths), len(set(asset_paths)))
        self.assertEqual(len([p for p in asset_paths if p.endswith("geralt_body_base")]), 1)

    def test_w2mg_base_parents_directly_to_master(self):
        builder = ChainBuilder(
            _stub_register_texture,
            chain_reader=lambda path, version: self.fail("w2mg base should not walk a chain"),
            params_reader=lambda material_bin: {},
        )
        builder.add_slot_material(
            {
                "name": "sword_mat",
                "witcher_props": {
                    "local": True,
                    "base_custom": r"engine\materials\graphs\pbr_std.w2mg",
                    "input_props": [{"name": "Diffuse", "type": "TEX_IMAGE", "value": r"items\sword_d01.xbm"}],
                },
            },
            "items/weapons/sword",
        )
        materials = builder.ordered_materials()
        self.assertEqual(len(materials), 1)
        self.assertEqual(materials[0]["parent_master"], "engine/materials/graphs/pbr_std")


class TestTextureRegistry(unittest.TestCase):
    def _make_registry(self, temp_dir):
        registry = texture_export.TextureRegistry(temp_dir)
        self._converted = []

        def fake_resolve(value):
            return "C:\\fake\\" + Path(str(value)).name

        def fake_convert(source_path, textures_dir, base_name):
            output = Path(textures_dir) / f"{base_name}.png"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"png")
            self._converted.append(str(output))
            return str(output)

        self._original = (
            texture_export.resolve_texture_path,
            texture_export.convert_texture_for_unreal,
        )
        texture_export.resolve_texture_path = fake_resolve
        texture_export.convert_texture_for_unreal = fake_convert
        return registry

    def tearDown(self):
        if hasattr(self, "_original"):
            (
                texture_export.resolve_texture_path,
                texture_export.convert_texture_for_unreal,
            ) = self._original

    def test_textures_export_once_with_flat_bundle_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = self._make_registry(temp_dir)
            depot = r"characters\models\geralt\body\model\t_01_mg__body_hires_d01.xbm"

            first = registry.register(depot, "Diffuse")
            second = registry.register(depot.replace("\\", "/"), "Diffuse")

            self.assertEqual(first, second)
            self.assertEqual(len(self._converted), 1)
            entries = registry.manifest_entries()
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["depot_path"],
                             "characters/models/geralt/body/model/t_01_mg__body_hires_d01")
            # Bundle files stay flat (depot-mirrored folders broke Windows
            # MAX_PATH in Blender); Unreal placement uses depot_path instead.
            self.assertEqual(entries[0]["file"], "Textures/t_01_mg__body_hires_d01.png")
            self.assertTrue(entries[0]["srgb"])

    def test_same_stem_from_different_depots_gets_unique_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = self._make_registry(temp_dir)

            first = registry.register(r"items\sword_a\diffuse.xbm", "Diffuse")
            second = registry.register(r"items\sword_b\diffuse.xbm", "Diffuse")

            self.assertNotEqual(first["depot"], second["depot"])
            files = sorted(entry["file"] for entry in registry.manifest_entries())
            self.assertEqual(files, ["Textures/diffuse.png", "Textures/diffuse_2.png"])

    def test_normal_textures_do_not_emit_roughness_sidecars(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = self._make_registry(temp_dir)
            registry.parallel = True

            registered = registry.register(r"items\wall\normal.xbm", "Normal")
            entries = registry.manifest_entries()

            self.assertEqual(registered["depot"], "items/wall/normal")
            self.assertEqual(len(self._converted), 1)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["compression"], "normal_rgba")
            self.assertFalse(any(entry["depot_path"].endswith("_rough") for entry in entries))

    def test_prefer_dds_ships_dds_without_transcode_or_rough(self):
        original = (texture_export.resolve_texture_path, texture_export.stage_texture_as_dds)
        staged = []

        def fake_stage(source_path, textures_dir, base_name):
            output = Path(textures_dir) / f"{base_name}.dds"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"dds")
            staged.append(str(output))
            return str(output)

        texture_export.resolve_texture_path = lambda value: "C:\\fake\\" + Path(str(value)).name
        texture_export.stage_texture_as_dds = fake_stage
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                registry = texture_export.TextureRegistry(temp_dir, prefer_dds=True)
                registry.register(r"items\wall\diffuse.xbm", "Diffuse")
                registry.register(r"items\wall\normal.xbm", "Normal")
                entries = registry.manifest_entries()
                files = sorted(entry["file"] for entry in entries)
                self.assertEqual(files, ["Textures/diffuse.dds", "Textures/normal.dds"])
                self.assertFalse(any(e["depot_path"].endswith("_rough") for e in entries))
                self.assertEqual(len(staged), 2)
        finally:
            texture_export.resolve_texture_path, texture_export.stage_texture_as_dds = original


class TestManifest(unittest.TestCase):
    def test_manifest_schema_and_content_root(self):
        manifest = build_manifest(
            asset_name="geralt body",
            bundle_root=r"F:\exports\geralt",
            meshes=[{"name": "t_01_mg__body_hires", "fbx": "Meshes/t_01_mg__body_hires.fbx",
                     "asset_path": "characters/models/geralt/body/model/t_01_mg__body_hires",
                     "kind": "skeletal", "slots": []}],
        )

        self.assertEqual(manifest["schema"], SCHEMA)
        self.assertEqual(manifest["source_game"], "w3")
        self.assertEqual(manifest["content_root"], "/Game/Witcher3")
        self.assertEqual(manifest["asset_name"], "geralt_body")
        self.assertNotIn("rig", manifest)
        self.assertNotIn("blueprint", manifest)

    def test_manifest_w2_default_content_root(self):
        manifest = build_manifest(
            asset_name="geralt body",
            bundle_root=r"F:\exports\geralt_w2",
            source_game="witcher2",
        )

        self.assertEqual(manifest["source_game"], "w2")
        self.assertEqual(manifest["content_root"], "/Game/Witcher2")

    def test_manifest_explicit_content_root_is_preserved(self):
        manifest = build_manifest(
            asset_name="geralt body",
            bundle_root=r"F:\exports\geralt",
            content_root="REDImport/",
        )

        self.assertEqual(manifest["content_root"], "/Game/REDImport")

    def test_manifest_includes_rig_and_blueprint_when_present(self):
        manifest = build_manifest(
            asset_name="geralt",
            bundle_root=r"F:\exports\geralt",
            rig={"name": "geralt", "fbx": "Meshes/geralt.fbx",
                 "asset_path": "characters/base_entities/geralt/geralt"},
            blueprint={"name": "geralt", "asset_path": "characters/npc_entities/main_npc/geralt",
                       "base_mesh_asset_path": "characters/base_entities/man_base/man_base",
                       "mesh_asset_paths": ["a", "b"]},
        )
        self.assertEqual(manifest["rig"]["asset_path"], "characters/base_entities/geralt/geralt")
        self.assertEqual(manifest["blueprint"]["name"], "geralt")
        self.assertEqual(
            manifest["blueprint"]["base_mesh_asset_path"],
            "characters/base_entities/man_base/man_base",
        )

    def test_manifest_includes_retarget_setup_when_present(self):
        manifest = build_manifest(
            asset_name="ciri",
            bundle_root=r"F:\exports\ciri",
            retarget_setup={"target_profile": "woman_base"},
        )
        self.assertEqual(manifest["retarget_setup"]["target_profile"], "woman_base")

    def test_manifest_includes_animation_entries(self):
        manifest = build_manifest(
            asset_name="geralt",
            bundle_root=r"F:\exports\geralt",
            animations=[{
                "name": "attack_fast_l_01",
                "fbx": "Animations/attack_fast_l_01.fbx",
                "asset_path": "animations/man/combat/man_geralt_sword/attack_fast_l_01",
                "source_animset": r"animations\man\combat\man_geralt_sword.w2anims",
            }],
        )
        self.assertEqual(
            manifest["animations"][0]["asset_path"],
            "animations/man/combat/man_geralt_sword/attack_fast_l_01",
        )

    def test_manifest_includes_speedtree_entries(self):
        manifest = build_manifest(
            asset_name="malus",
            bundle_root=r"F:\exports\malus",
            speedtrees=[{
                "asset_path": "environment/vegetation/trees/malus/malus",
                "depot_path": r"environment\vegetation\trees\malus\malus.srt",
                "file": "SpeedTrees/environment/vegetation/trees/malus/malus.srt",
                "texture_files": ["SpeedTrees/environment/vegetation/trees/malus/malus_d.dds"],
                "missing_textures": [],
                "force_import": True,
            }],
        )

        self.assertEqual(
            manifest["speedtrees"][0]["asset_path"],
            "environment/vegetation/trees/malus/malus",
        )
        self.assertTrue(manifest["speedtrees"][0]["force_import"])

    def test_manifest_includes_foliage_cells(self):
        manifest = build_manifest(
            asset_name="foliage_320.00_192.00",
            bundle_root=r"F:\exports\foliage",
            speedtrees=[{"asset_path": "environment/vegetation/trees/malus/malus", "force_import": True}],
            foliage={"cells": [{
                "layer_id": r"levels\prolog_village\source_foliage\foliage_320.00_192.00.flyr",
                "label": "foliage_320.00_192.00",
                "folder": "levels/prolog_village/source_foliage",
                "bounds": {"min": [0.0, 0.0], "max": [6400.0, 6400.0]},
                "types": [{
                    "name": "malus",
                    "asset_path": "environment/vegetation/trees/malus/malus",
                    "instances": [{"location": [1.0, 2.0, 3.0], "rotation": [0.0, 0.0, 0.0, 1.0], "scale": [1.0, 1.0, 1.0]}],
                }],
            }]},
        )

        cell = manifest["foliage"]["cells"][0]
        self.assertEqual(cell["types"][0]["asset_path"], "environment/vegetation/trees/malus/malus")
        # The FoliageType wraps the same mesh the SpeedTree import produces.
        self.assertEqual(cell["types"][0]["asset_path"], manifest["speedtrees"][0]["asset_path"])
        self.assertEqual(len(cell["types"][0]["instances"]), 1)
        # Foliage uses the `foliage` section, never `placements`.
        self.assertNotIn("placements", manifest)

    def test_speedtree_bundle_declares_material_and_wind_import_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            srt_path = Path(tmp) / "malus.srt"
            srt_path.write_bytes(b"SpeedTree___test")
            settings = types.SimpleNamespace(
                asset_name="",
                export_folder=str(Path(tmp) / "out"),
                content_root="",
            )
            context = types.SimpleNamespace(
                scene=types.SimpleNamespace(
                    witcher_file_browser=types.SimpleNamespace(loadmods=False),
                ),
            )

            with mock.patch.object(speedtree_bundle, "_collect_srt_texture_names", return_value=[]):
                result = speedtree_bundle.build_unreal_srt_bundle(
                    context,
                    settings,
                    str(srt_path),
                    r"environment\vegetation\trees\malus\malus.srt",
                )

        entry = result["manifest"]["speedtrees"][0]
        self.assertTrue(entry["force_import"])
        self.assertEqual(entry["asset_path"], "environment/vegetation/trees/malus/malus")
        self.assertEqual(entry["import_options"]["tree_scale"], 100.0)
        self.assertTrue(entry["import_options"]["create_materials"])
        self.assertTrue(entry["import_options"]["include_collision"])
        self.assertTrue(entry["import_options"]["fallback_trunk_collision"])
        self.assertTrue(entry["import_options"]["include_vertex_processing"])
        self.assertTrue(entry["import_options"]["include_wind"])
        self.assertEqual(entry["import_options"]["lod_screen_sizes"][1], 0.04)


class TestSocketClient(unittest.TestCase):
    def test_socket_message_uses_little_endian_length_prefix(self):
        payload = import_bundle_request(r"F:\bundle\witcher_unreal_export.json")
        encoded = encode_message(payload)
        size = int.from_bytes(encoded[:4], byteorder="little", signed=False)
        body = encoded[4:]

        self.assertEqual(size, len(body))
        decoded = json.loads(body.decode("utf-8"))
        self.assertEqual(decoded["command"], "import_bundle")
        self.assertEqual(decoded["schema"], SCHEMA)

    def test_send_import_request_reports_response_lost_after_request_is_sent(self):
        sent_payloads = []
        aborted = OSError("An established connection was aborted by the software in your host machine")
        aborted.winerror = 10053

        class FakeConnection:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def settimeout(self, timeout):
                self.timeout = timeout

            def setsockopt(self, *args):
                pass

            def ioctl(self, *args):
                pass

            def sendall(self, payload):
                sent_payloads.append(payload)

            def recv(self, size):
                raise aborted

        socket_module = send_import_request.__globals__["socket"]
        with mock.patch.object(socket_module, "create_connection", return_value=FakeConnection()):
            response = send_import_request(
                "127.0.0.1",
                40777,
                r"F:\bundle\witcher_unreal_export.json",
                timeout=1.0,
            )

        self.assertTrue(sent_payloads)
        self.assertTrue(response["success"])
        self.assertTrue(response["response_lost"])
        self.assertTrue(response["request_sent"])
        self.assertIn("lost the socket", response["warning"])

    def test_send_import_request_raises_when_request_was_not_sent(self):
        aborted = OSError("An established connection was aborted by the software in your host machine")
        aborted.winerror = 10053

        class FakeConnection:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def settimeout(self, timeout):
                pass

            def setsockopt(self, *args):
                pass

            def ioctl(self, *args):
                pass

            def sendall(self, payload):
                raise aborted

        socket_module = send_import_request.__globals__["socket"]
        with mock.patch.object(socket_module, "create_connection", return_value=FakeConnection()):
            with self.assertRaises(OSError):
                send_import_request(
                    "127.0.0.1",
                    40777,
                    r"F:\bundle\witcher_unreal_export.json",
                    timeout=1.0,
                )


class TestPluginInstall(unittest.TestCase):
    def test_default_plugin_source_is_bundled_inside_addon(self):
        source = Path(default_plugin_source())
        expected = REPO_ROOT / "witcher3_tools" / "unreal_export" / PLUGIN_NAME

        self.assertEqual(source, expected)
        self.assertTrue((source / PLUGIN_DESCRIPTOR).exists())

    def test_plugin_target_resolves_project_plugins_dir(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project_file = Path(temp_dir) / "WitcherTest.uproject"
            project_file.write_text("{}", encoding="utf-8")

            target = Path(plugin_target_dir(project_file))

            self.assertEqual(target, Path(temp_dir) / "Plugins" / PLUGIN_NAME)

    def test_install_plugin_copies_source_and_ignores_build_artifacts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project_file = root / "WitcherTest.uproject"
            project_file.write_text("{}", encoding="utf-8")

            source_dir = root / "SourcePlugin"
            (source_dir / "Source").mkdir(parents=True)
            (source_dir / "Binaries").mkdir()
            (source_dir / f"{PLUGIN_NAME}.uplugin").write_text("{}", encoding="utf-8")
            (source_dir / "Source" / "Importer.cpp").write_text("// source", encoding="utf-8")
            (source_dir / "Binaries" / "skip.dll").write_text("binary", encoding="utf-8")

            result = install_or_update_plugin(project_file, source_dir)
            target = Path(result["target_dir"])

            self.assertTrue(result["updated"])
            self.assertTrue((target / f"{PLUGIN_NAME}.uplugin").exists())
            self.assertTrue((target / "Source" / "Importer.cpp").exists())
            self.assertFalse((target / "Binaries").exists())

    def test_install_plugin_refuses_unexpected_existing_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project_file = root / "WitcherTest.uproject"
            project_file.write_text("{}", encoding="utf-8")

            source_dir = root / "SourcePlugin"
            source_dir.mkdir()
            (source_dir / f"{PLUGIN_NAME}.uplugin").write_text("{}", encoding="utf-8")

            target = root / "Plugins" / PLUGIN_NAME
            target.mkdir(parents=True)
            (target / "keep.txt").write_text("not our plugin", encoding="utf-8")

            with self.assertRaises(ValueError):
                install_or_update_plugin(project_file, source_dir)


class TestUnrealProjectInspection(unittest.TestCase):
    def test_project_inspection_reads_engine_and_missing_plugin(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project_file = Path(temp_dir) / "WitcherTest.uproject"
            project_file.write_text(json.dumps({"EngineAssociation": "5.4"}), encoding="utf-8")

            info = unreal_project.inspect_project(project_file)

            self.assertTrue(info["exists"])
            self.assertEqual(info["engine_association"], "5.4")
            self.assertFalse(info["plugin_installed"])
            self.assertEqual(unreal_project.plugin_status_label(info), "Not installed")
            self.assertEqual(unreal_project.short_project_status(info), "UE 5.4; Plugin missing")
            self.assertIn("EngineAssociation: 5.4", unreal_project.format_project_details(info))

    def test_project_inspection_reads_installed_plugin_version(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project_file = root / "WitcherTest.uproject"
            project_file.write_text(json.dumps({"EngineAssociation": "5.5"}), encoding="utf-8")

            descriptor = root / "Plugins" / PLUGIN_NAME / f"{PLUGIN_NAME}.uplugin"
            descriptor.parent.mkdir(parents=True)
            descriptor.write_text(
                json.dumps({"Version": 7, "VersionName": "0.2.0"}),
                encoding="utf-8",
            )

            info = unreal_project.inspect_project(project_file)

            self.assertTrue(info["plugin_installed"])
            self.assertEqual(info["plugin_version"], "7")
            self.assertEqual(info["plugin_version_name"], "0.2.0")
            self.assertEqual(unreal_project.plugin_status_label(info), "Installed (0.2.0)")
            self.assertEqual(unreal_project.short_project_status(info), "UE 5.5; Plugin installed")


if __name__ == "__main__":
    unittest.main()
