"""
Development Configuration for Witcher Blender Tools
====================================================
Thin loader that reads machine-specific settings from dev_config.json.

Inside Blender the config lives in Blender's writable per-extension storage,
outside the replaceable extension package. On first run in a new extension
repository, the newest config for this extension ID is migrated; an existing
package-local config is the legacy fallback. Otherwise dev_config.example.json
is copied so the developer has a ready-made template to fill in with local paths.

When a production package omits this folder, the add-on works normally without
any test overrides.
"""

import json
import shutil
from pathlib import Path

_PACKAGE_CONFIG_DIR = Path(__file__).parent
_PACKAGE_CONFIG_PATH = _PACKAGE_CONFIG_DIR / "dev_config.json"
_EXAMPLE_PATH = _PACKAGE_CONFIG_DIR / "dev_config.example.json"


def _get_extension_config_path():
    """Return Blender's writable per-extension config path when available."""
    try:
        import bpy  # noqa: F401
        from ..extension_paths import get_extension_user_dir
        extension_dir = get_extension_user_dir(create=True)
    except Exception:
        return None
    if not extension_dir:
        return None
    return Path(extension_dir) / "dev" / "dev_config.json"


_CONFIG_PATH = _get_extension_config_path() or _PACKAGE_CONFIG_PATH


def get_config_path():
    """Return the active machine-local development config path."""
    return _CONFIG_PATH


def _iter_previous_extension_configs():
    """Yield configs for this extension ID stored under other repositories."""
    if _CONFIG_PATH == _PACKAGE_CONFIG_PATH:
        return

    # .../extensions/.user/<repository>/<extension_id>/dev/dev_config.json
    try:
        extension_id_dir = _CONFIG_PATH.parents[1]
        current_repo_dir = extension_id_dir.parent
        extension_user_root = current_repo_dir.parent
    except IndexError:
        return
    if extension_user_root.name != ".user":
        return

    candidates = []
    try:
        repo_dirs = extension_user_root.iterdir()
    except OSError:
        return
    for repo_dir in repo_dirs:
        if repo_dir == current_repo_dir or not repo_dir.is_dir():
            continue
        candidate = repo_dir / extension_id_dir.name / "dev" / "dev_config.json"
        try:
            if candidate.is_file():
                candidates.append((candidate.stat().st_mtime, candidate))
        except OSError:
            continue
    for _mtime, candidate in sorted(candidates, reverse=True):
        yield candidate


def _ensure_config_exists():
    """Migrate a local config, or create one from the example template."""
    if _CONFIG_PATH.exists():
        return
    migration_sources = (
        *_iter_previous_extension_configs(),
        _PACKAGE_CONFIG_PATH,
        _EXAMPLE_PATH,
    )
    for source_path in migration_sources:
        if source_path == _CONFIG_PATH or not source_path.exists():
            continue
        try:
            _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, _CONFIG_PATH)
            return
        except OSError:
            continue


def _load_config():
    """Load and return the full JSON config dict."""
    _ensure_config_exists()
    if not _CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return False


_config = _load_config()

# Public attributes consumed by __init__.py and extension_paths.py.
DEV_MODE_ENABLED = _as_bool(_config.get("dev_mode_enabled", False))
ADDON_PREFS_DEFAULTS = _config.get("addon_prefs_defaults", {})
DEV_PANEL_OVERRIDES = _config.get("dev_panel_overrides", _config.get("runtime_overrides", {}))
# Backward-compatible alias for legacy references.
RUNTIME_OVERRIDES = DEV_PANEL_OVERRIDES
ADDON_PREFS_REDKIT_PROJECTS = _config.get("redkit_projects", [])
ADDON_PREFS_UNREAL_PROJECTS = _config.get("unreal_projects", [])
