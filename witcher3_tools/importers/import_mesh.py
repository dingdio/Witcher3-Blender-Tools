import logging
import hashlib
import os
import math
import time
import bpy
import bmesh
from bpy.types import Object
from typing import List, Tuple
from mathutils import Vector, Matrix
import numpy as np
import array
from ..CR2W.common_blender import repo_file, get_collision_for_mesh, get_collision_for_mesh_with_poses, win_safe_path
from ..importers.import_rig import rotate_and_connect_bones

from ..materials.cr2w import setup_w3_material_CR2W
from ..materials import reader as material_reader
from .. import (
    get_all_addon_prefs,
    get_mod_directory,
    get_modded_texture_path,
    get_texture_path,
    get_uncook_path,
)
from .. import file_helpers
from ..CR2W import w3_types
from ..CR2W import read_json_w3
from ..CR2W import CR2W_reader
from ..importers import data_types
from ..CR2W import dc_mesh
from ..CR2W.dc_mesh import MeshData
from ..CR2W.Types.BlenderMesh import CommonData
from ..CR2W.Types.SBufferInfos import SMeshInfos, EMeshVertexType, VertexSkinningEntry
from ..CR2W.dc_entity import CCollisionShapeConvex, CCollisionShapeTriMesh, CCollisionShapeBox, CCollisionShapeCapsule, CCollisionShapeSphere
from ..importers.import_nxs import createCol, createTri, createBox, createCapsule, createSphere, create_from_nxs
from .. import get_do_fix_tail, set_rig_rot90_enabled
from ..repo_paths import configured_w2_repo_roots, w2_source_repo_root_if_configured

log = logging.getLogger(__name__)

_MESH_PROFILE_ENABLED = True
_MESH_PROFILE_WARN_THRESHOLD = 0.25
_MESH_PROFILE_MATERIAL_WARN_THRESHOLD = 0.10
_MESH_SKELETON_CACHE = {}


def _log_mesh_profile_warning(message, *args):
    if not _MESH_PROFILE_ENABLED:
        return
    log.info("[mesh-import-profile] " + str(message), *args)

ZERO_WEIGHT_MASK_GROUP_NAME = "_w3_zero_weight_hidden"
ZERO_WEIGHT_MASK_MODIFIER_NAME = "W3 Zero Weight Mask"

TANGENT_SPACE_REDENGINE = 'REDENGINE'
TANGENT_SPACE_MIKKTSPACE = 'MIKKTSPACE'
TANGENT_SPACE_PRESERVE_IMPORTED = 'PRESERVE_IMPORTED'
TANGENT_SPACE_NO_MIRROR = 'NO_MIRROR'
TANGENT_SPACE_MODES = {
    TANGENT_SPACE_REDENGINE,
    TANGENT_SPACE_MIKKTSPACE,
    TANGENT_SPACE_PRESERVE_IMPORTED,
    TANGENT_SPACE_NO_MIRROR,
}

TANGENT_HANDEDNESS_AUTO = 'AUTO'
TANGENT_HANDEDNESS_AS_CALCULATED = 'AS_CALCULATED'
TANGENT_HANDEDNESS_FLIPPED = 'FLIPPED'
TANGENT_HANDEDNESS_MODES = {
    TANGENT_HANDEDNESS_AUTO,
    TANGENT_HANDEDNESS_AS_CALCULATED,
    TANGENT_HANDEDNESS_FLIPPED,
}

IMPORTED_NORMAL_ATTRIBUTE = "witcher_imported_normal"
IMPORTED_TANGENT_ATTRIBUTE = "witcher_imported_tangent"
IMPORTED_BINORMAL_ATTRIBUTE = "witcher_imported_binormal"
IMPORTED_BASIS_VALID_ATTRIBUTE = "witcher_imported_basis_valid"
IMPORTED_BASIS_REPAIRED_ATTRIBUTE = "witcher_imported_basis_repaired"
IMPORTED_BASIS_SIGNATURE_PROPERTY = "witcher_imported_basis_signature"
IMPORTED_BASIS_SOURCE_VERSION_PROPERTY = "witcher_imported_basis_source_version"
IMPORTED_BASIS_REPAIRED_COUNT_PROPERTY = "witcher_imported_basis_repaired_count"
MIKK_TANGENT_ATTRIBUTE = "_witcher_mikk_tangent"
MIKK_BINORMAL_ATTRIBUTE = "_witcher_mikk_binormal"
MIKK_NORMAL_ATTRIBUTE = "_witcher_mikk_normal"


def _mesh_has_skinned_chunks(CData):
    mesh_infos = getattr(CData, "meshInfos", None) or []
    return any(getattr(mesh_info, "vertexType", None) == EMeshVertexType.EMVT_SKINNED for mesh_info in mesh_infos)


def _derive_mesh_is_static(CData):
    mesh_infos = getattr(CData, "meshInfos", None) or []
    if mesh_infos:
        return not _mesh_has_skinned_chunks(CData)
    return bool(getattr(CData, "isStatic", False))


def _bone_matrix_to_rest_matrix(bone_matrix):
    fields = getattr(bone_matrix, "fields", bone_matrix)
    mat = Matrix()
    mat[0][0], mat[0][1], mat[0][2], mat[0][3] = fields[0], fields[4], fields[8], fields[12]
    mat[1][0], mat[1][1], mat[1][2], mat[1][3] = fields[1], fields[5], fields[9], fields[13]
    mat[2][0], mat[2][1], mat[2][2], mat[2][3] = fields[2], fields[6], fields[10], fields[14]
    mat[3][0], mat[3][1], mat[3][2], mat[3][3] = fields[3], fields[7], fields[11], fields[15]
    return mat.inverted()


def mesh_bone_data_to_skeleton(CData):
    if CData is None or not _mesh_has_skinned_chunks(CData):
        return None
    bone_data = getattr(CData, "boneData", None)
    joint_names = list(getattr(bone_data, "jointNames", []) or [])
    bone_matrices = list(getattr(bone_data, "boneMatrices", []) or [])
    if not joint_names or not bone_matrices:
        return None

    bones = []
    seen = set()
    for idx, name in enumerate(joint_names):
        name = str(name or "").strip()
        if not name or name in seen or idx >= len(bone_matrices):
            continue
        seen.add(name)
        mat = _bone_matrix_to_rest_matrix(bone_matrices[idx])
        pos = mat.to_translation()
        quat = mat.to_quaternion()
        bones.append(w3_types.W3Bone(
            len(bones),
            name,
            [float(pos.x), float(pos.y), float(pos.z), 1.0],
            -1,
            False,
            w3_types.Quaternion(float(quat.x), float(quat.y), float(quat.z), float(quat.w)),
            [1.0, 1.0, 1.0],
        ))
    return w3_types.CSkeleton(bones=bones) if bones else None


def _skeleton_bone_name(bone) -> str:
    if isinstance(bone, dict):
        return str(bone.get("name", "") or "").strip()
    return str(getattr(bone, "name", "") or "").strip()


def _skeleton_bone_head(bone):
    if isinstance(bone, dict):
        value = bone.get("co")
    else:
        value = getattr(bone, "co", None)
    if value is None or value is False:
        return None
    try:
        return Vector((float(value[0]), float(value[1]), float(value[2])))
    except Exception:
        return None


def _skeleton_is_dynamic_attachment_bind(bone_names) -> bool:
    names = [str(name or "").strip() for name in (bone_names or []) if str(name or "").strip()]
    if not names:
        return False
    anchor_names = {
        "Root",
        "head",
        "neck",
        "torso",
        "torso2",
        "torso3",
        "pelvis",
    }
    driven_names = [name for name in names if name not in anchor_names]
    if not driven_names:
        return False
    return all(name.startswith("dyng_") for name in driven_names)


def mesh_skeleton_matches_target_armature(skeleton_data, target_armature, *, tolerance=0.05) -> bool:
    if target_armature is None or getattr(target_armature, "type", None) != 'ARMATURE':
        return False
    bones = list(getattr(skeleton_data, "bones", []) or [])
    if not bones:
        return False
    target_bones = getattr(getattr(target_armature, "data", None), "bones", None)
    if target_bones is None:
        return False

    compared = 0
    missing = 0
    max_distance = 0.0
    distances = []
    compared_names = []
    for bone in bones:
        name = _skeleton_bone_name(bone)
        if not name:
            continue
        target_bone = target_bones.get(name)
        if target_bone is None:
            missing += 1
            continue
        source_head = _skeleton_bone_head(bone)
        if source_head is None:
            continue
        target_head = target_armature.matrix_world @ target_bone.head_local
        distance = (source_head - target_head).length
        distances.append(distance)
        compared_names.append(name)
        max_distance = max(max_distance, distance)
        compared += 1
    if compared <= 0 or missing:
        return False
    if max_distance <= tolerance:
        return True

    # Some character body meshes carry small asymmetric bind-pose offsets in
    # extremities while the shared skeleton core still matches tightly. Allow
    # those, but keep unrelated attachments rejected by requiring most bones to
    # be nearly identical.
    variant_tolerance = max(tolerance, 0.075)
    core_tolerance = min(tolerance, 0.01)
    close_count = sum(1 for distance in distances if distance <= core_tolerance)
    close_ratio = close_count / float(compared)
    if max_distance <= variant_tolerance and close_ratio >= 0.75:
        return True

    if _skeleton_is_dynamic_attachment_bind(compared_names):
        return True

    log.debug(
        "Mesh skeleton does not match target armature '%s' rest space: max=%.6f tolerance=%.6f close_ratio=%.2f",
        getattr(target_armature, "name", ""),
        max_distance,
        tolerance,
        close_ratio,
    )
    return False


def _target_armature_matches_mesh_bones(CData, target_armature, *, tolerance=0.05) -> bool:
    return mesh_skeleton_matches_target_armature(
        mesh_bone_data_to_skeleton(CData),
        target_armature,
        tolerance=tolerance,
    )


def read_mesh_skeleton_data(filename: str, *, keep_proxy_meshes=False, embedded_cmesh_chunk_index=None):
    filename = str(filename or "").strip()
    if not filename:
        return None
    try:
        stat = os.stat(win_safe_path(filename))
        cache_key = (
            os.path.normcase(os.path.normpath(filename)),
            int(stat.st_mtime_ns),
            int(stat.st_size),
            embedded_cmesh_chunk_index,
            bool(keep_proxy_meshes),
        )
    except Exception:
        cache_key = (
            os.path.normcase(os.path.normpath(filename)),
            embedded_cmesh_chunk_index,
            bool(keep_proxy_meshes),
        )
    if cache_key in _MESH_SKELETON_CACHE:
        return _MESH_SKELETON_CACHE[cache_key]
    CData, _bufferInfos, _mat_names, _materials, _meshName, _meshFile = dc_mesh.load_bin_mesh(
        filename,
        keep_lod_meshes=True,
        keep_proxy_meshes=keep_proxy_meshes,
        embedded_cmesh_chunk_index=embedded_cmesh_chunk_index,
    )
    skeleton_data = mesh_bone_data_to_skeleton(CData)
    _MESH_SKELETON_CACHE[cache_key] = skeleton_data
    if len(_MESH_SKELETON_CACHE) > 64:
        _MESH_SKELETON_CACHE.pop(next(iter(_MESH_SKELETON_CACHE.keys())), None)
    return skeleton_data


def mesh_file_matches_target_armature(filename: str, target_armature, *, tolerance=0.05,
                                      keep_proxy_meshes=False, embedded_cmesh_chunk_index=None) -> bool:
    skeleton_data = read_mesh_skeleton_data(
        filename,
        keep_proxy_meshes=keep_proxy_meshes,
        embedded_cmesh_chunk_index=embedded_cmesh_chunk_index,
    )
    return mesh_skeleton_matches_target_armature(
        skeleton_data,
        target_armature,
        tolerance=tolerance,
    )


def _mesh_object_setting(mesh_obj, key: str) -> str:
    settings = getattr(mesh_obj, "witcherui_MeshSettings", None)
    if settings is None:
        return ""
    try:
        return str(settings[key] or "").strip()
    except Exception:
        return ""


def _mesh_object_source_paths(mesh_obj) -> list[str]:
    paths = []
    for value in (
        _mesh_object_setting(mesh_obj, "item_repo_path"),
        _mesh_object_setting(mesh_obj, "source_mesh_path"),
    ):
        if value and value not in paths:
            paths.append(value)
    for key in ("witcher_path", "repo_path"):
        try:
            value = str(mesh_obj.get(key, "") or "").strip()
        except Exception:
            value = ""
        if value and value not in paths:
            paths.append(value)
    return paths


def mesh_object_target_rest_compatibility(mesh_obj, target_armature, *, tolerance=0.05):
    """Return True/False when mesh source data can be inspected, otherwise None."""
    for source_path in _mesh_object_source_paths(mesh_obj):
        candidates = [source_path]
        if not os.path.isabs(source_path):
            try:
                resolved = repo_file(source_path)
            except Exception:
                resolved = ""
            if resolved and resolved not in candidates:
                candidates.insert(0, resolved)
        for candidate in candidates:
            try:
                skeleton_data = read_mesh_skeleton_data(candidate)
                if skeleton_data is None:
                    continue
                return mesh_skeleton_matches_target_armature(
                    skeleton_data,
                    target_armature,
                    tolerance=tolerance,
                )
            except Exception:
                log.debug(
                    "Could not inspect mesh skeleton compatibility for '%s' from '%s'",
                    getattr(mesh_obj, "name", ""),
                    candidate,
                    exc_info=True,
                )
    return None


def _ensure_armature_binding(mesh_obj, armature_obj):
    if mesh_obj is None or armature_obj is None:
        return

    mesh_obj.parent = armature_obj

    existing_modifier = None
    for modifier in getattr(mesh_obj, "modifiers", []):
        if modifier.type == 'ARMATURE' and getattr(modifier, "object", None) == armature_obj:
            existing_modifier = modifier
            break

    if existing_modifier is None:
        existing_modifier = mesh_obj.modifiers.new(name="Armature", type='ARMATURE')

    existing_modifier.object = armature_obj
    existing_modifier.use_vertex_groups = True

