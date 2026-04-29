import logging
import os
import struct
try:
    from ..CR2W import bStream
    from ..CR2W.dc_entity import CCollisionShapeConvex, CCollisionShapeTriMesh, CCollisionShapeBox, CCollisionShapeCapsule, CCollisionShapeSphere
    import mathutils
except Exception as e:
    from ..CR2W import bStream
    from ..CR2W.dc_entity import CCollisionShapeConvex, CCollisionShapeTriMesh, CCollisionShapeBox, CCollisionShapeCapsule, CCollisionShapeSphere
import bmesh
import bpy
from pathlib import Path
log = logging.getLogger(__name__)


# Raw CSV data (simplified to just what we need)
csv_data = '''
MaterialName;DebugColor
carpet;RED
dirt_hard;BROWN
dirt_soil;BROWN
grass_long;GREEN1
grass_short;GREEN
gravel_large;BLUE
gravel_small;BLUE
ice_debris;BLUE
ice_solid;BLUE
ice_thin;BLUE
leaves;ORANGE
metal;GRAY
mud;BROWN
sand;YELLOW
snow_deep;WHITE
snow_firm;WHITE
stone_debris;BLACK
stone_solid;BLACK
swamp;GREEN
water_deep;BLUE
water_puddle;BLUE
water_shallow;BLUE
wood_hollow;BROWN
wood_debris;BROWN
wood_solid;BROWN
rubber;BROWN
glass;BLUE
iron;GRAY
flesh;RED
custom_sword;GRAY
TEST;BLUE
clay_tile;RED
sand_wet;YELLOW
mud_dry;BROWN
hay;ORANGE
metal_spoons;GRAY
water_deep_river;BLUE
dettlaff_flesh;RED
gold_coins;YELLOW
'''

# Parse the CSV into a dictionary
import csv
from io import StringIO

reader = csv.DictReader(StringIO(csv_data.strip()), delimiter=';')
material_colors = {row["MaterialName"]: row["DebugColor"] for row in reader}

# DebugColor to RGB mapping
color_map = {
    "RED": (1.0, 0.0, 0.0, 1),
    "GREEN": (0.0, 1.0, 0.0, 1),
    "GREEN1": (0.0, 1.0, 0.5, 1),
    "BROWN": (0.5, 0.25, 0.0, 1),
    "YELLOW": (1.0, 1.0, 0.0, 1),
    "BLUE": (0.0, 0.3, 1.0, 1),
    "ORANGE": (1.0, 0.5, 0.0, 1),
    "GRAY": (0.3, 0.3, 0.3, 1),
    "BLACK": (0.0, 0.0, 0.0, 1),
    "WHITE": (1.0, 1.0, 1.0, 1),
}


class FileFormatException(Exception):
    pass

class Vector3:
    def __init__(self, x=0.0, y=0.0, z=0.0):
        self.x = x
        self.y = y
        self.z = z

class Vector4:
    def __init__(self, x=0.0, y=0.0, z=0.0, w=0.0):
        self.x = x
        self.y = y
        self.z = z
        self.w = w

class MeshCacheEntry:
    """MESH (Triangle Mesh) cache entry from PhysX NXS format.

    Attributes:
        vertices (list): List of Vector3 vertex positions
        faces (list): List of triangle index triplets
        materials (list): Per-face material indices (when flags & 0x01)
        centerOfMass (Vector3): Center of mass position
        mass (float): Total mass value
        flags (int): Feature flags - bit 0: materials, bit 3: 16-bit indices
        uk1 (int): BVH tree parameter (after BV4 marker)
        uk2 (int): BVH build flags
        uk3 (list): 6 floats - BVH AABB bounds
        uvs (list): UV coordinate data
        uk4 (int): Extra data count or reserved
        bounds (dict): AABB with 'min' and 'max' Vector3
        faceFlags (list): Per-face byte flags
    """
    def __init__(self):
        self.vertices = []
        self.faces = []
        self.materials = []
        self.centerOfMass = Vector3()
        self.mass = 0.0
        self.flags = 0
        self.uk1 = 0
        self.uk2 = 0
        self.uk3 = []
        self.uvs = []
        self.uk4 = 0
        self.bounds = {'min': Vector3(), 'max': Vector3()}
        self.faceFlags = []


class CVXMCacheEntry:
    """CVXM (Convex Hull) cache entry from PhysX NXS format.

    Attributes:
        vertices (list): List of Vector3 vertex positions
        faceData (list): Per-face metadata (normal, vertex count, etc.)
        faces (list): List of face vertex index lists
        edges (list): Edge pairs for hull optimization
        triangles (list): Triangle data for rendering
        bounds (dict): AABB with 'min' and 'max' Vector3
        mass (float): Total mass value
        hashes (list): 6 UInt64 values - material/mesh identifiers
        uk1 (float): Parameter (possibly mass-related or scaling factor).
                     If uk1 < 0: transformation mode (old format), uk2 is populated.
        uk1b (float): Sentinel flag (new format). Only populated when uk1 >= 0.
                      If uk1b == -1.0: transformation mode, uk2 is populated.
                      Otherwise: adjacency mode, uk3 is populated.
        uk2 (Vector4): Transformation quaternion or plane (when in transformation mode)
        uk3 (list): Byte pairs for adjacency optimization (when in adjacency mode)
    """
    def __init__(self):
        self.vertices = []
        self.faceData = []
        self.faces = []
        self.edges = []
        self.triangles = []
        self.bounds = {'min': Vector3(), 'max': Vector3()}
        self.mass = 0.0
        self.hashes = []
        self.uk1 = 0.0
        self.uk1b = 0.0  # The actual conditional flag
        self.uk2 = Vector4()
        self.uk3 = []

