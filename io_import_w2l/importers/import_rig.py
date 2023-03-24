import os
import json

from ..CR2W.CR2W_types import getCR2W
from ..CR2W.dc_skeleton import create_Skeleton, load_bin_face, load_bin_skeleton

from math import degrees
from math import radians
import bpy
from typing import List, Tuple
from pathlib import Path
from mathutils import Vector, Quaternion, Euler, Matrix
from io_import_w2l.CR2W.json_convert.CR2WJsonObject import CR2WJsonData, CR2WJsonScalar, getRigTemplate

from io_import_w2l import file_helpers
from io_import_w2l.CR2W import w3_types
from io_import_w2l.CR2W import read_json_w3
from io_import_w2l.w3_armature_constants import *
from io_import_w2l.ui.ui_morphs import witcherui_add_redmorph
from . import bpyutils
from io_import_w2l import get_uncook_path

def load_json_skeleton(filename):
    dirpath, file = os.path.split(filename)
    basename, ext = os.path.splitext(file)
    if ext.lower() in ('.json'):
        w3Data = read_json_w3.readCSkeleton(filename)
    else:
        w3Data = None

    return w3Data

from math import isfinite
def _VectorXYZ(v):
    return Vector(v).xyz if all(isfinite(n) for n in v) else Vector((0,0,0))

def fix_bone_tail_on_hierarchy(all_edit_bones, edit_bone=None):
    """Recursively go through a bone hierarchy and move the bone tails to useful positions.
    Requires the armature to be in edit mode to minimize mode switching.
    """

    if not edit_bone:
        edit_bone = all_edit_bones[0]
    edit_bone.tail = fix_bone_tail(all_edit_bones, edit_bone).copy()
    if edit_bone.tail == edit_bone.head:
        edit_bone.tail = Vector([0, 0, 0.01]) + edit_bone.head

    # Recursion over this bone's children.
    for c in edit_bone.children:
        fix_bone_tail_on_hierarchy(all_edit_bones, c)

def fix_bone_tail(all_edit_bones, eb) -> Vector:
    if "IK" in eb.name:
        return eb.tail
    assert len(all_edit_bones) > 0, "Armature needs to be in edit mode for fix_bone_tail()."

    # If a bone is in BONE_CONNECT, just move its tail to the bone specified in the dictionary.
    if eb.name in BONE_CONNECT:
        target = all_edit_bones.get(BONE_CONNECT[eb.name])
        if target:
            return target.head

    # For bones with children, we'll just connect the bone to the first child.
    if len(eb.children) > 0:
        return eb.children[0].head

    if eb.parent:
        # Special treatment for the children of some bones
        if eb.parent.name in ['head', 'jaw']:
            return eb.head + Vector((0, 0, .001))

        # Get the parent's head->tail vector
        parent_vec = eb.parent.tail - eb.parent.head
        if len(eb.parent.children) > 1:
            # If the bone has siblings, set the scale to an arbitrary amount relative to parent.
            scale = .001
            if 'tongue' in eb.name:
                scale = .001
            return eb.head + parent_vec.normalized() * scale	# TODO change this number to .05 if the apply_transforms() gets fixed.
        else:
            # If no siblings, just use the parents transforms.
            return eb.head + parent_vec

    # For orphan bones, do nothing.
    return eb.tail

def get_root_bones(arm_ob: bpy.types.Object) -> List[bpy.types.EditBone]:
    """Return all bones with no parent."""
    parentless = []
    for eb in arm_ob.data.edit_bones:
        if not eb.parent:
            parentless.append(eb)
    return parentless

import numpy as np

