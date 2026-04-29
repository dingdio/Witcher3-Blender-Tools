try:
    import bpy
    from .witcher_cache.Bundles import LoadBundleManager
    from .witcher_cache.Bundles import BundleItem
    try:
        from .. import get_addon_name
        addon_name = get_addon_name()
    except Exception:
        addon_name = (__package__ or __name__).split('.')[0]
except Exception as e:
    pass
    #raise e
import os
import re
import json
import shutil
import sys
from contextlib import contextmanager
from ..extension_paths import get_dev_override
import logging
log = logging.getLogger(__name__)


def _get_addon_prefs():
    ctx = getattr(bpy, "context", None) if "bpy" in globals() else None
    prefs_root = getattr(ctx, "preferences", None) if ctx else None
    addons = getattr(prefs_root, "addons", None) if prefs_root else None
    if not addons:
        return None
    try:
        addon_entry = addons.get(addon_name) if hasattr(addons, "get") else addons[addon_name]
    except Exception:
        return None
    return getattr(addon_entry, "preferences", None)


def win_safe_path(path: str) -> str:
    """On Windows, apply \\?\\ prefix for paths > 250 chars to bypass MAX_PATH.
    Safe on all Windows 10/11 machines — no registry changes needed.
    NOTE: never call os.path.normpath() on the result (it strips the prefix).
    Returns the original path unchanged on non-Windows or short paths."""
    if sys.platform != 'win32' or not path:
        return path
    if path.startswith('\\\\?\\'):
        # Preserve valid extended-length filesystem paths, but strip invalid prefixes from depot paths.
        unprefixed = win_unprefix_path(path)
        drive, _ = os.path.splitdrive(unprefixed)
        is_unc = unprefixed.startswith('\\\\')
        return path if (drive or is_unc) else unprefixed
    # Only prefix real filesystem paths (drive letter or UNC). Never prefix depot/game-relative paths.
    drive, _ = os.path.splitdrive(path)
    is_unc = path.startswith('\\\\')
    if not drive and not is_unc:
        return path
    abs_p = os.path.abspath(path)
    if len(abs_p) > 250:
        if abs_p.startswith('\\\\'):
            return '\\\\?\\UNC\\' + abs_p.lstrip('\\')
        return '\\\\?\\' + abs_p
    return path


def win_bpy_image_path(path: str) -> str:
    """Blender image loading on Windows is more reliable with an explicit extended-length path."""
    if sys.platform != 'win32' or not path:
        return path
    if path.startswith('\\\\?\\'):
        # Keep valid extended filesystem paths; strip invalid prefixes from depot paths.
        unprefixed = win_unprefix_path(path)
        drive, _ = os.path.splitdrive(unprefixed)
        is_unc = unprefixed.startswith('\\\\')
        return path if (drive or is_unc) else unprefixed
    # Never prefix depot/game-relative paths like "characters\\...".
    drive, _ = os.path.splitdrive(path)
    is_unc = path.startswith('\\\\')
    if not drive and not is_unc:
        return path
    abs_p = os.path.abspath(path)
    if len(abs_p) <= 250:
        return path
    if abs_p.startswith('\\\\'):
        # UNC path -> \\?\UNC\server\share\...
        return '\\\\?\\UNC\\' + abs_p.lstrip('\\')
    return '\\\\?\\' + abs_p


def win_unprefix_path(path):
    """Remove Windows extended-length prefix for display/comparison."""
    if path is None:
        return path
    p = os.fspath(path)
    if not isinstance(p, str):
        return p
    if p.startswith('\\\\?\\UNC\\'):
        return '\\\\' + p[8:]
    if p.startswith('\\\\?\\'):
        return p[4:]
    return p


def win_path_key(path) -> str:
    """Case-insensitive normalized path key that treats prefixed/unprefixed Windows paths as equal."""
    if not path:
        return ""
    p = win_unprefix_path(path)
    try:
        p = os.path.abspath(p)
    except Exception:
        p = str(p)
    try:
        return os.path.normcase(p)
    except Exception:
        return str(p).lower()


def win_path_exists(path) -> bool:
    """`os.path.exists` with Windows long-path support."""
    if not path:
        return False
    try:
        return os.path.exists(win_safe_path(os.fspath(path)))
    except Exception:
        return False


def _iter_w3_mesh_fallback_paths(filepath: str):
    if not filepath:
        return
    normalized = str(filepath).replace("/", "\\")
    lower = normalized.lower()
    if not lower.endswith(".w2mesh") or lower.endswith("_hires.w2mesh"):
        return
    stem, ext = os.path.splitext(normalized)
    yield stem + "_hires" + ext


