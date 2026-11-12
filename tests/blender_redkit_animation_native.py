"""Run with Blender --background --factory-startup --python-exit-code 1 --python this_file."""
import copy
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
from witcher3_tools.animation import events, redkit
from witcher3_tools.CR2W import anims_builder as builder, cr2w_writer, w2anims_inject as wi
from witcher3_tools.CR2W.cutscene_event_schema import build_event_data
from witcher3_tools.importers import import_anims
from witcher3_tools.ui import ui_anims, ui_entity


def spec(name, entries):
    return dict(action_name=name, num_frames=31, dt=1 / 30, fps=30,
                bones=[dict(name='Root', pos_frames=[(0, 0, 0)] * 31,
                            rot_frames=[(0, 0, 0, 1)] * 31, scale_frames=[(1, 1, 1)] * 31)],
                entry_events=entries)


class RedkitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.depot, self.game = self.root / 'r4data', self.root / 'game'
        self.rel, self.link = 'animations/test.w2anims', 'sounds/test.w2animev'
        self.path = self.depot / self.rel
        self.path.parent.mkdir(parents=True)
        (self.depot / 'sounds').mkdir()
        self.game.mkdir()
        for patch in (mock.patch.object(redkit, '_get_redkit_depot_roots', return_value=[str(self.depot)]),
                      mock.patch.object(redkit, 'source_roots', return_value=[str(self.game)])):
            patch.start()
            self.addCleanup(patch.stop)
        redkit._read_events.cache_clear()
        self.addCleanup(redkit._read_events.cache_clear)
        self.main = dict(event_type='CExtAnimDurationEvent', source_game='W3', raw_fields=dict(
            eventName='Input', startTime=0.2, duration=0.3, animationName='Zulu', trackName='Combat'))
        self.sound = dict(event_type='CExtAnimSoundEvent', source_game='W3', raw_fields=dict(
            eventName='Grunt', startTime=0.25, animationName='Zulu', trackName='Voice', soundEventName='grunt'))
        data = builder.build_w2anims([spec('Zulu', [self.main]), spec('Alpha', [])])
        prop = data.CHUNKS.CHUNKS[0].GetVariableByName('extAnimEvents')
        handle = builder._make_import_handle(data, 'CExtAnimEventsFile', self.link, 'handle:CExtAnimEventsFile')
        prop.Handles = prop.elements = [handle]
        cr2w_writer.write_w2anims(data, str(self.path))
        ext = builder._init_cr2w()
        _, chunk = builder._add_chunk(ext, 'CExtAnimEventsFile', [])
        chunk.postPropsVariantProps = builder._make_cutscene_event_props([self.sound])
        cr2w_writer.write_w2anims(ext, str(self.depot / self.link))
        self.action = bpy.data.actions.new('Zulu')
        self.addCleanup(lambda: bpy.data.actions.remove(self.action))
        scene = bpy.context.scene
        scene.witcher_load_anim_on_select = False
        scene.witcher_anim_use_redkit = True
        scene.witcher_anim_redkit_tracks = True
        scene.witcher_anim_redkit_order = True

    def test_path_resolution_and_disabled_depot(self):
        self.assertEqual(Path(redkit.depot_file(bpy.context, self.rel)), self.path)
        self.assertEqual(Path(redkit.depot_file(bpy.context, str(self.game / self.rel))), self.path)
        self.assertEqual(redkit.depot_file(bpy.context, '../outside.w2anims'), '')
        with mock.patch.object(redkit, '_get_redkit_depot_roots', return_value=[]):
            self.assertEqual(redkit.depot_roots(), [])
            self.assertEqual(redkit.depot_file(bpy.context, self.rel), '')

    def test_set_button_loads_depot_and_clips_use_redkit_order(self):
        settings = SimpleNamespace(main_entity_skeleton='', animset_list=[], animset_list_index=-1, source_game='w3')
        with mock.patch.object(ui_entity, 'get_main_armature_and_rig_settings', return_value=(object(), settings)), \
             mock.patch.object(ui_entity, 'repo_file_for_source', side_effect=AssertionError('Game lookup used')):
            self.assertEqual(bpy.ops.witcher.list_loadapp(action='w2anims', path=self.rel, source_game='w3'), {'FINISHED'})
        scene = bpy.context.scene
        self.assertEqual(Path(scene.witcher_loaded_w2anims_path), self.path)
        self.assertEqual([i.name for i in scene.witcher_w2anims_list], ['Zulu', 'Alpha'])
        view = SimpleNamespace(filter_string='', filter_invert=False, bitflag_filter_item=1)
        self.assertEqual(ui_anims.TOOL_UL_List.filter_items(view, bpy.context, scene, 'witcher_w2anims_list')[1], [1, 0])
        scene.witcher_anim_redkit_order = False
        self.assertEqual(ui_anims.TOOL_UL_List.filter_items(view, bpy.context, scene, 'witcher_w2anims_list')[1], [])
        self.assertEqual(ui_anims._get_loaded_animset_ui_state(bpy.context)['source_badge'], 'REDkit')

    def test_custom_clip_order_keeps_selection_and_clamps(self):
        scene = bpy.context.scene
        scene.witcher_w2anims_list.clear()
        for name in ('Zulu', 'Alpha', 'Middle'):
            scene.witcher_w2anims_list.add().name = name
        scene.witcher_w2anims_list_index = 0
        scene.witcher_load_anim_on_select = True
        bpy.ops.witcher.list_reorder(direction='UP')
        self.assertEqual([i.name for i in scene.witcher_w2anims_list], ['Alpha', 'Zulu', 'Middle'])
        self.assertEqual(scene.witcher_w2anims_list_index, 1)
        self.assertFalse(scene.witcher_anim_redkit_order)
        self.assertTrue(scene.witcher_load_anim_on_select)
        bpy.ops.witcher.list_reorder(direction='TOP')
        bpy.ops.witcher.list_reorder(direction='UP')
        self.assertEqual(scene.witcher_w2anims_list[0].name, 'Zulu')
        bpy.ops.witcher.list_reorder(direction='BOTTOM')
        bpy.ops.witcher.list_reorder(direction='DOWN')
        self.assertEqual(scene.witcher_w2anims_list[-1].name, 'Zulu')
        self.assertEqual(scene.witcher_w2anims_list_index, 2)

    def test_linked_sound_events_and_track_recovery(self):
        reference = redkit.clip_events(bpy.context, str(self.path), 'Zulu')
        self.assertEqual([e.track_name for e in reference], ['Combat', 'Voice'])
        self.assertEqual(reference[1].source_file, self.link)
        cooked = copy.deepcopy(reference)
        for event in cooked:
            event.track_name = ''
            event.raw_fields.pop('trackName')
        restored = redkit.restore_tracks(cooked, reference)
        self.assertEqual([e.track_name for e in restored], ['Combat', 'Voice'])
        self.assertEqual([e.track_name for e in cooked], ['', ''])
        for original, result in zip(cooked, restored):
            self.assertEqual(original.raw_fields, {k: v for k, v in result.raw_fields.items() if k != 'trackName'})
        entry = SimpleNamespace(animation=SimpleNamespace(name='Zulu'), entries=cooked)
        direct = redkit.prepare_entry(bpy.context, str(self.path), entry)
        self.assertEqual(len(direct.entries), 2)
        bpy.context.scene.witcher_anim_redkit_tracks = False
        self.assertIs(redkit.prepare_entry(bpy.context, str(self.game / self.rel), entry), entry)
        self.assertEqual(len(redkit.prepare_entry(bpy.context, str(self.path), entry).entries), 2)

    def test_ambiguous_and_changed_events_keep_original_data(self):
        reference = build_event_data('W3', self.main['event_type'], self.main['raw_fields'])
        alternate = copy.copy(reference)
        alternate.track_name = 'Other'
        cooked = copy.copy(reference)
        cooked.track_name = ''
        self.assertEqual(redkit.restore_tracks([cooked], [reference, alternate])[0].track_name, '')
        cooked.start_time += 0.1
        self.assertEqual(redkit.restore_tracks([cooked], [reference])[0].track_name, '')
        cooked.track_name = 'Authored'
        self.assertEqual(redkit.restore_tracks([cooked], [reference])[0].track_name, 'Authored')

    def test_auto_tracks_on_redkit_and_game_import(self):
        scene = bpy.context.scene
        scene.witcher_anim_redkit_tracks = False
        self.addCleanup(setattr, scene, 'witcher_anim_auto_tracks', 'NONE')
        raw = copy.deepcopy(self.main)
        raw['raw_fields']['trackName'] = '__Default'
        cr2w_writer.write_w2anims(builder.build_w2anims([spec('Zulu', [raw])]), str(self.path))
        redkit._read_events.cache_clear()
        target = bpy.data.objects.new('ImportTarget', None)
        self.addCleanup(bpy.data.objects.remove, target)
        for path in (str(self.path), str(self.game / self.rel)):
            for mode, expected in (('NONE', '__Default'), ('NAME', 'Input'), ('CLASS', 'DurationEvent')):
                with self.subTest(source=path, mode=mode):
                    scene.witcher_anim_auto_tracks = mode
                    entry = SimpleNamespace(animation=SimpleNamespace(name='Zulu', framesPerSecond=30, animBuffer=None),
                                            entries=[build_event_data('W3', raw['event_type'], raw['raw_fields'])])
                    prepared = redkit.prepare_entry(bpy.context, path, entry)
                    importer = import_anims.AnimImporter(path, prepared)
                    importer._AnimImporter__assign_action(target, self.action, num_frames=31)
                    self.assertEqual(self.action.witcher_events[0].track_name, expected)
                    self.assertEqual(prepared.entries[0].track_name, '__Default')
                    self.assertEqual(events.modified(self.action, 0, 30), mode != 'NONE')
                    self.assertEqual(events.export_events(self.action, 30, 0, 30)[0]['raw_fields']['trackName'], expected)

    def test_tracks_and_export_do_not_duplicate_linked_sounds(self):
        reference = redkit.clip_events(bpy.context, str(self.path), 'Zulu')
        events.import_events(self.action, reference, 30, end=30)
        self.assertEqual([name for name, _ in events.track_rows(self.action)], ['__Default', 'Combat', 'Voice'])
        self.action.witcher_events[0].event_name = 'Edited input'
        data = spec('Zulu', events.export_events(self.action, 30, 0, 30))
        data.update(replace='Zulu', replace_events=True, event_sources=events.event_sources(self.action, 0, 30))
        out, _, _ = wi.inject_bytes(self.path.read_bytes(), [data])
        parsed = wi.describe_set(wi.RawCR2W(out))
        self.assertEqual(parsed[0]['events'], 1)
        standalone = self.root / 'standalone.w2anims'
        cr2w_writer.write_w2anims(builder.build_w2anims([data]), str(standalone))
        self.assertEqual(wi.describe_set(wi.RawCR2W(standalone.read_bytes()))[0]['events'], 2)
        self.action.witcher_events.remove(1)
        data.update(entry_events=events.export_events(self.action, 30, 0, 30), event_sources=events.event_sources(self.action, 0, 30))
        with self.assertRaisesRegex(wi.InjectError, 'Linked events'):
            wi.inject_bytes(self.path.read_bytes(), [data])

    def test_redkit_track_display_order_and_group_sorting(self):
        def add(name, frame, source=''):
            event = self.action.witcher_events.add()
            event.track_name, event.frame, event.source_file = name, frame, source

        def names():
            return [name for name, _ in events.track_rows(self.action)]

        # Import is batched so REDkit order is calculated after every field is populated.
        with events.defer_track_sync(self.action):
            # Plain draw tracks are appended in first-event order, not name order.
            for name, frame in [('Beta', 3), ('Zulu', 0), ('alpha', 1), ('Group!', 2)]:
                add(name, frame)
            add('', 0)
            add('__Default', 1)
            self.assertEqual(names(), ['__Default', 'Zulu', 'alpha', 'Group!', 'Beta'])
            self.assertEqual(len(events.track_rows(self.action)[0][1]), 2)
            # A grouped child sorts the existing draw list; later plain rows append.
            add('Group.child', 4)
            add('Aaron', 5)
            self.assertEqual(names(), ['__Default', 'Beta', 'Group.child', 'Group!', 'Zulu', 'alpha', 'Aaron'])
            # Linked files also create grouped children, sorting the unpinned rows.
            add('ZuluSound', 0, 'sounds/z_file.w2animev')
            add('AlphaSound', 0, 'sounds/a_file.w2animev')
            before = [events.values(e) for e in self.action.witcher_events]
            self.assertEqual(names(), ['__Default', 'Aaron', 'Beta', 'Group.child', 'Group!', 'Zulu', 'alpha', 'AlphaSound', 'ZuluSound'])
            self.assertEqual([events.values(e) for e in self.action.witcher_events], before)
        self.assertEqual([t.name for t in self.action.witcher_event_tracks], names())
        self.assertNotIn(('', 'ZuluSound'), events.track_keys(self.action))

    def test_previous_event_baseline_remains_unmodified(self):
        entries = redkit.clip_events(bpy.context, str(self.path), 'Zulu')[:1]
        events.import_events(self.action, entries, 30, end=30)
        baseline = json.loads(self.action['witcher_event_baseline'])
        for event in baseline:
            event.pop('source_file')
        self.action['witcher_event_baseline'] = json.dumps(baseline)
        self.assertFalse(events.modified(self.action, 0, 30))


if __name__ == '__main__':
    addon.register()
    unittest.main(argv=[__file__], verbosity=2)
