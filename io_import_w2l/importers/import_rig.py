import os
import json

from CR2W.CR2W_types import getCR2W
from CR2W.dc_skeleton import create_Skeleton, load_bin_face, load_bin_skeleton

from math import degrees
from math import radians
import bpy
from typing import List, Tuple
from pathlib import Path
from mathutils import Vector, Quaternion, Euler, Matrix

from io_import_w2l import file_helpers
from io_import_w2l.CR2W import w3_types
from io_import_w2l.CR2W import read_json_w3
from io_import_w2l.w3_armature_constants import *
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

class PMXImporter(object):
    """docstring for PMXImporter."""
    CATEGORIES = {
        0: 'SYSTEM',
        1: 'EYEBROW',
        2: 'EYE',
        3: 'MOUTH',
        }
    MORPH_TYPES = {
        0: 'group_morphs',
        1: 'vertex_morphs',
        2: 'bone_morphs',
        3: 'uv_morphs',
        4: 'uv_morphs',
        5: 'uv_morphs',
        6: 'uv_morphs',
        7: 'uv_morphs',
        8: 'material_morphs',
        }

    def __init__(self):
        self.__model = None
        self.__targetScene = bpyutils.SceneOp(bpy.context)

        self.__scale = None

        self.__root = None
        self.__armObj = None
        self.__meshObj = None

        self.__vertexGroupTable = None
        self.__textureTable = None
        self.__rigidTable = None

        self.__boneTable = []
        self.__materialTable = []
        self.__imageTable = {}

        self.__sdefVertices = {} # pmx vertices
        self.__blender_ik_links = set()
        self.__vertex_map = None

        self.__materialFaceCountTable = None

    def __createEditBones(self, obj, pmx_bones):
        """ create EditBones from pmx file data.
        @return the list of bone names which can be accessed by the bone index of pmx data.
        """
        editBoneTable = []
        nameTable = []
        specialTipBones = []
        dependency_cycle_ik_bones = []
        #for i, p_bone in enumerate(pmx_bones):
        #    if p_bone.isIK:
        #        if p_bone.target != -1:
        #            t = pmx_bones[p_bone.target]
        #            if p_bone.parent == t.parent:
        #                dependency_cycle_ik_bones.append(i)

        from math import isfinite
        def _VectorXYX(v):
            return Vector(v).xyz if all(isfinite(n) for n in v) else Vector((0,0,0))

        with bpyutils.edit_object(obj) as data:
            for i in pmx_bones:
                bone = data.edit_bones.new(name=i.name)
                loc = _VectorXYX(i.co) * self.__scale
                bone.head = loc
                editBoneTable.append(bone)
                nameTable.append(bone.name)

            for i, (b_bone, m_bone) in enumerate(zip(editBoneTable, pmx_bones)):
                if m_bone.parentId != -1:
                    if i not in dependency_cycle_ik_bones:
                        b_bone.parent = editBoneTable[m_bone.parentId]
                    else:
                        b_bone.parent = editBoneTable[m_bone.parentId].parent

        return nameTable, specialTipBones

    def createObjects(self, armObj):
        self.__armObj = armObj

    def execute(self, **args):
        if 'pmx' in args:
            self.__model = args['pmx']
        else:
            self.__model = None
        #self.__fixRepeatedMorphName()

        types = args.get('types', set())
        clean_model = args.get('clean_model', False)
        remove_doubles = args.get('remove_doubles', False)
        self.__scale = args.get('scale', 1.0)
        self.__use_mipmap = args.get('use_mipmap', True)
        self.__sph_blend_factor = args.get('sph_blend_factor', 1.0)
        self.__spa_blend_factor = args.get('spa_blend_factor', 1.0)
        self.__fix_IK_links = args.get('fix_IK_links', False)
        self.__apply_bone_fixed_axis = args.get('apply_bone_fixed_axis', False)
        self.__translator = args.get('translator', None)
        
        
        boneNameTable, specialTipBones = self.__createEditBones(self.__armObj, self.__model.bones)
        print("cake")
        


## create displayConnection attribute for all bonees. Will hold coordiante or patrent id to connect to

