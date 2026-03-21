"""Blender UI for Witcher 3 texture export and DDS conversion."""

from __future__ import annotations

import logging
import os

import bpy
from bpy.props import EnumProperty, PointerProperty, StringProperty
from bpy_extras.io_utils import ExportHelper

from .. import get_texture_path, get_uncook_path
from ..exporters.texture_groups import get_texture_group_enum_items, get_texture_group_info
from .ui_mesh import _compute_full_export_path, _get_active_redkit_project, _get_workspace_root

log = logging.getLogger(__name__)

_TEXTURE_EXPORT_IMAGE_KEY = "_w3_texture_export_image_name"
_TEXTURE_GROUP_ENUM_ITEMS = get_texture_group_enum_items()


def _get_active_image(context):
    """Get the active image from the Image Editor."""
    space = getattr(context, "space_data", None)
    if space and getattr(space, "type", None) == 'IMAGE_EDITOR' and getattr(space, "image", None) is not None:
        return space.image

    screen = getattr(context, "screen", None)
    if screen:
        for area in screen.areas:
            if area.type != 'IMAGE_EDITOR':
                continue
            image = getattr(area.spaces.active, "image", None)
            if image is not None:
                return image
    return None


def _remember_export_image(context, image):
    wm = getattr(context, "window_manager", None)
    if wm is None:
        return
    if image is None:
        try:
            del wm[_TEXTURE_EXPORT_IMAGE_KEY]
        except Exception:
            pass
        return
    wm[_TEXTURE_EXPORT_IMAGE_KEY] = image.name


def _get_export_target_image(context):
    image = _get_active_image(context)
    if image is not None:
        _remember_export_image(context, image)
        return image

    wm = getattr(context, "window_manager", None)
    image_name = str(getattr(wm, "get", lambda *_: "")(_TEXTURE_EXPORT_IMAGE_KEY, "") or "")
    return bpy.data.images.get(image_name) if image_name else None


def _get_image_source_path(image):
    source_path = ""
    filepath_from_user = getattr(image, "filepath_from_user", None)
    if callable(filepath_from_user):
        try:
            source_path = filepath_from_user() or ""
        except Exception:
            source_path = ""
    if not source_path:
        source_path = getattr(image, "filepath_raw", "") or getattr(image, "filepath", "") or ""
    return bpy.path.abspath(source_path) if source_path else ""


def _normalize_repo_path(path_value: str) -> str:
    return str(path_value or "").replace("/", "\\").lstrip("\\")


def _repo_path_from_root(filepath: str, root: str) -> str:
    if not filepath or not root:
        return ""

    path_abs = os.path.normcase(os.path.normpath(bpy.path.abspath(filepath)))
    root_abs = os.path.normcase(os.path.normpath(bpy.path.abspath(root)))
    if path_abs != root_abs and not path_abs.startswith(root_abs + os.sep):
        return ""

    try:
        rel_path = os.path.relpath(path_abs, root_abs)
    except ValueError:
        return ""

    if rel_path == ".":
        return ""
    return rel_path.replace("/", "\\")


def _normalize_source_path(filepath: str) -> str:
    if not filepath:
        return ""
    return os.path.normpath(bpy.path.abspath(filepath))


def _find_metadata_xbm_path(filepath: str) -> str:
    normalized_path = _normalize_source_path(filepath)
    if not normalized_path:
        return ""

    base, ext = os.path.splitext(normalized_path)
    if ext.lower() == ".xbm":
        return normalized_path

    xbm_path = base + ".xbm"
    return xbm_path if os.path.isfile(xbm_path) else ""


def _derive_texture_repo_path(context, filepath: str) -> str:
    if not filepath:
        return ""

    project_path = _get_active_redkit_project(context)
    workspace_root = _get_workspace_root(project_path) if project_path else None
    candidates = [
        workspace_root,
        get_texture_path(context),
        get_uncook_path(context),
    ]
    for root in candidates:
        repo_path = _repo_path_from_root(filepath, root)
        if repo_path:
            break
    else:
        return ""

    base, _ext = os.path.splitext(repo_path)
    return f"{base}.xbm"


