import os
from pathlib import Path

from io_import_w2l.setup_logging_bl import *
log = logging.getLogger(__name__)

def get_game_path(context) -> str:
    addon_prefs = context.preferences.addons[__package__].preferences
    witcher_game_path = addon_prefs.witcher_game_path
    return witcher_game_path

def get_witcher2_game_path(context) -> str:
    addon_prefs = context.preferences.addons[__package__].preferences
    return addon_prefs.witcher2_game_path

def get_uncook_path(context) -> str:
    addon_prefs = context.preferences.addons[__package__].preferences
    uncook_path = addon_prefs.uncook_path
    return uncook_path

def get_mod_directory(context) -> str:
    addon_prefs = context.preferences.addons[__package__].preferences
    mod_directory = addon_prefs.mod_directory
    return mod_directory

def get_wolvenkit(context) -> str:
    addon_prefs = context.preferences.addons[__package__].preferences
    wolvenkit = addon_prefs.wolvenkit
    return wolvenkit

def get_fbx_uncook_path(context) -> str:
    addon_prefs = context.preferences.addons[__package__].preferences
    fbx_uncook_path = addon_prefs.fbx_uncook_path
    return fbx_uncook_path

def get_texture_path(context) -> str:
    addon_prefs = context.preferences.addons[__package__].preferences
    tex_uncook_path = addon_prefs.tex_uncook_path
    return tex_uncook_path

def get_w2_unbundle_path(context) -> str:
    addon_prefs = context.preferences.addons[__package__].preferences
    w2_unbundle_path = addon_prefs.w2_unbundle_path
    return w2_unbundle_path

def get_modded_texture_path(context) -> str:
    addon_prefs = context.preferences.addons[__package__].preferences
    tex_mod_uncook_path = addon_prefs.tex_mod_uncook_path
    return tex_mod_uncook_path

def get_tex_ext(context) -> str:
    addon_prefs = context.preferences.addons[__package__].preferences
    tex_ext = addon_prefs.tex_ext
    return tex_ext

def get_W3_VOICE_PATH(context) -> str:
    addon_prefs = context.preferences.addons[__package__].preferences
    W3_VOICE_PATH = addon_prefs.W3_VOICE_PATH
    return W3_VOICE_PATH

def get_W3_OGG_PATH(context) -> str:
    addon_prefs = context.preferences.addons[__package__].preferences
    W3_OGG_PATH = addon_prefs.W3_OGG_PATH
    return W3_OGG_PATH

def get_W3_FOLIAGE_PATH(context) -> str:
    addon_prefs = context.preferences.addons[__package__].preferences
    W3_FOLIAGE_PATH = addon_prefs.W3_FOLIAGE_PATH
    return W3_FOLIAGE_PATH

def get_W3_REDCLOTH_PATH(context) -> str:
    addon_prefs = context.preferences.addons[__package__].preferences
    W3_REDCLOTH_PATH = addon_prefs.W3_REDCLOTH_PATH
    return W3_REDCLOTH_PATH

def get_use_fbx_repo(context) -> str:
    addon_prefs = context.preferences.addons[__package__].preferences
    use_fbx_repo = addon_prefs.use_fbx_repo
    return use_fbx_repo

from io_import_w2l import CR2W
from io_import_w2l.CR2W.w3_types import CSkeletalAnimationSetEntry
from io_import_w2l.CR2W.dc_anims import load_lipsync_file
#from io_import_w2l.importers import *
from io_import_w2l.importers import (
                                    import_anims,
                                    import_rig,
                                    import_w2l,
                                    import_mesh,
                                    import_w2w,
                                    import_texarray
                                    )
from io_import_w2l.exporters import (
                                    export_anims
                                    )
from io_import_w2l import constrain_util
from io_import_w2l import file_helpers
#from io_import_w2l.cloth_util import setup_w3_material_CR2W


#ui
from io_import_w2l.ui import ui_map
from io_import_w2l.ui.ui_map import (WITCH_OT_w2L,
                                     WITCH_OT_w2w,
                                     WITCH_OT_load_layer,
                                     WITCH_OT_load_layer_group,
                                     WITCH_OT_radish_w2L)
from io_import_w2l.ui import ui_anims
from io_import_w2l.ui import ui_entity
from io_import_w2l.ui import ui_morphs
from io_import_w2l.ui import ui_material
from io_import_w2l.ui.ui_morphs import (WITCH_OT_morphs)

