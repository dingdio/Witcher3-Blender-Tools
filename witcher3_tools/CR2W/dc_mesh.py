import logging
from pathlib import Path
import os
import re
import struct
from typing import List
from types import SimpleNamespace

import numpy as np

from .bin_helpers import (
    ReadBit6,
    ReadVLQInt32,
    read_i32_at as _read_i32_at,
    read_u8_at as _read_u8_at,
    read_u16_at as _read_u16_at,
    read_u32_at as _read_u32_at,
    read_witcher2_encoded_string_at as _read_w2_encoded_string_at,
)
from .common_blender import extract_missing_buffers, repo_file, win_safe_path
from .helper_function import flip_v

from .Types import CMesh
from .bStream import *

from .Types.VariousTypes import CNAME, CNAME_INDEX, NAME, CBufferVLQInt32, CColor, CFloat, CMatrix4x4, CPaddedBuffer, CUInt16, CUInt32
from .w3_types import Vector3D, w2rig
from .CR2W_types import PROPERTY, SMeshChunkPacked, getCR2W, W_CLASS
from .Types.BlenderMesh import CommonData
from .Types.SBufferInfos import MMatrix, SBufferInfos, SVertexBufferInfos, SMeshInfos, EMeshVertexType, VertexSkinningEntry
log = logging.getLogger(__name__)

_W2_IMPORT_FILE_MATERIAL_NAMES_CACHE = {}
_W2_EMBEDDED_CMESH_FILE_CACHE = {}
_W2_EMBEDDED_CMESH_FILE_CACHE_MAX = 4


def _clear_w2_cmesh_runtime_buffers(chunk):
    cmesh = getattr(chunk, "CMesh", None)
    if cmesh is None:
        return

    # W2 cooked/uncooked parsing fills these arrays manually from raw payload
    # data. Embedded CMesh imports may reuse a cached CR2W object, so clear the
    # previous import's runtime parse results before appending fresh values.
    for attr_name in (
        "ChunkgroupIndeces",
        "BoneNames",
        "Bonematrices",
        "Block3",
        "BoneIndecesMappingBoneIndex",
    ):
        buffer = getattr(cmesh, attr_name, None)
        elements = getattr(buffer, "elements", None)
        if elements is not None:
            elements.clear()


def _w2_embedded_cmesh_cache_key(filename):
    try:
        safe_path = os.path.abspath(win_safe_path(filename))
        stat = os.stat(safe_path)
        return (os.path.normcase(safe_path), int(getattr(stat, "st_mtime_ns", 0)), int(stat.st_size))
    except Exception:
        return None


def _load_cached_w2_embedded_cmesh_file(filename):
    cache_key = _w2_embedded_cmesh_cache_key(filename)
    if cache_key and cache_key in _W2_EMBEDDED_CMESH_FILE_CACHE:
        return _W2_EMBEDDED_CMESH_FILE_CACHE[cache_key]

    f = open_cr2w_read_stream(win_safe_path(filename))
    mesh_file = getCR2W(f)
    if getattr(getattr(mesh_file, "HEADER", None), "version", 999) > 115:
        return mesh_file, None
    raw_data = f.cr2w_buf

    result = (mesh_file, raw_data)
    if cache_key:
        _W2_EMBEDDED_CMESH_FILE_CACHE[cache_key] = result
        while len(_W2_EMBEDDED_CMESH_FILE_CACHE) > _W2_EMBEDDED_CMESH_FILE_CACHE_MAX:
            _W2_EMBEDDED_CMESH_FILE_CACHE.pop(next(iter(_W2_EMBEDDED_CMESH_FILE_CACHE)))
    return result


