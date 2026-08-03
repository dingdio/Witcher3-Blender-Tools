import copy
import logging
import re
import threading
import time
from contextvars import ContextVar
from functools import partial
from typing import List
log = logging.getLogger(__name__)

import os
from pathlib import Path

from .common_blender import get_repo_resolution_context, repo_file, redkit_repo_context
from .CR2W_file import create_level, read_CR2W
from .CR2W_types import Entity_Type_List, getCR2W, is_entity_chunk
from .bStream import bStream, bReadStream
from .prop_utils import prop_to_string
from .read_json_w3 import readCSkeletonData
from . import w3_types
from ..repo_paths import materialize_entity_repo_path, resolve_w2_repo_file_from_source

_template_file_cache = {}
_template_file_cache_lock = threading.RLock()
_template_file_cache_inflight = {}
_template_dependency_collectors = ContextVar("template_dependency_collectors", default=())
_prop_to_string = partial(prop_to_string, default=None)

_DEPOT_PATH_ROOTS = (
    "templates",
    "game",
    "characters",
    "items",
    "dlc",
    "environment",
    "environment_levels",
    "quests",
    "levels",
    "living_world",
    "gameplay",
    "animations",
    "fx",
    "engine",
    "globals",
    "gui",
    "ui",
)
_KNOWN_REPO_EXTS = (
    ".w2mesh",
    ".w2rig",
    ".w2anims",
    ".w2beh",
    ".w2steer",
    ".w2ent",
    ".redcloth",
    ".redapex",
    ".w3fac",
    ".w2fac",
    ".w2faces",
    ".w3dyng",
    ".dyng",
)

def clear_template_cache():
    """Clear the template file parse cache. Call at the start of each import."""
    with _template_file_cache_lock:
        _template_file_cache.clear()
    _dep_stat_memo.clear()


_dep_stat_memo = {}


def _dependencies_current(dependencies):
    # Repo priority changes require clear_template_cache().
    now = time.monotonic()
    for signature in dependencies:
        kind = signature[0] if signature else None
        if kind == "file":
            path, mtime, size = signature[1], signature[2], signature[3]
        elif kind == "repo":
            path, mtime, size = signature[4], signature[5], signature[6]
        else:
            continue
        if not path:
            continue
        memo = _dep_stat_memo.get(path)
        if memo is not None and memo[0] > now:
            current = memo[1]
        else:
            try:
                stat = os.stat(path)
                current = (
                    int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))),
                    int(stat.st_size),
                )
            except OSError:
                current = (0, 0)
            _dep_stat_memo[path] = (now + 3.0, current)
        if current != (mtime, size):
            return False
    return True


def _template_file_signature(path_value):
    raw_path = str(path_value or "").strip()
    if not raw_path:
        return "file", "", 0, 0
    path = os.path.normcase(os.path.normpath(os.path.abspath(raw_path)))
    try:
        stat = os.stat(path)
        return (
            "file",
            path,
            int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))),
            int(stat.st_size),
        )
    except OSError:
        return "file", path, 0, 0


def _template_repo_signature(logical_path, resolved_path, game_version=None, source_path=""):
    if game_version is None:
        version = None
    else:
        try:
            version = int(game_version)
        except Exception:
            version = None
    logical_path = str(logical_path or "").replace("/", "\\").strip()
    source_path = os.path.normcase(os.path.normpath(os.path.abspath(str(source_path)))) if source_path else ""
    file_signature = _template_file_signature(resolved_path)
    return (
        "repo",
        logical_path,
        source_path,
        version,
        file_signature[1],
        file_signature[2],
        file_signature[3],
    )


def _record_template_dependency(path_value):
    signature = _template_file_signature(path_value)
    for collector in _template_dependency_collectors.get():
        collector.add(signature)
    return signature


def _record_template_repo_dependency(logical_path, resolved_path, game_version=None, source_path=""):
    signature = _template_repo_signature(
        logical_path,
        resolved_path,
        game_version,
        source_path,
    )
    for collector in _template_dependency_collectors.get():
        collector.add(signature)
    return signature


def _read_template_dependency_cr2w(path_value):
    _record_template_dependency(path_value)
    return read_CR2W(path_value)


def _cached_template_result(cache_key):
    with _template_file_cache_lock:
        record = _template_file_cache.get(cache_key)
        if isinstance(record, dict) and "value" in record:
            value = record["value"]
            dependencies = tuple(record.get("dependencies", ()))
        else:
            value = record
            dependencies = ()
    if value is None:
        return None
    if not _dependencies_current(dependencies):
        with _template_file_cache_lock:
            _template_file_cache.pop(cache_key, None)
        return None
    for collector in _template_dependency_collectors.get():
        collector.update(dependencies)
    return copy.deepcopy(value)


def _store_template_result(cache_key, value):
    if isinstance(value, tuple) and value:
        try:
            value[0].plan_complete = True
        except Exception:
            pass
    collectors = _template_dependency_collectors.get()
    dependencies = tuple(sorted(collectors[-1], key=repr)) if collectors else ()
    dependency_paths = [
        signature[1] if signature[0] == "file" else signature[4]
        for signature in dependencies
        if signature and signature[0] in {"file", "repo"}
    ]
    if isinstance(value, tuple) and value:
        try:
            value[0].template_dependency_paths = list(dependency_paths)
        except Exception:
            pass
        if len(value) > 1 and value[1] is not None:
            try:
                value[1].template_dependency_paths = list(dependency_paths)
            except Exception:
                pass
            try:
                value[0].unsupported_components = list(
                    getattr(value[1], "unsupported_components", None) or []
                )
            except Exception:
                pass
    with _template_file_cache_lock:
        for old_key in tuple(_template_file_cache):
            if (
                old_key != cache_key
                and old_key[0] == cache_key[0]
                and old_key[2:] == cache_key[2:]
            ):
                _template_file_cache.pop(old_key, None)
        _template_file_cache[cache_key] = {
            "value": value,
            "dependencies": dependencies,
        }
    return copy.deepcopy(value)


def _flat_compiled_file(cr2w_file):
    for chunk in getattr(getattr(cr2w_file, "CHUNKS", None), "CHUNKS", None) or []:
        if getattr(chunk, "Type", None) != "CEntityTemplate":
            continue
        flat = getattr(chunk, "flatCompiledData", None)
        if getattr(getattr(flat, "CHUNKS", None), "CHUNKS", None):
            return flat
    return None


def _repo_path_key(path: str) -> str:
    return str(path or "").replace("/", "\\").strip().lower()


def _extract_depot_subpath(path_value):
    if not isinstance(path_value, str):
        return None
    normalized = path_value.strip().replace("/", "\\")
    if not normalized:
        return None
    lowered = normalized.lower()
    best_idx = None
    for root in _DEPOT_PATH_ROOTS:
        marker = f"{root}\\"
        idx = lowered.find(marker)
        if idx < 0:
            continue
        if idx > 0 and lowered[idx - 1] not in ("\\", "/"):
            continue
        if best_idx is None or idx < best_idx:
            best_idx = idx
    if best_idx is not None:
        return normalized[best_idx:]
    return None


def _expected_exts(expected_ext) -> tuple[str, ...]:
    if isinstance(expected_ext, (list, tuple, set)):
        return tuple(str(ext).lower() for ext in expected_ext if str(ext or "").strip())
    ext = str(expected_ext or "").lower()
    return (ext,) if ext else ()


def _is_valid_repo_path(path_value, expected_ext: str | None = None) -> bool:
    if not isinstance(path_value, str):
        return False
    candidate = _extract_depot_subpath(path_value)
    if not candidate:
        return False
    if any(ord(ch) < 32 for ch in candidate):
        return False
    lowered = candidate.lower()
    if lowered.startswith(("array:", "handle:", "ptr:")):
        return False
    expected_exts = _expected_exts(expected_ext)
    if expected_exts:
        return lowered.endswith(expected_exts)
    return any(lowered.endswith(ext) for ext in _KNOWN_REPO_EXTS)


def _path_candidate_exts(expected_ext: str):
    exts = _expected_exts(expected_ext)
    if len(exts) > 1:
        return exts
    ext = exts[0] if exts else ""
    candidate_map = {
        ".w2mesh": (".w2mesh", ".lmf", ".mmm"),
        ".w2rig": (".w2rig", ".hkx"),
        ".w3dyng": (".w3dyng", ".dyng"),
        ".dyng": (".dyng", ".w3dyng"),
    }
    return candidate_map.get(ext, (ext,))


def _has_candidate_ext(path_value, expected_ext: str) -> bool:
    if not isinstance(path_value, str):
        return False
    lowered = path_value.lower()
    return any(lowered.endswith(ext) for ext in _path_candidate_exts(expected_ext))


def _candidate_import_indices(import_index):
    try:
        if hasattr(import_index, "Index"):
            import_index = getattr(import_index, "Index")
        idx = int(import_index)
    except Exception:
        return []
    if idx >= 0x80000000:
        idx -= 0x100000000
    if idx < 0:
        idx = -idx - 1
    if idx < 0:
        return []
    out = [idx]
    if idx > 0:
        out.append(idx - 1)
    return out


def _template_cache_key(template_filename: str, game_version=None):
    path = os.path.normcase(os.path.normpath(os.path.abspath(str(template_filename or ""))))
    try:
        stat = os.stat(path)
        file_identity = (
            int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))),
            int(stat.st_size),
        )
    except OSError:
        file_identity = (0, 0)
    try:
        depot_context = get_repo_resolution_context(path)
    except Exception:
        depot_context = ((), False, (), ())
    if game_version is None:
        version = None
    else:
        try:
            version = int(game_version)
        except Exception:
            version = None
    return path, file_identity, version, depot_context


def _normalize_repo_subpath(depot_subpath: str, expected_ext: str):
    normalized = depot_subpath.replace("/", "\\").lstrip("\\")
    expected_exts = _expected_exts(expected_ext)
    primary_ext = expected_exts[0] if expected_exts else ""
    if primary_ext == ".w2mesh":
        normalized = re.sub(r"(?i)\\export\\", r"\\model\\", normalized, count=1)
    root, current_ext = os.path.splitext(normalized)
    normalized = root + (current_ext if current_ext.lower() in expected_exts else primary_ext)
    return normalized if _is_valid_repo_path(normalized, expected_ext) else None


def _repo_path_candidates(path_value, expected_ext: str):
    if not isinstance(path_value, str):
        return []
    normalized = path_value.replace("/", "\\")
    out = []
    seen = set()

    direct = _extract_depot_subpath(normalized)
    if direct and _has_candidate_ext(direct, expected_ext):
        key = _repo_path_key(direct)
        seen.add(key)
        out.append(direct)

    pattern = re.compile(
        r"(?i)(?:"
        + "|".join(re.escape(root) for root in _DEPOT_PATH_ROOTS)
        + r")[\\][A-Za-z0-9_./\\-]{0,260}?(?:"
        + "|".join(re.escape(ext) for ext in _path_candidate_exts(expected_ext))
        + r")"
    )
    for match in pattern.finditer(normalized):
        candidate = match.group(0).replace("/", "\\")
        key = _repo_path_key(candidate)
        if key in seen:
            continue
        seen.add(key)
        out.append(candidate)
    return out


def _normalize_repo_path_value(path_value, expected_ext: str):
    for candidate in _repo_path_candidates(path_value, expected_ext):
        normalized = _normalize_repo_subpath(candidate, expected_ext)
        if normalized:
            return normalized
    return None


def _source_repo_roots_for_chunk(chunk):
    cr2w_file = getattr(chunk, "_W_CLASS__CR2WFILE", None) if chunk else None
    file_name = getattr(cr2w_file, "fileName", None)
    if not file_name or not os.path.isabs(file_name):
        return []
    norm_path = os.path.normpath(file_name)
    lower_path = norm_path.lower()
    markers = ("\\game\\", "\\templates\\") + tuple(f"\\{root}\\" for root in _DEPOT_PATH_ROOTS)
    out = []
    seen = set()
    for marker in markers:
        idx = lower_path.find(marker)
        if idx <= 2:
            continue
        root = norm_path[:idx]
        norm_root = os.path.normcase(os.path.normpath(root))
        if norm_root in seen:
            continue
        seen.add(norm_root)
        out.append(root)
    parent_dir = os.path.dirname(norm_path)
    if parent_dir:
        norm_parent = os.path.normcase(os.path.normpath(parent_dir))
        if norm_parent not in seen:
            seen.add(norm_parent)
            out.append(parent_dir)
    return out


def _repo_path_exists(chunk, repo_path: str) -> bool:
    if not chunk or not repo_path:
        return False
    cr2w_file = getattr(chunk, "_W_CLASS__CR2WFILE", None)
    version = getattr(getattr(cr2w_file, "HEADER", None), "version", 999)
    rel_path = repo_path.replace("/", "\\").lstrip("\\")
    source_roots = _source_repo_roots_for_chunk(chunk)
    for root in source_roots:
        if os.path.exists(os.path.join(root, rel_path)):
            return True
    if version <= 115 and source_roots:
        return False
    try:
        resolved = repo_file(repo_path, version)
    except Exception:
        resolved = ""
    if resolved and os.path.exists(resolved):
        return True
    return False


def _canonical_component_name(chunk_name: str) -> str:
    name = str(chunk_name or "").strip().lower()
    if not name:
        return ""
    if name.startswith("mesh_"):
        name = name[5:]
    name = re.sub(r"[^a-z0-9_]+", "_", name).strip("_")
    return name


def _repair_w2_component_mesh_path(chunk, repo_path: str):
    if not chunk or not repo_path:
        return repo_path
    cr2w_file = getattr(chunk, "_W_CLASS__CR2WFILE", None)
    version = getattr(getattr(cr2w_file, "HEADER", None), "version", 999)
    if version > 115 or _repo_path_exists(chunk, repo_path):
        return repo_path

    component_name = _canonical_component_name(_prop_to_string(_find_prop_by_name(chunk, "name")))
    if not component_name:
        return repo_path

    directory, filename = os.path.split(repo_path.replace("/", "\\"))
    stem, ext = os.path.splitext(filename)
    prefix, sep, suffix = stem.rpartition("_")
    if not sep or not prefix or not suffix:
        return repo_path
    if f"__{component_name}_" in stem.lower():
        return repo_path

    candidate = os.path.join(directory, f"{prefix}__{component_name}_{suffix}{ext}").replace("/", "\\")
    if _repo_path_exists(chunk, candidate):
        return candidate
    return repo_path
def _resolve_repo_path_from_import_index(cr2w_file, import_index, expected_ext: str):
    if import_index is not None:
        try:
            candidate = _normalize_repo_path_value(str(import_index), expected_ext)
        except Exception:
            candidate = None
        if candidate:
            return candidate
    if not cr2w_file:
        return None
    imports = getattr(cr2w_file, "CR2WImport", None) or []
    for candidate_idx in _candidate_import_indices(import_index):
        if 0 <= candidate_idx < len(imports):
            imp = imports[candidate_idx]
            raw_path = getattr(imp, "path", None) or getattr(imp, "DepotPath", None)
            candidate = _normalize_repo_path_value(raw_path, expected_ext)
            if candidate:
                return candidate
    return None


def _resolve_w2_export_link_repo_path(cr2w_file, export_index, expected_ext: str):
    if not cr2w_file:
        return None
    version = getattr(getattr(cr2w_file, "HEADER", None), "version", 999)
    if version > 115:
        return None
    try:
        idx = int(export_index)
    except Exception:
        return None
    exports = getattr(cr2w_file, "CR2WExport", None) or []
    if not (0 <= idx < len(exports)):
        return None
    return _normalize_repo_path_value(getattr(exports[idx], "Link", None), expected_ext)


def _resolve_handle_repo_path(chunk, handle, expected_ext: str):
    if not handle:
        return None
    cr2w_file = getattr(chunk, "_W_CLASS__CR2WFILE", None) if chunk else None
    direct = _normalize_repo_path_value(getattr(handle, "DepotPath", None), expected_ext)
    if direct:
        return direct

    if getattr(handle, "ChunkHandle", False) and cr2w_file:
        ref_idx = getattr(handle, "Reference", None)
        candidate = _resolve_w2_export_link_repo_path(cr2w_file, ref_idx, expected_ext)
        if candidate:
            return candidate
        if isinstance(ref_idx, int) and 0 <= ref_idx < len(cr2w_file.CHUNKS.CHUNKS):
            ref_chunk = cr2w_file.CHUNKS.CHUNKS[ref_idx]
            for prop_name in ("importFile", "resource", "mesh", "skeleton", "mimicFace", "dyng"):
                candidate = _resolve_repo_path(ref_chunk, prop_name, expected_ext)
                if candidate:
                    return candidate

    raw_val = getattr(handle, "val", None)
    if isinstance(raw_val, int) and raw_val < 0:
        candidate = _resolve_repo_path_from_import_index(cr2w_file, -raw_val - 1, expected_ext)
        if candidate:
            return candidate

    idx = getattr(handle, "Index", None)
    if idx is not None:
        candidate = _resolve_repo_path_from_import_index(cr2w_file, idx, expected_ext)
        if candidate:
            return candidate
    return None


def _resolve_repo_path(chunk, prop_name: str, expected_ext: str):
    if not chunk:
        return None
    try:
        prop = chunk.GetVariableByName(prop_name)
    except Exception:
        prop = None
    if not prop:
        return None

    candidate = _normalize_repo_path_value(_prop_to_string(prop), expected_ext)
    if candidate:
        return candidate

    for handle in getattr(prop, "Handles", None) or []:
        candidate = _resolve_handle_repo_path(chunk, handle, expected_ext)
        if candidate:
            return candidate

    idx = getattr(prop, "Index", None)
    if idx is not None:
        candidate = _resolve_repo_path_from_import_index(
            getattr(chunk, "_W_CLASS__CR2WFILE", None),
            idx,
            expected_ext,
        )
        if candidate:
            return candidate
    return None


def _resolve_repo_paths_from_array(chunk, prop_name: str, expected_ext: str):
    if not chunk:
        return []
    try:
        prop = chunk.GetVariableByName(prop_name)
    except Exception:
        prop = None
    if not prop:
        return []

    out = []
    seen = set()
    handles = getattr(prop, "Handles", None) or []
    for handle in handles:
        candidate = _resolve_handle_repo_path(chunk, handle, expected_ext)
        if not candidate:
            continue
        key = _repo_path_key(candidate)
        if key in seen:
            continue
        seen.add(key)
        out.append(candidate)

    if out:
        return out

    candidate = _normalize_repo_path_value(_prop_to_string(prop), expected_ext)
    if candidate:
        key = _repo_path_key(candidate)
        if key not in seen:
            seen.add(key)
            out.append(candidate)
    return out


def _collect_w2_related_entity_paths(cr2w_file):
    out = []
    seen = set()
    if not cr2w_file:
        return out

    def _add(path_value):
        candidate = _normalize_repo_path_value(path_value, ".w2ent")
        if not candidate:
            return
        key = _repo_path_key(candidate)
        if key in seen:
            return
        seen.add(key)
        out.append(candidate)

    for chunk in getattr(getattr(cr2w_file, "CHUNKS", None), "CHUNKS", None) or []:
        if chunk.Type != "CEntityTemplate":
            continue
        try:
            includes = chunk.GetVariableByName("includes")
        except Exception:
            includes = None
        for handle in getattr(includes, "Handles", None) or []:
            _add(_resolve_handle_repo_path(chunk, handle, ".w2ent"))

    for imp in getattr(cr2w_file, "CR2WImport", None) or []:
        raw_path = getattr(imp, "path", None) or getattr(imp, "DepotPath", None)
        _add(raw_path)
    return out


def _load_w2_related_files_recursive(cr2w_file, inherit_visited):
    out = []
    complete = True
    seen_paths = set(inherit_visited or set())
    queue = [cr2w_file]
    while queue:
        source_file = queue.pop(0)
        for depot_path in _collect_w2_related_entity_paths(source_file):
            try:
                full_path = _resolve_w2_related_full_path(source_file, depot_path)
                _record_template_repo_dependency(
                    depot_path,
                    full_path,
                    getattr(getattr(source_file, "HEADER", None), "version", None),
                    getattr(source_file, "fileName", ""),
                )
                norm_full_path = os.path.normcase(os.path.normpath(full_path))
            except Exception:
                complete = False
                continue
            if norm_full_path in seen_paths:
                continue
            seen_paths.add(norm_full_path)
            try:
                related_file = _read_template_dependency_cr2w(full_path)
            except Exception:
                complete = False
                continue
            out.append((depot_path, full_path, related_file))
            queue.append(related_file)
    return out, complete


def _w2_repo_roots_from_file_path(file_name: str):
    if not file_name or not os.path.isabs(file_name):
        return []
    norm_path = os.path.normpath(file_name)
    lower_path = norm_path.lower()
    markers = ("\\data\\", "\\game\\", "\\templates\\") + tuple(f"\\{root}\\" for root in _DEPOT_PATH_ROOTS)
    out = []
    seen = set()
    for marker in markers:
        idx = lower_path.find(marker)
        if idx <= 2:
            continue
        if marker == "\\data\\":
            root = norm_path[:idx + len("\\data")]
        else:
            root = norm_path[:idx]
        norm_root = os.path.normcase(os.path.normpath(root))
        if norm_root in seen:
            continue
        seen.add(norm_root)
        out.append(root)
    return out


def _resolve_w2_related_full_path(cr2w_file, repo_path: str):
    if not repo_path:
        return ""
    if os.path.isabs(repo_path):
        return repo_path
    version = getattr(getattr(cr2w_file, "HEADER", None), "version", 999)
    rel_path = str(repo_path).replace("/", "\\").lstrip("\\")
    source_file_name = getattr(cr2w_file, "fileName", None)
    try:
        resolved_from_source = resolve_w2_repo_file_from_source(repo_path, source_file_name, version=version)
    except Exception:
        resolved_from_source = ""
    if resolved_from_source:
        return resolved_from_source
    fallback = ""
    source_roots = _w2_repo_roots_from_file_path(source_file_name)
    for root in source_roots:
        candidate = os.path.join(root, rel_path)
        if not fallback:
            fallback = candidate
        if os.path.exists(candidate):
            return candidate
    try:
        candidate = repo_file(repo_path, version)
    except Exception:
        candidate = ""
    if candidate and os.path.exists(candidate):
        return candidate
    return candidate or fallback or str(repo_path)


def is_valid_mesh_path(mesh_value) -> bool:
    """Return True when value looks like a real depot mesh path."""
    return _is_valid_repo_path(mesh_value, ".w2mesh")

def _convert_mesh_value(mesh_prop):
    if not mesh_prop:
        return None
    try:
        mesh_value = mesh_prop.ToString() if hasattr(mesh_prop, "ToString") else str(mesh_prop)
    except Exception:
        return None
    return _normalize_repo_path_value(mesh_value, ".w2mesh")

def _convert_color_value(color_prop):
    if not color_prop:
        return None
    if isinstance(color_prop, dict):
        return {
            key: color_prop.get(key)
            for key in ("Red", "Green", "Blue", "Alpha")
            if key in color_prop
        }

    prop_items = getattr(color_prop, "MoreProps", None) or getattr(color_prop, "More", None) or []
    color = {}
    for item in prop_items:
        key = getattr(item, "theName", None)
        value = getattr(item, "Value", None)
        if key in ("Red", "Green", "Blue", "Alpha"):
            color[key] = value

    if color:
        return color

    for key in ("Red", "Green", "Blue", "Alpha"):
        value = getattr(color_prop, key, None)
        if value is not None:
            color[key] = value
    return color or color_prop


def _convert_named_scalar_struct(struct_prop):
    if not struct_prop:
        return {}
    if isinstance(struct_prop, dict):
        return dict(struct_prop)

    values = {}
    prop_items = (
        getattr(struct_prop, "MoreProps", None)
        or getattr(struct_prop, "More", None)
        or getattr(struct_prop, "PROPS", None)
        or []
    )
    for item in prop_items:
        key = getattr(item, "theName", None)
        value = getattr(item, "Value", None)
        if key and isinstance(value, (bool, int, float)):
            values[key] = value
    return values

def _class_name_from_import(cr2w_file, imp):
    class_name = getattr(imp, "className", None)
    if isinstance(class_name, int):
        try:
            return cr2w_file.CNAMES[class_name].name.value
        except Exception:
            return None
    if hasattr(class_name, "value"):
        try:
            return class_name.value
        except Exception:
            return None
    return class_name

def _collect_mesh_import_paths(cr2w_file):
    """Collect CMesh import depot paths from a CR2W file in import-table order."""
    out = []
    if not cr2w_file:
        return out
    imports = getattr(cr2w_file, "CR2WImport", None) or []
    for imp in imports:
        class_name = _class_name_from_import(cr2w_file, imp)
        if class_name not in (None, "CMesh"):
            continue
        raw_path = getattr(imp, "path", None) or getattr(imp, "DepotPath", None)
        candidate = _normalize_repo_path_value(raw_path, ".w2mesh")
        if candidate:
            out.append(candidate)
    return out


def _collect_rig_import_paths(cr2w_file):
    """Collect CSkeleton import depot paths from a CR2W file in import-table order.
    Used as fallback when a CAnimatedComponent override chunk omits the skeleton property."""
    out = []
    if not cr2w_file:
        return out
    imports = getattr(cr2w_file, "CR2WImport", None) or []
    for imp in imports:
        class_name = _class_name_from_import(cr2w_file, imp)
        if class_name not in (None, "CSkeleton"):
            continue
        raw_path = getattr(imp, "path", None) or getattr(imp, "DepotPath", None)
        candidate = _normalize_repo_path_value(raw_path, ".w2rig")
        if candidate:
            out.append(candidate)
    return out

def _collect_beh_import_paths(cr2w_file):
    """Collect CBehaviorGraph depot paths (.w2beh) from a CR2W file's import table."""
    out = []
    if not cr2w_file:
        return out
    imports = getattr(cr2w_file, "CR2WImport", None) or []
    for imp in imports:
        class_name = _class_name_from_import(cr2w_file, imp)
        if class_name not in (None, "CBehaviorGraph"):
            continue
        raw_path = getattr(imp, "path", None) or getattr(imp, "DepotPath", None)
        candidate = _normalize_repo_path_value(raw_path, ".w2beh")
        if candidate:
            out.append(candidate)
    return out


def _mesh_path_from_import_index(chunk, import_index):
    cr2w_file = getattr(chunk, "_W_CLASS__CR2WFILE", None) if chunk else None
    return _resolve_repo_path_from_import_index(cr2w_file, import_index, ".w2mesh")

def _mesh_path_from_handle(chunk, handle):
    return _resolve_handle_repo_path(chunk, handle, ".w2mesh")

def _resolve_mesh_path(chunk, mesh_value):
    """Resolve a mesh path from parsed chunk data."""
    if is_valid_mesh_path(mesh_value):
        return _repair_w2_component_mesh_path(chunk, mesh_value)
    candidate = _resolve_repo_path(chunk, "mesh", ".w2mesh")
    if candidate:
        return _repair_w2_component_mesh_path(chunk, candidate)
    try:
        mesh_var = chunk.GetVariableByName("mesh") if chunk else None
    except Exception:
        mesh_var = None
    if mesh_var:
        try:
            direct_mesh = mesh_var.ToString()
        except Exception:
            direct_mesh = None
        if is_valid_mesh_path(direct_mesh):
            return _repair_w2_component_mesh_path(chunk, direct_mesh)
        handles = getattr(mesh_var, "Handles", None) or []
        for handle in handles:
            candidate = _mesh_path_from_handle(chunk, handle)
            if is_valid_mesh_path(candidate):
                return _repair_w2_component_mesh_path(chunk, candidate)
        candidate = _mesh_path_from_import_index(chunk, getattr(mesh_var, "Index", None))
        if is_valid_mesh_path(candidate):
            return _repair_w2_component_mesh_path(chunk, candidate)

    # Last parsed-property pass for chunks that encode the mesh indirectly.
    props = getattr(chunk, "PROPS", None) or []
    for prop in props:
        handles = getattr(prop, "Handles", None) or []
        for handle in handles:
            candidate = _mesh_path_from_handle(chunk, handle)
            if is_valid_mesh_path(candidate):
                return _repair_w2_component_mesh_path(chunk, candidate)
        candidate = _mesh_path_from_import_index(chunk, getattr(prop, "Index", None))
        if is_valid_mesh_path(candidate):
            return _repair_w2_component_mesh_path(chunk, candidate)

    embedded_source_path, embedded_cmesh_chunk_index = _w2_embedded_mesh_ref_info(chunk)
    if embedded_source_path and embedded_cmesh_chunk_index is not None:
        return embedded_source_path
    return None


