"""
Witcher Dev Panel - Section-Based Test Path Management
=======================================================
This module provides a Blender UI panel for managing test paths
organized by sections with the ability to add, remove, and reorganize.

Data is stored in the "test_paths" key of dev_config.json.
"""

import bpy
import os
import json
import subprocess
from pathlib import Path
from bpy.props import StringProperty, IntProperty, EnumProperty, BoolProperty
from bpy.types import Panel, Operator

from .. import w3_asset_browser
from ..extension_paths import get_dev_panel_overrides


# =============================================================================
# Config File Management
# =============================================================================

def get_config_path():
    """Get the path to the unified dev_config.json file."""
    return Path(__file__).parent / "dev_config.json"


def _load_full_config():
    """Load the entire dev_config.json."""
    config_path = get_config_path()
    if config_path.exists():
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading dev_config.json: {e}")
    return {}


def load_config():
    """Load test paths from the unified JSON file."""
    full = _load_full_config()
    return full.get("test_paths", {})


def save_config(data):
    """Save test paths back into the unified JSON file."""
    config_path = get_config_path()
    try:
        full = _load_full_config()
        full["test_paths"] = data
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(full, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Error saving dev_config.json: {e}")


# Global config cache
_config_cache = None
_config_mtime = 0
_journal_browser_diag_cache = {}

_JOURNAL_BROWSER_LABELS = {
    "BESTIARY": "Bestiary Browser",
    "CHARACTERS": "Character Browser",
}


def get_config():
    """Get config, reloading if file changed."""
    global _config_cache, _config_mtime
    config_path = get_config_path()
    
    try:
        current_mtime = config_path.stat().st_mtime if config_path.exists() else 0
    except:
        current_mtime = 0
    
    if _config_cache is None or current_mtime != _config_mtime:
        _config_cache = load_config()
        _config_mtime = current_mtime
    
    return _config_cache


def invalidate_cache():
    """Force config reload on next access."""
    global _config_cache, _config_mtime
    _config_cache = None
    _config_mtime = 0


def _safe_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _normalize_browser_path(path):
    path = _safe_text(path)
    if not path:
        return ""
    if os.path.isabs(path):
        return os.path.normpath(path)
    return w3_asset_browser._normalize_depot_path(path)


def _path_signature(path):
    path = _safe_text(path)
    if not path:
        return "missing"
    try:
        stat = os.stat(path)
        return f"{int(stat.st_mtime)}:{stat.st_size}"
    except OSError:
        return "missing"


def _is_w2ent_path(path):
    return _safe_text(path).lower().endswith(".w2ent")


def _journal_browser_diag_signature(browser_key):
    cache_path, _meta_path = w3_asset_browser._cache_file_paths(browser_key)
    map_path = w3_asset_browser._builtin_character_entity_map_path(browser_key)
    return (
        _path_signature(cache_path),
        _path_signature(map_path),
    )


def _build_journal_browser_diag_item(entry, overrides):
    journal_path = _normalize_browser_path(entry.get("journal_path"))
    name = _safe_text(entry.get("name")) or Path(journal_path).stem or "<unnamed>"
    override_present = journal_path in overrides
    override_value = _normalize_browser_path(overrides.get(journal_path, "")) if override_present else ""
    cached_repo_path = _normalize_browser_path(entry.get("repo_path"))
    cached_repo_source = _safe_text(entry.get("repo_source")) or "missing"

    if cached_repo_source == "journal" and _is_w2ent_path(cached_repo_path):
        repo_path = cached_repo_path
        repo_source = "journal"
    elif _is_w2ent_path(override_value):
        repo_path = override_value
        repo_source = "override"
    else:
        repo_path = ""
        repo_source = "missing"

    if repo_source == "journal":
        resolution_state = "journal"
    elif repo_source == "override":
        resolution_state = "override"
    elif override_present:
        resolution_state = "override-empty"
    else:
        resolution_state = "override-missing"

    return {
        "name": name,
        "journal_path": journal_path,
        "repo_path": repo_path,
        "repo_source": repo_source,
        "override_present": override_present,
        "override_value": override_value,
        "resolution_state": resolution_state,
    }


def _load_journal_browser_diagnostics(browser_key):
    browser_key = _safe_text(browser_key).upper() or "BESTIARY"
    signature = _journal_browser_diag_signature(browser_key)
    cached = _journal_browser_diag_cache.get(browser_key)
    if cached and cached.get("signature") == signature:
        return cached.get("data")

    entries = w3_asset_browser._load_journal_entries_from_disk_payload(browser_key)
    overrides = w3_asset_browser._load_builtin_character_entity_map(browser_key)

    data = {
        "browser_key": browser_key,
        "label": _JOURNAL_BROWSER_LABELS.get(browser_key, browser_key.title()),
        "cache_available": isinstance(entries, list),
        "override_count": len(overrides),
        "override_path": w3_asset_browser._builtin_character_entity_map_path(browser_key),
        "entry_count": 0,
        "resolved_count": 0,
        "unresolved": [],
    }

    if isinstance(entries, list):
        for entry in entries:
            if _safe_text(entry.get("entry_kind")).lower() == "group":
                continue

            item = _build_journal_browser_diag_item(entry, overrides)
            data["entry_count"] += 1

            if _is_w2ent_path(item["repo_path"]):
                data["resolved_count"] += 1
            else:
                data["unresolved"].append(item)

    data["unresolved"].sort(
        key=lambda item: (
            _safe_text(item.get("name")).lower(),
            _safe_text(item.get("journal_path")).lower(),
        )
    )

    _journal_browser_diag_cache[browser_key] = {
        "signature": signature,
        "data": data,
    }
    return data


def _journal_browser_diag_status_text(item):
    if _safe_text(item.get("resolution_state")) == "override-empty":
        return "override entry is blank"
    return "missing from overrides"


# =============================================================================
# Helper Functions
# =============================================================================

DEV_OPERATOR_ITEMS = [
    ('w2mesh', 'W2MESH', 'Mesh import test paths'),
    ('nxs', 'NXS', 'Collision import test paths'),
    ('apx', 'APX', 'Cloth import test paths (Entity only)'),
    ('w2ent_chara', 'CHARACTER', 'Character entity import (.w2ent)'),
    ('w2ent', 'ENTITY', 'Item/prop entity import (.w2ent)'),
    ('w2anims', 'W2ANIMS', 'Animation import (.w2anims)'),
    ('w2rig', 'W2RIG', 'Rig/skeleton import (.w2rig)'),
    ('w2l', 'W2L', 'Layer import (.w2l)'),
    ('w2w', 'W2W', 'World import (.w2w)'),
    ('w2scene', 'W2SCENE', 'Scene import (.w2scene)'),
    ('w2cutscene', 'W2CUTSCENE', 'Cutscene import (.w2cutscene)'),
    ('w2mi', 'W2MI', 'Material instance import (.w2mi)'),
    ('w2mg', 'W2MG', 'Material shader import (.w2mg)'),
    ('xbm', 'XBM', 'Texture import (.xbm)'),
    ('w2cube', 'W2CUBE', 'Cubemap import (.w2cube)'),
    ('inventory', 'INVENTORY', 'Inventory import (.w2ent)'),
    ('w3app', 'W3APP', 'Appearance import (.w3app)'),
    ('fbx', 'FBX', 'Witcher 3 FBX import (.fbx)'),
    ('voice', 'VOICE', 'Voice/lipsync import (.cr2w)'),
    ('flyr', 'FOLIAGE', 'Foliage import (.flyr)'),
    ('srt', 'SRT', 'SpeedTree import (.srt)'),
]

DEV_OPERATOR_IDS = {item[0] for item in DEV_OPERATOR_ITEMS}


def _get_saved_active_operator():
    config = get_config()
    active = config.get("active_operator")
    if isinstance(active, str) and active in DEV_OPERATOR_IDS:
        return active
    return DEV_OPERATOR_ITEMS[0][0]


def _save_active_operator(value):
    if value not in DEV_OPERATOR_IDS:
        return
    config = get_config()
    if config.get("active_operator") == value:
        return
    config["active_operator"] = value
    save_config(config)
    invalidate_cache()


def _on_active_operator_update(self, context):
    active = getattr(self, "witcher_dev_active_operator", None)
    if not active:
        return
    _save_active_operator(active)


def file_exists(path):
    """Check if a file exists."""
    return os.path.exists(path) if path else False


def get_basename(path):
    """Get the filename from a path."""
    return os.path.basename(path) if path else ""


def _get_entry_fs_path(path_obj):
    """Return a direct filesystem path stored on a dev-panel entry."""
    if not isinstance(path_obj, dict):
        return ""
    return str(path_obj.get("path", "") or "").strip()


def _get_entry_repo_path(path_obj):
    """Return a depot/repo path stored on a dev-panel entry."""
    if not isinstance(path_obj, dict):
        return ""
    repo_path = str(path_obj.get("repo_path", "") or "").strip()
    return repo_path.replace("/", "\\")


def _is_repo_path_entry(path_obj):
    return bool(_get_entry_repo_path(path_obj))


def _get_entry_display_path(path_obj):
    """Prefer repo_path for display when present; otherwise use absolute path."""
    return _get_entry_repo_path(path_obj) or _get_entry_fs_path(path_obj)


def _entry_can_import(path_obj):
    """Entries can import from either an absolute path or a repo_path."""
    return bool(_get_entry_display_path(path_obj))


def _entry_exists_ui(path_obj):
    """UI existence check without forcing bundle extraction on every redraw.

    `repo_path` entries are treated as importable because they may be extracted
    on-demand by `repo_file(...)` during the import action.
    """
    fs_path = _get_entry_fs_path(path_obj)
    if fs_path:
        return file_exists(fs_path)
    return _is_repo_path_entry(path_obj)


def _is_override_value_set(value) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) > 0
    return value is not None


