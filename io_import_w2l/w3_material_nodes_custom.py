import bpy
import os
from bpy.types import ShaderNodeCustomGroup, ShaderNodeGroup
import nodeitems_utils
from nodeitems_utils import NodeCategory, NodeItem

def AppendRENodeTree(reShaderName) -> bpy.types.ShaderNodeTree:
    path = os.path.join(os.path.dirname(__file__), "witcher3_materials.blend")
    bpy.ops.wm.append(
        filepath=os.path.join(path, 'NodeTree', reShaderName),
        directory=os.path.join(path, 'NodeTree'),
        filename=reShaderName
    )

    return bpy.data.node_groups.get(reShaderName)

def CleanNodeTree(nodeTree: bpy.types.NodeTree):
    if nodeTree is not None:
        for node in nodeTree.nodes:
            if node is not None and hasattr(node, 'node_tree'):
                CleanNodeTree(node.node_tree)
        bpy.data.node_groups.remove(nodeTree, do_unlink=True)


def DeepCopyNodeTree(nodeTree: bpy.types.NodeTree) -> bpy.types.NodeTree or None:
    if nodeTree is not None:
        nodeTree = nodeTree.copy()
        for node in nodeTree.nodes:
            if node is not None and hasattr(node, 'node_tree'):
                node.node_tree = DeepCopyNodeTree(node.node_tree)
        return nodeTree
    return None


def GetRENodeNoCopy(reShaderName) -> bpy.types.ShaderNodeTree:
    reNode: bpy.types.ShaderNodeTree or None = bpy.data.node_groups.get(reShaderName)
    return AppendRENodeTree(reShaderName) if reNode is None else reNode


def GetRENodeCopy(reShaderName) -> bpy.types.ShaderNodeTree or None:
    nodeTree = GetRENodeNoCopy(reShaderName)

    return DeepCopyNodeTree(nodeTree)

class Witcher3_Vector(ShaderNodeCustomGroup): #REENodeAlbedoWrinkles(ShaderNodeCustomGroup):
    bl_name = 'Witcher3_Vector'
    bl_label = "Witcher3_Vector"
    bl_description = "Witcher3 Vector"
    bl_type = 'NODE_WITCHER3_VECTOR'

    def init(self, context):
        self.node_tree = GetRENodeCopy('Witcher3_Vector')
        self.width = 240.0

    def draw_label(self):
        return self.bl_label

    def copy(self, node):
        if node.node_tree:
            self.node_tree = DeepCopyNodeTree(self.node_tree)
        else:
            self.node_tree = GetRENodeCopy('Witcher3_Vector')

    def free(self):
        CleanNodeTree(self.node_tree)
        self.node_tree = None

class MyNodeCategory(NodeCategory):
    @classmethod
    def poll(cls, context):
        return context.space_data.tree_type == 'ShaderNodeTree'

_classes = [
    Witcher3_Vector,
]

from bpy.utils import (register_class, unregister_class)

def register():
    for cls in _classes:
        register_class(cls)
    nodeitems_utils.register_node_categories('W3CustomShaderNodes',
            [MyNodeCategory('W3NODES', "W3 Node", items=[NodeItem(cls.bl_name) for cls in _classes])])

def unregister():
    nodeitems_utils.unregister_node_categories('W3CustomShaderNodes')
    for cls in _classes:
        unregister_class(cls)