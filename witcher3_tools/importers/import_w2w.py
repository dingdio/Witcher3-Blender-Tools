import logging
import os

import bpy

log = logging.getLogger(__name__)
import numpy as np
from ..importers.import_texarray import insert_color, get_texture_node, insert_heightmap_to_disp
from ..importers import terrain_w2ter
from ..CR2W.CR2W_file import WORLD
from ..CR2W.common_blender import repo_file, bpy_image_load_safe
from .. import CR2W
from ..importers import import_w2l
from ..CR2W.third_party_libs import yaml

from bpy.types import PropertyGroup

from bpy.props import (
    CollectionProperty,
    IntProperty,
    BoolProperty,
    StringProperty,
    PointerProperty,
)
from .. import get_uncook_path
from .. import get_fbx_uncook_path
from ..extension_paths import get_dev_override

W2W_NODES_PROP = "witcher_w2w_nodes"
W2W_LIST_PROP = "witcher_w2w_list_tree"
W2W_LIST_INDEX_PROP = "witcher_w2w_list_tree_index"

#
# This is what I am using to hold a single tree node in my raw example data.
# The entire example data is stored in **bpy.context.scene.witcher_w2w_nodes**
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
    myNodes = getattr(bpy.context.scene, W2W_NODES_PROP, None)
    if myNodes is None:
        return
    myNodes.clear()
    
    myNodes = AddNodes(world.groups, myNodes, 0)

    # calculate childCount for all nodes
    for  node in myNodes :
        if node.parentIndex != -1:
            parent = myNodes[node.parentIndex]
            parent.childCount = parent.childCount + 1
            
    log.debug("SetupNodeData: Node count: %d", len(myNodes))
    for i in range(len(myNodes)):
        node = myNodes[i]
        log.debug("  %d node:%s child:%d", i, node.name, node.childCount)


def SetupNodeData():
    myNodes = getattr(bpy.context.scene, W2W_NODES_PROP, None)
    if myNodes is None:
        return
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
        
    # calculate childCount for all nodes
    for  node in myNodes :
        if node.parentIndex != -1:
            parent = myNodes[node.parentIndex]
            parent.childCount = parent.childCount + 1
            
    log.debug("SetupNodeData: Node count: %d", len(myNodes))
    for i in range(len(myNodes)):
        node = myNodes[i]
        log.debug("  %d node:%s child:%d", i, node.name, node.childCount)


def NewListItem( treeList, node):
    item = treeList.add()
    item.name = node.name
    item.nodeIndex = node.selfIndex
    item.childCount = node.childCount
    return item


def seListIndexFunction(self, context):
    log.debug("seListIndexFunction called: %s", self)

def SetupListFromNodeData():
    scene = bpy.context.scene
    treeList = getattr(scene, W2W_LIST_PROP, None)
    myNodes = getattr(scene, W2W_NODES_PROP, None)
    if treeList is None or myNodes is None:
        return
    treeList.clear()
    
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
        log.warning("bad node index")
        return False
    
    child = node_list[child_node_index]
    if child.parentIndex == parent_node_index:
        return True
    return False



#
#   Operation to Expand a list item.
#
class MyListTreeItem_Expand(bpy.types.Operator):
    bl_idname = "witcher.w2w_listtree_expand" #NOT SURE WHAT TO PUT HERE.
    bl_label = "Tool Name"
    
    button_id: IntProperty(default=0)

    def execute(self, context):
        item_index = self.button_id
        item_list = context.scene.witcher_w2w_list_tree
        item = item_list[item_index]
        item_indent = item.indent
        
        nodeIndex = item.nodeIndex
        
        myNodes = context.scene.witcher_w2w_nodes
        
        log.debug("Toggle item: %s", item)
        if item.expanded:
            log.debug("Collapse Item %d", item_index)
            item.expanded = False
            
            nextIndex = item_index+1
            while True:
                if nextIndex >= len(item_list):
                    break
                if item_list[nextIndex].indent <= item_indent:
                    break
                item_list.remove(nextIndex)
        else:
            log.debug("Expand Item %d", item_index)
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
    bl_idname = "witcher.w2w_listtree_debug"
    bl_label = "Debug"
    
    action: StringProperty(default="default")
    
    def execute(self, context):
        action = self.action
        if "print" == action:
            log.debug("Debug Print")
            SetupNodeData()
            SetupListFromNodeData()
        elif "reset3" == action:
            log.debug("Debug Reset")
            SetupListFromNodeData()
        elif "clear" == action:
            log.debug("Debug Clear")
            bpy.context.scene.witcher_w2w_list_tree.clear()
        elif "group" == action:
            if True:
                debug_yml = get_dev_override("w2w_debug_level_yml", "")
                if not debug_yml or not os.path.isfile(debug_yml):
                    self.report({'WARNING'}, "No dev W2W debug YAML configured")
                    return {'CANCELLED'}
                with open(debug_yml, "r") as file:
                    levels_yml = yaml.full_load(file)

                    for list_name, filePaths in levels_yml.items():
                        for levelPath in filePaths:
                            levelFile = CR2W.CR2W_reader.load_w2l(levelPath)
                            import_w2l.btn_import_W2L(levelFile)

            return {'FINISHED'}

        elif "level" == action:
            myListTree_index = context.scene.witcher_w2w_list_tree_index
            log.debug("Level index: %s", myListTree_index)
            treeList = context.scene.witcher_w2w_list_tree
            #myNodes = bpy.context.scene.witcher_w2w_nodes
            log.debug("Level name: %s", treeList[myListTree_index].name)
            uncook_path = get_uncook_path(context)
            fbx_uncook_path = get_fbx_uncook_path(context)
            full_path = os.path.join(uncook_path, treeList[myListTree_index].name)
            level_file = CR2W.CR2W_reader.load_w2l(full_path)
            import_w2l.btn_import_W2L(level_file, fbx_uncook_path)
            # for node in myNodes:
            #     print("node name:{} parent:{} kids:{}".format(node.name, node.parentIndex, node.children))
            log.debug("level load")
        else:
            log.warning("unknown debug action: %s", action)

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
               op = col.operator("witcher.w2w_listtree_expand", text="", icon='DOT')
               op.button_id = index
               col.enabled = False
            #if False:
            #    pass
            elif item.expanded :
                op = col.operator("witcher.w2w_listtree_expand", text="", icon='TRIA_DOWN')
                op.button_id = index
            else:
                op = col.operator("witcher.w2w_listtree_expand", text="", icon='TRIA_RIGHT')
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
            "witcher_w2w_list_tree",
            scn,
            "witcher_w2w_list_tree_index",
            sort_lock = True
            )
            
        grid = layout.grid_flow( columns = 2 )
        
        grid.operator("witcher.w2w_listtree_debug", text="Reset").action = "reset3"
        grid.operator("witcher.w2w_listtree_debug", text="Clear").action = "clear"
        grid.operator("witcher.w2w_listtree_debug", text="Print").action = "print"


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

    return this_collection



