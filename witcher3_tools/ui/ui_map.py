import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

import bpy
from bpy.props import StringProperty, BoolProperty, CollectionProperty, IntProperty, FloatProperty, EnumProperty
from bpy_extras.io_utils import ImportHelper, ExportHelper
import addon_utils

from .. import CR2W, get_uncook_path, get_all_addon_prefs
from ..importers import import_w2l
from ..importers import import_w2w
from ..CR2W.common_blender import repo_file

from ..exporters import export_radish

class WITCH_OT_radish_w2L(bpy.types.Operator, ExportHelper):
    """Export radish layer"""
    bl_idname = "witcher.export_w2l_yml"
    bl_label = "export .yml"
    filename_ext = ".yml"
    bl_options = {'REGISTER', 'UNDO'}

    filter_glob: StringProperty(default='*.yml', options={'HIDDEN'})

    def execute(self, context):
        fdir = self.filepath
        log.info("Exporting layer")
        exporter = export_radish.radishExporter()
        exporter.export(fdir)
        return {'FINISHED'}

class WITCH_OT_export_textures(bpy.types.Operator):
    """Export radish textures"""
    bl_idname = "witcher.export_textures"
    bl_label = "export radish textures"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        fdir = self.filepath
        log.info("Exporting textures")
        return {'FINISHED'}


class WITCH_OT_w2L(bpy.types.Operator, ImportHelper):
    """Load Witcher 3 Level"""
    bl_idname = "witcher.import_w2l"
    bl_label = "Import .w2l"

    #filepath: StringProperty(subtype='FILE_PATH', )

    filename_ext = ".w2l"
    bl_options = {'REGISTER', 'UNDO'}

    filter_glob: StringProperty(default='*.w2l', options={'HIDDEN'})
    files: bpy.props.CollectionProperty(
            type=bpy.types.OperatorFileListElement,
            options={'HIDDEN', 'SKIP_SAVE'},
        )
    do_import_Mesh: BoolProperty(
        name="Mesh",
        default=True,
        description="If enabled, mesh types are imported"
    )
    do_import_Collision: BoolProperty(
        name="Collision",
        default=True,
        description="If enabled, mesh types are imported"
    )
    do_import_RigidBody: BoolProperty(
        name="RigidBody",
        default=True,
        description="If enabled, mesh types are imported"
    )
    do_import_Entity: BoolProperty(
        name="Entity",
        default=True,
        description="If enabled, Differnt types of Entities are imported"
    )
    do_import_PointLight: BoolProperty(
        name="PointLight",
        default=True,
        description="If enabled, PointLight types are imported"
    )
    do_import_SpotLight: BoolProperty(
        name="SpotLight",
        default=True,
        description="If enabled, SpotLight types are imported"
    )
    keep_lod_meshes: BoolProperty(
        name="Keep LODs",
        default=False,
        description="If enabled, it will keep low quality meshes. An extra empty transfrom will be created for each group of meshes"
    )
    keep_empty_lods: BoolProperty(
        name="Keep Empty LODs",
        default=False,
        description="If enabled, it will keep empty mesh LODs with zero polygons"
    )
    keep_proxy_meshes: BoolProperty(
        name="Keep Proxy Meshes",
        default=True,
        description="If enabled, it will always keep any proxy meshes regardless of lod"
    )
    do_enable_name_filter: BoolProperty(
        name="Enable Regex Filter",
        default=False,
        description="If enabled, only filenames matching the regex are imported"
    )
    do_name_filter_regex: StringProperty(
        name="Regex Filter",
        default='_proxy',
        description="Enter regex string such as \"_proxy|box\""
    )
    
    def draw(self, context):
        layout = self.layout
        sections = ["Import Filter", "Settings"]
        section_options = {
            "Import Filter" : ["do_import_Mesh","do_import_Collision","do_import_RigidBody","do_import_Entity",
                               "do_import_PointLight", "do_import_SpotLight",],
            "Settings" : [
                        "keep_lod_meshes",
                        "keep_empty_lods",
                        "keep_proxy_meshes",
                        "do_enable_name_filter",
                        "do_name_filter_regex"]
        }
        for section in sections:
            row = layout.row()
            box = row.box()
            box.label(text=section)
            for prop in section_options[section]:
                box.prop(self, prop)
    
    def execute(self, context):
        log.info("Importing layer")
        fdir = self.filepath
        files = self.files
        file: bpy.types.OperatorFileListElement

        if os.path.isdir(fdir):
            self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
            return {'CANCELLED'}

        start_time = time.time()
        if fdir.endswith(".w2l"):
            cur_dir = Path(self.filepath).parent

            file_list = [f.name for f in files if f.name] or [Path(self.filepath).name]
            for fname in file_list:
                filepath = str(cur_dir / fname)
                log.info("Importing file: %s", filepath)
                levelFile = CR2W.CR2W_reader.load_w2l(filepath)
                import_w2l.btn_import_W2L(levelFile, context, self.keep_lod_meshes,
                                          keep_empty_lods = self.keep_empty_lods,
                                          keep_proxy_meshes = self.keep_proxy_meshes,
                                        do_import_Mesh = self.do_import_Mesh,
                                        do_import_Collision = self.do_import_Collision,
                                        do_import_RigidBody = self.do_import_RigidBody,
                                        do_import_Entity = self.do_import_Entity,
                                        do_import_PointLight = self.do_import_PointLight,
                                        do_import_SpotLight = self.do_import_SpotLight,
                                        do_enable_name_filter = self.do_enable_name_filter,
                                        do_name_filter_regex = self.do_name_filter_regex,
                                        )
        else:
            log.warn('Did not select .w2l')
            self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
            return {'CANCELLED'}
        message = f"Finished importing layer in {time.time() - round(start_time, 2)} seconds."
        log.info(message)
        self.report({'INFO'}, message)
        return {'FINISHED'}
    def invoke(self, context, event):
        UNCOOK_PATH = os.path.join(get_uncook_path(context),"levels\\")
        if os.path.exists(UNCOOK_PATH):
            self.filepath = UNCOOK_PATH if self.filepath == '' else self.filepath
        return ImportHelper.invoke(self, context, event)

