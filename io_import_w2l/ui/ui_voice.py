from pathlib import Path
from io_import_w2l.importers import import_anims
from io_import_w2l.setup_logging_bl import *
log = logging.getLogger(__name__)
from io_import_w2l import get_W3_OGG_PATH
from io_import_w2l import get_W3_VOICE_PATH

import csv
import os
import bpy
import math

from bpy.types import PropertyGroup

from bpy.props import (
    CollectionProperty,
    IntProperty,
    BoolProperty,
    StringProperty,
    PointerProperty,
)

class VoiceLineResourceManager:
    resourceManager = None
    def __init__(self):
        
        RES_DIR = Path(__file__)
        RES_DIR = str(Path(RES_DIR).parents[1])
        filename = os.path.join(RES_DIR, "CR2W\\data\\actor_voicelines.csv")
        self.pathashespath = filename
        #self.HashdumpDict = {}
        reader = csv.DictReader(open(self.pathashespath), delimiter=";")
        
        self.HashdumpDict = {}
        for row in reader:
            self.HashdumpDict[row["ID"]] = row["CAT1"]+" "+row["CAT2"]+" "+row["CAT3"]+": "+row["Caption"]+" "+row["duration"]
    @staticmethod
    def Get():
        if (VoiceLineResourceManager.resourceManager == None):
            VoiceLineResourceManager.resourceManager = VoiceLineResourceManager();
        return VoiceLineResourceManager.resourceManager;




class MyVoiceListNode(bpy.types.PropertyGroup):
    name : bpy.props.StringProperty(default="")
    selfIndex : bpy.props.IntProperty(default=-1)
    parentIndex : bpy.props.IntProperty(default=-1)
    childCount : bpy.props.IntProperty(default=0)
    voiceLineId: bpy.props.StringProperty(default="0000000000")

class MyVoiceListItem(bpy.types.PropertyGroup):
    indent: bpy.props.IntProperty(default=0)
    expanded: bpy.props.BoolProperty(default=False)
    nodeIndex : bpy.props.IntProperty(default=-1) #index into the real tree data.
    childCount: bpy.props.IntProperty(default=0) #should equal myNodes[nodeIndex].childCount
    voiceLineId: bpy.props.StringProperty(default="0000000000")

def SetupNodeData():
    voiceList = VoiceLineResourceManager().Get()
    
    myNodes = bpy.context.scene.myNodes
    myNodes.clear()
    
    for (i, item) in voiceList.HashdumpDict.items():
        node = myNodes.add()
        node.name = "{} {}".format(i, item)
        node.selfIndex = len(myNodes)-1
        node.voiceLineId = str(i)
        
    # for i in range(4):
    #     node = myNodes.add()
    #     node.name = "subnode {}".format(i)
    #     node.selfIndex = len(myNodes)-1
    #     node.parentIndex = 2

    # parentIndex = len(myNodes)-2
        
    # for i in range(2):
    #     node = myNodes.add()
    #     node.name = "subnode {}".format(i)
    #     node.selfIndex = len(myNodes)-1
    #     node.parentIndex = parentIndex
        
    # parentIndex = len(myNodes)-3
        
    # for i in range(2):
    #     node = myNodes.add()
    #     node.name = "subnode {}".format(i)
    #     node.selfIndex = len(myNodes)-1
    #     node.parentIndex = parentIndex
        
    # parentIndex = len(myNodes)-1
        
    # for i in range(2):
    #     node = myNodes.add()
    #     node.name = "subnode {}".format(i)
    #     node.selfIndex = len(myNodes)-1
    #     node.parentIndex = parentIndex
        
    # calculate childCount for all nodes
    for  node in myNodes :
        if node.parentIndex != -1:
            parent = myNodes[node.parentIndex]
            parent.childCount = parent.childCount + 1
            
    print("++++ SetupNodeData ++++")
    print("Node count: {}".format(len(myNodes)))
    for i in range(len(myNodes)):
        node = myNodes[i]
        #print("{} node:{} child:{}".format(i, node.name, node.childCount))
        

