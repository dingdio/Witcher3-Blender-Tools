"""Run with Blender --background --factory-startup --python-exit-code 1 --python this_file."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import witcher3_tools as addon
from witcher3_tools.animation import events, event_preview
from witcher3_tools.animation.action_compat import assign_action
from witcher3_tools.CR2W import anims_builder, cr2w_writer, dc_anims, w2anims_inject
from witcher3_tools.CR2W.cutscene_event_schema import build_event_data
from witcher3_tools.ui import ui_anim_events


def spec(name, entries):
    return dict(action_name=name, num_frames=31, dt=1 / 30, fps=30,
                bones=[dict(name='Root', pos_frames=[(0, 0, 0)] * 31,
                            rot_frames=[(0, 0, 0, 1)] * 31, scale_frames=[(1, 1, 1)] * 31)],
                entry_events=entries)


class EventTests(unittest.TestCase):
    def setUp(self):
        event_preview.stop()
        for obj in list(bpy.data.objects):
            bpy.data.objects.remove(obj, do_unlink=True)
        for action in list(bpy.data.actions):
            bpy.data.actions.remove(action)
        bpy.context.scene.render.fps = 30
        bpy.context.scene.render.fps = 30
        bpy.ops.witcher.animation_event_clear_filters()
        self.owner = bpy.data.objects.new('Actor', bpy.data.armatures.new('Actor'))
        bpy.context.scene.collection.objects.link(self.owner)
        bpy.context.view_layer.objects.active = self.owner
        self.owner.select_set(True)
        bpy.ops.object.mode_set(mode='EDIT')
        bone = self.owner.data.edit_bones.new('Root')
        bone.head, bone.tail = (0, 0, 0), (0, 1, 0)
        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.context.scene.witcher_main_armature = self.owner
        self.action = bpy.data.actions.new('Clip')
        assign_action(self.owner, self.action)
        for frame in (0, 30):
            self.owner.keyframe_insert('location', frame=frame)
        sound = build_event_data('W3', 'CExtAnimSoundEvent', dict(
            eventName='Sword', startTime=0.1234567, animationName='Clip', trackName='Audio',
            soundEventName='sword_swing', bone='r_weapon', maxDistance=35.0))
        duration = build_event_data('W3', 'CExtAnimDurationEvent', dict(
            eventName='Window', startTime=0.2, animationName='Clip', trackName='Combat', duration=0.5))
        events.import_events(self.action, [sound, duration], 30, end=30)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def tearDown(self):
        event_preview.stop()

    def write_read(self, entries):
        path = Path(self.tmp.name) / 'events.w2anims'
        cr2w_writer.write_w2anims(anims_builder.build_w2anims([spec('Clip', entries)]), str(path))
        return dc_anims.load_bin_anims_single(str(path), 'Clip').animations[0].entries

    def test_track_view_zoom_pan_and_reset(self):
        from witcher3_tools.animation import event_tracks
        scene = bpy.context.scene
        scene.witcher_tracks_zoom = 2
        scene.witcher_tracks_pan = 5
        self.assertEqual(event_tracks.view_range(self.action, scene), (5, 20))
        bpy.ops.witcher.event_tracks_frame_all()
        self.assertEqual(event_tracks.view_range(self.action, scene), (0, 30))
        scene.witcher_tracks_zoom = 0.5
        self.assertEqual(event_tracks.view_range(self.action, scene), (0, 60))
        bpy.ops.witcher.event_tracks_frame_all()

    def test_default_tracks_are_display_only_until_assigned(self):
        for event in self.action.witcher_events:
            event.track_name = ''
        before = events.export_events(self.action, 30, 0, 30)
        self.assertEqual([n for n, _ in events.track_rows(self.action)], ['__Default', 'Audio', 'Combat'])
        self.assertEqual([n for n, _ in events.track_rows(self.action, True, show_empty=False)], ['__Default', 'SoundEvent', 'DurationEvent'])
        self.assertEqual(events.export_events(self.action, 30, 0, 30), before)
        self.assertEqual(self.action.witcher_events[0].track_display_name, '__Default')
        bpy.context.scene.witcher_event_track_filter = '__Default'
        self.assertTrue(ui_anim_events.matches_filter(self.action.witcher_events[0], bpy.context.scene))
        bpy.ops.witcher.event_track_names(group_by='CLASS')
        self.assertEqual(self.action.witcher_events[0].track_name, 'SoundEvent')
        self.action.witcher_events[0].track_display_name = '__Default'
        self.assertEqual(self.action.witcher_events[0].track_name, '')

    def test_empty_default_track_stays_visible_above_selected_rows(self):
        from witcher3_tools.animation import event_tracks
        self.action.witcher_events.clear()
        self.assertEqual(events.track_rows(self.action, show_empty=False), [('__Default', [])])
        for i in range(10):
            event = self.action.witcher_events.add()
            event.track_name = f'Track{i}'
            event.frame = i
        rows = events.track_rows(self.action)
        shown = event_tracks.visible_rows(rows, 9, 3)
        self.assertEqual([name for name, _ in shown], ['__Default', 'Track8', 'Track9'])

    def test_blank_tracks_do_not_fabricate_events_and_can_be_filled(self):
        from witcher3_tools.animation import event_tracks
        before = events.export_events(self.action, 30, 0, 30)
        self.assertEqual(bpy.ops.witcher.event_track_add(track_name='  Effects  '), {'FINISHED'})
        self.assertEqual(self.action.witcher_event_tracks[-1].name, 'Effects')
        self.assertEqual(bpy.context.scene.witcher_event_track_filter, 'Effects')
        self.assertIn(('Effects', []), events.track_rows(self.action))
        self.assertIn('Effects', ui_anim_events.track_names(bpy.context.scene, bpy.context, ''))
        self.assertEqual(events.export_events(self.action, 30, 0, 30), before)
        self.assertFalse(events.modified(self.action, 0, 30))
        for name in ('', '  ', '__Default', 'Audio', 'Effects'):
            with self.assertRaises(ValueError):
                events.add_track(self.action, name)
        target = bpy.data.actions.new('Copy')
        events.copy_events(self.action, target)
        self.assertEqual([t.name for t in target.witcher_event_tracks], ['__Default', 'Audio', 'Combat', 'Effects'])
        self.assertEqual(events.export_events(target, 30, 0, 30)[0]['raw_fields']['trackName'], 'Audio')
        for i in range(10):
            events.add_track(self.action, f'Empty {i}')
        shown = event_tracks.visible_rows(events.track_rows(self.action), 0, 3, 'Empty 9')
        self.assertEqual([name for name, _ in shown], ['__Default', 'Empty 8', 'Empty 9'])
        bpy.ops.witcher.animation_event_add(event_name='Spark', track_name='Effects')
        self.assertEqual(len([n for n, _ in events.track_rows(self.action) if n == 'Effects']), 1)
        bpy.ops.witcher.animation_event_remove()
        self.assertIn(('Effects', []), events.track_rows(self.action))

    def test_delete_track_moves_events_and_keeps_linked_names_separate(self):
        linked = self.action.witcher_events.add()
        for name, value in events.values(self.action.witcher_events[0]).items():
            setattr(linked, name, value)
        linked.source_file = 'sounds/shared.w2animev'
        before = [events.values(e) for e in self.action.witcher_events]
        bpy.context.scene.witcher_event_track_filter = 'Audio'
        with mock.patch.object(event_preview, 'stop') as stop:
            self.assertEqual(bpy.ops.witcher.event_track_delete(track=json.dumps(['', 'Audio'])), {'FINISHED'})
            stop.assert_called_once()
        self.assertEqual(bpy.context.scene.witcher_event_track_filter, '')
        self.assertEqual([e.track_name for e in self.action.witcher_events], ['', 'Combat', 'Audio'])
        before[0]['track_name'] = ''
        self.assertEqual([events.values(e) for e in self.action.witcher_events], before)
        self.assertEqual([e.track_name for e in self.write_read(events.export_events(self.action, 30, 0, 30))], ['', 'Combat', 'Audio'])
        with self.assertRaisesRegex(ValueError, 'permanent'):
            events.delete_track(self.action, '__Default', delete_events=True)
        with self.assertRaisesRegex(ValueError, 'no longer exists'):
            events.delete_track(self.action, 'Missing', delete_events=True)

    def test_delete_track_events_and_empty_track(self):
        linked = self.action.witcher_events.add()
        linked.track_name, linked.source_file = 'Audio', 'sounds/shared.w2animev'
        self.action.witcher_event_index = 1
        self.assertEqual(bpy.ops.witcher.event_track_delete(track=json.dumps(['', 'Audio']), event_handling='DELETE'), {'FINISHED'})
        self.assertEqual([e.track_name for e in self.action.witcher_events], ['Combat', 'Audio'])
        self.assertEqual(self.action.witcher_event_index, 0)
        self.assertEqual(bpy.ops.witcher.event_track_delete(track=json.dumps(['sounds/shared.w2animev', 'Audio']), event_handling='DELETE'), {'FINISHED'})
        self.assertEqual(len(self.action.witcher_events), 1)
        bpy.ops.witcher.event_track_add(track_name='Empty')
        self.assertEqual(bpy.ops.witcher.event_track_delete(track=json.dumps(['', 'Empty'])), {'FINISHED'})
        self.assertNotIn(('', 'Empty'), events.track_keys(self.action))
        unknown = bpy.data.actions.new('Unknown events')
        events.add_track(unknown, 'Empty')
        self.assertIsNone(events.export_events(unknown, 30, 0, 30))
        events.delete_track(unknown, 'Empty')
        self.assertFalse(unknown.witcher_events_known)

    def test_auto_create_tracks_preserves_names_and_exports_assignments(self):
        for event in self.action.witcher_events:
            event.track_name = '__Default'
        named = self.action.witcher_events.add()
        named.event_name, named.track_name = 'Keep', 'Authored'
        unnamed = self.action.witcher_events.add()
        unnamed.event_name, unnamed.event_type = '', 'CExtAnimOnSlopeEvent'
        baseline = self.action.get('witcher_event_baseline')
        before = [events.values(e) for e in self.action.witcher_events]
        bpy.context.scene.witcher_event_track_filter = '__Default'
        self.assertEqual(bpy.ops.witcher.event_track_names(group_by='NAME', scope='DEFAULT'), {'FINISHED'})
        self.assertEqual([e.track_name for e in self.action.witcher_events], ['Sword', 'Window', 'Authored', 'OnSlopeEvent'])
        self.assertEqual(bpy.context.scene.witcher_event_track_filter, '')
        self.assertEqual(events.create_tracks(self.action), 0)
        for old, event in zip(before, self.action.witcher_events):
            self.assertEqual({k: v for k, v in events.values(event).items() if k != 'track_name'},
                             {k: v for k, v in old.items() if k != 'track_name'})
        self.assertEqual(self.action.get('witcher_event_baseline'), baseline)
        self.assertTrue(events.modified(self.action, 0, 30))
        parsed = self.write_read(events.export_events(self.action, 30, 0, 30))
        self.assertEqual([e.track_name for e in parsed], ['Sword', 'Window', 'Authored', 'OnSlopeEvent'])

    def test_auto_create_can_regroup_existing_tracks_repeatedly(self):
        before = [events.values(e) for e in self.action.witcher_events]
        events.add_track(self.action, 'Keep empty')
        for grouping, names in (('NAME', ['Sword', 'Window']), ('CLASS', ['SoundEvent', 'DurationEvent']),
                                ('NAME', ['Sword', 'Window'])):
            with self.subTest(grouping=grouping):
                self.assertTrue(bpy.ops.witcher.event_track_names.poll())
                self.assertEqual(bpy.ops.witcher.event_track_names(group_by=grouping), {'FINISHED'})
                self.assertEqual([e.track_name for e in self.action.witcher_events], names)
                self.assertIn(('Keep empty', []), events.track_rows(self.action))
                self.assertEqual([n for n, _ in events.track_rows(self.action, show_empty=False)], ['__Default'] + names)
                for old, event in zip(before, self.action.witcher_events):
                    self.assertEqual({k: v for k, v in old.items() if k != 'track_name'},
                                     {k: v for k, v in events.values(event).items() if k != 'track_name'})
        self.assertEqual(events.create_tracks(self.action, 'NAME', 'UNLOCKED'), 0)

    def test_track_locks_protect_sources_destinations_and_linked_files(self):
        self.assertEqual(bpy.ops.witcher.event_track_lock(track=json.dumps(['', 'Audio']), locked=True), {'FINISHED'})
        self.assertIn(('', 'Audio'), events.locked_tracks(self.action))
        linked = self.action.witcher_events.add()
        for name, value in events.values(self.action.witcher_events[0]).items():
            setattr(linked, name, value)
        linked.source_file = 'sounds/shared.w2animev'
        events.add_track(self.action, 'Window')
        events.set_track_locked(self.action, 'Window')
        assignments, protected = events.track_assignments(self.action, 'NAME', 'UNLOCKED')
        self.assertEqual(assignments, [(2, 'Sword')])
        self.assertEqual(protected, 2)
        bpy.ops.witcher.event_track_names(group_by='NAME')
        self.assertEqual([e.track_name for e in self.action.witcher_events], ['Audio', 'Combat', 'Sword'])
        self.assertEqual(bpy.ops.witcher.event_track_lock(track=json.dumps(['', 'Audio']), locked=False), {'FINISHED'})
        events.set_track_locked(self.action, 'Window', locked=False)
        self.assertIn(('Window', []), events.track_rows(self.action))
        self.assertNotIn(('', 'Audio'), events.locked_tracks(self.action))
        self.assertIn('Audio', [t.name for t in self.action.witcher_event_tracks])
        bpy.ops.witcher.event_track_names(group_by='CLASS')
        self.assertEqual([e.track_name for e in self.action.witcher_events], ['SoundEvent', 'DurationEvent', 'SoundEvent'])
        # A protected default track stays available for manual authoring.
        events.set_track_locked(self.action, '__Default')
        self.action.witcher_events[0].track_name = ''
        self.assertEqual(events.create_tracks(self.action, 'NAME', 'DEFAULT'), 0)
        events.set_track_locked(self.action, 'SoundEvent', 'sounds/shared.w2animev')
        copy = bpy.data.actions.new('Copy with locks')
        events.copy_events(self.action, copy)
        self.assertEqual(events.locked_tracks(copy), {('', '__Default'), ('sounds/shared.w2animev', 'SoundEvent')})
        self.assertIn(('Window', []), events.track_rows(copy))
        events.delete_track(self.action, 'SoundEvent', 'sounds/shared.w2animev', delete_events=True)
        self.assertEqual(events.locked_tracks(self.action), {('', '__Default')})

    def test_track_icons_distinguish_mixed_linked_and_default_rows(self):
        from witcher3_tools.animation.event_tracks import track_icon
        sound, duration = self.action.witcher_events
        self.assertEqual(track_icon('__Default', []), 'PINNED')
        self.assertEqual(track_icon('__Default', [(0, sound)]), 'SOUND')
        self.assertEqual(track_icon('Audio', [(0, sound)]), 'SOUND')
        self.assertEqual(track_icon('Mixed', [(0, sound), (1, duration)]), 'NLA')
        sound.source_file = 'sounds/test.w2animev'
        self.assertEqual(track_icon('Audio', [(0, sound)]), 'SOUND')
        self.assertEqual(track_icon('__Default', [(0, sound)]), 'SOUND')

    def test_track_order_survives_edits_and_can_be_changed_without_export_changes(self):
        def names(**kwargs):
            return [name for name, _ in events.track_rows(self.action, **kwargs)]

        self.assertTrue(bpy.context.scene.witcher_tracks_show_empty)
        self.action.witcher_events[0].frame = 25
        self.assertEqual(names(), ['__Default', 'Audio', 'Combat'])
        self.action.witcher_events[0].track_name = 'Combat'
        self.assertEqual(names(), ['__Default', 'Audio', 'Combat'])
        self.assertEqual(names(show_empty=False), ['__Default', 'Combat'])
        before = events.export_events(self.action, 30, 0, 30)
        events.add_track(self.action, 'Blank')
        self.action.witcher_event_track_index = 3
        self.assertEqual(bpy.ops.witcher.event_track_move(direction='TOP'), {'FINISHED'})
        self.assertEqual(names(), ['__Default', 'Blank', 'Audio', 'Combat'])
        bpy.ops.witcher.event_track_move(direction='UP')
        self.assertEqual(names()[1], 'Blank')
        bpy.ops.witcher.event_track_move(direction='DOWN')
        self.assertEqual(names(), ['__Default', 'Audio', 'Blank', 'Combat'])
        bpy.ops.witcher.event_track_move(direction='BOTTOM')
        self.assertEqual(names(), ['__Default', 'Audio', 'Combat', 'Blank'])
        with self.assertRaisesRegex(ValueError, 'always stays at the top'):
            events.move_track(self.action, '', '__Default', 'BOTTOM')
        self.assertEqual(events.export_events(self.action, 30, 0, 30), before)
        bpy.context.scene.witcher_event_track_filter = 'Combat'
        target = bpy.data.actions.new('Copy with track order')
        events.copy_events(self.action, target)
        self.assertEqual(bpy.context.scene.witcher_event_track_filter, 'Combat')
        bpy.ops.witcher.event_track_delete(track=json.dumps(['', 'Blank']))
        self.assertEqual(bpy.context.scene.witcher_event_track_filter, 'Combat')

    def test_point_event_highlight_has_a_visible_window_without_extending_duration_events(self):
        from witcher3_tools.animation.event_tracks import event_active
        event = self.action.witcher_events[0]
        event.frame, event.duration = 5.25, 0
        for frame, active in [(5, False), (5.25, True), (6, True), (8, True), (8.25, False)]:
            self.assertEqual(event_active(event, frame, 30), active)
        self.assertTrue(event_active(event, 10, 60))
        self.assertFalse(event_active(event, 11.25, 60))
        event.duration = 0.5
        self.assertTrue(event_active(event, 5.5, 30))
        self.assertFalse(event_active(event, 5.75, 30))

    def test_shared_icon_geometry_is_valid_for_native_ui(self):
        from witcher3_tools.animation import event_tracks
        try:
            for _ in range(3):
                for kind in event_tracks._ICON_PATHS:
                    coords, vertices = event_tracks.icon_geometry(kind)
                    self.assertEqual(len(coords), len(vertices) * 2)
                    self.assertEqual(len(vertices) % 3, 0)
                    value = event_tracks.icon_value(kind)
                    self.assertGreater(value, 0)
                    self.assertEqual(event_tracks.icon_value(kind), value)
                event_tracks.unregister()
                self.assertEqual(event_tracks._ui_icons, {})
        finally:
            event_tracks.register()

    def test_event_marker_icons_use_event_type_independent_of_track(self):
        from witcher3_tools.animation.event_tracks import event_icon, _ICON_PATHS
        event = self.action.witcher_events[0]
        event.track_name = '__Default'
        event.source_file = 'sounds/test.w2animev'
        event.sound_event = ''
        for kind, duration, expected in (
            ('CExtAnimSoundEvent', 0, 'SOUND'),
            ('CExtAnimFootstepEvent', 0, 'SOUND'),
            ('CExtAnimEffectDurationEvent', 10, 'EFFECT'),
            ('CExtAnimItemSyncEvent', 0, 'SYNC'),
            ('CEASEnumEvent', 10, 'SCRIPT'),
            ('CExtAnimDurationEvent', 0, 'TIME'),
            ('CustomEvent', 10, 'KEY_HLT'),
            ('CustomEvent', 0, 'KEY_HLT'),
        ):
            with self.subTest(kind=kind):
                event.event_type, event.duration = kind, duration
                self.assertEqual(event_icon(event), expected)
                self.assertIn(expected, _ICON_PATHS)
        event.sound_event = 'sword_swing'
        self.assertEqual(event_icon(event), 'SOUND')

    def test_new_clip_and_export_scope(self):
        from witcher3_tools.ui import ui_anim_authoring
        from witcher3_tools.exporters import export_anims
        scene = bpy.context.scene
        scene.witcher_anim_export_set.clear()
        track = self.owner.animation_data.nla_tracks.new()
        track.strips.new('Existing clip', 1, self.action)
        self.assertEqual(bpy.ops.witcher.animation_new_clip(clip_name='From scratch', start=10, frames=31, fps=24), {'FINISHED'})
        new = self.owner.animation_data.action
        self.assertEqual(tuple(new.frame_range), (10, 40))
        self.assertTrue(new.witcher_events_known)
        self.assertEqual(len(new.witcher_events), 0)
        self.assertEqual([t.name for t in new.witcher_event_tracks], ['__Default'])
        self.assertIn(self.action.name, bpy.data.actions)
        self.assertEqual(len(self.owner.animation_data.nla_tracks), 1)
        self.assertFalse(self.owner.animation_data.use_nla)
        self.assertEqual(scene.render.fps, 24)
        targets = ui_anim_authoring.export_targets(bpy.context)
        self.assertEqual(targets[0][1], new)
        specs, skipped = export_anims.collect_set_specs(self.owner, list(scene.witcher_anim_export_set))
        self.assertEqual(skipped, [])
        self.assertEqual(specs[0]['num_frames'], 31)
        self.assertEqual(specs[0]['entry_events'], [])
        scene.witcher_anim_export_set.clear()
        assign_action(self.owner, self.action)
        scene.frame_start, scene.frame_end = 0, 5
        self.assertEqual(ui_anim_authoring.export_targets(bpy.context)[0][2], (0, 5))
        self.assertEqual(ui_anim_authoring.event_counts(self.action, 0, 5), (1, 1))

    def test_typed_event_fields_preserve_and_author_values(self):
        from witcher3_tools.ui import ui_anim_event_fields as typed
        bpy.types.Scene.test_event_editors = bpy.props.CollectionProperty(type=typed.WITCH_PG_EventFieldEditor)
        try:
            editors = bpy.context.scene.test_event_editors
            for kind, value in [('Bool', True), ('Float', 0.123456789), ('Uint32', 2147483649),
                                ('EAnimEffectAction', 'FutureEnumOption'),
                                ('SEnumVariant', {'enumType': 'ERotationRate'}),
                                ('EulerAngles', {'Pitch': 3.25, 'Yaw': 8.0, 'Roll': 0.0})]:
                field = SimpleNamespace(type_text=kind, is_set=True, value_text=ui_anim_events.field_text(value))
                editor = editors.add()
                typed.prepare(field, value, editor)
                before = field.value_text
                typed.sync(field, editor)
                self.assertEqual(field.value_text, before)
            field = SimpleNamespace(type_text='SEnumVariant', is_set=False, value_text='<unset>')
            editor = editors.add()
            typed.prepare(field, None, editor)
            field.is_set = True
            editor.enum_choice = 'RR_90'
            self.assertEqual(editor.int_value, 90)
            typed.sync(field, editor)
            self.assertEqual(json.loads(field.value_text), {'enumType': 'ERotationRate', 'enumValue': 90})
            editor.int_value = 60
            self.assertEqual(editor.enum_choice, 'RR_60')
            field.is_set = False
            typed.sync(field, editor)
            self.assertEqual(field.value_text, '<unset>')
            field = SimpleNamespace(type_text='Uint8', is_set=True, value_text='7')
            editor = editors.add()
            typed.prepare(field, 7, editor)
            editor.int_value = 255
            typed.sync(field, editor)
            self.assertEqual(field.value_text, '255')
            editor.int_value = 256
            with self.assertRaises(ValueError):
                typed.sync(field, editor)
        finally:
            del bpy.types.Scene.test_event_editors

    def test_export_summary_matches_scope_and_reports_issues(self):
        from witcher3_tools.ui import ui_anim_authoring as authoring
        bpy.types.Scene.test_export_summary = bpy.props.CollectionProperty(type=authoring.WITCH_PG_ExportClipSummary)
        scene = bpy.context.scene
        try:
            scene.witcher_anim_export_set.clear()
            summary = SimpleNamespace(clips=scene.test_export_summary, fallback_motion=False)
            scene.witcher_w3_anim_source = 'ACTION'
            scene.frame_start, scene.frame_end = 0, 5
            self.action.witcher_events[0].track_name = ''
            authoring.WITCH_OT_AnimationExportSummary.collect(summary, bpy.context)
            row = summary.clips[0]
            self.assertEqual((row.first, row.last, row.events, row.sounds, row.unnamed, row.excluded), (0, 5, 1, 1, 1, 1))
            self.assertEqual(row.motion, 'Off')
            self.assertFalse(row.problem)
            entry = scene.witcher_anim_export_set.add()
            entry.action_name = self.action.name
            entry.include_motion_extraction = True
            entry = scene.witcher_anim_export_set.add()
            entry.action_name = 'Missing'
            authoring.WITCH_OT_AnimationExportSummary.collect(summary, bpy.context)
            self.assertEqual((summary.clips[0].events, summary.clips[0].last), (2, 30))
            self.assertEqual(summary.clips[0].motion, 'No Trajectory bone')
            self.assertEqual(summary.clips[1].problem, 'Action not found')
            self.action.witcher_events[0].payload = '{invalid json'
            authoring.WITCH_OT_AnimationExportSummary.collect(summary, bpy.context)
            self.assertTrue(summary.clips[0].problem)
            self.action.witcher_events_known = False
            self.assertEqual(authoring.event_counts(self.action, 0, 30), (0, 0))
        finally:
            scene.witcher_anim_export_set.clear()
            del bpy.types.Scene.test_export_summary

    def test_native_fields_and_editing(self):
        self.assertFalse(events.modified(self.action, 0, 30))
        self.assertTrue(events.modified(self.action, 1, 30))
        copied = self.action.copy()
        self.assertEqual([events.values(e) for e in copied.witcher_events],
                         [events.values(e) for e in self.action.witcher_events])
        imported = self.write_read(events.export_events(self.action, 30, 0, 30))
        self.assertEqual(len(imported), 2)
        self.assertEqual(imported[0].raw_fields['soundEventName'], 'sword_swing')
        self.assertEqual(imported[0].raw_fields['bone'], 'r_weapon')
        self.assertAlmostEqual(imported[0].raw_fields['maxDistance'], 35)
        self.assertAlmostEqual(imported[1].duration, 0.5)
        self.action.witcher_events[0].frame = 15
        self.action.witcher_events[0].sound_event = 'sword_hit'
        self.action.witcher_events.remove(1)
        self.assertTrue(events.modified(self.action, 0, 30))
        edited = self.write_read(events.export_events(self.action, 30, 0, 30))
        self.assertEqual(len(edited), 1)
        self.assertAlmostEqual(edited[0].start_time, 0.5)
        self.assertEqual(edited[0].raw_fields['soundEventName'], 'sword_hit')

    def test_authored_enum_event_without_import_type_hints(self):
        self.action.witcher_events.clear()
        bpy.ops.witcher.animation_event_add(event_type='CEASEnumEvent', event_name='Rotate')
        item = self.action.witcher_events[0]
        editor = SimpleNamespace(kind='SEnumVariant', enum_type='ERotationRate', int_value=90, snapshot='')
        field = SimpleNamespace(field_name='enumVariant', type_text='SEnumVariant', is_set=True, value_text='<unset>')
        dialog = SimpleNamespace(event_class='CEASEnumEvent', original='{}',
                                 class_fields=[field], editors={'enumVariant': editor})
        self.assertEqual(ui_anim_events.WITCH_OT_AnimationEventFields.execute(dialog, bpy.context), {'FINISHED'})
        self.assertNotIn('field_types', json.loads(item.payload))
        written = self.write_read(events.export_events(self.action, 30, 0, 30))
        self.assertEqual(written[0].raw_fields['enumVariant'],
                         {'enumType': 'ERotationRate', 'enumValue': 90})

    def test_authored_struct_events_without_import_type_hints(self):
        self.action.witcher_events.clear()
        bpy.ops.witcher.animation_event_add(event_type='CPreAttackEvent', event_name='Attack')
        item = self.action.witcher_events[0]
        data = json.loads(item.payload)
        self.assertNotIn('field_types', data)
        self.assertEqual(data['raw_fields']['data']['attackName'], 'attack_light')
        data['raw_fields']['data'].update(rangeName='slash_short', swingDir=2)
        item.payload = json.dumps(data)
        multi = dict(event_type='CEASMultiValueSimpleEvent', source_game='W3', raw_fields=dict(
            eventName='Multi', startTime=0.1, animationName='Clip', trackName='', properties={
                'bools': [True, False, True], 'names': ['a', 'b'],
                'enums': [{'enumType': 'ERotationRate', 'enumValue': 90}, {'enumType': 'ESide', 'enumValue': 1}]}))
        single = dict(multi, raw_fields=dict(multi['raw_fields'], properties={'enums': [{'enumType': 'ESide', 'enumValue': 1}]}))
        written = self.write_read(events.export_events(self.action, 30, 0, 30) + [multi, single])
        attack = written[0].raw_fields['data']
        self.assertEqual((attack['rangeName'], attack['swingDir'], attack['Can_Parry_Attack']), ('slash_short', 2, True))
        self.assertEqual(written[1].raw_fields['properties'], multi['raw_fields']['properties'])
        self.assertEqual(written[2].raw_fields['properties'], single['raw_fields']['properties'])
        # The hints captured on reimport describe the same event.
        again = self.write_read([dict(event_type=e.type_name, source_game='W3', raw_fields=e.raw_fields,
                                      field_types=e.field_types) for e in written])
        self.assertEqual([e.raw_fields for e in again], [e.raw_fields for e in written])

    def test_small_bool_arrays_curves_and_soft_paths_roundtrip(self):
        def multi(bools):
            return dict(event_type='CEASMultiValueSimpleEvent', source_game='W3', raw_fields=dict(
                eventName='Multi', startTime=0.1, animationName='Clip', trackName='', properties={'bools': bools}))
        effect = dict(event_type='CExtAnimCutsceneEffectEvent', source_game='W3', raw_fields=dict(
            eventName='Fx', startTime=0.2, animationName='Clip', trackName='', template='fx/real_template.w2ent'))
        self.action.witcher_events.clear()
        bpy.ops.witcher.animation_event_add(event_type='CExtAnimMorphEvent', event_name='Morph')
        morph = events.export_events(self.action, 30, 0, 30)
        self.assertNotIn('field_types', json.loads(self.action.witcher_events[0].payload))
        written = self.write_read([multi([True]), multi([True, False]), multi([False, True, True, False]), effect] + morph)
        self.assertEqual([e.raw_fields['properties']['bools'] for e in written[:3]], [[1], [1, 0], [0, 1, 1, 0]])
        self.assertEqual(written[3].raw_fields['template'], 'fx/real_template.w2ent')
        curve = written[4].raw_fields['curve']
        self.assertEqual((curve['value type'], curve['type'], len(curve['Curve Values'])), ('CVT_Float', 'CT_Segmented', 2))
        self.assertAlmostEqual(curve['Curve Values'][1]['lue'], 1.0)

    def test_events_just_before_the_clip_survive_and_export_name_is_used(self):
        early = build_event_data('W3', 'CExtAnimEvent', dict(eventName='Early', startTime=-0.001, animationName='Clip', trackName=''))
        late = build_event_data('W3', 'CExtAnimEvent', dict(eventName='Late', startTime=2.0, animationName='Clip', trackName=''))
        events.import_events(self.action, [early, late], 30, end=30, keep_outside=True)
        self.assertEqual([e.event_name for e in self.action.witcher_events], ['Early', 'Late'])
        exported = events.export_events(self.action, 30, 0, 30, name='long_export_name')
        self.assertEqual([(e['raw_fields']['eventName'], e['raw_fields']['startTime'], e['raw_fields']['animationName'])
                          for e in exported], [('Early', 0.0, 'long_export_name')])
        events.import_events(self.action, [early, late], 30, end=30)
        self.assertEqual(len(self.action.witcher_events), 0)

    def test_forced_footstep_event_exports_without_a_sound_field(self):
        self.action.witcher_events.clear()
        bpy.ops.witcher.animation_event_add(event_type='CExtForcedLogicalFootstepAnimEvent', event_name='Step')
        written = self.write_read(events.export_events(self.action, 30, 0, 30))
        self.assertNotIn('soundEventName', written[0].raw_fields)

    def test_event_fields_time_is_clip_relative_and_ok_without_edits_changes_nothing(self):
        self.action.use_frame_range = True
        self.action.frame_start, self.action.frame_end = 10, 40
        item = self.action.witcher_events[1]
        item.frame = 16
        before = item.payload
        raw = json.loads(before)['raw_fields']
        raw.update(eventName=item.event_name, animationName='Clip', trackName=item.track_name, startTime=0.2, duration=0.5)
        self.action.witcher_event_index = 1
        dialog = SimpleNamespace(event_class=item.event_type, original=json.dumps(raw), class_fields=[], editors={})
        self.assertEqual(ui_anim_events.WITCH_OT_AnimationEventFields.execute(dialog, bpy.context), {'CANCELLED'})
        self.assertEqual(item.payload, before)
        field = SimpleNamespace(field_name='startTime', type_text='Float', is_set=True, value_text='0.5', children=[])
        dialog.class_fields = [field]
        self.assertEqual(ui_anim_events.WITCH_OT_AnimationEventFields.execute(dialog, bpy.context), {'FINISHED'})
        self.assertAlmostEqual(item.frame, 25)
        self.assertAlmostEqual(events.export_events(self.action, 30, 10, 40)[-1]['raw_fields']['startTime'], 0.5)

    def test_json_event_roundtrip_and_field_editor(self):
        exported = events.export_events(self.action, 30, 0, 30)
        events.import_events(self.action, json.loads(json.dumps(exported)), 30, end=30)
        self.assertEqual(self.action.witcher_events[0].sound_event, 'sword_swing')
        original = exported[0]['raw_fields']
        field_defaults = dict(name='', section_index=-1, class_name='CExtAnimSoundEvent', is_set=True,
                              show_unset=False, has_children=False, show_children=False, children=[])
        with mock.patch.object(ui_anim_events, 'context_clip', return_value=(self.owner, self.action, None)):
            result = bpy.ops.witcher.animation_event_fields(
                event_class='CExtAnimSoundEvent', original=json.dumps(original),
                class_fields=[dict(field_defaults, field_name='startTime', type_text='Float', value_text='0.5'),
                              dict(field_defaults, field_name='soundEventName', type_text='String', value_text='sword_hit')])
        self.assertEqual(result, {'FINISHED'})
        self.assertEqual(self.action.witcher_events[0].frame, 15)
        self.assertEqual(self.action.witcher_events[0].sound_event, 'sword_hit')
        with self.assertRaisesRegex(ValueError, 'Unsupported JSON event'):
            events.import_events(self.action, [{'unknown': 'event'}], 30)

    def test_typed_unknown_fields(self):
        item = self.action.witcher_events[0]
        data = json.loads(item.payload)
        data['raw_fields']['custom'] = {'tag': 'Sword', 'strength': 1.25}
        data['field_types'] = {'custom': {'type': 'SExtraEventData', 'fields': {'tag': 'CName', 'strength': 'Float'}}}
        item.payload = json.dumps(data)
        imported = self.write_read(events.export_events(self.action, 30, 0, 30))
        self.assertEqual(imported[0].raw_fields['custom'], {'tag': 'Sword', 'strength': 1.25})
        events.import_events(self.action, imported, 30, end=30)
        again = self.write_read(events.export_events(self.action, 30, 0, 30))
        self.assertEqual(again[0].raw_fields, imported[0].raw_fields)

    def test_event_arrays_handles_and_enums(self):
        item = self.action.witcher_events[0]
        data = json.loads(item.payload)
        data['raw_fields'].update(samples=[1.25, -2.0], names=['Left', 'Right'], templates=['characters/test.w2ent', ''],
                                  cast='PCP_Bone')
        data['field_types'] = dict(samples='array:2,0,Float', templates='array:2,0,handle:CEntityTemplate',
                                   names='array:2,0,CName', cast='EProjectileCastPosition')
        item.payload = json.dumps(data)
        imported = self.write_read(events.export_events(self.action, 30, 0, 30))
        self.assertEqual(imported[0].raw_fields['templates'], ['characters/test.w2ent', ''])
        self.assertEqual(imported[0].raw_fields['samples'], [1.25, -2.0])
        self.assertEqual(imported[0].raw_fields['names'], ['Left', 'Right'])
        self.assertEqual(imported[0].raw_fields['cast'], 'PCP_Bone')
        events.import_events(self.action, imported, 30, end=30)
        again = self.write_read(events.export_events(self.action, 30, 0, 30))
        self.assertEqual(again[0].raw_fields, imported[0].raw_fields)

    def test_sound_ansi_arrays_roundtrip_without_losing_events(self):
        from witcher3_tools.CR2W.event_props import make_field
        item = self.action.witcher_events[0]
        data = json.loads(item.payload)
        kind = 'array:2,0,StringAnsi'
        data['field_types'] = {'switchesToUpdate': kind}
        for switches in ([], [''], ['surface', 'equipment']):
            with self.subTest(switches=switches):
                prop = make_field('switchesToUpdate', kind, switches)
                encoded = cr2w_writer._encode_property_value(prop, {}, {})
                self.assertEqual(int.from_bytes(encoded[:4], 'little'), len(switches))
                data['raw_fields']['switchesToUpdate'] = switches
                item.payload = json.dumps(data)
                imported = self.write_read(events.export_events(self.action, 30, 0, 30))
                self.assertEqual(len(imported), 2)
                self.assertEqual(imported[0].raw_fields['switchesToUpdate'], switches)
                self.assertEqual(imported[1].event_name, 'Window')

    def test_injection_preserves_or_replaces_events(self):
        source = Path(self.tmp.name) / 'source.w2anims'
        entries = events.export_events(self.action, 30, 0, 30)
        cr2w_writer.write_w2anims(anims_builder.build_w2anims([spec('Clip', entries), spec('Other', [])]), str(source))
        for replace, expected in ((False, 2), (True, 0)):
            with self.subTest(replace=replace):
                out = Path(self.tmp.name) / f'out{replace}.w2anims'
                replacement = spec('Clip', [])
                replacement.update(replace='Clip', replace_events=replace)
                w2anims_inject.inject_file(source, out, [replacement])
                parsed = dc_anims.load_bin_anims_single(str(out), 'Clip').animations[0]
                self.assertEqual(len(parsed.entries), expected)

    def test_save_reload_and_operators(self):
        with mock.patch.object(ui_anim_events, 'context_clip', return_value=(self.owner, self.action, None)):
            bpy.context.scene.frame_set(12)
            self.assertEqual(bpy.ops.witcher.animation_event_add(event_name='New'), {'FINISHED'})
            self.assertEqual(self.action.witcher_events[-1].frame, 12)
            bpy.context.scene.frame_set(18)
            bpy.ops.witcher.animation_event_move()
            self.assertEqual(self.action.witcher_events[-1].frame, 18)
            bpy.ops.witcher.animation_event_remove()
            self.assertEqual(len(self.action.witcher_events), 2)
        path = str(Path(self.tmp.name) / 'events.blend')
        bpy.ops.witcher.event_track_add(track_name='Empty saved track')
        events.set_track_locked(self.action, 'Audio')
        events.set_track_locked(self.action, 'Empty saved track')
        events.move_track(self.action, '', 'Empty saved track', 'TOP')
        bpy.ops.wm.save_as_mainfile(filepath=path)
        bpy.ops.wm.open_mainfile(filepath=path)
        self.assertEqual(len(bpy.data.actions['Clip'].witcher_events), 2)
        self.assertFalse(events.modified(bpy.data.actions['Clip'], 0, 30))
        self.assertIn(('Empty saved track', []), events.track_rows(bpy.data.actions['Clip']))
        self.assertEqual(events.locked_tracks(bpy.data.actions['Clip']), {('', 'Audio'), ('', 'Empty saved track')})
        self.assertEqual([name for name, _ in events.track_rows(bpy.data.actions['Clip'])],
                         ['__Default', 'Empty saved track', 'Audio', 'Combat'])

    def test_event_filters_and_track_suggestions(self):
        scene = bpy.context.scene
        footstep = self.action.witcher_events.add()
        footstep.event_type, footstep.track_name = 'CExtAnimFootstepEvent', 'Audio'
        view = SimpleNamespace(bitflag_filter_item=1)

        def visible():
            return ui_anim_events.WITCH_UL_AnimationEvents.filter_items(view, bpy.context, self.action, 'witcher_events')[0]

        self.assertEqual(visible(), [1, 1, 1])
        scene.witcher_event_class_filter = 'CExtAnimSoundEvent'
        self.assertEqual(visible(), [1, 0, 0])
        scene.witcher_event_search = 'SWORD_SWING'
        self.assertEqual(visible(), [1, 0, 0])
        scene.witcher_event_track_filter = 'Combat'
        self.assertEqual(visible(), [0, 0, 0])
        bpy.ops.witcher.animation_event_clear_filters()
        scene.witcher_event_track_filter = 'Audio'
        self.assertEqual(visible(), [1, 0, 1])
        with mock.patch.object(ui_anim_events, 'context_clip', return_value=(self.owner, self.action, None)):
            self.assertEqual(ui_anim_events.track_names(scene, bpy.context, ''), ['Audio', 'Combat', '__Default'])
            self.assertEqual(ui_anim_events.track_names(footstep, bpy.context, ''), ['Audio', 'Combat', '__Default'])
            self.assertIn('CExtAnimSoundEvent', ui_anim_events.event_classes(scene, bpy.context, ''))

    def test_add_reuses_track_and_accepts_separate_track_name(self):
        with mock.patch.object(ui_anim_events, 'context_clip', return_value=(self.owner, self.action, None)):
            bpy.context.scene.witcher_event_class_filter = 'CExtAnimSoundEvent'
            bpy.ops.witcher.animation_event_add(event_name='Second sound', event_type='CExtAnimSoundEvent')
            self.assertEqual(self.action.witcher_events[-1].track_name, 'Audio')
            self.assertEqual(bpy.context.scene.witcher_event_class_filter, 'CExtAnimSoundEvent')
            bpy.ops.witcher.animation_event_add(event_name='Hit', track_name='Combat')
            self.assertEqual(self.action.witcher_events[-1].track_name, 'Combat')
            self.assertEqual(bpy.context.scene.witcher_event_class_filter, '')
            bpy.ops.witcher.animation_event_add(event_name='Spark', track_name='Effects')
            self.assertEqual(self.action.witcher_events[-1].track_name, 'Effects')
            self.assertEqual(len(self.owner.animation_data.nla_tracks), 0)
            self.action.witcher_events.clear()
            bpy.ops.witcher.animation_event_add(event_name='First')
            self.assertEqual(self.action.witcher_events[0].track_name, '')

    def test_sound_details_distinguish_cues_names_and_variants(self):
        repeat = self.action.witcher_events.add()
        repeat.sound_event = 'sword_swing'
        other = self.action.witcher_events.add()
        other.sound_event = 'sword_hit'
        preview, player = self.preview()
        player.wavs = dict.fromkeys(range(6), 'cached.wav')
        details = SimpleNamespace()
        context = SimpleNamespace(window_manager=mock.Mock())
        ui_anim_events.WITCH_OT_AnimationEventAudioInfo.invoke(details, context, None)
        self.assertEqual((details.cues, details.names, details.variants), (3, 2, 6))
        self.assertEqual(details.event_names, 'sword_hit, sword_swing')
        self.assertEqual(details.clip, self.action.name)

    def strip(self):
        self.owner.animation_data.action = None
        track = self.owner.animation_data.nla_tracks.new()
        strip = track.strips.new('Clip', 10, self.action)
        strip.scale, strip.repeat = 2, 2
        return track, strip

    def test_nla_time_mapping(self):
        self.action.witcher_events[0].frame = 3
        track, strip = self.strip()
        mapped = events.timeline_events(self.owner, 0, 200)
        self.assertEqual([r['frame'] for r in mapped if r['sound_event']], [16, 76])
        strip.use_reverse = True
        self.assertEqual([r['frame'] for r in events.timeline_events(self.owner, 0, 200) if r['sound_event']], [64, 124])
        strip.repeat = 1.5
        self.assertAlmostEqual(events.action_frame(strip, strip.frame_end), 15)
        track.mute = True
        self.assertIsNone(events.timeline_events(self.owner, 0, 200))
        track.mute = False
        strip.use_animated_time = True
        with self.assertRaisesRegex(ValueError, 'fixed playback speed'):
            events.timeline_events(self.owner, 0, 200)

    def preview(self, strip=None):
        player = mock.Mock()
        preview = event_preview.Preview(bpy.context.scene, self.owner, self.action, strip, player)
        event_preview._preview = preview
        return preview, player

    def test_sound_timing_and_stop(self):
        self.action.witcher_events[0].frame = 0
        preview, player = self.preview()
        bpy.context.scene.frame_set(0)
        event_preview.playback_started(bpy.context.scene)
        preview.advance(0, True)
        preview.advance(1, True)
        player.play.assert_called_once_with('sword_swing', False)
        event_preview.playback_stopped()
        player.stop.assert_called_once()
        self.assertTrue(preview.valid())
        self.owner.animation_data.action = bpy.data.actions.new('Other')
        event_preview.validate()
        self.assertIsNone(event_preview._preview)
        self.assertEqual(player.stop.call_count, 2)

    def test_sound_cleanup_on_delete_edit_mute_and_undo(self):
        preview, player = self.preview()
        self.action.witcher_events[0].frame += 1
        event_preview.validate()
        player.stop.assert_called_once()
        preview, player = self.preview()
        bpy.data.objects.remove(self.owner, do_unlink=True)
        event_preview.validate()
        player.stop.assert_called_once()
        self.assertIsNone(event_preview._preview)

    def test_reverse_strip_sound_and_muting(self):
        self.action.witcher_events[0].frame = 3
        track, strip = self.strip()
        strip.use_reverse = True
        preview, player = self.preview(strip)
        preview.advance(63, True)
        preview.advance(65, True)
        player.play.assert_called_once_with('sword_swing', False)
        track.mute = True
        event_preview.validate()
        player.stop.assert_called_once()
        self.assertIsNone(event_preview._preview)

    def test_controller_cleanup_after_actor_deletion(self):
        from witcher3_tools.controller.session import Session
        session = Session.__new__(Session)
        session.armature, session.sound = self.owner, mock.Mock()
        session._view = None
        session._save()
        sound = session.sound
        bpy.data.objects.remove(self.owner, do_unlink=True)
        session.restore()
        sound.stop.assert_called_once()
        self.assertIsNone(session._saved)

    def test_audio_handlers_stop_on_load_undo_and_unregister(self):
        for handler in ('load_pre', 'undo_pre', 'redo_pre'):
            preview, player = self.preview()
            self.assertIn(event_preview.stop, getattr(bpy.app.handlers, handler))
            event_preview.stop()
            player.stop.assert_called_once()
        preview, player = self.preview()
        event_preview.unregister()
        player.stop.assert_called_once()
        event_preview.register()


if __name__ == '__main__':
    addon.register()
    unittest.main(argv=[__file__], verbosity=2)