def create_armature2(mdl: w3_types.CSkeleton, nsp="", scale=1.0):
    PREFIX = nsp
    PREFIX = ""
    model_name =nsp#nsp.split(":")[0] #Path(mdl.header.name).stem
    armature = bpy.data.armatures.new(f"{model_name}_ARM_DATA")
    armature_obj = bpy.data.objects.new(f"{model_name}_ARM", armature)
    armature_obj.show_in_front = True
    bpy.context.collection.objects.link(armature_obj)

    armature_obj.select_set(True)
    bpy.context.view_layer.objects.active = armature_obj

    bpy.ops.object.mode_set(mode='EDIT')


    importer = PMXImporter()
    #importer.__model = mdl
    importer.createObjects(armature_obj)
    importer.execute(pmx = mdl)


    bpy.ops.object.mode_set(mode='OBJECT')

    bpy.context.active_object.rotation_euler[2] = radians(180)
    #bpy.context.collection.objects.unlink(armature_obj)
    return armature_obj

#!WORKING CREATE COMMETED OUT
def create_armature(mdl: w3_types.CSkeleton, nsp="", scale=1.0, do_fix_tail = False, context = None):
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

    context.active_object.rotation_euler[2] = radians(180)
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
    for bonedata in w3Data.bones:
        bone = arm.data.witcherui_RigSettings.bone_order_list.add()
        bone.name = bonedata.name
        
    # for bone in arm.pose.bones:
    #     print(bone.name)
    #     if bone.name == ns+"pelvis":
    #         adw = "ddaw"
    return arm

def import_w3_rig(filename, ns="", do_fix_tail = False, context = None):
    print("Importing file: ", filename)
    arm = start_rig_import(filename, ns, do_fix_tail, context)
    return arm
    w3Data = load_json_skeleton(filename)
    if not w3Data:
        return '{NONE}'
    else:
        return import_w3_rig2(w3Data, ns)

def import_w3_rig2(w3Data,ns="ciri"):
    currentNs = cmds.namespaceInfo(cur=True)
    cmds.namespace(relativeNames=True)
    if not cmds.namespace(ex=':%s'%ns):
        cmds.namespace(add=':%s'%ns)
    cmds.namespace(set=':%s'%ns)

    for bone in w3Data.bones:
        bone.name = bone.name.replace(" ", "_")
        cmds.select( d=True )
        if not cmds.objExists(bone.name):
            cmds.joint( name=bone.name, p=(float(bone.co[0]),float(bone.co[1]),float(bone.co[2])), rad=0.01 )
    for bone in w3Data.bones:
            if bone.parentId >= 0:
                try:
                    cmds.parent(bone.name,w3Data.bones[bone.parentId].name)
                except:
                    pass
    for bone in w3Data.bones:
        cmds.select(bone.name);
        cmds.setAttr("{}.translateX".format(bone.name),float(bone.co[0]));
        cmds.setAttr("{}.translateY".format(bone.name),float(bone.co[1]));
        cmds.setAttr("{}.translateZ".format(bone.name),float(bone.co[2]));
    for bone in w3Data.bones:
        cmds.select(bone.name);
        sel_list = om.MSelectionList()
        sel_list.add(bone.name)
        obj = sel_list.getDependNode(0)
        xform = om.MFnTransform(obj)
        xform.setRotation(bone.ro_quat, om.MSpace.kObject)
        # cmds.setAttr("{}.rotateX".format(bone.name),float(-bone.ro[0]));
        # cmds.setAttr("{}.rotateY".format(bone.name),float(-bone.ro[1]));
        # cmds.setAttr("{}.rotateZ".format(bone.name),float(-bone.ro[2]));
    cmds.namespace(set=currentNs)
    cmds.namespace(relativeNames=False)
    #get a list of root bones to return, these need to be groups and scaled to attach to mesh
    root_bones=[]
    for bone in w3Data.bones:
        if bone.parentId == -1:
            root_bones.append(ns+":"+bone.name)
    return root_bones;

