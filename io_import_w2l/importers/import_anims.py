from io_import_w2l import auto_scene_setup
from io_import_w2l.file_helpers import getFilenameFile, rm_ns
from io_import_w2l.CR2W import read_json_w3
from io_import_w2l.CR2W import w3_types
from io_import_w2l.CR2W.dc_anims import load_bin_anims, load_lipsync_file
from io_import_w2l.CR2W.CR2W_helpers import Enums
from io_import_w2l.setup_logging_bl import *
log = logging.getLogger(__name__)

from io_import_w2l.importers.import_rig import get_ordered_bones


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


from enum import Enum
class AnimationBufferType(Enum):
    Normal = 0
    Multi = 1

class AnimImporter:
    def __init__(self, filepath, SetEntry:w3_types.CSkeletalAnimationSetEntry, scale=1.0, use_pose_mode=False, use_NLA=False, facePose=False, NLA_track = 'anim_import', at_frame = 0):
        self.__animFile = animFile()
        self.__animFile.load(filepath=filepath)
        #log.debug(str(self.__animFile.header))
        self.__scale = scale
        self.__SetEntry = SetEntry
        self.__use_NLA = use_NLA
        self.__NLA_track = NLA_track
        self.__NLA_frame_margin = at_frame
        self.__frame_margin = 0 
        self.__AnimationBufferType = AnimationBufferType.Normal
        if type(SetEntry.animation.animBuffer) == w3_types.CAnimationBufferMultipart:
            self.__AnimationBufferType = AnimationBufferType.Multi
        self.__frame_current = 0
        self.facePose = facePose

    def __assign_action(self, target: Union[bpy.types.ID, HasAnimationData], action: bpy.types.Action):
        if target.animation_data is None:
            target.animation_data_create()

        if not self.__use_NLA:
            target.animation_data.action = action
        else:
            #frame_current = bpy.context.scene.frame_current
            if self.__NLA_track:
                target_track: bpy.types.NlaTrack = target.animation_data.nla_tracks.get(self.__NLA_track)
                if target_track is None:
                    target_track: bpy.types.NlaTrack = target.animation_data.nla_tracks.new()
                    target_track.name = self.__NLA_track #action.name
                if self.__AnimationBufferType == AnimationBufferType.Multi and self.__frame_current !=0 or self.__NLA_frame_margin !=0:
                    pass # adding multiple strips
                else:
                    for strip in target_track.strips:
                        target_track.strips.remove(strip)
            else:
                target_track: bpy.types.NlaTrack = target.animation_data.nla_tracks.new()
                target_track.name = action.name
                
            self.__frame_current = self.__NLA_frame_margin + self.__frame_current
            
            last_strip = target_track.strips[-1] if len(target_track.strips) else None
            try:
                target_strip = target_track.strips.new(action.name, int(self.__frame_current + 1), action)
            except Exception as e:
                target_strip = target_track.strips.new(action.name, int(last_strip.frame_end + 1), action)
            target_strip.frame_start = self.__frame_current
            start_frame, end_frame = action.frame_range
            length = end_frame - start_frame
            target_strip.frame_end = self.__frame_current + length
            target_strip.blend_type = 'REPLACE'
            
            if self.__NLA_track:
                if self.__NLA_track == 'mimic_import' or self.__NLA_track == 'voice_import' or self.__NLA_track == 'anim_import':
                    target_strip.blend_type = 'COMBINE'
            # try:
            #     __frame_start = self.__NLA_frame_margin + self.__frame_current #TODO exact float start
            #     target_strip = target_track.strips.new(action.name, int(__frame_start), action)
            # except Exception as e:
            #     raise e
            # target_strip.blend_type = 'COMBINE'

    def __assignPartToArmature(self, armObj, SkeletalAnimation, SkeletalAnimationData, armature_namespace, SkeletalAnimationType, scale):
        
        #!TODO get rid of this...
        face_animation = False
        camera_animation = False
        if(SkeletalAnimationData.tracks):
            face_animation = True
            for track in SkeletalAnimationData.tracks:
                if track.trackName == "hctFOV":
                    face_animation = False
                    camera_animation = True
                    break

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

            #! SCALE FRAMES
            bone_frames = len(bone.scaleFrames)
            if bone_frames == 1 and self.facePose and bone.scaleFrames[0].count(1.0) == 3:
                pass
            else:
                if bone.scaleFrames[0].count(1.0) != 3:
                    loc_frame_number = 0
                    #frame_skip_loc = round(float(total_frames)/float(len(keyFrames_loc)))
                    frame_skip_loc = float(total_frames)/float(len(keyFrames_loc))

            #! POSITION FRAMES
            bone_frames = len(bone.positionFrames)
            if bone_frames == 1 and self.facePose and bone.positionFrames[0].count(0.0) == 3: #for face animations that pass in 0s that are not used, might cause issues?
                pass
            else:
                loc_frame_number = 0
                #frame_skip_loc = round(float(total_frames)/float(len(keyFrames_loc)))
                frame_skip_loc = float(total_frames)/float(len(keyFrames_loc))
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
                    #Do not add frames past the total number of frames
                    if loc_frame > (total_frames - 1):
                        log.critical('Total loc frames excceded on bone: '+bone.BoneName+' Frame:'+str(loc_frame))
                        loc_frame = (total_frames - 1)
                    for i in range(3):
                        pos_curves[i].keyframe_points.add(1)
                        pos_curves[i].keyframe_points[-1].co = (loc_frame, pos[i])
                        pos_curves[i].keyframe_points[-1].interpolation = 'LINEAR'

            #! ROTATION FRAMES
            
            # if bone.BoneName == "r_pinky1":
            #     pass
            bone_frames = len(bone.rotationFrames)
            if bone_frames == 1 and self.facePose and shouldIgnoreFrame(bone): #for face animations that pass in 0s that are not used, might cause issues?
                pass
            else:
                rot_frame_number = 0
                #frame_skip_rotation = round(float(total_frames)/float(len(keyFrames_rot)))
                frame_skip_rotation = float(total_frames)/float(len(keyFrames_rot))
                
                
                if (len(keyFrames_rot) * frame_skip_rotation) > total_frames:
                    log.critical('Found bone with too many frames')
                
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
                        attempt_to_fix_rotation = True
                        
                        if attempt_to_fix_rotation:
                            def detect_up_axis(quaternion):
                                rotation_matrix = quaternion.to_matrix().to_4x4()
                                up_vector = Vector((0.0, 1.0, 0.0))
                                up_axis = up_vector @ rotation_matrix
                                
                                if abs(up_axis.y) > abs(up_axis.z):
                                    if up_axis.y > 0:
                                        return "Y+"
                                    else:
                                        return "Y-"
                                else:
                                    if up_axis.z > 0:
                                        return "Z+"
                                    else:
                                        return "Z-"
                            def detect_forward_vector(quaternion):
                                rotation_matrix = quaternion.to_matrix().to_4x4()
                                forward_vector = Vector((0.0, 0.0, -1.0))
                                forward_axis = forward_vector @ rotation_matrix
                                
                                if abs(forward_axis.x) > abs(forward_axis.y) and abs(forward_axis.x) > abs(forward_axis.z):
                                    if forward_axis.x > 0:
                                        return "X+"
                                    else:
                                        return "X-"
                                elif abs(forward_axis.y) > abs(forward_axis.z):
                                    if forward_axis.y > 0:
                                        return "Y+"
                                    else:
                                        return "Y-"
                                else:
                                    if forward_axis.z > 0:
                                        return "Z+"
                                    else:
                                        return "Z-"
                            up_axis = detect_up_axis(fixed_rot)
                            forward_vec = detect_forward_vector(fixed_rot)
                            if up_axis == 'Y+' and forward_vec == 'Z-':
                                pass # s
                            elif up_axis == 'Z+' and forward_vec == 'Y-':
                                fixed_rot.rotate(Euler([math.radians(-90), math.radians(180), math.radians(0)]))
                            elif up_axis == 'Z-' and forward_vec == 'Y-':
                                fixed_rot.rotate(Euler([math.radians(-90), math.radians(0), math.radians(0)]))
                            elif up_axis == 'Z' and forward_vec == 'Z-':
                                pass
                            elif up_axis == 'Z':
                                fixed_rot.rotate(Euler([math.radians(-90), math.radians(0), math.radians(0)]))
                                #fixed_rot.rotate(Quaternion((0.7071068, -0.7071068, 0, 0)))
                                degrees =  tuple(math.degrees(a) for a in fixed_rot.to_euler("XYZ"))
                                #root_bone.rotation_euler = (math.pi / 2.0, 0.0, 0.0)
                                # armObj.rotation_euler.x = math.radians(0)
                                # armObj.rotation_euler.y = math.radians(0)
                                # armObj.rotation_euler.z = np.pi
                                
                                # if degrees[0] <= -88 and degrees[0] >= -92:
                                #     armObj.rotation_euler.x = math.radians(90)
                                # elif degrees[0] > 88 and degrees[0] <92:
                                #     armObj.rotation_euler.x = math.radians(-90)
                                #     # if degrees[2] < -174 and degrees[2] > -181:
                                #     #     armObj.rotation_euler.x = math.radians(90)
                                #     #     armObj.rotation_euler.z = math.radians(0)
                                #     # elif degrees[0] > 88 and degrees[0] <92 and degrees[2] > 178 and degrees[2] < 181:
                                #     #     armObj.rotation_euler.x = math.radians(90)
                                #     #     if degrees[1] > 0:
                                #     #         armObj.rotation_euler.z = math.radians(0)
                                #     #     else:
                                #     #         armObj.rotation_euler.z = math.radians(180)
                                # if degrees[1] > 178 and degrees[1] < 181:
                                #     armObj.rotation_euler.z = math.radians(0)
                                # if degrees[2] > 88 and degrees[2] < 120:
                                #     armObj.rotation_euler.y = math.radians(-90)
                                #     armObj.rotation_euler.z = math.radians(-90)
                                # elif degrees[2] < -88 and degrees[2] > -120:
                                #     armObj.rotation_euler.y = math.radians(90)
                                #     armObj.rotation_euler.z = math.radians(90)
                            
                    #Do not add frames past the total number of frames
                    if frame > (total_frames - 1):
                        log.critical('Total frames excceded on bone: '+bone.BoneName+' Frame:'+str(frame))
                        frame = (total_frames - 1)
                    for i in range(4):
                        rot_curves[i].keyframe_points.add(1)
                        rot_curves[i].keyframe_points[-1].co = (frame, fixed_rot[i])
                        rot_curves[i].keyframe_points[-1].interpolation = 'LINEAR'
        bpy.ops.object.mode_set(mode='OBJECT')
        log.info(' Finished adding keyframes in %f seconds.', time.time() - start_time)

        control_bone_name = "w3_face_poses"
        if camera_animation:
            control_bone_name = "Camera_Node"
        AnimTracks = SkeletalAnimationData.tracks
        if AnimTracks and len(AnimTracks) > 1:
            log.info('---- morph animations:%5d  target: %s', len(AnimTracks), armObj.name)

            if control_bone_name not in armObj.pose.bones:
                log.warning('No shape key control bone. Add shape keys to '+armObj.name)
            else:
                mirror_map = {}#_MirrorMapper(meshObj.data.shape_keys.key_blocks) if self.__mirror else {}
                shapeKeyDict = {k:mirror_map.get(k, v) for k, v in armObj.pose.bones[control_bone_name].items()}


                for track in AnimTracks:
                    keyFrames = track.trackFrames
                    if len(keyFrames) == 0:
                        continue
                    name = track.trackName
                    if name not in shapeKeyDict:
                        log.warning('WARNING: not found shape key %s (%d frames)', name, len(keyFrames))
                        continue

                    #Info
                    track_frames = len(track.trackFrames)
                    total_frames = SkeletalAnimationData.numFrames
                    #frame_skip = round(float(total_frames)/float(track_frames))
                    frame_skip = float(total_frames)/float(track_frames)

                    log.info('(mesh) frames:%5d  name: %s', len(keyFrames), name)
                    shapeKey = shapeKeyDict[name]
                    fcurve = action.fcurves.new(data_path='pose.bones["%s"]["%s"]'% (control_bone_name, name))#  (data_path='key_blocks["%s"].value'%shapeKey.name)
                    fcurve.keyframe_points.add(len(keyFrames))
                    #keyFrames.sort(key=lambda x:x.frame_number)

                    frame_number = 0

                    if (len(keyFrames) * frame_skip) > total_frames:
                        log.critical('Found bone with too many tracks')

                    #Do not add frames past the total number of frames
                    if frame_number > (total_frames - 1):
                        frame_number = (total_frames - 1)
                    for k, v in zip(keyFrames, fcurve.keyframe_points):
                        v.co = (frame_number+self.__frame_margin, k)
                        v.interpolation = 'LINEAR'
                        frame_number += frame_skip * 1
                    # weights = tuple(i for i in keyFrames)
                    # shapeKey.slider_min = min(shapeKey.slider_min, floor(min(weights)))
                    # shapeKey.slider_max = max(shapeKey.slider_max, ceil(max(weights)))
        
        
        
        self.__assign_action(armObj, action)
        
        # def detect_up_axis(armature):
        #     x_axis = [1, 0, 0]
        #     y_axis = [0, 1, 0]
        #     z_axis = [0, 0, 1]
            
        #     origin = armature.location
            
        #     # Reset the rotation of the armature
        #     tmp_eular = armature.rotation_euler
        #     armature.rotation_euler = (0.0, 0.0, 0.0)
        #     bpy.context.view_layer.update()
        #     # for bone in armature.pose.bones:
        #     #     if bone.parent:
        #     #         bone.matrix = bone.matrix_basis
        #     x_sum = 0
        #     y_sum = 0
        #     z_sum = 0
            
        #     for bone in armature.pose.bones:
        #         head = bone.head + origin
        #         tail = bone.tail + origin
                
        #         x_sum += head[0] - tail[0]
        #         y_sum += head[1] - tail[1]
        #         z_sum += head[2] - tail[2]
        #         break
                
        #     armature.rotation_euler = tmp_eular
                
        #     if x_sum > 0 and abs(x_sum) > abs(y_sum) and abs(x_sum) > abs(z_sum):
        #         return x_axis
        #     elif x_sum < 0 and abs(x_sum) > abs(y_sum) and abs(x_sum) > abs(z_sum):
        #         return [-x for x in x_axis]
        #     elif y_sum > 0 and abs(y_sum) > abs(x_sum) and abs(y_sum) > abs(z_sum):
        #         return y_axis
        #     elif y_sum < 0 and abs(y_sum) > abs(x_sum) and abs(y_sum) > abs(z_sum):
        #         return [-y for y in y_axis]
        #     elif z_sum > 0:
        #         return z_axis
        #     else:
        #         return [-z for z in z_axis]
        # up_axis = detect_up_axis(armObj)
        # import mathutils
        # def rotate_root_bone_keyframes(armature, action):
        #     root_bone = armature.pose.bones[0]
        #     rotation_quaternion = Quaternion((0.7071068, -0.7071068, 0, 0))
        #     for fcurve in action.fcurves:
        #         if fcurve.data_path.startswith(root_bone.path_from_id()):
        #             for keyframe in fcurve.keyframe_points:
        #                 frame = keyframe.co[0]
        #                 if fcurve.data_path.endswith(".rotation_quaternion"):
        #                     value = Quaternion(keyframe.co[1:5])
        #                     keyframe.co = (frame, value * rotation_quaternion.normalized())
        #     bpy.context.view_layer.update()
        
        # if up_axis[2] == -1:
        #     #armObj.rotation_euler = (-(math.pi / 2.0), 0.0, 0.0)
        #     rotate_root_bone_keyframes(armObj, action)
        # elif up_axis[2] == -1:
        #     pass
        #     #armObj.rotation_euler = ((math.pi / 2.0), 0.0, 0.0)

    def __assignToArmature(self, armObj, action_name=None):

        def detect_maya_namespace(s):
            if ':' in s:
                srp = s.rpartition(':')
                return srp[0]+":"
            else:
                return None
        SkeletalAnimationType = self.__SetEntry.animation.SkeletalAnimationType
        AdditiveType = self.__SetEntry.animation.AdditiveType
        armature_namespace = detect_maya_namespace(armObj.pose.bones[0].name)
        scale = 1.0
        extra_frame = 1 if self.__frame_margin > 1 else 0
        SkeletalAnimation = self.__SetEntry.animation
        SkeletalAnimationData = SkeletalAnimation.animBuffer

        if self.__AnimationBufferType == AnimationBufferType.Multi:
            # self.__use_NLA = False #! TEMP
            # self.__frame_current = 0
            # self.__assignPartToArmature(armObj, SkeletalAnimation, SkeletalAnimationData.parts[0], armature_namespace, SkeletalAnimationType, scale)
            
            self.__use_NLA = True #! TEMP
            for idx, bufferpart in enumerate(SkeletalAnimationData.parts):
                populate_names(armObj, bufferpart)
                
                self.__frame_current = SkeletalAnimationData.firstFrames[idx]
                self.__assignPartToArmature(armObj, SkeletalAnimation, bufferpart, armature_namespace, SkeletalAnimationType, scale)
        else:
            populate_names(armObj, SkeletalAnimationData)
            self.__assignPartToArmature(armObj, SkeletalAnimation, SkeletalAnimationData, armature_namespace, SkeletalAnimationType, scale)
        
        

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

