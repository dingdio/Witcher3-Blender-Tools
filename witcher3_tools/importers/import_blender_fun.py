import logging
from pathlib import Path
import re
from ..CR2W.CR2W_helpers import Enums
from ..CR2W.CR2W_types import Entity_Type_List
import bpy
import os
from ..importers.import_helpers import MatrixToArray, checkLevel, meshPath, set_blender_object_transform, _transform_real
from mathutils import Matrix, Euler
from math import radians
import time

log = logging.getLogger(__name__)

from .. import cloth_util
from .. import fbx_util
from .. import get_uncook_path
from .. import get_W3_FOLIAGE_PATH
from .. import get_fbx_uncook_path
from .. import get_use_fbx_repo
from .. import get_do_import_redcloth
from ..importers import import_mesh
from ..external_addon_tools import get_srt_addon_status

from bpy_extras.wm_utils.progress_report import (
    ProgressReport,
    ProgressReportSubstep,
)


class lightObject:
    def __init__(self, meshName = "Light Item",
                    translation = False,
                    matrix = False,
                    transform = False,
                    block = False,
                    BlockDataObjectType = Enums.BlockDataObjectType.Mesh):
        self.name = meshName
        self.meshName = meshName
        self.translation = translation
        self.matrix = matrix
        self.transform = transform
        self.type = "Light"
        self.block = block
        self.BlockDataObjectType = BlockDataObjectType

from ..CR2W.common_blender import repo_file
# def repo_file(filepath: str):
#     if filepath.endswith('.fbx'):
#         return os.path.join(bpy.context.preferences.addons['io_import_w2l'].preferences.fbx_uncook_path, filepath)
#     else:
#         return os.path.join(bpy.context.preferences.addons['io_import_w2l'].preferences.uncook_path, filepath)

from .. import get_W3_REDCLOTH_PATH
from ..external_addon_tools import resolve_redcloth_apx

def get_CSectorData(level):
    if level.CSectorData:
        #import entities hold import data
        static_mesh_list = []
        #meshPath entities hold a transform and componants such as import data
        # THIS_ENTITY = meshPath("CSectorData_Transform", False, False, fbx_uncook_path, BasicEngineQsTransform())
        # THIS_ENTITY.type = "Entity"
        for idx, block in enumerate(level.CSectorData.BlockData):
            #TESTING
            this_type = Enums.BlockDataObjectType.getEnum(block.packedObjectType)
            if hasattr(block, 'resourceIndex') and block.resourceIndex < 12:
                this_resource = level.CSectorData.Resources[block.resourceIndex].pathHash
                log.debug(str(block.resourceIndex)+' '+this_resource)

            if block.packedObjectType == Enums.BlockDataObjectType.Mesh:# or block.packedObjectType == Enums.BlockDataObjectType.Invalid:
                mesh_path = level.CSectorData.Resources[block.packedObject.meshIndex].pathHash
                #obj_pos = level.CSectorData.Objects[idx].position
                static_mesh_list.append(meshPath(mesh_path, block.position, MatrixToArray(block.rotationMatrix) ))
            if block.packedObjectType == Enums.BlockDataObjectType.RigidBody:
                mesh_path = level.CSectorData.Resources[block.packedObject.meshIndex].pathHash
                static_mesh_list.append(meshPath(mesh_path, block.position, MatrixToArray(block.rotationMatrix), BlockDataObjectType = Enums.BlockDataObjectType.RigidBody ))
                log.info("found RigidBody in CSectorData")
            if block.packedObjectType == Enums.BlockDataObjectType.Collision:
                mesh_path = level.CSectorData.Resources[block.packedObject.meshIndex].pathHash
                static_mesh_list.append(meshPath(mesh_path, block.position, MatrixToArray(block.rotationMatrix), BlockDataObjectType = Enums.BlockDataObjectType.Collision))
                log.info("found Collision in CSectorData")
            if block.packedObjectType == Enums.BlockDataObjectType.PointLight:
                log.info("found point light in CSectorData")
                static_mesh_list.append(lightObject("PointLight", block.position, MatrixToArray(block.rotationMatrix), block = block, BlockDataObjectType = Enums.BlockDataObjectType.PointLight))
            if block.packedObjectType == Enums.BlockDataObjectType.SpotLight:
                static_mesh_list.append(lightObject("SpotLight", block.position, MatrixToArray(block.rotationMatrix), block = block, BlockDataObjectType = Enums.BlockDataObjectType.SpotLight))
                #light_path = level.CSectorData.Resources[block.resourceIndex].pathHash
                log.info("found spot light in CSectorData")
            if block.packedObjectType == Enums.BlockDataObjectType.Invalid:
                log.info("found point Invalid in CSectorData")
            if block.packedObjectType == Enums.BlockDataObjectType.Cloth:
                log.info("found point Cloth in CSectorData")
            if block.packedObjectType == Enums.BlockDataObjectType.Decal:
                log.info("found point Decal in CSectorData")
            if block.packedObjectType == Enums.BlockDataObjectType.Destruction:
                log.info("found point Destruction in CSectorData")
            if block.packedObjectType == Enums.BlockDataObjectType.Dimmer:
                log.info("found point Dimmer in CSectorData")
            if block.packedObjectType == Enums.BlockDataObjectType.Particles:
                log.info("found point Particles in CSectorData")
        return static_mesh_list
    else:
        return False


