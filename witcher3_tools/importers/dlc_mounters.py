import copy
import logging
import os
from pathlib import Path

try:
    import bpy
except Exception:
    bpy = None

from ..CR2W.common_blender import repo_file, redkit_repo_context
from ..CR2W.dc_entity import load_bin_entity
from ..CR2W.witcher_cache.Bundles import LoadBundleManager


log = logging.getLogger(__name__)

_DLC_ENTITY_APPEARANCE_MOUNTER_TYPE = "CR4EntityExternalAppearanceDLCMounter"
_DLC_ENTITY_APPEARANCE_ENTRY_TYPE = "CR4EntityExternalAppearanceDLC"
_DLC_ENTITY_TEMPLATE_PARAM_MOUNTER_TYPE = "CR4EntityTemplateParamDLCMounter"
_DLC_ANIM_TEMPLATE_PARAM_TYPES = {"CAnimAnimsetsParam", "CAnimMimicParam"}
_DEPOT_ROOT_MARKERS = (
    "\\dlc\\",
    "\\gameplay\\",
    "\\game\\",
    "\\characters\\",
    "\\items\\",
    "\\environment\\",
    "\\quests\\",
    "\\levels\\",
    "\\living_world\\",
    "\\animations\\",
    "\\fx\\",
    "\\engine\\",
    "\\globals\\",
    "\\gui\\",
    "\\ui\\",
    "\\templates\\",
)

_DLC_MOUNTER_CACHE = {
    "source_key": None,
    "reddlc_files": (),
    "scan_roots": (),
    "file_signature": (),
    "scan_signature": (),
    "table": {},
}
_DLC_TEMPLATE_PARAM_MOUNTER_CACHE = {
    "source_key": None,
    "reddlc_files": (),
    "scan_roots": (),
    "file_signature": (),
    "scan_signature": (),
    "table": {},
}
_PARSED_REDDLC_MOUNTER_CACHE = {}
_PARSED_REDDLC_TEMPLATE_PARAM_CACHE = {}
_PARSED_REDDLC_MOUNTER_CACHE_MAX = 128


def clear_dlc_mounter_cache():
    _DLC_MOUNTER_CACHE.update({
        "source_key": None,
        "reddlc_files": (),
        "scan_roots": (),
        "file_signature": (),
        "scan_signature": (),
        "table": {},
    })
    _DLC_TEMPLATE_PARAM_MOUNTER_CACHE.update({
        "source_key": None,
        "reddlc_files": (),
        "scan_roots": (),
        "file_signature": (),
        "scan_signature": (),
        "table": {},
    })
    _PARSED_REDDLC_MOUNTER_CACHE.clear()
    _PARSED_REDDLC_TEMPLATE_PARAM_CACHE.clear()


def _default_context():
    return getattr(bpy, "context", None) if bpy is not None else None


def _bpy_abspath(path: str) -> str:
    try:
        if bpy is not None:
            return bpy.path.abspath(path)
    except Exception:
        pass
    return path


def _addon_prefs(context=None):
    try:
        from .. import get_all_addon_prefs

        return get_all_addon_prefs(context or _default_context())
    except Exception:
        return None


def _uncook_path(context=None) -> str:
    try:
        from .. import get_uncook_path

        return get_uncook_path(context or _default_context())
    except Exception:
        return ""


def _dlc_mounters_enabled(context=None) -> bool:
    prefs = _addon_prefs(context)
    return bool(getattr(prefs, "read_dlc_mounters", False)) if prefs is not None else False


def _norm_fs_path(path: str) -> str:
    try:
        return os.path.normcase(os.path.normpath(str(path or "")))
    except Exception:
        return str(path or "").replace("/", "\\").lower()


def _add_unique_path(target, value):
    value = str(value or "").strip()
    if not value:
        return
    value = os.path.normpath(_bpy_abspath(value))
    key = _norm_fs_path(value)
    if not key or key in {_norm_fs_path(path) for path in target}:
        return
    target.append(value)