#TODO fix how this works
def populate_names(armObj, SkeletalAnimationData):
    ordered_bones = get_ordered_bones(armObj)
    witcher_tracks_list = armObj.data.witcherui_RigSettings.witcher_tracks_list
    
    for bidx, bone in enumerate(ordered_bones):
        if bidx < len(SkeletalAnimationData.bones) and type(SkeletalAnimationData.bones[bidx].BoneName) == int:
            SkeletalAnimationData.bones[bidx].BoneName = bone.name
        else:
            break
    for tidx, track in enumerate(witcher_tracks_list):
        if tidx < len(SkeletalAnimationData.tracks) and type(SkeletalAnimationData.tracks[tidx].trackName) == int:
            SkeletalAnimationData.tracks[tidx].trackName = track.name
        else:
            break

def NewListItem( treeList, node):
    item = treeList.add()
    item.name = node.animation.name
    item.framesPerSecond = node.animation.framesPerSecond
    item.numFrames = node.animation.animBuffer.numFrames
    item.duration = node.animation.duration
    item.SkeletalAnimationType = node.animation.SkeletalAnimationType
    if node.animation.AdditiveType:
        item.AdditiveType = node.animation.AdditiveType
    return item

def import_anim(context, fileName, AnimationSetEntry, facePose=False, use_NLA=False, override_select = False, update_scene_settings = True, NLA_track = 'anim_import', at_frame = 0):
    if not override_select:
        selected_objects = set(context.selected_objects)
    else:
        if override_select.__class__.__name__ == 'list':
            selected_objects = override_select
        else:
            selected_objects = [override_select]
    start_time = time.time()
    
    if type(AnimationSetEntry.animation.animBuffer) == w3_types.CAnimationBufferMultipart:
        use_NLA = True
    
    importer = AnimImporter(fileName, AnimationSetEntry, use_NLA=use_NLA, facePose=facePose, NLA_track = NLA_track, at_frame=at_frame)
    for i in selected_objects:
        importer.assign(i)
    log.info(' Finished importing motion in %f seconds.', time.time() - start_time)

    update_scene_settings = True # MAKE BLEND IMPORT PROP
    if update_scene_settings:
        auto_scene_setup.setupFrameRanges(use_NLA)
        auto_scene_setup.setupFps()
    context.scene.frame_set(context.scene.frame_current)
    return {'FINISHED'}

