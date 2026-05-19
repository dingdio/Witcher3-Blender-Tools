import csv
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path


MAX_RADISH_LINE_ID = 2147483647
PROJECT_STRINGS_CSV = "LocalEditorStringDataBaseW3_UTF8_mod_export.csv"


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


def _json_int(data, key):
    try:
        return int(data.get(key))
    except (TypeError, ValueError):
        return None


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
