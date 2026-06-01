from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "witcher3_tools" / "CR2W" / "data"
DIALOGUE_DATA_DIR = DATA_DIR / "dialogue"
INDEX_VERSION = 6


def _json_dump(value):
    if not value:
        return ""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class SetTable:
    def __init__(self):
        self._by_json = {}
        self._rows = []

    def id_for(self, kind, value):
        if not value:
            return 0
        payload = _json_dump(value)
        key = (kind, payload)
        existing = self._by_json.get(key)
        if existing is not None:
            return existing
        set_id = len(self._rows) + 1
        self._by_json[key] = set_id
        self._rows.append((kind, set_id, payload))
        return set_id

    @property
    def rows(self):
        return self._rows


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


def build_index(game: str, source_json: Path, output_db: Path) -> None:
    game = game.upper()
    with source_json.open("r", encoding="utf-8") as fh:
        source = json.load(fh)

    output_db.parent.mkdir(parents=True, exist_ok=True)
    tmp_db = output_db.with_suffix(output_db.suffix + ".tmp")
    if tmp_db.exists():
        tmp_db.unlink()

    sets = SetTable()
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
        for line_id, info in sorted((source.get("lines") or {}).items(), key=lambda item: item[0]):
            if not isinstance(info, dict):
                continue
            speaker = str(info.get("speaker", "") or "").strip().upper()
            scene_path = str(info.get("scene_path", "") or "").replace("/", "\\")
            source_scenes = [str(path or "").replace("/", "\\") for path in (info.get("source_scenes") or []) if path]
            if scene_path and scene_path not in source_scenes:
                source_scenes.insert(0, scene_path)
            entity_path = _preferred_entity_path(
                game,
                speaker,
                info.get("entity_path", ""),
                info.get("entity_paths"),
            )
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
        for speaker, info in sorted((source.get("speakers") or {}).items(), key=lambda item: item[0]):
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
        stats.pop("entity_count", None)
        stats.pop("entity_parse_errors", None)
        stats.update({
            "index_version": INDEX_VERSION,
            "index_line_count": len(line_rows),
            "index_speaker_count": len(speaker_rows),
            "index_set_count": len(sets.rows),
            "index_schema": "compact",
            "index_has_scene_entities": True,
        })
        conn.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            [
                ("index_version", str(INDEX_VERSION)),
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
    print(f"Wrote {output_db} ({output_db.stat().st_size:,} bytes)")


def main():
    parser = argparse.ArgumentParser(description="Build shipped scene/dialogue SQLite lookup indexes.")
    parser.add_argument("--w3", type=Path, help="Path to full W3 w3_scene_dialog_metadata.json")
    parser.add_argument("--w2", type=Path, help="Path to full W2 w2_scene_dialog_metadata.json")
    parser.add_argument("--out-dir", type=Path, default=DIALOGUE_DATA_DIR)
    args = parser.parse_args()

    if args.w3:
        build_index("W3", args.w3, args.out_dir / "w3" / "scene_dialog_index_v2.sqlite3")
    if args.w2:
        build_index("W2", args.w2, args.out_dir / "w2" / "scene_dialog_index_v2.sqlite3")
    if not args.w3 and not args.w2:
        parser.error("Pass at least --w3 or --w2")


if __name__ == "__main__":
    main()
