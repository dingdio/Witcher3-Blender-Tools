import math
import sys
import types
import unittest
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

if "witcher3_tools" not in sys.modules:
    _pkg = types.ModuleType("witcher3_tools")
    _pkg.__path__ = [str(REPO_ROOT / "witcher3_tools")]
    _pkg.__package__ = "witcher3_tools"
    _pkg.get_addon_name = lambda: "witcher3_tools"
    sys.modules["witcher3_tools"] = _pkg

from witcher3_tools.controller import core, freecam  # noqa: E402

DT = 1.0 / 60.0
LOOP_WINDOWS = [("LeftUp", 0.0, 0.03), ("LeftForward", 0.03, 0.26), ("RightUp", 0.26, 0.51),
                ("RightForward", 0.51, 0.76), ("LeftUp", 0.76, 1.0)]


def clip(name, length, dist=0.0, turn=0.0, windows=(), drift_yaw=0.0):
    frames = int(math.ceil(length * core.RATE)) + 1
    t = np.minimum(np.arange(frames) / core.RATE, length) / length
    yaw = turn * t + drift_yaw * t
    pos = np.zeros((frames, 3))
    step = dist / (frames - 1)
    for i in range(1, frames):
        pos[i] = pos[i - 1] + (-math.sin(yaw[i - 1]) * step, math.cos(yaw[i - 1]) * step, 0.0)
    return core.Clip(name, length, pos, yaw, [(n, o * length, c * length) for n, o, c in windows])


def profile():
    starts = {}
    for gait, length in (("walk", 0.633), ("run", 0.533), ("sprint", 0.6)):
        starts[gait] = [clip(f"{gait}_start_{d}", length, 0.6, math.radians(d), [("LeftUp" if d >= 0 else "RightUp", 0.63, 1.0)])
                        for d in (-180, -90, -45, 0, 45, 90, 180)]
    loops = {"walk": (clip("walk_loop", 1.0, 1.9, windows=LOOP_WINDOWS), None, None),
             "run": (clip("run_loop", 0.667, 3.0, windows=LOOP_WINDOWS), clip("run_left", 0.667, 3.0, windows=LOOP_WINDOWS, drift_yaw=0.5),
                     clip("run_right", 0.667, 3.0, windows=LOOP_WINDOWS, drift_yaw=-0.5)),
             "sprint": (clip("sprint_loop", 0.6, 3.6, windows=LOOP_WINDOWS), None, None)}
    stops = {g: {f: clip(f"{g}_stop_{f}", 1.5, 0.8, windows=[(f, 0.0, 1.0), ("AllowBlend", 0.3, 1.0)]) for f in core.FEET}
             for g in ("walk", "run")}
    stops["sprint"] = {f: clip(f"sprint_stop_{f}", 1.8, 1.2, windows=[(f, 0.0, 1.0)]) for f in ("LeftForward", "RightForward")}
    return core.Profile("test", clip("idle", 5.0), starts, loops, stops)


def run(ctl, seconds, move=(0.0, 0.0), speed=1.0, stop_delay=0.15):
    for _ in range(int(round(seconds / DT))):
        ctl.step(DT, move, speed, stop_delay)


class FlatWorld:
    def __init__(self, wall_y=None):
        self.wall_y = wall_y

    def floor(self, x, y, z):
        return 1.0 if x > 1.0 else 0.0

    def slide(self, x0, y0, z, x1, y1):
        if self.wall_y is not None and y1 > self.wall_y:
            return x1, y0
        return x1, y1