def _warn_missing_physical_material(shape_type, mesh_name):
    log.warning(
        f"{shape_type} collision in '{mesh_name}' has no physical material. "
        "Assign one before export to avoid REDkit issues."
    )


def _collect_zero_weight_vertex_indices(mesh_obj, weight_epsilon: float = 1e-8) -> List[int]:
    mesh = getattr(mesh_obj, "data", None)
    if getattr(mesh_obj, "type", None) != 'MESH' or mesh is None:
        return []

    group_names = {group.index: group.name for group in mesh_obj.vertex_groups}
    zero_weight_indices = []
    for vertex in mesh.vertices:
        if not any(
            group.weight > weight_epsilon
            and group_names.get(group.group) != ZERO_WEIGHT_MASK_GROUP_NAME
            for group in vertex.groups
        ):
            zero_weight_indices.append(vertex.index)
    return zero_weight_indices


def _hide_zero_weight_faces(mesh_obj, weight_epsilon: float = 1e-8) -> Tuple[int, int]:
    mesh = getattr(mesh_obj, "data", None)
    if getattr(mesh_obj, "type", None) != 'MESH' or mesh is None:
        return (0, 0)

    zero_weight_indices = _collect_zero_weight_vertex_indices(mesh_obj, weight_epsilon)
    if not zero_weight_indices:
        return (0, 0)

    zero_weight_set = set(zero_weight_indices)
    hidden_face_count = 0

    for polygon in mesh.polygons:
        if any(vertex_index in zero_weight_set for vertex_index in polygon.vertices):
            hidden_face_count += 1

    existing_group = mesh_obj.vertex_groups.get(ZERO_WEIGHT_MASK_GROUP_NAME)
    if existing_group is not None:
        mesh_obj.vertex_groups.remove(existing_group)
    zero_weight_group = mesh_obj.vertex_groups.new(name=ZERO_WEIGHT_MASK_GROUP_NAME)
    zero_weight_group.add(zero_weight_indices, 1.0, 'REPLACE')

    existing_modifier = mesh_obj.modifiers.get(ZERO_WEIGHT_MASK_MODIFIER_NAME)
    if existing_modifier is not None:
        mesh_obj.modifiers.remove(existing_modifier)
    mask_modifier = mesh_obj.modifiers.new(name=ZERO_WEIGHT_MASK_MODIFIER_NAME, type='MASK')
    mask_modifier.vertex_group = zero_weight_group.name
    mask_modifier.invert_vertex_group = True
    mask_modifier.show_in_editmode = True

    mesh.update()
    return (len(zero_weight_indices), hidden_face_count)


def _mesh_data_debug_summary(mesh_data):
    """Return a compact diagnostic summary for a parsed submesh."""
    verts = getattr(mesh_data, "vertex3DCoords", None) or []
    faces = getattr(mesh_data, "faces", None) or []
    uv1 = getattr(mesh_data, "UV_vertex3DCoords", None) or []
    uv2 = getattr(mesh_data, "UV2_vertex3DCoords", None) or []
    normals = getattr(mesh_data, "normals", None) or []
    normals_all = getattr(mesh_data, "normalsAll", None) or []
    skinning = getattr(mesh_data, "skinningVerts", None) or []
    vcols = getattr(mesh_data, "vertexColor", None) or []

    vert_count = len(verts)
    face_count = len(faces)
    degenerate_faces = 0
    out_of_range_faces = 0
    min_face_index = None
    max_face_index = None

    for face in faces:
        try:
            if len(face) != 3 or len(set(face)) < 3:
                degenerate_faces += 1
            face_has_oor = False
            for vi in face:
                if min_face_index is None or vi < min_face_index:
                    min_face_index = vi
                if max_face_index is None or vi > max_face_index:
                    max_face_index = vi
                if vi < 0 or vi >= vert_count:
                    face_has_oor = True
            if face_has_oor:
                out_of_range_faces += 1
        except Exception:
            degenerate_faces += 1

    face_range = "n/a"
    if min_face_index is not None and max_face_index is not None:
        face_range = f"{min_face_index}..{max_face_index}"

    return (
        f"verts={vert_count} faces={face_count} face_idx_range={face_range} "
        f"degenerate_faces={degenerate_faces} oor_faces={out_of_range_faces} "
        f"uv1={len(uv1)} uv2={len(uv2)} normals={len(normals)} "
        f"normalsAll={len(normals_all)} skinningVerts={len(skinning)} vcols={len(vcols)}"
    )


def _sanitize_mesh_faces_for_import(mesh_data):
    verts = getattr(mesh_data, "vertex3DCoords", None) or []
    faces = getattr(mesh_data, "faces", None) or []
    vert_count = len(verts)
    if not faces:
        return 0

    try:
        arr = np.asarray(faces, dtype=np.int64)
    except (TypeError, ValueError, OverflowError):
        arr = None
    if arr is not None and arr.ndim == 2 and arr.shape[1] == 3:
        valid = (
            (arr[:, 0] != arr[:, 1])
            & (arr[:, 1] != arr[:, 2])
            & (arr[:, 0] != arr[:, 2])
            & (arr >= 0).all(axis=1)
            & (arr < vert_count).all(axis=1)
        )
        dropped = int(arr.shape[0] - int(valid.sum()))
        if dropped:
            mesh_data.faces = arr[valid].tolist()
        return dropped

    # ragged/odd face data: keep the tolerant per-face path
    valid_faces = []
    dropped = 0
    for face in faces:
        try:
            clean_face = [int(idx) for idx in face]
        except Exception:
            dropped += 1
            continue
        if len(clean_face) != 3 or len(set(clean_face)) < 3:
            dropped += 1
            continue
        if any(idx < 0 or idx >= vert_count for idx in clean_face):
            dropped += 1
            continue
        valid_faces.append(clean_face)

    if dropped:
        mesh_data.faces = valid_faces
    return dropped


def _mesh_vertices_are_importable(mesh_data):
    verts = getattr(mesh_data, "vertex3DCoords", None) or []
    if not verts:
        return True
    try:
        arr = np.asarray(verts, dtype=np.float64)
    except (TypeError, ValueError):
        arr = None
    if arr is not None and arr.ndim == 2:
        if arr.shape[1] < 3:
            return False
        return bool(np.isfinite(arr[:, :3]).all())

    for vert in verts:
        try:
            if len(vert) < 3:
                return False
            if not all(math.isfinite(float(vert[idx])) for idx in range(3)):
                return False
        except Exception:
            return False
    return True


def _normalize_vector3(value, fallback, min_length=1e-8):
    x = float(value[0])
    y = float(value[1])
    z = float(value[2])
    if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
        return fallback

    length = math.sqrt((x * x) + (y * y) + (z * z))
    if length < min_length:
        return fallback
    return (x / length, y / length, z / length)


def _uv_determinant_is_degenerate(delta_u1, delta_v1, delta_u2, delta_v2, determinant):
    values = (delta_u1, delta_v1, delta_u2, delta_v2, determinant)
    if not all(math.isfinite(float(value)) for value in values):
        return True
    edge_scale_squared = (
        ((delta_u1 * delta_u1) + (delta_v1 * delta_v1))
        * ((delta_u2 * delta_u2) + (delta_v2 * delta_v2))
    )
    if edge_scale_squared <= 0.0:
        return True
    return (determinant * determinant) <= (1.0e-12 * edge_scale_squared)


def _fallback_tangent_basis(normal):
    nx, ny, nz = normal
    if abs(nz) < 0.999:
        ax, ay, az = 0.0, 0.0, 1.0
    else:
        ax, ay, az = 1.0, 0.0, 0.0

    tx = ay * nz - az * ny
    ty = az * nx - ax * nz
    tz = ax * ny - ay * nx
    tangent = _normalize_vector3((tx, ty, tz), (1.0, 0.0, 0.0))

    bx = tangent[1] * nz - tangent[2] * ny
    by = tangent[2] * nx - tangent[0] * nz
    bz = tangent[0] * ny - tangent[1] * nx
    bitangent = _normalize_vector3((bx, by, bz), (0.0, 1.0, 0.0))
    return tangent, bitangent


def _normalize_tangent_space_mode(value):
    mode = str(value or TANGENT_SPACE_REDENGINE).upper()
    if mode not in TANGENT_SPACE_MODES:
        raise ValueError(f"Unknown tangent-space export mode: {value!r}")
    return mode


def _normalize_tangent_handedness_mode(value):
    mode = str(value or TANGENT_HANDEDNESS_AUTO).upper()
    if mode not in TANGENT_HANDEDNESS_MODES:
        raise ValueError(f"Unknown tangent handedness export mode: {value!r}")
    return mode


def resolve_tangent_handedness_mode(
    tangent_space_mode,
    tangent_handedness_mode=TANGENT_HANDEDNESS_AUTO,
    source_mesh=None,
):
    """Resolve Auto using the imported game's binormal convention."""
    tangent_space_mode = _normalize_tangent_space_mode(tangent_space_mode)
    tangent_handedness_mode = _normalize_tangent_handedness_mode(
        tangent_handedness_mode)

    if tangent_space_mode not in (
        TANGENT_SPACE_REDENGINE,
        TANGENT_SPACE_MIKKTSPACE,
    ):
        return TANGENT_HANDEDNESS_AS_CALCULATED
    if tangent_handedness_mode != TANGENT_HANDEDNESS_AUTO:
        return tangent_handedness_mode

    try:
        source_version = int(source_mesh.get(
            IMPORTED_BASIS_SOURCE_VERSION_PROPERTY, 0) or 0)
    except (AttributeError, TypeError, ValueError):
        source_version = 0
    if source_version <= 0:
        return TANGENT_HANDEDNESS_AS_CALCULATED

    source_is_witcher_2 = source_version <= 115
    if tangent_space_mode == TANGENT_SPACE_REDENGINE:
        return (
            TANGENT_HANDEDNESS_FLIPPED
            if source_is_witcher_2
            else TANGENT_HANDEDNESS_AS_CALCULATED
        )
    return (
        TANGENT_HANDEDNESS_AS_CALCULATED
        if source_is_witcher_2
        else TANGENT_HANDEDNESS_FLIPPED
    )


def _get_primary_uv_layer(mesh):
    uv_layers = mesh.uv_layers
    uv_layer = uv_layers.get("DiffuseUV") if len(uv_layers) > 0 else None
    if uv_layer is None and len(uv_layers) > 0:
        uv_layer = uv_layers[0]
    return uv_layer


def _replace_mesh_attribute(mesh, name, data_type, domain):
    attributes = getattr(mesh, "attributes", None)
    if attributes is None:
        return None
    attribute = attributes.get(name)
    if attribute is not None and (
        attribute.data_type != data_type or attribute.domain != domain
    ):
        attributes.remove(attribute)
        attribute = None
    if attribute is None:
        attribute = attributes.new(name=name, type=data_type, domain=domain)
    return attribute


def _write_vector_mesh_attribute(mesh, name, values, domain):
    values = np.asarray(values, dtype=np.float32)
    expected_count = len(mesh.vertices) if domain == 'POINT' else len(mesh.loops)
    if values.shape != (expected_count, 3) or not np.isfinite(values).all():
        return False
    attribute = _replace_mesh_attribute(mesh, name, 'FLOAT_VECTOR', domain)
    if attribute is None or len(attribute.data) != expected_count:
        return False
    attribute.data.foreach_set("vector", values.ravel())
    return True


def _read_vector_mesh_attribute(mesh, name, domain):
    attributes = getattr(mesh, "attributes", None)
    attribute = attributes.get(name) if attributes is not None else None
    expected_count = len(mesh.vertices) if domain == 'POINT' else len(mesh.loops)
    if (
        attribute is None
        or attribute.data_type != 'FLOAT_VECTOR'
        or attribute.domain != domain
        or len(attribute.data) != expected_count
    ):
        return None
    values = np.empty(expected_count * 3, dtype=np.float32)
    attribute.data.foreach_get("vector", values)
    values = values.reshape((-1, 3))
    return values if np.isfinite(values).all() else None