def coordTransform(coords):
    x, y, z = coords
    y = -y
    return (x, z, y)

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

        for bl_bone in activebones:
            if bl_bone.parent:
                objectMatrix = bl_bone.parent.matrix_local.inverted()
            else:
                objectMatrix = armature.matrix_world.inverted()
            id = bones.find(bl_bone.name)
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
                            "X": 1,
                            "Y": 1,
                            "Z": 1,
                        })
            parentId = -1
            if bl_bone.parent:
                parentId = bones.find(bl_bone.parent.name)
            parentIdx.append(parentId)
            #xpsBone = w3_types.W3Bone(id, name, co, parentId)
            #xpsBones.append(xpsBone)
        output = {"nbBones": nbBones,
                    "names": names,
                    "parentIdx":parentIdx,
                    "positions":positions,
                    "rotations":rotations,
                    "scales":scales}
        with open(filename, "w") as file:
            file.write(json.dumps(output,indent=2, sort_keys=False))
        return
        start_time = time.time()
        importer = AnimImporter(fileName, animSetTemplate.animations[0])
        for i in selected_objects:
            importer.assign(i)
        logging.info(' Finished importing motion in %f seconds.', time.time() - start_time)

        update_scene_settings = True # MAKE BLEND IMPORT PROP
        if update_scene_settings:
            auto_scene_setup.setupFrameRanges()
            auto_scene_setup.setupFps()
        context.scene.frame_set(context.scene.frame_current)
        names = cmds.ls(sl=True,long=False) or []


        for eachSel in names:
            try:
                parent = cmds.listRelatives(eachSel, parent=True)[0]
                parentIdx.append(names.index(parent))
            except:
                parentIdx.append(-1) ##it has no parent
            pos = cmds.xform(eachSel, q= 1, t= 1)
            positions.append({
                            "X": pos[0],
                            "Y": pos[1],
                            "Z": pos[2],
                        })
            rot = cmds.xform(eachSel, q= 1, ro= 1)
            rot_quat = read_json_w3.eularToQuat([-rot[0],-rot[1],-rot[2]])
            rotations.append({
                            "X": rot_quat[0],
                            "Y": rot_quat[1],
                            "Z": rot_quat[2],
                            "W": rot_quat[3]
                        })
            scale = cmds.xform(eachSel, q= 1, s= 1, r=1)
            scales.append({
                            "X": scale[0],
                            "Y": scale[1],
                            "Z": scale[2],
                        })


def export_w3_rig_maya(filename):
    names = cmds.ls(sl=True,long=False) or []
    parentIdx = []
    positions = []
    rotations = []
    scales = []
    nbBones = len(names)
    output = list()
    for eachSel in names:
        try:
            parent = cmds.listRelatives(eachSel, parent=True)[0]
            parentIdx.append(names.index(parent))
        except:
            parentIdx.append(-1) ##it has no parent
        pos = cmds.xform(eachSel, q= 1, t= 1)
        positions.append({
                        "X": pos[0],
                        "Y": pos[1],
                        "Z": pos[2],
                    })
        rot = cmds.xform(eachSel, q= 1, ro= 1)
        rot_quat = read_json_w3.eularToQuat([-rot[0],-rot[1],-rot[2]])
        rotations.append({
                        "X": rot_quat[0],
                        "Y": rot_quat[1],
                        "Z": rot_quat[2],
                        "W": rot_quat[3]
                    })
        scale = cmds.xform(eachSel, q= 1, s= 1, r=1)
        scales.append({
                        "X": scale[0],
                        "Y": scale[1],
                        "Z": scale[2],
                    })
    output = {"nbBones": nbBones,
                    "names": names,
                    "parentIdx":parentIdx,
                    "positions":positions,
                    "rotations":rotations,
                    "scales":scales}
    with open(filename, "w") as file:
        file.write(json.dumps(output,indent=2, sort_keys=True))


def _getHierarchyRootJoint( joint="" ):
    rootJoint = joint
    while (True):
        parent = pm.listRelatives( rootJoint,
                                     parent=True,
                                     type='joint' )
        if not parent:
            break;
        rootJoint = parent[0]
    return rootJoint



