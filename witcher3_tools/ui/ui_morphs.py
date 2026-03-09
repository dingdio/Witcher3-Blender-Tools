import logging
import bpy
from typing import Tuple

log = logging.getLogger(__name__)
from bpy.props import StringProperty, BoolProperty
from mathutils import Vector

from ..CR2W.common_blender import repo_file, mod_loading_context
from ..ui.ui_utils import WITCH_PT_Base
from ..ui.armature_context import get_main_armature_and_rig_settings, set_main_armature
from ..CR2W.CR2W_types import dotdict
from ..CR2W.w3_types import CSkeletalAnimationSetEntry
from ..CR2W.dc_anims import load_lipsync_file
from ..importers import import_anims
from ..importers import import_rig
from bpy.types import PropertyGroup
from bpy.props import IntProperty, StringProperty, CollectionProperty, BoolProperty, EnumProperty

def update_rot90_comp(self, context):
    """Keep Rot90 state lightweight; explicit refresh is handled by dedicated operators."""
    return

def update_variants_enabled(self, context):
    """Refresh variant state and reload equipment when toggle changes."""
    try:
        armature_obj = None
        ob = getattr(context, "object", None) if context else None
        if ob and ob.type == 'ARMATURE':
            armature_obj = ob
        else:
            arm_data = getattr(self, "id_data", None)
            if arm_data:
                for obj in bpy.data.objects:
                    if obj.type == 'ARMATURE' and obj.data == arm_data:
                        armature_obj = obj
                        break
        if not armature_obj:
            return

        rig_settings = armature_obj.data.witcherui_RigSettings
        if getattr(rig_settings, "variants_auto", False):
            return
        slots = rig_settings.equipment_slots
        slot_index = None
        for i, slot in enumerate(slots):
            if slot == self:
                slot_index = i
                break
        if slot_index is None:
            return

        from ..ui.ui_equipment import refresh_variant_states, load_equipment_item
        refresh_variant_states(rig_settings)
        if self.is_loaded:
            saved_active = context.view_layer.objects.active
            saved_selection = [obj for obj in context.selected_objects]
            load_equipment_item(context, armature_obj, slot_index, rig_settings)
            bpy.ops.object.select_all(action='DESELECT')
            for obj in saved_selection:
                try:
                    if obj and obj.name in bpy.data.objects:
                        obj.select_set(True)
                except ReferenceError:
                    continue
            try:
                if saved_active and saved_active.name in bpy.data.objects:
                    context.view_layer.objects.active = saved_active
            except ReferenceError:
                pass
    except Exception:
        pass


_AUTO_LOADING_APPEARANCE = False


def on_app_list_index_changed(self, context):
    """Auto-load selected appearance when enabled in scene settings."""
    global _AUTO_LOADING_APPEARANCE
    if _AUTO_LOADING_APPEARANCE:
        return

    scene = getattr(context, "scene", None) if context else None
    if not scene or not getattr(scene, "witcher_load_app_on_select", False):
        return

    app_list = getattr(self, "app_list", None)
    app_index = int(getattr(self, "app_list_index", -1))
    if app_index < 0 or not app_list or app_index >= len(app_list):
        return

    arm_data = getattr(self, "id_data", None)
    if arm_data is None:
        return

    arm_obj = None
    for obj in scene.objects:
        if obj.type == "ARMATURE" and obj.data == arm_data:
            arm_obj = obj
            break
    if arm_obj is None:
        return

    # Skip callback while import_ent_template is still building app_list.
    if arm_obj.get("_w3_entity_import_in_progress", False):
        return

    item = app_list[app_index]
    try:
        from ..importers import import_entity
        _AUTO_LOADING_APPEARANCE = True
        set_main_armature(scene, arm_obj)
        with mod_loading_context(context):
            import_entity.import_from_list_item(context, item)
        try:
            bpy.ops.object.select_all(action='DESELECT')
            arm_obj.select_set(True)
            bpy.context.view_layer.objects.active = arm_obj
        except Exception:
            pass
    except Exception as exc:
        log.warning("Auto-load appearance failed: %s", exc)
    finally:
        _AUTO_LOADING_APPEARANCE = False