class CCollisionShapeConvexNXS(CCollisionShapeConvex):
    def __init__(self, entry:CVXMCacheEntry):
        self.physicalMaterialName = "nxs_material"
        self.vertices = []
        self.polygons = []
        self.matrix_world = _identity_matrix_list()

        try:
            for face in entry.faces:
                self.polygons.append(len(face))
                self.polygons+=face
            for vert in entry.vertices:
                self.vertices.append([vert.x,vert.y,vert.z,1.0])
        except Exception as e:
            log.error('Could not get CCollisionShapeConvex')

class CCollisionShapeTriMeshNXS(CCollisionShapeTriMesh):
    def __init__(self, entry:MeshCacheEntry):
        self.physicalMaterialNames = []
        self.vertices = []
        self.triangles = []
        self.physicalMaterialIndexes = []
        self.matrix_world = _identity_matrix_list()
        try:
            max_material_index = max((int(num) for num in entry.materials), default=0)
            self.physicalMaterialNames = [f"nxs_Material_{num}" for num in range(max_material_index + 1)]
            for vert in entry.vertices:
                self.vertices.append([vert.x,vert.y,vert.z,1.0])
            for face in entry.faces:
                self.triangles+=face
            self.physicalMaterialIndexes = entry.materials
        except Exception as e:
            log.error('Could not get CCollisionShapeTriMesh')


def createCol(shape_:CCollisionShapeConvex, mesh_name:str = "Custom"):
    def _normalize_scalar_list(value, field_name):
        if value is None:
            return []
        if isinstance(value, (list, tuple)):
            return [int(v) for v in value]
        if hasattr(value, "value"):
            return _normalize_scalar_list(value.value, field_name)
        raise TypeError(
            f"CONVEX collision '{mesh_name}' expected parsed list for {field_name}, "
            f"got {type(value).__name__}."
        )

    def _normalize_vertices(value):
        if value is None:
            return []
        if isinstance(value, (list, tuple)):
            normalized = []
            for entry in value:
                if isinstance(entry, (list, tuple)):
                    coords = list(entry)
                elif hasattr(entry, "x") and hasattr(entry, "y") and hasattr(entry, "z"):
                    coords = [entry.x, entry.y, entry.z, getattr(entry, "w", 1.0)]
                else:
                    raise TypeError(
                        f"CONVEX collision '{mesh_name}' has invalid vertex entry type "
                        f"{type(entry).__name__}."
                    )
                if len(coords) < 3:
                    raise ValueError(f"CONVEX collision '{mesh_name}' has invalid vertex entry: {coords!r}")
                if len(coords) < 4:
                    coords.append(1.0)
                normalized.append(coords)
            return normalized
        if hasattr(value, "More"):
            parsed = []
            for verts in value.More:
                if hasattr(verts, "MoreProps"):
                    coords = []
                    for prop in verts.MoreProps[:4]:
                        scalar = getattr(prop, "Value", getattr(prop, "value", None))
                        if scalar is not None:
                            coords.append(scalar)
                    if len(coords) >= 3:
                        if len(coords) < 4:
                            coords.append(1.0)
                        parsed.append(coords)
            return parsed
        if hasattr(value, "value"):
            return _normalize_vertices(value.value)
        raise TypeError(
            f"CONVEX collision '{mesh_name}' expected parsed vertex list, "
            f"got {type(value).__name__}."
        )

    material_name = getattr(shape_, "physicalMaterialName", None) or "nxs_material"
    polygons = _normalize_scalar_list(getattr(shape_, "polygons", None), "polygons")
    vertices = _normalize_vertices(getattr(shape_, "vertices", None))
    if not vertices or not polygons:
        raise ValueError(f"CONVEX collision '{mesh_name}' is missing parsed polygon or vertex data")

    mesh = bpy.data.meshes.new(mesh_name+'_col')
    bm = bmesh.new()
    for v in vertices:
        bm.verts.new(v[:3])  # Use only the first three components

    bm.verts.ensure_lookup_table()

    i = 0
    while i < len(polygons):
        face_vertex_count = polygons[i]
        if face_vertex_count < 3:
            i += 1 + max(face_vertex_count, 0)
            continue
        try:
            face_verts = [bm.verts[polygons[j]] for j in range(i + 1, i + 1 + face_vertex_count)]
            bm.faces.new(face_verts)
        except (ValueError, IndexError):
            pass
        i += 1 + face_vertex_count

    bm.to_mesh(mesh)
    bm.free()

    material = bpy.data.materials.get(material_name)
    if material is None:
        material = bpy.data.materials.new(name=material_name)
    mesh.materials.append(material)
   
    # Use Nodes (required for assigning Base Color)
    material.use_nodes = True
    bsdf = material.node_tree.nodes.get("Principled BSDF")

    # Assign color if DebugColor exists
    debug_color_name = material_colors.get(material_name)
    if debug_color_name:
        rgba = color_map.get(debug_color_name.upper())
        if rgba and bsdf:
            bsdf.inputs["Base Color"].default_value = rgba
            bsdf.inputs["Alpha"].default_value = 0.5
            log.debug("Assigned color %s (%s) to material %s", debug_color_name, rgba, material_name)
        else:
            log.debug("Unknown color name: %s", debug_color_name)
    else:
        log.debug("No debug color found for material: %s", material_name)




    obj = bpy.data.objects.new(mesh_name+'_col', mesh)

    bpy.context.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    #obj.select_set(True)
    return obj