def _derive_mesh_is_static(mesh_infos):
    if mesh_infos:
        return not any(getattr(mesh_info, "vertexType", None) == EMeshVertexType.EMVT_SKINNED for mesh_info in mesh_infos)
    return False

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

    def split_data(self, max_vertices=65535):
        """Split mesh data into UInt16-indexable chunks without recalculating vertex data."""
        total_vertices = len(self.vertex3DCoords)
        if total_vertices <= max_vertices:
            return [self]
        if max_vertices < 3:
            raise ValueError("Mesh chunks need room for at least one triangle.")

        source_info = self.meshInfo if isinstance(self.meshInfo, SMeshInfos) else SMeshInfos()
        skinning_by_vertex = {}
        for entry in self.skinningVerts:
            skinning_by_vertex.setdefault(int(entry.vertexId), []).append(entry)

        def copy_mesh_info(chunk):
            chunk.meshInfo = SMeshInfos()
            chunk.meshInfo.numVertices = len(chunk.vertex3DCoords)
            chunk.meshInfo.numIndices = len(chunk.faces) * 3
            chunk.meshInfo.numBonesPerVertex = getattr(source_info, "numBonesPerVertex", 4)
            chunk.meshInfo.vertexType = getattr(source_info, "vertexType", EMeshVertexType.EMVT_STATIC)
            chunk.meshInfo.materialID = getattr(source_info, "materialID", 0)
            chunk.meshInfo.lod = getattr(source_info, "lod", -1)
            chunk.meshInfo.distance = getattr(source_info, "distance", 0)

        def append_value(dst, src, index, fallback):
            if index < len(src):
                dst.append(src[index])
            else:
                dst.append(fallback)

        def append_vertex(chunk, old_index):
            new_index = len(chunk.vertex3DCoords)
            append_value(chunk.vertex3DCoords, self.vertex3DCoords, old_index, [0.0, 0.0, 0.0])
            append_value(chunk.UV_vertex3DCoords, self.UV_vertex3DCoords, old_index, [0.0, 1.0])
            append_value(chunk.UV2_vertex3DCoords, self.UV2_vertex3DCoords, old_index, [0.0, 1.0])
            append_value(chunk.tangent_vector, self.tangent_vector, old_index, [1.0, 0.0, 0.0])
            append_value(chunk.extra_vectors, self.extra_vectors, old_index, [0.0, 1.0, 0.0])
            append_value(chunk.normals, self.normals, old_index, [0.0, 0.0, 1.0])
            append_value(chunk.vertexColor, self.vertexColor, old_index, [0.0, 0.0, 0.0, 0.0])

            normal_offset = old_index * 3
            if normal_offset + 2 < len(self.normalsAll):
                chunk.normalsAll.extend(self.normalsAll[normal_offset:normal_offset + 3])
            elif old_index < len(self.normals):
                chunk.normalsAll.extend(self.normals[old_index])
            else:
                chunk.normalsAll.extend([0.0, 0.0, 1.0])

            for entry in skinning_by_vertex.get(old_index, []):
                copied = VertexSkinningEntry()
                copied.boneId = entry.boneId
                copied.boneId_idx = entry.boneId_idx
                copied.meshBufferId = entry.meshBufferId
                copied.vertexId = new_index
                copied.strength = entry.strength
                chunk.skinningVerts.append(copied)
            return new_index

        if not self.faces:
            chunks = []
            for start_index in range(0, total_vertices, max_vertices):
                chunk = MeshData()
                for old_index in range(start_index, min(start_index + max_vertices, total_vertices)):
                    append_vertex(chunk, old_index)
                copy_mesh_info(chunk)
                chunks.append(chunk)
            return chunks

        chunks = []
        chunk = MeshData()
        vertex_map = {}

        def flush_chunk():
            nonlocal chunk, vertex_map
            if not chunk.faces and not chunk.vertex3DCoords:
                return
            copy_mesh_info(chunk)
            chunks.append(chunk)
            chunk = MeshData()
            vertex_map = {}

        for face in self.faces:
            face_indices = [int(index) for index in face]
            missing = [index for index in face_indices if index not in vertex_map]
            if vertex_map and len(vertex_map) + len(missing) > max_vertices:
                flush_chunk()

            new_face = []
            for old_index in face_indices:
                new_index = vertex_map.get(old_index)
                if new_index is None:
                    new_index = append_vertex(chunk, old_index)
                    vertex_map[old_index] = new_index
                new_face.append(new_index)
            chunk.faces.append(new_face)

        flush_chunk()
        return chunks


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
                                  num_extra_floats, cToLin, CData, final_meshdata,
                                  emit_skinning=True):
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

    if emit_skinning:
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


def _w2_cnames(meshFile):
    names = []
    for cname in getattr(meshFile, "CNAMES", []) or []:
        try:
            names.append(cname.name.value)
        except Exception:
            names.append("")
    return names


def _w2_chunk_data_range(meshFile, chunk):
    chunk_index = getattr(chunk, "ChunkIndex", None)
    exports = getattr(meshFile, "CR2WExport", []) or []
    if chunk_index is None or chunk_index < 0 or chunk_index >= len(exports):
        return None, None
    export = exports[chunk_index]
    start = int(getattr(meshFile, "start", 0) or 0) + int(getattr(export, "dataOffset", 0) or 0)
    size = int(getattr(export, "dataSize", 0) or 0)
    return start, start + max(0, size)


