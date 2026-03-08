"""
Development Configuration for Witcher Blender Tools
====================================================
Thin loader that reads machine-specific settings from dev_config.json.

On first run, if dev_config.json does not exist it is copied from
dev_config.example.json so the developer has a ready-made template
to fill in with local paths.

When the addon is deployed, this entire 'dev' folder is excluded,
so the addon works normally without any test overrides.
"""

import json
import shutil
from pathlib import Path

_CONFIG_DIR = Path(__file__).parent
_CONFIG_PATH = _CONFIG_DIR / "dev_config.json"
_EXAMPLE_PATH = _CONFIG_DIR / "dev_config.example.json"


def _ensure_config_exists():
    """Create dev_config.json from the example template on first run."""
    if _CONFIG_PATH.exists():
        return
    if _EXAMPLE_PATH.exists():
        shutil.copy2(_EXAMPLE_PATH, _CONFIG_PATH)


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
