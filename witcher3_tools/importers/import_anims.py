import logging
from .. import auto_scene_setup
from ..file_helpers import getFilenameFile, rm_ns
from ..CR2W import read_json_w3
from ..CR2W import w3_types
from ..CR2W.dc_anims import load_bin_anims, load_lipsync_file, load_bin_anims_info, load_w2_anims_info
from ..CR2W.CR2W_helpers import Enums
log = logging.getLogger(__name__)

from ..importers.import_rig import get_ordered_bones
from ..importers.motion_tools import MotionExtraction, apply_motion, apply_motion_to_bone, extract_motion_from_bone
from .. import get_do_fix_tail, get_rig_rot90_enabled


import json
import copy
import math
from mathutils import Vector, Quaternion, Euler, Matrix
import os
import time
from typing import Union
import numpy as np

import bpy
from ..action_compat import assign_action, bind_strip_action_slot, new_action_fcurve, resolve_action_slot
matmul = (lambda a, b: a*b) if bpy.app.version < (2, 80, 0) else (lambda a, b: a.__matmul__(b))


def _set_active_object(obj):
    if obj is None:
        return False
    view_layer = getattr(bpy.context, "view_layer", None)
    if view_layer is None:
        return False
    try:
        obj.select_set(True)
    except Exception:
        pass
    try:
        view_layer.objects.active = obj
    except Exception:
        return False
    return view_layer.objects.active == obj


def _safe_mode_set(mode, obj=None):
    if obj is not None:
        _set_active_object(obj)
    view_layer = getattr(bpy.context, "view_layer", None)
    active = view_layer.objects.active if view_layer else None
    if active is None:
        log.debug("Skipping mode_set(%s): no active object.", mode)
        return False
    if getattr(active, "mode", None) == mode:
        return True
    try:
        bpy.ops.object.mode_set(mode=mode)
        return True
    except RuntimeError as exc:
        log.debug("Skipping mode_set(%s) on %s: %s", mode, getattr(active, "name", "<unknown>"), exc)
        return False


def _find_anim_bone(bones, *names):
    """Return the first animation bone whose name matches one of the candidates."""
    for name in names:
        for bone in bones:
            if bone.BoneName == name:
                return bone
    return None

def _get_quaternion_components(value):
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        if len(value) >= 4:
            return (
                float(value[0]),
                float(value[1]),
                float(value[2]),
                float(value[3]),
            )
        return None
    if isinstance(value, dict):
        return (
            float(value.get("X", value.get("x", 0.0))),
            float(value.get("Y", value.get("y", 0.0))),
            float(value.get("Z", value.get("z", 0.0))),
            float(value.get("W", value.get("w", 0.0))),
        )

    x = getattr(value, "X", getattr(value, "x", None))
    y = getattr(value, "Y", getattr(value, "y", None))
    z = getattr(value, "Z", getattr(value, "z", None))
    w = getattr(value, "W", getattr(value, "w", None))
    if None not in (x, y, z, w):
        return (float(x), float(y), float(z), float(w))
    return None

def shouldIgnoreFrame(bone):
    rotation_frames = list(getattr(bone, "rotationFrames", []) or [])
    if not rotation_frames:
        return False
    components = _get_quaternion_components(rotation_frames[0])
    if components is None:
        return False
    x, y, z, _w = components
    return abs(x) < 0.5 and abs(y) < 0.5 and abs(z) < 0.5

def slerp_quaternion(q1, q2, t):
    """Spherical linear interpolation between two Quaternion objects"""
    # Compute dot product
    dot = q1.w * q2.w + q1.x * q2.x + q1.y * q2.y + q1.z * q2.z
    
    # If negative dot, negate q2 to take shorter path
    if dot < 0:
        q2 = Quaternion((-q2.w, -q2.x, -q2.y, -q2.z))
        dot = -dot
    
    # If very close, use linear interpolation
    if dot > 0.9995:
        result = Quaternion((
            q1.w + t * (q2.w - q1.w),
            q1.x + t * (q2.x - q1.x),
            q1.y + t * (q2.y - q1.y),
            q1.z + t * (q2.z - q1.z)
        ))
        result.normalize()
        return result
    
    # SLERP
    theta_0 = math.acos(min(1.0, max(-1.0, dot)))
    theta = theta_0 * t
    sin_theta = math.sin(theta)
    sin_theta_0 = math.sin(theta_0)
    
    s1 = math.cos(theta) - dot * sin_theta / sin_theta_0
    s2 = sin_theta / sin_theta_0
    
    result = Quaternion((
        s1 * q1.w + s2 * q2.w,
        s1 * q1.x + s2 * q2.x,
        s1 * q1.y + s2 * q2.y,
        s1 * q1.z + s2 * q2.z
    ))
    result.normalize()
    return result

def lerp_vector(v1, v2, t):
    """Linear interpolation between two vectors (lists)"""
    return [v1[i] + t * (v2[i] - v1[i]) for i in range(len(v1))]

def catmull_rom_vector(p0, p1, p2, p3, t):
    """Uniform Catmull-Rom spline interpolation for mathutils.Vector"""
    t2 = t * t
    t3 = t2 * t
    return 0.5 * (
        (2.0 * p1) +
        (-p0 + p2) * t +
        (2.0 * p0 - 5.0 * p1 + 4.0 * p2 - p3) * t2 +
        (-p0 + 3.0 * p1 - 3.0 * p2 + p3) * t3
    )

def nlerp_quaternion(q1, q2, t):
    """Normalized linear interpolation between two Quaternion objects"""
    dot = q1.w * q2.w + q1.x * q2.x + q1.y * q2.y + q1.z * q2.z
    if dot < 0:
        q2 = Quaternion((-q2.w, -q2.x, -q2.y, -q2.z))
    result = Quaternion((
        q1.w + t * (q2.w - q1.w),
        q1.x + t * (q2.x - q1.x),
        q1.y + t * (q2.y - q1.y),
        q1.z + t * (q2.z - q1.z)
    ))
    result.normalize()
    return result

