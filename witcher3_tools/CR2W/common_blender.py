try:
    import bpy
    from .witcher_cache.Bundles import LoadBundleManager
    from .witcher_cache.Bundles import BundleItem
    from .witcher_cache.Witcher2Bundles import LoadWitcher2BundleManager
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
import threading
from contextlib import contextmanager
from ..extension_paths import get_dev_override, get_w2_uncook_root
from ..repo_paths import (
    coerce_w2_data_root,
    iter_w2_repo_path_variants,
    normalize_roots,
)
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


# Win32 has no single 260-char MAX_PATH limit: CreateDirectoryW (os.makedirs) rejects
# paths of 248+ chars (MAX_PATH minus room for an 8.3 filename) while CreateFileW allows
# up to 259, so a 249-char directory fails even though a 259-char file is legal. Prefix
# anything above 240 so every API is covered with margin. blender.exe is not
# longPathAware, so the LongPathsEnabled registry switch does NOT help inside Blender;
# the \\?\ prefix is the only mechanism that works.
WIN_LONG_PATH_THRESHOLD = 240


def win_safe_path(path: str) -> str:
    """On Windows, apply \\?\\ prefix for paths > WIN_LONG_PATH_THRESHOLD chars to
    bypass MAX_PATH. Safe on all Windows 10/11 machines - no registry changes needed.
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
    if len(abs_p) > WIN_LONG_PATH_THRESHOLD:
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
    if len(abs_p) <= WIN_LONG_PATH_THRESHOLD:
        return path
    if abs_p.startswith('\\\\'):
        # UNC path -> \\?\UNC\server\share\...
        return '\\\\?\\UNC\\' + abs_p.lstrip('\\')
    return '\\\\?\\' + abs_p


def win_extended_path(path: str) -> str:
    """Always-prefixed \\?\\ absolute form, regardless of length. Use for os.walk /
    os.scandir roots: os.walk derives every child path from the root string, so a
    bare root makes scandir fail silently on subdirectories deeper than ~257 chars
    in non-longPathAware processes (blender.exe) and entire subtrees vanish from
    the scan. Returns depot/relative paths and non-Windows paths unchanged."""
    if sys.platform != 'win32' or not path:
        return path
    p = win_unprefix_path(os.fspath(path))
    drive, _ = os.path.splitdrive(p)
    is_unc = p.startswith('\\\\')
    if not drive and not is_unc:
        return path
    abs_p = os.path.abspath(p)
    if abs_p.startswith('\\\\'):
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


def win_short_path(path: str) -> str:
    """Return an existing path's 8.3 short form on Windows when available."""
    if sys.platform != 'win32' or not path:
        return path
    try:
        import ctypes
        from ctypes import wintypes

        source_path = win_safe_path(os.fspath(path))
        get_short = ctypes.windll.kernel32.GetShortPathNameW
        get_short.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        get_short.restype = wintypes.DWORD

        size = get_short(source_path, None, 0)
        if not size:
            return win_unprefix_path(path)
        buffer = ctypes.create_unicode_buffer(size + 1)
        result = get_short(source_path, buffer, size + 1)
        if not result:
            return win_unprefix_path(path)
        return win_unprefix_path(buffer.value)
    except Exception:
        return win_unprefix_path(path)


def win_explorer_path(path: str) -> str:
    """Return a path form suitable for Windows Explorer shell commands."""
    if sys.platform != 'win32' or not path:
        return path
    path = win_unprefix_path(path)
    short_path = win_short_path(path)
    return short_path or path


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
_repo_context_state = threading.local()
_w2_bundle_reset_attempted_paths = set()
_w2_auto_detect_game_path_ready = False
_w2_auto_detect_game_path = ""
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


def _repo_context_stacks():
    redkit_stack = getattr(_repo_context_state, "redkit_stack", None)
    if redkit_stack is None:
        redkit_stack = []
        _repo_context_state.redkit_stack = redkit_stack
    w2_stack = getattr(_repo_context_state, "w2_stack", None)
    if w2_stack is None:
        w2_stack = []
        _repo_context_state.w2_stack = w2_stack
    return redkit_stack, w2_stack

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

