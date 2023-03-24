from pathlib import Path
from io_import_w2l import import_anims
#from io_import_w2l.filter_list import memory
from io_import_w2l.setup_logging_bl import *
log = logging.getLogger(__name__)
from io_import_w2l.CR2W.dc_anims import load_bin_anims_single

import csv
import os
import bpy
from bpy.types import PropertyGroup
from bpy.props import (
    CollectionProperty,
    IntProperty,
    BoolProperty,
    StringProperty,
    PointerProperty,
)
from io_import_w2l import get_uncook_path
class AnimsResourceManager:
    resourceManager = None
    def __init__(self):

        RES_DIR = Path(__file__)
        RES_DIR = str(Path(RES_DIR).parents[1])
        filename = os.path.join(RES_DIR, "CR2W\\data\\actor_animations.csv")
        self.pathashespath = filename
        #self.HashdumpDict = {}
        reader = csv.DictReader(open(self.pathashespath), delimiter=";")
        
        self.HashdumpDict = list(reader)
        # for row in reader:
        #     self.HashdumpDict[row["file"]+";"+row["id"]] = row["id"]
            #self.HashdumpDict[row["file"]] = row["cat1"]+" "+row["cat2"]+" "+row["cat3"]+": "+row["id"]+" "+row["caption"]+row["frames"]
    @staticmethod
    def Get():
        if (AnimsResourceManager.resourceManager == None):
            AnimsResourceManager.resourceManager = AnimsResourceManager();
        return AnimsResourceManager.resourceManager;


class MyAnimListItem(bpy.types.PropertyGroup):
    id: bpy.props.StringProperty(default="")
    prefix: bpy.props.StringProperty(default="")
    suffix: bpy.props.StringProperty(default="")
    caption: bpy.props.StringProperty(default="")
    child_count: bpy.props.StringProperty(default="")
    isSelected: bpy.props.BoolProperty(default=False)

    #?parent data??
    indent: bpy.props.IntProperty(default=0)
    expanded: bpy.props.BoolProperty(default=False)
    nodeIndex : bpy.props.IntProperty(default=-1) #index into the real tree data.
    
    name : bpy.props.StringProperty(default="")
    selfIndex : bpy.props.IntProperty(default=-1)
    parentIndex : bpy.props.IntProperty(default=-1)
    childCount: bpy.props.IntProperty(default=0) #should equal myNodes[nodeIndex].childCount
    animLineId: bpy.props.StringProperty(default="0000000000")
    vertex_group: bpy.props.StringProperty(default="")



def AddCLayerGroupExample(groups, parent_collection):
    this_collection = bpy.data.collections.new(groups.name)
    this_collection['group_type'] = "LayerGroup"
    if parent_collection:
        parent_collection.children.link(this_collection)
    if groups.ChildrenGroups:
        for subgroups in groups.ChildrenGroups:
            AddCLayerGroupExample(subgroups, this_collection)
    if groups.ChildrenInfos:
        for ChildInfo in groups.ChildrenInfos:
            child_collection = bpy.data.collections.new(os.path.basename(ChildInfo.depotFilePath))
            child_collection['level_path'] = ChildInfo.depotFilePath
            child_collection['layerBuildTag'] = ChildInfo.layerBuildTag
            child_collection['group_type'] = "LayerInfo"
            this_collection.children.link(child_collection)

def createCat(cat_name, dict):
    final_list = []
    for entry in dict:
        if entry['cat1'] == cat_name:
            final_list.append(entry)
    return final_list

# def get_filtered_dict(cat_name, dict, cat_num):
#     filtered_dictionary = {}
#     for key, value in enumerate(dict):
#         if (value['cat'+str(cat_num)] == cat_name):
#             filtered_dictionary[value['cat'+str(cat_num+1)]] = get_filtered_dict()
#     return filtered_dictionary

from io_import_w2l.filtered_list.animations_manager import CModStoryBoardAnimationListsManager
from io_import_w2l.filtered_list.storyboardasset import CModStoryBoardActor

def GetAnimationInfoByName(anim_name):
    uncook_path = get_uncook_path(bpy.context)
    manager = CModStoryBoardAnimationListsManager.active
    fdir = None
    found = False
    for anim in manager._animMeta.animList:
        if anim.id == anim_name:
            fdir = anim.path # animation might not be proper
            for anim_active in manager.active.active_list._items:
                if anim_active.id == anim.slotId:
                    fdir = anim.path
                    found = True
                    break
            if found:
                break
    if fdir == None:
        log.critical('Did not find animation!')
        return (None, None)
    #(, ) = item.animLineId.split(';')
    fdir = os.path.join(uncook_path, fdir)
    return (anim_name, fdir)

