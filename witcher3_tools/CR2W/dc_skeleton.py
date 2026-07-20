import json
import math
import struct

from .bin_helpers import getString, readString
from .bStream import *
from .w3_types import Quaternion, Vector3D
from .read_json_w3 import readCSkeletonData
from . import w3_types
from .CR2W_types import getCR2W
from .Types.VariousTypes import CQuaternion
from .havok_parser import HavokPackfile
import logging
log = logging.getLogger(__name__)

class MimicPose(w3_types.base_w3):
    """docstring for MimicPose."""
    def __init__(self):
        #super(MimicPose, self).__init__()
        self.name = ""
        self.bones = []
        self.duration = 0.0
        self.numFrames = 0
        self.dt = 0.0
    def __iter__(self):
        return iter(['name','bones','duration','numFrames','dt'])

class Bone(w3_types.base_w3):
    """docstring for Bone."""
    def __init__(self):
        #super(Bone, self).__init__()
        self.index = 0
        self.BoneName : str = "????"
        self.position_dt = 0
        self.position_numFrames = 0
        self.positionFrames =[] #new List<Vector>()
        self.rotation_dt = 0
        self.rotation_numFrames = 0
        self.rotationFrames =[] #new List<Quaternion>()
        self.scale_dt = 0
        self.scale_numFrames = 0
        self.scaleFrames =[] #new List<Vector>()

def read_skelly(skelly, *, allow_missing_rig_data=False):
    this_skeleton = w3_types.w2rig(
                nbBones=94,
                names= [],
                tracks= [],
                parentIdx = [],
                positions = [],
                rotations = [],
                scales = [])

    def prop_entries(prop):
        for attr in ("More", "value", "elements"):
            entries = getattr(prop, attr, None)
            if isinstance(entries, list):
                return entries
        return []

    def entry_name(entry):
        text = str(getattr(entry, "elementName", "") or "").strip()
        if text:
            return text
        if getattr(entry, "theName", "") in {
            "name", "m_name", "nameAsCName", "m_nameAsCName",
        } and hasattr(entry, "ToString"):
            try:
                text = str(entry.ToString() or "").strip()
            except Exception:
                text = ""
            if text:
                return text
        for prop_name in ("nameAsCName", "m_nameAsCName", "name", "m_name"):
            prop = entry.GetVariableByName(prop_name) if hasattr(entry, "GetVariableByName") else None
            if prop is None:
                continue
            for attr_path in (("Index", "String"), ("Value",), ("String",), ("value",)):
                value = prop
                for attr in attr_path:
                    value = getattr(value, attr, None)
                    if value is None:
                        break
                if hasattr(value, "ToString"):
                    value = value.ToString()
                elif hasattr(value, "String"):
                    value = getattr(value, "String", "")
                text = str(value or "").strip()
                if text:
                    return text
        return ""

    for item in skelly.PROPS:
        if item.theName in ("bones", "m_bones"):
            for idx, bone in enumerate(prop_entries(item)):
                bone_name = entry_name(bone)
                if not bone_name:
                    raise ValueError(f"CSkeleton bone {idx} has no name")
                this_skeleton.names.append(bone_name)
            this_skeleton.nbBones = len(this_skeleton.names)
        if item.theName in ("tracks", "m_tracks"):
            track_entries = prop_entries(item)
            direct_cname_entries = [
                entry for entry in track_entries
                if getattr(entry, "theName", "") in {"nameAsCName", "m_nameAsCName"}
            ]
            if direct_cname_entries:
                track_entries = direct_cname_entries
            for track in track_entries:
                track_name = entry_name(track)
                if track_name:
                    this_skeleton.tracks.append(track_name)
        if item.theName in ("parentIndices", "m_parentIndices"):
            parent_indices = getattr(item, "value", None)
            if isinstance(parent_indices, list):
                this_skeleton.parentIdx = parent_indices[:]
    source_cr2w = getattr(skelly, "_W_CLASS__CR2WFILE", None)
    source_file = getattr(source_cr2w, "fileName", "<memory>")
    source_version = getattr(getattr(source_cr2w, "HEADER", None), "version", 999)
    rig_data = getattr(getattr(skelly, "rigData", None), "rigData", []) or []
    for idx, data in enumerate(rig_data):
        position = data.position
        rotation = data.rotation
        scale = data.scale
        repaired = []
        if not all(math.isfinite(float(getattr(position, axis))) for axis in "xyzw"):
            position = w3_types.Vector3D(0.0, 0.0, 0.0)
            repaired.append("translation")
        if not all(math.isfinite(float(getattr(rotation, axis))) for axis in "xyzw"):
            rotation = w3_types.Quaternion(0.0, 0.0, 0.0, 1.0)
            repaired.append("rotation")
        if not all(math.isfinite(float(getattr(scale, axis))) for axis in "xyzw"):
            scale = w3_types.Vector3D(1.0, 1.0, 1.0)
            repaired.append("scale")
        if repaired:
            bone_name = this_skeleton.names[idx] if idx < len(this_skeleton.names) else str(idx)
            log.warning(
                "Repaired non-finite CSkeleton %s for bone '%s' in %s",
                "/".join(repaired),
                bone_name,
                source_file,
            )
        this_skeleton.positions.append(position)
        this_skeleton.rotations.append(rotation)
        this_skeleton.scales.append(scale)
    bone_count = len(this_skeleton.names)
    if bone_count:
        if len(this_skeleton.parentIdx) != bone_count:
            raise ValueError(
                f"CSkeleton parent count {len(this_skeleton.parentIdx)} does not match bone count {bone_count}"
            )
        seen_names = set()
        for idx, (bone_name, parent_idx) in enumerate(zip(this_skeleton.names, this_skeleton.parentIdx)):
            name_key = bone_name.casefold()
            if name_key in seen_names:
                raise ValueError(f"CSkeleton has duplicate bone name '{bone_name}'")
            seen_names.add(name_key)
            if parent_idx != -1 and not (0 <= parent_idx < idx):
                raise ValueError(
                    f"CSkeleton bone '{bone_name}' has invalid parent index {parent_idx}"
                )
        if rig_data and len(rig_data) != bone_count:
            raise ValueError(
                f"CSkeleton transform count {len(rig_data)} does not match bone count {bone_count}"
            )
        if not rig_data and source_version > 115 and not allow_missing_rig_data:
            raise ValueError(
                f"CSkeleton has {bone_count} named bones but no reference-pose transforms"
            )
        if not rig_data and source_version <= 115:
            this_skeleton.positions.extend(
                w3_types.Vector3D(0.0, 0.0, 0.0) for _ in range(bone_count)
            )
            this_skeleton.rotations.extend(
                w3_types.Quaternion(0.0, 0.0, 0.0, 1.0) for _ in range(bone_count)
            )
            this_skeleton.scales.extend(
                w3_types.Vector3D(1.0, 1.0, 1.0) for _ in range(bone_count)
            )
    return this_skeleton

