import logging
import os
import math
from pathlib import Path
from ..CR2W.common_blender import repo_file
from ..external_addon_tools import get_re_addon_status
log = logging.getLogger(__name__)
from .. import fbx_util, file_helpers
from .. import get_uncook_path
from .. import get_W3_VOICE_PATH
from .. import get_W3_OGG_PATH
from .. import get_rig_rot90_enabled
from .. import get_all_addon_prefs
from ..importers import import_anims, import_rig
from ..exporters import export_anims, export_cutscene
from ..action_compat import iter_action_fcurves, new_action_fcurve, remove_action_fcurve
# from io_import_w2l.importers import import_cutscene
# from io_import_w2l.importers import import_scene
from ..ui.ui_utils import WITCH_PT_Base
from ..ui.ui_anims_list import load_anim_into_scene, resolve_animation_load_context
from ..ui.armature_context import (
    get_main_armature,
    set_main_armature,
)


import bpy


def _find_character_armature(context):
    return get_main_armature(
        context,
        prefer_active=True,
        remember=True,
        fallback=True,
        allow_auxiliary_active=True,
    )


def _format_action_source_label(source):
    labels = {
        "NLA_PLAYING": "NLA (Playing)",
        "NLA_LAST": "NLA (Last Strip)",
        "ACTION_SLOT": "Action Slot",
        "NLA_FALLBACK": "NLA (Fallback)",
        "NLA_LAST_FALLBACK": "NLA (Last Strip, Fallback)",
        "ACTION_FALLBACK": "Action Slot (Fallback)",
        "NONE": "None",
    }
    return labels.get(source, source or "Unknown")


def _short_panel_header_text(text, max_len=28):
    value = str(text or "").strip()
    if not value:
        return ""
    return value if len(value) <= max_len else (value[: max_len - 1] + "…")


def _get_animation_panel_header_status(context):
    arm_obj = _find_character_armature(context)
    if not arm_obj:
        return "No target"

    try:
        scene = getattr(context, "scene", None)
        frame = getattr(scene, "frame_current", 0) if scene else 0
        nla_now, _ = export_anims.get_nla_action_at_frame(arm_obj, frame=frame)
        if nla_now:
            return _short_panel_header_text(getattr(nla_now, "name", "NLA"))
        nla_last, _ = export_anims.get_nla_last_action(arm_obj, prefer_tracks=("anim_import",))
        if nla_last:
            return _short_panel_header_text(getattr(nla_last, "name", "NLA"))
        action_slot = export_anims.get_action_slot(arm_obj)
        if action_slot:
            return _short_panel_header_text(getattr(action_slot, "name", "Action"))
    except Exception:
        pass

    return _short_panel_header_text(getattr(arm_obj, "name", "Animation"))


def _animset_compare_key(path_value):
    """Normalize animset paths for UI matching (handles .w2anims vs .w2anims.json)."""
    raw = str(path_value or "").strip()
    if not raw:
        return ""
    normalized = os.path.normpath(raw.replace("/", os.sep).replace("\\", os.sep))
    if normalized.lower().endswith(".json"):
        normalized = normalized[:-5]
    return os.path.normcase(normalized)


def _animset_repo_compare_key(context, repo_rel_path):
    repo_rel = str(repo_rel_path or "").strip()
    if not repo_rel or ":" in repo_rel:
        return ""
    abs_path = os.path.join(get_uncook_path(context), repo_rel.replace("/", os.sep).replace("\\", os.sep))
    return _animset_compare_key(abs_path)


def _resolve_root_orientation_action(armature_obj):
    if not armature_obj or armature_obj.type != 'ARMATURE' or not armature_obj.animation_data:
        return None

    action = None
    if armature_obj.animation_data.nla_tracks:
        anim_import_track = armature_obj.animation_data.nla_tracks.get('anim_import')
        if anim_import_track and anim_import_track.strips:
            for strip in reversed(anim_import_track.strips):
                if strip.action:
                    action = strip.action
                    break

    if action is None:
        action = armature_obj.animation_data.action

    if action is None and armature_obj.animation_data.nla_tracks:
        for track in armature_obj.animation_data.nla_tracks:
            for strip in track.strips:
                if strip.action:
                    action = strip.action
                    break
            if action:
                break

    return action


def _get_loaded_animset_ui_state(context):
    scene = getattr(context, "scene", None)
    if scene is None:
        return {
            "loaded_path": "",
            "loaded_key": "",
            "source_tag": "",
            "source_badge": "",
            "display_name": "",
            "display_path": "",
            "clip_count": 0,
            "has_loaded_set": False,
        }

    loaded_path = str(getattr(scene, "witcher_loaded_w2anims_path", "") or "").strip()
    loaded_key = _animset_compare_key(loaded_path)
    source_tag = str(getattr(scene, "witcher_loaded_w2anims_source_tag", "") or "").strip().upper()
    loaded_path_no_json = loaded_path[:-5] if loaded_path.lower().endswith(".json") else loaded_path

    display_name = ""
    display_path = ""
    if loaded_path_no_json:
        display_name = os.path.basename(loaded_path_no_json.replace("\\", "/"))
        try:
            uncook_root = os.path.normpath(get_uncook_path(context))
            rel_path = os.path.relpath(os.path.normpath(loaded_path_no_json), uncook_root)
            if not rel_path.startswith(".."):
                display_path = rel_path.replace("\\", "/")
        except Exception:
            pass

    clip_count = len(getattr(scene, "witcher_w2anims_list", []))
    has_loaded_set = bool(loaded_key or clip_count)
    if not display_name and has_loaded_set:
        display_name = "In-memory animation set"

    source_badge = {
        "W2": "W2",
        "W3": "W3",
        "JSON": "JSON",
        "FILE": "FILE",
        "MEMORY": "MEM",
    }.get(source_tag, source_tag or "")

    return {
        "loaded_path": loaded_path,
        "loaded_key": loaded_key,
        "source_tag": source_tag,
        "source_badge": source_badge,
        "display_name": display_name,
        "display_path": display_path,
        "clip_count": clip_count,
        "has_loaded_set": has_loaded_set,
    }


def _get_selected_collection_item(owner, collection_name, index_name):
    collection = getattr(owner, collection_name, None)
    if collection is None:
        return None, -1
    try:
        item_count = len(collection)
    except Exception:
        return None, -1
    if item_count <= 0:
        return None, -1

    current_index = int(getattr(owner, index_name, -1))
    safe_index = max(0, min(current_index, item_count - 1))
    return collection[safe_index], safe_index


def on_anim_list_index_changed(self, context):
    """Callback when animation list selection changes. Auto-loads if enabled."""
    if not getattr(context.scene, 'witcher_load_anim_on_select', False):
        return

    scene = context.scene
    item, _safe_index = _get_selected_collection_item(
        scene,
        "witcher_w2anims_list",
        "witcher_w2anims_list_index",
    )
    if item is None:
        return

    main_arm_obj = _find_character_armature(context)

    if not main_arm_obj:
        return

    anim_name = item.name
    fdir_abs = context.scene.witcher_loaded_w2anims_path

    if not fdir_abs:
        return

    try:
        load_anim_into_scene(context, anim_name, fdir_abs, main_arm_obj)
        # Apply root orientation if enabled
        auto_orient = getattr(context.scene, 'witcher_auto_orient_root', False)
        log.info(f"[on_select] Auto orient root setting: {auto_orient}")
        if auto_orient:
            apply_root_orientation(main_arm_obj)
    except FileNotFoundError as e:
        log.error(f"Auto-load animation failed: {e}")
        def _draw_error(self_op, ctx):
            self_op.layout.label(text=str(e))
        context.window_manager.popup_menu(_draw_error, title="Missing Buffer File", icon='ERROR')
    except Exception as e:
        log.error(f"Auto-load animation failed: {e}")
from bpy.types import Panel, Operator, UIList, PropertyGroup
from bpy.props import IntProperty, StringProperty, CollectionProperty, FloatProperty, BoolProperty, EnumProperty
from bpy_extras.io_utils import (
        ImportHelper,
        ExportHelper
        )


class ListItem(PropertyGroup):
    """Group of properties representing an item in the list."""

    name: StringProperty(
           name="Name",
           description="Name of the animation",
           default="Untitled")
    framesPerSecond: FloatProperty(
           name="Frames Per Second",
           description="",
           default=0)
    numFrames: IntProperty(
           name="Num Frames",
           description="",
           default=0)
    duration: FloatProperty(
           name="Duration",
           description="",
           default=0)
    SkeletalAnimationType: StringProperty(
           name="SkeletalAnimationType",
           description="",
           default="SAT_Normal")
    AdditiveType: StringProperty(
           name="AdditiveType",
           description="",
           default="")
    RootMotion: BoolProperty(
        name="Root Motion",
        default=False,
        options=set(),
        description="",
    )

    # jsonData: StringProperty(
    #        name="Animation in Json",
    #        description="",
    #        default="")

class TOOL_UL_List(UIList):
    """Demo UIList."""
    bl_idname = "TOOL_UL_List"
    layout_type = "DEFAULT" # could be "COMPACT" or "GRID"
    # list_id ToDo

    use_name_reverse: bpy.props.BoolProperty(
        name="Reverse Name",
        default=False,
        options=set(),
        description="Reverse name sort order",
    )

    use_order_name: bpy.props.BoolProperty(
        name="Name",
        default=False,
        options=set(),
        description="Sort groups by their name (case-insensitive)",
    )

    filter_string: bpy.props.StringProperty(
        name="filter_string",
        default = "",
        description="Filter string for name"
    )

    filter_invert: bpy.props.BoolProperty(
        name="Invert",
        default = False,
        options=set(),
        description="Invert Filter"
    )

    def filter_items(self, context,
                    data, # Data from which to take Collection property
                    property # Identifier of property in data, for the collection
        ):


        items = getattr(data, property)
        if not len(items):
            return [], []

        # https://docs.blender.org/api/current/bpy.types.UI_UL_list.html
        # helper functions for handling UIList objects.
        if self.filter_string:
            flt_flags = bpy.types.UI_UL_list.filter_items_by_name(
                    self.filter_string,
                    self.bitflag_filter_item,
                    items,
                    propname="name",
                    reverse=self.filter_invert)
        else:
            flt_flags = [self.bitflag_filter_item] * len(items)

        # https://docs.blender.org/api/current/bpy.types.UI_UL_list.html
        # helper functions for handling UIList objects.
        if self.use_order_name:
            flt_neworder = bpy.types.UI_UL_list.sort_items_by_name(items, "name")
            if self.use_name_reverse:
                flt_neworder.reverse()
        else:
            flt_neworder = []


        return flt_flags, flt_neworder

    def draw_filter(self, context,
                    layout # Layout to draw the item
        ):

        row = layout.row(align=True)
        row.prop(self, "filter_string", text="Filter", icon="VIEWZOOM")
        row.prop(self, "filter_invert", text="", icon="ARROW_LEFTRIGHT")


        row = layout.row(align=True)
        row.label(text="Order by:")
        row.prop(self, "use_order_name", toggle=True)

        icon = 'TRIA_UP' if self.use_name_reverse else 'TRIA_DOWN'
        row.prop(self, "use_name_reverse", text="", icon=icon)

    def draw_item(self, context,
                    layout, # Layout to draw the item
                    data, # Data from which to take Collection property
                    item, # Item of the collection property
                    icon, # Icon of the item in the collection
                    active_data, # Data from which to take property for the active element
                    active_propname, # Identifier of property in active_data, for the active element
                    index, # Index of the item in the collection - default 0
                    flt_flag # The filter-flag result for this item - default 0
            ):

        # Make sure your code supports all 3 layout types
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            layout.label(text=item.name)
        elif self.layout_type in {'GRID'}:
            layout.alignment = 'CENTER'
            layout.label(text="")

