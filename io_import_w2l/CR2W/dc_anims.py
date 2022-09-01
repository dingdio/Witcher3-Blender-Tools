from .setup_logging import *
log = logging.getLogger(__name__)

import os
import math
from pathlib import Path

from .CR2W_helpers import Enums

from .dc_skeleton import create_CMimicFace, create_Skeleton

from .common_blender import repo_file
from .CR2W_types import getCR2W
from .read_json_w3 import readCSkeletonData
from . import w3_types
from .w3_types import ( Track, w2AnimsFrames, Quaternion, Vector3D )

from .bin_helpers import (ReadUlong48, readUShort,
                        readFloat,
                        ReadFloat24,
                        ReadFloat16)

from bStream import *

class CVector3D:
    def __init__(self, f, compression = 0):
        self.x = 0.0
        self.y = 0.0
        self.z = 0.0
        if (compression == 0):
            self.x = readFloat(f)
            self.y = readFloat(f)
            self.z = readFloat(f)
        if (compression == 1):
            self.x = ReadFloat24(f)
            self.y = ReadFloat24(f)
            self.z = ReadFloat24(f)
        if (compression == 2):
            self.x = ReadFloat16(f)
            self.y = ReadFloat16(f)
            self.z = ReadFloat16(f)
    def getList(self):
        return [self.x, self.y, self.z]

class ReadCompressFloat():
    def __init__(self, f, compression):
        val = 0
        if (compression == 0):
            val = readFloat(f)
        if (compression == 1):
            val = ReadFloat24(f)
        if (compression == 2):
            val = ReadFloat16(f)
        self.val = val

def create_lipsync_anim(file, Skeleton_file):
    CHUNKS = file.CHUNKS.CHUNKS
    bones = []
    tracks = []

    for chunk in CHUNKS:
        if chunk.name == "CSkeletalAnimation":
            CSkeletalAnimation = chunk
        if chunk.name == "CAnimationBufferBitwiseCompressed":
            CAnimationBufferBitwiseCompressed = chunk
    return create_anim(file, CSkeletalAnimation, CAnimationBufferBitwiseCompressed, Skeleton_file)

