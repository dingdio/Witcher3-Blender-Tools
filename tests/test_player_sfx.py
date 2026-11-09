import random
import struct
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

if "witcher3_tools" not in sys.modules:
    _pkg = types.ModuleType("witcher3_tools")
    _pkg.__path__ = [str(REPO_ROOT / "witcher3_tools")]
    _pkg.__package__ = "witcher3_tools"
    _pkg.get_addon_name = lambda: "witcher3_tools"
    sys.modules["witcher3_tools"] = _pkg

from witcher3_tools.controller import sfx  # noqa: E402
from witcher3_tools.CR2W.witcher_cache.SoundCache import wwise_bank as wb  # noqa: E402

H = wb.fnv1_32


def f32(value):
    return struct.unpack("<I", struct.pack("<f", value))[0]


class FakeSound:
    def __init__(self, wem, ops=()):
        self.wem, self.ops = wem, ops

    def loop(self, count):
        return FakeSound(self.wem, self.ops + (("loop", count),))

    def delay(self, seconds):
        return FakeSound(self.wem, self.ops + (("delay", round(seconds, 3)),))


class FakeHandle:
    def __init__(self, sound):
        self.sound, self.status, self.volume, self.pitch = sound, True, 1.0, 1.0

    def stop(self):
        self.status = False


class FakeDevice:
    def __init__(self):
        self.handles = []

    def play(self, sound):
        self.handles.append(FakeHandle(sound))
        return self.handles[-1]


def graph():
    g = wb.SoundGraph()
    n = g.nodes
    n[1] = wb.Node(wb.ACTOR_MIXER, 1, props={wb.VOLUME: f32(1.0)})
    for i, wem in enumerate((101, 102, 103)):
        n[10 + i] = wb.Node(wb.SOUND, 10 + i, parent=20, source=wem)
    n[20] = wb.Node(wb.RANSEQ, 20, parent=30, avoid_repeat=2, playlist=[(10, 50000), (11, 50000), (12, 50000)])
    n[13] = wb.Node(wb.SOUND, 13, parent=30, source=201, props={wb.VOLUME: f32(-3.0), wb.PITCH: f32(1200.0)})
    n[30] = wb.Node(wb.SWITCH, 30, parent=1, group=H("entity_name"), default=H("geralt"),
                    switches={H("ciri"): [20], H("geralt"): [13]})
    for i, wem in enumerate((301, 302, 303)):
        n[40 + i] = wb.Node(wb.SOUND, 40 + i, parent=50, source=wem)
    n[50] = wb.Node(wb.RANSEQ, 50, mode=1, playlist=[(40, 1), (41, 1), (42, 1)])
    n[51] = wb.Node(wb.RANSEQ, 51, random_mode=1, playlist=[(40, 1), (41, 1), (42, 1)])
    n[60] = wb.Node(wb.SOUND, 60, source=401, props={wb.LOOP: 0})
    g.actions = {
        200: wb.Action(0x0403, 30),
        201: wb.Action(0x0403, 30, props={wb.PROBABILITY: f32(25.0)}),
        202: wb.Action(0x1901, 0, group=H("entity_name"), state=H("ciri")),
        203: wb.Action(0x0403, 50),
        204: wb.Action(0x0403, 60, props={wb.DELAY: 250}),
        205: wb.Action(0x0103, 60),
        206: wb.Action(0x0403, 51),
    }
    g.events = {H("grunt_vo"): [200], H("grunt_vo_RND"): [201], H("set_entity_name_ciri"): [202], H("steps"): [203],
                H("loop_start"): [204], H("loop_stop"): [205], H("shuffle"): [206]}
    return g


def player(switches=None, device=True, seed=1):
    wavs = {w: f"{w}.wav" for w in (101, 102, 103, 201, 301, 302, 303, 401)}
    return sfx.Player(SimpleNamespace(graph=graph()), wavs, switches or {}, FakeDevice() if device else None,
                      rng=random.Random(seed), load=FakeSound)


class PlayerTests(unittest.TestCase):
    def test_switch_routes_and_random_avoids_repeats(self):
        p = player({H("entity_name"): H("ciri")})
        picks = [p.play("grunt_vo")[0] for _ in range(30)]
        self.assertEqual(set(picks), {101, 102, 103})
        self.assertTrue(all(len(set(picks[i:i + 3])) == 3 for i in range(len(picks) - 2)))
        self.assertEqual(player().play("grunt_vo"), [201])

    def test_spawn_event_sets_the_switch(self):
        p = player()
        self.assertEqual(p.play("set_entity_name_ciri"), [])
        self.assertIn(p.play("grunt_vo")[0], (101, 102, 103))

    def test_sequence_and_shuffle(self):
        p = player()
        self.assertEqual([p.play("steps")[0] for _ in range(5)], [301, 302, 303, 301, 302])
        rounds = [p.play("shuffle")[0] for _ in range(9)]
        self.assertTrue(all(len(set(rounds[i:i + 3])) == 3 for i in range(len(rounds) - 2)))

    def test_probability_gates_rnd_events(self):
        p = player(seed=7)
        hits = sum(bool(p.play("grunt_vo_RND")) for _ in range(2000))
        self.assertAlmostEqual(hits / 2000, 0.25, delta=0.05)
        self.assertEqual(sum(bool(e[2]) for e in p.played), hits)

    def test_volume_and_pitch_sum_up_the_hierarchy(self):
        p = player()
        p.gains["voice"] = 0.5
        p.play("grunt_vo")
        handle = p.device.handles[-1]
        self.assertAlmostEqual(handle.volume, 10 ** (-2.0 / 20) * 0.5)
        self.assertAlmostEqual(handle.pitch, 2.0)

    def test_voice_is_mono_and_stop_ends_loops(self):
        p = player()
        p.play("grunt_vo")
        p.play("grunt_vo")
        first, second = p.device.handles
        self.assertEqual((first.status, second.status), (False, True))
        p.play("loop_start")
        looping = p.device.handles[-1]
        self.assertEqual(looping.sound.ops, (("loop", -1), ("delay", 0.25)))
        p.play("loop_stop")
        self.assertFalse(looping.status)
        p.stop()
        self.assertFalse(second.status)

    def test_without_a_device_it_only_logs(self):
        p = player(device=False)
        self.assertEqual(p.play("grunt_vo", footstep=False), [201])
        self.assertEqual(p.played, [("grunt_vo", "voice", [201])])

    def test_categories(self):
        self.assertEqual(sfx.category("w_footstep_walk", True), "footsteps")
        self.assertEqual(sfx.category("w_clothes_step_light", False), "foley")
        self.assertEqual(sfx.category("grunt_vo_attack_short", False), "voice")
        self.assertEqual(sfx.category("cmb_weapon_swoosh_light_fast", False), "combat")


if __name__ == "__main__":
    unittest.main()