def btn_import_w2w(worldFile: WORLD, filePath):
    collection = AddCLayerGroup(worldFile.groups, False)
    bpy.context.scene.collection.children.link(collection)
    layer_collection = bpy.context.view_layer.layer_collection.children[collection.name]
    bpy.context.view_layer.active_layer_collection = layer_collection

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

def _resolve_tile_buffer(terrain_tiles_dir, terrain_tiles_rel, buf_name):
    """Find a tile buffer file on disk or extract from bundle via repo_file."""
    # Check disk first
    disk_path = os.path.join(str(terrain_tiles_dir), buf_name)
    if os.path.isfile(disk_path):
        return disk_path
    # Try bundle extraction via repo_file
    if terrain_tiles_rel:
        rel_path = os.path.join(terrain_tiles_rel, buf_name)
        abs_path = repo_file(rel_path)
        if abs_path and os.path.exists(abs_path):
            return abs_path
    return None


def _discover_tile_count(terrain_tiles_dir):
    """Discover max tile count by scanning files on disk."""
    max_coord = -1
    if os.path.isdir(str(terrain_tiles_dir)):
        try:
            for entry in os.scandir(str(terrain_tiles_dir)):
                info = terrain_w2ter.parse_tile_filename(entry.name)
                if info:
                    max_coord = max(max_coord, info.x, info.y)
        except Exception:
            pass
    if max_coord >= 0:
        return max_coord + 1
    return 0


TERRAIN_IMPORT_FULL_MAP = "FULL_MAP"
TERRAIN_IMPORT_TILES = "TILES"


def _resolve_terrain_context(worldFile, filePath):
    fpath = Path(filePath)
    # filePath may be a .w2w file or a directory (radish import)
    if fpath.is_dir():
        hub_name = fpath.name
        w2w_dir = fpath
    else:
        hub_name = fpath.stem
        w2w_dir = fpath.parent
    terrain_tiles_dir = w2w_dir / "terrain_tiles"

    # Compute relative path for bundle extraction
    terrain_tiles_rel = None
    try:
        uncook_path = get_uncook_path(bpy.context)
        if uncook_path:
            rel_dir = os.path.relpath(str(w2w_dir), uncook_path)
            terrain_tiles_rel = os.path.join(rel_dir, "terrain_tiles")
    except Exception:
        pass

    # Compute tile grid from WORLD params
    tile_res = worldFile.tileRes or 256
    clipmap = worldFile.clipmapSize or worldFile.clipSize or 0
    n_tiles = 0
    if clipmap and tile_res and clipmap % tile_res == 0:
        n_tiles = clipmap // tile_res

    # Discover from disk if w2w didn't provide grid size
    if n_tiles <= 0:
        n_tiles = _discover_tile_count(terrain_tiles_dir)

    return {
        "hub_name": hub_name,
        "w2w_dir": w2w_dir,
        "terrain_tiles_dir": terrain_tiles_dir,
        "terrain_tiles_rel": terrain_tiles_rel,
        "tile_res": tile_res,
        "n_tiles": n_tiles,
    }


def _get_scene_terrain_multires_level():
    try:
        return int(bpy.context.scene.witcher_file_browser.terrain_multires_level)
    except Exception:
        return 5


