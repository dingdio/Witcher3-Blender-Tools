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
        
        self.isStatic = False
        self.autohideDistance:float = 100
        self.isTwoSided:bool = False
        self.useExtraStreams:bool = True
        self.mergeInGlobalShadowMesh:bool = True
        self.entityProxy:bool = False