def _repair_missing_w3_mesh_repo_path(bundle_manager, extract_root: str, filepath: str) -> str:
    if not bundle_manager:
        return ""
    for candidate in _iter_w3_mesh_fallback_paths(filepath):
        candidate_abs = os.path.join(extract_root, candidate)
        if os.path.exists(win_safe_path(candidate_abs)):
            return candidate
        if bundle_manager.find_item_by_hash(candidate):
            return candidate
        if bundle_manager.find_item_by_hash(f"{candidate}.1.buffer"):
            return candidate
    return ""


def win_path_getsize(path) -> int:
    """`os.path.getsize` with Windows long-path support."""
    return os.path.getsize(win_safe_path(os.fspath(path)))


def win_path_getmtime(path) -> float:
    """`os.path.getmtime` with Windows long-path support."""
    return os.path.getmtime(win_safe_path(os.fspath(path)))


def win_path_isfile(path) -> bool:
    """`os.path.isfile` with Windows long-path support."""
    if not path:
        return False
    try:
        return os.path.isfile(win_safe_path(os.fspath(path)))
    except Exception:
        return False


def win_path_isdir(path) -> bool:
    """`os.path.isdir` with Windows long-path support."""
    if not path:
        return False
    try:
        return os.path.isdir(win_safe_path(os.fspath(path)))
    except Exception:
        return False


def bpy_image_load_safe(path, **kwargs):
    """Load an image in Blender using an explicit Windows extended-length path when applicable."""
    import bpy

    original_path = win_unprefix_path(os.fspath(path))
    img = bpy.data.images.load(win_bpy_image_path(original_path), **kwargs)
    # Keep depot/game-relative paths unmodified in Blender UI/material panels.
    if img and not os.path.splitdrive(original_path)[0] and not original_path.startswith('\\\\'):
        if getattr(img, "filepath", None) != original_path:
            img.filepath = original_path
    return img


_repo_override_roots = []
_repo_override_read_only = False
_mod_priority_enabled = False
_mod_priority_high = True
_overwrite_existing = False
_mod_index = None
_mod_index_ready = False
_source_map_cache = {
    "path": "",
    "data": {},
    "mtime": 0,
}

def clear_mod_index_cache():
    """Clear cached mod override index so it can be rebuilt from current cache data."""
    global _mod_index, _mod_index_ready
    _mod_index = None
    _mod_index_ready = False

def set_repo_override_roots(roots, read_only=False):
    """Override repo_file search roots (read-only safe)."""
    global _repo_override_roots, _repo_override_read_only
    _repo_override_roots = [os.path.normpath(r) for r in (roots or []) if r]
    _repo_override_read_only = bool(read_only)

def clear_repo_override_roots():
    """Clear repo_file override roots."""
    global _repo_override_roots, _repo_override_read_only
    _repo_override_roots = []
    _repo_override_read_only = False

def set_mod_priority_settings(enabled=False, prefer_mods=True):
    """Enable mod priority resolution in repo_file."""
    global _mod_priority_enabled, _mod_priority_high
    _mod_priority_enabled = bool(enabled)
    _mod_priority_high = bool(prefer_mods)

def clear_mod_priority_settings():
    global _mod_priority_enabled, _mod_priority_high
    _mod_priority_enabled = False
    _mod_priority_high = True


def get_mod_priority_state():
    return bool(_mod_priority_enabled), bool(_mod_priority_high)

def set_overwrite_existing(enabled=False):
    """Allow repo_file to overwrite existing extracted files (with backup)."""
    global _overwrite_existing
    _overwrite_existing = bool(enabled)

def clear_overwrite_existing():
    global _overwrite_existing
    _overwrite_existing = False


def overwrite_existing_enabled() -> bool:
    return bool(_overwrite_existing)


def get_repo_override_state():
    return list(_repo_override_roots), bool(_repo_override_read_only)

