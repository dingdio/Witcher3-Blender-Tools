from io_import_w2l.setup_logging_bl import *
log = logging.getLogger(__name__)

import json
import os
import re
import bpy

from io_import_w2l import import_rig
#from io_import_w2l import settings
from io_import_w2l import fbx_util
from io_import_w2l import cloth_util
from io_import_w2l import constrain_util
from io_import_w2l.CR2W import read_json_w3
from io_import_w2l.CR2W.dc_entity import load_bin_entity
from io_import_w2l.CR2W import w3_types
from io_import_w2l import get_W3_REDCLOTH_PATH

def repo_file(filepath: str):
    if filepath.endswith('.fbx'):
        return os.path.join(bpy.context.preferences.addons['io_import_w2l'].preferences.fbx_uncook_path, filepath)
    else:
        return os.path.join(bpy.context.preferences.addons['io_import_w2l'].preferences.uncook_path, filepath)
    #repo = "D:/Witcher_uncooked_clean/raw_ent/"
    #return settings.get().repopath+filepath

def fixed(entity):
    suffix ="" #"_CONVERT_"
    entity.MovingPhysicalAgentComponent.skeleton = repo_file(entity.MovingPhysicalAgentComponent.skeleton)#+".json";
    
    for appearance in entity.appearances:
        for template in appearance.includedTemplates:
            for chunk in template['chunks']:
                if "mesh" in chunk:
                    chunk['mesh'] = repo_file(chunk['mesh'].replace(".w2mesh", suffix+".fbx"))
                if chunk['type'] == "CClothComponent":
                    resource = chunk['resource']
                    chunk['resource'] = repo_file(resource)
                    chunk['resource_apx'] = get_W3_REDCLOTH_PATH(bpy.context)+"\\"+resource.replace(".redcloth", ".apx")
                if "morphSource" in chunk:
                    chunk['morphSource'] = repo_file(chunk['morphSource'].replace(".w2mesh", suffix+".fbx"))
                if "morphTarget" in chunk:
                    chunk['morphTarget'] = repo_file(chunk['morphTarget'].replace(".w2mesh", suffix+".fbx"))
                if "skeleton" in chunk:
                    chunk['skeleton'] = repo_file(chunk['skeleton'])#+".json"
                if "dyng" in chunk:
                    chunk['dyng'] = repo_file(chunk['dyng'])#+".json"
                if "mimicFace" in chunk:
                    chunk['mimicFace'] = repo_file(chunk['mimicFace'])#+".json"
    if entity.staticMeshes:
        for chunk in entity.staticMeshes.get('chunks', []):
            if "mesh" in chunk:
                chunk['mesh'] = repo_file(chunk['mesh'].replace(".w2mesh", suffix+".fbx"))
            if "skeleton" in chunk:
                chunk['skeleton'] = repo_file(chunk['skeleton'])#+".json"
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

global GLOBAL_appearances
def NewListItem( treeList, node):
    item = treeList.add()
    item.name = node.name
    item.jsonData = "cake" #node.toJSON()
    return item

def NewAnimsetListItem( treeList, path, name):
    item = treeList.add()
    if path:
        item.path = path
    if name:
        item.name = name
    #item.jsonData = "cake" #node.toJSON()
    return item

def test_load_entity(filename) ->  w3_types.Entity:
    dirpath, file = os.path.split(filename)
    basename, ext = os.path.splitext(file)
    if ext.lower() in ('.json'):
        entity = read_json_w3.readEntFile(filename)
    elif ext.lower().endswith('.w2ent'):
        bin_data = load_bin_entity(filename)
        class_to_json = json.loads(json.dumps(bin_data,indent=2, default=vars, sort_keys=False))
        entity = w3_types.Entity()
        entity = entity.from_json(class_to_json)
    else:
        entity = None
    return entity


def import_ent_template(filename, load_face_poses):
    context = bpy.context
    # #! TEMP
    # #? FBX
    # # fbx_name = fbx_util.importFbx(temp_filename, "mesh", "cake")

    # #? REDCLOTH
    # redcloth_name = r"dlc\ep1\data\characters\models\secondary_npc\shani\model\b_01_wa__shani_px.redcloth"
    # recloth_name = r"dlc\ep1\data\characters\models\secondary_npc\shani\model\d_01_wa__shani_px.redcloth"

    # redcloth_fileName = ":\\w3.modding\\modkit\\r4data\\"+recloth_name
    # apxcloth_fileName = ":\\w3_uncook\\FBXs\\REDCLOTH\\"+recloth_name.replace(".redcloth", ".apx")
    # cloth_name = cloth_util.importCloth(apxcloth_fileName, redcloth_fileName, "CLOTH_NS", "CLOTH_NAME")

    # return
    # #! TEMP

    entity = test_load_entity(filename)

    entity = fixed(entity)
    
    treeList = context.scene.app_list
    treeList.clear()
    #import_MovingPhysicalAgentComponent(entity)
    if entity.appearances:
        global GLOBAL_appearances
        GLOBAL_appearances = entity
        for node in entity.appearances:
            item = NewListItem(treeList, node)
    else:
        import_MovingPhysicalAgentComponent(entity)

    context.scene.main_entity_skeleton = entity.MovingPhysicalAgentComponent.skeleton
    
    #Find the first (and only?) CMimicComponent and use it to import face animations
    for ent in entity.appearances:
        for template in ent.includedTemplates:
            for chunk in template['chunks']:
                if chunk['type'] == "CMimicComponent" and 'mimicFace' in chunk:
                    context.scene.main_face_skeleton = chunk['mimicFace']
                    break
    animset_list = context.scene.animset_list
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

    # #! TEMP
    # class Object(object):
    #     pass
    # temp = Object()
    # temp.name = "shani_naked"
    # #temp.name = "shani_wedding"
    # import_from_list_item(context, temp)
    # #! TEMP