def build_anchor_mask(total_frames, base_dt, track_dt, tol=1e-4):
    """Return a boolean mask for frames that align to original track keys."""
    if total_frames <= 0 or base_dt <= 0 or track_dt <= 0:
        return None
    mask = [False] * total_frames
    for i in range(total_frames):
        t = i * base_dt
        idx = t / track_dt
        if abs(idx - round(idx)) <= tol:
            mask[i] = True
    if mask:
        mask[0] = True
        mask[-1] = True
    return mask

def smooth_vector_frames(frames, anchor_mask=None):
    """Light smoothing for vector frames; skips anchor frames."""
    if len(frames) < 3:
        return frames
    out = list(frames)
    for i in range(1, len(frames) - 1):
        if anchor_mask and anchor_mask[i]:
            continue
        out[i] = (frames[i - 1] + frames[i] + frames[i + 1]) / 3.0
    out[0] = frames[0]
    out[-1] = frames[-1]
    return out

def smooth_quaternion_frames(frames, anchor_mask=None):
    """Light smoothing for quaternion frames; skips anchor frames."""
    if len(frames) < 3:
        return frames
    out = list(frames)
    for i in range(1, len(frames) - 1):
        if anchor_mask and anchor_mask[i]:
            continue
        q_prev = frames[i - 1]
        q_curr = frames[i]
        q_next = frames[i + 1]
        if q_prev.dot(q_curr) < 0.0:
            q_prev = Quaternion((-q_prev.w, -q_prev.x, -q_prev.y, -q_prev.z))
        if q_next.dot(q_curr) < 0.0:
            q_next = Quaternion((-q_next.w, -q_next.x, -q_next.y, -q_next.z))
        q = Quaternion((
            q_prev.w + q_curr.w + q_next.w,
            q_prev.x + q_curr.x + q_next.x,
            q_prev.y + q_curr.y + q_next.y,
            q_prev.z + q_curr.z + q_next.z
        ))
        q.normalize()
        out[i] = q
    out[0] = frames[0]
    out[-1] = frames[-1]
    return out

def resample_position_frames(pos_frames, bone_dt, duration, target_frames, mode="linear"):
    """Resample position frames to have keyframes at every integer Blender frame"""
    if len(pos_frames) <= 1 or len(pos_frames) >= target_frames:
        return pos_frames, False  # No resampling needed

    pos_frames = [p if isinstance(p, Vector) else Vector(p) for p in pos_frames]
    
    resampled = []
    src_duration = (len(pos_frames) - 1) * bone_dt
    
    for blender_frame in range(target_frames):
        # Convert Blender frame to time
        t = (blender_frame / (target_frames - 1)) * duration if target_frames > 1 else 0
        # Clamp time to source duration
        t = min(t, src_duration)
        # Find source frame index
        src_idx = t / bone_dt if bone_dt > 0 else 0
        src_floor = min(int(src_idx), len(pos_frames) - 1)
        src_ceil = min(src_floor + 1, len(pos_frames) - 1)
        frac = src_idx - src_floor
        
        if src_floor == src_ceil or frac == 0:
            resampled.append(pos_frames[src_floor])
        else:
            if mode == "linear":
                interp = lerp_vector(pos_frames[src_floor], pos_frames[src_ceil], frac)
                resampled.append(Vector(interp))
            else:
                # Catmull-Rom using neighboring samples when available
                p0 = pos_frames[src_floor - 1] if src_floor - 1 >= 0 else pos_frames[src_floor]
                p1 = pos_frames[src_floor]
                p2 = pos_frames[src_ceil]
                p3 = pos_frames[src_ceil + 1] if src_ceil + 1 < len(pos_frames) else pos_frames[src_ceil]
                resampled.append(catmull_rom_vector(p0, p1, p2, p3, frac))
    
    if resampled:
        resampled[0] = pos_frames[0]
        resampled[-1] = pos_frames[-1]
    return resampled, True

