"""Play clip sound events through decoded Wwise bank media."""
import logging
import random

from ..CR2W.witcher_cache.SoundCache import wwise_bank as wb
from .sfx_assets import GROUND, H

log = logging.getLogger(__name__)
CATEGORIES = ("footsteps", "foley", "combat", "voice")
LIMITS = {"footsteps": 4, "foley": 2, "combat": 2, "voice": 1}


def category(event: str, footstep: bool) -> str:
    if footstep or "footstep" in event:
        return "footsteps"
    if "clothes" in event:
        return "foley"
    if event.startswith(("grunt_", "vo_")):
        return "voice"
    return "combat"


def open_device():
    try:
        import aud
        return aud.Device()
    except Exception as exc:          # headless or no device
        log.info("player: no audio device (%s), sound is silent", exc)
        return None


def _load(path):
    import aud
    return aud.Sound(path).cache()


class Player:
    def __init__(self, library, wavs: dict, switches: dict, device=None, rng=None, load=_load):
        self.graph = library.graph
        self.switches = dict(switches)
        self.device = device
        self.rng = rng or random.Random()
        self.gains = dict.fromkeys(CATEGORIES, 1.0)
        self.handles = {c: [] for c in CATEGORIES}      # [(handle, target)]
        self.recent, self.next_in_sequence = {}, {}
        self.played = []                                 # [(event, category, [wem])]
        self.sounds = {wem: load(path) for wem, path in wavs.items()} if device is not None else {}
        self.wavs = wavs

    def play(self, event: str, footstep: bool = False, surface=None) -> list:
        """Run the event's actions; returns the wems it started."""
        kind = category(event, footstep)
        switches = self.switches if surface is None else {**self.switches, GROUND: surface}
        voices = []
        for action_id in self.graph.events.get(H(event), ()):
            action = self.graph.actions.get(action_id)
            op = action.type >> 8 if action is not None else None
            if op in (wb.SET_SWITCH, wb.SET_STATE):
                self.switches[action.group] = action.state
            elif op == wb.STOP:
                self._stop(action.target)
            elif op == wb.PLAY:
                chance = action.props.get(wb.PROBABILITY)
                if chance is not None and self.rng.random() * 100.0 >= wb.prop_float(chance):
                    continue
                self._pick(action.target, switches, voices, action.props.get(wb.DELAY, 0) / 1000.0, action.target)
        self.played.append((event, kind, [v[0] for v in voices]))
        for voice in voices:
            self._start(kind, *voice)
        return [v[0] for v in voices]

    def _pick(self, node_id, switches, out, delay, root):
        node = self.graph.nodes.get(node_id)
        if node is None or not self.graph.audible(node):
            return
        if node.kind == wb.SOUND:
            out.append((node.source, *self._mix(node, delay), root, node.props.get(wb.LOOP)))
            return
        if node.kind == wb.SWITCH:
            children = self.graph.switch_children(node, switches)
        elif node.kind == wb.RANSEQ:
            children = [self._choose(node)] if node.playlist else []
        else:
            children = node.children
        for child in children:
            self._pick(child, switches, out, delay, root)

    def _choose(self, node):
        items = node.playlist
        if node.mode == 1:
            i = self.next_in_sequence.get(node.id, 0)
            self.next_in_sequence[node.id] = (i + 1) % len(items)
            return items[i][0]
        avoid = len(items) - 1 if node.random_mode == 1 else node.avoid_repeat     # shuffle avoids a full round
        recent = self.recent.setdefault(node.id, [])
        pool = [(c, w) for c, w in items if c not in recent] or items
        total = sum(max(w, 0) for _, w in pool)
        r = self.rng.random() * total
        child = pool[-1][0]
        for c, w in pool:
            r -= max(w, 0)
            if r < 0.0 or total <= 0:
                child = c
                break
        recent.append(child)
        del recent[:max(len(recent) - avoid, 0)]
        return child

    def _value(self, node, prop):
        value = wb.prop_float(node.props[prop]) if prop in node.props else 0.0
        if prop in node.ranged:
            low, high = (wb.prop_float(v) for v in node.ranged[prop])
            value += self.rng.uniform(low, high)
        return value

    def _mix(self, node, delay):
        """(dB, cents, seconds) summed over the node and its parents, random ranges rolled per play."""
        db = cents = 0.0
        while node is not None:
            db += self._value(node, wb.VOLUME)
            cents += self._value(node, wb.PITCH)
            delay += self._value(node, wb.INITIAL_DELAY)
            node = self.graph.nodes.get(node.parent) if node.parent else None
        return db, cents, delay

    def _start(self, kind, wem, db, cents, delay, root, loop):
        sound = self.sounds.get(wem)
        if sound is None:
            return
        if loop is not None:
            sound = sound.loop(-1 if loop == 0 else loop - 1)      # Wwise 0 loops forever
        if delay > 0.0:
            sound = sound.delay(delay)
        live = [h for h in self.handles[kind] if h[0].status]
        while len(live) >= LIMITS[kind]:
            live.pop(0)[0].stop()
        handle = self.device.play(sound)
        handle.volume = 10.0 ** (db / 20.0) * self.gains[kind]
        handle.pitch = 2.0 ** (cents / 1200.0)
        live.append((handle, root))
        self.handles[kind] = live

    def _stop(self, target):
        for kind, live in self.handles.items():
            for handle, root in live:
                if root == target:
                    handle.stop()
            self.handles[kind] = [h for h in live if h[1] != target]

    def stop(self):
        for live in self.handles.values():
            for handle, _ in live:
                handle.stop()
            live.clear()