def _redkit_roots_for_path(path):
    if not path or not os.path.isabs(str(path)):
        return []
    roots = _get_redkit_depot_roots()
    return roots if any(_is_under_root(str(path), root) for root in roots) else []


def _w2_repo_context_for_source(source_path=None, roots=None):
    unbundle_root, game_data_root = _get_w2_repo_roots_from_prefs()
    values = []
    if source_path:
        values.append(source_path)
    values.extend(roots or [])
    for value in values:
        if not value:
            continue
        try:
            candidate = os.path.normpath(str(value))
        except Exception:
            continue
        if not os.path.isabs(candidate):
            continue
        if game_data_root and _is_under_root(candidate, game_data_root):
            return {"kind": "redkit_data", "root": game_data_root}
        if unbundle_root and _is_under_root(candidate, unbundle_root):
            return {"kind": "extracted", "root": unbundle_root}
    return None


@contextmanager
def redkit_repo_context(source_path=None, roots=None):
    """Temporarily enable REDkit dual-depot lookup for children of a REDkit source."""
    context_roots = list(roots or _redkit_roots_for_path(source_path))
    w2_context = _w2_repo_context_for_source(source_path, roots=roots)
    redkit_stack, w2_stack = _repo_context_stacks()
    redkit_stack.append(context_roots)
    w2_stack.append(w2_context)
    try:
        yield
    finally:
        redkit_stack.pop()
        w2_stack.pop()

@contextmanager
def vanilla_only_repo_context():
    """Temporarily suppress REDkit roots so repo_file resolves only via vanilla
    bundles/uncook. Used to load the cooked vanilla copy of an asset when its
    REDkit-uncooked sibling is missing data that only exists post-cook (e.g.
    CAnimAnimsetsParam baked into entity templates)."""
    redkit_stack, w2_stack = _repo_context_stacks()
    saved = list(redkit_stack)
    saved_w2 = list(w2_stack)
    redkit_stack.clear()
    w2_stack.clear()
    try:
        yield
    finally:
        redkit_stack.clear()
        redkit_stack.extend(saved)
        w2_stack.clear()
        w2_stack.extend(saved_w2)

def _active_redkit_repo_roots():
    redkit_stack, _w2_stack = _repo_context_stacks()
    for roots in reversed(redkit_stack):
        if roots:
            return roots
    return []


def _active_w2_repo_context():
    _redkit_stack, w2_stack = _repo_context_stacks()
    for context in reversed(w2_stack):
        if context:
            return context
    return None

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


def get_repo_resolution_context(source_path=None):
    redkit_roots = list(_active_redkit_repo_roots() or _redkit_roots_for_path(source_path))
    w2_context = _active_w2_repo_context() or _w2_repo_context_for_source(source_path)
    try:
        pref_roots = _get_repo_roots_from_prefs()
    except Exception:
        pref_roots = ("", "", "", False)
    normalized_pref_roots = tuple(
        bool(value)
        if isinstance(value, bool)
        else os.path.normcase(os.path.normpath(str(value))) if value else ""
        for value in pref_roots
    )
    return (
        tuple(os.path.normcase(os.path.normpath(root)) for root in _repo_override_roots),
        bool(_repo_override_read_only),
        tuple(os.path.normcase(os.path.normpath(root)) for root in redkit_roots),
        tuple(sorted((str(key), str(value)) for key, value in (w2_context or {}).items())),
        normalized_pref_roots,
        (bool(_mod_priority_enabled), bool(_mod_priority_high), bool(_overwrite_existing)),
    )