def resample_rotation_frames(rot_frames, bone_dt, duration, target_frames, mode="nlerp"):
    """Resample rotation frames (Quaternions) to have keyframes at every integer Blender frame"""
    if len(rot_frames) <= 1 or len(rot_frames) >= target_frames:
        return rot_frames, False  # No resampling needed
    
    resampled = []
    src_duration = (len(rot_frames) - 1) * bone_dt
    
    for blender_frame in range(target_frames):
        # Convert Blender frame to time
        t = (blender_frame / (target_frames - 1)) * duration if target_frames > 1 else 0
        # Clamp time to source duration
        t = min(t, src_duration)
        # Find source frame index
        src_idx = t / bone_dt if bone_dt > 0 else 0
        src_floor = min(int(src_idx), len(rot_frames) - 1)
        src_ceil = min(src_floor + 1, len(rot_frames) - 1)
        frac = src_idx - src_floor
        
        if src_floor == src_ceil or frac == 0:
            resampled.append(rot_frames[src_floor])
        else:
            if mode == "slerp":
                resampled.append(slerp_quaternion(rot_frames[src_floor], rot_frames[src_ceil], frac))
            else:
                resampled.append(nlerp_quaternion(rot_frames[src_floor], rot_frames[src_ceil], frac))
    
    if resampled:
        resampled[0] = rot_frames[0]
        resampled[-1] = rot_frames[-1]
    return resampled, True


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
            assign_action(target, action)
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
            bind_strip_action_slot(target_strip, resolve_action_slot(action, target=target, ensure=True))
            start_frame, end_frame = action.frame_range
            length = end_frame - start_frame
            target_strip.frame_end = self.__frame_current + length
            target_strip.blend_type = 'REPLACE'
            
            if self.__NLA_track:
                track_name = str(self.__NLA_track or "")
                if track_name in {'mimic_import', 'voice_import', 'anim_import'} or track_name.startswith('cutscene_import'):
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

        rig_settings = getattr(armObj.data, "witcherui_RigSettings", None)
        use_rot90 = get_rig_rot90_enabled(
            rig_settings,
            default=get_do_fix_tail(bpy.context)
        )


        use_root_source_bone = True
        if use_root_source_bone:
            root_bone = _find_anim_bone(anim_desc.bones, "Root")
            # Prefer Trajectory
            source_bone = _find_anim_bone(anim_desc.bones, "Trajectory", "Reference")
            if root_bone and source_bone:
                if source_bone.BoneName != "Trajectory":
                    log.info("Animation '%s': using '%s' as Root fallback", SkeletalAnimation.name, source_bone.BoneName)
                root_bone.positionFrames = [[0.0, 0.0, 0.0]]
                root_bone.position_dt = source_bone.position_dt
                root_bone.position_numFrames = 1
                root_bone.rotationFrames = source_bone.rotationFrames
                root_bone.rotationFramesQuat = source_bone.rotationFramesQuat
                root_bone.rotation_dt = source_bone.rotation_dt
                root_bone.rotation_numFrames = source_bone.rotation_numFrames
                root_bone.scaleFrames = [[1.0, 1.0, 1.0]]
                root_bone.scale_dt = source_bone.scale_dt
                root_bone.scale_numFrames = 1
        
        #add detected namespace to aniamtion data
        if armature_namespace:
            for i, bone in enumerate(anim_desc.bones):
                anim_desc.bones[i].BoneName = armature_namespace+bone.BoneName

        action_name = SkeletalAnimation.name or action_name or armObj.name
        action = bpy.data.actions.new(name=action_name)
        # Store import source metadata on the action for easy inspection in Blender
        try:
            action["w3_anim_source_file"] = self.__animFile.filepath
            source = getattr(SkeletalAnimationData, "_source", None)
            detail = getattr(SkeletalAnimationData, "_source_detail", None)
            if source:
                action["w3_anim_buffer_source"] = source
            if detail:
                action["w3_anim_buffer_detail"] = detail
            if source:
                log.info(f"Animation buffer source: {source}{' (' + detail + ')' if detail else ''}")
        except Exception:
            pass

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
        valid_bones = []
        for bone_data in anim_desc.bones:
            bl_bone = armObj.pose.bones.get(bone_data.BoneName)
            if bl_bone is None:
                log.warning('Animation data has unknown bone ' + bone_data.BoneName)
                continue
            valid_bones.append(bone_data)
            bone_name = bone_data.BoneName
            pos_curves = [dummy_keyframe_points] * 3
            rot_curves = [dummy_keyframe_points] * 4
            fcurves_rot = [dummy_keyframe_points]*4 # r0, r1, r2, (r3)
            fcurves_loc = [dummy_keyframe_points]*3 # x, y, z
            data_path_rot = prop_rot_map.get(bl_bone.rotation_mode, 'rotation_quaternion')
            bone_rotation = getattr(bl_bone, data_path_rot)
            data_path = 'pose.bones["%s"].location'%bl_bone.name
            for axis_i in range(3):
                fcurves_loc[axis_i] = new_action_fcurve(action, armObj, data_path=data_path, index=axis_i, group_name=bl_bone.name)
            data_path = 'pose.bones["%s"].%s'%(bl_bone.name, data_path_rot)
            for axis_i in range(len(bone_rotation)):
                fcurves_rot[axis_i] = new_action_fcurve(action, armObj, data_path=data_path, index=axis_i, group_name=bl_bone.name)

            pos_curves = fcurves_loc
            rot_curves = fcurves_rot

            curve_per_bone[bone_name] = pos_curves, rot_curves
        anim_desc.bones = valid_bones
        total_frames = anim_desc.numFrames
        duration = anim_desc.duration if anim_desc.duration > 0 else 1.0
        base_dt = getattr(anim_desc, "dt", 0.0) or 0.0
        if base_dt <= 0.0:
            base_dt = duration / (total_frames - 1) if total_frames > 1 else 1.0
        base_dt = round(base_dt, 8)
        # Conversion factor from time (seconds) to Blender frames
        time_to_frame = 1.0 / base_dt if base_dt > 0 else 1.0
        smooth_missing = getattr(bpy.context.scene, 'witcher_smooth_missing_frames', False)
        witcher_bake_every_frame = getattr(bpy.context.scene, 'witcher_bake_every_frame', True)
        witcher_scale_keys_to_duration = getattr(bpy.context.scene, 'witcher_scale_keys_to_duration', False)

        def _needs_resample(track_frames, track_dt):
            if len(track_frames) <= 1 or total_frames <= 1:
                return False
            if len(track_frames) != total_frames:
                return True
            if track_dt and abs(track_dt - base_dt) > 1e-6:
                return True
            return False
        
        start_time = time.time()
        for bone in anim_desc.bones:
            keyFrames_rot = bone.rotationFramesQuat
            keyFrames_loc = bone.positionFrames
            mdl_bone = bone #mdl.bones[bone.bone_id]
            bl_bone = armObj.pose.bones.get(mdl_bone.BoneName)
            if bl_bone is None:
                continue
            bl_bone.rotation_mode = 'QUATERNION'

            pos_scale = 1.0
            rot_scale = 1.0

            pos_frames = [Vector(np.multiply(np.multiply(pos, pos_scale), scale)) for pos in bone.positionFrames]
            #rot_frames = [Euler(np.multiply(Quaternion((rot.W, rot.X, rot.Y, rot.Z)).to_euler('XYZ'), rot_scale)) for rot in bone.rotationFramesQuat]
            
            #! 
            rot_frames = [Quaternion((rot.W, rot.X, rot.Y, rot.Z)) for rot in bone.rotationFramesQuat]
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
                pos_dt = bone.position_dt if bone.position_dt and bone.position_dt > 0 else base_dt
                pos_dt = round(pos_dt, 8)
                pos_resampled = False
                if witcher_bake_every_frame and _needs_resample(pos_frames, pos_dt):
                    pos_frames, pos_resampled = resample_position_frames(pos_frames, pos_dt, duration, total_frames)
                    if smooth_missing and pos_resampled:
                        anchor_mask = build_anchor_mask(total_frames, base_dt, pos_dt)
                        pos_frames = smooth_vector_frames(pos_frames, anchor_mask)
                num_pos_frames = len(pos_frames)
                prev_loc_frame = None
                last_frame = total_frames - 1
                for n, pos_frame in enumerate(pos_frames):
                    if witcher_bake_every_frame:
                        if pos_resampled or num_pos_frames == total_frames:
                            loc_frame = n
                        else:
                            loc_frame = n * (pos_dt / base_dt if base_dt > 0 else 1.0)
                    else:
                        if num_pos_frames == 1:
                            loc_frame = 0.0
                        elif witcher_scale_keys_to_duration and num_pos_frames > 1:
                            loc_frame = n * (last_frame / (num_pos_frames - 1))
                        else:
                            loc_frame = n * (pos_dt / base_dt if base_dt > 0 else 1.0)
                            if n == num_pos_frames - 1:
                                loc_frame = last_frame
                            elif loc_frame > last_frame:
                                loc_frame = last_frame
                        if prev_loc_frame is not None and abs(loc_frame - prev_loc_frame) < 1e-6:
                            continue
                        prev_loc_frame = loc_frame
                    if bl_bone.parent:
                        objectMatrix = bl_bone.parent.bone.matrix_local.inverted()
                        origPos = objectMatrix @ bl_bone.bone.matrix_local.translation
                    else:
                        #objectMatrix = armObj.matrix_world.inverted()
                        origPos = bl_bone.bone.matrix_local.translation
                    
                    origPos = Vector(( origPos.x, origPos.y, origPos.z ))
                    #origRot = bl_bone.bone.matrix_local.to_quaternion()  # LOCAL EditBone
                    
                    if use_rot90:
                        rotation_angle = math.radians(90)  # Convert -90 degrees to radians
                        rotation_matrix = Matrix.Rotation(rotation_angle, 4, 'Z')
                        pos_frame = rotation_matrix @ pos_frame
                        #origPos = rotation_matrix @ origPos
                    
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
                    pos_fix = Vector(( round(pos_fix.x, 8), round(pos_fix.y, 8), round(pos_fix.z, 8) ))
                    
                    do_retarget = False #! set this on to not import postions #!REMOVE
                    if do_retarget:
                        pos = pos_fix if mdl_bone.BoneName.lower() in ['Root', 'pelvis'] else Vector(( 0, 0, 0 ))
                    else:
                        pos = pos_fix
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
                rot_dt = bone.rotation_dt if bone.rotation_dt and bone.rotation_dt > 0 else base_dt
                rot_dt = round(rot_dt, 8)
                rot_resampled = False
                if witcher_bake_every_frame and _needs_resample(rot_frames, rot_dt):
                    rot_frames, rot_resampled = resample_rotation_frames(rot_frames, rot_dt, duration, total_frames)
                    if smooth_missing and rot_resampled:
                        anchor_mask = build_anchor_mask(total_frames, base_dt, rot_dt)
                        rot_frames = smooth_quaternion_frames(rot_frames, anchor_mask)
                num_rot_frames = len(rot_frames)
                
                prev_rot_frame = None
                last_frame = total_frames - 1
                for n, rot_frame in enumerate(rot_frames):
                    if witcher_bake_every_frame:
                        if rot_resampled or num_rot_frames == total_frames:
                            frame = n
                        else:
                            frame = n * (rot_dt / base_dt if base_dt > 0 else 1.0)
                    else:
                        if num_rot_frames == 1:
                            frame = 0.0
                        elif witcher_scale_keys_to_duration and num_rot_frames > 1:
                            frame = n * (last_frame / (num_rot_frames - 1))
                        else:
                            frame = n * (rot_dt / base_dt if base_dt > 0 else 1.0)
                            if n == num_rot_frames - 1:
                                frame = last_frame
                            elif frame > last_frame:
                                frame = last_frame
                        if prev_rot_frame is not None and abs(frame - prev_rot_frame) < 1e-6:
                            continue
                        prev_rot_frame = frame
                    fixed_rot = rot_frame
                    
                    if not face_animation and SkeletalAnimationType != "SAT_Additive":
                        rotate_by = 0
                        if use_rot90:
                            rotate_by = 90
                        
                        # Constants
                        z_plus_90 = Quaternion((0, 0, 1), math.radians(-rotate_by))
                        z_minus_90 = Quaternion((0, 0, 1), math.radians(rotate_by))
                        rotation_matrix = Matrix.Rotation(math.radians(rotate_by), 4, 'Z')

                        if bl_bone.parent:
                            origRotP_matrix = bl_bone.parent.bone.matrix_local @ rotation_matrix
                            origRotP = origRotP_matrix.to_quaternion()
                            fixed_rot = origRotP @ fixed_rot

                        origRot_matrix = bl_bone.bone.matrix_local @ rotation_matrix
                        origRot = origRot_matrix.to_quaternion()
                        fixed_rot = origRot.inverted() @ fixed_rot

                        # Apply the axis swap
                        fixed_rot = z_minus_90 @ fixed_rot @ z_plus_90

                        # Normalize the quaternion
                        fixed_rot.normalize()
                    else:
                        if use_rot90:
                            rotate_by = 90
                            z_plus_90 = Quaternion((0, 0, 1), math.radians(-rotate_by))
                            z_minus_90 = Quaternion((0, 0, 1), math.radians(rotate_by))
                            fixed_rot = z_minus_90 @ fixed_rot @ z_plus_90
                            fixed_rot.normalize()

                    for i in range(4):
                        rot_curves[i].keyframe_points.add(1)
                        rot_curves[i].keyframe_points[-1].co = (frame, fixed_rot[i])
                        rot_curves[i].keyframe_points[-1].interpolation = 'LINEAR'
        _safe_mode_set('OBJECT', armObj)
        log.info(' Finished adding keyframes in %f seconds.', time.time() - start_time)

        control_bone_name = "w3_face_poses"
        if camera_animation:
            control_bone_name = "Camera_Node"
        AnimTracks = SkeletalAnimationData.tracks
        morph_action_target = None
        if AnimTracks and len(AnimTracks) > 1:
            log.info('---- morph animations:%5d  target: %s', len(AnimTracks), armObj.name)

            control_arm_obj = armObj
            control_bone = control_arm_obj.pose.bones.get(control_bone_name)
            if control_bone is None:
                log.warning('No shape key control bone "%s" on "%s". Attempting to load face morphs.', control_bone_name, armObj.name)
                try:
                    from ..ui.ui_anims_list import ensure_owner_face_animation_setup

                    _loaded, ensured_arm_obj = ensure_owner_face_animation_setup(bpy.context, armObj)
                    if ensured_arm_obj and getattr(ensured_arm_obj, "type", None) == 'ARMATURE':
                        control_arm_obj = ensured_arm_obj
                    control_bone = control_arm_obj.pose.bones.get(control_bone_name)
                except Exception as exc:
                    log.warning('Failed to auto-load face morphs on "%s": %s', armObj.name, exc)
            if control_bone is None:
                log.warning('Shape key control bone "%s" still missing on "%s" after face morph attempt. Skipping morph tracks.', control_bone_name, armObj.name)
            else:
                if control_arm_obj != armObj:
                    morph_action_target = control_arm_obj
                mirror_map = {}#_MirrorMapper(meshObj.data.shape_keys.key_blocks) if self.__mirror else {}
                shapeKeyDict = {k:mirror_map.get(k, v) for k, v in control_bone.items()}


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
                    if total_frames == 0:
                        # numFrames not set in chunk (e.g. WolvenKit-cooked files); distribute over actual track frames
                        total_frames = track_frames
                    #frame_skip = round(float(total_frames)/float(track_frames))
                    frame_skip = float(total_frames)/float(track_frames) if track_frames > 0 else 1.0

                    log.info('(mesh) frames:%5d  name: %s', len(keyFrames), name)
                    shapeKey = shapeKeyDict[name]
                    fcurve = new_action_fcurve(action, control_arm_obj, data_path='pose.bones["%s"]["%s"]'% (control_bone_name, name))#  (data_path='key_blocks["%s"].value'%shapeKey.name)
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
        if morph_action_target is not None and morph_action_target != armObj:
            self.__assign_action(morph_action_target, action)
        
    def __assignToArmature(self, armObj, action_name=None):

        def detect_maya_namespace(s):
            if ':' in s:
                srp = s.rpartition(':')
                return srp[0]+":"
            else:
                return None
        SkeletalAnimationType = self.__SetEntry.animation.SkeletalAnimationType
        AdditiveType = self.__SetEntry.animation.AdditiveType
        if not armObj.pose.bones:
            log.warning("Animation target armature has no pose bones: %s", armObj.name)
            return
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
            fcurve = new_action_fcurve(action, meshObj.data.shape_keys, data_path='key_blocks["%s"].value'%shapeKey.name)
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
            _set_active_object(obj)
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