def resolve_texture_image_metadata(context, source_path, *, repo_path="", texture_group="Default"):
    normalized_path = _normalize_source_path(source_path)
    resolved_repo_path = _normalize_repo_path(repo_path)
    resolved_texture_group = texture_group or "Default"
    xbm_path = _find_metadata_xbm_path(normalized_path)

    if xbm_path and os.path.isfile(xbm_path):
        derived_repo_path = _derive_texture_repo_path(context, xbm_path)
        if derived_repo_path:
            resolved_repo_path = derived_repo_path
        resolved_texture_group = read_xbm_texture_group(xbm_path) or resolved_texture_group
    elif not resolved_repo_path and normalized_path:
        derived_repo_path = _derive_texture_repo_path(context, normalized_path)
        if derived_repo_path:
            resolved_repo_path = derived_repo_path

    return resolved_repo_path, resolved_texture_group


def _update_repo_path_from_export_path(context, image, export_filepath):
    project_path = _get_active_redkit_project(context)
    workspace_root = _get_workspace_root(project_path) if project_path else None
    if not workspace_root or image is None:
        return

    repo_path = _repo_path_from_root(export_filepath, workspace_root)
    if repo_path:
        image.witcherui_TextureSettings.repo_path = repo_path


def apply_texture_image_metadata(context, image, source_path, *, repo_path="", texture_group=""):
    if image is None or not hasattr(image, "witcherui_TextureSettings"):
        return

    settings = image.witcherui_TextureSettings
    resolved_repo_path, resolved_texture_group = resolve_texture_image_metadata(
        context,
        source_path,
        repo_path=repo_path or settings.repo_path,
        texture_group=texture_group or settings.texture_group or "Default",
    )
    if resolved_repo_path:
        settings.repo_path = resolved_repo_path
    if resolved_texture_group:
        settings.texture_group = resolved_texture_group


def read_xbm_texture_group(filepath: str) -> str:
    from ..CR2W.CR2W_types import getCR2W

    try:
        with open(filepath, "rb") as handle:
            cr2w = getCR2W(handle)
    except Exception:
        return "Default"

    for chunk in getattr(getattr(cr2w, "CHUNKS", None), "CHUNKS", []) or []:
        if getattr(chunk, "Type", "") != "CBitmapTexture":
            continue
        prop = chunk.GetVariableByName('textureGroup')
        if not prop:
            break
        if hasattr(prop, "String") and hasattr(prop.String, "String"):
            return prop.String.String or "Default"
        if hasattr(prop, "Index") and hasattr(prop.Index, "String"):
            return prop.Index.String or "Default"
        try:
            return prop.ToString() or "Default"
        except Exception:
            return "Default"
    return "Default"


def _draw_texture_info_box(layout, settings):
    info = get_texture_group_info(settings.texture_group)
    box = layout.box()
    box.label(text="Texture", icon='TEXTURE')
    box.prop(settings, "texture_group", text="Texture Group")
    box.label(text=f"Compression: {info.compression}")
    box.label(text=f"Mipmaps: {'Yes' if info.has_mips else 'No'}")
    return info


