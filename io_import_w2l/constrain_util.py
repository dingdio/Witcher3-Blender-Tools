import bpy
import sys
import os
from mathutils import Matrix
from io_import_w2l import file_helpers

VERBOSE = True

#add copytransforms on def bones
def CreateConstraints(arm_parent, arm_child):
    #switch to pose mode and find pose bones    
    bpy.ops.object.mode_set(mode='POSE', toggle=False)

    for tgt_parent_bone in arm_parent.pose.bones:
        tgt_child_bone = False
        p_bone_name = file_helpers.rm_ns(tgt_parent_bone.name)
        print(p_bone_name)

        for cBone in arm_child.pose.bones:
            c_bone_name = file_helpers.rm_ns(cBone.name)
            if c_bone_name == p_bone_name:
                tgt_child_bone = cBone
            # if 'placer_'+c_bone_name == p_bone_name:
            #     tgt_child_bone = cBone
        if tgt_child_bone:
            if VERBOSE:
                print(tgt_child_bone)
                print(tgt_parent_bone)


            # for cons in tgt_child_bone.constraints:
            #     tgt_child_bone.constraints.remove(cons)
            child_of = tgt_child_bone.constraints.new('CHILD_OF')
            child_of.name = tgt_parent_bone.name + " to " + tgt_child_bone.name
            child_of.target = arm_parent
            child_of.subtarget = tgt_parent_bone.name
            #child_of.influence = 0.5

            # copyTransform = tgt_child_bone.constraints.new('COPY_TRANSFORMS')
            # copyTransform.name = tgt_parent_bone.name + " to " + tgt_child_bone.name
            # copyTransform.target = arm_parent
            # copyTransform.subtarget = tgt_parent_bone.name

            # child_of.use_location_x = False
            # child_of.use_location_y = False
            # child_of.use_location_z = False
            # child_of.use_scale_x = False
            # child_of.use_scale_y = False
            # child_of.use_scale_z = False

            arm_child.data.bones.active = arm_child.data.bones[tgt_child_bone.name]


            bpy.ops.object.mode_set(mode='EDIT', toggle=False)
            #bone_to_edit = arm_child.data.bones[tgt_child_bone.name]
            #bone_to_edit.parent_clear(type='CLEAR')
            #bpy.context.active_bone.use_local_location = False
            #bpy.context.active_bone.use_inherit_rotation = False
            bpy.context.active_bone.parent = None
            bpy.ops.object.mode_set(mode='POSE', toggle=False)

            bpy.ops.constraint.childof_set_inverse(constraint=tgt_parent_bone.name + " to " + tgt_child_bone.name, owner='BONE')
            #bpy.ops.object.mode_set(mode='POSE', toggle=False)
            # context_py = bpy.context.copy()
            # context_py["constraint"] = child_of
            # arm_child.data.bones.active = tgt_child_bone.bone
            # bpy.ops.constraint.childof_set_inverse(context_py, constraint="Child Of", owner='BONE')

    bpy.ops.object.mode_set(mode='OBJECT', toggle=False)
    return

def constrain_w3_rig(arm_parent, arm_child, mo=False):
    print("Creating constraints...")
    CreateConstraints(arm_parent, arm_child)   

