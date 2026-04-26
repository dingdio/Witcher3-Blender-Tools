import logging
import os
import re
import math
import bpy
import time
import json
from pathlib import Path
from mathutils import Matrix
log = logging.getLogger(__name__)

from ..CR2W import mesh_builder
from .. import get_wolvenkit
from ..CR2W import cr2w_writer
from ..CR2W.Types.VariousTypes import CMatrix4x4
from ..CR2W.Types.SBufferInfos import BoneData
from ..importers.import_rig import get_ordered_bones
from ..importers.import_mesh import get_mesh_info, ZERO_WEIGHT_MASK_GROUP_NAME
from ..CR2W.dc_entity import (
    CCollisionShapeConvex, CCollisionShapeTriMesh,
    CCollisionShapeBox, CCollisionShapeSphere, CCollisionShapeCapsule
)
from .. import get_rig_rot90_enabled

# Collision suffixes to detect
COLLISION_SUFFIXES = ("_col", "_tri", "_box", "_sphere", "_capsule")
DEFAULT_PHYSICAL_MATERIAL = "default"
SKIN_WEIGHT_EPSILON = 1e-8


def _get_collision_material_name(mesh_obj):
    """Return the first material name, or a default if none are assigned."""
    if mesh_obj.data.materials:
        return mesh_obj.data.materials[0].name_full
    log.warning(
        f"Collision mesh '{mesh_obj.name}' has no physical material; using '{DEFAULT_PHYSICAL_MATERIAL}'."
    )
    return DEFAULT_PHYSICAL_MATERIAL

def get_collision_type(obj_name):
    """
    Detect collision type from object name, handling Blender's .NNN suffix.
    Returns the collision suffix (_col, _tri, _box, _sphere, _capsule) or None.

    Examples:
        'mesh_box' -> '_box'
        'mesh_box.001' -> '_box'
        'mesh_tri.003' -> '_tri'
    """
    # Strip Blender's .NNN suffix if present
    base_name = re.sub(r'\.\d{3}$', '', obj_name.lower())
    for suffix in COLLISION_SUFFIXES:
        if base_name.endswith(suffix):
            return suffix
    return None


def _mesh_has_skin_weights(mesh_obj):
    valid_group_indices = {
        group.index
        for group in getattr(mesh_obj, "vertex_groups", [])
        if group.name != ZERO_WEIGHT_MASK_GROUP_NAME
    }
    if not valid_group_indices:
        return False

    mesh_data = getattr(mesh_obj, "data", None)
    for vert in getattr(mesh_data, "vertices", []):
        for group in getattr(vert, "groups", []):
            if group.group in valid_group_indices and group.weight > SKIN_WEIGHT_EPSILON:
                return True
    return False


def _mesh_has_linked_armature(mesh_obj):
    parent = getattr(mesh_obj, "parent", None)
    if parent and getattr(parent, "type", None) == 'ARMATURE':
        bones = getattr(getattr(parent, "data", None), "bones", None)
        if bones and len(bones) > 0:
            return True

    for modifier in getattr(mesh_obj, "modifiers", []):
        armature_obj = getattr(modifier, "object", None)
        if modifier.type != 'ARMATURE' or not armature_obj or getattr(armature_obj, "type", None) != 'ARMATURE':
            continue
        bones = getattr(getattr(armature_obj, "data", None), "bones", None)
        if bones and len(bones) > 0:
            return True
    return False


def _mesh_requires_skinning(mesh_obj):
    return _mesh_has_linked_armature(mesh_obj) and _mesh_has_skin_weights(mesh_obj)


class WitcherMaterialInfo(object):
    def __init__(self):
        super(WitcherMaterialInfo, self).__init__()
        pass

def extract_bone_data(armature, matrix_ref=None, rotate_bones_90=False):
    bone_data = BoneData()
    bone_data.nbBones = len(armature.data.bones)
    ordered_bones = get_ordered_bones(armature)
    rot90_inv = Matrix.Rotation(math.radians(90), 4, 'Z') if rotate_bones_90 else None
    for bone in ordered_bones:
        bone_data.jointNames.append(bone.name)
        if rot90_inv:
            mat = (bone.matrix_local @ rot90_inv).inverted()
        else:
            mat = bone.matrix_local.inverted()
        bone_matrix = CMatrix4x4(None)
        bone_matrix.ax, bone_matrix.bx, bone_matrix.cx, bone_matrix.dx = mat[0][0], mat[0][1], mat[0][2], mat[0][3]
        bone_matrix.ay, bone_matrix.by, bone_matrix.cy, bone_matrix.dy = mat[1][0], mat[1][1], mat[1][2], mat[1][3]
        bone_matrix.az, bone_matrix.bz, bone_matrix.cz, bone_matrix.dz = mat[2][0], mat[2][1], mat[2][2], mat[2][3]
        bone_matrix.aw, bone_matrix.bw, bone_matrix.cw, bone_matrix.dw = mat[3][0], mat[3][1], mat[3][2], mat[3][3]
        bone_matrix.Create()
        bone_data.boneMatrices.append(bone_matrix)
    return bone_data


