import logging
import os
import re
import json
import math
import collections
import struct
from typing import Dict, List, Optional, Set, Tuple

import bpy
import mathutils
from mathutils import Vector, Quaternion, Euler, Matrix
from ..action_compat import iter_action_fcurves

log = logging.getLogger(__name__)

from ..w3_armature_constants import human_bone_order
from ..CR2W import w3_types
from ..CR2W import anims_builder, cr2w_writer
from ..importers.motion_tools import cline_from_per_frame
from .. import get_rig_rot90_enabled
from ..ui.armature_context import get_main_armature


def _remap_cline_rot90(frames, flags):
    """Remap CLineMotionExtraction2 frames from Blender to game axes (rot90).

    Blender→Game: game_x = -blender_y, game_y = blender_x, game_z = blender_z
    Flag bits: 0=X, 1=Y, 2=Z, 3=Yaw
    """
    n_components = bin(flags & 0xF).count('1')
    if n_components == 0:
        return frames, flags
    n_keyframes = len(frames) // n_components

    # Decompose interleaved frames into per-axis arrays
    blender_axes = {}
    axis_bits = [('x', 1), ('y', 2), ('z', 4), ('yaw', 8)]
    idx = 0
    for kf in range(n_keyframes):
        for key, bit in axis_bits:
            if flags & bit:
                blender_axes.setdefault(key, []).append(frames[idx])
                idx += 1

    # Remap to game axes
    game_x = [-v for v in blender_axes['y']] if (flags & 2) else []
    game_y = list(blender_axes.get('x', [])) if (flags & 1) else []
    game_z = list(blender_axes.get('z', [])) if (flags & 4) else []
    game_yaw = list(blender_axes.get('yaw', [])) if (flags & 8) else []

    new_flags = 0
    if game_x: new_flags |= 1
    if game_y: new_flags |= 2
    if game_z: new_flags |= 4
    if game_yaw: new_flags |= 8

    # Recompose interleaved frames in game axis order
    new_frames = []
    for kf in range(n_keyframes):
        if new_flags & 1: new_frames.append(game_x[kf])
        if new_flags & 2: new_frames.append(game_y[kf])
        if new_flags & 4: new_frames.append(game_z[kf])
        if new_flags & 8: new_frames.append(game_yaw[kf])

    return new_frames, new_flags


def get_selected_armature(context):
    if context is None:
        return None
    return get_main_armature(context, prefer_active=True, remember=True, fallback=True)


def _get_armature_bone_order(armature) -> List[str]:
    bone_order = list(human_bone_order)
    rig_settings = getattr(armature.data, "witcherui_RigSettings", None) if armature else None
    if rig_settings and len(rig_settings.bone_order_list):
        bone_order = [bone.name for bone in rig_settings.bone_order_list]
    return bone_order


def get_action_slot(armature):
    if not armature:
        return None
    animation_data = armature.animation_data
    if not animation_data:
        return None
    return animation_data.action


def _ordered_tracks(tracks, prefer_tracks=None):
    ordered = []
    seen_ids = set()
    if prefer_tracks:
        for name in prefer_tracks:
            for track in tracks:
                if track.name == name:
                    track_id = id(track)
                    if track_id not in seen_ids:
                        ordered.append(track)
                        seen_ids.add(track_id)
                    break
    for track in tracks:
        track_id = id(track)
        if track_id in seen_ids:
            continue
        ordered.append(track)
        seen_ids.add(track_id)
    return ordered


def _filter_solo_tracks(tracks):
    solo_tracks = [track for track in tracks if getattr(track, "is_solo", False)]
    return solo_tracks if solo_tracks else list(tracks)


def get_nla_action_at_frame(armature, frame=None, prefer_tracks=None):
    if not armature:
        return None, None
    animation_data = armature.animation_data
    if not animation_data or not animation_data.nla_tracks:
        return None, None

    tracks = _filter_solo_tracks(list(animation_data.nla_tracks))
    tracks = _ordered_tracks(tracks, prefer_tracks=prefer_tracks)

    if frame is None:
        try:
            frame = bpy.context.scene.frame_current
        except Exception:
            frame = None
    if frame is None:
        return None, None

    for track in tracks:
        if getattr(track, "mute", False):
            continue
        for strip in reversed(track.strips):
            if getattr(strip, "mute", False):
                continue
            if strip.action is None:
                continue
            if strip.frame_start <= frame <= strip.frame_end:
                return strip.action, {
                    "track": track.name,
                    "strip": strip.name,
                    "frame": frame,
                }
    return None, None


def get_nla_last_action(armature, prefer_tracks=None):
    if not armature:
        return None, None
    animation_data = armature.animation_data
    if not animation_data or not animation_data.nla_tracks:
        return None, None

    tracks = _filter_solo_tracks(list(animation_data.nla_tracks))
    tracks = _ordered_tracks(tracks, prefer_tracks=prefer_tracks)

    for track in tracks:
        if getattr(track, "mute", False):
            continue
        for strip in reversed(track.strips):
            if getattr(strip, "mute", False):
                continue
            if strip.action:
                return strip.action, {
                    "track": track.name,
                    "strip": strip.name,
                }
    return None, None