class PlayerCoreTests(unittest.TestCase):
    def test_idle_stays_put(self):
        ctl = core.Controller(profile())
        run(ctl, 1.0)
        self.assertEqual(ctl.state, core.IDLE)
        self.assertAlmostEqual(ctl.y, 0.0)

    def test_start_blends_the_two_neighbouring_turns(self):
        clips = profile().starts["run"]
        mix = core.bracket(clips, math.radians(60))
        self.assertEqual([c.name for c, _ in mix], ["run_start_45", "run_start_90"])
        self.assertAlmostEqual(mix[0][1], 2.0 / 3.0, places=3)
        self.assertEqual(core.bracket(clips, math.radians(90))[-1][0].name, "run_start_90")
        last, weight = core.bracket(clips, math.radians(179.9))[-1]
        self.assertEqual(last.name, "run_start_180")
        self.assertGreater(weight, 0.99)

    def test_run_reaches_loop_and_travels_forward(self):
        ctl = core.Controller(profile())
        ctl.step(DT, (0.0, 1.0))
        self.assertEqual(ctl.state, core.START)
        run(ctl, 3.0, (0.0, 1.0))
        self.assertEqual(ctl.state, core.LOOP)
        self.assertEqual(ctl.clip_name, "run_loop")
        self.assertGreater(ctl.y, 6.0)
        self.assertAlmostEqual(ctl.x, 0.0, places=3)
        y = ctl.y
        run(ctl, 1.334, (0.0, 1.0))
        self.assertAlmostEqual((ctl.y - y) / 1.334, 3.0 / 0.667, delta=0.05)

    def test_turning_start_ends_facing_the_stick(self):
        ctl = core.Controller(profile())
        run(ctl, 1.5, (-1.0, 0.0))
        self.assertAlmostEqual(math.degrees(ctl.yaw), 90.0, delta=2.0)
        self.assertLess(ctl.x, -1.0)

    def test_stop_waits_for_the_delay_then_matches_the_foot_window(self):
        ctl = core.Controller(profile())
        run(ctl, 3.0, (0.0, 1.0))
        loop = ctl.active
        while not (0.03 < loop.clip_time() < 0.05):
            ctl.step(DT, (0.0, 1.0))
        ctl.step(DT)
        self.assertEqual(ctl.state, core.LOOP)
        run(ctl, 0.15)
        self.assertEqual(ctl.state, core.STOP)
        self.assertEqual(ctl.active.dominant.name, "run_stop_RightUp")

    def test_stop_settles_into_idle(self):
        ctl = core.Controller(profile())
        run(ctl, 3.0, (0.0, 1.0))
        run(ctl, 3.0)
        self.assertEqual(ctl.state, core.IDLE)

    def test_stop_lets_a_new_start_in_from_allow_blend(self):
        ctl = core.Controller(profile())
        run(ctl, 3.0, (0.0, 1.0))
        run(ctl, 0.2)
        self.assertEqual(ctl.state, core.STOP)
        ctl.step(DT, (0.0, 1.0))
        self.assertEqual(ctl.state, core.STOP)
        run(ctl, 0.6, (0.0, 1.0))
        self.assertIn(ctl.state, (core.START, core.LOOP))

    def test_sprint_uses_the_sprint_loop_and_its_forward_stops(self):
        ctl = core.Controller(profile())
        run(ctl, 3.0, (0.0, 1.0), speed=1.5)
        self.assertEqual(ctl.clip_name, "sprint_loop")
        loop = ctl.active
        while not (0.48 < loop.clip_time() < 0.50):
            ctl.step(DT, (0.0, 1.0), 1.5)
        run(ctl, 0.2)
        self.assertEqual(ctl.active.dominant.name, "sprint_stop_LeftForward")

    def test_loop_steers_and_ignores_cycle_yaw(self):
        ctl = core.Controller(profile())
        run(ctl, 3.0, (0.0, 1.0))
        yaw = ctl.yaw
        run(ctl, 1.0, (0.0, 1.0))
        self.assertAlmostEqual(ctl.yaw, yaw, places=4)
        run(ctl, 1.0, (1.0, 0.0))
        self.assertAlmostEqual(math.degrees(ctl.yaw), -90.0, delta=1.0)

    def test_lean_picks_the_side_cycle_while_turning(self):
        ctl = core.Controller(profile())
        run(ctl, 3.0, (0.0, 1.0))
        ctl.step(DT, (-1.0, 0.0))
        ctl.step(DT, (-1.0, 0.0))
        self.assertGreater(ctl.lean, 0.0)
        self.assertIn("run_left", [c.name for c, _ in ctl.active.entries])

    def test_world_floor_steps_and_walls_block(self):
        ctl = core.Controller(profile(), FlatWorld(wall_y=2.0), pos=(0.0, 0.0, 0.0))
        run(ctl, 3.0, (0.0, 1.0))
        self.assertLessEqual(ctl.y, 2.0)
        self.assertEqual(ctl.z, 0.0)
        ctl.x = 2.0
        ctl.step(DT, (0.0, 1.0))
        self.assertEqual(ctl.z, 1.0)

    def test_falls_with_gravity_onto_a_lower_floor(self):
        ctl = core.Controller(profile(), FlatWorld(), pos=(0.0, 0.0, 3.0))
        self.assertGreater(ctl.z, 2.0)
        run(ctl, 0.3)
        self.assertGreater(ctl.z, 0.0)
        run(ctl, 1.0)
        self.assertEqual(ctl.z, 0.0)

    def test_circling_cycle_keeps_its_speed_across_the_wrap(self):
        prof = profile()
        circle = clip("circle", 0.6, 2.4, windows=LOOP_WINDOWS, drift_yaw=3.9)
        prof.loops = {"run": (circle, None, None)}
        ctl = core.Controller(prof)
        run(ctl, 2.0, (0.0, 1.0))
        steps = []
        for _ in range(90):
            x, y = ctl.x, ctl.y
            ctl.step(DT, (0.0, 1.0))
            steps.append(math.hypot(ctl.x - x, ctl.y - y))
        self.assertLess(max(steps) / min(steps), 1.2)

    def test_steering_ramps_the_turn_rate(self):
        ctl = core.Controller(profile())
        run(ctl, 3.0, (0.0, 1.0))
        rates = []
        for _ in range(60):
            ctl.step(DT, (0.0, -1.0))
            rates.append(ctl.turn_rate)
        jumps = [abs(b - a) for a, b in zip([0.0] + rates, rates)]
        self.assertLessEqual(max(jumps[:-20]), core.STEER_ACCEL * DT + 1e-6)
        self.assertLessEqual(max(jumps), 2.0 * core.STEER_ACCEL * DT)
        self.assertAlmostEqual(abs(math.degrees(ctl.yaw)), 180.0, delta=1.0)

    @staticmethod
    def attack(name="attack"):
        c = clip(name, 1.6, 0.6, windows=[("AllowInput", 0.3 / 1.6, 1.0), ("ActionBlend", 0.5 / 1.6, 1.0), ("AllowBlend", 0.87 / 1.6, 1.0),
                                          ("RotateEvent", 0.0, 0.3 / 1.6)])
        c.events = [(0.3, "CombatStanceLeft"), (0.66, "item")]
        return c

    def test_action_plays_once_then_idles(self):
        ctl = core.Controller(profile())
        ctl.play(self.attack(), "attack")
        self.assertEqual((ctl.state, ctl.action), (core.ACTION, "attack"))
        fired = []
        for _ in range(int(2.0 / DT)):
            ctl.step(DT)
            fired += ctl.fired
        self.assertEqual(fired, [("attack", "CombatStanceLeft"), ("attack", "item")])
        self.assertEqual((ctl.state, ctl.action), (core.IDLE, ""))
        self.assertGreater(ctl.y, 0.4)

    def test_queued_action_starts_in_its_action_blend_window(self):
        ctl = core.Controller(profile())
        ctl.play(self.attack("first"), "attack")
        run(ctl, 0.35)
        self.assertTrue(ctl.window_open("AllowInput"))
        ctl.queue(self.attack("second"), "attack")
        run(ctl, 0.1)
        self.assertEqual(ctl.active.dominant.name, "first")
        run(ctl, 0.1)
        self.assertEqual(ctl.active.dominant.name, "second")
        self.assertIsNone(ctl.queued)

    def test_stick_takes_over_from_allow_blend(self):
        ctl = core.Controller(profile())
        ctl.play(self.attack(), "attack")
        run(ctl, 0.32, (1.0, 0.0))
        turned = ctl.yaw
        self.assertLess(turned, math.radians(-30.0))
        run(ctl, 0.28, (1.0, 0.0))
        self.assertEqual(ctl.state, core.ACTION)
        self.assertAlmostEqual(ctl.yaw, turned, places=6)
        run(ctl, 0.4, (1.0, 0.0))
        self.assertIn(ctl.state, (core.START, core.LOOP))
        self.assertEqual(ctl.action, "")

    def test_pose_mix_weights_sum_to_one(self):
        ctl = core.Controller(profile())
        for _ in range(40):
            ctl.step(DT, (0.0, 1.0))
            self.assertAlmostEqual(sum(w for _, _, w in ctl.pose_mix()), 1.0, places=6)

    @staticmethod
    def draw():
        c = clip("draw", 1.2, 0.0)
        c.events = [(0.5, "item"), (0.9, "OnWeaponDrawReady")]
        return c

    def test_upper_body_draw_keeps_running(self):
        ctl = core.Controller(profile())
        run(ctl, 1.0, (0.0, 1.0))
        self.assertEqual(ctl.state, core.LOOP)
        y = ctl.y
        ctl.play(self.draw(), "draw:steelsword", upper=True)
        fired = []
        for _ in range(int(0.95 / DT)):
            ctl.step(DT, (0.0, 1.0))
            fired += ctl.fired
            self.assertEqual(ctl.state, core.LOOP)
        self.assertGreater(ctl.y - y, 2.0)                  # the draw has no say in root motion
        self.assertEqual([n for _, n in fired], ["item", "OnWeaponDrawReady"])
        self.assertEqual(ctl.action, "")                    # released at OnWeaponDrawReady
        run(ctl, 0.3, (0.0, 1.0))
        self.assertIsNone(ctl.upper)
        self.assertEqual(ctl.upper_mix(), ([], 0.0))

    def test_upper_body_draw_fades_in_and_reports_its_time(self):
        ctl = core.Controller(profile())
        run(ctl, 1.0, (0.0, 1.0))
        ctl.play(self.draw(), "draw:steelsword", upper=True)
        run(ctl, 0.3, (0.0, 1.0))
        mix, weight = ctl.upper_mix()
        self.assertEqual([c.name for c, _, _ in mix], ["draw"])
        self.assertAlmostEqual(weight, 1.0)
        self.assertAlmostEqual(ctl.action_time, 0.3, places=6)

    def test_full_body_action_replaces_an_upper_one(self):
        ctl = core.Controller(profile())
        run(ctl, 1.0, (0.0, 1.0))
        ctl.play(self.draw(), "draw:steelsword", upper=True)
        run(ctl, 0.1, (0.0, 1.0))
        ctl.play(self.attack(), "attack")
        self.assertEqual(ctl.state, core.ACTION)
        self.assertEqual(ctl.action_layer.dominant.name, "attack")
        run(ctl, 0.2)
        self.assertIsNone(ctl.upper)

    def test_profile_swap_crossfades_the_loop_at_the_same_foot(self):
        ctl = core.Controller(profile())
        run(ctl, 1.3, (0.0, 1.0))
        old = ctl.active
        foot = old.window(core.FEET, 0.0)[0]
        other = profile()
        ctl.set_profile(other)
        self.assertIsNot(ctl.active, old)
        self.assertEqual(ctl.active.state, core.LOOP)
        self.assertIs(ctl.active.dominant, other.loops["run"][0])
        self.assertEqual(ctl.active.window(core.FEET, 0.0)[0], foot)
        self.assertAlmostEqual(ctl.active.weight, 0.0)


