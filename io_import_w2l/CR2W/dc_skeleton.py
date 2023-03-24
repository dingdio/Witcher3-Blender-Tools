import json
from .w3_types import Quaternion, Vector3D
from .read_json_w3 import readCSkeletonData
from . import w3_types
from .CR2W_types import getCR2W

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
        f.close()
        rig = create_Skeleton(theFile)
    for i, rot in enumerate(rig.rotations):
        rig.rotations[i].w = -rot.w
    final = readCSkeletonData(rig)
    return final