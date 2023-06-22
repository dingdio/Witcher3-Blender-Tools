from io_import_w2l.setup_logging_bl import *
log = logging.getLogger(__name__)

import json
import os
import re
import bpy
import numpy as np
from pathlib import Path

import addon_utils
from io_import_w2l import import_rig, get_uncook_path, get_W3_REDCLOTH_PATH
#from io_import_w2l import settings
from io_import_w2l import fbx_util
from io_import_w2l import cloth_util
from io_import_w2l import constrain_util
from io_import_w2l.CR2W import read_json_w3
from io_import_w2l.CR2W import w3_types
from io_import_w2l.CR2W.dc_entity import load_bin_entity
from io_import_w2l.CR2W.CR2W_types import EngineTransform
from io_import_w2l.importers.import_helpers import set_blender_object_transform

from mathutils import Euler
from math import radians

# def repo_file(filepath: str):
#     if filepath.endswith('.fbx'):
#         return os.path.join(bpy.context.preferences.addons['io_import_w2l'].preferences.fbx_uncook_path, filepath)
#     else:
#         return os.path.join(bpy.context.preferences.addons['io_import_w2l'].preferences.uncook_path, filepath)
#     #repo = "D:/Witcher_uncooked_clean/raw_ent/"
#     #return settings.get().repopath+filepath
addon_name = "io_import_w2l"
def repo_file(filepath: str, version = 999):
    
    try:
        fbx_uncook_path = bpy.context.preferences.addons[addon_name].preferences.fbx_uncook_path
        uncook_path = bpy.context.preferences.addons[addon_name].preferences.uncook_path
        
        if version <= 115:
            fbx_uncook_path = bpy.context.preferences.addons[addon_name].preferences.fbx_uncook_path
            uncook_path = bpy.context.preferences.addons[addon_name].preferences.witcher2_game_path + '\\data'
    except Exception as e:
        fbx_uncook_path = "E:\\w3_uncook\\FBXs"
        uncook_path = "E:\\w3.modding\\modkit\\r4data"
        if version <= 115:
            fbx_uncook_path = "E:\\w3_uncook\\FBXs"
            uncook_path = "G:\\GOG Games\\The Witcher 2\\data"

    if filepath.endswith('.fbx'):
        return os.path.join(fbx_uncook_path, filepath)
    else:
        return os.path.join(uncook_path, filepath)



def fixed(entity, version = 999):
    use_fbx = False
    ext = ".fbx" if use_fbx else ".w2mesh"
    suffix ="" #"_CONVERT_"
    entity.MovingPhysicalAgentComponent.skeleton = repo_file(entity.MovingPhysicalAgentComponent.skeleton, version)#+".json";

    for appearance in entity.appearances:
        for template in appearance.includedTemplates:
            for chunk in template['chunks']:
                if "mesh" in chunk:
                    chunk['mesh'] = repo_file(chunk['mesh'].replace(".w2mesh", suffix+ext), version)
                if chunk['type'] == "CClothComponent":
                    resource = chunk['resource']
                    chunk['resource'] = repo_file(resource, version)
                    chunk['resource_apx'] = get_W3_REDCLOTH_PATH(bpy.context)+"\\"+resource.replace(".redcloth", ".apx")
                if "morphSource" in chunk:
                    chunk['morphSource'] = repo_file(chunk['morphSource'].replace(".w2mesh", suffix+ext), version)
                if "morphTarget" in chunk:
                    chunk['morphTarget'] = repo_file(chunk['morphTarget'].replace(".w2mesh", suffix+ext), version)
                if "skeleton" in chunk:
                    chunk['skeleton'] = repo_file(chunk['skeleton'], version)#+".json"
                if "dyng" in chunk:
                    chunk['dyng'] = repo_file(chunk['dyng'], version)#+".json"
                if "mimicFace" in chunk:
                    chunk['mimicFace'] = repo_file(chunk['mimicFace'], version)#+".json"
    if entity.staticMeshes:
        for chunk in entity.staticMeshes.get('chunks', []):
            if "mesh" in chunk:
                chunk['mesh'] = repo_file(chunk['mesh'].replace(".w2mesh", suffix+ext), version)
            if "skeleton" in chunk:
                chunk['skeleton'] = repo_file(chunk['skeleton'], version)#+".json"
    return entity

