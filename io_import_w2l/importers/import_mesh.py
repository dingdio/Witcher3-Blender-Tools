from imp import reload
import os

import bpy
from bpy.types import Object
from typing import List, Tuple
from mathutils import Vector, Matrix
import numpy as np
import array

from io_import_w2l.cloth_util import setup_w3_material_CR2W
from io_import_w2l import get_texture_path, get_uncook_path
from io_import_w2l import file_helpers
from io_import_w2l.CR2W import w3_types
from io_import_w2l.CR2W import read_json_w3
from io_import_w2l.w3_armature_constants import *
from io_import_w2l.importers import data_types
import io_import_w2l.CR2W.dc_mesh
reload(io_import_w2l.CR2W.dc_mesh)
from  io_import_w2l.CR2W.dc_mesh import MeshData
from io_import_w2l.CR2W.Types.BlenderMesh import CommonData
from io_import_w2l.CR2W.Types.SBufferInfos import SMeshInfos, EMeshVertexType, VertexSkinningEntry

from io_import_w2l.setup_logging_bl import *
log = logging.getLogger(__name__)

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
                keep_proxy_meshes:bool = False) -> w3_types.CSkeletalAnimationSet:
    dirpath, file = os.path.split(filename)
    basename, ext = os.path.splitext(file)
    if ext.lower() in ('.w2mesh'):
        with open(filename) as file:
            try:
                (CData, bufferInfos, the_material_names, the_materials, meshName, meshFile) = io_import_w2l.CR2W.dc_mesh.load_bin_mesh(filename, keep_lod_meshes, keep_proxy_meshes)
                (final_bl_meshes, armatures) = prepare_mesh_import(CData, bufferInfos, the_material_names, the_materials, meshName, meshFile,
                    do_import_mats,
                    do_import_armature,
                    keep_lod_meshes,
                    do_merge_normals,
                    rotate_180,
                    keep_empty_lods,
                    keep_proxy_meshes)
                
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
                return (final_bl_meshes, armatures)
            except Exception as e:
                raise e
    else:
        anim = None
    return anim

from io_import_w2l import get_mod_directory, get_texture_path, get_modded_texture_path
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
    
    if MOD_DIR in file_path:
        file_path = file_path.replace(MOD_DIR+'\\', '')
        for folder in possible_folders:
            if folder in file_path:
                file_path = file_path.replace(folder+'\\', '')
                break
        return file_path
    elif UNCOOK_DIR in file_path:
        file_path = file_path.replace(UNCOOK_DIR+'\\', '')
        return file_path
    elif MOD_TEX_PATH in file_path:
        file_path = file_path.replace(MOD_TEX_PATH+'\\', '')
        return file_path

    for root_folder in root_folders:
        if root_folder in file_path:
            parts = file_path.split(root_folder, 1)
            if len(parts) == 2:
                first_part, second_part = parts[0], root_folder + parts[1]
            else:
                first_part, second_part = file_path, ""
            return second_part

    return file_path

