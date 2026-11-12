"""Create clips and review the exact actions included by animation export."""
import bpy
from bpy.props import BoolProperty, CollectionProperty, IntProperty, StringProperty
from . import ui_motion
from ..animation import events
from ..animation.action_compat import assign_action
from ..exporters import export_anims
from ..rigging import ik as ik_rig


class WITCH_OT_AnimationNewClip(bpy.types.Operator):
    bl_idname = 'witcher.animation_new_clip'
    bl_label = 'New Clip'
    bl_description = 'Create an empty action in the character rest pose and add it to the Export Set; existing actions and NLA tracks are kept'
    bl_options = {'REGISTER', 'UNDO'}
    clip_name: StringProperty(name='Name', default='new_animation')
    start: IntProperty(name='First Frame', default=0, min=0)
    frames: IntProperty(name='Frame Count', default=31, min=2, max=100000)
    fps: IntProperty(name='Scene FPS', default=30, min=1, max=240)

    @classmethod
    def poll(cls, context):
        arm = export_anims.get_selected_armature(context)
        return arm is not None and arm.mode != 'EDIT' and not (arm.animation_data and arm.animation_data.use_tweak_mode)

    def invoke(self, context, event):
        self.fps = context.scene.render.fps
        return context.window_manager.invoke_props_dialog(self, width=360)

    def draw(self, context):
        for prop in ('clip_name', 'start', 'frames', 'fps'):
            self.layout.prop(self, prop)
        self.layout.label(text='FPS applies to every clip in this scene.', icon='INFO')
        self.layout.label(text='Starts in rest pose with NLA playback off.')

    def execute(self, context):
        if not self.clip_name.strip():
            self.report({'ERROR'}, 'Enter a clip name')
            return {'CANCELLED'}
        arm = export_anims.get_selected_armature(context)
        arm.animation_data_create()
        if arm.animation_data.action:
            arm.animation_data.action.use_fake_user = True
        action = bpy.data.actions.new(self.clip_name.strip())
        action.use_fake_user = True
        action.use_frame_range = True
        action.frame_start, action.frame_end = self.start, self.start + self.frames - 1
        action.witcher_events_known = True
        events.sync_tracks(action)
        assign_action(arm, action)
        arm.animation_data.use_nla = False
        arm.animation_data.action_blend_type = 'REPLACE'
        arm.animation_data.action_influence = 1
        for bone in arm.pose.bones:
            bone.matrix_basis.identity()
        scene = context.scene
        scene.render.fps, scene.render.fps_base = self.fps, 1
        scene.frame_start, scene.frame_end = self.start, self.start + self.frames - 1
        scene.frame_set(self.start)
        scene.witcher_w3_anim_source = 'ACTION'
        entry = scene.witcher_anim_export_set.add()
        entry.action_name = action.name
        scene.witcher_anim_export_set_index = len(scene.witcher_anim_export_set) - 1
        return {'FINISHED'}


def export_targets(context, fallback_motion=False):
    scene = context.scene
    entries = [e for e in scene.witcher_anim_export_set if e.enabled]
    if entries:
        return [(e.clip_name.strip() or e.action_name, bpy.data.actions.get(e.action_name), None, e.include_motion_extraction)
                for e in entries]
    arm = export_anims.get_selected_armature(context)
    action, _ = export_anims.resolve_action(arm, context=context, source_mode=scene.witcher_w3_anim_source)
    return [(action.name, action, (scene.frame_start, scene.frame_end), fallback_motion)] if action else []


def event_counts(action, first, last):
    if not action.witcher_events_known:
        return 0, 0
    included = [e for e in action.witcher_events if e.frame <= last + 1e-4 and e.frame + e.duration >= first - events.START_SLACK]
    return len(included), sum(bool(e.sound_event) for e in included)


class WITCH_PG_ExportClipSummary(bpy.types.PropertyGroup):
    clip_name: StringProperty(name='Clip')
    first: IntProperty(name='First Frame')
    last: IntProperty(name='Last Frame')
    events: IntProperty(name='Exported Events')
    sounds: IntProperty(name='Sound Cues')
    unnamed: IntProperty(name='Default Track Events')
    excluded: IntProperty(name='Outside Export Range')
    event_state: StringProperty(name='Event Data')
    motion: StringProperty(name='Motion Extraction')
    problem: StringProperty(name='Issue')


