from .setup_logging import *
log = logging.getLogger(__name__)
from typing import List

import json
import re
import sys

from .CR2W_helpers import Enums
from . import read_json_w3

class base_w3(object):
    def __getitem__(self, item):
        return getattr(self, item)
    
from .w3_types_CStorySceneEvent import elementTypes

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
    def __init__(self, bones=[], tracks=[]):
        self.bones = bones
        self.tracks = tracks

from .CR2W_types import PROPERTY, v_types

def loadProps(self, args):
    if not args:
        print('tried to load props with args')
        return

    if hasattr(args[0], "PROPS"):
        PROPS = args[0].PROPS
    if hasattr(args[0], "MoreProps"):
        PROPS = args[0].MoreProps
    if hasattr(args[0], "More"):
        PROPS = args[0].More

    arg:PROPERTY
    for arg in PROPS:
        if arg.theType in v_types:
            try:
                setattr(self, arg.theName, arg.Value)
            except Exception as e:
                if arg.theName == 'in':
                    setattr(self, "_in", arg.Value)
                else:
                    setattr(self, arg.theName, arg.String.String)
        elif arg.theType == 'CName':
            setattr(self, arg.theName, arg.Index.String)
        elif arg.theType == 'soft:CEntityTemplate':
            setattr(self, arg.theName, arg.Index.Path)
        elif arg.theType == 'TagList':
            #arg.TagList = arg.list(map(lambda x: x.value, arg.TagList))
            setattr(self, arg.theName, list(map(lambda x: x.value, arg.TagList)))
        else:
            if arg.theType in elementTypes:
                setattr(self, arg.theName, [arg])
            else:
                setattr(self, arg.theName, arg)

def str_to_class(classname):
    return getattr(sys.modules[__name__], classname)

class CStorySceneElement(base_w3):
    def __init__(self, *args):
        self.elementID = None #<property Name="elementID" Type="String" />
        self.approvedDuration = None #<property Name="approvedDuration" Type="Float" />
        self.isCopy = None #<property Name="isCopy" Type="Bool" />
        loadProps(self, args)
        
class CStoryScenePauseElement(CStorySceneElement):
    def __init__(self, *args):
        self.elementID = None #<property Name="elementID" Type="String" />
        self.approvedDuration = None #<property Name="approvedDuration" Type="Float" />
        self.isCopy = None #<property Name="isCopy" Type="Bool" />
        self.duration = None #<property Name="duration" Type="Float" />
        loadProps(self, args)

class CAbstractStorySceneLine(CStorySceneElement):
    def __init__(self, *args):
        self.elementID = None #<property Name="elementID" Type="String" />
        self.approvedDuration = None #<property Name="approvedDuration" Type="Float" />
        self.isCopy = None #<property Name="isCopy" Type="Bool" />
        self.voicetag = None #<property Name="voicetag" Type="CName" />
        self.comment = None #<property Name="comment" Type="LocalizedString" />
        self.speakingTo = None #<property Name="speakingTo" Type="CName" />
        loadProps(self, args)

class CStorySceneLine(CAbstractStorySceneLine):
    def __init__(self, *args):
        self.elementID = None #<property Name="elementID" Type="String" />
        self.approvedDuration = None #<property Name="approvedDuration" Type="Float" />
        self.isCopy = None #<property Name="isCopy" Type="Bool" />
        self.voicetag = None #<property Name="voicetag" Type="CName" />
        self.comment = None #<property Name="comment" Type="LocalizedString" />
        self.speakingTo = None #<property Name="speakingTo" Type="CName" />
        self.dialogLine = None #<property Name="dialogLine" Type="LocalizedString" />
        self.voiceFileName = None #<property Name="voiceFileName" Type="String" />
        self.noBreak = None #<property Name="noBreak" Type="Bool" />
        self.soundEventName = None #<property Name="soundEventName" Type="StringAnsi" />
        self.disableOcclusion = None #<property Name="disableOcclusion" Type="Bool" />
        self.isBackgroundLine = None #<property Name="isBackgroundLine" Type="Bool" />
        self.alternativeUI = None #<property Name="alternativeUI" Type="Bool" />
        loadProps(self, args)

#################
#    EVENTS     #
#               #
#################

class CStorySceneEvent(base_w3):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        loadProps(self, args)

class CStorySceneEventInterpolation(CStorySceneEvent):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.keyGuids = None #<property Name="keyGuids" Type="array:2,0,CGUID" />
        self.interpolationMethod = None #<property Name="interpolationMethod" Type="EInterpolationMethod" />
        self.easeInStyle = None #<property Name="easeInStyle" Type="EInterpolationEasingStyle" />
        self.easeInParameter = None #<property Name="easeInParameter" Type="Float" />
        self.easeOutStyle = None #<property Name="easeOutStyle" Type="EInterpolationEasingStyle" />
        self.easeOutParameter = None #<property Name="easeOutParameter" Type="Float" />
        loadProps(self, args)

class CStorySceneEventLightProperties(CStorySceneEvent):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.lightId = None #<property Name="lightId" Type="CName" />
        self.enabled = None #<property Name="enabled" Type="Bool" />
        self.additiveChanges = None #<property Name="additiveChanges" Type="Bool" />
        self.color = None #<property Name="color" Type="Color" />
        self.lightColorSource = None #<property Name="lightColorSource" Type="ESceneEventLightColorSource" />
        self.radius = None #<property Name="radius" Type="SSimpleCurve" />
        self.brightness = None #<property Name="brightness" Type="SSimpleCurve" />
        self.attenuation = None #<property Name="attenuation" Type="SSimpleCurve" />
        self.placement = None #<property Name="placement" Type="EngineTransform" />
        self.flickering = None #<property Name="flickering" Type="SLightFlickering" />
        self.useGlobalCoords = None #<property Name="useGlobalCoords" Type="Bool" />
        self.spotLightProperties = None #<property Name="spotLightProperties" Type="SStorySceneSpotLightProperties" />
        self.dimmerProperties = None #<property Name="dimmerProperties" Type="SStorySceneLightDimmerProperties" />
        self.attachment = None #<property Name="attachment" Type="SStorySceneAttachmentInfo" />
        self.lightTracker = None #<property Name="lightTracker" Type="SStorySceneLightTrackingInfo" />
        loadProps(self, args)

class CStorySceneEventDuration(CStorySceneEvent):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.duration = None #<property Name="duration" Type="Float" />
        loadProps(self, args)

class CStorySceneEventAnimClip(CStorySceneEventDuration):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.duration = None #<property Name="duration" Type="Float" />
        self.actor = None #<property Name="actor" Type="CName" />
        self.blendIn = None #<property Name="blendIn" Type="Float" />
        self.blendOut = None #<property Name="blendOut" Type="Float" />
        self.clipFront = None #<property Name="clipFront" Type="Float" />
        self.clipEnd = None #<property Name="clipEnd" Type="Float" />
        self.stretch = None #<property Name="stretch" Type="Float" />
        self.allowLookatsLevel = None #<property Name="allowLookatsLevel" Type="ELookAtLevel" />
        self.weight = None #<property Name="weight" Type="Float" />
        self.forceAnimationTimeFlag = None #<property Name="forceAnimationTimeFlag" Type="Bool" />
        self.forceAnimationTime = None #<property Name="forceAnimationTime" Type="Float" />
        self.voiceWeightCurve = None #<property Name="voiceWeightCurve" Type="SVoiceWeightCurve" />
        self.allowPoseCorrection = None #<property Name="allowPoseCorrection" Type="Bool" />
        loadProps(self, args)

class CStorySceneEventAnimation(CStorySceneEventAnimClip):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.duration = None #<property Name="duration" Type="Float" />
        self.actor = None #<property Name="actor" Type="CName" />
        self.blendIn = None #<property Name="blendIn" Type="Float" />
        self.blendOut = None #<property Name="blendOut" Type="Float" />
        self.clipFront = None #<property Name="clipFront" Type="Float" />
        self.clipEnd = None #<property Name="clipEnd" Type="Float" />
        self.stretch = None #<property Name="stretch" Type="Float" />
        self.allowLookatsLevel = None #<property Name="allowLookatsLevel" Type="ELookAtLevel" />
        self.weight = None #<property Name="weight" Type="Float" />
        self.forceAnimationTimeFlag = None #<property Name="forceAnimationTimeFlag" Type="Bool" />
        self.forceAnimationTime = None #<property Name="forceAnimationTime" Type="Float" />
        self.voiceWeightCurve = None #<property Name="voiceWeightCurve" Type="SVoiceWeightCurve" />
        self.allowPoseCorrection = None #<property Name="allowPoseCorrection" Type="Bool" />
        self.animationName = None #<property Name="animationName" Type="CName" />
        self.useMotionExtraction = None #<property Name="useMotionExtraction" Type="Bool" />
        self.useFakeMotion = None #<property Name="useFakeMotion" Type="Bool" />
        self.gatherSyncTokens = None #<property Name="gatherSyncTokens" Type="Bool" />
        self.muteSoundEvents = None #<property Name="muteSoundEvents" Type="Bool" />
        self.disableLookAt = None #<property Name="disableLookAt" Type="Bool" />
        self.disableLookAtSpeed = None #<property Name="disableLookAtSpeed" Type="Float" />
        self.useLowerBodyPartsForLookAt = None #<property Name="useLowerBodyPartsForLookAt" Type="Bool" />
        self.bonesIdx = None #<property Name="bonesIdx" Type="array:2,0,Int32" />
        self.bonesWeight = None #<property Name="bonesWeight" Type="array:2,0,Float" />
        self.animationType = None #<property Name="animationType" Type="EStorySceneAnimationType" />
        self.addConvertToAdditive = None #<property Name="addConvertToAdditive" Type="Bool" />
        self.addAdditiveType = None #<property Name="addAdditiveType" Type="EAdditiveType" />
        self.useWeightCurve = None #<property Name="useWeightCurve" Type="Bool" />
        self.weightCurve = None #<property Name="weightCurve" Type="SCurveData" />
        self.weightCurveChanged = None #<property Name="weightCurveChanged" Type="Bool" />
        self.supportsMotionExClipFront = None #<property Name="supportsMotionExClipFront" Type="Bool" />
        loadProps(self, args)

class CStorySceneEventAdditiveAnimation(CStorySceneEventAnimation):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.duration = None #<property Name="duration" Type="Float" />
        self.actor = None #<property Name="actor" Type="CName" />
        self.blendIn = None #<property Name="blendIn" Type="Float" />
        self.blendOut = None #<property Name="blendOut" Type="Float" />
        self.clipFront = None #<property Name="clipFront" Type="Float" />
        self.clipEnd = None #<property Name="clipEnd" Type="Float" />
        self.stretch = None #<property Name="stretch" Type="Float" />
        self.allowLookatsLevel = None #<property Name="allowLookatsLevel" Type="ELookAtLevel" />
        self.weight = None #<property Name="weight" Type="Float" />
        self.forceAnimationTimeFlag = None #<property Name="forceAnimationTimeFlag" Type="Bool" />
        self.forceAnimationTime = None #<property Name="forceAnimationTime" Type="Float" />
        self.voiceWeightCurve = None #<property Name="voiceWeightCurve" Type="SVoiceWeightCurve" />
        self.allowPoseCorrection = None #<property Name="allowPoseCorrection" Type="Bool" />
        self.animationName = None #<property Name="animationName" Type="CName" />
        self.useMotionExtraction = None #<property Name="useMotionExtraction" Type="Bool" />
        self.useFakeMotion = None #<property Name="useFakeMotion" Type="Bool" />
        self.gatherSyncTokens = None #<property Name="gatherSyncTokens" Type="Bool" />
        self.muteSoundEvents = None #<property Name="muteSoundEvents" Type="Bool" />
        self.disableLookAt = None #<property Name="disableLookAt" Type="Bool" />
        self.disableLookAtSpeed = None #<property Name="disableLookAtSpeed" Type="Float" />
        self.useLowerBodyPartsForLookAt = None #<property Name="useLowerBodyPartsForLookAt" Type="Bool" />
        self.bonesIdx = None #<property Name="bonesIdx" Type="array:2,0,Int32" />
        self.bonesWeight = None #<property Name="bonesWeight" Type="array:2,0,Float" />
        self.animationType = None #<property Name="animationType" Type="EStorySceneAnimationType" />
        self.addConvertToAdditive = None #<property Name="addConvertToAdditive" Type="Bool" />
        self.addAdditiveType = None #<property Name="addAdditiveType" Type="EAdditiveType" />
        self.useWeightCurve = None #<property Name="useWeightCurve" Type="Bool" />
        self.weightCurve = None #<property Name="weightCurve" Type="SCurveData" />
        self.weightCurveChanged = None #<property Name="weightCurveChanged" Type="Bool" />
        self.supportsMotionExClipFront = None #<property Name="supportsMotionExClipFront" Type="Bool" />
        self.convertToAdditive = None #<property Name="convertToAdditive" Type="Bool" />
        self.additiveType = None #<property Name="additiveType" Type="EAdditiveType" />
        loadProps(self, args)


class CStorySceneEventApplyAppearance(CStorySceneEvent):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.actor = None #<property Name="actor" Type="CName" />
        self.appearance = None #<property Name="appearance" Type="CName" />
        loadProps(self, args)

class CStorySceneEventAttachPropToSlot(CStorySceneEvent):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.propId = None #<property Name="propId" Type="CName" />
        self.activate = None #<property Name="activate" Type="Bool" />
        self.actorName = None #<property Name="actorName" Type="CName" />
        self.slotName = None #<property Name="slotName" Type="CName" />
        self.snapAtStart = None #<property Name="snapAtStart" Type="Bool" />
        self.showHide = None #<property Name="showHide" Type="Bool" />
        self.offset = None #<property Name="offset" Type="EngineTransform" />
        loadProps(self, args)

class CStorySceneEventBlend(CStorySceneEventDuration):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.duration = None #<property Name="duration" Type="Float" />
        self.keys = None #<property Name="keys" Type="array:2,0,CGUID" />
        loadProps(self, args)

class CStorySceneEventCamera(CStorySceneEvent):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        loadProps(self, args)

