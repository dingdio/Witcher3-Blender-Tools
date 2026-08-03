import copy
import logging
import os

try:
    import bpy
except Exception:
    bpy = None

from ..CR2W.common_blender import repo_file, redkit_repo_context, win_safe_path
from ..CR2W.dc_entity import load_bin_entity
from ..CR2W import w3_types
from ..CR2W.witcher_cache.DLC import LoadDLCManager


log = logging.getLogger(__name__)

_DLC_MOUNTER_INDEX_CACHE = {
    "source_key": None,
    "appearance_table": {},
    "template_param_table": {},
}
_DLC_W3APP_ENTITY_CACHE = {}
_DLC_W3APP_ENTITY_CACHE_MAX = 64


def clear_dlc_mounter_cache(reset_manager=False):
    _invalidate_dlc_mounter_index_cache()
    _DLC_W3APP_ENTITY_CACHE.clear()
    if reset_manager:
        try:
            from ..CR2W.witcher_cache.DLC import DLCManager

            DLCManager.InstanceManager = None
        except Exception:
            pass


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


def _dlc_mounters_enabled(context=None) -> bool:
    prefs = _addon_prefs(context)
    return bool(getattr(prefs, "read_dlc_mounters", False)) if prefs is not None else False


def _dlc_replace_appearances_enabled(context=None) -> bool:
    prefs = _addon_prefs(context)
    return bool(getattr(prefs, "do_replace_appearances", False)) if prefs is not None else False


def _invalidate_dlc_mounter_index_cache():
    _DLC_MOUNTER_INDEX_CACHE.update({
        "source_key": None,
        "appearance_table": {},
        "template_param_table": {},
    })


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

    matching_roots = []
    for root in roots or []:
        if not root or not _is_under_root_path(normalized, root):
            continue
        matching_roots.append(os.path.normpath(root))
    matching_roots.sort(key=len, reverse=True)
    for root in matching_roots:
        try:
            rel = os.path.relpath(normalized, root)
        except Exception:
            continue
        if rel and rel != ".":
            return rel.replace("/", "\\").lstrip("\\")

    return normalized.replace("/", "\\").lstrip("\\")


def _dlc_repo_path_key(path: str, roots=None) -> str:
    repo_path = _repo_path_from_abs_with_roots(path, roots)
    return repo_path.replace("/", "\\").strip().lstrip("\\").lower()


def _active_redkit_project_path(context=None) -> str:
    prefs = _addon_prefs(context)
    if prefs is None:
        return ""
    projects = list(getattr(prefs, "redkit_projects", []) or [])
    if not projects:
        return ""
    try:
        index = int(getattr(prefs, "redkit_projects_index", 0) or 0)
    except Exception:
        index = 0
    if index < 0 or index >= len(projects):
        return ""
    return str(getattr(projects[index], "path", "") or "").strip()


def _source_root_path(path: str) -> str:
    path = str(path or "").strip()
    if not path:
        return ""
    path = str(_bpy_abspath(path) or "").strip()
    return os.path.normpath(path) if path else ""


def _dlc_manager_source_roots(context=None) -> list[dict]:
    prefs = _addon_prefs(context)
    if prefs is None:
        return []
    uncook_root = _source_root_path(getattr(prefs, "uncook_path", "") or "")
    game_root = _source_root_path(getattr(prefs, "witcher_game_path", "") or "")
    return [
        {
            "source_id": "bundles_uncook",
            "source_label": "Bundles uncook",
            "source_type": "disk",
            "source_kind": "game",
            "root_path": uncook_root,
            "rel_paths": ("dlc",),
        },
        {
            "source_id": "assets_mods",
            "source_label": "Assets Mods",
            "source_type": "assets_mods",
            "source_kind": "game",
            "root_path": game_root,
            "repo_roots": (uncook_root,),
            "rel_paths": (),
        },
        {
            "source_id": "redkit_depot",
            "source_label": "REDkit depot",
            "source_type": "disk",
            "source_kind": "redkit",
            "root_path": _source_root_path(getattr(prefs, "redkit_depot_path", "") or ""),
            "rel_paths": ("dlc",),
        },
        {
            "source_id": "active_redkit_project",
            "source_label": "Active REDkit project",
            "source_type": "disk",
            "source_kind": "redkit",
            "root_path": _source_root_path(_active_redkit_project_path(context)),
            "rel_paths": (
                "dlc",
                os.path.join("r4data", "dlc"),
                os.path.join("workspace", "dlc"),
                os.path.join("content", "content0", "dlc"),
            ),
        },
    ]