from io_import_w2l.ui import ui_voice
from io_import_w2l.ui import ui_mimics
from io_import_w2l.ui import ui_anims_list
from io_import_w2l.ui import ui_import_menu
from io_import_w2l.ui import ui_scene
from io_import_w2l.ui.ui_mesh import WITCH_OT_w2mesh, WITCH_OT_apx, WITCH_OT_w2mesh_export
from io_import_w2l.ui.ui_utils import WITCH_PT_Base
from io_import_w2l.ui.ui_entity import WITCH_OT_ENTITY_lod_toggle
#from io_import_w2l.ui.ui_entity import WITCH_OT_w2ent_chara
from io_import_w2l.ui.ui_entity import WITCH_OT_w2ent
from io_import_w2l.ui.ui_material import WITCH_OT_w2mg, WITCH_OT_w2mi, WITCH_OT_xbm

from io_import_w2l.ui.ui_anims import WITCH_OT_ImportW2Rig, WITCH_OT_ExportW2AnimJson, WITCH_OT_ExportW2RigJson

from io_import_w2l import w3_material_nodes
from io_import_w2l import w3_material_blender
from io_import_w2l import w3_material_nodes_custom

import bpy
from bpy.types import (Panel, Operator)
from bpy.props import StringProperty, BoolProperty
from mathutils import Vector
from bpy_extras.io_utils import ImportHelper, ExportHelper
import addon_utils

bl_info = {
    "name": "Witcher 3 Tools",
    "author": "Dingdio",
    "version": (0, 7, 1),
    "blender": (3, 5, 1),
    "location": "File > Import-Export > Witcher 3 Assets",
    "description": "Tools for Witcher 3 and Witcher 2",
    "warning": "",
    "doc_url": "https://github.com/dingdio/Witcher3_Blender_Tools",
    "category": "Import-Export"
}

