
import logging
from mathutils import Vector, Quaternion, Euler, Matrix
import base64
import math
from ..CR2W import w3_types
from ..CR2W.om import MQuaternion
import bpy

log = logging.getLogger(__name__)

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
        for fcurve in action.fcurves:
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
    obj.animation_data_create()
    obj.animation_data.action = action

    # Flags interpretation
    x_movement = (motion_extraction.flags & 1) > 0
    z_movement = (motion_extraction.flags & 2) > 0
    y_movement = (motion_extraction.flags & 4) > 0
    yaw_rotation = (motion_extraction.flags & 8) > 0

    # Create FCurves based on flags
    fcurves = []
    if x_movement:
        fcurves.append(action.fcurves.new(data_path="location", index=0))
    if z_movement:
        fcurves.append(action.fcurves.new(data_path="location", index=1))
    if y_movement:
        fcurves.append(action.fcurves.new(data_path="location", index=2))
    if yaw_rotation:
        fcurves.append(action.fcurves.new(data_path="rotation_euler", index=2))

    # Blender 4.4+ slotted actions: assign the slot after FCurves are created
    # See: https://developer.blender.org/docs/release_notes/4.4/upgrading/slotted_actions/
    if bpy.app.version >= (4, 4, 0) and hasattr(action, 'slots') and len(action.slots) > 0:
        obj.animation_data.action_slot = action.slots[0]

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
# This approach creates a separate Empty object that follows Trajectory/Reference
# bones via constraints. The armature is parented to the empty, so toggling
# the constraint influence switches between root motion and in-place modes.

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


def setup_root_motion_controller(armature_obj):
    """
    Create a controller empty that counteracts Trajectory bone movement.

    The empty uses a COPY_LOCATION constraint with INVERT on all axes.
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
    for collection in armature_obj.users_collection:
        collection.objects.link(controller)
        break

    # Position controller at armature location
    controller.matrix_world = armature_world_matrix

    # Add COPY_LOCATION constraint with INVERT on all axes
    # This counteracts the Trajectory/Pelvis movement when enabled
    loc_constraint = controller.constraints.new('COPY_LOCATION')
    loc_constraint.name = "RootMotion_InPlace"
    loc_constraint.target = armature_obj
    loc_constraint.subtarget = "Trajectory"
    loc_constraint.use_x = True
    loc_constraint.use_y = True
    loc_constraint.use_z = True
    # INVERT on all axes to counteract Trajectory movement
    loc_constraint.invert_x = True
    loc_constraint.invert_y = True
    loc_constraint.invert_z = True
    loc_constraint.target_space = 'POSE'
    loc_constraint.owner_space = 'WORLD'
    # Start with constraint OFF = Root Motion mode (natural movement)
    loc_constraint.influence = 0.0

    # Parent armature to controller (keep transform)
    armature_obj.parent = controller
    armature_obj.matrix_parent_inverse = controller.matrix_world.inverted()

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

    # Get original position if stored, otherwise use origin
    original_loc = armature_obj.data.get('root_motion_original_loc', [0.0, 0.0, 0.0])

    # Unparent armature first
    armature_obj.parent = None

    # Reset armature to original position (or origin)
    armature_obj.location = (original_loc[0], original_loc[1], original_loc[2])

    # Remove controller
    bpy.data.objects.remove(controller, do_unlink=True)

    # Clean up ALL stored references
    props_to_remove = ['root_motion_controller', 'root_motion_mode', 'root_motion_original_loc']
    for prop in props_to_remove:
        if prop in armature_obj.data:
            del armature_obj.data[prop]

    log.info("Removed root motion controller from %s, reset to position %s", armature_obj.name, original_loc)
    return True


def has_root_motion_controller(armature_obj):
    """Check if armature has a root motion controller set up."""
    return get_controller_empty(armature_obj) is not None


def set_controller_mode(armature_obj, mode):
    """
    Set the root motion mode via controller constraints.

    Args:
        armature_obj: The armature object
        mode: 'ROOT_MOTION' (constraints OFF, natural movement) or 
              'IN_PLACE' (constraints ON with invert, counteracts movement)

    Returns True if successful.
    """
    controller = get_controller_empty(armature_obj)
    if not controller:
        return False

    # In-Place mode: constraints ON (inverted) to counteract Trajectory movement
    # Root Motion mode: constraints OFF, character moves naturally
    influence = 1.0 if mode == 'IN_PLACE' else 0.0

    for constraint in controller.constraints:
        if constraint.name.startswith("RootMotion_"):
            constraint.influence = influence

    armature_obj.data['root_motion_mode'] = mode

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