def _resolve_entry_import_path(path_obj):
    """Resolve an entry to a filesystem path suitable for import operators."""
    repo_path = _get_entry_repo_path(path_obj)
    if repo_path:
        from ..CR2W.common_blender import repo_file
        return repo_file(repo_path)
    return _get_entry_fs_path(path_obj)


# =============================================================================
# Operators for managing sections and paths
# =============================================================================

class DEV_OT_OpenConfigFile(Operator):
    """Open dev_config.json in default editor"""
    bl_idname = "witcher_dev.open_config_file"
    bl_label = "Open Config File"
    
    def execute(self, context):
        config_path = get_config_path()
        if config_path.exists():
            os.startfile(str(config_path))
        return {'FINISHED'}


class DEV_OT_OpenInExplorer(Operator):
    """Open file location in Explorer"""
    bl_idname = "witcher_dev.open_in_explorer"
    bl_label = "Open in Explorer"
    
    filepath: StringProperty()
    
    def execute(self, context):
        resolved_path = self.filepath
        # Allow passing a depot/repo path directly from dev-panel entries.
        if resolved_path and not os.path.isabs(resolved_path):
            try:
                from ..CR2W.common_blender import repo_file
                resolved_path = repo_file(resolved_path)
            except Exception:
                resolved_path = self.filepath

        if resolved_path and os.path.exists(resolved_path):
            subprocess.Popen(f'explorer /select,"{resolved_path}"')
        elif resolved_path:
            # Try to open parent directory
            parent = os.path.dirname(resolved_path)
            if os.path.exists(parent):
                subprocess.Popen(f'explorer "{parent}"')
            else:
                self.report({'WARNING'}, f"Path not found: {self.filepath}")
        return {'FINISHED'}


