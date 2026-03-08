"""Cache file export utilities.

Provides per-cache-type counts (items cached, files on disk) and a bulk
"Export All" modal operator that streams items to disk with a live progress
bar and a mandatory warning dialog.
"""

import bpy
import os
import re
import logging
import shutil

from bpy.props import StringProperty
from bpy.types import Operator

from .. import get_all_addon_prefs, get_texture_path, get_uncook_path, get_W3_VOICE_PATH
from ..CR2W.common_blender import win_path_exists, win_path_isdir
from ..external_addon_tools import (
    APX_ADDON_URL,
    SRT_ADDON_URL,
    ensure_apx_from_apb,
    get_apx_addon_status,
    get_srt_addon_status,
)

log = logging.getLogger(__name__)

# ── Module-level state ──────────────────────────────────────────────────────

# Stats are –1 until Refresh Stats is pressed.
_CACHE_STATS: dict = {
    "Bundle":    {"cached": -1, "on_disk": -1, "output_path": ""},
    "Texture":   {"cached": -1, "on_disk": -1, "output_path": ""},
    "Collision": {"cached": -1, "on_disk": -1, "output_path": "", "apb_on_disk": -1, "apx_on_disk": -1},
    "Speech":    {"cached": -1, "on_disk": -1, "on_disk_cr2w": -1, "on_disk_wem": -1, "output_path": ""},
}

_EXPORT_JOB: dict = {
    "running":    False,
    "cache_type": "",
    "overwrite":  False,
    "cancel_requested": False,
    "cancelled": False,
    "items":      [],   # list of (item, output_path)
    "index":      0,
    "total":      0,
    "done":       0,
    "skipped":    0,
    "errors":     0,
    "apb_extracted": 0,
    "apx_converted": 0,
    "apx_failed":    0,
    "timer":      None,
    "wm":         None,
    "context":    None,  # stored for post-finish stat refresh
}

# Items to process per timer tick.
_BATCH_SIZE = 150
_LOW_DISK_WARNING_BYTES = 5 * 1024 * 1024 * 1024  # 5 GiB


def _global_overwrite_enabled(context) -> bool:
    scene = getattr(context, "scene", None)
    if scene is None:
        return False
    return bool(getattr(scene, "witcher_cache_export_overwrite", False))


def _cache_output_root(context, cache_type: str) -> str:
    if cache_type == "Speech":
        return get_W3_VOICE_PATH(context) or ""
    if cache_type == "Texture":
        return get_texture_path(context) or ""
    return get_uncook_path(context) or ""


def _format_bytes(value: int) -> str:
    try:
        size = float(max(0, int(value)))
    except Exception:
        return "?"
    units = ("B", "KB", "MB", "GB", "TB")
    unit = 0
    while size >= 1024.0 and unit < len(units) - 1:
        size /= 1024.0
        unit += 1
    return f"{size:.1f} {units[unit]}" if unit > 0 else f"{int(size)} {units[unit]}"


def _format_count(value: int) -> str:
    try:
        count = int(value)
        return f"{count:,}" if count >= 0 else "?"
    except Exception:
        return "?"


def _first_existing_parent(path: str) -> str:
    if not path:
        return ""
    current = os.path.abspath(path)
    while current and not os.path.exists(current):
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return current if current and os.path.exists(current) else ""


def _disk_free_bytes(path: str) -> int:
    base = _first_existing_parent(path)
    if not base:
        return -1
    try:
        _total, _used, free = shutil.disk_usage(base)
        return int(free)
    except Exception:
        return -1


def _sum_item_sizes(items_dict: dict, size_attr: str, zsize_attr: str | None = None) -> tuple[int, int]:
    total_size = 0
    total_zsize = 0
    for value in items_dict.values():
        item = value[-1] if isinstance(value, list) else value
        if item is None:
            continue
        try:
            total_size += int(getattr(item, size_attr, 0) or 0)
        except Exception:
            pass
        if zsize_attr:
            try:
                total_zsize += int(getattr(item, zsize_attr, 0) or 0)
            except Exception:
                pass
    return total_size, total_zsize


def _cache_estimate_bytes(cache_type: str) -> int:
    try:
        return int(_CACHE_STATS.get(cache_type, {}).get("estimated_bytes", 0) or 0)
    except Exception:
        return 0


def _cache_estimate_compressed_bytes(cache_type: str) -> int:
    try:
        return int(_CACHE_STATS.get(cache_type, {}).get("estimated_compressed_bytes", 0) or 0)
    except Exception:
        return 0


# ── Helpers ─────────────────────────────────────────────────────────────────

def _count_dir_files(path: str, extensions=None) -> int:
    """Count files in *path* tree, optionally limited to *extensions* (lowercase set)."""
    if not path or not win_path_isdir(path):
        return 0
    count = 0
    try:
        for _root, _dirs, files in os.walk(path):
            for f in files:
                if extensions is None or os.path.splitext(f)[1].lower() in extensions:
                    count += 1
    except Exception:
        pass
    return count