class CStorySceneEventCameraAnim(CStorySceneEventAnimClip):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.duration = None #<property Name="duration" Type="Float" />
        self.actor = None #<property Name="actor" Type="CName" />
        self.blendIn = None #<property Name="blendIn" Type="Float" />
        self.blendOut = None #<property Name="blendOut" Type="Float" />
        self.clipFront = None #<property Name="clipFront" Type="Float" />
        self.clipEnd = None #<property Name="clipEnd" Type="Float" />
        self.stretch = None #<property Name="stretch" Type="Float" />
        self.allowLookatsLevel = None #<property Name="allowLookatsLevel" Type="ELookAtLevel" />
        self.weight = None #<property Name="weight" Type="Float" />
        self.forceAnimationTimeFlag = None #<property Name="forceAnimationTimeFlag" Type="Bool" />
        self.forceAnimationTime = None #<property Name="forceAnimationTime" Type="Float" />
        self.voiceWeightCurve = None #<property Name="voiceWeightCurve" Type="SVoiceWeightCurve" />
        self.allowPoseCorrection = None #<property Name="allowPoseCorrection" Type="Bool" />
        self.animationName = None #<property Name="animationName" Type="CName" />
        self.isIdle = None #<property Name="isIdle" Type="Bool" />
        loadProps(self, args)

class CStorySceneEventCameraBlend(CStorySceneEventDuration):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.duration = None #<property Name="duration" Type="Float" />
        self.blendKeys = None #<property Name="blendKeys" Type="array:2,0,SStorySceneCameraBlendKey" />
        self.interpolationType = None #<property Name="interpolationType" Type="ECameraInterpolation" />
        loadProps(self, args)

class CStorySceneEventCameraInterpolation(CStorySceneEventInterpolation):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.keyGuids = None #<property Name="keyGuids" Type="array:2,0,CGUID" />
        self.interpolationMethod = None #<property Name="interpolationMethod" Type="EInterpolationMethod" />
        self.easeInStyle = None #<property Name="easeInStyle" Type="EInterpolationEasingStyle" />
        self.easeInParameter = None #<property Name="easeInParameter" Type="Float" />
        self.easeOutStyle = None #<property Name="easeOutStyle" Type="EInterpolationEasingStyle" />
        self.easeOutParameter = None #<property Name="easeOutParameter" Type="Float" />
        self.keys = None #<property Name="keys" Type="array:2,0,CStorySceneEventCameraInterpolationKey" />
        
        
        loadProps(self, args)

class CStorySceneEventCameraInterpolationKey():
    def __init__(self, *args):
        self.bezierHandles = None #<property Name="bezierHandles" Type="[15]Bezier2dHandle" />
        self.interpolationTypes = None #<property Name="interpolationTypes" Type="[15]Uint32" />
        self.volatile = None #<property Name="volatile" Type="Bool" />
        loadProps(self, args)

class CStorySceneEventCameraLight(CStorySceneEvent):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.cameralightType = None #<property Name="cameralightType" Type="ECameraLightModType" />
        self.lightMod1 = None #<property Name="lightMod1" Type="SStorySceneCameraLightMod" />
        self.lightMod2 = None #<property Name="lightMod2" Type="SStorySceneCameraLightMod" />
        loadProps(self, args)

class CStorySceneEventCameraLightInterpolation(CStorySceneEventInterpolation):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.keyGuids = None #<property Name="keyGuids" Type="array:2,0,CGUID" />
        self.interpolationMethod = None #<property Name="interpolationMethod" Type="EInterpolationMethod" />
        self.easeInStyle = None #<property Name="easeInStyle" Type="EInterpolationEasingStyle" />
        self.easeInParameter = None #<property Name="easeInParameter" Type="Float" />
        self.easeOutStyle = None #<property Name="easeOutStyle" Type="EInterpolationEasingStyle" />
        self.easeOutParameter = None #<property Name="easeOutParameter" Type="Float" />
        self.keys = None #<property Name="keys" Type="array:2,0,CStorySceneEventCameraLightInterpolationKey" />
        loadProps(self, args)

class CStorySceneEventCameraLightInterpolationKey():
    def __init__(self, *args):
        self.bezierHandles = None #<property Name="bezierHandles" Type="[2]Bezier2dHandle" />
        self.interpolationTypes = None #<property Name="interpolationTypes" Type="[2]Uint32" />
        self.volatile = None #<property Name="volatile" Type="Bool" />
        loadProps(self, args)

class CStorySceneEventChangeActorGameState(CStorySceneEvent):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.actor = None #<property Name="actor" Type="CName" />
        self.snapToTerrain = None #<property Name="snapToTerrain" Type="Bool" />
        self.snapToTerrainDuration = None #<property Name="snapToTerrainDuration" Type="Float" />
        self.blendPoseDuration = None #<property Name="blendPoseDuration" Type="Float" />
        self.forceResetClothAndDangles = None #<property Name="forceResetClothAndDangles" Type="Bool" />
        self.switchToGameplayPose = None #<property Name="switchToGameplayPose" Type="Bool" />
        self.gameplayPoseTypeName = None #<property Name="gameplayPoseTypeName" Type="CName" />
        self.raiseGlobalBehaviorEvent = None #<property Name="raiseGlobalBehaviorEvent" Type="CName" />
        self.activateBehaviorGraph = None #<property Name="activateBehaviorGraph" Type="Int32" />
        self.startGameplayAction = None #<property Name="startGameplayAction" Type="Int32" />
        loadProps(self, args)

class CStorySceneEventChangePose(CStorySceneEventAnimClip):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.duration = None #<property Name="duration" Type="Float" />
        self.actor = None #<property Name="actor" Type="CName" />
        self.blendIn = None #<property Name="blendIn" Type="Float" />
        self.blendOut = None #<property Name="blendOut" Type="Float" />
        self.clipFront = None #<property Name="clipFront" Type="Float" />
        self.clipEnd = None #<property Name="clipEnd" Type="Float" />
        self.stretch = None #<property Name="stretch" Type="Float" />
        self.allowLookatsLevel = None #<property Name="allowLookatsLevel" Type="ELookAtLevel" />
        self.weight = None #<property Name="weight" Type="Float" />
        self.forceAnimationTimeFlag = None #<property Name="forceAnimationTimeFlag" Type="Bool" />
        self.forceAnimationTime = None #<property Name="forceAnimationTime" Type="Float" />
        self.voiceWeightCurve = None #<property Name="voiceWeightCurve" Type="SVoiceWeightCurve" />
        self.allowPoseCorrection = None #<property Name="allowPoseCorrection" Type="Bool" />
        self.stateName = None #<property Name="stateName" Type="CName" />
        self.status = None #<property Name="status" Type="CName" />
        self.emotionalState = None #<property Name="emotionalState" Type="CName" />
        self.poseName = None #<property Name="poseName" Type="CName" />
        self.transitionAnimation = None #<property Name="transitionAnimation" Type="CName" />
        self.useMotionExtraction = None #<property Name="useMotionExtraction" Type="Bool" />
        self.forceBodyIdleAnimation = None #<property Name="forceBodyIdleAnimation" Type="CName" />
        self.useWeightCurve = None #<property Name="useWeightCurve" Type="Bool" />
        self.weightCurve = None #<property Name="weightCurve" Type="SCurveData" />
        self.resetCloth = None #<property Name="resetCloth" Type="EDialogResetClothAndDanglesType" />
        loadProps(self, args)

class CStorySceneEventClothDisablingInterpolation(CStorySceneEventInterpolation):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.keyGuids = None #<property Name="keyGuids" Type="array:2,0,CGUID" />
        self.interpolationMethod = None #<property Name="interpolationMethod" Type="EInterpolationMethod" />
        self.easeInStyle = None #<property Name="easeInStyle" Type="EInterpolationEasingStyle" />
        self.easeInParameter = None #<property Name="easeInParameter" Type="Float" />
        self.easeOutStyle = None #<property Name="easeOutStyle" Type="EInterpolationEasingStyle" />
        self.easeOutParameter = None #<property Name="easeOutParameter" Type="Float" />
        self.keys = None #<property Name="keys" Type="array:2,0,CStorySceneEventClothDisablingInterpolationKey" />
        loadProps(self, args)

class CStorySceneEventClothDisablingInterpolationKey():
    def __init__(self, *args):
        self.bezierHandles = None #<property Name="bezierHandles" Type="[1]Bezier2dHandle" />
        self.interpolationTypes = None #<property Name="interpolationTypes" Type="[1]Uint32" />
        self.volatile = None #<property Name="volatile" Type="Bool" />
        loadProps(self, args)

class CStorySceneEventCsCamera(CStorySceneEvent):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        loadProps(self, args)

class CStorySceneEventCurveAnimation(CStorySceneEventDuration):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.duration = None #<property Name="duration" Type="Float" />
        self.curve = None #<property Name="curve" Type="SMultiCurve" />
        loadProps(self, args)

class CStorySceneEventCurveBlend(CStorySceneEventBlend):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.duration = None #<property Name="duration" Type="Float" />
        self.keys = None #<property Name="keys" Type="array:2,0,CGUID" />
        self.curve = None #<property Name="curve" Type="SMultiCurve" />
        loadProps(self, args)

class CStorySceneEventCustomCamera(CStorySceneEventCamera):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.cameraTranslation = None #<property Name="cameraTranslation" Type="Vector" />
        self.cameraRotation = None #<property Name="cameraRotation" Type="EulerAngles" />
        self.cameraZoom = None #<property Name="cameraZoom" Type="Float" />
        self.cameraFov = None #<property Name="cameraFov" Type="Float" />
        self.dofFocusDistFar = None #<property Name="dofFocusDistFar" Type="Float" />
        self.dofBlurDistFar = None #<property Name="dofBlurDistFar" Type="Float" />
        self.dofIntensity = None #<property Name="dofIntensity" Type="Float" />
        self.dofFocusDistNear = None #<property Name="dofFocusDistNear" Type="Float" />
        self.dofBlurDistNear = None #<property Name="dofBlurDistNear" Type="Float" />
        self.cameraDefinition = None #<property Name="cameraDefinition" Type="StorySceneCameraDefinition" />
        loadProps(self, args)

class CStorySceneEventCustomCameraInstance(CStorySceneEventCamera):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.customCameraName = None #<property Name="customCameraName" Type="CName" />
        self.enableCameraNoise = None #<property Name="enableCameraNoise" Type="Bool" />
        loadProps(self, args)

class CStorySceneEventDangleDisablingInterpolation(CStorySceneEventInterpolation):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.keyGuids = None #<property Name="keyGuids" Type="array:2,0,CGUID" />
        self.interpolationMethod = None #<property Name="interpolationMethod" Type="EInterpolationMethod" />
        self.easeInStyle = None #<property Name="easeInStyle" Type="EInterpolationEasingStyle" />
        self.easeInParameter = None #<property Name="easeInParameter" Type="Float" />
        self.easeOutStyle = None #<property Name="easeOutStyle" Type="EInterpolationEasingStyle" />
        self.easeOutParameter = None #<property Name="easeOutParameter" Type="Float" />
        self.keys = None #<property Name="keys" Type="array:2,0,CStorySceneEventDangleDisablingInterpolationKey" />
        loadProps(self, args)

class CStorySceneEventDangleDisablingInterpolationKey():
    def __init__(self, *args):
        self.bezierHandles = None #<property Name="bezierHandles" Type="[1]Bezier2dHandle" />
        self.interpolationTypes = None #<property Name="interpolationTypes" Type="[1]Uint32" />
        self.volatile = None #<property Name="volatile" Type="Bool" />
        loadProps(self, args)

class CStorySceneEventDebugComment(CStorySceneEventDuration):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.duration = None #<property Name="duration" Type="Float" />
        self.comment = None #<property Name="comment" Type="String" />
        loadProps(self, args)

class CStorySceneEventDespawn(CStorySceneEvent):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.actor = None #<property Name="actor" Type="CName" />
        loadProps(self, args)

class CStorySceneEventDialogLine(CStorySceneEvent):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.line = None #<property Name="line" Type="ptr:CStorySceneLine" />
        loadProps(self, args)


class CStorySceneEventEnhancedCameraBlend(CStorySceneEventCurveBlend):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.duration = None #<property Name="duration" Type="Float" />
        self.keys = None #<property Name="keys" Type="array:2,0,CGUID" />
        self.curve = None #<property Name="curve" Type="SMultiCurve" />
        self.baseCameraDefinition = None #<property Name="baseCameraDefinition" Type="StorySceneCameraDefinition" />
        loadProps(self, args)

class CStorySceneEventEnterActor(CStorySceneEventAnimClip):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.duration = None #<property Name="duration" Type="Float" />
        self.actor = None #<property Name="actor" Type="CName" />
        self.blendIn = None #<property Name="blendIn" Type="Float" />
        self.blendOut = None #<property Name="blendOut" Type="Float" />
        self.clipFront = None #<property Name="clipFront" Type="Float" />
        self.clipEnd = None #<property Name="clipEnd" Type="Float" />
        self.stretch = None #<property Name="stretch" Type="Float" />
        self.allowLookatsLevel = None #<property Name="allowLookatsLevel" Type="ELookAtLevel" />
        self.weight = None #<property Name="weight" Type="Float" />
        self.forceAnimationTimeFlag = None #<property Name="forceAnimationTimeFlag" Type="Bool" />
        self.forceAnimationTime = None #<property Name="forceAnimationTime" Type="Float" />
        self.voiceWeightCurve = None #<property Name="voiceWeightCurve" Type="SVoiceWeightCurve" />
        self.allowPoseCorrection = None #<property Name="allowPoseCorrection" Type="Bool" />
        self.behEvent = None #<property Name="behEvent" Type="CName" />
        loadProps(self, args)

class CStorySceneEventEquipItem(CStorySceneEvent):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.leftItem = None #<property Name="leftItem" Type="CName" />
        self.rightItem = None #<property Name="rightItem" Type="CName" />
        self.actor = None #<property Name="actor" Type="CName" />
        self.ignoreItemsWithTag = None #<property Name="ignoreItemsWithTag" Type="CName" />
        self.internalMode = None #<property Name="internalMode" Type="ESceneItemEventMode" />
        self.instant = None #<property Name="instant" Type="Bool" />
        loadProps(self, args)

class CStorySceneEventExitActor(CStorySceneEventAnimClip):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.duration = None #<property Name="duration" Type="Float" />
        self.actor = None #<property Name="actor" Type="CName" />
        self.blendIn = None #<property Name="blendIn" Type="Float" />
        self.blendOut = None #<property Name="blendOut" Type="Float" />
        self.clipFront = None #<property Name="clipFront" Type="Float" />
        self.clipEnd = None #<property Name="clipEnd" Type="Float" />
        self.stretch = None #<property Name="stretch" Type="Float" />
        self.allowLookatsLevel = None #<property Name="allowLookatsLevel" Type="ELookAtLevel" />
        self.weight = None #<property Name="weight" Type="Float" />
        self.forceAnimationTimeFlag = None #<property Name="forceAnimationTimeFlag" Type="Bool" />
        self.forceAnimationTime = None #<property Name="forceAnimationTime" Type="Float" />
        self.voiceWeightCurve = None #<property Name="voiceWeightCurve" Type="SVoiceWeightCurve" />
        self.allowPoseCorrection = None #<property Name="allowPoseCorrection" Type="Bool" />
        self.behEvent = None #<property Name="behEvent" Type="CName" />
        loadProps(self, args)