def resolve_action(armature, context=None, source_mode="AUTO", prefer_tracks=None):
    if not armature:
        return None, {"source": "NONE"}

    if prefer_tracks is None:
        prefer_tracks = ("anim_import",)

    frame = None
    if context and getattr(context, "scene", None):
        frame = context.scene.frame_current

    action_slot = get_action_slot(armature)
    nla_action, nla_info = get_nla_action_at_frame(armature, frame=frame)

    mode = (source_mode or "AUTO").upper()
    if mode == "ACTION":
        if action_slot:
            return action_slot, {"source": "ACTION_SLOT"}
        if nla_action:
            info = {"source": "NLA_FALLBACK"}
            info.update(nla_info or {})
            return nla_action, info
        nla_last, nla_last_info = get_nla_last_action(armature, prefer_tracks=prefer_tracks)
        if nla_last:
            info = {"source": "NLA_LAST_FALLBACK"}
            info.update(nla_last_info or {})
            return nla_last, info
        return None, {"source": "NONE"}

    if mode == "NLA":
        if nla_action:
            info = {"source": "NLA_PLAYING"}
            info.update(nla_info or {})
            return nla_action, info
        nla_last, nla_last_info = get_nla_last_action(armature, prefer_tracks=prefer_tracks)
        if nla_last:
            info = {"source": "NLA_LAST"}
            info.update(nla_last_info or {})
            return nla_last, info
        if action_slot:
            return action_slot, {"source": "ACTION_FALLBACK"}
        return None, {"source": "NONE"}

    if nla_action:
        info = {"source": "NLA_PLAYING"}
        info.update(nla_info or {})
        return nla_action, info
    if action_slot:
        return action_slot, {"source": "ACTION_SLOT"}
    nla_last, nla_last_info = get_nla_last_action(armature, prefer_tracks=prefer_tracks)
    if nla_last:
        info = {"source": "NLA_LAST"}
        info.update(nla_last_info or {})
        return nla_last, info
    return None, {"source": "NONE"}


