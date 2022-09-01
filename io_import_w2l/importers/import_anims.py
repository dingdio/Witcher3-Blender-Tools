from io_import_w2l import auto_scene_setup
from io_import_w2l.file_helpers import getFilenameFile, rm_ns
from io_import_w2l.CR2W import read_json_w3
from io_import_w2l.CR2W import w3_types
from io_import_w2l.CR2W.dc_anims import load_bin_anims, load_lipsync_file
from io_import_w2l.CR2W.CR2W_helpers import Enums
from io_import_w2l.setup_logging_bl import *
log = logging.getLogger(__name__)

import json
import copy
import math
from mathutils import Vector, Quaternion, Euler, Matrix
import os
import time
from typing import Union
import numpy as np

import bpy
matmul = (lambda a, b: a*b) if bpy.app.version < (2, 80, 0) else (lambda a, b: a.__matmul__(b))

def shouldIgnoreFrame(bone):
    checkArr = [1,1,1]
    #if bone.BoneName == "lowwer_left_lip":
    if abs(bone.rotationFrames[0][0]) < 0.5:
        checkArr[0] = 0
    if abs(bone.rotationFrames[0][1]) < 0.5:
        checkArr[1] = 0
    if abs(bone.rotationFrames[0][2]) < 0.5:
        checkArr[2] = 0
    if checkArr.count(0.0) == 3:
        return True
    return False

class animFile:
    def __init__(self):
        self.filepath = None
    def load(self, filepath):
        self.filepath = filepath

class HasAnimationData:
    animation_data: bpy.types.AnimData

class _InterpolationHelper:
    def __init__(self, mat):
        self.__indices = indices = [0, 1, 2]
        l = sorted((-abs(mat[i][j]), i, j) for i in range(3) for j in range(3))
        _, i, j = l[0]
        if i != j:
            indices[i], indices[j] = indices[j], indices[i]
        _, i, j = next(k for k in l if k[1] != i and k[2] != j)
        if indices[i] != j:
            idx = indices.index(j)
            indices[i], indices[idx] = indices[idx], indices[i]

    def convert(self, interpolation_xyz):
        return (interpolation_xyz[i] for i in self.__indices)

class BoneConverter:
    def __init__(self, pose_bone, scale, invert=False):
        mat = pose_bone.bone.matrix_local.to_3x3()
        mat[1], mat[2] = mat[2].copy(), mat[1].copy()
        self.__mat = mat.transposed()
        self.__scale = scale
        if invert:
            self.__mat.invert()
        self.convert_interpolation = _InterpolationHelper(self.__mat).convert

    def convert_location(self, location):
        return matmul(self.__mat, Vector(location)) * self.__scale

    def convert_rotation(self, rotation_xyzw):
        rot = Quaternion()
        rot.x, rot.y, rot.z, rot.w = rotation_xyzw
        return Quaternion(matmul(self.__mat, rot.axis) * -1, rot.angle).normalized()

class BoneConverterPoseMode:
    def __init__(self, pose_bone, scale, invert=False):
        mat = pose_bone.matrix.to_3x3()
        mat[1], mat[2] = mat[2].copy(), mat[1].copy()
        self.__mat = mat.transposed()
        self.__scale = scale
        self.__mat_rot = pose_bone.matrix_basis.to_3x3()
        self.__mat_loc = matmul(self.__mat_rot, self.__mat)
        self.__offset = pose_bone.location.copy()
        self.convert_location = self._convert_location
        self.convert_rotation = self._convert_rotation
        if invert:
            self.__mat.invert()
            self.__mat_rot.invert()
            self.__mat_loc.invert()
            self.convert_location = self._convert_location_inverted
            self.convert_rotation = self._convert_rotation_inverted
        self.convert_interpolation = _InterpolationHelper(self.__mat_loc).convert

    def _convert_location(self, location):
        return self.__offset + matmul(self.__mat_loc, Vector(location)) * self.__scale

    def _convert_rotation(self, rotation_xyzw):
        rot = Quaternion()
        rot.x, rot.y, rot.z, rot.w = rotation_xyzw
        rot = Quaternion(matmul(self.__mat, rot.axis) * -1, rot.angle)
        return matmul(self.__mat_rot, rot.to_matrix()).to_quaternion()

    def _convert_location_inverted(self, location):
        return matmul(self.__mat_loc, Vector(location) - self.__offset) * self.__scale

    def _convert_rotation_inverted(self, rotation_xyzw):
        rot = Quaternion()
        rot.x, rot.y, rot.z, rot.w = rotation_xyzw
        rot = matmul(self.__mat_rot, rot.to_matrix()).to_quaternion()
        return Quaternion(matmul(self.__mat, rot.axis) * -1, rot.angle).normalized()