def _resolve_component_mesh(chunk, component, next_mesh_import_path):
    component.mesh = _resolve_mesh_path(chunk, component.mesh)
    if not component.mesh:
        component.mesh = next_mesh_import_path()
    return component


def _chunk_props_summary(chunk, limit=10):
    props = getattr(chunk, "PROPS", None) or []
    out = []
    for prop in props[:limit]:
        out.append(f"{getattr(prop, 'theName', '?')}:{getattr(prop, 'theType', '?')}")
    return ", ".join(out)


def _describe_unknown_character_chunk(chunk, cr2w_file=None, template_name=""):
    parts = []
    file_name = getattr(cr2w_file, "fileName", None)
    if file_name:
        parts.append(f"file={file_name}")
    if template_name:
        parts.append(f"template={template_name}")
    parts.append(f"chunk={getattr(chunk, 'ChunkIndex', getattr(chunk, 'chunkIndex', '?'))}")
    chunk_name = getattr(chunk, "name", None)
    if chunk_name:
        parts.append(f"name={chunk_name}")
    props = _chunk_props_summary(chunk)
    if props:
        parts.append(f"props={props}")
    return ", ".join(parts)


def _log_unknown_character_chunk(chunk, cr2w_file=None, template_name=""):
    detail = _describe_unknown_character_chunk(chunk, cr2w_file=cr2w_file, template_name=template_name)
    chunk_type = getattr(chunk, "Type", getattr(chunk, "type", "?"))
    log.warning("Unknown Character Chunk: %s (%s)", chunk_type, detail)

class JsonChunk(object):
    """docstring for JsonChunk."""
    def __init__(self):
        super(JsonChunk, self).__init__()
        self.chunkIndex = 0
        self.type = 0
        #![JsonIgnore]
        #self.refChunk = 0

    def __getitem__(self, item):
        return getattr(self, item)

    def __setitem__(self, item, value):
        setattr(self, item, value)

    def get(self, item, default=None):
        return getattr(self, item, default)

    def __contains__(self, item):
        return hasattr(self, item)

    def keys(self):
        return vars(self).keys()

    def items(self):
        return vars(self).items()

class ModelEnt(object):
    """docstring for ModelEnt."""
    def __init__(self, templateFilename, ns):
        super(ModelEnt, self).__init__()
        self.templateFilename = templateFilename
        self.ns = ns
        self.chunks = []
        self.template_dependency_paths = []
        #self.animation_face_object = False
    def __getitem__(self, item):
        return getattr(self, item)

    def __setitem__(self, item, value):
        setattr(self, item, value)

    def get(self, item, default=None):
        return getattr(self, item, default)

    def __contains__(self, item):
        return hasattr(self, item)

    def keys(self):
        return vars(self).keys()

    def items(self):
        return vars(self).items()

class CRigidMeshComponent(JsonChunk):
    """docstring for CRigidMeshComponent."""
    def __init__(self, *args, **kwargs):
        self.tags = None                   #" Type="TagList" />
        self.transform = None                   #" Type="EngineTransform" />
        self.transformParent = None                   #" Type="ptr:CHardAttachment" />
        self.guid = None                   #" Type="CGUID" />
        self.name = None                   #" Type="String" />
        self.isStreamed = None                   #" Type="Bool" />
        self.boundingBox = None                   #" Type="Box" />
        self.drawableFlags = None                   #" Type="EDrawableFlags" />
        self.lightChannels = None                   #" Type="ELightChannel" />
        self.renderingPlane = None                   #" Type="ERenderingPlane" />
        self.forceLODLevel = None                   #" Type="Int32" />
        self.forceAutoHideDistance = None                   #" Type="Uint16" />
        self.shadowImportanceBias = None                   #" Type="EMeshShadowImportanceBias" />
        self.defaultEffectParams = None                   #" Type="Vector" />
        self.defaultEffectColor = None                   #" Type="Color" />
        self.mesh = None                   #" Type="handle:CMesh" />
        self.pathLibCollisionType = None                   #" Type="EPathLibCollision" />
        self.fadeOnCameraCollision = None                   #" Type="Bool" />
        self.physicalCollisionType = None                   #" Type="CPhysicalCollision" />
        self.motionType = None                   #" Type="EMotionType" />
        self.linearDamping = None                   #" Type="Float" />
        self.angularDamping = None                   #" Type="Float" />
        self.linearVelocityClamp = None                   #" Type="Float" />
        w3_types.loadProps(self, args)
    
    def convert_for_io(self):
        self.transformParent = self.transformParent.Value-1 if self.transformParent else None
        self.mesh = _convert_mesh_value(self.mesh)
        self.transform = self.transform.EngineTransform if self.transform else None
        return self

class CMeshComponent(JsonChunk):
    """docstring for CMeshComponent."""
    def __init__(self, *args, **kwargs):
        #super(CMeshComponent, self).__init__()
        self.tags = None #Type="TagList"
        self.transform = None #Type="EngineTransform"
        self.transformParent = None #Type="ptr:CHardAttachment"
        self.guid = None #Type="CGUID"
        self.name = None #Type="String"
        self.isStreamed = None #Type="Bool"
        self.boundingBox = None #Type="Box"
        self.drawableFlags = None #Type="EDrawableFlags"
        self.lightChannels = None #Type="ELightChannel"
        self.renderingPlane = None #Type="ERenderingPlane"
        self.forceLODLevel = None #Type="Int32"
        self.forceAutoHideDistance = None #Type="Uint16"
        self.shadowImportanceBias = None #Type="EMeshShadowImportanceBias"
        self.defaultEffectParams = None #Type="Vector"
        self.defaultEffectColor = None #Type="Color"
        self.mesh = None #Type="handle:CMesh"
        w3_types.loadProps(self, args)
    
    def convert_for_io(self):
        self.transformParent = self.transformParent.Value-1 if self.transformParent else None
        self.mesh = _convert_mesh_value(self.mesh)
        self.transform = self.transform.EngineTransform if self.transform else None
        return self

class CCollisionShapeConvex(JsonChunk):
    def __init__(self, *args, **kwargs):
        self.physicalMaterialName = None
        self.vertices = None
        self.polygons = None
        w3_types.loadProps(self, args)
        if self.vertices and self.polygons:
            try:
                self.polygons = self.polygons.value
                self.vertices = [[prop.Value for prop in verts.MoreProps[:4]] for verts in self.vertices.More if hasattr(verts, 'MoreProps') and len(verts.MoreProps) >= 4]
            except Exception as e:
                log.error('Could not get CCollisionShapeConvex')
                

class CCollisionShapeTriMesh(JsonChunk):
    def __init__(self, *args, **kwargs):
        self.physicalMaterialNames = None
        self.vertices = None
        self.triangles = None
        self.physicalMaterialIndexes = None
        w3_types.loadProps(self, args)
        if self.vertices and self.triangles:
            try:
                raw_vertices = self.vertices
                if hasattr(raw_vertices, "More"):
                    self.vertices = [
                        [prop.Value for prop in verts.MoreProps[:4]]
                        for verts in raw_vertices.More
                        if hasattr(verts, 'MoreProps') and len(verts.MoreProps) >= 4
                    ]
            except Exception:
                log.error('Could not parse CCollisionShapeTriMesh.vertices')

            try:
                raw_triangles = self.triangles
                if hasattr(raw_triangles, "value"):
                    self.triangles = raw_triangles.value
            except Exception:
                log.error('Could not parse CCollisionShapeTriMesh.triangles')

            try:
                raw_names = self.physicalMaterialNames
                parsed_names = []
                if isinstance(raw_names, list):
                    parsed_names = [name for name in raw_names if isinstance(name, str) and name]
                elif raw_names is not None:
                    index_items = getattr(raw_names, "Index", None)
                    if isinstance(index_items, list):
                        for item in index_items:
                            name = None
                            if isinstance(item, str):
                                name = item
                            elif hasattr(item, "String"):
                                name = item.String
                            elif hasattr(item, "ToString"):
                                try:
                                    name = item.ToString()
                                except Exception:
                                    name = None
                            if isinstance(name, str) and name:
                                parsed_names.append(name)

                    if not parsed_names:
                        elements = getattr(raw_names, "elements", None)
                        if isinstance(elements, list):
                            for item in elements:
                                name = None
                                if isinstance(item, str):
                                    name = item
                                elif hasattr(item, "String"):
                                    name = item.String
                                elif hasattr(item, "value"):
                                    val = item.value
                                    if isinstance(val, str):
                                        name = val
                                    elif hasattr(val, "name") and hasattr(val.name, "value"):
                                        name = val.name.value
                                elif hasattr(item, "name") and hasattr(item.name, "value"):
                                    name = item.name.value
                                if isinstance(name, str) and name:
                                    parsed_names.append(name)

                if parsed_names or getattr(raw_names, "Count", None) == 0:
                    self.physicalMaterialNames = parsed_names
            except Exception:
                log.error('Could not parse CCollisionShapeTriMesh.physicalMaterialNames')

            try:
                raw_indexes = self.physicalMaterialIndexes
                parsed_indexes = None
                if isinstance(raw_indexes, list):
                    parsed_indexes = raw_indexes
                elif raw_indexes is not None:
                    if hasattr(raw_indexes, "value"):
                        parsed_indexes = raw_indexes.value
                    elif hasattr(raw_indexes, "More"):
                        parsed_indexes = [
                            entry.Value if hasattr(entry, "Value") else entry
                            for entry in raw_indexes.More
                        ]
                if parsed_indexes is not None:
                    self.physicalMaterialIndexes = parsed_indexes
            except Exception:
                log.error('Could not parse CCollisionShapeTriMesh.physicalMaterialIndexes')


class CCollisionShapeBox(JsonChunk): # ICollisionShape
    def __init__(self, *args, **kwargs):
        self.physicalMaterialName = None
        self.pose = None
        self.halfExtendsX = None
        self.halfExtendsY = None
        self.halfExtendsZ = None
        w3_types.loadProps(self, args)
        
        if self.pose:
            try:
                # self.pose.More is a list of 4 rows (X, Y, Z, Translation)
                # Each row has .More containing 4 items with .theName ('X','Y','Z','W') and .Value
                matrix_4x4 = []
                for row in self.pose.More:
                    row_values = []
                    for item in row.More:
                        row_values.append(float(item.Value))
                    matrix_4x4.append(row_values)
                
                # Store as a clean 4x4 list (row-major): [[rx, ry, rz, tx], [ux, uy, uz, ty], ...]
                self.matrix_world = matrix_4x4
                
            except Exception as e:
                log.error(f'Could not parse pose matrix for CCollisionShapeBox: {e}')
                self.matrix_world = None
        else:
            self.matrix_world = None
        
        
        # if self.pose:
        #     self.final_pose = []
        #     the_matrix = [
        #     ]
        #     try:
        #         for vec in self.pose.More: # Matrix
        #             the_vec = []
        #             for vec_item in vec.More: # vectors
        #                 the_vec.append({vec_item.theName : vec_item.Value})
        #             the_matrix.append(the_vec)
        #             self.final_pose.append({vec.theName : the_matrix})
        #     except Exception as e:
        #         log.error('Could not get CCollisionShapeBox')
        
class CCollisionShapeSphere(JsonChunk): # ICollisionShape
    def __init__(self, *args, **kwargs):
        self.physicalMaterialName = None
        self.radius = None
        self.pose = None
        w3_types.loadProps(self, args)
        
        if self.pose:
            try:
                # self.pose.More is a list of 4 rows (X, Y, Z, Translation)
                # Each row has .More containing 4 items with .theName ('X','Y','Z','W') and .Value
                matrix_4x4 = []
                for row in self.pose.More:
                    row_values = []
                    for item in row.More:
                        row_values.append(float(item.Value))
                    matrix_4x4.append(row_values)
                
                # Store as a clean 4x4 list (row-major): [[rx, ry, rz, tx], [ux, uy, uz, ty], ...]
                self.matrix_world = matrix_4x4
                
            except Exception as e:
                log.error(f'Could not parse pose matrix for CCollisionShapeBox: {e}')
                self.matrix_world = None
        else:
            self.matrix_world = None
        

class CCollisionShapeCapsule(JsonChunk): # ICollisionShape
    def __init__(self, *args, **kwargs):
        self.physicalMaterialName = None
        self.radius = None
        self.height = None
        self.pose = None
        w3_types.loadProps(self, args)
        
        if self.pose:
            try:
                # self.pose.More is a list of 4 rows (X, Y, Z, Translation)
                # Each row has .More containing 4 items with .theName ('X','Y','Z','W') and .Value
                matrix_4x4 = []
                for row in self.pose.More:
                    row_values = []
                    for item in row.More:
                        row_values.append(float(item.Value))
                    matrix_4x4.append(row_values)
                
                # Store as a clean 4x4 list (row-major): [[rx, ry, rz, tx], [ux, uy, uz, ty], ...]
                self.matrix_world = matrix_4x4
                
            except Exception as e:
                log.error(f'Could not parse pose matrix for CCollisionShapeBox: {e}')
                self.matrix_world = None
        else:
            self.matrix_world = None

class CStaticMeshComponent(CMeshComponent):
    """docstring for CStaticMeshComponent."""
    def __init__(self, *args, **kwargs):
        super(CStaticMeshComponent, self).__init__(*args, **kwargs)
        self.pathLibCollisionType = None #Type="EPathLibCollision"
        self.fadeOnCameraCollision = None #Type="Bool"
        self.physicalCollisionType = None #Type="CPhysicalCollision"

class CRagdollMeshComponent(CMeshComponent):
    """Witcher 2 mesh component backed by a havok ragdoll / cloth body.
    """
    def __init__(self, *args, **kwargs):
        self.ragdollResource = None    #Type="handle:CRagdoll"
        self.baseBodyName = None       #Type="CName"
        self.initialMotionType = None  #Type="EMotionType"
        super(CRagdollMeshComponent, self).__init__(*args, **kwargs)

    def convert_for_io(self):
        super(CRagdollMeshComponent, self).convert_for_io()
        if self.ragdollResource is not None and hasattr(self.ragdollResource, "Value"):
            self.ragdollResource = self.ragdollResource.Value - 1
        return self

class CClothComponent(JsonChunk):
    """docstring for CClothComponent."""
    def __init__(self, resource, name: str = ""):
        super(CClothComponent, self).__init__()
        self.resource = resource
        self.name = name

class CFoliageComponent(JsonChunk):
    def __init__(self, srt, name: str = "", transform=None, transformParent=None,
                 srt_entries=None, srt_entry: str = ""):
        super(CFoliageComponent, self).__init__()
        self.srt = srt
        self.name = name
        self.transform = transform
        self.transformParent = transformParent
        self.srt_entries = dict(srt_entries or {})
        self.srt_entry = srt_entry

class CMorphedMeshComponent(JsonChunk):
    """docstring for CMorphedMeshComponent."""
    def __init__(self, morphTarget:str, morphSource:str, morphComponentId:str):
        super(CMorphedMeshComponent, self).__init__()
        self.morphTarget = morphTarget
        self.morphSource = morphSource
        #self.morphControlTextures = morphSource
        self.morphComponentId = morphComponentId

class CMimicComponent(JsonChunk):
    """docstring for CMimicComponent."""
    def __init__(self, name:str, mimicFace:str):
        super(CMimicComponent, self).__init__()
        self.name = name
        self.mimicFace = mimicFace

class CAnimatedComponent(JsonChunk):
    """docstring for CAnimatedComponent."""
    def __init__(self, *args, name:str = "", skeleton:str = ""):
        super(CAnimatedComponent, self).__init__()
        self.transform = None #Type="EngineTransform"
        self.transformParent = None #Type="ptr:CHardAttachment"
        self.guid = None #Type="CGUID"
        self.name = name
        self.skeleton = skeleton
        if args:
            w3_types.loadProps(self, args)

    def convert_for_io(self):
        self.transformParent = self.transformParent.Value-1 if self.transformParent else None
        self.transform = self.transform.EngineTransform if self.transform else None
        return self

class CAnimDangleComponent(JsonChunk):
    """docstring for CAnimDangleComponent."""
    def __init__(self, name:str, constraint:int):
        super(CAnimDangleComponent, self).__init__()
        self.name = name
        self.constraint = constraint

class CAnimDangleBufferComponent(JsonChunk):
    """docstring for CAnimDangleBufferComponent."""
    def __init__(self, name:str, skeleton:str):
        super(CAnimDangleBufferComponent, self).__init__()
        self.name = name
        self.skeleton = skeleton

class SkinningAttachment(JsonChunk):
    """docstring for SkinningAttachment."""
    def __init__(self, parent:int, child:int):
        super(SkinningAttachment, self).__init__()
        self.parent = parent
        self.child = child

class CMeshSkinningAttachment(SkinningAttachment):
    """docstring for CMeshSkinningAttachment."""
    def __init__(self, parent:int, child:int):
        super(CMeshSkinningAttachment, self).__init__(parent, child)

class CAnimatedAttachment(SkinningAttachment):
    """docstring for CAnimatedAttachment."""
    def __init__(self, parent:int, child:int):
        super(CAnimatedAttachment, self).__init__(parent, child)

class CHardAttachment(SkinningAttachment):
    """docstring for CHardAttachment."""
    def __init__(self, *args, **kwargs): #parent:int , child:int , parentSlot:int , parentSlotName:str):
        #super(CHardAttachment, self).__init__(parent, child)
        self.parent = None # Type="ptr:CNode"
        self.child = None # Type="ptr:CNode"
        self.isBroken:bool = None # Type="Bool"
        self.relativeTransform = None # Type="EngineTransform"
        self.parentSlotName = None # Type="CName"
        self.attachmentFlags = None # Type="EHardAttachmentFlags"
        self.parentSlot = None # Type="ptr:ISlot"
        w3_types.loadProps(self, args)
    
    def convert_for_io(self):
        self.parent = self.parent.Value-1 if self.parent else None
        self.child = self.child.Value-1 if self.child else None
        self.parentSlot = self.parentSlot.Value-1 if self.parentSlot else None
        self.relativeTransform = self.relativeTransform.EngineTransform if self.relativeTransform else None
        return self


class CAnimDangleConstraint_Breast(JsonChunk):
    """docstring for CAnimDangleConstraint_Breast."""
    def __init__(self, skeleton, settings=None):
        super(CAnimDangleConstraint_Breast, self).__init__()
        self.skeleton = skeleton
        for key, value in (settings or {}).items():
            setattr(self, key, value)

class CAnimDangleConstraint_Collar(JsonChunk):
    """docstring for CAnimDangleConstraint_Collar."""
    def __init__(self, skeleton):
        super(CAnimDangleConstraint_Collar, self).__init__()
        self.skeleton = skeleton

class CAnimDangleConstraint_Pusher(JsonChunk):
    """docstring for CAnimDangleConstraint_Pusher."""
    def __init__(self, skeleton):
        super(CAnimDangleConstraint_Pusher, self).__init__()
        self.skeleton = skeleton

class CAnimDangleConstraint_Hinge(JsonChunk):
    """docstring for CAnimDangleConstraint_Hinge."""
    def __init__(self, skeleton):
        super(CAnimDangleConstraint_Hinge, self).__init__()
        self.skeleton = skeleton

class CAnimDangleConstraint_Hood(JsonChunk):
    """docstring for CAnimDangleConstraint_Hood."""
    def __init__(self, skeleton):
        super(CAnimDangleConstraint_Hood, self).__init__()
        self.skeleton = skeleton

class CAnimDangleConstraint_Dress(JsonChunk):
    """docstring for CAnimDangleConstraint_Dress."""
    def __init__(self, skeleton):
        super(CAnimDangleConstraint_Dress, self).__init__()
        self.skeleton = skeleton

class CAnimDangleConstraint_Dyng(JsonChunk):
    """docstring for CAnimDangleConstraint_Dyng."""
    def __init__(self, skeleton, dyng, settings=None):
        super(CAnimDangleConstraint_Dyng, self).__init__()
        self.skeleton = skeleton
        self.dyng = dyng
        for key, value in (settings or {}).items():
            setattr(self, key, value)

class CSkeletonBoneSlot(JsonChunk):
    """docstring for CSkeletonBoneSlot."""
    def __init__(self, boneIndex:int):
        super(CSkeletonBoneSlot, self).__init__()
        self.boneIndex = boneIndex

class CCameraComponent(JsonChunk):
    def __init__(self, name):
        super(CCameraComponent, self).__init__()
        self.name = name
        self.transformParent = None #<ptr:CHardAttachment>

class CPointLightComponent(JsonChunk):
    def __init__(self, *args, **kwargs):
        super(CPointLightComponent, self).__init__()
        self.transform = None
        self.transformParent = None
        self.name = None
        self.radius = None
        self.color = None
        self.brightness = None
        self.lightFlickering = None
        w3_types.loadProps(self, args)

    def convert_for_io(self):
        self.transformParent = self.transformParent.Value-1 if self.transformParent else None
        self.transform = self.transform.EngineTransform if self.transform else None
        self.color = _convert_color_value(self.color)
        self.lightFlickering = _convert_named_scalar_struct(self.lightFlickering)
        return self

class CSpotLightComponent(CPointLightComponent):
    def __init__(self, *args, **kwargs):
        super(CSpotLightComponent, self).__init__(*args, **kwargs)
        self.innerAngle = None
        self.outerAngle = None
        self.shadowCastingMode = None
        self.shadowFadeDistance = None
        self.lightFlickering = None
        w3_types.loadProps(self, args)

entity_type_dict = {
    "CMeshComponent": CMeshComponent,
    "CClothComponent": CClothComponent,
    "CFurComponent": CMeshComponent,
    "CMorphedMeshComponent": CMorphedMeshComponent,
    "CMimicComponent": CMimicComponent,
    "CMeshSkinningAttachment": CMeshSkinningAttachment,
    "CAnimatedAttachment": CAnimatedAttachment,
    "CAnimDangleBufferComponent": CAnimDangleBufferComponent,
    "CAnimDangleComponent": CAnimDangleComponent,
    "CStaticMeshComponent": CStaticMeshComponent,
    "CAnimatedComponent": CAnimatedComponent,
    "CHardAttachment": CHardAttachment,
    "CSkeletonBoneSlot": CSkeletonBoneSlot,
    "CCameraComponent": CCameraComponent,
    "CPointLightComponent": CPointLightComponent,
    "CSpotLightComponent": CSpotLightComponent,
}

CAnimDangleConstraint_types = {
    "CAnimDangleConstraint_Dyng": CAnimDangleConstraint_Dyng,
    "CAnimDangleConstraint_Breast": CAnimDangleConstraint_Breast,
    "CAnimDangleConstraint_Collar": CAnimDangleConstraint_Collar,
    "CAnimDangleConstraint_Dress": CAnimDangleConstraint_Dress,
    "CAnimDangleConstraint_Hood": CAnimDangleConstraint_Hood,
    "CAnimDangleConstraint_Hinge": CAnimDangleConstraint_Hinge,
    "CAnimDangleConstraint_Pusher": CAnimDangleConstraint_Pusher,
}


def _mesh_chunk_signature(chunk):
    return (
        getattr(chunk, "type", None),
        getattr(chunk, "name", None),
        _repo_path_key(getattr(chunk, "mesh", None)),
        _repo_path_key(getattr(chunk, "resource", None)),
        _repo_path_key(getattr(chunk, "mimicFace", None)),
        bool(getattr(chunk, "w2_mimic_support", False)),
        getattr(chunk, "w2_mimic_mesh_role", None),
        _repo_path_key(getattr(chunk, "w2_mimic_faces", None)),
        _repo_path_key(getattr(chunk, "_embedded_source_path", None)),
        getattr(chunk, "_embedded_cmesh_chunk_index", getattr(chunk, "_embedded_mesh_chunk_index", None)),
        getattr(chunk, "transformParent", None),
        _transform_signature(getattr(chunk, "transform", None)),
    )

def _transform_signature(transform):
    if not transform:
        return None
    keys = ("X", "Y", "Z", "Yaw", "Pitch", "Roll", "Scale_x", "Scale_y", "Scale_z")
    if isinstance(transform, dict):
        return tuple(transform.get(key) for key in keys)
    return tuple(getattr(transform, key, None) for key in keys)

def _color_signature(color):
    if not color:
        return None
    if isinstance(color, dict):
        return tuple(color.get(key) for key in ("Red", "Green", "Blue", "Alpha"))
    return tuple(getattr(color, key, None) for key in ("Red", "Green", "Blue", "Alpha"))

def _light_chunk_signature(chunk):
    return (
        getattr(chunk, "type", None),
        getattr(chunk, "name", None),
        getattr(chunk, "brightness", None),
        getattr(chunk, "radius", None),
        getattr(chunk, "innerAngle", None),
        getattr(chunk, "outerAngle", None),
        _color_signature(getattr(chunk, "color", None)),
        _transform_signature(getattr(chunk, "transform", None)),
    )

def _animated_chunk_signature(chunk):
    return (
        getattr(chunk, "type", None),
        getattr(chunk, "name", None),
        getattr(chunk, "skeleton", None),
        tuple(getattr(chunk, "animationSets", None) or []),
        getattr(chunk, "transformParent", None),
        _transform_signature(getattr(chunk, "transform", None)),
    )


def _extract_cname_array_values(prop):
    out = []
    seen = set()
    for item in getattr(prop, "Index", None) or []:
        value = None
        if hasattr(item, "String"):
            value = item.String
        elif hasattr(item, "value"):
            value = item.value
        elif hasattr(item, "name") and hasattr(item.name, "value"):
            value = item.name.value
        elif hasattr(item, "ToString"):
            try:
                value = item.ToString()
            except Exception:
                value = None
        if hasattr(value, "value"):
            value = value.value
        if not isinstance(value, str):
            continue
        value = value.strip()
        if not value or value == "CName":
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def _entity_class_from_chunks(chunks) -> str:
    chunks = list(chunks or [])
    templates = [chunk for chunk in chunks if getattr(chunk, "Type", None) == "CEntityTemplate"]

    for template in templates:
        entity_class = str(_chunk_prop_string(template, "entityClass") or "").strip()
        if entity_class and entity_class != "CName":
            return entity_class

    for template in templates:
        for prop_name in ("cookedEntityObject", "entityObject"):
            prop = _find_prop_by_name(template, prop_name)
            if prop is None:
                continue
            value = getattr(prop, "Value", None)
            indices = [value - 1] if isinstance(value, int) and value > 0 else []
            indices.extend(
                ref for ref in (getattr(handle, "Reference", None) for handle in getattr(prop, "Handles", None) or [])
                if isinstance(ref, int)
            )
            for index in indices:
                if 0 <= index < len(chunks):
                    entity_type = str(getattr(chunks[index], "Type", "") or "").strip()
                    if entity_type:
                        return entity_type

    for chunk in chunks:
        if getattr(chunk, "Type", None) == "CEntityTemplate":
            continue
        if is_entity_chunk(chunk) or hasattr(chunk, "Components"):
            return str(getattr(chunk, "Type", "") or "").strip()

    if chunks and getattr(chunks[0], "Type", None) != "CEntityTemplate":
        return str(getattr(chunks[0], "Type", "") or "").strip()
    return ""


def _entity_class_from_cr2w(cr2w_file) -> str:
    entity_class = _entity_class_from_chunks(
        getattr(getattr(cr2w_file, "CHUNKS", None), "CHUNKS", None)
    )
    if entity_class:
        return entity_class
    flat_compiled = _flat_compiled_file(cr2w_file)
    return _entity_class_from_chunks(
        getattr(getattr(flat_compiled, "CHUNKS", None), "CHUNKS", None)
    ) if flat_compiled is not None else ""


