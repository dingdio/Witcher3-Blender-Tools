from imp import reload
import bpy
import time
import json
from pathlib import Path
from io_import_w2l.setup_logging_bl import *
log = logging.getLogger(__name__)

from io_import_w2l.CR2W import mesh_builder
#!REMORE
reload(mesh_builder)
#!!REMORE
from io_import_w2l import get_wolvenkit
from io_import_w2l.CR2W.Types.VariousTypes import CMatrix4x4
from io_import_w2l.CR2W.Types.SBufferInfos import BoneData
from io_import_w2l.importers.import_rig import get_ordered_bones
from io_import_w2l.importers.import_mesh import get_mesh_info


class WitcherMaterialInfo(object):
    def __init__(self):
        super(WitcherMaterialInfo, self).__init__()
        pass

def extract_bone_data(armature, matrix_ref = None):
    bone_data = BoneData()
    bone_data.nbBones = len(armature.data.bones)
    ordered_bones = get_ordered_bones(armature)
    for bone in ordered_bones:
        bone_data.jointNames.append(bone.name)
        mat = bone.matrix_local.inverted()
        bone_matrix = CMatrix4x4(None)
        bone_matrix.ax, bone_matrix.bx, bone_matrix.cx, bone_matrix.dx = mat[0][0], mat[0][1], mat[0][2], mat[0][3]
        bone_matrix.ay, bone_matrix.by, bone_matrix.cy, bone_matrix.dy = mat[1][0], mat[1][1], mat[1][2], mat[1][3]
        bone_matrix.az, bone_matrix.bz, bone_matrix.cz, bone_matrix.dz = mat[2][0], mat[2][1], mat[2][2], mat[2][3]
        bone_matrix.aw, bone_matrix.bw, bone_matrix.cw, bone_matrix.dw = mat[3][0], mat[3][1], mat[3][2], mat[3][3]
        bone_matrix.Create()
        bone_data.boneMatrices.append(bone_matrix)
    return bone_data


from io_import_w2l.w3_material_nodes import get_group_inputs, get_socket_value

def get_mesh_material_info(mesh_bl):
    material_props = []
    for mat in mesh_bl.materials:
        mat_props = mat.witcher_props
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
                                mat_dict['witcher_props']['input_props'].append(
                                    {'name':input_socket.name,
                                    'type': linked_socket.node.type,
                                    'value':get_socket_value(input_socket)})
        
        
        # create Vector4 if W value
        for prop in mat_dict['witcher_props']['input_props']:
            if prop['type'] == 'COMBXYZ':
                for w_prop in mat_dict['witcher_props']['input_props']:
                    if w_prop['name'] == prop['name']+'_W':
                        mat_dict['witcher_props']['input_props'].remove(w_prop)
                        prop['value'].append(w_prop['value'])
                        break

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

def get_vertex_group_info(armobj, mesh_ob):
    vgi = []
    try:
        for bone_obj in get_ordered_bones(armobj):
            if group_exists(mesh_ob, bone_obj.name):
                vgi.append(furthest_vertex_distance( mesh_ob, bone_obj ))
            else:
                vgi.append(0)
    except Exception as e:
        raise e
    return vgi

def convert_to_index_values(string_array, second_array):
    index_array = []
    for string in string_array:
        index_array.append(second_array.index(string))
    return index_array

import bmesh
def separate_mesh_by_verts(obj, num_verts):
    mesh = obj.data
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj

    if len(mesh.vertices) > num_verts:
        vert_group = obj.vertex_groups.new(name="Separated")

        bpy.ops.object.mode_set(mode='OBJECT')
        for face in mesh.polygons:
            ignore = False
            for vert in face.vertices:
                if vert >= num_verts:
                    ignore = True
            if not ignore:
                vert_group.add(face.vertices, 1.0, 'REPLACE')

        # for vert in mesh.vertices:
        #     if vert.index >= num_verts:
        #         vert_group.add([vert.index], 1.0, 'REPLACE')

        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_mode(type='VERT')
        bpy.ops.mesh.select_all(action='DESELECT')
        bpy.ops.object.vertex_group_set_active(group=vert_group.name)
        bpy.ops.object.vertex_group_select()
        bpy.ops.mesh.separate(type='SELECTED')
        bpy.ops.object.mode_set(mode='OBJECT')

        new_meshes = bpy.context.selected_objects[:]
        test_mesh = len(new_meshes[0].data.vertices)
        fixed_mesh = len(new_meshes[1].data.vertices)
        
        if fixed_mesh > num_verts:
            raise Exception('Bad split')
        
        submeshes = []
        for mesh in reversed(new_meshes):
            group = mesh.vertex_groups.get("Separated")
            mesh.vertex_groups.remove(group)
            submeshes.extend(separate_mesh_by_verts(mesh, num_verts))
        return submeshes
    else:
        return [obj]


def split_mesh_by_material(mesh_obj):
    mesh_copy = mesh_obj.copy()
    mesh_copy.data = mesh_obj.data.copy()
    bpy.context.collection.objects.link(mesh_copy)

    bpy.ops.object.select_all(action='DESELECT')
    mesh_copy.select_set(True)
    bpy.context.view_layer.objects.active = mesh_copy
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.mesh.separate(type='MATERIAL')
    bpy.ops.object.mode_set(mode='OBJECT')
    new_meshes = bpy.context.selected_objects[:]
    first_element = new_meshes.pop(0)
    new_meshes.append(first_element)
    #return new_meshes
    
    #! seperate work
    final_meshes = []
    
    for mesh in new_meshes:
        mesh_chunks = separate_mesh_by_verts(mesh, 65534)
        final_meshes.extend(mesh_chunks)
    
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

