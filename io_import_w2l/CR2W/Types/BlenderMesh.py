from typing import List

from CR2W.Types.SBufferInfos import SMeshInfos, BoneData, W3_DataCache
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