def _draw_redkit_box(layout, context, image):
    settings = image.witcherui_TextureSettings
    project_path = _get_active_redkit_project(context)
    workspace_root = _get_workspace_root(project_path) if project_path else None
    repo_path = settings.repo_path

    box = layout.box()
    box.label(text="REDkit Project", icon='FILE_FOLDER')
    if project_path:
        box.label(text=f"Project: {os.path.basename(project_path)}")
        if workspace_root:
            col = box.column(align=True)
            col.scale_y = 0.8
            col.label(text="Workspace:")
            col.label(text=f"  {workspace_root}")
    else:
        box.label(text="No REDkit project set", icon='ERROR')
        box.label(text="Configure in addon preferences")

    box.separator()
    box.prop(settings, "repo_path", text="Repo Path")
    row = box.row(align=True)
    row.operator("witcher.texture_set_repo_path_from_image_path", text="Use Current Image Path", icon='FILE_FOLDER')
    if getattr(getattr(context, "space_data", None), "type", "") == 'FILE_BROWSER':
        row.operator("witcher.texture_set_repo_path_from_browser", text="Set Repo from Current Folder", icon='FILE_FOLDER')

    if workspace_root and repo_path:
        full_path = _compute_full_export_path(workspace_root, repo_path)
        if full_path:
            col = box.column(align=True)
            col.scale_y = 0.8
            col.label(text="Full Path:")
            col.label(text=f"  {os.path.dirname(full_path)}")
            col.label(text=f"  {os.path.basename(full_path)}")

    box.separator()
    row = box.row()
    row.scale_y = 1.2
    if project_path:
        row.operator(
            "witcher.texture_export_goto_project_path",
            text="Go To Project Path" if repo_path else "Go To Workspace",
            icon='FILEBROWSER',
        )
    else:
        row.enabled = False
        row.operator("witcher.texture_export_goto_project_path", text="No Project Set", icon='ERROR')


class WitcherTextureSettings(bpy.types.PropertyGroup):
    repo_path: StringProperty(
        default="",
        name="Repo Path",
        description="Path for this in game, including filename and .xbm extension",
    )
    texture_group: EnumProperty(
        name="Texture Group",
        description="REDengine texture group, which determines XBM compression and mip generation",
        items=_TEXTURE_GROUP_ENUM_ITEMS,
        default='Default',
    )


class WITCH_OT_texture_export_goto_project_path(bpy.types.Operator):
    """Create the REDkit texture directory and navigate the file browser there."""
    bl_idname = "witcher.texture_export_goto_project_path"
    bl_label = "Go To Project Path"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        image = _get_export_target_image(context)
        if image is None:
            self.report({'ERROR'}, "No active image found.")
            return {'CANCELLED'}

        project_path = _get_active_redkit_project(context)
        if not project_path:
            self.report({'WARNING'}, "No REDkit project configured. Set one in addon preferences.")
            return {'CANCELLED'}

        workspace_root = _get_workspace_root(project_path)
        repo_path = image.witcherui_TextureSettings.repo_path
        full_path = _compute_full_export_path(workspace_root, repo_path) if repo_path else workspace_root
        if not full_path:
            self.report({'WARNING'}, "Could not compute project path.")
            return {'CANCELLED'}

        dir_path = os.path.dirname(full_path) if repo_path else full_path
        try:
            os.makedirs(dir_path, exist_ok=True)
        except Exception as exc:
            self.report({'ERROR'}, f"Failed to create directories: {exc}")
            return {'CANCELLED'}

        space = context.space_data
        if space and hasattr(space, 'params') and space.params:
            space.params.directory = dir_path.encode('utf-8')
            if repo_path:
                space.params.filename = os.path.basename(full_path)
            self.report({'INFO'}, f"Navigated to: {dir_path}")
        else:
            self.report({'INFO'}, f"Created path: {dir_path}")
        return {'FINISHED'}


