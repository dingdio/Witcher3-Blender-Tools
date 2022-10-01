from typing import List

from CR2W.Types.SBufferInfos import SMeshInfos


class CommonData(object):
    def __init__(self):
        #public string modelPath = "";
        # public StaticMesh staticMesh = StaticMesh.Create();
        self.materialInstances: List = []# public List<CMaterialInstance> materialInstances = new List<CMaterialInstance>();
        self.meshInfos: List[SMeshInfos] = []# public List<SMeshInfos> meshInfos = new List<SMeshInfos>();
        # public BoneData boneData = new BoneData();
        # public W3_DataCache w3_DataCache = new W3_DataCache();

        # public const float PI_OVER_180 = (float)Math.PI / 180.0f;