class _FCurve:
    @staticmethod
    def __x_co_0(x: bpy.types.Keyframe):
        return x.co[0]

    def __init__(self, default_value):
        self.__default_value = default_value
        self.__fcurve: Optional[bpy.types.FCurve] = None
        self.__sorted_keyframe_points: Optional[List[bpy.types.Keyframe]] = None

    def setFCurve(self, fcurve: bpy.types.FCurve):
        if not fcurve.is_valid:
            logging.warning('Skipping invalid FCurve')
            return
        if self.__fcurve is not None:
            logging.warning('FCurve already set, skipping duplicate')
            return
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
        exhausted = False
        kp: bpy.types.Keyframe
        for kp in self.__sorted_keyframe_points:
            i = int(kp.co[0]+0.5)
            if i == prev_i:
                prev_kp = kp
                continue
            prev_i = i
            frames = []
            while True:
                try:
                    frame = next(frame_iter)
                except StopIteration:
                    exhausted = True
                    break
                frames.append(frame)
                if frame >= i:
                    break
            if exhausted or not frames:
                break
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
        self.__bone_order = list(human_bone_order)

    def __allFrameKeys(self, curves: List[_FCurve], frame_numbers: Optional[List[int]] = None):
        if frame_numbers is None:
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
            if self.__frame_end != float('inf') and frame_end > self.__frame_end:
                frame_end = self.__frame_end
                all_frames.add(frame_end)

            all_frames = sorted(all_frames)
        else:
            all_frames = sorted({int(f) for f in frame_numbers})
            if len(all_frames) == 0:
                return
            frame_start = self.__frame_start
            frame_end = self.__frame_end
            # Clamp inf frame_end to actual data range
            if frame_end == float('inf'):
                frame_end = max(all_frames)
            if frame_start not in all_frames:
                all_frames.append(frame_start)
            if frame_end not in all_frames:
                all_frames.append(frame_end)
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

    def __exportBoneAnimation(self, armObj, frame_numbers: Optional[List[int]] = None, action=None, face_animation: bool = False):
        if armObj is None:
            return None
        if action is None:
            animation_data = armObj.animation_data
            if animation_data is None or animation_data.action is None:
                logging.warning('[WARNING] armature "%s" has no animation data', armObj.name)
                return None
            action = animation_data.action
        if action is None:
            logging.warning('[WARNING] armature "%s" has no action to export', armObj.name)
            return None

        vmd_bone_anim = BoneAnimation()

        anim_bones = {}
        rePath = re.compile(r'^pose\.bones\["(.+)"\]\.([a-z_]+)$')
        prop_rotation_map = {'QUATERNION':'rotation_quaternion', 'AXIS_ANGLE':'rotation_axis_angle'}
        for fcurve in iter_action_fcurves(action, target=armObj):
            m = rePath.match(fcurve.data_path)
            if m is None:
                continue
            bone = armObj.pose.bones.get(m.group(1), None)
            if bone is None:
                logging.warning(' * Bone not found: %s', m.group(1))
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
        # Detect rot90 compensation needed for export
        rig_settings = getattr(armObj.data, "witcherui_RigSettings", None)
        rot90_active = get_rig_rot90_enabled(rig_settings, default=False)
        if rot90_active:
            z_neg90 = Quaternion((0, 0, 1), math.radians(-90))
            z_pos90 = Quaternion((0, 0, 1), math.radians(90))
            rot_neg90_mat = Matrix.Rotation(math.radians(-90), 4, 'Z')

        for bone, bone_curves in anim_bones.items():
            key_name = bone.name
            frame_keys = vmd_bone_anim[key_name]
            prev_rot = None
            for frame_number, x, y, z, rw, rx, ry, rz in self.__allFrameKeys(bone_curves, frame_numbers=frame_numbers):
                key = BoneFrameKey()
                key.frame_number = frame_number - self.__frame_start

                bl_bone = bone
                quat = Quaternion([ rw[0], rx[0], ry[0], rz[0]])
                if face_animation:
                    co = Vector([x[0], y[0], z[0]])
                    if rot90_active:
                        co = rot_neg90_mat @ co
                    ro = quat.copy()
                    if rot90_active:
                        ro = z_neg90 @ ro @ z_pos90
                else:
                    if bl_bone.parent:
                        objectMatrix = bl_bone.parent.bone.matrix_local.inverted()
                    else:
                        objectMatrix = bl_bone.bone.matrix_local.inverted()
                    the_vec = Vector([x[0], y[0], z[0]])
                    co = objectMatrix @ bl_bone.bone.matrix_local @ the_vec
                    if rot90_active:
                        co = rot_neg90_mat @ co
                    if bl_bone.parent:
                        ro = objectMatrix @ bl_bone.bone.matrix_local @ quat.to_matrix().to_4x4()
                    else:
                        # Root: use bone.matrix_local directly to preserve rest orientation
                        ro = (bl_bone.bone.matrix_local @ quat.to_matrix().to_4x4())
                    ro = ro.to_quaternion()
                    if rot90_active:
                        if bl_bone.parent:
                            ro = z_neg90 @ ro @ z_pos90  # sandwich for child bones
                        else:
                            ro = ro @ z_pos90  # post-multiply for root bones
                key.location = Vector([co[0], co[1], co[2]])
                curr_rot = ro
                if prev_rot is not None:
                    curr_rot = self.__minRotationDiff(prev_rot, curr_rot)
                prev_rot = curr_rot
                key.rotation = [curr_rot.x, curr_rot.y, curr_rot.z, curr_rot.w]
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
        log.info("Exporting Animation")

        with open(filepath, "w") as file:
            file.write(json.dumps(CSkeletalAnimationSet.__dict__, default=lambda obj: obj.__json_serializable__() if hasattr(obj, "__json_serializable__") else obj.__dict__ ,indent=2, sort_keys=False))

    def export_native(self, **args):
        armature = args.get('armature', None)
        filepath = args.get('filepath', '')
        action = args.get('action', None)

        self.__scale = args.get('scale', 1.0)

        if args.get('use_frame_range', False):
            self.__frame_start = bpy.context.scene.frame_start
            self.__frame_end = bpy.context.scene.frame_end

        # Bone order preserved via bone_order_list stored on rig settings.
        # Refactor target: derive order from skeleton data directly rather than a stored list.
        self.__bone_order = _get_armature_bone_order(armature)

        if not armature:
            return

        frame_numbers = list(range(int(self.__frame_start), int(self.__frame_end) + 1))
        self.boneAnimation = self.__exportBoneAnimation(armature, frame_numbers=frame_numbers, action=action)
        if self.boneAnimation is None:
            return
        if action is None and armature and armature.animation_data:
            action = armature.animation_data.action
        if action is None:
            log.warning(f'No action found on armature "{armature.name}", skipping export')
            return
        action_name = action.name if action else armature.name

        num_frames = max(1, int(self.__frame_end - self.__frame_start + 1))
        fps_base = bpy.context.scene.render.fps_base if bpy.context.scene.render.fps_base else 1.0
        fps = float(bpy.context.scene.render.fps) / float(fps_base) if fps_base else float(bpy.context.scene.render.fps)
        dt = 1.0 / fps if fps > 0 else anims_builder.DEFAULT_DT

        bones_data = []
        for bone_name in self.__bone_order:
            frames = self.boneAnimation.get(bone_name, [])
            pos_frames = []
            rot_frames = []
            scale_frames = []

            prev_q = None
            for idx in range(num_frames):
                if frames:
                    frame = frames[idx] if idx < len(frames) else frames[-1]
                    loc = frame.location
                    rot = frame.rotation
                    scl = frame.scale
                else:
                    loc = Vector((0.0, 0.0, 0.0))
                    rot = [0.0, 0.0, 0.0, -1.0]
                    scl = [1.0, 1.0, 1.0]

                pos_frames.append((float(loc[0]), float(loc[1]), float(loc[2])))
                scale_frames.append((float(scl[0]), float(scl[1]), float(scl[2])))

                x, y, z, w = rot
                q = Quaternion((w, x, y, z))
                if q.dot(q) <= 1e-12:
                    q = Quaternion((1.0, 0.0, 0.0, 0.0))
                else:
                    q.normalize()
                if prev_q is not None and q.dot(prev_q) < 0.0:
                    q = -q
                prev_q = q
                rot_frames.append((float(q.x), float(q.y), float(q.z), float(q.w)))

            bones_data.append({
                "name": bone_name,
                "pos_frames": pos_frames,
                "rot_frames": rot_frames,
                "scale_frames": scale_frames,
            })

        skeletal_type = args.get('skeletal_type', 'SAT_Normal')
        additive_type = args.get('additive_type', None)
        include_motion_extraction = args.get('include_motion_extraction', False)

        motion_extraction_data = None
        if include_motion_extraction:
            trajectory_bone = armature.pose.bones.get("Trajectory")
            if trajectory_bone and armature.animation_data:
                try:
                    # 1) Sample every frame → CUncompressedMotionExtraction.
                    # pose_bone.matrix is in armature object space and already
                    # includes the full parent chain (Root), so it gives game
                    # world-space positions directly regardless of rot90.
                    uncomp_frames = []
                    initial_pos = None
                    initial_yaw = 0.0
                    for frame_idx in frame_numbers:
                        bpy.context.scene.frame_set(frame_idx)
                        traj = armature.pose.bones.get("Trajectory")
                        if not traj:
                            break
                        pos = traj.matrix.to_translation()
                        yaw = traj.matrix.to_euler('XYZ').z
                        if initial_pos is None:
                            initial_pos = pos.copy()
                            initial_yaw = yaw
                        dx = float(pos.x - initial_pos.x)
                        dy = float(pos.y - initial_pos.y)
                        dz = float(pos.z - initial_pos.z)
                        dyaw = float(yaw - initial_yaw)
                        uncomp_frames.append((dx, dy, dz, dyaw))
                    bpy.context.scene.frame_set(int(self.__frame_start))

                    # 2) Derive CLine (compressed) from the per-frame data.
                    fps_val = float(bpy.context.scene.render.fps)
                    me = cline_from_per_frame(uncomp_frames, frame_numbers,
                                              fps_val)
                    if me is not None:
                        motion_extraction_data = {
                            "duration": me.duration,
                            "frames": me.frames,
                            "delta_times": me.delta_times,
                            "flags": me.flags,
                            "uncompressed_frames": uncomp_frames,
                        }
                except Exception as e:
                    log.warning(f"Failed to generate motion extraction: {e}")

        cr2w = anims_builder.build_w2anims(
            action_name=action_name,
            bones=bones_data,
            num_frames=num_frames,
            dt=dt,
            fps=fps,
            skeletal_type=skeletal_type,
            additive_type=additive_type,
            motion_extraction=motion_extraction_data,
        )
        cr2w_writer.write_w2anims(cr2w, filepath)
    
    def export(self, **args):
        armature = args.get('armature', None)
        filepath = args.get('filepath', '')
        single_action = args.get('single_action', False)
        action = args.get('action', None)

        self.__scale = args.get('scale', 1.0)

        if args.get('use_frame_range', False):
            self.__frame_start = bpy.context.scene.frame_start
            self.__frame_end = bpy.context.scene.frame_end

        # Bone order preserved via bone_order_list stored on rig settings.
        # Refactor target: derive order from skeleton data directly rather than a stored list.
        self.__bone_order = _get_armature_bone_order(armature)

        if armature:
            self.boneAnimation = self.__exportBoneAnimation(armature, action=action)
            if self.boneAnimation is None:
                return
            if action is None and armature.animation_data:
                action = armature.animation_data.action
            if action is None:
                log.warning(f'No action found on armature "{armature.name}", skipping export')
                return
            self.__save(filepath, action.name, single_action)