def NewW2ANIMSListItem( treeList, node):
    item = treeList.add()
    item.name = node.animation.name
    item.framesPerSecond = node.animation.framesPerSecond
    item.numFrames = node.animation.animBuffer.numFrames
    item.duration = node.animation.duration
    item.SkeletalAnimationType = node.animation.SkeletalAnimationType
    if node.animation.AdditiveType:
        item.AdditiveType = node.animation.AdditiveType
    if node.animation.motionExtraction:
        item.RootMotion = True
    return item

def _vector_from_props(props):
    if not props:
        return None
    values = {}
    for prop in props:
        name = getattr(prop, "theName", None)
        if name in ("X", "Y", "Z", "W"):
            if hasattr(prop, "Value"):
                values[name] = prop.Value
            elif hasattr(prop, "value") and isinstance(prop.value, (int, float)):
                values[name] = prop.value
    if not values:
        return None
    return (
        float(values.get("X", 0.0)),
        float(values.get("Y", 0.0)),
        float(values.get("Z", 0.0)),
        float(values.get("W", 0.0)),
    )

def _extract_vector_frames(frames_prop):
    frames = []
    if frames_prop is None:
        return frames

    elements = getattr(frames_prop, "elements", None)
    if elements:
        for elem in elements:
            props = getattr(elem, "More", None)
            if props is None:
                props = getattr(elem, "MoreProps", None)
            vec = _vector_from_props(props)
            if vec is not None:
                frames.append(vec)
        if frames:
            return frames

    more = getattr(frames_prop, "More", None)
    if more:
        for elem in more:
            props = getattr(elem, "MoreProps", None)
            if props is None:
                props = getattr(elem, "More", None)
            vec = _vector_from_props(props)
            if vec is not None:
                frames.append(vec)
        if frames:
            return frames

    value = getattr(frames_prop, "value", None)
    if isinstance(value, list):
        for elem in value:
            if isinstance(elem, (list, tuple)) and len(elem) >= 3:
                x, y, z = elem[0], elem[1], elem[2]
                w = elem[3] if len(elem) > 3 else 0.0
                frames.append((float(x), float(y), float(z), float(w)))
            elif isinstance(elem, dict):
                x = elem.get("X", elem.get("x", 0.0))
                y = elem.get("Y", elem.get("y", 0.0))
                z = elem.get("Z", elem.get("z", 0.0))
                w = elem.get("W", elem.get("w", 0.0))
                frames.append((float(x), float(y), float(z), float(w)))
    return frames