class WITCH_OT_texture_set_repo_path_from_browser(bpy.types.Operator):
    """Set the image repo path from the current file browser folder."""
    bl_idname = "witcher.texture_set_repo_path_from_browser"
    bl_label = "Set Repo Path from Here"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        image = _get_export_target_image(context)
        if image is None:
            self.report({'ERROR'}, "No active image found.")
            return {'CANCELLED'}

        project_path = _get_active_redkit_project(context)
        if not project_path:
            self.report({'ERROR'}, "No active REDkit project found.")
            return {'CANCELLED'}

        workspace_root = _get_workspace_root(project_path)
        space = context.space_data
        if not (space and hasattr(space, 'params') and space.params):
            self.report({'ERROR'}, "Must run from File Browser area.")
            return {'CANCELLED'}

        current_filename = space.params.filename
        current_dir = space.params.directory
        if isinstance(current_dir, bytes):
            current_dir = current_dir.decode('utf-8')

        current_path_abs = os.path.abspath(current_dir)
        workspace_root_abs = os.path.abspath(workspace_root)
        if not current_path_abs.lower().startswith(workspace_root_abs.lower()):
            self.report({'WARNING'}, "Current folder is outside the active REDkit project workspace.")
            return {'CANCELLED'}

        try:
            rel_path = os.path.relpath(current_path_abs, workspace_root_abs)
        except ValueError:
            self.report({'ERROR'}, "Path is on a different drive.")
            return {'CANCELLED'}

        if rel_path == '.':
            rel_path = ""

        filename = current_filename.decode('utf-8') if isinstance(current_filename, bytes) else current_filename
        if rel_path and filename:
            full_repo_path = os.path.join(rel_path, filename)
        elif filename:
            full_repo_path = filename
        else:
            full_repo_path = rel_path

        image.witcherui_TextureSettings.repo_path = _normalize_repo_path(full_repo_path)
        self.report({'INFO'}, f"Updated Repo Path: {image.witcherui_TextureSettings.repo_path}")
        return {'FINISHED'}


class WITCH_OT_texture_set_repo_path_from_image_path(bpy.types.Operator):
    """Derive the image repo path from the current image filepath."""
    bl_idname = "witcher.texture_set_repo_path_from_image_path"
    bl_label = "Use Current Image Path"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _get_export_target_image(context) is not None

    def execute(self, context):
        image = _get_export_target_image(context)
        source_path = _get_image_source_path(image)
        repo_path = _derive_texture_repo_path(context, source_path)
        if not repo_path:
            self.report({'WARNING'}, "Could not derive a repo path from the current image path.")
            return {'CANCELLED'}

        image.witcherui_TextureSettings.repo_path = repo_path
        self.report({'INFO'}, f"Repo Path: {repo_path}")
        return {'FINISHED'}


class WITCH_OT_xbm_export(bpy.types.Operator, ExportHelper):
    """Export Witcher 3 Texture File."""
    bl_idname = "witcher.export_xbm"
    bl_label = "Export .xbm"
    filename_ext = ".xbm"
    bl_options = {'REGISTER'}

    filter_glob: StringProperty(default='*.xbm', options={'HIDDEN'})

    @classmethod
    def poll(cls, context):
        return _get_export_target_image(context) is not None

    def invoke(self, context, event):
        image = _get_export_target_image(context)
        if image is not None:
            _remember_export_image(context, image)
            settings = image.witcherui_TextureSettings
            project_path = _get_active_redkit_project(context)
            workspace_root = _get_workspace_root(project_path) if project_path else None
            if workspace_root and settings.repo_path:
                full_path = _compute_full_export_path(workspace_root, settings.repo_path)
                if full_path:
                    self.filepath = full_path
            elif not self.filepath:
                source_path = _get_image_source_path(image)
                if source_path:
                    default_name = os.path.splitext(os.path.basename(source_path))[0]
                else:
                    default_name = os.path.splitext(image.name)[0]
                self.filepath = default_name + self.filename_ext
        return super().invoke(context, event)

    def draw(self, context):
        image = _get_export_target_image(context)
        if image is None:
            self.layout.label(text="No active image found.", icon='ERROR')
            return

        settings = image.witcherui_TextureSettings
        self.layout.label(text=f"Image: {image.name}", icon='IMAGE_DATA')
        _draw_texture_info_box(self.layout, settings)
        _draw_redkit_box(self.layout, context, image)

    def execute(self, context):
        image = _get_export_target_image(context)
        if image is None:
            self.report({'ERROR'}, "No active image found. Open an image in the Image Editor.")
            return {'CANCELLED'}

        if image.size[0] == 0 or image.size[1] == 0:
            self.report({'ERROR'}, "Image has zero dimensions.")
            return {'CANCELLED'}

        try:
            from ..exporters.export_xbm import export_xbm

            settings = image.witcherui_TextureSettings
            export_xbm(image, self.filepath, texture_group=settings.texture_group)
            _update_repo_path_from_export_path(context, image, self.filepath)
        except Exception as exc:
            self.report({'ERROR'}, f"XBM export failed: {exc}")
            return {'CANCELLED'}

        self.report({'INFO'}, f"Exported XBM: {self.filepath}")
        return {'FINISHED'}


