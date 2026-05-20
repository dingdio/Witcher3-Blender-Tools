import csv
import json
import os
import re
import shutil
import subprocess
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path


MAX_RADISH_LINE_ID = 2147483647
PROJECT_STRINGS_CSV = "LocalEditorStringDataBaseW3_UTF8_mod_export.csv"
PROJECT_BACKUP_DIR = "RedkitFixer_backups"
LIPSYNC_BACKUP_DIR = "witcher_blender_lipsync"
DEFAULT_LIPSYNC_RESOURCE = 'CStoryScene "witcher_blender_tools_lipsync.w2scene"'
PROJECT_STRING_COLUMNS = (
    "ID",
    "RESOURCE",
    "PROPERTY",
    "VOICEOVER",
    "KEY",
    "BR",
    "CZ",
    "RU",
    "AR",
    "TR",
    "CN",
    "PL",
    "IT",
    "FR",
    "DE",
    "ZH",
    "ESMX",
    "EN",
    "KR",
    "ES",
    "JP",
    "HU",
)


@dataclass(frozen=True)
class ProjectIdInfo:
    project_path: Path
    metadata_path: Path
    id_space: int
    next_line_id: int
    used_count: int

    @property
    def project_name(self):
        return self.project_path.name


@dataclass(frozen=True)
class ProjectLineAssets:
    has_wav: bool = False
    has_wem: bool = False
    has_re: bool = False
    wav_is_silent: bool = False
    wav_duration: float = 0.0
    wav_path: Path = None
    wem_path: Path = None
    re_path: Path = None


@dataclass(frozen=True)
class ProjectVoiceLine:
    project_path: Path
    csv_path: Path
    line_id: str
    text: str
    speaker: str
    language: str
    voiceover: str
    resource: str
    property_name: str
    key: str
    row_index: int
    assets: ProjectLineAssets = field(default_factory=ProjectLineAssets)


@dataclass(frozen=True)
class ProjectLineUpdateResult:
    csv_changed: bool = False
    scenes_scanned: int = 0
    scenes_changed: int = 0
    assets_renamed: int = 0
    backup_dir: Path = None
    skipped_files: tuple = ()


@dataclass(frozen=True)
class ProjectValidationResult:
    duplicate_ids: tuple = ()
    duplicate_voiceovers: tuple = ()
    invalid_ids: tuple = ()
    empty_voiceover_lines: tuple = ()

    @property
    def error_count(self):
        return (
            len(self.duplicate_ids)
            + len(self.duplicate_voiceovers)
            + len(self.invalid_ids)
        )

    @property
    def warning_count(self):
        return len(self.empty_voiceover_lines)

    @property
    def is_valid(self):
        return self.error_count == 0

    def compact_message(self):
        parts = []
        if self.duplicate_ids:
            parts.append(f"duplicate IDs: {len(self.duplicate_ids)}")
        if self.duplicate_voiceovers:
            parts.append(f"duplicate voiceovers: {len(self.duplicate_voiceovers)}")
        if self.invalid_ids:
            parts.append(f"invalid IDs: {len(self.invalid_ids)}")
        if self.empty_voiceover_lines:
            parts.append(f"unvoiced rows: {len(self.empty_voiceover_lines)}")
        return "; ".join(parts) if parts else "No duplicate IDs or voiceovers found."


def get_active_project_path(context):
    try:
        from .. import get_all_addon_prefs

        prefs = get_all_addon_prefs(context)
    except Exception:
        return None

    projects = getattr(prefs, "redkit_projects", [])
    index = int(getattr(prefs, "redkit_projects_index", 0) or 0)
    if projects and 0 <= index < len(projects):
        path = str(getattr(projects[index], "path", "") or "").strip()
        if path:
            try:
                import bpy

                path = bpy.path.abspath(path)
            except Exception:
                pass
            return Path(os.path.normpath(path))
    return None


