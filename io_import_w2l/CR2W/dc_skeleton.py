import json

from .bin_helpers import getString, readString
from .bStream import *
from .w3_types import Quaternion, Vector3D
from .read_json_w3 import readCSkeletonData
from . import w3_types
from .CR2W_types import getCR2W, CQuaternion

class MimicPose(w3_types.base_w3):
    """docstring for MimicPose."""
    def __init__(self):
        #super(MimicPose, self).__init__()
        self.name = ""
        self.bones = []
        self.duration = 0.0
        self.numFrames = 0
        self.dt = 0.0
    def __iter__(self):
        return iter(['name','bones','duration','numFrames','dt'])

class Bone(w3_types.base_w3):
    """docstring for Bone."""
    def __init__(self):
        #super(Bone, self).__init__()
        self.index = 0
        self.BoneName : str = "????"
        self.position_dt = 0
        self.position_numFrames = 0
        self.positionFrames =[] #new List<Vector>()
        self.rotation_dt = 0
        self.rotation_numFrames = 0
        self.rotationFrames =[] #new List<Quaternion>()
        self.scale_dt = 0
        self.scale_numFrames = 0
        self.scaleFrames =[] #new List<Vector>()

def read_skelly(skelly):
    this_skeleton = w3_types.w2rig(
                nbBones=94,
                names= [],
                tracks= [],
                parentIdx = [],
                positions = [],
                rotations = [],
                scales = [])
    for item in skelly.PROPS:
        if item.theName == "bones":
            for bone in item.More:
                this_skeleton.names.append(bone.elementName)
        if item.theName == "tracks":
            for track in item.More:
                this_skeleton.tracks.append(track.elementName)
        if item.theName == "parentIndices":
            this_skeleton.parentIdx = item.value[:]
    if hasattr(skelly, "rigData"):
        for data in skelly.rigData.rigData:
            this_skeleton.positions.append(data.position)
            this_skeleton.rotations.append(data.rotation)
            this_skeleton.scales.append(data.scale)
    return this_skeleton

def create_CMimicFace(file):
    mimicPoses = []

    mimic = file.CHUNKS.CHUNKS[0]

    chunkMimicFace = file.CHUNKS.CHUNKS[0]
    mimicSkeleton = read_skelly(file.CHUNKS.CHUNKS[1])
    floatTrackSkeleton = read_skelly(file.CHUNKS.CHUNKS[2])

    #!INVERT W CHECK IF CAN BE CANGED SOMEWHERE ELSE??
    for i, rot in enumerate(mimicSkeleton.rotations):
        mimicSkeleton.rotations[i].w = -rot.w
    for i, rot in enumerate(floatTrackSkeleton.rotations):
        floatTrackSkeleton.rotations[i].w = -rot.w



    # # convert mapping into bone array
    mimicMapping = chunkMimicFace.GetVariableByName("mapping").value
    # # ie. mimicMapping[0] = "uv_center_slide2"

    # # give each mapping a name
    tracks = floatTrackSkeleton.tracks


    mimicPoses = chunkMimicFace.GetVariableByName("mimicPoses").value[:]
    # # save poses into
    final_poses = []
    for idx, mimicbones in enumerate(mimicPoses):
        pose = MimicPose()
        pose.name = tracks[idx]
        pose.numFrames = 1
        for jdx, bone in enumerate(mimicbones):
            myBone = Bone()
            map = mimicMapping[jdx]
            myBone.BoneName = mimicSkeleton.names[map]
            myBone.positionFrames.append(Vector3D(bone.x, bone.y, bone.z))
            myBone.rotationFrames.append(Quaternion(bone.pitch, bone.yaw, bone.roll, bone.w))
            myBone.scaleFrames.append(Vector3D(bone.scale_x, bone.scale_y, bone.scale_z))
            pose.bones.append(myBone)
        final_poses.append(pose)
    CMimicFace = w3_types.CMimicFace(name = "name",
                                     mimicSkeleton = mimicSkeleton,
                                     floatTrackSkeleton = floatTrackSkeleton,
                                     mimicPoses=final_poses)
    return CMimicFace

import os
import array