def create_armature(mdl: w3_types.CSkeleton, nsp="", scale=1.0, do_fix_tail = False, context = None, rotate_180 = False):
    if context == None:
        context = bpy.context
    PREFIX = nsp
    PREFIX = ""
    model_name =nsp#nsp.split(":")[0] #Path(mdl.header.name).stem
    armature = bpy.data.armatures.new(f"{model_name}_ARM_DATA")
    armature_obj = bpy.data.objects.new(f"{model_name}_ARM", armature)
    armature_obj.show_in_front = True
    context.collection.objects.link(armature_obj)

    armature_obj.select_set(True)
    context.view_layer.objects.active = armature_obj

    bpy.ops.object.mode_set(mode='EDIT')
    bl_bones = []
    for bone in mdl.bones:
        bl_bone = armature.edit_bones.new(PREFIX+bone.name)
        bl_bones.append(bl_bone)

    for bl_bone, s_bone in zip(bl_bones, mdl.bones):
        if s_bone.parentId != -1:
            bl_parent = bl_bones[s_bone.parentId]
            bl_bone.parent = bl_parent
        bl_bone.tail = (Vector([0, 0, 0.01]) * scale) + bl_bone.head

    bpy.ops.object.mode_set(mode='POSE')
    for se_bone in mdl.bones:
        bl_bone =  armature_obj.pose.bones.get(PREFIX+se_bone.name) #next((x for x in bl_bones if x.name == PREFIX+se_bone.name), None) 
        pos = Vector(se_bone.co) * scale
        rot = Quaternion((se_bone.ro_quat.W, se_bone.ro_quat.X, se_bone.ro_quat.Y, se_bone.ro_quat.Z)) #absolute_transforms[i]['rotation']

        mat = Matrix.Translation(pos) @ rot.to_matrix().to_4x4()
        bl_bone.matrix_basis.identity()

        bl_bone.matrix = bl_bone.parent.matrix @ mat if bl_bone.parent else mat

    bpy.ops.pose.armature_apply()
    
    if do_fix_tail: #!
        bpy.ops.object.mode_set(mode='EDIT')
        fix_bone_tail_on_hierarchy(armature.edit_bones)
    
    bpy.ops.object.mode_set(mode='OBJECT')

    if rotate_180:
        context.active_object.rotation_euler[2] = np.pi
    #context.collection.objects.unlink(armature_obj)
    return armature_obj



def start_rig_import(fileName = False, ns = "", do_fix_tail = False, context = None):
    ns = ns+":"
    #if not fileName:
        #fileName = r":\w3.modding\modkit\r4data\characters\models\geralt\scabbards\model\scabbards_crossbow.w2rig"
    print("Importing file: ", fileName)
    if fileName.endswith('.w2rig') or fileName.endswith('.w3dyng'):
        w3Data = load_bin_skeleton(fileName)
    elif fileName.endswith('.w2rig.json') or fileName.endswith('.w3dyng.json'):
        w3Data = load_json_skeleton(fileName)
    else:
        return {'ERROR'}
    arm = create_armature(w3Data, ns, 1.0, do_fix_tail, context)
    arm.data.witcherui_RigSettings.main_entity_skeleton = fileName
    
    tracks_bone:bpy.types.PoseBone = None
    if "Camera_Node" in arm.pose.bones:
        tracks_bone = arm.pose.bones["Camera_Node"]
    
    for bonedata in w3Data.bones:
        bone = arm.data.witcherui_RigSettings.bone_order_list.add()
        bone.name = bonedata.name
    if  tracks_bone:
        for track in w3Data.tracks:
            witcherui_add_redmorph(arm.data.witcherui_RigSettings.witcher_tracks_list, [track, track, 0])

    # for bone in arm.pose.bones:
    #     print(bone.name)
    #     if bone.name == ns+"pelvis":
    #         adw = "ddaw"
    return arm

def import_w3_rig(filename, ns="", do_fix_tail = False, context = None):
    #print("Importing file: ", filename)
    arm = start_rig_import(filename, ns, do_fix_tail, context)
    return arm

def get_ordered_bones(armature):
    ordered_bones = []
    bones_data = armature.data.bones
    if len(armature.data.witcherui_RigSettings.bone_order_list):
        for bone in armature.data.witcherui_RigSettings.bone_order_list:
            if armature.pose.bones.get(bone.name) is not None:
                ordered_bones.append(armature.data.bones[bone.name])

    for bone in bones_data:
        if bone not in ordered_bones:
            ordered_bones.append(bone)
    return ordered_bones