def _read_w2_cmesh_header(meshFile, chunk, data: bytes):
    """Read W2 CMesh props directly from the chunk data offset.

    W2 CMesh stores reflected properties followed by a mesh-specific payload in
    the same export range. This focused pass recovers the small reflected header
    without depending on the generic class reader to understand the payload.
    """
    names = _w2_cnames(meshFile)
    start, end = _w2_chunk_data_range(meshFile, chunk)
    if start is None:
        return {}, getattr(chunk, "classEnd", 0) or 0

    props = {}
    offset = start
    while offset + 4 <= min(end, len(data)):
        prop_start = offset
        try:
            name_id, offset = _read_u16_at(data, offset)
            type_id, offset = _read_u16_at(data, offset)
        except ValueError:
            return props, prop_start
        if not (0 < name_id < len(names)) or not (0 < type_id < len(names)):
            return props, prop_start

        prop_name = names[name_id]
        prop_type = names[type_id]
        offset += 2  # W2 property marker, normally 0xFFFF.
        size_offset = offset
        try:
            prop_size, offset = _read_i32_at(data, offset)
        except ValueError:
            return props, prop_start

        value_offset = offset
        prop_end = size_offset + prop_size
        if prop_size < 4 or prop_end <= prop_start or prop_end > end or prop_end > len(data):
            return props, prop_start

        try:
            if prop_name == "materials" and prop_type in ("@*IMaterial", "array:2,0,handle:IMaterial"):
                count, value_pos = _read_u32_at(data, value_offset)
                value_pos += 4
                handles = []
                for _ in range(count):
                    material_id, value_pos = _read_i32_at(data, value_pos)
                    handles.append(SimpleNamespace(
                        Reference=material_id - 1 if material_id > 0 else None,
                        DepotPath=None,
                        ChunkHandle=material_id > 0,
                        val=material_id,
                    ))
                props["materials"] = SimpleNamespace(Count=int(count), Handles=handles)
            elif prop_name == "materialNames" and prop_type in ("@String", "array:2,0,String"):
                count, value_pos = _read_u32_at(data, value_offset)
                value_pos += 4
                material_names = []
                for _ in range(count):
                    value, value_pos = _read_w2_encoded_string_at(data, value_pos)
                    material_names.append(value)
                props["materialNames"] = material_names
            elif prop_name == "importFile" and prop_type == "String":
                value, _value_pos = _read_w2_encoded_string_at(data, value_offset)
                props["importFile"] = value
        except Exception:
            log.debug("Failed to parse W2 CMesh property %s/%s", prop_name, prop_type, exc_info=True)

        offset = prop_end

    return props, offset


def _w2_prop_string_value(prop):
    if not prop:
        return ""
    if isinstance(prop, str):
        return prop
    string_value = getattr(prop, "String", None)
    if isinstance(string_value, str):
        return string_value
    nested = getattr(string_value, "String", None)
    if isinstance(nested, str):
        return nested
    value = getattr(prop, "Value", None)
    if isinstance(value, str):
        return value
    return ""


def _w2_import_file_stem(import_file):
    import_file = str(import_file or "").replace("\\", "/").strip()
    if not import_file:
        return ""
    base = import_file.rsplit("/", 1)[-1]
    stem, _ext = os.path.splitext(base)
    return stem


def _w2_string_array_values(prop):
    values = []
    for element in getattr(prop, "elements", []) or []:
        value = getattr(element, "String", "")
        if hasattr(value, "String"):
            value = value.String
        value = str(value or "").strip()
        if value:
            values.append(value)
    return values


def _iter_w2_mesh_paths_from_import_file(import_file):
    normalized = str(import_file or "").replace("/", "\\").strip()
    if not normalized:
        return

    lower = normalized.lower()
    marker = "\\w2_assets\\"
    marker_idx = lower.find(marker)
    if marker_idx >= 0:
        normalized = normalized[marker_idx + len(marker):]
    else:
        drive, tail = os.path.splitdrive(normalized)
        if drive:
            normalized = tail.lstrip("\\")

    root, _ext = os.path.splitext(normalized)
    if not root:
        return

    candidates = []
    if "\\export\\" in root.lower():
        parts = re.split(r"\\export\\", root, maxsplit=1, flags=re.IGNORECASE)
        if len(parts) == 2:
            candidates.append(parts[0] + "\\model\\" + parts[1] + ".w2mesh")
    candidates.append(root + ".w2mesh")

    seen = set()
    for candidate in candidates:
        candidate = candidate.replace("/", "\\").lstrip("\\")
        key = candidate.lower()
        if candidate and key not in seen:
            seen.add(key)
            yield candidate


