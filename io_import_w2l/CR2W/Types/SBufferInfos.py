from typing import List, Tuple, Dict
from CR2W.w3_types import Vector3D
from enum import Enum

class MMatrix(object):
    def __init__(self, translation:Vector3D, rotation:Vector3D, scale:Vector3D):
        self.translation = translation
        self.rotation = rotation
        self.scale = scale
    def __init__(self, translation:Vector3D, rotation:Vector3D):
        self.translation = translation
        self.rotation = rotation
        self.scale = Vector3D(0, 0, 0)
    def __init__(self, translation:Vector3D):
        self.translation = translation
        self.rotation = Vector3D(0, 0, 0)
        self.scale = Vector3D(0, 0, 0)
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

class VertexSkinningEntry:
    def __init__(self):
            self.boneId: int = 0
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