@contextmanager
def mod_loading_context(context=None, prefer_mods=None, overwrite=None):
    """Context manager that configures mod loading for all repo_file calls within the block.

    Reads settings from the Blender scene (witcher_file_browser) when context is provided
    and no explicit overrides are given.  Automatically clears globals on exit.

    Usage:
        with mod_loading_context(context):
            import_entity.import_ent_template(path, ...)
    """
    if context is not None and (prefer_mods is None or overwrite is None):
        try:
            witcher_file_browser = context.scene.witcher_file_browser
            if prefer_mods is None:
                prefer_mods = witcher_file_browser.use_mods_priority
            if overwrite is None:
                overwrite = witcher_file_browser.mods_overwrite
        except Exception:
            pass
    if prefer_mods is None:
        prefer_mods = False
    if overwrite is None:
        overwrite = False

    prev_enabled, prev_high = _mod_priority_enabled, _mod_priority_high
    prev_overwrite = _overwrite_existing
    set_mod_priority_settings(True, prefer_mods)
    set_overwrite_existing(overwrite)
    try:
        yield
    finally:
        set_mod_priority_settings(prev_enabled, prev_high)
        set_overwrite_existing(prev_overwrite)

def _is_under_root(path, root):
    try:
        return os.path.commonpath([os.path.normpath(path), os.path.normpath(root)]) == os.path.normpath(root)
    except Exception:
        return False

def _is_readonly_target(path):
    return _repo_override_read_only and any(_is_under_root(path, root) for root in _repo_override_roots)

def _get_source_map_path(uncook_path: str) -> str:
    return os.path.join(uncook_path, "_witcher_tools_sources.json")

def _load_source_map(uncook_path: str) -> dict:
    global _source_map_cache
    map_path = _get_source_map_path(uncook_path)
    try:
        mtime = os.path.getmtime(map_path) if os.path.exists(map_path) else 0
    except Exception:
        mtime = 0
    if _source_map_cache["path"] != map_path or _source_map_cache["mtime"] != mtime:
        data = {}
        if os.path.exists(map_path):
            try:
                with open(map_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        _source_map_cache = {
            "path": map_path,
            "data": data if isinstance(data, dict) else {},
            "mtime": mtime,
        }
    return _source_map_cache["data"]

def _save_source_map(uncook_path: str, data: dict) -> None:
    map_path = _get_source_map_path(uncook_path)
    try:
        os.makedirs(os.path.dirname(map_path), exist_ok=True)
        with open(map_path, "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"), sort_keys=True)
        _source_map_cache["path"] = map_path
        _source_map_cache["data"] = data
        _source_map_cache["mtime"] = os.path.getmtime(map_path)
    except Exception:
        pass

def set_source_for_path(uncook_path: str, rel_path: str, source_label: str) -> None:
    data = _load_source_map(uncook_path)
    data[rel_path] = source_label
    _save_source_map(uncook_path, data)

def get_source_for_path(uncook_path: str, rel_path: str) -> str:
    data = _load_source_map(uncook_path)
    return data.get(rel_path, "")

def _backup_existing_file(abs_path: str, uncook_path: str) -> None:
    try:
        rel_path = os.path.relpath(abs_path, uncook_path)
    except Exception:
        rel_path = os.path.basename(abs_path)
    backup_root = os.path.join(uncook_path, "_mod_overrides_backup")
    backup_path = os.path.join(backup_root, rel_path)
    os.makedirs(os.path.dirname(win_safe_path(backup_path)), exist_ok=True)
    final_path = backup_path
    if os.path.exists(win_safe_path(final_path)):
        base, ext = os.path.splitext(backup_path)
        idx = 1
        while os.path.exists(win_safe_path(f"{base}.bak{idx}{ext}")):
            idx += 1
        final_path = f"{base}.bak{idx}{ext}"
    try:
        shutil.move(win_safe_path(abs_path), win_safe_path(final_path))
    except Exception:
        pass

def prepare_extraction_target(abs_path: str, uncook_path: str) -> bool:
    """Prepare an output path for extraction. Returns False if should skip."""
    if _is_readonly_target(abs_path):
        return False
    if os.path.exists(win_safe_path(abs_path)):
        if not _overwrite_existing:
            return False
        _backup_existing_file(abs_path, uncook_path)
    parent = os.path.dirname(abs_path)
    safe_parent = win_safe_path(parent) if parent else parent
    if parent and not os.path.exists(safe_parent):
        os.makedirs(safe_parent, exist_ok=True)
    return True

def _strip_buffer_suffix(path: str) -> str:
    return re.sub(r"\.\d+\.buffer$", "", path, flags=re.IGNORECASE)

_BULK_BUFFER_PROBE_LIMIT = 100

def _get_buffer_sidecar_index(path: str, base_path: str):
    if not path or not base_path:
        return None
    norm_path = path.replace("/", "\\")
    norm_base = base_path.replace("/", "\\")
    prefix = norm_base + "."
    lower_path = norm_path.lower()
    if not lower_path.startswith(prefix.lower()) or not lower_path.endswith(".buffer"):
        return None
    suffix = norm_path[len(prefix):-len(".buffer")]
    if not suffix.isdigit():
        return None
    return int(suffix)