def export_w3_anim(context, savePath, use_native_writer=False,
                   skeletal_type="SAT_Normal", additive_type=None,
                   include_motion_extraction=False):
    armObj = get_selected_armature(context)
    if not armObj:
        log.warning("No armature selected for export")
        return {'CANCELLED'}

    source_mode = getattr(context.scene, "witcher_w3_anim_source", "AUTO")
    curr_action, action_info = resolve_action(armObj, context=context, source_mode=source_mode)
    if curr_action is None:
        log.warning(f'No action found to export on "{armObj.name}" (source: {source_mode})')
        return {'CANCELLED'}

    exporter = W3AnimationExporter()
    if use_native_writer:
        exporter.export_native(armature = armObj,
                               filepath = savePath,
                               use_frame_range = True,
                               single_action = True,
                               skeletal_type = skeletal_type,
                               additive_type = additive_type,
                               include_motion_extraction = include_motion_extraction,
                               action = curr_action)
        log.info(f'Finished Exporting {curr_action.name} (native .w2anims)')
    else:
        exporter.export(armature = armObj,
                        filepath = savePath,
                        use_frame_range = True,
                        single_action = True,
                        action = curr_action)
        log.info(f'Finished Exporting {curr_action.name} (json)')
    return {'FINISHED'}


CUTSCENE_DEFAULT_FPS = 30.0
CUTSCENE_ROOT_COMPONENT = "Root"
_CUTSCENE_FACE_TRACK_NAME_CACHE: Dict[str, List[str]] = {}
_CUTSCENE_RIG_BONE_NAME_CACHE: Dict[str, List[str]] = {}


