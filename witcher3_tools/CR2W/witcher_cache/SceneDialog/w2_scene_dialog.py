from __future__ import annotations

import logging
import os
from collections import Counter, defaultdict
from pathlib import Path

from ...CR2W_types import CSTRING, getCR2W
from ...prop_utils import prop_to_string, read_array_string_prop
from .scene_dialog_cache_utils import (
    build_signature,
    cache_dir,
    coerce_roots,
    file_signature,
    iter_files,
    load_cache,
    most_common_path,
    normcase,
    normalize_line_id,
    normalize_path,
    normalize_tag,
    repo_relative,
    save_cache,
)

log = logging.getLogger(__name__)

_CACHE_VERSION = 8
_CACHE_NAME = os.path.join("SceneDialog", "W2")
_CANONICAL_W2_ENTITIES = {
    "GERALT": "characters\\templates\\witcher\\player.w2ent",
}


def _cache_path() -> Path:
    return cache_dir(_CACHE_NAME) / "w2_scene_dialog_metadata.json"


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
            if prefs is not None and hasattr(prefs, "witcher2_game_path"):
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


def current_witcher2_scene_roots():
    prefs = _get_addon_prefs()
    if prefs is None:
        return []
    roots = []
    for attr in ("w2_unbundle_path", "witcher2_game_path"):
        value = str(getattr(prefs, attr, "") or "").strip()
        if value:
            roots.append(value)
    roots.extend(_redkit_project_paths(prefs))
    return roots


def resolve_witcher2_scene_roots(roots=None):
    raw_roots = current_witcher2_scene_roots() if roots is None else coerce_roots(roots)
    resolved = []
    seen = set()

    def record(path):
        norm = normalize_path(str(path or ""))
        if not norm or not os.path.isdir(norm):
            return
        key = normcase(norm)
        if key in seen:
            return
        seen.add(key)
        resolved.append(norm)

    for raw_root in raw_roots:
        root = normalize_path(str(raw_root))
        if not root:
            continue
        basename = os.path.basename(os.path.normpath(root)).lower()
        if basename in {"dataoff", "data"}:
            record(root)
        else:
            before_count = len(resolved)
            record(os.path.join(root, "dataOFF"))
            record(os.path.join(root, "data"))
            if len(resolved) == before_count:
                record(root)
    return resolved


def find_w2_scene_files(roots=None):
    return iter_files(resolve_witcher2_scene_roots(roots), ".w2scene")


def resolve_w2_repo_path(repo_path: str, roots=None) -> str:
    repo_path = str(repo_path or "").strip().replace("/", "\\").lstrip("\\")
    if not repo_path:
        return ""
    if os.path.isabs(repo_path) and os.path.exists(repo_path):
        return normalize_path(repo_path)
    for root in resolve_witcher2_scene_roots(roots):
        candidate = os.path.join(root, repo_path.replace("\\", os.sep))
        if os.path.exists(candidate):
            return normalize_path(candidate)
    return ""


def _prop_string(prop) -> str:
    if not prop:
        return ""
    try:
        return str(prop_to_string(prop) or "")
    except Exception:
        return ""


def _prop_string_values(prop):
    values = read_array_string_prop(prop)
    if not values:
        values = [_prop_string(value) for value in _iter_prop_values(prop)]
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


def _localized_line_id(prop) -> str:
    if not prop:
        return ""
    string_obj = getattr(prop, "String", None)
    value = getattr(string_obj, "val", None)
    if value is not None:
        return str(value or "").strip()
    return _prop_string(prop).strip()


def _chunks(cr2w):
    return getattr(getattr(cr2w, "CHUNKS", None), "CHUNKS", []) or []


def _get_var(obj, name):
    try:
        return obj.GetVariableByName(name)
    except Exception:
        return None


