"""Owned sound handles for animation event preview."""
import bpy
from bpy.app.handlers import persistent

from . import events

_preview = None
_syncing = False


def _set_enabled(scene, value):
    """Mirror the preview state on the Sounds toggle without re-entering its update."""
    global _syncing
    if scene.witcher_event_sounds != value:
        _syncing = True
        try:
            scene.witcher_event_sounds = value
        finally:
            _syncing = False


def toggled(scene, context):
    if _syncing:
        return
    if scene.witcher_event_sounds:
        enable(context)
    else:
        stop()


def enable(context):
    """Prepare clip sounds; report failures and leave Sounds enabled for retry."""
    from ..ui.ui_anim_events import context_clip
    owner, action, strip = context_clip(context)
    try:
        if owner is None or action is None:
            raise ValueError('Choose a character clip first')
        prepare(context, owner, action, strip)
    except (OSError, ValueError, RuntimeError) as exc:
        context.scene.witcher_event_audio_status = str(exc)


def playing():
    return any(window.screen.is_animation_playing for window in bpy.context.window_manager.windows)


def signature(owner, action):
    data = owner.animation_data
    return (data.action, data.action_slot_handle, data.use_nla, data.action_influence, data.action_blend_type,
            tuple((t.as_pointer(), t.mute, t.is_solo,
                   tuple((s.as_pointer(), s.action, s.mute, s.select, s.frame_start, s.frame_end, s.action_frame_start,
                          s.action_frame_end, s.scale, s.repeat, s.use_reverse,
                          s.use_animated_time, s.use_animated_influence, s.blend_type) for s in t.strips))
                  for t in data.nla_tracks),
            tuple(tuple(events.values(event).values()) for event in action.witcher_events))


class Preview:
    def __init__(self, scene, owner, action, strip, player):
        self.scene, self.owner, self.action, self.strip, self.player = scene, owner, action, strip, player
        self.target = scene.witcher_main_armature
        self.state = signature(owner, action)
        self.last = None
        self.was_playing = False
        records = (events.strip_events(strip, strip.frame_start, strip.frame_end) if strip is not None
                   else (events.values(e) for e in action.witcher_events))
        self.cues = [(e['frame'], e['sound_event'], 'Footstep' in e['event_type'])
                     for e in records if e['sound_event']]

    def valid(self):
        try:
            return (bpy.context.scene == self.scene and self.scene.objects.get(self.owner.name) == self.owner
                    and bpy.data.actions.get(self.action.name) == self.action
                    and self.scene.witcher_main_armature == self.target
                    and signature(self.owner, self.action) == self.state)
        except (ReferenceError, AttributeError):
            return False

    def advance(self, frame, is_playing):
        if not is_playing:
            if self.was_playing:
                self.player.stop()
            self.was_playing, self.last = False, None
            return
        self.was_playing = True
        action, strip = events.clip(self.owner, frame)
        if action != self.action or strip != self.strip:
            self.player.stop()
            self.last = None
            return
        if self.strip is not None and not self.strip.frame_start <= frame <= self.strip.frame_end:
            self.player.stop()
            self.last = None
            return
        current = frame
        previous = current - 1e-4 if self.last is None else self.last
        if current < previous:
            self.player.stop()
            previous = current - 1e-4
        for time, sound, footstep in self.cues:
            if previous < time <= current + 1e-5:
                self.player.play(sound, footstep)
        self.last = current


@persistent
def stop(*_args):
    global _preview
    previous, _preview = _preview, None
    if previous is not None:
        previous.player.stop()
        try:
            previous.scene.witcher_event_audio_status = ''
        except ReferenceError:
            pass


def _watch():
    if _preview is None:
        return None
    if not _preview.valid():
        stop()
        return None
    if not playing():
        _preview.advance(_preview.scene.frame_current_final, False)
    return 0.1


@persistent
def changed(scene, _depsgraph=None):
    if _preview is not None:
        if not _preview.valid():
            stop()
        elif scene == _preview.scene:
            _preview.advance(scene.frame_current_final, playing())


@persistent
def validate(*_args):
    if _preview is not None and not _preview.valid():
        stop()


@persistent
def playback_started(scene, *_args):
    # Sounds stay switched on across clip changes: prepare whichever clip is about to play.
    if scene.witcher_event_sounds and (_preview is None or not _preview.valid()):
        enable(bpy.context)
    if _preview is not None and _preview.valid():
        _preview.last = None
        _preview.advance(scene.frame_current_final, True)


@persistent
def playback_stopped(*_args):
    if _preview is not None:
        _preview.player.stop()
        _preview.last, _preview.was_playing = None, False


def prepare(context, owner, action, strip, audition=False):
    global _preview
    from .. import get_vgmstream_path
    from ..controller import sfx, sfx_assets
    from ..repo_paths import repo_file_for_source, source_game_for_rig_settings
    import os

    stop()
    if getattr(getattr(context.window_manager, 'witcher_player', None), 'running', False):
        raise ValueError('Stop the player controller before previewing timeline sounds')
    names = {event.sound_event for event in action.witcher_events if event.sound_event}
    if not names:
        raise ValueError('This action has no sound events')
    library = sfx_assets.game_library()
    settings = owner.data.witcherui_RigSettings
    template = repo_file_for_source(settings.repo_path, source_game_for_rig_settings(settings)) if settings.repo_path else None
    spawn = sfx_assets.template_emitter_events(template) if template and os.path.isfile(template) else []
    switches = library.switches(spawn, sfx_assets.armour_type(owner))
    wavs = sfx_assets.decode(library, library.pool(names, switches), sfx_assets.wav_cache_dir(), get_vgmstream_path(context))
    if not wavs:
        raise ValueError('No audio resolved; check the game sound cache and vgmstream path')
    device = sfx.open_device()
    if device is None:
        raise ValueError('No audio output device is available')
    player = sfx.Player(library, wavs, switches, device)
    _preview = Preview(context.scene, owner, action, strip, player)
    _set_enabled(context.scene, True)
    context.scene.witcher_event_audio_status = f'{len(_preview.cues)} sound cues ready'
    if not bpy.app.timers.is_registered(_watch):
        bpy.app.timers.register(_watch, first_interval=0.1)
    if audition and 0 <= action.witcher_event_index < len(action.witcher_events):
        event = action.witcher_events[action.witcher_event_index]
        player.play(event.sound_event, 'Footstep' in event.event_type)


HANDLERS = (('frame_change_post', changed), ('depsgraph_update_post', validate),
            ('animation_playback_pre', playback_started), ('animation_playback_post', playback_stopped),
            ('load_pre', stop), ('undo_pre', stop), ('redo_pre', stop))


def register():
    for name, callback in HANDLERS:
        handlers = getattr(bpy.app.handlers, name)
        if callback not in handlers:
            handlers.append(callback)


def unregister():
    stop()
    if bpy.app.timers.is_registered(_watch):
        bpy.app.timers.unregister(_watch)
    for name, callback in HANDLERS:
        handlers = getattr(bpy.app.handlers, name)
        if callback in handlers:
            handlers.remove(callback)
