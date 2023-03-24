
from imp import reload
import os
from pathlib import Path
from io_import_w2l.setup_logging_bl import *
log = logging.getLogger(__name__)

from io_import_w2l.importers import import_anims
from io_import_w2l.importers import import_cutscene
from io_import_w2l.importers import import_scene
reload(import_scene)

import bpy
from bpy.types import Panel, Operator, UIList, PropertyGroup
from bpy.props import IntProperty, StringProperty, CollectionProperty, FloatProperty
from bpy_extras.io_utils import (
        ImportHelper
        )

import os

from io_import_w2l import fbx_util
from io_import_w2l import get_uncook_path
from bpy.props import StringProperty, BoolProperty
from bpy_extras.io_utils import (
        ImportHelper
        )

def add_scene_section(name, json_data, scene):
    if not hasattr(scene, "witcher_sections"):
        scene["witcher_sections"] = []
    
    section = scene.witcher_sections.add()
    section.name = name
    section.json_data = json_data

class WitcherSection(bpy.types.PropertyGroup):
    name = StringProperty(name="Name")
    json_data = StringProperty(name="JSON Data")
bpy.utils.register_class(WitcherSection)

class ButtonOperatorImportW2scene(bpy.types.Operator, ImportHelper):
    """Import W2 Cutscee"""
    bl_idname = "object.import_w2_scene"
    bl_label = "W2 Scene"
    filename_ext = ".w2scene"
    def execute(self, context):
        if os.path.isdir(self.filepath):
            self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
            return {'CANCELLED'}
        context.scene.witcher_sections_filepath = self.filepath
        context.scene.witcher_sections.clear()
        sceneImporter = import_scene.import_w3_scene(self.filepath)
        sceneImporter.load_sections()
        for section in sceneImporter.scene_sections:
            add_scene_section(section.sectionName, "{}", context.scene)
        #sceneImporter.execute()
        bpy.context.view_layer.update()
        return {'FINISHED'}

class ButtonOperatorImportW2cutscene(bpy.types.Operator, ImportHelper):
    """Import W2 Cutscee"""
    bl_idname = "object.import_w2_cutscene"
    bl_label = "W2 Cutscene"
    filename_ext = ".w2cutscene"
    def execute(self, context):
        if os.path.isdir(self.filepath):
            self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
            return {'CANCELLED'}

        cutscene_data = import_cutscene.import_w3_cutscene(self.filepath)
        return {'FINISHED'}

from io_import_w2l import get_W3_VOICE_PATH
from io_import_w2l.ui.ui_utils import WITCH_PT_Base

class WITCHER_PT_scene_panel(WITCH_PT_Base, Panel):
    #bl_parent_id = "WITCH_PT_ENTITY_Panel"
    bl_idname = "WITCHER_PT_scene_panel"
    bl_label = "Scene / Cutscene"
    bl_description = ""
    #bl_options = {'HEADER_LAYOUT_EXPAND'}
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        """
        """
        object = context.scene
        if object == None:
            return


        row = self.layout.row()
        op = row.operator(ButtonOperatorImportW2cutscene.bl_idname, text="Import CS (.w2cutscene)", icon='SPHERE')
        op.filepath = os.path.join(get_uncook_path(context),"animations\\")

        
        box = self.layout.box()
        row = box.row()
        col = row.column(align=True)
        col.template_list("TOOL_UL_List", "The_List", object,
                            "demo_list", object, "list_index")
        col = row.column()
        box.operator("tool.list_loadanim", text="Load").action = "load"


        row = self.layout.row()
        op = row.operator(ButtonOperatorImportW2scene.bl_idname, text="Import Scene (.w2scene)", icon='SPHERE')
        op.filepath = os.path.join(get_uncook_path(context),"animations\\")
        
        object = context.scene
        if object == None:
            return

        box = self.layout.box()
        row = box.row()
        col = row.column(align=True)
        col.template_list("WITCHER_SECTIONS_UL_List", "", object, "witcher_sections", object, "witcher_sections_index")
        col = row.column()
        box.operator(Witcher_OT_load_section.bl_idname, text="Load Section")

class WITCHER_SECTIONS_UL_List(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        # Draw the scene section name
        layout.label(text=item.name)

class Witcher_OT_load_section(bpy.types.Operator):
    bl_idname = "witcher.load_section"
    bl_label = "Print Selected Index"
    bl_description = "Print the index of the selected scene section"

    def execute(self, context):
        # Get the scene object
        scene = context.scene

        # Print the index of the selected scene section
        print("Selected Index:", scene.witcher_sections_index)

        sceneImporter = import_scene.import_w3_scene(context.scene.witcher_sections_filepath)
        sceneImporter.load_sections()
        this_section = sceneImporter.scene_sections[scene.witcher_sections_index]
        print(this_section.sectionName)
        sceneImporter.load_section(this_section)
        sceneImporter.execute()


        return {'FINISHED'}

class WITCHER_PT_witcher_sections_panel(WITCH_PT_Base, Panel):
    bl_parent_id = "WITCHER_PT_scene_panel"
    bl_idname = "WITCHER_PT_witcher_sections_panel"
    bl_label = "Scene Sections"
    bl_description = ""
    #bl_options = {'HEADER_LAYOUT_EXPAND'}
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        object = context.scene
        if object == None:
            return

        box = self.layout.box()
        row = box.row()
        col = row.column(align=True)
        col.template_list("WITCHER_SECTIONS_UL_List", "", object, "witcher_sections", object, "witcher_sections_index")
        col = row.column()
        box.operator(Witcher_OT_load_section.bl_idname, text="Load Section")

classes = [
    ButtonOperatorImportW2cutscene,
    ButtonOperatorImportW2scene,
    WITCHER_PT_scene_panel,
    WITCHER_SECTIONS_UL_List,
    Witcher_OT_load_section,
    #WITCHER_PT_witcher_sections_panel,
]




def register():
    bpy.types.Scene.witcher_sections = bpy.props.CollectionProperty(type=WitcherSection)
    bpy.types.Scene.witcher_sections_index = bpy.props.IntProperty(default=0)
    bpy.types.Scene.witcher_sections_filepath = bpy.props.StringProperty(default="")
    for c in classes:
        bpy.utils.register_class(c)

def unregister():
    del bpy.types.Scene.witcher_sections
    del bpy.types.Scene.witcher_sections_index
    del bpy.types.Scene.witcher_sections_filepath
    for c in classes:
        bpy.utils.unregister_class(c)
