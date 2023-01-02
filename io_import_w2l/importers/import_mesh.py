from imp import reload
import os
import json

from CR2W.CR2W_types import getCR2W
from CR2W.Types.BlenderMesh import CommonData
from CR2W.dc_skeleton import create_Skeleton, load_bin_face, load_bin_skeleton
from math import degrees
from math import radians

import bpy

from typing import List, Tuple
from pathlib import Path
from mathutils import Vector, Quaternion, Euler, Matrix
import numpy as np
import array

from io_import_w2l.cloth_util import setup_w3_material_CR2W
from io_import_w2l import get_texture_path, get_uncook_path
from io_import_w2l import file_helpers
from io_import_w2l.CR2W import w3_types
from io_import_w2l.CR2W import read_json_w3
from io_import_w2l.w3_armature_constants import *
import CR2W.dc_mesh
reload(CR2W.dc_mesh)
from  CR2W.dc_mesh import MeshData

from CR2W.Types.SBufferInfos import MMatrix, SBufferInfos, SVertexBufferInfos, SMeshInfos, EMeshVertexType, VertexSkinningEntry

from io_import_w2l.setup_logging_bl import *
log = logging.getLogger(__name__)
### GENERAL SETTINGS

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
                rotate_180:bool = True) -> w3_types.CSkeletalAnimationSet:
    dirpath, file = os.path.split(filename)
    basename, ext = os.path.splitext(file)
    if ext.lower() in ('.w2mesh'):
        with open(filename) as file:
            try:
                (CData, bufferInfos, the_material_names, the_materials, meshName, meshFile) = CR2W.dc_mesh.load_bin_mesh(filename)
                return prepare_mesh_import(CData, bufferInfos, the_material_names, the_materials, meshName, meshFile,
                    do_import_mats,
                    do_import_armature,
                    keep_lod_meshes,
                    do_merge_normals,
                    rotate_180)
            except Exception as e:
                raise e
    else:
        anim = None
    return anim

def prepare_mesh_import(CData, bufferInfos, the_material_names, the_materials, meshName, meshFile,
                do_import_mats,
                do_import_armature,
                keep_lod_meshes,
                do_merge_normals,
                rotate_180):
    #TODO proxy meshes don't have lod0 they start at lod2, should import proxy anyway if requested
    #meshData = meshFile
    created_mesh_bl = []
    for idx, meshDataBl in enumerate(CData.meshDataAllMeshes):
        mat_id = CData.meshDataAllMeshes[idx].meshInfo.materialID
        lod_level = bufferInfos.verticesBuffer[idx].lod
        if not keep_lod_meshes and lod_level > 1 and "proxy" not in meshName:
            break
        #TODO some meshes seem to have no real data and should be discarded, prob a result of auto LOD creation
        #TODO trying to create them crashes blender
        skip = True
        for faces in meshDataBl.faces:
            if faces.count(0) == 3:
                continue
            else:
                skip = False
                break
        try:
            if not skip:
                obj = do_blender_mesh_import(meshDataBl, CData, do_merge_normals)
                obj['witcher_lod_level'] = lod_level
                obj['witcher_mat_id'] = mat_id
                created_mesh_bl.append(obj)
        except Exception as e:
            log.error("warning couldn't create one of the meshes at index ", idx)
    
    import bpy
    lod1 = []
    lod2 = []
    lod3 = []
    lod4 = []
    lods_to_create = [lod1,
                    lod2,
                    lod3,
                    lod4,]

    ##TODO read the right meshinfo when you disgard lod
    for idx, mesh_bl in enumerate(created_mesh_bl):
        # mat_id = CData.meshDataAllMeshes[idx].meshInfo.materialID
        # bufferInfos.verticesBuffer[idx].lod
        
        mat_id = mesh_bl['witcher_mat_id']
        lod_level = mesh_bl['witcher_lod_level']
        
        lod1.append(mesh_bl) if lod_level == 1 else 0
        lod2.append(mesh_bl) if lod_level == 2 else 0
        lod3.append(mesh_bl) if lod_level == 3 else 0
        lod4.append(mesh_bl) if lod_level == 4 else 0
  
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
                if rotate_180:
                    armature_obj.rotation_euler[2] = np.pi
                    bpy.ops.object.transform_apply(location=False, rotation=True, scale=False)
        except Exception as e:
            log.error("Problem creating armature")
        
    # LODS
    bpy.ops.object.mode_set(mode='OBJECT')
    final_bl_meshes = []
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
            if rotate_180:
                joined_obj.select_set(True)
                joined_obj.rotation_euler[2] = np.pi
                bpy.ops.object.transform_apply(location=False, rotation=True, scale=False)
                
            final_bl_meshes.append(joined_obj)

            if (CData.meshInfos[0].vertexType == EMeshVertexType.EMVT_SKINNED and do_import_armature):
                bpy.context.view_layer.objects.active = bpy.data.objects[armature_obj.name]
                bpy.ops.object.parent_set(type="ARMATURE_NAME", xmirror=False, keep_transform=False)
            if not keep_lod_meshes:
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
    if do_import_mats:
        ### MATERIALS
        force_mat_update = True
        uncook_path = get_texture_path(bpy.context)+"\\" #! THE PATH WITH THE TEXTURES NOT THE FBX FILES
        uncook_path_modkit = get_uncook_path(bpy.context)
        xml_path = "w2mesh"
        
        materials = []
        for o in the_materials.Handles:
            if o.Reference is not None:
                materials.append(meshFile.CHUNKS.CHUNKS[o.Reference])
            else:
                material_file_chunks = CR2W.CR2W_reader.load_material(uncook_path_modkit+"\\"+o.DepotPath)
                for chunk in material_file_chunks:
                    if chunk.Type == "CMaterialInstance":
                        materials.append(chunk)
        #material_names = [o.String.split('::')[1] for o in chunk.GetVariableByName('apexMaterialNames').elements]
        load_materials = True
        if load_materials:
            mat_filename = "cake"
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
            del(meshDataBl.vertexColor)

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
        for vert in meshDataBl.skinningVerts:
            assignVertexGroup(vert, CData, mesh_ob)

        return mesh_ob
    except Exception as e:
        log.warning("Not in Blender")
        return False

def assignVertexGroup(vert, CData, mesh_ob):
    boneIdx = vert.boneId
    vertexWeight = vert.strength
    if vertexWeight != 0:
        # use original index to get current bone name in blender
        boneName = CData.boneData.jointNames[boneIdx]
        if boneName:
            vertGroup = mesh_ob.vertex_groups.get(boneName)
            if not vertGroup:
                vertGroup = mesh_ob.vertex_groups.new(name=boneName)
            vertGroup.add([vert.vertexId], vertexWeight, 'REPLACE')



from typing import List, Tuple, Dict
from bpy.types import Image, Material, Object, Node

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
        else:
            pass
        #finished_mat.name = finished_mat.name +"_"+ target_mat.name