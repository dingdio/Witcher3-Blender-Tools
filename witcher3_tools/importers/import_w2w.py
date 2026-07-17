import logging
import os
import hashlib
import json
import math
from dataclasses import dataclass, replace

import bpy

log = logging.getLogger(__name__)
import numpy as np
from ..importers.import_texarray import insert_color, get_texture_node, insert_heightmap_to_disp
from ..importers import terrain_w2ter
from ..CR2W.CR2W_file import WORLD, read_CR2W
from ..CR2W.common_blender import repo_file, bpy_image_load_safe, redkit_repo_context, win_safe_path
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
from .. import get_all_addon_prefs
from ..extension_paths import get_dev_override, get_redkit_working_root
from ..terrain_core import (
    terrain_tile_bounds as _terrain_tile_bounds_2d,
    terrain_tile_from_world_position as _terrain_tile_from_world_position,
)

W2W_NODES_PROP = "witcher_w2w_nodes"
W2W_LIST_PROP = "witcher_w2w_list_tree"
W2W_LIST_INDEX_PROP = "witcher_w2w_list_tree_index"
WORLD_WATER_OBJECT_PROP = "witcher_world_water_object"
WORLD_WATER_MATERIAL_PROP = "witcher_world_water_material"
WORLD_WATER_KEY_PROP = "witcher_world_water_key"

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


def _store_world_layer_metadata(target, world_path="", world_root_collection=None):
    if target is None or not hasattr(target, "__setitem__"):
        return
    if world_path:
        target["world_path"] = str(world_path)
    if world_root_collection is not None:
        target["world_root_collection"] = str(getattr(world_root_collection, "name", ""))


def _find_world_layer_collection(world_path, scene=None):
    scene = scene or getattr(bpy.context, "scene", None)
    root = getattr(scene, "collection", None)
    if root is None or not world_path:
        return None
    target = os.path.normcase(os.path.abspath(os.path.normpath(str(world_path))))
    for collection in root.children:
        if collection.get("group_type") != "LayerGroup":
            continue
        stored = str(collection.get("world_path", "") or "")
        if stored and os.path.normcase(os.path.abspath(os.path.normpath(stored))) == target:
            return collection
    return None


def AddCLayerGroup(groups, parent_collection, world_path=""):
    if world_path and not parent_collection:
        existing = _find_world_layer_collection(world_path)
        if existing is not None:
            return existing
    this_collection = bpy.data.collections.new(groups.name)
    this_collection['group_type'] = "LayerGroup"
    this_collection['witcher_visible_on_start'] = bool(getattr(groups, 'isVisibleOnStart', True))
    if world_path and not parent_collection:
        this_collection["world_path"] = str(world_path)
    if parent_collection:
        parent_collection.children.link(this_collection)
    if groups.ChildrenGroups:
        for subgroups in groups.ChildrenGroups:
            AddCLayerGroup(subgroups, this_collection, world_path)
    if groups.ChildrenInfos:
        for ChildInfo in groups.ChildrenInfos:
            child_collection = bpy.data.collections.new(os.path.basename(ChildInfo.depotFilePath))
            child_collection['w2layer_path'] = ChildInfo.depotFilePath
            child_collection['level_path'] = ChildInfo.depotFilePath
            child_collection['layerBuildTag'] = ChildInfo.layerBuildTag
            child_collection['group_type'] = "LayerInfo"
            child_collection["witcher_layer_import_state"] = "unloaded"
            child_collection["witcher_layer_import_count"] = 0
            child_collection["witcher_layer_import_errors"] = 0
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
    collection = AddCLayerGroup(worldFile.groups, False, filePath)
    if collection.name not in bpy.context.scene.collection.children:
        bpy.context.scene.collection.children.link(collection)
    layer_collection = bpy.context.view_layer.layer_collection.children.get(collection.name)
    if layer_collection is not None:
        bpy.context.view_layer.active_layer_collection = layer_collection

    with redkit_repo_context(filePath):
        do_import_map_terrain(worldFile, filePath, world_root_collection=collection)

    # Keep world import usable when the optional preview UI is unavailable.
    try:
        from ..ui import ui_environment
        ui_environment.sync_world_import(bpy.context, worldFile, filePath)
    except Exception:
        log.warning("Could not sync imported world environment for %s", filePath, exc_info=True)


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

_MATERIALIZED_W2TER_BUFFER_CACHE = {}
_TERRAIN_GRID_TOPOLOGY_CACHE = {}


def _path_is_under_root(path, root):
    if not path or not root:
        return False
    try:
        norm_path = os.path.normcase(os.path.normpath(os.path.abspath(str(path))))
        norm_root = os.path.normcase(os.path.normpath(os.path.abspath(str(root))))
        return os.path.commonpath([norm_path, norm_root]) == norm_root
    except Exception:
        return False


def _relpath_under_root(path, root):
    if not _path_is_under_root(path, root):
        return None
    try:
        return os.path.relpath(str(path), str(root))
    except Exception:
        return None


def _configured_redkit_roots():
    roots = []
    try:
        prefs = get_all_addon_prefs(bpy.context)
    except Exception:
        prefs = None
    if not prefs:
        return roots
    for attr in ("redkit_depot_path", "redkit_uncooked_path"):
        value = str(getattr(prefs, attr, "") or "").strip()
        if value:
            roots.append(os.path.normpath(value))
    return roots


def _configured_redkit_workspace_roots():
    roots = []
    try:
        prefs = get_all_addon_prefs(bpy.context)
    except Exception:
        prefs = None
    if not prefs:
        return roots

    for item in getattr(prefs, "redkit_projects", []) or []:
        value = str(getattr(item, "path", "") or "").strip()
        if not value:
            continue
        try:
            value = bpy.path.abspath(value)
        except Exception:
            pass
        project_root = os.path.normpath(value)
        workspace_root = os.path.join(project_root, "workspace")
        if os.path.isdir(win_safe_path(workspace_root)):
            roots.append(workspace_root)
        elif os.path.isdir(win_safe_path(project_root)):
            # The preference may already point at the workspace directory.
            roots.append(project_root)

    unique = []
    seen = set()
    for root in roots:
        key = os.path.normcase(os.path.normpath(root))
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


def _workspace_ancestor(path):
    try:
        current = Path(os.path.abspath(str(path)))
    except Exception:
        return ""
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if candidate.name.casefold() == "workspace":
            return str(candidate)
    return ""


def _workspace_cache_namespace(workspace_root):
    try:
        identity = os.path.normcase(os.path.normpath(
            os.path.abspath(str(workspace_root))))
    except Exception:
        identity = str(workspace_root or "")
    return hashlib.sha1(
        identity.encode("utf-8", errors="replace")).hexdigest()[:12]


def _first_containing_root(path, roots):
    for root in roots or []:
        if _path_is_under_root(path, root):
            return root
    return ""


def _materialize_w2ter_embedded_buffers(tile_path, working_tiles_dir):
    """Write embedded .w2ter buffers as sidecars in working storage."""
    if not tile_path or not working_tiles_dir:
        return []
    try:
        source_path = os.path.abspath(str(tile_path))
    except Exception:
        source_path = str(tile_path)
    if not os.path.isfile(win_safe_path(source_path)):
        return []

    try:
        source_stat = os.stat(win_safe_path(source_path))
        source_mtime = source_stat.st_mtime
        source_mtime_ns = source_stat.st_mtime_ns
        source_size = source_stat.st_size
    except Exception:
        source_mtime = 0.0
        source_mtime_ns = 0
        source_size = 0
    cache_key = (
        os.path.normcase(os.path.normpath(source_path)),
        os.path.normcase(os.path.normpath(str(working_tiles_dir))),
        source_mtime_ns,
        source_size,
    )
    cached = _MATERIALIZED_W2TER_BUFFER_CACHE.get(cache_key)
    if cached:
        return [p for p in cached if os.path.isfile(win_safe_path(p))]

    try:
        cr2w_file = read_CR2W(source_path)
    except Exception:
        log.debug("Failed to read project terrain tile buffers: %s", source_path, exc_info=True)
        return []

    buffers = list(getattr(cr2w_file, "BufferData", None) or [])
    buffer_infos = list(getattr(cr2w_file, "CR2WBuffer", None) or [])
    if not buffers:
        return []

    try:
        os.makedirs(win_safe_path(str(working_tiles_dir)), exist_ok=True)
    except Exception:
        log.warning("Could not create terrain working directory: %s", working_tiles_dir, exc_info=True)
        return []

    base_name = os.path.basename(source_path)
    outputs = []
    for idx, data in enumerate(buffers):
        if data is None:
            continue
        info = buffer_infos[idx] if idx < len(buffer_infos) else None
        buffer_index = int(getattr(info, "index", idx + 1) or (idx + 1))
        out_path = os.path.join(str(working_tiles_dir), f"{base_name}.{buffer_index}.buffer")
        safe_out = win_safe_path(out_path)
        expected_size = len(data)
        write_needed = True
        if os.path.isfile(safe_out):
            try:
                write_needed = (
                    os.path.getsize(safe_out) != expected_size
                    or os.path.getmtime(safe_out) < source_mtime
                )
                if not write_needed:
                    with open(safe_out, "rb") as handle:
                        write_needed = handle.read() != data
            except Exception:
                write_needed = True
        if write_needed:
            try:
                with open(safe_out, "wb") as handle:
                    handle.write(data)
            except Exception:
                log.warning("Could not materialize terrain buffer: %s", out_path, exc_info=True)
                continue
        outputs.append(out_path)

    _MATERIALIZED_W2TER_BUFFER_CACHE[cache_key] = outputs
    return outputs


def _embedded_tile_source_path(terrain_tiles_dir, terrain_tiles_rel, tile_name):
    disk_path = os.path.join(str(terrain_tiles_dir), tile_name)
    if os.path.isfile(win_safe_path(disk_path)):
        return disk_path
    if terrain_tiles_rel:
        try:
            rel_path = os.path.join(terrain_tiles_rel, tile_name)
            abs_path = repo_file(rel_path)
            if abs_path and os.path.isfile(win_safe_path(abs_path)):
                return abs_path
        except Exception:
            pass
    return ""


def _resolve_tile_buffer(terrain_tiles_dir, terrain_tiles_rel, buf_name, working_tiles_dir=None):
    """Resolve a tile buffer, with direct workspace sources taking precedence."""
    # Loose buffers beside the selected .w2w are the most explicit override.
    disk_path = os.path.join(str(terrain_tiles_dir), buf_name)
    if os.path.isfile(win_safe_path(disk_path)):
        return disk_path

    tile_name = terrain_w2ter.W2TER_BUFFER_RE.sub(".w2ter", buf_name)
    local_source_tile = os.path.join(str(terrain_tiles_dir), tile_name)
    if working_tiles_dir and os.path.isfile(win_safe_path(local_source_tile)):
        # A workspace container shadows every cache/depot copy of this tile.
        # If it cannot be decoded, fail visibly instead of silently importing
        # an older terrain buffer from another source.
        for path in _materialize_w2ter_embedded_buffers(
            local_source_tile, working_tiles_dir
        ):
            if os.path.basename(path).lower() == buf_name.lower():
                return path
        return None

    # Resolve an authoritative repo/depot sidecar before considering generated
    # working files left over from an earlier import.
    if terrain_tiles_rel:
        rel_path = os.path.join(terrain_tiles_rel, buf_name)
        abs_path = repo_file(rel_path)
        if abs_path and os.path.exists(win_safe_path(abs_path)):
            return abs_path

    # A repo/source .w2ter may also contain its buffers internally.
    if working_tiles_dir and terrain_tiles_rel:
        source_tile = _embedded_tile_source_path(
            terrain_tiles_dir, terrain_tiles_rel, tile_name)
        if source_tile:
            for path in _materialize_w2ter_embedded_buffers(
                source_tile, working_tiles_dir
            ):
                if os.path.basename(path).lower() == buf_name.lower():
                    return path

    # Generated working buffers are only a last-resort offline cache.
    if working_tiles_dir:
        working_path = os.path.join(str(working_tiles_dir), buf_name)
        if os.path.isfile(win_safe_path(working_path)):
            return working_path
    return None


def _discover_tile_count(terrain_tiles_dir):
    """Discover max tile count by scanning files on disk."""
    max_coord = -1
    if os.path.isdir(win_safe_path(str(terrain_tiles_dir))):
        try:
            for entry in os.scandir(win_safe_path(str(terrain_tiles_dir))):
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

# Defaults balance preview quality and source resolution.
TERRAIN_TILE_PREVIEW_LEVEL = 6
TERRAIN_TILE_MAX_LEVEL = 8
TERRAIN_TILE_STITCH_VERSION = 1
TERRAIN_HEIGHTMAP_CACHE_LIMIT = 64

@dataclass(frozen=True, order=True)
class TerrainTileKey:
    """Stable source-space terrain tile coordinate."""

    x: int
    y: int

    def __post_init__(self):
        object.__setattr__(self, "x", int(self.x))
        object.__setattr__(self, "y", int(self.y))


@dataclass(frozen=True)
class TerrainTileBounds:
    """Half-open world-space XY bounds for one terrain tile."""

    key: TerrainTileKey
    world_y: int
    min_x: float
    min_y: float
    max_x: float
    max_y: float
    min_z: float
    max_z: float

    @property
    def tile_size(self):
        return self.max_x - self.min_x

    @property
    def center_x(self):
        return (self.min_x + self.max_x) * 0.5

    @property
    def center_y(self):
        return (self.min_y + self.max_y) * 0.5

    def contains_xy(self, x, y):
        """Return whether a point belongs to this tile using half-open edges."""
        return self.min_x <= float(x) < self.max_x and self.min_y <= float(y) < self.max_y


@dataclass(frozen=True)
class TerrainWorldSpec:
    """Small, serializable description needed for coordinate-scoped imports."""

    hub_name: str
    world_name: str
    world_path: str
    world_key: str
    terrain_size: float
    lowest_elevation: float
    highest_elevation: float
    tile_res: int
    x_tiles: int
    y_tiles: int
    terrain_tiles_dir: str
    terrain_tiles_rel: str = ""
    working_tiles_dir: str = ""


@dataclass(frozen=True)
class TerrainTileSourceRequest:
    """Explicit declaration of the files allowed to be resolved for a tile."""

    key: TerrainTileKey
    include_overlay: bool = True
    include_stitch_neighbors: bool = False

@dataclass(frozen=True)
class TerrainTileSource:
    """Resolved source paths for one tile only."""

    request: TerrainTileSourceRequest
    tile_name: str
    heightmap_buffer: str = ""
    texture_buffer: str = ""
    overlay_path: str = ""
    positive_x_buffer_path: str = ""
    positive_y_buffer_path: str = ""
    positive_xy_buffer_path: str = ""
    positive_x_texture_buffer_path: str = ""
    positive_y_texture_buffer_path: str = ""
    positive_xy_texture_buffer_path: str = ""

    @property
    def key(self):
        return self.request.key

    @property
    def available(self):
        return bool(self.heightmap_buffer)


@dataclass(frozen=True)
class TerrainTileImportResult:
    """Structured result returned to UI and foliage callers."""

    spec: TerrainWorldSpec
    source: TerrainTileSource
    bounds: TerrainTileBounds
    obj: object = None
    root: object = None
    world_collection: object = None
    created: bool = False
    reused: bool = False
    error: str = ""

    @property
    def ok(self):
        return self.obj is not None and not self.error


@dataclass(frozen=True)
class WorldTileLoadResult:
    """Combined terrain and optional foliage result shared by every UI entry."""

    spec: TerrainWorldSpec
    terrain: TerrainTileImportResult
    foliage: object = None
    foliage_error: str = ""