def _scene_lines(chunks):
    for chunk in chunks:
        if str(getattr(chunk, "name", "") or "") != "CStorySceneLine":
            continue
        line_id = _localized_line_id(_get_var(chunk, "dialogLine"))
        speaker = normalize_tag(_prop_string(_get_var(chunk, "voicetag")))
        if line_id and speaker:
            yield normalize_line_id(line_id, numeric=True), speaker


def _scene_template_speakers(chunks):
    speakers = []
    for chunk in chunks:
        if str(getattr(chunk, "name", "") or "") != "CStoryScene":
            continue
        scene_templates = _get_var(chunk, "sceneTemplates")
        for item in getattr(scene_templates, "More", []) or []:
            item_name = str(getattr(item, "theName", "") or "")
            if item_name == "voicetag":
                speaker = normalize_tag(_prop_string(item))
                if speaker:
                    speakers.append(speaker)
                continue
            get_var = getattr(item, "GetVariableByName", None)
            if callable(get_var):
                speaker = normalize_tag(_prop_string(_get_var(item, "voicetag")))
                if speaker:
                    speakers.append(speaker)
    return speakers


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


def _ptr_value(ptr):
    try:
        return int(getattr(ptr, "Value", ptr) or 0)
    except Exception:
        return 0


def _w2_dependency_paths(fh, cr2w):
    tables = getattr(cr2w, "CR2WTable", []) or []
    if len(tables) <= 3:
        return []
    table = tables[3]
    try:
        offset = int(getattr(table, "offset", 0) or 0) + int(getattr(cr2w, "start", 0) or 0)
        count = int(getattr(table, "itemCount", 0) or 0)
    except Exception:
        return []
    if offset <= 0 or count <= 0:
        return []

    current = fh.tell()
    paths = []
    try:
        fh.seek(offset)
        for _idx in range(count):
            try:
                value = str(CSTRING(fh).String or "").strip().replace("/", "\\")
            except Exception:
                break
            paths.append(value)
    finally:
        try:
            fh.seek(current)
        except Exception:
            pass
    return paths


def _entity_template_path(prop, dependencies):
    if prop is None:
        return ""
    for attr in ("DepotPath", "path"):
        value = str(getattr(prop, attr, "") or "").strip().replace("/", "\\")
        if value.lower().endswith(".w2ent"):
            return value
    index = getattr(prop, "Index", None)
    try:
        dep_index = int(getattr(index, "Index", 0) or 0)
    except Exception:
        dep_index = 0
    if dep_index > 0 and dep_index <= len(dependencies):
        value = str(dependencies[dep_index - 1] or "").strip().replace("/", "\\")
        if value.lower().endswith(".w2ent"):
            return value
    value = _prop_string(prop).replace("/", "\\")
    return value if value.lower().endswith(".w2ent") else ""


def _scene_template_entities(chunks, dependencies=None):
    dependencies = list(dependencies or [])
    entity_by_speaker = {}

    def add_entity(speaker, item):
        speaker = normalize_tag(speaker)
        if not speaker or not isinstance(item, dict):
            return
        path = str(item.get("path", "") or "").strip().replace("/", "\\")
        if not path:
            return
        appearance = str(item.get("appearance", "") or "").strip()
        row = {"path": path, "source": "scene_actor"}
        if appearance:
            row["appearance"] = appearance
        rows = entity_by_speaker.setdefault(speaker, [])
        key = (path.lower(), appearance.lower())
        if all((str(existing.get("path", "") or "").lower(), str(existing.get("appearance", "") or "").lower()) != key for existing in rows):
            rows.append(row)

    for chunk in chunks:
        if str(getattr(chunk, "name", "") or "") != "CStoryScene":
            continue
        scene_templates = _get_var(chunk, "sceneTemplates")
        for ptr in _iter_prop_values(scene_templates):
            actor_chunk = None
            ref = _ptr_value(ptr)
            if ref > 0 and ref <= len(chunks):
                actor_chunk = chunks[ref - 1]
            elif hasattr(ptr, "GetVariableByName"):
                actor_chunk = ptr
            if actor_chunk is None:
                continue
            template_path = _entity_template_path(_get_var(actor_chunk, "entityTemplate"), dependencies)
            if not template_path:
                continue
            appearances = _prop_string_values(_get_var(actor_chunk, "appearanceFilter"))
            entities = [
                {"path": template_path, "appearance": appearance, "source": "scene_actor"}
                for appearance in appearances
            ] or [{"path": template_path, "source": "scene_actor"}]
            keys = [
                _prop_string(_get_var(actor_chunk, "voicetag")),
                _prop_string(_get_var(actor_chunk, "id")),
                _prop_string(_get_var(actor_chunk, "alias")),
            ]
            dont_search = str(_prop_string(_get_var(actor_chunk, "dontSearchByVoicetag"))).lower() in {"true", "1"}
            if not dont_search:
                actor_tags = _get_var(actor_chunk, "actorTags")
                keys.extend(_prop_string(tag) for tag in _iter_prop_values(actor_tags))
            for key in keys:
                for entity in entities:
                    add_entity(key, entity)
    return entity_by_speaker