def create_CMimicFace(file):
    mimicPoses = []

    mimic = file.CHUNKS.CHUNKS[0]

    chunkMimicFace = file.CHUNKS.CHUNKS[0]
    mimicSkeleton = read_skelly(file.CHUNKS.CHUNKS[1])
    floatTrackSkeleton = read_skelly(file.CHUNKS.CHUNKS[2])

    #!INVERT W CHECK IF CAN BE CANGED SOMEWHERE ELSE??
    # for i, rot in enumerate(mimicSkeleton.rotations):
    #     mimicSkeleton.rotations[i].w = -rot.w
    # for i, rot in enumerate(floatTrackSkeleton.rotations):
    #     floatTrackSkeleton.rotations[i].w = -rot.w



    # # convert mapping into bone array
    mimicMapping = chunkMimicFace.GetVariableByName("mapping").value
    # # ie. mimicMapping[0] = "uv_center_slide2"

    # # give each mapping a name
    tracks = floatTrackSkeleton.tracks


    mimicPoses = chunkMimicFace.GetVariableByName("mimicPoses").value[:]
    # # save poses into
    final_poses = []
    for idx, mimicbones in enumerate(mimicPoses):
        pose = MimicPose()
        pose.name = tracks[idx]
        pose.numFrames = 1
        for jdx, bone in enumerate(mimicbones):
            myBone = Bone()
            map = mimicMapping[jdx]
            myBone.BoneName = mimicSkeleton.names[map]
            myBone.positionFrames.append(Vector3D(bone.x, bone.y, bone.z))
            myBone.rotationFrames.append(Quaternion(bone.pitch, bone.yaw, bone.roll, bone.w))
            myBone.scaleFrames.append(Vector3D(bone.scale_x, bone.scale_y, bone.scale_z))
            pose.bones.append(myBone)
        final_poses.append(pose)
    CMimicFace = w3_types.CMimicFace(name = "name",
                                     mimicSkeleton = mimicSkeleton,
                                     floatTrackSkeleton = floatTrackSkeleton,
                                     mimicPoses=final_poses)
    return CMimicFace