def recurLayerCollection(layerColl, collName):
    found = None
    if (layerColl.name == collName):
        return layerColl
    for layer in layerColl.children:
        found = recurLayerCollection(layer, collName)
        if found:
            return found
         
import math
def is_within_distance(mesh_translation, reference_vector, distance_threshold):
    # Calculate the Euclidean distance between the two vectors
    distance = math.sqrt((mesh_translation[0] - reference_vector[0])**2 + 
                        (mesh_translation[1] - reference_vector[1])**2 +
                        (mesh_translation[2] - reference_vector[2])**2)
    
    # Check if the distance is within the threshold
    if distance <= distance_threshold:
        return True
    else:
        return False

def import_light(mesh, parent_transform = False):
    block = mesh.block
    light_data = block.packedObject
    if block.packedObjectType == Enums.BlockDataObjectType.PointLight:
        bpy.ops.object.light_add(type='POINT', radius=1, align='WORLD', location=(0, 0, 0), scale=(1, 1, 1))
        light_obj = bpy.context.selected_objects[:][0]
        light_obj.data.energy = light_data.brightness * 10
        light_obj.data.color[0] = light_data.color.Red/255
        light_obj.data.color[1] = light_data.color.Green/255
        light_obj.data.color[2] = light_data.color.Blue/255
        # do some custom val? #light_obj.data.color[3] = color.Value/255
        light_obj.data.shadow_soft_size = light_data.radius/255
        #set_blender_object_transform(light_obj, component.GetVariableByName('transform').EngineTransform)
        
    elif block.packedObjectType == Enums.BlockDataObjectType.SpotLight:
        bpy.ops.object.light_add(type='SPOT', radius=1, align='WORLD', location=(0, 0, 0), scale=(1, 1, 1))
        light_obj = bpy.context.selected_objects[:][0]
        light_obj.data.energy = light_data.brightness * 3
        light_obj.data.color[0] = light_data.color.Red/255
        light_obj.data.color[1] = light_data.color.Green/255
        light_obj.data.color[2] = light_data.color.Blue/255
        light_obj.data.shadow_soft_size = light_data.radius/255

        #light_obj.data.spot_blend = component.GetVariableByName('innerAngle').Value
        light_obj.data.spot_blend = 0
        light_obj.data.spot_size = light_data.outerAngle
        #light_obj.data.spot_size = component.GetVariableByName('softness').Value



    obj = light_obj
    if parent_transform:
        obj.parent = parent_transform

    if mesh.transform:
        obj.rotation_euler = (0,0,0)
        x, y, z = (
            radians(_transform_real(mesh.transform, "Yaw", 0.0)),
            radians(_transform_real(mesh.transform, "Pitch", 0.0)),
            radians(_transform_real(mesh.transform, "Roll", 0.0)),
        )
        orders =  ['XYZ', 'XZY', 'YXZ', 'YZX', 'ZXY', 'ZYX']
        mat = Euler((x, y, z), orders[2]).to_matrix().to_4x4()

        obj.matrix_world @= mat
        obj.location[0] = _transform_real(mesh.transform, "X", 0.0)
        obj.location[1] = _transform_real(mesh.transform, "Y", 0.0)
        obj.location[2] = _transform_real(mesh.transform, "Z", 0.0)

        if isinstance(mesh.transform, dict) or hasattr(mesh.transform, "Scale_x"):
            obj.scale[0] = _transform_real(mesh.transform, "Scale_x", 1.0)
            obj.scale[1] = _transform_real(mesh.transform, "Scale_y", 1.0)
            obj.scale[2] = _transform_real(mesh.transform, "Scale_z", 1.0)

    if mesh.matrix:
        try:
            log.info(obj.name)
            mat = Matrix()
            #log.info(mat)
            obj.matrix_world = obj.matrix_world @ mat
        except Exception:
            error_message = "ERROR MESH IMPORTER: Can't import: " + mesh.fbxPath()
            log.info(error_message)
    if mesh.translation:
        obj.location[0] = mesh.translation.x
        obj.location[1] = mesh.translation.y
        obj.location[2] = mesh.translation.z
        
    if block.packedObjectType == Enums.BlockDataObjectType.SpotLight:
        # 90 to X in every spotlight
        rotation_euler = light_obj.rotation_euler
        rotation_euler.x += 1.5708  # 90 degrees in radians
        light_obj.rotation_euler = rotation_euler

#global repo_lookup_list

# import cProfile
# import pstats

