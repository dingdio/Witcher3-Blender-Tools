import logging
import os
import re
import time
import math
from collections import defaultdict
import bpy
from mathutils import Vector, Matrix
from ..importers import import_nxs
from ..importers.import_nxs import material_colors, color_map

log = logging.getLogger(__name__)

from bpy.props import StringProperty, BoolProperty, IntProperty, FloatProperty, EnumProperty
from bpy_extras.io_utils import (
        ImportHelper,
        ExportHelper
        )
from ..cloth_util import importCloth

from .. import file_helpers
from ..importers import import_mesh, import_rig
from .. import (
    constrain_util,
    get_W3_REDCLOTH_PATH,
    get_uncook_path,
    get_mod_directory,
    get_all_addon_prefs,
    get_rig_rot90_enabled,
    set_rig_rot90_enabled,
)
from ..exporters.export_mesh import do_export_mesh

import addon_utils

class WITCH_OT_apx(bpy.types.Operator, ImportHelper):
    """Load a Redcloth file with materials. To enable this button go to https://github.com/ArdCarraigh/Blender_APX_Addon and install the APX addon. Sep 19 2022 release"""
    bl_idname = "witcher.import_apx_materials"  # important since its how bpy.ops.import.apx is constructed
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
    
    def draw(self, context):
        layout = self.layout
        
        sections = ["General", "Clothing"]
        
        section_options = {
            "General" : ["rotate_180"], 
            "Clothing" : ["use_mat", "rm_ph_me"],
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
        (exist, enabled) = addon_utils.check("io_mesh_apx")
        if not enabled:
            (exist, enabled) = addon_utils.check("io_scene_apx")
        return enabled
    
    def execute(self, context):

        filepath = self.filepath
        if os.path.isdir(filepath):
            self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
            return {'CANCELLED'}
        apx_filepath = find_apx(filepath)
        
        if not os.path.isfile(apx_filepath):
            self.report(
                {'ERROR'},
                "ERROR cannot find associated .apx in the uncook path. "
                "Extract collision .apb and convert to .apx (io_mesh_apx + apex_sdk_cli)."
            )
            return {'CANCELLED'}
        else:
            importCloth(context, apx_filepath, self.use_mat, self.rotate_180, self.rm_ph_me, filepath)
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
        log.debug("Found %s at %s", filename, file_path)
        return file_path

    return apx_file_path

class WITCH_OT_w2mesh(bpy.types.Operator, ImportHelper):
    """Load Witcher 3 Mesh File"""
    bl_idname = "witcher.import_w2mesh"
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


# ---------------------------------------------------------------------------
#  Helpers for REDkit project path resolution
# ---------------------------------------------------------------------------

def _get_active_redkit_project(context):
    """Return the active REDkit project path string, or None."""
    addon_prefs = get_all_addon_prefs(context)
    projects = getattr(addon_prefs, "redkit_projects", [])
    index = getattr(addon_prefs, "redkit_projects_index", 0)
    if projects and 0 <= index < len(projects):
        p = projects[index].path
        if p:
            return os.path.normpath(bpy.path.abspath(p))
    return None


def _get_workspace_root(project_path):
    """Return the workspace subfolder inside a REDkit project."""
    if not project_path:
        return None
    ws = os.path.join(project_path, "workspace")
    return ws


def _get_main_mesh(context):
    """Return the primary mesh object for export context."""
    selected_armatures = [ob for ob in context.selected_objects if ob.type == 'ARMATURE']
    meshes = []
    for armature in selected_armatures:
        armature_meshes = [child for child in armature.children if child.type == 'MESH']
        meshes.extend(armature_meshes)
    if not selected_armatures:
        meshes = [ob for ob in context.selected_objects if ob.type == 'MESH']
    return meshes[0] if meshes else None

def _compute_full_export_path(workspace_root, repo_path):
    """Combine workspace root + repo path into a full filesystem path."""
    if not workspace_root or not repo_path:
        return None
    # Normalise repo_path separators
    clean_repo = repo_path.replace("/", os.sep).replace("\\", os.sep).lstrip(os.sep)
    return os.path.normpath(os.path.join(workspace_root, clean_repo))


# ---------------------------------------------------------------------------
#  "Go To Project Path" operator (runs inside the file browser)
# ---------------------------------------------------------------------------

class WITCH_OT_export_goto_project_path(bpy.types.Operator):
    """Create the REDkit project directory structure and navigate the file browser there"""
    bl_idname = "witcher.export_goto_project_path"
    bl_label = "Go To Project Path"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        project_path = _get_active_redkit_project(context)
        if not project_path:
            self.report({'WARNING'}, "No REDkit project configured. Set one in addon preferences.")
            return {'CANCELLED'}

        workspace_root = _get_workspace_root(project_path)

        main_mesh = _get_main_mesh(context)
        repo_path = ""
        if main_mesh:
            repo_path = main_mesh.witcherui_MeshSettings.item_repo_path

        if repo_path:
            full_path = _compute_full_export_path(workspace_root, repo_path)
        else:
            # No repo path — just go to the workspace root
            full_path = workspace_root

        if not full_path:
            self.report({'WARNING'}, "Could not compute project path.")
            return {'CANCELLED'}

        # Create directory structure
        dir_path = os.path.dirname(full_path) if repo_path else full_path
        try:
            os.makedirs(dir_path, exist_ok=True)
        except Exception as e:
            self.report({'ERROR'}, f"Failed to create directories: {e}")
            return {'CANCELLED'}

        # Navigate the file browser
        space = context.space_data
        if space and hasattr(space, 'params') and space.params:
            space.params.directory = dir_path.encode('utf-8')
            if repo_path:
                space.params.filename = os.path.basename(full_path)
            self.report({'INFO'}, f"Navigated to: {dir_path}")
        else:
            self.report({'INFO'}, f"Created path: {dir_path} (could not navigate browser)")

        return {'FINISHED'}


# ---------------------------------------------------------------------------
#  Set Repo Path
# ---------------------------------------------------------------------------

class WITCH_OT_set_repo_path_from_browser(bpy.types.Operator):
    """Set the mesh's Repo Path based on the current file browser location"""
    bl_idname = "witcher.set_repo_path_from_browser"
    bl_label = "Set Repo Path from Here"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        project_path = _get_active_redkit_project(context)
        if not project_path:
             self.report({'ERROR'}, "No active REDkit project found.")
             return {'CANCELLED'}

        workspace_root = _get_workspace_root(project_path)
        if not workspace_root:
             self.report({'ERROR'}, "Could not determine workspace root.")
             return {'CANCELLED'}

        # Get current browser directory and filename from active space
        space = context.space_data
        if not (space and hasattr(space, 'params') and space.params):
            self.report({'ERROR'}, "Must run from File Browser area.")
            return {'CANCELLED'}

        # Preserve the current filename the user typed into the dialog
        current_filename = space.params.filename

        # Determine current path
        try:
             current_dir = space.params.directory
             if isinstance(current_dir, bytes):
                  current_dir = current_dir.decode('utf-8')
        except Exception as e:
             self.report({'ERROR'}, f"Failed to read browser path: {e}")
             return {'CANCELLED'}

        current_path_abs = os.path.abspath(current_dir)
        workspace_root_abs = os.path.abspath(workspace_root)

        # Check if inside the workspace
        if not current_path_abs.lower().startswith(workspace_root_abs.lower()):
            self.report({'WARNING'}, "Current folder is outside the active REDkit project workspace.")
            return {'CANCELLED'}

        # Calculate relative path
        try:
             rel_path = os.path.relpath(current_path_abs, workspace_root_abs)
        except ValueError:
             self.report({'ERROR'}, "Path is on a different drive.")
             return {'CANCELLED'}

        if rel_path == '.':
            rel_path = ""

        # Build full repo path: directory + filename from the file browser
        filename = current_filename
        if isinstance(filename, bytes):
            filename = filename.decode('utf-8')
        if not filename:
            filename = ""

        if rel_path and filename:
            full_repo_path = os.path.join(rel_path, filename)
        elif filename:
            full_repo_path = filename
        else:
            full_repo_path = rel_path

        # Normalize separators
        full_repo_path = full_repo_path.replace('/', '\\')

        # Update mesh settings
        main_mesh = _get_main_mesh(context)
        if main_mesh:
            main_mesh.witcherui_MeshSettings.item_repo_path = full_repo_path
            self.report({'INFO'}, f"Updated Repo Path: {full_repo_path}")

            # Restore the filename the user had typed
            if current_filename:
                space.params.filename = current_filename

            # Force redraw to show updated property in sidebar
            for area in context.screen.areas:
                if area.type == 'FILE_BROWSER':
                    area.tag_redraw()
        else:
             self.report({'ERROR'}, "No active mesh found.")
             return {'CANCELLED'}

        return {'FINISHED'}


# ---------------------------------------------------------------------------
#  LOD Generation
# ---------------------------------------------------------------------------

class WITCH_OT_generate_lods(bpy.types.Operator):
    """Generate LOD meshes by decimating the selected mesh"""
    bl_idname = "witcher.generate_lods"
    bl_label = "Generate LODs"
    bl_options = {'REGISTER', 'UNDO'}

    lod_count: IntProperty(
        name="LOD Count",
        description="Number of LOD levels to generate",
        default=3, min=1, max=6
    )
    ratio_step: FloatProperty(
        name="Ratio Step",
        description="Each LOD multiplies polygon count by this ratio",
        default=0.5, min=0.05, max=0.9
    )
    base_distance: FloatProperty(
        name="Base Distance",
        description="Viewing distance for LOD 1 (doubles per LOD)",
        default=10.0, min=1.0
    )
    decimate_type: EnumProperty(
        name="Decimate Type",
        items=[
            ('COLLAPSE', "Collapse", "Best quality reduction"),
            ('UN_SUBDIVIDE', "Un-Subdivide", "Fast, works best on quads"),
        ],
        default='COLLAPSE'
    )

    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'MESH'

    def execute(self, context):
        import bmesh

        source = context.active_object
        # Determine base name: strip _lod0 if present, or use as-is
        if source.name.endswith("_lod0"):
            base_name = source.name[:-5]
        else:
            base_name = source.name
            # Rename source to _lod0
            source.name = base_name + "_lod0"

        # Set lod0 properties
        source.witcherui_MeshSettings.lod_level = 0
        source.witcherui_MeshSettings.distance = 0.0

        created = []
        ratio = 1.0

        for i in range(1, self.lod_count + 1):
            ratio *= self.ratio_step
            lod_name = f"{base_name}_lod{i}"

            # Remove existing LOD with this name
            existing = bpy.data.objects.get(lod_name)
            if existing:
                bpy.data.objects.remove(existing, do_unlink=True)

            # Duplicate the source mesh data
            new_mesh = source.data.copy()
            new_mesh.name = lod_name
            lod_obj = bpy.data.objects.new(lod_name, new_mesh)

            # Link to same collections as source
            for col in source.users_collection:
                col.objects.link(lod_obj)

            # Copy transform
            lod_obj.matrix_world = source.matrix_world.copy()

            # Parent to same parent
            if source.parent:
                lod_obj.parent = source.parent
                lod_obj.parent_type = source.parent_type
                if source.parent_type == 'BONE':
                    lod_obj.parent_bone = source.parent_bone

            # Apply decimate modifier
            if self.decimate_type == 'COLLAPSE':
                mod = lod_obj.modifiers.new(name="LOD_Decimate", type='DECIMATE')
                mod.ratio = ratio
            else:
                mod = lod_obj.modifiers.new(name="LOD_Decimate", type='DECIMATE')
                mod.decimate_type = 'UNSUBDIV'
                # iterations roughly maps to halving each time
                mod.iterations = i

            # Apply the modifier
            ctx = context.copy()
            ctx['object'] = lod_obj
            with context.temp_override(**ctx):
                bpy.ops.object.modifier_apply(modifier=mod.name)

            # Set mesh settings
            lod_obj.witcherui_MeshSettings.lod_level = i
            lod_obj.witcherui_MeshSettings.distance = self.base_distance * (2 ** (i - 1))

            # Copy repo path from source
            lod_obj.witcherui_MeshSettings.item_repo_path = source.witcherui_MeshSettings.item_repo_path

            created.append(lod_obj)

        face_counts = ", ".join([f"lod{i+1}: {len(obj.data.polygons)}" for i, obj in enumerate(created)])
        self.report({'INFO'}, f"Generated {len(created)} LODs ({face_counts})")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
#  Physical Material Constants (must match the game's CName values)
# ---------------------------------------------------------------------------

# Ordered list of valid physical material names from the game engine.
PHYSICAL_MATERIAL_NAMES = list(material_colors.keys())
DEFAULT_PHYSICAL_MATERIAL = "default"
if DEFAULT_PHYSICAL_MATERIAL not in PHYSICAL_MATERIAL_NAMES:
    PHYSICAL_MATERIAL_NAMES.insert(0, DEFAULT_PHYSICAL_MATERIAL)
PHYSICAL_MATERIAL_ENUM_ITEMS = [(name, name, "") for name in PHYSICAL_MATERIAL_NAMES]


def _physical_material_enum_items(scene=None, context=None):
    """Build EnumProperty items list for the physical material dropdown."""
    return PHYSICAL_MATERIAL_ENUM_ITEMS


def _assign_physical_material(mesh_data, material_name):
    """Assign a Blender material with the given physical material name to mesh_data.

    Creates the material if it doesn't exist, and sets the debug colour from
    the game's colour mapping so the user can visually identify the type.
    """
    mat = bpy.data.materials.get(material_name)
    if mat is None:
        mat = bpy.data.materials.new(name=material_name)
    if mesh_data.materials:
        mesh_data.materials.clear()
    mesh_data.materials.append(mat)

    # Set debug color from the game CSV mapping
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    debug_color_name = material_colors.get(material_name)
    if debug_color_name and bsdf:
        rgba = color_map.get(debug_color_name.upper())
        if rgba:
            bsdf.inputs["Base Color"].default_value = rgba
            bsdf.inputs["Alpha"].default_value = 0.5

    return mat


# ---------------------------------------------------------------------------
#  Collider Generation Helpers
# ---------------------------------------------------------------------------

def _create_collider_object(context, source, name, mesh_data, physical_material=None):
    """Create a collider object from mesh data, parented like the source.

    Args:
        physical_material: If provided, assigns this physical material to
                           the mesh. If None, no material is assigned.
    """
    material_name = physical_material or DEFAULT_PHYSICAL_MATERIAL
    _assign_physical_material(mesh_data, material_name)

    obj = bpy.data.objects.new(name, mesh_data)

    # Link to same collections
    for col in source.users_collection:
        col.objects.link(obj)

    # Transform & parent
    obj.matrix_world = source.matrix_world.copy()
    if source.parent:
        obj.parent = source.parent
        obj.parent_type = source.parent_type
        if source.parent_type == 'BONE':
            obj.parent_bone = source.parent_bone

    # Display as wireframe
    obj.display_type = 'WIRE'

    return obj


def _get_collider_base_name(source):
    """Strip LOD suffix to get base name for collider naming."""
    name = source.name
    # Strip _lodN suffix
    name = re.sub(r'_lod\d+$', '', name)
    # Strip Blender .NNN suffix
    name = re.sub(r'\.\d{3}$', '', name)
    return name


def _unique_object_name(base_name):
    """Return a unique object name by appending .### if needed."""
    if base_name not in bpy.data.objects:
        return base_name
    idx = 1
    while True:
        candidate = f"{base_name}.{idx:03d}"
        if candidate not in bpy.data.objects:
            return candidate
        idx += 1


# ---------------------------------------------------------------------------
#  Box Collider
# ---------------------------------------------------------------------------

class WITCH_OT_create_box_collider(bpy.types.Operator):
    """Create a box collider from the bounding box of the selected mesh"""
    bl_idname = "witcher.create_box_collider"
    bl_label = "Create Box Collider"
    bl_options = {'REGISTER', 'UNDO'}

    physical_material: EnumProperty(
        name="Physical Material",
        description="Physical material for the collision shape",
        items=PHYSICAL_MATERIAL_ENUM_ITEMS,
        default=DEFAULT_PHYSICAL_MATERIAL,
    )

    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'MESH'

    def execute(self, context):
        import bmesh

        source = context.active_object
        base_name = _get_collider_base_name(source)
        col_name = _unique_object_name(f"{base_name}_box")

        # Get bounding box corners in local space
        bbox = [source.matrix_world @ Vector(corner) for corner in source.bound_box]
        min_co = Vector((min(v.x for v in bbox), min(v.y for v in bbox), min(v.z for v in bbox)))
        max_co = Vector((max(v.x for v in bbox), max(v.y for v in bbox), max(v.z for v in bbox)))

        # Create box mesh
        verts = [
            (min_co.x, min_co.y, min_co.z),
            (max_co.x, min_co.y, min_co.z),
            (max_co.x, max_co.y, min_co.z),
            (min_co.x, max_co.y, min_co.z),
            (min_co.x, min_co.y, max_co.z),
            (max_co.x, min_co.y, max_co.z),
            (max_co.x, max_co.y, max_co.z),
            (min_co.x, max_co.y, max_co.z),
        ]
        faces = [
            (0, 1, 2, 3), (4, 5, 6, 7),  # bottom, top
            (0, 1, 5, 4), (2, 3, 7, 6),  # front, back
            (0, 3, 7, 4), (1, 2, 6, 5),  # left, right
        ]
        mesh = bpy.data.meshes.new(col_name)
        mesh.from_pydata(verts, [], faces)
        mesh.update()

        obj = _create_collider_object(context, source, col_name, mesh, physical_material=self.physical_material)
        # Reset transform since we built in world space
        obj.matrix_world = Matrix.Identity(4)

        self.report({'INFO'}, f"Created box collider: {col_name}")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
#  Sphere Collider
# ---------------------------------------------------------------------------

class WITCH_OT_create_sphere_collider(bpy.types.Operator):
    """Create a sphere collider from the bounding sphere of the selected mesh"""
    bl_idname = "witcher.create_sphere_collider"
    bl_label = "Create Sphere Collider"
    bl_options = {'REGISTER', 'UNDO'}

    segments: IntProperty(name="Segments", default=16, min=8, max=64)
    physical_material: EnumProperty(
        name="Physical Material",
        description="Physical material for the collision shape",
        items=PHYSICAL_MATERIAL_ENUM_ITEMS,
        default=DEFAULT_PHYSICAL_MATERIAL,
    )

    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'MESH'

    def execute(self, context):
        import bmesh
        from math import pi, sin, cos

        source = context.active_object
        base_name = _get_collider_base_name(source)
        col_name = _unique_object_name(f"{base_name}_sphere")

        # Compute bounding sphere
        bbox = [source.matrix_world @ Vector(corner) for corner in source.bound_box]
        center = sum(bbox, Vector()) / 8
        radius = max((v - center).length for v in bbox)

        # Create UV sphere
        bm = bmesh.new()
        bmesh.ops.create_uvsphere(bm, u_segments=self.segments, v_segments=self.segments // 2, radius=radius)

        mesh = bpy.data.meshes.new(col_name)
        bm.to_mesh(mesh)
        bm.free()

        obj = _create_collider_object(context, source, col_name, mesh, physical_material=self.physical_material)
        obj.matrix_world = Matrix.Translation(center)

        self.report({'INFO'}, f"Created sphere collider: {col_name}")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
#  Capsule Collider
# ---------------------------------------------------------------------------

class WITCH_OT_create_capsule_collider(bpy.types.Operator):
    """Create a capsule collider aligned to the longest axis of the bounding box"""
    bl_idname = "witcher.create_capsule_collider"
    bl_label = "Create Capsule Collider"
    bl_options = {'REGISTER', 'UNDO'}

    segments: IntProperty(name="Segments", default=16, min=8, max=64)
    physical_material: EnumProperty(
        name="Physical Material",
        description="Physical material for the collision shape",
        items=PHYSICAL_MATERIAL_ENUM_ITEMS,
        default=DEFAULT_PHYSICAL_MATERIAL,
    )

    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'MESH'

    def execute(self, context):
        import bmesh
        from math import pi, sin, cos

        source = context.active_object
        base_name = _get_collider_base_name(source)
        col_name = _unique_object_name(f"{base_name}_capsule")

        # Bounding box in world space
        bbox = [source.matrix_world @ Vector(corner) for corner in source.bound_box]
        min_co = Vector((min(v.x for v in bbox), min(v.y for v in bbox), min(v.z for v in bbox)))
        max_co = Vector((max(v.x for v in bbox), max(v.y for v in bbox), max(v.z for v in bbox)))
        center = (min_co + max_co) / 2
        size = max_co - min_co

        # Find longest axis for capsule direction
        dims = [(size.x, 0), (size.y, 1), (size.z, 2)]
        dims.sort(key=lambda d: d[0], reverse=True)
        long_axis = dims[0][1]
        half_length = dims[0][0] / 2

        # Radius from the two shorter axes
        short_axes = [d[0] for d in dims[1:]]
        radius = max(short_axes) / 2

        # Build capsule using cylinder + icospheres
        bm = bmesh.new()

        # Cylinder body (subtract sphere caps from height)
        cyl_half = max(0, half_length - radius)
        if cyl_half > 0:
            bmesh.ops.create_cone(bm,
                cap_ends=False,
                segments=self.segments,
                radius1=radius,
                radius2=radius,
                depth=cyl_half * 2)

        # Top hemisphere
        top_sphere = bmesh.new()
        bmesh.ops.create_uvsphere(top_sphere, u_segments=self.segments, v_segments=self.segments // 2, radius=radius)
        # Keep only top half
        geom_to_remove = [v for v in top_sphere.verts if v.co.z < -0.001]
        bmesh.ops.delete(top_sphere, geom=geom_to_remove, context='VERTS')
        # Translate up
        for v in top_sphere.verts:
            v.co.z += cyl_half

        # Bottom hemisphere
        bot_sphere = bmesh.new()
        bmesh.ops.create_uvsphere(bot_sphere, u_segments=self.segments, v_segments=self.segments // 2, radius=radius)
        geom_to_remove = [v for v in bot_sphere.verts if v.co.z > 0.001]
        bmesh.ops.delete(bot_sphere, geom=geom_to_remove, context='VERTS')
        for v in bot_sphere.verts:
            v.co.z -= cyl_half

        # Merge all into one mesh
        mesh = bpy.data.meshes.new(col_name)
        # Simple approach: create from the cylinder bmesh, add hemisphere meshes separately
        bm.to_mesh(mesh)
        bm.free()

        # Create temp meshes for hemispheres
        top_mesh = bpy.data.meshes.new("_temp_top")
        top_sphere.to_mesh(top_mesh)
        top_sphere.free()

        bot_mesh = bpy.data.meshes.new("_temp_bot")
        bot_sphere.to_mesh(bot_mesh)
        bot_sphere.free()

        # Combine into final bmesh
        final_bm = bmesh.new()
        final_bm.from_mesh(mesh)
        final_bm.from_mesh(top_mesh)
        final_bm.from_mesh(bot_mesh)
        # Match imported Redkit capsule convention: local capsule axis = X.
        # The generator is authored on local Z, so rotate geometry once here.
        bmesh.ops.transform(final_bm, matrix=Matrix.Rotation(-pi / 2, 4, 'Y'), verts=final_bm.verts)
        final_bm.to_mesh(mesh)
        final_bm.free()

        # Clean up temp meshes
        bpy.data.meshes.remove(top_mesh)
        bpy.data.meshes.remove(bot_mesh)

        obj = _create_collider_object(context, source, col_name, mesh, physical_material=self.physical_material)

        # Rotate object so local X (capsule axis) aligns with the detected world axis.
        rot = Matrix.Identity(4)
        if long_axis == 1:  # Y is longest
            rot = Matrix.Rotation(pi / 2, 4, 'Z')
        elif long_axis == 2:  # Z is longest
            rot = Matrix.Rotation(pi / 2, 4, 'Y')
        # X is default (no rotation needed)

        obj.matrix_world = Matrix.Translation(center) @ rot

        self.report({'INFO'}, f"Created capsule collider: {col_name}")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
#  Convex Hull Collider (_col)
# ---------------------------------------------------------------------------

class WITCH_OT_create_convex_collider(bpy.types.Operator):
    """Create a convex hull collider from the selected mesh"""
    bl_idname = "witcher.create_convex_collider"
    bl_label = "Create Convex Collider"
    bl_options = {'REGISTER', 'UNDO'}

    physical_material: EnumProperty(
        name="Physical Material",
        description="Physical material for the collision shape",
        items=PHYSICAL_MATERIAL_ENUM_ITEMS,
        default=DEFAULT_PHYSICAL_MATERIAL,
    )

    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'MESH'

    def execute(self, context):
        import bmesh

        source = context.active_object
        base_name = _get_collider_base_name(source)
        col_name = _unique_object_name(f"{base_name}_col")

        # Build convex hull from source mesh vertices
        bm = bmesh.new()
        bm.from_mesh(source.data)

        # Transform verts to world space
        for v in bm.verts:
            v.co = source.matrix_world @ v.co

        result = bmesh.ops.convex_hull(bm, input=bm.verts)

        # Remove interior geometry
        interior = result.get("geom_interior", [])
        unused = result.get("geom_unused", [])
        to_delete = [g for g in (interior + unused) if isinstance(g, bmesh.types.BMVert)]
        if to_delete:
            bmesh.ops.delete(bm, geom=to_delete, context='VERTS')

        mesh = bpy.data.meshes.new(col_name)
        bm.to_mesh(mesh)
        bm.free()

        obj = _create_collider_object(context, source, col_name, mesh, physical_material=self.physical_material)
        obj.matrix_world = Matrix.Identity(4)

        self.report({'INFO'}, f"Created convex collider: {col_name} ({len(mesh.polygons)} faces)")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
#  Triangle Mesh Collider (_tri)
# ---------------------------------------------------------------------------

class WITCH_OT_create_trimesh_collider(bpy.types.Operator):
    """Create a triangle mesh collider (triangulated copy) from the selected mesh"""
    bl_idname = "witcher.create_trimesh_collider"
    bl_label = "Create Trimesh Collider"
    bl_options = {'REGISTER', 'UNDO'}

    ratio: FloatProperty(
        name="Decimation Ratio",
        description="Target ratio of faces to keep (1.0 = no decimation)",
        default=1.0, min=0.01, max=1.0
    )
    merge_distance: FloatProperty(
        name="Merge Distance",
        description="Merge vertices closer than this distance (cleanup)",
        default=0.0, min=0.0, max=1.0,
        precision=4
    )
    use_dissolve: BoolProperty(
        name="Dissolve Flat Faces",
        description="Dissolve co-planar faces before decimation for cleaner geometry",
        default=False
    )
    dissolve_angle: FloatProperty(
        name="Dissolve Angle",
        description="Maximum angle between faces to dissolve (radians)",
        default=0.087,  # ~5 degrees
        min=0.0, max=1.5708,  # 0 to 90 degrees
        subtype='ANGLE'
    )
    physical_material: EnumProperty(
        name="Physical Material",
        description="Physical material for the collision shape",
        items=PHYSICAL_MATERIAL_ENUM_ITEMS,
        default=DEFAULT_PHYSICAL_MATERIAL,
    )

    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'MESH'

    def execute(self, context):
        import bmesh

        source = context.active_object
        base_name = _get_collider_base_name(source)
        col_name = _unique_object_name(f"{base_name}_tri")

        # Duplicate mesh data
        new_mesh = source.data.copy()
        new_mesh.name = col_name

        # --- Optional cleanup pass with bmesh ---
        bm = bmesh.new()
        bm.from_mesh(new_mesh)

        # 1. Remove loose vertices and edges
        loose_verts = [v for v in bm.verts if not v.link_faces]
        if loose_verts:
            bmesh.ops.delete(bm, geom=loose_verts, context='VERTS')
        loose_edges = [e for e in bm.edges if not e.link_faces]
        if loose_edges:
            bmesh.ops.delete(bm, geom=loose_edges, context='EDGES')

        # 2. Merge vertices by distance
        if self.merge_distance > 0:
            bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=self.merge_distance)

        # 3. Dissolve co-planar faces (simplifies flat areas before decimation)
        if self.use_dissolve:
            bmesh.ops.dissolve_limit(
                bm, angle_limit=self.dissolve_angle,
                use_dissolve_boundaries=False,
                verts=bm.verts, edges=bm.edges
            )

        bm.to_mesh(new_mesh)
        bm.free()

        # Create the object using helper
        obj = _create_collider_object(context, source, col_name, new_mesh, physical_material=self.physical_material)

        # --- Modifier pass ---
        # 4. Decimate
        if self.ratio < 1.0:
            mod = obj.modifiers.new(name="Tri_Decimate", type='DECIMATE')
            mod.ratio = self.ratio
            ctx = context.copy()
            ctx['object'] = obj
            with context.temp_override(**ctx):
                bpy.ops.object.modifier_apply(modifier=mod.name)

        # 5. Triangulate
        mod = obj.modifiers.new(name="Tri_Triangulate", type='TRIANGULATE')
        ctx = context.copy()
        ctx['object'] = obj
        with context.temp_override(**ctx):
            bpy.ops.object.modifier_apply(modifier=mod.name)

        orig_faces = len(source.data.polygons)
        final_tris = len(obj.data.polygons)
        self.report({'INFO'}, f"Created trimesh collider: {col_name} ({orig_faces} → {final_tris} tris)")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
#  Sound Info operators
# ---------------------------------------------------------------------------

class WITCH_OT_create_sound_info(bpy.types.Operator):
    """Add SMeshSoundInfo to the active mesh"""
    bl_idname = "witcher.create_sound_info"
    bl_label = "Create Sound Info"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'MESH'

    def execute(self, context):
        obj = context.active_object
        obj.witcherui_MeshSettings.soundInfo_enabled = True
        obj.witcherui_MeshSettings.soundInfo_soundTypeIdentification = 'flesh'
        obj.witcherui_MeshSettings.soundInfo_soundSizeIdentification = 'default'
        obj.witcherui_MeshSettings.soundInfo_soundBoneMappingInfo = 'NONE'
        self.report({'INFO'}, "Created Sound Info")
        return {'FINISHED'}


class WITCH_OT_remove_sound_info(bpy.types.Operator):
    """Remove SMeshSoundInfo from the active mesh"""
    bl_idname = "witcher.remove_sound_info"
    bl_label = "Remove Sound Info"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'MESH'

    def execute(self, context):
        obj = context.active_object
        obj.witcherui_MeshSettings.soundInfo_enabled = False
        obj.witcherui_MeshSettings.soundInfo_soundTypeIdentification = ''
        obj.witcherui_MeshSettings.soundInfo_soundSizeIdentification = ''
        obj.witcherui_MeshSettings.soundInfo_soundBoneMappingInfo = 'NONE'
        self.report({'INFO'}, "Removed Sound Info")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
#  Main export operator
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
#  Toggle Rotate Bones 90°
# ---------------------------------------------------------------------------

class WITCH_OT_toggle_rot90(bpy.types.Operator):
    """Toggle the display orientation fix on the active rig and connected rigs"""
    bl_idname = "witcher.toggle_rot90"
    bl_label = "Toggle Rotate Bones 90"
    bl_description = (
        "Switch between game-space rig orientation and Blender display orientation. "
        "Witcher rigs use game coordinates; Blender's edit-bone display/links are cleaner "
        "with a 90-degree Z compensation. This updates connected child rigs and special attachments."
    )
    bl_options = {'REGISTER', 'UNDO'}

    _SPECIAL_ATTACHMENT_TYPES = {
        "CAnimatedComponent",
        "CAnimDangleConstraint",
        "CAnimDangleComponent",
        "CCameraComponent",
    }
    _SPECIAL_ATTACHMENT_HINTS = ("CAnimated", "CAnimDangle", "CCameraComponent")

    @classmethod
    def poll(cls, context):
        return cls._resolve_seed_armature(context) is not None

    @staticmethod
    def _resolve_seed_armature(context):
        obj = context.active_object
        if obj and obj.type == 'ARMATURE':
            return obj
        if obj and obj.parent and obj.parent.type == 'ARMATURE':
            return obj.parent
        return None

    def _get_child_armatures(self, context, seed):
        """Find seed + all armatures connected to it via parenting, constraints, or entity namespace."""
        armatures = [seed]
        found = {seed.name}
        scene_arms = [o for o in context.scene.objects if o.type == 'ARMATURE' and o != seed]

        # Iteratively expand: parented to or constrained to a found armature
        expanding = True
        while expanding:
            expanding = False
            for arm in scene_arms:
                if arm.name in found:
                    continue
                linked = False
                # Parented to a found armature?
                if arm.parent and arm.parent.name in found:
                    linked = True
                # Has pose constraints targeting a found armature?
                if not linked and arm.pose:
                    for pb in arm.pose.bones:
                        for c in pb.constraints:
                            tgt = getattr(c, 'target', None)
                            if tgt and tgt.name in found:
                                linked = True
                                break
                        if linked:
                            break
                if linked:
                    armatures.append(arm)
                    found.add(arm.name)
                    expanding = True

        # Entity namespace fallback (e.g. "player:" prefix)
        if ":" in seed.name:
            prefix = seed.name.split(":", 1)[0] + ":"
            for arm in scene_arms:
                if arm.name not in found and arm.name.startswith(prefix):
                    armatures.append(arm)
                    found.add(arm.name)

        return armatures

    def _get_root_pose_bone(self, armature_obj):
        if not armature_obj or armature_obj.type != 'ARMATURE' or not armature_obj.pose:
            return None
        for pb in armature_obj.pose.bones:
            if pb.parent is None:
                return pb
        if armature_obj.pose.bones:
            return armature_obj.pose.bones[0]
        return None

    def _guess_parent_bone_name(self, child_arm):
        parent_arm = child_arm.parent if child_arm and child_arm.parent and child_arm.parent.type == 'ARMATURE' else None
        if not parent_arm:
            return ""

        meta_bone = str(child_arm.get("w2_special_parent_bone", "")).strip()
        if meta_bone and parent_arm.pose and parent_arm.pose.bones.get(meta_bone):
            return meta_bone

        if child_arm.parent_type == 'BONE' and child_arm.parent_bone:
            return child_arm.parent_bone

        root_pb = self._get_root_pose_bone(child_arm)
        if root_pb:
            for c in root_pb.constraints:
                target = getattr(c, "target", None)
                if c.type in {'COPY_TRANSFORMS', 'CHILD_OF'} and target == parent_arm and c.subtarget:
                    return c.subtarget

            if parent_arm.pose and parent_arm.pose.bones.get(root_pb.name):
                return root_pb.name

        if parent_arm.pose:
            for pb in parent_arm.pose.bones:
                if pb.parent is None:
                    return pb.name
        return ""

    def _is_special_attachment_armature(self, arm_obj):
        if not arm_obj or arm_obj.type != 'ARMATURE':
            return False
        if str(arm_obj.get("w2_special_attachment_mode", "")).strip() == "matched_armature":
            return False
        if bool(arm_obj.get("w2_special_attachment", False)):
            return True
        if arm_obj.get("witcher_type") in self._SPECIAL_ATTACHMENT_TYPES:
            return True
        parent = arm_obj.parent if arm_obj.parent and arm_obj.parent.type == 'ARMATURE' else None
        root_pb = self._get_root_pose_bone(arm_obj)
        if parent and root_pb:
            for c in root_pb.constraints:
                if c.type == 'COPY_TRANSFORMS' and getattr(c, "target", None) == parent:
                    return True
        name = arm_obj.name
        return any(hint in name for hint in self._SPECIAL_ATTACHMENT_HINTS)

    def _rebind_special_attachment(self, child_arm, use_rot90):
        parent_arm = child_arm.parent if child_arm and child_arm.parent and child_arm.parent.type == 'ARMATURE' else None
        if not parent_arm:
            return False

        parent_bone = self._guess_parent_bone_name(child_arm)
        if not parent_bone:
            return False

        root_pb = self._get_root_pose_bone(child_arm)
        if root_pb:
            for c in list(root_pb.constraints):
                if c.type == 'COPY_TRANSFORMS' and getattr(c, "target", None) == parent_arm:
                    root_pb.constraints.remove(c)

        world_matrix = child_arm.matrix_world.copy()
        child_arm.parent = parent_arm

        # Unified mode: special attachment armatures always use root COPY_TRANSFORMS.
        child_arm.parent_type = 'OBJECT'
        child_arm.parent_bone = ""
        if root_pb:
            has_copy = False
            for c in root_pb.constraints:
                if c.type == 'COPY_TRANSFORMS' and getattr(c, "target", None) == parent_arm and c.subtarget == parent_bone:
                    has_copy = True
                    break
            if not has_copy:
                c = root_pb.constraints.new('COPY_TRANSFORMS')
                c.name = f"{parent_bone} to {root_pb.name}"
                c.target = parent_arm
                c.subtarget = parent_bone

        child_arm.matrix_world = world_matrix
        return True

    def _collect_inter_rig_constraint_pairs(self, armatures):
        pairs = []
        arm_set = set(armatures)
        for child in armatures:
            if self._is_special_attachment_armature(child):
                continue
            targets = []
            if child.pose:
                for pb in child.pose.bones:
                    for c in pb.constraints:
                        target = getattr(c, "target", None)
                        if (
                            c.type in {'COPY_TRANSFORMS', 'CHILD_OF'}
                            and target
                            and target.type == 'ARMATURE'
                            and target in arm_set
                            and target != child
                        ):
                            if target not in targets:
                                targets.append(target)

            parent = child.parent if child.parent and child.parent.type == 'ARMATURE' else None
            if parent and parent in arm_set and parent != child and parent not in targets:
                targets.insert(0, parent)

            chosen_target = None
            if parent and parent in targets:
                chosen_target = parent
            elif targets:
                chosen_target = targets[0]

            if chosen_target is not None:
                pairs.append((chosen_target, child))
        return pairs

    def _clear_inter_rig_constraints(self, pairs):
        removed = 0
        for parent, child in pairs:
            if not child.pose:
                continue
            for pb in child.pose.bones:
                for c in list(pb.constraints):
                    if c.type in {'COPY_TRANSFORMS', 'CHILD_OF'} and getattr(c, "target", None) == parent:
                        pb.constraints.remove(c)
                        removed += 1
        return removed

    def _rebuild_inter_rig_constraints(self, pairs):
        rebuilt = 0
        for parent, child in pairs:
            try:
                constrain_util.CreateConstraints2(parent, child)
                rebuilt += 1
            except Exception:
                continue
        return rebuilt

    def _rotate_bones(self, armature_obj, apply):
        """Rotate edit bones -90 (apply) or +90 (remove) around Z."""
        bpy.ops.object.mode_set(mode='EDIT')
        if apply:
            import_rig.rotate_and_connect_bones(armature_obj)
        else:
            rotation_matrix = Matrix.Rotation(math.radians(90), 4, 'Z')
            for bone in armature_obj.data.edit_bones:
                original_head = bone.head.copy()
                bone.matrix = bone.matrix @ rotation_matrix
                bone.head = original_head

                direction = bone.tail - bone.head
                if direction.length > 0:
                    direction = direction.normalized()
                else:
                    direction = Vector((0.0, 1.0, 0.0))
                bone.tail = bone.head + (direction * 0.01)

                if bone.children:
                    child_head = bone.children[0].head
                    dir_to_child = child_head - bone.head
                    if dir_to_child.length > 0:
                        if (bone.tail - bone.head).normalized().dot(dir_to_child.normalized()) > 0.999:
                            bone.tail = child_head
        bpy.ops.object.mode_set(mode='OBJECT')

    def _refresh_slot_constraints(self, armatures):
        refreshed = 0
        try:
            from ..ui.ui_equipment import refresh_slot_constraints
        except Exception:
            return refreshed

        for arm in armatures:
            rig_settings = getattr(arm.data, "witcherui_RigSettings", None)
            if rig_settings and len(getattr(rig_settings, "entity_slots", [])):
                try:
                    refreshed += int(refresh_slot_constraints(arm))
                except Exception:
                    continue
        return refreshed

    def _has_loaded_equipment(self, armatures):
        for arm in armatures:
            rig_settings = getattr(arm.data, "witcherui_RigSettings", None)
            if not rig_settings:
                continue
            for slot in getattr(rig_settings, "equipment_slots", []):
                if getattr(slot, "is_loaded", False):
                    return True
        return False

    def execute(self, context):
        seed = self._resolve_seed_armature(context)
        if not seed:
            self.report({'WARNING'}, "Select an armature or skinned mesh")
            return {'CANCELLED'}

        rig_settings = getattr(seed.data, "witcherui_RigSettings", None)
        if not rig_settings:
            self.report({'WARNING'}, "No rig settings on this armature")
            return {'CANCELLED'}

        seed_current = get_rig_rot90_enabled(rig_settings, default=False)
        target_enabled = not seed_current

        armatures = self._get_child_armatures(context, seed)
        armatures_by_name = {arm.name: arm for arm in armatures}
        armatures = list(armatures_by_name.values())

        original_active = context.view_layer.objects.active
        original_selection = list(context.selected_objects)
        original_mode = context.mode
        if original_mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        rotated_count = 0
        rebound_count = 0
        rebuilt_pairs = 0
        refreshed_slots = 0

        try:
            inter_rig_pairs = self._collect_inter_rig_constraint_pairs(armatures)
            self._clear_inter_rig_constraints(inter_rig_pairs)

            for arm in armatures:
                arm_settings = getattr(arm.data, "witcherui_RigSettings", None)
                arm_current = get_rig_rot90_enabled(arm_settings, default=seed_current)
                if arm_current == target_enabled:
                    continue
                bpy.ops.object.select_all(action='DESELECT')
                arm.select_set(True)
                context.view_layer.objects.active = arm
                self._rotate_bones(arm, apply=target_enabled)
                rotated_count += 1

            for arm in armatures:
                arm_settings = getattr(arm.data, "witcherui_RigSettings", None)
                if arm_settings:
                    set_rig_rot90_enabled(arm_settings, target_enabled)

            for arm in armatures:
                if arm == seed:
                    continue
                if not self._is_special_attachment_armature(arm):
                    continue
                if arm.parent not in armatures:
                    continue
                if self._rebind_special_attachment(arm, use_rot90=target_enabled):
                    rebound_count += 1

            rebuilt_pairs = self._rebuild_inter_rig_constraints(inter_rig_pairs)
            # Avoid reapplying slot transforms while equipment is loaded (can introduce 90° offsets).
            if not self._has_loaded_equipment(armatures):
                refreshed_slots = self._refresh_slot_constraints(armatures)
        finally:
            bpy.ops.object.select_all(action='DESELECT')
            for sel in original_selection:
                if sel and sel.name in bpy.data.objects:
                    sel.select_set(True)
            if original_active and original_active.name in bpy.data.objects:
                context.view_layer.objects.active = original_active
            if original_mode != 'OBJECT':
                try:
                    bpy.ops.object.mode_set(mode=original_mode)
                except Exception:
                    pass
            context.view_layer.update()

        action_text = "applied" if target_enabled else "removed"
        self.report(
            {'INFO'},
            f"Rot90 {action_text}: rotated {rotated_count}, rebound {rebound_count}, rebuilt links {rebuilt_pairs}, refreshed slots {refreshed_slots}"
        )
        return {'FINISHED'}


def _merge_hierarchy_get_selected_armatures(context):
    return [obj for obj in context.selected_objects if obj.type == 'ARMATURE']


def _merge_hierarchy_get_selected_empties(context):
    return [obj for obj in context.selected_objects if obj.type == 'EMPTY']


def _merge_hierarchy_build_hierarchy(armatures):
    arm_names = {a.name for a in armatures}
    children_map = defaultdict(list)
    roots = []

    for arm in armatures:
        parent = arm.parent
        while parent and parent.name not in arm_names:
            parent = parent.parent
        if parent and parent.name in arm_names:
            children_map[parent.name].append(arm.name)
        else:
            roots.append(arm.name)

    return roots, children_map


def _merge_hierarchy_build_direct_parent_map(armatures):
    arm_names = {a.name for a in armatures}
    parent_by_child = {}
    children_map = defaultdict(list)
    roots = []

    for arm in armatures:
        parent = arm.parent if arm.parent and arm.parent.type == 'ARMATURE' else None
        if parent and parent.name in arm_names:
            parent_by_child[arm.name] = parent.name
            children_map[parent.name].append(arm.name)
        else:
            roots.append(arm.name)

    return roots, parent_by_child, children_map


def _merge_hierarchy_depth(name, parent_by_child):
    depth = 0
    seen = set()
    node = name
    while node in parent_by_child and node not in seen:
        seen.add(node)
        node = parent_by_child[node]
        depth += 1
    return depth


def _merge_hierarchy_postorder_children(parent_name, children_map):
    order = []
    for child_name in children_map.get(parent_name, []):
        order.extend(_merge_hierarchy_postorder_children(child_name, children_map))
        order.append(child_name)
    return order


def _merge_hierarchy_choose_top_root(roots):
    if not roots:
        return ""
    if len(roots) == 1:
        return roots[0]

    root_set = set(roots)
    inbound_counts = {name: 0 for name in roots}

    for source_name in roots:
        source_obj = _merge_hierarchy_safe_get(source_name)
        root_pb = _merge_hierarchy_get_root_pose_bone(source_obj)
        if not root_pb:
            continue

        targets = set()
        for c in root_pb.constraints:
            target = getattr(c, "target", None)
            if (
                c.type in {'COPY_TRANSFORMS', 'CHILD_OF'}
                and target
                and target.type == 'ARMATURE'
                and target.name in root_set
                and target.name != source_name
            ):
                targets.add(target.name)
        for target_name in targets:
            inbound_counts[target_name] += 1

    max_inbound = max(inbound_counts.values()) if inbound_counts else 0
    if max_inbound <= 0:
        return roots[0]

    # Stable tie-break: keep original roots order.
    for root_name in roots:
        if inbound_counts.get(root_name, 0) == max_inbound:
            return root_name
    return roots[0]


def _merge_hierarchy_get_processing_order(roots, children_map):
    order = []
    queue = list(roots)
    while queue:
        node = queue.pop(0)
        order.append(node)
        queue.extend(children_map.get(node, []))
    order.reverse()
    return order


def _merge_hierarchy_deselect_all():
    bpy.ops.object.select_all(action='DESELECT')


def _merge_hierarchy_set_active(obj):
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)


def _merge_hierarchy_safe_get(name):
    return bpy.data.objects.get(name)


def _merge_hierarchy_get_root_pose_bone(armature_obj):
    if not armature_obj or armature_obj.type != 'ARMATURE' or not armature_obj.pose:
        return None
    for pb in armature_obj.pose.bones:
        if pb.parent is None:
            return pb
    if armature_obj.pose.bones:
        return armature_obj.pose.bones[0]
    return None


def _merge_hierarchy_guess_attachment_bone(source_arm, target_arm):
    if not source_arm or not target_arm or source_arm.type != 'ARMATURE' or target_arm.type != 'ARMATURE':
        return ""

    target_pose_bones = target_arm.pose.bones if target_arm.pose else None
    if not target_pose_bones:
        return ""

    # Importer metadata for special attachment components.
    meta_bone = str(source_arm.get("w2_special_parent_bone", "")).strip()
    if meta_bone and target_pose_bones.get(meta_bone):
        return meta_bone

    # Direct bone parenting.
    if source_arm.parent == target_arm and source_arm.parent_type == 'BONE' and source_arm.parent_bone:
        if target_pose_bones.get(source_arm.parent_bone):
            return source_arm.parent_bone

    # Common Witcher attachment mode: object parent + root COPY_TRANSFORMS/CHILD_OF to parent bone.
    root_pb = _merge_hierarchy_get_root_pose_bone(source_arm)
    if root_pb:
        for c in root_pb.constraints:
            target = getattr(c, "target", None)
            if (
                c.type in {'COPY_TRANSFORMS', 'CHILD_OF'}
                and target == target_arm
                and c.subtarget
                and target_pose_bones.get(c.subtarget)
            ):
                return c.subtarget

    return ""


def _merge_hierarchy_apply_pose_constraints(arm_obj):
    _merge_hierarchy_deselect_all()
    _merge_hierarchy_set_active(arm_obj)
    bpy.ops.object.mode_set(mode='POSE')

    bpy.ops.pose.select_all(action='SELECT')
    try:
        bpy.ops.pose.visual_transform_apply()
    except Exception:
        for pbone in arm_obj.pose.bones:
            mat_local = arm_obj.convert_space(
                pose_bone=pbone,
                matrix=pbone.matrix,
                from_space='POSE',
                to_space='LOCAL',
            )
            pbone.matrix_basis = mat_local

    for pbone in arm_obj.pose.bones:
        for constraint in list(pbone.constraints):
            pbone.constraints.remove(constraint)

    bpy.ops.object.mode_set(mode='OBJECT')


def _merge_hierarchy_apply_rest_pose(arm_obj):
    _merge_hierarchy_deselect_all()
    _merge_hierarchy_set_active(arm_obj)
    bpy.ops.object.mode_set(mode='POSE')
    bpy.ops.pose.select_all(action='SELECT')
    bpy.ops.pose.armature_apply(selected=False)
    bpy.ops.object.mode_set(mode='OBJECT')


def _merge_hierarchy_world_space_bone_data(arm_obj, bone):
    mat = arm_obj.matrix_world @ bone.matrix_local
    head = mat @ Vector((0.0, 0.0, 0.0))
    tail = mat @ Vector((0.0, bone.length, 0.0))
    return head, tail, mat


def _merge_hierarchy_merge_armature_into(target_arm, source_arm, attachment_parent_bone=""):
    source_bones_data = {}
    for bone in source_arm.data.bones:
        head_ws, tail_ws, mat_ws = _merge_hierarchy_world_space_bone_data(source_arm, bone)
        source_bones_data[bone.name] = {
            'head': head_ws,
            'tail': tail_ws,
            'matrix': mat_ws,
            'parent_name': bone.parent.name if bone.parent else None,
            'use_connect': bool(bone.use_connect),
            'collections': [c.name for c in bone.collections] if hasattr(bone, 'collections') else [],
        }

    all_children = [obj for obj in bpy.data.objects if obj.parent == source_arm]
    child_parent_cache = []
    for child_obj in all_children:
        child_parent_cache.append({
            "obj": child_obj,
            "world_mat": child_obj.matrix_world.copy(),
            "parent_type": child_obj.parent_type,
            "parent_bone": child_obj.parent_bone if child_obj.parent_type == 'BONE' else "",
        })

    attachment_parent_bone = str(attachment_parent_bone or "").strip()
    if not attachment_parent_bone:
        attachment_parent_bone = _merge_hierarchy_guess_attachment_bone(source_arm, target_arm)

    for obj in bpy.data.objects:
        if obj.type != 'MESH':
            continue
        for mod in obj.modifiers:
            if mod.type == 'ARMATURE' and mod.object == source_arm:
                mod.object = target_arm

    _merge_hierarchy_deselect_all()
    _merge_hierarchy_set_active(target_arm)
    bpy.ops.object.mode_set(mode='EDIT')

    target_inv = target_arm.matrix_world.inverted()
    edit_bones = target_arm.data.edit_bones
    existing_target_bones = {bone.name for bone in target_arm.data.bones}
    source_root_bones = {bname for bname, bdata in source_bones_data.items() if not bdata['parent_name']}
    skipped_source_roots = {bname for bname in source_root_bones if bname in existing_target_bones}

    for bname, bdata in source_bones_data.items():
        if bname in existing_target_bones:
            # Keep top-rig bone shape/length exactly as authored on the target rig.
            continue

        eb = edit_bones.get(bname)
        if eb is None:
            eb = edit_bones.new(bname)

        head_local = target_inv @ bdata['head']
        tail_local = target_inv @ bdata['tail']
        if (tail_local - head_local).length < 1e-8:
            tail_local = head_local + Vector((0.0, 0.01, 0.0))

        eb.head = head_local
        eb.tail = tail_local
        try:
            roll_vec = (target_inv.to_3x3() @ bdata['matrix'].to_3x3()) @ Vector((0.0, 0.0, 1.0))
            eb.align_roll(roll_vec)
        except Exception:
            pass

    for bname, bdata in source_bones_data.items():
        if bname in existing_target_bones:
            # Do not re-parent or reconnect bones that already exist on the top rig.
            continue

        eb = edit_bones.get(bname)
        if eb is None:
            continue
        parent_name = bdata['parent_name']
        if parent_name and parent_name in skipped_source_roots and attachment_parent_bone and attachment_parent_bone in edit_bones:
            # Exception: source root bone was skipped (name collision). Re-anchor that subtree to the
            # source armature's mount bone on the target rig (e.g. scabbard Root -> torso3).
            eb.parent = edit_bones[attachment_parent_bone]
            eb.use_connect = False
        elif parent_name and parent_name in edit_bones:
            eb.parent = edit_bones[parent_name]
            eb.use_connect = bdata['use_connect']
        elif (not parent_name) and attachment_parent_bone and attachment_parent_bone in edit_bones:
            # Preserve source-armature mount point (for example parented/constraint-mounted to jaw).
            eb.parent = edit_bones[attachment_parent_bone]
            eb.use_connect = False

    bpy.ops.object.mode_set(mode='OBJECT')

    for child_data in child_parent_cache:
        child_obj = child_data["obj"]
        if child_obj is None or child_obj.name not in bpy.data.objects:
            continue
        world_mat = child_data["world_mat"]
        original_parent_bone = str(child_data.get("parent_bone", "") or "")

        child_obj.parent = target_arm
        if original_parent_bone and target_arm.data.bones.get(original_parent_bone):
            child_obj.parent_type = 'BONE'
            child_obj.parent_bone = original_parent_bone
        else:
            child_obj.parent_type = 'OBJECT'
            child_obj.parent_bone = ''
        child_obj.matrix_world = world_mat

    if hasattr(target_arm.data, 'collections'):
        for bname, bdata in source_bones_data.items():
            bone = target_arm.data.bones.get(bname)
            if bone is None:
                continue
            for coll_name in bdata['collections']:
                coll = target_arm.data.collections.get(coll_name)
                if coll is None:
                    coll = target_arm.data.collections.new(coll_name)
                if hasattr(coll, 'assign'):
                    coll.assign(bone)

    source_name = source_arm.name
    target_name = target_arm.name

    # Update mimicFace references that point to the source armature being deleted
    for obj in bpy.data.objects:
        if obj.type == 'ARMATURE' and obj.get('mimicFace') == source_name:
            obj['mimicFace'] = target_name

    source_arm_data = source_arm.data
    bpy.data.objects.remove(source_arm, do_unlink=True)
    if source_arm_data and source_arm_data.users == 0:
        bpy.data.armatures.remove(source_arm_data)

    log.info("Merged armature '%s' -> '%s'", source_name, target_name)


def _merge_hierarchy_cleanup_childless_empties(empty_names):
    deleted_count = 0
    changed = True

    while changed:
        changed = False
        for ename in list(empty_names):
            obj = bpy.data.objects.get(ename)
            if obj is None or obj.type != 'EMPTY':
                empty_names.discard(ename)
                continue

            children = [o for o in bpy.data.objects if o.parent == obj]
            if not children:
                bpy.data.objects.remove(obj, do_unlink=True)
                empty_names.discard(ename)
                deleted_count += 1
                changed = True

    return deleted_count


class WITCH_OT_merge_armature_hierarchy(bpy.types.Operator):
    """Merge selected armatures into one rig (experimental and destructive)"""
    bl_idname = "witcher.merge_armature_hierarchy"
    bl_label = "Merge Armature Hierarchy"
    bl_description = (
        "Experimental and destructive. Merge selected armature hierarchy into one rig. "
        "Recommended only for final full-model export (for example Unreal Engine). "
        "This will break equipment and appearance-changing systems."
    )
    bl_options = {'REGISTER', 'UNDO'}

    confirm_ok: BoolProperty(
        name="OK",
        description=(
            "Required confirmation. This operation is experimental and will break equipment and "
            "appearance-changing systems on the merged character."
        ),
        default=False,
    )

    @classmethod
    def poll(cls, context):
        if context.mode != 'OBJECT':
            return False
        return any(obj.type == 'ARMATURE' for obj in context.selected_objects)

    def invoke(self, context, event):
        self.confirm_ok = False
        return context.window_manager.invoke_props_dialog(self, width=560)

    def draw(self, context):
        layout = self.layout
        warn = layout.box()
        col = warn.column(align=True)
        col.label(text="Hierarchy Merge", icon='INFO')
        col.label(text="Merges selected armatures into a single top-level rig.")
        col.label(text="Breaks equipment and appearance-changing systems.")
        col.label(text="Recommended only for final full-model export (e.g. Unreal Engine).")
        layout.prop(self, "confirm_ok", text="OK, I understand and want to continue")

    def execute(self, context):
        if not self.confirm_ok:
            self.report({'WARNING'}, "Tick OK to confirm this experimental destructive operation")
            return {'CANCELLED'}

        original_active = context.view_layer.objects.active
        original_selection = list(context.selected_objects)
        original_mode = context.mode
        if original_mode != 'OBJECT':
            try:
                bpy.ops.object.mode_set(mode='OBJECT')
            except Exception:
                pass

        armatures = _merge_hierarchy_get_selected_armatures(context)
        selected_empty_names = {obj.name for obj in _merge_hierarchy_get_selected_empties(context)}

        if len(armatures) < 1:
            self.report({'ERROR'}, "Select at least one armature")
            return {'CANCELLED'}

        roots, parent_by_child, _children_map = _merge_hierarchy_build_direct_parent_map(armatures)
        if not roots:
            self.report({'ERROR'}, "Could not determine hierarchy root from selected armatures")
            return {'CANCELLED'}
        top_rig_name = _merge_hierarchy_choose_top_root(roots) or roots[0]
        ordered_roots = [top_rig_name] + [r for r in roots if r != top_rig_name]
        if len(ordered_roots) > 1:
            self.report(
                {'WARNING'},
                f"Multiple top-level rigs selected. Processing subtrees one-by-one, then merging into '{top_rig_name}'.",
            )

        merge_order = []
        for root_name in ordered_roots:
            # For each root subtree: fully merge each child subtree before the next sibling.
            merge_order.extend(_merge_hierarchy_postorder_children(root_name, _children_map))
        # Finally merge extra roots into the chosen top rig, one by one.
        for root_name in ordered_roots[1:]:
            merge_order.append(root_name)

        prep_order = sorted(
            [a.name for a in armatures],
            key=lambda n: _merge_hierarchy_depth(n, parent_by_child),
            reverse=True,
        )

        source_attachment_bones = {}
        for arm_name in merge_order:
            source_obj = _merge_hierarchy_safe_get(arm_name)
            if source_obj is None:
                continue
            target_name = parent_by_child.get(arm_name)
            if not target_name:
                target_name = top_rig_name
            if target_name == arm_name:
                continue
            target_obj = _merge_hierarchy_safe_get(target_name)
            if target_obj is None:
                continue
            source_attachment_bones[arm_name] = _merge_hierarchy_guess_attachment_bone(source_obj, target_obj)
        merged_count = 0
        deleted_empties = 0
        success = False

        try:
            for arm_name in prep_order:
                arm_obj = _merge_hierarchy_safe_get(arm_name)
                if arm_obj is None:
                    continue
                _merge_hierarchy_apply_pose_constraints(arm_obj)
                _merge_hierarchy_apply_rest_pose(arm_obj)

            for arm_name in merge_order:
                source = _merge_hierarchy_safe_get(arm_name)
                if source is None:
                    continue

                target_name = parent_by_child.get(arm_name)
                if not target_name:
                    target_name = top_rig_name
                target = _merge_hierarchy_safe_get(target_name)
                if target is None:
                    self.report({'ERROR'}, f"Parent rig '{target_name}' for '{arm_name}' disappeared")
                    return {'CANCELLED'}
                if target == source:
                    continue

                _merge_hierarchy_merge_armature_into(
                    target,
                    source,
                    attachment_parent_bone=source_attachment_bones.get(arm_name, ""),
                )
                merged_count += 1

            deleted_empties = _merge_hierarchy_cleanup_childless_empties(selected_empty_names)
            success = True
        finally:
            _merge_hierarchy_deselect_all()
            if success:
                final_rig = _merge_hierarchy_safe_get(top_rig_name)
                if final_rig:
                    _merge_hierarchy_set_active(final_rig)
                context.view_layer.update()
            else:
                for obj in original_selection:
                    if obj and obj.name in bpy.data.objects:
                        obj.select_set(True)
                if original_active and original_active.name in bpy.data.objects:
                    context.view_layer.objects.active = original_active
                if original_mode != 'OBJECT' and context.view_layer.objects.active:
                    try:
                        bpy.ops.object.mode_set(mode=original_mode)
                    except Exception:
                        pass

        final_rig = _merge_hierarchy_safe_get(top_rig_name)
        bone_count = len(final_rig.data.bones) if final_rig else 0
        self.report(
            {'INFO'},
            f"Merged {merged_count} rig(s) into '{top_rig_name}' ({bone_count} bones), cleaned {deleted_empties} empty/empties",
        )
        return {'FINISHED'}


def _join_meshes_for_export(context, meshes):
    """Temporarily join multiple mesh objects into one for single-LOD export.

    Creates duplicates of all meshes and joins them so the exporter sees one
    combined LOD.  The caller is responsible for deleting the returned object
    after the export with ``bpy.data.objects.remove(obj, do_unlink=True)``.
    """
    bpy.ops.object.select_all(action='DESELECT')
    for m in meshes:
        m.select_set(True)
    bpy.context.view_layer.objects.active = meshes[0]

    bpy.ops.object.duplicate(linked=False)

    # After duplicate, the new copies are selected and originals are not
    new_objs = [ob for ob in bpy.context.selected_objects if ob.type == 'MESH']

    if len(new_objs) == 1:
        return new_objs[0]

    # Join all copies into one object
    bpy.context.view_layer.objects.active = new_objs[0]
    bpy.ops.object.join()
    return bpy.context.view_layer.objects.active


class WITCH_OT_w2mesh_export(bpy.types.Operator, ExportHelper):
    """Export Witcher 3 Mesh File"""
    bl_idname = "witcher.export_w2mesh"
    bl_label = "Export .w2mesh"
    filename_ext = ".w2mesh"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if not context.selected_objects:
            return False
        return any(ob.type in ('MESH', 'ARMATURE') for ob in context.selected_objects)

    filter_glob: StringProperty(default='*.w2mesh', options={'HIDDEN'})
    
    keep_intermediate_json: BoolProperty(
        name="Keep Intermediate JSON",
        description="Keep the intermediate .json file after conversion to .w2mesh (for debugging)",
        default=False
    )

    use_wolvenkit_json: BoolProperty(
        name="Use Legacy WolvenKit Export",
        description="Use WolvenKit CLI for export (slower, but keeps intermediate JSON)",
        default=False
    )
    
    export_col_tri: BoolProperty(
        name="Export Collision and Trigger Meshes",
        description="Toggle the export of collision meshes (_col, _tri, _box, _sphere, _capsule)",
        default=True
    )

    strip_material_names: BoolProperty(
        name="Strip Material Names",
        description="Export materials as Material0, Material1, etc. instead of their Blender names",
        default=False
    )

    # Section collapse toggles
    show_redkit_project: BoolProperty(name="Show REDkit Project", default=True, options={'HIDDEN'})
    show_mesh_settings: BoolProperty(name="Show Mesh Settings", default=False, options={'HIDDEN'})
    show_sound_info: BoolProperty(name="Show Sound Info", default=False, options={'HIDDEN'})
    show_materials: BoolProperty(name="Show Materials", default=True, options={'HIDDEN'})
    show_lods: BoolProperty(name="Show LODs", default=True, options={'HIDDEN'})
    show_colliders: BoolProperty(name="Show Colliders", default=False, options={'HIDDEN'})
    show_collider_tools: BoolProperty(name="Show Collider Tools", default=False, options={'HIDDEN'})
    show_lod_tools: BoolProperty(name="Show LOD Tools", default=False, options={'HIDDEN'})
    show_advanced: BoolProperty(name="Show Advanced", default=False, options={'HIDDEN'})

    # Collision suffixes to detect
    COLLISION_SUFFIXES = ("_col", "_tri", "_box", "_sphere", "_capsule")

    def get_collision_type(self, obj_name):
        """
        Detect collision type from object name, handling Blender's .NNN suffix.
        Returns the collision suffix (_col, _tri, _box, _sphere, _capsule) or None.

        Examples:
            'mesh_box' -> '_box'
            'mesh_box.001' -> '_box'
            'mesh_tri.003' -> '_tri'
        """
        # Strip Blender's .NNN suffix if present
        base_name = re.sub(r'\.\d{3}$', '', obj_name)
        for suffix in self.COLLISION_SUFFIXES:
            if base_name.endswith(suffix):
                return suffix
        return None

    def find_related_meshes(self, base_name):
        lod_meshes = []
        col_tri_meshes = []
        # Iterate through all objects in the scene
        for obj in bpy.context.scene.objects:
            # Check for LOD meshes
            if obj.name.startswith(base_name) and obj.name[len(base_name):].startswith("_lod"):
                lod_meshes.append(obj)
            # Check for collision meshes (_col, _tri, _box, _sphere, _capsule)
            # Handles Blender's .NNN suffix (e.g., mesh_box.001)
            elif obj.name.startswith(base_name):
                col_type = self.get_collision_type(obj.name)
                if col_type:
                    col_tri_meshes.append(obj)
        return lod_meshes, col_tri_meshes

    def _classify_armature_children(self, armature):
        """Classify mesh children of an armature into LOD-named and non-LOD-named groups.

        Returns (lod_named, non_lod_named) where:
        - lod_named: children whose names end with _lod0, _lod1, etc.
        - non_lod_named: all other mesh children
        """
        children = [c for c in armature.children if c.type == 'MESH']
        lod_named = [c for c in children if re.search(r'_lod\d+$', c.name)]
        non_lod = [c for c in children if c not in lod_named]
        return lod_named, non_lod
    
    def _draw_section_header(self, layout, prop_name, label, icon):
        """Draw a collapsible section header. Returns the box to draw into, or None if collapsed."""
        box = layout.box()
        row = box.row()
        is_open = getattr(self, prop_name)
        row.prop(self, prop_name, icon='TRIA_DOWN' if is_open else 'TRIA_RIGHT',
                 text=label, emboss=False, icon_only=False)
        if icon != 'NONE':
            row.label(text="", icon=icon)
        return box if is_open else None

    def draw(self, context):
        layout = self.layout
        obj = context.active_object

        # --- Determine selection context (armature vs. standalone meshes) ---
        selected_armatures = [ob for ob in context.selected_objects if ob.type == 'ARMATURE']
        _arm_lod_named = []
        _arm_non_lod = []
        _arm_has_children = True
        if selected_armatures:
            _arm_lod_named, _arm_non_lod = self._classify_armature_children(selected_armatures[0])
            _arm_has_children = bool(_arm_lod_named or _arm_non_lod)

        # --- Export Summary ---
        summary_box = layout.box()
        if selected_armatures:
            arm = selected_armatures[0]
            if not _arm_has_children:
                row = summary_box.row()
                row.alert = True
                row.label(text=f"Armature '{arm.name}' has no mesh children to export.", icon='ERROR')
            elif _arm_non_lod and not _arm_lod_named:
                summary_box.label(text=f"Skeletal mesh — rig: {arm.name}", icon='ARMATURE_DATA')
                row = summary_box.row()
                row.alert = True
                row.label(text=f"{len(_arm_non_lod)} mesh(es) not named _lod — will combine into 1 LOD", icon='ERROR')
                summary_box.label(text="Tip: rename to _lod0/_lod1/... for explicit LOD control")
            else:
                summary_box.label(text=f"Skeletal mesh — rig: {arm.name}", icon='ARMATURE_DATA')
                summary_box.label(text=f"Will export {len(_arm_lod_named)} LOD(s) from named children", icon='INFO')

            # isStatic warning: skeletal meshes should not have isStatic enabled
            export_meshes_preview = _arm_lod_named if _arm_lod_named else (_arm_non_lod or [])
            for _m in export_meshes_preview:
                if hasattr(_m, 'witcherui_MeshSettings') and _m.witcherui_MeshSettings.isStatic:
                    row = summary_box.row()
                    row.alert = True
                    row.label(text="isStatic is ON — disable it for skeletal meshes", icon='ERROR')
                    break

            # UV2 / useExtraStreams tip
            for _m in export_meshes_preview:
                if _m.type == 'MESH':
                    _has_uv2 = len(_m.data.uv_layers) > 1
                    _has_vcol = _m.data.color_attributes.active_color_index != -1 and _m.data.color_attributes.active
                    _extra = hasattr(_m, 'witcherui_MeshSettings') and _m.witcherui_MeshSettings.useExtraStreams
                    if (_has_uv2 or _has_vcol) and not _extra:
                        parts = []
                        if _has_uv2:
                            parts.append("UV2")
                        if _has_vcol:
                            parts.append("vertex color")
                        row = summary_box.row()
                        row.alert = True
                        row.label(text=f"{', '.join(parts)} data found — enable 'Use Extra Streams'", icon='INFO')
                        break
        else:
            main_mesh_preview = _get_main_mesh(context)
            if main_mesh_preview:
                base_name_preview = main_mesh_preview.name.rsplit('_lod0', 1)[0]
                lod_preview, _ = self.find_related_meshes(base_name_preview)
                n_lods = len(lod_preview) if lod_preview else 1
                summary_box.label(text=f"Static mesh — will export {n_lods} LOD(s)", icon='MESH_DATA')

                # UV2 / useExtraStreams tip for standalone mesh exports
                _check_meshes = lod_preview if lod_preview else [main_mesh_preview]
                for _m in _check_meshes:
                    if _m.type == 'MESH':
                        _has_uv2 = len(_m.data.uv_layers) > 1
                        _has_vcol = _m.data.color_attributes.active_color_index != -1 and _m.data.color_attributes.active
                        _extra = hasattr(_m, 'witcherui_MeshSettings') and _m.witcherui_MeshSettings.useExtraStreams
                        if (_has_uv2 or _has_vcol) and not _extra:
                            parts = []
                            if _has_uv2:
                                parts.append("UV2")
                            if _has_vcol:
                                parts.append("vertex color")
                            row = summary_box.row()
                            row.alert = True
                            row.label(text=f"{', '.join(parts)} data found — enable 'Use Extra Streams'", icon='INFO')
                            break
            else:
                row = summary_box.row()
                row.alert = True
                row.label(text="No active mesh selected", icon='ERROR')

        # --- REDkit Project Section ---
        project_path = _get_active_redkit_project(context)
        workspace_root = _get_workspace_root(project_path) if project_path else None

        main_mesh = _get_main_mesh(context)
        repo_path = ""
        if main_mesh:
            repo_path = main_mesh.witcherui_MeshSettings.item_repo_path

        box = self._draw_section_header(layout, 'show_redkit_project', "REDkit Project", 'FILE_FOLDER')
        if box:
            if project_path:
                project_name = os.path.basename(project_path)
                box.label(text=f"Project: {project_name}")

                # Show workspace root (truncated for readability)
                if workspace_root:
                    col = box.column(align=True)
                    col.scale_y = 0.8
                    col.label(text="Workspace:")
                    col.label(text=f"  {workspace_root}")
            else:
                box.label(text="No REDkit project set", icon='ERROR')
                box.label(text="Configure in addon preferences")

            # Editable repo path on the mesh
            if main_mesh:
                box.separator()
                box.prop(main_mesh.witcherui_MeshSettings, "item_repo_path", text="Repo Path")
                box.operator("witcher.set_repo_path_from_browser", text="Set Repo from Current Folder", icon='FILE_FOLDER')

            # Computed full path
            if workspace_root and repo_path:
                full_path = _compute_full_export_path(workspace_root, repo_path)
                if full_path:
                    col = box.column(align=True)
                    col.scale_y = 0.8
                    col.label(text="Full Path:")
                    # Split long paths across lines for readability
                    dir_part = os.path.dirname(full_path)
                    file_part = os.path.basename(full_path)
                    col.label(text=f"  {dir_part}")
                    col.label(text=f"  {file_part}")

            # Go To Project Path button
            box.separator()
            row = box.row()
            row.scale_y = 1.3
            if project_path:
                if repo_path:
                    row.operator("witcher.export_goto_project_path",
                                 text="Go To Project Path",
                                 icon='FILEBROWSER')
                else:
                    row.operator("witcher.export_goto_project_path",
                                 text="Go To Workspace",
                                 icon='FILEBROWSER')
            else:
                row.enabled = False
                row.operator("witcher.export_goto_project_path",
                             text="No Project Set",
                             icon='ERROR')

        # --- Mesh Settings Section ---
        mesh_ob = _get_main_mesh(context)
        if mesh_ob:
            mesh_settings = mesh_ob.witcherui_MeshSettings

            box = self._draw_section_header(layout, 'show_mesh_settings', "Mesh Settings", 'MESH_DATA')
            if box:
                box.prop(mesh_settings, "lod_level")
                box.prop(mesh_settings, "distance")
                box.separator()
                box.prop(mesh_settings, "autohideDistance")
                box.prop(mesh_settings, "isTwoSided")
                box.prop(mesh_settings, "useExtraStreams")
                row = box.row()
                row.prop(mesh_settings, "generalizedMeshRadius")
                row.enabled = False
                box.prop(mesh_settings, "mergeInGlobalShadowMesh")
                box.prop(mesh_settings, "isOccluder")
                box.prop(mesh_settings, "smallestHoleOverride")
                box.prop(mesh_settings, "isStatic")
                box.prop(mesh_settings, "entityProxy")

            # --- Sound Info Section ---
            box = self._draw_section_header(layout, 'show_sound_info', "Sound Info", 'SOUND')
            if box:
                if mesh_settings.soundInfo_enabled:
                    row = box.row()
                    row.operator("witcher.remove_sound_info", text="Remove Sound Info", icon='X')
                    box.prop(mesh_settings, "soundInfo_soundTypeIdentification", text="Sound Type Identification")
                    box.prop(mesh_settings, "soundInfo_soundSizeIdentification", text="Sound Size Identification")
                    box.prop(mesh_settings, "soundInfo_soundBoneMappingInfo", text="Bone Mapping Preset")
                else:
                    row = box.row()
                    row.operator("witcher.create_sound_info", text="Create Sound Info", icon='ADD')

            # Dynamically find the LOD mesh list.
            # For armature exports: use the actual armature children (what execute() will use).
            # For standalone mesh exports: search by _lod naming convention.
            if selected_armatures:
                if _arm_lod_named:
                    lod_meshes = sorted(_arm_lod_named, key=lambda x: x.name)
                else:
                    # Non-LOD children: they'll be combined into one LOD0 at export time
                    lod_meshes = sorted(_arm_non_lod, key=lambda x: x.name)
                col_tri_meshes = []
            else:
                base_name = mesh_ob.name.rsplit('_lod0', 1)[0]
                lod_meshes, col_tri_meshes = self.find_related_meshes(base_name)

            # --- Material Export Order Preview ---
            preview_meshes = lod_meshes if lod_meshes else [mesh_ob]
            box = self._draw_section_header(layout, 'show_materials', "Material Export Order", 'MATERIAL')
            if box:
                box.prop(self, "strip_material_names")
                from ..w3_material_nodes import get_group_inputs, get_socket_value
                from ..exporters.export_mesh import scan_principled_bsdf
                import re as _re
                import os as _os
                # repo_path is already computed from main_mesh.witcherui_MeshSettings.item_repo_path
                _mesh_repo_dir = _os.path.dirname(repo_path.replace('/', '\\')) if repo_path else ""
                for lod_mesh in sorted(preview_meshes, key=lambda x: x.name):
                    lod_label = lod_mesh.name.split('_')[-1] if '_lod' in lod_mesh.name else lod_mesh.name
                    col = box.column(align=True)
                    col.label(text=f"{lod_label}:")

                    if lod_mesh.data.materials:
                        for mat_idx, mat in enumerate(lod_mesh.data.materials):
                            if not mat:
                                col.label(text=f"  {mat_idx}: (empty)")
                                continue
                            mat_name = mat.name
                            is_local = hasattr(mat, 'witcher_props') and mat.witcher_props.local
                            if self.strip_material_names:
                                match = _re.search(r'(Material\d+)', mat_name)
                                stripped = match.group(1) if match else f"Material{mat_idx}"
                                row = col.row()
                                row.label(text=f"  {mat_idx}: {mat_name}")
                                row.label(text=f"->  {stripped}")
                            else:
                                col.label(text=f"  {mat_idx}: {mat_name}")
                            if is_local:
                                group_inputs = get_group_inputs(mat)
                                if group_inputs:
                                    # Witcher node group: show connected texture inputs
                                    for inp in group_inputs:
                                        if inp.is_linked:
                                            linked = inp.links[0].from_socket
                                            if linked.node.type == 'TEX_IMAGE' and linked.node.image:
                                                tex_path = get_socket_value(inp)
                                                if isinstance(tex_path, str):
                                                    sub = col.row()
                                                    sub.scale_y = 0.7
                                                    sub.label(text=f"      {inp.name}: {tex_path}", icon='TEXTURE')
                                else:
                                    # No Witcher node group — check for Principled BSDF auto-convert
                                    bsdf_found = scan_principled_bsdf(mat, _mesh_repo_dir)
                                    if bsdf_found is not None:
                                        sub = col.row()
                                        sub.alert = True
                                        sub.label(text="      Auto-convert from Principled BSDF → pbr_std:", icon='INFO')
                                        if bsdf_found:
                                            for p in bsdf_found:
                                                sub2 = col.row()
                                                sub2.scale_y = 0.7
                                                sub2.label(text=f"      {p['name']}: {p['value']}", icon='TEXTURE')
                                        else:
                                            sub2 = col.row()
                                            sub2.scale_y = 0.7
                                            sub2.label(text="      (no textures found — pbr_std with defaults)", icon='DOT')
                            else:
                                # Non-local: show the w2mi/w2mg depot path
                                if hasattr(mat, 'witcher_props') and mat.witcher_props.base_custom:
                                    sub = col.row()
                                    sub.scale_y = 0.7
                                    sub.label(text=f"      {mat.witcher_props.base_custom}", icon='LINKED')
                    else:
                        col.label(text="  (no materials)")
                    box.separator()

            # --- LODs ---
            # For armature exports: always show the mesh list (even non-LOD named children).
            # For standalone mesh exports: only show if LOD meshes were found.
            show_lod_section = bool(lod_meshes) or (selected_armatures and _arm_has_children)
            if show_lod_section:
                if selected_armatures and _arm_non_lod and not _arm_lod_named:
                    section_label = f"Meshes ({len(_arm_non_lod)}) — will combine into LOD0"
                else:
                    section_label = f"LODs ({len(lod_meshes)})"
                box = self._draw_section_header(layout, 'show_lods', section_label, 'MOD_DECIM')
                if box:
                    for lod_mesh in lod_meshes:
                        row = box.row()
                        if selected_armatures and _arm_non_lod and not _arm_lod_named:
                            row.label(text=f"{lod_mesh.name}", icon='MESH_DATA')
                        else:
                            row.label(text=f"{lod_mesh.name}")
                            if hasattr(lod_mesh, "witcherui_MeshSettings"):
                                row.prop(lod_mesh.witcherui_MeshSettings, "distance", text="Dist")

            # --- Collision Meshes ---
            if col_tri_meshes:
                box = self._draw_section_header(layout, 'show_colliders', f"Collision Meshes ({len(col_tri_meshes)})", 'MOD_PHYSICS')
                if box:
                    for col_mesh in col_tri_meshes:
                        row = box.row()
                        col_type = self.get_collision_type(col_mesh.name) or "collision"
                        row.label(text=f"{col_mesh.name} ({col_type})")
                    box.prop(self, "export_col_tri", text="Export All Collision Meshes")

            # --- LOD Tools ---
            box = self._draw_section_header(layout, 'show_lod_tools', "LOD Tools", 'MOD_DECIM')
            if box:
                box.operator("witcher.generate_lods", text="Generate LODs", icon='MESH_DATA')

            # --- Collider Tools ---
            box = self._draw_section_header(layout, 'show_collider_tools', "Collider Tools", 'MOD_PHYSICS')
            if box:
                selected_material = DEFAULT_PHYSICAL_MATERIAL
                if hasattr(context.scene, "witcher_collision_physical_material"):
                    box.prop(context.scene, "witcher_collision_physical_material", text="Physical Material")
                    selected_material = context.scene.witcher_collision_physical_material
                row = box.row(align=True)
                op = row.operator("witcher.create_box_collider", text="Box", icon='MESH_CUBE')
                op.physical_material = selected_material
                op = row.operator("witcher.create_sphere_collider", text="Sphere", icon='MESH_UVSPHERE')
                op.physical_material = selected_material
                row = box.row(align=True)
                op = row.operator("witcher.create_capsule_collider", text="Capsule", icon='MESH_CAPSULE')
                op.physical_material = selected_material
                op = row.operator("witcher.create_convex_collider", text="Convex", icon='MESH_ICOSPHERE')
                op.physical_material = selected_material
                row = box.row(align=True)
                op = row.operator("witcher.create_trimesh_collider", text="Trimesh", icon='MESH_DATA')
                op.physical_material = selected_material

            # --- Advanced ---
            box = self._draw_section_header(layout, 'show_advanced', "Advanced", 'PREFERENCES')
            if box:
                box.prop(self, "use_wolvenkit_json")
                if self.use_wolvenkit_json:
                    box.prop(self, "keep_intermediate_json")

    def execute(self, context):
        if not bpy.context.selected_objects:
            self.report({'ERROR'}, "ERROR Nothing selected to export.")
            return {'CANCELLED'}
        
        # Save selection state to restore after export
        original_active = bpy.context.view_layer.objects.active
        original_selection = [obj for obj in bpy.context.selected_objects]
        original_mode = bpy.context.object.mode if bpy.context.object else 'OBJECT'
        
        # Ensure we're in object mode for export
        if original_mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        
        # Warn if mesh has SecondUV or vertex color but useExtraStreams is off
        mesh_ob = _get_main_mesh(context)
        if mesh_ob:
            settings = mesh_ob.witcherui_MeshSettings
            if not settings.useExtraStreams:
                me = mesh_ob.data
                has_second_uv = len(me.uv_layers) > 1
                has_vertex_color = me.color_attributes.active_color_index != -1 and me.color_attributes.active
                if has_second_uv or has_vertex_color:
                    parts = []
                    if has_second_uv:
                        parts.append("Second UV")
                    if has_vertex_color:
                        parts.append("vertex color")
                    self.report({'WARNING'}, f"Mesh has {' and '.join(parts)} data but 'Use Extra Streams' is off. Enable it to include this data in the export.")

            # Check for unresolved absolute texture paths in materials
            unresolved_mats = []
            for mat_slot in mesh_ob.material_slots:
                mat = mat_slot.material
                if mat and hasattr(mat, 'witcher_props') and mat.witcher_props.local:
                    from ..w3_material_nodes import get_group_inputs, get_socket_value, is_path_resolved
                    group_inputs = get_group_inputs(mat)
                    if group_inputs:
                        for input_socket in group_inputs:
                            if input_socket.is_linked:
                                linked_socket = input_socket.links[0].from_socket
                                if linked_socket.node.type == 'TEX_IMAGE' and linked_socket.node.image:
                                    val = get_socket_value(input_socket)
                                    if isinstance(val, str) and not is_path_resolved(val):
                                        unresolved_mats.append(f"{mat.name}:{input_socket.name}")
            if unresolved_mats:
                self.report({'WARNING'}, f"Unresolved absolute texture paths in: {', '.join(unresolved_mats[:5])}. Check addon path settings (REDkit Depot, Uncook, etc).")

        try:
            log.debug("Exporting w2mesh")
            fdir = self.filepath
            ext = file_helpers.getFilenameType(fdir)
            if ext == ".w2mesh":
                s = time.time()
                self.do_merge_normals = False
                
                selected_armatures = [ob for ob in original_selection if ob.type == 'ARMATURE']

                for armature in selected_armatures:
                    armature_meshes = [child for child in armature.children if child.type == 'MESH']

                    if not armature_meshes:
                        self.report({'ERROR'}, f"Armature '{armature.name}' has no mesh children to export.")
                        return {'CANCELLED'}

                    # Classify children: those following _lod naming are explicit LODs;
                    # everything else gets combined into a single LOD0.
                    lod_named = [m for m in armature_meshes if re.search(r'_lod\d+$', m.name)]
                    non_lod = [m for m in armature_meshes if m not in lod_named]

                    temp_joined = None
                    if non_lod and not lod_named:
                        # No _lod naming: combine everything into one LOD0
                        self.report({'INFO'}, f"Combining {len(non_lod)} non-LOD mesh children into a single LOD0.")
                        if len(non_lod) == 1:
                            meshes = non_lod
                        else:
                            temp_joined = _join_meshes_for_export(context, non_lod)
                            meshes = [temp_joined]
                    elif lod_named:
                        meshes = lod_named
                        if non_lod:
                            self.report({'WARNING'}, f"Ignoring {len(non_lod)} non-LOD-named children: {[m.name for m in non_lod]}. Only _lod named meshes will be exported.")
                    else:
                        meshes = armature_meshes

                    try:
                        mesh_back = do_export_mesh(context, fdir,
                                            armature = armature,
                                            meshes = meshes,
                                            keep_intermediate_json = self.keep_intermediate_json,
                                            use_native_writer = not self.use_wolvenkit_json,
                                            strip_material_names = self.strip_material_names)
                    except (RuntimeError, FileNotFoundError, ValueError) as e:
                        self.report({'ERROR'}, str(e))
                        return {'CANCELLED'}
                    finally:
                        if temp_joined and temp_joined.name in bpy.data.objects:
                            bpy.data.objects.remove(temp_joined, do_unlink=True)
                if len(selected_armatures) == 0:
                    meshes = [ob for ob in original_selection if ob.type == 'MESH']
                    if not meshes:
                        self.report({'ERROR'}, "No mesh objects selected for export.")
                        return {'CANCELLED'}
                    base_name = meshes[0].name.rsplit('_lod0', 1)[0]
                    lod_meshes, col_tri_meshes = self.find_related_meshes(base_name)
                    # If no LOD meshes found, use the selected meshes directly
                    if not lod_meshes:
                        lod_meshes = meshes
                    try:
                        mesh_back = do_export_mesh(context, fdir,
                                            armature = None,
                                            meshes = lod_meshes,
                                            col_tri_meshes = col_tri_meshes,
                                            export_col_tri = self.export_col_tri,
                                            keep_intermediate_json = self.keep_intermediate_json,
                                            use_native_writer = not self.use_wolvenkit_json,
                                            strip_material_names = self.strip_material_names)
                    except (RuntimeError, FileNotFoundError, ValueError) as e:
                        self.report({'ERROR'}, str(e))
                        return {'CANCELLED'}
                message = f'Exported .w2mesh file in {time.time() - s} seconds.'
                log.info(message)
                self.report({'INFO'}, message)
            else:
                self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
                return {'CANCELLED'}
        finally:
            # Restore selection state
            bpy.ops.object.select_all(action='DESELECT')
            for obj in original_selection:
                if obj and obj.name in bpy.data.objects:
                    obj.select_set(True)
            if original_active and original_active.name in bpy.data.objects:
                bpy.context.view_layer.objects.active = original_active
            
            # Now restore mode if we have an active object
            if bpy.context.view_layer.objects.active:
                try:
                    if original_mode != 'OBJECT':
                        bpy.ops.object.mode_set(mode=original_mode)
                except Exception:
                    pass  # Mode may not be applicable
        
        return {'FINISHED'}

    def invoke(self, context, event):
        # Only set filepath on first use. On subsequent exports, Blender
        # restores the last used filepath before calling invoke(), so
        # we respect that to preserve the last export directory.
        if not self.filepath:
            default_name = "default"
            active = bpy.context.active_object
            if active:
                if active.type == 'ARMATURE':
                    # Derive filename from first mesh child; strip _ARM suffix
                    mesh_children = [c for c in active.children if c.type == 'MESH']
                    if mesh_children:
                        # Use the base name of the first mesh child (strip _lod0 suffix too)
                        default_name = mesh_children[0].name.rsplit('_lod', 1)[0]
                    else:
                        # Fall back to armature name but strip _ARM
                        default_name = re.sub(r'_ARM(?:_DATA)?$', '', active.name, flags=re.IGNORECASE) or active.name
                else:
                    default_name = active.name
            self.filepath = default_name + self.filename_ext

        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}
    
    

class WITCH_OT_nxs(bpy.types.Operator, ImportHelper):
    """Load Nvidia Collision File"""
    bl_idname = "witcher.import_nxs"
    bl_label = "Import .nxs"
    filename_ext = ".nxs"
    bl_options = {'REGISTER', 'UNDO'}
    
    filter_glob: StringProperty(default='*.nxs', options={'HIDDEN'})
    
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
            "Settings" : ["rotate_180"]
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
        if ext == ".nxs":
            s = time.time()
            import_nxs.create_from_nxs(fdir)
            message = f'Imported .nxs file in {time.time() - s} seconds.'
            log.info(message)
            self.report({'INFO'}, message)
        else:
            self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
            return {'CANCELLED'}
        return {'FINISHED'}


# WITCH_PT_mesh_tools removed — LOD & Collider tools are now in the CMesh N-panel
