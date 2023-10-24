import os

import bpy
from io_import_w2l.importers.import_texarray import insert_color, get_texture_node, insert_heightmap_to_disp
from io_import_w2l.CR2W.CR2W_file import WORLD
from io_import_w2l import CR2W
from io_import_w2l.importers import import_w2l
from io_import_w2l.CR2W.third_party_libs import yaml

from bpy.types import PropertyGroup

from bpy.props import (
    CollectionProperty,
    IntProperty,
    BoolProperty,
    StringProperty,
    PointerProperty,
)
from io_import_w2l import get_uncook_path
from io_import_w2l import get_fbx_uncook_path

#
# This is what I am using to hold a single tree node in my raw example data.
# The entire example data is stored in **bpy.context.scene.myNodes**
#
class MyListTreeNode(bpy.types.PropertyGroup):
    selfIndex : bpy.props.IntProperty(default=-1)
    parentIndex : bpy.props.IntProperty(default=-1)
    childCount : bpy.props.IntProperty(default=0)


#
#   This represents an item that in the collection being rendered by
#   props.template_list. This collection is stored in ______
#   The collection represents a currently visible subset of MyListTreeNode
#   plus some extra info to render in a treelike fashion, eg indent.
#
class MyListTreeItem(bpy.types.PropertyGroup):
    indent: bpy.props.IntProperty(default=0)
    expanded: bpy.props.BoolProperty(default=False)
    nodeIndex : bpy.props.IntProperty(default=-1) #index into the real tree data.
    childCount: bpy.props.IntProperty(default=0) #should equal myNodes[nodeIndex].childCount
    
  
def AddNodes(groups, myNodes, parentIndex):
    node = myNodes.add()
    node.name = groups.name #"node {}".format(i)
    node.selfIndex = len(myNodes)-1
    if parentIndex:
        node.parentIndex = parentIndex
    if groups.ChildrenGroups:
        for subgroups in groups.ChildrenGroups:
            myNodes = AddNodes(subgroups, myNodes, node.selfIndex)
    if groups.ChildrenInfos:
        for ChildInfo in groups.ChildrenInfos:
            childnode = myNodes.add()
            childnode.name = ChildInfo.depotFilePath #"node {}".format(i)
            childnode.selfIndex = len(myNodes)-1
            childnode.parentIndex = node.selfIndex
    return myNodes


def SetupNodeDataWorld(world):
    bpy.types.Scene.myNodes = bpy.props.CollectionProperty(type=MyListTreeNode)
    myNodes = bpy.context.scene.myNodes
    myNodes.clear()
    
    myNodes = AddNodes(world.groups, myNodes, 0)

    # for group in world.groups.ChildrenGroups:
    #     myNodes = AddNodes(group, myNodes, False)
        # node = myNodes.add()
        # node.name = group.name #"node {}".format(i)
        # node.selfIndex = len(myNodes)-1
        
    # for i in range(4):
    #     node = myNodes.add()
    #     node.name = "subnode {}".format(i)
    #     node.selfIndex = len(myNodes)-1
    #     node.parentIndex = 2

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
        
        

def SetupNodeData():
    bpy.types.Scene.myNodes = bpy.props.CollectionProperty(type=MyListTreeNode)
    myNodes = bpy.context.scene.myNodes
    myNodes.clear()
    
    for i in range(5):
        node = myNodes.add()
        node.name = "node {}".format(i)
        node.selfIndex = len(myNodes)-1
        
    for i in range(4):
        node = myNodes.add()
        node.name = "subnode {}".format(i)
        node.selfIndex = len(myNodes)-1
        node.parentIndex = 2

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
        print("{} node:{} child:{}".format(i, node.name, node.childCount))
        
        

def NewListItem( treeList, node):
    item = treeList.add()
    item.name = node.name
    item.nodeIndex = node.selfIndex
    item.childCount = node.childCount
    return item


def seListIndexFunction(self, context):
    print("my test function", self)

def SetupListFromNodeData():
    bpy.types.Scene.myListTree = bpy.props.CollectionProperty(type=MyListTreeItem)
    bpy.types.Scene.myListTree_index = IntProperty(update=seListIndexFunction)
    
    treeList = bpy.context.scene.myListTree
    treeList.clear()
    
    myNodes = bpy.context.scene.myNodes
    
    for node in myNodes:
        #print("node name:{} parent:{} kids:{}".format(node.name, node.parentIndex, node.children))
        if -1 == node.parentIndex :
            NewListItem(treeList, node)