global GLOBAL_ANIMSET

def get_global_set():
    global GLOBAL_ANIMSET
    return GLOBAL_ANIMSET

def set_global_set(the_set):
    global GLOBAL_ANIMSET
    GLOBAL_ANIMSET = the_set

def import_from_list_item(context, item):
    for anim_set_entry in GLOBAL_ANIMSET.animations:
        if anim_set_entry.animation.name == item.name:
            if ':face:' in anim_set_entry.animation.name:
                import_anim(context, "lipsync_from_list", anim_set_entry, use_NLA=True, NLA_track="mimic_import")
            else:
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

def import_lipsync(context, fileName = False, load_from_data = False, use_NLA=True, NLA_track="mimic_import", override_select = None, at_frame = 0):
    if fileName:
        dirpath, file = os.path.split(fileName)
        basename, ext = os.path.splitext(file)
        if ext.lower().endswith('.cr2w'):
            lipsync_CSkeletalAnimation =  load_lipsync_file(fileName)
            anim_set_entry = w3_types.CSkeletalAnimationSetEntry()
            lipsync_CSkeletalAnimation.name = getFilenameFile(lipsync_CSkeletalAnimation.name)
            anim_set_entry.name = lipsync_CSkeletalAnimation.name
            anim_set_entry.animation = lipsync_CSkeletalAnimation
            import_anim(context, "lipsync", anim_set_entry, use_NLA=use_NLA, NLA_track=NLA_track, override_select = override_select, at_frame = at_frame)
    log.info('Lipsync loaded')
    return {'FINISHED'}
    
def start_import(context, fileName = False, load_from_data = False, rigPath = None):
    if fileName:
        animSetTemplate = import_w3_animSet(fileName, rigPath)
    elif load_from_data:
        animSetTemplate = load_from_data
    else:
        log.critical("populate_animSet error")
        return

    treeList = context.scene.demo_list
    treeList.clear()
    global GLOBAL_ANIMSET
    GLOBAL_ANIMSET = animSetTemplate
    for node in animSetTemplate.animations:
        item = NewListItem(treeList, node)
