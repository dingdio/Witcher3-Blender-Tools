# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTIBILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

import os
from pathlib import Path
import time
from typing import Tuple



def get_uncook_path(context) -> str:
    addon_prefs = context.preferences.addons[__package__].preferences
    uncook_path = addon_prefs.uncook_path
    return uncook_path

def get_fbx_uncook_path(context) -> str:
    addon_prefs = context.preferences.addons[__package__].preferences
    fbx_uncook_path = addon_prefs.fbx_uncook_path
    return fbx_uncook_path

def get_texture_path(context) -> str:
    addon_prefs = context.preferences.addons[__package__].preferences
    tex_uncook_path = addon_prefs.tex_uncook_path
    return tex_uncook_path

def get_modded_texture_path(context) -> str:
    addon_prefs = context.preferences.addons[__package__].preferences
    tex_mod_uncook_path = addon_prefs.tex_mod_uncook_path
    return tex_mod_uncook_path

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


def get_keep_lod_meshes(context) -> str:
    addon_prefs = context.preferences.addons[__package__].preferences
    keep_lod_meshes = addon_prefs.keep_lod_meshes
    return keep_lod_meshes


#logging
#from io_import_w2l.setup_logging_bl import *
from io_import_w2l.CR2W.setup_logging import *

from io_import_w2l import CR2W
from io_import_w2l.CR2W.w3_types import CSkeletalAnimationSetEntry
from io_import_w2l.CR2W.dc_anims import load_lipsync_file
from io_import_w2l.importers import import_anims
from io_import_w2l.importers import import_rig
from io_import_w2l.importers import import_w2l
#from io_import_w2l import export_anims
from io_import_w2l import constrain_util
from io_import_w2l import file_helpers

#ui
from io_import_w2l.ui import ui_anims
from io_import_w2l.ui import ui_entity
from io_import_w2l.ui import ui_morphs
from io_import_w2l.ui import ui_voice
from io_import_w2l.ui import ui_mimics
from io_import_w2l.ui import ui_anims_list
#from io_import_w2l import filter_list


bl_info = {
    "name": "Witcher 3 Tools",
    "author": "Dingdio",
    "version": (1, 0),
    "blender": (3, 00, 0),
    "location": "View3D > Witcher 3 Tools",
    "description": "Tools for Witcher 3",
    "warning": "",
    "doc_url": "",
    "category": "",
}

import bpy
from bpy.types import (Panel, Operator)
from bpy.app.handlers import persistent

from io_import_w2l.importers import import_w2w
from io_import_w2l.importers import import_texarray
from bpy.props import StringProperty

from bpy_extras.io_utils import (
        ImportHelper
        )

class Witcher3AddonPrefs(bpy.types.AddonPreferences):
    # this must match the addon name, use '__package__'
    # when defining this in a submodule of a python package.
    bl_idname = __package__

    uncook_path: StringProperty(
        name="Uncook Path",
        subtype='DIR_PATH',
        default='E:\\w3.modding\\modkit\\r4data',
        description="Path where you uncooked the game files."
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
        default='E:\\w3_uncook',
        description="Path where you exported the tga files."
    )
    
    tex_mod_uncook_path: StringProperty(
        name="(optional) Uncook Path modded TEXTURES (.tga)",
        subtype='DIR_PATH',
        default='E:\\w3.modding\\modkit\\modZOldWitcherArmour',
        description="(optional) Path where you exported the tga files from a mod."
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
        default='E:\\w3.modding\\radish-tools\\docs.speech\\enpc.w3speech-extracted_GOOD\\enpc.w3speech-extracted',
        description="Path where you extracted w3speech"
    )
    
    W3_OGG_PATH: StringProperty(
        name="Converted .wem files (.ogg)",
        subtype='DIR_PATH',
        default='F:\\voice_synth\\witcher\\speech\\ogg',
        description="Path with ogg files"
    )
    
    keep_lod_meshes: bpy.props.BoolProperty(name="Keep lod meshes", default = True)

    #importFacePoses
    def draw(self, context):
        layout = self.layout
        layout.label(text="Witcher 3 Mesh settings:")
        layout.prop(self, "keep_lod_meshes")
        layout.label(text="Witcher 3 Tools settings:")
        layout.prop(self, "uncook_path")
        layout.prop(self, "fbx_uncook_path")
        layout.prop(self, "tex_uncook_path")
        layout.prop(self, "W3_FOLIAGE_PATH")
        layout.prop(self, "W3_REDCLOTH_PATH")
        layout.prop(self, "W3_REDFUR_PATH")
        layout.prop(self, "W3_VOICE_PATH")
        layout.prop(self, "W3_OGG_PATH")