def read_entity_template_appearance_metadata(template_filename: str):
    template_filename = str(template_filename or "").strip()
    empty_result = {
        "all_names": [],
        "used_names": [],
        "default_name": "",
        "entity_class": "",
        "component_metadata_known": False,
        "has_armature_root": False,
        "has_mesh_components": False,
        "has_cloth_components": False,
        "base_has_cloth_components": False,
        "cloth_appearance_names": [],
        "has_inventory_entries": False,
    }
    if not template_filename:
        return copy.deepcopy(empty_result)

    if os.path.isabs(template_filename):
        if not os.path.exists(template_filename):
            return copy.deepcopy(empty_result)
        resolved_path = template_filename
    else:
        resolved_path = materialize_entity_repo_path(template_filename)
        if not resolved_path or not os.path.isabs(resolved_path) or not os.path.exists(resolved_path):
            return copy.deepcopy(empty_result)

    def _append_name(target, seen, value):
        name = str(value or "").strip()
        if not name:
            return
        key = name.lower()
        if key in seen:
            return
        seen.add(key)
        target.append(name)

    def _chunk_has_armature_hint(chunk) -> bool:
        if not chunk:
            return False
        chunk_type = str(getattr(chunk, "Type", "") or getattr(chunk, "name", "") or "").strip()
        if chunk_type not in {
            "CMovingPhysicalAgentComponent",
            "CAnimatedComponent",
            "CAnimDangleBufferComponent",
            "CMimicComponent",
        }:
            return False
        for prop_name in ("skeleton", "mimicFace"):
            prop = _find_prop_by_name(chunk, prop_name)
            value = _prop_to_string(prop)
            if isinstance(value, str) and value.strip():
                return True
        return False

    try:
        cr2w_file = _read_template_dependency_cr2w(resolved_path)
    except Exception:
        log.debug("Failed to read lightweight entity appearance metadata for %s", resolved_path, exc_info=True)
        return copy.deepcopy(empty_result)

    try:
        all_names = []
        used_names = []
        root_used_names = []
        all_seen = set()
        used_seen = set()
        root_used_seen = set()
        has_armature_root = False
        has_mesh_components = False
        has_cloth_components = False
        base_has_cloth_components = False
        cloth_appearance_keys = set()
        has_inventory_entries = False
        component_metadata_known = True
        entity_class = _entity_class_from_cr2w(cr2w_file)
        file_appearance_names = {}

        files_to_scan = [cr2w_file]
        if getattr(getattr(cr2w_file, "HEADER", None), "version", 999) <= 115:
            try:
                norm_path = os.path.normcase(os.path.normpath(resolved_path))
                related_files, related_complete = _load_w2_related_files_recursive(
                    cr2w_file,
                    {norm_path},
                )
                component_metadata_known = component_metadata_known and related_complete
                files_to_scan.extend(related_file for _depot_path, _full_path, related_file in related_files)
            except Exception:
                component_metadata_known = False
                log.debug("Failed to scan related Witcher 2 metadata for %s", resolved_path, exc_info=True)
            base_file_ids = {id(source_file) for source_file in files_to_scan}
        else:
            seen_paths = {os.path.normcase(os.path.normpath(resolved_path))}
            loaded_files_by_path = {
                os.path.normcase(os.path.normpath(resolved_path)): cr2w_file,
            }
            with redkit_repo_context(resolved_path):

                def _load_template_includes(queue, seen):
                    loaded_files = []
                    complete = True
                    while queue:
                        depot_path, owner_file = queue.pop(0)
                        try:
                            include_path = materialize_entity_repo_path(
                                depot_path,
                                version=getattr(getattr(owner_file, "HEADER", None), "version", 999),
                            )
                            if not include_path or not os.path.exists(include_path):
                                complete = False
                                continue
                            norm_include_path = os.path.normcase(os.path.normpath(include_path))
                            if norm_include_path in seen:
                                continue
                            seen.add(norm_include_path)
                            include_file = loaded_files_by_path.get(norm_include_path)
                            if include_file is None:
                                include_file = _read_template_dependency_cr2w(include_path)
                                loaded_files_by_path[norm_include_path] = include_file
                            loaded_files.append(include_file)
                            for include_chunk in getattr(getattr(include_file, "CHUNKS", None), "CHUNKS", None) or []:
                                if getattr(include_chunk, "Type", None) != "CEntityTemplate":
                                    continue
                                queue.extend(
                                    (included_path, include_file)
                                    for included_path in _resolve_repo_paths_from_array(
                                        include_chunk,
                                        "includes",
                                        ".w2ent",
                                    )
                                )
                        except Exception:
                            complete = False
                            log.debug("Failed to scan included entity metadata for %s", depot_path, exc_info=True)
                    return loaded_files, complete

                root_queue = [
                    (depot_path, source_file)
                    for source_file in files_to_scan
                    for chunk in getattr(getattr(source_file, "CHUNKS", None), "CHUNKS", None) or []
                    if getattr(chunk, "Type", None) == "CEntityTemplate"
                    for depot_path in _resolve_repo_paths_from_array(chunk, "includes", ".w2ent")
                ]
                included_files, includes_complete = _load_template_includes(root_queue, seen_paths)
                component_metadata_known = component_metadata_known and includes_complete
                files_to_scan.extend(included_files)

                base_files_to_scan = list(files_to_scan)
                base_file_ids = {id(source_file) for source_file in base_files_to_scan}
                all_file_ids = set(base_file_ids)

                for source_file in base_files_to_scan:
                    for chunk in getattr(getattr(source_file, "CHUNKS", None), "CHUNKS", None) or []:
                        chunk_type = getattr(chunk, "Type", None)
                        if chunk_type == "CEntityTemplate":
                            appearances = _iter_struct_items(chunk.GetVariableByName("appearances"))
                        elif chunk_type == "CEntityExternalAppearance":
                            appearance = chunk.GetVariableByName("appearance")
                            appearances = [appearance] if appearance is not None else []
                        else:
                            continue
                        for appearance in appearances:
                            appearance_name = str(
                                _prop_to_string(_find_prop_by_name(appearance, "name")) or ""
                            ).strip()
                            included_templates = _find_prop_by_name(appearance, "includedTemplates")
                            handles = list(getattr(included_templates, "Handles", None) or [])
                            depot_paths = []
                            depot_seen = set()
                            for handle in handles:
                                depot_path = _resolve_handle_repo_path(chunk, handle, ".w2ent")
                                if not depot_path:
                                    component_metadata_known = False
                                    continue
                                depot_key = _repo_path_key(depot_path)
                                if depot_key in depot_seen:
                                    continue
                                depot_seen.add(depot_key)
                                depot_paths.append(depot_path)
                            if not handles:
                                depot_path = _normalize_repo_path_value(
                                    _prop_to_string(included_templates),
                                    ".w2ent",
                                )
                                if depot_path:
                                    depot_paths.append(depot_path)
                            if not depot_paths:
                                continue
                            appearance_files, appearance_complete = _load_template_includes(
                                [(depot_path, source_file) for depot_path in depot_paths],
                                set(),
                            )
                            component_metadata_known = component_metadata_known and appearance_complete
                            if not appearance_name:
                                component_metadata_known = False
                            for appearance_file in appearance_files:
                                if appearance_name:
                                    file_appearance_names.setdefault(id(appearance_file), set()).add(
                                        appearance_name
                                    )
                                if id(appearance_file) not in all_file_ids:
                                    all_file_ids.add(id(appearance_file))
                                    files_to_scan.append(appearance_file)

        for source_file in files_to_scan:
            source_file_id = id(source_file)
            source_appearance_names = file_appearance_names.get(source_file_id, ())
            for chunk in getattr(getattr(source_file, "CHUNKS", None), "CHUNKS", None) or []:
                chunk_type = getattr(chunk, "Type", None)
                if is_entity_chunk(chunk):
                    try:
                        streaming_data = chunk.GetVariableByName("streamingDataBuffer")
                    except (AttributeError, TypeError):
                        streaming_data = _find_prop_by_name(chunk, "streamingDataBuffer")
                    if (
                        getattr(chunk, "BufferV1", False)
                        or getattr(chunk, "BufferV2", False)
                        or streaming_data is not None
                    ):
                        component_metadata_known = False
                if chunk_type == "CInventoryDefinition":
                    has_inventory_entries = True
                if not has_armature_root and _chunk_has_armature_hint(chunk):
                    has_armature_root = True
                if chunk_type in _MESH_BEARING_COMPONENT_TYPES:
                    has_mesh_components = True
                if chunk_type == "CClothComponent":
                    has_cloth_components = True
                    if source_file_id in base_file_ids:
                        base_has_cloth_components = True
                    cloth_appearance_keys.update(
                        name.lower() for name in source_appearance_names if name
                    )
                if chunk_type == "CEntityTemplate":
                    if source_file_id in base_file_ids:
                        used_prop = chunk.GetVariableByName("usedAppearances")
                        if used_prop:
                            for name in _extract_cname_array_values(used_prop):
                                _append_name(used_names, used_seen, name)
                                if source_file is cr2w_file:
                                    _append_name(root_used_names, root_used_seen, name)

                        appearances_prop = chunk.GetVariableByName("appearances")
                        for appearance in _iter_struct_items(appearances_prop):
                            name = _prop_to_string(_find_prop_by_name(appearance, "name"))
                            _append_name(all_names, all_seen, name)

                    flat_compiled = getattr(chunk, "flatCompiledData", None)
                    sub_chunks = getattr(getattr(flat_compiled, "CHUNKS", None), "CHUNKS", None) or []
                    if sub_chunks:
                        for sub_chunk in sub_chunks:
                            if not has_armature_root and _chunk_has_armature_hint(sub_chunk):
                                has_armature_root = True
                            sub_chunk_type = getattr(sub_chunk, "Type", None)
                            if sub_chunk_type in _MESH_BEARING_COMPONENT_TYPES:
                                has_mesh_components = True
                            if sub_chunk_type == "CClothComponent":
                                has_cloth_components = True
                                if source_file_id in base_file_ids:
                                    base_has_cloth_components = True
                                cloth_appearance_keys.update(
                                    name.lower() for name in source_appearance_names if name
                                )
                elif chunk_type == "CEntityExternalAppearance" and source_file_id in base_file_ids:
                    appearance = chunk.GetVariableByName("appearance")
                    name = _prop_to_string(_find_prop_by_name(appearance, "name"))
                    _append_name(all_names, all_seen, name)

        if root_used_names:
            used_names = root_used_names
        all_keys = {name.lower() for name in all_names}
        used_names = [name for name in used_names if name.lower() in all_keys]
        cloth_appearance_names = [
            name for name in all_names if name.lower() in cloth_appearance_keys
        ]
        return {
            "all_names": all_names,
            "used_names": used_names,
            "default_name": used_names[0] if used_names else (all_names[0] if all_names else ""),
            "entity_class": entity_class,
            "component_metadata_known": component_metadata_known,
            "has_armature_root": has_armature_root,
            "has_mesh_components": has_mesh_components,
            "has_cloth_components": has_cloth_components,
            "base_has_cloth_components": base_has_cloth_components,
            "cloth_appearance_names": cloth_appearance_names,
            "has_inventory_entries": has_inventory_entries,
        }
    except Exception:
        log.debug("Failed to read lightweight entity appearance metadata for %s", resolved_path, exc_info=True)
        return copy.deepcopy(empty_result)


def _find_prop_by_name(container, prop_name: str):
    if not container:
        return None
    getter = getattr(container, "GetVariableByName", None)
    if callable(getter):
        try:
            prop = getter(prop_name)
        except Exception:
            prop = None
        if prop:
            return prop
    for attr_name in ("MoreProps", "More", "PROPS"):
        for prop in getattr(container, attr_name, None) or []:
            if getattr(prop, "theName", None) == prop_name:
                return prop
    return None


def _chunk_prop_string(container, *prop_names: str, default=""):
    for prop_name in prop_names:
        value = _prop_to_string(_find_prop_by_name(container, prop_name))
        if value is not None:
            return value
    return default


def _chunk_prop_scalar(container, *prop_names, default=None):
    for prop_name in prop_names:
        prop = _find_prop_by_name(container, prop_name)
        if prop is None:
            continue
        for attr_name in ("Value", "value"):
            if hasattr(prop, attr_name):
                value = getattr(prop, attr_name)
                if isinstance(value, list):
                    continue
                return value
        value = _prop_to_string(prop)
        if value is not None:
            return value
    return default


def _chunk_prop_bool(container, *prop_names, default=None):
    value = _chunk_prop_scalar(container, *prop_names, default=default)
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    lowered = str(value).strip().lower()
    if lowered in {"true", "1", "yes"}:
        return True
    if lowered in {"false", "0", "no"}:
        return False
    return default


def _soft_handle_path(prop) -> str:
    if prop is None:
        return ""
    index_obj = getattr(prop, "Index", None)
    for attr_name in ("Path", "DepotPath"):
        path = str(getattr(index_obj, attr_name, "") or "").strip()
        if path:
            return path
    for handle in getattr(prop, "Handles", None) or []:
        path = str(getattr(handle, "DepotPath", "") or "").strip()
        if path:
            return path
    value = str(_prop_to_string(prop) or "").strip()
    return value if "\\" in value or "/" in value else ""


def _cname_array_values(prop) -> list[str]:
    values = []
    index_items = getattr(prop, "Index", None) if prop is not None else None
    if index_items is not None and not isinstance(index_items, (list, tuple)):
        index_items = [index_items]
    for item in index_items or []:
        value = str(getattr(item, "String", "") or "").strip()
        if value:
            values.append(value)
    for item in getattr(prop, "More", None) or []:
        value = str(_prop_to_string(item) or "").strip()
        if value:
            values.append(value)
    return values


def _fx_spawner_name(track_item, chunks_by_index) -> str:
    spawner_ptr = _chunk_prop_scalar(track_item, "spawner", default=None)
    try:
        spawner_index = int(spawner_ptr) - 1
    except (TypeError, ValueError):
        return ""
    spawner = chunks_by_index.get(spawner_index)
    if spawner is None:
        return ""
    component_name = _chunk_prop_string(spawner, "componentName", default="")
    if component_name:
        return component_name
    slot_names = _find_prop_by_name(spawner, "slotNames")
    values = _cname_array_values(slot_names)
    return values[0] if values else ""


def _effect_from_definition(definition, particle_items, chunks_by_index, effect_name="") -> dict:
    parsed_name = _chunk_prop_string(definition, "name", default="") if definition else ""
    result = {
        "name": str(effect_name or parsed_name or "").strip(),
        "length": float(_chunk_prop_scalar(definition, "length", default=0.0) or 0.0) if definition else 0.0,
        "loop_start": float(_chunk_prop_scalar(definition, "loopStart", default=0.0) or 0.0) if definition else 0.0,
        "loop_end": float(_chunk_prop_scalar(definition, "loopEnd", default=0.0) or 0.0) if definition else 0.0,
        "is_looped": bool(_chunk_prop_bool(definition, "isLooped", default=False)) if definition else False,
        "particle_systems": [],
    }
    for item in particle_items:
        particle_path = _soft_handle_path(_find_prop_by_name(item, "particleSystem"))
        if not particle_path:
            continue
        result["particle_systems"].append({
            "path": particle_path,
            "slot": _fx_spawner_name(item, chunks_by_index),
            "time_begin": float(_chunk_prop_scalar(item, "timeBegin", default=0.0) or 0.0),
            "duration": float(_chunk_prop_scalar(item, "timeDuration", default=0.0) or 0.0),
        })
    return result


def _parse_cooked_effect_buffer(buffer_prop, effect_name="") -> dict:
    buffer_data = getattr(buffer_prop, "Bufferdata", None)
    buffer_bytes = getattr(buffer_data, "Bytes", None)
    if not buffer_bytes:
        return {}

    effect_file = getCR2W(bReadStream(buffer_bytes, name=f"cookedEffect:{effect_name or 'unnamed'}"))
    chunks = list(getattr(getattr(effect_file, "CHUNKS", None), "CHUNKS", None) or [])
    chunks_by_index = {getattr(chunk, "ChunkIndex", -1): chunk for chunk in chunks}
    definition = next((chunk for chunk in chunks if getattr(chunk, "Type", "") == "CFXDefinition"), None)
    particle_items = [chunk for chunk in chunks if getattr(chunk, "Type", "") == "CFXTrackItemParticles"]
    return _effect_from_definition(definition, particle_items, chunks_by_index, effect_name)


def _extract_uncooked_entity_effects(template_chunk, chunks) -> list[dict]:
    chunks_by_index = {getattr(chunk, "ChunkIndex", -1): chunk for chunk in chunks}

    def _referenced(container, prop_name):
        return [
            chunk
            for ptr in _prop_handle_list(_find_prop_by_name(container, prop_name))
            if (chunk := chunks_by_index.get(ptr - 1)) is not None
        ]

    results = []
    for definition in _referenced(template_chunk, "effects"):
        if getattr(definition, "Type", "") != "CFXDefinition":
            continue
        particle_items = []
        for group in _referenced(definition, "trackGroups"):
            for track in _referenced(group, "tracks"):
                particle_items.extend(
                    item
                    for item in _referenced(track, "trackItems")
                    if getattr(item, "Type", "") == "CFXTrackItemParticles"
                )
        results.append(_effect_from_definition(definition, particle_items, chunks_by_index))
    return results


def _extract_cooked_entity_effects(template_chunk) -> list[dict]:
    cooked_effects = _find_prop_by_name(template_chunk, "cookedEffects")
    raw_items = list(getattr(cooked_effects, "More", None) or [])
    if not raw_items:
        return []

    effect_entries = []
    flat_props = []
    for item in raw_items:
        nested_props = list(getattr(item, "MoreProps", None) or [])
        if nested_props:
            effect_entries.append(nested_props)
        else:
            flat_props.append(item)

    current = []
    for prop in flat_props:
        prop_name = str(getattr(prop, "theName", "") or "")
        if prop_name == "name" and current:
            effect_entries.append(current)
            current = []
        current.append(prop)
        if prop_name == "buffer":
            effect_entries.append(current)
            current = []
    if current:
        effect_entries.append(current)

    results = []
    for props in effect_entries:
        name_prop = next((prop for prop in props if getattr(prop, "theName", "") == "name"), None)
        buffer_prop = next((prop for prop in props if getattr(prop, "theName", "") == "buffer"), None)
        if buffer_prop is None:
            continue
        effect_name = str(_prop_to_string(name_prop) or "").strip()
        try:
            effect = _parse_cooked_effect_buffer(buffer_prop, effect_name)
        except Exception:
            log.warning(
                "Failed to parse cooked entity effect '%s' from %s",
                effect_name or "<unnamed>",
                getattr(getattr(template_chunk, "_W_CLASS__CR2WFILE", None), "fileName", "<unknown>"),
                exc_info=True,
            )
            continue
        if effect:
            results.append(effect)
    return results


def _chunk_prop_float(container, *prop_names, default=None):
    value = _chunk_prop_scalar(container, *prop_names, default=default)
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _chunk_prop_int(container, *prop_names, default=None):
    value = _chunk_prop_scalar(container, *prop_names, default=default)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default


def _chunk_prop_vector4(container, *prop_names, default=None):
    for prop_name in prop_names:
        prop = _find_prop_by_name(container, prop_name)
        if prop is None:
            continue
        for attr_name in ("Value", "value"):
            direct = getattr(prop, attr_name, None)
            if isinstance(direct, (list, tuple)) and len(direct) >= 4:
                try:
                    return tuple(float(direct[i]) for i in range(4))
                except (TypeError, ValueError):
                    pass
        values = {}
        for attr_name in ("MoreProps", "More", "PROPS"):
            for child in getattr(prop, attr_name, None) or []:
                name = str(getattr(child, "theName", "") or "")
                if name in {"X", "Y", "Z", "W"}:
                    value = getattr(child, "Value", getattr(child, "value", None))
                    if value is not None:
                        values[name] = value
        if values:
            try:
                return (
                    float(values.get("X", 0.0)),
                    float(values.get("Y", 0.0)),
                    float(values.get("Z", 0.0)),
                    float(values.get("W", 0.0)),
                )
            except (TypeError, ValueError):
                pass
        text = _prop_to_string(prop)
        if text:
            parts = [part.strip() for part in text.replace(";", ",").split(",")]
            if len(parts) >= 4:
                try:
                    return tuple(float(parts[i]) for i in range(4))
                except (TypeError, ValueError):
                    pass
    return default


def _dyng_constraint_settings_from_chunk(chunk):
    settings = {
        "dampening": _chunk_prop_float(chunk, "m_dampening", "dampening"),
        "gravity": _chunk_prop_float(chunk, "m_gravity", "gravity"),
        "speed": _chunk_prop_float(chunk, "m_speed", "speed"),
        "wind": _chunk_prop_float(chunk, "m_wind", "wind"),
        "shake": _chunk_prop_float(chunk, "m_shake", "shake"),
        "dt": _chunk_prop_float(chunk, "m_dt", "dt"),
        "useOffsets": _chunk_prop_bool(chunk, "m_useOffsets", "useOffsets"),
        "planeCollision": _chunk_prop_bool(chunk, "m_planeCollision", "planeCollision"),
        "maxLinksIterations": _chunk_prop_int(
            chunk,
            "m_max_links_iterations",
            "m_maxLinksIterations",
            "maxLinksIterations",
        ),
    }
    return {key: value for key, value in settings.items() if value is not None}


def _breast_constraint_settings_from_chunk(chunk):
    settings = {
        "preset": _chunk_prop_string(chunk, "m_preset", "preset", default=None),
        "simTime": _chunk_prop_float(chunk, "m_simTime", "simTime"),
        "ellipse": _chunk_prop_vector4(chunk, "m_elA", "elA", "ellipse"),
        "startSimPointOffset": _chunk_prop_float(chunk, "m_startSimPointOffset", "startSimPointOffset"),
        "velDamp": _chunk_prop_float(chunk, "m_velDamp", "velDamp"),
        "bounceDamp": _chunk_prop_float(chunk, "m_bounceDamp", "bounceDamp"),
        "inAcc": _chunk_prop_float(chunk, "m_inAcc", "inAcc"),
        "inertiaScaler": _chunk_prop_float(chunk, "m_inertiaScaler", "inertiaScaler"),
        "blackHole": _chunk_prop_float(chunk, "m_blackHole", "blackHole"),
        "velClamp": _chunk_prop_float(chunk, "m_velClamp", "velClamp"),
        "gravity": _chunk_prop_float(chunk, "m_gravity", "gravity"),
        "movementBoneWeight": _chunk_prop_float(chunk, "m_movementBoneWeight", "movementBoneWeight"),
        "rotationBoneWeight": _chunk_prop_float(chunk, "m_rotationBoneWeight", "rotationBoneWeight"),
    }
    return {key: value for key, value in settings.items() if value is not None}


def _iter_struct_items(prop):
    items = list(getattr(prop, "More", None) or [])
    if not items:
        return []
    if getattr(prop, "Count", None) == 1 and all(getattr(item, "theName", None) for item in items):
        return [prop]
    return items


def _handle_value_to_chunk(CHUNKS, handle_value):
    """Resolve a 1-based CR2W handle/soft value to its chunk (CHUNKS is 0-based)."""
    try:
        idx = int(handle_value) - 1
    except (TypeError, ValueError):
        return None
    if 0 <= idx < len(CHUNKS):
        return CHUNKS[idx]
    return None


def _prop_handle_list(prop):
    """Return the list of (1-based) chunk indices referenced by an array-of-handles prop."""
    if prop is None:
        return []
    value = getattr(prop, "value", None)
    if isinstance(value, list):
        return [v for v in value if isinstance(v, int)]
    return []


def _extract_w2_ragdoll_meta(component_chunk, CHUNKS):
    base_body = _chunk_prop_string(component_chunk, "baseBodyName") or None
    motion = _prop_to_string(_find_prop_by_name(component_chunk, "initialMotionType")) or None

    ragdoll_handle = _find_prop_by_name(component_chunk, "ragdollResource")
    ragdoll_value = _prop_to_string(ragdoll_handle)
    ragdoll_chunk = _handle_value_to_chunk(CHUNKS, ragdoll_value)
    if ragdoll_chunk is None or getattr(ragdoll_chunk, "Type", None) != "CRagdoll":
        if not (base_body or motion):
            return None
        return {"base_body": base_body, "initial_motion_type": motion}

    meta = {
        "base_body": base_body,
        "initial_motion_type": motion,
        "import_file": _chunk_prop_string(ragdoll_chunk, "importFile") or None,
        "skeleton_low": _chunk_prop_string(ragdoll_chunk, "lowResSkeletonName") or None,
        "skeleton_high": _chunk_prop_string(ragdoll_chunk, "highResSkeletonName") or None,
        "bodies": [],
        "num_constraints": len(_prop_handle_list(_find_prop_by_name(ragdoll_chunk, "ragdollConstraints"))),
    }
    for body_handle in _prop_handle_list(_find_prop_by_name(ragdoll_chunk, "ragdollBodies")):
        body = _handle_value_to_chunk(CHUNKS, body_handle)
        if body is None or getattr(body, "Type", None) != "CRagdollBody":
            continue
        meta["bodies"].append({
            "name": _chunk_prop_string(body, "bodyName") or None,
            "inertia": _prop_to_string(_find_prop_by_name(body, "inertiaFactor")),
            "gravity": _prop_to_string(_find_prop_by_name(body, "gravityFactor")),
        })
    return meta


def _find_chunk_by_name(chunks, name, chunk_type=None):
    wanted = str(name or "").strip().lower()
    if not wanted:
        return None
    secondary_match = None
    for chunk in chunks or []:
        if chunk_type and getattr(chunk, "Type", None) != chunk_type:
            continue
        chunk_name = _prop_to_string(_find_prop_by_name(chunk, "name"))
        if not chunk_name:
            continue
        chunk_key = chunk_name.strip().lower()
        if chunk_key == wanted:
            return chunk
        if secondary_match is None and chunk_key.lstrip("_") == wanted.lstrip("_"):
            secondary_match = chunk
    return secondary_match


def _w2_part_key(value: str) -> str:
    return str(value or "").strip().lower().lstrip("_")


def _w2_compact_part_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", _w2_part_key(value))


def _w2_visual_chunk_matches_part(chunk, part_name: str) -> bool:
    chunk_type = getattr(chunk, "Type", None) or getattr(chunk, "type", None)
    if not chunk or chunk_type not in _VISUAL_MESH_COMPONENT_TYPES:
        return False

    part_key = _w2_part_key(part_name)
    if not part_key:
        return False

    part_compact = _w2_compact_part_key(part_key)
    chunk_name = _w2_part_key(
        getattr(chunk, "name", None)
        or _prop_to_string(_find_prop_by_name(chunk, "name"))
    )
    if chunk_name:
        if chunk_name == part_key or chunk_name == part_key.lstrip("_"):
            return True
        if part_compact and _w2_compact_part_key(chunk_name) == part_compact:
            return True

    return False


def _iter_w2_body_parts(template_chunk):
    body_parts_prop = _find_prop_by_name(template_chunk, "bodyParts")
    if not body_parts_prop:
        return
    for body_part in _iter_struct_items(body_parts_prop):
        part_name = _prop_to_string(_find_prop_by_name(body_part, "name"))
        if part_name:
            yield part_name, body_part


def _iter_w2_body_part_states(body_part_element):
    states_prop = _find_prop_by_name(body_part_element, "states")
    if not states_prop:
        return
    for state in _iter_struct_items(states_prop):
        yield _prop_to_string(_find_prop_by_name(state, "name")), state


def _iter_w2_component_refs(state_property_or_element):
    components_prop = _find_prop_by_name(state_property_or_element, "componentsInUse")
    if not components_prop:
        return
    for component_ref in _iter_struct_items(components_prop):
        component_name = _prop_to_string(_find_prop_by_name(component_ref, "name"))
        class_name = _prop_to_string(_find_prop_by_name(component_ref, "className"))
        if component_name:
            yield component_name, class_name


def _handle_ref_chunk(chunk, handle):
    if not chunk or not handle or not getattr(handle, "ChunkHandle", False):
        return None
    cr2w_file = getattr(chunk, "_W_CLASS__CR2WFILE", None)
    ref_idx = getattr(handle, "Reference", None)
    if not cr2w_file or not isinstance(ref_idx, int):
        return None
    if 0 <= ref_idx < len(cr2w_file.CHUNKS.CHUNKS):
        return cr2w_file.CHUNKS.CHUNKS[ref_idx]
    return None


