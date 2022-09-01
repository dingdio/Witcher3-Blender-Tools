import os
from io_import_w2l.setup_logging_bl import *
log = logging.getLogger(__name__)

from io_import_w2l.importers import import_anims
import bpy
from bpy.types import Panel, Operator, UIList, PropertyGroup
from bpy.props import IntProperty, StringProperty, CollectionProperty, FloatProperty
from bpy_extras.io_utils import (
        ImportHelper
        )
#-----------------------------------------------------------------------------
#
# An extremely simple class that is used as the list item in the UIList.
# It is possible to use a builtin type instead but this allows customization.
# The content is fairly arbitrary, execpt that the member should be
# bpy.props (ToDo: Verify this.)
# Since it contains bpy.props, it must be registered.
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

    # jsonData: StringProperty(
    #        name="Animation in Json",
    #        description="",
    #        default="")
    
#-----------------------------------------------------------------------------
#
# https://docs.blender.org/api/current/bpy.types.UIList.html#bpy.types.UIList
# The actual UIList class
# This class has a filter function that can be used to sort the properties
# into ascending or descending order using their name property.
class TOOL_UL_List(UIList):
    """Demo UIList."""
    bl_idname = "TOOL_UL_List"
    layout_type = "DEFAULT" # could be "COMPACT" or "GRID"
    # list_id ToDo

    # Custom properties, used in the filter functions
    # This property applies only if use_order_name is True.
    # In that case it determines whether to reverse the order of the sort.
    use_name_reverse: bpy.props.BoolProperty(
        name="Reverse Name",
        default=False,
        options=set(),
        description="Reverse name sort order",
    )

    # This properties tells whether to sort the list according to
    # the alphabetical order of the names.
    use_order_name: bpy.props.BoolProperty(
        name="Name",
        default=False,
        options=set(),
        description="Sort groups by their name (case-insensitive)",
    )

    # This property is the value for a simple name filter.
    filter_string: bpy.props.StringProperty(
        name="filter_string",
        default = "",
        description="Filter string for name"
    )

    # This property tells whether to invert the simple name filter
    filter_invert: bpy.props.BoolProperty(
        name="Invert",
        default = False,
        options=set(),
        description="Invert Filter"
    )

    #-------------------------------------------------------------------------
    # This function does two things, and as a result returns two arrays:
    # flt_flags - this is the filtering array returned by the filter
    #             part of the function. It has one element per item in the
    #             list and is set or cleared based on whether the item
    #             should be displayed.
    # flt_neworder - this is the sorting array returned by the sorting
    #             part of the function. It has one element per item
    #             the item is the new position in order for the
    #             item.
    # The arrays must be the same length as the list of items or empty
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

#-----------------------------------------------------------------------------
#
# An extremely simple list add operator
# Replace context.scene.demo_list with the actual list
class TOOL_OT_List_LoadAnim(Operator):
    """ Add an Item to the UIList"""
    bl_idname = "tool.list_loadanim"
    bl_label = "Load"
    bl_description = "load a new item to the list."
    
    action: StringProperty(default="default")
    @classmethod
    def poll(cls, context):
        return context.scene
    
    def execute(self, context):
        scene = context.scene
        action = self.action
        if "load" == action:
            print("=== load anim ====")
            if scene.list_index >= 0 and scene.demo_list:
                item = scene.demo_list[scene.list_index]
                
                import_anims.import_from_list_item(context, item)
            # context.scene.demo_list.add()
        elif "clear" == action:
            print("=== Debug Clear ====")
            bpy.context.scene.demo_list.clear()
        return {'FINISHED'}

#-----------------------------------------------------------------------------
#
# An extremely simple list add operator
# Replace context.scene.demo_list with the actual list
class TOOL_OT_List_Add(Operator):
    """ Add an Item to the UIList"""
    bl_idname = "tool.list_add"
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
        context.scene.demo_list.add()
        return {'FINISHED'}

#-----------------------------------------------------------------------------
#
# An extremely simple list remove operator
# Replace context.scene.demo_list with the actual list
# It's only possible to remove the item that is indexed
# The reorder routine keeps track of the index.
class TOOL_OT_List_Remove(Operator):
    """ Add an Item to the UIList"""
    bl_idname = "tool.list_remove"
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
                and context.scene.demo_list
                and len(context.scene.demo_list))
    
    def execute(self, context):
        alist = context.scene.demo_list
        index = context.scene.list_index
        context.scene.demo_list.remove(index)
        context.scene.list_index = min(max(0, index - 1), len(alist) - 1)
        return {'FINISHED'}

#-----------------------------------------------------------------------------
#
# An extremely simple list reordering operator
# Replace context.object.demo_list with the actual list
class TOOL_OT_List_Reorder(Operator):
    """ Add an Item to the UIList"""
    bl_idname = "tool.list_reorder"
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
                and context.scene.demo_list
                and len(context.scene.demo_list) > 1)

    def move_index(self):
        """ Move index of an item while clamping it. """
        index = bpy.context.scene.list_index
        list_length = len(bpy.context.scene.demo_list) - 1
        new_index = index + (-1 if self.direction == 'UP' else 1)

        bpy.context.scene.list_index = max(0, min(new_index, list_length))

    def execute(self, context):
        alist = context.scene.demo_list
        index = context.scene.list_index

        neighbor = index + (-1 if self.direction == 'UP' else 1)
        alist.move(neighbor, index)
        self.move_index()
        return {'FINISHED'}

#-----------------------------------------------------------------------------
#