def isChildNode(chunkIndex, templateChunks):
    for chunk in templateChunks:
        if "child" in chunk and chunk['child'] == chunkIndex:
            return True
    return False

def GetChunkNS(chunkIndex, templateChunks, index):
    for chunk in templateChunks:
        if chunk['chunkIndex'] == chunkIndex:
            return chunk['type']+str(index)+str(chunk['chunkIndex'])

#global GLOBAL_appearances
def NewListItem( treeList, node):
    item = treeList.add()
    item.name = node.name
    return item

def NewAnimsetListItem( treeList, path, name):
    item = treeList.add()
    if path:
        item.path = path
    if name:
        item.name = name
    return item


things = []
def class_fun(thing):
    try:
        if hasattr(thing,'theType') and thing.theType == 'CGUID' or hasattr(thing,'theType') and thing.theType == 'EPathEngineCollision':
            return None
        things.append(thing)
        return vars(thing)
    except Exception as e:
        return None

def test_load_entity(filename) ->  w3_types.Entity:
    #TODO add this custom json after normal bin file is loaded
    if filename.endswith("geralt_player.w2ent") or filename.endswith(r"player\player.w2ent"):
        RES_DIR = Path(__file__)
        RES_DIR = str(Path(RES_DIR).parents[1])
        filename = os.path.join(RES_DIR, r"CR2W\data\geralt_CUSTOM.w2ent.json")

    dirpath, file = os.path.split(filename)
    basename, ext = os.path.splitext(file)
    if ext.lower() in ('.json'):
        entity = read_json_w3.readEntFile(filename)
    elif ext.lower().endswith('.w2ent') or ext.lower().endswith('.w3app'):
        bin_data = load_bin_entity(filename)
        
        the_json = json.dumps(bin_data,indent=2, default=class_fun, sort_keys=False)
        class_to_json = json.loads(the_json)
        entity = w3_types.Entity()
        entity = entity.from_json(class_to_json)
    else:
        entity = None
    return entity

def import_ent_template(filename, load_face_poses = False, import_apperance = 0, parent_transform = None):
    app_idx = import_apperance-1
    context = bpy.context
    entity = test_load_entity(filename)
    entity = fixed(entity, entity.version)
    base_animation_skeleton = import_MovingPhysicalAgentComponent(entity, parent_transform)
    main_arm_obj = base_animation_skeleton
    
    if not main_arm_obj:
        return None
    rig_settings = main_arm_obj.data.witcherui_RigSettings

    rig_settings.jsonData = json.dumps(entity,indent=2, default=vars, sort_keys=False)

    treeList = rig_settings.app_list
    treeList.clear()
    if entity.appearances:
        for idx, node in enumerate(entity.appearances):
            item = NewListItem(treeList, node)
            rig_settings.app_list_index = app_idx if idx == app_idx else 0
            import_from_list_item(context, item, False) if idx == app_idx else 0
    rig_settings.app_list_index = 0 if app_idx == -1 else app_idx
    rig_settings.main_entity_skeleton = entity.MovingPhysicalAgentComponent.skeleton
    if get_uncook_path(context) in filename:
        rig_settings.repo_path = filename.replace(get_uncook_path(context)+"\\", '')
    else:
        rig_settings.repo_path = filename
        pass # find the entity another way
    rig_settings.entity_name = Path(filename).stem

    #Find the first (and only?) CMimicComponent and use it to import face animations
    for ent in entity.appearances:
        for template in ent.includedTemplates:
            for chunk in template['chunks']:
                if chunk['type'] == "CMimicComponent" and 'mimicFace' in chunk:
                    rig_settings.main_face_skeleton = chunk['mimicFace']
                    break
    animset_list = rig_settings.animset_list
    animset_list.clear()
    for animsetset in entity.CAnimAnimsetsParam:
        NewAnimsetListItem(animset_list, animsetset['name']+":", animsetset['name'])
        for path in animsetset['animationSets']:
            NewAnimsetListItem(animset_list, path, animsetset['name'])
    # for mimic_sets in entity.CAnimMimicParam[0]:
    #     for mimic_set in mimic_sets:
    #         NewAnimsetListItem(animset_list, mimic_set['name']+":", mimic_set['name'])
    #         for path in mimic_set['animationSets']:
    #             NewAnimsetListItem(animset_list, path, animsetset['name'])
    return main_arm_obj