def _resolve_terrain_context(worldFile, filePath, *, discover_tiles=True):
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
    output_dir = w2w_dir
    working_tiles_dir = None
    try:
        uncook_path = get_uncook_path(bpy.context)
        rel_dir = _relpath_under_root(str(w2w_dir), uncook_path) if uncook_path else None
        if rel_dir:
            terrain_tiles_rel = os.path.join(rel_dir, "terrain_tiles")
    except Exception:
        pass

    workspace_root = (
        _workspace_ancestor(str(w2w_dir))
        or _first_containing_root(
            str(w2w_dir), _configured_redkit_workspace_roots())
    )
    redkit_root = workspace_root or _first_containing_root(
        str(w2w_dir), _configured_redkit_roots())
    if redkit_root:
        rel_dir = _relpath_under_root(str(w2w_dir), redkit_root)
        if rel_dir:
            terrain_tiles_rel = os.path.join(rel_dir, "terrain_tiles")
            working_root = Path(get_redkit_working_root(create=True))
            if workspace_root:
                working_root = (
                    working_root
                    / "workspaces"
                    / _workspace_cache_namespace(workspace_root)
                )
            output_dir = working_root / rel_dir
            working_tiles_dir = output_dir / "terrain_tiles"

    # Compute tile grid from WORLD params
    tile_res = worldFile.tileRes or 256
    clipmap = worldFile.clipmapSize or worldFile.clipSize or 0
    n_tiles = 0
    if clipmap and tile_res and clipmap % tile_res == 0:
        n_tiles = clipmap // tile_res

    # Discover from disk if w2w didn't provide grid size
    if n_tiles <= 0 and discover_tiles:
        n_tiles = _discover_tile_count(terrain_tiles_dir)

    return {
        "hub_name": hub_name,
        "w2w_dir": w2w_dir,
        "output_dir": output_dir,
        "terrain_tiles_dir": terrain_tiles_dir,
        "terrain_tiles_rel": terrain_tiles_rel,
        "working_tiles_dir": working_tiles_dir,
        "tile_res": tile_res,
        "n_tiles": n_tiles,
    }


def _terrain_world_key(hub_name, world_path):
    """Return a stable identifier without requiring the path to exist."""
    path = str(world_path or "").strip()
    if path:
        try:
            path = os.path.abspath(path)
        except Exception:
            pass
        identity = os.path.normcase(os.path.normpath(path))
    else:
        identity = str(hub_name or "terrain").casefold()
    return hashlib.sha1(identity.encode("utf-8", errors="replace")).hexdigest()


def inspect_world_terrain(worldFile, filePath, *, discover_tiles=True):
    """Read world metadata required by tile tools."""
    ctx = _resolve_terrain_context(worldFile, filePath, discover_tiles=discover_tiles)
    world_path = str(filePath or "")
    hub_name = str(ctx["hub_name"] or "terrain")
    n_tiles = max(0, int(ctx["n_tiles"] or 0))
    return TerrainWorldSpec(
        hub_name=hub_name,
        world_name=str(getattr(worldFile, "worldName", None) or hub_name),
        world_path=world_path,
        world_key=_terrain_world_key(hub_name, world_path),
        terrain_size=float(getattr(worldFile, "terrainSize", 0.0) or 0.0),
        lowest_elevation=float(getattr(worldFile, "lowestElevation", 0.0) or 0.0),
        highest_elevation=float(getattr(worldFile, "highestElevation", 0.0) or 0.0),
        tile_res=max(1, int(ctx["tile_res"] or 256)),
        x_tiles=n_tiles,
        y_tiles=n_tiles,
        terrain_tiles_dir=str(ctx["terrain_tiles_dir"] or ""),
        terrain_tiles_rel=str(ctx["terrain_tiles_rel"] or ""),
        working_tiles_dir=str(ctx["working_tiles_dir"] or ""),
    )


def terrain_tile_bounds(spec, x, y):
    """Return half-open bounds for a tile without touching Blender or disk."""
    key = TerrainTileKey(x, y)
    bounds = _terrain_tile_bounds_2d(
        key.x,
        key.y,
        int(spec.x_tiles),
        int(spec.y_tiles),
        float(spec.terrain_size),
    )
    world_y = key.y
    return TerrainTileBounds(
        key=key,
        world_y=world_y,
        min_x=bounds.min_x,
        min_y=bounds.min_y,
        max_x=bounds.max_x,
        max_y=bounds.max_y,
        min_z=float(spec.lowest_elevation),
        max_z=float(spec.highest_elevation),
    )


def terrain_tile_from_world_position(spec, position):
    return _terrain_tile_from_world_position(
        position,
        int(spec.x_tiles),
        int(spec.y_tiles),
        float(spec.terrain_size),
    )


def terrain_tile_source_request(
    x,
    y,
    *,
    include_overlay=True,
    include_stitch_neighbors=False,
):
    """Construct a source request that can be inspected before any I/O."""
    return TerrainTileSourceRequest(
        key=TerrainTileKey(x, y),
        include_overlay=bool(include_overlay),
        include_stitch_neighbors=bool(include_stitch_neighbors),
    )


def _terrain_tile_heightmap_name(spec, x, y):
    return f"tile_{int(y)}_x_{int(x)}_res{int(spec.tile_res)}.w2ter.1.buffer"


def _resolve_terrain_tile_heightmap(spec, x, y):
    return str(_resolve_tile_buffer(
        spec.terrain_tiles_dir,
        spec.terrain_tiles_rel or None,
        _terrain_tile_heightmap_name(spec, x, y),
        working_tiles_dir=spec.working_tiles_dir or None,
    ) or "")


def _resolve_terrain_tile_texture(spec, x, y):
    texture_name = (
        f"tile_{int(y)}_x_{int(x)}_res{int(spec.tile_res)}.w2ter.2.buffer"
    )
    return str(_resolve_tile_buffer(
        spec.terrain_tiles_dir,
        spec.terrain_tiles_rel or None,
        texture_name,
        working_tiles_dir=spec.working_tiles_dir or None,
    ) or "")


def resolve_terrain_tile_source(spec, request):
    """Resolve only the buffers explicitly named by one tile request."""
    if not isinstance(request, TerrainTileSourceRequest):
        raise TypeError("request must be a TerrainTileSourceRequest")
    terrain_tile_bounds(spec, request.key.x, request.key.y)

    tile_name = f"tile_{request.key.y}_x_{request.key.x}_res{int(spec.tile_res)}"
    heightmap_path = _resolve_terrain_tile_heightmap(
        spec, request.key.x, request.key.y)

    texture_path = ""
    overlay_path = ""
    if request.include_overlay:
        texture_path = _resolve_terrain_tile_texture(
            spec, request.key.x, request.key.y)
        if texture_path:
            overlay_path = texture_path + ".overlay.png"

    positive_x_path = ""
    positive_y_path = ""
    positive_xy_path = ""
    positive_x_texture_path = ""
    positive_y_texture_path = ""
    positive_xy_texture_path = ""
    if request.include_stitch_neighbors:
        if request.key.x + 1 < int(spec.x_tiles):
            positive_x_path = _resolve_terrain_tile_heightmap(
                spec, request.key.x + 1, request.key.y)
            if request.include_overlay:
                positive_x_texture_path = _resolve_terrain_tile_texture(
                    spec, request.key.x + 1, request.key.y)
        if request.key.y + 1 < int(spec.y_tiles):
            positive_y_path = _resolve_terrain_tile_heightmap(
                spec, request.key.x, request.key.y + 1)
            if request.include_overlay:
                positive_y_texture_path = _resolve_terrain_tile_texture(
                    spec, request.key.x, request.key.y + 1)
        if (
            request.key.x + 1 < int(spec.x_tiles)
            and request.key.y + 1 < int(spec.y_tiles)
        ):
            positive_xy_path = _resolve_terrain_tile_heightmap(
                spec, request.key.x + 1, request.key.y + 1)
            if request.include_overlay:
                positive_xy_texture_path = _resolve_terrain_tile_texture(
                    spec, request.key.x + 1, request.key.y + 1)

    return TerrainTileSource(
        request=request,
        tile_name=tile_name,
        heightmap_buffer=str(heightmap_path or ""),
        texture_buffer=str(texture_path or ""),
        overlay_path=str(overlay_path or ""),
        positive_x_buffer_path=positive_x_path,
        positive_y_buffer_path=positive_y_path,
        positive_xy_buffer_path=positive_xy_path,
        positive_x_texture_buffer_path=positive_x_texture_path,
        positive_y_texture_buffer_path=positive_y_texture_path,
        positive_xy_texture_buffer_path=positive_xy_texture_path,
    )


def resolve_world_terrain_tile_source(
    spec,
    x,
    y,
    *,
    include_overlay=True,
    include_stitch_neighbors=False,
):
    """Convenience wrapper used by Blender UI operators."""
    return resolve_terrain_tile_source(
        spec,
        terrain_tile_source_request(
            x,
            y,
            include_overlay=include_overlay,
            include_stitch_neighbors=include_stitch_neighbors,
        ),
    )


def _get_scene_terrain_multires_level():
    try:
        return int(bpy.context.scene.witcher_file_browser.terrain_multires_level)
    except Exception:
        return TERRAIN_TILE_PREVIEW_LEVEL


def _clamp_tile_multires_level(level, tile_res):
    """Cap mesh density at the resolution supported by the source buffer."""
    try:
        level = max(0, int(level))
    except (TypeError, ValueError):
        level = TERRAIN_TILE_PREVIEW_LEVEL
    try:
        tile_res = max(1, int(tile_res))
    except (TypeError, ValueError):
        tile_res = 1
    if tile_res <= 1:
        return 0
    source_cap = int(math.floor(math.log2(tile_res)))
    return min(level, source_cap, TERRAIN_TILE_MAX_LEVEL)


def _get_scene_terrain_import_mode():
    try:
        mode = str(bpy.context.scene.witcher_file_browser.terrain_import_mode)
        if mode in {TERRAIN_IMPORT_FULL_MAP, TERRAIN_IMPORT_TILES}:
            return mode
    except Exception:
        pass
    return TERRAIN_IMPORT_FULL_MAP


def _collect_tile_buffer_paths_for_combine(terrain_tiles_dir, terrain_tiles_rel, n_tiles, tile_res, working_tiles_dir=None):
    """Collect tile buffer paths for combine workflow.

    Includes existing on-disk buffers and tries to resolve key buffers from bundle.
    """
    buffer_paths = []
    seen = set()

    if os.path.isdir(win_safe_path(str(terrain_tiles_dir))):
        try:
            for entry in os.scandir(win_safe_path(str(terrain_tiles_dir))):
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

    if working_tiles_dir and os.path.isdir(win_safe_path(str(terrain_tiles_dir))):
        try:
            for entry in os.scandir(win_safe_path(str(terrain_tiles_dir))):
                if not entry.is_file():
                    continue
                if not terrain_w2ter.is_w2ter_tile_name(entry.name):
                    continue
                if terrain_w2ter.is_w2ter_buffer_name(entry.name):
                    continue
                for path in _materialize_w2ter_embedded_buffers(entry.path, working_tiles_dir):
                    if not terrain_w2ter.is_w2ter_buffer_name(os.path.basename(path)):
                        continue
                    apath = os.path.abspath(path)
                    if apath not in seen:
                        buffer_paths.append(apath)
                        seen.add(apath)
        except Exception:
            log.debug("Failed to materialize terrain tiles from %s", terrain_tiles_dir, exc_info=True)

    # Ensure required height/texture buffers can be resolved from bundle paths too.
    for y in range(max(0, int(n_tiles))):
        for x in range(max(0, int(n_tiles))):
            tile_name = f"tile_{y}_x_{x}_res{tile_res}"
            for idx in (1, 2):
                buf_name = f"{tile_name}.w2ter.{idx}.buffer"
                buf_path = _resolve_tile_buffer(
                    terrain_tiles_dir,
                    terrain_tiles_rel,
                    buf_name,
                    working_tiles_dir=working_tiles_dir,
                )
                if not buf_path:
                    continue
                apath = os.path.abspath(buf_path)
                if apath not in seen:
                    buffer_paths.append(apath)
                    seen.add(apath)

    return buffer_paths


def _load_or_reload_terrain_image(path, colorspace):
    image = bpy_image_load_safe(str(path), check_existing=True)
    if image is None:
        return None
    try:
        stat = os.stat(win_safe_path(str(path)))
        stamp = f"{stat.st_mtime_ns}:{stat.st_size}"
    except OSError:
        stamp = ""
    try:
        previous = str(image.get("witcher_terrain_source_stamp", "") or "")
    except Exception:
        previous = ""
    if previous != stamp:
        try:
            image.reload()
        except Exception:
            log.debug("Could not reload refreshed terrain image: %s", path, exc_info=True)
        try:
            image["witcher_terrain_source_stamp"] = stamp
        except Exception:
            pass
    try:
        image.colorspace_settings.name = colorspace
    except Exception:
        pass
    return image


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
    image = _load_or_reload_terrain_image(heightmap_path, 'Non-Color')
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


def _get_terrain_material_controls(settings=None):
    if settings is None:
        try:
            settings = bpy.context.scene.witcher_file_browser
        except Exception:
            settings = None

    def value(name, default):
        return getattr(settings, name, default) if settings is not None else default

    def clamp(number, low, high):
        try:
            number = float(number)
        except (TypeError, ValueError):
            number = low
        return max(low, min(high, number))

    surface_mode = str(value("terrain_material_surface_mode", "SOURCE")).upper()
    if surface_mode not in {"SOURCE", "OVERRIDE"}:
        surface_mode = "SOURCE"
    slope_mode = str(value("terrain_material_slope_mode", "SOURCE")).upper()
    if slope_mode not in {"SOURCE", "HORIZONTAL", "VERTICAL"}:
        slope_mode = "SOURCE"
    debug_view = str(value("terrain_material_debug_view", "FINAL")).upper()
    if debug_view not in {
        "FINAL", "BASE_COLOR", "SLOPE", "ROUGHNESS", "SPECULAR",
        "MACRO_NORMAL", "FINAL_NORMAL",
    }:
        debug_view = "FINAL"
    return {
        "surface_mode": surface_mode,
        "roughness": clamp(value("terrain_material_roughness", 0.82), 0.0, 1.0),
        "specular": clamp(value("terrain_material_specular", 0.12), 0.0, 1.0),
        "normal_strength": clamp(
            value("terrain_material_normal_strength", 1.0), 0.0, 2.0),
        "tint_strength": clamp(
            value("terrain_material_tint_strength", 1.0), 0.0, 1.0),
        "fresnel_strength": clamp(
            value("terrain_material_fresnel_strength", 1.0), 0.0, 2.0),
        "slope_mode": slope_mode,
        "debug_view": debug_view,
    }


def _set_principled_terrain_values(principled, roughness=None, specular=None):
    if principled is None:
        return
    if roughness is None or specular is None:
        roughness, specular = _get_scene_terrain_material_values()
    if "Roughness" in principled.inputs:
        principled.inputs["Roughness"].default_value = float(roughness)
    if "Metallic" in principled.inputs:
        principled.inputs["Metallic"].default_value = 0.0
    if "IOR" in principled.inputs:
        from .terrain_detail import f0_to_ior
        principled.inputs["IOR"].default_value = float(f0_to_ior(specular))
        if "Specular IOR Level" in principled.inputs:
            principled.inputs["Specular IOR Level"].default_value = 0.5
    elif "Specular IOR Level" in principled.inputs:
        principled.inputs["Specular IOR Level"].default_value = float(specular)
    elif "Specular" in principled.inputs:
        principled.inputs["Specular"].default_value = float(specular)


def _apply_terrain_material_values(mat, roughness, specular):
    if mat is None or not mat.use_nodes or mat.node_tree is None:
        return False
    from . import terrain_detail_nodes
    return terrain_detail_nodes.configure_material_controls(
        mat,
        surface_mode="OVERRIDE",
        roughness=max(0.0, min(1.0, float(roughness))),
        specular=max(0.0, min(1.0, float(specular))),
    )


def _apply_terrain_material_controls(mat, controls):
    from . import terrain_detail_nodes
    return terrain_detail_nodes.configure_material_controls(mat, **controls)


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


