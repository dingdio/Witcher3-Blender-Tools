"""Player panel and modal input operator."""
import logging
import math
import time

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, StringProperty

from .ui_utils import WITCH_PT_Base

log = logging.getLogger(__name__)
STEP = 1.0 / 60.0
# The game's PC defaults (bin\config\r4game\legacy\base\input_qwerty.ini).
MOVE_KEYS = {'W': (1.0, 0.0), 'S': (-1.0, 0.0), 'A': (0.0, -1.0), 'D': (0.0, 1.0),
             'UP_ARROW': (1.0, 0.0), 'DOWN_ARROW': (-1.0, 0.0), 'LEFT_ARROW': (0.0, -1.0), 'RIGHT_ARROW': (0.0, 1.0)}
SPRINT_KEYS = {'LEFT_SHIFT', 'RIGHT_SHIFT'}
WALK_TOGGLE_KEYS = {'LEFT_CTRL', 'RIGHT_CTRL'}
DODGE_KEYS = {'LEFT_ALT', 'RIGHT_ALT'}
ROLL_KEYS = {'SPACE'}
SHEATHE_KEY = 'C'
FREECAM_KEY = 'F1'
FREECAM_KEYS = {'Q': (0.0, 1.0), 'E': (0.0, -1.0), 'R': (-1.0, 0.0), 'F': (1.0, 0.0)}      # (roll, up)
HELD_KEYS = set(MOVE_KEYS) | SPRINT_KEYS | set(FREECAM_KEYS)
PAD_RUN = 0.7                  # stick threshold for running
PAD_SPRINT = 0.8               # CanSprint needs the stick past this
SHEATHE_HOLD = 0.3             # SwordSheathe* State=Duration IdleTime
WEAPON_KEYS = {'ONE': "steelsword", 'TWO': "silversword"}
FOV_WHEEL = 2.0                # degrees per wheel notch
TOAST_TIME = 1.5
_session = None
_toast = ("", 0.0)
_toast_handle = None
_viewport = None               # (space, scene, shading, overlays, engine) restored on Stop


def _notify(text: str):
    global _toast
    _toast = (text, time.perf_counter())


def _draw_toast(area):
    import blf
    text, at = _toast
    age = time.perf_counter() - at
    if not text or age > TOAST_TIME or bpy.context.area != area:
        return
    region = bpy.context.region
    font, size = 0, 20 * bpy.context.preferences.system.ui_scale
    blf.size(font, size)
    width, _ = blf.dimensions(font, text)
    blf.position(font, (region.width - width) / 2, region.height * 0.12, 0)
    blf.color(font, 1.0, 1.0, 1.0, min(1.0, (TOAST_TIME - age) / 0.4))
    blf.enable(font, blf.SHADOW)
    blf.shadow(font, 3, 0.0, 0.0, 0.0, 0.8)
    blf.draw(font, text)
    blf.disable(font, blf.SHADOW)


def _show_toasts(area):
    global _toast_handle
    _hide_toasts()
    _toast_handle = bpy.types.SpaceView3D.draw_handler_add(_draw_toast, (area,), 'WINDOW', 'POST_PIXEL')


def _set_eevee(scene):
    for engine in ('BLENDER_EEVEE_NEXT', 'BLENDER_EEVEE'):      # renamed back to BLENDER_EEVEE in 5.0
        try:
            scene.render.engine = engine
            return
        except TypeError:
            pass


def _apply_viewport(props, context=None):
    if _viewport is None:
        return
    space, scene, shading, overlays, engine = _viewport
    try:
        if props.play_rendered:
            _set_eevee(scene)
        else:
            scene.render.engine = engine
        space.shading.type = 'RENDERED' if props.play_rendered else shading
        space.overlay.show_overlays = overlays and not props.play_hide_overlays
    except ReferenceError:              # the area or scene went away
        pass


def _viewport_for_play(context):
    global _viewport
    space = context.space_data
    _viewport = (space, context.scene, space.shading.type, space.overlay.show_overlays, context.scene.render.engine)
    _apply_viewport(context.window_manager.witcher_player)