def inList(name, mylist):
    for el in mylist:
        if el in name:
            return True
    return False

def create_on_prop(armobj: bpy.types.Armature,
                   current_app_list_index:int,
                   obj_to_hide:bpy.types.Object,
                   prop_name:str):
    driver_curve = obj_to_hide.driver_add(prop_name)
    driver = driver_curve.driver
    channel = "idx_on_app_list"
    driver.expression = "idx_on_app_list != "+str(current_app_list_index)
    var = driver.variables.get(channel)
    if var is None:
        var = driver.variables.new()
    var.type = "SINGLE_PROP"
    var.name = channel
    target = var.targets[0]
    target.id_type = "ARMATURE"
    target.data_path = "witcherui_RigSettings.app_list_index"
    target.id = armobj.data

def create_app_drivers(armobj: bpy.types.Armature, obj_to_hide:bpy.types.Object):
    current_app_list_index = armobj.data.witcherui_RigSettings.app_list_index
    create_on_prop(armobj, current_app_list_index, obj_to_hide, prop_name = "hide_render")
    create_on_prop(armobj, current_app_list_index, obj_to_hide, prop_name = "hide_viewport")
    for obj in obj_to_hide.children:
        create_app_drivers(armobj, obj)

import math

def fov_to_length( fov:float ):
    x = 43.266615300557 # Diagonal measurement for a 'normal' 35mm lens
    if ( fov < 1 or fov > 179 ):
        return None
    return ( x / ( 2 * math.tan( math.pi * fov / 360.0 ) ) )


def length_to_fov( length:float, crop:float = 1.0 ):
    x = 43.266615300557
    if ( length < 1 ):
        return None
    length *= crop
    return (2 * math.tan(x / ( 2.0 * length ) ) * 180.0 / math.pi)


def create_camera_drivers(armobj, camera, name):
    camera_data:bpy.types.Camera = camera.data
    camera_data.lens_unit = 'FOV' #convert witcher FOV angle to mm, angle cannot be driven it uses mm lens prop
    camera_data.sensor_fit = 'VERTICAL'
    camera_data.sensor_height = 43.266615300557

    driver_curve = camera_data.driver_add("lens")
    driver = driver_curve.driver
    channel = name
    driver.expression = f'43.266615300557 / ( 2 * tan( pi * {channel} / 360.0 ) )' #channel
    var = driver.variables.get(channel)
    if var is None:
        var = driver.variables.new()
    var.type = "SINGLE_PROP"
    var.name = channel
    armobj.pose.bones["Camera_Node"]["%s" % channel] = 35
    target = var.targets[0]
    target.id_type = "OBJECT"
    target.data_path = 'pose.bones["Camera_Node"]["%s"]' % channel #'["%s"]' % channel
    target.id = armobj
    armobj.update_tag()

