"""Read Witcher 3 Wwise banks (BKHD v150) into an event/media graph.

Format layouts follow wwiser; malformed nodes are recorded in Bank.errors.
"""
import struct
from dataclasses import dataclass, field

SOUND, ACTION, EVENT, RANSEQ, SWITCH, ACTOR_MIXER, BUS, LAYER = 2, 3, 4, 5, 6, 7, 8, 9
PLAY, STOP, SET_STATE, SET_SWITCH = 0x04, 0x01, 0x12, 0x19
# Raw u32 props: delay/transition in ms, loop as count, others as floats.
VOLUME, PITCH, INITIAL_DELAY, DELAY, TRANSITION, PROBABILITY, LOOP = 0x00, 0x01, 0x22, 0x39, 0x3A, 0x3B, 0x54


def fnv1_32(name: str) -> int:
    h = 2166136261
    for b in name.lower().encode("utf-8"):
        h = ((h * 16777619) & 0xFFFFFFFF) ^ b
    return h


MASTER_AUDIO_BUS = fnv1_32("Master Audio Bus")


def prop_float(raw: int) -> float:
    return struct.unpack("<f", struct.pack("<I", raw))[0]


@dataclass
class Node:
    kind: int
    id: int
    parent: int = 0
    bus: int = 0                                    # 0 inherits parent
    props: dict = field(default_factory=dict)       # id -> raw u32
    ranged: dict = field(default_factory=dict)      # id -> (min, max)
    children: list = field(default_factory=list)
    source: int = 0                                 # wem id
    stream: int = 0                                 # 0 bank, 1 stream, 2 prefetch
    mode: int = 0                                   # 0 random, 1 sequence
    random_mode: int = 0                            # 0 normal, 1 shuffle
    avoid_repeat: int = 0
    playlist: list = field(default_factory=list)    # [(child id, weight)]
    group: int = 0                                  # switch/state group id
    default: int = 0
    switches: dict = field(default_factory=dict)    # state id -> [child ids]


@dataclass
class Action:
    type: int
    target: int
    props: dict = field(default_factory=dict)
    group: int = 0
    state: int = 0


@dataclass
class Bank:
    version: int = 0
    id: int = 0
    events: dict = field(default_factory=dict)      # event id -> [action ids]
    actions: dict = field(default_factory=dict)
    nodes: dict = field(default_factory=dict)
    media: dict = field(default_factory=dict)       # wem id -> (offset, size)
    buses: dict = field(default_factory=dict)       # bus id -> parent bus id
    errors: list = field(default_factory=list)      # (type, id, message)


class _Reader:
    __slots__ = ("data", "pos")

    def __init__(self, data: bytes, pos: int = 0):
        self.data, self.pos = data, pos

    def u8(self):
        self.pos += 1
        return self.data[self.pos - 1]

    def u16(self):
        self.pos += 2
        return struct.unpack_from("<H", self.data, self.pos - 2)[0]

    def u32(self):
        self.pos += 4
        return struct.unpack_from("<I", self.data, self.pos - 4)[0]

    def var(self):
        value = 0
        while True:
            byte = self.u8()
            value = (value << 7) | (byte & 0x7F)
            if not byte & 0x80:
                return value

    def ids(self, count):
        self.pos += 4 * count
        return list(struct.unpack_from(f"<{count}I", self.data, self.pos - 4 * count))

    def skip(self, n):
        self.pos += n


def _props(r):
    keys = list(r.data[r.pos + 1:r.pos + 1 + r.data[r.pos]])
    r.skip(1 + len(keys))
    props = {k: r.u32() for k in keys}
    keys = list(r.data[r.pos + 1:r.pos + 1 + r.data[r.pos]])
    r.skip(1 + len(keys))
    return props, {k: (r.u32(), r.u32()) for k in keys}


def _positioning(r):
    bits = r.u8()
    if bits & 1 and bits >> 1 & 1:
        r.u8()
        if bits >> 5 & 3:
            r.skip(5)
            r.skip(16 * r.u32())
            r.skip(20 * r.u32())


def _node_base(r, node):
    r.u8()
    count = r.u8()
    if count:
        r.skip(1 + 6 * count)
    r.u8()
    r.skip(6 * r.u8())
    node.bus = r.u32()
    node.parent = r.u32()
    r.skip(1)
    node.props, node.ranged = _props(r)
    _positioning(r)
    if r.u8() >> 3 & 1:
        r.skip(16)
    r.skip(4 + 6)
    for _ in range(r.var()):
        r.var()
        r.skip(2)
    for _ in range(r.var()):
        r.skip(5)
        for _ in range(r.var()):
            r.skip(4)
            r.skip(6 * r.u16())
    _rtpc(r)


def _rtpc(r):
    for _ in range(r.u16()):
        r.skip(6)
        r.var()
        r.skip(5)
        r.skip(12 * r.u16())


def _sound(r, node):
    plugin = r.u32()
    node.stream = r.u8()
    node.source = r.u32()
    r.skip(5)
    if plugin & 0x0F == 2:
        r.skip(r.u32())
    _node_base(r, node)