def loadLevel(levelData, context = None, keep_lod_meshes:bool = False, **kwargs):
    #! profiler = cProfile.Profile()
    #! profiler.enable()
    
    #keep_empty_lods = kwargs.get('keep_empty_lods', False)
    #keep_proxy_meshes = kwargs.get('keep_proxy_meshes', False)
    
    do_import_Mesh = kwargs.get('do_import_Mesh', True)
    do_import_Collision = kwargs.get('do_import_Collision', True)
    do_import_RigidBody = kwargs.get('do_import_RigidBody', True)
    do_import_PointLight = kwargs.get('do_import_PointLight', True)
    do_import_SpotLight = kwargs.get('do_import_SpotLight', True)
    do_import_Entity = kwargs.get('do_import_Entity', True)
    do_enable_name_filter = kwargs.get('do_enable_name_filter', False)
    do_name_filter_regex = kwargs.get('do_name_filter_regex', '')
    
    reference_vector = (115.789, 44.8168, 0.892988)
    distance_threshold = 100.0
    
    if context == None:
        context = bpy.context
    # global repo_lookup_list
    # repo_lookup_list = defaultdict(list)
    # scene = bpy.context.scene
    # for o in scene.objects:
    #     if o.type != 'EMPTY':
    #         continue
    #     if len(o.name) > 4 and o.name[-4] != "." and 'repo_path' in o:
    #         repo_lookup_list[o['repo_path']].append(o)
    levelFile = levelData.layerNode
    errors = ['======Errors======= '+ levelFile]

    ready_to_import = True#checkLevel(levelData)

    #create collection lfor this level
    if ready_to_import:
        collectionFound = False
        repo_path = levelFile.replace(get_uncook_path(bpy.context)+"\\", "")
        for myCol in bpy.data.collections:
            if str(myCol.get("level_path", "")).strip() == repo_path:
                collectionFound = myCol
                log.info("Collection found in scene")
                break
        if collectionFound:
            collection = collectionFound
            layer_collection = bpy.context.view_layer.layer_collection
            layerColl = recurLayerCollection(layer_collection, collection.name)
            bpy.context.view_layer.active_layer_collection = layerColl
        else:
            level_name = os.path.basename(levelFile)
            collection = bpy.data.collections.new(os.path.basename(level_name))
            collection['level_path'] = levelFile
            bpy.context.scene.collection.children.link(collection)
            layer_collection = bpy.context.view_layer.layer_collection.children[collection.name]
            bpy.context.view_layer.active_layer_collection = layer_collection

    #start level import
    if ready_to_import:

        if levelData.Foliage:
            for treeCollection in levelData.Foliage.Trees.elements:
                treeFilePath = treeCollection.TreeType.DepotPath
                for treeTransform in treeCollection.TreeCollection.elements:
                    tree_mesh = meshPath(fbx_uncook_path = get_W3_FOLIAGE_PATH(bpy.context))
                    tree_mesh.meshName = treeFilePath
                    tree_mesh.transform = treeTransform
                    tree_mesh.type = "mesh_foliage"
                    import_single_mesh(tree_mesh, errors, keep_lod_meshes = keep_lod_meshes, **kwargs)
            for treeCollection in levelData.Foliage.Grasses.elements:
                treeFilePath = treeCollection.TreeType.DepotPath
                for treeTransform in treeCollection.TreeCollection.elements:
                    tree_mesh = meshPath(fbx_uncook_path = get_W3_FOLIAGE_PATH(bpy.context))
                    tree_mesh.meshName = treeFilePath
                    tree_mesh.transform = treeTransform
                    tree_mesh.type = "mesh_foliage"
                    import_single_mesh(tree_mesh, errors, keep_lod_meshes = keep_lod_meshes, **kwargs)

        mesh_list = get_CSectorData(levelData)
        if mesh_list:
            bpy.ops.object.empty_add(type="PLAIN_AXES", radius=1)
            empty_transform = bpy.context.object
            empty_transform.name = "CSectorData"
            
            bpy.ops.object.empty_add(type="PLAIN_AXES", radius=1)
            Collision_transform = bpy.context.object
            Collision_transform.name = "Collision"
            Collision_transform.parent = empty_transform
            bpy.ops.object.empty_add(type="PLAIN_AXES", radius=1)
            Rigid_transform = bpy.context.object
            Rigid_transform.name = "Rigid"
            Rigid_transform.parent = empty_transform
            bpy.ops.object.empty_add(type="PLAIN_AXES", radius=1)
            Mesh_transform = bpy.context.object
            Mesh_transform.name = "Mesh"
            Mesh_transform.parent = empty_transform
            bpy.ops.object.empty_add(type="PLAIN_AXES", radius=1)
            PointLight_transform = bpy.context.object
            PointLight_transform.name = "PointLight"
            PointLight_transform.parent = empty_transform
            bpy.ops.object.empty_add(type="PLAIN_AXES", radius=1)
            SpotLight_transform = bpy.context.object
            SpotLight_transform.name = "SpotLight"
            SpotLight_transform.parent = empty_transform

            #wm = context.window_manager
            #wm.progress_begin(0, len(mesh_list))
            total_loops = len(mesh_list)
            mesh:meshPath
            for idx, mesh in enumerate(mesh_list):
                #wm.progress_update(idx)
                #!REMOVE
                # if not is_within_distance((mesh.translation.x, mesh.translation.y, mesh.translation.z), reference_vector, distance_threshold):
                #     continue
                #!REMOVE
                    
                if re.search(do_name_filter_regex, mesh.fileName()) if do_enable_name_filter else True:
                    progress_msg = f"{idx+1}/{total_loops} - {os.path.basename(mesh.meshName)}"
                    if mesh.BlockDataObjectType == Enums.BlockDataObjectType.Mesh and do_import_Mesh:
                        import_single_mesh(mesh, errors, Mesh_transform, keep_lod_meshes = keep_lod_meshes, **kwargs)
                        #continue
                    elif mesh.BlockDataObjectType == Enums.BlockDataObjectType.Collision and do_import_Collision:
                        import_single_mesh(mesh, errors, Collision_transform, keep_lod_meshes = keep_lod_meshes, **kwargs)
                        #continue
                    elif mesh.BlockDataObjectType == Enums.BlockDataObjectType.RigidBody and do_import_RigidBody:
                        import_single_mesh(mesh, errors, Rigid_transform, keep_lod_meshes = keep_lod_meshes, **kwargs)
                        #continue
                    elif mesh.BlockDataObjectType == Enums.BlockDataObjectType.PointLight and do_import_PointLight:
                        import_light(mesh, PointLight_transform)
                    elif mesh.BlockDataObjectType == Enums.BlockDataObjectType.SpotLight and do_import_SpotLight:
                        import_light(mesh, SpotLight_transform)
                    progress_msg += " " * (80 - len(progress_msg))
                    log.info(progress_msg)
            #wm.progress_end()

        if do_import_Entity:
            for INCLUDE_OBJECT in levelData.includes:
                for ENTITY_OBJECT in INCLUDE_OBJECT.Entities:
                    #!REMOVE
                    # if not is_within_distance((ENTITY_OBJECT.transform.X, ENTITY_OBJECT.transform.Y, ENTITY_OBJECT.transform.Z), reference_vector, distance_threshold):
                    #     continue
                    #!REMOVE
                    if ENTITY_OBJECT.type in Entity_Type_List:
                        import_gameplay_entity(ENTITY_OBJECT, errors, keep_lod_meshes = keep_lod_meshes, **kwargs)
                
            total_loops = len(levelData.Entities)
            for idx, ENTITY_OBJECT in enumerate(levelData.Entities):
                #!REMOVE
                # if not is_within_distance((ENTITY_OBJECT.transform.X, ENTITY_OBJECT.transform.Y, ENTITY_OBJECT.transform.Z), reference_vector, distance_threshold):
                #     continue
                #!REMOVE
                #if True: #ENTITY_OBJECT.name == "basement_doors4 (CDoor)":
                if re.search(do_name_filter_regex, ENTITY_OBJECT.name) if do_enable_name_filter else True:
                    progress_msg = f"{idx+1}/{total_loops} - {ENTITY_OBJECT.name}"
                    if ENTITY_OBJECT.type in Entity_Type_List:
                        import_gameplay_entity(ENTITY_OBJECT, errors, keep_lod_meshes = keep_lod_meshes, **kwargs)
                    progress_msg += " " * (80 - len(progress_msg))
                    log.info(progress_msg)
            # for idx, ENTITY_OBJECT in enumerate(levelData.meshes):
            #     if ENTITY_OBJECT.type == "Mesh": #A SINGLE MESH WITH NO COMPONENTS
            #         import_single_mesh(ENTITY_OBJECT, errors, **kwargs)
            #         #log.info(idx, ENTITY_OBJECT.translation.x,ENTITY_OBJECT.translation.y,ENTITY_OBJECT.translation.z)
            #     if ENTITY_OBJECT.type == "CGameplayEntity" or ENTITY_OBJECT.type == "CSectorData": #A ENTITY WITH A TRANSFORM AND LIST OF MESH/LIGHTS
            #         import_gameplay_entity(ENTITY_OBJECT, errors)
            #     if ENTITY_OBJECT.type == "CEntity": # A MESH WITH COMPONENTS
            #         bpy.ops.object.empty_add(type="PLAIN_AXES", radius=1)
            #         Entity_transform = bpy.context.object
            #         Entity_transform.name = ENTITY_OBJECT.meshName #"CGameplayEntity_empty_transform"
            #         for comp in ENTITY_OBJECT.components:
            #             import_gameplay_entity(comp, errors, Entity_transform)
            #         set_blender_object_transform(Entity_transform, ENTITY_OBJECT.transform)

    for error in errors:
        log.error(error)
    
        
    #! #################
    #!     #PROFILER
    #! #################
    #! profiler.disable()
    
    #! # Dump profiling data to file
    #! with open('profile_results.log', 'w') as f:
    #!     profiler.dump_stats(f.name)

    #! # Read profiling data from file and print to log file
    #! with open('log_file.txt', 'w') as log_file:
    #!     stats = pstats.Stats('profile_results.log', stream=log_file)
    #!     stats.sort_stats('cumulative')
    #!     stats.print_stats()
    
    return {'FINISHED'}