class DEV_OT_CopyText(Operator):
    """Copy text to clipboard"""
    bl_idname = "witcher_dev.copy_text"
    bl_label = "Copy Text"

    value: StringProperty(default="")
    report_label: StringProperty(default="Text")

    def execute(self, context):
        context.window_manager.clipboard = self.value
        self.report({'INFO'}, f"Copied {self.report_label}")
        return {'FINISHED'}


class DEV_OT_ReloadConfig(Operator):
    """Reload test paths from file"""
    bl_idname = "witcher_dev.reload_config"
    bl_label = "Reload Config"
    
    def execute(self, context):
        invalidate_cache()
        self.report({'INFO'}, "Config reloaded from dev_config.json")
        return {'FINISHED'}


class DEV_OT_AddSection(Operator):
    """Add a new section"""
    bl_idname = "witcher_dev.add_section"
    bl_label = "Add Section"
    bl_options = {'REGISTER', 'UNDO'}
    
    operator_type: StringProperty(default="w2mesh")
    
    def execute(self, context):
        config = get_config()
        if self.operator_type not in config:
            config[self.operator_type] = {"enabled": True, "selected_section": 0, "selected_path": 0, "sections": []}
        
        config[self.operator_type]["sections"].append({
            "name": "New Section",
            "paths": []
        })
        
        save_config(config)
        invalidate_cache()
        return {'FINISHED'}


class DEV_OT_RemoveSection(Operator):
    """Remove a section (with confirmation)"""
    bl_idname = "witcher_dev.remove_section"
    bl_label = "Remove Section"
    bl_options = {'REGISTER', 'UNDO'}

    operator_type: StringProperty(default="w2mesh")
    section_index: IntProperty(default=0)

    def execute(self, context):
        config = get_config()
        op_data = config.get(self.operator_type, {})
        sections = op_data.get("sections", [])

        if 0 <= self.section_index < len(sections):
            section_name = sections[self.section_index].get("name", "Unnamed")
            sections.pop(self.section_index)
            save_config(config)
            invalidate_cache()
            self.report({'INFO'}, f"Removed section: {section_name}")
        return {'FINISHED'}

    def invoke(self, context, event):
        config = get_config()
        op_data = config.get(self.operator_type, {})
        sections = op_data.get("sections", [])

        if 0 <= self.section_index < len(sections):
            section = sections[self.section_index]
            section_name = section.get("name", "Unnamed")
            path_count = len(section.get("paths", []))
            return context.window_manager.invoke_confirm(self, event,
                title="Delete Section?",
                message=f"Delete '{section_name}' with {path_count} path(s)?",
                confirm_text="Delete",
                icon='ERROR')
        return {'CANCELLED'}