def _restore_viewport():
    global _viewport
    if _viewport is None:
        return
    space, scene, shading, overlays, engine = _viewport
    _viewport = None
    try:
        scene.render.engine = engine
        space.shading.type, space.overlay.show_overlays = shading, overlays
    except ReferenceError:
        pass


def _hide_toasts():
    global _toast_handle, _toast
    if _toast_handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_toast_handle, 'WINDOW')
        _toast_handle = None
    _toast = ("", 0.0)


def session():
    return _session


def _surfaces():
    from ..controller.surface import PHYSICAL_MATERIALS
    return sorted(PHYSICAL_MATERIALS)


class WitcherPlayerSettings(bpy.types.PropertyGroup):
    running: BoolProperty(default=False)
    captured: BoolProperty(default=False)
    profile: StringProperty()
    weapon: StringProperty()
    state: StringProperty()
    clip: StringProperty()
    gait: StringProperty()
    device: StringProperty()
    position: StringProperty(name="Position", description="World position (x y z, metres) and heading")
    speed: FloatProperty()
    tick_ms: FloatProperty()
    on_ground: BoolProperty()
    camera_distance: FloatProperty(name="Distance", default=3.5, min=0.5, max=15.0, description="Camera distance from the pivot")
    camera_height: FloatProperty(name="Height", default=1.6, min=0.0, max=3.0, description="Camera pivot height above the feet")
    mouse_sensitivity: FloatProperty(name="Mouse", default=0.2, min=0.01, max=2.0, description="Degrees of camera turn per pixel")
    stick_sensitivity: FloatProperty(name="Stick", default=150.0, min=10.0, max=360.0, description="Camera degrees per second at full right stick")
    invert_y: BoolProperty(name="Invert Y", default=False)
    deadzone: FloatProperty(name="Deadzone", default=0.2, min=0.0, max=0.9)
    walk_default: BoolProperty(name="Walk by Default", default=False,
                               description="Start walking; Ctrl toggles walk and run as in the game")
    freecam: BoolProperty()
    reference_ground: BoolProperty(name="Reference Ground", default=True,
                                   description="When nothing is under the character, import the REDkit reference level's "
                                               "terrain and environment (engine\\reference_level) and stand him on it")
    ground: BoolProperty(name="Stay on Ground", default=True,
                         description="Keep the character on the scene's visible geometry and out of walls (no navmesh: foliage, "
                                     "proxies and water are ignored)")
    camera_collision: BoolProperty(name="Camera Collision", default=True,
                                   description="Keep the camera in front of the scene's walls, floors and ceilings")
    play_rendered: BoolProperty(name="Rendered", default=True, update=_apply_viewport,
                                description="EEVEE Rendered shading while playing (restored on Stop)")
    play_hide_overlays: BoolProperty(name="Hide Overlays", default=True, update=_apply_viewport,
                                     description="Hide the viewport overlays while playing (restored on Stop)")
    follow: BoolProperty(name="Camera Follows", default=False,
                         description="Ease the camera behind the character while he moves away from it (the game's camera only "
                                     "moves with the mouse or right stick)")
    dangles: BoolProperty(name="Simulate Dangles", default=True,
                          description="Step the hair / cloth dangle solvers that have live preview on (Physics panel) every tick")
    sound: BoolProperty(name="Sound", default=True,
                        description="Play the clips' footstep, foley, swoosh and grunt events from the game's soundbanks "
                                    "(decoded once into the add-on cache)")
    sound_volume: FloatProperty(name="Volume", default=0.8, min=0.0, max=2.0, subtype='FACTOR')
    volume_footsteps: FloatProperty(name="Footsteps", default=1.0, min=0.0, max=2.0, subtype='FACTOR')
    volume_foley: FloatProperty(name="Clothes", default=0.7, min=0.0, max=2.0, subtype='FACTOR')
    volume_combat: FloatProperty(name="Combat", default=1.0, min=0.0, max=2.0, subtype='FACTOR')
    volume_voice: FloatProperty(name="Voice", default=1.0, min=0.0, max=2.0, subtype='FACTOR')
    default_surface: EnumProperty(
        name="Default Surface",
        items=[("default", "Game Default", "The banks' default footstep set")]
              + [(n, n.replace("_", " ").title(), f"Footsteps on {n}") for n in _surfaces()],
        description="Footstep surface where the ground under the character has no known physical material (no "
                    "collision, terrain control map or telling material name)")
    sound_debug: BoolProperty(name="Log Sounds", default=False,
                              description="Log every sound event with its surface and the wems it played")
    sound_status: StringProperty()


