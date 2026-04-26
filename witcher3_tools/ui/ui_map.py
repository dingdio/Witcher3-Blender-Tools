import logging
import os
import time
import json
import hashlib
import math
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager, nullcontext
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

log = logging.getLogger(__name__)

import bpy
from bpy.props import StringProperty, BoolProperty, CollectionProperty, IntProperty, FloatProperty, EnumProperty
from bpy_extras.io_utils import ImportHelper, ExportHelper
import addon_utils

from .. import CR2W, get_uncook_path, get_fbx_uncook_path, get_all_addon_prefs, setup_logging_bl
from ..extension_paths import get_cache_root
from ..importers import import_w2l
from ..importers import import_w2w
from ..importers import import_isolation
from ..importers import import_blender_fun
from ..CR2W import fast_cache_scan
from ..CR2W.common_blender import repo_file

from ..exporters import export_radish

_LEVEL_FILE_CACHE = {}
_WORLD_LAYER_INDEX_CACHE = {}
_WORLD_LAYER_RUNTIME_CACHE = {}
_WORLD_LAYER_SCAN_CACHE_VERSION = 4
_WORLD_LAYER_SPATIAL_CELL_SIZE = 10.0
_LAYER_SCAN_BATCH_SIZE = 16
_LAYER_LOAD_BATCH_SIZE = 8
_LAYER_SCAN_THREAD_WORKERS = max(6, min(12, int(os.cpu_count() or 6)))
_LAYER_STREAM_REDRAW_INTERVAL = 0.25
_LAYER_SCAN_TIMING_ENABLED = True
_LAYER_SCAN_LAYER_WARN_THRESHOLD = 0.25
_LAYER_SCAN_DEP_WARN_THRESHOLD = 0.05
_LAYER_SCAN_TIMING_PROGRESS_INTERVAL = 100
_LAYER_LOAD_TIMING_ENABLED = True
_LAYER_LOAD_LAYER_WARN_THRESHOLD = 0.25
_LAYER_LOAD_TIMING_PROGRESS_INTERVAL = 25
_MAP_IMPORT_PROFILE_LOG_FORMAT = "%(asctime)s %(levelname)8s %(name)s %(message)s"
_MAP_IMPORT_PROFILE_LOG_DATEFMT = "%H:%M:%S"
_layer_stream_last_redraw_ts = 0.0
_FAST_SCAN_ENTITY_TYPES = frozenset(getattr(CR2W.CR2W_types, "Entity_Type_List", ()) or ())
_FAST_SCAN_TOP_LEVEL_TYPES = frozenset({"CLayer", "CEntityTemplate", "CFoliageResource"})
_LAYER_COMPLETE_STATES = frozenset({"complete", "proxy_complete"})
_LAYER_COVERED_STATES = frozenset({"complete", "proxy_complete", "partial", "proxy_partial"})
_LAYER_QUERY_FILTER_KINDS = frozenset({
    "mesh",
    "component_mesh",
    "foliage",
    "grass",
    "collision",
    "rigid",
    "rigid_body",
    "point_light",
    "spot_light",
    "component_point_light",
    "component_spot_light",
    "cloth",
    "entity",
    "entity_template",
})
def _layer_import_kwargs_from_scene(scene_settings):
    return {
        "do_import_Mesh": bool(getattr(scene_settings, "terrain_layer_do_import_mesh", True)),
        "do_import_ProxyMesh": bool(getattr(scene_settings, "terrain_layer_do_import_proxy_mesh", False)),
        "do_import_Collision": bool(getattr(scene_settings, "terrain_layer_do_import_collision", True)),
        "do_import_RigidBody": bool(getattr(scene_settings, "terrain_layer_do_import_rigidbody", True)),
        "do_import_Entity": bool(getattr(scene_settings, "terrain_layer_do_import_entity", True)),
        "do_import_PointLight": bool(getattr(scene_settings, "terrain_layer_do_import_point_light", True)),
        "do_import_SpotLight": bool(getattr(scene_settings, "terrain_layer_do_import_spot_light", True)),
        "do_import_Redcloth": bool(getattr(scene_settings, "terrain_layer_do_import_redcloth", False)),
        "keep_lod_meshes": bool(getattr(scene_settings, "terrain_layer_keep_lod_meshes", False)),
        "keep_empty_lods": bool(getattr(scene_settings, "terrain_layer_keep_empty_lods", False)),
        "keep_proxy_meshes": bool(getattr(scene_settings, "terrain_layer_keep_proxy_meshes", True)),
        "do_enable_name_filter": bool(getattr(scene_settings, "terrain_layer_enable_name_filter", False)),
        "do_name_filter_regex": str(getattr(scene_settings, "terrain_layer_name_filter_regex", "") or ""),
    }


def _layer_import_query_filter_active(scene_settings):
    settings = _layer_import_kwargs_from_scene(scene_settings)
    content_keys = (
        "do_import_Mesh",
        "do_import_ProxyMesh",
        "do_import_Collision",
        "do_import_RigidBody",
        "do_import_Entity",
        "do_import_PointLight",
        "do_import_SpotLight",
        "do_import_Redcloth",
    )
    if any(not bool(settings.get(key, True)) for key in content_keys):
        return True
    return bool(settings.get("do_enable_name_filter")) and bool(str(settings.get("do_name_filter_regex", "") or ""))


def _layer_load_mode_signature_for_scene(scene_settings):
    dev_empty_only = False
    settings = _layer_import_kwargs_from_scene(scene_settings)
    regex = settings.get("do_name_filter_regex", "") if settings.get("do_enable_name_filter") else ""
    return (
        f"dev_empty={int(dev_empty_only)}"
        f";mesh={int(settings.get('do_import_Mesh', True))}"
        f";proxy_mesh={int(settings.get('do_import_ProxyMesh', False))}"
        f";collision={int(settings.get('do_import_Collision', True))}"
        f";rigid={int(settings.get('do_import_RigidBody', True))}"
        f";entity={int(settings.get('do_import_Entity', True))}"
        f";point={int(settings.get('do_import_PointLight', True))}"
        f";spot={int(settings.get('do_import_SpotLight', True))}"
        f";redcloth={int(settings.get('do_import_Redcloth', False))}"
        f";lods={int(settings.get('keep_lod_meshes', False))}"
        f";empty_lods={int(settings.get('keep_empty_lods', False))}"
        f";proxy={int(settings.get('keep_proxy_meshes', True))}"
        f";regex={regex}"
    )


def _new_layer_stream_job_state():
    return {
        "running": False,
        "mode": "",
        "phase": "",
        "cancel_requested": False,
        "cancelled": False,
        "timer": None,
        "wm": None,
        "context": None,
        "root_collection_name": "",
        "title": "",
        "detail": "",
        "current": 0,
        "total": 0,
        "scan": {},
        "index_data": None,
        "load": {},
        "radius": 0.0,
        "load_limit": 0,
        "skip_complete": False,
        "camera_position": None,
        "summary": "",
        "error": "",
        "profile_log_path": "",
        "profile_log_handler": None,
        "profile_log_level_changes": [],
    }


_LAYER_STREAM_JOB = _new_layer_stream_job_state()


class _MapImportProfileLogFormatter(logging.Formatter):
    def format(self, record):
        original_name = record.name
        display_name = getattr(setup_logging_bl, "_display_logger_name", None)
        if callable(display_name):
            try:
                record.name = display_name(original_name)
            except Exception:
                record.name = original_name
        try:
            return super().format(record)
        finally:
            record.name = original_name


def _map_import_profile_logger():
    addon_name = str(getattr(setup_logging_bl, "ADDON_NAME", "") or "").strip()
    if addon_name:
        return logging.getLogger(addon_name)
    return logging.getLogger(__name__.split(".", 1)[0])


def _map_import_profile_logger_names():
    root_name = _map_import_profile_logger().name or __name__.split(".", 1)[0]
    return (
        root_name,
        f"{root_name}.ui.ui_map",
        f"{root_name}.importers.import_blender_fun",
        f"{root_name}.importers.import_entity",
        f"{root_name}.importers.import_mesh",
        f"{root_name}.cloth_util",
        f"{root_name}.fbx_util",
        f"{root_name}.w3_material",
    )


def _enable_map_import_profile_loggers(job):
    changes = []
    for logger_name in _map_import_profile_logger_names():
        logger_obj = logging.getLogger(logger_name)
        previous_level = logger_obj.level
        if previous_level == logging.NOTSET or previous_level > logging.INFO:
            logger_obj.setLevel(logging.INFO)
        changes.append((logger_obj, previous_level))
    job["profile_log_level_changes"] = changes


def _restore_map_import_profile_loggers(job):
    changes = list((job or {}).get("profile_log_level_changes", []) or [])
    for logger_obj, previous_level in reversed(changes):
        try:
            logger_obj.setLevel(previous_level)
        except Exception:
            pass
    if job is not None:
        job["profile_log_level_changes"] = []


def _map_import_profile_log_root():
    log_dir = Path(get_cache_root(create=True)) / "map_import_profile_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def _sanitize_map_import_profile_label(value):
    text = str(value or "").strip()
    if not text:
        return "world"
    sanitized = "".join(ch if ch.isalnum() else "_" for ch in text)
    sanitized = sanitized.strip("_")
    while "__" in sanitized:
        sanitized = sanitized.replace("__", "_")
    return sanitized[:80] or "world"


def _map_import_profile_label(root_collection):
    if root_collection is not None:
        world_path = str(root_collection.get("world_path", "")).strip()
        if world_path:
            return _sanitize_map_import_profile_label(Path(world_path).stem)
        return _sanitize_map_import_profile_label(getattr(root_collection, "name", "world"))
    return "world"


def _create_map_import_profile_log_path(root_collection):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    label = _map_import_profile_label(root_collection)
    filename = f"map_import_profile_log_{label}_{timestamp}.txt"
    return _map_import_profile_log_root() / filename


def _start_map_import_profile_log(job, root_collection):
    if job is None:
        return ""
    if job.get("profile_log_handler") is not None:
        return str(job.get("profile_log_path", "") or "")
    try:
        log_path = _create_map_import_profile_log_path(root_collection)
        handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(_MapImportProfileLogFormatter(_MAP_IMPORT_PROFILE_LOG_FORMAT, datefmt=_MAP_IMPORT_PROFILE_LOG_DATEFMT))
        _map_import_profile_logger().addHandler(handler)
        _enable_map_import_profile_loggers(job)
    except Exception:
        log.exception("Failed to start map import profile log")
        return ""
    job["profile_log_path"] = str(log_path)
    job["profile_log_handler"] = handler
    log.info("Writing map import profile log to %s", log_path)
    return str(log_path)


def _stop_map_import_profile_log(job, completion_message=""):
    if job is None:
        return ""
    handler = job.get("profile_log_handler")
    log_path = str(job.get("profile_log_path", "") or "")
    if handler is None:
        return log_path
    try:
        if completion_message:
            log.info("%s", completion_message)
    finally:
        try:
            _map_import_profile_logger().removeHandler(handler)
        except Exception:
            pass
        try:
            handler.close()
        except Exception:
            pass
        job["profile_log_handler"] = None
        _restore_map_import_profile_loggers(job)
    return log_path

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
        default=10,
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


def _dedupe_existing_roots(roots):
    clean = []
    seen = set()

    for root in roots:
        if not root:
            continue
        norm = os.path.normpath(root)
        if norm in seen:
            continue
        if not os.path.isdir(norm):
            continue
        seen.add(norm)
        clean.append(norm)
    return clean


def _uncook_level_search_roots(context):
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

    return _dedupe_existing_roots(roots)


def _extra_level_search_roots(context):
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

    return _dedupe_existing_roots(roots)


def _path_is_within_root(path: str, root: str) -> bool:
    if not path or not root:
        return False
    try:
        norm_path = os.path.normcase(os.path.normpath(path))
        norm_root = os.path.normcase(os.path.normpath(root))
    except Exception:
        return False
    return norm_path == norm_root or norm_path.startswith(norm_root.rstrip("\\/") + os.sep)


def _world_source_prefers_uncook(context, root_collection=None) -> bool:
    if root_collection is None:
        return False
    world_path = str(root_collection.get("world_path", "")).strip()
    if not world_path:
        return False
    for root in _uncook_level_search_roots(context):
        if _path_is_within_root(world_path, root):
            return True
    return False


def _level_search_roots(context, root_collection=None):
    uncook_roots = _uncook_level_search_roots(context)
    extra_roots = _extra_level_search_roots(context)
    if _world_source_prefers_uncook(context, root_collection):
        return uncook_roots, extra_roots, True
    return uncook_roots + extra_roots, [], False


def _resolve_level_file(context, level_path: str, root_collection=None) -> str:
    if not level_path:
        return ""
    raw = str(level_path).strip()
    if not raw:
        return ""

    primary_roots, secondary_roots, prefer_repo_extract = _level_search_roots(context, root_collection)
    cache_key = (
        tuple(primary_roots),
        tuple(secondary_roots),
        bool(prefer_repo_extract),
        os.path.normcase(raw),
    )
    cached = _LEVEL_FILE_CACHE.get(cache_key)
    if cached is not None:
        return cached

    if os.path.isabs(raw) and os.path.isfile(raw):
        resolved = os.path.normpath(raw)
        _LEVEL_FILE_CACHE[cache_key] = resolved
        return resolved

    variants = _level_rel_variants(raw)
    for root in primary_roots:
        for rel in variants:
            candidate = os.path.normpath(os.path.join(root, rel))
            if os.path.isfile(candidate):
                _LEVEL_FILE_CACHE[cache_key] = candidate
                return candidate

    def try_repo_file():
        for rel in variants:
            try:
                candidate = repo_file(rel)
            except Exception:
                candidate = ""
            if candidate and os.path.isfile(candidate):
                resolved = os.path.normpath(candidate)
                _LEVEL_FILE_CACHE[cache_key] = resolved
                return resolved
        return ""

    if prefer_repo_extract:
        resolved = try_repo_file()
        if resolved:
            return resolved

    for root in secondary_roots:
        for rel in variants:
            candidate = os.path.normpath(os.path.join(root, rel))
            if os.path.isfile(candidate):
                _LEVEL_FILE_CACHE[cache_key] = candidate
                return candidate

    if not prefer_repo_extract:
        resolved = try_repo_file()
        if resolved:
            return resolved

    _LEVEL_FILE_CACHE[cache_key] = ""
    return ""


def _collection_identity(collection):
    if collection is None:
        return None
    try:
        return int(collection.as_pointer())
    except Exception:
        return id(collection)


def _find_parent_collection(target_collection):
    if target_collection is None:
        return None
    target_id = _collection_identity(target_collection)
    for candidate in bpy.data.collections:
        try:
            for child in candidate.children:
                if _collection_identity(child) == target_id:
                    return candidate
        except Exception:
            continue
    return None


def _find_world_root_collection_for_collection(collection):
    current = collection
    visited = set()
    while current is not None:
        current_id = _collection_identity(current)
        if current_id in visited:
            break
        visited.add(current_id)
        if str(current.get("world_path", "")).strip():
            return current
        current = _find_parent_collection(current)
    return None


def _find_world_root_collection(context):
    candidate_objects = []
    active_object = getattr(context, "active_object", None)
    if active_object is not None:
        candidate_objects.append(active_object)
    for obj in getattr(context, "selected_objects", []):
        if obj is None or obj == active_object:
            continue
        candidate_objects.append(obj)

    for obj in candidate_objects:
        if not hasattr(obj, "get"):
            continue
        collection_name = str(obj.get("world_root_collection", "")).strip()
        if collection_name:
            collection = bpy.data.collections.get(collection_name)
            if collection is not None:
                return collection
        world_path = str(obj.get("world_path", "")).strip()
        if world_path:
            for collection in bpy.data.collections:
                if str(collection.get("world_path", "")).strip() == world_path:
                    return collection

    return _find_world_root_collection_for_collection(getattr(context, "collection", None))


def _iter_layer_info_collections(root_collection):
    if root_collection is None:
        return
    for child in root_collection.children:
        child_group_type = str(child.get("group_type", "")).strip()
        if child_group_type == "LayerInfo":
            yield child
        elif child_group_type == "LayerGroup":
            yield from _iter_layer_info_collections(child)


def _world_layer_cache_dir():
    cache_dir = os.path.join(get_cache_root(create=True), "world_layer_scan")
    os.makedirs(cache_dir, exist_ok=True)
    return cache_dir


def _world_layer_cache_key(context, root_collection):
    world_path = str(root_collection.get("world_path", "")).strip()
    world_id = world_path or f"collection:{root_collection.name}"
    primary_roots, secondary_roots, prefer_repo_extract = _level_search_roots(context, root_collection)
    roots_id = "\n".join(primary_roots + ["--"] + secondary_roots)
    payload = f"{_WORLD_LAYER_SCAN_CACHE_VERSION}\n{world_id}\n{int(prefer_repo_extract)}\n{roots_id}"
    return hashlib.sha1(payload.encode("utf-8", "ignore")).hexdigest()


def _world_layer_cache_path(context, root_collection):
    return os.path.join(_world_layer_cache_dir(), f"{_world_layer_cache_key(context, root_collection)}.sqlite3")


def _build_scan_resolve_config(context, root_collection):
    primary_roots, secondary_roots, prefer_repo_extract = _level_search_roots(context, root_collection)

    def _normalize_roots(roots):
        normalized = []
        seen = set()
        for root in roots or []:
            root_value = str(root or "").strip()
            if not root_value:
                continue
            normalized_root = os.path.normpath(root_value)
            norm_key = os.path.normcase(normalized_root)
            if norm_key in seen:
                continue
            seen.add(norm_key)
            normalized.append(normalized_root)
        return normalized

    return {
        "primary_roots": _normalize_roots(primary_roots),
        "secondary_roots": _normalize_roots(secondary_roots),
        "prefer_repo_extract": bool(prefer_repo_extract),
    }


