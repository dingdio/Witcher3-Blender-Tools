import os
import subprocess
import logging
from typing import Any, Dict

import addon_utils
import bpy

log = logging.getLogger(__name__)


APX_ADDON_ID = "io_mesh_apx"
SRT_ADDON_ID = "io_mesh_srt"
RE_ADDON_ID = "blender_re_animations_plugin"

APX_ADDON_URL = "https://github.com/ArdCarraigh/Blender_APX_Addon"
SRT_ADDON_URL = "https://github.com/ArdCarraigh/Blender_SRT_Addon"
RE_ADDON_URL = "https://www.gog.com/en/game/the_witcher_3_redkit"


def _addon_state(addon_id: str) -> tuple[bool, bool]:
    try:
        exists, enabled = addon_utils.check(addon_id)
    except Exception:
        return False, False
    return bool(exists), bool(enabled)


def get_apx_addon_status(context=None) -> Dict[str, Any]:
    exists, enabled = _addon_state(APX_ADDON_ID)
    info: Dict[str, Any] = {
        "addon_id": APX_ADDON_ID,
        "exists": exists,
        "enabled": enabled,
        "url": APX_ADDON_URL,
        "sdk_path": "",
        "sdk_ready": False,
    }
    if not enabled:
        return info

    ctx = context or bpy.context
    addon = None
    try:
        addons = ctx.preferences.addons
        if hasattr(addons, "get"):
            addon = addons.get(APX_ADDON_ID)
        if addon is None:
            addon = addons[APX_ADDON_ID] if APX_ADDON_ID in addons else None
    except Exception:
        addon = None
    if not addon:
        return info

    prefs = getattr(addon, "preferences", None)
    sdk_path = getattr(prefs, "apex_sdk_cli", "") if prefs else ""
    sdk_path = (sdk_path or "").strip()
    info["sdk_path"] = sdk_path
    info["sdk_ready"] = bool(sdk_path and os.path.isfile(sdk_path))
    return info


def get_srt_addon_status() -> Dict[str, Any]:
    exists, enabled = _addon_state(SRT_ADDON_ID)
    return {
        "addon_id": SRT_ADDON_ID,
        "exists": exists,
        "enabled": enabled,
        "url": SRT_ADDON_URL,
    }


def get_re_addon_status() -> Dict[str, Any]:
    exists, enabled = _addon_state(RE_ADDON_ID)
    return {
        "addon_id": RE_ADDON_ID,
        "exists": exists,
        "enabled": enabled,
        "url": RE_ADDON_URL,
    }


def ensure_apx_from_apb(context, apb_path: str, overwrite: bool = False) -> Dict[str, Any]:
    """Convert a collision-cache APB to APX next to the APB using the APX addon SDK path."""
    apb_path = os.path.normpath(apb_path or "")
    apx_path = os.path.splitext(apb_path)[0] + ".apx" if apb_path else ""
    result: Dict[str, Any] = {
        "status": "invalid_input",
        "apb_path": apb_path,
        "apx_path": apx_path,
        "returncode": None,
        "message": "",
    }

    if not apb_path or os.path.splitext(apb_path)[1].lower() != ".apb":
        result["message"] = "Input is not an .apb file."
        return result
    if not os.path.isfile(apb_path):
        result["status"] = "missing_apb"
        result["message"] = "APB file does not exist."
        return result

    apx_status = get_apx_addon_status(context)
    if not apx_status["enabled"]:
        result["status"] = "apx_addon_disabled"
        result["message"] = "io_mesh_apx addon is not enabled."
        return result
    if not apx_status["sdk_ready"]:
        result["status"] = "apx_sdk_missing"
        result["message"] = "APX SDK CLI path is not configured or does not exist."
        return result

    apx_exists_before = os.path.isfile(apx_path)
    if apx_exists_before and not overwrite:
        result["status"] = "exists"
        result["message"] = "APX already exists."
        return result

    command = [apx_status["sdk_path"], "-s", "apx", apb_path]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
        result["returncode"] = completed.returncode
        if os.path.isfile(apx_path):
            result["status"] = "updated" if apx_exists_before else "converted"
            return result
        stderr = (completed.stderr or "").strip()
        stdout = (completed.stdout or "").strip()
        result["status"] = "failed"
        result["message"] = stderr or stdout or f"ParamTool exited with {completed.returncode}."
        return result
    except Exception as exc:
        result["status"] = "failed"
        result["message"] = str(exc)
        return result