class EvadeTests(unittest.TestCase):
    @staticmethod
    def dodge(name="dodge", travel=0.0):
        c = clip(name, 0.8, 3.5)
        c.root_pos = c.root_pos @ np.array([[math.cos(travel), math.sin(travel), 0], [-math.sin(travel), math.cos(travel), 0], [0, 0, 1]])
        c.windows = [("ActionBlend", 0.5, 0.8)]
        return c

    def test_turns_to_the_heading_in_a_tenth_of_a_second(self):
        ctl = core.Controller(profile())
        ctl.step(DT, (-1.0, 0.0))                           # stick to the left (+90 deg)
        left = math.radians(90.0)
        ctl.play(self.dodge(travel=-left), "dodge", face=(core.wrap(ctl.wanted_yaw + left), -left))   # a right-travelling clip
        run(ctl, 0.1, (-1.0, 0.0))
        self.assertAlmostEqual(abs(math.degrees(ctl.yaw)), 180.0, delta=1.0)
        start = (ctl.x, ctl.y)
        run(ctl, 0.2, (-1.0, 0.0))
        self.assertLess(ctl.x - start[0], -0.5)             # travels along the stick, not the facing

    def test_follows_the_stick_at_90_degrees_a_second_after_0_3_s(self):
        ctl = core.Controller(profile())
        ctl.step(DT, (0.0, 1.0))
        ctl.play(self.dodge(), "dodge", face=(0.0, 0.0))
        run(ctl, 0.3, (0.0, 1.0))
        self.assertAlmostEqual(ctl.yaw, 0.0, places=6)
        run(ctl, 0.2, (1.0, 0.0))                           # stick swings right
        self.assertAlmostEqual(math.degrees(ctl.yaw), -18.0, delta=1.6)

    def test_a_second_evade_waits_for_action_blend(self):
        ctl = core.Controller(profile())
        ctl.play(self.dodge("first"), "dodge")
        run(ctl, 0.2)
        ctl.queue(self.dodge("second"), "dodge", (0.0, 0.0))
        run(ctl, 0.25)
        self.assertEqual(ctl.active.dominant.name, "first")
        run(ctl, 0.1)
        self.assertEqual(ctl.active.dominant.name, "second")
        self.assertIsNotNone(ctl.active.face)


