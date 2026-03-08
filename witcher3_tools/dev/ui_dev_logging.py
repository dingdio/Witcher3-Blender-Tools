"""
Logging Control Panel for Witcher 3 Blender Tools.

Provides real-time visibility and control over the addon's logging system:
  - Per-module log level (DEBUG / INFO / WARNING / CRITICAL)
  - Master level control (sets all modules at once)
  - Live message counts broken down by level
  - Reset to defaults

Located in the 'W3 Dev' sidebar tab alongside the main dev panel.
"""
import logging

import bpy
from bpy.types import Operator, Panel
from bpy.props import StringProperty, IntProperty

from .. import setup_logging_bl


# ---------------------------------------------------------------------------
# Module groups – mirrors the structure of LOG_LEVELS in setup_logging_bl
# ---------------------------------------------------------------------------

MODULE_GROUPS = [
    ("Animation", [
        "importers.import_anims",
        "importers.motion_tools",
        "CR2W.dc_anims",
        "ui.ui_voice",
        "ui.ui_anims",
    ]),
    ("Mesh / Entity", [
        "importers.import_mesh",
        "importers.import_entity",
        "CR2W.dc_mesh",
        "CR2W.dc_entity",
        "ui.ui_entity",
        "w3_material",
        "importers.import_blender_fun",
    ]),
    ("Scene / Map", [
        "importers.import_scene",
        "ui.ui_map",
    ]),
    ("Core / CR2W", [
        "CR2W.CR2W_types",
        "CR2W.CR2W_file",
        "CR2W.bStream",
    ]),
]

# Levels shown as radio buttons per module
_LEVEL_BUTTONS = [
    (10, "DBG"),
    (20, "INF"),
    (30, "WRN"),
    (50, "CRT"),
]


def _full_name(short: str) -> str:
    """Prepend the addon package name to a short module path."""
    if not short:
        return setup_logging_bl.ADDON_NAME
    return f"{setup_logging_bl.ADDON_NAME}.{short}"


def _level_name(level: int) -> str:
    return setup_logging_bl.LEVEL_NAMES.get(level, str(level))


def _counts_label(full_name: str) -> str:
    """Build a compact count string, e.g. 'D:5 I:12 E:1'."""
    counts = setup_logging_bl.LOG_COUNTS.get(full_name)
    if not counts:
        return ""
    parts = []
    for short, key in [("D", "DEBUG"), ("I", "INFO"), ("W", "WARNING"),
                       ("E", "ERROR"), ("C", "CRITICAL")]:
        n = counts.get(key, 0)
        if n:
            parts.append(f"{short}:{n}")
    return "  ".join(parts)


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

class DEV_OT_LogLevelHelp(Operator):
    """Log Level Guide

DBG  (DEBUG 10)
  Everything. Internal state, per-bone loops, raw byte offsets,
  value dumps. Extremely noisy — use only when tracking down a
  specific parsing bug.

INF  (INFO 20)
  Normal operational flow. File opened, sections parsed, import
  complete, bone count, appearance selected. Good default for
  following what a module is doing without drowning in detail.

WRN  (WARNING 30)
  Unexpected but recoverable. Missing optional chunk, fallback
  value used, deprecated path hit. Worth knowing, won't break
  anything.

CRT  (CRITICAL 50)
  Used here as "OFF". The module is effectively silent — only
  catastrophic unhandled failures would surface, which are
  rare and usually come with a Python traceback anyway."""
    bl_idname = "dev.log_level_help"
    bl_label = "Log Level Guide"
    bl_options = {'REGISTER', 'INTERNAL'}

    def execute(self, context):
        return {'CANCELLED'}


class DEV_OT_SetLogLevel(Operator):
    """Set the log level for a specific module"""
    bl_idname = "dev.set_log_level"
    bl_label = "Set Log Level"
    bl_options = {'REGISTER', 'INTERNAL'}

    module_name: StringProperty(name="Module")
    level: IntProperty(name="Level")

    def execute(self, context):
        setup_logging_bl.set_module_level(self.module_name, self.level)
        return {'FINISHED'}


class DEV_OT_SetAllModuleLevels(Operator):
    """Set all modules to this log level"""
    bl_idname = "dev.set_all_module_levels"
    bl_label = "Set All Module Levels"
    bl_options = {'REGISTER', 'INTERNAL'}

    level: IntProperty(name="Level")

    def execute(self, context):
        setup_logging_bl.set_all_module_levels(self.level)
        return {'FINISHED'}


class DEV_OT_ResetLogDefaults(Operator):
    """Reset all module levels to the default configuration"""
    bl_idname = "dev.reset_log_defaults"
    bl_label = "Reset Defaults"
    bl_options = {'REGISTER', 'INTERNAL'}

    def execute(self, context):
        setup_logging_bl.apply_log_levels()
        return {'FINISHED'}