class CStorySceneEventFade(CStorySceneEventDuration):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.duration = None #<property Name="duration" Type="Float" />
        self._in = None #<property Name="in" Type="Bool" />
        self.color = None #<property Name="color" Type="Color" />
        loadProps(self, args)

class CStorySceneEventGameplayCamera(CStorySceneEventCamera):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        loadProps(self, args)

class CStorySceneEventGameplayLookAt(CStorySceneEventDuration):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.duration = None #<property Name="duration" Type="Float" />
        self.actor = None #<property Name="actor" Type="CName" />
        self.target = None #<property Name="target" Type="CName" />
        self.enabled = None #<property Name="enabled" Type="Bool" />
        self.instant = None #<property Name="instant" Type="Bool" />
        self.weight = None #<property Name="weight" Type="Float" />
        self.staticPoint = None #<property Name="staticPoint" Type="Vector" />
        self.type = None #<property Name="type" Type="EDialogLookAtType" />
        self.useWeightCurve = None #<property Name="useWeightCurve" Type="Bool" />
        self.weightCurve = None #<property Name="weightCurve" Type="SCurveData" />
        self.behaviorVarWeight = None #<property Name="behaviorVarWeight" Type="CName" />
        self.behaviorVarTarget = None #<property Name="behaviorVarTarget" Type="CName" />
        loadProps(self, args)

class CStorySceneEventGroup(CStorySceneEventDuration):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.duration = None #<property Name="duration" Type="Float" />
        loadProps(self, args)

class CStorySceneEventHideScabbard(CStorySceneEvent):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.setVisible = None #<property Name="setVisible" Type="Bool" />
        self.actorId = None #<property Name="actorId" Type="CName" />
        loadProps(self, args)

class CStorySceneEventHitSound(CStorySceneEvent):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.actor = None #<property Name="actor" Type="CName" />
        self.actorAttacker = None #<property Name="actorAttacker" Type="CName" />
        self.soundAttackType = None #<property Name="soundAttackType" Type="CName" />
        self.actorAttackerWeaponSlot = None #<property Name="actorAttackerWeaponSlot" Type="CName" />
        self.actorAttackerWeaponName = None #<property Name="actorAttackerWeaponName" Type="CName" />
        loadProps(self, args)

class CStorySceneEventInfo():
    def __init__(self, *args):
        self.eventGuid = None #<property Name="eventGuid" Type="CGUID" />
        self.sectionVariantId = None #<property Name="sectionVariantId" Type="Uint32" />
        loadProps(self, args)

class CStorySceneEventLightPropertiesInterpolation(CStorySceneEventInterpolation):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.keyGuids = None #<property Name="keyGuids" Type="array:2,0,CGUID" />
        self.interpolationMethod = None #<property Name="interpolationMethod" Type="EInterpolationMethod" />
        self.easeInStyle = None #<property Name="easeInStyle" Type="EInterpolationEasingStyle" />
        self.easeInParameter = None #<property Name="easeInParameter" Type="Float" />
        self.easeOutStyle = None #<property Name="easeOutStyle" Type="EInterpolationEasingStyle" />
        self.easeOutParameter = None #<property Name="easeOutParameter" Type="Float" />
        self.keys = None #<property Name="keys" Type="array:2,0,CStorySceneEventLightPropertiesInterpolationKey" />
        loadProps(self, args)

class CStorySceneEventLightPropertiesInterpolationKey():
    def __init__(self, *args):
        self.bezierHandles = None #<property Name="bezierHandles" Type="[18]Bezier2dHandle" />
        self.interpolationTypes = None #<property Name="interpolationTypes" Type="[18]Uint32" />
        self.volatile = None #<property Name="volatile" Type="Bool" />
        loadProps(self, args)

class CStorySceneEventLodOverride(CStorySceneEvent):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.actor = None #<property Name="actor" Type="CName" />
        self.actorsByTag = None #<property Name="actorsByTag" Type="TagList" />
        self.forceHighestLod = None #<property Name="forceHighestLod" Type="Bool" />
        self.disableAutoHide = None #<property Name="disableAutoHide" Type="Bool" />
        loadProps(self, args)

class CStorySceneEventLookAt(CStorySceneEvent):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.actor = None #<property Name="actor" Type="CName" />
        self.target = None #<property Name="target" Type="CName" />
        self.enabled = None #<property Name="enabled" Type="Bool" />
        self.type = None #<property Name="type" Type="EDialogLookAtType" />
        self.speed = None #<property Name="speed" Type="Float" />
        self.level = None #<property Name="level" Type="ELookAtLevel" />
        self.range = None #<property Name="range" Type="Float" />
        self.gameplayRange = None #<property Name="gameplayRange" Type="Float" />
        self.limitDeact = None #<property Name="limitDeact" Type="Bool" />
        self.instant = None #<property Name="instant" Type="Bool" />
        self.staticPoint = None #<property Name="staticPoint" Type="Vector" />
        self.headRotationRatio = None #<property Name="headRotationRatio" Type="Float" />
        self.eyesLookAtConvergenceWeight = None #<property Name="eyesLookAtConvergenceWeight" Type="Float" />
        self.eyesLookAtIsAdditive = None #<property Name="eyesLookAtIsAdditive" Type="Bool" />
        self.eyesLookAtDampScale = None #<property Name="eyesLookAtDampScale" Type="Float" />
        self.resetCloth = None #<property Name="resetCloth" Type="EDialogResetClothAndDanglesType" />
        loadProps(self, args)

class CStorySceneEventLookAtDuration(CStorySceneEventDuration):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.duration = None #<property Name="duration" Type="Float" />
        self.actor = None #<property Name="actor" Type="CName" />
        self.bodyTarget = None #<property Name="bodyTarget" Type="CName" />
        self.bodyEnabled = None #<property Name="bodyEnabled" Type="Bool" />
        self.bodyInstant = None #<property Name="bodyInstant" Type="Bool" />
        self.bodyWeight = None #<property Name="bodyWeight" Type="Float" />
        self.bodyStaticPointWS = None #<property Name="bodyStaticPointWS" Type="Vector" />
        self.type = None #<property Name="type" Type="EDialogLookAtType" />
        self.level = None #<property Name="level" Type="ELookAtLevel" />
        self.bodyTransitionWeight = None #<property Name="bodyTransitionWeight" Type="Float" />
        self.usesNewTransition = None #<property Name="usesNewTransition" Type="Bool" />
        self.useTwoTargets = None #<property Name="useTwoTargets" Type="Bool" />
        self.eyesTarget = None #<property Name="eyesTarget" Type="CName" />
        self.eyesEnabled = None #<property Name="eyesEnabled" Type="Bool" />
        self.eyesInstant = None #<property Name="eyesInstant" Type="Bool" />
        self.eyesWeight = None #<property Name="eyesWeight" Type="Float" />
        self.eyesStaticPointWS = None #<property Name="eyesStaticPointWS" Type="Vector" />
        self.eyesLookAtConvergenceWeight = None #<property Name="eyesLookAtConvergenceWeight" Type="Float" />
        self.eyesLookAtIsAdditive = None #<property Name="eyesLookAtIsAdditive" Type="Bool" />
        self.sceneRange = None #<property Name="sceneRange" Type="Float" />
        self.gameplayRange = None #<property Name="gameplayRange" Type="Float" />
        self.limitDeact = None #<property Name="limitDeact" Type="Bool" />
        self.resetCloth = None #<property Name="resetCloth" Type="EDialogResetClothAndDanglesType" />
        self.oldLookAtEyesSpeed = None #<property Name="oldLookAtEyesSpeed" Type="Float" />
        self.oldLookAtEyesDampScale = None #<property Name="oldLookAtEyesDampScale" Type="Float" />
        self.blinkSettings = None #<property Name="blinkSettings" Type="SStorySceneEventLookAtBlinkSettings" />
        loadProps(self, args)

class CStorySceneEventMimicLod(CStorySceneEvent):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.actor = None #<property Name="actor" Type="CName" />
        self.setMimicOn = None #<property Name="setMimicOn" Type="Bool" />
        loadProps(self, args)

class CStorySceneEventMimics(CStorySceneEventAnimClip):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.duration = None #<property Name="duration" Type="Float" />
        self.actor = None #<property Name="actor" Type="CName" />
        self.blendIn = None #<property Name="blendIn" Type="Float" />
        self.blendOut = None #<property Name="blendOut" Type="Float" />
        self.clipFront = None #<property Name="clipFront" Type="Float" />
        self.clipEnd = None #<property Name="clipEnd" Type="Float" />
        self.stretch = None #<property Name="stretch" Type="Float" />
        self.allowLookatsLevel = None #<property Name="allowLookatsLevel" Type="ELookAtLevel" />
        self.weight = None #<property Name="weight" Type="Float" />
        self.forceAnimationTimeFlag = None #<property Name="forceAnimationTimeFlag" Type="Bool" />
        self.forceAnimationTime = None #<property Name="forceAnimationTime" Type="Float" />
        self.voiceWeightCurve = None #<property Name="voiceWeightCurve" Type="SVoiceWeightCurve" />
        self.allowPoseCorrection = None #<property Name="allowPoseCorrection" Type="Bool" />
        self.stateName = None #<property Name="stateName" Type="CName" />
        self.mimicsEmotionalState = None #<property Name="mimicsEmotionalState" Type="CName" />
        self.mimicsLayer_Eyes = None #<property Name="mimicsLayer_Eyes" Type="CName" />
        self.mimicsLayer_Pose = None #<property Name="mimicsLayer_Pose" Type="CName" />
        self.mimicsLayer_Animation = None #<property Name="mimicsLayer_Animation" Type="CName" />
        self.mimicsPoseWeight = None #<property Name="mimicsPoseWeight" Type="Float" />
        self.transitionAnimation = None #<property Name="transitionAnimation" Type="CName" />
        self.forceMimicsIdleAnimation_Eyes = None #<property Name="forceMimicsIdleAnimation_Eyes" Type="CName" />
        self.forceMimicsIdleAnimation_Pose = None #<property Name="forceMimicsIdleAnimation_Pose" Type="CName" />
        self.forceMimicsIdleAnimation_Animation = None #<property Name="forceMimicsIdleAnimation_Animation" Type="CName" />
        self.useWeightCurve = None #<property Name="useWeightCurve" Type="Bool" />
        self.weightCurve = None #<property Name="weightCurve" Type="SCurveData" />
        loadProps(self, args)

class CStorySceneEventMimicsAnim(CStorySceneEventAnimClip):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.duration = None #<property Name="duration" Type="Float" />
        self.actor = None #<property Name="actor" Type="CName" />
        self.blendIn = None #<property Name="blendIn" Type="Float" />
        self.blendOut = None #<property Name="blendOut" Type="Float" />
        self.clipFront = None #<property Name="clipFront" Type="Float" />
        self.clipEnd = None #<property Name="clipEnd" Type="Float" />
        self.stretch = None #<property Name="stretch" Type="Float" />
        self.allowLookatsLevel = None #<property Name="allowLookatsLevel" Type="ELookAtLevel" />
        self.weight = None #<property Name="weight" Type="Float" />
        self.forceAnimationTimeFlag = None #<property Name="forceAnimationTimeFlag" Type="Bool" />
        self.forceAnimationTime = None #<property Name="forceAnimationTime" Type="Float" />
        self.voiceWeightCurve = None #<property Name="voiceWeightCurve" Type="SVoiceWeightCurve" />
        self.allowPoseCorrection = None #<property Name="allowPoseCorrection" Type="Bool" />
        self.animationName = None #<property Name="animationName" Type="CName" />
        self.fullEyesWeight = None #<property Name="fullEyesWeight" Type="Bool" />
        loadProps(self, args)

class CStorySceneEventMimicsFilter(CStorySceneEventDuration):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.duration = None #<property Name="duration" Type="Float" />
        self.actor = None #<property Name="actor" Type="CName" />
        self.filterName = None #<property Name="filterName" Type="CName" />
        self.weight = None #<property Name="weight" Type="Float" />
        self.useWeightCurve = None #<property Name="useWeightCurve" Type="Bool" />
        self.weightCurve = None #<property Name="weightCurve" Type="SCurveData" />
        loadProps(self, args)

class CStorySceneEventMimicsPose(CStorySceneEventDuration):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.duration = None #<property Name="duration" Type="Float" />
        self.actor = None #<property Name="actor" Type="CName" />
        self.poseName = None #<property Name="poseName" Type="CName" />
        self.weight = None #<property Name="weight" Type="Float" />
        self.useWeightCurve = None #<property Name="useWeightCurve" Type="Bool" />
        self.weightCurve = None #<property Name="weightCurve" Type="SCurveData" />
        loadProps(self, args)

class CStorySceneEventModifyEnv(CStorySceneEvent):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.environmentDefinition = None #<property Name="environmentDefinition" Type="handle:CEnvironmentDefinition" />
        self.activate = None #<property Name="activate" Type="Bool" />
        self.priority = None #<property Name="priority" Type="Int32" />
        self.blendFactor = None #<property Name="blendFactor" Type="Float" />
        self.blendInTime = None #<property Name="blendInTime" Type="Float" />
        loadProps(self, args)

class CStorySceneEventMorphInterpolation(CStorySceneEventInterpolation):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.keyGuids = None #<property Name="keyGuids" Type="array:2,0,CGUID" />
        self.interpolationMethod = None #<property Name="interpolationMethod" Type="EInterpolationMethod" />
        self.easeInStyle = None #<property Name="easeInStyle" Type="EInterpolationEasingStyle" />
        self.easeInParameter = None #<property Name="easeInParameter" Type="Float" />
        self.easeOutStyle = None #<property Name="easeOutStyle" Type="EInterpolationEasingStyle" />
        self.easeOutParameter = None #<property Name="easeOutParameter" Type="Float" />
        self.keys = None #<property Name="keys" Type="array:2,0,CStorySceneEventMorphInterpolationKey" />
        loadProps(self, args)

class CStorySceneEventMorphInterpolationKey():
    def __init__(self, *args):
        self.bezierHandles = None #<property Name="bezierHandles" Type="[1]Bezier2dHandle" />
        self.interpolationTypes = None #<property Name="interpolationTypes" Type="[1]Uint32" />
        self.volatile = None #<property Name="volatile" Type="Bool" />
        loadProps(self, args)

class CStorySceneEventOpenDoor(CStorySceneEvent):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.doorTag = None #<property Name="doorTag" Type="CName" />
        self.instant = None #<property Name="instant" Type="Bool" />
        self.openClose = None #<property Name="openClose" Type="Bool" />
        self.flipDirection = None #<property Name="flipDirection" Type="Bool" />
        loadProps(self, args)