class DEV_OT_MoveSection(Operator):
    """Move section up or down"""
    bl_idname = "witcher_dev.move_section"
    bl_label = "Move Section"
    bl_options = {'REGISTER', 'UNDO'}
    
    operator_type: StringProperty(default="w2mesh")
    section_index: IntProperty(default=0)
    direction: EnumProperty(items=[('UP', 'Up', ''), ('DOWN', 'Down', '')])
    
    def execute(self, context):
        config = get_config()
        op_data = config.get(self.operator_type, {})
        sections = op_data.get("sections", [])
        idx = self.section_index
        
        if self.direction == 'UP' and idx > 0:
            sections[idx], sections[idx-1] = sections[idx-1], sections[idx]
            save_config(config)
            invalidate_cache()
        elif self.direction == 'DOWN' and idx < len(sections) - 1:
            sections[idx], sections[idx+1] = sections[idx+1], sections[idx]
            save_config(config)
            invalidate_cache()
        return {'FINISHED'}


class DEV_OT_AddPath(Operator):
    """Add a new path to a section"""
    bl_idname = "witcher_dev.add_path"
    bl_label = "Add Path"
    bl_options = {'REGISTER', 'UNDO'}
    
    operator_type: StringProperty(default="w2mesh")
    section_index: IntProperty(default=0)
    
    def execute(self, context):
        config = get_config()
        op_data = config.get(self.operator_type, {})
        sections = op_data.get("sections", [])
        
        if 0 <= self.section_index < len(sections):
            sections[self.section_index]["paths"].append({
                "path": "",
                "note": "new path"
            })
            save_config(config)
            invalidate_cache()
        return {'FINISHED'}


class DEV_OT_RemovePath(Operator):
    """Remove a path from a section (with confirmation)"""
    bl_idname = "witcher_dev.remove_path"
    bl_label = "Remove Path"
    bl_options = {'REGISTER', 'UNDO'}

    operator_type: StringProperty(default="w2mesh")
    section_index: IntProperty(default=0)
    path_index: IntProperty(default=0)

    def execute(self, context):
        config = get_config()
        op_data = config.get(self.operator_type, {})
        sections = op_data.get("sections", [])

        if 0 <= self.section_index < len(sections):
            paths = sections[self.section_index].get("paths", [])
            if 0 <= self.path_index < len(paths):
                removed = paths.pop(self.path_index)
                save_config(config)
                invalidate_cache()
                self.report({'INFO'}, f"Removed: {get_basename(_get_entry_display_path(removed))}")
        return {'FINISHED'}

    def invoke(self, context, event):
        config = get_config()
        op_data = config.get(self.operator_type, {})
        sections = op_data.get("sections", [])

        if 0 <= self.section_index < len(sections):
            paths = sections[self.section_index].get("paths", [])
            if 0 <= self.path_index < len(paths):
                path_obj = paths[self.path_index]
                filename = get_basename(_get_entry_display_path(path_obj)) or "(empty)"
                note = path_obj.get("note", "")
                msg = f"Delete '{filename}'"
                if note:
                    msg += f" ({note})"
                msg += "?"
                return context.window_manager.invoke_confirm(self, event,
                    title="Delete Path?",
                    message=msg,
                    confirm_text="Delete",
                    icon='ERROR')
        return {'CANCELLED'}


class DEV_OT_SelectPath(Operator):
    """Select this path for import"""
    bl_idname = "witcher_dev.select_path"
    bl_label = "Select Path"
    
    operator_type: StringProperty(default="w2mesh")
    section_index: IntProperty(default=0)
    path_index: IntProperty(default=0)
    
    def execute(self, context):
        config = get_config()
        if self.operator_type in config:
            config[self.operator_type]["selected_section"] = self.section_index
            config[self.operator_type]["selected_path"] = self.path_index
            save_config(config)
            invalidate_cache()
        return {'FINISHED'}


class DEV_OT_ToggleEnabled(Operator):
    """Toggle test mode for operator"""
    bl_idname = "witcher_dev.toggle_enabled"
    bl_label = "Toggle Enabled"

    operator_type: StringProperty(default="w2mesh")

    def execute(self, context):
        config = get_config()
        if self.operator_type in config:
            config[self.operator_type]["enabled"] = not config[self.operator_type].get("enabled", True)
            save_config(config)
            invalidate_cache()
        return {'FINISHED'}


