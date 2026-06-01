from __future__ import annotations

import csv
import json
import logging
import os
import re
from collections import defaultdict
from pathlib import Path

from ....extension_paths import get_cache_root, get_dev_override_list

log = logging.getLogger(__name__)

_CACHE = None


def _data_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "data"


def _dialogue_data_dir() -> Path:
    return _data_dir() / "dialogue" / "w3"


def _shipped_path() -> Path:
    return _dialogue_data_dir() / "scene_voice_tags.json"


def _cache_override_dir(create=False) -> Path:
    return Path(get_cache_root(create=create)) / "SceneDialog" / "VoiceTags"


def _normalize_id(value: str) -> str:
    return str(value or "").strip().lower()


def _normalize_voicetag(value: str) -> str:
    return str(value or "").strip().upper()


def _format_display(value: str) -> str:
    value = str(value or "").strip().replace("_", " ")
    if not value:
        return ""
    parts = []
    for part in value.split():
        if any(ch.isdigit() for ch in part):
            parts.append(part.upper())
        else:
            parts.append(part.capitalize())
    return " ".join(parts)


def _scope_from_path(path: str) -> str:
    norm = str(path or "").replace("/", "\\").lower()
    match = re.search(r"\\dlc\\([^\\]+)\\", "\\" + norm)
    if match:
        return match.group(1)
    return "base"


def _entry_from_mapping(mapping, *, source="", source_index=0, row_index=0, user=False):
    if not isinstance(mapping, dict):
        return None
    voicetag = _normalize_voicetag(mapping.get("voicetag", mapping.get("Voicetag", "")))
    tag_id = str(mapping.get("id", mapping.get("Id", "")) or "").strip()
    if not voicetag and not tag_id:
        return None

    scope = str(mapping.get("scope", mapping.get("Scope", "")) or "").strip()
    if not scope:
        scope = _scope_from_path(source)

    aliases = mapping.get("aliases", mapping.get("Aliases", [])) or []
    if isinstance(aliases, str):
        aliases = [aliases]

    entry = {
        "game": str(mapping.get("game", mapping.get("Game", "W3")) or "W3").strip().upper(),
        "voicetag": voicetag,
        "display": str(mapping.get("display", mapping.get("Display", "")) or "").strip() or _format_display(voicetag),
        "id": tag_id,
        "id_key": _normalize_id(tag_id),
        "sex": str(mapping.get("sex", mapping.get("Sex", "")) or "").strip(),
        "pitch": str(mapping.get("pitch", mapping.get("Pitch", "")) or "").strip(),
        "template": str(mapping.get("template", mapping.get("Template", "")) or "").strip().replace("/", "\\"),
        "source": str(mapping.get("source", mapping.get("Source", source)) or source).replace("/", "\\"),
        "scope": scope,
        "source_index": int(mapping.get("source_index", source_index) or 0),
        "row_index": int(mapping.get("row_index", row_index) or 0),
        "user": bool(mapping.get("user", user)),
        "aliases": sorted({_normalize_id(alias) for alias in aliases if str(alias or "").strip()}),
    }
    return entry


def _read_json_entries(path: Path, *, source_index=0, user=False):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        log.warning("Failed to read scene voice tags JSON: %s", path, exc_info=True)
        return []
    raw_entries = data.get("entries", data) if isinstance(data, dict) else data
    if not isinstance(raw_entries, list):
        return []
    source_label = str(path)
    entries = []
    for row_index, raw_entry in enumerate(raw_entries):
        entry = _entry_from_mapping(
            raw_entry,
            source=source_label,
            source_index=source_index,
            row_index=row_index,
            user=user,
        )
        if entry:
            entries.append(entry)
    return entries


def _read_csv_entries(path: Path, *, source_index=0, user=False):
    entries = []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle, delimiter=";")
            for row_index, row in enumerate(reader):
                entry = _entry_from_mapping(
                    row,
                    source=str(path),
                    source_index=source_index,
                    row_index=row_index,
                    user=user,
                )
                if entry:
                    entries.append(entry)
    except Exception:
        log.warning("Failed to read scene voice tags CSV: %s", path, exc_info=True)
    return entries


def _read_entries(path: Path, *, source_index=0, user=False):
    suffix = path.suffix.lower()
    if suffix == ".json":
        return _read_json_entries(path, source_index=source_index, user=user)
    if suffix == ".csv":
        return _read_csv_entries(path, source_index=source_index, user=user)
    return []


def _existing_paths(paths):
    seen = set()
    result = []
    for path in paths:
        if not path:
            continue
        path = Path(path)
        key = os.path.normcase(os.path.normpath(str(path)))
        if key in seen or not path.is_file():
            continue
        seen.add(key)
        result.append(path)
    return result