class WITCH_OT_w2w(bpy.types.Operator, ImportHelper):
    """Load Witcher 3 Level"""
    bl_idname = "witcher.import_w2w"
    bl_label = "Import .w2w"
    filename_ext = ".w2w"
    bl_options = {'REGISTER', 'UNDO'}

    filter_glob: StringProperty(default='*.w2w;*.yml', options={'HIDDEN'})
    terrain_import_mode: EnumProperty(
        name="Terrain Import",
        description="Choose how terrain is imported",
        items=[
            ('FULL_MAP', 'Full Map', 'Import one combined map using Geometry Nodes + Multires'),
            ('TILES', 'Tiles', 'Import individual terrain tile meshes'),
        ],
        default='FULL_MAP',
    )
    terrain_multires_level: IntProperty(
        name="Terrain Multires",
        description="Multires subdivision levels used by terrain import",
        default=5,
        min=0,
        max=10,
    )
    terrain_material_roughness: FloatProperty(
        name="Terrain Roughness",
        description="Roughness for imported terrain materials",
        default=0.82,
        min=0.0,
        max=1.0,
    )
    terrain_material_specular: FloatProperty(
        name="Terrain Specular",
        description="Specular for imported terrain materials",
        default=0.12,
        min=0.0,
        max=1.0,
    )

    def _copy_settings_from_scene(self, context):
        tool = getattr(context.scene, "witcher_file_browser", None)
        if tool is None:
            return
        try:
            self.terrain_import_mode = str(getattr(tool, "terrain_import_mode", self.terrain_import_mode))
            self.terrain_multires_level = int(getattr(tool, "terrain_multires_level", self.terrain_multires_level))
            self.terrain_material_roughness = float(getattr(tool, "terrain_material_roughness", self.terrain_material_roughness))
            self.terrain_material_specular = float(getattr(tool, "terrain_material_specular", self.terrain_material_specular))
        except Exception:
            pass

    def _apply_settings_to_scene(self, context):
        tool = getattr(context.scene, "witcher_file_browser", None)
        if tool is None:
            return
        try:
            tool.terrain_import_mode = self.terrain_import_mode
            tool.terrain_multires_level = int(self.terrain_multires_level)
            if hasattr(tool, "terrain_material_roughness"):
                tool.terrain_material_roughness = float(self.terrain_material_roughness)
            if hasattr(tool, "terrain_material_specular"):
                tool.terrain_material_specular = float(self.terrain_material_specular)
        except Exception:
            pass

    def draw(self, context):
        layout = self.layout
        box = layout.box()
        box.label(text="Terrain Import", icon='GRID')
        box.prop(self, "terrain_import_mode", text="Mode")
        box.prop(self, "terrain_multires_level", text="Multires")
        box.prop(self, "terrain_material_roughness", text="Roughness")
        box.prop(self, "terrain_material_specular", text="Specular")

    def execute(self, context):
        log.info("Importing world")
        filePath = self.filepath
        self._apply_settings_to_scene(context)

        if os.path.isdir(filePath):
            log.warn('Did not select .w2w')
            self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
            return {'CANCELLED'}
        if filePath.endswith('.yml'):
            import_w2w.btn_import_radish(filePath)
        else:
            worldFile = CR2W.CR2W_reader.load_w2w(filePath)
            import_w2w.btn_import_w2w(worldFile, filePath)
        return {'FINISHED'}
    def invoke(self, context, event):
        self._copy_settings_from_scene(context)
        UNCOOK_PATH = os.path.join(get_uncook_path(context),"levels\\")
        if os.path.exists(UNCOOK_PATH):
            self.filepath = UNCOOK_PATH if self.filepath == '' else self.filepath
        return ImportHelper.invoke(self, context, event)