def _w2_material_names_from_matching_mesh(import_file, import_file_stem, material_count, version):
    if not import_file or not import_file_stem or material_count <= 0:
        return []

    cache_key = (str(import_file).lower(), int(material_count), int(version))
    if cache_key in _W2_IMPORT_FILE_MATERIAL_NAMES_CACHE:
        return list(_W2_IMPORT_FILE_MATERIAL_NAMES_CACHE[cache_key])

    for repo_path in _iter_w2_mesh_paths_from_import_file(import_file) or []:
        try:
            full_path = repo_file(repo_path, version=version)
        except Exception:
            continue
        if not full_path or not os.path.exists(win_safe_path(full_path)):
            continue
        try:
            with open(win_safe_path(full_path), "rb") as ref_handle:
                ref_mesh = getCR2W(ref_handle)
                for ref_chunk in getattr(getattr(ref_mesh, "CHUNKS", None), "CHUNKS", []) or []:
                    if getattr(ref_chunk, "Type", None) != "CMesh":
                        continue
                    names = _w2_string_array_values(ref_chunk.GetVariableByName("materialNames"))
                    if len(names) == material_count:
                        result = [f"{import_file_stem}_{name}" for name in names]
                        _W2_IMPORT_FILE_MATERIAL_NAMES_CACHE[cache_key] = list(result)
                        return result
        except Exception:
            log.debug("Failed to read W2 material names from matching mesh %s", full_path, exc_info=True)

    _W2_IMPORT_FILE_MATERIAL_NAMES_CACHE[cache_key] = []
    return []


def _w2_fallback_material_name(mesh_name, import_file_stem, fallback_prefix, idx, material_count):
    if import_file_stem:
        hint = f"{mesh_name} {import_file_stem}".lower()
        if "hair" in hint or "coif" in hint:
            if material_count == 1 or idx == 0:
                return f"{import_file_stem}_hair"
        if material_count == 1:
            return import_file_stem
        return f"{import_file_stem}_Material{idx}"
    return f"{fallback_prefix}_Material{idx}"


def _w2_uncooked_vertex_stride(num_bones_per_vertex: int, num_extra_floats: int):
    return (
        12  # position
        + num_bones_per_vertex  # bone ids
        + (num_bones_per_vertex * 4)  # weights
        + 12  # normals
        + 4  # color
        + 8  # uv1
        + 8  # uv2
        + 12  # tangent
        + (num_extra_floats * 4)
    )


def _w2_uncooked_counts_fit(data_len: int, data_offset: int, vertices_count: int,
                            indices_count: int, vertex_stride: int):
    min_size = data_offset + 1 + (vertices_count * vertex_stride) + 1 + (indices_count * 2)
    return min_size <= data_len


# Hard upper bounds for W2 submesh counts. Anything larger is a parser error
# (misaligned read producing garbage), not a real mesh — letting it through
# causes Blender to attempt multi-terabyte allocations and crash. Limits chosen
# generously vs. real shipped W2 meshes (Triss et al. are well under 1M verts).
_W2_MAX_SUBMESH_VERTICES = 10_000_000
_W2_MAX_SUBMESH_INDICES = 30_000_000


def _validate_w2_submesh_counts(num_vertices, num_indices, source_label,
                                data_len=None, data_offset=None):
    if num_vertices is None or num_indices is None:
        raise ValueError(
            f"{source_label}: missing vertex/index count (verts={num_vertices}, indices={num_indices})"
        )
    if num_vertices < 0 or num_indices < 0:
        raise ValueError(
            f"{source_label}: negative count (verts={num_vertices}, indices={num_indices})"
        )
    if num_vertices > _W2_MAX_SUBMESH_VERTICES or num_indices > _W2_MAX_SUBMESH_INDICES:
        raise ValueError(
            f"{source_label}: implausible counts (verts={num_vertices}, indices={num_indices}) "
            f"- parser likely misaligned"
        )
    if num_indices % 3 != 0:
        raise ValueError(
            f"{source_label}: index count {num_indices} is not a multiple of 3"
        )
    if data_len is not None and data_offset is not None:
        # Lower bound: indices alone (2 bytes each) must fit somewhere in the file.
        if data_offset + num_indices * 2 > data_len:
            raise ValueError(
                f"{source_label}: counts (verts={num_vertices}, indices={num_indices}) "
                f"would read past end of file (offset={data_offset}, data_len={data_len})"
            )