def _dedupe_root_paths(paths):
    roots = []
    for value in paths or []:
        value = str(value or "").strip()
        if not value:
            continue
        value = os.path.normpath(_bpy_abspath(value))
        if any(_is_under_root_path(value, existing) for existing in roots):
            continue
        roots = [existing for existing in roots if not _is_under_root_path(existing, value)]
        _add_unique_path(roots, value)
    return roots


def _is_under_root_path(path: str, root: str) -> bool:
    if not path or not root:
        return False
    try:
        path_norm = os.path.normcase(os.path.abspath(os.path.normpath(path)))
        root_norm = os.path.normcase(os.path.abspath(os.path.normpath(root)))
        return path_norm == root_norm or path_norm.startswith(root_norm.rstrip("\\/") + os.sep)
    except Exception:
        path_norm = _norm_fs_path(path)
        root_norm = _norm_fs_path(root).rstrip("\\/")
        return bool(path_norm and root_norm and (path_norm == root_norm or path_norm.startswith(root_norm + "\\")))


def _repo_path_from_abs_with_roots(path: str, roots=None) -> str:
    path = str(path or "").strip()
    if not path:
        return ""
    normalized = os.path.normpath(path)
    if not os.path.isabs(normalized):
        return normalized.replace("/", "\\").lstrip("\\")

    for root in roots or []:
        if not root or not _is_under_root_path(normalized, root):
            continue
        try:
            rel = os.path.relpath(normalized, root)
        except Exception:
            continue
        if rel and rel != ".":
            return rel.replace("/", "\\").lstrip("\\")

    marker_match = _find_depot_root_marker(normalized)
    if marker_match is not None:
        idx, _marker = marker_match
        return normalized[idx + 1:].replace("/", "\\").lstrip("\\")
    return normalized.replace("/", "\\").lstrip("\\")


def _dlc_repo_path_key(path: str, roots=None) -> str:
    repo_path = _repo_path_from_abs_with_roots(path, roots)
    return repo_path.replace("/", "\\").strip().lstrip("\\").lower()


def _dlc_appearance_name_from_path(path: str) -> str:
    stem = Path(str(path or "").replace("\\", "/")).stem
    return str(stem or "").strip()


def _find_depot_root_marker(path: str):
    lowered = str(path or "").lower()
    for marker in _DEPOT_ROOT_MARKERS:
        idx = lowered.find(marker)
        if idx >= 0:
            return idx, marker
    return None


def _cr2w_prop_string(prop) -> str:
    if prop is None:
        return ""
    try:
        value = prop.ToString()
    except Exception:
        value = None
    if hasattr(value, "value"):
        value = value.value
    return str(value or "").strip()


def _cr2w_string_array(prop) -> list[str]:
    values = []
    for element in getattr(prop, "elements", None) or []:
        value = ""
        try:
            value = element.ToString()
        except Exception:
            value = getattr(element, "String", None)
        value = str(value or "").strip()
        if value:
            values.append(value.replace("/", "\\"))
    if values:
        return values
    for item in getattr(prop, "More", None) or []:
        value = _cr2w_prop_string(item)
        if value:
            values.append(value.replace("/", "\\"))
    return values


def _cr2w_handle_path(prop) -> str:
    for handle in getattr(prop, "Handles", None) or []:
        value = str(getattr(handle, "DepotPath", "") or "").strip()
        if value:
            return value.replace("/", "\\")
    value = _cr2w_prop_string(prop)
    return value.replace("/", "\\") if value else ""


def _cr2w_handle_paths(prop) -> list[str]:
    values = []
    seen = set()

    def _add(value):
        value = str(value or "").replace("/", "\\").strip()
        if not value:
            return
        key = value.lower()
        if key in seen:
            return
        seen.add(key)
        values.append(value)

    for handle in getattr(prop, "Handles", None) or []:
        _add(getattr(handle, "DepotPath", "") or "")
    for element in getattr(prop, "elements", None) or []:
        _add(getattr(element, "DepotPath", "") or getattr(element, "path", "") or "")
    for item in getattr(prop, "More", None) or []:
        _add(getattr(item, "DepotPath", "") or getattr(item, "path", "") or "")
    if not values:
        for value in _cr2w_string_array(prop):
            _add(value)
    return values