class TOOL_OT_List_LoadAnim(Operator):
    """ Add an Item to the UIList"""
    bl_idname = "witcher.list_loadanim"
    bl_label = "Load"
    bl_description = "Load the selected animation"

    action: StringProperty(default="default")
    @classmethod
    def poll(cls, context):
        return context.scene

    def execute(self, context):
        scene = context.scene
        action = self.action
        if "load" == action or "load_cutscene" == action:
            if "load_cutscene" == action:
                list_name = "witcher_w2cutscene_list"
                index_name = "witcher_w2cutscene_list_index"
                working_list_path = context.scene.witcher_loaded_w2cutscene_path
            else:
                list_name = "witcher_w2anims_list"
                index_name = "witcher_w2anims_list_index"
                working_list_path = context.scene.witcher_loaded_w2anims_path
            log.debug("load anim")
            main_arm_obj = _find_character_armature(context)
            item, _safe_index = _get_selected_collection_item(scene, list_name, index_name)
            if item is not None:
                anim_name = item.name 
                fdir_abs = working_list_path  #repo_file(fdir) #!REMOVE !TODO link witcher_loaded_w2anims_path to an object? or keep for cutscene?
                #!REMOVE TODO load anim on click or highlight instead of having to hit the load button

                if not main_arm_obj:
                    self.report({'ERROR'}, "No armature found. Select or import a rig first.")
                    return {'CANCELLED'}
                
                _dirpath, file = os.path.split(fdir_abs)
                _basename, ext = os.path.splitext(file)
                try:
                    if ext.lower() == '.json':
                        _resolved_main_arm_obj, target_armatures, rig_path, _face_animation = resolve_animation_load_context(
                            context,
                            anim_name,
                            fdir=fdir_abs,
                            main_arm_obj=main_arm_obj,
                        )
                        animset = import_anims.import_w3_animSet(fdir_abs, rig_path)
                        #import json by name
                        target_obj = target_armatures if len(target_armatures) > 1 else target_armatures[0]
                        import_anims.import_from_list_item(
                            context,
                            item,
                            animset,
                            target_obj=target_obj,
                        )
                    else:
                        load_anim_into_scene(
                            context,
                            anim_name,
                            fdir_abs,
                            main_arm_obj,
                            face_target_mode="owner" if action == "load_cutscene" else "auto",
                        )
                except FileNotFoundError as e:
                    self.report({'ERROR'}, str(e))
                    return {'CANCELLED'}

                # Apply root orientation if enabled
                auto_orient = getattr(context.scene, 'witcher_auto_orient_root', False)
                log.info(f"Auto orient root setting: {auto_orient}")
                if auto_orient and main_arm_obj:
                    apply_root_orientation(main_arm_obj)

                #import_anims.import_from_list_item(context, item)
            # context.scene.witcher_w2anims_list.add()
            else:
                self.report({'ERROR'}, "No animation selected.")
                return {'CANCELLED'}
        elif "clear" == action:
            log.debug("Debug Clear")
            bpy.context.scene.witcher_w2anims_list.clear()
            bpy.context.scene.witcher_w2anims_list_index = -1
        return {'FINISHED'}

class TOOL_OT_List_Add(Operator):
    """ Add an Item to the UIList"""
    bl_idname = "witcher.list_add"
    bl_label = "Add"
    bl_description = "add a new item to the list."

    @classmethod
    def poll(cls, context):
        """ We can only add items to the list of an active object
            but the list may be empty or doesn't yet exist so
            just this function can only check if there is an active object
        """
        return context.scene

    def execute(self, context):
        context.scene.witcher_w2anims_list.add()
        return {'FINISHED'}

class TOOL_OT_List_Remove(Operator):
    """ Add an Item to the UIList"""
    bl_idname = "witcher.list_remove"
    bl_label = "Add"
    bl_description = "Remove an new item from the list."

    @classmethod
    def poll(cls, context):
        """ We can only remove items from the list of an active object
            that has items in it, but the list may be empty or doesn't
            yet exist and there's no reason to remove an item from an empty
            list.
        """
        return (context.scene
                and context.scene.witcher_w2anims_list
                and len(context.scene.witcher_w2anims_list))

    def execute(self, context):
        alist = context.scene.witcher_w2anims_list
        index = context.scene.witcher_w2anims_list_index
        context.scene.witcher_w2anims_list.remove(index)
        context.scene.witcher_w2anims_list_index = min(max(0, index - 1), len(alist) - 1)
        return {'FINISHED'}

class TOOL_OT_List_Reorder(Operator):
    """ Add an Item to the UIList"""
    bl_idname = "witcher.list_reorder"
    bl_label = "Add"
    bl_description = "add a new item to the list."

    direction: bpy.props.EnumProperty(items=(('UP', 'Up', ""),
                                              ('DOWN', 'Down', ""),))

    @classmethod
    def poll(cls, context):
        """ No reason to try to reorder a list with fewer than
            two items in it.
        """
        return (context.scene
                and context.scene.witcher_w2anims_list
                and len(context.scene.witcher_w2anims_list) > 1)

    def move_index(self):
        """ Move index of an item while clamping it. """
        index = bpy.context.scene.witcher_w2anims_list_index
        list_length = len(bpy.context.scene.witcher_w2anims_list) - 1
        new_index = index + (-1 if self.direction == 'UP' else 1)

        bpy.context.scene.witcher_w2anims_list_index = max(0, min(new_index, list_length))

    def execute(self, context):
        alist = context.scene.witcher_w2anims_list
        index = context.scene.witcher_w2anims_list_index

        neighbor = index + (-1 if self.direction == 'UP' else 1)
        alist.move(neighbor, index)
        self.move_index()
        return {'FINISHED'}

class ButtonOperatorImportW2Anims(bpy.types.Operator, ImportHelper):
    """Import W2 Anims"""
    bl_idname = "witcher.import_w2_anims_json"
    bl_label = "W2 Anims"
    filename_ext = ".w2anims"
    def execute(self, context):
        fdir = self.filepath
        if Path(fdir).is_dir():
            self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
            return {'CANCELLED'}
        import_anims.start_import(context, fdir)
        return {'FINISHED'}
    def invoke(self, context, event):
        UNCOOK_PATH = os.path.join(get_uncook_path(context),"animations\\")
        if os.path.exists(UNCOOK_PATH):
            self.filepath = UNCOOK_PATH if self.filepath == '' else self.filepath
        return ImportHelper.invoke(self, context, event)

import mathutils
class ButtonOperatorToggloRootMotion(bpy.types.Operator):
    """Toggle Root Motion"""
    bl_idname = "witcher.toggle_motion"
    bl_label = "Toggle Root Motion"
    def execute(self, context):
        # Check if there is an active object and if it's an armature
        if context.active_object and context.active_object.type == 'ARMATURE':
            armature = context.active_object.data

            # Store the original mode
            original_mode = context.mode

            # Switch to Edit mode if not already in Edit mode
            if original_mode != 'EDIT':
                bpy.ops.object.mode_set(mode='EDIT')

            # Check if the bone already exists
            if "RootMotion" in armature.edit_bones:
                # Bone exists, remove it
                armature.edit_bones.remove(armature.edit_bones["RootMotion"])
                log.info("Bone 'RootMotion' removed from the armature.")
            else:
                root_bone = armature.edit_bones['Root']
                # Bone doesn't exist, create it as the first bone
                new_bone = armature.edit_bones.new("RootMotion")
                new_bone.head = root_bone.head.copy()
                new_bone.tail = root_bone.tail.copy()
                #rotation_matrix = mathutils.Matrix.Rotation(-90.0, 3, 'Y')
                #new_bone.transform(rotation_matrix)
                root_bone.parent = new_bone
                armature.edit_bones.active = new_bone


                log.info("Bone 'RootMotion' added to the armature.")

            # Update the scene
            context.view_layer.update()

            # Return to the original mode
            bpy.ops.object.mode_set(mode=original_mode)
        else:
            log.warning("No active armature selected.")

        return {'FINISHED'}


class WITCH_OT_ToggleRootMotionDrivers(bpy.types.Operator):
    """Toggle root motion using drivers (no extra bones) - switches between world movement and in-place playback"""
    bl_idname = "witcher.toggle_root_motion_drivers"
    bl_label = "Toggle In-Place (Drivers)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'ARMATURE'

    def execute(self, context):
        from ..importers.motion_tools import (
            setup_root_motion_drivers,
            has_root_motion_drivers,
            set_root_motion_mode,
            get_root_motion_mode
        )

        armature = context.active_object

        # Setup drivers if not present
        if not has_root_motion_drivers(armature):
            if not setup_root_motion_drivers(armature):
                self.report({'ERROR'}, "Could not setup drivers - missing Root or Trajectory bone")
                return {'CANCELLED'}

        # Toggle mode
        current = get_root_motion_mode(armature)
        new_mode = 'IN_PLACE' if current == 'ROOT_MOTION' else 'ROOT_MOTION'
        set_root_motion_mode(armature, new_mode)

        mode_text = 'ON (World Movement)' if new_mode == 'ROOT_MOTION' else 'OFF (In-Place)'
        self.report({'INFO'}, f"Root Motion: {mode_text}")
        return {'FINISHED'}


class WITCH_OT_RemoveRootMotionDrivers(bpy.types.Operator):
    """Remove root motion drivers from Root bone"""
    bl_idname = "witcher.remove_root_motion_drivers"
    bl_label = "Remove Root Motion Drivers"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if not context.active_object or context.active_object.type != 'ARMATURE':
            return False
        from ..importers.motion_tools import has_root_motion_drivers
        return has_root_motion_drivers(context.active_object)

    def execute(self, context):
        from ..importers.motion_tools import remove_root_motion_drivers
        armature = context.active_object
        remove_root_motion_drivers(armature)
        self.report({'INFO'}, "Root motion drivers removed")
        return {'FINISHED'}


