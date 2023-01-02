import os
import time
import bpy

from io_import_w2l.setup_logging_bl import *
log = logging.getLogger(__name__)

from bpy.props import StringProperty, BoolProperty
from bpy_extras.io_utils import (
        ImportHelper
        )
from io_import_w2l.cloth_util import importCloth

from io_import_w2l import file_helpers
from io_import_w2l.importers import import_mesh
from io_import_w2l import get_W3_REDCLOTH_PATH, get_uncook_path

import addon_utils

class WITCH_OT_apx(bpy.types.Operator, ImportHelper):
    """Load a Redcloth file with materials. To enable this button go to https://github.com/ArdCarraigh/Blender_APX_Addon and install the APX addon. Sep 19 2022 release"""
    bl_idname = "import.apx_materials"  # important since its how bpy.ops.import.apx is constructed
    bl_label = "Import APX"

    # ImportHelper mixin class uses this
    filename_ext = ".redcloth"

    filter_glob: StringProperty(
        default="*.redcloth",
        options={'HIDDEN'},
        maxlen=255,  # Max internal buffer length, longer would be clamped.
    )

    # List of operator properties, the attributes will be assigned
    # to the class instance from the operator settings before calling.
    
    use_mat: BoolProperty(
        name="Prevent Material Duplication",
        description="Use existing materials from the scene if their name is identical to the ones of your mesh",
        default=True,
    )
    
    rotate_180: BoolProperty(
        name="Rotate 180°",
        description="Rotate both the mesh and the armature on the Z-axis by 180°",
        default=True
    )
    
    rm_ph_me: BoolProperty(
        name="Remove Physical Meshes",
        description="Remove the physical meshes after transfer of vertex colors to graphical meshes",
        default=True
    )

    def draw(self, context):
        layout = self.layout
        
        sections = ["General", "Clothing"]
        
        section_options = {
            "General" : ["rotate_180"], 
            "Clothing" : ["use_mat", "rm_ph_me"]
        }
        
        section_icons = {
            "General" : "WORLD", "Clothing" : "MATCLOTH", 
        }
        
        for section in sections:
            row = layout.row()
            box = row.box()
            box.label(text=section, icon=section_icons[section])
            for prop in section_options[section]:
                box.prop(self, prop)

    @classmethod
    def poll(self, context):
        #print(bpy.ops) # debug ops _utils
        (exist, enabled) = addon_utils.check("io_scene_apx")
        return enabled
    
    def execute(self, context):

        fdir = self.filepath
        if os.path.isdir(fdir):
            self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
            return {'CANCELLED'}
        uncook_path = get_uncook_path(bpy.context)
        repo_path = fdir.replace(uncook_path, "").replace(".redcloth", ".apx")
        apx_fdir = get_W3_REDCLOTH_PATH(bpy.context)+repo_path
        importCloth(context, apx_fdir, self.use_mat, self.rotate_180, self.rm_ph_me, fdir)
        return {'FINISHED'}

class WITCH_OT_w2mesh(bpy.types.Operator, ImportHelper):
    """Load Witcher 3 Mesh File"""
    bl_idname = "object.import_w2mesh_btn"
    bl_label = "Import .w2mesh"
    filename_ext = ".w2mesh"
    bl_options = {'REGISTER', 'UNDO'}
    
    filter_glob: StringProperty(default='*.w2mesh', options={'HIDDEN'})
    
    do_import_mats: BoolProperty(
        name="Apply Materials",
        default=True,
        description="If enabled, materials will be imported. You must have the game unbundled and tga textures uncooked. With the path to them set in the addon settings"
    )
    do_import_armature: BoolProperty(
        name="Import Armature",
        default=True,
        description="If enabled, the armature will be imported"
    )
    keep_lod_meshes: BoolProperty(
        name="Keep LODs",
        default=False,
        description="If enabled, it will keep low quality meshes and materials"
    )
    # do_merge_normals: BoolProperty(
    #     name="Merge Normals",
    #     default=False,
    #     description="If enabled, normals will be merged. Can cause blender to hang."
    # )
    rotate_180: BoolProperty(
        name="Rotate 180°",
        description="Rotate both the mesh and the armature on the Z-axis by 180°",
        default=True
    )
    def invoke(self, context, event):
        """Invoke."""
        return ImportHelper.invoke(self, context, event)

    def draw(self, context):
        layout = self.layout
        sections = ["Settings"]
        section_options = {
            "Settings" : ["do_import_mats",
                        "do_import_armature",
                        "keep_lod_meshes",
                        #"do_merge_normals",
                        "rotate_180"]
        }
        for section in sections:
            row = layout.row()
            box = row.box()
            box.label(text=section)
            for prop in section_options[section]:
                box.prop(self, prop)

    def execute(self, context):
        print("importing w2mesh now!")
        fdir = self.filepath
        if os.path.isdir(fdir):
            self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
            return {'CANCELLED'}
        
        ext = file_helpers.getFilenameType(fdir)
        if ext == ".w2mesh":
            s = time.time()
            self.do_merge_normals = False
            import_mesh.import_mesh(fdir,
                                    self.do_import_mats,
                                    self.do_import_armature,
                                    self.keep_lod_meshes,
                                    self.do_merge_normals,
                                    self.rotate_180)
            message = f'Imported .w2mesh file in {time.time() - s} seconds.'
            log.info(message)
            self.report({'INFO'}, message)
        else:
            self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
            return {'CANCELLED'}
        return {'FINISHED'}
