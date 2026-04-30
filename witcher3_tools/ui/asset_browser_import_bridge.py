import logging
import time
import uuid
import json
from contextlib import contextmanager
from functools import wraps

import bpy
from bpy.props import StringProperty

from .. import file_helpers
from ..importers import import_entity
from ..CR2W.common_blender import (
    clear_repo_override_roots,
    get_mod_priority_state,
    get_repo_override_state,
    overwrite_existing_enabled,
    set_mod_priority_settings,
    set_overwrite_existing,
    set_repo_override_roots,
)

log = logging.getLogger(__name__)

_CONTEXT_TTL_SECONDS = 15 * 60
_PENDING_IMPORT_CONTEXTS = {}


def _prune_stale_contexts():
    now = time.time()
    stale_tokens = [
        token for token, payload in _PENDING_IMPORT_CONTEXTS.items()
        if now - float(payload.get("created_at", 0.0) or 0.0) > _CONTEXT_TTL_SECONDS
    ]
    for token in stale_tokens:
        _PENDING_IMPORT_CONTEXTS.pop(token, None)


def _register_import_context(payload: dict) -> str:
    _prune_stale_contexts()
    token = uuid.uuid4().hex
    _PENDING_IMPORT_CONTEXTS[token] = dict(payload or {})
    _PENDING_IMPORT_CONTEXTS[token]["created_at"] = time.time()
    return token


def _get_import_context_by_token(token: str) -> dict | None:
    token = str(token or "").strip()
    if not token:
        return None
    payload = _PENDING_IMPORT_CONTEXTS.get(token)
    return payload if isinstance(payload, dict) else None


def get_asset_browser_import_context(operator) -> dict | None:
    return _get_import_context_by_token(getattr(operator, "ab_context_token", ""))


def has_asset_browser_import_context(operator) -> bool:
    return get_asset_browser_import_context(operator) is not None


def get_asset_browser_context_value(operator, key: str, default=None):
    payload = get_asset_browser_import_context(operator)
    if payload is None:
        return default
    return payload.get(key, default)


def clear_asset_browser_import_context(operator=None, token: str = ""):
    resolved = str(token or "").strip()
    if not resolved and operator is not None:
        resolved = str(getattr(operator, "ab_context_token", "") or "").strip()
    if not resolved:
        return
    _PENDING_IMPORT_CONTEXTS.pop(resolved, None)


@contextmanager
def asset_browser_operator_context(operator):
    payload = get_asset_browser_import_context(operator)
    if payload is None:
        yield
        return

    prev_enabled, prev_high = get_mod_priority_state()
    prev_overwrite = overwrite_existing_enabled()
    prev_roots, prev_read_only = get_repo_override_state()

    prefer_mods = bool(payload.get("prefer_mods", False))
    overwrite = bool(payload.get("overwrite", False))
    override_roots = list(payload.get("override_roots", []) or [])

    set_mod_priority_settings(True, prefer_mods)
    set_overwrite_existing(overwrite)
    if override_roots:
        set_repo_override_roots(override_roots, read_only=True)

    try:
        yield
    finally:
        set_mod_priority_settings(prev_enabled, prev_high)
        set_overwrite_existing(prev_overwrite)
        if prev_roots:
            set_repo_override_roots(prev_roots, read_only=prev_read_only)
        else:
            clear_repo_override_roots()


def record_asset_browser_import(operator, context):
    payload = get_asset_browser_import_context(operator)
    if payload is None:
        return
    path = str(payload.get("recent_path", "") or "").strip()
    cache_type = str(payload.get("recent_cache_type", "") or "").strip()
    if not path or not cache_type:
        return
    try:
        from . import ui_file_browser

        ui_file_browser.add_recent_import(context, path, cache_type)
    except Exception:
        log.debug("Failed to record asset browser recent import", exc_info=True)


def _ensure_context_token_property(cls):
    annotations = dict(getattr(cls, "__annotations__", {}) or {})
    if "ab_context_token" in annotations:
        return
    annotations["ab_context_token"] = StringProperty(
        default="",
        options={'HIDDEN', 'SKIP_SAVE'},
    )
    cls.__annotations__ = annotations


def patch_operator_for_asset_browser(cls, *, record_recent=True):
    if getattr(cls, "_asset_browser_context_patched", False):
        return cls

    _ensure_context_token_property(cls)
    original_execute = cls.execute

    @wraps(original_execute)
    def _execute_with_asset_browser_context(self, context):
        had_context = has_asset_browser_import_context(self)
        try:
            if had_context:
                with asset_browser_operator_context(self):
                    result = original_execute(self, context)
            else:
                result = original_execute(self, context)

            if had_context and record_recent and result and 'FINISHED' in result:
                record_asset_browser_import(self, context)
            return result
        finally:
            if had_context:
                clear_asset_browser_import_context(operator=self)

    cls.execute = _execute_with_asset_browser_context
    cls._asset_browser_context_patched = True
    return cls