def _w2_embedded_mesh_ref_info(source_chunk):
    if not source_chunk:
        return None, None
    cr2w_file = getattr(source_chunk, "_W_CLASS__CR2WFILE", None)
    version = getattr(getattr(cr2w_file, "HEADER", None), "version", 999)
    file_name = getattr(cr2w_file, "fileName", None)
    if version > 115 or not file_name:
        return None, None

    if getattr(source_chunk, "Type", None) == "CMesh":
        chunk_index = getattr(source_chunk, "ChunkIndex", None)
        return file_name, chunk_index if isinstance(chunk_index, int) else None

    try:
        mesh_prop = source_chunk.GetVariableByName("mesh")
    except Exception:
        mesh_prop = None
    for handle in getattr(mesh_prop, "Handles", None) or []:
        ref_chunk = _handle_ref_chunk(source_chunk, handle)
        if getattr(ref_chunk, "Type", None) == "CMesh":
            chunk_index = getattr(ref_chunk, "ChunkIndex", None)
            return file_name, chunk_index if isinstance(chunk_index, int) else None
    return None, None


def _attach_w2_embedded_mesh_info(source_chunk, converted_chunk):
    source_path, embedded_cmesh_chunk_index = _w2_embedded_mesh_ref_info(source_chunk)
    if source_path and embedded_cmesh_chunk_index is not None:
        setattr(converted_chunk, "_embedded_source_path", source_path)
        setattr(converted_chunk, "_embedded_cmesh_chunk_index", embedded_cmesh_chunk_index)
        if not is_valid_mesh_path(getattr(converted_chunk, "mesh", None)):
            setattr(converted_chunk, "mesh", source_path)
    return converted_chunk


def _coerce_w2_mesh_component_for_io(source_chunk, converted_chunk):
    if getattr(source_chunk, "Type", None) == "CDressMeshComponent":
        # W2 cooked dress components usually point at an editor/template rig while
        # carrying their actual mesh/skeleton payload inline. Do not require that
        # external template rig during cooked-only imports.
        setattr(converted_chunk, "skeleton", None)
    return converted_chunk


def _convert_chunk_for_model(chunk):
    if not chunk:
        return None
    if chunk.Type in _VISUAL_MESH_COMPONENT_TYPES:
        component = _coerce_w2_mesh_component_for_io(chunk, CMeshComponent(chunk).convert_for_io())
        component.mesh = _resolve_mesh_path(chunk, getattr(component, "mesh", None))
        _attach_w2_embedded_mesh_info(chunk, component)
        return component if component.mesh else None
    if chunk.Type == "CClothComponent":
        resource = _resolve_repo_path(chunk, "resource", (".redcloth", ".redapex"))
        _cname_prop = chunk.GetVariableByName("name")
        _cname = str(_prop_to_string(_cname_prop) or "").strip()
        return CClothComponent(resource, name=_cname) if resource else None
    if chunk.Type == "CMorphedMeshComponent":
        morph_target = _resolve_repo_path(chunk, "morphTarget", ".w2mesh")
        morph_source = _resolve_repo_path(chunk, "morphSource", ".w2mesh")
        morph_component_id = _prop_to_string(_find_prop_by_name(chunk, "morphComponentId"))
        return CMorphedMeshComponent(morph_target, morph_source, morph_component_id)
    return None


def _make_mesh_proxy_chunk(source_chunk, name: str, mesh_path: str, skeleton: str | None = None):
    proxy = JsonChunk()
    proxy.type = "CMeshComponent"
    proxy.chunkIndex = getattr(source_chunk, "ChunkIndex", 0)
    proxy.name = name
    proxy.mesh = mesh_path
    proxy.skeleton = skeleton
    _attach_w2_embedded_mesh_info(source_chunk, proxy)
    return proxy


# ============================================================
# Witcher 2 mimic support
# ============================================================
# W2 character heads define face resources through CHeadDefinifion
# (mimic mesh + mimic skeleton + .w2faces pose banks). Keep this as
# explicit W2 metadata so the W3 .w3fac/CMimicFace import path stays isolated.

def _resolve_w2_head_mesh_refs(head_chunk, prop_name):
    prop = _find_prop_by_name(head_chunk, prop_name)
    if not prop:
        return []

    out = []
    seen = set()
    for handle in getattr(prop, "Handles", None) or []:
        mesh_path = _resolve_handle_repo_path(head_chunk, handle, ".w2mesh")
        if not mesh_path:
            continue
        mesh_key = _repo_path_key(mesh_path)
        if mesh_key in seen:
            continue
        seen.add(mesh_key)
        out.append((mesh_path, _handle_ref_chunk(head_chunk, handle) or head_chunk))
    return out


def _resolve_w2_head_resource(chunk, prop_name, expected_ext):
    return _resolve_repo_path(chunk, prop_name, expected_ext)


def _w2_embedded_head_resource_ref_info(head_chunk, prop_name, expected_type):
    prop = _find_prop_by_name(head_chunk, prop_name)
    if not prop:
        return "", None
    cr2w_file = getattr(head_chunk, "_W_CLASS__CR2WFILE", None)
    file_name = getattr(cr2w_file, "fileName", None)
    if not file_name:
        return "", None
    for handle in getattr(prop, "Handles", None) or []:
        ref_chunk = _handle_ref_chunk(head_chunk, handle)
        if getattr(ref_chunk, "Type", None) != expected_type:
            continue
        chunk_index = getattr(ref_chunk, "ChunkIndex", None)
        return file_name, chunk_index if isinstance(chunk_index, int) else None
    return "", None


def _w2_prop_int(prop, default=None):
    if prop is None:
        return default
    value = getattr(prop, "Value", None)
    if value is None:
        value = getattr(prop, "value", None)
    try:
        return int(value)
    except Exception:
        return default


def _w2_prop_float(prop, default=0.0):
    if prop is None:
        return default
    value = getattr(prop, "Value", None)
    if value is None:
        value = getattr(prop, "value", None)
    try:
        return float(value)
    except Exception:
        return default


def _parse_w2_sbone_mapping(head_chunk, prop_name):
    prop = _find_prop_by_name(head_chunk, prop_name)
    if not prop:
        return []

    out = []
    for item in _iter_struct_items(prop):
        bone_a = _w2_prop_int(_find_prop_by_name(item, "boneA"), None)
        bone_b = _w2_prop_int(_find_prop_by_name(item, "boneB"), None)
        if bone_a is None or bone_b is None:
            continue
        out.append([bone_a, bone_b])
    return out


def _make_w2_mimic_proxy_chunk(source_chunk, head_name, mesh_path, mesh_role, metadata):
    proxy = _make_mesh_proxy_chunk(
        source_chunk,
        f"{head_name}_w2_mimic_{mesh_role}",
        mesh_path,
        metadata.get("mimic_skeleton") or None,
    )
    proxy.type = "CW2MimicHeadComponent"
    proxy.w2_mimic_support = True
    proxy.w2_mimic_head_name = head_name
    proxy.w2_mimic_mesh_role = mesh_role
    proxy.w2_mimic_mesh = mesh_path
    proxy.w2_mimic_mesh_high = metadata.get("mimic_mesh_high", "")
    proxy.w2_mimic_mesh_low = metadata.get("mimic_mesh_low", "")
    proxy.w2_mimic_skeleton = metadata.get("mimic_skeleton", "")
    proxy.w2_mimic_pose_skeleton = metadata.get("pose_skeleton", "")
    proxy.w2_mimic_parent_skeleton = metadata.get("parent_skeleton", "")
    proxy.w2_mimic_float_track_skeleton = metadata.get("float_track_skeleton", "")
    proxy.w2_mimic_skeleton_embedded_source = metadata.get("skeleton_embedded_source", "")
    proxy.w2_mimic_skeleton_embedded_chunk_index = metadata.get("skeleton_embedded_chunk_index", -1)
    proxy.w2_mimic_embedded_skeleton_data = metadata.get("embedded_skeleton_data")
    proxy.w2_mimic_faces = metadata.get(f"mimic_faces_{mesh_role}", "")
    proxy.w2_mimic_faces_high = metadata.get("mimic_faces_high", "")
    proxy.w2_mimic_faces_low = metadata.get("mimic_faces_low", "")
    proxy.w2_mimic_faces_high_embedded_source = metadata.get("faces_high_embedded_source", "")
    proxy.w2_mimic_faces_high_embedded_chunk_index = metadata.get("faces_high_embedded_chunk_index", -1)
    proxy.w2_mimic_faces_low_embedded_source = metadata.get("faces_low_embedded_source", "")
    proxy.w2_mimic_faces_low_embedded_chunk_index = metadata.get("faces_low_embedded_chunk_index", -1)
    proxy.w2_mimic_bone_mapping = metadata.get("bone_mapping", [])
    proxy.w2_mimic_bone_mapping_low = metadata.get("bone_mapping_low", [])
    proxy.w2_mimic_dist_for_default_head = metadata.get("dist_for_default_head", 0.0)
    proxy.w2_mimic_parent_slot_name = metadata.get("parent_slot_name", "")
    return proxy


def _resolve_w2_body_part_chunks(template_chunk, part_names, chunks):
    wanted_parts = []
    seen_part_keys = set()
    for part in part_names or []:
        part_key = _w2_part_key(part)
        if not part_key or part_key in seen_part_keys:
            continue
        seen_part_keys.add(part_key)
        wanted_parts.append(str(part or "").strip())
    if not wanted_parts:
        return []

    candidate_templates = []
    seen_templates = set()

    def _add_template(candidate):
        if not candidate or getattr(candidate, "Type", None) != "CEntityTemplate":
            return
        marker = id(candidate)
        if marker in seen_templates:
            return
        seen_templates.add(marker)
        candidate_templates.append(candidate)

    _add_template(template_chunk)
    for candidate in chunks or []:
        if _find_prop_by_name(candidate, "bodyParts"):
            _add_template(candidate)

    body_parts = {}
    for candidate in candidate_templates:
        for part_name, body_part in _iter_w2_body_parts(candidate):
            part_key = part_name.lower()
            body_parts[part_key] = body_part
            body_parts.setdefault(part_key.lstrip("_"), body_part)

    resolved_chunks = []
    seen = set()
    def _append_resolved_chunk(target, seen, ref_chunk):
        if not ref_chunk:
            return False
        signature = (getattr(ref_chunk, "Type", None), getattr(ref_chunk, "ChunkIndex", None))
        if signature in seen:
            return False
        seen.add(signature)
        target.append(ref_chunk)
        return True

    for part_name in wanted_parts:
        part_key = _w2_part_key(part_name)
        body_part = body_parts.get(part_key) or body_parts.get(part_key.lstrip("_"))
        found_part_chunk = False

        if body_part:
            states = list(_iter_w2_body_part_states(body_part))
            preferred_states = [state for state in states if str(state[0] or "").lower() == "default"] or states[:1]
            for _, state in preferred_states:
                for component_name, class_name in _iter_w2_component_refs(state):
                    ref_chunk = _find_chunk_by_name(chunks, component_name, class_name)
                    if _append_resolved_chunk(resolved_chunks, seen, ref_chunk):
                        found_part_chunk = True

        if found_part_chunk:
            continue

        # Some W2 character templates list appearance parts directly by component
        # name (body_1, legs_1, skirt_1, etc.) without a bodyParts table.
        for ref_chunk in chunks or []:
            if _w2_visual_chunk_matches_part(ref_chunk, part_name):
                _append_resolved_chunk(resolved_chunks, seen, ref_chunk)
    return resolved_chunks


def _collect_w2_body_part_chunk_indices(template_chunk, chunks):
    chunk_indices = set()
    for _, body_part in _iter_w2_body_parts(template_chunk):
        for _, state in _iter_w2_body_part_states(body_part):
            for component_name, class_name in _iter_w2_component_refs(state):
                ref_chunk = _find_chunk_by_name(chunks, component_name, class_name)
                if not ref_chunk:
                    continue
                chunk_index = getattr(ref_chunk, "ChunkIndex", None)
                if isinstance(chunk_index, int) and chunk_index > 0:
                    chunk_indices.add(chunk_index)
    return chunk_indices


def _collect_w2_body_part_component_names(template_chunk):
    names = set()
    for _, body_part in _iter_w2_body_parts(template_chunk):
        for _, state in _iter_w2_body_part_states(body_part):
            for component_name, _class_name in _iter_w2_component_refs(state):
                if component_name:
                    names.add(str(component_name).strip().lower())
    return names


def _collect_w2_body_part_state_table(template_chunk):
    states_by_part = {}
    for part_name, body_part in _iter_w2_body_parts(template_chunk):
        part_key = _w2_part_key(part_name)
        if not part_key:
            continue
        state_map = states_by_part.setdefault(part_key, {})
        for state_name, state in _iter_w2_body_part_states(body_part):
            state_key = str(state_name or "").strip().lower()
            if not state_key:
                continue
            components = [
                str(component_name or "").strip()
                for component_name, _class_name in _iter_w2_component_refs(state)
                if str(component_name or "").strip()
            ]
            if components:
                state_map[state_key] = components
    return states_by_part


def _merge_w2_body_part_state_table(target, source):
    target = dict(target or {})
    for part_name, states in (source or {}).items():
        part_key = _w2_part_key(part_name)
        if not part_key:
            continue
        target_states = target.setdefault(part_key, {})
        for state_name, components in (states or {}).items():
            state_key = str(state_name or "").strip().lower()
            if not state_key or state_key in target_states:
                continue
            values = [
                str(component or "").strip()
                for component in (components or [])
                if str(component or "").strip()
            ]
            if values:
                target_states[state_key] = values
    return target


def _iter_w2_head_param_names(chunks):
    seen = set()

    def _yield_head_name(head_chunk):
        if getattr(head_chunk, "Type", None) != "CHeadDefinifion":
            return None
        head_name = str(_prop_to_string(_find_prop_by_name(head_chunk, "name")) or "").strip()
        if not head_name:
            return None
        key = head_name.lower()
        if key in seen:
            return None
        seen.add(key)
        return head_name

    for chunk in chunks or []:
        if getattr(chunk, "Type", None) != "CHeadParam":
            continue
        heads_prop = _find_prop_by_name(chunk, "heads")
        for handle in getattr(heads_prop, "Handles", None) or []:
            head_name = _yield_head_name(_handle_ref_chunk(chunk, handle))
            if head_name:
                yield head_name

    for chunk in chunks or []:
        head_name = _yield_head_name(chunk)
        if head_name:
            yield head_name


def _w2_head_parent_slot_name(chunks):
    for chunk in chunks or []:
        if getattr(chunk, "Type", None) != "CHeadAttachment":
            continue
        slot_name = str(_prop_to_string(_find_prop_by_name(chunk, "parentSlotName")) or "").strip()
        if slot_name:
            return slot_name
    return ""


def _w2_embedded_skeleton_data_for_plan(cr2w_file, chunk_index):
    source_path = str(getattr(cr2w_file, "fileName", "") or "").strip()
    if not source_path or not os.path.isfile(source_path):
        return None
    try:
        chunk_index = int(chunk_index)
    except Exception:
        return None
    if chunk_index < 0:
        return None
    try:
        from .dc_skeleton import _read_w2_mimic_skeleton, read_skelly

        with open(source_path, "rb") as source_file:
            raw_data = source_file.read()
        chunks = list(getattr(getattr(cr2w_file, "CHUNKS", None), "CHUNKS", None) or [])
        for candidate_index in dict.fromkeys((chunk_index, chunk_index - 1)):
            if not (0 <= candidate_index < len(chunks)):
                continue
            chunk = chunks[candidate_index]
            if getattr(chunk, "Type", None) != "CSkeleton":
                continue
            rig = _read_w2_mimic_skeleton(cr2w_file, raw_data, candidate_index)
            if rig is None or not getattr(rig, "names", None):
                rig = read_skelly(chunk)
            if not getattr(rig, "names", None):
                continue
            skeleton_data = readCSkeletonData(rig)
            bone_names = {
                str(getattr(bone, "name", "") or "")
                for bone in getattr(skeleton_data, "bones", []) or []
            }
            if {"Rootface", "head_face"}.intersection(bone_names):
                return skeleton_data
    except Exception:
        log.debug(
            "Failed to embed W2 mimic skeleton data for %s #%s",
            source_path,
            chunk_index,
            exc_info=True,
        )
    return None


def _build_w2_head_chunks(chunks, head_name, cr2w_file=None):
    if not head_name:
        return []
    head_chunk = _find_chunk_by_name(chunks, head_name, "CHeadDefinifion")
    if not head_chunk:
        return []

    head_chunks = []
    seen = set()
    mimic_high_refs = _resolve_w2_head_mesh_refs(head_chunk, "meshesForMimicHighHead")
    mimic_low_refs = _resolve_w2_head_mesh_refs(head_chunk, "meshesForMimicLowHead")
    has_mimic_mesh = bool(mimic_high_refs or mimic_low_refs)
    dist_for_default_head = _w2_prop_float(_find_prop_by_name(head_chunk, "distForDefaultHead"), 0.0)
    parent_slot_name = _w2_head_parent_slot_name(chunks)
    for mesh_path, source_chunk in _resolve_w2_head_mesh_refs(head_chunk, "meshesForBaseHead"):
        mesh_key = _repo_path_key(mesh_path)
        if mesh_key in seen:
            continue
        seen.add(mesh_key)
        # poseSkeleton in cooked W2 head definitions often points to an embedded
        # mimic/face skeleton, not a mesh skinning rig. Do not coerce it into an
        # external .w2rig path for normal mesh import.
        proxy = _make_mesh_proxy_chunk(source_chunk, head_name, mesh_path, None)
        proxy.w2_head_support = True
        proxy.w2_head_name = head_name
        proxy.w2_head_mesh_role = "base"
        proxy.w2_head_hide_when_mimic_available = has_mimic_mesh
        proxy.w2_head_dist_for_default_head = dist_for_default_head
        proxy.w2_head_parent_slot_name = parent_slot_name
        head_chunks.append(proxy)

    # Witcher 2 mimic support: carry the high/low mimic resources alongside the
    # base head as a W2-only proxy chunk. The Blender importer consumes this
    # without setting W3 mimicFace/mimicFaceFile properties.
    # In W2 heads, poseSkeleton is the actual face rig used by the mimic mesh.
    # mimicSkeleton commonly points at the float-track skeleton.
    pose_skeleton = _resolve_w2_head_resource(head_chunk, "poseSkeleton", ".w2rig")
    float_track_skeleton = _resolve_w2_head_resource(head_chunk, "mimicSkeleton", ".w2rig")
    skeleton_embedded_source, skeleton_embedded_chunk_index = _w2_embedded_head_resource_ref_info(
        head_chunk,
        "poseSkeleton",
        "CSkeleton",
    )
    faces_high_embedded_source, faces_high_embedded_chunk_index = _w2_embedded_head_resource_ref_info(
        head_chunk,
        "mimicFacesHigh",
        "CMimicFaces",
    )
    faces_low_embedded_source, faces_low_embedded_chunk_index = _w2_embedded_head_resource_ref_info(
        head_chunk,
        "mimicFacesLow",
        "CMimicFaces",
    )
    metadata = {
        "mimic_mesh_high": mimic_high_refs[0][0] if mimic_high_refs else "",
        "mimic_mesh_low": mimic_low_refs[0][0] if mimic_low_refs else "",
        "mimic_skeleton": pose_skeleton or float_track_skeleton or "",
        "pose_skeleton": pose_skeleton or "",
        "parent_skeleton": _resolve_w2_head_resource(head_chunk, "parentSkeleton", ".w2rig") or "",
        "float_track_skeleton": float_track_skeleton or "",
        "skeleton_embedded_source": skeleton_embedded_source or "",
        "skeleton_embedded_chunk_index": skeleton_embedded_chunk_index if skeleton_embedded_chunk_index is not None else -1,
        "mimic_faces_high": _resolve_w2_head_resource(head_chunk, "mimicFacesHigh", ".w2faces") or "",
        "mimic_faces_low": _resolve_w2_head_resource(head_chunk, "mimicFacesLow", ".w2faces") or "",
        "faces_high_embedded_source": faces_high_embedded_source or "",
        "faces_high_embedded_chunk_index": faces_high_embedded_chunk_index if faces_high_embedded_chunk_index is not None else -1,
        "faces_low_embedded_source": faces_low_embedded_source or "",
        "faces_low_embedded_chunk_index": faces_low_embedded_chunk_index if faces_low_embedded_chunk_index is not None else -1,
        "bone_mapping": _parse_w2_sbone_mapping(head_chunk, "boneMapping"),
        "bone_mapping_low": _parse_w2_sbone_mapping(head_chunk, "boneMappingLow"),
        "dist_for_default_head": dist_for_default_head,
        "parent_slot_name": parent_slot_name,
    }
    if cr2w_file is not None and skeleton_embedded_chunk_index is not None:
        metadata["embedded_skeleton_data"] = _w2_embedded_skeleton_data_for_plan(
            cr2w_file,
            skeleton_embedded_chunk_index,
        )
    preferred_ref = mimic_high_refs[:1] or mimic_low_refs[:1]
    if preferred_ref and (metadata["mimic_skeleton"] or metadata["mimic_faces_high"] or metadata["mimic_faces_low"]):
        mesh_role = "high" if mimic_high_refs else "low"
        mesh_path, source_chunk = preferred_ref[0]
        head_chunks.append(_make_w2_mimic_proxy_chunk(source_chunk, head_name, mesh_path, mesh_role, metadata))
    return head_chunks


def _build_w2_cooked_appearance_template(file, template_chunk, appearance, current_app, chunks, base_mesh_paths):
    template_filename = f"{Path(file.fileName).stem}:{current_app.name}:cooked_bodyparts"
    model_ent = ModelEnt(template_filename, current_app.name)
    model_ent.plan_complete = True
    seen_mesh_paths = set(base_mesh_paths or [])
    seen_signatures = set()

    parts_prop = _find_prop_by_name(appearance, "parts")
    part_names = _extract_cname_array_values(parts_prop) if parts_prop else []
    for source_chunk in _resolve_w2_body_part_chunks(template_chunk, part_names, chunks):
        converted_chunk = _convert_chunk_for_model(source_chunk)
        if not converted_chunk:
            continue
        mesh_path = getattr(converted_chunk, "mesh", None)
        if mesh_path:
            mesh_key = _repo_path_key(mesh_path)
            if mesh_key in seen_mesh_paths:
                continue
            seen_mesh_paths.add(mesh_key)
        converted_chunk.type = source_chunk.Type
        converted_chunk.chunkIndex = source_chunk.ChunkIndex
        chunk_signature = _mesh_chunk_signature(converted_chunk)
        if chunk_signature in seen_signatures:
            continue
        seen_signatures.add(chunk_signature)
        model_ent.chunks.append(converted_chunk)

    head_name = _prop_to_string(_find_prop_by_name(appearance, "headName")) or getattr(current_app, "headName", None)
    for head_chunk in _build_w2_head_chunks(chunks, head_name, file):
        mesh_path = getattr(head_chunk, "mesh", None)
        if mesh_path:
            mesh_key = _repo_path_key(mesh_path)
            if mesh_key in seen_mesh_paths:
                continue
            seen_mesh_paths.add(mesh_key)
        chunk_signature = _mesh_chunk_signature(head_chunk)
        if chunk_signature in seen_signatures:
            continue
        seen_signatures.add(chunk_signature)
        model_ent.chunks.append(head_chunk)

    return model_ent if model_ent.chunks else None

def chunk_append(new_mesh, chunk, item, added_chunks=None):
    _attach_w2_embedded_mesh_info(chunk, item)
    new_mesh.chunks.append(item)
    new_mesh.chunks[-1].type = chunk.Type
    new_mesh.chunks[-1].chunkIndex = chunk.ChunkIndex
    if added_chunks is not None:
        added_chunks.add(chunk.ChunkIndex)

# Structural, metadata, or non-visual chunk types that should not warn when
# encountered in template parsing.
_KNOWN_STRUCTURAL_CHUNKS = {
    'CWetnessComponent',
    'CItemEntity', # Handled separately in ReadTemplate (streaming buffer path).
    "CEquipmentDefinition",
    "CEquipmentDefinitionEntry",
    "CEntityTemplate",
    "CEntity",
    "CGameplayEntity",
    "CInventoryComponent",
    "CInventoryDefinition",
    "CInventoryDefinitionEntry",
    "CInventoryInitializerRandom",
    "CInventoryInitializerUniform",
    "CActor",
    "CNewNPC",
    "CR4Player",
    "W3PlayerWitcher",
    "W3ReplacerCiri",
    "CNormalBlendComponent",
    "CNormalBlendAttachment",
    "CMaterialInstance",
    "CMovingPhysicalAgentComponent",
    "CExternalProxyComponent",
    "CExternalProxyAttachment",
    "CDropPhysicsSetup",
    "CDynamicColliderComponent",
    "CEffectDummyComponent",
    "CSoundEntityParam",
    "CHeadParam",
    "CHeadDefinifion",
    "CSkeleton",
    "CMimicFaces",
    "CR4LootParam",
}

_STREAMED_ITEM_CHUNK_TYPES = {
    "CItemEntity",
    "CWitcherSword",
    "Crossbow",
    "CWitcherJacket",
    "CWitcherPants",
    "CWitcherBoots",
}

_VISUAL_MESH_COMPONENT_TYPES = {
    "CBgMeshComponent",
    "CBgNpcItemComponent",
    "CBoatBodyComponent",
    "CImpostorMeshComponent",
    "CMeshComponent",
    "CMergedMeshComponent",
    "CMergedShadowMeshComponent",
    "CNavmeshComponent",
    "CStaticMeshComponent",
    "CFurComponent",
    "CRigidMeshComponent",
    "CRigidMeshComponentCooked",
    "CRagdollMeshComponent",
    "CScriptedDestroyableComponent",
    "CDressMeshComponent",
    "CWindowComponent",
}

_MESH_BEARING_COMPONENT_TYPES = _VISUAL_MESH_COMPONENT_TYPES | {"CMorphedMeshComponent"}

_SUPPORTED_ENTITY_VISUAL_CHUNKS = _VISUAL_MESH_COMPONENT_TYPES | {
    "CAnimatedAttachment",
    "CAnimatedComponent",
    "CAnimDangleBufferComponent",
    "CAnimDangleComponent",
    "CCameraComponent",
    "CClothComponent",
    "CDynamicFoliageComponent",
    "CExternalProxyAttachment",
    "CExternalProxyComponent",
    "CGameplayLightComponent",
    "CHeadAttachment",
    "CHardAttachment",
    "CMeshSkinningAttachment",
    "CMimicComponent",
    "CMorphedMeshComponent",
    "CMovingPhysicalAgentComponent",
    "CPointLightComponent",
    "CSkeletonBoneSlot",
    "CSpotLightComponent",
    "CSwitchableFoliageComponent",
}


def _destruction_plan_component_from_chunk(chunk):
    chunk_type = str(getattr(chunk, "Type", "") or getattr(chunk, "name", "") or "")
    if chunk_type == "CDestructionComponent":
        kind = "component_mesh"
        resource_names = ("m_baseResource", "resource")
        expected_exts = (".reddest",)
    elif chunk_type == "CDestructionSystemComponent":
        kind = "cloth"
        resource_names = ("m_resource", "resource")
        expected_exts = (".redapex", ".redcloth", ".apx")
    else:
        return None

    repo_path = ""
    for prop_name in resource_names:
        repo_path = _resolve_repo_path(chunk, prop_name, expected_exts) or ""
        if repo_path:
            break
    if not repo_path:
        return None

    transform_prop = _find_prop_by_name(chunk, "transform")
    drawable_prop = _find_prop_by_name(chunk, "drawableFlags")
    drawable_flags = getattr(drawable_prop, "Value", None)
    if drawable_flags is None and drawable_prop is not None:
        drawable_flags = _prop_to_string(drawable_prop)
    component_name = _chunk_prop_string(chunk, "name")
    return {
        "kind": kind,
        "name": component_name or Path(repo_path.replace("\\", "/")).stem or chunk_type,
        "repo_path": repo_path,
        "transform": getattr(transform_prop, "EngineTransform", None),
        "component_type": chunk_type,
        "component_name": component_name,
        "action_name": _chunk_prop_string(chunk, "actionName"),
        "drawable_flags": drawable_flags,
    }


