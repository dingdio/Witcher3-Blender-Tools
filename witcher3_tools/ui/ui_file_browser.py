import logging
import addon_utils

from ..importers import import_mesh

log = logging.getLogger(__name__)
from ..importers import terrain_w2ter
from ..CR2W.witcher_cache.TextureCache.TextureCacheItem import TextureCacheItem
from ..CR2W.common_blender import (
    repo_file,
    win_safe_path,
    win_path_exists,
    win_path_isdir,
    win_path_getsize,
    win_path_getmtime,
    bpy_image_load_safe,
    set_repo_override_roots,
    clear_repo_override_roots,
    mod_loading_context,
    get_mod_override_name,
    get_source_for_path,
    set_source_for_path,
    prepare_extraction_target,
    clear_mod_index_cache,
)

from .. import (
    file_helpers,
    clear_external_import_dependency_alert,
    get_all_addon_prefs,
    get_game_path,
    get_texture_path,
    get_W3_OGG_PATH,
    get_vgmstream_path,
    set_external_import_dependency_alert,
    get_uncook_path,
    get_witcher2_game_path,
)
from ..importers import import_entity
from ..mesh_import_settings import MeshImportSettings
from ..ui.blender_fun import convert_xbm_to_dds, convert_w2cube_to_dds, load_image_with_dds_repair
from .browser_dummy_icons import (
    clear_browser_dummy_icon_cache,
    ensure_browser_dummy_icon_path,
    get_browser_item_type_label,
)
from . import asset_browser_import_bridge
from bpy.props import IntProperty, StringProperty


def _update_hide_default_hidden(self, context):
    try:
        from ..ui import ui_map
        ui_map.apply_default_hidden_layer_groups(
            context,
            bool(self.terrain_layer_hide_default_hidden),
            bool(getattr(self, "terrain_layer_solo_default_hidden", False)),
        )
    except Exception:
        pass


def _update_layer_visibility_settings(self, context):
    try:
        from ..ui import ui_map
        ui_map.apply_layer_visibility_settings(context, self)
    except Exception:
        pass
from ..importers.import_entity import test_load_entity, fixed_chunk_paths
import os
import numpy as np
import struct
import zlib
from typing import Dict, Tuple, Optional

##############
# ASSET BROWSER

###############
import bpy
from bpy.props import StringProperty, CollectionProperty, BoolProperty, PointerProperty, FloatProperty, EnumProperty
from bpy.types import Operator, Panel, UIList, PropertyGroup, Scene
from bpy_extras.io_utils import ImportHelper
from pathlib import Path
import re


from ..CR2W.witcher_cache.Bundles import LoadBundleManager
from ..CR2W.witcher_cache.Bundles.BundleItem import BundleItem
from ..CR2W.witcher_cache.Bundles.Bundle import Bundle
from ..CR2W.witcher_cache.TextureCache import LoadTextureManager
from ..CR2W.witcher_cache.TextureCache.TextureCache import TextureCache
from ..CR2W.witcher_cache.CollisionCache import LoadCollisionManager
from ..CR2W.witcher_cache.CollisionCache.Collision_Cache import CollisionCache
from ..CR2W.witcher_cache.SoundCache import LoadSoundManager
from ..CR2W.witcher_cache.SoundCache.SoundCache import SoundCache
from ..CR2W.witcher_cache.SoundCache.SoundBanksInfo import SoundBanksInfoXML
from ..CR2W.witcher_cache.SoundCache.SoundManager import _soundbanks_metadata_path
from ..CR2W.witcher_cache.Speech import LoadSpeechManager
from ..external_addon_tools import ensure_apx_from_apb, get_apx_addon_status, get_srt_addon_status


def _legacy_apx_addon_enabled() -> bool:
    try:
        _exists, enabled = addon_utils.check("io_scene_apx")
        return bool(enabled)
    except Exception:
        return False

class FileItem(PropertyGroup):
    path: StringProperty()
    display_name: StringProperty()
    is_folder: BoolProperty()


class RecentItem(PropertyGroup):
    """Tracks recent browser activity"""
    path: StringProperty(name="Path")
    cache_type: StringProperty(name="Cache Type")
    timestamp: bpy.props.FloatProperty(name="Timestamp")


class BookmarkItem(PropertyGroup):
    """User-defined bookmark/favorite"""
    path: StringProperty(name="Path")
    name: StringProperty(name="Display Name")
    cache_type: StringProperty(name="Cache Type")
from collections import defaultdict

class FolderStructure:
    def __init__(self):
        self.items = {}
        self.index = {}
        self.cache_type = ""  # Track which cache this structure is for

    def clear(self):
        """Clear the folder structure for reloading a different cache."""
        self.items = {}
        self.index = {}
        self.cache_type = ""

    def add_path(self, path):
        parts = path.split("\\")
        current_level = self.items
        normalized_path = path.lower().replace('_', ' ')
        self.index[normalized_path] = path # todo improve index
        for i, part in enumerate(parts):
            if part not in current_level:
                current_level[part] = {}
            current_level = current_level[part]

    def get_items(self, current_path=""):
        items = []
        current_level = self.items
        if current_path:
            for part in current_path.split("\\"):
                current_level = current_level.get(part, {})
        sorted_items = sorted(current_level.items(), key=lambda x: (not bool(x[1]), x[0]))
        for name, subitems in sorted_items:
            item = {"name": name, "is_folder": bool(subitems)}
            items.append(item)
        return items
    
    def search_items(self, query, max_results=100):
        """Search with result limit for performance."""
        # Normalize query -> lowercase, spaces for underscores
        tokens = query.lower().replace('_', ' ').split()
        results = []
        for key, original_path in self.index.items():
            if all(token in key for token in tokens):
                results.append(original_path)
                if len(results) >= max_results:
                    break
        return results

    def path_exists(self, path):
        """Check if a path (folder or file) exists in the structure."""
        if not path:
            return True  # Root always exists
        parts = path.split("\\")
        current_level = self.items
        for part in parts:
            if part not in current_level:
                return False
            current_level = current_level[part]
        return True

    def get_parent_folder(self, path):
        """Get the parent folder of a path, or empty string if at root."""
        if not path or "\\" not in path:
            return ""
        return "\\".join(path.split("\\")[:-1])

def _sync_path_to_address_bar(self, context):
    """Keep the address bar in sync when folder navigation changes."""
    if self.path_input != self.current_folder:
        self.path_input = self.current_folder
    self.file_page_index = 0


def _on_browser_query_filter_update(self, context):
    self.file_page_index = 0


def _on_browser_grid_layout_update(self, context):
    self.file_page_index = 0


def _on_external_extract_root_update(self, context):
    cache_type = str(getattr(self, "active_cache_type", "") or "")
    if not is_external_cache(cache_type):
        return
    session = get_external_archive_session(cache_type)
    if session:
        archive_path = win_safe_path(bpy.path.abspath(session.get("archive_path", "") or ""))
        if archive_path:
            self.external_extract_root_archive_path = archive_path


class MySettings(PropertyGroup):
    current_folder: StringProperty(update=_sync_path_to_address_bar)
    search_query: StringProperty(
        name="Search",
        description=(
            "Search asset paths. At Home it searches across all cache types; "
            "inside a cache it searches the current active cache"
        ),
        update=_on_browser_query_filter_update,
    )
    active_cache_type: StringProperty(default="")  # "", "Bundle", "Collision", "Texture", "Sound", "Speech"
    loadmods: BoolProperty(default=False)  # Persist loadmods from browser invocation
    use_mods_priority: BoolProperty(
        default=False,
        description="Prefer installed mods over vanilla (off = mods only if vanilla missing)"
    )
    mods_overwrite: BoolProperty(
        default=False,
        description="Overwrite existing extracted files (moves previous to backup)"
    )
    open_import_dialog: BoolProperty(
        default=False,
        description="Open the matching importer dialog for single-file imports when available"
    )
    external_extract_root: StringProperty(
        name="Standalone Export Folder",
        description="Folder used for extracting and importing files from standalone archives opened in the asset browser",
        default="",
        subtype='DIR_PATH',
        update=_on_external_extract_root_update,
    )
    external_extract_root_archive_path: StringProperty(
        default="",
        options={'HIDDEN', 'SKIP_SAVE'},
    )
    external_texture_export_format: EnumProperty(
        name="Texture Export Format",
        description="Output format used when exporting textures from standalone texture archives",
        items=[
            ('DDS', 'DDS', 'Extract textures as DDS'),
            ('PNG', 'PNG', 'Extract textures as PNG'),
            ('TGA', 'TGA', 'Extract textures as TGA'),
        ],
        default='DDS',
    )
    external_export_ui_ping: IntProperty(
        default=0,
        options={'HIDDEN', 'SKIP_SAVE'},
    )
    preview_texture_path: StringProperty(default="")  # For texture preview popup
    revealed_file_path: StringProperty(default="", options={'HIDDEN', 'SKIP_SAVE'})
    # Phase 1 QoL
    extension_filter: StringProperty(
        name="View Filter",
        default="",
        description="Search/filter items in the current browser view by name or extension",
        update=_on_browser_query_filter_update,
    )
    # Phase 2 QoL
    path_input: StringProperty(default="", description="Navigate to path")
    file_page_index: IntProperty(default=0, min=0, options={'HIDDEN', 'SKIP_SAVE'})
    file_display_mode: EnumProperty(
        name="Layout",
        description="Display files as a list or icon grid",
        items=[
            ('LIST', 'List', 'Show files as rows', 'SHORTDISPLAY', 0),
            ('GRID', 'Grid', 'Show files as icon tiles', 'IMGDISPLAY', 1),
        ],
        default='LIST',
    )
    grid_size_mode_live: EnumProperty(
        name="Icon Size",
        description="Grid tile size preset for the current browser window",
        items=[
            ('SMALL', 'Small', 'Compact grid tiles with more columns', 'ALIGN_JUSTIFY', 0),
            ('MEDIUM', 'Medium', 'Balanced grid tiles', 'IMGDISPLAY', 1),
            ('LARGE', 'Large', 'Larger grid tiles with wider filename rows', 'FULLSCREEN_ENTER', 2),
        ],
        default='MEDIUM',
        options={'SKIP_SAVE'},
        update=_on_browser_grid_layout_update,
    )
    sort_by: EnumProperty(
        name="Sort",
        description="Sort order for files in the browser",
        items=[
            ('NAME', 'Name', 'Sort by file name', 'SORTALPHA', 0),
            ('EXT', 'Extension', 'Sort by file extension then name', 'FILTER', 1),
        ],
        default='NAME',
        update=_on_browser_grid_layout_update,
    )
    sort_ascending: BoolProperty(
        name="Ascending",
        description="Sort ascending (A→Z). Disable for descending (Z→A)",
        default=True,
        update=_on_browser_grid_layout_update,
    )

    debug_wrap_show: BoolProperty(
        name="Wrap Tuning",
        description="Show the filename-wrap debug sliders (dev mode only)",
        default=False,
    )
    debug_small_padding_px: bpy.props.IntProperty(name="Padding", default=80, min=0, max=160, update=_on_browser_grid_layout_update)
    debug_small_avg_char_px: bpy.props.FloatProperty(name="Avg Char px", default=6.0, min=3.0, max=24.0, precision=1, step=10, update=_on_browser_grid_layout_update)
    debug_small_chars_min: bpy.props.IntProperty(name="Chars Min", default=9, min=1, max=60, update=_on_browser_grid_layout_update)
    debug_small_chars_max: bpy.props.IntProperty(name="Chars Max", default=31, min=1, max=60, update=_on_browser_grid_layout_update)
    debug_medium_padding_px: bpy.props.IntProperty(name="Padding", default=80, min=0, max=160, update=_on_browser_grid_layout_update)
    debug_medium_avg_char_px: bpy.props.FloatProperty(name="Avg Char px", default=6.0, min=3.0, max=24.0, precision=1, step=10, update=_on_browser_grid_layout_update)
    debug_medium_chars_min: bpy.props.IntProperty(name="Chars Min", default=1, min=1, max=60, update=_on_browser_grid_layout_update)
    debug_medium_chars_max: bpy.props.IntProperty(name="Chars Max", default=17, min=1, max=60, update=_on_browser_grid_layout_update)
    debug_large_padding_px: bpy.props.IntProperty(name="Padding", default=80, min=0, max=160, update=_on_browser_grid_layout_update)
    debug_large_avg_char_px: bpy.props.FloatProperty(name="Avg Char px", default=6.0, min=3.0, max=24.0, precision=1, step=10, update=_on_browser_grid_layout_update)
    debug_large_chars_min: bpy.props.IntProperty(name="Chars Min", default=1, min=1, max=60, update=_on_browser_grid_layout_update)
    debug_large_chars_max: bpy.props.IntProperty(name="Chars Max", default=34, min=1, max=60, update=_on_browser_grid_layout_update)

    # Phase 4 QoL - view mode
    browser_view_mode: bpy.props.EnumProperty(
        name="View",
        items=[
            ('BROWSE', 'Browse', 'Browse files', 'FILE_FOLDER', 0),
            ('RECENT', 'Recent', 'Recently imported files', 'TIME', 1),
            ('BOOKMARKS', 'Bookmarks', 'Bookmarked paths', 'BOOKMARKS', 2),
        ],
        default='BROWSE'
    )
    # Batch selection tracking
    batch_select_mode: BoolProperty(default=False, description="Enable batch selection mode")
    # Terrain tile import
    terrain_multires_level: IntProperty(
        name="Terrain Multires",
        description="Multires subdivision levels for imported terrain tiles",
        default=10, min=0, max=10,
    )
    terrain_import_mode: bpy.props.EnumProperty(
        name="Terrain Import",
        description="Choose how terrain is imported from .w2ter tiles",
        items=[
            ('FULL_MAP', 'Full Map', 'Import a single combined map using Geometry Nodes + Multires'),
            ('TILES', 'Tiles', 'Import individual terrain tile meshes'),
        ],
        default='FULL_MAP',
    )
    terrain_material_roughness: FloatProperty(
        name="Terrain Roughness",
        description="Roughness applied to imported terrain materials",
        default=0.82,
        min=0.0,
        max=1.0,
    )
    terrain_material_specular: FloatProperty(
        name="Terrain Specular",
        description="Specular amount applied to imported terrain materials",
        default=0.12,
        min=0.0,
        max=1.0,
    )
    terrain_layer_load_radius: FloatProperty(
        name="Layer Load Radius",
        description="Load world layers whose cached bounds intersect this distance around the current viewport camera, measured in imported world/Blender units",
        default=100.0,
        min=1.0,
        soft_max=5000.0,
    )
    foliage_load_radius: FloatProperty(
        name="Foliage Load Radius",
        description="Radius around the viewport camera (in world units) to search for foliage cells to load",
        default=200.0,
        min=1.0,
        soft_max=2000.0,
    )
    terrain_layer_max_load_count: IntProperty(
        name="Layer Load Limit",
        description="Safety limit for nearby layer imports in one pass; set to 0 to load every matching layer",
        default=0,
        min=0,
        soft_max=512,
    )
    terrain_layer_skip_loaded: BoolProperty(
        name="Skip Complete Layers",
        description="Skip only layer collections that previously finished importing cleanly; partial or failed imports are still eligible",
        default=True,
    )
    terrain_layer_do_import_mesh: BoolProperty(
        name="Mesh",
        description="Import static mesh",
        default=True,
    )
    terrain_layer_do_import_proxy_mesh: BoolProperty(
        name="Proxy Mesh",
        description="Import proxy mesh items from nearby layers",
        default=True,
    )
    terrain_layer_do_import_collision: BoolProperty(
        name="Collision",
        description="Import collision mesh items from nearby layers",
        default=True,
    )
    terrain_layer_do_import_rigidbody: BoolProperty(
        name="Rigid Body",
        description="Import rigid-body mesh items from nearby layers",
        default=True,
    )
    terrain_layer_do_import_entity: BoolProperty(
        name="Entity",
        description="Import entity contents from nearby layers",
        default=True,
    )
    terrain_layer_do_import_point_light: BoolProperty(
        name="Point Lights",
        description="Import point lights from nearby layers",
        default=True,
    )
    terrain_layer_do_import_spot_light: BoolProperty(
        name="Spot Lights",
        description="Import spot lights from nearby layers",
        default=True,
    )
    terrain_layer_do_import_redcloth: BoolProperty(
        name="Redcloth",
        description="Import cloth simulation assets",
        default=True,
    )
    terrain_layer_do_import_redapex: BoolProperty(
        name="Redapex",
        description="Import Apex destruction resources from nearby layers",
        default=True,
    )
    terrain_layer_redapex_import_chunks: BoolProperty(
        name="Redapex Chunks",
        description="Keep destructible chunk meshes when importing redapex resources",
        default=False,
    )
    terrain_layer_redapex_import_floor: BoolProperty(
        name="Redapex Floor",
        description="Keep the helper floor plane created by the APX destruction importer",
        default=False,
    )
    terrain_layer_redapex_collections_as_empties: BoolProperty(
        name="Redapex Collections as Empties",
        description="Flatten APX importer collections into Blender empties/objects",
        default=True,
    )
    terrain_layer_instanced_sector: BoolProperty(
        name="Instance Repeated Meshes",
        description="Group identical CSectorData mesh placements into a single instancer object instead of one object per placement. Greatly reduces object count for dense static layers (rocks, cliffs, debris). Individual selection is not possible for instanced meshes.",
        default=False,
    )
    terrain_layer_keep_lod_meshes: BoolProperty(
        name="Keep LODs",
        description="Keep lower-detail mesh LODs when importing nearby layers",
        default=False,
    )
    terrain_layer_keep_empty_lods: BoolProperty(
        name="Keep Empty LODs",
        description="Keep empty mesh LODs with zero polygons when importing nearby layers",
        default=False,
    )
    terrain_layer_keep_proxy_meshes: BoolProperty(
        name="Keep Proxy Meshes",
        description="Keep proxy meshes even when higher LOD meshes are skipped",
        default=True,
    )
    terrain_layer_enable_name_filter: BoolProperty(
        name="Enable Regex Filter",
        description="Only import nearby layer items whose name or source path matches the regex",
        default=False,
    )
    terrain_layer_name_filter_regex: StringProperty(
        name="Regex Filter",
        description="Regex applied to layer item names and source paths",
        default="_proxy",
    )
    terrain_layer_hide_volume_meshes: BoolProperty(
        name="Hide Volume Meshes",
        description="Hide imported layer mesh objects identified as volume helpers, including names containing _volume_",
        default=False,
        update=lambda self, context: _update_layer_visibility_settings(self, context),
    )
    terrain_layer_solo_volume_meshes: BoolProperty(
        name="Solo Volume Meshes",
        description="Show imported layer volume helper meshes and hide other imported layer objects",
        default=False,
        update=lambda self, context: _update_layer_visibility_settings(self, context),
    )
    terrain_layer_hide_shadow_meshes: BoolProperty(
        name="Hide Shadow Meshes",
        description="Hide imported layer mesh objects identified as shadow helpers, including names containing _shadow_ or shadowmesh",
        default=False,
        update=lambda self, context: _update_layer_visibility_settings(self, context),
    )
    terrain_layer_solo_shadow_meshes: BoolProperty(
        name="Solo Shadow Meshes",
        description="Show imported layer shadow helper meshes and hide other imported layer objects",
        default=False,
        update=lambda self, context: _update_layer_visibility_settings(self, context),
    )
    terrain_layer_hide_collision: BoolProperty(
        name="Hide Collision",
        description="Hide collision objects imported from W2L/W2W layer collision resources while keeping them in the scene",
        default=False,
        update=lambda self, context: _update_layer_visibility_settings(self, context),
    )
    terrain_layer_solo_collision: BoolProperty(
        name="Solo Collision",
        description="Show imported layer collision objects and hide other imported layer objects",
        default=False,
        update=lambda self, context: _update_layer_visibility_settings(self, context),
    )
    terrain_layer_hide_engine_hidden_meshes: BoolProperty(
        name="Hide Engine-Hidden Meshes",
        description="Hide imported layer meshes whose REDengine visibility is off: CSectorData visible flags or component drawableFlags without DF_IsVisible",
        default=True,
        update=lambda self, context: _update_layer_visibility_settings(self, context),
    )
    terrain_layer_solo_engine_hidden_meshes: BoolProperty(
        name="Solo Engine-Hidden Meshes",
        description="Show only imported layer meshes hidden by REDengine visibility: CSectorData visible flags or component drawableFlags without DF_IsVisible",
        default=False,
        update=lambda self, context: _update_layer_visibility_settings(self, context),
    )
    terrain_layer_hide_proxy_meshes: BoolProperty(
        name="Hide Proxy Meshes",
        description="Hide imported layer proxy mesh objects while keeping them in the scene",
        default=False,
        update=lambda self, context: _update_layer_visibility_settings(self, context),
    )
    terrain_layer_solo_proxy_meshes: BoolProperty(
        name="Solo Proxy Meshes",
        description="Show imported layer proxy meshes and hide non-proxy layer meshes",
        default=False,
        update=lambda self, context: _update_layer_visibility_settings(self, context),
    )
    terrain_layer_hide_redapex: BoolProperty(
        name="Hide Redapex",
        description="Hide imported redapex objects while keeping them in the scene",
        default=False,
        update=lambda self, context: _update_layer_visibility_settings(self, context),
    )
    terrain_layer_solo_redapex: BoolProperty(
        name="Solo Redapex",
        description="Show imported redapex objects and hide other imported layer objects",
        default=False,
        update=lambda self, context: _update_layer_visibility_settings(self, context),
    )
    terrain_layer_write_profile_log: BoolProperty(
        name="Write Profile Log",
        description="Write a timestamped nearby-layer import profile log to the extension cache log folder",
        default=False,
    )
    terrain_layer_hide_default_hidden: BoolProperty(
        name="Hide Default-Hidden Layer Groups",
        description="Hide layer group collections whose isVisibleOnStart flag is off in the game data. Layers are always temporarily shown during any import operation, then this state is restored",
        default=False,
        update=lambda self, context: _update_hide_default_hidden(self, context),
    )
    terrain_layer_solo_default_hidden: BoolProperty(
        name="Solo Default-Hidden Layer Groups",
        description="Show default-hidden layer group collections and hide other layer-group branches where possible",
        default=False,
        update=lambda self, context: _update_hide_default_hidden(self, context),
    )


from ..CR2W.witcher_cache.CacheController import CacheController
folder_structure:FolderStructure = FolderStructure()

# Disk-based cache types (read-only depots/workspaces)
DISK_CACHE_TYPES = {
    "REDkit Depot",
    "REDkit Uncooked",
    "Workspace",
    "Cooked",
    "Witcher 2 Data",
}

EXTERNAL_BUNDLE_CACHE_TYPE = "External Bundle"
EXTERNAL_COLLISION_CACHE_TYPE = "External Collision"
EXTERNAL_TEXTURE_CACHE_TYPE = "External Texture"
EXTERNAL_SOUND_CACHE_TYPE = "External Sound"
EXTERNAL_CACHE_EFFECTIVE_TYPES = {
    EXTERNAL_BUNDLE_CACHE_TYPE: "Bundle",
    EXTERNAL_COLLISION_CACHE_TYPE: "Collision",
    EXTERNAL_TEXTURE_CACHE_TYPE: "Texture",
    EXTERNAL_SOUND_CACHE_TYPE: "Sound",
}
EXTERNAL_CACHE_TYPES = set(EXTERNAL_CACHE_EFFECTIVE_TYPES)

# Maps virtual path -> absolute path + metadata for disk caches
_file_source_map = {}
_file_source_info = {}

# Standalone archive sessions loaded via file picker (not tied to installed game paths)
_external_archive_sessions = {
    EXTERNAL_BUNDLE_CACHE_TYPE: None,
    EXTERNAL_COLLISION_CACHE_TYPE: None,
    EXTERNAL_TEXTURE_CACHE_TYPE: None,
    EXTERNAL_SOUND_CACHE_TYPE: None,
}

_EXTERNAL_BROWSER_EXPORT_JOB = {
    "running": False,
    "cache_type": "",
    "folder_path": "",
    "output_root": "",
    "texture_format": "DDS",
    "overwrite": False,
    "items": [],
    "index": 0,
    "total": 0,
    "done": 0,
    "skipped": 0,
    "errors": 0,
    "cancel_requested": False,
    "timer": None,
    "context": None,
}

_EXTERNAL_BROWSER_EXPORT_STATUS = {
    "visible": False,
    "state": "idle",
    "cache_type": "",
    "folder_path": "",
    "output_root": "",
    "processed": 0,
    "total": 0,
    "done": 0,
    "skipped": 0,
    "errors": 0,
    "message": "",
}

DEFAULT_BROWSER_FILE_PAGE_SIZE = 0
MIN_BROWSER_FILE_PAGE_SIZE = 0
MAX_BROWSER_FILE_PAGE_SIZE = 500
DEFAULT_BROWSER_GRID_MAX_ROWS = 0
MIN_BROWSER_GRID_MAX_ROWS = 0
MAX_BROWSER_GRID_MAX_ROWS = 12
DEFAULT_BROWSER_GRID_COLUMNS = 0
MAX_BROWSER_GRID_COLUMNS = 8
DEFAULT_BROWSER_GRID_SIZE_MODE = "MEDIUM"
BROWSER_GRID_SIZE_PRESETS = {
    "SMALL": {
        "tile_width": 130,
        "icon_scale_multiplier": 0.85,
        "name_chars_min": 9,
        "name_chars_max": 31,
        "name_padding_px": 80,
        "avg_char_px": 6.0,
    },
    "MEDIUM": {
        "tile_width": 200,
        "icon_scale_multiplier": 1.05,
        "name_chars_min": 1,
        "name_chars_max": 17,
        "name_padding_px": 80,
        "avg_char_px": 6.0,
    },
    "LARGE": {
        "tile_width": 290,
        "icon_scale_multiplier": 1.25,
        "name_chars_min": 1,
        "name_chars_max": 34,
        "name_padding_px": 80,
        "avg_char_px": 6.0,
    },
}
DEFAULT_BROWSER_FOLDER_PANEL_WIDTH = 0
MIN_BROWSER_FOLDER_PANEL_WIDTH = 180
MAX_BROWSER_FOLDER_PANEL_WIDTH = 640
DEFAULT_BROWSER_FOLDER_PANEL_WIDTH_PERCENT = 15
MIN_BROWSER_FOLDER_PANEL_WIDTH_PERCENT = 8
MAX_BROWSER_FOLDER_PANEL_WIDTH_PERCENT = 40
DEFAULT_BROWSER_POPUP_WIDTH_PERCENT = 50
MIN_BROWSER_POPUP_WIDTH_PERCENT = 20
MAX_BROWSER_POPUP_WIDTH_PERCENT = 100
BROWSER_POPUP_SAFE_MARGIN_PX = 48
BROWSER_MONITOR_HEIGHT_USAGE = 0.80
BROWSER_LIST_PAGE_ROW_HEIGHT_PX = 28
BROWSER_LIST_PAGE_OVERHEAD_PX = 220
BROWSER_GRID_PAGE_OVERHEAD_PX = 140
BROWSER_GRID_TILE_HEIGHT_RATIO = 1.12
BROWSER_GRID_TILE_HEIGHT_MIN_PX = 164

def is_external_cache(cache_type: str) -> bool:
    return cache_type in EXTERNAL_CACHE_TYPES

def get_effective_cache_type(cache_type: str) -> str:
    return EXTERNAL_CACHE_EFFECTIVE_TYPES.get(cache_type, cache_type)


def get_cache_type_icon(cache_type: str) -> str:
    effective_cache_type = get_effective_cache_type(cache_type)
    return {
        "Bundle": "PACKAGE",
        "Collision": "MESH_CUBE",
        "Texture": "IMAGE_DATA",
        "Sound": "SPEAKER",
        "Speech": "SPEAKER",
        "REDkit Depot": "FILE_FOLDER",
        "REDkit Uncooked": "FILE_FOLDER",
        "Workspace": "FILE_FOLDER",
        "Cooked": "PACKAGE",
        "Witcher 2 Data": "FILE_FOLDER",
    }.get(effective_cache_type, "FILE_FOLDER")


def cache_supports_scene_import(cache_type: str) -> bool:
    return True


def cache_supports_sound_preview(cache_type: str) -> bool:
    return get_effective_cache_type(cache_type) == "Sound"

def get_external_archive_session(cache_type: str):
    if not is_external_cache(cache_type):
        return None
    session = _external_archive_sessions.get(cache_type)
    return session if isinstance(session, dict) else None


def _get_external_archive_items(cache_type: str, item_path: str):
    session = get_external_archive_session(cache_type)
    if not session:
        return None
    return session.get("items", {}).get((item_path or "").replace("/", "\\"))


def _get_external_archive_item(cache_type: str, item_path: str):
    items = _get_external_archive_items(cache_type, item_path)
    return items[-1] if isinstance(items, list) and items else items

def set_external_archive_session(cache_type: str, archive_path: str, items: dict, collision_exts: Optional[dict] = None):
    if cache_type not in _external_archive_sessions:
        return
    _external_archive_sessions[cache_type] = {
        "archive_path": archive_path,
        "items": items or {},
        "collision_exts": collision_exts or {},
    }

def clear_external_archive_session(cache_type: Optional[str] = None):
    global _external_archive_sessions
    if cache_type is None:
        for key in list(_external_archive_sessions.keys()):
            _external_archive_sessions[key] = None
        return
    if cache_type in _external_archive_sessions:
        _external_archive_sessions[cache_type] = None


def _normalize_virtual_path(path: str) -> str:
    return str(path or "").replace("/", "\\").strip("\\")


def _normalize_real_path(path: str) -> str:
    return win_safe_path(bpy.path.abspath(path or ""))


def _paths_match(path_a: str, path_b: str) -> bool:
    norm_a = _normalize_real_path(path_a).lower()
    norm_b = _normalize_real_path(path_b).lower()
    return bool(norm_a and norm_b and norm_a == norm_b)


def _has_matching_external_archive_session(cache_type: str, archive_path: str = "") -> bool:
    session = get_external_archive_session(cache_type)
    if not session:
        return False
    if not archive_path:
        return True
    return _paths_match(session.get("archive_path", "") or "", archive_path)


def _can_restore_external_archive(cache_type: str, archive_path: str = "") -> bool:
    if not is_external_cache(cache_type):
        return False
    if _has_matching_external_archive_session(cache_type, archive_path):
        return True
    archive_path = _normalize_real_path(archive_path)
    return bool(archive_path and os.path.isfile(archive_path))


def _clear_browser_reveal(context):
    if context and hasattr(context.scene, "witcher_file_browser"):
        context.scene.witcher_file_browser.revealed_file_path = ""


def _set_browser_reveal(context, file_path: str):
    if context and hasattr(context.scene, "witcher_file_browser"):
        context.scene.witcher_file_browser.revealed_file_path = _normalize_virtual_path(file_path)


def _is_revealed_browser_item(browser, file_path: str) -> bool:
    revealed = _normalize_virtual_path(getattr(browser, "revealed_file_path", "") or "")
    candidate = _normalize_virtual_path(file_path)
    return bool(revealed and candidate and revealed.lower() == candidate.lower())


def _clamp_browser_file_page_size(value) -> int:
    try:
        size = int(value)
    except Exception:
        size = DEFAULT_BROWSER_FILE_PAGE_SIZE
    if size <= 0:
        return 0
    return max(10, min(MAX_BROWSER_FILE_PAGE_SIZE, size))


def _clamp_browser_grid_max_rows(value) -> int:
    try:
        size = int(value)
    except Exception:
        size = DEFAULT_BROWSER_GRID_MAX_ROWS
    if size <= 0:
        return 0
    return max(2, min(MAX_BROWSER_GRID_MAX_ROWS, size))


def _clamp_browser_grid_columns(value) -> int:
    try:
        size = int(value)
    except Exception:
        size = DEFAULT_BROWSER_GRID_COLUMNS
    return max(0, min(MAX_BROWSER_GRID_COLUMNS, size))


def _clamp_browser_folder_panel_width(value) -> int:
    try:
        size = int(value)
    except Exception:
        size = DEFAULT_BROWSER_FOLDER_PANEL_WIDTH
    return max(MIN_BROWSER_FOLDER_PANEL_WIDTH, min(MAX_BROWSER_FOLDER_PANEL_WIDTH, size))


def _clamp_browser_folder_panel_width_percent(value) -> int:
    try:
        size = int(value)
    except Exception:
        size = DEFAULT_BROWSER_FOLDER_PANEL_WIDTH_PERCENT
    return max(
        MIN_BROWSER_FOLDER_PANEL_WIDTH_PERCENT,
        min(MAX_BROWSER_FOLDER_PANEL_WIDTH_PERCENT, size),
    )


def _get_browser_file_page_size(context, display_mode: str = "LIST") -> int:
    display_mode = str(display_mode or "LIST").upper()
    try:
        addon_prefs = get_all_addon_prefs(context)
        if addon_prefs is not None:
            if display_mode == "GRID":
                rows_override = _clamp_browser_grid_max_rows(
                    getattr(addon_prefs, "browser_grid_max_rows", DEFAULT_BROWSER_GRID_MAX_ROWS)
                )
                rows = rows_override if rows_override > 0 else _estimate_browser_auto_grid_max_rows(context)
                return max(1, _get_browser_grid_columns(context) * rows)
            page_size_override = _clamp_browser_file_page_size(
                getattr(addon_prefs, "browser_file_page_size", DEFAULT_BROWSER_FILE_PAGE_SIZE)
            )
            return page_size_override if page_size_override > 0 else _estimate_browser_auto_list_page_size(context)
    except Exception:
        pass
    if display_mode == "GRID":
        return max(1, _get_browser_grid_columns(context) * _estimate_browser_auto_grid_max_rows(context))
    return _estimate_browser_auto_list_page_size(context)


def _get_browser_ui_scale(context) -> float:
    try:
        system_prefs = getattr(getattr(context, "preferences", None), "system", None)
        ui_scale = float(getattr(system_prefs, "ui_scale", 1.0) or 1.0)
        pixel_size = float(getattr(system_prefs, "pixel_size", 1.0) or 1.0)
        scale = ui_scale * pixel_size
    except Exception:
        scale = 1.0
    return max(0.5, min(4.0, scale))


def _get_browser_monitor_height_budget_px(context) -> int:
    _monitor_width, monitor_height = _get_context_monitor_size_px(context)
    if monitor_height <= 0:
        monitor_height = 1080
    return max(320, int(round(float(monitor_height) * BROWSER_MONITOR_HEIGHT_USAGE)))