def do_constraints(constrains, objdict, meshdict, HardAttachments, group_parent = None):
    return_objs = []

    for con_s in constrains:
        parent_obj = con_s[0]
        child_obj = con_s[1]
        if parent_obj in objdict and child_obj in objdict:
            constrain_util.CreateConstraints2(objdict[parent_obj], objdict[child_obj])
            objdict[child_obj].parent = objdict[parent_obj]
            if group_parent:
                if parent_obj == group_parent:
                    return_objs.append(objdict[child_obj])
        else:
            log.info('Failed to constrain '+child_obj+' to '+parent_obj)

    #TODO simplify LOD process
    for constrain in HardAttachments:
        (parent_arm_name, p_bone_name) = constrain[0].rsplit(':',1)
        relativeTransform = constrain[2]
        if "CAnimated" in constrain[1] or "CCameraComponent" in constrain[1] :
            target_name = constrain[1]
            if parent_arm_name in objdict and target_name in objdict:
                parent_arm = objdict[parent_arm_name]
                target_object = objdict[target_name]
                p_bone = parent_arm.pose.bones.get(p_bone_name)
                if p_bone is not None:
                    target_object.parent = parent_arm
                    target_object.parent_type = "BONE"
                    target_object.parent_bone = p_bone_name
                    # copyTransform = target_object.constraints.new('COPY_TRANSFORMS')
                    # copyTransform.name = p_bone_name + " to " + target_object.name
                    # copyTransform.target = parent_arm
                    # copyTransform.subtarget = p_bone_name
                    if "CCameraComponent" in constrain[1]:
                        create_camera_drivers(parent_arm, target_object, "hctFOV")


        else:
            bpy.ops.object.empty_add(type="PLAIN_AXES", radius=1)

            target_name = constrain[1]+"_lod0"
            if parent_arm_name in objdict and target_name in meshdict:
                target_transform = bpy.context.object
                target_transform.name = "CHardAttachment"
                target_mesh_obj = meshdict[target_name]
                target_mesh_obj.parent = target_transform

                parent_arm = objdict[parent_arm_name]
                p_bone = parent_arm.pose.bones.get(p_bone_name)
            if p_bone is not None:
                    target_transform.parent = parent_arm
                    target_transform.parent_type = "BONE"
                    target_transform.parent_bone = p_bone_name
                    # copyTransform = target_transform.constraints.new('COPY_TRANSFORMS')
                    # copyTransform.name = p_bone_name + " to " + target_transform.name
                    # copyTransform.target = parent_arm
                    # copyTransform.subtarget = p_bone_name
            target_object = target_transform

        target_object.parent = parent_arm
        #do relativeTransform
        if relativeTransform:
            rt = EngineTransform.from_json(**relativeTransform)
            set_blender_object_transform(target_object, rt, rotate_180 = False)#, from_this_object= parent_arm, pose_bone = p_bone if p_bone is not None else False)


    #if there are leftover meshes parent to top object TODO
    # for mesh in meshdict.values():
    #     if mesh.parent == None:
    #         if objdict:
    #             mesh.parent = list(objdict.values())[0]

    return return_objs

