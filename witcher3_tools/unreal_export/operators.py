"""Blender operators and UI for Unreal export bundles."""

from __future__ import annotations

import json
import traceback

import bpy
from bpy.props import BoolProperty, EnumProperty, IntProperty, PointerProperty, StringProperty

from ..ui.ui_utils import WITCH_PT_Base
from . import bundle
from . import plugin_install
from . import unreal_project
from .socket_client import send_import_request

_UNREAL_PROJECT_ENUM_CACHE = []


def _unreal_project_enum_items(self, context):
    global _UNREAL_PROJECT_ENUM_CACHE
    items = [("MANUAL", "Manual Path", "Use the editable Project path below", 'GREASEPENCIL', 0)]
    for index, project_path in unreal_project.iter_project_paths(context):
        name = project_path.stem or project_path.name or f"Project {index + 1}"
        items.append((str(index), name, str(project_path), 'FILE_BLEND', index + 1))
    _UNREAL_PROJECT_ENUM_CACHE = items
    return _UNREAL_PROJECT_ENUM_CACHE


def _on_unreal_project_update(self, context):
    value = str(getattr(self, "project_preset", "") or "")
    if value == "MANUAL":
        return
    if not unreal_project.set_active_project_index(context, value):
        return
    project_path = unreal_project.get_active_project_path(context)
    if project_path:
        self.unreal_project = str(project_path)


def _expander(layout, settings, prop_name: str, text: str):
    """Draw a collapse/expand toggle row; returns whether the section is open."""
    expanded = bool(getattr(settings, prop_name, False))
    layout.prop(
        settings,
        prop_name,
        text=text,
        icon="TRIA_DOWN" if expanded else "TRIA_RIGHT",
        emboss=False,
    )
    return expanded


class WITCHER_PG_UnrealExportSettings(bpy.types.PropertyGroup):
    unreal_project: StringProperty(name="Project", subtype="FILE_PATH", default="")
    project_preset: EnumProperty(
        name="Project Preset",
        items=_unreal_project_enum_items,
        description="Saved Unreal project from Add-on Preferences, or Manual Path",
        update=_on_unreal_project_update,
    )
    plugin_source: StringProperty(
        name="Plugin Source Override",
        subtype="DIR_PATH",
        default="",
        description=(
            "Advanced: folder to copy the importer plugin from. Leave blank to "
            "use the plugin bundled with this add-on (the normal case)"
        ),
    )
    host: StringProperty(name="Host", default="127.0.0.1")
    port: IntProperty(name="Port", default=40777, min=1, max=65535)
    show_plugin_advanced: BoolProperty(
        name="Advanced",
        default=False,
        description="Show the plugin source override",
    )
    show_connection: BoolProperty(
        name="Connection",
        default=False,
        description="Show the Unreal live-link host and port",
    )
    export_folder: StringProperty(name="Export Folder", subtype="DIR_PATH", default="")
    content_root: StringProperty(
        name="Content Root",
        description="Unreal content folder that mirrors the Witcher depot; default roots switch between Witcher 2 and Witcher 3",
        default="/Game/Witcher3",
    )
    asset_name: StringProperty(name="Asset Name", default="")
    last_status: StringProperty(name="Status", default="Ready")
    last_plugin_target: StringProperty(name="Plugin Target", subtype="DIR_PATH", default="")
    last_manifest_path: StringProperty(name="Manifest", subtype="FILE_PATH", default="")
    last_details: StringProperty(name="Details", default="")


