import logging
from pathlib import Path
import os
import struct
from typing import List
from types import SimpleNamespace

import numpy as np

from .bin_helpers import ReadBit6, ReadVLQInt32
from .common_blender import extract_missing_buffers, win_safe_path
from .helper_function import flip_v

from .Types import CMesh
from .bStream import *

from .Types.VariousTypes import CNAME, CNAME_INDEX, NAME, CBufferVLQInt32, CColor, CFloat, CMatrix4x4, CPaddedBuffer, CUInt16, CUInt32
from .w3_types import Vector3D, w2rig
from .CR2W_types import PROPERTY, SMeshChunkPacked, getCR2W, W_CLASS
from .Types.BlenderMesh import CommonData
from .Types.SBufferInfos import MMatrix, SBufferInfos, SVertexBufferInfos, SMeshInfos, EMeshVertexType, VertexSkinningEntry
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


def _srgb2lin_vec(s):
    """Vectorized srgb to linear conversion for numpy arrays."""
    return np.where(s <= 0.0404482362771082, s / 12.92, ((s + 0.055) / 1.055) ** 2.4)


def _read_indices_numpy(fhandle, num_indices):
    """Read index buffer in bulk and swap winding order. Returns list of [i0,i1,i2] faces."""
    raw = fhandle.read(num_indices * 2)
    indices = np.frombuffer(raw, dtype='<u2').copy()
    tris = indices.reshape(-1, 3)
    tris[:, [1, 2]] = tris[:, [2, 1]]  # swap columns 1,2 for winding order
    return tris.tolist()


def _read_vertices_uncooked_numpy(fhandle, num_vertices, num_bones_per_vertex,
                                  num_extra_floats, cToLin, CData, final_meshdata):
    """Read uncooked vertex data in bulk using numpy structured arrays.

    Used for both W2 uncooked and W3 rawVertices paths where the layout is:
    pos(3f) + boneIDs(Nb) + weights(Nf) + normals(3f) + color(4b) + UV1(2f) + UV2(2f)
    + tangent(3f) + extra(Mf)

    Populates final_meshdata fields and CData.w3_DataCache.vertices in-place.
    """
    nbpv = num_bones_per_vertex
    # Build structured dtype
    dt = np.dtype([
        ('pos', '<f4', 3),
        ('bone_ids', 'u1', nbpv),
        ('weights', '<f4', nbpv),
        ('normals', '<f4', 3),
        ('color', 'u1', 4),
        ('uv1', '<f4', 2),
        ('uv2', '<f4', 2),
        ('tangent', '<f4', 3),
        ('extra', '<f4', num_extra_floats),
    ])

    raw = fhandle.read(dt.itemsize * num_vertices)
    verts = np.frombuffer(raw, dtype=dt, count=num_vertices)

    # Positions
    final_meshdata.vertex3DCoords = verts['pos'].tolist()

    # Normals
    normals = verts['normals']
    final_meshdata.normals = normals.tolist()
    final_meshdata.normalsAll = normals.ravel().tolist()

    # Vertex colors
    color_f = verts['color'].astype(np.float64) / 255.0
    if cToLin:
        color_f[:, :3] = _srgb2lin_vec(color_f[:, :3])
    final_meshdata.vertexColor = color_f.tolist()

    # UVs (flip V: v = v * -1 + 1)
    uv1 = verts['uv1'].copy()
    uv1[:, 1] = uv1[:, 1] * -1.0 + 1.0
    final_meshdata.UV_vertex3DCoords = uv1.tolist()

    uv2 = verts['uv2'].copy()
    uv2[:, 1] = uv2[:, 1] * -1.0 + 1.0
    final_meshdata.UV2_vertex3DCoords = uv2.tolist()

    # Tangents
    final_meshdata.tangent_vector = verts['tangent'].tolist()

    # Extra vectors
    extra = verts['extra']
    final_meshdata.extra_vectors = extra.tolist()

    # Skinning data - extract non-zero weights
    bone_ids = verts['bone_ids']   # (N, nbpv)
    weights = verts['weights']     # (N, nbpv)
    nz_mask = weights != 0.0
    nz_vert, nz_slot = np.nonzero(nz_mask)

    for idx in range(len(nz_vert)):
        vi = int(nz_vert[idx])
        si = int(nz_slot[idx])
        entry = VertexSkinningEntry()
        entry.boneId = int(bone_ids[vi, si])
        entry.boneId_idx = si
        entry.meshBufferId = 0
        entry.vertexId = vi
        entry.strength = float(weights[vi, si])
        CData.w3_DataCache.vertices.append(entry)
        final_meshdata.skinningVerts.append(entry)