def update_all_terrain_material_controls(settings=None):
    """Live-sync source/override and debug controls to loaded terrain materials."""
    controls = _get_terrain_material_controls(settings)
    updated = 0
    seen = set()

    for mat in bpy.data.materials:
        if not mat.get("witcher_terrain_material"):
            continue
        if _apply_terrain_material_controls(mat, controls):
            seen.add(mat.name_full)
            updated += 1

    for obj in bpy.data.objects:
        if not _is_terrain_mesh_object(obj) or not hasattr(obj.data, "materials"):
            continue
        for mat in obj.data.materials:
            if mat is None or mat.name_full in seen:
                continue
            if _apply_terrain_material_controls(mat, controls):
                seen.add(mat.name_full)
                updated += 1
    return updated


def _world_water_objects(scene=None):
    scene = scene or getattr(bpy.context, "scene", None)
    return [
        obj for obj in getattr(scene, "objects", ())
        if bool(obj.get(WORLD_WATER_OBJECT_PROP, False))
    ]


def _world_water_materials(scene=None):
    seen = set()
    for obj in _world_water_objects(scene):
        for material in getattr(getattr(obj, "data", None), "materials", ()):
            if (
                material is not None
                and bool(material.get(WORLD_WATER_MATERIAL_PROP, False))
                and material.as_pointer() not in seen
            ):
                seen.add(material.as_pointer())
                yield material


def update_world_water_controls(settings=None, *, scene=None):
    def value(name, default):
        try:
            return float(getattr(settings, name))
        except (AttributeError, TypeError, ValueError):
            return default

    wind = max(0.0, min(1.0, value("water_wind", 0.35)))
    direction = math.radians(value("water_wind_direction", 26.57))
    flow = max(0.0, value("water_flow_speed", 0.6))
    foam = max(0.0, value("water_foam_intensity", 0.1343882230))
    reflection = max(0.0, min(1.0, value("water_reflection", 1.0)))
    clarity = max(0.0, min(1.0, value("water_clarity", 0.58)))
    level = value("water_level", 0.0)

    # Same magnitude as the authored default drift (4 m/s east, 2 m/s north).
    speed = 0.002236
    updated = 0
    for mat in _world_water_materials(scene):
        if mat.node_tree is None:
            continue
        nodes = mat.node_tree.nodes
        for name, val in (
            ("W3 Water Wind", wind),
            ("W3 Water Flow", flow),
            ("W3 Water Foam", foam),
        ):
            node = nodes.get(name)
            if node is not None:
                node.outputs[0].default_value = val
        node = nodes.get("W3 Water Flow Direction")
        if node is not None:
            node.inputs["X"].default_value = speed * math.cos(direction)
            node.inputs["Y"].default_value = speed * math.sin(direction)
        node = nodes.get("W3 Water Depth Opacity Scale")
        if node is not None:
            node.inputs["To Min"].default_value = 1.0 - clarity
        surface = nodes.get("W3 Water Surface")
        if surface is not None and "Specular IOR Level" in surface.inputs:
            surface.inputs["Specular IOR Level"].default_value = reflection
        updated += 1

    for obj in _world_water_objects(scene):
        if obj.type == 'MESH':
            obj.location.z = level
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
    tex.image = _load_or_reload_terrain_image(colormap_path, 'sRGB')
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


def _load_water_asset_image(rel_path, texarray_slice=0):
    try:
        from ..CR2W.common_blender import repo_file
        from ..CR2W import texture_converters
        src = repo_file(rel_path)
        if not src or not os.path.isfile(str(src)):
            return None
        if str(src).lower().endswith(".texarray"):
            slices = texture_converters.convert_texarray_to_dds(str(src))
            if not slices or texarray_slice >= len(slices):
                return None
            dds = str(slices[texarray_slice])
        else:
            dds = str(texture_converters.convert_xbm_to_dds(str(src)))
        if not dds or not os.path.isfile(dds):
            return None
        image = bpy.data.images.load(dds, check_existing=True)
        image.colorspace_settings.name = 'Non-Color'
        return image
    except Exception:
        log.debug("Water asset unavailable: %s", rel_path, exc_info=True)
        return None


