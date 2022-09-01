from .setup_logging import *
log = logging.getLogger(__name__)
from typing import List

import json
import re

from .CR2W_helpers import Enums
from . import read_json_w3

class base_w3(object):
    def __getitem__(self, item):
        return getattr(self, item)

class Vector3D(base_w3):
    def __init__(self, x, y, z):
            self.x = x
            self.y = y
            self.z = z
    def __iter__(self):
        return iter(['x','y','z'])

class Quaternion(base_w3):
    def __init__(self, x, y, z, w):
            self.X = x
            self.Y = y
            self.Z = z
            self.W = w
    def __iter__(self):
        return iter(['X','Y','Z','W'])
    def __json_serializable__(self):
        return [self.X, self.Y, self.Z, self.W]

class W3Bone(base_w3):
    def __init__(self, id, name, co, parentId, ro=False, ro_quat=False, sc=False):
        self.id = id
        self.name = name
        self.co = co
        self.ro = ro
        self.ro_quat = ro_quat
        self.sc = sc
        self.parentId = parentId

class w2AnimsFrames(base_w3): 
    def __init__(self,
                id,
                BoneName,
                position_dt,
                position_numFrames,
                positionFrames,
                rotation_dt,
                rotation_numFrames,
                rotationFrames,
                scale_dt,
                scale_numFrames,
                scaleFrames,
                rotationFramesQuat):
        self.id = id
        self.BoneName = BoneName
        self.position_dt = position_dt
        self.position_numFrames = position_numFrames
        self.positionFrames : Quaternion = positionFrames
        self.rotation_dt = rotation_dt
        self.rotation_numFrames = rotation_numFrames
        self.rotationFrames : Quaternion = rotationFrames
        self.scale_dt = scale_dt
        self.scale_numFrames = scale_numFrames
        self.scaleFrames = scaleFrames
        self.rotationFramesQuat : Quaternion = rotationFramesQuat

class CSkeleton(base_w3):
    def __init__(self, bones=[]):
        self.bones = bones

class SCutsceneActorDef(base_w3):
    def __init__(self,  tag,
                        name,
                        type,
                        template,
                        useMimic,
                        voiceTag):
        self.tag = tag
        self.name = name
        self.template = template
        self.useMimic = useMimic
        self.type = type
        self.voiceTag = voiceTag
    @classmethod
    def from_json(cls, data):
        return cls(**data)

class CSkeletalAnimationSet(base_w3):
    def __init__(self, animations=[]):
        self.animations:List[CSkeletalAnimationSetEntry] = animations
    @classmethod
    def from_json(cls, data):
        animations = list(map(CSkeletalAnimationSetEntry.from_json, data["animations"]))
        return cls(animations)

class CCutsceneTemplate(base_w3):
    def __init__(self, animations=[], SCutsceneActorDefs=[]):
        self.animations = animations
        self.SCutsceneActorDefs = []#SCutsceneActorDefs
    @classmethod
    def from_json(cls, data):
        SCutsceneActorDefs = []#list(map(SCutsceneActorDef.from_json, data["SCutsceneActorDefs"]))
        animations = list(map(CSkeletalAnimationSetEntry.from_json, data["animations"]))
        return cls(animations, SCutsceneActorDefs)

class CSkeletalAnimationSetEntry(base_w3):
    def __init__(self, animation="", entries=[]):
        self.animation: CSkeletalAnimation = animation
        self.entries = entries
    @classmethod
    def from_json(cls, data):
        data["animation"] = CSkeletalAnimation.from_json(data["animation"])
        return cls(**data)

class CSkeletalAnimation(base_w3):
    def __init__(self, name ="", duration=0.0, framesPerSecond=30.0, animBuffer=[], motionExtraction={}, SkeletalAnimationType = "SAT_Normal", AdditiveType=None):
        self.name: str = name
        self.duration : float = duration
        self.framesPerSecond : float = framesPerSecond
        self.animBuffer: IAnimationBuffer = animBuffer
        self.motionExtraction = motionExtraction
        self.SkeletalAnimationType : Enums.ESkeletalAnimationType = SkeletalAnimationType
        self.AdditiveType: Enums.EAdditiveType = AdditiveType
    @classmethod
    def from_json(cls, data):
        if 'parts' in data["animBuffer"]:
            animBuffer = CAnimationBufferMultipart.from_json(data["animBuffer"])
        else:
            animBuffer = CAnimationBufferBitwiseCompressed.from_json(data["animBuffer"])
        data["animBuffer"] = animBuffer
        return cls(**data)

from typing import Any

class IAnimationBuffer(base_w3):
    """docstring for IAnimationBuffer."""
    def __init__(self, arg):
        super(IAnimationBuffer, self).__init__()

