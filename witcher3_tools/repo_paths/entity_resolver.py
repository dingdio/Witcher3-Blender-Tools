"""Repo-relative entity/template path resolution helpers.

The game keeps item definitions and template depot paths as separate concerns.
This module mirrors that shape for the addon: callers provide a short template
identifier or repo path, and the resolver returns a repo-relative path that can
then be passed to the package materialization helpers.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Iterable

from .source_game import (
    configured_w2_repo_roots,
    is_under_root,
    normalize_roots,
    normalize_source_game,
)


DEFAULT_ENTITY_EXTENSIONS = (".w2ent",)
EQUIPMENT_ENTITY_EXTENSIONS = (".w2ent", ".w2mesh")

_ROOT_ENTITY_PATH_INDEX = {}
_BUNDLE_ENTITY_PATH_INDEX = {}


@dataclass(frozen=True)
class EntityPathResolveResult:
    repo_path: str
    source: str
    matched_key: str = ""
    source_root: str = ""


@dataclass(frozen=True)
class _IndexedEntityPath:
    repo_path: str
    score: tuple
    source_root: str = ""


def clear_entity_path_resolver_caches():
    _ROOT_ENTITY_PATH_INDEX.clear()
    _BUNDLE_ENTITY_PATH_INDEX.clear()


def _normalize_extensions(extensions: Iterable[str] | None):
    normalized = []
    seen = set()
    for ext in extensions or DEFAULT_ENTITY_EXTENSIONS:
        ext = str(ext or "").strip().lower()
        if not ext:
            continue
        if not ext.startswith("."):
            ext = "." + ext
        if ext in seen:
            continue
        seen.add(ext)
        normalized.append(ext)
    return tuple(normalized or DEFAULT_ENTITY_EXTENSIONS)


def _norm_root_key(path_value):
    try:
        return os.path.normcase(os.path.normpath(str(path_value or "")))
    except Exception:
        return str(path_value or "").lower()


def _repo_path(path_value):
    return str(path_value or "").replace("/", "\\").lstrip("\\")


def _path_has_separator(path_value):
    return "\\" in str(path_value or "").replace("/", "\\")


def _basename(path_value):
    return _repo_path(path_value).rsplit("\\", 1)[-1]


def _split_known_extension(path_value, extensions):
    base, ext = os.path.splitext(str(path_value or ""))
    if ext.lower() in extensions:
        return base, ext.lower()
    return str(path_value or ""), ""


def _candidate_repo_paths(identifier, extensions):
    rel_name = _repo_path(identifier)
    if not rel_name:
        return []

    candidates = []
    base, ext = os.path.splitext(rel_name)
    if ext.lower() in extensions:
        candidates.append(rel_name)
    else:
        for candidate_ext in extensions:
            candidates.append(rel_name + candidate_ext)

    if not _path_has_separator(rel_name):
        for path in list(candidates):
            if not path.lower().startswith("items\\"):
                candidates.append("items\\" + path)

    out = []
    seen = set()
    for candidate in candidates:
        key = candidate.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(candidate)
    return out


def _keys_for_repo_path(repo_path, extensions):
    rel_path = _repo_path(repo_path)
    if not rel_path:
        return []

    lower_rel = rel_path.lower()
    base_name = _basename(rel_path).lower()
    rel_stem, rel_ext = _split_known_extension(lower_rel, extensions)
    base_stem, base_ext = _split_known_extension(base_name, extensions)

    keys = [lower_rel]
    if rel_ext:
        keys.append(rel_stem)
    keys.append(base_name)
    if base_ext:
        keys.append(base_stem)

    out = []
    seen = set()
    for key in keys:
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def _keys_for_identifier(identifier, extensions):
    rel_name = _repo_path(identifier)
    if not rel_name:
        return []

    keys = []
    lower_rel = rel_name.lower()
    keys.append(lower_rel)

    rel_stem, rel_ext = _split_known_extension(lower_rel, extensions)
    if rel_ext:
        keys.append(rel_stem)
    else:
        for ext in extensions:
            keys.append(lower_rel + ext)

    base_name = _basename(rel_name).lower()
    if base_name != lower_rel:
        keys.append(base_name)

    base_stem, base_ext = _split_known_extension(base_name, extensions)
    if base_ext:
        keys.append(base_stem)
    else:
        for ext in extensions:
            keys.append(base_name + ext)

    if not _path_has_separator(rel_name):
        for ext in extensions:
            keys.append("items\\" + lower_rel + ext)

    out = []
    seen = set()
    for key in keys:
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def _score_repo_path(repo_path, source_game, source_rank=0):
    lower_path = _repo_path(repo_path).lower()
    _, ext = os.path.splitext(lower_path)

    if source_game == "w2" and lower_path.startswith(("items\\geralt\\", "items\\weapons\\")):
        domain_rank = 0
    elif lower_path.startswith("items\\"):
        domain_rank = 1
    else:
        domain_rank = 2

    if ext == ".w2ent":
        ext_rank = 0
    elif ext == ".w2mesh":
        ext_rank = 1
    else:
        ext_rank = 2

    return (int(source_rank), domain_rank, ext_rank, len(lower_path), lower_path)


def _add_repo_path(index, repo_path, source_game, extensions, *, source_rank=0, source_root=""):
    rel_path = _repo_path(repo_path)
    if not rel_path:
        return
    entry = _IndexedEntityPath(
        repo_path=rel_path,
        score=_score_repo_path(rel_path, source_game, source_rank=source_rank),
        source_root=source_root,
    )
    for key in _keys_for_repo_path(rel_path, extensions):
        existing = index.get(key)
        if existing is None or entry.score < existing.score:
            index[key] = entry


def _candidate_items_dirs(root):
    root = str(root or "").strip()
    if not root:
        return []
    try:
        root = os.path.normpath(root)
    except Exception:
        return []

    candidates = []
    if os.path.basename(root).lower() == "items":
        candidates.append((os.path.dirname(root), root))

    item_root = os.path.join(root, "items")
    if os.path.isdir(item_root):
        candidates.append((root, item_root))

    out = []
    seen = set()
    for repo_root, item_dir in candidates:
        key = _norm_root_key(item_dir)
        if key in seen:
            continue
        seen.add(key)
        if os.path.isdir(item_dir):
            out.append((repo_root, item_dir))
    return out


def _root_index_cache_key(source_game, root, extensions):
    return (source_game, _norm_root_key(root), tuple(extensions))


def _get_root_entity_index(source_game, root, extensions):
    cache_key = _root_index_cache_key(source_game, root, extensions)
    cached = _ROOT_ENTITY_PATH_INDEX.get(cache_key)
    if cached is not None:
        return cached

    index = {}
    for repo_root, item_dir in _candidate_items_dirs(root):
        for dirpath, dirnames, filenames in os.walk(item_dir):
            dirnames.sort()
            filenames.sort()
            for filename in filenames:
                if os.path.splitext(filename)[1].lower() not in extensions:
                    continue
                full_path = os.path.join(dirpath, filename)
                try:
                    rel_path = os.path.relpath(full_path, repo_root).replace("/", "\\")
                except Exception:
                    continue
                _add_repo_path(index, rel_path, source_game, extensions, source_root=root)

    _ROOT_ENTITY_PATH_INDEX[cache_key] = index
    return index


def _resolve_existing_path(identifier, source_game, roots, extensions):
    rel_name = _repo_path(identifier)
    if not rel_name:
        return None

    if os.path.isabs(str(identifier or "")):
        for root_index, root in enumerate(roots):
            if not is_under_root(identifier, root):
                continue
            if not os.path.exists(identifier):
                continue
            try:
                rel_path = os.path.relpath(identifier, root).replace("/", "\\")
            except Exception:
                continue
            return EntityPathResolveResult(
                repo_path=rel_path,
                source="existing_abs",
                matched_key=rel_path,
                source_root=root,
            )

    for root_index, root in enumerate(roots):
        for rel_path in _candidate_repo_paths(rel_name, extensions):
            candidate = os.path.join(root, rel_path)
            if not os.path.exists(candidate):
                continue
            return EntityPathResolveResult(
                repo_path=rel_path,
                source="existing_root",
                matched_key=rel_path,
                source_root=root,
            )
    return None


def _lookup_root_indexes(identifier, source_game, roots, extensions):
    keys = _keys_for_identifier(identifier, extensions)
    if not keys:
        return None
    best = None
    best_key = ""
    best_root = ""
    for root_index, root in enumerate(roots):
        index = _get_root_entity_index(source_game, root, extensions)
        for key in keys:
            entry = index.get(key)
            if entry is None:
                continue
            score = (root_index,) + tuple(entry.score)
            if best is None or score < best[0]:
                best = (score, entry)
                best_key = key
                best_root = root
    if best is None:
        return None
    entry = best[1]
    return EntityPathResolveResult(
        repo_path=entry.repo_path,
        source="root_index",
        matched_key=best_key,
        source_root=entry.source_root or best_root,
    )


def _current_w3_bundle_manager():
    try:
        from ..CR2W.witcher_cache.Bundles.BundleManager import BundleManager

        return BundleManager.InstanceManager
    except Exception:
        return None


def _get_bundle_manager(source_game, load_bundle_manager):
    if source_game == "w2":
        try:
            from ..CR2W.common_blender import _current_w2_bundle_manager, _load_w2_bundle_manager

            manager = _current_w2_bundle_manager()
            if (manager is None or not getattr(manager, "Items", None)) and load_bundle_manager:
                manager = _load_w2_bundle_manager()
            return manager
        except Exception:
            return None

    manager = _current_w3_bundle_manager()
    if (manager is None or not getattr(manager, "Items", None)) and load_bundle_manager:
        try:
            from ..CR2W.witcher_cache.Bundles import LoadBundleManager

            manager = LoadBundleManager()
        except Exception:
            manager = None
    return manager


def _bundle_index_cache_key(source_game, manager, extensions, include_non_items):
    items = getattr(manager, "Items", {}) if manager is not None else {}
    return (
        source_game,
        _norm_root_key(getattr(manager, "base_path", "")),
        id(manager),
        len(items),
        tuple(extensions),
        bool(include_non_items),
    )


def _get_bundle_entity_index(source_game, manager, extensions, include_non_items):
    if manager is None:
        return {}
    cache_key = _bundle_index_cache_key(source_game, manager, extensions, include_non_items)
    cached = _BUNDLE_ENTITY_PATH_INDEX.get(cache_key)
    if cached is not None:
        return cached

    index = {}
    items_by_path = getattr(manager, "Items", {}) or {}
    for item_key in items_by_path.keys():
        rel_path = _repo_path(item_key)
        lower_path = rel_path.lower()
        if os.path.splitext(lower_path)[1] not in extensions:
            continue
        if not include_non_items and not lower_path.startswith("items\\"):
            continue
        _add_repo_path(index, rel_path, source_game, extensions)

    _BUNDLE_ENTITY_PATH_INDEX[cache_key] = index
    return index


def _lookup_bundle_index(identifier, source_game, extensions, *, bundle_manager=None, load_bundle_manager=False, include_non_items=False):
    manager = bundle_manager or _get_bundle_manager(source_game, load_bundle_manager)
    if manager is None:
        return None

    keys = _keys_for_identifier(identifier, extensions)
    if not keys:
        return None

    index = _get_bundle_entity_index(source_game, manager, extensions, include_non_items)
    for key in keys:
        entry = index.get(key)
        if entry is not None:
            return EntityPathResolveResult(
                repo_path=entry.repo_path,
                source=f"{source_game}_bundle_index",
                matched_key=key,
                source_root=str(getattr(manager, "base_path", "") or ""),
            )
    return None


def _resolve_roots(source_game, search_roots):
    roots = list(search_roots or [])
    if source_game == "w2":
        try:
            roots.extend(configured_w2_repo_roots())
        except Exception:
            pass
    return normalize_roots(roots)


def resolve_entity_repo_path(
    identifier,
    *,
    source_game="w3",
    search_roots=None,
    extensions=None,
    allow_bundle_index=True,
    bundle_manager=None,
    load_bundle_manager=False,
    include_non_items=False,
):
    """Resolve a short entity/template id to a repo-relative path.

    The result is intentionally not an extracted absolute path; pass
    ``result.repo_path`` to ``materialize_entity_repo_path()`` to materialize it.
    """
    rel_name = _repo_path(identifier)
    if not rel_name:
        return None

    source_game = normalize_source_game(source_game)
    extensions = _normalize_extensions(extensions)
    roots = _resolve_roots(source_game, search_roots)

    resolved = _resolve_existing_path(rel_name, source_game, roots, extensions)
    if resolved:
        return resolved

    resolved = _lookup_root_indexes(rel_name, source_game, roots, extensions)
    if resolved:
        return resolved

    if allow_bundle_index:
        resolved = _lookup_bundle_index(
            rel_name,
            source_game,
            extensions,
            bundle_manager=bundle_manager,
            load_bundle_manager=load_bundle_manager,
            include_non_items=include_non_items,
        )
        if resolved:
            return resolved

    return None


def remember_entity_repo_path(root, repo_path, *, source_game="w3"):
    """Teach existing root indexes about a repo path extracted after indexing."""
    root_key = _norm_root_key(root)
    rel_path = _repo_path(repo_path)
    if not root_key or not rel_path:
        return

    source_game = normalize_source_game(source_game)
    for (cached_source_game, cached_root, _extensions), index in list(_ROOT_ENTITY_PATH_INDEX.items()):
        if cached_source_game != source_game or cached_root != root_key:
            continue
        _add_repo_path(index, rel_path, source_game, _extensions, source_root=root)
