import importlib.util
import os
import struct
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "wwise_bank", REPO_ROOT / "witcher3_tools" / "CR2W" / "witcher_cache" / "SoundCache" / "wwise_bank.py"
)
wb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wb)
H = wb.fnv1_32

GAME_ROOT = os.environ.get("W3_GAME", "")
SOUND_CACHE = os.path.join(GAME_ROOT, "content", "content0", "soundspc.cache") if GAME_ROOT else ""
if not os.path.isfile(SOUND_CACHE):
    SOUND_CACHE = ""


def _node_base(parent=0, props=None):
    props = props or {}
    return (
        bytes(4) + struct.pack("<II", 0, parent) + b"\0"
        + bytes([len(props)]) + bytes(props) + b"".join(struct.pack("<I", v) for v in props.values())
        + b"\0" + b"\0" + b"\0" + bytes(4) + bytes(6) + b"\0\0" + bytes(2)
    )


def _obj(kind, object_id, body):
    payload = struct.pack("<I", object_id) + body
    return bytes([kind]) + struct.pack("<I", len(payload)) + payload


def _chunk(tag, body):
    return tag + struct.pack("<I", len(body)) + body


def _bank(objects, media):
    data = b"".join(media.values())
    didx, offset = b"", 0
    for wem, blob in media.items():
        didx += struct.pack("<III", wem, offset, len(blob))
        offset += len(blob)
    return (
        _chunk(b"BKHD", struct.pack("<II", 150, H("test_bank")) + bytes(8))
        + _chunk(b"DIDX", didx)
        + _chunk(b"DATA", data)
        + _chunk(b"HIRC", struct.pack("<I", len(objects)) + b"".join(objects))
    )


def _sound(object_id, wem, parent):
    return _obj(wb.SOUND, object_id, struct.pack("<IBII", 0x00140001, 0, wem, 4) + b"\0" + _node_base(parent))


def _ranseq(object_id, children, parent, avoid=2):
    tail = struct.pack("<HHHfffH", 1, 0, 0, 0, 0, 0, avoid) + bytes([0, 0, 0, 0])
    tail += struct.pack("<I", len(children)) + b"".join(struct.pack("<I", c) for c in children)
    tail += struct.pack("<H", len(children)) + b"".join(struct.pack("<II", c, 50000) for c in children)
    return _obj(wb.RANSEQ, object_id, _node_base(parent, {wb.VOLUME: struct.unpack("<I", struct.pack("<f", -3.0))[0]}) + tail)


def _switch(object_id, group, default, packages):
    children = [c for ids in packages.values() for c in ids]
    tail = b"\0" + struct.pack("<II", H(group), H(default)) + b"\0"
    tail += struct.pack("<I", len(children)) + b"".join(struct.pack("<I", c) for c in children)
    tail += struct.pack("<I", len(packages))
    for state, ids in packages.items():
        tail += struct.pack("<II", H(state), len(ids)) + b"".join(struct.pack("<I", c) for c in ids)
    tail += struct.pack("<I", 0)
    return _obj(wb.SWITCH, object_id, _node_base() + tail)


def _event(name, actions):
    return _obj(wb.EVENT, H(name), bytes([len(actions)]) + b"".join(struct.pack("<I", a) for a in actions))


def _action(object_id, kind, target, group=None, state=None):
    body = struct.pack("<HI", kind, target) + b"\0\0\0"
    if group is not None:
        body += struct.pack("<II", H(group), H(state))
    else:
        body += b"\0" + struct.pack("<II", 0, 0)
    return _obj(wb.ACTION, object_id, body)


def _parse(blob):
    return wb.parse_bank(lambda offset, length: blob[offset:offset + length], len(blob))