def _strip_text(value) -> str:
    return str(value or "").strip()


def _normalize_repo_path(path: str) -> str:
    return _strip_text(path).replace("/", "\\").lstrip("\\")


def _normalize_cutscene_component(component_name: str) -> str:
    component_text = _strip_text(component_name)
    return component_text or CUTSCENE_ROOT_COMPONENT


def _is_face_cutscene_component(component_name: str) -> bool:
    return _normalize_cutscene_component(component_name).lower() == "face"


def _safe_int(value, default=-1) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _compose_cutscene_animation_name(actor_name: str, component_name: str, action_name: str) -> str:
    actor_name = _strip_text(actor_name)
    component_name = _normalize_cutscene_component(component_name)
    action_name = _strip_text(action_name)
    if actor_name and action_name:
        return f"{actor_name}:{component_name}:{action_name}"
    return action_name or actor_name


def _iter_action_bone_names(action, target=None) -> List[str]:
    if action is None:
        return []
    prefix = 'pose.bones["'
    names = []
    seen = set()
    valid_suffixes = {
        ".location",
        ".rotation_quaternion",
        ".rotation_axis_angle",
        ".rotation_euler",
    }
    for fcurve in iter_action_fcurves(action, target=target):
        data_path = str(getattr(fcurve, "data_path", "") or "")
        if not data_path.startswith(prefix):
            continue
        end_idx = data_path.find('"]')
        if end_idx <= len(prefix):
            continue
        suffix = data_path[end_idx + 2:]
        if suffix not in valid_suffixes:
            continue
        bone_name = _strip_text(data_path[len(prefix):end_idx])
        if not bone_name or bone_name in seen:
            continue
        seen.add(bone_name)
        names.append(bone_name)
    return names


def _resolve_cutscene_animation_rig_load_path(rig_repo_path: str) -> str:
    rig_repo_path = _normalize_repo_path(rig_repo_path)
    if not rig_repo_path:
        return ""
    try:
        from ..CR2W.common_blender import repo_file

        return repo_file(rig_repo_path)
    except Exception:
        log.warning("Failed to resolve rig path '%s' while exporting cutscene.", rig_repo_path, exc_info=True)
        return ""


def _load_cutscene_rig_bone_names(rig_path: str) -> List[str]:
    rig_path = _strip_text(rig_path)
    if not rig_path:
        return []
    cached = _CUTSCENE_RIG_BONE_NAME_CACHE.get(rig_path)
    if cached is not None:
        return list(cached)

    bone_names: List[str] = []
    try:
        from ..CR2W.dc_anims import load_base_skeleton

        skeleton = load_base_skeleton(rig_path)
        raw_names = getattr(skeleton, "names", None) or []
        bone_names = [_strip_text(name) for name in raw_names if _strip_text(name)]
    except Exception:
        log.warning("Failed to load rig bone names from '%s' while exporting cutscene.", rig_path, exc_info=True)
        bone_names = []

    _CUTSCENE_RIG_BONE_NAME_CACHE[rig_path] = list(bone_names)
    return list(bone_names)


def _load_source_cutscene_animation(entry, source_cache: Dict[str, object], rig_load_path: str = ""):
    if not entry or source_cache is None:
        return None
    if not _strip_text(rig_load_path):
        return None

    source_path = _strip_text(entry.get("source_path", ""))
    if not source_path or not source_path.lower().endswith(".w2cutscene"):
        return None

    animation_name = _strip_text(entry.get("source_animation_name", ""))
    if not animation_name:
        animation_name = _compose_cutscene_animation_name(
            entry.get("actor_name", ""),
            entry.get("component", ""),
            entry.get("action_name", ""),
        )
    if not animation_name:
        return None

    cache_key = ("cutscene_source_animation", source_path, animation_name, _strip_text(rig_load_path))
    if cache_key in source_cache:
        return source_cache[cache_key]

    source_animation = None
    try:
        from ..CR2W.dc_anims import load_bin_anims_single

        anim_set = load_bin_anims_single(
            source_path,
            anim_name=animation_name,
            rigPath=rig_load_path or None,
        )
        animations = getattr(anim_set, "animations", None) or []
        if animations:
            source_animation = getattr(animations[0], "animation", None)
    except Exception:
        log.warning(
            "Failed to inspect source cutscene animation '%s' from '%s' while exporting.",
            animation_name,
            source_path,
            exc_info=True,
        )

    source_cache[cache_key] = source_animation
    return source_animation


