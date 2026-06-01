from __future__ import annotations

import logging
import os
from collections import Counter, defaultdict
from pathlib import Path

from ...CR2W_types import getCR2W
from ...prop_utils import prop_to_string, read_array_string_prop
from ... import w3_types
from .scene_dialog_cache_utils import (
    build_signature,
    cache_dir,
    coerce_roots,
    data_dir,
    file_signature,
    format_display,
    iter_files,
    load_cache,
    load_json_file,
    most_common_path,
    normcase,
    normalize_line_id,
    normalize_path,
    normalize_tag,
    repo_relative,
    save_cache,
)
from .scene_dialog_voice_tags import LoadSceneVoiceTags, user_override_paths

log = logging.getLogger(__name__)

_CACHE_VERSION = 9
_CACHE_NAME = os.path.join("SceneDialog", "W3")


def _cache_path() -> Path:
    return cache_dir(_CACHE_NAME) / "w3_scene_dialog_metadata.json"


def _get_addon_prefs():
    try:
        import bpy
    except Exception:
        return None
    context = getattr(bpy, "context", None)
    prefs_root = getattr(context, "preferences", None) if context else None
    addons = getattr(prefs_root, "addons", None) if prefs_root else None
    if not addons:
        return None
    try:
        values = addons.values()
    except Exception:
        values = addons
    try:
        for addon in values:
            prefs = getattr(addon, "preferences", None)
            if prefs is not None and hasattr(prefs, "redkit_depot_path"):
                return prefs
    except Exception:
        return None
    return None


def _redkit_project_paths(prefs):
    paths = []
    try:
        for item in getattr(prefs, "redkit_projects", []) or []:
            path = str(getattr(item, "path", "") or "").strip()
            if path:
                paths.append(path)
    except Exception:
        pass
    return paths


def current_witcher3_scene_roots():
    prefs = _get_addon_prefs()
    if prefs is None:
        return []
    depot_roots = []
    value = str(getattr(prefs, "redkit_depot_path", "") or "").strip()
    if value:
        depot_roots.append(value)
    depot_roots.extend(_redkit_project_paths(prefs))
    if depot_roots:
        return depot_roots
    fallback_roots = []
    for attr in ("uncook_path", "redkit_uncooked_path"):
        value = str(getattr(prefs, attr, "") or "").strip()
        if value:
            fallback_roots.append(value)
    return fallback_roots


def resolve_witcher3_scene_roots(roots=None):
    raw_roots = current_witcher3_scene_roots() if roots is None else coerce_roots(roots)
    resolved = []
    seen = set()

    def record(path):
        norm = normalize_path(str(path or ""))
        if not norm or not os.path.isdir(norm):
            return False
        key = normcase(norm)
        if key in seen:
            return False
        seen.add(key)
        resolved.append(norm)
        return True

    for raw_root in raw_roots:
        root = normalize_path(str(raw_root))
        if not root:
            continue
        basename = os.path.basename(os.path.normpath(root)).lower()
        if basename == "r4data":
            record(root)
            continue
        if record(os.path.join(root, "r4data")):
            continue
        record(root)
    return resolved


def find_w3_scene_files(roots=None):
    return iter_files(resolve_witcher3_scene_roots(roots), ".w2scene")


def resolve_w3_repo_path(repo_path: str, roots=None) -> str:
    repo_path = str(repo_path or "").strip().replace("/", "\\").lstrip("\\")
    if not repo_path:
        return ""
    if os.path.isabs(repo_path) and os.path.exists(repo_path):
        return normalize_path(repo_path)
    variants = [repo_path]
    lower = repo_path.lower()
    if lower.startswith("quests\\skellige\\quest_files\\"):
        variants.append("quests\\sidequests\\skellige\\quest_files\\" + repo_path[len("quests\\skellige\\quest_files\\"):])
    for root in resolve_witcher3_scene_roots(roots):
        for variant in variants:
            candidate = os.path.join(root, variant.replace("\\", os.sep))
            if os.path.exists(candidate):
                return normalize_path(candidate)
    return ""


