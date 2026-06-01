from __future__ import annotations

import json
import logging
import os
from collections import Counter
from pathlib import Path

from ....extension_paths import get_cache_root

log = logging.getLogger(__name__)

def data_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "data"


def cache_dir(name: str) -> Path:
    path = Path(get_cache_root(create=True)) / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def normalize_path(path: str) -> str:
    if not path:
        return ""
    try:
        return os.path.normpath(os.path.abspath(path))
    except Exception:
        return os.path.normpath(path)


def normcase(path: str) -> str:
    return os.path.normcase(os.path.normpath(str(path or "")))


def coerce_roots(roots):
    if roots is None:
        return []
    if isinstance(roots, (str, os.PathLike)):
        return [roots]
    try:
        return [root for root in roots if root]
    except TypeError:
        return [roots]


def iter_files(roots, suffix):
    found = []
    seen = set()
    suffix = str(suffix or "").lower()
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d.lower() not in {"__pycache__", ".git", ".svn"}]
            for filename in filenames:
                if not filename.lower().endswith(suffix):
                    continue
                path = os.path.join(dirpath, filename)
                key = normcase(path)
                if key in seen:
                    continue
                seen.add(key)
                found.append(path)
    found.sort(key=normcase)
    return found


def roots_cache_key(roots) -> str:
    parts = []
    seen = set()
    for root in roots:
        key = normcase(root)
        if key in seen:
            continue
        seen.add(key)
        parts.append(root)
    return os.pathsep.join(parts)


def file_signature(path: Path):
    try:
        stat = path.stat()
    except OSError:
        return {"path": str(path), "exists": False}
    return {
        "path": str(path),
        "exists": True,
        "size": int(getattr(stat, "st_size", 0) or 0),
        "mtime_ns": int(getattr(stat, "st_mtime_ns", 0) or 0),
    }


def build_signature(scene_files, roots, *, extra=None):
    total_size = 0
    max_mtime_ns = 0
    checked = 0
    for path in scene_files:
        try:
            stat = os.stat(path)
        except OSError:
            continue
        checked += 1
        total_size += int(getattr(stat, "st_size", 0) or 0)
        max_mtime_ns = max(max_mtime_ns, int(getattr(stat, "st_mtime_ns", 0) or 0))
    signature = {
        "roots": roots_cache_key(roots),
        "scene_count": len(scene_files),
        "checked": checked,
        "total_size": total_size,
        "max_mtime_ns": max_mtime_ns,
    }
    if extra:
        signature.update(extra)
    return signature


def load_json_file(path: Path):
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def load_cache(path: Path, version: int, signature):
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        log.debug("Failed to read scene dialogue cache: %s", path, exc_info=True)
        return None
    if not isinstance(data, dict):
        return None
    if int(data.get("version") or 0) != int(version):
        return None
    if data.get("signature") != signature:
        return None
    return data


def save_cache(path: Path, data):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        log.warning("Failed to write scene dialogue cache: %s", path, exc_info=True)


def repo_relative(path: str, roots) -> str:
    norm = normalize_path(path)
    best_root = ""
    for root in roots:
        root_norm = normalize_path(root)
        try:
            common = os.path.commonpath([norm, root_norm])
        except Exception:
            continue
        if normcase(common) == normcase(root_norm) and len(root_norm) > len(best_root):
            best_root = root_norm
    if best_root:
        try:
            return os.path.relpath(norm, best_root).replace("/", "\\")
        except Exception:
            pass
    return os.path.basename(norm)


def normalize_line_id(value, *, numeric=False) -> str:
    text = str(value or "").strip()
    if text.upper().startswith("VO_ID"):
        text = text[5:]
    if numeric and text.isdigit():
        return str(int(text))
    return text


def normalize_tag(value: str) -> str:
    return str(value or "").strip().upper()


def format_display(value: str) -> str:
    value = str(value or "").strip().replace("_", " ")
    if not value:
        return ""
    parts = []
    for part in value.split():
        parts.append(part.upper() if any(ch.isdigit() for ch in part) else part.capitalize())
    return " ".join(parts)


def most_common_path(counter: Counter) -> str:
    if not counter:
        return ""
    return sorted(counter.items(), key=lambda item: (-int(item[1]), item[0].lower()))[0][0]
