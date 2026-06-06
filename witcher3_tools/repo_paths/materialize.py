"""Materialize repo-relative asset paths into filesystem paths.

Resolvers in this package return depot/repo-relative paths. This module is the
boundary that turns those paths into absolute extracted/readable paths. The
legacy extraction implementation still lives in ``CR2W.common_blender``; this
module provides the package-level API so callers do not need to import from CR2W
directly.
"""

from __future__ import annotations

import os

from .source_game import normalize_source_game, version_for_source_game


def materialize_repo_path(filepath, source_game="", *, version=None, is_abs_path=False):
    """Return the absolute/materialized path for a repo-relative asset path."""
    if not filepath:
        return ""

    from ..CR2W.common_blender import repo_file

    effective_version = version if version is not None else version_for_source_game(source_game)
    return repo_file(filepath, version=effective_version, is_abs_path=is_abs_path)


def materialize_entity_repo_path(filepath, source_game="", *, version=None):
    """Materialize an entity/template repo path using the source game's version."""
    return materialize_repo_path(filepath, source_game=source_game, version=version)


def repo_file_for_source(filepath, source_game="", *, version=None, is_abs_path=False):
    """Compatibility-style alias for source-aware repo materialization."""
    return materialize_repo_path(
        filepath,
        source_game=source_game,
        version=version,
        is_abs_path=is_abs_path,
    )


def existing_repo_file_for_source(filepath, source_game="", *, version=None, allow_json=True):
    resolved = repo_file_for_source(filepath, source_game, version=version)
    if not resolved:
        return ""

    from ..CR2W.common_blender import win_safe_path

    safe = win_safe_path(resolved)
    if allow_json and os.path.isfile(safe + ".json"):
        return resolved + ".json"
    if os.path.exists(safe):
        return resolved
    return ""


def materialize_resolved_entity(result, source_game="", *, version=None):
    """Materialize an EntityPathResolveResult-like object."""
    repo_path = getattr(result, "repo_path", "") if result is not None else ""
    if not repo_path:
        return ""
    return materialize_entity_repo_path(
        repo_path,
        source_game=normalize_source_game(source_game),
        version=version,
    )


def resolve_materialized_entity_path(
    identifier,
    source_game="",
    *,
    search_roots=None,
    extensions=None,
    allow_bundle_index=True,
    bundle_manager=None,
    load_bundle_manager=False,
    include_non_items=False,
    version=None,
):
    """Resolve an entity/template id and return the materialized filesystem path."""
    from .entity_resolver import resolve_entity_repo_path

    source_key = normalize_source_game(source_game)
    resolved = resolve_entity_repo_path(
        identifier,
        source_game=source_key,
        search_roots=search_roots,
        extensions=extensions,
        allow_bundle_index=allow_bundle_index,
        bundle_manager=bundle_manager,
        load_bundle_manager=load_bundle_manager,
        include_non_items=include_non_items,
    )
    return materialize_resolved_entity(resolved, source_game=source_key, version=version)
