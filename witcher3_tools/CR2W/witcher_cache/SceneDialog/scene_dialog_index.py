from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

from ....extension_paths import get_cache_root

log = logging.getLogger(__name__)

_INDEX_VERSION = 6
_SUPPORTED_INDEX_VERSIONS = {1, 2, 3, 4, 5, 6}
_DB_BY_GAME = {
    "W2": Path("dialogue") / "w2" / "scene_dialog_index_v2.sqlite3",
    "W3": Path("dialogue") / "w3" / "scene_dialog_index_v2.sqlite3",
}
_USER_DB_BY_GAME = {
    "W2": "user_scene_dialog_index_v2.sqlite3",
    "W3": "user_scene_dialog_index_v2.sqlite3",
}
_metadata_cache = {}


def _data_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "data"


def _db_path(game: str) -> Path:
    rel_path = _DB_BY_GAME.get(str(game or "").upper())
    return _data_dir() / rel_path if rel_path else _data_dir()


def _user_db_path(game: str, *, create=False) -> Path:
    game = str(game or "").upper()
    cache_dir = Path(get_cache_root(create=create)) / "SceneDialog" / game.lower()
    if create:
        cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / _USER_DB_BY_GAME.get(game, "")


def _normalize_line_id(value, game: str) -> str:
    text = str(value or "").strip()
    game = str(game or "").upper()
    if game == "W2" and text.upper().startswith("VO_ID"):
        text = text[5:]
    if game in {"W2", "W3"} and text.isdigit():
        return str(int(text))
    return text


def _normalize_speaker(value) -> str:
    return str(value or "").strip().upper()


def _json_loads(value, fallback):
    if not value:
        return fallback
    try:
        loaded = json.loads(value)
    except Exception:
        return fallback
    return loaded if loaded is not None else fallback