def _store_imported_tangent_basis(mesh, mesh_data, source_version=0):
    vert_count = len(mesh.vertices)
    normals = np.asarray(getattr(mesh_data, "normals", []) or [], dtype=np.float32)
    if normals.shape != (vert_count, 3):
        normals_all = getattr(mesh_data, "normalsAll", []) or []
        if len(normals_all) == vert_count * 3:
            normals = np.asarray(normals_all, dtype=np.float32).reshape((-1, 3))

    tangents = np.asarray(getattr(mesh_data, "tangent_vector", []) or [], dtype=np.float32)
    extra_vectors = getattr(mesh_data, "extra_vectors", []) or []
    if len(extra_vectors) == vert_count:
        binormals = np.asarray([value[:3] for value in extra_vectors], dtype=np.float32)
    else:
        binormals = np.empty((0, 3), dtype=np.float32)

    if not (
        normals.shape == (vert_count, 3)
        and tangents.shape == (vert_count, 3)
        and binormals.shape == (vert_count, 3)
    ):
        return False

    normals = normals.copy()
    tangents = tangents.copy()
    binormals = binormals.copy()
    repaired = ~(
        np.isfinite(normals).all(axis=1)
        & np.isfinite(tangents).all(axis=1)
        & np.isfinite(binormals).all(axis=1)
    )
    if np.any(repaired):
        solved_tangents = None
        solved_binormals = None
        if (
            len(getattr(mesh_data, "vertex3DCoords", []) or []) == vert_count
            and len(getattr(mesh_data, "UV_vertex3DCoords", []) or []) == vert_count
        ):
            try:
                solved_tangents, solved_binormals = _solve_meshdata_tangent_basis(
                    mesh_data, mirror_correction=True)
                solved_tangents = np.asarray(solved_tangents, dtype=np.float64)
                solved_binormals = np.asarray(solved_binormals, dtype=np.float64)
                if (
                    solved_tangents.shape != (vert_count, 3)
                    or solved_binormals.shape != (vert_count, 3)
                ):
                    solved_tangents = None
                    solved_binormals = None
            except (IndexError, TypeError, ValueError):
                solved_tangents = None
                solved_binormals = None

        preserve_w2_handedness = 0 < int(source_version or 0) <= 115
        for vert_idx in np.flatnonzero(repaired):
            fallback_normal = _normalize_vector3(
                mesh.vertices[int(vert_idx)].normal,
                (0.0, 0.0, 1.0),
            )
            source_normal_is_finite = bool(np.isfinite(normals[vert_idx]).all())
            basis_normal = _normalize_vector3(normals[vert_idx], fallback_normal)
            stored_normal = (
                tuple(float(value) for value in normals[vert_idx])
                if source_normal_is_finite
                else basis_normal
            )
            fallback_tangent, fallback_binormal = _fallback_tangent_basis(basis_normal)

            tangent = None
            binormal = None
            if solved_tangents is not None and solved_binormals is not None:
                tangent = _normalize_vector3(solved_tangents[vert_idx], None)
                binormal = _normalize_vector3(solved_binormals[vert_idx], None)
            if tangent is None or binormal is None:
                tangent, binormal = fallback_tangent, fallback_binormal

            tangent_dot_normal = sum(tangent[i] * basis_normal[i] for i in range(3))
            tangent = _normalize_vector3((
                tangent[0] - basis_normal[0] * tangent_dot_normal,
                tangent[1] - basis_normal[1] * tangent_dot_normal,
                tangent[2] - basis_normal[2] * tangent_dot_normal,
            ), fallback_tangent)
            cross_tn = (
                tangent[1] * basis_normal[2] - tangent[2] * basis_normal[1],
                tangent[2] * basis_normal[0] - tangent[0] * basis_normal[2],
                tangent[0] * basis_normal[1] - tangent[1] * basis_normal[0],
            )
            solver_sign = -1.0 if sum(
                cross_tn[i] * binormal[i] for i in range(3)) < 0.0 else 1.0
            binormal = _normalize_vector3(
                tuple(solver_sign * value for value in cross_tn),
                fallback_binormal,
            )
            if preserve_w2_handedness:
                # W2 uses the opposite binormal convention.
                binormal = (-binormal[0], -binormal[1], -binormal[2])

            normals[vert_idx] = stored_normal
            tangents[vert_idx] = tangent
            binormals[vert_idx] = binormal

        log.warning(
            "Repaired %d non-finite imported tangent-basis vertices on '%s'; "
            "all other source basis rows remain unchanged.",
            int(np.count_nonzero(repaired)),
            mesh.name,
        )

    if not (
        np.isfinite(normals).all()
        and np.isfinite(tangents).all()
        and np.isfinite(binormals).all()
    ):
        return False

    if not all((
        _write_vector_mesh_attribute(mesh, IMPORTED_NORMAL_ATTRIBUTE, normals, 'POINT'),
        _write_vector_mesh_attribute(mesh, IMPORTED_TANGENT_ATTRIBUTE, tangents, 'POINT'),
        _write_vector_mesh_attribute(mesh, IMPORTED_BINORMAL_ATTRIBUTE, binormals, 'POINT'),
    )):
        return False

    valid_attribute = _replace_mesh_attribute(
        mesh, IMPORTED_BASIS_VALID_ATTRIBUTE, 'INT', 'POINT')
    repaired_attribute = _replace_mesh_attribute(
        mesh, IMPORTED_BASIS_REPAIRED_ATTRIBUTE, 'INT', 'POINT')
    if (
        valid_attribute is None
        or repaired_attribute is None
        or len(valid_attribute.data) != vert_count
        or len(repaired_attribute.data) != vert_count
    ):
        return False
    valid_attribute.data.foreach_set("value", np.ones(vert_count, dtype=np.int32))
    repaired_attribute.data.foreach_set("value", repaired.astype(np.int32))
    mesh[IMPORTED_BASIS_SOURCE_VERSION_PROPERTY] = int(source_version or 0)
    mesh[IMPORTED_BASIS_REPAIRED_COUNT_PROPERTY] = int(np.count_nonzero(repaired))
    return True


def _imported_basis_attributes_status(mesh):
    if mesh is None:
        return False, "mesh data is unavailable"
    for name in (
        IMPORTED_NORMAL_ATTRIBUTE,
        IMPORTED_TANGENT_ATTRIBUTE,
        IMPORTED_BINORMAL_ATTRIBUTE,
    ):
        if _read_vector_mesh_attribute(mesh, name, 'POINT') is None:
            return False, f"missing imported basis attribute '{name}'"

    attributes = getattr(mesh, "attributes", None)
    valid_attribute = attributes.get(IMPORTED_BASIS_VALID_ATTRIBUTE) if attributes is not None else None
    if (
        valid_attribute is None
        or valid_attribute.data_type != 'INT'
        or valid_attribute.domain != 'POINT'
        or len(valid_attribute.data) != len(mesh.vertices)
    ):
        return False, "missing imported-basis validity data"
    validity = np.empty(len(mesh.vertices), dtype=np.int32)
    valid_attribute.data.foreach_get("value", validity)
    if not np.all(validity == 1):
        return False, "mesh contains vertices without an imported basis"
    return True, ""


def _mesh_imported_basis_signature(mesh):
    mesh.calc_loop_triangles()
    digest = hashlib.sha256()

    def _update_quantized(values):
        values = np.asarray(values, dtype=np.float64)
        digest.update(np.rint(values * 1.0e6).astype('<i8').tobytes())

    digest.update(np.asarray(
        [len(mesh.vertices), len(mesh.loops), len(mesh.polygons)], dtype='<i8').tobytes())

    positions = np.empty(len(mesh.vertices) * 3, dtype='<f4')
    mesh.vertices.foreach_get("co", positions)
    _update_quantized(positions)

    loop_vertices = np.empty(len(mesh.loops), dtype='<i4')
    mesh.loops.foreach_get("vertex_index", loop_vertices)
    digest.update(loop_vertices.tobytes())

    polygon_starts = np.empty(len(mesh.polygons), dtype='<i4')
    polygon_totals = np.empty(len(mesh.polygons), dtype='<i4')
    mesh.polygons.foreach_get("loop_start", polygon_starts)
    mesh.polygons.foreach_get("loop_total", polygon_totals)
    digest.update(polygon_starts.tobytes())
    digest.update(polygon_totals.tobytes())

    loop_normals = np.empty(len(mesh.loops) * 3, dtype='<f4')
    mesh.loops.foreach_get("normal", loop_normals)
    _update_quantized(loop_normals)

    uv_layer = _get_primary_uv_layer(mesh)
    if uv_layer is None:
        digest.update(b"NO_UV0")
    else:
        uvs = np.empty(len(mesh.loops) * 2, dtype='<f4')
        uv_layer.data.foreach_get("uv", uvs)
        _update_quantized(uvs)

    for name in (
        IMPORTED_NORMAL_ATTRIBUTE,
        IMPORTED_TANGENT_ATTRIBUTE,
        IMPORTED_BINORMAL_ATTRIBUTE,
    ):
        values = _read_vector_mesh_attribute(mesh, name, 'POINT')
        if values is None:
            raise ValueError(f"Missing imported basis attribute '{name}'.")
        _update_quantized(values)
    return digest.hexdigest()


def _mark_imported_tangent_basis(mesh):
    valid, _reason = _imported_basis_attributes_status(mesh)
    if not valid:
        if IMPORTED_BASIS_SIGNATURE_PROPERTY in mesh:
            del mesh[IMPORTED_BASIS_SIGNATURE_PROPERTY]
        return False
    repaired_count = 0
    repaired_attribute = mesh.attributes.get(IMPORTED_BASIS_REPAIRED_ATTRIBUTE)
    if (
        repaired_attribute is not None
        and repaired_attribute.data_type == 'INT'
        and repaired_attribute.domain == 'POINT'
        and len(repaired_attribute.data) == len(mesh.vertices)
    ):
        repaired = np.empty(len(mesh.vertices), dtype=np.int32)
        repaired_attribute.data.foreach_get("value", repaired)
        repaired_count = int(np.count_nonzero(repaired))
    mesh[IMPORTED_BASIS_REPAIRED_COUNT_PROPERTY] = repaired_count
    mesh[IMPORTED_BASIS_SIGNATURE_PROPERTY] = _mesh_imported_basis_signature(mesh)
    return True


def imported_tangent_basis_status(mesh):
    valid, reason = _imported_basis_attributes_status(mesh)
    if not valid:
        return False, reason
    stored_signature = str(mesh.get(IMPORTED_BASIS_SIGNATURE_PROPERTY, "") or "")
    if not stored_signature:
        return False, "mesh was not imported with tangent-basis preservation data; re-import it"
    try:
        current_signature = _mesh_imported_basis_signature(mesh)
    except (RuntimeError, ValueError) as exc:
        return False, str(exc)
    if current_signature != stored_signature:
        return False, "positions, topology, UVs, normals, or imported basis changed after import"
    return True, ""


def _bake_mikktspace_loop_basis(mesh):
    """Cache the full-LOD MikkTSpace basis in corner attributes."""
    uv_layer = _get_primary_uv_layer(mesh)
    if uv_layer is None:
        raise ValueError("MikkTSpace export requires a DiffuseUV/first UV layer.")

    mesh.calc_loop_triangles()
    positions = np.empty(len(mesh.vertices) * 3, dtype=np.float64)
    mesh.vertices.foreach_get("co", positions)
    if not np.isfinite(positions).all():
        raise ValueError("MikkTSpace export cannot process non-finite vertex positions.")

    source_normals = np.empty((len(mesh.loops), 3), dtype=np.float32)
    tangents = np.empty((len(mesh.loops), 3), dtype=np.float32)
    binormals = np.empty((len(mesh.loops), 3), dtype=np.float32)
    for loop_idx, loop in enumerate(mesh.loops):
        normal = _normalize_vector3(loop.normal, (0.0, 0.0, 1.0))
        tangent, binormal = _fallback_tangent_basis(normal)
        source_normals[loop_idx] = normal
        tangents[loop_idx] = tangent
        binormals[loop_idx] = binormal

    invalid_polygons = set()
    for loop_tri in mesh.loop_triangles:
        loop_indices = tuple(loop_tri.loops)
        verts = [mesh.vertices[mesh.loops[index].vertex_index].co for index in loop_indices]
        uvs = [uv_layer.data[index].uv for index in loop_indices]
        edge1 = verts[1] - verts[0]
        edge2 = verts[2] - verts[0]
        determinant = (
            (float(uvs[1][0]) - float(uvs[0][0]))
            * (float(uvs[2][1]) - float(uvs[0][1]))
            - (float(uvs[2][0]) - float(uvs[0][0]))
            * (float(uvs[1][1]) - float(uvs[0][1]))
        )
        delta_u1 = float(uvs[1][0]) - float(uvs[0][0])
        delta_v1 = float(uvs[1][1]) - float(uvs[0][1])
        delta_u2 = float(uvs[2][0]) - float(uvs[0][0])
        delta_v2 = float(uvs[2][1]) - float(uvs[0][1])
        geometry_scale_squared = edge1.length_squared * edge2.length_squared
        if (
            geometry_scale_squared <= 0.0
            or edge1.cross(edge2).length_squared
            <= (1.0e-12 * geometry_scale_squared)
            or _uv_determinant_is_degenerate(
                delta_u1, delta_v1, delta_u2, delta_v2, determinant)
        ):
            invalid_polygons.add(int(loop_tri.polygon_index))

    invalid_loops = {
        loop_idx
        for polygon_idx in invalid_polygons
        for loop_idx in mesh.polygons[polygon_idx].loop_indices
    }

    calculation_mesh = mesh
    calculation_mesh_is_copy = False
    calculated = False
    try:
        source_loop_indices = np.arange(len(mesh.loops), dtype=np.int32)
        if invalid_polygons:
            # Exclude invalid triangles from Blender's Mikk calculation.
            calculation_mesh = mesh.copy()
            calculation_mesh_is_copy = True
            source_loop_attribute = _replace_mesh_attribute(
                calculation_mesh,
                "_witcher_mikk_source_loop",
                'INT',
                'CORNER',
            )
            source_loop_attribute.data.foreach_set(
                "value", np.arange(len(calculation_mesh.loops), dtype=np.int32))

            bm = bmesh.new()
            try:
                bm.from_mesh(calculation_mesh)
                bm.faces.ensure_lookup_table()
                faces_to_delete = [
                    face for face in bm.faces
                    if face.index in invalid_polygons
                ]
                if faces_to_delete:
                    bmesh.ops.delete(bm, geom=faces_to_delete, context='FACES')
                bm.to_mesh(calculation_mesh)
            finally:
                bm.free()
            calculation_mesh.update()

            if len(calculation_mesh.loops):
                source_loop_attribute = calculation_mesh.attributes.get(
                    "_witcher_mikk_source_loop")
                if (
                    source_loop_attribute is None
                    or source_loop_attribute.data_type != 'INT'
                    or source_loop_attribute.domain != 'CORNER'
                    or len(source_loop_attribute.data) != len(calculation_mesh.loops)
                ):
                    raise ValueError("Could not map sanitized MikkTSpace corners for export.")
                source_loop_indices = np.empty(len(calculation_mesh.loops), dtype=np.int32)
                source_loop_attribute.data.foreach_get("value", source_loop_indices)
                calculation_mesh.normals_split_custom_set(
                    [source_normals[index] for index in source_loop_indices])
            else:
                source_loop_indices = np.empty(0, dtype=np.int32)

        if len(calculation_mesh.polygons):
            calculation_uv_layer = _get_primary_uv_layer(calculation_mesh)
            calculation_mesh.calc_tangents(uvmap=calculation_uv_layer.name)
            calculated = True

            candidate_basis = {}
            for calculation_loop_idx, source_loop_idx in enumerate(source_loop_indices):
                source_loop_idx = int(source_loop_idx)
                if source_loop_idx in invalid_loops:
                    continue
                loop = calculation_mesh.loops[calculation_loop_idx]
                normal = tuple(float(value) for value in source_normals[source_loop_idx])
                tangent = _normalize_vector3(loop.tangent, None)
                sign = float(loop.bitangent_sign)
                if tangent is None or not math.isfinite(sign) or abs(sign) < 0.5:
                    continue

                tangent_dot_normal = sum(tangent[i] * normal[i] for i in range(3))
                tangent = _normalize_vector3((
                    tangent[0] - normal[0] * tangent_dot_normal,
                    tangent[1] - normal[1] * tangent_dot_normal,
                    tangent[2] - normal[2] * tangent_dot_normal,
                ), None)
                if tangent is None:
                    continue

                blender_sign = -1.0 if sign < 0.0 else 1.0
                cross_nt = (
                    normal[1] * tangent[2] - normal[2] * tangent[1],
                    normal[2] * tangent[0] - normal[0] * tangent[2],
                    normal[0] * tangent[1] - normal[1] * tangent[0],
                )
                # Match the binormal sign used by Blender FBX and REDkit.
                binormal = _normalize_vector3(
                    tuple(blender_sign * value for value in cross_nt), None)
                if binormal is None:
                    continue
                candidate_basis[source_loop_idx] = (tangent, binormal)

            # Commit whole polygons to avoid mixed Mikk/fallback handedness.
            for polygon in mesh.polygons:
                polygon_loops = tuple(polygon.loop_indices)
                if all(loop_idx in candidate_basis for loop_idx in polygon_loops):
                    for source_loop_idx in polygon_loops:
                        tangent, binormal = candidate_basis[source_loop_idx]
                        tangents[source_loop_idx] = tangent
                        binormals[source_loop_idx] = binormal
                else:
                    invalid_loops.update(polygon_loops)

        if not (
            _write_vector_mesh_attribute(mesh, MIKK_NORMAL_ATTRIBUTE, source_normals, 'CORNER')
            and _write_vector_mesh_attribute(mesh, MIKK_TANGENT_ATTRIBUTE, tangents, 'CORNER')
            and _write_vector_mesh_attribute(mesh, MIKK_BINORMAL_ATTRIBUTE, binormals, 'CORNER')
        ):
            raise ValueError("Could not cache MikkTSpace corner data for export.")
    except RuntimeError as exc:
        raise ValueError(
            f"Blender could not calculate MikkTSpace from UV layer '{uv_layer.name}': {exc}") from exc
    finally:
        if calculated:
            calculation_mesh.free_tangents()
        if calculation_mesh_is_copy:
            bpy.data.meshes.remove(calculation_mesh)