from .CR2W_types import PROPERTY, getCR2W, W_CLASS
def create_Skeleton_w2(f, rigFile):
    this_skeleton = w3_types.w2rig(
            nbBones=94,
            names= [],
            tracks= [],
            parentIdx = [],
            positions = [],
            rotations = [],
            scales = [])
    
    
    for chunk in rigFile.CHUNKS.CHUNKS:
        if chunk.name == "CSkeleton":
            f.seek(0)
            br:bStream = bStream(data = f.read())
            f.close()
            br.seek(chunk.PROPS[-1].dataEnd)

            br.seek(3, os.SEEK_CUR)
            chunkSize = br.readUInt32() # = readU32(file)
            br.seek(36, os.SEEK_CUR) # unk
            br.seek(28, os.SEEK_CUR) # app info : version (4 bytes) + 24 bytes string
            br.seek(116, os.SEEK_CUR) # "__classname__", "__type__"...

            unk = br.readUInt32() # = readU32(file)
            endOfBonesNamesAdress = br.readUInt32() # = readU32(file)
            endOfBonesUnk1Adress = br.readUInt32() # = readU32(file)
            endOfBonesUnk2Adress = br.readUInt32() # = readU32(file)
            dataSize = br.readUInt32() # = readU32(file)
            br.seek(8, os.SEEK_CUR) # 2x dataSize
            br.seek(112, os.SEEK_CUR)

            #  Data chunk start
            dataStartAdress = br.tell()
            #log->addLineAndFlush(formatString("dataStartAdress = %d", dataStartAdress))
            br.seek(8, os.SEEK_CUR)
            nbBones = br.readUInt32()
            this_skeleton.nbBones = nbBones #skeleton.setBonesCount(nbBones)
            #log->addLineAndFlush(formatString("%d bones at %d", nbBones, br.tell()-4))
            br.seek(20, os.SEEK_CUR) # 3x bones count

            br.seek(16, os.SEEK_CUR)

            rootName = br.readString(16) # getString(br.fhandle) #core::stringc rootName = readStringFixedSize(file, 16)
            #log->addLineAndFlush(formatString("Root = %s", rootName.c_str()))

            bonesParentIdChunkAdress = br.tell()
            #long bonesNameChunkAdress = (dataStartAdress + endOfBonesNamesAdress) - totalNamesSize
            #long bonesNameChunkAdress = (dataStartAdress + endOfBonesNamesAdress) - (nbBones * 48)
            
            def seemToBeAnASCIICharacter(c):
                return c >= 2 and c <= 127

            boneNameSizes = array.array('i', [0] * nbBones)
            totalNamesSize = 0

            # Search bone names size
            br.seek(dataStartAdress + endOfBonesNamesAdress-1)
            for i in range(nbBones):
                isInText: bool = False
                textSize = 0
                while (1):
                    c = br.readByte() #char c = readS8(file);
                    br.seek(-2, os.SEEK_CUR)

                    if ( not isInText and seemToBeAnASCIICharacter(c)):
                        isInText = True
                    if (isInText and not seemToBeAnASCIICharacter(c)):
                        br.seek(1, os.SEEK_CUR)
                        boneNameSizes[nbBones-(i+1)] = textSize
                        totalNamesSize += textSize
                        #log->addLineAndFlush(formatString("Text size is : %d", textSize));
                        break
                    textSize+=1
            bonesNameChunkAdress = (dataStartAdress + endOfBonesNamesAdress) - totalNamesSize

            offset = 0
            br.seek(bonesNameChunkAdress - 8)
            while True:
                fl = br.readFloat()
                if fl > 0.09 and fl < 10.1:
                    break
                br.seek(-8, os.SEEK_CUR)
                offset += 4
            bonesTransformChunkAdress = bonesNameChunkAdress - (offset + nbBones * 48)

            br.seek(bonesNameChunkAdress)

            for i in range(nbBones):
                boneName = br.readString(boneNameSizes[i])
                boneName = boneName.replace('\x00', '')
                this_skeleton.names.append(boneName)
            
            br.seek(bonesParentIdChunkAdress)
            for i in range(nbBones):
                this_skeleton.parentIdx.append(br.readInt16())

            br.seek(bonesTransformChunkAdress)
            for _ in range(nbBones):
                this_skeleton.positions.append(CQuaternion(br.fhandle))
                this_skeleton.rotations.append(CQuaternion(br.fhandle))
                this_skeleton.scales.append(CQuaternion(br.fhandle))

    return this_skeleton
    
    

    
    
    
    

    for item in skelly.PROPS:
        if item.theName == "bones":
            for bone in item.More:
                this_skeleton.names.append(bone.elementName)
        if item.theName == "tracks":
            for track in item.More:
                this_skeleton.tracks.append(track.elementName)
        if item.theName == "parentIndices":
            this_skeleton.parentIdx = item.value[:]
    if hasattr(skelly, "rigData"):
        for data in skelly.rigData.rigData:
            this_skeleton.positions.append(data.position)
            this_skeleton.rotations.append(data.rotation)
            this_skeleton.scales.append(data.scale)
    return this_skeleton

def create_Skeleton(file):
    for chunk in file.CHUNKS.CHUNKS:
        if chunk.name == "CSkeleton":
            skelly = read_skelly(chunk)
            break
    return skelly

def load_bin_face(fileName) -> w3_types.CMimicFace:
    face_fileName = fileName
    with open(face_fileName,"rb") as f:
        theFile = getCR2W(f)
        f.close()
        CMimicFace = create_CMimicFace(theFile)
    return CMimicFace

def load_bin_skeleton(fileName):
    with open(fileName,"rb") as f:
        theFile = getCR2W(f)
        
        if theFile.HEADER.version <= 115:
            rig = create_Skeleton_w2(f, theFile)
        else:
            f.close()
            rig = create_Skeleton(theFile)
    for i, rot in enumerate(rig.rotations):
        rig.rotations[i].w = -rot.w
    final = readCSkeletonData(rig)
    return final