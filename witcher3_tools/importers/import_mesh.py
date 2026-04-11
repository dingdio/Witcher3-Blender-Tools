import logging
import os
import math
import bpy
from bpy.types import Object
from typing import List, Tuple
from mathutils import Vector, Matrix
import numpy as np
import array
from ..CR2W.common_blender import repo_file, get_collision_for_mesh, win_safe_path
from ..importers.import_rig import rotate_and_connect_bones

from ..cloth_util import setup_w3_material_CR2W
from .. import get_texture_path, get_uncook_path, get_w2_unbundle_path, get_witcher2_game_path
from .. import file_helpers
from ..CR2W import w3_types
from ..CR2W import read_json_w3
from ..CR2W import CR2W_reader
from ..w3_armature_constants import *
from ..importers import data_types
from ..CR2W import dc_mesh
from ..CR2W.dc_mesh import MeshData
from ..CR2W.Types.BlenderMesh import CommonData
from ..CR2W.Types.SBufferInfos import SMeshInfos, EMeshVertexType, VertexSkinningEntry
from ..CR2W.dc_entity import CCollisionShapeConvex, CCollisionShapeTriMesh, CCollisionShapeBox, CCollisionShapeCapsule, CCollisionShapeSphere
from ..importers.import_nxs import createCol, createTri, createBox, createCapsule, createSphere, create_from_nxs
from .. import get_do_fix_tail, set_rig_rot90_enabled

log = logging.getLogger(__name__)

ZERO_WEIGHT_MASK_GROUP_NAME = "_w3_zero_weight_hidden"
ZERO_WEIGHT_MASK_MODIFIER_NAME = "W3 Zero Weight Mask"


def _mesh_has_skinned_chunks(CData):
    mesh_infos = getattr(CData, "meshInfos", None) or []
    return any(getattr(mesh_info, "vertexType", None) == EMeshVertexType.EMVT_SKINNED for mesh_info in mesh_infos)


def _derive_mesh_is_static(CData):
    mesh_infos = getattr(CData, "meshInfos", None) or []
    if mesh_infos:
        return not _mesh_has_skinned_chunks(CData)
    return bool(getattr(CData, "isStatic", False))

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


def _normalize_vector3(value, fallback):
    x = float(value[0])
    y = float(value[1])
    z = float(value[2])
    if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
        return fallback

    length = math.sqrt((x * x) + (y * y) + (z * z))
    if length < 1e-8:
        return fallback
    return (x / length, y / length, z / length)


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

    bx = -(ny * tangent[2] - nz * tangent[1])
    by = -(nz * tangent[0] - nx * tangent[2])
    bz = -(nx * tangent[1] - ny * tangent[0])
    bitangent = _normalize_vector3((bx, by, bz), (0.0, 1.0, 0.0))
    return tangent, bitangent


def _triangle_corner_angle(edge_a, edge_b):
    len_a = math.sqrt(float(np.dot(edge_a, edge_a)))
    len_b = math.sqrt(float(np.dot(edge_b, edge_b)))
    if len_a < 1e-8 or len_b < 1e-8:
        return 0.0

    cos_angle = float(np.dot(edge_a, edge_b)) / (len_a * len_b)
    cos_angle = max(-1.0, min(1.0, cos_angle))
    return math.acos(cos_angle)