def _resolve_level_dependency_for_scan(level_path, version, resolve_config):
    raw = str(level_path or "").strip()
    if not raw:
        return ""

    raw = raw.replace("/", os.sep).replace("\\", os.sep)
    if os.path.isabs(raw) and os.path.isfile(raw):
        return os.path.normpath(raw)

    variants = _level_rel_variants(raw)
    primary_roots = resolve_config.get("primary_roots", ()) or ()
    secondary_roots = resolve_config.get("secondary_roots", ()) or ()
    prefer_repo_extract = bool(resolve_config.get("prefer_repo_extract"))

    def try_roots(roots):
        for root in roots:
            for rel in variants:
                candidate = os.path.normpath(os.path.join(root, rel))
                if os.path.isfile(candidate):
                    return candidate
        return ""

    def try_repo_file():
        for rel in variants:
            try:
                candidate = repo_file(rel, version)
            except Exception:
                candidate = ""
            if candidate and os.path.isfile(candidate):
                return os.path.normpath(candidate)
        return ""

    resolved = try_roots(primary_roots)
    if resolved:
        return resolved
    if prefer_repo_extract:
        resolved = try_repo_file()
        if resolved:
            return resolved

    resolved = try_roots(secondary_roots)
    if resolved:
        return resolved
    if not prefer_repo_extract:
        resolved = try_repo_file()
        if resolved:
            return resolved

    return ""