class ButtonOperatorImportW2Anims(bpy.types.Operator, ImportHelper):
    """Import W2 Anims"""
    bl_idname = "object.import_w2_anims_json"
    bl_label = "W2 Anims"
    filename_ext = ".w2anims"
    def execute(self, context):
        fdir = self.filepath
        if (os.path.exists(fdir+'.json')):
            fdir = fdir + '.json'
        if fdir.endswith('.cr2w'):
            log.info('Importing Lipsync')
            import_anims.import_lipsync(context, fdir)
        else:
            import_anims.start_import(context, fdir)
        return {'FINISHED'}


from io_import_w2l import get_W3_VOICE_PATH

class WITCH_PT_Base:
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Witcher'
    bl_context = ''#'objectmode'
    #bl_options = {'DEFAULT_CLOSED'}
class TOOL_PT_Panel(WITCH_PT_Base, Panel):
    bl_idname = "TOOL_PT_Panel"
    bl_label = "Animation Set"
    bl_description = "Demonstration of UIList Features"
    #bl_options = {'HEADER_LAYOUT_EXPAND'}
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        """ 
        """
        object = context.scene
        if object == None:
            return

        row = self.layout.row()
        op = row.operator(ButtonOperatorImportW2Anims.bl_idname, text="Import Voiceline", icon='SPHERE')
        op.filepath = get_W3_VOICE_PATH(bpy.context) #r"\w3.modding\radish-tools\docs.speech\enpc.w3speech-extracted_GOOD\enpc.w3speech-extracted"
        
        row = self.layout.row()
        op = row.operator(ButtonOperatorImportW2Anims.bl_idname, text="Import .w2anims.json", icon='SPHERE')
        op.filepath = os.path.join(get_uncook_path(context),"animations\\")
        
        row = self.layout.row()
        row.alignment = "CENTER"
        

        col = row.column(align=True)
        col.template_list("TOOL_UL_List", "The_List", object,
                            "demo_list", object, "list_index")
        
        col = row.column(align=True)
        col.operator("tool.list_add", text="", icon="ADD")
        col.operator("tool.list_remove", text="", icon="REMOVE")

        if len(object.demo_list) > 1:
            col.operator("tool.list_reorder", text="",
                icon="TRIA_UP").direction = "UP"
            col.operator("tool.list_reorder", text="",
                icon="TRIA_DOWN").direction = "DOWN"

        grid = self.layout.grid_flow( columns = 2 )
        
        grid.operator("tool.list_loadanim", text="Load").action = "load"
        grid.operator("tool.list_loadanim", text="Clear List").action = "clear"
        row = self.layout.row()
        if object.list_index >= 0 and object.demo_list:
            item = object.demo_list[object.list_index]

            #row = self.layout.row()
            #row.prop(item, "name")

            column = self.layout.column(heading ="Selected Info")
            row.label(text="Name: "+str(item.name))
            #column.prop(item, "Name")
            column.label(text="Num Frames: "+str(item.numFrames))
            column.label(text="FPS: "+str(round(item.framesPerSecond, 2)))
            column.label(text="Length: "+str(round(item.duration, 2))+" seconds.")
            column.label(text=" Animation Type: "+str(item.SkeletalAnimationType))
            if len(item.AdditiveType):
                column.label(text="Additive Type: "+str(item.AdditiveType))
            #row.prop(item, "prop2")


import os

from io_import_w2l import fbx_util
from io_import_w2l import get_uncook_path
from bpy.props import StringProperty, BoolProperty
from bpy_extras.io_utils import (
        ImportHelper
        )

class WM_OT_import_w3_fbx(Operator, ImportHelper):
    """Select an entire character folder or single FBX file. If you select multiple characters, all their skeletons will be merged into one, not recommended."""
    bl_idname = "import_scene.witcher3_fbx_ding"
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

        # If the user didn't change the uncook path from the default
        if uncook_path == 'E:\\Path_to_your_uncooked_folder\\Uncooked\\':
            raise Exception("Please browse your Uncooked folder in the Addon Preferences UI in Edit->Preferences->Addons->Witcher 3 FBX Import Tools.")

        #tex_table = load_texture_table()
        #bpy.ops.outliner.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)

        fbx_util.importFbx(filepath
                            ,"name"
                            ,"name"
                            ,uncook_path = uncook_path
                            ,keep_lod_meshes = keep_lod_meshes
                        )

        return {'FINISHED'}


def menu_func_import(self, context):
    self.layout.operator(WM_OT_import_w3_fbx.bl_idname, text="Witcher 3 FBX Ding")


#-----------------------------------------------------------------------------
#
classes = [
    ButtonOperatorImportW2Anims,
    WM_OT_import_w3_fbx,
    ListItem,
    TOOL_UL_List,
    TOOL_OT_List_Add,
    TOOL_OT_List_Remove,
    TOOL_OT_List_Reorder,
    TOOL_PT_Panel,
    TOOL_OT_List_LoadAnim,
]



def register():
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)
    for c in classes:
        bpy.utils.register_class(c)

    # bpy.types.Scene.anim_export_name = StringProperty(
    #        name="Anim Export Name",
    #        description="Name of the animation",
    #        default="My_New_Anim")
    bpy.types.Scene.demo_list = CollectionProperty(type = ListItem)
    bpy.types.Scene.list_index = IntProperty(name = "Index for demo_list",
                                             default = 0)


def unregister():
    del bpy.types.Scene.demo_list
    del bpy.types.Scene.list_index
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    #del bpy.types.Scene.anim_export_name
    for c in classes:
        bpy.utils.unregister_class(c)
        
if __name__ == '__main__':
    register()