def set_active_project_index(context, index):
    try:
        from .. import get_all_addon_prefs

        prefs = get_all_addon_prefs(context)
    except Exception:
        return False

    projects = getattr(prefs, "redkit_projects", [])
    try:
        index = int(index)
    except (TypeError, ValueError):
        return False
    if projects and 0 <= index < len(projects):
        prefs.redkit_projects_index = index
        return True
    return False


def iter_project_paths(context):
    try:
        from .. import get_all_addon_prefs

        prefs = get_all_addon_prefs(context)
    except Exception:
        return []

    items = []
    for index, item in enumerate(getattr(prefs, "redkit_projects", []) or []):
        path = str(getattr(item, "path", "") or "").strip()
        if not path:
            continue
        try:
            import bpy

            path = bpy.path.abspath(path)
        except Exception:
            pass
        items.append((index, Path(os.path.normpath(path))))
    return items


def _json_int(data, key):
    try:
        return int(data.get(key))
    except (TypeError, ValueError):
        return None


def _language_column(language):
    language = str(language or "en").strip().upper()
    return language if language in PROJECT_STRING_COLUMNS else "EN"


def _speaker_from_voiceover(voiceover, line_id):
    voiceover = str(voiceover or "").strip()
    line_id = str(line_id or "").strip()
    if not voiceover:
        return ""
    suffix = f"_{line_id}"
    if line_id and voiceover.upper().endswith(suffix.upper()):
        return voiceover[:-len(suffix)]
    parts = voiceover.rsplit("_", 2)
    if len(parts) == 3 and parts[-1].isdigit():
        return parts[0]
    return voiceover


def voiceover_name(speaker, line_id):
    speaker = str(speaker or "").strip().upper()
    speaker = re.sub(r"[^A-Z0-9_]+", "_", speaker)
    line_id = re.sub(r"\D+", "", str(line_id or ""))
    return f"{speaker or 'GRLT'}_{line_id}"


def _matching_stems(voiceover, speaker, line_id):
    stems = []
    for value in (
        voiceover,
        voiceover_name(speaker, line_id),
        f"{str(speaker or '').strip()}_{str(line_id or '').strip()}",
    ):
        value = str(value or "").strip()
        if value and value not in stems:
            stems.append(value)
    return stems


def _first_file_in(folder, stems, suffix, line_id):
    folder = Path(folder)
    suffix = suffix.lower()
    for stem in stems:
        candidate = folder / f"{stem}{suffix}"
        if candidate.is_file():
            return candidate

    line_id = str(line_id or "").strip()
    if line_id and folder.is_dir():
        for pattern in (f"*_{line_id}{suffix}", f"*{line_id}{suffix}"):
            matches = sorted(path for path in folder.glob(pattern) if path.is_file())
            if matches:
                return matches[0]
    return None


def wav_duration_seconds(path):
    if not path:
        return 0.0
    try:
        with wave.open(str(path), "rb") as handle:
            frame_rate = handle.getframerate()
            if frame_rate <= 0:
                return 0.0
            return float(handle.getnframes()) / float(frame_rate)
    except Exception:
        return 0.0


def _is_silent_wav(path, chunk_frames=8192):
    if not path:
        return False
    try:
        with wave.open(str(path), "rb") as handle:
            if handle.getnframes() <= 0:
                return True
            while True:
                frames = handle.readframes(chunk_frames)
                if not frames:
                    return True
                if any(frames):
                    return False
    except Exception:
        return False


def find_project_line_assets(project_path, language, line_id, voiceover="", speaker=""):
    project_path = Path(project_path)
    language = str(language or "en").strip().lower() or "en"
    stems = _matching_stems(voiceover, speaker, line_id)
    speech_root = project_path / "speech" / language

    wav_path = _first_file_in(speech_root / "audio_original", stems, ".wav", line_id)
    if wav_path is None:
        wav_path = _first_file_in(speech_root / "audio", stems, ".wav", line_id)
    wem_path = _first_file_in(speech_root / "audio", stems, ".wem", line_id)
    re_path = _first_file_in(speech_root / "lipsync", stems, ".re", line_id)
    return ProjectLineAssets(
        has_wav=wav_path is not None,
        has_wem=wem_path is not None,
        has_re=re_path is not None,
        wav_is_silent=_is_silent_wav(wav_path) if wav_path is not None else False,
        wav_duration=wav_duration_seconds(wav_path) if wav_path is not None else 0.0,
        wav_path=wav_path,
        wem_path=wem_path,
        re_path=re_path,
    )