def _build_uncompressed_motion_extraction(frames, duration):
    if not frames:
        return None
    base = frames[0]
    x_vals = [v[0] - base[0] for v in frames]
    y_vals = [v[1] - base[1] for v in frames]
    z_vals = [v[2] - base[2] for v in frames]
    yaw_vals = [v[3] - base[3] for v in frames]

    flags = 0
    if any(abs(v) > 1e-6 for v in x_vals):
        flags |= 1
    if any(abs(v) > 1e-6 for v in z_vals):
        flags |= 2
    if any(abs(v) > 1e-6 for v in y_vals):
        flags |= 4
    if any(abs(v) > 1e-6 for v in yaw_vals):
        flags |= 8

    flat_frames = []
    for v in frames:
        if flags & 1:
            flat_frames.append(v[0])
        if flags & 2:
            flat_frames.append(v[2])
        if flags & 4:
            flat_frames.append(v[1])
        if flags & 8:
            flat_frames.append(v[3])

    delta_times = [1] * max(0, len(frames) - 1)
    if duration is None:
        fps = float(bpy.context.scene.render.fps or 30.0)
        duration = len(frames) / fps if fps > 0 else 0.0
    return MotionExtraction(duration=duration, delta_times=delta_times, frames=flat_frames, flags=flags)