def _load_voice_names():
    data = load_json_file(data_dir() / "voice_names.json")
    return {str(k): str(v) for k, v in data.items() if str(k or "").strip() and str(v or "").strip()}


def _load_speaker_codes():
    data = load_json_file(data_dir() / "speaker_codes.json")
    return {
        str(k).strip().lower(): str(v).strip()
        for k, v in data.items()
        if not str(k).startswith("_") and str(k or "").strip() and str(v or "").strip()
    }


def _voice_tag_info(registry, speaker_codes, code_or_tag: str):
    raw = str(code_or_tag or "").strip()
    if not raw:
        return None
    try:
        entry = registry.resolve_entry(raw)
    except Exception:
        entry = None
    raw_key = raw.lower()
    tag_id = str(entry.get("id", "") or raw).strip() if isinstance(entry, dict) else raw
    voicetag = normalize_tag(entry.get("voicetag", "") if isinstance(entry, dict) else "")
    display = (
        speaker_codes.get(raw_key)
        or speaker_codes.get(str(tag_id or "").lower())
        or (str(entry.get("display", "") or "") if isinstance(entry, dict) else "")
        or format_display(raw)
        or raw
    )
    if not voicetag:
        voicetag = normalize_tag(display or raw)
    return {
        "display": str(display or "").strip(),
        "speaker": normalize_tag(display),
        "voicetag": voicetag,
        "id": str(tag_id or "").strip().upper(),
        "template": str(entry.get("template", "") or "").strip().replace("/", "\\") if isinstance(entry, dict) else "",
        "entry": dict(entry) if isinstance(entry, dict) else {},
    }


def _iter_prop_values(prop):
    if prop is None:
        return []
    if isinstance(prop, (list, tuple, set)):
        return list(prop)
    for attr in ("value", "More", "elements", "_elements", "Handles"):
        values = getattr(prop, attr, None)
        if values is not None:
            if isinstance(values, (list, tuple, set)):
                return list(values)
            return [values]
    return []


def _prop_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, (int, float, str)):
        text = str(value or "").strip()
        return "" if " object at 0x" in text else text
    try:
        text = prop_to_string(value)
        if text and " object at 0x" not in text:
            return str(text).strip()
    except Exception:
        pass
    handle_path = getattr(value, "DepotPath", None)
    if handle_path:
        return str(handle_path or "").strip()
    for attr_name in ("String", "Value", "val", "id", "alias", "name"):
        attr = getattr(value, attr_name, None)
        if attr is None:
            continue
        if hasattr(attr, "val"):
            attr = getattr(attr, "val", "")
        if hasattr(attr, "String"):
            attr = getattr(attr, "String", "")
        if hasattr(attr, "DepotPath"):
            attr = getattr(attr, "DepotPath", "")
        text = str(attr or "").strip()
        if text and " object at 0x" not in text:
            return text
    return ""


def _prop_text_values(prop):
    values = read_array_string_prop(prop)
    if not values:
        values = [_prop_text(value) for value in _iter_prop_values(prop)]
    out = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _get_var(obj, name):
    getter = getattr(obj, "GetVariableByName", None)
    if not callable(getter):
        return None
    try:
        return getter(name)
    except Exception:
        return None


def _localized_line_id(prop) -> str:
    string_obj = getattr(prop, "String", None) if prop is not None else None
    if string_obj is not None:
        value = getattr(string_obj, "val", None)
        if value is not None:
            return str(value or "").strip()
    return _prop_text(prop).strip()


def _load_scene_struct(scene_path: str):
    with open(scene_path, "rb") as fh:
        cr2w = getCR2W(fh)
    chunks = getattr(getattr(cr2w, "CHUNKS", None), "CHUNKS", []) or []
    story_scene_chunk = next((chunk for chunk in chunks if str(getattr(chunk, "name", "") or "") == "CStoryScene"), None)
    if story_scene_chunk is None:
        return chunks, None
    story_scene = w3_types.CStoryScene()
    story_scene.chunksRef = chunks
    story_scene.LocalizedStringsRef = getattr(cr2w, "LocalizedStrings", None)
    for prop in getattr(story_scene_chunk, "PROPS", []) or []:
        try:
            setattr(story_scene, prop.theName, prop)
        except Exception:
            pass
    return chunks, story_scene


