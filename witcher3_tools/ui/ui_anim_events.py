"""Animation event editing and track preview."""
import json
import math
import os

import bpy
from bpy.app.handlers import persistent
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, FloatProperty, IntProperty, StringProperty

from ..animation import events, event_tracks
from . import ui_anim_event_fields as typed_fields
from ..CR2W.cutscene_event_schema import (iter_event_classes, event_inherits_from, get_event_class, event_declared_fields,
                                           event_declared_field_names)
from ..CR2W.event_props import STRUCTS, struct_default
from .ui_rig import _find_ik_armature
from .ui_scene import W2SceneFieldItem, draw_w2scene_cr2w_class_info, _w2scene_fill_nested_field_entries

EVENT_TYPES = [(c.name, c.name.removeprefix('CExtAnim'), c.base) for c in iter_event_classes('W3')]
TRACK_GROUPING_ITEMS = (
    ('NAME', 'By Event Name', 'Group events by event name; use the sound name or event class when unnamed'),
    ('CLASS', 'By Event Class', 'Group events by REDengine class'),
)


def context_clip(context):
    owner = _find_ik_armature(context)
    action, strip = events.clip(owner, context.scene.frame_current_final, selected=True)
    return owner, action, strip


def event_class_names(self, context, edit_text):
    return [(name, base) for name, _, base in EVENT_TYPES]


def track_names(self, context, edit_text):
    action = self.id_data if isinstance(self.id_data, bpy.types.Action) else context_clip(context)[1]
    return sorted({name for _, name in events.track_keys(action)}) if action else [events.DEFAULT_TRACK]


def event_classes(self, context, edit_text):
    action = context_clip(context)[1]
    return sorted({e.event_type for e in action.witcher_events}) if action else []


def update_event_track(self, context):
    events.sync_tracks(self.id_data)
    event_tracks.tag_redraw(context=context)


# Persist toggles across Blender sessions.
REMEMBERED = ('witcher_event_tracks', 'witcher_event_sounds', 'witcher_event_effects',
              'witcher_effects_trails', 'witcher_effects_blood')
STATE_FILE = None
_restoring = False


def _state_path():
    if STATE_FILE:
        return STATE_FILE
    if bpy.app.background:  # tests and batch runs neither read nor change the user's choices
        return None
    return os.path.join(bpy.utils.user_resource('CONFIG', path='witcher3_tools', create=True), 'animation_events_ui.json')


def remember(scene, context=None):
    path = _state_path()
    if path and not _restoring:
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump({name: bool(getattr(scene, name)) for name in REMEMBERED}, f)
        except OSError:
            pass


def restore(scene):
    """Restore toggles; defer sound loading and effect baking until use."""
    global _restoring
    path = _state_path()
    if not path or not os.path.isfile(path):
        return
    from ..animation import event_preview
    try:
        with open(path, encoding='utf-8') as f:
            state = json.load(f)
    except (OSError, ValueError):
        return
    _restoring = True
    try:
        for name in REMEMBERED:
            if name == 'witcher_event_sounds':
                event_preview._set_enabled(scene, bool(state.get(name, False)))
            elif name in state:
                setattr(scene, name, bool(state[name]))
    finally:
        _restoring = False


def toggle_tracks(self, context):
    event_tracks.tag_redraw(self, context)
    remember(self)


def toggle_sounds(self, context):
    from ..animation import event_preview
    event_preview.toggled(self, context)
    remember(self)


def select_track(self, context):
    index = self.witcher_event_track_index
    if context_clip(context)[1] == self and 0 <= index < len(self.witcher_event_tracks):
        context.scene.witcher_event_track_filter = self.witcher_event_tracks[index].name
    event_tracks.tag_redraw(context=context)


@persistent
def restore_track_layout(_unused=None):
    for action in bpy.data.actions:
        if action.witcher_events or action.witcher_event_tracks or action.witcher_events_known:
            events.sync_tracks(action)
    for scene in bpy.data.scenes:
        restore(scene)


def matches_filter(item, scene):
    query = scene.witcher_event_search.casefold()
    return (query in ' '.join((item.event_name, item.sound_event, item.event_type, item.track_name or events.DEFAULT_TRACK)).casefold()
            and (not scene.witcher_event_class_filter or item.event_type == scene.witcher_event_class_filter)
            and (not scene.witcher_event_track_filter or (item.track_name or events.DEFAULT_TRACK) == scene.witcher_event_track_filter))


class WITCH_PG_AnimationEvent(bpy.types.PropertyGroup):
    event_type: StringProperty(name='Class', default='CExtAnimEvent', description='REDengine event class; it decides which fields the event has',
                               search=event_class_names, search_options={'SORT'}, update=event_tracks.tag_redraw)
    event_name: StringProperty(name='Event', default='Event', description='Name of this event, independent of its track', update=event_tracks.tag_redraw)
    track_name: StringProperty(name='Track', description='Event grouping within this clip; choose an existing track or type a new name. This is not an NLA track', search=track_names, update=update_event_track)
    track_display_name: StringProperty(name='Track', description='Empty names use the REDkit __Default track; named tracks are exported', search=track_names,
        get=lambda self: self.track_name or events.DEFAULT_TRACK,
        set=lambda self, value: setattr(self, 'track_name', '' if value == events.DEFAULT_TRACK else value))
    frame: FloatProperty(name='Frame', precision=3, description='Start frame within the action; NLA timing is mapped automatically', update=event_tracks.tag_redraw)
    duration: FloatProperty(name='Duration', min=0, precision=3, description='Duration in action frames', update=event_tracks.tag_redraw)
    sound_event: StringProperty(name='Sound Event', description='Wwise event name from the game soundbanks', update=event_tracks.tag_redraw)
    source_game: StringProperty(default='W3')
    source_file: StringProperty(name='Event File', description='Linked REDkit event file supplying this event or its recovered track name', update=update_event_track)
    payload: StringProperty(name='Fields', default='{}')


class WITCH_PG_EventTrack(bpy.types.PropertyGroup):
    name: StringProperty(name='Track', update=event_tracks.tag_redraw)
    source_file: StringProperty(name='Event File')
    locked: BoolProperty(name='Protect from Auto Create', default=False, update=event_tracks.tag_redraw)