from ..w3_material_nodes import get_group_inputs, get_socket_value, get_repo_from_abs_path, is_path_resolved

def _collect_tex_nodes(from_node, depth=4, _visited=None):
    """Walk the node graph upstream from from_node, collecting all TEX_IMAGE nodes."""
    if _visited is None:
        _visited = set()
    node_id = id(from_node)
    if node_id in _visited or depth <= 0:
        return []
    _visited.add(node_id)
    if from_node.type == 'TEX_IMAGE':
        return [from_node]
    results = []
    for inp in from_node.inputs:
        if inp.is_linked:
            results.extend(_collect_tex_nodes(inp.links[0].from_node, depth - 1, _visited))
    return results


def _image_pixel_count(image):
    if image is None:
        return 0
    try:
        return image.size[0] * image.size[1]
    except Exception:
        return 0


def scan_principled_bsdf(mat, mesh_repo_dir=""):
    """Scan a material for a Principled BSDF and extract textures for auto-conversion.

    If the material has a Principled BSDF connected to Material Output (i.e. it is NOT
    a Witcher node-group material), this returns a list of input_prop dicts that can be
    used directly as ``input_props`` in the material export dict:

        [{'name': 'Diffuse', 'type': 'TEX_IMAGE', 'value': 'depot\\path.xbm', 'display_name': 'image.png'}, ...]

    When multiple TEX_IMAGE nodes feed an input (e.g. through a MixRGB), the largest
    image (by pixel count) is chosen.

    If no Principled BSDF is found, returns ``None``.
    If a BSDF is found but no usable textures, returns an empty list.

    ``mesh_repo_dir`` is used as a fallback directory for textures that cannot be
    resolved to a game-relative depot path (e.g. textures outside the uncook tree).
    """
    if not mat or not mat.node_tree:
        return None

    node_tree = mat.node_tree
    output_node = next(
        (n for n in node_tree.nodes if n.type == 'OUTPUT_MATERIAL' and n.is_active_output),
        None,
    )
    if not output_node:
        return None

    surface_inp = output_node.inputs.get('Surface')
    if not (surface_inp and surface_inp.is_linked):
        return None

    principled = surface_inp.links[0].from_node
    if principled.type != 'BSDF_PRINCIPLED':
        return None

    def _resolve(image):
        if not image:
            return None
        # Always place auto-converted textures next to the mesh in the depot.
        # Use just the image filename stem + .xbm so the user knows exactly where
        # to put the exported texture relative to their .w2mesh file.
        basename = os.path.splitext(os.path.basename(bpy.path.abspath(image.filepath)))[0] + '.xbm'
        if mesh_repo_dir:
            return os.path.join(mesh_repo_dir, basename).replace('/', '\\')
        return basename

    def _best_tex(input_socket):
        """Return the largest TEX_IMAGE node reachable from input_socket, or None."""
        if not (input_socket and input_socket.is_linked):
            return None
        nodes = _collect_tex_nodes(input_socket.links[0].from_node)
        if not nodes:
            return None
        return max(nodes, key=lambda n: _image_pixel_count(n.image))

    found = []

    # Base Color → Diffuse
    tex = _best_tex(principled.inputs.get("Base Color"))
    if tex:
        path = _resolve(tex.image)
        if path:
            found.append({'name': 'Diffuse', 'type': 'TEX_IMAGE', 'value': path,
                          'display_name': tex.image.name if tex.image else '?'})

    # Normal → Normal  (look through a Normal Map node if present)
    normal_inp = principled.inputs.get("Normal")
    if normal_inp and normal_inp.is_linked:
        from_node = normal_inp.links[0].from_node
        if from_node.type == 'NORMAL_MAP':
            color_inp = from_node.inputs.get("Color")
            tex = _best_tex(color_inp)
        else:
            tex = _best_tex(normal_inp)
        if tex:
            path = _resolve(tex.image)
            if path:
                found.append({'name': 'Normal', 'type': 'TEX_IMAGE', 'value': path,
                              'display_name': tex.image.name if tex.image else '?'})

    return found