def _chunk_object(chunks, ptr_value, cls):
    try:
        ptr = int(getattr(ptr_value, "Value", ptr_value) or 0)
    except Exception:
        ptr = 0
    if ptr <= 0 or ptr > len(chunks):
        return None
    try:
        return cls(chunks[ptr - 1])
    except Exception:
        return None


def _scene_actor_entity_map(chunks, story_scene):
    result = {}
    if story_scene is None:
        return result

    def add_entity(tag, item):
        tag = normalize_tag(tag)
        if not tag or not isinstance(item, dict):
            return
        path = str(item.get("path", "") or "").strip().replace("/", "\\")
        if not path:
            return
        appearance = str(item.get("appearance", "") or "").strip()
        row = {"path": path, "source": "scene_actor"}
        if appearance:
            row["appearance"] = appearance
        rows = result.setdefault(tag, [])
        key = (path.lower(), appearance.lower())
        if all((str(existing.get("path", "") or "").lower(), str(existing.get("appearance", "") or "").lower()) != key for existing in rows):
            rows.append(row)

    for actor_ref in _iter_prop_values(getattr(story_scene, "sceneTemplates", None)):
        actor = _chunk_object(chunks, actor_ref, w3_types.CStorySceneActor)
        if actor is None:
            continue
        template_path = _prop_text(getattr(actor, "entityTemplate", None)).replace("/", "\\")
        if not template_path:
            continue
        appearances = _prop_text_values(getattr(actor, "appearanceFilter", None))
        entities = [
            {"path": template_path, "appearance": appearance, "source": "scene_actor"}
            for appearance in appearances
        ] or [{"path": template_path, "source": "scene_actor"}]
        keys = [
            _prop_text(getattr(actor, "id", None)),
            _prop_text(getattr(actor, "alias", None)),
        ]
        if not bool(getattr(actor, "dontSearchByVoicetag", False) or False):
            keys.extend(_prop_text(tag) for tag in _iter_prop_values(getattr(actor, "actorTags", None)))
        for key in keys:
            for entity in entities:
                add_entity(key, entity)
    return result


def _scene_dialog_lines(scene_path: str):
    chunks, story_scene = _load_scene_struct(scene_path)
    actor_entity_by_tag = _scene_actor_entity_map(chunks, story_scene)
    rows = []
    for chunk in chunks:
        if str(getattr(chunk, "name", "") or "") != "CStorySceneLine":
            continue
        try:
            line = w3_types.CStorySceneLine(chunk)
        except Exception:
            continue
        line_id = normalize_line_id(_localized_line_id(getattr(line, "dialogLine", None)), numeric=True)
        tag = normalize_tag(_prop_text(getattr(line, "voicetag", None)))
        if not line_id or not tag:
            continue
        rows.append((line_id, tag, actor_entity_by_tag.get(tag, [])))
    return rows


def _load_voice_tag_entity_index():
    data = load_json_file(data_dir() / "dialogue" / "w3" / "voice_tag_entities.json")
    entries = data.get("voice_tags", data)
    if not isinstance(entries, dict):
        return {}
    result = {}
    for tag, items in entries.items():
        tag_key = normalize_tag(tag)
        if not tag_key or not isinstance(items, list):
            continue
        rows = []
        seen = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path", "") or "").strip().replace("/", "\\")
            appearance = str(item.get("appearance", "") or "").strip()
            if not path or not appearance:
                continue
            key = (path.lower(), appearance.lower())
            if key in seen:
                continue
            seen.add(key)
            rows.append({
                "path": path,
                "appearance": appearance,
                "source": "entity_voice_tag",
            })
        if rows:
            result[tag_key] = rows
    return result