def _open_world_layer_cache_db(cache_path):
    if not cache_path:
        return None
    try:
        conn = sqlite3.connect(cache_path, timeout=30.0)
    except Exception as exc:
        log.debug("Failed to open world layer cache database %s: %s", cache_path, exc)
        return None
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass
    for pragma in (
        "PRAGMA synchronous=NORMAL",
        "PRAGMA temp_store=MEMORY",
        "PRAGMA cache_size=-65536",
    ):
        try:
            conn.execute(pragma)
        except Exception:
            pass
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS layers (
            level_key TEXT PRIMARY KEY,
            level_path TEXT NOT NULL,
            resolved_path TEXT NOT NULL,
            file_mtime REAL NOT NULL,
            file_size INTEGER NOT NULL,
            has_bounds INTEGER NOT NULL,
            min_x REAL,
            min_y REAL,
            max_x REAL,
            max_y REAL,
            object_count INTEGER NOT NULL,
            has_manifest INTEGER NOT NULL,
            import_item_count INTEGER NOT NULL,
            items_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS item_spatial (
            level_key TEXT NOT NULL,
            item_id TEXT NOT NULL,
            world_x REAL NOT NULL,
            world_y REAL NOT NULL,
            world_z REAL NOT NULL,
            cell_x INTEGER NOT NULL,
            cell_y INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_item_spatial_cell
            ON item_spatial (cell_x, cell_y, level_key);
        CREATE INDEX IF NOT EXISTS idx_item_spatial_level
            ON item_spatial (level_key);
        """
    )
    return conn


def _close_world_layer_cache_db(conn, *, commit=False, rollback=False):
    if conn is None:
        return
    try:
        if rollback:
            conn.rollback()
        elif commit:
            conn.commit()
    except Exception:
        pass
    try:
        conn.close()
    except Exception:
        pass


def _reset_world_layer_cache_db(conn, world_path=""):
    if conn is None:
        return
    conn.execute("DELETE FROM meta")
    conn.execute("DELETE FROM layers")
    conn.execute("DELETE FROM item_spatial")
    conn.executemany(
        "INSERT INTO meta (key, value) VALUES (?, ?)",
        (
            ("version", str(int(_WORLD_LAYER_SCAN_CACHE_VERSION))),
            ("world_path", str(world_path or "").strip()),
        ),
    )


def _load_world_layer_cache_entry(conn, level_key, *, include_items=False):
    if conn is None or not level_key:
        return None
    row = conn.execute(
        """
        SELECT level_path, resolved_path, file_mtime, file_size,
               has_bounds, min_x, min_y, max_x, max_y,
               object_count, has_manifest, import_item_count, items_json
        FROM layers
        WHERE level_key = ?
        """,
        (level_key,),
    ).fetchone()
    if row is None:
        return None

    has_bounds = bool(int(row["has_bounds"] or 0))
    entry = {
        "level_path": str(row["level_path"] or ""),
        "resolved_path": str(row["resolved_path"] or ""),
        "file_mtime": float(row["file_mtime"] or 0.0),
        "file_size": int(row["file_size"] or 0),
        "has_bounds": has_bounds,
        "has_manifest": bool(int(row["has_manifest"] or 0)),
        "object_count": int(row["object_count"] or 0),
        "import_item_count": int(row["import_item_count"] or 0),
    }
    if has_bounds:
        entry.update({
            "min_x": float(row["min_x"] or 0.0),
            "min_y": float(row["min_y"] or 0.0),
            "max_x": float(row["max_x"] or 0.0),
            "max_y": float(row["max_y"] or 0.0),
        })
    if include_items:
        try:
            items = json.loads(str(row["items_json"] or "[]"))
        except Exception:
            items = []
        entry["items"] = items if isinstance(items, list) else []
    return entry


def _load_world_layer_cache_items(conn, level_key):
    if conn is None or not level_key:
        return []
    row = conn.execute(
        "SELECT items_json FROM layers WHERE level_key = ?",
        (level_key,),
    ).fetchone()
    if row is None:
        return []
    try:
        items = json.loads(str(row["items_json"] or "[]"))
    except Exception:
        items = []
    return items if isinstance(items, list) else []


def _store_world_layer_cache_entry(conn, level_key, entry):
    if conn is None or not level_key or not isinstance(entry, dict):
        return
    items = list(entry.get("items", []) or [])
    try:
        items_json = json.dumps(items, separators=(",", ":"))
    except Exception:
        items_json = "[]"
    has_bounds = bool(entry.get("has_bounds", False))
    conn.execute(
        """
        INSERT INTO layers (
            level_key, level_path, resolved_path, file_mtime, file_size,
            has_bounds, min_x, min_y, max_x, max_y,
            object_count, has_manifest, import_item_count, items_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(level_key) DO UPDATE SET
            level_path = excluded.level_path,
            resolved_path = excluded.resolved_path,
            file_mtime = excluded.file_mtime,
            file_size = excluded.file_size,
            has_bounds = excluded.has_bounds,
            min_x = excluded.min_x,
            min_y = excluded.min_y,
            max_x = excluded.max_x,
            max_y = excluded.max_y,
            object_count = excluded.object_count,
            has_manifest = excluded.has_manifest,
            import_item_count = excluded.import_item_count,
            items_json = excluded.items_json
        """,
        (
            level_key,
            str(entry.get("level_path", "") or ""),
            str(entry.get("resolved_path", "") or ""),
            float(entry.get("file_mtime", 0.0) or 0.0),
            int(entry.get("file_size", 0) or 0),
            1 if has_bounds else 0,
            float(entry.get("min_x", 0.0) or 0.0) if has_bounds else None,
            float(entry.get("min_y", 0.0) or 0.0) if has_bounds else None,
            float(entry.get("max_x", 0.0) or 0.0) if has_bounds else None,
            float(entry.get("max_y", 0.0) or 0.0) if has_bounds else None,
            int(entry.get("object_count", 0) or 0),
            1 if entry.get("has_manifest", False) else 0,
            int(entry.get("import_item_count", 0) or 0),
            items_json,
        ),
    )
    conn.execute("DELETE FROM item_spatial WHERE level_key = ?", (level_key,))
    spatial_rows = []
    for item in items:
        if not isinstance(item, dict) or not _manifest_countable_item(item):
            continue
        position = _manifest_item_position(item)
        if position is None:
            continue
        cell_x, cell_y = _spatial_cell_key(position[0], position[1], _WORLD_LAYER_SPATIAL_CELL_SIZE)
        spatial_rows.append(
            (
                level_key,
                str(item.get("id", "") or ""),
                float(position[0]),
                float(position[1]),
                float(position[2]),
                int(cell_x),
                int(cell_y),
            )
        )
    if spatial_rows:
        conn.executemany(
            """
            INSERT INTO item_spatial (
                level_key, item_id, world_x, world_y, world_z, cell_x, cell_y
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            spatial_rows,
        )


def _make_world_layer_index_entry(collection_name, level_path, level_key, cache_entry, *, include_items=False):
    entry = {
        "collection_name": collection_name,
        "level_key": level_key,
        "level_path": level_path,
        "resolved_path": str(cache_entry.get("resolved_path", "") or ""),
        "object_count": int(cache_entry.get("object_count", 0) or 0),
        "import_item_count": int(cache_entry.get("import_item_count", 0) or 0),
        "has_manifest": bool(cache_entry.get("has_manifest", False)),
    }
    if cache_entry.get("has_bounds", False):
        entry.update({
            "min_x": float(cache_entry["min_x"]),
            "min_y": float(cache_entry["min_y"]),
            "max_x": float(cache_entry["max_x"]),
            "max_y": float(cache_entry["max_y"]),
        })
    if include_items:
        entry["items"] = list(cache_entry.get("items", []) or [])
    return entry


def _manifest_countable_item(item):
    return str(item.get("kind", "") or "").strip().lower() not in {"group", "entity"}


def _manifest_item_matches_kind_filter(item, item_kind_filter=None):
    if not item_kind_filter:
        return True
    kind = str(item.get("kind", "") or "").strip().lower()
    return kind in item_kind_filter


def _manifest_item_position(item):
    position = item.get("world_position")
    if not isinstance(position, (list, tuple)) or len(position) < 2:
        return None
    try:
        x = float(position[0])
        y = float(position[1])
        z = float(position[2]) if len(position) > 2 else 0.0
        return (x, y, z)
    except Exception:
        return None


def _new_layer_scan_timing_totals():
    return {
        "layers": 0,
        "total_seconds": 0.0,
        "parse_seconds": 0.0,
        "bounds_seconds": 0.0,
        "manifest_seconds": 0.0,
        "create_level_seconds": 0.0,
        "resolve_plan_seconds": 0.0,
        "store_seconds": 0.0,
        "slowest_layer_name": "",
        "slowest_total_seconds": 0.0,
        "slowest_details": {},
    }


def _format_layer_scan_timing(value):
    try:
        return f"{float(value or 0.0):.3f}s"
    except Exception:
        return "0.000s"


def _log_layer_scan_timing_warning(message, *args):
    if not _LAYER_SCAN_TIMING_ENABLED:
        return
    log.info("[layer-scan-profile] " + str(message), *args)


def _log_layer_load_timing_warning(message, *args):
    if not _LAYER_LOAD_TIMING_ENABLED:
        return
    log.info("[layer-load-profile] " + str(message), *args)


def _spatial_cell_key(x, y, cell_size):
    return (int(math.floor(float(x) / cell_size)), int(math.floor(float(y) / cell_size)))


def _iter_spatial_cells_for_radius(x, y, radius, cell_size):
    min_x = float(x) - float(radius)
    max_x = float(x) + float(radius)
    min_y = float(y) - float(radius)
    max_y = float(y) + float(radius)
    min_cell_x = int(math.floor(min_x / cell_size))
    max_cell_x = int(math.floor(max_x / cell_size))
    min_cell_y = int(math.floor(min_y / cell_size))
    max_cell_y = int(math.floor(max_y / cell_size))
    for cell_x in range(min_cell_x, max_cell_x + 1):
        for cell_y in range(min_cell_y, max_cell_y + 1):
            yield (cell_x, cell_y)


def _world_layer_cache_skip_level_keys(index, skip_complete, camera_position=None, radius=None, mode_signature=None):
    if not skip_complete:
        return set()
    return set(_world_layer_complete_level_keys(index, camera_position=camera_position, radius=radius, mode_signature=mode_signature))


def _query_world_layer_cache_nearby_counts(index, camera_position, radius, skip_complete):
    cache_path = str(index.get("cache_path", "") or "").strip()
    if not cache_path or camera_position is None:
        return 0, 0
    radius_value = max(0.0, float(radius or 0.0))
    if radius_value <= 0.0:
        return 0, 0
    cell_size = float(_WORLD_LAYER_SPATIAL_CELL_SIZE)
    min_cell_x = int(math.floor((float(camera_position[0]) - radius_value) / cell_size))
    max_cell_x = int(math.floor((float(camera_position[0]) + radius_value) / cell_size))
    min_cell_y = int(math.floor((float(camera_position[1]) - radius_value) / cell_size))
    max_cell_y = int(math.floor((float(camera_position[1]) + radius_value) / cell_size))
    radius_sq = radius_value * radius_value
    skip_level_keys = sorted(_world_layer_cache_skip_level_keys(index, skip_complete))

    sql = (
        "SELECT COUNT(*) AS nearby_items, COUNT(DISTINCT level_key) AS nearby_layers "
        "FROM item_spatial "
        "WHERE cell_x BETWEEN ? AND ? "
        "AND cell_y BETWEEN ? AND ? "
        "AND ((world_x - ?) * (world_x - ?) + (world_y - ?) * (world_y - ?)) <= ?"
    )
    params = [
        min_cell_x,
        max_cell_x,
        min_cell_y,
        max_cell_y,
        float(camera_position[0]),
        float(camera_position[0]),
        float(camera_position[1]),
        float(camera_position[1]),
        radius_sq,
    ]
    if skip_level_keys:
        placeholders = ",".join("?" for _ in skip_level_keys)
        sql += f" AND level_key NOT IN ({placeholders})"
        params.extend(skip_level_keys)

    conn = _open_world_layer_cache_db(cache_path)
    if conn is None:
        return 0, 0
    try:
        row = conn.execute(sql, params).fetchone()
    finally:
        _close_world_layer_cache_db(conn)
    if row is None:
        return 0, 0
    return int(row["nearby_items"] or 0), int(row["nearby_layers"] or 0)


def _new_layer_load_timing_totals():
    return {
        "layers": 0,
        "total_seconds": 0.0,
        "plan_load_seconds": 0.0,
        "import_seconds": 0.0,
        "slowest_layer_name": "",
        "slowest_total_seconds": 0.0,
    }


def _world_layer_entry_map(index):
    if not isinstance(index, dict):
        return {}
    entry_map = index.get("_entry_by_level_key")
    if isinstance(entry_map, dict):
        return entry_map
    entry_map = {}
    for entry in index.get("entries", []):
        if not isinstance(entry, dict):
            continue
        level_key = str(entry.get("level_key", "") or "").strip()
        if level_key:
            entry_map[level_key] = entry
    index["_entry_by_level_key"] = entry_map
    return entry_map


def _collection_level_key(collection):
    if collection is None:
        return ""
    level_path = str(collection.get("level_path", "") or "").strip()
    if not level_path:
        return ""
    return _normalize_level_rel_path(level_path).lower()


def _world_layer_complete_level_keys(index, camera_position=None, radius=None, mode_signature=None):
    if not isinstance(index, dict):
        return set()
    use_cover_check = (
        camera_position is not None
        and radius is not None
        and float(radius or 0.0) > 0.0
    )
    if not use_cover_check:
        cached = index.get("_complete_level_keys")
        if isinstance(cached, set):
            return cached
    started = time.perf_counter()
    skip_level_keys = set()
    collection_total = 0
    complete_count = 0
    covered_count = 0
    for collection in bpy.data.collections:
        collection_total += 1
        if _collection_has_loaded_content(collection, mode_signature=mode_signature):
            level_key = _collection_level_key(collection)
            if level_key:
                skip_level_keys.add(level_key)
                complete_count += 1
            continue
        if use_cover_check and _layer_covered_by_previous_load(collection, camera_position, radius, mode_signature=mode_signature):
            level_key = _collection_level_key(collection)
            if level_key:
                skip_level_keys.add(level_key)
                covered_count += 1
    if not use_cover_check:
        index["_complete_level_keys"] = skip_level_keys
    _log_layer_load_timing_warning(
        "skip-state scan %s (collections %d, complete %d, covered %d)",
        _format_layer_scan_timing(time.perf_counter() - started),
        int(collection_total),
        int(complete_count),
        int(covered_count),
    )
    return skip_level_keys


def _update_world_layer_complete_state(index, entry=None, collection=None):
    if not isinstance(index, dict):
        return
    complete_level_keys = _world_layer_complete_level_keys(index)
    level_key = ""
    if collection is not None:
        level_key = _collection_level_key(collection)
    if not level_key and isinstance(entry, dict):
        level_key = str(entry.get("level_key", "") or "").strip().lower()
    if not level_key:
        return
    target_collection = collection
    if target_collection is None and isinstance(entry, dict):
        target_collection = bpy.data.collections.get(str(entry.get("collection_name", "") or ""))
    if _collection_has_loaded_content(target_collection):
        complete_level_keys.add(level_key)
    else:
        complete_level_keys.discard(level_key)


def _build_world_layer_runtime_index(index):
    if not isinstance(index, dict):
        return None
    cache_key = str(index.get("cache_key", "") or "").strip()
    if not cache_key:
        return None

    if str(index.get("cache_backend", "") or "").strip().lower() == "sqlite":
        runtime = {
            "cache_key": cache_key,
            "cell_size": float(_WORLD_LAYER_SPATIAL_CELL_SIZE),
            "cache_path": str(index.get("cache_path", "") or ""),
            "uses_db": True,
        }
        _WORLD_LAYER_RUNTIME_CACHE[cache_key] = runtime
        return runtime

    cell_size = float(_WORLD_LAYER_SPATIAL_CELL_SIZE)
    buckets = {}
    item_counts_by_collection = {}
    for entry in index.get("entries", []):
        collection_name = str(entry.get("collection_name", "") or "").strip()
        if not collection_name:
            continue
        countable_count = 0
        for item in entry.get("items", []) or []:
            if not isinstance(item, dict) or not _manifest_countable_item(item):
                continue
            position = _manifest_item_position(item)
            if position is None:
                continue
            countable_count += 1
            record = (collection_name, position[0], position[1], position[2])
            cell_key = _spatial_cell_key(position[0], position[1], cell_size)
            bucket = buckets.get(cell_key)
            if bucket is None:
                bucket = []
                buckets[cell_key] = bucket
            bucket.append(record)
        item_counts_by_collection[collection_name] = countable_count

    runtime = {
        "cache_key": cache_key,
        "cell_size": cell_size,
        "buckets": buckets,
        "item_counts_by_collection": item_counts_by_collection,
    }
    _WORLD_LAYER_RUNTIME_CACHE[cache_key] = runtime
    return runtime


def _get_world_layer_runtime_index(index):
    if not isinstance(index, dict):
        return None
    cache_key = str(index.get("cache_key", "") or "").strip()
    if not cache_key:
        return None
    runtime = _WORLD_LAYER_RUNTIME_CACHE.get(cache_key)
    if runtime is None:
        runtime = _build_world_layer_runtime_index(index)
    return runtime


def _count_nearby_manifest_items_for_entry(
    entry,
    camera_position,
    radius_sq,
    item_kind_filter=None,
    import_filter_kwargs=None,
    context=None,
):
    if camera_position is None:
        return 0, None
    count = 0
    nearest_sq = None
    items = list(entry.get("items", []) or [])
    if import_filter_kwargs is not None:
        items = import_blender_fun.cached_plan_filter_items_for_import_options(
            items,
            import_filter_kwargs or {},
            context=context,
        )
    for item in items:
        if not isinstance(item, dict) or not _manifest_countable_item(item):
            continue
        if not _manifest_item_matches_kind_filter(item, item_kind_filter):
            continue
        position = _manifest_item_position(item)
        if position is None:
            continue
        dx = position[0] - float(camera_position[0])
        dy = position[1] - float(camera_position[1])
        distance_sq = (dx * dx) + (dy * dy)
        if distance_sq > radius_sq:
            continue
        count += 1
        if nearest_sq is None or distance_sq < nearest_sq:
            nearest_sq = distance_sq
    return count, nearest_sq


def _query_world_layer_runtime_nearby(index, camera_position, radius, skip_complete):
    runtime = _get_world_layer_runtime_index(index)
    if runtime is None or camera_position is None:
        return 0, 0
    if runtime.get("uses_db"):
        return _query_world_layer_cache_nearby_counts(index, camera_position, radius, skip_complete)

    radius_value = max(0.0, float(radius or 0.0))
    if radius_value <= 0.0:
        return 0, 0

    skip_collection_names = set()
    if skip_complete:
        for entry in index.get("entries", []):
            collection = bpy.data.collections.get(entry.get("collection_name", ""))
            if collection is not None and _collection_has_loaded_content(collection):
                skip_collection_names.add(collection.name)

    nearby_layers = set()
    nearby_items = 0
    radius_sq = radius_value * radius_value
    buckets = runtime.get("buckets", {}) or {}
    cell_size = float(runtime.get("cell_size", _WORLD_LAYER_SPATIAL_CELL_SIZE) or _WORLD_LAYER_SPATIAL_CELL_SIZE)

    for cell_key in _iter_spatial_cells_for_radius(camera_position[0], camera_position[1], radius_value, cell_size):
        for collection_name, pos_x, pos_y, _pos_z in buckets.get(cell_key, ()):
            if collection_name in skip_collection_names:
                continue
            dx = float(pos_x) - float(camera_position[0])
            dy = float(pos_y) - float(camera_position[1])
            if (dx * dx + dy * dy) > radius_sq:
                continue
            nearby_items += 1
            nearby_layers.add(collection_name)

    return nearby_items, len(nearby_layers)


def _scan_logger_names():
    package_name = str(getattr(CR2W, "__name__", "") or "")
    if not package_name:
        return ()
    return (
        f"{package_name}.CR2W_types",
        f"{package_name}.CR2W_file",
    )


@contextmanager
def _suppress_world_layer_scan_logs():
    changes = []
    for logger_name in _scan_logger_names():
        logger_obj = logging.getLogger(logger_name)
        previous_level = logger_obj.level
        logger_obj.setLevel(logging.CRITICAL + 1)
        changes.append((logger_obj, previous_level))
    try:
        yield
    finally:
        for logger_obj, previous_level in changes:
            logger_obj.setLevel(previous_level)


class _OperatorProgress:
    def __init__(self, context, total, title="Progress"):
        self.context = context
        self.window_manager = getattr(context, "window_manager", None)
        self.workspace = getattr(context, "workspace", None)
        self.total = max(1, int(total or 1))
        self.title = str(title or "Progress").strip()
        self._last_log_time = 0.0
        self._last_redraw_time = 0.0
        self._closed = False

    def _tag_redraw(self):
        context = self.context
        try:
            if context and getattr(context, "region", None) and hasattr(context.region, "tag_redraw"):
                context.region.tag_redraw()
            if context and getattr(context, "area", None):
                context.area.tag_redraw()
                for region in getattr(context.area, "regions", []):
                    if hasattr(region, "tag_redraw"):
                        region.tag_redraw()
            if context and getattr(context, "screen", None):
                for area in context.screen.areas:
                    area.tag_redraw()
                    for region in getattr(area, "regions", []):
                        if hasattr(region, "tag_redraw"):
                            region.tag_redraw()
            if self.window_manager:
                for window in getattr(self.window_manager, "windows", []):
                    screen = getattr(window, "screen", None)
                    if not screen:
                        continue
                    for area in getattr(screen, "areas", []):
                        area.tag_redraw()
                        for region in getattr(area, "regions", []):
                            if hasattr(region, "tag_redraw"):
                                region.tag_redraw()
        except Exception:
            pass

    def __enter__(self):
        if self.window_manager:
            try:
                self.window_manager.progress_begin(0, self.total)
            except Exception:
                pass
        return self

    def update(self, value, message="", force=False):
        clamped = max(0, min(self.total, int(value or 0)))
        text = str(message or self.title)

        if self.window_manager:
            try:
                self.window_manager.progress_update(clamped)
            except Exception:
                pass
        if self.workspace:
            try:
                self.workspace.status_text_set(text)
            except Exception:
                pass

        now = time.monotonic()
        if force or clamped >= self.total or (now - self._last_log_time) >= 0.5:
            log.info("%s", text)
            self._last_log_time = now
        if force or clamped >= self.total or (now - self._last_redraw_time) >= 0.15:
            self._tag_redraw()
            self._last_redraw_time = now

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self.window_manager:
            try:
                self.window_manager.progress_end()
            except Exception:
                pass
        if self.workspace:
            try:
                self.workspace.status_text_set(None)
            except Exception:
                pass

    def __exit__(self, exc_type, exc, tb):
        self.close()


def _bounds_from_cr2w(cr2w_file):
    if cr2w_file is None:
        return None

    chunks = getattr(getattr(cr2w_file, "CHUNKS", None), "CHUNKS", None) or []
    sector_chunk = None
    for chunk in chunks:
        if getattr(chunk, "name", "") == "CSectorData":
            sector_chunk = chunk
            break

    min_x = float("inf")
    min_y = float("inf")
    max_x = float("-inf")
    max_y = float("-inf")
    object_count = 0

    for obj in (getattr(sector_chunk, "Objects", []) or []) if sector_chunk is not None else []:
        pos = getattr(obj, "position", None)
        if pos is None:
            continue
        x = float(getattr(pos, "x", 0.0) or 0.0)
        y = float(getattr(pos, "y", 0.0) or 0.0)
        radius = max(0.0, float(getattr(obj, "radius", 0.0) or 0.0))
        min_x = min(min_x, x - radius)
        min_y = min(min_y, y - radius)
        max_x = max(max_x, x + radius)
        max_y = max(max_y, y + radius)
        object_count += 1

    if object_count <= 0:
        for chunk in chunks:
            if not hasattr(chunk, "GetVariableByName"):
                continue
            try:
                transform_prop = chunk.GetVariableByName("transform")
            except Exception:
                transform_prop = None
            transform = getattr(transform_prop, "EngineTransform", None) if transform_prop else None
            if transform is None:
                continue

            x = float(getattr(transform, "X", 0.0) or 0.0)
            y = float(getattr(transform, "Y", 0.0) or 0.0)
            radius = 0.0

            try:
                streaming_distance = chunk.GetVariableByName("streamingDistance")
            except Exception:
                streaming_distance = None
            if streaming_distance is not None:
                try:
                    radius = max(0.0, float(getattr(streaming_distance, "Value", 0.0) or 0.0))
                except Exception:
                    radius = 0.0

            min_x = min(min_x, x - radius)
            min_y = min(min_y, y - radius)
            max_x = max(max_x, x + radius)
            max_y = max(max_y, y + radius)
            object_count += 1

    if object_count <= 0:
        return None

    return {
        "has_bounds": True,
        "min_x": float(min_x),
        "min_y": float(min_y),
        "max_x": float(max_x),
        "max_y": float(max_y),
        "object_count": int(object_count),
    }


def _parse_level_cr2w(resolved_path):
    """Thread-safe: parses a .w2l file's binary structure (no bpy access)."""
    try:
        with _suppress_world_layer_scan_logs():
            return CR2W.CR2W_file.read_CR2W(resolved_path)
    except Exception as exc:
        log.warning("Failed to parse layer file %s: %s", resolved_path, exc)
        return None


def _read_level_export_names_lightweight(resolved_path):
    """Read just the export metadata needed to classify a layer before full parsing."""
    path_value = str(resolved_path or "").strip()
    if not path_value:
        return None

    try:
        with open(path_value, "rb") as handle:
            start = handle.tell()
            header = CR2W.CR2W_types.CR2W_header(handle)
            version = int(getattr(header, "version", 0) or 0)
            updated_format = int(getattr(CR2W.CR2W_types, "UPDATED_RESOURCE_FORMAT_VERSION", 0) or 0)
            if version <= 115 or version < updated_format:
                return None

            tables = [CR2W.CR2W_types.CR2WTABLE(i, handle, version) for i in range(10)]
            if int(getattr(tables[1], "itemCount", 0) or 0) <= 0:
                return []

            proxy = SimpleNamespace(
                start=start,
                CR2WTable=tables,
                CNAMES=[],
            )

            handle.seek(tables[1].offset + start)
            for _ in range(tables[1].itemCount):
                proxy.CNAMES.append(CR2W.CR2W_types.NAME(handle, proxy))

            export_names = []
            if int(getattr(tables[4], "itemCount", 0) or 0) <= 0:
                return export_names

            handle.seek(tables[4].offset + start)
            for _ in range(tables[4].itemCount):
                export = CR2W.CR2W_types.CR2WExport(handle, proxy)
                export_names.append(str(getattr(export, "name", "") or ""))
            return export_names
    except Exception as exc:
        log.debug("Failed to read lightweight CR2W export summary for %s: %s", path_value, exc)
        return None


def _summarize_level_exports_for_fast_scan(export_names):
    names = [str(name or "").strip() for name in (export_names or []) if str(name or "").strip()]
    top_level_type = names[0] if names else ""
    name_set = set(names)
    has_entities = any(name in _FAST_SCAN_ENTITY_TYPES for name in name_set)
    has_sector_data = "CSectorData" in name_set
    has_foliage = "CFoliageResource" in name_set
    has_template_chunk = "CEntityTemplate" in name_set
    requires_full_parse = (
        top_level_type not in _FAST_SCAN_TOP_LEVEL_TYPES
        or has_entities
        or has_sector_data
        or has_foliage
        or has_template_chunk
    )
    return {
        "top_level_type": top_level_type,
        "export_count": len(names),
        "requires_full_parse": bool(requires_full_parse),
        "has_entities": bool(has_entities),
        "has_sector_data": bool(has_sector_data),
        "has_foliage": bool(has_foliage),
        "has_template_chunk": bool(has_template_chunk),
    }


def _build_fast_empty_layer_cache_entry(level_path, resolved_path, file_mtime, file_size):
    return {
        "level_path": level_path,
        "resolved_path": resolved_path,
        "file_mtime": file_mtime,
        "file_size": file_size,
        "has_bounds": False,
        "object_count": 0,
        "has_manifest": False,
        "import_item_count": 0,
        "items": [],
        "_fast_skip": True,
    }


def _new_layer_scan_dependency_cache():
    return {
        "levels": {},
        "inflight": {},
        "fast_levels": {},
        "fast_inflight": {},
        "lock": threading.RLock(),
        "thread_local": threading.local(),
        "fast_thread_local": threading.local(),
        "stats": {
            "hits": 0,
            "misses": 0,
            "stores": 0,
            "cycles": 0,
            "parse_seconds": 0.0,
            "create_level_seconds": 0.0,
            "total_seconds": 0.0,
            "slowest_path": "",
            "slowest_total_seconds": 0.0,
        },
        "fast_stats": {
            "hits": 0,
            "misses": 0,
            "stores": 0,
            "cycles": 0,
            "parse_seconds": 0.0,
            "create_level_seconds": 0.0,
            "total_seconds": 0.0,
            "slowest_path": "",
            "slowest_total_seconds": 0.0,
        },
    }


def _layer_scan_dependency_state(dependency_cache):
    thread_local = dependency_cache.get("thread_local")
    active = getattr(thread_local, "active", None)
    if active is None:
        active = set()
        thread_local.active = active
    return active


def _layer_scan_fast_dependency_state(dependency_cache):
    thread_local = dependency_cache.get("fast_thread_local")
    active = getattr(thread_local, "active", None)
    if active is None:
        active = set()
        thread_local.active = active
    return active


def _load_layer_scan_fast_dependency(resolved_path, dependency_cache, resolve_config=None):
    path_value = str(resolved_path or "").strip()
    if not path_value:
        return None
    cache_key = os.path.normcase(os.path.abspath(path_value))
    levels = dependency_cache["fast_levels"]
    inflight = dependency_cache["fast_inflight"]
    stats = dependency_cache["fast_stats"]
    lock = dependency_cache["lock"]
    active = _layer_scan_fast_dependency_state(dependency_cache)

    if cache_key in active:
        with lock:
            stats["cycles"] += 1
        log.warning("Detected recursive fast layer-template dependency while scanning %s", resolved_path)
        return None

    owner = False
    wait_event = None
    while True:
        with lock:
            cached = levels.get(cache_key)
            if cached is not None:
                stats["hits"] += 1
                return cached
            wait_event = inflight.get(cache_key)
            if wait_event is None:
                wait_event = threading.Event()
                inflight[cache_key] = wait_event
                stats["misses"] += 1
                owner = True
                break
        wait_event.wait()

    dependency_result = None
    active.add(cache_key)
    dep_started = time.perf_counter()
    parse_seconds = 0.0
    try:
        dependency_resolver = None
        if resolve_config is not None:
            dependency_resolver = (
                lambda depot_path, version=999: _resolve_level_dependency_for_scan(
                    depot_path,
                    version,
                    resolve_config,
                )
            )
        parse_started = time.perf_counter()
        dependency_result = fast_cache_scan.scan_dependency_file(
            resolved_path,
            dependency_resolver=dependency_resolver,
            dependency_loader=lambda dep_path: _load_layer_scan_fast_dependency(
                dep_path,
                dependency_cache,
                resolve_config=resolve_config,
            ),
        )
        parse_seconds = time.perf_counter() - parse_started
        return dependency_result
    finally:
        dep_total_seconds = time.perf_counter() - dep_started
        active.discard(cache_key)
        if owner and wait_event is not None:
            with lock:
                stats["parse_seconds"] = float(stats.get("parse_seconds", 0.0) or 0.0) + parse_seconds
                stats["total_seconds"] = float(stats.get("total_seconds", 0.0) or 0.0) + dep_total_seconds
                if dep_total_seconds > float(stats.get("slowest_total_seconds", 0.0) or 0.0):
                    stats["slowest_total_seconds"] = dep_total_seconds
                    stats["slowest_path"] = path_value
                if dependency_result is not None:
                    levels[cache_key] = dependency_result
                    stats["stores"] += 1
                inflight.pop(cache_key, None)
            wait_event.set()
            if dep_total_seconds >= _LAYER_SCAN_DEP_WARN_THRESHOLD:
                _log_layer_scan_timing_warning(
                    "dependency %s total %s (parse %s, create_level %s)",
                    os.path.basename(path_value) or path_value,
                    _format_layer_scan_timing(dep_total_seconds),
                    _format_layer_scan_timing(parse_seconds),
                    _format_layer_scan_timing(0.0),
                )


def _combined_layer_scan_dependency_stats(dependency_cache):
    if not dependency_cache:
        return {}

    lock = dependency_cache.get("lock")
    if lock is not None:
        with lock:
            slow_stats = dict(dependency_cache.get("stats", {}) or {})
            fast_stats = dict(dependency_cache.get("fast_stats", {}) or {})
    else:
        slow_stats = dict(dependency_cache.get("stats", {}) or {})
        fast_stats = dict(dependency_cache.get("fast_stats", {}) or {})

    combined = {
        "hits": int(slow_stats.get("hits", 0) or 0) + int(fast_stats.get("hits", 0) or 0),
        "misses": int(slow_stats.get("misses", 0) or 0) + int(fast_stats.get("misses", 0) or 0),
        "stores": int(slow_stats.get("stores", 0) or 0) + int(fast_stats.get("stores", 0) or 0),
        "cycles": int(slow_stats.get("cycles", 0) or 0) + int(fast_stats.get("cycles", 0) or 0),
        "parse_seconds": float(slow_stats.get("parse_seconds", 0.0) or 0.0)
        + float(fast_stats.get("parse_seconds", 0.0) or 0.0),
        "create_level_seconds": float(slow_stats.get("create_level_seconds", 0.0) or 0.0)
        + float(fast_stats.get("create_level_seconds", 0.0) or 0.0),
        "total_seconds": float(slow_stats.get("total_seconds", 0.0) or 0.0)
        + float(fast_stats.get("total_seconds", 0.0) or 0.0),
        "slowest_path": str(slow_stats.get("slowest_path", "") or ""),
        "slowest_total_seconds": float(slow_stats.get("slowest_total_seconds", 0.0) or 0.0),
    }
    if float(fast_stats.get("slowest_total_seconds", 0.0) or 0.0) > combined["slowest_total_seconds"]:
        combined["slowest_total_seconds"] = float(fast_stats.get("slowest_total_seconds", 0.0) or 0.0)
        combined["slowest_path"] = str(fast_stats.get("slowest_path", "") or "")
    return combined


def _load_layer_scan_dependency(resolved_path, dependency_cache, resolve_config=None):
    path_value = str(resolved_path or "").strip()
    if not path_value:
        return None
    cache_key = os.path.normcase(os.path.abspath(path_value))
    levels = dependency_cache["levels"]
    inflight = dependency_cache["inflight"]
    stats = dependency_cache["stats"]
    lock = dependency_cache["lock"]
    active = _layer_scan_dependency_state(dependency_cache)

    if cache_key in active:
        with lock:
            stats["cycles"] += 1
        log.warning("Detected recursive layer-template dependency while scanning %s", resolved_path)
        return None

    owner = False
    wait_event = None
    while True:
        with lock:
            cached = levels.get(cache_key)
            if cached is not None:
                stats["hits"] += 1
                return cached
            wait_event = inflight.get(cache_key)
            if wait_event is None:
                wait_event = threading.Event()
                inflight[cache_key] = wait_event
                stats["misses"] += 1
                owner = True
                break
        wait_event.wait()

    level_file = None
    active.add(cache_key)
    dep_started = time.perf_counter()
    parse_seconds = 0.0
    create_level_seconds = 0.0
    try:
        parse_started = time.perf_counter()
        cr2w_file = _parse_level_cr2w(resolved_path)
        parse_seconds = time.perf_counter() - parse_started
        if cr2w_file is None:
            return None
        dependency_resolver = None
        if resolve_config is not None:
            dependency_resolver = (
                lambda depot_path, version=999: _resolve_level_dependency_for_scan(
                    depot_path,
                    version,
                    resolve_config,
                )
            )
        create_started = time.perf_counter()
        level_file = CR2W.CR2W_file.create_level(
            cr2w_file,
            resolved_path,
            dependency_loader=lambda dep_path: _load_layer_scan_dependency(
                dep_path,
                dependency_cache,
                resolve_config=resolve_config,
            ),
            dependency_resolver=dependency_resolver,
        )
        create_level_seconds = time.perf_counter() - create_started
        return level_file
    finally:
        dep_total_seconds = time.perf_counter() - dep_started
        active.discard(cache_key)
        if owner and wait_event is not None:
            with lock:
                stats["parse_seconds"] = float(stats.get("parse_seconds", 0.0) or 0.0) + parse_seconds
                stats["create_level_seconds"] = float(stats.get("create_level_seconds", 0.0) or 0.0) + create_level_seconds
                stats["total_seconds"] = float(stats.get("total_seconds", 0.0) or 0.0) + dep_total_seconds
                if dep_total_seconds > float(stats.get("slowest_total_seconds", 0.0) or 0.0):
                    stats["slowest_total_seconds"] = dep_total_seconds
                    stats["slowest_path"] = path_value
                if level_file is not None:
                    levels[cache_key] = level_file
                    stats["stores"] += 1
                inflight.pop(cache_key, None)
            wait_event.set()
            if dep_total_seconds >= _LAYER_SCAN_DEP_WARN_THRESHOLD:
                _log_layer_scan_timing_warning(
                    "dependency %s total %s (parse %s, create_level %s)",
                    os.path.basename(path_value) or path_value,
                    _format_layer_scan_timing(dep_total_seconds),
                    _format_layer_scan_timing(parse_seconds),
                    _format_layer_scan_timing(create_level_seconds),
                )


def _sync_layer_scan_dependency_cache_stats(scan):
    dependency_cache = scan.get("dependency_cache")
    if not dependency_cache:
        return
    cache_stats = _combined_layer_scan_dependency_stats(dependency_cache)
    stats = scan.get("stats", {}) or {}
    stats["template_cache_hits"] = int(cache_stats.get("hits", 0) or 0)
    stats["template_cache_misses"] = int(cache_stats.get("misses", 0) or 0)
    stats["template_cache_stores"] = int(cache_stats.get("stores", 0) or 0)
    stats["template_cycles"] = int(cache_stats.get("cycles", 0) or 0)
    stats["template_parse_seconds"] = float(cache_stats.get("parse_seconds", 0.0) or 0.0)
    stats["template_create_level_seconds"] = float(cache_stats.get("create_level_seconds", 0.0) or 0.0)
    stats["template_total_seconds"] = float(cache_stats.get("total_seconds", 0.0) or 0.0)


def _build_level_and_manifest(
    cr2w_file,
    resolved_path,
    dependency_cache=None,
    resolve_config=None,
    mesh_fbx_uncook_path=None,
    mesh_uncook_path=None,
    timing_info=None,
):
    if cr2w_file is None:
        return {"has_manifest": False, "import_item_count": 0, "items": []}
    manifest_started = time.perf_counter()
    create_level_seconds = 0.0
    resolve_plan_seconds = 0.0
    try:
        dependency_resolver = None
        if resolve_config is not None:
            dependency_resolver = (
                lambda depot_path, version=999: _resolve_level_dependency_for_scan(
                    depot_path,
                    version,
                    resolve_config,
                )
            )
        create_started = time.perf_counter()
        with _suppress_world_layer_scan_logs():
            if dependency_cache is None:
                level_file = CR2W.CR2W_file.create_level(
                    cr2w_file,
                    resolved_path,
                    dependency_resolver=dependency_resolver,
                )
            else:
                level_file = CR2W.CR2W_file.create_level(
                    cr2w_file,
                    resolved_path,
                    dependency_loader=lambda dep_path: _load_layer_scan_dependency(
                        dep_path,
                        dependency_cache,
                        resolve_config=resolve_config,
                    ),
                    dependency_resolver=dependency_resolver,
                )
        create_level_seconds = time.perf_counter() - create_started
        resolve_started = time.perf_counter()
        plan = import_blender_fun.resolve_level_import_plan(
            level_file,
            _mesh_fbx_uncook_path=mesh_fbx_uncook_path,
            _mesh_uncook_path=mesh_uncook_path,
        )
        resolve_plan_seconds = time.perf_counter() - resolve_started
    except Exception as exc:
        log.warning("Failed to resolve layer manifest for %s: %s", resolved_path, exc)
        return {"has_manifest": False, "import_item_count": 0, "items": []}
    finally:
        if timing_info is not None:
            timing_info["create_level_seconds"] = create_level_seconds
            timing_info["resolve_plan_seconds"] = resolve_plan_seconds
            timing_info["manifest_seconds"] = time.perf_counter() - manifest_started

    items = list(plan.get("items", []) or [])
    import_item_count = 0
    for item in items:
        if isinstance(item, dict) and _manifest_countable_item(item) and _manifest_item_position(item) is not None:
            import_item_count += 1
    return {
        "has_manifest": True,
        "import_item_count": int(import_item_count),
        "items": items,
    }


def _scan_level_bounds(resolved_path):
    return _bounds_from_cr2w(_parse_level_cr2w(resolved_path))


def _scan_level_manifest(resolved_path):
    return _build_level_and_manifest(_parse_level_cr2w(resolved_path), resolved_path)


def _scan_level_cache_entry(
    level_path,
    resolved_path,
    file_mtime,
    file_size,
    *,
    dependency_cache=None,
    resolve_config=None,
    mesh_fbx_uncook_path=None,
    mesh_uncook_path=None,
):
    layer_started = time.perf_counter()
    parse_started = time.perf_counter()
    export_summary = _summarize_level_exports_for_fast_scan(
        _read_level_export_names_lightweight(resolved_path)
    )
    if export_summary is not None and not export_summary.get("requires_full_parse", True):
        parse_seconds = time.perf_counter() - parse_started
        entry = _build_fast_empty_layer_cache_entry(
            level_path,
            resolved_path,
            file_mtime,
            file_size,
        )
        entry["_timing"] = {
            "parse_seconds": parse_seconds,
            "bounds_seconds": 0.0,
            "manifest_seconds": 0.0,
            "create_level_seconds": 0.0,
            "resolve_plan_seconds": 0.0,
            "total_seconds": time.perf_counter() - layer_started,
        }
        return entry

    dependency_resolver = None
    if resolve_config is not None:
        dependency_resolver = (
            lambda depot_path, version=999: _resolve_level_dependency_for_scan(
                depot_path,
                version,
                resolve_config,
            )
        )
    fast_entry = fast_cache_scan.scan_cache_entry(
        level_path,
        resolved_path,
        file_mtime,
        file_size,
        dependency_resolver=dependency_resolver,
        dependency_loader=(
            (lambda dep_path: _load_layer_scan_fast_dependency(dep_path, dependency_cache, resolve_config=resolve_config))
            if dependency_cache is not None
            else None
        ),
    )
    if fast_entry is not None:
        parse_seconds = time.perf_counter() - parse_started
        fast_entry["_timing"] = {
            "parse_seconds": parse_seconds,
            "bounds_seconds": 0.0,
            "manifest_seconds": 0.0,
            "create_level_seconds": 0.0,
            "resolve_plan_seconds": 0.0,
            "total_seconds": time.perf_counter() - layer_started,
        }
        fast_entry["_fast_scan"] = True
        return fast_entry

    cr2w_file = _parse_level_cr2w(resolved_path)
    parse_seconds = time.perf_counter() - parse_started
    bounds_started = time.perf_counter()
    bounds = _bounds_from_cr2w(cr2w_file)
    bounds_seconds = time.perf_counter() - bounds_started
    timing_info = {}
    manifest = _build_level_and_manifest(
        cr2w_file,
        resolved_path,
        dependency_cache=dependency_cache,
        resolve_config=resolve_config,
        mesh_fbx_uncook_path=mesh_fbx_uncook_path,
        mesh_uncook_path=mesh_uncook_path,
        timing_info=timing_info,
    )
    entry = {
        "level_path": level_path,
        "resolved_path": resolved_path,
        "file_mtime": file_mtime,
        "file_size": file_size,
    }
    if bounds is None:
        entry["has_bounds"] = False
    else:
        entry.update(bounds)
    entry.update(manifest)
    entry["_timing"] = {
        "parse_seconds": parse_seconds,
        "bounds_seconds": bounds_seconds,
        "manifest_seconds": float(timing_info.get("manifest_seconds", 0.0) or 0.0),
        "create_level_seconds": float(timing_info.get("create_level_seconds", 0.0) or 0.0),
        "resolve_plan_seconds": float(timing_info.get("resolve_plan_seconds", 0.0) or 0.0),
        "total_seconds": time.perf_counter() - layer_started,
    }
    return entry


def _get_world_layer_index(context, root_collection, rebuild=False, show_progress=False, progress_title="Layer Scan"):
    cache_key = _world_layer_cache_key(context, root_collection)
    if not rebuild:
        cached_index = _WORLD_LAYER_INDEX_CACHE.get(cache_key)
        if cached_index is not None:
            return cached_index

    cache_path = _world_layer_cache_path(context, root_collection)
    layer_collections = [
        collection
        for collection in _iter_layer_info_collections(root_collection)
        if str(collection.get("level_path", "")).strip()
    ]

    entries = []
    dependency_cache = _new_layer_scan_dependency_cache()
    resolve_config = _build_scan_resolve_config(context, root_collection)
    try:
        mesh_fbx_uncook_path = get_fbx_uncook_path(context)
    except Exception:
        mesh_fbx_uncook_path = ""
    try:
        mesh_uncook_path = get_uncook_path(context)
    except Exception:
        mesh_uncook_path = ""
    stats = {
        "layers_total": len(layer_collections),
        "indexed": 0,
        "cache_hits": 0,
        "scanned": 0,
        "missing": 0,
        "no_bounds": 0,
        "fast_skipped": 0,
    }
    progress_context = (
        _OperatorProgress(context, len(layer_collections), progress_title)
        if show_progress and layer_collections
        else nullcontext()
    )
    conn = _open_world_layer_cache_db(cache_path)
    if conn is None:
        return None
    try:
        if rebuild:
            _reset_world_layer_cache_db(conn, str(root_collection.get("world_path", "")).strip())

        with progress_context as progress:
            for layer_index, collection in enumerate(layer_collections, start=1):
                level_path = str(collection.get("level_path", "")).strip()
                level_name = Path(level_path).name or collection.name
                if progress:
                    progress.update(
                        layer_index - 1,
                        (
                            f"{progress_title} {layer_index}/{stats['layers_total']}: {level_name} "
                            f"(indexed {stats['indexed']}, scanned {stats['scanned']}, cached {stats['cache_hits']})"
                        ),
                    )

                level_key = _normalize_level_rel_path(level_path).lower()
                resolved_path = _resolve_level_file(context, level_path, root_collection)
                if not resolved_path or not os.path.isfile(resolved_path):
                    stats["missing"] += 1
                    continue

                try:
                    file_mtime = float(os.path.getmtime(resolved_path))
                    file_size = int(os.path.getsize(resolved_path))
                except OSError:
                    stats["missing"] += 1
                    continue

                cache_entry = None if rebuild else _load_world_layer_cache_entry(conn, level_key, include_items=False)
                if (
                    cache_entry
                    and cache_entry.get("resolved_path", "") == resolved_path
                    and float(cache_entry.get("file_mtime", -1.0)) == file_mtime
                    and int(cache_entry.get("file_size", -1)) == file_size
                ):
                    stats["cache_hits"] += 1
                    updated_entry = dict(cache_entry)
                else:
                    stats["scanned"] += 1
                    updated_entry = _scan_level_cache_entry(
                        level_path,
                        resolved_path,
                        file_mtime,
                        file_size,
                        dependency_cache=dependency_cache,
                        resolve_config=resolve_config,
                        mesh_fbx_uncook_path=mesh_fbx_uncook_path,
                        mesh_uncook_path=mesh_uncook_path,
                    )
                    _store_world_layer_cache_entry(conn, level_key, updated_entry)

                if not updated_entry.get("has_bounds", False):
                    stats["no_bounds"] += 1
                    continue

                entries.append(
                    _make_world_layer_index_entry(
                        collection.name,
                        level_path,
                        level_key,
                        updated_entry,
                    )
                )
                stats["indexed"] = len(entries)

            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                ("version", str(int(_WORLD_LAYER_SCAN_CACHE_VERSION))),
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                ("world_path", str(root_collection.get("world_path", "")).strip()),
            )
            stats["indexed"] = len(entries)
            _close_world_layer_cache_db(conn, commit=True)
            conn = None

            index = {
                "cache_key": cache_key,
                "cache_path": cache_path,
                "cache_backend": "sqlite",
                "entries": entries,
                "stats": stats,
            }
            _WORLD_LAYER_INDEX_CACHE[cache_key] = index
            _build_world_layer_runtime_index(index)

            if progress:
                progress.update(
                    stats["layers_total"],
                    (
                        f"{progress_title} complete: indexed {stats['indexed']}/{stats['layers_total']} "
                        f"(scanned {stats['scanned']}, cached {stats['cache_hits']}, missing {stats['missing']}, "
                        f"no bounds {stats['no_bounds']})"
                    ),
                    force=True,
                )

            return index
    finally:
        if conn is not None:
            _close_world_layer_cache_db(conn, rollback=True)


def _hydrate_world_layer_index_from_disk(context, root_collection):
    """Build the in-memory world-layer index directly from the on-disk cache file.

    Returns the hydrated index when every current layer collection is covered
    by a valid persisted entry; returns None otherwise so the caller can fall
    back to a modal scan.
    """
    cache_key = _world_layer_cache_key(context, root_collection)
    cache_path = _world_layer_cache_path(context, root_collection)
    if not os.path.isfile(cache_path):
        return None
    conn = _open_world_layer_cache_db(cache_path)
    if conn is None:
        return None

    started = time.perf_counter()
    query_seconds = 0.0
    try:
        entries = []
        stats = {
            "layers_total": 0,
            "indexed": 0,
            "cache_hits": 0,
            "scanned": 0,
            "fast_scanned": 0,
            "missing": 0,
            "no_bounds": 0,
            "fast_skipped": 0,
        }
        for collection in _iter_layer_info_collections(root_collection):
            level_path = str(collection.get("level_path", "")).strip()
            if not level_path:
                continue
            stats["layers_total"] += 1
            level_key = _normalize_level_rel_path(level_path).lower()
            query_started = time.perf_counter()
            cache_entry = _load_world_layer_cache_entry(conn, level_key, include_items=False)
            query_seconds += time.perf_counter() - query_started
            if not cache_entry:
                _log_layer_load_timing_warning(
                    "hydrate index aborted after %d/%d layers for %s (query %s)",
                    int(stats.get("cache_hits", 0) or 0),
                    int(stats.get("layers_total", 0) or 0),
                    level_path or collection.name,
                    _format_layer_scan_timing(query_seconds),
                )
                return None
            if not cache_entry.get("has_bounds", False):
                stats["no_bounds"] += 1
                stats["cache_hits"] += 1
                continue
            entries.append(
                _make_world_layer_index_entry(
                    collection.name,
                    level_path,
                    level_key,
                    cache_entry,
                )
            )
            stats["cache_hits"] += 1
        stats["indexed"] = len(entries)
    finally:
        _close_world_layer_cache_db(conn)

    total_seconds = time.perf_counter() - started
    index = {
        "cache_key": cache_key,
        "cache_path": cache_path,
        "cache_backend": "sqlite",
        "entries": entries,
        "stats": stats,
    }
    _WORLD_LAYER_INDEX_CACHE[cache_key] = index
    _build_world_layer_runtime_index(index)
    _log_layer_load_timing_warning(
        "hydrate index total %s (query %s, layers %d, indexed %d, no bounds %d)",
        _format_layer_scan_timing(total_seconds),
        _format_layer_scan_timing(query_seconds),
        int(stats.get("layers_total", 0) or 0),
        int(stats.get("indexed", 0) or 0),
        int(stats.get("no_bounds", 0) or 0),
    )
    return index


def _clear_world_layer_index_cache(context, root_collection):
    cache_key = _world_layer_cache_key(context, root_collection)
    _WORLD_LAYER_INDEX_CACHE.pop(cache_key, None)
    _WORLD_LAYER_RUNTIME_CACHE.pop(cache_key, None)
    cache_path = _world_layer_cache_path(context, root_collection)
    for path_value in (cache_path, f"{cache_path}-wal", f"{cache_path}-shm"):
        if os.path.isfile(path_value):
            try:
                os.remove(path_value)
            except OSError:
                pass


def _reset_layer_stream_job():
    profile_handler = _LAYER_STREAM_JOB.get("profile_log_handler")
    if profile_handler is not None:
        try:
            _map_import_profile_logger().removeHandler(profile_handler)
        except Exception:
            pass
        try:
            profile_handler.close()
        except Exception:
            pass
        _restore_map_import_profile_loggers(_LAYER_STREAM_JOB)
    load = _LAYER_STREAM_JOB.get("load", {}) or {}
    stack = load.get("batch_isolation_stack")
    if stack is not None:
        try:
            stack.close()
        except Exception:
            pass
    scan = _LAYER_STREAM_JOB.get("scan", {}) or {}
    _shutdown_scan_executor(scan, cancel_pending=True)
    cache_conn = scan.get("cache_conn")
    if cache_conn is not None:
        _close_world_layer_cache_db(cache_conn, rollback=True)
    _LAYER_STREAM_JOB.clear()
    _LAYER_STREAM_JOB.update(_new_layer_stream_job_state())


def layer_stream_job_running() -> bool:
    return bool(_LAYER_STREAM_JOB.get("running"))


def _tag_layer_stream_redraw(context=None, wm=None):
    context = context or bpy.context
    wm = wm or getattr(context, "window_manager", None) or getattr(bpy.context, "window_manager", None)
    try:
        if context and getattr(context, "region", None) and hasattr(context.region, "tag_redraw"):
            context.region.tag_redraw()
        if context and getattr(context, "area", None):
            context.area.tag_redraw()
            for region in getattr(context.area, "regions", []):
                if hasattr(region, "tag_redraw"):
                    region.tag_redraw()
        if context and getattr(context, "screen", None):
            for area in context.screen.areas:
                area.tag_redraw()
                for region in getattr(area, "regions", []):
                    if hasattr(region, "tag_redraw"):
                        region.tag_redraw()
        if wm:
            for window in getattr(wm, "windows", []):
                screen = getattr(window, "screen", None)
                if not screen:
                    continue
                for area in getattr(screen, "areas", []):
                    area.tag_redraw()
                    for region in getattr(area, "regions", []):
                        if hasattr(region, "tag_redraw"):
                            region.tag_redraw()
    except Exception:
        pass


def _maybe_tag_layer_stream_redraw(context=None, wm=None, force=False):
    """Throttled wrapper around _tag_layer_stream_redraw.

    Modal handlers fire many times per second; tagging every Blender area for
    redraw on each tick is the dominant UI cost during long imports. This
    coalesces redraw requests to roughly _LAYER_STREAM_REDRAW_INTERVAL.
    """
    global _layer_stream_last_redraw_ts
    now = time.monotonic()
    if not force and (now - _layer_stream_last_redraw_ts) < _LAYER_STREAM_REDRAW_INTERVAL:
        return
    _layer_stream_last_redraw_ts = now
    _tag_layer_stream_redraw(context=context, wm=wm)


def _set_layer_stream_progress(job, title, current, total, detail=""):
    shown_total = max(0, int(total or 0))
    job["title"] = str(title or "").strip()
    job["current"] = max(0, int(current or 0))
    job["total"] = shown_total
    job["detail"] = str(detail or "")


def draw_layer_stream_job_ui(layout, context) -> None:
    job = _LAYER_STREAM_JOB
    if not job.get("running"):
        return

    shown_total = max(0, int(job.get("total", 0) or 0))
    current = max(0, min(int(job.get("current", 0) or 0), shown_total if shown_total > 0 else int(job.get("current", 0) or 0)))
    pct = 100.0 if shown_total <= 0 else (current / shown_total) * 100.0

    box = layout.box()
    header = box.row(align=True)
    header.label(
        text=f"{job.get('title', 'Working')}  {current:,} / {shown_total:,}  ({pct:.1f}%)",
        icon='TIME',
    )
    header.operator("witcher.cancel_layer_stream_job", text="Cancel", icon='CANCEL')

    detail = str(job.get("detail", "") or "").strip()
    if detail:
        box.label(text=detail)

    if job.get("phase") == "scan":
        stats = (job.get("scan", {}) or {}).get("stats", {}) or {}
        box.label(
            text=(
                f"Indexed: {int(stats.get('indexed', 0)):,}   "
                f"Scanned: {int(stats.get('scanned', 0)):,}   "
                f"Cached: {int(stats.get('cache_hits', 0)):,}   "
                f"Missing: {int(stats.get('missing', 0)):,}   "
                f"No Bounds: {int(stats.get('no_bounds', 0)):,}"
            )
        )
    elif job.get("phase") == "load":
        load = job.get("load", {}) or {}
        box.label(
            text=(
                f"Imported: {int(load.get('imported', 0)):,}   "
                f"Failed: {int(load.get('failed', 0)):,}   "
                f"Skipped Complete: {int(load.get('skipped_complete', 0)):,}"
            )
        )


def _start_layer_stream_job(context, mode, root_collection):
    _reset_layer_stream_job()
    job = _LAYER_STREAM_JOB
    job["running"] = True
    job["mode"] = str(mode or "").strip()
    job["context"] = context
    job["wm"] = getattr(context, "window_manager", None)
    job["root_collection_name"] = str(getattr(root_collection, "name", "") or "")
    return job


def _start_layer_scan_phase(job, context, root_collection, rebuild=False, title="Scanning world layers"):
    cache_key = _world_layer_cache_key(context, root_collection)
    cache_path = _world_layer_cache_path(context, root_collection)
    resolve_config = _build_scan_resolve_config(context, root_collection)
    try:
        mesh_fbx_uncook_path = get_fbx_uncook_path(context)
    except Exception:
        mesh_fbx_uncook_path = ""
    try:
        mesh_uncook_path = get_uncook_path(context)
    except Exception:
        mesh_uncook_path = ""
    cache_conn = _open_world_layer_cache_db(cache_path)
    if cache_conn is None:
        raise RuntimeError("Could not open world layer cache database")
    if rebuild:
        _reset_world_layer_cache_db(cache_conn, str(root_collection.get("world_path", "")).strip())

    work_items = []
    cache_hit_count = 0
    parse_count = 0
    missing_count = 0

    for collection in _iter_layer_info_collections(root_collection):
        level_path = str(collection.get("level_path", "")).strip()
        if not level_path:
            continue
        level_name = Path(level_path).name or collection.name
        item = {
            "collection_name": collection.name,
            "level_path": level_path,
            "level_name": level_name,
            "level_key": _normalize_level_rel_path(level_path).lower(),
            "resolved_path": "",
            "file_mtime": -1.0,
            "file_size": -1,
            "cached_entry": None,
            "needs_parse": False,
            "missing": False,
        }
        resolved_path = _resolve_level_file(context, level_path, root_collection)
        if not resolved_path or not os.path.isfile(resolved_path):
            item["missing"] = True
            missing_count += 1
            work_items.append(item)
            continue
        try:
            item["file_mtime"] = float(os.path.getmtime(resolved_path))
            item["file_size"] = int(os.path.getsize(resolved_path))
        except OSError:
            item["missing"] = True
            missing_count += 1
            work_items.append(item)
            continue
        item["resolved_path"] = resolved_path

        cache_entry = None if rebuild else _load_world_layer_cache_entry(cache_conn, item["level_key"], include_items=False)
        if (
            cache_entry
            and cache_entry.get("resolved_path", "") == resolved_path
            and float(cache_entry.get("file_mtime", -1.0)) == item["file_mtime"]
            and int(cache_entry.get("file_size", -1)) == item["file_size"]
        ):
            item["cached_entry"] = cache_entry
            cache_hit_count += 1
        else:
            item["needs_parse"] = True
            parse_count += 1
        work_items.append(item)

    total = len(work_items)
    job["phase"] = "scan"
    job["scan"] = {
        "cache_key": cache_key,
        "cache_path": cache_path,
        "world_path": str(root_collection.get("world_path", "")).strip(),
        "work_items": work_items,
        "queue_index": 0,
        "completed_index": 0,
        "total": total,
        "entries": [],
        "cache_conn": cache_conn,
        "dependency_cache": _new_layer_scan_dependency_cache(),
        "timing_totals": _new_layer_scan_timing_totals(),
        "resolve_config": resolve_config,
        "mesh_fbx_uncook_path": mesh_fbx_uncook_path,
        "mesh_uncook_path": mesh_uncook_path,
        "executor": None,
        "pending_futures": {},
        "max_workers": _LAYER_SCAN_THREAD_WORKERS,
        "stats": {
            "layers_total": total,
            "indexed": 0,
            "cache_hits": 0,
            "scanned": 0,
            "missing": 0,
            "no_bounds": 0,
            "fast_skipped": 0,
            "template_cache_hits": 0,
            "template_cache_misses": 0,
            "template_cache_stores": 0,
            "template_cycles": 0,
        },
    }
    detail = (
        f"Preparing layer scan: {cache_hit_count:,} cached, "
        f"{parse_count:,} to parse, {missing_count:,} missing"
    )
    _set_layer_stream_progress(job, title, 0, total, detail)

    if parse_count > 0:
        worker_count = max(1, min(_LAYER_SCAN_THREAD_WORKERS, parse_count))
        job["scan"]["executor"] = ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="w3-layer-scan",
        )


def _shutdown_scan_executor(scan, *, cancel_pending=False):
    executor = scan.get("executor")
    if executor is None:
        return
    if cancel_pending:
        for future in scan.get("pending_futures", {}).values():
            future.cancel()
    try:
        executor.shutdown(wait=False)
    except Exception:
        pass
    scan["executor"] = None
    scan["pending_futures"] = {}


def _finalize_layer_scan_index(job, root_collection):
    scan = job.get("scan", {}) or {}
    _shutdown_scan_executor(scan, cancel_pending=True)
    _sync_layer_scan_dependency_cache_stats(scan)
    stats = scan.get("stats", {}) or {}
    stats["indexed"] = len(scan.get("entries", []))
    cache_conn = scan.get("cache_conn")
    if cache_conn is not None:
        cache_conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("version", str(int(_WORLD_LAYER_SCAN_CACHE_VERSION))),
        )
        cache_conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("world_path", str(scan.get("world_path", "") or "")),
        )
        _close_world_layer_cache_db(cache_conn, commit=True)
        scan["cache_conn"] = None

    index = {
        "cache_key": scan.get("cache_key", ""),
        "cache_path": scan.get("cache_path", ""),
        "cache_backend": "sqlite",
        "entries": list(scan.get("entries", [])),
        "stats": dict(stats),
    }
    if index["cache_key"]:
        _WORLD_LAYER_INDEX_CACHE[index["cache_key"]] = index
        _build_world_layer_runtime_index(index)
    job["index_data"] = index
    _set_layer_stream_progress(
        job,
        job.get("title", "Scanning world layers"),
        scan.get("total", 0),
        scan.get("total", 0),
        (
            f"Scan complete: indexed {int(stats.get('indexed', 0))}/{int(stats.get('layers_total', 0))} "
            f"(scanned {int(stats.get('scanned', 0))}, cached {int(stats.get('cache_hits', 0))}, "
            f"fast-path {int(stats.get('fast_scanned', 0))}, missing {int(stats.get('missing', 0))}, "
            f"no bounds {int(stats.get('no_bounds', 0))}, "
            f"fast-skipped {int(stats.get('fast_skipped', 0))}, "
            f"template reuse {int(stats.get('template_cache_hits', 0))} hits / {int(stats.get('template_cache_stores', 0))} unique)"
        ),
    )
    if _LAYER_SCAN_TIMING_ENABLED:
        timing_totals = scan.get("timing_totals") or {}
        profiled_layers = int(timing_totals.get("layers", 0) or 0)
        if profiled_layers > 0:
            avg_total = float(timing_totals.get("total_seconds", 0.0) or 0.0) / profiled_layers
            avg_parse = float(timing_totals.get("parse_seconds", 0.0) or 0.0) / profiled_layers
            avg_manifest = float(timing_totals.get("manifest_seconds", 0.0) or 0.0) / profiled_layers
            avg_create = float(timing_totals.get("create_level_seconds", 0.0) or 0.0) / profiled_layers
            avg_resolve = float(timing_totals.get("resolve_plan_seconds", 0.0) or 0.0) / profiled_layers
            avg_store = float(timing_totals.get("store_seconds", 0.0) or 0.0) / profiled_layers
            _log_layer_scan_timing_warning(
                "final summary: %d layers, avg total %s (parse %s, manifest %s, create_level %s, resolve_plan %s, store %s), fast-path %d, fast-skipped %d, slowest %s %s",
                profiled_layers,
                _format_layer_scan_timing(avg_total),
                _format_layer_scan_timing(avg_parse),
                _format_layer_scan_timing(avg_manifest),
                _format_layer_scan_timing(avg_create),
                _format_layer_scan_timing(avg_resolve),
                _format_layer_scan_timing(avg_store),
                int(stats.get("fast_scanned", 0) or 0),
                int(stats.get("fast_skipped", 0) or 0),
                timing_totals.get("slowest_layer_name", "") or "<none>",
                _format_layer_scan_timing(timing_totals.get("slowest_total_seconds", 0.0)),
            )

        dep_stats = stats
        dep_total = float(dep_stats.get("template_total_seconds", 0.0) or 0.0)
        dep_stores = int(dep_stats.get("template_cache_stores", 0) or 0)
        if dep_stores > 0 or dep_total > 0.0:
            dep_cache = scan.get("dependency_cache") or {}
            dep_cache_stats = _combined_layer_scan_dependency_stats(dep_cache)
            _log_layer_scan_timing_warning(
                "dependency summary: %d unique, %d hits, total %s (parse %s, create_level %s), slowest %s %s",
                dep_stores,
                int(dep_stats.get("template_cache_hits", 0) or 0),
                _format_layer_scan_timing(dep_total),
                _format_layer_scan_timing(dep_stats.get("template_parse_seconds", 0.0)),
                _format_layer_scan_timing(dep_stats.get("template_create_level_seconds", 0.0)),
                dep_cache_stats.get("slowest_path", "") or "<none>",
                _format_layer_scan_timing(dep_cache_stats.get("slowest_total_seconds", 0.0)),
            )
    return index