def NewListItem( voiceList, node):
    item = voiceList.add()
    item.name = node.name
    item.nodeIndex = node.selfIndex
    item.childCount = node.childCount
    item.voiceLineId = node.voiceLineId
    return item


def SetupListFromNodeData():
    bpy.types.Scene.myVoiceList = bpy.props.CollectionProperty(type=MyVoiceListItem)
    bpy.types.Scene.myVoiceList_index = IntProperty()
    
    voiceList = bpy.context.scene.myVoiceList
    voiceList.clear()
    
    myNodes = bpy.context.scene.myNodes
    
    for node in myNodes:
        #print("node name:{} parent:{} kids:{}".format(node.name, node.parentIndex, node.children))
        if -1 == node.parentIndex :
            NewListItem(voiceList, node)

#
#   Inserts a new item into myVoiceList at position item_index
#   by copying data from node
#
def InsertBeneath( voiceList, parentIndex, parentIndent, node):
    after_index =parentIndex + 1
    item = NewListItem(voiceList,node)
    item.indent = parentIndent+1
    item_index = len(voiceList) -1 #because add() appends to end.
    voiceList.move(item_index,after_index)


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
class MyVoiceListItem_Expand(bpy.types.Operator):
    bl_idname = "object.myvoicelist_expand" #NOT SURE WHAT TO PUT HERE.
    bl_label = "Tool Name"
    
    button_id: IntProperty(default=0)

    def execute(self, context):
        item_index = self.button_id
        item_list = context.scene.myVoiceList
        item = item_list[item_index]
        item_indent = item.indent
        
        nodeIndex = item.nodeIndex
        
        myNodes = context.scene.myNodes
        
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
    


#check in radish dirs if string, wav and cr2w exist. If they do add it to voice list and make it avaliaible.
radish_dirs = [
    r"E:\w3.mods\w3.modCakeTest"
]
global_sound = None
def load_voice_and_lipsync(voiceLineId, actor = None, context = None, at_frame = 0):
    unpadded_line_id = ''+voiceLineId
    if context == None:
        context = bpy.context
    namelen = len(voiceLineId)
    if namelen != 10:
        zeros = "0000000000"
        num_of_zeros = 10 - namelen
        voiceLineId = zeros[:num_of_zeros] + voiceLineId
    sound_directory_to_check: Path = Path(get_W3_OGG_PATH(context))
    cr2w_directory_to_check: Path =  Path(get_W3_VOICE_PATH(context))
    
    soundPath: Path = sound_directory_to_check / f"{voiceLineId}.ogg"
    cr2wPath: Path = cr2w_directory_to_check / f"{voiceLineId}.cr2w"
    
    
    ##? RADISH CHECKING
    if not cr2wPath.is_file():
        for dir in radish_dirs:
            dir = Path(dir) / "speech/speech.en.wem"
            files = Path(dir).glob('*')
            for file in files:
                if file.suffix == ".cr2w" and unpadded_line_id in file.stem:
                    print(file.stem)
                    cr2wPath = file
                    break
        #check radish dirs
    
    if cr2wPath.is_file() and not soundPath.is_file():
        path = cr2wPath
        folder = path.parent.name
        if "speech." in folder and ".wem" in folder and "lipsyncanim" in cr2wPath.stem:
            speechId = cr2wPath.stem.split('.')[0]
            soundFolder = str(path.parent.parent)+"\\"+path.parent.name.replace('wem','wav')
            if os.path.isdir(soundFolder):
                files = Path(soundFolder).glob('*')
                for file in files:
                    if file.suffix == ".wav" and speechId in file.stem:
                        soundPath = file
                        break
    ##? RADISH CHECKING
    
    if cr2wPath.is_file():
        log.info('Importing Lipsync')
        import_anims.import_lipsync(context, str(cr2wPath), use_NLA=True, NLA_track="voice_import", override_select=actor, at_frame=at_frame)
    if soundPath.is_file():
        log.info('Importing Sound')
        scene = context.scene 

        #bpy.ops.sequencer.delete()
        if not scene.sequence_editor:
            scene.sequence_editor_create()

        if at_frame == 0:
            sound_strips = [strip for strip in scene.sequence_editor.sequences if strip.type == 'SOUND']
            # Remove the sound strips
            for strip in sound_strips:
                scene.sequence_editor.sequences.remove(strip)

        # try:
        #     soundstrip = scene.sequence_editor.sequences.new_sound("voiceline", str(soundPath), 1, at_frame)
        # except Exception as e:

        soundstrip = scene.sequence_editor.sequences.new_sound(soundPath.stem, str(soundPath), channel=1, frame_start= math.ceil(at_frame)+1)
        soundstrip.frame_start = at_frame