def resolve_redcloth_apx(context, redcloth_resource_path: str, loadmods: bool = False) -> Dict[str, Any]:
    """Resolve or generate a .apx path for a depot-style .redcloth resource path."""
    redcloth_resource_path = (redcloth_resource_path or "").replace("/", "\\").lstrip("\\")
    result: Dict[str, Any] = {
        "status": "invalid_input",
        "redcloth_resource": redcloth_resource_path,
        "apb_path": "",
        "apx_path": "",
        "message": "",
    }

    if not redcloth_resource_path or not redcloth_resource_path.lower().endswith(".redcloth"):
        result["message"] = "Input is not a .redcloth depot path."
        return result

    try:
        from . import get_uncook_path
        from .CR2W.common_blender import repo_file
        from .CR2W.witcher_cache.CollisionCache import LoadCollisionManager
    except Exception as exc:
        result["status"] = "failed"
        result["message"] = f"Failed to load redcloth helpers: {exc}"
        return result

    ctx = context or bpy.context
    uncook = (get_uncook_path(ctx) or "").strip()
    base_rel = os.path.splitext(redcloth_resource_path)[0]

    apx_uncook = os.path.normpath(os.path.join(uncook, base_rel + ".apx")) if uncook else ""
    apb_uncook = os.path.normpath(os.path.join(uncook, base_rel + ".apb")) if uncook else ""
    result["apx_path"] = apx_uncook
    result["apb_path"] = apb_uncook

    if apx_uncook and os.path.isfile(apx_uncook):
        result["status"] = "existing_apx"
        return result

    if apb_uncook and os.path.isfile(apb_uncook):
        conv = ensure_apx_from_apb(ctx, apb_uncook, overwrite=False)
        result["apb_path"] = conv.get("apb_path", apb_uncook)
        result["apx_path"] = conv.get("apx_path", apx_uncook)
        if conv["status"] in {"converted", "updated", "exists"} and os.path.isfile(result["apx_path"]):
            result["status"] = "converted_from_uncook_apb"
            return result
        result["message"] = conv.get("message", "")

    # Fallback: any already-extracted repo path (if repo_file can resolve .apb)
    try:
        repo_apb = repo_file(base_rel + ".apb")
    except Exception:
        repo_apb = ""
    if repo_apb and os.path.isfile(repo_apb):
        conv = ensure_apx_from_apb(ctx, repo_apb, overwrite=False)
        result["apb_path"] = conv.get("apb_path", repo_apb)
        result["apx_path"] = conv.get("apx_path", os.path.splitext(repo_apb)[0] + ".apx")
        if conv["status"] in {"converted", "updated", "exists"} and os.path.isfile(result["apx_path"]):
            result["status"] = "converted_from_repo_apb"
            return result
        result["message"] = conv.get("message", result["message"])

    # Fallback: extract the APB from collision cache to uncook, then convert.
    if uncook:
        try:
            cm = LoadCollisionManager(do_reload=False, loadmods=loadmods)
            items = cm.find_item_by_path_name(redcloth_resource_path)
            if items:
                item = items[-1] if isinstance(items, list) else items
                item_name = getattr(item, "Name", redcloth_resource_path)
                out_ext = getattr(item, "Extension", ".apb") or ".apb"
                rel_out = os.path.splitext(item_name)[0] + out_ext
                apb_out = os.path.normpath(os.path.join(uncook, rel_out.replace("/", os.sep).lstrip(os.sep)))
                result["apb_path"] = apb_out
                if not os.path.isfile(apb_out):
                    parent = os.path.dirname(apb_out)
                    if parent:
                        os.makedirs(parent, exist_ok=True)
                    written = item.extract_to_file(apb_out)
                    if written:
                        apb_out = os.path.normpath(written)
                        result["apb_path"] = apb_out
                conv = ensure_apx_from_apb(ctx, apb_out, overwrite=False)
                result["apx_path"] = conv.get("apx_path", os.path.splitext(apb_out)[0] + ".apx")
                if conv["status"] in {"converted", "updated", "exists"} and os.path.isfile(result["apx_path"]):
                    result["status"] = "extracted_and_converted_from_collision_cache"
                    return result
                result["message"] = conv.get("message", result["message"])
            else:
                result["message"] = "No collision cache entry found for redcloth resource."
        except Exception as exc:
            result["message"] = f"Collision cache lookup/extract failed: {exc}"
            log.debug("resolve_redcloth_apx failed for %s: %s", redcloth_resource_path, exc)

    result["status"] = "missing_apx"
    if not result["message"]:
        result["message"] = "Could not find APX or APB for redcloth resource."
    return result