def _record_scan_entry(scan, work_item, updated_entry):
    if not updated_entry.get("has_bounds", False):
        scan["stats"]["no_bounds"] += 1
        if updated_entry.get("_fast_skip", False):
            scan["stats"]["fast_skipped"] = int(scan["stats"].get("fast_skipped", 0) or 0) + 1
        return
    scan["entries"].append(
        _make_world_layer_index_entry(
            work_item["collection_name"],
            work_item["level_path"],
            work_item["level_key"],
            updated_entry,
        )
    )
    scan["stats"]["indexed"] = len(scan["entries"])


def _record_layer_scan_timing(scan, work_item, entry, store_seconds):
    timing = entry.pop("_timing", None)
    if not timing or not _LAYER_SCAN_TIMING_ENABLED:
        return

    totals = scan.setdefault("timing_totals", _new_layer_scan_timing_totals())
    totals["layers"] += 1
    totals["total_seconds"] += float(timing.get("total_seconds", 0.0) or 0.0)
    totals["parse_seconds"] += float(timing.get("parse_seconds", 0.0) or 0.0)
    totals["bounds_seconds"] += float(timing.get("bounds_seconds", 0.0) or 0.0)
    totals["manifest_seconds"] += float(timing.get("manifest_seconds", 0.0) or 0.0)
    totals["create_level_seconds"] += float(timing.get("create_level_seconds", 0.0) or 0.0)
    totals["resolve_plan_seconds"] += float(timing.get("resolve_plan_seconds", 0.0) or 0.0)
    totals["store_seconds"] += float(store_seconds or 0.0)

    layer_total_seconds = float(timing.get("total_seconds", 0.0) or 0.0) + float(store_seconds or 0.0)
    if layer_total_seconds > float(totals.get("slowest_total_seconds", 0.0) or 0.0):
        totals["slowest_total_seconds"] = layer_total_seconds
        totals["slowest_layer_name"] = str(work_item.get("level_name", "") or work_item.get("level_path", "") or "Layer")
        totals["slowest_details"] = {
            "parse_seconds": float(timing.get("parse_seconds", 0.0) or 0.0),
            "bounds_seconds": float(timing.get("bounds_seconds", 0.0) or 0.0),
            "manifest_seconds": float(timing.get("manifest_seconds", 0.0) or 0.0),
            "create_level_seconds": float(timing.get("create_level_seconds", 0.0) or 0.0),
            "resolve_plan_seconds": float(timing.get("resolve_plan_seconds", 0.0) or 0.0),
            "store_seconds": float(store_seconds or 0.0),
            "import_item_count": int(entry.get("import_item_count", 0) or 0),
        }

    if layer_total_seconds >= _LAYER_SCAN_LAYER_WARN_THRESHOLD:
        _log_layer_scan_timing_warning(
            "layer %s total %s (parse %s, bounds %s, manifest %s, create_level %s, resolve_plan %s, store %s, items %d)",
            work_item.get("level_name", "") or work_item.get("level_path", "") or "Layer",
            _format_layer_scan_timing(layer_total_seconds),
            _format_layer_scan_timing(timing.get("parse_seconds", 0.0)),
            _format_layer_scan_timing(timing.get("bounds_seconds", 0.0)),
            _format_layer_scan_timing(timing.get("manifest_seconds", 0.0)),
            _format_layer_scan_timing(timing.get("create_level_seconds", 0.0)),
            _format_layer_scan_timing(timing.get("resolve_plan_seconds", 0.0)),
            _format_layer_scan_timing(store_seconds),
            int(entry.get("import_item_count", 0) or 0),
        )

    if totals["layers"] % _LAYER_SCAN_TIMING_PROGRESS_INTERVAL == 0:
        avg_total = totals["total_seconds"] / max(1, totals["layers"])
        avg_parse = totals["parse_seconds"] / max(1, totals["layers"])
        avg_manifest = totals["manifest_seconds"] / max(1, totals["layers"])
        avg_create = totals["create_level_seconds"] / max(1, totals["layers"])
        avg_resolve = totals["resolve_plan_seconds"] / max(1, totals["layers"])
        avg_store = totals["store_seconds"] / max(1, totals["layers"])
        _log_layer_scan_timing_warning(
            "summary after %d layers: avg total %s (parse %s, manifest %s, create_level %s, resolve_plan %s, store %s), fast-path %d, fast-skipped %d, slowest %s %s",
            totals["layers"],
            _format_layer_scan_timing(avg_total),
            _format_layer_scan_timing(avg_parse),
            _format_layer_scan_timing(avg_manifest),
            _format_layer_scan_timing(avg_create),
            _format_layer_scan_timing(avg_resolve),
            _format_layer_scan_timing(avg_store),
            int((scan.get("stats", {}) or {}).get("fast_scanned", 0) or 0),
            int((scan.get("stats", {}) or {}).get("fast_skipped", 0) or 0),
            totals.get("slowest_layer_name", "") or "<none>",
            _format_layer_scan_timing(totals.get("slowest_total_seconds", 0.0)),
        )