# =============================================================================
# CONTROLLER EMPTY APPROACH (Recommended)
# =============================================================================

class WITCH_OT_SetupRootMotionController(bpy.types.Operator):
    """Create a controller empty that follows Trajectory for root motion control"""
    bl_idname = "witcher.setup_root_motion_controller"
    bl_label = "Setup Root Motion Controller"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if not context.active_object or context.active_object.type != 'ARMATURE':
            return False
        from ..importers.motion_tools import has_root_motion_controller
        return not has_root_motion_controller(context.active_object)

    def execute(self, context):
        from ..importers.motion_tools import setup_root_motion_controller
        armature = context.active_object

        controller = setup_root_motion_controller(armature)
        if not controller:
            self.report({'ERROR'}, "Could not create controller - missing Trajectory bone")
            return {'CANCELLED'}

        self.report({'INFO'}, f"Created root motion controller: {controller.name}")
        return {'FINISHED'}


class WITCH_OT_RemoveRootMotionController(bpy.types.Operator):
    """Remove the root motion controller empty and unparent armature"""
    bl_idname = "witcher.remove_root_motion_controller"
    bl_label = "Remove Controller"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if not context.active_object or context.active_object.type != 'ARMATURE':
            return False
        from ..importers.motion_tools import has_root_motion_controller
        return has_root_motion_controller(context.active_object)

    def execute(self, context):
        from ..importers.motion_tools import remove_root_motion_controller
        armature = context.active_object

        if remove_root_motion_controller(armature):
            self.report({'INFO'}, "Root motion controller removed")
            return {'FINISHED'}
        else:
            self.report({'ERROR'}, "Could not remove controller")
            return {'CANCELLED'}


class WITCH_OT_ToggleRootMotionController(bpy.types.Operator):
    """Toggle between root motion (character moves) and in-place (character stays put) modes"""
    bl_idname = "witcher.toggle_root_motion_controller"
    bl_label = "Toggle Root Motion"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if not context.active_object or context.active_object.type != 'ARMATURE':
            return False
        from ..importers.motion_tools import has_root_motion_controller
        return has_root_motion_controller(context.active_object)

    def execute(self, context):
        from ..importers.motion_tools import toggle_controller_mode
        armature = context.active_object

        success, new_mode = toggle_controller_mode(armature)
        if success:
            mode_text = 'In-Place (Trajectory Counteracted)' if new_mode == 'IN_PLACE' else 'Root Motion (Natural Movement)'
            self.report({'INFO'}, f"Mode: {mode_text}")
            return {'FINISHED'}
        else:
            self.report({'ERROR'}, "Could not toggle mode")
            return {'CANCELLED'}


def apply_root_orientation(armature_obj):
    """
    Orient Root bone so the character faces the direction of its natural movement.
    """
    if not armature_obj or armature_obj.type != 'ARMATURE':
        return False

    action = _resolve_root_orientation_action(armature_obj)
    if action is None:
        return False

    pose_bones = armature_obj.pose.bones
    if "Root" not in pose_bones:
        log.warning("Auto Orient Root skipped: no 'Root' bone found in armature")
        return False

    # Check if already applied — don't apply twice
    if action.get("root_orientation_applied", False):
        log.info(f"Root orientation already applied to {action.name}")
        return True

    root_bone = pose_bones["Root"]

    # The swap copies the Trajectory bone's rotation onto Root keeping the world-space movement direction identical to the motion extraction.
    initial_quat = _read_root_first_frame_quat(action, armature_obj)

    # Step 2: Remove ALL fcurves for Root bone (rotation, location, scale)
    root_data_paths = [
        'pose.bones["Root"].rotation_quaternion',
        'pose.bones["Root"].rotation_euler',
        'pose.bones["Root"].location',
        'pose.bones["Root"].scale',
    ]

    fcurves_to_remove = []
    for fc in iter_action_fcurves(action, target=armature_obj):
        if fc.data_path in root_data_paths:
            fcurves_to_remove.append(fc)

    for fc in fcurves_to_remove:
        remove_action_fcurve(action, fc, target=armature_obj)

    log.info(f"Removed {len(fcurves_to_remove)} fcurves from Root bone")

    # ------------------------------------------------------------------
    # Step 3: Key Root with the preserved first-frame rotation (static)
    # ------------------------------------------------------------------
    root_bone.rotation_mode = 'QUATERNION'

    quat_path = 'pose.bones["Root"].rotation_quaternion'
    loc_path = 'pose.bones["Root"].location'

    quat_values = [initial_quat.w, initial_quat.x, initial_quat.y, initial_quat.z]
    for i, val in enumerate(quat_values):
        fc = new_action_fcurve(action, armature_obj, data_path=quat_path, index=i, group_name="Root")
        kp = fc.keyframe_points.insert(1, val)
        kp.interpolation = 'LINEAR'

    for i in range(3):
        fc = new_action_fcurve(action, armature_obj, data_path=loc_path, index=i, group_name="Root")
        kp = fc.keyframe_points.insert(1, 0.0)
        kp.interpolation = 'LINEAR'

    # Mark as applied
    action["root_orientation_applied"] = True

    log.info(f"Applied root orientation to {action.name}")
    log.info(f"  Root initial quaternion: {initial_quat}")

    return True


def _read_root_first_frame_quat(action, armature_obj=None):
    """Read Root bone's rotation at its first keyframe, returned as a Quaternion.

    Reads fcurve values directly (no frame_set overhead) and handles both
    quaternion and euler rotation modes.  Returns identity if no Root rotation
    fcurves exist.
    """
    quat_path = 'pose.bones["Root"].rotation_quaternion'
    euler_path = 'pose.bones["Root"].rotation_euler'

    first_frame = None
    quat_curves = {}   # array_index → fcurve
    euler_curves = {}  # array_index → fcurve
    euler_order = 'XYZ'

    fcurve_iter = (iter_action_fcurves(action, target=armature_obj)
                   if armature_obj is not None else action.fcurves)
    for fc in fcurve_iter:
        if fc.data_path == quat_path and fc.keyframe_points:
            f = fc.keyframe_points[0].co[0]
            if first_frame is None or f < first_frame:
                first_frame = f
            quat_curves[fc.array_index] = fc
        elif fc.data_path == euler_path and fc.keyframe_points:
            f = fc.keyframe_points[0].co[0]
            if first_frame is None or f < first_frame:
                first_frame = f
            euler_curves[fc.array_index] = fc

    if first_frame is None:
        log.info("_read_root_first_frame_quat: no Root rotation fcurves, returning identity")
        return mathutils.Quaternion()

    if quat_curves:
        w = quat_curves[0].evaluate(first_frame) if 0 in quat_curves else 1.0
        x = quat_curves[1].evaluate(first_frame) if 1 in quat_curves else 0.0
        y = quat_curves[2].evaluate(first_frame) if 2 in quat_curves else 0.0
        z = quat_curves[3].evaluate(first_frame) if 3 in quat_curves else 0.0
        return mathutils.Quaternion((w, x, y, z)).normalized()

    # Euler fallback
    ex = euler_curves[0].evaluate(first_frame) if 0 in euler_curves else 0.0
    ey = euler_curves[1].evaluate(first_frame) if 1 in euler_curves else 0.0
    ez = euler_curves[2].evaluate(first_frame) if 2 in euler_curves else 0.0
    return mathutils.Euler((ex, ey, ez), euler_order).to_quaternion()


class WITCH_OT_ApplyRootOrientation(bpy.types.Operator):
    """Apply orientation correction to Root bone animation (Z+ up, X+ towards Y-)"""
    bl_idname = "witcher.apply_root_orientation"
    bl_label = "Orient Root"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if not obj or obj.type != 'ARMATURE' or not obj.animation_data:
            return False
        pose_bones = getattr(getattr(obj, "pose", None), "bones", None)
        if pose_bones is None or "Root" not in pose_bones:
            return False
        # Allow if there's an active action OR NLA tracks with strips
        if obj.animation_data.action:
            return True
        if obj.animation_data.nla_tracks:
            for track in obj.animation_data.nla_tracks:
                if track.strips:
                    return True
        return False

    def execute(self, context):
        armature = context.active_object

        # Check if already applied first
        action = None
        if armature.animation_data:
            action = armature.animation_data.action
            if action is None and armature.animation_data.nla_tracks:
                for track in armature.animation_data.nla_tracks:
                    for strip in track.strips:
                        if strip.action:
                            action = strip.action
                            break
                    if action:
                        break

        if action and action.get("root_orientation_applied", False):
            self.report({'INFO'}, f"Root orientation already applied to '{action.name}'")
            return {'FINISHED'}

        if apply_root_orientation(armature):
            self.report({'INFO'}, "Root orientation applied to animation")
        else:
            self.report({'WARNING'}, "Could not apply root orientation - check console")
        return {'FINISHED'}


class WITCH_OT_ResampleAnimation(bpy.types.Operator):
    """Resample the active animation to a uniform frame rate using Blender's bake.
    This fixes shaking/jittering caused by bones having different sample rates.
    The original keyframes are replaced with baked values at each frame."""
    bl_idname = "witcher.resample_animation"
    bl_label = "Resample Animation"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (obj and obj.type == 'ARMATURE' and 
                obj.animation_data and obj.animation_data.action)
    
    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)
    
    def execute(self, context):
        obj = context.active_object
        action = obj.animation_data.action
        
        # Get frame range
        frame_start = int(action.frame_range[0])
        frame_end = int(action.frame_range[1])
        
        # Store original action name
        original_name = action.name
        
        # Use Blender's built-in bake operator
        try:
            bpy.ops.nla.bake(
                frame_start=frame_start,
                frame_end=frame_end,
                step=1,
                only_selected=False,
                visual_keying=False,
                clear_constraints=False,
                clear_parents=False,
                use_current_action=True,
                clean_curves=False,
                bake_types={'POSE'}
            )
            self.report({'INFO'}, f"Resampled animation '{original_name}' to frames {frame_start}-{frame_end}")
        except Exception as e:
            self.report({'ERROR'}, f"Bake failed: {str(e)}")
            return {'CANCELLED'}
        
        return {'FINISHED'}


