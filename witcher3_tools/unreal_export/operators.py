"""Blender operators and UI for Unreal export bundles."""

from __future__ import annotations

import json
import logging
import time
import traceback
from contextlib import contextmanager

import bpy
from bpy.props import BoolProperty, EnumProperty, IntProperty, PointerProperty, StringProperty

from ..ui.ui_utils import WITCH_PT_Base
from . import bundle
from . import manifest
from . import world_bundle
from . import placements_bundle
from . import w2l_placements
from . import plugin_install
from . import unreal_project
from .socket_client import probe_import_server, send_import_request

_UNREAL_PROJECT_ENUM_CACHE = []


@contextmanager
def _quiet_send_logging(settings, action: str):
    if action != "SEND" or not bool(getattr(settings, "quiet_send_logging", True)):
        yield
        return

    saved = []
    seen = set()
    package_root = __name__.split(".unreal_export", 1)[0]
    loggers = [logging.getLogger(package_root), logging.getLogger("witcher3_tools")]
    for name, logger in logging.Logger.manager.loggerDict.items():
        if isinstance(logger, logging.Logger) and "witcher3_tools" in name:
            loggers.append(logger)

    for logger in loggers:
        if id(logger) in seen:
            continue
        seen.add(id(logger))
        saved.append((logger, logger.level))
        if logger.getEffectiveLevel() < logging.WARNING:
            logger.setLevel(logging.WARNING)
    try:
        yield
    finally:
        for logger, level in saved:
            logger.setLevel(level)


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


def _preflight_send_connection(settings) -> str:
    return probe_import_server(settings.host, settings.port, timeout=1.0)