def _normalize_level_rel_path(level_path: str) -> str:
    if not level_path:
        return ""
    rel = str(level_path).replace("/", "\\").strip().lstrip("\\")
    if not rel:
        return ""
    return os.path.normpath(rel)


def _level_rel_variants(level_path: str):
    rel = _normalize_level_rel_path(level_path)
    if not rel:
        return []
    variants = []

    def add(path):
        norm = os.path.normpath(path)
        if norm and norm not in variants:
            variants.append(norm)

    add(rel)
    lower = rel.lower()
    if lower.startswith("levels\\"):
        add(rel[len("levels\\"):])
    else:
        add(os.path.join("levels", rel))
    return variants


def _level_search_roots(context):
    roots = []

    def add(path):
        if not path:
            return
        try:
            norm = os.path.normpath(bpy.path.abspath(path))
        except Exception:
            norm = os.path.normpath(path)
        if norm and norm not in roots:
            roots.append(norm)

    uncook_path = get_uncook_path(context)
    add(uncook_path)
    if uncook_path:
        add(os.path.join(uncook_path, "levels"))
        parent = os.path.dirname(os.path.normpath(uncook_path))
        add(parent)
        add(os.path.join(parent, "levels"))
        grandparent = os.path.dirname(parent) if parent else ""
        add(grandparent)
        add(os.path.join(grandparent, "levels"))

    try:
        prefs = get_all_addon_prefs(context)
        for attr in ("redkit_depot_path", "redkit_uncooked_path", "mod_directory", "witcher_game_path", "witcher2_game_path"):
            value = getattr(prefs, attr, "")
            add(value)
            add(os.path.join(value, "levels"))
        for item in getattr(prefs, "redkit_projects", []):
            proj = getattr(item, "path", "")
            add(proj)
            add(os.path.join(proj, "workspace"))
            add(os.path.join(proj, "workspace", "levels"))
    except Exception:
        pass

    return [root for root in roots if root and os.path.isdir(root)]