def _ensure_simple_water_material(
    heightmap_path="",
    lowest_elevation=0.0,
    highest_elevation=0.0,
    foam_image=None,
    medium_normal_image=None,
    owner_key="",
):
    """Build a three-band ocean preview with terrain-depth foam."""

    owner_key = str(owner_key or "")
    mat = next(
        (
            material for material in bpy.data.materials
            if bool(material.get(WORLD_WATER_MATERIAL_PROP, False))
            and str(material.get(WORLD_WATER_KEY_PROP, "") or "") == owner_key
        ),
        None,
    )
    if mat is None:
        mat = bpy.data.materials.new(name="water_simple_m")
    mat.use_nodes = True

    nt = mat.node_tree
    nodes = nt.nodes
    links = nt.links
    nodes.clear()

    out = nodes.new("ShaderNodeOutputMaterial")
    out.name = "W3 Water Output"
    out.location = (820, 80)

    # Prologue-noon defaults; .env curves update these named controls in place.
    tint = nodes.new("ShaderNodeRGB")
    tint.name = "W3 Water Tint"
    tint.label = "Water Color"
    tint.location = (-760, 420)
    tint.outputs[0].default_value = (
        31.8886987873 / 255.0,
        35.8815087077 / 255.0,
        18.9390292482 / 255.0,
        1.0,
    )

    fresnel_gain = nodes.new("ShaderNodeValue")
    fresnel_gain.name = "W3 Water Fresnel Gain"
    fresnel_gain.label = "Water Fresnel Gain"
    fresnel_gain.location = (-260, 280)
    fresnel_gain.outputs[0].default_value = 0.0735272020

    ambient_scale = nodes.new("ShaderNodeValue")
    ambient_scale.name = "W3 Water Ambient Scale"
    ambient_scale.location = (-760, 280)
    ambient_scale.outputs[0].default_value = 0.2953799963

    diffuse_scale = nodes.new("ShaderNodeValue")
    diffuse_scale.name = "W3 Water Diffuse Scale"
    diffuse_scale.location = (-760, 220)
    diffuse_scale.outputs[0].default_value = 1.0625499487

    flow = nodes.new("ShaderNodeValue")
    flow.name = "W3 Water Flow"
    flow.location = (-760, 160)
    # Default flow used when the environment curve is empty.
    flow.outputs[0].default_value = 0.6

    foam = nodes.new("ShaderNodeValue")
    foam.name = "W3 Water Foam"
    foam.location = (-760, 100)
    # Default foam intensity after the environment offset and clamp.
    foam.outputs[0].default_value = 0.1343882230

    # Wind scale drives both wave amplitude and foam.
    wind = nodes.new("ShaderNodeValue")
    wind.name = "W3 Water Wind"
    wind.label = "Wind Scale"
    wind.location = (-760, 40)
    wind.outputs[0].default_value = 0.35
    wind_wave_gain = nodes.new("ShaderNodeMath")
    wind_wave_gain.name = "W3 Water Wind Wave Gain"
    wind_wave_gain.operation = "MULTIPLY_ADD"
    wind_wave_gain.location = (-560, 40)
    wind_wave_gain.inputs[1].default_value = 1.6
    wind_wave_gain.inputs[2].default_value = 0.4
    links.new(wind.outputs[0], wind_wave_gain.inputs[0])

    # Eevee refraction either flickers or turns opaque; blended Principled stays
    # transparent and retains specular water.
    surface_color = nodes.new("ShaderNodeMixRGB")
    surface_color.name = "W3 Water Surface Color"
    surface_color.location = (-260, 420)
    surface_color.inputs[0].default_value = 0.15
    surface_color.inputs[1].default_value = (0.17, 0.27, 0.39, 1.0)
    links.new(tint.outputs[0], surface_color.inputs[2])
    surface_lighting = nodes.new("ShaderNodeMixRGB")
    surface_lighting.name = "W3 Water Surface Lighting"
    surface_lighting.blend_type = "MULTIPLY"
    surface_lighting.inputs[0].default_value = 1.0
    surface_lighting.location = (0, 420)
    links.new(surface_color.outputs[0], surface_lighting.inputs[1])
    links.new(diffuse_scale.outputs[0], surface_lighting.inputs[2])

    surface = nodes.new("ShaderNodeBsdfPrincipled")
    surface.name = "W3 Water Surface"
    surface.location = (620, 180)
    surface.inputs["Roughness"].default_value = 0.12
    if "IOR" in surface.inputs:
        surface.inputs["IOR"].default_value = 1.333
    if "Specular IOR Level" in surface.inputs:
        surface.inputs["Specular IOR Level"].default_value = 1.0
    elif "Specular" in surface.inputs:
        surface.inputs["Specular"].default_value = 1.0
    # Facing controls grazing opacity; terrain depth controls shallow foam.
    extinction = nodes.new("ShaderNodeValue")
    extinction.name = "W3 Water Extinction Preview"
    extinction.label = "Facing/shallow opacity"
    extinction.location = (300, 500)
    extinction.outputs[0].default_value = 0.10
    facing = nodes.new("ShaderNodeLayerWeight")
    facing.name = "W3 Water Facing"
    facing.location = (40, 180)
    grazing_opacity = nodes.new("ShaderNodeMath")
    grazing_opacity.name = "W3 Water Grazing Opacity"
    grazing_opacity.operation = "ADD"
    grazing_opacity.location = (300, 360)
    grazing_opacity.inputs[1].default_value = 0.78
    links.new(fresnel_gain.outputs[0], grazing_opacity.inputs[0])
    opacity = nodes.new("ShaderNodeMapRange")
    opacity.name = "W3 Water Opacity"
    opacity.clamp = True
    opacity.location = (420, 260)
    opacity.inputs["From Min"].default_value = 0.0
    opacity.inputs["From Max"].default_value = 1.0
    links.new(facing.outputs["Facing"], opacity.inputs["Value"])
    links.new(grazing_opacity.outputs[0], opacity.inputs["To Min"])
    links.new(extinction.outputs[0], opacity.inputs["To Max"])
    links.new(surface.outputs["BSDF"], out.inputs["Surface"])

    # Use large, medium, and small world-space wave bands on the 2 km plane.
    texcoord = nodes.new("ShaderNodeTexCoord")
    texcoord.name = "W3 Water Coordinates"
    texcoord.location = (-1260, -260)
    water_time = nodes.new("ShaderNodeValue")
    water_time.name = "W3 Water Time"
    water_time.location = (-1260, -420)
    scene = bpy.context.scene
    driver = water_time.outputs[0].driver_add("default_value").driver
    driver.expression = "frame * fps_base / fps"
    for name, data_path in (("fps", "render.fps"), ("fps_base", "render.fps_base")):
        variable = driver.variables.new()
        variable.name = name
        variable.type = "SINGLE_PROP"
        variable.targets[0].id_type = "SCENE"
        variable.targets[0].id = scene
        variable.targets[0].data_path = data_path

    flow_direction = nodes.new("ShaderNodeCombineXYZ")
    flow_direction.name = "W3 Water Flow Direction"
    flow_direction.location = (-1040, -420)
    # The 2 km coordinates yield 4 m/s east and 2 m/s north before flow scaling.
    flow_direction.inputs["X"].default_value = 0.002
    flow_direction.inputs["Y"].default_value = 0.001
    time_offset = nodes.new("ShaderNodeVectorMath")
    time_offset.name = "W3 Water Time Offset"
    time_offset.operation = "SCALE"
    time_offset.location = (-820, -420)
    links.new(flow_direction.outputs[0], time_offset.inputs[0])
    links.new(water_time.outputs[0], time_offset.inputs["Scale"])
    flow_offset = nodes.new("ShaderNodeVectorMath")
    flow_offset.name = "W3 Water Flow Offset"
    flow_offset.operation = "SCALE"
    flow_offset.location = (-600, -420)
    links.new(time_offset.outputs[0], flow_offset.inputs[0])
    links.new(flow.outputs[0], flow_offset.inputs["Scale"])
    animated_coordinates = nodes.new("ShaderNodeVectorMath")
    animated_coordinates.name = "W3 Water Animated Coordinates"
    animated_coordinates.operation = "ADD"
    animated_coordinates.location = (-820, -260)
    links.new(texcoord.outputs["Generated"], animated_coordinates.inputs[0])
    links.new(flow_offset.outputs[0], animated_coordinates.inputs[1])
    camera_data = nodes.new("ShaderNodeCameraData")
    camera_data.name = "W3 Water Camera Distance"
    camera_data.location = (-1040, -640)
    def band_fade_gain(label, strength, fade_distance, index):
        fade = nodes.new("ShaderNodeMapRange")
        fade.name = f"W3 Water {label} Distance Fade"
        fade.clamp = True
        fade.location = (-520 + index * 220, -430 - index * 130)
        fade.inputs["From Min"].default_value = 0.0
        fade.inputs["From Max"].default_value = fade_distance
        fade.inputs["To Min"].default_value = strength
        fade.inputs["To Max"].default_value = 0.0
        links.new(camera_data.outputs["View Distance"], fade.inputs["Value"])
        band_gain = nodes.new("ShaderNodeMath")
        band_gain.name = f"W3 Water {label} Wind Strength"
        band_gain.operation = "MULTIPLY"
        band_gain.location = (-520 + index * 220, -360 - index * 130)
        links.new(fade.outputs["Result"], band_gain.inputs[0])
        links.new(wind_wave_gain.outputs[0], band_gain.inputs[1])
        return band_gain

    normal_out = None
    small_waves = None

    # wave_med_n must anchor the bump chain because Normal Map has no base input.
    if medium_normal_image is not None:
        medium_gain = band_fade_gain("Medium", 0.6, 120.0, 1)
        med_uv = nodes.new("ShaderNodeVectorMath")
        med_uv.name = "W3 Water Medium UV"
        med_uv.operation = "SCALE"
        med_uv.inputs["Scale"].default_value = 57.8
        med_uv.location = (-520, -520)
        links.new(animated_coordinates.outputs[0], med_uv.inputs[0])
        med_tex = nodes.new("ShaderNodeTexImage")
        med_tex.name = "W3 Water Medium Wave Texture"
        med_tex.image = medium_normal_image
        med_tex.extension = "REPEAT"
        med_tex.location = (-300, -520)
        links.new(med_uv.outputs[0], med_tex.inputs["Vector"])
        med_nm = nodes.new("ShaderNodeNormalMap")
        med_nm.name = "W3 Water Medium Normal Map"
        med_nm.location = (-80, -520)
        links.new(med_tex.outputs["Color"], med_nm.inputs["Color"])
        links.new(medium_gain.outputs[0], med_nm.inputs["Strength"])
        normal_out = med_nm.outputs["Normal"]
    else:
        medium_gain = band_fade_gain("Medium", 0.055, 120.0, 1)
        noise = nodes.new("ShaderNodeTexNoise")
        noise.name = "W3 Water Medium Waves"
        noise.noise_dimensions = "4D"
        noise.location = (-580, -390)
        noise.inputs["Scale"].default_value = 57.8
        noise.inputs["Detail"].default_value = 0.0
        noise.inputs["Roughness"].default_value = 0.55
        noise.inputs["W"].default_value = 11.7
        links.new(animated_coordinates.outputs[0], noise.inputs["Vector"])
        bump = nodes.new("ShaderNodeBump")
        bump.name = "W3 Water Medium Normal"
        bump.location = (-300, -390)
        bump.inputs["Distance"].default_value = 0.10
        links.new(medium_gain.outputs[0], bump.inputs["Strength"])
        links.new(noise.outputs["Fac"], bump.inputs["Height"])
        normal_out = bump.outputs["Normal"]

    for index, (label, scale, strength, distance, fade_distance, w_offset) in enumerate((
        ("Large", 12.4, 0.11, 0.22, 2000.0, 0.0),
        ("Small", 250.0, 0.018, 0.035, 50.0, 23.1),
    )):
        noise = nodes.new("ShaderNodeTexNoise")
        noise.name = f"W3 Water {label} Waves"
        noise.noise_dimensions = "4D"
        noise.location = (-800 + index * 440, -260 - index * 260)
        noise.inputs["Scale"].default_value = scale
        noise.inputs["Detail"].default_value = 0.0
        noise.inputs["Roughness"].default_value = 0.55
        noise.inputs["W"].default_value = w_offset
        links.new(animated_coordinates.outputs[0], noise.inputs["Vector"])
        if label == "Small":
            small_waves = noise
        band_gain = band_fade_gain(label, strength, fade_distance, index * 2)
        bump = nodes.new("ShaderNodeBump")
        bump.name = f"W3 Water {label} Normal"
        bump.location = (-520 + index * 440, -260 - index * 260)
        bump.inputs["Distance"].default_value = distance
        links.new(band_gain.outputs[0], bump.inputs["Strength"])
        links.new(noise.outputs["Fac"], bump.inputs["Height"])
        links.new(normal_out, bump.inputs["Normal"])
        normal_out = bump.outputs["Normal"]

    if normal_out is not None:
        links.new(normal_out, surface.inputs["Normal"])

    # Both import modes provide the same heightmap for depth and foam.
    depth = nodes.new("ShaderNodeMapRange")
    depth.name = "W3 Water Depth Mask"
    depth.clamp = True
    depth.location = (80, -540)
    # Hide dry land with a soft +/-0.75m band, independent of distance fade.
    shore = nodes.new("ShaderNodeMapRange")
    shore.name = "W3 Water Shore Mask"
    shore.clamp = True
    shore.location = (80, -420)
    elevation_range = float(highest_elevation) - float(lowest_elevation)
    valid_heightmap = bool(heightmap_path) and os.path.isfile(str(heightmap_path))
    if valid_heightmap and elevation_range > 1e-6:
        terrain_height = nodes.new("ShaderNodeTexImage")
        terrain_height.name = "W3 Water Terrain Height"
        terrain_height.location = (-420, -620)
        terrain_height.image = _load_or_reload_terrain_image(heightmap_path, 'Non-Color')
        terrain_height.interpolation = "Linear"
        terrain_height.extension = "CLIP"
        links.new(texcoord.outputs["Generated"], terrain_height.inputs["Vector"])
        links.new(terrain_height.outputs["Color"], depth.inputs["Value"])
        water_height = max(
            0.0,
            min(1.0, -float(lowest_elevation) / elevation_range),
        )
        depth.inputs["From Min"].default_value = water_height
        depth.inputs["From Max"].default_value = water_height - (2.5 / elevation_range)
        depth.inputs["To Min"].default_value = 0.0
        depth.inputs["To Max"].default_value = 1.0
        shore_band = 0.75 / elevation_range
        links.new(terrain_height.outputs["Color"], shore.inputs["Value"])
        shore.inputs["From Min"].default_value = water_height + shore_band
        shore.inputs["From Max"].default_value = water_height - shore_band
        shore.inputs["To Min"].default_value = 0.0
        shore.inputs["To Max"].default_value = 1.0
    else:
        depth.inputs["From Min"].default_value = 0.0
        depth.inputs["From Max"].default_value = 1.0
        depth.inputs["To Min"].default_value = 1.0
        depth.inputs["To Max"].default_value = 1.0
        shore.inputs["To Min"].default_value = 1.0
        shore.inputs["To Max"].default_value = 1.0

    # Foam only hugs the first ~70cm of depth; broad banks stay foam-free.
    shallow = nodes.new("ShaderNodeValToRGB")
    shallow.name = "W3 Water Shallow Mask"
    shallow.location = (280, -520)
    ramp = shallow.color_ramp
    ramp.elements[0].position = 0.0
    ramp.elements[0].color = (0.0, 0.0, 0.0, 1.0)
    ramp.elements[1].position = 0.28
    ramp.elements[1].color = (0.0, 0.0, 0.0, 1.0)
    for position in (0.03, 0.12):
        element = ramp.elements.new(position)
        element.color = (1.0, 1.0, 1.0, 1.0)
    links.new(depth.outputs["Result"], shallow.inputs["Fac"])

    depth_scale = nodes.new("ShaderNodeMapRange")
    depth_scale.name = "W3 Water Depth Opacity Scale"
    depth_scale.clamp = True
    depth_scale.location = (300, -360)
    depth_scale.inputs["From Min"].default_value = 0.0
    depth_scale.inputs["From Max"].default_value = 1.0
    depth_scale.inputs["To Min"].default_value = 0.42
    depth_scale.inputs["To Max"].default_value = 1.0
    links.new(depth.outputs["Result"], depth_scale.inputs["Value"])
    depth_distance = nodes.new("ShaderNodeMapRange")
    depth_distance.name = "W3 Water Depth Distance Fade"
    depth_distance.clamp = True
    depth_distance.location = (300, -440)
    depth_distance.inputs["From Min"].default_value = 0.0
    depth_distance.inputs["From Max"].default_value = 200.0
    depth_distance.inputs["To Min"].default_value = 0.0
    depth_distance.inputs["To Max"].default_value = 1.0
    links.new(camera_data.outputs["View Distance"], depth_distance.inputs["Value"])
    depth_mix = nodes.new("ShaderNodeMixRGB")
    depth_mix.name = "W3 Water Depth Opacity Mix"
    depth_mix.location = (500, -380)
    depth_mix.inputs[2].default_value = (1.0, 1.0, 1.0, 1.0)
    links.new(depth_distance.outputs["Result"], depth_mix.inputs[0])
    links.new(depth_scale.outputs["Result"], depth_mix.inputs[1])
    depth_alpha = nodes.new("ShaderNodeMath")
    depth_alpha.name = "W3 Water Depth Opacity"
    depth_alpha.operation = "MULTIPLY"
    depth_alpha.location = (680, -300)
    links.new(opacity.outputs["Result"], depth_alpha.inputs[0])
    links.new(depth_mix.outputs[0], depth_alpha.inputs[1])

    foam_gain = nodes.new("ShaderNodeMath")
    foam_gain.name = "W3 Water Foam Gain"
    foam_gain.operation = "ADD"
    foam_gain.use_clamp = True
    foam_gain.inputs[1].default_value = 0.5
    foam_gain.location = (80, -760)
    links.new(foam.outputs[0], foam_gain.inputs[0])
    foam_lighting = nodes.new("ShaderNodeMath")
    foam_lighting.name = "W3 Water Foam Lighting"
    foam_lighting.operation = "MULTIPLY"
    foam_lighting.location = (280, -760)
    links.new(foam_gain.outputs[0], foam_lighting.inputs[0])
    links.new(diffuse_scale.outputs[0], foam_lighting.inputs[1])

    foam_pattern = nodes.new("ShaderNodeMapRange")
    foam_pattern.name = "W3 Water Foam Pattern"
    foam_pattern.clamp = True
    foam_pattern.location = (80, -900)
    foam_pattern.inputs["From Min"].default_value = 0.52
    foam_pattern.inputs["From Max"].default_value = 0.72
    if small_waves is not None:
        links.new(small_waves.outputs["Fac"], foam_pattern.inputs["Value"])
    foam_mask = nodes.new("ShaderNodeMath")
    foam_mask.name = "W3 Water Shore Foam"
    foam_mask.operation = "MULTIPLY"
    foam_mask.location = (480, -660)
    links.new(shallow.outputs["Color"], foam_mask.inputs[0])
    links.new(foam_pattern.outputs["Result"], foam_mask.inputs[1])
    foam_base = nodes.new("ShaderNodeMath")
    foam_base.name = "W3 Water Foam Base"
    foam_base.operation = "MULTIPLY"
    foam_base.location = (680, -700)
    links.new(foam_mask.outputs[0], foam_base.inputs[0])
    links.new(foam_lighting.outputs[0], foam_base.inputs[1])
    foam_strength = nodes.new("ShaderNodeMath")
    foam_strength.name = "W3 Water Foam Strength"
    foam_strength.operation = "MULTIPLY"
    foam_strength.location = (680, -620)
    # Broad procedural foam needs a restrained gain.
    foam_strength.inputs[1].default_value = 1.0
    links.new(foam_base.outputs[0], foam_strength.inputs[0])

    # Multiply two drifting control samples for patches and gate by wind.
    foam_combined = nodes.new("ShaderNodeMath")
    foam_combined.name = "W3 Water Foam Combined"
    foam_combined.operation = "ADD"
    foam_combined.use_clamp = True
    foam_combined.location = (840, -560)
    foam_combined.inputs[1].default_value = 0.0
    links.new(foam_strength.outputs[0], foam_combined.inputs[0])
    if foam_image is not None:
        slow_drift = nodes.new("ShaderNodeVectorMath")
        slow_drift.name = "W3 Water Foam Slow Drift"
        slow_drift.operation = "SCALE"
        slow_drift.inputs["Scale"].default_value = 0.4
        slow_drift.location = (-420, -1040)
        links.new(flow_offset.outputs[0], slow_drift.inputs[0])
        slow_coords = nodes.new("ShaderNodeVectorMath")
        slow_coords.name = "W3 Water Foam Slow Coordinates"
        slow_coords.operation = "ADD"
        slow_coords.location = (-300, -1040)
        links.new(texcoord.outputs["Generated"], slow_coords.inputs[0])
        links.new(slow_drift.outputs[0], slow_coords.inputs[1])
        uv_large = nodes.new("ShaderNodeVectorMath")
        uv_large.name = "W3 Water Foam UV Large"
        uv_large.operation = "SCALE"
        uv_large.inputs["Scale"].default_value = 25.0
        uv_large.location = (-180, -1040)
        links.new(slow_coords.outputs[0], uv_large.inputs[0])
        uv_small = nodes.new("ShaderNodeVectorMath")
        uv_small.name = "W3 Water Foam UV Small"
        uv_small.operation = "SCALE"
        uv_small.inputs["Scale"].default_value = 120.0
        uv_small.location = (-180, -1300)
        links.new(animated_coordinates.outputs[0], uv_small.inputs[0])
        patch_large = nodes.new("ShaderNodeTexImage")
        patch_large.name = "W3 Water Foam Patch Large"
        patch_large.image = foam_image
        patch_large.extension = "REPEAT"
        patch_large.location = (-20, -1040)
        links.new(uv_large.outputs[0], patch_large.inputs["Vector"])
        patch_small = nodes.new("ShaderNodeTexImage")
        patch_small.name = "W3 Water Foam Patch Small"
        patch_small.image = foam_image
        patch_small.extension = "REPEAT"
        patch_small.location = (-20, -1300)
        links.new(uv_small.outputs[0], patch_small.inputs["Vector"])
        patch_product = nodes.new("ShaderNodeMath")
        patch_product.name = "W3 Water Foam Patch Product"
        patch_product.operation = "MULTIPLY"
        patch_product.location = (260, -1100)
        links.new(patch_large.outputs["Color"], patch_product.inputs[0])
        links.new(patch_small.outputs["Color"], patch_product.inputs[1])
        patch_shape = nodes.new("ShaderNodeMapRange")
        patch_shape.name = "W3 Water Foam Patch Shape"
        patch_shape.clamp = True
        patch_shape.location = (440, -1100)
        patch_shape.inputs["From Min"].default_value = 0.20
        patch_shape.inputs["From Max"].default_value = 0.55
        patch_shape.inputs["To Max"].default_value = 1.6
        links.new(patch_product.outputs[0], patch_shape.inputs["Value"])
        foam_wind = nodes.new("ShaderNodeMath")
        foam_wind.name = "W3 Water Foam Wind"
        foam_wind.operation = "MAXIMUM"
        foam_wind.inputs[1].default_value = 0.05
        foam_wind.location = (440, -960)
        links.new(wind.outputs[0], foam_wind.inputs[0])
        patch_gain = nodes.new("ShaderNodeMath")
        patch_gain.name = "W3 Water Foam Patch Gain"
        patch_gain.operation = "MULTIPLY"
        patch_gain.location = (620, -1040)
        links.new(patch_shape.outputs["Result"], patch_gain.inputs[0])
        links.new(foam_wind.outputs[0], patch_gain.inputs[1])
        patches = nodes.new("ShaderNodeMath")
        patches.name = "W3 Water Foam Patches"
        patches.operation = "MULTIPLY"
        patches.location = (700, -980)
        links.new(patch_gain.outputs[0], patches.inputs[0])
        links.new(foam_lighting.outputs[0], patches.inputs[1])
        links.new(patches.outputs[0], foam_combined.inputs[1])

    depth_invert = nodes.new("ShaderNodeMath")
    depth_invert.name = "W3 Water Depth Invert"
    depth_invert.operation = "SUBTRACT"
    depth_invert.inputs[0].default_value = 1.0
    depth_invert.location = (40, 320)
    links.new(depth.outputs["Result"], depth_invert.inputs[1])
    shallow_tint = nodes.new("ShaderNodeMixRGB")
    shallow_tint.name = "W3 Water Shallow Tint"
    shallow_tint.location = (200, 300)
    shallow_tint.inputs[2].default_value = (0.27, 0.45, 0.38, 1.0)
    links.new(depth_invert.outputs[0], shallow_tint.inputs[0])
    links.new(surface_lighting.outputs[0], shallow_tint.inputs[1])

    foam_color = nodes.new("ShaderNodeMixRGB")
    foam_color.name = "W3 Water Foam Color"
    foam_color.location = (420, 420)
    foam_color.inputs[2].default_value = (0.85, 0.87, 0.88, 1.0)
    links.new(foam_combined.outputs[0], foam_color.inputs[0])
    links.new(shallow_tint.outputs[0], foam_color.inputs[1])
    links.new(foam_color.outputs[0], surface.inputs["Base Color"])

    foam_alpha = nodes.new("ShaderNodeMath")
    foam_alpha.name = "W3 Water Foam Opacity"
    foam_alpha.operation = "MULTIPLY"
    foam_alpha.inputs[1].default_value = 0.30
    foam_alpha.location = (680, -460)
    links.new(foam_combined.outputs[0], foam_alpha.inputs[0])
    final_alpha = nodes.new("ShaderNodeMath")
    final_alpha.name = "W3 Water Final Opacity"
    final_alpha.operation = "ADD"
    final_alpha.use_clamp = True
    final_alpha.location = (780, -220)
    links.new(depth_alpha.outputs[0], final_alpha.inputs[0])
    links.new(foam_alpha.outputs[0], final_alpha.inputs[1])
    shore_cut = nodes.new("ShaderNodeMath")
    shore_cut.name = "W3 Water Shore Cut"
    shore_cut.operation = "MULTIPLY"
    shore_cut.location = (900, -220)
    links.new(final_alpha.outputs[0], shore_cut.inputs[0])
    links.new(shore.outputs["Result"], shore_cut.inputs[1])
    links.new(shore_cut.outputs[0], surface.inputs["Alpha"])

    for attr, value in (
        ("surface_render_method", "BLENDED"),
        ("use_raytrace_refraction", False),
        ("use_screen_refraction", False),
        ("use_transparent_shadow", False),
        ("use_transparency_overlap", False),
    ):
        if hasattr(mat, attr):
            try:
                setattr(mat, attr, value)
            except (AttributeError, TypeError, ValueError):
                pass
    mat.diffuse_color = surface_color.inputs[1].default_value
    mat[WORLD_WATER_MATERIAL_PROP] = True
    mat[WORLD_WATER_KEY_PROP] = owner_key
    mat["witcher_world_water_version"] = 9
    mat["witcher_water_extinction_preview"] = 0.10
    mat["witcher_water_representative_depth_m"] = 0.65
    mat["witcher_water_heightmap_path"] = str(heightmap_path or "")
    mat["witcher_water_foam_texture"] = bool(foam_image is not None)
    mat["witcher_water_medium_normal_texture"] = bool(medium_normal_image is not None)
    return mat