# class ButtonOperator(bpy.types.Operator):
#     """Tooltip"""
#     bl_idname = "object.import_scn_btn"
#     bl_label = "Import .scn"

#     def execute(self, context):
#         print("importing now!")
#         #import_scn.btn_import_SCN()
#         return {'FINISHED'}


def import_group(coll, uncook_path):
    for child in coll.children:
        if child.group_type and child.group_type == "LayerInfo":
            print("LOADING LEVEL "+child.name)
            if child.level_path:
                fdir =  os.path.join(uncook_path, child.level_path)
                if Path(fdir).exists():
                    levelFile = CR2W.CR2W_reader.load_w2l(fdir)
                    import_w2l.btn_import_W2L(levelFile)
                else:
                    print("Can't find level "+fdir)
    for child in coll.children:
        if child.group_type and child.group_type == "LayerGroup":
            print("LAYER_GROUP "+child.name)
            import_group(child, uncook_path)


class LOAD_LAYER_GROUP_ButtonOperator(bpy.types.Operator):
    """IMPORT_LAYER_ButtonOperator"""
    bl_idname = "object.load_this_layer"
    bl_label = "Load This LayerGroup"

    def execute(self, context):
        coll = context.collection
        if coll:
            #loop all child colls
            #if LayerInfo load level
            #if LayerGroup load group

            uncook_path = get_uncook_path(context)
            
            start_time = time.time()
            import_group(coll, uncook_path)
            logging.info(' Finished importing LayerGroup in %f seconds.', time.time() - start_time)
            #CLayerInfo
            #coll.level_path
            #coll.layerBuildTag
        return {'FINISHED'}

class LOAD_LEVEL_ButtonOperator(bpy.types.Operator):
    """IMPORT_LEVEL_ButtonOperator"""
    bl_idname = "object.load_this_level"
    bl_label = "Load This Level"

    # @classmethod
    # def poll(cls, context):
    #     return context.layer_collection is not None

    def execute(self, context):
        coll = context.collection
        if coll:
            #CLayerInfo
            #coll.level_path
            #coll.layerBuildTag
            uncook_path = get_uncook_path(context)
            fdir =  os.path.join(uncook_path, coll.level_path) 
            levelFile = CR2W.CR2W_reader.load_w2l(fdir)
            import_w2l.btn_import_W2L(levelFile)
        return {'FINISHED'}


def create_morph_and_driver(self, obj, mesh_bl_o, this_POSE):
    bpy.context.view_layer.objects.active =  mesh_bl_o
    apply_ret = bpy.ops.object.modifier_apply_as_shapekey(keep_modifier=True, modifier="Armature")

    if 'FINISHED' not in apply_ret:
        self.report({'ERROR'}, "Error on pplying modifier, Object: {0}, ShapeKey: {1}, apply modifier: {2}".format(mesh_bl_o.name, this_POSE.name, apply_ret))
        ret = False
    else:
        new_morph = mesh_bl_o.data.shape_keys.key_blocks[-1]
        new_morph.name = this_POSE.name # rename
        driver_curve = new_morph.driver_add("value")
        driver = driver_curve.driver
        channel = this_POSE.name
        driver.expression = channel
        var = driver.variables.get(channel)
        if var is None:
            var = driver.variables.new()
        var.type = "SINGLE_PROP"
        var.name = channel
        target = var.targets[0]
        target.id_type = "OBJECT"
        target.data_path = 'pose.bones["w3_face_poses"]["%s"]' % channel #'["%s"]' % channel
        target.id = obj # 

#! ------------------------------------------------------------------------
#!    Debug
#! ------------------------------------------------------------------------