def _get_scene_terrain_import_mode():
    try:
        mode = str(bpy.context.scene.witcher_file_browser.terrain_import_mode)
        if mode in {TERRAIN_IMPORT_FULL_MAP, TERRAIN_IMPORT_TILES}:
            return mode
    except Exception:
        pass
    return TERRAIN_IMPORT_FULL_MAP


def _collect_tile_buffer_paths_for_combine(terrain_tiles_dir, terrain_tiles_rel, n_tiles, tile_res):
    """Collect tile buffer paths for combine workflow.

    Includes existing on-disk buffers and tries to resolve key buffers from bundle.
    """
    buffer_paths = []
    seen = set()

    if os.path.isdir(str(terrain_tiles_dir)):
        try:
            for entry in os.scandir(str(terrain_tiles_dir)):
                if not entry.is_file():
                    continue
                if not terrain_w2ter.is_w2ter_buffer_name(entry.name):
                    continue
                apath = os.path.abspath(entry.path)
                if apath not in seen:
                    buffer_paths.append(apath)
                    seen.add(apath)
        except Exception:
            pass

    # Ensure required height/texture buffers can be resolved from bundle paths too.
    for y in range(max(0, int(n_tiles))):
        for x in range(max(0, int(n_tiles))):
            tile_name = f"tile_{y}_x_{x}_res{tile_res}"
            for idx in (1, 2):
                buf_name = f"{tile_name}.w2ter.{idx}.buffer"
                buf_path = _resolve_tile_buffer(terrain_tiles_dir, terrain_tiles_rel, buf_name)
                if not buf_path:
                    continue
                apath = os.path.abspath(buf_path)
                if apath not in seen:
                    buffer_paths.append(apath)
                    seen.add(apath)

    return buffer_paths


def _create_full_map_geo_nodes(obj, heightmap_path, lowest_elevation, highest_elevation):
    """Create Geometry Nodes modifier that displaces mesh from terrain heightmap."""
    gn_modifier = obj.modifiers.new(type='NODES', name="terrain_geo")

    ngt = bpy.context.blend_data.node_groups.new(
        type='GeometryNodeTree',
        name=f"{obj.name}_TerrainGeo",
    )
    gn_modifier.node_group = ngt

    group_inputs = ngt.nodes.new('NodeGroupInput')
    group_inputs.location = (-550, 0)
    group_outputs = ngt.nodes.new('NodeGroupOutput')
    group_outputs.location = (300, 0)

    use_interface = hasattr(ngt, "interface") and hasattr(ngt.interface, "new_socket")

    def add_group_socket(name: str, in_out: str, socket_type: str):
        if use_interface:
            return ngt.interface.new_socket(name=name, in_out=in_out, socket_type=socket_type)
        collection = ngt.inputs if in_out == 'INPUT' else ngt.outputs
        return collection.new(socket_type, name)

    add_group_socket("Geometry", "OUTPUT", "NodeSocketGeometry")
    add_group_socket("Geometry", "INPUT", "NodeSocketGeometry")

    node_img = ngt.nodes.new(type="GeometryNodeImageTexture")
    node_img.width = 300
    node_img.location = (-320, 0)
    image = bpy_image_load_safe(str(heightmap_path), check_existing=True)
    image.colorspace_settings.name = 'Non-Color'
    node_img.inputs['Image'].default_value = image

    node_s1 = ngt.nodes.new(type="ShaderNodeVectorMath")
    node_s1.location = (-320, -300)
    node_s1.operation = 'SCALE'

    node_s2 = ngt.nodes.new(type="ShaderNodeVectorMath")
    node_s2.location = (0, -300)
    node_s2.operation = 'SCALE'
    node_s2.inputs[3].default_value = abs(float(lowest_elevation)) + abs(float(highest_elevation))

    ngt.links.new(node_s1.outputs[0], node_s2.inputs[0])
    ngt.links.new(node_img.outputs[0], node_s1.inputs[3])

    uv_vector_output = None
    if use_interface:
        try:
            node_uv = ngt.nodes.new("GeometryNodeInputNamedAttribute")
            node_uv.location = (-520, -140)
            try:
                node_uv.data_type = 'FLOAT_VECTOR'
            except Exception:
                try:
                    node_uv.data_type = 'VECTOR'
                except Exception:
                    pass
            if "Name" in node_uv.inputs:
                node_uv.inputs["Name"].default_value = "UVMap"
            else:
                node_uv.inputs[0].default_value = "UVMap"
            uv_vector_output = node_uv.outputs[0]
        except Exception:
            uv_vector_output = None

    if uv_vector_output is None:
        add_group_socket("Input", "INPUT", "NodeSocketVector")
        try:
            bpy.ops.object.geometry_nodes_input_attribute_toggle(
                prop_path="[\"Input_2_use_attribute\"]",
                modifier_name=gn_modifier.name,
            )
            gn_modifier["Input_2_attribute_name"] = "UVMap"
        except Exception:
            pass
        uv_vector_output = group_inputs.outputs.get("Input") or group_inputs.outputs[1]

    ngt.links.new(uv_vector_output, node_img.inputs["Vector"])

    node_norm = ngt.nodes.new('GeometryNodeInputNormal')
    node_norm.location = (-350, -350)
    ngt.links.new(node_norm.outputs['Normal'], node_s1.inputs['Vector'])

    node_sp = ngt.nodes.new(type="GeometryNodeSetPosition")
    node_sp.location = (0, 0)
    ngt.links.new(group_inputs.outputs.get("Geometry") or group_inputs.outputs[0], node_sp.inputs[0])
    ngt.links.new(node_s2.outputs[0], node_sp.inputs[3])
    ngt.links.new(node_sp.outputs[0], group_outputs.inputs.get("Geometry") or group_outputs.inputs[0])

    return gn_modifier


