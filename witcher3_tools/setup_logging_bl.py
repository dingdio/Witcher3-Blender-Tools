"""
Centralized logging configuration for Witcher 3 Blender Tools.

To enable debug logging for a specific module:
1. Find the module in LOG_LEVELS below
2. Change its level from CRITICAL/WARNING to INFO or DEBUG
3. Reload the addon (or restart Blender)

Log Levels (from most to least verbose):
    DEBUG    - Detailed diagnostic information
    INFO     - General operational messages
    WARNING  - Something unexpected but not breaking
    ERROR    - Something failed
    CRITICAL - Severe error, addon may not work
"""
import logging
import collections

ADDON_NAME = __package__ or __name__.split('.')[0]
ADDON_DISPLAY_NAME = ADDON_NAME.rsplit(".", 1)[-1]

def _mod(path: str) -> str:
    return f"{ADDON_NAME}.{path}" if path else ADDON_NAME


# =============================================================================
# CONFIGURE LOG LEVELS HERE
# =============================================================================
# Change any module to logging.DEBUG or logging.INFO to see its log messages.
# Default is CRITICAL (silent) for most modules to reduce console noise.

LOG_LEVELS = {
    # Root logger - controls default for all modules
    _mod(""): logging.WARNING,

    # Animation system
    _mod("importers.import_anims"): logging.WARNING,  # Animation import
    _mod("importers.motion_tools"): logging.WARNING,  # Motion extraction
    _mod("CR2W.dc_anims"): logging.WARNING,           # Animation data parsing
    _mod("ui.ui_anims"): logging.WARNING,         # Animation UI
    _mod("ui.ui_voice"): logging.WARNING,         # Animation UI

    # Mesh/Entity import
    _mod("importers.import_mesh"): logging.WARNING,
    _mod("importers.import_entity"): logging.WARNING,
    _mod("CR2W.dc_mesh"): logging.WARNING,
    _mod("CR2W.dc_entity"): logging.WARNING,
    _mod("ui.ui_entity"): logging.WARNING,         # Animation UI

    # Materials
    _mod("w3_material"): logging.WARNING,
    _mod("importers.import_blender_fun"): logging.WARNING,

    # Scene/Map
    _mod("importers.import_scene"): logging.WARNING,
    _mod("ui.ui_map"): logging.WARNING,

    # Core parsing
    _mod("CR2W.CR2W_types"): logging.WARNING,
    _mod("CR2W.CR2W_file"): logging.WARNING,
    _mod("CR2W.bStream"): logging.WARNING,
}

# Immutable snapshot of the defaults — used by apply_log_levels() to
# always reset to the values as defined above, never to mutated state.
_DEFAULT_LEVELS: dict = dict(LOG_LEVELS)

# =============================================================================
# END CONFIGURATION
# =============================================================================

# Map numeric level → display name
LEVEL_NAMES = {10: 'DEBUG', 20: 'INFO', 30: 'WARNING', 40: 'ERROR', 50: 'CRITICAL'}

# Per-logger message counts: LOG_COUNTS[logger_name][level_name] = int
LOG_COUNTS: dict = collections.defaultdict(lambda: collections.defaultdict(int))


class _LogCountHandler(logging.Handler):
    """Counts emitted log records per logger name and level."""
    def emit(self, record):
        LOG_COUNTS[record.name][record.levelname] += 1


def _display_logger_name(full_name: str) -> str:
    """Show addon-relative names in console output, even under Blender's bl_ext namespace."""
    if not full_name:
        return full_name

    if full_name == ADDON_NAME:
        return ADDON_DISPLAY_NAME

    addon_prefix = f"{ADDON_NAME}."
    if full_name.startswith(addon_prefix):
        return full_name[len(addon_prefix):]

    # Fallbacks for mixed logger naming (legacy/non-extension names during reloads).
    if full_name == ADDON_DISPLAY_NAME:
        return ADDON_DISPLAY_NAME

    display_prefix = f"{ADDON_DISPLAY_NAME}."
    idx = full_name.find(display_prefix)
    if idx >= 0:
        return full_name[idx + len(display_prefix):]

    return full_name


class _AddonConsoleFormatter(logging.Formatter):
    """Formats addon logs with a shortened logger name for readability."""

    def format(self, record):
        original_name = record.name
        record.name = _display_logger_name(original_name)
        try:
            return super().format(record)
        finally:
            record.name = original_name


def get_log_counts() -> dict:
    """Return the live message count dict."""
    return LOG_COUNTS


def reset_log_counts():
    """Clear all accumulated message counts."""
    LOG_COUNTS.clear()


def get_current_level(module_name: str) -> int:
    """Return the effective numeric log level for a module."""
    return logging.getLogger(module_name).level


def apply_log_levels():
    """Reset all loggers to the original default levels (from _DEFAULT_LEVELS)."""
    for name, level in _DEFAULT_LEVELS.items():
        logging.getLogger(name).setLevel(level)
        LOG_LEVELS[name] = level