class Witcher3AddonPrefs(bpy.types.AddonPreferences):
    # this must match the addon name, use '__package__'
    # when defining this in a submodule of a python package.
    bl_idname = __package__

    witcher_game_path: StringProperty(
        name="Witcher 3 Path",
        subtype='DIR_PATH',
        default="E:\\GOG Games\\The Witcher 3 Wild Hunt GOTY",
        description="Path where The Witcher 3 is installed."
    )
    witcher2_game_path: StringProperty(
        name="Witcher 2 Path",
        subtype='DIR_PATH',
        default="G:\\GOG Games\\The Witcher 2",
        description="Path where The Witcher 2 is installed."
    )
    uncook_path: StringProperty(
        name="Uncook Path",
        subtype='DIR_PATH',
        default="E:\\w3.modding\\modkit_new\\r4data",#'E:\\w3.modding\\modkit\\r4data',
        description="Path where you uncooked the game files."
    )
    wolvenkit: StringProperty(
        name="Wolvenkit 7 CLI exe",
        subtype='FILE_PATH',
        default="G:\\sourcetree\\WolvenKit-7\\WolvenKit.CLI\\bin\\Release\\net481\\WolvenKit.CLI.exe",
        description="Wolvenkit .exe."
    )
    mod_directory: StringProperty(
        name="Wolvenkit Project Path",
        subtype='DIR_PATH',
        #default="E:\\w3.mods\\wolvenProjects\\mesh_import_testing",
        default="E:\\w3.mods\\wolvenProjects\\mesh_replace_new",
        description="Path of the current Wolvenkit mod."
    )
    fbx_uncook_path: StringProperty(
        name="Uncook Path FBX (.fbx)",
        subtype='DIR_PATH',
        default='E:\\w3_uncook\\FBXs',
        description="Path where you exported the FBX files."
    )

    tex_uncook_path: StringProperty(
        name="Uncook Path TEXTURES (.tga)",
        subtype='DIR_PATH',
        default='E:\\w3_uncook_new',#"E:\\w3_uncook_new",#
        description="Path where you exported the tga files."
    )
    
    w2_unbundle_path: StringProperty(
        name="Witcher 2 Unbundle",
        subtype='DIR_PATH',
        default='D:\\Witcher2_extracted',
        description="Extracted Witcher 2 dzip files"
    )

    tex_mod_uncook_path: StringProperty(
        name="(optional) Uncook Path modded TEXTURES (.tga)",
        subtype='DIR_PATH',
        default='E:\\w3.modding\\modkit\\modZOldWitcherArmour',
        description="(optional) Path where you exported the tga files from a mod."
    )
    
    tex_ext_opts = [
        #("custom", "Custom", "Description for value 1"),
        (".tga", ".tga", ".tga"),
        (".dds", ".dds", ".dds"),
        (".png", ".png", ".png"),
    ]
    tex_ext: bpy.props.EnumProperty(
        name="Texture Type",
        description="Select prefered texture type",
        items=tex_ext_opts,
        default=".tga",
    )
    

    W3_FOLIAGE_PATH: StringProperty(
        name="Uncook Path FOLIAGE (.fbx)",
        subtype='DIR_PATH',
        default='E:\\w3_uncook\\FBXs\\FOLIAGE',
        description="Path where you exported the fbx files."
    )

    W3_REDCLOTH_PATH: StringProperty(
        name="Uncook Path REDCLOTH (.apx)",
        subtype='DIR_PATH',
        default='E:\\w3_uncook\\FBXs\\REDCLOTH',
        description="Path where you exported the apx files."
    )

    W3_REDFUR_PATH: StringProperty(
        name="Uncook Path REDFUR (.apx)",
        subtype='DIR_PATH',
        default='E:\\w3_uncook\\FBXs\\REDFUR',
        description="Path where you exported the apx files."
    )

    W3_VOICE_PATH: StringProperty(
        name="Extracted lipsync (.cr2w)",
        subtype='DIR_PATH',
        default=r'E:\w3.modding\radish-tools_PREVIEW\docs.speech\enpc.w3speech-extracted_GOOD\enpc.w3speech-extracted',
        description="Path where you extracted w3speech"
    )

    W3_OGG_PATH: StringProperty(
        name="Converted .wem files (.ogg)",
        subtype='DIR_PATH',
        default='F:\\voice_synth\\witcher\\speech\\ogg',
        description="Path with ogg files"
    )

    #keep_lod_meshes: bpy.props.BoolProperty(name="Keep lod meshes", default = False)
    use_fbx_repo: bpy.props.BoolProperty(name="Use FBX repo",
                                        default=False,
                                        description="Enable this to load from the fbx repo when importing meshes, maps etc.")

    #importFacePoses
    def draw(self, context):
        layout = self.layout
        layout.label(text="<< WITCHER 3 SETTINGS >>")
        layout.prop(self, "uncook_path")
        layout.prop(self, "tex_uncook_path")
        layout.prop(self, "witcher_game_path")
        
        layout.label(text="<< WITCHER 2 SETTINGS >>")
        layout.prop(self, "w2_unbundle_path")
        layout.prop(self, "witcher2_game_path")
        
        layout.label(text="<< COMMON SETTINGS >>")
        layout.prop(self, "tex_ext")
        
        layout.label(text='<< MOD PATHS >>')
        layout.prop(self, "wolvenkit")
        layout.prop(self, "mod_directory")
        layout.prop(self, "tex_mod_uncook_path")
        
        
        layout.label(text='<< WITCHER 3 EXTRA SETTINGS  >>')
        layout.prop(self, "W3_FOLIAGE_PATH")
        layout.prop(self, "W3_REDCLOTH_PATH")
        layout.prop(self, "W3_REDFUR_PATH")
        layout.prop(self, "W3_VOICE_PATH")
        layout.prop(self, "W3_OGG_PATH")
        
        layout.label(text="<< FBX >>")
        layout.prop(self, "use_fbx_repo")
        layout.prop(self, "fbx_uncook_path")

class WITCH_OT_ViewportNormals(bpy.types.Operator):
    bl_description = "Switch normal map nodes to a faster custom node. Get https://github.com/theoldben/BlenderNormalGroups addon to enable button"
    bl_idname = 'witcher.normal_map_group'
    bl_label = "Normal Map nodes to Custom"
    bl_options = {'UNDO'}

    @classmethod
    def poll(self, context):
        (exist, enabled) = addon_utils.check("normal_map_to_group")
        return enabled

    def execute(self, context):
        bpy.ops.node.normal_map_group()
        return {'FINISHED'}