def _resolve_level_file(context, level_path: str) -> str:
    if not level_path:
        return ""
    raw = str(level_path).strip()
    if not raw:
        return ""

    if os.path.isabs(raw) and os.path.isfile(raw):
        return os.path.normpath(raw)

    variants = _level_rel_variants(raw)
    for root in _level_search_roots(context):
        for rel in variants:
            candidate = os.path.normpath(os.path.join(root, rel))
            if os.path.isfile(candidate):
                return candidate

    for rel in variants:
        try:
            candidate = repo_file(rel)
        except Exception:
            candidate = ""
        if candidate and os.path.isfile(candidate):
            return os.path.normpath(candidate)
    return ""


def _import_level_from_collection(context, coll):
    level_path = str(coll.get("level_path", "")).strip()
    if not level_path:
        return False, "", "Collection has no level_path"
    resolved = _resolve_level_file(context, level_path)
    if not resolved:
        return False, "", f"Could not resolve level path: {level_path}"
    try:
        level_file = CR2W.CR2W_reader.load_w2l(resolved)
        import_w2l.btn_import_W2L(level_file)
    except Exception as e:
        return False, resolved, str(e)
    return True, resolved, ""


def import_group(context, coll, stats):
    for child in coll.children:
        child_group_type = str(child.get("group_type", "")).strip()
        if child_group_type == "LayerInfo":
            log.info("LOADING LEVEL %s", child.name)
            ok, resolved, err = _import_level_from_collection(context, child)
            if ok:
                stats["imported"] += 1
            else:
                stats["failed"] += 1
                msg = f"Can't load level {child.name} ({str(child.get('level_path', ''))})"
                if resolved:
                    msg += f" from {resolved}"
                if err:
                    msg += f": {err}"
                log.warning("%s", msg)
                stats["messages"].append(msg)
    for child in coll.children:
        child_group_type = str(child.get("group_type", "")).strip()
        if child_group_type == "LayerGroup":
            log.info("LAYER_GROUP %s", child.name)
            import_group(context, child, stats)

class WITCH_OT_load_layer_group(bpy.types.Operator):
    """IMPORT_LAYER_ButtonOperator"""
    bl_idname = "witcher.load_layer_group"
    bl_label = "Load This LayerGroup"

    def execute(self, context):
        coll = context.collection
        if coll:
            start_time = time.time()
            stats = {"imported": 0, "failed": 0, "messages": []}
            import_group(context, coll, stats)
            log.info(' Finished importing LayerGroup in %f seconds.', time.time() - start_time)
            if stats["failed"] > 0:
                self.report({'WARNING'}, f"Imported {stats['imported']} levels, failed {stats['failed']}")
                if stats["messages"]:
                    log.warning(stats["messages"][0])
            else:
                self.report({'INFO'}, f"Imported {stats['imported']} levels")
        else:
            self.report({'WARNING'}, "No active collection")
        return {'FINISHED'}

class WITCH_OT_load_layer(bpy.types.Operator):
    """Load Layer ButtonOperator"""
    bl_idname = "witcher.load_layer"
    bl_label = "Load This Layer"

    # @classmethod
    # def poll(cls, context):
    #     return context.layer_collection is not None

    def execute(self, context):
        coll = context.collection
        if not coll:
            self.report({'WARNING'}, "No active collection")
            return {'CANCELLED'}
        ok, resolved, err = _import_level_from_collection(context, coll)
        if not ok:
            self.report({'ERROR'}, err or "Failed to load level")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Loaded level: {Path(resolved).name}")
        return {'FINISHED'}



# from bpy.utils import (register_class, unregister_class)

# _classes = [
#     WITCH_OT_w2L,
#     WITCH_OT_w2w
# ]

# def register():
#     for cls in _classes:
#         register_class(cls)

# def unregister():
#     for cls in _classes:
#         unregister_class(cls)