class WITCH_UL_ExportClipSummary(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        layout.label(text=item.clip_name, icon='ERROR' if item.problem else 'INFO' if item.event_state != 'Available' else 'CHECKMARK')


class WITCH_OT_AnimationExportSummary(bpy.types.Operator):
    bl_idname = 'witcher.animation_export_summary'
    bl_label = 'Export Summary'
    bl_description = 'Review every exported clip: frame range, events, default tracks, and regenerated motion with the current tolerance'
    clips: CollectionProperty(type=WITCH_PG_ExportClipSummary)
    index: IntProperty(default=0)
    fallback_motion: BoolProperty(default=False)

    def collect(self, context):
        self.clips.clear()
        arm = export_anims.get_selected_armature(context)
        from ..CR2W.anims_builder import _make_cutscene_event_props
        for name, action, frame_range, motion in export_targets(context, self.fallback_motion):
            row = self.clips.add()
            row.clip_name = name
            if action is None:
                row.problem = 'Action not found'
                continue
            if ik_rig.has_unbaked_ik_controls(arm, action):
                row.problem = 'Skipped: bake IK to a game action first'
                continue
            row.first, row.last = map(int, frame_range or action.frame_range)
            row.events, row.sounds = event_counts(action, row.first, row.last)
            row.excluded = len(action.witcher_events) - row.events if action.witcher_events_known else 0
            row.unnamed = sum((not e.track_name or e.track_name == events.DEFAULT_TRACK) and e.frame <= row.last + 1e-4 and e.frame + e.duration >= row.first - events.START_SLACK for e in action.witcher_events) if action.witcher_events_known else 0
            row.event_state = 'Available' if action.witcher_events_known else 'Unavailable; no action event data exported'
            row.motion = 'Off'
            try:
                _make_cutscene_event_props(events.export_events(action, events.fps(context.scene), row.first, row.last))
                if motion:
                    if arm is None or 'Trajectory' not in arm.pose.bones:
                        row.motion = 'No Trajectory bone'
                    else:
                        from ..animation import trajectory_tools as tt, motion_extraction as mx
                        frames = list(range(row.first, row.last + 1))
                        with tt.using_action(arm, action):
                            raw = tt.motion_from_trajectory(context, arm, frames)
                        duration = (row.last - row.first) / events.fps(context.scene)
                        settings = context.scene.witcher_motion
                        me = mx.compress(raw, duration, eps=settings.eps, flags=ui_motion.flags_from_settings(settings))
                        row.motion = (f'{len(me["delta_times"]) + 1} keys; max error {mx.max_error(me, raw, duration):.5f}'
                                      if me else 'No movement above thresholds')
            except (ValueError, TypeError, KeyError, RuntimeError) as exc:
                row.problem = str(exc)

    def invoke(self, context, event):
        self.collect(context)
        return context.window_manager.invoke_props_dialog(self, width=560)

    def draw(self, context):
        self.layout.template_list('WITCH_UL_ExportClipSummary', '', self, 'clips', self, 'index', rows=5)
        if 0 <= self.index < len(self.clips):
            item = self.clips[self.index]
            for prop in ('clip_name', 'first', 'last', 'event_state', 'events', 'sounds', 'unnamed', 'excluded', 'motion'):
                self.layout.prop(item, prop)
            if item.problem:
                self.layout.prop(item, 'problem')
        self.layout.prop(context.scene.witcher_motion, 'eps', text='Motion Tolerance')
        self.layout.label(text='Clip values above are an information snapshot.', icon='INFO')
        self.layout.label(text='Reopen after changing export settings.')

    def execute(self, context):
        return {'FINISHED'}


def draw_summary(layout, context, fallback_motion=False, motion_tolerance=False):
    """Draw clip/event counts with export details and event help."""
    targets = export_targets(context, fallback_motion)
    total = sum(event_counts(a, *map(int, bounds or a.frame_range))[0] for _, a, bounds, _ in targets if a)
    row = layout.row(align=True)
    row.label(text=f'{len(targets)} clips, {total} events', icon='EXPORT')
    row.operator('witcher.animation_export_summary', text='', icon='INFO').fallback_motion = fallback_motion
    row.operator('witcher.animation_event_export_info', text='', icon='QUESTION')
    missing = sum(a is None or not a.witcher_events_known for _, a, _, _ in targets)
    if missing:
        layout.label(text=f'{missing} clips: no event data', icon='INFO')
    if motion_tolerance:  # the sidebar already has it under Motion Extraction
        layout.prop(context.scene.witcher_motion, 'eps', text='Motion Tolerance')


CLASSES = (WITCH_OT_AnimationNewClip, WITCH_PG_ExportClipSummary, WITCH_UL_ExportClipSummary, WITCH_OT_AnimationExportSummary)
