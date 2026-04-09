from typing import List

from .SBufferInfos import SMeshInfos, BoneData, W3_DataCache
import math

class CommonData(object):
    PI_OVER_180 = math.pi / 180.0

    def __init__(self):
        self.modelPath = ""
        self.staticMesh = None
        self.materialInstances: List = []
        self.meshInfos: List[SMeshInfos] = []
        self.boneData = BoneData()
        self.w3_DataCache = W3_DataCache()
        
        self.autohideDistance:float = 20.0
        self.isTwoSided:bool = False
        self.useExtraStreams:bool = False
        self.generalizedMeshRadius:float = 0.0
        self.mergeInGlobalShadowMesh:bool = True
        self.isOccluder:bool = True
        self.smallestHoleOverride:float = -1.0
        self.isStatic:bool = True
        self.entityProxy:bool = False


