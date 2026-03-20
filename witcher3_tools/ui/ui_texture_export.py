"""Blender operator for exporting Witcher 3 .xbm texture files."""

import bpy
from bpy.props import StringProperty
from bpy_extras.io_utils import ExportHelper


class WITCH_OT_xbm_export(bpy.types.Operator, ExportHelper):
    """Export Witcher 3 Texture File"""
    bl_idname = "witcher.export_xbm"
    bl_label = "Export .xbm"
    filename_ext = ".xbm"
    bl_options = {'REGISTER'}

    filter_glob: StringProperty(default='*.xbm', options={'HIDDEN'})

    @classmethod
    def poll(cls, context):
        # Available when there's an active image in any image editor
        for area in context.screen.areas:
            if area.type == 'IMAGE_EDITOR':
                space = area.spaces.active
                if space.image is not None:
                    return True
        return False

    def invoke(self, context, event):
        image = self._get_active_image(context)
        if image and image.name:
            # Suggest filename from image name, stripping existing extension
            import os
            base = os.path.splitext(image.name)[0]
            self.filepath = base + ".xbm"
        return super().invoke(context, event)

    def execute(self, context):
        image = self._get_active_image(context)
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

    def _get_active_image(self, context):
        """Get the active image from the Image Editor."""
        for area in context.screen.areas:
            if area.type == 'IMAGE_EDITOR':
                space = area.spaces.active
                if space.image is not None:
                    return space.image
        return None


classes = (
    WITCH_OT_xbm_export,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