class WITCHER_PT_animset_panel(WITCH_PT_Base, Panel):
    # Promoted to top-level: no longer hidden inside Character Appearances.
    bl_idname = "WITCHER_PT_animset_panel"
    bl_label = "Animation"
    bl_description = "Animation sets, clips, speech, and playback controls"
    bl_options = set()  # Open by default — prominent, not collapsed

    def draw_header(self, context):
        self.layout.label(text="", icon='ACTION')

    def draw_header_preset(self, context):
        text = _get_animation_panel_header_status(context)
        ui_scale = context.preferences.system.ui_scale
        max_chars = max(8, int((context.region.width - 135 * ui_scale) / (7 * ui_scale)))
        if len(text) > max_chars:
            text = text[:max_chars - 3] + "..."
        self.layout.label(text=text)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = False  # Must be False so prop_enum() shows text ON buttons, not beside them
        layout.use_property_decorate = False
        scene = context.scene
        if scene is None:
            return

        def section(section_id, label, icon, default_closed=False):
            container = layout.box()
            header, body = container.panel(section_id, default_closed=default_closed)
            header.label(text=label, icon=icon)
            return body

        display_armature = _find_character_armature(context)
        rig_settings = None
        if display_armature and display_armature.type == 'ARMATURE' and hasattr(display_armature.data, "witcherui_RigSettings"):
            rig_settings = display_armature.data.witcherui_RigSettings

        # --- Active context banner (always visible at the top of this panel) ---
        ctx_box = layout.box()
        ctx_row = ctx_box.row(align=True)
        if display_armature:
            ctx_row.label(text=display_armature.name, icon='ARMATURE_DATA')
            frame = scene.frame_current
            nla_now, _ = export_anims.get_nla_action_at_frame(display_armature, frame=frame)
            if nla_now:
                ctx_row.label(text=nla_now.name, icon='ACTION')
            else:
                ctx_row.label(text="No action @ frame", icon='ACTION')
        else:
            ctx_row.alert = True
            ctx_row.label(text="No character selected", icon='INFO')

        # --- Section navigator: 3 big highlighted buttons ---
        anim_tab = getattr(scene, "witcher_anim_tab", "CLIPS")
        nav_row = layout.row(align=True)
        nav_row.scale_y = 1.6
        nav_row.prop_enum(scene, "witcher_anim_tab", 'SETS')
        nav_row.prop_enum(scene, "witcher_anim_tab", 'CLIPS')
        nav_row.prop_enum(scene, "witcher_anim_tab", 'SPEECH')
        layout.separator(factor=0.3)

        # ===================== SETS TAB =====================
        if anim_tab == "SETS":
            loaded_set = _get_loaded_animset_ui_state(context)

            # Current animset status + explicit import entry point (TW2/TW3 .w2anims)
            status_box = layout.box()
            head_row = status_box.row(align=True)
            head_row.label(text="Current Loaded Set", icon='CHECKMARK' if loaded_set["has_loaded_set"] else 'INFO')
            if loaded_set["has_loaded_set"] and loaded_set["source_badge"]:
                badge_row = head_row.row(align=True)
                badge_row.enabled = False
                badge_row.label(text=f"[{loaded_set['source_badge']}]")
            action_row = status_box.row(align=True)
            action_row.operator(ButtonOperatorImportW2Anims.bl_idname, text="Import Set (.w2anims)", icon='IMPORT')
            if loaded_set["has_loaded_set"]:
                action_row.prop_enum(scene, "witcher_anim_tab", 'CLIPS', text="Clips")

            if loaded_set["has_loaded_set"]:
                title_row = status_box.row(align=True)
                title_row.label(text=loaded_set["display_name"] or "Loaded animation set", icon='ACTION')
                title_row.label(text=f"{loaded_set['clip_count']} clips", icon='ANIM_DATA')
                status_box.label(text="Load clip entries in the Clips tab (next to Sets).", icon='INFO')
                if loaded_set["display_path"]:
                    path_row = status_box.row()
                    path_row.scale_y = 0.75
                    path_row.label(text=loaded_set["display_path"], icon='FILE')
            else:
                status_box.label(text="No .w2anims set loaded yet.", icon='INFO')
                status_box.label(text="Load a character-linked set below, or import a .w2anims file.", icon='IMPORT')

            if not rig_settings:
                layout.label(text="Select/import a character armature to browse character-linked sets.", icon='INFO')
            else:
                # Entity info header
                info_box = layout.box()
                info_box.label(text=f"{display_armature.name}", icon='ARMATURE_DATA')
                skeleton = getattr(rig_settings, 'main_entity_skeleton', '') or '-'
                info_row = info_box.row()
                info_row.scale_y = 0.75
                info_row.label(text=f"Skeleton: {skeleton}", icon='BONE_DATA')

                if not rig_settings.animset_list:
                    layout.label(text="No animation sets found. Import a character entity to populate.", icon='INFO')
                else:
                    search_box = layout.box()
                    search_row = search_box.row(align=True)
                    search_row.prop(scene, "witcher_animset_filter_text", text="", icon='VIEWZOOM')
                    filter_text = str(getattr(scene, "witcher_animset_filter_text", "") or "").strip().lower()

                    groups = []
                    current_group_name = "Sets"
                    current_group_items = []
                    for item in rig_settings.animset_list:
                        item_path = str(getattr(item, "path", "") or "")
                        if ":" in item_path:
                            if current_group_items:
                                groups.append((current_group_name, current_group_items))
                            current_group_name = item_path.rstrip(":") or "Sets"
                            current_group_items = []
                            continue
                        current_group_items.append(item)
                    if current_group_items:
                        groups.append((current_group_name, current_group_items))

                    total_set_count = 0
                    visible_set_count = 0
                    matched_group_count = 0

                    for group_name, group_items in groups:
                        total_set_count += len(group_items)

                        visible_items = []
                        for item in group_items:
                            item_path = str(getattr(item, "path", "") or "")
                            filename = item_path.replace("\\", "/").split("/")[-1]
                            haystack = f"{filename} {item_path} {group_name}".lower()
                            if filter_text and filter_text not in haystack:
                                continue
                            visible_items.append(item)

                        if not visible_items:
                            continue

                        visible_set_count += len(visible_items)
                        matched_group_count += 1

                        current_box = layout.box()
                        hdr = current_box.row()
                        hdr.enabled = False
                        hdr.label(text=group_name, icon='OUTLINER_OB_ARMATURE')

                        for item in visible_items:
                            item_path = str(getattr(item, "path", "") or "")
                            filename = item_path.replace("\\", "/").split("/")[-1]
                            is_loaded_set = bool(loaded_set["loaded_key"]) and (
                                _animset_repo_compare_key(context, item_path) == loaded_set["loaded_key"]
                            )
                            button_icon = 'CHECKMARK' if is_loaded_set else 'ACTION'
                            button_text = filename if not is_loaded_set else f"{filename}  [Loaded]"
                            file_row = current_box.row(align=True)
                            op = file_row.operator(
                                "witcher.list_loadapp",
                                text=button_text,
                                icon=button_icon,
                                depress=is_loaded_set,
                            )
                            op.action = "w2anims"
                            op.path = item_path
                            reveal_op = file_row.operator("witcher.reveal_anim_in_explorer", text="", icon='FILE_FOLDER')
                            reveal_op.path = item_path
                            info_op = file_row.operator("witcher.animset_path_info", text="", icon='QUESTION')
                            info_op.path = item_path

                    stats_row = search_box.row(align=True)
                    if filter_text:
                        stats_row.label(text=f"{visible_set_count}/{total_set_count} sets in {matched_group_count} groups", icon='FILTER')
                    else:
                        stats_row.label(text=f"{total_set_count} sets in {len(groups)} groups", icon='INFO')

                    if total_set_count > 0 and visible_set_count == 0:
                        no_match = layout.box()
                        no_match.label(text=f"No sets match '{filter_text}'", icon='INFO')
                        no_match.label(text="Try part of file name, path, or group (e.g. sword).")

        body = section("witcher_anim_imported_sets", "Imported Animation Clips", 'ACTION') if anim_tab == "CLIPS" else None
        if body:
            col_main = body.column(align=True)
            loaded_set = _get_loaded_animset_ui_state(context)

            if loaded_set["has_loaded_set"]:
                set_info = col_main.box()
                hdr = set_info.row(align=True)
                hdr.label(text="Current Set", icon='CHECKMARK')
                if loaded_set["source_badge"]:
                    badge = hdr.row(align=True)
                    badge.enabled = False
                    badge.label(text=f"[{loaded_set['source_badge']}]")
                hdr.label(text=f"{loaded_set['clip_count']} clips", icon='ANIM_DATA')
                set_info.label(text=loaded_set["display_name"] or "Loaded animation set", icon='ACTION')
                if loaded_set["display_path"]:
                    path_row = set_info.row()
                    path_row.scale_y = 0.75
                    path_row.label(text=loaded_set["display_path"], icon='FILE')
            else:
                hint = col_main.box()
                hint.label(text="No animation set loaded yet.", icon='INFO')
                hint.label(text="Import a .w2anims set to populate clip entries.", icon='IMPORT')

            col_main.operator(ButtonOperatorImportW2Anims.bl_idname, text="Import Set (.w2anims)", icon='IMPORT')

            box = col_main.box()
            list_row = box.row()
            col = list_row.column(align=True)
            col.template_list(
                "TOOL_UL_List",
                "The_List",
                scene,
                "witcher_w2anims_list",
                scene,
                "witcher_w2anims_list_index",
            )
            reorder_col = list_row.column()
            if len(scene.witcher_w2anims_list) > 1:
                reorder_col.operator("witcher.list_reorder", text="", icon="TRIA_UP").direction = "UP"
                reorder_col.operator("witcher.list_reorder", text="", icon="TRIA_DOWN").direction = "DOWN"

            row = box.row(align=True)
            row.operator("witcher.list_loadanim", text="Load Clip", icon='PLAY').action = "load"
            row.prop(scene, "witcher_load_anim_on_select", text="Load on Select")

            opts = box.box()
            opts.label(text="Import / Decode Options", icon='SETTINGS')
            opts.prop(scene, "witcher_prefer_uncompressed_anims", text="Prefer Uncompressed Data")
            opts.prop(scene, "witcher_bake_every_frame", text="Bake Every Frame")
            opts.prop(scene, "witcher_smooth_missing_frames", text="Smooth Missing Frames")
            opts.prop(scene, "witcher_scale_keys_to_duration", text="Scale Keys to Duration")

            motion_box = box.box()
            motion_box.label(text="Motion Extraction Debug", icon='ACTION')
            motion_box.label(text="Import motion extraction debug object when present.")
            motion_box.prop(scene, "witcher_motion_extraction_debug_compressed", text="Compressed")
            motion_box.prop(scene, "witcher_motion_extraction_debug_uncompressed", text="Uncompressed")

            orient_row = box.row(align=True)
            orient_row.prop(scene, "witcher_auto_orient_root", text="Auto Orient Root")
            orient_row.operator(WITCH_OT_ApplyRootOrientation.bl_idname, text="", icon='ORIENTATION_GLOBAL')

            item, _safe_index = _get_selected_collection_item(
                scene,
                "witcher_w2anims_list",
                "witcher_w2anims_list_index",
            )
            if item is not None:
                info = col_main.box()
                info.label(text="Selected Clip", icon='INFO')
                info.label(text=f"Name: {item.name}")
                info.label(text=f"Frames: {item.numFrames}")
                info.label(text=f"FPS: {round(item.framesPerSecond, 2)}")
                info.label(text=f"Length: {round(item.duration, 2)} sec")
                info.label(text=f"Type: {item.SkeletalAnimationType}")
                if len(item.AdditiveType):
                    info.label(text=f"Additive: {item.AdditiveType}")
                info.label(text=f"Root Motion: {item.RootMotion}")

        body = section("witcher_anim_quick_browser", "Quick Animation Browser", 'PRESET') if anim_tab == "CLIPS" else None
        if body:
            from . import ui_anims_list as _ui_anims_list
            if hasattr(_ui_anims_list, "ensure_quick_anim_list_current"):
                _ui_anims_list.ensure_quick_anim_list_current(context)
            col = body.column(align=True)
            search_row = col.row(align=True)
            if hasattr(scene, "witcher_quick_anim_search"):
                search_row.prop(scene, "witcher_quick_anim_search", text="", icon='VIEWZOOM')
                clear_btn = search_row.row(align=True)
                clear_btn.enabled = bool(getattr(scene, "witcher_quick_anim_search", ""))
                clear_op = clear_btn.operator("witcher.myanimlist_debug", text="", icon='X')
                clear_op.action = "clear_search"
            if hasattr(scene, "witcher_quick_anim_load_on_select"):
                search_row.prop(scene, "witcher_quick_anim_load_on_select", text="Load on Select")

            if hasattr(scene, "witcher_auto_orient_root"):
                col.prop(scene, "witcher_auto_orient_root", text="Auto Orient Root")
            if hasattr(scene, "witcher_quick_anim_auto_collapse_categories"):
                col.prop(scene, "witcher_quick_anim_auto_collapse_categories", text="Auto Collapse Categories")
            if hasattr(scene, "witcher_quick_anim_show_all"):
                col.prop(scene, "witcher_quick_anim_show_all", text="Show All Animations")

            bulk_row = col.row(align=True)
            bulk_row.operator("witcher.quick_anim_category_bulk", text="Expand All").action = "expand_all"
            bulk_row.operator("witcher.quick_anim_category_bulk", text="Collapse All").action = "collapse_all"

            list_box = col.box()
            list_box.template_list(
                listtype_name='MYANIMLISTITEM_UL_basic',
                list_id='W3_UI_ANIMATION_LIST',
                dataptr=scene,
                propname='witcher_quick_anim_list',
                active_dataptr=scene,
                active_propname='witcher_quick_anim_list_index',
                rows=7,
            )
            list_box.label(text=f"{len(getattr(scene, 'witcher_quick_anim_list', []))} visible entries", icon='INFO')
            act = list_box.row(align=True)
            act.operator("witcher.myanimlist_debug", text="Rebuild", icon='FILE_REFRESH').action = "reset3"
            act.operator("witcher.myanimlist_debug", text="Load", icon='PLAY').action = "load"


        if anim_tab == "SPEECH":
            try:
                from . import ui_voice as _ui_voice
            except Exception:
                _ui_voice = None
            try:
                from . import ui_mimics as _ui_mimics_dialog
            except Exception:
                _ui_mimics_dialog = None

        # --- Quick Mimic Import (own section, above Dialogue Browser) ---
        mimic_body = section("witcher_anim_quick_mimic", "Quick Mimic Import", 'SHAPEKEY_DATA') if anim_tab == "SPEECH" else None
        if mimic_body:
            mimic_col = mimic_body.column(align=True)
            mimic_props_ready_dialog = bool(
                _ui_mimics_dialog
                and hasattr(scene, _ui_mimics_dialog.MIMIC_LIST_PROP)
                and hasattr(scene, _ui_mimics_dialog.MIMIC_LIST_INDEX_PROP)
                and hasattr(scene, _ui_mimics_dialog.MIMIC_AUTO_LOAD_PROP)
            )
            if mimic_props_ready_dialog:
                if hasattr(_ui_mimics_dialog, "ensure_mimic_list_initialized"):
                    _ui_mimics_dialog.ensure_mimic_list_initialized(context)

                search_row = mimic_col.row(align=True)
                if hasattr(scene, _ui_mimics_dialog.MIMIC_SEARCH_PROP):
                    search_row.prop(scene, _ui_mimics_dialog.MIMIC_SEARCH_PROP, text="", icon='VIEWZOOM')
                    clear_btn = search_row.row(align=True)
                    clear_btn.enabled = bool(getattr(scene, _ui_mimics_dialog.MIMIC_SEARCH_PROP, ""))
                    clear_btn.operator("witcher.quick_mimic_debug", text="", icon='X').action = "clear_search"
                search_row.prop(scene, _ui_mimics_dialog.MIMIC_AUTO_LOAD_PROP, text="Load on Select")

                if hasattr(scene, _ui_mimics_dialog.MIMIC_AUTO_COLLAPSE_PROP):
                    mimic_col.prop(scene, _ui_mimics_dialog.MIMIC_AUTO_COLLAPSE_PROP, text="Auto Collapse Categories")

                bulk_row = mimic_col.row(align=True)
                bulk_row.operator("witcher.quick_mimic_category_bulk", text="Expand All").action = "expand_all"
                bulk_row.operator("witcher.quick_mimic_category_bulk", text="Collapse All").action = "collapse_all"

                mimic_list_box = mimic_col.box()
                mimic_list_box.template_list(
                    "MYMIMICLISTITEM_UL_basic",
                    "W3_UI_MIMIC_LIST_DIALOG",
                    scene,
                    _ui_mimics_dialog.MIMIC_LIST_PROP,
                    scene,
                    _ui_mimics_dialog.MIMIC_LIST_INDEX_PROP,
                    sort_lock=True,
                    rows=7,
                )
                mimic_list_box.label(
                    text=f"{len(getattr(scene, _ui_mimics_dialog.MIMIC_LIST_PROP, []))} visible entries",
                    icon='INFO',
                )
                mimic_actions = mimic_list_box.row(align=True)
                mimic_actions.operator("witcher.quick_mimic_debug", text="Rebuild", icon='FILE_REFRESH').action = "reset3"
                mimic_actions.operator("witcher.quick_mimic_debug", text="Load", icon='PLAY').action = "load"
            else:
                mimic_col.label(text="Mimic properties not registered.", icon='INFO')

        body = section("witcher_anim_dialogue_browser", "Dialogue Browser", 'TEXT', default_closed=False) if anim_tab == "SPEECH" else None
        if body:
            col = body.column(align=True)
            if _ui_voice and hasattr(_ui_voice, "ensure_voice_list_initialized"):
                _ui_voice.ensure_voice_list_initialized(context)

            # --- Options row (compact) ---
            option_row = col.row(align=True)
            if hasattr(scene, "witcher_voice_show_details"):
                option_row.prop(scene, "witcher_voice_show_details", text="IDs/dur")
            if hasattr(scene, "witcher_voice_replace_audio"):
                option_row.prop(scene, "witcher_voice_replace_audio", text="Replace")
            if hasattr(scene, "witcher_voice_recreate_phonemes"):
                option_row.prop(scene, "witcher_voice_recreate_phonemes", text="Phonemes")
            if getattr(scene, "witcher_voice_recreate_phonemes", False):
                if hasattr(scene, "witcher_voice_phoneme_accuracy"):
                    col.prop(scene, "witcher_voice_phoneme_accuracy", text="Accuracy", slider=True)

            # --- Loaded lipsync status ---
            _arm = _find_character_armature(context)
            if _arm and _arm.animation_data:
                _voice_tracks = []
                for _trk in _arm.animation_data.nla_tracks:
                    if _trk.name in ("voice_import", "voice_import_phoneme"):
                        for _strip in _trk.strips:
                            _aname = _strip.action.name if _strip.action else "?"
                            _label = "Phonemes" if "phoneme" in _trk.name else "Morphs"
                            _voice_tracks.append((_label, _aname, _strip.frame_start, _strip.frame_end))
                if _voice_tracks:
                    status_box = col.box()
                    for _label, _aname, _fs, _fe in _voice_tracks:
                        status_box.label(text=f"{_label}: {_aname}  [{int(_fs)}-{int(_fe)}]", icon='NLA')
                    status_box.operator("witcher.clear_lipsync", text="Clear Lipsync", icon='TRASH')

            # --- Popular / pinned speaker quick-filters ---
            popular_speakers = []
            if _ui_voice and hasattr(_ui_voice, "_voice_popular_speakers_cache"):
                popular_speakers = list(getattr(_ui_voice, "_voice_popular_speakers_cache", []))
            if popular_speakers:
                col.label(text="Popular speakers", icon='COMMUNITY')
                popular_grid = col.grid_flow(columns=4, align=True)
                for speaker in popular_speakers:
                    op = popular_grid.operator(
                        "witcher.quick_voice_filter_speaker",
                        text=f"[{speaker}]",
                        icon='FILTER',
                    )
                    op.speaker = speaker
                    if _ui_voice and hasattr(_ui_voice, "_get_speaker_count"):
                        try:
                            op.count = int(_ui_voice._get_speaker_count(speaker))
                        except Exception:
                            pass

            if hasattr(scene, "witcher_voice_pinned_speakers") and scene.witcher_voice_pinned_speakers:
                col.label(text="Pinned", icon='BOOKMARKS')
                pinned_grid = col.grid_flow(columns=4, align=True)
                for pin in scene.witcher_voice_pinned_speakers:
                    pin_name = getattr(pin, "name", "")
                    if not pin_name:
                        continue
                    op = pinned_grid.operator(
                        "witcher.quick_voice_filter_speaker",
                        text=f"[{pin_name}]",
                        icon='FILTER',
                    )
                    op.speaker = pin_name
                    if _ui_voice and hasattr(_ui_voice, "_get_speaker_count"):
                        try:
                            op.count = int(_ui_voice._get_speaker_count(pin_name))
                        except Exception:
                            pass

            # --- Dialogue list ---
            if hasattr(scene, "witcher_voice_list") and hasattr(scene, "witcher_voice_list_index"):
                # --- Selected-line speaker filter (lives with the filter controls) ---
                if 0 <= scene.witcher_voice_list_index < len(scene.witcher_voice_list):
                    selected = scene.witcher_voice_list[scene.witcher_voice_list_index]
                    speaker = getattr(selected, "speaker", "")
                    if speaker:
                        effective_speaker = ""
                        speaker_is_pinned = False
                        if _ui_voice and hasattr(_ui_voice, "_get_effective_speaker"):
                            try:
                                effective_speaker = _ui_voice._get_effective_speaker(scene)
                            except Exception:
                                effective_speaker = ""
                        if _ui_voice and hasattr(_ui_voice, "_is_pinned"):
                            try:
                                speaker_is_pinned = bool(_ui_voice._is_pinned(scene, speaker))
                            except Exception:
                                speaker_is_pinned = False
                        chip_row = col.row(align=True)
                        chip_row.operator("witcher.quick_voice_filter_speaker", text=f"Only [{speaker}]", icon='FILTER').speaker = speaker
                        chip_row.operator("witcher.quick_voice_pin_speaker", text="", icon='BOOKMARKS').speaker = speaker
                        if speaker_is_pinned and hasattr(bpy.ops.witcher, "quick_voice_unpin_speaker"):
                            chip_row.operator("witcher.quick_voice_unpin_speaker", text="", icon='X').speaker = speaker
                        if effective_speaker:
                            chip_row.operator("witcher.quick_voice_clear_speaker", text="Clear", icon='PANEL_CLOSE')

                # --- Pager ---
                if _ui_voice and hasattr(_ui_voice, "get_voice_browser_stats"):
                    stats = _ui_voice.get_voice_browser_stats(scene)
                    if all(hasattr(scene, p) for p in ("witcher_voice_page_size", "witcher_voice_page_index")):
                        pager = col.row(align=True)
                        pager.prop(scene, "witcher_voice_page_size", text="Rows")
                        pager.operator("witcher.quick_voice_page", text="<<").action = "first"
                        pager.operator("witcher.quick_voice_page", text="<").action = "prev"
                        pager.label(text=f"{stats['page_index'] + 1}/{stats['total_pages']}")
                        pager.operator("witcher.quick_voice_page", text=">").action = "next"
                        pager.operator("witcher.quick_voice_page", text=">>").action = "last"
                    col.label(
                        text=(
                            f"Showing {stats['visible_start']}-{stats['visible_end']} "
                            f"of {stats['filtered']} filtered  ({stats['total']} total)"
                        ),
                        icon='INFO',
                    )
                else:
                    total_nodes = _ui_voice.get_voice_node_count() if _ui_voice else 0
                    col.label(text=f"Showing {len(scene.witcher_voice_list)} of {total_nodes} lines", icon='INFO')

                # --- The actual list ---
                col.template_list(
                    "MYVOICELISTITEM_UL_basic",
                    "",
                    scene,
                    "witcher_voice_list",
                    scene,
                    "witcher_voice_list_index",
                    sort_lock=True,
                    rows=7,
                )

                # --- Actions (first thing under the list) ---
                act_row = col.row(align=True)
                if hasattr(scene, "witcher_voice_load_on_select"):
                    act_row.prop(scene, "witcher_voice_load_on_select", text="Load on Select")
                act_row.operator("witcher.quick_voice_debug", text="Rebuild", icon='FILE_REFRESH').action = "reset3"
                act_row.operator("witcher.quick_voice_debug", text="Load", icon='PLAY').action = "load"

                # --- Search bar ---
                if hasattr(scene, "witcher_voice_search_text"):
                    search_row = col.row(align=True)
                    search_row.prop(scene, "witcher_voice_search_text", text="", icon='VIEWZOOM')
                    search_row.operator("witcher.quick_voice_clear_filter", text="", icon='X')

                    # Search status / syntax hint
                    raw_search = getattr(scene, "witcher_voice_search_text", "").strip()
                    eff_speaker = ""
                    if _ui_voice and hasattr(_ui_voice, "_get_effective_speaker"):
                        try:
                            eff_speaker = _ui_voice._get_effective_speaker(scene)
                        except Exception:
                            pass
                    if raw_search or eff_speaker:
                        hint_parts = []
                        if eff_speaker:
                            hint_parts.append(f"speaker={eff_speaker}")
                        if raw_search and _ui_voice and hasattr(_ui_voice, "_parse_search_tokens"):
                            try:
                                _toks, _sp = _ui_voice._parse_search_tokens(raw_search)
                                id_toks   = [t for t in _toks if t['type'] == 'id']
                                text_toks = [t for t in _toks if t['type'] != 'id']
                                if id_toks:
                                    hint_parts.append(f"id:{id_toks[0]['terms'][0]}")
                                if text_toks:
                                    parts = []
                                    c = {tt: sum(1 for t in text_toks if t['type'] == tt) for tt in ('and','phrase','not','or')}
                                    if c['and']:    parts.append(f"{c['and']} word(s)")
                                    if c['phrase']: parts.append(f"{c['phrase']} phrase(s)")
                                    if c['not']:    parts.append(f"{c['not']} excluded")
                                    if c['or']:     parts.append(f"{c['or']} OR-group(s)")
                                    if parts:
                                        hint_parts.append("text: " + ", ".join(parts))
                            except Exception:
                                pass
                        if hint_parts:
                            col.label(text="Filtering: " + " | ".join(hint_parts), icon='VIEWZOOM')
                    else:
                        col.label(
                            text='Tip: words  "phrase"  -exclude  id:NNN  @SPEAKER  w1|w2',
                            icon='INFO',
                        )

                # --- Utility row (visually separated) ---
                col.separator(factor=0.5)
                util_row = col.row(align=True)
                util_row.scale_y = 0.85
                util_row.operator("witcher.quick_voice_copy", text="Copy Selected", icon='COPYDOWN').scope = "selected"
                util_row.operator("witcher.quick_voice_copy", text="Copy All", icon='COPYDOWN').scope = "all"
            else:
                col.label(text="Dialogue browser properties are not registered yet.", icon='INFO')

            # --- Speech Cache Tools (collapsed sub-panel) ---
            cache_header, cache_body = col.panel("witcher_dialogue_cache_tools", default_closed=True)
            cache_header.label(text="Cache Tools", icon='FILE_FOLDER')
            if cache_body:
                # Paths are configured in Addon Preferences; just expose an open button here.
                cache_body.operator(
                    "witcher.open_voice_audio_path",
                    text="Open Audio Folder",
                    icon='FILE_FOLDER',
                )
                if all(hasattr(scene, p) for p in (
                    "witcher_speech_pair_total",
                    "witcher_speech_pair_extracted",
                    "witcher_speech_pair_cr2w",
                    "witcher_speech_pair_wem",
                )):
                    counts = cache_body.box()
                    counts.label(text=f"Bundle pairs: {scene.witcher_speech_pair_total}")
                    counts.label(text=f"Extracted pairs: {scene.witcher_speech_pair_extracted}")
                    counts.label(text=f".cr2w files: {scene.witcher_speech_pair_cr2w}")
                    counts.label(text=f".wem files: {scene.witcher_speech_pair_wem}")
                    if getattr(scene, "witcher_speech_pair_last_refresh", ""):
                        counts.label(text=f"Last refresh: {scene.witcher_speech_pair_last_refresh}")
                cache_body.operator(
                    "witcher.refresh_speech_counts",
                    text="Refresh Counts",
                    icon='FILE_REFRESH',
                )

        body = section("witcher_anim_playback", "Playback / Root Motion", 'CON_LOCLIKE', default_closed=False) if anim_tab == "CLIPS" else None
        if body:
            col_main = body.column(align=True)
            root_motion_box = col_main.box()
            root_motion_box.label(text="Root Motion", icon='CON_LOCLIKE')

            active_armature = display_armature if (display_armature and display_armature.type == 'ARMATURE') else context.active_object
            if active_armature and active_armature.type == 'ARMATURE':
                from ..importers.motion_tools import (
                    has_root_motion_controller, get_controller_mode,
                    has_root_motion_drivers, get_root_motion_mode
                )

                has_controller = has_root_motion_controller(active_armature)
                if has_controller:
                    current_mode = get_controller_mode(active_armature)
                    icon = 'PAUSE' if current_mode == 'IN_PLACE' else 'PLAY'
                    text_label = "In-Place (Locked)" if current_mode == 'IN_PLACE' else "Root Motion (Moving)"
                    row = root_motion_box.row(align=True)
                    row.operator(WITCH_OT_ToggleRootMotionController.bl_idname, text=text_label, icon=icon)
                    row.operator(WITCH_OT_RemoveRootMotionController.bl_idname, text="", icon='X')
                    root_motion_box.label(
                        text=("Trajectory counteracted" if current_mode == 'IN_PLACE' else "Natural animation"),
                        icon='INFO',
                    )
                else:
                    root_motion_box.operator(
                        WITCH_OT_SetupRootMotionController.bl_idname,
                        text="Setup Controller (Recommended)",
                        icon='EMPTY_ARROWS',
                    )

                alt_box = root_motion_box.box()
                alt_box.label(text="Alternatives", icon='DOWNARROW_HLT')
                has_drivers = has_root_motion_drivers(active_armature)
                if has_drivers:
                    driver_mode = get_root_motion_mode(active_armature)
                    row = alt_box.row(align=True)
                    row.operator(
                        WITCH_OT_ToggleRootMotionDrivers.bl_idname,
                        text=f"Drivers: {'ON' if driver_mode == 'ROOT_MOTION' else 'OFF'}",
                        icon='DRIVER',
                    )
                    row.operator(WITCH_OT_RemoveRootMotionDrivers.bl_idname, text="", icon='X')
                else:
                    alt_box.operator(WITCH_OT_ToggleRootMotionDrivers.bl_idname, text="Setup Drivers (Root bone)", icon='DRIVER')
                alt_box.operator(ButtonOperatorToggloRootMotion.bl_idname, text="Toggle RootMotion Bone", icon='BONE_DATA')
            else:
                root_motion_box.label(text="Select a character armature", icon='INFO')

            col_main.operator(WITCH_OT_ResampleAnimation.bl_idname, text="Resample Animation", icon='TIME')

            current_box = col_main.box()
            current_box.label(text="Current Animation", icon='ACTION')
            if display_armature:
                frame = scene.frame_current
                nla_action, nla_info = export_anims.get_nla_action_at_frame(display_armature, frame=frame)
                if nla_action:
                    track_name = (nla_info or {}).get("track", "")
                    strip_name = (nla_info or {}).get("strip", "")
                    extra = f" [{track_name}/{strip_name}]" if track_name or strip_name else ""
                    current_box.label(text=f"NLA (Playing @ {frame}): {nla_action.name}{extra}")
                else:
                    nla_last_action, nla_last_info = export_anims.get_nla_last_action(display_armature, prefer_tracks=("anim_import",))
                    if nla_last_action:
                        track_name = (nla_last_info or {}).get("track", "")
                        strip_name = (nla_last_info or {}).get("strip", "")
                        extra = f" [{track_name}/{strip_name}]" if track_name or strip_name else ""
                        current_box.label(text=f"NLA (Last Strip): {nla_last_action.name}{extra}")
                    else:
                        current_box.label(text="NLA: None")

                action_slot = export_anims.get_action_slot(display_armature)
                current_box.label(text=f"Action Slot: {action_slot.name}" if action_slot else "Action Slot: None")

                if hasattr(scene, "witcher_w3_anim_source"):
                    current_box.prop(scene, "witcher_w3_anim_source", text="Source", expand=True)

                resolved_action, resolved_info = export_anims.resolve_action(
                    display_armature,
                    context=context,
                    source_mode=getattr(scene, "witcher_w3_anim_source", "NLA"),
                )
                if resolved_action:
                    source_label = _format_action_source_label((resolved_info or {}).get("source"))
                    if source_label:
                        current_box.label(text=f"Using: {source_label}")
                    current_box.label(text=f"Current: {resolved_action.name}")
                else:
                    current_box.label(text="Current: None")
            else:
                current_box.label(text="No armature found.", icon='INFO')

            if rig_settings and getattr(rig_settings, "witcher_tracks_list", None):
                tracks_box = col_main.box()
                row = tracks_box.row(align=False)
                row.prop(
                    rig_settings,
                    "witcher_tracks_collapse",
                    icon="TRIA_DOWN" if not rig_settings.witcher_tracks_collapse else "TRIA_RIGHT",
                    icon_only=True,
                    emboss=False,
                )
                track_items = [x for x in rig_settings.witcher_tracks_list if x.type == 0]
                row.label(text=f"Tracks ({len(track_items)})", icon='ANIM')
                if not rig_settings.witcher_tracks_collapse:
                    for track in track_items:
                        if 'hctFOV' in track.name:
                            camera_bone = display_armature.pose.bones.get("Camera_Node") if display_armature and display_armature.pose else None
                            if camera_bone and hasattr(camera_bone, '["' + track.path + '"]'):
                                tracks_box.prop(camera_bone, '["' + track.path + '"]', text=track.name)

            action_info_box = col_main.box()
            action_info_box.label(text="Action Import Info", icon='INFO')
            action = None
            if display_armature:
                action, _ = export_anims.resolve_action(
                    display_armature,
                    context=context,
                    source_mode=getattr(scene, "witcher_w3_anim_source", "NLA"),
                )
            if action:
                source_file = action.get("w3_anim_source_file", "")
                buffer_source = action.get("w3_anim_buffer_source", "")
                buffer_detail = action.get("w3_anim_buffer_detail", "")
                if source_file:
                    action_info_box.label(text="File: " + os.path.basename(source_file))
                if buffer_source:
                    detail_text = f" ({buffer_detail})" if buffer_detail else ""
                    action_info_box.label(text="Buffer: " + buffer_source + detail_text)
                if not source_file and not buffer_source:
                    action_info_box.label(text="No import metadata found.")
            else:
                action_info_box.label(text="No action found.")