def _skip_bundle_item(name: str) -> bool:
    """Return True if a bundle item should be skipped during bulk export.

    `.buffer` sidecar files are skipped – they are automatically paired with
    their parent file during normal extraction.  The one exception is
    `.w2ter.N.buffer` terrain tile buffers which must be present.
    """
    lower = name.lower()
    if ".buffer" not in lower:
        return False
    return not bool(re.search(r'\.w2ter\.\d+\.buffer$', lower))


def _refresh_ondisk_for(cache_type: str, output_root: str) -> None:
    """Update only the on-disk count for one cache type after export."""
    if cache_type == "Speech":
        cr2w_count = _count_dir_files(output_root, {".cr2w"})
        wem_count = _count_dir_files(output_root, {".wem"})
        _CACHE_STATS["Speech"]["on_disk"] = min(cr2w_count, wem_count)
        _CACHE_STATS["Speech"]["on_disk_cr2w"] = cr2w_count
        _CACHE_STATS["Speech"]["on_disk_wem"] = wem_count
        _CACHE_STATS["Speech"]["output_path"] = output_root
        return

    ext_map = {
        "Bundle":    None,
        "Texture":   {".dds"},
        "Collision": {".nxs", ".apb", ".apx", ".bin"},
    }
    if cache_type not in ext_map:
        return
    on_disk = _count_dir_files(output_root, ext_map[cache_type])
    if cache_type in _CACHE_STATS:
        _CACHE_STATS[cache_type]["on_disk"] = on_disk
        _CACHE_STATS[cache_type]["output_path"] = output_root
        if cache_type == "Collision":
            _CACHE_STATS[cache_type]["apb_on_disk"] = _count_dir_files(output_root, {".apb"})
            _CACHE_STATS[cache_type]["apx_on_disk"] = _count_dir_files(output_root, {".apx"})


def refresh_cache_stats(context) -> None:
    """Recompute cached-item and on-disk counts for all cache types."""
    from ..CR2W.witcher_cache.Bundles import LoadBundleManager
    from ..CR2W.witcher_cache.TextureCache import LoadTextureManager
    from ..CR2W.witcher_cache.CollisionCache import LoadCollisionManager
    from ..CR2W.witcher_cache.Speech import LoadSpeechManager

    uncook = get_uncook_path(context) or ""
    texture_root = get_texture_path(context) or ""

    # ── Bundle ──────────────────────────────────────────────────────────────
    try:
        bm = LoadBundleManager(loadmods=False, reset_cache=False)
        cached = 0
        estimated_bytes = 0
        estimated_zbytes = 0
        for path, item_list in bm.Items.items():
            item = item_list[-1] if isinstance(item_list, list) else item_list
            name = getattr(item, "name", "") or str(path)
            if not name or _skip_bundle_item(name):
                continue
            cached += 1
            estimated_bytes += int(getattr(item, "size", 0) or 0)
            estimated_zbytes += int(getattr(item, "zsize", 0) or 0)
        on_disk = _count_dir_files(uncook)
        _CACHE_STATS["Bundle"] = {
            "cached": cached,
            "on_disk": on_disk,
            "output_path": uncook,
            "estimated_bytes": estimated_bytes,
            "estimated_compressed_bytes": estimated_zbytes,
        }
    except Exception as e:
        log.warning("Bundle stats error: %s", e)

    # ── Texture ─────────────────────────────────────────────────────────────
    try:
        tm = LoadTextureManager(do_reload=False, loadmods=False)
        cached = len(tm.Items)
        estimated_bytes, estimated_zbytes = _sum_item_sizes(tm.Items, "Size", "ZSize")
        on_disk = _count_dir_files(texture_root, {".dds"})
        _CACHE_STATS["Texture"] = {
            "cached": cached,
            "on_disk": on_disk,
            "output_path": texture_root,
            "estimated_bytes": estimated_bytes,
            "estimated_compressed_bytes": estimated_zbytes,
        }
    except Exception as e:
        log.warning("Texture stats error: %s", e)

    # ── Collision ───────────────────────────────────────────────────────────
    try:
        cm = LoadCollisionManager(do_reload=False, loadmods=False)
        cached = len(cm.FileList)
        estimated_bytes, estimated_zbytes = _sum_item_sizes(cm.Items, "Size", "ZSize")
        on_disk = _count_dir_files(uncook, {".nxs", ".apb", ".apx", ".bin"})
        _CACHE_STATS["Collision"] = {
            "cached": cached,
            "on_disk": on_disk,
            "output_path": uncook,
            "apb_on_disk": _count_dir_files(uncook, {".apb"}),
            "apx_on_disk": _count_dir_files(uncook, {".apx"}),
            "estimated_bytes": estimated_bytes,
            "estimated_compressed_bytes": estimated_zbytes,
        }
    except Exception as e:
        log.warning("Collision stats error: %s", e)

    # ── Speech (unbundle to voice path) ─────────────────────────────────────
    try:
        from ..CR2W.witcher_cache.Speech import LoadSpeechManager
        sm = LoadSpeechManager()
        cached = len(sm.Items)
        estimated_bytes, estimated_zbytes = _sum_item_sizes(sm.Items, "size", "z_size")
        voice_path = get_W3_VOICE_PATH(context) or ""
        cr2w_count = _count_dir_files(voice_path, {".cr2w"})
        wem_count = _count_dir_files(voice_path, {".wem"})
        _CACHE_STATS["Speech"] = {
            "cached": cached,
            "on_disk": min(cr2w_count, wem_count),
            "on_disk_cr2w": cr2w_count,
            "on_disk_wem": wem_count,
            "output_path": voice_path,
            "estimated_bytes": estimated_bytes,
            "estimated_compressed_bytes": estimated_zbytes,
        }
    except Exception as e:
        log.warning("Speech stats error: %s", e)


