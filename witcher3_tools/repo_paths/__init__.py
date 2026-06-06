"""Path and repo-resolution helpers for Witcher source assets."""

from .source_game import (
    W2_REPO_ROOT_MARKERS,
    W2_REPO_VERSION,
    W3_REPO_VERSION,
    coerce_w2_data_root,
    configured_w2_repo_roots,
    display_path_relative_to_source_roots,
    is_under_root,
    iter_w2_known_alias_paths,
    iter_w2_repo_path_variants,
    iter_w2_texture_path_variants,
    normalize_roots,
    normalize_source_game,
    resolve_w2_repo_file_from_root,
    resolve_w2_repo_file_from_source,
    same_path,
    source_game_for_animset_item,
    source_game_for_rig_settings,
    source_game_from_version,
    source_root_candidates_from_file,
    source_roots,
    version_for_source_game,
    w2_source_repo_root,
    w2_source_repo_root_if_configured,
    w2_source_roots,
)

from .materialize import (
    existing_repo_file_for_source,
    materialize_entity_repo_path,
    materialize_repo_path,
    materialize_resolved_entity,
    repo_file_for_source,
    resolve_materialized_entity_path,
)

from .entity_resolver import (
    DEFAULT_ENTITY_EXTENSIONS,
    EQUIPMENT_ENTITY_EXTENSIONS,
    EntityPathResolveResult,
    clear_entity_path_resolver_caches,
    remember_entity_repo_path,
    resolve_entity_repo_path,
)