def prepare_mesh_import(CData, bufferInfos, the_material_names, the_materials, meshName, meshFile,
                do_import_mats,
                do_import_armature,
                keep_lod_meshes,
                do_merge_normals,
                rotate_180,
                keep_empty_lods,
                keep_proxy_meshes):
    #TODO proxy meshes don't have lod0 they start at lod1, should import proxy anyway if requested
    #meshData = meshFile
    created_mesh_bl = []
    for idx, meshDataBl in enumerate(CData.meshDataAllMeshes):
        mat_id = CData.meshDataAllMeshes[idx].meshInfo.materialID
        lod_level = CData.meshDataAllMeshes[idx].meshInfo.lod #if not bufferInfos.verticesBuffer else bufferInfos.verticesBuffer[idx].lod
        distance = CData.meshDataAllMeshes[idx].meshInfo.distance
        
        if not keep_lod_meshes and lod_level > 0 and "proxy" not in meshName:
            break
        #TODO some meshes seem to have no real data and should be discarded, prob a result of auto LOD creation
        #TODO trying to create them crashes blender
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
                obj = do_blender_mesh_import(meshDataBl, CData, do_merge_normals)
                #obj.witcherui_MeshSettings['witcher_lod_level'] = lod_level
                #obj.witcherui_MeshSettings['witcher_distance'] = distance
                #obj.witcherui_MeshSettings['witcher_mat_id'] = mat_id
                obj.witcherui_MeshSettings['lod_level'] = lod_level
                obj.witcherui_MeshSettings['distance'] = distance
                obj.witcherui_MeshSettings['mat_id'] = mat_id
                obj.witcherui_MeshSettings['item_repo_path'] = get_repo_from_abs_path(meshFile.fileName)
                obj.witcherui_MeshSettings['make_export_dir'] = True
                
                if lod_level == 0:
                    obj.witcherui_MeshSettings['autohideDistance'] = CData.autohideDistance
                    obj.witcherui_MeshSettings['isTwoSided'] = CData.isTwoSided
                    obj.witcherui_MeshSettings['useExtraStreams'] = CData.useExtraStreams
                    obj.witcherui_MeshSettings['mergeInGlobalShadowMesh'] = CData.mergeInGlobalShadowMesh
                    obj.witcherui_MeshSettings['entityProxy'] = CData.entityProxy
                created_mesh_bl.append(obj)
        except Exception as e:
            log.error("warning couldn't create one of the meshes at index ", idx)
    
    import bpy
    lod0 = []
    lod1 = []
    lod2 = []
    lod3 = []
    lods_to_create = [lod0,
                    lod1,
                    lod2,
                    lod3]

    for idx, mesh_bl in enumerate(created_mesh_bl):
        # mat_id = CData.meshDataAllMeshes[idx].meshInfo.materialID
        # bufferInfos.verticesBuffer[idx].lod

        mat_id = mesh_bl.witcherui_MeshSettings['mat_id']
        lod_level = mesh_bl.witcherui_MeshSettings['lod_level']

        lod0.append(mesh_bl) if lod_level == 0 else 0
        lod1.append(mesh_bl) if lod_level == 1 else 0
        lod2.append(mesh_bl) if lod_level == 2 else 0
        lod3.append(mesh_bl) if lod_level == 3 else 0

        if the_material_names[mat_id] in bpy.data.materials:
            mesh_bl.data.materials.append(bpy.data.materials[the_material_names[mat_id]])
        else:
            temp_mat = bpy.data.materials.new(the_material_names[mat_id])
            mesh_bl.data.materials.append(temp_mat)

    if do_import_armature:
        try:
            #==========#
            # Armature #
            #==========#
            if (CData.meshInfos[0].vertexType == EMeshVertexType.EMVT_SKINNED):
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
                bpy.ops.object.select_all(action='DESELECT')
                bpy.context.view_layer.objects.active = lod_meshes[0]
                for bl_mesh in lod_meshes:
                    bl_mesh.select_set(True)
                if len(lod_meshes)> 1:
                    bpy.ops.object.join()
                joined_obj = bpy.context.selected_objects[:][0]
                joined_obj.name = meshName+"_lod"+str(idx)
                
                ## ROTATE 180
                # if rotate_180:
                #     joined_obj.select_set(True)
                #     joined_obj.rotation_euler[2] = np.pi
                #     bpy.ops.object.transform_apply(location=False, rotation=True, scale=False)
                    
                final_bl_meshes.append(joined_obj)

                if (CData.meshInfos[0].vertexType == EMeshVertexType.EMVT_SKINNED and do_import_armature):
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
        uncook_path = get_texture_path(bpy.context)+"\\" #! THE PATH WITH THE TEXTURES NOT THE FBX FILES
        uncook_path_modkit = get_uncook_path(bpy.context)
        xml_path = "w2mesh"
        
        materials = []
        for o in the_materials.Handles:
            if o.Reference is not None:
                materials.append(meshFile.CHUNKS.CHUNKS[o.Reference])
                materials[-1].local = True
            else:
                material_file_chunks = io_import_w2l.CR2W.CR2W_reader.load_material(uncook_path_modkit+"\\"+o.DepotPath)
                for chunk in material_file_chunks:
                    if chunk.Type == "CMaterialInstance":
                        materials.append(chunk)
                        materials[-1].local = False
                        materials[-1].DepotPath = o.DepotPath
        #material_names = [o.String.split('::')[1] for o in chunk.GetVariableByName('apexMaterialNames').elements]
        load_materials = True
        if load_materials:
            mat_filename = "witcher_mat"
            load_w3_materials_CR2W_Mesh(final_bl_meshes, uncook_path, materials, the_material_names, mat_filename=mat_filename)

    #===========#
    #  Finish   #
    #===========#
    #select everything just imported
    armatures = []
    if (CData.meshInfos[0].vertexType == EMeshVertexType.EMVT_SKINNED and do_import_armature):
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
    try:
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
        allUVMaps = [meshDataBl.UV_vertex3DCoords, meshDataBl.UV2_vertex3DCoords]
        uvmap_names = []
        for k in range(len(allUVMaps)):
            uvmap_names.append("DiffuseUV" if k == 0 else "SecondUV")
        for k in range(len(uvmap_names)):
            mesh.uv_layers.new(name=uvmap_names[k])
            for face in mesh.polygons:
                for vert_idx, loop_idx in zip(face.vertices, face.loop_indices):
                    mesh.uv_layers[uvmap_names[k]].data[loop_idx].uv = allUVMaps[k][vert_idx]

        #==============#
        # Vertex Color #
        #==============#
        if meshDataBl.vertexColor:
            mesh.color_attributes.new(name = 'Color', domain = 'POINT', type = 'BYTE_COLOR')
            for vert in mesh.vertices:
                mesh.color_attributes.active.data[vert.index].color = meshDataBl.vertexColor[vert.index]
            #del(meshDataBl.vertexColor)

        #=========#
        # Normals #
        #=========#
        
        fbx_method = True
        if fbx_method: # taken from blender fbx importer
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
            mesh_da = mesh
            mesh_da.create_normals_split()
            mesh_da.use_auto_smooth = True
            mesh_da.normals_split_custom_set_from_vertices(meshDataBl.normals)
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
        for idx in CData.boneData.BoneIndecesMappingBoneIndex:
            mesh_ob.vertex_groups.new(name=CData.boneData.jointNames[idx])
        for vert in meshDataBl.skinningVerts:
            try:
                assignVertexGroup(vert, CData, mesh_ob)
            except Exception as e:
                if CData.isStatic:
                    log.critical('found skinning verts on static mesh')
                    break

        return mesh_ob
    except Exception as e:
        log.warning("Not in Blender")
        return False