def _collect_buffer_sidecar_entries(entries, base_path: str):
    matches = []
    for entry_path, item_list in entries:
        if not isinstance(entry_path, str):
            continue
        sidecar_index = _get_buffer_sidecar_index(entry_path, base_path)
        if sidecar_index is None:
            continue
        matches.append((sidecar_index, entry_path.replace("/", "\\"), item_list))
    matches.sort(key=lambda item: item[0])
    return matches

def _collect_buffer_sidecar_items(bundle_manager, filepath: str):
    matches = []
    for buf_idx in range(1, _BULK_BUFFER_PROBE_LIMIT + 1):
        rel_path = f"{filepath}.{buf_idx}.buffer"
        item_list = bundle_manager.find_item_by_hash(rel_path)
        if item_list:
            matches.append((rel_path, item_list))
            continue
        if buf_idx < _BULK_BUFFER_PROBE_LIMIT:
            return matches
        break

    seen_paths = {path.lower() for path, _ in matches}
    for _, rel_path, item_list in _collect_buffer_sidecar_entries(bundle_manager.Items.items(), filepath):
        rel_key = rel_path.lower()
        if rel_key in seen_paths:
            continue
        matches.append((rel_path, item_list))
    return matches

def _collect_bundle_extract_items(bundle_manager, filepath: str):
    items = []
    base_item = bundle_manager.find_item_by_hash(filepath)
    if base_item:
        items.append((filepath, base_item))
    if filepath.endswith('.w2mesh') or filepath.endswith('.w2anims'):
        items.extend(_collect_buffer_sidecar_items(bundle_manager, filepath))
    return items

def _normalize_mod_inner_path(inner: str) -> str:
    if not inner:
        return inner
    norm = inner.replace("/", "\\")
    lower = norm.lower()
    for prefix in ("content\\", "content0\\", "content1\\", "content2\\"):
        if lower.startswith(prefix):
            norm = norm[len(prefix):]
            lower = norm.lower()
            break
    for marker in ("\\content\\", "\\content0\\", "\\content1\\", "\\content2\\"):
        if marker in lower:
            idx = lower.index(marker) + len(marker)
            norm = norm[idx:]
            lower = norm.lower()
            break
    return norm
def _build_mod_order():
    order = {}
    try:
        from .witcher_cache.common_cache.WitcherArchiveManager import Configuration, WitcherArchiveManager
        from .witcher_cache import cache_meta
        mods_dirs = cache_meta.get_mod_dirs(Configuration.GameModDir)
        for idx, d in enumerate(mods_dirs):
            order[os.path.basename(d)] = idx
        dlc_dirs = cache_meta.get_dlc_dirs(Configuration.ExecutablePath, vanilla_only=False, vanilla_list=WitcherArchiveManager.VanillaDLClist)
        vanilla_set = {v.lower() for v in WitcherArchiveManager.VanillaDLClist}
        start = len(order)
        for idx, d in enumerate(dlc_dirs):
            name = os.path.basename(d)
            if name.lower() in vanilla_set:
                continue
            if name not in order:
                order[name] = start + idx
    except Exception:
        pass
    return order

def _ensure_mod_index():
    global _mod_index, _mod_index_ready
    if _mod_index_ready and _mod_index is not None:
        return
    _mod_index = {}
    _mod_index_ready = True
    try:
        manager = LoadBundleManager(loadmods=True)
    except Exception:
        return
    mod_order = _build_mod_order()
    for key, items in manager.Items.items():
        if not isinstance(key, str):
            continue
        key_norm = key.replace("/", "\\")
        if "\\" not in key_norm:
            continue
        mod_name, inner = key_norm.split("\\", 1)
        inner_norm = _normalize_mod_inner_path(inner)
        base_path = _strip_buffer_suffix(inner_norm).lower()
        order = mod_order.get(mod_name, 0)
        entry = _mod_index.get(base_path)
        if entry is None or order > entry["order"]:
            entry = {"mod": mod_name, "order": order, "items": []}
            _mod_index[base_path] = entry
        if entry["mod"] == mod_name:
            entry["items"].append((inner_norm, items))

def get_mod_override_name(filepath: str) -> str:
    if not filepath:
        return ""
    _ensure_mod_index()
    key = filepath.replace("/", "\\")
    key = _strip_buffer_suffix(key).lower()
    entry = _mod_index.get(key) if _mod_index else None
    return entry["mod"] if entry else ""