class SceneDialogIndexMetadata:
    """Read-only shipped scene/dialogue lookup optimized for UI use."""

    def __init__(self, game: str, db_path: Path):
        self.game = str(game or "").upper()
        self.db_path = Path(db_path)
        self._conn = None
        self._set_cache = {}
        self._columns_cache = {}
        self._line_summary_cache = None
        self.index_version = 0
        self.stats = {}
        self.voice_tags = {}
        self.data = {}
        self.lines = {}
        self.speakers = {}

    def available(self) -> bool:
        return self.db_path.is_file()

    def _connect(self):
        if self._conn is not None:
            return self._conn
        if not self.available():
            return None
        try:
            uri = self.db_path.as_uri() + "?mode=ro&immutable=1"
            conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        except Exception:
            conn = sqlite3.connect(str(self.db_path), timeout=2.0)
            try:
                conn.execute("PRAGMA query_only = ON")
            except Exception:
                pass
        conn.row_factory = sqlite3.Row
        self._conn = conn
        self._load_meta()
        return conn

    def _load_meta(self) -> None:
        if self.stats:
            return
        try:
            rows = self._conn.execute("SELECT key, value FROM meta").fetchall()
        except Exception:
            rows = []
        meta = {str(row["key"]): str(row["value"]) for row in rows}
        try:
            version = int(meta.get("index_version") or 0)
        except Exception:
            version = 0
        if version and version not in _SUPPORTED_INDEX_VERSIONS:
            log.warning(
                "%s scene dialog index version mismatch: got %s, expected %s",
                self.game,
                version,
                _INDEX_VERSION,
            )
        self.index_version = version
        self.stats = _json_loads(meta.get("stats"), {}) or {}
        self.stats.setdefault("source", "shipped")
        self.stats.setdefault("db_path", str(self.db_path))

    def _columns(self, table: str):
        table = str(table or "")
        cached = self._columns_cache.get(table)
        if cached is not None:
            return cached
        conn = self._connect()
        if conn is None:
            return set()
        try:
            rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        except Exception:
            rows = []
        columns = {str(row["name"]) for row in rows}
        self._columns_cache[table] = columns
        return columns

    def _has_column(self, table: str, column: str) -> bool:
        return column in self._columns(table)

    def _get_set(self, kind: str, set_id):
        try:
            set_id = int(set_id)
        except Exception:
            return []
        if set_id <= 0:
            return []
        key = (kind, set_id)
        if key in self._set_cache:
            return self._set_cache[key]
        conn = self._connect()
        if conn is None:
            return []
        try:
            row = conn.execute(
                "SELECT json FROM sets WHERE kind = ? AND set_id = ?",
                (kind, set_id),
            ).fetchone()
        except Exception:
            row = None
        value = _json_loads(row["json"], []) if row else []
        if not isinstance(value, list):
            value = []
        self._set_cache[key] = value
        return value

    def _row_to_line(self, row, *, include_assoc: bool, include_scenes: bool = False) -> dict:
        if row is None:
            return {}
        row_keys = set(row.keys())
        result = {
            "speaker": str(row["speaker"] or ""),
            "scene_path": str(row["scene_path"] or ""),
            "entity_path": str(row["entity_path"] or ""),
        }
        voicetag = str(row["voicetag"] or "")
        speaker_code = str(row["speaker_code"] or "")
        if voicetag:
            result["voicetag"] = voicetag
        if speaker_code:
            result["id"] = speaker_code

        if "speakers_json" in row_keys:
            speakers = _json_loads(row["speakers_json"], [])
        else:
            speakers = self._get_set("speakers", row["speakers_set"])
        if speakers:
            result["speakers"] = speakers

        if include_assoc or include_scenes:
            if "source_scenes_json" in row_keys:
                source_scenes = _json_loads(row["source_scenes_json"], [])
            else:
                source_scenes = self._get_set("scenes", row["scenes_set"])
            if source_scenes:
                result["source_scenes"] = source_scenes
            elif result["scene_path"]:
                result["source_scenes"] = [result["scene_path"]]
        if include_assoc:
            if "entities_set" in row_keys:
                entities = self._get_set("entities", row["entities_set"])
                if entities:
                    result["entity_paths"] = entities
        return result

    def preload_line_summaries(self) -> dict:
        if self._line_summary_cache is not None:
            return self._line_summary_cache
        conn = self._connect()
        if conn is None:
            self._line_summary_cache = {}
            return self._line_summary_cache
        try:
            rows = conn.execute("SELECT * FROM lines").fetchall()
        except Exception:
            log.debug("Failed to read %s scene dialog summaries.", self.game, exc_info=True)
            rows = []
        self._line_summary_cache = {
            str(row["line_id"]): self._row_to_line(row, include_assoc=False, include_scenes=True)
            for row in rows
        }
        return self._line_summary_cache

    def get_line(self, line_id) -> dict:
        conn = self._connect()
        if conn is None:
            return {}
        line_key = _normalize_line_id(line_id, self.game)
        try:
            row = conn.execute("SELECT * FROM lines WHERE line_id = ?", (line_key,)).fetchone()
        except Exception:
            row = None
        return self._row_to_line(row, include_assoc=True)

    def get_speaker(self, speaker) -> dict:
        conn = self._connect()
        if conn is None:
            return {}
        speaker_key = _normalize_speaker(speaker)
        try:
            row = conn.execute("SELECT * FROM speakers WHERE speaker = ?", (speaker_key,)).fetchone()
        except Exception:
            row = None
        if row is None:
            return {}
        result = {
            "entity_path": str(row["entity_path"] or ""),
            "line_count": int(row["line_count"] or 0),
        }
        row_keys = set(row.keys())
        if "entities_set" in row_keys:
            entities = self._get_set("entities", row["entities_set"])
            if entities:
                result["entity_paths"] = entities
        if "voice_tag_json" in row_keys:
            voice_tag = _json_loads(row["voice_tag_json"], {})
            if isinstance(voice_tag, dict) and voice_tag:
                result["voice_tag"] = voice_tag
        if "voice_tag_candidates_json" in row_keys:
            voice_tag_candidates = _json_loads(row["voice_tag_candidates_json"], [])
            if isinstance(voice_tag_candidates, list) and voice_tag_candidates:
                result["voice_tag_candidates"] = voice_tag_candidates
        return result

    def resolve_line_speaker(self, line_id) -> str:
        return str(self.get_line(line_id).get("speaker", "") or "").strip().upper()

    def resolve_line_entity(self, line_id, speaker="") -> str:
        line = self.get_line(line_id)
        return str(line.get("entity_path", "") or "").strip()