def SetupActor(main_arm_obj):
    #main_arm_obj = bpy.context.active_object
    rig_settings = main_arm_obj.data.witcherui_RigSettings
    animListsManager: CModStoryBoardAnimationListsManager = CModStoryBoardAnimationListsManager()

    actor = CModStoryBoardActor()
    
    animset_list = rig_settings.animset_list
    actor._animPaths = []
    for set in animset_list:
        if ":" not in set.path:
            actor._animPaths.append(set.path)
    
    animListsManager.lazyLoad()

    #TODO list should be filtered by the list of w2anims passed into it from the entity object
    list = animListsManager.getAnimationListFor(actor)
    #list.setWildcardFilter("")
    filteredList = list.getFilteredList()
    print(list.getMatchingItemCount(),"/",list.getTotalCount())
    myAnims = bpy.context.scene.myAnimList
    myAnims.clear()
    for (i, item) in enumerate(filteredList):
        anim = myAnims.add()
        anim.id = str(item.id)
        anim.prefix = item.prefix
        anim.suffix = item.suffix
        anim.caption = item.caption
        anim.child_count = str(item.child_count)
        anim.isSelected = item.isSelected
        anim.name = "{}{}{}".format(item.prefix, item.caption, item.suffix)
        anim.selfIndex = len(myAnims)-1
        anim.animLineId = str(i)

def SetupNodeData(context):
    ob = context.object
    if ob and ob.type == "ARMATURE" and "CMovingPhysicalAgentComponent" in ob.name:
        main_arm_obj = ob
        SetupActor(main_arm_obj)

def FilterData(context):
    list = CModStoryBoardAnimationListsManager.active_list
    if list:
        list.setWildcardFilter(context.scene.anim_search_str)
        filteredList = list.getFilteredList()
        print(list.getMatchingItemCount(),"/",list.getTotalCount())
        myAnims = bpy.context.scene.myAnimList
        myAnims.clear()
        for (i, item) in enumerate(filteredList):
            anim = myAnims.add()
            anim.id = str(item.id)
            anim.prefix = item.prefix
            anim.suffix = item.suffix
            anim.caption = item.caption
            anim.child_count = str(item.child_count)
            anim.isSelected = item.isSelected
            anim.name = "{}{}{}".format(item.prefix, item.caption, item.suffix)
            anim.selfIndex = len(myAnims)-1
            anim.animLineId = str(i)

def load_anim_into_scene(context, anim_name, fdir, main_arm_obj, NLA_track = 'anim_import', at_frame = 0):
    rig_settings = main_arm_obj.data.witcherui_RigSettings
    if "_mimic_" in fdir:
        result = load_bin_anims_single(fdir, anim_name, rigPath=rig_settings.main_face_skeleton)
    else:
        result = load_bin_anims_single(fdir, anim_name, rigPath=rig_settings.main_entity_skeleton)
    animation = result.animations[0]
    import_anims.import_anim(context, fdir, animation, use_NLA= True, NLA_track = NLA_track, at_frame = at_frame)
    # print(fdir)
    # print(anim_name)

class MyAnimListItem_Debug(bpy.types.Operator):
    bl_idname = "witcher.myanimlist_debug"
    bl_label = "Debug"
    
    action: StringProperty(default="default")
    
    def execute(self, context):
        
        uncook_path = get_uncook_path(context)
        scene = context.scene
        action = self.action
        if "load" == action:
            ob = context.object
            if ob and ob.type == "ARMATURE" and "CMovingPhysicalAgentComponent" in ob.name:
                main_arm_obj = ob
                
                #main_arm_obj = bpy.context.active_object
                rig_settings = main_arm_obj.data.witcherui_RigSettings
            if scene.myAnimList_index >= 0 and scene.myAnimList:
                manager = CModStoryBoardAnimationListsManager.active
                item = scene.myAnimList[scene.myAnimList_index]
                (anim_name, fdir) = manager.getAnimationName(int(item.id))
                #(, ) = item.animLineId.split(';')
                fdir = os.path.join(uncook_path, fdir)
                
                load_anim_into_scene(context, anim_name, fdir, main_arm_obj)
        elif "reset3" == action:
            print("=== Debug Reset ====")
            context.scene.anim_search_str = ""
            SetupNodeData(context)
            #memory.changed()
        elif "search" == action:
            FilterData(context)
        elif "clear" == action:
            print("=== Debug Clear ====")
            bpy.context.scene.myAnimList.clear()
        else:
            print("unknown debug action: "+action)

        return {'FINISHED'}


import bpy
from io_import_w2l.filtered_list.animations_manager import CModStoryBoardAnimationListsManager