import bpy
import bmesh
from mathutils import Matrix

def _apply_collision_shape_pose(obj, shape_, obj_name):
    """Apply the cached local collision pose stored in RED collision data."""
    if hasattr(shape_, 'matrix_world') and shape_.matrix_world:
        mat_list = shape_.matrix_world
        mat = Matrix([
            [mat_list[0][0], mat_list[1][0], mat_list[2][0], mat_list[3][0]],
            [mat_list[0][1], mat_list[1][1], mat_list[2][1], mat_list[3][1]],
            [mat_list[0][2], mat_list[1][2], mat_list[2][2], mat_list[3][2]],
            [mat_list[0][3], mat_list[1][3], mat_list[2][3], mat_list[3][3]],
        ])
        obj.matrix_world = mat
    else:
        log.warning("No valid matrix_world for %s", obj_name)


def _setup_collision_object(mesh, obj_name, material_name, shape_):
    """Common setup: material, debug color, linking, display settings"""
    # Material handling
    material = bpy.data.materials.get(material_name)
    if material is None:
        material = bpy.data.materials.new(name=material_name)
    
    mesh.materials.append(material)
    
    # Debug color assignment
    material.use_nodes = True
    bsdf = material.node_tree.nodes.get("Principled BSDF")
    
    debug_color_name = material_colors.get(material_name)
    if debug_color_name and bsdf:
        rgba = color_map.get(debug_color_name.upper())
        if rgba:
            bsdf.inputs["Base Color"].default_value = rgba
            bsdf.inputs["Alpha"].default_value = 0.5
            log.debug("Assigned color %s (%s) to material %s", debug_color_name, rgba, material_name)
        else:
            log.debug("Unknown color name: %s", debug_color_name)
    else:
        log.debug("No debug color found for material: %s", material_name)
    
    # Create and link object
    obj = bpy.data.objects.new(obj_name, mesh)
    bpy.context.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    
    # Debug display
    obj.display_type = 'WIRE'
    obj.show_in_front = True
    
    # Apply pose matrix if available
    _apply_collision_shape_pose(obj, shape_, obj_name)
    
    return obj

def createBox(shape_: CCollisionShapeBox, mesh_name: str = "Custom"):
    material_name = shape_.physicalMaterialName or "DefaultCollisionMaterial"
    mesh_name_full = mesh_name + '_box'
    
    mesh = bpy.data.meshes.new(mesh_name_full)
    
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=2.0)  # default cube: half-extents = 1
    
    # Scale to actual half extents
    half_x = getattr(shape_, 'halfExtendsX', 1.0)
    half_y = getattr(shape_, 'halfExtendsY', 1.0)
    half_z = getattr(shape_, 'halfExtendsZ', 1.0)
    
    scale_mat = (Matrix.Scale(half_x, 4, (1, 0, 0)) @
                 Matrix.Scale(half_y, 4, (0, 1, 0)) @
                 Matrix.Scale(half_z, 4, (0, 0, 1)))
    
    for v in bm.verts:
        v.co = scale_mat @ v.co
    
    bm.normal_update()
    bm.to_mesh(mesh)
    bm.free()
    
    return _setup_collision_object(mesh, mesh_name_full, material_name, shape_)

def createSphere(shape_: CCollisionShapeSphere, mesh_name: str = "Custom"):
    material_name = shape_.physicalMaterialName or "DefaultCollisionMaterial"
    mesh_name_full = mesh_name + '_sphere'
    
    radius = getattr(shape_, 'radius', 1.0)
    
    mesh = bpy.data.meshes.new(mesh_name_full)
    
    bm = bmesh.new()
    # Match the collision-panel sphere collider default resolution (segments=16).
    bmesh.ops.create_uvsphere(bm, u_segments=16, v_segments=8, radius=radius)
    
    bm.normal_update()
    bm.to_mesh(mesh)
    bm.free()
    
    return _setup_collision_object(mesh, mesh_name_full, material_name, shape_)

from math import pi
from mathutils import Matrix

def createCapsule(shape_, mesh_name: str = "Custom"):
    material_name = shape_.physicalMaterialName or "DefaultCollisionMaterial"
    mesh_name_full = mesh_name + "_capsule"

    # Physics capsule semantics:
    # height = center-to-center distance
    radius = float(shape_.radius)
    height = float(shape_.height)

    half_cyl = height * 0.5

    mesh = bpy.data.meshes.new(mesh_name_full)
    bm = bmesh.new()

    # Base sphere
    bmesh.ops.create_uvsphere(
        bm,
        u_segments=16,
        v_segments=8,
        radius=radius
    )

    # Stretch into capsule along Z
    for v in bm.verts:
        if v.co.z > 0.0:
            v.co.z += half_cyl
        elif v.co.z < 0.0:
            v.co.z -= half_cyl

    # 🔑 Rotate geometry from Blender Z → Engine X
    rot = Matrix.Rotation(-pi / 2.0, 4, 'Y')
    bmesh.ops.transform(bm, matrix=rot, verts=bm.verts)

    bm.normal_update()
    bm.to_mesh(mesh)
    bm.free()

    return _setup_collision_object(mesh, mesh_name_full, material_name, shape_)