class WITCH_OT_player_play(bpy.types.Operator):
    bl_idname = "witcher.player_play"
    bl_label = "Play"
    bl_description = ("Play the selected character with the game's default controls: WASD or left stick, Shift or A sprints, "
                      "Ctrl toggles walk, 1 / 2 or d-pad draw the steel / silver sword (hold or C to sheathe), left mouse or X "
                      "attacks, Alt or B dodges and Space or A rolls with a sword drawn, F1 free camera (wheel or d-pad up / down "
                      "sets its FOV), Tab frees the mouse, "
                      "Esc or Back stops")

    _timer = None
    _keys = None
    _pressed = None
    _buttons = 0
    _walk = False
    _sprint_toggle = False
    _sprinting = False
    _last = 0.0
    _accumulator = 0.0
    _region = None
    _region_3d = None
    _space = None
    _area = None

    @classmethod
    def poll(cls, context):
        from ..controller.session import player_armature
        return (context.area is not None and context.area.type == 'VIEW_3D' and player_armature(context) is not None
                and not context.window_manager.witcher_player.running)

    def invoke(self, context, event):
        global _session
        from ..controller.session import Session, player_armature
        from ..controller.world import ensure_ground
        props = context.window_manager.witcher_player
        armature = player_armature(context)
        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        if props.reference_ground:
            try:
                if ensure_ground(context, armature):
                    self.report({'INFO'}, "Imported the reference level ground")
            except Exception as exc:          # ground is optional
                log.exception("player: reference ground import failed")
                self.report({'WARNING'}, f"Reference ground not imported: {exc}")
        try:
            _session = Session(armature, props)
        except (FileNotFoundError, KeyError, ValueError) as exc:
            log.exception("player: cannot load the locomotion profile")
            self.report({'ERROR'}, f"No locomotion clips for {armature.name}: {exc}")
            return {'CANCELLED'}
        try:
            _session.start()
        except Exception as exc:
            log.exception("player: cannot start play")
            _session = None
            self.report({'ERROR'}, f"Cannot start play: {exc}")
            return {'CANCELLED'}
        props.running, props.captured, props.freecam = True, True, False
        self._keys, self._pressed, self._buttons = set(), {}, 0
        self._walk, self._sprint_toggle, self._sprinting = props.walk_default, False, False
        self._area = context.area
        self._region = next((r for r in context.area.regions if r.type == 'WINDOW'), context.region)
        self._region_3d = context.space_data.region_3d
        self._space = context.space_data
        self._last, self._accumulator = time.perf_counter(), 0.0
        self._capture(context, True)
        _show_toasts(self._area)
        _viewport_for_play(context)
        self._timer = context.window_manager.event_timer_add(STEP, window=context.window)
        context.window_manager.modal_handler_add(self)
        self._present(context, 0.0)
        return {'RUNNING_MODAL'}

    def _centre(self):
        return self._region.x + self._region.width // 2, self._region.y + self._region.height // 2

    def _capture(self, context, on: bool):
        context.window_manager.witcher_player.captured = on
        if on:
            context.window.cursor_modal_set('NONE')
            context.window.cursor_warp(*self._centre())
        else:
            context.window.cursor_modal_restore()

    def modal(self, context, event):
        props = context.window_manager.witcher_player
        if not props.running or _session is None or (event.type == 'ESC' and event.value == 'PRESS'):
            return self._finish(context)
        if event.type == 'WINDOW_DEACTIVATE':           # releases never arrive while another window has focus
            self._keys.clear()
            self._pressed.clear()
            if props.captured:
                self._capture(context, False)
            return {'PASS_THROUGH'}
        if event.type == 'TIMER':
            try:
                return self._tick(context)
            except ReferenceError:                      # the armature went away under us
                return self._finish(context)
        if not props.captured:
            if event.type == 'TAB' and event.value == 'PRESS':
                self._capture(context, True)
                return {'RUNNING_MODAL'}
            return {'PASS_THROUGH'}
        if event.type == 'MOUSEMOVE':
            cx, cy = self._centre()
            dx, dy = event.mouse_x - cx, event.mouse_y - cy
            if dx or dy:
                if _session.freecam is not None:
                    _session.freecam.mouse(dx, dy)
                else:
                    sign = -1.0 if props.invert_y else 1.0
                    _session.orbit(dx * props.mouse_sensitivity, -dy * props.mouse_sensitivity * sign)
                context.window.cursor_warp(cx, cy)
            return {'RUNNING_MODAL'}
        if event.type in HELD_KEYS:
            if event.value == 'PRESS':
                self._keys.add(event.type)
            elif event.value == 'RELEASE':
                self._keys.discard(event.type)
        elif event.value == 'PRESS':
            self._press(context, event.type)
        elif event.value == 'RELEASE' and event.type in WEAPON_KEYS:
            self._release_sword(event.type)
        return {'RUNNING_MODAL'}            # nothing reaches Blender's keymap while captured (undo, delete, grab)

    def _press(self, context, key: str):
        props = context.window_manager.witcher_player
        if key == 'TAB':
            self._capture(context, False)
        elif key == FREECAM_KEY:
            self._toggle_freecam(context)
        elif _session.freecam is not None:
            if key == 'RIGHTMOUSE':
                _session.teleport(centre=True)
            elif key in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE'}:
                self._zoom(-FOV_WHEEL if key == 'WHEELUPMOUSE' else FOV_WHEEL)
        elif key in WALK_TOGGLE_KEYS:
            if not self._sprinting:                     # WalkToggle is ignored while sprinting
                self._walk = not self._walk
        elif key in WEAPON_KEYS:
            category = WEAPON_KEYS[key]
            self._pressed[key] = (time.perf_counter(), _session.drawn == category)
            _session.sword_key(category)
        elif key == SHEATHE_KEY:
            _session.sheathe()
        elif key in DODGE_KEYS:
            _session.evade("dodge")
        elif key in ROLL_KEYS:
            _session.evade("roll")
        elif key == 'LEFTMOUSE':
            _session.attack()

    def _release_sword(self, key: str):
        """The held sword's key sheathes it on release (sheatheIfAlreadyEquipped)."""
        pressed = self._pressed.pop(key, None)
        if pressed is not None and pressed[1]:
            _session.sheathe(WEAPON_KEYS[key])

    def _hold_swords(self):
        for key, (at, _) in list(self._pressed.items()):
            if time.perf_counter() - at >= SHEATHE_HOLD:
                del self._pressed[key]
                _session.sheathe(WEAPON_KEYS[key.removeprefix("PAD_")])

    def _tick(self, context):
        now = time.perf_counter()
        self._accumulator += min(now - self._last, 0.25)
        self._last = now
        steps = 0
        started = time.perf_counter()
        while self._accumulator >= STEP and steps < 4:
            if not self._update(context, STEP):
                return self._finish(context)
            self._accumulator -= STEP
            steps += 1
        if steps:
            _session.tick_ms = (time.perf_counter() - started) * 1000.0 / steps
            self._present(context, steps * STEP)
        return {'RUNNING_MODAL'}

    def _update(self, context, dt: float) -> bool:
        from ..controller import xinput
        props = context.window_manager.witcher_player
        self._hold_swords()
        keys = self._keys
        forward = sum(MOVE_KEYS[k][0] for k in keys if k in MOVE_KEYS)
        right = sum(MOVE_KEYS[k][1] for k in keys if k in MOVE_KEYS)
        sprint = bool(keys & SPRINT_KEYS)
        pad = xinput.read(0, props.deadzone)
        props.device = "keyboard" if pad is None else "gamepad"
        if _session.freecam is not None:
            return self._update_freecam(context, dt, pad, forward, right, sprint)
        stop_delay, stick = 0.15, 0.0
        if pad is not None:
            buttons = pad["buttons"]
            pressed = buttons & ~self._buttons
            self._buttons = buttons
            if buttons & xinput.BUTTON_BACK:
                return False
            if self._pad_freecam(context, pad, pressed):
                return True
            self._pad_actions(pressed, buttons)
            if pressed & xinput.BUTTON_LTHUMB:
                self._sprint_toggle = not self._sprint_toggle
            if pad["rx"] or pad["ry"]:
                sign = -1.0 if props.invert_y else 1.0
                _session.orbit(pad["rx"] * props.stick_sensitivity * dt, pad["ry"] * props.stick_sensitivity * dt * sign)
            stick = math.hypot(pad["lx"], pad["ly"])
            if stick > 0.0:
                forward, right = pad["ly"], pad["lx"]
                stop_delay = 0.05
                sprint = stick > PAD_SPRINT and (bool(buttons & xinput.BUTTON_A) or self._sprint_toggle)
        self._step_player(dt, forward, right, sprint, stop_delay, stick)
        return True

    def _step_player(self, dt, forward, right, sprint, stop_delay, stick=0.0):
        from ..controller.core import SPEEDS
        walk = self._walk or (0.0 < stick < PAD_RUN)
        self._sprinting = sprint and bool(forward or right)
        speed = SPEEDS[2] if self._sprinting else SPEEDS[0] if walk else SPEEDS[1]
        if self._sprint_toggle and (speed < SPEEDS[1] or not (forward or right)):
            self._sprint_toggle = False                 # SprintToggle drops with walk or idle
        _session.step(dt, _session.move_vector(forward, right), speed, stop_delay)

    def _pad_actions(self, pressed: int, buttons: int):
        from ..controller import xinput
        if pressed & xinput.BUTTON_X:
            _session.attack()
        if pressed & xinput.BUTTON_B:
            _session.evade("dodge")
        if pressed & xinput.BUTTON_A:
            _session.evade("roll")
        for button, key in ((xinput.BUTTON_DPAD_LEFT, 'ONE'), (xinput.BUTTON_DPAD_RIGHT, 'TWO')):
            pad_key = "PAD_" + key
            if pressed & button:
                self._pressed[pad_key] = (time.perf_counter(), _session.drawn == WEAPON_KEYS[key])
                _session.sword_key(WEAPON_KEYS[key])
            elif not buttons & button and pad_key in self._pressed:
                self._release_sword_pad(pad_key)

    def _release_sword_pad(self, pad_key: str):
        pressed = self._pressed.pop(pad_key)
        if pressed[1]:
            _session.sheathe(WEAPON_KEYS[pad_key[4:]])

    def _pad_freecam(self, context, pad, pressed: int) -> bool:
        """LB + RB + both triggers enter the free camera."""
        from ..controller import xinput
        both = xinput.BUTTON_LB | xinput.BUTTON_RB
        if pressed & both and pad["buttons"] & both == both and pad["lt"] > 0.5 and pad["rt"] > 0.5:
            self._toggle_freecam(context)
            return True
        return False

    def _update_freecam(self, context, dt, pad, forward, right, boost) -> bool:
        from ..controller import xinput
        cam = _session.freecam
        roll = sum(FREECAM_KEYS[k][0] for k in self._keys if k in FREECAM_KEYS)
        up = sum(FREECAM_KEYS[k][1] for k in self._keys if k in FREECAM_KEYS)
        cam.keys(forward, right, up, boost, roll)
        player = (0.0, 0.0)
        if pad is not None:
            buttons = pad["buttons"]
            pressed = buttons & ~self._buttons
            self._buttons = buttons
            if buttons & xinput.BUTTON_BACK:
                return False
            if pressed & xinput.BUTTON_B:
                self._toggle_freecam(context)
                return True
            if buttons & xinput.BUTTON_RB:              # inputs skip the camera and drive the player
                player = (pad["ly"], pad["lx"])
            elif pad["lx"] or pad["ly"] or pad["rx"] or pad["ry"] or pad["lt"] or buttons:
                pad_up = (1.0 if buttons & xinput.BUTTON_Y else 0.0) - (1.0 if buttons & xinput.BUTTON_A else 0.0)
                pad_roll = (-1.0 if buttons & xinput.BUTTON_DPAD_LEFT else 0.0) + (1.0 if buttons & xinput.BUTTON_DPAD_RIGHT else 0.0)
                cam.pad(pad["lx"], pad["ly"], pad["rx"], pad["ry"], pad["lt"], pad_up, pad_roll)
                if buttons & xinput.BUTTON_DPAD_UP:
                    self._zoom(1.0)
                elif buttons & xinput.BUTTON_DPAD_DOWN:
                    self._zoom(-1.0)
                if pressed & xinput.BUTTON_X:
                    _session.teleport()
        cam.tick(dt)
        self._step_player(dt, *player, False, 0.05)
        return True

    def _toggle_freecam(self, context):
        on = context.window_manager.witcher_player.freecam = _session.toggle_freecam()
        _notify(f"Free camera  FOV {_session.freecam.fov:.0f}\u00b0" if on else "Follow camera")

    def _zoom(self, step: float):
        _session.freecam.zoom(step)
        _notify(f"FOV {_session.freecam.fov:.0f}°")

    def _present(self, context, dt: float):
        props = context.window_manager.witcher_player
        _session.apply()
        if props.dangles and dt > 0.0:
            _session.step_dangles(dt)
        if self._region_3d is not None:
            _session.view(self._region_3d, dt or STEP, self._space, (self._region.width, self._region.height))
        for key, value in _session.status().items():
            setattr(props, key, value)
        if self._area is not None:
            self._area.tag_redraw()

    def _finish(self, context):
        global _session
        props = context.window_manager.witcher_player
        if self._timer is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        self._capture(context, False)
        _hide_toasts()
        _restore_viewport()
        if _session is not None:
            _session.restore()
            _session = None
        props.running = props.freecam = False
        props.state = props.sound_status = ""
        if self._area is not None:
            self._area.tag_redraw()
        return {'FINISHED'}