class CompositeSceneDialogIndexMetadata:
    """Read user scene metadata first, then fall back to the shipped index."""

    def __init__(self, game: str, indexes):
        self.game = str(game or "").upper()
        self.indexes = [idx for idx in indexes if idx is not None and idx.available()]
        self.db_path = ";".join(str(idx.db_path) for idx in self.indexes)
        self.data = {}
        self.lines = {}
        self.speakers = {}
        self.voice_tags = {}
        self.stats = {"source": "composite", "db_path": self.db_path}

    def available(self) -> bool:
        return bool(self.indexes)

    def preload_line_summaries(self) -> dict:
        merged = {}
        for index in reversed(self.indexes):
            try:
                merged.update(index.preload_line_summaries() or {})
            except Exception:
                log.debug("Failed to preload scene dialogue overlay.", exc_info=True)
        return merged

    def get_line(self, line_id) -> dict:
        for index in self.indexes:
            info = index.get_line(line_id)
            if info:
                return info
        return {}

    def get_speaker(self, speaker) -> dict:
        merged = {}
        for index in reversed(self.indexes):
            info = index.get_speaker(speaker)
            if info:
                merged.update(info)
        return merged

    def resolve_line_speaker(self, line_id) -> str:
        return str(self.get_line(line_id).get("speaker", "") or "").strip().upper()

    def resolve_line_entity(self, line_id, speaker="") -> str:
        line = self.get_line(line_id)
        return str(line.get("entity_path", "") or "").strip()


def _json_dump(value):
    if not value:
        return ""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class _SetTable:
    def __init__(self):
        self._by_json = {}
        self.rows = []

    def id_for(self, kind, value):
        if not value:
            return 0
        payload = _json_dump(value)
        key = (kind, payload)
        existing = self._by_json.get(key)
        if existing is not None:
            return existing
        set_id = len(self.rows) + 1
        self._by_json[key] = set_id
        self.rows.append((kind, set_id, payload))
        return set_id


def _compact_speakers(speakers):
    result = []
    for speaker in (speakers or [])[:4]:
        if not isinstance(speaker, dict):
            continue
        name = str(speaker.get("name", "") or speaker.get("voicetag", "") or "").strip().upper()
        if not name:
            continue
        out = {"name": name}
        for key in ("display", "voicetag", "id"):
            value = str(speaker.get(key, "") or "").strip()
            if value:
                out[key] = value
        count = speaker.get("count", speaker.get("score", ""))
        if count != "":
            out["count"] = int(count) if isinstance(count, int) or str(count).isdigit() else count
        result.append(out)
    return result


def _compact_entity_paths(entities, preferred_path="", max_count=128):
    result = []
    seen = set()

    def add(entity):
        if isinstance(entity, dict):
            path = str(entity.get("path", "") or "").strip().replace("/", "\\")
            appearance = str(entity.get("appearance", "") or "").strip()
            source = str(entity.get("source", "") or "").strip()
            count = entity.get("count", "")
        else:
            path = str(entity or "").strip().replace("/", "\\")
            appearance = ""
            source = ""
            count = ""
        if not path:
            return
        key = (path.lower(), appearance.lower())
        if key in seen:
            return
        seen.add(key)
        item = {"path": path}
        if appearance:
            item["appearance"] = appearance
        if source:
            item["source"] = source
        if count != "":
            try:
                item["count"] = int(count)
            except Exception:
                item["count"] = count
        result.append(item)

    preferred_path = str(preferred_path or "").strip().replace("/", "\\")
    preferred_added = False
    if preferred_path:
        for entity in entities or []:
            path = str(entity.get("path", "") if isinstance(entity, dict) else entity or "").strip().replace("/", "\\")
            if path.lower() == preferred_path.lower():
                add(entity)
                preferred_added = True
                break
        if not preferred_added:
            add({"path": preferred_path})
    for entity in entities or []:
        add(entity)
        if len(result) >= max_count:
            break
    return result[:max_count]