def _iter_cr2w_ptr_chunks(prop, chunks):
    for value in getattr(prop, "value", None) or []:
        try:
            ptr = int(value)
        except Exception:
            continue
        if 1 <= ptr <= len(chunks):
            yield chunks[ptr - 1]
    for handle in getattr(prop, "Handles", None) or []:
        ref = getattr(handle, "Reference", None)
        if isinstance(ref, int) and 0 <= ref < len(chunks):
            yield chunks[ref]
            continue
        ptr = getattr(handle, "val", None)
        if isinstance(ptr, int) and 1 <= ptr <= len(chunks):
            yield chunks[ptr - 1]


def _reddlc_mounter_cache_key(path: str) -> tuple:
    try:
        stat = os.stat(path)
        return (_norm_fs_path(path), int(stat.st_mtime_ns), int(stat.st_size))
    except Exception:
        return (_norm_fs_path(path), 0, -1)


def _cache_parsed_reddlc_mounters(cache_key: tuple, parsed: dict):
    _PARSED_REDDLC_MOUNTER_CACHE[cache_key] = copy.deepcopy(parsed or {})
    while len(_PARSED_REDDLC_MOUNTER_CACHE) > _PARSED_REDDLC_MOUNTER_CACHE_MAX:
        _PARSED_REDDLC_MOUNTER_CACHE.pop(next(iter(_PARSED_REDDLC_MOUNTER_CACHE)))


def _cache_parsed_reddlc_template_params(cache_key: tuple, parsed: dict):
    _PARSED_REDDLC_TEMPLATE_PARAM_CACHE[cache_key] = copy.deepcopy(parsed or {})
    while len(_PARSED_REDDLC_TEMPLATE_PARAM_CACHE) > _PARSED_REDDLC_MOUNTER_CACHE_MAX:
        _PARSED_REDDLC_TEMPLATE_PARAM_CACHE.pop(next(iter(_PARSED_REDDLC_TEMPLATE_PARAM_CACHE)))


def parse_entity_external_appearance_mounters(reddlc_path: str) -> dict[str, list[dict]]:
    cache_key = _reddlc_mounter_cache_key(reddlc_path)
    cached = _PARSED_REDDLC_MOUNTER_CACHE.get(cache_key)
    if cached is not None:
        log.debug("DLC mounter file cache hit: %s", reddlc_path)
        return copy.deepcopy(cached)

    try:
        from ..CR2W.CR2W_file import read_CR2W

        cr2w_file = read_CR2W(reddlc_path)
    except Exception:
        log.warning("Failed to read DLC entity appearance mounter: %s", reddlc_path, exc_info=True)
        return {}

    chunks = list(getattr(getattr(cr2w_file, "CHUNKS", None), "CHUNKS", None) or [])
    table = {}
    for mounter in chunks:
        if getattr(mounter, "Type", None) != _DLC_ENTITY_APPEARANCE_MOUNTER_TYPE:
            continue

        template_paths = _cr2w_string_array(mounter.GetVariableByName("entityTemplatePaths"))
        if not template_paths:
            continue

        entry_prop = mounter.GetVariableByName("entityExternalAppearances")
        entries = []
        for entry_chunk in _iter_cr2w_ptr_chunks(entry_prop, chunks):
            if getattr(entry_chunk, "Type", None) != _DLC_ENTITY_APPEARANCE_ENTRY_TYPE:
                continue
            replacement_name = (
                _cr2w_prop_string(entry_chunk.GetVariableByName("appearanceToRepleace"))
                or _cr2w_prop_string(entry_chunk.GetVariableByName("appearanceToReplace"))
            )
            w3app_path = _cr2w_handle_path(entry_chunk.GetVariableByName("entityExternalAppearance"))
            if not w3app_path or not w3app_path.lower().endswith(".w3app"):
                continue
            entries.append({
                "replacement_name": replacement_name,
                "appearance_name": _dlc_appearance_name_from_path(w3app_path),
                "w3app_path": w3app_path,
                "reddlc_path": reddlc_path,
            })

        if not entries:
            continue

        for template_path in template_paths:
            key = _dlc_repo_path_key(template_path)
            if key:
                table.setdefault(key, []).extend(copy.deepcopy(entries))

    _cache_parsed_reddlc_mounters(cache_key, table)
    return table