def _merge_scene_and_voice_tag_entities(scene_entities, voice_tag_entities):
    voice_tag_entities = [dict(item) for item in (voice_tag_entities or []) if isinstance(item, dict)]
    result = []
    seen = set()

    def add(item):
        if not isinstance(item, dict):
            return
        path = str(item.get("path", "") or "").strip().replace("/", "\\")
        if not path:
            return
        appearance = str(item.get("appearance", "") or "").strip()
        key = (path.lower(), appearance.lower())
        if key in seen:
            return
        seen.add(key)
        out = dict(item)
        out["path"] = path
        if appearance:
            out["appearance"] = appearance
        result.append(out)

    for item in scene_entities or []:
        add(item)
    for item in voice_tag_entities:
        add(item)
    return result


def _entity_path_from_key(key) -> str:
    if isinstance(key, tuple):
        key = key[0] if key else ""
    return str(key or "").strip().replace("/", "\\")


def _entity_appearance_from_key(key) -> str:
    if isinstance(key, tuple) and len(key) > 1:
        return str(key[1] or "").strip()
    return ""


def _signature(scene_files, roots=None, registry=None):
    registry_dict = registry.as_dict() if registry is not None else {}
    base_dir = data_dir()
    return build_signature(
        scene_files,
        resolve_witcher3_scene_roots(roots),
        extra={
            "scene_dialog_version": _CACHE_VERSION,
            "voice_names": file_signature(base_dir / "voice_names.json"),
            "speaker_codes": file_signature(base_dir / "speaker_codes.json"),
            "scene_voice_tags": file_signature(base_dir / "dialogue" / "w3" / "scene_voice_tags.json"),
            "voice_tag_entities": file_signature(base_dir / "dialogue" / "w3" / "voice_tag_entities.json"),
            "scene_voice_tag_overrides": [file_signature(path) for path in user_override_paths(create=False)],
            "scene_voice_tags_entry_count": len(registry_dict.get("entries", []) or []),
            "scene_voice_tags_id_count": int(registry_dict.get("id_count", 0) or 0),
            "scene_voice_tags_voicetag_count": int(registry_dict.get("voicetag_count", 0) or 0),
        },
    )


