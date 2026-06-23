"""Shared preview/icon facade for browser-style asset thumbnails.

The heavy preview resolver still lives in ui_file_browser for now. This module
gives equipment and future browsers a public boundary so they do not import the
file browser's private helpers directly.
"""

from .browser_dummy_icons import ensure_browser_dummy_icon_path, ensure_browser_error_icon_path


def _file_browser():
    from . import ui_file_browser

    return ui_file_browser


def get_witcher2_bundle_cache_type():
    return _file_browser().WITCHER2_BUNDLE_CACHE_TYPE


def iter_preview_lookup_paths(file_path: str):
    try:
        return _file_browser()._iter_preview_lookup_paths(file_path)
    except Exception:
        return [file_path]


def expand_scaleform_icon_candidates(context, icon_path: str):
    try:
        return _file_browser()._expand_scaleform_icon_candidates(context, icon_path)
    except Exception:
        return []


def get_browser_item_icon_info(context, cache_type: str, item_path: str, loadmods: bool = False):
    return _file_browser()._get_browser_item_icon_info(
        context,
        cache_type,
        item_path,
        loadmods=loadmods,
    )


def ensure_dummy_icon_path(cache_type: str, item_path: str) -> str:
    return ensure_browser_dummy_icon_path(cache_type, item_path)


def ensure_error_icon_path() -> str:
    return ensure_browser_error_icon_path()