def _solve_meshdata_tangent_basis(mesh_data: MeshData, mirror_correction=True):
    vert_count = len(mesh_data.vertex3DCoords)
    if vert_count == 0:
        return [], []

    positions = np.asarray(mesh_data.vertex3DCoords, dtype=np.float64)
    normals = np.asarray(mesh_data.normals, dtype=np.float64)
    if normals.shape != (vert_count, 3):
        normals = np.zeros((vert_count, 3), dtype=np.float64)
        if len(mesh_data.normalsAll) == vert_count * 3:
            normals[:] = np.asarray(mesh_data.normalsAll, dtype=np.float64).reshape((-1, 3))

    uvs = np.asarray(mesh_data.UV_vertex3DCoords, dtype=np.float64)
    if uvs.shape != (vert_count, 2):
        uvs = np.zeros((vert_count, 2), dtype=np.float64)
        uvs[:, 1] = 1.0

    uv_handedness = np.zeros(vert_count, dtype=np.int8)
    uv_handedness_conflict = np.zeros(vert_count, dtype=np.bool_)
    group_by_pos_uv = {}
    vertex_groups = np.empty(vert_count, dtype=np.int64)
    for vert_idx in range(vert_count):
        key = (
            float(positions[vert_idx][0]),
            float(positions[vert_idx][1]),
            float(positions[vert_idx][2]),
            float(uvs[vert_idx][0]),
            float(uvs[vert_idx][1]),
        )
        group_idx = group_by_pos_uv.get(key)
        if group_idx is None:
            group_idx = len(group_by_pos_uv)
            group_by_pos_uv[key] = group_idx
        vertex_groups[vert_idx] = group_idx

    # Preserve legacy grouping and unorthogonalized tangent accumulation.
    tangent_accum = np.zeros((len(group_by_pos_uv), 3), dtype=np.float64)
    degenerate_uv_faces = 0
    for face in mesh_data.faces:
        if len(face) != 3:
            continue
        i1, i2, i3 = (int(face[0]), int(face[1]), int(face[2]))
        if (
            i1 < 0 or i2 < 0 or i3 < 0 or
            i1 >= vert_count or i2 >= vert_count or i3 >= vert_count
        ):
            continue

        edge1 = positions[i2] - positions[i1]
        edge2 = positions[i3] - positions[i1]
        delta_u1 = uvs[i2][0] - uvs[i1][0]
        delta_u2 = uvs[i3][0] - uvs[i1][0]
        delta_v1 = uvs[i2][1] - uvs[i1][1]
        delta_v2 = uvs[i3][1] - uvs[i1][1]
        denom = (delta_u1 * delta_v2) - (delta_u2 * delta_v1)
        if mirror_correction:
            uv_is_degenerate = _uv_determinant_is_degenerate(
                delta_u1, delta_v1, delta_u2, delta_v2, denom)
        else:
            # Preserve the legacy debug path.
            uv_is_degenerate = (
                not math.isfinite(float(denom))
                or abs(float(denom)) <= 1e-7
            )
        if uv_is_degenerate:
            degenerate_uv_faces += 1
            continue

        sdir = ((delta_v2 * edge1) - (delta_v1 * edge2)) / float(denom)
        if not np.all(np.isfinite(sdir)):
            degenerate_uv_faces += 1
            continue

        face_handedness = -1 if denom < 0.0 else 1
        for vert_idx in (i1, i2, i3):
            tangent_accum[vertex_groups[vert_idx]] += sdir
            if uv_handedness_conflict[vert_idx]:
                continue
            current_handedness = int(uv_handedness[vert_idx])
            if current_handedness == 0:
                uv_handedness[vert_idx] = face_handedness
            elif current_handedness != face_handedness:
                # Keep the legacy binormal for unsplit orientation conflicts.
                uv_handedness[vert_idx] = 0
                uv_handedness_conflict[vert_idx] = True

    vertex_is_mirrored = uv_handedness < 0
    tangents = []
    bitangents = []
    fallback_count = 0
    for vert_idx in range(vert_count):
        normal = _normalize_vector3(normals[vert_idx], (0.0, 0.0, 1.0))
        fallback_tangent, fallback_bitangent = _fallback_tangent_basis(normal)

        tangent = _normalize_vector3(tangent_accum[vertex_groups[vert_idx]], None)
        if tangent is None:
            tangent, bitangent = fallback_tangent, fallback_bitangent
            fallback_count += 1
        else:
            bitangent = _normalize_vector3(
                (
                    tangent[1] * normal[2] - tangent[2] * normal[1],
                    tangent[2] * normal[0] - tangent[0] * normal[2],
                    tangent[0] * normal[1] - tangent[1] * normal[0],
                ),
                fallback_bitangent,
            )

        # Mirrored charts need the opposite binormal for REDengine tangent W=+1.
        if mirror_correction and vertex_is_mirrored[vert_idx]:
            bitangent = (-bitangent[0], -bitangent[1], -bitangent[2])

        tangents.append([tangent[0], tangent[1], tangent[2]])
        bitangents.append([bitangent[0], bitangent[1], bitangent[2]])

    if degenerate_uv_faces or fallback_count:
        log.debug(
            "Solved tangent basis for %d verts with %d degenerate UV faces and %d fallback vertices.",
            vert_count,
            degenerate_uv_faces,
            fallback_count,
        )

    return tangents, bitangents

def blen_read_geom_array_gen_direct_looptovert(mesh, fbx_data, stride):
    fbx_data_len = len(fbx_data) # stride
    loops = mesh.loops
    for p in mesh.polygons:
        for lidx in p.loop_indices:
            vidx = loops[lidx].vertex_index
            if vidx < fbx_data_len:
                yield lidx, vidx * stride

def import_mesh(filename:str,
                do_import_mats:bool = True,
                do_import_armature:bool = True,
                keep_lod_meshes:bool = False,
                do_merge_normals:bool = False,
                rotate_180:bool = False,
                keep_empty_lods:bool = False,
                keep_proxy_meshes:bool = False,
                do_import_collision:bool = False,
                hide_zero_weight_faces:bool = True,
                build_material_nodes:bool = True,
                target_armature=None,
                embedded_cmesh_chunk_index=None) -> w3_types.CSkeletalAnimationSet:
    mesh_started = time.perf_counter()
    parse_seconds = 0.0
    prepare_seconds = 0.0
    collision_seconds = 0.0
    dirpath, file = os.path.split(filename)
    basename, ext = os.path.splitext(file)
    if ext.lower() in ('.w2mesh', '.w2ent') or embedded_cmesh_chunk_index is not None:
        with open(win_safe_path(filename), "rb") as _mesh_file:
            try:
                parse_started = time.perf_counter()
                (CData, bufferInfos, the_material_names, the_materials, meshName, meshFile) = dc_mesh.load_bin_mesh(
                    filename,
                    keep_lod_meshes,
                    keep_proxy_meshes,
                    embedded_cmesh_chunk_index=embedded_cmesh_chunk_index,
                )
                parse_seconds = time.perf_counter() - parse_started
                mesh_chunks = getattr(CData, "meshDataAllMeshes", None) or []
                material_names = the_material_names or []
                material_handles = getattr(the_materials, "Handles", None) or []
                log.info(
                    "Mesh import start '%s': submeshes=%d material_names=%d material_handles=%d import_mats=%s import_armature=%s keep_lods=%s keep_proxy=%s",
                    meshName,
                    len(mesh_chunks),
                    len(material_names),
                    len(material_handles),
                    do_import_mats,
                    do_import_armature,
                    keep_lod_meshes,
                    keep_proxy_meshes,
                )
                if material_names:
                    log.debug("Mesh '%s' material names: %s", meshName, material_names)
                prepare_started = time.perf_counter()
                (final_bl_meshes, armatures) = prepare_mesh_import(CData, bufferInfos, the_material_names, the_materials, meshName, meshFile,
                    do_import_mats,
                    do_import_armature,
                    keep_lod_meshes,
                    do_merge_normals,
                    rotate_180,
                    keep_empty_lods,
                    keep_proxy_meshes,
                    hide_zero_weight_faces,
                    build_material_nodes,
                    target_armature)
                prepare_seconds = time.perf_counter() - prepare_started
                
                if rotate_180:
                    if armatures:
                            for armature_obj in armatures:
                                    armature_obj.rotation_euler[2] = np.pi
                                    #bpy.ops.object.transform_apply(location=False, rotation=True, scale=False)
                    elif final_bl_meshes:
                            for joined_obj in final_bl_meshes:
                                #joined_obj.select_set(True)
                                joined_obj.rotation_euler[2] = np.pi
                                #bpy.ops.object.transform_apply(location=False, rotation=True, scale=False)
                
                ###################
                ##### COLLISION ###
                ###################
                # class CCollisionShapeConvex():
                #     def __init__(self, physicalMaterialName, vertices, polygons):
                #         self.physicalMaterialName = physicalMaterialName
                        
                #         self.vertices = []
                #         for verts in vertices.More:
                #                 self.vertices.append(
                #                     [verts.MoreProps[0].Value,
                #                     verts.MoreProps[1].Value,
                #                     verts.MoreProps[2].Value,
                #                     verts.MoreProps[3].Value]
                #                 )
                #         self.polygons = polygons
                #         print(self.vertices)
                #         print(self.polygons)

                from ..CR2W.CR2W_types import W_CLASS
                collision_started = time.perf_counter()
                found_embedded_collision = False

                ###################
                ##### COLLISION ###
                ###################
                # Uncooked mesh: import embedded CCollisionMesh shapes directly.
                # Cooked mesh: no CCollisionMesh chunk present; use collision cache instead.
                if do_import_collision:
                    for CHUNK in meshFile.CHUNKS.CHUNKS:
                        CHUNK:W_CLASS
                        if CHUNK.name == 'CCollisionMesh':
                            found_embedded_collision = True
                            log.info('Found embedded Collision Mesh (uncooked)')
                            shapes = CHUNK.GetVariableByName('shapes')
                            if hasattr(shapes, 'value'): ##TODO HANDLE WITCHER 2 COLLISION
                                for shape_chunk_id in shapes.value:
                                    shape_ = meshFile.CHUNKS.CHUNKS[shape_chunk_id-1]
                                    log.info(shape_.Type+' found')
                                    if shape_.Type == 'CCollisionShapeConvex':
                                        col_ = CCollisionShapeConvex(shape_)
                                        try:
                                            createCol(col_, meshName)
                                        except Exception as e:
                                            log.warning("Skipping convex collision for '%s': %s", meshName, e)
                                            continue
                                        if not getattr(col_, 'physicalMaterialName', None):
                                            _warn_missing_physical_material(shape_.Type, meshName)
                                        log.debug("physicalMaterialName: %s", col_.physicalMaterialName)
                                        log.debug("polygons: %s", col_.polygons)
                                        log.debug("vertices: %s", col_.vertices)
                                    elif shape_.Type == 'CCollisionShapeTriMesh':
                                        tri_ = CCollisionShapeTriMesh(shape_)
                                        try:
                                            createTri(tri_, meshName)
                                        except Exception as e:
                                            log.warning("Skipping tri collision for '%s': %s", meshName, e)
                                            continue
                                        if not getattr(tri_, 'physicalMaterialNames', None):
                                            _warn_missing_physical_material(shape_.Type, meshName)
                                        log.debug("physicalMaterialNames: %s", tri_.physicalMaterialNames)
                                        log.debug("vertices: %s", tri_.vertices)
                                        log.debug("triangles: %s", tri_.triangles)
                                        log.debug("physicalMaterialIndexes: %s", tri_.physicalMaterialIndexes)
                                    elif shape_.Type == 'CCollisionShapeBox':
                                        box_ = CCollisionShapeBox(shape_)
                                        createBox(box_, meshName)
                                        if not getattr(box_, 'physicalMaterialName', None):
                                            _warn_missing_physical_material(shape_.Type, meshName)
                                        log.debug("physicalMaterialName: %s", getattr(box_, 'physicalMaterialName', 'NO_MATERIAL'))
                                    elif shape_.Type == 'CCollisionShapeSphere':
                                        sphere_ = CCollisionShapeSphere(shape_)
                                        createSphere(sphere_, meshName)
                                        if not getattr(sphere_, 'physicalMaterialName', None):
                                            _warn_missing_physical_material(shape_.Type, meshName)
                                        log.debug("physicalMaterialName: %s", getattr(sphere_, 'physicalMaterialName', 'NO_MATERIAL'))
                                    elif shape_.Type == 'CCollisionShapeCapsule':
                                        capsule_ = CCollisionShapeCapsule(shape_)
                                        createCapsule(capsule_, meshName)
                                        if not getattr(capsule_, 'physicalMaterialName', None):
                                            _warn_missing_physical_material(shape_.Type, meshName)
                                        log.debug("physicalMaterialName: %s", getattr(capsule_, 'physicalMaterialName', 'NO_MATERIAL'))
                            break

                ###################
                ### CACHE COLLISION
                ###################
                # Cooked mesh: no embedded collision chunk found, look up the .nxs in
                # the collision cache. Poses (localToMesh transforms per shape) are
                # parsed from the RED header wrapper stored alongside the NXS data.
                if do_import_collision and not found_embedded_collision:
                    try:
                        collision_path, shape_items = get_collision_for_mesh_with_poses(filename)
                        if collision_path and os.path.exists(collision_path):
                            log.info(f'Loading collision from cache: {collision_path}')
                            create_from_nxs(collision_path, shape_items=shape_items)
                    except Exception as e:
                        log.warning(f'Failed to load collision from cache: {e}')

                collision_seconds = time.perf_counter() - collision_started
                total_seconds = time.perf_counter() - mesh_started
                if total_seconds >= _MESH_PROFILE_WARN_THRESHOLD:
                    _log_mesh_profile_warning(
                        "cr2w mesh %s total %.3fs (parse %.3fs, prepare %.3fs, collision %.3fs, submeshes %d, objects %d)",
                        meshName,
                        total_seconds,
                        parse_seconds,
                        prepare_seconds,
                        collision_seconds,
                        len(mesh_chunks),
                        len(final_bl_meshes or []),
                    )
                return (final_bl_meshes, armatures)
            except Exception as e:
                raise e
    else:
        anim = None
    return anim