class WITCH_OT_player_stop(bpy.types.Operator):
    bl_idname = "witcher.player_stop"
    bl_label = "Stop"
    bl_description = "Stop playing and restore the character's pose, transform, animation and the viewport"

    def execute(self, context):
        context.window_manager.witcher_player.running = False
        return {'FINISHED'}


CHARACTERS = {'GERALT': ("Geralt", "player.w2ent", "import_geralt"), 'CIRI': ("Ciri", "ciri_player.w2ent", "import_ciri")}


def _character_rig(context, entity: str):
    from ..controller.session import _is_player_rig
    return next((o for o in context.scene.objects if o.parent is None and _is_player_rig(o) and
                 (o.data.witcherui_RigSettings.repo_path or "").replace("/", "\\").lower().endswith("\\" + entity)), None)


class WITCH_OT_player_import_play(bpy.types.Operator):
    bl_idname = "witcher.player_import_play"
    bl_label = "Import & Play"
    bl_description = "Import the character at the 3D cursor (unless already in the scene), select it and start playing"

    character: bpy.props.EnumProperty(items=[(k, v[0], f"Import and play {v[0]}") for k, v in CHARACTERS.items()],
                                      default='GERALT', options={'SKIP_SAVE'})

    @classmethod
    def poll(cls, context):
        return context.area is not None and context.area.type == 'VIEW_3D' and not context.window_manager.witcher_player.running

    def execute(self, context):
        label, entity, op = CHARACTERS[self.character]
        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        armature = _character_rig(context, entity)
        if armature is None:
            if getattr(bpy.ops.witcher, op)() != {'FINISHED'}:
                return {'CANCELLED'}
            armature = _character_rig(context, entity)
            if armature is None:
                self.report({'ERROR'}, f"{label} did not import")
                return {'CANCELLED'}
            armature.location = context.scene.cursor.location
        for obj in context.view_layer.objects:
            obj.select_set(False)
        armature.hide_set(False)
        armature.select_set(True)
        context.view_layer.objects.active = armature
        return bpy.ops.witcher.player_play('INVOKE_DEFAULT')