def createTri(shape_:CCollisionShapeTriMesh, mesh_name:str = "Custom"):
    def _require_list(value, field_name):
        if value is None:
            return []
        if isinstance(value, (list, tuple)):
            return list(value)
        raise TypeError(
            f"TRI collision '{mesh_name}' expected parsed list for {field_name}, "
            f"got {type(value).__name__}. Parser failed to decode the file."
        )

    triangles = [int(v) for v in _require_list(shape_.triangles, "triangles")]
    vertices = [list(v) for v in _require_list(shape_.vertices, "vertices")]
    physicalMaterialIndexes = [int(v) for v in _require_list(shape_.physicalMaterialIndexes, "physicalMaterialIndexes")]
    physicalMaterialNames = _require_list(shape_.physicalMaterialNames, "physicalMaterialNames")
    if any(not isinstance(name, str) or not name for name in physicalMaterialNames):
        raise TypeError(f"TRI collision '{mesh_name}' has invalid physicalMaterialNames data")

    max_index = max((idx for idx in physicalMaterialIndexes if idx >= 0), default=0)

    if not physicalMaterialNames:
        # Empty TRI material-name array is treated as implicit default material.
        # If cooked PhysX data has sparse material indices, keep the geometry
        # importable by synthesizing placeholder slots for visualization.
        physicalMaterialNames = ["default"] + [
            f"nxs_Material_{idx}" for idx in range(1, max_index + 1)
        ]

    if not physicalMaterialIndexes and triangles:
        raise ValueError(f"TRI collision '{mesh_name}' has triangles but no physicalMaterialIndexes")

    if len(physicalMaterialNames) <= max_index:
        old_count = len(physicalMaterialNames)
        physicalMaterialNames.extend(
            f"nxs_Material_{idx}" for idx in range(old_count, max_index + 1)
        )
        log.warning(
            "TRI collision '%s' material index references %d slot(s) but file contains only %d name(s); added placeholder slots",
            mesh_name,
            max_index + 1,
            old_count,
        )

    mesh = bpy.data.meshes.new(mesh_name+'_tri')
    obj = bpy.data.objects.new(mesh_name+'_tri', mesh)
    bpy.context.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    #obj.select_set(True)

    bm = bmesh.new()
    for v in vertices:
        if len(v) < 3:
            raise ValueError(f"TRI collision '{mesh_name}' has invalid vertex entry: {v!r}")
        bm.verts.new(v[:3])
    bm.verts.ensure_lookup_table()
    for i in range(0, len(triangles), 3):
        if i + 2 >= len(triangles):
            break
        try:
            bm.faces.new([bm.verts[triangles[i]], bm.verts[triangles[i+1]], bm.verts[triangles[i+2]]])
        except (ValueError, IndexError):
            # Skip invalid/duplicate faces in malformed collision meshes.
            continue
    bm.to_mesh(mesh)
    bm.free()
    for material_name in physicalMaterialNames:
        material = bpy.data.materials.get(material_name)
        if material is None:
            material = bpy.data.materials.new(name=material_name)
        material.use_nodes = True
        bsdf = material.node_tree.nodes.get("Principled BSDF") if material.node_tree else None
        debug_color_name = material_colors.get(material_name)
        if debug_color_name and bsdf:
            rgba = color_map.get(debug_color_name.upper())
            if rgba:
                bsdf.inputs["Base Color"].default_value = rgba
                bsdf.inputs["Alpha"].default_value = 0.5
        mesh.materials.append(material)

    slot_count = len(mesh.materials)
    for i, polygon in enumerate(mesh.polygons):
        src_index = physicalMaterialIndexes[i] if i < len(physicalMaterialIndexes) else 0
        if src_index < 0:
            src_index = 0
        if slot_count:
            polygon.material_index = min(src_index, slot_count - 1)

    obj.display_type = 'WIRE'
    obj.show_in_front = True
    _apply_collision_shape_pose(obj, shape_, mesh_name+'_tri')
    return obj

