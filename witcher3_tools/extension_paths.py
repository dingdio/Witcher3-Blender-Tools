import os
import tempfile


def _ensure_dir(path: str, create: bool) -> str:
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def get_extension_user_dir(create: bool = True) -> str:
    """Return the per-extension user data directory (extension-safe writable path)."""
    try:
        import bpy
        if hasattr(bpy.utils, "extension_path_user"):
            try:
                path = bpy.utils.extension_path_user(__package__, create=create)
            except TypeError:
                path = bpy.utils.extension_path_user(__package__)
            if path:
                return _ensure_dir(path, create)
    except Exception:
        pass

    # Fallback for legacy add-on installs or non-Blender contexts.
    try:
        import bpy
        path = bpy.utils.user_resource("CONFIG", path=__package__.split(".")[-1], create=create)
        if path:
            return _ensure_dir(path, create)
    except Exception:
        pass

    base = os.path.join(tempfile.gettempdir(), __package__.split(".")[-1])
    return _ensure_dir(base, create)


def get_cache_root(create: bool = True) -> str:
    """Return the root directory for cached data."""
    return _ensure_dir(os.path.join(get_extension_user_dir(create), "witcher_cache"), create)


def get_uncook_root(create: bool = True) -> str:
    """Return the root directory for uncooked/exported game resources."""
    return _ensure_dir(os.path.join(get_extension_user_dir(create), "witcher_uncook"), create)


def get_audio_root(create: bool = True) -> str:
    """Return the root directory for speech/lipsync and converted audio outputs."""
    return _ensure_dir(os.path.join(get_extension_user_dir(create), "witcher_audio"), create)


def get_texture_root(create: bool = True) -> str:
    """Return the optional separate root directory for exported textures."""
    return _ensure_dir(os.path.join(get_extension_user_dir(create), "witcher_textures"), create)


def get_temp_root(create: bool = True) -> str:
    """Return a persistent temp directory for the extension."""
    return _ensure_dir(os.path.join(get_extension_user_dir(create), "temp"), create)


def _get_dev_config_module():
    try:
        from .dev import dev_config
        return dev_config
    except Exception:
        return None


def get_dev_mode_state() -> dict:
    """Return dev-mode availability and activation state."""
    dev_config = _get_dev_config_module()
    has_dev_folder = dev_config is not None
    dev_mode_enabled = bool(getattr(dev_config, "DEV_MODE_ENABLED", False)) if has_dev_folder else False
    return {
        "dev_folder_present": has_dev_folder,
        "dev_mode_enabled": dev_mode_enabled,
        "active": has_dev_folder and dev_mode_enabled,
    }


def is_dev_mode_active() -> bool:
    return bool(get_dev_mode_state().get("active"))


def get_dev_panel_overrides(*, include_when_disabled: bool = False) -> dict:
    """Return dev-panel overrides from dev_config, optionally even when dev mode is off."""
    dev_config = _get_dev_config_module()
    if dev_config is None:
        return {}

    overrides = getattr(dev_config, "DEV_PANEL_OVERRIDES", None)
    if overrides is None:
        # Backward compatibility with older configs/code.
        overrides = getattr(dev_config, "RUNTIME_OVERRIDES", {})
    if not isinstance(overrides, dict):
        return {}

    if include_when_disabled:
        return dict(overrides)

    if not bool(getattr(dev_config, "DEV_MODE_ENABLED", False)):
        return {}

    return dict(overrides)


def get_dev_runtime_overrides(*, include_when_disabled: bool = False) -> dict:
    """Backward-compatible alias for get_dev_panel_overrides()."""
    return get_dev_panel_overrides(include_when_disabled=include_when_disabled)


def get_dev_override(key: str, default=None):
    """Return a dev-only override value when dev mode is enabled."""
    overrides = get_dev_panel_overrides()
    return overrides.get(key, default)


def get_dev_override_list(key: str, default=None):
    """Return a dev-only list override when dev mode is enabled."""
    if default is None:
        default = []
    value = get_dev_override(key, default)
    if isinstance(value, list):
        return value
    return default