def _solve_meshdata_tangent_basis(mesh_data: MeshData):
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

    tan1 = np.zeros((vert_count, 3), dtype=np.float64)
    tan2 = np.zeros((vert_count, 3), dtype=np.float64)
    degenerate_uv_faces = 0

    for face in mesh_data.faces:
        if len(face) != 3:
            continue

        i1, i2, i3 = (int(face[0]), int(face[1]), int(face[2]))
        if (
            i1 < 0 or i2 < 0 or i3 < 0 or
            i1 >= vert_count or i2 >= vert_count or i3 >= vert_count or
            i1 == i2 or i2 == i3 or i1 == i3
        ):
            continue

        p1 = positions[i1]
        p2 = positions[i2]
        p3 = positions[i3]
        uv1 = uvs[i1]
        uv2 = uvs[i2]
        uv3 = uvs[i3]

        edge1 = p2 - p1
        edge2 = p3 - p1
        delta_u1 = uv2[0] - uv1[0]
        delta_u2 = uv3[0] - uv1[0]
        delta_v1 = uv2[1] - uv1[1]
        delta_v2 = uv3[1] - uv1[1]
        denom = (delta_u1 * delta_v2) - (delta_u2 * delta_v1)
        if not math.isfinite(float(denom)) or abs(float(denom)) < 1e-20:
            degenerate_uv_faces += 1
            continue

        inv_denom = 1.0 / float(denom)
        sdir = ((delta_v2 * edge1) - (delta_v1 * edge2)) * inv_denom
        tdir = ((delta_u1 * edge2) - (delta_u2 * edge1)) * inv_denom

        angle_1 = _triangle_corner_angle(p2 - p1, p3 - p1)
        angle_2 = _triangle_corner_angle(p3 - p2, p1 - p2)
        angle_3 = _triangle_corner_angle(p1 - p3, p2 - p3)
        for vert_idx, weight in ((i1, angle_1), (i2, angle_2), (i3, angle_3)):
            tan1[vert_idx] += sdir * weight
            tan2[vert_idx] += tdir * weight

    tangents = []
    bitangents = []
    fallback_count = 0
    for vert_idx in range(vert_count):
        normal = _normalize_vector3(normals[vert_idx], (0.0, 0.0, 1.0))
        tangent_accum = tan1[vert_idx]
        tangent_proj = tangent_accum - (np.dot(normal, tangent_accum) * np.asarray(normal, dtype=np.float64))
        tangent = _normalize_vector3(tangent_proj, None)
        if tangent is None:
            tangent, bitangent = _fallback_tangent_basis(normal)
            fallback_count += 1
        else:
            handedness = -1.0 if float(np.dot(np.cross(normal, tangent), tan2[vert_idx])) < 0.0 else 1.0
            bitangent = -np.cross(normal, tangent) * handedness
            bitangent = _normalize_vector3(bitangent, _fallback_tangent_basis(normal)[1])

        tangents.append([tangent[0], tangent[1], tangent[2]])
        bitangents.append([bitangent[0], bitangent[1], bitangent[2]])

    if degenerate_uv_faces or fallback_count:
        log.debug(
            "Solved tangent basis for %d verts with %d degenerate UV faces and %d fallback tangents.",
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
                do_import_cache_collision:bool = False,
                hide_zero_weight_faces:bool = True) -> w3_types.CSkeletalAnimationSet:
    dirpath, file = os.path.split(filename)
    basename, ext = os.path.splitext(file)
    if ext.lower() in ('.w2mesh', '.w2ent'):
        with open(win_safe_path(filename), "rb") as _mesh_file:
            try:
                (CData, bufferInfos, the_material_names, the_materials, meshName, meshFile) = dc_mesh.load_bin_mesh(filename, keep_lod_meshes, keep_proxy_meshes)
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
                (final_bl_meshes, armatures) = prepare_mesh_import(CData, bufferInfos, the_material_names, the_materials, meshName, meshFile,
                    do_import_mats,
                    do_import_armature,
                    keep_lod_meshes,
                    do_merge_normals,
                    rotate_180,
                    keep_empty_lods,
                    keep_proxy_meshes,
                    hide_zero_weight_faces)
                
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
                for CHUNK in meshFile.CHUNKS.CHUNKS:
                    CHUNK:W_CLASS
                    if CHUNK.name == 'CCollisionMesh':
                        log.info('Found Collision Mesh')
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
                # Optionally load .nxs collision file from collision cache
                if do_import_cache_collision:
                    try:
                        collision_path = get_collision_for_mesh(filename)
                        if collision_path and os.path.exists(collision_path):
                            log.info(f'Loading collision from cache: {collision_path}')
                            create_from_nxs(collision_path)
                    except Exception as e:
                        log.warning(f'Failed to load collision from cache: {e}')

                return (final_bl_meshes, armatures)
            except Exception as e:
                raise e
    else:
        anim = None
    return anim