def _read_vertices_cooked_w2_numpy(fhandle, num_vertices, vertex_size, is_skinned,
                                   num_bones_per_vertex, has_second_uv, cToLin,
                                   bones_id_map, bone_names, CData, final_meshdata):
    """Read cooked W2 vertex data in bulk.

    Layout: pos(3f) + [skinning(8b) if skinned] + packed_normal(4b) + color(4b)
    + UV1(2f) + [UV2(2f) if has_second_uv]. Remaining bytes skipped via vertex_size stride.
    """
    raw = fhandle.read(vertex_size * num_vertices)
    if len(raw) < vertex_size * num_vertices:
        return  # truncated data, bail out

    nbpv = num_bones_per_vertex
    for i in range(num_vertices):
        base = i * vertex_size
        # Position: 3 floats
        pos = struct.unpack_from('<3f', raw, base)
        final_meshdata.vertex3DCoords.append(list(pos))

        off = base + 12
        # Skinning
        if is_skinned:
            skin_data = raw[off:off + nbpv * 2]
            off += nbpv * 2
            for j in range(min(4, nbpv)):
                weight_byte = skin_data[j + nbpv]
                if weight_byte != 0:
                    bone_id = bones_id_map[skin_data[j]]
                    entry = VertexSkinningEntry()
                    entry.boneId = bone_id
                    entry.boneId_idx = j
                    entry.meshBufferId = 0
                    entry.vertexId = i
                    entry.strength = float(weight_byte) / 255.0
                    CData.w3_DataCache.vertices.append(entry)
                    final_meshdata.skinningVerts.append(entry)

        # Packed normal: 4 bytes
        nb = raw[off:off + 4]
        off += 4
        fx = (nb[0] - 127) / 127.0
        fy = (nb[1] - 127) / 127.0
        fz = (nb[2] - 127) / 127.0
        final_meshdata.normals.append([fx, fy, fz])
        final_meshdata.normalsAll.extend([fx, fy, fz])

        # Color: 4 bytes
        cb = raw[off:off + 4]
        off += 4
        if cToLin:
            final_meshdata.vertexColor.append([
                srgb2lin(cb[0] / 255.0), srgb2lin(cb[1] / 255.0),
                srgb2lin(cb[2] / 255.0), cb[3] / 255.0])
        else:
            final_meshdata.vertexColor.append([
                cb[0] / 255.0, cb[1] / 255.0, cb[2] / 255.0, cb[3] / 255.0])

        # UV1: 2 floats
        uv = struct.unpack_from('<2f', raw, off)
        off += 8
        final_meshdata.UV_vertex3DCoords.append([uv[0], uv[1] * -1.0 + 1.0])

        # UV2: 2 floats (optional)
        if has_second_uv:
            uv2 = struct.unpack_from('<2f', raw, off)
            final_meshdata.UV2_vertex3DCoords.append([uv2[0], uv2[1] * -1.0 + 1.0])