def _get_mod_entry(filepath: str):
    _ensure_mod_index()
    key = filepath.replace("/", "\\")
    key = _strip_buffer_suffix(key).lower()
    return _mod_index.get(key) if _mod_index else None


_TEXTURE_REPO_EXTENSIONS = {
    ".xbm",
    ".dds",
    ".tga",
    ".png",
    ".jpg",
    ".jpeg",
    ".bmp",
    ".w2cube",
}


def _is_texture_repo_path(filepath: str) -> bool:
    ext = os.path.splitext((filepath or "").replace("/", "\\"))[1].lower()
    return ext in _TEXTURE_REPO_EXTENSIONS


def _get_repo_roots_from_prefs(version=999):
    use_separate_texture_path = False
    fbx_uncook_path = ""
    uncook_path = ""
    texture_path = ""

    prefs = _get_addon_prefs()
    if not prefs:
        return fbx_uncook_path, uncook_path, texture_path, use_separate_texture_path

    fbx_uncook_path = prefs.fbx_uncook_path
    uncook_path = prefs.uncook_path
    texture_path = uncook_path
    use_separate_texture_path = bool(getattr(prefs, "use_separate_texture_uncook_path", False))
    if use_separate_texture_path:
        separate_texture_path = str(getattr(prefs, "tex_uncook_path", "") or "").strip()
        if separate_texture_path:
            texture_path = separate_texture_path

    if version <= 115:
        fbx_uncook_path = prefs.fbx_uncook_path
        uncook_path = prefs.witcher2_game_path + '\\data'
        texture_path = uncook_path

    return fbx_uncook_path, uncook_path, texture_path, use_separate_texture_path

