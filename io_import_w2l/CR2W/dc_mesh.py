from pathlib import Path
from typing import List
from .bStream import *

from CR2W.Types.VariousTypes import CNAME, CNAME_INDEX, NAME, CColor, CMatrix4x4
from CR2W.w3_types import Vector3D, w2rig
from CR2W.CR2W_types import PROPERTY, getCR2W, CLASS
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
        self.some_vector: List[object] = []
        self.faces = []
        self.normals = []
        self.normalsAll = []
        self.skinningVerts = []
        self.vertexColor = []

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

def load_bin_mesh(filename):
    #OPTIONS
    cToLin = True
    
    #raise NotImplementedError
    log.info('FileLoading: '+ filename)

    # with open(filename,"rb") as meshFileReader:
    #     meshFile = getCR2W(meshFileReader)
    #     #f.close()
    f = open(filename,"rb")
    meshFile = getCR2W(f)
    meshName = Path(meshFile.fileName).stem

    CData:CommonData = CommonData()
    CData.modelPath = meshFile.fileName
    CData.modelName = meshName
    bonePositions: List[Vector3D] = []
    
    bufferInfos:SBufferInfos = SBufferInfos()
    chunk: CLASS
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
                        cakse = br.tell()
                        br.seek(1,1)# br.BaseStream.Position += 1; // Unknown
                        buffInfo.verticesCoordsOffset = br.readUInt32()
                        buffInfo.uvOffset = br.readUInt32()
                        buffInfo.normalsOffset = br.readUInt32()
                        buffInfo.vcOffset_and_uv2 = br.readUInt32()  # color maybe?
                        br.seek(5,1)#br.BaseStream.Position += 9 # Unknown
                        buffInfo.indicesOffset = br.readUInt32()
                        br.seek(1,1)#br.BaseStream.Position += 1 # 0x1D

                        buffInfo.nbVertices = br.readUInt16()
                        buffInfo.nbIndices = br.readUInt32()
                        buffInfo.materialID = br.readByte()
                        br.seek(2,1)#br.BaseStream.Position += 2 # Unknown
                        buffInfo.lod = br.readByte() # lod ?
                        cakse = br.tell()

                        vertexBufferInfos.append(buffInfo)
                elif cookedData.theName == "indexBufferOffset":
                    bufferInfos.indexBufferOffset = cookedData.Value # uint.Parse(cookedData.ToString())
                elif cookedData.theName == "indexBufferSize":
                    bufferInfos.indexBufferSize = cookedData.Value # uint.Parse(cookedData.ToString())
                elif cookedData.theName == "vertexBufferOffset":
                    bufferInfos.vertexBufferOffset = cookedData.Value # uint.Parse(cookedData.ToString())
                elif cookedData.theName == "vertexBufferSize":
                    bufferInfos.vertexBufferSize = cookedData.Value # uint.Parse(cookedData.ToString())
                elif cookedData.theName == "quantizationOffset":
                    bufferInfos.quantizationOffset.x = cookedData.More[0].Value #float.Parse((cookedData as CVector).variables[0].ToString())
                    bufferInfos.quantizationOffset.y = cookedData.More[1].Value #float.Parse((cookedData as CVector).variables[1].ToString())
                    bufferInfos.quantizationOffset.z = cookedData.More[2].Value #float.Parse((cookedData as CVector).variables[2].ToString())
                elif cookedData.theName == "quantizationScale":
                    bufferInfos.quantizationScale.x = cookedData.More[0].Value #float.Parse((cookedData as CVector).variables[0].ToString())
                    bufferInfos.quantizationScale.y = cookedData.More[1].Value #float.Parse((cookedData as CVector).variables[1].ToString())
                    bufferInfos.quantizationScale.z = cookedData.More[2].Value #float.Parse((cookedData as CVector).variables[2].ToString())
                elif cookedData.theName == "bonePositions":
                    try:
                        for item in cookedData.More:#foreach (CVector item in cookedData as CArray)
                            if cookedData.Count == 1: # TODO fix how 1 element arrays are returned
                                item = cookedData
                                item.MoreProps = item.More
                            if (len(item.MoreProps) == 4):
                                pos = Vector3D(0,0,0)
                                
                                # Vector3Df pos = new Vector3Df()
                                pos.x = item.MoreProps[0].Value# pos.X = (item.variables[0] as CFloat).val
                                pos.y = item.MoreProps[1].Value# pos.Y = (item.variables[1] as CFloat).val
                                pos.z = item.MoreProps[2].Value# pos.Z = (item.variables[2] as CFloat).val
                                bonePositions.append(pos)# bonePositions.Add(pos)
                    except Exception as e:
                        raise e

            bufferInfos.verticesBuffer = vertexBufferInfos
            meshChunks = chunk.GetVariableByName("chunks") # CArray
            for meshChunk in meshChunks.chunks.elements:
                meshInfo:SMeshInfos = SMeshInfos()
                
                mi: PROPERTY
                for mi in meshChunk.MoreProps:
                    if mi.theName == "numVertices":
                        meshInfo.numVertices = mi.Value #uint.Parse(mi.ToString());
                    elif mi.theName == "numIndices":
                        meshInfo.numIndices = mi.Value#uint.Parse(mi.ToString());
                    elif mi.theName == "numBonesPerVertex":
                        meshInfo.numBonesPerVertex = mi.Value #uint.Parse(mi.ToString());
                    elif mi.theName == "firstVertex":
                        meshInfo.firstVertex = mi.Value #uint.Parse(mi.ToString());
                    elif mi.theName == "firstIndex":
                        meshInfo.firstIndex = mi.Value #uint.Parse(mi.ToString());
                    elif mi.theName == "vertexType":
                        if (mi.Index.String == "MVT_StaticMesh"):
                            meshInfo.vertexType = EMeshVertexType.EMVT_STATIC
                        elif (mi.Index.String == "MVT_SkinnedMesh"):
                            meshInfo.vertexType = EMeshVertexType.EMVT_SKINNED
                    elif mi.theName == "materialID":
                        meshInfo.materialID = mi.Value#uint.Parse(mi.ToString());
                CData.meshInfos.append(meshInfo)
            #wda
            # // bone names and matrices
            boneNames = chunk.CMesh.boneNames #chunk.GetVariableByName("boneNames") as CBufferVLQ<CName>;
            bonematrices = chunk.CMesh.Bonematrices #chunk.GetVariableByName("bonematrices") as CBufferVLQ<CMatrix4x4>;
            CData.boneData.nbBones = len(boneNames.elements) #(uint)boneNames.elements.Count;
            for i in range(CData.boneData.nbBones):
                name: CNAME_INDEX = boneNames.elements[i]
                CData.boneData.jointNames.append(name.value.name.value)

                cmatrix: CMatrix4x4 = bonematrices.elements[i]
                # matrix = MMatrix()#     Matrix matrix = new Matrix();
                # for j in range(16):
                #     value = cmatrix.fields[j]
                #     matrix.SetElement(j, value)
                #CData.boneData.boneMatrices.append(matrix)
                #TODO LOOK AT MATRIX PREPRATION BEFORE USING IN BLENDER
                CData.boneData.boneMatrices.append(cmatrix)



    read_lods = True # TODO ignore LODs properly
    CData.meshDataAllMeshes = []
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
            vBufferInf = SVertexBufferInfos() # TODO get rid of this
            # nbVertices = 0
            # firstVertexOffset = 0
            # nbIndices = 0
            # firstIndiceOffset = 0

            if (vBufferInf.lod == 1 or read_lods):
                final_meshdata:MeshData = MeshData()
                final_meshdata.meshInfo = meshInfo
                CData.meshDataAllMeshes.append(final_meshdata)

                #Vertices -------------------------------------------------------------------
                # Load DeferredDataBuffer
                vertBufferIndex = rawVertices.ValueA
                bufferInfo = meshFile.CR2WBuffer[vertBufferIndex - 1]

                # vertexSize = 8
                # if (meshInfo.vertexType == EMeshVertexType.EMVT_SKINNED):
                #     vertexSize += meshInfo.numBonesPerVertex * 2

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
                        fweight = br.readFloat()

                        if (fweight != 0.0):
                            vertexSkinningEntry = VertexSkinningEntry()
                            vertexSkinningEntry.boneId = boneId
                            vertexSkinningEntry.meshBufferId = 0
                            vertexSkinningEntry.vertexId = i
                            vertexSkinningEntry.strength = fweight
                            CData.w3_DataCache.vertices.append(vertexSkinningEntry)
                            final_meshdata.skinningVerts.append(vertexSkinningEntry)
                    
                    ## NORMALS    
                    fx = br.readFloat()
                    fy = br.readFloat()
                    fz = br.readFloat()
                    
                    # 000:-95.50341796875
                    # 001:19.64809799194336
                    # 002:22.385143280029297
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
                    final_meshdata.UV_vertex3DCoords.append([uf,(vf*-1)+1]) # flip
                    
                    uf2 = br.readFloat()
                    vf2 = br.readFloat()
                    final_meshdata.UV2_vertex3DCoords.append([uf2,(vf2*-1)+1]) # flip

                    
                    fsx = br.readFloat()
                    fsy = br.readFloat()
                    fsz = br.readFloat()
                    final_meshdata.some_vector.append([fsx, fsy, fsz])
                    
                    br.seek(76, 1)


                    # vertex3DCoord = [x * bufferInfos.quantizationScale.x + bufferInfos.quantizationOffset.x,
                    #                  y * bufferInfos.quantizationScale.y + bufferInfos.quantizationOffset.y,
                    #                  z * bufferInfos.quantizationScale.z + bufferInfos.quantizationOffset.z]
                    vertex3DCoord = [x,
                                     y,
                                     z ]
                    final_meshdata.vertex3DCoords.append(vertex3DCoord)
                lastVertOffset = br.tell() - bufferInfo.offset
                #Indices -------------------------------------------------------------------
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
            if (vBufferInf.lod == 1 or read_lods):
                final_meshdata:MeshData = MeshData()
                final_meshdata.meshInfo = meshInfo
                CData.meshDataAllMeshes.append(final_meshdata)
                
                vertexSize = 8
                if (meshInfo.vertexType == EMeshVertexType.EMVT_SKINNED):
                    vertexSize += meshInfo.numBonesPerVertex * 2

                br.seek(vBufferInf.verticesCoordsOffset + firstVertexOffset * vertexSize, 0)
                vertex3DCoords: List[object] = []#List<Vertex3D> vertex3DCoords = new List<Vertex3D>();
                #defaultColor = CColor().Set(255, 255, 255, 255) #Color defaultColor = new Color(255, 255, 255, 255);
                for i in range(meshInfo.numVertices):
                    x = br.readUInt16() #br.ReadUInt16()
                    y = br.readUInt16() #br.ReadUInt16()
                    z = br.readUInt16() #br.ReadUInt16()
                    w = br.readUInt16() #br.ReadUInt16()
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
                    # vertex3DCoord.Position = new Vector3Df(x, y, z) / 65535f * bufferInfos.quantizationScale + bufferInfos.quantizationOffset;
                    # vertex3DCoord.Color = defaultColor;
                    # vertex3DCoords.Add(vertex3DCoord);
            
                #### UVs
                br.seek(vBufferInf.uvOffset + firstVertexOffset * 4, 0)
            

                for i in range(meshInfo.numVertices):
                    uf = br.ReadHalfFloat()
                    vf = br.ReadHalfFloat()
                    final_meshdata.UV_vertex3DCoords.append([uf,(vf*-1)+1]) # flip
                    
                # br.seek(vBufferInf.uvOffset + firstVertexOffset * 4, 0)
                # for i in range(meshInfo.numVertices):
                #     uf = br.ReadHalfFloat()
                #     vf = br.ReadHalfFloat()
                #     final_meshdata.UV2_vertex3DCoords.append([uf,(vf*-1)+1]) # flip

                br.seek(vBufferInf.normalsOffset + firstVertexOffset * 8) # bufferFile->seek(vBufferInf.normalsOffset + firstVertexOffset * 8);
                # ##std::cout << "POS vNormals=" << bufferFile->getPos() << std::endl;
                for i in range(meshInfo.numVertices): #for (u32 i = 0; i < meshInfos.numVertices; ++i)
                    bytes = br.read(4)#core::array<u8> bytes = readDataArray<u8>(bufferFile, 4);
                    br.seek(4,1)#bufferFile->seek(4, true);

                    x = ((bytes[0]&0b11111111) | ((bytes[1]&0b11) << 8)) #u16
                    y = ((bytes[1]&0b11111100) | ((bytes[2]&0b00001111) << 8)) >> 2 #u16
                    z = ((bytes[2]&0b11110000) | ((bytes[3]&0b00111111) << 8)) >> 4 #u16

                    fx = (x - 512) / 512.0; ##f32
                    fy = (y - 512) / 512.0; ##f32
                    fz = (z - 512) / 512.0; ##f32

                    final_meshdata.normals.append([fx, fy, fz])
                    final_meshdata.normalsAll.append(fx)
                    final_meshdata.normalsAll.append(fy)
                    final_meshdata.normalsAll.append(fz)

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
                    #br.seek(4,1) #br.readUInt32()
                    uf = br.ReadHalfFloat()
                    vf = br.ReadHalfFloat()
                    final_meshdata.UV2_vertex3DCoords.append([uf,(vf*-1)+1]) # flip

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