class WITCH_OT_import_w3_fbx(Operator, ImportHelper):
    """Same as normal FBX import but applies materials. Need seprate "FBX Import plugin for blender" enabled. Download from Nexus"""
    bl_idname = "witcher.import_witcher3_fbx"
    bl_label = "Import Witcher 3 FBX"
    bl_options = {'REGISTER', 'UNDO'}

    # Properties provided or used by ImportHelper mixin class.
    filename_ext = ".fbx"
    filter_glob: StringProperty(
        default="*.fbx",
        options={'HIDDEN'}
    )
    files: CollectionProperty(
        name="File Path",
        description="File path used for importing",
        type=bpy.types.OperatorFileListElement
    )
    directory: StringProperty()

    # Other properties
    recursive: BoolProperty(
        name = "Recursive",
        default = False,
        description = "Recursive import. Be careful, and have a console open"
    )
    keep_lod_meshes: BoolProperty(
        name="Keep LODs",
        default=False,
        description="If enabled, it will keep low quality meshes and materials"
    )
    remove_doubles: BoolProperty(
        name="Remove Doubles",
        default=True,
        description="Disable this if you get incorrectly merged verts."
    )
    quadrangulate: BoolProperty(
        name="Tris to Quads",
        default=True,
        description="Runs the Tris to Quads operator on imported meshes with UV seams enabled. Therefore it shouldn't break anything"
    )
    combined_armatures: BoolProperty(
        name="Combine Armatures",
        default=True,
        description="Merge all armatures into one"
    )
    force_update_mats: BoolProperty(
        name="Overwrite Materials",
        default=False,
        description="Re-create materials even if they were already imported before. Their old versions will be overwritten"
    )

    def execute(self, context):
        # if not bpy.data.is_saved:
        # 	self.report({'ERROR'}, 'Please save your file first. Textures will be written in a "textures" folder next to the .blend file.')
        # 	return {'CANCELLED'}

        filepath = self.filepath	# Provided by ImportHelper.

        uncook_path = get_uncook_path(context)
        recursive = self.recursive
        keep_lod_meshes = self.keep_lod_meshes
        remove_doubles = self.remove_doubles
        quadrangulate = self.quadrangulate
        combined_armatures = self.combined_armatures
        if recursive:
            combined_armatures = False

        paths = [os.path.join(self.directory, name.name)
            for name in self.files]

        if not uncook_path or not os.path.isdir(uncook_path):
            raise Exception("Please set a valid Uncook Path in Edit -> Preferences -> Add-ons -> Witcher 3 Tools.")

        #bpy.ops.outliner.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)

        fbx_util.importFbx(filepath
                            ,"name"
                            ,"name"
                            ,uncook_path = uncook_path
                            ,keep_lod_meshes = keep_lod_meshes
                        )

        return {'FINISHED'}