root_folders = [
    "animations",
    "characters",
    "dlc",
    "engine",
    "environment",
    "fx",
    "game",
    "gameplay",
    "items",
    "levels",
    "living_world",
    "merged_content",
    "movies",
    "qa",
    "quests",
    "scripts",
    "soundbanks"
]

possible_folders = [
    'files\\Raw\\Mod',
    'files\\Raw\\DLC',
    'files\\Mod\\Cooked',
    'files\\Mod\\Uncooked',
    'files\\DLC\\Cooked',
    'files\\DLC\\Uncooked',
]

def get_repo_from_abs_path(file_path):
    UNCOOK_DIR = get_uncook_path(bpy.context)
    MOD_DIR = get_mod_directory(bpy.context)
    MOD_TEX_PATH = get_modded_texture_path(bpy.context)
    addon_prefs = get_all_addon_prefs(bpy.context)

    def _try_strip(path, root):
        root = os.path.realpath(bpy.path.abspath(root)) if root else ""
        if root and root in path:
            return path.replace(root + '\\', '')
        return None

    # REDkit project paths
    for path_item in addon_prefs.redkit_projects:
        if path_item.path:
            # Try workspace subfolder first (REDkit convention)
            result = _try_strip(file_path, os.path.join(path_item.path, "workspace"))
            if not result:
                result = _try_strip(file_path, path_item.path)
            if result:
                return result

    # REDkit uncooked depot
    result = _try_strip(file_path, addon_prefs.redkit_uncooked_path)
    if result:
        return result

    # REDkit depot (r4data)
    result = _try_strip(file_path, addon_prefs.redkit_depot_path)
    if result:
        return result

    # Witcher 2 roots must be checked before the Witcher 3 uncook root so W2
    # imported meshes/items keep their source-game relative paths.
    for w2_root in configured_w2_repo_roots(bpy.context):
        result = _try_strip(file_path, w2_root)
        if result:
            return result

    # Mod directory
    if MOD_DIR and MOD_DIR in file_path:
        file_path = file_path.replace(MOD_DIR + '\\', '')
        for folder in possible_folders:
            if folder in file_path:
                file_path = file_path.replace(folder + '\\', '')
                break
        return file_path

    # Uncook path
    result = _try_strip(file_path, UNCOOK_DIR)
    if result:
        return result

    # Modded texture path
    result = _try_strip(file_path, MOD_TEX_PATH)
    if result:
        return result

    for root_folder in root_folders:
        if root_folder in file_path:
            parts = file_path.split(root_folder, 1)
            if len(parts) == 2:
                first_part, second_part = parts[0], root_folder + parts[1]
            else:
                first_part, second_part = file_path, ""
            return second_part

    game_repo_path = os.path.splitdrive(file_path)[1]
    return game_repo_path.lstrip('\\/')