def get_mesh_material_info(mesh_bl, mesh_obj=None):
    # Derive the mesh's repo directory for texture fallback paths
    mesh_repo_dir = ""
    if mesh_obj and hasattr(mesh_obj, 'witcherui_MeshSettings'):
        repo = mesh_obj.witcherui_MeshSettings.item_repo_path
        if repo:
            mesh_repo_dir = os.path.dirname(repo.replace('/', '\\'))

    material_props = []
    for mat in mesh_bl.materials:
        if not mat:
            continue
        mat_props = getattr(mat, 'witcher_props', None)
        if mat_props is None:
            log.warning(f"Material '{mat.name}' has no witcher_props, skipping")
            continue
        mat_dict = {
            'name': mat.name,
            'witcher_props': {
                'name': mat_props.name,
                'enableMask': mat_props.enableMask,
                'local': mat_props.local,
                #'base': mat_props.base,
                'base_custom': mat_props.base_custom,
                'input_props':[] #[{'name':input_prop.name, 'is_enabled': input_prop.is_enabled} for input_prop in mat_props.input_props]
            }
        }
        if mat_props.local:
            group_inputs = get_group_inputs(mat)
            if group_inputs:
                for input_socket in group_inputs:
                    if input_socket.is_linked:
                        linked_socket = input_socket.links[0].from_socket
                        if linked_socket.node.witcher_include:

                            if linked_socket.node.type == 'GROUP':
                                for input_socket_group in linked_socket.node.inputs:
                                    if input_socket_group.is_linked:
                                        linked_socket_inner = input_socket_group.links[0].from_socket
                                        mat_dict['witcher_props']['input_props'].append(
                                            {'name':linked_socket.node.name,
                                            'type': 'handle:CTextureArray',#linked_socket_inner.node.type,
                                            'value':get_socket_value(input_socket_group)})
                                        break
                            else:
                                export_type = linked_socket.node.type
                                if input_socket.type == 'VECTOR':
                                    export_type = 'COMBXYZ'
                                mat_dict['witcher_props']['input_props'].append(
                                    {'name':input_socket.name,
                                    'type': export_type,
                                    'value':get_socket_value(input_socket)})
            else:
                # No Witcher node group — check for a Principled BSDF and auto-convert
                bsdf_props = scan_principled_bsdf(mat, mesh_repo_dir)
                if bsdf_props is not None:
                    # Force local material with pbr_std base and extracted texture params
                    mat_dict['witcher_props']['local'] = True
                    mat_dict['witcher_props']['base_custom'] = r'engine\materials\graphs\pbr_std.w2mg'
                    mat_dict['witcher_props']['input_props'] = [
                        {'name': p['name'], 'type': p['type'], 'value': p['value']}
                        for p in bsdf_props
                    ]
                    log.info(f"Auto-converted Principled BSDF material '{mat.name}' → pbr_std with {[p['name'] for p in bsdf_props]}")
        
        
        material_props.append(mat_dict)
    return material_props

def furthest_vertex_distance_vector(mesh_obj, vector_obj):
    furthest_distance = 0
    for vert_obj in mesh_obj.data.vertices:
        distance = (vert_obj.co - vector_obj).length
        if distance > furthest_distance:
            furthest_distance = distance
    return furthest_distance

def furthest_vertex_distance(mesh_obj, bone_obj):
    vertex_group_name = bone_obj.name
    vertex_group = mesh_obj.vertex_groups[vertex_group_name]
    vertices = [v.index for v in mesh_obj.data.vertices if vertex_group.index in [g.group for g in v.groups]]
    furthest_distance = 0
    for vert in vertices:
        vert_obj = mesh_obj.data.vertices[vert]
        distance = (vert_obj.co - bone_obj.head.xyz).length
        if distance > furthest_distance:
            furthest_distance = distance
    return furthest_distance * 1.2

def group_exists(mesh_obj, group_name):
    for group in mesh_obj.vertex_groups:
        if group.name == group_name:
            return True
    return False

def _build_group_vertex_map(mesh_obj):
    """Pre-build a map of vertex_group_index → list of vertex coords.

    Scans all vertices once instead of once per bone.
    """
    group_verts = {}
    for v in mesh_obj.data.vertices:
        for g in v.groups:
            group_verts.setdefault(g.group, []).append(v.co)
    return group_verts

def get_vertex_group_info(armobj, mesh_ob):
    vgi = []
    group_name_to_index = {g.name: g.index for g in mesh_ob.vertex_groups}
    group_verts = _build_group_vertex_map(mesh_ob)

    for bone_obj in get_ordered_bones(armobj):
        gi = group_name_to_index.get(bone_obj.name)
        if gi is not None and gi in group_verts:
            bone_head = bone_obj.head.xyz
            furthest = 0.0
            for co in group_verts[gi]:
                dist = (co - bone_head).length
                if dist > furthest:
                    furthest = dist
            vgi.append(furthest * 1.2)
        else:
            vgi.append(0)
    return vgi

def convert_to_index_values(string_array, second_array):
    index_array = []
    for string in string_array:
        index_array.append(second_array.index(string)) if string in second_array else False
    return index_array

import bmesh

