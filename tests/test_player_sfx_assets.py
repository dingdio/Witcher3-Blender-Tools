import gzip
import json
import os
import sys
import tempfile
import types
import unittest
import wave
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

if "witcher3_tools" not in sys.modules:
    _pkg = types.ModuleType("witcher3_tools")
    _pkg.__path__ = [str(REPO_ROOT / "witcher3_tools")]
    _pkg.__package__ = "witcher3_tools"
    _pkg.get_addon_name = lambda: "witcher3_tools"
    sys.modules["witcher3_tools"] = _pkg

from witcher3_tools.controller import sfx_assets as sa
import test_wwise_bank as tb

H = sa.H
VGMSTREAM = REPO_ROOT / "witcher3_tools" / "CR2W" / "third_party_libs" / "vgmstream-win64" / "vgmstream-cli.exe"
UNCOOK_ROOT = os.environ.get("W3_UNCOOK", "")
CIRI_TEMPLATE = (os.path.join(UNCOOK_ROOT, "gameplay", "templates", "characters", "player", "ciri_player.w2ent")
                 if UNCOOK_ROOT else "")
if not os.path.isfile(CIRI_TEMPLATE):
    CIRI_TEMPLATE = ""


class _Item:
    def __init__(self, raw_name, parent, offset, size):
        self.RawName, self.Name, self.ParentFile, self.PageOffset, self.Size = raw_name, raw_name, parent, offset, size


class SyntheticLibraryTests(unittest.TestCase):
    def setUp(self):
        bank = tb._bank(
            [
                tb._sound(10, 1001, 30), tb._sound(11, 1002, 30), tb._sound(12, 1003, 30),
                tb._switch(30, "ground_materials", "dirt_hard", {"dirt_hard": [10], "stone_solid": [11], "mud": [12]}),
                tb._event("step", [40]), tb._action(40, 0x0403, 30),
                tb._event("set_entity_name_ciri", [41]), tb._action(41, 0x1901, 0, "entity_name", "ciri"),
            ],
            {1001: b"DIRT", 1002: b"BANK", 1003: b"MUD!"},
        )
        fd, self.cache = tempfile.mkstemp(suffix=".cache")
        loose = b"LOOSE-1002"
        with os.fdopen(fd, "wb") as f:
            f.write(b"pad!" + bank + loose)
        self.library = sa.Library([
            _Item("test.bnk", self.cache, 4, len(bank)),
            _Item("1002.wem", self.cache, 4 + len(bank), len(loose)),
        ])

    def tearDown(self):
        os.unlink(self.cache)

    def _read(self, wem):
        path, offset, size = self.library.locate(wem)
        with open(path, "rb") as f:
            f.seek(offset)
            return f.read(size)

    def test_locate_prefers_loose_wems(self):
        self.assertEqual(self._read(1001), b"DIRT")
        self.assertEqual(self._read(1002), b"LOOSE-1002")
        self.assertIsNone(self.library.locate(4242))

    def test_spawn_events_and_armour_set_switches(self):
        switches = self.library.switches(["set_entity_name_ciri", "not_an_event"], armour="leather")
        self.assertEqual(switches, {H("entity_name"): H("ciri"), sa.ARMOUR: H("leather")})
        self.assertNotIn(sa.ARMOUR, self.library.switches(armour="default"))

    def test_pool_covers_every_surface(self):
        self.assertEqual(sorted(self.library.pool(["step"], {})), [1001, 1002, 1003])
        self.assertEqual(self.library.graph.sources(H("step"), {sa.GROUND: H("stone_solid")}), [1002])


@unittest.skipUnless(tb.SOUND_CACHE and VGMSTREAM.is_file(), "Witcher 3 soundspc.cache or vgmstream not available")
class GameLibraryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from witcher3_tools.CR2W.witcher_cache.SoundCache.SoundCache import SoundCache
        cls.library = sa.Library(SoundCache(tb.SOUND_CACHE).Files)

    def test_library_is_complete(self):
        self.assertEqual(self.library.errors, [])
        self.assertIn(H("stone_solid"), self.library.surfaces)

    def test_swoosh_skips_haptic_rumble(self):
        info = json.load(gzip.open(REPO_ROOT / "witcher3_tools/CR2W/data/soundbanksinfo.json.gz"))
        names = {int(k): v["short_name"] for k, v in info["streamed_files_by_id"].items()}
        pool = self.library.graph.sources(H("cmb_weapon_swoosh_light_fast"), {})
        self.assertEqual(len(pool), 6)
        self.assertTrue(all(names.get(w, "").startswith("combat\\weapon_whoosh\\sword\\") for w in pool), pool)

    def test_ciri_grunts_decode_once(self):
        switches = self.library.switches(["set_entity_gender_female", "set_entity_name_ciri"])
        pool = self.library.pool(["grunt_vo_attack_short"], switches)
        self.assertEqual(len(pool), 8)
        with tempfile.TemporaryDirectory() as out:
            wavs = sa.decode(self.library, pool, out, str(VGMSTREAM))
            self.assertEqual(sorted(wavs), sorted(pool))
            with wave.open(wavs[pool[0]]) as w:
                self.assertEqual((w.getnchannels(), w.getframerate(), w.getsampwidth()), (1, 48000, 2))
            self.assertEqual(sorted(os.listdir(out)), sorted(f"{wem}.wav" for wem in pool))
            stamp = os.path.getmtime(wavs[pool[0]])
            self.assertEqual(sa.decode(self.library, pool, out, str(VGMSTREAM)), wavs)
            self.assertEqual(os.path.getmtime(wavs[pool[0]]), stamp)

    @unittest.skipUnless(CIRI_TEMPLATE, "cooked ciri_player.w2ent not available")
    def test_template_spawn_events(self):
        events = sa.template_emitter_events(CIRI_TEMPLATE)
        self.assertEqual(set(events), {"set_entity_gender_female", "set_entity_name_ciri"})
        self.assertEqual(self.library.switches(events)[H("entity_name")], H("ciri"))


if __name__ == "__main__":
    unittest.main()