class AnimImporter:
    def __init__(self, filepath, SetEntry, scale=1.0, use_pose_mode=False, use_NLA=False, facePose=False, NLA_track = 'anim_import'):
        self.__animFile = animFile()
        self.__animFile.load(filepath=filepath)
        #log.debug(str(self.__animFile.header))
        self.__scale = scale
        self.__SetEntry = SetEntry
        self.__use_NLA = use_NLA
        self.__NLA_track = NLA_track
        self.__bone_util_cls = BoneConverterPoseMode if use_pose_mode else BoneConverter
        self.__frame_margin = 0
        self.facePose = facePose

    @staticmethod
    def __swap_components(vec, mp):
        __pat = 'XYZ'
        return [vec[__pat.index(k)] for k in mp]

    @staticmethod
    def __minRotationDiff(prev_q, curr_q):
        t1 = (prev_q.w - curr_q.w)**2 + (prev_q.x - curr_q.x)**2 + (prev_q.y - curr_q.y)**2 + (prev_q.z - curr_q.z)**2
        t2 = (prev_q.w + curr_q.w)**2 + (prev_q.x + curr_q.x)**2 + (prev_q.y + curr_q.y)**2 + (prev_q.z + curr_q.z)**2
        #t1 = prev_q.rotation_difference(curr_q).angle
        #t2 = prev_q.rotation_difference(-curr_q).angle
        return -curr_q if t2 < t1 else curr_q

    @staticmethod
    def __setInterpolation(bezier, kp0, kp1):
        if bezier[0] == bezier[1] and bezier[2] == bezier[3]:
            kp0.interpolation = 'LINEAR'
        else:
            kp0.interpolation = 'BEZIER'
        kp0.handle_right_type = 'FREE'
        kp1.handle_left_type = 'FREE'
        d = (kp1.co - kp0.co) / 127.0
        kp0.handle_right = kp0.co + Vector((d.x * bezier[0], d.y * bezier[1]))
        kp1.handle_left = kp0.co + Vector((d.x * bezier[2], d.y * bezier[3]))

    @staticmethod
    def __fixFcurveHandles(fcurve):
        kp0 = fcurve.keyframe_points[0]
        kp0.handle_left_type = 'FREE'
        kp0.handle_left = kp0.co + Vector((-1, 0))
        kp = fcurve.keyframe_points[-1]
        kp.handle_right_type = 'FREE'
        kp.handle_right = kp.co + Vector((1, 0))

    @staticmethod
    def __keyframe_insert_inner(fcurves: bpy.types.ActionFCurves, path: str, index: int, frame: float, value: float):
        fcurve = fcurves.find(path, index=index)
        if fcurve is None:
            fcurve = fcurves.new(path, index=index)
        fcurve.keyframe_points.insert(frame, value, options={'FAST'})

    @staticmethod
    def __keyframe_insert(fcurves: bpy.types.ActionFCurves, path: str, frame: float, value: Union[int, float, Vector]):
        if isinstance(value, (int, float)):
            AnimImporter.__keyframe_insert_inner(fcurves, path, 0, frame, value)

        elif isinstance(value, Vector):
            AnimImporter.__keyframe_insert_inner(fcurves, path, 0, frame, value[0])
            AnimImporter.__keyframe_insert_inner(fcurves, path, 1, frame, value[1])
            AnimImporter.__keyframe_insert_inner(fcurves, path, 2, frame, value[2])

        else:
            raise TypeError('Unsupported type: {0}'.format(type(value)))

    def __getBoneConverter(self, bone):
        converter = self.__bone_util_cls(bone, self.__scale)
        mode = bone.rotation_mode
        compatible_quaternion = self.__minRotationDiff
        class _ConverterWrap:
            convert_location = converter.convert_location
            convert_interpolation = converter.convert_interpolation
            if mode == 'QUATERNION':
                convert_rotation = converter.convert_rotation
                compatible_rotation = compatible_quaternion
            elif mode == 'AXIS_ANGLE':
                @staticmethod
                def convert_rotation(rot):
                    (x, y, z), angle = converter.convert_rotation(rot).to_axis_angle()
                    return (angle, x, y, z)
                @staticmethod
                def compatible_rotation(prev, curr):
                    angle, x, y, z = curr
                    if prev[1]*x + prev[2]*y + prev[3]*z < 0:
                        angle, x, y, z = -angle, -x, -y, -z
                    angle_diff = prev[0] - angle
                    if abs(angle_diff) > math.pi:
                        pi_2 = math.pi * 2
                        bias = -0.5 if angle_diff < 0 else 0.5
                        angle += int(bias + angle_diff/pi_2) * pi_2
                    return (angle, x, y, z)
            else:
                convert_rotation = lambda rot: converter.convert_rotation(rot).to_euler(mode)
                compatible_rotation = lambda prev, curr: curr.make_compatible(prev) or curr
        return _ConverterWrap


    def __assign_action(self, target: Union[bpy.types.ID, HasAnimationData], action: bpy.types.Action):
        if target.animation_data is None:
            target.animation_data_create()

        if not self.__use_NLA:
            target.animation_data.action = action
        else:
            frame_current = bpy.context.scene.frame_current
            frame_current = 0 #!TEMP
            if self.__NLA_track:
                target_track: bpy.types.NlaTrack = target.animation_data.nla_tracks.get(self.__NLA_track)
                if target_track is None:
                    target_track: bpy.types.NlaTrack = target.animation_data.nla_tracks.new()
                    target_track.name = self.__NLA_track #action.name
                for strip in target_track.strips:
                    target_track.strips.remove(strip)
            else:
                target_track: bpy.types.NlaTrack = target.animation_data.nla_tracks.new()
                target_track.name = action.name
                
            target_strip = target_track.strips.new(action.name, frame_current, action)
            target_strip.blend_type = 'COMBINE'

    def __assignToArmature(self, armObj, action_name=None):
        def detect_maya_namespace(s):
            if ':' in s:
                srp = s.rpartition(':')
                return srp[0]+":"
            else:
                return None
        SkeletalAnimationType = self.__SetEntry.animation.SkeletalAnimationType
        AdditiveType = self.__SetEntry.animation.AdditiveType

        # class ESkeletalAnimationType(Enum):
        #     """Docstring for ESkeletalAnimationType."""
        #     SAT_Normal = 0
        #     SAT_Additive = 1
        #     SAT_MS = 2
            
        # class EAdditiveType(Enum):
        #     """Docstring for EAdditiveType."""
        #     AT_Local = 0
        #     AT_Ref = 1
        #     AT_TPose = 2
        #     AT_Animation = 3

        armature_namespace = detect_maya_namespace(armObj.pose.bones[0].name)
        scale = 1.0
        extra_frame = 1 if self.__frame_margin > 1 else 0
        SkeletalAnimation = self.__SetEntry.animation
        SkeletalAnimationData = SkeletalAnimation.animBuffer
        face_animation = False
        if(SkeletalAnimationData.tracks):
            face_animation = True
        multipart = False
        if hasattr(SkeletalAnimationData, 'parts'):
            multipart=True
        if multipart:
            log.info('multipart not implemented')
        else:
            anim_desc = copy.deepcopy(SkeletalAnimationData)

        #add detected namespace to aniamtion data
        if armature_namespace:
            for i, bone in enumerate(anim_desc.bones):
                anim_desc.bones[i].BoneName = armature_namespace+bone.BoneName

        action_name = SkeletalAnimation.name or action_name or armObj.name
        action = bpy.data.actions.new(name=action_name)

        curr_action = (armObj.animation_data.action
            if armObj.animation_data is not None and
            armObj.animation_data.action is not None
            else None)

        class _Dummy: pass
        dummy_keyframe_points = iter(lambda: _Dummy, None)
        prop_rot_map = {'QUATERNION':'rotation_quaternion', 'AXIS_ANGLE':'rotation_axis_angle'}

        world_pos_list = [] #['r_weapon', 'l_weapon']#,
                        #   'silver_sword_back',
                        #   'steel_sword_back' ,
                        #   'crossbow_back'] #TODO look at this
        curve_per_bone = {}
        for bone_data in anim_desc.bones:
            bone_name = bone_data.BoneName
            group = action.groups.new(name=bone_name)
            pos_curves = [dummy_keyframe_points] * 3
            rot_curves = [dummy_keyframe_points] * 4
            bl_bone = armObj.pose.bones.get(bone_data.BoneName)
            fcurves_rot = [dummy_keyframe_points]*4 # r0, r1, r2, (r3)
            fcurves_loc = [dummy_keyframe_points]*3 # x, y, z
            data_path_rot = prop_rot_map.get(bl_bone.rotation_mode, 'rotation_quaternion')
            bone_rotation = getattr(bl_bone, data_path_rot)
            data_path = 'pose.bones["%s"].location'%bl_bone.name
            for axis_i in range(3):
                fcurves_loc[axis_i] = action.fcurves.new(data_path=data_path, index=axis_i, action_group=bl_bone.name)
            data_path = 'pose.bones["%s"].%s'%(bl_bone.name, data_path_rot)
            for axis_i in range(len(bone_rotation)):
                fcurves_rot[axis_i] = action.fcurves.new(data_path=data_path, index=axis_i, action_group=bl_bone.name)

            pos_curves = fcurves_loc
            rot_curves = fcurves_rot

            curve_per_bone[bone_name] = pos_curves, rot_curves
        total_frames = anim_desc.numFrames
        
        start_time = time.time()
        for bone in anim_desc.bones:
            keyFrames_rot = bone.rotationFramesQuat
            keyFrames_loc = bone.positionFrames
            mdl_bone = bone #mdl.bones[bone.bone_id]
            bl_bone = armObj.pose.bones.get(mdl_bone.BoneName)
            bl_bone.rotation_mode = 'QUATERNION'

            pos_scale = 1.0
            rot_scale = 1.0

            pos_frames = [Vector(np.multiply(np.multiply(pos, pos_scale), scale)) for pos in bone.positionFrames]
            #rot_frames = [Euler(np.multiply(Quaternion((rot.W, rot.X, rot.Y, rot.Z)).to_euler('XYZ'), rot_scale)) for rot in bone.rotationFramesQuat]
            
            #! IN JSON FILES I FLIPPED W FOR SOME REASON
            rot_frames = [Quaternion((-rot.W, rot.X, rot.Y, rot.Z)) for rot in bone.rotationFramesQuat]
            pos_curves, rot_curves = curve_per_bone[mdl_bone.BoneName]


            #! POSITION FRAMES
            bone_frames = len(bone.positionFrames)
            if bone_frames == 1 and self.facePose and bone.positionFrames[0].count(0.0) == 3: #for face animations that pass in 0s that are not used, might cause issues?
                pass
            else:
                loc_frame_number = 0
                frame_skip_loc = round(float(total_frames)/float(len(keyFrames_loc)))
                for n, pos_frame in enumerate(pos_frames):
                    loc_frame = loc_frame_number
                    loc_frame_number += frame_skip_loc * 1
                    if bl_bone.parent:
                        objectMatrix = bl_bone.parent.bone.matrix_local.inverted()
                        origPos = objectMatrix @ bl_bone.bone.matrix_local.translation
                    else:
                        #objectMatrix = armObj.matrix_world.inverted()
                        origPos = bl_bone.bone.matrix_local.translation
                    origPos = Vector(( origPos.x, origPos.y, origPos.z ))
                    #origRot = bl_bone.bone.matrix_local.to_quaternion()  # LOCAL EditBone
                    
                    pos_fix = pos_frame
                    
                    if SkeletalAnimationType == "SAT_Additive" or face_animation:
                        pos_fix = pos_fix
                    else:
                        pos_fix = pos_frame - origPos
                        #! DISABLE FOR FACE POSES
                        if self.facePose or mdl_bone.BoneName in world_pos_list :
                            log.debug("face mode active")
                        else:
                            pos_fix = bl_bone.bone.matrix.to_quaternion().inverted() @ pos_fix #origRot.inverted() @ pos_fix
                    pos_fix = Vector(( round(pos_fix.x, 6), round(pos_fix.y, 6), round(pos_fix.z, 6) ))
                    pos = pos_fix
                    for i in range(3):
                        pos_curves[i].keyframe_points.add(1)
                        pos_curves[i].keyframe_points[-1].co = (loc_frame, pos[i])
                        pos_curves[i].keyframe_points[-1].interpolation = 'LINEAR'

            #! ROTATION FRAMES
            
            bone_frames = len(bone.rotationFrames)
            if bone_frames == 1 and self.facePose and shouldIgnoreFrame(bone): #for face animations that pass in 0s that are not used, might cause issues?
                pass
            else:
                rot_frame_number = 0
                frame_skip_rotation = round(float(total_frames)/float(len(keyFrames_rot)))
                for n, rot_frame in enumerate(rot_frames):
                    if self.facePose:
                        rot_frame.w = -rot_frame.w
                    #rot_frame.w = -rot_frame.w #!!!!!!!!!!!!!!!! THE JSON AND W2ANIMS ARE INCONSISTANT FIX FLIP W
                    frame = rot_frame_number
                    rot_frame_number += frame_skip_rotation * 1
                    fixed_rot = rot_frame

                    if not face_animation and SkeletalAnimationType != "SAT_Additive":
                        if bl_bone.parent:
                            origRotP = bl_bone.parent.bone.matrix_local.to_quaternion()  # LOCAL EditBone
                            fixed_rot = origRotP @ fixed_rot

                        origRot = bl_bone.bone.matrix_local.to_quaternion()  # LOCAL EditBone
                        fixed_rot = origRot.inverted() @ fixed_rot
                        fixed_rot= Quaternion((-fixed_rot.w,-fixed_rot.x,-fixed_rot.y,-fixed_rot.z))

                    #TODO Rotate the armature object so the character faces forward after load
                    if bl_bone.parent is None:
                        #fixed_rot.rotate(Euler([math.radians(-90), math.radians(0), math.radians(0)]))
                        #fixed_rot.rotate(Quaternion((0.7071068, -0.7071068, 0, 0)))
                        degrees =  tuple(math.degrees(a) for a in fixed_rot.to_euler("XYZ"))
                        armObj.rotation_euler.x = math.radians(0)
                        armObj.rotation_euler.y = math.radians(0)
                        armObj.rotation_euler.z = math.radians(180)
                        
                        if degrees[0] <= -88 and degrees[0] >= -92:
                            armObj.rotation_euler.x = math.radians(90)
                        elif degrees[0] > 88 and degrees[0] <92:
                            armObj.rotation_euler.x = math.radians(-90)
                        if degrees[1] > 178 and degrees[1] < 181:
                            armObj.rotation_euler.z = math.radians(0)
                        if degrees[2] > 88 and degrees[2] < 120:
                            armObj.rotation_euler.y = math.radians(-90)
                            armObj.rotation_euler.z = math.radians(-90)
                        elif degrees[2] < -88 and degrees[2] > -120:
                            armObj.rotation_euler.y = math.radians(90)
                            armObj.rotation_euler.z = math.radians(90)

                    for i in range(4):
                        rot_curves[i].keyframe_points.add(1)
                        rot_curves[i].keyframe_points[-1].co = (frame, fixed_rot[i])
                        rot_curves[i].keyframe_points[-1].interpolation = 'LINEAR'
        bpy.ops.object.mode_set(mode='OBJECT')
        log.critical(' Finished adding keyframes in %f seconds.', time.time() - start_time)
        
        # if len(armObj.keys()) > 1:
        #     # First item is _RNA_UI
        #     print("Object",armObj.name,"custom properties:")
        #     for K in armObj.keys():
        #         if K not in '_RNA_UI':
        #             print( K , "-" , armObj[K] )
        # if "jaw_open_a" in armObj.keys():
        #     print( "jaw_open_a" , "-" , armObj["jaw_open_a"] )
        
        SkeletalAnimation = self.__SetEntry.animation
        SkeletalAnimationData = SkeletalAnimation.animBuffer

        shapeKeyAnim = SkeletalAnimationData.tracks
        if shapeKeyAnim and len(shapeKeyAnim) > 1:
            log.info('---- morph animations:%5d  target: %s', len(shapeKeyAnim), armObj.name)

            if "w3_face_poses" not in armObj.pose.bones:
                log.warning('No shape key control bone. Add shape keys to '+armObj.name)
            else:
                mirror_map = {}#_MirrorMapper(meshObj.data.shape_keys.key_blocks) if self.__mirror else {}
                shapeKeyDict = {k:mirror_map.get(k, v) for k, v in armObj.pose.bones["w3_face_poses"].items()}

                from math import floor, ceil
                #for name, keyFrames in shapeKeyAnim.items():
                for track in shapeKeyAnim:
                    keyFrames = track.trackFrames
                    if len(keyFrames) == 0:
                        continue
                    name = track.trackName #"ciri_"+track.trackName+"_DUP"
                    if name not in shapeKeyDict:
                        log.warning('WARNING: not found shape key %s (%d frames)', name, len(keyFrames))
                        continue
                    log.info('(mesh) frames:%5d  name: %s', len(keyFrames), name)
                    shapeKey = shapeKeyDict[name]
                    fcurve = action.fcurves.new(data_path='pose.bones["w3_face_poses"]["%s"]'%name)#  (data_path='key_blocks["%s"].value'%shapeKey.name)
                    fcurve.keyframe_points.add(len(keyFrames))
                    #keyFrames.sort(key=lambda x:x.frame_number)
                    frame_number = 0
                    track_frames = len(track.trackFrames)
                    total_frames = SkeletalAnimationData.numFrames
                    frame_skip = round(float(total_frames)/float(track_frames))
                    #frame_array = [frame_skip*n for n in range(0,track_frames)]
                    for k, v in zip(keyFrames, fcurve.keyframe_points):
                        v.co = (frame_number+self.__frame_margin, k)
                        v.interpolation = 'LINEAR'
                        frame_number+=frame_skip*1
                    # weights = tuple(i for i in keyFrames)
                    # shapeKey.slider_min = min(shapeKey.slider_min, floor(min(weights)))
                    # shapeKey.slider_max = max(shapeKey.slider_max, ceil(max(weights)))
        
        
        
        self.__assign_action(armObj, action)

    def __assignToMesh(self, meshObj, action_name=None):
        SkeletalAnimation = self.__SetEntry.animation
        SkeletalAnimationData = SkeletalAnimation.animBuffer

        shapeKeyAnim = SkeletalAnimationData.tracks
        log.info('---- morph animations:%5d  target: %s', len(shapeKeyAnim), meshObj.name)
        if len(shapeKeyAnim) < 1:
            return

        action_name = SkeletalAnimation.name+"_Facial" or action_name or meshObj.name
        action = bpy.data.actions.new(name=action_name)

        mirror_map = {}#_MirrorMapper(meshObj.data.shape_keys.key_blocks) if self.__mirror else {}
        shapeKeyDict = {k:mirror_map.get(k, v) for k, v in meshObj.data.shape_keys.key_blocks.items()}

        from math import floor, ceil
        #for name, keyFrames in shapeKeyAnim.items():
        for track in shapeKeyAnim:
            keyFrames = track.trackFrames
            if len(keyFrames) == 0:
                continue
            name = track.trackName #"ciri_"+track.trackName+"_DUP"
            if name not in shapeKeyDict:
                log.warning('WARNING: not found shape key %s (%d frames)', name, len(keyFrames))
                continue
            log.info('(mesh) frames:%5d  name: %s', len(keyFrames), name)
            shapeKey = shapeKeyDict[name]
            fcurve = action.fcurves.new(data_path='key_blocks["%s"].value'%shapeKey.name)
            fcurve.keyframe_points.add(len(keyFrames))
            #keyFrames.sort(key=lambda x:x.frame_number)
            frame_number = 0
            track_frames = len(track.trackFrames)
            total_frames = SkeletalAnimationData.numFrames
            frame_skip = round(float(total_frames)/float(track_frames))
            #frame_array = [frame_skip*n for n in range(0,track_frames)]
            for k, v in zip(keyFrames, fcurve.keyframe_points):
                v.co = (frame_number+self.__frame_margin, k)
                v.interpolation = 'LINEAR'
                frame_number+=frame_skip*1
            weights = tuple(i for i in keyFrames)
            shapeKey.slider_min = min(shapeKey.slider_min, floor(min(weights)))
            shapeKey.slider_max = max(shapeKey.slider_max, ceil(max(weights)))

        self.__assign_action(meshObj.data.shape_keys, action)


    def assign(self, obj, action_name=None):
        if obj is None:
            return
        if action_name is None:
            action_name = os.path.splitext(os.path.basename(self.__animFile.filepath))[0]

        if getattr(obj.data, 'shape_keys', None):
            self.__assignToMesh(obj, action_name+'_facial')
        elif obj.type == 'ARMATURE':
            self.__assignToArmature(obj, action_name+'_bone')
        else:
            pass