def _build_export_items(context, cache_type: str) -> list:
    """Return a list of *(item, output_path)* tuples for the given cache type."""
    uncook = get_uncook_path(context) or ""
    texture_root = get_texture_path(context) or ""
    voice_path = get_W3_VOICE_PATH(context) or ""
    if cache_type in {"Bundle", "Collision"} and not uncook:
        raise ValueError("Uncook Path is not configured in addon preferences.")
    if cache_type == "Texture" and not texture_root:
        raise ValueError("Texture Path is not configured in addon preferences.")
    if cache_type == "Speech" and not voice_path:
        raise ValueError("W3 Voice Path is not configured in addon preferences.")

    items = []

    if cache_type == "Bundle":
        from ..CR2W.witcher_cache.Bundles import LoadBundleManager
        bm = LoadBundleManager(loadmods=False, reset_cache=False)
        for path, item_list in bm.Items.items():
            item = item_list[-1] if isinstance(item_list, list) else item_list
            name = getattr(item, 'name', '') or str(path)
            if not name or _skip_bundle_item(name):
                continue
            out = os.path.join(uncook, name.replace("/", os.sep).lstrip(os.sep))
            items.append((item, out))

    elif cache_type == "Texture":
        from ..CR2W.witcher_cache.TextureCache import LoadTextureManager
        tm = LoadTextureManager(do_reload=False, loadmods=False)
        for path, item_list in tm.Items.items():
            item = item_list[-1] if isinstance(item_list, list) else item_list
            name = getattr(item, 'Name', '') or getattr(item, 'name', '') or str(path)
            if not name:
                continue
            # extract_to_file always writes .dds; pre-compute the actual path
            out = os.path.join(
                texture_root,
                (os.path.splitext(name)[0] + ".dds").replace("/", os.sep).lstrip(os.sep)
            )
            items.append((item, out))

    elif cache_type == "Collision":
        from ..CR2W.witcher_cache.CollisionCache import LoadCollisionManager
        cm = LoadCollisionManager(do_reload=False, loadmods=False)
        for path, item_list in cm.Items.items():
            item = item_list[-1] if isinstance(item_list, list) else item_list
            name = getattr(item, 'Name', '') or getattr(item, 'name', '') or str(path)
            if not name:
                continue
            ext = getattr(item, 'Extension', '')
            out_name = (os.path.splitext(name)[0] + ext) if ext else name
            out = os.path.join(uncook, out_name.replace("/", os.sep).lstrip(os.sep))
            items.append((item, out))

    elif cache_type == "Speech":
        from ..CR2W.witcher_cache.Speech import LoadSpeechManager
        from ..CR2W.witcher_cache.Speech.W3Speech import pad_filename
        sm = LoadSpeechManager()
        for key, item_list in sm.Items.items():
            item = item_list[-1] if isinstance(item_list, list) else item_list
            entry_id = str(getattr(item, "id", key))
            base_name = pad_filename(os.path.splitext(os.path.basename(entry_id))[0])
            cr2w_out = os.path.join(voice_path, f"{base_name}.cr2w")
            wem_out = os.path.join(voice_path, f"{base_name}.wem")
            items.append((item, (entry_id, cr2w_out, wem_out)))

    return items


# ── Warning copy per cache type ──────────────────────────────────────────────

_WARNINGS = {
    "Bundle": (
        "~200,000+ game files including meshes, entities, scripts, and more.",
        "This requires several GB of free disk space and may take hours on slow hardware.",
    ),
    "Texture": (
        "~20,000+ DDS textures extracted from texture.cache archives.",
        "This requires several GB of free disk space and may take a long time.",
    ),
    "Collision": (
        "~10,000+ collision and physics mesh files.",
        "This will take a few minutes.",
    ),
    "Speech": (
        "~60,000+ speech pairs (.cr2w + .wem).",
        "This can take a long time and requires substantial disk space.",
    ),
}


# ── Operators ────────────────────────────────────────────────────────────────

class WITCHER_OT_RefreshCacheStats(Operator):
    bl_idname = "witcher.refresh_cache_stats"
    bl_label = "Refresh Cache Stats"
    bl_description = (
        "Count cached items and on-disk files for Bundle, Texture, Collision, "
        "and Speech caches.  May be slow on very large uncook directories."
    )

    def execute(self, context):
        try:
            refresh_cache_stats(context)
            self.report({'INFO'}, "Cache stats refreshed.")
        except Exception as e:
            self.report({'ERROR'}, f"Stats refresh failed: {e}")
        return {'FINISHED'}