def _unsupported_entity_visual_chunk_types(chunks):
    unsupported = []
    visual_tokens = (
        "Anim",
        "Cloth",
        "Decal",
        "Destruction",
        "Effect",
        "Fur",
        "Light",
        "Mesh",
        "Mimic",
        "Particle",
        "Visual",
    )
    for chunk in chunks or []:
        chunk_type = str(
            getattr(chunk, "Type", "") or getattr(chunk, "name", "") or ""
        ).strip()
        if (
            not chunk_type
            or chunk_type in _KNOWN_STRUCTURAL_CHUNKS
            or chunk_type in _SUPPORTED_ENTITY_VISUAL_CHUNKS
            or chunk_type.startswith("CAnimDangleConstraint_")
            or is_entity_chunk(chunk)
        ):
            continue
        is_visual_candidate = (
            chunk_type.endswith(("Attachment", "Component", "Slot"))
            or (
                any(token in chunk_type for token in visual_tokens)
                and not chunk_type.endswith(("Entity", "Param"))
            )
        )
        if is_visual_candidate and chunk_type not in unsupported:
            unsupported.append(chunk_type)
    return unsupported

# Synthetic chunk indices for streamed items live well above any real chunk index
# so the hard attachments we fabricate can reference each streamed mesh uniquely.
_STREAMED_ATTACHMENT_SYNTH_INDEX_BASE = 1_000_000


def _read_streamed_attachment_slots(CHUNKS):
    """Return streamed component names mapped to hard-attachment bone slots."""
    slots = {}
    for tchunk in CHUNKS:
        if getattr(tchunk, "Type", None) != "CEntityTemplate":
            continue
        prop = _find_prop_by_name(tchunk, "streamedAttachments")
        elements = getattr(prop, "More", None) if prop is not None else None
        if not elements:
            continue
        for el in elements:
            try:
                child = _prop_to_string(el.GetVariableByName("childName"))
                data_prop = el.GetVariableByName("data")
                raw = bytes(getattr(data_prop, "More", None) or [])
                if not child or not raw:
                    continue
                sub = getCR2W(bReadStream(raw, name="streamedAttachmentData"))
                for ach in sub.CHUNKS.CHUNKS:
                    if ach.Type != "CHardAttachment":
                        continue
                    slot = _prop_to_string(ach.GetVariableByName("parentSlotName"))
                    if slot:
                        slots[str(child).strip()] = str(slot).strip()
                    break
            except Exception:
                log.debug("Failed to read a streamedAttachment slot", exc_info=True)
    return slots


def _w2_chunk_type_name(chunk):
    return str(getattr(chunk, "type", None) or getattr(chunk, "Type", None) or "").strip()


def _is_w2_synthetic_appearance_chunk(chunk):
    chunk_type = _w2_chunk_type_name(chunk)
    if chunk_type in _VISUAL_MESH_COMPONENT_TYPES:
        return bool(getattr(chunk, "mesh", None) or _resolve_mesh_path(chunk, None))
    if chunk_type in {"CClothComponent", "CMorphedMeshComponent"}:
        return True
    return bool(getattr(chunk, "w2_head_support", False) or getattr(chunk, "w2_mimic_support", False))


_switchable_foliage_cache = {}


def _switchable_foliage_entries(w2sf_repo_path):
    key = _repo_path_key(w2sf_repo_path)
    if not key:
        return {}
    cached = _switchable_foliage_cache.get(key)
    if cached is not None:
        return dict(cached)

    entries = {}
    try:
        abs_path = repo_file(w2sf_repo_path)
        if abs_path and os.path.isfile(abs_path):
            from .dc_environment import resource_path as _handle_resource_path
            w2sf = read_CR2W(abs_path)
            for res_chunk in w2sf.CHUNKS.CHUNKS:
                if getattr(res_chunk, "Type", None) != "CSwitchableFoliageResource":
                    continue
                entries_prop = res_chunk.GetVariableByName("entries")
                for element in getattr(entries_prop, "More", None) or []:
                    entry_name = srt = None
                    for sub in getattr(element, "MoreProps", None) or []:
                        if getattr(sub, "theName", None) == "name":
                            entry_name = _prop_to_string(sub)
                        elif getattr(sub, "theName", None) == "tree":
                            srt = _normalize_repo_path_value(_handle_resource_path(sub), ".srt")
                    if entry_name and srt:
                        entries[str(entry_name)] = srt
    except Exception:
        log.warning("Failed to read switchable foliage resource: %s", w2sf_repo_path, exc_info=True)
    _switchable_foliage_cache[key] = dict(entries)
    return entries


def _foliage_component_from_chunk(chunk):
    name = _chunk_prop_string(chunk, "name")
    transform_prop = chunk.GetVariableByName("transform")
    transform = getattr(transform_prop, "EngineTransform", None) if transform_prop else None
    parent_prop = chunk.GetVariableByName("transformParent")
    parent_value = getattr(parent_prop, "Value", None) if parent_prop else None
    transform_parent = parent_value - 1 if isinstance(parent_value, int) else None

    if chunk.Type == "CDynamicFoliageComponent":
        srt = _resolve_repo_path(chunk, "baseTree", ".srt")
        if not srt:
            log.warning(
                "CDynamicFoliageComponent #%s has no resolvable baseTree; props=%s",
                chunk.ChunkIndex, _chunk_props_summary(chunk),
            )
            return None
        return CFoliageComponent(srt, name=name, transform=transform, transformParent=transform_parent)

    resource = _resolve_repo_path(chunk, "resource", ".w2sf")
    entries = _switchable_foliage_entries(resource)
    if not entries:
        log.warning(
            "CSwitchableFoliageComponent #%s has no resolvable entries (resource=%s)",
            chunk.ChunkIndex, resource,
        )
        return None
    # The engine selects the active entry at runtime.
    entry = "full" if "full" in entries else next(iter(entries))
    return CFoliageComponent(
        entries[entry], name=name, transform=transform, transformParent=transform_parent,
        srt_entries=entries, srt_entry=entry,
    )


_FOLIAGE_COMPONENT_TYPES = ("CDynamicFoliageComponent", "CSwitchableFoliageComponent")


def ReadTemplate(CR2W_FILE, new_mesh, this_Entity = None) -> ModelEnt:
    previous_chunk = False
    CHUNKS = CR2W_FILE.CHUNKS.CHUNKS
    mesh_import_paths = _collect_mesh_import_paths(CR2W_FILE)
    mesh_import_cursor = 0
    streamed_component_cache = {"resolved": False, "chunk": None}
    seen_mesh_signatures = set()
    seen_light_signatures = set()
    seen_animated_signatures = set()

    def _next_mesh_import_path():
        nonlocal mesh_import_cursor
        if mesh_import_cursor < len(mesh_import_paths):
            path = mesh_import_paths[mesh_import_cursor]
            mesh_import_cursor += 1
            return path
        return None

    def _append_unique_chunk(source_chunk, converted_chunk, added_chunks=None):
        _attach_w2_embedded_mesh_info(source_chunk, converted_chunk)
        converted_chunk.type = getattr(source_chunk, "Type", getattr(converted_chunk, "type", None))
        converted_chunk.chunkIndex = getattr(source_chunk, "ChunkIndex", getattr(converted_chunk, "chunkIndex", 0))
        signature = _mesh_chunk_signature(converted_chunk)
        if signature in seen_mesh_signatures:
            return False
        seen_mesh_signatures.add(signature)
        chunk_append(new_mesh, source_chunk, converted_chunk, added_chunks)
        return True

    def _append_unique_light_chunk(source_chunk, converted_chunk, added_chunks=None):
        converted_chunk.type = getattr(source_chunk, "Type", getattr(converted_chunk, "type", None))
        converted_chunk.chunkIndex = getattr(source_chunk, "ChunkIndex", getattr(converted_chunk, "chunkIndex", 0))
        signature = _light_chunk_signature(converted_chunk)
        if signature in seen_light_signatures:
            return False
        seen_light_signatures.add(signature)
        chunk_append(new_mesh, source_chunk, converted_chunk, added_chunks)
        return True

    def _append_unique_animated_chunk(source_chunk, converted_chunk, added_chunks=None):
        converted_chunk.type = getattr(source_chunk, "Type", getattr(converted_chunk, "type", None))
        converted_chunk.chunkIndex = getattr(source_chunk, "ChunkIndex", getattr(converted_chunk, "chunkIndex", 0))
        signature = _animated_chunk_signature(converted_chunk)
        if signature in seen_animated_signatures:
            return False
        seen_animated_signatures.add(signature)
        chunk_append(new_mesh, source_chunk, converted_chunk, added_chunks)
        return True

    def _get_streamed_component_chunk():
        if streamed_component_cache["resolved"]:
            return streamed_component_cache["chunk"]
        streamed_component_cache["resolved"] = True
        try:
            level_data = create_level(CR2W_FILE, "")
        except Exception:
            streamed_component_cache["chunk"] = None
            return None
        entities = getattr(level_data, "Entities", None)
        if not entities:
            streamed_component_cache["chunk"] = None
            return None
        stream_buf = getattr(entities[0], "streamingDataBuffer", None)
        if not (
            stream_buf
            and hasattr(stream_buf, "CHUNKS")
            and getattr(stream_buf.CHUNKS, "CHUNKS", None)
        ):
            streamed_component_cache["chunk"] = None
            return None
        streamed_component_cache["chunk"] = stream_buf.CHUNKS.CHUNKS[0]
        return streamed_component_cache["chunk"]

    def _append_streamed_mesh(owner_chunk, streamed_chunk):
        if not streamed_chunk:
            return False
        streamed_type = getattr(streamed_chunk, "Type", "")
        try:
            if streamed_type == "CRigidMeshComponent":
                streamed_component = CRigidMeshComponent(streamed_chunk).convert_for_io()
            else:
                # CItemEntity/Crossbow buffers can expose CMeshComponent-like chunks.
                streamed_component = CMeshComponent(streamed_chunk).convert_for_io()
        except Exception as e:
            log.warning(
                "Failed to convert streamed mesh chunk for %s #%s (%s): %s",
                owner_chunk.Type,
                owner_chunk.ChunkIndex,
                streamed_type or "unknown",
                e,
            )
            return False

        streamed_component = _resolve_component_mesh(streamed_chunk, streamed_component, _next_mesh_import_path)
        if streamed_component.mesh:
            appended = _append_unique_chunk(owner_chunk, streamed_component)
            if appended:
                new_mesh.chunks[-1].type = streamed_type or new_mesh.chunks[-1].type
            return appended

        log.warning(
            f"Skipping {owner_chunk.Type} with invalid streamed mesh ref: {owner_chunk.ChunkIndex}; "
            f"props={_chunk_props_summary(streamed_chunk)}"
        )
        return False

    def _apply_external_proxy_attachments():
        for proxy_chunk in CHUNKS:
            if getattr(proxy_chunk, "Type", None) != "CExternalProxyAttachment":
                continue
            original_attachment = proxy_chunk.GetVariableByName("originalAttachment")
            original_index = getattr(original_attachment, "Value", None)
            if not isinstance(original_index, int) or original_index <= 0 or original_index > len(CHUNKS):
                log.debug(
                    "Skipping CExternalProxyAttachment with invalid originalAttachment=%s in %s",
                    original_index,
                    getattr(CR2W_FILE, "fileName", ""),
                )
                continue

            attachment_chunk = CHUNKS[original_index - 1]
            existing_props = {
                getattr(prop, "theName", None): idx
                for idx, prop in enumerate(getattr(attachment_chunk, "PROPS", None) or [])
                if getattr(prop, "theName", None)
            }
            for prop in getattr(proxy_chunk, "PROPS", None) or []:
                prop_name = getattr(prop, "theName", None)
                if not prop_name or prop_name == "originalAttachment":
                    continue
                if prop_name in existing_props:
                    attachment_chunk.PROPS[existing_props[prop_name]] = prop
                else:
                    attachment_chunk.PROPS.append(prop)

    _apply_external_proxy_attachments()
    
    for chunk in CHUNKS:
        if chunk.Type in ("CMeshComponent", "CStaticMeshComponent", "CDressMeshComponent"):
            mesh_component = _resolve_component_mesh(chunk, _coerce_w2_mesh_component_for_io(chunk, CMeshComponent(chunk).convert_for_io()), _next_mesh_import_path)
            if mesh_component.mesh:
                _append_unique_chunk(chunk, mesh_component)
            else:
                log.warning(
                    f"Skipping CMeshComponent with invalid mesh ref in template: {chunk.ChunkIndex}; "
                    f"props={_chunk_props_summary(chunk)}"
                )
        elif (chunk.Type == "CRigidMeshComponent" or chunk.Type == "CRagdollMeshComponent"):
            mesh_component = _resolve_component_mesh(chunk, CMeshComponent(chunk).convert_for_io(), _next_mesh_import_path)
            if mesh_component.mesh:
                _append_unique_chunk(chunk, mesh_component)
            else:
                log.warning(
                    f"Skipping {chunk.Type} with invalid mesh ref in template: {chunk.ChunkIndex}; "
                    f"props={_chunk_props_summary(chunk)}"
                )
        elif chunk.Type in _STREAMED_ITEM_CHUNK_TYPES:
            # Inventory items across W2/W3 can use inline streamed mesh buffers,
            # but some templates still expose regular mesh components instead.
            if not _append_streamed_mesh(chunk, _get_streamed_component_chunk()):
                log.debug(
                    "%s has no streamingDataBuffer in template chunk %s; "
                    "falling back to regular mesh components.",
                    chunk.Type,
                    chunk.ChunkIndex,
                )
        elif (chunk.Type == "CClothComponent"):
            if chunk.GetVariableByName("resource"): #! sometimes there are no resource in files??
                cloth = chunk.GetVariableByName("resource").ToString()
                _cname_prop = chunk.GetVariableByName("name")
                _cname = str(_prop_to_string(_cname_prop) or "").strip()
                chunk_append(new_mesh, chunk, CClothComponent(cloth, name=_cname))
        elif (chunk.Type == "CFurComponent"):
            if (chunk.GetVariableByName("mesh")):
                fur_component = _resolve_component_mesh(chunk, CMeshComponent(chunk).convert_for_io(), _next_mesh_import_path)
                if fur_component.mesh:
                    _append_unique_chunk(chunk, fur_component)
                else:
                    log.warning(
                        f"Skipping CFurComponent with invalid mesh ref in template: {chunk.ChunkIndex}; "
                        f"props={_chunk_props_summary(chunk)}"
                    )
        elif chunk.Type in _FOLIAGE_COMPONENT_TYPES:
            foliage_component = _foliage_component_from_chunk(chunk)
            if foliage_component is not None:
                chunk_append(new_mesh, chunk, foliage_component)
        elif chunk.Type in _VISUAL_MESH_COMPONENT_TYPES:
            mesh_component = _resolve_component_mesh(chunk, CMeshComponent(chunk).convert_for_io(), _next_mesh_import_path)
            if mesh_component.mesh:
                _append_unique_chunk(chunk, mesh_component)
            else:
                log.warning(
                    f"Skipping {chunk.Type} with invalid mesh ref in template: {chunk.ChunkIndex}; "
                    f"props={_chunk_props_summary(chunk)}"
                )
        elif (chunk.Type == "CMorphedMeshComponent"):
            morphTarget = _resolve_repo_path(chunk, "morphTarget", ".w2mesh")
            morphSource = _resolve_repo_path(chunk, "morphSource", ".w2mesh")
            morphComponentId = _chunk_prop_string(chunk, "morphComponentId")
            chunk_append(new_mesh, chunk, CMorphedMeshComponent(morphTarget, morphSource, morphComponentId))
        elif (chunk.Type == "CMimicComponent"):
            name = _chunk_prop_string(chunk, "name")
            mimicFace = (
                _resolve_repo_path(chunk, "mimicFace", ".w3fac")
                or _resolve_repo_path(chunk, "mimicFace", ".w2fac")
            )
            chunk_append(new_mesh, chunk, CMimicComponent(name, mimicFace))
            #TODO GetFACE needed?
            #new_mesh.animation_face_object = GetFace(mimicFace)
        elif (chunk.Type == "CMeshSkinningAttachment"):
            parent_var = chunk.GetVariableByName("parent")
            child_var = chunk.GetVariableByName("child")
            if parent_var and child_var:
                chunk_append(new_mesh, chunk, CMeshSkinningAttachment(parent_var.Value-1, child_var.Value-1))
            elif not _chunk_prop_bool(chunk, "isBroken", default=False):
                log.warning(f'CMeshSkinningAttachment missing parent or child at template chunk {chunk.ChunkIndex}')
        elif (chunk.Type == "CAnimatedAttachment"):
            parent_var = chunk.GetVariableByName("parent")
            child_var = chunk.GetVariableByName("child")
            if parent_var and child_var:
                chunk_append(new_mesh, chunk, CAnimatedAttachment(parent_var.Value-1, child_var.Value-1))
            elif not _chunk_prop_bool(chunk, "isBroken", default=False):
                log.warning(f'CAnimatedAttachment missing parent or child at template chunk {chunk.ChunkIndex}')
        elif (chunk.Type == "CAnimDangleBufferComponent"):
            name = _chunk_prop_string(chunk, "name")
            skeleton = _resolve_repo_path(chunk, "skeleton", ".w2rig")
            chunk_append(new_mesh, chunk, CAnimDangleBufferComponent(name, skeleton))
        elif (chunk.Type == "CAnimDangleComponent"):
            name = _chunk_prop_string(chunk, "name")
            constraint_var = chunk.GetVariableByName("constraint")
            constraint = constraint_var.Value - 1 if constraint_var else None
            chunk_append(new_mesh, chunk, CAnimDangleComponent(name, constraint))
        elif (chunk.Type == "CAnimDangleConstraint_Dyng"):
            dyng = _resolve_repo_path(chunk, "dyng", ".w3dyng") if chunk.GetVariableByName("dyng") else None
            if not dyng and chunk.GetVariableByName("dyng"):
                dyng = _resolve_repo_path(chunk, "dyng", ".dyng")
            skeleton = _resolve_repo_path(chunk, "skeleton", ".w2rig") if chunk.GetVariableByName("skeleton") else None
            chunk_append(new_mesh, chunk, CAnimDangleConstraint_Dyng(skeleton, dyng, _dyng_constraint_settings_from_chunk(chunk)))
        elif (chunk.Type == "CAnimDangleConstraint_Breast"):
            skeleton = _resolve_repo_path(chunk, "skeleton", ".w2rig")
            chunk_append(new_mesh, chunk, CAnimDangleConstraint_Breast(skeleton, _breast_constraint_settings_from_chunk(chunk)))
        elif (chunk.Type in CAnimDangleConstraint_types):
            skeleton = _resolve_repo_path(chunk, "skeleton", ".w2rig")
            chunk_append(new_mesh, chunk, CAnimDangleConstraint_types[chunk.Type](skeleton))
        elif (chunk.Type == "CHardAttachment"): #TODO NormalBlend Stuff
            if (chunk.GetVariableByName("parentSlot")):
                chunk_append(new_mesh, chunk, CHardAttachment(chunk).convert_for_io())
        else:
            if chunk.Type in _KNOWN_STRUCTURAL_CHUNKS or is_entity_chunk(chunk):
                log.debug("Skipping structural chunk in ReadTemplate: %s", chunk.Type)
            else:
                _log_unknown_character_chunk(
                    chunk,
                    cr2w_file=CR2W_FILE,
                    template_name=getattr(new_mesh, "name", ""),
                )
    return new_mesh, this_Entity

def LoadCEntityTemplateFile(templateFilename: str, game_version=None) -> ModelEnt:
    if os.path.isabs(templateFilename) and os.path.exists(templateFilename):
        file_name_full = templateFilename
    else:
        file_name_full = materialize_entity_repo_path(templateFilename, version=game_version)
    root_dependency = _record_template_repo_dependency(
        templateFilename,
        file_name_full,
        game_version,
    )
    cache_key = _template_cache_key(file_name_full, game_version)
    current_thread = threading.get_ident()

    with _template_file_cache_lock:
        cached = _cached_template_result(cache_key)
        if cached is not None:
            return cached
        pending = _template_file_cache_inflight.get(cache_key)
        is_loader = pending is None
        if is_loader:
            pending = {
                "event": threading.Event(),
                "owner": current_thread,
                "error": None,
            }
            _template_file_cache_inflight[cache_key] = pending
        elif pending.get("owner") == current_thread:
            raise RuntimeError(f"Cyclic entity template dependency: {templateFilename}")

    if not is_loader:
        if pending["event"].wait(timeout=60):
            if pending.get("error") is not None:
                raise RuntimeError(
                    f"Entity template load failed: {templateFilename}"
                ) from pending["error"]
            cached = _cached_template_result(cache_key)
            if cached is None:
                raise RuntimeError(f"Entity template load produced no cache entry: {templateFilename}")
            return cached
        log.warning("Timed out waiting for in-flight entity template %s; loading in place", templateFilename)

    dependency_collector = {root_dependency}
    collector_token = _template_dependency_collectors.set(
        _template_dependency_collectors.get() + (dependency_collector,)
    )
    try:
        return _load_centity_template_file(
            templateFilename,
            game_version,
            file_name_full=file_name_full,
            cache_key=cache_key,
        )
    except Exception as exc:
        if is_loader:
            pending["error"] = exc
        raise
    finally:
        _template_dependency_collectors.reset(collector_token)
        if is_loader:
            with _template_file_cache_lock:
                _template_file_cache_inflight.pop(cache_key, None)
                pending["event"].set()


def _load_centity_template_file(
    templateFilename: str,
    game_version,
    *,
    file_name_full,
    cache_key,
) -> ModelEnt:
    new_mesh = ModelEnt(templateFilename, Path(templateFilename).stem)
    cr2w_file = _read_template_dependency_cr2w(file_name_full)
    is_w2_entity = getattr(getattr(cr2w_file, "HEADER", None), "version", 999) <= 115
    parsed_entity = None
    if not is_w2_entity:
        with redkit_repo_context(file_name_full):
            parsed_mesh, parsed_entity = ReadTemplate(cr2w_file, new_mesh)
    else:
        parsed_mesh = new_mesh

    with redkit_repo_context(file_name_full):
        full_entity = create_CEntity(cr2w_file)
    full_mesh = getattr(full_entity, "staticMeshes", None)
    if full_mesh and getattr(full_mesh, "chunks", None):
        has_full_mesh = any(getattr(c, "mesh", None) for c in full_mesh.chunks)
        if not is_w2_entity or has_full_mesh or getattr(full_entity, "appearances", None):
            full_mesh.templateFilename = templateFilename
            full_mesh.ns = Path(templateFilename).stem
            return _store_template_result(cache_key, (full_mesh, full_entity))
    if is_w2_entity and getattr(full_entity, "appearances", None):
        parsed_mesh.templateFilename = templateFilename
        parsed_mesh.ns = Path(templateFilename).stem
        return _store_template_result(cache_key, (parsed_mesh, full_entity))
    if not is_w2_entity and any(getattr(c, "mesh", None) for c in getattr(parsed_mesh, "chunks", [])):
        return _store_template_result(cache_key, (parsed_mesh, parsed_entity or full_entity))
    return _store_template_result(
        cache_key,
        (parsed_mesh, full_entity if is_w2_entity else parsed_entity),
    )