import copy
def export_w3_rig(context, filename):
    xpsBones = []
    selected_objects = set(context.selected_objects)

    for obj in selected_objects:
        if obj.type == 'ARMATURE':
            armature = obj
            break
    if armature:
        bones = armature.data.bones
        print('Exporting Armature', len(bones), 'Bones')
        # activebones = [bone for bone in bones if bone.layers[0]]

        activebones = bones

        names = []
        parentIdx = []
        positions = []
        rotations = []
        scales = []
        nbBones = len(activebones)
        output = list()
        
        ordered_bones = get_ordered_bones(armature)

        for bl_bone in ordered_bones:
            if bl_bone.parent:
                objectMatrix = bl_bone.parent.matrix_local.inverted()
            else:
                objectMatrix = armature.matrix_world.inverted()
            for idx, b in enumerate(ordered_bones):
                if bl_bone.name == b.name:
                    id = idx
                    break
            name = bl_bone.name
            names.append(file_helpers.rm_ns(name))
            co = objectMatrix @ bl_bone.head_local.xyz

            positions.append({
                            "X": round(co[0], 3),
                            "Y": round(co[1], 3),
                            "Z": round(co[2], 3)
                        })

            origRot = bl_bone.matrix.to_quaternion()
            rotations.append({
                            "X": round(origRot.x, 6),
                            "Y": round(origRot.y, 6),
                            "Z": round(origRot.z, 6),
                            "W": round(-origRot.w, 6)
                        })
            scales.append({
                            "X": 1.0,
                            "Y": 1.0,
                            "Z": 1.0,
                        })
            parentId = -1
            if bl_bone.parent:
                for idx, b in enumerate(ordered_bones):
                    if bl_bone.parent.name == b.name:
                        parentId = idx
                        break
            parentIdx.append(parentId)
            #xpsBone = w3_types.W3Bone(id, name, co, parentId)
            #xpsBones.append(xpsBone)
        output = {"nbBones": nbBones,
                    "names": names,
                    "parentIdx":parentIdx,
                    "positions":positions,
                    "rotations":rotations,
                    "scales":scales}
        with open(filename+'_OLD_FORMAT.json', "w") as file:
            file.write(json.dumps(output, indent=2, sort_keys=False))

        for rot in rotations:
            rot['W'] = -rot['W']
        rig:CR2WJsonData = getRigTemplate()
        skelly = rig._chunks['CSkeleton #0']
        bones = skelly._vars['bones']._elements
        rigdata = skelly._vars['rigdata']._elements
        bones_json = bones[0]
        rigdata_json = rigdata[0]
        
        new_bones_json = []
        new_rigdata_json = []
        for idx, name in enumerate(names):
            b_n = copy.deepcopy(bones_json)
            b_n._vars['name'] = CR2WJsonScalar(_type = 'StringAnsi', _value = name)
            b_n._vars['nameAsCName'] = CR2WJsonScalar(_type = 'CName', _value = name)
            new_bones_json.append(b_n)

            b_rd = copy.deepcopy(rigdata_json)
            for var in ('X','Y','Z'):
                b_rd._vars['Position']._vars[var] = CR2WJsonScalar(_type = 'Float', _value = positions[idx][var])
                b_rd._vars['Scale']._vars[var] = CR2WJsonScalar(_type = 'Float', _value = scales[idx][var])
            for var in ('X','Y','Z','W'):
                b_rd._vars['Rotation']._vars[var] = CR2WJsonScalar(_type = 'Float', _value = rotations[idx][var])
            new_rigdata_json.append(b_rd)

        skelly._vars['bones']._elements = new_bones_json
        skelly._vars['parentIndices']._elements = list(map(lambda x: CR2WJsonScalar(_type = 'Int16', _value = x), parentIdx))
        skelly._vars['rigdata']._elements = new_rigdata_json

        with open(filename, "w") as file:
            file.write(json.dumps(rig, default=vars, sort_keys=False, separators=(',', ":")))



def loadFaceFile(filename):
    dirpath, file = os.path.split(filename)
    basename, ext = os.path.splitext(file)
    if ext.lower().endswith('.json'):
        faceData = read_json_w3.readFaceFile(filename)
    elif ext.lower().endswith('.w3fac'):
        bin_data = load_bin_face(filename)
        faceData = read_json_w3.readFaceFileData(bin_data)
    else:
        faceData = None

    return faceData

