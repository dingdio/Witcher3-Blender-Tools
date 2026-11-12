"""Clip locomotion in Blender coordinates: metres, Z up, +Y forward.

Starts and stops sync at foot windows; gait loops share phase and crossfade.
"""
import math
from dataclasses import dataclass, field

import numpy as np

RATE = 60                          # avoids fast-start kinks at 30 Hz
IDLE, START, LOOP, STOP, ACTION = "idle", "start", "loop", "stop", "action"
ACTION_FADE = 0.15
EXIT_EVENTS = ("OnWeaponDrawReady", "OnWeaponHolsterReady")     # draw/sheathe lack AllowBlend
FEET = ("LeftUp", "LeftForward", "RightUp", "RightForward")
GAITS = ("walk", "run", "sprint")
SPEEDS = (0.6, 1.0, 1.5)           # game-relative gait speeds
MOVE_DEADZONE = 0.2
FADE = 0.2
IDLE_FADE = 0.4
STOP_ENTRY_MAX = 0.3               # latest stop entry, seconds
INTERRUPT_STOP_AFTER = 0.4         # stops lacking AllowBlend
GAIT_DAMP = 4.0
LEAN_DAMP = 3.0
STEER_MIN, STEER_GAIN = 120.0, 3.0  # degrees per second
STEER_ACCEL = math.radians(2400.0)  # prevents snapped turns
STEP_UP = 0.5
SNAP_DOWN = 0.35
GRAVITY = 9.81
FOOTSTEP_ALPHA = 0.3               # minimum blend weight
FOOTSTEP_REPEAT = 0.1              # same-foot deduplication, seconds
EVADE_TURN = 0.1
EVADE_STEER_AFTER = 0.3
EVADE_STEER = math.radians(90.0)


def wrap(angle: float) -> float:
    return (angle + math.pi) % (2 * math.pi) - math.pi


@dataclass
class Clip:
    name: str
    length: float
    root_pos: np.ndarray           # (F, 3), object space
    root_yaw: np.ndarray           # (F,), unwrapped radians
    windows: list = field(default_factory=list)       # [(name, open, close)]
    events: list = field(default_factory=list)        # [(time, name)]
    sounds: list = field(default_factory=list)        # [(time, sound event, bone, footstep, cooldown)]
    poses: object = None
    effects: list = field(default_factory=list)       # [(time, effect name, slot, stop, duration, blood preview)]

    @property
    def speed(self) -> float:
        return float(np.linalg.norm(self.root_pos[-1, :2] - self.root_pos[0, :2]) / self.length) if self.length else 0.0

    @property
    def turn(self) -> float:
        return float(self.root_yaw[-1] - self.root_yaw[0])

    def root_at(self, t: float):
        if len(self.root_pos) < 2:
            return self.root_pos[0], float(self.root_yaw[0])
        t = min(max(t, 0.0), self.length)
        i = min(int(t * RATE), len(self.root_pos) - 2)
        span = min((i + 1) / RATE, self.length) - i / RATE        # final sample may be early
        a = min((t - i / RATE) / span, 1.0) if span > 1e-9 else 1.0
        return self.root_pos[i] * (1 - a) + self.root_pos[i + 1] * a, float(self.root_yaw[i] * (1 - a) + self.root_yaw[i + 1] * a)

    def window(self, name: str):
        """(open, close) of the longest window called `name`, or None."""
        spans = [(close - open_, open_, close) for n, open_, close in self.windows if n == name]
        return max(spans)[1:] if spans else None


def still_clip(name: str, length: float = 1.0) -> Clip:
    return Clip(name, length, np.zeros((2, 3)), np.zeros(2))


@dataclass
class Profile:
    name: str
    idle: Clip
    starts: dict                   # gait -> [Clip]
    loops: dict                    # gait -> (forward, left, right)
    stops: dict                    # gait -> {foot window: Clip}

    @property
    def clips(self) -> list:
        out = [self.idle]
        for clips in self.starts.values():
            out += clips
        for clips in self.loops.values():
            out += [c for c in clips if c is not None]
        for clips in self.stops.values():
            out += list(clips.values())
        return out


def _nearest(table: dict, gait: float):
    for i in sorted(range(len(GAITS)), key=lambda i: abs(i - gait)):
        if table.get(GAITS[i]):
            return table[GAITS[i]]
    return None


def _has_feet(clip) -> bool:
    return clip is not None and any(n in FEET for n, _, _ in clip.windows)


def gait_of(speed: float) -> float:
    return float(np.interp(speed, SPEEDS, range(len(SPEEDS))))