#
#   Inserts a new item into myListTree at position item_index
#   by copying data from node
#
def InsertBeneath( treeList, parentIndex, parentIndent, node):
    after_index =parentIndex + 1
    item = NewListItem(treeList,node)
    item.indent = parentIndent+1
    item_index = len(treeList) -1 #because add() appends to end.
    treeList.move(item_index,after_index)


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
class MyListTreeItem_Expand(bpy.types.Operator):
    bl_idname = "object.mylisttree_expand" #NOT SURE WHAT TO PUT HERE.
    bl_label = "Tool Name"
    
    button_id: IntProperty(default=0)

    def execute(self, context):
        item_index = self.button_id
        item_list = context.scene.myListTree
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
    

#
#   Several debug operations
#   (bundled into a single operator with an "action" property)
#
class MyListTreeItem_Debug(bpy.types.Operator):
    bl_idname = "object.mylisttree_debug"
    bl_label = "Debug"
    
    action: StringProperty(default="default")
    
    def execute(self, context):
        action = self.action
        if "print" == action:
            print("=== Debug Print ====")
            SetupNodeData()
            SetupListFromNodeData()
        elif "reset3" == action:
            print("=== Debug Reset ====")
            SetupListFromNodeData()
        elif "clear" == action:
            print("=== Debug Clear ====")
            bpy.context.scene.myListTree.clear()
        elif "group" == action:
            if True:
                with open(r"F:\RE3R_MODS\Blender_Scripts\io_import_w2l\test_level.yml", "r") as file:
                    levels_yml = yaml.full_load(file)

                    for list_name, filePaths in levels_yml.items():
                        for levelPath in filePaths:
                            levelFile = CR2W.CR2W_reader.load_w2l(levelPath)
                            import_w2l.btn_import_W2L(levelFile)

            return {'FINISHED'}
            print("=== group load ====")
            myListTree_index = context.scene.myListTree_index
            item_list = context.scene.myListTree
            item = item_list[myListTree_index]
            item_indent = item.indent
            
            nodeIndex = item.nodeIndex
            
            myNodes = context.scene.myNodes
            uncook_path = get_uncook_path(context)
            fbx_uncook_path = get_fbx_uncook_path(context)
            for n in myNodes:
                if nodeIndex == n.parentIndex:
                    full_path = os.path.join(uncook_path, n.name)
                    ext = os.path.splitext(full_path)[-1].lower()
                    if ext == ".w2l":
                        level_file = CR2W.CR2W_reader.load_w2l(full_path)
                        import_w2l.btn_import_W2L(level_file, fbx_uncook_path)
                        #InsertBeneath(item_list, item_index, item_indent, n)
                    #TODO ADD SUBGROUPS?
        elif "level" == action:
            myListTree_index = context.scene.myListTree_index
            print(myListTree_index)
            treeList = context.scene.myListTree
            #myNodes = bpy.context.scene.myNodes
            print(treeList[myListTree_index].name)
            uncook_path = get_uncook_path(context)
            fbx_uncook_path = get_fbx_uncook_path(context)
            full_path = os.path.join(uncook_path, treeList[myListTree_index].name)
            level_file = CR2W.CR2W_reader.load_w2l(full_path)
            import_w2l.btn_import_W2L(level_file, fbx_uncook_path)
            # for node in myNodes:
            #     print("node name:{} parent:{} kids:{}".format(node.name, node.parentIndex, node.children))
            print("=== level load ====")
        else:
            print("unknown debug action: "+action)

        return {'FINISHED'}


#
#   My List UI class to draw my MyListTreeItem
#   (The most important thing it does is show how to draw a list item)
#
#note this naming convention is important. For more info search for _UL_ in:
# https://wiki.blender.org/wiki/Reference/Release_Notes/2.80/Python_API/Addons
class MYLISTTREEITEM_UL_basic(bpy.types.UIList):

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        scene = data
        #print(data, item, active_data, active_propname)
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            
            for i in range(item.indent):
                split = layout.split(factor = 0.1)
            
            col = layout.column()
            
            #print("item:{} childCount:{}".format(item.name, item.childCount)) 
            if item.childCount == 0:
               op = col.operator("object.mylisttree_expand", text="", icon='DOT')
               op.button_id = index
               col.enabled = False
            #if False:
            #    pass
            elif item.expanded :
                op = col.operator("object.mylisttree_expand", text="", icon='TRIA_DOWN')
                op.button_id = index
            else:
                op = col.operator("object.mylisttree_expand", text="", icon='TRIA_RIGHT')
                op.button_id = index
            
            col = layout.column()
            col.label(text=item.name)
            