class CAnimationBufferBitwiseCompressed(IAnimationBuffer):
    def __init__(self, bones=[], tracks=[], duration=0.0, numFrames=0, dt=0.0333333351, version = 0):
        self.bones: List[w2AnimsFrames] = bones
        self.tracks = tracks
        self.duration = duration
        self.numFrames = numFrames
        self.dt = dt
        self.version = version
    @classmethod
    def from_json(cls, data):
        data["bones"] = read_json_w3.readAnimation(data["bones"])
        if data.get("tracks", []):
            data["tracks"] = read_json_w3.readTracks(data["tracks"])
        return cls(**data)

class CAnimationBufferMultipart(IAnimationBuffer):
    def __init__(self, numFrames=0,numBones=0, numTracks=0, firstFrames=[], parts=[] ):
        self.numFrames = numFrames
        self.numBones = numBones
        self.numTracks = numTracks
        self.firstFrames = firstFrames
        self.parts = parts
    @classmethod
    def from_json(cls, data):
        parts = list(map(CAnimationBufferBitwiseCompressed.from_json, data["parts"]))
        data["parts"] = parts
        return cls(**data)

class CMimicFace(base_w3):
    def __init__(self, name="", mimicSkeleton = [], floatTrackSkeleton = [], mimicPoses=[]):
        self.name = name
        self.mimicSkeleton = mimicSkeleton
        self.floatTrackSkeleton = floatTrackSkeleton
        self.mimicPoses = mimicPoses

class Track(base_w3): 
    def __init__(self,
                id,
                trackName,
                numFrames,
                dt,
                trackFrames):
        self.id = id
        self.trackName = trackName
        self.numFrames = numFrames
        self.dt = dt
        self.trackFrames = trackFrames

class CMovingPhysicalAgentComponent(base_w3):
    def __init__(self, skeleton="none", name="none"):
        self.skeleton = skeleton
        self.name = name
    @classmethod
    def from_json(cls, data):
        try:
            return cls(**data)
        except Exception as e:
            return cls(data['skeleton'], data['name'])

class CAppearance(base_w3):
    def __init__(self,
                name="",
                includedTemplates=[]):
        self.name = name
        self.includedTemplates = includedTemplates
    @classmethod
    def from_json(cls, data):
        return cls(**data)

class CColorShift(base_w3):
    """docstring for ColorShift."""
    def __init__(self, hue: int = 0, saturation: int = 0, luminance: int = 0 ):
        super(CColorShift, self).__init__()
        self.hue: int = hue if hue else 0
        self.saturation: int = saturation if saturation else 0
        self.luminance: int = luminance if luminance else 0
        self.hue_bl: (-180 + self.hue) / 360
        self.saturation_bl: self.saturation / 255
        self.hue_bl: self.luminance / 255
            
            

class SEntityTemplateColoringEntry(base_w3):
    """docstring for SEntityTemplateColoringEntry."""
    def __init__(self, appearance = "", componentName = "", colorShift1 =None, colorShift2 = None):
        super(SEntityTemplateColoringEntry, self).__init__()
        self.appearance:str = appearance
        self.componentName:str = componentName
        self.colorShift1:CColorShift = colorShift1
        self.colorShift2:CColorShift = colorShift2

    

class Entity(base_w3): 
    def __init__(self,
                name="default_name",
                MovingPhysicalAgentComponent= {},
                appearances = [],
                staticMeshes = {},
                CAnimAnimsetsParam = [],
                CAnimMimicParam = [],
                coloringEntries = []):
        self.name:str = name
        self.MovingPhysicalAgentComponent = MovingPhysicalAgentComponent
        self.appearances = appearances
        self.staticMeshes = staticMeshes
        self.CAnimAnimsetsParam = CAnimAnimsetsParam
        self.CAnimMimicParam = CAnimMimicParam,
        self.coloringEntries: List[SEntityTemplateColoringEntry] = coloringEntries

    @classmethod
    def from_json(cls, data):
        data["MovingPhysicalAgentComponent"] = CMovingPhysicalAgentComponent.from_json(data["MovingPhysicalAgentComponent"])
        data["appearances"] = list(map(CAppearance.from_json, data["appearances"]))
        return cls(**data)

class w2rig(base_w3):
    def __init__(self,
                nbBones=94,
                names= [],
                tracks= [],
                parentIdx = [],
                positions = [],
                rotations = [],
                scales = []):
        self.nbBones = nbBones
        self.names = names
        self.tracks = tracks
        self.parentIdx = parentIdx
        self.positions = positions
        self.rotations = rotations
        self.scales = scales
    @classmethod
    def from_json(cls, data):
        return cls(**data)