class CStorySceneEventOverrideAnimation(CStorySceneEventAnimation):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.duration = None #<property Name="duration" Type="Float" />
        self.actor = None #<property Name="actor" Type="CName" />
        self.blendIn = None #<property Name="blendIn" Type="Float" />
        self.blendOut = None #<property Name="blendOut" Type="Float" />
        self.clipFront = None #<property Name="clipFront" Type="Float" />
        self.clipEnd = None #<property Name="clipEnd" Type="Float" />
        self.stretch = None #<property Name="stretch" Type="Float" />
        self.allowLookatsLevel = None #<property Name="allowLookatsLevel" Type="ELookAtLevel" />
        self.weight = None #<property Name="weight" Type="Float" />
        self.forceAnimationTimeFlag = None #<property Name="forceAnimationTimeFlag" Type="Bool" />
        self.forceAnimationTime = None #<property Name="forceAnimationTime" Type="Float" />
        self.voiceWeightCurve = None #<property Name="voiceWeightCurve" Type="SVoiceWeightCurve" />
        self.allowPoseCorrection = None #<property Name="allowPoseCorrection" Type="Bool" />
        self.animationName = None #<property Name="animationName" Type="CName" />
        self.useMotionExtraction = None #<property Name="useMotionExtraction" Type="Bool" />
        self.useFakeMotion = None #<property Name="useFakeMotion" Type="Bool" />
        self.gatherSyncTokens = None #<property Name="gatherSyncTokens" Type="Bool" />
        self.muteSoundEvents = None #<property Name="muteSoundEvents" Type="Bool" />
        self.disableLookAt = None #<property Name="disableLookAt" Type="Bool" />
        self.disableLookAtSpeed = None #<property Name="disableLookAtSpeed" Type="Float" />
        self.useLowerBodyPartsForLookAt = None #<property Name="useLowerBodyPartsForLookAt" Type="Bool" />
        self.bonesIdx = None #<property Name="bonesIdx" Type="array:2,0,Int32" />
        self.bonesWeight = None #<property Name="bonesWeight" Type="array:2,0,Float" />
        self.animationType = None #<property Name="animationType" Type="EStorySceneAnimationType" />
        self.addConvertToAdditive = None #<property Name="addConvertToAdditive" Type="Bool" />
        self.addAdditiveType = None #<property Name="addAdditiveType" Type="EAdditiveType" />
        self.useWeightCurve = None #<property Name="useWeightCurve" Type="Bool" />
        self.weightCurve = None #<property Name="weightCurve" Type="SCurveData" />
        self.weightCurveChanged = None #<property Name="weightCurveChanged" Type="Bool" />
        self.supportsMotionExClipFront = None #<property Name="supportsMotionExClipFront" Type="Bool" />
        self.fakeProp = None #<property Name="fakeProp" Type="Bool" />
        loadProps(self, args)

class CStorySceneEventOverridePlacement(CStorySceneEvent):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.actorName = None #<property Name="actorName" Type="CName" />
        self.placement = None #<property Name="placement" Type="EngineTransform" />
        self.resetCloth = None #<property Name="resetCloth" Type="EDialogResetClothAndDanglesType" />
        loadProps(self, args)

class CStorySceneEventOverridePlacementDuration(CStorySceneEventCurveAnimation):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.duration = None #<property Name="duration" Type="Float" />
        self.curve = None #<property Name="curve" Type="SMultiCurve" />
        self.actorName = None #<property Name="actorName" Type="CName" />
        loadProps(self, args)

class CStorySceneEventPlacementInterpolation(CStorySceneEventInterpolation):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.keyGuids = None #<property Name="keyGuids" Type="array:2,0,CGUID" />
        self.interpolationMethod = None #<property Name="interpolationMethod" Type="EInterpolationMethod" />
        self.easeInStyle = None #<property Name="easeInStyle" Type="EInterpolationEasingStyle" />
        self.easeInParameter = None #<property Name="easeInParameter" Type="Float" />
        self.easeOutStyle = None #<property Name="easeOutStyle" Type="EInterpolationEasingStyle" />
        self.easeOutParameter = None #<property Name="easeOutParameter" Type="Float" />
        self.keys = None #<property Name="keys" Type="array:2,0,CStorySceneEventPlacementInterpolationKey" />
        loadProps(self, args)

class CStorySceneEventPlacementInterpolationKey():
    def __init__(self, *args):
        self.bezierHandles = None #<property Name="bezierHandles" Type="[6]Bezier2dHandle" />
        self.interpolationTypes = None #<property Name="interpolationTypes" Type="[6]Uint32" />
        self.volatile = None #<property Name="volatile" Type="Bool" />
        loadProps(self, args)

class CStorySceneEventPoseKey(CStorySceneEventDuration):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.duration = None #<property Name="duration" Type="Float" />
        self.actor = None #<property Name="actor" Type="CName" />
        self.blendIn = None #<property Name="blendIn" Type="Float" />
        self.blendOut = None #<property Name="blendOut" Type="Float" />
        self.weightBlendType = None #<property Name="weightBlendType" Type="EInterpolationType" />
        self.weight = None #<property Name="weight" Type="Float" />
        self.useWeightCurve = None #<property Name="useWeightCurve" Type="Bool" />
        self.weightCurve = None #<property Name="weightCurve" Type="SCurveData" />
        self.linkToDialogset = None #<property Name="linkToDialogset" Type="Bool" />
        self.version = None #<property Name="version" Type="Int32" />
        self.cachedBones = None #<property Name="cachedBones" Type="array:2,0,Int32" />
        self.cachedTransforms = None #<property Name="cachedTransforms" Type="array:133,0,EngineQsTransform" />
        self.cachedTracks = None #<property Name="cachedTracks" Type="array:2,0,Int32" />
        self.cachedTracksValues = None #<property Name="cachedTracksValues" Type="array:2,0,Float" />
        loadProps(self, args)

class CStorySceneEventPropPlacementInterpolation(CStorySceneEventInterpolation):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.keyGuids = None #<property Name="keyGuids" Type="array:2,0,CGUID" />
        self.interpolationMethod = None #<property Name="interpolationMethod" Type="EInterpolationMethod" />
        self.easeInStyle = None #<property Name="easeInStyle" Type="EInterpolationEasingStyle" />
        self.easeInParameter = None #<property Name="easeInParameter" Type="Float" />
        self.easeOutStyle = None #<property Name="easeOutStyle" Type="EInterpolationEasingStyle" />
        self.easeOutParameter = None #<property Name="easeOutParameter" Type="Float" />
        self.keys = None #<property Name="keys" Type="array:2,0,CStorySceneEventPropPlacementInterpolationKey" />
        loadProps(self, args)

class CStorySceneEventPropPlacementInterpolationKey():
    def __init__(self, *args):
        self.bezierHandles = None #<property Name="bezierHandles" Type="[6]Bezier2dHandle" />
        self.interpolationTypes = None #<property Name="interpolationTypes" Type="[6]Uint32" />
        self.volatile = None #<property Name="volatile" Type="Bool" />
        loadProps(self, args)

class CStorySceneEventPropVisibility(CStorySceneEvent):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.propID = None #<property Name="propID" Type="CName" />
        self.showHideFlag = None #<property Name="showHideFlag" Type="Bool" />
        loadProps(self, args)

class CStorySceneEventReward(CStorySceneEvent):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.npcTag = None #<property Name="npcTag" Type="CName" />
        self.itemName = None #<property Name="itemName" Type="CName" />
        self.rewardName = None #<property Name="rewardName" Type="CName" />
        self.quantity = None #<property Name="quantity" Type="Int32" />
        self.dontInformGui = None #<property Name="dontInformGui" Type="Bool" />
        loadProps(self, args)

class CStorySceneEventRotate(CStorySceneEvent):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.actor = None #<property Name="actor" Type="CName" />
        self.angle = None #<property Name="angle" Type="Float" />
        self.absoluteAngle = None #<property Name="absoluteAngle" Type="Bool" />
        self.toCamera = None #<property Name="toCamera" Type="Bool" />
        self.instant = None #<property Name="instant" Type="Bool" />
        loadProps(self, args)

class CStorySceneEventScenePropPlacement(CStorySceneEvent):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.propId = None #<property Name="propId" Type="CName" />
        self.placement = None #<property Name="placement" Type="EngineTransform" />
        self.showHide = None #<property Name="showHide" Type="Bool" />
        self.rotationCyclesPitch = None #<property Name="rotationCyclesPitch" Type="Uint32" />
        self.rotationCyclesRoll = None #<property Name="rotationCyclesRoll" Type="Uint32" />
        self.rotationCyclesYaw = None #<property Name="rotationCyclesYaw" Type="Uint32" />
        loadProps(self, args)

class CStorySceneEventSetupItemForSync(CStorySceneEvent):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.itemName = None #<property Name="itemName" Type="CName" />
        self.activate = None #<property Name="activate" Type="Bool" />
        self.actorToSyncTo = None #<property Name="actorToSyncTo" Type="CName" />
        loadProps(self, args)

class CStorySceneEventSound(CStorySceneEvent):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.soundEventName = None #<property Name="soundEventName" Type="StringAnsi" />
        self.actor = None #<property Name="actor" Type="CName" />
        self.bone = None #<property Name="bone" Type="CName" />
        self.dbVolume = None #<property Name="dbVolume" Type="Float" />
        loadProps(self, args)

class CStorySceneEventStartBlendToGameplayCamera(CStorySceneEventCustomCamera):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.cameraTranslation = None #<property Name="cameraTranslation" Type="Vector" />
        self.cameraRotation = None #<property Name="cameraRotation" Type="EulerAngles" />
        self.cameraZoom = None #<property Name="cameraZoom" Type="Float" />
        self.cameraFov = None #<property Name="cameraFov" Type="Float" />
        self.dofFocusDistFar = None #<property Name="dofFocusDistFar" Type="Float" />
        self.dofBlurDistFar = None #<property Name="dofBlurDistFar" Type="Float" />
        self.dofIntensity = None #<property Name="dofIntensity" Type="Float" />
        self.dofFocusDistNear = None #<property Name="dofFocusDistNear" Type="Float" />
        self.dofBlurDistNear = None #<property Name="dofBlurDistNear" Type="Float" />
        self.cameraDefinition = None #<property Name="cameraDefinition" Type="StorySceneCameraDefinition" />
        self.blendTime = None #<property Name="blendTime" Type="Float" />
        self.changesCamera = None #<property Name="changesCamera" Type="Bool" />
        self.lightsBlendTime = None #<property Name="lightsBlendTime" Type="Float" />
        loadProps(self, args)

class CStorySceneEventSurfaceEffect(CStorySceneEvent):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.type = None #<property Name="type" Type="ESceneEventSurfacePostFXType" />
        self.position = None #<property Name="position" Type="Vector" />
        self.fadeInTime = None #<property Name="fadeInTime" Type="Float" />
        self.fadeOutTime = None #<property Name="fadeOutTime" Type="Float" />
        self.durationTime = None #<property Name="durationTime" Type="Float" />
        self.radius = None #<property Name="radius" Type="Float" />
        loadProps(self, args)

class CStorySceneEventTimelapse(CStorySceneEvent):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.enable = None #<property Name="enable" Type="Bool" />
        loadProps(self, args)

class CStorySceneEventUseHiresShadows(CStorySceneEvent):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.actor = None #<property Name="actor" Type="CName" />
        self.useHiresShadows = None #<property Name="useHiresShadows" Type="Bool" />
        loadProps(self, args)

class CStorySceneEventVideoOverlay(CStorySceneEvent):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.fileName = None #<property Name="fileName" Type="String" />
        loadProps(self, args)

class CStorySceneEventVisibility(CStorySceneEvent):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.actor = None #<property Name="actor" Type="CName" />
        self.showHideFlag = None #<property Name="showHideFlag" Type="Bool" />
        loadProps(self, args)

class CStorySceneEventWalk(CStorySceneEventCurveAnimation):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.duration = None #<property Name="duration" Type="Float" />
        self.curve = None #<property Name="curve" Type="SMultiCurve" />
        self.actorName = None #<property Name="actorName" Type="CName" />
        self.animationStartName = None #<property Name="animationStartName" Type="CName" />
        self.animationLoopName = None #<property Name="animationLoopName" Type="CName" />
        self.animationStopName = None #<property Name="animationStopName" Type="CName" />
        loadProps(self, args)

class CStorySceneEventWeatherChange(CStorySceneEvent):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.weatherName = None #<property Name="weatherName" Type="CName" />
        self.blendTime = None #<property Name="blendTime" Type="Float" />
        loadProps(self, args)

class CStorySceneEventWorldEntityEffect(CStorySceneEvent):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.entityTag = None #<property Name="entityTag" Type="CName" />
        self.effectName = None #<property Name="effectName" Type="CName" />
        self.startStop = None #<property Name="startStop" Type="Bool" />
        loadProps(self, args)

class CStorySceneEventWorldPropPlacement(CStorySceneEvent):
    def __init__(self, *args):
        self.eventName = None #<property Name="eventName" Type="String" />
        self.startPosition = None #<property Name="startPosition" Type="Float" />
        self.isMuted = None #<property Name="isMuted" Type="Bool" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.sceneElement = None #<property Name="sceneElement" Type="ptr:CStorySceneElement" />
        self.GUID = None #<property Name="GUID" Type="CGUID" />
        self.interpolationEventGUID = None #<property Name="interpolationEventGUID" Type="CGUID" />
        self.blendParentGUID = None #<property Name="blendParentGUID" Type="CGUID" />
        self.linkParentGUID = None #<property Name="linkParentGUID" Type="CGUID" />
        self.linkParentTimeOffset = None #<property Name="linkParentTimeOffset" Type="Float" />
        self.propId = None #<property Name="propId" Type="CName" />
        self.placement = None #<property Name="placement" Type="EngineTransform" />
        self.showHide = None #<property Name="showHide" Type="Bool" />

################
#  EVENTS END  #
#              #
################