class WITCH_UL_AnimationEvents(bpy.types.UIList):
    def draw_filter(self, context, layout):
        pass

    def filter_items(self, context, data, propname):
        return [self.bitflag_filter_item if matches_filter(e, context.scene) else 0
                for e in getattr(data, propname)], []

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        row.operator('witcher.animation_event_jump', text='', icon='PLAY', emboss=False).index = index
        row.label(text=item.event_name or item.sound_event or item.event_type,
                  icon_value=event_tracks.icon_value(event_tracks.event_icon(item)))
        frame = row.row()
        frame.alignment = 'RIGHT'
        frame.label(text=f'{item.frame:.3g}' + (f'+{item.duration:.3g}' if item.duration else ''))


class WITCH_UL_EventTracks(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        row.prop(item, 'locked', text='', icon='LOCKED' if item.locked else 'UNLOCKED', emboss=False)
        items = [(i, e) for i, e in enumerate(data.witcher_events)
                 if e.source_file == item.source_file and (e.track_name or events.DEFAULT_TRACK) == item.name]
        row.label(text=item.name, icon_value=event_tracks.icon_value(event_tracks.track_icon(item.name, items)))


class WITCH_OT_EventTrackMove(bpy.types.Operator):
    bl_idname = 'witcher.event_track_move'
    bl_label = 'Move Event Track'
    bl_description = 'Reorder this clip\'s tracks in Blender; __Default stays first and event timing is unchanged'
    bl_options = {'REGISTER', 'UNDO'}
    direction: EnumProperty(items=(('TOP', 'Top', ''), ('UP', 'Up', ''), ('DOWN', 'Down', ''), ('BOTTOM', 'Bottom', '')))

    @classmethod
    def poll(cls, context):
        action = context_clip(context)[1]
        return action is not None and 0 < action.witcher_event_track_index < len(action.witcher_event_tracks)

    def execute(self, context):
        action = context_clip(context)[1]
        track = action.witcher_event_tracks[action.witcher_event_track_index]
        events.move_track(action, track.source_file, track.name, self.direction)
        event_tracks.tag_redraw(context=context)
        return {'FINISHED'}


class WITCH_OT_AnimationEventAdd(bpy.types.Operator):
    bl_idname = 'witcher.animation_event_add'
    bl_label = 'Add Animation Event'
    bl_description = 'Create an event at the playhead on the chosen event track; existing events are unchanged'
    bl_options = {'REGISTER', 'UNDO'}
    event_type: EnumProperty(name='Class', items=EVENT_TYPES, default='CExtAnimEvent', description='REDengine class controlling this event and its available fields')
    event_name: StringProperty(name='Name', default='Event', description='Name of the new event, independent of its track')
    track_name: StringProperty(name='Track', description='Choose an existing event track or type a new grouping name; does not create an NLA track', search=track_names)

    def invoke(self, context, event):
        _, action, _ = context_clip(context)
        if action is None:
            return {'CANCELLED'}
        self.track_name = context.scene.witcher_event_track_filter or self.default_track(action)
        kind = context.scene.witcher_event_class_filter
        if kind in {item[0] for item in EVENT_TYPES}:
            self.event_type = kind
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        self.layout.prop(self, 'event_type')
        self.layout.prop(self, 'event_name')
        self.layout.prop(self, 'track_name')
        self.layout.label(text='Choose a track or type a new name', icon='INFO')

    def default_track(self, action):
        index = action.witcher_event_index
        return (action.witcher_events[index].track_name or events.DEFAULT_TRACK) if 0 <= index < len(action.witcher_events) else events.DEFAULT_TRACK

    def execute(self, context):
        _, action, strip = context_clip(context)
        if action is None:
            self.report({'ERROR'}, 'Choose an animation action first')
            return {'CANCELLED'}
        track = self.track_name or self.default_track(action)
        if track == events.DEFAULT_TRACK:
            track = ''
        event = action.witcher_events.add()
        event.event_type, event.event_name, event.track_name = self.event_type, self.event_name, track
        event.frame = events.action_frame(strip, context.scene.frame_current_final)
        if event_inherits_from('W3', self.event_type, 'CExtAnimDurationEvent'):
            event.duration = 1
        # SEnumVariant has its own editor; other structs need their members to be editable at all.
        structs = {f.name: struct_default(f.type_name) for f in event_declared_fields('W3', self.event_type)
                   if f.type_name in STRUCTS and f.type_name != 'SEnumVariant'}
        if structs:
            event.payload = json.dumps({'raw_fields': structs})
        action.witcher_events_known = True
        action.witcher_event_index = len(action.witcher_events) - 1
        if not matches_filter(event, context.scene):
            bpy.ops.witcher.animation_event_clear_filters()
        return {'FINISHED'}


class WITCH_OT_AnimationEventClearFilters(bpy.types.Operator):
    bl_idname = 'witcher.animation_event_clear_filters'
    bl_label = 'Clear Event Filters'
    bl_description = 'Show all events by clearing the text, class and track filters; does not change any event'

    def execute(self, context):
        context.scene.witcher_event_search = ''
        context.scene.witcher_event_class_filter = ''
        context.scene.witcher_event_track_filter = ''
        return {'FINISHED'}


class WITCH_OT_AnimationEventRemove(bpy.types.Operator):
    bl_idname = 'witcher.animation_event_remove'
    bl_label = 'Remove Animation Event'
    bl_description = 'Delete the selected event from this clip and stop sound preview; other events on its track are kept'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        _, action, _ = context_clip(context)
        if action is None or not action.witcher_events:
            return {'CANCELLED'}
        action.witcher_events.remove(action.witcher_event_index)
        action.witcher_event_index = min(action.witcher_event_index, max(0, len(action.witcher_events) - 1))
        action.witcher_events_known = True
        from ..animation.event_preview import stop
        stop()
        event_tracks.tag_redraw(context=context)
        return {'FINISHED'}


class WITCH_OT_AnimationEventJump(bpy.types.Operator):
    bl_idname = 'witcher.animation_event_jump'
    bl_label = 'Go to Event'
    bl_description = 'Select this event and move the playhead to its start, accounting for NLA strip timing'
    index: IntProperty(default=-1)

    def execute(self, context):
        _, action, strip = context_clip(context)
        index = self.index if self.index >= 0 else action.witcher_event_index if action else -1
        if action is None or not 0 <= index < len(action.witcher_events):
            return {'CANCELLED'}
        action.witcher_event_index = index
        frame = action.witcher_events[index].frame
        if strip is not None:
            frame = strip.frame_start + (strip.action_frame_end - frame if strip.use_reverse else frame - strip.action_frame_start) * strip.scale
        whole = int(frame // 1)
        context.scene.frame_set(whole, subframe=frame - whole)
        return {'FINISHED'}


class WITCH_OT_AnimationEventMove(bpy.types.Operator):
    bl_idname = 'witcher.animation_event_move'
    bl_label = 'Move to Playhead'
    bl_description = 'Move the selected event to the current playhead, accounting for NLA strip timing; keep its duration'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        _, action, strip = context_clip(context)
        if action is None or not action.witcher_events:
            return {'CANCELLED'}
        action.witcher_events[action.witcher_event_index].frame = events.action_frame(strip, context.scene.frame_current_final)
        return {'FINISHED'}


class WITCH_OT_AnimationEventFields(bpy.types.Operator):
    bl_idname = 'witcher.animation_event_fields'
    bl_label = 'Event Fields'
    bl_description = 'Inspect or edit this event\'s REDengine fields, grouped by class, including inherited and unset fields'
    bl_options = {'REGISTER', 'UNDO'}
    class_fields: CollectionProperty(type=W2SceneFieldItem)
    editors: CollectionProperty(type=typed_fields.WITCH_PG_EventFieldEditor)
    original: StringProperty()
    event_class: StringProperty(name='Class', search=event_class_names, search_options={'SORT'})

    def invoke(self, context, event):
        _, action, _ = context_clip(context)
        if action is None or not action.witcher_events:
            return {'CANCELLED'}
        item = action.witcher_events[action.witcher_event_index]
        raw = json.loads(item.payload).get('raw_fields', {})
        # Clip-relative seconds, as exported.
        raw.update(eventName=item.event_name, animationName=action.name, trackName=item.track_name,
                   startTime=(item.frame - action.frame_range[0]) / events.fps(context.scene))
        if item.sound_event or 'soundEventName' in raw:
            raw['soundEventName'] = item.sound_event
        if event_inherits_from(item.source_game, item.event_type, 'CExtAnimDurationEvent'):
            raw['duration'] = item.duration / events.fps(context.scene)
        self.original = json.dumps(raw)
        self.event_class = item.event_type
        self.class_fields.clear()
        self.editors.clear()
        classes, cls = [], get_event_class(item.source_game, item.event_type)
        while cls is not None:
            classes.insert(0, cls)
            cls = get_event_class(item.source_game, cls.base)
        declared = set()
        for cls in classes:
            for field in cls.own_fields:
                declared.add(field.name)
                self.add_field(cls.name, field.name, field.type_name, raw)
        types = json.loads(item.payload).get('field_types', {})
        for name in raw.keys() - declared:
            kind = types.get(name, '')
            self.add_field(item.event_type, name, kind.get('type', '') if isinstance(kind, dict) else kind, raw)
        return context.window_manager.invoke_props_dialog(self, width=650)

    def add_field(self, cls, name, kind, raw):
        field = self.class_fields.add()
        field.class_name, field.field_name, field.type_text = cls, name, kind
        field.is_set = name in raw
        field.value_text = field_text(raw[name]) if field.is_set else '<unset>'
        editor = self.editors.add()
        editor.name = name
        typed_fields.prepare(field, raw.get(name), editor)
        if field.is_set:
            _w2scene_fill_nested_field_entries(field.children, raw[name])
            field.has_children = bool(field.children)
            for child in field.children:
                child.value_text = field_text(nested_value(raw[name], child.item_key))

    def draw(self, context):
        self.layout.prop(self, 'event_class')
        draw_w2scene_cr2w_class_info(self.layout, self, panel_id='animation_event_fields', draw_field=lambda layout, field: typed_fields.draw_field(layout, field, self.editors.get(field.field_name)))

    def execute(self, context):
        _, action, _ = context_clip(context)
        if action is None or not action.witcher_events:
            return {'CANCELLED'}
        try:
            raw = json.loads(self.original)
            original = json.loads(self.original)
            for field in self.class_fields:
                editor = self.editors.get(field.field_name) if hasattr(self, 'editors') else None
                typed_fields.sync(field, editor)
                name = field.field_name
                if field.value_text == '<unset>':
                    raw.pop(name, None)
                    continue
                if field.value_text != field_text(original.get(name)):
                    kind = field.type_text
                    raw[name] = (field.value_text if kind in {'CName', 'String', 'StringAnsi'} or (kind.startswith('E') and kind != 'EulerAngles') or kind.startswith(('handle:', 'soft:', 'ptr:'))
                                 else json.loads(field.value_text))
                for child in ([] if getattr(editor, 'kind', '') else field.children):
                    old = nested_value(original[name], child.item_key)
                    if child.value_text != field_text(old):
                        nested_value(raw[name], child.item_key, child.value_text if isinstance(old, str) else json.loads(child.value_text), write=True)
            rate = events.fps(context.scene)
            frame, duration = float(raw.get('startTime', 0)) * rate + action.frame_range[0], float(raw.get('duration', 0)) * rate
            if duration < 0 or not all(math.isfinite(v) for v in (frame, duration)):
                raise ValueError('Event times must be finite and duration cannot be negative')
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        item = action.witcher_events[action.witcher_event_index]
        if raw == original and self.event_class == item.event_type:
            return {'CANCELLED'}  # nothing edited: keep the clip unmodified
        data = json.loads(item.payload)
        data['raw_fields'] = raw
        item.payload = json.dumps(data)
        item.event_type = self.event_class
        item.sound_event = raw.get('soundEventName', '')
        item.event_name, item.track_name = raw.get('eventName', ''), raw.get('trackName', '')
        item.frame, item.duration = frame, duration
        return {'FINISHED'}


def field_text(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def nested_value(value, path, replacement=None, write=False):
    indices = [int(i) for i in path.split('/')]
    for index in indices[:-1]:
        value = value[list(value)[index]] if isinstance(value, dict) else value[index]
    key = list(value)[indices[-1]] if isinstance(value, dict) else indices[-1]
    if write:
        value[key] = replacement
    return value[key]


class WITCH_OT_AnimationEventAudio(bpy.types.Operator):
    bl_idname = 'witcher.animation_event_audio'
    bl_label = 'Preview Sounds'
    bl_description = 'Prepare game sound events for this character and clip; stops on clip changes, deletion or stopping playback'
    stop: BoolProperty(default=False)
    audition: BoolProperty(default=False)

    @classmethod
    def description(cls, context, properties):
        if properties.stop:
            return 'Stop all event preview sounds and disable sound playback for this clip'
        if properties.audition:
            return 'Play the selected sound event now; also prepare this clip\'s sounds for timeline playback'
        return 'Prepare this clip\'s game sounds, then play the timeline to hear them at their event frames; stops on clip changes or character deletion'

    def execute(self, context):
        from ..animation import event_preview
        if self.stop:
            context.scene.witcher_event_sounds = False
            return {'FINISHED'}
        owner, action, strip = context_clip(context)
        if owner is None or action is None:
            return {'CANCELLED'}
        try:
            event_preview.prepare(context, owner, action, strip, audition=self.audition)
        except (OSError, ValueError, RuntimeError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class WITCH_OT_AnimationEventTrails(bpy.types.Operator):
    bl_idname = 'witcher.animation_event_trails'
    bl_label = 'Bake Weapon Trails'
    bl_description = ('Bake the held weapon\'s trail events for this action or NLA strip. Pauses playback, and puts the equipped '
                      'sword in the hand (or a default steel sword when none is equipped). Rebake after editing motion, events, '
                      'equipment or timing. View in Material Preview or Rendered mode')
    bl_options = {'REGISTER', 'UNDO'}
    clear: BoolProperty(default=False, options={'HIDDEN'})
    blood: BoolProperty(default=False, options={'HIDDEN'})

    @classmethod
    def description(cls, context, properties):
        if properties.blood:
            return ('Simulate ordinary red-blood hits at BloodTrail/BloodTrailForced markers using the held weapon. '
                    'Starts its default blood particles at each marker, ignoring the effect-track delay; no victim or collision required')
        if properties.clear:
            return 'Remove this character and clip\'s generated light and blood trails'
        return cls.bl_description

    @classmethod
    def poll(cls, context):
        return context_clip(context)[1] is not None

    def execute(self, context):
        from ..animation import event_effects
        owner, action, strip = context_clip(context)
        if self.clear:
            count = event_effects.clear(context.scene, owner, action)
            self.report({'INFO'}, f'Cleared {count} weapon trails')
            return {'FINISHED'}
        try:
            objects, skipped, notes = event_effects.bake(context, owner, action, strip, blood=self.blood)
        except (ValueError, OSError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, '. '.join(notes + [f'Baked {len(objects)} weapon trails; view in Material Preview or Rendered mode']))
        if skipped:
            self.report({'WARNING'}, '; '.join(skipped))
        return {'FINISHED'}


class WITCH_OT_EventEffects(bpy.types.Operator):
    bl_idname = 'witcher.event_effects'
    bl_label = 'Weapon Effects'
    bl_description = ('Show this clip\'s weapon trails. Bakes them for the current clip, pausing playback and putting a sword in '
                      'the hand when needed, and again for each clip you load while this is on. Off hides them')
    bl_options = {'REGISTER', 'UNDO'}
    rebake: BoolProperty(default=False, options={'HIDDEN', 'SKIP_SAVE'})

    @classmethod
    def description(cls, context, properties):
        return 'Bake the enabled effects again after editing motion, events, equipment or timing' if properties.rebake else cls.bl_description

    def execute(self, context):
        from ..animation import event_effects
        scene = context.scene
        if not self.rebake:
            scene.witcher_event_effects = not scene.witcher_event_effects
        if not scene.witcher_event_effects and not self.rebake:
            event_effects.set_visible(scene, False)
            return {'FINISHED'}
        owner, action, strip = context_clip(context)
        if action is None:
            self.report({'INFO'}, 'Effects are on; they bake when a clip is loaded')
            return {'FINISHED'}
        count, messages = event_effects.refresh(context, owner, action, strip)
        self.report({'INFO'}, '. '.join(messages + [f'{count} weapon effects baked; view in Material Preview or Rendered mode']))
        return {'FINISHED'}


class WITCH_OT_EventEffectsSettings(bpy.types.Operator):
    bl_idname = 'witcher.event_effects_settings'
    bl_label = 'Weapon Effect Settings'
    bl_description = 'Choose which weapon effects are baked for a clip'

    def invoke(self, context, event):
        return context.window_manager.invoke_popup(self, width=300)

    def draw(self, context):
        scene = context.scene
        box = self.layout.box()
        box.label(text='Baked for each clip')
        box.prop(scene, 'witcher_effects_trails')
        box.prop(scene, 'witcher_effects_blood')
        box.label(text='Rebake to apply a change', icon='INFO')

    def execute(self, context):
        return {'FINISHED'}


class WITCH_OT_AnimationEventAudioInfo(bpy.types.Operator):
    bl_idname = 'witcher.animation_event_audio_info'
    bl_label = 'Sound Preview Details'
    bl_description = 'Show the prepared clip, sound event names and cached audio files, including variants; these are not additional timeline events'
    character: StringProperty(name='Character', description='Character whose sounds are prepared; copyable information only')
    clip: StringProperty(name='Clip', description='Animation whose sounds are prepared; copyable information only')
    event_names: StringProperty(name='Sound event names', description='Distinct Wwise sound event names referenced by this clip; copyable information only')
    cues: IntProperty(name='Sound cues in clip', description='Number of events with a sound name in this action, including repeated names')
    names: IntProperty(name='Distinct sound event names', description='Number of different Wwise sound event names referenced by this action')
    variants: IntProperty(name='Cached audio files', description='Decoded audio files available to these sound events, including layers and random or surface variants')

    status: StringProperty(name='Status', description='What the Sounds toggle is doing right now; copyable information only')

    def invoke(self, context, event):
        from ..animation import event_preview
        preview = event_preview._preview
        scene = getattr(context, 'scene', None) or bpy.context.scene
        self.character = self.clip = self.event_names = ''
        self.cues = self.names = self.variants = 0
        if preview is not None and preview.valid():
            names = [e.sound_event for e in preview.action.witcher_events if e.sound_event]
            self.character, self.clip = preview.owner.name, preview.action.name
            self.cues, self.names, self.variants = len(names), len(set(names)), len(preview.player.wavs)
            self.event_names = ', '.join(sorted(set(names)))
        self.status = (scene.witcher_event_audio_status or 'On: sounds load when playback starts'
                       if scene.witcher_event_sounds else 'Off')
        return context.window_manager.invoke_props_dialog(self, width=540)

    def draw(self, context):
        for name in ('status', 'character', 'clip', 'cues', 'names', 'variants', 'event_names'):
            self.layout.prop(self, name)
        self.layout.label(text='Audio files include layers, random and ground-surface variants.')
        self.layout.label(text='Playback chooses the matching sounds when each event fires.')
        self.layout.label(text='These files do not add events or tracks to the scene.')

    def execute(self, context):
        return {'FINISHED'}


class WITCH_OT_AnimationEventExportInfo(bpy.types.Operator):
    bl_idname = 'witcher.animation_event_export_info'
    bl_label = 'Exported Event Data'
    bl_description = 'Explain which animation event data is exported, including timing, tracks and sound references'

    def invoke(self, context, event):
        return context.window_manager.invoke_popup(self, width=510)

    def draw(self, context):
        box = self.layout.box()
        box.label(text='Included with the animation', icon='INFO')
        box.label(text='Events stored on the exported action are included automatically.')
        box.label(text='Includes event classes, names, track names and event fields.')
        box.label(text='Only events overlapping the exported frame range are included.')
        box.label(text='Timing is relative to the clip start; durations are trimmed to fit.')
        box.label(text='Event list filters and Show Tracks do not affect export.')
        box.label(text='Sound event references are included; audio files are not embedded.')
        box.label(text='Review or edit the data in the Events tab before exporting.')
        box = self.layout.box()
        box.label(text='Replacing a clip in an existing set')
        box.label(text='Unchanged or unavailable action event data keeps target events.')
        box.label(text='Edited action events replace target events, including deletions.')
        box.label(text='Linked event files are checked to avoid duplicate sound cues.')

    def execute(self, context):
        return {'FINISHED'}


class WITCH_OT_EventTrackInfo(bpy.types.Operator):
    bl_idname = 'witcher.event_track_info'
    bl_label = 'Event Track Names'
    bl_description = 'Explain stored tracks, the REDkit default track, recovery and display grouping'

    def invoke(self, context, event):
        return context.window_manager.invoke_popup(self, width=540)

    def draw(self, context):
        for text in (
            'A track is a name stored on each event; unnamed events sit on __Default.',
            'Track names are exported. Row order, empty tracks and locks stay in the .blend.',
            'Cooked game clips have no track names: Recover REDkit Track Names (Sets tab)',
            'restores them from the matching depot set on import.',
            'Auto Create Tracks names tracks after the event name or class;',
            'locked tracks are left alone. Grouping never changes event timing.',
        ):
            self.layout.label(text=text)
        box = self.layout.box()
        box.label(text='Track Icons')
        for icon, text in (('PINNED', 'Pinned default track'), ('SOUND', 'Sound events only'),
                           ('LINKED', 'Linked event file'), ('NLA', 'Other or mixed events'),
                           ('LOCKED', 'Protected from Auto Create')):
            box.label(text=text, icon_value=event_tracks.icon_value(icon))

    def execute(self, context):
        return {'FINISHED'}


class WITCH_OT_EventTrackNames(bpy.types.Operator):
    bl_idname = 'witcher.event_track_names'
    bl_label = 'Auto Create Tracks'
    bl_description = 'Group events by name or class, including existing unlocked tracks. Protected tracks and event timing are kept'
    bl_options = {'REGISTER', 'UNDO'}
    group_by: EnumProperty(name='Group Events', items=TRACK_GROUPING_ITEMS, default='NAME')
    scope: EnumProperty(name='Apply To', items=(
        ('UNLOCKED', 'All Unlocked Tracks', 'Regroup existing tracks as well as __Default; protected tracks are unchanged'),
        ('DEFAULT', 'Only __Default', 'Assign unnamed/default events while keeping existing named tracks'),
    ), default='UNLOCKED')

    @classmethod
    def poll(cls, context):
        action = context_clip(context)[1]
        return action is not None and bool(action.witcher_events)

    def invoke(self, context, event):
        if not self.properties.is_property_set('group_by'):
            mode = context.scene.witcher_anim_auto_tracks
            self.group_by = mode if mode != 'NONE' else 'NAME'
        return context.window_manager.invoke_props_dialog(self, width=420)

    def draw(self, context):
        self.layout.prop(self, 'group_by')
        self.layout.prop(self, 'scope')
        assignments, protected = events.track_assignments(context_clip(context)[1], self.group_by, self.scope)
        self.layout.label(text=f'{len(assignments)} events to regroup; {protected} protected', icon='INFO')
        self.layout.label(text='Keeps every event, its timing and empty tracks.')
        self.layout.label(text='New track names are saved and exported.', icon='INFO')

    def execute(self, context):
        _, action, _ = context_clip(context)
        if action is None:
            return {'CANCELLED'}
        changed = events.create_tracks(action, self.group_by, self.scope)
        if changed:
            context.scene.witcher_event_track_filter = ''
        event_tracks.tag_redraw(context=context)
        self.report({'INFO'}, f'Assigned tracks to {changed} events')
        return {'FINISHED'}


class WITCH_OT_EventTrackAdd(bpy.types.Operator):
    bl_idname = 'witcher.event_track_add'
    bl_label = 'Add Blank Track'
    bl_description = 'Create an empty event track for this clip, saved in the blend file'
    bl_options = {'REGISTER', 'UNDO'}
    track_name: StringProperty(name='Name', default='New Track')

    @classmethod
    def poll(cls, context):
        return context_clip(context)[1] is not None

    def invoke(self, context, event):
        names = {name for _, name in events.track_keys(context_clip(context)[1])}
        self.track_name = 'New Track'
        number = 2
        while self.track_name in names:
            self.track_name = f'New Track {number}'
            number += 1
        return context.window_manager.invoke_props_dialog(self, width=420)

    def draw(self, context):
        self.layout.prop(self, 'track_name')
        self.layout.label(text='Empty tracks are saved in this .blend.', icon='INFO')
        self.layout.label(text='Exported track names are carried by their events.')

    def execute(self, context):
        try:
            name = events.add_track(context_clip(context)[1], self.track_name)
        except ValueError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        bpy.ops.witcher.animation_event_clear_filters()
        context.scene.witcher_event_track_filter = name
        context.scene.witcher_event_tracks = True
        event_tracks.tag_redraw(context=context)
        return {'FINISHED'}


# Keep dynamic enum strings and numeric IDs stable for Blender's popup and redo.
_TRACK_CHOICES = {}


def all_tracks(self, context):
    action = context_clip(context)[1] if context else None
    items = []
    for source, name in sorted(events.track_keys(action) if action else []):
        key = json.dumps([source, name])
        if key not in _TRACK_CHOICES:
            label = name + (' (' + source.replace('\\', '/').rsplit('/', 1)[-1] + ')' if source else '')
            _TRACK_CHOICES[key] = (key, label, source or 'This clip', len(_TRACK_CHOICES) + 1)
        items.append(_TRACK_CHOICES[key])
    return items


def deletable_tracks(self, context):
    return [item for item in all_tracks(self, context) if json.loads(item[0])[1] != events.DEFAULT_TRACK]


def selected_track(context, choices):
    action = context_clip(context)[1]
    selected = action.witcher_event_index
    key = json.dumps([action.witcher_events[selected].source_file, action.witcher_events[selected].track_name or events.DEFAULT_TRACK]) if 0 <= selected < len(action.witcher_events) else ''
    filtered = context.scene.witcher_event_track_filter
    if filtered:
        key = next((item[0] for item in choices if json.loads(item[0])[1] == filtered), key)
    return key if key in {item[0] for item in choices} else choices[0][0]


def update_track_lock(self, context):
    if self.track:
        self.locked = tuple(json.loads(self.track)) in events.locked_tracks(context_clip(context)[1])


class WITCH_OT_EventTrackLock(bpy.types.Operator):
    bl_idname = 'witcher.event_track_lock'
    bl_label = 'Track Locks'
    bl_description = 'Protect a track from Auto Create; events cannot be moved into or out of it by regrouping'
    bl_options = {'REGISTER', 'UNDO'}
    track: EnumProperty(name='Track', items=all_tracks, update=update_track_lock)
    locked: BoolProperty(name='Protect from Auto Create', default=False)

    @classmethod
    def poll(cls, context):
        return context_clip(context)[1] is not None

    def invoke(self, context, event):
        self.track = selected_track(context, all_tracks(self, context))
        return context.window_manager.invoke_props_dialog(self, width=440)

    def draw(self, context):
        self.layout.prop(self, 'track')
        self.layout.prop(self, 'locked', icon='LOCKED' if self.locked else 'UNLOCKED')
        self.layout.label(text='Stops Auto Create moving events in or out.')
        self.layout.label(text='Manual edits and Delete Track remain available.')

    def execute(self, context):
        try:
            source, name = json.loads(self.track)
            events.set_track_locked(context_clip(context)[1], name, source, self.locked)
        except ValueError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        event_tracks.tag_redraw(context=context)
        return {'FINISHED'}


class WITCH_OT_EventTrackDelete(bpy.types.Operator):
    bl_idname = 'witcher.event_track_delete'
    bl_label = 'Delete Track'
    bl_description = 'Delete a named track; move its events to __Default or delete them. The default track is permanent'
    bl_options = {'REGISTER', 'UNDO'}
    track: EnumProperty(name='Track', items=deletable_tracks)
    event_handling: EnumProperty(name='Events', items=(
        ('MOVE', 'Move to __Default', 'Keep every event and its timing on the default track'),
        ('DELETE', 'Delete Events', 'Delete all events on the selected track from this clip'),
    ), default='MOVE')

    @classmethod
    def poll(cls, context):
        return bool(deletable_tracks(None, context))

    def invoke(self, context, event):
        self.track = selected_track(context, deletable_tracks(self, context))
        self.event_handling = 'MOVE'
        return context.window_manager.invoke_props_dialog(self, width=440)

    def draw(self, context):
        self.layout.prop(self, 'track')
        if not self.track:
            return
        source, name = json.loads(self.track)
        action = context_clip(context)[1]
        count = sum(e.track_name == name and e.source_file == source for e in action.witcher_events)
        self.layout.label(text=f'{count} events on this track', icon='INFO')
        if count:
            self.layout.prop(self, 'event_handling')
        if source:
            self.layout.label(text='Only the selected event-file track is affected.')

    def execute(self, context):
        filtered = context.scene.witcher_event_track_filter
        try:
            source, name = json.loads(self.track)
            count = events.delete_track(context_clip(context)[1], name, source, self.event_handling == 'DELETE')
        except ValueError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        context.scene.witcher_event_track_filter = '' if filtered == name else filtered
        from ..animation.event_preview import stop
        stop()
        event_tracks.tag_redraw(context=context)
        message = (f'Deleted track and {count} events' if self.event_handling == 'DELETE'
                   else f'Deleted track; moved {count} events to __Default') if count else 'Deleted empty track'
        self.report({'INFO'}, message)
        return {'FINISHED'}


class WITCH_OT_EventTrackSettings(bpy.types.Operator):
    bl_idname = 'witcher.event_track_settings'
    bl_label = 'Event Track View'
    bl_description = 'Position and size the event overlay; zoom and pan independently of the viewport'

    def invoke(self, context, event):
        return context.window_manager.invoke_popup(self, width=320)

    def draw(self, context):
        scene = context.scene
        box = self.layout.box()
        box.label(text='Track Grouping')
        box.prop(scene, 'witcher_tracks_show_empty')
        box.prop(scene, 'witcher_tracks_group_unnamed')
        box.operator('witcher.event_track_names')
        box.prop(scene, 'witcher_anim_auto_tracks', text='On Import')
        box.operator('witcher.event_track_info', icon='INFO')
        box = self.layout.box()
        box.label(text='Placement')
        box.prop(scene, 'witcher_tracks_position', expand=True)
        box.prop(scene, 'witcher_tracks_margin')
        box.prop(scene, 'witcher_tracks_fit')
        row = box.row()
        row.enabled = not scene.witcher_tracks_fit
        row.prop(scene, 'witcher_tracks_width')
        box = self.layout.box()
        box.label(text='Time View')
        box.prop(scene, 'witcher_tracks_zoom')
        box.prop(scene, 'witcher_tracks_pan')
        box.operator('witcher.event_tracks_frame_all', icon='ZOOM_ALL')

    def execute(self, context):
        return {'FINISHED'}


class WITCH_OT_EventTracksFrameAll(bpy.types.Operator):
    bl_idname = 'witcher.event_tracks_frame_all'
    bl_label = 'Frame All'
    bl_description = 'Reset event track zoom and pan to show the complete clip'

    def execute(self, context):
        context.scene.witcher_tracks_zoom = 1
        context.scene.witcher_tracks_pan = 0
        return {'FINISHED'}


TRACK_VIEW_PROPERTIES = {
    'witcher_tracks_show_empty': BoolProperty(name='Show Empty Tracks', description='Show empty rows in the viewport; __Default always remains visible and Track Order lists every track', default=True, update=event_tracks.tag_redraw),
    'witcher_tracks_group_unnamed': BoolProperty(name='Group Unnamed by Class', description='Display-only grouping; unnamed events still export on REDkit __Default unless you assign track names', default=False, update=event_tracks.tag_redraw),
    'witcher_tracks_position': EnumProperty(name='Position', items=(('TOP', 'Top', ''), ('BOTTOM', 'Bottom', '')), default='TOP', update=event_tracks.tag_redraw),
    'witcher_tracks_margin': IntProperty(name='Vertical Offset', default=0, min=0, max=2000, update=event_tracks.tag_redraw),
    'witcher_tracks_fit': BoolProperty(name='Fit Viewport Width', default=True, update=event_tracks.tag_redraw),
    'witcher_tracks_width': IntProperty(name='Width', description='Overlay width in UI pixels; limited by available space', default=800, min=260, max=4000, update=event_tracks.tag_redraw),
    'witcher_tracks_zoom': FloatProperty(name='Zoom', description='Time magnification; 1 shows the full clip', default=1, min=0.1, max=100, update=event_tracks.tag_redraw),
    'witcher_tracks_pan': FloatProperty(name='Pan (Frames)', description='Offset from the beginning of the clip in action frames', default=0, update=event_tracks.tag_redraw),
}


def _section(layout, idname, text, icon):
    header, body = layout.panel(idname, default_closed=True)
    header.label(text=text, icon=icon)
    return header, body


def draw(layout, context):
    scene = context.scene
    owner, action, strip = context_clip(context)
    layout.prop(scene, 'witcher_main_armature', text='Character')
    if action is None:
        layout.label(text='Choose an action or NLA strip', icon='INFO')
        return
    layout.prop(action, 'name', text='Clip')
    row = layout.row(align=True)
    row.prop(scene, 'witcher_event_tracks', text='Tracks', icon='HIDE_OFF' if scene.witcher_event_tracks else 'HIDE_ON', toggle=True)
    row.prop(scene, 'witcher_event_sounds', text='Sounds', icon='OUTLINER_OB_SPEAKER' if scene.witcher_event_sounds else 'MUTE_IPO_ON',
             toggle=True)
    row.operator('witcher.animation_event_audio_info', text='', icon='INFO')
    row.operator('witcher.event_effects', text='Effects', icon='PARTICLES', depress=scene.witcher_event_effects)

    # The event list comes first: it is what this tab is for.
    row = layout.row(align=True)
    row.prop(scene, 'witcher_event_search', text='', icon='VIEWZOOM')
    row.operator('witcher.animation_event_clear_filters', text='', icon='X')
    row = layout.row(align=True)
    row.prop(scene, 'witcher_event_class_filter', text='', placeholder='Class')
    row.prop(scene, 'witcher_event_track_filter', text='', placeholder='Track')
    index = action.witcher_event_index
    selected = 0 <= index < len(action.witcher_events) and matches_filter(action.witcher_events[index], scene)
    row = layout.row()
    row.template_list('WITCH_UL_AnimationEvents', '', action, 'witcher_events', action, 'witcher_event_index', rows=6)
    buttons = row.column(align=True)
    buttons.operator('witcher.animation_event_add', text='', icon='ADD')
    remove = buttons.row()
    remove.enabled = selected
    remove.operator('witcher.animation_event_remove', text='', icon='REMOVE')
    if not action.witcher_events_known:
        layout.label(text='Event data unavailable', icon='INFO')
    if selected:
        item = action.witcher_events[index]
        box = layout.box()
        box.prop(item, 'event_name')
        box.prop(item, 'event_type')
        box.prop(item, 'track_display_name')
        if item.source_file:
            box.prop(item, 'source_file')
        row = box.row(align=True)
        row.prop(item, 'frame')
        row.operator('witcher.animation_event_move', text='', icon='KEY_HLT')
        row.operator('witcher.animation_event_jump', text='', icon='PLAY')
        if item.duration or event_inherits_from(item.source_game, item.event_type, 'CExtAnimDurationEvent'):
            box.prop(item, 'duration')
        if item.sound_event or 'soundEventName' in event_declared_field_names(item.source_game, item.event_type):
            row = box.row(align=True)
            row.prop(item, 'sound_event', text='Sound')
            row.operator('witcher.animation_event_audio', text='', icon='PLAY').audition = True
        box.operator('witcher.animation_event_fields', icon='PREFERENCES')

    header, body = _section(layout, 'witcher_events_tracks', 'Tracks', 'NLA')
    header.operator('witcher.event_track_settings', text='', icon='PREFERENCES', emboss=False)
    if body:
        row = body.row(align=True)
        unnamed = sum(not e.track_name or e.track_name == events.DEFAULT_TRACK for e in action.witcher_events)
        row.label(text=f'{unnamed} events on default track' if unnamed else 'Stored track names' if action.witcher_events else 'No events')
        row.operator('witcher.event_track_info', text='', icon='INFO')
        body.operator('witcher.event_track_names', icon='NLA')
        row = body.row(align=True)
        row.operator('witcher.event_track_add', text='Add', icon='ADD')
        row.operator('witcher.event_track_delete', text='Delete', icon='REMOVE')
        row.operator('witcher.event_track_lock', text='Locks', icon='LOCKED')
        body.prop(scene, 'witcher_tracks_show_empty')
        row = body.row()
        row.template_list('WITCH_UL_EventTracks', '', action, 'witcher_event_tracks', action, 'witcher_event_track_index', rows=4)
        buttons = row.column(align=True)
        for direction, icon in (('TOP', 'TRIA_UP_BAR'), ('UP', 'TRIA_UP'), ('DOWN', 'TRIA_DOWN'), ('BOTTOM', 'TRIA_DOWN_BAR')):
            buttons.operator('witcher.event_track_move', text='', icon=icon).direction = direction

    header, body = _section(layout, 'witcher_events_effects', 'Weapon Effects', 'PARTICLES')
    header.operator('witcher.event_effects_settings', text='', icon='PREFERENCES', emboss=False)
    if body:
        row = body.row(align=True)
        row.enabled = scene.witcher_event_effects
        row.operator('witcher.event_effects', text='Rebake', icon='FILE_REFRESH').rebake = True
        row.operator('witcher.animation_event_trails', text='Clear', icon='X').clear = True
        body.label(text='Rebake after motion or timing edits', icon='INFO')

CLASSES = (WITCH_OT_EventTrackInfo, WITCH_OT_EventTrackNames, WITCH_OT_EventTrackAdd, WITCH_OT_EventTrackDelete, WITCH_OT_EventTrackLock, WITCH_OT_EventTrackMove, WITCH_UL_EventTracks, WITCH_PG_EventTrack, WITCH_OT_AnimationEventExportInfo, WITCH_OT_EventTrackSettings, WITCH_OT_EventTracksFrameAll, WITCH_PG_AnimationEvent, WITCH_UL_AnimationEvents, WITCH_OT_AnimationEventAdd,
           WITCH_OT_AnimationEventClearFilters,
           WITCH_OT_AnimationEventRemove, WITCH_OT_AnimationEventJump, WITCH_OT_AnimationEventMove,
           WITCH_OT_AnimationEventFields, WITCH_OT_AnimationEventAudio, WITCH_OT_AnimationEventAudioInfo,
           WITCH_OT_AnimationEventTrails, WITCH_OT_EventEffects, WITCH_OT_EventEffectsSettings)


def register():
    typed_fields.register()
    for name, prop in TRACK_VIEW_PROPERTIES.items():
        setattr(bpy.types.Scene, name, prop)
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Action.witcher_events = CollectionProperty(type=WITCH_PG_AnimationEvent)
    bpy.types.Action.witcher_event_tracks = CollectionProperty(type=WITCH_PG_EventTrack)
    bpy.types.Action.witcher_track_layout_initialized = BoolProperty(default=False)
    bpy.types.Action.witcher_event_track_index = IntProperty(default=0, min=0, update=select_track)
    bpy.types.Action.witcher_events_known = BoolProperty(default=False)
    bpy.types.Action.witcher_event_index = IntProperty(default=0, min=0, update=event_tracks.tag_redraw)
    bpy.types.Scene.witcher_event_tracks = BoolProperty(name='Show Event Tracks', default=False, description='Draw this clip\'s events by track in the viewport; the selected event has a white outline', update=toggle_tracks)
    bpy.types.Scene.witcher_event_effects = BoolProperty(name='Weapon Effects', default=False, update=remember)
    bpy.types.Scene.witcher_effects_trails = BoolProperty(
        name='Light Trails', default=True, update=remember,
        description='Bake the held weapon\'s light trails from the clip\'s item-effect events')
    bpy.types.Scene.witcher_effects_blood = BoolProperty(
        name='Blood Hits', default=False, update=remember,
        description='Simulate red-blood hits at BloodTrail markers; no victim or collision is needed')
    bpy.types.Scene.witcher_event_audio_status = StringProperty(name='Audio', options={'SKIP_SAVE'})
    bpy.types.Scene.witcher_event_sounds = BoolProperty(
        name='Timeline Sounds', default=False, options={'SKIP_SAVE'},
        description='Play this clip\'s game sound events while the timeline plays; stays on when you switch clips',
        update=toggle_sounds)
    bpy.types.Scene.witcher_event_search = StringProperty(name='Search Events', description='Filter by event name, sound event name, class or track; case insensitive', options={'SKIP_SAVE'})
    bpy.types.Scene.witcher_event_class_filter = StringProperty(name='Class', description='Show only this exact event class; clear to show all classes', search=event_classes, options={'SKIP_SAVE'})
    bpy.types.Scene.witcher_event_track_filter = StringProperty(name='Track', description='Show only this event track; clear to show all tracks', search=track_names, options={'SKIP_SAVE'})
    from ..animation import event_preview
    event_preview.register()
    event_tracks.register()
    if restore_track_layout not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(restore_track_layout)
    # Add-on enable runs under RestrictBlend; data is available on the next UI tick.
    if not bpy.app.timers.is_registered(restore_track_layout):
        bpy.app.timers.register(restore_track_layout, first_interval=0.0)


def unregister():
    if bpy.app.timers.is_registered(restore_track_layout):
        bpy.app.timers.unregister(restore_track_layout)
    if restore_track_layout in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(restore_track_layout)
    for name in TRACK_VIEW_PROPERTIES:
        delattr(bpy.types.Scene, name)
    from ..animation import event_preview
    event_preview.unregister()
    event_tracks.unregister()
    for owner, names in ((bpy.types.Action, ('witcher_events', 'witcher_event_tracks', 'witcher_track_layout_initialized', 'witcher_event_track_index', 'witcher_events_known', 'witcher_event_index')),
                         (bpy.types.Scene, ('witcher_event_tracks', 'witcher_event_effects', 'witcher_effects_trails', 'witcher_effects_blood',
                                           'witcher_event_audio_status', 'witcher_event_sounds', 'witcher_event_search',
                                           'witcher_event_class_filter', 'witcher_event_track_filter'))):
        for name in names:
            delattr(owner, name)
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
    typed_fields.unregister()