def _parse_anim_template_param_chunk(param_chunk, reddlc_path: str) -> dict | None:
    param_type = str(getattr(param_chunk, "Type", "") or getattr(param_chunk, "name", "") or "").strip()
    if param_type not in _DLC_ANIM_TEMPLATE_PARAM_TYPES:
        return None

    animsets = _cr2w_handle_paths(param_chunk.GetVariableByName("animationSets"))
    if not animsets:
        return None

    name = _cr2w_prop_string(param_chunk.GetVariableByName("name"))
    if not name and param_type == "CAnimMimicParam":
        name = "MimicSets"

    return {
        "param_type": param_type,
        "name": name,
        "componentName": _cr2w_prop_string(param_chunk.GetVariableByName("componentName")),
        "animationSets": animsets,
        "reddlc_path": reddlc_path,
    }


def parse_entity_template_param_mounters(reddlc_path: str) -> dict[str, list[dict]]:
    cache_key = _reddlc_mounter_cache_key(reddlc_path)
    cached = _PARSED_REDDLC_TEMPLATE_PARAM_CACHE.get(cache_key)
    if cached is not None:
        log.debug("DLC template-param mounter file cache hit: %s", reddlc_path)
        return copy.deepcopy(cached)

    try:
        from ..CR2W.CR2W_file import read_CR2W

        cr2w_file = read_CR2W(reddlc_path)
    except Exception:
        log.warning("Failed to read DLC entity template-param mounter: %s", reddlc_path, exc_info=True)
        return {}

    chunks = list(getattr(getattr(cr2w_file, "CHUNKS", None), "CHUNKS", None) or [])
    table = {}
    for mounter in chunks:
        if getattr(mounter, "Type", None) != _DLC_ENTITY_TEMPLATE_PARAM_MOUNTER_TYPE:
            continue

        template_paths = _cr2w_string_array(mounter.GetVariableByName("entityTemplatePaths"))
        if not template_paths:
            continue

        entries = []
        for param_chunk in _iter_cr2w_ptr_chunks(mounter.GetVariableByName("entityTemplateParams"), chunks):
            entry = _parse_anim_template_param_chunk(param_chunk, reddlc_path)
            if entry:
                entries.append(entry)
        if not entries:
            continue

        for template_path in template_paths:
            key = _dlc_repo_path_key(template_path)
            if key:
                table.setdefault(key, []).extend(copy.deepcopy(entries))

    _cache_parsed_reddlc_template_params(cache_key, table)
    return table


def _scan_disk_reddlc_files(root: str):
    root = str(root or "").strip()
    if not root:
        return [], []
    root = os.path.normpath(_bpy_abspath(root))
    if os.path.isfile(root):
        return ([root], []) if root.lower().endswith(".reddlc") else ([], [])
    if not os.path.isdir(root):
        return [], []

    candidates = []
    lower_root = root.lower()
    if lower_root.endswith("\\dlc") or lower_root.endswith("/dlc"):
        candidates.append(root)
    for rel in (
        "dlc",
        os.path.join("r4data", "dlc"),
        os.path.join("workspace", "dlc"),
        os.path.join("content", "content0", "dlc"),
    ):
        candidate = os.path.join(root, rel)
        if os.path.isdir(candidate):
            candidates.append(candidate)

    files = []
    scan_roots = []
    seen_files = set()
    seen_roots = set()
    for candidate in candidates:
        root_key = _norm_fs_path(candidate)
        if root_key in seen_roots:
            continue
        seen_roots.add(root_key)
        scan_roots.append(candidate)
        for dirpath, _dirnames, filenames in os.walk(candidate):
            for filename in filenames:
                if not filename.lower().endswith(".reddlc"):
                    continue
                path = os.path.join(dirpath, filename)
                file_key = _norm_fs_path(path)
                if file_key in seen_files:
                    continue
                seen_files.add(file_key)
                files.append(path)
    return files, scan_roots