def _get_scene_terrain_material_values():
    roughness = 0.82
    specular = 0.12
    try:
        tool = bpy.context.scene.witcher_file_browser
        roughness = float(getattr(tool, "terrain_material_roughness", roughness))
        specular = float(getattr(tool, "terrain_material_specular", specular))
    except Exception:
        pass
    roughness = max(0.0, min(1.0, roughness))
    specular = max(0.0, min(1.0, specular))
    return roughness, specular


def _set_principled_terrain_values(principled, roughness=None, specular=None):
    if principled is None:
        return
    if roughness is None or specular is None:
        roughness, specular = _get_scene_terrain_material_values()
    if "Roughness" in principled.inputs:
        principled.inputs["Roughness"].default_value = float(roughness)
    if "Metallic" in principled.inputs:
        principled.inputs["Metallic"].default_value = 0.0
    if "Specular IOR Level" in principled.inputs:
        principled.inputs["Specular IOR Level"].default_value = float(specular)
    elif "Specular" in principled.inputs:
        principled.inputs["Specular"].default_value = float(specular)


def _apply_terrain_material_values(mat, roughness, specular):
    if mat is None or not mat.use_nodes or mat.node_tree is None:
        return False
    principled = mat.node_tree.nodes.get("Principled BSDF")
    if principled is None:
        return False
    _set_principled_terrain_values(principled, roughness, specular)
    mat["witcher_terrain_material"] = True
    return True


def _is_terrain_mesh_object(obj):
    if obj is None or obj.type != 'MESH':
        return False
    if obj.get("terrain_mode") == "full_map":
        return True
    return ("terrain_multires" in obj and "tile_x" in obj and "tile_y" in obj)


def update_all_terrain_material_values(roughness, specular):
    updated = 0
    seen = set()

    # First pass: explicitly tagged terrain materials.
    for mat in bpy.data.materials:
        if not mat.get("witcher_terrain_material"):
            continue
        if _apply_terrain_material_values(mat, roughness, specular):
            seen.add(mat.name_full)
            updated += 1

    # Second pass: materials currently assigned to terrain objects (for backward compatibility).
    for obj in bpy.data.objects:
        if not _is_terrain_mesh_object(obj):
            continue
        if not hasattr(obj.data, "materials"):
            continue
        for mat in obj.data.materials:
            if mat is None:
                continue
            if mat.name_full in seen:
                continue
            if _apply_terrain_material_values(mat, roughness, specular):
                seen.add(mat.name_full)
                updated += 1
    return updated


def _create_full_map_material(obj, colormap_path, mat_name):
    """Create simple material using combined overlay image as Base Color."""
    mat = bpy.data.materials.new(name=mat_name)
    mat.use_nodes = True
    principled = mat.node_tree.nodes.get("Principled BSDF")
    if principled is None:
        principled = mat.node_tree.nodes.new("ShaderNodeBsdfPrincipled")
    tex = mat.node_tree.nodes.new("ShaderNodeTexImage")
    insert_color(mat, principled, tex, None, str(colormap_path))
    _set_principled_terrain_values(principled)
    mat["witcher_terrain_material"] = True
    obj.data.materials.clear()
    obj.data.materials.append(mat)
    return mat


def _add_multires_modifier(obj, level):
    multires = obj.modifiers.new(type='MULTIRES', name="tileres")
    for _ in range(max(0, int(level))):
        bpy.ops.object.multires_subdivide(modifier=multires.name, mode='LINEAR')
    target = max(0, int(level))
    if hasattr(multires, "levels"):
        multires.levels = min(target, int(getattr(multires, "total_levels", target)))
    if hasattr(multires, "sculpt_levels"):
        multires.sculpt_levels = min(target, int(getattr(multires, "total_levels", target)))
    if hasattr(multires, "render_levels"):
        multires.render_levels = min(target, int(getattr(multires, "total_levels", target)))
    return multires