def _dispatch_scan_parse_jobs(scan):
    """Top up the scan worker pool with up to max_workers in-flight futures."""
    executor = scan.get("executor")
    if executor is None:
        return
    work_items = scan["work_items"]
    total = scan["total"]
    pending_futures = scan["pending_futures"]
    max_workers = scan["max_workers"]
    next_dispatch = scan.get("next_dispatch_index", scan["queue_index"])
    while len(pending_futures) < max_workers and next_dispatch < total:
        item = work_items[next_dispatch]
        if item.get("needs_parse") and item.get("future") is None and not item.get("missing"):
            future = executor.submit(
                _scan_level_cache_entry,
                item["level_path"],
                item["resolved_path"],
                item["file_mtime"],
                item["file_size"],
                dependency_cache=scan.get("dependency_cache"),
                resolve_config=scan.get("resolve_config"),
                mesh_fbx_uncook_path=scan.get("mesh_fbx_uncook_path"),
                mesh_uncook_path=scan.get("mesh_uncook_path"),
            )
            item["future"] = future
            pending_futures[next_dispatch] = future
        next_dispatch += 1
    scan["next_dispatch_index"] = next_dispatch


def _process_scan_work_item(scan, item, entry):
    """Main-thread bookkeeping for a fully-resolved layer cache entry."""
    store_started = time.perf_counter()
    _store_world_layer_cache_entry(scan.get("cache_conn"), item["level_key"], entry)
    store_seconds = time.perf_counter() - store_started
    scan["stats"]["scanned"] += 1
    if entry.get("_fast_scan", False):
        scan["stats"]["fast_scanned"] = int(scan["stats"].get("fast_scanned", 0) or 0) + 1
    _sync_layer_scan_dependency_cache_stats(scan)
    _record_scan_entry(scan, item, entry)
    _record_layer_scan_timing(scan, item, entry, store_seconds)