def read_project_voice_lines(project_path, language="en", include_unvoiced=False):
    project_path = Path(project_path)
    csv_path = project_path / PROJECT_STRINGS_CSV
    if not csv_path.is_file():
        raise FileNotFoundError(f"REDkit strings CSV not found: {csv_path}")

    language = str(language or "en").strip().lower() or "en"
    lang_column = _language_column(language)
    lines = []
    with open(csv_path, "r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=";")
        for row_index, row in enumerate(reader):
            line_id = str(row.get("ID", "") or "").strip()
            if not re.match(r"^\d+$", line_id):
                continue

            voiceover = str(row.get("VOICEOVER", "") or "").strip()
            if not voiceover and not include_unvoiced:
                continue

            text = str(row.get(lang_column, "") or row.get("EN", "") or "").strip()
            if not text and not include_unvoiced:
                continue

            speaker = _speaker_from_voiceover(voiceover, line_id)
            assets = find_project_line_assets(project_path, language, line_id, voiceover, speaker)
            lines.append(ProjectVoiceLine(
                project_path=project_path,
                csv_path=csv_path,
                line_id=line_id,
                text=text,
                speaker=speaker,
                language=language,
                voiceover=voiceover,
                resource=str(row.get("RESOURCE", "") or "").strip(),
                property_name=str(row.get("PROPERTY", "") or "").strip(),
                key=str(row.get("KEY", "") or "").strip(),
                row_index=row_index,
                assets=assets,
            ))
    return lines


def find_project_voice_line(project_path, line_id, language="en", include_unvoiced=True):
    line_id = str(line_id or "").strip()
    if not line_id:
        return None
    for project_line in read_project_voice_lines(project_path, language=language, include_unvoiced=include_unvoiced):
        if str(project_line.line_id or "").strip() == line_id:
            return project_line
    return None


def validate_project_voice_lines(project_path):
    project_path = Path(project_path)
    csv_path = project_path / PROJECT_STRINGS_CSV
    if not csv_path.is_file():
        raise FileNotFoundError(f"REDkit strings CSV not found: {csv_path}")

    id_rows = {}
    voiceover_rows = {}
    invalid_ids = []
    empty_voiceover_lines = []
    with open(csv_path, "r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=";")
        for row_number, row in enumerate(reader, start=2):
            line_id = str(row.get("ID", "") or "").strip()
            voiceover = str(row.get("VOICEOVER", "") or "").strip()
            if not re.match(r"^\d+$", line_id):
                invalid_ids.append(f"row {row_number}: {line_id or '<empty>'}")
            else:
                id_rows.setdefault(line_id, []).append(row_number)
            if voiceover:
                voiceover_rows.setdefault(voiceover.upper(), []).append(row_number)
            elif str(row.get("PROPERTY", "") or "").strip().lower() == "line text":
                empty_voiceover_lines.append(f"row {row_number}: {line_id or '<empty>'}")

    duplicate_ids = tuple(
        f"{line_id}: rows {', '.join(str(row) for row in rows)}"
        for line_id, rows in sorted(id_rows.items())
        if len(rows) > 1
    )
    duplicate_voiceovers = tuple(
        f"{voiceover}: rows {', '.join(str(row) for row in rows)}"
        for voiceover, rows in sorted(voiceover_rows.items())
        if len(rows) > 1
    )
    return ProjectValidationResult(
        duplicate_ids=duplicate_ids,
        duplicate_voiceovers=duplicate_voiceovers,
        invalid_ids=tuple(invalid_ids),
        empty_voiceover_lines=tuple(empty_voiceover_lines),
    )


def _iter_metadata_candidates(project_path):
    project_path = Path(project_path)
    yield from sorted(project_path.glob("*.w3edit"))
    yield from sorted(project_path.glob("packed/mods/*/content/info.json"))


def read_project_id_space(project_path):
    for candidate in _iter_metadata_candidates(project_path):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        id_space = _json_int(data, "idSpace")
        if id_space is not None:
            return id_space, candidate
    return None, None


def read_project_string_ids(project_path):
    csv_path = Path(project_path) / PROJECT_STRINGS_CSV
    if not csv_path.is_file():
        return set()

    ids = set()
    with open(csv_path, "r", encoding="utf-8", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=";")
        for row in reader:
            raw_id = str(row.get("ID", "") or "").strip()
            if not raw_id:
                continue
            match = re.match(r"^\d+$", raw_id)
            if not match:
                continue
            try:
                ids.add(int(raw_id))
            except ValueError:
                continue
    return ids


def next_project_line_id(project_path):
    project_path = Path(project_path)
    id_space, metadata_path = read_project_id_space(project_path)
    if id_space is None:
        return None
    if id_space > MAX_RADISH_LINE_ID:
        return None

    used_ids = {
        value for value in read_project_string_ids(project_path)
        if id_space <= value <= MAX_RADISH_LINE_ID
    }
    next_id = (max(used_ids) + 1) if used_ids else (id_space + 1)
    if next_id > MAX_RADISH_LINE_ID:
        return None

    return ProjectIdInfo(
        project_path=project_path,
        metadata_path=metadata_path,
        id_space=id_space,
        next_line_id=next_id,
        used_count=len(used_ids),
    )


def get_active_project_id_info(context):
    project_path = get_active_project_path(context)
    if not project_path:
        return None
    return next_project_line_id(project_path)


def _normalize_line_id(line_id):
    return re.sub(r"\D+", "", str(line_id or ""))


def _replace_line_id_suffix(value, old_id, new_id):
    value = str(value or "").strip()
    old_id = str(old_id or "").strip()
    new_id = str(new_id or "").strip()
    if not value or not old_id or not new_id:
        return value
    return re.sub(rf"(?<!\d){re.escape(old_id)}(?!\d)", new_id, value)


def _backup_file(project_path, backup_dir, path):
    project_path = Path(project_path)
    backup_dir = Path(backup_dir)
    path = Path(path)
    try:
        relative = path.relative_to(project_path)
    except ValueError:
        relative = Path(path.name)
    target = backup_dir / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and not target.exists():
        shutil.copy2(path, target)
    return target


def _make_backup_dir(project_path):
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    backup_dir = Path(project_path) / PROJECT_BACKUP_DIR / LIPSYNC_BACKUP_DIR / timestamp
    backup_dir.mkdir(parents=True, exist_ok=True)
    return backup_dir


def update_project_line_csv(project_path, old_id, new_id, text=None, speaker=None, language="en", backup_dir=None):
    project_path = Path(project_path)
    csv_path = project_path / PROJECT_STRINGS_CSV
    if not csv_path.is_file():
        raise FileNotFoundError(f"REDkit strings CSV not found: {csv_path}")

    old_id = _normalize_line_id(old_id)
    new_id = _normalize_line_id(new_id)
    if not old_id or not new_id:
        raise ValueError("Both old and new line IDs must be numeric.")
    if int(new_id) > MAX_RADISH_LINE_ID:
        raise ValueError(f"Line ID must be {MAX_RADISH_LINE_ID} or lower.")

    if backup_dir is not None:
        _backup_file(project_path, backup_dir, csv_path)

    language_column = _language_column(language)
    new_voiceover = ""
    changed = False
    rows = []
    with open(csv_path, "r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=";")
        fieldnames = list(reader.fieldnames or PROJECT_STRING_COLUMNS)
        for row in reader:
            if str(row.get("ID", "") or "").strip() != old_id:
                rows.append(row)
                continue

            row["ID"] = new_id
            if str(row.get("KEY", "") or "").strip() == old_id:
                row["KEY"] = new_id

            current_voiceover = str(row.get("VOICEOVER", "") or "").strip()
            if current_voiceover:
                new_voiceover = voiceover_name(speaker or _speaker_from_voiceover(current_voiceover, old_id), new_id)
                row["VOICEOVER"] = new_voiceover

            if text is not None and language_column in fieldnames:
                row[language_column] = str(text or "")
            changed = True
            rows.append(row)

    if not changed:
        raise ValueError(f"String ID {old_id} was not found in {csv_path.name}.")

    proposed_id_count = sum(1 for row in rows if str(row.get("ID", "") or "").strip() == new_id)
    if proposed_id_count > 1:
        raise ValueError(f"Project already has string ID {new_id}.")
    if new_voiceover:
        proposed_voice_count = sum(
            1 for row in rows
            if str(row.get("VOICEOVER", "") or "").strip().upper() == new_voiceover.upper()
        )
        if proposed_voice_count > 1:
            raise ValueError(f"Project already has voiceover {new_voiceover}.")

    validation = validate_project_voice_lines(project_path)
    if validation.duplicate_ids or validation.duplicate_voiceovers or validation.invalid_ids:
        raise ValueError(f"Project string CSV validation failed: {validation.compact_message()}")

    with open(csv_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter=";", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})

    return changed


def add_project_line(project_path, line_id, text, speaker, language="en", resource="", property_name="Line text", key=None):
    project_path = Path(project_path)
    csv_path = project_path / PROJECT_STRINGS_CSV
    if not csv_path.is_file():
        raise FileNotFoundError(f"REDkit strings CSV not found: {csv_path}")

    line_id = _normalize_line_id(line_id)
    if not line_id:
        raise ValueError("Line ID must be numeric.")
    if int(line_id) > MAX_RADISH_LINE_ID:
        raise ValueError(f"Line ID must be {MAX_RADISH_LINE_ID} or lower.")

    language = str(language or "en").strip().lower() or "en"
    language_column = _language_column(language)
    speaker = str(speaker or "").strip()
    voiceover = voiceover_name(speaker, line_id)
    resource = str(resource or DEFAULT_LIPSYNC_RESOURCE)
    property_name = str(property_name or "Line text")
    key = str(line_id if key is None else key)

    rows = []
    with open(csv_path, "r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=";")
        fieldnames = list(reader.fieldnames or PROJECT_STRING_COLUMNS)
        for row in reader:
            rows.append(row)

    for column in PROJECT_STRING_COLUMNS:
        if column not in fieldnames:
            fieldnames.append(column)

    if any(str(row.get("ID", "") or "").strip() == line_id for row in rows):
        raise ValueError(f"Project already has string ID {line_id}.")
    if voiceover and any(str(row.get("VOICEOVER", "") or "").strip().upper() == voiceover.upper() for row in rows):
        raise ValueError(f"Project already has voiceover {voiceover}.")

    row = {column: "" for column in fieldnames}
    row["ID"] = line_id
    row["RESOURCE"] = resource
    row["PROPERTY"] = property_name
    row["VOICEOVER"] = voiceover
    row["KEY"] = key
    if language_column in fieldnames:
        row[language_column] = str(text or "")

    backup_dir = _make_backup_dir(project_path)
    _backup_file(project_path, backup_dir, csv_path)
    rows.append(row)

    with open(csv_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter=";", lineterminator="\n")
        writer.writeheader()
        for item in rows:
            writer.writerow({field: item.get(field, "") for field in fieldnames})

    assets = find_project_line_assets(project_path, language, line_id, voiceover, speaker)
    return ProjectVoiceLine(
        project_path=project_path,
        csv_path=csv_path,
        line_id=line_id,
        text=str(text or ""),
        speaker=_speaker_from_voiceover(voiceover, line_id),
        language=language,
        voiceover=voiceover,
        resource=resource,
        property_name=property_name,
        key=key,
        row_index=len(rows) - 1,
        assets=assets,
    )


def rename_project_line_assets(project_path, old_id, new_id, language="en", old_voiceover="", new_voiceover="", backup_dir=None):
    project_path = Path(project_path)
    old_id = _normalize_line_id(old_id)
    new_id = _normalize_line_id(new_id)
    language = str(language or "en").strip().lower() or "en"
    old_voiceover = str(old_voiceover or "").strip()
    new_voiceover = str(new_voiceover or "").strip()
    if not old_voiceover:
        old_voiceover = f"*_{old_id}"
    if not new_voiceover:
        new_voiceover = _replace_line_id_suffix(old_voiceover, old_id, new_id)

    renamed = 0
    speech_root = project_path / "speech" / language
    folders = (
        (speech_root / "audio", (".wem", ".wav")),
        (speech_root / "audio_original", (".wav",)),
        (speech_root / "lipsync", (".re",)),
    )
    for folder, suffixes in folders:
        if not folder.is_dir():
            continue
        candidates = []
        if "*" not in old_voiceover:
            candidates.extend(folder / f"{old_voiceover}{suffix}" for suffix in suffixes)
        for suffix in suffixes:
            candidates.extend(sorted(folder.glob(f"*_{old_id}{suffix}")))

        seen = set()
        for source in candidates:
            key = str(source).lower()
            if key in seen or not source.is_file():
                continue
            seen.add(key)
            if source.stem == old_voiceover:
                target_stem = new_voiceover
            else:
                target_stem = _replace_line_id_suffix(source.stem, old_id, new_id)
            target = source.with_name(f"{target_stem}{source.suffix}")
            if target == source:
                continue
            if target.exists():
                continue
            if backup_dir is not None:
                _backup_file(project_path, backup_dir, source)
            source.rename(target)
            renamed += 1
    return renamed


def _replace_voice_path_value(value, old_id, new_id, old_voiceover, new_voiceover):
    text = str(value or "")
    if not text:
        return text

    normalized = text.replace("/", "\\")
    filename = normalized.rsplit("\\", 1)[-1]
    stem, dot, suffix = filename.partition(".")
    if old_voiceover and stem == old_voiceover:
        new_filename = f"{new_voiceover}{dot}{suffix}" if dot else new_voiceover
        return text[:len(text) - len(filename)] + new_filename
    if old_id and re.search(rf"(?<!\d){re.escape(old_id)}(?!\d)", stem):
        new_stem = _replace_line_id_suffix(stem, old_id, new_id)
        new_filename = f"{new_stem}{dot}{suffix}" if dot else new_stem
        return text[:len(text) - len(filename)] + new_filename
    return text


def _mutate_scene_json_value(value, field_name, old_id, new_id, old_voiceover, new_voiceover):
    if isinstance(value, dict):
        changed = False
        if str(value.get("_type", "") or "") == "LocalizedString":
            raw = str(value.get("_value", "") or "").strip()
            if raw == old_id:
                value["_value"] = new_id
                changed = True
        for key, child in list(value.items()):
            if key == "_value" and field_name in {"voiceFileName", "overriddenLipsyncFilePath", "overriddenAudioFilePath"}:
                updated = _replace_voice_path_value(child, old_id, new_id, old_voiceover, new_voiceover)
                if updated != child:
                    value[key] = updated
                    changed = True
            elif _mutate_scene_json_value(child, key, old_id, new_id, old_voiceover, new_voiceover):
                changed = True
        return changed

    if isinstance(value, list):
        changed = False
        for child in value:
            if _mutate_scene_json_value(child, field_name, old_id, new_id, old_voiceover, new_voiceover):
                changed = True
        return changed

    return False


def _run_wolvenkit(command, timeout=180):
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        creationflags=creationflags,
    )
    if result.returncode != 0:
        log_text = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
        raise RuntimeError(log_text or f"WolvenKit failed with exit code {result.returncode}.")
    return result


def update_project_scene_line_ids(project_path, wolvenkit_path, old_id, new_id, old_voiceover="", new_voiceover="", backup_dir=None):
    project_path = Path(project_path)
    wolvenkit_path = Path(str(wolvenkit_path or ""))
    if not wolvenkit_path.is_file():
        raise FileNotFoundError("WolvenKit CLI is not configured or does not exist.")

    workspace = project_path / "workspace"
    if not workspace.is_dir():
        raise FileNotFoundError(f"REDkit project workspace not found: {workspace}")

    old_id = _normalize_line_id(old_id)
    new_id = _normalize_line_id(new_id)
    old_voiceover = str(old_voiceover or "").strip()
    new_voiceover = str(new_voiceover or "").strip() or _replace_line_id_suffix(old_voiceover, old_id, new_id)
    temp_dir = Path(backup_dir or _make_backup_dir(project_path)) / "_tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)

    scanned = 0
    changed = 0
    skipped = []
    for scene_path in sorted(workspace.rglob("*.w2scene")):
        scanned += 1
        json_path = temp_dir / f"{abs(hash(str(scene_path).lower()))}.json"
        try:
            _run_wolvenkit([
                str(wolvenkit_path),
                "--cr2w2json",
                "--input",
                str(scene_path),
                "--output",
                str(json_path),
            ])
            with open(json_path, "r", encoding="utf-8", errors="replace") as handle:
                data = json.load(handle)
            if not _mutate_scene_json_value(data, "", old_id, new_id, old_voiceover, new_voiceover):
                continue
            if backup_dir is not None:
                _backup_file(project_path, backup_dir, scene_path)
            with open(json_path, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(data, handle, indent=2, ensure_ascii=False)
            _run_wolvenkit([
                str(wolvenkit_path),
                "--json2cr2w",
                "--input",
                str(json_path),
                "--output",
                str(scene_path),
            ])
            changed += 1
        except Exception as exc:
            skipped.append(f"{scene_path}: {exc}")
    try:
        shutil.rmtree(temp_dir)
    except Exception:
        pass
    return scanned, changed, tuple(skipped)


def update_project_line(project_path, old_id, new_id, text=None, speaker=None, language="en",
                        old_voiceover="", new_voiceover="", wolvenkit_path="", update_scenes=False,
                        rename_assets=True):
    project_path = Path(project_path)
    backup_dir = _make_backup_dir(project_path)
    old_id = _normalize_line_id(old_id)
    new_id = _normalize_line_id(new_id)
    old_voiceover = str(old_voiceover or "").strip()
    new_voiceover = str(new_voiceover or voiceover_name(speaker, new_id)).strip()

    csv_changed = update_project_line_csv(
        project_path,
        old_id,
        new_id,
        text=text,
        speaker=speaker,
        language=language,
        backup_dir=backup_dir,
    )
    assets_renamed = 0
    if rename_assets and (old_id != new_id or old_voiceover != new_voiceover):
        assets_renamed = rename_project_line_assets(
            project_path,
            old_id,
            new_id,
            language=language,
            old_voiceover=old_voiceover,
            new_voiceover=new_voiceover,
            backup_dir=backup_dir,
        )

    scenes_scanned = 0
    scenes_changed = 0
    skipped = ()
    if update_scenes and (old_id != new_id or old_voiceover != new_voiceover):
        scenes_scanned, scenes_changed, skipped = update_project_scene_line_ids(
            project_path,
            wolvenkit_path,
            old_id,
            new_id,
            old_voiceover=old_voiceover,
            new_voiceover=new_voiceover,
            backup_dir=backup_dir,
        )

    return ProjectLineUpdateResult(
        csv_changed=csv_changed,
        scenes_scanned=scenes_scanned,
        scenes_changed=scenes_changed,
        assets_renamed=assets_renamed,
        backup_dir=backup_dir,
        skipped_files=skipped,
    )
