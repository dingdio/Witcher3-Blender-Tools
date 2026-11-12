"""Check animation state, preview persistence and trajectory editing in Blender 4.5+."""
import math
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock
from pathlib import Path

import bpy
from mathutils import Matrix, Quaternion, Vector

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import witcher3_tools as addon
from witcher3_tools.animation import mixamo_retarget as rt, trajectory_tools as tt
from witcher3_tools.animation.action_compat import assign_action, iter_action_fcurves
from witcher3_tools.exporters.export_anims import W3AnimationExporter
from witcher3_tools.exporters import export_anims
from witcher3_tools.CR2W import anims_builder, cr2w_writer, w2anims_inject
from witcher3_tools.ui import ui_inject, ui_motion


def armature(name, names=("Root", "Trajectory", "pelvis")):
    data = bpy.data.armatures.new(name)
    obj = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.mode_set(mode='EDIT')
    for i, name in enumerate(names):
        bone = data.edit_bones.new(name)
        bone.head, bone.tail = (0, 0, i), (0, 1, i)
        if i:
            bone.parent = data.edit_bones[names[0]]
    bpy.ops.object.mode_set(mode='OBJECT')
    for name in names:
        data.witcherui_RigSettings.bone_order_list.add().name = name
    return obj


def action(obj, name, distance=1.0, yaw=0.0):
    clip = bpy.data.actions.new(name)
    assign_action(obj, clip)
    pb = obj.pose.bones.get("Trajectory") or obj.pose.bones[0]
    pb.rotation_mode = 'QUATERNION'
    for frame, amount in ((1, 0.0), (11, 1.0)):
        pb.location = (0, distance * amount, 0)
        pb.keyframe_insert("location", frame=frame)
        pb.rotation_quaternion = Quaternion((0, 0, 1), yaw * amount)
        pb.keyframe_insert("rotation_quaternion", frame=frame)
    return clip


def curve_data(clip, obj, paths):
    return [(c.data_path, c.array_index,
             [(tuple(k.co), tuple(k.handle_left), tuple(k.handle_right), k.interpolation) for k in c.keyframe_points])
            for c in iter_action_fcurves(clip, target=obj) if c.data_path in paths]