def CreateConstraints2(arm_parent, arm_child):
    bpy.ops.object.mode_set(mode='POSE', toggle=False)
    for tgt_parent_bone in arm_parent.pose.bones:
        tgt_child_bone = False
        p_bone_name = file_helpers.rm_ns(tgt_parent_bone.name)
        print(p_bone_name)

        for cBone in arm_child.pose.bones:
            c_bone_name = file_helpers.rm_ns(cBone.name)
            if c_bone_name == p_bone_name:
                tgt_child_bone = cBone
        #some positions of the face rig of a character don't match
        CHILD_OF_list = ['ears', 'jaw', 'tongue1', 'tongue2', 'tongue_right_side', 'tongue_left_side','left_eye', 'right_eye'
                         ,'right_chick1','left_chick1']
        #if tgt_child_bone and "ears" not in tgt_child_bone.name and not "eye" == tgt_child_bone.name and not "jaw" == tgt_child_bone.name:
        if tgt_child_bone and tgt_child_bone.name not in CHILD_OF_list:
            for cons in tgt_child_bone.constraints:
                tgt_child_bone.constraints.remove(cons)
            copyTransform = tgt_child_bone.constraints.new('COPY_TRANSFORMS')
            copyTransform.name = tgt_parent_bone.name + " to " + tgt_child_bone.name
            copyTransform.target = arm_parent
            copyTransform.subtarget = tgt_parent_bone.name
            
            #! TEMP STUFF FOR ADDING IK
            # copyTransform.target_space = "WORLD"
            # copyTransform.owner_space = "WORLD"
            # copyTransform.target_space = "LOCAL_WITH_PARENT"
            # copyTransform.owner_space = "LOCAL_WITH_PARENT"
            
            # copyRotation = tgt_child_bone.constraints.new('COPY_ROTATION')
            # copyRotation.name = tgt_parent_bone.name + " to " + tgt_child_bone.name
            # copyRotation.target = arm_parent
            # copyRotation.subtarget = tgt_parent_bone.name
            # copyRotation.mix_mode = "REPLACE"
            # copyRotation.target_space = "LOCAL_OWNER_ORIENT"
            # copyRotation.owner_space = "LOCAL"
            
            
            # copyLocation = tgt_child_bone.constraints.new('COPY_LOCATION')
            # copyLocation.name = tgt_parent_bone.name + " to " + tgt_child_bone.name
            # copyLocation.target = arm_parent
            # copyLocation.subtarget = tgt_parent_bone.name
            #! TEMP STUFF END
            
        elif tgt_child_bone:
            child_of = tgt_child_bone.constraints.new('CHILD_OF')
            child_of.name = tgt_parent_bone.name + " to " + tgt_child_bone.name
            child_of.target = arm_parent
            child_of.subtarget = tgt_parent_bone.name
            # if (tgt_child_bone.name == "torso3"
            #     or tgt_child_bone.name == "l_shoulder"
            #     or tgt_child_bone.name == "r_shoulder"
            #     or tgt_child_bone.name == "neck"
            #     or tgt_child_bone.name == "placer_thyroid"):
            #     child_of.inverse_matrix = Matrix()
            # else:
            #     child_of.inverse_matrix = Matrix() @ tgt_child_bone.matrix.inverted()

            # if tgt_child_bone.name == "torso3":
            #     child_of.inverse_matrix = Matrix()
                #bpy.ops.object.mode_set(mode='OBJECT', toggle=False)
                #return
                #ca ="adwa"
                #tgt_child_bone.matrix = arm_parent.matrix_parent_inverse @ tgt_parent_bone.matrix @ tgt_child_bone.matrix.inverted()
                #child_of.inverse_matrix = arm_parent.matrix_parent_inverse @ tgt_child_bone.matrix#.inverted()
                # for c in arm_child.constraints:
                #     print(f"{c.name}: {c.type}")
                #bpy.ops.constraint.childof_clear_inverse(bpy.context.copy(), constraint=tgt_parent_bone.name + " to " + tgt_child_bone.name, owner='BONE')
                #bpy.ops.object.mode_set(mode='OBJECT', toggle=False)
                #return
                #bpy.ops.constraint.childof_set_inverse(constraint=tgt_parent_bone.name + " to " + tgt_child_bone.name, owner='BONE')
            # arm_child.data.bones.active = arm_child.data.bones[tgt_child_bone.name]
            # bpy.ops.object.mode_set(mode='EDIT', toggle=False)
            # bpy.context.active_bone.parent = None
            # bpy.ops.object.mode_set(mode='POSE', toggle=False)
    bpy.ops.object.mode_set(mode='OBJECT', toggle=False)
    return {'FINISHED'}

def attach_weapon(p_bone_name = "r_weapon"):
    #bpy.data.objects['CMeshComponent14:Armature']
    arm_parent = False
    child = bpy.context.active_object

    selected_objs = [obj for obj in bpy.context.selected_objects if obj != bpy.context.active_object]

    for obj in selected_objs:
        if obj.type != 'ARMATURE':
            continue
        if not arm_parent:
            arm_parent = obj
            continue
    #p_bone_name = "r_weapon"
    print("Attaching item / weapon...")
    copyTransform = child.constraints.new('COPY_TRANSFORMS')
    copyTransform.name = p_bone_name + " to " + child.name
    copyTransform.target = arm_parent
    copyTransform.subtarget = p_bone_name

def do_it():
    #bpy.data.objects['CMeshComponent14:Armature']
    arm_parent = False
    arm_child = bpy.context.active_object

    selected_objs = [obj for obj in bpy.context.selected_objects if obj != bpy.context.active_object]

    for obj in selected_objs:
        if obj.type != 'ARMATURE':
            continue
        if not arm_parent:
            arm_parent = obj
            continue

    print("CAKE")
    # arm_parent = bpy.context.object
    # objects = bpy.context.selected_objects
    # arm = arm_parent.data

    # if bpy.context.object.type != 'ARMATURE':
    #     print("No Armature selected! Exiting script.")
    #     return {"ERROR"}
    print("Creating constraints...")
    cake = CreateConstraints2(arm_parent, arm_child)   
    print("Script finished")
    return {'FINISHED'}