class OBJECT_OT_anims_skp_folder_toggle(bpy.types.Operator):
    bl_idname = 'object.anims_skp_folder_toggle'
    bl_label = 'operators.FolderToggle.bl_label'
    bl_description = 'operators.FolderToggle.bl_description'
    bl_options = {'REGISTER', 'UNDO'}
    
    index: bpy.props.IntProperty(options={'HIDDEN'})
    
    @classmethod
    def poll(cls, context):
        return context.scene.myAnimList #context.object and context.object.data.shape_keys
    
    def execute(self, context):
        #obj = context.object
        #shape_keys = obj.data.shape_keys
        key_blocks = context.scene.myAnimList #shape_keys.key_blocks
        #active_key = obj.active_shape_key
        
        # if active_key and key_blocks[self.index].name in memory.tree.active.get_parents(active_key.name):
        #     # The active index shouldn't be on a hidden shape key.
        #     obj.active_shape_key_index = self.index
        sel_item = key_blocks[self.index]
        #core.folder.toggle(key_blocks[self.index])
        active = CModStoryBoardAnimationListsManager.active
        list = CModStoryBoardAnimationListsManager.active_list
        if list:
            list.setSelection(sel_item.id, True)
            #list.setWildcardFilter("geralt")
            filteredList = list.getFilteredList()
            print(list.getMatchingItemCount(),"/",list.getTotalCount())
            myAnims = bpy.context.scene.myAnimList
            myAnims.clear()
            for (i, item) in enumerate(filteredList):
                if "dog" in str(item.id):
                    sgf56 = 4636
                anim = myAnims.add()
                anim.id = str(item.id)
                anim.prefix = item.prefix
                anim.suffix = item.suffix
                anim.caption = item.caption
                anim.child_count = str(item.child_count)
                anim.isSelected = item.isSelected
                anim.name = "{}{}{}".format(item.prefix, item.caption, item.suffix)
                anim.selfIndex = len(myAnims)-1
                anim.animLineId = str(i)
                if sel_item.id == item.id:
                    context.scene.myAnimList_index = anim.selfIndex
        return {'FINISHED'}



class MYANIMLISTITEM_UL_basic(bpy.types.UIList):
    animListsManager: CModStoryBoardAnimationListsManager = CModStoryBoardAnimationListsManager()

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index=0, flt_flag=0):
        
        
        frame = layout.row(align=True)
        if item.id.startswith('CAT'):
            op = frame.operator(
                operator='object.anims_skp_folder_toggle',
                text="",
                icon= 'TRIA_RIGHT' if "+" in item.prefix else "TRIA_DOWN", #'TRIA_DOWN', 'TRIA_RIGHT'#core.folder.get_active_icon(item),
                emboss=False)

            op.index = index

        frame.prop(
            data=item,
            property='name',
            text="",
            emboss=False,
            icon="NONE")#core.preferences.shape_key_icon)
    def filter_items(self, context, data, propname):
        scene = context.scene
        return ([],[])
            

from io_import_w2l.ui.ui_utils import WITCH_PT_Base
class SCENE_PT_myanimlist(WITCH_PT_Base, bpy.types.Panel):
    bl_parent_id = "WITCH_PT_Quick"

    bl_label = "Quick Anim List"
    bl_idname = "SCENE_PT_myanimlist"

    def draw(self, context):
        scn = context.scene
        layout = self.layout
        
        row = layout.row()
        layout = self.layout
        
        row = self.layout.row()
        row.prop(context.scene, "anim_search_str")
        row.operator(MyAnimListItem_Debug.bl_idname, text="Search").action = "search"
        row = layout.row()
        row.template_list(
            listtype_name='MYANIMLISTITEM_UL_basic',#'MYANIMLISTITEM_UL_basic',
            dataptr=bpy.context.scene,
            propname='myAnimList',
            active_dataptr=bpy.context.scene,
            active_propname='myAnimList_index',
            list_id='W3_UI_ANIMATION_LIST',
            rows=8)
        grid = layout.grid_flow( columns = 2 )
        
        grid.operator(MyAnimListItem_Debug.bl_idname, text="Reset").action = "reset3"
        #grid.operator(MyAnimListItem_Debug.bl_idname, text="Clear").action = "clear"
        grid.operator(MyAnimListItem_Debug.bl_idname, text="Load").action = "load"


classes = (
        MyAnimListItem,
        MyAnimListItem_Debug,
        OBJECT_OT_anims_skp_folder_toggle,
        MYANIMLISTITEM_UL_basic,
        SCENE_PT_myanimlist)

def update_filter(self, context):
    #print(self.rna_type.identifier)
    FilterData(context)

def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.myAnimList = bpy.props.CollectionProperty(type=MyAnimListItem)
    bpy.types.Scene.myAnimList_index = IntProperty()
    # bpy.types.Scene.myAnimList_pointer = PointerProperty(type=bpy.types.UIList
    #                                                      ,name = "Main Anim List")
    bpy.types.Scene.anim_search_str = StringProperty(
                                            name="",
                                            description="Search Animations",
                                            default="",
                                            update=update_filter)

def unregister():
    for cls in classes:
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.anim_search_str


if __name__ == "__main__":
    register()