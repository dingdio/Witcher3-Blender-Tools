from pathlib import Path
import os
from typing import List

from .bin_helpers import ReadBit6, ReadVLQInt32

from .Types import CMesh
from .bStream import *

from .Types.VariousTypes import CNAME, CNAME_INDEX, NAME, CBufferVLQInt32, CColor, CFloat, CMatrix4x4, CPaddedBuffer, CUInt16, CUInt32
from .w3_types import Vector3D, w2rig
from .CR2W_types import PROPERTY, SMeshChunkPacked, getCR2W, W_CLASS
from .Types.BlenderMesh import CommonData
from .Types.SBufferInfos import MMatrix, SBufferInfos, SVertexBufferInfos, SMeshInfos, EMeshVertexType, VertexSkinningEntry
from .setup_logging import *
log = logging.getLogger(__name__)

class MeshData(object):
    """docstring for MeshData."""
    def __init__(self):
        self.vertex3DCoords: List[object] = []
        self.UV_vertex3DCoords: List[object] = []
        self.UV2_vertex3DCoords: List[object] = []
        self.tangent_vector: List[object] = []
        self.extra_vectors = []
        self.faces = []
        self.normals = []
        self.normalsAll = []
        self.skinningVerts = []
        self.vertexColor = []
        self.meshInfo = [SMeshInfos]

    #!Not currently in use
    def split_data(self):
        num_verts = 65534
        num_splits = (len(self.vertex3DCoords) + num_verts - 1) // num_verts
        split_data = []
        vertex_faces = {}
        used_faces = set()
        for i, face in enumerate(self.faces):
            for vertex_index in face:
                if vertex_index not in vertex_faces:
                    vertex_faces[vertex_index] = set()
                vertex_faces[vertex_index].add(i)

        for i in range(num_splits):
            split_mesh = MeshData()
            start_index = i * num_verts
            end_index = min((i + 1) * num_verts, len(self.vertex3DCoords))
            split_mesh.vertex3DCoords = self.vertex3DCoords[start_index:end_index]
            split_mesh.UV_vertex3DCoords = self.UV_vertex3DCoords[start_index:end_index]
            split_mesh.UV2_vertex3DCoords = self.UV2_vertex3DCoords[start_index:end_index]
            split_mesh.tangent_vector = self.tangent_vector[start_index:end_index]
            split_mesh.extra_vectors = self.extra_vectors[start_index:end_index]
            split_mesh.faces = self.faces[start_index:end_index]
            split_mesh.normals = self.normals[start_index:end_index]
            split_mesh.normalsAll = self.normalsAll[start_index:end_index*3]
            split_mesh.skinningVerts = self.skinningVerts[start_index:end_index]
            split_mesh.vertexColor = self.vertexColor[start_index:end_index]
            faces = []
            for idx in reversed(range(start_index, end_index)):
                for face_index in vertex_faces.get(idx, []):
                    if face_index not in used_faces:
                        faces.append(self.faces[face_index])
                        used_faces.add(face_index)
            split_mesh.faces = self.faces[start_index:len(faces)]
            split_mesh.meshInfo = SMeshInfos()
            split_mesh.meshInfo.numVertices = self.meshInfo
            split_mesh.meshInfo.numIndices = self.meshInfo
            split_mesh.meshInfo.numBonesPerVertex = self.meshInfo
            split_mesh.meshInfo.firstVertex = self.meshInfo
            split_mesh.meshInfo.firstIndex = self.meshInfo
            split_mesh.meshInfo.vertexType = self.meshInfo
            split_mesh.meshInfo.materialID = self.meshInfo
            split_mesh.meshInfo.lod = self.meshInfo
            split_mesh.meshInfo.distance = self.meshInfo
            split_data.append(split_mesh)
        return split_data


def lin2srgb(lin):
    if lin > 0.0031308:
        s = 1.055 * (pow(lin, (1.0 / 2.4))) - 0.055
    else:
        s = 12.92 * lin
    return s

def srgb2lin(s):
    if s <= 0.0404482362771082:
        lin = s / 12.92
    else:
        lin = pow(((s + 0.055) / 1.055), 2.4)
    return lin

#!### WITCHER 2 CLASSES

from typing import List

class TW2_LOD:
    def __init__(self):
        self.submeshesIds: List[int] = []
        self.distancePC: float = 0.0
        self.distanceXenon: float = 0.0
        self.useOnPC: bool = False
        self.useOnXenon: bool = False
class SubmeshData:
    def __init__(self):
        self.vertexType = EMeshVertexType.EMVT_STATIC
        self.vertexType_w2: int = 0
        self.verticesStart: int = 0
        self.verticesCount: int = 0
        self.indicesStart: int = 0
        self.indicesCount: int = 0
        self.bonesId: List[int] = []
        self.materialID: int = 0

        self.lod = 0
        self.distance = 0