def _ensure_simple_water_material():
    mat_name = "water_simple_m"
    mat = bpy.data.materials.get(mat_name)
    if mat is None:
        mat = bpy.data.materials.new(name=mat_name)
    mat.use_nodes = True

    nt = mat.node_tree
    nodes = nt.nodes
    links = nt.links
    nodes.clear()

    out = nodes.new("ShaderNodeOutputMaterial")
    out.location = (320, 0)
    bsdf = nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (60, 0)
    links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

    bsdf.inputs["Base Color"].default_value = (0.02, 0.16, 0.24, 1.0)
    bsdf.inputs["Roughness"].default_value = 0.05
    if "Specular IOR Level" in bsdf.inputs:
        bsdf.inputs["Specular IOR Level"].default_value = 0.7
    elif "Specular" in bsdf.inputs:
        bsdf.inputs["Specular"].default_value = 0.7
    if "Transmission Weight" in bsdf.inputs:
        bsdf.inputs["Transmission Weight"].default_value = 0.92
    elif "Transmission" in bsdf.inputs:
        bsdf.inputs["Transmission"].default_value = 0.92
    if "IOR" in bsdf.inputs:
        bsdf.inputs["IOR"].default_value = 1.333

    # Small normal breakup for simple water look.
    noise = nodes.new("ShaderNodeTexNoise")
    noise.location = (-520, -120)
    noise.inputs["Scale"].default_value = 18.0
    noise.inputs["Detail"].default_value = 2.0

    bump = nodes.new("ShaderNodeBump")
    bump.location = (-220, -120)
    bump.inputs["Strength"].default_value = 0.06
    links.new(noise.outputs["Fac"], bump.inputs["Height"])
    links.new(bump.outputs["Normal"], bsdf.inputs["Normal"])

    if hasattr(mat, "blend_method"):
        mat.blend_method = 'BLEND'
    if hasattr(mat, "shadow_method"):
        mat.shadow_method = 'HASHED'
    return mat


def _ensure_world_water_plane(hub_name, terrain_size):
    obj_name = f"water_for_{hub_name}"
    water_obj = bpy.data.objects.get(obj_name)
    if water_obj is None:
        bpy.ops.mesh.primitive_plane_add(
            size=float(terrain_size),
            enter_editmode=False,
            align='WORLD',
            location=(0, 0, 0),
            scale=(1, 1, 1),
        )
        water_obj = bpy.context.selected_objects[:][0]
        water_obj.name = obj_name

    try:
        water_obj.location = (0.0, 0.0, 0.0)
        water_obj.dimensions[0] = float(terrain_size)
        water_obj.dimensions[1] = float(terrain_size)
    except Exception:
        pass

    if water_obj.type == 'MESH':
        mat = _ensure_simple_water_material()
        water_obj.data.materials.clear()
        water_obj.data.materials.append(mat)
    return water_obj


def adjust_full_map_multires(obj, target_level):
    """Adjust multires on full-map terrain object (adds subdivision levels if needed)."""
    if obj is None or obj.type != 'MESH':
        return False

    multires = None
    for mod in obj.modifiers:
        if mod.type == 'MULTIRES':
            multires = mod
            break
    if multires is None:
        bpy.context.view_layer.objects.active = obj
        obj.select_set(True)
        multires = obj.modifiers.new(type='MULTIRES', name="tileres")

    target = max(0, int(target_level))
    current_total = int(getattr(multires, "total_levels", 0))

    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    for _ in range(max(0, target - current_total)):
        bpy.ops.object.multires_subdivide(modifier=multires.name, mode='LINEAR')

    final_total = int(getattr(multires, "total_levels", target))
    view_level = min(target, final_total)
    if hasattr(multires, "levels"):
        multires.levels = view_level
    if hasattr(multires, "sculpt_levels"):
        multires.sculpt_levels = view_level
    if hasattr(multires, "render_levels"):
        multires.render_levels = view_level
    obj["terrain_multires"] = view_level
    return True


def import_combined_terrain_full_map(
    hub_name,
    heightmap_path,
    colormap_path,
    terrain_size,
    lowest_elevation,
    highest_elevation,
    multires_level,
    world_name=None,
):
    """Import a single full-map terrain mesh using combined PNG maps."""
    if not os.path.isfile(str(heightmap_path)):
        return None
    if not os.path.isfile(str(colormap_path)):
        return None

    bpy.ops.object.select_all(action='DESELECT')
    bpy.ops.mesh.primitive_plane_add(
        size=float(terrain_size),
        enter_editmode=False,
        align='WORLD',
        location=(0, 0, float(lowest_elevation)),
        scale=(1, 1, 1),
    )
    obj = bpy.context.selected_objects[:][0]
    obj.name = world_name or f"terrain_full_{hub_name}"

    for area in bpy.context.screen.areas:
        if area.type != 'VIEW_3D':
            continue
        for space in area.spaces:
            if space.type == 'VIEW_3D':
                space.clip_end = 9999

    _create_full_map_geo_nodes(obj, heightmap_path, lowest_elevation, highest_elevation)
    _create_full_map_material(obj, colormap_path, f"{hub_name}_terrain_m")
    _add_multires_modifier(obj, multires_level)

    obj["terrain_mode"] = "full_map"
    obj["terrain_hub"] = str(hub_name)
    obj["terrainSize"] = float(terrain_size)
    obj["lowestElevation"] = float(lowest_elevation)
    obj["highestElevation"] = float(highest_elevation)
    obj["terrain_multires"] = int(multires_level)
    obj["terrain_heightmap_path"] = str(heightmap_path)
    obj["terrain_colormap_path"] = str(colormap_path)
    return obj