class DEV_OT_ToggleProblem(Operator):
    """Toggle problem status for a path"""
    bl_idname = "witcher_dev.toggle_problem"
    bl_label = "Toggle Problem"

    operator_type: StringProperty(default="w2mesh")
    section_index: IntProperty(default=0)
    path_index: IntProperty(default=0)

    def execute(self, context):
        config = get_config()
        op_data = config.get(self.operator_type, {})
        sections = op_data.get("sections", [])

        if 0 <= self.section_index < len(sections):
            paths = sections[self.section_index].get("paths", [])
            if 0 <= self.path_index < len(paths):
                path_obj = paths[self.path_index]
                # Toggle problem - if has problem, clear it; if not, set empty problem
                if path_obj.get("problem"):
                    del path_obj["problem"]
                    self.report({'INFO'}, "Problem cleared")
                else:
                    path_obj["problem"] = "Issue needs investigation"
                    self.report({'INFO'}, "Marked as problem")
                save_config(config)
                invalidate_cache()
        return {'FINISHED'}


class DEV_OT_SetProblem(Operator):
    """Set or edit the problem message for a path"""
    bl_idname = "witcher_dev.set_problem"
    bl_label = "Set Problem"
    bl_options = {'REGISTER', 'UNDO'}

    operator_type: StringProperty(default="w2mesh")
    section_index: IntProperty(default=0)
    path_index: IntProperty(default=0)
    problem_message: StringProperty(
        name="Problem Description",
        description="Describe the issue with this file",
        default=""
    )

    def execute(self, context):
        config = get_config()
        op_data = config.get(self.operator_type, {})
        sections = op_data.get("sections", [])

        if 0 <= self.section_index < len(sections):
            paths = sections[self.section_index].get("paths", [])
            if 0 <= self.path_index < len(paths):
                path_obj = paths[self.path_index]
                if self.problem_message.strip():
                    path_obj["problem"] = self.problem_message.strip()
                    self.report({'INFO'}, "Problem message set")
                elif "problem" in path_obj:
                    del path_obj["problem"]
                    self.report({'INFO'}, "Problem cleared")
                save_config(config)
                invalidate_cache()
        return {'FINISHED'}

    def invoke(self, context, event):
        config = get_config()
        op_data = config.get(self.operator_type, {})
        sections = op_data.get("sections", [])

        if 0 <= self.section_index < len(sections):
            paths = sections[self.section_index].get("paths", [])
            if 0 <= self.path_index < len(paths):
                path_obj = paths[self.path_index]
                self.problem_message = path_obj.get("problem", "")
                return context.window_manager.invoke_props_dialog(self, width=400)
        return {'CANCELLED'}

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "problem_message", text="")
        layout.label(text="Leave empty to clear problem status", icon='INFO')


class DEV_OT_ClearProblem(Operator):
    """Clear the problem status for a path"""
    bl_idname = "witcher_dev.clear_problem"
    bl_label = "Clear Problem"

    operator_type: StringProperty(default="w2mesh")
    section_index: IntProperty(default=0)
    path_index: IntProperty(default=0)

    def execute(self, context):
        config = get_config()
        op_data = config.get(self.operator_type, {})
        sections = op_data.get("sections", [])

        if 0 <= self.section_index < len(sections):
            paths = sections[self.section_index].get("paths", [])
            if 0 <= self.path_index < len(paths):
                path_obj = paths[self.path_index]
                if "problem" in path_obj:
                    del path_obj["problem"]
                    save_config(config)
                    invalidate_cache()
                    self.report({'INFO'}, "Problem cleared")
        return {'FINISHED'}