def create_anim(file, CSkeletalAnimation, CAnimationBufferBitwiseCompressed, Skeleton_file):
    bones = []
    tracks = []

    #ANIM PART
    chunk = CSkeletalAnimation
    SkeletalAnimationType = "SAT_Normal"
    AdditiveType = None
    for prop in chunk.PROPS:
        if prop.theName == "name":
            name = prop.Index.String
        if prop.theName == "duration":
            duration = prop.Value
        if prop.theName == "framesPerSecond":
            framesPerSecond = prop.Value
        if prop.theName == "Animation type for reimport":
            SkeletalAnimationType = prop.ToString()
        if prop.theName == "Additive type for reimport":
            AdditiveType = prop.ToString()
            
    #BUFFER PART
    chunk = CAnimationBufferBitwiseCompressed
    buffer_duration = chunk.GetVariableByName('duration')
    if buffer_duration is not None:
        buffer_duration = chunk.GetVariableByName('duration').Value
    else:
        buffer_duration = duration
    buffer_numFrames = chunk.GetVariableByName('numFrames').Value
    
    #some addatives don't have this?
    buffer_dt = chunk.GetVariableByName('dt').Value if chunk.GetVariableByName('dt') else None

    compressionSettings = chunk.GetVariableByName('compressionSettings')
    if compressionSettings is not None:
        orientationCompressionMethod = compressionSettings.GetVariableByName('orientationCompressionMethod').Index.String
    else:
        orientationCompressionMethod = chunk.GetVariableByName('orientationCompressionMethod')
        if orientationCompressionMethod is not None:
            orientationCompressionMethod = chunk.GetVariableByName('orientationCompressionMethod').Index.String
        else:
            orientationCompressionMethod = "ABOCM_PackIn64bitsW"
    
    the_data = []
    
    deferredData = chunk.GetVariableByName("deferredData");
    streamingOption = chunk.GetVariableByName("streamingOption")
    if (deferredData is not None and deferredData.ValueA != 0):
        if (streamingOption is not None and streamingOption.Index.String == "ABSO_PartiallyStreamable"):
            def_path = file.fileName + "." + str(deferredData.ValueA) + ".buffer"
            f = open(def_path,"rb")
            def_data = f.read()
            data_in_file = chunk.GetVariableByName('data').value
            b = bytearray(data_in_file) + def_data
            the_data = bStream(data = b)
            f.close()

            # f = open("data_temp", "wb")
            # f.write(bytearray(the_data))
            # f.close()

            # data = ConvertAnimation.Combine((chunk.GetVariableByName("data") as CByteArray).Bytes,
            # File.ReadAllBytes(animFile.FileName + "." + deferredData.val + ".buffer"));
        else:
            def_path = file.fileName + "." + str(deferredData.ValueA) + ".buffer"
            f = open(def_path,"rb")
            b = f.read()
            the_data = bStream(data = b)
            f.close()
            # f = open("data_temp", "wb")
            # f.write(bytearray(def_data))
            # f.close()
            #data = File.ReadAllBytes();
    else:
        #get data
        b = chunk.GetVariableByName('data').value
        the_data = bStream(data = bytearray(b))
        # f = open("data_temp", "wb")
        # f.write(bytearray(the_data))
        # f.close()
    #f = open("data_temp","rb")
    f = the_data
    bones_prop = chunk.GetVariableByName('bones')
    for (idx, bone) in enumerate(bones_prop.More):
        this_bone = w2AnimsFrames(idx,
            BoneName = Skeleton_file.names[idx],
            position_dt = "",
            position_numFrames = "",
            positionFrames = [],
            rotation_dt = "",
            rotation_numFrames = "",
            rotationFrames = [],
            scale_dt = "",
            scale_numFrames = "",
            scaleFrames = [],
            rotationFramesQuat = "")
        #for item in boneData.More:
        this_bone.position_dt = bone.position.GetVariableByName('dt').Value
        this_bone.position_numFrames = bone.position.GetVariableByName('numFrames').Value
        compression = bone.position.GetVariableByName('compression')
        if compression is not None:
            compression = bone.position.GetVariableByName('compression').Value
        else:
            compression = 0
        dataAddr = bone.position.GetVariableByName('dataAddr')
        dataAddrFallback = bone.position.GetVariableByName('dataAddrFallback')
        if dataAddr is not None:
            dataAddr = dataAddr.Value#print(dataAddr.Value)
        else:
            dataAddr = 0
        if dataAddrFallback is not None:
            dataAddrFallback = dataAddrFallback.Value #print(dataAddrFallback.Value)
        else:
            dataAddrFallback = 0
        f.seek(dataAddr)
        for _ in range(0, this_bone.position_numFrames):
            this_bone.positionFrames.append(CVector3D(f, compression).getList())

        # if len(this_bone.positionFrames) == 0:
        #     this_bone.positionFrames = [{"x": 0.0,"y": 0.0,"z": 0.0}]
        this_bone.rotation_dt = bone.orientation.GetVariableByName('dt').Value
        this_bone.rotation_numFrames = bone.orientation.GetVariableByName('numFrames').Value
        dataAddr = bone.orientation.GetVariableByName('dataAddr')
        dataAddrFallback = bone.orientation.GetVariableByName('dataAddrFallback')
        compression = bone.orientation.GetVariableByName('compression')
        if compression is not None:
            compression = bone.orientation.GetVariableByName('compression').Value
        else:
            compression = 0
        if dataAddr is not None:
            dataAddr = dataAddr.Value#print(dataAddr.Value)
        else:
            dataAddr = 0
        if dataAddrFallback is not None:
            dataAddrFallback = dataAddrFallback.Value #print(dataAddrFallback.Value)
        else:
            dataAddrFallback = 0
        f.seek(dataAddr)

        for _ in range(0, this_bone.rotation_numFrames):
            if "ABOCM_PackIn48bitsW" in orientationCompressionMethod:
                bits = ReadUlong48(f)
                orients = []
                orients.append((bits & 0x0000FFF000000000) >> 36)
                orients.append((bits & 0x0000000FFF000000) >> 24)
                orients.append((bits & 0x0000000000FFF000) >> 12)
                orients.append((bits & 0x0000000000000FFF))
                for (i, item) in enumerate(orients):
                    orients[i] = (2047.0 - orients[i]) * (1 / 2048.0)
                orients[3] = -orients[3]
                this_bone.rotationFrames.append(Quaternion(orients[0], orients[1], orients[2], orients[3]))
                #print(bits)
            if "ABOCM_AsFloat_XYZSignedWInLastBit" in orientationCompressionMethod:
                (x, y, z) = CVector3D(f, compression).getList()
                int_values = [x for x in bytearray(struct.pack("f", z))]
                signW = (int_values[0] & 1) > 0;
                minScalar = min(x * x + y * y + z * z, 1.0);
                w = math.sqrt(1.0 - minScalar);
                if (not signW):
                    w = -w;
                this_bone.rotationFrames.append(Quaternion(x, y, z, w))
            if "ABOCM_PackIn64bitsW" in orientationCompressionMethod:
                orients = []
                orients.append(readUShort(f))
                orients.append(readUShort(f))
                orients.append(readUShort(f))
                orients.append(readUShort(f))

                for (i, item) in enumerate(orients):
                    orients[i] = (32768.0 - orients[i]) * (1 / 32767.0)
                orients[3] = -orients[3]
                this_bone.rotationFrames.append(Quaternion(orients[0], orients[1], orients[2], orients[3]))
        this_bone.scale_dt = bone.scale.GetVariableByName('dt').Value
        this_bone.scale_numFrames = bone.scale.GetVariableByName('numFrames').Value
        compression = bone.scale.GetVariableByName('compression')
        if compression is not None:
            compression = bone.scale.GetVariableByName('compression').Value
        else:
            compression = 2
        dataAddr = bone.scale.GetVariableByName('dataAddr')
        dataAddrFallback = bone.scale.GetVariableByName('dataAddrFallback')
        if dataAddr is not None:
            dataAddr = dataAddr.Value#print(dataAddr.Value)
        else:
            dataAddr = 0
        if dataAddrFallback is not None:
            dataAddrFallback = dataAddrFallback.Value #print(dataAddrFallback.Value)
        else:
            dataAddrFallback = 0
        f.seek(dataAddr)
        for _ in range(0, this_bone.scale_numFrames):
            this_bone.scaleFrames.append(CVector3D(f, compression).getList())

        this_bone.rotationFramesQuat = this_bone.rotationFrames
        bones.append(this_bone)


    tracks_prop = chunk.GetVariableByName('tracks')
    if tracks_prop is not None:
        for (idx, track) in enumerate(tracks_prop.More):
            this_track = Track(idx,
                trackName = Skeleton_file.tracks[idx],
                numFrames = "",
                dt = "",
                trackFrames = [])
            trackData = track
            this_track.dt = trackData.GetVariableByName('dt').Value
            this_track.numFrames = trackData.GetVariableByName('numFrames').Value
            compression = trackData.GetVariableByName('compression')
            if compression is not None:
                compression = compression.Value
            else:
                compression = 0
            dataAddr = trackData.GetVariableByName('dataAddr')
            dataAddrFallback = trackData.GetVariableByName('dataAddrFallback')
            if dataAddr is not None:
                dataAddr = dataAddr.Value #print(dataAddr.Value)
            else:
                dataAddr = 0
            if dataAddrFallback is not None:
                dataAddrFallback = dataAddrFallback.Value #print(dataAddrFallback.Value)
            else:
                dataAddrFallback = 0
            f.seek(dataAddr)
            for _ in range(0, this_track.numFrames):
                this_track.trackFrames.append(ReadCompressFloat(f, compression).val)
            tracks.append(this_track)

    buffer = w3_types.CAnimationBufferBitwiseCompressed(bones, tracks, duration=buffer_duration, numFrames=buffer_numFrames, dt=buffer_dt)
    anim = w3_types.CSkeletalAnimation(name, duration, framesPerSecond, animBuffer=buffer, SkeletalAnimationType = SkeletalAnimationType, AdditiveType = AdditiveType)
    return anim