def _expand_motion_extraction_frames(motion_extraction, total_frames):
    """Expand MotionExtraction into per-frame axis arrays in Blender space."""
    if not motion_extraction or total_frames <= 0:
        return None
    flags = motion_extraction.flags
    frames = motion_extraction.frames or []
    delta_times = motion_extraction.delta_times or []
    num_movements = sum([(flags & 1) > 0, (flags & 2) > 0, (flags & 4) > 0, (flags & 8) > 0])
    if num_movements == 0 or not frames:
        return None

    axis_order = []
    if flags & 1:
        axis_order.append("x")
    if flags & 2:
        axis_order.append("y")  # apply_motion maps flag2 to Blender Y
    if flags & 4:
        axis_order.append("z")  # apply_motion maps flag4 to Blender Z
    if flags & 8:
        axis_order.append("yaw")

    axis_values = {
        "x": [0.0] * total_frames,
        "y": [0.0] * total_frames,
        "z": [0.0] * total_frames,
        "yaw": [0.0] * total_frames,
    }
    current = {"x": 0.0, "y": 0.0, "z": 0.0, "yaw": 0.0}

    frame_number = 0
    frame_idx = 0
    step = 0
    while frame_idx < len(frames) and frame_number < total_frames:
        values = frames[frame_idx:frame_idx + num_movements]
        frame_idx += num_movements
        for axis, value in zip(axis_order, values):
            current[axis] = float(value)
        next_frame = frame_number + (delta_times[step] if step < len(delta_times) else 1)
        next_frame = min(next_frame, total_frames)
        for f in range(frame_number, next_frame):
            axis_values["x"][f] = current["x"]
            axis_values["y"][f] = current["y"]
            axis_values["z"][f] = current["z"]
            axis_values["yaw"][f] = current["yaw"]
        frame_number = next_frame
        step += 1

    for f in range(frame_number, total_frames):
        axis_values["x"][f] = current["x"]
        axis_values["y"][f] = current["y"]
        axis_values["z"][f] = current["z"]
        axis_values["yaw"][f] = current["yaw"]

    return axis_values

def _choose_uncompressed_mapping(frames, compressed_axes):
    """Pick between raw XYZ and Y/Z-swapped to best match compressed data."""
    if not compressed_axes or not frames:
        return "raw"
    total = len(frames)
    first = frames[0]
    def make_series(mode):
        series = {"x": [], "y": [], "z": []}
        for v in frames:
            dx = v[0] - first[0]
            dy = v[1] - first[1]
            dz = v[2] - first[2]
            if mode == "swap_yz":
                dy, dz = dz, dy
            # Map into Blender axes as apply_motion will do:
            # X -> X, Z -> Y, Y -> Z
            series["x"].append(dx)
            series["y"].append(dz)
            series["z"].append(dy)
        return series

    def score(series):
        err = 0.0
        for axis in ("x", "y", "z"):
            comp = compressed_axes.get(axis)
            if not comp:
                continue
            for i in range(min(total, len(comp))):
                err += abs(series[axis][i] - comp[i])
        return err

    raw_series = make_series("raw")
    swap_series = make_series("swap_yz")
    if score(swap_series) < score(raw_series):
        return "swap_yz"
    return "raw"