def witcherui_add_redmorph(collection, item):
    for el in collection:
        if el.name == item[0] and el.path == item[1] and el.type == item[2]:
            return

    add_item = collection.add()
    add_item.name = item[0]
    add_item.path = item[1]
    add_item.type = item[2]
    return

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
                if constraint.target.name == mimicFace and arm_obj.name not in face_arms:
                    face_arms.append(arm_obj.name)

    for mesh_obj in all_objs:
        if mesh_obj.type != 'MESH':
            continue
        for modifier in mesh_obj.modifiers:
            if modifier.type != 'ARMATURE':
                continue
            if modifier.object.name in face_arms and mesh_obj.name not in face_meshes:
                face_meshes.append(mesh_obj.name)
    return (face_meshes, face_arms)


from mathutils import Vector
class ButtonActiveDebug(bpy.types.Operator):
    """Select the CMovingPhysicalAgentComponent and press this button. 
    It will take a while but should create all the face morphs and add a bone to control them."""
    bl_idname = "object.active_debug"
    bl_label = "Active Debug"

    def execute(self, context):
        main_obj = bpy.context.active_object
        
        bpy.ops.object.mode_set(mode='EDIT')
        arm_obj = main_obj
        bl_ctrl_bone = arm_obj.data.edit_bones.get("w3_face_poses")
        if bl_ctrl_bone == None:
            bl_ctrl_bone = arm_obj.data.edit_bones.new("w3_face_poses")
            bl_ctrl_bone.parent = None
            bl_ctrl_bone.use_deform = False
            bl_ctrl_bone.head = Vector([-0.5, 0, 1.5])
            bl_ctrl_bone.tail = Vector([0, 0, 0.2]) + bl_ctrl_bone.head
        bpy.ops.object.mode_set(mode='OBJECT')

        # for c in obj.constraints:
        #     print(f"{c.name}: {c.type}")

        #cake = bpy.data.objects["shani:CMimicComponent12_ARM"].pose.bones["torso3"].constraints["Child Of"]
        #cake = bpy.data.objects["shani:CMimicComponent12_ARM"].pose.bones["torso3"].constraints["torso3 to torso3"]
        #dawd = "23"
        # for o in bpy.data.objects:
        #     for c in o.constraints:
        #         print(f"{c.name}: {c.type}")
        #! LIPSYNC
        # anim = load_lipsync_file(r"E:\w3.modding\radish-tools\docs.speech\enpc.w3speech-extracted_GOOD\enpc.w3speech-extracted\0000498215.cr2w")
        # set_entry = CSkeletalAnimationSetEntry()
        # set_entry.animation = anim
        # import_anims.import_anim(context, "cake", set_entry)
        
        #! FACE POSES
        #fileName = os.path.join(get_uncook_path(context), "characters\\models\\main_npc\\triss\\h_01_wa__triss\\h_01_wa__triss.w3fac")
        #fileName = r'E:\\w3.modding\\modkit\\r4data\\dlc\\ep1\\data\\characters\\models\\secondary_npc\\shani\\h_01_wa__shani\\h_01_wa__shani.w3fac'
        #fileName = r"D:\Witcher_uncooked_clean\raw_ent_TEST\dlc\ep1\data\characters\models\secondary_npc\shani\h_01_wa__shani\h_01_wa__shani.w3fac.json"
        #fileName = r"E:\w3.modding\modkit\r4data\characters\models\geralt\head\model\h_01_mg__geralt.w3fac"
        fileName = main_obj['mimicFaceFile']
        
        faceData = import_rig.loadFaceFile(fileName)
        # for pose in faceData.mimicPoses:
        #     for bone in pose.animBuffer.bones:
        #         for rot in bone.rotationFramesQuat:
        #             rot.W = rot.W
        #             rot.X = rot.X
        #             rot.Y = rot.Y
        #             rot.Z = rot.Z
        #main_arm_obj = bpy.context.scene.objects["shani:_ARM"]
        
        rig_settings = main_obj.data.witcherui_RigSettings
        rig_settings.model_armature_object = main_obj
        
        for pose in faceData.mimicPoses:
            # if pose.name != "default":
            #     continue
            bl_ctrl_bone_pose = main_obj.pose.bones['w3_face_poses']
            bl_ctrl_bone_pose[pose.name] = 0.0
            property_manager = bl_ctrl_bone_pose.id_properties_ui(pose.name)
            property_manager.update(min = 0., max = 1)
            witcherui_add_redmorph(rig_settings.witcher_morphs_list, [pose.name, pose.name, 4])
            
            #this_POSE = faceData.mimicPoses[2]
            this_POSE = pose

            face_rig = bpy.context.scene.objects[main_obj['mimicFace']]
            bpy.context.view_layer.objects.active = face_rig
            (face_meshes, face_arms ) = get_face_meshs(main_obj['mimicFace']) #get_face_meshs(obj.name) #get_face_meshs(obj['mimicFace'])
            #return {'FINISHED'}
            this_POSE.SkeletalAnimationType = "SAT_Additive"
            set_entry = CSkeletalAnimationSetEntry()
            set_entry.animation = this_POSE
            for pb in face_rig.pose.bones:
                pb.matrix_basis.identity()
            #bpy.ops.object.mode_set(mode='POSE', toggle=False)
            #bpy.ops.pose.transforms_clear()
            import_anims.import_anim(context, "cake", set_entry, facePose=True, override_select=[face_rig])
            
            
            context.scene.frame_current = 0
            
            #!GET MESH OBJECTS FOR THIS AND APPLY SHAPE KEYS
            # eyes_mouth_obj =bpy.context.scene.objects["he_01_wa__shani_Mesh_lod0"] 
            # mesh_bl_o = bpy.context.scene.objects["h_01_wa__shani_Mesh_lod0"]
            
            for face_mesh in face_meshes:
                the_mesh = bpy.context.scene.objects[face_mesh]
                create_morph_and_driver(self, main_obj, the_mesh, this_POSE)
                # create_morph_and_driver(self, obj, mesh_bl_o, faceData)
                # create_morph_and_driver(self, obj, eyes_mouth_obj, faceData)

            #! RETURN ACTIVE OBJECT
            bpy.context.view_layer.objects.active = face_rig
            for pb in face_rig.pose.bones:
                pb.matrix_basis.identity()
            face_rig.animation_data.action = None
            
        #! RETURN MAIN OBJECT
        bpy.context.view_layer.objects.active = main_obj

        bpy.ops.object.mode_set(mode='POSE')
        for face_mesh in face_meshes:
            the_mesh = bpy.context.scene.objects[face_mesh]
            if the_mesh.data.shape_keys.animation_data is not None:
                for oDrv in the_mesh.data.shape_keys.animation_data.drivers:
                    driver = oDrv.driver
                    driver.expression += " "
                    driver.expression = driver.expression[:-1]
        return {'FINISHED'}

    def __del__(self):
        pass
        #bpy.ops.object.modifier_apply_as_shapekey(keep_modifier=True, modifier="Armature")
        #print("End")