class FreeCameraTests(unittest.TestCase):
    def test_starts_two_metres_above_the_player_with_his_yaw(self):
        cam = freecam.FreeCamera.at_player(1.0, 2.0, 3.0, math.radians(90.0))
        self.assertEqual(cam.pos, [1.0, 2.0, 5.0])
        self.assertAlmostEqual(cam.rot[0], 90.0)

    def test_keyboard_speed_matches_the_engine(self):
        cam = freecam.FreeCamera((0.0, 0.0, 0.0), 0.0)
        cam.keys(1.0, 0.0, 0.0, False, 0.0)
        for _ in range(60):
            cam.tick(DT)
        self.assertAlmostEqual(cam.pos[1], 25.0, places=4)  # key 5 x speed 5
        cam.keys(1.0, 0.0, 0.0, True, 0.0)
        for _ in range(60):
            cam.tick(DT)
        self.assertAlmostEqual(cam.pos[1], 25.0 + 150.0, places=3)  # Shift: speed 5 + 5 * 5

    def test_yaw_left_moves_along_minus_x_and_q_rises_slowly(self):
        cam = freecam.FreeCamera((0.0, 0.0, 0.0), 90.0)
        cam.keys(1.0, 1.0, 1.0, False, 0.0)
        cam.tick(1.0)
        self.assertAlmostEqual(cam.pos[0], -25.0, places=4)
        self.assertAlmostEqual(cam.pos[1], 25.0, places=4)   # right of a camera facing -X is +Y
        self.assertAlmostEqual(cam.pos[2], 1.25, places=4)

    def test_view_smooths_toward_the_target(self):
        cam = freecam.FreeCamera((0.0, 0.0, 0.0), 0.0)
        cam.mouse(-30.0, 0.0)
        cam.tick(DT)
        self.assertAlmostEqual(cam.rot[0], 10.0)             # 30 counts x 20 deg/s x 1/60 s
        self.assertAlmostEqual(cam.visible_rot[0], 6.0)
        cam.tick(DT)
        self.assertAlmostEqual(cam.rot[0], 10.0)             # look input lasts one tick
        self.assertAlmostEqual(cam.visible_rot[0], 8.4)

    def test_fov_is_clamped(self):
        cam = freecam.FreeCamera((0.0, 0.0, 0.0), 0.0)
        cam.zoom(500.0)
        self.assertEqual(cam.fov, 150.0)
        cam.zoom(-500.0)
        self.assertEqual(cam.fov, 5.0)