def _dlc_enabled_map_from_prefs(context=None) -> dict:
    prefs = _addon_prefs(context)
    if prefs is None:
        return {}
    enabled = {}
    for item in getattr(prefs, "dlc_mounter_sources", []) or []:
        value = bool(getattr(item, "enabled", True))
        key = str(getattr(item, "key", "") or "")
        if key:
            enabled[key] = value
        path_key = _norm_fs_path(getattr(item, "reddlc_path", ""))
        if path_key:
            enabled[path_key] = value
    return enabled


def _load_dlc_manager(context=None, reset_cache=False):
    return LoadDLCManager(
        source_roots=_dlc_manager_source_roots(context),
        enabled_by_key=_dlc_enabled_map_from_prefs(context),
        reset_cache=reset_cache,
    )


def build_dlc_mounter_cache_signature(context=None):
    from ..CR2W.witcher_cache.DLC import DLCManager

    return DLCManager.BuildSourceSignature(_dlc_manager_source_roots(context))


def build_dlc_mounter_plan_signature(context=None):
    w3app_dependencies = {}
    try:
        appearance_table = _get_dlc_mounter_index(context).get("appearance_table") or {}
        for entries in appearance_table.values():
            for entry in entries or []:
                logical_path = str(entry.get("w3app_path", "") or "").strip()
                resolved_path, _repo_roots = _resolve_dlc_w3app_path(entry)
                normalized_path = os.path.normcase(os.path.normpath(str(resolved_path or "")))
                try:
                    stat = os.stat(resolved_path)
                    identity = (int(stat.st_mtime_ns), int(stat.st_size))
                except OSError:
                    identity = (0, -1)
                key = (logical_path.lower(), normalized_path)
                w3app_dependencies[key] = (
                    logical_path,
                    normalized_path,
                    identity,
                )
    except Exception:
        log.debug("Failed to build DLC appearance dependency signature.", exc_info=True)
    return {
        "enabled": _dlc_mounters_enabled(context),
        "replace_appearances": _dlc_replace_appearances_enabled(context),
        "source": build_dlc_mounter_cache_signature(context),
        "enabled_map": sorted(_dlc_enabled_map_from_prefs(context).items()),
        "w3app_dependencies": [w3app_dependencies[key] for key in sorted(w3app_dependencies)],
    }


def refresh_dlc_mounter_cache(context=None, sync_sources=False):
    if sync_sources:
        sync_dlc_mounter_sources(context)
        return _load_dlc_manager(context)
    manager = _load_dlc_manager(context, reset_cache=True)
    _invalidate_dlc_mounter_index_cache()
    return manager


def discover_dlc_mounter_sources(context=None, reset_cache=False) -> list[dict]:
    manager = _load_dlc_manager(context, reset_cache=reset_cache)
    return [definition.to_ui_dict() for definition in getattr(manager, "definitions", []) or []]


def _localize_dlc_source_strings(sources: list[dict]) -> None:
    try:
        from ..CR2W.witcher_cache.W3Strings.W3StringManager import W3StringManager

        string_manager = W3StringManager.Get()
        resolve = getattr(string_manager, "GetStringByKey", None)
    except Exception:
        log.debug("DLC localization unavailable.", exc_info=True)
        resolve = None

    def _resolve_key(key: str):
        if resolve is None:
            return None
        try:
            return resolve(key)
        except Exception:
            log.debug("DLC localization key lookup failed: %s", key, exc_info=True)
            return None

    for source in sources or []:
        name_key = str(source.get("dlc_name_key") or source.get("dlc_name") or "").strip()
        description_key = str(source.get("dlc_description_key") or source.get("dlc_description") or "").strip()
        if name_key:
            source["dlc_name"] = _resolve_key(name_key) or name_key
        if description_key:
            source["dlc_description"] = _resolve_key(description_key) or description_key


