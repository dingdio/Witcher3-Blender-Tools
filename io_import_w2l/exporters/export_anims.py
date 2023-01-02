import re
import json
import collections
import struct
from typing import List, Optional, Set

import bpy
import mathutils
from mathutils import Vector, Quaternion, Euler

from io_import_w2l.setup_logging_bl import *
log = logging.getLogger(__name__)

from io_import_w2l.w3_armature_constants import human_bone_order
from io_import_w2l.CR2W import w3_types

class _FCurve:
    @staticmethod
    def __x_co_0(x: bpy.types.Keyframe):
        return x.co[0]

    def __init__(self, default_value):
        self.__default_value = default_value
        self.__fcurve: Optional[bpy.types.FCurve] = None
        self.__sorted_keyframe_points: Optional[List[bpy.types.Keyframe]] = None

    def setFCurve(self, fcurve: bpy.types.FCurve):
        assert(fcurve.is_valid and self.__fcurve is None)
        self.__fcurve = fcurve
        self.__sorted_keyframe_points: List[bpy.types.Keyframe] = sorted(self.__fcurve.keyframe_points, key=self.__x_co_0)

    def frameNumbers(self):
        sorted_keyframe_points = self.__sorted_keyframe_points
        result: Set[int] = set()
        if sorted_keyframe_points is None:
            return result

        if len(sorted_keyframe_points) == 0:
            return result

        kp1 = sorted_keyframe_points[0]
        result.add(int(kp1.co[0]+0.5))

        kp0 = kp1
        for kp1 in sorted_keyframe_points[1:]:
            result.add(int(kp1.co[0]+0.5))
            if kp0.interpolation != 'LINEAR' and kp1.co.x - kp0.co.x > 2.5:
                if kp0.interpolation == 'CONSTANT':
                    result.add(int(kp1.co[0]-0.5))
            kp0 = kp1

        return result

    def sampleFrames(self, frame_numbers: List[int]):
        # assume set(frame_numbers) & set(self.frameNumbers()) == set(self.frameNumbers())
        fcurve = self.__fcurve
        if fcurve is None or len(fcurve.keyframe_points) == 0: # no key frames
            return [[self.__default_value, ((20, 20), (107, 107))] for _ in frame_numbers]

        result = list()

        evaluate = fcurve.evaluate
        frame_iter = iter(frame_numbers)
        prev_kp = None
        prev_i = None
        kp: bpy.types.Keyframe
        for kp in self.__sorted_keyframe_points:
            i = int(kp.co[0]+0.5)
            if i == prev_i:
                prev_kp = kp
                continue
            prev_i = i
            frames = []
            while True:
                frame = next(frame_iter)
                frames.append(frame)
                if frame >= i:
                    break
            assert(len(frames) >= 1 and frames[-1] == i)
            if prev_kp is None:
                for f in frames: # starting key frames
                    result.append([kp.co[1], ((20, 20), (107, 107))])
            elif len(frames) == 1:
                result.append([kp.co[1], ((20, 20), (107, 107))])
            else:
                for f in frames:
                    result.append([evaluate(f), ((20, 20), (107, 107))])
            prev_kp = kp

        prev_kp_co_1 = prev_kp.co[1]
        result.extend([[prev_kp_co_1, ((20, 20), (107, 107))] for _ in frame_iter])
        
        return result

class _AnimationBase(collections.defaultdict):
    def __init__(self):
        collections.defaultdict.__init__(self, list)

    @staticmethod
    def frameClass():
        raise NotImplementedError

class BoneAnimation(_AnimationBase):
    def __init__(self):
        _AnimationBase.__init__(self)

    @staticmethod
    def frameClass():
        return BoneFrameKey

class BoneFrameKey:
    def __init__(self):
        self.frame_number = 0
        self.location = []
        self.rotation = []
        self.scale = []

    def __repr__(self):
        return '<BoneFrameKey frame %s, loa %s, rot %s , scl %s>'%(
            str(self.frame_number),
            str(self.location),
            str(self.rotation),
            str(self.scale),
            )