def split_mesh_by_material(mesh_obj):
    src_mesh = mesh_obj.data
    used_material_indices = sorted({poly.material_index for poly in src_mesh.polygons})

    # Empty mesh or no polygon assignments: export a direct copy.
    if not used_material_indices:
        mesh_copy = mesh_obj.copy()
        mesh_copy.data = src_mesh.copy()
        bpy.context.collection.objects.link(mesh_copy)
        return [mesh_copy]

    # Common case: one material slot in use, no split needed.
    if len(used_material_indices) == 1:
        mat_idx = used_material_indices[0]
        mesh_copy = mesh_obj.copy()
        single_mesh = src_mesh.copy()
        target_material = single_mesh.materials[mat_idx] if mat_idx < len(single_mesh.materials) else None
        single_mesh.materials.clear()
        if target_material is not None:
            single_mesh.materials.append(target_material)
        for poly in single_mesh.polygons:
            poly.material_index = 0
        mesh_copy.data = single_mesh
        bpy.context.collection.objects.link(mesh_copy)
        return [mesh_copy]

    final_meshes = []
    for mat_idx in used_material_indices:
        split_mesh = src_mesh.copy()
        bm = bmesh.new()
        bm.from_mesh(split_mesh)
        faces_to_delete = [face for face in bm.faces if face.material_index != mat_idx]
        if faces_to_delete:
            bmesh.ops.delete(bm, geom=faces_to_delete, context='FACES')
        bm.to_mesh(split_mesh)
        bm.free()
        split_mesh.update()

        if len(split_mesh.polygons) == 0:
            bpy.data.meshes.remove(split_mesh)
            continue

        # Each split object should expose exactly one material in slot 0.
        target_material = src_mesh.materials[mat_idx] if mat_idx < len(src_mesh.materials) else None
        split_mesh.materials.clear()
        if target_material is not None:
            split_mesh.materials.append(target_material)
        for poly in split_mesh.polygons:
            poly.material_index = 0

        split_obj = mesh_obj.copy()
        split_obj.data = split_mesh
        bpy.context.collection.objects.link(split_obj)
        final_meshes.append(split_obj)

    if not final_meshes:
        mesh_copy = mesh_obj.copy()
        mesh_copy.data = src_mesh.copy()
        bpy.context.collection.objects.link(mesh_copy)
        return [mesh_copy]

    return final_meshes

# def split_mesh_by_material_old(mesh_obj):
#     import bmesh
#     bm = bmesh.new()
#     bm.from_mesh(mesh_obj.data)
#     new_meshes = {}
#     for mat in mesh_obj.material_slots:
#         new_mesh = bpy.data.meshes.new(mat.name)
#         new_mesh.materials.append(mat.material)
#         new_bm = bmesh.new()
#         vert_map = {}
#         added_verts = set()
#         for face in bm.faces:
#             if face.material_index == mat.slot_index:
#                 new_face_verts = []
#                 for v in face.verts:
#                     if v.index not in vert_map:
#                         new_v = new_bm.verts.new(v.co)
#                         vert_map[v.index] = new_v
#                         added_verts.add(new_v)
#                     new_face_verts.append(vert_map[v.index])
#                 new_bm.faces.new(new_face_verts)
#         new_bm.to_mesh(new_mesh)
#         new_bm.free()
#         new_mesh_obj = bpy.data.objects.new(mat.name, new_mesh)
#         new_meshes[mat.name] = new_mesh_obj
#     bm.free()
#     return new_meshes

import mathutils
def get_mesh_median(mesh):
    median = mathutils.Vector()
    if not mesh.vertices:
        return median
    for v in mesh.vertices:
        median += v.co
    median /= len(mesh.vertices)
    return median

def calculate_mesh_radius(obj):
    mesh = obj.data
    radius = 0.0
    median = get_mesh_median(mesh)
    for v in mesh.vertices:
        distance = (obj.matrix_world @ v.co - median).length
        if distance > radius:
            radius = distance
    return radius

def get_mesh_radius_and_bounding_box(mesh_object):
    bounding_box = mesh_object.bound_box
    x_coords = [v[0] for v in bounding_box]
    y_coords = [v[1] for v in bounding_box]
    z_coords = [v[2] for v in bounding_box]
    max_point = mathutils.Vector((max(x_coords), max(y_coords), max(z_coords)))
    min_point = mathutils.Vector((min(x_coords), min(y_coords), min(z_coords)))
    generalizedMeshRadius = calculate_mesh_radius(mesh_object)
    return generalizedMeshRadius, [list(min_point), list(max_point)]


def mesh_to_CCollisionShapeConvex(mesh_obj):
    """
    Convert a Blender mesh to a CCollisionShapeConvex object.

    Vertices are transformed to world space since convex shapes store
    actual geometry (not pose matrices like Box/Sphere/Capsule).

    Args:
        mesh_obj: Blender object (bpy.types.Object)

    Returns:
        CCollisionShapeConvex: Instance populated with mesh data
    """
    # Transform vertices from local to world space
    matrix = mesh_obj.matrix_world
    vertices = []
    for v in mesh_obj.data.vertices:
        world_co = matrix @ v.co
        vertices.append([world_co[0], world_co[1], world_co[2], 1.0])
    
    # Extract polygons as a flat list: [vertex_count, idx1, idx2, ..., vertex_count, ...]
    polygons = []
    for poly in mesh_obj.data.polygons:
        polygons.append(len(poly.vertices))  # Number of vertices in the face
        polygons.extend(poly.vertices)       # Vertex indices
    
    # Determine physicalMaterialName
    # If the mesh has materials, use the first material's name; otherwise, set to None
    physicalMaterialName = _get_collision_material_name(mesh_obj)
    
    # Create and populate the collision shape
    shape = CCollisionShapeConvex()
    shape.vertices = vertices
    shape.polygons = polygons
    shape.physicalMaterialName = physicalMaterialName
    
    return shape