def sync_dlc_mounter_sources(context=None) -> int:
    prefs = _addon_prefs(context)
    if prefs is None or not hasattr(prefs, "dlc_mounter_sources"):
        return 0

    enabled_by_key = {
        str(getattr(item, "key", "") or ""): bool(getattr(item, "enabled", True))
        for item in getattr(prefs, "dlc_mounter_sources", []) or []
    }
    enabled_by_path = {
        _norm_fs_path(getattr(item, "reddlc_path", "")): bool(getattr(item, "enabled", True))
        for item in getattr(prefs, "dlc_mounter_sources", []) or []
    }
    discovered = discover_dlc_mounter_sources(context, reset_cache=True)
    _localize_dlc_source_strings(discovered)
    collection = prefs.dlc_mounter_sources
    collection.clear()
    for source in discovered:
        item = collection.add()
        item.key = source.get("key", "")
        item.source_id = source.get("source_id", "")
        item.source_label = source.get("source_label", "")
        item.source_kind = source.get("source_kind", "")
        item.is_vanilla = bool(source.get("is_vanilla", False))
        item.dlc_id = source.get("dlc_id", "")
        item.dlc_name = source.get("dlc_name", "")
        item.dlc_description = source.get("dlc_description", "")
        item.dlc_name_key = source.get("dlc_name_key", "")
        item.dlc_description_key = source.get("dlc_description_key", "")
        item.dlc_folder_name = source.get("dlc_folder_name", "")
        item.mounter_types = source.get("mounter_types", "")
        item.root_path = source.get("root_path", "")
        item.dlc_dir = source.get("dlc_dir", "")
        item.reddlc_path = source.get("reddlc_path", "")
        path_key = _norm_fs_path(item.reddlc_path)
        item.enabled = enabled_by_key.get(item.key, enabled_by_path.get(path_key, True))

    _invalidate_dlc_mounter_index_cache()
    return len(discovered)


def _dlc_source_roots_from_prefs(context=None) -> list[str]:
    prefs = _addon_prefs(context)
    roots = []
    if prefs is None:
        return roots

    for attr_name in ("redkit_depot_path", "uncook_path"):
        _add_unique_path(roots, getattr(prefs, attr_name, "") or "")

    project_path = _active_redkit_project_path(context)
    if project_path:
        _add_unique_path(roots, project_path)
        _add_unique_path(roots, os.path.join(project_path, "workspace"))
        _add_unique_path(roots, os.path.join(project_path, "r4data"))
        _add_unique_path(roots, os.path.join(project_path, "content", "content0"))
    return _dedupe_root_paths(roots)


def _dlc_mounter_source_key(manager):
    return tuple(
        (
            str(getattr(definition, "key", "") or ""),
            _norm_fs_path(getattr(definition, "reddlc_path", "")),
            bool(getattr(definition, "enabled", True)),
        )
        for definition in getattr(manager, "mounted_content", []) or []
    )


def _get_dlc_mounter_index(context=None) -> dict:
    if not _dlc_mounters_enabled(context):
        return {}

    manager = _load_dlc_manager(context)
    source_key = _dlc_mounter_source_key(manager)
    if _DLC_MOUNTER_INDEX_CACHE.get("source_key") == source_key:
        return _DLC_MOUNTER_INDEX_CACHE

    appearance_table = manager.GetAppearanceMounterTable()
    template_param_table = manager.GetTemplateParamMounterTable()

    _DLC_MOUNTER_INDEX_CACHE.update({
        "source_key": source_key,
        "appearance_table": appearance_table,
        "template_param_table": template_param_table,
    })
    log.info(
        "DLC mounter index: %d enabled .reddlc files, %d appearance mappings, %d template-param mappings",
        len(getattr(manager, "mounted_content", []) or []),
        len(appearance_table),
        len(template_param_table),
    )
    return _DLC_MOUNTER_INDEX_CACHE


def _get_dlc_mounter_table(context=None) -> dict[str, list[dict]]:
    return _get_dlc_mounter_index(context).get("appearance_table") or {}


def _get_dlc_template_param_mounter_table(context=None) -> dict[str, list[dict]]:
    return _get_dlc_mounter_index(context).get("template_param_table") or {}


def _get_dlc_mounter_entries_for_entity(filename: str, context=None) -> list[dict]:
    root_candidates = []
    for root in _dlc_source_roots_from_prefs(context):
        _add_unique_path(root_candidates, root)
    key = _dlc_repo_path_key(filename, root_candidates)
    if not key:
        return []
    table = _get_dlc_mounter_table(context)
    return list(table.get(key, []) or [])