def _read_vertices_cooked_w3_numpy(fhandle, num_vertices, vertex_size, is_skinned,
                                   num_bones_per_vertex, quant_scale, quant_offset,
                                   CData, final_meshdata):
    """Read cooked W3 vertex data (quantized uint16 positions + byte skinning).

    Layout: pos(3×u16 + 1×u16 padding) + [skinning(N*2 bytes) if skinned]
    Remaining data (UVs, normals, colors) is in separate buffer regions.
    """
    raw = fhandle.read(vertex_size * num_vertices)
    if len(raw) < vertex_size * num_vertices:
        return

    nbpv = num_bones_per_vertex
    # Parse positions and skinning in one pass using struct
    for i in range(num_vertices):
        base = i * vertex_size
        x, y, z, w = struct.unpack_from('<4H', raw, base)
        off = base + 8

        if is_skinned:
            skin_data = raw[off:off + nbpv * 2]
            for j in range(nbpv):
                weight_byte = skin_data[j + nbpv]
                if weight_byte != 0:
                    entry = VertexSkinningEntry()
                    entry.boneId = skin_data[j]
                    entry.meshBufferId = 0
                    entry.vertexId = i
                    entry.strength = float(weight_byte) / 255.0
                    CData.w3_DataCache.vertices.append(entry)
                    final_meshdata.skinningVerts.append(entry)

        final_meshdata.vertex3DCoords.append([
            x / 65535 * quant_scale.x + quant_offset.x,
            y / 65535 * quant_scale.y + quant_offset.y,
            z / 65535 * quant_scale.z + quant_offset.z,
        ])


def _read_uvs_halffloat_numpy(fhandle, num_vertices):
    """Read half-float UV pairs in bulk. Returns list of [u, flipped_v]."""
    raw = fhandle.read(num_vertices * 4)
    uvs = np.frombuffer(raw, dtype='<f2').reshape(num_vertices, 2).astype(np.float32)
    uvs[:, 1] = uvs[:, 1] * -1.0 + 1.0
    return uvs.tolist()


def _read_normals_packed10bit_numpy(fhandle, num_vertices):
    """Read packed 10-bit normals+tangents (4+4 bytes per vertex) in bulk."""
    raw = fhandle.read(num_vertices * 8)
    data = np.frombuffer(raw, dtype='u1').reshape(num_vertices, 8)
    normals_raw = data[:, :4]
    tangents_raw = data[:, 4:]

    def _unpack_10bit(d):
        b0, b1, b2, b3 = d[:, 0].astype(np.int32), d[:, 1].astype(np.int32), d[:, 2].astype(np.int32), d[:, 3].astype(np.int32)
        x = (b0 | ((b1 & 0x03) << 8))
        y = ((b1 >> 2) | ((b2 & 0x0F) << 6))
        z = ((b2 >> 4) | ((b3 & 0x3F) << 4))
        return np.column_stack([(x - 512) / 512.0, (y - 512) / 512.0, (z - 512) / 512.0])

    normals = _unpack_10bit(normals_raw)
    tangents = _unpack_10bit(tangents_raw)
    return normals, tangents


def _read_vertex_colors_and_uv2_numpy(fhandle, num_vertices, cToLin):
    """Read interleaved vertex colors (4 bytes) + half-float UV2 (4 bytes)."""
    raw = fhandle.read(num_vertices * 8)
    data = np.frombuffer(raw, dtype='u1').reshape(num_vertices, 8)

    # Color: first 4 bytes
    color_f = data[:, :4].astype(np.float64) / 255.0
    if cToLin:
        color_f[:, :3] = _srgb2lin_vec(color_f[:, :3])
    colors = color_f.tolist()

    # UV2: bytes 4-7 as two half-floats
    uv2_raw = data[:, 4:8].view('<f2').reshape(num_vertices, 2).astype(np.float32)
    uv2_raw[:, 1] = uv2_raw[:, 1] * -1.0 + 1.0
    uv2s = uv2_raw.tolist()

    return colors, uv2s