def create_anim_set(file, Skeleton_file):
    CHUNKS = file.CHUNKS.CHUNKS
    for chunk in CHUNKS:
        if chunk.name == "CSkeletalAnimationSet":
            set = chunk
            break;
    skeleton = set.GetVariableByName('skeleton')
    set_animations = set.GetVariableByName('animations')
    animations = []
    for idx, anim_ptr in enumerate(set_animations.value):
        anim_entry = CHUNKS[anim_ptr-1]
        anim = CHUNKS[anim_entry.GetVariableByName('animation').Value-1]
        anim_buffer = CHUNKS[anim.GetVariableByName('animBuffer').Value-1]
        log.info(str(idx)+" "+anim.GetVariableByName('name').Index.String)
        animation = create_anim(file, anim, anim_buffer, Skeleton_file)
        entries = []
        final_entry = w3_types.CSkeletalAnimationSetEntry(animation, entries)
        animations.append(final_entry)

    final_set = w3_types.CSkeletalAnimationSet(animations)
    return final_set

def load_lipsync_file(fileName_in = False) -> w3_types.CSkeletalAnimation:
    if fileName_in:
        fileName = fileName_in
    #face_fileName = r"dlc\ep1\data\characters\models\secondary_npc\shani\h_01_wa__shani\h_01_wa__shani.w3fac"
    face_fileName = repo_file(r"characters\models\geralt\head\model\h_01_mg__geralt.w3fac")
    with open(face_fileName,"rb") as f:
        theFile = getCR2W(f)
        f.close()
        CMimicFace = create_CMimicFace(theFile)
    with open(fileName,"rb") as f:
        theFile = getCR2W(f)
        f.close()

    anim = create_lipsync_anim(theFile, CMimicFace.floatTrackSkeleton)
    return anim