from bpy.types import Object, Mesh

def repo_in_scene(dct, path):
    if path in dct.keys():
        return True
    else:
        return False

def check_if_empty_already_in_scene(repo_path):
    start_time1 = time.time()
    for o in bpy.context.scene.objects:
        if o.type != 'EMPTY':
            continue
        if len(o.name) > 4 and o.name[-4] != "." and 'repo_path' in o and o['repo_path'] == repo_path:
    #if repo_path in repo_lookup_list.keys(): repo_in_scene(repo_lookup_list, repo_path):
        #o = repo_lookup_list[repo_path][0]
            log.info('Check Mesh found in %f seconds.', time.time() - start_time1)
            start_time2 = time.time()
            #log.info("COPYING", o['repo_path'])
            new_obj = o.copy()
            for ch_obj in o.children:
                new_ch_obj = ch_obj.copy()
                new_ch_obj.parent = new_obj
                bpy.context.collection.objects.link(new_ch_obj)
            bpy.context.collection.objects.link(new_obj)
            x, y, z = (radians(0), radians(0), radians(0))
            mat = Euler((x, y, z)).to_matrix().to_4x4()
            new_obj.matrix_world = mat
            new_obj.matrix_local = mat
            new_obj.matrix_basis = mat

            new_obj.location[0] = 0
            new_obj.location[1] = 0
            new_obj.location[2] = 0
            new_obj.scale[0] = 1
            new_obj.scale[1] = 1
            new_obj.scale[2] = 1
            new_obj.parent = None
            log.info('Check Mesh Finished importing in %f seconds.', time.time() - start_time2)
            return new_obj
    return False