class ButtonOperatorW2L(bpy.types.Operator, ImportHelper):
    """Load Witcher 3 Level"""
    bl_idname = "object.import_w2l_btn"
    bl_label = "Import .w2l"
    filename_ext = ".w2l"

    # @classmethod
    # def poll(cls, context):
    #     # Always can import
    #     return True

    def execute(self, context):
        print("importing now!")
        fdir = self.filepath

        start_time = time.time()
        if fdir.endswith(".w2l"):
            levelFile = CR2W.CR2W_reader.load_w2l(fdir)
            import_w2l.btn_import_W2L(levelFile)
        else:
            fdir = os.path.join(get_uncook_path(context),"levels\\prolog_village\\surroundings\\architecture.w2l")
            levelFile = CR2W.CR2W_reader.load_w2l(fdir)
        logging.info(' Finished importing level in %f seconds.', time.time() - start_time)

        # if status == '{NONE}':
        #     # self.report({'DEBUG'}, "DEBUG File Format unrecognized")
        #     # self.report({'INFO'}, "INFO File Format unrecognized")
        #     # self.report({'OPERATOR'}, "OPERATOR File Format unrecognized")
        #     # self.report({'WARNING'}, "WARNING File Format unrecognized")
        #     # self.report({'ERROR'}, "ERROR File Format unrecognized")
        #     self.report({'ERROR'}, "ERROR File Format unrecognized")
        return {'FINISHED'}