def user_override_paths(create=False):
    cache_dir = _cache_override_dir(create=create)
    paths = [
        _dialogue_data_dir() / "scene_voice_tags_user.json",
        _dialogue_data_dir() / "scene_voice_tags_user.csv",
        cache_dir / "scene_voice_tags_user.json",
        cache_dir / "scene_voice_tags_user.csv",
    ]
    paths.extend(Path(p) for p in get_dev_override_list("scene_voice_tags_paths", []) if isinstance(p, str) and p)
    return _existing_paths(paths)


class SceneVoiceTagRegistry:
    def __init__(self, entries):
        self.entries = list(entries or [])
        self.by_id = defaultdict(list)
        self.by_alias = defaultdict(list)
        self.by_voicetag = defaultdict(list)

        for entry in self.entries:
            id_key = entry.get("id_key") or _normalize_id(entry.get("id"))
            if id_key:
                self.by_id[id_key].append(entry)
                self.by_alias[id_key].append(entry)
            voicetag_key = _normalize_voicetag(entry.get("voicetag"))
            if voicetag_key:
                self.by_voicetag[voicetag_key].append(entry)
                self.by_alias[_normalize_id(voicetag_key)].append(entry)
            for alias in entry.get("aliases", []) or []:
                if alias:
                    self.by_alias[alias].append(entry)

        for bucket in (self.by_id, self.by_alias, self.by_voicetag):
            for key, values in list(bucket.items()):
                bucket[key] = self._sort_entries(values)

    @staticmethod
    def _sort_entries(entries):
        return sorted(
            entries,
            key=lambda entry: (
                0 if entry.get("user") else 1,
                int(entry.get("source_index", 0) or 0),
                int(entry.get("row_index", 0) or 0),
                str(entry.get("voicetag", "")),
            ),
        )

    def entries_for_id(self, tag_id: str):
        return list(self.by_id.get(_normalize_id(tag_id), []))

    def entries_for_alias(self, value: str):
        return list(self.by_alias.get(_normalize_id(value), []))

    def entries_for_voicetag(self, voicetag: str):
        return list(self.by_voicetag.get(_normalize_voicetag(voicetag), []))

    def primary_for_id(self, tag_id: str):
        entries = self.entries_for_id(tag_id)
        return entries[0] if entries else None

    def primary_for_voicetag(self, voicetag: str):
        entries = self.entries_for_voicetag(voicetag)
        return entries[0] if entries else None

    def resolve_entry(self, value: str):
        candidates = self.entries_for_alias(value)
        return candidates[0] if candidates else None

    def display_for_id(self, tag_id: str) -> str:
        entry = self.primary_for_id(tag_id)
        return str(entry.get("display", "") or "") if entry else ""

    def display_for_value(self, value: str) -> str:
        entry = self.resolve_entry(value)
        return str(entry.get("display", "") or "") if entry else ""

    def voicetag_for_id(self, tag_id: str) -> str:
        entry = self.primary_for_id(tag_id)
        return str(entry.get("voicetag", "") or "") if entry else ""

    def candidates_for_id(self, tag_id: str):
        return [
            {
                "voicetag": entry.get("voicetag", ""),
                "display": entry.get("display", ""),
                "id": entry.get("id", ""),
                "sex": entry.get("sex", ""),
                "pitch": entry.get("pitch", ""),
                "template": entry.get("template", ""),
                "scope": entry.get("scope", ""),
                "source": entry.get("source", ""),
                "user": bool(entry.get("user")),
            }
            for entry in self.entries_for_id(tag_id)
        ]

    def as_dict(self):
        return {
            "entries": list(self.entries),
            "id_count": len(self.by_id),
            "voicetag_count": len(self.by_voicetag),
        }


def LoadSceneVoiceTags(do_reload=False, extra_paths=None) -> SceneVoiceTagRegistry:
    global _CACHE
    if _CACHE is not None and not do_reload and not extra_paths:
        return _CACHE

    paths = []
    shipped = _shipped_path()
    if shipped.is_file():
        paths.append((shipped, False))
    for path in user_override_paths(create=False):
        paths.append((path, True))
    for path in extra_paths or []:
        paths.append((Path(path), True))

    entries = []
    for source_index, (path, user) in enumerate(paths):
        entries.extend(_read_entries(path, source_index=source_index, user=user))

    registry = SceneVoiceTagRegistry(entries)
    if not extra_paths:
        _CACHE = registry
    return registry


def ClearSceneVoiceTagsCache():
    global _CACHE
    _CACHE = None