def _read_cvxm_entry(f: bStream) -> CVXMCacheEntry:
    """Read a single CVXM (Convex Hull) entry from the stream.

    Assumes the stream is positioned after the NXS magic, at the CVXM marker.
    """
    if f.readUInt32() != 1297634883:  # CVXM
        return None
    version = f.readUInt32()
    if version != 13:
        raise FileFormatException(f"Unsupported CVXM version: {version}")
    f.readUInt32()  # Skip 4 bytes
    if f.readUInt32() != 21316425:  # ICE
        return None
    if f.readUInt32() != 1279806531:  # CLHL
        return None
    f.readUInt32()  # Skip 4 bytes
    if f.readUInt32() != 21316425:  # ICE
        return None
    if f.readUInt32() != 1279809091:  # CVHL
        return None

    convHullVersion = f.readUInt32()
    entry = CVXMCacheEntry()
    vertex_count = f.readUInt32()
    edge_count = f.readUInt32()
    face_data_count = f.readUInt32()
    uk_count = f.readUInt32()  # Total polygon indices count

    for _ in range(vertex_count):
        entry.vertices.append(Vector3(
            x=f.readFloat(),
            y=f.readFloat(),
            z=f.readFloat()
        ))

    for _ in range(face_data_count):
        normal = Vector3(
            x=f.readFloat(),
            y=f.readFloat(),
            z=f.readFloat()
        )
        uk1 = f.readFloat()
        bits = f.readUInt16()
        uk2 = (bits & 0b1100000000000000) >> 14
        face_id = bits & 0b0011111111111111
        vertex_count_in_face = f.readByte()
        uk3 = f.readByte()
        faceData = {'normal': normal, 'uk1': uk1, 'uk2': uk2, 'id': face_id, 'vertexCount': vertex_count_in_face, 'uk3': uk3}
        entry.faceData.append(faceData)

    for face_data in entry.faceData:
        ra = []
        for _ in range(face_data['vertexCount']):
            ra.append(f.readByte())
        entry.faces.append(ra)

    for _ in range(edge_count):
        ra = [f.readByte(), f.readByte()]
        entry.edges.append(ra)

    for _ in range(vertex_count):
        ra = [f.readByte(), f.readByte(), f.readByte()]
        entry.triangles.append(ra)

    entry.bounds['min'] = Vector3(x=f.readFloat(), y=f.readFloat(), z=f.readFloat())
    entry.bounds['max'] = Vector3(x=f.readFloat(), y=f.readFloat(), z=f.readFloat())

    entry.mass = f.readFloat()

    for _ in range(6):
        entry.hashes.append(f.readUInt64())

    # Optional tail metadata varies between CVXM variants. Older files use a
    # transform/adjacency block here; newer files may store ICE-tagged sections
    # (e.g. SUPM/GAUS) after the convex data. Geometry is already parsed above,
    # so tail parsing must be best-effort and never break import.
    try:
        entry.uk1 = f.readFloat()
        if entry.uk1 is None:
            return entry

        if entry.uk1 < 0:
            # Transformation mode (old format): uk1 is negative, read Vector4
            entry.uk2 = Vector4(
                x=f.readFloat(),
                y=f.readFloat(),
                z=f.readFloat(),
                w=f.readFloat()
            )
        else:
            # Check for -1.0 sentinel (new format)
            entry.uk1b = f.readFloat()
            if entry.uk1b is None:
                return entry

            if entry.uk1b == -1.0:
                # Transformation mode with explicit sentinel
                entry.uk2 = Vector4(
                    x=f.readFloat(),
                    y=f.readFloat(),
                    z=f.readFloat(),
                    w=f.readFloat()
                )
            else:
                # Some CVXM variants continue with ICE-tagged metadata blocks
                # instead of adjacency pairs. Detect and ignore them.
                next4 = f.read(4)
                if len(next4) == 4:
                    f.seek(-4, os.SEEK_CUR)
                if next4 == b'ICE\x01':
                    return entry

                # Adjacency mode - rewind and read uk1b position as count
                f.seek(-4, os.SEEK_CUR)
                uk3_count = f.readUInt32()

                # Guard against false positives (e.g. float 1.0 => 1065353216)
                cur = f.tell()
                f.seek(0, os.SEEK_END)
                end = f.tell()
                f.seek(cur, os.SEEK_SET)
                remaining = max(0, end - cur)
                if uk3_count is None or uk3_count * 2 > remaining:
                    log.debug(
                        "CVXM tail adjacency count looks invalid (%s with %d bytes remaining); "
                        "ignoring unsupported tail metadata.",
                        uk3_count, remaining
                    )
                    return entry

                for _ in range(uk3_count):
                    ra = [f.readByte(), f.readByte()]
                    entry.uk3.append(ra)
    except Exception as e:
        log.debug("Ignoring CVXM optional tail metadata parse error: %s", e)

    return entry


def _read_mesh_entry(f: bStream) -> MeshCacheEntry:
    """Read a single MESH (Triangle Mesh) entry from the stream.

    Assumes the stream is positioned after the NXS magic, at the MESH marker.
    """
    if f.readUInt32() != 1213416781:  # MESH
        return None
    entry = MeshCacheEntry()

    unk1 = f.readUInt32()  # PhysX version: 15 (PhysX 3.4+) or 12 (Witcher 3)
    entry.flags = f.readUInt32()
    unk2 = f.readUInt32()  # Typically 1 or 7 (may be weld tolerance as float)

    vertex_count = f.readInt32()
    face_count = f.readInt32()

    for _ in range(vertex_count):
        entry.vertices.append(Vector3(
            x=f.readFloat(),
            y=f.readFloat(),
            z=f.readFloat()
        ))

    for _ in range(face_count):
        ra = []
        if (entry.flags & 0b1000) > 0:
            # 16-bit indices
            ra.extend([f.readUInt16(), f.readUInt16(), f.readUInt16()])
        else:
            # 8-bit indices
            ra.extend([f.readByte(), f.readByte(), f.readByte()])
        entry.faces.append(ra)

    if (entry.flags & 0b1) > 0:
        # Has per-face material indices
        for _ in range(face_count):
            entry.materials.append(f.readUInt16())

    # BVH acceleration structure data (optional, may not exist in all files)
    try:
        BV4 = f.read(4)  # BV4 marker

        entry.uk1 = f.readUInt32()  # BVH tree parameter

        entry.centerOfMass = Vector3(
            x=f.readFloat(),
            y=f.readFloat(),
            z=f.readFloat()
        )

        entry.mass = f.readFloat()
        entry.uk2 = f.readUInt32()  # BVH build flags

        # BVH bounds (6 floats)
        for _ in range(6):
            entry.uk3.append(f.readFloat())

        uv_count = f.readUInt32()
        for _ in range(uv_count):
            uv = []
            for _ in range(4):
                ra = []
                ra.extend([f.readUInt16() / 65535.0, f.readUInt16() / 65535.0])
                uv.append(ra)
            entry.uvs.append(uv)

        entry.uk4 = f.readUInt32()

        entry.bounds['min'] = Vector3(x=f.readFloat(), y=f.readFloat(), z=f.readFloat())
        entry.bounds['max'] = Vector3(x=f.readFloat(), y=f.readFloat(), z=f.readFloat())

        face_flag_count = f.readUInt32()

        for _ in range(face_flag_count):
            entry.faceFlags.append(f.readByte())
    except Exception as e:
        # BVH data may be truncated or absent in some files
        log.debug(f"Optional BVH data not fully read: {e}")

    return entry