class WITCH_OT_AddConstraints(bpy.types.Operator):
    """Add Constraints"""
    bl_idname = "witcher.add_constraints"
    bl_label = "Add Constraints"
    bl_description = "Object Mode. Create bone constraints based on same bone names or r_weapon/l_weapon bones. Select Armature then Ctrl+Select Armature you want to attach to it"
    action: StringProperty(default="default")
    def execute(self, context):
        scene = context.scene
        action = self.action
        if action == "add_const":
            constrain_util.do_it(1)
        if action == "add_const_ik":
            constrain_util.do_it(2)
        elif action == "attach_r_weapon":
            constrain_util.attach_weapon("r_weapon")
        elif action == "attach_l_weapon":
            constrain_util.attach_weapon("l_weapon")
        return {'FINISHED'}


class WITCH_OT_load_texarray(bpy.types.Operator, ImportHelper):
    """WITCH_OT_load_texarray"""
    bl_idname = "witcher.load_texarray"
    bl_label = "Load texarray json"
    filename_ext = ".json"
    bl_options = {'REGISTER', 'UNDO'}

    filter_glob: StringProperty(default='*.json', options={'HIDDEN'})
    def execute(self, context):
        fdir = self.filepath
        print("Importing Material")
        if os.path.isdir(fdir):
            self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
            return {'CANCELLED'}
        else:
            import_texarray.start_import(fdir)
        return {'FINISHED'}

#----------------------------------------------------------
#   Utilities panel
#----------------------------------------------------------

class WITCH_PT_Utils(WITCH_PT_Base, bpy.types.Panel):
    bl_label = "Utilities"

    def draw(self, context):
        ob = context.object
        coll = context.collection
        scn = context.scene
        layout = self.layout
        box = layout.box()
        if ob:
            box.label(text = "Active Object: %s" % ob.entity_type)
            box.prop(ob, "name")
            if ob.template:
                box.prop(ob, "template")
            if ob.entity_type:
                box.prop(ob, "entity_type")
                
            if ob.type == "MESH":
                mesh_settings = ob.witcherui_MeshSettings
                box.prop(mesh_settings, "lod_level")
                box.prop(mesh_settings, "distance")
                box.prop(mesh_settings, "mat_id")
                if mesh_settings.lod_level == 0:
                    box.label(text = "Global Mesh Settings:")
                    box.prop(mesh_settings, "autohideDistance")
                    box.prop(mesh_settings, "isTwoSided")
                    box.prop(mesh_settings, "useExtraStreams")
                    box.prop(mesh_settings, "mergeInGlobalShadowMesh")
                    box.prop(mesh_settings, "entityProxy")

        else:
            box.label(text = "No active object")

        box = layout.box()
        if coll:
            box.prop(coll, "name")

            #CLayerInfo
            if coll.level_path:
                box.prop(coll, "level_path")
            if coll.layerBuildTag:
                box.prop(coll, "layerBuildTag")
            if coll.level_path:
                row = layout.row()
                row.operator(WITCH_OT_load_layer.bl_idname, text="Load This Level", icon='CUBE')

            #CLayerGroup
            if coll.group_type and coll.group_type == "LayerGroup":
                row = layout.row()
                row.operator(WITCH_OT_load_layer_group.bl_idname, text="Load This LayerGroup", icon='CUBE')
        else:
            box.label(text = "No active collection")