class DEV_OT_ResetLogCounts(Operator):
    """Clear all accumulated message counts"""
    bl_idname = "dev.reset_log_counts"
    bl_label = "Clear Counts"
    bl_options = {'REGISTER', 'INTERNAL'}

    def execute(self, context):
        setup_logging_bl.reset_log_counts()
        return {'FINISHED'}


class DEV_OT_ToggleConsoleLogFormat(Operator):
    """Toggle addon-local console formatting (shows logger/module name)"""
    bl_idname = "dev.toggle_console_log_format"
    bl_label = "Toggle Console Log Format"
    bl_options = {'REGISTER', 'INTERNAL'}

    def execute(self, context):
        setup_logging_bl.set_console_format_enabled(
            not setup_logging_bl.is_console_format_enabled()
        )
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------

class VIEW3D_PT_witcher_logging(Panel):
    """Witcher logging control panel – real-time level and count overview"""
    bl_label = "Logging Control"
    bl_idname = "VIEW3D_PT_witcher_logging"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'W3 Dev'
    bl_options = {'DEFAULT_CLOSED'}

    def draw_header(self, context):
        """Show a warning icon in the header whenever Debug Logging override is active."""
        from .. import is_verbose_logging
        if is_verbose_logging():
            self.layout.label(text="", icon='ERROR')

    def draw(self, context):
        layout = self.layout

        # --- Verbose override warning (full-width, impossible to miss) ---
        from .. import is_verbose_logging
        if is_verbose_logging():
            col = layout.column(align=True)
            col.alert = True
            col.label(text="\u26a0  DEBUG OVERRIDE ACTIVE", icon='ERROR')
            col.label(text="   All levels overridden by Addon Settings")
            col.label(text="   \u2192 Debug Logging toggle. Per-module")
            col.label(text="   levels below have no effect.")
            layout.separator(factor=0.5)

        # --- Master level control ---
        master_box = layout.box()
        header_row = master_box.row(align=True)
        header_row.label(text="Set all modules:", icon='MODIFIER')
        header_row.operator("dev.log_level_help", text="", icon='QUESTION', emboss=False)

        level_row = master_box.row(align=True)
        for level_val, btn_label in _LEVEL_BUTTONS:
            op = level_row.operator(
                "dev.set_all_module_levels",
                text=btn_label,
            )
            op.level = level_val

        util_row = master_box.row(align=True)
        util_row.operator("dev.reset_log_defaults", icon='RECOVER_LAST')
        util_row.operator("dev.reset_log_counts", icon='TRASH', text="Clear Counts")
        util_row.operator(
            "dev.toggle_console_log_format",
            text="Module Names",
            icon='CONSOLE',
            depress=setup_logging_bl.is_console_format_enabled(),
        )

        layout.separator(factor=0.5)

        # --- Root logger (styled like other groups) ---
        root_box = layout.box()
        root_box.label(text="Addon Root", icon='HOME')
        self._draw_module_row(root_box, "", setup_logging_bl.ADDON_DISPLAY_NAME)

        layout.separator(factor=0.3)

        # --- Per-group module rows ---
        for group_label, short_names in MODULE_GROUPS:
            box = layout.box()
            box.label(text=group_label, icon='PREFERENCES')
            for short in short_names:
                self._draw_module_row(box, short, short)

    def _draw_module_row(self, layout, short_name: str, display_name: str):
        full = _full_name(short_name)
        current_level = logging.getLogger(full).level

        row = layout.row(align=False)

        # Module name label (fixed width via split)
        split = row.split(factor=0.42)
        split.label(text=display_name)

        # Level radio buttons
        level_row = split.row(align=True)
        for level_val, btn_label in _LEVEL_BUTTONS:
            op = level_row.operator(
                "dev.set_log_level",
                text=btn_label,
                depress=(current_level == level_val),
            )
            op.module_name = full
            op.level = level_val

        # Message counts
        counts_str = _counts_label(full)
        if counts_str:
            counts = setup_logging_bl.LOG_COUNTS.get(full, {})
            has_errors = counts.get("ERROR", 0) + counts.get("CRITICAL", 0) > 0
            count_row = layout.row()
            count_row.alert = has_errors
            count_row.label(text=f"    {counts_str}")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_classes = [
    DEV_OT_LogLevelHelp,
    DEV_OT_SetLogLevel,
    DEV_OT_SetAllModuleLevels,
    DEV_OT_ResetLogDefaults,
    DEV_OT_ResetLogCounts,
    DEV_OT_ToggleConsoleLogFormat,
    VIEW3D_PT_witcher_logging,
]


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
