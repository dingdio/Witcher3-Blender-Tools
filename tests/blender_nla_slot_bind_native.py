import sys
from pathlib import Path

import bpy

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from witcher3_tools.animation.action_compat import bind_strip_action_slot, resolve_action_slot


def new_armature(name):
    obj = bpy.data.objects.new(name, bpy.data.armatures.new(name))
    bpy.context.scene.collection.objects.link(obj)
    obj.animation_data_create()
    return obj


def keyed_slot(action, name, data_path):
    slot = action.slots.new(id_type="OBJECT", name=name)
    layer = action.layers[0] if action.layers else action.layers.new("Layer")
    strip = layer.strips[0] if layer.strips else layer.strips.new(type="KEYFRAME")
    strip.channelbag(slot, ensure=True).fcurves.new(data_path, index=0).keyframe_points.insert(1, 0.0)
    return slot


arm = new_armature("fresh")
action = bpy.data.actions.new("single")
slot = keyed_slot(action, "only", "location")
nla_strip = arm.animation_data.nla_tracks.new().strips.new("s", 1, action)
assert nla_strip.action_slot == slot
bind_strip_action_slot(nla_strip, resolve_action_slot(action, target=arm, ensure=True))
assert nla_strip.action_slot == slot

print("NLA_SLOT_BIND_OK")