def _iter_bundle_reddlc_files(context=None):
    try:
        bundle_manager = LoadBundleManager()
    except Exception:
        log.warning("Failed to load bundle manager while scanning DLC mounters", exc_info=True)
        return []

    files = []
    seen = set()
    for key, bundle_items in getattr(bundle_manager, "Items", {}).items():
        depot_path = str(key or "").replace("/", "\\").lstrip("\\")
        depot_lower = depot_path.lower()
        if not depot_lower.startswith("dlc\\") or not depot_lower.endswith(".reddlc"):
            continue
        if not bundle_items:
            continue
        final_item = bundle_items[-1]
        item_name = str(getattr(final_item, "name", depot_path) or depot_path).replace("/", "\\").lstrip("\\")
        local_path = repo_file(item_name)
        if not os.path.isfile(local_path):
            uncook_root = _uncook_path(context)
            if uncook_root:
                export_path = os.path.join(uncook_root, item_name)
                try:
                    final_item.extract_to_file(export_path)
                    local_path = export_path
                except Exception:
                    log.warning("Failed to extract DLC mounter file: %s", item_name, exc_info=True)
                    continue
        if not os.path.isfile(local_path):
            continue
        file_key = _norm_fs_path(local_path)
        if file_key in seen:
            continue
        seen.add(file_key)
        files.append(local_path)
    return files


def _dlc_source_roots_from_prefs(context=None) -> list[str]:
    prefs = _addon_prefs(context)
    roots = []
    if prefs is None:
        return roots

    for attr_name in ("redkit_depot_path", "redkit_uncooked_path", "uncook_path"):
        _add_unique_path(roots, getattr(prefs, attr_name, "") or "")

    for project in getattr(prefs, "redkit_projects", []) or []:
        project_path = str(getattr(project, "path", "") or "").strip()
        if not project_path:
            continue
        _add_unique_path(roots, project_path)
        _add_unique_path(roots, os.path.join(project_path, "workspace"))
        _add_unique_path(roots, os.path.join(project_path, "r4data"))
        _add_unique_path(roots, os.path.join(project_path, "content", "content0"))
    return _dedupe_root_paths(roots)


def _dlc_source_roots_from_entity_path(filename: str) -> list[str]:
    filename = str(filename or "").strip()
    if not filename or not os.path.isabs(filename):
        return []
    norm = os.path.normpath(filename)
    roots = []
    marker_match = _find_depot_root_marker(norm)
    if marker_match is not None:
        idx, _marker = marker_match
        if idx > 0:
            _add_unique_path(roots, norm[:idx])
    return _dedupe_root_paths(roots)


def _dlc_mounter_source_key(context=None, source_roots=None):
    roots = []
    for root in _dlc_source_roots_from_prefs(context):
        _add_unique_path(roots, root)
    for root in source_roots or []:
        _add_unique_path(roots, root)
    roots = _dedupe_root_paths(roots)
    return tuple(_norm_fs_path(root) for root in roots if root)


def _path_stat_tuple(path: str):
    try:
        stat = os.stat(path)
        return (_norm_fs_path(path), int(stat.st_mtime_ns), int(stat.st_size))
    except Exception:
        return (_norm_fs_path(path), 0, -1)


def _signature_for_files(paths) -> tuple:
    return tuple(sorted(_path_stat_tuple(path) for path in paths or []))


def _signature_for_scan_roots(paths) -> tuple:
    return tuple(sorted(_path_stat_tuple(path) for path in paths or []))