class WITCHER_OT_OpenCacheExportFolder(Operator):
    """Open the export output folder in the OS file browser."""
    bl_idname = "witcher.open_cache_export_folder"
    bl_label = "Open Folder"
    bl_description = "Open the export output folder in the system file browser"

    cache_type: StringProperty(default="Bundle")

    def execute(self, context):
        path = _cache_output_root(context, self.cache_type)
        if not path or not win_path_isdir(path):
            self.report({'WARNING'}, f"Output folder not found: {path or '(not configured)'}")
            return {'CANCELLED'}
        try:
            bpy.ops.wm.path_open(filepath=path)
        except Exception as e:
            self.report({'ERROR'}, f"Could not open folder: {e}")
            return {'CANCELLED'}
        return {'FINISHED'}


class WITCHER_OT_CacheStatsInfo(Operator):
    """Show non-inline cache-specific status details."""
    bl_idname = "witcher.cache_stats_info"
    bl_label = "Cache Status Details"
    bl_description = "Show cache-specific status details (APX/WEM counts, estimates, and output path)"

    cache_type: StringProperty(default="Bundle")

    def invoke(self, context, event):
        return context.window_manager.invoke_popup(self, width=460)

    def draw(self, context):
        layout = self.layout
        ctype = self.cache_type or "Bundle"
        stats = _CACHE_STATS.get(ctype, {})

        cached = stats.get("cached", -1)
        on_disk = stats.get("on_disk", -1)
        output_path = stats.get("output_path", "") or "(not configured)"
        est_bytes = _cache_estimate_bytes(ctype)
        est_zbytes = _cache_estimate_compressed_bytes(ctype)

        layout.label(text=f"{ctype} Cache Status", icon='INFO')
        col = layout.column(align=True)
        col.label(text=f"Cache: {_format_count(cached)}")
        col.label(text=f"On disk: {_format_count(on_disk)}")
        col.label(text=f"Output path: {output_path}")

        if est_bytes > 0:
            col.label(text=f"Estimated full export: {_format_bytes(est_bytes)}")
        if est_zbytes > 0:
            col.label(text=f"Compressed-source size: {_format_bytes(est_zbytes)}")

        if ctype == "Speech":
            cr2w_count = stats.get("on_disk_cr2w", -1)
            wem_count = stats.get("on_disk_wem", -1)
            col.separator()
            col.label(text=f".cr2w on disk: {_format_count(cr2w_count)}")
            col.label(text=f".wem on disk: {_format_count(wem_count)}")
            col.label(text="On disk uses complete pairs (.cr2w + .wem).")
        elif ctype == "Collision":
            apb_on_disk = stats.get("apb_on_disk", -1)
            apx_on_disk = stats.get("apx_on_disk", -1)
            apx_status = get_apx_addon_status(context)
            col.separator()
            col.label(text=f".apb on disk: {_format_count(apb_on_disk)}")
            col.label(text=f".apx on disk: {_format_count(apx_on_disk)}")
            if not apx_status.get("enabled", False):
                col.label(text="APB->APX conversion: io_mesh_apx not enabled.", icon='ERROR')
            elif apx_status.get("sdk_ready", False):
                col.label(text="APB->APX conversion: ready.", icon='CHECKMARK')
            else:
                col.label(text="APB->APX conversion: apex_sdk_cli missing/invalid.", icon='ERROR')

    def execute(self, context):
        return {'FINISHED'}


class WITCHER_OT_CancelCacheExport(Operator):
    bl_idname = "witcher.cancel_cache_export"
    bl_label = "Cancel Export"
    bl_description = "Cancel the currently running bulk cache export job"

    def execute(self, context):
        if not _EXPORT_JOB.get("running"):
            return {'CANCELLED'}
        _EXPORT_JOB["cancel_requested"] = True
        return {'FINISHED'}


class WITCHER_OT_BrowseCacheInBrowser(Operator):
    """Open the Witcher Asset Browser navigated to a specific cache type."""
    bl_idname = "witcher.browse_cache_in_browser"
    bl_label = "Browse in Asset Browser"
    bl_description = "Open the Witcher Asset Browser and navigate directly to this cache type"

    cache_type: StringProperty(default="Bundle")

    def execute(self, context):
        # Select the cache type in the browser state first
        try:
            bpy.ops.witcher.select_cache_type(cache_type=self.cache_type)
        except Exception as e:
            log.warning("Could not pre-select cache type: %s", e)

        # Open the asset browser popup
        try:
            bpy.ops.witcher.simple_file_browser('INVOKE_DEFAULT')
        except Exception as e:
            self.report({'ERROR'}, f"Could not open asset browser: {e}")
            return {'CANCELLED'}
        return {'FINISHED'}