class DEV_OT_ImportPath(Operator):
    """Import the selected test path"""
    bl_idname = "witcher_dev.import_path"
    bl_label = "Import"
    bl_options = {'REGISTER', 'UNDO'}

    operator_type: StringProperty(default="w2mesh")
    section_index: IntProperty(default=0)
    path_index: IntProperty(default=0)

    def execute(self, context):
        config = get_config()
        op_data = config.get(self.operator_type, {})
        sections = op_data.get("sections", [])

        if 0 <= self.section_index < len(sections):
            paths = sections[self.section_index].get("paths", [])
            if 0 <= self.path_index < len(paths):
                path_obj = paths[self.path_index]
                display_path = _get_entry_display_path(path_obj)
                if not _entry_can_import(path_obj):
                    self.report({'ERROR'}, "Empty path entry")
                    return {'CANCELLED'}

                try:
                    filepath = _resolve_entry_import_path(path_obj)
                except Exception as e:
                    self.report({'ERROR'}, f"Path resolve failed: {display_path} ({e})")
                    return {'CANCELLED'}

                if not file_exists(filepath):
                    if _is_repo_path_entry(path_obj):
                        self.report({'ERROR'}, f"Repo path not found/extracted: {display_path}")
                    else:
                        self.report({'ERROR'}, f"File not found: {filepath}")
                    return {'CANCELLED'}

                # Determine invoke mode based on open_dialog setting
                open_dialog = context.scene.witcher_dev_open_dialog
                invoke_mode = 'INVOKE_DEFAULT' if open_dialog else 'EXEC_DEFAULT'

                # Call the appropriate import operator
                try:
                    if self.operator_type == 'w2mesh':
                        bpy.ops.witcher.import_w2mesh(invoke_mode, filepath=filepath)
                    elif self.operator_type == 'nxs':
                        bpy.ops.witcher.import_nxs(invoke_mode, filepath=filepath)
                    elif self.operator_type == 'apx':
                        bpy.ops.witcher.import_apx_materials(invoke_mode, filepath=filepath)
                    elif self.operator_type == 'w2ent_chara':
                        bpy.ops.witcher.import_w2ent_character(invoke_mode, filepath=filepath)
                    elif self.operator_type == 'w2ent':
                        bpy.ops.witcher.import_w2ent(invoke_mode, filepath=filepath)
                    elif self.operator_type == 'w2anims':
                        bpy.ops.witcher.import_w2_anims_json(invoke_mode, filepath=filepath)
                    elif self.operator_type == 'w2rig':
                        bpy.ops.witcher.import_w2_rig(invoke_mode, filepath=filepath)
                    elif self.operator_type == 'w2l':
                        bpy.ops.witcher.import_w2l(invoke_mode, filepath=filepath)
                    elif self.operator_type == 'w2w':
                        bpy.ops.witcher.import_w2w(invoke_mode, filepath=filepath)
                    elif self.operator_type == 'w2scene':
                        bpy.ops.witcher.import_w2_scene(invoke_mode, filepath=filepath)
                    elif self.operator_type == 'w2cutscene':
                        bpy.ops.witcher.import_w2_cutscene(invoke_mode, filepath=filepath)
                    elif self.operator_type == 'w2mi':
                        bpy.ops.witcher.import_w2mi(invoke_mode, filepath=filepath)
                    elif self.operator_type == 'w2mg':
                        bpy.ops.witcher.import_w2mg(invoke_mode, filepath=filepath)
                    elif self.operator_type == 'xbm':
                        bpy.ops.witcher.import_xbm(invoke_mode, filepath=filepath)
                    elif self.operator_type == 'w2cube':
                        bpy.ops.witcher.import_w2cube(invoke_mode, filepath=filepath)
                    elif self.operator_type == 'inventory':
                        bpy.ops.witcher.import_w2ent_inventory(invoke_mode, filepath=filepath)
                    elif self.operator_type == 'w3app':
                        bpy.ops.witcher.import_w3app(invoke_mode, filepath=filepath)
                    elif self.operator_type == 'fbx':
                        bpy.ops.witcher.import_witcher3_fbx(invoke_mode, filepath=filepath)
                    elif self.operator_type == 'voice':
                        bpy.ops.witcher.import_w2_voice(invoke_mode, filepath=filepath)
                    elif self.operator_type == 'flyr':
                        bpy.ops.witcher.import_flyr(invoke_mode, filepath=filepath)
                    elif self.operator_type == 'srt':
                        bpy.ops.witcher.import_srt(invoke_mode, filepath=filepath)
                    else:
                        self.report({'ERROR'}, f"Unknown operator type: {self.operator_type}")
                        return {'CANCELLED'}
                except Exception as e:
                    self.report({'ERROR'}, f"Import failed: {e}")
                    return {'CANCELLED'}

                return {'FINISHED'}

        self.report({'ERROR'}, "Invalid path selection")
        return {'CANCELLED'}


# =============================================================================
# Scene Properties for UI state
# =============================================================================

def register_props():
    bpy.types.Scene.witcher_dev_active_operator = EnumProperty(
        name="Operator Type",
        items=DEV_OPERATOR_ITEMS,
        default=_get_saved_active_operator(),
        update=_on_active_operator_update,
    )
    bpy.types.Scene.witcher_dev_open_dialog = BoolProperty(
        name="Open Dialog",
        description="Open file dialog when importing (shows import options)",
        default=False
    )


def unregister_props():
    del bpy.types.Scene.witcher_dev_open_dialog
    del bpy.types.Scene.witcher_dev_active_operator


# =============================================================================
# Main Panel
# =============================================================================

