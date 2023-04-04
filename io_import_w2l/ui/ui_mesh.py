import os
import time
import bpy

from io_import_w2l.setup_logging_bl import *
log = logging.getLogger(__name__)

from bpy.props import StringProperty, BoolProperty
from bpy_extras.io_utils import (
        ImportHelper,
        ExportHelper
        )
from io_import_w2l.cloth_util import importCloth

from io_import_w2l import file_helpers
from io_import_w2l.importers import import_mesh
from io_import_w2l import get_W3_REDCLOTH_PATH, get_uncook_path, get_mod_directory
from io_import_w2l.exporters.export_mesh import do_export_mesh

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
        default=False
    )
    
    rm_ph_me: BoolProperty(
        name="Remove Physical Meshes",
        description="Remove the physical meshes after transfer of vertex colors to graphical meshes",
        default=True
    )
    
    DO_WEAR_CLOTH: BoolProperty(
        name="Setup for Character",
        description="Converts clothing rig so it can be attached to character. Might not want this if using APX tool to export new clothes",
        default=True
    )

    def draw(self, context):
        layout = self.layout
        
        sections = ["General", "Clothing"]
        
        section_options = {
            "General" : ["rotate_180"], 
            "Clothing" : ["use_mat", "rm_ph_me", "DO_WEAR_CLOTH"],
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

        filepath = self.filepath
        if os.path.isdir(filepath):
            self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
            return {'CANCELLED'}
        apx_filepath = find_apx(filepath)
        
        if not os.path.isfile(apx_filepath):
            self.report({'ERROR'}, "ERROR cannot find associated .apx. Did you set the apx folder?")
            return {'CANCELLED'}
        else:
            importCloth(context, apx_filepath, self.use_mat, self.rotate_180, self.rm_ph_me, filepath, DO_WEAR_CLOTH = self.DO_WEAR_CLOTH)
            return {'FINISHED'}

    def invoke(self, context, event):
        """Invoke."""
        UNCOOK_PATH = get_uncook_path(context) + "\\"
        if os.path.exists(UNCOOK_PATH):
            self.filepath = UNCOOK_PATH if self.filepath == '' else self.filepath
        return ImportHelper.invoke(self, context, event)

from pathlib import Path

root_folders = [
    "animations",
    "characters",
    "dlc",
    "engine",
    "environment",
    "fx",
    "game",
    "gameplay",
    "items",
    "levels",
    "living_world",
    "merged_content",
    "movies",
    "qa",
    "quests",
    "scripts",
    "soundbanks"
]

def find_apx(filepath):
    REDCLOTH_PATH = get_W3_REDCLOTH_PATH(bpy.context) # where the apx files are
    UNCOOK_PATH = get_uncook_path(bpy.context) # where the redcloth files are
    
    apx_file_path = os.path.splitext(filepath)[0] + ".apx"
    if os.path.exists(apx_file_path):
        return apx_file_path

    repo_path = apx_file_path.replace(UNCOOK_PATH, "")
    apx_filepath = REDCLOTH_PATH+repo_path
    if os.path.isfile(apx_filepath):
        return apx_filepath

    for root_folder in root_folders:
        if root_folder in apx_file_path:
            parts = apx_file_path.split(root_folder, 1)
            if len(parts) == 2:
                first_part, second_part = parts[0], root_folder + parts[1]
            else:
                first_part, second_part = apx_file_path, ""
            apx_path = REDCLOTH_PATH+second_part
            if os.path.isfile(apx_path):
                return apx_path
    
    filename = os.path.basename(apx_file_path)
    for file_path in Path(REDCLOTH_PATH).rglob(filename):
        print(f"Found {filename} at {file_path}")
        return file_path

    return apx_file_path

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
    keep_empty_lods: BoolProperty(
        name="Keep Empty LODs",
        default=False,
        description="If enabled, it will keep empty mesh LODs with zero polygons"
    )
    # do_merge_normals: BoolProperty(
    #     name="Merge Normals",
    #     default=False,
    #     description="If enabled, normals will be merged. Can cause blender to hang."
    # )
    rotate_180: BoolProperty(
        name="Rotate 180°",
        description="Rotate both the mesh and the armature on the Z-axis by 180°",
        default=False
    )
    def invoke(self, context, event):
        """Invoke."""
        UNCOOK_PATH = get_uncook_path(context) + "\\"
        if os.path.exists(UNCOOK_PATH):
            self.filepath = UNCOOK_PATH if self.filepath == '' else self.filepath
        #wm = context.window_manager.fileselect_add(self)
        return ImportHelper.invoke(self, context, event)

    def draw(self, context):
        layout = self.layout
        sections = ["Settings"]
        section_options = {
            "Settings" : ["do_import_mats",
                        "do_import_armature",
                        "keep_lod_meshes",
                        "keep_empty_lods",
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
                                    self.rotate_180,
                                    self.keep_empty_lods)
            message = f'Imported .w2mesh file in {time.time() - s} seconds.'
            log.info(message)
            self.report({'INFO'}, message)
        else:
            self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
            return {'CANCELLED'}
        return {'FINISHED'}



class WITCH_OT_w2mesh_export(bpy.types.Operator, ExportHelper):
    """Export Witcher 3 Mesh File"""
    bl_idname = "object.export_w2mesh_btn"
    bl_label = "Export .w2mesh.json"
    filename_ext = ".json"
    bl_options = {'REGISTER', 'UNDO'}
    
    filter_glob: StringProperty(default='*.json', options={'HIDDEN'})
    

    def execute(self, context):
        if not bpy.context.selected_objects:
            self.report({'ERROR'}, "ERROR Nothing selected to export.")
            return {'CANCELLED'}
        
        print("Exporting w2mesh now!")
        fdir = self.filepath
        ext = file_helpers.getFilenameType(fdir)
        if ext == ".json":
            s = time.time()
            self.do_merge_normals = False
            
            selected_armatures = [ob for ob in bpy.context.selected_objects if ob.type == 'ARMATURE']
            meshes = []

            for armature in selected_armatures:
                armature_meshes = [child for child in armature.children if child.type == 'MESH']
                meshes.extend(armature_meshes)
                mesh_back = do_export_mesh(context, fdir,
                                    armature = armature,
                                    meshes = meshes)
            if len(selected_armatures) == 0:
                meshes = [ob for ob in bpy.context.selected_objects if ob.type == 'MESH']
                mesh_back = do_export_mesh(context, fdir,
                                    armature = None,
                                    meshes = meshes)
            #exporter = export_radish.radishExporter()
            #exporter.export(fdir)
            message = f'Exported .w2mesh.json file in {time.time() - s} seconds.'
            log.info(message)
            self.report({'INFO'}, message)
        else:
            self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
            return {'CANCELLED'}
        
        
        return {'FINISHED'}
    def invoke(self, context, event):
        mod_uncooked_dir = Path(get_mod_directory(context)) / "files\\Mod\\Uncooked"
        if mod_uncooked_dir.exists():
            self.filepath = str(mod_uncooked_dir)
        
        if bpy.context.active_object:

            selected_armatures = [ob for ob in bpy.context.selected_objects if ob.type == 'ARMATURE']
            meshes = []
            for armature in selected_armatures:
                armature_meshes = [child for child in armature.children if child.type == 'MESH']
                meshes.extend(armature_meshes)
            if len(selected_armatures) == 0:
                meshes = [ob for ob in bpy.context.selected_objects if ob.type == 'MESH']

            if meshes:
                main_mesh = meshes[0]
                if main_mesh.witcherui_MeshSettings.item_repo_path != "":
                    if main_mesh.witcherui_MeshSettings.is_DLC:
                        dlc_uncooked_dir = Path(get_mod_directory(context)) / "files\\DLC\\Uncooked"
                        if dlc_uncooked_dir.exists():
                            self.filepath = str(dlc_uncooked_dir)


                    game_repo_path = os.path.splitdrive(main_mesh.witcherui_MeshSettings['item_repo_path'])[1]

                    self.filepath += "\\" + game_repo_path + self.filename_ext
                    directory = os.path.dirname(self.filepath)
                    directory = os.path.normpath(directory)
                    if os.path.exists(directory):
                        pass
                    elif main_mesh.witcherui_MeshSettings.make_export_dir:
                        # Create the directory if it does not exist
                        if not os.path.exists(directory):
                            try:
                                os.makedirs(directory)
                                print(directory, ' created!')
                            except Exception as e:
                                log.critical(e)
                                log.critical("Check repo path is valid")
                else:
                    self.filepath += "\\" + bpy.context.active_object.name + ".w2mesh" + self.filename_ext
            else:
                self.filepath += "\\" + bpy.context.active_object.name + ".w2mesh" + self.filename_ext
        else:
            self.filepath += "\\" + "default" + ".w2mesh" + self.filename_ext
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}