def prepare_mesh_import(CData, bufferInfos, the_material_names, the_materials, meshName, meshFile,
                do_import_mats,
                do_import_armature,
                keep_lod_meshes,
                do_merge_normals,
                rotate_180,
                keep_empty_lods,
                keep_proxy_meshes,
                hide_zero_weight_faces,
                build_material_nodes=True,
                target_armature=None):
    #TODO proxy meshes don't have lod0 they start at lod1, should import proxy anyway if requested
    #meshData = meshFile
    created_mesh_bl = []
    created_mesh_entries = []
    source_is_skinned = _mesh_has_skinned_chunks(CData)
    source_lod_levels = []
    for mesh_entry in getattr(CData, "meshDataAllMeshes", []) or []:
        try:
            source_lod_levels.append(int(getattr(getattr(mesh_entry, "meshInfo", None), "lod", 0) or 0))
        except (TypeError, ValueError):
            source_lod_levels.append(0)
    primary_source_lod_level = min(source_lod_levels) if source_lod_levels else 0

    def _apply_common_mesh_settings(settings):
        settings['autohideDistance'] = CData.autohideDistance
        settings['isTwoSided'] = CData.isTwoSided
        settings['useExtraStreams'] = CData.useExtraStreams
        settings['generalizedMeshRadius'] = CData.generalizedMeshRadius
        settings['mergeInGlobalShadowMesh'] = CData.mergeInGlobalShadowMesh
        settings['isOccluder'] = CData.isOccluder
        settings['smallestHoleOverride'] = CData.smallestHoleOverride
        settings['source_is_skinned'] = source_is_skinned
        settings['entityProxy'] = CData.entityProxy
        if hasattr(CData, 'soundInfo') and CData.soundInfo:
            settings.soundInfo_enabled = True
            settings.soundInfo_soundTypeIdentification = CData.soundInfo.get('soundTypeIdentification', '')
            size_id = CData.soundInfo.get('soundSizeIdentification', '')
            settings.soundInfo_soundSizeIdentification = size_id if size_id else 'default'
            bone_mapping = CData.soundInfo.get('soundBoneMappingInfo', '')
            valid_enums = {'TorsoArmor', 'LegArmor', 'HandArmor', 'HeadArmor'}
            settings.soundInfo_soundBoneMappingInfo = bone_mapping if bone_mapping in valid_enums else 'NONE'

    for idx, meshDataBl in enumerate(CData.meshDataAllMeshes):
        mesh_info = CData.meshDataAllMeshes[idx].meshInfo
        mat_id = getattr(mesh_info, "materialID", 0)
        lod_level = getattr(mesh_info, "lod", 0) #if not bufferInfos.verticesBuffer else bufferInfos.verticesBuffer[idx].lod
        distance = getattr(mesh_info, "distance", 0.0)

        if log.isEnabledFor(logging.DEBUG):
            log.debug(
                "Submesh[%d] '%s': lod=%s distance=%s mat_id=%s %s",
                idx,
                meshName,
                lod_level,
                distance,
                mat_id,
                _mesh_data_debug_summary(meshDataBl),
            )
        if not _mesh_vertices_are_importable(meshDataBl):
            log.warning(
                "Skipping submesh[%d] '%s' because parsed vertex coordinates are invalid.",
                idx,
                meshName,
            )
            continue
        dropped_faces = _sanitize_mesh_faces_for_import(meshDataBl)
        if dropped_faces:
            log.warning(
                "Dropped %d invalid faces from submesh[%d] '%s' before Blender mesh creation.",
                dropped_faces,
                idx,
                meshName,
            )
        
        if not keep_lod_meshes and lod_level > 0 and "proxy" not in meshName:
            log.debug(
                "Stopping submesh import for '%s' at index %d because keep_lod_meshes=False and encountered LOD %s",
                meshName,
                idx,
                lod_level,
            )
            break
        # KNOWN LIMITATION: Some LOD meshes (likely auto-generated) have no geometry data.
        # Blender crashes if we try to create a mesh with zero valid faces, so skip them.
        skip = True
        if not meshDataBl.vertex3DCoords and keep_empty_lods:
            skip = False # most likely a proxy mesh with zero verts
        for faces in meshDataBl.faces:
            if faces.count(0) == 3:
                continue
            else:
                skip = False
                break
        try:
            if not skip:
                log.debug("Creating Blender mesh for submesh[%d] '%s' (lod=%s mat_id=%s)", idx, meshName, lod_level, mat_id)
                obj = do_blender_mesh_import(meshDataBl, CData, do_merge_normals)
                #obj.witcherui_MeshSettings['witcher_lod_level'] = lod_level
                #obj.witcherui_MeshSettings['witcher_distance'] = distance
                #obj.witcherui_MeshSettings['witcher_mat_id'] = mat_id
                obj.witcherui_MeshSettings['source_lod_level'] = lod_level
                obj.witcherui_MeshSettings['distance'] = distance
                obj.witcherui_MeshSettings['mat_id'] = mat_id
                obj.witcherui_MeshSettings['item_repo_path'] = get_repo_from_abs_path(meshFile.fileName)
                obj.witcherui_MeshSettings['make_export_dir'] = True
                _apply_common_mesh_settings(obj.witcherui_MeshSettings)
                created_mesh_bl.append(obj)
                created_mesh_entries.append((obj, lod_level, mat_id))
                log.debug(
                    "Created submesh[%d] '%s' as object '%s' (polygons=%d material_slots=%d)",
                    idx,
                    meshName,
                    obj.name,
                    len(getattr(obj.data, "polygons", [])),
                    len(obj.material_slots),
                )
            else:
                log.debug("Skipping submesh[%d] '%s' because no valid geometry was detected", idx, meshName)
        except Exception:
            log.critical(
                "warning couldn't create one of the meshes at index %s (mesh='%s' lod=%s mat_id=%s)",
                idx,
                meshName,
                lod_level,
                mat_id,
            )
            log.critical(
                "Submesh[%d] '%s' creation failed. %s",
                idx,
                meshName,
                _mesh_data_debug_summary(meshDataBl),
                exc_info=True,
            )

    # If nothing was created (e.g., mesh has no verts/materials), return an empty object.
    if not created_mesh_bl:
        empty_mesh = bpy.data.meshes.new(meshName)
        empty_obj = bpy.data.objects.new(meshName, empty_mesh)
        bpy.context.collection.objects.link(empty_obj)
        try:
            empty_obj.witcherui_MeshSettings['item_repo_path'] = get_repo_from_abs_path(meshFile.fileName)
            empty_obj.witcherui_MeshSettings['make_export_dir'] = True
            empty_obj.witcherui_MeshSettings['source_lod_level'] = primary_source_lod_level
            empty_obj.witcherui_MeshSettings['source_is_skinned'] = source_is_skinned
        except Exception:
            pass
        return ([empty_obj], [])
    
    lod0 = []
    lod1 = []
    lod2 = []
    lod3 = []
    lods_to_create = [lod0,
                    lod1,
                    lod2,
                    lod3]

    # Pre-create all materials in CR2W materialNames order and keep direct
    # object references. Material names are truncated by dropping the
    # beginning so the end of the source name stays visible.
    def _keep_name_end(name: str, max_len: int = 63) -> str:
        return name if len(name) <= max_len else name[-max_len:]

    def _norm_material_owner_key(path: str) -> str:
        return str(path or "").replace("/", "\\").lower()

    source_mesh_repo_path = _norm_material_owner_key(get_repo_from_abs_path(meshFile.fileName))

    def _resolve_blender_material_name(source_name: str, source_mesh_path: str) -> str:
        base_name = _keep_name_end(source_name, 63)
        existing = bpy.data.materials.get(base_name)
        if existing is None:
            return base_name
        if (
            existing.get("w3_source_material_name") == source_name
            and _norm_material_owner_key(existing.get("w3_source_mesh_path")) == source_mesh_path
        ):
            return base_name

        # Collision: prepend a short numeric tag while preserving the name end.
        counter = 1
        while True:
            prefix = f"{counter:03d}_"
            tail_len = 63 - len(prefix)
            candidate = prefix + _keep_name_end(source_name, tail_len)
            existing = bpy.data.materials.get(candidate)
            if existing is None:
                return candidate
            if (
                existing.get("w3_source_material_name") == source_name
                and _norm_material_owner_key(existing.get("w3_source_mesh_path")) == source_mesh_path
            ):
                return candidate
            counter += 1

    ordered_materials = []
    for mat_name in the_material_names:
        blender_mat_name = _resolve_blender_material_name(mat_name, source_mesh_repo_path)
        mat = bpy.data.materials.get(blender_mat_name)
        if mat is None:
            mat = bpy.data.materials.new(blender_mat_name)
            mat["w3_source_material_name"] = mat_name
            mat["w3_source_mesh_path"] = source_mesh_repo_path
        ordered_materials.append(mat)
    log.info(
        "Prepared material slots for '%s': blender_slots=%d source_material_names=%d",
        meshName,
        len(ordered_materials),
        len(the_material_names or []),
    )
    if the_material_names:
        debug_mapping = [
            f"{i}:{the_material_names[i]} -> {ordered_materials[i].name}"
            for i in range(min(len(ordered_materials), len(the_material_names)))
        ]
        log.debug("Material slot mapping for '%s': %s", meshName, " | ".join(debug_mapping))

    for mesh_bl, lod_level, mat_id in created_mesh_entries:
        lod0.append(mesh_bl) if lod_level == 0 else 0
        lod1.append(mesh_bl) if lod_level == 1 else 0
        lod2.append(mesh_bl) if lod_level == 2 else 0
        lod3.append(mesh_bl) if lod_level == 3 else 0

        # Add ALL materials in CR2W order so the slot list matches the
        # original file.  Assign faces to the correct material index.
        for mat in ordered_materials:
            mesh_bl.data.materials.append(mat)
        if ordered_materials and (mat_id < 0 or mat_id >= len(ordered_materials)):
            log.warning(
                "Object '%s' submesh materialID=%s is out of range for %d prepared slots on mesh '%s'",
                mesh_bl.name,
                mat_id,
                len(ordered_materials),
                meshName,
            )
        poly_count = len(mesh_bl.data.polygons)
        if poly_count:
            mesh_bl.data.polygons.foreach_set(
                "material_index", np.full(poly_count, mat_id, dtype=np.int32)
            )
        log.debug(
            "Assigned %d material slots to object '%s' and set %d polygons to material index %s",
            len(ordered_materials),
            mesh_bl.name,
            len(mesh_bl.data.polygons),
            mat_id,
        )

    bind_armature_obj = None
    if (
        target_armature is not None
        and getattr(target_armature, "type", None) == 'ARMATURE'
        and _mesh_has_skinned_chunks(CData)
    ):
        if _target_armature_matches_mesh_bones(CData, target_armature):
            bind_armature_obj = target_armature
        else:
            log.info(
                "Mesh '%s' does not match target '%s' rest space; importing its own armature.",
                meshName,
                getattr(target_armature, "name", ""),
            )
    create_mesh_armature = bool(do_import_armature and bind_armature_obj is None)

    if create_mesh_armature:
        try:
            #==========#
            # Armature #
            #==========#
            if _mesh_has_skinned_chunks(CData):
                scale = 1.0
                armature = bpy.data.armatures.new(CData.modelName+"_"+f"ARM_DATA")
                
                armature_obj = bpy.data.objects.new(CData.modelName+"_"+f"ARM", armature)
                armature_obj.show_in_front = True
                bpy.context.collection.objects.link(armature_obj)

                # SELECT ARM
                armature_obj.select_set(True)
                bpy.context.view_layer.objects.active = armature_obj
                
                bpy.ops.object.mode_set(mode='EDIT')
                bl_bones = []
                for name in CData.boneData.jointNames:
                    bl_bone = armature.edit_bones.new(name)
                    bl_bones.append(bl_bone)
                    bl_bone.tail = (Vector([0, 0, 0.01]) * scale) + bl_bone.head
                    
                for idx, bone_matrix in enumerate(CData.boneData.boneMatrices):
                    bl_bone =  armature_obj.data.edit_bones.get(CData.boneData.jointNames[idx])
                    mat = _bone_matrix_to_rest_matrix(bone_matrix)
                    bl_bone.matrix = mat
    
                # ROTATE ARM 180
                # if rotate_180:
                #     armature_obj.rotation_euler[2] = np.pi
                #     bpy.ops.object.transform_apply(location=False, rotation=True, scale=False)
                do_fix_tail = get_do_fix_tail(bpy.context) #True
                if do_fix_tail:
                    rotate_and_connect_bones(armature_obj)
                try:
                    rig_settings = armature_obj.data.witcherui_RigSettings
                    set_rig_rot90_enabled(rig_settings, do_fix_tail)
                except Exception:
                    pass
                bpy.ops.object.mode_set(mode='OBJECT')
                #from io_import_w2l.exporters import export_mesh
                #_bone_data = export_mesh.extract_bone_data(armature_obj, CData.boneData.boneMatrices)
        except Exception as e:
            log.error("Problem creating armature")
        
    # LODS
    final_bl_meshes = []
    if lod0 or lod1 or lod2 or lod3:
        if bpy.context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        for idx, lod_meshes in enumerate(lods_to_create):
            if lod_meshes:
                joinable_meshes = [
                    obj for obj in lod_meshes
                    if obj is not None
                    and getattr(obj, "type", "") == 'MESH'
                    and getattr(obj, "data", None) is not None
                    and len(getattr(obj.data, "vertices", ())) > 0
                ]
                if not joinable_meshes:
                    log.warning(
                        "Skipping LOD join for '%s' lod%d: no mesh data in %d candidate objects",
                        meshName,
                        idx,
                        len(lod_meshes),
                    )
                    continue
                if len(joinable_meshes) != len(lod_meshes):
                    log.warning(
                        "LOD join filtered empty/non-mesh objects for '%s' lod%d: kept=%d dropped=%d",
                        meshName,
                        idx,
                        len(joinable_meshes),
                        len(lod_meshes) - len(joinable_meshes),
                    )
                bpy.ops.object.select_all(action='DESELECT')
                bpy.context.view_layer.objects.active = joinable_meshes[0]
                for bl_mesh in joinable_meshes:
                    bl_mesh.select_set(True)
                if len(joinable_meshes) > 1:
                    bpy.ops.object.join()
                joined_obj = joinable_meshes[0] if len(joinable_meshes) == 1 else bpy.context.selected_objects[:][0]
                joined_obj.name = meshName+"_lod"+str(idx)
                joined_obj.witcherui_MeshSettings['source_lod_level'] = idx
                 
                ## ROTATE 180
                # if rotate_180:
                #     joined_obj.select_set(True)
                #     joined_obj.rotation_euler[2] = np.pi
                #     bpy.ops.object.transform_apply(location=False, rotation=True, scale=False)
                    
                final_bl_meshes.append(joined_obj)

                if (_mesh_has_skinned_chunks(CData) and (bind_armature_obj is not None or create_mesh_armature)):
                    target = bind_armature_obj or armature_obj
                    if target is not None:
                        bpy.context.view_layer.objects.active = target
                        _ensure_armature_binding(joined_obj, target)
                if not keep_lod_meshes and not keep_proxy_meshes:
                    break
                        # if bl_mesh != lod_meshes[0]:
                        #     lod_meshes[0].append(bl_mesh)

    for mesh_obj in final_bl_meshes:
        if not _mark_imported_tangent_basis(mesh_obj.data):
            log.debug(
                "Imported tangent basis is unavailable or incomplete on '%s'.",
                mesh_obj.name,
            )

    is_skinned_mesh = _mesh_has_skinned_chunks(CData)
    if hide_zero_weight_faces and is_skinned_mesh:
        for mesh_obj in final_bl_meshes:
            zero_weight_vert_count, hidden_face_count = _hide_zero_weight_faces(mesh_obj)
            if hidden_face_count:
                log.info(
                    "Hidden %d faces touching %d zero-weight vertices on skinned mesh '%s'",
                    hidden_face_count,
                    zero_weight_vert_count,
                    mesh_obj.name,
                )
        # override = bpy.context.copy()
        # override["area.type"] = ['OUTLINER']
        # override["display_mode"] = ['ORPHAN_DATA']
        # bpy.ops.outliner.orphans_purge(override) 

    #===========#
    # Materials #
    #===========#
    if do_import_mats and final_bl_meshes:
        apply_mesh_materials(
            meshFile,
            the_materials,
            the_material_names,
            final_bl_meshes,
            meshName,
            build_material_nodes=build_material_nodes,
        )

    armatures = []
    if (_mesh_has_skinned_chunks(CData) and create_mesh_armature):
        armature_obj.select_set(True)
        bpy.context.view_layer.objects.active = armature_obj
        armatures.append(armature_obj)
    elif bind_armature_obj is not None:
        bpy.context.view_layer.objects.active = bind_armature_obj
    else:
        if final_bl_meshes:
            bpy.context.view_layer.objects.active = final_bl_meshes[0]
    for mesh in final_bl_meshes:
        mesh.select_set(True)
    try:
        from ..unreal_export.mesh_signature import mesh_geometry_signature
        for mesh_obj in final_bl_meshes:
            mesh_obj.witcherui_MeshSettings['source_signature'] = mesh_geometry_signature(mesh_obj)
    except Exception:
        pass
    return (final_bl_meshes, armatures)


def apply_mesh_materials(meshFile, the_materials, the_material_names, final_bl_meshes, meshName, build_material_nodes=True):
    if final_bl_meshes:
        if meshFile.HEADER.version <= 115:
            uncook_path = w2_source_repo_root_if_configured(getattr(meshFile, "fileName", "") or "")
            if not uncook_path:
                roots = configured_w2_repo_roots(bpy.context)
                uncook_path = roots[0] if roots else ""
            uncook_path = (uncook_path.rstrip("\\/") + "\\") if uncook_path else ""
        else:
            uncook_path = get_texture_path(bpy.context)+"\\"
        
        materials = []
        handles = getattr(the_materials, "Handles", None) if the_materials else None
        if handles:
            log.info(
                "Resolving mesh materials for '%s': handles=%d material_names=%d objects=%d uncook_path='%s'",
                meshName,
                len(handles),
                len(the_material_names or []),
                len(final_bl_meshes),
                uncook_path,
            )
            for handle_idx, o in enumerate(handles):
                slot_name = the_material_names[handle_idx] if handle_idx < len(the_material_names) else f"<missing-name-{handle_idx}>"
                if o.Reference is not None:
                    materials.append(meshFile.CHUNKS.CHUNKS[o.Reference])
                    materials[-1].local = True
                    log.debug(
                        "Material handle[%d] '%s': resolved local chunk ref=%s type=%s",
                        handle_idx,
                        slot_name,
                        o.Reference,
                        getattr(materials[-1], "Type", type(materials[-1]).__name__),
                    )
                else:
                    log.debug(
                        "Material handle[%d] '%s': resolving depot '%s'",
                        handle_idx,
                        slot_name,
                        getattr(o, "DepotPath", None),
                    )
                    # Reuse the session material cache.
                    loaded = material_reader._load_material_root_chunk(
                        o.DepotPath,
                        version=meshFile.HEADER.version,
                    )
                    if loaded is not None:
                        loaded.local = False
                        loaded.DepotPath = o.DepotPath
                        materials.append(loaded)
                        log.debug(
                            "Material handle[%d] '%s': resolved external material type=%s depot='%s'",
                            handle_idx,
                            slot_name,
                            getattr(loaded, "Type", type(loaded).__name__),
                            getattr(loaded, "DepotPath", None),
                        )
                    else:
                        log.warning(f"Could not resolve material handle: {o.DepotPath} - inserting placeholder to preserve slot alignment")
                        materials.append(None)
        load_materials = True if materials else False
        if load_materials:
            mat_filename = "witcher_mat"
            log.info(
                "Applying resolved mesh materials for '%s': resolved=%d objects=%d",
                meshName,
                len(materials),
                len(final_bl_meshes),
            )
            load_w3_materials_CR2W_Mesh(
                final_bl_meshes,
                uncook_path,
                materials,
                the_material_names,
                mat_filename=mat_filename,
                build_material_nodes=build_material_nodes,
            )


def import_mesh_materials(filename, mesh_objects, embedded_cmesh_chunk_index=None):
    mesh_objects = [o for o in mesh_objects or [] if getattr(o, "type", "") == 'MESH']
    if not mesh_objects:
        return 0
    (CData, bufferInfos, the_material_names, the_materials, meshName, meshFile) = dc_mesh.load_bin_mesh(
        filename,
        False,
        False,
        embedded_cmesh_chunk_index=embedded_cmesh_chunk_index,
    )
    apply_mesh_materials(meshFile, the_materials, the_material_names, mesh_objects, meshName)
    return len(mesh_objects)