class WITCHER_OT_ExportAllCache(Operator):
    """Extract every item in the selected cache type to disk."""
    bl_idname = "witcher.export_all_cache"
    bl_label = "Export All Cache to Disk"
    bl_description = "Bulk-extract all items from a cache archive to the configured output path"

    cache_type: StringProperty(default="Bundle")

    # ── Invoke: show confirmation dialog ────────────────────────────────────

    def invoke(self, context, event):
        if _EXPORT_JOB["running"]:
            self.report({'WARNING'}, "An export job is already running.")
            return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self, width=540)

    def draw(self, context):
        layout = self.layout

        # Red alert box
        warn_box = layout.box()
        warn_box.alert = True
        warn_col = warn_box.column(align=True)
        warn_col.label(text="WARNING: This operation is NOT required for normal use!", icon='ERROR')
        warn_col.label(text="The Witcher Asset Browser extracts files automatically on demand.")

        layout.separator(factor=0.5)
        layout.label(text=f"You are about to export ALL {self.cache_type} cache files:", icon='EXPORT')

        lines = _WARNINGS.get(self.cache_type, ())
        col = layout.column(align=True)
        for line in lines:
            col.label(text=f"   {line}")

        if self.cache_type in ("Bundle", "Texture"):
            layout.separator(factor=0.3)
            time_box = layout.box()
            time_box.alert = True
            time_box.label(text="This may take a long time.  Do not close Blender.", icon='TIME')

        layout.separator(factor=0.5)
        overwrite = _global_overwrite_enabled(context)
        layout.label(
            text=f"Overwrite mode: {'ON - existing files will be overwritten' if overwrite else 'OFF - existing files will be skipped'}",
            icon='CHECKBOX_HLT' if overwrite else 'CHECKBOX_DEHLT',
        )

        output_root = _cache_output_root(context, self.cache_type)
        free_bytes = _disk_free_bytes(output_root)
        est_bytes = _cache_estimate_bytes(self.cache_type)
        est_compressed = _cache_estimate_compressed_bytes(self.cache_type)
        if free_bytes >= 0:
            layout.label(text=f"Free space: {_format_bytes(free_bytes)}", icon='DISK_DRIVE')
        if est_bytes > 0:
            est_text = f"Estimated export size: {_format_bytes(est_bytes)}"
            if est_compressed > 0:
                est_text += f" (compressed source: {_format_bytes(est_compressed)})"
            layout.label(text=est_text, icon='INFO')
        if free_bytes >= 0 and est_bytes > 0 and free_bytes < est_bytes:
            insufficient = layout.box()
            insufficient.alert = True
            insufficient.label(
                text=f"Likely insufficient space ({_format_bytes(est_bytes - free_bytes)} short).",
                icon='ERROR',
            )
        elif free_bytes >= 0 and free_bytes <= _LOW_DISK_WARNING_BYTES:
            low = layout.box()
            low.alert = True
            low.label(text="Low disk space warning.", icon='ERROR')

        layout.separator(factor=0.5)
        layout.label(text="Only proceed if you need all files pre-extracted at once.")
        layout.label(text="Click  OK  to start,  or  Cancel  to abort.")

    def execute(self, context):
        try:
            items = _build_export_items(context, self.cache_type)
        except ValueError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        except Exception as exc:
            self.report({'ERROR'}, f"Failed to load {self.cache_type} cache: {exc}")
            return {'CANCELLED'}

        if not items:
            self.report({'WARNING'}, f"No exportable items found in the {self.cache_type} cache.")
            return {'CANCELLED'}

        job = _EXPORT_JOB
        job["running"]    = True
        job["cache_type"] = self.cache_type
        job["overwrite"]  = _global_overwrite_enabled(context)
        job["cancel_requested"] = False
        job["cancelled"] = False
        job["items"]      = items
        job["index"]      = 0
        job["total"]      = len(items)
        job["done"]       = 0
        job["skipped"]    = 0
        job["errors"]     = 0
        job["apb_extracted"] = 0
        job["apx_converted"] = 0
        job["apx_failed"]    = 0
        job["context"]    = context

        wm = context.window_manager
        wm.progress_begin(0, len(items))
        job["wm"]    = wm
        job["timer"] = wm.event_timer_add(0.01, window=context.window)
        wm.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    # ── Modal: process a batch of items per timer tick ───────────────────────

    def modal(self, context, event):
        if event.type == 'ESC':
            _EXPORT_JOB["cancel_requested"] = True
            return {'RUNNING_MODAL'}

        if event.type != 'TIMER':
            return {'PASS_THROUGH'}

        job = _EXPORT_JOB
        if not job["running"]:
            return self._finish(context)
        if job.get("cancel_requested"):
            return self._finish(context, cancelled=True)

        overwrite = job["overwrite"]
        batch_end = min(job["index"] + _BATCH_SIZE, job["total"])
        while job["index"] < batch_end and not job.get("cancel_requested"):
            item, out_path = job["items"][job["index"]]
            job["index"] += 1
            try:
                extracted_path = ""
                if job["cache_type"] == "Speech":
                    entry_id, cr2w_out, wem_out = out_path
                    if not overwrite and win_path_exists(cr2w_out) and win_path_exists(wem_out):
                        job["skipped"] += 1
                    else:
                        parent = os.path.dirname(cr2w_out)
                        if parent:
                            os.makedirs(parent, exist_ok=True)
                        extracted_path = item.extract_to_file(entry_id) or wem_out
                        job["done"] += 1
                else:
                    extracted_path = out_path
                    if not overwrite and win_path_exists(out_path):
                        job["skipped"] += 1
                    else:
                        parent = os.path.dirname(out_path)
                        if parent:
                            os.makedirs(parent, exist_ok=True)
                        extracted_path = item.extract_to_file(out_path) or out_path
                        job["done"] += 1
                        if job["cache_type"] == "Collision" and str(extracted_path).lower().endswith(".apb"):
                            job["apb_extracted"] += 1

                if job["cache_type"] == "Collision" and str(extracted_path).lower().endswith(".apb"):
                    conv = ensure_apx_from_apb(context, extracted_path, overwrite=overwrite)
                    if conv["status"] in {"converted", "updated"}:
                        job["apx_converted"] += 1
                    elif conv["status"] == "failed":
                        job["apx_failed"] += 1
            except Exception as exc:
                log.debug("Export error for %s: %s", out_path, exc)
                job["errors"] += 1

        job["wm"].progress_update(job["index"])

        # Redraw all areas so the progress label stays current
        for area in context.screen.areas:
            area.tag_redraw()

        if job.get("cancel_requested"):
            return self._finish(context, cancelled=True)

        if job["index"] >= job["total"]:
            return self._finish(context)

        return {'RUNNING_MODAL'}

    def _finish(self, context, cancelled: bool = False):
        job = _EXPORT_JOB
        wm    = job.get("wm")
        timer = job.get("timer")

        if wm and timer:
            try:
                wm.event_timer_remove(timer)
            except Exception:
                pass
            try:
                wm.progress_end()
            except Exception:
                pass

        done    = job["done"]
        skipped = job["skipped"]
        errors  = job["errors"]
        apb_extracted = job.get("apb_extracted", 0)
        apx_converted = job.get("apx_converted", 0)
        apx_failed    = job.get("apx_failed", 0)
        ctype   = job["cache_type"]
        ctx     = job.get("context") or context
        processed = job["index"]
        total = job["total"]

        job["running"] = False
        job["cancel_requested"] = False
        job["cancelled"] = bool(cancelled)
        job["items"]   = []
        job["timer"]   = None
        job["wm"]      = None
        job["context"] = None
        job["apb_extracted"] = 0
        job["apx_converted"] = 0
        job["apx_failed"]    = 0

        # Auto-refresh the on-disk count so the UI is immediately up to date
        try:
            output_root = _cache_output_root(ctx, ctype)
            if output_root:
                _refresh_ondisk_for(ctype, output_root)
        except Exception:
            pass

        level = 'INFO' if (errors == 0 and not cancelled) else 'WARNING'
        if cancelled:
            message = (
                f"{ctype} export cancelled at {processed:,}/{total:,}: "
                f"{done:,} extracted, {skipped:,} skipped, {errors:,} errors."
            )
        else:
            message = (
                f"{ctype} export complete: {done:,} extracted, "
                f"{skipped:,} skipped (already on disk), {errors:,} errors."
            )
        if ctype == "Collision":
            message += f" APB extracted: {apb_extracted:,}. APX converted: {apx_converted:,}."
            if apx_failed:
                message += f" APX conversion failures: {apx_failed:,}."
        self.report({level}, message)
        return {'CANCELLED'} if cancelled else {'FINISHED'}