def _load_source_cutscene_track_names(entry, source_cache: Dict[str, object], rig_load_path: str = "") -> List[str]:
    source_animation = _load_source_cutscene_animation(entry, source_cache, rig_load_path=rig_load_path)
    if source_animation is None:
        return []

    track_names = []
    seen = set()
    for track in getattr(getattr(source_animation, "animBuffer", None), "tracks", None) or []:
        track_name = _strip_text(getattr(track, "trackName", ""))
        if not track_name or track_name in seen:
            continue
        seen.add(track_name)
        track_names.append(track_name)
    return track_names


def _resolve_cutscene_export_bone_order(armature_obj, action, component: str, source_entry=None,
                                        source_cache: Optional[Dict[str, object]] = None,
                                        rig_repo_path: str = "") -> List[str]:
    if armature_obj is None:
        return []

    pose = getattr(armature_obj, "pose", None)
    pose_bones = getattr(pose, "bones", None)
    pose_bone_names = set(pose_bones.keys()) if pose_bones is not None else set()
    armature_order = _get_armature_bone_order(armature_obj)

    rig_load_path = _resolve_cutscene_animation_rig_load_path(rig_repo_path)
    source_animation = _load_source_cutscene_animation(source_entry, source_cache, rig_load_path=rig_load_path)
    if source_animation is not None:
        source_bone_names = []
        for bone in getattr(getattr(source_animation, "animBuffer", None), "bones", None) or []:
            bone_name = _strip_text(getattr(bone, "BoneName", ""))
            if bone_name:
                source_bone_names.append(bone_name)
        if source_bone_names:
            filtered = [name for name in source_bone_names if not pose_bone_names or name in pose_bone_names]
            if filtered:
                return filtered

    if _is_face_cutscene_component(component):
        face_bone_names = [
            name for name in _load_cutscene_rig_bone_names(rig_load_path)
            if not pose_bone_names or name in pose_bone_names
        ]
        if face_bone_names:
            return face_bone_names

    animated_bone_names = _iter_action_bone_names(action, target=armature_obj)
    if animated_bone_names:
        animated_set = set(animated_bone_names)
        ordered_names = [name for name in armature_order if name in animated_set]
        for name in animated_bone_names:
            if name in ordered_names:
                continue
            if pose_bone_names and name not in pose_bone_names:
                continue
            ordered_names.append(name)
        if ordered_names:
            return ordered_names

    return [name for name in armature_order if not pose_bone_names or name in pose_bone_names]


def _pose_bone_custom_prop_names(armature_obj, bone_name: str) -> List[str]:
    if armature_obj is None or getattr(armature_obj, "type", None) != 'ARMATURE':
        return []
    pose = getattr(armature_obj, "pose", None)
    pose_bone = getattr(pose, "bones", {}).get(bone_name) if pose else None
    if pose_bone is None:
        return []
    names = []
    for key in pose_bone.keys():
        key_text = _strip_text(key)
        if not key_text or key_text == "_RNA_UI":
            continue
        names.append(key_text)
    return names


def _pose_bone_custom_prop_value(armature_obj, bone_name: str, prop_name: str, default: float = 0.0) -> float:
    if armature_obj is None or getattr(armature_obj, "type", None) != 'ARMATURE':
        return float(default)
    pose = getattr(armature_obj, "pose", None)
    pose_bone = getattr(pose, "bones", {}).get(bone_name) if pose else None
    if pose_bone is None:
        return float(default)
    try:
        return float(pose_bone.get(prop_name, default))
    except Exception:
        return float(default)


def _iter_action_custom_prop_names(action, bone_name: str, target=None) -> List[str]:
    if action is None:
        return []
    prefix = f'pose.bones["{bone_name}"]["'
    names = []
    seen = set()
    for fcurve in iter_action_fcurves(action, target=target):
        data_path = str(getattr(fcurve, "data_path", "") or "")
        if not data_path.startswith(prefix) or not data_path.endswith('"]'):
            continue
        prop_name = data_path[len(prefix):-2]
        if not prop_name or prop_name in seen:
            continue
        seen.add(prop_name)
        names.append(prop_name)
    return names


def _sample_action_custom_prop_frames(action, bone_name: str, prop_name: str, frame_numbers: List[int], target=None, default_value: float = 0.0):
    curve = _FCurve(float(default_value))
    data_path = f'pose.bones["{bone_name}"]["{prop_name}"]'
    has_curve = False
    for fcurve in iter_action_fcurves(action, target=target):
        if str(getattr(fcurve, "data_path", "") or "") != data_path:
            continue
        curve.setFCurve(fcurve)
        has_curve = True
        break
    sampled = curve.sampleFrames(frame_numbers)
    values = []
    for sample in sampled:
        if isinstance(sample, (list, tuple)) and sample:
            values.append(float(sample[0]))
        else:
            values.append(float(default_value))
    return values, has_curve