def _build_metadata(scene_files, roots, signature, registry):
    resolved_roots = resolve_witcher3_scene_roots(roots)
    voice_names = _load_voice_names()
    speaker_codes = _load_speaker_codes()

    line_tags = defaultdict(Counter)
    line_scenes = defaultdict(Counter)
    line_entities = defaultdict(lambda: defaultdict(Counter))
    speaker_entities = defaultdict(Counter)
    speaker_line_counts = Counter()
    tag_infos = {}
    scene_parse_errors = 0
    entity_voice_tags = _load_voice_tag_entity_index()
    entity_voice_tag_link_count = sum(len(items) for items in entity_voice_tags.values())

    def remember_info(info):
        if not info:
            return
        tag = normalize_tag(info.get("voicetag", ""))
        if tag and tag not in tag_infos:
            tag_infos[tag] = info

    def add_line_info(line_id, info, score, scene_rel=""):
        line_id = normalize_line_id(line_id, numeric=True)
        if not line_id or not info:
            return
        tag = normalize_tag(info.get("voicetag", ""))
        if not tag:
            return
        remember_info(info)
        line_tags[line_id][tag] += int(score)
        speaker_line_counts[tag] += 1
        if scene_rel:
            line_scenes[line_id][scene_rel] += 1

    for line_id, code in voice_names.items():
        add_line_info(line_id, _voice_tag_info(registry, speaker_codes, code), 10)

    for scene_path in scene_files:
        scene_rel = repo_relative(scene_path, resolved_roots)
        try:
            scene_lines = _scene_dialog_lines(scene_path)
        except Exception:
            scene_parse_errors += 1
            log.debug("Failed to parse W3 scene dialogue metadata: %s", scene_path, exc_info=True)
            continue

        for line_id, tag, scene_entities in scene_lines:
            info = _voice_tag_info(registry, speaker_codes, tag)
            add_line_info(line_id, info, 30, scene_rel=scene_rel)
            for entity in scene_entities or []:
                if not isinstance(entity, dict):
                    continue
                entity_path = str(entity.get("path", "") or "").strip().replace("/", "\\")
                if not entity_path:
                    continue
                appearance = str(entity.get("appearance", "") or "").strip()
                line_entities[line_id][tag][(entity_path, appearance)] += 1
                speaker_entities[tag][entity_path] += 1

    for entry in getattr(registry, "entries", []) or []:
        tag = normalize_tag(entry.get("voicetag", ""))
        if tag and tag not in tag_infos:
            tag_infos[tag] = _voice_tag_info(registry, speaker_codes, entry.get("id") or tag) or {
                "display": entry.get("display", tag),
                "speaker": normalize_tag(entry.get("display", tag)),
                "voicetag": tag,
                "id": str(entry.get("id", "") or "").upper(),
                "template": str(entry.get("template", "") or "").replace("/", "\\"),
                "entry": dict(entry),
            }

    speakers_out = {}
    for tag in sorted(tag_infos):
        info = tag_infos.get(tag) or {}
        template = str(info.get("template", "") or "").strip().replace("/", "\\")
        entity_path = template or most_common_path(speaker_entities.get(tag, Counter()))
        speakers_out[tag] = {
            "speaker": str(info.get("speaker", "") or "").upper(),
            "display": str(info.get("display", "") or ""),
            "voicetag": tag,
            "id": str(info.get("id", "") or "").upper(),
            "line_count": int(speaker_line_counts.get(tag, 0)),
            "entity_path": entity_path,
            "voice_tag": info.get("entry", {}) or {},
        }

    def speaker_entry_for(tag):
        entry = speakers_out.get(tag)
        if entry:
            return entry
        info = tag_infos.get(tag) or {"display": format_display(tag), "speaker": normalize_tag(format_display(tag)), "id": ""}
        return {
            "speaker": str(info.get("speaker", "") or "").upper(),
            "display": str(info.get("display", "") or ""),
            "voicetag": tag,
            "id": str(info.get("id", "") or "").upper(),
            "line_count": 0,
            "entity_path": "",
            "voice_tag": info.get("entry", {}) or {},
        }

    lines_out = {}
    for line_id, tag_counts in line_tags.items():
        ranked_tags = sorted(tag_counts.items(), key=lambda item: (-int(item[1]), item[0]))
        best_tag = ranked_tags[0][0] if ranked_tags else ""
        speaker_entry = speaker_entry_for(best_tag)
        source_scenes = [
            path for path, _count in sorted(
                line_scenes.get(line_id, Counter()).items(),
                key=lambda item: (-int(item[1]), item[0].lower()),
            )[:5]
        ]
        line_entity_counter = line_entities.get(line_id, {}).get(best_tag, Counter())
        direct_path_counter = Counter()
        for key, count in line_entity_counter.items():
            path = _entity_path_from_key(key)
            if path:
                direct_path_counter[path] += int(count)
        direct_entity_path = most_common_path(direct_path_counter)
        entity_paths = [
            {
                "path": _entity_path_from_key(key),
                "appearance": _entity_appearance_from_key(key),
                "count": int(count),
                "source": "scene_actor",
            }
            for key, count in sorted(
                line_entity_counter.items(),
                key=lambda item: (
                    -int(item[1]),
                    _entity_path_from_key(item[0]).lower(),
                    _entity_appearance_from_key(item[0]).lower(),
                ),
            )
            if _entity_path_from_key(key)
        ]
        if best_tag:
            entity_paths = _merge_scene_and_voice_tag_entities(entity_paths, entity_voice_tags.get(best_tag))
        entity_path = direct_entity_path
        lines_out[str(line_id)] = {
            "speaker": str(speaker_entry.get("speaker", "") or "").upper(),
            "display": str(speaker_entry.get("display", "") or ""),
            "voicetag": best_tag,
            "id": str(speaker_entry.get("id", "") or "").upper(),
            "speakers": [
                {
                    "name": str(speaker_entry_for(tag).get("speaker", "") or "").upper(),
                    "display": str(speaker_entry_for(tag).get("display", "") or ""),
                    "voicetag": tag,
                    "id": str(speaker_entry_for(tag).get("id", "") or ""),
                    "count": int(count),
                }
                for tag, count in ranked_tags[:4]
            ],
            "speaker_ambiguous": len(ranked_tags) > 1,
            "scene_path": source_scenes[0] if source_scenes else "",
            "source_scenes": source_scenes,
            "entity_path": entity_path,
            "entity_paths": entity_paths,
            "voice_tag": speaker_entry.get("voice_tag", {}) or {},
        }

    return {
        "version": _CACHE_VERSION,
        "signature": signature,
        "stats": {
            "scene_count": len(scene_files),
            "scene_parse_errors": scene_parse_errors,
            "entity_voice_tag_count": len(entity_voice_tags),
            "entity_voice_tag_link_count": entity_voice_tag_link_count,
            "line_count": len(lines_out),
            "speaker_count": len(speakers_out),
            "voice_tag_count": len(getattr(registry, "entries", []) or []),
            "source": "scan",
        },
        "lines": lines_out,
        "speakers": speakers_out,
        "voice_tags": registry.as_dict(),
    }