def _estimate_browser_auto_list_page_size(context) -> int:
    ui_scale = _get_browser_ui_scale(context)
    budget_height = _get_browser_monitor_height_budget_px(context)
    usable_height = max(120, budget_height - int(round(BROWSER_LIST_PAGE_OVERHEAD_PX * ui_scale)))
    row_height = max(18, int(round(BROWSER_LIST_PAGE_ROW_HEIGHT_PX * ui_scale)))
    rows = max(1, usable_height // row_height)
    return max(10, min(MAX_BROWSER_FILE_PAGE_SIZE, rows))


def _clamp_browser_popup_width_percent(value) -> int:
    try:
        size = int(value)
    except Exception:
        size = DEFAULT_BROWSER_POPUP_WIDTH_PERCENT
    return max(MIN_BROWSER_POPUP_WIDTH_PERCENT, min(MAX_BROWSER_POPUP_WIDTH_PERCENT, size))


def _get_context_monitor_size_px(context) -> tuple[int, int]:
    window = getattr(context, "window", None)
    try:
        window_width = int(getattr(window, "width", 0) or 0)
    except Exception:
        window_width = 0
    try:
        window_height = int(getattr(window, "height", 0) or 0)
    except Exception:
        window_height = 0

    if os.name == "nt" and window is not None:
        try:
            import ctypes
            from ctypes import wintypes

            class _RECT(ctypes.Structure):
                _fields_ = [
                    ("left", wintypes.LONG),
                    ("top", wintypes.LONG),
                    ("right", wintypes.LONG),
                    ("bottom", wintypes.LONG),
                ]

            class _MONITORINFO(ctypes.Structure):
                _fields_ = [
                    ("cbSize", wintypes.DWORD),
                    ("rcMonitor", _RECT),
                    ("rcWork", _RECT),
                    ("dwFlags", wintypes.DWORD),
                ]

            win_x = int(getattr(window, "x", 0) or 0)
            win_y = int(getattr(window, "y", 0) or 0)
            rect = _RECT(win_x, win_y, win_x + max(1, window_width), win_y + max(1, window_height))
            monitor = ctypes.windll.user32.MonitorFromRect(ctypes.byref(rect), 2)
            if monitor:
                info = _MONITORINFO()
                info.cbSize = ctypes.sizeof(_MONITORINFO)
                if ctypes.windll.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                    monitor_width = int(info.rcMonitor.right - info.rcMonitor.left)
                    monitor_height = int(info.rcMonitor.bottom - info.rcMonitor.top)
                    if monitor_width > 0 and monitor_height > 0:
                        return monitor_width, monitor_height
        except Exception:
            pass

    if window_width > 0 and window_height > 0:
        return window_width, window_height
    return 1920, 1080


def _get_browser_popup_target_width_px(context) -> int:
    prefs = get_all_addon_prefs(context)
    monitor_width, _monitor_height = _get_context_monitor_size_px(context)
    if monitor_width <= 0:
        monitor_width = 1920

    desired_width = 0
    if prefs and getattr(prefs, "browser_popup_width", 0) > 0:
        desired_width = int(getattr(prefs, "browser_popup_width", 0) or 0)
    else:
        percent = DEFAULT_BROWSER_POPUP_WIDTH_PERCENT
        if prefs is not None:
            percent = _clamp_browser_popup_width_percent(
                getattr(prefs, "browser_popup_width_percent", DEFAULT_BROWSER_POPUP_WIDTH_PERCENT)
            )
        desired_width = int(round((percent / 100.0) * monitor_width))

    safe_margin = int(round(BROWSER_POPUP_SAFE_MARGIN_PX * _get_browser_ui_scale(context)))
    max_width = max(360, monitor_width - safe_margin)
    return max(360, min(max_width, desired_width))


def _get_browser_popup_layout_width_px(context) -> int:
    popup_width = 0
    try:
        popup_width = int(getattr(getattr(context, "region", None), "width", 0) or 0)
    except Exception:
        popup_width = 0
    if popup_width > 0:
        return popup_width
    return _get_browser_popup_target_width_px(context)


def _get_browser_folder_panel_width_px(context) -> int:
    try:
        addon_prefs = get_all_addon_prefs(context)
        if addon_prefs is not None:
            fixed_width = int(getattr(addon_prefs, "browser_folder_panel_width", DEFAULT_BROWSER_FOLDER_PANEL_WIDTH) or 0)
            if fixed_width > 0:
                return _clamp_browser_folder_panel_width(fixed_width)
            monitor_width, _monitor_height = _get_context_monitor_size_px(context)
            if monitor_width <= 0:
                monitor_width = 1920
            percent = _clamp_browser_folder_panel_width_percent(
                getattr(
                    addon_prefs,
                    "browser_folder_panel_width_percent",
                    DEFAULT_BROWSER_FOLDER_PANEL_WIDTH_PERCENT,
                )
            )
            return _clamp_browser_folder_panel_width(int(round((percent / 100.0) * monitor_width)))
    except Exception:
        pass
    return _clamp_browser_folder_panel_width(
        int(round((DEFAULT_BROWSER_FOLDER_PANEL_WIDTH_PERCENT / 100.0) * 1920))
    )


def _get_browser_effective_folder_panel_width_px(context, popup_width: int | None = None) -> int:
    popup_width = int(popup_width or 0)
    if popup_width <= 0:
        popup_width = _get_browser_popup_layout_width_px(context)
    desired_width = _get_browser_folder_panel_width_px(context)
    max_width = max(MIN_BROWSER_FOLDER_PANEL_WIDTH, popup_width - 220)
    return max(MIN_BROWSER_FOLDER_PANEL_WIDTH, min(max_width, desired_width))


def _get_browser_folder_split_factor(context) -> float:
    popup_width = _get_browser_popup_layout_width_px(context)
    if popup_width <= 0:
        return 0.35
    folder_width = _get_browser_effective_folder_panel_width_px(context, popup_width)
    factor = float(folder_width) / float(max(1, popup_width))
    return max(0.12, min(0.7, factor))


def _get_browser_grid_size_mode(context) -> str:
    try:
        browser = getattr(getattr(context, "scene", None), "witcher_file_browser", None)
        mode = str(getattr(browser, "grid_size_mode_live", "") or "")
    except Exception:
        mode = ""
    if mode in BROWSER_GRID_SIZE_PRESETS:
        return mode

    try:
        addon_prefs = get_all_addon_prefs(context)
    except Exception:
        addon_prefs = None
    if addon_prefs is not None:
        mode = str(getattr(addon_prefs, "browser_grid_size_mode", DEFAULT_BROWSER_GRID_SIZE_MODE) or "")
        if mode in BROWSER_GRID_SIZE_PRESETS:
            return mode
    return DEFAULT_BROWSER_GRID_SIZE_MODE


def _is_browser_dev_mode_enabled() -> bool:
    try:
        from ..dev import dev_config
        return bool(getattr(dev_config, "DEV_MODE_ENABLED", False))
    except Exception:
        return False


def _get_browser_grid_size_preset(context) -> dict:
    mode = _get_browser_grid_size_mode(context)
    preset = dict(BROWSER_GRID_SIZE_PRESETS.get(
        mode,
        BROWSER_GRID_SIZE_PRESETS[DEFAULT_BROWSER_GRID_SIZE_MODE],
    ))
    if _is_browser_dev_mode_enabled():
        try:
            browser = getattr(getattr(context, "scene", None), "witcher_file_browser", None)
        except Exception:
            browser = None
        if browser is not None:
            suffix = mode.lower()
            padding = getattr(browser, f"debug_{suffix}_padding_px", None)
            avg_char = getattr(browser, f"debug_{suffix}_avg_char_px", None)
            chars_min = getattr(browser, f"debug_{suffix}_chars_min", None)
            chars_max = getattr(browser, f"debug_{suffix}_chars_max", None)
            if padding is not None:
                preset["name_padding_px"] = int(padding)
            if avg_char is not None:
                preset["avg_char_px"] = float(avg_char)
            if chars_min is not None:
                preset["name_chars_min"] = int(chars_min)
            if chars_max is not None:
                preset["name_chars_max"] = int(chars_max)
    return preset


def _get_browser_grid_target_tile_width(context) -> int:
    return int(_get_browser_grid_size_preset(context).get("tile_width", 150))


def _estimate_browser_auto_grid_max_rows(context) -> int:
    ui_scale = _get_browser_ui_scale(context)
    budget_height = _get_browser_monitor_height_budget_px(context)
    usable_height = max(120, budget_height - int(round(BROWSER_GRID_PAGE_OVERHEAD_PX * ui_scale)))
    target_tile_width = _get_browser_grid_target_tile_width(context)
    row_height = max(
        int(round(BROWSER_GRID_TILE_HEIGHT_MIN_PX * ui_scale)),
        int(round(target_tile_width * BROWSER_GRID_TILE_HEIGHT_RATIO * ui_scale)),
    )
    rows = max(1, usable_height // max(1, row_height))
    return max(2, min(MAX_BROWSER_GRID_MAX_ROWS, rows))


def _get_browser_grid_columns(context) -> int:
    try:
        addon_prefs = get_all_addon_prefs(context)
    except Exception:
        addon_prefs = None

    if addon_prefs is not None:
        fixed_columns = _clamp_browser_grid_columns(getattr(addon_prefs, "browser_grid_columns", DEFAULT_BROWSER_GRID_COLUMNS))
        if fixed_columns > 0:
            return fixed_columns

    ui_scale = _get_browser_ui_scale(context)
    tile_width_physical = max(1, int(round(_get_browser_grid_target_tile_width(context) * ui_scale)))

    file_width = _estimate_browser_file_column_width_px(context)
    columns = max(1, int(file_width // tile_width_physical))
    return max(1, min(MAX_BROWSER_GRID_COLUMNS, columns))


def _estimate_browser_file_column_width_px(context) -> int:
    popup_width = _get_browser_popup_layout_width_px(context)
    usable_popup_width = max(360, popup_width - 24)
    folder_width = _get_browser_effective_folder_panel_width_px(context, usable_popup_width)
    file_width = usable_popup_width - folder_width - 16
    return max(220, file_width)


def _get_browser_grid_actual_tile_width_px(context, columns: int = 3) -> int:
    file_width = _estimate_browser_file_column_width_px(context)
    return max(120, int(file_width / max(1, int(columns or 1))))


def _get_browser_grid_icon_scale(context, columns: int = 3) -> float:
    preset = _get_browser_grid_size_preset(context)
    tile_width = _get_browser_grid_actual_tile_width_px(context, columns=columns)
    base_scale = tile_width / 36.0
    scale_multiplier = float(preset.get("icon_scale_multiplier", 1.0) or 1.0)
    return max(3.0, min(6.4, base_scale * scale_multiplier))


def _get_browser_grid_name_char_limit(context, columns: int = 3) -> int:
    preset = _get_browser_grid_size_preset(context)
    tile_width = _get_browser_grid_actual_tile_width_px(context, columns=columns)
    ui_scale = _get_browser_ui_scale(context)
    usable_width = max(48, tile_width - int(round(float(preset.get("name_padding_px", 28) or 28) * ui_scale)))
    avg_char_px = max(10.0, float(preset.get("avg_char_px", 14.5) or 14.5) * ui_scale)
    estimated_limit = int(usable_width / avg_char_px)
    min_chars = int(preset.get("name_chars_min", 6) or 6)
    max_chars = int(preset.get("name_chars_max", 11) or 11)
    return max(min_chars, min(max_chars, estimated_limit))


def _split_grid_display_name_lines(
    context,
    display_name: str,
    *,
    columns: int = 3,
):
    text = re.sub(r"\s+", " ", str(display_name or "").strip())
    if not text:
        return [""]
    char_limit = max(4, _get_browser_grid_name_char_limit(context, columns=columns))
    if len(text) <= char_limit:
        return [text]

    lines = []
    remaining = text
    delimiters = (" [", " (", "__", "_", "-", ".", " ")
    while remaining:
        if len(remaining) <= char_limit:
            lines.append(remaining)
            break

        split_index = 0
        for delimiter in delimiters:
            is_open_bracket = delimiter in {" [", " ("}
            if is_open_bracket:
                search_end = char_limit + len(delimiter)
            else:
                search_end = char_limit
            candidate_index = remaining.rfind(delimiter, 0, search_end)
            if candidate_index < 0:
                continue
            candidate_end = candidate_index if is_open_bracket else candidate_index + len(delimiter)
            if candidate_end > char_limit:
                continue
            if candidate_end > split_index:
                split_index = candidate_end

        if split_index <= 0:
            split_index = char_limit

        line_text = remaining[:split_index].rstrip(" ")
        if not line_text:
            split_index = min(len(remaining), char_limit)
            line_text = remaining[:split_index]

        lines.append(line_text)
        remaining = remaining[split_index:].lstrip(" ")

    return [line for line in lines if line] or [text]


def _get_browser_sort_prefs(browser) -> tuple[str, bool]:
    sort_by = str(getattr(browser, "sort_by", "NAME") or "NAME")
    if sort_by not in {"NAME", "EXT"}:
        sort_by = "NAME"
    ascending = bool(getattr(browser, "sort_ascending", True))
    return sort_by, ascending


def _sort_browser_items(items, browser):
    sort_by, ascending = _get_browser_sort_prefs(browser)
    if sort_by == "EXT":
        def key_fn(item):
            name = item.get("name", "")
            dot = name.rfind(".")
            ext = name[dot + 1:].lower() if dot >= 0 else ""
            return (ext, name.lower())
    else:
        key_fn = lambda item: item.get("name", "").lower()
    return sorted(items, key=key_fn, reverse=not ascending)


def _sort_browser_search_results(results, browser):
    """Sort a flat list of path strings (search results)."""
    sort_by, ascending = _get_browser_sort_prefs(browser)
    if sort_by == "EXT":
        def key_fn(path):
            dot = path.rfind(".")
            ext = path[dot + 1:].lower() if dot >= 0 else ""
            return (ext, path.lower())
    else:
        key_fn = lambda path: path.lower()
    return sorted(results, key=key_fn, reverse=not ascending)


def _get_browser_page_stats(items, page_index: int, page_size: int):
    total = len(items)
    total_pages = max(1, ((total + page_size - 1) // page_size)) if total else 1
    page_index = max(0, min(int(page_index or 0), total_pages - 1))
    start = page_index * page_size
    end = min(start + page_size, total)
    return {
        "items": items[start:end],
        "total": total,
        "page_index": page_index,
        "total_pages": total_pages,
        "visible_start": start + 1 if total else 0,
        "visible_end": end,
    }


def _ensure_revealed_file_page(context):
    if not context or not hasattr(context.scene, "witcher_file_browser"):
        return
    browser = context.scene.witcher_file_browser
    revealed = _normalize_virtual_path(getattr(browser, "revealed_file_path", "") or "")
    if not revealed or browser.search_query:
        return

    folder_items = folder_structure.get_items(browser.current_folder)
    filter_text = browser.extension_filter.strip().lower()
    filtered_items = [
        item for item in folder_items
        if item['is_folder'] or not filter_text or filter_text in item['name'].lower()
    ]
    filtered_items = _sort_browser_items(
        [item for item in filtered_items if not item['is_folder']],
        browser,
    )

    visible_files = []
    for item in filtered_items:
        if item['is_folder']:
            continue
        full_item_path = (
            browser.current_folder + "\\" + item['name']
            if browser.current_folder else item['name']
        )
        visible_files.append(_normalize_virtual_path(full_item_path))

    try:
        index = next(i for i, item_path in enumerate(visible_files) if item_path.lower() == revealed.lower())
    except StopIteration:
        return
    page_size = _get_browser_file_page_size(context, getattr(browser, "file_display_mode", "LIST"))
    browser.file_page_index = index // page_size


def _default_external_archive_export_root(archive_path: str) -> str:
    archive_path = win_safe_path(bpy.path.abspath(archive_path or ""))
    if not archive_path:
        return ""
    parent = os.path.dirname(archive_path)
    stem = Path(archive_path).stem or "archive"
    return win_safe_path(os.path.join(parent, f"{stem}_extracted"))


def _prime_external_archive_export_root(context, archive_path: str) -> str:
    if not context or not hasattr(context.scene, "witcher_file_browser"):
        return ""
    archive_path = _normalize_real_path(archive_path)
    if not archive_path:
        return ""
    browser = context.scene.witcher_file_browser
    tracked_archive = _normalize_real_path(getattr(browser, "external_extract_root_archive_path", "") or "")
    configured_root = str(getattr(browser, "external_extract_root", "") or "").strip()
    if configured_root and tracked_archive and tracked_archive.lower() == archive_path.lower():
        browser.external_extract_root_archive_path = archive_path
        return win_safe_path(bpy.path.abspath(configured_root))

    root = _default_external_archive_export_root(archive_path)
    if root:
        browser.external_extract_root = root
        browser.external_extract_root_archive_path = archive_path
    return root


def _get_external_archive_output_root(context, cache_type: str = "", create: bool = False) -> str:
    if not context or not hasattr(context.scene, "witcher_file_browser"):
        return ""
    if cache_type and not is_external_cache(cache_type):
        return ""

    browser = context.scene.witcher_file_browser
    configured = str(getattr(browser, "external_extract_root", "") or "").strip()
    root = win_safe_path(bpy.path.abspath(configured)) if configured else ""
    if not root:
        session = get_external_archive_session(cache_type or getattr(browser, "active_cache_type", ""))
        if session:
            root = _default_external_archive_export_root(session.get("archive_path", ""))
            if root:
                browser.external_extract_root = root
                browser.external_extract_root_archive_path = win_safe_path(
                    bpy.path.abspath(session.get("archive_path", "") or "")
                )
    if create and root:
        os.makedirs(root, exist_ok=True)
    return root


def _get_external_texture_export_ext(context, texture_format: str = "") -> str:
    browser = getattr(getattr(context, "scene", None), "witcher_file_browser", None)
    fmt = str(texture_format or getattr(browser, "external_texture_export_format", "DDS") or "DDS").upper()
    return {
        "DDS": ".dds",
        "PNG": ".png",
        "TGA": ".tga",
    }.get(fmt, ".dds")


def _get_external_item_output_rel_path(context, cache_type: str, item_path: str, loadmods: bool = False, texture_format: str = "") -> str:
    rel_path = get_vanilla_path(item_path, loadmods)
    if not rel_path:
        return ""

    effective_cache_type = get_effective_cache_type(cache_type)
    if effective_cache_type == "Collision":
        return get_collision_output_rel_path(rel_path, loadmods=False)
    if effective_cache_type == "Texture":
        base, _ext = os.path.splitext(rel_path)
        return base + _get_external_texture_export_ext(context, texture_format=texture_format)
    return rel_path


def _get_external_item_output_path(context, cache_type: str, item_path: str, loadmods: bool = False, texture_format: str = "") -> str:
    root = _get_external_archive_output_root(context, cache_type)
    rel_path = _get_external_item_output_rel_path(
        context,
        cache_type,
        item_path,
        loadmods=loadmods,
        texture_format=texture_format,
    )
    if not root or not rel_path:
        return ""
    return win_safe_path(os.path.join(root, rel_path.replace("/", os.sep).replace("\\", os.sep)))

def _normalize_dir(path: str) -> str:
    if not path:
        return ""
    return os.path.normpath(bpy.path.abspath(path))

def _get_witcher2_data_root(context) -> str:
    game_root = _normalize_dir(get_witcher2_game_path(context))
    if not game_root:
        return ""
    if os.path.basename(os.path.normpath(game_root)).lower() == "data":
        return game_root
    return os.path.join(game_root, "data")

def is_disk_cache(cache_type: str) -> bool:
    return cache_type in DISK_CACHE_TYPES

def _is_w2ter_buffer_name(name: str) -> bool:
    return terrain_w2ter.is_w2ter_buffer_name(name)

def _should_skip_buffer_name(name: str) -> bool:
    return ".buffer" in name.lower() and not _is_w2ter_buffer_name(name)

def _get_project_labels(project_paths):
    labels = {}
    used = set()
    for path in project_paths:
        base = os.path.basename(os.path.normpath(path)) or "Project"
        label = base
        idx = 2
        while label in used:
            label = f"{base}_{idx}"
            idx += 1
        labels[path] = label
        used.add(label)
    return labels

def _get_workspace_root(project_root: str) -> str:
    if not project_root:
        return ""
    workspace = os.path.join(project_root, "workspace")
    if os.path.isdir(workspace):
        return workspace
    if os.path.isdir(project_root):
        return project_root
    return ""

def _get_cooked_content_roots(project_root: str):
    roots = []
    if not project_root:
        return roots
    packed = os.path.join(project_root, "packed")
    if not os.path.isdir(packed):
        return roots
    for base in ("mods", "dlc"):
        base_dir = os.path.join(packed, base)
        if not os.path.isdir(base_dir):
            continue
        try:
            for entry in os.scandir(base_dir):
                if not entry.is_dir():
                    continue
                content_root = os.path.join(entry.path, "content")
                if os.path.isdir(content_root):
                    roots.append((content_root, entry.name))
        except Exception:
            continue
    return roots

def _scan_disk_root(root_path, prefix="", source_kind="", project_root=None):
    if not root_path or not os.path.isdir(root_path):
        return
    root_path = os.path.normpath(root_path)
    for dirpath, _, filenames in os.walk(root_path):
        rel_dir = os.path.relpath(dirpath, root_path)
        if rel_dir == ".":
            rel_dir = ""
        for fname in filenames:
            if _should_skip_buffer_name(fname):
                continue
            rel_path = os.path.join(rel_dir, fname) if rel_dir else fname
            rel_path = rel_path.replace("/", "\\")
            virtual_path = os.path.join(prefix, rel_path) if prefix else rel_path
            virtual_path = virtual_path.replace("/", "\\")
            if virtual_path in _file_source_map:
                continue
            abs_path = os.path.join(dirpath, fname)
            _file_source_map[virtual_path] = abs_path
            _file_source_info[virtual_path] = {
                "source": source_kind,
                "root_path": root_path,
                "project_root": project_root,
            }
            folder_structure.add_path(virtual_path)

def populate_disk_folder_structure(cache_type, context):
    global _file_source_map, _file_source_info
    folder_structure.clear()
    folder_structure.cache_type = cache_type
    _file_source_map = {}
    _file_source_info = {}

    addon_prefs = get_all_addon_prefs(context)
    depot_root = _normalize_dir(getattr(addon_prefs, "redkit_depot_path", ""))
    uncooked_root = _normalize_dir(getattr(addon_prefs, "redkit_uncooked_path", ""))
    w2_data_root = _get_witcher2_data_root(context)

    project_paths = [p.path for p in getattr(addon_prefs, "redkit_projects", []) if p.path]
    project_labels = _get_project_labels(project_paths)

    if cache_type == "REDkit Depot":
        _scan_disk_root(depot_root, source_kind="depot")
        return
    if cache_type == "REDkit Uncooked":
        _scan_disk_root(uncooked_root, source_kind="uncooked")
        return
    if cache_type == "Witcher 2 Data":
        _scan_disk_root(w2_data_root, source_kind="witcher2")
        return
    if cache_type == "Workspace":
        for project_root in project_paths:
            label = project_labels.get(project_root, "Project")
            workspace_root = _get_workspace_root(project_root)
            _scan_disk_root(workspace_root, prefix=label, source_kind="workspace", project_root=project_root)
        return
    if cache_type == "Cooked":
        for project_root in project_paths:
            label = project_labels.get(project_root, "Project")
            for content_root, content_label in _get_cooked_content_roots(project_root):
                prefix = f"{label}\\{content_label}"
                _scan_disk_root(content_root, prefix=prefix, source_kind="cooked", project_root=project_root)

def get_disk_abs_path(cache_type, item_path):
    if not is_disk_cache(cache_type):
        return None
    if os.path.isabs(item_path):
        return item_path
    return _file_source_map.get(item_path)

def get_repo_override_roots_for_item(context, cache_type, item_path):
    if not is_disk_cache(cache_type):
        return []
    addon_prefs = get_all_addon_prefs(context)
    depot_root = _normalize_dir(getattr(addon_prefs, "redkit_depot_path", ""))
    uncooked_root = _normalize_dir(getattr(addon_prefs, "redkit_uncooked_path", ""))
    w2_data_root = _get_witcher2_data_root(context)
    project_paths = [p.path for p in getattr(addon_prefs, "redkit_projects", []) if p.path]
    workspace_roots = [_get_workspace_root(p) for p in project_paths]
    info = _file_source_info.get(item_path, {})
    root_path = info.get("root_path", "")
    project_root = info.get("project_root", "")

    roots = []
    if cache_type == "Workspace":
        roots = [root_path, depot_root, uncooked_root]
    elif cache_type == "Cooked":
        workspace_root = _get_workspace_root(project_root)
        roots = [root_path, workspace_root, depot_root, uncooked_root]
    elif cache_type == "REDkit Depot":
        roots = workspace_roots + [depot_root, uncooked_root]
    elif cache_type == "REDkit Uncooked":
        roots = workspace_roots + [uncooked_root, depot_root]
    elif cache_type == "Witcher 2 Data":
        roots = [w2_data_root]
    # Filter empties and de-dup
    clean = []
    seen = set()
    for r in roots:
        if not r:
            continue
        nr = os.path.normpath(r)
        if nr in seen:
            continue
        seen.add(nr)
        clean.append(nr)
    return clean

# Texture file extensions that support preview
TEXTURE_EXTENSIONS = {'.xbm', '.dds', '.png', '.jpg', '.jpeg', '.tga', '.bmp', '.w2cube'}

def is_texture_file(filename):
    """Check if a filename has a texture extension."""
    ext = os.path.splitext(filename)[1].lower()
    return ext in TEXTURE_EXTENSIONS

def is_w2ter_buffer_file(filename: str) -> bool:
    return _is_w2ter_buffer_name(filename)

def get_w2ter_buffer_label(filename: str) -> str:
    idx = terrain_w2ter.get_w2ter_buffer_index(filename)
    return terrain_w2ter.get_w2ter_buffer_label(idx)

def is_w2ter_tile_file(filename: str) -> bool:
    lower = filename.lower()
    return ".w2ter" in lower

def build_w2cube_preview(fdir):
    """Convert a .w2cube file to DDS and return the path for preview.

    Tries TextureCache first (full resolution cubemap DDS), then falls
    back to the low-res embedded data. Blender can load the resulting
    DDS directly and will show the first face as a preview.
    """
    try:
        dds_path = convert_w2cube_to_dds(fdir)
        if dds_path and os.path.exists(dds_path):
            return dds_path
    except Exception as e:
        log.warning(f"build_w2cube_preview failed for {fdir}: {e}")
    return None


def get_vanilla_path(item_path, loadmods):
    """Strip mod folder prefix from path when browsing mods.
    Mod items are stored as 'ModName\\original\\path'. Returns the original path.
    """
    if item_path:
        item_path = item_path.replace("/", "\\")
    if loadmods and "\\" in item_path:
        parts = item_path.split("\\", 1)
        if len(parts) > 1:
            return parts[1]
    return item_path

def strip_mod_prefix(item_path: str, mod_name: str):
    if not item_path:
        return item_path
    item_path = item_path.replace("/", "\\")
    if mod_name:
        prefix = (mod_name + "\\").lower()
        if item_path.lower().startswith(prefix):
            return item_path[len(mod_name) + 1:]
    return item_path

def get_collision_output_rel_path(item_path: str, loadmods: bool = False) -> str:
    """Return the extracted on-disk relative path for a collision-cache entry."""
    rel_path = get_vanilla_path(item_path, loadmods)
    if not rel_path:
        return rel_path

    key_norm = (item_path or rel_path).replace("/", "\\")
    ext = collision_extension_map.get(key_norm) or collision_extension_map.get(rel_path)
    if not ext:
        return rel_path

    base, current_ext = os.path.splitext(rel_path)
    if current_ext.lower() == ext.lower():
        return rel_path
    return base + ext


def _ensure_redcloth_apx_for_asset_import(context, redcloth_abs_path: str, redcloth_rel_path: str, loadmods: bool = False) -> str:
    """Ensure a .redcloth/.redapex file has a corresponding .apx in the uncook tree."""
    from .ui_mesh import find_apx

    redcloth_abs_path = win_safe_path(redcloth_abs_path or "")
    redcloth_rel_path = (redcloth_rel_path or "").replace("/", "\\")

    # First try existing APX resolution (same folder / configured legacy path / recursive search).
    apx_path = str(find_apx(redcloth_abs_path))
    if apx_path and win_path_exists(apx_path):
        return win_safe_path(apx_path)

    # Next try APB already extracted beside the redcloth file.
    local_apb = os.path.splitext(redcloth_abs_path)[0] + ".apb"
    if not win_path_exists(local_apb):
        # Pull the APB from collision.cache into the uncook path on demand.
        try:
            manager = LoadCollisionManager(loadmods=loadmods)
            items = manager.find_item_by_path_name(redcloth_rel_path)
            if items:
                final_item = items[-1] if isinstance(items, list) else items
                output_ext = getattr(final_item, "Extension", ".apb") or ".apb"
                item_name = getattr(final_item, "Name", redcloth_rel_path)
                rel_name = get_vanilla_path(item_name, loadmods)
                rel_apb = os.path.splitext(rel_name)[0] + output_ext
                uncook_path = get_uncook_path(context) or ""
                local_apb = os.path.join(uncook_path, rel_apb)
                if uncook_path and prepare_extraction_target(local_apb, uncook_path):
                    written = final_item.extract_to_file(local_apb)
                    if written:
                        local_apb = written
        except Exception as exc:
            log.warning("Redcloth APB extraction failed for %s: %s", redcloth_rel_path, exc)

    conv = ensure_apx_from_apb(context, local_apb, overwrite=False)
    if conv["status"] in {"converted", "updated", "exists"} and win_path_exists(conv["apx_path"]):
        return win_safe_path(conv["apx_path"])
    return ""


def _srt_json_from_file(abs_file_path: str) -> str:
    """Ensure a JSON sidecar exists for an SRT file and return its path."""
    abs_file_path = win_safe_path(abs_file_path or "")
    lower = abs_file_path.lower()
    if lower.endswith(".json"):
        return abs_file_path if win_path_exists(abs_file_path) else ""
    if not lower.endswith(".srt") or not win_path_exists(abs_file_path):
        return ""

    json_path = abs_file_path + ".json"
    try:
        src_mtime = os.path.getmtime(abs_file_path)
        json_mtime = os.path.getmtime(json_path) if os.path.exists(json_path) else -1
    except Exception:
        src_mtime = -1
        json_mtime = -1

    if json_mtime >= src_mtime and win_path_exists(json_path):
        return json_path

    try:
        import importlib
        import subprocess

        srt_mod = importlib.import_module("io_mesh_srt")
        converter = os.path.join(os.path.dirname(srt_mod.__file__), "converter", "srt_json_converter.exe")
        if not os.path.isfile(converter):
            return json_path if win_path_exists(json_path) else ""
        command = [converter, "-d", abs_file_path, "-o", os.path.dirname(abs_file_path)]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        if completed.returncode != 0 and not win_path_exists(json_path):
            log.warning(
                "SRT converter failed for %s (code=%s): %s",
                abs_file_path,
                completed.returncode,
                (completed.stderr or completed.stdout or "").strip(),
            )
            return ""
    except Exception as exc:
        log.warning("Could not generate SRT JSON sidecar for %s: %s", abs_file_path, exc)
        return json_path if win_path_exists(json_path) else ""

    return json_path if win_path_exists(json_path) else ""


def _prepare_srt_lod0_json(json_path: str) -> str:
    """Create a stripped copy of an SRT JSON with only LOD0, no billboards.

    Returns the path to the stripped JSON file (``*_lod0.json``), or the
    original *json_path* unchanged when stripping is unnecessary or fails.
    """
    if not json_path or not win_path_exists(json_path):
        return json_path or ""

    lod0_path = json_path.replace(".srt.json", "_lod0.srt.json")
    if lod0_path == json_path:
        lod0_path = os.path.splitext(json_path)[0] + "_lod0.json"

    try:
        src_mtime = os.path.getmtime(json_path)
        lod0_mtime = os.path.getmtime(lod0_path) if os.path.exists(lod0_path) else -1
    except Exception:
        src_mtime = -1
        lod0_mtime = -1

    if lod0_mtime >= src_mtime and win_path_exists(lod0_path):
        return lod0_path

    try:
        import json as _json
        with open(json_path, "r", encoding="utf-8") as fh:
            srt = _json.load(fh)

        # Keep only LOD0
        if "Geometry" in srt and "PLods" in srt["Geometry"]:
            srt["Geometry"]["PLods"] = srt["Geometry"]["PLods"][:1]

        # Remove billboards
        srt.pop("VerticalBillboards", None)
        if "HorizontalBillboard" in srt:
            srt["HorizontalBillboard"]["BPresent"] = False

        # Remove collision objects
        srt.pop("CollisionObjects", None)

        with open(lod0_path, "w", encoding="utf-8") as fh:
            _json.dump(srt, fh)
        return lod0_path
    except Exception as exc:
        log.warning("Could not create LOD0-stripped SRT JSON for %s: %s", json_path, exc)
        return json_path


def _collect_srt_texture_names(json_path: str) -> list[str]:
    """Collect referenced texture filenames from an SRT JSON file."""
    if not json_path or not win_path_exists(json_path):
        return []

    try:
        import json
        with open(json_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        log.warning("Failed reading SRT JSON for texture extraction (%s): %s", json_path, exc)
        return []

    names: list[str] = []
    seen: set[str] = set()

    def add_name(value):
        if not isinstance(value, str):
            return
        value = value.strip()
        if not value:
            return
        base = os.path.basename(value.replace("/", os.sep).replace("\\", os.sep))
        if not base:
            return
        key = base.lower()
        if key in seen:
            return
        seen.add(key)
        names.append(base)

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "ApTextures" and isinstance(value, list):
                    for tex in value:
                        add_name(tex)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return names


def _choose_srt_texture_cache_item(manager, tex_name: str, srt_rel_folder: str):
    tex_name = os.path.basename(tex_name or "")
    if not tex_name:
        return None
    tex_base = tex_name.lower()
    tex_stem = os.path.splitext(tex_base)[0]
    srt_rel_folder = (srt_rel_folder or "").replace("/", "\\").lower().strip("\\")

    best = None
    best_score = -1
    for key, item_list in manager.Items.items():
        if not isinstance(key, str):
            continue
        item = item_list[-1] if isinstance(item_list, list) else item_list
        item_name = (getattr(item, "Name", None) or key or "").replace("/", "\\")
        if not item_name:
            continue
        item_base = os.path.basename(item_name).lower()
        item_stem = os.path.splitext(item_base)[0]
        if item_base != tex_base and item_stem != tex_stem:
            continue

        item_dir = os.path.dirname(item_name).lower().strip("\\")
        score = 0
        if item_base == tex_base:
            score += 20
        else:
            score += 10
        if srt_rel_folder and item_dir == srt_rel_folder:
            score += 100
        elif srt_rel_folder and item_dir.startswith(srt_rel_folder + "\\"):
            score += 40
        if item_name.lower().endswith(".xbm"):
            score += 5

        if score > best_score:
            best_score = score
            best = item
    return best


def _export_srt_textures_for_import(context, abs_srt_path: str, rel_srt_path: str, loadmods: bool = False) -> dict:
    """Extract TextureCache DDS files referenced by an SRT into the SRT's folder."""
    result = {
        "requested": 0,
        "exported": 0,
        "existing": 0,
        "missing": [],
        "json_path": "",
        "import_path": abs_srt_path,
    }

    json_path = _srt_json_from_file(abs_srt_path)
    result["json_path"] = json_path
    if json_path:
        # Use JSON directly to avoid running the converter twice in io_mesh_srt.
        result["import_path"] = json_path

    tex_names = _collect_srt_texture_names(json_path)
    result["requested"] = len(tex_names)
    if not tex_names:
        return result

    srt_dir = os.path.dirname(abs_srt_path)
    rel_vanilla = get_vanilla_path(rel_srt_path, loadmods) or rel_srt_path or ""
    srt_rel_folder = os.path.dirname(rel_vanilla.replace("/", "\\"))
    uncook_root = get_uncook_path(context) or ""

    try:
        manager = LoadTextureManager(loadmods=loadmods)
    except Exception as exc:
        log.warning("Failed loading TextureCache for SRT texture extraction: %s", exc)
        result["missing"] = tex_names[:]
        return result

    for tex_name in tex_names:
        out_path = os.path.join(srt_dir, tex_name)
        out_dds = os.path.splitext(out_path)[0] + ".dds"
        if win_path_exists(out_dds):
            result["existing"] += 1
            continue

        item = _choose_srt_texture_cache_item(manager, tex_name, srt_rel_folder)
        if not item:
            result["missing"].append(tex_name)
            continue

        try:
            if prepare_extraction_target(out_path, uncook_root):
                item.extract_to_file(out_path)
            if win_path_exists(out_dds):
                result["exported"] += 1
            else:
                result["missing"].append(tex_name)
        except Exception as exc:
            log.warning("Failed extracting SRT texture %s -> %s: %s", tex_name, out_dds, exc)
            result["missing"].append(tex_name)

    if result["requested"]:
        log.info(
            "SRT texture extraction for %s: %d requested, %d exported, %d already present, %d missing",
            os.path.basename(abs_srt_path),
            result["requested"],
            result["exported"],
            result["existing"],
            len(result["missing"]),
        )
    return result


def _snapshot_srt_import_state(context) -> dict:
    return {
        "active_collection_name": getattr(context.view_layer.active_layer_collection.collection, "name", ""),
        "collection_names": set(c.name for c in bpy.data.collections),
        "object_names": set(o.name for o in bpy.data.objects),
    }


def _is_billboard_object(obj, billboard_collection_names: set) -> bool:
    """Check if an object belongs to a billboard collection or has billboard-like naming."""
    name_lower = obj.name.lower()
    if "billboard" in name_lower:
        return True
    for coll in obj.users_collection:
        if coll.name in billboard_collection_names:
            return True
    return False


def _prune_srt_import_to_lod0(snapshot: dict) -> dict:
    """Keep only the main LOD0 mesh object from a freshly imported SRT asset.
    Also removes all vertical/horizontal billboard objects and their collections."""
    result = {"kept": [], "removed": 0}
    if not snapshot:
        return result

    pre_collections = snapshot.get("collection_names", set())
    pre_objects = snapshot.get("object_names", set())

    new_collections = [c for c in bpy.data.collections if c.name not in pre_collections]
    new_objects = [o for o in bpy.data.objects if o.name not in pre_objects]
    if not new_objects:
        return result

    main_srt_collections = [c for c in new_collections if "SpeedTreeMainCollection" in c]

    # Identify billboard collections (VerticalBillboards, HorizontalBillboard, etc.)
    billboard_collection_names = set()
    for coll in new_collections:
        if "billboard" in coll.name.lower():
            billboard_collection_names.add(coll.name)

    lod0_objects = []

    if main_srt_collections:
        for main_coll in main_srt_collections:
            for child in main_coll.children:
                if child.name.startswith("LOD0"):
                    lod0_objects.extend([
                        obj for obj in child.objects
                        if obj.type == 'MESH'
                        and not _is_billboard_object(obj, billboard_collection_names)
                    ])

    if not lod0_objects:
        lod0_objects = [
            obj for obj in new_objects
            if obj.type == 'MESH' and "lod0" in obj.name.lower()
            and not _is_billboard_object(obj, billboard_collection_names)
        ]
    if not lod0_objects:
        lod0_objects = [
            obj for obj in new_objects
            if obj.type == 'MESH'
            and not _is_billboard_object(obj, billboard_collection_names)
        ]
    if not lod0_objects:
        return result

    keep_obj = lod0_objects[0]
    result["kept"] = [keep_obj.name]
    keep_name = keep_obj.name

    for obj in list(new_objects):
        if obj.name == keep_name:
            continue
        try:
            bpy.data.objects.remove(obj, do_unlink=True)
            result["removed"] += 1
        except Exception as exc:
            log.debug("Could not remove SRT object %s during LOD0 prune: %s", obj.name, exc)

    # Remove empty collections left behind (child-first).
    parent_map = {}
    for parent in bpy.data.collections:
        for child in parent.children:
            parent_map[child.name] = parent.name

    def coll_depth(coll):
        depth = 0
        seen = set()
        cur_name = coll.name
        while cur_name in parent_map and cur_name not in seen:
            seen.add(cur_name)
            cur_name = parent_map[cur_name]
            depth += 1
        return depth

    for coll in sorted(new_collections, key=coll_depth, reverse=True):
        if bpy.data.collections.get(coll.name) is None:
            continue
        try:
            if len(coll.all_objects) == 0 and len(coll.children) == 0:
                bpy.data.collections.remove(coll)
        except Exception:
            pass

    return result


def _flatten_srt_import_collections(context, abs_import_path: str, snapshot: dict) -> None:
    """Remove io_mesh_srt-created collections and parent imported objects under one empty."""
    if not snapshot:
        return

    pre_collections = snapshot.get("collection_names", set())
    pre_objects = snapshot.get("object_names", set())
    target_collection_name = snapshot.get("active_collection_name", "")

    new_collections = [c for c in bpy.data.collections if c.name not in pre_collections]
    if not new_collections:
        return
    new_objects = [o for o in bpy.data.objects if o.name not in pre_objects]

    main_srt_collections = [c for c in new_collections if "SpeedTreeMainCollection" in c]
    if main_srt_collections:
        imported_objects = []
        seen_obj_names = set()
        for coll in main_srt_collections:
            for obj in coll.all_objects:
                if obj.name in seen_obj_names:
                    continue
                seen_obj_names.add(obj.name)
                imported_objects.append(obj)
    else:
        imported_objects = new_objects

    if not imported_objects:
        return

    target_collection = bpy.data.collections.get(target_collection_name)
    if target_collection is None:
        target_collection = context.view_layer.active_layer_collection.collection

    stem_name = Path(abs_import_path).stem
    if stem_name.endswith(".srt"):
        stem_name = Path(stem_name).stem
    group_empty = bpy.data.objects.new(f"{stem_name}_grp", None)
    group_empty.empty_display_type = 'PLAIN_AXES'
    target_collection.objects.link(group_empty)

    imported_obj_names = {obj.name for obj in imported_objects}
    for obj in imported_objects:
        if target_collection not in obj.users_collection:
            target_collection.objects.link(obj)
        if obj is group_empty:
            continue
        if obj.parent and obj.parent.name in imported_obj_names:
            continue
        try:
            world = obj.matrix_world.copy()
            obj.parent = group_empty
            obj.matrix_world = world
        except Exception:
            obj.parent = group_empty

    # Unlink and remove collections created by the SRT addon (children first).
    parent_map = {}
    for parent in bpy.data.collections:
        for child in parent.children:
            parent_map[child.name] = parent.name

    def coll_depth(coll):
        depth = 0
        seen = set()
        cur_name = coll.name
        while cur_name in parent_map and cur_name not in seen:
            seen.add(cur_name)
            cur_name = parent_map[cur_name]
            depth += 1
        return depth

    for coll in sorted(new_collections, key=coll_depth, reverse=True):
        if bpy.data.collections.get(coll.name) is None:
            continue
        try:
            for obj in list(coll.objects):
                try:
                    coll.objects.unlink(obj)
                except Exception:
                    pass
            bpy.data.collections.remove(coll)
        except Exception as exc:
            log.debug("Could not remove imported SRT collection %s: %s", coll.name, exc)

    # Restore active layer collection to the target (pre-import) collection.
    # Without this, io_mesh_srt's next import fails because it tries to create
    # children under the now-deleted SpeedTreeMainCollection layer.
    _restore_active_layer_collection(context, target_collection_name)


def _restore_active_layer_collection(context, collection_name: str) -> None:
    """Set view_layer.active_layer_collection to match *collection_name*."""
    vl = context.view_layer

    def _find_layer_coll(layer_coll, name):
        if layer_coll.collection.name == name:
            return layer_coll
        for child in layer_coll.children:
            found = _find_layer_coll(child, name)
            if found:
                return found
        return None

    if collection_name:
        target = _find_layer_coll(vl.layer_collection, collection_name)
        if target:
            vl.active_layer_collection = target
            return
    # Fallback: reset to the scene's root collection.
    vl.active_layer_collection = vl.layer_collection


def _find_redcloth_material_for_collision_apb(context, collision_item_path: str, loadmods: bool = False) -> str:
    """Find/extract the matching .redcloth/.redapex material file for a collision cache APB import."""
    collision_item_path = (collision_item_path or "").replace("/", "\\")
    if not collision_item_path:
        return ""

    candidates = []
    if collision_item_path.lower().endswith((".redcloth", ".redapex")):
        candidates.append(collision_item_path)
    else:
        base, _ext = os.path.splitext(collision_item_path)
        candidates.append(base + ".redcloth")
        candidates.append(base + ".redapex")

    # Also try vanilla path when browsing mods.
    vanilla_path = get_vanilla_path(collision_item_path, loadmods)
    if vanilla_path:
        if vanilla_path.lower().endswith((".redcloth", ".redapex")):
            candidates.append(vanilla_path)
        else:
            base, _ext = os.path.splitext(vanilla_path)
            candidates.append(base + ".redcloth")
            candidates.append(base + ".redapex")

    seen = set()
    browser = getattr(getattr(context, "scene", None), "witcher_file_browser", None)
    active_cache_type = str(getattr(browser, "active_cache_type", "") or "")
    for candidate in candidates:
        candidate = (candidate or "").replace("/", "\\")
        key = candidate.lower()
        if not candidate or key in seen:
            continue
        seen.add(key)
        if active_cache_type == EXTERNAL_COLLISION_CACHE_TYPE:
            abs_path = _get_external_item_output_path(context, active_cache_type, candidate, loadmods=False)
            if abs_path and win_path_exists(abs_path):
                return win_safe_path(abs_path)
        try:
            abs_path = repo_file(candidate)
        except Exception as exc:
            log.debug("repo_file failed for redcloth candidate %s: %s", candidate, exc)
            continue
        if abs_path and win_path_exists(abs_path):
            return win_safe_path(abs_path)
    return ""

def get_uncook_file_info(context, item_path, loadmods=False):
    """Check if a file exists in the uncook path and return (exists, size_on_disk).
    Returns (False, 0) if not found.
    When loadmods=True, strips the mod folder prefix before checking.
    """
    try:
        addon_prefs = get_all_addon_prefs(context)
        uncook_path = addon_prefs.uncook_path
        # Strip mod prefix when browsing mods
        check_path = get_vanilla_path(item_path, loadmods)
        abs_path = os.path.join(uncook_path, check_path)
        if win_path_exists(abs_path):
            return (True, win_path_getsize(abs_path))
        return (False, 0)
    except Exception:
        return (False, 0)


def get_external_file_info(context, cache_type, item_path, loadmods=False):
    """Check extracted file presence for standalone archive sessions."""
    try:
        abs_path = _get_external_item_output_path(context, cache_type, item_path, loadmods=loadmods)
        if abs_path and win_path_exists(abs_path):
            return (True, win_path_getsize(abs_path))
        return (False, 0)
    except Exception:
        return (False, 0)


def get_collision_file_info(context, item_path, loadmods=False):
    """Check extracted collision file presence using the resolved output extension."""
    try:
        addon_prefs = get_all_addon_prefs(context)
        uncook_path = addon_prefs.uncook_path
        check_path = get_collision_output_rel_path(item_path, loadmods=loadmods)
        abs_path = os.path.join(uncook_path, check_path)
        if win_path_exists(abs_path):
            return (True, win_path_getsize(abs_path))
        return (False, 0)
    except Exception:
        return (False, 0)

def get_texture_file_info(context, item_path, loadmods=False):
    """Check if a texture exists in the configured texture output path (dds preferred)."""
    try:
        texture_root = get_texture_path(context) or ""
        check_path = get_vanilla_path(item_path, loadmods)
        abs_xbm = os.path.join(texture_root, check_path)
        abs_dds = os.path.splitext(abs_xbm)[0] + ".dds"
        if win_path_exists(abs_dds):
            return (True, win_path_getsize(abs_dds))
        if win_path_exists(abs_xbm):
            return (True, win_path_getsize(abs_xbm))
        return (False, 0)
    except Exception:
        return (False, 0)

def get_file_info(context, cache_type, item_path, loadmods=False):
    """Get file existence and size for cache or disk sources."""
    if is_disk_cache(cache_type):
        abs_path = get_disk_abs_path(cache_type, item_path)
        if abs_path and win_path_exists(abs_path):
            try:
                return (True, win_path_getsize(abs_path))
            except Exception:
                return (True, 0)
        return (False, 0)
    if is_external_cache(cache_type):
        return get_external_file_info(context, cache_type, item_path, loadmods=loadmods)
    if cache_type == "Texture":
        return get_texture_file_info(context, item_path, loadmods=loadmods)
    if get_effective_cache_type(cache_type) == "Collision":
        return get_collision_file_info(context, item_path, loadmods=loadmods)
    return get_uncook_file_info(context, item_path, loadmods=loadmods)

def get_source_label(context, item_path, loadmods=False, cache_type=""):
    """Return recorded source label for extracted files (e.g., mod:XYZ or vanilla)."""
    try:
        if is_external_cache(cache_type):
            return ""
        effective_cache_type = get_effective_cache_type(cache_type) if cache_type else cache_type
        source_root = get_texture_path(context) if effective_cache_type == "Texture" else get_uncook_path(context)
        source_root = source_root or ""
        rel_path = get_vanilla_path(item_path, loadmods)
        return get_source_for_path(source_root, rel_path)
    except Exception:
        return ""

def get_mod_override_label(item_path, loadmods=False):
    rel_path = get_vanilla_path(item_path, loadmods)
    return get_mod_override_name(rel_path)

def get_uncook_abs_path(context, item_path, loadmods=False) -> str:
    try:
        addon_prefs = get_all_addon_prefs(context)
        uncook_path = addon_prefs.uncook_path
        rel_path = get_vanilla_path(item_path, loadmods)
        return os.path.join(uncook_path, rel_path)
    except Exception:
        return ""


def get_browser_item_output_path(context, cache_type, item_path, loadmods=False) -> str:
    if is_disk_cache(cache_type):
        return get_disk_abs_path(cache_type, item_path) or ""

    if is_external_cache(cache_type):
        return _get_external_item_output_path(context, cache_type, item_path, loadmods=loadmods)

    effective_cache_type = get_effective_cache_type(cache_type)
    if effective_cache_type == "Collision":
        addon_prefs = get_all_addon_prefs(context)
        check_path = get_collision_output_rel_path(item_path, loadmods=loadmods)
        if not addon_prefs or not check_path:
            return ""
        return os.path.join(addon_prefs.uncook_path, check_path)

    rel_path = get_vanilla_path(item_path, loadmods)
    if not rel_path:
        return ""

    abs_path = repo_file(rel_path)
    if effective_cache_type == "Texture":
        dds_path = os.path.splitext(abs_path)[0] + ".dds"
        if win_path_exists(dds_path):
            return dds_path
    return abs_path


_browser_asset_preview_collection = None
_browser_asset_preview_path_cache = {}
_browser_w2ent_icon_path_cache = {}
_BROWSER_W2ENT_ICON_CACHE_VERSION = "v5"
_browser_scaleform_root_cache = {
    "game_path": "",
    "roots": (),
    "scanned": False,
}


def _cache_bounded_store(cache: dict, key, value, max_entries: int = 2048):
    if len(cache) >= max_entries and key not in cache:
        cache.clear()
    cache[key] = value


def _get_browser_asset_preview_collection():
    global _browser_asset_preview_collection
    if _browser_asset_preview_collection is None:
        _browser_asset_preview_collection = bpy.utils.previews.new()
    return _browser_asset_preview_collection


def _clear_browser_asset_previews():
    global _browser_asset_preview_collection
    _browser_asset_preview_path_cache.clear()
    _browser_w2ent_icon_path_cache.clear()
    clear_browser_dummy_icon_cache()
    _browser_scaleform_root_cache["game_path"] = ""
    _browser_scaleform_root_cache["roots"] = ()
    _browser_scaleform_root_cache["scanned"] = False
    if _browser_asset_preview_collection is not None:
        try:
            bpy.utils.previews.remove(_browser_asset_preview_collection)
        except Exception:
            pass
    _browser_asset_preview_collection = None


def _make_browser_preview_temp_path(cache_type: str, file_path: str, suffix: str = ".dds") -> str:
    import hashlib
    import tempfile

    safe_name = os.path.splitext(os.path.basename(file_path or "preview"))[0] or "preview"
    digest = hashlib.sha1(
        f"{cache_type}|{_normalize_virtual_path(file_path)}".encode("utf-8", errors="ignore")
    ).hexdigest()[:12]
    preview_dir = os.path.join(tempfile.gettempdir(), "witcher_preview", "browser")
    os.makedirs(preview_dir, exist_ok=True)
    return win_safe_path(os.path.join(preview_dir, f"{safe_name}.{digest}{suffix}"))


def _collect_strings_from_cr2w_value(value, out_strings: set[str], seen: set[int]):
    if value is None:
        return

    obj_id = id(value)
    if obj_id in seen:
        return

    if isinstance(value, (list, tuple, set)):
        seen.add(obj_id)
        for item in value:
            _collect_strings_from_cr2w_value(item, out_strings, seen)
        return

    if isinstance(value, dict):
        seen.add(obj_id)
        for item in value.values():
            _collect_strings_from_cr2w_value(item, out_strings, seen)
        return

    seen.add(obj_id)

    if isinstance(value, str):
        text = value.strip()
        if text:
            out_strings.add(text)
        return

    if hasattr(value, "ToString"):
        try:
            text = value.ToString()
            if isinstance(text, str) and text.strip():
                out_strings.add(text.strip())
        except Exception:
            pass

    for attr_name in ("String", "Value", "value", "DepotPath"):
        try:
            attr = getattr(value, attr_name, None)
        except Exception:
            attr = None
        if attr is not None and attr is not value:
            _collect_strings_from_cr2w_value(attr, out_strings, seen)

    for attr_name in ("PROPS", "More", "elements", "Handles"):
        try:
            attr = getattr(value, attr_name, None)
        except Exception:
            attr = None
        if attr:
            _collect_strings_from_cr2w_value(attr, out_strings, seen)

    try:
        index_attr = getattr(value, "Index", None)
    except Exception:
        index_attr = None
    if index_attr is not None and index_attr is not value:
        _collect_strings_from_cr2w_value(index_attr, out_strings, seen)


def _extract_scaleform_root_from_path(path_value: str) -> str:
    normalized = _normalize_virtual_path(path_value)
    if not normalized:
        return ""

    lower = normalized.lower()
    gui_token = "gameplay\\gui_new"
    idx = lower.find(gui_token)
    if idx >= 0:
        return normalized[:idx + len(gui_token)].rstrip("\\")
    if lower.endswith("\\gui_new"):
        return normalized.rstrip("\\")
    if lower.endswith("\\gui_new\\"):
        return normalized.rstrip("\\")
    return ""


def _extract_scaleform_roots_from_reddlc(reddlc_path: str):
    roots = set()
    try:
        from ..CR2W import CR2W_file

        cr2w_file = CR2W_file.read_CR2W(reddlc_path)
    except Exception:
        log.debug("Failed to read DLC scaleform mounter file: %s", reddlc_path, exc_info=True)
        return roots

    try:
        scaleform_chunks = cr2w_file.CHUNKS.GetObjectsOfType("CR4ScaleformContentDLCMounter")
    except Exception:
        scaleform_chunks = []

    roots_from_strings = set()
    for chunk in scaleform_chunks:
        strings = set()
        _collect_strings_from_cr2w_value(chunk, strings, set())
        for raw in strings:
            root = _extract_scaleform_root_from_path(raw)
            if root:
                roots.add(root)
                roots_from_strings.add(root)

    if scaleform_chunks and not roots_from_strings:
        dlc_name = os.path.basename(os.path.dirname(reddlc_path))
        if dlc_name:
            roots.add(_normalize_virtual_path(os.path.join("dlc", dlc_name, "data", "gameplay", "gui_new")))
    return roots


def _get_scaleform_content_roots(context):
    try:
        game_path = win_safe_path(bpy.path.abspath(get_game_path(context) or ""))
    except Exception:
        game_path = ""

    if (
        _browser_scaleform_root_cache.get("scanned")
        and _browser_scaleform_root_cache.get("game_path") == game_path
    ):
        return list(_browser_scaleform_root_cache.get("roots") or ())

    extra_roots = set()
    try:
        from .. import w3_asset_browser

        reddlc_files = list(w3_asset_browser._iter_top_level_reddlc_files(game_path))
        for reddlc_path in reddlc_files:
            extra_roots.update(_extract_scaleform_roots_from_reddlc(reddlc_path))
    except Exception:
        log.debug("Failed to scan DLC Scaleform roots for browser icon resolution", exc_info=True)

    ordered_roots = ["gameplay\\gui_new"]
    ordered_roots.extend(sorted(_normalize_virtual_path(root) for root in extra_roots if root))
    normalized_roots = tuple(dict.fromkeys(_normalize_virtual_path(root) for root in ordered_roots if root))
    _browser_scaleform_root_cache["game_path"] = game_path
    _browser_scaleform_root_cache["roots"] = normalized_roots
    _browser_scaleform_root_cache["scanned"] = True
    return list(normalized_roots)


def _expand_scaleform_icon_candidates(context, icon_path: str):
    raw_icon_path = _normalize_virtual_path(icon_path)
    if not raw_icon_path:
        return []

    candidates = []
    seen = set()

    def _add(path_value: str):
        normalized = _normalize_virtual_path(path_value)
        if not normalized:
            return
        key = normalized.lower()
        if key in seen:
            return
        seen.add(key)
        candidates.append(normalized)

    for scaleform_root in _get_scaleform_content_roots(context):
        _add(os.path.join(scaleform_root, raw_icon_path))

    return candidates


def _resolve_browser_image_abs_path(abs_path: str) -> str:
    abs_path = win_safe_path(abs_path or "")
    if not abs_path or not win_path_exists(abs_path):
        return ""

    ext = os.path.splitext(abs_path)[1].lower()
    if ext in {'.dds', '.png', '.jpg', '.jpeg', '.tga', '.bmp'}:
        return abs_path
    if ext == '.xbm':
        dds_path = os.path.splitext(abs_path)[0] + '.dds'
        if not win_path_exists(dds_path):
            try:
                convert_xbm_to_dds(abs_path)
            except Exception:
                return ""
        if win_path_exists(dds_path):
            return dds_path
    return ""


def _iter_preview_lookup_paths(file_path: str):
    normalized = _normalize_virtual_path(file_path)
    if not normalized:
        return []
    base, ext = os.path.splitext(normalized)
    candidates = [normalized]
    for alt_ext in ('.xbm', '.dds', '.png', '.tga', '.bmp'):
        candidate = base + alt_ext
        if candidate.lower() != normalized.lower():
            candidates.append(candidate)
    return candidates


def _resolve_preview_image_path(context, cache_type: str, file_path: str, loadmods: bool = False) -> str:
    """Resolve an image file suitable for previewing a browser asset icon."""
    search_path = _normalize_virtual_path(file_path)
    if not search_path:
        return ""

    temp_path = ""
    override_roots = []
    if is_disk_cache(cache_type):
        override_roots = get_repo_override_roots_for_item(context, cache_type, search_path)

    try:
        if override_roots:
            set_repo_override_roots(override_roots, read_only=True)

        with mod_loading_context(context, overwrite=False):
            if os.path.splitext(search_path)[1].lower() == '.w2cube':
                abs_path = ""
                try:
                    abs_path = repo_file(get_vanilla_path(search_path, loadmods))
                except Exception:
                    abs_path = ""
                if not abs_path or not win_path_exists(abs_path):
                    abs_path = get_uncook_abs_path(context, search_path, loadmods)
                if not abs_path or not win_path_exists(abs_path):
                    if is_disk_cache(cache_type):
                        abs_path = get_disk_abs_path(cache_type, search_path)
                if abs_path and win_path_exists(abs_path):
                    return build_w2cube_preview(abs_path) or ""
                return ""

            if is_w2ter_buffer_file(search_path):
                return build_w2ter_buffer_preview(context, cache_type, search_path) or ""

            if is_disk_cache(cache_type):
                for candidate_path in _iter_preview_lookup_paths(search_path):
                    resolved = _resolve_browser_image_abs_path(get_disk_abs_path(cache_type, candidate_path))
                    if resolved:
                        return resolved
                return ""

            if get_effective_cache_type(cache_type) == "Texture":
                for candidate_path in _iter_preview_lookup_paths(search_path):
                    items = _get_texture_cache_items(candidate_path, cache_type=cache_type, loadmods=loadmods)
                    if items:
                        final_item = items[-1] if isinstance(items, list) else items
                        temp_path = _make_browser_preview_temp_path(cache_type, candidate_path, ".dds")
                        if not win_path_exists(temp_path):
                            final_item.extract_to_file(temp_path)
                        if win_path_exists(temp_path):
                            return temp_path

                for candidate_path in _iter_preview_lookup_paths(search_path):
                    try:
                        abs_path = repo_file(candidate_path)
                    except Exception:
                        abs_path = ""
                    resolved = _resolve_browser_image_abs_path(abs_path)
                    if resolved:
                        return resolved
                return ""

            try:
                manager = LoadTextureManager(loadmods=loadmods)
                for candidate_path in _iter_preview_lookup_paths(search_path):
                    items = manager.find_item_by_path_name(candidate_path)
                    if items:
                        final_item = items[-1] if isinstance(items, list) else items
                        temp_path = _make_browser_preview_temp_path(cache_type, candidate_path, ".dds")
                        if not win_path_exists(temp_path):
                            final_item.extract_to_file(temp_path)
                        if win_path_exists(temp_path):
                            return temp_path
            except Exception:
                pass

            for candidate_path in _iter_preview_lookup_paths(search_path):
                try:
                    lookup_path = candidate_path
                    if loadmods and "\\" in candidate_path:
                        mod_name = candidate_path.split("\\", 1)[0]
                        lookup_path = strip_mod_prefix(candidate_path, mod_name)
                    abs_path = repo_file(lookup_path)
                except Exception:
                    abs_path = ""
                resolved = _resolve_browser_image_abs_path(abs_path)
                if resolved:
                    return resolved
            return ""
    finally:
        if override_roots:
            clear_repo_override_roots()


def _resolve_w2ent_icon_virtual_path(context, cache_type: str, item_path: str, loadmods: bool = False) -> str:
    normalized_item = _normalize_virtual_path(get_vanilla_path(item_path, loadmods) or item_path)
    if not normalized_item.lower().endswith(".w2ent"):
        return ""

    source_game = "w2" if cache_type == "Witcher 2 Data" else "w3"
    cache_key = (_BROWSER_W2ENT_ICON_CACHE_VERSION, source_game, normalized_item.lower())
    if cache_key in _browser_w2ent_icon_path_cache:
        return _browser_w2ent_icon_path_cache[cache_key]

    icon_path = ""
    try:
        from . import ui_equipment

        search_roots = []
        if source_game == "w2":
            search_roots = get_repo_override_roots_for_item(context, cache_type, normalized_item)
        ui_equipment.ensure_equipment_catalog_ready(
            source_game=source_game,
            search_roots=search_roots,
            context=context,
            require_browser_icon_fields=True,
        )
        raw_icon_path = ""
        identifier_candidates = [
            normalized_item,
            os.path.splitext(os.path.basename(normalized_item))[0],
        ]
        for identifier in identifier_candidates:
            raw_icon_path = str(
                ui_equipment.get_item_icon_path(identifier, source_game=source_game, strict=True) or ""
            )
            if raw_icon_path:
                break
        for candidate_path in _expand_scaleform_icon_candidates(context, raw_icon_path):
            preview_path = _resolve_preview_image_path(context, cache_type, candidate_path, loadmods=loadmods)
            if preview_path and win_path_exists(preview_path):
                icon_path = candidate_path
                break
    except Exception:
        icon_path = ""

    _cache_bounded_store(_browser_w2ent_icon_path_cache, cache_key, icon_path, max_entries=4096)
    return icon_path


def _get_browser_item_preview_target(context, cache_type: str, item_path: str, loadmods: bool = False):
    normalized_item = _normalize_virtual_path(item_path)
    if not normalized_item:
        return None
    if is_texture_file(normalized_item) or is_w2ter_buffer_file(normalized_item):
        return {
            "kind": "image",
            "target_path": normalized_item,
        }
    if normalized_item.lower().endswith(".w2ent"):
        icon_path = _resolve_w2ent_icon_virtual_path(context, cache_type, normalized_item, loadmods=loadmods)
        if icon_path:
            return {
                "kind": "entity_icon",
                "target_path": icon_path,
            }
    return None


def _add_browser_preview_button(layout, context, cache_type: str, item_path: str, loadmods: bool = False) -> bool:
    preview_target = _get_browser_item_preview_target(context, cache_type, item_path, loadmods=loadmods)
    if not preview_target:
        return False
    op = layout.operator("witcher.texture_preview", text="", icon='IMAGE_DATA')
    op.file_path = preview_target["target_path"]
    op.cache_type = cache_type
    return True


def _add_browser_preview_button_slot(layout, context, cache_type: str, item_path: str, loadmods: bool = False) -> bool:
    slot = layout.row(align=True)
    if _add_browser_preview_button(slot, context, cache_type, item_path, loadmods=loadmods):
        return True
    slot.enabled = False
    slot.label(text="", icon='IMAGE_DATA')
    return False


def _get_browser_item_icon_info(context, cache_type: str, item_path: str, loadmods: bool = False):
    type_label = get_browser_item_type_label(item_path, cache_type)
    preview_target = _get_browser_item_preview_target(context, cache_type, item_path, loadmods=loadmods)
    preview_path = ""
    cache_key = ""
    is_dummy = False

    if preview_target:
        target_path = preview_target["target_path"]
        cache_key = f"{cache_type}|{int(bool(loadmods))}|{preview_target['kind']}|{target_path.lower()}"
        preview_path = _browser_asset_preview_path_cache.get(cache_key, "")
        if not preview_path or not win_path_exists(preview_path):
            preview_path = _resolve_preview_image_path(context, cache_type, target_path, loadmods=loadmods)
            if preview_path and win_path_exists(preview_path):
                _cache_bounded_store(_browser_asset_preview_path_cache, cache_key, preview_path, max_entries=4096)
            else:
                preview_path = ""

    if not preview_path:
        preview_path = ensure_browser_dummy_icon_path(cache_type, item_path)
        cache_key = f"dummy|{cache_type}|{type_label}"
        is_dummy = True

    if not preview_path or not win_path_exists(preview_path):
        return {
            "icon_id": 0,
            "is_dummy": is_dummy,
            "type_label": type_label,
            "has_preview_target": bool(preview_target),
        }

    preview_key = f"{cache_key}|{preview_path.lower()}"
    pcoll = _get_browser_asset_preview_collection()
    try:
        icon = pcoll.get(preview_key)
    except Exception:
        icon = None
    if icon is None:
        try:
            icon = pcoll.load(preview_key, win_safe_path(preview_path), 'IMAGE')
        except Exception:
            icon = None

    icon_id = 0
    try:
        if icon is not None:
            icon_id = int(icon.icon_id)
    except Exception:
        icon_id = 0

    return {
        "icon_id": icon_id,
        "is_dummy": is_dummy,
        "type_label": type_label,
        "has_preview_target": bool(preview_target),
    }


def _ensure_browser_item_icon_id(context, cache_type: str, item_path: str, loadmods: bool = False) -> int:
    return int(_get_browser_item_icon_info(context, cache_type, item_path, loadmods=loadmods).get("icon_id", 0) or 0)


_sound_preview_state = {
    "cache_type": "",
    "item_path": "",
    "wav_path": "",
    "sound_name": "",
}
_sound_preview_handle = None
_sound_preview_device = None


def _tag_browser_redraw(context) -> None:
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
    except Exception:
        pass


def _touch_external_export_ui(context) -> None:
    browser = getattr(getattr(context, "scene", None), "witcher_file_browser", None)
    if browser is not None:
        try:
            browser.external_export_ui_ping = (int(getattr(browser, "external_export_ui_ping", 0) or 0) + 1) % 1000000
        except Exception:
            pass
    _tag_browser_redraw(context)


def _set_external_export_status(
    *,
    visible: bool,
    state: str,
    cache_type: str = "",
    folder_path: str = "",
    output_root: str = "",
    processed: int = 0,
    total: int = 0,
    done: int = 0,
    skipped: int = 0,
    errors: int = 0,
    message: str = "",
) -> None:
    _EXTERNAL_BROWSER_EXPORT_STATUS.update({
        "visible": bool(visible),
        "state": str(state or "idle"),
        "cache_type": str(cache_type or ""),
        "folder_path": str(folder_path or ""),
        "output_root": str(output_root or ""),
        "processed": int(processed or 0),
        "total": int(total or 0),
        "done": int(done or 0),
        "skipped": int(skipped or 0),
        "errors": int(errors or 0),
        "message": str(message or ""),
    })


def _sound_preview_matches(cache_type: str, item_path: str) -> bool:
    return (
        _sound_preview_state.get("cache_type") == (cache_type or "")
        and _sound_preview_state.get("item_path") == (item_path or "").replace("/", "\\")
    )


def _clear_sound_preview(context=None) -> None:
    global _sound_preview_handle, _sound_preview_state

    try:
        if _sound_preview_handle is not None:
            _sound_preview_handle.stop()
    except Exception:
        pass
    _sound_preview_handle = None

    sound_name = _sound_preview_state.get("sound_name") or ""
    if sound_name:
        try:
            sound_data = bpy.data.sounds.get(sound_name)
            if sound_data is not None and getattr(sound_data, "users", 0) == 0:
                bpy.data.sounds.remove(sound_data)
        except Exception:
            pass

    _sound_preview_state = {
        "cache_type": "",
        "item_path": "",
        "wav_path": "",
        "sound_name": "",
    }
    _tag_browser_redraw(context)


def _get_texture_cache_items(item_path: str, cache_type: str = "Texture", loadmods: bool = False):
    full_path_norm = (item_path or "").replace("/", "\\")
    if cache_type == EXTERNAL_TEXTURE_CACHE_TYPE:
        return _get_external_archive_items(cache_type, full_path_norm)
    manager = LoadTextureManager(loadmods=loadmods)
    return manager.find_item_by_path_name(full_path_norm)


def _get_sound_cache_items(item_path: str, cache_type: str = "Sound", loadmods: bool = False):
    full_path_norm = (item_path or "").replace("/", "\\")
    if cache_type == EXTERNAL_SOUND_CACHE_TYPE:
        return _get_external_archive_items(cache_type, full_path_norm)
    manager = LoadSoundManager(loadmods=loadmods)
    return manager.find_item_by_path_name(full_path_norm)


def _get_sound_item_and_export_path(context, item_path: str, loadmods: bool = False, cache_type: str = "Sound"):
    full_path_norm = (item_path or "").replace("/", "\\")
    items = _get_sound_cache_items(full_path_norm, cache_type=cache_type, loadmods=loadmods)
    if not items:
        return None, ""

    final_item = items[-1] if isinstance(items, list) else items
    mod_name = ""
    if cache_type != EXTERNAL_SOUND_CACHE_TYPE and loadmods and "\\" in full_path_norm:
        mod_name = full_path_norm.split("\\", 1)[0]

    item_name = getattr(final_item, 'name', None) or getattr(final_item, 'Name', full_path_norm)
    vanilla_name = strip_mod_prefix(item_name, mod_name)
    if vanilla_name == item_name and mod_name:
        vanilla_name = strip_mod_prefix(full_path_norm, mod_name)

    if cache_type == EXTERNAL_SOUND_CACHE_TYPE:
        export_path = _get_external_item_output_path(context, cache_type, vanilla_name, loadmods=False)
    else:
        export_path = repo_file(vanilla_name)
    return final_item, export_path


def ensure_sound_item_extracted(context, item_path: str, loadmods: bool = False, cache_type: str = "Sound") -> str:
    final_item, export_path = _get_sound_item_and_export_path(
        context,
        item_path,
        loadmods=loadmods,
        cache_type=cache_type,
    )
    if final_item is None or not export_path:
        return ""

    if not win_path_exists(export_path):
        if cache_type == EXTERNAL_SOUND_CACHE_TYPE:
            prep_root = _get_external_archive_output_root(context, cache_type, create=True)
        else:
            addon_prefs = get_all_addon_prefs(context)
            prep_root = addon_prefs.uncook_path if addon_prefs else ""
        if prepare_extraction_target(export_path, prep_root):
            written_path = final_item.extract_to_file(export_path)
            if written_path:
                export_path = written_path
    return export_path if win_path_exists(export_path) else ""


def _sound_wav_output_path(context, item_path: str) -> str:
    output_root = get_W3_OGG_PATH(context) or bpy.app.tempdir or ""
    if not output_root:
        output_root = os.path.join(Path.home(), "AppData", "Local", "Temp")
    rel_path = (item_path or "").replace("/", os.sep).replace("\\", os.sep).lstrip(os.sep)
    if not rel_path:
        rel_path = "sound_preview"
    return win_safe_path(
        os.path.join(output_root, "asset_browser_sound_cache", os.path.splitext(rel_path)[0] + ".wav")
    )


def ensure_sound_wav(context, sound_abs_path: str, item_path: str) -> str:
    sound_abs_path = win_safe_path(sound_abs_path or "")
    if not sound_abs_path or not win_path_exists(sound_abs_path):
        raise RuntimeError("Sound file is not available on disk.")

    vgmstream_path = get_vgmstream_path(context)
    if not os.path.isfile(vgmstream_path):
        raise RuntimeError(f"vgmstream executable not found: {vgmstream_path}")

    output_wav = _sound_wav_output_path(context, item_path)
    os.makedirs(os.path.dirname(output_wav), exist_ok=True)

    try:
        src_mtime = os.path.getmtime(sound_abs_path)
        wav_mtime = os.path.getmtime(output_wav) if os.path.exists(output_wav) else -1
    except Exception:
        src_mtime = -1
        wav_mtime = -1

    if wav_mtime >= src_mtime and win_path_exists(output_wav):
        return output_wav

    import subprocess

    command = [vgmstream_path, "-i"]
    if sound_abs_path.lower().endswith(".bnk"):
        command.extend(["-s", "1"])
    command.extend(["-o", output_wav, sound_abs_path])
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0 or not win_path_exists(output_wav):
        details = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(details or f"vgmstream conversion failed with exit code {completed.returncode}")
    return output_wav


def import_sound_to_timeline(context, item_path: str, loadmods: bool = False, cache_type: str = "Sound"):
    sound_abs_path = ensure_sound_item_extracted(
        context,
        item_path,
        loadmods=loadmods,
        cache_type=cache_type,
    )
    if not sound_abs_path:
        raise RuntimeError(f"Sound item not found: {item_path}")

    wav_path = ensure_sound_wav(context, sound_abs_path, item_path)

    from .ui_voice import _get_next_sound_channel, _get_sequence_editor_strips

    scene = context.scene
    if not scene.sequence_editor:
        scene.sequence_editor_create()
    strips = _get_sequence_editor_strips(scene.sequence_editor)
    if strips is None:
        raise RuntimeError("Blender sequence editor strips API is unavailable")

    if getattr(scene, "witcher_voice_replace_audio", False):
        sound_strips = [strip for strip in strips if strip.type == 'SOUND']
        for strip in sound_strips:
            strips.remove(strip)

    channel = 1 if getattr(scene, "witcher_voice_replace_audio", False) else _get_next_sound_channel(scene)
    frame_start = int(getattr(scene, "frame_current", 0))
    strip_name = Path(item_path.replace("\\", "/")).stem or Path(wav_path).stem
    soundstrip = strips.new_sound(strip_name, wav_path, channel=channel, frame_start=frame_start)
    soundstrip.frame_start = frame_start
    strip_end = int(getattr(soundstrip, "frame_final_end", frame_start))
    if strip_end > scene.frame_end:
        scene.frame_end = strip_end
    return soundstrip, wav_path


def play_sound_preview(context, cache_type: str, item_path: str, loadmods: bool = False) -> str:
    global _sound_preview_device, _sound_preview_handle, _sound_preview_state

    sound_abs_path = ensure_sound_item_extracted(
        context,
        item_path,
        loadmods=loadmods,
        cache_type=cache_type,
    )
    if not sound_abs_path:
        raise RuntimeError(f"Sound item not found: {item_path}")

    wav_path = ensure_sound_wav(context, sound_abs_path, item_path)

    try:
        import aud
    except Exception as exc:
        raise RuntimeError(f"Blender aud module is unavailable: {exc}") from exc

    _clear_sound_preview(context)

    if _sound_preview_device is None:
        _sound_preview_device = aud.Device()

    sound_data = bpy.data.sounds.load(wav_path, check_existing=False)
    sound_factory = aud.Sound(wav_path)
    _sound_preview_handle = _sound_preview_device.play(sound_factory)
    _sound_preview_state = {
        "cache_type": cache_type or "",
        "item_path": (item_path or "").replace("/", "\\"),
        "wav_path": wav_path,
        "sound_name": getattr(sound_data, "name", ""),
    }
    _tag_browser_redraw(context)
    return wav_path

def ensure_bundle_item_extracted(context, full_path, loadmods=False) -> str:
    manager = LoadBundleManager(loadmods=loadmods)
    full_path_norm = full_path.replace("/", "\\")
    items = manager.Items.get(full_path_norm) or manager.find_item_by_hash(full_path_norm)

    if not items and loadmods:
        vanilla_path = get_vanilla_path(full_path_norm, loadmods)
        if vanilla_path:
            suffix = "\\" + vanilla_path.lower()
            for key, value in manager.Items.items():
                if isinstance(key, str) and key.replace("/", "\\").lower().endswith(suffix):
                    items = value
                    break

    if not items:
        return ""

    final_item = items[-1] if isinstance(items, list) else items
    item_name = getattr(final_item, 'name', None) or getattr(final_item, 'Name', full_path_norm)

    mod_name = ""
    if loadmods and "\\" in full_path_norm:
        mod_name = full_path_norm.split("\\", 1)[0]

    vanilla_name = strip_mod_prefix(item_name, mod_name)
    if vanilla_name == item_name and mod_name:
        vanilla_name = strip_mod_prefix(full_path_norm, mod_name)

    export_path = repo_file(vanilla_name)
    if not win_path_exists(export_path):
        addon_prefs = get_all_addon_prefs(context)
        uncook_path = addon_prefs.uncook_path
        if prepare_extraction_target(export_path, uncook_path):
            final_item.extract_to_file(export_path)
    return export_path if win_path_exists(export_path) else ""

def resolve_w2ter_buffer_abs_path(context, cache_type, file_path, loadmods=False) -> str:
    if is_disk_cache(cache_type):
        abs_path = get_disk_abs_path(cache_type, file_path)
        return abs_path if abs_path and win_path_exists(abs_path) else ""

    abs_path = get_uncook_abs_path(context, file_path, loadmods)
    if abs_path and win_path_exists(abs_path):
        return abs_path

    if cache_type == "Bundle":
        return ensure_bundle_item_extracted(context, file_path, loadmods)

    return ""

def _save_preview_png(path: str, rgba_u8: np.ndarray) -> bool:
    try:
        rgba = np.asarray(rgba_u8, dtype=np.uint8)
        if rgba.ndim != 3 or rgba.shape[2] != 4:
            raise ValueError(f"Expected RGBA uint8 array, got shape {getattr(rgba, 'shape', None)}")

        height, width, _channels = rgba.shape
        parent_dir = os.path.dirname(path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)

        def _png_chunk(chunk_type: bytes, chunk_data: bytes) -> bytes:
            crc = zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF
            return (
                struct.pack(">I", len(chunk_data))
                + chunk_type
                + chunk_data
                + struct.pack(">I", crc)
            )

        raw_rows = bytearray()
        for row_index in range(height):
            raw_rows.append(0)  # PNG filter type 0
            raw_rows.extend(rgba[row_index].tobytes())

        png_bytes = bytearray(b"\x89PNG\r\n\x1a\n")
        png_bytes.extend(
            _png_chunk(
                b"IHDR",
                struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0),
            )
        )
        png_bytes.extend(_png_chunk(b"IDAT", zlib.compress(bytes(raw_rows), level=6)))
        png_bytes.extend(_png_chunk(b"IEND", b""))

        with open(path, "wb") as handle:
            handle.write(png_bytes)
        return True
    except Exception:
        log.debug("Failed to write preview PNG: %s", path, exc_info=True)
        return False

def build_w2ter_buffer_preview(context, cache_type, file_path) -> str:
    loadmods = context.scene.witcher_file_browser.loadmods if context and hasattr(context.scene, "witcher_file_browser") else False
    abs_path = resolve_w2ter_buffer_abs_path(context, cache_type, file_path, loadmods)
    if not abs_path or not win_path_exists(abs_path):
        return ""

    info = terrain_w2ter.parse_tile_filename(abs_path)
    if not info or info.buffer_index is None:
        return ""

    buffer_index = info.buffer_index
    label = terrain_w2ter.get_w2ter_buffer_label(buffer_index)

    import tempfile
    temp_dir = os.path.join(tempfile.gettempdir(), "witcher_preview", "w2ter")
    os.makedirs(temp_dir, exist_ok=True)

    base_name = os.path.basename(abs_path)
    safe_label = label.replace(" ", "_") if label else f"buffer{buffer_index}"

    if buffer_index >= 3:
        rgba = terrain_w2ter.decode_tintmap_file_to_rgba(abs_path, target_res_px=info.res)
        if rgba is None:
            return ""
        preview_path = os.path.join(temp_dir, f"{base_name}.{safe_label}.png")
        src_mtime = win_path_getmtime(abs_path)
        if not win_path_exists(preview_path) or win_path_getmtime(preview_path) < src_mtime:
            if not _save_preview_png(preview_path, rgba):
                return ""
        return preview_path

    data = np.fromfile(win_safe_path(abs_path), dtype="<u2")
    if data.size != info.res * info.res:
        return ""
    tile = data.reshape((info.res, info.res))

    if buffer_index == 1:
        minv = int(tile.min())
        maxv = int(tile.max())
        if maxv > minv:
            gray = ((tile - minv) * 255 // (maxv - minv)).astype(np.uint8)
        else:
            gray = np.zeros_like(tile, dtype=np.uint8)
        rgb = np.stack([gray, gray, gray], axis=2)
    elif buffer_index == 2:
        r = ((tile & 0x1F) * 255 // 31).astype(np.uint8)
        g = (((tile >> 5) & 0x1F) * 255 // 31).astype(np.uint8)
        b = (((tile >> 10) & 0x3F) * 255 // 63).astype(np.uint8)
        rgb = np.stack([r, g, b], axis=2)
    else:
        return ""

    alpha = np.full((info.res, info.res, 1), 255, dtype=np.uint8)
    rgba = np.concatenate([rgb, alpha], axis=2)

    preview_path = os.path.join(temp_dir, f"{base_name}.{safe_label}.png")
    src_mtime = win_path_getmtime(abs_path)
    if not win_path_exists(preview_path) or win_path_getmtime(preview_path) < src_mtime:
        if not _save_preview_png(preview_path, rgba):
            return ""
    return preview_path

def get_bundle_item_size(cache_type, item_path, loadmods=False):
    """Get the uncompressed size of an item from the appropriate cache manager.
    Returns 0 if not found.
    """
    try:
        if cache_type == EXTERNAL_BUNDLE_CACHE_TYPE:
            session = get_external_archive_session(cache_type)
            if session:
                items = session["items"].get(item_path)
                if items:
                    final_item = items[-1] if isinstance(items, list) else items
                    return getattr(final_item, 'size', 0) or 0
        elif cache_type == "Bundle":
            manager = LoadBundleManager(loadmods=loadmods)
            items = manager.find_item_by_hash(item_path)
            if not items and loadmods:
                # Fallback: try to resolve mod-prefixed entries by vanilla path suffix
                vanilla_path = get_vanilla_path(item_path, loadmods)
                if vanilla_path:
                    suffix = "\\" + vanilla_path
                    for key, value in manager.Items.items():
                        if isinstance(key, str) and key.replace("/", "\\").endswith(suffix):
                            items = value
                            break
            if items:
                final_item = items[-1] if isinstance(items, list) else items
                return getattr(final_item, 'size', 0) or 0
        elif cache_type == EXTERNAL_COLLISION_CACHE_TYPE:
            session = get_external_archive_session(cache_type)
            if session:
                items = session["items"].get(item_path)
                if items:
                    final_item = items[-1] if isinstance(items, list) else items
                    return getattr(final_item, 'Size', 0) or getattr(final_item, 'size', 0) or 0
        elif cache_type == EXTERNAL_SOUND_CACHE_TYPE:
            session = get_external_archive_session(cache_type)
            if session:
                items = session["items"].get(item_path)
                if items:
                    final_item = items[-1] if isinstance(items, list) else items
                    return getattr(final_item, 'Size', 0) or getattr(final_item, 'size', 0) or 0
        elif cache_type == "Collision":
            manager = LoadCollisionManager(loadmods=loadmods)
            items = manager.find_item_by_path_name(item_path)
            if items:
                final_item = items[-1] if isinstance(items, list) else items
                return getattr(final_item, 'Size', 0) or getattr(final_item, 'size', 0) or 0
        elif cache_type == "Sound":
            manager = LoadSoundManager(loadmods=loadmods)
            items = manager.find_item_by_path_name(item_path)
            if items:
                final_item = items[-1] if isinstance(items, list) else items
                return getattr(final_item, 'Size', 0) or getattr(final_item, 'size', 0) or 0
    except Exception:
        pass
    return 0

def _format_size_bytes(size_bytes: int) -> str:
    try:
        value = float(max(0, int(size_bytes)))
    except Exception:
        return "?"
    units = ("B", "KB", "MB", "GB", "TB")
    idx = 0
    while value >= 1024.0 and idx < len(units) - 1:
        value /= 1024.0
        idx += 1
    return f"{value:.1f} {units[idx]}" if idx > 0 else f"{int(value)} {units[idx]}"

def _safe_int(value) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0

def _resolve_item_for_stats(cache_type: str, item_path: str, loadmods: bool = False):
    item_key = (item_path or "").replace("/", "\\")

    if cache_type == EXTERNAL_BUNDLE_CACHE_TYPE:
        session = get_external_archive_session(cache_type)
        if session:
            items = session.get("items", {}).get(item_key)
            return items[-1] if isinstance(items, list) and items else items
        return None

    if cache_type == EXTERNAL_COLLISION_CACHE_TYPE:
        session = get_external_archive_session(cache_type)
        if session:
            items = session.get("items", {}).get(item_key)
            return items[-1] if isinstance(items, list) and items else items
        return None

    if cache_type == EXTERNAL_TEXTURE_CACHE_TYPE:
        session = get_external_archive_session(cache_type)
        if session:
            items = session.get("items", {}).get(item_key)
            return items[-1] if isinstance(items, list) and items else items
        return None

    if cache_type == EXTERNAL_SOUND_CACHE_TYPE:
        session = get_external_archive_session(cache_type)
        if session:
            items = session.get("items", {}).get(item_key)
            return items[-1] if isinstance(items, list) and items else items
        return None

    if cache_type == "Bundle":
        manager = LoadBundleManager(loadmods=loadmods)
        items = manager.find_item_by_hash(item_key)
        if not items and loadmods:
            vanilla_path = get_vanilla_path(item_key, loadmods)
            if vanilla_path:
                suffix = "\\" + vanilla_path.replace("/", "\\").lower()
                for key, value in manager.Items.items():
                    if isinstance(key, str) and key.replace("/", "\\").lower().endswith(suffix):
                        items = value
                        break
        return items[-1] if isinstance(items, list) and items else items

    if cache_type == "Collision":
        manager = LoadCollisionManager(loadmods=loadmods)
        items = manager.find_item_by_path_name(item_key)
        return items[-1] if isinstance(items, list) and items else items

    if cache_type == "Texture":
        manager = LoadTextureManager(loadmods=loadmods)
        items = manager.find_item_by_path_name(item_key)
        return items[-1] if isinstance(items, list) and items else items

    if cache_type == "Sound":
        manager = LoadSoundManager(loadmods=loadmods)
        items = manager.find_item_by_path_name(item_key)
        return items[-1] if isinstance(items, list) and items else items

    if cache_type == "Speech":
        if loadmods:
            return None
        manager = LoadSpeechManager()
        items = manager.find_item_by_hash(item_key)
        return items[-1] if isinstance(items, list) and items else items

    return None

def _item_compression_type(item) -> str:
    if item is None:
        return "Unknown"
    try:
        ctype = getattr(item, "compression_type", None)
        if callable(ctype):
            ctype = ctype()
        if ctype:
            return str(ctype)
    except Exception:
        pass
    try:
        ctype = getattr(item, "CompressionType", None)
        if callable(ctype):
            ctype = ctype()
        if ctype:
            return str(ctype)
    except Exception:
        pass
    return "Unknown"

def get_cache_item_stats(context, cache_type: str, item_path: str, loadmods: bool = False) -> dict:
    item_key = (item_path or "").replace("/", "\\")
    stats = {
        "cache_type": cache_type or "",
        "item_path": item_key,
        "found": False,
        "is_disk": False,
        "compression": "Unknown",
        "compression_code": "",
        "is_compressed": None,
        "size_uncompressed": 0,
        "size_compressed": 0,
        "size_on_disk": 0,
    }

    if is_disk_cache(cache_type):
        abs_path = get_disk_abs_path(cache_type, item_key)
        file_exists = bool(abs_path and win_path_exists(abs_path))
        size_on_disk = _safe_int(win_path_getsize(abs_path)) if file_exists else 0
        stats.update({
            "found": file_exists,
            "is_disk": True,
            "compression": "N/A (disk file)",
            "is_compressed": False,
            "size_uncompressed": size_on_disk,
            "size_compressed": size_on_disk,
            "size_on_disk": size_on_disk,
        })
        return stats

    item = _resolve_item_for_stats(cache_type, item_key, loadmods=loadmods)
    if item is None:
        return stats

    size_uncompressed = _safe_int(
        getattr(item, "size", None)
        or getattr(item, "Size", None)
        or getattr(item, "UncompressedSize", None)
    )
    size_compressed = _safe_int(
        getattr(item, "zsize", None)
        or getattr(item, "ZSize", None)
        or getattr(item, "CompressedSize", None)
    )

    compression = _item_compression_type(item)
    compression_code = getattr(item, "compression", "")
    compression_code = str(compression_code) if compression_code not in ("", None) else ""

    c_norm = compression.strip().lower()
    if c_norm in {"none", "n/a (disk file)", "n/a"}:
        is_compressed = False
    elif c_norm == "unknown":
        is_compressed = bool(size_compressed > 0 and size_uncompressed > 0 and size_compressed < size_uncompressed)
    else:
        is_compressed = True

    stats.update({
        "found": True,
        "compression": compression,
        "compression_code": compression_code,
        "is_compressed": is_compressed,
        "size_uncompressed": size_uncompressed,
        "size_compressed": size_compressed,
    })

    file_exists, size_on_disk = get_file_info(context, cache_type, item_key, loadmods=loadmods)
    if file_exists:
        stats["size_on_disk"] = _safe_int(size_on_disk)

    return stats

def _expected_mod_from_item_path(item_path: str, loadmods: bool) -> str:
    if not loadmods or not item_path:
        return ""
    norm = item_path.replace("/", "\\")
    if "\\" not in norm:
        return ""
    return norm.split("\\", 1)[0]

def get_status_icon(context, cache_type, item_path, loadmods=False):
    """Return status icon id for an asset row."""
    file_exists, file_size = get_file_info(context, cache_type, item_path, loadmods=loadmods)
    if not file_exists:
        return "BLANK1"
    if is_disk_cache(cache_type):
        return "CHECKMARK"

    bundle_size = get_bundle_item_size(cache_type, item_path, loadmods=loadmods)
    if bundle_size > 0 and file_size != bundle_size:
        return "ERROR"

    source_label = get_source_label(context, item_path, loadmods=loadmods, cache_type=cache_type)
    if source_label.startswith("mod:"):
        source_mod = source_label[4:]
        expected_mod = _expected_mod_from_item_path(item_path, loadmods)
        # In vanilla browsing, a mod-sourced extracted file is a mismatch against vanilla.
        if not loadmods:
            return "ERROR"
        # In mod browsing, mismatch if extracted source is from a different mod.
        if expected_mod and source_mod and expected_mod.lower() != source_mod.lower():
            return "ERROR"
    return "CHECKMARK"

# Maps collision cache paths to their output extensions (from Comtype)
# e.g., "path/to/file.w2mesh" -> ".nxs"
collision_extension_map: dict = {}

# Search result cache to avoid expensive re-searches on every UI redraw
_search_cache = {
    'query': '',
    'cache_type': '',  # "" for global, or specific cache type
    'loadmods': False,
    'results': [],
}

def get_cached_search_results(query, cache_type, folder_struct, loadmods=False):
    """Return cached search results, or perform search if query changed."""
    global _search_cache

    if (_search_cache['query'] == query
            and _search_cache['cache_type'] == cache_type
            and _search_cache['loadmods'] == loadmods):
        return _search_cache['results']

    # Perform the search
    MAX_RESULTS = 100
    results = []

    if cache_type:
        # Search within specific cache (uses folder_structure.index)
        results = folder_struct.search_items(query, max_results=MAX_RESULTS)
    else:
        # Global search across all caches - do this ONCE
        for ct, loader in [
            ("Bundle", lambda: LoadBundleManager(loadmods=loadmods)),
            ("Collision", lambda: LoadCollisionManager(loadmods=loadmods)),
            ("Texture", lambda: LoadTextureManager(loadmods=loadmods)),
            ("Sound", lambda: LoadSoundManager(loadmods=loadmods)),
        ]:
            try:
                manager = loader()
                tokens = query.lower().replace('_', ' ').split()
                for key in manager.Items.keys():
                    key_str = str(key) if not isinstance(key, str) else key
                    if _should_skip_buffer_name(key_str):
                        continue
                    normalized = key_str.lower().replace('_', ' ')
                    if all(token in normalized for token in tokens):
                        results.append((ct, key_str))
                        if len(results) >= MAX_RESULTS * 4:
                            break
            except Exception as e:
                log.error("Failed to search %s: %s", ct, e)

    _search_cache['query'] = query
    _search_cache['cache_type'] = cache_type
    _search_cache['loadmods'] = loadmods
    _search_cache['results'] = results
    return results

def clear_search_cache():
    """Clear the search cache."""
    global _search_cache
    _search_cache = {'query': '', 'cache_type': '', 'loadmods': False, 'results': []}

def refresh_mod_cache_managers():
    """Force rebuild of mod cache managers so removed mods disappear immediately."""
    clear_mod_index_cache()
    try:
        LoadBundleManager(loadmods=True, reset_cache=True)
    except Exception as e:
        log.error("Failed to refresh mod bundle cache: %s", e)
    try:
        LoadCollisionManager(loadmods=True, do_reload=True)
    except Exception as e:
        log.error("Failed to refresh mod collision cache: %s", e)
    try:
        LoadTextureManager(loadmods=True, do_reload=True)
    except Exception as e:
        log.error("Failed to refresh mod texture cache: %s", e)
    try:
        LoadSoundManager(loadmods=True, do_reload=True)
    except Exception as e:
        log.error("Failed to refresh mod sound cache: %s", e)

# Navigation history for back/forward
_nav_history = []  # List of (cache_type, folder) tuples
_nav_index = -1
_nav_updating = False  # Prevent recursive history updates

def add_to_nav_history(cache_type, folder):
    """Add current location to navigation history."""
    global _nav_history, _nav_index, _nav_updating
    if _nav_updating:
        return
    current = (cache_type, folder)
    # Don't add duplicates consecutively
    if _nav_history and _nav_index >= 0 and _nav_history[_nav_index] == current:
        return
    # Truncate forward history when navigating to new location
    _nav_history = _nav_history[:_nav_index + 1]
    _nav_history.append(current)
    _nav_index = len(_nav_history) - 1
    # Limit history size
    if len(_nav_history) > 50:
        _nav_history = _nav_history[-50:]
        _nav_index = len(_nav_history) - 1

def can_go_back():
    return _nav_index > 0

def can_go_forward():
    return _nav_index < len(_nav_history) - 1

def clear_nav_history():
    global _nav_history, _nav_index
    _nav_history = []
    _nav_index = -1

def _should_show_terrain_tools(current_folder: str, folder_items) -> bool:
    if not current_folder:
        return False
    if os.path.basename(current_folder).lower() == "terrain_tiles":
        return True
    for item in folder_items:
        if item.get("is_folder"):
            continue
        if is_w2ter_tile_file(item.get("name", "")):
            return True
    return False

def _collect_w2ter_items_in_folder(folder_path: str):
    folder_items = folder_structure.get_items(folder_path)
    base_paths = []
    buffer_paths = []
    for item in folder_items:
        if item.get("is_folder"):
            continue
        name = item.get("name", "")
        full_path = (folder_path + "\\" + name) if folder_path else name
        if is_w2ter_buffer_file(name):
            buffer_paths.append(full_path)
        elif name.lower().endswith(".w2ter"):
            base_paths.append(full_path)
    return base_paths, buffer_paths

def _get_terrain_output_dir(folder_abs: str) -> Tuple[str, str]:
    if not folder_abs:
        return "", "terrain"
    base_name = os.path.basename(folder_abs)
    if base_name.lower() == "terrain_tiles":
        output_dir = os.path.dirname(folder_abs)
        hub_name = os.path.basename(output_dir) or "terrain"
        return output_dir, hub_name
    return folder_abs, base_name or "terrain"

def _find_w2w_path(folder_abs: str) -> str:
    if not folder_abs:
        return ""
    if os.path.basename(folder_abs).lower() == "terrain_tiles":
        search_root = os.path.dirname(folder_abs)
    else:
        search_root = folder_abs
    if not search_root or not win_path_isdir(search_root):
        return ""
    try:
        for entry in os.scandir(win_safe_path(search_root)):
            if entry.is_file() and entry.name.lower().endswith(".w2w"):
                return entry.path
    except Exception:
        return ""
    candidate = os.path.join(search_root, os.path.basename(search_root) + ".w2w")
    if win_path_exists(candidate):
        return candidate
    return ""

def _infer_tiles_from_w2w(world) -> Tuple[Optional[int], Optional[int]]:
    res = getattr(world, "tileRes", None)
    clipmap_size = getattr(world, "clipmapSize", 0) or 0
    clip_size = getattr(world, "clipSize", 0) or 0

    def _calc_tiles(val):
        if not val:
            return None
        if res and val % res == 0:
            return int(val // res)
        if val <= 256:
            return int(val)
        return None

    tiles = _calc_tiles(clipmap_size) or _calc_tiles(clip_size)
    if tiles and tiles > 0:
        return res, tiles
    return res, None

def _resolve_w2w_path(context, cache_type, folder_path, folder_abs, loadmods) -> str:
    """Resolve the .w2w world file path for a terrain_tiles folder."""
    if cache_type == "Bundle":
        if folder_path and os.path.basename(folder_path).lower() == "terrain_tiles":
            parent_virtual = os.path.dirname(folder_path)
            hub_name = os.path.basename(parent_virtual)
            if hub_name:
                w2w_virtual = os.path.join(parent_virtual, hub_name + ".w2w")
                w2w_path = ensure_bundle_item_extracted(context, w2w_virtual, loadmods)
                if w2w_path:
                    return w2w_path
                vanilla = get_vanilla_path(w2w_virtual, loadmods)
                candidate = repo_file(vanilla)
                if candidate and win_path_exists(candidate):
                    return candidate
    else:
        return _find_w2w_path(folder_abs)
    return ""

def _get_w2w_world_data(context, cache_type, folder_path, folder_abs, loadmods):
    """Load and return the full WORLD object from the .w2w file, or None."""
    w2w_path = _resolve_w2w_path(context, cache_type, folder_path, folder_abs, loadmods)
    if not w2w_path:
        return None
    try:
        from ..CR2W import CR2W_reader
        return CR2W_reader.load_w2w(w2w_path)
    except Exception as e:
        log.error("Failed to read w2w for terrain tiles: %s", e)
        return None

def _get_w2w_grid_params(
    context,
    cache_type: str,
    folder_path: str,
    folder_abs: str,
    loadmods: bool,
) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    world = _get_w2w_world_data(context, cache_type, folder_path, folder_abs, loadmods)
    if not world:
        return None, None, None
    res, tiles = _infer_tiles_from_w2w(world)
    if tiles:
        return res, tiles, tiles
    return res, None, None

def combine_w2ter_folder(context, cache_type, folder_path, loadmods=False) -> Dict[str, object]:
    if not is_disk_cache(cache_type) and cache_type != "Bundle":
        return {"outputs": [], "output_dir": "", "info": {"error": f"Unsupported cache: {cache_type}"}}
    base_paths, buffer_paths = _collect_w2ter_items_in_folder(folder_path)
    if not buffer_paths:
        return {"outputs": [], "output_dir": "", "info": {"error": "No w2ter buffers found"}}

    if cache_type == "Bundle":
        for base in base_paths:
            ensure_bundle_item_extracted(context, base, loadmods)

    abs_buffer_paths = []
    for buf in buffer_paths:
        abs_path = resolve_w2ter_buffer_abs_path(context, cache_type, buf, loadmods)
        if abs_path:
            abs_buffer_paths.append(abs_path)

    if not abs_buffer_paths:
        return {"outputs": [], "output_dir": "", "info": {"error": "Missing buffer files on disk"}}

    if is_disk_cache(cache_type):
        folder_abs = os.path.dirname(abs_buffer_paths[0]) if abs_buffer_paths else ""
    else:
        folder_abs = get_uncook_abs_path(context, folder_path, loadmods)

    output_dir, hub_name = _get_terrain_output_dir(folder_abs)
    if not output_dir:
        return {"outputs": [], "output_dir": "", "info": {"error": "Failed to resolve output folder"}}
    res_override, x_tiles_override, y_tiles_override = _get_w2w_grid_params(
        context,
        cache_type,
        folder_path,
        folder_abs,
        loadmods,
    )
    result = terrain_w2ter.combine_w2ter_tiles(
        abs_buffer_paths,
        output_dir,
        hub_name,
        res_override=res_override,
        x_tiles_override=x_tiles_override,
        y_tiles_override=y_tiles_override,
    )
    result["output_dir"] = output_dir
    result["hub_name"] = hub_name
    result["folder_abs"] = folder_abs
    return result


def import_terrain_fullmap_from_folder(
    context, cache_type, folder_path, loadmods, multires_level
) -> Dict[str, object]:
    """Import terrain as one full map object using combined PNG outputs + geo nodes."""
    result = combine_w2ter_folder(context, cache_type, folder_path, loadmods)
    outputs = result.get("outputs", [])
    output_dir = result.get("output_dir", "")
    hub_name = result.get("hub_name", "")
    folder_abs = result.get("folder_abs", "")
    info = result.get("info", {})

    if not outputs or not output_dir or not hub_name:
        return {"object_name": "", "hub_name": "", "error": info.get("error", "Failed to combine terrain tiles")}

    heightmap_png = os.path.join(output_dir, f"{hub_name}.heightmap.png")
    colormap_png = os.path.join(output_dir, f"{hub_name}.overlay.png")
    if not os.path.isfile(heightmap_png):
        return {"object_name": "", "hub_name": hub_name, "error": f"Missing {hub_name}.heightmap.png"}
    if not os.path.isfile(colormap_png):
        return {"object_name": "", "hub_name": hub_name, "error": f"Missing {hub_name}.overlay.png"}

    # Resolve world settings from .w2w when available.
    world = _get_w2w_world_data(context, cache_type, folder_path, folder_abs, loadmods)
    if world:
        terrain_size = getattr(world, "terrainSize", 2000.0)
        lowest_elevation = getattr(world, "lowestElevation", 0.0)
        highest_elevation = getattr(world, "highestElevation", 100.0)
        world_name = getattr(world, "worldName", None) or hub_name
    else:
        terrain_size = 2000.0
        lowest_elevation = 0.0
        highest_elevation = 100.0
        world_name = hub_name
        log.warning("No .w2w file found, using default terrain parameters for full map import")

    from ..importers import import_w2w
    obj = import_w2w.import_combined_terrain_full_map(
        hub_name=hub_name,
        heightmap_path=heightmap_png,
        colormap_path=colormap_png,
        terrain_size=terrain_size,
        lowest_elevation=lowest_elevation,
        highest_elevation=highest_elevation,
        multires_level=multires_level,
        world_name=world_name,
    )
    if not obj:
        return {"object_name": "", "hub_name": hub_name, "error": "Failed to create full-map terrain object"}

    return {"object_name": obj.name, "hub_name": hub_name}


def import_terrain_tiles_from_folder(
    context, cache_type, folder_path, loadmods, multires_level
) -> Dict[str, object]:
    """Extract w2ter tile buffers and import them as individual Blender objects."""
    if not is_disk_cache(cache_type) and cache_type != "Bundle":
        return {"tile_count": 0, "error": f"Unsupported cache: {cache_type}"}

    base_paths, buffer_paths = _collect_w2ter_items_in_folder(folder_path)
    if not buffer_paths:
        return {"tile_count": 0, "error": "No w2ter buffers found"}

    # Extract base .w2ter files if from bundle
    if cache_type == "Bundle":
        for base in base_paths:
            ensure_bundle_item_extracted(context, base, loadmods)

    # Resolve all buffer paths to disk
    abs_buffer_paths = []
    for buf in buffer_paths:
        abs_path = resolve_w2ter_buffer_abs_path(context, cache_type, buf, loadmods)
        if abs_path:
            abs_buffer_paths.append(abs_path)

    if not abs_buffer_paths:
        return {"tile_count": 0, "error": "Missing buffer files on disk"}

    # Get folder abs path for w2w resolution
    if is_disk_cache(cache_type):
        folder_abs = os.path.dirname(abs_buffer_paths[0]) if abs_buffer_paths else ""
    else:
        folder_abs = get_uncook_abs_path(context, folder_path, loadmods)

    # Get world data from .w2w
    world = _get_w2w_world_data(context, cache_type, folder_path, folder_abs, loadmods)
    if world:
        terrain_size = getattr(world, "terrainSize", 2000.0)
        lowest_elevation = getattr(world, "lowestElevation", 0.0)
        highest_elevation = getattr(world, "highestElevation", 100.0)
    else:
        terrain_size = 2000.0
        lowest_elevation = 0.0
        highest_elevation = 100.0
        log.warning("No .w2w file found, using default terrain parameters")

    # Collect and organize tiles
    tile_info = terrain_w2ter.collect_tile_buffers(abs_buffer_paths)
    tiles = tile_info.get("tiles", {})
    x_tiles = tile_info.get("x_tiles", 0)
    y_tiles = tile_info.get("y_tiles", 0)

    # Override grid dimensions from w2w if available
    if world:
        res_w2w, tiles_w2w = _infer_tiles_from_w2w(world)
        if tiles_w2w and tiles_w2w > x_tiles:
            x_tiles = tiles_w2w
        if tiles_w2w and tiles_w2w > y_tiles:
            y_tiles = tiles_w2w

    if x_tiles == 0 or y_tiles == 0:
        return {"tile_count": 0, "error": "Could not determine tile grid dimensions"}

    # Collect raw heightmap buffers and generate overlay PNGs
    tile_res_detected = tile_info.get("res") or 256
    tile_heightmap_buffers = {}
    tile_overlays = {}

    if 1 in tiles:
        for (x, y), path in tiles[1].items():
            tile_heightmap_buffers[(x, y)] = path

    if 2 in tiles:
        for (x, y), path in tiles[2].items():
            info = terrain_w2ter.parse_tile_filename(path)
            if not info:
                continue
            overlay_path = path + ".overlay.png"
            # Always regenerate to avoid stale cached overlays from older orientation logic.
            try:
                terrain_w2ter._tile_texture_pngs(path, info)
            except Exception:
                pass
            if win_path_exists(overlay_path):
                tile_overlays[(x, y)] = overlay_path

    if not tile_heightmap_buffers:
        return {"tile_count": 0, "error": "No heightmap tiles found (buffer 1)"}

    # Determine hub name
    output_dir, hub_name = _get_terrain_output_dir(folder_abs)

    # Import tiles
    from ..importers import import_w2w
    empty, count = import_w2w.do_import_terrain_tiles(
        tile_heightmap_buffers=tile_heightmap_buffers,
        tile_overlays=tile_overlays,
        x_tiles=x_tiles,
        y_tiles=y_tiles,
        tile_res=tile_res_detected,
        terrain_size=terrain_size,
        lowest_elevation=lowest_elevation,
        highest_elevation=highest_elevation,
        multires_level=multires_level,
        hub_name=hub_name,
    )

    return {"tile_count": count, "hub_name": hub_name}


def save_browser_state(context, cache_type, folder):
    """Save browser state to addon preferences for cross-session persistence."""
    if is_external_cache(cache_type):
        return
    try:
        addon_prefs = get_all_addon_prefs(context)
        addon_prefs.browser_last_cache_type = cache_type
        addon_prefs.browser_last_folder = folder
    except Exception:
        pass  # Silently fail if addon prefs not available


def load_browser_state(context):
    """Load browser state from addon preferences."""
    try:
        addon_prefs = get_all_addon_prefs(context)
        return addon_prefs.browser_last_cache_type, addon_prefs.browser_last_folder
    except Exception:
        return "", ""


import json
import time

MAX_RECENT_ITEMS = 20
RECENT_KIND_IMPORT = "import"
RECENT_KIND_EXTERNAL_ARCHIVE = "external_archive"


def _recent_entry_key(entry):
    return (
        entry.get("kind", RECENT_KIND_IMPORT),
        entry.get("cache_type", ""),
        entry.get("path", ""),
        entry.get("archive_path", "") if is_external_cache(entry.get("cache_type", "")) else "",
    )


def _normalize_recent_entry(entry):
    if not isinstance(entry, dict):
        return None

    path = str(entry.get("path", "") or "")
    cache_type = str(entry.get("cache_type", "") or "")
    if not path or not cache_type:
        return None

    normalized = {
        "path": path,
        "cache_type": cache_type,
        "timestamp": float(entry.get("timestamp", time.time()) or time.time()),
        "kind": str(entry.get("kind", RECENT_KIND_IMPORT) or RECENT_KIND_IMPORT),
    }

    archive_path = str(entry.get("archive_path", "") or "")
    if archive_path:
        normalized["archive_path"] = archive_path

    display_name = str(entry.get("display_name", "") or "")
    if display_name:
        normalized["display_name"] = display_name

    return normalized


def add_recent_entry(context, path, cache_type, kind=RECENT_KIND_IMPORT, archive_path="", display_name=""):
    """Add a recent browser activity entry."""
    try:
        addon_prefs = get_all_addon_prefs(context)
        raw_recent = json.loads(addon_prefs.browser_recent_imports or "[]")
        recent = [item for item in (_normalize_recent_entry(r) for r in raw_recent) if item]

        entry = _normalize_recent_entry({
            "path": path,
            "cache_type": cache_type,
            "kind": kind,
            "archive_path": archive_path,
            "display_name": display_name,
            "timestamp": time.time(),
        })
        if not entry:
            return

        entry_key = _recent_entry_key(entry)
        recent = [r for r in recent if _recent_entry_key(r) != entry_key]
        recent.insert(0, entry)
        recent = recent[:MAX_RECENT_ITEMS]

        addon_prefs.browser_recent_imports = json.dumps(recent)
    except Exception as e:
        log.error("Failed to save recent browser entry: %s", e)


def add_recent_import(context, path, cache_type):
    """Add an import to the recent files list."""
    archive_path = ""
    if is_external_cache(cache_type):
        session = get_external_archive_session(cache_type)
        if session:
            archive_path = session.get("archive_path", "") or ""
    add_recent_entry(
        context,
        path,
        cache_type,
        kind=RECENT_KIND_IMPORT,
        archive_path=archive_path,
    )


def add_recent_external_archive(context, cache_type, archive_path):
    """Track a standalone archive open/reopen in the recent list."""
    display_name = os.path.basename(archive_path or "") or archive_path
    add_recent_entry(
        context,
        archive_path,
        cache_type,
        kind=RECENT_KIND_EXTERNAL_ARCHIVE,
        display_name=display_name,
    )


def get_recent_imports(context):
    """Get recent browser activity entries."""
    try:
        addon_prefs = get_all_addon_prefs(context)
        raw_recent = json.loads(addon_prefs.browser_recent_imports or "[]")
        return [item for item in (_normalize_recent_entry(r) for r in raw_recent) if item]
    except Exception:
        return []


def clear_recent_imports(context):
    """Clear all recent browser activity."""
    try:
        addon_prefs = get_all_addon_prefs(context)
        addon_prefs.browser_recent_imports = "[]"
    except Exception:
        pass


def get_last_recent_external_archive(context):
    """Return the most recently opened standalone archive, if any."""
    for item in get_recent_imports(context):
        if item.get("kind") == RECENT_KIND_EXTERNAL_ARCHIVE and is_external_cache(item.get("cache_type", "")):
            return item
    return None


def add_bookmark(context, path, cache_type, name=None, archive_path=""):
    """Add a bookmark."""
    try:
        addon_prefs = get_all_addon_prefs(context)
        bookmarks = json.loads(addon_prefs.browser_bookmarks or "[]")
        archive_path = str(archive_path or "")
        if is_external_cache(cache_type) and not archive_path:
            session = get_external_archive_session(cache_type)
            if session:
                archive_path = session.get("archive_path", "") or ""

        # Check if already exists
        if any(
            b.get('path') == path
            and b.get('cache_type') == cache_type
            and str(b.get('archive_path', '') or '') == archive_path
            for b in bookmarks
        ):
            return False

        # Add bookmark
        display_name = name or (path.split("\\")[-1] if path else cache_type)
        bookmark = {
            'path': path,
            'cache_type': cache_type,
            'name': display_name
        }
        if archive_path:
            bookmark['archive_path'] = archive_path
        bookmarks.append(bookmark)

        addon_prefs.browser_bookmarks = json.dumps(bookmarks)
        return True
    except Exception as e:
        log.error("Failed to add bookmark: %s", e)
        return False


def remove_bookmark(context, path, cache_type, archive_path=""):
    """Remove a bookmark."""
    try:
        addon_prefs = get_all_addon_prefs(context)
        bookmarks = json.loads(addon_prefs.browser_bookmarks or "[]")
        archive_path = str(archive_path or "")
        bookmarks = [
            b for b in bookmarks
            if not (
                b.get('path') == path
                and b.get('cache_type') == cache_type
                and str(b.get('archive_path', '') or '') == archive_path
            )
        ]
        addon_prefs.browser_bookmarks = json.dumps(bookmarks)
        return True
    except Exception:
        return False


def get_bookmarks(context):
    """Get list of bookmarks."""
    try:
        addon_prefs = get_all_addon_prefs(context)
        return json.loads(addon_prefs.browser_bookmarks or "[]")
    except Exception:
        return []


def is_bookmarked(context, path, cache_type, archive_path=""):
    """Check if a path is bookmarked."""
    bookmarks = get_bookmarks(context)
    archive_path = str(archive_path or "")
    return any(
        b.get('path') == path
        and b.get('cache_type') == cache_type
        and str(b.get('archive_path', '') or '') == archive_path
        for b in bookmarks
    )


# Batch selection tracking
_batch_selected_files = set()  # Set of (cache_type, path) tuples


def toggle_batch_selection(cache_type, path):
    """Toggle a file's selection state."""
    global _batch_selected_files
    key = (cache_type, path)
    if key in _batch_selected_files:
        _batch_selected_files.discard(key)
        return False
    else:
        _batch_selected_files.add(key)
        return True


def is_batch_selected(cache_type, path):
    """Check if a file is selected for batch import."""
    return (cache_type, path) in _batch_selected_files


def add_batch_selection(cache_type, path):
    """Add a file to batch selection. Returns True if newly added."""
    global _batch_selected_files
    key = (cache_type, path)
    if key in _batch_selected_files:
        return False
    _batch_selected_files.add(key)
    return True


def clear_batch_selection():
    """Clear all batch selections."""
    global _batch_selected_files
    _batch_selected_files = set()


def get_batch_selection():
    """Get all selected files."""
    return list(_batch_selected_files)


def get_visible_batch_file_paths(context):
    """Get file paths visible in the current folder view (respects extension filter)."""
    if not context or not hasattr(context.scene, "witcher_file_browser"):
        return []

    witcher_file_browser = context.scene.witcher_file_browser
    if not witcher_file_browser.active_cache_type or witcher_file_browser.search_query:
        return []

    folder_items = folder_structure.get_items(witcher_file_browser.current_folder)
    filter_text = witcher_file_browser.extension_filter.strip().lower()
    if filter_text:
        filtered_items = [
            item for item in folder_items
            if item['is_folder'] or filter_text in item['name'].lower()
        ]
    else:
        filtered_items = folder_items

    file_items = [item for item in filtered_items if not item['is_folder']]
    page_size = _get_browser_file_page_size(context, getattr(witcher_file_browser, "file_display_mode", "LIST"))
    file_stats = _get_browser_page_stats(file_items, witcher_file_browser.file_page_index, page_size)
    visible_files = []
    for item in file_stats["items"]:
        full_item_path = (
            witcher_file_browser.current_folder + "\\" + item['name']
            if witcher_file_browser.current_folder else item['name']
        )
        visible_files.append(full_item_path)
    return visible_files


def get_export_scope_file_paths(context):
    """Return recursively exportable file paths under the current browser folder."""
    if not context or not hasattr(context.scene, "witcher_file_browser"):
        return []

    witcher_file_browser = context.scene.witcher_file_browser
    cache_type = witcher_file_browser.active_cache_type
    if not cache_type or witcher_file_browser.search_query:
        return []

    current_folder = str(witcher_file_browser.current_folder or "").replace("/", "\\").strip("\\")
    filter_text = witcher_file_browser.extension_filter.strip().lower()
    prefix = (current_folder + "\\").lower() if current_folder else ""
    paths = []
    seen = set()
    for original_path in folder_structure.index.values():
        full_path = str(original_path or "").replace("/", "\\")
        if not full_path:
            continue
        full_lower = full_path.lower()
        if prefix and not full_lower.startswith(prefix):
            continue
        if filter_text and filter_text not in os.path.basename(full_path).lower():
            continue
        if full_lower in seen:
            continue
        seen.add(full_lower)
        paths.append(full_path)
    return sorted(paths, key=str.lower)


def _export_external_archive_item(
    context,
    cache_type: str,
    item_path: str,
    *,
    overwrite: bool = False,
    output_root: str = "",
    texture_format: str = "",
) -> dict:
    """Extract one file from a standalone archive to the chosen export root."""
    cache_type = str(cache_type or "")
    if not is_external_cache(cache_type):
        raise ValueError(f"Unsupported external cache type: {cache_type}")

    full_path_norm = (item_path or "").replace("/", "\\")
    if not full_path_norm:
        raise ValueError("No item path provided")

    output_root = win_safe_path(output_root or _get_external_archive_output_root(context, cache_type, create=True))
    if not output_root:
        raise ValueError("No standalone archive export folder is configured")

    result = {
        "primary_path": "",
        "all_paths": [],
        "skipped": False,
    }

    if cache_type == EXTERNAL_BUNDLE_CACHE_TYPE:
        session = get_external_archive_session(cache_type)
        items = session.get("items", {}).get(full_path_norm) if session else None
        if not items:
            raise FileNotFoundError(f"Bundle item not found: {full_path_norm}")
        final_item = items[-1] if isinstance(items, list) else items
        item_name = getattr(final_item, "name", None) or getattr(final_item, "Name", full_path_norm)
        rel_path = get_vanilla_path(item_name, False)
        export_path = win_safe_path(os.path.join(output_root, rel_path.replace("/", os.sep).replace("\\", os.sep)))
        if not overwrite and win_path_exists(export_path):
            result["primary_path"] = export_path
            result["all_paths"] = [export_path]
            result["skipped"] = True
            return result
        if prepare_extraction_target(export_path, output_root):
            written_path = final_item.extract_to_file(export_path)
            if written_path:
                export_path = written_path
        result["primary_path"] = export_path
        result["all_paths"] = [export_path]
        return result

    if cache_type == EXTERNAL_COLLISION_CACHE_TYPE:
        session = get_external_archive_session(cache_type)
        items = session.get("items", {}).get(full_path_norm) if session else None
        if not items:
            raise FileNotFoundError(f"Collision item not found: {full_path_norm}")
        final_item = items[-1] if isinstance(items, list) else items
        rel_path = get_collision_output_rel_path(full_path_norm, loadmods=False)
        export_path = win_safe_path(os.path.join(output_root, rel_path.replace("/", os.sep).replace("\\", os.sep)))
        if not overwrite and win_path_exists(export_path):
            result["primary_path"] = export_path
            result["all_paths"] = [export_path]
            result["skipped"] = True
            return result
        if prepare_extraction_target(export_path, output_root):
            written_path = final_item.extract_to_file(export_path)
            if written_path:
                export_path = written_path
        result["primary_path"] = export_path
        result["all_paths"] = [export_path]
        return result

    if cache_type == EXTERNAL_SOUND_CACHE_TYPE:
        final_item, export_path = _get_sound_item_and_export_path(
            context,
            full_path_norm,
            loadmods=False,
            cache_type=cache_type,
        )
        if final_item is None or not export_path:
            raise FileNotFoundError(f"Sound item not found: {full_path_norm}")
        if not overwrite and win_path_exists(export_path):
            result["primary_path"] = export_path
            result["all_paths"] = [export_path]
            result["skipped"] = True
            return result
        if prepare_extraction_target(export_path, output_root):
            written_path = final_item.extract_to_file(export_path)
            if written_path:
                export_path = written_path
        result["primary_path"] = export_path
        result["all_paths"] = [export_path]
        return result

    if cache_type == EXTERNAL_TEXTURE_CACHE_TYPE:
        items = _get_texture_cache_items(full_path_norm, cache_type=cache_type)
        if not items:
            raise FileNotFoundError(f"Texture item not found: {full_path_norm}")

        final_item = items[-1] if isinstance(items, list) else items
        item_name = getattr(final_item, "Name", full_path_norm) or full_path_norm
        rel_name = get_vanilla_path(item_name, False)
        base_rel = os.path.splitext(rel_name)[0]
        desired_ext = _get_external_texture_export_ext(context, texture_format=texture_format)
        target_path = win_safe_path(os.path.join(output_root, (base_rel + desired_ext).replace("/", os.sep).replace("\\", os.sep)))
        dds_path = win_safe_path(os.path.join(output_root, (base_rel + ".dds").replace("/", os.sep).replace("\\", os.sep)))

        if not overwrite and win_path_exists(target_path):
            result["primary_path"] = target_path
            result["all_paths"] = [target_path]
            result["skipped"] = True
            return result

        if prepare_extraction_target(dds_path, output_root):
            final_item.extract_to_file(dds_path)

        final_path = dds_path
        if desired_ext != ".dds":
            from ..CR2W import texconv_wrapper

            if desired_ext == ".png":
                final_path = texconv_wrapper.convert_dds_to_png(dds_path, output_dir=os.path.dirname(target_path))
            elif desired_ext == ".tga":
                final_path = texconv_wrapper.convert_dds_to_tga(dds_path, output_dir=os.path.dirname(target_path))
            else:
                raise ValueError(f"Unsupported texture export format: {desired_ext}")
            if win_path_exists(dds_path):
                try:
                    os.unlink(dds_path)
                except OSError:
                    pass

        result["primary_path"] = win_safe_path(final_path)
        result["all_paths"] = [win_safe_path(final_path)]
        return result

    raise ValueError(f"Unsupported external cache type: {cache_type}")


def _load_external_archive_session(cache_type: str, archive_path: str):
    filepath = win_safe_path(bpy.path.abspath(archive_path or ""))
    if not filepath or not os.path.isfile(filepath):
        raise FileNotFoundError(f"Archive file not found: {archive_path}")

    if cache_type == EXTERNAL_COLLISION_CACHE_TYPE:
        archive = CollisionCache(filepath)
        items = {}
        collision_exts = {}
        for item in getattr(archive, "Files", []):
            key = (getattr(item, "Name", "") or "").replace("/", "\\")
            if not key or _should_skip_buffer_name(key):
                continue
            items.setdefault(key, []).append(item)
            ext = getattr(item, "Extension", "")
            if ext:
                collision_exts[key] = ext
        if not items:
            raise ValueError("No readable entries found in collision cache")
        set_external_archive_session(
            EXTERNAL_COLLISION_CACHE_TYPE,
            filepath,
            items,
            collision_exts=collision_exts,
        )
        return {"archive_path": filepath, "item_count": len(items)}

    if cache_type == EXTERNAL_TEXTURE_CACHE_TYPE:
        archive = TextureCache(filepath)
        items = {}
        for item in getattr(archive, "Files", []):
            key = (getattr(item, "Name", "") or "").replace("/", "\\")
            if not key or _should_skip_buffer_name(key):
                continue
            items.setdefault(key, []).append(item)
        if not items:
            raise ValueError("No readable entries found in texture cache")
        set_external_archive_session(EXTERNAL_TEXTURE_CACHE_TYPE, filepath, items)
        return {"archive_path": filepath, "item_count": len(items)}

    if cache_type == EXTERNAL_SOUND_CACHE_TYPE:
        soundbanks_info = SoundBanksInfoXML(_soundbanks_metadata_path())
        archive = SoundCache(filepath, soundbanks_info=soundbanks_info)
        items = {}
        for item in getattr(archive, "Files", []):
            key = (getattr(item, "Name", "") or "").replace("/", "\\")
            if not key or _should_skip_buffer_name(key):
                continue
            items.setdefault(key, []).append(item)
        if not items:
            raise ValueError("No readable entries found in sound cache")
        set_external_archive_session(EXTERNAL_SOUND_CACHE_TYPE, filepath, items)
        return {"archive_path": filepath, "item_count": len(items)}

    if cache_type == EXTERNAL_BUNDLE_CACHE_TYPE:
        archive = Bundle(filepath)
        items = {}
        for key, item in getattr(archive, "Items", {}).items():
            key_str = str(key).replace("/", "\\")
            if not key_str or _should_skip_buffer_name(key_str):
                continue
            items.setdefault(key_str, []).append(item)
        if not items:
            raise ValueError("No readable entries found in bundle")
        set_external_archive_session(EXTERNAL_BUNDLE_CACHE_TYPE, filepath, items)
        return {"archive_path": filepath, "item_count": len(items)}

    raise ValueError(f"Unsupported external cache type: {cache_type}")


def _ensure_external_archive_session(context, cache_type: str, archive_path: str = ""):
    session = get_external_archive_session(cache_type)
    requested_path = _normalize_real_path(archive_path)
    current_path = _normalize_real_path(session.get("archive_path", "") or "") if session else ""
    if session and (not requested_path or current_path.lower() == requested_path.lower()):
        return {
            "archive_path": current_path,
            "item_count": len(session.get("items", {})),
        }
    if not requested_path:
        raise ValueError(f"No standalone archive loaded for {cache_type}")
    return _load_external_archive_session(cache_type, requested_path)


def _activate_external_archive_browser(context, cache_type: str):
    """Switch the asset browser to a loaded standalone archive session."""
    if not context or not hasattr(context.scene, "witcher_file_browser"):
        return

    witcher_file_browser = context.scene.witcher_file_browser
    witcher_file_browser.browser_view_mode = 'BROWSE'
    witcher_file_browser.loadmods = False
    witcher_file_browser.active_cache_type = cache_type
    witcher_file_browser.current_folder = ""
    witcher_file_browser.search_query = ""
    witcher_file_browser.path_input = ""

    clear_search_cache()
    clear_nav_history()
    try:
        SelectCacheTypeOperator.populate_folder_structure(SelectCacheTypeOperator, cache_type, context)
    except Exception as e:
        log.error("Failed to populate external archive browser: %s", e)
        return
    add_to_nav_history(cache_type, "")
    save_browser_state(context, cache_type, "")


def _prepare_external_archive_browser(context, cache_type: str, archive_path: str):
    info = _ensure_external_archive_session(context, cache_type, archive_path)
    archive_path = info.get("archive_path", archive_path)
    _prime_external_archive_export_root(context, archive_path)
    _activate_external_archive_browser(context, cache_type)
    add_recent_external_archive(context, cache_type, archive_path)
    return info


def _navigate_browser_to_location(
    context,
    cache_type: str,
    folder_path: str = "",
    *,
    reveal_file: str = "",
    archive_path: str = "",
    clear_filter: bool = False,
):
    if not context or not hasattr(context.scene, "witcher_file_browser"):
        return

    browser = context.scene.witcher_file_browser
    cache_type = str(cache_type or "")
    folder_path = _normalize_virtual_path(folder_path)
    reveal_file = _normalize_virtual_path(reveal_file)

    browser.browser_view_mode = 'BROWSE'
    if is_external_cache(cache_type):
        info = _ensure_external_archive_session(context, cache_type, archive_path)
        _prime_external_archive_export_root(context, info.get("archive_path", archive_path))
        add_recent_external_archive(context, cache_type, info.get("archive_path", archive_path))

    if cache_type != browser.active_cache_type:
        browser.active_cache_type = cache_type
        if cache_type:
            SelectCacheTypeOperator.populate_folder_structure(SelectCacheTypeOperator, cache_type, context)

    browser.current_folder = folder_path
    browser.search_query = ""
    if clear_filter:
        browser.extension_filter = ""
    if reveal_file:
        _set_browser_reveal(context, reveal_file)
        _ensure_revealed_file_page(context)
    else:
        _clear_browser_reveal(context)
    clear_search_cache()
    add_to_nav_history(cache_type, folder_path)
    save_browser_state(context, cache_type, folder_path)


def _open_external_archive_browser_popup(context, cache_type: str, archive_path: str):
    info = _prepare_external_archive_browser(context, cache_type, archive_path)
    try:
        bpy.ops.witcher.simple_file_browser(
            'INVOKE_DEFAULT',
            restore_last_state=False,
            startup_cache_type=cache_type,
            startup_folder="",
        )
    except Exception as exc:
        log.error("Failed to open asset browser popup for standalone archive %s: %s", archive_path, exc, exc_info=True)
    return info


class OpenExternalCollisionCacheOperator(Operator, ImportHelper):
    """Open a standalone collision.cache file in the asset browser"""
    bl_idname = "witcher.open_external_collision_cache"
    bl_label = "Open Collision Cache"

    filename_ext = ".cache"
    filter_glob: StringProperty(default="*.cache", options={'HIDDEN'})

    def execute(self, context):
        filepath = bpy.path.abspath(self.filepath or "")
        try:
            info = _open_external_archive_browser_popup(context, EXTERNAL_COLLISION_CACHE_TYPE, filepath)
        except Exception as e:
            self.report({'ERROR'}, f"Failed to read collision cache: {e}")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Loaded collision cache: {os.path.basename(info['archive_path'])} ({info['item_count']} items)")
        return {'FINISHED'}


class OpenExternalTextureCacheOperator(Operator, ImportHelper):
    """Open a standalone texture.cache file in the asset browser"""
    bl_idname = "witcher.open_external_texture_cache"
    bl_label = "Open Texture Cache"

    filename_ext = ".cache"
    filter_glob: StringProperty(default="*.cache", options={'HIDDEN'})

    def execute(self, context):
        filepath = bpy.path.abspath(self.filepath or "")
        try:
            info = _open_external_archive_browser_popup(context, EXTERNAL_TEXTURE_CACHE_TYPE, filepath)
        except Exception as e:
            self.report({'ERROR'}, f"Failed to read texture cache: {e}")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Loaded texture cache: {os.path.basename(info['archive_path'])} ({info['item_count']} items)")
        return {'FINISHED'}


class OpenExternalSoundCacheOperator(Operator, ImportHelper):
    """Open a standalone sound.cache file in the asset browser"""
    bl_idname = "witcher.open_external_sound_cache"
    bl_label = "Open Sound Cache"

    filename_ext = ".cache"
    filter_glob: StringProperty(default="*.cache", options={'HIDDEN'})

    def execute(self, context):
        filepath = bpy.path.abspath(self.filepath or "")
        try:
            info = _open_external_archive_browser_popup(context, EXTERNAL_SOUND_CACHE_TYPE, filepath)
        except Exception as e:
            self.report({'ERROR'}, f"Failed to read sound cache: {e}")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Loaded sound cache: {os.path.basename(info['archive_path'])} ({info['item_count']} items)")
        return {'FINISHED'}


class OpenExternalBundleOperator(Operator, ImportHelper):
    """Open a standalone bundle file in the asset browser"""
    bl_idname = "witcher.open_external_bundle"
    bl_label = "Open Bundle"

    filename_ext = ".bundle"
    filter_glob: StringProperty(default="*.bundle", options={'HIDDEN'})

    def execute(self, context):
        filepath = bpy.path.abspath(self.filepath or "")
        try:
            info = _open_external_archive_browser_popup(context, EXTERNAL_BUNDLE_CACHE_TYPE, filepath)
        except Exception as e:
            self.report({'ERROR'}, f"Failed to read bundle: {e}")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Loaded bundle: {os.path.basename(info['archive_path'])} ({info['item_count']} items)")
        return {'FINISHED'}

class SimpleFileBrowser(Operator):
    """Browse Witcher 3 game assets by cache type"""
    bl_idname = "witcher.simple_file_browser"
    bl_label = "Witcher Asset Browser"
    loadmods: bpy.props.BoolProperty(name="Load Mods", default=False)
    startup_cache_type: StringProperty(default="", options={'HIDDEN', 'SKIP_SAVE'})
    startup_folder: StringProperty(default="", options={'HIDDEN', 'SKIP_SAVE'})
    restore_last_state: BoolProperty(default=True, options={'HIDDEN', 'SKIP_SAVE'})
    _ui_refresh_timer = None

    def execute(self, context):
        self._remove_ui_refresh_timer(context)
        return {'FINISHED'}
    
    def get_desired_popup_width_px(self, context: bpy.types.Context) -> int:
        target_width_px = _get_browser_popup_target_width_px(context)
        ui_scale = _get_browser_ui_scale(context)
        return max(320, int(round(target_width_px / max(0.5, ui_scale))))

    def _remove_ui_refresh_timer(self, context):
        wm = getattr(context, "window_manager", None)
        timer = getattr(self, "_ui_refresh_timer", None)
        if wm and timer:
            try:
                wm.event_timer_remove(timer)
            except Exception:
                pass
        self._ui_refresh_timer = None

    def cancel(self, context):
        self._remove_ui_refresh_timer(context)

    def modal(self, context, event):
        if event.type == 'TIMER' and event.timer == getattr(self, "_ui_refresh_timer", None):
            _touch_external_export_ui(context)
            return {'RUNNING_MODAL'}
        return {'PASS_THROUGH'}

    def invoke(self, context, event):
        witcher_file_browser = context.scene.witcher_file_browser
        witcher_file_browser.loadmods = self.loadmods  # Store for SelectCacheTypeOperator
        witcher_file_browser.search_query = ""
        try:
            prefs = get_all_addon_prefs(context)
            if prefs is not None:
                preset_mode = str(getattr(prefs, "browser_grid_size_mode", DEFAULT_BROWSER_GRID_SIZE_MODE) or "")
                if preset_mode not in BROWSER_GRID_SIZE_PRESETS:
                    preset_mode = DEFAULT_BROWSER_GRID_SIZE_MODE
                witcher_file_browser.grid_size_mode_live = preset_mode
        except Exception:
            pass

        if self.loadmods:
            refresh_mod_cache_managers()

        startup_cache_type = str(getattr(self, "startup_cache_type", "") or "")
        startup_folder = str(getattr(self, "startup_folder", "") or "")
        if startup_cache_type:
            if is_external_cache(startup_cache_type) and not get_external_archive_session(startup_cache_type):
                startup_cache_type, startup_folder = "", ""

        if startup_cache_type:
            witcher_file_browser.active_cache_type = startup_cache_type
            witcher_file_browser.current_folder = startup_folder
            SelectCacheTypeOperator.populate_folder_structure(self, startup_cache_type, context)
            clear_nav_history()
            add_to_nav_history(startup_cache_type, startup_folder)
        else:
            # Try to restore last browser state from addon preferences
            last_cache_type, last_folder = load_browser_state(context)
            if is_external_cache(last_cache_type) and not get_external_archive_session(last_cache_type):
                last_cache_type, last_folder = "", ""

            if self.restore_last_state and last_cache_type:
                # Restore to last used cache type and folder
                witcher_file_browser.active_cache_type = last_cache_type
                witcher_file_browser.current_folder = last_folder
                # Populate folder structure for this cache type
                SelectCacheTypeOperator.populate_folder_structure(self, last_cache_type, context)
                # Initialize navigation history with restored state
                add_to_nav_history(last_cache_type, last_folder)
            else:
                # Reset to root level (cache type selection)
                witcher_file_browser.current_folder = ""
                witcher_file_browser.active_cache_type = ""
                clear_nav_history()

        wm = context.window_manager
        self._remove_ui_refresh_timer(context)
        self._ui_refresh_timer = wm.event_timer_add(0.1, window=context.window)
        return wm.invoke_props_dialog(self, width=self.get_desired_popup_width_px(context))

    # def initialize_folder_structure(self):
    #     # Initialize folder structure with data
    #     paths = [
    #         "characters\\npc_entities\\main_npc\\ciri.w2ent",
    #         "characters\\models\\main_npc\\ciri\\model\\body_03_wa__ciri.w2mesh",
    #         "animations\\interaction\\finishers\\geralt_finishers.w2anims",
    #         "gameplay\\gui_new\\swf\\photomode\\photomode.redswf",
    #         "environment\\textures_tileable\\road_tool\\footpath_n.xbm"
    #     ]
    #     for path in paths:
    #         folder_structure.add_path(path)

    def initialize_folder_structure(self):
        
        #managers = CacheController().GetManagers(loadmods = True)
        cache_controller = CacheController()
        folder_structure.items.clear()
        folder_structure.index.clear()
        # Initialize the folder structure
        # Assuming LoadBundleManager and its methods are defined
        bundle_manager = cache_controller.GetManagers(self.loadmods)[0]
        #bundle_manager = LoadTextureManager()
        for key, item_list in bundle_manager.Items.items():
            folder_structure.add_path(key)
            for bundle_item in item_list:
                if not hasattr(bundle_item, 'name'):
                    bundle_item.name = bundle_item.Name
                folder_structure.add_path(bundle_item.name)

    def _draw_external_archive_tools(self, layout, context, witcher_file_browser):
        cache_type = witcher_file_browser.active_cache_type
        job = _EXTERNAL_BROWSER_EXPORT_JOB
        status = _EXTERNAL_BROWSER_EXPORT_STATUS
        _ui_ping = int(getattr(witcher_file_browser, "external_export_ui_ping", 0) or 0)
        if not is_external_cache(cache_type):
            return

        session = get_external_archive_session(cache_type)
        if not session:
            return

        scope_paths = get_export_scope_file_paths(context)
        scope_label = witcher_file_browser.current_folder or "Entire archive"
        archive_name = os.path.basename(session.get("archive_path", "")) or "(not loaded)"
        output_root = _get_external_archive_output_root(context, cache_type)
        filter_text = witcher_file_browser.extension_filter.strip()
        is_full_archive_scope = not witcher_file_browser.current_folder and not filter_text
        export_count = len(scope_paths)
        if is_full_archive_scope:
            export_button_text = f"Export All ({export_count:,})"
        else:
            export_button_text = f"Export {export_count:,} Files"

        box = layout.box()
        header = box.row(align=True)
        header.label(text="Standalone Export", icon='EXPORT')
        if get_effective_cache_type(cache_type) == "Texture":
            header.prop(witcher_file_browser, "external_texture_export_format", text="")
        header.prop(witcher_file_browser, "mods_overwrite", text="Overwrite")

        meta_row = box.row(align=True)
        meta_row.scale_y = 0.9
        meta_row.label(text=f"Archive: {archive_name}", icon='FILE')
        meta_row.separator(factor=1.0)
        meta_row.label(text=f"Scope: {scope_label} ({len(scope_paths):,})", icon='FILE_FOLDER')

        folder_row = box.row(align=True)
        folder_row.prop(witcher_file_browser, "external_extract_root", text="Folder")
        open_folder_sub = folder_row.row(align=True)
        open_folder_sub.enabled = bool(output_root)
        open_op = open_folder_sub.operator(
            "witcher.open_external_archive_folder",
            text="",
            icon='FILE_FOLDER',
        )
        open_op.cache_type = cache_type

        if get_effective_cache_type(cache_type) == "Texture" and witcher_file_browser.external_texture_export_format != 'DDS':
            from ..CR2W import texconv_wrapper

            if not texconv_wrapper.is_available():
                warn = box.row(align=True)
                warn.alert = True
                warn.label(text="PNG/TGA export requires texconv.dll.", icon='ERROR')

        action_row = box.row(align=True)
        action_row.enabled = bool(scope_paths) and not job["running"]
        export_op = action_row.operator(
            "witcher.external_archive_export_all",
            text=export_button_text,
            icon='EXPORT',
        )
        export_op.cache_type = cache_type
        export_op.folder_path = witcher_file_browser.current_folder

        if job["running"]:
            progress_box = box.box()
            progress_box.alert = True
            total = max(1, int(job.get("total", 0) or 0))
            pct = (int(job.get("index", 0) or 0) / total) * 100.0
            progress_row = progress_box.row(align=True)
            progress_row.label(
                text=(
                    f"Exporting... "
                    f"{int(job.get('index', 0) or 0):,} / {int(job.get('total', 0) or 0):,} ({pct:.1f}%)"
                ),
                icon='TIME',
            )
            progress_row.operator("witcher.cancel_external_archive_export", text="Cancel", icon='CANCEL')
            detail_row = progress_box.row(align=True)
            detail_row.scale_y = 0.9
            detail_row.label(
                text=(
                    f"Extracted: {int(job.get('done', 0) or 0):,}   "
                    f"Skipped: {int(job.get('skipped', 0) or 0):,}   "
                    f"Errors: {int(job.get('errors', 0) or 0):,}"
                )
            )
        elif status.get("visible") and status.get("cache_type") == cache_type:
            state = str(status.get("state", "") or "")
            processed = int(status.get("processed", 0) or 0)
            total = max(1, int(status.get("total", 0) or 0))
            pct = (processed / total) * 100.0
            result_box = box.box()
            if state == "completed":
                result_box.alert = False
            else:
                result_box.alert = True
            title_row = result_box.row(align=True)
            if state == "completed":
                title_row.label(text=f"Finished ({pct:.1f}%)", icon='CHECKMARK')
            elif state == "cancelled":
                title_row.label(text=f"Cancelled ({pct:.1f}%)", icon='CANCEL')
            else:
                title_row.label(text="Finished With Warnings", icon='ERROR')
            dismiss_row = title_row.row(align=True)
            dismiss_row.alignment = 'RIGHT'
            dismiss_row.operator("witcher.dismiss_external_archive_export_status", text="", icon='X')
            stats_row = result_box.row(align=True)
            stats_row.scale_y = 0.9
            stats_row.label(
                text=(
                    f"Extracted: {int(status.get('done', 0) or 0):,}   "
                    f"Skipped: {int(status.get('skipped', 0) or 0):,}   "
                    f"Errors: {int(status.get('errors', 0) or 0):,}"
                )
            )
        elif not scope_paths:
            empty = box.row(align=True)
            empty.alert = True
            empty.label(text="No files match the current folder/filter.", icon='ERROR')

    def _draw_grid_file_entry(
        self,
        grid,
        context,
        witcher_file_browser,
        item,
        display_name: str,
        full_item_path: str,
        *,
        grid_columns: int,
        name_lines,
        reserved_name_lines: int,
        file_exists: bool,
        status_icon: str,
    ):
        box = grid.box()

        icon_info = _get_browser_item_icon_info(
            context,
            witcher_file_browser.active_cache_type,
            full_item_path,
            loadmods=witcher_file_browser.loadmods,
        )

        icon_row = box.row()
        icon_row.alignment = 'CENTER'
        icon_id = int(icon_info.get("icon_id", 0) or 0)
        if icon_id:
            icon_row.template_icon(icon_value=icon_id, scale=_get_browser_grid_icon_scale(context, columns=grid_columns))
        else:
            icon_row.label(text="", icon=status_icon or 'FILE')

        actions_row = box.row(align=True)
        if witcher_file_browser.batch_select_mode:
            is_selected = is_batch_selected(witcher_file_browser.active_cache_type, full_item_path)
            select_icon = "CHECKBOX_HLT" if is_selected else "CHECKBOX_DEHLT"
            sel_op = actions_row.operator("witcher.toggle_batch_select", text="", icon=select_icon)
            sel_op.path = full_item_path
            sel_op.cache_type = witcher_file_browser.active_cache_type

        _add_browser_preview_button(
            actions_row,
            context,
            witcher_file_browser.active_cache_type,
            full_item_path,
            loadmods=witcher_file_browser.loadmods,
        )

        copy_op = actions_row.operator("witcher.copy_path", text="", icon="COPYDOWN")
        copy_op.path = full_item_path

        loc_row = actions_row.row(align=True)
        loc_row.enabled = file_exists
        op_loc = loc_row.operator("witcher.open_file_location", text="", icon="FILEBROWSER")
        op_loc.file_path = full_item_path
        op_loc.cache_type = witcher_file_browser.active_cache_type

        if not witcher_file_browser.batch_select_mode and cache_supports_scene_import(witcher_file_browser.active_cache_type):
            op_import = actions_row.operator("witcher.file_action_import_to_scene", text="", icon='IMPORT')
            op_import.file_path = item['name']

        if cache_supports_sound_preview(witcher_file_browser.active_cache_type):
            is_playing = _sound_preview_matches(witcher_file_browser.active_cache_type, full_item_path)
            op_sound = actions_row.operator(
                "witcher.sound_preview_toggle",
                text="",
                icon='CANCEL' if is_playing else 'PLAY',
            )
            op_sound.file_path = full_item_path
            op_sound.cache_type = witcher_file_browser.active_cache_type

        if witcher_file_browser.active_cache_type != "Texture" and not is_disk_cache(witcher_file_browser.active_cache_type):
            op_export = actions_row.operator("witcher.file_action", text="", icon='EXPORT')
            op_export.file_path = item['name']

        stats_op = actions_row.operator("witcher.file_item_stats", text="", icon='INFO')
        stats_op.file_path = full_item_path
        stats_op.cache_type = witcher_file_browser.active_cache_type
        stats_op.loadmods = witcher_file_browser.loadmods

        is_revealed = _is_revealed_browser_item(witcher_file_browser, full_item_path)
        draw_name_lines = [str(line or "") for line in (name_lines or [display_name])]
        target_line_count = max(1, int(reserved_name_lines or len(draw_name_lines)))
        while len(draw_name_lines) < target_line_count:
            draw_name_lines.append(" ")
        name_col = box.column(align=True)
        name_col.alert = is_revealed
        for line_text in draw_name_lines[:target_line_count]:
            name_col.label(text=line_text, translate=False)

    def draw(self, context):
        layout = self.layout
        witcher_file_browser = context.scene.witcher_file_browser

        # View mode tabs at top (Browse | Recent | Bookmarks)
        view_row = layout.row(align=True)
        view_row.prop(witcher_file_browser, "browser_view_mode", expand=True)
        layout.separator(factor=0.5)

        # Mod priority controls
        mods_row = layout.row(align=True)
        mods_row.prop(witcher_file_browser, "use_mods_priority", text="Load Mods")
        mods_row.prop(witcher_file_browser, "mods_overwrite", text="Overwrite")
        mods_row.prop(witcher_file_browser, "open_import_dialog", text="Open Dialog")
        layout.separator(factor=0.5)

        # Handle Recent view
        if witcher_file_browser.browser_view_mode == 'RECENT':
            self.draw_recent_view(layout, context)
            return

        # Handle Bookmarks view
        if witcher_file_browser.browser_view_mode == 'BOOKMARKS':
            self.draw_bookmarks_view(layout, context)
            return

        # BROWSE MODE - ROOT LEVEL - Show cache type selection
        if not witcher_file_browser.active_cache_type:
            layout.label(text="Select Cache Type:", icon='FILE_FOLDER')

            # Global search bar at root level with clear button
            layout.separator(factor=0.3)
            search_row = layout.row(align=True)
            search_row.prop(witcher_file_browser, "search_query", text="", icon="VIEWZOOM")
            if witcher_file_browser.search_query:
                search_row.operator("witcher.clear_search", text="", icon="X")
            layout.separator(factor=0.3)

            if witcher_file_browser.search_query:
                # Search across all caches (uses cached results)
                self.draw_global_search_results(layout, witcher_file_browser.search_query)
            else:
                # Show cache type buttons
                col = layout.column(align=True)
                cache_types = [
                    ("Bundle", "PACKAGE", "Game asset bundles (.w2mesh, .w2ent, etc.)"),
                    ("Collision", "MESH_CUBE", "Collision meshes (.nxs)"),
                    ("Texture", "IMAGE_DATA", "Texture cache (.xbm, .dds)"),
                    ("Sound", "SPEAKER", "Sound cache (.wem, .bnk)"),
                    ("Speech", "SPEAKER", "Speech/audio files"),
                    ("REDkit Depot", "FILE_FOLDER", "REDkit r4data depot (read-only)"),
                    ("REDkit Uncooked", "FILE_FOLDER", "REDkit uncooked depot (read-only)"),
                    ("Workspace", "FILE_FOLDER", "Project workspace(s)"),
                    ("Cooked", "PACKAGE", "Project cooked output (packed)"),
                    ("Witcher 2 Data", "FILE_FOLDER", "Witcher 2 game data (read-only)"),
                ]
                for cache_name, icon, desc in cache_types:
                    row = col.row(align=True)
                    op = row.operator("witcher.select_cache_type", text=cache_name, icon=icon)
                    op.cache_type = cache_name
                    row.label(text=desc)

                last_external = get_last_recent_external_archive(context)
                last_external_path = last_external.get("path", "") if last_external else ""
                last_external_available = bool(last_external) and _can_restore_external_archive(
                    last_external.get("cache_type", ""),
                    last_external_path,
                )
                if last_external and last_external_available:
                    layout.separator(factor=0.4)
                    resume_box = layout.box()
                    resume_head = resume_box.row(align=True)
                    resume_head.label(text="Last External", icon='FILEBROWSER')
                    resume_head.label(
                        text=get_effective_cache_type(last_external.get("cache_type", "")),
                        icon=get_cache_type_icon(last_external.get("cache_type", "")),
                    )
                    resume_row = resume_box.row(align=True)
                    op = resume_row.operator(
                        "witcher.reopen_external_archive",
                        text=last_external.get("display_name", "") or os.path.basename(last_external_path) or last_external_path,
                        icon=get_cache_type_icon(last_external.get("cache_type", "")),
                    )
                    op.cache_type = last_external.get("cache_type", "")
                    op.archive_path = last_external_path
                    copy_op = resume_row.operator("witcher.copy_path", text="", icon="COPYDOWN")
                    copy_op.path = last_external_path

                layout.separator(factor=0.5)
                ext_box = layout.box()
                ext_box.label(text="Standalone Archives", icon='FILEBROWSER')
                ext_box.operator(
                    "witcher.open_external_collision_cache",
                    text="Open collision.cache",
                    icon='MESH_CUBE',
                )
                ext_box.operator(
                    "witcher.open_external_texture_cache",
                    text="Open texture.cache",
                    icon='IMAGE_DATA',
                )
                ext_box.operator(
                    "witcher.open_external_sound_cache",
                    text="Open sound.cache",
                    icon='SPEAKER',
                )
                ext_box.operator(
                    "witcher.open_external_bundle",
                    text="Open .bundle",
                    icon='FILEBROWSER',
                )
            return

        # Header: navigation buttons + cache type indicator
        row = layout.row(align=True)
        row.operator("witcher.browser_go_home", text="", icon="HOME")
        back_row = row.row(align=True)
        back_row.enabled = can_go_back()
        back_row.operator("witcher.navigate_back", text="", icon="BACK")
        fwd_row = row.row(align=True)
        fwd_row.enabled = can_go_forward()
        fwd_row.operator("witcher.navigate_forward", text="", icon="FORWARD")
        row.label(text=f"[{witcher_file_browser.active_cache_type}]", icon='FILE_FOLDER')
        if is_external_cache(witcher_file_browser.active_cache_type):
            session = get_external_archive_session(witcher_file_browser.active_cache_type)
            archive_label = os.path.basename(session["archive_path"]) if session else "(not loaded)"
            row.label(text=archive_label, icon='FILE')
        row.operator("witcher.status_icon_help", text="", icon='QUESTION')

        # Address bar: shows current path, paste/type to navigate, copy current path
        addr_row = layout.row(align=True)
        addr_row.prop(witcher_file_browser, "path_input", text="", icon="FILE_FOLDER")
        copy_op = addr_row.operator("witcher.copy_path", text="", icon="COPYDOWN")
        copy_op.path = witcher_file_browser.current_folder
        addr_row.operator("witcher.navigate_to_path", text="Go")

        self._draw_external_archive_tools(layout, context, witcher_file_browser)

        # Search + view filter on one row: search takes left ~60%, filter the rest
        layout.separator(factor=0.3)
        search_filter_row = layout.row(align=False)
        search_split = search_filter_row.split(factor=0.60, align=True)
        search_part = search_split.row(align=True)
        search_part.prop(witcher_file_browser, "search_query", text="", icon="VIEWZOOM")
        if witcher_file_browser.search_query:
            search_part.operator("witcher.clear_search", text="", icon="X")
        filter_part = search_split.row(align=True)
        filter_part.label(text="", icon='FILTER')
        filter_part.prop(witcher_file_browser, "extension_filter", text="View Filter")
        if witcher_file_browser.extension_filter:
            filter_part.operator("witcher.clear_extension_filter", text="", icon="X")

        layout.separator(factor=0.5)

        if not witcher_file_browser.search_query:
            terrain_items = folder_structure.get_items(witcher_file_browser.current_folder)
            if _should_show_terrain_tools(witcher_file_browser.current_folder, terrain_items):
                terrain_box = layout.box()
                terrain_row = terrain_box.row(align=True)
                terrain_row.label(text="Terrain Tiles", icon="GRID")
                op = terrain_row.operator("witcher.combine_w2ter_tiles", text="Export + Combine", icon='EXPORT')
                op.folder_path = witcher_file_browser.current_folder
                op.cache_type = witcher_file_browser.active_cache_type
                mode_row = terrain_box.row(align=True)
                mode_row.prop(witcher_file_browser, "terrain_import_mode", text="Mode")
                op_full = mode_row.operator("witcher.import_terrain_fullmap", text="Import Full Map", icon='NODETREE')
                op_full.folder_path = witcher_file_browser.current_folder
                op_full.cache_type = witcher_file_browser.active_cache_type
                # Import tiles row
                import_row = terrain_box.row(align=True)
                import_row.prop(witcher_file_browser, "terrain_multires_level", text="Multires")
                op_import = import_row.operator("witcher.import_terrain_tiles", text="Import Tiles", icon='IMPORT')
                op_import.folder_path = witcher_file_browser.current_folder
                op_import.cache_type = witcher_file_browser.active_cache_type
                mat_row = terrain_box.row(align=True)
                mat_row.prop(witcher_file_browser, "terrain_material_roughness", text="Rough")
                mat_row.prop(witcher_file_browser, "terrain_material_specular", text="Spec")
                terrain_box.operator(
                    "witcher.apply_terrain_material_values",
                    text="Apply Terrain Material To Loaded",
                    icon='SHADING_RENDERED',
                )
                layout.separator(factor=0.5)

        # If searching, show search results (uses cached results)
        if witcher_file_browser.search_query:
            col = layout.column(align=True)
            MAX_SEARCH_RESULTS = 100
            search_results = get_cached_search_results(witcher_file_browser.search_query, witcher_file_browser.active_cache_type, folder_structure, loadmods=witcher_file_browser.loadmods)
            filter_text = witcher_file_browser.extension_filter.strip().lower()
            if filter_text:
                search_results = [item for item in search_results if filter_text in item.lower()]
            search_results = _sort_browser_search_results(search_results, witcher_file_browser)
            if not search_results:
                col.label(text="No results found", icon='ERROR')
            else:
                results_header = col.row(align=True)
                capped = search_results[:MAX_SEARCH_RESULTS]
                results_header.label(text=f"{len(capped)} result(s)", icon='FILE')
                results_header.operator("witcher.copy_all_search_paths", text="Copy All Paths", icon="COPYDOWN")
                col.separator(factor=0.3)
                for item in capped:
                    # Use split for path / buttons (go to source + import)
                    row_split = col.split(factor=0.70, align=True)
                    # Checkmark indicator (always present for alignment)
                    file_exists, file_size = get_file_info(context, witcher_file_browser.active_cache_type, item, loadmods=witcher_file_browser.loadmods)
                    path_row = row_split.row(align=True)
                    source_label = get_source_label(
                        context,
                        item,
                        loadmods=witcher_file_browser.loadmods,
                        cache_type=witcher_file_browser.active_cache_type,
                    )
                    status_icon = get_status_icon(
                        context,
                        witcher_file_browser.active_cache_type,
                        item,
                        loadmods=witcher_file_browser.loadmods,
                    )
                    path_row.label(text="", icon=status_icon)
                    display_label = item
                    if get_effective_cache_type(witcher_file_browser.active_cache_type) == "Collision":
                        ext = collision_extension_map.get(item, "")
                        if ext:
                            display_label = f"{display_label} [{ext}]"
                    if source_label.startswith("mod:"):
                        display_label = f"{display_label} [src:{source_label[4:]}]"
                    elif source_label == "vanilla" and witcher_file_browser.loadmods:
                        display_label = f"{display_label} [src:vanilla]"
                    elif witcher_file_browser.use_mods_priority:
                        mod_label = get_mod_override_label(item, loadmods=witcher_file_browser.loadmods)
                        if mod_label:
                            display_label = f"{display_label} [ovr:{mod_label}]"
                    icon_info = _get_browser_item_icon_info(
                        context,
                        witcher_file_browser.active_cache_type,
                        item,
                        loadmods=witcher_file_browser.loadmods,
                    )
                    icon_id = int(icon_info.get("icon_id", 0) or 0)
                    if icon_id:
                        path_row.label(text=display_label, icon_value=icon_id)
                    else:
                        path_row.label(text=display_label, icon='FILE')
                    btns = row_split.row(align=True)
                    # Copy path
                    copy_op = btns.operator("witcher.copy_path", text="", icon="COPYDOWN")
                    copy_op.path = item
                    # Go to source button - navigate to containing folder
                    op_goto = btns.operator("witcher.goto_search_result", text="", icon='FILE_PARENT')
                    op_goto.file_path = item
                    # Open file location on disk (always present for alignment)
                    loc_sub = btns.row(align=True)
                    loc_sub.enabled = file_exists
                    op_loc = loc_sub.operator("witcher.open_file_location", text="", icon="FILEBROWSER")
                    op_loc.file_path = item
                    op_loc.cache_type = witcher_file_browser.active_cache_type
                    _add_browser_preview_button_slot(
                        btns,
                        context,
                        witcher_file_browser.active_cache_type,
                        item,
                        loadmods=witcher_file_browser.loadmods,
                    )
                    if cache_supports_sound_preview(witcher_file_browser.active_cache_type):
                        is_playing = _sound_preview_matches(witcher_file_browser.active_cache_type, item)
                        op_sound = btns.operator(
                            "witcher.sound_preview_toggle",
                            text="",
                            icon='CANCEL' if is_playing else 'PLAY',
                        )
                        op_sound.file_path = item
                        op_sound.cache_type = witcher_file_browser.active_cache_type
                    if cache_supports_scene_import(witcher_file_browser.active_cache_type):
                        op = btns.operator("witcher.file_action_import_to_scene", text="", icon='IMPORT')
                    else:
                        op = btns.operator("witcher.file_action", text="", icon='EXPORT')
                    op.file_path = item
                    # Item stats popup (cache metadata)
                    stats_op = btns.operator("witcher.file_item_stats", text="", icon='INFO')
                    stats_op.file_path = item
                    stats_op.cache_type = witcher_file_browser.active_cache_type
                    stats_op.loadmods = witcher_file_browser.loadmods
                if len(search_results) > MAX_SEARCH_RESULTS:
                    col.label(text="Showing first 100 results. Refine search for more.", icon='INFO')
        else:
            # Split layout: left = folders, right = files
            split = layout.split(factor=_get_browser_folder_split_factor(context))
            folder_items = folder_structure.get_items(witcher_file_browser.current_folder)
            filter_text = witcher_file_browser.extension_filter.strip().lower()
            if filter_text:
                filtered_items = [
                    item for item in folder_items
                    if item['is_folder'] or filter_text in item['name'].lower()
                ]
            else:
                filtered_items = folder_items

            folder_entries = _sort_browser_items(
                [item for item in filtered_items if item['is_folder']],
                witcher_file_browser,
            )
            file_entries = _sort_browser_items(
                [item for item in filtered_items if not item['is_folder']],
                witcher_file_browser,
            )
            folder_count = len(folder_entries)
            file_count = len(file_entries)
            page_size = _get_browser_file_page_size(context, witcher_file_browser.file_display_mode)
            file_stats = _get_browser_page_stats(file_entries, witcher_file_browser.file_page_index, page_size)
            if witcher_file_browser.file_page_index != file_stats["page_index"]:
                witcher_file_browser.file_page_index = file_stats["page_index"]

            # --- Folder column ---
            folder_col = split.column(align=True)
            folder_head = folder_col.row(align=True)
            folder_head.label(text="Folders", icon="FILE_FOLDER")
            folder_col.separator(factor=0.3)

            if witcher_file_browser.current_folder:
                up = folder_col.operator("witcher.navigate_folder", text=".. (Up)", icon='FILE_PARENT')
                up.target_folder = ""

            for item in folder_entries:
                folder_row = folder_col.row(align=True)
                op = folder_row.operator("witcher.navigate_folder",
                                        text=item['name'],
                                        icon='FILE_FOLDER',
                                        emboss=False)
                op.target_folder = (witcher_file_browser.current_folder + "\\" + item['name']
                                    if witcher_file_browser.current_folder else item['name'])
                # Copy folder path button
                full_folder_path = (witcher_file_browser.current_folder + "\\" + item['name']
                                   if witcher_file_browser.current_folder else item['name'])
                copy_op = folder_row.operator("witcher.copy_path", text="", icon="COPYDOWN")
                copy_op.path = full_folder_path

            # --- File column ---
            file_col = split.column(align=True)

            # Batch selection toggle and import button
            batch_row = file_col.row(align=True)
            batch_row.prop(witcher_file_browser, "batch_select_mode", text="", icon="CHECKBOX_HLT" if witcher_file_browser.batch_select_mode else "CHECKBOX_DEHLT")
            batch_row.label(text="Files", icon="FILE")
            if witcher_file_browser.batch_select_mode:
                selected_count = len(get_batch_selection())
                if file_count > 0:
                    batch_row.operator("witcher.select_all_batch_visible", text="All", icon='CHECKBOX_HLT')
                if selected_count > 0:
                    batch_row.operator("witcher.import_batch_selected", text=f"Import ({selected_count})", icon='IMPORT')
                    batch_row.operator("witcher.clear_batch_select", text="", icon='X')

            layout_row = file_col.row(align=True)
            layout_mode = layout_row.row(align=True)
            layout_mode.prop(witcher_file_browser, "file_display_mode", text="", icon_only=True, expand=True)
            layout_row.separator_spacer()
            tools_sub = layout_row.row(align=True)
            tools_sub.prop(witcher_file_browser, "sort_by", text="", icon_only=True)
            asc_icon = 'SORT_ASC' if witcher_file_browser.sort_ascending else 'SORT_DESC'
            tools_sub.prop(witcher_file_browser, "sort_ascending", text="", icon=asc_icon, toggle=True)
            size_sub = tools_sub.row(align=True)
            size_sub.enabled = witcher_file_browser.file_display_mode == 'GRID'
            size_sub.prop(witcher_file_browser, "grid_size_mode_live", text="", icon_only=True)
            if _is_browser_dev_mode_enabled() and witcher_file_browser.file_display_mode == 'GRID':
                tools_sub.prop(
                    witcher_file_browser,
                    "debug_wrap_show",
                    text="",
                    icon='TOOL_SETTINGS',
                    toggle=True,
                )
                if witcher_file_browser.debug_wrap_show:
                    dev_box = file_col.box()
                    dev_header = dev_box.row(align=True)
                    dev_header.label(text="Dev: Filename Wrap Tuning", icon='TOOL_SETTINGS')
                    current_mode = witcher_file_browser.grid_size_mode_live
                    dev_header.label(text=f"Active: {current_mode.title()}")
                    for mode_key, label in (("small", "Small"), ("medium", "Medium"), ("large", "Large")):
                        sub = dev_box.column(align=True)
                        sub.label(text=label, icon='CHECKMARK' if current_mode == mode_key.upper() else 'BLANK1')
                        grid = sub.grid_flow(row_major=True, columns=2, even_columns=True, align=True)
                        grid.prop(witcher_file_browser, f"debug_{mode_key}_padding_px")
                        grid.prop(witcher_file_browser, f"debug_{mode_key}_avg_char_px")
                        grid.prop(witcher_file_browser, f"debug_{mode_key}_chars_min")
                        grid.prop(witcher_file_browser, f"debug_{mode_key}_chars_max")

            if file_stats["total"] > page_size:
                file_nav = file_col.row(align=True)
                file_nav.scale_y = 0.9
                file_nav.label(text=f"{file_stats['visible_start']}-{file_stats['visible_end']} / {file_stats['total']}")
                prev_row = file_nav.row(align=True)
                prev_row.enabled = file_stats["page_index"] > 0
                prev_row.operator("witcher.browser_page", text="<").action = "prev"
                next_row = file_nav.row(align=True)
                next_row.enabled = file_stats["page_index"] < (file_stats["total_pages"] - 1)
                next_row.operator("witcher.browser_page", text=">").action = "next"

            file_col.separator(factor=0.3)

            if witcher_file_browser.file_display_mode == 'GRID':
                grid_columns = _get_browser_grid_columns(context)
                visible_items = list(file_stats["items"])
                grid_entries = []
                name_lines_by_path = {}
                reserved_name_lines = 1
                grid = file_col.grid_flow(
                    row_major=True,
                    columns=grid_columns,
                    even_columns=True,
                    even_rows=True,
                    align=True,
                )
                for item in visible_items:
                    display_name = item['name']
                    full_item_path = (witcher_file_browser.current_folder + "\\" + item['name']
                                     if witcher_file_browser.current_folder else item['name'])
                    if get_effective_cache_type(witcher_file_browser.active_cache_type) == "Collision":
                        ext = collision_extension_map.get(full_item_path, "")
                        if ext:
                            display_name = f"{item['name']} [{ext}]"

                    source_label = get_source_label(
                        context,
                        full_item_path,
                        loadmods=witcher_file_browser.loadmods,
                        cache_type=witcher_file_browser.active_cache_type,
                    )
                    if source_label.startswith("mod:"):
                        display_name = f"{display_name} [src:{source_label[4:]}]"
                    elif source_label == "vanilla" and witcher_file_browser.loadmods:
                        display_name = f"{display_name} [src:vanilla]"
                    elif witcher_file_browser.use_mods_priority:
                        mod_label = get_mod_override_label(full_item_path, loadmods=witcher_file_browser.loadmods)
                        if mod_label:
                            display_name = f"{display_name} [ovr:{mod_label}]"

                    w2ter_label = get_w2ter_buffer_label(item['name'])
                    if w2ter_label:
                        display_name = f"{display_name} [{w2ter_label}]"

                    file_exists, file_size = get_file_info(
                        context,
                        witcher_file_browser.active_cache_type,
                        full_item_path,
                        loadmods=witcher_file_browser.loadmods,
                    )
                    status_icon = get_status_icon(
                        context,
                        witcher_file_browser.active_cache_type,
                        full_item_path,
                        loadmods=witcher_file_browser.loadmods,
                    )
                    wrapped_name_lines = _split_grid_display_name_lines(
                        context,
                        display_name,
                        columns=grid_columns,
                    )
                    grid_entries.append({
                        "item": item,
                        "display_name": display_name,
                        "full_item_path": full_item_path,
                        "file_exists": file_exists,
                        "status_icon": status_icon,
                    })
                    name_lines_by_path[full_item_path] = wrapped_name_lines
                    reserved_name_lines = max(reserved_name_lines, len(wrapped_name_lines))

                for entry in grid_entries:
                    self._draw_grid_file_entry(
                        grid,
                        context,
                        witcher_file_browser,
                        entry["item"],
                        entry["display_name"],
                        entry["full_item_path"],
                        grid_columns=grid_columns,
                        name_lines=name_lines_by_path.get(entry["full_item_path"], [entry["display_name"]]),
                        reserved_name_lines=reserved_name_lines,
                        file_exists=entry["file_exists"],
                        status_icon=entry["status_icon"],
                    )
                placeholder_count = (grid_columns - (len(visible_items) % grid_columns)) % grid_columns
                for _ in range(placeholder_count):
                    placeholder = grid.column(align=True)
                    placeholder.enabled = False
                    placeholder.label(text="")
            else:
                for item in file_stats["items"]:
                    row = file_col.row(align=True)

                    # For collision cache, show extension suffix from Comtype
                    display_name = item['name']
                    full_item_path = (witcher_file_browser.current_folder + "\\" + item['name']
                                     if witcher_file_browser.current_folder else item['name'])
                    if get_effective_cache_type(witcher_file_browser.active_cache_type) == "Collision":
                        ext = collision_extension_map.get(full_item_path, "")
                        if ext:
                            display_name = f"{item['name']} [{ext}]"

                    source_label = get_source_label(
                        context,
                        full_item_path,
                        loadmods=witcher_file_browser.loadmods,
                        cache_type=witcher_file_browser.active_cache_type,
                    )
                    if source_label.startswith("mod:"):
                        display_name = f"{display_name} [src:{source_label[4:]}]"
                    elif source_label == "vanilla" and witcher_file_browser.loadmods:
                        display_name = f"{display_name} [src:vanilla]"
                    elif witcher_file_browser.use_mods_priority:
                        mod_label = get_mod_override_label(full_item_path, loadmods=witcher_file_browser.loadmods)
                        if mod_label:
                            display_name = f"{display_name} [ovr:{mod_label}]"

                    w2ter_label = get_w2ter_buffer_label(item['name'])
                    if w2ter_label:
                        display_name = f"{display_name} [{w2ter_label}]"

                    # Batch selection checkbox (first if in batch mode)
                    if witcher_file_browser.batch_select_mode:
                        is_selected = is_batch_selected(witcher_file_browser.active_cache_type, full_item_path)
                        select_icon = "CHECKBOX_HLT" if is_selected else "CHECKBOX_DEHLT"
                        sel_op = row.operator("witcher.toggle_batch_select", text="", icon=select_icon)
                        sel_op.path = full_item_path
                        sel_op.cache_type = witcher_file_browser.active_cache_type

                    # Exported status indicator (always present for alignment)
                    file_exists, file_size = get_file_info(context, witcher_file_browser.active_cache_type, full_item_path, loadmods=witcher_file_browser.loadmods)
                    status_icon = get_status_icon(
                        context,
                        witcher_file_browser.active_cache_type,
                        full_item_path,
                        loadmods=witcher_file_browser.loadmods,
                    )
                    row.label(text="", icon=status_icon)

                    # Action buttons FIRST (before filename)
                    copy_op = row.operator("witcher.copy_path", text="", icon="COPYDOWN")
                    copy_op.path = full_item_path

                    loc_sub = row.row(align=True)
                    loc_sub.enabled = file_exists
                    op_loc = loc_sub.operator("witcher.open_file_location", text="", icon="FILEBROWSER")
                    op_loc.file_path = full_item_path
                    op_loc.cache_type = witcher_file_browser.active_cache_type

                    if not witcher_file_browser.batch_select_mode and cache_supports_scene_import(witcher_file_browser.active_cache_type):
                        op1 = row.operator("witcher.file_action_import_to_scene",
                                            text="", icon='IMPORT')
                        op1.file_path = item['name']

                    _add_browser_preview_button_slot(
                        row,
                        context,
                        witcher_file_browser.active_cache_type,
                        full_item_path,
                        loadmods=witcher_file_browser.loadmods,
                    )

                    if cache_supports_sound_preview(witcher_file_browser.active_cache_type):
                        is_playing = _sound_preview_matches(witcher_file_browser.active_cache_type, full_item_path)
                        op_sound = row.operator(
                            "witcher.sound_preview_toggle",
                            text="",
                            icon='CANCEL' if is_playing else 'PLAY',
                        )
                        op_sound.file_path = full_item_path
                        op_sound.cache_type = witcher_file_browser.active_cache_type

                    if witcher_file_browser.active_cache_type != "Texture" and not is_disk_cache(witcher_file_browser.active_cache_type):
                        op2 = row.operator("witcher.file_action",
                                            text="", icon='EXPORT')
                        op2.file_path = item['name']

                    is_revealed = _is_revealed_browser_item(witcher_file_browser, full_item_path)
                    icon_info = _get_browser_item_icon_info(
                        context,
                        witcher_file_browser.active_cache_type,
                        full_item_path,
                        loadmods=witcher_file_browser.loadmods,
                    )
                    icon_id = int(icon_info.get("icon_id", 0) or 0)
                    name_row = row.row(align=True)
                    name_row.alert = is_revealed
                    if icon_id:
                        name_row.label(text=display_name, icon_value=icon_id)
                    else:
                        name_row.label(text=display_name, icon='VIEWZOOM' if is_revealed else 'FILE')

                    stats_op = row.operator("witcher.file_item_stats", text="", icon='INFO')
                    stats_op.file_path = full_item_path
                    stats_op.cache_type = witcher_file_browser.active_cache_type
                    stats_op.loadmods = witcher_file_browser.loadmods


            # File/folder count display at bottom
            layout.separator(factor=0.3)
            count_row = layout.row()
            count_row.label(text=f"{folder_count} folders, {file_count} files")



    def draw_global_search_results(self, layout, query):
        """Search across all cache types and display results with cache source."""
        MAX_RESULTS = 100
        witcher_file_browser = bpy.context.scene.witcher_file_browser
        loadmods = witcher_file_browser.loadmods

        # Use cached search results (avoids re-searching on every UI redraw)
        results = get_cached_search_results(query, "", folder_structure, loadmods=loadmods)

        # Display results
        col = layout.column(align=True)
        if not results:
            col.label(text="No results found", icon='ERROR')
            return

        results_header = col.row(align=True)
        capped_count = min(len(results), MAX_RESULTS)
        results_header.label(text=f"{capped_count} result(s)", icon='FILE')
        results_header.operator("witcher.copy_all_search_paths", text="Copy All Paths", icon="COPYDOWN")
        col.separator(factor=0.3)

        for cache_type, path in results[:MAX_RESULTS]:
            # Use split for proper proportions: 10% cache type, 65% path, 25% buttons
            row_split = col.split(factor=0.1, align=True)
            cache_abbrev = cache_type[:3]
            row_split.label(text=f"[{cache_abbrev}]")
            path_btn_split = row_split.split(factor=0.78, align=True)
            # Checkmark indicator (always present for alignment)
            path_row = path_btn_split.row(align=True)
            file_exists, file_size = get_file_info(bpy.context, cache_type, path, loadmods=loadmods)
            source_label = get_source_label(bpy.context, path, loadmods=loadmods, cache_type=cache_type)
            status_icon = get_status_icon(bpy.context, cache_type, path, loadmods=loadmods)
            path_row.label(text="", icon=status_icon)
            display_label = path
            if source_label.startswith("mod:"):
                display_label = f"{display_label} [src:{source_label[4:]}]"
            elif source_label == "vanilla" and witcher_file_browser.loadmods:
                display_label = f"{display_label} [src:vanilla]"
            elif witcher_file_browser.use_mods_priority:
                mod_label = get_mod_override_label(path, loadmods=loadmods)
                if mod_label:
                    display_label = f"{display_label} [ovr:{mod_label}]"
            icon_info = _get_browser_item_icon_info(
                bpy.context,
                cache_type,
                path,
                loadmods=loadmods,
            )
            icon_id = int(icon_info.get("icon_id", 0) or 0)
            if icon_id:
                path_row.label(text=display_label, icon_value=icon_id)
            else:
                path_row.label(text=display_label, icon='FILE')
            btns = path_btn_split.row(align=True)
            # Copy path
            copy_op = btns.operator("witcher.copy_path", text="", icon="COPYDOWN")
            copy_op.path = path
            # Go to source button - navigate to cache and folder
            op_goto = btns.operator("witcher.goto_global_search_result", text="", icon='FILE_PARENT')
            op_goto.file_path = path
            op_goto.cache_type = cache_type
            # Open file location on disk (always present for alignment)
            loc_sub = btns.row(align=True)
            loc_sub.enabled = file_exists
            op_loc = loc_sub.operator("witcher.open_file_location", text="", icon="FILEBROWSER")
            op_loc.file_path = path
            op_loc.cache_type = cache_type
            _add_browser_preview_button_slot(
                btns,
                bpy.context,
                cache_type,
                path,
                loadmods=loadmods,
            )
            if cache_supports_sound_preview(cache_type):
                is_playing = _sound_preview_matches(cache_type, path)
                op_sound = btns.operator(
                    "witcher.sound_preview_toggle",
                    text="",
                    icon='CANCEL' if is_playing else 'PLAY',
                )
                op_sound.file_path = path
                op_sound.cache_type = cache_type
            if cache_supports_scene_import(cache_type):
                op = btns.operator("witcher.file_action_global_import", text="", icon='IMPORT')
            else:
                op = btns.operator("witcher.file_action_global_export", text="", icon='EXPORT')
            op.file_path = path
            op.cache_type = cache_type
            # Item stats popup (cache metadata)
            stats_op = btns.operator("witcher.file_item_stats", text="", icon='INFO')
            stats_op.file_path = path
            stats_op.cache_type = cache_type
            stats_op.loadmods = loadmods

        if len(results) > MAX_RESULTS:
            col.label(text=f"... and {len(results) - MAX_RESULTS} more results", icon='INFO')

    def draw_recent_view(self, layout, context):
        """Draw the recent activity view."""
        recent = get_recent_imports(context)

        header_row = layout.row()
        header_row.label(text="Recent", icon='TIME')
        if recent:
            header_row.operator("witcher.clear_recent_imports", text="Clear", icon='X')

        layout.separator(factor=0.5)

        if not recent:
            layout.label(text="No recent imports or archives", icon='INFO')
            return

        col = layout.column(align=True)
        for item in recent:
            path = item.get('path', '')
            cache_type = item.get('cache_type', '')
            effective_cache_type = get_effective_cache_type(cache_type)
            cache_abbrev = effective_cache_type[:3] if effective_cache_type else "?"
            kind = item.get('kind', RECENT_KIND_IMPORT)
            archive_available = _can_restore_external_archive(cache_type, path)

            row = col.split(factor=0.1, align=True)
            row.label(text=f"[{cache_abbrev}]")
            path_row = row.split(factor=0.75, align=True)
            if kind == RECENT_KIND_EXTERNAL_ARCHIVE:
                display_name = item.get('display_name', '') or os.path.basename(path) or path
                path_row.label(text=display_name, icon=get_cache_type_icon(cache_type))
            else:
                display_path = path or "(unknown path)"
                path_row.label(text=display_path, icon='FILE')
            btns = path_row.row(align=True)

            # Copy path
            copy_op = btns.operator("witcher.copy_path", text="", icon="COPYDOWN")
            copy_op.path = path

            if kind == RECENT_KIND_EXTERNAL_ARCHIVE:
                action_row = btns.row(align=True)
                action_row.enabled = archive_available
                op = action_row.operator("witcher.reopen_external_archive", text="", icon='FILEBROWSER')
                op.archive_path = path
                op.cache_type = cache_type
            else:
                # Go to source
                goto_available = True
                if is_external_cache(cache_type):
                    archive_path = item.get('archive_path', '')
                    goto_available = _can_restore_external_archive(cache_type, archive_path)
                goto_row = btns.row(align=True)
                goto_row.enabled = goto_available
                op_goto = goto_row.operator("witcher.goto_global_search_result", text="", icon='FILE_PARENT')
                op_goto.file_path = path
                op_goto.cache_type = cache_type
                op_goto.archive_path = item.get('archive_path', '')

                # Import again
                import_available = True
                if is_external_cache(cache_type):
                    archive_path = item.get('archive_path', '')
                    import_available = _can_restore_external_archive(cache_type, archive_path)
                action_row = btns.row(align=True)
                action_row.enabled = import_available
                op = action_row.operator("witcher.import_recent", text="", icon='IMPORT')
                op.path = path
                op.cache_type = cache_type
                op.archive_path = item.get('archive_path', '')

    def draw_bookmarks_view(self, layout, context):
        """Draw the bookmarks view."""
        bookmarks = get_bookmarks(context)
        witcher_file_browser = context.scene.witcher_file_browser

        header_row = layout.row()
        header_row.label(text="Bookmarks", icon='BOOKMARKS')

        # Add current location to bookmarks
        if witcher_file_browser.active_cache_type:
            add_bm = header_row.operator("witcher.add_bookmark", text="Add Current", icon='ADD')
            add_bm.path = witcher_file_browser.current_folder
            add_bm.cache_type = witcher_file_browser.active_cache_type
            if is_external_cache(witcher_file_browser.active_cache_type):
                session = get_external_archive_session(witcher_file_browser.active_cache_type)
                if session:
                    add_bm.archive_path = session.get("archive_path", "") or ""

        layout.separator(factor=0.5)

        if not bookmarks:
            layout.label(text="No bookmarks saved", icon='INFO')
            layout.label(text="Navigate to a folder and click 'Add Current'")
            return

        col = layout.column(align=True)
        for bm in bookmarks:
            path = bm.get('path', '')
            cache_type = bm.get('cache_type', '')
            archive_path = bm.get('archive_path', '')
            display_path = path or "Entire archive"
            effective_cache_type = get_effective_cache_type(cache_type)
            goto_available = True
            if is_external_cache(cache_type):
                goto_available = _can_restore_external_archive(cache_type, archive_path)

            row = col.split(factor=0.1, align=True)
            cache_abbrev = effective_cache_type[:3] if effective_cache_type else "?"
            row.label(text=f"[{cache_abbrev}]")
            path_row = row.split(factor=0.75, align=True)
            path_row.label(text=display_path, icon='BOOKMARKS')
            btns = path_row.row(align=True)

            # Copy path
            copy_op = btns.operator("witcher.copy_path", text="", icon="COPYDOWN")
            copy_op.path = path

            # Go to bookmark
            goto_row = btns.row(align=True)
            goto_row.enabled = goto_available
            op_goto = goto_row.operator("witcher.goto_bookmark", text="", icon='FILE_PARENT')
            op_goto.path = path
            op_goto.cache_type = cache_type
            op_goto.archive_path = archive_path

            # Remove bookmark
            op_rm = btns.operator("witcher.remove_bookmark", text="", icon='X')
            op_rm.path = path
            op_rm.cache_type = cache_type
            op_rm.archive_path = archive_path

    def export_and_load_image(self, full_path):
        def export_item(item):
            final_item:TextureCacheItem = item[-1]
            export_root = get_texture_path(bpy.context)
            exportPath = os.path.join(export_root, final_item.name)
            final_item.extract_to_file(exportPath)
            return exportPath

        manager = LoadTextureManager()
        item = manager.find_item_by_path_name(full_path)
        exportPath = export_item(item)
        return exportPath

    def display_image_in_modal(self, layout, image_path):
        # Load the image into a preview collection
        pcoll = bpy.utils.previews.new()
        thumb = pcoll.load("custom_icon", win_safe_path(image_path), 'IMAGE')

        # Use the preview in the UI
        layout.template_icon_view(pcoll, "custom_icon", show_labels=True, scale=8.0, scale_popup=6.0)

        # Clean up the preview collection
        bpy.utils.previews.remove(pcoll)

class ClearSearchOperator(Operator):
    """Clear the search query"""
    bl_idname = "witcher.clear_search"
    bl_label = "Clear Search"

    def execute(self, context):
        witcher_file_browser = context.scene.witcher_file_browser
        witcher_file_browser.search_query = ""
        clear_search_cache()
        return {'FINISHED'}

class StatusIconHelpOperator(Operator):
    """Show help for status icons"""
    bl_idname = "witcher.status_icon_help"
    bl_label = "Status Icon Legend"
    bl_description = "Show status icon meanings"

    def invoke(self, context, event):
        return context.window_manager.invoke_popup(self, width=380)

    def draw(self, context):
        layout = self.layout
        wfb = context.scene.witcher_file_browser
        layout.label(text="Status Icons", icon='INFO')
        layout.separator()
        layout.label(text="Extracted and matches bundle", icon='CHECKMARK')
        layout.label(text="Extracted but mismatched (size/source/buffers)", icon='ERROR')
        layout.label(text="Not extracted (will extract on import)", icon='BLANK1')
        layout.separator()
        layout.label(text="Disk sources: checkmark means file exists on disk", icon='INFO')
        if wfb.use_mods_priority:
            layout.label(text="ovr:MOD = mod override available", icon='INFO')
        if wfb.use_mods_priority or wfb.loadmods:
            layout.label(text="src:MOD = extracted from mod", icon='INFO')
        if wfb.loadmods:
            layout.label(text="src:vanilla = extracted from vanilla while browsing mods", icon='INFO')

    def execute(self, context):
        return {'FINISHED'}


class FileItemStatsOperator(Operator):
    """Show cache metadata for the selected item."""
    bl_idname = "witcher.file_item_stats"
    bl_label = "Item Stats"
    bl_description = "Show compressed/uncompressed sizes and compression metadata"

    file_path: StringProperty()
    cache_type: StringProperty(default="")
    loadmods: BoolProperty(default=False)

    def invoke(self, context, event):
        self._stats = get_cache_item_stats(
            context,
            self.cache_type or context.scene.witcher_file_browser.active_cache_type,
            self.file_path,
            loadmods=self.loadmods,
        )
        return context.window_manager.invoke_popup(self, width=460)

    def draw(self, context):
        layout = self.layout
        stats = getattr(self, "_stats", None) or get_cache_item_stats(
            context,
            self.cache_type or context.scene.witcher_file_browser.active_cache_type,
            self.file_path,
            loadmods=self.loadmods,
        )

        cache_label = stats.get("cache_type") or "Unknown"
        item_path = stats.get("item_path") or self.file_path

        layout.label(text="Asset Stats", icon='INFO')
        col = layout.column(align=True)
        col.label(text=f"Cache: {cache_label}")
        col.label(text=f"Path: {item_path}")

        if not stats.get("found"):
            col.separator()
            col.label(text="No metadata found for this item in current cache.", icon='ERROR')
            return

        size_u = int(stats.get("size_uncompressed", 0) or 0)
        size_c = int(stats.get("size_compressed", 0) or 0)
        size_disk = int(stats.get("size_on_disk", 0) or 0)
        compression = stats.get("compression", "Unknown")
        compression_code = stats.get("compression_code", "")
        compressed_flag = stats.get("is_compressed", None)

        col.separator()
        col.label(text=f"Uncompressed: {_format_size_bytes(size_u)} ({size_u:,} bytes)")
        col.label(text=f"Compressed: {_format_size_bytes(size_c)} ({size_c:,} bytes)")
        if size_u > 0 and size_c > 0:
            ratio = (size_c / size_u) * 100.0
            delta = size_u - size_c
            sign = "-" if delta >= 0 else "+"
            col.label(
                text=f"Ratio: {ratio:.1f}% ({sign}{_format_size_bytes(abs(delta))} vs uncompressed)"
            )

        if compressed_flag is True:
            col.label(text="Compressed: Yes", icon='CHECKMARK')
        elif compressed_flag is False:
            col.label(text="Compressed: No", icon='X')
        else:
            col.label(text="Compressed: Unknown", icon='QUESTION')

        comp_text = f"Compression: {compression}"
        if compression_code:
            comp_text += f" (code {compression_code})"
        col.label(text=comp_text)

        if size_disk > 0:
            col.label(text=f"Extracted on disk: {_format_size_bytes(size_disk)} ({size_disk:,} bytes)")

    def execute(self, context):
        return {'FINISHED'}


class GoHomeOperator(Operator):
    """Return to cache type selection (root level)"""
    bl_idname = "witcher.browser_go_home"
    bl_label = "Go Home"

    def execute(self, context):
        global folder_structure, _nav_history, _nav_index, _search_cache, _file_source_map, _file_source_info

        witcher_file_browser = context.scene.witcher_file_browser
        _clear_browser_reveal(context)

        # Reset all browser state to defaults
        witcher_file_browser.active_cache_type = ""
        witcher_file_browser.current_folder = ""
        witcher_file_browser.search_query = ""
        witcher_file_browser.path_input = ""
        witcher_file_browser.extension_filter = ""
        witcher_file_browser.file_page_index = 0

        # Clear folder structure completely
        folder_structure.items = {}
        folder_structure.index = {}
        folder_structure.cache_type = ""
        _file_source_map = {}
        _file_source_info = {}

        # Clear navigation history
        _nav_history = []
        _nav_index = -1

        # Clear search cache
        _search_cache = {'query': '', 'cache_type': '', 'results': []}

        return {'FINISHED'}


class ClearExtensionFilterOperator(Operator):
    """Clear the current view filter"""
    bl_idname = "witcher.clear_extension_filter"
    bl_label = "Clear View Filter"

    def execute(self, context):
        witcher_file_browser = context.scene.witcher_file_browser
        witcher_file_browser.extension_filter = ""
        return {'FINISHED'}


class AddBookmarkOperator(Operator):
    """Add current location to bookmarks"""
    bl_idname = "witcher.add_bookmark"
    bl_label = "Add Bookmark"
    path: StringProperty()
    cache_type: StringProperty()
    archive_path: StringProperty(default="")

    def execute(self, context):
        if add_bookmark(context, self.path, self.cache_type, archive_path=self.archive_path):
            self.report({'INFO'}, f"Bookmarked: {self.path or 'Root'}")
        else:
            self.report({'INFO'}, "Already bookmarked")
        return {'FINISHED'}


class RemoveBookmarkOperator(Operator):
    """Remove bookmark"""
    bl_idname = "witcher.remove_bookmark"
    bl_label = "Remove Bookmark"
    path: StringProperty()
    cache_type: StringProperty()
    archive_path: StringProperty(default="")

    def execute(self, context):
        remove_bookmark(context, self.path, self.cache_type, archive_path=self.archive_path)
        self.report({'INFO'}, "Bookmark removed")
        return {'FINISHED'}


class GotoBookmarkOperator(Operator):
    """Navigate to a bookmarked location"""
    bl_idname = "witcher.goto_bookmark"
    bl_label = "Go to Bookmark"
    path: StringProperty()
    cache_type: StringProperty()
    archive_path: StringProperty(default="")

    def execute(self, context):
        try:
            _navigate_browser_to_location(
                context,
                self.cache_type,
                self.path,
                archive_path=self.archive_path,
            )
        except Exception as exc:
            self.report({'ERROR'}, f"Failed to open bookmark: {exc}")
            return {'CANCELLED'}
        return {'FINISHED'}


class ClearRecentImportsOperator(Operator):
    """Clear recent activity list"""
    bl_idname = "witcher.clear_recent_imports"
    bl_label = "Clear Recent"

    def execute(self, context):
        clear_recent_imports(context)
        self.report({'INFO'}, "Recent items cleared")
        return {'FINISHED'}


class ReopenExternalArchiveOperator(Operator):
    """Reopen a standalone archive in the asset browser"""
    bl_idname = "witcher.reopen_external_archive"
    bl_label = "Reopen External Archive"

    archive_path: StringProperty()
    cache_type: StringProperty()

    def execute(self, context):
        try:
            info = _prepare_external_archive_browser(context, self.cache_type, self.archive_path)
        except Exception as exc:
            self.report({'ERROR'}, f"Failed to reopen external archive: {exc}")
            return {'CANCELLED'}

        self.report(
            {'INFO'},
            f"Loaded {get_effective_cache_type(self.cache_type).lower()} archive: "
            f"{os.path.basename(info['archive_path'])} ({info['item_count']} items)"
        )
        return {'FINISHED'}


class ImportRecentOperator(Operator):
    """Import a file from recent imports"""
    bl_idname = "witcher.import_recent"
    bl_label = "Import Recent"
    path: StringProperty()
    cache_type: StringProperty()
    archive_path: StringProperty(default="")

    def execute(self, context):
        witcher_file_browser = context.scene.witcher_file_browser
        original_cache_type = witcher_file_browser.active_cache_type
        witcher_file_browser.active_cache_type = self.cache_type
        invoke_mode = 'INVOKE_DEFAULT' if getattr(witcher_file_browser, "open_import_dialog", False) else 'EXEC_DEFAULT'

        if is_external_cache(self.cache_type) and not _has_matching_external_archive_session(self.cache_type, self.archive_path):
            if not self.archive_path:
                self.report({'ERROR'}, "This standalone archive is no longer loaded; reopen it from Recent first")
                return {'CANCELLED'}
            try:
                info = _ensure_external_archive_session(context, self.cache_type, self.archive_path)
                _prime_external_archive_export_root(context, info.get("archive_path", self.archive_path))
                add_recent_external_archive(context, self.cache_type, self.archive_path)
            except Exception as exc:
                self.report({'ERROR'}, f"Failed to reload standalone archive: {exc}")
                return {'CANCELLED'}

        try:
            return bpy.ops.witcher.file_action_import_to_scene(invoke_mode, file_path=self.path)
        finally:
            witcher_file_browser.active_cache_type = original_cache_type


class ToggleBatchSelectOperator(Operator):
    """Toggle file selection for batch import"""
    bl_idname = "witcher.toggle_batch_select"
    bl_label = "Toggle Selection"
    path: StringProperty()
    cache_type: StringProperty()

    def execute(self, context):
        selected = toggle_batch_selection(self.cache_type, self.path)
        status = "Selected" if selected else "Deselected"
        self.report({'INFO'}, f"{status}: {self.path.split(chr(92))[-1]}")
        return {'FINISHED'}


class SelectAllBatchVisibleOperator(Operator):
    """Select all visible files in the current folder view"""
    bl_idname = "witcher.select_all_batch_visible"
    bl_label = "Select All Visible"

    def execute(self, context):
        witcher_file_browser = context.scene.witcher_file_browser
        cache_type = witcher_file_browser.active_cache_type
        visible_files = get_visible_batch_file_paths(context)
        if not visible_files:
            self.report({'WARNING'}, "No visible files to select")
            return {'CANCELLED'}

        added = 0
        for path in visible_files:
            if add_batch_selection(cache_type, path):
                added += 1

        self.report({'INFO'}, f"Selected {len(visible_files)} visible files ({added} new)")
        return {'FINISHED'}


class ClearBatchSelectOperator(Operator):
    """Clear all batch selections"""
    bl_idname = "witcher.clear_batch_select"
    bl_label = "Clear Selection"

    def execute(self, context):
        clear_batch_selection()
        self.report({'INFO'}, "Selection cleared")
        return {'FINISHED'}


class ImportBatchSelectedOperator(Operator):
    """Import all selected files"""
    bl_idname = "witcher.import_batch_selected"
    bl_label = "Import Selected"

    def execute(self, context):
        selected = get_batch_selection()
        if not selected:
            self.report({'WARNING'}, "No files selected")
            return {'CANCELLED'}

        witcher_file_browser = context.scene.witcher_file_browser
        original_cache_type = witcher_file_browser.active_cache_type

        imported = 0
        for cache_type, path in selected:
            witcher_file_browser.active_cache_type = cache_type
            try:
                bpy.ops.witcher.file_action_import_to_scene(file_path=path)
                imported += 1
            except Exception as e:
                log.error("Failed to import %s: %s", path, e)

        witcher_file_browser.active_cache_type = original_cache_type
        clear_batch_selection()

        self.report({'INFO'}, f"Imported {imported} files")
        return {'FINISHED'}


class BrowserPageOperator(Operator):
    """Navigate browser list pages"""
    bl_idname = "witcher.browser_page"
    bl_label = "Browser Page"

    action: StringProperty(default="next")

    def execute(self, context):
        browser = context.scene.witcher_file_browser
        if browser.search_query:
            return {'CANCELLED'}

        folder_items = folder_structure.get_items(browser.current_folder)
        filter_text = browser.extension_filter.strip().lower()
        if filter_text:
            filtered_items = [
                item for item in folder_items
                if item['is_folder'] or filter_text in item['name'].lower()
            ]
        else:
            filtered_items = folder_items

        items = [item for item in filtered_items if not item['is_folder']]
        page_size = _get_browser_file_page_size(context, browser.file_display_mode)
        stats = _get_browser_page_stats(items, browser.file_page_index, page_size)
        current = stats["page_index"]
        last = max(0, stats["total_pages"] - 1)
        if self.action == "prev":
            target_index = max(0, current - 1)
        elif self.action == "next":
            target_index = min(last, current + 1)
        else:
            return {'CANCELLED'}

        browser.file_page_index = target_index
        return {'FINISHED'}


class CopyPathOperator(Operator):
    """Copy path to clipboard"""
    bl_idname = "witcher.copy_path"
    bl_label = "Copy Path"
    path: StringProperty()

    def execute(self, context):
        bpy.context.window_manager.clipboard = self.path
        self.report({'INFO'}, f"Copied: {self.path}")
        return {'FINISHED'}


class CopyAllSearchPathsOperator(Operator):
    """Copy all search result paths to clipboard (one per line)"""
    bl_idname = "witcher.copy_all_search_paths"
    bl_label = "Copy All Paths"
    bl_description = "Copy all search result paths to clipboard, one per line"

    def execute(self, context):
        wfb = context.scene.witcher_file_browser
        results = get_cached_search_results(
            wfb.search_query, wfb.active_cache_type, folder_structure, loadmods=wfb.loadmods
        )
        filter_text = wfb.extension_filter.strip().lower()
        if not results:
            self.report({'WARNING'}, "No search results to copy")
            return {'CANCELLED'}

        if wfb.active_cache_type:
            paths = [item for item in results if not filter_text or filter_text in item.lower()]
        else:
            paths = [path for _, path in results]

        if not paths:
            self.report({'WARNING'}, "No results match the current filter")
            return {'CANCELLED'}

        bpy.context.window_manager.clipboard = "\n".join(paths)
        self.report({'INFO'}, f"Copied {len(paths)} paths to clipboard")
        return {'FINISHED'}


class OpenFileLocationOperator(Operator):
    """Reveal an exported file in the OS file browser"""
    bl_idname = "witcher.open_file_location"
    bl_label = "Open File Location"
    file_path: StringProperty()
    cache_type: StringProperty(default="")

    def execute(self, context):
        try:
            witcher_file_browser = context.scene.witcher_file_browser
            cache_type = self.cache_type or witcher_file_browser.active_cache_type

            if is_disk_cache(cache_type):
                abs_path = get_disk_abs_path(cache_type, self.file_path)
                if not abs_path:
                    self.report({'WARNING'}, f"File not found: {self.file_path}")
                    return {'CANCELLED'}
            else:
                abs_path = get_browser_item_output_path(
                    context,
                    cache_type,
                    self.file_path,
                    witcher_file_browser.loadmods,
                )

            abs_path = win_safe_path(abs_path or "")
            if os.path.isfile(abs_path):
                try:
                    import subprocess

                    subprocess.Popen(["explorer", "/select,", abs_path])
                    return {'FINISHED'}
                except Exception:
                    pass

            parent_dir = abs_path if os.path.isdir(abs_path) else os.path.dirname(abs_path)
            if os.path.isdir(parent_dir):
                bpy.ops.wm.path_open(filepath=parent_dir)
                return {'FINISHED'}
            else:
                self.report({'WARNING'}, f"Directory not found: {parent_dir}")
                return {'CANCELLED'}
        except Exception as e:
            self.report({'ERROR'}, f"Failed to open location: {e}")
            return {'CANCELLED'}


class NavigateToPathOperator(Operator):
    """Navigate to a typed/pasted path"""
    bl_idname = "witcher.navigate_to_path"
    bl_label = "Go to Path"

    def execute(self, context):
        witcher_file_browser = context.scene.witcher_file_browser
        path = witcher_file_browser.path_input.strip().replace("/", "\\")

        # Remove leading/trailing backslashes
        path = path.strip("\\")

        if not path:
            witcher_file_browser.path_input = ""
            return {'FINISHED'}

        # Check if this looks like a file path (has an extension in last part)
        parts = path.split("\\")
        last_part = parts[-1] if parts else ""
        is_likely_file = "." in last_part and not last_part.startswith(".")

        # If it's a file path, navigate to its parent folder
        if is_likely_file and len(parts) > 1:
            parent_path = "\\".join(parts[:-1])
            if folder_structure.path_exists(parent_path):
                _navigate_browser_to_location(
                    context,
                    witcher_file_browser.active_cache_type,
                    parent_path,
                    reveal_file=path,
                    clear_filter=True,
                )
                return {'FINISHED'}

        # Check if path exists as a folder
        if folder_structure.path_exists(path):
            _clear_browser_reveal(context)
            witcher_file_browser.current_folder = path
            add_to_nav_history(witcher_file_browser.active_cache_type, path)
            save_browser_state(context, witcher_file_browser.active_cache_type, path)
            return {'FINISHED'}

        # Try to find the closest valid parent
        valid_path = ""
        for i, part in enumerate(parts):
            test_path = "\\".join(parts[:i+1])
            if folder_structure.path_exists(test_path):
                valid_path = test_path
            else:
                break

        if valid_path:
            _clear_browser_reveal(context)
            witcher_file_browser.current_folder = valid_path
            add_to_nav_history(witcher_file_browser.active_cache_type, valid_path)
            save_browser_state(context, witcher_file_browser.active_cache_type, valid_path)
            self.report({'INFO'}, f"Navigated to: {valid_path}")
        else:
            self.report({'WARNING'}, f"Path not found: {path}")
            return {'CANCELLED'}

        return {'FINISHED'}


class NavigateBackOperator(Operator):
    """Navigate back in history"""
    bl_idname = "witcher.navigate_back"
    bl_label = "Back"

    @classmethod
    def poll(cls, context):
        return can_go_back()

    def execute(self, context):
        global _nav_history, _nav_index, _nav_updating
        if not can_go_back():
            return {'CANCELLED'}

        _nav_updating = True
        _nav_index -= 1
        cache_type, folder = _nav_history[_nav_index]

        witcher_file_browser = context.scene.witcher_file_browser
        _clear_browser_reveal(context)

        # If cache type changed, need to repopulate folder structure
        if cache_type != witcher_file_browser.active_cache_type:
            witcher_file_browser.active_cache_type = cache_type
            if cache_type:
                SelectCacheTypeOperator.populate_folder_structure(self, cache_type, context)

        witcher_file_browser.current_folder = folder
        witcher_file_browser.search_query = ""
        _nav_updating = False
        return {'FINISHED'}


class NavigateForwardOperator(Operator):
    """Navigate forward in history"""
    bl_idname = "witcher.navigate_forward"
    bl_label = "Forward"

    @classmethod
    def poll(cls, context):
        return can_go_forward()

    def execute(self, context):
        global _nav_history, _nav_index, _nav_updating
        if not can_go_forward():
            return {'CANCELLED'}

        _nav_updating = True
        _nav_index += 1
        cache_type, folder = _nav_history[_nav_index]

        witcher_file_browser = context.scene.witcher_file_browser
        _clear_browser_reveal(context)

        # If cache type changed, need to repopulate folder structure
        if cache_type != witcher_file_browser.active_cache_type:
            witcher_file_browser.active_cache_type = cache_type
            if cache_type:
                SelectCacheTypeOperator.populate_folder_structure(self, cache_type, context)

        witcher_file_browser.current_folder = folder
        witcher_file_browser.search_query = ""
        _nav_updating = False
        return {'FINISHED'}


class GotoSearchResultOperator(Operator):
    """Navigate to the folder containing a search result"""
    bl_idname = "witcher.goto_search_result"
    bl_label = "Go to Source"
    file_path: StringProperty()

    def execute(self, context):
        # Get the parent folder of the file
        if "\\" in self.file_path:
            parent_folder = "\\".join(self.file_path.split("\\")[:-1])
        else:
            parent_folder = ""
        _navigate_browser_to_location(
            context,
            context.scene.witcher_file_browser.active_cache_type,
            parent_folder,
            reveal_file=self.file_path,
            clear_filter=True,
        )
        return {'FINISHED'}


class GotoGlobalSearchResultOperator(Operator):
    """Navigate to the cache and folder containing a search result"""
    bl_idname = "witcher.goto_global_search_result"
    bl_label = "Go to Source"
    file_path: StringProperty()
    cache_type: StringProperty()
    archive_path: StringProperty(default="")

    def execute(self, context):
        # Get the parent folder of the file
        if "\\" in self.file_path:
            parent_folder = "\\".join(self.file_path.split("\\")[:-1])
        else:
            parent_folder = ""
        try:
            _navigate_browser_to_location(
                context,
                self.cache_type,
                parent_folder,
                reveal_file=self.file_path,
                archive_path=self.archive_path,
                clear_filter=True,
            )
        except Exception as exc:
            self.report({'ERROR'}, f"Failed to open source location: {exc}")
            return {'CANCELLED'}
        return {'FINISHED'}


class NavigateFolderOperator(Operator):
    """Navigate to a folder"""
    bl_idname = "witcher.navigate_folder"
    bl_label = "Navigate Folder"
    target_folder: StringProperty()
    go_to_root: BoolProperty(default=False)

    def execute(self, context):
        witcher_file_browser = context.scene.witcher_file_browser

        # Return to root cache selection
        if self.go_to_root:
            _clear_browser_reveal(context)
            witcher_file_browser.active_cache_type = ""
            witcher_file_browser.current_folder = ""
            witcher_file_browser.search_query = ""
            # Clear caches to ensure clean state
            clear_search_cache()
            clear_nav_history()
            folder_structure.clear()
            return {'FINISHED'}

        _clear_browser_reveal(context)
        if self.target_folder:
            witcher_file_browser.current_folder = self.target_folder
        else:  # Go up one level
            current_path = witcher_file_browser.current_folder
            parent_path = "\\".join(current_path.split("\\")[:-1])
            witcher_file_browser.current_folder = parent_path

        # Add to navigation history and save state
        add_to_nav_history(witcher_file_browser.active_cache_type, witcher_file_browser.current_folder)
        save_browser_state(context, witcher_file_browser.active_cache_type, witcher_file_browser.current_folder)
        return {'FINISHED'}


class SelectCacheTypeOperator(Operator):
    """Select a cache type to browse"""
    bl_idname = "witcher.select_cache_type"
    bl_label = "Select Cache Type"

    cache_type: StringProperty()

    def execute(self, context):
        witcher_file_browser = context.scene.witcher_file_browser
        _clear_browser_reveal(context)
        witcher_file_browser.browser_view_mode = 'BROWSE'
        witcher_file_browser.active_cache_type = self.cache_type
        witcher_file_browser.current_folder = ""
        witcher_file_browser.search_query = ""

        # Reinitialize folder structure for this cache type
        self.populate_folder_structure(self.cache_type, context)

        # Add to navigation history and save state
        add_to_nav_history(self.cache_type, "")
        save_browser_state(context, self.cache_type, "")
        return {'FINISHED'}

    def populate_folder_structure(self, cache_type, context):
        global folder_structure
        global collision_extension_map
        folder_structure.clear()
        folder_structure.cache_type = cache_type
        clear_search_cache()

        witcher_file_browser = context.scene.witcher_file_browser
        loadmods = witcher_file_browser.loadmods
        if loadmods:
            clear_mod_index_cache()
        elif witcher_file_browser.use_mods_priority:
            clear_mod_index_cache()
            try:
                LoadBundleManager(loadmods=True, reset_cache=True)
            except Exception as e:
                log.error("Failed to refresh mod override cache: %s", e)

        if is_external_cache(cache_type):
            session = get_external_archive_session(cache_type)
            collision_extension_map.clear()
            if not session:
                return
            items = session.get("items", {})
            collision_exts = session.get("collision_exts", {})
            for original_key, item_list in items.items():
                key_str = str(original_key) if not isinstance(original_key, str) else original_key
                if _should_skip_buffer_name(key_str):
                    continue
                if cache_type == EXTERNAL_COLLISION_CACHE_TYPE:
                    ext = collision_exts.get(key_str)
                    if not ext and item_list:
                        final_item = item_list[-1] if isinstance(item_list, list) else item_list
                        ext = getattr(final_item, 'Extension', '')
                    if ext:
                        collision_extension_map[key_str] = ext
                folder_structure.add_path(key_str)
            return

        if is_disk_cache(cache_type):
            collision_extension_map.clear()
            populate_disk_folder_structure(cache_type, context)
            return

        # Get appropriate manager
        manager = None
        try:
            if cache_type == "Bundle":
                manager = LoadBundleManager(loadmods=loadmods, reset_cache=loadmods)
            elif cache_type == "Collision":
                manager = LoadCollisionManager(loadmods=loadmods, do_reload=loadmods)
            elif cache_type == "Texture":
                manager = LoadTextureManager(loadmods=loadmods, do_reload=loadmods)
            elif cache_type == "Sound":
                manager = LoadSoundManager(loadmods=loadmods, do_reload=loadmods)
            elif cache_type == "Speech":
                if loadmods:
                    return  # Speech doesn't support mod loading
                manager = LoadSpeechManager()
        except Exception as e:
            log.error("Failed to load %s manager: %s", cache_type, e)
            return

        if manager is None:
            return

        # For collision cache, build extension map from Comtype
        if cache_type == "Collision":
            collision_extension_map.clear()
            for original_key, item_list in manager.Items.items():
                key_str = str(original_key) if not isinstance(original_key, str) else original_key
                if _should_skip_buffer_name(key_str):
                    continue
                # Get the latest version's extension from Comtype
                final_item = item_list[-1] if isinstance(item_list, list) else item_list
                ext = getattr(final_item, 'Extension', '')
                if ext:
                    collision_extension_map[key_str] = ext
                folder_structure.add_path(key_str)
        else:
            # Populate from manager.Items
            for key in manager.Items.keys():
                # Handle non-string keys (e.g., Speech cache uses integer hash keys)
                key_str = str(key) if not isinstance(key, str) else key
                # Skip non-terrain buffer files (e.g., .w2mesh.1.buffer)
                if _should_skip_buffer_name(key_str):
                    continue
                folder_structure.add_path(key_str)

class FileActionOperatorImportToScene(Operator):
    """Import a file directly to the scene"""
    bl_idname = "witcher.file_action_import_to_scene"
    bl_label = "Import to Scene"
    file_path: StringProperty()

    def invoke(self, context, event):
        witcher_file_browser = getattr(context.scene, "witcher_file_browser", None)
        if not witcher_file_browser or not getattr(witcher_file_browser, "open_import_dialog", False):
            return self.execute(context)
        if self._build_import_state(context)["effective_cache_type"] == "Sound":
            return self.execute(context)

        with mod_loading_context(context):
            resolved = self._resolve_import_target(context)

        if not resolved:
            return {'CANCELLED'}

        try:
            dialog_result = asset_browser_import_bridge.invoke_asset_browser_import_dialog(context, resolved)
        except Exception as exc:
            log.error("Failed to open asset browser import dialog for %s: %s", resolved.get("abs_file_path"), exc, exc_info=True)
            self.report({'ERROR'}, f"Failed to open import dialog: {exc}")
            return {'CANCELLED'}

        if dialog_result is not None:
            return dialog_result

        with mod_loading_context(context):
            return self._execute_resolved_import(context, resolved)

    def execute(self, context):
        request = self._build_import_state(context)
        if request["effective_cache_type"] == "Sound":
            with mod_loading_context(context):
                return self._execute_sound_import(context, request)
        with mod_loading_context(context):
            resolved = self._resolve_import_target(context)
            if not resolved:
                return {'CANCELLED'}
            return self._execute_resolved_import(context, resolved)

    def _build_import_state(self, context):
        witcher_file_browser = context.scene.witcher_file_browser
        cache_type = witcher_file_browser.active_cache_type
        overwrite_existing = witcher_file_browser.mods_overwrite

        # Build full path for lookup
        full_path = (witcher_file_browser.current_folder + "\\" + self.file_path
                     if witcher_file_browser.current_folder else self.file_path)

        # For search results, file_path is already the full path
        if "\\" in self.file_path:
            full_path = self.file_path

        loadmods = witcher_file_browser.loadmods
        effective_cache_type = get_effective_cache_type(cache_type)
        full_path_norm = full_path.replace("/", "\\")
        return {
            "cache_type": cache_type,
            "overwrite_existing": overwrite_existing,
            "full_path": full_path,
            "loadmods": loadmods,
            "effective_cache_type": effective_cache_type,
            "full_path_norm": full_path_norm,
        }

    def _execute_sound_import(self, context, request):
        full_path = request["full_path"]
        cache_type = request["cache_type"]
        full_path_norm = request["full_path_norm"]
        loadmods = request["loadmods"]
        try:
            soundstrip, wav_path = import_sound_to_timeline(
                context,
                full_path_norm,
                loadmods=loadmods,
                cache_type=cache_type,
            )
        except Exception as exc:
            self.report({'ERROR'}, f"Sound import failed: {exc}")
            return {'CANCELLED'}
        add_recent_import(context, full_path, cache_type)
        self.report(
            {'INFO'},
            f"Imported sound strip: {getattr(soundstrip, 'name', os.path.basename(wav_path))}"
        )
        return {'FINISHED'}

    def _resolve_import_target(self, context):
        request = self._build_import_state(context)
        cache_type = request["cache_type"]
        overwrite_existing = request["overwrite_existing"]
        full_path = request["full_path"]
        loadmods = request["loadmods"]
        effective_cache_type = request["effective_cache_type"]
        full_path_norm = request["full_path_norm"]
        mod_name = ""
        if loadmods and "\\" in full_path_norm:
            mod_name = full_path_norm.split("\\", 1)[0]
        abs_file_path = None
        override_roots = []
        try:
            if is_disk_cache(cache_type):
                abs_file_path = get_disk_abs_path(cache_type, full_path)
                override_roots = get_repo_override_roots_for_item(context, cache_type, full_path)
            elif cache_type == EXTERNAL_BUNDLE_CACHE_TYPE:
                session = get_external_archive_session(cache_type)
                if not session:
                    self.report({'ERROR'}, "No external bundle loaded")
                    return None

                export_root = _get_external_archive_output_root(context, cache_type, create=True)
                if not export_root:
                    self.report({'ERROR'}, "Choose an export folder before importing from a standalone bundle")
                    return None
                item_lists = []
                if full_path_norm.endswith('.w2mesh') or full_path_norm.endswith('.w2anims'):
                    pattern = re.compile(re.escape(full_path_norm) + r"(\.\d+\.buffer)?$", re.IGNORECASE)
                    item_lists = [
                        value for key, value in session["items"].items()
                        if isinstance(key, str) and pattern.match(key.replace("/", "\\"))
                    ]
                else:
                    item = session["items"].get(full_path_norm)
                    if item:
                        item_lists = [item]

                if not item_lists:
                    self.report({'ERROR'}, f"Bundle item not found: {full_path}")
                    return None

                base_item = None
                buffer_items = []
                for item_list in item_lists:
                    final_item = item_list[-1] if isinstance(item_list, list) else item_list
                    item_name = getattr(final_item, 'name', None) or getattr(final_item, 'Name', full_path_norm)
                    inner_name = (item_name or full_path_norm).replace("/", "\\")
                    if inner_name.lower() == full_path_norm.lower():
                        base_item = (inner_name, final_item)
                    else:
                        buffer_items.append((inner_name, final_item))

                if base_item:
                    abs_file_path = os.path.join(export_root, base_item[0])
                    if prepare_extraction_target(abs_file_path, export_root):
                        base_item[1].extract_to_file(abs_file_path)
                else:
                    abs_file_path = os.path.join(export_root, full_path_norm)

                for inner_name, final_item in buffer_items:
                    out_path = os.path.join(export_root, inner_name)
                    if prepare_extraction_target(out_path, export_root):
                        final_item.extract_to_file(out_path)

            elif cache_type == "Bundle":
                if loadmods:
                    # For mods, use mod BundleManager to find and extract
                    manager = LoadBundleManager(loadmods=True)
                    addon_prefs = get_all_addon_prefs(context)
                    uncook_path = addon_prefs.uncook_path
                    full_path_norm = full_path.replace("/", "\\")
                    base_name = get_vanilla_path(full_path_norm, True)
                    abs_file_path = os.path.join(uncook_path, base_name)
                    mod_name = full_path_norm.split("\\", 1)[0] if "\\" in full_path_norm else ""
                    if not mod_name:
                        mod_name = get_mod_override_label(base_name, loadmods=True) or ""
                    mod_label = f"mod:{mod_name}" if mod_name else ""
                    base_exists = win_path_exists(abs_file_path)
                    base_source = get_source_for_path(uncook_path, base_name) if base_exists else ""
                    base_from_same_mod = mod_label and base_source == mod_label

                    if base_exists and not overwrite_existing and not base_from_same_mod:
                        log.debug("Skipping mod override for %s (overwrite disabled)", base_name)
                    else:
                        # Extract base + buffers for meshes/anims
                        item_lists = []
                        if full_path_norm.endswith('.w2mesh') or full_path_norm.endswith('.w2anims'):
                            pattern = re.compile(re.escape(full_path_norm) + r"(\.\d+\.buffer)?$", re.IGNORECASE)
                            item_lists = [
                                value for key, value in manager.Items.items()
                                if isinstance(key, str) and pattern.match(key.replace("/", "\\"))
                            ]
                        else:
                            item = manager.Items.get(full_path_norm) or manager.Items.get(full_path)
                            if item:
                                item_lists = [item]

                        if not item_lists and mod_name:
                            # Fallback: try mod-prefixed lookup by vanilla path
                            target = f"{mod_name}\\{base_name}"
                            if full_path_norm.endswith('.w2mesh') or full_path_norm.endswith('.w2anims'):
                                pattern = re.compile(re.escape(target) + r"(\.\d+\.buffer)?$", re.IGNORECASE)
                                item_lists = [
                                    value for key, value in manager.Items.items()
                                    if isinstance(key, str) and pattern.match(key.replace("/", "\\"))
                                ]
                            else:
                                for key, value in manager.Items.items():
                                    if isinstance(key, str) and key.replace("/", "\\").lower() == target.lower():
                                        item_lists = [value]
                                        break

                        if not item_lists:
                            self.report({'ERROR'}, f"Mod bundle item not found: {full_path}")
                            log.error("Mod bundle item not found: %s", full_path)
                            return None
                        else:
                            base_item = None
                            buffer_items = []
                            for item_list in item_lists:
                                final_item = item_list[-1] if isinstance(item_list, list) else item_list
                                item_name = getattr(final_item, 'name', None) or getattr(final_item, 'Name', full_path)
                                inner_name = item_name.replace("/", "\\") if item_name else base_name
                                if mod_name and isinstance(inner_name, str):
                                    mod_prefix = (mod_name + "\\").lower()
                                    if inner_name.lower().startswith(mod_prefix):
                                        inner_name = inner_name.split("\\", 1)[1]
                                if inner_name == base_name:
                                    base_item = (inner_name, final_item)
                                else:
                                    buffer_items.append((inner_name, final_item))

                            extracted_any = False
                            base_extracted = False
                            if base_item:
                                out_path = os.path.join(uncook_path, base_item[0])
                                if prepare_extraction_target(out_path, uncook_path):
                                    base_item[1].extract_to_file(out_path)
                                    extracted_any = True
                                    base_extracted = True

                            if base_extracted or base_from_same_mod:
                                for inner_name, final_item in buffer_items:
                                    out_path = os.path.join(uncook_path, inner_name)
                                    if prepare_extraction_target(out_path, uncook_path):
                                        final_item.extract_to_file(out_path)
                                        extracted_any = True

                            if extracted_any and (base_extracted or base_from_same_mod) and mod_label:
                                set_source_for_path(uncook_path, base_name, mod_label)

                    # If user explicitly wants mod override, do not fall back to vanilla
                    prefer_mods = bool(getattr(context.scene.witcher_file_browser, "use_mods_priority", False))
                    if prefer_mods and overwrite_existing:
                        if not win_path_exists(abs_file_path):
                            self.report({'ERROR'}, f"Mod extract failed: {base_name}")
                            return None
                        if mod_label:
                            source_label = get_source_for_path(uncook_path, base_name)
                            if source_label != mod_label:
                                self.report({'ERROR'}, f"Expected {mod_label} but found {source_label or 'unknown'}: {base_name}")
                                return None
                        else:
                            self.report({'ERROR'}, f"Could not resolve mod source for: {base_name}")
                            return None
                else:
                    abs_file_path = repo_file(full_path)
            elif cache_type == EXTERNAL_COLLISION_CACHE_TYPE:
                export_result = _export_external_archive_item(
                    context,
                    cache_type,
                    full_path_norm,
                    overwrite=overwrite_existing,
                )
                abs_file_path = export_result.get("primary_path", "")
            elif cache_type == EXTERNAL_TEXTURE_CACHE_TYPE:
                export_result = _export_external_archive_item(
                    context,
                    cache_type,
                    full_path_norm,
                    overwrite=overwrite_existing,
                )
                abs_file_path = export_result.get("primary_path", "")
            elif cache_type == EXTERNAL_SOUND_CACHE_TYPE:
                abs_file_path = ensure_sound_item_extracted(
                    context,
                    full_path_norm,
                    loadmods=loadmods,
                    cache_type=cache_type,
                )
            elif effective_cache_type == "Collision":
                # Extract collision file directly from collision cache (NOT via repo_file/BundleManager)
                manager = LoadCollisionManager(loadmods=loadmods)
                items = manager.find_item_by_path_name(full_path)
                if items:
                    final_item = items[-1] if isinstance(items, list) else items
                    # Get the correct output extension from Comtype
                    output_ext = getattr(final_item, 'Extension', '.nxs')
                    # Build output path: original path stem + correct extension
                    base_name = os.path.splitext(final_item.Name)[0]
                    output_name = base_name + output_ext

                    # Get the uncook path for extraction
                    addon_prefs = get_all_addon_prefs(context)
                    uncook_path = addon_prefs.uncook_path
                    abs_file_path = os.path.join(uncook_path, output_name)

                    # Extract directly from collision cache
                    if prepare_extraction_target(abs_file_path, uncook_path):
                        abs_file_path = final_item.extract_to_file(abs_file_path)
                else:
                    log.warning("Collision item not found: %s", full_path)
            elif cache_type == "Texture":
                # Extract texture file from cache
                manager = LoadTextureManager(loadmods=loadmods)
                items = manager.find_item_by_path_name(full_path_norm)
                if items:
                    final_item = items[-1] if isinstance(items, list) else items
                    item_name = getattr(final_item, 'Name', full_path_norm)
                    abs_file_path = repo_file(strip_mod_prefix(item_name, mod_name))
                    if abs_file_path:
                        texture_root = get_texture_path(context) or ""
                        uncook_root = get_uncook_path(context) or ""
                        prep_root = texture_root or uncook_root
                        try:
                            norm_abs = os.path.normcase(os.path.normpath(abs_file_path))
                            norm_uncook = os.path.normcase(os.path.normpath(uncook_root)) if uncook_root else ""
                            if norm_uncook and norm_abs.startswith(norm_uncook + os.sep):
                                prep_root = uncook_root
                        except Exception:
                            pass
                        if prepare_extraction_target(abs_file_path, prep_root):
                            final_item.extract_to_file(abs_file_path)
                        dds_path = os.path.splitext(abs_file_path)[0] + ".dds"
                        if win_path_exists(dds_path):
                            abs_file_path = dds_path
            elif cache_type == "Speech":
                manager = LoadSpeechManager()
                items = manager.find_item_by_hash(full_path)
                if items:
                    final_item = items[-1] if isinstance(items, list) else items
                    item_name = getattr(final_item, 'Name', full_path)
                    abs_file_path = repo_file(item_name)
                    if abs_file_path:
                        addon_prefs = get_all_addon_prefs(context)
                        uncook_path = addon_prefs.uncook_path
                        if prepare_extraction_target(abs_file_path, uncook_path):
                            final_item.extract_to_file(abs_file_path)
            else:
                # No cache type selected (shouldn't happen), try repo_file directly
                abs_file_path = repo_file(full_path)
        except Exception as e:
            log.error("Failed to get file: %s", e)
            return None

        abs_file_path = win_safe_path(abs_file_path) if abs_file_path else abs_file_path

        if not abs_file_path or not win_path_exists(abs_file_path):
            self.report({'ERROR'}, f"File not found: {abs_file_path}")
            log.error("File not found: %s", abs_file_path)
            return None

        request.update({
            "abs_file_path": abs_file_path,
            "override_roots": override_roots,
        })
        return request

    def _execute_resolved_import(self, context, resolved):
        cache_type = resolved["cache_type"]
        overwrite_existing = resolved["overwrite_existing"]
        full_path = resolved["full_path"]
        loadmods = resolved["loadmods"]
        effective_cache_type = resolved["effective_cache_type"]
        full_path_norm = resolved["full_path_norm"]
        abs_file_path = resolved["abs_file_path"]
        override_roots = resolved.get("override_roots") or []

        # Texture imports should always resolve cooked .xbm sources to a viewable/importable DDS first.
        if abs_file_path.lower().endswith(".xbm"):
            dds_path = os.path.splitext(abs_file_path)[0] + ".dds"
            if not win_path_exists(dds_path):
                try:
                    converted_path = convert_xbm_to_dds(abs_file_path)
                    if converted_path:
                        dds_path = converted_path
                except Exception as exc:
                    self.report({'ERROR'}, f"Failed to convert XBM to DDS: {exc}")
                    log.error("Failed to convert XBM to DDS for import: %s", abs_file_path, exc_info=True)
                    return {'CANCELLED'}
            if win_path_exists(dds_path):
                abs_file_path = dds_path
            else:
                self.report({'ERROR'}, f"Could not resolve DDS for texture: {os.path.basename(abs_file_path)}")
                log.error("Could not resolve DDS for texture import: %s", abs_file_path)
                return {'CANCELLED'}

        # Import based on file extension and cache type
        ext = file_helpers.getFilenameType(abs_file_path)

        try:
            if override_roots:
                set_repo_override_roots(override_roots, read_only=True)

            if effective_cache_type == "Collision":
                # Import collision based on actual extracted file extension
                if ext == ".nxs":
                    from ..importers import import_nxs
                    import_nxs.create_from_nxs(abs_file_path)
                elif ext == ".apb":
                    from ..cloth_util import importCloth

                    conv = ensure_apx_from_apb(context, abs_file_path, overwrite=False)
                    apx_path = conv.get("apx_path") or (os.path.splitext(abs_file_path)[0] + ".apx")
                    if not (apx_path and win_path_exists(apx_path)):
                        conv_status = conv.get("status", "")
                        if conv_status in {"apx_addon_disabled", "apx_sdk_missing"}:
                            set_external_import_dependency_alert(
                                "redcloth",
                                source_path=abs_file_path,
                                status=conv_status,
                                reason=conv.get("message", ""),
                            )
                        self.report(
                            {'ERROR'},
                            "Could not prepare .apx from .apb for cloth import: "
                            f"{conv.get('message') or conv.get('status')}"
                        )
                        return {'CANCELLED'}

                    redcloth_mat_path = _find_redcloth_material_for_collision_apb(
                        context,
                        full_path_norm,
                        loadmods=loadmods,
                    )
                    if not redcloth_mat_path:
                        self.report(
                            {'ERROR'},
                            f"Could not find matching .redcloth asset for collision APB: {full_path_norm}"
                        )
                        return {'CANCELLED'}

                    cloth_obj = importCloth(
                        context,
                        apx_path,
                        True,
                        False,
                        True,
                        redcloth_mat_path,
                    )
                    if cloth_obj is None:
                        apx_status = get_apx_addon_status(context)
                        if not apx_status["enabled"] and not _legacy_apx_addon_enabled():
                            set_external_import_dependency_alert(
                                "redcloth",
                                source_path=abs_file_path,
                                status="apx_addon_disabled",
                                reason="io_mesh_apx addon is not enabled.",
                            )
                        self.report({'ERROR'}, f"Redcloth import failed for {os.path.basename(abs_file_path)}")
                        return {'CANCELLED'}
                    clear_external_import_dependency_alert("redcloth")
                elif ext == ".bin":
                    log.info("BIN (terrain) import not yet implemented: %s", abs_file_path)
                else:
                    log.warning("Unknown collision format %s: %s", ext, abs_file_path)
            elif ext in {".redcloth", ".redapex"}:
                from ..cloth_util import importCloth
                from .ui_mesh import import_redapex_resource

                apx_path = _ensure_redcloth_apx_for_asset_import(
                    context,
                    abs_file_path,
                    full_path_norm,
                    loadmods=loadmods,
                )
                if not apx_path or not win_path_exists(apx_path):
                    apx_status = get_apx_addon_status(context)
                    if not apx_status["enabled"]:
                        set_external_import_dependency_alert(
                            "redcloth",
                            source_path=abs_file_path,
                            status="apx_addon_disabled",
                            reason="io_mesh_apx addon is not enabled.",
                        )
                    elif not apx_status["sdk_ready"]:
                        set_external_import_dependency_alert(
                            "redcloth",
                            source_path=abs_file_path,
                            status="apx_sdk_missing",
                            reason="APX SDK CLI path is not configured or does not exist.",
                        )
                    self.report(
                        {'ERROR'},
                        f"No matching .apx found/generated for this {ext}. "
                        "Enable io_mesh_apx and set its apex_sdk_cli to convert collision .apb files."
                    )
                    return {'CANCELLED'}

                imported_obj = None
                if ext == ".redapex":
                    imported_obj = import_redapex_resource(
                        context,
                        abs_file_path,
                        use_mat=True,
                        rotate_180=False,
                        import_chunks=False,
                        import_floor=False,
                        collections_as_empties=True,
                    )
                else:
                    imported_obj = importCloth(
                        context,
                        apx_path,
                        True,
                        False,
                        True,
                        abs_file_path,
                    )
                if imported_obj is None:
                    apx_status = get_apx_addon_status(context)
                    if not apx_status["enabled"] and not _legacy_apx_addon_enabled():
                        set_external_import_dependency_alert(
                            "redcloth",
                            source_path=abs_file_path,
                            status="apx_addon_disabled",
                            reason="io_mesh_apx addon is not enabled.",
                        )
                    self.report({'ERROR'}, f"{ext} import failed for {os.path.basename(abs_file_path)}")
                    return {'CANCELLED'}
                clear_external_import_dependency_alert("redcloth")
            elif ext == ".srt":
                srt_status = get_srt_addon_status()
                if not srt_status["enabled"]:
                    set_external_import_dependency_alert(
                        "speedtree",
                        source_path=abs_file_path,
                        status="srt_addon_disabled",
                        reason="io_mesh_srt addon is not enabled.",
                    )
                    self.report(
                        {'ERROR'},
                        "io_mesh_srt is required to import .srt from the Asset Browser."
                    )
                    return {'CANCELLED'}
                prefs = get_all_addon_prefs(context)
                use_custom_grouping = bool(getattr(prefs, "ab_srt_custom_grouping", True))
                lod0_only = bool(getattr(prefs, "ab_srt_lod0_only", True))

                srt_snapshot = _snapshot_srt_import_state(context) if use_custom_grouping else {}
                tex_stats = _export_srt_textures_for_import(
                    context,
                    abs_file_path,
                    full_path_norm,
                    loadmods=loadmods,
                )
                srt_import_path = tex_stats.get("import_path") or abs_file_path
                if lod0_only:
                    srt_import_path = _prepare_srt_lod0_json(srt_import_path)
                result = getattr(bpy.ops, "import").srt_json(filepath=srt_import_path)
                if 'FINISHED' not in result:
                    self.report({'ERROR'}, f"SRT import failed: {os.path.basename(abs_file_path)}")
                    return {'CANCELLED'}
                clear_external_import_dependency_alert("speedtree")
                if use_custom_grouping:
                    _flatten_srt_import_collections(context, srt_import_path, srt_snapshot)
                if tex_stats.get("missing"):
                    log.warning(
                        "SRT import missing %d referenced textures in TextureCache for %s: %s",
                        len(tex_stats["missing"]),
                        os.path.basename(abs_file_path),
                        ", ".join(tex_stats["missing"][:8]) + ("..." if len(tex_stats["missing"]) > 8 else ""),
                    )
            elif ext in {".dds", ".png", ".jpg", ".jpeg", ".tga", ".bmp"}:
                if ext == ".dds":
                    img, load_error = load_image_with_dds_repair(
                        abs_file_path,
                        check_existing=True,
                        allow_dds_repair=overwrite_existing,
                    )
                else:
                    load_error = None
                    img = None
                    try:
                        img = bpy_image_load_safe(abs_file_path, check_existing=True)
                    except Exception as exc:
                        load_error = exc
                if img is None:
                    if load_error is not None:
                        self.report({'ERROR'}, f"Texture import failed: {load_error}")
                    else:
                        self.report({'ERROR'}, f"Texture import failed: {os.path.basename(abs_file_path)}")
                    return {'CANCELLED'}
            elif ext == ".w2mesh":
                mesh_import_settings = MeshImportSettings.from_addon_prefs(get_all_addon_prefs(context))
                import_mesh.import_mesh(
                    abs_file_path,
                    do_merge_normals=False,
                    **mesh_import_settings.to_import_mesh_kwargs(),
                )
            elif ext == ".w2cube":
                result = bpy.ops.witcher.import_w2cube('EXEC_DEFAULT', filepath=abs_file_path)
                if 'FINISHED' not in result:
                    self.report({'ERROR'}, f"Cubemap import failed: {os.path.basename(abs_file_path)}")
                    return {'CANCELLED'}
            elif ext == ".w2ent":
                metadata = import_entity.get_entity_appearance_metadata(abs_file_path)
                w2ent_mode = import_entity.classify_entity_import_metadata(metadata, context=context)
                if w2ent_mode == "character":
                    default_appearance_name = str(metadata.get("default_name", "") or "").strip()
                    import_entity.import_direct_entity_file(
                        abs_file_path,
                        False,
                        0 if default_appearance_name else 1,
                        None,
                        selected_appearance_name=default_appearance_name,
                    )
                elif not import_entity.try_apply_inventory_file_to_selected_character(context, abs_file_path):
                    import_entity.import_direct_entity_file(abs_file_path, False, 0, None)
            elif ext == ".flyr":
                from ..CR2W import CR2W_reader
                from ..importers import import_w2l
                foliage = CR2W_reader.load_foliage(abs_file_path)
                import_w2l.btn_import_w2ent(foliage)
            elif ext == ".w2l":
                from ..CR2W import CR2W_reader
                from ..importers import import_w2l
                from . import ui_map
                level_file = CR2W_reader.load_w2l(abs_file_path)
                import_w2l.btn_import_W2L(level_file, context, False, keep_proxy_meshes=True)
                ui_map.apply_layer_visibility_settings(context)
            elif ext == ".w2w":
                from ..CR2W import CR2W_reader
                from ..importers import import_w2w
                world_file = CR2W_reader.load_w2w(abs_file_path)
                import_w2w.btn_import_w2w(world_file, abs_file_path)
            elif ext == ".w2scene":
                from ..importers import import_scene
                scene_importer = import_scene.import_w3_scene(abs_file_path)
                if hasattr(scene_importer, "load_sections"):
                    scene_importer.load_sections()
                if hasattr(context.scene, "witcher_sections"):
                    context.scene.witcher_sections.clear()
                    context.scene.witcher_sections_filepath = abs_file_path
                    for section in getattr(scene_importer, "scene_sections", []):
                        item = context.scene.witcher_sections.add()
                        item.name = getattr(section, "sectionName", str(section))
                        item.json_data = "{}"
                bpy.context.view_layer.update()
            elif ext == ".w2cutscene":
                from ..importers import import_cutscene
                import_cutscene.import_w3_cutscene(abs_file_path)
            elif ext == ".w2anims" or abs_file_path.lower().endswith(".w2anims.json"):
                from ..importers import import_anims
                import_anims.start_import(context, abs_file_path)
            elif (ext in {".w2rig", ".w3dyng"}
                  or abs_file_path.lower().endswith((".w2rig.json", ".w3dyng.json"))):
                from ..importers import import_rig
                rig_name = os.path.splitext(os.path.basename(abs_file_path))[0]
                if rig_name.endswith('.w2rig') or rig_name.endswith('.w3dyng'):
                    rig_name = os.path.splitext(rig_name)[0]
                import_rig.start_rig_import(abs_file_path, rig_name, None, context=context)
            elif ext == ".w3fac":
                from ..importers import import_rig
                face_data = import_rig.loadFaceFile(abs_file_path)
                import_rig.create_armature(face_data.mimicSkeleton, "yes", context=context)
            else:
                self.report({'ERROR'}, f"Import not implemented for {ext} files from {cache_type} cache")
                log.info("Import not implemented for %s files from %s cache", ext, cache_type)
                return {'CANCELLED'}
        finally:
            if override_roots:
                clear_repo_override_roots()

        # Track in recent imports
        add_recent_import(context, full_path, cache_type)

        filename = os.path.basename(full_path)
        self.report({'INFO'}, f"Successfully imported: {filename}")
        return {'FINISHED'}


class SoundPreviewToggleOperator(Operator):
    """Play or stop a sound preview for Sound cache items."""
    bl_idname = "witcher.sound_preview_toggle"
    bl_label = "Toggle Sound Preview"

    file_path: StringProperty()
    cache_type: StringProperty(default="")

    def execute(self, context):
        witcher_file_browser = context.scene.witcher_file_browser
        cache_type = self.cache_type or witcher_file_browser.active_cache_type
        effective_cache_type = get_effective_cache_type(cache_type)
        if effective_cache_type != "Sound":
            self.report({'WARNING'}, "Sound preview is only available for Sound cache items.")
            return {'CANCELLED'}

        full_path = self.file_path
        if "\\" not in full_path and witcher_file_browser.current_folder:
            full_path = witcher_file_browser.current_folder + "\\" + self.file_path
        full_path = full_path.replace("/", "\\")

        if _sound_preview_matches(cache_type, full_path):
            _clear_sound_preview(context)
            self.report({'INFO'}, f"Stopped preview: {os.path.basename(full_path)}")
            return {'FINISHED'}

        try:
            wav_path = play_sound_preview(
                context,
                cache_type,
                full_path,
                loadmods=witcher_file_browser.loadmods,
            )
        except Exception as exc:
            self.report({'ERROR'}, f"Sound preview failed: {exc}")
            return {'CANCELLED'}

        self.report({'INFO'}, f"Playing preview: {os.path.basename(wav_path)}")
        return {'FINISHED'}


class GlobalImportOperator(Operator):
    """Import file from global search result"""
    bl_idname = "witcher.file_action_global_import"
    bl_label = "Global Import"

    file_path: StringProperty()
    cache_type: StringProperty()

    def execute(self, context):
        if not cache_supports_scene_import(self.cache_type):
            self.report({'WARNING'}, "Sound cache items are export-only. Use Export to extract them to disk.")
            return {'CANCELLED'}
        # Set the cache type temporarily for import
        witcher_file_browser = context.scene.witcher_file_browser
        original_cache_type = witcher_file_browser.active_cache_type
        witcher_file_browser.active_cache_type = self.cache_type
        invoke_mode = 'INVOKE_DEFAULT' if getattr(witcher_file_browser, "open_import_dialog", False) else 'EXEC_DEFAULT'

        try:
            return bpy.ops.witcher.file_action_import_to_scene(invoke_mode, file_path=self.file_path)
        finally:
            witcher_file_browser.active_cache_type = original_cache_type


class GlobalExportOperator(Operator):
    """Export file from global search result"""
    bl_idname = "witcher.file_action_global_export"
    bl_label = "Global Export"

    file_path: StringProperty()
    cache_type: StringProperty()

    def execute(self, context):
        witcher_file_browser = context.scene.witcher_file_browser
        original_cache_type = witcher_file_browser.active_cache_type
        witcher_file_browser.active_cache_type = self.cache_type

        try:
            return bpy.ops.witcher.file_action(file_path=self.file_path)
        finally:
            witcher_file_browser.active_cache_type = original_cache_type


class OpenExternalArchiveFolderOperator(Operator):
    """Open the current standalone archive export folder."""
    bl_idname = "witcher.open_external_archive_folder"
    bl_label = "Open Standalone Export Folder"

    cache_type: StringProperty(default="")

    def execute(self, context):
        cache_type = self.cache_type or getattr(context.scene.witcher_file_browser, "active_cache_type", "")
        output_root = _get_external_archive_output_root(context, cache_type, create=True)
        if not output_root:
            self.report({'ERROR'}, "No standalone archive export folder is set")
            return {'CANCELLED'}
        try:
            bpy.ops.wm.path_open(filepath=output_root)
        except Exception as exc:
            self.report({'ERROR'}, f"Could not open folder: {exc}")
            return {'CANCELLED'}
        return {'FINISHED'}


class CancelExternalArchiveExportOperator(Operator):
    bl_idname = "witcher.cancel_external_archive_export"
    bl_label = "Cancel Standalone Export"

    def execute(self, context):
        if not _EXTERNAL_BROWSER_EXPORT_JOB.get("running"):
            return {'CANCELLED'}
        _EXTERNAL_BROWSER_EXPORT_JOB["cancel_requested"] = True
        _touch_external_export_ui(context)
        self.report({'INFO'}, "Standalone export cancellation requested")
        return {'FINISHED'}


class DismissExternalArchiveExportStatusOperator(Operator):
    bl_idname = "witcher.dismiss_external_archive_export_status"
    bl_label = "Dismiss Standalone Export Status"

    def execute(self, context):
        _set_external_export_status(visible=False, state="idle")
        _touch_external_export_ui(context)
        return {'FINISHED'}


class ExternalArchiveExportAllOperator(Operator):
    """Bulk-extract the current standalone archive scope to the chosen export folder."""
    bl_idname = "witcher.external_archive_export_all"
    bl_label = "Export Standalone Archive Scope"

    cache_type: StringProperty(default="")
    folder_path: StringProperty(default="")

    def execute(self, context):
        browser = getattr(context.scene, "witcher_file_browser", None)
        if browser is None:
            self.report({'ERROR'}, "Asset browser state is unavailable")
            return {'CANCELLED'}

        if _EXTERNAL_BROWSER_EXPORT_JOB.get("running"):
            self.report({'WARNING'}, "A standalone archive export is already running")
            return {'CANCELLED'}

        cache_type = self.cache_type or browser.active_cache_type
        if not is_external_cache(cache_type):
            self.report({'ERROR'}, "Standalone archive export is only available for external archive sessions")
            return {'CANCELLED'}

        session = get_external_archive_session(cache_type)
        if not session:
            self.report({'ERROR'}, "No standalone archive is loaded")
            return {'CANCELLED'}

        output_root = _get_external_archive_output_root(context, cache_type, create=True)
        if not output_root:
            self.report({'ERROR'}, "Choose an export folder before starting the standalone export")
            return {'CANCELLED'}

        if cache_type == EXTERNAL_TEXTURE_CACHE_TYPE and browser.external_texture_export_format != 'DDS':
            from ..CR2W import texconv_wrapper

            if not texconv_wrapper.is_available():
                self.report({'ERROR'}, "PNG/TGA export requires texconv.dll in CR2W/third_party_libs")
                return {'CANCELLED'}

        scope_paths = get_export_scope_file_paths(context)
        if not scope_paths:
            self.report({'WARNING'}, "No files match the current folder/filter")
            return {'CANCELLED'}

        _set_external_export_status(visible=False, state="idle")
        job = _EXTERNAL_BROWSER_EXPORT_JOB
        job["running"] = True
        job["cache_type"] = cache_type
        job["folder_path"] = self.folder_path or browser.current_folder
        job["output_root"] = output_root
        job["texture_format"] = str(getattr(browser, "external_texture_export_format", "DDS") or "DDS")
        job["overwrite"] = bool(getattr(browser, "mods_overwrite", False))
        job["items"] = scope_paths
        job["index"] = 0
        job["total"] = len(scope_paths)
        job["done"] = 0
        job["skipped"] = 0
        job["errors"] = 0
        job["cancel_requested"] = False
        job["context"] = context

        wm = context.window_manager
        job["timer"] = wm.event_timer_add(0.01, window=context.window)
        wm.modal_handler_add(self)
        _touch_external_export_ui(context)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type == 'ESC':
            _EXTERNAL_BROWSER_EXPORT_JOB["cancel_requested"] = True
            return {'RUNNING_MODAL'}

        if event.type != 'TIMER':
            return {'PASS_THROUGH'}

        job = _EXTERNAL_BROWSER_EXPORT_JOB
        if not job.get("running"):
            return self._finish(context)
        if job.get("cancel_requested"):
            return self._finish(context, cancelled=True)

        if int(job["index"]) < int(job["total"]) and not job.get("cancel_requested"):
            item_path = job["items"][int(job["index"])]
            job["index"] += 1
            try:
                export_result = _export_external_archive_item(
                    context,
                    job["cache_type"],
                    item_path,
                    overwrite=bool(job.get("overwrite", False)),
                    output_root=job.get("output_root", ""),
                    texture_format=str(job.get("texture_format", "DDS") or "DDS"),
                )
                if export_result.get("skipped"):
                    job["skipped"] += 1
                else:
                    job["done"] += 1
            except Exception as exc:
                log.debug("Standalone archive export failed for %s: %s", item_path, exc, exc_info=True)
                job["errors"] += 1

        _touch_external_export_ui(context)

        if job.get("cancel_requested"):
            return self._finish(context, cancelled=True)
        if int(job["index"]) >= int(job["total"]):
            return self._finish(context)
        return {'RUNNING_MODAL'}

    def _finish(self, context, cancelled: bool = False):
        job = _EXTERNAL_BROWSER_EXPORT_JOB
        timer = job.get("timer")
        wm = getattr(context, "window_manager", None)
        if wm and timer:
            try:
                wm.event_timer_remove(timer)
            except Exception:
                pass

        cache_type = str(job.get("cache_type", "") or "")
        folder_path = str(job.get("folder_path", "") or "")
        output_root = str(job.get("output_root", "") or "")
        done = int(job.get("done", 0) or 0)
        skipped = int(job.get("skipped", 0) or 0)
        errors = int(job.get("errors", 0) or 0)
        processed = int(job.get("index", 0) or 0)
        total = int(job.get("total", 0) or 0)

        job["running"] = False
        job["cache_type"] = ""
        job["folder_path"] = ""
        job["output_root"] = ""
        job["texture_format"] = "DDS"
        job["overwrite"] = False
        job["items"] = []
        job["index"] = 0
        job["total"] = 0
        job["done"] = 0
        job["skipped"] = 0
        job["errors"] = 0
        job["cancel_requested"] = False
        job["timer"] = None
        job["context"] = None

        _tag_browser_redraw(context)

        scope_label = folder_path or "entire archive"
        if cancelled:
            message = (
                f"{cache_type} export cancelled at {processed:,}/{total:,}: {scope_label} -> {output_root} "
                f"({done:,} extracted, {skipped:,} skipped, {errors:,} errors)"
            )
            _set_external_export_status(
                visible=True,
                state="cancelled",
                cache_type=cache_type,
                folder_path=folder_path,
                output_root=output_root,
                processed=processed,
                total=total,
                done=done,
                skipped=skipped,
                errors=errors,
                message=message,
            )
            _touch_external_export_ui(context)
            self.report({'WARNING'}, message)
            return {'CANCELLED'}

        message = (
            f"{cache_type} export complete: {scope_label} -> {output_root} "
            f"({done:,} extracted, {skipped:,} skipped, {errors:,} errors)"
        )
        _set_external_export_status(
            visible=True,
            state="completed" if errors == 0 else "completed_with_errors",
            cache_type=cache_type,
            folder_path=folder_path,
            output_root=output_root,
            processed=processed,
            total=total,
            done=done,
            skipped=skipped,
            errors=errors,
            message=message,
        )
        _touch_external_export_ui(context)
        self.report({'INFO' if errors == 0 else 'WARNING'}, message)
        return {'FINISHED'}


class TexturePreviewOperator(Operator):
    """Preview an image from any cache type"""
    bl_idname = "witcher.texture_preview"
    bl_label = "Preview Image"

    file_path: StringProperty()
    cache_type: StringProperty(default="Texture")
    preview_scale: IntProperty(name="Zoom", default=16, min=6, max=32)

    # Class-level preview collection
    _preview_collection = None

    @classmethod
    def get_preview_collection(cls):
        if cls._preview_collection is None:
            cls._preview_collection = bpy.utils.previews.new()
        return cls._preview_collection

    def execute(self, context):
        return {'FINISHED'}

    def invoke(self, context, event):
        with mod_loading_context(context, overwrite=False):
            return self._invoke_inner(context, event)

    def _invoke_inner(self, context, event):
        witcher_file_browser = context.scene.witcher_file_browser
        temp_path = _resolve_preview_image_path(
            context,
            self.cache_type,
            self.file_path,
            loadmods=witcher_file_browser.loadmods,
        )
        if temp_path and win_path_exists(temp_path):
            context.scene.witcher_file_browser.preview_texture_path = temp_path
            pcoll = self.get_preview_collection()
            if self.file_path in pcoll:
                del pcoll[self.file_path]
            pcoll.load(self.file_path, win_safe_path(temp_path), 'IMAGE')
            return context.window_manager.invoke_popup(self, width=560)

        import tempfile
        from pathlib import Path

        temp_dir = tempfile.gettempdir()
        filename = os.path.basename(self.file_path)
        temp_path = None
        witcher_file_browser = context.scene.witcher_file_browser
        search_path = self.file_path.replace("/", "\\")

        if os.path.splitext(self.file_path)[1].lower() == '.w2cube':
            # Extract from bundle via repo_file, then build preview
            abs_path = None
            try:
                vanilla_path = get_vanilla_path(search_path, witcher_file_browser.loadmods)
                abs_path = repo_file(vanilla_path)
            except Exception:
                pass
            if not abs_path or not win_path_exists(abs_path):
                abs_path = get_uncook_abs_path(context, self.file_path, witcher_file_browser.loadmods)
            if not abs_path or not win_path_exists(abs_path):
                if is_disk_cache(self.cache_type):
                    abs_path = get_disk_abs_path(self.cache_type, self.file_path)
            if abs_path and win_path_exists(abs_path):
                temp_path = build_w2cube_preview(abs_path)
            if not temp_path:
                self.report({'WARNING'}, f"Could not generate cubemap preview for: {self.file_path}")
                return {'CANCELLED'}
        elif is_w2ter_buffer_file(self.file_path):
            temp_path = build_w2ter_buffer_preview(context, self.cache_type, self.file_path)
        elif is_disk_cache(self.cache_type):
            abs_path = get_disk_abs_path(self.cache_type, self.file_path)
            if abs_path and win_path_exists(abs_path):
                ext = os.path.splitext(abs_path)[1].lower()
                if ext in {'.dds', '.png', '.jpg', '.jpeg', '.tga', '.bmp'}:
                    temp_path = abs_path
                else:
                    self.report({'WARNING'}, f"Cannot preview {ext} files from disk")
                    return {'CANCELLED'}
            else:
                self.report({'WARNING'}, f"Texture not found: {self.file_path}")
                return {'CANCELLED'}
        # Try TextureCache first (produces proper DDS), then fall back to bundle/disk extraction
        elif get_effective_cache_type(self.cache_type) == "Texture":
            # Direct texture cache lookup
            items = _get_texture_cache_items(
                search_path,
                cache_type=self.cache_type,
                loadmods=witcher_file_browser.loadmods,
            )
            if items:
                final_item = items[-1] if isinstance(items, list) else items
                temp_path = os.path.join(temp_dir, "witcher_preview", filename)
                temp_path = str(Path(temp_path).with_suffix('.dds'))
                os.makedirs(os.path.dirname(temp_path), exist_ok=True)
                final_item.extract_to_file(temp_path)

            # Fallback: XBM not in TextureCache — try bundle/disk (e.g. proxy textures)
            if not temp_path or not win_path_exists(temp_path):
                try:
                    abs_path = repo_file(search_path)
                    if abs_path and win_path_exists(abs_path):
                        ext = os.path.splitext(abs_path)[1].lower()
                        if ext in {'.dds', '.png', '.jpg', '.jpeg', '.tga', '.bmp'}:
                            temp_path = abs_path
                        elif ext == '.xbm':
                            dds_path = os.path.splitext(abs_path)[0] + '.dds'
                            if not win_path_exists(dds_path):
                                convert_xbm_to_dds(abs_path)
                            if win_path_exists(dds_path):
                                temp_path = dds_path
                except Exception:
                    pass
        else:
            # For non-Texture caches, try TextureCache first (it produces viewable DDS)
            try:
                manager = LoadTextureManager(loadmods=witcher_file_browser.loadmods)
                items = manager.find_item_by_path_name(search_path)
                if items:
                    final_item = items[-1] if isinstance(items, list) else items
                    temp_path = os.path.join(temp_dir, "witcher_preview", filename)
                    temp_path = str(Path(temp_path).with_suffix('.dds'))
                    os.makedirs(os.path.dirname(temp_path), exist_ok=True)
                    final_item.extract_to_file(temp_path)
            except Exception:
                pass

            # If TextureCache didn't have it, extract from bundle
            if not temp_path or not win_path_exists(temp_path):
                try:
                    mod_name = ""
                    if witcher_file_browser.loadmods and "\\" in search_path:
                        mod_name = search_path.split("\\", 1)[0]
                    abs_path = repo_file(strip_mod_prefix(search_path, mod_name))
                    if abs_path and win_path_exists(abs_path):
                        ext = os.path.splitext(abs_path)[1].lower()
                        if ext in {'.dds', '.png', '.jpg', '.jpeg', '.tga', '.bmp'}:
                            temp_path = abs_path
                        elif ext == '.xbm':
                            dds_path = os.path.splitext(abs_path)[0] + '.dds'
                            if not win_path_exists(dds_path):
                                convert_xbm_to_dds(abs_path)
                            if win_path_exists(dds_path):
                                temp_path = dds_path
                        else:
                            self.report({'WARNING'}, f"Cannot preview {ext} files directly from bundle")
                            return {'CANCELLED'}
                except Exception as e:
                    self.report({'WARNING'}, f"Failed to extract texture: {e}")
                    return {'CANCELLED'}

        if not temp_path or not win_path_exists(temp_path):
            self.report({'WARNING'}, f"Texture not found: {self.file_path}")
            return {'CANCELLED'}

        # Store path for draw method
        context.scene.witcher_file_browser.preview_texture_path = temp_path

        # Load into preview collection
        pcoll = self.get_preview_collection()
        # Clear old preview if exists with same key
        if self.file_path in pcoll:
            del pcoll[self.file_path]
        # Load the new preview
        pcoll.load(self.file_path, win_safe_path(temp_path), 'IMAGE')

        return context.window_manager.invoke_popup(self, width=560)

    def draw(self, context):
        layout = self.layout
        preview_path = context.scene.witcher_file_browser.preview_texture_path

        if preview_path and win_path_exists(preview_path):
            pcoll = self.get_preview_collection()

            # Get the preview icon
            if self.file_path in pcoll:
                icon_id = pcoll[self.file_path].icon_id

                layout.prop(self, "preview_scale", slider=True)

                # Display the image preview using template_icon
                col = layout.column(align=True)
                col.template_icon(icon_value=icon_id, scale=float(self.preview_scale))

                # Show filename
                img_name = os.path.basename(preview_path)
                layout.label(text=img_name, icon='IMAGE_DATA')

                # Try to get size info from Blender's image data if loaded
                if img_name in bpy.data.images:
                    img = bpy.data.images[img_name]
                    layout.label(text=f"Size: {img.size[0]} x {img.size[1]}")
                else:
                    # Load image just to get size
                    try:
                        img = bpy_image_load_safe(preview_path)
                        layout.label(text=f"Size: {img.size[0]} x {img.size[1]}")
                    except Exception:
                        pass
            else:
                layout.label(text="Preview not available", icon='ERROR')
        else:
            layout.label(text="Preview not available", icon='ERROR')



class CombineTerrainTilesOperator(Operator):
    """Extract and combine all w2ter tiles in the current folder"""
    bl_idname = "witcher.combine_w2ter_tiles"
    bl_label = "Export + Combine Tiles"
    bl_description = "Export all .w2ter buffers in this folder and build combined maps"

    folder_path: StringProperty()
    cache_type: StringProperty()

    def execute(self, context):
        witcher_file_browser = context.scene.witcher_file_browser
        folder_path = self.folder_path or witcher_file_browser.current_folder
        cache_type = self.cache_type or witcher_file_browser.active_cache_type
        loadmods = witcher_file_browser.loadmods

        if not folder_path:
            self.report({'ERROR'}, "No folder selected")
            return {'CANCELLED'}

        with mod_loading_context(context):
            result = combine_w2ter_folder(context, cache_type, folder_path, loadmods)

        outputs = result.get("outputs", [])
        output_dir = result.get("output_dir", "")
        info = result.get("info", {})

        if not outputs:
            error = info.get("error", "No output files generated")
            self.report({'ERROR'}, error)
            return {'CANCELLED'}

        skipped = info.get("skipped", [])
        if skipped:
            log.warning("Skipped %d tiles with mismatched resolution", len(skipped))

        self.report({'INFO'}, f"Combined {len(outputs)} outputs -> {output_dir}")
        return {'FINISHED'}


class ImportTerrainFullMapOperator(Operator):
    """Import terrain as one combined map object with Geometry Nodes + Multires"""
    bl_idname = "witcher.import_terrain_fullmap"
    bl_label = "Import Terrain Full Map"
    bl_description = "Combine .w2ter buffers and import as one terrain object using Geometry Nodes + Multires"

    folder_path: StringProperty()
    cache_type: StringProperty()

    def execute(self, context):
        witcher_file_browser = context.scene.witcher_file_browser
        folder_path = self.folder_path or witcher_file_browser.current_folder
        cache_type = self.cache_type or witcher_file_browser.active_cache_type
        loadmods = witcher_file_browser.loadmods
        multires_level = witcher_file_browser.terrain_multires_level

        if not folder_path:
            self.report({'ERROR'}, "No folder selected")
            return {'CANCELLED'}

        with mod_loading_context(context):
            result = import_terrain_fullmap_from_folder(
                context, cache_type, folder_path, loadmods, multires_level
            )

        obj_name = result.get("object_name", "")
        if not obj_name:
            self.report({'ERROR'}, result.get("error", "Failed to import full terrain map"))
            return {'CANCELLED'}

        self.report({'INFO'}, f"Imported full terrain map: {obj_name}")
        return {'FINISHED'}


class ImportTerrainTilesOperator(Operator):
    """Import terrain tiles as individual Blender objects with heightmap and overlay"""
    bl_idname = "witcher.import_terrain_tiles"
    bl_label = "Import Terrain Tiles"
    bl_description = "Import each .w2ter tile as a separate mesh with heightmap displacement and overlay texture"

    folder_path: StringProperty()
    cache_type: StringProperty()

    def execute(self, context):
        witcher_file_browser = context.scene.witcher_file_browser
        folder_path = self.folder_path or witcher_file_browser.current_folder
        cache_type = self.cache_type or witcher_file_browser.active_cache_type
        loadmods = witcher_file_browser.loadmods
        multires_level = witcher_file_browser.terrain_multires_level

        if not folder_path:
            self.report({'ERROR'}, "No folder selected")
            return {'CANCELLED'}

        with mod_loading_context(context):
            result = import_terrain_tiles_from_folder(
                context, cache_type, folder_path, loadmods, multires_level
            )

        tile_count = result.get("tile_count", 0)
        if tile_count == 0:
            error = result.get("error", "No tiles imported")
            self.report({'ERROR'}, error)
            return {'CANCELLED'}

        hub_name = result.get("hub_name", "")
        self.report({'INFO'}, f"Imported {tile_count} terrain tiles ({hub_name})")
        return {'FINISHED'}


class AdjustTileMultiresOperator(Operator):
    """Adjust multires level on selected terrain tile objects"""
    bl_idname = "witcher.adjust_tile_multires"
    bl_label = "Adjust Tile Multires"
    bl_options = {'REGISTER', 'UNDO'}

    target_level: IntProperty(
        name="Target Level",
        description="Target multires subdivision level",
        default=5, min=0, max=10,
    )

    def execute(self, context):
        from ..importers.import_w2w import rebuild_tile_mesh
        count = 0
        for obj in context.selected_objects:
            if "terrain_multires" not in obj:
                continue
            current = obj["terrain_multires"]
            if current == self.target_level:
                count += 1
                continue
            if rebuild_tile_mesh(obj, self.target_level):
                count += 1

        if count == 0:
            self.report({'WARNING'}, "No terrain tiles selected (missing tile properties?)")
            return {'CANCELLED'}

        self.report({'INFO'}, f"Adjusted {count} tiles to multires level {self.target_level}")
        return {'FINISHED'}


class FileActionOperator(Operator):
    """Perform an action on a file (Extract/Export)"""
    bl_idname = "witcher.file_action"
    bl_label = "File Action"
    file_path: StringProperty()

    def execute(self, context):
        witcher_file_browser = context.scene.witcher_file_browser
        cache_type = witcher_file_browser.active_cache_type
        effective_cache_type = get_effective_cache_type(cache_type)

        if is_disk_cache(cache_type):
            self.report({'INFO'}, "Export not available for disk sources")
            return {'CANCELLED'}

        # Build full path
        full_path = (witcher_file_browser.current_folder + "\\" + self.file_path
                     if witcher_file_browser.current_folder else self.file_path)
        log.debug("Action on file [%s]: %s", cache_type, full_path)
        if is_external_cache(cache_type):
            output_root = _get_external_archive_output_root(context, cache_type, create=True)
            if not output_root:
                self.report({'ERROR'}, "Choose an export folder before extracting from a standalone archive")
                return {'CANCELLED'}
            try:
                export_result = _export_external_archive_item(
                    context,
                    cache_type,
                    full_path,
                    overwrite=bool(getattr(witcher_file_browser, "mods_overwrite", False)),
                    output_root=output_root,
                )
            except Exception as exc:
                self.report({'ERROR'}, f"Export failed: {exc}")
                return {'CANCELLED'}

            export_path = export_result.get("primary_path", "")
            if not export_path or not win_path_exists(export_path):
                self.report({'ERROR'}, f"Export failed: {full_path}")
                return {'CANCELLED'}

            filename = os.path.basename(export_path)
            self.report({'INFO'}, f"Exported: {filename} -> {export_path}")
            return {'FINISHED'}
        # Get appropriate manager and find item
        loadmods = witcher_file_browser.loadmods
        full_path_norm = full_path.replace("/", "\\")
        mod_name = ""
        if loadmods and "\\" in full_path_norm:
            mod_name = full_path_norm.split("\\", 1)[0]
        items = None
        item_lists = None
        try:
            if cache_type == EXTERNAL_BUNDLE_CACHE_TYPE:
                session = get_external_archive_session(cache_type)
                if not session:
                    log.warning("No external bundle session loaded")
                    return {'CANCELLED'}
                if full_path_norm.lower().endswith(".w2ter"):
                    pattern = re.compile(re.escape(full_path_norm) + r"(\.\d+\.buffer)?$", re.IGNORECASE)
                    item_lists = [
                        value for key, value in session["items"].items()
                        if isinstance(key, str) and pattern.match(key.replace("/", "\\"))
                    ]
                if not item_lists:
                    items = session["items"].get(full_path_norm)
                    if items:
                        item_lists = [items]
            elif cache_type == EXTERNAL_TEXTURE_CACHE_TYPE:
                items = _get_external_archive_items(cache_type, full_path_norm)
            elif cache_type == EXTERNAL_SOUND_CACHE_TYPE:
                items = _get_external_archive_items(cache_type, full_path_norm)
            elif cache_type == "Bundle":
                manager = LoadBundleManager(loadmods=loadmods)
                if full_path_norm.lower().endswith(".w2ter"):
                    pattern = re.compile(re.escape(full_path_norm) + r"(\.\d+\.buffer)?$", re.IGNORECASE)
                    item_lists = [
                        value for key, value in manager.Items.items()
                        if isinstance(key, str) and pattern.match(key.replace("/", "\\"))
                    ]
                if not item_lists:
                    items = manager.find_item_by_hash(full_path_norm)
                    if items:
                        item_lists = [items]
            elif cache_type == EXTERNAL_COLLISION_CACHE_TYPE:
                session = get_external_archive_session(cache_type)
                items = session["items"].get(full_path_norm) if session else None
            elif cache_type == "Collision":
                manager = LoadCollisionManager(loadmods=loadmods)
                items = manager.find_item_by_path_name(full_path_norm)
            elif cache_type == "Texture":
                manager = LoadTextureManager(loadmods=loadmods)
                items = manager.find_item_by_path_name(full_path_norm)
            elif cache_type == "Sound":
                manager = LoadSoundManager(loadmods=loadmods)
                items = manager.find_item_by_path_name(full_path_norm)
            elif cache_type == "Speech":
                manager = LoadSpeechManager()
                items = manager.find_item_by_hash(full_path_norm)
        except Exception as e:
            log.error("Failed to load manager: %s", e)
            return {'CANCELLED'}

        if effective_cache_type == "Bundle" and not item_lists:
            log.warning("Item not found: %s", full_path)
            return {'CANCELLED'}
        if effective_cache_type != "Bundle" and not items:
            log.warning("Item not found: %s", full_path)
            return {'CANCELLED'}

        export_path = ""
        exported_bundle_paths = []
        if effective_cache_type == "Bundle":
            primary_export_path = ""
            for item_list in item_lists:
                final_item = item_list[-1] if isinstance(item_list, list) else item_list
                item_name = getattr(final_item, 'name', None) or getattr(final_item, 'Name', full_path)
                vanilla_name = strip_mod_prefix(item_name, mod_name)
                if vanilla_name == item_name and mod_name:
                    vanilla_name = strip_mod_prefix(full_path_norm, mod_name)
                export_path = repo_file(vanilla_name)
                if not win_path_exists(export_path):
                    written_path = final_item.extract_to_file(export_path)
                    if written_path:
                        export_path = written_path
                    log.debug("Extracted to: %s", export_path)
                if win_path_exists(export_path):
                    exported_bundle_paths.append(export_path)
                if not primary_export_path and not vanilla_name.lower().endswith(".buffer"):
                    primary_export_path = export_path
            if primary_export_path:
                export_path = primary_export_path
        else:
            # Get the final item (last in list)
            final_item = items[-1] if isinstance(items, list) else items

            # Get export path and extract - use unprefixed item name for disk path
            if effective_cache_type == "Collision":
                vanilla_name = get_collision_output_rel_path(full_path_norm, loadmods=loadmods)
            else:
                item_name = getattr(final_item, 'name', None) or getattr(final_item, 'Name', full_path)
                # Strip mod prefix only when it matches the active mod folder.
                vanilla_name = strip_mod_prefix(item_name, mod_name)
                if vanilla_name == item_name and mod_name:
                    # Fallback: strip from full_path when the item name is missing or prefixed differently
                    vanilla_name = strip_mod_prefix(full_path_norm, mod_name)
            export_path = repo_file(vanilla_name)

            if not win_path_exists(export_path):
                written_path = final_item.extract_to_file(export_path)
                if written_path:
                    export_path = written_path
                # Texture caches output DDS regardless of requested extension.
                dds_path = os.path.splitext(export_path)[0] + ".dds"
                if win_path_exists(dds_path):
                    export_path = dds_path
                log.debug("Extracted to: %s", export_path)

        # If we exported an XBM from a bundle, prefer TextureCache DDS when available.
        if export_path.lower().endswith(".xbm") and win_path_exists(export_path):
            try:
                convert_xbm_to_dds(export_path)
                dds_path = os.path.splitext(export_path)[0] + ".dds"
                if win_path_exists(dds_path):
                    export_path = dds_path
            except Exception as e:
                log.warning("Failed to convert xbm_to_dds: %s", e)

        # Auto-import for certain file types
        if export_path.endswith(".w2ent"):
            if not import_entity.try_apply_inventory_file_to_selected_character(context, export_path):
                import_entity.import_direct_entity_file(export_path, False, 0, None)

        # For terrain tiles, emit per-tile images next to extracted buffers.
        if effective_cache_type == "Bundle" and full_path_norm.lower().endswith(".w2ter"):
            buffer_paths = [p for p in exported_bundle_paths if terrain_w2ter.is_w2ter_buffer_name(p)]
            if buffer_paths:
                terrain_w2ter.export_tile_images(buffer_paths)

        filename = os.path.basename(export_path)
        self.report({'INFO'}, f"Exported: {filename} -> {export_path}")
        return {'FINISHED'}


class WITCHER_PT_AssetBrowser(Panel):
    """Asset Browser launcher in the N-Panel sidebar"""
    bl_label = "Asset Browser"
    bl_idname = "WITCHER_PT_asset_browser"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Witcher'
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return False  # Content embedded in WITCH_PT_Main

    def draw_header(self, context):
        self.layout.label(text="", icon='FILE_FOLDER')

    def draw(self, context):
        layout = self.layout
        layout.use_property_decorate = False

        # Box 1: Browse Assets
        open_box = layout.box()
        open_box.label(text="Browse Assets", icon='FILE_FOLDER')
        open_col = open_box.column(align=True)
        open_col.scale_y = 1.4
        open_col.operator("witcher.simple_file_browser", text="Assets Vanilla", icon="FILE_FOLDER").loadmods = False
        open_col.operator("witcher.simple_file_browser", text="Assets Mods", icon="FILE_FOLDER").loadmods = True

        browser_settings = getattr(context.scene, "witcher_file_browser", None)
        if browser_settings:
            mod_row = open_box.row(align=True)
            mod_row.scale_y = 0.9
            mod_row.label(text="Mod options", icon='MODIFIER')
            mod_row.prop(browser_settings, "use_mods_priority", text="Load Mods")
            mod_row.prop(browser_settings, "mods_overwrite", text="Overwrite")

        # Box 2: Characters (merged Quick Imports + Browsers)
        char_box = layout.box()
        char_box.label(text="Characters", icon='OUTLINER_OB_ARMATURE')
        char_col = char_box.column(align=True)
        quick_row = char_col.row(align=True)
        quick_row.scale_y = 1.4
        quick_row.operator("witcher.import_geralt", text="Geralt", icon='USER')
        quick_row.operator("witcher.import_ciri", text="Ciri", icon='USER')
        char_col.separator()
        ref_row = char_col.row(align=True)
        ref_row.operator("witcher.image_browser", text="Bestiary", icon='BOOKMARKS')
        ref_row = char_col.row(align=True)
        ref_row.operator("witcher.character_image_browser", text="Characters", icon='OUTLINER_OB_ARMATURE')


def register():
    bpy.utils.register_class(FileItem)
    bpy.utils.register_class(RecentItem)
    bpy.utils.register_class(BookmarkItem)
    bpy.utils.register_class(MySettings)
    bpy.utils.register_class(OpenExternalCollisionCacheOperator)
    bpy.utils.register_class(OpenExternalTextureCacheOperator)
    bpy.utils.register_class(OpenExternalSoundCacheOperator)
    bpy.utils.register_class(OpenExternalBundleOperator)
    bpy.utils.register_class(SimpleFileBrowser)
    bpy.utils.register_class(ClearSearchOperator)
    bpy.utils.register_class(StatusIconHelpOperator)
    bpy.utils.register_class(FileItemStatsOperator)
    bpy.utils.register_class(GoHomeOperator)
    bpy.utils.register_class(ClearExtensionFilterOperator)
    bpy.utils.register_class(CopyPathOperator)
    bpy.utils.register_class(CopyAllSearchPathsOperator)
    bpy.utils.register_class(OpenFileLocationOperator)
    bpy.utils.register_class(AddBookmarkOperator)
    bpy.utils.register_class(RemoveBookmarkOperator)
    bpy.utils.register_class(GotoBookmarkOperator)
    bpy.utils.register_class(ClearRecentImportsOperator)
    bpy.utils.register_class(ReopenExternalArchiveOperator)
    bpy.utils.register_class(ImportRecentOperator)
    bpy.utils.register_class(ToggleBatchSelectOperator)
    bpy.utils.register_class(SelectAllBatchVisibleOperator)
    bpy.utils.register_class(ClearBatchSelectOperator)
    bpy.utils.register_class(ImportBatchSelectedOperator)
    bpy.utils.register_class(BrowserPageOperator)
    bpy.utils.register_class(NavigateToPathOperator)
    bpy.utils.register_class(NavigateBackOperator)
    bpy.utils.register_class(NavigateForwardOperator)
    bpy.utils.register_class(GotoSearchResultOperator)
    bpy.utils.register_class(GotoGlobalSearchResultOperator)
    bpy.utils.register_class(NavigateFolderOperator)
    bpy.utils.register_class(SelectCacheTypeOperator)
    bpy.utils.register_class(CombineTerrainTilesOperator)
    bpy.utils.register_class(ImportTerrainFullMapOperator)
    bpy.utils.register_class(ImportTerrainTilesOperator)
    bpy.utils.register_class(AdjustTileMultiresOperator)
    bpy.utils.register_class(FileActionOperator)
    bpy.utils.register_class(FileActionOperatorImportToScene)
    bpy.utils.register_class(SoundPreviewToggleOperator)
    bpy.utils.register_class(GlobalImportOperator)
    bpy.utils.register_class(GlobalExportOperator)
    bpy.utils.register_class(OpenExternalArchiveFolderOperator)
    bpy.utils.register_class(CancelExternalArchiveExportOperator)
    bpy.utils.register_class(DismissExternalArchiveExportStatusOperator)
    bpy.utils.register_class(ExternalArchiveExportAllOperator)
    bpy.utils.register_class(TexturePreviewOperator)
    bpy.utils.register_class(WITCHER_PT_AssetBrowser)

    bpy.types.Scene.witcher_file_browser = PointerProperty(type=MySettings)
    bpy.types.Scene.witcher_file_items = CollectionProperty(type=FileItem)


def unregister():
    _clear_sound_preview()
    _clear_browser_asset_previews()
    if hasattr(bpy.types.Scene, "witcher_file_browser"):
        delattr(bpy.types.Scene, "witcher_file_browser")
    if hasattr(bpy.types.Scene, "witcher_file_items"):
        delattr(bpy.types.Scene, "witcher_file_items")

    bpy.utils.unregister_class(WITCHER_PT_AssetBrowser)
    bpy.utils.unregister_class(TexturePreviewOperator)
    bpy.utils.unregister_class(ExternalArchiveExportAllOperator)
    bpy.utils.unregister_class(DismissExternalArchiveExportStatusOperator)
    bpy.utils.unregister_class(CancelExternalArchiveExportOperator)
    bpy.utils.unregister_class(OpenExternalArchiveFolderOperator)
    bpy.utils.unregister_class(GlobalExportOperator)
    bpy.utils.unregister_class(GlobalImportOperator)
    bpy.utils.unregister_class(SoundPreviewToggleOperator)
    bpy.utils.unregister_class(OpenExternalBundleOperator)
    bpy.utils.unregister_class(OpenExternalSoundCacheOperator)
    bpy.utils.unregister_class(OpenExternalTextureCacheOperator)
    bpy.utils.unregister_class(OpenExternalCollisionCacheOperator)
    bpy.utils.unregister_class(SimpleFileBrowser)
    bpy.utils.unregister_class(GotoGlobalSearchResultOperator)
    bpy.utils.unregister_class(GotoSearchResultOperator)
    bpy.utils.unregister_class(FileItemStatsOperator)
    bpy.utils.unregister_class(NavigateForwardOperator)
    bpy.utils.unregister_class(NavigateBackOperator)
    bpy.utils.unregister_class(NavigateToPathOperator)
    bpy.utils.unregister_class(ReopenExternalArchiveOperator)
    bpy.utils.unregister_class(ImportRecentOperator)
    bpy.utils.unregister_class(ImportBatchSelectedOperator)
    bpy.utils.unregister_class(BrowserPageOperator)
    bpy.utils.unregister_class(ClearBatchSelectOperator)
    bpy.utils.unregister_class(SelectAllBatchVisibleOperator)
    bpy.utils.unregister_class(ToggleBatchSelectOperator)
    bpy.utils.unregister_class(ClearRecentImportsOperator)
    bpy.utils.unregister_class(GotoBookmarkOperator)
    bpy.utils.unregister_class(RemoveBookmarkOperator)
    bpy.utils.unregister_class(AddBookmarkOperator)
    bpy.utils.unregister_class(OpenFileLocationOperator)
    bpy.utils.unregister_class(CopyAllSearchPathsOperator)
    bpy.utils.unregister_class(CopyPathOperator)
    bpy.utils.unregister_class(ClearExtensionFilterOperator)
    bpy.utils.unregister_class(GoHomeOperator)
    bpy.utils.unregister_class(StatusIconHelpOperator)
    bpy.utils.unregister_class(ClearSearchOperator)
    bpy.utils.unregister_class(NavigateFolderOperator)
    bpy.utils.unregister_class(SelectCacheTypeOperator)
    bpy.utils.unregister_class(FileActionOperatorImportToScene)
    bpy.utils.unregister_class(FileActionOperator)
    bpy.utils.unregister_class(AdjustTileMultiresOperator)
    bpy.utils.unregister_class(ImportTerrainTilesOperator)
    bpy.utils.unregister_class(ImportTerrainFullMapOperator)
    bpy.utils.unregister_class(CombineTerrainTilesOperator)
    bpy.utils.unregister_class(BookmarkItem)
    bpy.utils.unregister_class(RecentItem)
    bpy.utils.unregister_class(FileItem)
    bpy.utils.unregister_class(MySettings)