class ButtonOperatorW2W(bpy.types.Operator, ImportHelper):
    """Load Witcher 3 Level"""
    bl_idname = "object.import_w2w_btn"
    bl_label = "Import .w2w"
    filename_ext = ".w2w"

    # @classmethod
    # def poll(cls, context):
    #     # Always can import
    #     return True

    def execute(self, context):
        print("importing now!")
        fdir = self.filepath
        #fdir = r"E:\w3.modding\modkit\r4data\dlc\bob\data\levels\bob\bob.w2w"
        #fdir = r"E:\w3.modding\modkit\r4data_unbundled\levels\prolog_village\prolog_village.w2w"
        #fdir = r"E:\w3.modding\modkit\r4data\levels\kaer_morhen\kaer_morhen.w2w"
        #fdir = r"E:\w3.modding\modkit\r4data\levels\novigrad\novigrad.w2w"
        worldFile = CR2W.CR2W_reader.load_w2w(fdir)
        # print(worldFile)

        import_w2w.btn_import_w2w(worldFile)
        #import_w2w.SetupNodeDataWorld(worldFile)
        #import_w2w.SetupListFromNodeData()
        return {'FINISHED'}

class ButtonOperatorw2ent(bpy.types.Operator, ImportHelper):
    """Load Witcher 3 Level"""
    bl_idname = "object.import_w2ent_btn"
    bl_label = "Import .w2ent"
    filename_ext = ".w2ent, flyr"
    def execute(self, context):
        print("importing now!")
        fdir = self.filepath
        #fdir = r"E:\w3.modding\modkit\r4data\items\weapons\swords\witcher_steel_scabbards\witcher_steel_bear_scabbard.w2ent"
        #fdir = r"E:\w3.modding\modkit\r4data\items\weapons\swords\witcher_silver_scabbards\witcher_silver_bear_scabbard.w2ent"
        #fdir = r"E:\w3.modding\modkit\r4data\dlc\bob\data\items\weapons\swords\witcher_steel_swords\witcher_steel_viper_ep2_sword_lvl4.w2ent"
        #fdir = r"E:\w3.modding\modkit\r4data\items\weapons\swords\silver_swords\silver_sword_lvl1.w2ent"
        #fdir = r"E:\w3.modding\modkit\r4data\characters\models\main_npc\ciri\l_01_wa__lingerie_ciri.w2ent"
        ext = file_helpers.getFilenameType(fdir)
        if ext == ".flyr":
            foliage = CR2W.CR2W_reader.load_foliage(fdir)
            import_w2l.btn_import_w2ent(foliage)
        elif ext == ".w2ent":
            #entity = CR2W.CR2W_reader.load_entity(r"E:\w3.modding\modkit\r4data\environment\decorations\light_sources\hanging_lamp\hanging_lantern_red.w2ent")
            #entity = CR2W.CR2W_reader.load_entity(r"E:\w3.modding\modkit\r4data\environment\decorations\light_sources\complex\candelabra_standing_complex.w2ent")
            #entity = CR2W.CR2W_reader.load_entity(r"E:\w3.modding\modkit\r4data\gameplay\containers\_container_definitions\_unique_containers\_chest_fisherman.w2ent")
 
            entity = CR2W.CR2W_reader.load_entity(fdir)
            import_w2l.btn_import_w2ent(entity)
        else:
            return {'ERROR'}
        return {'FINISHED'}

class ButtonOperatorAddConstraints(bpy.types.Operator):
    """Add Constraints"""
    bl_idname = "object.add_constraints"
    bl_label = "Add Constraints"
    bl_description = "Object Mode. Create bone constraints based on same bone names or r_weapon/l_weapon bones. Select Armature then Ctrl+Select Armature you want to attach to it"
    action: StringProperty(default="default")
    def execute(self, context):
        scene = context.scene
        action = self.action
        if action == "add_const":
            constrain_util.do_it()
        elif action == "attach_r_weapon":
            constrain_util.attach_weapon("r_weapon")
        elif action == "attach_l_weapon":
            constrain_util.attach_weapon("l_weapon")
        return {'FINISHED'}

