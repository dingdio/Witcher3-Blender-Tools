
import logging
import os
from pathlib import Path
log = logging.getLogger(__name__)

from ..importers import import_anims
from ..importers import import_cutscene
from ..importers import import_scene

import bpy
from bpy.types import Panel, Operator, UIList, PropertyGroup
from bpy.props import IntProperty, StringProperty, CollectionProperty, FloatProperty, BoolProperty
from bpy_extras.io_utils import ImportHelper

from .. import fbx_util
from .. import get_uncook_path

def add_scene_section(name, json_data, scene):
    if not hasattr(scene, "witcher_sections"):
        scene["witcher_sections"] = []
    
    section = scene.witcher_sections.add()
    section.name = name
    section.json_data = json_data

class WitcherSection(bpy.types.PropertyGroup):
    name = StringProperty(name="Name")
    json_data = StringProperty(name="JSON Data")

class ButtonOperatorImportW2scene(bpy.types.Operator, ImportHelper):
    """Import W2 Cutscee"""
    bl_idname = "witcher.import_w2_scene"
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
    def invoke(self, context, event):
        UNCOOK_PATH = os.path.join(get_uncook_path(context),"animations\\")
        if os.path.exists(UNCOOK_PATH):
            self.filepath = UNCOOK_PATH if self.filepath == '' else self.filepath
        return ImportHelper.invoke(self, context, event)

class ButtonOperatorImportW2cutscene(bpy.types.Operator, ImportHelper):
    """Import W2 Cutscee"""
    bl_idname = "witcher.import_w2_cutscene"
    bl_label = "W2 Cutscene"
    filename_ext = ".w2cutscene"
    def execute(self, context):
        if os.path.isdir(self.filepath):
            self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
            return {'CANCELLED'}

        cutscene_data = import_cutscene.import_w3_cutscene(self.filepath)
        return {'FINISHED'}
    def invoke(self, context, event):
        UNCOOK_PATH = os.path.join(get_uncook_path(context),"animations\\")
        if os.path.exists(UNCOOK_PATH):
            self.filepath = UNCOOK_PATH if self.filepath == '' else self.filepath
        return ImportHelper.invoke(self, context, event)

from .. import get_W3_VOICE_PATH
from ..ui.ui_utils import WITCH_PT_Base

class WITCHER_PT_scene_panel(WITCH_PT_Base, Panel):
    #bl_parent_id = "WITCH_PT_ENTITY_Panel"
    bl_idname = "WITCHER_PT_scene_panel"
    bl_label = "Scene / Cutscene"
    bl_description = ""
    #bl_options = {'HEADER_LAYOUT_EXPAND'}
    bl_options = {'DEFAULT_CLOSED'}

    def draw_header(self, context):
        self.layout.label(text="", icon='SCENE_DATA')

    def draw(self, context):
        """
        """
        object = context.scene
        if object == None:
            return


        row = self.layout.row()
        row.operator(ButtonOperatorImportW2cutscene.bl_idname, text="Import CS (.w2cutscene)", icon='SPHERE')
        

        
        box = self.layout.box()
        row = box.row()
        col = row.column(align=True)
        col.template_list("TOOL_UL_List", "The_cs_List", object,
                            "witcher_w2cutscene_list", object, "witcher_w2cutscene_list_index")
        col = row.column()
        box.operator("witcher.list_loadanim", text="Load").action = "load_cutscene"


        row = self.layout.row()
        row.operator(ButtonOperatorImportW2scene.bl_idname, text="Import Scene (.w2scene)", icon='SPHERE')
        
        
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
        log.debug("Selected Index: %s", scene.witcher_sections_index)

        sceneImporter = import_scene.import_w3_scene(context.scene.witcher_sections_filepath)
        sceneImporter.load_sections()
        this_section = sceneImporter.scene_sections[scene.witcher_sections_index]
        log.debug("Section: %s", this_section.sectionName)
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
    WitcherSection,
    ButtonOperatorImportW2cutscene,
    ButtonOperatorImportW2scene,
    WITCHER_PT_scene_panel,
    WITCHER_SECTIONS_UL_List,
    Witcher_OT_load_section,
    #WITCHER_PT_witcher_sections_panel,
]




def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.witcher_sections = bpy.props.CollectionProperty(type=WitcherSection)
    bpy.types.Scene.witcher_sections_index = bpy.props.IntProperty(default=0)
    bpy.types.Scene.witcher_sections_filepath = bpy.props.StringProperty(default="")

def unregister():
    if hasattr(bpy.types.Scene, "witcher_sections"):
        del bpy.types.Scene.witcher_sections
    if hasattr(bpy.types.Scene, "witcher_sections_index"):
        del bpy.types.Scene.witcher_sections_index
    if hasattr(bpy.types.Scene, "witcher_sections_filepath"):
        del bpy.types.Scene.witcher_sections_filepath
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
