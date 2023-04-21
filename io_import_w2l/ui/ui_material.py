import os
import struct
import bpy
from pathlib import Path
from bpy.types import (Panel, Operator)
from bpy.props import StringProperty, BoolProperty
from mathutils import Vector
from bpy_extras.io_utils import ImportHelper, ExportHelper
import addon_utils
from io_import_w2l import file_helpers, w3_material_blender, CR2W, get_texture_path, get_uncook_path
from io_import_w2l.cloth_util import setup_w3_material_CR2W

from io_import_w2l.CR2W.CR2W_types import getCR2W
from io_import_w2l.CR2W import bStream
from io_import_w2l.ui.blender_fun import convert_xbm_to_dds

class WITCH_OT_w2mg(bpy.types.Operator, ImportHelper):
    """Load Witcher 3 Material Shader"""
    bl_idname = "witcher.import_w2mg"
    bl_label = "Import .w2mg"
    filename_ext = ".w2mg"
    filter_glob: StringProperty(default='*.w2mg', options={'HIDDEN'})
    do_update_mats: BoolProperty(
        name="Material Update",
        default=True,
        description="If enabled, it will replace the material with same name instead of creating a new one"
    )
    def execute(self, context):
        print("importing material now!")
        fdir = self.filepath
        if os.path.isdir(fdir):
            self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
            return {'CANCELLED'}
        ext = file_helpers.getFilenameType(fdir)
        if ext == ".w2mg":
            w3_material_blender.import_w2mg(fdir, self)
        else:
            self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
            return {'CANCELLED'}
        return {'FINISHED'}
    
    def invoke(self, context, event):
        UNCOOK_PATH = get_uncook_path(context) + '\\'
        if os.path.exists(UNCOOK_PATH):
            self.filepath = UNCOOK_PATH if self.filepath == '' else self.filepath
        return ImportHelper.invoke(self, context, event)

class WITCH_OT_w2mi(bpy.types.Operator, ImportHelper):
    """Load Witcher 3 Material Instance"""
    bl_idname = "witcher.import_w2mi"
    bl_label = "Import .w2mi"
    filename_ext = ".w2mi"
    filter_glob: StringProperty(default='*.w2mi', options={'HIDDEN'})
    do_update_mats: BoolProperty(
        name="Material Update",
        default=True,
        description="If enabled, it will replace the material with same name instead of creating a new one"
    )
    def execute(self, context):
        print("importing material instance now!")
        fdir = self.filepath
        if os.path.isdir(fdir):
            self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
            return {'CANCELLED'}
        ext = file_helpers.getFilenameType(fdir)
        if ext == ".w2mi":
            bpy.ops.mesh.primitive_plane_add()
            obj = bpy.context.selected_objects[:][0]
            instance_filename = Path(fdir).stem
            materials = []
            material_file_chunks = CR2W.CR2W_reader.load_material(fdir)
            for idx, mat in enumerate(material_file_chunks):
                # if idx > 0:
                #     raise Exception('wut')
                target_mat = False
                if self.do_update_mats:
                    if instance_filename in obj.data.materials:
                        target_mat = obj.data.materials[instance_filename] #None
                    if instance_filename in bpy.data.materials:
                        target_mat = bpy.data.materials[instance_filename] #None
                if not target_mat:
                    target_mat = bpy.data.materials.new(name=instance_filename)

                finished_mat = setup_w3_material_CR2W(get_texture_path(context), target_mat, mat, force_update=True, mat_filename=instance_filename, is_instance_file = True)

                if instance_filename in obj.data.materials and not self.do_update_mats:
                    obj.material_slots[target_mat.name].material = finished_mat
                else:
                    obj.data.materials.append(finished_mat)
        else:
            self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
            return {'CANCELLED'}
        return {'FINISHED'}
    
    def invoke(self, context, event):
        UNCOOK_PATH = get_uncook_path(context) + '\\'
        if os.path.exists(UNCOOK_PATH):
            self.filepath = UNCOOK_PATH if self.filepath == '' else self.filepath
        return ImportHelper.invoke(self, context, event)

class WITCH_OT_xbm(bpy.types.Operator, ImportHelper):
    """Load Witcher 2 Texture"""
    bl_idname = "witcher.import_xbm"
    bl_label = "Import W2 .xbm"
    filename_ext = ".xbm"
    filter_glob: StringProperty(default='*.xbm', options={'HIDDEN'})
    def execute(self, context):
        print("importing xbm")
        fdir = self.filepath
        if os.path.isdir(fdir):
            self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
            return {'CANCELLED'}
        ext = file_helpers.getFilenameType(fdir)
        if ext == ".xbm":
            dds_path = convert_xbm_to_dds(fdir)
            bpy.data.images.load(dds_path,check_existing=True)
                    
        else:
            self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
            return {'CANCELLED'}
        return {'FINISHED'}
    
    def invoke(self, context, event):
        UNCOOK_PATH = get_uncook_path(context) + '\\'
        if os.path.exists(UNCOOK_PATH):
            self.filepath = UNCOOK_PATH if self.filepath == '' else self.filepath
        return ImportHelper.invoke(self, context, event)


from bpy.utils import (register_class, unregister_class)

_classes= [
    WITCH_OT_xbm,
    WITCH_OT_w2mi,
    WITCH_OT_w2mg,
]

def register():
    for cls in _classes:
        register_class(cls)

def unregister():
    for cls in _classes:
        unregister_class(cls)