def repo_file(filepath: str, version = 999, is_abs_path = False):
    try:
        version = int(version)
    except Exception:
        version = 999

    fbx_uncook_path, uncook_path, texture_path, use_separate_texture_path = _get_repo_roots_from_prefs(version)

    # Check override roots first (read-only depots/workspaces)
    if _repo_override_roots:
        if os.path.isabs(filepath):
            for root in _repo_override_roots:
                if _is_under_root(filepath, root):
                    return filepath
        for root in _repo_override_roots:
            candidate = os.path.join(root, filepath)
            if os.path.exists(win_safe_path(candidate)):
                return candidate

    filepath = filepath.replace("/", "\\")

    if os.path.isabs(filepath) and not is_abs_path:
        return filepath

    if is_abs_path:
        for root in (texture_path, uncook_path):
            root = (root or "").replace("/", "\\").rstrip("\\")
            if not root:
                continue
            prefix = root + "\\"
            if filepath.lower().startswith(prefix.lower()):
                filepath = filepath[len(prefix):]
                break

    filepath_key = filepath.lower()

    is_texture_repo = version > 115 and use_separate_texture_path and _is_texture_repo_path(filepath)
    extract_root = uncook_path
    if is_texture_repo:
        texture_abs = os.path.join(texture_path, filepath)
        uncook_abs = os.path.join(uncook_path, filepath)
        if not _overwrite_existing:
            if os.path.exists(win_safe_path(texture_abs)):
                return texture_abs
            if os.path.exists(win_safe_path(uncook_abs)):
                return uncook_abs
        # Texture cache/mirror is primary source; bundle fallback writes to uncook.
        extract_root = uncook_path

    if filepath.endswith('.fbx'):
        if not fbx_uncook_path:
            return filepath
        return os.path.join(fbx_uncook_path, filepath)
    else:
        if not extract_root:
            if is_texture_repo and texture_path:
                return os.path.join(texture_path, filepath)
            return filepath
        abs_filename = os.path.join(extract_root, filepath)
        if version <= 115 and not os.path.exists(win_safe_path(abs_filename)):
            templates_fallback = None
            lower_filepath = filepath.lower()
            if not lower_filepath.startswith("templates\\"):
                templates_fallback = os.path.join(extract_root, "templates", filepath)
            elif lower_filepath.startswith("templates\\"):
                stripped = filepath[len("templates\\"):]
                if stripped:
                    fallback_candidate = os.path.join(extract_root, stripped)
                    if os.path.exists(win_safe_path(fallback_candidate)):
                        return fallback_candidate
            if templates_fallback and os.path.exists(win_safe_path(templates_fallback)):
                return templates_fallback
        mod_entry = None
        if _mod_priority_enabled:
            mod_entry = _get_mod_entry(filepath)
            if mod_entry and _mod_priority_high:
                if _is_readonly_target(abs_filename):
                    return abs_filename
                mod_label = f"mod:{mod_entry['mod']}"
                base_exists = os.path.exists(win_safe_path(abs_filename))
                base_source = get_source_for_path(extract_root, filepath) if base_exists else ""
                base_from_same_mod = base_source == mod_label
                if base_exists and not _overwrite_existing and not base_from_same_mod:
                    # Avoid mixing mod buffers with a base file from another source.
                    return abs_filename

                base_item = None
                buffer_items = _collect_buffer_sidecar_entries(mod_entry["items"], filepath)
                for inner, item_list in mod_entry["items"]:
                    inner_norm = _normalize_mod_inner_path(inner)
                    if inner_norm.lower() == filepath_key:
                        base_item = (inner_norm, item_list)

                extracted_any = False
                base_extracted = False
                if base_item:
                    out_path = os.path.join(extract_root, base_item[0])
                    if prepare_extraction_target(out_path, extract_root):
                        final_item:BundleItem = base_item[1][-1]
                        final_item.extract_to_file(out_path)
                        extracted_any = True
                        base_extracted = True

                if base_extracted or base_from_same_mod:
                    for _, inner, item_list in buffer_items:
                        out_path = os.path.join(extract_root, inner)
                        if not prepare_extraction_target(out_path, extract_root):
                            continue
                        final_item:BundleItem = item_list[-1]
                        final_item.extract_to_file(out_path)
                        extracted_any = True

                mod_ready = False
                if base_from_same_mod and base_exists:
                    mod_ready = True
                if extracted_any and (base_extracted or base_from_same_mod):
                    set_source_for_path(extract_root, filepath, mod_label)
                    mod_ready = True

                if mod_ready:
                    return abs_filename
                if _overwrite_existing:
                    return abs_filename

        if not os.path.exists(win_safe_path(abs_filename)) or _overwrite_existing: #and os.path.isfile(abs_filename):
            if _is_readonly_target(abs_filename):
                return abs_filename
            log.info("Extracting %s", filepath)
            bundle_manager = LoadBundleManager()
            items = _collect_bundle_extract_items(bundle_manager, filepath)
            if items:
                extracted_any = False
                for rel_path, item in items:
                    final_item:BundleItem = item[-1]
                    out_path = os.path.join(extract_root, rel_path)
                    if not prepare_extraction_target(out_path, extract_root):
                        continue
                    final_item.extract_to_file(out_path)
                    extracted_any = True
                if extracted_any:
                    set_source_for_path(extract_root, filepath, "vanilla")
            else:
                repaired_filepath = ""
                if version > 115 and filepath.endswith('.w2mesh'):
                    repaired_filepath = _repair_missing_w3_mesh_repo_path(bundle_manager, extract_root, filepath)
                    if repaired_filepath:
                        log.info("Repairing missing Witcher 3 mesh path: %s -> %s", filepath, repaired_filepath)
                        filepath = repaired_filepath
                        filepath_key = filepath.lower()
                        abs_filename = os.path.join(extract_root, filepath)
                        items = _collect_bundle_extract_items(bundle_manager, filepath)
                if items:
                    extracted_any = False
                    for rel_path, item in items:
                        final_item:BundleItem = item[-1]
                        out_path = os.path.join(extract_root, rel_path)
                        if not prepare_extraction_target(out_path, extract_root):
                            continue
                        final_item.extract_to_file(out_path)
                        extracted_any = True
                    if extracted_any:
                        set_source_for_path(extract_root, filepath, "vanilla")
                else:
                    if "." not in os.path.basename(abs_filename) and not _is_readonly_target(abs_filename):
                        os.makedirs(abs_filename)
                    elif mod_entry and not _mod_priority_high:
                        # Fallback to mod if vanilla missing and mods are low priority
                        if not os.path.exists(win_safe_path(abs_filename)) or _overwrite_existing:
                            if _is_readonly_target(abs_filename):
                                return abs_filename
                            mod_label = f"mod:{mod_entry['mod']}"
                            base_item = None
                            buffer_items = _collect_buffer_sidecar_entries(mod_entry["items"], filepath)
                            for inner, item_list in mod_entry["items"]:
                                inner_norm = _normalize_mod_inner_path(inner)
                                if inner_norm.lower() == filepath_key:
                                    base_item = (inner_norm, item_list)

                            extracted_any = False
                            base_extracted = False
                            if base_item:
                                out_path = os.path.join(extract_root, base_item[0])
                                if prepare_extraction_target(out_path, extract_root):
                                    final_item:BundleItem = base_item[1][-1]
                                    final_item.extract_to_file(out_path)
                                    extracted_any = True
                                    base_extracted = True

                            if base_extracted:
                                for _, inner, item_list in buffer_items:
                                    out_path = os.path.join(extract_root, inner)
                                    if not prepare_extraction_target(out_path, extract_root):
                                        continue
                                    final_item:BundleItem = item_list[-1]
                                    final_item.extract_to_file(out_path)
                                    extracted_any = True

                            if extracted_any and base_extracted:
                                set_source_for_path(extract_root, filepath, mod_label)
        return abs_filename

