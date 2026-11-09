"""Build character locomotion profiles from their animation sets.

Later sets override earlier ones; monster clips are selected by name and motion.
"""
import logging
import math
import os
import re
import struct
import time
from dataclasses import dataclass, field

import numpy as np

from . import clips, core

log = logging.getLogger(__name__)
_DIRECTIONS = ("forward", "forwardleft", "left", "backleft", "bwd_lft", "back_ccw",
               "forwardright", "right", "backright", "bwd_rgt", "back_cw")
_FEET = {"LeftUp": "leftup", "LeftForward": "leftforward", "RightUp": "rightup", "RightForward": "rightforward"}
HUMANOID = {
    "idle": ("locomotion_idle",),
    "starts": {g: [f"locomotion_{g}_idle_to_{d}" for d in _DIRECTIONS] for g in core.GAITS},
    "loops": {"walk": ("locomotion_walk_cycle_fast_forward", None, None),
              "run": ("locomotion_run_cycle_fast_forward", "locomotion_run_cycle_fast_left", "locomotion_run_cycle_fast_right"),
              "sprint": ("locomotion_sprint_cycle_forward", "locomotion_sprint_cycle_fast_left", "locomotion_sprint_cycle_fast_right")},
    "stops": {g: {foot: f"locomotion_{g}_{n}_to_idle" for foot, n in _FEET.items()} for g in core.GAITS},
}
# Avoid extracting unrelated animation sets.
_SKIP_SETS = re.compile(r"dialog|quest|finisher|cutscene|mimic|community|inventory|gui_|drinking|meditation|focus|boat|"
                        r"horse|swimming|carry|scabbard|fistfight|signs|petard|tornado|gabriel|longsword|dismember|"
                        r"interaction|\\combat\\|skating|ghost|lookat|additive|constraints", re.I)
_MONSTER_SKIP = re.compile(r"attack|hit|death|dead|die|taunt|combat|fly|swim|dive|jump|land|dialog|dialoque|cine|special|"
                           r"summon|stunned|burning|cage|spawn|eat|lookat|look_|knock|crawl|fall|getup|get_up|strafe|block|"
                           r"charge|dodge|counter|parry|shake|sleep|lower|scared|flee|aggressive_to|lean|_add", re.I)
_IDLE = re.compile(r"(^|_)idle(_?\d+)?$")
_LOOP = re.compile(r"(^|_)(walk|run|move_walk|move_run)(_f|_forward)?$")
_SIDE = re.compile(r"(^|_)(walk|run)(_f)?_(l|r|left|right)(_tighter)?$|(^|_)(walk|run)_turn_(l|r|left|right)$")
_START = re.compile(r"(walk|run)_start|start_(walk|run)|idle_to_(walk|run)|ready_to_walk")
_STOP = re.compile(r"(walk|run)(_[a-z]+)?_to_idle$|stop_(walk|run)|(walk|run)_stop")
_NAMES = {}


def animset_names(path: str) -> set:
    """Read potential clip names from the CR2W string table."""
    key = (path, os.path.getmtime(path))
    if key not in _NAMES:
        with open(path, "rb") as f:
            head = f.read(52)
            if head[:4] != b"CR2W":
                return set()
            offset, size = struct.unpack_from("<II", head, 40)      # string table
            f.seek(offset)
            _NAMES[key] = {s.decode("utf-8", "ignore") for s in f.read(size).split(b"\0") if s}
    return _NAMES[key]


def entity_animsets(arm, skip=None) -> list:
    """Resolve animation sets in precedence order, filtering before extraction."""
    from ..repo_paths import repo_file_for_source, source_game_for_animset_item
    rs = arm.data.witcherui_RigSettings
    out = []
    for item in rs.animset_list:
        depot = (item.path or "").strip()
        if not depot or depot.endswith(":") or not depot.lower().endswith(".w2anims") or (skip is not None and skip.search(depot)):
            continue
        path = repo_file_for_source(depot, source_game_for_animset_item(item, rs))
        if path and os.path.isfile(path):
            out.append(str(path))
    return out


class Library:
    """Clip names of a character's sets, loaded on demand from the latest set holding them."""

    def __init__(self, arm, sets: list):
        from ..repo_paths import repo_file_for_source, source_game_for_rig_settings
        rs = arm.data.witcherui_RigSettings
        skeleton = (rs.main_entity_skeleton or "").strip()
        game = source_game_for_rig_settings(rs)
        self.arm = arm
        self.rig = (skeleton if os.path.isabs(skeleton) else repo_file_for_source(skeleton, game)) if skeleton else None
        self.sets = [(path, animset_names(path)) for path in sets]
        self.names = set().union(*(names for _, names in self.sets)) if self.sets else set()
        self.loaded = {}

    def clip(self, name, cyclic=False):
        if not name or name not in self.names:
            return None
        if name not in self.loaded:
            self.loaded[name] = None
            for path, names in reversed(self.sets):
                if name in names:
                    try:
                        self.loaded[name] = clips.load_clip(self.arm, path, name, self.rig, cyclic)
                        break
                    except (KeyError, IndexError):        # string hit was not a clip
                        continue
        return self.loaded[name]