def mesh_to_CCollisionShapeTriMesh(mesh_obj):
    """
    Convert a Blender mesh to a CCollisionShapeTriMesh object.
    Assumes the mesh is triangulated (all polygons are triangles).

    Vertices are transformed to world space since tri-mesh shapes store
    actual geometry (not pose matrices like Box/Sphere/Capsule).

    Args:
        mesh_obj: Blender object (bpy.types.Object)

    Returns:
        CCollisionShapeTriMesh: Instance populated with mesh data
    """
    mesh_data = mesh_obj.data
    matrix = mesh_obj.matrix_world

    # Validate that all polygons are triangles
    if any(len(poly.vertices) != 3 for poly in mesh_data.polygons):
        raise ValueError("Mesh must be triangulated for CCollisionShapeTriMesh")

    # Extract triangles as a flat list of vertex indices (3 per triangle)
    triangles = [v for poly in mesh_data.polygons for v in poly.vertices]

    # REDkit serializes trimesh vertices in first-use order from the triangle
    # stream (not Blender's raw mesh vertex index order). Reindex to match.
    used_vertex_order = []
    seen_vertex_indices = set()
    for vert_idx in triangles:
        if vert_idx not in seen_vertex_indices:
            seen_vertex_indices.add(vert_idx)
            used_vertex_order.append(vert_idx)

    old_to_new_index = {old_idx: new_idx for new_idx, old_idx in enumerate(used_vertex_order)}
    triangles = [old_to_new_index[vert_idx] for vert_idx in triangles]

    # Transform only the used vertices to world space, in REDkit-compatible order
    vertices = []
    for old_idx in used_vertex_order:
        world_co = matrix @ mesh_data.vertices[old_idx].co
        vertices.append([world_co[0], world_co[1], world_co[2], 1.0])

    # Extract material indices for each triangle
    physicalMaterialIndexes = [poly.material_index for poly in mesh_data.polygons]

    # Extract all material names from the mesh
    physicalMaterialNames = [mat.name for mat in mesh_data.materials] if mesh_data.materials else []
    if not physicalMaterialNames:
        log.warning(
            f"Collision mesh '{mesh_obj.name}' has no physical material; using '{DEFAULT_PHYSICAL_MATERIAL}'."
        )
        physicalMaterialNames = [DEFAULT_PHYSICAL_MATERIAL]
        physicalMaterialIndexes = [0 for _ in mesh_data.polygons]

    # Create and populate the collision shape
    shape = CCollisionShapeTriMesh()
    shape.vertices = vertices
    shape.triangles = triangles
    shape.physicalMaterialIndexes = physicalMaterialIndexes
    shape.physicalMaterialNames = physicalMaterialNames
    
    return shape

def _get_identity_matrix():
    """Return a 4x4 identity matrix as nested lists."""
    return [
        [1.0, 0.0, 0.0, 0.0],
        [0.0, 1.0, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.0],
        [0.0, 0.0, 0.0, 1.0]
    ]

def _matrix_to_list(matrix):
    """Convert a Blender Matrix to a 4x4 nested list for game file format.

    The import code transposes the file matrix (reading mat_list[col][row]),
    so we store the Blender matrix transposed (columns as rows) to match.
    """
    return [[matrix[row][col] for row in range(4)] for col in range(4)]

def mesh_to_CCollisionShapeBox(mesh_obj):
    """
    Convert a Blender mesh to a CCollisionShapeBox object.

    The box dimensions are derived from the mesh's LOCAL bounding box (without transform).
    The matrix_world stores position/rotation/scale and is applied on import.

    Args:
        mesh_obj: Blender object (bpy.types.Object)

    Returns:
        CCollisionShapeBox: Instance populated with mesh data
    """
    # Use LOCAL bounding box (no transform applied) to get base dimensions
    # Import will apply matrix_world which includes scale
    bbox = mesh_obj.bound_box  # Local coordinates

    # Calculate min/max coordinates from local bbox
    x_coords = [v[0] for v in bbox]
    y_coords = [v[1] for v in bbox]
    z_coords = [v[2] for v in bbox]

    min_x, max_x = min(x_coords), max(x_coords)
    min_y, max_y = min(y_coords), max(y_coords)
    min_z, max_z = min(z_coords), max(z_coords)

    # Calculate half extents from LOCAL dimensions
    half_x = (max_x - min_x) / 2.0
    half_y = (max_y - min_y) / 2.0
    half_z = (max_z - min_z) / 2.0

    # Determine physicalMaterialName
    physicalMaterialName = _get_collision_material_name(mesh_obj)

    # Get world transform as pose matrix (includes position, rotation, scale)
    matrix_world = _matrix_to_list(mesh_obj.matrix_world)

    # Create and populate the collision shape
    shape = CCollisionShapeBox()
    shape.physicalMaterialName = physicalMaterialName
    shape.matrix_world = matrix_world
    shape.halfExtendsX = half_x
    shape.halfExtendsY = half_y
    shape.halfExtendsZ = half_z

    return shape