import bmesh
def create_lopsided_cube():
    # Create a cube
    bpy.ops.mesh.primitive_cube_add(location=(0, 0, 0))
    cube = bpy.context.object

    # Scale the cube to specified dimensions
    cube.scale = (0.01, 0.04, 0.01)

    # Enter edit mode to adjust vertices
    bpy.ops.object.mode_set(mode='EDIT')

    # Use BMesh to modify vertices
    bm = bmesh.from_edit_mesh(cube.data)

    # Select the vertices on the y+ side
    for vert in bm.verts:
        if vert.co.y > 0:
            vert.select = True
        else:
            vert.select = False

    # Scale down the selected vertices uniformly
    bmesh.ops.scale(bm, vec=(0.5, 0.5, 0.5), verts=[v for v in bm.verts if v.select])

    # Update the mesh and return to object mode
    bmesh.update_edit_mesh(cube.data)
    bpy.ops.object.mode_set(mode='OBJECT')

    return cube

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
    
    import_compressed_motion = getattr(context.scene, "witcher_motion_extraction_debug_compressed", False)
    import_uncompressed_motion = getattr(context.scene, "witcher_motion_extraction_debug_uncompressed", False)

    if import_compressed_motion and AnimationSetEntry.animation.motionExtraction:
        motion_data = AnimationSetEntry.animation.motionExtraction
        try:
            cube = create_lopsided_cube()
            cube.show_axis = True
            cube.name = AnimationSetEntry.animation.name + "_motion"

            motion_extraction = MotionExtraction(
                duration=motion_data['duration'],
                delta_times=motion_data['deltaTimes'],
                frames=motion_data['frames'],
                flags=motion_data['flags']
            )

            log.info(f"Motion extraction (compressed): frames={len(motion_data['frames'])}, deltaTimes={motion_data['deltaTimes']}, flags={motion_data['flags']}")
            apply_motion(cube, motion_extraction)
        finally:
            bpy.ops.object.select_all(action='DESELECT')
            for obj in selected_objects:
                obj.select_set(True)
                bpy.context.view_layer.objects.active = obj

    if import_uncompressed_motion:
        uncompressed_chunk = getattr(AnimationSetEntry.animation, "uncompressedMotionExtraction", None)
        if uncompressed_chunk:
            frames_prop = uncompressed_chunk.GetVariableByName("frames") if hasattr(uncompressed_chunk, "GetVariableByName") else None
            duration_prop = uncompressed_chunk.GetVariableByName("duration") if hasattr(uncompressed_chunk, "GetVariableByName") else None
            frames = _extract_vector_frames(frames_prop)
            duration = duration_prop.Value if duration_prop and hasattr(duration_prop, "Value") else None

            if not frames:
                log.warning("Uncompressed motion extraction frames not found or could not be parsed.")
            else:
                compressed_axes = None
                if AnimationSetEntry.animation.motionExtraction:
                    motion_data = AnimationSetEntry.animation.motionExtraction
                    compressed_me = MotionExtraction(
                        duration=motion_data['duration'],
                        delta_times=motion_data['deltaTimes'],
                        frames=motion_data['frames'],
                        flags=motion_data['flags']
                    )
                    compressed_axes = _expand_motion_extraction_frames(compressed_me, len(frames))

                mapping = _choose_uncompressed_mapping(frames, compressed_axes)
                if mapping == "swap_yz":
                    remapped = [(v[0], v[2], v[1], v[3]) for v in frames]
                else:
                    remapped = list(frames)

                motion_extraction = _build_uncompressed_motion_extraction(remapped, duration)
                if motion_extraction:
                    try:
                        cube = create_lopsided_cube()
                        cube.show_axis = True
                        cube.name = AnimationSetEntry.animation.name + "_motion_uncompressed"
                        log.info(f"Motion extraction (uncompressed): frames={len(frames)}, flags={motion_extraction.flags}, mapping={mapping}")
                        apply_motion(cube, motion_extraction)
                    finally:
                        bpy.ops.object.select_all(action='DESELECT')
                        for obj in selected_objects:
                            obj.select_set(True)
                            bpy.context.view_layer.objects.active = obj
        else:
            log.info("No uncompressed motion extraction data found.")

        if False:
            #! IMPORT AN OBJECT TO DEBUG
            motion_frames = apply_motion_to_bone(AnimationSetEntry.animation)
            frames = w3_types.w2AnimsFrames(0,
                            BoneName = motion_frames['BoneName'],
                            position_dt = motion_frames['position_dt'],
                            position_numFrames = motion_frames['position_numFrames'],
                            positionFrames = motion_frames['positionFrames'],
                            rotation_dt = motion_frames['rotation_dt'],
                            rotation_numFrames = motion_frames['rotation_numFrames'],
                            rotationFrames = motion_frames['rotationFrames'],
                            scale_dt = motion_frames['scale_dt'],
                            scale_numFrames = motion_frames['scale_numFrames'],
                            scaleFrames = motion_frames['scaleFrames'],
                            rotationFramesQuat = motion_frames['rotationFrames'])
            test_extraction = extract_motion_from_bone(frames)
            
            #AnimationSetEntry.animation.animBuffer.bones[0] = frames
            #AnimationSetEntry.animation.animBuffer.bones[0].BoneName = 'Root'
            AnimationSetEntry.animation.animBuffer.bones.append(frames)
            
            # keyframe_values = AnimationSetEntry.animation.motionExtraction['frames']
            # bpy.ops.object.empty_add()
            # empty_obj = bpy.context.active_object
            # for frame, value in enumerate(keyframe_values, start=1):
            #     empty_obj.location.x = value
            #     empty_obj.keyframe_insert(data_path="location", index=0, frame=frame)
    
    importer = AnimImporter(fileName, AnimationSetEntry, use_NLA=use_NLA, facePose=facePose, NLA_track = NLA_track, at_frame=at_frame)
    for i in selected_objects:
        importer.assign(i)
    log.info(' Finished importing motion in %f seconds.', time.time() - start_time)

    #update_scene_settings = True # MAKE BLEND IMPORT PROP
    if update_scene_settings:
        target_for_ranges = None
        for obj in selected_objects:
            if obj and getattr(obj, "type", None) == 'ARMATURE':
                target_for_ranges = obj
                break
        if target_for_ranges is None:
            for obj in selected_objects:
                if obj:
                    target_for_ranges = obj
                    break
        auto_scene_setup.setupFrameRanges(use_NLA, target_obj=target_for_ranges)
        auto_scene_setup.setupFps()
        bpy.context.scene.frame_current = 0#context.scene.frame_current)
    return {'FINISHED'}

# global GLOBAL_ANIMSET

# def get_global_set():
#     global GLOBAL_ANIMSET
#     return GLOBAL_ANIMSET

