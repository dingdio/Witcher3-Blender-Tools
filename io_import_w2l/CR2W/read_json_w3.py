
import json

from . import w3_types
from . import om
#import maya.api.OpenMaya as om
from math import degrees
from math import radians
#from mathutils import Vector, Quaternion, Euler

# def readxyz(file):
#     x = file["X"]  # X pos
#     y = file["Y"]  # Y pos
#     z = file["Z"]  # Z pos
#     coords = [x, y, z]
#     return coords

def readxyz(file):
    if "X" in file:
        x = file["X"]  # X pos
        y = file["Y"]  # Y pos
        z = file["Z"]  # Z pos
    elif "x" in file:
        x = file["x"]  # X pos
        y = file["y"]  # Y pos
        z = file["z"]  # Z pos
    else:
        x = file[0]
        y = file[1]
        z = file[2]
    coords = [x, y, z]
    return coords

def readEulerXYZ(file):
    x = degrees((file.x))  # X pos
    y = degrees((file.y))  # y pos
    z = degrees((file.z))  # z pos
    coords = [x, y, z]
    return coords

    
def deg_to_rad_XYZ(file):
    x = radians(file[0])  # X pos
    y = radians(file[1])  # y pos
    z = radians(file[2])  # z pos
    coords = [x, y, z]
    return coords


def readXYZW(file):
    if "X" in file:
        x = file["X"]
        y = file["Y"]
        z = file["Z"]
        w = file["W"]
    elif "x" in file:
        x = file["x"]
        y = file["y"]
        z = file["z"]
        w = file["w"]
    elif len(file) == 4:
        x = file[0]
        y = file[1]
        z = file[2]
        w = file[3]
    else:
        # xyz value in quat
        x = file[0]
        y = file[1]
        z = file[2]
        w = 1
    coords = [x, y, z, w]
    return coords

def readBones(file):
    bones = []
    # Bone Count
    boneCount = len(file["names"])
    for boneId in range(boneCount):
        boneName = file["names"][boneId]
        parentId = file["parentIdx"][boneId]
        coords = readxyz(file["positions"][boneId])
        scale = readxyz(file["scales"][boneId])
        quat_read = readXYZW(file["rotations"][boneId])
        quat = om.MQuaternion( quat_read[0],quat_read[1],quat_read[2],-quat_read[3])#-quat_read[3] )
        quat.normalizeIt()
        #euler = quat.asEulerRotation()
        #e = euler.reorderIt(5)

        ro = False #readEulerXYZ(e)

        w3Bone = w3_types.W3Bone(boneId, boneName, coords, parentId, ro, quat, scale)
        bones.append(w3Bone)
    return bones



# def readBonesBin(file):
#     bones = []
#     # Bone Count
#     boneCount = len(file.names)
#     for boneId in range(boneCount):
#         boneName = file.names[boneId]
#         parentId = file.parentIdx[boneId]
#         coords = readxyz(file.positions[boneId])
#         scale = readxyz(file.scales[boneId])
#         quat_read = readXYZW(file.rotations[boneId])
#         quat = om.MQuaternion( quat_read[0],quat_read[1],quat_read[2],-quat_read[3] )
#         quat.normalizeIt()
#         #euler = quat.asEulerRotation()
#         #e = euler.reorderIt(5)

#         ro = False #readEulerXYZ(e)

#         w3Bone = w3_types.W3Bone(boneId, boneName, coords, parentId, ro, quat, scale)
#         bones.append(w3Bone)
#     return bones


def eularToQuat(file):
    rot_rad = deg_to_rad_XYZ(file)
    euler = om.MEulerRotation( rot_rad[0],rot_rad[1],rot_rad[2], 5 )
    quat = euler.asQuaternion()
    rot = [quat.x,quat.y,quat.z,quat.w]
    return rot

def readCSkeleton(filename):
    with open(filename) as file:
        data = file.read()
    ioStream = json.loads(data)
    print('Reading Bones')
    bones = readBones(ioStream)
    hasBones = bool(bones)
    w3ModelData = w3_types.CSkeleton(bones=bones)
    return w3ModelData
    
def readCSkeletonData(data):
    bones = readBones(data)
    hasBones = bool(bones)
    w3ModelData = w3_types.CSkeleton(bones=bones)
    return w3ModelData

def readTracks(file):
    tracks = []
    # track Count
    trackCount = len(file)
    for trackId in range(trackCount):
        trackName = file[trackId]['trackName']
        trackFrames= []
        trackFramesArr = file[trackId]['trackFrames']
        for frameId in range(len(trackFramesArr)):
            trackFrames.append(trackFramesArr[frameId])
        frames = w3_types.Track(trackId,
                        trackName = trackName,
                        numFrames = file[trackId]['numFrames'],
                        dt = file[trackId]['dt'],
                        trackFrames= trackFrames)
        tracks.append(frames)
    return tracks