def constrain_w3_rig(source, target, mo=False):
    #attach source to target
    #any bone in the source attaches to target
    root_constrained = False
    if not pm.namespace( exists=source ):
        return
    #get all bones in source
    source = source+":*"
    pm.select( source,  r=1, hi=1 )
    #TODO FIND A WAY TO MOVE MESH INTO POSITION BEFORE DOING A CONSTRAIN WITH MAINTAIN OFFEST
    #USING THE ROOT OF HI SHOULD WORK
    bones = pm.ls( selection=True, type="joint" )

    root_bones=[]
    for bone in bones:
        if _getHierarchyRootJoint(bone.getName()) in root_bones:
            pass
        else:
            print(_getHierarchyRootJoint(bone.getName()))
            root_bones.append(_getHierarchyRootJoint(bone.getName()))
    # root = _getHierarchyRootJoint(bones[0].getName())
    # pm.select( root, hi=1 )
    # bones = pm.ls( selection=True, type="joint" )

    #loop those bones
    for joint in bones:
        bone_name = joint.getName().split(':')[-1]
        if pm.objExists("{}:{}".format(target,bone_name)):
            # root_constrain = False
            # for rb in root_bones:
            #     if bone_name == rb.split(':')[-1]:
            #         root_constrain = True
            if "eye" in bone_name or "ear" in bone_name:
                pm.parentConstraint( joint.getName(), "{}:{}".format(target,bone_name), mo=True )
            else:
                pm.parentConstraint( joint.getName(), "{}:{}".format(target,bone_name) )

def hard_attach(source, target, mo=False):
    pm.parentConstraint( source, target, mo=True )

def import_w3_animation_OLD(w3Data, SkeletalAnimation, type, al=False):
    bone_frames = {}
    for rig_bone in w3Data.bones:
        bone_frames[rig_bone.name]={
            "positionFrames":[],
            "rotationFrames":[],
            "scaleFrames":[]
        }
    animData = SkeletalAnimation.animBuffer
    multipart = False
    if animData.parts:
        multipart=True

    # start time of playback
    #cmds.playbackOptions(q= 1, min= 1)
    # end time of playback
    #cmds.playbackOptions(q= 1, max= animData.numFrames, aet=animData.numFrames)
    cmds.playbackOptions( min='1', max=str(animData.numFrames), ast='1', aet=str(animData.numFrames))
    # start time of playback
    start = 1 #cmds.playbackOptions(q= 1, min= 1)
    # end time of playback
    end = animData.numFrames+1#cmds.playbackOptions(q= 1, max= 1)
    for fi in range(int(start), int(end)):
        time_index=fi
        fi=fi-1
        # move frame
        #cmds.currentTime(i, e= 1)
        # for bone in animData.bones:

        #     ## NEED TO CHECK DT AND SKIP PROPER FRAMES
        #     if cmds.objExists(bone.BoneName):
        #         cmds.select(bone.BoneName);
        #         sel_list = om.MSelectionList()
        #         sel_list.add(bone.BoneName)
        #         obj = sel_list.getDependNode(0)
        #         xform = om.MFnTransform(obj)
        #         try:
        #             bone_frames = len(bone.positionFrames)
        #             total_frames = animData.numFrames
        #             frame_skip = round(float(total_frames)/float(bone_frames))
        #             frame_array = [frame_skip*n for n in range(0,bone_frames)]
        #             if float(fi) in frame_array:
        #                 cmds.xform( t=(bone.positionFrames[frame_array.index(fi)][0],
        #                                 bone.positionFrames[frame_array.index(fi)][1],
        #                                 bone.positionFrames[frame_array.index(fi)][2]))
        #                 if al:
        #                     pm.setKeyframe(bone.BoneName, t=time_index, at='translate', al=al)
        #                 else:
        #                     pm.setKeyframe(bone.BoneName, t=time_index, at='translate')
        #                 #cmds.setKeyframe( at='translate', itt='spline', ott='spline', al=al )
        #         except IndexError:
        #             pass
        #             # handle this
        #         try:
        #             bone_frames = len(bone.rotationFrames)
        #             total_frames = animData.numFrames
        #             frame_skip = round(float(total_frames)/float(bone_frames))
        #             frame_array = [frame_skip*n for n in range(0,bone_frames)]
        #             if float(fi) in frame_array:
        #                 #MIMIC POSES DON'T GET INVERTED
        #                 if type is "face":
        #                     cmds.xform( ro=(-bone.rotationFrames[frame_array.index(fi)][0],
        #                                     -bone.rotationFrames[frame_array.index(fi)][1],
        #                                     -bone.rotationFrames[frame_array.index(fi)][2]))
        #                 else:
        #                     xform.setRotation(bone.rotationFramesQuat[frame_array.index(fi)], om.MSpace.kObject)
        #                 # if type is "face":
        #                 #     cmds.xform( ro=(bone.rotationFrames[frame_array.index(fi)][0],
        #                 #                     bone.rotationFrames[frame_array.index(fi)][1],
        #                 #                     bone.rotationFrames[frame_array.index(fi)][2]))
        #                 # else:
        #                 #     cmds.xform( ro=(-bone.rotationFrames[frame_array.index(fi)][0],
        #                 #                     -bone.rotationFrames[frame_array.index(fi)][1],
        #                 #                     -bone.rotationFrames[frame_array.index(fi)][2]))
        #                 #cmds.setKeyframe( at='rotate', itt='auto', ott='auto', al=al )
        #                 if al:
        #                     pm.setKeyframe(bone.BoneName, t=time_index, at='rotate', al=al)
        #                 else:
        #                     pm.setKeyframe(bone.BoneName, t=time_index, at='rotate')
        #         except IndexError:
        #             pass
        for track in animData.tracks:
            ns= "ciri_"
            trackname = ns+track.trackName
            if pm.animLayer(trackname, query=True, ex=True):
                try:
                    track_frames = len(track.trackFrames)
                    total_frames = animData.numFrames
                    frame_skip = round(float(total_frames)/float(track_frames))
                    frame_array = [frame_skip*n for n in range(0,track_frames)]
                    if float(fi) in frame_array:
                        weight = round(track.trackFrames[frame_array.index(fi)], 5)
                        pm.select(trackname)
                        pm.animLayer( trackname, edit=True, weight= weight)
                        pm.setKeyframe( trackname, attribute='weight', t=time_index )

                        #cmds.setKeyframe( at='translate', itt='spline', ott='spline', al=al )
                except IndexError:
                    pass
                    # handle this
    return SkeletalAnimation

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

