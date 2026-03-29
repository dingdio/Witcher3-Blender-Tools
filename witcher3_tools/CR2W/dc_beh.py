"""
dc_beh.py  –  Lightweight .w2beh (Witcher 3 Behavior Graph) reader.

Extracts the data most useful for the addon from a behavior graph binary:
  - idle_animation  : best-guess idle/default animation name
  - animations      : all CBehaviorGraphAnimationNode animation names
  - events          : all CBehaviorEventDescription event names
  - variables       : all CBehaviorVariable float variable names
  - states          : all state-node names
"""

import logging
import os
import re
from collections import namedtuple
from .CR2W_types import getCR2W

log = logging.getLogger(__name__)
_BEH_INFO_CACHE = {}
_BEH_INFO_CACHE_MAX = 256

# ── Public result type ────────────────────────────────────────────────────────

BehInfo = namedtuple("BehInfo", [
    "idle_animation",   # str | None  – best-guess idle anim name
    "animations",       # list[str]   – all animation names referenced
    "events",           # list[str]   – behavior event names
    "variables",        # list[str]   – float variable names
    "states",           # list[str]   – state machine state names
])

# ── Internal helpers ──────────────────────────────────────────────────────────

# State chunk types that carry stateName / nameAsName
_STATE_TYPES = frozenset({
    "CBehaviorGraphStateNode",
    "CBehaviorGraphScriptStateNode",
    "CBehaviorGraphScriptStateReportingNode",
})

# Animation node types that carry an animationName property
_ANIM_NODE_TYPES = frozenset({
    "CBehaviorGraphAnimationNode",
    "CBehaviorGraphAnimationExtNode",
})

# Matches transition-to-idle animations: bear_run_to_idle, bruxa_move_run_to_idle_f, etc.
# Any name containing _to_idle (with any suffix or none) is a transition, not the base idle.
_TRANSITION_TO_IDLE = re.compile(r'_to_idle', re.IGNORECASE)

# Terms that identify non-base-idle animations that should be deprioritised:
#   additive  – additive/overlay animations, never a standalone idle
#   lookat    – look-at modifier animations
#   combat    – combat-state idles (e.g. combat_locomotion_man_geralt_ex_idle)
_DOWNGRADE_TERMS = re.compile(r'additive|lookat|look_at|combat', re.IGNORECASE)


def _cname(chunk, prop_name):
    """Return the string value of a CName property, or None."""
    try:
        p = chunk.GetVariableByName(prop_name)
        if p is None:
            return None
        # CName properties use .Index.String; fall back to .Value for plain strings
        val = getattr(getattr(p, "Index", None), "String", None)
        if val is None:
            val = getattr(p, "Value", None)
        return str(val) if val else None
    except Exception:
        return None


def _guess_idle(animations):
    """Pick the best idle candidate from a list of animation names.

    Priority 1 – 'locomotion'+'idle', not _to_idle, no downgrade terms.
                 (locomotion_idle, woman_noble_locomotion_idle)
    Priority 2 – 'standing'+'idle', not _to_idle, no downgrade terms.
                 (horse_standing_idle01) — beats rider/passenger cross-contamination.
    Priority 3 – 'idle', not _to_idle, no downgrade terms.
                 (bear_idle, monster_archas_idle)
    Priority 4 – 'locomotion'+'idle', not _to_idle   [downgrade terms present]
    Priority 5 – 'idle', not _to_idle                [downgrade terms present]
    Priority 6 – any name containing 'idle'.
    Priority 7 – first animation in the list.

    Downgrade terms: additive, lookat, combat (overlays / combat-specific idles).
    """
    for anim in animations:
        lo = anim.lower()
        if "locomotion" in lo and "idle" in lo and not _TRANSITION_TO_IDLE.search(lo) and not _DOWNGRADE_TERMS.search(lo):
            return anim
    for anim in animations:
        lo = anim.lower()
        if "standing" in lo and "idle" in lo and not _TRANSITION_TO_IDLE.search(lo) and not _DOWNGRADE_TERMS.search(lo):
            return anim
    for anim in animations:
        lo = anim.lower()
        if "idle" in lo and not _TRANSITION_TO_IDLE.search(lo) and not _DOWNGRADE_TERMS.search(lo):
            return anim
    for anim in animations:
        lo = anim.lower()
        if "locomotion" in lo and "idle" in lo and not _TRANSITION_TO_IDLE.search(lo):
            return anim
    for anim in animations:
        lo = anim.lower()
        if "idle" in lo and not _TRANSITION_TO_IDLE.search(lo):
            return anim
    for anim in animations:
        if "idle" in anim.lower():
            return anim
    return animations[0] if animations else None