def _load_face_track_names(face_file_path: str) -> List[str]:
    face_file_path = _normalize_repo_path(face_file_path)
    if not face_file_path:
        return []
    cached = _CUTSCENE_FACE_TRACK_NAME_CACHE.get(face_file_path)
    if cached is not None:
        return list(cached)

    track_names: List[str] = []
    try:
        from ..CR2W.common_blender import repo_file
        from ..importers.import_rig import loadFaceFile

        face_data = loadFaceFile(repo_file(face_file_path))
        float_track_skeleton = getattr(face_data, "floatTrackSkeleton", None)
        raw_track_names = getattr(float_track_skeleton, "tracks", None) or []
        track_names = [_strip_text(track_name) for track_name in raw_track_names if _strip_text(track_name)]
    except Exception:
        log.warning("Failed to load face track names from '%s' while exporting cutscene.", face_file_path, exc_info=True)
        track_names = []

    _CUTSCENE_FACE_TRACK_NAME_CACHE[face_file_path] = list(track_names)
    return list(track_names)


def _resolve_cutscene_face_track_names(armature_obj, related_armatures=None) -> List[str]:
    track_names: List[str] = []
    seen = set()
    candidate_armatures = list(related_armatures or [])
    if not candidate_armatures and armature_obj is not None:
        candidate_armatures = [armature_obj]
    for candidate in candidate_armatures:
        _entity_skeleton, face_skeleton = _get_armature_skeleton_paths(candidate)
        for face_file_path in (face_skeleton, _normalize_repo_path(candidate.get("mimicFaceFile", ""))):
            for track_name in _load_face_track_names(face_file_path):
                if track_name in seen:
                    continue
                seen.add(track_name)
                track_names.append(track_name)
    if track_names:
        return track_names

    rig_settings = getattr(getattr(armature_obj, "data", None), "witcherui_RigSettings", None)
    morph_entries = getattr(rig_settings, "witcher_morphs_list", None) if rig_settings else None
    if morph_entries:
        for entry in morph_entries:
            if int(getattr(entry, "type", 0) or 0) not in (4, 5):
                continue
            track_name = _strip_text(getattr(entry, "path", "") or getattr(entry, "name", ""))
            if not track_name or track_name in seen:
                continue
            seen.add(track_name)
            track_names.append(track_name)
    if track_names:
        return track_names

    for track_name in _pose_bone_custom_prop_names(armature_obj, "w3_face_poses"):
        if track_name in seen:
            continue
        seen.add(track_name)
        track_names.append(track_name)
    return track_names


def _resolve_cutscene_camera_track_names(armature_obj) -> List[str]:
    track_names: List[str] = []
    seen = set()
    rig_settings = getattr(getattr(armature_obj, "data", None), "witcherui_RigSettings", None)
    track_entries = getattr(rig_settings, "witcher_tracks_list", None) if rig_settings else None
    if track_entries:
        for entry in track_entries:
            if int(getattr(entry, "type", 0) or 0) != 0:
                continue
            track_name = _strip_text(getattr(entry, "path", "") or getattr(entry, "name", ""))
            if not track_name or track_name in seen:
                continue
            seen.add(track_name)
            track_names.append(track_name)
    if track_names:
        return track_names

    for track_name in _pose_bone_custom_prop_names(armature_obj, "Camera_Node"):
        if track_name in seen:
            continue
        seen.add(track_name)
        track_names.append(track_name)
    return track_names


def _collect_cutscene_action_tracks(armature_obj, action, component: str, frame_numbers: List[int],
                                    source_entry=None, source_cache=None, rig_repo_path: str = "",
                                    related_armatures=None) -> List[Dict[str, object]]:
    if armature_obj is None or action is None or not frame_numbers:
        return []

    rig_load_path = _resolve_cutscene_animation_rig_load_path(rig_repo_path)
    source_track_names = _load_source_cutscene_track_names(source_entry, source_cache, rig_load_path=rig_load_path)

    if _is_face_cutscene_component(component):
        control_bone_name = "w3_face_poses"
        ordered_names = source_track_names or _resolve_cutscene_face_track_names(
            armature_obj,
            related_armatures=related_armatures,
        )
    else:
        control_bone_name = "Camera_Node"
        ordered_names = source_track_names or _resolve_cutscene_camera_track_names(armature_obj)

    animated_names = _iter_action_custom_prop_names(action, control_bone_name, target=armature_obj)
    if not ordered_names and not animated_names:
        return []

    if source_track_names:
        source_name_set = set(source_track_names)
        animated_names = [track_name for track_name in animated_names if _strip_text(track_name) in source_name_set]

    seen = set()
    track_names = []
    for track_name in ordered_names + animated_names:
        track_name = _strip_text(track_name)
        if not track_name or track_name in seen:
            continue
        seen.add(track_name)
        track_names.append(track_name)

    tracks_data = []
    for track_name in track_names:
        default_value = _pose_bone_custom_prop_value(armature_obj, control_bone_name, track_name, default=0.0)
        values, _has_curve = _sample_action_custom_prop_frames(
            action,
            control_bone_name,
            track_name,
            frame_numbers,
            target=armature_obj,
            default_value=default_value,
        )
        if not values:
            values = [float(default_value)]

        first_value = float(values[0])
        is_constant = all(abs(float(value) - first_value) <= 1e-6 for value in values[1:])
        tracks_data.append({
            "name": track_name,
            "track_frames": [first_value] if is_constant else [float(value) for value in values],
            "num_frames": 1 if is_constant else len(values),
            "compression": 0,
        })

    return tracks_data


