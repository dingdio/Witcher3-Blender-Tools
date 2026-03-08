try:
    import bpy
    try:
        from ... import get_addon_name
        addon_name = get_addon_name()
    except Exception:
        addon_name = (__package__ or __name__).split('.')[0]
except Exception as e:
    pass
    #raise e
import os
from ...extension_paths import get_dev_override

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


def get_game_path():
    prefs = _get_addon_prefs()
    if prefs:
        return prefs.witcher_game_path
    return get_dev_override("fallback_game_path", "")

def get_W3_VOICE_PATH():
    prefs = _get_addon_prefs()
    if prefs:
        return prefs.W3_VOICE_PATH
    return get_dev_override("fallback_voice_path", "")

def get_W3_OGG_PATH():
    prefs = _get_addon_prefs()
    if prefs:
        return prefs.W3_VOICE_PATH
    return get_dev_override("fallback_ogg_path", "")