def inList(name, mylist):
    for el in mylist:
        if el in name:
            return True
    return False

#CAnimatedComponent
#CMovingAgentComponent
#MovingPhysicalAgentComponent

def do_constraints(constrains, objdict, meshdict, HardAttachments):
    for con_s in constrains:
        if con_s[0] in objdict and con_s[1] in objdict:
            constrain_util.CreateConstraints2(objdict[con_s[0]], objdict[con_s[1]])  

    #TODO simplify LOD process
    for constrain in HardAttachments:
        (parent_arm_name, p_bone_name) = constrain[0].rsplit(':',1)
        if "CAnimated" in constrain[1]:
            target_name = constrain[1]
            if parent_arm_name in objdict and target_name in objdict:
                parent_arm = objdict[parent_arm_name]
                target_arm = objdict[target_name]
                p_bone = parent_arm.pose.bones.get(p_bone_name)
                if p_bone is not None:
                    copyTransform = target_arm.constraints.new('COPY_TRANSFORMS')
                    copyTransform.name = p_bone_name + " to " + target_arm.name
                    copyTransform.target = parent_arm
                    copyTransform.subtarget = p_bone_name
        else:
            target_name = constrain[1]+"_lod0"
            if parent_arm_name in objdict and target_name in meshdict:
                parent_arm = objdict[parent_arm_name]
                mesh = meshdict[target_name]
                p_bone = parent_arm.pose.bones.get(p_bone_name)
                if p_bone is not None:
                    copyTransform = mesh.constraints.new('COPY_TRANSFORMS')
                    copyTransform.name = p_bone_name + " to " + mesh.name
                    copyTransform.target = parent_arm
                    copyTransform.subtarget = p_bone_name

def import_chunks(entity, ent_namespace, cur_chunks, constrains, objdict, meshdict, HardAttachments, hide_shadowmesh, root_skeleton, i, selectedAppearance = None):
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
        if "resource" in chunk:
            cloth_name = cloth_util.importCloth(chunk['resource_apx'], chunk['resource'], chunk['type']+str(i)+str(chunk['chunkIndex']), entity.name)
            #objdict.update({chunk_namespace:cloth_name})
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
        if chunk['type'] == "CHardAttachment":
            parent = chunk['parent']
            child = chunk['child']
            childNS = False
            parentSlotName = chunk['parentSlotName']
            parentSlot = chunk['parentSlot']
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
                HardAttachments.append([ent_namespace+parentNS+':'+parentSlotName, ent_namespace+childNS])
            else:
                log.debug("ERROR FINDING SKINNING ATTACHMENT")
    return (constrains, objdict, meshdict, HardAttachments, root_skeleton)


def import_MovingPhysicalAgentComponent(entity):
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

    return root_skeleton

def import_app(context, selectedAppearance, entity, base_animation_skeleton):
    ent_namespace = entity.name+":"
    
    #OPTIONS
    hide_shadowmesh = True
    mimic_namespace = False
    root_skeleton = False
    faceData = False
    
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
        (constrains, objdict, meshdict, HardAttachments, root_skeleton) = import_chunks(entity, ent_namespace, cur_chunks, constrains, objdict, meshdict, HardAttachments, hide_shadowmesh, root_skeleton, i, selectedAppearance)
    do_constraints(constrains, objdict, meshdict, HardAttachments)
    load_face_poses = False
    if load_face_poses:
        mimicPoses = import_rig.import_w3_mimicPoses(faceData.mimicPoses, faceData.mimicSkeleton, actor=entity.name, mimic_namespace=mimic_namespace)
    
def import_from_list_item(context, item):
    global GLOBAL_appearances
    entity = GLOBAL_appearances
    for app in GLOBAL_appearances.appearances:
        if app.name == item.name:
            base_animation_skeleton = import_MovingPhysicalAgentComponent(entity)
            import_app(context, app, entity, base_animation_skeleton)
