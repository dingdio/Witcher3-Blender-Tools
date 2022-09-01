from io_import_w2l.setup_logging_bl import *
log = logging.getLogger(__name__)

import os
import bpy
from io_import_w2l.importers import import_entity
from io_import_w2l.importers import import_anims
from bpy.types import Panel, Operator, UIList, PropertyGroup
from bpy.props import IntProperty, StringProperty, CollectionProperty

from io_import_w2l import get_uncook_path

from bpy_extras.io_utils import (
        ImportHelper
        )

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

class APP_TOOL_UL_List(UIList):
    """Demo UIList."""
    bl_idname = "APP_TOOL_UL_List"
    layout_type = "DEFAULT" # could be "COMPACT" or "GRID"

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
        #Make sure your code supports all 3 layout types
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            layout.label(text=item.name)

        elif self.layout_type in {'GRID'}:
            layout.alignment = 'CENTER'
            layout.label(text="")

class ButtonOperatorw2entChara(bpy.types.Operator, ImportHelper):
    """Load Witcher 3 Character"""
    bl_idname = "object.import_w2ent_chara_btn"
    bl_label = "Import Character Json"
    filename_ext = ".w2ent"
    def execute(self, context):
        print("importing now!")
        fdir = self.filepath

        if fdir.endswith(".w2ent") or fdir.endswith(".json"):
            import_entity.import_ent_template(fdir, False)
        else:
            fdir = os.path.join(get_uncook_path(context),"characters\\npc_entities\\main_npc\\lambert.w2ent")
            root_bone = import_entity.import_ent_template(fdir, False)
        return {'FINISHED'}

class APP_TOOL_OT_list_loadapp(Operator):
    """ Add an Item to the UIList"""
    bl_idname = "tool.list_loadapp"
    bl_label = "Load"
    bl_description = "load a new item to the list."

    action: StringProperty(default="default")
    @classmethod
    def poll(cls, context):
        return context.scene

    def execute(self, context):
        scene = context.scene
        action = self.action

        lods = ['_lod0','_lod1','_lod2']

        if "w2anims" == action:
            print("=== load w2anims ====")
            print(scene.main_entity_skeleton)
            
            if scene.animset_list_index >= 0 and scene.animset_list:
                repoPath = scene.animset_list[scene.animset_list_index]
                fdir = os.path.join(get_uncook_path(context),repoPath.path)
                print(fdir)
                loadFromJson = True
                if loadFromJson:
                    if (os.path.exists(fdir+'.json')):
                        fdir = fdir + '.json'
                if "_mimic_" in fdir:
                    import_anims.start_import(context, fdir, rigPath=scene.main_face_skeleton)
                else:
                    import_anims.start_import(context, fdir, rigPath=scene.main_entity_skeleton)

        if "load" == action:
            print("=== load apperance ====")
            if scene.app_list_index >= 0 and scene.app_list:
                item = scene.app_list[scene.app_list_index]

                import_entity.import_from_list_item(context, item)
            # context.scene.app_list.add()
        elif "clear" == action:
            print("=== Debug Clear ====")
            bpy.context.scene.app_list.clear()
        elif action in lods:
            lod_idx = int(action[-1:])
            lod_meshes = []
            for mesh in scene.objects:
                # only for meshes
                if mesh.type == 'MESH':
                    if mesh.name[-5:-1] == "_lod":
                        mesh.hide_viewport = True
                        mesh.hide_render = True
                        if mesh.name[:-5] not in lod_meshes:
                            lod_meshes.append(mesh.name[:-5])
            for lod_mesh in lod_meshes:
                mesh_bl = scene.objects.get(lod_mesh+action)
                if mesh_bl:
                    mesh_bl.hide_viewport = False
                    mesh_bl.hide_render = False
                else:
                    mesh_bl = scene.objects.get(lod_mesh+"_lod"+str(lod_idx-1))
                    if mesh_bl:
                        mesh_bl.hide_viewport = False
                        mesh_bl.hide_render = False
                    else:
                        mesh_bl = scene.objects.get(lod_mesh+"_lod"+str(lod_idx-1))
                        if mesh_bl:
                            mesh_bl.hide_viewport = False
                            mesh_bl.hide_render = False
                        else:
                            log.debug("LOD ERROR")

            # if mesh.name[-5:] == action:
            #     mesh.hide_viewport = False
            #     mesh.hide_render = False

        return {'FINISHED'}