class VIEW3D_PT_witcher_dev(Panel):
    """Witcher Development Testing Panel"""
    bl_label = "Dev Panel"
    bl_idname = "VIEW3D_PT_witcher_dev"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'W3 Dev'
    
    def draw(self, context):
        layout = self.layout
        config = get_config()
        row = layout.row()
        row.label(text="Dev mode active", icon='CHECKMARK')

        override_data = get_dev_panel_overrides(include_when_disabled=True)
        set_keys = [k for k, v in override_data.items() if _is_override_value_set(v)]
        fallback_set = [k for k in set_keys if k.startswith("fallback_")]
        direct_set = [k for k in set_keys if not k.startswith("fallback_")]

        info = layout.box()
        info_col = info.column(align=True)
        info_col.label(text="addon_prefs_defaults: seeds empty Add-on Preferences once", icon='PREFERENCES')
        info_col.label(text="dev_panel_overrides: live dev-only runtime values", icon='TOOL_SETTINGS')
        info_col.label(text=f"Configured override keys: {len(set_keys)}", icon='INFO')
        info_col.label(text=f"fallback_* set: {len(fallback_set)} | direct keys set: {len(direct_set)}", icon='INFO')

        fallback_labels = [
            ("fallback_game_path", "W3 game"),
            ("fallback_uncook_path_w3", "W3 uncook"),
            ("fallback_fbx_uncook_path", "FBX uncook"),
            ("fallback_w2_data_path", "W2 data"),
            ("fallback_voice_path", "Voice"),
            ("fallback_ogg_path", "OGG"),
        ]
        source_box = layout.box()
        source_col = source_box.column(align=True)
        source_col.label(text="Fallback path keys in dev_panel_overrides", icon='FILE_FOLDER')
        for key, label in fallback_labels:
            row = source_col.row(align=True)
            if key in fallback_set:
                row.alert = True
                row.label(text=f"{label}: set", icon='CHECKMARK')
            else:
                row.label(text=f"{label}: not set", icon='PREFERENCES')
        
        # Config file buttons
        row = layout.row(align=True)
        row.operator("witcher_dev.open_config_file", icon='FILE_TEXT', text="Edit JSON")
        row.operator("witcher_dev.reload_config", icon='FILE_REFRESH', text="Reload")
        
        layout.separator()
        
        # Operator type selector (dropdown for many types)
        layout.prop(context.scene, "witcher_dev_active_operator")
        
        op_type = context.scene.witcher_dev_active_operator
        op_data = config.get(op_type, {})
        
        # Enabled toggle
        row = layout.row()
        enabled = op_data.get("enabled", True)
        op = row.operator("witcher_dev.toggle_enabled", 
                         icon='CHECKBOX_HLT' if enabled else 'CHECKBOX_DEHLT',
                         text=f"{'Enabled' if enabled else 'Disabled'}", depress=enabled)
        op.operator_type = op_type
        
        # Open dialog toggle
        row.prop(context.scene, "witcher_dev_open_dialog", text="Open Dialog")
        
        # Add section button
        row = layout.row()
        add_op = row.operator("witcher_dev.add_section", icon='ADD', text="Add Section")
        add_op.operator_type = op_type
        
        layout.separator()
        
        # Get selection state
        selected_sec = op_data.get("selected_section", 0)
        selected_path = op_data.get("selected_path", 0)
        
        # Draw sections
        sections = op_data.get("sections", [])
        for sec_idx, section in enumerate(sections):
            self._draw_section(layout, op_type, section, sec_idx, selected_sec, selected_path)

        layout.separator()
        self._draw_journal_browser_diagnostics(layout)
    
    def _draw_section(self, layout, op_type, section, sec_idx, selected_sec, selected_path):
        """Draw a single section with its paths."""
        box = layout.box()
        
        # Section header
        header = box.row()
        header.label(text=section.get("name", "Unnamed"), icon='DISCLOSURE_TRI_DOWN')
        
        # Section controls
        row = header.row(align=True)
        op = row.operator("witcher_dev.move_section", icon='TRIA_UP', text="")
        op.operator_type = op_type
        op.section_index = sec_idx
        op.direction = 'UP'
        
        op = row.operator("witcher_dev.move_section", icon='TRIA_DOWN', text="")
        op.operator_type = op_type
        op.section_index = sec_idx
        op.direction = 'DOWN'
        
        op = row.operator("witcher_dev.remove_section", icon='X', text="")
        op.operator_type = op_type
        op.section_index = sec_idx
        
        # Paths
        paths = section.get("paths", [])
        for path_idx, path_obj in enumerate(paths):
            display_path = _get_entry_display_path(path_obj)
            note = path_obj.get("note", "")
            problem = path_obj.get("problem", "")
            is_selected = (selected_sec == sec_idx and selected_path == path_idx)
            is_repo_path = _is_repo_path_entry(path_obj)
            exists = _entry_exists_ui(path_obj)
            has_problem = bool(problem)

            row = box.row(align=True)

            # Selection indicator
            icon = 'RADIOBUT_ON' if is_selected else 'RADIOBUT_OFF'
            op = row.operator("witcher_dev.select_path", icon=icon, text="")
            op.operator_type = op_type
            op.section_index = sec_idx
            op.path_index = path_idx

            # File exists indicator
            exists_icon = 'CHECKMARK' if exists else 'ERROR'
            row.label(text="", icon=exists_icon)

            # Problem indicator button (click to edit problem message)
            problem_icon = 'ERROR' if has_problem else 'BLANK1'
            op = row.operator("witcher_dev.set_problem", icon=problem_icon, text="")
            op.operator_type = op_type
            op.section_index = sec_idx
            op.path_index = path_idx

            # Path basename and note
            label = get_basename(display_path) if display_path else "(empty)"
            if is_repo_path:
                label = f"[repo] {label}"
            if note:
                label = f"{label} - {note}"
            row.label(text=label)

            # Open in Explorer button
            sub = row.row()
            sub.enabled = bool(display_path)
            op = sub.operator("witcher_dev.open_in_explorer", icon='FILE_FOLDER', text="")
            op.filepath = display_path

            # Import button
            sub = row.row()
            sub.enabled = exists
            op = sub.operator("witcher_dev.import_path", icon='IMPORT', text="")
            op.operator_type = op_type
            op.section_index = sec_idx
            op.path_index = path_idx

            # Remove path button
            op = row.operator("witcher_dev.remove_path", icon='REMOVE', text="")
            op.operator_type = op_type
            op.section_index = sec_idx
            op.path_index = path_idx

            # Show problem message on separate row if present
            if has_problem:
                prob_row = box.row()
                prob_row.alert = True
                prob_row.label(text=f"    ⚠ {problem}", icon='BLANK1')
        
        # Add path button
        row = box.row()
        op = row.operator("witcher_dev.add_path", icon='ADD', text="Add Path")
        op.operator_type = op_type
        op.section_index = sec_idx

    def _draw_journal_browser_diagnostics(self, layout):
        root = layout.box()
        root.label(text="Journal Browser .w2ent Diagnostics", icon='FILE')

        for browser_key in ("BESTIARY", "CHARACTERS"):
            diagnostics = _load_journal_browser_diagnostics(browser_key)
            self._draw_journal_browser_diagnostic_box(root, diagnostics)

    def _draw_journal_browser_diagnostic_box(self, layout, diagnostics):
        box = layout.box()
        header = box.row(align=True)
        header.label(text=diagnostics.get("label", "Journal Browser"), icon='FILE')
        refresh = header.operator("witcher.journal_browser_refresh", text="", icon='FILE_REFRESH')
        refresh.browser_key = diagnostics.get("browser_key", "")

        if not diagnostics.get("cache_available"):
            info = box.column(align=True)
            info.label(text="No browser cache found yet.", icon='INFO')
            info.label(text="Use refresh here or open the browser once to populate diagnostics.", icon='BLANK1')
            return

        counts = box.column(align=True)
        counts.label(text=f"Entries: {int(diagnostics.get('entry_count', 0))}", icon='INFO')
        counts.label(text=f"Resolved: {int(diagnostics.get('resolved_count', 0))}", icon='CHECKMARK')
        counts.label(text=f"Unresolved: {len(diagnostics.get('unresolved', []))}", icon='ERROR')
        counts.label(text=f"Override entries: {int(diagnostics.get('override_count', 0))}", icon='FILE_TEXT')

        override_row = box.row(align=True)
        override_row.label(text="Overrides JSON", icon='FILE_TEXT')
        copy_override = override_row.operator("witcher_dev.copy_text", text="", icon='COPYDOWN')
        copy_override.value = _safe_text(diagnostics.get("override_path"))
        copy_override.report_label = f"{diagnostics.get('label', 'browser')} override path"

        unresolved_box = box.box()
        unresolved = diagnostics.get("unresolved", [])
        unresolved_box.label(text=f"Unresolved Entries: {len(unresolved)}", icon='ERROR')

        if not unresolved:
            unresolved_box.label(text="All current entries resolved by journal or overrides.", icon='CHECKMARK')
            return

        column = unresolved_box.column(align=True)
        for item in unresolved:
            row = column.row(align=True)
            row.label(text=_safe_text(item.get("name")) or "<unnamed>", icon='ERROR')
            row.label(text=_journal_browser_diag_status_text(item))
            copy_path = row.operator("witcher_dev.copy_text", text="", icon='COPYDOWN')
            copy_path.value = _safe_text(item.get("journal_path"))
            copy_path.report_label = "journal repo path"

            path_row = column.row(align=True)
            path_row.label(text=_safe_text(item.get("journal_path")), icon='FILE')


# =============================================================================
# Registration
# =============================================================================

classes = [
    DEV_OT_OpenConfigFile,
    DEV_OT_OpenInExplorer,
    DEV_OT_CopyText,
    DEV_OT_ReloadConfig,
    DEV_OT_AddSection,
    DEV_OT_RemoveSection,
    DEV_OT_MoveSection,
    DEV_OT_AddPath,
    DEV_OT_RemovePath,
    DEV_OT_SelectPath,
    DEV_OT_ToggleEnabled,
    DEV_OT_ToggleProblem,
    DEV_OT_SetProblem,
    DEV_OT_ClearProblem,
    DEV_OT_ImportPath,
    VIEW3D_PT_witcher_dev,
]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    register_props()


def unregister():
    unregister_props()
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