def _signature(scene_files, roots=None):
    return build_signature(
        scene_files,
        resolve_witcher2_scene_roots(roots),
        extra={
            "scene_dialog_version": _CACHE_VERSION,
            "w2_canonical_entities": dict(_CANONICAL_W2_ENTITIES),
        },
    )


def _entity_path_from_key(key) -> str:
    if isinstance(key, tuple):
        key = key[0] if key else ""
    return str(key or "").strip().replace("/", "\\")


def _entity_appearance_from_key(key) -> str:
    if isinstance(key, tuple) and len(key) > 1:
        return str(key[1] or "").strip()
    return ""


def _build_metadata(scene_files, roots, signature):
    resolved_roots = resolve_witcher2_scene_roots(roots)
    line_speakers = defaultdict(Counter)
    line_scenes = defaultdict(Counter)
    line_entities = defaultdict(lambda: defaultdict(Counter))
    speaker_entities = defaultdict(Counter)
    speaker_line_counts = Counter()
    parse_errors = 0

    for scene_path in scene_files:
        scene_rel = repo_relative(scene_path, resolved_roots)
        try:
            with open(scene_path, "rb") as fh:
                cr2w = getCR2W(fh)
                chunks = _chunks(cr2w)
                dependencies = _w2_dependency_paths(fh, cr2w)
        except Exception:
            parse_errors += 1
            log.debug("Failed to parse W2 scene dialogue metadata: %s", scene_path, exc_info=True)
            continue

        scene_template_speakers = _scene_template_speakers(chunks)
        entity_by_speaker = _scene_template_entities(chunks, dependencies)
        scene_lines = list(_scene_lines(chunks))
        scene_speakers = sorted({speaker for _line_id, speaker in scene_lines} | set(scene_template_speakers))

        for line_id, speaker in scene_lines:
            line_speakers[line_id][speaker] += 1
            line_scenes[line_id][scene_rel] += 1
            speaker_line_counts[speaker] += 1
            canonical_path = _CANONICAL_W2_ENTITIES.get(speaker)
            if canonical_path:
                scene_entities = [{"path": canonical_path, "source": "scene_actor"}]
            else:
                scene_entities = entity_by_speaker.get(speaker, [])
            for entity in scene_entities or []:
                if not isinstance(entity, dict):
                    continue
                entity_path = str(entity.get("path", "") or "").strip().replace("/", "\\")
                if not entity_path:
                    continue
                appearance = str(entity.get("appearance", "") or "").strip()
                line_entities[line_id][speaker][(entity_path, appearance)] += 1
                speaker_entities[speaker][entity_path] += 1

    speakers_out = {}
    for speaker in sorted(set(speaker_line_counts) | set(_CANONICAL_W2_ENTITIES)):
        entity_path = _CANONICAL_W2_ENTITIES.get(speaker) or most_common_path(speaker_entities.get(speaker, Counter()))
        speakers_out[speaker] = {
            "line_count": int(speaker_line_counts.get(speaker, 0)),
            "entity_path": entity_path,
        }

    lines_out = {}
    for line_id, speaker_counts in line_speakers.items():
        ranked_speakers = sorted(speaker_counts.items(), key=lambda item: (-int(item[1]), item[0]))
        best_speaker = ranked_speakers[0][0] if ranked_speakers else ""
        source_scenes = [
            path for path, _count in sorted(
                line_scenes.get(line_id, Counter()).items(),
                key=lambda item: (-int(item[1]), item[0].lower()),
            )[:5]
        ]
        line_entity_counter = line_entities.get(line_id, {}).get(best_speaker, Counter())
        direct_path_counter = Counter()
        for key, count in line_entity_counter.items():
            path = _entity_path_from_key(key)
            if path:
                direct_path_counter[path] += int(count)
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
        entity_path = most_common_path(direct_path_counter)
        lines_out[str(line_id)] = {
            "speaker": best_speaker,
            "speakers": [{"name": speaker, "count": int(count)} for speaker, count in ranked_speakers[:4]],
            "speaker_ambiguous": len(ranked_speakers) > 1,
            "scene_path": source_scenes[0] if source_scenes else "",
            "source_scenes": source_scenes,
            "entity_path": entity_path,
            "entity_paths": entity_paths,
        }

    return {
        "version": _CACHE_VERSION,
        "signature": signature,
        "stats": {
            "scene_count": len(scene_files),
            "scene_parse_errors": parse_errors,
            "line_count": len(lines_out),
            "speaker_count": len(speakers_out),
            "source": "scan",
        },
        "lines": lines_out,
        "speakers": speakers_out,
    }