class ButtonOperatorImportW2RigJson(bpy.types.Operator):
    """Import W2 rig Json"""
    bl_idname = "object.import_w2_rig_json"
    bl_label = "W2 rig Json"
    def execute(self, context):
        import_rig.start_rig_import(context)
        return {'FINISHED'}

class ButtonOperatorExportW2RigJson(bpy.types.Operator):
    """export W2 rig Json"""
    bl_idname = "object.export_w2_rig_json"
    bl_label = "W2 rig Json"
    def execute(self, context):
        import_rig.export_w3_rig(context, r"F:\RE3R_MODS\Blender_Scripts\io_import_w2l\woman_base.w2rig.json")
        return {'FINISHED'}
    
class ButtonOperatorExportW2AnimJson(bpy.types.Operator):
    """export W2 rig Json"""
    bl_idname = "object.export_w2_anim_json"
    bl_label = "W2 rig Json"
    def execute(self, context):
        #export_anims.export_w3_anim(context, r"F:\RE3R_MODS\Blender_Scripts\io_import_w2l\test.w2anim.json")
        return {'FINISHED'}
#----------------------------------------------------------
#   Panels
#----------------------------------------------------------

class WITCH_PT_Base:
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Witcher'
    bl_context = ''#{'objectmode', 'posemode'}
    #bl_options = {'DEFAULT_CLOSED'}


class _XpsPanels():
    """All XPS panel inherit from this."""

    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Witcher'
    bl_context = ''#'objectmode'

#----------------------------------------------------------
#   Utilities panel
#----------------------------------------------------------

class WITCH_PT_Utils(_XpsPanels, bpy.types.Panel):
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
                row.operator(LOAD_LEVEL_ButtonOperator.bl_idname, text="Load This Level", icon='CUBE')

            #CLayerGroup
            if coll.group_type and coll.group_type == "LayerGroup":
                row = layout.row()
                row.operator(LOAD_LAYER_GROUP_ButtonOperator.bl_idname, text="Load This LayerGroup", icon='CUBE')
        else:
            box.label(text = "No active collection")
            



class CustomPanel(_XpsPanels, bpy.types.Panel):
    bl_idname = "OBJECT_PT_w2l"
    bl_label = "Witcher 3 Tools"

    def draw(self, context):
        layout = self.layout

        obj = context.object
        row = layout.row()
        row.operator(ButtonActiveDebug.bl_idname, text="(load face morphs) Active Debug", icon='SPHERE')
        self.layout.label(text = "Map")
        row = layout.row()
        
        op = row.operator(ButtonOperatorW2L.bl_idname, text="Import .w2l", icon='SPHERE')
        op.filepath = os.path.join(get_uncook_path(context),"levels\\")

        row = layout.row()
        op = row.operator(ButtonOperatorW2W.bl_idname, text="Import .w2w", icon='SPHERE')
        op.filepath = get_uncook_path(context)+"\\levels\\"
        
        row = layout.row()
        row.operator(LOAD_TEXARRAY_ButtonOperator.bl_idname, text="Import texarray", icon='SPHERE')

        self.layout.label(text = "Entity")
        row = layout.row()
        op = row.operator(ButtonOperatorw2ent.bl_idname, text="Import .w2ent", icon='SPHERE')
        op.filepath = get_uncook_path(context)
        #materials

        self.layout.label(text = "Animation")
        #anims
        row = layout.row()
        row.operator(ButtonOperatorAddConstraints.bl_idname, text="Add Constraints", icon='SPHERE').action = "add_const"
        
        row = layout.row()
        row.operator(ButtonOperatorAddConstraints.bl_idname, text="Attach to r_weapon", icon='SPHERE').action = "attach_r_weapon"
        row = layout.row()
        row.operator(ButtonOperatorAddConstraints.bl_idname, text="Attach to l_weapon", icon='SPHERE').action = "attach_l_weapon"
        
        row = layout.row()
        row.operator(ButtonOperatorImportW2RigJson.bl_idname, text="Import .w2rig.json", icon='SPHERE')
        
        row = layout.row()
        row.operator(ButtonOperatorExportW2RigJson.bl_idname, text="Export .w2rig.json", icon='SPHERE')
        
        
        #row = layout.prop(context.scene, "anim_export_name")
        row = layout.row()
        row.operator(ButtonOperatorExportW2AnimJson.bl_idname, text="Export .w2anims.json", icon='SPHERE')


        #self.layout.separator()
        # scn = context.scene
        # layout = self.layout
        
        # row = layout.row()
        # row.template_list(
        #     "MYLISTTREEITEM_UL_basic",
        #     "",
        #     scn,
        #     "myListTree",
        #     scn,
        #     "myListTree_index",
        #     sort_lock = True
        #     )
            
        #grid = layout.grid_flow( columns = 2 )
        
        # grid.operator("object.mylisttree_debug", text="Reset").action = "reset3"
        # grid.operator("object.mylisttree_debug", text="Clear").action = "clear"
        # grid.operator("object.mylisttree_debug", text="Print").action = "print"
        #grid.operator("object.mylisttree_debug", text="Load Group").action = "group"
        #grid.operator("object.mylisttree_debug", text="Load w2l").action = "level"
    # def draw(self, context):
    #     layout = self.layout
        
    #     obj = context.object
    #     col = layout.column()

    #     col.label(text='Import:')
    #     # c = col.column()
    #     r = col.row(align=True)
    #     r1c1 = r.column(align=True)
    #     r1c1.operator(ButtonOperator.bl_idname, text="Import", icon='None')
    #     col = layout.column()