def _preferred_entity_path(game, speaker, entity_path, entities):
    game = str(game or "").upper()
    speaker = str(speaker or "").strip().upper()
    entity_path = str(entity_path or "").strip().replace("/", "\\")
    if game == "W2" and speaker == "GERALT":
        for entity in entities or []:
            path = str(entity.get("path", "") if isinstance(entity, dict) else entity or "").strip().replace("/", "\\")
            if path.lower() == "characters\\templates\\witcher\\player.w2ent":
                return path
    if entity_path:
        return entity_path
    if game == "W3":
        return ""
    paths = [str(entity.get("path", "") if isinstance(entity, dict) else entity or "").strip().replace("/", "\\") for entity in (entities or [])]
    paths = [path for path in paths if path]
    return paths[0] if paths else ""


def SaveUserSceneDialogIndexMetadata(game: str, source: dict) -> str:
    """Write scanned user scene metadata to a user-cache SQLite overlay."""
    game = str(game or "").upper()
    if game not in _USER_DB_BY_GAME or not isinstance(source, dict):
        return ""

    output_db = _user_db_path(game, create=True)
    tmp_db = output_db.with_suffix(output_db.suffix + ".tmp")
    if tmp_db.exists():
        tmp_db.unlink()

    sets = _SetTable()
    conn = sqlite3.connect(str(tmp_db))
    try:
        conn.execute("PRAGMA journal_mode = OFF")
        conn.execute("PRAGMA synchronous = OFF")
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute(
            "CREATE TABLE sets ("
            "kind TEXT NOT NULL, "
            "set_id INTEGER NOT NULL, "
            "json TEXT NOT NULL, "
            "PRIMARY KEY (kind, set_id)"
            ")"
        )
        conn.execute(
            "CREATE TABLE lines ("
            "line_id TEXT PRIMARY KEY, "
            "speaker TEXT NOT NULL DEFAULT '', "
            "scene_path TEXT NOT NULL DEFAULT '', "
            "entity_path TEXT NOT NULL DEFAULT '', "
            "voicetag TEXT NOT NULL DEFAULT '', "
            "speaker_code TEXT NOT NULL DEFAULT '', "
            "speakers_set INTEGER NOT NULL DEFAULT 0, "
            "scenes_set INTEGER NOT NULL DEFAULT 0, "
            "entities_set INTEGER NOT NULL DEFAULT 0"
            ")"
        )
        conn.execute(
            "CREATE TABLE speakers ("
            "speaker TEXT PRIMARY KEY, "
            "entity_path TEXT NOT NULL DEFAULT '', "
            "line_count INTEGER NOT NULL DEFAULT 0, "
            "voice_tag_json TEXT NOT NULL DEFAULT ''"
            ")"
        )

        line_rows = []
        for line_id, info in sorted((source.get("lines") or {}).items(), key=lambda item: str(item[0])):
            if not isinstance(info, dict):
                continue
            speaker = str(info.get("speaker", "") or "").strip().upper()
            scene_path = str(info.get("scene_path", "") or "").replace("/", "\\")
            source_scenes = [str(path or "").replace("/", "\\") for path in (info.get("source_scenes") or []) if path]
            if scene_path and scene_path not in source_scenes:
                source_scenes.insert(0, scene_path)
            entity_path = _preferred_entity_path(game, speaker, info.get("entity_path", ""), info.get("entity_paths"))
            line_rows.append((
                str(line_id),
                speaker,
                scene_path,
                entity_path,
                str(info.get("voicetag", "") or ""),
                str(info.get("id", "") or ""),
                sets.id_for("speakers", _compact_speakers(info.get("speakers"))),
                sets.id_for("scenes", source_scenes[:5]),
                sets.id_for("entities", _compact_entity_paths(info.get("entity_paths"), entity_path)),
            ))

        speaker_rows = []
        for speaker, info in sorted((source.get("speakers") or {}).items(), key=lambda item: str(item[0])):
            if not isinstance(info, dict):
                continue
            speaker_key = str(speaker or "").strip().upper()
            entity_path = _preferred_entity_path(
                game,
                speaker_key,
                info.get("entity_path", ""),
                info.get("entity_paths"),
            )
            voice_tag = info.get("voice_tag", {}) if isinstance(info.get("voice_tag", {}), dict) else {}
            speaker_rows.append((
                speaker_key,
                entity_path,
                int(info.get("line_count", 0) or 0),
                _json_dump(voice_tag),
            ))

        conn.executemany("INSERT INTO sets (kind, set_id, json) VALUES (?, ?, ?)", sets.rows)
        conn.executemany(
            "INSERT INTO lines (line_id, speaker, scene_path, entity_path, voicetag, speaker_code, speakers_set, scenes_set, entities_set) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            line_rows,
        )
        conn.executemany(
            "INSERT INTO speakers (speaker, entity_path, line_count, voice_tag_json) VALUES (?, ?, ?, ?)",
            speaker_rows,
        )
        conn.execute("CREATE INDEX lines_speaker_idx ON lines (speaker)")
        stats = dict(source.get("stats", {}) or {})
        stats.update({
            "index_version": _INDEX_VERSION,
            "index_line_count": len(line_rows),
            "index_speaker_count": len(speaker_rows),
            "index_set_count": len(sets.rows),
            "index_schema": "compact",
            "index_has_scene_entities": True,
            "source": "user",
        })
        conn.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            [
                ("index_version", str(_INDEX_VERSION)),
                ("game", game),
                ("stats", _json_dump(stats)),
            ],
        )
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()

    if output_db.exists():
        output_db.unlink()
    tmp_db.replace(output_db)
    _metadata_cache.pop(game, None)
    log.info("Wrote user %s scene dialogue index: %s", game, output_db)
    return str(output_db)