def load_base_skeleton(rigPath):
    with open(rigPath, "rb") as f:
        theFile = getCR2W(f)
        f.close()
        if rigPath.endswith('.w3fac'):
            CMimicFace = create_CMimicFace(theFile)
            return CMimicFace.floatTrackSkeleton
        elif rigPath.endswith('.w2rig'):
            return create_Skeleton(theFile)
        else:
            log.error('Error loading rig, check path and extension.')
            return None

def load_bin_anims_single(fileName, anim_name = None, rigPath = None ) -> w3_types.CSkeletalAnimationSet:
    if not rigPath:
        rigPath = repo_file(r"characters\models\geralt\head\model\h_01_mg__geralt.w3fac")
    
    rig = load_base_skeleton(rigPath)
    with open(fileName, "rb") as f:
        theFile = getCR2W(f, anim_name)
        f.close()
    anim_set = create_anim_set(theFile, rig)
    return anim_set
    
def load_bin_anims(fileName, rigPath = False) -> w3_types.CSkeletalAnimationSet:
    
    if not rigPath:
        rigPath = repo_file(r"characters\base_entities\man_base\man_base.w2rig")
        if "witcher_scabbards" in fileName:
            rigPath = repo_file(r"characters\models\geralt\scabbards\model\scabbards_crossbow.w2rig")
    rig = load_base_skeleton(rigPath)
    #LOAD THE BASE SKELETON


    with open(fileName, "rb") as f:
        theFile = getCR2W(f)
        f.close()
    anim_set = create_anim_set(theFile, rig)
    return anim_set