def create_CEntity(
    file,
    _inherit_visited=None,
    _included_entity_assets=None,
    _allow_unscoped_import_fallbacks=True,
):
    hasCMovingPhysicalAgentComponent = False
    CHUNKS = file.CHUNKS.CHUNKS
    flat_compiled_file = _flat_compiled_file(file) if file.HEADER.version > 115 else None
    if flat_compiled_file is not None:
        has_external_proxies = any(
            getattr(chunk, "Type", None) in {"CExternalProxyComponent", "CExternalProxyAttachment"}
            for chunk in CHUNKS
        )
        has_resolved_components = any(
            getattr(chunk, "Type", None) in (
                _VISUAL_MESH_COMPONENT_TYPES
                | {
                    "CAnimatedComponent",
                    "CClothComponent",
                    "CAnimDangleComponent",
                    "CMovingPhysicalAgentComponent",
                }
            )
            for chunk in CHUNKS
        )
        if not has_external_proxies and has_resolved_components:
            flat_compiled_file = None
    this_Entity = w3_types.Entity()
    this_Entity.name = Path(file.fileName).stem
    this_Entity.type = _entity_class_from_cr2w(file) or None
    this_Entity.version = getattr(getattr(file, "HEADER", None), "version", 999)
    this_Entity.appearances = []
    this_Entity.coloringEntries = []
    this_Entity.slots = []
    this_Entity.w2_body_part_states = {}
    this_Entity.cookedEffects = []
    this_Entity.isLightOn = None
    this_Entity.plan_components = []
    incomplete_components = []
    this_Entity.unsupported_components = []
    new_mesh = ModelEnt("staticMeshes", "staticMeshes")
    added_chunks = set()  # Track chunk indices already added to avoid duplicates
    streamed_attachment_slots = _read_streamed_attachment_slots(CHUNKS)
    streamed_synth_counter = [0]
    streamed_hard_attached = set()  # component names already bone-slot attached
    pending_streamed_hard = []      # (mesh chunkIndex, bone slot) to wire after the loop
    seen_mesh_signatures = set()
    seen_light_signatures = set()
    seen_animated_signatures = set()
    this_Entity.CAnimAnimsetsParam = []
    this_Entity.CAnimMimicParam = []
    mesh_import_paths = (
        _collect_mesh_import_paths(file)
        if _allow_unscoped_import_fallbacks
        else []
    )
    mesh_import_cursor = 0
    top_level_template_includes = []
    top_level_template_include_set = set()
    top_level_included_entity_cache = None
    inherited_beh_paths = []
    pending_w2_appearances = []
    w2_body_part_chunk_indices = set()
    w2_body_part_component_names = set()
    w2_related_entity_paths = []
    w2_related_files = []
    w2_related_search_chunks = []
    inherit_visited = set(_inherit_visited or [])
    included_entity_assets = {
        str(path or "").replace("/", "\\").strip().lower(): entity
        for path, entity in dict(_included_entity_assets or {}).items()
        if str(path or "").strip() and entity is not None
    }
    current_file_name = getattr(file, "fileName", None)
    if current_file_name:
        inherit_visited.add(os.path.normcase(os.path.normpath(str(current_file_name))))

    def _append_unique_repo_path(target, path):
        path = str(path or "").replace("/", "\\").strip()
        if not path:
            return
        key = path.lower()
        if key in {str(existing or "").replace("/", "\\").strip().lower() for existing in target}:
            return
        target.append(path)

    def _next_mesh_import_path():
        nonlocal mesh_import_cursor
        if mesh_import_cursor < len(mesh_import_paths):
            path = mesh_import_paths[mesh_import_cursor]
            mesh_import_cursor += 1
            return path
        return None

    def _register_streamed_slot_mesh(mc, comp_type):
        """Append a streamed mesh as a synthetic hard-attached slot item."""
        name = str(getattr(mc, "name", "") or "").strip()
        slot = streamed_attachment_slots.get(name) if (name and mc.mesh) else None
        if not slot:
            return False
        if name in streamed_hard_attached:
            return True  # already added via the other streamed buffer path
        streamed_hard_attached.add(name)
        streamed_synth_counter[0] += 1
        mesh_idx = _STREAMED_ATTACHMENT_SYNTH_INDEX_BASE + streamed_synth_counter[0]
        comp_transform = getattr(mc, "transform", None)
        mc.transform = None
        mc.type = comp_type
        mc.chunkIndex = mesh_idx
        new_mesh.chunks.append(mc)
        pending_streamed_hard.append((mesh_idx, slot, comp_transform))
        log.debug('Registered streamed %s "%s" -> bone slot %s (%s)', comp_type, name, slot, mc.mesh)
        return True

    def _append_unique_chunk(source_chunk, converted_chunk, added_chunks=None):
        _attach_w2_embedded_mesh_info(source_chunk, converted_chunk)
        converted_chunk.type = getattr(source_chunk, "Type", getattr(converted_chunk, "type", None))
        converted_chunk.chunkIndex = getattr(source_chunk, "ChunkIndex", getattr(converted_chunk, "chunkIndex", 0))
        signature = _mesh_chunk_signature(converted_chunk)
        if signature in seen_mesh_signatures:
            return False
        seen_mesh_signatures.add(signature)
        chunk_append(new_mesh, source_chunk, converted_chunk, added_chunks)
        return True

    def _append_unique_light_chunk(source_chunk, converted_chunk, added_chunks=None):
        converted_chunk.type = getattr(source_chunk, "Type", getattr(converted_chunk, "type", None))
        converted_chunk.chunkIndex = getattr(source_chunk, "ChunkIndex", getattr(converted_chunk, "chunkIndex", 0))
        signature = _light_chunk_signature(converted_chunk)
        if signature in seen_light_signatures:
            return False
        seen_light_signatures.add(signature)
        chunk_append(new_mesh, source_chunk, converted_chunk, added_chunks)
        return True

    def _append_unique_animated_chunk(source_chunk, converted_chunk, added_chunks=None):
        converted_chunk.type = getattr(source_chunk, "Type", getattr(converted_chunk, "type", None))
        converted_chunk.chunkIndex = getattr(source_chunk, "ChunkIndex", getattr(converted_chunk, "chunkIndex", 0))
        signature = _animated_chunk_signature(converted_chunk)
        if signature in seen_animated_signatures:
            return False
        seen_animated_signatures.add(signature)
        chunk_append(new_mesh, source_chunk, converted_chunk, added_chunks)
        return True

    def _append_unique_generated_chunk(converted_chunk):
        signature = _mesh_chunk_signature(converted_chunk)
        if signature in seen_mesh_signatures:
            return False
        seen_mesh_signatures.add(signature)
        new_mesh.chunks.append(converted_chunk)
        return True

    def _coloring_entry_key(entry):
        if isinstance(entry, dict):
            return (str(entry.get("appearance", "")), str(entry.get("componentName", "")))
        return (str(getattr(entry, "appearance", "")), str(getattr(entry, "componentName", "")))

    related_w2_entity_cache = None

    def _iter_related_w2_entities():
        nonlocal related_w2_entity_cache
        if related_w2_entity_cache is not None:
            return related_w2_entity_cache
        related_w2_entity_cache = []
        for depot_path, full_path, related_file in w2_related_files:
            norm_full_path = os.path.normcase(os.path.normpath(full_path))
            if norm_full_path in inherit_visited:
                continue
            try:
                related_entity = create_CEntity(
                    related_file,
                    _inherit_visited=inherit_visited | {norm_full_path},
                    _included_entity_assets=included_entity_assets,
                )
            except Exception as e:
                log.debug("Failed to build related Witcher 2 entity '%s': %s", depot_path, e)
                continue
            related_w2_entity_cache.append((depot_path, related_entity))
        return related_w2_entity_cache

    def _merge_related_inventory_definitions(target_defs, source_defs):
        target_defs = list(target_defs or [])
        seen = {
            tuple(sorted((str(getattr(entry, "category", "")), str(getattr(getattr(entry, "initializer", None), "itemName", "") or str(getattr(entry, "item", "")))) for entry in getattr(inv_def, "entries", []) or []))
            for inv_def in target_defs
        }
        for source_def in source_defs or []:
            signature = tuple(sorted((str(getattr(entry, "category", "")), str(getattr(getattr(entry, "initializer", None), "itemName", "") or str(getattr(entry, "item", "")))) for entry in getattr(source_def, "entries", []) or []))
            if signature in seen:
                continue
            seen.add(signature)
            target_defs.append(copy.deepcopy(source_def))
        return target_defs

    def _slot_merge_key(slot):
        return (
            str(getattr(slot, "name", "") or "").lower(),
            str(getattr(slot, "componentName", "") or "").lower(),
            str(getattr(slot, "boneName", "") or "").lower(),
        )

    def _merge_related_slots(target_slots, source_slots):
        target_slots = list(target_slots or [])
        seen = {_slot_merge_key(slot) for slot in target_slots}
        for source_slot in source_slots or []:
            key = _slot_merge_key(source_slot)
            if key in seen:
                continue
            seen.add(key)
            target_slots.append(copy.deepcopy(source_slot))
        return target_slots

    def _has_mesh_bearing_chunks(chunks):
        return any(getattr(chunk, "mesh", None) for chunk in (chunks or []))

    def _is_w2_proxy_wrapper_entity():
        if file.HEADER.version > 115:
            return False
        return any(
            getattr(chunk, "Type", None) in {"CExternalProxyComponent", "CExternalProxyAttachment"}
            for chunk in CHUNKS
        )

    def _related_w2_file_matches_external_proxy(related_file):
        if not guids:
            return False
        for related_chunk in getattr(getattr(related_file, "CHUNKS", None), "CHUNKS", None) or []:
            try:
                guid_var = related_chunk.GetVariableByName("guid")
            except Exception:
                guid_var = None
            guid = getattr(getattr(guid_var, "GUID", None), "GuidString", None)
            if guid and guid in guids:
                return True
        return False

    def _w2_prop_chunk_index(chunk, prop_name):
        if not chunk:
            return None
        try:
            prop = chunk.GetVariableByName(prop_name)
        except Exception:
            prop = None
        if not prop:
            return None
        value = getattr(prop, "Value", None)
        if isinstance(value, int) and 0 < value <= len(CHUNKS):
            return value - 1
        for handle in getattr(prop, "Handles", None) or []:
            ref = getattr(handle, "Reference", None)
            if isinstance(ref, int) and 0 <= ref < len(CHUNKS):
                return ref
        return None

    def _w2_component_indices(entity_chunk):
        indices = set()
        for component_index in getattr(entity_chunk, "Components", None) or []:
            if isinstance(component_index, int) and 0 < component_index <= len(CHUNKS):
                indices.add(component_index - 1)
        return indices

    def _w2_selected_entity_root_indices():
        if file.HEADER.version > 115:
            return set()
        roots = set()
        for template_chunk in CHUNKS:
            if getattr(template_chunk, "Type", None) != "CEntityTemplate":
                continue
            cooked_root = _w2_prop_chunk_index(template_chunk, "cookedEntityObject")
            editor_root = _w2_prop_chunk_index(template_chunk, "entityObject")
            selected_root = cooked_root if cooked_root is not None else editor_root
            if selected_root is None:
                continue
            roots.add(selected_root)
        return roots

    def _w2_attachment_parent_child_indices(chunk):
        return (
            _w2_prop_chunk_index(chunk, "parent"),
            _w2_prop_chunk_index(chunk, "child"),
        )

    def _w2_reachable_entity_graph_indices(root_indices):
        if not root_indices:
            return set()
        reachable = set(root_indices)

        def add_index(index):
            if isinstance(index, int) and 0 <= index < len(CHUNKS) and index not in reachable:
                reachable.add(index)
                return True
            return False

        changed = True
        while changed:
            changed = False
            for idx in list(reachable):
                chunk = CHUNKS[idx]
                for component_idx in _w2_component_indices(chunk):
                    changed |= add_index(component_idx)
                changed |= add_index(_w2_prop_chunk_index(chunk, "transformParent"))
                changed |= add_index(_w2_prop_chunk_index(chunk, "parentSlot"))
                changed |= add_index(_w2_prop_chunk_index(chunk, "originalAttachment"))
                parent_idx, child_idx = _w2_attachment_parent_child_indices(chunk)
                changed |= add_index(parent_idx)
                changed |= add_index(child_idx)

            for chunk in CHUNKS:
                chunk_type = getattr(chunk, "Type", None)
                if chunk_type not in {
                    "CMeshSkinningAttachment",
                    "CAnimatedAttachment",
                    "CHardAttachment",
                    "CExternalProxyAttachment",
                }:
                    continue
                chunk_idx = getattr(chunk, "ChunkIndex", None)
                parent_idx, child_idx = _w2_attachment_parent_child_indices(chunk)
                original_idx = _w2_prop_chunk_index(chunk, "originalAttachment")
                if (
                    parent_idx in reachable
                    or child_idx in reachable
                    or original_idx in reachable
                    or chunk_idx in reachable
                ):
                    changed |= add_index(chunk_idx)
                    changed |= add_index(parent_idx)
                    changed |= add_index(child_idx)
                    changed |= add_index(original_idx)
                    if original_idx is not None:
                        changed |= add_index(_w2_prop_chunk_index(CHUNKS[original_idx], "parentSlot"))
        return reachable

    w2_selected_entity_roots = _w2_selected_entity_root_indices()
    w2_selected_graph_indices = _w2_reachable_entity_graph_indices(w2_selected_entity_roots)
    w2_external_proxy_original_attachment_indices = {
        original_idx
        for idx in w2_selected_graph_indices
        for original_idx in [_w2_prop_chunk_index(CHUNKS[idx], "originalAttachment")]
        if original_idx is not None
    }
    w2_external_proxy_child_attachment_indices = {}
    for idx in w2_selected_graph_indices:
        if getattr(CHUNKS[idx], "Type", None) != "CExternalProxyAttachment":
            continue
        original_idx = _w2_prop_chunk_index(CHUNKS[idx], "originalAttachment")
        _parent_idx, child_idx = _w2_attachment_parent_child_indices(CHUNKS[idx])
        if original_idx is not None and child_idx is not None:
            w2_external_proxy_child_attachment_indices[child_idx] = original_idx
    w2_graph_chunk_types = (
        set(Entity_Type_List)
        | _VISUAL_MESH_COMPONENT_TYPES
        | {
            "CAnimatedComponent",
            "CMovingPhysicalAgentComponent",
            "CMimicComponent",
            "CAnimDangleBufferComponent",
            "CAnimDangleComponent",
            "CStaticMeshComponent",
            "CClothComponent",
            "CFurComponent",
            "CMorphedMeshComponent",
            "CPointLightComponent",
            "CSpotLightComponent",
            "CCameraComponent",
            "CMeshSkinningAttachment",
            "CAnimatedAttachment",
            "CHardAttachment",
            "CExternalProxyComponent",
            "CExternalProxyAttachment",
            "CSkeletonBoneSlot",
        }
        | set(CAnimDangleConstraint_types.keys())
    )

    def _w2_should_skip_unselected_graph_chunk(chunk):
        if file.HEADER.version > 115 or not w2_selected_graph_indices:
            return False
        chunk_type = getattr(chunk, "Type", None)
        if chunk_type not in w2_graph_chunk_types:
            return False
        return getattr(chunk, "ChunkIndex", None) not in w2_selected_graph_indices

    w2_related_proxy_component_cache = {}

    def _w2_chunk_guid(chunk):
        try:
            guid_var = chunk.GetVariableByName("guid")
        except Exception:
            guid_var = None
        return getattr(getattr(guid_var, "GUID", None), "GuidString", None)

    def _w2_component_signature(component):
        return (
            getattr(component, "type", None),
            getattr(component, "name", None),
            _repo_path_key(getattr(component, "skeleton", None)),
            _repo_path_key(getattr(component, "mesh", None)),
            _repo_path_key(getattr(component, "resource", None)),
        )

    def _w2_convert_proxy_component_source(source_chunk, proxy_chunk):
        source_type = getattr(source_chunk, "Type", None)
        if source_type == "CAnimatedComponent":
            component = CAnimatedComponent(source_chunk).convert_for_io()
            name = _chunk_prop_string(source_chunk, "name")
            skeleton = _resolve_repo_path(source_chunk, "skeleton", ".w2rig")
            if not skeleton:
                skeleton_paths = (
                    _collect_rig_import_paths(
                        getattr(source_chunk, "_W_CLASS__CR2WFILE", None)
                    )
                    if _allow_unscoped_import_fallbacks
                    else []
                )
                if skeleton_paths:
                    skeleton = skeleton_paths[0]
            component.name = name or component.name
            component.skeleton = skeleton
            component.animationSets = _resolve_repo_paths_from_array(source_chunk, "animationSets", ".w2anims")
        elif source_type == "CMovingPhysicalAgentComponent":
            name = _chunk_prop_string(source_chunk, "name")
            skeleton = _resolve_repo_path(source_chunk, "skeleton", ".w2rig")
            if not skeleton:
                return None
            component = w3_types.CMovingPhysicalAgentComponent(skeleton, name)
            component.animationSets = _resolve_repo_paths_from_array(source_chunk, "animationSets", ".w2anims")
        elif source_type == "CStaticMeshComponent":
            component = CStaticMeshComponent(source_chunk).convert_for_io()
            component.mesh = _resolve_mesh_path(source_chunk, component.mesh)
            if not component.mesh:
                return None
        elif source_type in {"CMeshComponent", "CDressMeshComponent"}:
            component = _coerce_w2_mesh_component_for_io(
                source_chunk,
                CMeshComponent(source_chunk).convert_for_io(),
            )
            component.mesh = _resolve_mesh_path(source_chunk, component.mesh)
            if not component.mesh:
                return None
        elif source_type == "CRigidMeshComponent":
            component = CRigidMeshComponent(source_chunk).convert_for_io()
            component.mesh = _resolve_mesh_path(source_chunk, component.mesh)
            if not component.mesh:
                return None
        elif source_type == "CRagdollMeshComponent":
            if source_chunk.GetVariableByName("mesh") is None:
                return None
            component = CRagdollMeshComponent(source_chunk).convert_for_io()
            component.mesh = _resolve_mesh_path(source_chunk, component.mesh)
            if not component.mesh:
                return None
            source_file = getattr(source_chunk, "_W_CLASS__CR2WFILE", None)
            source_chunks = getattr(getattr(source_file, "CHUNKS", None), "CHUNKS", None) or []
            component.ragdoll_meta = _extract_w2_ragdoll_meta(source_chunk, source_chunks)
        elif source_type == "CFurComponent":
            component = CMeshComponent(source_chunk).convert_for_io()
            component.mesh = _resolve_mesh_path(source_chunk, component.mesh)
            if not component.mesh:
                return None
        elif source_type in {"CPointLightComponent", "CSpotLightComponent"}:
            light_cls = CSpotLightComponent if source_type == "CSpotLightComponent" else CPointLightComponent
            component = light_cls(source_chunk).convert_for_io()
        elif source_type == "CCameraComponent":
            component = CCameraComponent(_chunk_prop_string(source_chunk, "name"))
        else:
            return None
        component.type = source_type
        proxy_chunk_index = getattr(proxy_chunk, "ChunkIndex", getattr(source_chunk, "ChunkIndex", 0))
        component.chunkIndex = proxy_chunk_index
        if proxy_chunk_index in w2_external_proxy_child_attachment_indices:
            component.transformParent = w2_external_proxy_child_attachment_indices[proxy_chunk_index]
        return component

    def _w2_proxy_component_from_related_guid(proxy_chunk):
        guid = _w2_chunk_guid(proxy_chunk)
        if not guid:
            return None
        if guid not in w2_related_proxy_component_cache:
            matches = []
            seen = set()
            for _depot_path, _full_path, related_file in w2_related_files:
                for source_chunk in getattr(getattr(related_file, "CHUNKS", None), "CHUNKS", None) or []:
                    if _w2_chunk_guid(source_chunk) != guid:
                        continue
                    component = _w2_convert_proxy_component_source(source_chunk, proxy_chunk)
                    if component is None:
                        continue
                    signature = _w2_component_signature(component)
                    if signature in seen:
                        continue
                    seen.add(signature)
                    matches.append(component)
            w2_related_proxy_component_cache[guid] = matches[0] if len(matches) == 1 else None
        component = w2_related_proxy_component_cache.get(guid)
        return copy.deepcopy(component) if component is not None else None

    def _w2_proxy_component_replacement(proxy_chunk):
        return _w2_proxy_component_from_related_guid(proxy_chunk)

    def _append_w2_proxy_component(proxy_chunk):
        nonlocal hasCMovingPhysicalAgentComponent
        component = _w2_proxy_component_replacement(proxy_chunk)
        if component is None:
            return False
        component_type = getattr(component, "type", None)
        if component_type in _VISUAL_MESH_COMPONENT_TYPES or getattr(component, "mesh", None):
            signature = _mesh_chunk_signature(component)
            if signature in seen_mesh_signatures:
                return False
            seen_mesh_signatures.add(signature)
        elif component_type in {"CPointLightComponent", "CSpotLightComponent"}:
            signature = _light_chunk_signature(component)
            if signature in seen_light_signatures:
                return False
            seen_light_signatures.add(signature)
        elif component_type in {"CMovingPhysicalAgentComponent", "CAnimatedComponent"}:
            signature = _animated_chunk_signature(component)
            if signature in seen_animated_signatures:
                return False
            seen_animated_signatures.add(signature)
        new_mesh.chunks.append(component)
        added_chunks.add(getattr(proxy_chunk, "ChunkIndex", getattr(component, "chunkIndex", 0)))
        if component_type in {"CMovingPhysicalAgentComponent", "CAnimatedComponent"}:
            this_Entity.MovingPhysicalAgentComponent = component
            hasCMovingPhysicalAgentComponent = True
        return True

    def _materialize_w2_external_proxy_attachment(proxy_chunk):
        original_idx = _w2_prop_chunk_index(proxy_chunk, "originalAttachment")
        parent_idx, child_idx = _w2_attachment_parent_child_indices(proxy_chunk)
        if original_idx is None or parent_idx is None or child_idx is None:
            return None
        original_chunk = CHUNKS[original_idx]
        original_type = getattr(original_chunk, "Type", None)
        if original_type == "CHardAttachment":
            attachment = CHardAttachment(original_chunk).convert_for_io()
            attachment.parent = parent_idx
            attachment.child = child_idx
        elif original_type == "CMeshSkinningAttachment":
            attachment = CMeshSkinningAttachment(parent_idx, child_idx)
        elif original_type == "CAnimatedAttachment":
            attachment = CAnimatedAttachment(parent_idx, child_idx)
        else:
            return None
        proxy_is_broken = None
        try:
            proxy_is_broken = getattr(proxy_chunk.GetVariableByName("isBroken"), "Value", None)
        except Exception:
            proxy_is_broken = None
        attachment.isBroken = bool(proxy_is_broken) if proxy_is_broken is not None else False
        attachment.type = original_type
        attachment.chunkIndex = original_idx
        attachment.w2_external_proxy_attachment = getattr(proxy_chunk, "ChunkIndex", None)
        return attachment

    def _merge_related_appearances(source_entity):
        source_apps = list(getattr(source_entity, "appearances", []) or [])
        if not source_apps:
            return
        if not this_Entity.appearances:
            this_Entity.appearances = copy.deepcopy(source_apps)
            return
        target_by_name = {
            str(getattr(app, "name", "")).lower(): app
            for app in this_Entity.appearances
        }
        for source_app in source_apps:
            source_name = str(getattr(source_app, "name", "")).lower()
            target_app = target_by_name.get(source_name)
            if not target_app:
                this_Entity.appearances.append(copy.deepcopy(source_app))
                continue
            if not getattr(target_app, "includedTemplates", None) and getattr(source_app, "includedTemplates", None):
                target_app.includedTemplates = copy.deepcopy(source_app.includedTemplates)
            if not getattr(target_app, "appearanceParams", None) and getattr(source_app, "appearanceParams", None):
                target_app.appearanceParams = copy.deepcopy(source_app.appearanceParams)
            if not getattr(target_app, "inventoryDefinitions", None) and getattr(source_app, "inventoryDefinitions", None):
                target_app.inventoryDefinitions = copy.deepcopy(source_app.inventoryDefinitions)
            if not getattr(target_app, "headName", None) and getattr(source_app, "headName", None):
                target_app.headName = source_app.headName

    def _iter_top_level_included_entities():
        nonlocal top_level_included_entity_cache
        if file.HEADER.version <= 115 or not top_level_template_includes:
            return []
        if top_level_included_entity_cache is not None:
            return top_level_included_entity_cache

        included_entities = []
        for include_path in top_level_template_includes:
            depot_path = str(include_path or "").strip()
            if not depot_path or not depot_path.lower().endswith(".w2ent"):
                continue
            cached_entity = included_entity_assets.get(
                depot_path.replace("/", "\\").strip().lower()
            )
            if cached_entity is not None:
                included_entities.append((depot_path, copy.deepcopy(cached_entity)))
                continue
            try:
                include_full_path = materialize_entity_repo_path(
                    depot_path,
                    version=file.HEADER.version,
                )
                _record_template_repo_dependency(
                    depot_path,
                    include_full_path,
                    file.HEADER.version,
                    getattr(file, "fileName", ""),
                )
                norm_include_path = os.path.normcase(os.path.normpath(include_full_path))
            except Exception as e:
                incomplete_components.append(
                    f"unresolved included template {depot_path}: {e}"
                )
                log.debug(f"Failed to resolve included template path '{depot_path}': {e}")
                continue
            if norm_include_path in inherit_visited:
                continue
            try:
                include_cr2w = _read_template_dependency_cr2w(include_full_path)
                include_entity = create_CEntity(
                    include_cr2w,
                    _inherit_visited=inherit_visited | {norm_include_path},
                    _included_entity_assets=included_entity_assets,
                )
            except Exception as e:
                incomplete_components.append(
                    f"unresolved included template {depot_path}: {e}"
                )
                log.debug(f"Failed to load included template '{depot_path}': {e}")
                continue
            if include_entity is None:
                incomplete_components.append(
                    f"unresolved included template {depot_path}: compiler returned no entity"
                )
                continue
            included_entities.append((depot_path, include_entity))

        top_level_included_entity_cache = included_entities
        return top_level_included_entity_cache

    def _append_inherited_animated_chunk(inherited_chunk):
        converted_chunk = copy.deepcopy(inherited_chunk)
        converted_chunk.type = getattr(converted_chunk, "type", "CAnimatedComponent")
        converted_chunk.chunkIndex = getattr(converted_chunk, "chunkIndex", 0)
        signature = _animated_chunk_signature(converted_chunk)
        if signature in seen_animated_signatures:
            return False
        seen_animated_signatures.add(signature)
        new_mesh.chunks.append(converted_chunk)
        return True

    def _merge_inherited_template_components():
        nonlocal hasCMovingPhysicalAgentComponent
        if file.HEADER.version <= 115:
            return

        needs_inherited_animated = not hasCMovingPhysicalAgentComponent and not any(
            getattr(chunk, "type", None) == "CAnimatedComponent"
            for chunk in getattr(new_mesh, "chunks", None) or []
        )
        beh_seen = {_repo_path_key(path) for path in inherited_beh_paths}
        for depot_path, include_entity in _iter_top_level_included_entities():
            _merge_related_appearances(include_entity)

            for plan_component in getattr(include_entity, "plan_components", None) or []:
                if plan_component not in this_Entity.plan_components:
                    this_Entity.plan_components.append(copy.deepcopy(plan_component))

            inherited_component = getattr(include_entity, "MovingPhysicalAgentComponent", None)
            if inherited_component and needs_inherited_animated and not hasCMovingPhysicalAgentComponent:
                this_Entity.MovingPhysicalAgentComponent = copy.deepcopy(inherited_component)
                hasCMovingPhysicalAgentComponent = True
                log.debug("Inherited MovingPhysicalAgentComponent from template '%s'", depot_path)

            if needs_inherited_animated:
                for inherited_chunk in getattr(getattr(include_entity, "staticMeshes", None), "chunks", None) or []:
                    if getattr(inherited_chunk, "type", None) != "CAnimatedComponent":
                        continue
                    if _append_inherited_animated_chunk(inherited_chunk):
                        log.debug(
                            "Inherited CAnimatedComponent '%s' from template '%s'",
                            getattr(inherited_chunk, "name", ""),
                            depot_path,
                        )

            for beh_path in getattr(include_entity, "beh_paths", None) or []:
                beh_key = _repo_path_key(beh_path)
                if not beh_key or beh_key in beh_seen:
                    continue
                beh_seen.add(beh_key)
                inherited_beh_paths.append(beh_path)

    def _merge_inherited_coloring_entries():
        if file.HEADER.version <= 115 or not top_level_template_includes:
            return
        existing_keys = {_coloring_entry_key(e) for e in (this_Entity.coloringEntries or [])}
        for _depot_path, include_entity in _iter_top_level_included_entities():
            for inherited_entry in getattr(include_entity, "coloringEntries", []) or []:
                key = _coloring_entry_key(inherited_entry)
                if key in existing_keys:
                    continue
                this_Entity.coloringEntries.append(inherited_entry)
                existing_keys.add(key)

    def _merge_inherited_slots_inventory_and_effects():
        if file.HEADER.version <= 115 or not top_level_template_includes:
            return

        slot_names = {
            str(getattr(slot, "name", "") or "").lower()
            for slot in this_Entity.slots
            if getattr(slot, "name", None)
        }
        effect_names = {
            str(effect.get("name", "") or "").lower()
            for effect in this_Entity.cookedEffects
            if effect.get("name")
        }
        for _depot_path, include_entity in _iter_top_level_included_entities():
            for inherited_slot in getattr(include_entity, "slots", None) or []:
                slot_name = str(getattr(inherited_slot, "name", "") or "").lower()
                if slot_name and slot_name in slot_names:
                    continue
                this_Entity.slots.append(copy.deepcopy(inherited_slot))
                if slot_name:
                    slot_names.add(slot_name)

            inherited_defs = getattr(include_entity, "inventoryDefinitions", None) or []
            if inherited_defs:
                current_defs = getattr(this_Entity, "inventoryDefinitions", None) or []
                this_Entity.inventoryDefinitions = _merge_related_inventory_definitions(
                    current_defs,
                    inherited_defs,
                )

            for inherited_effect in getattr(include_entity, "cookedEffects", None) or []:
                effect_name = str(inherited_effect.get("name", "") or "").lower()
                if effect_name and effect_name in effect_names:
                    continue
                this_Entity.cookedEffects.append(copy.deepcopy(inherited_effect))
                if effect_name:
                    effect_names.add(effect_name)

    def _anim_param_signature(param):
        if isinstance(param, dict):
            name = str(param.get("name", "") or "")
            component_name = str(param.get("componentName", "") or "")
            animsets = tuple(str(path or "").replace("/", "\\").lower() for path in (param.get("animationSets", []) or []))
        else:
            name = str(getattr(param, "name", "") or "")
            component_name = str(getattr(param, "componentName", "") or "")
            animsets = tuple(str(path or "").replace("/", "\\").lower() for path in (getattr(param, "animationSets", []) or []))
        return name.lower(), component_name.lower(), animsets

    def _merge_inherited_anim_params():
        if file.HEADER.version <= 115 or not top_level_template_includes:
            return
        for attr_name in ("CAnimAnimsetsParam", "CAnimMimicParam"):
            current_params = getattr(this_Entity, attr_name, None)
            if current_params is None:
                current_params = []
                setattr(this_Entity, attr_name, current_params)
            existing_keys = {_anim_param_signature(param) for param in current_params}
            for _depot_path, include_entity in _iter_top_level_included_entities():
                for inherited_param in getattr(include_entity, attr_name, None) or []:
                    key = _anim_param_signature(inherited_param)
                    if key in existing_keys:
                        continue
                    current_params.append(copy.deepcopy(inherited_param))
                    existing_keys.add(key)

    def _apply_external_proxy_attachments():
        for proxy_chunk in CHUNKS:
            if getattr(proxy_chunk, "Type", None) != "CExternalProxyAttachment":
                continue
            original_attachment = proxy_chunk.GetVariableByName("originalAttachment")
            original_index = getattr(original_attachment, "Value", None)
            if not isinstance(original_index, int) or original_index <= 0 or original_index > len(CHUNKS):
                log.debug(
                    "Skipping CExternalProxyAttachment with invalid originalAttachment=%s in %s",
                    original_index,
                    getattr(file, "fileName", ""),
                )
                continue

            attachment_chunk = CHUNKS[original_index - 1]
            existing_props = {
                getattr(prop, "theName", None): idx
                for idx, prop in enumerate(getattr(attachment_chunk, "PROPS", None) or [])
                if getattr(prop, "theName", None)
            }
            for prop in getattr(proxy_chunk, "PROPS", None) or []:
                prop_name = getattr(prop, "theName", None)
                if not prop_name or prop_name == "originalAttachment":
                    continue
                if prop_name in existing_props:
                    attachment_chunk.PROPS[existing_props[prop_name]] = prop
                else:
                    attachment_chunk.PROPS.append(prop)

    _apply_external_proxy_attachments()

    def _synthesize_missing_transform_parents_from_hard_attachments():
        child_to_attachment = {}
        for chunk in getattr(new_mesh, "chunks", None) or []:
            if getattr(chunk, "type", None) != "CHardAttachment":
                continue
            child_index = getattr(chunk, "child", None)
            attachment_index = getattr(chunk, "chunkIndex", None)
            if isinstance(child_index, int) and isinstance(attachment_index, int):
                child_to_attachment.setdefault(child_index, attachment_index)

        if not child_to_attachment:
            return

        for chunk in getattr(new_mesh, "chunks", None) or []:
            if getattr(chunk, "type", None) not in {
                "CMeshComponent",
                "CStaticMeshComponent",
                "CRigidMeshComponent",
                "CRagdollMeshComponent",
                "CFurComponent",
            }:
                continue
            if getattr(chunk, "transformParent", None) is not None:
                continue
            attachment_index = child_to_attachment.get(getattr(chunk, "chunkIndex", None))
            if attachment_index is not None:
                chunk.transformParent = attachment_index

    def _chunk_type_by_index():
        return {
            getattr(chunk, "ChunkIndex", None): getattr(chunk, "Type", None)
            for chunk in CHUNKS
        }

    def _attachment_parent_type(chunk, chunk_type_lookup):
        transform_parent = getattr(chunk, "transformParent", None)
        if transform_parent is None:
            return ""
        for candidate in getattr(new_mesh, "chunks", None) or []:
            if (
                getattr(candidate, "type", None) == "CHardAttachment"
                and getattr(candidate, "chunkIndex", None) == transform_parent
            ):
                return str(chunk_type_lookup.get(getattr(candidate, "parent", None), "") or "")
        return ""

    def _visual_mesh_identity(chunk):
        if getattr(chunk, "type", None) not in _VISUAL_MESH_COMPONENT_TYPES:
            return None
        mesh_path = _repo_path_key(getattr(chunk, "mesh", None))
        embedded_source = _repo_path_key(getattr(chunk, "_embedded_source_path", None))
        embedded_index = getattr(chunk, "_embedded_cmesh_chunk_index", getattr(chunk, "_embedded_mesh_chunk_index", None))
        return (
            getattr(chunk, "type", None),
            mesh_path,
            embedded_source,
            embedded_index,
        )

    def _prune_w2_appearance_meshes_from_base():
        if file.HEADER.version > 115 or not getattr(this_Entity, "appearances", None):
            return

        appearance_mesh_keys = set()
        for appearance in getattr(this_Entity, "appearances", None) or []:
            for template in getattr(appearance, "includedTemplates", None) or []:
                for app_chunk in getattr(template, "chunks", None) or []:
                    if not _is_w2_synthetic_appearance_chunk(app_chunk):
                        continue
                    mesh_key = _repo_path_key(getattr(app_chunk, "mesh", None))
                    if mesh_key:
                        appearance_mesh_keys.add(mesh_key)
        if not appearance_mesh_keys:
            return

        pruned_chunks = []
        removed = []
        for chunk in getattr(new_mesh, "chunks", None) or []:
            if (
                _is_w2_synthetic_appearance_chunk(chunk)
                and _repo_path_key(getattr(chunk, "mesh", None)) in appearance_mesh_keys
            ):
                removed.append((getattr(chunk, "type", None), getattr(chunk, "name", None), getattr(chunk, "chunkIndex", None)))
                continue
            pruned_chunks.append(chunk)
        if removed:
            new_mesh.chunks = pruned_chunks
            log.debug(
                "Pruned %d W2 base appearance mesh chunks from %s: %s",
                len(removed),
                getattr(file, "fileName", ""),
                removed,
            )

    def _prune_w2_external_proxy_duplicate_meshes():
        if file.HEADER.version > 115:
            return
        chunks = list(getattr(new_mesh, "chunks", None) or [])
        if not chunks:
            return

        chunk_type_lookup = _chunk_type_by_index()
        identities_with_real_attachment = set()
        for chunk in chunks:
            identity = _visual_mesh_identity(chunk)
            if not identity:
                continue
            parent_type = _attachment_parent_type(chunk, chunk_type_lookup)
            if parent_type and parent_type != "CExternalProxyComponent":
                identities_with_real_attachment.add(identity)

        if not identities_with_real_attachment:
            return

        pruned_chunks = []
        removed_indices = set()
        for chunk in chunks:
            identity = _visual_mesh_identity(chunk)
            parent_type = _attachment_parent_type(chunk, chunk_type_lookup)
            if identity in identities_with_real_attachment and parent_type == "CExternalProxyComponent":
                removed_indices.add(getattr(chunk, "chunkIndex", None))
                log.debug(
                    "Pruned W2 external-proxy duplicate mesh %s #%s from %s; real attached duplicate exists.",
                    getattr(chunk, "type", None),
                    getattr(chunk, "chunkIndex", None),
                    getattr(file, "fileName", ""),
                )
                continue
            pruned_chunks.append(chunk)

        removed_indices.discard(None)
        if removed_indices:
            new_mesh.chunks = pruned_chunks

    def _prune_orphaned_attachment_chunks():
        chunks = list(getattr(new_mesh, "chunks", None) or [])
        if not chunks:
            return
        attachment_types = {
            "CMeshSkinningAttachment",
            "CAnimatedAttachment",
            "CHardAttachment",
        }
        changed = True
        while changed:
            changed = False
            chunk_indices = {getattr(chunk, "chunkIndex", None) for chunk in chunks}
            orphan_attachment_indices = set()
            for chunk in chunks:
                if getattr(chunk, "type", None) not in attachment_types:
                    continue
                parent_index = getattr(chunk, "parent", None)
                child_index = getattr(chunk, "child", None)
                if parent_index in chunk_indices and child_index in chunk_indices:
                    continue
                attachment_index = getattr(chunk, "chunkIndex", None)
                orphan_attachment_indices.add(attachment_index)
            orphan_attachment_indices.discard(None)
            if not orphan_attachment_indices:
                break
            pruned_chunks = []
            for chunk in chunks:
                if getattr(chunk, "chunkIndex", None) in orphan_attachment_indices:
                    continue
                transform_parent = getattr(chunk, "transformParent", None)
                if transform_parent in orphan_attachment_indices:
                    continue
                pruned_chunks.append(chunk)
            chunks = pruned_chunks
            changed = True
        new_mesh.chunks = chunks

    #ReadTemplate(file, new_mesh, this_Entity)
    if file.HEADER.version <= 115:
        ## Witcher 2 has CExternalProxyComponent that replaces chunks with chunks in the templates include
        #CExternalProxyAttachment + orginal makes for final attachment
        guids = {}

        w2_related_files, _w2_related_complete = _load_w2_related_files_recursive(file, inherit_visited)
        w2_related_entity_paths = [depot_path for depot_path, _full_path, _related_file in w2_related_files]
        w2_related_search_chunks = [
            chunk
            for _depot_path, _full_path, related_file in w2_related_files
            for chunk in related_file.CHUNKS.CHUNKS
        ]
        for chunk in CHUNKS:
            if chunk.name != "CExternalProxyComponent":
                continue
            guid = _w2_chunk_guid(chunk)
            if guid:
                guids[guid] = chunk
            else:
                log.debug(
                    "Skipping W2 CExternalProxyComponent without guid: chunk=%s props=%s",
                    getattr(chunk, "ChunkIndex", None),
                    _chunk_props_summary(chunk),
                )

        for template_chunk in CHUNKS:
            if template_chunk.name == "CEntityTemplate":
                this_Entity.w2_body_part_states = _merge_w2_body_part_state_table(
                    this_Entity.w2_body_part_states,
                    _collect_w2_body_part_state_table(template_chunk),
                )
                w2_body_part_chunk_indices.update(
                    _collect_w2_body_part_chunk_indices(template_chunk, CHUNKS)
                )
                w2_body_part_component_names.update(
                    _collect_w2_body_part_component_names(template_chunk)
            )

    def _prune_w2_duplicate_moving_components():
        if file.HEADER.version > 115:
            return
        target_component = getattr(this_Entity, "MovingPhysicalAgentComponent", None)
        if not target_component:
            return
        target_key = (
            str(getattr(target_component, "name", "") or "").strip().lower(),
            _repo_path_key(getattr(target_component, "skeleton", None)),
        )
        target_chunk_index = getattr(target_component, "chunkIndex", None)
        if not target_key[0] or not target_key[1] or target_chunk_index is None:
            return

        pruned_chunks = []
        removed = []
        for chunk in getattr(new_mesh, "chunks", None) or []:
            if getattr(chunk, "type", None) != "CMovingPhysicalAgentComponent":
                pruned_chunks.append(chunk)
                continue
            chunk_key = (
                str(getattr(chunk, "name", "") or "").strip().lower(),
                _repo_path_key(getattr(chunk, "skeleton", None)),
            )
            if chunk_key == target_key and getattr(chunk, "chunkIndex", None) != target_chunk_index:
                removed.append(getattr(chunk, "chunkIndex", None))
                continue
            pruned_chunks.append(chunk)
        if removed:
            new_mesh.chunks = pruned_chunks
            log.debug(
                "Pruned duplicate W2 CMovingPhysicalAgentComponent chunk(s) from %s: %s",
                getattr(file, "fileName", ""),
                removed,
            )

        for related_chunk in w2_related_search_chunks:
            if related_chunk.name == "CEntityTemplate":
                this_Entity.w2_body_part_states = _merge_w2_body_part_state_table(
                    this_Entity.w2_body_part_states,
                    _collect_w2_body_part_state_table(related_chunk),
                )
                w2_body_part_component_names.update(
                    _collect_w2_body_part_component_names(related_chunk)
                )

    def _resolve_initializer_chunk(init_prop):
        if not init_prop:
            return None

        ptr = None
        if hasattr(init_prop, "Value") and isinstance(init_prop.Value, int):
            ptr = init_prop.Value
        elif hasattr(init_prop, "value"):
            init_value = getattr(init_prop, "value", None)
            if isinstance(init_value, int):
                ptr = init_value
            elif isinstance(init_value, list):
                for candidate in init_value:
                    if isinstance(candidate, int) and candidate > 0:
                        ptr = candidate
                        break

        if (not isinstance(ptr, int) or ptr <= 0) and hasattr(init_prop, "Handles") and init_prop.Handles:
            first_handle = init_prop.Handles[0]
            handle_ptr = getattr(first_handle, "val", None)
            if not isinstance(handle_ptr, int) or handle_ptr <= 0:
                ref_idx = getattr(first_handle, "Reference", None)
                if isinstance(ref_idx, int) and ref_idx >= 0:
                    handle_ptr = ref_idx + 1
            if isinstance(handle_ptr, int) and handle_ptr > 0:
                ptr = handle_ptr

        if not isinstance(ptr, int) or ptr <= 0 or ptr > len(CHUNKS):
            return None
        return CHUNKS[ptr - 1]

    def _resolve_inventory_initializer(inv_entry):
        init_chunk = _resolve_initializer_chunk(getattr(inv_entry, "initializer", None))
        if not init_chunk:
            return None
        if init_chunk.Type == "CInventoryInitializerUniform":
            return w3_types.CInventoryInitializerUniform(init_chunk)
        if init_chunk.Type == "CInventoryInitializerRandom":
            return w3_types.CInventoryInitializerRandom(init_chunk)
        # Last parsed-data path: instantiate a matching initializer type when available.
        try:
            return w3_types.str_to_class(init_chunk.Type)(init_chunk)
        except Exception:
            return None

    def _resolve_equipment_initializer(equip_entry):
        init_chunk = _resolve_initializer_chunk(getattr(equip_entry, "initializer", None))
        if not init_chunk:
            return None
        if init_chunk.Type == "CEquipmentInitializerUniform":
            return w3_types.CEquipmentInitializerUniform(init_chunk)
        if init_chunk.Type == "CEquipmentInitializerRandom":
            return w3_types.CEquipmentInitializerRandom(init_chunk)
        try:
            return w3_types.str_to_class(init_chunk.Type)(init_chunk)
        except Exception:
            return None

    def _parse_inventory_definition(def_chunk):
        final_inv_entries = []
        inv_def = w3_types.CInventoryDefinition(def_chunk)
        if inv_def.entries:
            entry_ptrs = []
            if hasattr(inv_def.entries, "value") and inv_def.entries.value:
                entry_ptrs = inv_def.entries.value
            elif hasattr(inv_def.entries, "Handles") and inv_def.entries.Handles:
                entry_ptrs = [handle.val for handle in inv_def.entries.Handles]
            for inv_ptr in entry_ptrs:
                if not isinstance(inv_ptr, int) or inv_ptr <= 0 or inv_ptr > len(CHUNKS):
                    continue
                inv_entry = w3_types.CInventoryDefinitionEntry(CHUNKS[inv_ptr-1])
                resolved_init = _resolve_inventory_initializer(inv_entry)
                if resolved_init:
                    inv_entry.initializer = resolved_init
                final_inv_entries.append(inv_entry)
        setattr(inv_def, 'entries', final_inv_entries)
        return inv_def

    seen_effects = set()
    for chunk in CHUNKS:
        if chunk.Type == "CGameplayLightComponent" and this_Entity.isLightOn is None:
            this_Entity.isLightOn = _chunk_prop_bool(chunk, "isLightOn", default=None)
        if chunk.Type != "CEntityTemplate":
            continue
        for effect in (
            _extract_cooked_entity_effects(chunk)
            + _extract_uncooked_entity_effects(chunk, CHUNKS)
        ):
            particle_paths = tuple(
                str(item.get("path", "") or "").lower()
                for item in effect.get("particle_systems", [])
            )
            effect_key = (str(effect.get("name", "") or "").lower(), particle_paths)
            if effect_key in seen_effects:
                continue
            seen_effects.add(effect_key)
            this_Entity.cookedEffects.append(effect)

    for chunk in CHUNKS:
        if _w2_should_skip_unselected_graph_chunk(chunk):
            continue
        if chunk.Type == "CEntityTemplate":
            includes = chunk.GetVariableByName("includes")
            if includes and hasattr(includes, "Handles"):
                for include in includes.Handles:
                    depot_path = _resolve_handle_repo_path(chunk, include, ".w2ent")
                    if not depot_path:
                        continue
                    depot_key = str(depot_path).lower()
                    if depot_key in top_level_template_include_set:
                        continue
                    top_level_template_include_set.add(depot_key)
                    top_level_template_includes.append(depot_path)

            template_params = chunk.GetVariableByName("templateParams")
            if template_params:
                # Try both .value and .More accessors for array elements
                params_array = None
                if hasattr(template_params, "value") and template_params.value:
                    params_array = template_params.value
                elif hasattr(template_params, "More") and template_params.More:
                    # .More typically contains objects, need to get their pointer values
                    params_array = []
                    for param in template_params.More:
                        if hasattr(param, "Value"):
                            params_array.append(param.Value)
                        elif hasattr(param, "ChunkIndex"):
                            params_array.append(param.ChunkIndex + 1)  # ChunkIndex is 0-based, ptr is 1-based

                if params_array:
                    for ptr in params_array:
                        if isinstance(ptr, int) and ptr > 0 and ptr <= len(CHUNKS):
                            def_chunk = CHUNKS[ptr - 1]
                            if def_chunk.Type == "CInventoryDefinition":
                                inv_def = _parse_inventory_definition(def_chunk)
                                if not hasattr(this_Entity, "inventoryDefinitions"):
                                    this_Entity.inventoryDefinitions = []
                                this_Entity.inventoryDefinitions.append(inv_def)
                                log.debug(
                                    "Added inventory definition with %d entries",
                                    len(inv_def.entries) if inv_def.entries else 0,
                                )

    # Secondary parsed-data pass for cooked files that expose inventory definitions directly.
    if not hasattr(this_Entity, "inventoryDefinitions") or not this_Entity.inventoryDefinitions:
        for chunk in CHUNKS:
            if chunk.Type == "CInventoryDefinition":
                inv_def = _parse_inventory_definition(chunk)
                if not hasattr(this_Entity, "inventoryDefinitions"):
                    this_Entity.inventoryDefinitions = []
                this_Entity.inventoryDefinitions.append(inv_def)
                log.debug(
                    "Added direct inventory definition with %d entries",
                    len(inv_def.entries) if inv_def.entries else 0,
                )

    for chunk in CHUNKS:
        if _w2_should_skip_unselected_graph_chunk(chunk):
            continue
        if chunk.Type in {"CEntityTemplate", "CEntityExternalAppearance"}:
            slots = chunk.GetVariableByName("slots")
            if slots:
                for slot in _iter_struct_items(slots):
                    currentSlot = w3_types.EntitySlot(False, slot)
                    currentSlot.transform = currentSlot.transform.EngineTransform if currentSlot.transform else None
                    this_Entity.slots.append(currentSlot)

            if chunk.Type != "CEntityExternalAppearance" and not chunk.GetVariableByName("appearances"):
                continue

            if chunk.Type == "CEntityExternalAppearance":
                appearances = [chunk.GetVariableByName("appearance")]
            else:
                appearances = chunk.GetVariableByName("appearances").More
            for appearance in appearances:
                currentApp = w3_types.CEntityAppearance(False, appearance)
                if file.HEADER.version <= 115:
                    currentApp.w2_parts = _extract_cname_array_values(_find_prop_by_name(appearance, "parts"))
                    currentApp.w2_headName = _prop_to_string(_find_prop_by_name(appearance, "headName"))
                if currentApp.includedTemplates:
                    final_includedTemplates = []
                    includedTemplates = currentApp.includedTemplates.ToArray()
                    for entryTemplate in includedTemplates:
                        entry = entryTemplate.DepotPath
                        (templateMesh, entity) = LoadCEntityTemplateFile(entry, file.HEADER.version)
                        final_includedTemplates.append(templateMesh)
                    setattr(currentApp, 'includedTemplates', final_includedTemplates) # Replace pointers with chunks
                elif appearance.GetVariableByName("parts"): #!WITCHER 2
                    pending_w2_appearances.append((chunk, appearance, currentApp))
                else:
                    #some "invisible" appearances have no entities attached
                    log.warning("Entity has no includedTemplates")
                    #GetFace(@"characters\models\geralt\head\model\h_01_mg__geralt.w3fac")
                if currentApp.appearanceParams:
                    final_CEquipmentDefinitions = []
                    for ptr in currentApp.appearanceParams.value:
                        def_chunk = CHUNKS[ptr-1]
                        if def_chunk.Type == 'CEquipmentDefinition':
                            final_entries = []
                            CEquipmentDefinition = w3_types.CEquipmentDefinition(def_chunk)
                            if CEquipmentDefinition.entries:
                                entry_ptrs = []
                                if hasattr(CEquipmentDefinition.entries, "value") and CEquipmentDefinition.entries.value:
                                    entry_ptrs = CEquipmentDefinition.entries.value
                                elif hasattr(CEquipmentDefinition.entries, "Handles") and CEquipmentDefinition.entries.Handles:
                                    entry_ptrs = [handle.val for handle in CEquipmentDefinition.entries.Handles]
                                for ptr in entry_ptrs:
                                    if not isinstance(ptr, int) or ptr <= 0 or ptr > len(CHUNKS):
                                        continue
                                    entry = w3_types.CEquipmentDefinitionEntry(CHUNKS[ptr-1])
                                    resolved_init = _resolve_equipment_initializer(entry)
                                    if resolved_init:
                                        entry.initializer = resolved_init
                                        if not getattr(entry, "defaultItemName", None):
                                            init_item_name = getattr(resolved_init, "itemName", None) or getattr(resolved_init, "item", None)
                                            if init_item_name:
                                                entry.defaultItemName = init_item_name
                                    final_entries.append(entry)
                            setattr(CEquipmentDefinition, 'entries', final_entries) # Replace pointers with chunks
                            final_CEquipmentDefinitions.append(CEquipmentDefinition)
                        elif def_chunk.Type == 'CInventoryDefinition':
                            # Parse inventory definitions separately from appearance equipment.
                            inv_def = _parse_inventory_definition(def_chunk)
                            # Store on appearance for later processing
                            if not hasattr(currentApp, 'inventoryDefinitions'):
                                currentApp.inventoryDefinitions = []
                            currentApp.inventoryDefinitions.append(inv_def)
                    setattr(currentApp, 'appearanceParams', final_CEquipmentDefinitions) # Replace pointers with chunks
                this_Entity.appearances.append(currentApp)
                #print(appearance.elementName)
                
            coloringEntries = chunk.GetVariableByName("coloringEntries")
            if coloringEntries:
                for coloringEntry in coloringEntries.More:
                    if coloringEntries.Count == 1:
                        coloringEntry = coloringEntries
                    colorShift1 = coloringEntry.GetVariableByName('colorShift1')
                    if colorShift1:
                        colorShift1 = w3_types.CColorShift(colorShift1.GetVariableByName('hue').Value if colorShift1.GetVariableByName('hue') else 0,
                                                           colorShift1.GetVariableByName('saturation').Value if colorShift1.GetVariableByName('saturation') else 0,
                                                           colorShift1.GetVariableByName('luminance').Value if colorShift1.GetVariableByName('luminance') else 0)
                    colorShift2 = coloringEntry.GetVariableByName('colorShift2')
                    if colorShift2:
                        colorShift2 =  w3_types.CColorShift(colorShift2.GetVariableByName('hue').Value if colorShift2.GetVariableByName('hue') else 0,
                                                           colorShift2.GetVariableByName('saturation').Value if colorShift2.GetVariableByName('saturation') else 0,
                                                           colorShift2.GetVariableByName('luminance').Value if colorShift2.GetVariableByName('luminance') else 0)
                    this_Entity.coloringEntries.append(
                        w3_types.SEntityTemplateColoringEntry(
                            coloringEntry.GetVariableByName('appearance').ToString(),
                            coloringEntry.GetVariableByName('componentName').ToString(),
                            colorShift1,
                            colorShift2))
                        # { 'name': "MimicSets",
                        #   'animationSets':list(map(lambda x: x.DepotPath, chunk.GetVariableByName("animationSets").ToArray()))
                        # })

        elif (
            is_entity_chunk(chunk)
            or chunk.ChunkIndex in w2_selected_entity_roots
        ):
            entity_chunk = chunk  # save before inner loop reassigns chunk variable
            this_Entity.is_entity = True
            if not this_Entity.type:
                this_Entity.type = entity_chunk.Type
            entity_animated_component_chunk_index = None  # track for synthetic skinning attachment
            if hasattr(chunk, 'Components'):
            #for staticChunkPtr in chunk.GetVariableByName("components").ToArray():
                if not chunk.Components and mesh_import_paths:
                    log_fn = log.debug if file.HEADER.version <= 115 or top_level_template_includes else log.warning
                    fallback_note = "top-level mesh fallback"
                    if top_level_template_includes:
                        fallback_note += " plus inherited template components"
                    log_fn(
                        f"{chunk.Type} has empty Components list while mesh imports exist "
                        f"({len(mesh_import_paths)}), using {fallback_note}: {file.fileName}"
                    )
                for chunk_idx in chunk.Components:
                    if not isinstance(chunk_idx, int) or chunk_idx <= 0 or chunk_idx > len(CHUNKS):
                        log.debug(
                            "Skipping invalid component reference %s in %s: chunk_count=%s file=%s",
                            chunk_idx,
                            getattr(entity_chunk, "Type", None),
                            len(CHUNKS),
                            getattr(file, "fileName", ""),
                        )
                        continue
                    chunk = CHUNKS[chunk_idx-1] #staticChunkPtr.Reference
                    chunk_name = _prop_to_string(_find_prop_by_name(chunk, "name"))
                    if chunk.ChunkIndex in w2_body_part_chunk_indices or str(chunk_name or "").strip().lower() in w2_body_part_component_names:
                        continue
                    if (chunk.Type == "CStaticMeshComponent"):
                        static_mesh_component = _resolve_component_mesh(chunk, CStaticMeshComponent(chunk).convert_for_io(), _next_mesh_import_path)
                        if static_mesh_component.mesh:
                            _append_unique_chunk(chunk, static_mesh_component, added_chunks)
                        else:
                            log.warning(
                                f"Skipping CStaticMeshComponent with invalid mesh ref: {chunk.ChunkIndex}; "
                                f"props={_chunk_props_summary(chunk)}"
                            )
                    elif chunk.Type in ("CMeshComponent", "CDressMeshComponent"):
                        mesh_component = _resolve_component_mesh(chunk, _coerce_w2_mesh_component_for_io(chunk, CMeshComponent(chunk).convert_for_io()), _next_mesh_import_path)
                        if mesh_component.mesh:
                            _append_unique_chunk(chunk, mesh_component, added_chunks)
                        else:
                            log.warning(
                                f"Skipping CMeshComponent with invalid mesh ref: {chunk.ChunkIndex}; "
                                f"props={_chunk_props_summary(chunk)}"
                            )
                    elif (chunk.Type == "CRigidMeshComponent" or chunk.Type == "CRagdollMeshComponent"):
                        if chunk.Type == "CRagdollMeshComponent" and chunk.GetVariableByName("mesh") is None:
                            log.debug(
                                f"Skipping physics-only CRagdollMeshComponent (no mesh ref): {chunk.ChunkIndex}; "
                                f"props={_chunk_props_summary(chunk)}"
                            )
                        else:
                            mesh_cls = CRagdollMeshComponent if chunk.Type == "CRagdollMeshComponent" else CMeshComponent
                            mesh_component = _resolve_component_mesh(chunk, mesh_cls(chunk).convert_for_io(), _next_mesh_import_path)
                            if chunk.Type == "CRagdollMeshComponent":
                                mesh_component.ragdoll_meta = _extract_w2_ragdoll_meta(chunk, CHUNKS)
                            if mesh_component.mesh:
                                _append_unique_chunk(chunk, mesh_component, added_chunks)
                            else:
                                log.warning(
                                    f"Skipping {chunk.Type} with invalid mesh ref: {chunk.ChunkIndex}; "
                                    f"props={_chunk_props_summary(chunk)}"
                                )
                    elif (chunk.Type == "CFurComponent"):
                        fur_component = _resolve_component_mesh(chunk, CMeshComponent(chunk).convert_for_io(), _next_mesh_import_path)
                        if fur_component.mesh:
                            _append_unique_chunk(chunk, fur_component, added_chunks)
                        else:
                            log.warning(
                                f"Skipping CFurComponent with invalid mesh ref: {chunk.ChunkIndex}; "
                                f"props={_chunk_props_summary(chunk)}"
                            )
                    elif chunk.Type in _VISUAL_MESH_COMPONENT_TYPES:
                        mesh_component = _resolve_component_mesh(chunk, CMeshComponent(chunk).convert_for_io(), _next_mesh_import_path)
                        if mesh_component.mesh:
                            _append_unique_chunk(chunk, mesh_component, added_chunks)
                        else:
                            log.warning(
                                f"Skipping {chunk.Type} with invalid mesh ref: {chunk.ChunkIndex}; "
                                f"props={_chunk_props_summary(chunk)}"
                            )
                    elif chunk.Type == "CPointLightComponent":
                        _append_unique_light_chunk(chunk, CPointLightComponent(chunk).convert_for_io(), added_chunks)
                    elif chunk.Type == "CSpotLightComponent":
                        _append_unique_light_chunk(chunk, CSpotLightComponent(chunk).convert_for_io(), added_chunks)
                    elif (chunk.Type == "CAnimatedComponent"):
                        animated_component = CAnimatedComponent(chunk).convert_for_io()
                        name = _chunk_prop_string(chunk, "name")
                        skeleton = _resolve_repo_path(chunk, "skeleton", ".w2rig")
                        animation_sets = _resolve_repo_paths_from_array(chunk, "animationSets", ".w2anims")
                        if not skeleton:
                            # Component may be an override chunk that stores only
                            # non-skeleton properties (e.g. transform). Fall back to
                            # the first CSkeleton referenced in the file's import table.
                            rig_paths = (
                                _collect_rig_import_paths(file)
                                if _allow_unscoped_import_fallbacks
                                else []
                            )
                            if rig_paths:
                                skeleton = rig_paths[0]
                                log.debug(
                                    f"CAnimatedComponent #{chunk.ChunkIndex} has no skeleton "
                                    f"property; using import-table fallback: {skeleton}"
                                )
                        entity_animated_component_chunk_index = chunk.ChunkIndex
                        animated_component.name = name or animated_component.name
                        animated_component.skeleton = skeleton
                        animated_component.animationSets = animation_sets
                        _append_unique_animated_chunk(chunk, animated_component, added_chunks)
                    elif (chunk.Type == "CCameraComponent"):
                        name = _chunk_prop_string(chunk, "name")
                        chunk_append(new_mesh, chunk, CCameraComponent(name), added_chunks)
            # Cooked item entities (Crossbow, CItemEntity, CWitcherSword, etc.) and
            # uncooked redkit *_dialogue.w2ent preview entities (a generic CEntity
            # wrapper with empty Components) both park their visual components inside
            # a SharedDataBuffer rather than as direct Components.  If the entity has
            # one, parse it and extract every recognized mesh component type.
            sdb = entity_chunk.GetVariableByName('streamingDataBuffer')
            if sdb and hasattr(sdb, 'Bufferdata') and hasattr(sdb.Bufferdata, 'Bytes') and sdb.Bufferdata.Bytes:
                try:
                    buf_stream = bReadStream(sdb.Bufferdata.Bytes, name='streamingDataBuffer')
                    buf_cr2w = getCR2W(buf_stream)
                    for buf_chunk in buf_cr2w.CHUNKS.CHUNKS:
                        if buf_chunk.Type in _FOLIAGE_COMPONENT_TYPES:
                            foliage_component = _foliage_component_from_chunk(buf_chunk)
                            if foliage_component is not None:
                                chunk_append(new_mesh, buf_chunk, foliage_component)
                            continue
                        if buf_chunk.Type == 'CStaticMeshComponent':
                            mc = CStaticMeshComponent(buf_chunk).convert_for_io()
                        elif buf_chunk.Type == 'CRagdollMeshComponent':
                            if buf_chunk.GetVariableByName("mesh") is None:
                                continue
                            mc = CRagdollMeshComponent(buf_chunk).convert_for_io()
                            mc.ragdoll_meta = _extract_w2_ragdoll_meta(buf_chunk, buf_cr2w.CHUNKS.CHUNKS)
                        elif buf_chunk.Type in _VISUAL_MESH_COMPONENT_TYPES:
                            mc = CMeshComponent(buf_chunk).convert_for_io()
                        else:
                            continue
                        mc = _resolve_component_mesh(buf_chunk, mc, _next_mesh_import_path)
                        if _register_streamed_slot_mesh(mc, buf_chunk.Type):
                            continue
                        if mc.mesh and _append_unique_chunk(buf_chunk, mc):
                            log.debug(
                                'Extracted %s from streamingDataBuffer of %s #%s: %s',
                                buf_chunk.Type, entity_chunk.Type, entity_chunk.ChunkIndex, mc.mesh,
                            )
                            # Bind the entity's rig (CAnimatedComponent) to skinned meshes
                            # via a synthesized CMeshSkinningAttachment, like cooked levels do.
                            if (entity_animated_component_chunk_index is not None
                                    and buf_chunk.Type in ('CMeshComponent', 'CRigidMeshComponent', 'CRagdollMeshComponent', 'CFurComponent')):
                                skinning = CMeshSkinningAttachment(
                                    entity_animated_component_chunk_index,
                                    buf_chunk.ChunkIndex,
                                )
                                skinning.type = 'CMeshSkinningAttachment'
                                skinning.chunkIndex = -1  # synthetic, no real CR2W chunk
                                new_mesh.chunks.append(skinning)
                except Exception as e:
                    log.warning(
                        'Failed to parse streamingDataBuffer for %s #%s: %s',
                        entity_chunk.Type, entity_chunk.ChunkIndex, e,
                    )
            elif entity_chunk.Type in _STREAMED_ITEM_CHUNK_TYPES:
                log.debug(
                    '%s #%s: streamingDataBuffer not in PROPS or has no Bytes '
                    '(sdb=%s, PROPS=%s); mesh sourced from flatCompiledData if available.',
                    entity_chunk.Type, entity_chunk.ChunkIndex,
                    sdb, _chunk_props_summary(entity_chunk),
                )
        elif chunk.Type == "CExternalProxyComponent":
            _append_w2_proxy_component(chunk)
        elif chunk.Type == "CExternalProxyAttachment":
            attachment = _materialize_w2_external_proxy_attachment(chunk)
            if attachment is not None:
                new_mesh.chunks.append(attachment)
                added_chunks.add(getattr(attachment, "chunkIndex", getattr(chunk, "ChunkIndex", 0)))
        elif (chunk.Type == "CHardAttachment"):
            if chunk.ChunkIndex in w2_external_proxy_original_attachment_indices:
                continue
            #if (chunk.GetVariableByName("parentSlot")): 
            chunk_append(new_mesh, chunk, CHardAttachment(chunk).convert_for_io())
        elif (chunk.Type == "CMeshSkinningAttachment"):
            parent_var = chunk.GetVariableByName("parent")
            child_var = chunk.GetVariableByName("child")
            if parent_var and child_var:
                chunk_append(new_mesh, chunk, CMeshSkinningAttachment(parent_var.Value-1, child_var.Value-1))
            elif not _chunk_prop_bool(chunk, "isBroken", default=False):
                log.warning(f'CMeshSkinningAttachment missing parent or child at chunk {chunk.ChunkIndex}')
        elif (chunk.Type == "CSkeletonBoneSlot"):
            bone_index_var = chunk.GetVariableByName("boneIndex")
            boneIndex = getattr(bone_index_var, "Value", None)
            if boneIndex is not None:
                chunk_append(new_mesh, chunk, CSkeletonBoneSlot(boneIndex))
            else:
                log.debug(
                    "Skipping CSkeletonBoneSlot without boneIndex at chunk %s; props=%s",
                    getattr(chunk, "ChunkIndex", None),
                    _chunk_props_summary(chunk),
                )
        elif(chunk.name == "CMovingPhysicalAgentComponent" and chunk.GetVariableByName("skeleton")):
            name = _chunk_prop_string(chunk, "name")
            skeleton = _resolve_repo_path(chunk, "skeleton", ".w2rig")
            moving_component = w3_types.CMovingPhysicalAgentComponent(skeleton, name)
            moving_component.animationSets = _resolve_repo_paths_from_array(chunk, "animationSets", ".w2anims")
            chunk_append(new_mesh, chunk, moving_component)
            hasCMovingPhysicalAgentComponent = True;
            this_Entity.MovingPhysicalAgentComponent= new_mesh.chunks[-1]
        elif(chunk.name == "CAnimAnimsetsParam"):
            if chunk.GetVariableByName("animationSets"):
                this_Entity.CAnimAnimsetsParam.append({
                    'name': _chunk_prop_string(chunk, "name"),
                    'componentName': _chunk_prop_string(chunk, "componentName"),
                    'animationSets': _resolve_repo_paths_from_array(chunk, "animationSets", ".w2anims"),
                })
        elif(chunk.name == "CAnimMimicParam"):
            if chunk.GetVariableByName("animationSets"):
                this_Entity.CAnimMimicParam.append({ 'name': "MimicSets",
                                                'animationSets':_resolve_repo_paths_from_array(chunk, "animationSets", ".w2anims")
                                            })
        
        ############
        #ITEMS FROM MESH
        #
        #######
        # Only add top-level mesh chunks if they weren't already added via CEntity.Components
        elif (chunk.Type == "CStaticMeshComponent") and chunk.ChunkIndex not in added_chunks and chunk.ChunkIndex not in w2_body_part_chunk_indices and str(_prop_to_string(_find_prop_by_name(chunk, "name")) or "").strip().lower() not in w2_body_part_component_names:
            mc = _resolve_component_mesh(chunk, CStaticMeshComponent(chunk).convert_for_io(), _next_mesh_import_path)
            if mc.mesh:
                _append_unique_chunk(chunk, mc, added_chunks)
            else:
                log.warning(
                    f"Skipping top-level CStaticMeshComponent with invalid mesh ref at chunk {chunk.ChunkIndex}; "
                    f"props={_chunk_props_summary(chunk)}"
                )
        elif chunk.Type in ("CMeshComponent", "CDressMeshComponent") and chunk.ChunkIndex not in added_chunks and chunk.ChunkIndex not in w2_body_part_chunk_indices and str(_prop_to_string(_find_prop_by_name(chunk, "name")) or "").strip().lower() not in w2_body_part_component_names:
            mc = _resolve_component_mesh(chunk, _coerce_w2_mesh_component_for_io(chunk, CMeshComponent(chunk).convert_for_io()), _next_mesh_import_path)
            if mc.mesh:
                _append_unique_chunk(chunk, mc, added_chunks)
            else:
                log.warning(
                    f"Skipping top-level CMeshComponent with invalid mesh ref at chunk {chunk.ChunkIndex}; "
                    f"props={_chunk_props_summary(chunk)}"
                )
        elif (chunk.Type == "CRigidMeshComponent" or chunk.Type == "CRagdollMeshComponent") and chunk.ChunkIndex not in added_chunks and chunk.ChunkIndex not in w2_body_part_chunk_indices and str(_prop_to_string(_find_prop_by_name(chunk, "name")) or "").strip().lower() not in w2_body_part_component_names:
            if chunk.Type == "CRagdollMeshComponent" and chunk.GetVariableByName("mesh") is None:
                log.debug(
                    f"Skipping top-level physics-only CRagdollMeshComponent (no mesh ref) at chunk {chunk.ChunkIndex}; "
                    f"props={_chunk_props_summary(chunk)}"
                )
            else:
                mesh_cls = CRagdollMeshComponent if chunk.Type == "CRagdollMeshComponent" else CMeshComponent
                mc = _resolve_component_mesh(chunk, mesh_cls(chunk).convert_for_io(), _next_mesh_import_path)
                if chunk.Type == "CRagdollMeshComponent":
                    mc.ragdoll_meta = _extract_w2_ragdoll_meta(chunk, CHUNKS)
                if mc.mesh:
                    _append_unique_chunk(chunk, mc, added_chunks)
                else:
                    log.warning(
                        f"Skipping top-level {chunk.Type} with invalid mesh ref at chunk {chunk.ChunkIndex}; "
                        f"props={_chunk_props_summary(chunk)}"
                    )
        elif (chunk.Type == "CClothComponent") and chunk.ChunkIndex not in added_chunks and chunk.ChunkIndex not in w2_body_part_chunk_indices and str(_prop_to_string(_find_prop_by_name(chunk, "name")) or "").strip().lower() not in w2_body_part_component_names:
            if chunk.GetVariableByName("resource"): #! sometimes there are no resource in files??
                cloth = chunk.GetVariableByName("resource").ToString()
                _cname_prop = chunk.GetVariableByName("name")
                _cname = str(_prop_to_string(_cname_prop) or "").strip()
                chunk_append(new_mesh, chunk, CClothComponent(cloth, name=_cname), added_chunks)
        elif (chunk.Type == "CFurComponent") and chunk.ChunkIndex not in added_chunks and chunk.ChunkIndex not in w2_body_part_chunk_indices and str(_prop_to_string(_find_prop_by_name(chunk, "name")) or "").strip().lower() not in w2_body_part_component_names:
            if (chunk.GetVariableByName("mesh")):
                fur_component = _resolve_component_mesh(chunk, CMeshComponent(chunk).convert_for_io(), _next_mesh_import_path)
                if fur_component.mesh:
                    _append_unique_chunk(chunk, fur_component, added_chunks)
                else:
                    log.warning(
                        f"Skipping top-level CFurComponent with invalid mesh ref: {chunk.ChunkIndex}; "
                        f"props={_chunk_props_summary(chunk)}"
                    )
        elif chunk.Type in _VISUAL_MESH_COMPONENT_TYPES and chunk.ChunkIndex not in added_chunks and chunk.ChunkIndex not in w2_body_part_chunk_indices and str(_prop_to_string(_find_prop_by_name(chunk, "name")) or "").strip().lower() not in w2_body_part_component_names:
            mesh_component = _resolve_component_mesh(chunk, CMeshComponent(chunk).convert_for_io(), _next_mesh_import_path)
            if mesh_component.mesh:
                _append_unique_chunk(chunk, mesh_component, added_chunks)
            else:
                log.warning(
                    f"Skipping top-level {chunk.Type} with invalid mesh ref: {chunk.ChunkIndex}; "
                    f"props={_chunk_props_summary(chunk)}"
                )
        elif chunk.Type in _FOLIAGE_COMPONENT_TYPES and chunk.ChunkIndex not in added_chunks:
            foliage_component = _foliage_component_from_chunk(chunk)
            if foliage_component is not None:
                chunk_append(new_mesh, chunk, foliage_component, added_chunks)
        elif (chunk.Type == "CPointLightComponent") and chunk.ChunkIndex not in added_chunks and chunk.ChunkIndex not in w2_body_part_chunk_indices and str(_prop_to_string(_find_prop_by_name(chunk, "name")) or "").strip().lower() not in w2_body_part_component_names:
            _append_unique_light_chunk(chunk, CPointLightComponent(chunk).convert_for_io(), added_chunks)
        elif (chunk.Type == "CSpotLightComponent") and chunk.ChunkIndex not in added_chunks and chunk.ChunkIndex not in w2_body_part_chunk_indices and str(_prop_to_string(_find_prop_by_name(chunk, "name")) or "").strip().lower() not in w2_body_part_component_names:
            _append_unique_light_chunk(chunk, CSpotLightComponent(chunk).convert_for_io(), added_chunks)
        elif (chunk.Type == "CAnimatedComponent") and chunk.ChunkIndex not in added_chunks and chunk.ChunkIndex not in w2_body_part_chunk_indices and str(_prop_to_string(_find_prop_by_name(chunk, "name")) or "").strip().lower() not in w2_body_part_component_names:
            animated_component = CAnimatedComponent(chunk).convert_for_io()
            name = _chunk_prop_string(chunk, "name")
            skeleton = _resolve_repo_path(chunk, "skeleton", ".w2rig")
            animation_sets = _resolve_repo_paths_from_array(chunk, "animationSets", ".w2anims")
            if not skeleton:
                rig_paths = (
                    _collect_rig_import_paths(file)
                    if _allow_unscoped_import_fallbacks
                    else []
                )
                if rig_paths:
                    skeleton = rig_paths[0]
                    log.debug(
                        f"CAnimatedComponent #{chunk.ChunkIndex} has no skeleton "
                        f"property; using import-table fallback: {skeleton}"
                    )
            animated_component.name = name or animated_component.name
            animated_component.skeleton = skeleton
            animated_component.animationSets = animation_sets
            _append_unique_animated_chunk(chunk, animated_component, added_chunks)
        elif (chunk.Type == "CMorphedMeshComponent"):
            morphTarget = _resolve_repo_path(chunk, "morphTarget", ".w2mesh")
            morphSource = _resolve_repo_path(chunk, "morphSource", ".w2mesh")
            morphComponentId = _chunk_prop_string(chunk, "morphComponentId")
            chunk_append(new_mesh, chunk, CMorphedMeshComponent(morphTarget, morphSource, morphComponentId))
        elif (chunk.Type == "CMimicComponent"):
            name = _chunk_prop_string(chunk, "name")
            mimicFace = (
                _resolve_repo_path(chunk, "mimicFace", ".w3fac")
                or _resolve_repo_path(chunk, "mimicFace", ".w2fac")
            )
            chunk_append(new_mesh, chunk, CMimicComponent(name, mimicFace))
            #TODO GetFACE needed?
            #new_mesh.animation_face_object = GetFace(mimicFace)
        # elif (chunk.Type == "CMeshSkinningAttachment"):
        #     parent = chunk.GetVariableByName("parent").Value-1
        #     child = chunk.GetVariableByName("child").Value-1
        #     chunk_append(new_mesh, chunk, CMeshSkinningAttachment(parent, child))
        elif (chunk.Type == "CAnimatedAttachment"):
            parent_var = chunk.GetVariableByName("parent")
            child_var = chunk.GetVariableByName("child")
            if parent_var and child_var:
                chunk_append(new_mesh, chunk, CAnimatedAttachment(parent_var.Value-1, child_var.Value-1))
            elif not _chunk_prop_bool(chunk, "isBroken", default=False):
                log.warning(f'CAnimatedAttachment missing parent or child at chunk {chunk.ChunkIndex}')
        elif (chunk.Type == "CAnimDangleBufferComponent"):
            name = _chunk_prop_string(chunk, "name")
            skeleton = _resolve_repo_path(chunk, "skeleton", ".w2rig")
            chunk_append(new_mesh, chunk, CAnimDangleBufferComponent(name, skeleton))
        elif (chunk.Type == "CAnimDangleComponent"):
            name = _chunk_prop_string(chunk, "name")
            constraint_var = chunk.GetVariableByName("constraint")
            constraint = constraint_var.Value - 1 if constraint_var else None
            chunk_append(new_mesh, chunk, CAnimDangleComponent(name, constraint))
        elif (chunk.Type == "CAnimDangleConstraint_Dyng"):
            dyng = _resolve_repo_path(chunk, "dyng", ".w3dyng") if chunk.GetVariableByName("dyng") else None
            if not dyng and chunk.GetVariableByName("dyng"):
                dyng = _resolve_repo_path(chunk, "dyng", ".dyng")
            skeleton = _resolve_repo_path(chunk, "skeleton", ".w2rig") if chunk.GetVariableByName("skeleton") else None
            chunk_append(new_mesh, chunk, CAnimDangleConstraint_Dyng(skeleton, dyng, _dyng_constraint_settings_from_chunk(chunk)))
        elif (chunk.Type == "CAnimDangleConstraint_Breast"):
            skeleton = _resolve_repo_path(chunk, "skeleton", ".w2rig")
            chunk_append(new_mesh, chunk, CAnimDangleConstraint_Breast(skeleton, _breast_constraint_settings_from_chunk(chunk)))
        elif (chunk.Type in CAnimDangleConstraint_types):
            skeleton = _resolve_repo_path(chunk, "skeleton", ".w2rig")
            chunk_append(new_mesh, chunk, CAnimDangleConstraint_types[chunk.Type](skeleton))
        # elif (chunk.Type == "CHardAttachment"): #TODO NormalBlend Stuff
        #     if (chunk.GetVariableByName("parentSlot")):
        #         chunk_append(new_mesh, chunk, CHardAttachment(chunk).convert_for_io())
        ############
        #ITEMS FROM MESH END
        #
        #######


    if file.HEADER.version <= 115:
        for head_name in _iter_w2_head_param_names(CHUNKS):
            for head_chunk in _build_w2_head_chunks(CHUNKS, head_name, file):
                _append_unique_generated_chunk(head_chunk)

    if flat_compiled_file is not None:
        try:
            resolved_entity = create_CEntity(
                flat_compiled_file,
                _inherit_visited=inherit_visited,
                _included_entity_assets=included_entity_assets,
            )
            resolved_mesh = getattr(resolved_entity, "staticMeshes", None)
            if resolved_mesh and getattr(resolved_mesh, "chunks", None):
                new_mesh = resolved_mesh
                pending_streamed_hard.clear()
            resolved_moving = getattr(resolved_entity, "MovingPhysicalAgentComponent", None)
            if resolved_moving is not None:
                this_Entity.MovingPhysicalAgentComponent = resolved_moving
                hasCMovingPhysicalAgentComponent = True
            if not this_Entity.type:
                this_Entity.type = getattr(resolved_entity, "type", None)
        except Exception as e:
            log.warning("Failed to resolve flatCompiledData for %s: %s", file.fileName, e)

    # Wire streamed slot items to the animated component now that every chunk is known.
    if pending_streamed_hard:
        anim_parent_index = next(
            (getattr(c, "chunkIndex", None) for c in new_mesh.chunks
             if getattr(c, "type", None) == "CAnimatedComponent"),
            entity_animated_component_chunk_index,
        )
        if anim_parent_index is not None:
            for mesh_idx, slot, comp_transform in pending_streamed_hard:
                hard = CHardAttachment()
                hard.parent = anim_parent_index
                hard.child = mesh_idx
                hard.parentSlotName = slot
                hard.relativeTransform = comp_transform
                hard.type = 'CHardAttachment'
                streamed_synth_counter[0] += 1
                hard.chunkIndex = _STREAMED_ATTACHMENT_SYNTH_INDEX_BASE + streamed_synth_counter[0]
                new_mesh.chunks.append(hard)
        else:
            log.warning(
                "Streamed slot items found but no CAnimatedComponent to attach them to (%d items)",
                len(pending_streamed_hard),
            )

    if pending_w2_appearances:
        search_chunks = CHUNKS + [chunk for chunk in w2_related_search_chunks if chunk not in CHUNKS]
        for template_chunk, appearance, current_app in pending_w2_appearances:
            cooked_template = _build_w2_cooked_appearance_template(
                file,
                template_chunk,
                appearance,
                current_app,
                search_chunks,
                set(),
            )
            if cooked_template:
                setattr(current_app, "includedTemplates", [cooked_template])
            else:
                log.debug(
                    "Witcher 2 cooked appearance had no resolved chunks: appearance=%s parts=%s head=%s",
                    getattr(current_app, "name", ""),
                    _extract_cname_array_values(_find_prop_by_name(appearance, "parts")),
                    _prop_to_string(_find_prop_by_name(appearance, "headName")),
                )


    if file.HEADER.version <= 115:
        related_proxy_paths = {
            _repo_path_key(depot_path)
            for depot_path, _full_path, related_file in w2_related_files
            if _related_w2_file_matches_external_proxy(related_file)
        }
        for _depot_path, related_entity in _iter_related_w2_entities():
            if not hasCMovingPhysicalAgentComponent and getattr(related_entity, "MovingPhysicalAgentComponent", None):
                this_Entity.MovingPhysicalAgentComponent = copy.deepcopy(related_entity.MovingPhysicalAgentComponent)
                hasCMovingPhysicalAgentComponent = True
            _merge_related_appearances(related_entity)
            this_Entity.w2_body_part_states = _merge_w2_body_part_state_table(
                this_Entity.w2_body_part_states,
                getattr(related_entity, "w2_body_part_states", None),
            )
            if getattr(related_entity, "slots", None):
                this_Entity.slots = _merge_related_slots(this_Entity.slots, related_entity.slots)
            if getattr(related_entity, "inventoryDefinitions", None):
                current_defs = getattr(this_Entity, "inventoryDefinitions", [])
                this_Entity.inventoryDefinitions = _merge_related_inventory_definitions(current_defs, related_entity.inventoryDefinitions)
            for plan_component in getattr(related_entity, "plan_components", None) or []:
                if plan_component not in this_Entity.plan_components:
                    this_Entity.plan_components.append(copy.deepcopy(plan_component))
            related_chunks = getattr(getattr(related_entity, "staticMeshes", None), "chunks", None)
            if (
                related_chunks
                and _is_w2_proxy_wrapper_entity()
                and _repo_path_key(_depot_path) in related_proxy_paths
                and not _has_mesh_bearing_chunks(getattr(new_mesh, "chunks", None))
            ):
                new_mesh.chunks = copy.deepcopy(related_chunks)

    _merge_inherited_template_components()
    _merge_inherited_slots_inventory_and_effects()
    _merge_inherited_coloring_entries()
    _merge_inherited_anim_params()
    _synthesize_missing_transform_parents_from_hard_attachments()
    _prune_w2_duplicate_moving_components()
    _prune_w2_appearance_meshes_from_base()
    _prune_w2_external_proxy_duplicate_meshes()
    _prune_orphaned_attachment_chunks()

    if not hasCMovingPhysicalAgentComponent:
        for ent in new_mesh.chunks:
            if ent.type == "CAnimatedComponent":
                this_Entity.MovingPhysicalAgentComponent = ent
                break
    this_Entity.staticMeshes = new_mesh
    beh_paths = (
        _collect_beh_import_paths(file)
        if _allow_unscoped_import_fallbacks
        else []
    )
    beh_seen = {_repo_path_key(path) for path in beh_paths}
    for beh_path in inherited_beh_paths:
        beh_key = _repo_path_key(beh_path)
        if not beh_key or beh_key in beh_seen:
            continue
        beh_seen.add(beh_key)
        beh_paths.append(beh_path)
    this_Entity.beh_paths = beh_paths

    included_template_paths = []
    for include_path in top_level_template_includes:
        _append_unique_repo_path(included_template_paths, include_path)
    for include_path in w2_related_entity_paths:
        _append_unique_repo_path(included_template_paths, include_path)
    for _depot_path, include_entity in _iter_top_level_included_entities() or []:
        for inherited_path in getattr(include_entity, "included_template_paths", None) or []:
            _append_unique_repo_path(included_template_paths, inherited_path)
    this_Entity.included_template_paths = included_template_paths
    template_dependency_paths = list(included_template_paths)
    for _depot_path, full_path, _related_file in w2_related_files:
        _append_unique_repo_path(template_dependency_paths, full_path)
    for appearance in this_Entity.appearances:
        for template in getattr(appearance, "includedTemplates", None) or []:
            _append_unique_repo_path(
                template_dependency_paths,
                getattr(template, "templateFilename", ""),
            )
            for dependency_path in getattr(template, "template_dependency_paths", None) or []:
                _append_unique_repo_path(template_dependency_paths, dependency_path)
    for _depot_path, include_entity in _iter_top_level_included_entities() or []:
        for dependency_path in getattr(include_entity, "template_dependency_paths", None) or []:
            _append_unique_repo_path(template_dependency_paths, dependency_path)
    this_Entity.template_dependency_paths = template_dependency_paths
    unsupported_source_chunks = CHUNKS
    if file.HEADER.version <= 115 and w2_selected_graph_indices:
        unsupported_source_chunks = [
            CHUNKS[index]
            for index in sorted(w2_selected_graph_indices)
            if 0 <= index < len(CHUNKS)
        ]
    unsupported_components = _unsupported_entity_visual_chunk_types(
        unsupported_source_chunks
    )
    for source_chunk in unsupported_source_chunks:
        source_type = str(
            getattr(source_chunk, "Type", "") or getattr(source_chunk, "name", "") or ""
        )
        missing_detail = ""
        if source_type in _VISUAL_MESH_COMPONENT_TYPES:
            physics_only_ragdoll = (
                source_type == "CRagdollMeshComponent"
                and _find_prop_by_name(source_chunk, "mesh") is None
            )
            if (
                not physics_only_ragdoll
                and not _resolve_mesh_path(source_chunk, None)
                and not top_level_template_includes
            ):
                missing_detail = "missing mesh resource"
        elif source_type == "CMorphedMeshComponent":
            morph_source = _resolve_repo_path(source_chunk, "morphSource", ".w2mesh")
            morph_target = _resolve_repo_path(source_chunk, "morphTarget", ".w2mesh")
            if not morph_source or not morph_target:
                missing_detail = "missing morph source/target resource"
        elif source_type == "CClothComponent":
            cloth_resource = _resolve_repo_path(
                source_chunk,
                "resource",
                (".redcloth", ".redapex", ".apx"),
            )
            if not cloth_resource and not top_level_template_includes:
                missing_detail = "missing cloth resource"
        if missing_detail:
            incomplete_components.append(
                f"incomplete {source_type} #{getattr(source_chunk, 'ChunkIndex', '?')}: "
                f"{missing_detail}"
            )
    own_destruction_counts = {}
    converted_destruction_counts = {}
    for source_chunk in unsupported_source_chunks:
        source_type = str(
            getattr(source_chunk, "Type", "") or getattr(source_chunk, "name", "") or ""
        )
        if source_type not in {"CDestructionComponent", "CDestructionSystemComponent"}:
            continue
        own_destruction_counts[source_type] = own_destruction_counts.get(source_type, 0) + 1
        plan_component = _destruction_plan_component_from_chunk(source_chunk)
        if plan_component is None:
            continue
        converted_destruction_counts[source_type] = converted_destruction_counts.get(source_type, 0) + 1
        if plan_component not in this_Entity.plan_components:
            this_Entity.plan_components.append(plan_component)
    unsupported_components = [
        component_type
        for component_type in unsupported_components
        if component_type not in own_destruction_counts
        or converted_destruction_counts.get(component_type, 0) < own_destruction_counts[component_type]
    ]
    seen_unsupported = set(unsupported_components)
    for component_type in (
        [t for a in this_Entity.appearances for tm in (getattr(a, "includedTemplates", None) or []) for t in (getattr(tm, "unsupported_components", None) or [])]
        + [t for _d, e in _iter_related_w2_entities() or [] for t in (getattr(e, "unsupported_components", None) or [])]
        + [t for _d, e in _iter_top_level_included_entities() or [] for t in (getattr(e, "unsupported_components", None) or [])]
        + incomplete_components
    ):
        if component_type not in seen_unsupported:
            seen_unsupported.add(component_type)
            unsupported_components.append(component_type)
    this_Entity.unsupported_components = unsupported_components
    return this_Entity

def load_bin_entity(fileName) -> w3_types.Entity:
    with open(fileName,"rb") as f:
        theFile = getCR2W(f)
        f.close()
        with redkit_repo_context(fileName):
            CEntity = create_CEntity(theFile)
        CEntity.version = theFile.HEADER.version
    return CEntity