def _humanoid(lib: Library, spec=HUMANOID):
    idle = next((c for c in (lib.clip(n, True) for n in spec["idle"]) if c is not None), None)
    loops = {g: tuple(lib.clip(n, True) for n in names) for g, names in spec["loops"].items()}
    starts = {g: [c for c in map(lib.clip, names) if c is not None] for g, names in spec["starts"].items()}
    stops = {g: {foot: lib.clip(n) for foot, n in names.items()} for g, names in spec["stops"].items()}
    return idle, loops, starts, stops


def _prefixed(spec, prefix: str, idle):
    return {"idle": idle,
            "starts": {g: [prefix + n for n in names] for g, names in spec["starts"].items()},
            "loops": {g: tuple(prefix + n if n else None for n in names) for g, names in spec["loops"].items()},
            "stops": {g: {f: prefix + n for f, n in feet.items()} for g, feet in spec["stops"].items()}}


# Combat locomotion uses prefixed clip names.
COMBAT = _prefixed(HUMANOID, "combat_", ("combat_locomotion_man_geralt_ex_idle", "combat_locomotion_geralt_idle_alert_far"))
DRAW = {"steelsword": "man_geralt_sword_unsheathe_steel", "silversword": "man_geralt_sword_unsheathe_silver"}
SHEATHE = {"steelsword": "man_geralt_sword_sheathe_steel", "silversword": "man_geralt_sword_sheathe_silver"}
STANCES = {"CombatStanceLeft": "lp", "CombatStanceRight": "rp"}    # next attack's start pose
_ATTACK = re.compile(r"_sword_attack_fast_(\d+)_(lp|rp)_40ms$")
# Plain dodges and rolls; flips, pirouettes, jumps, fists and turning variants are left to the behaviour graph.
_EVADES = {"dodge": re.compile(r"_sword_dodge_(forward|back|left|right)_\d+c?m(_(lp|rp))?(_\d+)?$"),
           "roll": re.compile(r"_sword_dodge_roll_(lp|rp)_[fb]_\d+$")}
DIRECTIONS = ("forward", "left", "back", "right")      # playerEvadeDirection order, 90 degrees apart
_SKIP_COMBAT_SETS = re.compile(_SKIP_SETS.pattern.replace(r"|\\combat\\", ""), re.I)


@dataclass
class Moves:
    combat: object = None          # Profile or None
    draw: dict = field(default_factory=dict)
    sheathe: dict = field(default_factory=dict)
    attacks: dict = field(default_factory=dict)     # stance -> [Clip] by combo order
    evades: dict = field(default_factory=dict)      # "dodge"/"roll" -> [Evade]


@dataclass
class Evade:
    clip: object
    direction: str                 # of travel, from the root motion
    travel: float                  # travel heading relative to facing, radians, positive left
    stance: str                    # "lp", "rp" or "" for either


def _evade(clip, stance: str):
    tx, ty = _travel(clip)
    if math.hypot(tx, ty) < 0.5:
        return None
    travel = math.atan2(-tx, ty)
    return Evade(clip, DIRECTIONS[round(travel / (math.pi / 2)) % 4], travel, stance)


def stance_after(clip) -> str:
    return next((STANCES[n] for _, n in reversed(clip.events) if n in STANCES), "")


def build_moves(arm) -> Moves:
    """Load sword actions and combat locomotion."""
    started = time.perf_counter()
    lib = Library(arm, entity_animsets(arm, _SKIP_COMBAT_SETS))
    moves = Moves()
    for table, out in ((DRAW, moves.draw), (SHEATHE, moves.sheathe)):
        for category, name in table.items():
            clip = lib.clip(name)
            if clip is not None:
                out[category] = clip
    names = next((sorted(n for n in names if _ATTACK.search(n)) for _, names in reversed(lib.sets)
                  if any(_ATTACK.search(n) for n in names)), [])
    for name in sorted(names, key=lambda n: int(_ATTACK.search(n).group(1))):
        clip = lib.clip(name)
        if clip is not None:
            moves.attacks.setdefault(_ATTACK.search(name).group(2), []).append(clip)
    for kind, pattern in _EVADES.items():
        for name in sorted(n for n in lib.names if pattern.search(n)):
            clip = lib.clip(name)
            stance = next((s for s in ("lp", "rp") if f"_{s}_" in name + "_"), "")
            evade = _evade(clip, stance) if clip is not None else None
            if evade is not None:
                moves.evades.setdefault(kind, []).append(evade)
    if COMBAT["loops"]["run"][0] in lib.names:
        idle, loops, starts, stops = _humanoid(lib, COMBAT)
        loops = {g: c for g, c in loops.items() if c and c[0] is not None and c[0].speed > 0.3}
        starts = {g: [c for c in cs if _heads_where_it_goes(c, math.radians(60))] for g, cs in starts.items()}
        starts = {g: cs for g, cs in starts.items() if any(abs(c.turn) < math.radians(20) for c in cs)}
        stops = {g: {k: c for k, c in feet.items() if c is not None} for g, feet in stops.items()}
        if loops:
            moves.combat = core.Profile("combat", idle or _held(next(iter(loops.values()))[0]), starts, loops,
                                        {g: c for g, c in stops.items() if c})
    log.info("player moves %s: draw %s, %d attacks, evades %s, combat locomotion %s in %.1fs", arm.name, sorted(moves.draw),
             sum(len(v) for v in moves.attacks.values()), {k: len(v) for k, v in moves.evades.items()},
             moves.combat is not None, time.perf_counter() - started)
    return moves