class EffectEventTests(unittest.TestCase):
    def test_payloads_fire_once_with_their_crossing_time(self):
        attack = PlayerCoreTests.attack()
        attack.effects = [(0.0, 'start', 'r_weapon', False, None, False),
                          (0.253, 'light_trail_fx', 'r_weapon', False, 0.2, False),
                          (0.4, 'default_blood_trail', 'r_weapon', False, None, True),
                          (0.6, 'light_trail_fx', 'r_weapon', True, None, False)]
        for upper in (False, True):
            ctl = core.Controller(profile())
            ctl.play(attack, 'attack', upper=upper)
            fired = []
            for _ in range(90):
                ctl.step(DT, (0.0, 0.0), 1.0)
                fired.extend(ctl.effects)
            self.assertEqual([e for _, e in fired], attack.effects)
            for time, event in fired:
                self.assertAlmostEqual(time, event[0])

    def test_replaced_action_does_not_start_an_effect(self):
        first, second = PlayerCoreTests.attack('first'), PlayerCoreTests.attack('second')
        first.effects = [(0.2, 'old', 'r_weapon', False, None, False)]
        second.effects = [(0.2, 'new', 'r_weapon', False, None, False)]
        ctl = core.Controller(profile())
        ctl.play(first, 'attack')
        run(ctl, 0.1)
        ctl.play(second, 'attack')
        fired = []
        for _ in range(60):
            ctl.step(DT, (0.0, 0.0), 1.0)
            fired.extend(ctl.effects)
        self.assertEqual([e[1] for _, e in fired], ['new'])