def import_w3_face(filename):
    #load skeleton for face using import_w3_rig
    faceData = loadFaceFile(filename)
    mimicSkeleton = import_w3_rig2(faceData.mimicSkeleton)
    #floatTrackSkeleton = import_w3_rig2(faceData.floatTrackSkeleton)

    #TODO create checkbox to select what to import
    #mimicPoses = import_w3_mimicPoses(faceData.mimicPoses, faceData.mimicSkeleton)
    #load the mimicPoses as keyframe 0 poses each with anim layer
    return mimicSkeleton

def import_w3_mimicPoses_test(filename):
    faceData = loadFaceFile(filename)
    mimicSkeleton = import_w3_rig2(faceData.mimicSkeleton)
    poses_import =[]
    for item in faceData.mimicPoses:
        if item.name == "eye_right_left":
            poses_import.append(item)
    mimicPoses = import_w3_mimicPoses(poses_import, faceData.mimicSkeleton, "shani", "shani:CMimicComponent12")
    return mimicPoses

def export_w3_face(filename):
    pass

def import_w3_mimicPoses(poses, mimicSkeleton, actor, mimic_namespace):
    # ns = "ciri"
    # if not pm.namespace( exists=ns ):
    #     pm.namespace( add=ns )
    # ns = ns+":"
    # root_ctrl = None
    # try:
    #     root_ctrl = pm.PyNode('torso3')
    # except:
    #     root_ctrl = pm.PyNode(ns + 'torso3')

    ##create the layers 
    for pose in poses:
        # if pose.name == 'lips_blow':
        #   break
        if not pm.animLayer(actor+"_"+pose.name, ex=True, q=True):
            pm.animation.animLayer(actor+"_"+pose.name, weight=0.0)

    for pose in poses:
        # if pose.name == 'lips_blow':
        #   break
        animBuffer = pose.animBuffer
        select_list= [] #select only the bones moved by the pose.

        for bone in animBuffer.bones:
            all_zeros = bone.positionFrames[0].count(0.0)
            all_zerosQ = bone.rotationFrames[0].count(0.0)
            if bone.positionFrames[0].count(0.0) == 3 and anims.shouldIgnoreFrame(bone): #bone.rotationFrames[0].count(0.0) is 3:
                pass
            else:
                select_list.append(mimic_namespace+":"+bone.BoneName)
        pm.select(select_list)
        pm.animation.animLayer(actor+"_"+pose.name, edit=True, addSelectedObjects=True)
        anims.import_w3_animation2(pose, mimic_namespace, "face", actor+"_"+pose.name)