def _read_w2_vlq_at(data: bytes, offset: int):
    if offset >= len(data):
        raise ValueError("Unexpected end of W2 VLQ data")
    b1 = data[offset]
    offset += 1
    sign = (b1 & 128) == 128
    has_next = (b1 & 64) == 64
    size = b1 % 128 % 64
    shift = 6
    while has_next:
        if offset >= len(data):
            raise ValueError("Unexpected end of W2 VLQ data")
        b = data[offset]
        offset += 1
        size = (b % 128) << shift | size
        has_next = (b & 128) == 128
        shift += 7
    return (-size if sign else size), offset


def _has_w2_expected_vlq_at(data: bytes, offset: int, expected: int, max_padding=4):
    for padding in range(max_padding + 1):
        try:
            value, _end = _read_w2_vlq_at(data, offset + padding)
        except ValueError:
            continue
        if value == expected:
            return True
    return False


def _looks_like_w2_uncooked_mesh_payload(data: bytes, payload_start: int, version: int):
    """Return True when the W2 mesh payload has a plausible uncooked header.

    The older reader used byte 3 == 5 as a cooked marker. Some shipped W2
    uncooked meshes also have that value in their small payload prefix, so check
    the first submesh header before committing to the cooked path.
    """
    if payload_start < 0 or payload_start >= len(data):
        return False

    header_size = 3 if version <= 86 else 7
    offset = payload_start + header_size
    if offset >= len(data):
        return False

    nb_submeshes = data[offset]
    if nb_submeshes <= 0 or nb_submeshes > 128:
        return False

    try:
        offset += 1
        vertex_type, offset = _read_u16_at(data, offset)
        _material_id, offset = _read_u32_at(data, offset)
    except ValueError:
        return False

    if vertex_type > 64:
        return False

    num_extra_floats = 9 if version > 100 else 3
    vertex_stride = _w2_uncooked_vertex_stride(4, num_extra_floats)
    for unknown_size in (1, 2):
        try:
            value_offset = offset + unknown_size
            vertices_count, value_offset = _read_u32_at(data, value_offset)
            indices_count, value_offset = _read_u32_at(data, value_offset)
        except ValueError:
            continue
        if (
            _is_plausible_w2_uncooked_counts(vertices_count, indices_count)
            and _w2_uncooked_counts_fit(len(data), value_offset, vertices_count, indices_count, vertex_stride)
            and _has_w2_expected_vlq_at(data, value_offset, vertices_count)
        ):
            return True

    return False


def _is_plausible_w2_uncooked_counts(vertices_count, indices_count):
    if vertices_count is None or indices_count is None:
        return False
    if vertices_count <= 0 or vertices_count > 5_000_000:
        return False
    if indices_count <= 0 or indices_count > 15_000_000:
        return False
    if indices_count % 3:
        return False
    return True


def _read_w2_expected_vlq(br: bStream, expected: int, max_padding=4):
    if expected <= 0:
        return ReadVLQInt32(br.fhandle)

    start = br.tell()
    for padding in range(max_padding + 1):
        br.seek(start + padding)
        try:
            value = ReadVLQInt32(br.fhandle)
        except Exception:
            continue
        if value == expected:
            return value

    br.seek(start)
    return ReadVLQInt32(br.fhandle)


def _normalize_embedded_cmesh_chunk_index(value):
    if value is None:
        return None
    if isinstance(value, (list, tuple, set)):
        values = sorted({int(index) for index in value if index is not None})
        if not values:
            return None
        if len(values) > 1:
            raise ValueError(
                f"embedded_cmesh_chunk_index selects one Witcher 2 CMesh chunk; got {values}"
            )
        return values[0]
    return int(value)