def read_all_nxs_entries(f: bStream) -> list:
    """Read all NXS entries from file until EOF.

    NXS files can contain multiple collision mesh entries (CVXM/MESH)
    which may be padded/aligned to fixed boundaries. Each entry starts
    with the NXS magic (0x014E5358).

    Returns:
        list: List of CVXMCacheEntry and/or MeshCacheEntry objects
    """
    entries = []
    NXS_MAGIC = bytes([0x4E, 0x58, 0x53, 0x01])  # "NXS\x01" in little-endian

    # Read entire file to scan for magic markers
    start_pos = f.tell()
    file_data =  f.readAll() #f.read()
    f.seek(start_pos)

    # Find all NXS magic positions
    magic_positions = []
    pos = 0
    while True:
        idx = file_data.find(NXS_MAGIC, pos)
        if idx == -1:
            break
        magic_positions.append(start_pos + idx)
        pos = idx + 1

    # Read each entry
    for entry_pos in magic_positions:
        try:
            f.seek(entry_pos + 4)  # Skip NXS magic

            # Peek at type marker
            type_id = f.read(4)
            f.seek(-4, os.SEEK_CUR)

            if len(type_id) < 4:
                log.debug(f"Skipping truncated NXS marker at offset {entry_pos}")
                continue

            entry = None
            if type_id == b'CVXM':
                entry = _read_cvxm_entry(f)
            elif type_id == b'MESH':
                entry = _read_mesh_entry(f)
            else:
                log.warning(f"Unknown NXS entry type at offset {entry_pos}: {type_id}")
                continue

            if entry:
                # Record byte span so mixed files can recover non-NXS primitive
                # blobs that appear before/after NXS entries in the same file.
                try:
                    entry._file_start = entry_pos
                    entry._file_end = f.tell()
                except Exception:
                    pass
                entries.append(entry)
        except Exception as e:
            # Some files contain false-positive NXS magic bytes or truncated
            # trailing data. Skip the bad candidate and continue scanning.
            log.warning(f"Skipping malformed NXS entry at offset {entry_pos}: {e}")
            continue

    return entries


def read_cvxm_cache_entry(f: bStream):
    """Read a single NXS entry (legacy function for backwards compatibility)."""
    if f.readUInt32() != 22239310:  # NXS
        return None
    type_id = f.read(4)
    f.seek(-4, os.SEEK_CUR)

    if type_id[0] == ord('C'):
        return _read_cvxm_entry(f)
    else:
        return _read_mesh_entry(f)