from .. import get_mod_directory, get_texture_path, get_modded_texture_path, get_all_addon_prefs
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
                hide_zero_weight_faces):
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

        log.debug(
            "Submesh[%d] '%s': lod=%s distance=%s mat_id=%s %s",
            idx,
            meshName,
            lod_level,
            distance,
            mat_id,
            _mesh_data_debug_summary(meshDataBl),
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
        for face in mesh_bl.data.polygons:
            face.material_index = mat_id
        log.debug(
            "Assigned %d material slots to object '%s' and set %d polygons to material index %s",
            len(ordered_materials),
            mesh_bl.name,
            len(mesh_bl.data.polygons),
            mat_id,
        )

    if do_import_armature:
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
                    bone_matrix = bone_matrix.fields
                    mat:Matrix = Matrix()
                    
                    mat[0][0], mat[0][1], mat[0][2], mat[0][3] = bone_matrix[0], bone_matrix[4], bone_matrix[8], bone_matrix[12]
                    mat[1][0], mat[1][1], mat[1][2], mat[1][3] = bone_matrix[1], bone_matrix[5], bone_matrix[9], bone_matrix[13]
                    mat[2][0], mat[2][1], mat[2][2], mat[2][3] = bone_matrix[2], bone_matrix[6], bone_matrix[10], bone_matrix[14]
                    mat[3][0], mat[3][1], mat[3][2], mat[3][3] = bone_matrix[3], bone_matrix[7], bone_matrix[11], bone_matrix[15]


                    # poss = mat.to_translation()
                    # quat = mat.to_quaternion()
                    # scl = mat.to_scale()
                    mat = mat.inverted()
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

                if (_mesh_has_skinned_chunks(CData) and do_import_armature):
                    bpy.context.view_layer.objects.active = bpy.data.objects[armature_obj.name]
                    #bpy.ops.object.parent_set(type="ARMATURE_NAME", xmirror=False, keep_transform=False)
                    for mesh_obj in final_bl_meshes:
                        mesh_obj.parent = armature_obj
                        armature_mod = mesh_obj.modifiers.new(name="Armature", type='ARMATURE')
                        armature_mod.object = armature_obj
                        armature_mod.use_vertex_groups = True
                if not keep_lod_meshes and not keep_proxy_meshes:
                    break
                        # if bl_mesh != lod_meshes[0]:
                        #     lod_meshes[0].append(bl_mesh)

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
        ### MATERIALS
        force_mat_update = True
        
        
        if meshFile.HEADER.version <= 115:
            uncook_path = get_witcher2_game_path(bpy.context)+"\\data\\" #! THE PATH WITH THE TEXTURES NOT THE FBX FILES
            #uncook_path_modkit = get_witcher2_game_path(bpy.context)
        else:
            uncook_path = get_texture_path(bpy.context)+"\\" #! THE PATH WITH THE TEXTURES NOT THE FBX FILES
            #uncook_path_modkit = get_uncook_path(bpy.context)
        xml_path = "w2mesh"
        
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
                    material_file_chunks = CR2W_reader.load_material(repo_file(o.DepotPath, version=meshFile.HEADER.version))
                    loaded = None
                    for chunk in material_file_chunks:
                        if chunk.Type in ("CMaterialInstance", "CMaterialGraph"):
                            loaded = chunk
                            if chunk.Type == "CMaterialGraph":
                                # Attach sibling CMaterialParameter* chunks so the material
                                # system can read the graph's default parameter values.
                                loaded._graph_params = [
                                    c for c in material_file_chunks
                                    if c.Type.startswith("CMaterialParameter")
                                ]
                            break
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
        #material_names = [o.String.split('::')[1] for o in chunk.GetVariableByName('apexMaterialNames').elements]

        load_materials = True if materials else False
        if load_materials:
            mat_filename = "witcher_mat"
            log.info(
                "Applying resolved mesh materials for '%s': resolved=%d objects=%d",
                meshName,
                len(materials),
                len(final_bl_meshes),
            )
            load_w3_materials_CR2W_Mesh(final_bl_meshes, uncook_path, materials, the_material_names, mat_filename=mat_filename)

    #===========#
    #  Finish   #
    #===========#
    #select everything just imported
    armatures = []
    if (_mesh_has_skinned_chunks(CData) and do_import_armature):
        armature_obj.select_set(True)
        bpy.context.view_layer.objects.active = armature_obj
        armatures.append(armature_obj)
    else:
        if final_bl_meshes:
            bpy.context.view_layer.objects.active = final_bl_meshes[0]
    for mesh in final_bl_meshes:
        mesh.select_set(True)
    return (final_bl_meshes, armatures)

#returns mesh object
def do_blender_mesh_import(meshDataBl: MeshData, CData: CommonData, do_merge_normals:bool):
    if True: #try:
        import bpy
        name = CData.modelName+"_Mesh"
        mesh = bpy.data.meshes.new(name)
        mesh_ob = bpy.data.objects.new(name, mesh)
        #col = bpy.data.collections.get("Collection")
        #col.objects.link(obj)
        bpy.context.collection.objects.link(mesh_ob)
        bpy.context.view_layer.objects.active = mesh_ob
        mesh.from_pydata(meshDataBl.vertex3DCoords, [], meshDataBl.faces)
        
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
        for uv_name, uv_data in allUVMaps:
            uv_layer = mesh.uv_layers.new(name=uv_name)
            # Build flat UV array mapped from loop -> vertex using foreach_set
            loop_count = len(mesh.loops)
            loop_vert_indices = np.empty(loop_count, dtype=np.int32)
            mesh.loops.foreach_get("vertex_index", loop_vert_indices)
            uv_arr = np.array(uv_data, dtype=np.float64)
            flat_uvs = uv_arr[loop_vert_indices].ravel()
            uv_layer.data.foreach_set("uv", flat_uvs)
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
                    mesh.normals_split_custom_set(the_custom_normals)
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
        #=========#
        # Weights #
        #=========#
        sorted_array = []



        # for Witcher 2
        for index in CData.boneData.BoneIndecesMappingBoneIndex:
            if index < len(CData.boneData.jointNames):
                sorted_array.append(CData.boneData.jointNames[index])

        if len(sorted_array) < len(CData.boneData.BoneIndecesMappingBoneIndex):
            for the_bone in CData.boneData.jointNames:
                if the_bone not in sorted_array:
                    sorted_array.append(the_bone)
                if len(sorted_array) == len(CData.boneData.BoneIndecesMappingBoneIndex):
                    break

        #todo check skinning verts for any groups that are not created for some reason
        for group_name in sorted_array:
            try:
                mesh_ob.vertex_groups.new(name=group_name)
            except Exception as e:
                log.error("Error creating vertex group: %s", e)
        for vert in meshDataBl.skinningVerts:
            try:
                assignVertexGroup(vert, CData, mesh_ob)
            except Exception as e:
                if _derive_mesh_is_static(CData):
                    log.critical('found skinning verts on static mesh')
                    break

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
    ):
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