# ── UI draw helper ─────────────────────────────────────────────────────────

def draw_export_stats_ui(layout, context) -> None:
    """File export stats table with per-cache-type controls."""
    box = layout.box()

    header = box.row(align=True)
    header.label(text="File Export", icon='EXPORT')
    header.operator(
        "witcher.refresh_cache_stats",
        text="Refresh Stats",
        icon='FILE_REFRESH',
    )

    job = _EXPORT_JOB
    if job["running"]:
        total = job["total"] or 1
        pct = job["index"] / total * 100

        prog_box = box.box()
        prog_row = prog_box.row(align=True)
        prog_row.label(
            text=(
                f"Exporting {job['cache_type']}...  "
                f"{job['index']:,} / {job['total']:,}  ({pct:.1f}%)"
            ),
            icon='TIME',
        )
        prog_row.operator("witcher.cancel_cache_export", text="Cancel", icon='CANCEL')

        detail_row = prog_box.row(align=True)
        detail_text = (
            f"   Extracted: {job['done']:,}   "
            f"Skipped: {job['skipped']:,}   "
            f"Errors: {job['errors']:,}"
        )
        if job["cache_type"] == "Collision":
            detail_text += (
                f"   APB: {job.get('apb_extracted', 0):,}   "
                f"APX: {job.get('apx_converted', 0):,}"
            )
        detail_row.label(text=detail_text)
        return

    col = box.column(align=True)

    for ctype, icon_name in (
        ("Bundle", "PACKAGE"),
        ("Texture", "IMAGE_DATA"),
        ("Collision", "MESH_DATA"),
        ("Speech", "SPEAKER"),
    ):
        stats = _CACHE_STATS.get(ctype, {})
        cached = stats.get("cached", -1)
        on_disk = stats.get("on_disk", -1)

        cached_str = _format_count(cached)
        ondisk_str = _format_count(on_disk)

        row = col.row(align=True)
        split = row.split(factor=0.67, align=True)
        info = split.row(align=True)
        actions = split.row(align=True)

        info.label(text=ctype, icon=icon_name)
        info.label(text=f"Cache: {cached_str}")
        info.label(text=f"On disk: {ondisk_str}")

        info_op = actions.operator("witcher.cache_stats_info", text="", icon='INFO')
        info_op.cache_type = ctype

        ex_op = actions.operator("witcher.export_all_cache", text="Export All", icon='EXPORT')
        ex_op.cache_type = ctype

        browse_op = actions.operator("witcher.browse_cache_in_browser", text="", icon='VIEWZOOM')
        browse_op.cache_type = ctype

        folder_op = actions.operator("witcher.open_cache_export_folder", text="", icon='FILE_FOLDER')
        folder_op.cache_type = ctype

    scene = getattr(context, "scene", None)
    if scene is not None and hasattr(scene, "witcher_cache_export_overwrite"):
        box.prop(
            scene,
            "witcher_cache_export_overwrite",
            text="Overwrite existing files during bulk export",
        )

    uncook_path = get_uncook_path(context) or ""
    texture_path = get_texture_path(context) or ""
    voice_path = get_W3_VOICE_PATH(context) or ""

    uncook_free = _disk_free_bytes(uncook_path)
    texture_free = _disk_free_bytes(texture_path)
    voice_free = _disk_free_bytes(voice_path)

    uncook_est = sum(_cache_estimate_bytes(name) for name in ("Bundle", "Collision"))
    uncook_est_z = sum(_cache_estimate_compressed_bytes(name) for name in ("Bundle", "Collision"))
    texture_est = _cache_estimate_bytes("Texture")
    texture_est_z = _cache_estimate_compressed_bytes("Texture")
    speech_est = _cache_estimate_bytes("Speech")
    speech_est_z = _cache_estimate_compressed_bytes("Speech")

    disk_box = box.box()
    disk_box.label(text="Disk Space", icon='DISK_DRIVE')

    uncook_line = disk_box.row(align=True)
    uncook_line.label(text="Uncook target:")
    if uncook_free >= 0:
        uncook_line.label(text=f"Free: {_format_bytes(uncook_free)}")
        if uncook_est > 0:
            uncook_line.label(text=f"Estimated full export: {_format_bytes(uncook_est)}")

    if uncook_est > 0 and uncook_est_z > 0:
        disk_box.label(text=f"Uncook compressed-source size: {_format_bytes(uncook_est_z)}", icon='INFO')

    if uncook_free >= 0 and uncook_est > 0 and uncook_free < uncook_est:
        warn = disk_box.row(align=True)
        warn.alert = True
        warn.label(
            text=f"Uncook warning: likely short by {_format_bytes(uncook_est - uncook_free)}",
            icon='ERROR',
        )
    elif uncook_free >= 0 and uncook_free <= _LOW_DISK_WARNING_BYTES:
        warn = disk_box.row(align=True)
        warn.alert = True
        warn.label(text="Uncook warning: low free disk space.", icon='ERROR')

    texture_line = disk_box.row(align=True)
    texture_line.label(text="Texture target:")
    if texture_free >= 0:
        texture_line.label(text=f"Free: {_format_bytes(texture_free)}")
        if texture_est > 0:
            texture_line.label(text=f"Estimated full export: {_format_bytes(texture_est)}")

    if texture_est > 0 and texture_est_z > 0:
        disk_box.label(text=f"Texture compressed-source size: {_format_bytes(texture_est_z)}", icon='INFO')

    if texture_free >= 0 and texture_est > 0 and texture_free < texture_est:
        warn = disk_box.row(align=True)
        warn.alert = True
        warn.label(
            text=f"Texture warning: likely short by {_format_bytes(texture_est - texture_free)}",
            icon='ERROR',
        )
    elif texture_free >= 0 and texture_free <= _LOW_DISK_WARNING_BYTES:
        warn = disk_box.row(align=True)
        warn.alert = True
        warn.label(text="Texture warning: low free disk space.", icon='ERROR')

    voice_line = disk_box.row(align=True)
    voice_line.label(text="Voice target:")
    if voice_free >= 0:
        voice_line.label(text=f"Free: {_format_bytes(voice_free)}")
        if speech_est > 0:
            voice_line.label(text=f"Estimated full export: {_format_bytes(speech_est)}")

    if speech_est > 0 and speech_est_z > 0:
        disk_box.label(text=f"Voice compressed-source size: {_format_bytes(speech_est_z)}", icon='INFO')

    if voice_free >= 0 and speech_est > 0 and voice_free < speech_est:
        warn = disk_box.row(align=True)
        warn.alert = True
        warn.label(
            text=f"Voice warning: likely short by {_format_bytes(speech_est - voice_free)}",
            icon='ERROR',
        )
    elif voice_free >= 0 and voice_free <= _LOW_DISK_WARNING_BYTES:
        warn = disk_box.row(align=True)
        warn.alert = True
        warn.label(text="Voice warning: low free disk space.", icon='ERROR')