def mesh_to_CCollisionShapeSphere(mesh_obj):
    """
    Convert a Blender mesh to a CCollisionShapeSphere object.

    The radius is derived from the mesh's LOCAL bounding box (without transform).
    The matrix_world stores position/rotation/scale and is applied on import.

    Args:
        mesh_obj: Blender object (bpy.types.Object)

    Returns:
        CCollisionShapeSphere: Instance populated with mesh data
    """
    # Use LOCAL bounding box (no transform applied) to get base dimensions
    # Import will apply matrix_world which includes scale
    bbox = mesh_obj.bound_box  # Local coordinates

    # Calculate dimensions from local bbox
    x_coords = [v[0] for v in bbox]
    y_coords = [v[1] for v in bbox]
    z_coords = [v[2] for v in bbox]

    dim_x = max(x_coords) - min(x_coords)
    dim_y = max(y_coords) - min(y_coords)
    dim_z = max(z_coords) - min(z_coords)

    # Radius is half the largest LOCAL dimension
    radius = max(dim_x, dim_y, dim_z) / 2.0

    # Determine physicalMaterialName
    physicalMaterialName = _get_collision_material_name(mesh_obj)

    # Get world transform as pose matrix
    matrix_world = _matrix_to_list(mesh_obj.matrix_world)

    # Create and populate the collision shape
    shape = CCollisionShapeSphere()
    shape.physicalMaterialName = physicalMaterialName
    shape.matrix_world = matrix_world
    shape.radius = radius

    return shape

def mesh_to_CCollisionShapeCapsule(mesh_obj):
    """
    Convert a Blender mesh to a CCollisionShapeCapsule object.

    The exported primitive uses the game convention (capsule axis = local X).
    This exporter auto-detects which local axis the Blender mesh is elongated on,
    derives radius/height from that axis, and rotates the exported pose matrix so
    the engine capsule matches the Blender mesh orientation.

    The matrix_world stores position/rotation/scale and is applied on import.

    Args:
        mesh_obj: Blender object (bpy.types.Object)

    Returns:
        CCollisionShapeCapsule: Instance populated with mesh data
    """
    # Use LOCAL bounding box (no transform applied) to get base dimensions
    # Import will apply matrix_world which includes scale
    bbox = mesh_obj.bound_box  # Local coordinates

    # Calculate dimensions from local bbox
    x_coords = [v[0] for v in bbox]
    y_coords = [v[1] for v in bbox]
    z_coords = [v[2] for v in bbox]

    dim_x = max(x_coords) - min(x_coords)
    dim_y = max(y_coords) - min(y_coords)
    dim_z = max(z_coords) - min(z_coords)

    dims = {'X': dim_x, 'Y': dim_y, 'Z': dim_z}
    axis_name, axis_length = max(dims.items(), key=lambda item: item[1])
    cross_dims = [length for name, length in dims.items() if name != axis_name]

    # Radius from the two dimensions perpendicular to the capsule axis.
    radius = max(cross_dims) / 2.0 if cross_dims else (axis_length / 2.0)

    # Height is center-to-center distance along the detected capsule axis.
    height = max(0.0, axis_length - 2.0 * radius)

    # Determine physicalMaterialName
    physicalMaterialName = _get_collision_material_name(mesh_obj)

    # The engine expects capsule primitives aligned to local X.
    # If the Blender mesh is authored along local Y/Z, rotate the exported
    # pose so the game primitive points along the same world-space direction.
    pose_matrix = mesh_obj.matrix_world.copy()
    if axis_name == 'Y':
        pose_matrix = pose_matrix @ Matrix.Rotation(math.radians(90.0), 4, 'Z')
    elif axis_name == 'Z':
        pose_matrix = pose_matrix @ Matrix.Rotation(math.radians(90.0), 4, 'Y')

    matrix_world = _matrix_to_list(pose_matrix)

    # Create and populate the collision shape
    shape = CCollisionShapeCapsule()
    shape.physicalMaterialName = physicalMaterialName
    shape.matrix_world = matrix_world
    shape.radius = radius
    shape.height = height

    return shape