def extract_missing_buffers(abs_w2anims_path: str, required_index: int | None = None) -> set[int]:
    """Extract any missing .N.buffer sidecar files for a .w2anims/.w2mesh.

    Called on-demand by dc_anims when a specific buffer is missing, NOT on
    every repo_file call. If required_index is provided, only that sidecar is
    probed and extracted; otherwise all missing sidecars are extracted in one
    pass so the bundle manager is only loaded once.
    """
    extracted = set()
    _, uncook_path, _, _ = _get_repo_roots_from_prefs()
    if not uncook_path:
        return extracted
    norm_file = os.path.normcase(os.path.normpath(abs_w2anims_path))
    norm_uncook = os.path.normcase(os.path.normpath(uncook_path))
    if not norm_file.startswith(norm_uncook):
        return extracted
    rel_path = os.path.relpath(abs_w2anims_path, uncook_path)
    bundle_manager = LoadBundleManager()

    if required_index is not None:
        buf_rel = f"{rel_path}.{required_index}.buffer"
        buf_abs = f"{abs_w2anims_path}.{required_index}.buffer"
        if os.path.exists(win_safe_path(buf_abs)):
            return extracted
        buf_item = bundle_manager.find_item_by_hash(buf_rel)
        if buf_item:
            final_item: BundleItem = buf_item[-1]
            out_path = os.path.join(uncook_path, buf_rel)
            if prepare_extraction_target(out_path, uncook_path):
                final_item.extract_to_file(out_path)
                extracted.add(required_index)
                log.info("Extracted missing buffer: %s", buf_rel)
        return extracted

    for buf_idx, buf_rel, buf_item in _collect_buffer_sidecar_entries(bundle_manager.Items.items(), rel_path):
        buf_abs = f"{abs_w2anims_path}.{buf_idx}.buffer"
        if os.path.exists(win_safe_path(buf_abs)):
            continue
        final_item: BundleItem = buf_item[-1]
        out_path = os.path.join(uncook_path, buf_rel)
        if prepare_extraction_target(out_path, uncook_path):
            final_item.extract_to_file(out_path)
            extracted.add(buf_idx)
            log.info("Extracted missing buffer: %s", buf_rel)
    return extracted

def get_game_path():
    prefs = _get_addon_prefs()
    if prefs:
        return prefs.witcher_game_path
    return get_dev_override("fallback_game_path", "")


def repo_collision_file(mesh_filepath: str) -> str:
    """
    Find and extract the collision file (.nxs) associated with a mesh.

    Given a mesh filepath (e.g., "items\\weapons\\sword.w2mesh"), this function
    will search the collision cache for the corresponding .nxs file and extract
    it if not already present.

    Args:
        mesh_filepath: Path to the mesh file (with or without extension)

    Returns:
        Absolute path to the extracted .nxs file, or None if not found
    """
    from .witcher_cache.CollisionCache.CollisionManager import CollisionManager

    _, uncook_path, _, _ = _get_repo_roots_from_prefs()
    if not uncook_path:
        return None

    # Generate collision path from mesh path
    # The collision file typically has the same path but with .nxs extension
    if mesh_filepath.endswith('.w2mesh'):
        collision_path = mesh_filepath[:-8]  # Remove .w2mesh
    else:
        collision_path = mesh_filepath

    # Collision files in cache are stored without extension
    # Try to find it in the collision manager
    collision_manager = CollisionManager.Get()

    # Search for matching collision file
    item = None

    # Try exact match first (collision files may be stored with full path)
    items = collision_manager.find_item_by_path_name(collision_path)
    if items and len(items) > 0:
        item = items[0]
    else:
        # Try with .nxs extension
        items = collision_manager.find_item_by_path_name(collision_path + ".nxs")
        if items and len(items) > 0:
            item = items[0]

    if item is None:
        # Try to find by partial path match (in case of path format differences)
        normalized_path = collision_path.replace('/', '\\').lower()
        for key in collision_manager.Items:
            if normalized_path in key.lower():
                items = collision_manager.Items[key]
                if items and len(items) > 0:
                    item = items[0]
                    break

    if item is None:
        return None

    # Determine output path
    output_path = os.path.join(uncook_path, item.Name)
    if not output_path.endswith('.nxs'):
        output_path = output_path + '.nxs'

    # Extract if not already present
    if not os.path.exists(output_path):
        try:
            extracted_path = item.extract_to_file(output_path)
            log.info("Extracted collision file: %s", extracted_path)
            return extracted_path
        except Exception as e:
            log.error("Failed to extract collision file: %s", e)
            return None

    return output_path