def _get_dlc_template_param_entries_for_entity(filename: str, context=None) -> list[dict]:
    root_candidates = []
    for root in _dlc_source_roots_from_prefs(context):
        _add_unique_path(root_candidates, root)
    key = _dlc_repo_path_key(filename, root_candidates)
    if not key:
        return []
    table = _get_dlc_template_param_mounter_table(context)
    return list(table.get(key, []) or [])


def get_dlc_external_appearance_names_for_entity(filename: str, context=None) -> list[str]:
    names = []
    seen = set()
    replace_appearances = _dlc_replace_appearances_enabled(context)
    for entry in _get_dlc_mounter_entries_for_entity(filename, context):
        if replace_appearances:
            name = str(entry.get("replacement_name", "") or entry.get("appearance_name", "") or "").strip()
        else:
            name = str(entry.get("appearance_name", "") or "").strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names


def _resolve_dlc_w3app_path(entry: dict):
    w3app_path = str(entry.get("w3app_path", "") or "").replace("/", "\\").strip()
    if not w3app_path:
        return "", []
    roots = []
    repo_roots = list(entry.get("repo_roots", []) or [])
    repo_root = str(entry.get("repo_root", "") or "").strip()
    if repo_root and repo_root not in repo_roots:
        repo_roots.append(repo_root)
    for repo_root in repo_roots:
        repo_root = str(repo_root or "").strip()
        if not repo_root:
            continue
        _add_unique_path(roots, repo_root)
        candidate = os.path.join(repo_root, w3app_path)
        if os.path.isfile(win_safe_path(candidate)):
            return candidate, roots
    try:
        resolved = repo_file(w3app_path)
    except Exception:
        resolved = ""
    if resolved and os.path.isfile(win_safe_path(resolved)):
        return resolved, roots
    return resolved or w3app_path, roots


def _dlc_w3app_entity_cache_key(path: str, repo_roots=None) -> tuple:
    try:
        stat = os.stat(path)
        file_key = (_norm_fs_path(path), int(stat.st_mtime_ns), int(stat.st_size))
    except Exception:
        file_key = (_norm_fs_path(path), 0, -1)
    roots_key = tuple(_norm_fs_path(root) for root in (repo_roots or []) if root)
    return file_key + (roots_key,)


_DLC_W3APP_ENTITY_FAILED = {}


def _load_dlc_w3app_entity(w3app_abs: str, repo_roots=None):
    # Cached entities are shared; callers deepcopy before customization.
    cache_key = _dlc_w3app_entity_cache_key(w3app_abs, repo_roots)
    cached = _DLC_W3APP_ENTITY_CACHE.get(cache_key)
    if cached is not None:
        return cached
    # Cache failures to avoid repeated parsing and warnings.
    if cache_key in _DLC_W3APP_ENTITY_FAILED:
        return None

    try:
        with redkit_repo_context(w3app_abs, roots=repo_roots):
            dlc_entity = load_bin_entity(w3app_abs)
    except Exception:
        _DLC_W3APP_ENTITY_FAILED[cache_key] = True
        while len(_DLC_W3APP_ENTITY_FAILED) > _DLC_W3APP_ENTITY_CACHE_MAX:
            _DLC_W3APP_ENTITY_FAILED.pop(next(iter(_DLC_W3APP_ENTITY_FAILED)))
        raise

    _DLC_W3APP_ENTITY_CACHE[cache_key] = dlc_entity
    while len(_DLC_W3APP_ENTITY_CACHE) > _DLC_W3APP_ENTITY_CACHE_MAX:
        _DLC_W3APP_ENTITY_CACHE.pop(next(iter(_DLC_W3APP_ENTITY_CACHE)))
    return dlc_entity