def _ensure_world_water_plane(
    hub_name,
    terrain_size,
    heightmap_path="",
    lowest_elevation=0.0,
    highest_elevation=0.0,
    world_key="",
):
    scene = bpy.context.scene
    world_key = str(world_key or hub_name or "")
    obj_name = f"water_for_{hub_name}"
    water_obj = next(
        (
            obj for obj in scene.objects
            if bool(obj.get(WORLD_WATER_OBJECT_PROP, False))
            and str(obj.get(WORLD_WATER_KEY_PROP, "") or "") == world_key
        ),
        None,
    )
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
    water_obj[WORLD_WATER_OBJECT_PROP] = True
    water_obj[WORLD_WATER_KEY_PROP] = world_key

    try:
        water_obj.location = (0.0, 0.0, 0.0)
        water_obj.dimensions[0] = float(terrain_size)
        water_obj.dimensions[1] = float(terrain_size)
    except Exception:
        pass

    if water_obj.type == 'MESH':
        material_key = f"{scene.name_full}:{world_key}"
        mat = _ensure_simple_water_material(
            heightmap_path,
            lowest_elevation,
            highest_elevation,
            foam_image=_load_water_asset_image(
                "environment\\water\\global_ocean\\ocean_control.texarray"),
            medium_normal_image=_load_water_asset_image(
                "environment\\water\\global_ocean\\wave_med_n.xbm"),
            owner_key=material_key,
        )
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
    world_path="",
    world_root_collection=None,
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
                space.clip_end = max(float(space.clip_end), float(terrain_size) * math.sqrt(2.0))

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
    _store_world_layer_metadata(obj, world_path, world_root_collection)
    return obj


FULLMAP_BAKE_RES_DEFAULT = 8192
TERRAIN_LAYER_COLOR_CACHE_VERSION = 2
_TERRAIN_LAYER_FALLBACK_COLOR = (0.3, 0.3, 0.3)


def _get_scene_terrain_bake_res():
    try:
        return int(getattr(bpy.context.scene.witcher_file_browser, "terrain_bake_res", FULLMAP_BAKE_RES_DEFAULT))
    except Exception:
        return FULLMAP_BAKE_RES_DEFAULT


def _terrain_bake_enabled():
    try:
        return bool(getattr(bpy.context.scene.witcher_file_browser, "terrain_bake_diffuse", True))
    except Exception:
        return True


def _terrain_layer_color_cache_path(output_dir, hub_name):
    return os.path.join(output_dir, f"{hub_name}.layercolors.json")


def _looks_like_fallback_layer_colors(colors):
    try:
        arr = np.asarray(colors, dtype=np.float32)
    except Exception:
        return False
    if arr.ndim != 2 or arr.shape[0] == 0 or arr.shape[1] != 3:
        return False
    fallback = np.asarray(_TERRAIN_LAYER_FALLBACK_COLOR, dtype=np.float32)
    return bool(np.allclose(arr, fallback[None, :], atol=1e-6))


def _cached_layer_colors_are_complete(cached):
    if not isinstance(cached, dict) or not cached.get("colors"):
        return False
    if "complete" in cached and not bool(cached.get("complete")):
        return False
    try:
        if int(cached.get("missing_colors", 0) or 0) > 0:
            return False
    except Exception:
        return False
    # Legacy cache files did not record completeness. Reject the known poisoned
    # state where every layer was cached as the fallback gray.
    if "complete" not in cached and "missing_colors" not in cached:
        if _looks_like_fallback_layer_colors(cached.get("colors")):
            return False
    return True


def _read_cached_layer_colors(cache_path, texarray):
    if not os.path.isfile(cache_path):
        return None
    try:
        import json
        with open(cache_path, "r") as f:
            cached = json.load(f)
        if cached.get("texarray") != texarray:
            return None
        if not _cached_layer_colors_are_complete(cached):
            return None
        return np.array(cached["colors"], dtype=np.float32)
    except Exception:
        return None


def _terrain_preview_image_path(texture_path):
    if not texture_path:
        return ""
    texture_path = str(texture_path)
    if os.path.splitext(texture_path)[1].lower() != ".xbm":
        return texture_path

    source_path = os.path.abspath(texture_path)
    if not os.path.isfile(win_safe_path(source_path)):
        return ""

    try:
        stat = os.stat(win_safe_path(source_path))
        key = f"{os.path.normcase(os.path.normpath(source_path))}|{stat.st_mtime_ns}|{stat.st_size}"
    except Exception:
        key = os.path.normcase(os.path.normpath(source_path))
    digest = hashlib.sha1(key.encode("utf-8", errors="ignore")).hexdigest()
    stem = os.path.splitext(os.path.basename(source_path))[0] or "texture"
    cache_dir = os.path.join(
        get_redkit_working_root(create=True),
        "_converted_textures",
        "terrain_preview",
        digest[:2],
    )
    out_path = os.path.join(cache_dir, f"{stem}_{digest[:16]}.dds")
    try:
        os.makedirs(win_safe_path(cache_dir), exist_ok=True)
        if os.path.isfile(win_safe_path(out_path)):
            return out_path
        from ..CR2W import texture_converters

        converted_path = texture_converters.convert_xbm_to_dds(
            source_path,
            force=False,
            out_path=out_path,
        )
        if converted_path and os.path.isfile(win_safe_path(converted_path)):
            return converted_path
        if os.path.isfile(win_safe_path(out_path)):
            return out_path
    except Exception:
        log.debug("Terrain bake XBM conversion failed for %s", texture_path, exc_info=True)
    return ""


def _avg_dds_color(dds_path):
    image_path = _terrain_preview_image_path(dds_path)
    if not image_path or not os.path.isfile(win_safe_path(image_path)):
        return None
    img = None
    try:
        img = bpy_image_load_safe(image_path, check_existing=False)
        if img is None or img.size[0] == 0 or img.size[1] == 0:
            return None
        try:
            img.colorspace_settings.name = 'Non-Color'
        except Exception:
            pass
        try:
            img.scale(4, 4)
        except Exception:
            pass
        px = np.array(img.pixels[:], dtype=np.float32)
        if px.size < 4:
            return None
        rgb = px.reshape(-1, 4)[:, :3].mean(axis=0)
        return (float(rgb[0]), float(rgb[1]), float(rgb[2]))
    except Exception:
        log.debug("avg color failed for %s", image_path, exc_info=True)
        return None
    finally:
        if img is not None:
            try:
                bpy.data.images.remove(img)
            except Exception:
                pass


def _compute_terrain_layer_colors(worldFile, output_dir, hub_name):
    import json
    cache_path = _terrain_layer_color_cache_path(output_dir, hub_name)
    try:
        from ..unreal_export.terrain_material import extract_terrain_material_set
        mset = extract_terrain_material_set(worldFile)
    except Exception:
        log.debug("terrain material set extraction failed", exc_info=True)
        mset = None

    texarray = getattr(mset, "diffuse_texarray", "") if mset else ""

    cached_colors = _read_cached_layer_colors(cache_path, texarray)
    if cached_colors is not None:
        return cached_colors

    if not mset or not getattr(mset, "layers", None):
        if mset and getattr(mset, "warnings", None):
            log.info("Terrain bake: %s", "; ".join(mset.warnings))
        return None

    colors = []
    missing_colors = 0
    for layer in mset.layers:
        c = _avg_dds_color(getattr(layer, "diffuse_dds", ""))
        if c:
            colors.append(list(c))
        else:
            missing_colors += 1
            colors.append([0.3, 0.3, 0.3])
    if missing_colors:
        log.warning(
            "Terrain bake: %d diffuse texture(s) could not be sampled; using fallback layer colors.",
            missing_colors,
        )

    try:
        with open(cache_path, "w") as f:
            json.dump({
                "version": TERRAIN_LAYER_COLOR_CACHE_VERSION,
                "texarray": texarray,
                "colors": colors,
                "complete": missing_colors == 0,
                "missing_colors": missing_colors,
            }, f)
    except Exception:
        pass
    return np.array(colors, dtype=np.float32)


def _bake_fullmap_diffuse(worldFile, output_dir, hub_name, tiles, tile_res, n_tiles, buffer_paths):
    if not _terrain_bake_enabled() or not tiles:
        return None
    try:
        baked_path = os.path.join(output_dir, f"{hub_name}.terrain_baked.png")
        src_mtime = terrain_w2ter._max_source_mtime(buffer_paths)
        cache_path = _terrain_layer_color_cache_path(output_dir, hub_name)
        cache_mtime = terrain_w2ter._safe_mtime(cache_path)
        cache_complete = False
        try:
            import json
            with open(cache_path, "r") as f:
                cache_complete = _cached_layer_colors_are_complete(json.load(f))
        except Exception:
            cache_complete = False
        if cache_complete and terrain_w2ter._is_fresh(baked_path, max(src_mtime, cache_mtime)):
            return baked_path

        layer_colors = _compute_terrain_layer_colors(worldFile, output_dir, hub_name)
        if layer_colors is None or not len(layer_colors):
            log.info("Terrain bake skipped (no layer colors); using overlay palette")
            return None
        src_mtime = max(src_mtime, terrain_w2ter._safe_mtime(cache_path))
        return terrain_w2ter.bake_terrain_fullmap_from_tiles(
            tiles, tile_res, n_tiles, n_tiles, layer_colors, baked_path,
            out_res=_get_scene_terrain_bake_res(),
            use_slope=True,
            terrain_size=worldFile.terrainSize,
            lowest_elevation=worldFile.lowestElevation,
            highest_elevation=worldFile.highestElevation,
            skip_existing=True,
            src_mtime=src_mtime,
        )
    except Exception:
        log.warning("Terrain diffuse bake failed; using overlay palette", exc_info=True)
        return None


def _do_import_map_terrain_full_map(worldFile, filePath, world_root_collection=None):
    ctx = _resolve_terrain_context(worldFile, filePath)
    detail_enabled = _get_scene_terrain_detail_enabled()
    detail_spec = inspect_world_terrain(worldFile, filePath) if detail_enabled else None
    hub_name = ctx["hub_name"]
    n_tiles = ctx["n_tiles"]
    tile_res = ctx["tile_res"]
    water_key = _terrain_world_key(hub_name, filePath)

    if n_tiles <= 0:
        log.warning("Could not determine terrain tile grid for %s", hub_name)
        return None

    _ensure_world_water_plane(hub_name, worldFile.terrainSize, world_key=water_key)

    multires_level = _get_scene_terrain_multires_level()
    buffer_paths = _collect_tile_buffer_paths_for_combine(
        ctx["terrain_tiles_dir"],
        ctx["terrain_tiles_rel"],
        n_tiles,
        tile_res,
        working_tiles_dir=ctx["working_tiles_dir"],
    )
    if not buffer_paths:
        log.warning("No terrain buffers found for %s", hub_name)
        return None

    output_dir = str(ctx["output_dir"])
    combine_targets = (
        ("heightmap", "overlay", "bkgrnd", "blend", "tint")
        if ctx["working_tiles_dir"] or detail_enabled else ("heightmap",)
    )
    combined = terrain_w2ter.combine_w2ter_tiles(
        buffer_paths,
        output_dir,
        hub_name,
        res_override=tile_res,
        x_tiles_override=n_tiles,
        y_tiles_override=n_tiles,
        targets=combine_targets,
        skip_existing=True,
    )

    heightmap_path = os.path.join(output_dir, f"{hub_name}.heightmap.png")
    if not os.path.isfile(heightmap_path):
        log.warning("Missing combined heightmap PNG: %s", heightmap_path)
        return None
    _ensure_world_water_plane(
        hub_name,
        worldFile.terrainSize,
        heightmap_path,
        worldFile.lowestElevation,
        worldFile.highestElevation,
        world_key=water_key,
    )

    tiles = (combined or {}).get("info", {}).get("tiles", {}) or {}
    baked_path = _bake_fullmap_diffuse(
        worldFile, output_dir, hub_name, tiles, tile_res, n_tiles, buffer_paths
    )
    if baked_path and os.path.isfile(baked_path):
        colormap_path = baked_path
        log.info("Using baked terrain diffuse: %s", os.path.basename(baked_path))
    else:
        terrain_w2ter.combine_w2ter_tiles(
            buffer_paths,
            output_dir,
            hub_name,
            res_override=tile_res,
            x_tiles_override=n_tiles,
            y_tiles_override=n_tiles,
            targets=("overlay",),
            skip_existing=True,
        )
        colormap_path = os.path.join(output_dir, f"{hub_name}.overlay.png")
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
        world_path=filePath,
        world_root_collection=world_root_collection,
    )
    if obj:
        if detail_enabled:
            try:
                _apply_fullmap_detail_material(
                    worldFile,
                    detail_spec,
                    obj,
                    tiles.get(2, {}),
                    output_dir,
                )
            except Exception:
                log.warning(
                    "Full-map texarray material failed; keeping the baked terrain material",
                    exc_info=True,
                )
        log.info("Imported full terrain map: %s", obj.name)
    return obj


def _do_import_map_terrain_tiles(worldFile, filePath, world_root_collection=None):
    ctx = _resolve_terrain_context(worldFile, filePath)
    detail_enabled = _get_scene_terrain_detail_enabled()
    detail_spec = inspect_world_terrain(worldFile, filePath) if detail_enabled else None
    hub_name = ctx["hub_name"]
    n_tiles = ctx["n_tiles"]
    tile_res = ctx["tile_res"]
    water_key = _terrain_world_key(hub_name, filePath)

    if n_tiles <= 0:
        log.warning("Could not determine terrain tile grid for %s", hub_name)
        return

    _ensure_world_water_plane(hub_name, worldFile.terrainSize, world_key=water_key)

    multires_level = _get_scene_terrain_multires_level()

    # Find/extract tile buffers
    tile_heightmap_buffers = {}  # (x,y) -> raw .w2ter.1.buffer path
    tile_texture_buffers = {}    # (x,y) -> raw .w2ter.2.buffer path
    tile_overlays = {}           # (x,y) -> overlay PNG path

    for y in range(n_tiles):
        for x in range(n_tiles):
            tile_name = f"tile_{y}_x_{x}_res{tile_res}"

            # Buffer 1 = heightmap (raw uint16 data)
            buf1_name = f"{tile_name}.w2ter.1.buffer"
            buf1_path = _resolve_tile_buffer(
                ctx["terrain_tiles_dir"],
                ctx["terrain_tiles_rel"],
                buf1_name,
                working_tiles_dir=ctx["working_tiles_dir"],
            )
            if buf1_path:
                tile_heightmap_buffers[(x, y)] = buf1_path

            # Buffer 2 = texturemap (overlay PNG for material)
            buf2_name = f"{tile_name}.w2ter.2.buffer"
            buf2_path = _resolve_tile_buffer(
                ctx["terrain_tiles_dir"],
                ctx["terrain_tiles_rel"],
                buf2_name,
                working_tiles_dir=ctx["working_tiles_dir"],
            )
            if buf2_path:
                tile_texture_buffers[(x, y)] = str(buf2_path)
                info = terrain_w2ter.TileInfo(x=x, y=y, res=tile_res, buffer_index=2)
                overlay_path = buf2_path + ".overlay.png"
                try:
                    terrain_w2ter._tile_texture_pngs(
                        buf2_path, info, which=("overlay",), skip_existing=True
                    )
                except Exception:
                    pass
                if os.path.exists(overlay_path):
                    tile_overlays[(x, y)] = overlay_path

    if not tile_heightmap_buffers:
        log.warning("No terrain tile heightmaps found for %s", hub_name)
        return

    heightmap_path = os.path.join(str(ctx["output_dir"]), f"{hub_name}.heightmap.png")
    try:
        terrain_w2ter.combine_w2ter_tiles(
            list(tile_heightmap_buffers.values()),
            str(ctx["output_dir"]),
            hub_name,
            res_override=tile_res,
            x_tiles_override=n_tiles,
            y_tiles_override=n_tiles,
            targets=("heightmap",),
            skip_existing=True,
        )
    except Exception:
        log.warning("Water depth heightmap combine failed", exc_info=True)
    if os.path.isfile(heightmap_path):
        _ensure_world_water_plane(
            hub_name,
            worldFile.terrainSize,
            heightmap_path,
            worldFile.lowestElevation,
            worldFile.highestElevation,
            world_key=water_key,
        )

    log.info("Importing %d terrain tiles for %s (%dx%d grid)", len(tile_heightmap_buffers), hub_name, n_tiles, n_tiles)

    do_import_terrain_tiles(
        tile_heightmap_buffers=tile_heightmap_buffers,
        tile_texture_buffers=tile_texture_buffers,
        tile_overlays=tile_overlays,
        x_tiles=n_tiles,
        y_tiles=n_tiles,
        tile_res=tile_res,
        terrain_size=worldFile.terrainSize,
        lowest_elevation=worldFile.lowestElevation,
        highest_elevation=worldFile.highestElevation,
        multires_level=multires_level,
        hub_name=hub_name,
        world_path=filePath,
        world_root_collection=world_root_collection,
        world_file=worldFile,
        terrain_spec=detail_spec,
        detail_material=detail_enabled,
    )