class SyntheticBankTests(unittest.TestCase):
    def setUp(self):
        self.blob = _bank(
            [
                _sound(10, 1001, 20), _sound(11, 1002, 20), _sound(12, 2001, 21),
                _ranseq(20, [10, 11], 30), _ranseq(21, [12], 30),
                _switch(30, "entity_name", "geralt", {"ciri": [20], "geralt": [21]}),
                _event("grunt", [40]), _action(40, 0x0403, 30),
                _event("set_entity_name_ciri", [41]), _action(41, 0x1901, 0, "entity_name", "ciri"),
            ],
            {1001: b"AAAA", 1002: b"BBBB", 2001: b"CCCC"},
        )
        self.bank = _parse(self.blob)
        self.graph = wb.SoundGraph()
        self.graph.add("test.bnk", self.bank)

    def test_every_object_decodes(self):
        self.assertEqual(self.bank.errors, [])
        self.assertEqual(self.bank.version, 150)
        self.assertEqual(self.bank.nodes[20].avoid_repeat, 2)
        self.assertAlmostEqual(wb.prop_float(self.bank.nodes[20].props[wb.VOLUME]), -3.0)

    def test_switch_routes_and_defaults(self):
        self.assertEqual(self.graph.sources(H("grunt"), {H("entity_name"): H("ciri")}), [1001, 1002])
        self.assertEqual(self.graph.sources(H("grunt"), {H("entity_name"): H("geralt")}), [2001])
        self.assertEqual(self.graph.sources(H("grunt")), [2001])
        self.assertEqual(self.graph.sources(H("grunt"), {H("entity_name"): H("hjalmar")}), [2001])

    def test_set_switch_action(self):
        action = self.bank.actions[41]
        self.assertEqual((action.type >> 8, action.group, action.state), (wb.SET_SWITCH, H("entity_name"), H("ciri")))

    def test_media_offsets_are_absolute(self):
        name, offset, length = self.graph.media[1002]
        self.assertEqual((name, self.blob[offset:offset + length]), ("test.bnk", b"BBBB"))

    def test_size_mismatch_is_recorded_not_fatal(self):
        broken = _obj(wb.SOUND, 99, struct.pack("<IBII", 0x00140001, 0, 5, 4) + b"\0" + _node_base() + b"\0")
        bank = _parse(_bank([broken, _sound(10, 1001, 0)], {1001: b"AAAA"}))
        self.assertEqual([(k, i) for k, i, _ in bank.errors], [(wb.SOUND, 99)])
        self.assertIn(10, bank.nodes)


def _game_graph():
    graph = wb.SoundGraph()
    errors = []
    with open(SOUND_CACHE, "rb") as f:
        f.read(16)
        info, count, _, name_offset, name_size = struct.unpack("<QIIQI", f.read(28))
        f.seek(info)
        entries = [struct.unpack("<IIQQ", f.read(24)) for _ in range(count)]
        f.seek(name_offset)
        names = f.read(name_size)
        for name_at, _, offset, size in entries:
            name = names[name_at:names.index(b"\0", name_at)].decode()
            if name.endswith(".bnk"):
                def read(at, length, base=offset):
                    f.seek(base + at)
                    return f.read(length)
                bank = wb.parse_bank(read, size)
                errors += [(name, *e) for e in bank.errors]
                graph.add(name, bank)
    return graph, errors


@unittest.skipUnless(SOUND_CACHE, "Witcher 3 soundspc.cache not available")
class GameBankTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.graph, cls.errors = _game_graph()

    def test_every_node_decodes(self):
        self.assertEqual(self.errors, [])
        self.assertGreater(len(self.graph.nodes), 40000)

    def test_grunts_follow_entity_name(self):
        ciri = self.graph.sources(H("grunt_vo_attack_short"), {H("entity_name"): H("ciri")})
        # Cross-checked against wwiser TXTP.
        self.assertEqual(
            set(ciri), {343087735, 19304980, 331119309, 120397104, 38293499, 33987614, 8837511, 776719441}
        )
        geralt = self.graph.sources(H("grunt_vo_attack_short"), {H("entity_name"): H("geralt")})
        self.assertTrue(geralt)
        self.assertFalse(set(ciri) & set(geralt))

    def test_footsteps_follow_ground_material(self):
        stone = self.graph.sources(H("w_footstep_walk"), {H("ground_materials"): H("stone_solid")})
        mud = self.graph.sources(H("w_footstep_walk"), {H("ground_materials"): H("mud")})
        self.assertIn(790264843, stone)
        self.assertTrue(mud)
        self.assertFalse(set(stone) & set(mud))

    def test_swoosh_resolves_to_embedded_media(self):
        wems = self.graph.sources(H("cmb_weapon_swoosh_light_fast"))
        self.assertIn(292509143, wems)
        self.assertTrue(all(wem in self.graph.media for wem in wems))


if __name__ == "__main__":
    unittest.main()