#
#   My Panel UI, assigned to view.
#
class SCENE_PT_mylisttree(bpy.types.Panel):

    bl_label = "My List Tree"
    bl_idname = "SCENE_PT_mylisttree"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "My Category"

    def draw(self, context):

        scn = context.scene
        layout = self.layout
        
        row = layout.row()
        row.template_list(
            "MYLISTTREEITEM_UL_basic",
            "",
            scn,
            "myListTree",
            scn,
            "myListTree_index",
            sort_lock = True
            )
            
        grid = layout.grid_flow( columns = 2 )
        
        grid.operator("object.mylisttree_debug", text="Reset").action = "reset3"
        grid.operator("object.mylisttree_debug", text="Clear").action = "clear"
        grid.operator("object.mylisttree_debug", text="Print").action = "print"


def AddCLayerGroup(groups, parent_collection):
    this_collection = bpy.data.collections.new(groups.name)
    this_collection['group_type'] = "LayerGroup"
    if parent_collection:
        parent_collection.children.link(this_collection)
    if groups.ChildrenGroups:
        for subgroups in groups.ChildrenGroups:
            AddCLayerGroup(subgroups, this_collection)
    if groups.ChildrenInfos:
        for ChildInfo in groups.ChildrenInfos:
            child_collection = bpy.data.collections.new(os.path.basename(ChildInfo.depotFilePath))
            child_collection['level_path'] = ChildInfo.depotFilePath
            child_collection['layerBuildTag'] = ChildInfo.layerBuildTag
            child_collection['group_type'] = "LayerInfo"
            this_collection.children.link(child_collection)
            
            tags = {
                "LBT_None" : "NONE",
                "LBT_Ignored" : "COLOR_01",
                "LBT_EnvOutdoor" : "COLOR_02",
                "LBT_EnvIndoor" : "COLOR_03",
                "LBT_EnvUnderground" : "COLOR_08",
                "LBT_Quest" : "COLOR_05",
                "LBT_Communities" : "COLOR_06",
                "LBT_Audio" : "COLOR_07",
                "LBT_Nav" : "COLOR_06",
                "LBT_Gameplay" : "COLOR_04",
                "LBT_DLC" : "COLOR_06"
            }
            if ChildInfo.layerBuildTag:
                child_collection.color_tag = tags[ChildInfo.layerBuildTag]
            # if ChildInfo.layerBuildTag == "LBT_EnvIndoor":
            #     child_collection.color_tag = "COLOR_02"
            # elif ChildInfo.layerBuildTag == "LBT_Gameplay":
            #     child_collection.color_tag = "COLOR_03"
            # elif ChildInfo.layerBuildTag == "LBT_EnvOutdoor":
            #     child_collection.color_tag = "COLOR_04"
            # elif ChildInfo.layerBuildTag == "LBT_Communities":
            #     child_collection.color_tag = "COLOR_06"
            # elif ChildInfo.layerBuildTag == "LBT_Quest":
            #     child_collection.color_tag = "COLOR_07"
            # else:
            #     child_collection.color_tag = "NONE"
                
    return this_collection



def btn_import_w2w(worldFile: WORLD, filePath):
    # collection = bpy.data.collections.new(worldFile.worldName)
    # collection['world_path'] = worldFile.worldName
    collection = AddCLayerGroup(worldFile.groups, False)
    bpy.context.scene.collection.children.link(collection)
    layer_collection = bpy.context.view_layer.layer_collection.children[collection.name]
    bpy.context.view_layer.active_layer_collection = layer_collection
    

    worldFile.heightMap = Path(filePath).stem+'.heightmap.png'
    worldFile.colormap = Path(filePath).stem+'.overlay.png'
    do_import_map_terrain(worldFile, filePath)