class AnimationStateTests(unittest.TestCase):
    def setUp(self):
        for obj in list(bpy.data.objects):
            bpy.data.objects.remove(obj, do_unlink=True)
        for coll in list(bpy.data.collections):
            if coll.get("previous_pose") is not None:
                bpy.data.collections.remove(coll)
        for clip in list(bpy.data.actions):
            bpy.data.actions.remove(clip)
        bpy.context.scene.frame_set(3, subframe=0.25)
        bpy.context.scene.render.fps = 30

    def test_export_normalizes_interpolated_quaternion_channels(self):
        obj = armature('Rotation', names=('Root',))
        clip = bpy.data.actions.new('Authored rotation')
        assign_action(obj, clip)
        bone = obj.pose.bones['Root']
        bone.rotation_mode = 'QUATERNION'
        for frame, angle in ((0, 0.0), (2, 2.0)):
            bone.rotation_quaternion = Quaternion((1, 0, 0), angle)
            bone.keyframe_insert('rotation_quaternion', frame=frame)
        for curve in iter_action_fcurves(clip, target=obj):
            for key in curve.keyframe_points:
                key.interpolation = 'LINEAR'
        bpy.context.scene.frame_set(1)
        expected = bone.matrix.to_quaternion().normalized()
        exporter = W3AnimationExporter()
        exporter._W3AnimationExporter__frame_start = 0
        exporter._W3AnimationExporter__frame_end = 2
        spec = exporter.collect_native_anim_spec(armature=obj, action=clip)
        x, y, z, w = spec['bones'][0]['rot_frames'][1]
        actual = Quaternion((w, x, y, z))
        self.assertLess(abs(actual.rotation_difference(expected).angle), 1e-4)

    def test_sparse_authored_action_preserves_unkeyed_bones(self):
        obj = armature('Sparse', names=('Root', 'Static', 'Animated'))
        clip = bpy.data.actions.new('Authored sparse clip')
        assign_action(obj, clip)
        bone = obj.pose.bones['Animated']
        for frame, x in ((0, 0.0), (2, 0.1)):
            bone.location.x = x
            bone.keyframe_insert('location', frame=frame)
        static = obj.pose.bones['Static']
        static.location = (0.2, 0.3, 0.4)
        static.rotation_mode = 'QUATERNION'
        static.rotation_quaternion = Quaternion((0, 0, 1), 0.5)
        bpy.context.view_layer.update()
        expected = obj.pose.bones['Root'].matrix.inverted() @ static.matrix
        exporter = W3AnimationExporter()
        exporter._W3AnimationExporter__frame_start = 0
        exporter._W3AnimationExporter__frame_end = 2
        spec = exporter.collect_native_anim_spec(armature=obj, action=clip)
        result = next(b for b in spec['bones'] if b['name'] == 'Static')
        for position, rotation in zip(result['pos_frames'], result['rot_frames']):
            self.assertLess((Vector(position) - expected.translation).length, 1e-5)
            x, y, z, w = rotation
            self.assertLess(Quaternion((w, x, y, z)).rotation_difference(expected.to_quaternion()).angle, 1e-4)

    def test_export_isolates_action_and_restores_slot_and_nla(self):
        obj = armature("Target")
        wanted = action(obj, "One metre")
        other = action(obj, "Ten metres", 10)
        data = obj.animation_data
        track = data.nla_tracks.new()
        track.strips.new("Other", 1, other)
        track.is_solo = True
        second_slot = other.slots.new('OBJECT', 'Second')
        data.action_slot = second_slot
        data.action_blend_type, data.action_influence = 'ADD', 0.25
        bpy.context.scene.frame_set(3, subframe=0.25)
        exporter = W3AnimationExporter()
        exporter._W3AnimationExporter__frame_start = 1
        exporter._W3AnimationExporter__frame_end = 11
        spec = exporter.collect_native_anim_spec(armature=obj, action=wanted, include_motion_extraction=True)
        self.assertAlmostEqual(spec["motion_extraction"]["frames"][-1], 1.0, places=5)
        trajectory = next(b for b in spec["bones"] if b["name"] == "Trajectory")
        self.assertAlmostEqual(trajectory["pos_frames"][-1][1], 1.0, places=5)
        self.assertEqual(data.action, other)
        self.assertEqual(data.action_slot, second_slot)
        self.assertTrue(data.use_nla and track.is_solo)
        self.assertEqual(data.action_blend_type, 'ADD')
        self.assertAlmostEqual(data.action_influence, 0.25)
        self.assertAlmostEqual(bpy.context.scene.frame_current_final, 3.25)

    def test_sampling_exception_restores_unkeyed_pose(self):
        obj = armature("Target")
        clip = action(obj, "Clip")
        obj.animation_data.action = None
        pb = obj.pose.bones['pelvis']
        pb.location = (3, 2, 1)
        before = pb.matrix_basis.copy()
        with self.assertRaisesRegex(RuntimeError, "probe"):
            with tt.using_action(obj, clip):
                bpy.context.scene.frame_set(8)
                raise RuntimeError("probe")
        self.assertIsNone(obj.animation_data.action)
        self.assertEqual(pb.matrix_basis, before)
        self.assertAlmostEqual(bpy.context.scene.frame_current_final, 3.25)

    def test_moving_replacement_rejects_axes_that_drop_all_travel(self):
        obj = armature('Target')
        clip = action(obj, 'Walk')
        exporter = W3AnimationExporter()
        exporter._W3AnimationExporter__frame_start = 1
        exporter._W3AnimationExporter__frame_end = 11
        spec = exporter.collect_native_anim_spec(armature=obj, action=clip, include_motion_extraction=True)
        entry = SimpleNamespace(action_name=clip.name, replace_clip=clip.name, skeletal_anim_type='SAT_Normal',
                                additive_type='NONE', include_motion_extraction=True)
        settings = bpy.context.scene.witcher_motion
        settings.axes, settings.use_x, settings.use_y = 'CUSTOM', True, False
        try:
            with tempfile.TemporaryDirectory() as tmp:
                src, dst = Path(tmp) / 'set.w2anims', Path(tmp) / 'out.w2anims'
                cr2w_writer.write_w2anims(anims_builder.build_w2anims([spec]), str(src))
                with mock.patch.object(export_anims, 'get_selected_armature', return_value=obj):
                    with self.assertRaisesRegex(w2anims_inject.InjectError, 'axis settings'):
                        export_anims.inject_w3_anim_set(bpy.context, str(src), str(dst), [entry])
                self.assertFalse(dst.exists())
        finally:
            settings.axes, settings.use_x, settings.use_y = 'AUTO', False, True

    def test_preview_survives_save_and_restores_original(self):
        obj = armature("Target")
        source = armature("Source", ("Hips",))
        original = action(obj, "Original")
        slot = original.slots.new('OBJECT', 'Original slot')
        obj.animation_data.action_slot = slot
        handle = slot.handle
        obj.animation_data.action_blend_type = 'ADD'
        obj.animation_data.action_influence = 0.4
        obj.pose.bones['pelvis'].rotation_mode = 'XYZ'
        obj.pose.bones['pelvis'].rotation_euler = (0.2, 0.3, 0.4)
        before = obj.pose.bones['pelvis'].matrix_basis.copy()
        rows = [('pelvis', 'Hips', '', 0.0)]
        for _ in range(2):
            rt.preview(bpy.context, source, obj, rows, 1.0, match_rest=False, ik=False)
        self.assertGreater(original.users, 0)
        obj.name, original.name = "Renamed target", "Renamed action"
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'preview.blend')
            bpy.ops.wm.save_as_mainfile(filepath=path)
            bpy.ops.wm.open_mainfile(filepath=path)
        obj = bpy.data.objects['Renamed target']
        self.assertTrue(rt.preview_active(obj))
        rt.clear_preview(obj)
        self.assertEqual(obj.animation_data.action.name, 'Renamed action')
        self.assertEqual(obj.animation_data.action_slot_handle, handle)
        self.assertTrue(obj.animation_data.use_nla)
        self.assertEqual(obj.animation_data.action_blend_type, 'ADD')
        self.assertAlmostEqual(obj.animation_data.action_influence, 0.4)
        self.assertEqual(obj.pose.bones['pelvis'].rotation_mode, 'XYZ')
        self.assertLess(max(abs(v) for row in (obj.pose.bones['pelvis'].matrix_basis - before) for v in row), 1e-6)
        self.assertFalse(rt.preview_active(obj))

    def test_bake_keeps_previous_action_and_disables_target_nla(self):
        obj = armature("Target")
        original = action(obj, "Original")
        source = armature("Source", ("Hips",))
        action(source, "Source clip")
        track = obj.animation_data.nla_tracks.new()
        track.strips.new("Original", 1, original)
        track.is_solo = True
        rows = [('pelvis', 'Hips', '', 0.0)]
        rt.preview(bpy.context, source, obj, rows, 1.0, match_rest=False, ik=False)
        baked, _ = rt.bake(bpy.context, source, obj, rows, 1.0, 'Baked', match_rest=False, ik=False)
        self.assertEqual(obj.animation_data.action, baked)
        self.assertTrue(original.use_fake_user)
        self.assertFalse(obj.animation_data.use_nla)
        self.assertFalse(rt.preview_active(obj))
        tt.generate_trajectory(bpy.context, obj, baked, rotation='SMOOTH')

    def test_generate_keeps_authored_rotation_and_scale_curves(self):
        obj = armature("Target")
        clip = action(obj, "Authored turn", yaw=math.pi / 4)
        trajectory = obj.pose.bones['Trajectory']
        trajectory.scale = (1.2, 1.2, 1.2)
        trajectory.keyframe_insert('scale', frame=1)
        hips = obj.pose.bones['pelvis']
        hips.rotation_mode = 'QUATERNION'
        for frame, y, yaw in ((1, 0.0, 0.0), (11, 2.0, math.pi)):
            hips.location = (0, y, 0)
            hips.rotation_quaternion = Quaternion((0, 0, 1), yaw)
            hips.keyframe_insert('location', frame=frame)
            hips.keyframe_insert('rotation_quaternion', frame=frame)
        paths = {trajectory.path_from_id(p) for p in (*tt.ROTATION_PROPS, 'scale')}
        before = curve_data(clip, obj, paths)
        body = tt.sample_bone(bpy.context, obj, 'pelvis', range(1, 12))
        tt.generate_trajectory(bpy.context, obj, clip)
        self.assertEqual(curve_data(clip, obj, paths), before)
        self.assertEqual(tt.sample_bone(bpy.context, obj, 'pelvis', range(1, 12)), body)
        for rotation in ('NONE', 'LINEAR', 'SMOOTH'):
            with self.assertRaisesRegex(ValueError, 'already has rotation keys'):
                tt.generate_trajectory(bpy.context, obj, clip, rotation=rotation)
        self.assertEqual(curve_data(clip, obj, paths), before)


    def test_depot_stays_read_only_when_project_overlaps_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            depot = Path(tmp) / 'workspace'
            prefs = SimpleNamespace(redkit_depot_path=str(depot), redkit_uncooked_path='', uncook_path='',
                                    redkit_projects=[SimpleNamespace(path=tmp)])
            with mock.patch.object(ui_inject, '_prefs', return_value=prefs):
                self.assertEqual(ui_inject.classify(bpy.context, str(depot / 'set.w2anims'))[0], 'DEPOT')

    def test_path_uses_first_frame_and_preserves_same_named_object(self):
        obj = armature('Target')
        clip = action(obj, 'Moving root')
        root = obj.pose.bones['Root']
        for frame, x in ((1, 5), (11, 20)):
            root.location.x = x
            root.keyframe_insert('location', frame=frame)
        me, _, _ = tt.build_motion_extraction(bpy.context, obj, clip)
        existing = bpy.data.objects.new(ui_motion.PATH_OBJECT, None)
        bpy.context.scene.collection.objects.link(existing)
        bpy.context.scene.frame_set(11)
        ui_motion._show_path(bpy.context, obj, clip, me)
        path = ui_motion._path_object()
        self.assertNotEqual(path, existing)
        self.assertAlmostEqual(path.matrix_world.translation.x, 5.0)
        self.assertEqual(bpy.context.scene.frame_current, 11)
        ui_motion._show_path(bpy.context, obj, clip, None)
        self.assertIsNone(ui_motion._path_object())
        self.assertEqual(bpy.data.objects.get(ui_motion.PATH_OBJECT), existing)


if __name__ == '__main__':
    addon.register()
    unittest.main(argv=[__file__], verbosity=2)