class MeshExporter(object):
    """docstring for MeshExporter."""
    def __init__(self):
        super(MeshExporter, self).__init__()
        self.cr2w = None
        self.bone_data = BoneData() # create empty bone data as default for static meshes
        self.__armature = None
        self.__meshes = None

    def __loadMeshData(self, meshObj, bone_map):
        bl_mesh = meshObj.data
        
        has_excess_weights = False
        for vertex in bl_mesh.vertices:
            vertex_groups = vertex.groups
            if len(vertex_groups) > 4:
                has_excess_weights = True
                break

        if has_excess_weights:
            bpy.context.view_layer.objects.active = meshObj
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.object.vertex_group_limit_total(limit=4)
            bpy.ops.object.mode_set(mode='OBJECT')
            log.debug("Applied 'Limit Total' operation to mesh part.")
        #else:
            #print("The mesh does not have vertices with more than 4 weights.")
        
        
        triangulated = len(bl_mesh.loops) == len(bl_mesh.polygons) * 3
        mesh_for_work = bl_mesh
        if not triangulated:
            tmp_me = bpy.data.meshes.new_from_object(
                        meshObj, preserve_all_data_layers=True, depsgraph = bpy.context.evaluated_depsgraph_get())
            import bmesh
            bm = bmesh.new()
            bm.from_mesh(tmp_me)
            bmesh.ops.triangulate(bm, faces=bm.faces)
            bm.to_mesh(tmp_me)
            bm.free()
            mesh_for_work = tmp_me

        exportMeshdata = get_mesh_info(mesh_for_work, meshObj)
        exportMaterialdata = get_mesh_material_info(mesh_for_work)

        if not triangulated:
            bpy.data.meshes.remove(tmp_me)
        return (exportMeshdata, exportMaterialdata)

    def execute(self, filePath, **args):
        self.filePath = filePath
        self.__armature = args.get('armature', None)
        self.__meshes = sorted(args.get('meshes', []), key=lambda x: x.name)
        
        
        if self.__armature:
            self.bone_data = extract_bone_data(self.__armature)
            vert_group_info = get_vertex_group_info(self.__armature, self.__meshes[0])
            self.bone_data.Block3 = vert_group_info
            group_names = [group.name for group in self.__meshes[0].vertex_groups]
            self.bone_data.BoneIndecesMappingBoneIndex = convert_to_index_values(group_names, self.bone_data.jointNames)
            # Block3:[]
            # BoneIndecesMappingBoneIndex:[]
        nameMap = [] #self.__exportBones(meshes)
        
        #Note the mesh radius on vanilla w2mesh is calculated for all lods together.
        rad_box = get_mesh_radius_and_bounding_box(self.__meshes[0])
        
        #class Common_Info
        common_info = {
            'generalizedMeshRadius' : rad_box[0],
            'boundingBox' : rad_box[1],
            'lod0_MeshSettings' : self.__meshes[0].witcherui_MeshSettings
        }
        #generalizedMeshRadius
        #boundingBox

        # MESH STUFF
        #todo chunks are stored in reversed sort order by faces
        ALL_LODS = []
        for m in self.__meshes:
            new_meshes = split_mesh_by_material(m)
            mesh_data = [self.__loadMeshData(i, nameMap) for i in new_meshes]
            for mesh in new_meshes:
                bpy.data.meshes.remove(mesh.data)
            del new_meshes

            # final_mesh_data = []

            # for d in mesh_data:
            #     cake = d[0]
            #     num_in = cake.meshInfo.numVertices
            #     if num_in > 65534:
            #         split_data = cake.split_data()
            #         for data in split_data:
            #             final_mesh_data.append([data, d[1]])
            #     else:
            #         final_mesh_data.append(d)

            ALL_LODS.append([mesh_data, m.witcherui_MeshSettings])
        #mesh_data_orig = [self.__loadMeshData(i, nameMap) for i in self.__meshes]
        
        self.cr2w = mesh_builder.BuildMesh(ALL_LODS, self.bone_data, common_info)

        # if args.get('copy_textures', False):
        #     output_dir = os.path.dirname(filePath)
        #     import_folder = root.get('import_folder', '') if root else ''
        #     base_folder = bpyutils.addon_preferences('base_texture_folder', '')
        #     self.__copy_textures(output_dir, import_folder or base_folder)

        self.__save_json(filePath)
        
    def __save_json(self, filePath):
        json_data = self.cr2w.GetJson()
        savePath = Path(filePath)
        final_savePath = str(savePath)+'.json' if '.json' not in str(savePath) else str(savePath)
        #final_savePath = str(savePath.with_suffix(''))+'_FROM_BLENDER_.w2mesh.json'
        with open(final_savePath, "w") as file:
            file.write(json.dumps(json_data,indent=2, default=vars, sort_keys=False))
        convert = True
        if convert:
            WolvenKit = Path(get_wolvenkit(bpy.context))
            if WolvenKit.exists():
                import subprocess
                command = [str(WolvenKit), "--input", final_savePath, "--json2cr2w"]
                subprocess.run(command)
            else:
                log.critical('Wolvenkit CLI .exe not found.')

def do_export_mesh(context, filePath, **kwargs):
    print("--------------------EXPORTING MESH------------------------")
    start_time = time.time()
    exporter = MeshExporter()
    exporter.execute(filePath, **kwargs)