@contextmanager
def mod_loading_context(context=None, prefer_mods=None, overwrite=None):
    """Context manager that configures mod loading for all repo_file calls within the block.

    Reads settings from the Blender scene (witcher_file_browser) when context is provided
    and no explicit overrides are given.  Automatically clears globals on exit.

    Usage:
        with mod_loading_context(context):
            import_entity.import_entity_file(path, ...)
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
        path_key = os.path.normcase(os.path.normpath(path))
        root_key = os.path.normcase(os.path.normpath(root))
        return os.path.commonpath([path_key, root_key]) == root_key
    except Exception:
        return False

def _is_configured_w2_extract_target(path):
    prefs = _get_addon_prefs()
    if not prefs:
        return False
    w2_unbundle_root = str(getattr(prefs, "w2_unbundle_path", "") or "").strip()
    return bool(w2_unbundle_root and _is_under_root(path, w2_unbundle_root))

def _is_readonly_target(path):
    if _is_configured_w2_extract_target(path):
        return False
    return _repo_override_read_only and any(_is_under_root(path, root) for root in _repo_override_roots)

def _get_source_map_path(uncook_path: str) -> str:
    return os.path.join(uncook_path, "_witcher_tools_sources.json")

def _load_source_map(uncook_path: str) -> dict:
    global _source_map_cache
    map_path = _get_source_map_path(uncook_path)
    # Pending writes make the in-memory map authoritative.
    if _source_map_dirty and _source_map_cache["path"] == map_path:
        return _source_map_cache["data"]
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

def _save_source_map(uncook_path: str, data: dict) -> bool:
    map_path = _get_source_map_path(uncook_path)
    temp_path = map_path + ".tmp"
    try:
        os.makedirs(os.path.dirname(map_path), exist_ok=True)
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"), sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, map_path)
        _source_map_cache["path"] = map_path
        _source_map_cache["data"] = data
        _source_map_cache["mtime"] = os.path.getmtime(map_path)
        return True
    except Exception:
        log.exception("Failed to save source map %s", map_path)
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception:
            pass
        return False

_source_map_dirty = False
_source_map_flush_scheduled = False
_SOURCE_MAP_FLUSH_DELAY = 2.0

def flush_source_map():
    """Write pending source-map entries to disk. Safe to call any time."""
    global _source_map_dirty, _source_map_flush_scheduled
    _source_map_flush_scheduled = False
    if not _source_map_dirty:
        return None
    map_path = str(_source_map_cache.get("path") or "")
    if map_path and _save_source_map(os.path.dirname(map_path), _source_map_cache.get("data") or {}):
        _source_map_dirty = False
        return None
    # Retain dirty state when deferred flush fails.
    _source_map_flush_scheduled = True
    return _SOURCE_MAP_FLUSH_DELAY

def _schedule_source_map_flush() -> None:
    global _source_map_flush_scheduled
    if _source_map_flush_scheduled:
        return
    try:
        import bpy
        if not bpy.app.background and bpy.app.timers is not None:
            bpy.app.timers.register(flush_source_map, first_interval=_SOURCE_MAP_FLUSH_DELAY)
            _source_map_flush_scheduled = True
            return
    except Exception:
        pass
    import atexit
    atexit.register(flush_source_map)
    _source_map_flush_scheduled = True

def set_source_for_path(uncook_path: str, rel_path: str, source_label: str) -> None:
    global _source_map_dirty
    map_path = _get_source_map_path(uncook_path)
    if _source_map_dirty and _source_map_cache["path"] != map_path:
        if flush_source_map() is not None:
            raise OSError(f"Could not flush source map {_source_map_cache['path']}")
    data = _load_source_map(uncook_path)
    if data.get(rel_path) == source_label:
        return
    data[rel_path] = source_label
    _source_map_cache["path"] = map_path
    _source_map_cache["data"] = data
    # Debounce large source-map writes.
    _source_map_dirty = True
    _schedule_source_map_flush()

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
    if filepath.endswith((".w2mesh", ".w2anims", ".reddest")):
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
        mods_dirs = cache_meta.get_all_mod_dirs(Configuration.ExecutablePath)
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
    ".texarray",
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
        uncook_path = str(getattr(prefs, "w2_unbundle_path", "") or "").strip()
        texture_path = uncook_path

    return fbx_uncook_path, uncook_path, texture_path, use_separate_texture_path


def _get_w2_repo_roots_from_prefs():
    prefs = _get_addon_prefs()
    if not prefs:
        return "", ""
    w2_uncook = str(getattr(prefs, "w2_unbundle_path", "") or "").strip()
    w2_game = str(getattr(prefs, "witcher2_game_path", "") or "").strip()
    w2_game_data = coerce_w2_data_root(w2_game)
    return (
        os.path.normpath(w2_uncook) if w2_uncook else "",
        os.path.normpath(w2_game_data) if w2_game_data else "",
    )


def _find_w2_existing_repo_file(root: str, filepath: str, skip_path: str = "") -> str:
    if not root:
        return ""
    skip_key = os.path.normcase(os.path.normpath(skip_path)) if skip_path else ""
    for rel_path in iter_w2_repo_path_variants(filepath):
        candidate = os.path.join(root, rel_path)
        candidate_key = os.path.normcase(os.path.normpath(candidate))
        if skip_key and candidate_key == skip_key:
            continue
        if os.path.exists(win_safe_path(candidate)):
            return candidate
    return ""


def _load_w2_bundle_manager(*, game_path: str = "", reset_cache: bool = False):
    try:
        from .witcher_cache.Witcher2Bundles import LoadWitcher2BundleManager as _load_manager
    except Exception:
        log.debug("Failed to import Witcher 2 DZIP manager", exc_info=True)
        return None
    try:
        return _load_manager(reset_cache=reset_cache, game_path=game_path)
    except Exception:
        log.debug("Failed to load Witcher 2 DZIP manager for %s", game_path or "<configured>", exc_info=True)
        return None


def _current_w2_bundle_manager():
    try:
        from .witcher_cache.Witcher2Bundles.DzipManager import DzipManager

        return DzipManager.InstanceManager
    except Exception:
        return None


def _configured_w2_game_path_for_bundles():
    prefs = _get_addon_prefs()
    if not prefs:
        return ""
    return str(getattr(prefs, "witcher2_game_path", "") or "").strip()


def _is_valid_w2_game_path_for_bundles(game_path: str) -> bool:
    return bool(game_path and os.path.isdir(os.path.join(game_path, "CookedPC")))


def _iter_w2_bundle_game_path_candidates(current_base: str = ""):
    seen = set()

    def emit(path):
        path = str(path or "").strip()
        if not _is_valid_w2_game_path_for_bundles(path):
            return None
        key = os.path.normcase(os.path.normpath(path))
        if key in seen:
            return None
        seen.add(key)
        return os.path.normpath(path)

    for candidate in (
        current_base,
        _configured_w2_game_path_for_bundles(),
        _auto_detect_w2_game_path_for_bundles(),
    ):
        candidate = emit(candidate)
        if candidate:
            yield candidate


def _find_w2_bundle_items_in_manager(manager, filepath: str):
    if manager is None:
        return None
    for rel_path in iter_w2_repo_path_variants(filepath):
        items = manager.find_item_by_hash(rel_path)
        if items:
            return items
    return None


def _auto_detect_w2_game_path_for_bundles():
    global _w2_auto_detect_game_path_ready, _w2_auto_detect_game_path
    if _w2_auto_detect_game_path_ready:
        return _w2_auto_detect_game_path
    try:
        from ..read_game_bin import auto_detect_witcher2_game_path

        _w2_auto_detect_game_path = auto_detect_witcher2_game_path()
    except Exception:
        _w2_auto_detect_game_path = ""
    _w2_auto_detect_game_path_ready = True
    return _w2_auto_detect_game_path


def _find_w2_bundle_items(filepath: str):
    current_manager = _current_w2_bundle_manager()
    current_base = str(getattr(current_manager, "base_path", "") or "")
    candidate_paths = list(_iter_w2_bundle_game_path_candidates(current_base))
    if not candidate_paths:
        if current_manager is None:
            current_manager = _load_w2_bundle_manager()
            current_base = str(getattr(current_manager, "base_path", "") or "")
        items = _find_w2_bundle_items_in_manager(current_manager, filepath)
        if items:
            return items
        log.debug(
            "Witcher 2 DZIP item not found: %s (configured game path: %s, detected game path: %s)",
            filepath,
            current_base or "<unset>",
            "<none>",
        )
        return None

    managers = []
    for game_path in candidate_paths:
        manager = _load_w2_bundle_manager(game_path=game_path)
        managers.append(manager)
        items = _find_w2_bundle_items_in_manager(manager, filepath)
        if items:
            return items

        base_key = os.path.normcase(os.path.normpath(game_path))
        item_count = len(getattr(manager, "Items", {}) or {}) if manager is not None else 0
        if not item_count and base_key not in _w2_bundle_reset_attempted_paths:
            _w2_bundle_reset_attempted_paths.add(base_key)
            reset_manager = _load_w2_bundle_manager(game_path=game_path, reset_cache=True)
            managers[-1] = reset_manager
            items = _find_w2_bundle_items_in_manager(reset_manager, filepath)
            if items:
                log.warning("Witcher 2 DZIP cache rebuilt to resolve: %s", filepath)
                return items

    log.debug(
        "Witcher 2 DZIP item not found: %s (configured game path: %s, detected game path: %s)",
        filepath,
        _configured_w2_game_path_for_bundles() or "<unset>",
        _auto_detect_w2_game_path_for_bundles() or "<none>",
    )
    return None


def _extract_w2_bundle_repo_file(filepath: str, extract_root: str) -> str:
    if not extract_root:
        return ""
    abs_filename = os.path.join(extract_root, filepath)
    if _is_readonly_target(abs_filename):
        return ""
    items = _find_w2_bundle_items(filepath)
    if not items:
        return ""
    final_item = items[-1] if isinstance(items, list) else items
    item_name = getattr(final_item, "name", "") or filepath
    out_rel_path = item_name.replace("/", "\\")
    out_path = os.path.join(extract_root, out_rel_path)
    try:
        target_ready = prepare_extraction_target(out_path, extract_root)
    except Exception:
        log.warning("Failed to prepare Witcher 2 extraction target: %s", out_path, exc_info=True)
        return ""
    if target_ready:
        try:
            final_item.extract_to_file(out_path)
        except Exception:
            log.warning("Failed to extract Witcher 2 bundle item %s to %s", item_name, out_path, exc_info=True)
            return ""
        set_source_for_path(extract_root, out_rel_path, "witcher2")
        if os.path.exists(win_safe_path(out_path)):
            return out_path
    return ""


def _w2_cache_extract_root() -> str:
    try:
        return os.path.normpath(get_w2_uncook_root(create=True))
    except Exception:
        log.debug("Failed to get Witcher 2 fallback extraction cache", exc_info=True)
        return ""


def _warn_w2_cache_fallback(filepath: str, resolved_path: str) -> None:
    log.warning(
        "WITCHER 2 CACHE FALLBACK: using extension cache for '%s' after the configured W2 uncook root did not produce a file: %s",
        filepath,
        resolved_path,
    )


def _find_w2_existing_in_roots(filepath: str, roots, *, skip_path: str = "") -> str:
    for root in normalize_roots(roots):
        candidate = _find_w2_existing_repo_file(root, filepath, skip_path=skip_path)
        if candidate:
            return candidate
    return ""


def _extract_w2_bundle_to_roots(filepath: str, roots, *, primary_root: str = "") -> str:
    primary_key = os.path.normcase(os.path.normpath(primary_root)) if primary_root else ""
    for root in normalize_roots(roots):
        extracted_candidate = _extract_w2_bundle_repo_file(filepath, root)
        if not extracted_candidate:
            continue
        root_key = os.path.normcase(os.path.normpath(root))
        if primary_key and root_key != primary_key:
            _warn_w2_cache_fallback(filepath, extracted_candidate)
        return extracted_candidate
    return ""


def _warn_w2_redkit_fallback(filepath: str, resolved_path: str) -> None:
    log.debug(
        "WITCHER 2 REDKIT RESOLVE: loading '%s' from REDkit: %s",
        filepath,
        resolved_path,
    )


def _resolve_w2_repo_file(filepath: str, extract_root: str, abs_filename: str) -> str:
    unbundle_root, game_data_root = _get_w2_repo_roots_from_prefs()
    context = _active_w2_repo_context() or {}
    context_kind = context.get("kind", "")
    context_root = context.get("root", "")

    extracted_roots = [extract_root, unbundle_root]
    if context_kind == "extracted":
        extracted_roots.insert(0, context_root)
    extracted_roots = normalize_roots(extracted_roots)

    if not _overwrite_existing:
        extracted_candidate = _find_w2_existing_in_roots(filepath, extracted_roots)
        if extracted_candidate:
            return extracted_candidate

    extracted_candidate = _extract_w2_bundle_to_roots(
        filepath,
        [extract_root],
        primary_root=extract_root,
    )
    if extracted_candidate:
        return extracted_candidate

    if _overwrite_existing:
        extracted_candidate = _extract_w2_bundle_to_roots(
            filepath,
            [extract_root],
            primary_root=extract_root,
        )
        if extracted_candidate:
            return extracted_candidate

    game_roots = []
    if context_kind == "redkit_data":
        game_roots.append(context_root)
    game_roots.append(game_data_root)
    game_roots = normalize_roots(game_roots, existing_only=True)
    for root in game_roots:
        game_data_candidate = _find_w2_existing_repo_file(root, filepath, skip_path=abs_filename)
        if game_data_candidate and not _overwrite_existing:
            _warn_w2_redkit_fallback(filepath, game_data_candidate)
            return game_data_candidate

    return abs_filename


def _get_redkit_depot_roots():
    """Return the configured REDkit dual-depot roots in priority order.

    REDkit splits its source files across two depots:
      - redkit_depot_path     : the `r4data`-style project depot (.w2ent, scripts,
                                 source-side definitions). Highest priority.
      - redkit_uncooked_path  : the generated binary depot (.w2mesh, .xbm, etc).
                                 Mesh/binary asset depot.
    """
    prefs = _get_addon_prefs()
    if not prefs:
        return []
    roots = []
    for attr in ("redkit_depot_path", "redkit_uncooked_path"):
        value = str(getattr(prefs, attr, "") or "").strip()
        if value:
            roots.append(os.path.normpath(value))
    return roots


def _resolve_redkit_repo_file(filepath: str, roots) -> str:
    relpath = str(filepath or "").replace("/", "\\")
    if os.path.isabs(relpath):
        for root in roots:
            if _is_under_root(relpath, root):
                if os.path.exists(win_safe_path(relpath)):
                    return relpath
                relpath = os.path.relpath(relpath, root)
                break
        else:
            return ""
    relpath = relpath.lstrip("\\")

    seen = set()
    for _ in range(11):  # Initial path plus ten redirected targets.
        key = os.path.normcase(os.path.normpath(relpath))
        if key in seen:
            log.warning("REDkit resource link cycle detected at '%s'.", relpath)
            return ""
        seen.add(key)

        candidates = []
        for root in roots:
            candidate = os.path.normpath(os.path.join(root, relpath))
            if not _is_under_root(candidate, root):
                continue
            if os.path.exists(win_safe_path(candidate)):
                return candidate
            candidates.append(candidate)

        target = ""
        for candidate in candidates:
            link_path = candidate + ".link"
            if not os.path.isfile(win_safe_path(link_path)):
                continue
            try:
                with open(win_safe_path(link_path), "r", encoding="utf-8-sig") as handle:
                    target = handle.read().strip()
            except OSError as exc:
                log.warning("Could not read REDkit resource link '%s': %s", link_path, exc)
                return ""
            break

        target = target.replace("/", "\\")
        if not target:
            return ""
        drive, _tail = os.path.splitdrive(target)
        target = os.path.normpath(target)
        if drive or target.startswith("\\") or target == ".." or target.startswith("..\\"):
            log.warning("Ignoring invalid REDkit resource link target '%s'.", target)
            return ""
        relpath = target

    log.warning("REDkit resource link depth exceeded for '%s'.", filepath)
    return ""


def repo_file(filepath: str, version = 999, is_abs_path = False):
    try:
        version = int(version)
    except Exception:
        version = 999

    fbx_uncook_path, uncook_path, texture_path, use_separate_texture_path = _get_repo_roots_from_prefs(version)
    redkit_roots = _active_redkit_repo_roots()

    # REDkit dual-depot resolution is intentionally context-bound. It is active
    # only while resolving dependencies of a source file already loaded from a
    # REDkit depot, so normal bundle/uncook imports are not hijacked by REDkit
    # preference paths.
    if redkit_roots:
        redkit_file = _resolve_redkit_repo_file(filepath, redkit_roots)
        if redkit_file:
            return redkit_file

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
            if version <= 115:
                extract_root = _w2_cache_extract_root()
                if extract_root:
                    abs_filename = os.path.join(extract_root, filepath)
                    return _resolve_w2_repo_file(filepath, extract_root, abs_filename)
            if is_texture_repo and texture_path:
                return os.path.join(texture_path, filepath)
            return filepath
        abs_filename = os.path.join(extract_root, filepath)
        if version <= 115:
            return _resolve_w2_repo_file(filepath, extract_root, abs_filename)
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
                        os.makedirs(win_safe_path(abs_filename), exist_ok=True)
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
    """Extract missing .N.buffer sidecars from cooked bundles."""
    extracted = set()
    _, uncook_path, _, _ = _get_repo_roots_from_prefs()
    if not uncook_path:
        return extracted

    source_path = win_unprefix_path(abs_w2anims_path)
    uncook_root = win_unprefix_path(uncook_path)
    norm_file = os.path.normcase(os.path.normpath(source_path))
    norm_uncook = os.path.normcase(os.path.normpath(uncook_root))
    if norm_file != norm_uncook and not norm_file.startswith(norm_uncook + os.sep):
        return extracted
    rel_path = os.path.relpath(source_path, uncook_root)
    bundle_manager = LoadBundleManager()

    if required_index is not None:
        buf_rel = f"{rel_path}.{required_index}.buffer"
        buf_abs = f"{source_path}.{required_index}.buffer"
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
        buf_abs = f"{source_path}.{buf_idx}.buffer"
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


def _find_collision_item(collision_manager, lookup_path: str):
    """Find the CollisionCacheItem for a mesh/collision path.

    Collision cache entries are keyed by the full source path, exact-match-first order
    """
    if not lookup_path:
        return None

    low = lookup_path.lower()
    if low.endswith('.w2mesh'):
        base = lookup_path[:-len('.w2mesh')]
    elif low.endswith('.nxs'):
        base = lookup_path[:-len('.nxs')]
    else:
        base = lookup_path

    seen = set()
    for cand in (lookup_path, base + '.w2mesh', base + '.nxs', base):
        if not cand or cand in seen:
            continue
        seen.add(cand)
        items = collision_manager.find_item_by_path_name(cand)
        if items and len(items) > 0:
            return items[0]

    normalized = base.replace('/', '\\').lower()
    if normalized:
        for key in collision_manager.Items:
            key_low = key.lower()
            idx = key_low.find(normalized)
            if idx == -1:
                continue
            rest = key_low[idx + len(normalized):]
            if rest.startswith('.'):
                items = collision_manager.Items[key]
                if items and len(items) > 0:
                    return items[0]
    return None


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

    # Collision cache keys carry the full source path (.w2mesh). Let the shared
    # resolver handle the .w2mesh/.nxs variants and sibling-safe fallback.
    collision_manager = CollisionManager.Get()
    item = _find_collision_item(collision_manager, mesh_filepath)

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

    collision_manager = CollisionManager.Get()
    item = _find_collision_item(collision_manager, mesh_filepath)

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


def get_collision_shape_items_for_file(filepath: str):
    """Return per-shape pose data for a standalone collision/mesh file.
    """
    from .witcher_cache.CollisionCache.CollisionManager import CollisionManager

    _, uncook_path, _, _ = _get_repo_roots_from_prefs()

    rel = filepath
    if os.path.isabs(rel) and uncook_path and uncook_path in rel:
        rel = rel.replace(uncook_path + '\\', '').replace(uncook_path + '/', '')

    collision_manager = CollisionManager.Get()
    item = _find_collision_item(collision_manager, rel)
    if item is None:
        return []

    try:
        return item.get_shapes_with_data()
    except Exception as e:
        log.warning("Failed to read collision shape poses for %s: %s", filepath, e)
        return []