def draw_addon_status_ui(layout, context) -> None:
    """APX and SRT external addon install status and SDK validation."""
    deps_box = layout.box()
    deps_box.label(text="External Addons", icon='PLUGIN')

    apx_status = get_apx_addon_status(context)
    apx_row = deps_box.row(align=True)
    apx_icon = 'CHECKMARK' if apx_status["enabled"] else 'ERROR'
    apx_row.label(
        text=f"io_mesh_apx: {'enabled' if apx_status['enabled'] else 'not enabled'}",
        icon=apx_icon,
    )
    if not apx_status["exists"]:
        apx_row.operator("wm.url_open", text="GitHub", icon='URL').url = APX_ADDON_URL

    sdk_row = deps_box.row(align=True)
    if not apx_status["enabled"]:
        sdk_row.label(text="APX SDK CLI: enable io_mesh_apx to configure apex_sdk_cli", icon='INFO')
    elif apx_status["sdk_ready"]:
        sdk_row.label(text="APX SDK CLI: configured", icon='CHECKMARK')
    else:
        sdk_row.alert = True
        sdk_row.label(text="APX SDK CLI (apex_sdk_cli): missing/invalid, APB->APX conversion disabled", icon='ERROR')

    srt_status = get_srt_addon_status()
    srt_row = deps_box.row(align=True)
    srt_icon = 'CHECKMARK' if srt_status["enabled"] else 'ERROR'
    srt_row.label(
        text=f"io_mesh_srt: {'enabled' if srt_status['enabled'] else 'not enabled'}",
        icon=srt_icon,
    )
    if not srt_status["exists"]:
        srt_row.operator("wm.url_open", text="GitHub", icon='URL').url = SRT_ADDON_URL