def check_if_mesh_already_in_scene(repo_path):

    start_time1 = time.time()
    # name = Path(repo_path).stem+"_Mesh_lod0"
    # try:
    #     o = bpy.context.scene.objects[name]
    # except Exception as e:
    #     try:
    #         name = Path(repo_path).stem+"_Mesh"
    #         o = bpy.context.scene.objects[name]
    #     except Exception as e:
    #         return False
    # #else:
    for o in bpy.context.scene.objects:
        if o.type != 'MESH':
            continue
        if o.name[-4] != "." and 'repo_path' in o and o['repo_path'] == repo_path:
            log.info('Check Mesh found in %f seconds.', time.time() - start_time1)
            start_time2 = time.time()
            #log.info("COPYING", o['repo_path'])
            new_obj = o.copy()
            #new_obj.data = o.data.copy()
            #new_obj.animation_data_clear()
            bpy.context.collection.objects.link(new_obj)

            # new_obj.rotation_euler[0] = 0
            # new_obj.rotation_euler[1] = 0
            # new_obj.rotation_euler[2] = 0
            # new_obj.rotation_euler = (0,0,0)
            x, y, z = (radians(0), radians(0), radians(0))
            mat = Euler((x, y, z)).to_matrix().to_4x4()
            new_obj.matrix_world = mat
            new_obj.matrix_local = mat
            new_obj.matrix_basis = mat

            new_obj.location[0] = 0
            new_obj.location[1] = 0
            new_obj.location[2] = 0
            new_obj.scale[0] = 1
            new_obj.scale[1] = 1
            new_obj.scale[2] = 1
            new_obj.parent = None
            log.info('Check Mesh Finished importing in %f seconds.', time.time() - start_time2)
            return new_obj
    return False

def _import_foliage_mesh(mesh: meshPath):
    """Import a foliage tree mesh using SRT (SpeedTree) if available, FBX as fallback."""
    srt_status = get_srt_addon_status()
    if srt_status["enabled"]:
        try:
            srt_path = repo_file(mesh.meshName)
            if srt_path and os.path.exists(srt_path):
                from ..ui.ui_file_browser import (
                    _export_srt_textures_for_import,
                    _prepare_srt_lod0_json,
                    _snapshot_srt_import_state,
                    _flatten_srt_import_collections,
                )
                from .. import get_all_addon_prefs
                context = bpy.context
                prefs = get_all_addon_prefs(context)
                use_custom_grouping = bool(getattr(prefs, "ab_srt_custom_grouping", True))
                lod0_only = bool(getattr(prefs, "ab_srt_lod0_only", True))

                srt_snapshot = _snapshot_srt_import_state(context) if use_custom_grouping else {}
                tex_stats = _export_srt_textures_for_import(
                    context, srt_path, mesh.meshName, loadmods=False,
                )
                import_path = tex_stats.get("import_path") or srt_path
                if lod0_only:
                    import_path = _prepare_srt_lod0_json(import_path)
                result = getattr(bpy.ops, "import").srt_json(filepath=import_path)
                if 'FINISHED' in result:
                    if use_custom_grouping:
                        _flatten_srt_import_collections(context, import_path, srt_snapshot)
                    return
                log.warning("SRT import failed for %s, falling back to FBX", mesh.meshName)
            else:
                log.warning("SRT file not found: %s, falling back to FBX", mesh.meshName)
        except Exception as e:
            log.warning("SRT import error for %s: %s, falling back to FBX", mesh.meshName, e)
    # Fallback to FBX
    bpy.ops.import_scene.fbx(filepath=mesh.fbxPath())


