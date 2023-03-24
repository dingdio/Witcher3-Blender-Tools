import bpy
from typing import Tuple
from bpy.props import StringProperty, BoolProperty
from mathutils import Vector

from io_import_w2l.ui.ui_utils import WITCH_PT_Base
from io_import_w2l.CR2W.CR2W_types import dotdict
from io_import_w2l.CR2W.w3_types import CSkeletalAnimationSetEntry
from io_import_w2l.CR2W.dc_anims import load_lipsync_file
from io_import_w2l.importers import import_anims
from io_import_w2l.importers import import_rig
from bpy.types import PropertyGroup
from bpy.props import IntProperty, StringProperty, CollectionProperty, BoolProperty

class witcherui_redmorph(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty(name = "Name")
    path: bpy.props.StringProperty(name = "Path")
    type: bpy.props.IntProperty(name = "Type")
    value: bpy.props.FloatProperty(name = "value")

bpy.utils.register_class(witcherui_redmorph)

class ListItemBone(PropertyGroup):
    """."""
    name: StringProperty(
           name="Bone",
           description="Name of bone",
           default="")
bpy.utils.register_class(ListItemBone)


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

bpy.utils.register_class(ListItemAnimset)
bpy.utils.register_class(ListItemApp)

class witcherui_MeshSettings(bpy.types.PropertyGroup):
    lod_level: bpy.props.IntProperty(default = 0)
    distance: bpy.props.FloatProperty(default = 0)
    mat_id: bpy.props.IntProperty(default = 0)
    
    autohideDistance: bpy.props.IntProperty(default = 100)
    isTwoSided: bpy.props.BoolProperty(default = False)
    useExtraStreams: bpy.props.BoolProperty(default = True)
    mergeInGlobalShadowMesh: bpy.props.BoolProperty(default = True)
    entityProxy: bpy.props.BoolProperty(default = False)
    
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

class witcherui_RigSettings(bpy.types.PropertyGroup):
    model_name: bpy.props.StringProperty(default = "",
                        name = "Model name",
                        description = "Model name")
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
    
    #Tracks
    witcher_tracks_list: bpy.props.CollectionProperty(name = "Tracks",
                        type=witcherui_redmorph)
    witcher_tracks_collapse: bpy.props.BoolProperty(default = True)

    #apperance list
    app_list : CollectionProperty(type = ListItemApp)
    app_list_index : IntProperty(name = "Index for app_list",
                                             default = 0)
    
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
    
    do_import_redcloth : BoolProperty(
                                            name="Include redcloth",
                                            description="Import redcloth with apperances",
                                            default=1)
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



bpy.utils.register_class(witcherui_RigSettings)
bpy.types.Armature.witcherui_RigSettings = bpy.props.PointerProperty(type = witcherui_RigSettings)

bpy.utils.register_class(witcherui_MeshSettings)
bpy.types.Object.witcherui_MeshSettings = bpy.props.PointerProperty(type = witcherui_MeshSettings)

class WITCH_PT_WitcherMorphs(WITCH_PT_Base, bpy.types.Panel):
    bl_parent_id = "WITCH_PT_ENTITY_Panel"
    bl_idname = "WITCH_PT_WitcherMorphs"
    bl_label = "Morphs"
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

        if ob and ob.type == "ARMATURE" and "CMovingPhysicalAgentComponent" in ob.name:
            main_arm_obj = ob

            main_arm_obj = bpy.context.active_object
            rig_settings = main_arm_obj.data.witcherui_RigSettings
            layout = self.layout
            if rig_settings.witcher_face_morphs:
                box = layout.box()
                row = box.row(align=False)
                row.prop(rig_settings, "witcher_morphs_collapse", icon="TRIA_DOWN" if not rig_settings.witcher_morphs_collapse else "TRIA_RIGHT", icon_only=True, emboss=False)
                body_morphs = [x for x in rig_settings.witcher_morphs_list if x.type == 4] #and self.morph_filter(x, rig_settings)]
                row.label(text="Face (" + str(len(body_morphs)) + ")")

                if not rig_settings.witcher_morphs_collapse:
                    the_data = rig_settings.model_armature_object.pose.bones["w3_face_poses"]

                    for morph in body_morphs:
                        if hasattr(the_data,'[\"' + morph.path + '\"]'):
                            box.prop(the_data, '[\"' + morph.path + '\"]', text = morph.name)

                        else:
                            pass

#import bpy

import io
from contextlib import redirect_stdout, redirect_stderr
import os
import sys

def create_morph_and_driver(self, obj, mesh_bl_o, this_POSE):
    bpy.context.view_layer.objects.active =  mesh_bl_o
    
    apply_ret = bpy.ops.object.modifier_apply_as_shapekey(keep_modifier=True, modifier="Armature", report=False)

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
        target.id = obj

def witcherui_add_redmorph(collection, item, value = 0.0):
    for el in collection:
        if el.name == item[0] and el.path == item[1] and el.type == item[2]:
            return

    add_item = collection.add()
    add_item.name = item[0]
    add_item.path = item[1]
    add_item.type = item[2]
    add_item.value = value
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


from mathutils import Euler
from math import radians
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

class WITCH_OT_morphs(bpy.types.Operator):
    """Must load a character in the Characher Appearances panel first. Select the CMovingPhysicalAgentComponent and press this button. It may take a while but should create all the face morphs and add a pose bone to control them"""
    bl_idname = "witcher.load_face_morphs"
    bl_label = "Active Debug"

    def execute(self, context):
        main_obj = bpy.context.active_object
        
        save_world = main_obj.matrix_world
        save_local = main_obj.matrix_local
        save_basis =main_obj.matrix_basis
        save_location = main_obj.location
        save_scale = main_obj.scale
        reset_transforms(main_obj)
        current_pose_position = main_obj.data.pose_position
        main_obj.data.pose_position = "REST"
            
        

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

        fileName = main_obj['mimicFaceFile']

        faceData = import_rig.loadFaceFile(fileName)

        rig_settings = main_obj.data.witcherui_RigSettings
        rig_settings.model_armature_object = main_obj

        import time
        start_time = time.time()
        
        suppress = False
        
        #!suppress
        if suppress:
            old = os.dup(sys.stdout.fileno())
            devnull = open(os.devnull, 'w')
            os.dup2(devnull.fileno(), sys.stdout.fileno())

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
            import_anims.import_anim(context, "inported", set_entry, facePose=True, override_select=[face_rig])


            context.scene.frame_current = 0
            #!GET MESH OBJECTS FOR THIS AND APPLY SHAPE KEYS

            for face_mesh in face_meshes:
                the_mesh = bpy.context.scene.objects[face_mesh]
                create_morph_and_driver(self, main_obj, the_mesh, this_POSE)

            #! RETURN ACTIVE OBJECT
            bpy.context.view_layer.objects.active = face_rig
            for pb in face_rig.pose.bones:
                pb.matrix_basis.identity()
            face_rig.animation_data.action = None

        #!stop suppress
        if suppress:
            sys.stdout.flush()
            os.dup2(old, sys.stdout.fileno())
            os.close(old)
        time_taken = time.time() - start_time
        print(f'Loaded morphs in {time_taken} seconds.')

        #! RETURN MAIN OBJECT
        bpy.context.view_layer.objects.active = main_obj

        bpy.ops.object.mode_set(mode='POSE')
        for face_mesh in face_meshes:
            the_mesh = bpy.context.scene.objects[face_mesh]
            if the_mesh.data.shape_keys and the_mesh.data.shape_keys.animation_data is not None:
                for oDrv in the_mesh.data.shape_keys.animation_data.drivers:
                    driver = oDrv.driver
                    driver.expression += " "
                    driver.expression = driver.expression[:-1]
                    
        
        bpy.ops.object.mode_set(mode='OBJECT')
        #bpy.context.view_layer.objects.active = main_obj
        
        
        main_obj.matrix_world = save_world
        main_obj.matrix_local = save_local
        main_obj.matrix_basis = save_basis
        main_obj.location = save_location
        main_obj.scale = save_scale
        main_obj.data.pose_position = current_pose_position
            
        return {'FINISHED'}

    def __del__(self):
        pass
        #bpy.ops.object.modifier_apply_as_shapekey(keep_modifier=True, modifier="Armature")

from bpy.utils import (register_class, unregister_class)

_classes = [
    WITCH_PT_WitcherMorphs,
]


def register():
    for cls in _classes:
        register_class(cls)

def unregister():
    for cls in _classes:
        unregister_class(cls)