def load_w3_materials_CR2W_Mesh(
        objs: List[Object]
        ,uncook_path: str
        ,materials_bin: str
        ,material_names: str
        ,force_mat_update = False
        ,mat_filename = str
    ):
    
    for idx, mat in enumerate(materials_bin):
        xml_mat_name = material_names[idx]
        log.info(xml_mat_name)
        target_mat = False
        for obj in objs:
            if xml_mat_name in obj.data.materials:
                target_mat = obj.data.materials[xml_mat_name] #None
            if not target_mat:
                for m in obj.data.materials:
                    if m.name in xml_mat_name:
                        log.info("partial material match {m.name} {xml_mat_name}")
                        target_mat = m
                if not target_mat:
                    continue
        if target_mat:
            finished_mat = setup_w3_material_CR2W(uncook_path, target_mat, mat, force_update=force_mat_update, mat_filename=mat_filename)
            for obj in objs:
                if target_mat.name in obj.material_slots:
                    obj.material_slots[target_mat.name].material = finished_mat
                    #finished_mat.name = xml_mat_name
        else:
            pass
        #finished_mat.name = finished_mat.name +"_"+ target_mat.name

def assignVertexGroup(vert, CData, mesh_ob):
    boneIdx = vert.boneId
    vertexWeight = vert.strength
    if vertexWeight != 0:
        # use original index to get current bone name in blender
        boneName = CData.boneData.jointNames[boneIdx]
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

    for v_group in mesh_ob.vertex_groups:
        bone_name = v_group.name
        for vert in me.vertices:
            for group in vert.groups:
                if group.group == v_group.index:
                    vse = VertexSkinningEntry()
                    vse.vertexId = vert.index
                    vse.boneId = bone_name
                    vse.boneId_idx = None
                    vse.strength = group.weight
                    exportMeshdata.skinningVerts.append(vse)

    me.use_auto_smooth = True
    me.calc_normals_split()
    new_normals = [None] * len(me.vertices)
    for l in me.loops:
        new_normals[l.vertex_index] = l.normal[:]

    try:
        for idy, normal in enumerate(new_normals):
                exportMeshdata.normals.append(list(normal))
                exportMeshdata.normalsAll.append(normal[0])
                exportMeshdata.normalsAll.append(normal[1])
                exportMeshdata.normalsAll.append(normal[2])
    except Exception as e:
        raise e # something happened to normals during chunk generation

    loop_nbr = len(me.loops)
    t_pvi = array.array(data_types.ARRAY_INT32, (0,)) * loop_nbr
    t_ls = [None] * len(me.polygons)

    me.loops.foreach_get("vertex_index", t_pvi)
    me.polygons.foreach_get("loop_start", t_ls)

    for loop_start in t_ls:
        exportMeshdata.faces.append([t_pvi[loop_start],t_pvi[loop_start+1],t_pvi[loop_start+2]])

    for vert in me.vertices:
        exportMeshdata.UV_vertex3DCoords.append([0.0, 1.0])
        exportMeshdata.UV2_vertex3DCoords.append([0.0, 1.0])

    for idx, uv_layer in enumerate(me.uv_layers):
        for face in me.polygons:
            for vert_idx, loop_idx in zip(face.vertices, face.loop_indices):
                if idx == 0:
                    exportMeshdata.UV_vertex3DCoords[vert_idx] = (uv_layer.data[loop_idx].uv.to_tuple())
                elif idx == 1:
                    exportMeshdata.UV2_vertex3DCoords[vert_idx] = (uv_layer.data[loop_idx].uv.to_tuple())
                else:
                    log.critical('Ignoing UVs below the first and second.')
                    break

    for vert in me.vertices:
        exportMeshdata.vertex3DCoords.append([vert.co.x, vert.co.y, vert.co.z] )
        if me.color_attributes.active:
            color = me.color_attributes.active.data[vert.index].color
            colarr = []
            for col in color:
                colarr.append(col)
            exportMeshdata.vertexColor.append(colarr)
        else:
            #exportMeshdata.vertexColor.append([1.0, 1.0, 1.0, 1.0])
            exportMeshdata.vertexColor.append([0.0, 0.0, 0.0, 0.0])
    exportMeshdata.meshInfo = SMeshInfos()
    exportMeshdata.meshInfo.numIndices = len(exportMeshdata.faces)*3
    exportMeshdata.meshInfo.numVertices = len(exportMeshdata.vertex3DCoords)

    ## TANGENTS
    UV_SEL = 0 # DiffuseUV
    uv_names = [uvlayer.name for uvlayer in me.uv_layers]
    for idy, name in enumerate(uv_names):
        me.calc_tangents(uvmap=name) if idy == UV_SEL else None
        #break
    for idx, uvlayer in enumerate(me.uv_layers):
        if idx == UV_SEL:
            name = uvlayer.name

            new_tangents = [None] * len(me.vertices)
            new_bitangent = [None] * len(me.vertices)
            for l in me.loops:
                new_tangents[l.vertex_index] = list(l.tangent[:])
                new_bitangent[l.vertex_index] = list((l.bitangent * -1)[:])

    me.free_tangents()
    me.free_normals_split()
    exportMeshdata.tangent_vector = new_tangents
    exportMeshdata.extra_vectors = new_bitangent
    
    return exportMeshdata