class WITCH_OT_ImportW2Rig(bpy.types.Operator, ImportHelper):
    """Load Witcher 3 .w2rig file or .w2rig.json"""
    bl_idname = "witcher.import_w2_rig"
    bl_label = "Import .w2rig"
    filename_ext = ".w2rig, .w2rig.json; w3dyny"
    bl_options = {'REGISTER', 'UNDO'}

    filter_glob: StringProperty(default='*.w2rig;*.w2rig.json;*.w3dyng;*.w3dyng.json', options={'HIDDEN'})

    def execute(self, context):
        log.debug("importing rig")
        fdir = self.filepath
        if os.path.isdir(fdir):
            self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
            return {'CANCELLED'}
        ext = file_helpers.getFilenameType(fdir)
        if ext == ".w2rig" or ext == ".json" or ext == ".w3dyng":
            rig_name = os.path.splitext(os.path.basename(fdir))[0]
            # Strip double extensions like .w2rig.json
            if rig_name.endswith('.w2rig') or rig_name.endswith('.w3dyng'):
                rig_name = os.path.splitext(rig_name)[0]
            armature_obj = import_rig.start_rig_import(fdir, rig_name, None, context=context)
            set_main_armature(context.scene, armature_obj)
        elif ext ==".w3fac":
            faceData = import_rig.loadFaceFile(fdir)
            armature_obj = import_rig.create_armature(faceData.mimicSkeleton, "yes", context=context)
            set_main_armature(context.scene, armature_obj)
        return {'FINISHED'}
    def invoke(self, context, event):
        UNCOOK_PATH = os.path.join(get_uncook_path(context),"characters\\base_entities\\")
        if os.path.exists(UNCOOK_PATH):
            self.filepath = UNCOOK_PATH if self.filepath == '' else self.filepath
        return ImportHelper.invoke(self, context, event)

