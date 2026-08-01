"""
Witcher Blender Tools - Development Module
============================================
This module is excluded from production builds.
It provides dev testing features like test path management.
"""

from ..dev import ui_dev_panel, ui_dev_logging, ui_dev_timers
from ..extension_paths import is_dev_mode_active

_DEV_FEATURES_REGISTERED = False


def register():
    """Register all dev-specific features."""
    global _DEV_FEATURES_REGISTERED
    if _DEV_FEATURES_REGISTERED:
        return
    if not is_dev_mode_active():
        return
    ui_dev_panel.register()
    ui_dev_logging.register()
    ui_dev_timers.register()
    _DEV_FEATURES_REGISTERED = True


def unregister():
    """Unregister all dev-specific features."""
    global _DEV_FEATURES_REGISTERED
    if not _DEV_FEATURES_REGISTERED:
        return
    ui_dev_timers.unregister()
    ui_dev_logging.unregister()
    ui_dev_panel.unregister()
    _DEV_FEATURES_REGISTERED = False