def _discover_dlc_mounter_files(context=None, source_roots=None):
    roots = []
    for root in _dlc_source_roots_from_prefs(context):
        _add_unique_path(roots, root)
    for root in source_roots or []:
        _add_unique_path(roots, root)
    roots = _dedupe_root_paths(roots)

    files = []
    scan_roots = []
    seen_files = set()
    seen_roots = set()

    def _add_file(path):
        key = _norm_fs_path(path)
        if not key or key in seen_files:
            return
        seen_files.add(key)
        files.append(path)

    for root in roots:
        found_files, found_scan_roots = _scan_disk_reddlc_files(root)
        for path in found_files:
            _add_file(path)
        for scan_root in found_scan_roots:
            key = _norm_fs_path(scan_root)
            if key and key not in seen_roots:
                seen_roots.add(key)
                scan_roots.append(scan_root)

    for path in _iter_bundle_reddlc_files(context):
        _add_file(path)

    return files, scan_roots


def _get_dlc_mounter_table(context=None, source_roots=None) -> dict[str, list[dict]]:
    if not _dlc_mounters_enabled(context):
        return {}

    source_key = _dlc_mounter_source_key(context, source_roots)
    cached_files = _DLC_MOUNTER_CACHE.get("reddlc_files") or ()
    cached_scan_roots = _DLC_MOUNTER_CACHE.get("scan_roots") or ()
    if _DLC_MOUNTER_CACHE.get("source_key") == source_key and cached_files is not None:
        file_signature = _signature_for_files(cached_files)
        scan_signature = _signature_for_scan_roots(cached_scan_roots)
        if (
            file_signature == _DLC_MOUNTER_CACHE.get("file_signature")
            and scan_signature == _DLC_MOUNTER_CACHE.get("scan_signature")
        ):
            return _DLC_MOUNTER_CACHE.get("table") or {}

    reddlc_files, scan_roots = _discover_dlc_mounter_files(context, source_roots)
    table = {}
    for reddlc_path in reddlc_files:
        parsed = parse_entity_external_appearance_mounters(reddlc_path)
        for key, entries in parsed.items():
            if not key or not entries:
                continue
            table.setdefault(key, []).extend(entries)

    for key, entries in list(table.items()):
        deduped = []
        seen_entries = set()
        for entry in entries:
            signature = (
                str(entry.get("appearance_name", "")).lower(),
                str(entry.get("w3app_path", "")).replace("/", "\\").lower(),
            )
            if signature in seen_entries:
                continue
            seen_entries.add(signature)
            deduped.append(entry)
        table[key] = deduped

    _DLC_MOUNTER_CACHE.update({
        "source_key": source_key,
        "reddlc_files": tuple(reddlc_files),
        "scan_roots": tuple(scan_roots),
        "file_signature": _signature_for_files(reddlc_files),
        "scan_signature": _signature_for_scan_roots(scan_roots),
        "table": table,
    })
    log.info("DLC mounter scan: %d .reddlc files, %d entity template mappings", len(reddlc_files), len(table))
    return table


def _get_dlc_template_param_mounter_table(context=None, source_roots=None) -> dict[str, list[dict]]:
    if not _dlc_mounters_enabled(context):
        return {}

    source_key = _dlc_mounter_source_key(context, source_roots)
    cached_files = _DLC_TEMPLATE_PARAM_MOUNTER_CACHE.get("reddlc_files") or ()
    cached_scan_roots = _DLC_TEMPLATE_PARAM_MOUNTER_CACHE.get("scan_roots") or ()
    if _DLC_TEMPLATE_PARAM_MOUNTER_CACHE.get("source_key") == source_key and cached_files is not None:
        file_signature = _signature_for_files(cached_files)
        scan_signature = _signature_for_scan_roots(cached_scan_roots)
        if (
            file_signature == _DLC_TEMPLATE_PARAM_MOUNTER_CACHE.get("file_signature")
            and scan_signature == _DLC_TEMPLATE_PARAM_MOUNTER_CACHE.get("scan_signature")
        ):
            return _DLC_TEMPLATE_PARAM_MOUNTER_CACHE.get("table") or {}

    reddlc_files, scan_roots = _discover_dlc_mounter_files(context, source_roots)
    table = {}
    for reddlc_path in reddlc_files:
        parsed = parse_entity_template_param_mounters(reddlc_path)
        for key, entries in parsed.items():
            if not key or not entries:
                continue
            table.setdefault(key, []).extend(entries)

    for key, entries in list(table.items()):
        deduped = []
        seen_entries = set()
        for entry in entries:
            signature = (
                str(entry.get("param_type", "")).lower(),
                str(entry.get("name", "")).lower(),
                str(entry.get("componentName", "")).lower(),
                tuple(str(path or "").replace("/", "\\").lower() for path in entry.get("animationSets", []) or []),
            )
            if signature in seen_entries:
                continue
            seen_entries.add(signature)
            deduped.append(entry)
        table[key] = deduped

    _DLC_TEMPLATE_PARAM_MOUNTER_CACHE.update({
        "source_key": source_key,
        "reddlc_files": tuple(reddlc_files),
        "scan_roots": tuple(scan_roots),
        "file_signature": _signature_for_files(reddlc_files),
        "scan_signature": _signature_for_scan_roots(scan_roots),
        "table": table,
    })
    log.info("DLC template-param mounter scan: %d .reddlc files, %d entity template mappings", len(reddlc_files), len(table))
    return table