def _identity_matrix_list():
    # 4x4 identity in the nested-list format expected by _setup_collision_object.
    return [
        [1.0, 0.0, 0.0, 0.0],
        [0.0, 1.0, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]


def _try_import_primitive_blob_bytes(data: bytes, base_name: str, source_label: str = ""):
    """Interpret tiny non-NXS primitive blobs and create collision objects."""
    if len(data) >= 4 and data[:4] == b'NXS\x01':
        return None

    def _valid_positive_float(v: float) -> bool:
        return v is not None and v > 0.0 and abs(v) < 1_000_000.0

    # Box primitive: 3 x float32 half-extents (X, Y, Z)
    if len(data) == 12:
        try:
            half_x, half_y, half_z = struct.unpack('<3f', data)
        except struct.error:
            return None

        dims = (half_x, half_y, half_z)
        if not all(_valid_positive_float(v) for v in dims):
            return None

        shape = CCollisionShapeBox()
        shape.physicalMaterialName = "nxs_material"
        shape.matrix_world = _identity_matrix_list()
        shape.halfExtendsX = float(half_x)
        shape.halfExtendsY = float(half_y)
        shape.halfExtendsZ = float(half_z)

        log.info(
            "Primitive collision fallback: interpreting non-NXS %s (%d bytes) as box half-extents %s",
            source_label or base_name, len(data), tuple(round(v, 6) for v in dims)
        )
        return [createBox(shape, base_name)]

    # Sphere primitive: 1 x float32 radius
    if len(data) == 4:
        try:
            (radius,) = struct.unpack('<f', data)
        except struct.error:
            return None

        if not _valid_positive_float(radius):
            return None

        shape = CCollisionShapeSphere()
        shape.physicalMaterialName = "nxs_material"
        shape.matrix_world = _identity_matrix_list()
        shape.radius = float(radius)

        log.info(
            "Primitive collision fallback: interpreting non-NXS %s (%d bytes) as sphere radius %s",
            source_label or base_name, len(data), round(radius, 6)
        )
        return [createSphere(shape, base_name)]

    # Capsule primitive: 2 x float32 (radius, halfHeight)
    # createCapsule expects center-to-center distance, so convert height = 2 * halfHeight
    if len(data) == 8:
        try:
            radius, half_height = struct.unpack('<2f', data)
        except struct.error:
            return None

        if not (_valid_positive_float(radius) and _valid_positive_float(half_height)):
            return None

        shape = CCollisionShapeCapsule()
        shape.physicalMaterialName = "nxs_material"
        shape.matrix_world = _identity_matrix_list()
        shape.radius = float(radius)
        shape.height = float(half_height) * 2.0

        log.info(
            "Primitive collision fallback: interpreting non-NXS %s (%d bytes) as capsule radius=%s halfHeight=%s",
            source_label or base_name, len(data), round(radius, 6), round(half_height, 6)
        )
        return [createCapsule(shape, base_name)]

    # Multi-primitive blob: the collision cache extractor concatenates sub-file payloads
    # into one stream without type tags. Priority:
    #   1. Pure capsule decomposition when len % 8 == 0 and every capsule is elongated (h >= r)
    #   2. Pure box decomposition when len % 12 == 0
    #   3. Greedy mixed: boxes (12 b) first, then capsules (8 b), then spheres (4 b)
    if len(data) % 4 == 0 and len(data) >= 16:
        primitives = []

        # 1. Try pure capsule
        if len(data) % 8 == 0:
            caps = []
            for i in range(0, len(data), 8):
                r, h = struct.unpack_from('<2f', data, i)
                if _valid_positive_float(r) and _valid_positive_float(h) and h >= r:
                    caps.append(('capsule', r, h))
                else:
                    caps = []
                    break
            if caps:
                primitives = caps

        # 2. Try pure box (if capsule didn't win)
        if not primitives and len(data) % 12 == 0:
            boxes = []
            for i in range(0, len(data), 12):
                hx, hy, hz = struct.unpack_from('<3f', data, i)
                if all(_valid_positive_float(v) for v in (hx, hy, hz)):
                    boxes.append(('box', hx, hy, hz))
                else:
                    boxes = []
                    break
            if boxes:
                primitives = boxes

        # 3. Greedy mixed fallback
        if not primitives:
            offset = 0
            while offset < len(data):
                remaining = len(data) - offset
                if remaining >= 12:
                    hx, hy, hz = struct.unpack_from('<3f', data, offset)
                    if all(_valid_positive_float(v) for v in (hx, hy, hz)):
                        primitives.append(('box', hx, hy, hz))
                        offset += 12
                        continue
                if remaining >= 8:
                    r, h = struct.unpack_from('<2f', data, offset)
                    if _valid_positive_float(r) and _valid_positive_float(h):
                        primitives.append(('capsule', r, h))
                        offset += 8
                        continue
                if remaining >= 4:
                    (r,) = struct.unpack_from('<f', data, offset)
                    if _valid_positive_float(r):
                        primitives.append(('sphere', r))
                        offset += 4
                        continue
                primitives = []
                break

        if primitives:
            log.info(
                "Primitive collision fallback: interpreting non-NXS %s (%d bytes) as %d primitive(s): %s",
                source_label or base_name, len(data), len(primitives),
                [p[0] for p in primitives],
            )
            objects = []
            for i, prim in enumerate(primitives):
                prim_name = base_name if len(primitives) == 1 else f"{base_name}_{i:02d}"
                if prim[0] == 'box':
                    hx, hy, hz = prim[1], prim[2], prim[3]
                    shape = CCollisionShapeBox()
                    shape.physicalMaterialName = "nxs_material"
                    shape.matrix_world = _identity_matrix_list()
                    shape.halfExtendsX = float(hx)
                    shape.halfExtendsY = float(hy)
                    shape.halfExtendsZ = float(hz)
                    objects.append(createBox(shape, prim_name))
                elif prim[0] == 'capsule':
                    r, h = prim[1], prim[2]
                    shape = CCollisionShapeCapsule()
                    shape.physicalMaterialName = "nxs_material"
                    shape.matrix_world = _identity_matrix_list()
                    shape.radius = float(r)
                    shape.height = float(h) * 2.0
                    objects.append(createCapsule(shape, prim_name))
                elif prim[0] == 'sphere':
                    r = prim[1]
                    shape = CCollisionShapeSphere()
                    shape.physicalMaterialName = "nxs_material"
                    shape.matrix_world = _identity_matrix_list()
                    shape.radius = float(r)
                    objects.append(createSphere(shape, prim_name))
            if objects:
                return objects

    return None


def _try_import_mixed_file_primitives(filePath: str, entries: list):
    """Import primitive blobs that coexist with NXS entries in one file."""
    try:
        data = Path(filePath).read_bytes()
    except Exception:
        return []

    spans = []
    for entry in entries or []:
        start = getattr(entry, "_file_start", None)
        end = getattr(entry, "_file_end", None)
        if isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(data):
            spans.append((start, end))
    if not spans:
        return []

    spans.sort()
    objects = []
    gap_index = 0
    cursor = 0
    for start, end in spans:
        if start > cursor:
            gap = data[cursor:start]
            # Primitive blobs are tiny; ignore large non-NXS gaps/padding noise.
            if gap and len(gap) <= 32:
                prim_name = Path(filePath).stem if gap_index == 0 else f"{Path(filePath).stem}_prim{gap_index:02d}"
                prim_objs = _try_import_primitive_blob_bytes(
                    gap,
                    prim_name,
                    source_label=f"{filePath} [offset {cursor}:{start}]",
                )
                if prim_objs:
                    objects.extend(prim_objs)
                    gap_index += 1
        if end > cursor:
            cursor = end

    if cursor < len(data):
        gap = data[cursor:]
        if gap and len(gap) <= 32:
            prim_name = f"{Path(filePath).stem}_prim{gap_index:02d}"
            prim_objs = _try_import_primitive_blob_bytes(
                gap,
                prim_name,
                source_label=f"{filePath} [offset {cursor}:{len(data)}]",
            )
            if prim_objs:
                objects.extend(prim_objs)

    return objects


def _try_import_primitive_blob(filePath: str):
    """Fallback importer for tiny non-NXS primitive collision blobs.

    Some collision cache entries are stored as stripped primitive parameters
    (e.g. 3 float half-extents for a box) but are extracted with an `.nxs`
    extension by the cache browser. These are not PhysX NXS files and should
    not go through the CVXM/MESH parser.
    """
    try:
        data = Path(filePath).read_bytes()
    except Exception:
        return None
    return _try_import_primitive_blob_bytes(data, Path(filePath).stem, source_label=filePath)

def _create_primitive_from_item(payload: bytes, flag: int, pose, material_name: str, name: str):
    """Create one collision primitive from a RED header item's data and pose.

    Args:
        payload: Raw shape bytes (12=box, 4=sphere, 8=capsule)
        flag: PxGeometryType (0=sphere, 2=capsule, 3=box, 4/5=NXS handled elsewhere)
        pose: 4x4 row-major matrix list or None for identity
        material_name: Physics material name
        name: Blender object name base

    Returns:
        Created Blender object, or None on failure
    """
    pose_mat = pose if pose is not None else _identity_matrix_list()
    mat_name = material_name or "nxs_material"

    if flag == 3:  # box: 3 x float32 half-extents
        if len(payload) < 12:
            log.warning("Box primitive payload too small: %d bytes for '%s'", len(payload), name)
            return None
        hx, hy, hz = struct.unpack_from('<3f', payload)
        shape = CCollisionShapeBox()
        shape.physicalMaterialName = mat_name
        shape.matrix_world = pose_mat
        shape.halfExtendsX = float(hx)
        shape.halfExtendsY = float(hy)
        shape.halfExtendsZ = float(hz)
        return createBox(shape, name)

    elif flag == 0:  # sphere: 1 x float32 radius
        if len(payload) < 4:
            log.warning("Sphere primitive payload too small: %d bytes for '%s'", len(payload), name)
            return None
        (r,) = struct.unpack_from('<f', payload)
        shape = CCollisionShapeSphere()
        shape.physicalMaterialName = mat_name
        shape.matrix_world = pose_mat
        shape.radius = float(r)
        return createSphere(shape, name)

    elif flag == 2:  # capsule: 2 x float32 (radius, halfHeight)
        if len(payload) < 8:
            log.warning("Capsule primitive payload too small: %d bytes for '%s'", len(payload), name)
            return None
        r, half_h = struct.unpack_from('<2f', payload)
        shape = CCollisionShapeCapsule()
        shape.physicalMaterialName = mat_name
        shape.matrix_world = pose_mat
        shape.radius = float(r)
        shape.height = float(half_h) * 2.0
        return createCapsule(shape, name)

    log.debug("Unhandled primitive flag %d for '%s'", flag, name)
    return None


def create_from_nxs(filePath, shape_items=None):
    """Import NXS file, creating Blender objects for all collision entries.

    Args:
        filePath: Path to the NXS file
        shape_items: Optional list of (matrix_4x4_or_None, flag, payload_bytes, material_name)
                     tuples from CollisionCacheItem.get_shapes_with_data(). When provided:
                     - NXS shapes (flag 4=convex, 5=trimesh): pose applied to the created object
                     - Primitive shapes (flag 0-3): created individually with pose + material

    Returns:
        list: List of created Blender objects

    Raises:
        FileFormatException: If no valid NXS entries are found and no shape_items
    """
    NXS_FLAGS = (4, 5)
    nxs_poses = [pose for pose, flag, payload, mat in shape_items if flag in NXS_FLAGS] if shape_items else []

    base_name = Path(filePath).stem
    nxs_file = bStream(path=filePath)
    objects = []

    with nxs_file:
        entries = read_all_nxs_entries(nxs_file)
        if not entries:
            try:
                nxs_file.seek(0)
                header = nxs_file.read(16)
                nxs_file.seek(0, os.SEEK_END)
                file_size = nxs_file.tell()
            except Exception:
                header = b""
                file_size = -1

    if entries:
        primitive_objects = _try_import_mixed_file_primitives(filePath, entries)
        if primitive_objects:
            objects.extend(primitive_objects)

    if not entries:
        # Use per-item data from RED header when available — gives correct pose per primitive.
        if shape_items:
            primitive_items = [(pose, flag, payload, mat) for pose, flag, payload, mat in shape_items if flag not in NXS_FLAGS]
            if primitive_items:
                for i, (pose, flag, payload, mat) in enumerate(primitive_items):
                    prim_name = base_name if len(primitive_items) == 1 else f"{base_name}_{i:02d}"
                    obj = _create_primitive_from_item(payload, flag, pose, mat, prim_name)
                    if obj:
                        objects.append(obj)
                if objects:
                    log.info("Imported %d primitive collision object(s) from %s", len(objects), filePath)
                    return objects

        # Fall back to heuristic blob parsing (no pose data available)
        primitive_objects = _try_import_primitive_blob(filePath)
        if primitive_objects:
            log.info(f"Imported {len(primitive_objects)} primitive collision object(s) from {filePath}")
            return primitive_objects

        header_hex = header.hex(" ") if header else "<unavailable>"
        raise FileFormatException(
            f"No valid NXS entries found in file (size={file_size} bytes, header={header_hex})"
        )

    for i, entry in enumerate(entries):
        mesh_name = f"{base_name}_{i:02d}" if len(entries) > 1 else base_name

        if isinstance(entry, CVXMCacheEntry):
            shape = CCollisionShapeConvexNXS(entry)
            if i < len(nxs_poses) and nxs_poses[i] is not None:
                shape.matrix_world = nxs_poses[i]
            obj = createCol(shape, mesh_name)
            objects.append(obj)
        elif isinstance(entry, MeshCacheEntry):
            shape = CCollisionShapeTriMeshNXS(entry)
            if i < len(nxs_poses) and nxs_poses[i] is not None:
                shape.matrix_world = nxs_poses[i]
            obj = createTri(shape, mesh_name)
            objects.append(obj)

    log.info(f"Imported {len(objects)} collision objects from {filePath}")
    return objects