def import_chunks(entity,
                  ent_namespace,
                  cur_chunks,
                  constrains,
                  objdict,
                  meshdict,
                  HardAttachments,
                  hide_shadowmesh,
                  root_skeleton,
                  i,
                  selectedAppearance = None,
                  do_import_redcloth = True):

    hasCMovingPhysicalAgentComponent = False
    for chunk in cur_chunks:
        #each chunk gets it's own namespace as each "CMeshComponent" has lods and materials with the same name
        # ENTITY_NAMESPACE + TYPE + TEMPLATE_INDEX + CHUNK_INDEX
        chunk_namespace = ent_namespace+chunk['type']+str(i)+str(chunk['chunkIndex'])
        if not isChildNode(chunk['chunkIndex'], cur_chunks):
            constrains.append([entity.name, chunk_namespace])
        if chunk['type'] == "CMeshSkinningAttachment" or chunk['type'] == "CAnimatedAttachment":
            parent = chunk['parent']
            child = chunk['child']
            parentNS, childNS = 0,0
            for findChunk in cur_chunks:
                if findChunk['chunkIndex'] == parent:
                    if findChunk['type'] == "CAnimDangleComponent":
                        parentNS = GetChunkNS(findChunk['constraint'], cur_chunks, i)
                    else:
                        parentNS = findChunk['type']+str(i)+str(parent)
                if findChunk['chunkIndex'] == child:
                    if findChunk['type'] == "CAnimDangleComponent":
                        childNS = GetChunkNS(findChunk['constraint'], cur_chunks, i)
                    else:
                        childNS = findChunk['type']+str(i)+str(child)
            if parentNS and childNS:
                log.debug([parentNS, childNS])
                constrains.append([ent_namespace+parentNS, ent_namespace+childNS])
            else:
                log.debug("ERROR FINDING SKINNING ATTACHMENT")
        if "mesh" in chunk:
            (meshes, armatures) = fbx_util.importFbx(chunk['mesh'], chunk['type']+str(i)+str(chunk['chunkIndex']), entity.name)
            if selectedAppearance and len(chunk['name']):
                for colorEntry in entity.coloringEntries:
                    if selectedAppearance.name == colorEntry['appearance']:
                        if colorEntry['componentName'] == chunk['name']:
                            for mesh in meshes:
                                if colorEntry['colorShift1']:
                                    mesh['colorShift1_hue'] = colorEntry['colorShift1']['hue']
                                    mesh['colorShift1_saturation'] = colorEntry['colorShift1']['saturation']
                                    mesh['colorShift1_luminance'] = colorEntry['colorShift1']['luminance']
                                if colorEntry['colorShift2']:
                                    mesh['colorShift2_hue'] = colorEntry['colorShift2']['hue']
                                    mesh['colorShift2_saturation'] = colorEntry['colorShift2']['saturation']
                                    mesh['colorShift2_luminance'] = colorEntry['colorShift2']['luminance']
                                mesh.update_tag()
                            for arm in armatures:
                                pass

            for arm in armatures:
                objdict.update({chunk_namespace:arm})
            for mesh in meshes:
                if mesh.name[-5:-1] == "_lod":
                    meshdict.update({chunk_namespace+mesh.name[-5:]:mesh})
                else:
                    meshdict.update({chunk_namespace:mesh})
                if hide_shadowmesh and "shadowmesh" in mesh.name:
                    mesh.hide_viewport = True
                    mesh.hide_render = True
            if 'transform' in chunk and chunk['transform']:
                rt = EngineTransform.from_json(**chunk['transform'])
                set_blender_object_transform(mesh, rt, rotate_180 = False)#, from_this_object= parent_arm, pose_bone = p_bone if p_bone is not None else False)

        if "resource" in chunk:
            if do_import_redcloth:
                cloth_arma = cloth_util.importCloth(False, chunk['resource_apx'], True, True, True, chunk['resource'], chunk['type']+str(i)+str(chunk['chunkIndex']), entity.name)
                if cloth_arma.type == 'EMPTY':
                    #group_empty = cloth_arma
                    for child in cloth_arma.children:
                        if child.type == 'ARMATURE':
                            cloth_arma = child
                            cloth_arma.parent = None
                            break
                    # for child in group_empty.children:
                    #     bpy.data.objects.remove(child)
                    # bpy.data.objects.remove(group_empty)
                objdict.update({chunk_namespace:cloth_arma})
        if "morphComponentId" in chunk:
            morphSource = fbx_util.importFbx(chunk['morphSource'], chunk['type']+str(i)+str(chunk['chunkIndex']), entity.name)
            morphTarget = fbx_util.importFbx(chunk['morphTarget'], chunk['type']+str(i)+str(chunk['chunkIndex'])+"_morphTarget", entity.name)
            # if "\\he_" in chunk['morphSource']:
            #     eye_meshes.append(chunk_namespace)
            # if "\\c_" in chunk['morphSource'] or "\\hh_" in chunk['morphSource'] or "\\hb_" in chunk['morphSource']:
            #     hair_meshes.append(chunk_namespace)
            #morphs_todo.append([morphTarget+":Mesh", morphSource+":Mesh", chunk['morphComponentId']])
            #log.debug(morphTarget+":Mesh", morphSource+":Mesh", chunk['morphComponentId'])
            #bshape_def = pm.blendShape(morphTarget+":Mesh", morphSource+":Mesh", n=chunk['morphComponentId'])
        if chunk['type'] == "CMovingPhysicalAgentComponent":
            rig_grp_name = entity.name+chunk['type']+"_rig"+"_grp"
            CMovingPhysicalAgentComponent = import_rig.import_w3_rig(chunk['skeleton'],chunk_namespace)
            objdict.update({chunk_namespace:CMovingPhysicalAgentComponent})
            root_skeleton = CMovingPhysicalAgentComponent
            hasCMovingPhysicalAgentComponent = True
        elif "skeleton" in chunk:
            rig_grp_name = entity.name+chunk['type']+"_rig"+"_grp"
            root_bone = import_rig.import_w3_rig(chunk['skeleton'],chunk_namespace)
            objdict.update({chunk_namespace:root_bone})
            if not hasCMovingPhysicalAgentComponent:
                root_skeleton = root_bone
        if "dyng" in chunk:
            rig_grp_name = entity.name+chunk['type']+"_rig"+"_grp"
            root_bone = import_rig.import_w3_rig(chunk['dyng'],chunk_namespace)
            objdict.update({chunk_namespace:root_bone})
        if "mimicFace" in chunk:
            rig_grp_name = entity.name+chunk['type']+"_rig"+"_grp"
            #root_bone = import_rig.import_w3_rig(chunk['rig'],chunk_namespace)
            faceData = import_rig.loadFaceFile(chunk['mimicFace'])
            root_bone = import_rig.create_armature(faceData.mimicSkeleton,chunk_namespace)
            mimic_rig_bl = root_bone
            mimic_rig_bl['mimicFaceFile'] = chunk['mimicFace']
            mimic_namespace = chunk_namespace
            objdict.update({chunk_namespace:root_bone})
            objdict[entity.name]['mimicFace'] = root_bone.name
            objdict[entity.name]['mimicFaceFile'] = chunk['mimicFace']
        if chunk['type'] == "CCameraComponent":
            camera_data = bpy.data.cameras.new(name='Camera')
            camera_object = bpy.data.objects.new('Camera', camera_data)
            bpy.context.collection.objects.link(camera_object)
            camera_object.rotation_euler[0] = np.pi/2
            objdict.update({chunk_namespace:camera_object})
        if chunk['type'] == "CHardAttachment":
            parent = chunk['parent']
            child = chunk['child']
            childNS = False
            parentSlotName = chunk['parentSlotName']
            parentSlot = chunk['parentSlot'] # ??
            for findChunk in cur_chunks:
                if findChunk['chunkIndex'] == parent:
                    if findChunk['type'] == "CAnimDangleComponent":
                        parentNS = GetChunkNS(findChunk['constraint'], cur_chunks, i)
                    else:
                        parentNS = findChunk['type']+str(i)+str(parent)
                if findChunk['chunkIndex'] == child:
                    if findChunk['type'] == "CAnimDangleComponent":
                        childNS = GetChunkNS(findChunk['constraint'], cur_chunks, i)
                    else:
                        childNS = findChunk['type']+str(i)+str(child)

            if parentSlotName and childNS:
                #log.debug([parentSlotName, childNS])
                # if pm.objExists(ent_namespace+parentSlotName) and pm.objExists( ent_namespace+childNS):
                HardAttachments.append([ent_namespace+parentNS+':'+parentSlotName, ent_namespace+childNS, chunk['relativeTransform'] if 'relativeTransform' in chunk else None])
            else:
                log.debug("ERROR FINDING SKINNING ATTACHMENT")
    return (constrains, objdict, meshdict, HardAttachments, root_skeleton)