class MyVoiceListItem_Debug(bpy.types.Operator):
    bl_idname = "object.myvoicelist_debug"
    bl_label = "Debug"
    
    action: StringProperty(default="default")
    
    def execute(self, context):
        scene = context.scene
        action = self.action
        if "load" == action:
            
            if scene.myVoiceList_index >= 0 and scene.myVoiceList:
                item = scene.myVoiceList[scene.myVoiceList_index]

                filename = item.voiceLineId
                load_voice_and_lipsync(filename)
                
        elif "reset3" == action:
            print("=== Debug Reset ====")
            SetupNodeData()
            SetupListFromNodeData()
        elif "clear" == action:
            print("=== Debug Clear ====")
            bpy.context.scene.myVoiceList.clear()
        else:
            print("unknown debug action: "+action)

        return {'FINISHED'}


class MYVOICELISTITEM_UL_basic(bpy.types.UIList):

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        scene = data
        #print(data, item, active_data, active_propname)
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            
            for i in range(item.indent):
                split = layout.split(factor = 0.1)
            
            col = layout.column()
            
            #print("item:{} childCount:{}".format(item.name, item.childCount)) 
            if item.childCount == 0:
               op = col.operator("object.myvoicelist_expand", text="", icon='DOT')
               op.button_id = index
               col.enabled = False
            #if False:
            #    pass
            elif item.expanded :
                op = col.operator("object.myvoicelist_expand", text="", icon='TRIA_DOWN')
                op.button_id = index
            else:
                op = col.operator("object.myvoicelist_expand", text="", icon='TRIA_RIGHT')
                op.button_id = index
            
            col = layout.column()
            col.label(text=item.name)
            
from io_import_w2l.ui.ui_utils import WITCH_PT_Base

class SCENE_PT_myvoicelist(WITCH_PT_Base, bpy.types.Panel):
    bl_parent_id = "WITCH_PT_Quick"

    bl_label = "Quick Voice List"
    bl_idname = "SCENE_PT_myvoicelist"

    def draw(self, context):

        scn = context.scene
        layout = self.layout
        
        row = layout.row()
        row.template_list(
            "MYVOICELISTITEM_UL_basic",
            "",
            scn,
            "myVoiceList",
            scn,
            "myVoiceList_index",
            sort_lock = True
            )
            
        grid = layout.grid_flow( columns = 2 )
        
        grid.operator("object.myvoicelist_debug", text="Reset").action = "reset3"
        #grid.operator("object.myvoicelist_debug", text="Clear").action = "clear"
        grid.operator("object.myvoicelist_debug", text="Load").action = "load"


classes = (
        MyVoiceListNode,
        MyVoiceListItem,
        MyVoiceListItem_Expand,
        MyVoiceListItem_Debug,
        MYVOICELISTITEM_UL_basic,
        SCENE_PT_myvoicelist)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.myNodes = bpy.props.CollectionProperty(type=MyVoiceListNode)
    bpy.types.Scene.myVoiceList = bpy.props.CollectionProperty(type=MyVoiceListItem)
    bpy.types.Scene.myVoiceList_index = IntProperty()


def unregister():
    for cls in classes:
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()