class W3AnimationExporter:
    def __init__(self):
        self.__scale = 1
        self.__frame_start = 1
        self.__frame_end = float('inf')
        self.__bone_order = human_bone_order

    def __allFrameKeys(self, curves: List[_FCurve]):
        all_frames = set()
        for i in curves:
            all_frames |= i.frameNumbers()

        if len(all_frames) == 0:
            return

        frame_start = min(all_frames)
        if frame_start != self.__frame_start:
            frame_start = self.__frame_start
            all_frames.add(frame_start)

        frame_end = max(all_frames)
        if frame_end > self.__frame_end:
            frame_end = self.__frame_end
            all_frames.add(frame_end)

        all_frames = sorted(all_frames)
        all_keys = [i.sampleFrames(all_frames) for i in curves]
        #return zip(all_frames, *all_keys)
        for data in zip(all_frames, *all_keys):
            frame_number = data[0]
            if frame_number < frame_start:
                continue
            if frame_number > frame_end:
                break
            yield data

    @staticmethod
    def __minRotationDiff(prev_q, curr_q):
        t1 = (prev_q.w - curr_q.w)**2 + (prev_q.x - curr_q.x)**2 + (prev_q.y - curr_q.y)**2 + (prev_q.z - curr_q.z)**2
        t2 = (prev_q.w + curr_q.w)**2 + (prev_q.x + curr_q.x)**2 + (prev_q.y + curr_q.y)**2 + (prev_q.z + curr_q.z)**2
        #t1 = prev_q.rotation_difference(curr_q).angle
        #t2 = prev_q.rotation_difference(-curr_q).angle
        return -curr_q if t2 < t1 else curr_q

    def __exportBoneAnimation(self, armObj):
        if armObj is None:
            return None
        animation_data = armObj.animation_data
        if animation_data is None or animation_data.action is None:
            logging.warning('[WARNING] armature "%s" has no animation data', armObj.name)
            return None

        vmd_bone_anim = BoneAnimation()

        anim_bones = {}
        rePath = re.compile(r'^pose\.bones\["(.+)"\]\.([a-z_]+)$')
        prop_rotation_map = {'QUATERNION':'rotation_quaternion', 'AXIS_ANGLE':'rotation_axis_angle'}
        for fcurve in animation_data.action.fcurves:
            m = rePath.match(fcurve.data_path)
            if m is None:
                continue
            bone = armObj.pose.bones.get(m.group(1), None)
            if bone is None:
                logging.warning(' * Bone not found: %s', m.group(1))
                continue
            if bone.is_mmd_shadow_bone:
                continue
            prop_name = m.group(2)
            if prop_name not in {'location', prop_rotation_map.get(bone.rotation_mode, 'rotation_euler')}:
                continue

            if bone not in anim_bones:
                data = list(bone.location)
                if bone.rotation_mode == 'QUATERNION':
                    data += list(bone.rotation_quaternion)
                elif bone.rotation_mode == 'AXIS_ANGLE':
                    data += list(bone.rotation_axis_angle)
                else:
                    data += ([bone.rotation_mode] + list(bone.rotation_euler))
                anim_bones[bone] = [_FCurve(i) for i in data] # x, y, z, rw, rx, ry, rz
            bone_curves = anim_bones[bone]
            if prop_name == 'location': # x, y, z
                bone_curves[fcurve.array_index].setFCurve(fcurve)
            elif prop_name == 'rotation_quaternion': # rw, rx, ry, rz
                bone_curves[3+fcurve.array_index].setFCurve(fcurve)
            elif prop_name == 'rotation_axis_angle': # rw, rx, ry, rz
                bone_curves[3+fcurve.array_index].setFCurve(fcurve)
            elif prop_name == 'rotation_euler': # mode, rx, ry, rz
                bone_curves[3+fcurve.array_index+1].setFCurve(fcurve)
        for bone, bone_curves in anim_bones.items():
            key_name = bone.name
            frame_keys = vmd_bone_anim[key_name]
            prev_rot = None
            for frame_number, x, y, z, rw, rx, ry, rz in self.__allFrameKeys(bone_curves):
                key = BoneFrameKey()
                key.frame_number = frame_number - self.__frame_start
                
                #!MOVE THIS TO BONE VECTOR MATRIX METHOD
                bl_bone = bone
                if bl_bone.parent:
                    objectMatrix = bl_bone.parent.bone.matrix_local.inverted()
                else:
                    objectMatrix = bl_bone.bone.matrix_local.inverted()
                the_vec = Vector([x[0], y[0], z[0]])
                co = objectMatrix @ bl_bone.bone.matrix_local @ the_vec
                key.location = Vector([co[0], co[1], co[2]])
                quat = Quaternion([ rw[0], rx[0], ry[0], rz[0]])
                ro = objectMatrix @ bl_bone.bone.matrix_local @ quat.to_matrix().to_4x4()
                ro = ro.to_quaternion()
                curr_rot = ro
                if prev_rot is not None:
                    curr_rot = self.__minRotationDiff(prev_rot, curr_rot)
                prev_rot = curr_rot
                key.rotation = [ro.x, ro.y, ro.z, -ro.w]
                key.scale = [1.0, 1.0, 1.0]
                frame_keys.append(key)
            logging.info('(bone) frames:%5d  name: %s', len(frame_keys), key_name)
        logging.info('---- bone animations:%5d  source: %s', len(vmd_bone_anim), armObj.name)
        return vmd_bone_anim
    
    def __reduceFrames(self, frames):
        firstFrame = frames[0]
        
        for frame in frames:
            if firstFrame.location == frame.location and firstFrame.rotation == frame.rotation and firstFrame.scale == frame.scale:
                continue
            else:
                return (frames, False)
        return ([firstFrame], True)
        
    def __save(self, filepath, action_name, single_action):
        
        bones = []
        longestnumframes = 0
        #for name, frames in self.boneAnimation.items():
        
        #get total frames first
        for name in self.__bone_order:
            frames = self.boneAnimation[name]
            if longestnumframes > len(frames):
                pass
            else:
                longestnumframes = len(frames)
        
        for name in self.__bone_order:
            frames = self.boneAnimation[name]
            positionFrames = []
            rotationFrames = []
            scaleFrames = []
            (frames, is_reduced) = self.__reduceFrames(frames)
            for frame in frames:
                positionFrames.append({
                            "x": round(frame.location.x, 8), 
                            "y": round(frame.location.y, 8),  
                            "z": round(frame.location.z, 8), 
                        })
                rotationFrames.append({ 
                            "X": round(frame.rotation[0], 11), 
                            "Y": round(frame.rotation[1], 11),  
                            "Z": round(frame.rotation[2], 11), 
                            "W": round(frame.rotation[3], 11), 
                        })
                scaleFrames.append({
                            "x": 1.0, 
                            "y": 1.0, 
                            "z": 1.0, 
                        })
                
            
            position_dt = 0.0333333351
            rotation_dt = 0.0333333351
            scale_dt = 0.0333333351
            if len(positionFrames) != longestnumframes and len(positionFrames) !=1:
                position_dt = 0.06666667
            if len(rotationFrames) != longestnumframes and len(rotationFrames) !=1:
                rotation_dt = 0.06666667
            if len(scaleFrames) != longestnumframes and len(scaleFrames) !=1:
                scale_dt = 0.06666667
                
            boneframes = w3_types.w2AnimsFrames(
                id = name,
                BoneName = name, #boneName,
                position_dt = position_dt,
                position_numFrames = len(positionFrames),
                positionFrames = positionFrames,
                rotation_dt = rotation_dt,
                rotation_numFrames = len(rotationFrames),
                rotationFrames = rotationFrames,
                scale_dt = scale_dt,
                scale_numFrames = len(scaleFrames), 
                scaleFrames = scaleFrames,
                rotationFramesQuat = None
            )
            bones.append(boneframes)
        CBuffer = w3_types.CAnimationBufferBitwiseCompressed()
        CBuffer.bones = bones
        CBuffer.numFrames = longestnumframes
        CBuffer.duration = longestnumframes * 0.0333333351
        CAnimation = w3_types.CSkeletalAnimation(action_name,
                                                CBuffer.duration,
                                                30.0,
                                                CBuffer)
        del CAnimation.motionExtraction
        CSetEntry = w3_types.CSkeletalAnimationSetEntry(CAnimation)
        if not single_action:
            CSkeletalAnimationSet = w3_types.CSkeletalAnimationSet([CSetEntry])
        else:
            CSkeletalAnimationSet = CSetEntry
        print('Exporting Animation')

        with open(filepath, "w") as file:
            file.write(json.dumps(CSkeletalAnimationSet.__dict__, default=lambda obj: obj.__json_serializable__() if hasattr(obj, "__json_serializable__") else obj.__dict__ ,indent=2, sort_keys=False))
    
    def export(self, **args):
        armature = args.get('armature', None)
        filepath = args.get('filepath', '')
        single_action = args.get('single_action', False)

        self.__scale = args.get('scale', 1.0)

        if args.get('use_frame_range', False):
            self.__frame_start = bpy.context.scene.frame_start
            self.__frame_end = bpy.context.scene.frame_end

        #TODO find better way to not lose animation skeleton bone order?
        if len(armature.data.witcherui_RigSettings.bone_order_list):
            self.__bone_order.clear()
            for bone in armature.data.witcherui_RigSettings.bone_order_list:
                self.__bone_order.append(bone.name)

        if armature:
            self.boneAnimation = self.__exportBoneAnimation(armature)
            curr_action = (armature.animation_data.action
                if armature.animation_data is not None and
                armature.animation_data.action is not None
                else None)
            self.__save(filepath, curr_action.name, single_action)

def export_w3_anim(context, savePath):
    selected_objects = set(context.selected_objects)

    for obj in selected_objects:
        if obj.type == 'ARMATURE':
            armObj = obj
            break
    if armObj:
        curr_action = (armObj.animation_data.action
            if armObj.animation_data is not None and
            armObj.animation_data.action is not None
            else None)
    
        exporter = W3AnimationExporter()
        exporter.export(armature = armObj,
                        filepath = savePath,
                        use_frame_range = True,
                        single_action = True)
        log.info(f'Finished Exporting {curr_action.name}')
    return {'FINISHED'}