def import_single_mesh(mesh:meshPath, errors, parent_transform = False, keep_lod_meshes = False, version = 999, **kwargs):
    use_fbx = get_use_fbx_repo(bpy.context)

    obj = check_if_empty_already_in_scene(mesh.meshName)
    # if keep_lod_meshes:
    #     obj = check_if_empty_already_in_scene(mesh.meshName)
    # else:
    #     obj = check_if_mesh_already_in_scene(mesh.meshName)
    #obj = False
    if not obj:
        # if keep_lod_meshes:
        #     bpy.ops.object.empty_add(type="PLAIN_AXES", radius=1)
        #     obj = bpy.context.object
        bpy.ops.object.empty_add(type="PLAIN_AXES", radius=1)
        obj = bpy.context.object
        try:
            if mesh.type == "mesh_foliage":
                _import_foliage_mesh(mesh)
            else:
                if use_fbx and os.path.exists(mesh.fbxPath()):
                    fbx_util.importFbx(mesh.fbxPath(),mesh.fileName(),mesh.fileName(), keep_lod_meshes=keep_lod_meshes)
                elif not use_fbx:
                    import_mesh.import_mesh(repo_file(mesh.meshName, version), keep_lod_meshes = keep_lod_meshes, keep_empty_lods = kwargs.get('keep_empty_lods', False), keep_proxy_meshes = kwargs.get('keep_proxy_meshes', False))
                else:
                    log.warning("Can't find FBX file %s", mesh.fbxPath())
                    bpy.ops.mesh.primitive_cube_add()
                    objs = bpy.context.selected_objects[:]
                    objs[0].color = (0,0,1,1)
                    objs[0].name = "ERROR_CUBE"
                    err_mat = bpy.data.materials.new("ERROR_CUBE_MAT")
                    err_mat.use_nodes = True
                    principled = err_mat.node_tree.nodes['Principled BSDF']
                    principled.inputs['Base Color'].default_value = (0,0,1,1)
                    objs[0].data.materials.append(err_mat)

        except Exception:
            log.exception("Problem importing mesh %s", mesh.meshName)
            raise
        try:
            
            objs = bpy.context.selected_objects[:]
            #if keep_lod_meshes:
            obj.name = Path(mesh.meshName).stem
            obj['repo_path'] = mesh.meshName
            for subobj in objs:
                subobj.parent = obj
            # else:
            #     obj = objs[0]
            #     obj['repo_path'] = mesh.meshName
            #apply scale
            bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
        except Exception:
            #usually tried to do something with materials and failed
            log.exception("Problem finalizing imported mesh %s", mesh.meshName)
            return
    if parent_transform:
        obj.parent = parent_transform

    if mesh.transform:
        obj.rotation_euler = (0,0,0)
        #THIS WORKS?
        x, y, z = (
            radians(_transform_real(mesh.transform, "Yaw", 0.0)),
            radians(_transform_real(mesh.transform, "Pitch", 0.0)),
            radians(_transform_real(mesh.transform, "Roll", 0.0)),
        )
        orders =  ['XYZ', 'XZY', 'YXZ', 'YZX', 'ZXY', 'ZYX']
        mat = Euler((x, y, z), orders[2]).to_matrix().to_4x4()

        rotate_180 = False
        if rotate_180:
            mat[0][0], mat[0][1], mat[0][2] = -mat[0][0], -mat[0][1], mat[0][2]
            mat[1][0], mat[1][1], mat[1][2] = -mat[1][0], -mat[1][1], mat[1][2]
            mat[2][0], mat[2][1], mat[2][2] = -mat[2][0], -mat[2][1], mat[2][2]
        else:
            mat[0][0], mat[0][1], mat[0][2] = mat[0][0], mat[0][1], mat[0][2]
            mat[1][0], mat[1][1], mat[1][2] = mat[1][0], mat[1][1], mat[1][2]
            mat[2][0], mat[2][1], mat[2][2] = mat[2][0], mat[2][1], mat[2][2]

        obj.matrix_world @= mat
        # obj.rotation_euler[0] = mesh.transform.Pitch
        # obj.rotation_euler[1] = mesh.transform.Yaw
        # obj.rotation_euler[2] = mesh.transform.Roll
        obj.location[0] = _transform_real(mesh.transform, "X", 0.0)
        obj.location[1] = _transform_real(mesh.transform, "Y", 0.0)
        obj.location[2] = _transform_real(mesh.transform, "Z", 0.0)

        #foliage transforms don't have scale
        if isinstance(mesh.transform, dict) or hasattr(mesh.transform, "Scale_x"):
            obj.scale[0] = _transform_real(mesh.transform, "Scale_x", 1.0)
            obj.scale[1] = _transform_real(mesh.transform, "Scale_y", 1.0)
            obj.scale[2] = _transform_real(mesh.transform, "Scale_z", 1.0)
        # else:
        #     obj.scale[0] =0.01
        #     obj.scale[1] =0.01
        #     obj.scale[2] =0.01
    if mesh.matrix:
        try:
            #obj = bpy.context.selected_objects[:][0]
            #MATRIX PART
            log.info(obj.name)
            mat = Matrix()

            rotate_180 = False
            if rotate_180:
                mat[0][0], mat[0][1], mat[0][2] = -mesh.matrix[0][0], -mesh.matrix[1][0], mesh.matrix[2][0]
                mat[1][0], mat[1][1], mat[1][2] = -mesh.matrix[0][1], -mesh.matrix[1][1], mesh.matrix[2][1]
                mat[2][0], mat[2][1], mat[2][2] = -mesh.matrix[0][2], -mesh.matrix[1][2], mesh.matrix[2][2]
            else:
                mat[0][0], mat[0][1], mat[0][2] = mesh.matrix[0][0], mesh.matrix[1][0], mesh.matrix[2][0]
                mat[1][0], mat[1][1], mat[1][2] = mesh.matrix[0][1], mesh.matrix[1][1], mesh.matrix[2][1]
                mat[2][0], mat[2][1], mat[2][2] = mesh.matrix[0][2], mesh.matrix[1][2], mesh.matrix[2][2]
            #log.info(mat)
            obj.matrix_world = obj.matrix_world @ mat
        except Exception:
            error_message = "ERROR MESH IMPORTER: Can't import: " + mesh.fbxPath()
            log.info(error_message)
            errors.append(error_message)
    if mesh.translation:
        obj.location[0] = mesh.translation.x
        obj.location[1] = mesh.translation.y
        obj.location[2] = mesh.translation.z