def _get_dlc_mounter_entries_for_entity(filename: str, context=None) -> list[dict]:
    source_roots = _dlc_source_roots_from_entity_path(filename)
    root_candidates = []
    for root in _dlc_source_roots_from_prefs(context):
        _add_unique_path(root_candidates, root)
    for root in source_roots:
        _add_unique_path(root_candidates, root)
    key = _dlc_repo_path_key(filename, root_candidates)
    if not key:
        return []
    table = _get_dlc_mounter_table(context, source_roots)
    return list(table.get(key, []) or [])


def _get_dlc_template_param_entries_for_entity(filename: str, context=None) -> list[dict]:
    source_roots = _dlc_source_roots_from_entity_path(filename)
    root_candidates = []
    for root in _dlc_source_roots_from_prefs(context):
        _add_unique_path(root_candidates, root)
    for root in source_roots:
        _add_unique_path(root_candidates, root)
    key = _dlc_repo_path_key(filename, root_candidates)
    if not key:
        return []
    table = _get_dlc_template_param_mounter_table(context, source_roots)
    return list(table.get(key, []) or [])


def get_dlc_external_appearance_names_for_entity(filename: str, context=None) -> list[str]:
    names = []
    seen = set()
    for entry in _get_dlc_mounter_entries_for_entity(filename, context):
        name = str(entry.get("appearance_name", "") or "").strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names


def _derive_repo_root_from_dlc_path(path: str) -> str:
    path = str(path or "").strip()
    if not path or not os.path.isabs(path):
        return ""
    norm = os.path.normpath(path)
    lowered = norm.lower()
    marker = "\\dlc\\"
    idx = lowered.find(marker)
    if idx > 0:
        return norm[:idx]
    return ""


def _resolve_dlc_w3app_path(entry: dict):
    w3app_path = str(entry.get("w3app_path", "") or "").replace("/", "\\").strip()
    if not w3app_path:
        return "", []
    roots = []
    reddlc_root = _derive_repo_root_from_dlc_path(entry.get("reddlc_path", ""))
    if reddlc_root:
        _add_unique_path(roots, reddlc_root)
        candidate = os.path.join(reddlc_root, w3app_path)
        if os.path.isfile(candidate):
            return candidate, roots
    try:
        resolved = repo_file(w3app_path)
    except Exception:
        resolved = ""
    if resolved and os.path.isfile(resolved):
        resolved_root = _derive_repo_root_from_dlc_path(resolved)
        if resolved_root:
            _add_unique_path(roots, resolved_root)
        return resolved, roots
    return resolved or w3app_path, roots


