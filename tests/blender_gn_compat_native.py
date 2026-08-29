import sys
from pathlib import Path

import bpy

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from witcher3_tools import duplication
from witcher3_tools.gn_compat import gn_input_get, gn_input_identifiers, gn_input_set


def mesh_object(name, collection):
    obj = bpy.data.objects.new(name, bpy.data.meshes.new(name))
    collection.objects.link(obj)
    return obj


collection = bpy.data.collections.new("Source")
bpy.context.scene.collection.children.link(collection)
host = mesh_object("host", collection)
target = mesh_object("target", collection)
target.parent = host

group = bpy.data.node_groups.new("gn", "GeometryNodeTree")
group.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
group.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
obj_socket = group.interface.new_socket("Target", in_out="INPUT", socket_type="NodeSocketObject")
float_socket = group.interface.new_socket("Wind", in_out="INPUT", socket_type="NodeSocketFloat")
group_in = group.nodes.new("NodeGroupInput")
group_out = group.nodes.new("NodeGroupOutput")
group.links.new(group_in.outputs[0], group_out.inputs[0])

mod = host.modifiers.new("GN", "NODES")
mod.node_group = group

gn_input_set(mod, obj_socket.identifier, target)
gn_input_set(mod, float_socket.identifier, 2.5)
assert gn_input_get(mod, obj_socket.identifier) == target
assert abs(gn_input_get(mod, float_socket.identifier) - 2.5) < 1e-6
assert {obj_socket.identifier, float_socket.identifier} <= set(gn_input_identifiers(mod))

bpy.context.view_layer.update()
clone_root = duplication.duplicate_object_hierarchy(bpy.context, host)
clone_target = next(iter(clone_root.children))
clone_mod = clone_root.modifiers[0]
assert clone_target is not target and clone_target.name != target.name
assert gn_input_get(clone_mod, obj_socket.identifier) == clone_target, gn_input_get(clone_mod, obj_socket.identifier)
assert gn_input_get(mod, obj_socket.identifier) == target
print("GN_COMPAT_OK")
