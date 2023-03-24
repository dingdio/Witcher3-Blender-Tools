from typing import List, Tuple, Dict
from ..w3_types import Vector3D
from enum import Enum

class MMatrix(object):
    # def __init__(self, translation:Vector3D, rotation:Vector3D, scale:Vector3D):
    #     self.translation = translation
    #     self.rotation = rotation
    #     self.scale = scale
    # def __init__(self, translation:Vector3D, rotation:Vector3D):
    #     self.translation = translation
    #     self.rotation = rotation
    #     self.scale = Vector3D(0, 0, 0)
    # def __init__(self, translation:Vector3D):
    #     self.translation = translation
    #     self.rotation = Vector3D(0, 0, 0)
    #     self.scale = Vector3D(0, 0, 0)
    def __init__(self):
        self.translation = Vector3D(0, 0, 0)
        self.rotation = Vector3D(0, 0, 0)
        self.scale = Vector3D(0, 0, 0)
    def SetElement(self, idx, value):
        self.translation.x = value if idx == 0 else self.translation.x
        self.translation.y = value if idx == 1 else self.translation.y
        self.translation.z = value if idx == 2 else self.translation.z
        
        self.rotation.x = value if idx == 3 else self.rotation.x
        self.rotation.y = value if idx == 4 else self.rotation.y
        self.rotation.z = value if idx == 5 else self.rotation.z
        
        self.scale.x = value if idx == 6 else self.scale.x
        self.scale.y = value if idx == 7 else self.scale.y
        self.scale.z = value if idx == 8 else self.scale.z

class SBufferInfos:
    def __init__(self):
        self.vertexBufferOffset: numpy.uint = 0
        self.vertexBufferSize: numpy.uint = 0

        self.indexBufferOffset: numpy.uint = 0
        self.indexBufferSize: numpy.uint = 0

        self.quantizationScale: Vector3D = Vector3D(1,1,1)
        self.quantizationOffset: Vector3D = Vector3D(0,0,0)

        self.verticesBuffer: List[SVertexBufferInfos] = []
    #Information about the .buffer file

class SVertexBufferInfos:
    def __init__(self):
        self.verticesCoordsOffset: numpy.uint = 0
        self.uvOffset: numpy.uint = 0
        self.normalsOffset: numpy.uint = 0

        self.indicesOffset: numpy.uint = 0

        self.nbVertices: numpy.ushort = 0
        self.nbIndices : numpy.uint = 0

        self.materialID: numpy.byte = 0

        self.lod: numpy.byte = 1
    #Information to load a mesh from the buffer

class EMeshVertexType(Enum):
    EMVT_STATIC = 0
    EMVT_SKINNED = 1

import numpy

class SMeshInfos:
    def __init__(self):
        self.numVertices: int = 0
        self.numIndices: int = 0
        self.numBonesPerVertex: int = 4

        self.firstVertex: int = 0
        self.firstIndex: int = 0

        self.vertexType: EMeshVertexType.EMVT_STATIC = EMeshVertexType.EMVT_STATIC

        self.materialID: int = 0
        
        self.lod: int = -1 # -1 means it should not be used
        self.distance: int = 0 # the distance the lod will be used

class VertexSkinningEntry:
    def __init__(self):
            self.boneId: int = 0
            self.boneId_idx: int = 0
            self.meshBufferId: numpy.ushort = 0
            self.vertexId: int = 0
            self.strength: float = 0


class BoneEntry:
    def __init__(self):
            self.name: str = ""
            self.offsetMatrix: MMatrix = MMatrix()

class W3_DataCache:
    def __init__(self):
        self.vertices: List[VertexSkinningEntry] = []
        self.bones: List[BoneEntry] = []

class BoneData:
    def __init__(self):
        self.nbBones: int = 0

        self.jointNames: List[str] = []
        self.boneMatrices: List[MMatrix] = []
        self.BoneIndecesMappingBoneIndex = []
        self.Block3 = []
        self.vertex_groups = []

import numpy as np
class UncookedVertex:
    def __init__(self):
        self.x:float = 0.0
        self.y:float = 0.0
        self.z:float = 0.0
        self.bone_x:np.ubyte = 0.0
        self.bone_y:np.ubyte = 0.0
        self.bone_z:np.ubyte = 0.0
        self.bone_w:np.ubyte = 0.0
        self.weight_x:float = 0.0
        self.weight_y:float = 0.0
        self.weight_z:float = 0.0
        self.weight_w:float = 0.0
        self.normx:float = 0.0
        self.normy:float = 0.0
        self.normz:float = 0.0
        self.r:np.ubyte = 0.0
        self.g:np.ubyte = 0.0
        self.b:np.ubyte = 0.0
        self.a:np.ubyte = 0.0
        self.ux:float = 0.0
        self.uv:float = 0.0
        self.ux2:float = 0.0
        self.uv2:float = 0.0
        self.some_x:float = 0.0
        self.some_y:float = 0.0
        self.some_z:float = 0.0
        self.zero0:float = 0.0
        self.zero1:float = 0.0
        self.zero2:float = 0.0
        self.zero3:float = 0.0
        self.zero4:float = 0.0
        self.zero5:float = 0.0
        self.zero6:float = 0.0
        self.zero7:float = 0.0
        self.zero8:float = 0.0
        self.zero9:float = 0.0
        self.zero10:float = 0.0
        self.zero11:float = 0.0
        self.zero12:float = 0.0
        self.zero13:float = 0.0
        self.zero14:float = 0.0
        self.zero15:float = 0.0
        self.zero16:float = 0.0
        self.zero17:float = 0.0
        self.zero18:float = 0.0

#buffer is simple list of vert info
#each meshinfos reperests 1 mesh in buffer and will have numVertices to read too in uncooked buffer
class UncookedBuffer:
    def __init__(self):
        # local uint vert_offset = 3985
        # local uint vert_count = 4
        self.vertices: List[UncookedVertex] = []