# Witcher 2 mimic faces support.
#
# CMimicFaces (.w2faces) predates the Witcher 3 .w3fac layout.  It stores a
# handle to a CSkeleton plus a nested array of EngineQsTransform poses.  Keep
# this parser separate so the W3 mimicFaceFile/.w3fac path stays untouched.
def _w2_cname(cr2w_file, index):
    if index is None or index < 0:
        return ""
    cnames = getattr(cr2w_file, "CNAMES", []) or []
    if index >= len(cnames):
        return ""
    name = getattr(cnames[index], "name", "")
    return str(getattr(name, "value", name) or "")


def _w2_chunk_range(cr2w_file, chunk_index):
    exports = getattr(cr2w_file, "CR2WExport", None) or []
    if chunk_index is None or chunk_index < 0 or chunk_index >= len(exports):
        return (0, 0)
    export = exports[chunk_index]
    start = int(getattr(cr2w_file, "start", 0) or 0) + int(getattr(export, "dataOffset", 0) or 0)
    end = start + int(getattr(export, "dataSize", 0) or 0)
    return (start, end)


def _iter_w2_chunk_props(cr2w_file, file_data, chunk_index):
    start, end = _w2_chunk_range(cr2w_file, chunk_index)
    pos = start
    file_size = len(file_data)
    end = min(end, file_size)

    while pos + 10 <= end:
        name_idx, type_idx, unk = struct.unpack_from("<HHh", file_data, pos)
        size = struct.unpack_from("<I", file_data, pos + 6)[0]
        if name_idx == 0 or type_idx == 0 or unk != -1 or size < 4:
            break

        data_start = pos + 10
        data_end = data_start + size - 4
        if data_end < data_start or data_end > end:
            break

        yield _w2_cname(cr2w_file, name_idx), _w2_cname(cr2w_file, type_idx), data_start, data_end
        pos = data_end


def _skip_w2_array_element_type_marker(cr2w_file, file_data, pos, end):
    if pos + 4 > end:
        return pos
    type_idx, marker = struct.unpack_from("<Hh", file_data, pos)
    if marker != -1:
        return pos
    if _w2_cname(cr2w_file, type_idx):
        return pos + 4
    return pos


def _read_w2_engine_qs_transform(file_data, pos, end):
    if pos >= end:
        raise ValueError("Unexpected end of W2 EngineQsTransform data")

    flags = file_data[pos]
    pos += 1
    x = y = z = 0.0
    pitch = yaw = roll = 0.0
    w = 1.0
    scale_x = scale_y = scale_z = 1.0

    if flags & 1:
        if pos + 12 > end:
            raise ValueError("Unexpected end of W2 EngineQsTransform position data")
        x, y, z = struct.unpack_from("<fff", file_data, pos)
        pos += 12
    if flags & 2:
        if pos + 16 > end:
            raise ValueError("Unexpected end of W2 EngineQsTransform rotation data")
        pitch, yaw, roll, w = struct.unpack_from("<ffff", file_data, pos)
        pos += 16
    if flags & 4:
        if pos + 12 > end:
            raise ValueError("Unexpected end of W2 EngineQsTransform scale data")
        scale_x, scale_y, scale_z = struct.unpack_from("<fff", file_data, pos)
        pos += 12

    return (x, y, z, pitch, yaw, roll, w, scale_x, scale_y, scale_z), pos