def do_import_map_terrain(worldFile, filePath, world_root_collection=None):
    mode = _get_scene_terrain_import_mode()
    if mode == TERRAIN_IMPORT_TILES:
        _do_import_map_terrain_tiles(worldFile, filePath, world_root_collection)
        return

    obj = _do_import_map_terrain_full_map(worldFile, filePath, world_root_collection)
    if obj is None:
        # Do not silently fall back to all tiles.
        log.warning("Full-map terrain import failed; all-tiles fallback was not started")


def _terrain_grid_topology(mesh_res):
    """Return cached loop, polygon, and UV arrays for a square grid."""
    mesh_res = max(2, int(mesh_res))
    cached = _TERRAIN_GRID_TOPOLOGY_CACHE.get(mesh_res)
    if cached is not None:
        return cached

    face_side = mesh_res - 1
    lower_left = (
        np.arange(face_side, dtype=np.int32)[:, None] * mesh_res
        + np.arange(face_side, dtype=np.int32)[None, :]
    ).ravel()
    loop_vertices = np.column_stack((
        lower_left,
        lower_left + 1,
        lower_left + mesh_res + 1,
        lower_left + mesh_res,
    )).astype(np.int32, copy=False).ravel()
    face_count = face_side * face_side
    loop_starts = np.arange(0, loop_vertices.size, 4, dtype=np.int32)
    loop_totals = np.full(face_count, 4, dtype=np.int32)

    uv_axis = np.linspace(0.0, 1.0, mesh_res, dtype=np.float32)
    uv_x, uv_y = np.meshgrid(uv_axis, uv_axis)
    vertex_uv = np.column_stack((uv_x.ravel(), uv_y.ravel()))
    loop_uv = vertex_uv[loop_vertices].astype(np.float32, copy=False)
    cached = (loop_vertices, loop_starts, loop_totals, loop_uv)
    _TERRAIN_GRID_TOPOLOGY_CACHE[mesh_res] = cached
    return cached


def _read_tile_heightmap(heightmap_buffer_path, tile_res, cache=None):
    """Read and validate one row-major uint16 terrain height buffer."""
    tile_res = max(1, int(tile_res))
    source_path = str(heightmap_buffer_path)
    safe_path = win_safe_path(source_path)
    cache_key = (os.path.normcase(os.path.abspath(source_path)), tile_res)
    if cache is not None and cache_key in cache:
        heightmap = cache.pop(cache_key)
        cache[cache_key] = heightmap
        return heightmap

    heightmap = np.fromfile(safe_path, dtype="<u2")
    if heightmap.size != tile_res * tile_res:
        raise ValueError(
            f"Terrain buffer has {heightmap.size} samples; expected {tile_res * tile_res}"
        )
    heightmap = heightmap.reshape((tile_res, tile_res))
    if cache is not None:
        cache[cache_key] = heightmap
        while len(cache) > TERRAIN_HEIGHTMAP_CACHE_LIMIT:
            cache.pop(next(iter(cache)))
    return heightmap


def _read_optional_tile_heightmap(heightmap_buffer_path, tile_res, cache=None):
    if not heightmap_buffer_path:
        return None
    try:
        return _read_tile_heightmap(heightmap_buffer_path, tile_res, cache=cache)
    except (OSError, ValueError):
        log.warning(
            "Could not read terrain stitch buffer; clamping the tile edge: %s",
            heightmap_buffer_path,
            exc_info=True,
        )
        return None


def _stitch_tile_heightmap(current, right=None, up=None, diagonal=None):
    """Build a neighbor-padded square tile height lattice."""
    current = np.asarray(current)
    if current.ndim != 2 or current.shape[0] != current.shape[1]:
        raise ValueError("Terrain heightmap must be a square 2D array")

    shape = current.shape
    for label, neighbor in (("right", right), ("up", up), ("diagonal", diagonal)):
        if neighbor is not None and np.asarray(neighbor).shape != shape:
            raise ValueError(f"Terrain {label} heightmap shape does not match the current tile")

    tile_res = shape[0]
    stitched = np.empty((tile_res + 1, tile_res + 1), dtype=current.dtype)
    stitched[:tile_res, :tile_res] = current
    stitched[:tile_res, tile_res] = (
        np.asarray(right)[:, 0] if right is not None else current[:, -1]
    )
    stitched[tile_res, :tile_res] = (
        np.asarray(up)[0, :] if up is not None else current[-1, :]
    )
    stitched[tile_res, tile_res] = (
        np.asarray(diagonal)[0, 0] if diagonal is not None else current[-1, -1]
    )
    return stitched


def _terrain_tile_sample_indices(tile_res, mesh_res):
    """Return inclusive source-lattice indices for a terrain mesh level."""
    tile_res = max(1, int(tile_res))
    mesh_res = max(2, int(mesh_res))
    return np.rint(
        np.linspace(0, tile_res, mesh_res, dtype=np.float64)
    ).astype(np.intp)


def _create_tile_mesh(
    name,
    heightmap_buffer_path,
    tile_res,
    mesh_res,
    tile_size,
    elev_range,
    *,
    positive_x_buffer_path="",
    positive_y_buffer_path="",
    positive_xy_buffer_path="",
    heightmap_cache=None,
):
    """Create a terrain grid using neighbor edge samples."""
    heightmap = _read_tile_heightmap(
        heightmap_buffer_path, tile_res, cache=heightmap_cache)
    right = _read_optional_tile_heightmap(
        positive_x_buffer_path, tile_res, cache=heightmap_cache)
    up = _read_optional_tile_heightmap(
        positive_y_buffer_path, tile_res, cache=heightmap_cache)
    diagonal = _read_optional_tile_heightmap(
        positive_xy_buffer_path, tile_res, cache=heightmap_cache)
    stitched_heightmap = _stitch_tile_heightmap(heightmap, right, up, diagonal)

    mesh = bpy.data.meshes.new(name)
    half = tile_size / 2.0
    mesh_res = max(2, int(mesh_res))

    # Use NumPy and Blender bulk APIs for dense grids.
    axis = np.linspace(-half, half, mesh_res, dtype=np.float32)
    sample_axis = _terrain_tile_sample_indices(tile_res, mesh_res)
    sampled_height = stitched_heightmap[
        np.ix_(sample_axis, sample_axis)
    ].astype(np.float32)
    sampled_height *= float(elev_range) / 65535.0

    grid_x, grid_y = np.meshgrid(axis, axis)
    vertex_count = mesh_res * mesh_res
    coordinates = np.empty((vertex_count, 3), dtype=np.float32)
    coordinates[:, 0] = grid_x.ravel()
    coordinates[:, 1] = grid_y.ravel()
    coordinates[:, 2] = sampled_height.ravel()

    loop_vertices, loop_starts, loop_totals, loop_uv = _terrain_grid_topology(mesh_res)
    face_count = loop_starts.size

    mesh.vertices.add(vertex_count)
    mesh.vertices.foreach_set("co", coordinates.ravel())
    mesh.loops.add(loop_vertices.size)
    mesh.loops.foreach_set("vertex_index", loop_vertices)
    mesh.polygons.add(face_count)
    mesh.polygons.foreach_set("loop_start", loop_starts)
    mesh.polygons.foreach_set("loop_total", loop_totals)

    uv_layer = mesh.uv_layers.new(name="UVMap")
    uv_layer.uv.foreach_set("vector", loop_uv.ravel())
    mesh.uv_layers.active = uv_layer
    mesh.update(calc_edges=True)
    return mesh


def _terrain_tile_neighbor_path_from_object(obj, property_name, dx, dy):
    """Read stored stitch metadata, with a legacy loaded-sibling fallback."""
    path = str(obj.get(property_name, "") or "")
    if path:
        return path

    parent = getattr(obj, "parent", None)
    x = obj.get("tile_x")
    y = obj.get("tile_y")
    if parent is None or x is None or y is None:
        return ""
    target_x = int(x) + int(dx)
    target_y = int(y) + int(dy)
    for candidate in getattr(parent, "children", ()):
        if candidate is obj or getattr(candidate, "type", None) != 'MESH':
            continue
        if (
            int(candidate.get("tile_x", -1)) == target_x
            and int(candidate.get("tile_y", -1)) == target_y
        ):
            return str(candidate.get("tile_buffer_path", "") or "")
    return ""


def rebuild_tile_mesh(obj, target_level):
    """Rebuild a terrain tile at a new resolution."""
    buffer_path = obj.get("tile_buffer_path")
    tile_res = obj.get("tile_res")
    tile_size = obj.get("tile_size")
    elev_range = obj.get("elev_range")

    if not buffer_path or not os.path.isfile(win_safe_path(buffer_path)):
        return False
    if not all(v is not None for v in [tile_res, tile_size, elev_range]):
        return False

    target_level = _clamp_tile_multires_level(target_level, tile_res)
    mesh_res = (1 << target_level) + 1 if target_level > 0 else 2
    positive_x_buffer_path = _terrain_tile_neighbor_path_from_object(
        obj, "positive_x_buffer_path", 1, 0)
    positive_y_buffer_path = _terrain_tile_neighbor_path_from_object(
        obj, "positive_y_buffer_path", 0, 1)
    positive_xy_buffer_path = _terrain_tile_neighbor_path_from_object(
        obj, "positive_xy_buffer_path", 1, 1)
    old_mesh = obj.data
    new_mesh = _create_tile_mesh(
        obj.name, buffer_path, int(tile_res), mesh_res,
        float(tile_size), float(elev_range),
        positive_x_buffer_path=positive_x_buffer_path,
        positive_y_buffer_path=positive_y_buffer_path,
        positive_xy_buffer_path=positive_xy_buffer_path,
    )
    if old_mesh is not None:
        for material in old_mesh.materials:
            if material is not None:
                new_mesh.materials.append(material)
    obj.data = new_mesh
    obj["terrain_multires"] = target_level
    # Force reuse validation after a manual mesh rebuild.
    if "terrain_tile_source_signature" in obj:
        del obj["terrain_tile_source_signature"]

    # Remove old mesh if no other users
    if old_mesh and old_mesh.users == 0:
        bpy.data.meshes.remove(old_mesh)

    return True


def _apply_tile_overlay_material(obj, overlay_path, mat_name):
    """Create or update a stable per-tile overlay material."""
    named = bpy.data.materials.get(mat_name)
    mat = named if named is not None and named.get("witcher_terrain_material_key") == mat_name else None
    if mat is None:
        mat = next(
            (
                candidate
                for candidate in bpy.data.materials
                if candidate.get("witcher_terrain_material_key") == mat_name
            ),
            None,
        )
    if mat is None:
        mat = bpy.data.materials.new(name=mat_name)
    mat.use_nodes = True
    nt = mat.node_tree
    principled = nt.nodes.get("Principled BSDF")
    if principled is None:
        principled = nt.nodes.new("ShaderNodeBsdfPrincipled")
    tex = nt.nodes.get("Witcher Terrain Overlay")
    if tex is None or tex.bl_idname != "ShaderNodeTexImage":
        tex = nt.nodes.new("ShaderNodeTexImage")
        tex.name = "Witcher Terrain Overlay"
    uv = nt.nodes.get("Witcher Terrain UV")
    if uv is None or uv.bl_idname != "ShaderNodeUVMap":
        uv = nt.nodes.new("ShaderNodeUVMap")
        uv.name = "Witcher Terrain UV"
    uv.uv_map = "UVMap"
    uv.location = (tex.location[0] - 220, tex.location[1])
    if not tex.inputs["Vector"].is_linked:
        nt.links.new(uv.outputs["UV"], tex.inputs["Vector"])
    for link in tuple(principled.inputs["Base Color"].links):
        nt.links.remove(link)
    insert_color(mat, principled, tex, None, str(overlay_path))
    _set_principled_terrain_values(principled)
    mat["witcher_terrain_material"] = True
    mat["witcher_terrain_material_key"] = mat_name
    mat["witcher_terrain_overlay_path"] = str(overlay_path)
    if "witcher_terrain_detail_sig" in mat:
        del mat["witcher_terrain_detail_sig"]
    obj.data.materials.clear()
    obj.data.materials.append(mat)
    return mat


def _iter_collection_tree(root_collection):
    if root_collection is None:
        return
    seen = set()
    stack = [root_collection]
    while stack:
        collection = stack.pop()
        key = id(collection)
        try:
            key = int(collection.as_pointer())
        except Exception:
            pass
        if key in seen:
            continue
        seen.add(key)
        yield collection
        stack.extend(list(getattr(collection, "children", ()) or ()))


def _find_scene_world_collection(spec, scene=None):
    scene = scene or getattr(bpy.context, "scene", None)
    root = getattr(scene, "collection", None)
    for candidate in _iter_collection_tree(root) or ():
        if (
            candidate.get("witcher_terrain_world_collection")
            and candidate.get("witcher_terrain_world_key") == spec.world_key
        ):
            return candidate
    return None


def ensure_world_terrain_collection(spec, world_root_collection=None, *, scene=None):
    """Return a reusable lightweight world collection for terrain consumers."""
    if world_root_collection is not None:
        collection = world_root_collection
    else:
        scene = scene or getattr(bpy.context, "scene", None)
        collection = _find_scene_world_collection(spec, scene)
        if collection is None:
            collection = bpy.data.collections.new(f"world_{spec.hub_name}")
            scene.collection.children.link(collection)

    collection["witcher_terrain_world_collection"] = True
    collection["witcher_terrain_world_key"] = spec.world_key
    collection["world_path"] = spec.world_path
    collection["world_name"] = spec.world_name
    collection["terrainSize"] = spec.terrain_size
    collection["tileRes"] = spec.tile_res
    collection["x_tiles"] = spec.x_tiles
    collection["y_tiles"] = spec.y_tiles
    return collection


def _ensure_terrain_root(spec, multires_level, world_collection):
    root = None
    scoped_objects = list(getattr(world_collection, "all_objects", ()) or ())
    for candidate in scoped_objects:
        if (
            candidate.get("witcher_terrain_root")
            and candidate.get("witcher_terrain_world_key") == spec.world_key
        ):
            root = candidate
            break

    # Reuse an unambiguous legacy terrain root.
    if root is None:
        candidate = next(
            (obj for obj in scoped_objects if obj.name == f"terrain_{spec.hub_name}"),
            None,
        )
        if (
            candidate is not None
            and getattr(candidate, "type", None) == 'EMPTY'
            and "terrainSize" in candidate
            and "tileRes" in candidate
        ):
            candidate_path = str(candidate.get("world_path", "") or "")
            if not candidate_path or _terrain_world_key(spec.hub_name, candidate_path) == spec.world_key:
                root = candidate

    if root is None:
        root = bpy.data.objects.new(f"terrain_{spec.hub_name}", None)
        root.empty_display_type = 'PLAIN_AXES'

    if world_collection is not None and world_collection not in root.users_collection:
        world_collection.objects.link(root)
    elif not root.users_collection:
        bpy.context.collection.objects.link(root)

    tile_size = spec.terrain_size / max(1, spec.x_tiles, spec.y_tiles)
    root.empty_display_size = tile_size * 0.5
    root.location = (0.0, 0.0, 0.0)
    root["witcher_terrain_root"] = True
    root["witcher_terrain_world_key"] = spec.world_key
    root["terrainSize"] = spec.terrain_size
    root["tileRes"] = spec.tile_res
    root["lowestElevation"] = spec.lowest_elevation
    root["highestElevation"] = spec.highest_elevation
    root["x_tiles"] = spec.x_tiles
    root["y_tiles"] = spec.y_tiles
    root["multires_level"] = multires_level
    root["tile_y_inverted"] = False
    root["z_offset_applied_to_tiles"] = True
    _store_world_layer_metadata(root, spec.world_path, world_collection)
    return root