def _do_import_map_terrain_full_map(worldFile, filePath):
    ctx = _resolve_terrain_context(worldFile, filePath)
    hub_name = ctx["hub_name"]
    n_tiles = ctx["n_tiles"]
    tile_res = ctx["tile_res"]

    if n_tiles <= 0:
        log.warning("Could not determine terrain tile grid for %s", hub_name)
        return None

    _ensure_world_water_plane(hub_name, worldFile.terrainSize)

    multires_level = _get_scene_terrain_multires_level()
    buffer_paths = _collect_tile_buffer_paths_for_combine(
        ctx["terrain_tiles_dir"],
        ctx["terrain_tiles_rel"],
        n_tiles,
        tile_res,
    )
    if not buffer_paths:
        log.warning("No terrain buffers found for %s", hub_name)
        return None

    output_dir = str(ctx["w2w_dir"])
    terrain_w2ter.combine_w2ter_tiles(
        buffer_paths,
        output_dir,
        hub_name,
        res_override=tile_res,
        x_tiles_override=n_tiles,
        y_tiles_override=n_tiles,
    )

    heightmap_path = os.path.join(output_dir, f"{hub_name}.heightmap.png")
    colormap_path = os.path.join(output_dir, f"{hub_name}.overlay.png")
    if not os.path.isfile(heightmap_path):
        log.warning("Missing combined heightmap PNG: %s", heightmap_path)
        return None
    if not os.path.isfile(colormap_path):
        log.warning("Missing combined overlay PNG: %s", colormap_path)
        return None

    obj = import_combined_terrain_full_map(
        hub_name=hub_name,
        heightmap_path=heightmap_path,
        colormap_path=colormap_path,
        terrain_size=worldFile.terrainSize,
        lowest_elevation=worldFile.lowestElevation,
        highest_elevation=worldFile.highestElevation,
        multires_level=multires_level,
        world_name=getattr(worldFile, "worldName", None) or hub_name,
    )
    if obj:
        log.info("Imported full terrain map: %s", obj.name)
    return obj


def _do_import_map_terrain_tiles(worldFile, filePath):
    ctx = _resolve_terrain_context(worldFile, filePath)
    hub_name = ctx["hub_name"]
    n_tiles = ctx["n_tiles"]
    tile_res = ctx["tile_res"]

    if n_tiles <= 0:
        log.warning("Could not determine terrain tile grid for %s", hub_name)
        return

    _ensure_world_water_plane(hub_name, worldFile.terrainSize)

    multires_level = _get_scene_terrain_multires_level()

    # Find/extract tile buffers
    tile_heightmap_buffers = {}  # (x,y) -> raw .w2ter.1.buffer path
    tile_overlays = {}           # (x,y) -> overlay PNG path

    for y in range(n_tiles):
        for x in range(n_tiles):
            tile_name = f"tile_{y}_x_{x}_res{tile_res}"

            # Buffer 1 = heightmap (raw uint16 data)
            buf1_name = f"{tile_name}.w2ter.1.buffer"
            buf1_path = _resolve_tile_buffer(ctx["terrain_tiles_dir"], ctx["terrain_tiles_rel"], buf1_name)
            if buf1_path:
                tile_heightmap_buffers[(x, y)] = buf1_path

            # Buffer 2 = texturemap (overlay PNG for material)
            buf2_name = f"{tile_name}.w2ter.2.buffer"
            buf2_path = _resolve_tile_buffer(ctx["terrain_tiles_dir"], ctx["terrain_tiles_rel"], buf2_name)
            if buf2_path:
                info = terrain_w2ter.TileInfo(x=x, y=y, res=tile_res, buffer_index=2)
                overlay_path = buf2_path + ".overlay.png"
                # Always regenerate to avoid stale cached overlays from older orientation logic.
                try:
                    terrain_w2ter._tile_texture_pngs(buf2_path, info)
                except Exception:
                    pass
                if os.path.exists(overlay_path):
                    tile_overlays[(x, y)] = overlay_path

    if not tile_heightmap_buffers:
        log.warning("No terrain tile heightmaps found for %s", hub_name)
        return

    log.info("Importing %d terrain tiles for %s (%dx%d grid)", len(tile_heightmap_buffers), hub_name, n_tiles, n_tiles)

    do_import_terrain_tiles(
        tile_heightmap_buffers=tile_heightmap_buffers,
        tile_overlays=tile_overlays,
        x_tiles=n_tiles,
        y_tiles=n_tiles,
        tile_res=tile_res,
        terrain_size=worldFile.terrainSize,
        lowest_elevation=worldFile.lowestElevation,
        highest_elevation=worldFile.highestElevation,
        multires_level=multires_level,
        hub_name=hub_name,
    )


def do_import_map_terrain(worldFile, filePath):
    mode = _get_scene_terrain_import_mode()
    if mode == TERRAIN_IMPORT_TILES:
        _do_import_map_terrain_tiles(worldFile, filePath)
        return

    obj = _do_import_map_terrain_full_map(worldFile, filePath)
    if obj is None:
        log.info("Falling back to tile terrain import")
        _do_import_map_terrain_tiles(worldFile, filePath)