def _process_layer_scan_batch(job, context):
    scan = job.get("scan", {}) or {}
    root_collection = bpy.data.collections.get(job.get("root_collection_name", ""))
    if root_collection is None:
        raise RuntimeError("World root collection no longer exists")

    total = int(scan.get("total", 0) or 0)
    if total <= 0:
        _finalize_layer_scan_index(job, root_collection)
        return True

    if job.get("cancel_requested"):
        _shutdown_scan_executor(scan, cancel_pending=True)
        _sync_layer_scan_dependency_cache_stats(scan)
        return False

    work_items = scan["work_items"]
    pending_futures = scan["pending_futures"]
    title = job.get("title", "Scanning world layers")

    _dispatch_scan_parse_jobs(scan)

    completed_in_batch = 0
    while completed_in_batch < _LAYER_SCAN_BATCH_SIZE and scan["queue_index"] < total:
        item_index = scan["queue_index"]
        item = work_items[item_index]

        if item["missing"]:
            scan["stats"]["missing"] += 1
        elif item["cached_entry"] is not None:
            scan["stats"]["cache_hits"] += 1
            updated_entry = dict(item["cached_entry"])
            _record_scan_entry(scan, item, updated_entry)
        else:
            future = item.get("future")
            if future is None:
                # Synchronous fallback when no executor is in use.
                updated_entry = _scan_level_cache_entry(
                    item["level_path"],
                    item["resolved_path"],
                    item["file_mtime"],
                    item["file_size"],
                    dependency_cache=scan.get("dependency_cache"),
                    resolve_config=scan.get("resolve_config"),
                    mesh_fbx_uncook_path=scan.get("mesh_fbx_uncook_path"),
                    mesh_uncook_path=scan.get("mesh_uncook_path"),
                )
                _process_scan_work_item(scan, item, updated_entry)
            else:
                if not future.done():
                    break
                pending_futures.pop(item_index, None)
                try:
                    updated_entry = future.result()
                except Exception as exc:
                    log.warning("Layer scan failed for %s: %s", item["resolved_path"], exc)
                    updated_entry = {
                        "level_path": item["level_path"],
                        "resolved_path": item["resolved_path"],
                        "file_mtime": item["file_mtime"],
                        "file_size": item["file_size"],
                        "has_bounds": False,
                        "has_manifest": False,
                        "import_item_count": 0,
                        "items": [],
                    }
                item["future"] = None
                _process_scan_work_item(scan, item, updated_entry)
                _dispatch_scan_parse_jobs(scan)

        scan["queue_index"] += 1
        scan["completed_index"] += 1
        completed_in_batch += 1

    _sync_layer_scan_dependency_cache_stats(scan)
    progress_index = scan["completed_index"]
    detail_item = work_items[min(progress_index, total - 1)] if total > 0 else None
    detail_name = detail_item["level_name"] if detail_item else ""
    pending_label = f", {len(pending_futures)} scanning" if pending_futures else ""
    _set_layer_stream_progress(
        job,
        title,
        progress_index,
        total,
        f"Scanning {progress_index}/{total}: {detail_name}{pending_label}",
    )

    if scan["completed_index"] >= total:
        _finalize_layer_scan_index(job, root_collection)
        return True
    return False


def _query_world_layer_cache_nearby_candidates(
    index,
    camera_position,
    radius,
    skip_complete,
    mode_signature=None,
    item_kind_filter=None,
    import_filter_kwargs=None,
    context=None,
):
    cache_path = str(index.get("cache_path", "") or "").strip()
    if not cache_path or camera_position is None:
        return [], 0

    radius_value = max(0.0, float(radius or 0.0))
    if radius_value <= 0.0:
        return [], 0

    started = time.perf_counter()
    entry_map_started = time.perf_counter()
    entry_by_level_key = _world_layer_entry_map(index)
    entry_map_seconds = time.perf_counter() - entry_map_started

    skip_started = time.perf_counter()
    skip_level_keys = _world_layer_cache_skip_level_keys(
        index,
        skip_complete,
        camera_position=camera_position,
        radius=radius_value,
        mode_signature=mode_signature,
    )
    skip_seconds = time.perf_counter() - skip_started
    cell_size = float(_WORLD_LAYER_SPATIAL_CELL_SIZE)
    min_cell_x = int(math.floor((float(camera_position[0]) - radius_value) / cell_size))
    max_cell_x = int(math.floor((float(camera_position[0]) + radius_value) / cell_size))
    min_cell_y = int(math.floor((float(camera_position[1]) - radius_value) / cell_size))
    max_cell_y = int(math.floor((float(camera_position[1]) + radius_value) / cell_size))
    radius_sq = radius_value * radius_value

    sql = (
        "SELECT level_key, COUNT(*) AS nearby_item_count, "
        "MIN(((world_x - ?) * (world_x - ?) + (world_y - ?) * (world_y - ?))) AS nearest_sq "
        "FROM item_spatial "
        "WHERE cell_x BETWEEN ? AND ? "
        "AND cell_y BETWEEN ? AND ? "
        "AND ((world_x - ?) * (world_x - ?) + (world_y - ?) * (world_y - ?)) <= ?"
    )
    params = [
        float(camera_position[0]),
        float(camera_position[0]),
        float(camera_position[1]),
        float(camera_position[1]),
        min_cell_x,
        max_cell_x,
        min_cell_y,
        max_cell_y,
        float(camera_position[0]),
        float(camera_position[0]),
        float(camera_position[1]),
        float(camera_position[1]),
        radius_sq,
    ]
    if skip_level_keys:
        placeholders = ",".join("?" for _ in skip_level_keys)
        sql += f" AND level_key NOT IN ({placeholders})"
        params.extend(sorted(skip_level_keys))
    sql += " GROUP BY level_key"

    conn = _open_world_layer_cache_db(cache_path)
    if conn is None:
        return [], len(skip_level_keys)
    try:
        sql_started = time.perf_counter()
        rows = conn.execute(sql, params).fetchall()
        sql_seconds = time.perf_counter() - sql_started
    finally:
        _close_world_layer_cache_db(conn)

    materialize_started = time.perf_counter()
    candidates = []
    seen_level_keys = set()
    for row in rows:
        level_key = str(row["level_key"] or "").strip()
        if not level_key:
            continue
        entry = entry_by_level_key.get(level_key)
        if entry is None:
            continue
        nearby_item_count = int(row["nearby_item_count"] or 0)
        nearest_sq = float(row["nearest_sq"] or 0.0)
        if item_kind_filter or import_filter_kwargs is not None:
            _load_cached_plan_items_for_entry(index, entry)
            nearby_item_count, filtered_nearest_sq = _count_nearby_manifest_items_for_entry(
                entry,
                camera_position,
                radius_sq,
                item_kind_filter=item_kind_filter,
                import_filter_kwargs=import_filter_kwargs,
                context=context,
            )
            if nearby_item_count <= 0:
                continue
            nearest_sq = filtered_nearest_sq if filtered_nearest_sq is not None else nearest_sq
        seen_level_keys.add(level_key)
        candidates.append(
            (
                nearest_sq,
                entry["collection_name"],
                entry,
                nearby_item_count,
            )
        )
    materialize_seconds = time.perf_counter() - materialize_started

    # Fallback for layers missing manifest rows: use bounds-only candidate selection.
    fallback_started = time.perf_counter()
    for entry in index.get("entries", []):
        level_key = str(entry.get("level_key", "") or "").strip()
        if not level_key or level_key in seen_level_keys or level_key in skip_level_keys:
            continue
        if entry.get("has_manifest", False):
            continue
        if "min_x" not in entry:
            continue
        collection = bpy.data.collections.get(entry.get("collection_name", ""))
        if collection is None:
            continue
        if skip_complete and _layer_should_skip_for_load(collection, camera_position, radius_value, mode_signature=mode_signature):
            continue
        distance_sq = _distance_sq_to_bounds_xy(
            float(camera_position[0]),
            float(camera_position[1]),
            entry,
        )
        if distance_sq > radius_sq:
            continue
        candidates.append((distance_sq, entry["collection_name"], entry, 0))
    fallback_seconds = time.perf_counter() - fallback_started

    total_seconds = time.perf_counter() - started
    _log_layer_load_timing_warning(
        "nearby query total %s (entry map %s, skip %s, sql %s, rows %s, bounds fallback %s, entries %d, skipped %d, sql rows %d, candidates %d)",
        _format_layer_scan_timing(total_seconds),
        _format_layer_scan_timing(entry_map_seconds),
        _format_layer_scan_timing(skip_seconds),
        _format_layer_scan_timing(sql_seconds),
        _format_layer_scan_timing(materialize_seconds),
        _format_layer_scan_timing(fallback_seconds),
        len(index.get("entries", []) or []),
        len(skip_level_keys),
        len(rows),
        len(candidates),
    )
    return candidates, len(skip_level_keys)


def _load_cached_plan_items_for_entry(index, entry):
    if not isinstance(entry, dict):
        return None
    if "items" in entry:
        return list(entry.get("items", []) or [])
    if not entry.get("has_manifest", False):
        return None
    if str(index.get("cache_backend", "") or "").strip().lower() != "sqlite":
        return None
    cache_path = str(index.get("cache_path", "") or "").strip()
    level_key = str(entry.get("level_key", "") or "").strip()
    if not cache_path or not level_key:
        return None
    conn = _open_world_layer_cache_db(cache_path)
    if conn is None:
        return None
    try:
        items = _load_world_layer_cache_items(conn, level_key)
    finally:
        _close_world_layer_cache_db(conn)
    entry["items"] = list(items or [])
    return entry["items"]


def _prepare_layer_load_phase(job, context):
    root_collection = bpy.data.collections.get(job.get("root_collection_name", ""))
    if root_collection is None:
        raise RuntimeError("World root collection no longer exists")

    prepare_started = time.perf_counter()
    index = job.get("index_data") or {"entries": [], "stats": {"indexed": 0}}
    radius = max(1.0, float(job.get("radius", 0.0) or 0.0))
    load_limit = max(0, int(job.get("load_limit", 0) or 0))
    skip_complete = bool(job.get("skip_complete"))
    camera_position = job.get("camera_position")
    if camera_position is None:
        raise RuntimeError("Could not determine a viewport or scene camera position")

    radius_sq = radius * radius
    query_seconds = 0.0
    mode_signature = job.get("mode_signature")
    import_filter_kwargs = job.get("import_filter_kwargs") if bool(job.get("import_filter_active")) else None
    item_kind_filter = _LAYER_QUERY_FILTER_KINDS if import_filter_kwargs is not None else None
    if str(index.get("cache_backend", "") or "").strip().lower() == "sqlite":
        query_started = time.perf_counter()
        candidates, skipped_complete = _query_world_layer_cache_nearby_candidates(
            index,
            camera_position,
            radius,
            skip_complete,
            mode_signature=mode_signature,
            item_kind_filter=item_kind_filter,
            import_filter_kwargs=import_filter_kwargs,
            context=context,
        )
        query_seconds = time.perf_counter() - query_started
    else:
        candidates = []
        skipped_complete = 0

        query_started = time.perf_counter()
        for entry in index.get("entries", []):
            collection = bpy.data.collections.get(entry["collection_name"])
            if collection is None:
                continue
            if skip_complete and _layer_should_skip_for_load(collection, camera_position, radius, mode_signature=mode_signature):
                skipped_complete += 1
                continue
            nearby_item_count, nearest_item_sq = _count_nearby_manifest_items_for_entry(
                entry,
                camera_position,
                radius_sq,
                item_kind_filter=item_kind_filter,
                import_filter_kwargs=import_filter_kwargs,
                context=context,
            )
            if entry.get("items"):
                if nearby_item_count <= 0:
                    continue
                distance_sq = nearest_item_sq if nearest_item_sq is not None else _distance_sq_to_bounds_xy(camera_position[0], camera_position[1], entry)
            else:
                distance_sq = _distance_sq_to_bounds_xy(camera_position[0], camera_position[1], entry)
                if distance_sq > radius_sq:
                    continue
            candidates.append((distance_sq, collection.name, entry, nearby_item_count))
        query_seconds = time.perf_counter() - query_started

    sort_started = time.perf_counter()
    candidates.sort(key=lambda item: (item[0], item[2]["level_path"].lower()))
    selected_candidates = candidates if load_limit <= 0 else candidates[:load_limit]
    sort_seconds = time.perf_counter() - sort_started

    view_layer = getattr(context, "view_layer", None)
    job["phase"] = "load"
    job["load"] = {
        "items": selected_candidates,
        "index": 0,
        "total": len(selected_candidates),
        "candidate_count": len(candidates),
        "candidate_item_count": sum(int(item[3] or 0) for item in candidates),
        "selected_item_count": sum(int(item[3] or 0) for item in selected_candidates),
        "imported": 0,
        "failed": 0,
        "skipped_complete": skipped_complete,
        "messages": [],
        "previous_active_layer_collection": getattr(view_layer, "active_layer_collection", None) if view_layer else None,
        "batch_isolation_stack": None,
        "batch_isolation_session": None,
        "timing_totals": _new_layer_load_timing_totals(),
    }
    _set_layer_stream_progress(job, "Loading nearby layers", 0, len(selected_candidates), "Preparing nearby layer import...")

    if not selected_candidates:
        total_seconds = time.perf_counter() - prepare_started
        _log_layer_load_timing_warning(
            "prepare load total %s (query %s, sort %s, isolation %s, indexed %d, candidates %d, selected %d, nearby items %d, skipped complete %d)",
            _format_layer_scan_timing(total_seconds),
            _format_layer_scan_timing(query_seconds),
            _format_layer_scan_timing(sort_seconds),
            _format_layer_scan_timing(0.0),
            int(index.get("stats", {}).get("indexed", 0) or 0),
            len(candidates),
            len(selected_candidates),
            int(job["load"].get("selected_item_count", 0) or 0),
            int(skipped_complete or 0),
        )
        job["summary"] = (
            f"No nearby importable objects within {radius:.0f} world units "
            f"(indexed {index.get('stats', {}).get('indexed', 0)}, skipped complete {skipped_complete})"
        )
        return False
    isolation_started = time.perf_counter()
    _start_layer_load_batch_isolation(job, context)
    isolation_seconds = time.perf_counter() - isolation_started
    total_seconds = time.perf_counter() - prepare_started
    _log_layer_load_timing_warning(
        "prepare load total %s (query %s, sort %s, isolation %s, indexed %d, candidates %d, selected %d, nearby items %d, skipped complete %d)",
        _format_layer_scan_timing(total_seconds),
        _format_layer_scan_timing(query_seconds),
        _format_layer_scan_timing(sort_seconds),
        _format_layer_scan_timing(isolation_seconds),
        int(index.get("stats", {}).get("indexed", 0) or 0),
        len(candidates),
        len(selected_candidates),
        int(job["load"].get("selected_item_count", 0) or 0),
        int(skipped_complete or 0),
    )
    return True


