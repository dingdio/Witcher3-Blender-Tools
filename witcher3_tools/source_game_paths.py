import os


W2_REPO_VERSION = 115
W3_REPO_VERSION = 999
W2_REPO_ROOT_MARKERS = (
    "\\levels\\",
    "\\templates\\",
    "\\environment_levels\\",
    "\\environment\\",
    "\\game\\",
    "\\characters\\",
    "\\items\\",
    "\\engine\\",
    "\\dlc\\",
)


def normalize_source_game(source_game) -> str:
    value = str(source_game or "").strip().lower().replace(" ", "")
    return "w2" if value in {"w2", "witcher2", "tw2"} else "w3"


def source_game_from_version(version) -> str:
    try:
        return "w2" if int(version) <= W2_REPO_VERSION else "w3"
    except Exception:
        return "w3"


def version_for_source_game(source_game, default_version=W3_REPO_VERSION) -> int:
    return W2_REPO_VERSION if normalize_source_game(source_game) == "w2" else int(default_version)


def coerce_w2_data_root(path_value) -> str:
    path_value = str(path_value or "").strip()
    if not path_value:
        return ""
    norm_path = os.path.normpath(path_value)
    if os.path.basename(norm_path).lower() == "data":
        return norm_path
    return os.path.normpath(os.path.join(norm_path, "data"))


def normalize_roots(roots, *, existing_only=False):
    out = []
    seen = set()
    for root in roots or []:
        if not root:
            continue
        root = str(root)
        try:
            norm = os.path.normcase(os.path.normpath(root))
        except Exception:
            norm = root.lower()
        if not norm or norm in seen:
            continue
        if existing_only and not os.path.isdir(root):
            continue
        seen.add(norm)
        out.append(root)
    return out


def w2_source_repo_root(source_filename) -> str:
    if not source_filename or not os.path.isabs(str(source_filename)):
        return ""
    norm_path = os.path.normpath(str(source_filename))
    lower_path = norm_path.lower()
    hits = [lower_path.find(marker) for marker in W2_REPO_ROOT_MARKERS if lower_path.find(marker) > 2]
    if not hits:
        return ""
    return norm_path[: min(hits)].rstrip("\\")


def _existing_path(path_value) -> bool:
    if os.path.exists(path_value):
        return True
    try:
        from .CR2W.common_blender import win_safe_path

        return os.path.exists(win_safe_path(path_value))
    except Exception:
        return False


def resolve_w2_repo_file_from_root(filepath, root, *, extract_from_bundles=True) -> str:
    root = str(root or "").strip()
    rel_path = str(filepath or "").replace("/", "\\").lstrip("\\")
    if not root or not rel_path or os.path.isabs(rel_path):
        return ""

    candidate = os.path.join(root, rel_path)
    if _existing_path(candidate):
        return candidate
    if not extract_from_bundles:
        return ""

    try:
        from .CR2W.common_blender import _extract_w2_bundle_repo_file

        return _extract_w2_bundle_repo_file(rel_path, root)
    except Exception:
        return ""


def resolve_w2_repo_file_from_source(filepath, source_filename, *, version=None) -> str:
    if version is not None and source_game_from_version(version) != "w2":
        return ""
    source_root = w2_source_repo_root(source_filename)
    if not source_root:
        return ""
    return resolve_w2_repo_file_from_root(filepath, source_root)


def source_game_for_rig_settings(rig_settings, fallback="w3") -> str:
    return normalize_source_game(getattr(rig_settings, "source_game", "") or fallback)


def source_game_for_animset_item(item, rig_settings=None, fallback="w3") -> str:
    item_game = normalize_source_game(getattr(item, "source_game", "") or "")
    if getattr(item, "source_game", ""):
        return item_game
    return source_game_for_rig_settings(rig_settings, fallback=fallback)


def repo_file_for_source(filepath, source_game="", *, version=None, is_abs_path=False):
    if not filepath:
        return ""
    from .CR2W.common_blender import repo_file

    effective_version = version if version is not None else version_for_source_game(source_game)
    return repo_file(filepath, version=effective_version, is_abs_path=is_abs_path)