def _cancel_unreachable_unreal(operator, settings, message: str):
    settings.last_status = "Unreal import server not reachable"
    settings.last_details = message
    operator.report({"ERROR"}, message)
    return {"CANCELLED"}


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
    show_placements_advanced: BoolProperty(
        name="Placement Options",
        default=False,
        description="Show placement export performance options",
    )
    show_overwrite: BoolProperty(
        name="Overwrite Policy",
        default=False,
        description="Show which existing Unreal assets a re-import is allowed to replace",
    )
    # Per-category overwrite policy. All default False (reuse every existing
    # Unreal asset) -- the historical "existing UE assets win" behaviour. These
    # serialize into the manifest's ``overwrite`` block; see manifest.py.
    overwrite_meshes: BoolProperty(
        name="Meshes",
        default=False,
        description="Re-export and reimport static/skeletal meshes, replacing the existing Unreal assets",
    )
    overwrite_skeletons: BoolProperty(
        name="Skeletons",
        default=False,
        description="Reimport the rig skeleton, replacing the existing Unreal Skeleton asset",
    )
    overwrite_animations: BoolProperty(
        name="Animations",
        default=False,
        description="Reimport animations, replacing existing Unreal AnimSequences",
    )
    overwrite_blueprints: BoolProperty(
        name="Blueprints",
        default=False,
        description="Rebuild character/entity blueprints, replacing existing ones",
    )
    overwrite_material_instances: BoolProperty(
        name="Material Instances",
        default=False,
        description="Refresh material-instance parameters and parents on existing Unreal MIs",
    )
    overwrite_materials_base: BoolProperty(
        name="Base Materials",
        default=False,
        description=(
            "Rebuild generated base UMaterial master materials. Off by default to "
            "protect hand-authored masters at mirrored depot paths"
        ),
    )
    overwrite_textures: BoolProperty(
        name="Textures",
        default=False,
        description=(
            "Reimport textures, replacing existing Unreal texture assets. Off by "
            "default to protect hand-tweaked texture import settings"
        ),
    )
    prefer_source_buffers: BoolProperty(
        name="Fast Mesh Buffers",
        default=True,
        description=(
            "Send unedited meshes straight from the source .w2mesh as decoded buffers "
            "(skips the FBX round-trip). Edited meshes still export through FBX"
        ),
    )
    quiet_send_logging: BoolProperty(
        name="Quiet SEND Logging",
        default=True,
        description="Reduce Blender console logging while building and sending live Unreal bundles",
    )
    placement_export_collision: BoolProperty(
        name="Collision",
        default=False,
        description="Export RED collision meshes as hidden Unreal collision actors. Slower",
    )
    placement_write_profile_log: BoolProperty(
        name="Profile Log",
        default=True,
        description="Write a timing log for placement export phases",
    )
    placement_skip_materials: BoolProperty(
        name="Fast (geometry only)",
        default=False,
        description=(
            "Send .w2l layer geometry + placement only, with Unreal's default material "
            "(fastest, for placement iteration). Off by default; full material sends "
            "stage cached PNG textures and use packed alpha in the Unreal shader"
        ),
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
        if self.action == "SEND":
            connection_error = _preflight_send_connection(settings)
            if connection_error:
                return _cancel_unreachable_unreal(self, settings, connection_error)

        try:
            with _quiet_send_logging(settings, self.action):
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


class WITCHER_OT_export_unreal_world(bpy.types.Operator):
    bl_idname = "witcher.export_unreal_world"
    bl_label = "Export World Terrain to Unreal"
    bl_description = (
        "Export the selected full-map terrain as an Unreal Landscape bundle "
        "(heightmap, transform, water). Import the world's .w2w terrain in Full "
        "Map mode and select it first"
    )
    bl_options = {"REGISTER"}

    action: EnumProperty(
        name="Action",
        items=[
            ("BUNDLE", "Export Bundle", "Write the terrain R16, textures, and manifest"),
            ("SEND", "Send to Unreal", "Write the bundle and send it to the running Unreal plugin"),
        ],
        default="BUNDLE",
    )

    @classmethod
    def poll(cls, context):
        return context is not None and context.scene is not None

    def execute(self, context):
        settings = context.scene.witcher_unreal_export
        if not settings.export_folder:
            settings.export_folder = bundle.default_export_folder()
        if self.action == "SEND":
            connection_error = _preflight_send_connection(settings)
            if connection_error:
                return _cancel_unreachable_unreal(self, settings, connection_error)

        try:
            with _quiet_send_logging(settings, self.action):
                result = world_bundle.build_unreal_world_bundle(context, settings)
                settings.last_manifest_path = result["manifest_path"]
                warning_count = len(result["manifest"].get("warnings", []))
                settings.last_status = f"World bundle ready ({warning_count} warning{'s' if warning_count != 1 else ''})"
                settings.last_details = _format_world_bundle_details(result)

                if self.action == "SEND":
                    response = send_import_request(settings.host, settings.port, result["manifest_path"])
                    success = bool(response.get("success"))
                    settings.last_status = "Unreal terrain import complete" if success else "Unreal terrain import failed"
                    settings.last_details += "\n\nUnreal response:\n" + json.dumps(response, indent=2)
                    if not success:
                        self.report({"ERROR"}, settings.last_status)
                        return {"CANCELLED"}

            self.report({"INFO"}, settings.last_status)
            return {"FINISHED"}
        except Exception as exc:
            settings.last_status = "Unreal world export failed"
            settings.last_details = f"{exc}\n\n{traceback.format_exc()}"
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class WITCHER_OT_export_unreal_placements(bpy.types.Operator):
    bl_idname = "witcher.export_unreal_placements"
    bl_label = "Export World Placements to Unreal"
    bl_description = (
        "Export the CSectorData placements in the selected layer(s) as Unreal "
        "StaticMeshActors and instanced static meshes (HISM) positioned on the exported Landscape. "
        "Load layers with 'Load Layers Around Camera', then select the layer "
        "collection(s) and send; re-send more layers to fill the map in"
    )
    bl_options = {"REGISTER"}

    action: EnumProperty(
        name="Action",
        items=[
            ("BUNDLE", "Export Bundle", "Write the placement FBXs, textures, and manifest"),
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
        if self.action == "SEND":
            connection_error = _preflight_send_connection(settings)
            if connection_error:
                return _cancel_unreachable_unreal(self, settings, connection_error)

        try:
            with _quiet_send_logging(settings, self.action):
                result = placements_bundle.build_unreal_placements_bundle(context, settings)
                settings.last_manifest_path = result["manifest_path"]
                warning_count = len(result["manifest"].get("warnings", []))
                settings.last_status = f"Placements bundle ready ({warning_count} warning{'s' if warning_count != 1 else ''})"
                settings.last_details = _format_placements_bundle_details(result)

                if self.action == "SEND":
                    send_started = time.perf_counter()
                    settings.last_status = "Sending placements to Unreal"
                    response = send_import_request(settings.host, settings.port, result["manifest_path"])
                    send_seconds = time.perf_counter() - send_started
                    success = bool(response.get("success"))
                    settings.last_status = "Unreal placements import complete" if success else "Unreal placements import failed"
                    settings.last_details += (
                        f"\n\nUnreal send/import time: {send_seconds:.3f}s"
                        "\n\nUnreal response:\n"
                        + json.dumps(response, indent=2)
                    )
                    if not success:
                        self.report({"ERROR"}, settings.last_status)
                        return {"CANCELLED"}

            self.report({"INFO"}, settings.last_status)
            return {"FINISHED"}
        except Exception as exc:
            settings.last_status = "Unreal placements export failed"
            settings.last_details = f"{exc}\n\n{traceback.format_exc()}"
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class WITCHER_OT_export_unreal_w2l(bpy.types.Operator):
    bl_idname = "witcher.export_unreal_w2l"
    bl_label = "Send .w2l Layer to Unreal"
    bl_description = (
        "Parse a Witcher .w2l layer file directly and send its placed meshes to "
        "Unreal as positioned actors on the exported Landscape -- without importing "
        "anything into Blender. Re-send more layers to fill the map in"
    )
    bl_options = {"REGISTER"}

    w2l_path: StringProperty(
        name=".w2l",
        description="Depot or absolute path to the .w2l layer file",
        subtype="FILE_PATH",
        default="",
    )
    action: EnumProperty(
        name="Action",
        items=[
            ("BUNDLE", "Export Bundle", "Write the layer buffers, textures, and manifest"),
            ("SEND", "Send to Unreal", "Write the bundle and send it to the running Unreal plugin"),
        ],
        default="SEND",
    )

    @classmethod
    def poll(cls, context):
        return context is not None and context.scene is not None

    def execute(self, context):
        button_started = time.perf_counter()
        settings = context.scene.witcher_unreal_export
        if not settings.export_folder:
            settings.export_folder = bundle.default_export_folder()
        if not str(getattr(self, "w2l_path", "") or "").strip():
            settings.last_status = "No .w2l selected"
            settings.last_details = "Select a .w2l layer file before sending to Unreal."
            self.report({"ERROR"}, settings.last_details)
            return {"CANCELLED"}
        if self.action == "SEND":
            connection_error = _preflight_send_connection(settings)
            if connection_error:
                return _cancel_unreachable_unreal(self, settings, connection_error)

        try:
            with _quiet_send_logging(settings, self.action):
                result = w2l_placements.build_unreal_w2l_bundle(context, settings, self.w2l_path)
                settings.last_manifest_path = result["manifest_path"]
                warning_count = len(result["manifest"].get("warnings", []))
                settings.last_status = f"Layer bundle ready ({warning_count} warning{'s' if warning_count != 1 else ''})"
                settings.last_details = _format_w2l_bundle_details(result)

                if self.action == "SEND":
                    settings.last_status = "Sending layer to Unreal"
                    send_started = time.perf_counter()
                    response = send_import_request(settings.host, settings.port, result["manifest_path"])
                    send_seconds = time.perf_counter() - send_started
                    total_seconds = time.perf_counter() - button_started
                    success = bool(response.get("success"))
                    settings.last_status = "Unreal layer import complete" if success else "Unreal layer import failed"
                    timing_report = _format_send_timing_report(
                        result.get("asset_name", "layer"),
                        total_seconds,
                        float(result.get("elapsed_seconds", 0.0) or 0.0),
                        send_seconds,
                        response,
                        result.get("build_timings"),
                    )
                    print(timing_report)
                    settings.last_details += "\n\n" + timing_report
                    settings.last_details += "\n\nUnreal response:\n" + json.dumps(response, indent=2)
                    if not success:
                        self.report({"ERROR"}, settings.last_status)
                        return {"CANCELLED"}
                    settings.last_status += f" ({total_seconds:.1f}s)"

            self.report({"INFO"}, settings.last_status)
            return {"FINISHED"}
        except Exception as exc:
            settings.last_status = "Unreal layer export failed"
            settings.last_details = f"{exc}\n\n{traceback.format_exc()}"
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class WITCHER_OT_send_unreal_layers_around_camera(bpy.types.Operator):
    bl_idname = "witcher.send_unreal_layers_around_camera"
    bl_label = "Send Layers Around Camera to Unreal"
    bl_description = (
        "Parse every .w2l layer near the viewport camera (using the world layer "
        "scan cache) and send their placed meshes to Unreal as positioned actors "
    )
    bl_options = {"REGISTER"}

    action: EnumProperty(
        name="Action",
        items=[
            ("BUNDLE", "Export Bundle", "Write the layer buffers, textures, and manifest"),
            ("SEND", "Send to Unreal", "Write the bundle and send it to the running Unreal plugin"),
        ],
        default="SEND",
    )

    @classmethod
    def poll(cls, context):
        return context is not None and context.scene is not None

    def execute(self, context):
        button_started = time.perf_counter()
        from ..ui.ui_map import select_nearby_w2l_paths

        settings = context.scene.witcher_unreal_export
        if not settings.export_folder:
            settings.export_folder = bundle.default_export_folder()

        try:
            selection = select_nearby_w2l_paths(context)
        except ValueError as exc:
            settings.last_status = "No nearby layers"
            settings.last_details = str(exc)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        w2l_paths = selection.get("paths", [])
        if not w2l_paths:
            radius = selection.get("radius", 0.0)
            msg = (
                f"No .w2l layers found within {radius:.0f} world units of the camera "
                f"(checked {selection.get('candidate_count', 0)} cached layers)."
            )
            settings.last_status = "No nearby layers"
            settings.last_details = msg
            self.report({"WARNING"}, msg)
            return {"CANCELLED"}

        if self.action == "SEND":
            connection_error = _preflight_send_connection(settings)
            if connection_error:
                return _cancel_unreachable_unreal(self, settings, connection_error)

        try:
            with _quiet_send_logging(settings, self.action):
                scene_settings = getattr(getattr(context, "scene", None), "witcher_file_browser", None)
                include_collision_blocks = bool(
                    getattr(settings, "placement_export_collision", False)
                    or getattr(scene_settings, "terrain_layer_do_import_collision", False)
                )
                result = w2l_placements.build_unreal_w2l_bundle_multi(
                    context,
                    settings,
                    w2l_paths,
                    include_collision_blocks=include_collision_blocks,
                    include_point_lights=bool(getattr(scene_settings, "terrain_layer_do_import_point_light", True)),
                    include_spot_lights=bool(getattr(scene_settings, "terrain_layer_do_import_spot_light", True)),
                )
                settings.last_manifest_path = result["manifest_path"]
                warning_count = len(result["manifest"].get("warnings", []))
                settings.last_status = (
                    f"Layers bundle ready ({len(w2l_paths)} layers, "
                    f"{warning_count} warning{'s' if warning_count != 1 else ''})"
                )
                settings.last_details = _format_w2l_multi_bundle_details(result, selection)

                if self.action == "SEND":
                    settings.last_status = f"Sending {len(w2l_paths)} layers to Unreal"
                    send_started = time.perf_counter()
                    response = send_import_request(settings.host, settings.port, result["manifest_path"])
                    send_seconds = time.perf_counter() - send_started
                    total_seconds = time.perf_counter() - button_started
                    success = bool(response.get("success"))
                    settings.last_status = (
                        "Unreal layers import complete" if success else "Unreal layers import failed"
                    )
                    timing_report = _format_send_timing_report(
                        f"{len(w2l_paths)} layers around camera",
                        total_seconds,
                        float(result.get("elapsed_seconds", 0.0) or 0.0),
                        send_seconds,
                        response,
                        result.get("build_timings"),
                    )
                    print(timing_report)
                    settings.last_details += "\n\n" + timing_report
                    settings.last_details += "\n\nUnreal response:\n" + json.dumps(response, indent=2)
                    if not success:
                        self.report({"ERROR"}, settings.last_status)
                        return {"CANCELLED"}
                    settings.last_status += f" ({total_seconds:.1f}s)"

            self.report({"INFO"}, settings.last_status)
            return {"FINISHED"}
        except Exception as exc:
            settings.last_status = "Unreal layers export failed"
            settings.last_details = f"{exc}\n\n{traceback.format_exc()}"
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class WITCHER_OT_unreal_overwrite_preset(bpy.types.Operator):
    bl_idname = "witcher.unreal_overwrite_preset"
    bl_label = "Overwrite Preset"
    bl_description = "Apply an overwrite policy preset to the per-category options below"
    bl_options = {"REGISTER", "UNDO"}

    preset: EnumProperty(
        name="Preset",
        items=[
            ("reuse_all", "Reuse All", "Never overwrite; reuse every existing Unreal asset"),
            ("overwrite_all", "Overwrite All",
             "Overwrite every category, including base materials and textures"),
            ("overwrite_except_base", "Overwrite (keep base mats & textures)",
             "Overwrite everything except base materials and textures; material instances are still refreshed"),
        ],
        default="reuse_all",
    )

    def execute(self, context):
        settings = context.scene.witcher_unreal_export
        for key, value in manifest.overwrite_preset(self.preset).items():
            setattr(settings, f"overwrite_{key}", bool(value))
        settings.show_overwrite = True
        self.report({"INFO"}, f"Overwrite preset applied: {self.preset}")
        return {"FINISHED"}


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

        self._draw_overwrite_policy(box, settings)

        row = box.row(align=True)
        op = row.operator(
            WITCHER_OT_export_unreal_item.bl_idname, text="Export Bundle", icon="FILE_TICK"
        )
        op.action = "BUNDLE"
        send = row.operator(
            WITCHER_OT_export_unreal_item.bl_idname, text="Send to Unreal", icon="URL"
        )
        send.action = "SEND"

        box.separator()
        box.label(text="World Terrain (Landscape)", icon="WORLD")
        world_row = box.row(align=True)
        w_bundle = world_row.operator(
            WITCHER_OT_export_unreal_world.bl_idname, text="Export Terrain", icon="FILE_TICK"
        )
        w_bundle.action = "BUNDLE"
        w_send = world_row.operator(
            WITCHER_OT_export_unreal_world.bl_idname, text="Send Terrain", icon="URL"
        )
        w_send.action = "SEND"

        box.separator()
        box.label(text="World Placements (Buildings/Props)", icon="MESH_DATA")
        placement_row = box.row(align=True)
        p_bundle = placement_row.operator(
            WITCHER_OT_export_unreal_placements.bl_idname, text="Export Placements", icon="FILE_TICK"
        )
        p_bundle.action = "BUNDLE"
        p_send = placement_row.operator(
            WITCHER_OT_export_unreal_placements.bl_idname, text="Send Placements", icon="URL"
        )
        p_send.action = "SEND"
        box.label(
            text="'Send Nearby Layers to Unreal' is under World > Load Layers",
            icon="INFO",
        )

        if _expander(box, settings, "show_placements_advanced", "Placement Options"):
            placement_options = box.column(align=True)
            placement_options.use_property_split = True
            placement_options.use_property_decorate = False
            placement_options.prop(settings, "placement_skip_materials", text="Fast .w2l (geometry only)")
            placement_options.prop(settings, "placement_export_collision", text="Collision")
            placement_options.prop(settings, "placement_write_profile_log", text="Profile Log")
            placement_options.label(text="FBX reuse follows Overwrite Policy > Meshes", icon="INFO")

        if _expander(box, settings, "show_connection", "Connection"):
            conn = box.column()
            conn.use_property_split = True
            conn.use_property_decorate = False
            conn.prop(settings, "host")
            conn.prop(settings, "port")
            conn.prop(settings, "quiet_send_logging")

        status_row = box.row(align=True)
        status_row.label(text=settings.last_status or "Ready", icon="INFO")
        status_row.operator(
            WITCHER_OT_unreal_export_details.bl_idname, text="", icon="TEXT"
        )

    def _draw_overwrite_policy(self, box, settings):
        if not _expander(box, settings, "show_overwrite", "Overwrite Policy"):
            return
        ow = box.column(align=True)
        ow.label(text="Replace existing Unreal assets on import:")
        preset_row = ow.row(align=True)
        for value, label in (
            ("reuse_all", "Reuse All"),
            ("overwrite_all", "Overwrite All"),
            ("overwrite_except_base", "All but Base/Tex"),
        ):
            preset_row.operator(
                WITCHER_OT_unreal_overwrite_preset.bl_idname, text=label
            ).preset = value
        cats = ow.column(align=True)
        cats.use_property_split = True
        cats.use_property_decorate = False
        cats.prop(settings, "overwrite_meshes")
        cats.prop(settings, "overwrite_skeletons")
        cats.prop(settings, "overwrite_animations")
        cats.prop(settings, "overwrite_blueprints")
        cats.prop(settings, "overwrite_material_instances")
        cats.prop(settings, "overwrite_materials_base")
        cats.prop(settings, "overwrite_textures")


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


def _format_placements_bundle_details(result: dict) -> str:
    manifest = result.get("manifest", {})
    placements = manifest.get("placements", {}) or {}
    layers = placements.get("layers", []) or []
    light_total = sum(len((group.get("lights", []) or [])) for group in layers)
    meshes = manifest.get("meshes", []) or []
    collision_meshes = [mesh for mesh in meshes if mesh.get("collision")]
    visual_meshes = [mesh for mesh in meshes if not mesh.get("collision")]
    profile = result.get("profile", {}) or {}
    profile_counts = profile.get("counts", {}) or {}
    profile_totals = profile.get("totals", {}) or {}
    profile_path = result.get("profile_path", "")
    lines = [
        f"Asset: {result.get('asset_name', '')}",
        f"Bundle: {result.get('bundle_root', '')}",
        f"Manifest: {result.get('manifest_path', '')}",
        f"Profile: {profile_path or '(not written)'}",
        f"Content root: {manifest.get('content_root', '')}",
        "",
        f"Visual meshes: {len(visual_meshes)}"
        f" | Collision meshes: {len(collision_meshes)}"
        f" | Lights: {light_total}"
        f" | Layers: {len(layers)}",
        f"Total time: {profile.get('total_seconds', 0.0):.3f}s"
        f" | FBX: {profile_totals.get('visual_fbx_export', 0.0):.3f}s"
        f" | Materials: {profile_totals.get('material_scan', 0.0):.3f}s"
        f" | Collision: {profile_totals.get('collision_fbx_export', 0.0):.3f}s",
        f"FBX exported: {profile_counts.get('visual_fbx_exported', 0)}"
        f" | reused: {profile_counts.get('visual_fbx_reused', 0)}"
        f" | collision exported: {profile_counts.get('collision_fbx_exported', 0)}"
        f" | collision reused: {profile_counts.get('collision_fbx_reused', 0)}",
        "",
        "Placements per layer:",
    ]
    if layers:
        for group in layers:
            actors = group.get("actors", []) or []
            instancers = group.get("instancers", []) or []
            lights = group.get("lights", []) or []
            inst_total = sum(len(i.get("instances", [])) for i in instancers)
            folder = group.get("folder", "")
            lines.append(
                f"- {folder + '/' if folder else ''}{group.get('label', '')}:"
                f" {len(actors)} actor(s)"
                + (f", {inst_total} instanced" if inst_total else "")
                + (f", {len(lights)} light(s)" if lights else "")
            )
    else:
        lines.append("- none")
    lines += ["", "Warnings:"]
    warnings = manifest.get("warnings", [])
    if warnings:
        lines.extend(f"- {warning}" for warning in warnings)
    else:
        lines.append("- none")
    return "\n".join(lines)


def _format_send_timing_report(
    label: str,
    total_seconds: float,
    build_seconds: float,
    send_seconds: float,
    response: dict,
    build_timings: dict | None = None,
) -> str:
    ue_total = float(response.get("total_seconds", 0.0) or 0.0)
    timings = response.get("timings", {}) or {}
    asset_count = int(response.get("imported_asset_count", 0) or 0)
    bar = "=" * 58
    lines = [
        bar,
        f"Send-to-Unreal timing: {label}",
        f"  Button press -> loaded:  {total_seconds:7.2f} s",
        f"    Blender build:         {build_seconds:7.2f} s",
    ]
    for name, secs in sorted((build_timings or {}).items(), key=lambda kv: float(kv[1] or 0.0), reverse=True):
        secs = float(secs or 0.0)
        if secs >= 0.0005:
            lines.append(f"        {name:<12} {secs:7.2f} s")
    lines.append(f"    Send + Unreal import:  {send_seconds:7.2f} s")
    if ue_total > 0.0 or timings:
        lines.append(f"      Unreal total:        {ue_total:7.2f} s")
        for name, secs in sorted(timings.items(), key=lambda kv: float(kv[1] or 0.0), reverse=True):
            secs = float(secs or 0.0)
            if secs >= 0.0005:
                lines.append(f"        {name:<12} {secs:7.2f} s")
        lines.append(f"      network/overhead:    {max(0.0, send_seconds - ue_total):7.2f} s")
    else:
        lines.append("      (rebuild the Unreal plugin for the per-phase breakdown)")
    lines.append(f"  Unreal assets imported: {asset_count}")
    lines.append(bar)
    return "\n".join(lines)


def _format_w2l_bundle_details(result: dict) -> str:
    manifest = result.get("manifest", {})
    counts = result.get("counts", {}) or {}
    skipped = counts.get("skipped", {}) or {}
    lines = [
        f"Layer: {result.get('layer_id', '')}",
        f"Asset: {result.get('asset_name', '')}",
        f"Bundle: {result.get('bundle_root', '')}",
        f"Manifest: {result.get('manifest_path', '')}",
        f"Content root: {manifest.get('content_root', '')}",
        f"Source game: {str(manifest.get('source_game', 'w3')).upper()}",
        f"Mode: {'FAST (geometry only, default material)' if result.get('skip_materials') else 'full materials + textures'}",
        "",
        f"Unique meshes: {counts.get('unique_meshes', 0)}"
        f" | Actors: {counts.get('actors', 0)}"
        f" | Instancers: {counts.get('instancers', 0)}"
        f" ({counts.get('instances', 0)} instances)",
        f"Build time: {result.get('elapsed_seconds', 0.0):.3f}s",
    ]
    if skipped:
        lines.append("Skipped blocks: " + ", ".join(f"{key}={value}" for key, value in sorted(skipped.items())))
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


def _format_w2l_multi_bundle_details(result: dict, selection: dict | None = None) -> str:
    manifest = result.get("manifest", {})
    counts = result.get("counts", {}) or {}
    skipped = counts.get("skipped", {}) or {}
    selection = selection or {}
    lines = [
        f"Asset: {result.get('asset_name', '')}",
        f"Bundle: {result.get('bundle_root', '')}",
        f"Manifest: {result.get('manifest_path', '')}",
        f"Content root: {manifest.get('content_root', '')}",
        f"Source game: {str(manifest.get('source_game', 'w3')).upper()}",
        f"Mode: {'FAST (geometry only, default material)' if result.get('skip_materials') else 'full materials + textures'}",
        "",
    ]
    if selection:
        cam = selection.get("camera_position") or (0.0, 0.0, 0.0)
        lines.append(
            f"Camera: ({cam[0]:.1f}, {cam[1]:.1f}, {cam[2]:.1f})"
            f" | Radius: {selection.get('radius', 0.0):.0f}"
            f" | Cached layers in range: {selection.get('candidate_count', 0)}"
        )
    lines += [
        f"Layers sent: {counts.get('layers', 0)}"
        f" (with placements: {counts.get('layers_with_placements', 0)})",
        f"Unique meshes: {counts.get('unique_meshes', 0)}"
        f" | Actors: {counts.get('actors', 0)}"
        f" | Instancers: {counts.get('instancers', 0)}"
        f" ({counts.get('instances', 0)} instances)",
        f"Materials: {len(manifest.get('materials', []))}"
        f" | Masters: {len(manifest.get('masters', []))}"
        f" | Textures: {len(manifest.get('textures', []))}",
        f"Build time: {result.get('elapsed_seconds', 0.0):.3f}s",
    ]
    if skipped:
        lines.append("Skipped blocks: " + ", ".join(f"{key}={value}" for key, value in sorted(skipped.items())))
    unresolved = selection.get("unresolved") or []
    if unresolved:
        lines.append(f"Unresolved layer files: {len(unresolved)}")
    lines += ["", "Warnings:"]
    warnings = manifest.get("warnings", [])
    if warnings:
        lines.extend(f"- {warning}" for warning in warnings)
    else:
        lines.append("- none")
    return "\n".join(lines)


def _format_world_bundle_details(result: dict) -> str:
    manifest = result.get("manifest", {})
    terrain = manifest.get("terrain", {}) or {}
    transform = terrain.get("transform", {}) or {}
    elevation = terrain.get("elevation", {}) or {}
    lines = [
        f"Asset: {result.get('asset_name', '')}",
        f"Bundle: {result.get('bundle_root', '')}",
        f"Manifest: {result.get('manifest_path', '')}",
        f"Content root: {manifest.get('content_root', '')}",
        "",
        "Terrain:",
        f"- Landscape asset: {terrain.get('asset_path', '')}",
        f"- Heightmap R16: {terrain.get('heightmap_r16', '')}",
        f"- Resolution: {terrain.get('resolution', '')} (source {terrain.get('source_resolution', '')})",
        f"- Components/axis: {terrain.get('component_count_per_axis', '')}"
        f" @ {terrain.get('subsection_size_quads', '')} quads x {terrain.get('num_subsections', '')} sub",
        f"- Terrain size: {terrain.get('terrain_size', '')} m",
        f"- Elevation: {elevation.get('lowest', '')} .. {elevation.get('highest', '')} m",
        f"- Location (cm): {transform.get('location', '')}",
        f"- Scale (cm): {transform.get('scale', '')}",
        f"- Base colour tex: {terrain.get('base_color_texture', '') or '(none)'}",
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
    WITCHER_OT_export_unreal_world,
    WITCHER_OT_export_unreal_placements,
    WITCHER_OT_export_unreal_w2l,
    WITCHER_OT_send_unreal_layers_around_camera,
    WITCHER_OT_unreal_overwrite_preset,
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