def draw_import_options_ui(layout, context) -> None:
    """Global import options: Redcloth and SpeedTree settings."""
    prefs = get_all_addon_prefs(context)
    options_box = layout.box()
    options_box.label(text="Import Options", icon='SETTINGS')

    redcloth_col = options_box.column(align=True)
    redcloth_col.label(text="Redcloth (.apx)", icon='MATCLOTH')
    redcloth_col.prop(prefs, "do_import_redcloth")
    redcloth_col.prop(prefs, "DO_WEAR_CLOTH")
    redcloth_col.prop(prefs, "redcloth_simulation_enabled")
    redcloth_col.prop(prefs, "redcloth_wind_velocity")

    srt_col = options_box.column(align=True)
    srt_col.label(text="SpeedTree (.srt)", icon='OUTLINER_COLLECTION')
    srt_col.prop(prefs, "ab_srt_custom_grouping")
    srt_col.prop(prefs, "ab_srt_lod0_only")


def draw_cache_export_ui(layout, context) -> None:
    """Legacy wrapper — kept for any external callers. Calls all three sub-sections."""
    draw_export_stats_ui(layout, context)
    draw_addon_status_ui(layout, context)
    draw_import_options_ui(layout, context)


# ── Registration ──────────────────────────────────────────────────────────────

def register():
    if not hasattr(bpy.types.Scene, "witcher_cache_export_overwrite"):
        bpy.types.Scene.witcher_cache_export_overwrite = bpy.props.BoolProperty(
            name="Overwrite Existing Files",
            description="If enabled, bulk export overwrites files already on disk",
            default=False,
        )
    bpy.utils.register_class(WITCHER_OT_RefreshCacheStats)
    bpy.utils.register_class(WITCHER_OT_OpenCacheExportFolder)
    bpy.utils.register_class(WITCHER_OT_CacheStatsInfo)
    bpy.utils.register_class(WITCHER_OT_CancelCacheExport)
    bpy.utils.register_class(WITCHER_OT_BrowseCacheInBrowser)
    bpy.utils.register_class(WITCHER_OT_ExportAllCache)


def unregister():
    bpy.utils.unregister_class(WITCHER_OT_ExportAllCache)
    bpy.utils.unregister_class(WITCHER_OT_BrowseCacheInBrowser)
    bpy.utils.unregister_class(WITCHER_OT_CancelCacheExport)
    bpy.utils.unregister_class(WITCHER_OT_CacheStatsInfo)
    bpy.utils.unregister_class(WITCHER_OT_OpenCacheExportFolder)
    bpy.utils.unregister_class(WITCHER_OT_RefreshCacheStats)
    if hasattr(bpy.types.Scene, "witcher_cache_export_overwrite"):
        del bpy.types.Scene.witcher_cache_export_overwrite