class WITCH_OT_ExportW2RigJson(bpy.types.Operator, ExportHelper):
    """export W2 rig Json"""
    bl_idname = "witcher.export_w2_rig"
    bl_label = "Export"
    filename_ext = ".json"
    filename = ".w2rig"
    def execute(self, context):
        obj = context.object
        fdir = self.filepath
        ext = file_helpers.getFilenameType(fdir)
        import_rig.export_w3_rig(context, fdir)
        return {'FINISHED'}


from ..importers.motion_tools import generate_motion_extraction, MotionExtraction

def _normalize_w2anims_export_path(path, use_native_writer):
    p = Path(path)
    if use_native_writer:
        if p.suffix.lower() == ".json":
            p = p.with_suffix("")
        if p.suffix.lower() != ".w2anims":
            p = p.with_suffix(".w2anims")
        return str(p)

    if p.suffix.lower() == ".json":
        return str(p)
    if p.suffix.lower() == ".w2anims":
        return str(p) + ".json"
    return str(p.with_suffix(".json"))

class WITCH_OT_ExportW2AnimJson(bpy.types.Operator, ExportHelper):
    """export W2 Anim Json"""
    bl_idname = "witcher.export_w2_anim"
    bl_label = "Export"
    filename_ext = ".w2anims"

    @classmethod
    def poll(cls, context):
        return export_anims.get_selected_armature(context) is not None

    use_json_legacy: BoolProperty(
        name="Use JSON (Legacy)",
        description="Export as .w2anims.json for WolvenKit processing instead of writing .w2anims directly",
        default=False
    )

    skeletal_anim_type: EnumProperty(
        name="Animation Type",
        description="Skeletal animation type",
        items=[
            ('SAT_Normal', "Normal", "Standard skeletal animation"),
            ('SAT_Additive', "Additive", "Additive skeletal animation"),
            ('SAT_MS', "MS", "Motion-sampled animation"),
        ],
        default='SAT_Normal',
    )

    additive_type: EnumProperty(
        name="Additive Type",
        description="Additive animation type (only used when Animation Type is Additive)",
        items=[
            ('NONE', "None", "No additive type"),
            ('AT_Local', "Local", "Local additive"),
            ('AT_Ref', "Ref", "Reference additive"),
            ('AT_TPose', "T-Pose", "T-Pose additive"),
            ('AT_Animation', "Animation", "Animation additive"),
        ],
        default='NONE',
    )

    include_motion_extraction: BoolProperty(
        name="Include Motion Extraction",
        description="Generate motion extraction from Trajectory bone",
        default=False,
    )

    def draw(self, context):
        layout = self.layout
        source_mode = getattr(context.scene, "witcher_w3_anim_source", "NLA")
        armature = export_anims.get_selected_armature(context)
        action, info = export_anims.resolve_action(armature, context=context, source_mode=source_mode)
        if action:
            layout.label(text=f"Exporting Action: {action.name}", icon='ACTION')
        else:
            layout.label(text="Exporting Action: None", icon='INFO')
        source_label = _format_action_source_label((info or {}).get("source"))
        if source_label:
            layout.label(text=f"Source: {source_label}")
        layout.separator()
        layout.prop(self, "skeletal_anim_type")
        if self.skeletal_anim_type == 'SAT_Additive':
            layout.prop(self, "additive_type")
        layout.prop(self, "include_motion_extraction")
        layout.separator()
        layout.prop(self, "use_json_legacy")

    def execute(self, context):
        obj = context.object
        use_native = not self.use_json_legacy
        fdir = _normalize_w2anims_export_path(self.filepath, use_native)
        ext = file_helpers.getFilenameType(fdir)

        additive = self.additive_type if self.additive_type != 'NONE' else None
        export_anims.export_w3_anim(
            context, fdir,
            use_native_writer=use_native,
            skeletal_type=self.skeletal_anim_type,
            additive_type=additive,
            include_motion_extraction=self.include_motion_extraction,
        )
        return {'FINISHED'}

    def invoke(self, context, event):
        source_mode = getattr(context.scene, "witcher_w3_anim_source", "NLA")
        armature = export_anims.get_selected_armature(context)
        action, _ = export_anims.resolve_action(armature, context=context, source_mode=source_mode)
        if action:
            current_path = Path(self.filepath) if self.filepath else None
            if current_path and str(current_path.parent) not in (".", ""):
                self.filepath = str(current_path.parent / f"{action.name}{self.filename_ext}")
            else:
                self.filepath = f"{action.name}{self.filename_ext}"
        return ExportHelper.invoke(self, context, event)