def append_dlc_external_appearances(entity, filename: str, context=None) -> int:
    if entity is None or not _dlc_mounters_enabled(context):
        return 0
    _, ext = os.path.splitext(str(filename or ""))
    if ext.lower() == ".w3app":
        return 0

    entries = _get_dlc_mounter_entries_for_entity(filename, context)
    if not entries:
        return 0

    appearances = getattr(entity, "appearances", None)
    if appearances is None:
        appearances = []
        setattr(entity, "appearances", appearances)

    existing_names = {
        str(getattr(app, "name", "") or "").strip().lower()
        for app in appearances
        if str(getattr(app, "name", "") or "").strip()
    }
    added = 0
    for entry in entries:
        appearance_name = str(entry.get("appearance_name", "") or "").strip()
        if not appearance_name or appearance_name.lower() in existing_names:
            continue

        w3app_abs, repo_roots = _resolve_dlc_w3app_path(entry)
        if not w3app_abs or not os.path.isfile(w3app_abs):
            log.warning("DLC appearance file not found: %s", entry.get("w3app_path", ""))
            continue

        try:
            with redkit_repo_context(w3app_abs, roots=repo_roots):
                dlc_entity = load_bin_entity(w3app_abs)
        except Exception:
            log.warning("Failed to load DLC appearance file: %s", w3app_abs, exc_info=True)
            continue

        dlc_apps = list(getattr(dlc_entity, "appearances", None) or [])
        if not dlc_apps:
            log.warning("DLC appearance file has no appearances: %s", w3app_abs)
            continue

        dlc_app = copy.deepcopy(dlc_apps[0])
        dlc_app.name = appearance_name
        appearances.append(dlc_app)
        existing_names.add(appearance_name.lower())
        added += 1

    if added:
        log.info("Added %d DLC mounter appearances to %s", added, filename)
    return added


def _anim_param_signature(param, fallback_type="") -> tuple:
    if isinstance(param, dict):
        param_type = str(param.get("param_type", "") or param.get("type", "") or fallback_type or "")
        name = str(param.get("name", "") or "")
        component_name = str(param.get("componentName", "") or "")
        animsets = param.get("animationSets", []) or []
    else:
        param_type = str(getattr(param, "param_type", "") or getattr(param, "type", "") or fallback_type or "")
        name = str(getattr(param, "name", "") or "")
        component_name = str(getattr(param, "componentName", "") or "")
        animsets = getattr(param, "animationSets", []) or []
    return (
        param_type.lower(),
        name.lower(),
        component_name.lower(),
        tuple(str(path or "").replace("/", "\\").lower() for path in animsets),
    )


def _iter_template_param_source_paths(entity, filename: str):
    seen = set()

    def _yield(path):
        path = str(path or "").replace("/", "\\").strip()
        if not path:
            return
        key = path.lower()
        if key in seen:
            return
        seen.add(key)
        yield path

    yield from _yield(filename)
    for include_path in getattr(entity, "included_template_paths", None) or []:
        yield from _yield(include_path)


def append_dlc_entity_template_params(entity, filename: str, context=None) -> int:
    if entity is None or not _dlc_mounters_enabled(context):
        return 0

    added = 0
    for source_path in _iter_template_param_source_paths(entity, filename):
        entries = _get_dlc_template_param_entries_for_entity(source_path, context)
        if not entries:
            continue

        for entry in entries:
            param_type = str(entry.get("param_type", "") or "").strip()
            if param_type == "CAnimMimicParam":
                attr_name = "CAnimMimicParam"
            elif param_type == "CAnimAnimsetsParam":
                attr_name = "CAnimAnimsetsParam"
            else:
                continue

            current_params = getattr(entity, attr_name, None)
            if current_params is None:
                current_params = []
                setattr(entity, attr_name, current_params)

            new_param = {
                "param_type": param_type,
                "name": str(entry.get("name", "") or ("MimicSets" if attr_name == "CAnimMimicParam" else "AnimSets")),
                "componentName": str(entry.get("componentName", "") or ""),
                "animationSets": [str(path or "").replace("/", "\\") for path in entry.get("animationSets", []) or []],
            }
            if not new_param["animationSets"]:
                continue

            signature = _anim_param_signature(new_param, param_type)
            existing = {_anim_param_signature(param, param_type) for param in current_params}
            if signature in existing:
                continue

            current_params.append(new_param)
            added += 1

    if added:
        log.info("Added %d DLC mounter anim template param(s) to %s", added, filename)
    return added