class WITCH_PT_Player(WITCH_PT_Base, bpy.types.Panel):
    bl_idname = "WITCH_PT_Player"
    bl_label = "Player"
    bl_options = {'DEFAULT_CLOSED'}

    def draw_header(self, context):
        self.layout.label(text="", icon='ARMATURE_DATA')

    def draw(self, context):
        from ..controller.session import player_armature
        layout = self.layout
        props = context.window_manager.witcher_player
        armature = player_armature(context)
        box = layout.box()
        if armature is not None:
            box.prop(armature, "name", text="Character", icon='ARMATURE_DATA')
        else:
            box.label(text="No character rig selected", icon='ARMATURE_DATA')
        if props.running:
            row = box.row()
            row.operator("witcher.player_stop", icon='PAUSE')
            row.label(text="Esc Stop  Tab Mouse  F1 Free Cam")
            if props.freecam:
                col = box.column(align=True)
                col.label(text="Free Camera", icon='VIEW_CAMERA')
                col.label(text="WASD Fly  Q/E Up/Down  Shift Fast")
                col.label(text="R/F Roll  Wheel FOV  RMB Move Geralt There")
            elif props.weapon:
                col = box.column(align=True)
                col.label(text=f"Sword: {props.weapon}", icon='MOD_SIMPLEDEFORM')
                col.label(text="1/2 Draw (hold) or C Sheathe  LMB Attack")
                col.label(text="Alt Dodge  Space Roll (sword drawn)")
            box.label(text="Ctrl Walk  Shift Sprint")
            status = layout.box()
            status.label(text=props.state.title(), icon='PLAY')
            status.prop(props, "clip", text="Clip")
            col = status.column(align=True)
            col.label(text=f"{props.profile}  {props.gait}  {props.speed:.2f} m/s")
            col.label(text=f"{props.device}  tick {props.tick_ms:.1f} ms",
                      icon='GHOST_ENABLED' if props.on_ground else 'GHOST_DISABLED')
            status.prop(props, "position", text="")
            if props.sound_status:
                status.prop(props, "sound_status", text="Sound", icon='SOUND')
        else:
            if armature is not None:
                row = box.row()
                row.scale_y = 1.4
                row.operator("witcher.player_play", icon='PLAY')
            col = box.column(align=True)
            col.label(text="Import & Play", icon='IMPORT')
            row = col.row(align=True)
            for key, (label, _, _) in CHARACTERS.items():
                row.operator("witcher.player_import_play", text=label).character = key
            box.prop(props, "reference_ground")
        settings = layout.box()
        settings.label(text="Camera", icon='CAMERA_DATA')
        col = settings.column(align=True)
        col.prop(props, "camera_distance")
        col.prop(props, "camera_height")
        col.prop(props, "camera_collision")
        col.prop(props, "follow")
        row = settings.row(align=True)
        row.prop(props, "play_rendered", toggle=True, icon='SHADING_RENDERED')
        row.prop(props, "play_hide_overlays", toggle=True, icon='OVERLAY')
        settings.label(text="Input", icon='MOUSE_MOVE')
        col = settings.column(align=True)
        col.prop(props, "mouse_sensitivity")
        col.prop(props, "stick_sensitivity")
        col.prop(props, "deadzone")
        col.prop(props, "invert_y")
        col.prop(props, "walk_default")
        settings.label(text="World", icon='WORLD')
        col = settings.column(align=True)
        col.prop(props, "ground")
        col.prop(props, "dangles")
        settings.label(text="Sound", icon='SOUND')
        col = settings.column(align=True)
        row = col.row()
        row.enabled = not props.running            # read on play
        row.prop(props, "sound")
        sub = col.column(align=True)
        sub.active = props.sound
        for name in ("sound_volume", "volume_footsteps", "volume_foley", "volume_combat", "volume_voice"):
            sub.prop(props, name)
        sub = settings.column(align=True)
        sub.active = props.sound
        sub.prop(props, "default_surface")
        sub.prop(props, "sound_debug")


_classes = (WitcherPlayerSettings, WITCH_OT_player_play, WITCH_OT_player_stop, WITCH_OT_player_import_play, WITCH_PT_Player)


@bpy.app.handlers.persistent
def _stop(*_args):
    """Restore before an undo, redo or file load frees the armature the session holds."""
    global _session
    if _session is not None:
        _session.restore()
        _session = None


_HANDLERS = (bpy.app.handlers.load_pre, bpy.app.handlers.undo_pre, bpy.app.handlers.redo_pre)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.WindowManager.witcher_player = bpy.props.PointerProperty(type=WitcherPlayerSettings)
    for handlers in _HANDLERS:
        handlers.append(_stop)


def unregister():
    for handlers in _HANDLERS:
        if _stop in handlers:
            handlers.remove(_stop)
    _stop()
    del bpy.types.WindowManager.witcher_player
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