class WITCH_OT_ExportW2Cutscene(bpy.types.Operator, ExportHelper):
    """Export W2 Cutscene (native .w2cutscene)"""
    bl_idname = "witcher.export_w2_cutscene"
    bl_label = "Export Cutscene"
    filename_ext = ".w2cutscene"
    filter_glob: StringProperty(default="*.w2cutscene", options={'HIDDEN'})

    export_redkit_re_files: BoolProperty(
        name="Export Redkit .re Files",
        description="Export each cutscene entry as a Redkit-friendly .re file next to the .w2cutscene",
        default=False,
    )

    export_redkit_csv: BoolProperty(
        name="Export Redkit CSV",
        description="Write an animation;component CSV manifest next to the .w2cutscene. This also exports Redkit .re files.",
        default=False,
    )

    @classmethod
    def poll(cls, context):
        scene = getattr(context, "scene", None)
        if scene is None:
            return False
        return any(
            getattr(obj, "type", None) == 'ARMATURE'
            and str(obj.get("cutscene_actor_name", "") or "").strip()
            for obj in scene.objects
        )

    def draw(self, context):
        layout = self.layout
        re_status = get_re_addon_status()

        layout.prop(self, "export_redkit_re_files")
        layout.prop(self, "export_redkit_csv")
        if self.export_redkit_csv and not self.export_redkit_re_files:
            layout.label(text="CSV export also writes Redkit .re files.", icon='INFO')
        if self.export_redkit_re_files or self.export_redkit_csv:
            icon = 'CHECKMARK' if re_status["enabled"] else 'ERROR'
            status = "enabled" if re_status["enabled"] else "not enabled"
            layout.label(text=f"RE addon: {status}", icon=icon)
            if not re_status["enabled"]:
                warning_row = layout.row()
                warning_row.alert = True
                warning_row.label(text="Enable blender_re_animations_plugin before exporting Redkit .re files.", icon='ERROR')
            layout.label(text="Files write to <cutscene>_redkit/<actor>/*.re", icon='FILE_FOLDER')

    def execute(self, context):
        if self.export_redkit_re_files or self.export_redkit_csv:
            re_status = get_re_addon_status()
            if not re_status["enabled"]:
                self.report({'ERROR'}, "Enable blender_re_animations_plugin to export Redkit .re files")
                return {'CANCELLED'}

        return export_cutscene.export_w3_cutscene(
            context,
            self.filepath,
            export_redkit_re_files=self.export_redkit_re_files,
            export_redkit_csv=self.export_redkit_csv,
        )


#-----------------------------------------------------------------------------
#
classes = [
    ButtonOperatorImportW2Anims,
    ButtonOperatorToggloRootMotion,
    WITCH_OT_ToggleRootMotionDrivers,
    WITCH_OT_RemoveRootMotionDrivers,
    WITCH_OT_SetupRootMotionController,
    WITCH_OT_RemoveRootMotionController,
    WITCH_OT_ToggleRootMotionController,
    WITCH_OT_ApplyRootOrientation,
    WITCH_OT_ResampleAnimation,
    ListItem,
    TOOL_UL_List,
    TOOL_OT_List_Add,
    TOOL_OT_List_Remove,
    TOOL_OT_List_Reorder,
    WITCHER_PT_animset_panel,
    TOOL_OT_List_LoadAnim,
    WITCH_OT_ImportW2Rig,
    WITCH_OT_ExportW2RigJson,
    WITCH_OT_ExportW2AnimJson,
    WITCH_OT_ExportW2Cutscene,
]



def register():
    #bpy.types.TOPBAR_MT_file_import.append(menu_func_import)
    for c in classes:
        bpy.utils.register_class(c)

    # bpy.types.Scene.anim_export_name = StringProperty(
    #        name="Anim Export Name",
    #        description="Name of the animation",
    #        default="My_New_Anim")
    bpy.types.Scene.witcher_anim_tab = EnumProperty(
        name="Animation Tab",
        description="Active sub-section of the Animation panel",
        items=[
            ('SETS',   "Sets",   "Character-linked animation sets (idle, locomotion, facial)"),
            ('CLIPS',  "Clips",  "Import and browse individual animation clips"),
            ('SPEECH', "Speech", "Voiceline import and dialogue browser"),
        ],
        default='CLIPS',
    )
    bpy.types.Scene.witcher_loaded_w2anims_path = StringProperty(default='')
    bpy.types.Scene.witcher_loaded_w2anims_source_tag = StringProperty(
        name="Loaded Animation Set Source",
        description="Compact source tag for the currently loaded .w2anims set (W2/W3/JSON/etc.)",
        default='',
        options={'SKIP_SAVE'},
    )
    bpy.types.Scene.witcher_animset_filter_text = StringProperty(
        name="Set Filter",
        description="Filter character-linked animation sets by filename, path, or category",
        default='',
    )
    bpy.types.Scene.witcher_w2anims_list = CollectionProperty(type = ListItem)
    bpy.types.Scene.witcher_w2anims_list_index = IntProperty(name = "Index for witcher_w2anims_list",
                                             default = 0,
                                             update = on_anim_list_index_changed)

    bpy.types.Scene.witcher_load_anim_on_select = BoolProperty(
        name="Load on Select",
        description="Automatically load animation when selecting it in the list",
        default=False
    )

    bpy.types.Scene.witcher_w3_anim_source = EnumProperty(
        name="Animation Source",
        description="Choose which animation source is treated as current",
        items=[
            ('NLA', "NLA", "Use NLA strip at current frame (or last strip)"),
            ('ACTION', "Action Slot", "Use the legacy action slot"),
        ],
        default='NLA'
    )

    bpy.types.Scene.witcher_auto_orient_root = BoolProperty(
        name="Auto Orient Root",
        description="Automatically orient Root bone after import (Z+ up, X+ towards Y-). Experimental.",
        default=True
    )

    bpy.types.Scene.witcher_motion_extraction_debug_compressed = BoolProperty(
        name="Import Compressed Motion Extraction",
        description="If enabled, imports compressed motion extraction as a debug object when present",
        default=False
    )

    bpy.types.Scene.witcher_motion_extraction_debug_uncompressed = BoolProperty(
        name="Import Uncompressed Motion Extraction",
        description="If enabled, attempts to import uncompressed motion extraction as a debug object when present",
        default=False
    )
    
    bpy.types.Scene.witcher_loaded_w2cutscene_path = StringProperty(default='')
    bpy.types.Scene.witcher_w2cutscene_list = CollectionProperty(type = ListItem)
    bpy.types.Scene.witcher_w2cutscene_list_index = IntProperty(name = "Index for witcher_w2cutscene_list",
                                             default = 0)

    bpy.types.Scene.witcher_loaded_w2scene_path = StringProperty(default='')
    bpy.types.Scene.witcher_w2scene_list = CollectionProperty(type = ListItem)
    bpy.types.Scene.witcher_w2scene_list_index = IntProperty(name = "Index for witcher_w2scene_list",
                                             default = 0)
    
    bpy.types.Scene.witcher_prefer_uncompressed_anims = BoolProperty(
        name="Prefer Uncompressed Animation Data",
        description="For uncooked .w2anims files, use embedded uncompressed keyframe data instead of compressed buffers (experimental)",
        default=False
    )
    bpy.types.Scene.witcher_bake_every_frame = BoolProperty(
        name="Bake Every Frame",
        description="Insert keyframes on every frame after resampling (more accurate, less smooth)",
        default=True
    )
    bpy.types.Scene.witcher_smooth_missing_frames = BoolProperty(
        name="Smooth Missing Frames",
        description="Apply light smoothing to resampled missing frames (may reduce pops, less accurate)",
        default=False
    )
    bpy.types.Scene.witcher_scale_keys_to_duration = BoolProperty(
        name="Scale Keys to Duration",
        description="When not baking every frame, scale key times to fit the animation duration using animation dt",
        default=False
    )


def unregister():
    for prop_name in (
        "witcher_anim_tab",
        "witcher_w2anims_list",
        "witcher_w2anims_list_index",
        "witcher_loaded_w2anims_path",
        "witcher_loaded_w2anims_source_tag",
        "witcher_animset_filter_text",
        "witcher_load_anim_on_select",
        "witcher_w3_anim_source",
        "witcher_auto_orient_root",
        "witcher_motion_extraction_debug_compressed",
        "witcher_motion_extraction_debug_uncompressed",
        "witcher_w2cutscene_list",
        "witcher_w2cutscene_list_index",
        "witcher_loaded_w2cutscene_path",
        "witcher_w2scene_list",
        "witcher_w2scene_list_index",
        "witcher_loaded_w2scene_path",
        "witcher_prefer_uncompressed_anims",
        "witcher_bake_every_frame",
        "witcher_smooth_missing_frames",
        "witcher_scale_keys_to_duration",
    ):
        if hasattr(bpy.types.Scene, prop_name):
            delattr(bpy.types.Scene, prop_name)
    #bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    #del bpy.types.Scene.anim_export_name
    for c in reversed(classes):
        bpy.utils.unregister_class(c)

if __name__ == '__main__':
    register()