class MeshExporter(object):
    """docstring for MeshExporter."""
    def __init__(self):
        super(MeshExporter, self).__init__()
        self.cr2w = None
        self.bone_data = BoneData() # create empty bone data as default for static meshes
        self.__armature = None
        self.__meshes = None

    def __loadMeshData(self, meshObj, bone_map, original_mesh_obj=None):
        bl_mesh = meshObj.data

        # Export from a temporary mesh copy so export-time calculations never
        # modify the user's original mesh data.
        mesh_for_work = bl_mesh.copy()
        try:
            has_excess_weights = any(len(vertex.groups) > 4 for vertex in mesh_for_work.vertices)
            if has_excess_weights:
                log.warning(
                    f"Mesh '{meshObj.name}' has vertices with more than 4 weights; "
                    "export keeps the strongest 4 influences per vertex without modifying the source mesh."
                )

            exportMeshdata = get_mesh_info(mesh_for_work, meshObj)
            exportMaterialdata = get_mesh_material_info(mesh_for_work, mesh_obj=original_mesh_obj or meshObj)
            return (exportMeshdata, exportMaterialdata)
        finally:
            bpy.data.meshes.remove(mesh_for_work)

    def execute(self, filePath, **args):
        self.filePath = filePath
        self.__meshes = sorted(args.get('meshes', []), key=lambda x: x.name)
        export_col_tri = args.get('export_col_tri', False)
        self.col_mesh_data = []
        if export_col_tri:
            col_tri_meshes = args.get('col_tri_meshes', [])  # Now a list of collision meshes
            for col_mesh_obj in col_tri_meshes:
                col_type = get_collision_type(col_mesh_obj.name)
                if col_type == "_box":
                    self.col_mesh_data.append(mesh_to_CCollisionShapeBox(col_mesh_obj))
                elif col_type == "_sphere":
                    self.col_mesh_data.append(mesh_to_CCollisionShapeSphere(col_mesh_obj))
                elif col_type == "_capsule":
                    self.col_mesh_data.append(mesh_to_CCollisionShapeCapsule(col_mesh_obj))
                elif col_type == "_col":
                    self.col_mesh_data.append(mesh_to_CCollisionShapeConvex(col_mesh_obj))
                elif col_type == "_tri":
                    self.col_mesh_data.append(mesh_to_CCollisionShapeTriMesh(col_mesh_obj))

        if not self.__meshes:
            raise ValueError("No meshes provided for export")

        skinned_meshes = [mesh.name for mesh in self.__meshes if _mesh_requires_skinning(mesh)]
        static_meshes = [mesh.name for mesh in self.__meshes if not _mesh_requires_skinning(mesh)]
        if skinned_meshes and static_meshes:
            raise ValueError(
                "Selected meshes mix skinned and static export data. "
                "Export matching mesh types separately."
            )
        requires_skinning = bool(skinned_meshes)

        explicit_armature = args.get('armature', None)
        detected_armature = explicit_armature if explicit_armature and explicit_armature.type == 'ARMATURE' else None
        if detected_armature is None:
            linked_armatures = {}
            for mesh in self.__meshes:
                parent = getattr(mesh, "parent", None)
                if parent and getattr(parent, "type", None) == 'ARMATURE':
                    linked_armatures.setdefault(parent.name_full, parent)
                for modifier in getattr(mesh, "modifiers", []):
                    armature_obj = getattr(modifier, "object", None)
                    if modifier.type == 'ARMATURE' and armature_obj and getattr(armature_obj, "type", None) == 'ARMATURE':
                        linked_armatures.setdefault(armature_obj.name_full, armature_obj)
            if requires_skinning and len(linked_armatures) > 1:
                names = ", ".join(sorted(linked_armatures.keys()))
                raise ValueError(
                    f"Selected meshes reference multiple armatures ({names}). "
                    "Select one armature or export matching meshes separately."
                )
            if len(linked_armatures) == 1:
                detected_armature = next(iter(linked_armatures.values()))

        self.__armature = detected_armature if requires_skinning else None
        if requires_skinning and self.__armature is None:
            mesh_list = ", ".join(skinned_meshes[:3])
            if len(skinned_meshes) > 3:
                mesh_list += ", ..."
            raise ValueError(
                "Selected meshes contain skinning data but no armature was selected or linked. "
                f"Attach/select the rig before export ({mesh_list})."
            )
        if self.__armature and explicit_armature is None:
            log.info(
                "Auto-detected armature '%s' from mesh parent/modifier for export.",
                self.__armature.name,
            )

        if requires_skinning:
            rot90_override = args.get('rotate_bones_90', None)
            if rot90_override is None:
                rig_settings = getattr(self.__armature.data, "witcherui_RigSettings", None)
                rot90 = get_rig_rot90_enabled(rig_settings, default=False)
            else:
                rot90 = bool(rot90_override)
            self.bone_data = extract_bone_data(self.__armature, rotate_bones_90=rot90)
            vert_group_info = get_vertex_group_info(self.__armature, self.__meshes[0])
            self.bone_data.Block3 = vert_group_info
            group_names = [
                group.name
                for group in self.__meshes[0].vertex_groups
                if group.name != ZERO_WEIGHT_MASK_GROUP_NAME
            ]
            self.bone_data.BoneIndecesMappingBoneIndex = convert_to_index_values(group_names, self.bone_data.jointNames)
            # Block3:[]
            # BoneIndecesMappingBoneIndex:[]
        else:
            self.bone_data = BoneData()
        is_static = not requires_skinning
        nameMap = [] #self.__exportBones(meshes)
        
        #Note the mesh radius on vanilla w2mesh is calculated for all lods together.
        rad_box = get_mesh_radius_and_bounding_box(self.__meshes[0])
        
        #class Common_Info
        lod0_settings = self.__meshes[0].witcherui_MeshSettings
        common_info = {
            'generalizedMeshRadius' : rad_box[0],
            'boundingBox' : rad_box[1],
            'isStatic' : is_static,
            'lod0_MeshSettings' : lod0_settings
        }
        if lod0_settings.soundInfo_enabled:
            bone_mapping = lod0_settings.soundInfo_soundBoneMappingInfo
            size_id = lod0_settings.soundInfo_soundSizeIdentification
            common_info['soundInfo'] = {
                'enabled': True,
                'soundTypeIdentification': lod0_settings.soundInfo_soundTypeIdentification,
                'soundSizeIdentification': '' if size_id == 'default' else size_id,
                'soundBoneMappingInfo': '' if bone_mapping == 'NONE' else bone_mapping,
            }
        #generalizedMeshRadius
        #boundingBox

        # MESH STUFF
        #todo chunks are stored in reversed sort order by faces
        ALL_LODS = []
        _temp_meshes = []  # Track all temporary mesh objects for cleanup
        try:
            for m in self.__meshes:
                new_meshes = split_mesh_by_material(m)
                _temp_meshes.extend(new_meshes)
                mesh_data = [self.__loadMeshData(i, nameMap, original_mesh_obj=m) for i in new_meshes]
                # Data extracted — remove temp meshes for this LOD
                for mesh in new_meshes:
                    if mesh.name in bpy.data.objects:
                        mesh_data_block = mesh.data
                        bpy.data.objects.remove(mesh, do_unlink=True)
                        if mesh_data_block and mesh_data_block.users == 0:
                            bpy.data.meshes.remove(mesh_data_block)
                _temp_meshes.clear()

                ALL_LODS.append([mesh_data, m.witcherui_MeshSettings])

            self.cr2w = mesh_builder.BuildMesh(ALL_LODS, self.bone_data, common_info, self.col_mesh_data,
                                              strip_material_names=args.get('strip_material_names', False))

            if args.get('use_native_writer', False):
                cr2w_writer.write_w2mesh(self.cr2w, filePath)
            else:
                self.__save_json(filePath, args.get('keep_intermediate_json', False))
        finally:
            # Clean up any leftover temporary meshes (e.g. on error)
            for obj in _temp_meshes:
                try:
                    if obj and obj.name in bpy.data.objects:
                        mesh_data_block = obj.data
                        bpy.data.objects.remove(obj, do_unlink=True)
                        if mesh_data_block and mesh_data_block.users == 0:
                            bpy.data.meshes.remove(mesh_data_block)
                except Exception:
                    pass
        
    def __save_json(self, filePath, keep_intermediate_json=False):
        import uuid
        json_data = self.cr2w.GetJson()
        savePath = Path(filePath)
        
        # Generate unique temp JSON filename to avoid conflicts with existing files
        # Format: .tmp_filename_UUID.w2mesh.json (hidden temp file)
        unique_id = uuid.uuid4().hex[:8]
        base_name = savePath.stem  # filename without extension
        parent_dir = savePath.parent
        
        # Create temp JSON path with unique ID - prefixed with .tmp_ to be clearly temporary
        temp_json_name = f".tmp_{base_name}_{unique_id}.w2mesh.json"
        temp_json_path = parent_dir / temp_json_name
        
        conversion_success = False
        
        try:
            # Write the JSON file
            with open(temp_json_path, "w") as file:
                file.write(json.dumps(json_data, indent=2, default=vars, sort_keys=False))
            
            log.info(f"Created intermediate JSON: {temp_json_path}")
            
            # Convert JSON to w2mesh using WolvenKit
            WolvenKit = Path(get_wolvenkit(bpy.context))
            if WolvenKit.exists():
                import subprocess
                command = [str(WolvenKit), "--input", str(temp_json_path), "--json2cr2w"]
                result = subprocess.run(command, capture_output=True, text=True, timeout=120)
                
                if result.returncode != 0:
                    error_msg = f"WolvenKit conversion failed (exit code {result.returncode}): {result.stderr}"
                    log.error(error_msg)
                    raise RuntimeError(error_msg)
                else:
                    # WolvenKit creates the w2mesh file by removing .json extension
                    # The output will be: .tmp_filename_UUID.w2mesh
                    converted_path = parent_dir / f".tmp_{base_name}_{unique_id}.w2mesh"
                    final_path = savePath  # This is already the target .w2mesh path
                    
                    # Rename from temp name to final name
                    if converted_path.exists():
                        # If target already exists, remove it first
                        if final_path.exists():
                            final_path.unlink()
                        converted_path.rename(final_path)
                        log.info(f"Created w2mesh: {final_path}")
                        conversion_success = True
            else:
                error_msg = 'WolvenKit CLI .exe not found. JSON file created but not converted to w2mesh. Check addon preferences.'
                log.critical(error_msg)
                raise FileNotFoundError(error_msg)
        
        finally:
            # Always clean up temp JSON unless user wants to keep it
            if keep_intermediate_json and conversion_success:
                # If keeping, rename to final name pattern (without unique ID and .tmp_ prefix)
                final_json_path = parent_dir / f"{base_name}.w2mesh.json"
                if final_json_path.exists():
                    # Don't overwrite existing JSON - keep temp name but remove .tmp_ prefix
                    kept_json_path = parent_dir / f"{base_name}_{unique_id}.w2mesh.json"
                    if temp_json_path.exists():
                        temp_json_path.rename(kept_json_path)
                        log.info(f"Existing JSON found, saved intermediate JSON as: {kept_json_path}")
                else:
                    if temp_json_path.exists():
                        temp_json_path.rename(final_json_path)
                        log.info(f"Saved intermediate JSON as: {final_json_path}")
            else:
                # Delete the temp JSON
                if temp_json_path.exists():
                    temp_json_path.unlink()
                    log.info(f"Deleted intermediate JSON: {temp_json_path}")

def do_export_mesh(context, filePath, **kwargs):
    log.info("--------------------EXPORTING MESH------------------------")
    start_time = time.time()
    exporter = MeshExporter()
    exporter.execute(filePath, **kwargs)
