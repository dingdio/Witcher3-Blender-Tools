from pathlib import Path
from io_import_w2l.CR2W.dc_anims import load_bin_anims_single
from io_import_w2l.importers import import_anims
from io_import_w2l.setup_logging_bl import *

log = logging.getLogger(__name__)

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

class MimicsResourceManager:
    resourceManager = None
    def __init__(self):
        
        RES_DIR = Path(__file__)
        RES_DIR = str(Path(RES_DIR).parents[1])
        filename = os.path.join(RES_DIR, "CR2W\\data\\actor_mimics.csv")
        self.pathashespath = filename
        #self.HashdumpDict = {}
        reader = csv.DictReader(open(self.pathashespath), delimiter=";")
        
        self.HashdumpDict = {}
        for row in reader:
            self.HashdumpDict[row["file"]+";"+row["id"]] = row["id"]
            #self.HashdumpDict[row["file"]] = row["cat1"]+" "+row["cat2"]+" "+row["cat3"]+": "+row["id"]+" "+row["caption"]+row["frames"]
    @staticmethod
    def Get():
        if (MimicsResourceManager.resourceManager == None):
            MimicsResourceManager.resourceManager = MimicsResourceManager();
        return MimicsResourceManager.resourceManager;




class MyMimicListNode(bpy.types.PropertyGroup):
    name : bpy.props.StringProperty(default="")
    selfIndex : bpy.props.IntProperty(default=-1)
    parentIndex : bpy.props.IntProperty(default=-1)
    childCount : bpy.props.IntProperty(default=0)
    mimicLineId: bpy.props.StringProperty(default="0000000000")

class MyMimicListItem(bpy.types.PropertyGroup):
    indent: bpy.props.IntProperty(default=0)
    expanded: bpy.props.BoolProperty(default=False)
    nodeIndex : bpy.props.IntProperty(default=-1) #index into the real tree data.
    childCount: bpy.props.IntProperty(default=0) #should equal myNodes[nodeIndex].childCount
    mimicLineId: bpy.props.StringProperty(default="0000000000")

def SetupNodeData():
    mimicList = MimicsResourceManager().Get()
    
    myNodes = bpy.context.scene.myMimicNodes
    myNodes.clear()
    
    for (i, item) in mimicList.HashdumpDict.items():
        node = myNodes.add()
        node.name = "{}".format(item)
        node.selfIndex = len(myNodes)-1
        node.mimicLineId = str(i)
        
    # calculate childCount for all nodes
    for  node in myNodes :
        if node.parentIndex != -1:
            parent = myNodes[node.parentIndex]
            parent.childCount = parent.childCount + 1
            
    print("++++ SetupNodeData ++++")
    print("Node count: {}".format(len(myNodes)))
    for i in range(len(myNodes)):
        node = myNodes[i]
        print("{} node:{} child:{}".format(i, node.name, node.childCount))
        

def NewListItem( mimicList, node):
    item = mimicList.add()
    item.name = node.name
    item.nodeIndex = node.selfIndex
    item.childCount = node.childCount
    item.mimicLineId = node.mimicLineId
    return item


def SetupListFromNodeData():
    bpy.types.Scene.myMimicList = bpy.props.CollectionProperty(type=MyMimicListItem)
    bpy.types.Scene.myMimicList_index = IntProperty()
    
    mimicList = bpy.context.scene.myMimicList
    mimicList.clear()
    
    myNodes = bpy.context.scene.myMimicNodes
    
    for node in myNodes:
        #print("node name:{} parent:{} kids:{}".format(node.name, node.parentIndex, node.children))
        if -1 == node.parentIndex :
            NewListItem(mimicList, node)

#
#   Inserts a new item into myMimicList at position item_index
#   by copying data from node
#
def InsertBeneath( mimicList, parentIndex, parentIndent, node):
    after_index =parentIndex + 1
    item = NewListItem(mimicList,node)
    item.indent = parentIndent+1
    item_index = len(mimicList) -1 #because add() appends to end.
    mimicList.move(item_index,after_index)


def IsChild( child_node_index, parent_node_index, node_list):
    if child_node_index == -1:
        print("bad node index")
        return False
    
    child = node_list[child_node_index]
    if child.parentIndex == parent_node_index:
        return True
    return False

