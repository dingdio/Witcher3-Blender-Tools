
import logging
from mathutils import Vector, Quaternion, Euler, Matrix
import base64
import json
import math
from ..CR2W import w3_types
from ..CR2W.om import MQuaternion
from ..animation.action_compat import assign_action, iter_action_fcurves, new_action_fcurve
import bpy
from bpy.app.handlers import persistent

log = logging.getLogger(__name__)

W3_MOTION_EXTRACTION_DATA_PROP = "w3_motion_extraction_data"

class MotionExtraction:
    def __init__(self, duration, delta_times, frames, flags):
        self.duration = duration
        self.delta_times = delta_times
        self.frames = frames
        self.flags = flags

### ! EXTRACTION 
def get_bone_animation_data(obj, bone_name):
    keyframes = set()
    action = obj.animation_data.action
    if not action:
        # Iterate over NLA tracks and strips
        for track in obj.animation_data.nla_tracks:
            for strip in track.strips:
                if strip.action:
                    action = strip.action
                    break
    if action:
        for fcurve in iter_action_fcurves(action, target=obj):
            if bone_name in fcurve.data_path:
                for keyframe in fcurve.keyframe_points:
                    frame_number = int(keyframe.co[0])
                    keyframes.add(frame_number)
    keyframes = sorted(keyframes)
    return keyframes

def simplify_curve(keyframes, values, threshold=0.01):
    """Simplify a curve, returning list of (frame_number, value) tuples."""
    simplified = [(keyframes[0], values[0])]
    for i in range(1, len(keyframes) - 1):
        prev_frame, prev_value = simplified[-1]
        next_frame, next_value = keyframes[i + 1], values[i + 1]

        if abs(values[i] - prev_value) > threshold or abs(next_value - values[i]) > threshold:
            simplified.append((keyframes[i], values[i]))
    simplified.append((keyframes[-1], values[-1]))
    return simplified


def _interp_simplified(frame, simplified):
    """Linearly interpolate a simplified curve at the given frame number.

    simplified: list of (frame_number, value) tuples, sorted by frame.
    """
    if frame <= simplified[0][0]:
        return simplified[0][1]
    if frame >= simplified[-1][0]:
        return simplified[-1][1]
    for i in range(1, len(simplified)):
        if frame <= simplified[i][0]:
            f0, v0 = simplified[i - 1]
            f1, v1 = simplified[i]
            if f1 == f0:
                return v0
            t = (frame - f0) / (f1 - f0)
            return v0 + t * (v1 - v0)
    return simplified[-1][1]

def cline_from_per_frame(per_frame_data, frame_numbers, fps, threshold=0.01):
    """Derive CLineMotionExtraction2 from per-frame motion data.

    per_frame_data: list of (dx, dy, dz, dyaw) tuples, one per frame,
                    relative to first frame (deltas).
    frame_numbers:  list of integer frame numbers matching per_frame_data.
    fps:            scene frames per second.
    threshold:      simplification threshold.

    Returns: MotionExtraction or None if insufficient data.
    """
    if not per_frame_data or len(per_frame_data) < 2:
        return None
    if fps <= 0:
        log.warning("FPS is zero or negative, cannot compute motion extraction duration")
        return None

    duration = (frame_numbers[-1] - frame_numbers[0]) / fps

    x_vals  = [f[0] for f in per_frame_data]
    y_vals  = [f[1] for f in per_frame_data]
    z_vals  = [f[2] for f in per_frame_data]
    yaw_vals = [f[3] for f in per_frame_data]

    flags = 0
    if any(abs(v) > threshold for v in x_vals):   flags |= 1
    if any(abs(v) > threshold for v in y_vals):   flags |= 2
    if any(abs(v) > threshold for v in z_vals):   flags |= 4
    if any(abs(v) > threshold for v in yaw_vals): flags |= 8

    if flags == 0:
        return MotionExtraction(duration, [], [], 0)

    axis_data = [('x', 1, x_vals), ('y', 2, y_vals),
                 ('z', 4, z_vals), ('yaw', 8, yaw_vals)]
    simplified = {}
    unified_frame_set = set()

    for key, bit, vals in axis_data:
        if flags & bit:
            s = simplify_curve(list(frame_numbers), vals, threshold)
            simplified[key] = s
            for frame_num, _val in s:
                unified_frame_set.add(frame_num)

    unified_frames = sorted(unified_frame_set)

    delta_times = []
    for i in range(1, len(unified_frames)):
        delta_times.append(int(unified_frames[i] - unified_frames[i - 1]))

    axis_map = [('x', 1), ('y', 2), ('z', 4), ('yaw', 8)]
    flat_frames = []
    for frame_num in unified_frames:
        for key, bit in axis_map:
            if flags & bit:
                flat_frames.append(_interp_simplified(frame_num, simplified[key]))

    return MotionExtraction(duration, delta_times, flat_frames, flags)


def extract_motion_data(obj, trajectory_bone_name, threshold=0.01):
    keyframes = get_bone_animation_data(obj, trajectory_bone_name)
    if not keyframes:
        raise ValueError(f"No keyframes found for bone: {trajectory_bone_name}")

    frame_rate = bpy.context.scene.render.fps
    duration = (keyframes[-1] - keyframes[0]) / frame_rate

    # pose_bone.matrix is in armature object space and already includes the
    # full parent chain (Root).  With rot90, Root's rest = Rot(-90°Z) cancels
    # the import's +90°Z position compensation, so the armature-space position
    # of Trajectory equals the game world-space position directly.
    # No additional Root.matrix multiplication needed.
    movements = {'x': [], 'y': [], 'z': [], 'yaw': []}

    bpy.context.scene.frame_set(keyframes[0])
    trajectory_bone = obj.pose.bones[trajectory_bone_name]
    initial_position = trajectory_bone.matrix.to_translation()
    initial_yaw = trajectory_bone.matrix.to_euler('XYZ').z

    for frame in keyframes:
        bpy.context.scene.frame_set(frame)
        trajectory_bone = obj.pose.bones[trajectory_bone_name]
        position = trajectory_bone.matrix.to_translation()
        yaw = trajectory_bone.matrix.to_euler('XYZ').z

        movements['x'].append(position.x - initial_position.x)
        movements['y'].append(position.y - initial_position.y)
        movements['z'].append(position.z - initial_position.z)
        movements['yaw'].append(yaw - initial_yaw)

    # Determine which movements are significant
    flags = 0
    if any(abs(x) > threshold for x in movements['x']):
        flags |= 1
    if any(abs(y) > threshold for y in movements['y']):
        flags |= 2
    if any(abs(z) > threshold for z in movements['z']):
        flags |= 4
    if any(abs(yaw) > threshold for yaw in movements['yaw']):
        flags |= 8

    # Simplify each active axis and collect unified frame set
    axis_map = [('x', 1), ('y', 2), ('z', 4), ('yaw', 8)]
    simplified = {}
    unified_frame_set = set()

    for key, bit in axis_map:
        if flags & bit:
            s = simplify_curve(keyframes, movements[key], threshold)
            simplified[key] = s
            for frame_num, val in s:
                unified_frame_set.add(frame_num)

    unified_frames = sorted(unified_frame_set)

    # Compute delta_times between consecutive unified frames
    delta_times = []
    for i in range(1, len(unified_frames)):
        delta_times.append(int(unified_frames[i] - unified_frames[i - 1]))

    # Build interleaved frames array by interpolating values at unified frames
    flat_frames = []
    for frame_num in unified_frames:
        for key, bit in axis_map:
            if flags & bit:
                flat_frames.append(_interp_simplified(frame_num, simplified[key]))

    return duration, delta_times, flat_frames, flags

def generate_motion_extraction(obj, trajectory_bone_name, threshold=0.01):
    duration, delta_times, frames_data, flags = extract_motion_data(obj, trajectory_bone_name, threshold)
    return MotionExtraction(duration, delta_times, list(frames_data), flags)
### !/ EXTRACTION END