class WITCH_PT_Main(WITCH_PT_Base, bpy.types.Panel):
    bl_idname = "WITCH_PT_Main"
    bl_label = "Witcher 3 Tools"

    def draw(self, context):
        layout:bpy.types.UILayout = self.layout # UILayout
        #Map
        row = layout.row().box()
        row = row.column(align=True)
        row.label(text='Map Import')
        row.operator(WITCH_OT_w2L.bl_idname, text="Layer (.w2l)", icon='SPHERE')
        row.operator(WITCH_OT_w2w.bl_idname, text="World (.w2w)", icon='WORLD_DATA')
        row.operator(WITCH_OT_load_texarray.bl_idname, text="Texarray (.json)", icon='TEXTURE_DATA')
        row = layout.row().box()
        row = row.column(align=True)
        
        row.label(text='Radish yml Export')
        op = row.operator(WITCH_OT_radish_w2L.bl_idname, text="Layer (.yml)", icon='SPHERE')
        row = layout.row().box()
        row = row.column(align=True)

        #Mesh
        row.label(text='Mesh Import')
        row.operator(WITCH_OT_w2mesh.bl_idname, text="Mesh (.w2mesh)", icon='MESH_DATA')
        row.operator(WITCH_OT_apx.bl_idname, text="Redcloth (.redcloth)", icon='MESH_DATA')

        #Mesh
        row.label(text='Mesh Export')
        row.operator(WITCH_OT_w2mesh_export.bl_idname, text="Mesh (.w2mesh)", icon='MESH_DATA')

        ob = context.object
        if context.selected_objects:
            if ob and ob.type == "MESH" or ob and ob.type == "ARMATURE":
                if ob.type == "ARMATURE":
                    armature_meshes = [child for child in ob.children if child.type == 'MESH']
                    if armature_meshes:
                        ob = armature_meshes[0]

                
                box = layout.box()
                row = box.row(align=False)
                row.prop(ob.witcherui_MeshSettings, "witcher_meshexport_collapse", icon="TRIA_DOWN" if not ob.witcherui_MeshSettings.witcher_meshexport_collapse else "TRIA_RIGHT", icon_only=True, emboss=False)
                row.label(text="Mesh Export Settings")

                if not ob.witcherui_MeshSettings.witcher_meshexport_collapse:
                    mesh_settings = ob.witcherui_MeshSettings
                    box.prop(mesh_settings, "lod_level")
                    box.prop(mesh_settings, "distance")
                    box.prop(mesh_settings, "mat_id")
                    if mesh_settings.lod_level == 0:
                        box.label(text = "Global Mesh Settings:")
                        box.prop(mesh_settings, "autohideDistance")
                        box.prop(mesh_settings, "isTwoSided")
                        box.prop(mesh_settings, "useExtraStreams")
                        box.prop(mesh_settings, "mergeInGlobalShadowMesh")
                        box.prop(mesh_settings, "entityProxy")
                        box.prop(mesh_settings, "item_repo_path")
                        box.prop(mesh_settings, "make_export_dir")
                        box.prop(mesh_settings, "is_DLC")
                        
            
        #Mesh
        row = layout.row().box()
        row = row.column(align=True)
        row.label(text='Material Import')
        row.operator(WITCH_OT_w2mi.bl_idname, text="Instance (.w2mi)", icon='MESH_DATA')
        row.operator(WITCH_OT_w2mg.bl_idname, text="Shader (.w2mg)", icon='MESH_DATA')
        row.operator(WITCH_OT_xbm.bl_idname, text="Texture (.xbm)", icon='SPHERE')

        # row.label(text='Material Export')
        # row.operator(WITCH_OT_w2mi.bl_idname, text="Instance (.w2mi)", icon='MESH_DATA')

        #Entity
        row = layout.row().box()
        row = row.column(align=True)
        row.label(text='Entity Import')
        row.operator(WITCH_OT_w2ent.bl_idname, text="Items (.w2ent)", icon='SPHERE')

        #Animation
        row = layout.row().box()
        row = row.column(align=True)
        row.label(text='Animation Tools')
        row.operator(WITCH_OT_AddConstraints.bl_idname, text="Add Constraints", icon='CONSTRAINT').action = "add_const"
        row.operator(WITCH_OT_AddConstraints.bl_idname, text="Add Constraints IK", icon='CONSTRAINT').action = "add_const_ik"
        row.operator(WITCH_OT_AddConstraints.bl_idname, text="Attach to r_weapon", icon='CONSTRAINT').action = "attach_r_weapon"
        row.operator(WITCH_OT_AddConstraints.bl_idname, text="Attach to l_weapon", icon='CONSTRAINT').action = "attach_l_weapon"
        row.operator(WITCH_OT_ViewportNormals.bl_idname, text="Faster Viewport Normals", icon='MESH_DATA')

        row = layout.row().box()
        row = row.column(align=True)
        row.label(text='Animation Import')
        row.operator(WITCH_OT_ImportW2Rig.bl_idname, text="Rig (.w2rig)", icon='ARMATURE_DATA')

        row = layout.row().box()
        row = row.column(align=True)
        row.label(text='Animation Export')
        row.operator(WITCH_OT_ExportW2RigJson.bl_idname, text="Rig (.w2rig)", icon='ARMATURE_DATA')
        row.operator(WITCH_OT_ExportW2AnimJson.bl_idname, text="Anim (.w2anims)", icon='MESH_DATA')
        # row.operator(WITCH_OT_ExportW2RigJson.bl_idname, text="Rig Json (.w2rig.json)", icon='ARMATURE_DATA')
        # row.operator(WITCH_OT_ExportW2AnimJson.bl_idname, text="Anim Json (.w2anims.json)", icon='MESH_DATA')


        #Morphs
        row = layout.row().box()
        row = row.column(align=True)
        row.label(text='Morphs')
        row.operator(WITCH_OT_morphs.bl_idname, text="Load Face Morphs", icon='SHAPEKEY_DATA')

        #General Settings
        row = layout.row().box()
        column = row.column(align=True)
        column.label(text='General Settings')
        # addon_prefs = context.preferences.addons[__package__].preferences
        # row.prop(addon_prefs, 'keep_lod_meshes')
        addon_prefs = context.preferences.addons[__package__].preferences
        column.prop(addon_prefs, 'use_fbx_repo')
        column = row.column(align=True)
        row_lod = column.row()
        row_lod.operator(WITCH_OT_ENTITY_lod_toggle.bl_idname, text="lod0").action = "_lod0"
        row_lod.operator(WITCH_OT_ENTITY_lod_toggle.bl_idname, text="lod1").action = "_lod1"
        row_lod.operator(WITCH_OT_ENTITY_lod_toggle.bl_idname, text="lod2").action = "_lod2"
        column = row.column(align=True)
        row = column.row()
        row.operator(WITCH_OT_ENTITY_lod_toggle.bl_idname, text="Hide Collision Mesh").action = "_collisionHide"
        row.operator(WITCH_OT_ENTITY_lod_toggle.bl_idname, text="Show Collision Mesh").action = "_collisionShow"