#
#   Operation to Expand a list item.
#
class MyMimicListItem_Expand(bpy.types.Operator):
    bl_idname = "object.mymimiclist_expand" #NOT SURE WHAT TO PUT HERE.
    bl_label = "Tool Name"
    
    button_id: IntProperty(default=0)

    def execute(self, context):
        item_index = self.button_id
        item_list = context.scene.myMimicList
        item = item_list[item_index]
        item_indent = item.indent
        
        nodeIndex = item.nodeIndex
        
        myNodes = context.scene.myMimicNodes
        
        print(item)
        if item.expanded:
            print("=== Collapse Item {} ===".format(item_index))
            item.expanded = False
            
            nextIndex = item_index+1
            while True:
                if nextIndex >= len(item_list):
                    break
                if item_list[nextIndex].indent <= item_indent:
                    break
                item_list.remove(nextIndex)
        else:
            print("=== Expand Item {} ===".format(item_index))
            item.expanded = True
            
            for n in myNodes:
                if nodeIndex == n.parentIndex:
                    InsertBeneath(item_list, item_index, item_indent, n)
            
        return {'FINISHED'}
    


#
#   Several debug operations
#   (bundled into a single operator with an "action" property)
#
class MyMimicListItem_Debug(bpy.types.Operator):
    bl_idname = "object.mymimiclist_debug"
    bl_label = "Debug"
    
    action: StringProperty(default="default")
    
    def execute(self, context):
        
        uncook_path = get_uncook_path(context)
        scene = context.scene
        action = self.action
        if "load" == action:
            
            if scene.myMimicList_index >= 0 and scene.myMimicList:
                item = scene.myMimicList[scene.myMimicList_index]
                (fileName, anim_name) = item.mimicLineId.split(';')
                fileName = os.path.join(uncook_path, fileName)
                result = load_bin_anims_single(fileName, anim_name)
                animation = result.animations[0]
                import_anims.import_anim(context, fileName, animation, use_NLA=True, NLA_track="mimic_import")
                print(fileName)
                print(anim_name)
        elif "reset3" == action:
            print("=== Debug Reset ====")
            SetupNodeData()
            SetupListFromNodeData()
        elif "clear" == action:
            print("=== Debug Clear ====")
            bpy.context.scene.myMimicList.clear()
        else:
            print("unknown debug action: "+action)

        return {'FINISHED'}


class MYMIMICLISTITEM_UL_basic(bpy.types.UIList):

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        scene = data
        #print(data, item, active_data, active_propname)
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            
            for i in range(item.indent):
                split = layout.split(factor = 0.1)
            
            col = layout.column()
            
            #print("item:{} childCount:{}".format(item.name, item.childCount)) 
            if item.childCount == 0:
               op = col.operator("object.mymimiclist_expand", text="", icon='DOT')
               op.button_id = index
               col.enabled = False
            #if False:
            #    pass
            elif item.expanded :
                op = col.operator("object.mymimiclist_expand", text="", icon='TRIA_DOWN')
                op.button_id = index
            else:
                op = col.operator("object.mymimiclist_expand", text="", icon='TRIA_RIGHT')
                op.button_id = index
            
            col = layout.column()
            col.label(text=item.name)
            

class SCENE_PT_mymimiclist(bpy.types.Panel):

    bl_label = "Quick Mimic List"
    bl_idname = "SCENE_PT_mymimiclist"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Witcher 3"

    def draw(self, context):

        scn = context.scene
        layout = self.layout
        
        row = layout.row()
        row.template_list(
            "MYMIMICLISTITEM_UL_basic",
            "",
            scn,
            "myMimicList",
            scn,
            "myMimicList_index",
            sort_lock = True
            )
            
        grid = layout.grid_flow( columns = 2 )
        
        grid.operator("object.mymimiclist_debug", text="Reset").action = "reset3"
        grid.operator("object.mymimiclist_debug", text="Clear").action = "clear"
        grid.operator("object.mymimiclist_debug", text="Load").action = "load"


classes = (
        MyMimicListNode,
        MyMimicListItem,
        MyMimicListItem_Expand,
        MyMimicListItem_Debug,
        MYMIMICLISTITEM_UL_basic,
        SCENE_PT_mymimiclist)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.myMimicNodes = bpy.props.CollectionProperty(type=MyMimicListNode)
    bpy.types.Scene.myMimicList = bpy.props.CollectionProperty(type=MyMimicListItem)
    bpy.types.Scene.myMimicList_index = IntProperty()


def unregister():
    for cls in classes:
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()