"""Blender operators and UI for Unreal export bundles."""

from __future__ import annotations

import json
import traceback

import bpy
from bpy.props import EnumProperty, IntProperty, PointerProperty, StringProperty

from ..ui.ui_utils import WITCH_PT_Base
from . import bundle
from . import plugin_install
from .socket_client import send_import_request


class WITCHER_PG_UnrealExportSettings(bpy.types.PropertyGroup):
    unreal_project: StringProperty(name="Project", subtype="FILE_PATH", default="")
    plugin_source: StringProperty(name="Plugin Source", subtype="DIR_PATH", default="")
    host: StringProperty(name="Host", default="127.0.0.1")
    port: IntProperty(name="Port", default=40777, min=1, max=65535)
    export_folder: StringProperty(name="Export Folder", subtype="DIR_PATH", default="")
    content_root: StringProperty(
        name="Content Root",
        description="Unreal content folder that mirrors the Witcher depot (game-relative asset paths are created below it)",
        default="/Game/ImportedFbx",
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
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        plugin_box = layout.box()
        plugin_box.label(text="Project Plugin", icon="PLUGIN")
        plugin_box.prop(settings, "unreal_project")
        plugin_box.prop(settings, "plugin_source")
        plugin_box.operator(WITCHER_OT_install_unreal_plugin.bl_idname, text="Install/Update", icon="IMPORT")

        box = layout.box()
        box.label(text="Export to Unreal", icon="EXPORT")
        box.prop(settings, "host")
        box.prop(settings, "port")
        box.prop(settings, "export_folder")
        box.prop(settings, "content_root")
        box.prop(settings, "asset_name")

        row = box.row(align=True)
        op = row.operator(WITCHER_OT_export_unreal_item.bl_idname, text="Export Bundle", icon="FILE_TICK")
        op.action = "BUNDLE"
        op = row.operator(WITCHER_OT_export_unreal_item.bl_idname, text="Send to Unreal", icon="URL")
        op.action = "SEND"

        status_row = box.row(align=True)
        status_row.label(text=settings.last_status or "Ready", icon="INFO")
        status_row.operator(WITCHER_OT_unreal_export_details.bl_idname, text="", icon="TEXT")


def _format_bundle_details(result: dict) -> str:
    manifest = result.get("manifest", {})
    lines = [
        f"Asset: {result.get('asset_name', '')}",
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