def load_bin_mesh(filename, keep_lod_meshes = True, keep_proxy_meshes = False):
    #OPTIONS
    cToLin = True
    read_lods = keep_lod_meshes

    #raise NotImplementedError
    log.info('FileLoading: '+ filename)

    # with open(filename,"rb") as meshFileReader:
    #     meshFile = getCR2W(meshFileReader)
    #     #f.close()
    f = open(filename,"rb")
    meshFile = getCR2W(f)
    meshName = Path(meshFile.fileName).stem
    if "proxy" in meshName and keep_proxy_meshes:
        keep_lod_meshes = True

    CData:CommonData = CommonData()
    CData.modelPath = meshFile.fileName
    CData.modelName = meshName
    CData.meshDataAllMeshes = []
    bonePositions: List[Vector3D] = []

    bufferInfos:SBufferInfos = SBufferInfos()


    #?###################?#
    #?#### WITCHER 2 ####?#
    #?###################?#
    if meshFile.HEADER.version <= 115: #? WITCHER 2
        f.seek(0)
        br:bStream = bStream(data = f.read())
        f.close()

        chunk: W_CLASS
        for chunk in meshFile.CHUNKS.CHUNKS:
            if chunk.Type == "CMesh":
                the_materials = chunk.GetVariableByName("materials")
                the_material_names_chunk = chunk.GetVariableByName("materialNames")
                the_material_names = []
                if the_material_names_chunk:
                    for mat in the_material_names_chunk.elements:
                        the_material_names.append(meshName+"_"+mat.String)
                else:
                    for idx in range(the_materials.Count):
                        the_material_names.append("Material"+str(idx))

                #go to buffer start
                br.seek(chunk.PROPS[-1].dataEnd)

                is_uncooked = False
                yes = br.read(8)
                if (yes[3] != 5): #
                    is_uncooked = True
                    br.seek(-8,1)
                    #log.critical('Error reading LODs')
                    #return
                
                if is_uncooked:
                    ############! UNCOOKED STUFF
                    yes = br.read(7)
                    nbSubMesh = br.readUByte()

                    subMeshesData:List[SubmeshData] = []

                    def readUncookedSubmeshData(br:bStream):
                        submesh = SubmeshData()
                        submesh.vertexType_w2 = br.readUInt16() # TODO check this
                        if submesh.vertexType_w2 in [1,5] :
                            submesh.vertexType = EMeshVertexType.EMVT_SKINNED
                        
                        submesh.materialID = br.readUInt32()
                        stuffmore = br.read(1)
                        efaewf = br.tell()
                        submesh.verticesCount = br.readUInt32() # 4240
                        submesh.indicesCount = br.readUInt32() #21651

                        meshInfo = SMeshInfos()
                        meshInfo.numVertices = submesh.verticesCount
                        meshInfo.numIndices = submesh.indicesCount
                        #!TODO vertexSize check
                        #br.seek(submesh.verticesCount * 112, os.SEEK_CUR)
                        #?##########################
                        #?####### READ VERTS #######
                        #?##########################
                        final_meshdata:MeshData = MeshData()
                        final_meshdata.meshInfo = meshInfo
                        CData.meshDataAllMeshes.append(final_meshdata)

                        #?#################?#
                        #?### Vertices ####?#
                        #?#################?#
                        numVertices_count = ReadVLQInt32(br.fhandle)
                        for i in range(meshInfo.numVertices):
                            x = br.readFloat()
                            y = br.readFloat()
                            z = br.readFloat()
                            skinningData =[]
                            for j in range(meshInfo.numBonesPerVertex):
                                skinningData.append(br.readUByte())

                            for j in range(meshInfo.numBonesPerVertex):
                                boneId = skinningData[j]
                                boneId_idx = j
                                fweight = br.readFloat()

                                if (fweight != 0.0):
                                    vertexSkinningEntry = VertexSkinningEntry()
                                    vertexSkinningEntry.boneId = boneId
                                    vertexSkinningEntry.boneId_idx = boneId_idx
                                    vertexSkinningEntry.meshBufferId = 0
                                    vertexSkinningEntry.vertexId = int(i)
                                    vertexSkinningEntry.strength = fweight
                                    CData.w3_DataCache.vertices.append(vertexSkinningEntry)
                                    final_meshdata.skinningVerts.append(vertexSkinningEntry)

                            ## NORMALS
                            fx = br.readFloat()
                            fy = br.readFloat()
                            fz = br.readFloat()

                            final_meshdata.normals.append([fx, fy, fz])
                            final_meshdata.normalsAll.append(fx)
                            final_meshdata.normalsAll.append(fy)
                            final_meshdata.normalsAll.append(fz)

                            if cToLin:
                                final_meshdata.vertexColor.append([
                                    srgb2lin( br.readUByte()/ 255 ),
                                    srgb2lin( br.readUByte()/ 255 ),
                                    srgb2lin( br.readUByte()/ 255 ),
                                    br.readUByte()/ 255])
                            else:
                                final_meshdata.vertexColor.append([
                                    br.readUByte()/ 255,
                                    br.readUByte()/ 255,
                                    br.readUByte()/ 255,
                                    br.readUByte()/ 255])

                            # UVS
                            uf = br.readFloat()
                            vf = br.readFloat()
                            final_meshdata.UV_vertex3DCoords.append([uf,(vf*-1)+1]) # flip

                            uf2 = br.readFloat()
                            vf2 = br.readFloat()
                            final_meshdata.UV2_vertex3DCoords.append([uf2,(vf2*-1)+1]) # flip

                            fsx = br.readFloat()
                            fsy = br.readFloat()
                            fsz = br.readFloat()
                            final_meshdata.tangent_vector.append([fsx, fsy, fsz])

                            f_0 = br.readFloat()
                            f_1 = br.readFloat()
                            f_2 = br.readFloat()
                            f_3 = br.readFloat()
                            f_4 = br.readFloat()
                            f_5 = br.readFloat()
                            f_6 = br.readFloat()
                            f_7 = br.readFloat()
                            f_8 = br.readFloat()
                            final_meshdata.extra_vectors.append([f_0,f_1,f_2,f_3,f_4,f_5,f_6,f_7,f_8])

                            vertex3DCoord = [x,y,z]
                            final_meshdata.vertex3DCoords.append(vertex3DCoord)


                        lastVertOffset = br.tell() #- bufferInfo.offset
                        #?#########################
                        #?#########################
                        #?#########################
                        vertend = br.tell()

                        #?#################?#
                        #?#### Indices ####?#
                        #?#################?#
                        numIndices_count = ReadVLQInt32(br.fhandle)
                        indices = [] #List<ushort>
                        for i in range(meshInfo.numIndices):
                            indices.append(0)

                        for i in range(meshInfo.numIndices):
                            index = br.readUInt16()
                            if (i % 3 == 0):
                                indices[i] = index
                            elif (i % 3 == 1):
                                indices[i + 1] = index
                            elif (i % 3 == 2):
                                indices[i - 1] = index
                        i = 0
                        while i < int(meshInfo.numIndices):
                            final_meshdata.faces.append([indices[i],indices[i+1],indices[i+2]])
                            i+=3
                        lastIOffset = br.tell()

                        bonesId_count = br.readByte()
                        for _ in range(bonesId_count):
                            submesh.bonesId.append(br.readUInt16())

                        final_meshdata.meshInfo = submesh
                        return submesh

                    subMeshesData = [readUncookedSubmeshData(br) for _ in range(nbSubMesh)]
                    CData.meshInfos = subMeshesData


                    #!!!!!!!!!!!!!!!!!!!!!!!!!!!!

                ##################
                ###### LODS ######
                ##################
                LODs = []

                lodCount = br.readUByte()
                for _ in range(lodCount):
                    lod = TW2_LOD()
                    nbSubmeshes = br.readUByte()

                    buffer = CPaddedBuffer(meshFile,CUInt16)
                    chunk.CMesh.ChunkgroupIndeces.elements.append(buffer)
                    for _ in range(nbSubmeshes):
                        val_ = CUInt16(meshFile)
                        val_.Read(br.fhandle,0)
                        lod.submeshesIds.append(val_.val)
                        buffer.elements.append(val_)

                    lod.distancePC = br.readFloat()
                    if meshFile.HEADER.version > 107:
                        lod.distanceXenon = br.readFloat()
                        lod.useOnPC = br.readUByte()
                        lod.useOnXenon = br.readUByte()
                    buffer.padding = lod.distancePC

                    LODs.append(lod)

                ##########################
                ###### W2 BONE DATA ######
                ##########################
                nbBones = ReadBit6(br.fhandle) #br.readUByte()
                hasBones = nbBones > 0
                boneNames:List[str] = []

                if hasBones:
                    for _ in range(nbBones):
                        boneMatrix = CMatrix4x4(meshFile)
                        boneMatrix.Read(br.fhandle, 0)
                        chunk.CMesh.Bonematrices.elements.append(boneMatrix)

                        boneName = CNAME_INDEX(meshFile)
                        boneName.Read(br.fhandle, 0)
                        chunk.CMesh.BoneNames.elements.append(boneName)

                        block3 = CFloat(meshFile)
                        block3.Read(br.fhandle, 0)
                        chunk.CMesh.Block3.elements.append(block3)

                chunk.CMesh.BoneIndecesMappingBoneIndex.Read(br.fhandle,0)
                BoneIndecesMappingBoneIndex = [el.val for el in chunk.CMesh.BoneIndecesMappingBoneIndex.elements] if hasattr(chunk, "CMesh") and hasattr(chunk.CMesh, "BoneIndecesMappingBoneIndex") and hasattr(chunk.CMesh.BoneIndecesMappingBoneIndex, "elements") else []

                for idx, bone in enumerate(chunk.CMesh.BoneNames.elements):
                    boneNames.append(bone.value.name.value)

                ###! REPEATED CODE
                ChunkgroupIndeces = chunk.CMesh.ChunkgroupIndeces
                for idx, PaddedBuffer in enumerate(ChunkgroupIndeces.elements):#list of lods

                    if PaddedBuffer.elements:
                        for chunkIndex in PaddedBuffer.elements:#chunk list for single lod
                            if is_uncooked:
                                CData.meshInfos[chunkIndex.val].lod = idx
                                CData.meshInfos[chunkIndex.val].distance = PaddedBuffer.padding
                    else:
                        #for lod levels with no chunks assigned create a blank meshinfo
                        meshInfo:SMeshInfos = SMeshInfos()
                        meshInfo.lod = idx
                        meshInfo.distance = PaddedBuffer.padding
                        CData.meshInfos.append(meshInfo)
                CData.meshInfos.sort(key=lambda x: x.lod)
                # bone names and matrices
                boneNames = chunk.CMesh.BoneNames
                bonematrices = chunk.CMesh.Bonematrices
                CData.boneData.nbBones = len(boneNames.elements)
                for i in range(CData.boneData.nbBones):
                    name: CNAME_INDEX = boneNames.elements[i]
                    CData.boneData.jointNames.append(name.value.name.value)

                    cmatrix: CMatrix4x4 = bonematrices.elements[i]
                    #TODO LOOK AT MATRIX PREPRATION BEFORE USING IN BLENDER
                    CData.boneData.boneMatrices.append(cmatrix)
                CData.boneData.BoneIndecesMappingBoneIndex = [el.val for el in chunk.CMesh.BoneIndecesMappingBoneIndex.elements] if hasattr(chunk, "CMesh") and hasattr(chunk.CMesh, "BoneIndecesMappingBoneIndex") and hasattr(chunk.CMesh.BoneIndecesMappingBoneIndex, "elements") else []
                CData.boneData.Block3 = [el.val for el in chunk.CMesh.Block3.elements] if hasattr(chunk, "CMesh") and hasattr(chunk.CMesh, "Block3") and hasattr(chunk.CMesh.Block3, "elements") else []

                ###! REPEATED CODE

                materialIds = []
                def loadSubmesh(br:bStream, submesh:SubmeshData, meshIndicesOffset:int, materialIds: List[int], boneNames:List[str]):
                    from .dc_mesh import MeshData
                    final_meshdata:MeshData = MeshData()
                    final_meshdata.meshInfo = submesh
                    CData.meshDataAllMeshes.append(final_meshdata)
                    submeshStartPos = br.tell()

                    vertexSize = 0
                    hasSecondUVLayer = False
                    isSkinned = False

                    if submesh.vertexType_w2 == 0:
                        vertexSize = 36
                    elif submesh.vertexType_w2 == 6:
                        vertexSize = 44
                        hasSecondUVLayer = True
                    elif submesh.vertexType_w2 in [9, 5]:
                        vertexSize = 60
                    elif submesh.vertexType_w2 in [1, 11]:
                        vertexSize = 44
                        isSkinned = True
                        submesh.vertexType = EMeshVertexType.EMVT_SKINNED
                    elif submesh.vertexType_w2 == 7:
                        vertexSize = 52
                        isSkinned = True
                        submesh.vertexType = EMeshVertexType.EMVT_SKINNED
                        hasSecondUVLayer = True
                    else:
                        vertexSize = 52
                        isSkinned = True
                        submesh.vertexType = EMeshVertexType.EMVT_SKINNED

                    log.critical(f"submesh (vertype: {submesh.vertexType_w2}, vertsize: {vertexSize}, vertStart = {br.tell()})")
                    #?#################?#
                    #?### Vertices ####?#
                    #?#################?#
                    br.seek(submeshStartPos + submesh.verticesStart * vertexSize)
                    for i in range(submesh.verticesCount):
                        vertexAdress = br.tell()
                        position = [br.readFloat() for _ in range(3)]
                        final_meshdata.vertex3DCoords.append(position)

                        # Weights
                        if isSkinned:
                            weightsData = [br.readUByte() for _ in range(8)] #readDataArray(file, 8, "u8")
                            for vertexWeightsId in range(4): #numBonesPerVertex
                                strength = weightsData[vertexWeightsId + 4]
                                if strength != 0:
                                    boneId = submesh.bonesId[weightsData[vertexWeightsId]]
                                    boneName = CData.boneData.jointNames[boneId]

                                    vertexSkinningEntry = VertexSkinningEntry()
                                    vertexSkinningEntry.boneId = boneId
                                    vertexSkinningEntry.boneId_idx = vertexWeightsId #boneId_idx
                                    vertexSkinningEntry.meshBufferId = 0
                                    vertexSkinningEntry.vertexId = int(i)
                                    vertexSkinningEntry.strength = float(strength) / 255.0
                                    CData.w3_DataCache.vertices.append(vertexSkinningEntry)
                                    final_meshdata.skinningVerts.append(vertexSkinningEntry)

                        # Normals and color
                        bytes = [br.readUByte() for _ in range(4)]
                        fx = (bytes[0] - 127) / 127.0
                        fy = (bytes[1] - 127) / 127.0
                        fz = (bytes[2] - 127) / 127.0

                        if cToLin:
                            final_meshdata.vertexColor.append([
                                srgb2lin( br.readUByte()/ 255 ),
                                srgb2lin( br.readUByte()/ 255 ),
                                srgb2lin( br.readUByte()/ 255 ),
                                br.readUByte()/ 255])
                        else:
                            final_meshdata.vertexColor.append([
                                br.readUByte()/ 255,
                                br.readUByte()/ 255,
                                br.readUByte()/ 255,
                                br.readUByte()/ 255])

                        final_meshdata.normals.append([fx, fy, fz])
                        final_meshdata.normalsAll.append(fx)
                        final_meshdata.normalsAll.append(fy)
                        final_meshdata.normalsAll.append(fz)

                        # UVS
                        uf = br.readFloat()
                        vf = br.readFloat()
                        final_meshdata.UV_vertex3DCoords.append([uf,(vf*-1)+1]) # flip

                        if hasSecondUVLayer:
                            uf = br.readFloat()
                            vf = br.readFloat()
                            final_meshdata.UV2_vertex3DCoords.append([uf,(vf*-1)+1]) # flip

                        br.seek(vertexAdress + vertexSize)


                    #?#################?#
                    #?#### Indices ####?#
                    #?#################?#
                    br.seek(submeshStartPos + meshIndicesOffset + submesh.indicesStart * 2) #br.seek(bufferInfo.offset + lastIOffset, 0)
                    indices = []
                    for i in range(submesh.indicesCount): #! range(meshInfo.numIndices):
                        indices.append(0)

                    for i in range(submesh.indicesCount): #! range(meshInfo.numIndices):
                        index = br.readUInt16()

                        # Indice need to be inversed for the normals
                        if (i % 3 == 0):
                            indices[i] = index
                        elif (i % 3 == 1):
                            indices[i + 1] = index
                        elif (i % 3 == 2):
                            indices[i - 1] = index
                    i = 0
                    while i < int(submesh.indicesCount): #! int(meshInfo.numIndices):
                        final_meshdata.faces.append([indices[i],indices[i+1],indices[i+2]])
                        i+=3
                    lastIOffset = br.tell() - meshIndicesOffset #! bufferInfo.offset

                def readSubmeshData(br):
                    submesh = SubmeshData()
                    submesh.vertexType_w2 = br.readUByte()

                    submesh.verticesStart = br.readUInt32()
                    submesh.indicesStart = br.readUInt32()
                    submesh.verticesCount = br.readUInt32()
                    submesh.indicesCount = br.readUInt32()

                    bonesCount = br.readByte()
                    if (bonesCount > 0):
                        submesh.bonesId = [br.readUInt16() for _ in range(bonesCount)]

                    submesh.materialID = br.readUInt32()
                    submesh.lod = 0
                    submesh.distance = 0

                    return submesh

                def loadSubmeshes(br, LODs:List[TW2_LOD], materialIds: List, boneNames):
                    subMeshesStartAdress = br.tell()
                    subMeshesInfosOffset = br.readUInt32()
                    br.seek(subMeshesStartAdress + subMeshesInfosOffset)
                    br.seek(8, os.SEEK_CUR)
                    meshIndicesOffset = br.readUInt32()
                    br.seek(12, os.SEEK_CUR)
                    nbSubMesh = br.readUByte()

                    subMeshesData:List[SubmeshData] = []
                    subMeshesData = [readSubmeshData(br) for _ in range(nbSubMesh)]
                    CData.meshInfos = subMeshesData

                    #fix submesh lod
                    for lod_idx, lod in enumerate(LODs):
                        for id in lod.submeshesIds:
                            try:
                                subMeshesData[id].lod = lod_idx
                                subMeshesData[id].distance = lod.distancePC
                            except Exception as e:
                                raise e

                    for i in range(nbSubMesh):
                        if not keep_lod_meshes and i not in LODs[0].submeshesIds:
                            continue
                        br.seek(subMeshesStartAdress + 4)
                        loadSubmesh(br, subMeshesData[i], meshIndicesOffset, materialIds, boneNames)
                if is_uncooked:
                    pass
                else:
                    loadSubmeshes(br, LODs, materialIds, boneNames) # FOR COOKED
                break #!TODO load multiple embedded cmesh not just first

        return (CData, bufferInfos, the_material_names, the_materials, meshName, meshFile)

    #?###################?#
    #?#### WITCHER 3 ####?#
    #?###################?#
    chunk: W_CLASS
    for chunk in meshFile.CHUNKS.CHUNKS:
        if chunk.Type == "CMesh":
            the_materials = chunk.GetVariableByName("materials")
            the_material_names_chunk = chunk.GetVariableByName("materialNames")
            the_material_names = []
            if the_material_names_chunk:
                for mat in the_material_names_chunk.elements:
                    the_material_names.append(meshName+"_"+mat.String)
            else:
                for idx in range(the_materials.Count):
                    the_material_names.append(meshName+"_"+"Material"+str(idx))



            #####################
            #!  GATHER MESH INFOS
            #
            #####################


            # *************** CHECK UNCOOKED BUFFER ***************
            rawVertices = chunk.GetVariableByName("rawVertices")
            rawIndices = chunk.GetVariableByName("rawIndices")
            # *************** CHECK UNCOOKED BUFFER ***************
            vertexBufferInfos: List[SVertexBufferInfos] = []
            cookedDatas = chunk.GetVariableByName("cookedData")
            for cookedData in cookedDatas.More:

                # renderChunks appear in meshes that have been uncooked. These offsets don't matter in that case.
                if cookedData.theName == "renderChunks":
                    b = bytearray(cookedData.value)
                    br = bStream(data = b)

                    nbBuffers = br.readByte()
                    for _ in range(nbBuffers):
                        buffInfo:SVertexBufferInfos = SVertexBufferInfos()
                        buffInfo.firstunk = br.readByte()#br.seek(1,1)# br.BaseStream.Position += 1; // Unknown
                        buffInfo.verticesCoordsOffset = br.readUInt32()
                        buffInfo.uvOffset = br.readUInt32()
                        buffInfo.normalsOffset = br.readUInt32()
                        buffInfo.vcOffset_and_uv2 = br.readUInt32()
                        buffInfo.someOffset = br.readUInt32()
                        buffInfo.ukb = br.readByte()#br.seek(1,1)#br.BaseStream.Position += 9 # Unknown
                        buffInfo.indicesOffset = br.readUInt32()
                        buffInfo.ukb2 = br.readByte()#br.seek(1,1)#br.BaseStream.Position += 1 # 0x1D
                        buffInfo.nbVertices = br.readUInt16()
                        buffInfo.nbIndices = br.readUInt32()
                        buffInfo.materialID = br.readByte()
                        buffInfo.someByte1 = br.readByte()
                        buffInfo.someByte2 = br.readByte()
                        buffInfo.someByte3lod = br.readByte() # not lod ?
                        vertexBufferInfos.append(buffInfo)
                elif cookedData.theName == "indexBufferOffset":
                    bufferInfos.indexBufferOffset = cookedData.Value
                elif cookedData.theName == "indexBufferSize":
                    bufferInfos.indexBufferSize = cookedData.Value
                elif cookedData.theName == "vertexBufferOffset":
                    bufferInfos.vertexBufferOffset = cookedData.Value
                elif cookedData.theName == "vertexBufferSize":
                    bufferInfos.vertexBufferSize = cookedData.Value
                elif cookedData.theName == "quantizationOffset":
                    bufferInfos.quantizationOffset.x = cookedData.More[0].Value
                    bufferInfos.quantizationOffset.y = cookedData.More[1].Value
                    bufferInfos.quantizationOffset.z = cookedData.More[2].Value
                elif cookedData.theName == "quantizationScale":
                    bufferInfos.quantizationScale.x = cookedData.More[0].Value
                    bufferInfos.quantizationScale.y = cookedData.More[1].Value
                    bufferInfos.quantizationScale.z = cookedData.More[2].Value
                elif cookedData.theName == "bonePositions":
                    try:
                        for item in cookedData.More:
                            if cookedData.Count == 1: # TODO fix how 1 element arrays are returned
                                item = cookedData
                                item.MoreProps = item.More
                            if (len(item.MoreProps) == 4):
                                pos = Vector3D(0,0,0)
                                pos.x = item.MoreProps[0].Value
                                pos.y = item.MoreProps[1].Value
                                pos.z = item.MoreProps[2].Value
                                bonePositions.append(pos)
                    except Exception as e:
                        raise e

            bufferInfos.verticesBuffer = vertexBufferInfos

            meshChunks = chunk.GetVariableByName("chunks")
            for meshChunk in meshChunks.chunks.elements:
                meshInfo:SMeshInfos = SMeshInfos()

                mi: PROPERTY
                for mi in meshChunk.MoreProps:
                    if mi.theName == "numVertices":
                        meshInfo.numVertices = mi.Value
                    elif mi.theName == "numIndices":
                        meshInfo.numIndices = mi.Value
                    elif mi.theName == "numBonesPerVertex":
                        meshInfo.numBonesPerVertex = mi.Value
                    elif mi.theName == "firstVertex":
                        meshInfo.firstVertex = mi.Value
                    elif mi.theName == "firstIndex":
                        meshInfo.firstIndex = mi.Value
                    elif mi.theName == "vertexType":
                        if (mi.Index.String == "MVT_StaticMesh"):
                            meshInfo.vertexType = EMeshVertexType.EMVT_STATIC
                        elif (mi.Index.String == "MVT_SkinnedMesh"):
                            meshInfo.vertexType = EMeshVertexType.EMVT_SKINNED
                    elif mi.theName == "materialID":
                        meshInfo.materialID = mi.Value
                CData.meshInfos.append(meshInfo)



            ##################
            #!  FINISH CODE  #
            #
            ##################
            CData.autohideDistance = chunk.GetVariableByName("autoHideDistance").Value if chunk.GetVariableByName("autoHideDistance") else None
            CData.isTwoSided = chunk.GetVariableByName("isTwoSided").Value if chunk.GetVariableByName("isTwoSided") else None
            CData.useExtraStreams = chunk.GetVariableByName("useExtraStreams").Value if chunk.GetVariableByName("useExtraStreams") else None
            CData.mergeInGlobalShadowMesh = chunk.GetVariableByName("mergeInGlobalShadowMesh").Value if chunk.GetVariableByName("mergeInGlobalShadowMesh") else None
            CData.entityProxy = chunk.GetVariableByName("entityProxy").Value if chunk.GetVariableByName("entityProxy") else None
            CData.isStatic = chunk.GetVariableByName("isStatic").Value if chunk.GetVariableByName("isStatic") else False

            ChunkgroupIndeces = chunk.CMesh.ChunkgroupIndeces
            for idx, PaddedBuffer in enumerate(ChunkgroupIndeces.elements):#list of lods

                if PaddedBuffer.elements:
                    for chunkIndex in PaddedBuffer.elements:#chunk list for single lod
                        CData.meshInfos[chunkIndex.val].lod = idx
                        CData.meshInfos[chunkIndex.val].distance = PaddedBuffer.padding
                else:
                    #for lod levels with no chunks assigned create a blank meshinfo
                    meshInfo:SMeshInfos = SMeshInfos()
                    meshInfo.lod = idx
                    meshInfo.distance = PaddedBuffer.padding
                    CData.meshInfos.append(meshInfo)
            CData.meshInfos.sort(key=lambda x: x.lod)
            # bone names and matrices
            boneNames = chunk.CMesh.BoneNames
            bonematrices = chunk.CMesh.Bonematrices
            CData.boneData.nbBones = len(boneNames.elements)
            for i in range(CData.boneData.nbBones):
                name: CNAME_INDEX = boneNames.elements[i]
                CData.boneData.jointNames.append(name.value.name.value)

                cmatrix: CMatrix4x4 = bonematrices.elements[i]
                #TODO LOOK AT MATRIX PREPRATION BEFORE USING IN BLENDER
                CData.boneData.boneMatrices.append(cmatrix)
            CData.boneData.BoneIndecesMappingBoneIndex = [el.val for el in chunk.CMesh.BoneIndecesMappingBoneIndex.elements] if hasattr(chunk, "CMesh") and hasattr(chunk.CMesh, "BoneIndecesMappingBoneIndex") and hasattr(chunk.CMesh.BoneIndecesMappingBoneIndex, "elements") else []
            CData.boneData.Block3 = [el.val for el in chunk.CMesh.Block3.elements] if hasattr(chunk, "CMesh") and hasattr(chunk.CMesh, "Block3") and hasattr(chunk.CMesh.Block3, "elements") else []

    # TODO BETTER CHECK OF COOKED/UNCOOKED
    if rawVertices:
        #=====================================================================#
        #                READ UNCOOKED BUFFER INFOS                           #
        #                                                                     #
        #=====================================================================#
        f.seek(0)
        br = bStream(data = f.read())
        f.close()
        lastVertOffset = 0
        lastIOffset = 0
        for idx, meshInfo in enumerate(CData.meshInfos):
            if (meshInfo.lod == 0 or read_lods):
                final_meshdata:MeshData = MeshData()
                final_meshdata.meshInfo = meshInfo
                CData.meshDataAllMeshes.append(final_meshdata)

                #?#################?#
                #?### Vertices ####?#
                #?#################?#
                vertBufferIndex = rawVertices.ValueA
                bufferInfo = meshFile.CR2WBuffer[vertBufferIndex - 1]

                br.seek(bufferInfo.offset + lastVertOffset)
                for i in range(meshInfo.numVertices):
                    x = br.readFloat()
                    y = br.readFloat()
                    z = br.readFloat()
                #if (meshInfo.vertexType == EMeshVertexType.EMVT_SKINNED):
                    skinningData =[] #br.read(meshInfo.numBonesPerVertex * 2)
                    #ints_test = [x for x in skinningData]
                    for j in range(meshInfo.numBonesPerVertex):
                        skinningData.append(br.readUByte())

                    for j in range(meshInfo.numBonesPerVertex):
                        boneId = skinningData[j]
                        boneId_idx = j
                        fweight = br.readFloat()

                        if (fweight != 0.0):
                            vertexSkinningEntry = VertexSkinningEntry()
                            vertexSkinningEntry.boneId = boneId
                            vertexSkinningEntry.boneId_idx = boneId_idx
                            vertexSkinningEntry.meshBufferId = 0
                            vertexSkinningEntry.vertexId = int(i)
                            vertexSkinningEntry.strength = fweight
                            CData.w3_DataCache.vertices.append(vertexSkinningEntry)
                            final_meshdata.skinningVerts.append(vertexSkinningEntry)

                    ## NORMALS
                    fx = br.readFloat()
                    fy = br.readFloat()
                    fz = br.readFloat()

                    final_meshdata.normals.append([fx, fy, fz])
                    final_meshdata.normalsAll.append(fx)
                    final_meshdata.normalsAll.append(fy)
                    final_meshdata.normalsAll.append(fz)

                    #normals /= numpy.linalg.norm(normals, axis=-1)

                    if cToLin:
                        final_meshdata.vertexColor.append([
                            srgb2lin( br.readUByte()/ 255 ),
                            srgb2lin( br.readUByte()/ 255 ),
                            srgb2lin( br.readUByte()/ 255 ),
                            br.readUByte()/ 255])
                    else:
                        final_meshdata.vertexColor.append([
                            br.readUByte()/ 255,
                            br.readUByte()/ 255,
                            br.readUByte()/ 255,
                            br.readUByte()/ 255])

                    # UVS
                    uf = br.readFloat()
                    vf = br.readFloat()
                    #final_meshdata.UV_vertex3DCoords.append([uf,(vf*-1)+1]) # flip
                    final_meshdata.UV_vertex3DCoords.append([uf,(vf*-1)+1]) # flip

                    uf2 = br.readFloat()
                    vf2 = br.readFloat()
                    final_meshdata.UV2_vertex3DCoords.append([uf2,(vf2*-1)+1]) # flip

                    fsx = br.readFloat()
                    fsy = br.readFloat()
                    fsz = br.readFloat()
                    final_meshdata.tangent_vector.append([fsx, fsy, fsz])

                    f_0 = br.readFloat()
                    f_1 = br.readFloat()
                    f_2 = br.readFloat()
                    f_3 = br.readFloat()
                    f_4 = br.readFloat()
                    f_5 = br.readFloat()
                    f_6 = br.readFloat()
                    f_7 = br.readFloat()
                    f_8 = br.readFloat()
                    f_9 = br.readFloat()
                    f_10 = br.readFloat()
                    f_11 = br.readFloat()
                    f_12 = br.readFloat()
                    f_13 = br.readFloat()
                    f_14 = br.readFloat()
                    f_15 = br.readFloat()
                    f_16 = br.readFloat()
                    f_17 = br.readFloat()
                    f_18 = br.readFloat()
                    final_meshdata.extra_vectors.append([f_0,f_1,f_2,f_3,f_4,f_5,f_6,f_7,f_8,f_9,f_10,f_11,f_12,f_13,f_14,f_15,f_16,f_17,f_18])
                    #br.seek(76, 1)


                    # vertex3DCoord = [x * bufferInfos.quantizationScale.x + bufferInfos.quantizationOffset.x,
                    #                  y * bufferInfos.quantizationScale.y + bufferInfos.quantizationOffset.y,
                    #                  z * bufferInfos.quantizationScale.z + bufferInfos.quantizationOffset.z]
                    vertex3DCoord = [x,
                                     y,
                                     z ]
                    final_meshdata.vertex3DCoords.append(vertex3DCoord)
                lastVertOffset = br.tell() - bufferInfo.offset

                #?#################?#
                #?#### Indices ####?#
                #?#################?#
                # Load DeferredDataBuffer
                rawIndicesIndex = rawIndices.ValueA
                bufferInfo = meshFile.CR2WBuffer[rawIndicesIndex - 1]
                br.seek(bufferInfo.offset + lastIOffset, 0)#br.seek(bufferInfo.offset)
                indices = [] #List<ushort>
                for i in range(meshInfo.numIndices):
                    indices.append(0)

                for i in range(meshInfo.numIndices):
                    index = br.readUInt16()
                    #indices[i] = index
                    # Indice need to be inversed for the normals
                    if (i % 3 == 0):
                        indices[i] = index
                    elif (i % 3 == 1):
                        indices[i + 1] = index
                    elif (i % 3 == 2):
                        indices[i - 1] = index
                i = 0
                while i < int(meshInfo.numIndices):
                    final_meshdata.faces.append([indices[i],indices[i+1],indices[i+2]])
                    i+=3
                lastIOffset = br.tell() - bufferInfo.offset

        br.close()

    else:
        #=====================================================================#
        #                     READ COOKED BUFFER INFOS                        #
        #                                                                     #
        #=====================================================================#

        try:
            def_path = meshFile.fileName + ".1.buffer"
            f = open(def_path,"rb")
            br = bStream(data = f.read())
            f.close()
        except Exception as e:
            raise e
        lastVertOffset = 0
        lastIOffset = 0

        for meshInfo in CData.meshInfos:
            vBufferInf = SVertexBufferInfos()
            nbVertices = 0
            firstVertexOffset = 0
            nbIndices = 0
            firstIndiceOffset = 0
            for i in range(len(bufferInfos.verticesBuffer)):
                nbVertices += bufferInfos.verticesBuffer[i].nbVertices
                if (nbVertices > meshInfo.firstVertex):
                    vBufferInf = bufferInfos.verticesBuffer[i]
                    # the index of the first vertex in the buffer
                    firstVertexOffset = meshInfo.firstVertex - (nbVertices - vBufferInf.nbVertices)
                    break
            for i in range(len(bufferInfos.verticesBuffer)):
                nbIndices += bufferInfos.verticesBuffer[i].nbIndices
                if (nbIndices > meshInfo.firstIndex):
                    vBufferInf = bufferInfos.verticesBuffer[i]
                    firstIndiceOffset = meshInfo.firstIndex - (nbIndices - vBufferInf.nbIndices)
                    break
            # Load only best LOD
            if (meshInfo.lod == 0 or read_lods):
                final_meshdata:MeshData = MeshData()
                final_meshdata.meshInfo = meshInfo
                CData.meshDataAllMeshes.append(final_meshdata)

                vertexSize = 8
                if (meshInfo.vertexType == EMeshVertexType.EMVT_SKINNED):
                    vertexSize += meshInfo.numBonesPerVertex * 2

                br.seek(vBufferInf.verticesCoordsOffset + firstVertexOffset * vertexSize, 0)
                vertex3DCoords: List[object] = []
                for i in range(meshInfo.numVertices):
                    x = br.readUInt16()
                    y = br.readUInt16()
                    z = br.readUInt16()
                    w = br.readUInt16()
                    if (meshInfo.vertexType == EMeshVertexType.EMVT_SKINNED):
                        skinningData = br.read(meshInfo.numBonesPerVertex * 2)
                        #ints_test = [x for x in skinningData]

                        for j in range(meshInfo.numBonesPerVertex):
                            boneId = skinningData[j]
                            weight = skinningData[j + meshInfo.numBonesPerVertex]
                            fweight = weight / 255.0

                            if (weight != 0):
                                vertexSkinningEntry = VertexSkinningEntry()
                                vertexSkinningEntry.boneId = boneId
                                vertexSkinningEntry.meshBufferId = 0
                                vertexSkinningEntry.vertexId = i
                                vertexSkinningEntry.strength = fweight
                                CData.w3_DataCache.vertices.append(vertexSkinningEntry)
                                final_meshdata.skinningVerts.append(vertexSkinningEntry)

                    vertex3DCoord = [x / 65535 * bufferInfos.quantizationScale.x + bufferInfos.quantizationOffset.x,
                                     y / 65535 * bufferInfos.quantizationScale.y + bufferInfos.quantizationOffset.y,
                                     z / 65535 * bufferInfos.quantizationScale.z + bufferInfos.quantizationOffset.z]
                    final_meshdata.vertex3DCoords.append(vertex3DCoord)

                #### UVs
                br.seek(vBufferInf.uvOffset + firstVertexOffset * 4, 0)


                for i in range(meshInfo.numVertices):
                    uf = br.ReadHalfFloat()
                    vf = br.ReadHalfFloat()
                    final_meshdata.UV_vertex3DCoords.append([uf,(vf*-1)+1]) # flip

                br.seek(vBufferInf.normalsOffset + firstVertexOffset * 8)
                for i in range(meshInfo.numVertices):
                    bytesN = br.read(4) #normal
                    bytesT = br.read(4) #tangent

                    def read_normal(bytes):
                        x = ((bytes[0]&0b11111111) | ((bytes[1]&0b11) << 8)) #u16
                        y = ((bytes[1]&0b11111100) | ((bytes[2]&0b00001111) << 8)) >> 2 #u16
                        z = ((bytes[2]&0b11110000) | ((bytes[3]&0b00111111) << 8)) >> 4 #u16

                        fx = (x - 512) / 512.0; ##f32
                        fy = (y - 512) / 512.0; ##f32
                        fz = (z - 512) / 512.0; ##f32
                        return fx, fy, fz
                    (fx, fy, fz) = read_normal(bytesN)
                    (bx, by, bz) = read_normal(bytesT)

                    final_meshdata.normals.append([fx, fy, fz])
                    final_meshdata.normalsAll.append(fx)
                    final_meshdata.normalsAll.append(fy)
                    final_meshdata.normalsAll.append(fz)
                    final_meshdata.tangent_vector.append([bx, by, bz])

                if vBufferInf.vcOffset_and_uv2:
                    br.seek(vBufferInf.vcOffset_and_uv2)
                    for i in range(meshInfo.numVertices):
                        if cToLin:
                            final_meshdata.vertexColor.append([
                                srgb2lin( br.readUByte()/ 255 ),
                                srgb2lin( br.readUByte()/ 255 ),
                                srgb2lin( br.readUByte()/ 255 ),
                                br.readUByte()/ 255])
                        else:
                            final_meshdata.vertexColor.append([
                                br.readUByte()/ 255,
                                br.readUByte()/ 255,
                                br.readUByte()/ 255,
                                br.readUByte()/ 255])

                        uf = br.ReadHalfFloat()
                        vf = br.ReadHalfFloat()
                        final_meshdata.UV2_vertex3DCoords.append([uf,(vf*-1)+1]) # flip
                else:
                    for i in range(meshInfo.numVertices):
                        final_meshdata.vertexColor.append([1.0,1.0,1.0,1.0])
                        final_meshdata.UV2_vertex3DCoords.append([0.0,1.0]) # flip

                #TODO there is zero padding after the final vertex of all meshes

                #Indices -------------------------------------------------------------------
                br.seek(bufferInfos.indexBufferOffset + vBufferInf.indicesOffset + firstIndiceOffset * 2, 0)#br.BaseStream.Seek(bufferInfos.indexBufferOffset + vBufferInf.indicesOffset + firstIndiceOffset * 2, SeekOrigin.Begin);

                indices = [] #List<ushort>
                for i in range(meshInfo.numIndices):
                    indices.append(0)

                for i in range(meshInfo.numIndices):
                    index = br.readUInt16()
                    #indices[i] = index
                    # Indice need to be inversed for the normals
                    if (i % 3 == 0):
                        indices[i] = index
                    elif (i % 3 == 1):
                        indices[i + 1] = index
                    elif (i % 3 == 2):
                        indices[i - 1] = index
                i = 0
                while i < int(meshInfo.numIndices):
                    final_meshdata.faces.append([indices[i],indices[i+1],indices[i+2]])
                    i+=3
 #=====================================================================#
 #                         END OF BUFFER READ                          #
 #                                                                     #
 #=====================================================================#

    return (CData, bufferInfos, the_material_names, the_materials, meshName, meshFile)