def bracket(clips, angle: float):
    """[(Clip, weight)] of the one or two clips whose turns enclose `angle`."""
    clips = sorted(clips, key=lambda c: c.turn)
    turns = [c.turn for c in clips]
    if angle <= turns[0]:
        return [(clips[0], 1.0)]
    if angle >= turns[-1]:
        return [(clips[-1], 1.0)]
    i = next(i for i in range(len(turns) - 1) if turns[i] <= angle <= turns[i + 1])
    a = (angle - turns[i]) / max(turns[i + 1] - turns[i], 1e-6)
    return [(clips[i], 1.0 - a), (clips[i + 1], a)]


class Layer:
    """Weighted clips sharing one normalized phase."""

    def __init__(self, state: str, entries, looping: bool, fade: float, weight: float = 0.0, phase: float = 0.0, yaw: bool = True):
        self.state = state
        self.entries = [(c, w) for c, w in entries if c is not None and w > 0.0]
        self.looping = looping
        self.phase = phase
        self.weight = weight
        self.target = 1.0
        self.fade = max(fade, 1e-3)
        self.yaw = yaw                 # loops steer instead of using clip yaw
        self.done = False

    @property
    def length(self) -> float:
        total = sum(w for _, w in self.entries)
        return (sum(c.length * w for c, w in self.entries) / total) if total else 1.0

    @property
    def time(self) -> float:
        return self.phase * self.length

    @property
    def dominant(self):
        return max(self.entries, key=lambda e: e[1])[0] if self.entries else None

    def clip_time(self, clip=None) -> float:
        clip = clip or self.dominant
        return self.phase * clip.length if clip else 0.0

    def window(self, names, dt: float):
        """Current or newly opened window: (name, fraction, seconds)."""
        clip = self.dominant
        if clip is None:
            return None
        t = self.clip_time(clip)
        step = dt * clip.length / self.length
        for name, open_, close in clip.windows:
            if name not in names:
                continue
            for tt in ((t, t + clip.length) if self.looping else (t,)):
                if open_ <= tt < close or tt - step < open_ <= tt:
                    return name, min(max((tt - open_) / max(close - open_, 1e-6), 0.0), 1.0), max(tt - open_, 0.0)
        return None

    def set_entries(self, entries):
        self.entries = [(c, w) for c, w in entries if c is not None and w > 0.0]

    def advance(self, dt: float):
        """Advance and return each clip's time span."""
        p0 = self.phase
        p1 = p0 + dt / self.length
        if self.looping:
            p1 %= 1.0
        elif p1 >= 1.0:
            p1, self.done = 1.0, True
        self.phase = p1
        return [(c, w, p0 * c.length, p1 * c.length) for c, w in self.entries]

    def step_fade(self, dt: float) -> bool:
        """Fade toward the target; False when fully faded out."""
        d = dt / self.fade
        self.weight = min(self.weight + d, self.target) if self.weight < self.target else max(self.weight - d, self.target)
        return self.target > 0.0 or self.weight > 0.0