class WITCH_PT_Quick(WITCH_PT_Base, bpy.types.Panel):
    bl_label = "QUICK ANIMATION IMPORT"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        pass

from bpy.utils import (register_class, unregister_class)

_classes = [
    #ent_import
    WITCH_OT_morphs,
    WITCH_OT_w2L,
    WITCH_OT_w2w,
    # WITCH_OT_w2mi,
    # WITCH_OT_w2mg,
    #WITCH_OT_w2ent,
    WITCH_OT_radish_w2L,
    #anims
    WITCH_OT_AddConstraints,
    #WITCH_OT_ImportW2Rig,
    # WITCH_OT_ExportW2RigJson,
    # WITCH_OT_ExportW2AnimJson,
    WITCH_OT_ViewportNormals,
    WITCH_OT_load_layer,
    WITCH_OT_load_layer_group,
    WITCH_OT_load_texarray,

    #panels
    WITCH_PT_Main,
    #WITCH_PT_Utils,
]

def register():
    bpy.utils.register_class(Witcher3AddonPrefs)
    bpy.types.Object.template = StringProperty(
        name = "template"
    )
    bpy.types.Object.entity_type = StringProperty(
        name = "entity_type"
    )
    bpy.types.Collection.level_path = StringProperty(
        name = "level_path"
    )
    bpy.types.Collection.layerBuildTag = StringProperty(
        name = "layerBuildTag"
    )
    bpy.types.Collection.world_path = StringProperty(
        name = "world_path"
    )
    bpy.types.Collection.group_type = StringProperty(
        name = "group_type"
    )
    for cls in _classes:
        register_class(cls)
    ui_entity.register()
    ui_material.register()
    ui_morphs.register()
    ui_import_menu.register()
    #ui_map.register()
    ui_anims.register()
    ui_scene.register()
    register_class(WITCH_PT_Utils)
    register_class(WITCH_PT_Quick)
    ui_voice.register()
    ui_mimics.register()
    ui_anims_list.register()
    w3_material_nodes.register()
    w3_material_nodes_custom.register()

def unregister():
    w3_material_nodes_custom.unregister()
    unregister_class(WITCH_PT_Quick)
    unregister_class(WITCH_PT_Utils)
    bpy.utils.unregister_class(Witcher3AddonPrefs)
    del bpy.types.Object.template
    del bpy.types.Object.entity_type

    del bpy.types.Collection.level_path
    del bpy.types.Collection.layerBuildTag
    del bpy.types.Collection.world_path
    del bpy.types.Collection.group_type
    for cls in _classes:
        unregister_class(cls)
    ui_import_menu.unregister()
    #ui_map.unregister()
    ui_scene.unregister()
    ui_anims.unregister()
    ui_material.unregister()
    ui_entity.unregister()
    ui_morphs.unregister()
    ui_voice.unregister()
    ui_mimics.unregister()
    ui_anims_list.unregister()
    w3_material_nodes.unregister()