class WITCHER_OT_export_unreal_item(bpy.types.Operator):
    bl_idname = "witcher.export_unreal_item"
    bl_label = "Export to Unreal"
    bl_description = "Export the selected Witcher mesh/character as an Unreal import bundle"
    bl_options = {"REGISTER"}

    action: EnumProperty(
        name="Action",
        items=[
            ("BUNDLE", "Export Bundle", "Write FBX, textures, and manifest"),
            ("SEND", "Send to Unreal", "Write the bundle and send it to the running Unreal plugin"),
        ],
        default="BUNDLE",
    )

    @classmethod
    def poll(cls, context):
        return bool(getattr(context, "selected_objects", None))

    def execute(self, context):
        settings = context.scene.witcher_unreal_export
        if not settings.export_folder:
            settings.export_folder = bundle.default_export_folder()

        try:
            result = bundle.build_unreal_export_bundle(context, settings)
            settings.last_manifest_path = result["manifest_path"]
            warning_count = len(result["manifest"].get("warnings", []))
            settings.last_status = f"Bundle ready ({warning_count} warning{'s' if warning_count != 1 else ''})"
            settings.last_details = _format_bundle_details(result)

            if self.action == "SEND":
                response = send_import_request(settings.host, settings.port, result["manifest_path"])
                success = bool(response.get("success"))
                settings.last_status = "Unreal import complete" if success else "Unreal import failed"
                settings.last_details += "\n\nUnreal response:\n" + json.dumps(response, indent=2)
                if not success:
                    self.report({"ERROR"}, settings.last_status)
                    return {"CANCELLED"}

            self.report({"INFO"}, settings.last_status)
            return {"FINISHED"}
        except Exception as exc:
            settings.last_status = "Unreal export failed"
            settings.last_details = f"{exc}\n\n{traceback.format_exc()}"
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class WITCHER_OT_install_unreal_plugin(bpy.types.Operator):
    bl_idname = "witcher.install_unreal_plugin"
    bl_label = "Install/Update Plugin"
    bl_description = "Copy the WitcherToolsImporter plugin into the selected Unreal project"
    bl_options = {"REGISTER"}

    def execute(self, context):
        settings = context.scene.witcher_unreal_export
        if not settings.unreal_project.strip():
            settings.last_status = "Select Unreal project"
            settings.last_details = "Select the Unreal .uproject file before installing the plugin."
            self.report({"ERROR"}, settings.last_details)
            return {"CANCELLED"}
        if not settings.plugin_source:
            settings.plugin_source = plugin_install.default_plugin_source()

        try:
            project_file = bpy.path.abspath(settings.unreal_project.strip())
            source_dir = bpy.path.abspath(settings.plugin_source.strip())
            result = plugin_install.install_or_update_plugin(project_file, source_dir)
            settings.unreal_project = result["project_file"]
            settings.plugin_source = result["source_dir"]
            settings.last_plugin_target = result["target_dir"]
            settings.last_status = "Plugin installed" if result.get("updated") else "Plugin already installed"
            settings.last_details = plugin_install.format_install_details(result)
            self.report({"INFO"}, settings.last_status)
            return {"FINISHED"}
        except Exception as exc:
            settings.last_status = "Plugin install failed"
            settings.last_details = f"{exc}\n\n{traceback.format_exc()}"
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class WITCHER_OT_unreal_export_details(bpy.types.Operator):
    bl_idname = "witcher.unreal_export_details"
    bl_label = "Unreal Export Details"
    bl_description = "Show copyable paths and diagnostics for the last Unreal export"

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=720)

    def draw(self, context):
        settings = context.scene.witcher_unreal_export
        layout = self.layout
        col = layout.column(align=True)
        col.prop(settings, "last_plugin_target", text="Plugin")
        col.prop(settings, "last_manifest_path", text="Manifest")
        col.prop(settings, "last_details", text="Details")

    def execute(self, context):
        return {"FINISHED"}


class WITCHER_OT_unreal_project_details(bpy.types.Operator):
    bl_idname = "witcher.unreal_project_details"
    bl_label = "Unreal Project Details"
    bl_description = "Show copyable Unreal project and plugin status details"

    project_file: StringProperty(name="Project", subtype="FILE_PATH", default="")
    engine_version: StringProperty(name="Engine Version", default="Unknown")
    plugin_status: StringProperty(name="Plugin Status", default="Not checked")
    plugin_target: StringProperty(name="Plugin Target", subtype="DIR_PATH", default="")
    project_details: StringProperty(name="Details", default="")

    def invoke(self, context, event):
        settings = context.scene.witcher_unreal_export
        info = unreal_project.inspect_project(getattr(settings, "unreal_project", ""))
        self.project_file = info.get("project_file", "")
        self.engine_version = info.get("engine_association") or "Unknown"
        self.plugin_status = unreal_project.plugin_status_label(info)
        self.plugin_target = info.get("plugin_target_dir", "")
        self.project_details = unreal_project.format_project_details(info)
        return context.window_manager.invoke_props_dialog(self, width=720)

    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)
        col.prop(self, "project_file", text="Project")
        col.prop(self, "engine_version", text="Engine")
        col.prop(self, "plugin_status", text="Plugin")
        col.prop(self, "plugin_target", text="Target")
        col.prop(self, "project_details", text="Details")

    def execute(self, context):
        return {"FINISHED"}