MeshComponent_Type_List = ['CStaticMeshComponent',
                            'CMeshComponent',
                            'CRigidMeshComponent',
                            "CBgMeshComponent",
                            "CBgNpcItemComponent",
                            "CBoatBodyComponent",
                            "CDressMeshComponent",
                            "CFurComponent",
                            "CImpostorMeshComponent",
                            "CMergedMeshComponent",
                            "CMergedShadowMeshComponent",
                            "CMorphedMeshComponent",
                            "CNavmeshComponent",
                            "CRigidMeshComponentCooked",
                            "CScriptedDestroyableComponent",
                            "CWindowComponent"]

def getDataBufferMesh(entity):
    mesh_list = []
    cloth_list = []
    if hasattr(entity, "streamingDataBuffer") and entity.streamingDataBuffer:
        for chunk in entity.streamingDataBuffer.CHUNKS.CHUNKS:
            if chunk.name in Entity_Type_List:
                log.info("Found an entity in data buffer??")
            if chunk.name in MeshComponent_Type_List:
                mesh_list.append(meshPath(fbx_uncook_path = get_fbx_uncook_path(bpy.context)).static_from_chunk(chunk))
            
            if chunk.name in "CClothComponent":
                cloth_list.append(chunk)

    return (mesh_list, cloth_list)

from .. import get_witcher2_game_path

def import_single_component(component, parent_obj, keep_lod_meshes = False, **kwargs):
    if component.name == "CMeshComponent" or component.name == "CStaticMeshComponent":
        try:
            mesh = meshPath(fbx_uncook_path = get_fbx_uncook_path(bpy.context)).static_from_chunk(component)
            # if component.get_CR2W_version() <= 115:
            #     mesh.uncook_path = get_witcher2_game_path(bpy.context) + '\\data'
            import_single_mesh(mesh, [], parent_obj, keep_lod_meshes = keep_lod_meshes, version = component.get_CR2W_version(), **kwargs)
        except Exception as e:
            log.critical('import_single_component mesh fail') #w2 has embedded here??
    elif component.name == "CPointLightComponent":
        bpy.ops.object.light_add(type='POINT', radius=1, align='WORLD', location=(0, 0, 0), scale=(1, 1, 1))
        light_obj = bpy.context.selected_objects[:][0]
        light_obj.parent = parent_obj
        if component.GetVariableByName('brightness'):
            light_obj.data.energy = component.GetVariableByName('brightness').Value * 10

        
        COLOR = component.GetVariableByName('color')
        if COLOR:
            for color in COLOR.More:
                if color.theName == "Red":
                    light_obj.data.color[0] = color.Value/255
                elif color.theName == "Green":
                    light_obj.data.color[1] = color.Value/255
                elif color.theName == "Blue":
                    light_obj.data.color[2] = color.Value/255
                elif color.theName == "Alpha":
                    pass # do some custom val?
                    #light_obj.data.color[3] = color.Value/255
        RADIUS = component.GetVariableByName('radius')
        if RADIUS:
            light_obj.data.shadow_soft_size = RADIUS.Value
        if component.GetVariableByName('transform'):
            set_blender_object_transform(light_obj, component.GetVariableByName('transform').EngineTransform)
    
    elif component.name == "CSpotLightComponent":
        bpy.ops.object.light_add(type='SPOT', radius=1, align='WORLD', location=(0, 0, 0), scale=(1, 1, 1))
        light_obj = bpy.context.selected_objects[:][0]
        light_obj.parent = parent_obj
        light_obj.data.energy = component.GetVariableByName('brightness').Value * 3

        COLOR = component.GetVariableByName('color')
        if COLOR:
            for color in COLOR.More:
                if color.theName == "Red":
                    light_obj.data.color[0] = color.Value/255
                elif color.theName == "Green":
                    light_obj.data.color[1] = color.Value/255
                elif color.theName == "Blue":
                    light_obj.data.color[2] = color.Value/255
                elif color.theName == "Alpha":
                    pass # do some custom val?
                    #light_obj.data.color[3] = color.Value/255
        RADIUS = component.GetVariableByName('radius')
        if RADIUS:
            light_obj.data.shadow_soft_size = RADIUS.Value
        if component.GetVariableByName('transform'):
            set_blender_object_transform(light_obj, component.GetVariableByName('transform').EngineTransform)
            #TODO should add 90 to X in every spotlight so it matches engine
            rotation_euler = light_obj.rotation_euler
            rotation_euler.x += 1.5708  # 90 degrees in radians
            light_obj.rotation_euler = rotation_euler

        #light_obj.data.spot_blend = component.GetVariableByName('innerAngle').Value
        light_obj.data.spot_blend = 0
        light_obj.data.spot_size = component.GetVariableByName('outerAngle').Value
        #light_obj.data.spot_size = component.GetVariableByName('softness').Value