def source_roots(context, source_game, *, existing_only=False):
    from . import get_uncook_path, get_w2_unbundle_path, get_witcher2_game_path

    roots = []
    if normalize_source_game(source_game) == "w2":
        w2_uncook = str(get_w2_unbundle_path(context) or "").strip()
        if w2_uncook:
            roots.append(w2_uncook)
        w2_game = str(get_witcher2_game_path(context) or "").strip()
        if w2_game:
            roots.append(coerce_w2_data_root(w2_game))
    else:
        uncook = str(get_uncook_path(context) or "").strip()
        if uncook:
            roots.append(uncook)

    return normalize_roots(roots, existing_only=existing_only)


def w2_source_roots(context, *, existing_only=False):
    return source_roots(context, "w2", existing_only=existing_only)


def iter_w2_texture_path_variants(filepath: str):
    filepath = str(filepath or "").replace("/", "\\").lstrip("\\")
    if not filepath:
        return
    yield filepath
    root, ext = os.path.splitext(filepath)
    ext = ext.lower()
    if ext in {".dds", ".tga", ".png", ".jpg", ".jpeg", ".bmp"}:
        yield root + ".xbm"
    elif ext == ".xbm":
        yield root + ".dds"


def iter_w2_known_alias_paths(filepath: str):
    filepath = str(filepath or "").replace("/", "\\").lstrip("\\")
    base = filepath.rsplit("\\", 1)[-1].lower()
    stem, ext = os.path.splitext(base)
    if ext == ".w2mesh":
        lower_filepath = filepath.lower()
        model_marker = "\\model\\"
        path_offset = len("templates\\") if lower_filepath.startswith("templates\\") else 0
        match_path = lower_filepath[path_offset:]
        if match_path.startswith("items\\geralt\\geralt_secondary_wepons\\"):
            model_idx = match_path.rfind(model_marker)
            if model_idx != -1:
                model_idx += path_offset
                yield filepath[:model_idx] + "\\" + filepath[model_idx + len(model_marker):]
        return
    if ext != ".w2rig":
        return
    scabbard_folders = {
        "scabbardsteel": "scabbard_steel",
        "scabbardsilver": "scabbard_silver",
        "scabbardsabre": "scabbard_sabre",
    }
    folder = scabbard_folders.get(stem)
    if folder:
        yield f"items\\geralt\\geralt_scabbards\\{folder}\\{base}"
    witcher_rig_aliases = {
        "witcher_moj": "characters\\templates\\witcher\\model\\witcher_without_ponytail.w2rig",
        "witcher_with_ponytail": "characters\\templates\\witcher\\model\\witcher_rig.w2rig",
    }
    alias = witcher_rig_aliases.get(stem)
    if alias:
        yield alias
    if stem.startswith("door_"):
        yield f"environment\\decorations\\devices\\door\\model\\{base}"


def iter_w2_repo_path_variants(filepath: str):
    filepath = str(filepath or "").replace("/", "\\").lstrip("\\")
    if not filepath:
        return
    seen = set()

    def emit(value):
        value = value.replace("/", "\\").lstrip("\\")
        key = value.lower()
        if value and key not in seen:
            seen.add(key)
            return value
        return None

    def emit_with_template_variants(value):
        first = emit(value)
        if first:
            yield first

        lower_value = value.lower()
        if not lower_value.startswith("templates\\"):
            candidate = emit("templates\\" + value)
            if candidate:
                yield candidate
        else:
            stripped = value[len("templates\\"):].lstrip("\\")
            candidate = emit(stripped)
            if candidate:
                yield candidate

    for candidate in iter_w2_texture_path_variants(filepath):
        yield from emit_with_template_variants(candidate)
    for candidate in iter_w2_known_alias_paths(filepath):
        yield from emit_with_template_variants(candidate)


def existing_repo_file_for_source(filepath, source_game="", *, version=None, allow_json=True):
    resolved = repo_file_for_source(filepath, source_game, version=version)
    if not resolved:
        return ""
    from .CR2W.common_blender import win_safe_path

    safe = win_safe_path(resolved)
    if allow_json and os.path.isfile(safe + ".json"):
        return resolved + ".json"
    if os.path.exists(safe):
        return resolved
    return ""


def display_path_relative_to_source_roots(path_value, context, source_game):
    path_value = str(path_value or "").strip()
    if not path_value:
        return ""
    try:
        norm_path = os.path.normpath(path_value)
        for root in source_roots(context, source_game):
            norm_root = os.path.normpath(root)
            rel_path = os.path.relpath(norm_path, norm_root)
            if not rel_path.startswith(".."):
                return rel_path.replace("\\", "/")
    except Exception:
        pass
    return ""