# Public alias so callers outside this module can reuse the same heuristic
guess_idle = _guess_idle


def _beh_cache_key(beh_path):
    try:
        stat = os.stat(beh_path)
        return (
            os.path.normcase(os.path.normpath(beh_path)),
            stat.st_mtime_ns,
            stat.st_size,
        )
    except Exception:
        return (os.path.normcase(os.path.normpath(beh_path)),)


def _cache_beh_info(cache_key, info):
    _BEH_INFO_CACHE[cache_key] = info
    if len(_BEH_INFO_CACHE) <= _BEH_INFO_CACHE_MAX:
        return
    try:
        first_key = next(iter(_BEH_INFO_CACHE))
    except Exception:
        return
    _BEH_INFO_CACHE.pop(first_key, None)


# ── Public API ────────────────────────────────────────────────────────────────

def read_beh_info(beh_path) -> BehInfo:
    """
    Parse a .w2beh binary file and return a BehInfo.

    Args:
        beh_path: absolute path to the .w2beh file.

    Returns:
        BehInfo with extracted data, or an empty BehInfo on failure.
    """
    empty = BehInfo(None, [], [], [], [])
    cache_key = _beh_cache_key(beh_path)
    cached = _BEH_INFO_CACHE.get(cache_key)
    if cached is not None:
        return cached

    try:
        with open(beh_path, "rb") as f:
            cr2w = getCR2W(f)
    except Exception as e:
        log.warning("dc_beh: could not open %s – %s", beh_path, e)
        return empty

    try:
        chunks = cr2w.CHUNKS.CHUNKS
    except Exception as e:
        log.warning("dc_beh: could not read chunks from %s – %s", beh_path, e)
        return empty

    animations = []
    events     = []
    variables  = []
    states     = []

    for chunk in chunks:
        ctype = getattr(chunk, "name", "") or ""

        if ctype in _ANIM_NODE_TYPES:
            anim = _cname(chunk, "animationName")
            if anim and anim not in animations:
                animations.append(anim)

        elif ctype == "CBehaviorEventDescription":
            ev = _cname(chunk, "eventName")
            if ev and ev not in events:
                events.append(ev)

        elif ctype == "CBehaviorVariable":
            var = _cname(chunk, "name")
            if var and var not in variables:
                variables.append(var)

        elif ctype in _STATE_TYPES:
            state = _cname(chunk, "stateName") or _cname(chunk, "nameAsName")
            if state and state not in states:
                states.append(state)

    info = BehInfo(_find_idle_from_states(chunks, cr2w) or _guess_idle(animations), animations, events, variables, states)
    _cache_beh_info(cache_key, info)
    return info


def _find_idle_from_states(chunks, cr2w):
    """Return the animation name from the state machine state named 'Idle'.

    Builds a parent→children map from CR2WExport.parentID, then walks down
    from any state chunk whose stateName/nameAsName is exactly 'Idle' until
    a CBehaviorGraphAnimationNode is found.
    """
    cr2w_exports = getattr(cr2w, "CR2WExport", None)
    if not cr2w_exports:
        return None

    # Build parent_idx (0-based) → list of child chunks
    parent_to_children = {}
    for chunk in chunks:
        try:
            parent_id = cr2w_exports[chunk.ChunkIndex].parentID
            # parentID is 1-based; 0 means no parent (root)
            parent_idx = parent_id - 1 if parent_id > 0 else None
        except (IndexError, AttributeError):
            parent_idx = None
        if parent_idx is not None:
            parent_to_children.setdefault(parent_idx, []).append(chunk)

    def _collect_anims_under(chunk_idx, out, depth=0):
        """DFS: collect all animation node animationNames in subtree."""
        if depth > 8:
            return
        for child in parent_to_children.get(chunk_idx, []):
            if child.name in _ANIM_NODE_TYPES:
                anim = _cname(child, "animationName")
                if anim:
                    out.append(anim)
            _collect_anims_under(child.ChunkIndex, out, depth + 1)

    # Collect animations from ALL "Idle" state nodes, then pick the best.
    # This ensures a locomotion_idle found in a later state beats a contextual
    # man_carry_crate_idle found in an earlier state.
    all_idle_anims = []
    for chunk in chunks:
        if chunk.name not in _STATE_TYPES:
            continue
        state_name = _cname(chunk, "stateName") or _cname(chunk, "nameAsName") or ""
        if state_name.lower() != "idle":
            continue
        _collect_anims_under(chunk.ChunkIndex, all_idle_anims)

    return _guess_idle(all_idle_anims) if all_idle_anims else None
