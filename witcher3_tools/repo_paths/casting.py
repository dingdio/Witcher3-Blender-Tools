"""Casting index queries: colloquial actor name -> template record (no bpy)."""
from __future__ import annotations

import json
import os
import re
from functools import lru_cache

INDEX_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "CR2W", "data", "casting_index.json",
)


def _norm(value) -> str:
    value = re.sub(r"[_\-.]+", " ", str(value or "").lower())
    value = re.sub(r"[^a-z0-9 ]+", "", value)
    return re.sub(r"\s+", " ", value).strip()


@lru_cache(maxsize=1)
def load_casting_index() -> dict:
    with open(INDEX_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


def casting_record(template_path) -> dict | None:
    """Full record for a template path, with animsets/rigs de-interned."""
    index = load_casting_index()
    rec = index["templates"].get(str(template_path or "").replace("/", "\\").lower())
    if rec is None:
        return None
    strings = index["strings"]
    out = dict(rec)
    out["animsets"] = [strings[i] for i in rec.get("animsets", [])]
    out["rigs"] = [strings[i] for i in rec.get("rigs", [])]
    return out


def resolve_cast(name, category=None, limit=8) -> list[dict]:
    """Rank casting candidates for a colloquial name.

    Returns [{"path", "score", "alias", "record"}] best-first. category filters
    on the record category (character/monster/animal/player/background).
    """
    query = _norm(name)
    if not query:
        return []
    index = load_casting_index()
    aliases = index["aliases"]
    templates = index["templates"]
    scored: dict[str, tuple] = {}

    def offer(paths, base_score, alias, cap):
        # category-filter before capping, or a filtered query can come back empty
        rank = 0
        for path in paths:
            if category and (templates.get(path) or {}).get("category") != category:
                continue
            cur = scored.get(path)
            if cur is None or base_score - rank > cur[0]:
                scored[path] = (base_score - rank, alias)
            rank += 1
            if rank >= cap:
                break

    exact = aliases.get(query)
    if exact:
        offer(exact, 100, query, limit)

    query_tokens = set(query.split())
    if not exact or len(scored) < limit:
        for alias, paths in aliases.items():
            if alias == query:
                continue
            if alias.startswith(query):
                score = 70
            elif query in alias:
                score = 55
            elif query_tokens and query_tokens <= set(alias.split()):
                score = 60
            else:
                continue
            score -= min(len(alias) - len(query), 20) * 0.5
            offer(paths, score, alias, 3)

    results = [
        {"path": path, "score": score, "alias": alias, "record": casting_record(path)}
        for path, (score, alias) in scored.items()
    ]
    results.sort(key=lambda r: (-r["score"], r["path"]))
    return results[: int(limit)]