class W2SceneDialogMetadata:
    def __init__(self, data):
        self.data = data if isinstance(data, dict) else {}
        self.lines = self.data.get("lines", {}) if isinstance(self.data.get("lines", {}), dict) else {}
        self.speakers = self.data.get("speakers", {}) if isinstance(self.data.get("speakers", {}), dict) else {}
        self.stats = self.data.get("stats", {}) if isinstance(self.data.get("stats", {}), dict) else {}

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


def LoadWitcher2SceneDialogMetadata(do_reload=False, roots=None) -> W2SceneDialogMetadata:
    global _metadata_cache, _metadata_signature

    if not do_reload and roots is None and _metadata_cache is not None:
        if not (isinstance(_metadata_signature, tuple) and _metadata_signature[:1] == ("shipped",)):
            return _metadata_cache

    if not do_reload and roots is None:
        try:
            from .scene_dialog_index import LoadSceneDialogIndexMetadata

            shipped_metadata = LoadSceneDialogIndexMetadata("W2")
            if shipped_metadata is not None and shipped_metadata.available():
                _metadata_cache = shipped_metadata
                _metadata_signature = ("shipped", str(shipped_metadata.db_path))
                return shipped_metadata
        except Exception:
            log.debug("Failed to load shipped W2 scene dialogue index.", exc_info=True)

    scene_files = find_w2_scene_files(roots)
    signature = _signature(scene_files, roots=roots)
    if not do_reload and _metadata_cache is not None and _metadata_signature == signature:
        return _metadata_cache

    data = None if do_reload else load_cache(_cache_path(), _CACHE_VERSION, signature)
    if data is None:
        data = _build_metadata(scene_files, roots, signature)
        save_cache(_cache_path(), data)
        try:
            from .scene_dialog_index import SaveUserSceneDialogIndexMetadata

            SaveUserSceneDialogIndexMetadata("W2", data)
        except Exception:
            log.debug("Failed to write user W2 scene dialogue index.", exc_info=True)

    _metadata_cache = W2SceneDialogMetadata(data)
    _metadata_signature = signature
    return _metadata_cache


def ClearWitcher2SceneDialogMetadataCache():
    global _metadata_cache, _metadata_signature
    _metadata_cache = None
    _metadata_signature = None
