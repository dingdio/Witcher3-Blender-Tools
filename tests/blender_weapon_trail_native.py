"""Run with Blender --background --factory-startup --python-exit-code 1 --python this_file."""
import math
from pathlib import Path
import sys

import bpy
from mathutils import Vector

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import witcher3_tools as addon
from witcher3_tools.animation import event_effects, events
from witcher3_tools.CR2W import dc_entity
from witcher3_tools.CR2W.common_blender import _get_redkit_depot_roots, redkit_repo_context, repo_file
from witcher3_tools.CR2W.cutscene_event_schema import build_event_data
from witcher3_tools.importers import import_particle, import_trail


def main():
    addon.register()
    scene = bpy.context.scene
    scene.render.fps = 30
    roots = _get_redkit_depot_roots()
    assert roots, 'Real trail test requires configured REDkit depots'
    print('GAME_ROOT', addon._get_prefs().witcher_game_path)
    print('BUNDLE_COUNT', len(list(Path(addon._get_prefs().witcher_game_path).glob('content/content*/bundles/*.bundle'))))
    source = r'dlc\ep1\data\items\weapons\swords\witcher_steel_swords\witcher_steel_viper_v2_sword_lvl1.w2ent'
    with redkit_repo_context(roots=roots):
        entity = dc_entity.load_bin_entity(repo_file(source))
    assert abs(entity.effectComponents['fx_point']['transform'].Z - 0.5) < 1e-6
    effect = next(e for e in entity.cookedEffects if e['name'] == 'light_trail_fx')
    track = effect['particle_systems'][0]
    assert len(track['curves']) == 3
    assert import_particle._evaluate_curve(track['curves'][1], 0.3) > 0.999
    assert import_particle._evaluate_curve(track['curves'][1], 1) == 0

    with redkit_repo_context(roots=roots):
        system, _, _ = import_particle._resolve_source(track['path'])
        emitter = system.emitters[0]
        params = import_trail.material_parameters(emitter.material)
    assert params['color']['Blue'] == 255 and params['color']['Red'] == 153
    assert params['diffuse'].endswith('default_trail_fast.xbm')
    assert emitter.drawer_type == 'CParticleDrawerTrail'
    owner = bpy.data.objects.new('Trail test character', bpy.data.armatures.new('Trail test rig'))
    scene.collection.objects.link(owner)
    owner.animation_data_create()
    action = bpy.data.actions.new('Trail test attack')
    event = build_event_data('W3', 'CExtAnimItemEffectEvent',
                            dict(eventName='fx_trail', startTime=0.25, effectName='light_trail_fx'))
    blood_event = build_event_data('W3', 'CExtAnimEvent', dict(eventName='BloodTrail', startTime=0.4))
    events.import_events(action, [event, blood_event], 30, end=48)
    action.use_frame_range = True
    action.frame_start, action.frame_end = 0, 48
    nla = owner.animation_data.nla_tracks.new()
    strip = nla.strips.new(action.name, 10, action)
    strip.action_frame_start, strip.action_frame_end = 0, 48
    strip.scale = 2
    slot = owner.data.witcherui_RigSettings.equipment_slots.add()
    slot.category, slot.hold_slot = 'steelsword', 'r_weapon'
    slot.resolved_repo_path, slot.equip_guid = source, 'test-equipment-guid'
    slot.is_loaded = slot.is_in_hold_slot = True
    weapon = bpy.data.objects.new('Trail test weapon', bpy.data.meshes.new('Trail test weapon'))
    scene.collection.objects.link(weapon)
    weapon['witcher_equip_guid'] = slot.equip_guid
    weapon['witcher_name'] = 'witcher_steel_viper_v2_sword_lvl1'
    for frame, x in ((0, 0), (110, 11)):
        weapon.location.x = x
        weapon.keyframe_insert('location', frame=frame)
    for curve in import_particle._action_fcurves(weapon.animation_data.action):
        for key in curve.keyframe_points:
            key.interpolation = 'LINEAR'
    scene.frame_set(19, subframe=0.25)
    baseline = [events.values(e) for e in action.witcher_events]
    objects, skipped, _ = event_effects.bake(bpy.context, owner, action, strip)
    assert not skipped and len(objects) == 1
    obj = objects[0]
    start, end = obj['witcher_trail_frame_start'], obj['witcher_trail_frame_end']
    assert abs(start - 25) < 1e-4, start  # offset + NLA scale * trigger frame
    assert abs(end - (25 + effect['length'] * 30)) < 1e-4  # effect lifetime is real seconds
    assert abs(scene.frame_current_final - 19.25) < 1e-6
    assert [events.values(e) for e in action.witcher_events] == baseline
    assert obj.parent is None, 'History must stay in world space'
    assert abs(obj['witcher_trail_width'] - 1.2) < 1e-5
    assert abs(obj['witcher_trail_lifetime'] - 0.32) < 1e-5
    center = (obj.data.vertices[0].co + obj.data.vertices[1].co) / 2
    assert (center - Vector((2.5, 0, 0.5))).length < 1e-4, center
    assert abs((obj.data.vertices[0].co - obj.data.vertices[1].co).length - 1.2) < 1e-5
    assert obj.data.attributes['w3_trail_birth'].data[0].value == 0
    mat = obj.data.materials[0]
    assert len([n for n in mat.node_tree.nodes if n.type == 'TEX_IMAGE' and n.image]) == 2
    assert not mat.node_tree.animation_data.drivers[0].driver.use_self
    for frame in (start+4, start-1, end+1, start+4):
        scene.frame_set(math.floor(frame), subframe=frame % 1)
        assert obj.hide_render == (frame < start or frame >= end), (frame, start, end, obj.hide_render,
            [(f.data_path, [(k.co[:], k.interpolation) for k in f.keyframe_points])
             for f in import_particle._action_fcurves(obj.animation_data.action)])
    objects2, _, _ = event_effects.bake(bpy.context, owner, action, strip)
    assert len(event_effects.previews(scene, owner, action)) == 1
    assert len(objects2) == 1
    blood_objects, skipped, _ = event_effects.bake(bpy.context, owner, action, strip, blood=True)
    assert not skipped and len(blood_objects) == 1
    blood = blood_objects[0]
    assert len(event_effects.previews(scene, owner, action)) == 2, 'Blood bake must preserve light trail'
    assert abs(blood['witcher_trail_frame_start'] - 34) < 1e-4
    assert blood['witcher_trail_samples'] == 20
    assert abs(blood['witcher_trail_lifetime'] - 0.5) < 1e-6
    assert abs(blood['witcher_blood_source_track_offset'] - 0.508628) < 1e-5
    assert abs(scene.frame_current_final - (start+4)) < 1e-4
    center = (blood.data.vertices[0].co + blood.data.vertices[1].co) / 2
    assert (center - Vector((3.4, 0, 0.9))).length < 1e-4, center
    assert abs((blood.data.vertices[0].co - blood.data.vertices[1].co).length - 1.5) < 1e-5
    assert abs((blood.data.vertices[-2].co - blood.data.vertices[-1].co).length - 0.55) < 1e-5
    assert blood.data.materials[0]['witcher_source_texture'].endswith('blood_trail_3.xbm')
    for curve in import_particle._action_fcurves(blood.data.shape_keys.animation_data.action):
        if curve.data_path.startswith('key_blocks'):
            assert all(k.interpolation == 'LINEAR' for k in curve.keyframe_points)
    for frame in (37, 33, 57, 37):
        scene.frame_set(frame)
        assert blood.hide_render == (frame < 34 or frame >= blood['witcher_trail_frame_end'])
        if frame == 37:
            evaluated = blood.evaluated_get(bpy.context.evaluated_depsgraph_get())
            center = (evaluated.data.vertices[0].co + evaluated.data.vertices[1].co) / 2
            assert abs(center.z - 0.87) < 0.001, center  # 0.9 + 0.5 * -6 * 0.1^2
    counts = (len(bpy.data.objects), len(bpy.data.meshes), len(bpy.data.materials), len(bpy.data.actions))
    event_effects.bake(bpy.context, owner, action, strip, blood=True)
    assert counts == (len(bpy.data.objects), len(bpy.data.meshes), len(bpy.data.materials), len(bpy.data.actions))
    event_effects.bake(bpy.context, owner, action, strip)
    assert len(event_effects.previews(scene, owner, action)) == 2, 'Light bake must preserve blood trail'
    assert [events.values(e) for e in action.witcher_events] == baseline
    slot.is_in_hold_slot = False
    from unittest import mock
    for blood_preview in (False, True):
        # An empty hand asks for a weapon; when none can be equipped the bake fails cleanly.
        with mock.patch.object(event_effects, 'hold_weapon', side_effect=ValueError('No weapon is equipped')) as hold:
            try:
                event_effects.bake(bpy.context, owner, action, strip, blood=blood_preview)
            except ValueError as exc:
                assert 'No weapon' in str(exc)
            else:
                raise AssertionError('Empty hand must not bake a trail')
            assert hold.call_args[0][2] == 'r_weapon'
    assert len(event_effects.previews(scene, owner, action)) == 2, 'Failed rebake must retain old preview'
    assert event_effects.clear(scene, owner, action) == 2
    slot.is_in_hold_slot = True
    slot.resolved_repo_path = r'items\weapons\swords\witcher_steel_swords\witcher_steel_bear_sword_lvl1.w2ent'
    weapon['witcher_name'] = 'steel_sword_02'
    blood_objects, skipped, _ = event_effects.bake(bpy.context, owner, action, strip, blood=True)
    assert not skipped and len(blood_objects) == 2
    red = next(o for o in blood_objects if o['witcher_trail_samples'] == 64)
    assert abs(red['witcher_trail_lifetime'] - 0.16) < 1e-6
    assert red.data.attributes.get('w3_trail_distance')
    assert red.data.materials[0]['witcher_source_material'] == import_trail.RED_BLOOD_MATERIAL
    assert event_effects.clear(scene, owner, action) == 2
    assert weapon.name in scene.objects
    print('WEAPON_TRAIL_NATIVE_OK', bpy.app.version_string)


if __name__ == '__main__':
    main()