def _read_w2_mimic_pose_arrays(cr2w_file, file_data, data_start, data_end):
    pos = data_start
    if pos + 4 > data_end:
        return []

    pose_count = struct.unpack_from("<I", file_data, pos)[0]
    pos += 4
    pos = _skip_w2_array_element_type_marker(cr2w_file, file_data, pos, data_end)

    poses = []
    for _pose_index in range(pose_count):
        if pos + 4 > data_end:
            raise ValueError("Unexpected end of W2 mimic pose array")

        bone_count = struct.unpack_from("<I", file_data, pos)[0]
        pos += 4
        pos = _skip_w2_array_element_type_marker(cr2w_file, file_data, pos, data_end)

        transforms = []
        for _bone_index in range(bone_count):
            transform, pos = _read_w2_engine_qs_transform(file_data, pos, data_end)
            transforms.append(transform)
        poses.append(transforms)

    if pos != data_end:
        log.debug("W2 mimic pose parser ended at %s, expected %s", pos, data_end)
    return poses


def _find_w2_mimic_faces_chunk(cr2w_file, chunk_index=None):
    chunks = getattr(getattr(cr2w_file, "CHUNKS", None), "CHUNKS", None) or []
    if chunk_index is not None:
        chunk_index = int(chunk_index)
        if 0 <= chunk_index < len(chunks) and getattr(chunks[chunk_index], "name", "") == "CMimicFaces":
            return chunk_index
        raise ValueError(f"W2 CMimicFaces chunk not found at index {chunk_index}")

    for idx, chunk in enumerate(chunks):
        if getattr(chunk, "name", "") == "CMimicFaces":
            return idx
    raise ValueError("No W2 CMimicFaces chunk found")


def _find_w2_mimic_skeleton_chunk(cr2w_file, face_chunk_index):
    chunks = getattr(getattr(cr2w_file, "CHUNKS", None), "CHUNKS", None) or []
    face_chunk = chunks[face_chunk_index] if 0 <= face_chunk_index < len(chunks) else None
    skeleton_prop = face_chunk.GetVariableByName("mimicSkeleton") if face_chunk else None
    handles = getattr(skeleton_prop, "Handles", None) or []
    if handles:
        ref = getattr(handles[0], "Reference", None)
        if isinstance(ref, int) and 0 <= ref < len(chunks):
            return ref

    for idx in range(face_chunk_index + 1, len(chunks)):
        if getattr(chunks[idx], "name", "") == "CSkeleton":
            return idx
    return None


def _read_w2_mimic_skeleton(cr2w_file, file_data, skeleton_chunk_index):
    chunks = getattr(getattr(cr2w_file, "CHUNKS", None), "CHUNKS", None) or []
    if skeleton_chunk_index is None or skeleton_chunk_index < 0 or skeleton_chunk_index >= len(chunks):
        return w3_types.w2rig(nbBones=0, names=[], tracks=[], parentIdx=[], positions=[], rotations=[], scales=[])

    skel_chunk = chunks[skeleton_chunk_index]
    skeleton = read_skelly(skel_chunk)
    if getattr(skeleton, "names", None):
        return skeleton

    start, end = _w2_chunk_range(cr2w_file, skeleton_chunk_index)
    chunk_data = file_data[start:end]
    if HavokPackfile.find_magic(chunk_data) >= 0:
        return create_Skeleton_w2_uncooked(chunk_data, cr2w_file)

    return skeleton


def _w2_mimic_pose_name(index, pose_count, track_names=None):
    if pose_count == 3:
        low_pose_names = ("w2_low_ref", "w2_low_jaw", "w2_low_eyes")
        if index < len(low_pose_names):
            return low_pose_names[index]

    track_names = [str(name or "").strip() for name in (track_names or [])]
    track_names = [name for name in track_names if name]
    if track_names:
        if index == 0:
            return "w2_base"
        # This matches CEdMimicFacesEditor::UpdatePose:
        # pose 0 is displayed as <base>, pose N uses GetTrackName(N - 1).
        track_index = index - 1
        if 0 <= track_index < len(track_names):
            return track_names[track_index]

    return f"w2_pose_{index:03d}"


