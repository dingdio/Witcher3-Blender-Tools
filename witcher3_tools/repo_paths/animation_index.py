"""Animation queries: what can an actor play, by tag/duration (no bpy).

Derives everything at load time from CR2W/data/actor_animations.csv joined with
casting_index rig/animset data — no generated artifact to regenerate.
"""
from __future__ import annotations

import csv
import os
import re
from functools import lru_cache

from .casting import casting_record, resolve_cast

CSV_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "CR2W", "data", "actor_animations.csv",
)
DEFAULT_FPS = 30.0

# token -> tag; multiple tokens may map to one tag
_TAG_TOKENS = {
    "walk": "walk", "run": "run", "sprint": "sprint", "jump": "jump",
    "dodge": "dodge", "roll": "dodge", "evade": "dodge",
    "attack": "attack", "atk": "attack", "strike": "attack", "combo": "attack",
    "hit": "hit", "parry": "parry", "block": "parry", "counter": "parry",
    "death": "death", "die": "death", "dead": "death", "killed": "death",
    "idle": "idle", "stand": "idle", "wait": "idle",
    "sit": "sit", "sleep": "sleep", "swim": "swim", "dance": "dance",
    "drink": "drink", "eat": "eat", "talk": "talk", "gesture": "gesture",
    "greet": "gesture", "work": "work",
    "sword": "sword", "fist": "fist", "fistfight": "fist", "bow": "ranged",
    "crossbow": "ranged", "spawn": "spawn", "taunt": "taunt",
    "scared": "scared", "flee": "flee", "fear": "scared",
    "turn": "turn", "loop": "loop", "additive": "additive",
    "explore": "exploration", "exploration": "exploration",
    "geralt": "geralt", "cs": "cutscene",
}


def _derive_tags(anim_id: str) -> tuple:
    tags = set()
    for token in re.split(r"[_\W]+", str(anim_id or "").lower()):
        tag = _TAG_TOKENS.get(token)
        if tag:
            tags.add(tag)
    return tuple(sorted(tags))


@lru_cache(maxsize=1)
def _load_rows() -> list:
    rows = []
    with open(CSV_PATH, "r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle, delimiter=";"):
            anim_id = (row.get("id") or "").strip()
            file_path = (row.get("file") or "").strip().replace("/", "\\")
            if not anim_id or not file_path:
                continue
            try:
                frames = int(row.get("frames") or 0)
            except ValueError:
                frames = 0
            rows.append({
                "name": anim_id,
                "file": file_path,
                "file_lower": file_path.lower(),
                "frames": frames,
                "duration_s": round(frames / DEFAULT_FPS, 3),
                "tags": _derive_tags(anim_id),
                "caption": (row.get("caption") or "").strip(),
                "category": (row.get("cat1") or "").strip(),
            })
    return rows


# templates with no rig data in the corpus dump (e.g. radish storyboardui geralt)
_FALLBACK_FAMILIES = {"player": {"man"}, "geralt": {"man"}, "geralt npc": {"man"}}


def _rig_families(record: dict) -> set:
    families = set()
    for rig in record.get("rigs") or []:
        base = str(rig).replace("\\", "/").rsplit("/", 1)[-1].lower()
        m = re.match(r"(.+?)_base\.w2rig$", base)
        if m:
            families.add(m.group(1))
    if not families:
        for alias in record.get("aliases") or []:
            families |= _FALLBACK_FAMILIES.get(str(alias).lower(), set())
    return families


def _family_matches(family: str, file_lower: str) -> bool:
    if f"\\{family}\\" in file_lower:
        return True
    # prefix only: "man_horse_sword" is a man-rig animset, not a horse one
    return file_lower.rsplit("\\", 1)[-1].startswith(f"{family}_")


def _actor_record(actor) -> dict | None:
    text = str(actor or "").strip()
    if not text:
        return None
    if text.lower().endswith(".w2ent") or "\\" in text or "/" in text:
        return casting_record(text)
    hits = resolve_cast(text, limit=1)
    return hits[0]["record"] if hits else None


def find_anims(actor=None, tags=None, name_contains="", min_len=None, max_len=None, limit=50) -> list:
    """Rank playable animations. actor: alias/template path (rig-aware filter, ignored
    when unknown); tags: all must match; name_contains: words that must all appear;
    min_len/max_len in seconds."""
    rows = _load_rows()

    record = _actor_record(actor) if actor is not None else None
    if record is not None:
        explicit = {str(p).replace("/", "\\").lower() for p in record.get("animsets") or []}
        families = _rig_families(record)
        def actor_ok(row):
            if row["file_lower"] in explicit:
                return True
            return any(_family_matches(family, row["file_lower"]) for family in families)
        rows = [row for row in rows if actor_ok(row)]

    wanted = {str(t).strip().lower() for t in (tags or []) if str(t).strip()}
    if wanted:
        rows = [row for row in rows if wanted <= set(row["tags"])]
    needles = str(name_contains or "").lower().split()
    if needles:
        rows = [row for row in rows if all(n in row["name"].lower() for n in needles)]
    if min_len is not None:
        rows = [row for row in rows if row["duration_s"] >= float(min_len)]
    if max_len is not None:
        rows = [row for row in rows if row["duration_s"] <= float(max_len)]

    rows = sorted(rows, key=lambda row: (len(row["name"]), row["name"]))
    return [
        {key: row[key] for key in ("name", "file", "frames", "duration_s", "tags", "caption")}
        for row in rows[: int(limit)]
    ]