class CStorySceneSection(base_w3):
    def __init__(self, *args):
        self.linkedElements = None #<property Name="linkedElements" Type="array:2,0,ptr:CStorySceneLinkElement" />
        self.nextLinkElement = None #<property Name="nextLinkElement" Type="ptr:CStorySceneLinkElement" />
        self.comment = None #<property Name="comment" Type="String" />
        self.contexID = None #<property Name="contexID" Type="Int32" />
        self.nextVariantId = None #<property Name="nextVariantId" Type="Uint32" />
        self.defaultVariantId = None #<property Name="defaultVariantId" Type="Uint32" />
        self.variants = None #<property Name="variants" Type="array:2,0,ptr:CStorySceneSectionVariant" />
        self.localeVariantMappings = None #<property Name="localeVariantMappings" Type="array:2,0,ptr:CStorySceneLocaleVariantMapping" />
        self.sceneElements = None #<property Name="sceneElements" Type="array:2,0,ptr:CStorySceneElement" />
        self.events = None #<property Name="events" Type="array:2,0,ptr:CStorySceneEvent" />
        self.eventsInfo = None #<property Name="eventsInfo" Type="array:2,0,ptr:CStorySceneEventInfo" />
        self.choice = None #<property Name="choice" Type="ptr:CStorySceneChoice" />
        self.sectionId = None #<property Name="sectionId" Type="Uint32" />
        self.sectionName = None #<property Name="sectionName" Type="String" />
        self.tags = None #<property Name="tags" Type="TagList" />
        self.interceptRadius = None #<property Name="interceptRadius" Type="Float" />
        self.interceptTimeout = None #<property Name="interceptTimeout" Type="Float" />
        self.interceptSections = None #<property Name="interceptSections" Type="array:2,0,ptr:CStorySceneSection" />
        self.isGameplay = None #<property Name="isGameplay" Type="Bool" />
        self.isImportant = None #<property Name="isImportant" Type="Bool" />
        self.allowCameraMovement = None #<property Name="allowCameraMovement" Type="Bool" />
        self.hasCinematicOneliners = None #<property Name="hasCinematicOneliners" Type="Bool" />
        self.manualFadeIn = None #<property Name="manualFadeIn" Type="Bool" />
        self.fadeInAtBeginning = None #<property Name="fadeInAtBeginning" Type="Bool" />
        self.fadeOutAtEnd = None #<property Name="fadeOutAtEnd" Type="Bool" />
        self.pauseInCombat = None #<property Name="pauseInCombat" Type="Bool" />
        self.canBeSkipped = None #<property Name="canBeSkipped" Type="Bool" />
        self.canHaveLookats = None #<property Name="canHaveLookats" Type="Bool" />
        self.numberOfInputPaths = None #<property Name="numberOfInputPaths" Type="Uint32" />
        self.dialogsetChangeTo = None #<property Name="dialogsetChangeTo" Type="CName" />
        self.forceDialogset = None #<property Name="forceDialogset" Type="Bool" />
        self.inputPathsElements = None #<property Name="inputPathsElements" Type="array:2,0,ptr:CStorySceneLinkElement" />
        self.streamingLock = None #<property Name="streamingLock" Type="Bool" />
        self.streamingAreaTag = None #<property Name="streamingAreaTag" Type="CName" />
        self.streamingUseCameraPosition = None #<property Name="streamingUseCameraPosition" Type="Bool" />
        self.streamingCameraAllowedJumpDistance = None #<property Name="streamingCameraAllowedJumpDistance" Type="Float" />
        self.blockMusicTriggers = None #<property Name="blockMusicTriggers" Type="Bool" />
        self.soundListenerOverride = None #<property Name="soundListenerOverride" Type="String" />
        self.soundEventsOnEnd = None #<property Name="soundEventsOnEnd" Type="array:2,0,CName" />
        self.soundEventsOnSkip = None #<property Name="soundEventsOnSkip" Type="array:2,0,CName" />
        self.maxBoxExtentsToApplyHiResShadows = None #<property Name="maxBoxExtentsToApplyHiResShadows" Type="Float" />
        self.distantLightStartOverride = None #<property Name="distantLightStartOverride" Type="Float" />
  
        sceneEventElements = []
        for sceneEventElement in args[0].sceneEventElements.elements:
            sceneEventElement = sceneEventElement.PROP
            if sceneEventElement.theType in elementTypes:
                cls = getattr(sys.modules[__name__], sceneEventElement.theType)
                sceneEventElement = cls(sceneEventElement)
            else:
                raise Exception('Unknown Event Type')
            sceneEventElements.append(sceneEventElement)
        
        self.sceneEventElements = sceneEventElements
        loadProps(self, args)


class CStorySceneDialogsetSlot(base_w3):
    def __init__(self, *args):
        self.slotNumber = None #<property Name="slotNumber" Type="Uint32" />
        self.slotName = None #<property Name="slotName" Type="CName" />
        self.slotPlacement = None #<property Name="slotPlacement" Type="EngineTransform" />
        self.actorName = None #<property Name="actorName" Type="CName" />
        self.actorVisibility = None #<property Name="actorVisibility" Type="Bool" />
        self.actorStatus = None #<property Name="actorStatus" Type="CName" />
        self.actorEmotionalState = None #<property Name="actorEmotionalState" Type="CName" />
        self.actorPoseName = None #<property Name="actorPoseName" Type="CName" />
        self.actorMimicsEmotionalState = None #<property Name="actorMimicsEmotionalState" Type="CName" />
        self.actorMimicsLayer_Eyes = None #<property Name="actorMimicsLayer_Eyes" Type="CName" />
        self.actorMimicsLayer_Pose = None #<property Name="actorMimicsLayer_Pose" Type="CName" />
        self.actorMimicsLayer_Animation = None #<property Name="actorMimicsLayer_Animation" Type="CName" />
        self.actorMimicsLayer_Pose_Weight = None #<property Name="actorMimicsLayer_Pose_Weight" Type="Float" />
        self.forceBodyIdleAnimation = None #<property Name="forceBodyIdleAnimation" Type="CName" />
        self.forceBodyIdleAnimationWeight = None #<property Name="forceBodyIdleAnimationWeight" Type="Float" />
        self.actorState = None #<property Name="actorState" Type="CName" />
        self.ID = None #<property Name="ID" Type="CGUID" />
        self.setupAction = None #<property Name="setupAction" Type="array:2,0,ptr:CStorySceneAction" />
        loadProps(self, args)


class CStorySceneDialogsetInstance(base_w3):
    def __init__(self, *args):
        self.name = None #<property Name="name" Type="CName" />
        self.slots = None #<property Name="slots" Type="array:2,0,ptr:CStorySceneDialogsetSlot" />
        self.placementTag = None #<property Name="placementTag" Type="TagList" />
        self.snapToTerrain = None #<property Name="snapToTerrain" Type="Bool" />
        self.findSafePlacement = None #<property Name="findSafePlacement" Type="Bool" />
        self.safePlacementRadius = None #<property Name="safePlacementRadius" Type="Float" />
        self.areCamerasUsedForBoundsCalculation = None #<property Name="areCamerasUsedForBoundsCalculation" Type="Bool" />
        self.path = None #<property Name="path" Type="String" />
        
        loadProps(self, args)

class StorySceneCameraDefinition(base_w3):
    def __init__(self, *args):
        
        self.cameraName = None #<property Name="cameraName" Type="CName" />
        self.cameraTransform = None #<property Name="cameraTransform" Type="EngineTransform" />
        self.cameraZoom = None #<property Name="cameraZoom" Type="Float" />
        self.cameraFov = None #<property Name="cameraFov" Type="Float" />
        self.enableCameraNoise = None #<property Name="enableCameraNoise" Type="Bool" />
        self.dofFocusDistFar = None #<property Name="dofFocusDistFar" Type="Float" />
        self.dofBlurDistFar = None #<property Name="dofBlurDistFar" Type="Float" />
        self.dofIntensity = None #<property Name="dofIntensity" Type="Float" />
        self.dofFocusDistNear = None #<property Name="dofFocusDistNear" Type="Float" />
        self.dofBlurDistNear = None #<property Name="dofBlurDistNear" Type="Float" />
        self.sourceSlotName = None #<property Name="sourceSlotName" Type="CName" />
        self.targetSlotName = None #<property Name="targetSlotName" Type="CName" />
        self.sourceEyesHeigth = None #<property Name="sourceEyesHeigth" Type="Float" />
        self.targetEyesLS = None #<property Name="targetEyesLS" Type="Vector" />
        self.dof = None #<property Name="dof" Type="ApertureDofParams" />
        self.bokehDofParams = None #<property Name="bokehDofParams" Type="SBokehDofParams" />
        self.genParam = None #<property Name="genParam" Type="CEventGeneratorCameraParams" />
        self.cameraAdjustVersion = None #<property Name="cameraAdjustVersion" Type="Uint8" />
        
        loadProps(self, args)
    

class CStorySceneActor(base_w3):
    def __init__(self, *args):
        self.id = None# <property Name="id" Type="CName" />
        self.actorTags = None# <property Name="actorTags" Type="TagList" />
        self.entityTemplate = None# <property Name="entityTemplate" Type="soft:CEntityTemplate" />
        self.appearanceFilter = None# <property Name="appearanceFilter" Type="array:2,0,CName" />
        self.dontSearchByVoicetag = None# <property Name="dontSearchByVoicetag" Type="Bool" />
        self.useHiresShadows = None# <property Name="useHiresShadows" Type="Bool" />
        self.forceSpawn = None# <property Name="forceSpawn" Type="Bool" />
        self.useMimic = None# <property Name="useMimic" Type="Bool" />
        self.alias = None# <property Name="alias" Type="String" />

        loadProps(self, args)

def loadPropsJSON(self, args):
    for arg in args[0]:
        if arg['Type'] == 'String' or arg['Type'] == 'Bool':
            setattr(self, arg['Name'], arg['val'])
        elif arg['Type'] == 'CName' or arg['Type'] == 'ECutsceneActorType':
            setattr(self, arg['Name'], arg['Value'])
        elif arg['Type'] == 'soft:CEntityTemplate':
            setattr(self, arg['Name'], arg['DepotPath'])
        else:
            setattr(self, arg['Name'], arg)

class SCutsceneActorDef(base_w3):
    def __init__(self, isJson = True, *args):
        self.name = None # <property Name="name" Type="String" />
        self.tag = None # <property Name="tag" Type="TagList" />
        self.voiceTag = None # <property Name="voiceTag" Type="CName" />
        self.template = None # <property Name="template" Type="soft:CEntityTemplate" />
        self.appearance = None # <property Name="appearance" Type="CName" />
        self.type = None # <property Name="type" Type="ECutsceneActorType" />
        self.finalPosition = None # <property Name="finalPosition" Type="TagList" />
        self.killMe = None # <property Name="killMe" Type="Bool" />
        self.useMimic = None # <property Name="useMimic" Type="Bool" />
        self.animationAtFinalPosition = None # <property Name="animationAtFinalPosition" Type="CName" />
        
        loadPropsJSON(self, args) if isJson else loadProps(self, args)
            

        
    @classmethod
    def from_json(cls, data):
        if data['Type'] == 'SCutsceneActorDef':
            return cls(True, data['Content'])
        else:
            raise Exception('bad json SCutsceneActorDef')

class CSkeletalAnimationSet(base_w3):
    def __init__(self, animations=[]):
        self.animations:List[CSkeletalAnimationSetEntry] = animations
    @classmethod
    def from_json(cls, data):
        animations = list(map(CSkeletalAnimationSetEntry.from_json, data["animations"]))
        return cls(animations)


class CStoryScene(base_w3): # CResource
    def __init__(self):
        self.controlParts = None #" Type="array:2,0,ptr:CStorySceneControlPart" />
        self.sections = None #" Type="array:2,0,ptr:CStorySceneSection" />
        self.elementIDCounter = None #" Type="Uint32" />
        self.sectionIDCounter = None #" Type="Uint32" />
        self.sceneId = None #" Type="Uint32" />
        self.sceneTemplates = None #" Type="array:2,0,ptr:CStorySceneActor" />
        self.sceneProps = None #" Type="array:2,0,ptr:CStorySceneProp" />
        self.sceneEffects = None #" Type="array:2,0,ptr:CStorySceneEffect" />
        self.sceneLights = None #" Type="array:2,0,ptr:CStorySceneLight" />
        self.mayActorsStartWorking = None #" Type="Bool" />
        self.surpassWaterRendering = None #" Type="Bool" />
        self.dialogsetInstances = None #" Type="array:2,0,ptr:CStorySceneDialogsetInstance" />
        self.cameraDefinitions = None #" Type="array:2,0,StorySceneCameraDefinition" />
        self.banksDependency = None #" Type="array:2,0,CName" />
        self.blockMusicTriggers = None #" Type="Bool" />
        self.muteSpeechUnderWater = None #" Type="Bool" />
        self.soundListenerOverride = None #" Type="String" />
        self.soundEventsOnEnd = None #" Type="array:2,0,CName" />
        self.soundEventsOnSkip = None #" Type="array:2,0,CName" />
        
        self.chunksRef = None
        self.LocalizedStringsRef = None
    @classmethod
    def from_json(cls, data):
        return cls(data)

class CCutsceneTemplate(base_w3):
    def __init__(self, animations=[], SCutsceneActorDefs=[], *args, **kwargs):

        self.animevents = []
        self.effects = []
        if kwargs or args:
            for arg in args[0].items():
                if hasattr(self, arg[0]):
                    setattr(self, arg[0], arg[1])
        self.animations = animations
        self.SCutsceneActorDefs = SCutsceneActorDefs


        # <property Name="requiredSfxTag" Type="CName" />
        # <property Name="animations" Type="array:2,0,ptr:CSkeletalAnimationSetEntry" />
        # <property Name="extAnimEvents" Type="array:2,0,handle:CExtAnimEventsFile" />
        # <property Name="skeleton" Type="handle:CSkeleton" />
        # <property Name="compressedPoses" Type="array:2,0,ptr:ICompressedPose" />
        # <property Name="Streaming option" Type="SAnimationBufferStreamingOption" />
        # <property Name="Number of non-streamable bones" Type="Uint32" />
        # <property Name="modifiers" Type="array:2,0,ptr:ICutsceneModifier" />
        # <property Name="point" Type="TagList" />
        # <property Name="lastLevelLoaded" Type="String" />
        # <property Name="actorsDef" Type="array:2,0,SCutsceneActorDef" />
        # <property Name="isValid" Type="Bool" />
        # <property Name="fadeBefore" Type="Float" />
        # <property Name="fadeAfter" Type="Float" />
        # <property Name="cameraBlendInTime" Type="Float" />
        # <property Name="cameraBlendOutTime" Type="Float" />
        # <property Name="blackscreenWhenLoading" Type="Bool" />
        # <property Name="checkActorsPosition" Type="Bool" />
        # <property Name="entToHideTags" Type="array:2,0,CName" />
        # <property Name="usedInFiles" Type="array:2,0,String" />
        # <property Name="resourcesToPreloadManuallyPaths" Type="array:2,0,String" />
        # <property Name="reverbName" Type="String" />
        # <property Name="burnedAudioTrackName" Type="StringAnsi" />
        # <property Name="banksDependency" Type="array:2,0,CName" />
        # <property Name="streamable" Type="Bool" />
        # <property Name="effects" Type="array:2,0,ptr:CFXDefinition" />


    @classmethod
    def from_json(cls, data):
        SCutsceneActorDefs = list(map(SCutsceneActorDef.from_json, data["SCutsceneActorDefs"]['Content']))
        animations = list(map(CSkeletalAnimationSetEntry.from_json, data["animations"]))

        return cls(animations, SCutsceneActorDefs, data)

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
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    #################