def _find_imported_terrain_tile(root, spec, key, existing_by_key=None):
    if root is None:
        return None
    coordinate = (key.x, key.y)
    if existing_by_key is not None:
        return existing_by_key.get(coordinate)
    for obj in root.children:
        if (
            getattr(obj, "type", None) == 'MESH'
            and obj.get("witcher_terrain_tile")
            and obj.get("witcher_terrain_world_key") == spec.world_key
            and int(obj.get("tile_x", -1)) == key.x
            and int(obj.get("tile_y", -1)) == key.y
        ):
            return obj

    # Reuse a matching legacy tile.
    for obj in root.children:
        if (
            getattr(obj, "type", None) == 'MESH'
            and obj.get("tile_buffer_path")
            and int(obj.get("tile_x", -1)) == key.x
            and int(obj.get("tile_y", -1)) == key.y
        ):
            return obj
    return None


def find_world_terrain_tile(spec, x, y, *, world_collection=None, root=None):
    """Find an imported tile by stable world/coordinate metadata."""

    key = TerrainTileKey(x, y)
    terrain_tile_bounds(spec, key.x, key.y)
    if root is None:
        world_collection = world_collection or _find_scene_world_collection(spec)
        if world_collection is None:
            return None
        root = next(
            (
                obj for obj in getattr(world_collection, "all_objects", ())
                if obj.get("witcher_terrain_root")
                and obj.get("witcher_terrain_world_key") == spec.world_key
            ),
            None,
        )
    return _find_imported_terrain_tile(root, spec, key)


def unload_world_terrain_tile(spec, x, y, *, world_collection=None, root=None):
    """Remove one imported terrain tile while retaining reusable world caches."""

    obj = find_world_terrain_tile(
        spec,
        x,
        y,
        world_collection=world_collection,
        root=root,
    )
    if obj is None:
        return False
    mesh = obj.data if getattr(obj, "type", None) == 'MESH' else None
    bpy.data.objects.remove(obj, do_unlink=True)
    if mesh is not None and mesh.users == 0:
        bpy.data.meshes.remove(mesh)
    return True


def _source_file_stamp(path):
    if not path:
        return ""
    try:
        stat = os.stat(win_safe_path(path))
        return f"{os.path.normcase(os.path.normpath(path))}:{stat.st_size}:{stat.st_mtime_ns}"
    except OSError:
        return os.path.normcase(os.path.normpath(path))


def _terrain_elevation_range(spec) -> float:
    return max(0.0, float(spec.highest_elevation) - float(spec.lowest_elevation))


def _tile_import_signature(spec, source, bounds, multires_level):
    values = (
        TERRAIN_TILE_STITCH_VERSION,
        spec.world_key,
        source.key.x,
        source.key.y,
        spec.tile_res,
        spec.terrain_size,
        spec.x_tiles,
        spec.y_tiles,
        spec.lowest_elevation,
        spec.highest_elevation,
        bounds.min_x,
        bounds.min_y,
        bounds.max_x,
        bounds.max_y,
        multires_level,
        _source_file_stamp(source.heightmap_buffer),
        _source_file_stamp(source.positive_x_buffer_path),
        _source_file_stamp(source.positive_y_buffer_path),
        _source_file_stamp(source.positive_xy_buffer_path),
        _source_file_stamp(source.overlay_path),
    )
    return hashlib.sha1(repr(values).encode("utf-8", errors="replace")).hexdigest()


def _prepare_tile_overlay(source, tile_res):
    if not source.request.include_overlay:
        return ""
    overlay_path = source.overlay_path or (source.texture_buffer + ".overlay.png")
    if not source.texture_buffer:
        return overlay_path if os.path.isfile(win_safe_path(overlay_path)) else ""
    try:
        before_stamp = _source_file_stamp(overlay_path)
    except Exception:
        before_stamp = ""
    info = terrain_w2ter.TileInfo(
        x=source.key.x,
        y=source.key.y,
        res=int(tile_res),
        buffer_index=2,
    )
    try:
        terrain_w2ter._tile_texture_pngs(
            source.texture_buffer,
            info,
            which=("overlay",),
            skip_existing=True,
        )
    except Exception:
        log.warning("Could not generate terrain tile overlay: %s", source.tile_name, exc_info=True)
    if not os.path.isfile(win_safe_path(overlay_path)):
        return ""
    if _source_file_stamp(overlay_path) != before_stamp:
        overlay_key = os.path.normcase(os.path.normpath(overlay_path))
        for image in getattr(bpy.data, "images", ()):
            image_path = str(getattr(image, "filepath", "") or "")
            try:
                image_key = os.path.normcase(os.path.normpath(bpy.path.abspath(image_path)))
            except Exception:
                image_key = os.path.normcase(os.path.normpath(image_path))
            if image_key == overlay_key:
                try:
                    image.reload()
                except Exception:
                    log.debug("Could not reload refreshed terrain overlay: %s", overlay_path, exc_info=True)
    return overlay_path


def _tile_material_name(spec, key):
    return f"terrain_{spec.hub_name}_{spec.world_key[:8]}_tile_{key.y}_x_{key.x}_mat"


_TERRAIN_DETAIL_WORLD_CACHE = {}


def _terrain_detail_cached_assets_available(assets):
    try:
        atlas = assets["atlas"]
        diffuse_path = str(atlas.get("diffuse", "") or "")
    except (AttributeError, KeyError, TypeError):
        return False
    if not diffuse_path:
        return False

    for field in ("diffuse", "normal", "json"):
        path = str(atlas.get(field, "") or "")
        if path and not os.path.isfile(win_safe_path(path)):
            return False
    return True


def _get_scene_terrain_detail_enabled():
    try:
        return bool(bpy.context.scene.witcher_file_browser.terrain_detail_material)
    except Exception:
        return True


def _get_scene_terrain_detail_res():
    try:
        return int(bpy.context.scene.witcher_file_browser.terrain_detail_texture_res)
    except Exception:
        return 1024


def _world_colormap_start_mip(worldFile):
    clip = getattr(worldFile, "terrainClipMap", None)
    if clip is None:
        return None
    try:
        prop = clip.GetVariableByName("colormapStartingMip")
        if prop is not None:
            return int(prop.Value)
    except Exception:
        pass
    return None


def _resolve_tile_tint_buffer_at(spec, mip, x, y):
    if mip is None or int(mip) < 0:
        return ""
    tile_name = f"tile_{int(y)}_x_{int(x)}_res{int(spec.tile_res)}"
    buf_name = f"{tile_name}.w2ter.{int(mip) * 2 + 3}.buffer"
    path = _resolve_tile_buffer(
        spec.terrain_tiles_dir,
        spec.terrain_tiles_rel or None,
        buf_name,
        working_tiles_dir=spec.working_tiles_dir or None,
    )
    return str(path or "")


def _resolve_tile_tint_buffer(spec, worldFile, source):
    return _resolve_tile_tint_buffer_at(
        spec,
        _world_colormap_start_mip(worldFile),
        source.key.x,
        source.key.y,
    )


def _terrain_detail_parameter_world_path(spec):
    selected_path = os.path.normpath(str(spec.world_path or ""))
    if not selected_path:
        return ""

    source_roots = [
        *_configured_redkit_workspace_roots(),
        *_configured_redkit_roots(),
    ]
    unique_roots = []
    seen = set()
    for root in source_roots:
        key = os.path.normcase(os.path.normpath(str(root)))
        if key and key not in seen:
            seen.add(key)
            unique_roots.append(os.path.normpath(str(root)))
    if not unique_roots:
        return selected_path

    relative_path = None
    try:
        uncook_root = get_uncook_path(bpy.context)
    except Exception:
        uncook_root = ""
    if uncook_root:
        relative_path = _relpath_under_root(selected_path, uncook_root)
    if not relative_path:
        for root in unique_roots:
            relative_path = _relpath_under_root(selected_path, root)
            if relative_path:
                break
    if not relative_path:
        parts = Path(selected_path).parts
        for index, part in enumerate(parts):
            if part.casefold() in {"levels", "dlc"}:
                relative_path = os.path.join(*parts[index:])
                break
    if not relative_path:
        return selected_path

    for root in unique_roots:
        candidate = os.path.normpath(os.path.join(root, relative_path))
        if os.path.isfile(win_safe_path(candidate)):
            return candidate
    return selected_path


def _terrain_detail_parameter_world(worldFile, spec, source_path=None):
    selected_path = os.path.normpath(str(spec.world_path or ""))
    source_path = os.path.normpath(str(
        source_path or _terrain_detail_parameter_world_path(spec) or selected_path))
    if not source_path or os.path.normcase(source_path) == os.path.normcase(selected_path):
        return worldFile, selected_path
    try:
        source_world = CR2W.CR2W_reader.load_w2w(
            source_path, include_groups=False)
    except Exception:
        log.warning(
            "Could not read project Texture Pack values: %s",
            source_path,
            exc_info=True,
        )
        return worldFile, selected_path
    return (source_world, source_path) if source_world is not None else (worldFile, selected_path)


def _terrain_detail_world_assets(worldFile, spec, slice_px):
    key = (spec.world_key, int(slice_px))
    parameter_world_path = _terrain_detail_parameter_world_path(spec)
    source_stamp = _source_file_stamp(parameter_world_path)
    cached = _TERRAIN_DETAIL_WORLD_CACHE.get(key)
    if (
        cached is not None
        and cached.get("source_stamp") == source_stamp
        and _terrain_detail_cached_assets_available(cached)
    ):
        return cached
    if cached is not None:
        _TERRAIN_DETAIL_WORLD_CACHE.pop(key, None)

    parameter_world, parameter_world_path = _terrain_detail_parameter_world(
        worldFile, spec, parameter_world_path)
    source_stamp = _source_file_stamp(parameter_world_path)

    from ..unreal_export import terrain_material as w3_terrain_material
    from . import terrain_detail

    # Prefer project material parameters over generated fallback data when both
    # are available.
    with redkit_repo_context(
        parameter_world_path or spec.world_path,
        roots=_configured_redkit_roots(),
    ):
        mat_set = w3_terrain_material.extract_terrain_material_set(parameter_world)
    if not mat_set.layers:
        raise RuntimeError(
            "; ".join(mat_set.warnings) or "terrain material has no texture layers")

    out_dir = str(spec.working_tiles_dir or spec.terrain_tiles_dir)
    atlas = terrain_detail.pack_world_detail_atlases(
        spec.hub_name, mat_set.layers, out_dir, slice_px=int(slice_px))
    if atlas is None:
        raise RuntimeError("could not pack terrain detail atlases")

    param_fields = ("blend_sharpness", "slope_base_dampening", "slope_normal_dampening",
                    "falloff", "specularity", "specularity_base", "specularity_scale")
    params_rows = [{f: getattr(layer, f, None) for f in param_fields}
                   for layer in mat_set.layers]
    assets = {
        "atlas": atlas,
        "params_rows": params_rows,
        "layer_count": len(mat_set.layers),
        "layer_metadata": terrain_detail.build_terrain_layer_metadata(mat_set.layers),
        "fresnel_power": float(getattr(mat_set, "fresnel_power", 2.0)),
        "source_stamp": source_stamp,
        "parameter_world_path": str(parameter_world_path or spec.world_path),
    }
    _TERRAIN_DETAIL_WORLD_CACHE[key] = assets
    return assets


_TERRAIN_INSPECTOR_PATH_PROPS = {
    "texture_buffer": "witcher_terrain_texture_buffer",
    "positive_x_texture_buffer": "witcher_terrain_positive_x_texture_buffer",
    "positive_y_texture_buffer": "witcher_terrain_positive_y_texture_buffer",
    "positive_xy_texture_buffer": "witcher_terrain_positive_xy_texture_buffer",
}
_TERRAIN_INSPECTOR_LAYERS_PROP = "witcher_terrain_layer_metadata"


def _store_terrain_inspector_metadata(obj, source, tile_res, layer_metadata):
    if obj is None:
        return
    values = {
        "texture_buffer": getattr(source, "texture_buffer", ""),
        "positive_x_texture_buffer": getattr(source, "positive_x_texture_buffer_path", ""),
        "positive_y_texture_buffer": getattr(source, "positive_y_texture_buffer_path", ""),
        "positive_xy_texture_buffer": getattr(source, "positive_xy_texture_buffer_path", ""),
    }
    for key, prop_name in _TERRAIN_INSPECTOR_PATH_PROPS.items():
        obj[prop_name] = str(values[key] or "")
    obj["witcher_terrain_control_res"] = int(tile_res)
    obj[_TERRAIN_INSPECTOR_LAYERS_PROP] = json.dumps(
        list(layer_metadata or []), separators=(",", ":"))


def _control_buffer_from_height_buffer(path):
    path = str(path or "")
    suffix = ".1.buffer"
    if path.lower().endswith(suffix):
        return path[:-len(suffix)] + ".2.buffer"
    return ""


def ensure_terrain_inspector_metadata(obj):
    if obj is None:
        raise RuntimeError("No terrain tile is active")

    result = {
        key: str(obj.get(prop_name, "") or "")
        for key, prop_name in _TERRAIN_INSPECTOR_PATH_PROPS.items()
    }
    legacy_height_props = {
        "texture_buffer": "tile_buffer_path",
        "positive_x_texture_buffer": "positive_x_buffer_path",
        "positive_y_texture_buffer": "positive_y_buffer_path",
        "positive_xy_texture_buffer": "positive_xy_buffer_path",
    }
    for key, height_prop in legacy_height_props.items():
        if not result[key]:
            result[key] = _control_buffer_from_height_buffer(obj.get(height_prop, ""))
            if result[key]:
                obj[_TERRAIN_INSPECTOR_PATH_PROPS[key]] = result[key]

    try:
        result["res"] = int(obj.get("witcher_terrain_control_res", obj.get("tile_res", 0)) or 0)
    except (TypeError, ValueError):
        result["res"] = 0
    if result["res"] <= 0:
        raise RuntimeError("The terrain tile has no control-map resolution metadata")
    if not result["texture_buffer"] or not os.path.isfile(win_safe_path(result["texture_buffer"])):
        raise RuntimeError(f"Terrain control buffer not found: {result['texture_buffer'] or '<unset>'}")

    layer_metadata = []
    encoded = str(obj.get(_TERRAIN_INSPECTOR_LAYERS_PROP, "") or "")
    if encoded:
        try:
            decoded = json.loads(encoded)
            if isinstance(decoded, list):
                layer_metadata = decoded
        except (TypeError, ValueError, json.JSONDecodeError):
            layer_metadata = []

    if not layer_metadata:
        world_path = str(obj.get("world_path", "") or "")
        if not world_path:
            parent = getattr(obj, "parent", None)
            world_path = str(parent.get("world_path", "") or "") if parent is not None else ""
        if not world_path or not os.path.isfile(win_safe_path(world_path)):
            raise RuntimeError("Layer names are unavailable; reimport this tile with the updated importer")

        from ..CR2W import CR2W_reader
        from ..unreal_export import terrain_material as w3_terrain_material
        from . import terrain_detail

        world_file = CR2W_reader.load_w2w(world_path)
        with redkit_repo_context(world_path, roots=_configured_redkit_roots()):
            mat_set = w3_terrain_material.extract_terrain_material_set(world_file)
        layer_metadata = terrain_detail.build_terrain_layer_metadata(mat_set.layers)
        if not layer_metadata:
            raise RuntimeError(
                "; ".join(getattr(mat_set, "warnings", []) or [])
                or "The terrain material contains no atlas layers")
        obj[_TERRAIN_INSPECTOR_LAYERS_PROP] = json.dumps(
            layer_metadata, separators=(",", ":"))

    result["layers"] = layer_metadata
    return result