def _load_dlc_appearance_from_entry(entry: dict):
    appearance_name = str(entry.get("appearance_name", "") or "").strip()
    w3app_abs, repo_roots = _resolve_dlc_w3app_path(entry)
    if not w3app_abs or not os.path.isfile(win_safe_path(w3app_abs)):
        log.warning("DLC appearance file not found: %s", entry.get("w3app_path", ""))
        return None

    try:
        dlc_entity = _load_dlc_w3app_entity(w3app_abs, repo_roots)
    except Exception as exc:
        log.warning("Failed to load DLC appearance file %s: %s", w3app_abs, exc)
        log.debug("DLC appearance load traceback for %s", w3app_abs, exc_info=True)
        return None
    if dlc_entity is None:
        return None

    dlc_apps = list(getattr(dlc_entity, "appearances", None) or [])
    if not dlc_apps:
        log.warning("DLC appearance file has no appearances: %s", w3app_abs)
        return None

    dlc_app = copy.deepcopy(dlc_apps[0])
    dlc_app.name = appearance_name
    dlc_app._dlc_mounter_entry = copy.deepcopy(entry)
    dlc_app._dlc_mounter_lazy = False
    dependency_paths = [
        w3app_abs,
        str(entry.get("reddlc_path", "") or "").strip(),
        *(getattr(dlc_entity, "template_dependency_paths", None) or []),
    ]
    dlc_app.template_dependency_paths = list(dict.fromkeys(
        path for path in dependency_paths if path
    ))
    return dlc_app


def _make_lazy_dlc_appearance(entry: dict, appearance_name=None):
    app = w3_types.CEntityAppearance()
    app.name = str(appearance_name or entry.get("appearance_name", "") or "").strip()
    app.includedTemplates = []
    app._dlc_mounter_entry = copy.deepcopy(entry)
    app._dlc_mounter_lazy = True
    return app


def realize_dlc_external_appearance(entity, appearance, context=None) -> bool:
    if appearance is None:
        return False
    entry = getattr(appearance, "_dlc_mounter_entry", None)
    if not entry:
        return True
    if not getattr(appearance, "_dlc_mounter_lazy", False):
        return True

    appearance_name = str(getattr(appearance, "name", "") or entry.get("appearance_name", "") or "").strip()
    loaded_app = _load_dlc_appearance_from_entry(entry)
    if loaded_app is None:
        return False

    appearance.__dict__.clear()
    appearance.__dict__.update(copy.deepcopy(vars(loaded_app)))
    appearance.name = appearance_name or str(getattr(loaded_app, "name", "") or "")
    appearance._dlc_mounter_entry = copy.deepcopy(entry)
    appearance._dlc_mounter_lazy = False
    return True


def append_dlc_external_appearances(entity, filename: str, context=None, load_appearances=False) -> int:
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

    existing_names = {}
    for index, app in enumerate(appearances):
        app_name = str(getattr(app, "name", "") or "").strip()
        if app_name:
            existing_names[app_name.lower()] = index

    replace_appearances = _dlc_replace_appearances_enabled(context)
    added = 0
    replaced = 0
    for entry in entries:
        appearance_name = str(entry.get("appearance_name", "") or "").strip()
        replacement_name = str(entry.get("replacement_name", "") or "").strip()
        target_name = replacement_name if replace_appearances and replacement_name else appearance_name
        if not target_name:
            continue

        target_key = target_name.lower()
        replace_index = existing_names.get(target_key) if replace_appearances and replacement_name else None
        if replace_appearances and replacement_name and replace_index is None:
            log.debug("DLC mounter replacement target not found: %s", replacement_name)
            continue
        if not replace_appearances and target_key in existing_names:
            continue

        if load_appearances:
            dlc_app = _load_dlc_appearance_from_entry(entry)
        else:
            dlc_app = _make_lazy_dlc_appearance(entry, target_name)
        if dlc_app is None:
            continue
        dlc_app.name = target_name

        if replace_index is not None:
            appearances[replace_index] = dlc_app
            replaced += 1
        else:
            appearances.append(dlc_app)
            added += 1
        existing_names[target_key] = replace_index if replace_index is not None else len(appearances) - 1

    if added or replaced:
        log.info("Applied DLC mounter appearance link(s) to %s: added=%d replaced=%d", filename, added, replaced)
    return added + replaced


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
            dependency_paths = getattr(entity, "template_dependency_paths", None)
            if dependency_paths is None:
                dependency_paths = []
                entity.template_dependency_paths = dependency_paths
            reddlc_path = str(entry.get("reddlc_path", "") or "").strip()
            if reddlc_path and reddlc_path not in dependency_paths:
                dependency_paths.append(reddlc_path)
            added += 1

    if added:
        log.info("Added %d DLC mounter anim template param(s) to %s", added, filename)
    return added