from pathlib import Path
def btn_import_radish(filename):
    filePath = Path(filename).parent
    with open(filename, "r") as file:
        levels_yml = yaml.full_load(file)
        data = levels_yml["WorldDefinition"]
        worldFile = WORLD()
        worldFile.worldName = data['name']
        worldFile.terrainSize = data['terrain']['terrainSize']
        worldFile.lowestElevation = data['terrain']['minHeight']
        worldFile.highestElevation = data['terrain']['maxHeight']
        worldFile.heightMap = data['terrain']['heightfield']
        worldFile.colormap = data['terrain']['colormap']
        worldFile.tileRes = data['terrain']['tileRes']
        do_import_map_terrain(worldFile, filePath)

def do_import_map_terrain(worldFile, filePath):
    heightmap_file:Path = Path(filePath).parent / worldFile.heightMap
    colormap_file:Path = Path(filePath).parent / worldFile.colormap
    path_to_search_for_maps = Path("E:\w3.modding\w3terrain-extract-v2020-03-30")

    if not heightmap_file.is_file():
        heightmap_file = Path(r"E:\w3.modding\w3terrain-extract-v2020-03-30\prolog_village\prolog_village.heightmap.png")
        for fname in path_to_search_for_maps.rglob("*"):
            if fname.is_file() and fname.name == worldFile.heightMap:
                heightmap_file = fname
                break
    if not colormap_file.is_file():
        colormap_file = Path(r"E:\w3.modding\w3terrain-extract-v2020-03-30\prolog_village\prolog_village.colormap.png")
        for fname in path_to_search_for_maps.rglob("*"):
            if fname.is_file() and fname.name == worldFile.colormap:
                colormap_file = fname
                break

    water_obj = bpy.context.scene.objects.get("water_for_map")
    if not water_obj:
        bpy.ops.mesh.primitive_plane_add(size=worldFile.terrainSize, enter_editmode=False, align='WORLD', location=(0, 0, 0), scale=(1, 1, 1))
        obj: bpy.types.Object = bpy.context.selected_objects[:][0]
        obj.name = "water_for_map"
        water_mat = bpy.data.materials.new(name='water_m')
        water_mat.use_nodes = True
        water_mat.diffuse_color = [0.0, 0.5, 0.8, 1.0]
        obj.data.materials.append(water_mat)
    
    
    
    bpy.ops.object.select_all(action='DESELECT')
    
    bpy.ops.mesh.primitive_plane_add(size=worldFile.terrainSize, enter_editmode=False, align='WORLD', location=(0, 0, worldFile.lowestElevation), scale=(1, 1, 1))
    obj: bpy.types.Object = bpy.context.selected_objects[:][0]
    obj.name = worldFile.worldName
    #obj.location = [0,0]
    for a in bpy.context.screen.areas:
        if a.type == 'VIEW_3D':
            for s in a.spaces:
                if s.type == 'VIEW_3D':
                    s.clip_end = 9999
    
    def apply_geo_nodes():
        #############
        # MODIFIERS #
        #bpy.context.scene.cycles.feature_set = 'EXPERIMENTAL'
        # bpy.context.scene.render.engine = 'CYCLES'
        # bpy.context.scene.cycles.device = 'GPU'
        #obj.cycles.use_adaptive_subdivision = True

        #bpy.ops.object.modifier_set_active(modifier="GeometryNodes")

        gn_modifier:bpy.types.Modifier = obj.modifiers.new(type='NODES', name="GeometryNodes")

        ngt:bpy.types.GeometryNodeTree = bpy.context.blend_data.node_groups.new(
            type='GeometryNodeTree',
            name='Geometry Nodes'
        )
        gn_modifier.node_group = ngt

        # create group inputs
        group_inputs = ngt.nodes.new('NodeGroupInput')
        group_inputs.location = (-550,0)
        # create group outputs
        group_outputs = ngt.nodes.new('NodeGroupOutput')
        group_outputs.location = (300,0)
        
        ngt.outputs.new('NodeSocketGeometry',"Geometry")
        ngt.inputs.new('NodeSocketGeometry',"Geometry")
        ngt.inputs.new('NodeSocketVector',"Input")
        
        bpy.ops.object.geometry_nodes_input_attribute_toggle(prop_path="[\"Input_2_use_attribute\"]", modifier_name=gn_modifier.name)
        gn_modifier["Input_2_attribute_name"] = "UVMap"
        
        nodeImg:bpy.types.GeometryNodeImageTexture = ngt.nodes.new(type="GeometryNodeImageTexture")
        nodeImg.width = 300
        nodeImg.location = (-320,0)
        path = str(heightmap_file)
        image = bpy.data.images.load(path, check_existing=True)
        image.colorspace_settings.name = 'Non-Color'
        nodeImg.inputs['Image'].default_value = image
        
        nodeS1 = ngt.nodes.new(type="ShaderNodeVectorMath")
        nodeS1.location = (-320,-300)
        nodeS1.operation = 'SCALE'
        nodeS2 = ngt.nodes.new(type="ShaderNodeVectorMath")
        nodeS2.location = (0,-300)
        nodeS2.operation = 'SCALE'
        nodeS2.inputs[3].default_value = abs(worldFile.lowestElevation) + abs(worldFile.highestElevation)
        ngt.links.new(nodeS1.outputs[0], nodeS2.inputs[0])
        ngt.links.new(nodeImg.outputs[0], nodeS1.inputs[3])
        ngt.links.new(group_inputs.outputs[1], nodeImg.inputs[1])
        
        nodenorm = ngt.nodes.new('GeometryNodeInputNormal')
        nodenorm.location = (-350,-350)
        ngt.links.new(nodenorm.outputs['Normal'], nodeS1.inputs['Vector'])
        
        
        nodeSP = ngt.nodes.new(type="GeometryNodeSetPosition")
        nodeSP.location = (0,0)
        ngt.links.new(group_inputs.outputs[0], nodeSP.inputs[0])
        ngt.links.new(nodeS2.outputs[0], nodeSP.inputs[3])
        ngt.links.new(nodeSP.outputs[0], group_outputs.inputs[0])

    def apply_materials():
        #############
        # MATERIALS #
        mat = bpy.data.materials.new(name=worldFile.worldName+'_m') #set new material to variable
        mat.use_nodes = True
        mat.cycles.displacement_method = 'DISPLACEMENT'

        obj.data.materials.append(mat) #add the material to the object
        Material_Output = mat.node_tree.nodes.get("Material Output")

        disp_material = mat.node_tree.nodes.get(mat.name+"_Displacement")
        if disp_material == None:
            disp_material =  mat.node_tree.nodes.new("ShaderNodeDisplacement")
            disp_material.name = mat.name+"_Displacement"
            disp_material.location = (disp_material.location[0]+320, disp_material.location[1]-300)

        disp_material.inputs[1].default_value = worldFile.lowestElevation/100
        disp_material.inputs[2].default_value = worldFile.highestElevation + abs(worldFile.lowestElevation)



        mat.node_tree.links.new(Material_Output.inputs["Displacement"], disp_material.outputs["Displacement"])

        #Disp MAP
        path = str(heightmap_file)
        filename = os.path.basename(path)
        tex = get_texture_node(filename, mat)
        if tex == None:
            tex = mat.node_tree.nodes.new("ShaderNodeTexImage")
        insert_heightmap_to_disp(mat, disp_material, tex, path)
        
        
        principled = mat.node_tree.nodes.get("Principled BSDF")
        if principled == None:
            principled =  mat.node_tree.nodes.new("ShaderNodeBsdfPrincipled")
        # mapping =  mat.node_tree.nodes.get("Mapping")
        # if mapping == None:
        #     mapping =  mat.node_tree.nodes.new("Mapping")
        #COLOUR MAP
        path = str(colormap_file)
        color_path = path
        color_filename = os.path.basename(color_path)
        tex = get_texture_node(color_filename, mat)
        if tex == None:
            tex = mat.node_tree.nodes.new("ShaderNodeTexImage")
        insert_color(mat, principled, tex, None, color_path)
    
    def apply_MultiresModifier():
        multires:bpy.types.MultiresModifier = obj.modifiers.new(type='MULTIRES', name="tileres")
        
        for _ in range(7):
            bpy.ops.object.multires_subdivide(modifier=multires.name, mode='LINEAR')

    apply_geo_nodes()
    # apply_materials()
    apply_MultiresModifier()

classes = (
        MyListTreeNode,
        MyListTreeItem,
        MyListTreeItem_Expand,
        MyListTreeItem_Debug,
        MYLISTTREEITEM_UL_basic)#,
        #SCENE_PT_mylisttree)


from bpy.utils import (register_class, unregister_class)
def register():
    for cls in classes:
        register_class(cls)

    # SetupNodeData()
    # SetupListFromNodeData()


def unregister():
    for cls in classes:
        unregister_class(cls)


if __name__ == "__main__":
    register()