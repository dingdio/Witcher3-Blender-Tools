import os
import json
from io_import_w2l import get_uncook_path
from io_import_w2l.CR2W import read_json_w3
from io_import_w2l.CR2W import w3_types
from io_import_w2l.importers import import_entity
from io_import_w2l.CR2W.dc_anims import load_bin_cutscene

def loadCutsceneFile(filename):
    dirpath, file = os.path.split(filename)
    basename, ext = os.path.splitext(file)
    if ext.lower() in ('.json'):
        with open(filename) as file:
            return read_json_w3.Read_CCutsceneTemplate(json.loads(file.read()))
    elif ext.lower().endswith('.w2cutscene'):
        return load_bin_cutscene(filename)
    else:
        return None

import bpy
from .import_anims import NewListItem, set_global_set


def check_if_actor_already_in_scene(repo_path):
    for o in bpy.context.scene.objects:
        if o.type != 'ARMATURE':
            continue
        if len(o.name) > 4 and o.name[-4] != "." and o.data.witcherui_RigSettings.repo_path == repo_path:
            return o
    return False

def import_w3_cutscene(filename):
    CCutsceneTemplate = loadCutsceneFile(filename)
    context = bpy.context
    treeList = context.scene.demo_list
    treeList.clear()
    set_global_set(CCutsceneTemplate)
    for node in CCutsceneTemplate.animations:
        item = NewListItem(treeList, node)
    
    #check if user wants to import actors
    #TODO new property group for all cutscene data??
    actor:w3_types.SCutsceneActorDef
    for actor in CCutsceneTemplate.SCutsceneActorDefs:
        actor.useMimic = False
        #!find actor in scene
        #!if not actor import actor
        #!apply stuff like voice tags etc.
        
        #? make sure to duplicate the template if there are multiple in scene and apply unique tags
        actor_obj = check_if_actor_already_in_scene(actor.template)
        if not actor_obj:
            import_entity.import_ent_template(get_uncook_path(bpy.context)+'\\'+actor.template, load_face_poses=actor.useMimic)
            #if not apperance import and apply apperance

        # if actor.name == "trajectories":
        #     actor_obj = False #check_if_actor_already_in_scene(actor.template)
        #     if not actor_obj and actor.name == "trajectories":
        #         import_entity.import_ent_template(get_uncook_path(bpy.context)+'\\'+actor.template, load_face_poses=actor.useMimic)
        #!if usemimic check and load face morphs

    return CCutsceneTemplate