class W3SceneDialogMetadata:
    def __init__(self, data):
        self.data = data if isinstance(data, dict) else {}
        self.lines = self.data.get("lines", {}) if isinstance(self.data.get("lines", {}), dict) else {}
        self.speakers = self.data.get("speakers", {}) if isinstance(self.data.get("speakers", {}), dict) else {}
        self.stats = self.data.get("stats", {}) if isinstance(self.data.get("stats", {}), dict) else {}
        self.voice_tags = self.data.get("voice_tags", {}) if isinstance(self.data.get("voice_tags", {}), dict) else {}

    def get_line(self, line_id) -> dict:
        return self.lines.get(str(normalize_line_id(line_id, numeric=True)), {}) or {}

    def get_speaker(self, speaker) -> dict:
        return self.speakers.get(normalize_tag(speaker), {}) or {}

    def resolve_line_speaker(self, line_id) -> str:
        return str(self.get_line(line_id).get("speaker", "") or "").strip().upper()

    def resolve_line_entity(self, line_id, speaker="") -> str:
        line = self.get_line(line_id)
        return str(line.get("entity_path", "") or "").strip()

    def preload_line_summaries(self) -> dict:
        return self.lines


_metadata_cache = None
_metadata_signature = None


def LoadWitcher3SceneDialogMetadata(do_reload=False, roots=None) -> W3SceneDialogMetadata:
    global _metadata_cache, _metadata_signature

    if not do_reload and roots is None and _metadata_cache is not None:
        if not (isinstance(_metadata_signature, tuple) and _metadata_signature[:1] == ("shipped",)):
            return _metadata_cache

    if not do_reload and roots is None:
        try:
            from .scene_dialog_index import LoadSceneDialogIndexMetadata

            shipped_metadata = LoadSceneDialogIndexMetadata("W3")
            if shipped_metadata is not None and shipped_metadata.available():
                _metadata_cache = shipped_metadata
                _metadata_signature = ("shipped", str(shipped_metadata.db_path))
                return shipped_metadata
        except Exception:
            log.debug("Failed to load shipped W3 scene dialogue index.", exc_info=True)

    registry = LoadSceneVoiceTags(do_reload=do_reload)
    scene_files = find_w3_scene_files(roots)
    signature = _signature(scene_files, roots=roots, registry=registry)
    if not do_reload and _metadata_cache is not None and _metadata_signature == signature:
        return _metadata_cache

    data = None if do_reload else load_cache(_cache_path(), _CACHE_VERSION, signature)
    if data is None:
        data = _build_metadata(scene_files, roots, signature, registry)
        save_cache(_cache_path(), data)
        try:
            from .scene_dialog_index import SaveUserSceneDialogIndexMetadata

            SaveUserSceneDialogIndexMetadata("W3", data)
        except Exception:
            log.debug("Failed to write user W3 scene dialogue index.", exc_info=True)

    _metadata_cache = W3SceneDialogMetadata(data)
    _metadata_signature = signature
    return _metadata_cache


def ClearWitcher3SceneDialogMetadataCache():
    global _metadata_cache, _metadata_signature
    _metadata_cache = None
    _metadata_signature = None