def NewListItem( treeList, node):
    item = treeList.add()
    item.name = node.animation.name
    item.framesPerSecond = node.animation.framesPerSecond
    item.numFrames = node.animation.animBuffer.numFrames
    item.duration = node.animation.duration
    item.SkeletalAnimationType = node.animation.SkeletalAnimationType
    if node.animation.AdditiveType:
        item.AdditiveType = node.animation.AdditiveType
    
    #item.jsonData = "cake" #node.toJSON()
    #item.nodeIndex = node.selfIndex
    #item.childCount = node.childCount
    return item


def import_anim(context, fileName, AnimationSetEntry, facePose=False, use_NLA=False, override_select = False, update_scene_settings = True, NLA_track = 'anim_import'):
    if not override_select:
        selected_objects = set(context.selected_objects)
    else:
        selected_objects = override_select
    start_time = time.time()
    importer = AnimImporter(fileName, AnimationSetEntry, use_NLA=use_NLA, facePose=facePose, NLA_track = NLA_track)
    for i in selected_objects:
        importer.assign(i)
    log.info(' Finished importing motion in %f seconds.', time.time() - start_time)

    #update_scene_settings = True # MAKE BLEND IMPORT PROP
    if update_scene_settings:
        auto_scene_setup.setupFrameRanges()
        auto_scene_setup.setupFps()
    context.scene.frame_set(context.scene.frame_current)
    return {'FINISHED'}

