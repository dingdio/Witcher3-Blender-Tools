from CR2W.Types.VariousTypes import (CNAME,
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


class CMesh(object):
    """docstring for CMesh."""

    def __init__(self, CR2WFILE):
        super(CMesh, self).__init__()
        self.CR2WFILE = CR2WFILE
        # self.baseResourceFilePath = False
        # self.navigationObstacle = False
        # self.collisionMesh = False
        # self.useExtraStreams = False
        # self.generalizedMeshRadius = False
        # self.mergeInGlobalShadowMesh = False
        # self.isOccluder = False
        # self.smallestHoleOverride = False
        # self.chunks = False
        # self.rawVertices = False
        # self.rawIndices = False
        # self.isStatic = False
        # self.entityProxy = False
        # self.cookedData = False
        # self.soundInfo = False
        # self.internalVersion = False
        # self.chunksBuffer = False
        

    def Create(arg):
        pass

    def Read(self, f, size):
        self.chunkgroupIndeces = CBufferVLQInt32(self.CR2WFILE, CPaddedBuffer, CUInt16)
        self.chunkgroupIndeces.Read(f, 0)
        self.boneNames = CBufferVLQInt32(self.CR2WFILE, CNAME_INDEX)
        self.boneNames.Read(f, 0)
        self.Bonematrices = CBufferVLQInt32(self.CR2WFILE, CMatrix4x4)
        self.Bonematrices.Read(f, 0)
        self.Block3 = CBufferVLQInt32(self.CR2WFILE, CFloat)
        self.Block3.Read(f, 0)
        self.BoneIndecesMappingBoneIndex = CBufferVLQInt32(self.CR2WFILE, CUInt32)
        self.BoneIndecesMappingBoneIndex.Read(f, 0)

    def Write(arg):
        pass