def create_CMimicFaces_w2(file, file_data, chunk_index=None, track_names=None):
    face_chunk_index = _find_w2_mimic_faces_chunk(file, chunk_index=chunk_index)
    skeleton_chunk_index = _find_w2_mimic_skeleton_chunk(file, face_chunk_index)
    mimicSkeleton = _read_w2_mimic_skeleton(file, file_data, skeleton_chunk_index)

    raw_poses = []
    for prop_name, prop_type, data_start, data_end in _iter_w2_chunk_props(file, file_data, face_chunk_index):
        if prop_name == "mimicPoses" and "EngineQsTransform" in prop_type:
            raw_poses = _read_w2_mimic_pose_arrays(file, file_data, data_start, data_end)
            break

    bone_names = list(getattr(mimicSkeleton, "names", []) or [])
    track_names = list(track_names or getattr(mimicSkeleton, "tracks", []) or [])
    if track_names and not getattr(mimicSkeleton, "tracks", None):
        mimicSkeleton.tracks = track_names[:]
    final_poses = []
    for pose_index, transforms in enumerate(raw_poses):
        pose = MimicPose()
        pose.name = _w2_mimic_pose_name(pose_index, len(raw_poses), track_names=track_names)
        pose.numFrames = 1
        pose.duration = 0.0
        pose.dt = 0.0

        for bone_index, transform in enumerate(transforms):
            x, y, z, pitch, yaw, roll, w, scale_x, scale_y, scale_z = transform
            myBone = Bone()
            myBone.index = bone_index
            myBone.BoneName = bone_names[bone_index] if bone_index < len(bone_names) else str(bone_index)
            myBone.position_numFrames = 1
            myBone.rotation_numFrames = 1
            myBone.scale_numFrames = 1
            myBone.positionFrames.append(Vector3D(x, y, z))
            myBone.rotationFrames.append(Quaternion(pitch, yaw, roll, w))
            myBone.scaleFrames.append(Vector3D(scale_x, scale_y, scale_z))
            pose.bones.append(myBone)

        final_poses.append(pose)

    CMimicFaces = w3_types.CMimicFace(
        name="w2faces",
        mimicSkeleton=mimicSkeleton,
        floatTrackSkeleton=w3_types.w2rig(nbBones=0, names=[], tracks=[], parentIdx=[], positions=[], rotations=[], scales=[]),
        mimicPoses=final_poses,
    )
    CMimicFaces.source_game = "w2"
    CMimicFaces.mimic_faces_chunk_index = face_chunk_index
    CMimicFaces.mimic_skeleton_chunk_index = skeleton_chunk_index
    return CMimicFaces


import os

from .CR2W_types import PROPERTY, getCR2W, W_CLASS


def create_Skeleton_w2(f, rigFile):
    """Parse a cooked Witcher 2 .w2rig file (hardcoded Havok-adjacent layout)."""
    this_skeleton = w3_types.w2rig(
            nbBones=94,
            names= [],
            tracks= [],
            parentIdx = [],
            positions = [],
            rotations = [],
            scales = [])


    for chunk in rigFile.CHUNKS.CHUNKS:
        if chunk.name == "CSkeleton":
            f.seek(0)
            br:bStream = bStream(data = f.read())
            f.close()
            br.seek(chunk.PROPS[-1].dataEnd)

            br.seek(3, os.SEEK_CUR)
            chunkSize = br.readUInt32() # = readU32(file)
            br.seek(36, os.SEEK_CUR) # unk
            br.seek(28, os.SEEK_CUR) # app info : version (4 bytes) + 24 bytes string
            br.seek(116, os.SEEK_CUR) # "__classname__", "__type__"...

            unk = br.readUInt32() # = readU32(file)
            endOfBonesNamesAdress = br.readUInt32() # = readU32(file)
            endOfBonesUnk1Adress = br.readUInt32() # = readU32(file)
            endOfBonesUnk2Adress = br.readUInt32() # = readU32(file)
            dataSize = br.readUInt32() # = readU32(file)
            br.seek(8, os.SEEK_CUR) # 2x dataSize
            br.seek(112, os.SEEK_CUR)

            #  Data chunk start
            dataStartAdress = br.tell()
            #log->addLineAndFlush(formatString("dataStartAdress = %d", dataStartAdress))
            br.seek(8, os.SEEK_CUR)
            nbBones = br.readUInt32()
            this_skeleton.nbBones = nbBones #skeleton.setBonesCount(nbBones)
            #log->addLineAndFlush(formatString("%d bones at %d", nbBones, br.tell()-4))
            br.seek(20, os.SEEK_CUR) # 3x bones count

            br.seek(16, os.SEEK_CUR)

            rootName = br.readString(16) # getString(br.fhandle) #core::stringc rootName = readStringFixedSize(file, 16)
            #log->addLineAndFlush(formatString("Root = %s", rootName.c_str()))

            bonesParentIdChunkAdress = br.tell()
            #long bonesNameChunkAdress = (dataStartAdress + endOfBonesNamesAdress) - totalNamesSize
            #long bonesNameChunkAdress = (dataStartAdress + endOfBonesNamesAdress) - (nbBones * 48)

            # Each bone record is 32 bytes: [16-byte data][16-byte null-padded name]
            bonesNameChunkAdress = (dataStartAdress + endOfBonesNamesAdress) - nbBones * 32

            offset = 0
            br.seek(bonesNameChunkAdress - 8)
            while True:
                fl = br.readFloat()
                if fl > 0.09 and fl < 10.1:
                    break
                br.seek(-8, os.SEEK_CUR)
                offset += 4
            bonesTransformChunkAdress = bonesNameChunkAdress - (offset + nbBones * 48)

            br.seek(bonesNameChunkAdress)

            for i in range(nbBones):
                br.seek(16, os.SEEK_CUR)                # skip 16-byte data field
                boneName = br.readString(16)             # 16-byte null-padded name field
                boneName = boneName.replace('\x00', '')
                this_skeleton.names.append(boneName)

            br.seek(bonesParentIdChunkAdress)
            for i in range(nbBones):
                this_skeleton.parentIdx.append(br.readInt16())

            br.seek(bonesTransformChunkAdress)
            for _ in range(nbBones):
                this_skeleton.positions.append(CQuaternion(br.fhandle))
                this_skeleton.rotations.append(CQuaternion(br.fhandle))
                this_skeleton.scales.append(CQuaternion(br.fhandle))

    return this_skeleton