def apply_motion(obj, motion_extraction):
    # Validate motion extraction data
    if not motion_extraction.frames:
        log.warning("motion_extraction.frames is empty, skipping keyframe creation for %s", obj.name)
        return

    if motion_extraction.flags == 0:
        log.warning("motion_extraction.flags is 0, no movement components enabled for %s", obj.name)
        return

    # Ensure the object is selected and active
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj

    # Create a new action for the object
    action = bpy.data.actions.new(name="MotionExtractionAction")
    assign_action(obj, action)

    # Flags interpretation
    x_movement = (motion_extraction.flags & 1) > 0
    z_movement = (motion_extraction.flags & 2) > 0
    y_movement = (motion_extraction.flags & 4) > 0
    yaw_rotation = (motion_extraction.flags & 8) > 0

    # Create FCurves based on flags
    fcurves = []
    if x_movement:
        fcurves.append(new_action_fcurve(action, obj, data_path="location", index=0))
    if z_movement:
        fcurves.append(new_action_fcurve(action, obj, data_path="location", index=1))
    if y_movement:
        fcurves.append(new_action_fcurve(action, obj, data_path="location", index=2))
    if yaw_rotation:
        fcurves.append(new_action_fcurve(action, obj, data_path="rotation_euler", index=2))

    # Insert keyframes
    frame_number = 0
    num_movements = sum([x_movement, z_movement, y_movement, yaw_rotation])
    for i in range(0, len(motion_extraction.frames), num_movements):
        movement_data = motion_extraction.frames[i:i + num_movements]
        for j, fcurve in enumerate(fcurves):
            fcurve.keyframe_points.insert(frame_number, movement_data[j])
        
        if i // num_movements < len(motion_extraction.delta_times):
            frame_number += motion_extraction.delta_times[i // num_movements]

    # Set interpolation to linear for all FCurves
    for fcurve in fcurves:
        for kp in fcurve.keyframe_points:
            kp.interpolation = 'LINEAR'

    # Set extrapolation mode to cyclic and after mode to "repeat with offset"
    # for fcurve in fcurves:
    #     fcurve.modifiers.new(type='CYCLES')
    #     fcurve.extrapolation = 'LINEAR'
    #     cycles_mod = fcurve.modifiers[0]
    #     cycles_mod.mode_after = 'REPEAT_OFFSET'


    # Set the frame range for the animation
    #total_frames = sum(motion_extraction.delta_times)

def framesToSec(frames, fps):
    return float(frames) / fps

def secToFrames(sec, fps):
    return int(sec * fps + 0.05)

def objXYZ(X, Y, Z):
    ret = {}
    ret["x"] = float(X)
    ret["y"] = float(Y)
    ret["z"] = float(Z)
    return ret

def objToXYZ(obj):
    if obj.isEmpty():
        log.debug("Empty motion extraction object")
    X = float(obj["x"])
    Y = float(obj["y"])
    Z = float(obj["z"])
    return X, Y, Z

def objToXYZW(obj):
    X = float(obj["X"])
    Y = float(obj["Y"])
    Z = float(obj["Z"])
    W = float(obj["W"])
    return X, Y, Z, W

def objXYZW(X, Y, Z, W):
    return MQuaternion(float(X), float(Y), float(Z), float(W))

def objQuanternion(Pitch, Yaw, Roll):
    vec4 = Euler((math.radians(Pitch), math.radians(Yaw), math.radians(Roll)), 'XYZ').to_quaternion()
    return objXYZW(vec4.x, vec4.y, vec4.z, vec4.w)

def interpolatePos(k, X1, Y1, Z1, X2, Y2, Z2):
    X1 = X1 * (1.0 - k) + X2 * k
    Y1 = Y1 * (1.0 - k) + Y2 * k
    Z1 = Z1 * (1.0 - k) + Z2 * k
    return X1, Y1, Z1

def interpolateRot(k, X1, Y1, Z1, W1, X2, Y2, Z2, W2):
    q1 = Quaternion(W1, X1, Y1, Z1)
    q2 = Quaternion(W2, X2, Y2, Z2)
    q1 = Quaternion.nlerp(q1, q2, k)
    return q1.x(), q1.y(), q1.z(), q1.scalar()


def blendMotion(motion, animFrames, framePoints):
    if not motion:
        motion.extend([0] * (animFrames + 1))
        return motion

    res = []
    # print("+++ motion: ", motion)

    pointIdx = 0
    delta = 0
    res.append(motion[0])

    for frame in range(1, animFrames + 1):
        res.append(res[-1] + delta)
        # dbg = ""

        if frame == framePoints[pointIdx]:
            res[-1] = motion[pointIdx]
            if frame < animFrames:
                delta = (motion[pointIdx + 1] - motion[pointIdx]) / (framePoints[pointIdx + 1] - framePoints[pointIdx])
                pointIdx += 1

            # dbg += "! "
        # dbg += f"[{frame}] = {res[frame]}"
        # print(dbg)

    motion[:] = res
    return motion

def blendPos(posArray, targetFrames):
    resArray = []
    frames = len(posArray)
    log.debug("blendPos [%d] -> [%d]", frames, targetFrames)
    
    if frames > targetFrames:
        log.error("BlendPos: can't bake! posArray size (%d) is bigger than required (%d)", frames, targetFrames)
    
    if frames == targetFrames:
        return posArray
    
    if frames == 1:
        while len(resArray) < targetFrames:
            resArray.append(posArray[0])
        return resArray
    
    resArray.append(posArray[0])

    partSize = (targetFrames - 1.0) / (frames - 1.0)
    currentPartSize = 0.0
    log.debug("blendPos.partSize = %s", partSize)
    
    j = 0
    for i in range(2, targetFrames, 1):
        currentPartSize += 1.0
        
        if currentPartSize > partSize:
            j += 1
            currentPartSize -= partSize
        
        k = currentPartSize / partSize
        k = min(1.0, max(0.0, k))
        
        # [j..j+1]
        X1, Y1, Z1 = objToXYZ(posArray[j])
        X2, Y2, Z2 = objToXYZ(posArray[j + 1])
        
        interpolated_pos = interpolatePos(k, X1, Y1, Z1, X2, Y2, Z2)
        resArray.append(objXYZ(*interpolated_pos))

    log.debug("blendPos.lastK = %s (must be around 1.0)", (currentPartSize + 1.0) / partSize)
    resArray.append(posArray[-1])
    
    return resArray

import base64
from typing import Dict, Union

def reverse_motion_from_bone(mBoneObj: Dict[str, Union[str, int, float, list]]) -> Dict[str, Union[str, int, float, list]]:
    checkSwapYZpos = False # motion: Use Y-up coordinates
    checkUseYRot = False # Use Y-axis rotation (instead of Z) - only for Z-up scenes!

    BYTE_X = 1
    BYTE_Y = 2
    BYTE_Z = 4
    BYTE_RotZ = 8
    mW3AngleKoefficient = -8.07
    
    motionObj = {}

    motionFrames = []
    if "positionFrames" in mBoneObj:
        positionFrames = mBoneObj["positionFrames"]
        if isinstance(positionFrames[0], list):
            # Swap back YZ if necessary
            if checkSwapYZpos:
                motionFrames.extend([[frame[0], frame[2], frame[1]] for frame in positionFrames])
            else:
                motionFrames.extend(positionFrames)
        else:
            motionFrames.append([0, 0, 0])  # Default position frame if only one frame is available
    else:
        motionFrames.append([0, 0, 0])  # Default position frame if none available

    if "rotationFrames" in mBoneObj:
        rotationFrames = mBoneObj["rotationFrames"]
        motionRotZ = [quat.eulerAngles()[2] * mW3AngleKoefficient / 360.0 for quat in rotationFrames]
        motionFrames.extend(motionRotZ)
    else:
        motionFrames.append(0)  # Default rotation frame if none available

    motionObj["frames"] = motionFrames

    # Assuming the deltaTimes are the same as before
    motionObj["deltaTimes"] = base64.b64encode([1] * len(motionFrames)).decode()

    # Assuming flags are the same as before
    motionObj["flags"] = BYTE_X | BYTE_Y | BYTE_Z | BYTE_RotZ

    return motionObj



def apply_motion_to_bone(animObj: w3_types.CSkeletalAnimation) -> w3_types.CSkeletalAnimation:
    checkSwapYZpos = False # motion: Use Y-up coordinates
    checkUseYRot = False # Use Y-axis rotation (instead of Z) - only for Z-up scenes!

    BYTE_X = 1
    BYTE_Y = 2
    BYTE_Z = 4
    BYTE_RotZ = 8
    mW3AngleKoefficient = -8.07
    
    if not animObj:
        log.debug("Empty anim object, skipping.")
        return False

    animName = animObj["name"]
    log.info("Processing anim: %s", animName)

    motionObj = animObj["motionExtraction"]
    if not motionObj:
        log.debug("    Empty motionExtraction, skipping.")
        return False

    bufferObj = animObj["animBuffer"]
    bonesArray = bufferObj["bones"]
    if not bonesArray:
        log.warning("    Corrupted bones (or anim) buffer, skipping.")

    mBoneName = "RootMotion"  # Replace with your actual bone name
    mFps = 30  # Replace with your actual frames per second value

    animFrames = bufferObj["numFrames"]
    mBoneIdx = -1
    for i in range(len(bonesArray)):
        if bonesArray[i]["BoneName"] == mBoneName:
            mBoneIdx = i
            break

    # if mBoneIdx >= 0:
    #     posFrames = bonesArray[mBoneIdx]["position_numFrames"]
    #     rotFrames = bonesArray[mBoneIdx]["rotation_numFrames"]

    # else:
    #     bonesArray.append(None)
    #     mBoneIdx = len(bonesArray) - 1

    mBoneObj = {
        "BoneName": mBoneName,
        "index": mBoneIdx,
        "position_dt": 1.0 / mFps,
        "rotation_dt": 1.0 / mFps,
        "scale_dt": 1.0 / mFps,
        "scale_numFrames": 1,
        "scaleFrames": [[1, 1, 1]]
    }
    
    deltaTotal = 0
    framePoints = [1]

    if isinstance(motionObj["deltaTimes"], str):
        deltaTimes = base64.b64decode(motionObj["deltaTimes"])
        for i in range(len(deltaTimes)):
            framePoints.append(framePoints[-1] + deltaTimes[i])
            deltaTotal += deltaTimes[i]
    elif len(motionObj["deltaTimes"]):
        deltaTimes = motionObj["deltaTimes"]
        for i in range(len(deltaTimes)):
            framePoints.append(framePoints[-1] + int(deltaTimes[i]))
            deltaTotal += deltaTimes[i]
    else:
        log.error("Unknown deltaTimes format")

    motionX, motionY, motionZ, motionRotZ = [], [], [], []
    framesObj = motionObj["frames"]
    flags = motionObj["flags"]
    anyFlag = bool(flags & 15)  # 1 | 2 | 4 | 8
    framesSets = 0

    if anyFlag:
        i = 0
        while i < len(framesObj):
            framesSets += 1
            if flags & BYTE_X:
                if i >= len(framesObj):
                    log.warning("Incomplete frames array in motionExtraction, anim: %s, setting zero RootMotion.", animObj['name'])
                    anyFlag = False
                    break
                motionX.append(float(framesObj[i]))
                i += 1
            if flags & BYTE_Y:
                if i >= len(framesObj):
                    log.warning("Incomplete frames array in motionExtraction, anim: %s, setting zero RootMotion.", animObj['name'])
                    anyFlag = False
                    break
                motionY.append(float(framesObj[i]))
                i += 1
            if flags & BYTE_Z:
                if i >= len(framesObj):
                    log.warning("Incomplete frames array in motionExtraction, anim: %s, setting zero RootMotion.", animObj['name'])
                    anyFlag = False
                    break
                motionZ.append(float(framesObj[i]))
                i += 1
            if flags & BYTE_RotZ:
                if i >= len(framesObj):
                    log.warning("Incomplete frames array in motionExtraction, anim: %s, setting zero RootMotion.", animObj['name'])
                    anyFlag = False
                    break
                motionRotZ.append(float(framesObj[i]) * 360.0 / mW3AngleKoefficient)
                i += 1
    else:
        log.debug("Flags = 0 in motionExtraction, setting null motion. %s", animName)

    if len(framePoints) != framesSets or deltaTotal > animFrames or deltaTotal < animFrames - 1:
        log.warning("Incorrect motionExtraction [framePointsSize=%d, framesSets=%d, deltaTotal=%d, animFrames=%d], setting zero RootMotion.", len(framePoints), framesSets, deltaTotal, animFrames)
        anyFlag = False

    if not anyFlag:
        mBoneObj["position_numFrames"] = 1
        mBoneObj["positionFrames"] = [0,0,0]
        mBoneObj["rotation_numFrames"] = 1
        mBoneObj["rotationFrames"] = [MQuaternion( 0, 0, 0, 1)]
    else:
        if flags & BYTE_RotZ:
            mBoneObj["rotation_numFrames"] = animFrames
            rotationFrames = []
            motionRotZ = blendMotion(motionRotZ, animFrames, framePoints)
            for frame in range(1, animFrames):
                if checkUseYRot:
                    rotationFrames.append(objQuanternion(0, motionRotZ[frame], 0))
                else:
                    rotationFrames.append(objQuanternion(0, 0, motionRotZ[frame]))
            mBoneObj["rotationFrames"] = rotationFrames
        else:
            mBoneObj["rotation_numFrames"] = 1
            mBoneObj["rotationFrames"] = [MQuaternion( 0, 0, 0, 1)]

        if flags & (BYTE_X | BYTE_Y | BYTE_Z):
            mBoneObj["position_numFrames"] = animFrames
            positionFrames = []
            motionX = blendMotion(motionX, animFrames, framePoints)
            motionY = blendMotion(motionY, animFrames, framePoints)
            motionZ = blendMotion(motionZ, animFrames, framePoints)

            for frame in range(1, animFrames):
                if checkSwapYZpos:
                    positionFrames.append([motionX[frame], motionZ[frame], motionY[frame]])
                else:
                    positionFrames.append([motionX[frame], motionY[frame], motionZ[frame]])
            mBoneObj["positionFrames"] = positionFrames
        else:
            mBoneObj["position_numFrames"] = 1
            mBoneObj["positionFrames"] = [0, 0, 0]

        log.debug("Finished processing anim: %s", animName)

    return mBoneObj
    # END
    # bonesArray[mBoneIdx] = mBoneObj
    # bufferObj["bones"] = bonesArray
    # animObj["animBuffer"] = bufferObj
    # ref = animObj

import json

# def extract_motion_from_bone(anim_obj:w3_types.w2AnimsFrames, only_print=False): # Reutrn motion extraction
#     final_motion = {
#         "duration": 1.3666666746139526,
#         "frames":[0.0, 0.0, -0.009934775531291962, -0.020615503191947937, -0.066676065325737, 0.07088396698236465, -0.09034168720245361, 0.3657502830028534, 0.01984633132815361, 1.17538583278656, 0.10672131180763245, 1.3760299682617188, 0.1473539173603058, 1.4081826210021973, 0.08582499623298645, 1.2734756469726562, 0.0864957943558693, 1.236992597579956],
#         "deltaTimes": [2, 4, 4, 8, 4, 5, 8, 6],
#         "flags": 3
#     }
def extract_motion_from_bone(mBoneObj: dict, animObj: w3_types.CSkeletalAnimation) -> w3_types.CSkeletalAnimation:
    checkSwapYZpos = False  # motion: Use Y-up coordinates
    checkUseYRot = False  # Use Y-axis rotation (instead of Z) - only for Z-up scenes!

    BYTE_X = 1
    BYTE_Y = 2
    BYTE_Z = 4
    BYTE_RotZ = 8
    mW3AngleKoefficient = -8.07

    if not animObj:
        log.debug("Empty anim object, skipping.")
        return False

    animName = animObj["name"]
    log.info("Processing anim: %s", animName)

    bufferObj = animObj["animBuffer"]
    bonesArray = bufferObj["bones"]
    if not bonesArray:
        log.warning("    Corrupted bones (or anim) buffer, skipping.")

    mFps = 30  # Replace with your actual frames per second value
    animFrames = bufferObj["numFrames"]

    motionObj = {
        "deltaTimes": [],
        "frames": [],
        "flags": 0
    }

    rotationFrames = mBoneObj.get("rotationFrames", [])
    positionFrames = mBoneObj.get("positionFrames", [])

    # if rotationFrames:
    #     motionObj["flags"] |= BYTE_RotZ
    #     for frame in rotationFrames:
    #         if checkUseYRot:
    #             motionObj["frames"].append(frame.y)
    #         else:
    #             motionObj["frames"].append(frame.z / 360.0 * mW3AngleKoefficient)

    if positionFrames:
        if any(frame[0] != 0 for frame in positionFrames):
            motionObj["flags"] |= BYTE_X
        if any(frame[1] != 0 for frame in positionFrames):
            motionObj["flags"] |= BYTE_Y
        if any(frame[2] != 0 for frame in positionFrames):
            motionObj["flags"] |= BYTE_Z

        for frame in positionFrames:
            if checkSwapYZpos:
                motionObj["frames"].extend([frame[0], frame[2], frame[1]])
            else:
                motionObj["frames"].extend([frame[0], frame[1], frame[2]])

    # Calculate deltaTimes
    framePoints = [1]
    for i in range(1, animFrames):
        delta = int(mFps * (i / animFrames))
        motionObj["deltaTimes"].append(delta)
        framePoints.append(framePoints[-1] + delta)

    if len(framePoints) != animFrames:
        log.warning("Incorrect motion extraction [framePointsSize=%d, animFrames=%d], setting zero motion.", len(framePoints), animFrames)
        return False

    animObj["motionExtraction"] = motionObj

    log.info("    [FINISH] Processed anim: %s", animName)

    return animObj


# =============================================================================
# ROOT MOTION TOGGLE SYSTEM (Driver-Based)
# =============================================================================

def setup_root_motion_drivers(armature_obj):
    """
    Add drivers to Root bone for in-place toggle.

    When in_place_factor = 0: Root stays at origin (root motion ON)
    When in_place_factor = 1: Root counters Trajectory movement (in-place mode)

    Returns True if drivers were set up successfully, False otherwise.
    """
    if not armature_obj or armature_obj.type != 'ARMATURE':
        return False

    root_bone = armature_obj.pose.bones.get("Root")
    trajectory_bone = armature_obj.pose.bones.get("Trajectory")

    if not root_bone or not trajectory_bone:
        log.warning("setup_root_motion_drivers: Missing Root or Trajectory bone")
        return False

    # Ensure armature has the in_place_factor property
    if 'in_place_factor' not in armature_obj.data:
        armature_obj.data['in_place_factor'] = 0.0

    # Remove existing drivers on Root bone
    try:
        root_bone.driver_remove('location')
    except Exception:
        pass
    try:
        root_bone.driver_remove('rotation_euler')
    except Exception:
        pass

    # Setup location drivers (X, Y, Z)
    for i, axis in enumerate(['X', 'Y', 'Z']):
        try:
            fcurve = root_bone.driver_add('location', i)
            driver = fcurve.driver
            driver.type = 'SCRIPTED'

            # Variable: trajectory location
            var_traj = driver.variables.new()
            var_traj.name = 'traj'
            var_traj.type = 'TRANSFORMS'
            var_traj.targets[0].id = armature_obj
            var_traj.targets[0].bone_target = 'Trajectory'
            var_traj.targets[0].transform_type = f'LOC_{axis}'
            var_traj.targets[0].transform_space = 'LOCAL_SPACE'

            # Variable: in_place_factor from armature data
            var_factor = driver.variables.new()
            var_factor.name = 'factor'
            var_factor.type = 'SINGLE_PROP'
            var_factor.targets[0].id_type = 'ARMATURE'
            var_factor.targets[0].id = armature_obj.data
            var_factor.targets[0].data_path = '["in_place_factor"]'

            # Expression: negate trajectory when in-place
            driver.expression = '-traj * factor'
        except Exception as e:
            log.error("Failed to add location driver for axis %s: %s", axis, e)
            return False

    # Setup rotation Z driver (yaw only)
    try:
        # Ensure root bone is in euler mode for rotation drivers
        root_bone.rotation_mode = 'XYZ'

        fcurve = root_bone.driver_add('rotation_euler', 2)  # Z axis
        driver = fcurve.driver
        driver.type = 'SCRIPTED'

        var_traj = driver.variables.new()
        var_traj.name = 'traj_rot'
        var_traj.type = 'TRANSFORMS'
        var_traj.targets[0].id = armature_obj
        var_traj.targets[0].bone_target = 'Trajectory'
        var_traj.targets[0].transform_type = 'ROT_Z'
        var_traj.targets[0].transform_space = 'LOCAL_SPACE'

        var_factor = driver.variables.new()
        var_factor.name = 'factor'
        var_factor.type = 'SINGLE_PROP'
        var_factor.targets[0].id_type = 'ARMATURE'
        var_factor.targets[0].id = armature_obj.data
        var_factor.targets[0].data_path = '["in_place_factor"]'

        driver.expression = '-traj_rot * factor'
    except Exception as e:
        log.error("Failed to add rotation driver: %s", e)
        return False

    log.info("Root motion drivers set up on %s", armature_obj.name)
    return True


def remove_root_motion_drivers(armature_obj):
    """Remove root motion drivers from Root bone."""
    if not armature_obj or armature_obj.type != 'ARMATURE':
        return False

    root_bone = armature_obj.pose.bones.get("Root")
    if not root_bone:
        return False

    try:
        root_bone.driver_remove('location')
    except Exception:
        pass
    try:
        root_bone.driver_remove('rotation_euler')
    except Exception:
        pass

    log.info("Root motion drivers removed from %s", armature_obj.name)
    return True


def has_root_motion_drivers(armature_obj):
    """Check if Root bone has root motion drivers set up."""
    if not armature_obj or armature_obj.type != 'ARMATURE':
        return False

    root_bone = armature_obj.pose.bones.get("Root")
    if not root_bone:
        return False

    # Check if bone has animation data with drivers
    if armature_obj.animation_data and armature_obj.animation_data.drivers:
        for fcurve in armature_obj.animation_data.drivers:
            if 'pose.bones["Root"]' in fcurve.data_path:
                return True

    return False


def set_root_motion_mode(armature_obj, mode):
    """
    Set the root motion mode.

    Args:
        armature_obj: The armature object
        mode: 'ROOT_MOTION' or 'IN_PLACE'

    Returns True if successful, False otherwise.
    """
    if not armature_obj or armature_obj.type != 'ARMATURE':
        return False

    factor = 0.0 if mode == 'ROOT_MOTION' else 1.0
    armature_obj.data['in_place_factor'] = factor
    armature_obj.data['root_motion_mode'] = mode

    return True


def get_root_motion_mode(armature_obj):
    """Get the current root motion mode."""
    if not armature_obj or armature_obj.type != 'ARMATURE':
        return 'ROOT_MOTION'

    return armature_obj.data.get('root_motion_mode', 'ROOT_MOTION')


# =============================================================================
# ROOT MOTION CONTROLLER EMPTY SYSTEM
# =============================================================================
# This approach creates a separate Empty object that counteracts Trajectory
# movement. The armature is parented to the empty, so toggling switches
# between root motion and in-place modes without a parent-to-child constraint.

CONTROLLER_EMPTY_NAME = "RootMotionController"


def get_controller_empty(armature_obj):
    """Get the controller empty for an armature, if it exists."""
    if not armature_obj:
        return None

    # Check if armature has a parent that's our controller
    if armature_obj.parent and armature_obj.parent.name.startswith(CONTROLLER_EMPTY_NAME):
        return armature_obj.parent

    # Check by name pattern in same collection
    controller_name = f"{CONTROLLER_EMPTY_NAME}_{armature_obj.name}"
    return bpy.data.objects.get(controller_name)


def _parent_keep_world(child_obj, parent_obj):
    """Parent an object while preserving its world transform."""
    if child_obj is None:
        return
    world_matrix = child_obj.matrix_world.copy()
    child_obj.parent = parent_obj
    if parent_obj is not None:
        child_obj.matrix_parent_inverse = parent_obj.matrix_world.inverted()
    else:
        child_obj.matrix_parent_inverse = Matrix.Identity(4)
    child_obj.matrix_world = world_matrix


def _link_object_like(obj, reference_obj):
    collections = list(getattr(reference_obj, "users_collection", []) or [])
    if collections:
        collections[0].objects.link(obj)
    else:
        bpy.context.collection.objects.link(obj)


def _object_local_matrix(obj):
    try:
        return obj.matrix_basis.copy()
    except Exception:
        try:
            return obj.matrix_local.copy()
        except Exception:
            return Matrix.Identity(4)


def _set_object_local_matrix(obj, matrix):
    try:
        obj.matrix_basis = matrix
    except Exception:
        obj.matrix_local = matrix


def _parent_direct_local(child_obj, parent_obj, local_matrix):
    if child_obj is None:
        return
    child_obj.parent = parent_obj
    child_obj.matrix_parent_inverse = Matrix.Identity(4)
    _set_object_local_matrix(child_obj, local_matrix)


def _clear_root_motion_controller_dependencies(controller):
    if controller is None:
        return
    for constraint in list(getattr(controller, "constraints", []) or []):
        if str(getattr(constraint, "name", "") or "").startswith("RootMotion_"):
            controller.constraints.remove(constraint)
    try:
        controller.driver_remove('rotation_euler', 2)
    except Exception:
        pass


def _pose_bone_local_yaw(pose_bone):
    if pose_bone is None:
        return 0.0
    try:
        if pose_bone.rotation_mode == 'QUATERNION':
            quat = pose_bone.rotation_quaternion.copy()
            if quat.dot(quat) > 1e-12:
                quat.normalize()
                return float(quat.to_euler('XYZ').z)
        if pose_bone.rotation_mode == 'AXIS_ANGLE':
            values = pose_bone.rotation_axis_angle
            axis = Vector((values[1], values[2], values[3]))
            if axis.length > 1e-8:
                return float(Quaternion(axis.normalized(), values[0]).to_euler('XYZ').z)
        return float(pose_bone.rotation_euler.z)
    except Exception:
        return 0.0


def _trajectory_counter_matrix(armature_obj):
    trajectory_bone = getattr(getattr(armature_obj, "pose", None), "bones", {}).get("Trajectory")
    if trajectory_bone is None:
        return Matrix.Identity(4)
    try:
        loc = trajectory_bone.matrix.to_translation()
    except Exception:
        loc = Vector((0.0, 0.0, 0.0))
    yaw = _pose_bone_local_yaw(trajectory_bone)
    trajectory_matrix = Matrix.Translation(loc) @ Matrix.Rotation(float(yaw), 4, 'Z')
    return trajectory_matrix.inverted_safe()


def _update_root_motion_controller(armature_obj):
    if get_controller_mode(armature_obj) != 'IN_PLACE':
        return
    controller = get_controller_empty(armature_obj)
    if controller is None:
        return
    _clear_root_motion_controller_dependencies(controller)
    rest_matrix = _matrix_from_flat(controller.get(
        "w3_root_motion_rest_local_matrix",
        _flatten_matrix(_object_local_matrix(controller)),
    ))
    counter_matrix = _trajectory_counter_matrix(armature_obj)
    _set_object_local_matrix(controller, rest_matrix @ counter_matrix)


def setup_root_motion_controller(armature_obj):
    """
    Create a controller empty that counteracts Trajectory bone movement.

    The empty is updated from the Trajectory bone by the frame handler.
    When enabled (In-Place mode), it counteracts the Pelvis movement 
    baked into the Trajectory bone, keeping the character stationary.
    When disabled (Root Motion mode), the character moves naturally.

    Returns the controller empty, or None on failure.
    """
    if not armature_obj or armature_obj.type != 'ARMATURE':
        log.warning("setup_root_motion_controller: Invalid armature object")
        return None

    # Check for Trajectory bone (required)
    trajectory_bone = armature_obj.pose.bones.get("Trajectory")

    if not trajectory_bone:
        log.warning("setup_root_motion_controller: No Trajectory bone found")
        return None

    # Check if controller already exists
    existing = get_controller_empty(armature_obj)
    if existing:
        _clear_root_motion_controller_dependencies(existing)
        if "w3_root_motion_rest_local_matrix" not in existing:
            existing["w3_root_motion_rest_local_matrix"] = _flatten_matrix(_object_local_matrix(existing))
        existing["w3_root_motion_armature"] = armature_obj.name
        log.debug("Controller empty already exists: %s", existing.name)
        return existing

    # Store armature's current world transform
    armature_world_matrix = armature_obj.matrix_world.copy()

    # Create the controller empty
    controller_name = f"{CONTROLLER_EMPTY_NAME}_{armature_obj.name}"
    controller = bpy.data.objects.new(controller_name, None)
    controller.empty_display_type = 'ARROWS'
    controller.empty_display_size = 0.5

    # Link to same collection as armature
    _link_object_like(controller, armature_obj)

    # Position controller at armature location
    controller.matrix_world = armature_world_matrix

    armature_obj.data['root_motion_controller_factor'] = 0.0
    controller.rotation_mode = 'XYZ'

    auto_controller = get_auto_motion_controller(armature_obj)
    if auto_controller is not None and armature_obj.parent == auto_controller:
        local_matrix = auto_controller.matrix_world.inverted_safe() @ armature_world_matrix
        _parent_direct_local(controller, auto_controller, local_matrix)
    else:
        _set_object_local_matrix(controller, armature_world_matrix)
    controller["w3_root_motion_rest_local_matrix"] = _flatten_matrix(_object_local_matrix(controller))
    controller["w3_root_motion_armature"] = armature_obj.name

    # Parent armature to controller (keep transform)
    _parent_keep_world(armature_obj, controller)

    # Store reference to controller on armature
    armature_obj.data['root_motion_controller'] = controller.name
    # Start in Root Motion mode (constraints off, natural movement)
    armature_obj.data['root_motion_mode'] = 'ROOT_MOTION'
    # Store original position for restoration when removed
    armature_obj.data['root_motion_original_loc'] = list(armature_obj.location)

    log.info("Created root motion controller: %s", controller.name)
    return controller


def remove_root_motion_controller(armature_obj):
    """Remove the controller empty and restore armature to original position."""
    if not armature_obj or armature_obj.type != 'ARMATURE':
        return False

    controller = get_controller_empty(armature_obj)
    if not controller:
        log.debug("No controller empty found")
        return False

    parent = controller.parent
    if parent is not None and parent.name.startswith(AUTO_MOTION_CONTROLLER_NAME):
        _parent_keep_world(armature_obj, parent)
    else:
        # Get original position if stored, otherwise use origin
        original_loc = armature_obj.data.get('root_motion_original_loc', [0.0, 0.0, 0.0])

        # Unparent armature first
        armature_obj.parent = None

        # Reset armature to original position (or origin)
        armature_obj.location = (original_loc[0], original_loc[1], original_loc[2])

    # Remove controller
    bpy.data.objects.remove(controller, do_unlink=True)

    # Clean up ALL stored references
    props_to_remove = [
        'root_motion_controller',
        'root_motion_mode',
        'root_motion_original_loc',
        'root_motion_controller_factor',
    ]
    for prop in props_to_remove:
        if prop in armature_obj.data:
            del armature_obj.data[prop]

    log.info("Removed root motion controller from %s", armature_obj.name)
    return True


def has_root_motion_controller(armature_obj):
    """Check if armature has a root motion controller set up."""
    return get_controller_empty(armature_obj) is not None


def set_controller_mode(armature_obj, mode):
    """
    Set the root motion mode via the controller empty.

    Args:
        armature_obj: The armature object
        mode: 'ROOT_MOTION' (natural movement) or
              'IN_PLACE' (counteracts movement)

    Returns True if successful.
    """
    controller = get_controller_empty(armature_obj)
    if not controller:
        return False

    _clear_root_motion_controller_dependencies(controller)
    influence = 1.0 if mode == 'IN_PLACE' else 0.0

    armature_obj.data['root_motion_mode'] = mode
    armature_obj.data['root_motion_controller_factor'] = influence
    if mode == 'IN_PLACE':
        _update_root_motion_controller(armature_obj)
        ensure_auto_motion_handler()
    else:
        rest_matrix = _matrix_from_flat(controller.get(
            "w3_root_motion_rest_local_matrix",
            _flatten_matrix(_object_local_matrix(controller)),
        ))
        _set_object_local_matrix(controller, rest_matrix)
        maybe_remove_auto_motion_handler()

    log.info("Root motion mode set to %s (influence=%s)", mode, influence)
    return True


def get_controller_mode(armature_obj):
    """Get current root motion mode from controller."""
    if not armature_obj or armature_obj.type != 'ARMATURE':
        return 'ROOT_MOTION'

    return armature_obj.data.get('root_motion_mode', 'ROOT_MOTION')


def toggle_controller_mode(armature_obj):
    """Toggle between ROOT_MOTION and IN_PLACE modes."""
    current = get_controller_mode(armature_obj)
    new_mode = 'IN_PLACE' if current == 'ROOT_MOTION' else 'ROOT_MOTION'
    return set_controller_mode(armature_obj, new_mode), new_mode


# =============================================================================
# AUTO MOTION CONTROLLER SYSTEM
# =============================================================================

AUTO_MOTION_CONTROLLER_NAME = "AutoMotionController"
MOTION_DIRECTION_CONTROLLER_NAME = "MotionDirectionController"
_AUTO_MOTION_HANDLER_ACTIVE = False
_AUTO_MOTION_UPDATING = False


def get_auto_motion_controller(armature_obj):
    """Get the outer auto-motion controller for an armature, if it exists."""
    if not armature_obj:
        return None
    try:
        controller_name = armature_obj.data.get('auto_motion_controller', "")
    except Exception:
        controller_name = ""
    if controller_name:
        controller = bpy.data.objects.get(controller_name)
        if controller:
            return controller
    controller_name = f"{AUTO_MOTION_CONTROLLER_NAME}_{armature_obj.name}"
    return bpy.data.objects.get(controller_name)


def get_motion_direction_controller(armature_obj):
    """Get the steering parent for Auto Motion, if it exists."""
    if not armature_obj:
        return None
    try:
        controller_name = armature_obj.data.get('motion_direction_controller', "")
    except Exception:
        controller_name = ""
    if controller_name:
        controller = bpy.data.objects.get(controller_name)
        if controller:
            return controller
    controller_name = f"{MOTION_DIRECTION_CONTROLLER_NAME}_{armature_obj.name}"
    return bpy.data.objects.get(controller_name)


def _is_motion_direction_controller(obj):
    return obj is not None and str(getattr(obj, "name", "") or "").startswith(MOTION_DIRECTION_CONTROLLER_NAME)


def _armature_for_stored_controller(prop_name, controller_name):
    if not controller_name:
        return None
    for obj in bpy.data.objects:
        if getattr(obj, "type", None) != 'ARMATURE':
            continue
        try:
            if obj.data.get(prop_name, "") == controller_name:
                return obj
        except Exception:
            continue
    return None


def _armature_from_controller_prop(controller):
    if controller is None:
        return None
    for prop_name in ("w3_root_motion_armature", "w3_auto_motion_armature"):
        armature_name = str(controller.get(prop_name, "") or "")
        if not armature_name:
            continue
        armature = bpy.data.objects.get(armature_name)
        if armature is not None and getattr(armature, "type", None) == 'ARMATURE':
            return armature
    return None


def _child_armature_recursive(obj):
    if obj is None:
        return None
    stack = list(getattr(obj, "children", []) or [])
    while stack:
        child = stack.pop(0)
        if getattr(child, "type", None) == 'ARMATURE':
            return child
        stack.extend(list(getattr(child, "children", []) or []))
    return None


def resolve_motion_controller_armature(obj):
    """Resolve an armature from the armature itself or one of its motion controller empties."""
    if obj is None:
        return None
    if getattr(obj, "type", None) == 'ARMATURE':
        return obj

    controller_name = str(getattr(obj, "name", "") or "")
    controller_prefixes = (CONTROLLER_EMPTY_NAME, AUTO_MOTION_CONTROLLER_NAME, MOTION_DIRECTION_CONTROLLER_NAME)
    is_controller_name = any(controller_name.startswith(f"{prefix}_") for prefix in controller_prefixes)
    armature = (
        _armature_from_controller_prop(obj)
        or _armature_for_stored_controller('root_motion_controller', controller_name)
        or _armature_for_stored_controller('auto_motion_controller', controller_name)
        or _armature_for_stored_controller('motion_direction_controller', controller_name)
    )
    if armature is not None:
        return armature

    if is_controller_name:
        armature = _child_armature_recursive(obj)
        if armature is not None:
            return armature

    for prefix in controller_prefixes:
        expected = f"{prefix}_"
        if controller_name.startswith(expected):
            candidate = bpy.data.objects.get(controller_name[len(expected):])
            if candidate is not None and getattr(candidate, "type", None) == 'ARMATURE':
                return candidate
    return None


def has_auto_motion_controller(armature_obj):
    return get_auto_motion_controller(armature_obj) is not None


def get_auto_motion_enabled(armature_obj):
    if not armature_obj or armature_obj.type != 'ARMATURE':
        return False
    return bool(armature_obj.data.get('auto_motion_enabled', False))


def _flatten_matrix(matrix):
    return [float(matrix[row][col]) for row in range(4) for col in range(4)]


def _matrix_from_flat(value):
    try:
        values = [float(v) for v in value]
        if len(values) == 16:
            return Matrix((
                values[0:4],
                values[4:8],
                values[8:12],
                values[12:16],
            ))
    except Exception:
        pass
    return Matrix.Identity(4)


def _matrix_power(matrix, count):
    result = Matrix.Identity(4)
    for _i in range(max(0, int(count))):
        result = matrix @ result
    return result


def _motion_delta_times(value):
    if value is None:
        return []
    if isinstance(value, str):
        try:
            return [int(item) for item in base64.b64decode(value)]
        except Exception:
            try:
                decoded = json.loads(value)
                return [int(item) for item in decoded]
            except Exception:
                return []
    if isinstance(value, (bytes, bytearray)):
        return [int(item) for item in value]
    try:
        return [int(item) for item in value]
    except Exception:
        return []


def _read_action_motion_data(action):
    if action is None:
        return None
    try:
        raw_data = action.get(W3_MOTION_EXTRACTION_DATA_PROP, None)
    except Exception:
        raw_data = None
    if raw_data is None:
        return None
    if isinstance(raw_data, str):
        try:
            raw_data = json.loads(raw_data)
        except Exception:
            return None
    if not isinstance(raw_data, dict):
        return None
    try:
        flags = int(raw_data.get("flags", 0) or 0)
        frames = [float(value) for value in (raw_data.get("frames") or [])]
        delta_times = _motion_delta_times(raw_data.get("deltaTimes", None))
    except Exception:
        return None
    axis_count = sum(1 for bit in (1, 2, 4, 8) if flags & bit)
    if flags == 0 or axis_count <= 0 or len(frames) < axis_count:
        return None
    sample_count = len(frames) // axis_count
    if sample_count < 2:
        return None
    return {
        "flags": flags,
        "frames": frames[:sample_count * axis_count],
        "delta_times": delta_times[:max(0, sample_count - 1)],
        "sample_count": sample_count,
    }


def _motion_axis_order(flags):
    axis_order = []
    if flags & 1:
        axis_order.append("x")
    if flags & 2:
        axis_order.append("y")
    if flags & 4:
        axis_order.append("z")
    if flags & 8:
        axis_order.append("yaw")
    return axis_order


def _motion_keyframes_from_data(motion_data):
    if not motion_data:
        return []
    flags = int(motion_data.get("flags", 0) or 0)
    axis_order = _motion_axis_order(flags)
    axis_count = len(axis_order)
    frames = list(motion_data.get("frames") or [])
    sample_count = int(motion_data.get("sample_count", 0) or 0)
    if axis_count <= 0 or sample_count <= 0:
        return []
    delta_times = list(motion_data.get("delta_times") or [])
    keyframes = []
    action_frame = 0.0
    for sample_index in range(sample_count):
        sample = frames[sample_index * axis_count:(sample_index + 1) * axis_count]
        values = {"x": 0.0, "y": 0.0, "z": 0.0, "yaw": 0.0}
        for axis, value in zip(axis_order, sample):
            values[axis] = float(value)
        keyframes.append((
            float(action_frame),
            Vector((values["x"], values["y"], values["z"])),
            float(values["yaw"]),
        ))
        if sample_index < len(delta_times):
            action_frame += float(delta_times[sample_index])
    return keyframes


def _motion_value_at_frame(keyframes, action_frame):
    if not keyframes:
        return Vector((0.0, 0.0, 0.0)), 0.0
    frame = float(action_frame)
    first_frame, first_loc, first_yaw = keyframes[0]
    if frame <= float(first_frame):
        return first_loc.copy(), float(first_yaw)
    last_frame, last_loc, last_yaw = keyframes[-1]
    if frame >= float(last_frame):
        return last_loc.copy(), float(last_yaw)
    for left, right in zip(keyframes[:-1], keyframes[1:]):
        left_frame, left_loc, left_yaw = left
        right_frame, right_loc, right_yaw = right
        if float(left_frame) <= frame <= float(right_frame):
            span = max(1e-6, float(right_frame) - float(left_frame))
            factor = max(0.0, min(1.0, (frame - float(left_frame)) / span))
            loc = left_loc.lerp(right_loc, factor)
            yaw = float(left_yaw) + ((float(right_yaw) - float(left_yaw)) * factor)
            return loc, yaw
    return last_loc.copy(), float(last_yaw)


def _motion_matrix_from_keyframes(keyframes, action_frame):
    loc, yaw = _motion_value_at_frame(keyframes, action_frame)
    return Matrix.Translation(loc) @ Matrix.Rotation(float(yaw), 4, 'Z')


def _find_pose_bone_name(armature_obj, wanted_name):
    bones = getattr(getattr(armature_obj, "pose", None), "bones", None)
    if not bones:
        return ""
    wanted_lower = str(wanted_name or "").lower()
    for pose_bone in bones:
        if str(pose_bone.name).lower() == wanted_lower:
            return pose_bone.name
    return ""


def _trajectory_curve_source(action, armature_obj):
    trajectory_name = _find_pose_bone_name(armature_obj, "Trajectory")
    if not action or not trajectory_name:
        return None
    prefix = f'pose.bones["{trajectory_name}"].'
    curves = {
        "location": {},
        "rotation_euler": {},
        "rotation_quaternion": {},
        "rotation_axis_angle": {},
    }
    for fcurve in iter_action_fcurves(action, target=armature_obj):
        data_path = str(getattr(fcurve, "data_path", "") or "")
        if not data_path.startswith(prefix):
            continue
        prop_name = data_path[len(prefix):]
        if prop_name in curves:
            curves[prop_name][int(getattr(fcurve, "array_index", 0))] = fcurve
    if not any(curves[prop] for prop in curves):
        return None
    start_frame, end_frame = [float(value) for value in action.frame_range]
    duration = max(0.0, end_frame - start_frame)
    if duration <= 0.0:
        return None
    return {
        "type": "trajectory",
        "label": "Trajectory",
        "action_start": start_frame,
        "duration": duration,
        "curves": curves,
    }


def _eval_curve(curves_by_index, index, frame, default=0.0):
    fcurve = curves_by_index.get(index)
    if fcurve is None:
        return float(default)
    try:
        return float(fcurve.evaluate(frame))
    except Exception:
        return float(default)


def _trajectory_matrix_at_frame(source, action_frame):
    curves = source.get("curves") or {}
    frame = float(source.get("action_start", 0.0)) + float(action_frame)
    loc_curves = curves.get("location") or {}
    loc = Vector((
        _eval_curve(loc_curves, 0, frame, 0.0),
        _eval_curve(loc_curves, 1, frame, 0.0),
        _eval_curve(loc_curves, 2, frame, 0.0),
    ))

    yaw = 0.0
    quat_curves = curves.get("rotation_quaternion") or {}
    if quat_curves:
        quat = Quaternion((
            _eval_curve(quat_curves, 0, frame, 1.0),
            _eval_curve(quat_curves, 1, frame, 0.0),
            _eval_curve(quat_curves, 2, frame, 0.0),
            _eval_curve(quat_curves, 3, frame, 0.0),
        ))
        if quat.dot(quat) > 1e-12:
            quat.normalize()
            yaw = quat.to_euler("XYZ").z
    else:
        euler_curves = curves.get("rotation_euler") or {}
        if euler_curves:
            yaw = _eval_curve(euler_curves, 2, frame, 0.0)
        else:
            axis_curves = curves.get("rotation_axis_angle") or {}
            if axis_curves:
                angle = _eval_curve(axis_curves, 0, frame, 0.0)
                axis = Vector((
                    _eval_curve(axis_curves, 1, frame, 0.0),
                    _eval_curve(axis_curves, 2, frame, 0.0),
                    _eval_curve(axis_curves, 3, frame, 1.0),
                ))
                if axis.length > 1e-8:
                    quat = Quaternion(axis.normalized(), angle)
                    yaw = quat.to_euler("XYZ").z

    return Matrix.Translation(loc) @ Matrix.Rotation(float(yaw), 4, 'Z')


def _auto_motion_source_for_action(action, armature_obj):
    motion_data = _read_action_motion_data(action)
    keyframes = _motion_keyframes_from_data(motion_data)
    if len(keyframes) >= 2 and keyframes[-1][0] > 0.0:
        action_start = float(action.frame_range[0]) if action is not None else 0.0
        return {
            "type": "motion_extraction",
            "label": "Motion Extraction",
            "action_start": action_start,
            "duration": float(keyframes[-1][0]),
            "keyframes": keyframes,
        }
    return _trajectory_curve_source(action, armature_obj)


def _auto_motion_source_matrix(source, action_frame):
    duration = max(0.0, float(source.get("duration", 0.0) or 0.0))
    frame = max(0.0, min(float(action_frame), duration))
    if source.get("type") == "motion_extraction":
        return _motion_matrix_from_keyframes(source.get("keyframes") or [], frame)
    if source.get("type") == "trajectory":
        return _trajectory_matrix_at_frame(source, frame)
    return Matrix.Identity(4)


def _ordered_nla_tracks(tracks, prefer_tracks=None):
    ordered = []
    seen_ids = set()
    if prefer_tracks:
        for name in prefer_tracks:
            for track in tracks:
                if getattr(track, "name", "") == name:
                    track_id = id(track)
                    if track_id not in seen_ids:
                        ordered.append(track)
                        seen_ids.add(track_id)
                    break
    for track in tracks:
        track_id = id(track)
        if track_id not in seen_ids:
            ordered.append(track)
            seen_ids.add(track_id)
    return ordered


def _filter_solo_nla_tracks(tracks):
    solo_tracks = [track for track in tracks if getattr(track, "is_solo", False)]
    return solo_tracks if solo_tracks else list(tracks)


def _nla_action_at_frame(armature_obj, frame=None, prefer_tracks=("anim_import",)):
    animation_data = getattr(armature_obj, "animation_data", None)
    if not animation_data or not getattr(animation_data, "nla_tracks", None):
        return None
    if frame is None:
        try:
            frame = float(bpy.context.scene.frame_current)
        except Exception:
            frame = None
    if frame is None:
        return None
    tracks = _ordered_nla_tracks(_filter_solo_nla_tracks(list(animation_data.nla_tracks)), prefer_tracks=prefer_tracks)
    for track in tracks:
        if getattr(track, "mute", False):
            continue
        for strip in reversed(track.strips):
            if getattr(strip, "mute", False) or getattr(strip, "action", None) is None:
                continue
            if float(strip.frame_start) <= frame <= float(strip.frame_end):
                return strip.action
    return None


def _nla_last_action(armature_obj, prefer_tracks=("anim_import",)):
    animation_data = getattr(armature_obj, "animation_data", None)
    if not animation_data or not getattr(animation_data, "nla_tracks", None):
        return None
    tracks = _ordered_nla_tracks(_filter_solo_nla_tracks(list(animation_data.nla_tracks)), prefer_tracks=prefer_tracks)
    for track in tracks:
        if getattr(track, "mute", False):
            continue
        for strip in reversed(track.strips):
            if not getattr(strip, "mute", False) and getattr(strip, "action", None) is not None:
                return strip.action
    return None


def _action_has_auto_motion_source(action, armature_obj):
    return _auto_motion_source_for_action(action, armature_obj) is not None


def _active_armature_action(armature_obj):
    animation_data = getattr(armature_obj, "animation_data", None)
    if not animation_data:
        return None

    nla_action = _nla_action_at_frame(armature_obj)
    if _action_has_auto_motion_source(nla_action, armature_obj):
        return nla_action

    action = getattr(animation_data, "action", None)
    if _action_has_auto_motion_source(action, armature_obj):
        return action

    nla_last = _nla_last_action(armature_obj)
    if _action_has_auto_motion_source(nla_last, armature_obj):
        return nla_last

    return nla_action or action or nla_last


def get_auto_motion_source_label(armature_obj):
    action = _active_armature_action(armature_obj)
    source = _auto_motion_source_for_action(action, armature_obj)
    return source.get("label", "") if source else ""


def _auto_motion_base_local_matrix(controller):
    return _matrix_from_flat(controller.get(
        "w3_auto_motion_base_local_matrix",
        _flatten_matrix(_object_local_matrix(controller)),
    ))


def _auto_motion_rest_local_matrix(controller):
    return _matrix_from_flat(controller.get(
        "w3_auto_motion_rest_local_matrix",
        _flatten_matrix(_object_local_matrix(controller)),
    ))


def _store_auto_motion_base_local(controller, matrix):
    controller["w3_auto_motion_base_local_matrix"] = _flatten_matrix(matrix)


def _store_auto_motion_rest_local(controller, matrix):
    controller["w3_auto_motion_rest_local_matrix"] = _flatten_matrix(matrix)


def _ensure_motion_direction_controller(armature_obj, auto_controller, reference_obj=None):
    """Ensure Auto Motion has a steerable parent empty."""
    if auto_controller is None:
        return None

    reference_obj = reference_obj or auto_controller
    direction = get_motion_direction_controller(armature_obj)
    if direction is None and _is_motion_direction_controller(auto_controller.parent):
        direction = auto_controller.parent

    old_parent = auto_controller.parent
    auto_world = auto_controller.matrix_world.copy()
    if direction is None:
        direction_name = f"{MOTION_DIRECTION_CONTROLLER_NAME}_{armature_obj.name}"
        direction = bpy.data.objects.new(direction_name, None)
        direction.empty_display_type = 'ARROWS'
        direction.empty_display_size = 1.2
        direction.matrix_world = auto_world
        direction["w3_auto_motion_armature"] = armature_obj.name
        _link_object_like(direction, reference_obj)
        if old_parent is not None:
            _parent_keep_world(direction, old_parent)

    if auto_controller.parent != direction:
        local_matrix = direction.matrix_world.inverted_safe() @ auto_world
        _parent_direct_local(auto_controller, direction, local_matrix)

    armature_obj.data['motion_direction_controller'] = direction.name
    direction["w3_auto_motion_armature"] = armature_obj.name
    return direction


def _ensure_auto_motion_controller_state(armature_obj, controller, reference_obj=None):
    if controller is None:
        return
    _ensure_motion_direction_controller(armature_obj, controller, reference_obj=reference_obj)
    local_matrix = _object_local_matrix(controller)
    if "w3_auto_motion_rest_local_matrix" not in controller:
        _store_auto_motion_rest_local(controller, local_matrix)
    if "w3_auto_motion_base_local_matrix" not in controller:
        _store_auto_motion_base_local(controller, local_matrix)


def _is_scene_playing():
    try:
        screen = bpy.context.screen
        return bool(getattr(screen, "is_animation_playing", False))
    except Exception:
        return False


def _auto_motion_controller_child(armature_obj):
    root_controller = get_controller_empty(armature_obj)
    if root_controller is not None:
        _clear_root_motion_controller_dependencies(root_controller)
        return root_controller
    return armature_obj


def setup_auto_motion_controller(armature_obj):
    """Create the outer controller that accumulates extracted actor motion."""
    if not armature_obj or armature_obj.type != 'ARMATURE':
        log.warning("setup_auto_motion_controller: Invalid armature object")
        return None
    existing = get_auto_motion_controller(armature_obj)
    if existing:
        _ensure_auto_motion_controller_state(armature_obj, existing, reference_obj=existing)
        return existing
    action = _active_armature_action(armature_obj)
    if _auto_motion_source_for_action(action, armature_obj) is None:
        log.warning("setup_auto_motion_controller: No motion extraction or Trajectory curves found")
        return None

    child = _auto_motion_controller_child(armature_obj)
    child_world_matrix = child.matrix_world.copy()
    direction_name = f"{MOTION_DIRECTION_CONTROLLER_NAME}_{armature_obj.name}"
    direction = bpy.data.objects.new(direction_name, None)
    direction.empty_display_type = 'ARROWS'
    direction.empty_display_size = 1.2
    direction.matrix_world = child_world_matrix
    direction["w3_auto_motion_armature"] = armature_obj.name
    _link_object_like(direction, child)

    controller_name = f"{AUTO_MOTION_CONTROLLER_NAME}_{armature_obj.name}"
    controller = bpy.data.objects.new(controller_name, None)
    controller.empty_display_type = 'SINGLE_ARROW'
    controller.empty_display_size = 0.8
    _link_object_like(controller, child)
    _parent_direct_local(controller, direction, Matrix.Identity(4))

    _parent_keep_world(child, controller)

    rest_matrix = Matrix.Identity(4)
    _store_auto_motion_rest_local(controller, rest_matrix)
    _store_auto_motion_base_local(controller, rest_matrix)
    controller["w3_auto_motion_armature"] = armature_obj.name
    armature_obj.data['motion_direction_controller'] = direction.name
    armature_obj.data['auto_motion_controller'] = controller.name
    armature_obj.data['auto_motion_enabled'] = False
    armature_obj.data['auto_motion_loop_count'] = 0
    armature_obj.data['auto_motion_last_frame'] = float(getattr(bpy.context.scene, "frame_current", 0.0))
    armature_obj.data['auto_motion_last_action'] = getattr(action, "name", "")
    return controller


def remove_auto_motion_controller(armature_obj):
    """Remove the auto-motion controller while preserving child world transforms."""
    if not armature_obj or armature_obj.type != 'ARMATURE':
        return False
    controller = get_auto_motion_controller(armature_obj)
    if not controller:
        return False
    direction = get_motion_direction_controller(armature_obj)
    if direction is None and _is_motion_direction_controller(controller.parent):
        direction = controller.parent
    parent = direction.parent if direction is not None and controller.parent == direction else controller.parent
    children = list(controller.children)
    for child in children:
        _parent_keep_world(child, parent)
    bpy.data.objects.remove(controller, do_unlink=True)
    if direction is not None and _is_motion_direction_controller(direction) and not list(direction.children):
        bpy.data.objects.remove(direction, do_unlink=True)
    for prop in (
        'auto_motion_controller',
        'motion_direction_controller',
        'auto_motion_enabled',
        'auto_motion_loop_count',
        'auto_motion_last_frame',
        'auto_motion_last_action',
        'auto_motion_source',
    ):
        if prop in armature_obj.data:
            del armature_obj.data[prop]
    maybe_remove_auto_motion_handler()
    return True


def set_auto_motion_enabled(armature_obj, enabled):
    controller = get_auto_motion_controller(armature_obj)
    if controller is None:
        controller = setup_auto_motion_controller(armature_obj)
    if controller is None:
        return False
    _ensure_auto_motion_controller_state(armature_obj, controller, reference_obj=armature_obj)
    if not enabled:
        armature_obj.data['auto_motion_loop_count'] = 0
        armature_obj.data['auto_motion_enabled'] = False
        maybe_remove_auto_motion_handler()
        return True

    action = _active_armature_action(armature_obj)
    source = _auto_motion_source_for_action(action, armature_obj)
    if source is None:
        return False

    scene = bpy.context.scene
    current_frame = float(getattr(scene, "frame_current", 0.0) or 0.0)
    try:
        current_frame += float(getattr(scene, "frame_subframe", 0.0) or 0.0)
    except Exception:
        pass
    action_start = float(source.get("action_start", 0.0) or 0.0)
    duration = max(0.0, float(source.get("duration", 0.0) or 0.0))
    relative_frame = max(0.0, min(current_frame - action_start, duration))
    current_motion = _auto_motion_source_matrix(source, relative_frame)
    base_matrix = _object_local_matrix(controller) @ current_motion.inverted_safe()
    _store_auto_motion_base_local(controller, base_matrix)
    armature_obj.data['auto_motion_loop_count'] = 0
    armature_obj.data['auto_motion_last_frame'] = current_frame
    armature_obj.data['auto_motion_last_action'] = getattr(action, "name", "")
    armature_obj.data['auto_motion_source'] = source.get("label", "")
    armature_obj.data['auto_motion_enabled'] = True
    ensure_auto_motion_handler()
    return True


def toggle_auto_motion_enabled(armature_obj):
    new_state = not get_auto_motion_enabled(armature_obj)
    return set_auto_motion_enabled(armature_obj, new_state), new_state


def reset_auto_motion_controller(armature_obj):
    controller = get_auto_motion_controller(armature_obj)
    if controller is None:
        return False
    _ensure_auto_motion_controller_state(armature_obj, controller, reference_obj=armature_obj)
    rest_matrix = _auto_motion_rest_local_matrix(controller)
    _store_auto_motion_base_local(controller, rest_matrix)
    _set_object_local_matrix(controller, rest_matrix)
    armature_obj.data['auto_motion_loop_count'] = 0
    scene = getattr(bpy.context, "scene", None)
    armature_obj.data['auto_motion_last_frame'] = float(getattr(scene, "frame_current", 0.0) or 0.0)
    if scene is not None and get_auto_motion_enabled(armature_obj):
        _update_auto_motion_for_armature(scene, armature_obj)
    return True


def _update_auto_motion_for_armature(scene, armature_obj):
    if not get_auto_motion_enabled(armature_obj):
        return
    controller = get_auto_motion_controller(armature_obj)
    if controller is None:
        armature_obj.data['auto_motion_enabled'] = False
        return
    _ensure_auto_motion_controller_state(armature_obj, controller, reference_obj=armature_obj)
    action = _active_armature_action(armature_obj)
    source = _auto_motion_source_for_action(action, armature_obj)
    if source is None:
        return

    current_frame = float(getattr(scene, "frame_current", 0.0) or 0.0)
    try:
        current_frame += float(getattr(scene, "frame_subframe", 0.0) or 0.0)
    except Exception:
        pass
    last_frame = float(armature_obj.data.get('auto_motion_last_frame', current_frame))
    last_action = str(armature_obj.data.get('auto_motion_last_action', ""))
    action_name = getattr(action, "name", "")
    loop_count = int(armature_obj.data.get('auto_motion_loop_count', 0) or 0)

    action_start = float(source.get("action_start", 0.0) or 0.0)
    duration = max(0.0, float(source.get("duration", 0.0) or 0.0))
    action_end = action_start + duration
    scene_start = float(getattr(scene, "frame_start", action_start) or action_start)
    loop_start = max(action_start, scene_start)
    playing = _is_scene_playing()

    relative_frame = max(0.0, min(current_frame - action_start, duration))

    if action_name != last_action:
        loop_count = 0
        current_motion = _auto_motion_source_matrix(source, relative_frame)
        _store_auto_motion_base_local(
            controller,
            _object_local_matrix(controller) @ current_motion.inverted_safe(),
        )
    elif current_frame < last_frame - 0.1:
        if playing and last_frame >= action_end - 0.5 and current_frame <= loop_start + 0.5:
            loop_count += 1
        else:
            loop_count = 0

    current_motion = _auto_motion_source_matrix(source, relative_frame)
    cycle_motion = _auto_motion_source_matrix(source, duration)
    base_matrix = _auto_motion_base_local_matrix(controller)
    _set_object_local_matrix(controller, base_matrix @ (_matrix_power(cycle_motion, loop_count) @ current_motion))

    armature_obj.data['auto_motion_loop_count'] = int(loop_count)
    armature_obj.data['auto_motion_last_frame'] = float(current_frame)
    armature_obj.data['auto_motion_last_action'] = action_name
    armature_obj.data['auto_motion_source'] = source.get("label", "")


@persistent
def _auto_motion_frame_change_handler(scene, depsgraph=None):
    global _AUTO_MOTION_UPDATING
    if _AUTO_MOTION_UPDATING:
        return
    _AUTO_MOTION_UPDATING = True
    try:
        for obj in list(bpy.data.objects):
            if getattr(obj, "type", None) != 'ARMATURE':
                continue
            if get_controller_mode(obj) == 'IN_PLACE':
                _update_root_motion_controller(obj)
            if get_auto_motion_enabled(obj):
                _update_auto_motion_for_armature(scene, obj)
    except Exception:
        log.debug("Auto motion frame update failed", exc_info=True)
    finally:
        _AUTO_MOTION_UPDATING = False


def ensure_auto_motion_handler():
    global _AUTO_MOTION_HANDLER_ACTIVE
    if _auto_motion_frame_change_handler not in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.append(_auto_motion_frame_change_handler)
    _AUTO_MOTION_HANDLER_ACTIVE = True


def maybe_remove_auto_motion_handler():
    global _AUTO_MOTION_HANDLER_ACTIVE
    for obj in bpy.data.objects:
        if getattr(obj, "type", None) != 'ARMATURE':
            continue
        if get_auto_motion_enabled(obj) or get_controller_mode(obj) == 'IN_PLACE':
            return
    if _auto_motion_frame_change_handler in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.remove(_auto_motion_frame_change_handler)
    _AUTO_MOTION_HANDLER_ACTIVE = False


def remove_auto_motion_handler():
    global _AUTO_MOTION_HANDLER_ACTIVE
    if _auto_motion_frame_change_handler in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.remove(_auto_motion_frame_change_handler)
    _AUTO_MOTION_HANDLER_ACTIVE = False