def import_gameplay_entity(ENTITY_OBJECT, errors, parent_obj = False, keep_lod_meshes = False, **kwargs):
    #TRANSFORM FOR THIS ENTITY
    bpy.ops.object.empty_add(type="PLAIN_AXES", radius=1)
    empty_transform = bpy.context.object

    if parent_obj:
        empty_transform.name = ENTITY_OBJECT.name+"_SUB" # "CGameplayEntity_empty_transform"
        empty_transform.parent = parent_obj
    else:
        empty_transform.name = ENTITY_OBJECT.name

    try:
        (mesh_list, cloth_list) = getDataBufferMesh(ENTITY_OBJECT)
    except Exception as e:
        raise e
    if mesh_list:
        for mesh in mesh_list:
            import_single_mesh(mesh, errors, empty_transform, keep_lod_meshes = keep_lod_meshes, **kwargs)
    if cloth_list:
        for chunk in cloth_list:
            try:
                if not get_do_import_redcloth(bpy.context):
                    continue
                cloth_name = chunk.GetVariableByName('name').String.String
                resource = chunk.GetVariableByName('resource').Handles[0].DepotPath
                apx_info = resolve_redcloth_apx(bpy.context, resource, loadmods=False)
                resource_apx = apx_info.get("apx_path", "")
                if not resource_apx or not os.path.isfile(resource_apx):
                    log.warning(
                        "Skipping redcloth import for %s: %s",
                        resource,
                        apx_info.get("message") or apx_info.get("status"),
                    )
                    continue
                resource = repo_file(resource)
                cloth_arma = cloth_util.importCloth(
                    False,
                    resource_apx,
                    True,
                    False,
                    True,
                    resource,
                    "CClothComponent",
                    cloth_name,
                )
                if cloth_arma:
                    cloth_arma.parent = empty_transform
            except Exception as e:
                log.warning("Problem with cloth import: %s", e)
    
    for component in ENTITY_OBJECT.Components:
        import_single_component(component, empty_transform, keep_lod_meshes = keep_lod_meshes, **kwargs)
    #MESH THIS ENTITY HAS
    # for mesh in ENTITY_OBJECT.static_mesh_list:
    #     import_single_mesh(mesh, errors, empty_transform, **kwargs)
    if ENTITY_OBJECT.isCreatedFromTemplate:
        empty_transform['entity_type'] = ENTITY_OBJECT.type
        empty_transform['template'] = ENTITY_OBJECT.templatePath

    
        #TODO work for all animated objects
        if '(CDoor)' in ENTITY_OBJECT.name:
            from ..importers import import_entity
            ent_template = import_entity.import_ent_template(ENTITY_OBJECT.template.layerNode, False, 0, empty_transform)
            ent_template.parent = empty_transform
            pass
        else:
            if ENTITY_OBJECT.template.includes:
                bpy.ops.object.empty_add(type="PLAIN_AXES", radius=1)
                include_transform = bpy.context.object
                include_transform.name = "INCLUDES"
                include_transform.parent = empty_transform
                for INCLUDE_OBJECT in ENTITY_OBJECT.template.includes:
                    for inc_entity in INCLUDE_OBJECT.Entities:
                        if inc_entity.type in Entity_Type_List:
                            import_gameplay_entity(inc_entity, errors, include_transform, keep_lod_meshes = keep_lod_meshes)
            for entity in ENTITY_OBJECT.template.Entities:
                import_gameplay_entity(entity, errors, empty_transform, keep_lod_meshes = keep_lod_meshes)
                # mesh_list = getDataBufferMesh(entity)
                # for mesh in mesh_list:
                #     import_single_mesh(mesh, errors, empty_transform, **kwargs)
                # for component in entity.Components:
                #     import_single_component(component, empty_transform, **kwargs)

    if ENTITY_OBJECT.transform:
        set_blender_object_transform(empty_transform, ENTITY_OBJECT.transform)