class LOAD_TEXARRAY_ButtonOperator(bpy.types.Operator):
    """LOAD_TEXARRAY_ButtonOperator"""
    bl_idname = "object.load_this_texarray"
    bl_label = "LOAD_TEXARRAY"

    def execute(self, context):
        print("Importing Material")
        fileName = r"E:\w3.modding\w3terrain-extract-v2020-03-30\terrain.json"
        import_texarray.start_import(fileName)
        return {'FINISHED'}

# class TexArrayPanel(_XpsPanels, bpy.types.Panel):
#     bl_idname = "OBJECT_PT_W3_TEXARRAY"
#     bl_label = "TEXARRAY"

#     def draw(self, context):
#         layout = self.layout

#         obj = context.object

#         row = layout.row()
#         row.operator(LOAD_TEXARRAY_ButtonOperator.bl_idname, text="Import texarray", icon='SPHERE')


from bpy.utils import (register_class, unregister_class)

_classes = [
    #ent_import
    ButtonActiveDebug,
    ButtonOperatorW2L,
    ButtonOperatorW2W,
    ButtonOperatorw2ent,
    #anims
    ButtonOperatorAddConstraints,
    ButtonOperatorImportW2RigJson,
    ButtonOperatorExportW2RigJson,
    ButtonOperatorExportW2AnimJson,
    #panel
    CustomPanel,

    WITCH_PT_Utils,
    LOAD_LEVEL_ButtonOperator,
    LOAD_LAYER_GROUP_ButtonOperator,

    LOAD_TEXARRAY_ButtonOperator
    # TexArrayPanel
]


# @persistent
# def load_fonts(scene):
#     import_w2w.SetupNodeData()
#     import_w2w.SetupListFromNodeData()

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
    ui_anims.register()
    ui_entity.register()
    ui_morphs.register()
    ui_voice.register()
    ui_mimics.register()
    ui_anims_list.register()
    # filter_list.register()
    for cls in _classes:
        register_class(cls)
    # bpy.types.Scene.temp = StringProperty(
    #     name = "temp"
    # )
    #import_w2w.register()
    #bpy.app.handlers.load_post.append(load_fonts)
    #import_w2w.SetupNodeData()
    #import_w2w.SetupListFromNodeData()

def unregister():
    bpy.utils.unregister_class(Witcher3AddonPrefs)
    del bpy.types.Object.template
    del bpy.types.Object.entity_type

    del bpy.types.Collection.level_path
    del bpy.types.Collection.layerBuildTag
    del bpy.types.Collection.world_path
    del bpy.types.Collection.group_type
    ui_anims.unregister()
    ui_entity.unregister()
    ui_morphs.unregister()
    ui_voice.unregister()
    ui_mimics.unregister()
    ui_anims_list.unregister()
    #filter_list.unregister()
    for cls in _classes:
        unregister_class(cls)
    #import_w2w.unregister()
    #bpy.app.handlers.load_post.remove(load_fonts)