def _create_tile_mesh(name, heightmap_buffer_path, tile_res, mesh_res,
                      tile_size, elev_range, lowest_elevation):
    """Create a grid mesh with vertex Z from raw heightmap data.

    Args:
        name: mesh/object name
        heightmap_buffer_path: path to raw .w2ter.1.buffer (uint16 LE)
        tile_res: heightmap resolution (e.g. 256)
        mesh_res: mesh grid resolution (vertices per side, e.g. 33 for multires 5)
        tile_size: size of tile in world units
        elev_range: abs(lowest) + abs(highest) elevation
        lowest_elevation: world Z offset for the tile

    Returns:
        bpy.types.Mesh
    """
    import bmesh

    # Read raw heightmap
    heightmap = np.fromfile(heightmap_buffer_path, dtype="<u2")
    if heightmap.size != tile_res * tile_res:
        # Fallback: flat mesh
        heightmap = np.zeros((tile_res, tile_res), dtype=np.uint16)
    else:
        heightmap = heightmap.reshape((tile_res, tile_res))

    mesh = bpy.data.meshes.new(name)
    bm = bmesh.new()

    half = tile_size / 2.0
    step = tile_size / (mesh_res - 1) if mesh_res > 1 else tile_size

    # Create vertices - sample heightmap at each grid point
    for vy in range(mesh_res):
        for vx in range(mesh_res):
            # UV coords [0..1]
            u = vx / (mesh_res - 1) if mesh_res > 1 else 0.5
            v = vy / (mesh_res - 1) if mesh_res > 1 else 0.5

            # Sample heightmap at nearest pixel
            px = min(int(u * (tile_res - 1) + 0.5), tile_res - 1)
            py = min(int(v * (tile_res - 1) + 0.5), tile_res - 1)
            height_norm = heightmap[py, px] / 65535.0
            z = height_norm * elev_range

            x_pos = -half + vx * step
            y_pos = -half + vy * step
            bm.verts.new((x_pos, y_pos, z))

    # Create faces
    bm.verts.ensure_lookup_table()
    for vy in range(mesh_res - 1):
        for vx in range(mesh_res - 1):
            i = vy * mesh_res + vx
            v0 = bm.verts[i]
            v1 = bm.verts[i + 1]
            v2 = bm.verts[i + mesh_res + 1]
            v3 = bm.verts[i + mesh_res]
            bm.faces.new([v0, v1, v2, v3])

    # Create UV layer
    uv_layer = bm.loops.layers.uv.new("UVMap")
    for face in bm.faces:
        for loop in face.loops:
            # Derive UV directly from local XY so UVs are always valid.
            u = (loop.vert.co.x + half) / tile_size if tile_size else 0.5
            v = (loop.vert.co.y + half) / tile_size if tile_size else 0.5
            loop[uv_layer].uv = (max(0.0, min(1.0, u)), max(0.0, min(1.0, v)))

    bm.to_mesh(mesh)
    bm.free()
    if mesh.uv_layers:
        mesh.uv_layers.active = mesh.uv_layers[0]
    mesh.update()
    return mesh


def rebuild_tile_mesh(obj, target_level):
    """Rebuild a terrain tile mesh at a new resolution level.

    Reads tile metadata from custom properties and recreates the mesh
    from the raw heightmap buffer at the new resolution.

    Args:
        obj: Blender object with tile custom properties
        target_level: new multires level (2^level + 1 verts per side)

    Returns:
        True if successful, False otherwise
    """
    buffer_path = obj.get("tile_buffer_path")
    tile_res = obj.get("tile_res")
    tile_size = obj.get("tile_size")
    elev_range = obj.get("elev_range")
    lowest_elevation = obj.get("lowest_elevation")

    if not buffer_path or not os.path.isfile(buffer_path):
        return False
    if not all(v is not None for v in [tile_res, tile_size, elev_range, lowest_elevation]):
        return False

    mesh_res = (1 << target_level) + 1 if target_level > 0 else 2
    old_mesh = obj.data
    new_mesh = _create_tile_mesh(
        obj.name, buffer_path, int(tile_res), mesh_res,
        float(tile_size), float(elev_range), float(lowest_elevation),
    )
    obj.data = new_mesh
    obj["terrain_multires"] = target_level

    # Remove old mesh if no other users
    if old_mesh and old_mesh.users == 0:
        bpy.data.meshes.remove(old_mesh)

    return True


def _apply_tile_overlay_material(obj, overlay_path, mat_name):
    """Create material with overlay texture as diffuse Base Color."""
    mat = bpy.data.materials.new(name=mat_name)
    mat.use_nodes = True
    nt = mat.node_tree
    principled = nt.nodes.get("Principled BSDF")
    if principled is None:
        principled = nt.nodes.new("ShaderNodeBsdfPrincipled")
    tex = nt.nodes.new("ShaderNodeTexImage")
    uv = nt.nodes.new("ShaderNodeUVMap")
    uv.uv_map = "UVMap"
    uv.location = (tex.location[0] - 220, tex.location[1])
    nt.links.new(uv.outputs["UV"], tex.inputs["Vector"])
    insert_color(mat, principled, tex, None, str(overlay_path))
    _set_principled_terrain_values(principled)
    mat["witcher_terrain_material"] = True
    obj.data.materials.append(mat)