def _start_layer_load_batch_isolation(job, context):
    load = job.get("load", {}) or {}
    if load.get("batch_isolation_stack") is not None:
        return load.get("batch_isolation_session")
    stack = ExitStack()
    try:
        session = stack.enter_context(
            import_isolation.isolated_import_batch_session(
                context,
                label="NearbyLayers",
            )
        )
    except Exception:
        stack.close()
        raise
    load["batch_isolation_stack"] = stack
    load["batch_isolation_session"] = session
    return session


def _record_layer_load_timing(load, collection_name, plan_item_count, used_cached_plan, plan_load_seconds, import_seconds, total_seconds, ok):
    totals = load.get("timing_totals")
    if not isinstance(totals, dict):
        totals = _new_layer_load_timing_totals()
        load["timing_totals"] = totals
    totals["layers"] = int(totals.get("layers", 0) or 0) + 1
    totals["total_seconds"] = float(totals.get("total_seconds", 0.0) or 0.0) + float(total_seconds or 0.0)
    totals["plan_load_seconds"] = float(totals.get("plan_load_seconds", 0.0) or 0.0) + float(plan_load_seconds or 0.0)
    totals["import_seconds"] = float(totals.get("import_seconds", 0.0) or 0.0) + float(import_seconds or 0.0)
    if float(total_seconds or 0.0) > float(totals.get("slowest_total_seconds", 0.0) or 0.0):
        totals["slowest_total_seconds"] = float(total_seconds or 0.0)
        totals["slowest_layer_name"] = str(collection_name or "")

    if (not ok) or float(total_seconds or 0.0) >= _LAYER_LOAD_LAYER_WARN_THRESHOLD:
        _log_layer_load_timing_warning(
            "layer %s total %s (plan %s, import %s, plan items %d, cached-plan %s, ok %s)",
            collection_name or "<unknown>",
            _format_layer_scan_timing(total_seconds),
            _format_layer_scan_timing(plan_load_seconds),
            _format_layer_scan_timing(import_seconds),
            int(plan_item_count or 0),
            "yes" if used_cached_plan else "no",
            "yes" if ok else "no",
        )

    if totals["layers"] % _LAYER_LOAD_TIMING_PROGRESS_INTERVAL == 0:
        layer_count = max(1, int(totals.get("layers", 0) or 0))
        _log_layer_load_timing_warning(
            "summary after %d layers: avg total %s (plan %s, import %s), slowest %s %s",
            layer_count,
            _format_layer_scan_timing(float(totals.get("total_seconds", 0.0) or 0.0) / layer_count),
            _format_layer_scan_timing(float(totals.get("plan_load_seconds", 0.0) or 0.0) / layer_count),
            _format_layer_scan_timing(float(totals.get("import_seconds", 0.0) or 0.0) / layer_count),
            totals.get("slowest_layer_name", "") or "<none>",
            _format_layer_scan_timing(totals.get("slowest_total_seconds", 0.0)),
        )


def _close_layer_load_batch_isolation(job):
    load = job.get("load", {}) or {}
    stack = load.get("batch_isolation_stack")
    load["batch_isolation_stack"] = None
    load["batch_isolation_session"] = None
    if stack is not None:
        try:
            stack.close()
        except Exception:
            pass


def _process_layer_load_batch(job, context):
    load = job.get("load", {}) or {}
    total = int(load.get("total", 0) or 0)
    if total <= 0:
        return True

    batch_end = min(int(load.get("index", 0) or 0) + _LAYER_LOAD_BATCH_SIZE, total)
    while int(load.get("index", 0) or 0) < batch_end and not job.get("cancel_requested"):
        item_index = int(load.get("index", 0) or 0)
        _distance_sq, collection_name, entry, _nearby_item_count = load["items"][item_index]
        load["index"] = item_index + 1
        _set_layer_stream_progress(job, "Loading nearby layers", load["index"] - 1, total, f"Loading nearby layer {load['index']}/{total}: {collection_name}")

        collection = bpy.data.collections.get(collection_name)
        layer_started = time.perf_counter()
        plan_load_seconds = 0.0
        import_seconds = 0.0
        plan_item_count = 0
        used_cached_plan = False
        cancelled_during_layer = False
        if collection is None:
            ok = False
            resolved = ""
            err = "Collection no longer exists"
        else:
            plan_started = time.perf_counter()
            cached_plan_items = _load_cached_plan_items_for_entry(job.get("index_data") or {}, entry)
            plan_load_seconds = time.perf_counter() - plan_started
            plan_item_count = len(cached_plan_items or [])
            used_cached_plan = cached_plan_items is not None
            import_started = time.perf_counter()
            ok, resolved, err, cancelled_during_layer = _import_level_from_collection(
                context,
                collection,
                camera_position=job.get("camera_position"),
                radius=job.get("radius", 0.0),
                isolation_batch_session=load.get("batch_isolation_session"),
                cached_plan_items=cached_plan_items,
                cancel_check=lambda: bool(job.get("cancel_requested")),
                import_settings=job.get("import_kwargs"),
                mode_signature=job.get("mode_signature"),
            )
            import_seconds = time.perf_counter() - import_started

        total_seconds = time.perf_counter() - layer_started
        _record_layer_load_timing(
            load,
            collection_name,
            plan_item_count,
            used_cached_plan,
            plan_load_seconds,
            import_seconds,
            total_seconds,
            ok,
        )

        if cancelled_during_layer:
            job["cancel_requested"] = True
            _update_world_layer_complete_state(job.get("index_data") or {}, entry, collection)
            _set_layer_stream_progress(
                job,
                "Loading nearby layers",
                load["index"] - 1,
                total,
                f"Cancelling nearby layer load after {collection_name}",
            )
            break

        if ok:
            load["imported"] += 1
            _update_world_layer_complete_state(job.get("index_data") or {}, entry, collection)
            _set_layer_stream_progress(job, "Loading nearby layers", load["index"], total, f"Loading nearby layer {load['index']}/{total}: {collection_name}")
            continue

        load["failed"] += 1
        _update_world_layer_complete_state(job.get("index_data") or {}, entry, collection)
        msg = f"Can't load level {collection_name} ({entry['level_path']})"
        if resolved:
            msg += f" from {resolved}"
        if err:
            msg += f": {err}"
        load["messages"].append(msg)
        log.warning("%s", msg)
        _set_layer_stream_progress(job, "Loading nearby layers", load["index"], total, f"Loading nearby layer {load['index']}/{total}: {collection_name}")

    if int(load.get("index", 0) or 0) >= total:
        index = job.get("index_data") or {"stats": {"indexed": 0}}
        radius = max(1.0, float(job.get("radius", 0.0) or 0.0))
        timing_totals = load.get("timing_totals") or {}
        loaded_layers = max(1, int(timing_totals.get("layers", 0) or 0))
        _log_layer_load_timing_warning(
            "final summary: %d layers, avg total %s (plan %s, import %s), slowest %s %s",
            int(timing_totals.get("layers", 0) or 0),
            _format_layer_scan_timing(float(timing_totals.get("total_seconds", 0.0) or 0.0) / loaded_layers),
            _format_layer_scan_timing(float(timing_totals.get("plan_load_seconds", 0.0) or 0.0) / loaded_layers),
            _format_layer_scan_timing(float(timing_totals.get("import_seconds", 0.0) or 0.0) / loaded_layers),
            timing_totals.get("slowest_layer_name", "") or "<none>",
            _format_layer_scan_timing(timing_totals.get("slowest_total_seconds", 0.0)),
        )
        job["summary"] = (
            f"Loaded {int(load.get('imported', 0))}/{total} nearby layers "
            f"covering {int(load.get('selected_item_count', 0)):,} cached nearby objects "
            f"(radius {radius:.0f} world units, indexed {int(index.get('stats', {}).get('indexed', 0))})"
        )
        if int(job.get("load_limit", 0) or 0) > 0 and int(load.get("candidate_count", 0) or 0) > total:
            job["summary"] += f", limited from {int(load.get('candidate_count', 0))}"
        if bool(job.get("skip_complete")):
            job["summary"] += f", skipped complete {int(load.get('skipped_complete', 0))}"
        if int(load.get("failed", 0) or 0) > 0:
            job["summary"] += f", failed {int(load.get('failed', 0))}"
        _set_layer_stream_progress(
            job,
            "Loading nearby layers",
            total,
            total,
            f"Finished loading nearby layers: {int(load.get('imported', 0))}/{total} imported, {int(load.get('failed', 0))} failed",
        )
        return True
    return False


def _finish_layer_stream_job(operator, context, cancelled=False, failed=False):
    job = dict(_LAYER_STREAM_JOB)
    wm = job.get("wm")
    timer = job.get("timer")
    profile_log_path = str(job.get("profile_log_path", "") or "")
    if wm and timer:
        try:
            wm.event_timer_remove(timer)
        except Exception:
            pass

    load = job.get("load", {}) or {}
    _close_layer_load_batch_isolation(job)
    previous_active_layer_collection = load.get("previous_active_layer_collection")
    view_layer = getattr(context, "view_layer", None)
    if view_layer is not None and previous_active_layer_collection is not None:
        try:
            view_layer.active_layer_collection = previous_active_layer_collection
        except Exception:
            pass

    if job.get("phase") == "load" and not failed:
        _apply_layer_post_import_visibility(job, context)

    if failed:
        message = job.get("error", "") or "Layer scan/load failed."
        level = 'ERROR'
    elif job.get("mode") == "scan_cache":
        stats = (job.get("scan", {}) or {}).get("stats", {}) or {}
        if cancelled:
            message = (
                f"Layer scan cancelled at {int(job.get('current', 0)):,}/{int(job.get('total', 0)):,}: "
                f"indexed {int(stats.get('indexed', 0)):,}, scanned {int(stats.get('scanned', 0)):,}, "
                f"cached {int(stats.get('cache_hits', 0)):,}, fast-path {int(stats.get('fast_scanned', 0)):,}, "
                f"missing {int(stats.get('missing', 0)):,}, "
                f"no bounds {int(stats.get('no_bounds', 0)):,}, fast-skipped {int(stats.get('fast_skipped', 0)):,}, "
                f"template reuse {int(stats.get('template_cache_hits', 0)):,} hits "
                f"across {int(stats.get('template_cache_stores', 0)):,} unique dependencies."
            )
            level = 'WARNING'
        else:
            message = (
                f"Indexed {int(stats.get('indexed', 0))}/{int(stats.get('layers_total', 0))} layers "
                f"(scanned {int(stats.get('scanned', 0))}, cached {int(stats.get('cache_hits', 0))}, "
                f"fast-path {int(stats.get('fast_scanned', 0))}, missing {int(stats.get('missing', 0))}, "
                f"no bounds {int(stats.get('no_bounds', 0))}, "
                f"fast-skipped {int(stats.get('fast_skipped', 0))}, "
                f"template reuse {int(stats.get('template_cache_hits', 0))} hits / {int(stats.get('template_cache_stores', 0))} unique)"
            )
            level = 'INFO'
    else:
        scan = job.get("scan", {}) or {}
        load = job.get("load", {}) or {}
        if cancelled and job.get("phase") == "scan":
            stats = scan.get("stats", {}) or {}
            message = (
                f"Nearby layer load cancelled while scanning {int(job.get('current', 0)):,}/{int(job.get('total', 0)):,} layers: "
                f"indexed {int(stats.get('indexed', 0)):,}, scanned {int(stats.get('scanned', 0)):,}, "
                f"cached {int(stats.get('cache_hits', 0)):,}, fast-path {int(stats.get('fast_scanned', 0)):,}, "
                f"missing {int(stats.get('missing', 0)):,}, "
                f"no bounds {int(stats.get('no_bounds', 0)):,}, fast-skipped {int(stats.get('fast_skipped', 0)):,}, "
                f"template reuse {int(stats.get('template_cache_hits', 0)):,} hits "
                f"across {int(stats.get('template_cache_stores', 0)):,} unique dependencies."
            )
            level = 'WARNING'
        elif cancelled:
            message = (
                f"Nearby layer load cancelled at {int(load.get('index', 0)):,}/{int(load.get('total', 0)):,}: "
                f"{int(load.get('imported', 0)):,} imported, {int(load.get('failed', 0)):,} failed, "
                f"{int(load.get('skipped_complete', 0)):,} skipped complete."
            )
            level = 'WARNING'
        else:
            message = job.get("summary", "") or "Nearby layer load complete."
            level = 'WARNING' if int(load.get("failed", 0) or 0) > 0 else 'INFO'

    if profile_log_path:
        message = f"{message} Log: {Path(profile_log_path).name}"
        _stop_map_import_profile_log(_LAYER_STREAM_JOB, f"Map import profile log saved to {profile_log_path}")

    nearby_started_at = job.get("nearby_load_started_at")
    if nearby_started_at is not None:
        try:
            elapsed = time.perf_counter() - float(nearby_started_at)
            message = f"{message} (took {elapsed:.2f}s)"
        except Exception:
            pass

    _reset_layer_stream_job()
    _tag_layer_stream_redraw(context, wm=wm)
    operator.report({level}, message)
    return {'CANCELLED'} if (cancelled or failed) else {'FINISHED'}


def _collection_has_loaded_content(collection, mode_signature=None):
    if collection is None:
        return False
    state = str(collection.get("witcher_layer_import_state", "") or "").strip().lower()
    if state not in _LAYER_COMPLETE_STATES:
        return False
    if mode_signature is not None:
        prev_mode = str(collection.get("witcher_layer_load_mode", "") or "")
        if prev_mode and prev_mode != str(mode_signature):
            return False
    return True


def _layer_covered_by_previous_load(collection, camera_position, radius, mode_signature=None):
    if collection is None or camera_position is None:
        return False
    state = str(collection.get("witcher_layer_import_state", "") or "").strip().lower()
    if state not in _LAYER_COVERED_STATES:
        return False
    if collection.get("witcher_layer_load_radius") is None:
        return False
    if mode_signature is not None:
        prev_mode = str(collection.get("witcher_layer_load_mode", "") or "")
        if prev_mode and prev_mode != str(mode_signature):
            return False
    try:
        prev_radius = float(collection.get("witcher_layer_load_radius", 0.0) or 0.0)
        prev_x = float(collection.get("witcher_layer_load_camera_x", 0.0) or 0.0)
        prev_y = float(collection.get("witcher_layer_load_camera_y", 0.0) or 0.0)
        new_radius = float(radius or 0.0)
    except Exception:
        return False
    if prev_radius <= 0.0 or new_radius <= 0.0:
        return False
    dx = float(camera_position[0]) - prev_x
    dy = float(camera_position[1]) - prev_y
    # Viewport navigation can move the view origin slightly without changing the
    # intended load area. Keep small nudges from invalidating prior coverage.
    epsilon = max(1.0, min(25.0, prev_radius * 0.02))
    return (math.sqrt(dx * dx + dy * dy) + new_radius) <= (prev_radius + epsilon)


def _layer_should_skip_for_load(collection, camera_position, radius, mode_signature=None):
    if _collection_has_loaded_content(collection, mode_signature=mode_signature):
        return True
    return _layer_covered_by_previous_load(collection, camera_position, radius, mode_signature=mode_signature)


def _layer_visibility_token_name(obj):
    name = str(getattr(obj, "name", "") or "").lower().replace(".", "_")
    data_name = str(getattr(getattr(obj, "data", None), "name", "") or "").lower().replace(".", "_")
    return f"_{name}_{data_name}_"


def _layer_visibility_is_volume_mesh(obj):
    return "_volume_" in _layer_visibility_token_name(obj)


def _layer_visibility_is_shadow_mesh(obj):
    token_name = _layer_visibility_token_name(obj)
    compact_name = token_name.replace("_", "").replace("-", "").replace(" ", "")
    return "_shadow_" in token_name or "shadowmesh" in compact_name


def _apply_layer_post_import_visibility(job, context):
    hide_volume = bool((job or {}).get("hide_volume_meshes"))
    hide_shadow = bool((job or {}).get("hide_shadow_meshes"))
    if not hide_volume and not hide_shadow:
        return 0
    root_collection = bpy.data.collections.get(str((job or {}).get("root_collection_name", "") or ""))
    if root_collection is None:
        root_collection = _find_world_root_collection(context)
    if root_collection is None:
        return 0
    hidden_count = 0
    for obj in list(getattr(root_collection, "all_objects", []) or []):
        if getattr(obj, "type", "") != 'MESH':
            continue
        should_hide = (
            (hide_volume and _layer_visibility_is_volume_mesh(obj))
            or (hide_shadow and _layer_visibility_is_shadow_mesh(obj))
        )
        if not should_hide:
            continue
        try:
            obj.hide_viewport = True
            obj.hide_render = True
            hidden_count += 1
        except Exception:
            pass
    if hidden_count:
        _log_layer_load_timing_warning("post-import visibility hid %d mesh object(s)", hidden_count)
    return hidden_count