def create_Skeleton_w2_uncooked(file_data, rigFile):
    """Parse an uncooked Witcher 2 .w2rig file with embedded Havok 6.5.0 packfile.

    Uses the havok_parser module to extract skeleton data from the embedded
    Havok packfile, then converts it into a w2rig object compatible with the
    rest of the import pipeline.
    """
    this_skeleton = w3_types.w2rig(
            nbBones=0,
            names=[],
            tracks=[],
            parentIdx=[],
            positions=[],
            rotations=[],
            scales=[])

    packfile = HavokPackfile.from_data(file_data)
    if packfile is None:
        log.warning("No Havok packfile found in W2 rig file")
        return this_skeleton

    havok_skel = packfile.read_skeleton()
    if havok_skel is None:
        log.warning("Failed to parse Havok skeleton data")
        return this_skeleton

    this_skeleton.nbBones = havok_skel.num_bones
    this_skeleton.names = havok_skel.names
    this_skeleton.tracks = havok_skel.tracks
    this_skeleton.parentIdx = havok_skel.parent_indices

    # Convert Havok QsTransform tuples (x, y, z, w) to w3_types.Quaternion
    # objects with X, Y, Z, W attributes (used by readBones -> readxyz/readXYZW)
    for pos, rot, scl in zip(havok_skel.positions, havok_skel.rotations, havok_skel.scales):
        this_skeleton.positions.append(w3_types.Quaternion(pos[0], pos[1], pos[2], pos[3]))
        this_skeleton.rotations.append(w3_types.Quaternion(rot[0], rot[1], rot[2], rot[3]))
        this_skeleton.scales.append(w3_types.Quaternion(scl[0], scl[1], scl[2], scl[3]))

    return this_skeleton


