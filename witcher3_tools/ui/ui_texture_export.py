"""Blender operators for Witcher 3 texture export and DDS conversion."""

import os
import logging

import bpy
from bpy.props import StringProperty, EnumProperty
from bpy_extras.io_utils import ExportHelper

log = logging.getLogger(__name__)


def _get_active_image(context):
    """Get the active image from the Image Editor."""
    for area in context.screen.areas:
        if area.type == 'IMAGE_EDITOR':
            space = area.spaces.active
            if space.image is not None:
                return space.image
    return None


class WITCH_OT_xbm_export(bpy.types.Operator, ExportHelper):
    """Export Witcher 3 Texture File"""
    bl_idname = "witcher.export_xbm"
    bl_label = "Export .xbm"
    filename_ext = ".xbm"
    bl_options = {'REGISTER'}

    filter_glob: StringProperty(default='*.xbm', options={'HIDDEN'})

    @classmethod
    def poll(cls, context):
        return _get_active_image(context) is not None

    def invoke(self, context, event):
        image = _get_active_image(context)
        if image and image.name:
            base = os.path.splitext(image.name)[0]
            self.filepath = base + ".xbm"
        return super().invoke(context, event)

    def execute(self, context):
        image = _get_active_image(context)
        if image is None:
            self.report({'ERROR'}, "No active image found. Open an image in the Image Editor.")
            return {'CANCELLED'}

        if image.size[0] == 0 or image.size[1] == 0:
            self.report({'ERROR'}, "Image has zero dimensions.")
            return {'CANCELLED'}

        try:
            from ..exporters.export_xbm import export_xbm
            export_xbm(image, self.filepath)
        except Exception as e:
            self.report({'ERROR'}, f"XBM export failed: {e}")
            return {'CANCELLED'}

        self.report({'INFO'}, f"Exported XBM: {self.filepath}")
        return {'FINISHED'}


class WITCH_OT_dds_convert(bpy.types.Operator):
    """Convert the active DDS image to PNG so it can be edited in Blender. """  \
    """Compressed DDS textures (DXT1, DXT5, BC7, etc.) are read-only in Blender. """ \
    """This converts the file on disk and reloads it as an editable PNG"""
    bl_idname = "witcher.dds_convert_to_editable"
    bl_label = "Convert DDS to Editable"
    bl_options = {'REGISTER', 'UNDO'}

    output_format: EnumProperty(
        name="Format",
        items=[
            ('png', "PNG", "Convert to PNG (lossless, with alpha)"),
            ('tga', "TGA", "Convert to TGA (lossless, with alpha)"),
        ],
        default='png',
    )

    @classmethod
    def poll(cls, context):
        image = _get_active_image(context)
        if image is None:
            return False
        filepath = bpy.path.abspath(image.filepath)
        return filepath.lower().endswith('.dds')

    def execute(self, context):
        from ..CR2W import texconv_wrapper

        if not texconv_wrapper.is_available():
            self.report({'ERROR'},
                "texconv.dll not found. Place it in the addon's CR2W/third_party_libs/ folder.")
            return {'CANCELLED'}

        image = _get_active_image(context)
        if image is None:
            self.report({'ERROR'}, "No active image found.")
            return {'CANCELLED'}

        dds_path = bpy.path.abspath(image.filepath)
        if not os.path.isfile(dds_path):
            self.report({'ERROR'}, f"DDS file not found: {dds_path}")
            return {'CANCELLED'}

        try:
            if self.output_format == 'tga':
                output_path = texconv_wrapper.convert_dds_to_tga(dds_path)
            else:
                output_path = texconv_wrapper.convert_dds_to_png(dds_path)
        except RuntimeError as e:
            self.report({'ERROR'}, f"Conversion failed: {e}")
            return {'CANCELLED'}

        # Reload the image from the converted file
        image.filepath = output_path
        image.reload()
        # Update the datablock name to reflect the new file
        image.name = os.path.basename(output_path)

        self.report({'INFO'}, f"Converted to {self.output_format.upper()}: {output_path}")
        return {'FINISHED'}


classes = (
    WITCH_OT_xbm_export,
    WITCH_OT_dds_convert,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