def readAnimation(file):
    bones = []
    # Bone Count
    boneCount = len(file)
    for boneId in range(boneCount):
        boneName = file[boneId]['BoneName']
        positionFrames=[]
        rotationFrames=[]
        rotationFramesQuat=[]
        scaleFrames=[]

        posFramesArr = file[boneId]['positionFrames']
        for frameId in range(len(posFramesArr)):
            positionFrames.append(readxyz(posFramesArr[frameId]))

        scaleFramesArr = file[boneId]['scaleFrames']
        for frameId in range(len(scaleFramesArr)):
            scaleFrames.append(readxyz(scaleFramesArr[frameId]))

        rotFramesArr = file[boneId]['rotationFrames']
        for frameId in range(len(rotFramesArr)):
            quat_read = readXYZW(rotFramesArr[frameId])
            #! FLIP W HERE???? (last quat xyzw)
            quat = om.MQuaternion(quat_read[0],quat_read[1],quat_read[2], quat_read[3] )
            #euler = Quaternion((quat.W, quat.X, quat.Y, quat.Z)).to_euler('ZYX') #quat.asEulerRotation()
            #e = euler.reorderIt(5)
            #ro = readEulerXYZ(euler) #readEulerXYZ(e)
            #rotationFrames.append(ro)
            quat.normalizeIt()
            rotationFramesQuat.append(quat)

        frames = w3_types.w2AnimsFrames(boneId,
                        BoneName = file[boneId]['BoneName'],
                        position_dt = file[boneId]['position_dt'],
                        position_numFrames = file[boneId]['position_numFrames'],
                        positionFrames = positionFrames,
                        rotation_dt = file[boneId]['rotation_dt'],
                        rotation_numFrames = file[boneId]['rotation_numFrames'],
                        rotationFrames = rotationFrames,
                        scale_dt = file[boneId]['scale_dt'],
                        scale_numFrames = file[boneId]['scale_numFrames'],
                        scaleFrames = scaleFrames,
                        rotationFramesQuat = rotationFramesQuat)
        bones.append(frames)
    return bones

def readSkeletalAnimationSetEntry(json):
    return w3_types.CSkeletalAnimationSetEntry.from_json(json)

def readSkeletalAnimation(json):
    print('Reading Animation')
    return w3_types.CSkeletalAnimation.from_json(json)

def readAnimBuffer(animBuffer):
    #if 'parts' in animBuffer:
    if hasattr(animBuffer, 'parts'):
        result = readMultiPartAnimBuffer(animBuffer)
    else:
        result = readSingleAnimBuffer(animBuffer)
    return result

def readMultiPartAnimBuffer(animBuffer):
    numFrames = animBuffer['numFrames']
    numBones = animBuffer['numBones']
    numTracks = animBuffer['numTracks']
    firstFrames = animBuffer['firstFrames']
    parts = readParts(animBuffer['parts'])
    result = w3_types.CAnimationBufferMultipart(numFrames = numFrames,
                                                numBones = numBones,
                                                numTracks = numTracks,
                                                firstFrames = firstFrames,
                                                parts = parts)
    return result

def readParts(parts):
    result = []
    for part in parts:
        mybuffer = readSingleAnimBuffer(part)
        result.append(mybuffer)
    return result

def readSingleAnimBuffer(animBuffer):

    bones = readAnimation(animBuffer['bones'])
    hasBones = bool(bones)
    if 'tracks' in animBuffer and animBuffer['tracks']:
        tracks = readTracks(animBuffer['tracks'])
    else:
        tracks = []
    duration = animBuffer['duration']
    numFrames = animBuffer['numFrames']
    dt = animBuffer['dt']
    result = w3_types.CAnimationBufferBitwiseCompressed(bones=bones,
                                            tracks=tracks,
                                            duration=duration,
                                            numFrames=numFrames,
                                            dt=dt)
    return result


def readFaceFile(filename):
    with open(filename) as file:
        data = file.read()
        loaded = json.loads(data)
        return readFaceFileData(loaded)

def readFaceFileData(loaded):
    print('Reading Face')
    name = "N/A"
    
    mimicSkeleton = w3_types.CSkeleton(bones=readBones(loaded['mimicSkeleton']))
    floatTrackSkeleton = w3_types.CSkeleton(bones=readBones(loaded['floatTrackSkeleton']))
    mimicPoses = []
    for pose in loaded['mimicPoses']:
        posename = pose['name']
        newPose = w3_types.CSkeletalAnimation(name = posename,
                                        animBuffer=readAnimBuffer(pose))
        # for bone in newPose.animBuffer.bones:
        #     for frame in bone.rotationFramesQuat:
        #         frame = frame.invertIt()
        mimicPoses.append(newPose)
    CMimicFace = w3_types.CMimicFace(name = name,
                                     mimicSkeleton = mimicSkeleton,
                                     floatTrackSkeleton = floatTrackSkeleton,
                                     mimicPoses=mimicPoses)
    return CMimicFace


def readEntFile(filename):
    with open(filename) as file:
        data = file.read()
        ioStream = json.loads(data)
    print('Reading Entity')
    ioStream['name'] = ioStream['name'].replace(" ", "_")
    entity = w3_types.Entity.from_json(ioStream)
                # name = ,
                # animation_rig = ioStream['animation_rig'],
                # includedTemplates  = ioStream['includedTemplates'],
                # staticMeshes  = ioStream.get("staticMeshes", {}))
    return entity

def Read_CSkeletalAnimationSet(json):
    print('Reading Skeletal AnimationSet')
    if 'animations' in json:
        data = w3_types.CSkeletalAnimationSet.from_json(json)
    else:
        data =  w3_types.CSkeletalAnimationSet()
        data.animations.append(w3_types.CSkeletalAnimationSetEntry.from_json(json))
        
    return data

def Read_CCutsceneTemplate(json):
    print('Reading Cutscene')
    data = w3_types.CCutsceneTemplate.from_json(json)
    return data