def set_module_level(module_name: str, level: int):
    """
    Dynamically set the log level for a specific module.

    Args:
        module_name: Full module path (e.g., 'addon_name.CR2W.dc_anims')
        level: logging.DEBUG, logging.INFO, logging.WARNING, etc.

    Example:
        from . import setup_logging_bl
        setup_logging_bl.set_module_level(f'{ADDON_NAME}.CR2W.dc_anims', logging.DEBUG)
    """
    logging.getLogger(module_name).setLevel(level)
    LOG_LEVELS[module_name] = level


def enable_debug_for(*modules: str):
    """
    Quick helper to enable DEBUG level for multiple modules.

    Example:
        setup_logging_bl.enable_debug_for(f'{ADDON_NAME}.CR2W.dc_anims', f'{ADDON_NAME}.importers.import_anims')
    """
    for module in modules:
        set_module_level(module, logging.DEBUG)


def enable_all_debug():
    """Enable DEBUG level for all witcher modules (very verbose!)."""
    for name in list(LOG_LEVELS):
        set_module_level(name, logging.DEBUG)


def silence_all():
    """Silence all log output (set everything to CRITICAL)."""
    for name in list(LOG_LEVELS):
        set_module_level(name, logging.CRITICAL)


def set_all_module_levels(level: int):
    """Set all registered modules to the given level."""
    for name in list(LOG_LEVELS):
        set_module_level(name, level)


# Addon-local console format (restores old '%(name)s' output without clobbering Blender's root logging).
ADDON_CONSOLE_FORMAT = '%(levelname)8s %(name)s %(message)s'
_HANDLER_KIND_ATTR = "_w3tb_handler_kind"
_COUNT_HANDLER_KIND = "count"
_CONSOLE_HANDLER_KIND = "console"
_console_format_enabled = True
_console_handler = None


# Prevent "No handlers found" warnings when CR2W is used standalone
_pkg_logger = logging.getLogger(ADDON_NAME)
if not _pkg_logger.handlers:
    _pkg_logger.addHandler(logging.NullHandler())


def _handler_kind(handler: logging.Handler) -> str:
    return getattr(handler, _HANDLER_KIND_ATTR, "")


def _tag_handler(handler: logging.Handler, kind: str) -> logging.Handler:
    setattr(handler, _HANDLER_KIND_ATTR, kind)
    return handler


def _find_handler(kind: str):
    for handler in _pkg_logger.handlers:
        if _handler_kind(handler) == kind:
            return handler
    return None


def _remove_count_handlers():
    """Remove count handlers from previous module loads so counts bind to current LOG_COUNTS."""
    for handler in list(_pkg_logger.handlers):
        if _handler_kind(handler) == _COUNT_HANDLER_KIND:
            _pkg_logger.removeHandler(handler)
            continue
        # Also remove legacy/stale count handlers created before tagging support.
        if handler.__class__.__name__ == "_LogCountHandler":
            _pkg_logger.removeHandler(handler)


def _remove_console_handlers():
    """Remove addon console handlers from previous module loads to refresh formatter behavior."""
    for handler in list(_pkg_logger.handlers):
        if _handler_kind(handler) == _CONSOLE_HANDLER_KIND:
            _pkg_logger.removeHandler(handler)


def _ensure_console_handler() -> logging.Handler:
    global _console_handler
    if _console_handler is None:
        handler = logging.StreamHandler()
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(_AddonConsoleFormatter(ADDON_CONSOLE_FORMAT))
        _console_handler = _tag_handler(handler, _CONSOLE_HANDLER_KIND)
    return _console_handler


def is_console_format_enabled() -> bool:
    """Return whether addon logs use the addon-local console formatter."""
    return _console_format_enabled


def set_console_format_enabled(enabled: bool):
    """Toggle addon-local console formatting without touching global root handlers."""
    global _console_format_enabled

    enabled = bool(enabled)
    _console_format_enabled = enabled

    if enabled:
        if _find_handler(_CONSOLE_HANDLER_KIND) is None:
            _pkg_logger.addHandler(_ensure_console_handler())
        # Stop propagation to Blender/root to avoid duplicate lines when our handler is active.
        _pkg_logger.propagate = False
        return

    for handler in list(_pkg_logger.handlers):
        if _handler_kind(handler) == _CONSOLE_HANDLER_KIND:
            _pkg_logger.removeHandler(handler)
    _pkg_logger.propagate = True


# Count only this addon's messages (not numpy, other addons, etc.)
_count_handler = _LogCountHandler()
_count_handler.setLevel(logging.DEBUG)
_tag_handler(_count_handler, _COUNT_HANDLER_KIND)
_remove_count_handlers()
_pkg_logger.addHandler(_count_handler)

# Enable addon-local formatted console output by default (can be toggled in dev UI).
_remove_console_handlers()
set_console_format_enabled(True)

# Apply configured levels
apply_log_levels()


def register():
    pass