def load_bin_mesh(filename, keep_lod_meshes = True, keep_proxy_meshes = False, embedded_cmesh_chunk_index=None):
    #OPTIONS
    cToLin = True

    #raise NotImplementedError
    embedded_cmesh_chunk_index = _normalize_embedded_cmesh_chunk_index(embedded_cmesh_chunk_index)
    _diag_label = f'{filename}{f" [embedded CMesh chunk {embedded_cmesh_chunk_index}]" if embedded_cmesh_chunk_index is not None else ""}'
    log.debug('FileLoading: %s', _diag_label)

    f = None
    raw_data = None
    if embedded_cmesh_chunk_index is not None:
        meshFile, raw_data = _load_cached_w2_embedded_cmesh_file(filename)
    else:
        f = open_cr2w_read_stream(win_safe_path(filename))
        meshFile = getCR2W(f)
    if f is None and getattr(getattr(meshFile, "HEADER", None), "version", 999) > 115:
        f = open_cr2w_read_stream(win_safe_path(filename))
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
        if raw_data is None:
            f.seek(0)
            raw_data = f.read()
        br:bStream = bStream(data = raw_data)
        if f is not None:
            f.close()
        the_material_names = []
        the_materials = SimpleNamespace(Count=0, Handles=[])
        cmesh_count = sum(1 for item in meshFile.CHUNKS.CHUNKS if getattr(item, "Type", None) == "CMesh")
        selected_cmesh_found = False

        chunk: W_CLASS
        for chunk in meshFile.CHUNKS.CHUNKS:
            if chunk.Type == "CMesh":
                if embedded_cmesh_chunk_index is not None and getattr(chunk, "ChunkIndex", None) != embedded_cmesh_chunk_index:
                    continue
                selected_cmesh_found = True
                _clear_w2_cmesh_runtime_buffers(chunk)
                raw_props, payload_start = _read_w2_cmesh_header(meshFile, chunk, raw_data)
                the_materials = chunk.GetVariableByName("materials") or raw_props.get("materials")
                the_material_names_chunk = chunk.GetVariableByName("materialNames")
                the_material_names = []
                import_file_path = (
                    _w2_prop_string_value(chunk.GetVariableByName("importFile"))
                    or raw_props.get("importFile")
                )
                import_file_stem = _w2_import_file_stem(import_file_path)
                chunk_mesh_name = meshName
                if embedded_cmesh_chunk_index is not None and import_file_stem:
                    chunk_mesh_name = import_file_stem
                    meshName = chunk_mesh_name
                    CData.modelName = chunk_mesh_name
                fallback_prefix = chunk_mesh_name
                if cmesh_count > 1 or embedded_cmesh_chunk_index is not None:
                    fallback_prefix = f"{chunk_mesh_name}_cmesh{getattr(chunk, 'ChunkIndex', 0)}"
                if the_material_names_chunk:
                    for mat_name in _w2_string_array_values(the_material_names_chunk):
                        the_material_names.append(chunk_mesh_name+"_"+mat_name)
                elif raw_props.get("materialNames"):
                    for mat_name in raw_props.get("materialNames") or []:
                        the_material_names.append(chunk_mesh_name+"_"+mat_name)
                else:
                    if the_materials:
                        matched_names = _w2_material_names_from_matching_mesh(
                            import_file_path,
                            import_file_stem,
                            the_materials.Count,
                            meshFile.HEADER.version,
                        )
                        if matched_names:
                            the_material_names.extend(matched_names)
                        else:
                            for idx in range(the_materials.Count):
                                the_material_names.append(
                                    _w2_fallback_material_name(
                                        chunk_mesh_name,
                                        import_file_stem,
                                        fallback_prefix,
                                        idx,
                                        the_materials.Count,
                                    )
                            )
                    else:
                        # Allow meshes with no materials
                        the_materials = SimpleNamespace(Count=0, Handles=[])

                #go to buffer start
                br.seek(payload_start)

                is_uncooked = False
                looks_uncooked = _looks_like_w2_uncooked_mesh_payload(
                    raw_data, payload_start, meshFile.HEADER.version)
                yes = br.read(8)
                if (looks_uncooked or len(yes) < 4 or yes[3] != 5 or meshFile.HEADER.version <= 83): #
                    is_uncooked = True
                    br.seek(payload_start)
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
                        num_extra_floats = 9 if extra_vectors else 3
                        submesh = SubmeshData()
                        submesh.vertexType_w2 = br.readUInt16() # TODO check this
                        if submesh.vertexType_w2 in [1,5] :
                            submesh.vertexType = EMeshVertexType.EMVT_SKINNED
                        
                        submesh.materialID = br.readUInt32()
                        header_unknown_pos = br.tell()
                        count_layout_found = False
                        for unknown_size in (1, 2):
                            br.seek(header_unknown_pos)
                            br.read(unknown_size)
                            submesh.verticesCount = br.readUInt32()
                            submesh.indicesCount = br.readUInt32()
                            vertex_stride = _w2_uncooked_vertex_stride(4, num_extra_floats)
                            if (
                                _is_plausible_w2_uncooked_counts(submesh.verticesCount, submesh.indicesCount)
                                and _w2_uncooked_counts_fit(
                                    len(raw_data), br.tell(), submesh.verticesCount,
                                    submesh.indicesCount, vertex_stride)
                                and _has_w2_expected_vlq_at(raw_data, br.tell(), submesh.verticesCount)
                            ):
                                count_layout_found = True
                                break
                        if not count_layout_found:
                            br.seek(header_unknown_pos)
                            br.read(1)
                            submesh.verticesCount = br.readUInt32()
                            submesh.indicesCount = br.readUInt32()

                        _validate_w2_submesh_counts(
                            submesh.verticesCount,
                            submesh.indicesCount,
                            f"W2 uncooked submesh in {meshName} (chunk {getattr(chunk, 'ChunkIndex', '?')}, vertexType={submesh.vertexType_w2})",
                            data_len=len(raw_data),
                            data_offset=br.tell(),
                        )

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
                        _read_w2_expected_vlq(br, meshInfo.numVertices)
                        num_extra_floats = 9 if extra_vectors else 3
                        _read_vertices_uncooked_numpy(
                            br.fhandle, meshInfo.numVertices,
                            meshInfo.numBonesPerVertex, num_extra_floats,
                            cToLin, CData, final_meshdata,
                            submesh.vertexType == EMeshVertexType.EMVT_SKINNED)
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
                        _read_w2_expected_vlq(br, meshInfo.numIndices)
                        final_meshdata.faces = _read_indices_numpy(br.fhandle, meshInfo.numIndices)
                        lastIOffset = br.tell()

                        bonesId_count = ReadBit6(br.fhandle)
                        for _ in range(bonesId_count):
                            submesh.bonesId.append(br.readUInt16())

                        # Replace skinning ids with mapped ids. Static W2 meshes
                        # still use the same fixed vertex stride, but do not have
                        # a bone map.
                        if final_meshdata.skinningVerts and submesh.bonesId:
                            valid_skinning_verts = []
                            invalid_skinning_verts = set()
                            for skinningVert in final_meshdata.skinningVerts:
                                if 0 <= skinningVert.boneId < len(submesh.bonesId):
                                    skinningVert.boneId = submesh.bonesId[skinningVert.boneId]
                                    valid_skinning_verts.append(skinningVert)
                                else:
                                    invalid_skinning_verts.add(id(skinningVert))
                            if invalid_skinning_verts:
                                CData.w3_DataCache.vertices = [
                                    entry for entry in CData.w3_DataCache.vertices
                                    if id(entry) not in invalid_skinning_verts
                                ]
                                final_meshdata.skinningVerts = valid_skinning_verts
                        elif final_meshdata.skinningVerts:
                            invalid_skinning_verts = {id(entry) for entry in final_meshdata.skinningVerts}
                            CData.w3_DataCache.vertices = [
                                entry for entry in CData.w3_DataCache.vertices
                                if id(entry) not in invalid_skinning_verts
                            ]
                            final_meshdata.skinningVerts = []

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
                    nbSubmeshes = ReadBit6(br.fhandle)

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

                    log.debug(
                        "W2 cooked submesh vertex layout: vertype=%s vertsize=%s vertStart=%s",
                        submesh.vertexType_w2,
                        vertexSize,
                        br.tell(),
                    )
                    vertex_offset = submeshStartPos + submesh.verticesStart * vertexSize
                    vertex_end = vertex_offset + submesh.verticesCount * vertexSize
                    index_offset = submeshStartPos + meshIndicesOffset + submesh.indicesStart * 2
                    index_end = index_offset + submesh.indicesCount * 2
                    if (
                        vertex_offset < 0
                        or index_offset < 0
                        or vertex_end > len(raw_data)
                        or index_end > len(raw_data)
                    ):
                        raise ValueError(
                            f"W2 cooked submesh in {meshName} (chunk {getattr(chunk, 'ChunkIndex', '?')}, "
                            f"vertexType={submesh.vertexType_w2}) ranges past end of file "
                            f"(vertex={vertex_offset}:{vertex_end}, index={index_offset}:{index_end}, "
                            f"data_len={len(raw_data)})"
                        )

                    #?#################?#
                    #?### Vertices ####?#
                    #?#################?#
                    br.seek(vertex_offset)
                    _read_vertices_cooked_w2_numpy(
                        br.fhandle, submesh.verticesCount, vertexSize,
                        isSkinned, 4, hasSecondUVLayer, cToLin,
                        submesh.bonesId, CData.boneData.jointNames,
                        CData, final_meshdata)
                    br.seek(vertex_end)


                    #?#################?#
                    #?#### Indices ####?#
                    #?#################?#
                    br.seek(index_offset) #br.seek(bufferInfo.offset + lastIOffset, 0)
                    final_meshdata.faces = _read_indices_numpy(br.fhandle, submesh.indicesCount)
                    lastIOffset = br.tell() - meshIndicesOffset #! bufferInfo.offset

                def readSubmeshData(br):
                    submesh = SubmeshData()
                    submesh.vertexType_w2 = br.readUByte()

                    submesh.verticesStart = br.readUInt32()
                    submesh.indicesStart = br.readUInt32()
                    submesh.verticesCount = br.readUInt32()
                    submesh.indicesCount = br.readUInt32()

                    _validate_w2_submesh_counts(
                        submesh.verticesCount,
                        submesh.indicesCount,
                        f"W2 cooked submesh in {meshName} (chunk {getattr(chunk, 'ChunkIndex', '?')}, vertexType={submesh.vertexType_w2})",
                    )
                    # verticesStart/indicesStart are used as seek offsets into the
                    # submesh data block; if they're absurd we'll seek past EOF and
                    # hand garbage to the numpy reader -> bogus Blender allocation.
                    if submesh.verticesStart < 0 or submesh.verticesStart > _W2_MAX_SUBMESH_VERTICES:
                        raise ValueError(
                            f"W2 cooked submesh in {meshName}: implausible verticesStart={submesh.verticesStart}"
                        )
                    if submesh.indicesStart < 0 or submesh.indicesStart > _W2_MAX_SUBMESH_INDICES:
                        raise ValueError(
                            f"W2 cooked submesh in {meshName}: implausible indicesStart={submesh.indicesStart}"
                        )

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
                        if LODs and not keep_lod_meshes and i not in LODs[0].submeshesIds:
                            continue
                        br.seek(subMeshesStartAdress + 4)
                        loadSubmesh(br, subMeshesData[i], meshIndicesOffset, materialIds, boneNames)
                if is_uncooked:
                    pass
                else:
                    loadSubmeshes(br, LODs, materialIds, boneNames) # FOR COOKED
                break

        if embedded_cmesh_chunk_index is not None and not selected_cmesh_found:
            raise ValueError(
                f"Embedded Witcher 2 CMesh chunk {embedded_cmesh_chunk_index} not found in {filename}"
            )
        log.debug('FileLoading: %s - parse complete (%d submeshes)',
                    _diag_label, len(getattr(CData, 'meshDataAllMeshes', []) or []))
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

            # wcclite uses CMesh.boundingBox to calculate quant instead of cookedData, cookedData is more accurate to what the game displays
            use_bbox_uncook = False
            
            if use_bbox_uncook:
                bbox_var = chunk.GetVariableByName("boundingBox")
                if bbox_var is not None:
                    bbox_min = bbox_var.GetVariableByName("Min")
                    bbox_max = bbox_var.GetVariableByName("Max")
                    if bbox_min is not None and bbox_max is not None:
                        min_x = bbox_min.More[0].Value; min_y = bbox_min.More[1].Value; min_z = bbox_min.More[2].Value
                        max_x = bbox_max.More[0].Value; max_y = bbox_max.More[1].Value; max_z = bbox_max.More[2].Value
                        bufferInfos.quantizationOffset.x = min_x
                        bufferInfos.quantizationOffset.y = min_y
                        bufferInfos.quantizationOffset.z = min_z
                        bufferInfos.quantizationScale.x = max_x - min_x
                        bufferInfos.quantizationScale.y = max_y - min_y
                        bufferInfos.quantizationScale.z = max_z - min_z

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
            chunk_is_static = chunk.GetVariableByName("isStatic").Value if chunk.GetVariableByName("isStatic") else None
            CData.isStatic = _derive_mesh_is_static(CData.meshInfos) if CData.meshInfos else bool(chunk_is_static)
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

    log.debug('FileLoading: %s - parse complete (W3, %d submeshes)',
                _diag_label, len(getattr(CData, 'meshDataAllMeshes', []) or []))
    return (CData, bufferInfos, the_material_names, the_materials, meshName, meshFile)