# OTHER STORYSCENE CLASSES

##############

from .Types.CMesh import CResource


class IReferencable:
    pass
class ISerializable(IReferencable):
    pass
class IScriptable(ISerializable):
    pass
class CObject(IScriptable):
    pass
class IStorySceneItem(CObject):
    pass
class IStorySceneChoiceLineAction(CObject):
    pass

class IGameSystem(CObject):
    pass

class CGraphSocket(ISerializable):
    def __init__(self, *args):
        self.block = None #ptr:CGraphBlock"
        self.name = None #CName"
        self.connections = None #array:2,0,ptr:CGraphConnection"
        loadProps(self, args)

class CGraphBlock(CObject):
    def __init__(self, *args):
        self.sockets = None # array:2,0,ptr:CGraphSocket"
        loadProps(self, args)

class CNode(CObject):
    def __init__(self, *args):
        self.tags = None #TagList"
        self.transform = None #EngineTransform"
        self.transformParent = None #ptr:CHardAttachment"
        self.guid = None #CGUID"
        loadProps(self, args)

class CComponent(CNode):
    def __init__(self, *args):
        self.tags = None #TagList"
        self.transform = None #EngineTransform"
        self.transformParent = None #ptr:CHardAttachment"
        self.guid = None #CGUID"
        self.name = None #String"
        self.isStreamed = None #Bool"
        loadProps(self, args)

class CSpriteComponent(CComponent):
    def __init__(self, *args):
        self.tags = None #TagList"
        self.transform = None #EngineTransform"
        self.transformParent = None #ptr:CHardAttachment"
        self.guid = None #CGUID"
        self.name = None #String"
        self.isStreamed = None #Bool"
        self.isVisible = None #Bool"
        self.icon = None #handle:CBitmapTexture"
        loadProps(self, args)

# class CStoryScene(CResource):
#     def __init__(self, *args):
#         self.controlParts = None #array:2,0,ptr:CStorySceneControlPart"
#         self.sections = None #array:2,0,ptr:CStorySceneSection"
#         self.elementIDCounter = None #Uint32"
#         self.sectionIDCounter = None #Uint32"
#         self.sceneId = None #Uint32"
#         self.sceneTemplates = None #array:2,0,ptr:CStorySceneActor"
#         self.sceneProps = None #array:2,0,ptr:CStorySceneProp"
#         self.sceneEffects = None #array:2,0,ptr:CStorySceneEffect"
#         self.sceneLights = None #array:2,0,ptr:CStorySceneLight"
#         self.mayActorsStartWorking = None #Bool"
#         self.surpassWaterRendering = None #Bool"
#         self.dialogsetInstances = None #array:2,0,ptr:CStorySceneDialogsetInstance"
#         self.cameraDefinitions = None #array:2,0,StorySceneCameraDefinition"
#         self.banksDependency = None #array:2,0,CName"
#         self.blockMusicTriggers = None #Bool"
#         self.muteSpeechUnderWater = None #Bool"
#         self.soundListenerOverride = None #String"
#         self.soundEventsOnEnd = None #array:2,0,CName"
#         self.soundEventsOnSkip = None #array:2,0,CName"
#         loadProps(self, args)

class CStorySceneAction(CObject):
    def __init__(self, *args):
        self.maxTime = None #Float"
        loadProps(self, args)

class CStorySceneActionEquipItem(CStorySceneAction):
    def __init__(self, *args):
        self.maxTime = None #Float"
        self.leftHandItem = None #CName"
        self.rightHandItem = None #CName"
        loadProps(self, args)

class CStorySceneActionTeleport(CStorySceneAction):
    def __init__(self, *args):
        self.maxTime = None #Float"
        self.allowedDistance = None #Float"
        loadProps(self, args)

class CStorySceneActionMoveTo(CStorySceneActionTeleport):
    def __init__(self, *args):
        self.maxTime = None #Float"
        self.allowedDistance = None #Float"
        loadProps(self, args)

class CStorySceneActionRotateToPlayer(CStorySceneAction):
    def __init__(self, *args):
        self.maxTime = None #Float"
        self.acceptableAngleDif = None #Float"
        loadProps(self, args)

class CStorySceneActionSlide(CStorySceneActionTeleport):
    def __init__(self, *args):
        self.maxTime = None #Float"
        self.allowedDistance = None #Float"
        self.slideTime = None #Float"
        loadProps(self, args)

class CStorySceneActionStartWork(CStorySceneAction):
    def __init__(self, *args):
        self.maxTime = None #Float"
        self.jobTree = None #handle:CJobTree"
        self.category = None #CName"
        loadProps(self, args)

class CStorySceneActionStopWork(CStorySceneAction):
    def __init__(self, *args):
        self.maxTime = None #Float"
        loadProps(self, args)

#!
# class CStorySceneActor(IStorySceneItem):
#     def __init__(self, *args):
#         self.id = None #CName"
#         self.actorTags = None #TagList"
#         self.entityTemplate = None #soft:CEntityTemplate"
#         self.appearanceFilter = None #array:2,0,CName"
#         self.dontSearchByVoicetag = None #Bool"
#         self.useHiresShadows = None #Bool"
#         self.forceSpawn = None #Bool"
#         self.useMimic = None #Bool"
#         self.alias = None #String"
#         loadProps(self, args)

class CStorySceneActorEffectEvent(CStorySceneEvent):
    def __init__(self, *args):
        self.eventName = None #String"
        self.startPosition = None #Float"
        self.isMuted = None #Bool"
        self.contexID = None #Int32"
        self.sceneElement = None #ptr:CStorySceneElement"
        self.GUID = None #CGUID"
        self.interpolationEventGUID = None #CGUID"
        self.blendParentGUID = None #CGUID"
        self.linkParentGUID = None #CGUID"
        self.linkParentTimeOffset = None #Float"
        self.actor = None #CName"
        self.effectName = None #CName"
        self.startOrStop = None #Bool"
        self.persistAcrossSections = None #Bool"
        loadProps(self, args)

class CStorySceneActorEffectEventDuration(CStorySceneEventDuration):
    def __init__(self, *args):
        self.eventName = None #String"
        self.startPosition = None #Float"
        self.isMuted = None #Bool"
        self.contexID = None #Int32"
        self.sceneElement = None #ptr:CStorySceneElement"
        self.GUID = None #CGUID"
        self.interpolationEventGUID = None #CGUID"
        self.blendParentGUID = None #CGUID"
        self.linkParentGUID = None #CGUID"
        self.linkParentTimeOffset = None #Float"
        self.duration = None #Float"
        self.actor = None #CName"
        self.effectName = None #CName"
        loadProps(self, args)

class CStorySceneActorMap(CObject):
    def __init__(self, *args):
        pass
class CStorySceneActorPosition:
    def __init__(self, *args):
        self.position = None #TagList"
        self.distance = None #Float"
        self.useRotation = None #Bool"
        self.performAction = None #EStoryScenePerformActionMode"
        loadProps(self, args)

class CStorySceneActorTemplate:
    def __init__(self, *args):
        self.template = None #handle:CEntityTemplate"
        self.appearances = None #array:2,0,CName"
        self.tags = None #TagList"
        loadProps(self, args)

class CStorySceneAddFactEvent(CStorySceneEvent):
    def __init__(self, *args):
        self.eventName = None #String"
        self.startPosition = None #Float"
        self.isMuted = None #Bool"
        self.contexID = None #Int32"
        self.sceneElement = None #ptr:CStorySceneElement"
        self.GUID = None #CGUID"
        self.interpolationEventGUID = None #CGUID"
        self.blendParentGUID = None #CGUID"
        self.linkParentGUID = None #CGUID"
        self.linkParentTimeOffset = None #Float"
        self.factId = None #String"
        self.expireTime = None #Int32"
        self.factValue = None #Int32"
        loadProps(self, args)

class CStorySceneBlockingElement(CStorySceneElement):
    def __init__(self, *args):
        self.elementID = None #String"
        self.approvedDuration = None #Float"
        self.isCopy = None #Bool"
        self.event = None #ptr:CStorySceneEvent"
        loadProps(self, args)

class CStorySceneCameraBlendEvent(CStorySceneEventBlend):
    def __init__(self, *args):
        self.eventName = None #String"
        self.startPosition = None #Float"
        self.isMuted = None #Bool"
        self.contexID = None #Int32"
        self.sceneElement = None #ptr:CStorySceneElement"
        self.GUID = None #CGUID"
        self.interpolationEventGUID = None #CGUID"
        self.blendParentGUID = None #CGUID"
        self.linkParentGUID = None #CGUID"
        self.linkParentTimeOffset = None #Float"
        self.duration = None #Float"
        self.keys = None #array:2,0,CGUID"
        self.firstPointOfInterpolation = None #Float"
        self.lastPointOfInterpolation = None #Float"
        self.firstPartInterpolation = None #ECameraInterpolation"
        self.lastPartInterpolation = None #ECameraInterpolation"
        loadProps(self, args)

class CStorySceneChoice(CStorySceneElement):
    def __init__(self, *args):
        self.elementID = None #String"
        self.approvedDuration = None #Float"
        self.isCopy = None #Bool"
        self.choiceLines = None #array:2,0,ptr:CStorySceneChoiceLine"
        self.timeLimit = None #Float"
        self.duration = None #Float"
        self.isLooped = None #Bool"
        self.questChoice = None #Bool"
        self.showLastLine = None #Bool"
        self.alternativeUI = None #Bool"
        loadProps(self, args)


class CStorySceneLinkElement(CObject):
    def __init__(self, *args):
        self.linkedElements = None #array:2,0,ptr:CStorySceneLinkElement"
        self.nextLinkElement = None #ptr:CStorySceneLinkElement"
        loadProps(self, args)

class CStorySceneChoiceLine(CStorySceneLinkElement):
    def __init__(self, *args):
        self.linkedElements = None #array:2,0,ptr:CStorySceneLinkElement"
        self.nextLinkElement = None #ptr:CStorySceneLinkElement"
        self.choiceLine = None #LocalizedString"
        self.choiceComment = None #LocalizedString"
        self.questCondition = None #ptr:IQuestCondition"
        self.memo = None #array:2,0,ptr:ISceneChoiceMemo"
        self.singleUseChoice = None #Bool"
        self.emphasisLine = None #Bool"
        self.action = None #ptr:IStorySceneChoiceLineAction"
        loadProps(self, args)

class CStorySceneChoiceLineActionScripted(IStorySceneChoiceLineAction):
    def __init__(self, *args):
        pass
class CStorySceneChoiceLineActionScriptedContentGuard(CStorySceneChoiceLineActionScripted):
    def __init__(self, *args):
        self.playGoChunk = None #CName"
        loadProps(self, args)

class CStorySceneChoiceLineActionStallForContent(CStorySceneChoiceLineActionScripted):
    def __init__(self, *args):
        pass

class CStorySceneComment(CStorySceneElement):
    def __init__(self, *args):
        self.elementID = None #String"
        self.approvedDuration = None #Float"
        self.isCopy = None #Bool"
        self.commentText = None #LocalizedString"
        loadProps(self, args)

class CStorySceneComponent(CSpriteComponent):
    def __init__(self, *args):
        self.tags = None #TagList"
        self.transform = None #EngineTransform"
        self.transformParent = None #ptr:CHardAttachment"
        self.guid = None #CGUID"
        self.name = None #String"
        self.isStreamed = None #Bool"
        self.isVisible = None #Bool"
        self.icon = None #handle:CBitmapTexture"
        self.storyScene = None #soft:CStoryScene"
        loadProps(self, args)

class CStorySceneControlPart(CStorySceneLinkElement):
    def __init__(self, *args):
        self.linkedElements = None #array:2,0,ptr:CStorySceneLinkElement"
        self.nextLinkElement = None #ptr:CStorySceneLinkElement"
        self.comment = None #String"
        loadProps(self, args)

class CStorySceneCutscenePlayer(CStorySceneElement):
    def __init__(self, *args):
        self.elementID = None #String"
        self.approvedDuration = None #Float"
        self.isCopy = None #Bool"
        self.descriptionText = None #String"
        loadProps(self, args)

class CStorySceneCutsceneSection(CStorySceneSection):
    def __init__(self, *args):
        self.linkedElements = None #array:2,0,ptr:CStorySceneLinkElement"
        self.nextLinkElement = None #ptr:CStorySceneLinkElement"
        self.comment = None #String"
        self.contexID = None #Int32"
        self.nextVariantId = None #Uint32"
        self.defaultVariantId = None #Uint32"
        self.variants = None #array:2,0,ptr:CStorySceneSectionVariant"
        self.localeVariantMappings = None #array:2,0,ptr:CStorySceneLocaleVariantMapping"
        self.sceneElements = None #array:2,0,ptr:CStorySceneElement"
        self.events = None #array:2,0,ptr:CStorySceneEvent"
        self.eventsInfo = None #array:2,0,ptr:CStorySceneEventInfo"
        self.choice = None #ptr:CStorySceneChoice"
        self.sectionId = None #Uint32"
        self.sectionName = None #String"
        self.tags = None #TagList"
        self.interceptRadius = None #Float"
        self.interceptTimeout = None #Float"
        self.interceptSections = None #array:2,0,ptr:CStorySceneSection"
        self.isGameplay = None #Bool"
        self.isImportant = None #Bool"
        self.allowCameraMovement = None #Bool"
        self.hasCinematicOneliners = None #Bool"
        self.manualFadeIn = None #Bool"
        self.fadeInAtBeginning = None #Bool"
        self.fadeOutAtEnd = None #Bool"
        self.pauseInCombat = None #Bool"
        self.canBeSkipped = None #Bool"
        self.canHaveLookats = None #Bool"
        self.numberOfInputPaths = None #Uint32"
        self.dialogsetChangeTo = None #CName"
        self.forceDialogset = None #Bool"
        self.inputPathsElements = None #array:2,0,ptr:CStorySceneLinkElement"
        self.streamingLock = None #Bool"
        self.streamingAreaTag = None #CName"
        self.streamingUseCameraPosition = None #Bool"
        self.streamingCameraAllowedJumpDistance = None #Float"
        self.blockMusicTriggers = None #Bool"
        self.soundListenerOverride = None #String"
        self.soundEventsOnEnd = None #array:2,0,CName"
        self.soundEventsOnSkip = None #array:2,0,CName"
        self.maxBoxExtentsToApplyHiResShadows = None #Float"
        self.distantLightStartOverride = None #Float"
        self.cutscene = None #handle:CCutsceneTemplate"
        self.point = None #TagList"
        self.looped = None #Bool"
        self.actorOverrides = None #array:2,0,SCutsceneActorOverrideMapping"
        self.clearActorsHands = None #Bool"
        loadProps(self, args)