global GLOBAL_ANIMSET

def import_from_list_item(context, item):
    #cake = w3_types.CSkeletalAnimationSetEntry.from_json(json.loads(item.jsonData))
    for anim_set_entry in GLOBAL_ANIMSET.animations:
        if anim_set_entry.animation.name == item.name:
            import_anim(context, "from_list", anim_set_entry)

def import_w3_animSet(filename, rigPath = False)-> w3_types.CSkeletalAnimationSet:
    dirpath, file = os.path.split(filename)
    basename, ext = os.path.splitext(file)
    if ext.lower() in ('.json'):
        with open(filename) as file:
            return read_json_w3.Read_CSkeletalAnimationSet(json.loads(file.read()))
    elif ext.lower().endswith('.w2anims'):
        return load_bin_anims(filename, rigPath)
    else:
        anim = None
    return anim

def import_lipsync(context, fileName = False, load_from_data = False, use_NLA=True, NLA_track="mimic_import"):
    if fileName:
        dirpath, file = os.path.split(fileName)
        basename, ext = os.path.splitext(file)
        if ext.lower().endswith('.cr2w'):
            lipsync_CSkeletalAnimation =  load_lipsync_file(fileName)
            anim_set_entry = w3_types.CSkeletalAnimationSetEntry()
            lipsync_CSkeletalAnimation.name = getFilenameFile(lipsync_CSkeletalAnimation.name)
            anim_set_entry.name = lipsync_CSkeletalAnimation.name
            anim_set_entry.animation = lipsync_CSkeletalAnimation
            import_anim(context, "lipsync", anim_set_entry, use_NLA=use_NLA, NLA_track=NLA_track)
    log.info('Lipsync loaded')
    return {'FINISHED'}
    
def start_import(context, fileName = False, load_from_data = False, rigPath = r"E:\w3.modding\modkit\r4data\characters\base_entities\woman_base\woman_base.w2rig"):
    if fileName.endswith('.w2anims') or fileName.endswith('.json'):
        pass
    else:
        fileName = r"E:\w3.modding\modkit\r4data\animations\man\combat\man_geralt_gabriel.w2anims.json"
    if fileName:
        animSetTemplate = import_w3_animSet(fileName, rigPath)
    elif load_from_data:
        animSetTemplate = load_from_data
    else:
        print("populate_animSet error")
        return
    #!DELETE
    # with open(r"F:\RE3R_MODS\Blender_Scripts\io_import_w2l\test_w2anims.json", "w") as file:
    #     file.write(json.dumps(animSetTemplate,indent=2, default=vars, sort_keys=False))

    treeList = context.scene.demo_list
    treeList.clear()
    global GLOBAL_ANIMSET
    GLOBAL_ANIMSET = animSetTemplate
    for node in animSetTemplate.animations:
        item = NewListItem(treeList, node)

    #animSetTemplate.animations[0]
    
    