def import_MovingPhysicalAgentComponent(entity, parent_transform = None):
    ent_namespace = entity.name+":"

    #OPTIONS
    hide_shadowmesh = True
    mimic_namespace = False
    root_skeleton = False
    faceData = False

    #CONTRAINT ARRAYS
    constrains = []
    morphs_todo = []
    HardAttachments = []

    #DICTS
    objdict = {}
    meshdict = {}
    
    
    if entity.staticMeshes is not None:
        cur_chunks = entity.staticMeshes.get('chunks', [])
        (constrains, objdict, meshdict, HardAttachments, root_skeleton) = import_chunks(entity, ent_namespace, cur_chunks, constrains, objdict, meshdict, HardAttachments, hide_shadowmesh, root_skeleton, i='')
    
    
    
    do_constraints(constrains, objdict, meshdict, HardAttachments)

    if parent_transform:
        root_skeleton.parent = parent_transform
        for mesh in list(objdict.values()) + list(meshdict.values()):
            if mesh.parent == None:
                mesh.parent = parent_transform
    return root_skeleton

def reset_transforms(new_obj):
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

def import_app(context,
               selectedAppearance,
               entity,
               base_animation_skeleton,
               do_import_redcloth):
    (exist, enabled) = addon_utils.check("io_mesh_apx")
    if not enabled:
        (exist, enabled) = addon_utils.check("io_scene_apx")
    if not enabled:
        do_import_redcloth = False

    save_world = base_animation_skeleton.matrix_world
    save_local = base_animation_skeleton.matrix_local
    save_basis =base_animation_skeleton.matrix_basis
    save_location = base_animation_skeleton.location
    save_scale = base_animation_skeleton.scale
    reset_transforms(base_animation_skeleton)
    current_pose_position = base_animation_skeleton.data.pose_position
    base_animation_skeleton.data.pose_position = "REST"

    ent_namespace = entity.name+":"

    #OPTIONS
    hide_shadowmesh = True
    mimic_namespace = False
    root_skeleton = False
    faceData = False
    group_parent = True #None

    if group_parent:
        group_parent = entity.name
        # group entire apperance
        bpy.ops.object.empty_add(type="PLAIN_AXES", radius=1)
        empty_transform = bpy.context.object
        empty_transform.name = selectedAppearance.name
        empty_transform.parent = base_animation_skeleton

    constrains = []
    morphs_todo = []
    HardAttachments = []

    #DICTS
    objdict = {}
    objdict.update({entity.name:base_animation_skeleton})
    meshdict = {}

    log.debug(selectedAppearance.name)
    for i in range(len(selectedAppearance.includedTemplates)):
        cur_chunks = selectedAppearance.includedTemplates[i]['chunks']
        (constrains, objdict, meshdict, HardAttachments, root_skeleton) = import_chunks(entity, ent_namespace, cur_chunks, constrains, objdict, meshdict, HardAttachments, hide_shadowmesh, root_skeleton, i, selectedAppearance, do_import_redcloth)
    apperance_level_objects = do_constraints(constrains, objdict, meshdict, HardAttachments, group_parent)

    #if grouping the entire appreance together
    if group_parent:
        for obj in apperance_level_objects:
            obj.parent = empty_transform
        create_app_drivers(base_animation_skeleton, empty_transform)
    load_face_poses = False
    if load_face_poses:
        mimicPoses = import_rig.import_w3_mimicPoses(faceData.mimicPoses, faceData.mimicSkeleton, actor=entity.name, mimic_namespace=mimic_namespace)


    base_animation_skeleton.matrix_world = save_world
    base_animation_skeleton.matrix_local = save_local
    base_animation_skeleton.matrix_basis = save_basis
    base_animation_skeleton.location = save_location
    base_animation_skeleton.scale = save_scale
    base_animation_skeleton.data.pose_position = current_pose_position

from io_import_w2l.importers import bpyutils

def import_from_list_item(context, item, do_import_redcloth):
    ob = context.object
    if ob and ob.type == "ARMATURE" and "CMovingPhysicalAgentComponent" in ob.name:
        base_animation_skeleton = ob
        rig_settings = base_animation_skeleton.data.witcherui_RigSettings
        class_to_json = json.loads(rig_settings.jsonData)
        entity = w3_types.Entity.from_json(class_to_json)

        for app in entity.appearances:
            if app.name == item.name:
                import_app(context, app, entity, base_animation_skeleton, do_import_redcloth)
                bpyutils.select_object(base_animation_skeleton)
                #bpy.ops.witcher.load_face_morphs()