class SoundEventTests(unittest.TestCase):
    @staticmethod
    def heard(ctl, seconds, move=(0.0, 0.0), speed=1.0):
        out = []
        for _ in range(int(round(seconds / DT))):
            ctl.step(DT, move, speed)
            out += [(ctl.clock, *s) for s in ctl.sounds]
        return out

    def test_loop_footsteps_alternate_across_the_wrap(self):
        prof = profile()
        prof.loops["walk"][0].sounds = [(0.0, "step", "l_foot", True, 0.2), (0.5, "step", "r_foot", True, 0.2)]
        ctl = core.Controller(prof)
        run(ctl, 1.5, (0.0, 1.0), speed=0.6)
        steps = self.heard(ctl, 4.0, (0.0, 1.0), speed=0.6)
        self.assertEqual(ctl.state, core.LOOP)
        self.assertIn(len(steps), (7, 8, 9))
        bones = [s[2] for s in steps]
        self.assertTrue(all(a != b for a, b in zip(bones, bones[1:])))
        gaps = [b[0] - a[0] for a, b in zip(steps, steps[1:])]
        self.assertTrue(all(abs(g - 0.5) < 0.05 for g in gaps))

    def test_one_shot_sounds_fire_once_up_to_its_end(self):
        attack = PlayerCoreTests.attack()
        attack.sounds = [(0.0, "clothes", "torso", False, 0.0), (0.164, "grunt", "head", False, 0.0),
                         (0.256, "swoosh", "r_hand", False, 0.0), (attack.length, "end", "torso", False, 0.0)]
        ctl = core.Controller(profile())
        ctl.play(attack, "attack")
        self.assertEqual([s[1] for s in self.heard(ctl, 2.0)], ["clothes", "grunt", "swoosh", "end"])

    def test_footsteps_need_blend_weight_other_sounds_do_not(self):
        attack = PlayerCoreTests.attack()
        attack.sounds = [(0.0, "step", "l_foot", True, 0.2), (0.0, "clothes", "torso", False, 0.0),
                         (0.5, "step", "r_foot", True, 0.2)]
        ctl = core.Controller(profile())
        ctl.play(attack, "attack")
        self.assertEqual([(s[1], s[2]) for s in self.heard(ctl, 1.0)], [("clothes", "torso"), ("step", "r_foot")])

    def test_filter_cooldown_and_footstep_repeat(self):
        attack = PlayerCoreTests.attack()
        attack.sounds = [(0.3, "step", "l_foot", True, 0.0), (0.35, "step", "l_foot", True, 0.0),
                         (0.36, "step", "r_foot", True, 0.0), (0.4, "rustle", "torso", False, 0.5),
                         (0.6, "rustle", "torso", False, 0.5), (1.0, "rustle", "torso", False, 0.5)]
        ctl = core.Controller(profile())
        ctl.play(attack, "attack")
        heard = [(round(t - DT, 2), e, b) for t, e, b, _ in self.heard(ctl, 1.6)]
        self.assertEqual([(e, b) for _, e, b in heard],
                         [("step", "l_foot"), ("step", "r_foot"), ("rustle", "torso"), ("rustle", "torso")])

    def test_a_replaced_action_stays_quiet(self):
        first, second = PlayerCoreTests.attack("first"), PlayerCoreTests.attack("second")
        first.sounds = [(0.164, "grunt_first", "head", False, 0.0)]
        second.sounds = [(0.164, "grunt_second", "head", False, 0.0)]
        ctl = core.Controller(profile())
        ctl.play(first, "attack")
        run(ctl, 0.1)
        ctl.play(second, "attack")
        self.assertEqual([s[1] for s in self.heard(ctl, 1.0)], ["grunt_second"])


if __name__ == "__main__":
    unittest.main()