class WITCH_OT_dds_convert(bpy.types.Operator):
    """Convert the active DDS image to TGA so it can be edited in Blender."""
    bl_idname = "witcher.dds_convert_to_editable"
    bl_label = "Convert DDS to TGA"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        image = _get_export_target_image(context)
        if image is None:
            return False
        filepath = bpy.path.abspath(image.filepath)
        return filepath.lower().endswith('.dds')

    def execute(self, context):
        from ..CR2W import texconv_wrapper

        if not texconv_wrapper.is_available():
            self.report({'ERROR'}, "texconv.dll not found. Place it in CR2W/third_party_libs/.")
            return {'CANCELLED'}

        image = _get_export_target_image(context)
        if image is None:
            self.report({'ERROR'}, "No active image found.")
            return {'CANCELLED'}

        dds_path = bpy.path.abspath(image.filepath)
        if not os.path.isfile(dds_path):
            self.report({'ERROR'}, f"DDS file not found: {dds_path}")
            return {'CANCELLED'}

        settings = image.witcherui_TextureSettings
        existing_repo_path = settings.repo_path
        existing_texture_group = settings.texture_group

        try:
            output_path = texconv_wrapper.convert_dds_to_tga(dds_path)
        except RuntimeError as exc:
            self.report({'ERROR'}, f"Conversion failed: {exc}")
            return {'CANCELLED'}

        image.filepath = output_path
        image.reload()
        image.name = os.path.basename(output_path)
        apply_texture_image_metadata(
            context,
            image,
            dds_path,
            repo_path=existing_repo_path,
            texture_group=existing_texture_group,
        )

        self.report({'INFO'}, f"Converted to TGA: {output_path}")
        return {'FINISHED'}


class WITCH_PT_texture_tools(bpy.types.Panel):
    bl_label = "Texture"
    bl_idname = "WITCH_PT_texture_tools"
    bl_space_type = 'IMAGE_EDITOR'
    bl_region_type = 'UI'
    bl_category = 'Witcher'

    @classmethod
    def poll(cls, context):
        return _get_export_target_image(context) is not None

    def draw(self, context):
        image = _get_export_target_image(context)
        if image is None:
            self.layout.label(text="No active image found.", icon='ERROR')
            return

        _remember_export_image(context, image)
        settings = image.witcherui_TextureSettings

        self.layout.label(text=image.name, icon='IMAGE_DATA')
        source_path = _get_image_source_path(image)
        if source_path:
            col = self.layout.column(align=True)
            col.scale_y = 0.8
            col.label(text="Source:")
            col.label(text=f"  {source_path}")

        _draw_texture_info_box(self.layout, settings)
        _draw_redkit_box(self.layout, context, image)

        row = self.layout.row(align=True)
        row.scale_y = 1.2
        row.operator("witcher.export_xbm", text="Export .xbm", icon='EXPORT')
        row.operator("witcher.dds_convert_to_editable", text="Convert DDS", icon='FILE_REFRESH')


classes = (
    WitcherTextureSettings,
    WITCH_OT_texture_export_goto_project_path,
    WITCH_OT_texture_set_repo_path_from_browser,
    WITCH_OT_texture_set_repo_path_from_image_path,
    WITCH_OT_xbm_export,
    WITCH_OT_dds_convert,
    WITCH_PT_texture_tools,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Image.witcherui_TextureSettings = PointerProperty(type=WitcherTextureSettings)


def unregister():
    if hasattr(bpy.types.Image, "witcherui_TextureSettings"):
        del bpy.types.Image.witcherui_TextureSettings
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
