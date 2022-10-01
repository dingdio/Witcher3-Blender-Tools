from typing import List, Tuple, Dict
from CR2W.w3_types import Vector3D

class MMatrix(object):
    def __init__(self):
        self.translation = Vector3D(0, 0, 0)
        self.rotation = Vector3D(0, 0, 0)
        self.scale = Vector3D(0, 0, 0)
    def __init__(self, translation:Vector3D):
        self.translation = translation
        self.rotation = Vector3D(0, 0, 0)
        self.scale = Vector3D(0, 0, 0)
    def __init__(self, translation:Vector3D, rotation:Vector3D):
        self.translation = translation
        self.rotation = rotation
        self.scale = Vector3D(0, 0, 0)
    def __init__(self, translation:Vector3D, rotation:Vector3D, scale:Vector3D):
        self.translation = translation
        self.rotation = rotation
        self.scale = scale

class SBufferInfos:
    def __init__(self):
        self.vertexBufferOffset: numpy.uint = 0 # public uint
        self.vertexBufferSize: numpy.uint = 0 # public uint

        self.indexBufferOffset: numpy.uint = 0 # public uint
        self.indexBufferSize: numpy.uint = 0 # public uint

        self.quantizationScale: Vector3D = Vector3D(1,1,1) #new Vector3Df(1, 1, 1); public Vector3Df
        self.quantizationOffset: Vector3D = Vector3D(0,0,0)  # new Vector3Df(0, 0, 0);  public Vector3Df

        self.verticesBuffer: List[SVertexBufferInfos] = [] #new List<SVertexBufferInfos>(); # public List<SVertexBufferInfos>

    #Informations about the .buffer file
class SVertexBufferInfos:
    def __init__(self):
        self.verticesCoordsOffset: numpy.uint = 0 # public uint
        self.uvOffset: numpy.uint = 0 # public uint
        self.normalsOffset: numpy.uint = 0 # public uint

        self.indicesOffset: numpy.uint = 0 # public uint

        self.nbVertices: numpy.ushort = 0 # public ushort
        self.nbIndices : numpy.uint = 0 # public uint

        self.materialID: numpy.byte = 0 # public byte

        self.lod: numpy.byte = 1 # public byte

    #Information to load a mesh from the buffer

class EMeshVertexType(object):
    EMVT_STATIC = 0
    EMVT_SKINNED = 1

import numpy

class SMeshInfos:
    def __init__(self):
        self.numVertices: int = 0 # public uint
        self.numIndices: int = 0 # public uint
        self.numBonesPerVertex: int = 4 # public uint

        self.firstVertex: int = 0 # public uint
        self.firstIndex: int = 0 # public uint

        self.vertexType: EMeshVertexType.EMVT_STATIC = EMeshVertexType.EMVT_STATIC # public EMeshVertexType

        self.materialID: int = 0 # public uint

class VertexSkinningEntry:
    def __init__(self):
            self.boneId: int = 0 # public uint
            self.meshBufferId: numpy.ushort = 0 # public ushort
            self.vertexId: int = 0 # public uint
            self.strength: float = 0 # public float


class BoneEntry:
    def __init__(self):
            self.name: str = "" # public string
            self.offsetMatrix: MMatrix = MMatrix() #public Matrix new Matrix()

class W3_DataCache:
    def __init__(self):
        self.vertices: List[VertexSkinningEntry] = [] #public List<VertexSkinningEntry>  new List<VertexSkinningEntry>();
        self.bones: List[BoneEntry] = [] #public List<BoneEntry>  new List<BoneEntry>();

class BoneData:
    def __init__(self):
        self.nbBones: int = 0 # public uint

        self.jointNames: List[str] = [] #public List<string>  new List<string>();
        self.boneMatrices: List[MMatrix] = [] #public List<Matrix>  new List<Matrix>();