def _build_cutscene_bones_data(bone_order: List[str], bone_anim, num_frames: int):
    bones_data = []
    for bone_name in bone_order:
        frames = bone_anim.get(bone_name, [])
        pos_frames = []
        rot_frames = []
        scale_frames = []

        prev_q = None
        for idx in range(num_frames):
            if frames:
                frame = frames[idx] if idx < len(frames) else frames[-1]
                loc = frame.location
                rot = frame.rotation
                scl = frame.scale
            else:
                loc = Vector((0.0, 0.0, 0.0))
                rot = [0.0, 0.0, 0.0, -1.0]
                scl = [1.0, 1.0, 1.0]

            pos_frames.append((float(loc[0]), float(loc[1]), float(loc[2])))
            scale_frames.append((float(scl[0]), float(scl[1]), float(scl[2])))

            x, y, z, w = rot
            q = Quaternion((w, x, y, z))
            if q.dot(q) <= 1e-12:
                q = Quaternion((1.0, 0.0, 0.0, 0.0))
            else:
                q.normalize()
            if prev_q is not None and q.dot(prev_q) < 0.0:
                q = -q
            prev_q = q
            rot_frames.append((float(q.x), float(q.y), float(q.z), float(q.w)))

        bones_data.append({
            "name": bone_name,
            "pos_frames": pos_frames,
            "rot_frames": rot_frames,
            "scale_frames": scale_frames,
        })
    return bones_data


def _build_cutscene_animation_from_action(armature_obj, action, actor_name, component, action_name,
                                          frame_start, frame_end, fps, skeleton_path="",
                                          source_entry=None, source_cache=None,
                                          related_armatures=None):
    exporter = W3AnimationExporter()
    rig_repo_path = _normalize_repo_path(skeleton_path)
    bone_order = _resolve_cutscene_export_bone_order(
        armature_obj,
        action,
        component,
        source_entry=source_entry,
        source_cache=source_cache,
        rig_repo_path=rig_repo_path,
    )
    if not bone_order:
        bone_order = _get_armature_bone_order(armature_obj)
    exporter._W3AnimationExporter__bone_order = list(bone_order)
    exporter._W3AnimationExporter__frame_start = int(frame_start)
    exporter._W3AnimationExporter__frame_end = int(frame_end)

    frame_numbers = list(range(int(frame_start), int(frame_end) + 1))
    bone_anim = exporter._W3AnimationExporter__exportBoneAnimation(
        armature_obj,
        frame_numbers=frame_numbers,
        action=action,
        face_animation=_is_face_cutscene_component(component),
    )
    if bone_anim is None:
        return None

    num_frames = max(1, int(frame_end - frame_start + 1))
    dt = 1.0 / float(fps) if float(fps) > 0.0 else anims_builder.DEFAULT_DT
    bones_data = _build_cutscene_bones_data(bone_order, bone_anim, num_frames)
    tracks_data = _collect_cutscene_action_tracks(
        armature_obj,
        action,
        component,
        frame_numbers,
        source_entry=source_entry,
        source_cache=source_cache,
        rig_repo_path=rig_repo_path,
        related_armatures=related_armatures,
    )
    return {
        "actor": actor_name,
        "component": component,
        "action_name": action_name,
        "bones": bones_data,
        "tracks": tracks_data,
        "num_frames": num_frames,
        "dt": dt,
        "fps": float(fps),
        "skeletal_type": "SAT_Normal",
        "additive_type": None,
        "motion_extraction": None,
        "skeleton_path": rig_repo_path,
    }
def _get_armature_skeleton_paths(armature_obj) -> Tuple[str, str]:
    rig_settings = getattr(getattr(armature_obj, "data", None), "witcherui_RigSettings", None)
    entity_skeleton = _normalize_repo_path(getattr(rig_settings, "main_entity_skeleton", "") if rig_settings else "")
    face_skeleton = _normalize_repo_path(getattr(rig_settings, "main_face_skeleton", "") if rig_settings else "")

    if not entity_skeleton:
        armature_path = _normalize_repo_path(armature_obj.get("witcher_path", ""))
        if armature_path.lower().endswith((".w2rig", ".w3dyng")):
            entity_skeleton = armature_path

    if not face_skeleton:
        face_skeleton = _normalize_repo_path(armature_obj.get("mimicFaceFile", ""))

    return entity_skeleton, face_skeleton
def export_w3_cutscene(context, savePath, export_redkit_re_files=False, export_redkit_csv=False, **kwargs):
    """Compatibility wrapper for cutscene export."""
    from . import export_cutscene

    if "export_re_sidecars" in kwargs:
        export_redkit_re_files = bool(kwargs.pop("export_re_sidecars") or export_redkit_re_files)

    return export_cutscene.export_w3_cutscene(
        context,
        savePath,
        export_redkit_re_files=export_redkit_re_files,
        export_redkit_csv=export_redkit_csv,
    )
