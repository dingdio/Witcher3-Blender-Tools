"""Native game clips exercise joint translations, pole planes and action/NLA baking."""
import math
import os
import sys
import unittest
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import witcher3_tools as addon
from witcher3_tools.animation import events
from witcher3_tools.animation.action_compat import iter_action_fcurves
from witcher3_tools.CR2W import dc_anims
from witcher3_tools.importers import import_anims, import_rig
from witcher3_tools.rigging import ik
from witcher3_tools.ui import ui_rig

DEPOT = Path(os.environ.get('W3_REDKIT_DEPOT', r'C:\w3.modding\The Witcher 3 REDkit\r4data'))
RIG = DEPOT / 'characters/base_entities/man_base/man_base.w2rig'
CLIPS = (('man_sword_1handed', 'man_npc_sword_1hand_wounded_crawl'),
         ('man_geralt_sword', 'man_geralt_sword_attack_fast_1_rp_40ms'),
         ('man_geralt_sword', 'man_geralt_sword_attack_fast_1_lp_40ms'))


@unittest.skipUnless(RIG.is_file(), 'Set W3_REDKIT_DEPOT to the native game assets')
class BakeTests(unittest.TestCase):
    def load(self, name, clip, nla=False):
        if bpy.context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        for obj in list(bpy.data.objects):
            bpy.data.objects.remove(obj, do_unlink=True)
        for action in list(bpy.data.actions):
            bpy.data.actions.remove(action)
        arm = import_rig.import_w3_rig(str(RIG), do_fix_tail=True)
        bpy.context.view_layer.objects.active = arm
        arm.select_set(True)
        bpy.context.scene.witcher_main_armature = arm
        path = DEPOT / f'animations/man/combat/{name}.w2anims'
        entry = dc_anims.load_bin_anims_single(str(path), clip, rigPath=str(RIG)).animations[0]
        import_anims.import_anim(bpy.context, str(path), entry, override_select=arm, use_NLA=nla)
        return arm, entry.animation.animBuffer.numFrames

    def sample(self, arm, frames):
        result = []
        for frame in range(frames):
            bpy.context.scene.frame_set(frame)
            bpy.context.view_layer.update()
            result.append({b.name: b.matrix.copy() for b in arm.pose.bones if not b.name.startswith('W3IK_')})
        return result

    def assert_pose(self, arm, expected):
        maximum = 0
        for frame, bones in enumerate(expected):
            bpy.context.scene.frame_set(frame)
            bpy.context.view_layer.update()
            for name, matrix in bones.items():
                actual = arm.pose.bones[name].matrix
                error = (matrix.translation - actual.translation).length
                angle = abs(matrix.to_quaternion().rotation_difference(actual.to_quaternion()).angle)
                angle = min(angle, abs(2 * math.pi - angle))
                self.assertLess(error, 0.0002, (frame, name, error))
                self.assertLess(math.degrees(angle), 0.06, (frame, name, math.degrees(angle)))
                maximum = max(maximum, error)
        return maximum

    def test_fk_ik_fk_preserves_pose_and_events(self):
        for name, clip in CLIPS:
            for nla in (False, True):
                with self.subTest(clip=clip, nla=nla):
                    arm, frames = self.load(name, clip, nla)
                    expected = self.sample(arm, frames)
                    source = events.clip(arm, 0)[0]
                    event_values = [events.values(e) for e in source.witcher_events]
                    neck = [c for c in iter_action_fcurves(source, target=arm) if '"neck"' in c.data_path]
                    for curve in neck:
                        for key in curve.keyframe_points:
                            key.interpolation = 'BEZIER'
                    expected = self.sample(arm, frames)
                    result = ik.bake_fk_to_ik(bpy.context, arm, 0, frames - 1, tuple(ik.LIMB_DEFS))
                    self.assertTrue(result, result.message)
                    maximum = self.assert_pose(arm, expected)
                    self.assertGreaterEqual(result.details['max_position_error'] + 1e-5, maximum)
                    self.assertTrue(all(k.interpolation == 'BEZIER' for c in neck for k in c.keyframe_points))
                    count = len(bpy.data.actions)
                    result = ik.bake_ik_to_fk(bpy.context, arm, 0, frames - 1, tuple(ik.LIMB_DEFS))
                    self.assertTrue(result, result.message)
                    self.assertEqual(len(bpy.data.actions), count + 1)
                    self.assertFalse(arm.animation_data.use_nla)
                    self.assertTrue(all(t.mute and not t.is_solo for t in arm.animation_data.nla_tracks))
                    self.assertEqual(len(arm.animation_data.action.witcher_events), len(event_values))
                    ik.remove_ik_rig(bpy.context, arm)
                    self.assert_pose(arm, expected)

    def test_accessory_selection_and_nla_range(self):
        arm, frames = self.load(*CLIPS[0], nla=True)
        accessory = bpy.data.objects.new('Scabbards', bpy.data.armatures.new('Scabbards'))
        bpy.context.scene.collection.objects.link(accessory)
        accessory.parent = arm
        accessory.select_set(True)
        bpy.context.view_layer.objects.active = accessory
        bpy.context.scene.witcher_main_armature = accessory
        self.assertEqual(ui_rig._find_ik_armature(bpy.context), arm)
        self.assertEqual(ui_rig._ik_frame_range(bpy.context), (0, frames - 1))
        self.assertEqual(bpy.ops.witcher.rig_select_body(), {'FINISHED'})
        self.assertEqual(list(bpy.context.selected_objects), [arm])
        self.assertEqual(bpy.context.scene.witcher_main_armature, arm)


if __name__ == '__main__':
    addon.register()
    unittest.main(argv=[__file__], verbosity=2)