class WITCH_PT_UnrealExport(WITCH_PT_Base, bpy.types.Panel):
    bl_idname = "WITCH_PT_UnrealExport"
    bl_label = "Unreal Export"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context):
        return context is not None and context.scene is not None

    def draw_header(self, context):
        self.layout.label(text="", icon="EXPORT")

    def draw(self, context):
        settings = context.scene.witcher_unreal_export
        info = unreal_project.inspect_project_cached(getattr(settings, "unreal_project", ""))
        layout = self.layout

        self._draw_project_box(layout, settings, info)
        self._draw_export_box(layout, settings)

    # ---- step 1: project + plugin ----

    def _draw_project_box(self, layout, settings, info):
        box = layout.box()
        box.label(text="1. Unreal Project", icon="FILE_BLEND")

        col = box.column()
        col.use_property_split = True
        col.use_property_decorate = False
        preset_row = col.row(align=True)
        preset_row.prop(settings, "project_preset", text="Project")
        preset_row.operator(
            "witcher.open_addon_preferences", text="", icon="PREFERENCES"
        )
        col.prop(settings, "unreal_project", text=".uproject")

        project_label, project_icon = unreal_project.project_status_line(info)
        status_row = box.row(align=True)
        status_row.label(text=project_label, icon=project_icon)
        status_row.operator(
            WITCHER_OT_unreal_project_details.bl_idname, text="", icon="INFO"
        )

        box.separator()

        plugin_label, plugin_icon = unreal_project.plugin_status_line(info)
        box.label(text=plugin_label, icon=plugin_icon)

        action_label, action_icon = unreal_project.plugin_action(info)
        action_row = box.row()
        # Make install/update prominent only when there is something to do.
        action_row.enabled = bool(info.get("is_uproject") and info.get("exists"))
        action_row.operator(
            WITCHER_OT_install_unreal_plugin.bl_idname, text=action_label, icon=action_icon
        )

        if _expander(box, settings, "show_plugin_advanced", "Advanced"):
            adv = box.column()
            adv.use_property_split = True
            adv.use_property_decorate = False
            adv.prop(settings, "plugin_source", text="Source Override")
            if not str(getattr(settings, "plugin_source", "") or "").strip():
                bundled = info.get("bundled_version_name") or info.get("bundled_version") or "?"
                adv.label(text=f"Using bundled plugin v{bundled}", icon="CHECKMARK")

    # ---- step 2: export / send ----

    def _draw_export_box(self, layout, settings):
        box = layout.box()
        box.label(text="2. Export Selected", icon="EXPORT")

        col = box.column()
        col.use_property_split = True
        col.use_property_decorate = False
        col.prop(settings, "content_root")
        col.prop(settings, "asset_name")
        col.prop(settings, "export_folder")

        row = box.row(align=True)
        op = row.operator(
            WITCHER_OT_export_unreal_item.bl_idname, text="Export Bundle", icon="FILE_TICK"
        )
        op.action = "BUNDLE"
        send = row.operator(
            WITCHER_OT_export_unreal_item.bl_idname, text="Send to Unreal", icon="URL"
        )
        send.action = "SEND"

        if _expander(box, settings, "show_connection", "Connection"):
            conn = box.column()
            conn.use_property_split = True
            conn.use_property_decorate = False
            conn.prop(settings, "host")
            conn.prop(settings, "port")

        status_row = box.row(align=True)
        status_row.label(text=settings.last_status or "Ready", icon="INFO")
        status_row.operator(
            WITCHER_OT_unreal_export_details.bl_idname, text="", icon="TEXT"
        )


def _format_bundle_details(result: dict) -> str:
    manifest = result.get("manifest", {})
    lines = [
        f"Asset: {result.get('asset_name', '')}",
        f"Source game: {str(manifest.get('source_game', 'w3')).upper()}",
        f"Bundle: {result.get('bundle_root', '')}",
        f"Manifest: {result.get('manifest_path', '')}",
        f"Content root: {manifest.get('content_root', '')}",
        "",
        "Meshes:",
    ]
    meshes = manifest.get("meshes", [])
    if meshes:
        lines.extend(f"- {mesh.get('asset_path', '')} ({mesh.get('kind', '')})" for mesh in meshes)
    else:
        lines.append("- none")
    rig = manifest.get("rig")
    if rig:
        lines.append(f"Rig: {rig.get('asset_path', '')}")
    blueprint = manifest.get("blueprint")
    if blueprint:
        lines.append(f"Blueprint: {blueprint.get('asset_path', '')}")
    animations = manifest.get("animations", [])
    lines.append(f"Animations: {len(animations)}")
    if animations:
        lines.extend(f"- {anim.get('asset_path', '')}" for anim in animations)
    lines += [
        f"Materials: {len(manifest.get('materials', []))}"
        f" | Masters: {len(manifest.get('masters', []))}"
        f" | Textures: {len(manifest.get('textures', []))}",
        "",
        "Warnings:",
    ]
    warnings = manifest.get("warnings", [])
    if warnings:
        lines.extend(f"- {warning}" for warning in warnings)
    else:
        lines.append("- none")
    return "\n".join(lines)


classes = (
    WITCHER_PG_UnrealExportSettings,
    WITCHER_OT_export_unreal_item,
    WITCHER_OT_install_unreal_plugin,
    WITCHER_OT_unreal_export_details,
    WITCHER_OT_unreal_project_details,
    WITCH_PT_UnrealExport,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.witcher_unreal_export = PointerProperty(type=WITCHER_PG_UnrealExportSettings)


def unregister():
    if hasattr(bpy.types.Scene, "witcher_unreal_export"):
        del bpy.types.Scene.witcher_unreal_export
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