class witcherui_redmorph(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty(name = "Name")
    path: bpy.props.StringProperty(name = "Path")
    type: bpy.props.IntProperty(name = "Type")
    value: bpy.props.FloatProperty(name = "value")

class ListItemBone(PropertyGroup):
    """."""
    name: StringProperty(
           name="Bone",
           description="Name of bone",
           default="")


class ListItemAnimset(PropertyGroup):
    """."""
    path: StringProperty(
           name="Path",
           description="Path to Animset",
           default="Untitled")

    name: StringProperty(
           name="Name",
           description="",
           default="")


class ListItemApp(PropertyGroup):
    """Group of properties representing an item in the list."""

    name: StringProperty(
           name="Name",
           description="Name of the animation",
           default="Untitled")

    jsonData: StringProperty(
           name="Animation in Json",
           description="",
           default="")


# =============================================================================
# Persistent Equipment & Template Slot Entries (stored on armature)
# =============================================================================

def _update_rune_level(self, context):
    """Deferred callback to update rune_normal mapping nodes."""
    from ..ui.ui_equipment import update_rune_level
    update_rune_level(self, context)


def _get_item_app_enum_items(self, context):
    import json
    try:
        names = json.loads(self.item_appearances_json or '[]')
    except Exception:
        names = []
    if not names:
        return [('__default__', 'Default', '')]
    return [(n, n, '') for n in names]


def _on_item_appearance_changed(self, context):
    """Reload the equipment item when the user picks a different appearance."""
    try:
        armature_obj = None
        ob = getattr(context, "object", None) if context else None
        if ob and ob.type == 'ARMATURE':
            armature_obj = ob
        else:
            arm_data = getattr(self, "id_data", None)
            if arm_data:
                for obj in bpy.data.objects:
                    if obj.type == 'ARMATURE' and obj.data == arm_data:
                        armature_obj = obj
                        break
        if not armature_obj:
            return

        rig_settings = armature_obj.data.witcherui_RigSettings
        slots = rig_settings.equipment_slots
        slot_index = None
        for i, slot in enumerate(slots):
            if slot == self:
                slot_index = i
                break
        if slot_index is None:
            return

        from ..ui.ui_equipment import load_equipment_item
        if self.is_loaded:
            saved_active = context.view_layer.objects.active
            saved_selection = [obj for obj in context.selected_objects]
            load_equipment_item(context, armature_obj, slot_index, rig_settings)
            bpy.ops.object.select_all(action='DESELECT')
            for obj in saved_selection:
                try:
                    if obj and obj.name in bpy.data.objects:
                        obj.select_set(True)
                except ReferenceError:
                    continue
            try:
                if saved_active and saved_active.name in bpy.data.objects:
                    context.view_layer.objects.active = saved_active
            except ReferenceError:
                pass
    except Exception:
        pass


class EquipmentSlotEntry(bpy.types.PropertyGroup):
    """Persistent equipment slot stored on the armature. Survives Blender restarts."""
    category: StringProperty(name="Category", default="")
    item_name: StringProperty(name="Item Name", default="")
    equip_template: StringProperty(name="Equip Template", default="")
    base_equip_template: StringProperty(name="Base Equip Template", default="")
    equip_guid: StringProperty(name="Equip GUID", default="")
    is_inventory: BoolProperty(name="Is Inventory", default=False)
    equip_slot: StringProperty(name="Equip Slot", default="")
    hold_slot: StringProperty(name="Hold Slot", default="")
    weapon: BoolProperty(name="Weapon", default=False)
    attachment_type: StringProperty(name="Attachment Type", default="")
    variants_json: StringProperty(name="Variants JSON", default="")
    bound_items_json: StringProperty(name="Bound Items JSON", default="")
    variants_enabled: BoolProperty(name="Variants Enabled", default=False, update=update_variants_enabled)
    variant_active: BoolProperty(name="Variant Active", default=False)
    variant_template: StringProperty(name="Variant Template", default="")
    variant_category: StringProperty(name="Variant Category", default="")
    variant_equip_slot: StringProperty(name="Variant Equip Slot", default="")
    variant_hold_slot: StringProperty(name="Variant Hold Slot", default="")
    is_loaded: BoolProperty(name="Is Loaded", default=False)
    is_in_hold_slot: BoolProperty(name="In Hold Slot", default=False, description="True if equipment is in hold slot, False if in mount slot")
    rune_level: EnumProperty(
        name="Rune Level",
        items=[
            ('NONE', "None", "No rune"),
            ('1', "1", "Rune level 1"),
            ('2', "2", "Rune level 2"),
            ('3', "3", "Rune level 3"),
        ],
        default='NONE',
        update=_update_rune_level
    )
    item_appearances_json: StringProperty(
        name="Item Appearances JSON",
        default="",
        description="JSON array of appearance names available on this item entity"
    )
    item_appearance_name: EnumProperty(
        name="Item Appearance",
        items=_get_item_app_enum_items,
        update=_on_item_appearance_changed,
        description="Select appearance (dye variant) for this item"
    )
    item_coloring_json: StringProperty(
        name="Item Coloring JSON",
        default="",
        description="JSON list of coloring entries for the selected appearance"
    )


class TemplateSlotEntry(bpy.types.PropertyGroup):
    """Persistent included template slot stored on the armature. Survives Blender restarts."""
    template_filename: StringProperty(name="Template Filename", default="")
    ns: StringProperty(name="Namespace", default="")
    template_guid: StringProperty(name="Template GUID", default="")
    data_json: StringProperty(name="Template Data JSON", default="")
    is_loaded: BoolProperty(name="Is Loaded", default=False)
    is_hidden: BoolProperty(name="Is Hidden", default=False)
    appearance_names: StringProperty(name="Appearances", default="", description="Comma-separated list of appearances using this template")
    # Per-appearance visibility: JSON dict like {"app1": true, "app2": false}
    # True = hidden in that appearance, False = visible in that appearance
    hidden_in_appearances: StringProperty(name="Hidden In Appearances", default="{}", description="JSON dict of per-appearance hidden state")


class EntitySlotEntry(bpy.types.PropertyGroup):
    """Persistent entity slot (maps to Witcher EntitySlot). Stored on armature, survives restarts."""
    slot_name: StringProperty(name="Slot Name", default="")
    component_name: StringProperty(name="Component Name", default="")
    bone_name: StringProperty(name="Bone Name", default="")
    transform_json: StringProperty(name="Transform JSON", default="{}")
    free_position_x: BoolProperty(default=False)
    free_position_y: BoolProperty(default=False)
    free_position_z: BoolProperty(default=False)
    free_rotation: BoolProperty(default=False)


class witcherui_MeshSettings(bpy.types.PropertyGroup):
    lod_level: bpy.props.IntProperty(default = 0)
    distance: bpy.props.FloatProperty(default = 0)
    mat_id: bpy.props.IntProperty(default = 0)
    
    autohideDistance: bpy.props.FloatProperty(default = 200.0,
                        name = "Auto Hide Distance",
                        description = "Hide mesh after this distance")
    isTwoSided: bpy.props.BoolProperty(default = False,
                        name = "Is Two Sided",
                        description = "Render mesh on both sides")
    useExtraStreams: bpy.props.BoolProperty(default = False,
                        name = "Use Extra Streams",
                        description = "Use vertex color and Second UV on this mesh")
    generalizedMeshRadius: bpy.props.FloatProperty(default = 0.0,
                        name = "Generalized Mesh Radius",
                        description = "Generalized mesh size (generated on export)")
    mergeInGlobalShadowMesh: bpy.props.BoolProperty(default = True,
                        name = "Merge In Global Shadow Mesh",
                        description = "Allow chunks to be extracted into global shadow mesh")
    isOccluder: bpy.props.BoolProperty(default = True,
                        name = "Is Occluder",
                        description = "Is mesh used as occluder?")
    smallestHoleOverride: bpy.props.FloatProperty(default = -1.0,
                        name = "Smallest Hole Override",
                        description = "Temporary override for the smallest hole parameter for this mesh. (-1 is default)")
    isStatic: bpy.props.BoolProperty(default = True,
                        name = "Is Static",
                        description = "Is this mesh static?")
    entityProxy: bpy.props.BoolProperty(default = False,
                        name = "Entity Proxy",
                        description = "Is this a generated entity proxy")

    soundInfo_enabled: bpy.props.BoolProperty(default = False,
                        name = "Has Sound Info",
                        description = "Mesh has SMeshSoundInfo data")
    soundInfo_soundTypeIdentification: bpy.props.StringProperty(default = "",
                        name = "Sound Type Identification",
                        description = "Material type for sound (e.g. flesh, metal, wood)")
    soundInfo_soundSizeIdentification: bpy.props.StringProperty(default = "default",
                        name = "Sound Size Identification",
                        description = "Size/weight modifier for sound (e.g. default)")
    soundInfo_soundBoneMappingInfo: bpy.props.EnumProperty(
                        name = "Bone Mapping Preset",
                        description = "Which bones to track for spatial audio and foley",
                        items = [
                            ('NONE', "None", "No bone mapping"),
                            ('TorsoArmor', "TorsoArmor", "Map to torso3, torso bones (default fallback)"),
                            ('LegArmor', "LegArmor", "Map to l_thigh, r_thigh bones"),
                            ('HandArmor', "HandArmor", "Map to l_hand, r_hand bones"),
                            ('HeadArmor', "HeadArmor", "Map to head bone"),
                        ],
                        default = 'NONE')

    item_repo_path:bpy.props.StringProperty(default = "",
                        name = "Repo Path",
                        description = "Path for this in game. Including filename and .w2mesh extension")
    make_export_dir: bpy.props.BoolProperty(default = False,
                        name = "Make Mod Dirs",
                        description = "True: Create directories inside mod folder if they don't exist")
    is_DLC: bpy.props.BoolProperty(default = False,
                        name = "Is DLC",
                        description = "True: Use the DLC folder instead of Mod folder")
    
    witcher_meshexport_collapse: bpy.props.BoolProperty(default = False)

def _phoneme_enabled_update_callback(self, context):
    """Sync the phoneme_enabled toggle to the pose bone float property used by drivers.

    Shape key drivers read the toggle from ``pose_bone["phoneme_enabled"]`` (a plain float
    0.0/1.0) rather than the PointerProperty sub-path, because the latter is unreliable as a
    Blender driver variable target.  This callback keeps them in sync whenever the UI toggle
    changes.
    """
    arm_data = self.id_data  # bpy.types.Armature that owns this PropertyGroup
    if arm_data is None:
        return
    for arm_obj in bpy.data.objects:
        if arm_obj.type == 'ARMATURE' and arm_obj.data is arm_data:
            pb = arm_obj.pose.bones.get("w3_face_poses")
            if pb is not None:
                if "phoneme_enabled" not in pb:
                    pb["phoneme_enabled"] = 1.0
                    prop_ui = pb.id_properties_ui("phoneme_enabled")
                    prop_ui.update(min=0.0, max=1.0)
                pb["phoneme_enabled"] = 1.0 if self.phoneme_enabled else 0.0
            break


class witcherui_RigSettings(bpy.types.PropertyGroup):
    model_name: bpy.props.StringProperty(default = "",
                        name = "Model name",
                        description = "Model name")
    rot90_state: EnumProperty(
                        name="Rot90 State",
                        description="Current rig orientation state for Blender display compatibility",
                        items=[
                            ('UNKNOWN', "Unknown", "Legacy scene without explicit Rot90 state"),
                            ('OFF', "Off", "Game-space rig orientation (no display fix)"),
                            ('ON', "On", "Blender display fix applied"),
                        ],
                        default='UNKNOWN')
    rot90_imported: bpy.props.BoolProperty(default=False,
                        name="Rig Rotated 90",
                        description="True if bones were imported with 90-degree rotation")
    rot90_compensate: bpy.props.BoolProperty(default=False,
                        name="Compensate Rot90",
                        description="Apply 90-degree compensation to slots/equipment",
                        update=update_rot90_comp)
    variants_auto: bpy.props.BoolProperty(default=True,
                        name="Variants Auto",
                        description="Auto-enable variants when their category is equipped")
    equipment_ui_tab: EnumProperty(
                        name="Equipment Tab",
                        items=[
                            ('APPEARANCE', "Appearance", "Select and load appearances"),
                            ('TEMPLATES', "Templates", "Manage included templates"),
                            ('EQUIPMENT', "Equipment", "Manage equipment entries and slots"),
                            ('SLOTS', "Slots", "View entity slots and mounting points"),
                        ],
                        default='EQUIPMENT')
    def poll_mesh(self, object):
        return object.type == 'MESH'
    model_body: bpy.props.PointerProperty(name = "Model Body",
                        description = "",
                        type = bpy.types.Object,
                        poll = poll_mesh)
    def poll_armature(self, object):
        if object.type == 'ARMATURE':
            return object.data == self.id_data
        else:
            return False
    model_armature_object: bpy.props.PointerProperty(name = "Model Armature Object",
                        description = "",
                        type = bpy.types.Object,
                        poll = poll_armature)

    witcher_morphs_list: bpy.props.CollectionProperty(name = "Witcher Morphs List",
                        type=witcherui_redmorph)

    witcher_morphs_number: bpy.props.IntProperty(default = 0,
                        name = "")
    witcher_face_morphs: bpy.props.BoolProperty(default = True,
                        name = "Morphs from mimic poses",
                        description = "Search for witcher Body morphs")
    witcher_morphs_collapse: bpy.props.BoolProperty(default = True)
    witcher_morphs_collapse2: bpy.props.BoolProperty(default = True)
    phoneme_enabled: bpy.props.BoolProperty(default = True,
                        name = "Phoneme Control",
                        description = "Enable phoneme-driven control of face morphs",
                        update = _phoneme_enabled_update_callback)
    morph_search_filter: bpy.props.StringProperty(default = "",
                        name = "",
                        description = "Morph Seach Filter")
    
    #Tracks
    witcher_tracks_list: bpy.props.CollectionProperty(name = "Tracks",
                        type=witcherui_redmorph)
    witcher_tracks_collapse: bpy.props.BoolProperty(default = True)

    #apperance list
    app_list : CollectionProperty(type = ListItemApp)
    app_list_index : IntProperty(name = "Index for app_list",
                                             default = 0,
                                             update=on_app_list_index_changed)
    
    main_entity_skeleton : StringProperty(
                                            name="Main Rig",
                                            description="Name of the rig",
                                            default="")

    main_face_skeleton : StringProperty(
                                            name="Main Face Rig",
                                            description="Name of the rig",
                                            default="")
    repo_path : StringProperty(
                                            name="Entity File",
                                            description="Entity Location in game files",
                                            default="")
    entity_name : StringProperty(
                                            name="Entity Name",
                                            description="Entity Name",
                                            default="")
    
    do_import_lods : BoolProperty(
                                            name="Include LODs",
                                            description="Include LODs",
                                            default=0)

    #animset list
    animset_list : CollectionProperty(type = ListItemAnimset)
    animset_list_index : IntProperty(name = "Index for Animset list",
                                             default = 0)

    jsonData: StringProperty(name="Json Data",
                            description="Json Data of entire character",
                            default="")

    bone_order_list : CollectionProperty(type=ListItemBone)

    # Persistent equipment slots (GUID-tracked, survives restarts)
    equipment_slots : CollectionProperty(type=EquipmentSlotEntry)
    equipment_slots_index : IntProperty(name="Equipment Slot Index", default=0)

    # Persistent template slots (GUID-tracked, survives restarts)
    template_slots : CollectionProperty(type=TemplateSlotEntry)
    template_slots_index : IntProperty(name="Template Slot Index", default=0)

    # Persistent entity slots (maps to Witcher EntitySlot - equipment mounting points)
    entity_slots : CollectionProperty(type=EntitySlotEntry)
    entity_slots_index : IntProperty(name="Entity Slot Index", default=0)
    show_entity_slots : BoolProperty(name="Show Entity Slots", default=False, 
                                      description="Toggle visibility of entity slot empties in viewport")

class WITCH_PT_WitcherMorphs(WITCH_PT_Base, bpy.types.Panel):
    # Embedded into Character panel's Morphs tab — hidden as standalone sub-panel.
    bl_idname = "WITCH_PT_WitcherMorphs"
    bl_label = "Morphs"

    @classmethod
    def poll(cls, context):
        return False  # Content embedded via Character panel tabs

    def draw(self, context):
        ob = context.object
        coll = context.collection
        scn = context.scene
        layout:bpy.types.UILayout = self.layout
        box = layout.box()
        # if ob:
        #     box.label(text = "Active Object: %s" % ob.entity_type)
        #     box.prop(ob, "name")
        #     if ob.template:
        #         box.prop(ob, "template")
        #     if ob.entity_type:
        #         box.prop(ob, "entity_type")
        # else:
        #     box.label(text = "No active object")
        box.operator(WITCH_OT_morphs.bl_idname, text="Load Face Morphs", icon='SHAPEKEY_DATA')
        box.operator(WITCH_OT_phonemes.bl_idname, text="Create Phonemes", icon='SHAPEKEY_DATA')

        # --- Loaded lipsync status ---
        pre_arm_obj = get_main_armature_and_rig_settings(
            context, prefer_active=True, remember=False, fallback=True,
        )[0]
        voice_tracks = _get_loaded_voice_tracks(pre_arm_obj) if pre_arm_obj else []
        if voice_tracks:
            status_box = layout.box()
            status_box.label(text="Loaded Lipsync", icon='SPEAKER')
            for track_name, action_name, f_start, f_end in voice_tracks:
                label = "Phonemes" if "phoneme" in track_name else "Morphs"
                status_box.label(text=f"{label}: {action_name}  [{int(f_start)}-{int(f_end)}]", icon='NLA')
            status_box.operator(WITCH_OT_clear_lipsync.bl_idname, text="Clear Lipsync & Reset Morphs", icon='TRASH')

        main_arm_obj, rig_settings = get_main_armature_and_rig_settings(
            context,
            prefer_active=True,
            remember=True,
            fallback=True,
        )
        if main_arm_obj and rig_settings:
            layout = self.layout

            row = layout.row()
            row.prop(rig_settings, "morph_search_filter", icon = "VIEWZOOM")

            control_arm_obj = rig_settings.model_armature_object or main_arm_obj
            if rig_settings.witcher_face_morphs and control_arm_obj:
                box = layout.box()
                row = box.row(align=False)
                #body_morphs = [x for x in rig_settings.witcher_morphs_list if x.type == 4] #and self.morph_filter(x, rig_settings)]
                row.prop(rig_settings, "witcher_morphs_collapse", icon="TRIA_DOWN" if not rig_settings.witcher_morphs_collapse else "TRIA_RIGHT", icon_only=True, emboss=False)
                face_morphs = [x for x in rig_settings.witcher_morphs_list if x.type == 4 and rig_settings.morph_search_filter.lower() in x.name.lower()]
                face_phonemes = [x for x in rig_settings.witcher_morphs_list if x.type == 5 and rig_settings.morph_search_filter.lower() in x.name.lower()]
                face_total = len(face_morphs) + len(face_phonemes)

                row.label(text="Face (" + str(face_total) + ")")
                box.prop(rig_settings, "phoneme_enabled", text="Phoneme Control")
                if not rig_settings.witcher_morphs_collapse:
                    the_data = control_arm_obj.pose.bones.get("w3_face_poses")
                    if the_data is None:
                        box.label(text="Missing w3_face_poses control bone. Reload face morphs.", icon='ERROR')
                        return

                    ref_keys = None
                    if rig_settings.phoneme_enabled:
                        face_rig_name = main_arm_obj.get('mimicFace')
                        if face_rig_name:
                            face_meshes = _resolve_face_mesh_names(main_arm_obj, face_rig_name)
                            if face_meshes:
                                ref_mesh = scn.objects.get(face_meshes[0])
                                if ref_mesh and ref_mesh.data.shape_keys:
                                    ref_keys = ref_mesh.data.shape_keys.key_blocks

                    def _draw_morphs_section():
                        if not face_morphs:
                            return
                        box.label(text="Morphs (" + str(len(face_morphs)) + ")")
                        for morph in face_morphs:
                            if _pose_bone_has_custom_prop(the_data, morph.path):
                                box.prop(the_data, '[\"' + morph.path + '\"]', text = morph.name)
                            elif rig_settings.phoneme_enabled and ref_keys and morph.path in ref_keys:
                                morph_col = box.column()
                                morph_col.enabled = False
                                morph_col.prop(ref_keys[morph.path], "value", text = morph.name)

                    def _draw_phonemes_section():
                        if not face_phonemes:
                            return
                        box.label(text="Phonemes (" + str(len(face_phonemes)) + ")")
                        phoneme_col = box.column()
                        phoneme_col.enabled = rig_settings.phoneme_enabled
                        for morph in face_phonemes:
                            if _pose_bone_has_custom_prop(the_data, morph.path):
                                phoneme_col.prop(the_data, '[\"' + morph.path + '\"]', text = morph.name)

                    if rig_settings.phoneme_enabled:
                        _draw_phonemes_section()
                        _draw_morphs_section()
                    else:
                        _draw_morphs_section()
                        _draw_phonemes_section()

                box = layout.box()
                row = box.row(align=False)
                row.prop(rig_settings, "witcher_morphs_collapse2", icon="TRIA_DOWN" if not rig_settings.witcher_morphs_collapse2 else "TRIA_RIGHT", icon_only=True, emboss=False)
                body_comp_morphs = [x for x in rig_settings.witcher_morphs_list if x.type == 3 and rig_settings.morph_search_filter.lower() in x.name.lower()]
                row.label(text="Morph Components (" + str(len(body_comp_morphs)) + ")")
                if not rig_settings.witcher_morphs_collapse2:
                    the_data = control_arm_obj.pose.bones.get("w3_face_poses")
                    if the_data is None:
                        box.label(text="Missing w3_face_poses control bone. Reload face morphs.", icon='ERROR')
                        return

                    for morph in body_comp_morphs:
                        if _pose_bone_has_custom_prop(the_data, morph.path):
                            box.prop(the_data, '[\"' + morph.path + '\"]', text = morph.name)
                        else:
                            pass
#import bpy

import io
from contextlib import redirect_stdout, redirect_stderr
import os
import sys


def create_morph_and_driver(self, obj, mesh_bl_o, this_POSE_name, control_bone_name='w3_face_poses'):
    bpy.context.view_layer.objects.active = mesh_bl_o

    shape_keys = mesh_bl_o.data.shape_keys
    if shape_keys and this_POSE_name in shape_keys.key_blocks:
        new_morph = shape_keys.key_blocks[this_POSE_name]
    else:
        apply_ret = bpy.ops.object.modifier_apply_as_shapekey(keep_modifier=True, modifier="Armature", report=False)
        if 'FINISHED' not in apply_ret:
            self.report({'ERROR'}, "Error applying modifier, Object: {0}, ShapeKey: {1}, apply modifier: {2}".format(mesh_bl_o.name, this_POSE_name, apply_ret))
            return False
        new_morph = mesh_bl_o.data.shape_keys.key_blocks[-1]
        new_morph.name = this_POSE_name  # rename

    driver_curve = new_morph.driver_add("value")
    driver = driver_curve.driver
    channel = this_POSE_name
    driver.expression = channel
    var = driver.variables.get(channel)
    if var is None:
        var = driver.variables.new()
    var.type = "SINGLE_PROP"
    var.name = channel
    target = var.targets[0]
    target.id_type = "OBJECT"
    target.data_path = 'pose.bones["%s"]["%s"]' % (control_bone_name, channel)
    target.id = obj
    return True

def witcherui_add_redmorph(collection, item, value = 0.0, existing_keys=None):
    key = (item[0], item[1], item[2])
    if existing_keys is None:
        for el in collection:
            if el.name == key[0] and el.path == key[1] and el.type == key[2]:
                return
    else:
        if key in existing_keys:
            return

    add_item = collection.add()
    add_item.name = key[0]
    add_item.path = key[1]
    add_item.type = key[2]
    add_item.value = value
    if existing_keys is not None:
        existing_keys.add(key)
    return add_item

def get_face_meshs(mimicFace: str) -> Tuple:
    face_arms = []
    face_meshes = []
    #face_rig =bpy.context.scene.objects[mimicFace]
    all_objs = bpy.data.objects
    for arm_obj in all_objs:
        if arm_obj.type != 'ARMATURE':
            continue
        for bone in arm_obj.pose.bones:
            for constraint in bone.constraints:
                target = getattr(constraint, "target", None)
                if target and target.name == mimicFace and arm_obj.name not in face_arms:
                    face_arms.append(arm_obj.name)

    for mesh_obj in all_objs:
        if mesh_obj.type != 'MESH':
            continue
        for modifier in mesh_obj.modifiers:
            if modifier.type != 'ARMATURE':
                continue
            mod_target = getattr(modifier, "object", None)
            if mod_target and mod_target.name in face_arms and mesh_obj.name not in face_meshes:
                face_meshes.append(mesh_obj.name)
    return (face_meshes, face_arms)


def _get_face_meshs_merged(main_obj, faceData) -> Tuple:
    """Find face meshes when face rig has been merged into the main armature."""
    face_bone_names = {b.name for b in faceData.mimicSkeleton} if faceData.mimicSkeleton else set()
    face_meshes = []
    for mesh_obj in bpy.data.objects:
        if mesh_obj.type != 'MESH':
            continue
        has_arm_mod = False
        for mod in mesh_obj.modifiers:
            if mod.type == 'ARMATURE' and getattr(mod, "object", None) == main_obj:
                has_arm_mod = True
                break
        if not has_arm_mod:
            continue
        vg_names = {vg.name for vg in mesh_obj.vertex_groups}
        if face_bone_names & vg_names:
            face_meshes.append(mesh_obj.name)
    return (face_meshes, [main_obj.name])


def _get_skinned_meshes_for_armature(arm_obj) -> list:
    if arm_obj is None or arm_obj.type != 'ARMATURE':
        return []
    mesh_names = []
    for mesh_obj in bpy.data.objects:
        if mesh_obj.type != 'MESH':
            continue
        if mesh_obj.parent == arm_obj:
            mesh_names.append(mesh_obj.name)
            continue
        for mod in mesh_obj.modifiers:
            if mod.type == 'ARMATURE' and getattr(mod, "object", None) == arm_obj:
                mesh_names.append(mesh_obj.name)
                break
    return mesh_names


def _resolve_face_mesh_names(main_obj, face_rig_name, faceData=None) -> list:
    face_meshes = []
    scene = bpy.context.scene
    face_rig = scene.objects.get(face_rig_name) if scene and face_rig_name else None
    if face_rig and face_rig != main_obj:
        (face_meshes, _face_arms) = get_face_meshs(face_rig.name)
    elif face_rig == main_obj and faceData:
        (face_meshes, _face_arms) = _get_face_meshs_merged(main_obj, faceData)
    elif face_rig_name:
        (face_meshes, _face_arms) = get_face_meshs(face_rig_name)

    if not face_meshes:
        face_meshes = _get_skinned_meshes_for_armature(main_obj)

    dedup = []
    seen = set()
    for name in face_meshes:
        if name in seen:
            continue
        seen.add(name)
        dedup.append(name)
    return dedup


def _pose_bone_has_custom_prop(pose_bone, prop_name: str) -> bool:
    if pose_bone is None or not prop_name:
        return False
    try:
        return prop_name in pose_bone
    except Exception:
        return False


def _refresh_driver_expression(driver):
    if driver is None:
        return
    expr = (driver.expression or "").strip()
    if not expr:
        expr = "0.0"
    # Force a real expression change so Blender refreshes driver dependencies.
    driver.expression = f"({expr})+0"
    driver.expression = expr


def _resolve_target_armature(context):
    main_obj, _rig_settings = get_main_armature_and_rig_settings(
        context,
        prefer_active=False,
        remember=True,
        fallback=True,
    )
    if main_obj and main_obj.type == 'ARMATURE':
        return main_obj
    active_obj = getattr(context, "active_object", None)
    if active_obj and active_obj.type == 'ARMATURE':
        return active_obj
    return None


from mathutils import Euler
from math import radians


def _activate_object(obj):
    if obj is None:
        return False
    view_layer = getattr(bpy.context, "view_layer", None)
    if view_layer is None:
        return False
    try:
        obj.select_set(True)
    except Exception:
        pass
    try:
        view_layer.objects.active = obj
    except Exception:
        return False
    return view_layer.objects.active == obj


def _safe_mode_set(mode, obj=None):
    if obj is not None:
        _activate_object(obj)
    view_layer = getattr(bpy.context, "view_layer", None)
    active = view_layer.objects.active if view_layer else None
    if active is None:
        return False
    if getattr(active, "mode", None) == mode:
        return True
    try:
        bpy.ops.object.mode_set(mode=mode)
        return True
    except RuntimeError:
        return False


def reset_transforms(new_obj):
    x, y, z = (radians(0), radians(0), radians(0))
    mat = Euler((x, y, z)).to_matrix().to_4x4()
    new_obj.matrix_world = mat
    new_obj.matrix_local = mat
    new_obj.matrix_basis = mat

    new_obj.location[0] = 0
    new_obj.location[1] = 0
    new_obj.location[2] = 0
    new_obj.scale[0] = 1
    new_obj.scale[1] = 1
    new_obj.scale[2] = 1

def create_control_bone(arm_obj:bpy.types.Object, name = "w3_face_poses"):
    #with edit_object(arm_obj):
    context = bpy.context
    view_layer = context.view_layer
    prev_active = view_layer.objects.active
    prev_selected = [obj for obj in context.selected_objects]
    prev_mode = arm_obj.mode
    try:
        # Ensure the armature is the active object before switching modes.
        for obj in prev_selected:
            try:
                obj.select_set(False)
            except Exception:
                pass
        _activate_object(arm_obj)
        if arm_obj.mode != "OBJECT":
            _safe_mode_set("OBJECT", arm_obj)
        _safe_mode_set("EDIT", arm_obj)

        bl_ctrl_bone = arm_obj.data.edit_bones.get(name)
        if bl_ctrl_bone == None:
            bl_ctrl_bone = arm_obj.data.edit_bones.new(name)
            bl_ctrl_bone.parent = None
            bl_ctrl_bone.use_deform = False
            bl_ctrl_bone.head = Vector([-0.5, 0, 1.5])
            bl_ctrl_bone.tail = Vector([0, 0, 0.2]) + bl_ctrl_bone.head
    finally:
        # Restore previous mode on the armature when possible.
        try:
            if arm_obj.name in bpy.data.objects and arm_obj.mode != prev_mode:
                _safe_mode_set(prev_mode, arm_obj)
        except Exception:
            pass
        # Restore selection and active object.
        try:
            for obj in context.selected_objects:
                obj.select_set(False)
        except Exception:
            pass
        for obj in prev_selected:
            try:
                if obj and obj.name in bpy.data.objects:
                    obj.select_set(True)
            except Exception:
                pass
        try:
            if prev_active and prev_active.name in bpy.data.objects:
                view_layer.objects.active = prev_active
        except Exception:
            pass

class WITCH_OT_morphs(bpy.types.Operator):
    """Load face morph drivers for the currently targeted character armature."""
    bl_idname = "witcher.load_face_morphs"
    bl_label = "Active Debug"

    def execute(self, context):
        main_obj:bpy.types.Object = _resolve_target_armature(context)
        if not main_obj:
            self.report({'WARNING'}, "No character target armature found. Set the Character target armature first.")
            return {'CANCELLED'}
        if 'mimicFaceFile' not in main_obj:
            self.report({'WARNING'}, "Please ensure a Mimic Face Rig is available to use this function.")
            return {'CANCELLED'}
        if getattr(context, "scene", None):
            set_main_armature(context.scene, main_obj)
        control_bone_name = 'w3_face_poses'
        save_world = main_obj.matrix_world.copy()
        save_local = main_obj.matrix_local.copy()
        save_basis = main_obj.matrix_basis.copy()
        save_location = main_obj.location.copy()
        save_scale = main_obj.scale.copy()
        current_pose_position = main_obj.data.pose_position
        try:
            reset_transforms(main_obj)
            main_obj.data.pose_position = "REST"

            create_control_bone(main_obj, control_bone_name)
            _safe_mode_set('OBJECT', main_obj)
            fileName = main_obj['mimicFaceFile']
            faceData = import_rig.loadFaceFile(repo_file(fileName))

            rig_settings = main_obj.data.witcherui_RigSettings
            rig_settings.model_armature_object = main_obj

            import time
            start_time = time.time()
            
            suppress = False

            scene = context.scene

            face_rig = scene.objects.get(main_obj['mimicFace'])
            if not face_rig:
                # Check if face bones were merged into the main armature
                face_bone_names = {b.name for b in faceData.mimicSkeleton} if faceData.mimicSkeleton else set()
                if face_bone_names and any(main_obj.pose.bones.get(bn) for bn in face_bone_names):
                    face_rig = main_obj
                else:
                    self.report({'WARNING'}, f"Could not find face rig '{main_obj['mimicFace']}' in the scene.")
                    return {'CANCELLED'}

            merged_state = (face_rig == main_obj)
            if merged_state:
                (face_meshes, face_arms) = _get_face_meshs_merged(main_obj, faceData)
            else:
                (face_meshes, face_arms) = get_face_meshs(main_obj['mimicFace'])
            face_mesh_objs = []
            for mesh_name in face_meshes:
                mesh_obj = scene.objects.get(mesh_name)
                if mesh_obj:
                    face_mesh_objs.append(mesh_obj)

            bl_ctrl_bone_pose = main_obj.pose.bones[control_bone_name]
            morph_list = rig_settings.witcher_morphs_list
            existing_morph_keys = {(el.name, el.path, el.type) for el in morph_list}

            prev_bake_every_frame = getattr(scene, 'witcher_bake_every_frame', None)
            if prev_bake_every_frame is not None:
                scene.witcher_bake_every_frame = False
            
            #!suppress
            if suppress:
                old = os.dup(sys.stdout.fileno())
                devnull = open(os.devnull, 'w')
                os.dup2(devnull.fileno(), sys.stdout.fileno())

            try:
                for pose in faceData.mimicPoses:
                    # if pose.name != "default":
                    #     continue
                    bl_ctrl_bone_pose[pose.name] = 0.0
                    property_manager = bl_ctrl_bone_pose.id_properties_ui(pose.name)
                    property_manager.update(min = 0., max = 1)
                    witcherui_add_redmorph(morph_list, [pose.name, pose.name, 4], existing_keys=existing_morph_keys)

                    pose.SkeletalAnimationType = "SAT_Additive"
                    set_entry = CSkeletalAnimationSetEntry()
                    set_entry.animation = pose
                    if not _activate_object(face_rig):
                        self.report({'WARNING'}, f"Could not activate face rig '{face_rig.name}'.")
                        return {'CANCELLED'}
                    for pb in face_rig.pose.bones:
                        pb.matrix_basis.identity()
                    #bpy.ops.object.mode_set(mode='POSE', toggle=False)
                    #bpy.ops.pose.transforms_clear()
                    import_anims.import_anim(context, "imported", set_entry, facePose=True, override_select=[face_rig], update_scene_settings=False , at_frame=0)

                    #!GET MESH OBJECTS FOR THIS AND APPLY SHAPE KEYS
                    for the_mesh in face_mesh_objs:
                        create_morph_and_driver(self, main_obj, the_mesh, pose.name)

                    #! RETURN ACTIVE OBJECT
                    _activate_object(face_rig)
                    for pb in face_rig.pose.bones:
                        pb.matrix_basis.identity()
                    if face_rig.animation_data:
                        face_rig.animation_data.action = None
                        if hasattr(face_rig.animation_data, "action_slot"):
                            face_rig.animation_data.action_slot = None
            finally:
                if prev_bake_every_frame is not None:
                    scene.witcher_bake_every_frame = prev_bake_every_frame

            #!stop suppress
            if suppress:
                sys.stdout.flush()
                os.dup2(old, sys.stdout.fileno())
                os.close(old)
            time_taken = time.time() - start_time
            log.info("Loaded morphs in %.2f seconds.", time_taken)

            #! RETURN MAIN OBJECT
            _activate_object(main_obj)

            _safe_mode_set('POSE', main_obj)
            for face_mesh in face_meshes:
                the_mesh = bpy.context.scene.objects.get(face_mesh)
                if not the_mesh:
                    continue
                if the_mesh.data.shape_keys and the_mesh.data.shape_keys.animation_data is not None:
                    for oDrv in the_mesh.data.shape_keys.animation_data.drivers:
                        driver = oDrv.driver
                        _refresh_driver_expression(driver)

            _safe_mode_set('OBJECT', main_obj)

            # Create phoneme setup automatically the first time morphs are loaded.
            has_phoneme_entries = any(el.type == 5 for el in morph_list)
            if not has_phoneme_entries:
                try:
                    _activate_object(main_obj)
                    op_result = bpy.ops.witcher.load_face_phonemes()
                    if 'FINISHED' not in op_result:
                        log.warning("Auto phoneme creation returned %s", op_result)
                except Exception as exc:
                    log.warning("Auto phoneme creation failed: %s", exc)

            #bpy.context.view_layer.objects.active = main_obj
            return {'FINISHED'}
        finally:
            if main_obj and main_obj.name in bpy.data.objects:
                main_obj.matrix_world = save_world
                main_obj.matrix_local = save_local
                main_obj.matrix_basis = save_basis
                main_obj.location = save_location
                main_obj.scale = save_scale
                main_obj.data.pose_position = current_pose_position

    def __del__(self):
        pass
        #bpy.ops.object.modifier_apply_as_shapekey(keep_modifier=True, modifier="Armature")


from . import phoneme_helper
class WITCH_OT_phonemes(bpy.types.Operator):
    bl_idname = "witcher.load_face_phonemes"
    bl_label = "Create phonemes"

    def execute(self, context):
        main_obj:bpy.types.Object = _resolve_target_armature(context)
        if not main_obj:
            self.report({'WARNING'}, "No character target armature found. Set the Character target armature first.")
            return {'CANCELLED'}
        if 'mimicFaceFile' not in main_obj or 'mimicFace' not in main_obj:
            self.report({'WARNING'}, "Please load Face Morphs before creating phonemes.")
            return {'CANCELLED'}
        if getattr(context, "scene", None):
            set_main_armature(context.scene, main_obj)

        try:
            pose_bone = main_obj.pose.bones['w3_face_poses']
        except KeyError:
            self.report({'WARNING'}, "Please load Face Morphs before creating phonemes (missing w3_face_poses).")
            return {'CANCELLED'}

        rig_settings = main_obj.data.witcherui_RigSettings
        rig_settings.model_armature_object = main_obj

        try:
            phonemes_data, morphs_data, phoneme_list, morph_list = phoneme_helper.read_phoneme_weights()
        except Exception as exc:
            self.report({'ERROR'}, f"Failed to read phonemes.txt: {exc}")
            return {'CANCELLED'}

        if not phoneme_list or not morph_list:
            self.report({'ERROR'}, "phonemes.txt did not contain any phoneme or morph data.")
            return {'CANCELLED'}

        scene = context.scene
        faceData = None
        try:
            face_file = main_obj.get('mimicFaceFile')
            if face_file:
                faceData = import_rig.loadFaceFile(repo_file(face_file))
        except Exception as exc:
            log.warning("Failed to read mimic face data for phoneme setup: %s", exc)

        face_rig_name = main_obj['mimicFace']
        face_rig = scene.objects.get(face_rig_name)
        if not face_rig:
            face_bone_names = {b.name for b in faceData.mimicSkeleton} if faceData and faceData.mimicSkeleton else set()
            if face_bone_names and any(main_obj.pose.bones.get(bn) for bn in face_bone_names):
                face_rig = main_obj
            elif face_rig_name == main_obj.name:
                face_rig = main_obj
            else:
                self.report({'WARNING'}, f"Could not find face rig '{face_rig_name}' in the scene.")
                return {'CANCELLED'}

        if face_rig == main_obj and faceData:
            (face_meshes, _face_arms) = _get_face_meshs_merged(main_obj, faceData)
        else:
            (face_meshes, _face_arms) = get_face_meshs(face_rig.name)
        if not face_meshes:
            face_meshes = _get_skinned_meshes_for_armature(main_obj)
        face_mesh_objs = []
        for mesh_name in face_meshes:
            mesh_obj = scene.objects.get(mesh_name)
            if mesh_obj:
                face_mesh_objs.append(mesh_obj)

        if not face_mesh_objs:
            self.report({'WARNING'}, "No face meshes found for the mimic face rig.")
            return {'CANCELLED'}

        morph_list_collection = rig_settings.witcher_morphs_list
        existing_by_key = {(el.name, el.path): el for el in morph_list_collection}

        def ensure_pose_property(prop_name):
            if prop_name not in pose_bone:
                pose_bone[prop_name] = 0.0
            prop_ui = pose_bone.id_properties_ui(prop_name)
            prop_ui.update(min=0.0, max=1.0)

        for morph_name in morph_list:
            ensure_pose_property(morph_name)
            existing = existing_by_key.get((morph_name, morph_name))
            if existing is None:
                added = witcherui_add_redmorph(morph_list_collection, [morph_name, morph_name, 4])
                if added is not None:
                    existing_by_key[(morph_name, morph_name)] = added

        for phoneme in phoneme_list:
            ensure_pose_property(phoneme)
            existing = existing_by_key.get((phoneme, phoneme))
            if existing is None:
                added = witcherui_add_redmorph(morph_list_collection, [phoneme, phoneme, 5])
                if added is not None:
                    existing_by_key[(phoneme, phoneme)] = added
            elif existing.type == 4:
                existing.type = 5

        prev_active = context.view_layer.objects.active
        prev_mode = main_obj.mode
        if prev_mode != 'OBJECT':
            _safe_mode_set('OBJECT', main_obj)

        # Store the toggle as a float custom property on the pose bone so that shape key
        # drivers can read it via a reliable OBJECT → pose-bone variable (same mechanism
        # used by the manual morph variable 'm', which is known to work).
        if "phoneme_enabled" not in pose_bone:
            pose_bone["phoneme_enabled"] = 1.0 if rig_settings.phoneme_enabled else 0.0
            prop_ui = pose_bone.id_properties_ui("phoneme_enabled")
            prop_ui.update(min=0.0, max=1.0)
        else:
            pose_bone["phoneme_enabled"] = 1.0 if rig_settings.phoneme_enabled else 0.0

        try:
            for mesh_obj in face_mesh_objs:
                context.view_layer.objects.active = mesh_obj
                phoneme_helper.ensure_shape_keys(mesh_obj, morph_list)
                phoneme_helper.ensure_shape_keys(mesh_obj, phoneme_list)
                phoneme_helper.setup_phoneme_shape_key_drivers(mesh_obj, main_obj, pose_bone.name, phoneme_list)
                phoneme_helper.setup_morph_shape_key_drivers(
                    mesh_obj,
                    main_obj,
                    pose_bone.name,
                    morphs_data,
                    phoneme_list,
                    toggle_pose_prop="phoneme_enabled",
                )
            for mesh_obj in face_mesh_objs:
                shape_keys = mesh_obj.data.shape_keys
                if not shape_keys or not shape_keys.animation_data:
                    continue
                for fcurve in shape_keys.animation_data.drivers:
                    driver = fcurve.driver
                    if not driver:
                        continue
                    _refresh_driver_expression(driver)
        finally:
            if prev_active and prev_active.name in bpy.data.objects:
                context.view_layer.objects.active = prev_active
            if prev_mode != 'OBJECT' and main_obj.name in bpy.data.objects:
                context.view_layer.objects.active = main_obj
                _safe_mode_set(prev_mode, main_obj)

        # Tag all involved objects dirty so the depsgraph rebuilds dependency edges
        # for the newly created drivers, then force a full evaluation pass so drivers
        # are actually run and the panel reflects correct values immediately.
        for mesh_obj in face_mesh_objs:
            mesh_obj.update_tag()
        main_obj.update_tag()
        context.scene.frame_set(context.scene.frame_current)
        return {'FINISHED'}

class WITCH_OT_clear_lipsync(bpy.types.Operator):
    """Remove all voice/lipsync NLA tracks and reset face morph & phoneme values to zero"""
    bl_idname = "witcher.clear_lipsync"
    bl_label = "Clear Lipsync"
    bl_options = {'UNDO'}

    def execute(self, context):
        main_obj = _resolve_target_armature(context)
        if not main_obj:
            self.report({'WARNING'}, "No character armature found.")
            return {'CANCELLED'}

        pose_bone = main_obj.pose.bones.get("w3_face_poses")
        rig_settings = getattr(main_obj.data, "witcherui_RigSettings", None)

        # Collect track names to remove
        voice_track_names = {"voice_import", "voice_import_phoneme"}

        # Remove armature NLA tracks
        tracks_removed = 0
        actions_removed = []
        if main_obj.animation_data:
            for track in list(main_obj.animation_data.nla_tracks):
                if track.name in voice_track_names:
                    for strip in track.strips:
                        if strip.action and strip.action.name not in actions_removed:
                            actions_removed.append(strip.action.name)
                    main_obj.animation_data.nla_tracks.remove(track)
                    tracks_removed += 1

        # Remove shape key NLA tracks on face meshes
        face_meshes = []
        face_rig_name = main_obj.get('mimicFace')
        if face_rig_name:
            face_meshes = _resolve_face_mesh_names(main_obj, face_rig_name)
        for mesh_name in face_meshes:
            mesh_obj = context.scene.objects.get(mesh_name)
            if not mesh_obj or not mesh_obj.data.shape_keys:
                continue
            sk = mesh_obj.data.shape_keys
            if sk.animation_data:
                for track in list(sk.animation_data.nla_tracks):
                    if track.name in voice_track_names:
                        for strip in track.strips:
                            if strip.action and strip.action.name not in actions_removed:
                                actions_removed.append(strip.action.name)
                        sk.animation_data.nla_tracks.remove(track)
                        tracks_removed += 1

        # Clean up orphaned actions
        for action_name in actions_removed:
            action = bpy.data.actions.get(action_name)
            if action and action.users == 0:
                bpy.data.actions.remove(action)

        # Reset all morph and phoneme pose bone values to zero
        props_reset = 0
        if pose_bone and rig_settings:
            for entry in rig_settings.witcher_morphs_list:
                if entry.path in pose_bone:
                    pose_bone[entry.path] = 0.0
                    props_reset += 1

        # Force depsgraph update so UI reflects zeroed values
        main_obj.update_tag()
        context.scene.frame_set(context.scene.frame_current)

        self.report({'INFO'}, f"Cleared {tracks_removed} track(s), reset {props_reset} morph(s) to zero.")
        return {'FINISHED'}


def _get_loaded_voice_tracks(armature):
    """Return list of (track_name, action_name, frame_range) for voice NLA tracks."""
    voice_track_names = {"voice_import", "voice_import_phoneme"}
    tracks = []
    if not armature or not armature.animation_data:
        return tracks
    for track in armature.animation_data.nla_tracks:
        if track.name in voice_track_names:
            for strip in track.strips:
                action_name = strip.action.name if strip.action else "?"
                tracks.append((track.name, action_name, strip.frame_start, strip.frame_end))
    return tracks


from bpy.utils import (register_class, unregister_class)

_classes = [
    WITCH_PT_WitcherMorphs,
    WITCH_OT_phonemes,
    WITCH_OT_clear_lipsync,
]

_property_group_classes = [
    witcherui_redmorph,
    ListItemBone,
    ListItemAnimset,
    ListItemApp,
    EquipmentSlotEntry,
    TemplateSlotEntry,
    EntitySlotEntry,
    witcherui_RigSettings,
    witcherui_MeshSettings,
]


def register():
    for cls in _property_group_classes:
        register_class(cls)
    bpy.types.Armature.witcherui_RigSettings = bpy.props.PointerProperty(type=witcherui_RigSettings)
    bpy.types.Object.witcherui_MeshSettings = bpy.props.PointerProperty(type=witcherui_MeshSettings)
    for cls in _classes:
        register_class(cls)

def unregister():
    if hasattr(bpy.types.Object, "witcherui_MeshSettings"):
        del bpy.types.Object.witcherui_MeshSettings
    if hasattr(bpy.types.Armature, "witcherui_RigSettings"):
        del bpy.types.Armature.witcherui_RigSettings
    for cls in _classes:
        unregister_class(cls)
    for cls in reversed(_property_group_classes):
        unregister_class(cls)