def _apply_tile_detail_material(worldFile, spec, source, bounds, obj, mat_name):
    from . import terrain_detail, terrain_detail_nodes

    assets = _terrain_detail_world_assets(worldFile, spec, _get_scene_terrain_detail_res())
    tint_mip = _world_colormap_start_mip(worldFile)
    tint_buffer = _resolve_tile_tint_buffer_at(
        spec, tint_mip, source.key.x, source.key.y)
    positive_x_tint_buffer = ""
    positive_y_tint_buffer = ""
    positive_xy_tint_buffer = ""
    if source.key.x + 1 < int(spec.x_tiles):
        positive_x_tint_buffer = _resolve_tile_tint_buffer_at(
            spec, tint_mip, source.key.x + 1, source.key.y)
    if source.key.y + 1 < int(spec.y_tiles):
        positive_y_tint_buffer = _resolve_tile_tint_buffer_at(
            spec, tint_mip, source.key.x, source.key.y + 1)
    if (
        source.key.x + 1 < int(spec.x_tiles)
        and source.key.y + 1 < int(spec.y_tiles)
    ):
        positive_xy_tint_buffer = _resolve_tile_tint_buffer_at(
            spec, tint_mip, source.key.x + 1, source.key.y + 1)
    maps = terrain_detail.build_tile_detail_maps(
        source.texture_buffer,
        source.heightmap_buffer,
        int(spec.tile_res),
        float(bounds.tile_size),
        _terrain_elevation_range(spec),
        assets["params_rows"],
        layer_count=assets["layer_count"],
        tint_buffer=tint_buffer,
        positive_x_texture_buffer=source.positive_x_texture_buffer_path,
        positive_y_texture_buffer=source.positive_y_texture_buffer_path,
        positive_xy_texture_buffer=source.positive_xy_texture_buffer_path,
        positive_x_heightmap_buffer=source.positive_x_buffer_path,
        positive_y_heightmap_buffer=source.positive_y_buffer_path,
        positive_xy_heightmap_buffer=source.positive_xy_buffer_path,
        positive_x_tint_buffer=positive_x_tint_buffer,
        positive_y_tint_buffer=positive_y_tint_buffer,
        positive_xy_tint_buffer=positive_xy_tint_buffer,
    )
    if maps is None:
        raise RuntimeError(f"tile control buffer unavailable: {source.texture_buffer}")
    _store_terrain_inspector_metadata(
        obj, source, int(spec.tile_res), assets.get("layer_metadata") or [])
    mat = terrain_detail_nodes.apply_tile_detail_material(
        obj,
        mat_name,
        assets["atlas"],
        maps,
        fresnel_power=assets["fresnel_power"],
        texture_pack_key=spec.world_key,
        layer_metadata=assets.get("layer_metadata") or [],
    )
    if mat is not None:
        _apply_terrain_material_controls(mat, _get_terrain_material_controls())
    return mat


def _apply_fullmap_detail_material(worldFile, spec, obj, texture_buffers, out_dir):
    from . import terrain_detail, terrain_detail_nodes

    assets = _terrain_detail_world_assets(worldFile, spec, _get_scene_terrain_detail_res())
    tint_path = os.path.join(str(out_dir), f"{spec.hub_name}.tint.png")
    maps = terrain_detail.build_fullmap_detail_maps(
        texture_buffers,
        int(spec.tile_res),
        int(spec.x_tiles),
        int(spec.y_tiles),
        assets["params_rows"],
        str(out_dir),
        spec.hub_name,
        layer_count=assets["layer_count"],
        target_res=_get_scene_terrain_bake_res(),
        tint_path=tint_path,
    )
    if maps is None:
        raise RuntimeError("full-map terrain control buffers are unavailable")
    mat_name = f"terrain_{spec.hub_name}_{spec.world_key[:8]}_full_mat"
    mat = terrain_detail_nodes.apply_tile_detail_material(
        obj,
        mat_name,
        assets["atlas"],
        maps,
        fresnel_power=assets["fresnel_power"],
        texture_pack_key=spec.world_key,
        layer_metadata=assets.get("layer_metadata") or [],
    )
    if mat is not None:
        _apply_terrain_material_controls(mat, _get_terrain_material_controls())
    return mat


def _link_terrain_object(obj, world_collection):
    if world_collection is not None and world_collection not in obj.users_collection:
        world_collection.objects.link(obj)
    elif not obj.users_collection:
        bpy.context.collection.objects.link(obj)


def _import_resolved_terrain_tile(
    spec,
    source,
    bounds,
    *,
    multires_level,
    world_collection,
    root=None,
    existing_by_key=None,
    heightmap_cache=None,
):
    if not source.heightmap_buffer or not os.path.isfile(win_safe_path(source.heightmap_buffer)):
        return TerrainTileImportResult(
            spec=spec,
            source=source,
            bounds=bounds,
            world_collection=world_collection,
            error=f"Heightmap buffer not found for {source.tile_name}",
        )

    level = _clamp_tile_multires_level(multires_level, spec.tile_res)
    overlay_path = _prepare_tile_overlay(source, spec.tile_res)
    if overlay_path != source.overlay_path:
        source = replace(source, overlay_path=overlay_path)

    if root is None:
        root = _ensure_terrain_root(spec, level, world_collection)
    existing = _find_imported_terrain_tile(root, spec, source.key, existing_by_key)
    signature = _tile_import_signature(spec, source, bounds, level)
    if existing is not None and existing.get("terrain_tile_source_signature") == signature:
        _link_terrain_object(existing, world_collection)
        existing.parent = root
        return TerrainTileImportResult(
            spec=spec,
            source=source,
            bounds=bounds,
            obj=existing,
            root=root,
            world_collection=world_collection,
            reused=True,
        )

    mesh_res = (1 << level) + 1 if level > 0 else 2
    mesh = _create_tile_mesh(
        f"tile_{source.key.y}_x_{source.key.x}",
        source.heightmap_buffer,
        spec.tile_res,
        mesh_res,
        bounds.tile_size,
        _terrain_elevation_range(spec),
        positive_x_buffer_path=source.positive_x_buffer_path,
        positive_y_buffer_path=source.positive_y_buffer_path,
        positive_xy_buffer_path=source.positive_xy_buffer_path,
        heightmap_cache=heightmap_cache,
    )

    created = existing is None
    if created:
        obj = bpy.data.objects.new(f"tile_{source.key.y}_x_{source.key.x}", mesh)
        _link_terrain_object(obj, world_collection)
        old_mesh = None
    else:
        obj = existing
        old_mesh = obj.data
        obj.data = mesh

    obj.location = (bounds.center_x, bounds.center_y, spec.lowest_elevation)
    obj["witcher_terrain_tile"] = True
    obj["witcher_terrain_world_key"] = spec.world_key
    obj["tile_x"] = source.key.x
    obj["tile_y"] = source.key.y
    obj["tile_world_y"] = bounds.world_y
    obj["terrain_multires"] = level
    obj["tile_buffer_path"] = source.heightmap_buffer
    obj["positive_x_buffer_path"] = source.positive_x_buffer_path
    obj["positive_y_buffer_path"] = source.positive_y_buffer_path
    obj["positive_xy_buffer_path"] = source.positive_xy_buffer_path
    obj["tile_res"] = spec.tile_res
    obj["tile_size"] = bounds.tile_size
    obj["elev_range"] = _terrain_elevation_range(spec)
    obj["lowest_elevation"] = spec.lowest_elevation
    obj["tile_bounds_min_x"] = bounds.min_x
    obj["tile_bounds_min_y"] = bounds.min_y
    obj["tile_bounds_max_x"] = bounds.max_x
    obj["tile_bounds_max_y"] = bounds.max_y
    obj["terrain_tile_source_signature"] = signature
    _store_world_layer_metadata(obj, spec.world_path, world_collection)

    if overlay_path:
        _apply_tile_overlay_material(obj, overlay_path, _tile_material_name(spec, source.key))

    obj.parent = root
    obj.matrix_parent_inverse = root.matrix_world.inverted()
    if existing_by_key is not None:
        existing_by_key[(source.key.x, source.key.y)] = obj
    if old_mesh is not None and old_mesh.users == 0:
        bpy.data.meshes.remove(old_mesh)

    return TerrainTileImportResult(
        spec=spec,
        source=source,
        bounds=bounds,
        obj=obj,
        root=root,
        world_collection=world_collection,
        created=created,
    )


def import_world_terrain_tile(
    worldFile,
    filePath,
    x,
    y,
    *,
    multires_level=TERRAIN_TILE_PREVIEW_LEVEL,
    world_root_collection=None,
    include_overlay=True,
    detail_material=None,
):
    """Resolve and import one terrain tile."""
    spec = inspect_world_terrain(worldFile, filePath)
    bounds = terrain_tile_bounds(spec, x, y)
    with redkit_repo_context(filePath):
        source = resolve_world_terrain_tile_source(
            spec,
            x,
            y,
            include_overlay=include_overlay,
            include_stitch_neighbors=True,
        )
    if not source.available:
        return TerrainTileImportResult(
            spec=spec,
            source=source,
            bounds=bounds,
            world_collection=world_root_collection,
            error=f"Heightmap buffer not found for {source.tile_name}",
        )

    world_collection = ensure_world_terrain_collection(spec, world_root_collection)
    result = _import_resolved_terrain_tile(
        spec,
        source,
        bounds,
        multires_level=multires_level,
        world_collection=world_collection,
    )
    if detail_material is None:
        detail_material = _get_scene_terrain_detail_enabled()
    if result.ok and detail_material:
        try:
            with redkit_repo_context(filePath):
                _apply_tile_detail_material(
                    worldFile, spec, result.source, result.bounds, result.obj,
                    _tile_material_name(spec, result.source.key))
        except Exception:
            log.warning("Terrain detail material failed for %s; keeping the overlay material",
                        source.tile_name, exc_info=True)
    return result


def import_world_tile_with_foliage(
    world_file,
    file_path,
    x,
    y,
    *,
    context=None,
    multires_level=TERRAIN_TILE_PREVIEW_LEVEL,
    world_root_collection=None,
    include_foliage=True,
    foliage_mode="PROXY",
    detail_material=None,
):
    """Import one terrain tile and treat foliage failure as partial success."""

    spec = inspect_world_terrain(world_file, file_path)
    terrain = import_world_terrain_tile(
        world_file,
        file_path,
        x,
        y,
        multires_level=multires_level,
        world_root_collection=world_root_collection,
        detail_material=detail_material,
    )
    if terrain is None or not terrain.ok:
        raise FileNotFoundError(
            getattr(terrain, "error", "") or f"Terrain tile {int(x)}, {int(y)} was not found"
        )

    foliage = None
    foliage_error = ""
    if include_foliage:
        try:
            from . import import_foliage

            foliage = import_foliage.load_foliage_for_tile(
                file_path,
                terrain.world_collection,
                context or getattr(bpy, "context", None),
                int(x),
                int(y),
                int(spec.x_tiles),
                int(spec.y_tiles),
                float(spec.terrain_size),
                source_mode=str(foliage_mode or "PROXY"),
            )
        except Exception as exc:
            foliage_error = str(exc)
            log.exception("Tile foliage import failed")
    return WorldTileLoadResult(
        spec=spec,
        terrain=terrain,
        foliage=foliage,
        foliage_error=foliage_error,
    )


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
    world_path="",
    world_root_collection=None,
    tile_texture_buffers=None,
    world_file=None,
    terrain_spec=None,
    detail_material=None,
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
    level = _clamp_tile_multires_level(multires_level, tile_res)
    world_path = str(world_path or "")
    spec = terrain_spec or TerrainWorldSpec(
        hub_name=str(hub_name or "terrain"),
        world_name=str(hub_name or "terrain"),
        world_path=world_path,
        world_key=_terrain_world_key(hub_name, world_path),
        terrain_size=float(terrain_size),
        lowest_elevation=float(lowest_elevation),
        highest_elevation=float(highest_elevation),
        tile_res=max(1, int(tile_res)),
        x_tiles=max(0, int(x_tiles)),
        y_tiles=max(0, int(y_tiles)),
        terrain_tiles_dir="",
    )
    tile_texture_buffers = tile_texture_buffers or {}
    if detail_material is None:
        detail_material = _get_scene_terrain_detail_enabled()
    world_collection = ensure_world_terrain_collection(spec, world_root_collection)
    empty = _ensure_terrain_root(spec, level, world_collection)
    existing_by_key = {
        (int(obj.get("tile_x", -1)), int(obj.get("tile_y", -1))): obj
        for obj in empty.children
        if getattr(obj, "type", None) == 'MESH'
        and obj.get("tile_buffer_path")
        and int(obj.get("tile_x", -1)) >= 0
        and int(obj.get("tile_y", -1)) >= 0
    }

    # Set viewport clip
    screen = getattr(bpy.context, "screen", None)
    for a in getattr(screen, "areas", ()):
        if a.type == 'VIEW_3D':
            for s in a.spaces:
                if s.type == 'VIEW_3D':
                    s.clip_end = max(float(s.clip_end), float(spec.terrain_size) * math.sqrt(2.0))

    count = 0
    heightmap_cache = {}
    for (x, y), buffer_path in sorted(
        tile_heightmap_buffers.items(),
        key=lambda item: (item[0][1], item[0][0]),
    ):
        overlay_path = tile_overlays.get((x, y))
        request = terrain_tile_source_request(
            x,
            y,
            include_overlay=bool(overlay_path),
            include_stitch_neighbors=True,
        )
        source = TerrainTileSource(
            request=request,
            tile_name=f"tile_{y}_x_{x}_res{spec.tile_res}",
            heightmap_buffer=str(buffer_path or ""),
            texture_buffer=str(tile_texture_buffers.get((x, y)) or ""),
            overlay_path=str(overlay_path or ""),
            positive_x_buffer_path=str(
                tile_heightmap_buffers.get((x + 1, y)) or ""),
            positive_y_buffer_path=str(
                tile_heightmap_buffers.get((x, y + 1)) or ""),
            positive_xy_buffer_path=str(
                tile_heightmap_buffers.get((x + 1, y + 1)) or ""),
            positive_x_texture_buffer_path=str(
                tile_texture_buffers.get((x + 1, y)) or ""),
            positive_y_texture_buffer_path=str(
                tile_texture_buffers.get((x, y + 1)) or ""),
            positive_xy_texture_buffer_path=str(
                tile_texture_buffers.get((x + 1, y + 1)) or ""),
        )
        result = _import_resolved_terrain_tile(
            spec,
            source,
            terrain_tile_bounds(spec, x, y),
            multires_level=level,
            world_collection=world_collection,
            root=empty,
            existing_by_key=existing_by_key,
            heightmap_cache=heightmap_cache,
        )
        if result.ok:
            count += 1
            if detail_material and world_file is not None and source.texture_buffer:
                try:
                    with redkit_repo_context(spec.world_path):
                        _apply_tile_detail_material(
                            world_file,
                            spec,
                            source,
                            result.bounds,
                            result.obj,
                            _tile_material_name(spec, source.key),
                        )
                except Exception:
                    log.warning(
                        "Terrain detail material failed for %s; keeping the overlay material",
                        source.tile_name,
                        exc_info=True,
                    )

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