def _apply_tile_multires(obj, level):
    """Add multires modifier and subdivide to the given level."""
    if level <= 0:
        return
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    multires = obj.modifiers.new(type='MULTIRES', name="tileres")
    for _ in range(level):
        bpy.ops.object.multires_subdivide(modifier=multires.name, mode='LINEAR')


def _source_tile_y_to_world_row(tile_y, y_tiles):
    """Convert source tile Y index into Blender world row index.

    Keep this aligned with full-map import orientation. Empirically, using the
    source Y directly matches the full-map result.
    """
    return tile_y


def do_import_terrain_tiles(
    tile_heightmap_buffers,
    tile_overlays,
    x_tiles,
    y_tiles,
    tile_res,
    terrain_size,
    lowest_elevation,
    highest_elevation,
    multires_level,
    hub_name,
):
    """Import terrain tiles as individual Blender objects with baked heightmap geometry.

    Args:
        tile_heightmap_buffers: dict of (x,y) -> raw .w2ter.1.buffer path
        tile_overlays: dict of (x,y) -> overlay PNG path
        x_tiles, y_tiles: grid dimensions
        tile_res: heightmap pixel resolution per tile (e.g. 256)
        terrain_size: total terrain size in world units
        lowest_elevation, highest_elevation: elevation range
        multires_level: mesh resolution level (2^level + 1 verts per side)
        hub_name: name for the parent empty

    Returns:
        (parent empty, tile count)
    """
    tile_size = terrain_size / max(x_tiles, y_tiles)
    elev_range = abs(lowest_elevation) + abs(highest_elevation)
    # Mesh resolution: 2^level + 1 vertices per side (matching multires convention)
    mesh_res = (1 << multires_level) + 1 if multires_level > 0 else 2

    # Parent empty
    empty = bpy.data.objects.new(f"terrain_{hub_name}", None)
    empty.empty_display_type = 'PLAIN_AXES'
    empty.empty_display_size = tile_size / 2
    empty.location = (0, 0, 0)
    empty["terrainSize"] = terrain_size
    empty["tileRes"] = tile_res
    empty["lowestElevation"] = lowest_elevation
    empty["highestElevation"] = highest_elevation
    empty["x_tiles"] = x_tiles
    empty["y_tiles"] = y_tiles
    empty["multires_level"] = multires_level
    empty["tile_y_inverted"] = False
    empty["z_offset_applied_to_tiles"] = True
    bpy.context.collection.objects.link(empty)

    # Set viewport clip
    for a in bpy.context.screen.areas:
        if a.type == 'VIEW_3D':
            for s in a.spaces:
                if s.type == 'VIEW_3D':
                    s.clip_end = 9999

    count = 0
    for (x, y), buffer_path in sorted(
        tile_heightmap_buffers.items(),
        key=lambda item: (item[0][1], item[0][0]),
    ):
        tile_name = f"tile_{y}_x_{x}"
        world_y = _source_tile_y_to_world_row(y, y_tiles)

        # Create mesh with baked heightmap vertex positions
        mesh = _create_tile_mesh(
            tile_name, buffer_path, tile_res, mesh_res,
            tile_size, elev_range, lowest_elevation,
        )

        obj = bpy.data.objects.new(tile_name, mesh)
        loc_x = -terrain_size / 2 + tile_size / 2 + x * tile_size
        loc_y = -terrain_size / 2 + tile_size / 2 + world_y * tile_size
        obj.location = (loc_x, loc_y, lowest_elevation)
        obj["tile_x"] = x
        obj["tile_y"] = y
        obj["tile_world_y"] = world_y
        obj["terrain_multires"] = multires_level
        obj["tile_buffer_path"] = buffer_path
        obj["tile_res"] = tile_res
        obj["tile_size"] = tile_size
        obj["elev_range"] = elev_range
        obj["lowest_elevation"] = lowest_elevation
        bpy.context.collection.objects.link(obj)

        # Overlay texture as diffuse material
        overlay_path = tile_overlays.get((x, y))
        if overlay_path:
            _apply_tile_overlay_material(obj, overlay_path, f"{tile_name}_mat")

        obj.parent = empty
        obj.matrix_parent_inverse = empty.matrix_world.inverted()
        count += 1

    return empty, count


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
    bpy.types.Scene.witcher_w2w_nodes = bpy.props.CollectionProperty(type=MyListTreeNode)
    bpy.types.Scene.witcher_w2w_list_tree = bpy.props.CollectionProperty(type=MyListTreeItem)
    bpy.types.Scene.witcher_w2w_list_tree_index = IntProperty(update=seListIndexFunction)

    # SetupNodeData()
    # SetupListFromNodeData()


def unregister():
    if hasattr(bpy.types.Scene, "witcher_w2w_list_tree_index"):
        del bpy.types.Scene.witcher_w2w_list_tree_index
    if hasattr(bpy.types.Scene, "witcher_w2w_list_tree"):
        del bpy.types.Scene.witcher_w2w_list_tree
    if hasattr(bpy.types.Scene, "witcher_w2w_nodes"):
        del bpy.types.Scene.witcher_w2w_nodes
    for cls in reversed(classes):
        unregister_class(cls)


if __name__ == "__main__":
    register()