def get_mesh_info(me, mesh_ob, meshDataBl = None):
    exportMeshdata:MeshData = MeshData()

    if bpy.app.version < (4, 1, 0):
        me.use_auto_smooth = True
        me.calc_normals_split()

    me.calc_loop_triangles()

    # Prefer explicit Witcher UV names when present; otherwise fall back to
    # index order. This keeps round-trips stable if Blender reorders layers.
    uv_layers = me.uv_layers
    uv1_layer = uv_layers.get("DiffuseUV") if len(uv_layers) > 0 else None
    if uv1_layer is None and len(uv_layers) > 0:
        uv1_layer = uv_layers[0]

    uv2_layer = uv_layers.get("SecondUV") if len(uv_layers) > 1 else None
    if uv2_layer is None:
        for uv_layer in uv_layers:
            if uv_layer != uv1_layer:
                uv2_layer = uv_layer
                break

    color_attribute = None
    if me.color_attributes.active_color_index != -1 and me.color_attributes.active:
        color_attribute = me.color_attributes.active

    vertex_group_names = {
        group.index: group.name
        for group in mesh_ob.vertex_groups
        if group.name != ZERO_WEIGHT_MASK_GROUP_NAME
    }
    source_vertex_weights = {}
    for vert in me.vertices:
        weights = []
        for group in vert.groups:
            bone_name = vertex_group_names.get(group.group)
            if bone_name and group.weight != 0.0:
                weights.append((bone_name, group.weight))
        source_vertex_weights[vert.index] = weights

    def _read_loop_color(loop_idx: int, vert_idx: int):
        if not color_attribute:
            return [0.0, 0.0, 0.0, 0.0]

        data_idx = loop_idx if color_attribute.domain == 'CORNER' else vert_idx
        if data_idx >= len(color_attribute.data):
            return [0.0, 0.0, 0.0, 1.0]

        item = color_attribute.data[data_idx]
        if color_attribute.data_type in ('BYTE_COLOR', 'FLOAT_COLOR'):
            color = item.color
            return [float(color[0]), float(color[1]), float(color[2]), float(color[3])]
        if color_attribute.data_type == 'FLOAT_VECTOR':
            vec = item.vector
            return [float(vec[0]), float(vec[1]), float(vec[2]), 1.0]
        return [0.0, 0.0, 0.0, 1.0]

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
        if not math.isfinite(determinant) or abs(determinant) < 1e-20:
            return 0
        return -1 if determinant < 0.0 else 1

    vertex_lookup = {}
    loops = me.loops
    for loop_tri in me.loop_triangles:
        tri_indices = []
        triangle_handedness = _triangle_uv_handedness(loop_tri.loops)
        for loop_idx in loop_tri.loops:
            loop = loops[loop_idx]
            src_vert_idx = loop.vertex_index
            src_vert = me.vertices[src_vert_idx]

            normal = (float(loop.normal[0]), float(loop.normal[1]), float(loop.normal[2]))
            uv1 = _read_loop_uv(uv1_layer, loop_idx)
            uv2 = _read_loop_uv(uv2_layer, loop_idx)

            color = _read_loop_color(loop_idx, src_vert_idx)

            # tangent bases 
            key = (src_vert_idx, normal, uv1, uv2, triangle_handedness, tuple(color))
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

                for bone_name, weight in source_vertex_weights.get(src_vert_idx, []):
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
    exportMeshdata.tangent_vector, exportMeshdata.extra_vectors = _solve_meshdata_tangent_basis(exportMeshdata)

    # W2 mesh indices are UInt16; fail fast instead of writing wrapped/corrupt
    # indices that lead to broken UVs/geometry on reimport.
    if exportMeshdata.meshInfo.numVertices > 65535:
        raise ValueError(
            f"Export mesh '{mesh_ob.name}' expands to {exportMeshdata.meshInfo.numVertices} vertices "
            f"after normal/UV splits (UInt16 limit is 65535). Split the mesh into smaller parts."
        )

    if bpy.app.version < (4, 1, 0):
        me.free_normals_split()

    return exportMeshdata