def ClearUserSceneDialogIndexMetadata(game: str) -> bool:
    game = str(game or "").upper()
    if game not in _USER_DB_BY_GAME:
        return False
    path = _user_db_path(game)
    removed = False
    try:
        if path.exists():
            path.unlink()
            removed = True
    except Exception:
        log.warning("Failed to remove user %s scene dialogue index: %s", game, path, exc_info=True)
        return False
    _metadata_cache.pop(game, None)
    return removed


def LoadSceneDialogIndexMetadata(game: str):
    game = str(game or "").upper()
    if game not in _DB_BY_GAME:
        return None
    cached = _metadata_cache.get(game)
    if cached is not None:
        return cached
    indexes = []
    user_metadata = SceneDialogIndexMetadata(game, _user_db_path(game))
    shipped_metadata = SceneDialogIndexMetadata(game, _db_path(game))
    if user_metadata.available():
        try:
            user_metadata._connect()
        except Exception:
            log.debug("Failed to inspect user %s scene dialogue index.", game, exc_info=True)
        if int(getattr(user_metadata, "index_version", 0) or 0) >= _INDEX_VERSION:
            indexes.append(user_metadata)
        else:
            log.info(
                "Ignoring older user %s scene dialogue index version %s; expected %s.",
                game,
                getattr(user_metadata, "index_version", 0),
                _INDEX_VERSION,
            )
    if shipped_metadata.available():
        indexes.append(shipped_metadata)
    if not indexes:
        return None
    metadata = indexes[0] if len(indexes) == 1 else CompositeSceneDialogIndexMetadata(game, indexes)
    _metadata_cache[game] = metadata
    return metadata