##############################
#       Animset List         #
##############################
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

class ANIMSET_UL_List(UIList):
    """List for the Animsets"""
    bl_idname = "ANIMSET_UL_List"
    layout_type = "DEFAULT" # could be "COMPACT" or "GRID"

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
        #Make sure your code supports all 3 layout types
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            layout.label(text=item.path)

        elif self.layout_type in {'GRID'}:
            layout.alignment = 'CENTER'
            layout.label(text="")

class _CAKE_Base:
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Witcher'
    bl_context = ''#'objectmode'
    #bl_options = {'DEFAULT_CLOSED'}
class APP_TOOL_PT_Panel(_CAKE_Base, Panel):
    bl_idname = "APP_TOOL_PT_Panel"
    bl_label = "Character Appearances"
    bl_description = "Demonstration of UIList Features"
    #bl_options = {'HEADER_LAYOUT_EXPAND'}
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        """ Draw a UI List and its controls using the same format used by
            various UI Lists in the user interface, such as Vertex Groups
            or Shape Keys in the Object Properties Tab of the Properties
            Editor.
        """

        object = context.scene
        if object == None:
            return

        self.layout.label(text = "Character")
        row = self.layout.row()
        op = row.operator(ButtonOperatorw2entChara.bl_idname, text="Import Character", icon='SPHERE')
        op.filepath = os.path.join(get_uncook_path(context),"characters\\")

        row = self.layout.row()
        row.alignment = "CENTER"


        col = row.column(align=True)
        col.template_list("APP_TOOL_UL_List", "The_List", object,
                            "app_list", object, "app_list_index")


        grid = self.layout.grid_flow( columns = 2 )

        grid.operator("tool.list_loadapp", text="Load").action = "load"
        grid.operator("tool.list_loadapp", text="Clear List").action = "clear"

        row = self.layout.row()
        row.operator("tool.list_loadapp", text="Set lod0").action = "_lod0"
        row.operator("tool.list_loadapp", text="Set lod1").action = "_lod1"
        row.operator("tool.list_loadapp", text="Set lod2").action = "_lod2"
        if object.app_list_index >= 0 and object.app_list:
            item = object.app_list[object.app_list_index]

            row = self.layout.row()
            row.prop(item, "name")
        row = self.layout.row()
        row.prop(context.scene, "main_entity_skeleton")
        row = self.layout.row()
        row.prop(context.scene, "main_face_skeleton")


        row = self.layout.row()
        col = row.column(align=True)
        col.template_list("ANIMSET_UL_List", "The_List_2", object,
                            "animset_list", object, "animset_list_index")
        row = self.layout.row()
        row.operator("tool.list_loadapp", text="Load .w2anims").action = "w2anims"


classes = [
    ListItemApp,
    ButtonOperatorw2entChara,
    APP_TOOL_UL_List,
    APP_TOOL_PT_Panel,
    APP_TOOL_OT_list_loadapp,

    #animset
    ListItemAnimset,
    ANIMSET_UL_List,
]



def register():
    for c in classes:
        bpy.utils.register_class(c)


    #apperance list
    bpy.types.Scene.app_list = CollectionProperty(type = ListItemApp)
    bpy.types.Scene.app_list_index = IntProperty(name = "Index for app_list",
                                             default = 0)

    #TODO The follow stuff should be attached to armature object?
    bpy.types.Scene.main_entity_skeleton = StringProperty(
                                            name="Main Rig",
                                            description="Name of the rig",
                                            default="")

    bpy.types.Scene.main_face_skeleton = StringProperty(
                                            name="Main Face Rig",
                                            description="Name of the rig",
                                            default="")

    #animset list
    bpy.types.Scene.animset_list = CollectionProperty(type = ListItemAnimset)
    bpy.types.Scene.animset_list_index = IntProperty(name = "Index for Animset list",
                                             default = 0)


def unregister():
    del bpy.types.Scene.app_list
    del bpy.types.Scene.app_list_index

    del bpy.types.Scene.main_entity_skeleton
    del bpy.types.Scene.main_face_skeleton

    del bpy.types.Scene.animset_list
    del bpy.types.Scene.animset_list_index
    for c in classes:
        bpy.utils.unregister_class(c)

if __name__ == '__main__':
    register()