def _ranseq(r, node):
    _node_base(r, node)
    r.skip(18)
    node.avoid_repeat = r.u16()
    r.skip(1)
    node.random_mode, node.mode = r.u8(), r.u8()
    r.skip(1)
    node.children = r.ids(r.u32())
    for _ in range(r.u16()):
        node.playlist.append((r.u32(), r.u32()))


def _switch(r, node):
    _node_base(r, node)
    r.skip(1)
    node.group, node.default = r.u32(), r.u32()
    r.skip(1)
    node.children = r.ids(r.u32())
    for _ in range(r.u32()):
        state = r.u32()
        node.switches[state] = r.ids(r.u32())
    r.skip(14 * r.u32())


def _layer(r, node):
    _node_base(r, node)
    node.children = r.ids(r.u32())
    for _ in range(r.u32()):
        r.skip(4)
        _rtpc(r)
        r.skip(5)
        for _ in range(r.u32()):
            r.skip(4)
            r.skip(12 * r.u32())
    r.skip(1)


def _actor_mixer(r, node):
    _node_base(r, node)
    node.children = r.ids(r.u32())


_NODES = {SOUND: _sound, RANSEQ: _ranseq, SWITCH: _switch, LAYER: _layer, ACTOR_MIXER: _actor_mixer}


def _hirc(data, bank):
    r = _Reader(data)
    for _ in range(r.u32()):
        kind, size = r.u8(), r.u32()
        end = r.pos + size
        object_id = r.u32()
        try:
            if kind == EVENT:
                bank.events[object_id] = r.ids(r.var())
            elif kind == ACTION:
                action = Action(r.u16(), r.u32())
                r.skip(1)
                action.props = _props(r)[0]
                if action.type >> 8 in (SET_SWITCH, SET_STATE):
                    action.group, action.state = r.u32(), r.u32()
                bank.actions[object_id] = action
            elif kind == BUS:
                bank.buses[object_id] = r.u32()
            elif kind in _NODES:
                node = Node(kind, object_id)
                _NODES[kind](r, node)
                if r.pos != end:
                    raise ValueError(f"decoded {r.pos - end:+d} bytes past the object")
                bank.nodes[object_id] = node
        except (ValueError, struct.error, IndexError) as exc:
            bank.errors.append((kind, object_id, str(exc)))
        r.pos = end


def parse_bank(read, size: int) -> Bank:
    """Parse one bank without loading its DATA chunk."""
    bank = Bank()
    pos = data_offset = 0
    didx = b""
    while pos + 8 <= size:
        tag, length = struct.unpack("<4sI", read(pos, 8))
        if tag == b"BKHD":
            bank.version, bank.id = struct.unpack("<II", read(pos + 8, 8))
        elif tag == b"DIDX":
            didx = read(pos + 8, length)
        elif tag == b"DATA":
            data_offset = pos + 8
        elif tag == b"HIRC":
            _hirc(read(pos + 8, length), bank)
        pos += 8 + length
    if not bank.version:
        raise ValueError("not a Wwise bank (no BKHD chunk)")
    for i in range(0, len(didx), 12):
        wem, offset, length = struct.unpack_from("<III", didx, i)
        bank.media[wem] = (data_offset + offset, length)
    return bank


class SoundGraph:
    """Merge event graphs and media locations across banks."""

    def __init__(self):
        self.events, self.actions, self.nodes, self.media, self.buses = {}, {}, {}, {}, {}

    def add(self, name: str, bank: Bank):
        for mine, theirs in ((self.events, bank.events), (self.actions, bank.actions), (self.nodes, bank.nodes),
                             (self.buses, bank.buses)):
            for key, value in theirs.items():
                mine.setdefault(key, value)
        for wem, location in bank.media.items():
            self.media.setdefault(wem, (name, *location))

    def switch_children(self, node: Node, switches: dict) -> list:
        # Unassigned surfaces fall through to the default ground switch.
        return node.switches.get(switches.get(node.group, node.default)) or node.switches.get(node.default, [])

    def audible(self, node: Node) -> bool:
        # Exclude haptic rumble routed to another root bus.
        while node is not None and not node.bus:
            node = self.nodes.get(node.parent)
        bus = node.bus if node is not None else 0
        if bus not in self.buses:
            return True
        while self.buses.get(bus):
            bus = self.buses[bus]
        return bus == MASTER_AUDIO_BUS

    def sources(self, event_id: int, switches: dict = None) -> list:
        """List possible wems for an event and switch state."""
        out = []
        for action_id in self.events.get(event_id, ()):
            action = self.actions.get(action_id)
            if action is not None and action.type >> 8 == PLAY:
                self._collect(action.target, switches or {}, out, set())
        return out

    def _collect(self, node_id, switches, out, seen):
        node = self.nodes.get(node_id)
        if node is None or node_id in seen or not self.audible(node):
            return
        seen.add(node_id)
        if node.kind == SOUND:
            if node.source not in out:
                out.append(node.source)
            return
        if node.kind == SWITCH:
            children = self.switch_children(node, switches)
        elif node.kind == RANSEQ:
            children = [child for child, _ in node.playlist]
        else:
            children = node.children
        for child in children:
            self._collect(child, switches, out, seen)