#returns mesh object
def do_blender_mesh_import(meshDataBl: MeshData, CData: CommonData, do_merge_normals:bool):
    if True: #try:
        import bpy
        def _diag(msg):
            log.debug("do_blender_mesh_import[%s]: %s", CData.modelName, msg)
        _diag(f"START verts={len(meshDataBl.vertex3DCoords)} faces={len(meshDataBl.faces)} "
              f"normalsAll={len(meshDataBl.normalsAll)} skinningVerts={len(meshDataBl.skinningVerts)} "
              f"jointNames={len(CData.boneData.jointNames)} BIMBI={len(CData.boneData.BoneIndecesMappingBoneIndex)}")
        name = CData.modelName+"_Mesh"
        mesh = bpy.data.meshes.new(name)
        mesh_ob = bpy.data.objects.new(name, mesh)
        bpy.context.collection.objects.link(mesh_ob)
        bpy.context.view_layer.objects.active = mesh_ob
        _diag("BEFORE from_pydata")
        mesh.from_pydata(meshDataBl.vertex3DCoords, [], meshDataBl.faces)
        _diag(f"AFTER from_pydata (loops={len(mesh.loops)} polys={len(mesh.polygons)})")
        
        #=========#
        #    UV   #
        #=========#
        # Always add DiffuseUV; add SecondUV only when explicitly used or
        # when data is non-default.
        vert_count = len(meshDataBl.vertex3DCoords)
        uv2_data = meshDataBl.UV2_vertex3DCoords
        if len(uv2_data) != vert_count:
            uv2_data = [[0.0, 1.0] for _ in range(vert_count)]

        uv1_data = meshDataBl.UV_vertex3DCoords
        if len(uv1_data) != vert_count:
            uv1_data = [[0.0, 1.0] for _ in range(vert_count)]

        allUVMaps = [("DiffuseUV", uv1_data)]
        has_meaningful_uv2 = any(
            abs(float(uv[0])) > 1e-6 or abs(float(uv[1]) - 1.0) > 1e-6
            for uv in (uv2_data or [])
        )
        if CData.useExtraStreams or has_meaningful_uv2:
            allUVMaps.append(("SecondUV", uv2_data))
        _diag(f"BEFORE UV loop (count={len(allUVMaps)})")
        for uv_name, uv_data in allUVMaps:
            _diag(f"  UV: {uv_name} data_len={len(uv_data)}")
            uv_layer = mesh.uv_layers.new(name=uv_name)
            # Build flat UV array mapped from loop -> vertex using foreach_set
            loop_count = len(mesh.loops)
            loop_vert_indices = np.empty(loop_count, dtype=np.int32)
            mesh.loops.foreach_get("vertex_index", loop_vert_indices)
            uv_arr = np.array(uv_data, dtype=np.float64)
            flat_uvs = uv_arr[loop_vert_indices].ravel()
            uv_layer.data.foreach_set("uv", flat_uvs)
        _diag("AFTER UV loop")
        if mesh.uv_layers.get("DiffuseUV"):
            # Keep export behavior deterministic: UV0 comes from DiffuseUV.
            diffuse_uv = mesh.uv_layers["DiffuseUV"]
            mesh.uv_layers.active = diffuse_uv
            if hasattr(diffuse_uv, "active_render"):
                diffuse_uv.active_render = True

        #==============#
        # Vertex Color #
        #==============#
        color_data = meshDataBl.vertexColor
        if color_data is not None and len(color_data) != vert_count:
            color_data = [[0.0, 0.0, 0.0, 0.0] for _ in range(vert_count)]

        has_meaningful_color = color_data is not None and any(
            abs(float(col[0])) > 1e-6
            or abs(float(col[1])) > 1e-6
            or abs(float(col[2])) > 1e-6
            or abs(float(col[3])) > 1e-6
            for col in color_data
        )
        if CData.useExtraStreams or has_meaningful_color:
            if color_data is None:
                color_data = [[1.0, 1.0, 1.0, 1.0]] * vert_count
            color_attr = mesh.color_attributes.new(name = 'Color', domain = 'POINT', type = 'BYTE_COLOR')
            flat_colors = np.array(color_data, dtype=np.float32).ravel()
            color_attr.data.foreach_set("color", flat_colors)

        #=========#
        # Normals #
        #=========#
        
        fbx_method = True
        if fbx_method: # taken from blender fbx importer
            if bpy.app.version < (4, 1, 0):
                mesh.create_normals_split()

                for face in mesh.polygons:
                    face.use_smooth = True  # loop normals have effect only if smooth shading ?

                n_normals = array.array('d', meshDataBl.normalsAll)
                normals = np.frombuffer(n_normals, dtype='d')
                normals /= np.linalg.norm(normals, axis=-1)
                
                generator = blen_read_geom_array_gen_direct_looptovert(mesh, normals, 3)
                
                def _process(blend_data, blen_attr, fbx_data, xform, item_size, blen_idx, fbx_idx):
                    the_loop = mesh.loops[blen_idx]
                    datayes = fbx_data[fbx_idx:fbx_idx + item_size]
                    setattr(the_loop, blen_attr, datayes)
                    normalized_vector = datayes / np.linalg.norm(datayes)
                for blen_idx, fbx_idx in generator:
                    _process(mesh.loops, "normal", normals, False, 3, blen_idx, fbx_idx)

                # create custom data to write normals correctly?
                mesh.validate(clean_customdata=False)  # important to not remove loop normals here!
                mesh.update()

                clnors = array.array('f', [0.0] * (len(mesh.loops) * 3))
                mesh.loops.foreach_get("normal", clnors)

                mesh.polygons.foreach_set("use_smooth", [True] * len(mesh.polygons))

                mesh.normals_split_custom_set(tuple(zip(*(iter(clnors),) * 3)))
                mesh.use_auto_smooth = True
                #mesh.show_edge_sharp = True  # optionnal
                mesh.free_normals_split()
            else:
                _diag(f"BEFORE normals path (4.1+) normalsAll={len(meshDataBl.normalsAll)}")
                mesh.polygons.foreach_set("use_smooth", [True] * len(mesh.polygons))

                # Build per-vertex normals array and normalize
                vert_normals = np.array(meshDataBl.normalsAll, dtype=np.float64).reshape(-1, 3)
                norms = np.linalg.norm(vert_normals, axis=1, keepdims=True)
                norms[norms == 0] = 1.0
                vert_normals /= norms

                def _build_loop_custom_normals_vectorized():
                    loop_count = len(mesh.loops)
                    loop_vert_indices = np.empty(loop_count, dtype=np.int32)
                    mesh.loops.foreach_get("vertex_index", loop_vert_indices)
                    # Clamp indices to valid range
                    max_idx = len(vert_normals) - 1
                    np.clip(loop_vert_indices, 0, max_idx, out=loop_vert_indices)
                    loop_normals = vert_normals[loop_vert_indices]
                    # Replace zero-length normals with up vector
                    zero_mask = np.all(loop_normals == 0, axis=1)
                    loop_normals[zero_mask] = [0.0, 0.0, 1.0]
                    return [tuple(n) for n in loop_normals]

                the_custom_normals = _build_loop_custom_normals_vectorized()
                pre_validate_loop_count = len(the_custom_normals)

                mesh.validate(clean_customdata=False)  # important to not remove loop normals here!
                mesh.update()

                post_validate_loop_count = len(mesh.loops)
                if pre_validate_loop_count != post_validate_loop_count:
                    log.warning(
                        "Mesh '%s' validate changed loop count during custom normal assignment (%d -> %d). Rebuilding loop normals.",
                        mesh_ob.name,
                        pre_validate_loop_count,
                        post_validate_loop_count,
                    )
                    the_custom_normals = _build_loop_custom_normals_vectorized()
                if len(the_custom_normals) != len(mesh.loops):
                    log.warning(
                        "Skipping custom normals on '%s' because generated normals (%d) != loops (%d) after rebuild",
                        mesh_ob.name,
                        len(the_custom_normals),
                        len(mesh.loops),
                    )
                    the_custom_normals = []

                if the_custom_normals:
                    _diag(f"BEFORE normals_split_custom_set count={len(the_custom_normals)}")
                    mesh.normals_split_custom_set(the_custom_normals)
                    _diag("AFTER normals_split_custom_set")
        else:
            mesh_da = mesh
            if bpy.app.version < (4, 1, 0):
                mesh_da.create_normals_split() #!BLENDER >4.1
                mesh_da.use_auto_smooth = True
            mesh_da.normals_split_custom_set_from_vertices(meshDataBl.normals)
            if bpy.app.version < (4, 1, 0):
                mesh_da.free_normals_split()

            #do_merge_normals = False
            if do_merge_normals:
                def merge_normals():
                    bpy.ops.mesh.select_all(action='SELECT')
                    bpy.ops.mesh.merge_normals() # some meshes cause blender to hang doing this command
                    bpy.ops.mesh.select_all(action='DESELECT')
                bpy.ops.object.mode_set(mode='EDIT', toggle=False)
                merge_normals()
                bpy.ops.object.mode_set(mode='OBJECT')

        _store_imported_tangent_basis(
            mesh,
            meshDataBl,
            source_version=getattr(CData, "sourceMeshVersion", 0),
        )
        #=========#
        # Weights #
        #=========#
        sorted_array = []
        sorted_names = set()
        duplicate_group_names = 0

        def _append_unique_group_name(group_name):
            nonlocal duplicate_group_names
            if not group_name:
                return
            if group_name in sorted_names:
                duplicate_group_names += 1
                return
            sorted_names.add(group_name)
            sorted_array.append(group_name)

        # for Witcher 2
        for index in CData.boneData.BoneIndecesMappingBoneIndex:
            if 0 <= index < len(CData.boneData.jointNames):
                _append_unique_group_name(CData.boneData.jointNames[index])

        if len(sorted_array) < len(CData.boneData.BoneIndecesMappingBoneIndex):
            for the_bone in CData.boneData.jointNames:
                _append_unique_group_name(the_bone)
                if len(sorted_array) == len(CData.boneData.BoneIndecesMappingBoneIndex):
                    break

        if duplicate_group_names:
            log.debug(
                "Skipped %d duplicate vertex group names while importing '%s'",
                duplicate_group_names,
                mesh_ob.name,
            )

        #todo check skinning verts for any groups that are not created for some reason
        _diag(f"BEFORE vertex_groups create (sorted_array={len(sorted_array)})")
        for group_name in sorted_array:
            try:
                if mesh_ob.vertex_groups.get(group_name) is None:
                    mesh_ob.vertex_groups.new(name=group_name)
            except Exception as e:
                log.error("Error creating vertex group: %s", e)
        _diag(f"BEFORE assignVertexGroup loop (skinningVerts={len(meshDataBl.skinningVerts)})")
        for vert in meshDataBl.skinningVerts:
            try:
                assignVertexGroup(vert, CData, mesh_ob)
            except Exception as e:
                if _derive_mesh_is_static(CData):
                    log.critical('found skinning verts on static mesh')
                    break
        _diag("END do_blender_mesh_import")
        return mesh_ob
    # except Exception as e:
    #     log.warning("Not in Blender")
    #     return False

def load_w3_materials_CR2W_Mesh(
        objs: List[Object]
        ,uncook_path: str
        ,materials_bin: str
        ,material_names: str
        ,force_mat_update = False
        ,mat_filename = str
        ,build_material_nodes = True
    ):
    materials_started = time.perf_counter()
    objs = objs or []
    materials_bin = materials_bin or []
    material_names = material_names or []
    obj_names = [obj.name for obj in objs]
    log.info(
        "load_w3_materials_CR2W_Mesh: objects=%d materials=%d material_names=%d mat_filename='%s'",
        len(objs),
        len(materials_bin or []),
        len(material_names or []),
        mat_filename,
    )
    log.debug("Material targets: %s", obj_names)
    if (materials_bin or []) and (material_names or []) and len(materials_bin) != len(material_names):
        log.warning(
            "Material handle/name count mismatch during mesh material import: handles=%d names=%d",
            len(materials_bin),
            len(material_names),
        )

    for idx, mat in enumerate(materials_bin):
        slot_started = time.perf_counter()
        if mat is None:
            log.warning(f"Skipping unresolved material at slot {idx} ({material_names[idx] if idx < len(material_names) else '?'})")
            continue
        xml_mat_name = material_names[idx] if idx < len(material_names) else f"Material{idx}"
        if idx >= len(material_names):
            log.warning("Material slot %d has no material name entry; using fallback '%s'", idx, xml_mat_name)
        log.info(xml_mat_name)
        log.debug(
            "Material slot %d '%s': chunk_type=%s local=%s depot='%s'",
            idx,
            xml_mat_name,
            getattr(mat, "Type", type(mat).__name__),
            getattr(mat, "local", None),
            getattr(mat, "DepotPath", None),
        )
        target_slots = []
        target_slot_obj_names = []
        missing_slot_objs = []
        for obj in objs:
            if idx < len(obj.material_slots):
                target_slots.append(obj.material_slots[idx])
                target_slot_obj_names.append(obj.name)
            else:
                missing_slot_objs.append(f"{obj.name}(slots={len(obj.material_slots)})")
        if missing_slot_objs:
            log.debug("Material slot %d '%s' missing on objects: %s", idx, xml_mat_name, ", ".join(missing_slot_objs))

        target_mat = target_slots[0].material if target_slots else None
        if not target_mat:
            log.debug("Material slot %d '%s': no direct slot target, trying fallback name matching", idx, xml_mat_name)
            # Fallback for legacy/irregular slot layouts.
            for obj in objs:
                for m in obj.data.materials:
                    if m and (m.name == xml_mat_name or m.name in xml_mat_name):
                        target_mat = m
                        log.debug(
                            "Material slot %d '%s': fallback matched Blender material '%s' on object '%s'",
                            idx,
                            xml_mat_name,
                            m.name,
                            obj.name,
                        )
                        break
                if target_mat:
                    break

        if target_mat:
            log.debug(
                "Material slot %d '%s': building target material '%s' for objects=%s",
                idx,
                xml_mat_name,
                target_mat.name,
                target_slot_obj_names or ["<fallback>"],
            )
            try:
                finished_mat = setup_w3_material_CR2W(
                    uncook_path,
                    target_mat,
                    mat,
                    force_update=force_mat_update,
                    mat_filename=mat_filename,
                    build_nodes=build_material_nodes,
                )
            except Exception:
                log.exception(
                    "Material slot %d '%s' failed during setup (target='%s', chunk_type=%s, local=%s, depot='%s')",
                    idx,
                    xml_mat_name,
                    getattr(target_mat, "name", None),
                    getattr(mat, "Type", type(mat).__name__),
                    getattr(mat, "local", None),
                    getattr(mat, "DepotPath", None),
                )
                continue
            if target_slots:
                for slot in target_slots:
                    slot.material = finished_mat
            else:
                for obj in objs:
                    if target_mat.name in obj.material_slots:
                        obj.material_slots[target_mat.name].material = finished_mat
            log.debug(
                "Material slot %d '%s': applied Blender material '%s' to %d direct slots",
                idx,
                xml_mat_name,
                getattr(finished_mat, "name", None),
                len(target_slots),
            )
        else:
            log.info(
                "Material slot %d '%s': no target Blender slot/material found (likely skipped submesh/LOD-only slot)",
                idx,
                xml_mat_name,
            )
        slot_seconds = time.perf_counter() - slot_started
        if slot_seconds >= _MESH_PROFILE_MATERIAL_WARN_THRESHOLD:
            _log_mesh_profile_warning(
                "mesh material slot %d '%s' total %.3fs (objects %d, target %s)",
                idx,
                xml_mat_name,
                slot_seconds,
                len(objs),
                getattr(target_mat, "name", "<none>") if target_mat else "<none>",
            )
    total_seconds = time.perf_counter() - materials_started
    if total_seconds >= _MESH_PROFILE_WARN_THRESHOLD:
        _log_mesh_profile_warning(
            "mesh material batch total %.3fs (objects %d, materials %d, names %d, mat_file %s)",
            total_seconds,
            len(objs),
            len(materials_bin),
            len(material_names),
            mat_filename,
        )
        #finished_mat.name = finished_mat.name +"_"+ target_mat.name