def _quaternion_from_dyng_transform(transform):
    rotation = [[float(transform[row][column]) for column in range(3)] for row in range(3)]
    trace = rotation[0][0] + rotation[1][1] + rotation[2][2]
    if trace > 0.0:
        root = math.sqrt(trace + 1.0)
        scale = 0.5 / root
        x = (rotation[2][1] - rotation[1][2]) * scale
        y = (rotation[0][2] - rotation[2][0]) * scale
        z = (rotation[1][0] - rotation[0][1]) * scale
        w = 0.5 * root
    else:
        next_axis = (1, 2, 0)
        axis = max(range(3), key=lambda idx: rotation[idx][idx])
        second = next_axis[axis]
        third = next_axis[second]
        root = math.sqrt(max(
            0.0,
            rotation[axis][axis] - rotation[second][second] - rotation[third][third] + 1.0,
        ))
        if root <= 1e-12:
            return w3_types.Quaternion(0.0, 0.0, 0.0, 1.0)
        scale = 0.5 / root
        values = [0.0, 0.0, 0.0, 0.0]
        values[axis] = 0.5 * root
        values[3] = (rotation[third][second] - rotation[second][third]) * scale
        values[second] = (rotation[second][axis] + rotation[axis][second]) * scale
        values[third] = (rotation[third][axis] + rotation[axis][third]) * scale
        x, y, z, w = values

    # Match REDengine's post-conversion sign convention.
    return w3_types.Quaternion(-x, -y, -z, w)


def _skeleton_from_dyng_resource(resource, tracks=()):
    nodes = list(resource.nodes)
    names = [node.name for node in nodes]
    indices = {}
    for idx, name in enumerate(names):
        indices.setdefault(name, idx)
    return w3_types.w2rig(
        nbBones=len(nodes),
        names=names,
        tracks=list(tracks or ()),
        parentIdx=[indices.get(node.parent, -1) for node in nodes],
        positions=[w3_types.Vector3D(*node.transform[3][:3]) for node in nodes],
        rotations=[_quaternion_from_dyng_transform(node.transform) for node in nodes],
        scales=[w3_types.Vector3D(1.0, 1.0, 1.0) for _node in nodes],
    )


def create_Skeleton(file):
    chunks = file.CHUNKS.CHUNKS
    dyng_chunk = next((chunk for chunk in chunks if chunk.name == "CDyngResource"), None)
    skeleton_chunk = next((chunk for chunk in chunks if chunk.name == "CSkeleton"), None)
    if skeleton_chunk is None:
        if dyng_chunk is not None:
            from ..physics.dyng import parse_dyng_chunk

            return _skeleton_from_dyng_resource(parse_dyng_chunk(dyng_chunk))
        raise ValueError("No CSkeleton chunk found")

    skelly = read_skelly(
        skeleton_chunk,
        allow_missing_rig_data=dyng_chunk is not None,
    )
    rig_data = getattr(getattr(skeleton_chunk, "rigData", None), "rigData", []) or []
    if not rig_data:
        if dyng_chunk is not None:
            from ..physics.dyng import parse_dyng_chunk

            return _skeleton_from_dyng_resource(parse_dyng_chunk(dyng_chunk), skelly.tracks)
    return skelly

def load_bin_face(fileName) -> w3_types.CMimicFace:
    face_fileName = fileName
    theFile = getCR2W(open_cr2w_read_stream(face_fileName))
    CMimicFace = create_CMimicFace(theFile)
    return CMimicFace

def _load_w2_track_names(track_skeleton_file):
    if not track_skeleton_file or not os.path.exists(track_skeleton_file):
        return []
    try:
        skeleton = load_bin_skeleton(track_skeleton_file)
    except Exception:
        log.warning("Failed to load W2 mimic track skeleton: %s", track_skeleton_file, exc_info=True)
        return []
    return [str(track or "").strip() for track in (getattr(skeleton, "tracks", []) or []) if str(track or "").strip()]


def load_bin_w2_faces(fileName, chunk_index=None, track_skeleton_file=None) -> w3_types.CMimicFace:
    f = open_cr2w_read_stream(fileName)
    file_data = f.cr2w_buf
    theFile = getCR2W(f)
    track_names = _load_w2_track_names(track_skeleton_file)
    return create_CMimicFaces_w2(theFile, file_data, chunk_index=chunk_index, track_names=track_names)

def load_bin_skeleton(fileName):
    f = open_cr2w_read_stream(fileName)
    theFile = getCR2W(f)

    if theFile.HEADER.version <= 115:
        # W2 format: check if uncooked (embedded Havok) or cooked
        file_data = f.cr2w_buf
        f.seek(0)
        if HavokPackfile.find_magic(file_data) >= 0:
            rig = create_Skeleton_w2_uncooked(file_data, theFile)
        else:
            rig = create_Skeleton_w2(f, theFile)
    else:
        rig = create_Skeleton(theFile)
    final = readCSkeletonData(rig)
    return final