class Controller:
    def __init__(self, profile: Profile, world=None, pos=(0.0, 0.0, 0.0), yaw: float = 0.0):
        self.profile = profile
        self.world = world
        self.x, self.y, self.z = (float(v) for v in pos)
        self.yaw = float(yaw)
        self.gait = 1.0                # continuous index into GAITS
        self.lean = 0.0                # damped turn rate, positive left
        self.turn_rate = 0.0
        self.speed = 0.0
        self.vz = 0.0
        self.wanted_yaw = self.yaw
        self.wanted_speed = SPEEDS[1]
        self.released = math.inf       # seconds since movement input
        self.action = ""               # active one-shot kind
        self.queued = None             # (Clip, kind)
        self.fired = []                # [(action kind, event name)]
        self.effects = []              # [(controller time, Clip.effects entry)]
        self.sounds = []               # [(sound event, bone, footstep)]
        self.clock = 0.0
        self._heard = {}               # (event, bone) -> last sound time
        self.layers = [Layer(IDLE, [(profile.idle, 1.0)], True, 1.0, 1.0)]
        self.upper = None              # upper-body one-shot over locomotion, no root motion
        self.state = IDLE
        self._settle(self.x, self.y, 0.0)

    @property
    def active(self) -> Layer:
        return self.layers[-1]

    @property
    def gait_name(self) -> str:
        """The nearest gait the profile has (a ghoul asked to sprint runs)."""
        order = sorted(range(len(GAITS)), key=lambda i: abs(i - self.gait))
        return next((GAITS[i] for i in order if self.profile.loops.get(GAITS[i])), GAITS[order[0]])

    def _push(self, layer: Layer):
        for old in self.layers:
            old.target = 0.0
            old.fade = min(old.fade, layer.fade)       # avoid lingering poses
        self.layers.append(layer)
        self.state = layer.state

    def _start(self, desired_yaw: float, speed: float):
        self.gait = gait_of(speed)
        clips = _nearest(self.profile.starts, self.gait)
        if not clips:
            return self._loop()
        self._push(Layer(START, bracket(clips, wrap(desired_yaw - self.yaw)), False, FADE))

    def _loop_entries(self):
        g = min(max(self.gait, 0.0), len(GAITS) - 1.0)
        i = min(int(g), len(GAITS) - 2)
        a = g - i
        out = []
        for gi, gw in ((i, 1.0 - a), (i + 1, a)):
            if gw <= 1e-4:
                continue
            forward, left, right = _nearest(self.profile.loops, gi) or (None, None, None)
            side = left if self.lean > 0.0 else right
            rate = abs(side.turn) / side.length if side is not None and side.length else 0.0
            k = min(abs(self.lean) / rate, 1.0) if rate > 1e-6 else 0.0      # match side-cycle turn rate
            out += [(forward, gw * (1.0 - k)), (side, gw * k)]
        return out

    def _loop(self, hit=None):
        layer = Layer(LOOP, self._loop_entries(), True, FADE, yaw=False)
        clip = layer.dominant
        span = clip.window(hit[0]) if hit is not None and clip is not None else None
        if span is not None:
            layer.phase = (span[0] + hit[1] * (span[1] - span[0])) / clip.length
        self._push(layer)

    def _stop(self, hit=None):
        stops = _nearest(self.profile.stops, round(self.gait)) or {}
        name = hit[0] if hit is not None else FEET[1]
        clip = stops.get(name) or stops.get(name.replace("Up", "Forward")) or next(iter(stops.values()), None)
        if clip is None:
            return self._idle()
        at = min(hit[2] if hit is not None else 0.0, STOP_ENTRY_MAX, clip.length)
        self._push(Layer(STOP, [(clip, 1.0)], False, FADE, phase=at / clip.length))

    def _idle(self):
        self._push(Layer(IDLE, [(self.profile.idle, 1.0)], True, IDLE_FADE))

    @property
    def locomoting(self) -> bool:
        return self.state in (START, LOOP, STOP)

    def play(self, clip: Clip, kind: str, fade: float = ACTION_FADE, upper: bool = False, face=None):
        """Play a one-shot using its own root motion, or on the upper body while locomotion carries on.

        face: (yaw, travel offset) turns the player so the clip travels along the stick, like an evade.
        """
        layer = Layer(ACTION, [(clip, 1.0)], False, fade, weight=self.upper.weight if upper and self.upper else 0.0)
        layer.kind, layer.face = kind, face
        self.action, self.queued = kind, None
        if upper:
            self.upper = layer
        else:
            if self.upper is not None:
                self.upper.target = 0.0
            self._push(layer)

    def queue(self, clip: Clip, kind: str, face=None):
        self.queued = (clip, kind, face)

    def _play_queued(self, upper: bool = False):
        clip, kind, face = self.queued
        self.play(clip, kind, upper=upper and face is None, face=face)

    def _face(self, layer: Layer, held: bool, dt: float):
        """Evade turning (combat.ws): reach the heading in 0.1 s, then follow the stick at 90 deg/s from 0.3 s."""
        yaw, offset = layer.face
        t = layer.time
        if t < EVADE_TURN:
            self.yaw = wrap(self.yaw + wrap(yaw - self.yaw) * min(1.0, dt / (EVADE_TURN - t)))
        elif t >= EVADE_STEER_AFTER and held:
            diff = wrap(self.wanted_yaw - offset - self.yaw)
            self.yaw = wrap(self.yaw + max(-EVADE_STEER * dt, min(EVADE_STEER * dt, diff)))

    @property
    def action_layer(self):
        return self.upper if self.upper is not None and self.upper.target > 0.0 else (self.active if self.state == ACTION else None)

    @property
    def action_time(self) -> float:
        layer = self.action_layer
        return layer.time if layer is not None else 0.0

    def window_open(self, name: str) -> bool:
        layer = self.action_layer
        clip = layer.dominant if layer is not None else None
        span = clip.window(name) if clip is not None else None
        return span is not None and span[0] <= layer.time < span[1]

    def set_profile(self, profile: Profile):
        """Swap locomotion sets; a running loop crossfades in at the same foot."""
        if profile is self.profile:
            return
        self.profile = profile
        if self.state == LOOP:
            old = self.active
            hit = old.window(FEET, 0.0)
            self._loop(hit)
            if hit is None:
                self.active.phase = old.phase

    @staticmethod
    def _exit_time(clip: Clip) -> float:
        span = clip.window("AllowBlend")
        if span is not None:
            return span[0]
        return next((t for t, n in clip.events if n in EXIT_EVENTS), clip.length - FADE)

    def _can_interrupt(self, layer: Layer) -> bool:
        clip = layer.dominant
        span = clip.window("AllowBlend") if clip is not None else None
        t = layer.clip_time(clip)
        return span[0] <= t if span is not None else t >= INTERRUPT_STOP_AFTER

    def _steer(self, desired_yaw: float, dt: float):
        if dt <= 0.0:
            return
        diff = wrap(desired_yaw - self.yaw)
        rate = min(math.radians(STEER_MIN + STEER_GAIN * abs(math.degrees(diff))), math.sqrt(2.0 * STEER_ACCEL * abs(diff)))
        wanted = math.copysign(rate, diff)
        step = STEER_ACCEL * dt
        self.turn_rate = min(max(wanted, self.turn_rate - step), self.turn_rate + step)
        turn = self.turn_rate * dt
        if abs(turn) >= abs(diff) and turn * diff >= 0.0:
            turn = diff
            self.turn_rate = diff / dt
        self.yaw = wrap(self.yaw + turn)

    def step(self, dt: float, move=(0.0, 0.0), speed: float = SPEEDS[1], stop_delay: float = 0.15):
        """Advance one fixed step; stop_delay extends input briefly after release."""
        mx, my = move
        if math.hypot(mx, my) >= MOVE_DEADZONE:
            self.wanted_yaw = math.atan2(-mx, my)
            self.wanted_speed = speed
            self.released = 0.0
        else:
            self.released += dt
        moving = self.released <= stop_delay
        if moving:
            self.gait += (gait_of(self.wanted_speed) - self.gait) * min(1.0, GAIT_DAMP * dt)
        state, active = self.state, self.active
        steered = False
        if state == IDLE:
            if moving:
                self._start(self.wanted_yaw, self.wanted_speed)
        elif state == START:
            hit = active.window(FEET, dt)
            clip = active.dominant
            ending = active.done or (not _has_feet(clip) and active.time >= active.length - FADE)
            if hit is not None or ending:
                (self._loop if moving else self._stop)(hit)
        elif state == LOOP:
            if moving:
                self._steer(self.wanted_yaw, dt)
                steered = True
                active.set_entries(self._loop_entries())
            else:
                hit = active.window(FEET, dt)
                if hit is not None or not _has_feet(active.dominant):
                    self._stop(hit)
        elif state == STOP:
            if moving and self._can_interrupt(active):
                self._start(self.wanted_yaw, self.wanted_speed)
            elif active.done:
                self._idle()
        elif state == ACTION:
            clip, t = active.dominant, active.time
            if getattr(active, "face", None) is not None:
                self._face(active, self.released == 0.0, dt)
            if self.queued is not None and (self.window_open("ActionBlend") or t >= self._exit_time(clip) or active.done):
                self._play_queued()
            elif moving and t >= self._exit_time(clip):
                self.action = ""
                self._start(self.wanted_yaw, self.wanted_speed)
            elif active.done:
                self.action = ""
                self._idle()
            elif moving and active.face is None and self.window_open("RotateEvent"):
                self._steer(self.wanted_yaw, dt)
                steered = True
        up = self.upper
        if up is not None and up.target > 0.0:
            clip, t = up.dominant, up.time
            if self.queued is not None and (self.window_open("ActionBlend") or t >= self._exit_time(clip) or up.done):
                self._play_queued(upper=self.locomoting)
            elif up.done or t >= self._exit_time(clip):
                up.target, up.fade, self.action = 0.0, ACTION_FADE, ""
        if not steered:
            self.turn_rate = 0.0
        self.lean += (self.turn_rate - self.lean) * min(1.0, LEAN_DAMP * dt)

        dx = dy = dyaw = total = 0.0
        self.fired, self.sounds, self.effects = [], [], []
        self.clock += dt
        for layer in self.layers:
            motion = layer.advance(dt)
            if layer.state == ACTION and motion and layer.target > 0.0:
                clip, _, t0, t1 = motion[0]
                self.fired += [(layer.kind, n) for t, n in clip.events if t0 < t <= t1 or (t0 == t == 0.0)]
                self.effects += [(self.clock - (t1 - e[0]), e) for e in clip.effects
                                 if t0 < e[0] <= t1 or (t0 == e[0] == 0.0)]
            if motion:
                self._hear(layer, *max(motion, key=lambda m: m[1]))
            if layer.weight <= 0.0:
                continue
            for clip, w, t0, t1 in motion:
                k = layer.weight * w
                spans = ((t0, clip.length), (0.0, t1)) if t1 < t0 else ((t0, t1),)
                for a, b in spans:
                    pa, ya = clip.root_at(a)                  # sample each part in its starting heading
                    pb, yb = clip.root_at(b)
                    d = pb - pa
                    c, s = math.cos(ya), math.sin(ya)
                    dx += k * (c * d[0] + s * d[1])
                    dy += k * (-s * d[0] + c * d[1])
                    dyaw += k * (yb - ya) if layer.yaw else 0.0
                total += k
        if total > 0.0:
            dx, dy, dyaw = dx / total, dy / total, dyaw / total
        c, s = math.cos(self.yaw), math.sin(self.yaw)
        wx, wy = c * dx - s * dy, s * dx + c * dy
        self.yaw = wrap(self.yaw + dyaw)
        self.speed = math.hypot(wx, wy) / dt if dt > 0 else 0.0
        ox, oy = self.x, self.y
        self.x += wx
        self.y += wy
        self._settle(ox, oy, dt)
        self.layers = [layer for layer in self.layers if layer.step_fade(dt)]
        if self.upper is not None:
            motion = self.upper.advance(dt)
            if self.upper.target > 0.0:
                clip, _, t0, t1 = motion[0]
                self.fired += [(self.upper.kind, n) for t, n in clip.events if t0 < t <= t1 or (t0 == t == 0.0)]
                self.effects += [(self.clock - (t1 - e[0]), e) for e in clip.effects
                                 if t0 < e[0] <= t1 or (t0 == e[0] == 0.0)]
                self._hear(self.upper, *motion[0])
            if not self.upper.step_fade(dt):
                self.upper = None
        return self.state

    def _hear(self, layer: Layer, clip: Clip, w: float, t0: float, t1: float):
        """Emit sounds crossed by the dominant clip; footsteps need blend weight."""
        spans = ((t0, clip.length), (0.0, t1)) if t1 < t0 else ((t0, t1),)
        ending = t0 < t1 >= clip.length
        for t, event, bone, footstep, cooldown in clip.sounds:
            if not (any(a <= t < b for a, b in spans) or (ending and t >= clip.length)):
                continue
            if (layer.weight * w <= FOOTSTEP_ALPHA) if footstep else layer.target <= 0.0:
                continue
            last = self._heard.get((event, bone))
            if last is not None and self.clock - last < (max(cooldown, FOOTSTEP_REPEAT) if footstep else cooldown):
                continue
            self._heard[(event, bone)] = self.clock
            self.sounds.append((event, bone, footstep))

    def _settle(self, ox: float, oy: float, dt: float):
        if self.world is None:
            return
        self.x, self.y = self.world.slide(ox, oy, self.z, self.x, self.y)
        floor = self.world.floor(self.x, self.y, self.z)
        if floor is None:
            self.vz = 0.0
        elif floor >= self.z - SNAP_DOWN and self.vz >= 0.0:
            self.z, self.vz = floor, 0.0
        else:
            self.vz -= GRAVITY * dt
            self.z += self.vz * dt
            if self.z <= floor:
                self.z, self.vz = floor, 0.0

    def pose_mix(self) -> list:
        """[(Clip, clip time, weight)] of everything playing, weights summing to 1."""
        out = []
        for layer in self.layers:
            for clip, w in layer.entries:
                k = layer.weight * w
                if k > 1e-4:
                    out.append((clip, layer.phase * clip.length, k))
        total = sum(k for _, _, k in out) or 1.0
        return [(c, t, k / total) for c, t, k in out]

    def upper_mix(self):
        """([(Clip, clip time, 1.0)], weight) of the upper-body one-shot, or ([], 0.0)."""
        up = self.upper
        if up is None or up.weight <= 0.0:
            return [], 0.0
        return [(c, up.phase * c.length, 1.0) for c, _ in up.entries], up.weight

    @property
    def clip_name(self) -> str:
        mix = self.pose_mix()
        return max(mix, key=lambda e: e[2])[0].name if mix else ""