class CStorySceneGraphBlock(CGraphBlock):
    def __init__(self, *args):
        self.sockets = None #array:2,0,ptr:CGraphSocket"
        loadProps(self, args)

class CStorySceneGraphSocket(CGraphSocket):
    def __init__(self, *args):
        self.block = None #ptr:CGraphBlock"
        self.name = None #CName"
        self.connections = None #array:2,0,ptr:CGraphConnection"
        self.linkElement = None #ptr:CStorySceneLinkElement"
        loadProps(self, args)

class CStorySceneSectionBlock(CStorySceneGraphBlock):
    def __init__(self, *args):
        self.sockets = None #array:2,0,ptr:CGraphSocket"
        self.section = None #ptr:CStorySceneSection"
        loadProps(self, args)

class CStorySceneCutsceneSectionBlock(CStorySceneSectionBlock):
    def __init__(self, *args):
        self.sockets = None #array:2,0,ptr:CGraphSocket"
        self.section = None #ptr:CStorySceneSection"
        loadProps(self, args)

class CStorySceneDanglesShakeEvent(CStorySceneEvent):
    def __init__(self, *args):
        self.eventName = None #String"
        self.startPosition = None #Float"
        self.isMuted = None #Bool"
        self.contexID = None #Int32"
        self.sceneElement = None #ptr:CStorySceneElement"
        self.GUID = None #CGUID"
        self.interpolationEventGUID = None #CGUID"
        self.blendParentGUID = None #CGUID"
        self.linkParentGUID = None #CGUID"
        self.linkParentTimeOffset = None #Float"
        self.actor = None #CName"
        self.factor = None #Float"
        loadProps(self, args)

class CStorySceneDanglesShakeEventInterpolation(CStorySceneEventInterpolation):
    def __init__(self, *args):
        self.eventName = None #String"
        self.startPosition = None #Float"
        self.isMuted = None #Bool"
        self.contexID = None #Int32"
        self.sceneElement = None #ptr:CStorySceneElement"
        self.GUID = None #CGUID"
        self.interpolationEventGUID = None #CGUID"
        self.blendParentGUID = None #CGUID"
        self.linkParentGUID = None #CGUID"
        self.linkParentTimeOffset = None #Float"
        self.keyGuids = None #array:2,0,CGUID"
        self.interpolationMethod = None #EInterpolationMethod"
        self.easeInStyle = None #EInterpolationEasingStyle"
        self.easeInParameter = None #Float"
        self.easeOutStyle = None #EInterpolationEasingStyle"
        self.easeOutParameter = None #Float"
        self.keys = None #array:2,0,CStorySceneDanglesShakeEventInterpolationKey"
        loadProps(self, args)

class CStorySceneDanglesShakeEventInterpolationKey:
    def __init__(self, *args):
        self.bezierHandles = None #1]Bezier2dHandle"
        self.interpolationTypes = None #1]Uint32"
        self.volatile = None #Bool"
        loadProps(self, args)

class CStorySceneDialogset(CSkeletalAnimationSet):
    def __init__(self, *args):
        self.requiredSfxTag = None #CName"
        self.animations = None #array:2,0,ptr:CSkeletalAnimationSetEntry"
        self.extAnimEvents = None #array:2,0,handle:CExtAnimEventsFile"
        self.skeleton = None #handle:CSkeleton"
        self.compressedPoses = None #array:2,0,ptr:ICompressedPose"
        self.Streaming = None #="SAnimationBufferStreamingOption"
        self.Number = None # bones" Type="Uint32"
        self.dialogsetName = None #CName"
        self.dialogsetTransitionEvent = None #CName"
        self.isDynamic = None #Bool"
        self.characterTrajectories = None #array:2,0,EngineTransform"
        self.cameraTrajectories = None #array:2,0,EngineTransform"
        self.personalCameras = None #array:2,0,SScenePersonalCameraDescription"
        self.masterCameras = None #array:2,0,SSceneMasterCameraDescription"
        self.customCameras = None #array:2,0,SSceneCustomCameraDescription"
        self.cameraEyePositions = None #array:2,0,Vector"
        self.slots = None #array:2,0,ptr:CStorySceneDialogsetSlot"
        self.cameras = None #array:2,0,StorySceneCameraDefinition"
        loadProps(self, args)

# class CStorySceneDialogsetInstance(CObject):
#     def __init__(self, *args):
#         self.name = None #CName"
#         self.slots = None #array:2,0,ptr:CStorySceneDialogsetSlot"
#         self.placementTag = None #TagList"
#         self.snapToTerrain = None #Bool"
#         self.findSafePlacement = None #Bool"
#         self.safePlacementRadius = None #Float"
#         self.areCamerasUsedForBoundsCalculation = None #Bool"
#         self.path = None #String"
#         loadProps(self, args)

#!DEFINED EARLY
# class CStorySceneDialogsetSlot(CObject):
#     def __init__(self, *args):
#         self.slotNumber = None #Uint32"
#         self.slotName = None #CName"
#         self.slotPlacement = None #EngineTransform"
#         self.actorName = None #CName"
#         self.actorVisibility = None #Bool"
#         self.actorStatus = None #CName"
#         self.actorEmotionalState = None #CName"
#         self.actorPoseName = None #CName"
#         self.actorMimicsEmotionalState = None #CName"
#         self.actorMimicsLayer_Eyes = None #CName"
#         self.actorMimicsLayer_Pose = None #CName"
#         self.actorMimicsLayer_Animation = None #CName"
#         self.actorMimicsLayer_Pose_Weight = None #Float"
#         self.forceBodyIdleAnimation = None #CName"
#         self.forceBodyIdleAnimationWeight = None #Float"
#         self.actorState = None #CName"
#         self.ID = None #CGUID"
#         self.setupAction = None #array:2,0,ptr:CStorySceneAction"
#         loadProps(self, args)

class CStorySceneDisableDangleEvent(CStorySceneEvent):
    def __init__(self, *args):
        self.eventName = None #String"
        self.startPosition = None #Float"
        self.isMuted = None #Bool"
        self.contexID = None #Int32"
        self.sceneElement = None #ptr:CStorySceneElement"
        self.GUID = None #CGUID"
        self.interpolationEventGUID = None #CGUID"
        self.blendParentGUID = None #CGUID"
        self.linkParentGUID = None #CGUID"
        self.linkParentTimeOffset = None #Float"
        self.actor = None #CName"
        self.weight = None #Float"
        loadProps(self, args)

class CStorySceneDisablePhysicsClothEvent(CStorySceneEvent):
    def __init__(self, *args):
        self.eventName = None #String"
        self.startPosition = None #Float"
        self.isMuted = None #Bool"
        self.contexID = None #Int32"
        self.sceneElement = None #ptr:CStorySceneElement"
        self.GUID = None #CGUID"
        self.interpolationEventGUID = None #CGUID"
        self.blendParentGUID = None #CGUID"
        self.linkParentGUID = None #CGUID"
        self.linkParentTimeOffset = None #Float"
        self.actor = None #CName"
        self.weight = None #Float"
        self.blendTime = None #Float"
        loadProps(self, args)

class CStorySceneEffect(IStorySceneItem):
    def __init__(self, *args):
        self.id = None #CName"
        self.particleSystem = None #soft:CParticleSystem"
        loadProps(self, args)

class CStorySceneFlowCondition(CStorySceneControlPart):
    def __init__(self, *args):
        self.linkedElements = None #array:2,0,ptr:CStorySceneLinkElement"
        self.nextLinkElement = None #ptr:CStorySceneLinkElement"
        self.comment = None #String"
        self.trueLink = None #ptr:CStorySceneLinkElement"
        self.falseLink = None #ptr:CStorySceneLinkElement"
        self.questCondition = None #ptr:IQuestCondition"
        loadProps(self, args)

class CStorySceneFlowConditionBlock(CStorySceneGraphBlock):
    def __init__(self, *args):
        self.sockets = None #array:2,0,ptr:CGraphSocket"
        self.description = None #String"
        self.condition = None #ptr:CStorySceneFlowCondition"
        loadProps(self, args)

class CStorySceneFlowSwitch(CStorySceneControlPart):
    def __init__(self, *args):
        self.linkedElements = None #array:2,0,ptr:CStorySceneLinkElement"
        self.nextLinkElement = None #ptr:CStorySceneLinkElement"
        self.comment = None #String"
        self.cases = None #array:2,0,ptr:CStorySceneFlowSwitchCase"
        self.defaultLink = None #ptr:CStorySceneLinkElement"
        loadProps(self, args)

class CStorySceneFlowSwitchBlock(CStorySceneGraphBlock):
    def __init__(self, *args):
        self.sockets = None #array:2,0,ptr:CGraphSocket"
        self.description = None #String"
        self.switch = None #ptr:CStorySceneFlowSwitch"
        loadProps(self, args)

class CStorySceneFlowSwitchCase(CObject):
    def __init__(self, *args):
        self.whenCondition = None #ptr:IQuestCondition"
        self.thenLink = None #ptr:CStorySceneLinkElement"
        loadProps(self, args)

class CStorySceneGraph(CObject):
    def __init__(self, *args):
        self.graphBlocks = None #array:2,0,ptr:CGraphBlock"
        loadProps(self, args)

class CStorySceneInput(CStorySceneControlPart):
    def __init__(self, *args):
        self.linkedElements = None #array:2,0,ptr:CStorySceneLinkElement"
        self.nextLinkElement = None #ptr:CStorySceneLinkElement"
        self.comment = None #String"
        self.inputName = None #String"
        self.voicetagMappings = None #array:2,0,CStorySceneVoicetagMapping"
        self.musicState = None #ESoundStateDuringScene"
        self.ambientsState = None #ESoundStateDuringScene"
        self.sceneNearPlane = None #ENearPlaneDistance"
        self.sceneFarPlane = None #EFarPlaneDistance"
        self.dontStopByExternalSystems = None #Bool"
        self.maxActorsStaryingDistanceFromPlacement = None #Float"
        self.maxActorsStartingDistanceFormPlayer = None #Float"
        self.dialogsetPlacementTag = None #CName"
        self.dialogsetInstanceName = None #CName"
        self.enableIntroVehicleDismount = None #Bool"
        self.enableIntroLookAts = None #Bool"
        self.introTotalTime = None #Float"
        self.enableIntroFadeOut = None #Bool"
        self.introFadeOutStartTime = None #Float"
        self.blockSceneArea = None #Bool"
        self.enableDestroyDeadActorsAround = None #Bool"
        self.isImportant = None #Bool"
        self.isGameplay = None #Bool"
        loadProps(self, args)

class CStorySceneInputBlock(CStorySceneGraphBlock):
    def __init__(self, *args):
        self.sockets = None #array:2,0,ptr:CGraphSocket"
        self.input = None #ptr:CStorySceneInput"
        loadProps(self, args)

class CStorySceneLight(IStorySceneItem):
    def __init__(self, *args):
        self.id = None #CName"
        self.type = None #ELightType"
        self.innerAngle = None #Float"
        self.outerAngle = None #Float"
        self.softness = None #Float"
        self.shadowCastingMode = None #ELightShadowCastingMode"
        self.shadowFadeDistance = None #Float"
        self.shadowFadeRange = None #Float"
        self.dimmerType = None #EDimmerType"
        self.dimmerAreaMarker = None #Bool"
        loadProps(self, args)

class CStorySceneLine(CAbstractStorySceneLine):
    def __init__(self, *args):
        self.elementID = None #String"
        self.approvedDuration = None #Float"
        self.isCopy = None #Bool"
        self.voicetag = None #CName"
        self.comment = None #LocalizedString"
        self.speakingTo = None #CName"
        self.dialogLine = None #LocalizedString"
        self.voiceFileName = None #String"
        self.noBreak = None #Bool"
        self.soundEventName = None #StringAnsi"
        self.disableOcclusion = None #Bool"
        self.isBackgroundLine = None #Bool"
        self.alternativeUI = None #Bool"
        loadProps(self, args)

class CStorySceneLinkHub(CStorySceneControlPart):
    def __init__(self, *args):
        self.linkedElements = None #array:2,0,ptr:CStorySceneLinkElement"
        self.nextLinkElement = None #ptr:CStorySceneLinkElement"
        self.comment = None #String"
        self.numSockets = None #Uint32"
        loadProps(self, args)

class CStorySceneLinkHubBlock(CStorySceneGraphBlock):
    def __init__(self, *args):
        self.sockets = None #array:2,0,ptr:CGraphSocket"
        self.hub = None #ptr:CStorySceneLinkHub"
        loadProps(self, args)

class CStorySceneLocaleVariantMapping:
    def __init__(self, *args):
        self.localeId = None #Uint32"
        self.variantId = None #Uint32"
        loadProps(self, args)

class CStorySceneMorphEvent(CStorySceneEvent):
    def __init__(self, *args):
        self.eventName = None #String"
        self.startPosition = None #Float"
        self.isMuted = None #Bool"
        self.contexID = None #Int32"
        self.sceneElement = None #ptr:CStorySceneElement"
        self.GUID = None #CGUID"
        self.interpolationEventGUID = None #CGUID"
        self.blendParentGUID = None #CGUID"
        self.linkParentGUID = None #CGUID"
        self.linkParentTimeOffset = None #Float"
        self.actor = None #CName"
        self.weight = None #Float"
        self.morphComponentId = None #CName"
        loadProps(self, args)

class CStorySceneOutput(CStorySceneControlPart):
    def __init__(self, *args):
        self.linkedElements = None #array:2,0,ptr:CStorySceneLinkElement"
        self.nextLinkElement = None #ptr:CStorySceneLinkElement"
        self.comment = None #String"
        self.name = None #CName"
        self.questOutput = None #Bool"
        self.endsWithBlackscreen = None #Bool"
        self.blackscreenColor = None #Color"
        self.gameplayCameraBlendTime = None #Float"
        self.environmentLightsBlendTime = None #Float"
        self.gameplayCameraUseFocusTarget = None #Bool"
        loadProps(self, args)

class CStorySceneOutputBlock(CStorySceneGraphBlock):
    def __init__(self, *args):
        self.sockets = None #array:2,0,ptr:CGraphSocket"
        self.output = None #ptr:CStorySceneOutput"
        loadProps(self, args)