def _gait(name: str) -> str:
    return "run" if re.search(r"(^|_)run", name) else "walk"


def _travel(clip) -> np.ndarray:
    return clip.root_pos[-1, :2] - clip.root_pos[0, :2]


def _heads_where_it_goes(clip, tolerance: float) -> bool:
    """The clip travels within `tolerance` radians of the heading it ends on (not a backwards walk or a strafe)."""
    tx, ty = _travel(clip)
    return math.hypot(tx, ty) > 0.2 and abs(core.wrap(math.atan2(-tx, ty) - clip.turn)) < tolerance


def _monster(lib: Library):
    names = sorted((n for n in lib.names if not _MONSTER_SKIP.search(n)), key=len)
    idle = next((c for c in (lib.clip(n, True) for n in names if _IDLE.search(n)) if c is not None), None)
    loops = {}
    for name in names:
        if _LOOP.search(name) and _gait(name) not in loops:
            clip = lib.clip(name, True)
            if clip is not None and abs(clip.turn) < math.radians(20) and _heads_where_it_goes(clip, math.radians(30)):
                loops[_gait(name)] = [clip, None, None]
    for name in names:
        gait = _gait(name)
        if gait in loops and _SIDE.search(name):
            clip = lib.clip(name, True)
            if clip is None or abs(clip.turn) < math.radians(15):        # skip strafes
                continue
            slot = 1 if clip.turn > 0 else 2
            loops[gait][slot] = loops[gait][slot] or clip
    starts, stops = {}, {}
    for name in names:
        if _STOP.search(name):
            clip = lib.clip(name)
            if clip is not None:
                stops.setdefault(_gait(name), {}).setdefault("Stop", clip)
        elif _START.search(name):
            clip = lib.clip(name)
            if clip is not None:
                starts.setdefault(_gait(name), []).append(clip)
    return idle, {g: tuple(c) for g, c in loops.items()}, starts, stops


def _held(clip, name="idle") -> core.Clip:
    """A still clip holding the first pose of `clip` (for a character without an idle)."""
    poses = {k: v[:1].repeat(2, axis=0) for k, v in clip.poses.items()}
    return core.Clip(name, 1.0, np.zeros((2, 3)), np.zeros(2), poses=poses)


def build(arm, skip_sets: bool = True) -> core.Profile:
    started = time.perf_counter()
    lib = Library(arm, entity_animsets(arm, _SKIP_SETS if skip_sets else None))
    family = "humanoid" if HUMANOID["loops"]["run"][0] in lib.names or HUMANOID["loops"]["walk"][0] in lib.names else "monster"
    idle, loops, starts, stops = (_humanoid if family == "humanoid" else _monster)(lib)
    loops = {g: c for g, c in loops.items() if c and c[0] is not None and c[0].speed > 0.3}
    # Drop starts without forward travel; then fall back to a neighbouring gait.
    starts = {g: [c for c in cs if _heads_where_it_goes(c, math.radians(60))] for g, cs in starts.items()}
    starts = {g: cs for g, cs in starts.items() if any(abs(c.turn) < math.radians(20) for c in cs)}
    if not loops:
        if skip_sets:
            return build(arm, False)
        raise KeyError(f"no locomotion loops in the animation sets of {arm.name}")
    if idle is None:
        idle = _held(next(iter(loops.values()))[0])
    starts = {g: c for g, c in starts.items() if c}
    stops = {g: {k: c for k, c in feet.items() if c is not None} for g, feet in stops.items()}
    profile = core.Profile(family, idle, starts, loops, {g: c for g, c in stops.items() if c})
    log.info("player profile %s (%s): %d clips from %d sets in %.1fs", arm.name, family,
             sum(c is not None for c in lib.loaded.values()), len(lib.sets), time.perf_counter() - started)
    return profile