def assignVertexGroup(vert, CData, mesh_ob):
    boneIdx = vert.boneId
    vertexWeight = vert.strength
    if vertexWeight != 0:
        # use original index to get current bone name in blender
        boneName = CData.boneData.jointNames[boneIdx]
        
        #For Witcher 2 the index mapping is broken here.
        #boneName = CData.boneData.jointNames[CData.boneData.BoneIndecesMappingBoneIndex[boneIdx]] 
        
        if boneName:
            vertGroup = mesh_ob.vertex_groups.get(boneName)
            if vertGroup:
                #raise Exception('Vert Groups should all be created!')
                #vertGroup = mesh_ob.vertex_groups.new(name=boneName)
                vertGroup.add([vert.vertexId], vertexWeight, 'REPLACE')

def get_vertex_weights(mesh_obj, vertex_group_name):
    vertex_weights = []
    vertex_group = mesh_obj.vertex_groups.get(vertex_group_name)
    if vertex_group:
        for vertex in mesh_obj.data.vertices:
            vertex_weights.append(vertex.groups[vertex_group.index].weight)
    return vertex_weights

def get_mesh_info(
    me,
    mesh_ob,
    meshDataBl=None,
    tangent_space_mode=TANGENT_SPACE_REDENGINE,
    tangent_handedness_mode=TANGENT_HANDEDNESS_AUTO,
    basis_source_mesh=None,
):
    exportMeshdata:MeshData = MeshData()
    tangent_space_mode = _normalize_tangent_space_mode(tangent_space_mode)
    handedness_source_mesh = basis_source_mesh if basis_source_mesh is not None else me
    tangent_handedness_mode = resolve_tangent_handedness_mode(
        tangent_space_mode,
        tangent_handedness_mode,
        handedness_source_mesh,
    )
    flip_tangent_handedness = (
        tangent_handedness_mode == TANGENT_HANDEDNESS_FLIPPED)

    if bpy.app.version < (4, 1, 0):
        me.use_auto_smooth = True
        me.calc_normals_split()

    me.calc_loop_triangles()

    # Prefer explicit Witcher UV names when present; otherwise fall back to
    # index order. This keeps round-trips stable if Blender reorders layers.
    uv_layers = me.uv_layers
    uv1_layer = _get_primary_uv_layer(me)

    uv2_layer = uv_layers.get("SecondUV") if len(uv_layers) > 1 else None
    if uv2_layer is None:
        for uv_layer in uv_layers:
            if uv_layer != uv1_layer:
                uv2_layer = uv_layer
                break

    preserved_normals = None
    preserved_tangents = None
    preserved_binormals = None
    mikk_normals = None
    mikk_tangents = None
    mikk_binormals = None
    if tangent_space_mode == TANGENT_SPACE_PRESERVE_IMPORTED:
        validation_mesh = basis_source_mesh or me
        basis_valid, basis_reason = imported_tangent_basis_status(validation_mesh)
        if not basis_valid:
            raise ValueError(
                f"Preserve Imported Basis is unavailable for '{mesh_ob.name}': {basis_reason}.")
        preserved_normals = _read_vector_mesh_attribute(me, IMPORTED_NORMAL_ATTRIBUTE, 'POINT')
        preserved_tangents = _read_vector_mesh_attribute(me, IMPORTED_TANGENT_ATTRIBUTE, 'POINT')
        preserved_binormals = _read_vector_mesh_attribute(me, IMPORTED_BINORMAL_ATTRIBUTE, 'POINT')
        if any(value is None for value in (
            preserved_normals, preserved_tangents, preserved_binormals
        )):
            raise ValueError(
                f"Preserve Imported Basis data was lost while preparing '{mesh_ob.name}' for export.")
    elif tangent_space_mode == TANGENT_SPACE_MIKKTSPACE:
        mikk_normals = _read_vector_mesh_attribute(me, MIKK_NORMAL_ATTRIBUTE, 'CORNER')
        mikk_tangents = _read_vector_mesh_attribute(me, MIKK_TANGENT_ATTRIBUTE, 'CORNER')
        mikk_binormals = _read_vector_mesh_attribute(me, MIKK_BINORMAL_ATTRIBUTE, 'CORNER')
        if mikk_normals is None or mikk_tangents is None or mikk_binormals is None:
            raise ValueError(
                f"MikkTSpace corner basis was not prepared for '{mesh_ob.name}'.")

    color_values = None
    color_domain = None
    color_attributes = getattr(me, "color_attributes", None)
    if color_attributes is not None:
        for candidate in (
            color_attributes.get("Color"),
            getattr(color_attributes, "active_color", None),
            getattr(color_attributes, "active", None),
        ):
            if (
                candidate is not None
                and candidate.data_type in ('BYTE_COLOR', 'FLOAT_COLOR')
                and candidate.domain in ('POINT', 'CORNER')
            ):
                color_domain = candidate.domain
                color_values = np.empty((len(candidate.data), 4), dtype=np.float32)
                candidate.data.foreach_get("color", color_values.ravel())
                break

    vertex_group_names = {
        group.index: group.name
        for group in mesh_ob.vertex_groups
        if group.name != ZERO_WEIGHT_MASK_GROUP_NAME
    }
    source_vertex_weights = {}
    for vert in me.vertices:
        weights = []
        for group in sorted(vert.groups, key=lambda item: item.group):
            bone_name = vertex_group_names.get(group.group)
            if bone_name and group.weight != 0.0:
                weights.append((bone_name, float(group.weight)))
        source_vertex_weights[vert.index] = tuple(weights)

    def _read_loop_color(loop_idx: int, vert_idx: int):
        if color_values is None:
            return [0.0, 0.0, 0.0, 0.0]

        data_idx = loop_idx if color_domain == 'CORNER' else vert_idx
        if data_idx >= len(color_values):
            return [0.0, 0.0, 0.0, 1.0]

        color = color_values[data_idx]
        return [float(color[0]), float(color[1]), float(color[2]), float(color[3])]

    def _read_loop_uv(uv_layer, loop_idx: int):
        if not uv_layer or loop_idx >= len(uv_layer.data):
            return (0.0, 1.0)
        uv = uv_layer.data[loop_idx].uv
        u = float(uv[0])
        v = float(uv[1])
        if not (math.isfinite(u) and math.isfinite(v)):
            return (0.0, 1.0)
        return (u, v)


    def _triangle_uv_handedness(loop_indices):
        if uv1_layer is None or len(loop_indices) != 3:
            return 0

        uv_a = _read_loop_uv(uv1_layer, loop_indices[0])
        uv_b = _read_loop_uv(uv1_layer, loop_indices[1])
        uv_c = _read_loop_uv(uv1_layer, loop_indices[2])
        delta_u1 = uv_b[0] - uv_a[0]
        delta_u2 = uv_c[0] - uv_a[0]
        delta_v1 = uv_b[1] - uv_a[1]
        delta_v2 = uv_c[1] - uv_a[1]
        determinant = (delta_u1 * delta_v2) - (delta_u2 * delta_v1)
        if _uv_determinant_is_degenerate(
            delta_u1, delta_v1, delta_u2, delta_v2, determinant
        ):
            return 0
        return -1 if determinant < 0.0 else 1


    vertex_normal_clusters = {}

    def _canonical_loop_normal(src_vert_idx: int, normal):
        clusters = vertex_normal_clusters.setdefault(src_vert_idx, [])
        for existing in clusters:
            dot = (
                existing[0] * normal[0]
                + existing[1] * normal[1]
                + existing[2] * normal[2]
            )
            if dot >= 0.999999:
                return existing
        clusters.append(normal)
        return normal

    vertex_lookup = {}
    loops = me.loops
    for loop_tri in me.loop_triangles:
        tri_indices = []
        triangle_is_mirrored = (
            tangent_space_mode == TANGENT_SPACE_REDENGINE
            and _triangle_uv_handedness(loop_tri.loops) < 0
        )
        for loop_idx in loop_tri.loops:
            loop = loops[loop_idx]
            src_vert_idx = loop.vertex_index
            src_vert = me.vertices[src_vert_idx]

            tangent = None
            bitangent = None
            if tangent_space_mode == TANGENT_SPACE_PRESERVE_IMPORTED:
                normal = tuple(float(value) for value in preserved_normals[src_vert_idx])
                tangent = tuple(float(value) for value in preserved_tangents[src_vert_idx])
                bitangent = tuple(float(value) for value in preserved_binormals[src_vert_idx])
            elif tangent_space_mode == TANGENT_SPACE_MIKKTSPACE:
                normal = tuple(float(value) for value in mikk_normals[loop_idx])
                tangent = tuple(float(value) for value in mikk_tangents[loop_idx])
                bitangent = tuple(float(value) for value in mikk_binormals[loop_idx])
                if flip_tangent_handedness:
                    bitangent = tuple(-value for value in bitangent)
            else:
                normal = _canonical_loop_normal(
                    src_vert_idx,
                    (float(loop.normal[0]), float(loop.normal[1]), float(loop.normal[2])),
                )
            uv1 = _read_loop_uv(uv1_layer, loop_idx)
            uv2 = _read_loop_uv(uv2_layer, loop_idx)

            color = _read_loop_color(loop_idx, src_vert_idx)

            if tangent_space_mode in (
                TANGENT_SPACE_MIKKTSPACE,
                TANGENT_SPACE_PRESERVE_IMPORTED,
            ):
                key = (
                    src_vert_idx, normal, uv1, uv2,
                    tangent, bitangent, tuple(color),
                )
            elif tangent_space_mode == TANGENT_SPACE_REDENGINE:
                key = (src_vert_idx, normal, uv1, uv2, triangle_is_mirrored, tuple(color))
            else:
                key = (src_vert_idx, normal, uv1, uv2, tuple(color))
            export_vert_idx = vertex_lookup.get(key)
            if export_vert_idx is None:
                export_vert_idx = len(exportMeshdata.vertex3DCoords)
                vertex_lookup[key] = export_vert_idx

                exportMeshdata.vertex3DCoords.append([
                    float(src_vert.co.x),
                    float(src_vert.co.y),
                    float(src_vert.co.z),
                ])
                exportMeshdata.normals.append([normal[0], normal[1], normal[2]])
                exportMeshdata.normalsAll.extend([normal[0], normal[1], normal[2]])
                exportMeshdata.UV_vertex3DCoords.append([uv1[0], uv1[1]])
                exportMeshdata.UV2_vertex3DCoords.append([uv2[0], uv2[1]])
                exportMeshdata.vertexColor.append(color)
                if tangent is not None and bitangent is not None:
                    exportMeshdata.tangent_vector.append([
                        tangent[0], tangent[1], tangent[2]])
                    exportMeshdata.extra_vectors.append([
                        bitangent[0], bitangent[1], bitangent[2]])

                for bone_name, weight in source_vertex_weights.get(src_vert_idx, ()):
                    vse = VertexSkinningEntry()
                    vse.vertexId = export_vert_idx
                    vse.boneId = bone_name
                    vse.boneId_idx = None
                    vse.strength = weight
                    exportMeshdata.skinningVerts.append(vse)

            tri_indices.append(export_vert_idx)

        exportMeshdata.faces.append(tri_indices)

    exportMeshdata.meshInfo = SMeshInfos()
    exportMeshdata.meshInfo.numIndices = len(exportMeshdata.faces) * 3
    exportMeshdata.meshInfo.numVertices = len(exportMeshdata.vertex3DCoords)
    if tangent_space_mode == TANGENT_SPACE_REDENGINE:
        exportMeshdata.tangent_vector, exportMeshdata.extra_vectors = (
            _solve_meshdata_tangent_basis(exportMeshdata, mirror_correction=True))
        if flip_tangent_handedness:
            exportMeshdata.extra_vectors = [
                [-value for value in binormal]
                for binormal in exportMeshdata.extra_vectors
            ]
    elif tangent_space_mode == TANGENT_SPACE_NO_MIRROR:
        exportMeshdata.tangent_vector, exportMeshdata.extra_vectors = (
            _solve_meshdata_tangent_basis(exportMeshdata, mirror_correction=False))

    if bpy.app.version < (4, 1, 0):
        me.free_normals_split()

    return exportMeshdata