#!### WITCHER 2 CLASSES

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

    #raise NotImplementedError
    log.info('FileLoading: '+ filename)

    # with open(filename,"rb") as meshFileReader:
    #     meshFile = getCR2W(meshFileReader)
    #     #f.close()
    f = open(win_safe_path(filename),"rb")
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
                    if the_materials:
                        for idx in range(the_materials.Count):
                            the_material_names.append("Material"+str(idx))
                    else:
                        # Allow meshes with no materials
                        the_materials = SimpleNamespace(Count=0, Handles=[])

                #go to buffer start
                br.seek(chunk.PROPS[-1].dataEnd)

                is_uncooked = False
                yes = br.read(8)
                if (yes[3] != 5 or meshFile.HEADER.version <= 83): #
                    is_uncooked = True
                    br.seek(-8,1)
                    #log.critical('Error reading LODs')
                    #return
                
                if is_uncooked:
                    ############! UNCOOKED STUFF
                    if meshFile.HEADER.version <= 86:
                        yes = br.read(3)
                        nbSubMesh = br.readUByte()
                    else:
                        yes = br.read(7)
                        nbSubMesh = br.readUByte()

                    subMeshesData:List[SubmeshData] = []

                    def readUncookedSubmeshData(br:bStream):
                        extra_vectors = True
                        if meshFile.HEADER.version <= 100: # 95, 92 lowest
                            extra_vectors = False
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
                        num_extra_floats = 9 if extra_vectors else 3
                        _read_vertices_uncooked_numpy(
                            br.fhandle, meshInfo.numVertices,
                            meshInfo.numBonesPerVertex, num_extra_floats,
                            cToLin, CData, final_meshdata)
                        # Pad extra_vectors to 9 elements if only 3 were in file
                        if not extra_vectors:
                            final_meshdata.extra_vectors = [
                                ev + [0, 0, 0, 0, 0, 0] for ev in final_meshdata.extra_vectors
                            ]

                        lastVertOffset = br.tell() #- bufferInfo.offset
                        #?#########################
                        #?#########################
                        #?#########################
                        vertend = br.tell()

                        #?#################?#
                        #?#### Indices ####?#
                        #?#################?#
                        numIndices_count = ReadVLQInt32(br.fhandle)
                        final_meshdata.faces = _read_indices_numpy(br.fhandle, meshInfo.numIndices)
                        lastIOffset = br.tell()

                        bonesId_count = br.readByte()
                        for _ in range(bonesId_count):
                            submesh.bonesId.append(br.readUInt16())

                        #Replace skinning ids with mapped id
                        for skinningVert in final_meshdata.skinningVerts:
                            skinningVert.boneId = submesh.bonesId[skinningVert.boneId]

                        final_meshdata.meshInfo = submesh
                        return submesh

                    subMeshesData = [readUncookedSubmeshData(br) for _ in range(nbSubMesh)]
                    CData.meshInfos = subMeshesData
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

                        #! TODO FIX BONES LESS THAN VERSION 89
                        if meshFile.HEADER.version <= 89:
                            pass
                        else:
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

                #TODO fix wrong weights being applied
                if meshFile.HEADER.version <= 89:
                    #CData.boneData.BoneIndecesMappingBoneIndex = [index for index in range(CData.boneData.nbBones)]
                    CData.boneData.Block3 = [0.0] * CData.boneData.nbBones
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
                    _read_vertices_cooked_w2_numpy(
                        br.fhandle, submesh.verticesCount, vertexSize,
                        isSkinned, 4, hasSecondUVLayer, cToLin,
                        submesh.bonesId, CData.boneData.jointNames,
                        CData, final_meshdata)
                    br.seek(submeshStartPos + submesh.verticesStart * vertexSize + vertexSize * submesh.verticesCount)


                    #?#################?#
                    #?#### Indices ####?#
                    #?#################?#
                    br.seek(submeshStartPos + meshIndicesOffset + submesh.indicesStart * 2) #br.seek(bufferInfo.offset + lastIOffset, 0)
                    final_meshdata.faces = _read_indices_numpy(br.fhandle, submesh.indicesCount)
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
                if the_materials:
                    for idx in range(the_materials.Count):
                        the_material_names.append(meshName+"_"+"Material"+str(idx))
                else:
                    # Allow meshes with no materials
                    the_materials = SimpleNamespace(Count=0, Handles=[])



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
            if not meshChunks or not getattr(meshChunks, "chunks", None) or not getattr(meshChunks.chunks, "elements", None):
                # Mesh has no chunks/geometry
                return (CData, bufferInfos, the_material_names, the_materials, meshName, meshFile)
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
            CData.autohideDistance = chunk.GetVariableByName("autoHideDistance").Value if chunk.GetVariableByName("autoHideDistance") else 20.0
            CData.isTwoSided = chunk.GetVariableByName("isTwoSided").Value if chunk.GetVariableByName("isTwoSided") else False
            CData.useExtraStreams = chunk.GetVariableByName("useExtraStreams").Value if chunk.GetVariableByName("useExtraStreams") else False
            CData.generalizedMeshRadius = chunk.GetVariableByName("generalizedMeshRadius").Value if chunk.GetVariableByName("generalizedMeshRadius") else 0.0
            CData.mergeInGlobalShadowMesh = chunk.GetVariableByName("mergeInGlobalShadowMesh").Value if chunk.GetVariableByName("mergeInGlobalShadowMesh") else True
            CData.isOccluder = chunk.GetVariableByName("isOccluder").Value if chunk.GetVariableByName("isOccluder") else True
            CData.smallestHoleOverride = chunk.GetVariableByName("smallestHoleOverride").Value if chunk.GetVariableByName("smallestHoleOverride") else -1.0
            CData.isStatic = chunk.GetVariableByName("isStatic").Value if chunk.GetVariableByName("isStatic") else False
            CData.entityProxy = chunk.GetVariableByName("entityProxy").Value if chunk.GetVariableByName("entityProxy") else False

            # SMeshSoundInfo
            CData.soundInfo = None
            soundInfoProp = chunk.GetVariableByName("soundInfo")
            if soundInfoProp and hasattr(soundInfoProp, 'Value') and soundInfoProp.Value and soundInfoProp.Value > 0:
                soundInfoChunkIdx = soundInfoProp.Value - 1
                if soundInfoChunkIdx < len(meshFile.CHUNKS.CHUNKS):
                    soundInfoChunk = meshFile.CHUNKS.CHUNKS[soundInfoChunkIdx]
                    CData.soundInfo = {
                        'soundTypeIdentification': '',
                        'soundSizeIdentification': '',
                        'soundBoneMappingInfo': '',
                    }
                    prop = soundInfoChunk.GetVariableByName("soundTypeIdentification")
                    if prop and hasattr(prop, 'Index') and prop.Index:
                        CData.soundInfo['soundTypeIdentification'] = prop.Index.String if hasattr(prop.Index, 'String') else str(prop.Index)
                    prop = soundInfoChunk.GetVariableByName("soundSizeIdentification")
                    if prop and hasattr(prop, 'Index') and prop.Index:
                        CData.soundInfo['soundSizeIdentification'] = prop.Index.String if hasattr(prop.Index, 'String') else str(prop.Index)
                    prop = soundInfoChunk.GetVariableByName("soundBoneMappingInfo")
                    if prop and hasattr(prop, 'Index') and prop.Index:
                        CData.soundInfo['soundBoneMappingInfo'] = prop.Index.String if hasattr(prop.Index, 'String') else str(prop.Index)

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
            if (meshInfo.lod == 0 or keep_lod_meshes):
                final_meshdata:MeshData = MeshData()
                final_meshdata.meshInfo = meshInfo
                CData.meshDataAllMeshes.append(final_meshdata)

                #?#################?#
                #?### Vertices ####?#
                #?#################?#
                vertBufferIndex = rawVertices.ValueA
                bufferInfo = meshFile.CR2WBuffer[vertBufferIndex - 1]

                br.seek(bufferInfo.offset + lastVertOffset)
                _read_vertices_uncooked_numpy(
                    br.fhandle, meshInfo.numVertices,
                    meshInfo.numBonesPerVertex, 19,
                    cToLin, CData, final_meshdata)
                lastVertOffset = br.tell() - bufferInfo.offset

                #?#################?#
                #?#### Indices ####?#
                #?#################?#
                # Load DeferredDataBuffer
                rawIndicesIndex = rawIndices.ValueA
                bufferInfo = meshFile.CR2WBuffer[rawIndicesIndex - 1]
                br.seek(bufferInfo.offset + lastIOffset, 0)#br.seek(bufferInfo.offset)
                final_meshdata.faces = _read_indices_numpy(br.fhandle, meshInfo.numIndices)
                lastIOffset = br.tell() - bufferInfo.offset

        br.close()

    else:
        #=====================================================================#
        #                     READ COOKED BUFFER INFOS                        #
        #                                                                     #
        #=====================================================================#

        def_path = meshFile.fileName + ".1.buffer"
        safe_def_path = win_safe_path(def_path)
        if not os.path.exists(safe_def_path):
            extract_missing_buffers(meshFile.fileName, required_index=1)
            safe_def_path = win_safe_path(def_path)
        if not os.path.exists(safe_def_path):
            raise FileNotFoundError(
                f"Missing required mesh buffer {def_path}; buffer index 1 could not be found or extracted."
            )
        f = open(safe_def_path,"rb")
        br = bStream(data = f.read())
        f.close()
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
            if (meshInfo.lod == 0 or keep_lod_meshes):
                final_meshdata:MeshData = MeshData()
                final_meshdata.meshInfo = meshInfo
                CData.meshDataAllMeshes.append(final_meshdata)

                vertexSize = 8
                if (meshInfo.vertexType == EMeshVertexType.EMVT_SKINNED):
                    vertexSize += meshInfo.numBonesPerVertex * 2

                br.seek(vBufferInf.verticesCoordsOffset + firstVertexOffset * vertexSize, 0)
                isSkinned = (meshInfo.vertexType == EMeshVertexType.EMVT_SKINNED)
                _read_vertices_cooked_w3_numpy(
                    br.fhandle, meshInfo.numVertices, vertexSize,
                    isSkinned, meshInfo.numBonesPerVertex,
                    bufferInfos.quantizationScale, bufferInfos.quantizationOffset,
                    CData, final_meshdata)

                #### UVs
                br.seek(vBufferInf.uvOffset + firstVertexOffset * 4, 0)
                final_meshdata.UV_vertex3DCoords = _read_uvs_halffloat_numpy(br.fhandle, meshInfo.numVertices)

                br.seek(vBufferInf.normalsOffset + firstVertexOffset * 8)
                normals_arr, tangents_arr = _read_normals_packed10bit_numpy(br.fhandle, meshInfo.numVertices)
                final_meshdata.normals = normals_arr.tolist()
                final_meshdata.normalsAll = normals_arr.ravel().tolist()
                final_meshdata.tangent_vector = tangents_arr.tolist()

                if vBufferInf.vcOffset_and_uv2:
                    br.seek(vBufferInf.vcOffset_and_uv2)
                    colors, uv2s = _read_vertex_colors_and_uv2_numpy(br.fhandle, meshInfo.numVertices, cToLin)
                    final_meshdata.vertexColor = colors
                    final_meshdata.UV2_vertex3DCoords = uv2s
                else:
                    final_meshdata.vertexColor = None
                    final_meshdata.UV2_vertex3DCoords = [[0.0, 1.0]] * meshInfo.numVertices

                #TODO there is zero padding after the final vertex of all meshes

                #Indices -------------------------------------------------------------------
                br.seek(bufferInfos.indexBufferOffset + vBufferInf.indicesOffset + firstIndiceOffset * 2, 0)
                final_meshdata.faces = _read_indices_numpy(br.fhandle, meshInfo.numIndices)
 #=====================================================================#
 #                         END OF BUFFER READ                          #
 #                                                                     #
 #=====================================================================#

    return (CData, bufferInfos, the_material_names, the_materials, meshName, meshFile)