# def set_global_set(the_set):
#     global GLOBAL_ANIMSET
#     GLOBAL_ANIMSET = the_set

def import_from_list_item(context, item, ANIMSET, target_obj=None):
    for anim_set_entry in ANIMSET.animations:
        if anim_set_entry.animation.name == item.name:
            if ':face' in str(anim_set_entry.animation.name or "").lower():
                import_anim(
                    context,
                    "lipsync_from_list",
                    anim_set_entry,
                    use_NLA=True,
                    NLA_track="mimic_import",
                    override_select=target_obj if target_obj else False,
                )
            else:
                import_anim(
                    context,
                    "from_list",
                    anim_set_entry,
                    override_select=target_obj if target_obj else False,
                )

def _is_w2_cr2w_version(filename):
    """Check if a file is a Witcher 2 CR2W file (version <= 115)."""
    try:
        import struct
        with open(filename, 'rb') as f:
            magic = f.read(4)
            if magic != b'CR2W':
                return False
            version = struct.unpack('<I', f.read(4))[0]
            return version <= 115
    except Exception:
        return False


def import_w3_animSet(filename, rigPath = False)->w3_types.CSkeletalAnimationSet:
    dirpath, file = os.path.split(filename)
    basename, ext = os.path.splitext(file)
    if ext.lower() in ('.json'):
        with open(filename) as file:
            return read_json_w3.Read_CSkeletalAnimationSet(json.loads(file.read()))
    elif ext.lower().endswith('.w2anims'):
        if _is_w2_cr2w_version(filename):
            return load_w2_anims_info(filename)
        return load_bin_anims_info(filename, rigPath=rigPath)
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

    #!REMOVE
    #TODO MUST RECORD THE ABSOLOUTE PATH AND REPO PATH OF THE LOADED ANIM SET AND LINK IT TO THE ITEMS
    #TODO REMOVE global GLOBAL_ANIMSET and use something else to store the loaded set.
    #!/REMOVE
    treeList = context.scene.witcher_w2anims_list
    treeList.clear()
    context.scene.witcher_loaded_w2anims_path = fileName
    # Persist a compact source tag so the UI can show whether the loaded set is W2/W3/etc.
    source_tag = "MEMORY" if load_from_data else "FILE"
    try:
        if fileName:
            lower_name = str(fileName).lower()
            if lower_name.endswith(".json"):
                source_tag = "JSON"
            elif lower_name.endswith(".w2anims"):
                source_tag = "W2" if _is_w2_cr2w_version(fileName) else "W3"
    except Exception:
        pass
    if hasattr(context.scene, "witcher_loaded_w2anims_source_tag"):
        context.scene.witcher_loaded_w2anims_source_tag = source_tag
    # global GLOBAL_ANIMSET
    # GLOBAL_ANIMSET = animSetTemplate
    for node in animSetTemplate.animations:
        item = NewW2ANIMSListItem(treeList, node)


def load_idle_animation_for_armature(context, armature_obj):
    """Load and apply the idle animation stored on an armature's rig_settings.

    Returns True if the idle animation was applied, False otherwise.
    """
    try:
        rig_settings = getattr(getattr(armature_obj, "data", None), "witcherui_RigSettings", None)
        if rig_settings is None:
            return False

        idle_name = (getattr(rig_settings, "idle_animation_name", "") or "").strip()
        if not idle_name:
            return False

        # Collect all real animset paths (skip group header entries that end with ":")
        anim_paths = []
        for item in getattr(rig_settings, "animset_list", []) or []:
            p = (getattr(item, "path", "") or "").strip()
            if p and not p.endswith(":"):
                anim_paths.append(p)
        if not anim_paths:
            return False

        from ..CR2W.common_blender import repo_file as _repo_file
        from ..CR2W.dc_anims import load_bin_anims_single
        import os
        from .. import get_uncook_path

        skel = (getattr(rig_settings, "main_entity_skeleton", "") or "").strip()
        rig_path = _repo_file(skel) if skel else None

        for anim_path in anim_paths:
            # Phase 1: locate animation entry — catch parse/IO errors, skip to next animset
            animation = None
            try:
                _repo_file(anim_path)
                fdir = os.path.join(get_uncook_path(context), anim_path)
                if os.path.exists(fdir + ".json"):
                    fdir += ".json"
                if not os.path.exists(fdir):
                    continue

                _, ext = os.path.splitext(fdir)
                if ext.lower() == ".json":
                    anim_set = import_w3_animSet(fdir, rig_path)
                    if not anim_set:
                        continue
                    def _ename(e):
                        return getattr(getattr(e, "animation", None), "name", None) or ""
                    animation = next((e for e in anim_set.animations if _ename(e) == idle_name), None)
                    if animation is None:
                        animation = next((e for e in anim_set.animations if _ename(e).lower() == idle_name.lower()), None)
                else:
                    result = load_bin_anims_single(fdir, idle_name, rigPath=rig_path)
                    if result and result.animations:
                        animation = result.animations[0]
            except Exception:
                log.debug("load_idle_animation_for_armature: parse error in %s", anim_path, exc_info=True)
                continue

            if animation is None:
                continue

            # Phase 2: apply to Blender — errors here are real failures, log at WARNING
            log.debug("load_idle_animation_for_armature: found '%s' in %s, applying…", idle_name, anim_path)
            import_anim(context, fdir, animation, use_NLA=False, override_select=armature_obj)
            log.info("Applied idle animation '%s' to %s", idle_name, getattr(armature_obj, "name", "?"))
            return True

        log.warning("load_idle_animation_for_armature: '%s' not found in any of %d animsets for %s",
                    idle_name, len(anim_paths), getattr(armature_obj, "name", "?"))
        return False

    except Exception:
        log.warning("load_idle_animation_for_armature failed: %s", getattr(armature_obj, "name", "?"), exc_info=True)
        return False