def patch_operator_group_for_asset_browser(classes, *, record_recent=True):
    for cls in classes:
        patch_operator_for_asset_browser(cls, record_recent=record_recent)


def _build_import_context_payload(context, resolved: dict) -> dict:
    browser_settings = getattr(context.scene, "witcher_file_browser", None)
    return {
        "prefer_mods": bool(getattr(browser_settings, "use_mods_priority", False)),
        "overwrite": bool(getattr(browser_settings, "mods_overwrite", False)),
        "loadmods": bool(getattr(browser_settings, "loadmods", False)),
        "override_roots": list(resolved.get("override_roots") or []),
        "recent_path": str(resolved.get("full_path", "") or ""),
        "recent_cache_type": str(resolved.get("cache_type", "") or ""),
    }


def invoke_asset_browser_import_dialog(context, resolved: dict):
    payload = _build_import_context_payload(context, resolved)
    token = _register_import_context(payload)
    kwargs = {
        "filepath": resolved["abs_file_path"],
        "ab_context_token": token,
    }

    abs_file_path = str(resolved.get("abs_file_path", "") or "")
    effective_cache_type = str(resolved.get("effective_cache_type", "") or "")
    ext = file_helpers.getFilenameType(abs_file_path)
    abs_lower = abs_file_path.lower()

    try:
        if effective_cache_type == "Collision" and ext == ".nxs":
            result = bpy.ops.witcher.import_nxs('INVOKE_DEFAULT', **kwargs)
        elif ext == ".redcloth":
            result = bpy.ops.witcher.import_redcloth_materials('INVOKE_DEFAULT', **kwargs)
        elif ext == ".redapex":
            result = bpy.ops.witcher.import_redapex_materials('INVOKE_DEFAULT', **kwargs)
        elif ext == ".srt":
            result = bpy.ops.witcher.import_srt('INVOKE_DEFAULT', **kwargs)
        elif ext == ".w2mesh":
            result = bpy.ops.witcher.import_w2mesh('INVOKE_DEFAULT', **kwargs)
        elif ext == ".w2cube":
            result = bpy.ops.witcher.import_w2cube('INVOKE_DEFAULT', **kwargs)
        elif ext == ".w2ent":
            metadata = import_entity.get_entity_appearance_metadata(abs_file_path)
            w2ent_mode = import_entity.classify_entity_import_metadata(metadata, context=context)
            if w2ent_mode == "character":
                result = bpy.ops.witcher.import_w2ent_character(
                    'INVOKE_DEFAULT',
                    appearance_metadata_json=json.dumps(metadata, sort_keys=False),
                    appearance_metadata_path=abs_file_path,
                    **kwargs,
                )
            elif w2ent_mode == "inventory":
                result = bpy.ops.witcher.import_w2ent_inventory(
                    'INVOKE_DEFAULT',
                    import_mode='MOUNTS',
                    **kwargs,
                )
            else:
                result = bpy.ops.witcher.import_w2ent('INVOKE_DEFAULT', **kwargs)
        elif ext == ".flyr":
            result = bpy.ops.witcher.import_flyr('INVOKE_DEFAULT', **kwargs)
        elif ext == ".w2l":
            result = bpy.ops.witcher.import_w2l('INVOKE_DEFAULT', **kwargs)
        elif ext == ".w2w":
            result = bpy.ops.witcher.import_w2w('INVOKE_DEFAULT', **kwargs)
        elif ext == ".w2scene":
            result = bpy.ops.witcher.import_w2_scene('INVOKE_DEFAULT', **kwargs)
        elif ext == ".w2cutscene":
            result = bpy.ops.witcher.import_w2_cutscene('INVOKE_DEFAULT', **kwargs)
        elif ext == ".w2anims" or abs_lower.endswith(".w2anims.json"):
            result = bpy.ops.witcher.import_w2_anims_json('INVOKE_DEFAULT', **kwargs)
        elif ext in {".w2rig", ".w3dyng", ".w3fac"} or abs_lower.endswith((".w2rig.json", ".w3dyng.json")):
            result = bpy.ops.witcher.import_w2_rig('INVOKE_DEFAULT', **kwargs)
        elif ext == ".w2mi":
            result = bpy.ops.witcher.import_w2mi('INVOKE_DEFAULT', **kwargs)
        elif ext == ".w2mg":
            result = bpy.ops.witcher.import_w2mg('INVOKE_DEFAULT', **kwargs)
        elif ext == ".xbm":
            result = bpy.ops.witcher.import_xbm('INVOKE_DEFAULT', **kwargs)
        else:
            clear_asset_browser_import_context(token=token)
            return None
    except Exception:
        clear_asset_browser_import_context(token=token)
        raise

    if result and 'CANCELLED' in result:
        clear_asset_browser_import_context(token=token)
    return result