def repo_collision_file_with_poses(mesh_filepath: str):
    """Like repo_collision_file but also returns per-shape data from the RED header.

    Returns:
        tuple: (path, shape_items) where path is the extracted .nxs path (or None) and
               shape_items is a list of (matrix_4x4_or_None, flag, payload_bytes, material_name)
               tuples from CollisionCacheItem.get_shapes_with_data().
               matrix_4x4 is row-major [[X.x,X.y,X.z,X.w], [Y...], [Z...], [T...]]
               matching the format expected by _setup_collision_object in import_nxs.py.
    """
    from .witcher_cache.CollisionCache.CollisionManager import CollisionManager

    _, uncook_path, _, _ = _get_repo_roots_from_prefs()
    if not uncook_path:
        return None, []

    if mesh_filepath.endswith('.w2mesh'):
        collision_path = mesh_filepath[:-8]
    else:
        collision_path = mesh_filepath

    collision_manager = CollisionManager.Get()
    item = None

    items = collision_manager.find_item_by_path_name(collision_path)
    if items and len(items) > 0:
        item = items[0]
    else:
        items = collision_manager.find_item_by_path_name(collision_path + ".nxs")
        if items and len(items) > 0:
            item = items[0]

    if item is None:
        normalized_path = collision_path.replace('/', '\\').lower()
        for key in collision_manager.Items:
            if normalized_path in key.lower():
                items = collision_manager.Items[key]
                if items and len(items) > 0:
                    item = items[0]
                    break

    if item is None:
        return None, []

    shape_items = item.get_shapes_with_data()

    output_path = os.path.join(uncook_path, item.Name)
    if not output_path.endswith('.nxs'):
        output_path = output_path + '.nxs'

    if not os.path.exists(output_path):
        try:
            extracted_path = item.extract_to_file(output_path)
            log.info("Extracted collision file: %s", extracted_path)
            return extracted_path, shape_items
        except Exception as e:
            log.error("Failed to extract collision file: %s", e)
            return None, []

    return output_path, shape_items


def get_collision_for_mesh(mesh_filepath: str) -> str:
    """
    Convenience function to get collision file path for a mesh.

    This is a simpler wrapper that handles common path transformations.

    Args:
        mesh_filepath: Absolute or relative path to a mesh file

    Returns:
        Path to extracted .nxs collision file, or None if not found
    """
    _, uncook_path, _, _ = _get_repo_roots_from_prefs()
    if os.path.isabs(mesh_filepath) and not uncook_path:
        return None

    # If absolute path, convert to relative
    if os.path.isabs(mesh_filepath):
        if uncook_path and uncook_path in mesh_filepath:
            mesh_filepath = mesh_filepath.replace(uncook_path + '\\', '')
            mesh_filepath = mesh_filepath.replace(uncook_path + '/', '')

    return repo_collision_file(mesh_filepath)


def get_collision_for_mesh_with_poses(mesh_filepath: str):
    """Like get_collision_for_mesh but also returns per-shape pose matrices.

    Returns:
        tuple: (path, poses) — path is the extracted .nxs file path (or None),
               poses is a list of (matrix_4x4, flag) from CollisionCacheItem.get_shape_poses().
    """
    _, uncook_path, _, _ = _get_repo_roots_from_prefs()
    if os.path.isabs(mesh_filepath) and not uncook_path:
        return None, []

    if os.path.isabs(mesh_filepath):
        if uncook_path and uncook_path in mesh_filepath:
            mesh_filepath = mesh_filepath.replace(uncook_path + '\\', '')
            mesh_filepath = mesh_filepath.replace(uncook_path + '/', '')

    return repo_collision_file_with_poses(mesh_filepath)
