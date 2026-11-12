"""Run in factory-startup Blender with configured REDkit depots."""
import math
from pathlib import Path
import sys
from types import SimpleNamespace

import bpy
from mathutils import Vector

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import witcher3_tools as addon
from witcher3_tools.controller import clips, effects, session, world
from witcher3_tools.CR2W.cutscene_event_schema import build_event_data


addon.register()
scene = bpy.context.scene
print('GAME_ROOT', addon._get_prefs().witcher_game_path)
print('BUNDLE_COUNT', len(list(Path(addon._get_prefs().witcher_game_path).glob('content/content*/bundles/*.bundle'))))
owner = bpy.data.objects.new('FX test player', bpy.data.armatures.new('FX test player'))
scene.collection.objects.link(owner)
slot = owner.data.witcherui_RigSettings.equipment_slots.add()
slot.category, slot.hold_slot, slot.equip_slot = 'steelsword', 'r_weapon', 'sword_back'
slot.resolved_repo_path = r'dlc\ep1\data\items\weapons\swords\witcher_steel_swords\witcher_steel_viper_v2_sword_lvl1.w2ent'
slot.equip_guid = 'player-fx-test'
slot.is_loaded, slot.weapon = True, True
back, hand = [bpy.data.objects.new(name, None) for name in ('Back slot', 'Hand slot')]
for obj, name in ((back, 'sword_back'), (hand, 'r_weapon')):
    scene.collection.objects.link(obj)
    obj['witcher_owner_armature'] = owner.name
    obj['witcher_slot_name'] = name
weapon = bpy.data.objects.new('FX test sword', bpy.data.meshes.new('FX test sword'))
scene.collection.objects.link(weapon)
weapon['witcher_equip_guid'] = slot.equip_guid
weapon['witcher_name'] = 'witcher_steel_viper_v2_sword_lvl1'
weapon.parent = hand
weapons = session.find_weapons(owner)
assert weapons['steelsword']['objects'] == [weapon], 'Already held swords must be found'
entries = [build_event_data('W3', 'CExtAnimItemEffectEvent',
                           dict(eventName='fx_trail', startTime=0.25, effectName='light_trail_fx')),
           build_event_data('W3', 'CExtAnimEvent', dict(eventName='BloodTrail', startTime=0.4))]
events = clips.effect_events(entries)
assert events[0] == (0.25, 'light_trail_fx', 'r_weapon', False, None, False)
assert events[1][-1] is True
counts = (len(bpy.data.objects), len(bpy.data.meshes), len(bpy.data.materials), len(bpy.data.actions))
fx = effects.Effects(scene, owner, weapons, [SimpleNamespace(effects=events)])
light = fx.ribbons[('steelsword', 'light_trail_fx', False)][0]
blood = fx.ribbons[('steelsword', 'default_blood_trail', True)][0]
frame = scene.frame_current
for i in range(91):
    t = i / 60
    weapon.location = (t * 4, math.sin(t * 4), 2)
    bpy.context.view_layer.update()
    for event in events:
        if abs(t - event[0]) < 1e-6:
            fx.trigger('steelsword', t, event, False)
    fx.update(t)
    if i == 18:
        assert not light.obj.hide_viewport and blood.obj.hide_viewport
        assert len(light.points) > 1 and (light.points[-1]-light.points[0]).length > 0.1
        assert abs(light.clock.default_value - 0.05) < 1e-6
        assert not light.obj.data.materials[0].node_tree.animation_data
    if i == 60:
        assert light.obj.hide_viewport and blood.obj.hide_viewport
assert scene.frame_current == frame, 'Live effects must not advance the timeline'
fx.trigger('steelsword', 1.5, events[1], True)
for i in range(91, 106):
    t = i / 60
    weapon.location.x = t * 4
    bpy.context.view_layer.update()
    fx.update(t)
assert not blood.obj.hide_viewport and len(blood.points) == 20
first = blood.points[0].copy()
center = (blood.obj.data.vertices[0].co + blood.obj.data.vertices[1].co) / 2
assert (center - first - Vector((0, 0, -3 * 0.25 ** 2))).length < 1e-5
assert abs((blood.obj.data.vertices[0].co-blood.obj.data.vertices[1].co).length - 1.5) < 1e-5
fx.stop()
assert all(r.obj.hide_viewport for rs in fx.ribbons.values() for r in rs)
fx.close()
assert counts == (len(bpy.data.objects), len(bpy.data.meshes), len(bpy.data.materials), len(bpy.data.actions))

# Bear steel adds its own additive red ribbon to the ordinary falling blood.
slot.resolved_repo_path = r'items\weapons\swords\witcher_steel_swords\witcher_steel_bear_sword_lvl1.w2ent'
weapon['witcher_name'] = 'steel_sword_02'
fx = effects.Effects(scene, owner, weapons, [SimpleNamespace(effects=events)])
bloods = fx.ribbons[('steelsword', 'default_blood_trail', True)]
assert len(bloods) == 2
red = next(r for r in bloods if 'Trail Time' in r.nodes)
assert len(red.times) == 64 and abs(red.settings.lifetime - 0.16) < 1e-6
fx.trigger('steelsword', 0, events[1], True)
for i in range(1, 16):
    weapon.location.x = i / 15
    bpy.context.view_layer.update()
    fx.update(i / 60)
assert all(not r.obj.hide_viewport and len(r.points) > 10 for r in bloods)
assert red.nodes['Head Distance'].outputs[0].default_value > 0
fx.close()
assert counts == (len(bpy.data.objects), len(bpy.data.meshes), len(bpy.data.materials), len(bpy.data.actions))

# Reference environments and VFX must never become ground or camera blockers.
for obj in list(scene.objects):
    if obj.type == 'MESH':
        obj.hide_set(True)
for z, marker in ((0, None), (200, 'witcher_environment_preview'), (4, 'witcher_player_effect'),
                  (3, 'witcher_trail_version'), (2, 'witcher_world_water_object')):
    mesh = bpy.data.meshes.new('Ground test')
    mesh.from_pydata([(-10,-10,z),(10,-10,z),(10,10,z),(-10,10,z)], [], [(0,1,2,3)])
    obj = bpy.data.objects.new('Ground test', mesh)
    scene.collection.objects.link(obj)
    if marker:
        obj[marker] = True
        assert world._skip_object(obj)
bpy.context.view_layer.update()
assert abs(world._ground_below(owner)) < 1e-6
print('PLAYER_EFFECTS_AND_GROUND_OK', bpy.app.version_string)
