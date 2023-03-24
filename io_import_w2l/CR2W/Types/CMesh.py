from ..Types.VariousTypes import (CNAME,
                                     CNAME_INDEX,
                                     NAME,
                                     CBufferVLQInt32,
                                     CFloat,
                                     CMatrix4x4,
                                     CPaddedBuffer,
                                     CUInt16,
                                     CUInt32)


class CResource(object):
    """docstring for CResource."""

    def __init__(self):
        super(CResource, self).__init__()
        self.importFile = False
        self.importFileTimeStamp = False


class CMeshTypeResource(CResource):
    """docstring for CMeshTypeResource."""

    def __init__(self):
        super(CMeshTypeResource, self).__init__()
        self.materialNames = False
        self.authorName = False
        self.materials = False
        self.boundingBox = False
        self.autoHideDistance = False
        self.isTwoSided = False

    def Create(arg):
        pass

    def Read(arg):
        pass

    def Write(arg):
        pass

class CMesh(CMeshTypeResource):
    """docstring for CMesh."""

    def __init__(self, CR2WFILE):
        #super(CMesh, self).__init__()
        self.__CR2WFILE = CR2WFILE
        
        # self.importFile = False
        # self.importFileTimeStamp = False
        # self.materialNames = False
        # self.materials = None # Type="array:2,0,handle:IMaterial"
        # self.boundingBox = None # Type="Box"
        # self.autoHideDistance = None # Type="Float"
        # self.isTwoSided = None # Type="Bool"
        # self.collisionMesh = None # Type="handle:CCollisionMesh"
        # self.useExtraStreams = None # Type="Bool"
        # self.generalizedMeshRadius = None # Type="Float"
        # self.mergeInGlobalShadowMesh = None # Type="Bool"
        # self.isOccluder = None # Type="Bool"
        # self.smallestHoleOverride = None # Type="Float"
        # self.chunks = None # Type="array:2,0,SMeshChunkPacked"
        # self.rawVertices = None # Type="DeferredDataBuffer"
        # self.rawIndices = None # Type="DeferredDataBuffer"
        # self.isStatic = None # Type="Bool"
        # self.entityProxy = None # Type="Bool"
        # self.cookedData = None # Type="SMeshCookedData"
        # self.soundInfo = None # Type="ptr:SMeshSoundInfo"
        # self.internalVersion = None # Type="Uint8"
        # self.chunksBuffer = None # Type="DeferredDataBuffer"

        #arrays
        self.ChunkgroupIndeces = CBufferVLQInt32(self.__CR2WFILE, CPaddedBuffer, CUInt16, theName= "ChunkgroupIndeces")
        self.BoneNames = CBufferVLQInt32(self.__CR2WFILE, CNAME_INDEX, theName= "BoneNames")
        self.Bonematrices = CBufferVLQInt32(self.__CR2WFILE, CMatrix4x4, theName= "Bonematrices")
        self.Block3 = CBufferVLQInt32(self.__CR2WFILE, CFloat, theName= "Block3")
        self.BoneIndecesMappingBoneIndex = CBufferVLQInt32(self.__CR2WFILE, CUInt32, theName= "BoneIndecesMappingBoneIndex")
        

    def Create(arg):
        pass

    def Read(self, f, size):
        self.ChunkgroupIndeces.Read(f, 0)
        self.BoneNames.Read(f, 0)
        self.Bonematrices.Read(f, 0)
        self.Block3.Read(f, 0)
        self.BoneIndecesMappingBoneIndex.Read(f, 0)

    def Write(arg):
        pass

    def WriteJson(arg):
        pass