def _distance_sq_to_bounds_xy(point_x, point_y, entry):
    dx = 0.0
    dy = 0.0
    if point_x < entry["min_x"]:
        dx = entry["min_x"] - point_x
    elif point_x > entry["max_x"]:
        dx = point_x - entry["max_x"]
    if point_y < entry["min_y"]:
        dy = entry["min_y"] - point_y
    elif point_y > entry["max_y"]:
        dy = point_y - entry["max_y"]
    return dx * dx + dy * dy


def _get_current_view3d_area(context=None):
    area = getattr(context, "area", None) if context is not None else None
    if getattr(area, "type", "") == 'VIEW_3D':
        return area

    window = getattr(context, "window", None) if context is not None else None
    if window is None:
        window = getattr(bpy.context, "window", None)
    screen = getattr(window, "screen", None)
    if screen is None:
        return None

    for area in getattr(screen, "areas", []) or []:
        if getattr(area, "type", "") == 'VIEW_3D':
            return area
    return None


def _get_camera_position(context=None):
    area = _get_current_view3d_area(context)
    if area is None:
        return None

    try:
        region_3d = area.spaces.active.region_3d
        current_location = region_3d.view_matrix.inverted().translation
        return float(current_location.x), float(current_location.y), float(current_location.z)
    except Exception:
        return None


def get_camera_position_label(context):
    position = _get_camera_position(context)
    if position is None:
        return "Camera Position: unavailable"
    return f"Camera Position: {position[0]:.1f}, {position[1]:.1f}, {position[2]:.1f}"


_nearby_scan_label = "Scan Cache Nearby: click Scan to update"


def _compute_nearby_cache_summary_label(context, camera_position, root_collection=None):
    if camera_position is None:
        return "Scan Cache Nearby: camera position unavailable"

    root_collection = root_collection or _find_world_root_collection(context)
    if root_collection is None:
        return "Scan Cache Nearby: no world root selected"

    cache_key = _world_layer_cache_key(context, root_collection)
    index = _WORLD_LAYER_INDEX_CACHE.get(cache_key)
    if index is None:
        index = _hydrate_world_layer_index_from_disk(context, root_collection)
    if index is None:
        return "Scan Cache Nearby: rebuild cache for exact counts"

    runtime = _get_world_layer_runtime_index(index)
    if runtime is None:
        return "Scan Cache Nearby: rebuild cache for exact counts"

    scene_settings = getattr(getattr(context, "scene", None), "witcher_file_browser", None)
    radius = max(1.0, float(getattr(scene_settings, "terrain_layer_load_radius", 100.0) or 100.0))
    skip_loaded = bool(getattr(scene_settings, "terrain_layer_skip_loaded", True))
    nearby_items, nearby_layers = _query_world_layer_runtime_nearby(index, camera_position, radius, skip_loaded)
    return f"Scan Cache Nearby: {nearby_items:,} importable objects in {nearby_layers:,} layers"


def get_nearby_cache_summary_label(context):
    return _nearby_scan_label


def _refresh_nearby_scan_label(context):
    global _nearby_scan_label
    position = _get_camera_position(context)
    _nearby_scan_label = _compute_nearby_cache_summary_label(context, position)
    window = getattr(context, "window", None) or getattr(bpy.context, "window", None)
    screen = getattr(window, "screen", None) if window is not None else None
    if screen is not None:
        for area in getattr(screen, "areas", []) or []:
            if getattr(area, "type", "") != 'VIEW_3D':
                continue
            for region in area.regions:
                if region.type == 'UI':
                    region.tag_redraw()
                    break


class WITCH_OT_scan_layers_nearby(bpy.types.Operator):
    bl_idname = "witcher.scan_layers_nearby"
    bl_label = "Scan Nearby"
    bl_description = "Count cached importable objects/layers within the configured radius around the current viewport camera"

    def execute(self, context):
        _refresh_nearby_scan_label(context)
        return {'FINISHED'}


def _import_level_from_collection(
    context,
    coll,
    camera_position=None,
    radius=0.0,
    isolation_batch_session=None,
    cached_plan_items=None,
    cancel_check=None,
    import_settings=None,
    mode_signature=None,
):
    level_path = str(coll.get("level_path", "")).strip()
    if not level_path:
        return False, "", "Collection has no level_path", False
    root_collection = _find_world_root_collection_for_collection(coll)
    resolved = _resolve_level_file(context, level_path, root_collection)
    if not resolved:
        return False, "", f"Could not resolve level path: {level_path}", False

    scene_settings = getattr(getattr(context, "scene", None), "witcher_file_browser", None)
    dev_empty_only = False
    import_settings = dict(import_settings or {})
    full_cached_plan = False
    if cached_plan_items is not None and not dev_empty_only:
        full_cached_plan = import_blender_fun.cached_plan_can_use_full_import(
            cached_plan_items,
            camera_position=camera_position,
            radius=radius,
            import_kwargs=import_settings,
            context=context,
        )
    use_fast_path = cached_plan_items is not None and (dev_empty_only or full_cached_plan)

    try:
        import_kwargs = {"_level_target_collection": coll}
        import_kwargs.update(import_settings)
        if mode_signature:
            import_kwargs["_layer_import_mode_signature"] = str(mode_signature)
        if dev_empty_only:
            import_kwargs["_dev_empty_only"] = True
        if callable(cancel_check):
            import_kwargs["_cancel_check"] = cancel_check
        try:
            radius_value = float(radius or 0.0)
        except Exception:
            radius_value = 0.0
        if camera_position is not None and radius_value > 0.0:
            import_kwargs["_nearby_camera_position"] = tuple(camera_position)
            import_kwargs["_nearby_radius"] = radius_value

        scope_label = Path(level_path).stem or coll.name or "Level"
        use_isolation_scope = (
            isolation_batch_session is not None
            and getattr(isolation_batch_session, "isolated", False)
        )

        if use_fast_path:
            if use_isolation_scope:
                with import_isolation.isolated_batch_import_target(
                    isolation_batch_session,
                    coll,
                    label=scope_label,
                ) as scope:
                    import_blender_fun.loadLevelFromCachedPlan(
                        resolved,
                        cached_plan_items,
                        context=scope.context,
                        **import_kwargs,
                    )
            else:
                import_blender_fun.loadLevelFromCachedPlan(
                    resolved,
                    cached_plan_items,
                    context=context,
                    **import_kwargs,
                )
        else:
            level_file = CR2W.CR2W_reader.load_w2l(resolved)
            if use_isolation_scope:
                with import_isolation.isolated_batch_import_target(
                    isolation_batch_session,
                    coll,
                    label=scope_label,
                ) as scope:
                    import_w2l.btn_import_W2L(level_file, context=scope.context, **import_kwargs)
            else:
                import_w2l.btn_import_W2L(level_file, context=context, **import_kwargs)
    except import_blender_fun.LayerImportCancelled as e:
        return False, resolved, str(e) or "Cancelled by user", True
    except Exception as e:
        return False, resolved, str(e), False
    return True, resolved, "", False


def import_group(context, coll, stats):
    for child in coll.children:
        child_group_type = str(child.get("group_type", "")).strip()
        if child_group_type == "LayerInfo":
            log.info("LOADING LEVEL %s", child.name)
            ok, resolved, err, _cancelled = _import_level_from_collection(context, child)
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
        elif child_group_type == "LayerGroup":
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
        ok, resolved, err, _cancelled = _import_level_from_collection(context, coll)
        if not ok:
            self.report({'ERROR'}, err or "Failed to load level")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Loaded level: {Path(resolved).name}")
        return {'FINISHED'}


class WITCH_OT_cancel_layer_stream_job(bpy.types.Operator):
    bl_idname = "witcher.cancel_layer_stream_job"
    bl_label = "Cancel Layer Scan/Load"
    bl_description = "Cancel the currently running terrain layer scan/load job"

    def execute(self, context):
        if not layer_stream_job_running():
            return {'CANCELLED'}
        _LAYER_STREAM_JOB["cancel_requested"] = True
        _tag_layer_stream_redraw(context, wm=getattr(context, "window_manager", None))
        return {'FINISHED'}


class WITCH_OT_rebuild_layer_scan_cache(bpy.types.Operator):
    bl_idname = "witcher.rebuild_layer_scan_cache"
    bl_label = "Rebuild Layer Scan Cache"
    bl_description = "Rebuild the cached world-layer bounds and nearby import manifest used by load-around-camera"

    def execute(self, context):
        if layer_stream_job_running():
            self.report({'WARNING'}, "A terrain layer scan/load job is already running")
            return {'CANCELLED'}
        root_collection = _find_world_root_collection(context)
        if root_collection is None:
            self.report({'WARNING'}, "Select imported terrain or a world layer collection first")
            return {'CANCELLED'}
        if getattr(context, "window", None) is None:
            self.report({'ERROR'}, "This operator must be started from a Blender window")
            return {'CANCELLED'}

        _LEVEL_FILE_CACHE.clear()
        _clear_world_layer_index_cache(context, root_collection)
        job = _start_layer_stream_job(context, "scan_cache", root_collection)
        _start_layer_scan_phase(job, context, root_collection, rebuild=True, title="Scanning world layers")
        if int(job.get("total", 0) or 0) <= 0:
            _finalize_layer_scan_index(job, root_collection)
            return _finish_layer_stream_job(self, context)

        wm = context.window_manager
        job["wm"] = wm
        job["timer"] = wm.event_timer_add(0.01, window=context.window)
        wm.modal_handler_add(self)
        _tag_layer_stream_redraw(context, wm=wm)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type == 'ESC':
            _LAYER_STREAM_JOB["cancel_requested"] = True
            return {'RUNNING_MODAL'}
        if event.type != 'TIMER':
            return {'PASS_THROUGH'}
        if not layer_stream_job_running():
            return _finish_layer_stream_job(self, context)
        if _LAYER_STREAM_JOB.get("cancel_requested"):
            return _finish_layer_stream_job(self, context, cancelled=True)
        try:
            if _process_layer_scan_batch(_LAYER_STREAM_JOB, context):
                return _finish_layer_stream_job(self, context)
        except Exception as exc:
            log.exception("Terrain layer scan job failed")
            _LAYER_STREAM_JOB["error"] = str(exc)
            return _finish_layer_stream_job(self, context, failed=True)
        _maybe_tag_layer_stream_redraw(context, wm=getattr(context, "window_manager", None))
        return {'RUNNING_MODAL'}


class WITCH_OT_load_layers_around_camera(bpy.types.Operator):
    bl_idname = "witcher.load_layers_around_camera"
    bl_label = "Load Layers Around Camera"
    bl_description = "Load nearby world layers based on cached .w2l bounds and the current viewport camera"

    def execute(self, context):
        execute_started = time.perf_counter()
        hydrate_seconds = 0.0
        prepare_seconds = 0.0
        index_source = "memory"
        if layer_stream_job_running():
            self.report({'WARNING'}, "A terrain layer scan/load job is already running")
            return {'CANCELLED'}
        root_collection = _find_world_root_collection(context)
        if root_collection is None:
            self.report({'WARNING'}, "Select imported terrain or a world layer collection first")
            return {'CANCELLED'}

        camera_position = _get_camera_position(context)
        if camera_position is None:
            self.report({'WARNING'}, "Could not determine a viewport or scene camera position")
            return {'CANCELLED'}
        if getattr(context, "window", None) is None:
            self.report({'ERROR'}, "This operator must be started from a Blender window")
            return {'CANCELLED'}

        scene_settings = getattr(context.scene, "witcher_file_browser", None)
        radius = max(1.0, float(getattr(scene_settings, "terrain_layer_load_radius", 100.0)))
        load_limit = max(0, int(getattr(scene_settings, "terrain_layer_max_load_count", 0)))
        skip_loaded = bool(getattr(scene_settings, "terrain_layer_skip_loaded", True))
        write_profile_log = bool(getattr(scene_settings, "terrain_layer_write_profile_log", False))
        import_kwargs = _layer_import_kwargs_from_scene(scene_settings)
        import_filter_active = _layer_import_query_filter_active(scene_settings)

        job = _start_layer_stream_job(context, "load_nearby", root_collection)
        job["radius"] = radius
        job["load_limit"] = load_limit
        job["skip_complete"] = skip_loaded
        job["camera_position"] = camera_position
        job["mode_signature"] = _layer_load_mode_signature_for_scene(scene_settings)
        job["import_kwargs"] = import_kwargs
        job["import_filter_kwargs"] = import_kwargs
        job["import_filter_active"] = import_filter_active
        job["hide_volume_meshes"] = bool(getattr(scene_settings, "terrain_layer_hide_volume_meshes", False))
        job["hide_shadow_meshes"] = bool(getattr(scene_settings, "terrain_layer_hide_shadow_meshes", False))
        job["nearby_load_started_at"] = execute_started
        _log_layer_load_timing_warning(
            "settings radius %.1f limit %d skip_complete %s mesh %s proxy_mesh %s collision %s rigid %s entity %s point %s spot %s redcloth %s lods %s empty_lods %s keep_proxy_lods %s regex_enabled %s regex '%s'",
            radius,
            load_limit,
            bool(skip_loaded),
            bool(import_kwargs.get("do_import_Mesh", True)),
            bool(import_kwargs.get("do_import_ProxyMesh", False)),
            bool(import_kwargs.get("do_import_Collision", True)),
            bool(import_kwargs.get("do_import_RigidBody", True)),
            bool(import_kwargs.get("do_import_Entity", True)),
            bool(import_kwargs.get("do_import_PointLight", True)),
            bool(import_kwargs.get("do_import_SpotLight", True)),
            bool(import_kwargs.get("do_import_Redcloth", False)),
            bool(import_kwargs.get("keep_lod_meshes", False)),
            bool(import_kwargs.get("keep_empty_lods", False)),
            bool(import_kwargs.get("keep_proxy_meshes", True)),
            bool(import_kwargs.get("do_enable_name_filter", False)),
            str(import_kwargs.get("do_name_filter_regex", "") or ""),
        )
        if write_profile_log:
            _start_map_import_profile_log(job, root_collection)

        cache_key = _world_layer_cache_key(context, root_collection)
        cached_index = _WORLD_LAYER_INDEX_CACHE.get(cache_key)
        if cached_index is None:
            hydrate_started = time.perf_counter()
            cached_index = _hydrate_world_layer_index_from_disk(context, root_collection)
            hydrate_seconds = time.perf_counter() - hydrate_started
            index_source = "disk" if cached_index is not None else "scan"
        if cached_index is not None:
            job["index_data"] = cached_index
            prepare_started = time.perf_counter()
            if not _prepare_layer_load_phase(job, context):
                prepare_seconds = time.perf_counter() - prepare_started
                _log_layer_load_timing_warning(
                    "operator execute total %s (index %s %s, prepare %s)",
                    _format_layer_scan_timing(time.perf_counter() - execute_started),
                    index_source,
                    _format_layer_scan_timing(hydrate_seconds),
                    _format_layer_scan_timing(prepare_seconds),
                )
                return _finish_layer_stream_job(self, context)
            prepare_seconds = time.perf_counter() - prepare_started
        else:
            _start_layer_scan_phase(job, context, root_collection, rebuild=False, title="Checking layer scan cache")
            index_source = "scan"
            if int(job.get("total", 0) or 0) <= 0:
                _finalize_layer_scan_index(job, root_collection)
                prepare_started = time.perf_counter()
                if not _prepare_layer_load_phase(job, context):
                    prepare_seconds = time.perf_counter() - prepare_started
                    _log_layer_load_timing_warning(
                        "operator execute total %s (index %s %s, prepare %s)",
                        _format_layer_scan_timing(time.perf_counter() - execute_started),
                        index_source,
                        _format_layer_scan_timing(hydrate_seconds),
                        _format_layer_scan_timing(prepare_seconds),
                    )
                    return _finish_layer_stream_job(self, context)
                prepare_seconds = time.perf_counter() - prepare_started

        wm = context.window_manager
        job["wm"] = wm
        job["timer"] = wm.event_timer_add(0.01, window=context.window)
        wm.modal_handler_add(self)
        _tag_layer_stream_redraw(context, wm=wm)
        _log_layer_load_timing_warning(
            "operator execute total %s (index %s %s, prepare %s)",
            _format_layer_scan_timing(time.perf_counter() - execute_started),
            index_source,
            _format_layer_scan_timing(hydrate_seconds),
            _format_layer_scan_timing(prepare_seconds),
        )
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type == 'ESC':
            _LAYER_STREAM_JOB["cancel_requested"] = True
            return {'RUNNING_MODAL'}
        if event.type != 'TIMER':
            return {'PASS_THROUGH'}
        if not layer_stream_job_running():
            return _finish_layer_stream_job(self, context)
        if _LAYER_STREAM_JOB.get("cancel_requested"):
            return _finish_layer_stream_job(self, context, cancelled=True)
        try:
            if _LAYER_STREAM_JOB.get("phase") == "scan":
                if _process_layer_scan_batch(_LAYER_STREAM_JOB, context):
                    if not _prepare_layer_load_phase(_LAYER_STREAM_JOB, context):
                        return _finish_layer_stream_job(self, context)
            elif _LAYER_STREAM_JOB.get("phase") == "load":
                if _process_layer_load_batch(_LAYER_STREAM_JOB, context):
                    return _finish_layer_stream_job(self, context)
            else:
                return _finish_layer_stream_job(self, context)
        except Exception as exc:
            log.exception("Terrain nearby layer load job failed")
            _LAYER_STREAM_JOB["error"] = str(exc)
            return _finish_layer_stream_job(self, context, failed=True)
        _maybe_tag_layer_stream_redraw(context, wm=getattr(context, "window_manager", None))
        return {'RUNNING_MODAL'}