class CStorySceneOverridePlacementBlend(CStorySceneEventCurveBlend):
    def __init__(self, *args):
        self.eventName = None #String"
        self.startPosition = None #Float"
        self.isMuted = None #Bool"
        self.contexID = None #Int32"
        self.sceneElement = None #ptr:CStorySceneElement"
        self.GUID = None #CGUID"
        self.interpolationEventGUID = None #CGUID"
        self.blendParentGUID = None #CGUID"
        self.linkParentGUID = None #CGUID"
        self.linkParentTimeOffset = None #Float"
        self.duration = None #Float"
        self.keys = None #array:2,0,CGUID"
        self.curve = None #SMultiCurve"
        self.actorName = None #CName"
        self.animationStartName = None #CName"
        self.animationLoopName = None #CName"
        self.animationStopName = None #CName"
        loadProps(self, args)

class CStoryScenePauseElement(CStorySceneElement):
    def __init__(self, *args):
        self.elementID = None #String"
        self.approvedDuration = None #Float"
        self.isCopy = None #Bool"
        self.duration = None #Float"
        loadProps(self, args)

from .CR2W_file import CGameplayEntity, CEntity
class CStoryScenePlayer(CEntity):
    def __init__(self, *args):
        self.tags = None #TagList"
        self.transform = None #EngineTransform"
        self.transformParent = None #ptr:CHardAttachment"
        self.guid = None #CGUID"
        self.components = None #array:2,0,ptr:CComponent"
        self.template = None #handle:CEntityTemplate"
        self.streamingDataBuffer = None #SharedDataBuffer"
        self.streamingDistance = None #Uint8"
        self.entityStaticFlags = None #EEntityStaticFlags"
        self.autoPlayEffectName = None #CName"
        self.entityFlags = None #Uint8"
        self.storyScene = None #handle:CStoryScene"
        self.injectedScenes = None #array:2,0,handle:CStoryScene"
        self.isPaused = None #Uint16"
        self.isGameplay = None #Bool"
        loadProps(self, args)

class CStoryScenePreviewPlayer(CStoryScenePlayer):
    def __init__(self, *args):
        self.tags = None #TagList"
        self.transform = None #EngineTransform"
        self.transformParent = None #ptr:CHardAttachment"
        self.guid = None #CGUID"
        self.components = None #array:2,0,ptr:CComponent"
        self.template = None #handle:CEntityTemplate"
        self.streamingDataBuffer = None #SharedDataBuffer"
        self.streamingDistance = None #Uint8"
        self.entityStaticFlags = None #EEntityStaticFlags"
        self.autoPlayEffectName = None #CName"
        self.entityFlags = None #Uint8"
        self.storyScene = None #handle:CStoryScene"
        self.injectedScenes = None #array:2,0,handle:CStoryScene"
        self.isPaused = None #Uint16"
        self.isGameplay = None #Bool"
        loadProps(self, args)

class CStorySceneProp(IStorySceneItem):
    def __init__(self, *args):
        self.id = None #CName"
        self.entityTemplate = None #soft:CEntityTemplate"
        self.forceBehaviorGraph = None #CName"
        self.resetBehaviorGraph = None #Bool"
        self.useMimics = None #Bool"
        loadProps(self, args)

class CStoryScenePropEffectEvent(CStorySceneEvent):
    def __init__(self, *args):
        self.eventName = None #String"
        self.startPosition = None #Float"
        self.isMuted = None #Bool"
        self.contexID = None #Int32"
        self.sceneElement = None #ptr:CStorySceneElement"
        self.GUID = None #CGUID"
        self.interpolationEventGUID = None #CGUID"
        self.blendParentGUID = None #CGUID"
        self.linkParentGUID = None #CGUID"
        self.linkParentTimeOffset = None #Float"
        self.propID = None #CName"
        self.effectName = None #CName"
        self.startOrStop = None #Bool"
        loadProps(self, args)

class CStorySceneQuestChoiceLine(CStorySceneComment):
    def __init__(self, *args):
        self.elementID = None #String"
        self.approvedDuration = None #Float"
        self.isCopy = None #Bool"
        self.commentText = None #LocalizedString"
        self.emphasisLine = None #Bool"
        self.returnToChoice = None #Bool"
        self.action = None #ptr:IStorySceneChoiceLineAction"
        loadProps(self, args)

class CStorySceneRandomBlock(CStorySceneGraphBlock):
    def __init__(self, *args):
        self.sockets = None #array:2,0,ptr:CGraphSocket"
        self.controlPart = None #ptr:CStorySceneRandomizer"
        loadProps(self, args)

class CStorySceneRandomizer(CStorySceneControlPart):
    def __init__(self, *args):
        self.linkedElements = None #array:2,0,ptr:CStorySceneLinkElement"
        self.nextLinkElement = None #ptr:CStorySceneLinkElement"
        self.comment = None #String"
        self.outputs = None #array:2,0,ptr:CStorySceneLinkElement"
        loadProps(self, args)

class CStorySceneResetClothAndDanglesEvent(CStorySceneEvent):
    def __init__(self, *args):
        self.eventName = None #String"
        self.startPosition = None #Float"
        self.isMuted = None #Bool"
        self.contexID = None #Int32"
        self.sceneElement = None #ptr:CStorySceneElement"
        self.GUID = None #CGUID"
        self.interpolationEventGUID = None #CGUID"
        self.blendParentGUID = None #CGUID"
        self.linkParentGUID = None #CGUID"
        self.linkParentTimeOffset = None #Float"
        self.actor = None #CName"
        self.forceRelaxedState = None #Bool"
        loadProps(self, args)

class CStorySceneScript(CStorySceneControlPart):
    def __init__(self, *args):
        self.linkedElements = None #array:2,0,ptr:CStorySceneLinkElement"
        self.nextLinkElement = None #ptr:CStorySceneLinkElement"
        self.comment = None #String"
        self.functionName = None #CName"
        self.links = None #array:2,0,ptr:CStorySceneLinkElement"
        loadProps(self, args)

class CStorySceneScriptingBlock(CStorySceneGraphBlock):
    def __init__(self, *args):
        self.sockets = None #array:2,0,ptr:CGraphSocket"
        self.sceneScript = None #ptr:CStorySceneScript"
        loadProps(self, args)

class CStorySceneScriptLine(CStorySceneElement):
    def __init__(self, *args):
        self.elementID = None #String"
        self.approvedDuration = None #Float"
        self.isCopy = None #Bool"
        self.script = None #String"
        self.sceneScript = None #ptr:CStorySceneScript"
        loadProps(self, args)

#! DEFINED EARLY
# class CStorySceneSection(CStorySceneControlPart):
#     def __init__(self, *args):
#         self.linkedElements = None #array:2,0,ptr:CStorySceneLinkElement"
#         self.nextLinkElement = None #ptr:CStorySceneLinkElement"
#         self.comment = None #String"
#         self.contexID = None #Int32"
#         self.nextVariantId = None #Uint32"
#         self.defaultVariantId = None #Uint32"
#         self.variants = None #array:2,0,ptr:CStorySceneSectionVariant"
#         self.localeVariantMappings = None #array:2,0,ptr:CStorySceneLocaleVariantMapping"
#         self.sceneElements = None #array:2,0,ptr:CStorySceneElement"
#         self.events = None #array:2,0,ptr:CStorySceneEvent"
#         self.eventsInfo = None #array:2,0,ptr:CStorySceneEventInfo"
#         self.choice = None #ptr:CStorySceneChoice"
#         self.sectionId = None #Uint32"
#         self.sectionName = None #String"
#         self.tags = None #TagList"
#         self.interceptRadius = None #Float"
#         self.interceptTimeout = None #Float"
#         self.interceptSections = None #array:2,0,ptr:CStorySceneSection"
#         self.isGameplay = None #Bool"
#         self.isImportant = None #Bool"
#         self.allowCameraMovement = None #Bool"
#         self.hasCinematicOneliners = None #Bool"
#         self.manualFadeIn = None #Bool"
#         self.fadeInAtBeginning = None #Bool"
#         self.fadeOutAtEnd = None #Bool"
#         self.pauseInCombat = None #Bool"
#         self.canBeSkipped = None #Bool"
#         self.canHaveLookats = None #Bool"
#         self.numberOfInputPaths = None #Uint32"
#         self.dialogsetChangeTo = None #CName"
#         self.forceDialogset = None #Bool"
#         self.inputPathsElements = None #array:2,0,ptr:CStorySceneLinkElement"
#         self.streamingLock = None #Bool"
#         self.streamingAreaTag = None #CName"
#         self.streamingUseCameraPosition = None #Bool"
#         self.streamingCameraAllowedJumpDistance = None #Float"
#         self.blockMusicTriggers = None #Bool"
#         self.soundListenerOverride = None #String"
#         self.soundEventsOnEnd = None #array:2,0,CName"
#         self.soundEventsOnSkip = None #array:2,0,CName"
#         self.maxBoxExtentsToApplyHiResShadows = None #Float"
#         self.distantLightStartOverride = None #Float"
#         loadProps(self, args)

class CStorySceneSectionVariant:
    def __init__(self, *args):
        self.id = None #Uint32"
        self.localeId = None #Uint32"
        self.events = None #array:2,0,CGUID"
        self.elementInfo = None #array:2,0,CStorySceneSectionVariantElementInfo"
        loadProps(self, args)

class CStorySceneSectionVariantElementInfo:
    def __init__(self, *args):
        self.elementId = None #String"
        self.approvedDuration = None #Float"
        loadProps(self, args)

class CStorySceneSpawner(CGameplayEntity):
    def __init__(self, *args):
        self.tags = None #TagList"
        self.transform = None #EngineTransform"
        self.transformParent = None #ptr:CHardAttachment"
        self.guid = None #CGUID"
        self.components = None #array:2,0,ptr:CComponent"
        self.template = None #handle:CEntityTemplate"
        self.streamingDataBuffer = None #SharedDataBuffer"
        self.streamingDistance = None #Uint8"
        self.entityStaticFlags = None #EEntityStaticFlags"
        self.autoPlayEffectName = None #CName"
        self.entityFlags = None #Uint8"
        self.idTag = None #IdTag"
        self.isSaveable = None #Bool"
        self.propertyAnimationSet = None #ptr:CPropertyAnimationSet"
        self.displayName = None #LocalizedString"
        self.stats = None #ptr:CCharacterStats"
        self.isInteractionActivator = None #Bool"
        self.aimVector = None #Vector"
        self.gameplayFlags = None #Uint32"
        self.focusModeVisibility = None #EFocusModeVisibility"
        self.storyScene = None #handle:CStoryScene"
        self.inputName = None #String"
        loadProps(self, args)

class CStorySceneSystem(IGameSystem):
    def __init__(self, *args):
        self.activeScenes = None #array:2,0,handle:CStoryScenePlayer"
        self.actorMap = None #ptr:CStorySceneActorMap"
        loadProps(self, args)

class CStorySceneVideoBlock(CStorySceneGraphBlock):
    def __init__(self, *args):
        self.sockets = None #array:2,0,ptr:CGraphSocket"
        self.sceneVideo = None #ptr:CStorySceneVideoSection"
        loadProps(self, args)

class CStorySceneVideoElement(CStorySceneElement):
    def __init__(self, *args):
        self.elementID = None #String"
        self.approvedDuration = None #Float"
        self.isCopy = None #Bool"
        self.description = None #String"
        loadProps(self, args)

class CStorySceneVideoSection(CStorySceneSection):
    def __init__(self, *args):
        self.linkedElements = None #array:2,0,ptr:CStorySceneLinkElement"
        self.nextLinkElement = None #ptr:CStorySceneLinkElement"
        self.comment = None #String"
        self.contexID = None #Int32"
        self.nextVariantId = None #Uint32"
        self.defaultVariantId = None #Uint32"
        self.variants = None #array:2,0,ptr:CStorySceneSectionVariant"
        self.localeVariantMappings = None #array:2,0,ptr:CStorySceneLocaleVariantMapping"
        self.sceneElements = None #array:2,0,ptr:CStorySceneElement"
        self.events = None #array:2,0,ptr:CStorySceneEvent"
        self.eventsInfo = None #array:2,0,ptr:CStorySceneEventInfo"
        self.choice = None #ptr:CStorySceneChoice"
        self.sectionId = None #Uint32"
        self.sectionName = None #String"
        self.tags = None #TagList"
        self.interceptRadius = None #Float"
        self.interceptTimeout = None #Float"
        self.interceptSections = None #array:2,0,ptr:CStorySceneSection"
        self.isGameplay = None #Bool"
        self.isImportant = None #Bool"
        self.allowCameraMovement = None #Bool"
        self.hasCinematicOneliners = None #Bool"
        self.manualFadeIn = None #Bool"
        self.fadeInAtBeginning = None #Bool"
        self.fadeOutAtEnd = None #Bool"
        self.pauseInCombat = None #Bool"
        self.canBeSkipped = None #Bool"
        self.canHaveLookats = None #Bool"
        self.numberOfInputPaths = None #Uint32"
        self.dialogsetChangeTo = None #CName"
        self.forceDialogset = None #Bool"
        self.inputPathsElements = None #array:2,0,ptr:CStorySceneLinkElement"
        self.streamingLock = None #Bool"
        self.streamingAreaTag = None #CName"
        self.streamingUseCameraPosition = None #Bool"
        self.streamingCameraAllowedJumpDistance = None #Float"
        self.blockMusicTriggers = None #Bool"
        self.soundListenerOverride = None #String"
        self.soundEventsOnEnd = None #array:2,0,CName"
        self.soundEventsOnSkip = None #array:2,0,CName"
        self.maxBoxExtentsToApplyHiResShadows = None #Float"
        self.distantLightStartOverride = None #Float"
        self.videoFileName = None #String"
        self.eventDescription = None #String"
        self.suppressRendering = None #Bool"
        self.extraVideoFileNames = None #array:2,0,String"
        loadProps(self, args)

class CStorySceneVoicetagMapping:
    def __init__(self, *args):
        self.voicetag = None #CName"
        self.mustUseContextActor = None #Bool"
        self.invulnerable = None #Bool"
        self.actorOptional = None #Bool"
        loadProps(self, args)

class CStorySceneWaypointComponent(CComponent):
    def __init__(self, *args):
        self.tags = None #TagList"
        self.transform = None #EngineTransform"
        self.transformParent = None #ptr:CHardAttachment"
        self.guid = None #CGUID"
        self.name = None #String"
        self.isStreamed = None #Bool"
        self.dialogsetName = None #CName"
        self.dialogset = None #handle:CStorySceneDialogset"
        self.showCameras = None #Bool"
        self.useDefaultDialogsetPositions = None #Bool"
        loadProps(self, args)


#################

# STORYSCENE CLASSES END

##############