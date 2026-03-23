import sys as _sys
import os
import re
import shutil
from pathlib import Path

from .extension_paths import (
    get_audio_root,
    get_cache_root,
    get_temp_root,
    get_texture_root,
    get_uncook_root,
)

LEGACY_ADDON_NAME = "io_import_w2l"
ADDON_NAME = __package__ or __name__

# Extension builds run under the bl_ext namespace; avoid registering top-level aliases there.
def _is_extension_context() -> bool:
    name = __package__ or __name__ or ""
    return name.startswith("bl_ext.")

# Allow the addon folder to be renamed while keeping legacy import paths working.
if __name__ != LEGACY_ADDON_NAME and not _is_extension_context():
    _sys.modules.setdefault(LEGACY_ADDON_NAME, _sys.modules[__name__])

def get_addon_name() -> str:
    return ADDON_NAME

def _load_dev_pref_overrides():
    """Load dev-only addon preference defaults from the excluded dev module."""
    try:
        from .dev import dev_config
    except Exception:
        return {}, []

    if not getattr(dev_config, "DEV_MODE_ENABLED", False):
        return {}, []

    defaults = getattr(dev_config, "ADDON_PREFS_DEFAULTS", {})
    if not isinstance(defaults, dict):
        defaults = {}

    redkit_projects = getattr(dev_config, "ADDON_PREFS_REDKIT_PROJECTS", [])
    if not isinstance(redkit_projects, list):
        redkit_projects = []

    return defaults, redkit_projects

def _apply_dev_pref_overrides(prefs):
    """Apply dev-only defaults without overwriting existing user preferences."""
    defaults, redkit_projects = _load_dev_pref_overrides()

    for key, value in defaults.items():
        if not value:
            continue
        if not hasattr(prefs, key):
            continue
        current = getattr(prefs, key, "")
        # Skip booleans: their False default is falsy, so the "only set if
        # empty" check below would re-apply the override on every startup.
        if isinstance(value, bool):
            continue
        if current:
            continue
        setattr(prefs, key, value)

    if redkit_projects and hasattr(prefs, "redkit_projects") and len(prefs.redkit_projects) == 0:
        for path in redkit_projects:
            if not path:
                continue
            item = prefs.redkit_projects.add()
            item.path = path

from .setup_logging_bl import *
from . import setup_logging_bl
from .read_game_bin import (
    update_witcher_game_path,
    auto_detect_witcher3_game_path,
    auto_detect_witcher2_game_path,
    get_witcher3_exe_path,
    get_witcher2_exe_path,
    is_valid_witcher3_game_path,
    is_valid_witcher2_game_path,
    WITCHER3_EXE_REL,
    WITCHER2_EXE_REL,
)
log = logging.getLogger(__name__)

_EXTERNAL_IMPORT_DEPENDENCY_ALERT = {}


def _tag_all_areas_redraw():
    """Best-effort UI redraw so runtime alerts appear immediately."""
    try:
        import bpy as _bpy
        wm = getattr(_bpy.context, "window_manager", None)
        if not wm:
            return
        for window in getattr(wm, "windows", []):
            screen = getattr(window, "screen", None)
            if not screen:
                continue
            for area in getattr(screen, "areas", []):
                try:
                    area.tag_redraw()
                except Exception:
                    pass
    except Exception:
        pass


def set_external_import_dependency_alert(kind, *, source_path="", status="", reason=""):
    """Show a top-of-panel warning after an import failed due to missing external addon dependencies."""
    global _EXTERNAL_IMPORT_DEPENDENCY_ALERT

    kind_norm = (kind or "").strip().lower() or "external"
    source_path = (source_path or "").strip()
    reason = (reason or "").strip()
    status = (status or "").strip()
    source_name = os.path.basename(source_path) if source_path else ""

    _EXTERNAL_IMPORT_DEPENDENCY_ALERT = {
        "active": True,
        "kind": kind_norm,
        "source_path": source_path,
        "source_name": source_name,
        "status": status,
        "reason": reason,
    }
    _tag_all_areas_redraw()


def get_external_import_dependency_alert():
    if not _EXTERNAL_IMPORT_DEPENDENCY_ALERT.get("active"):
        return {}
    return dict(_EXTERNAL_IMPORT_DEPENDENCY_ALERT)


def clear_external_import_dependency_alert(kind=""):
    global _EXTERNAL_IMPORT_DEPENDENCY_ALERT
    if not _EXTERNAL_IMPORT_DEPENDENCY_ALERT.get("active"):
        return False
    kind_norm = (kind or "").strip().lower()
    if kind_norm and _EXTERNAL_IMPORT_DEPENDENCY_ALERT.get("kind") != kind_norm:
        return False
    _EXTERNAL_IMPORT_DEPENDENCY_ALERT = {}
    _tag_all_areas_redraw()
    return True


def _update_verbose_logging(prefs, context):
    """Toggle all module log levels between verbose (INFO) and quiet (CRITICAL)."""
    if prefs.verbose_logging:
        setup_logging_bl.enable_all_debug()
        log.info("Verbose logging enabled")
    else:
        setup_logging_bl.apply_log_levels()  # Reset to configured defaults
        log.info("Verbose logging disabled")


def is_verbose_logging() -> bool:
    """Check if verbose logging is enabled in addon preferences.
    Safe to call even when no context is available."""
    try:
        import bpy
        prefs = bpy.context.preferences.addons[ADDON_NAME].preferences
        return prefs.verbose_logging
    except Exception:
        return False

def get_game_path(context) -> str:
    addon_prefs = context.preferences.addons[ADDON_NAME].preferences
    witcher_game_path = addon_prefs.witcher_game_path
    return witcher_game_path

def get_witcher2_game_path(context) -> str:
    addon_prefs = context.preferences.addons[ADDON_NAME].preferences
    return addon_prefs.witcher2_game_path

def get_uncook_path(context) -> str:
    addon_prefs = context.preferences.addons[ADDON_NAME].preferences
    uncook_path = addon_prefs.uncook_path
    return uncook_path

def get_mod_directory(context) -> str:
    addon_prefs = context.preferences.addons[ADDON_NAME].preferences
    mod_directory = addon_prefs.mod_directory
    return mod_directory

def get_wolvenkit(context) -> str:
    addon_prefs = context.preferences.addons[ADDON_NAME].preferences
    wolvenkit = addon_prefs.wolvenkit
    return wolvenkit

def get_fbx_uncook_path(context) -> str:
    addon_prefs = context.preferences.addons[ADDON_NAME].preferences
    fbx_uncook_path = addon_prefs.fbx_uncook_path
    return fbx_uncook_path

def get_texture_path(context) -> str:
    addon_prefs = context.preferences.addons[ADDON_NAME].preferences
    use_separate = bool(getattr(addon_prefs, "use_separate_texture_uncook_path", False))
    if use_separate:
        tex_uncook_path = str(getattr(addon_prefs, "tex_uncook_path", "") or "").strip()
        if tex_uncook_path:
            return tex_uncook_path
    return addon_prefs.uncook_path

def get_w2_unbundle_path(context) -> str:
    addon_prefs = context.preferences.addons[ADDON_NAME].preferences
    w2_unbundle_path = addon_prefs.w2_unbundle_path
    return w2_unbundle_path

def get_modded_texture_path(context) -> str:
    addon_prefs = context.preferences.addons[ADDON_NAME].preferences
    tex_mod_uncook_path = addon_prefs.tex_mod_uncook_path
    return tex_mod_uncook_path

def get_tex_ext(context) -> str:
    addon_prefs = context.preferences.addons[ADDON_NAME].preferences
    tex_ext = addon_prefs.tex_ext
    return tex_ext

def get_W3_VOICE_PATH(context) -> str:
    addon_prefs = context.preferences.addons[ADDON_NAME].preferences
    W3_VOICE_PATH = addon_prefs.W3_VOICE_PATH
    return W3_VOICE_PATH

def get_W3_OGG_PATH(context) -> str:
    addon_prefs = context.preferences.addons[ADDON_NAME].preferences
    # Compatibility alias: audio conversions now use the same folder as lipsync extraction.
    return addon_prefs.W3_VOICE_PATH

def get_vgmstream_path(context) -> str:
    #addon_prefs = context.preferences.addons[ADDON_NAME].preferences
    #vgmstream_path = addon_prefs.vgmstream_path
    script_dir = os.path.dirname(os.path.abspath(__file__))
    exe_name = r"CR2W\third_party_libs\vgmstream-win64\vgmstream-cli.exe"
    exe_path = os.path.join(script_dir, exe_name)
    vgmstream_path = exe_path
    return vgmstream_path

def get_all_addon_prefs(context):
    return context.preferences.addons[ADDON_NAME].preferences

def get_do_import_redcloth(context) -> bool:
    addon_prefs = context.preferences.addons[ADDON_NAME].preferences
    return bool(getattr(addon_prefs, "do_import_redcloth", True))

def get_DO_WEAR_CLOTH(context) -> bool:
    addon_prefs = context.preferences.addons[ADDON_NAME].preferences
    return bool(getattr(addon_prefs, "DO_WEAR_CLOTH", True))

def get_redcloth_simulation_enabled(context) -> bool:
    addon_prefs = context.preferences.addons[ADDON_NAME].preferences
    return bool(getattr(addon_prefs, "redcloth_simulation_enabled", True))

def get_redcloth_wind_velocity(context) -> float:
    addon_prefs = context.preferences.addons[ADDON_NAME].preferences
    try:
        return float(getattr(addon_prefs, "redcloth_wind_velocity", 0.0))
    except Exception:
        return 0.0

def get_W3_FOLIAGE_PATH(context) -> str:
    addon_prefs = context.preferences.addons[ADDON_NAME].preferences
    W3_FOLIAGE_PATH = addon_prefs.W3_FOLIAGE_PATH or addon_prefs.uncook_path
    return W3_FOLIAGE_PATH

def get_W3_REDCLOTH_PATH(context) -> str:
    addon_prefs = context.preferences.addons[ADDON_NAME].preferences
    W3_REDCLOTH_PATH = addon_prefs.W3_REDCLOTH_PATH or addon_prefs.uncook_path
    return W3_REDCLOTH_PATH

def get_W3_REDFUR_PATH(context) -> str:
    addon_prefs = context.preferences.addons[ADDON_NAME].preferences
    W3_REDFUR_PATH = addon_prefs.W3_REDFUR_PATH or addon_prefs.uncook_path
    return W3_REDFUR_PATH

def get_use_fbx_repo(context) -> str:
    addon_prefs = context.preferences.addons[ADDON_NAME].preferences
    use_fbx_repo = addon_prefs.use_fbx_repo
    return use_fbx_repo

def get_do_fix_tail(context) -> bool:
    addon_prefs = context.preferences.addons[ADDON_NAME].preferences
    do_fix_tail = addon_prefs.do_fix_tail
    return do_fix_tail

def get_rig_rot90_enabled(rig_settings, default=False):
    """Return whether the rig currently has rot90 applied."""
    if rig_settings is None:
        return bool(default)
    return bool(getattr(rig_settings, "rot90_imported", default))

def set_rig_rot90_enabled(rig_settings, enabled: bool):
    """Set rot90 state on rig settings."""
    if rig_settings is None:
        return
    rig_settings.rot90_imported = bool(enabled)
    rig_settings.rot90_compensate = bool(enabled)


from . import CR2W
from .CR2W.w3_types import CSkeletalAnimationSetEntry
from .CR2W.dc_anims import load_lipsync_file
#from io_import_w2l.importers import *
from .importers import (
                                    import_anims,
                                    import_rig,
                                    import_w2l,
                                    import_mesh,
                                    import_w2w,
                                    import_texarray
                                    )
from .exporters import (
                                    export_anims
                                    )
from . import constrain_util
from . import file_helpers
#from io_import_w2l.cloth_util import setup_w3_material_CR2W


#ui
from .ui import ui_custom_icons
from .ui import ui_map
from .ui.ui_map import (WITCH_OT_w2L,
                                     WITCH_OT_w2w,
                                     WITCH_OT_load_layer,
                                     WITCH_OT_load_layer_group,
                                     WITCH_OT_radish_w2L,
                                     WITCH_OT_export_textures)
from .ui import ui_anims
from .ui import ui_speech
from .ui import ui_entity
from .ui import ui_morphs
from .ui import ui_material
from .ui.ui_morphs import (WITCH_OT_morphs)

from .ui import ui_voice
from .ui import ui_mimics
from .ui import ui_re_anims
from .ui import ui_anims_list
from .ui import ui_texture_export
from .ui import ui_import_menu
from .ui import ui_scene
from .ui import armature_context
from .ui import ui_cache_export
from .ui.ui_mesh import (WITCH_OT_w2mesh, WITCH_OT_apx, WITCH_OT_w2mesh_export, WITCH_OT_nxs,
                         WITCH_OT_export_goto_project_path,
                         WITCH_OT_create_sound_info, WITCH_OT_remove_sound_info,
                         WITCH_OT_toggle_rot90, WITCH_OT_merge_armature_hierarchy,
                         PHYSICAL_MATERIAL_ENUM_ITEMS, DEFAULT_PHYSICAL_MATERIAL, PHYSICAL_MATERIAL_NAMES)
from .ui.ui_utils import WITCH_PT_Base
from .ui.ui_entity import WITCH_OT_ENTITY_lod_toggle
#from io_import_w2l.ui.ui_entity import WITCH_OT_w2ent_chara
from .ui.ui_entity import WITCH_OT_w2ent
from .ui.ui_material import WITCH_OT_w2mg, WITCH_OT_w2mi, WITCH_OT_xbm, WITCH_OT_w2cube

from .ui.ui_anims import WITCH_OT_ImportW2Rig, WITCH_OT_ExportW2AnimJson, WITCH_OT_ExportW2RigJson

from . import w3_material_nodes
from . import w3_material_blender
from . import w3_material_nodes_custom
from . import w3_asset_browser

# New unified panel system
from .ui import panels as unified_panels
from .ui import lists as unified_lists

import bpy
from bpy.types import (Panel, Operator)
from bpy.props import StringProperty, BoolProperty, CollectionProperty, IntProperty, EnumProperty
from mathutils import Vector
from bpy_extras.io_utils import ImportHelper, ExportHelper
import addon_utils

bl_info = {
    "name": "Witcher 3 Tools",
    "author": "Dingdio",
    "version": (1, 0, 1),
    "blender": (4, 5, 0),
    "location": "File > Import-Export > Witcher 3 Assets",
    "description": "Tools for Witcher 3 and Witcher 2",
    "warning": "",
    "doc_url": "https://github.com/dingdio/Witcher3_Blender_Tools",
    "category": "Import-Export"
}

import tempfile

def create_semi_persistent_temp_dir(base_name="blender_temp_"):
    temp_root = get_temp_root(create=True)
    temp_dir_path = os.path.join(temp_root, "witcher_tools_" + base_name)
    if not os.path.exists(temp_dir_path):
        os.makedirs(temp_dir_path)

    return temp_dir_path


def _default_uncook_path():
    return get_uncook_root(create=True)


def _default_texture_path(*, create: bool = False):
    return get_texture_root(create=create)


def _default_w3_audio_path():
    return get_audio_root(create=True)


def _normalize_pref_path(path_value: str) -> str:
    raw = str(path_value or "").strip()
    if not raw:
        return ""
    try:
        raw = bpy.path.abspath(raw)
    except Exception:
        pass
    return os.path.normcase(os.path.normpath(raw))


def _paths_match(path_a: str, path_b: str) -> bool:
    norm_a = _normalize_pref_path(path_a)
    norm_b = _normalize_pref_path(path_b)
    return bool(norm_a and norm_b and norm_a == norm_b)


def _update_use_separate_texture_uncook_path(prefs, context):
    if bool(getattr(prefs, "use_separate_texture_uncook_path", False)):
        tex_path = str(getattr(prefs, "tex_uncook_path", "") or "").strip()
        if not tex_path:
            prefs.tex_uncook_path = _default_texture_path(create=True)


def _auto_initialize_game_and_audio_paths(prefs, context):
    legacy_uncook_default = create_semi_persistent_temp_dir("uncook")
    legacy_audio_default = create_semi_persistent_temp_dir("audio")

    # Migrate untouched legacy temp defaults to extension-root folders.
    if _paths_match(getattr(prefs, "uncook_path", ""), legacy_uncook_default):
        prefs.uncook_path = _default_uncook_path()
    if _paths_match(getattr(prefs, "W3_VOICE_PATH", ""), legacy_audio_default):
        prefs.W3_VOICE_PATH = _default_w3_audio_path()

    if not getattr(prefs, "uncook_path", ""):
        prefs.uncook_path = _default_uncook_path()
    if not getattr(prefs, "W3_VOICE_PATH", ""):
        prefs.W3_VOICE_PATH = _default_w3_audio_path()
    if bool(getattr(prefs, "use_separate_texture_uncook_path", False)):
        tex_path = str(getattr(prefs, "tex_uncook_path", "") or "").strip()
        if not tex_path:
            prefs.tex_uncook_path = _default_texture_path(create=True)

    current_game_path = (getattr(prefs, "witcher_game_path", "") or "").strip()
    current_game_path_abs = bpy.path.abspath(current_game_path) if current_game_path else ""
    if not current_game_path and not is_valid_witcher3_game_path(current_game_path_abs):
        detected_game_path = auto_detect_witcher3_game_path()
        if detected_game_path and detected_game_path != current_game_path:
            prefs.witcher_game_path = detected_game_path

    current_w2_path = (getattr(prefs, "witcher2_game_path", "") or "").strip()
    current_w2_path_abs = bpy.path.abspath(current_w2_path) if current_w2_path else ""
    if not current_w2_path and not is_valid_witcher2_game_path(current_w2_path_abs):
        detected_w2_path = auto_detect_witcher2_game_path()
        if detected_w2_path and detected_w2_path != current_w2_path:
            prefs.witcher2_game_path = detected_w2_path

    # Always refresh version info / cache-layer config for current value.
    update_witcher_game_path(prefs, context)


def get_witcher3_game_path_issue(context) -> str:
    try:
        addon_prefs = get_all_addon_prefs(context)
    except Exception:
        return ""

    raw_game_path = (getattr(addon_prefs, "witcher_game_path", "") or "").strip()
    if not raw_game_path:
        return f"Set Witcher 3 install folder ({WITCHER3_EXE_REL}) in addon preferences."
    game_path = bpy.path.abspath(raw_game_path)
    if is_valid_witcher3_game_path(game_path):
        return ""
    exe_path = get_witcher3_exe_path(game_path)
    return f"Invalid Witcher 3 path. Missing: {exe_path}"


def get_witcher2_game_path_issue(context) -> str:
    try:
        addon_prefs = get_all_addon_prefs(context)
    except Exception:
        return ""

    raw_game_path = (getattr(addon_prefs, "witcher2_game_path", "") or "").strip()
    if not raw_game_path:
        return f"Set Witcher 2 install folder ({WITCHER2_EXE_REL}) in addon preferences."
    game_path = bpy.path.abspath(raw_game_path)
    if is_valid_witcher2_game_path(game_path):
        return ""
    exe_path = get_witcher2_exe_path(game_path)
    return f"Invalid Witcher 2 path. Missing: {exe_path}"


def ensure_witcher3_game_path_initialized(context) -> bool:
    prefs = get_all_addon_prefs(context)
    _auto_initialize_game_and_audio_paths(prefs, context)
    return not bool(get_witcher3_game_path_issue(context))

class PathItem(bpy.types.PropertyGroup):
    path: StringProperty(
        name="Path",
        subtype='DIR_PATH',
        description="A directory path"
    )
class AddPathOperator(bpy.types.Operator):
    bl_idname = "witcher.add_path"
    bl_label = "Add Path"

    def execute(self, context):
        addon_prefs = get_all_addon_prefs(context)
        new_item = addon_prefs.path_list.add()
        new_item.path = ""  # Starts with an empty path; user can edit it
        return {'FINISHED'}
    
class RemovePathOperator(bpy.types.Operator):
    bl_idname = "witcher.remove_path"
    bl_label = "Remove Path"

    def execute(self, context):
        addon_prefs = get_all_addon_prefs(context)
        index = addon_prefs.active_path_index
        if 0 <= index < len(addon_prefs.path_list):
            addon_prefs.path_list.remove(index)
            # Adjust index if it exceeds the new length
            if index >= len(addon_prefs.path_list):
                addon_prefs.active_path_index = len(addon_prefs.path_list) - 1
        return {'FINISHED'}

class AddRedkitProjectOperator(bpy.types.Operator):
    bl_idname = "witcher.add_redkit_project"
    bl_label = "Add REDkit Project"

    def execute(self, context):
        addon_prefs = get_all_addon_prefs(context)
        new_item = addon_prefs.redkit_projects.add()
        new_item.path = ""  # Starts empty; user can edit
        return {'FINISHED'}


class RemoveRedkitProjectOperator(bpy.types.Operator):
    bl_idname = "witcher.remove_redkit_project"
    bl_label = "Remove REDkit Project"

    def execute(self, context):
        addon_prefs = get_all_addon_prefs(context)
        index = addon_prefs.redkit_projects_index
        if 0 <= index < len(addon_prefs.redkit_projects):
            addon_prefs.redkit_projects.remove(index)
            if index >= len(addon_prefs.redkit_projects):
                addon_prefs.redkit_projects_index = len(addon_prefs.redkit_projects) - 1
        return {'FINISHED'}


class WITCHER_OT_reset_browser_popup_width(bpy.types.Operator):
    """Reset the asset browser popup width to the default (30% of window)"""
    bl_idname = "witcher.reset_browser_popup_width"
    bl_label = "Reset to Default"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        get_all_addon_prefs(context).browser_popup_width = 0
        return {'FINISHED'}


class WITCHER_OT_autofind_w3_path(bpy.types.Operator):
    bl_idname = "witcher.autofind_w3_path"
    bl_label = "Auto Find Witcher 3 Path"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        addon_prefs = get_all_addon_prefs(context)
        detected_path = auto_detect_witcher3_game_path()
        if not detected_path:
            update_witcher_game_path(addon_prefs, context)
            self.report({'WARNING'}, "Could not auto-find Witcher 3 install path.")
            return {'CANCELLED'}

        addon_prefs.witcher_game_path = detected_path
        update_witcher_game_path(addon_prefs, context)
        self.report({'INFO'}, f"Witcher 3 path set: {detected_path}")
        return {'FINISHED'}


class WITCHER_OT_autofind_w2_path(bpy.types.Operator):
    bl_idname = "witcher.autofind_w2_path"
    bl_label = "Auto Find Witcher 2 Path"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        addon_prefs = get_all_addon_prefs(context)
        detected_path = auto_detect_witcher2_game_path()
        if not detected_path:
            self.report({'WARNING'}, "Could not auto-find Witcher 2 install path.")
            return {'CANCELLED'}

        addon_prefs.witcher2_game_path = detected_path
        self.report({'INFO'}, f"Witcher 2 path set: {detected_path}")
        return {'FINISHED'}


class WITCHER_OT_open_pref_path(bpy.types.Operator):
    bl_idname = "witcher.open_pref_path"
    bl_label = "Open Path in Explorer"
    bl_description = "Open this path in Explorer/Finder (files open their containing folder)"
    bl_options = {'INTERNAL'}

    path: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
    is_file: BoolProperty(default=False, options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        raw_path = (self.path or "").strip()
        if not raw_path:
            self.report({'WARNING'}, "Path is empty")
            return {'CANCELLED'}

        target_path = bpy.path.abspath(raw_path)
        target_path = os.path.normpath(target_path)

        # For file fields, open the containing folder instead of the file itself.
        if self.is_file or (os.path.exists(target_path) and os.path.isfile(target_path)):
            parent = os.path.dirname(target_path)
            if parent:
                target_path = parent

        # If the target does not exist yet, walk up to the nearest existing parent.
        probe_path = target_path
        while probe_path and not os.path.exists(probe_path):
            parent = os.path.dirname(probe_path)
            if not parent or parent == probe_path:
                break
            probe_path = parent

        if not probe_path or not os.path.exists(probe_path):
            self.report({'WARNING'}, f"Path does not exist: {target_path}")
            return {'CANCELLED'}

        try:
            bpy.ops.wm.path_open(filepath=probe_path)
        except Exception as exc:
            self.report({'ERROR'}, f"Failed to open path: {exc}")
            return {'CANCELLED'}

        if probe_path != target_path:
            self.report({'INFO'}, f"Opened nearest existing folder: {probe_path}")
        return {'FINISHED'}


class WITCHER_OT_pref_help_popup(bpy.types.Operator):
    bl_idname = "witcher.pref_help_popup"
    bl_label = "Preference Help"
    bl_options = {'INTERNAL'}

    topic: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
    path: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
    is_file: BoolProperty(default=False, options={'HIDDEN', 'SKIP_SAVE'})
    title_text: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def _get_help_content(self):
        topic = (self.topic or "").strip().lower()
        title_text = (self.title_text or "").strip()

        if topic == "uncook_path":
            return {
                "title": "Uncook Path",
                "icon": 'INFO',
                "lines": [
                    "This folder is auto-created by the add-on.",
                    "Its location is saved in Blender's add-on preferences.",
                    "You can keep the default path, or set your own folder.",
                    "It is used as a working/export folder for bundle extraction/export tasks.",
                ],
                "warnings": [
                    "Export workflows may create, move, or overwrite files in this folder.",
                    "Use a fresh folder or a folder you do not care about (recommended).",
                ],
            }

        if topic == "speech_path":
            return {
                "title": "Speech Audio Path",
                "icon": 'SPEAKER',
                "lines": [
                    "This is the combined working folder for speech/lipsync extraction",
                    "and audio conversion.",
                    "It can contain extracted lipsync data plus converted audio files",
                    "such as .ogg and .wav.",
                    "The add-on auto-creates a default path, but you can set a custom folder.",
                ],
                "warnings": [],
            }

        if topic == "external_addons":
            return {
                "title": "External Addons",
                "icon": 'PLUGIN',
                "lines": [
                    "These optional Blender add-ons enable extra import formats.",
                    "io_mesh_apx is needed for Redcloth/APX imports.",
                    "io_mesh_srt is needed for SpeedTree .srt imports.",
                    "If they are missing or disabled, those imports will be unavailable.",
                ],
                "warnings": [],
            }

        kind_label = "file" if self.is_file else "folder"
        return {
            "title": title_text or "Path Help",
            "icon": 'INFO',
            "lines": [
                f"This setting stores a {kind_label} path used by the add-on.",
                "You can set it manually or use Blender's path picker button.",
            ],
            "warnings": [],
        }

    @classmethod
    def description(cls, context, props):
        topic = (getattr(props, "topic", "") or "").strip().lower()
        if topic == "external_addons":
            return "External add-ons required for Redcloth (.apx) and SpeedTree (.srt) imports."
        if topic == "uncook_path":
            return "What the Uncook Path is used for and why a separate working folder is recommended."
        if topic == "speech_path":
            return "What the Speech Audio Path stores for lipsync and audio conversion workflows."
        return "Show help for this setting."

    def invoke(self, context, event):
        return context.window_manager.invoke_popup(self, width=560)

    def draw(self, context):
        layout = self.layout
        content = self._get_help_content()

        header = layout.row()
        header.label(text=content["title"], icon=content["icon"])

        if (self.path or "").strip():
            action_row = layout.row()
            action_row.scale_y = 1.1
            open_op = action_row.operator("witcher.open_pref_path", text="Open in Explorer", icon='FILE_FOLDER')
            open_op.path = self.path
            open_op.is_file = self.is_file

        body = layout.box().column(align=True)
        for line in content["lines"]:
            body.label(text=line)

        if content["warnings"]:
            warn_box = layout.box()
            warn_header = warn_box.row()
            warn_header.alert = True
            warn_header.label(text="Warning", icon='ERROR')

            warn_col = warn_box.column(align=True)
            for line in content["warnings"]:
                warn_row = warn_col.row()
                warn_row.alert = True
                warn_row.label(text=line)

    def execute(self, context):
        return {'FINISHED'}


class WITCHER_UL_path_list(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        layout.prop(item, "path", text="", emboss=False)

class Witcher3AddonPrefs(bpy.types.AddonPreferences):
    # this must match the addon name, use '__package__'
    # when defining this in a submodule of a python package.
    bl_idname = __package__

    witcher_game_path: StringProperty(
        name="Witcher 3 Path",
        subtype='DIR_PATH',
        default="",
        description="Path where The Witcher 3 is installed.",
        update=update_witcher_game_path
    )
    version_info: StringProperty(
        name="Version Info",
        default="",
        options={'HIDDEN'}  # Don't show as a UI field
    )
    witcher2_game_path: StringProperty(
        name="Witcher 2 Path",
        subtype='DIR_PATH',
        default="",
        description="Path where The Witcher 2 is installed."
    )
    uncook_path: StringProperty(
        name="Uncook Path",
        subtype='DIR_PATH',
        default=_default_uncook_path(),
        description="Path where you uncooked the game files."
    )
    wolvenkit: StringProperty(
        name="Wolvenkit 7 CLI exe",
        subtype='FILE_PATH',
        default="",
        description="Wolvenkit .exe."
    )
    mod_directory: StringProperty(
        name="Wolvenkit Project Path",
        subtype='DIR_PATH',
        default="",
        description="Path of the current Wolvenkit mod. This can also be used as the root path of your textures."
    )

    redkit_depot_path: StringProperty(
        name="REDkit Depot Path (r4data)",
        subtype='DIR_PATH',
        default="",
        description="Main REDkit depot (read-only)."
    )
    prefer_redkit_equipment_xml: BoolProperty(
        name="Prefer REDkit For Equipment XML",
        default=False,
        description="When refreshing equipment categories, prefer REDkit r4data/gameplay/items over uncook and bundles."
    )

    redkit_uncooked_path: StringProperty(
        name="REDkit Uncooked Depot Path",
        subtype='DIR_PATH',
        default="",
        description="Generated REDkit uncooked depot (read-only)."
    )

    redkit_projects: CollectionProperty(type=PathItem)
    redkit_projects_index: IntProperty()

    # New properties for the path list
    path_list: CollectionProperty(type=PathItem)
    active_path_index: IntProperty()
    
    fbx_uncook_path: StringProperty(
        name="Uncook Path FBX (.fbx)",
        subtype='DIR_PATH',
        default="",
        description="Path where you exported the FBX files."
    )

    tex_uncook_path: StringProperty(
        name="Uncook Path TEXTURES (.tga,.dds)",
        subtype='DIR_PATH',
        default="",
        description="Optional separate path where you exported textures."
    )
    use_separate_texture_uncook_path: BoolProperty(
        name="Use Separate Texture Folder",
        default=False,
        description="If enabled, textures use their own export folder instead of the Uncook Path.",
        update=_update_use_separate_texture_uncook_path,
    )
    
    w2_unbundle_path: StringProperty(
        name="Witcher 2 Unbundle",
        subtype='DIR_PATH',
        default="",
        description="Extracted Witcher 2 dzip files"
    )

    tex_mod_uncook_path: StringProperty(
        name="(optional) Uncook Path modded TEXTURES (.tga,.dds)",
        subtype='DIR_PATH',
        default="",
        description="(optional) Path where you exported the tga files from a mod."
    )
    
    tex_ext_opts = [
        #("custom", "Custom", "Description for value 1"),
        (".tga", ".tga", ".tga"),
        (".dds", ".dds", ".dds"),
        (".png", ".png", ".png"),
    ]
    tex_ext: bpy.props.EnumProperty(
        name="Texture Type",
        description="Select prefered texture type",
        items=tex_ext_opts,
        default=".dds",
    )
    

    W3_FOLIAGE_PATH: StringProperty(
        name="Uncook Path FOLIAGE (.fbx)",
        subtype='DIR_PATH',
        default="",
        description="Path where you exported the fbx files."
    )

    W3_REDCLOTH_PATH: StringProperty(
        name="Uncook Path REDCLOTH (.apx)",
        subtype='DIR_PATH',
        default="",
        description="Path where you exported the apx files."
    )

    W3_REDFUR_PATH: StringProperty(
        name="Uncook Path REDFUR (.apx)",
        subtype='DIR_PATH',
        default="",
        description="Path where you exported the apx files."
    )

    W3_VOICE_PATH: StringProperty(
        name="Speech Audio Path (.cr2w/.wem/.ogg/.wav)",
        subtype='DIR_PATH',
        default=_default_w3_audio_path(),
        description="Combined path for extracted lipsync files and converted audio files.",
    )
    
    # vgmstream_path: StringProperty(
    #     name="vgmstream Path",
    #     subtype='FILE_PATH',
    #     description="Path to vgmstream-cli.exe",
    # )
    #keep_lod_meshes: bpy.props.BoolProperty(name="Keep lod meshes", default = False)
    use_fbx_repo: bpy.props.BoolProperty(name="Use FBX repo",
                                        default=False,
                                        description="Enable this to load from the fbx repo when importing meshes, maps etc.")
    do_fix_tail: bpy.props.BoolProperty(
        name="Rotate Bones 90 (Blender display fix)",
        default=True,
        description=(
            "Import default for rig orientation. Witcher uses game-space axes, while Blender edit-bones "
            "display more clearly with a -90 degree Z compensation. Enable for easier rig editing and "
            "attachments; disable to keep raw game orientation."
        )
    )

    # Asset Browser state persistence
    browser_last_cache_type: StringProperty(
        name="Last Browser Cache Type",
        default="",
        description="Remember last used cache type in asset browser"
    )
    browser_last_folder: StringProperty(
        name="Last Browser Folder",
        default="",
        description="Remember last folder in asset browser"
    )

    # Recent imports tracking (stored as JSON string for persistence)
    browser_recent_imports: StringProperty(
        name="Recent Imports",
        default="[]",
        description="JSON list of recently imported files"
    )

    # Bookmarks (stored as JSON string for persistence)
    browser_bookmarks: StringProperty(
        name="Bookmarks",
        default="[]",
        description="JSON list of bookmarked paths"
    )

    # Global helper behavior toggles for Asset Browser imports
    do_import_redcloth: bpy.props.BoolProperty(
        name="Import Redcloth",
        default=True,
        description="Global redcloth import toggle used by entity/appearance imports"
    )
    DO_WEAR_CLOTH: bpy.props.BoolProperty(
        name="Redcloth Setup for Character",
        default=True,
        description="Global redcloth setup mode that prepares the cloth rig for character attachment"
    )
    redcloth_simulation_enabled: bpy.props.BoolProperty(
        name="Redcloth Cloth Simulation Enabled",
        default=True,
        description="Enable the imported ClothSimulation modifier by default"
    )
    redcloth_wind_velocity: bpy.props.FloatProperty(
        name="Redcloth Wind Velocity",
        default=0.0,
        min=0.0,
        max=99.0,
        description="Default wind velocity applied to imported APX redcloth ClothSimulation modifiers (Socket_5)"
    )
    ab_srt_custom_grouping: bpy.props.BoolProperty(
        name="SRT: Group Imports",
        default=True,
        description="After io_mesh_srt import, collapse created collections and parent imported objects under one empty group"
    )
    ab_srt_lod0_only: bpy.props.BoolProperty(
        name="SRT: Import LOD0 Only",
        default=True,
        description="After import, keep only the main LOD0 mesh object and remove other SRT-imported objects"
    )

    verbose_logging: bpy.props.BoolProperty(
        name="Debug Logging",
        default=False,
        description="Set ALL module log levels to DEBUG. Shows detailed info in "
                    "Blender's System Console (Window > Toggle System Console). "
                    "Per-module control is available in internal dev tools when enabled. "
                    "May reduce performance",
        update=lambda self, ctx: _update_verbose_logging(self, ctx),
    )

    browser_popup_width: bpy.props.IntProperty(
        name="Asset Browser Width",
        description="Width of the asset browser popup in pixels. Set to 0 to use the default (30% of window width)",
        default=0,
        min=0,
        max=3000,
    )

    #importFacePoses
    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        def section(title, icon='NONE'):
            box = layout.box()
            header = box.row()
            header.label(text=title, icon=icon)
            col = box.column()
            col.use_property_split = True
            col.use_property_decorate = False
            return box, col

        def draw_path_prop(parent, prop_name, *, is_file=False, help_topic=""):
            row = parent.row(align=True)
            row.prop(self, prop_name)
            help_op = row.operator("witcher.pref_help_popup", text="", icon='QUESTION')
            help_op.topic = help_topic
            help_op.path = getattr(self, prop_name, "")
            help_op.is_file = is_file
            try:
                help_op.title_text = self.bl_rna.properties[prop_name].name
            except Exception:
                help_op.title_text = prop_name

        # Witcher 3 paths and data sources
        w3_box, w3_col = section("Witcher 3 Settings", 'FILE_FOLDER')
        row = w3_col.row(align=True)
        row.prop(self, "witcher_game_path")
        row.operator("witcher.autofind_w3_path", text="Auto Find", icon='VIEWZOOM')
        help_op = row.operator("witcher.pref_help_popup", text="", icon='QUESTION')
        help_op.topic = ""
        help_op.path = getattr(self, "witcher_game_path", "")
        help_op.is_file = False
        help_op.title_text = "Witcher 3 Path"

        if self.version_info:
            info_box = w3_box.box()
            info_box.label(text="Detected Game Version")
            for line in self.version_info.split("\n"):
                info_box.label(text=line)

        draw_path_prop(w3_col, "uncook_path", help_topic="uncook_path")
        w3_col.prop(self, "use_separate_texture_uncook_path")
        if self.use_separate_texture_uncook_path:
            draw_path_prop(w3_col, "tex_uncook_path")
        else:
            info_row = w3_col.row()
            info_row.label(text="Textures use Uncook Path by default.", icon='INFO')
        draw_path_prop(w3_col, "W3_VOICE_PATH", help_topic="speech_path")

        # Witcher 2 paths
        w2_box, w2_col = section("Witcher 2 Settings", 'FILE_FOLDER')
        draw_path_prop(w2_col, "w2_unbundle_path")
        row = w2_col.row(align=True)
        row.prop(self, "witcher2_game_path")
        row.operator("witcher.autofind_w2_path", text="Auto Find", icon='VIEWZOOM')
        help_op = row.operator("witcher.pref_help_popup", text="", icon='QUESTION')
        help_op.topic = ""
        help_op.path = getattr(self, "witcher2_game_path", "")
        help_op.is_file = False
        help_op.title_text = "Witcher 2 Path"
        w2_issue = get_witcher2_game_path_issue(context)
        if w2_issue:
            issue_row = w2_box.row()
            issue_row.alert = bool(self.witcher2_game_path)
            issue_row.label(text=w2_issue, icon='ERROR' if self.witcher2_game_path else 'INFO')

        # Shared/global settings
        common_box, common_col = section("Common Settings", 'TOOL_SETTINGS')
        common_col.prop(self, "tex_ext")
        common_col.prop(self, "verbose_logging")
        width_row = common_col.row(align=True)
        width_row.prop(self, "browser_popup_width")
        width_row.operator("witcher.reset_browser_popup_width", text="", icon='LOOP_BACK')

        # External importer add-on status (APX / SRT)
        ext_addons_box, ext_addons_col = section("External Addons", 'PLUGIN')
        ext_info_row = ext_addons_col.row(align=True)
        ext_info_row.label(text="Used for Redcloth and SpeedTree imports")
        help_op = ext_info_row.operator("witcher.pref_help_popup", text="", icon='QUESTION')
        help_op.topic = "external_addons"
        help_op.path = ""
        help_op.is_file = False
        help_op.title_text = "External Addons"

        deps_box = ext_addons_box.box()
        deps_box.label(text="Status", icon='PLUGIN')

        apx_status = ui_cache_export.get_apx_addon_status(context)
        apx_row = deps_box.row(align=True)
        apx_icon = 'CHECKMARK' if apx_status["enabled"] else 'ERROR'
        apx_row.label(
            text=f"io_mesh_apx: {'enabled' if apx_status['enabled'] else 'not enabled'}",
            icon=apx_icon,
        )
        if not apx_status["exists"]:
            apx_row.operator("wm.url_open", text="GitHub", icon='URL').url = ui_cache_export.APX_ADDON_URL

        sdk_row = deps_box.row(align=True)
        if not apx_status["enabled"]:
            sdk_row.label(text="APX SDK CLI: enable io_mesh_apx to configure apex_sdk_cli", icon='INFO')
        elif apx_status["sdk_ready"]:
            sdk_row.label(text="APX SDK CLI: configured", icon='CHECKMARK')
        else:
            sdk_row.alert = True
            sdk_row.label(text="APX SDK CLI (apex_sdk_cli): missing/invalid, APB->APX conversion disabled", icon='ERROR')

        srt_status = ui_cache_export.get_srt_addon_status()
        srt_row = deps_box.row(align=True)
        srt_icon = 'CHECKMARK' if srt_status["enabled"] else 'ERROR'
        srt_row.label(
            text=f"io_mesh_srt: {'enabled' if srt_status['enabled'] else 'not enabled'}",
            icon=srt_icon,
        )
        if not srt_status["exists"]:
            srt_row.operator("wm.url_open", text="GitHub", icon='URL').url = ui_cache_export.SRT_ADDON_URL

        # Modding/work project paths
        mod_box, mod_col = section("Mod Paths", 'FILE_FOLDER')
        draw_path_prop(mod_col, "wolvenkit", is_file=True)
        draw_path_prop(mod_col, "mod_directory")
        draw_path_prop(mod_col, "tex_mod_uncook_path")

        # REDkit integration paths
        redkit_box, redkit_col = section("REDkit Paths", 'FILE_FOLDER')
        draw_path_prop(redkit_col, "redkit_depot_path")
        redkit_col.prop(self, "prefer_redkit_equipment_xml")
        draw_path_prop(redkit_col, "redkit_uncooked_path")

        projects_box = redkit_box.box()
        projects_box.label(text="REDkit Projects")
        row = projects_box.row(align=True)
        row.template_list("WITCHER_UL_path_list", "", self, "redkit_projects", self, "redkit_projects_index", rows=3)
        col = row.column(align=True)
        col.operator("witcher.add_redkit_project", text="", icon="ADD")
        col.operator("witcher.remove_redkit_project", text="", icon="REMOVE")

        # Extra/legacy options
        extra_box, _extra_col = section("Witcher 3 Extra Settings", 'PREFERENCES')
        fbx_box = extra_box.box()
        fbx_box.label(text="FBX (Deprecated)")
        fbx_col = fbx_box.column()
        fbx_col.use_property_split = True
        fbx_col.use_property_decorate = False
        # fbx_col.prop(self, "vgmstream_path")
        fbx_col.prop(self, "use_fbx_repo")
        draw_path_prop(fbx_col, "fbx_uncook_path")

class WITCH_OT_ViewportNormals(bpy.types.Operator):
    bl_description = "Switch normal map nodes to a faster custom node. Get https://github.com/theoldben/BlenderNormalGroups addon to enable button"
    bl_idname = 'witcher.normal_map_group'
    bl_label = "Normal Map nodes to Custom"
    bl_options = {'UNDO'}

    @classmethod
    def poll(self, context):
        (exist, enabled) = addon_utils.check("normal_map_to_group")
        return enabled

    def execute(self, context):
        bpy.ops.node.normal_map_group()
        return {'FINISHED'}


class WITCH_OT_ToggleClothSimulation(bpy.types.Operator):
    """Show or hide all APX ClothSimulation geometry-nodes modifiers in the scene."""
    bl_idname = "witcher.toggle_cloth_simulation"
    bl_label = "Toggle Cloth Simulation"
    show: BoolProperty(default=True)

    @classmethod
    def description(cls, context, props):
        return "Show all APX ClothSimulation modifiers in the scene" if props.show else "Hide all APX ClothSimulation modifiers in the scene"

    def execute(self, context):
        from .cloth_util import _find_clothsimulation_modifier
        count = 0
        for obj in context.scene.objects:
            mod = _find_clothsimulation_modifier(obj)
            if mod:
                mod.show_viewport = self.show
                mod.show_render = self.show
                count += 1
        self.report({'INFO'}, f"{'Showed' if self.show else 'Hid'} ClothSimulation on {count} object(s)")
        return {'FINISHED'}


class WITCH_OT_AddConstraints(bpy.types.Operator):
    """Add Constraints"""
    bl_idname = "witcher.add_constraints"
    bl_label = "Add Constraints"
    bl_description = "Object Mode. Create bone constraints based on same bone names or r_weapon/l_weapon bones. Select Armature then Ctrl+Select Armature you want to attach to it"
    action: StringProperty(default="default")

    @classmethod
    def description(cls, context, props):
        action_descriptions = {
            "add_const": (
                "Object Mode: Match bone names between two armatures and add Copy Rotation + Location constraints. "
                "Select the source armature, then Ctrl+click the target armature."
            ),
            "add_const_ik": (
                "Object Mode: Match bone names and add IK constraints. "
                "Select the source armature, then Ctrl+click the target armature."
            ),
            "attach_r_weapon": (
                "Constrain the r_weapon bone of the selected object to the active armature's r_weapon bone. "
                "Used to attach weapon rigs to a character rig."
            ),
            "attach_l_weapon": (
                "Constrain the l_weapon bone of the selected object to the active armature's l_weapon bone. "
                "Used to attach weapon rigs to a character rig."
            ),
        }
        return action_descriptions.get(getattr(props, "action", ""), cls.bl_description)

    def execute(self, context):
        scene = context.scene
        action = self.action
        if action == "add_const":
            constrain_util.do_it(1)
        if action == "add_const_ik":
            constrain_util.do_it(2)
        elif action == "attach_r_weapon":
            constrain_util.attach_weapon("r_weapon")
        elif action == "attach_l_weapon":
            constrain_util.attach_weapon("l_weapon")
        return {'FINISHED'}


class WITCH_OT_load_texarray(bpy.types.Operator, ImportHelper):
    """WITCH_OT_load_texarray"""
    bl_idname = "witcher.load_texarray"
    bl_label = "Load texarray json"
    filename_ext = ".json"
    bl_options = {'REGISTER', 'UNDO'}

    filter_glob: StringProperty(default='*.json', options={'HIDDEN'})
    def execute(self, context):
        fdir = self.filepath
        log.debug("Importing Material")
        if os.path.isdir(fdir):
            self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
            return {'CANCELLED'}
        else:
            import_texarray.start_import(fdir)
        return {'FINISHED'}


class WITCHER_OT_open_external_path(bpy.types.Operator):
    """Open a configured path in the OS file browser"""
    bl_idname = "witcher.open_external_path"
    bl_label = "Open Path"
    bl_options = {'INTERNAL'}

    path: StringProperty()
    treat_as_file: BoolProperty(default=False)

    def execute(self, context):
        if not self.path:
            self.report({'WARNING'}, "Path is empty")
            return {'CANCELLED'}

        path = bpy.path.abspath(self.path)
        path = os.path.normpath(path)
        open_target = os.path.dirname(path) if self.treat_as_file else path

        if not open_target:
            self.report({'WARNING'}, "Path is invalid")
            return {'CANCELLED'}

        if not os.path.exists(open_target):
            self.report({'WARNING'}, f"Path not found: {open_target}")
            return {'CANCELLED'}

        try:
            result = bpy.ops.wm.path_open(filepath=open_target)
            if isinstance(result, set) and 'FINISHED' in result:
                return {'FINISHED'}
        except Exception as e:
            self.report({'ERROR'}, f"Failed to open path: {e}")
            return {'CANCELLED'}

        return {'CANCELLED'}


class WITCHER_OT_open_addon_preferences(bpy.types.Operator):
    """Open Blender Preferences and focus this add-on when possible"""
    bl_idname = "witcher.open_addon_preferences"
    bl_label = "Open Add-on Preferences"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        candidates = []
        for name in (ADDON_NAME, LEGACY_ADDON_NAME):
            if name and name not in candidates:
                candidates.append(name)

        try:
            for addon in getattr(context.preferences, "addons", []):
                module = getattr(addon, "module", "")
                if not module:
                    continue
                if module in candidates:
                    continue
                if module.endswith(".witcher3_tools") or module.endswith(LEGACY_ADDON_NAME):
                    candidates.append(module)
        except Exception:
            pass

        pref_ops = getattr(bpy.ops, "preferences", None)
        if pref_ops and hasattr(pref_ops, "addon_show"):
            for module in candidates:
                try:
                    result = bpy.ops.preferences.addon_show(module=module)
                    if isinstance(result, set) and 'FINISHED' in result:
                        return {'FINISHED'}
                except Exception:
                    continue

        try:
            bpy.ops.screen.userpref_show('INVOKE_DEFAULT')
        except Exception as e:
            self.report({'ERROR'}, f"Failed to open Blender Preferences: {e}")
            return {'CANCELLED'}

        try:
            if hasattr(context.preferences, "active_section"):
                context.preferences.active_section = 'ADDONS'
        except Exception:
            pass

        try:
            wm = context.window_manager
            if hasattr(wm, "addon_search"):
                wm.addon_search = "Witcher 3 Tools"
        except Exception:
            pass

        if pref_ops and hasattr(pref_ops, "addon_expand"):
            for module in candidates:
                try:
                    bpy.ops.preferences.addon_expand(module=module)
                    break
                except Exception:
                    continue

        self.report({'INFO'}, "Opened Blender Preferences > Add-ons")
        return {'FINISHED'}


class WITCHER_OT_dismiss_external_import_alert(bpy.types.Operator):
    """Dismiss the external import dependency warning banner"""
    bl_idname = "witcher.dismiss_external_import_alert"
    bl_label = "Dismiss External Addon Warning"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        clear_external_import_dependency_alert()
        return {'FINISHED'}

#----------------------------------------------------------
#   Utilities panel
#----------------------------------------------------------
import time
from .CR2W.witcher_cache import cache_meta

CACHE_ITEMS = (
    {
        "name": "string_cache.pkl",
        "relative_path": os.path.join("W3Strings", "string_cache.pkl"),
        "label": "string_cache.pkl",
        "description": "Localized string table cache (string IDs to text).",
    },
    {
        "name": "texture_cache.pkl",
        "relative_path": os.path.join("TextureCache", "texture_cache.pkl"),
        "label": "texture_cache.pkl",
        "description": "Vanilla texture cache index from game archives.",
    },
    {
        "name": "texture_cache_mods.pkl",
        "relative_path": os.path.join("TextureCache", "texture_cache_mods.pkl"),
        "label": "texture_cache_mods.pkl",
        "description": "Mod/DLC texture cache index.",
    },
    {
        "name": "collision_cache.pkl",
        "relative_path": os.path.join("CollisionCache", "collision_cache.pkl"),
        "label": "collision_cache.pkl",
        "description": "Vanilla collision cache index.",
    },
    {
        "name": "collision_cache_mods.pkl",
        "relative_path": os.path.join("CollisionCache", "collision_cache_mods.pkl"),
        "label": "collision_cache_mods.pkl",
        "description": "Mod/DLC collision cache index.",
    },
    {
        "name": "speech_cache.pkl",
        "relative_path": os.path.join("Speech", "speech_cache.pkl"),
        "label": "speech_cache.pkl",
        "description": "Speech archive lookup cache.",
    },
    {
        "name": "bundle_cache.pkl",
        "relative_path": os.path.join("Bundles", "bundle_cache.pkl"),
        "label": "bundle_cache.pkl",
        "description": "Vanilla bundle index cache.",
    },
    {
        "name": "bundle_cache_mods.pkl",
        "relative_path": os.path.join("Bundles", "bundle_cache_mods.pkl"),
        "label": "bundle_cache_mods.pkl",
        "description": "Mod/DLC bundle index cache.",
    },
    {
        "name": "journal_browser_bestiary.pkl",
        "relative_path": os.path.join("JournalBrowser", "journal_browser_bestiary.pkl"),
        "label": "journal_browser_bestiary.pkl",
        "description": "Bestiary browser entry cache.",
    },
    {
        "name": "journal_browser_characters.pkl",
        "relative_path": os.path.join("JournalBrowser", "journal_browser_characters.pkl"),
        "label": "journal_browser_characters.pkl",
        "description": "Characters browser entry cache.",
    },
    {
        "name": "journal_icons_bestiary",
        "relative_path": os.path.join("JournalBrowser", "icons", "bestiary"),
        "label": "journal icons (bestiary)",
        "description": "Copied icon images used by the Bestiary browser UI.",
        "is_dir": True,
    },
    {
        "name": "journal_icons_characters",
        "relative_path": os.path.join("JournalBrowser", "icons", "characters"),
        "label": "journal icons (characters)",
        "description": "Copied icon images used by the Characters browser UI.",
        "is_dir": True,
    },
    {
        "name": "pathhashes.csv",
        "relative_path": "pathhashes.csv",
        "label": "pathhashes.csv",
        "description": "Reference table mapping resource hashes to bundle paths.",
    },
    {
        "name": "equipment_categories.json",
        "relative_path": "equipment_categories.json",
        "label": "equipment_categories.json",
        "description": "Cached equipment category and attribute data from gameplay/items XML.",
    },
    {
        "name": "equipment_items_xml_bundle",
        "relative_path": "equipment_items_xml_bundle",
        "label": "equipment_items_xml_bundle",
        "description": "Extracted gameplay/items XML files from bundles used for equipment scanning.",
        "is_dir": True,
    },
)

CACHE_ITEMS_BY_NAME = {item["name"]: item for item in CACHE_ITEMS}
CACHE_ITEM_ORDER = [item["name"] for item in CACHE_ITEMS]

ASSET_BROWSER_MAIN_CACHE_NAMES = {
    "string_cache.pkl",
}

CACHE_GROUP_LABELS = {
    "main": "Main (Asset Browser)",
    "main_mods": "Main Mods (Asset Browser)",
    "other": "Other (Supporting / Reference)",
}

# Backwards-compatible mapping used by existing operators/helpers.
CACHE_PATHS = {item["name"]: item["relative_path"] for item in CACHE_ITEMS}

# Cache health status (not persisted)
CACHE_STATUS = {}

CACHE_STATUS_ICONS = {
    "ok": "CHECKMARK",
    "stale": "ERROR",
    "missing": "CANCEL",
    "unknown": "QUESTION",
    "unchecked": "QUESTION",
}


def _get_cache_item(cache_name: str) -> dict:
    return CACHE_ITEMS_BY_NAME.get(cache_name, {})


def _get_cache_label(cache_name: str) -> str:
    item = _get_cache_item(cache_name)
    return item.get("label", cache_name)


def _get_cache_description(cache_name: str) -> str:
    item = _get_cache_item(cache_name)
    return item.get("description", "Generated cache/reference artifact.")


def _get_cache_group(cache_name: str) -> str:
    name = str(cache_name or "").lower()
    if name.endswith(".pkl"):
        if "mods" in name:
            return "main_mods"
        return "main"
    if cache_name in ASSET_BROWSER_MAIN_CACHE_NAMES:
        return "main"
    return "other"


def _get_cache_group_label(cache_name: str) -> str:
    return CACHE_GROUP_LABELS.get(_get_cache_group(cache_name), CACHE_GROUP_LABELS["other"])

def _get_cache_abs_path(cache_name: str) -> str:
    item = _get_cache_item(cache_name)
    root_kind = item.get("root", "cache")
    cache_root = get_temp_root(create=True) if root_kind == "temp" else get_cache_root(create=True)
    relative_path = CACHE_PATHS.get(cache_name, cache_name)
    return os.path.join(cache_root, relative_path)

def _get_cache_signature_builder(cache_name: str):
    if cache_name == "string_cache.pkl":
        return lambda: W3StringManager.BuildSourceSignature()
    if cache_name == "texture_cache.pkl":
        return lambda: TextureManager.BuildSourceSignature()
    if cache_name == "texture_cache_mods.pkl":
        return lambda: TextureManager.BuildSourceSignature(loadmods=True)
    if cache_name == "collision_cache.pkl":
        return lambda: CollisionManager.BuildSourceSignature()
    if cache_name == "collision_cache_mods.pkl":
        return lambda: CollisionManager.BuildSourceSignature(loadmods=True)
    if cache_name == "speech_cache.pkl":
        return lambda: SpeechManager.BuildSourceSignature()
    if cache_name == "bundle_cache.pkl":
        return lambda: BundleManager.BuildSourceSignature(False)
    if cache_name == "bundle_cache_mods.pkl":
        return lambda: BundleManager.BuildSourceSignature(True)
    if cache_name == "journal_browser_bestiary.pkl":
        return lambda: w3_asset_browser._journal_browser_signature("BESTIARY")
    if cache_name == "journal_browser_characters.pkl":
        return lambda: w3_asset_browser._journal_browser_signature("CHARACTERS")
    return None

def _check_cache_status(cache_name: str):
    cache_path = _get_cache_abs_path(cache_name)
    if not os.path.exists(cache_path):
        return "missing", "Cache file not found"

    item = _get_cache_item(cache_name)
    if bool(item.get("is_dir")):
        return "ok", "Directory present"

    builder = _get_cache_signature_builder(cache_name)
    if builder is None:
        return "ok", "Present (no signature check)"

    try:
        signature, _source = builder()
    except Exception:
        log.debug("Failed to build signature for %s", cache_name, exc_info=True)
        return "unknown", "Signature check failed"
    meta_path = cache_meta.get_meta_path(cache_path)
    meta = cache_meta.load_meta(meta_path)
    meta_signature = meta.get("signature", {}) if isinstance(meta, dict) else {}

    if not meta_signature:
        return "unknown", "No cache metadata"
    if cache_meta.signatures_match(meta_signature, signature):
        return "ok", "Up to date"
    return "stale", "Sources changed"


def _refresh_journal_cache(browser_key: str) -> bool:
    key = (browser_key or "").strip().upper()
    if key not in {"BESTIARY", "CHARACTERS"}:
        return False
    try:
        w3_asset_browser._smart_refresh_journal_cache(key)
        return True
    except Exception:
        log.warning("Failed to refresh journal cache for %s", key, exc_info=True)
        return False


def _refresh_pathhashes_cache() -> bool:
    try:
        from .CR2W.witcher_cache import bundle

        path = _get_cache_abs_path("pathhashes.csv")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        bundle.create_pathhashes(outputPath=path)
        return os.path.exists(path)
    except Exception:
        log.warning("Failed to rebuild pathhashes.csv", exc_info=True)
        return False


def _refresh_equipment_categories_cache() -> bool:
    try:
        op = getattr(getattr(bpy.ops, "witcher", None), "equipment_refresh_categories", None)
        if op is None:
            return False
        result = op()
        return isinstance(result, set) and ("FINISHED" in result)
    except Exception:
        log.warning("Failed to refresh equipment categories cache", exc_info=True)
        return False


def _refresh_equipment_xml_bundle_cache() -> bool:
    try:
        from .ui import ui_equipment

        root = ui_equipment._extract_equipment_xmls_from_bundles()
        return bool(root and os.path.isdir(root))
    except Exception:
        log.warning("Failed to refresh equipment XML bundle cache", exc_info=True)
        return False


def _run_cache_refresh_action(action) -> bool:
    if action is None:
        return False
    try:
        result = action()
    except Exception:
        log.warning("Cache refresh action failed", exc_info=True)
        return False
    if isinstance(result, set):
        return "FINISHED" in result
    if result is None:
        return True
    return bool(result)


def _refresh_cache_by_name(cache_name: str) -> bool:
    refresh_actions = {
        "string_cache.pkl": lambda: W3StringManager.Get(do_reload=True),
        "texture_cache.pkl": lambda: TextureManager.Get(do_reload=True),
        "texture_cache_mods.pkl": lambda: TextureManager.Get(do_reload=True, loadmods=True),
        "collision_cache.pkl": lambda: CollisionManager.Get(do_reload=True),
        "collision_cache_mods.pkl": lambda: CollisionManager.Get(do_reload=True, loadmods=True),
        "speech_cache.pkl": lambda: SpeechManager.Get(do_reload=True),
        "bundle_cache.pkl": lambda: BundleManager.Get(loadmods=False, reset_cache=True),
        "bundle_cache_mods.pkl": lambda: BundleManager.Get(loadmods=True, reset_cache=True),
        "journal_browser_bestiary.pkl": lambda: _refresh_journal_cache("BESTIARY"),
        "journal_browser_characters.pkl": lambda: _refresh_journal_cache("CHARACTERS"),
        "journal_icons_bestiary": lambda: _refresh_journal_cache("BESTIARY"),
        "journal_icons_characters": lambda: _refresh_journal_cache("CHARACTERS"),
        "pathhashes.csv": _refresh_pathhashes_cache,
        "equipment_categories.json": _refresh_equipment_categories_cache,
        "equipment_items_xml_bundle": _refresh_equipment_xml_bundle_cache,
    }
    return _run_cache_refresh_action(refresh_actions.get(cache_name))


def _delete_cache_by_name(cache_name: str) -> bool:
    try:
        if cache_name == "journal_browser_bestiary.pkl":
            w3_asset_browser._clear_journal_browser_caches("BESTIARY")
            return True
        if cache_name == "journal_browser_characters.pkl":
            w3_asset_browser._clear_journal_browser_caches("CHARACTERS")
            return True

        file_path = _get_cache_abs_path(cache_name)
        item = _get_cache_item(cache_name)
        if not os.path.exists(file_path):
            return False

        if bool(item.get("is_dir")) or os.path.isdir(file_path):
            shutil.rmtree(file_path, ignore_errors=False)
            return True

        os.remove(file_path)
        meta_path = cache_meta.get_meta_path(file_path)
        if os.path.exists(meta_path):
            try:
                os.remove(meta_path)
            except Exception:
                pass
        return True
    except Exception:
        log.warning("Failed to delete cache/reference item %s", cache_name, exc_info=True)
        return False


def _draw_cache_management_table(body):
    box = body.box()

    header = box.row(align=True)
    header.label(text="Cache / Reference")
    header.label(text="")
    header.label(text="Status")
    header.label(text="Modified")

    for group_key in ("main", "main_mods", "other"):
        group_items = [name for name in CACHE_ITEM_ORDER if _get_cache_group(name) == group_key]
        if not group_items:
            continue

        group_row = box.row()
        group_row.label(text=CACHE_GROUP_LABELS[group_key], icon='BOOKMARKS')

        for cache_name in group_items:
            cache_path = _get_cache_abs_path(cache_name)
            label = _get_cache_label(cache_name)

            status_info = CACHE_STATUS.get(cache_name, {})
            status = status_info.get("status", "unchecked")
            status_text = status_info.get("detail", "Unchecked")
            icon = CACHE_STATUS_ICONS.get(status, "QUESTION")

            if os.path.exists(cache_path):
                modification_time = os.path.getmtime(cache_path)
                modification_date = time.strftime("%Y-%m-%d %H:%M", time.localtime(modification_time))
            else:
                modification_date = "-"

            row = box.row(align=True)
            row.label(text=label)
            info = row.operator("witcher.cache_info", text="", icon='INFO', emboss=False)
            info.cache_name = cache_name
            row.label(text=status_text, icon=icon)
            row.label(text=modification_date)

            op = row.operator("witcher.check_cache", text="", icon='VIEWZOOM')
            op.cache_name = cache_name
            op = row.operator("witcher.refresh_cache_checked", text="", icon='FILE_REFRESH')
            op.cache_name = cache_name
            op = row.operator("witcher.delete_cache", text="", icon='TRASH')
            op.cache_name = cache_name


# ---------------------------------------------------------------------------
#  Collision / LOD helpers (shared by CMesh panel and export operator)
# ---------------------------------------------------------------------------

_COLLISION_SUFFIXES = ("_col", "_tri", "_box", "_sphere", "_capsule")

def _get_collision_type(obj_name):
    base_name = re.sub(r'\.\d{3}$', '', obj_name)
    for suffix in _COLLISION_SUFFIXES:
        if base_name.endswith(suffix):
            return suffix
    return None

def _find_related_meshes(base_name):
    lod_meshes = []
    col_tri_meshes = []
    for obj in bpy.context.scene.objects:
        if obj.name.startswith(base_name) and obj.name[len(base_name):].startswith("_lod"):
            lod_meshes.append(obj)
        elif obj.name.startswith(base_name):
            if _get_collision_type(obj.name):
                col_tri_meshes.append(obj)
    return lod_meshes, col_tri_meshes


def _get_collision_material_status(obj):
    """Return collision material names/validity for the active collision mesh.

    For tri meshes, returns all mesh material slots in slot order (this is the
    physicalMaterialNames array order used on export).
    """
    if not obj or obj.type != 'MESH':
        return None

    col_type = _get_collision_type(obj.name)
    if not col_type:
        return None

    valid_names = set(PHYSICAL_MATERIAL_NAMES)
    slot_names = []
    for mat in getattr(obj.data, "materials", []):
        slot_names.append(mat.name if mat else "")

    entries = []
    if col_type == "_tri":
        for idx, name in enumerate(slot_names):
            entries.append({
                "slot": idx,
                "name": name,
                "valid": bool(name) and (name in valid_names),
            })
    else:
        name = slot_names[0] if slot_names else ""
        entries.append({
            "slot": 0,
            "name": name,
            "valid": bool(name) and (name in valid_names),
        })

    valid_count = sum(1 for entry in entries if entry["valid"])
    return {
        "type": col_type,
        "entries": entries,
        "valid_count": valid_count,
        "invalid_count": len(entries) - valid_count,
    }

def _resolve_cmesh_target(context):
    """Return the target mesh object for the CMesh panel.
    If an armature is selected, returns the first mesh child (lod0).
    If a mesh is selected, returns it directly.
    Returns None if no valid target found."""
    ob = context.active_object
    if not ob:
        return None
    if ob.type == 'MESH':
        return ob
    if ob.type == 'ARMATURE':
        meshes = [child for child in ob.children if child.type == 'MESH']
        return meshes[0] if meshes else None
    return None


def _get_cmesh_header_status(context) -> str:
    mesh_ob = _resolve_cmesh_target(context)
    if mesh_ob is not None:
        return mesh_ob.name
    ob = getattr(context, "active_object", None)
    if ob is not None:
        return f"No target ({ob.type}: {ob.name})"
    return "No target"

def _is_terrain_root(obj):
    return (
        obj is not None
        and obj.type == 'EMPTY'
        and "terrainSize" in obj
        and "x_tiles" in obj
        and "y_tiles" in obj
    )


def _is_terrain_tile(obj):
    return (
        obj is not None
        and obj.type == 'MESH'
        and "terrain_multires" in obj
        and "tile_x" in obj
        and "tile_y" in obj
    )


def _is_terrain_full_map(obj):
    return (
        obj is not None
        and obj.type == 'MESH'
        and obj.get("terrain_mode") == "full_map"
    )


def _terrain_root_from_object(obj):
    current = obj
    while current is not None:
        if _is_terrain_root(current):
            return current
        current = current.parent
    return None


def _resolve_terrain_root(context):
    if not context or not context.active_object:
        return None
    return _terrain_root_from_object(context.active_object)


def _resolve_terrain_full_map(context):
    if not context or not context.active_object:
        return None
    obj = context.active_object
    if _is_terrain_full_map(obj):
        return obj
    return None


def _get_terrain_tiles(root):
    if not root:
        return []
    return [child for child in root.children if _is_terrain_tile(child)]


def _draw_external_path_sections(layout, addon_prefs, section_prefix="witcher_extpaths"):
    """Draw categorized external path shortcuts inside the given layout."""
    layout.use_property_decorate = False

    action_row = layout.row(align=True)
    action_row.operator("witcher.open_addon_preferences", text="Open Add-on Preferences", icon='PREFERENCES')

    def add_row(col, label, path_value, is_file=False):
        path_text = str(path_value or "").strip()
        is_set = bool(path_text)

        row = col.row(align=True)
        row.alert = not is_set
        row.label(text=label)
        op = row.operator("witcher.open_external_path", text="", icon="FILE_FOLDER")
        op.path = path_text
        op.treat_as_file = is_file

        path_row = col.row(align=True)
        if is_set:
            path_row.label(text=path_text, icon='FILE' if is_file else 'FILE_FOLDER')
        else:
            path_row.alert = True
            path_row.label(text="Open Preferences to add this path", icon='PREFERENCES')

    def section(section_id, label, icon, default_closed=False):
        container = layout.box()
        header, body = container.panel(section_id, default_closed=default_closed)
        header.label(text=label, icon=icon)
        return body

    # --- Witcher 3 paths ---
    body = section(f"{section_prefix}_w3", "Witcher 3", 'SCENE_DATA')
    if body:
        col = body.column(align=True)
        add_row(col, "Game", addon_prefs.witcher_game_path)
        add_row(col, "Uncook", addon_prefs.uncook_path)
        if bool(getattr(addon_prefs, "use_separate_texture_uncook_path", False)):
            add_row(col, "Textures", addon_prefs.tex_uncook_path)
        else:
            add_row(col, "Textures (Uncook)", addon_prefs.uncook_path)

    # --- Mod / Tools paths ---
    body = section(f"{section_prefix}_modtools", "Mod / Tools", 'TOOL_SETTINGS')
    if body:
        col = body.column(align=True)
        add_row(col, "WolvenKit CLI", addon_prefs.wolvenkit, is_file=True)
        add_row(col, "WolvenKit Project", addon_prefs.mod_directory)
        add_row(col, "Mod Textures", addon_prefs.tex_mod_uncook_path)

    # --- REDkit paths ---
    body = section(f"{section_prefix}_redkit", "REDkit", 'FILE_FOLDER')
    if body:
        col = body.column(align=True)
        add_row(col, "REDkit Depot", addon_prefs.redkit_depot_path)
        add_row(col, "REDkit Uncooked", addon_prefs.redkit_uncooked_path)
        for item in addon_prefs.redkit_projects:
            label = os.path.basename(item.path.rstrip("\\/")) or "Project"
            add_row(col, f"Project: {label}", item.path)

    # --- Audio ---
    body = section(f"{section_prefix}_audio", "Audio", 'SOUND', default_closed=True)
    if body:
        col = body.column(align=True)
        add_row(col, "Speech Audio", addon_prefs.W3_VOICE_PATH)

    # --- Witcher 2 paths ---
    body = section(f"{section_prefix}_w2", "Witcher 2", 'SCENE_DATA', default_closed=True)
    if body:
        col = body.column(align=True)
        add_row(col, "Game", addon_prefs.witcher2_game_path)
        add_row(col, "Unbundle", addon_prefs.w2_unbundle_path)

    # --- Extra / user-defined paths ---
    if len(addon_prefs.path_list) > 0:
        body = section(f"{section_prefix}_extra", "Extra Paths", 'FILEBROWSER', default_closed=True)
        if body:
            col = body.column(align=True)
            for item in addon_prefs.path_list:
                label = os.path.basename(item.path.rstrip("\\/")) or "Path"
                add_row(col, label, item.path)


# ---------------------------------------------------------------------------
#  CMesh Properties Panel
# ---------------------------------------------------------------------------

class WITCH_PT_CMesh(WITCH_PT_Base, bpy.types.Panel):
    bl_label = "CMesh"
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return context is not None and context.scene is not None

    def draw_header(self, context):
        self.layout.label(text="", icon='MESH_DATA')

    def draw_header_preset(self, context):
        text = _get_cmesh_header_status(context)
        ui_scale = context.preferences.system.ui_scale
        # ~7 logical pixels per character; ~110px reserved for fold arrow, icon, "CMesh" title, padding
        max_chars = max(8, int((context.region.width - 110 * ui_scale) / (7 * ui_scale)))
        if len(text) > max_chars:
            text = text[:max_chars - 3] + "..."
        self.layout.label(text=text)

    # Reorganized into collapsible boxed sections so mesh metadata and edit controls scan top-to-bottom.
    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        mesh_ob = _resolve_cmesh_target(context)
        active_ob = getattr(context, "active_object", None)

        banner = layout.box()
        banner_col = banner.column(align=True)
        if not mesh_ob:
            banner_col.label(text="No CMesh target selected", icon='INFO')
            if active_ob is not None:
                banner_col.label(text=f"Active: {active_ob.name} ({active_ob.type})", icon='RESTRICT_SELECT_OFF')
            banner_col.label(text="Select a mesh, or an armature with mesh children.")
            return
        banner_col.label(text=f"Target: {mesh_ob.name}", icon='CHECKMARK')
        if active_ob is not None and active_ob != mesh_ob:
            banner_col.label(text=f"Active selection: {active_ob.name} ({active_ob.type})", icon='RESTRICT_SELECT_OFF')
        if not hasattr(mesh_ob, "witcherui_MeshSettings"):
            banner_col.label(text="Selected mesh has no Witcher mesh settings.", icon='ERROR')
            return
        mesh_settings = mesh_ob.witcherui_MeshSettings

        def section(section_id, label, icon, default_closed=False):
            container = layout.box()
            header, body = container.panel(section_id, default_closed=default_closed)
            header.label(text=label, icon=icon)
            return body

        # --- Mesh Info ---
        body = section("witcher_cmesh_info", "Mesh Info", 'OBJECT_DATA')
        if body:
            col = body.column(align=True)
            col.prop(mesh_settings, "item_repo_path")
            row = col.row(align=True)
            row.prop(mesh_settings, "lod_level")
            row.prop(mesh_settings, "distance")
            col.prop(mesh_settings, "mat_id")

        # --- CMesh Properties ---
        body = section("witcher_cmesh_props", "CMesh Properties", 'MESH_DATA')
        if body:
            col = body.column(align=True)
            col.prop(mesh_settings, "autohideDistance")
            col.prop(mesh_settings, "isTwoSided")
            col.prop(mesh_settings, "useExtraStreams")
            row = col.row()
            row.prop(mesh_settings, "generalizedMeshRadius")
            row.enabled = False
            col.prop(mesh_settings, "mergeInGlobalShadowMesh")
            col.prop(mesh_settings, "isOccluder")
            col.prop(mesh_settings, "smallestHoleOverride")
            col.prop(mesh_settings, "isStatic")
            col.prop(mesh_settings, "entityProxy")

        # --- Sound Info ---
        body = section("witcher_cmesh_sound", "Sound Info", 'SOUND', default_closed=True)
        if body:
            col = body.column(align=True)
            if mesh_settings.soundInfo_enabled:
                col.operator("witcher.remove_sound_info", text="Remove Sound Info", icon='X')
                col.prop(mesh_settings, "soundInfo_soundTypeIdentification", text="Sound Type Identification")
                col.prop(mesh_settings, "soundInfo_soundSizeIdentification", text="Sound Size Identification")
                col.prop(mesh_settings, "soundInfo_soundBoneMappingInfo", text="Bone Mapping Preset")
            else:
                col.operator("witcher.create_sound_info", text="Create Sound Info", icon='ADD')

        # --- LODs ---
        base_name = mesh_ob.name.rsplit('_lod0', 1)[0]
        lod_meshes, col_tri_meshes = _find_related_meshes(base_name)

        body = section("witcher_cmesh_lods", "LODs", 'MOD_DECIM')
        if body:
            col = body.column(align=True)
            if lod_meshes:
                for lod_mesh in lod_meshes:
                    row = col.row(align=True)
                    row.label(text=lod_mesh.name)
                    if hasattr(lod_mesh, "witcherui_MeshSettings"):
                        row.prop(lod_mesh.witcherui_MeshSettings, "distance", text="Dist")
            else:
                col.label(text="No related LOD meshes found", icon='INFO')
            col.separator()
            col.operator("witcher.generate_lods", text="Generate LODs", icon='MESH_DATA')

        # --- Collision ---
        body = section("witcher_cmesh_collision", "Collision", 'MOD_PHYSICS', default_closed=True)
        if body:
            col = body.column(align=True)

            # Create Collider at top — stays stable as the list grows below
            selected_material = DEFAULT_PHYSICAL_MATERIAL
            if hasattr(context.scene, "witcher_collision_physical_material"):
                col.prop(context.scene, "witcher_collision_physical_material", text="Physical Material")
                selected_material = context.scene.witcher_collision_physical_material

            action_box = col.box()
            action_box.label(text="Create Collider", icon='ADD')
            row = action_box.row(align=True)
            op = row.operator("witcher.create_box_collider", text="Box", icon='MESH_CUBE')
            op.physical_material = selected_material
            op = row.operator("witcher.create_sphere_collider", text="Sphere", icon='MESH_UVSPHERE')
            op.physical_material = selected_material
            row = action_box.row(align=True)
            op = row.operator("witcher.create_capsule_collider", text="Capsule", icon='MESH_CAPSULE')
            op.physical_material = selected_material
            op = row.operator("witcher.create_convex_collider", text="Convex", icon='MESH_ICOSPHERE')
            op.physical_material = selected_material
            row = action_box.row(align=True)
            op = row.operator("witcher.create_trimesh_collider", text="Trimesh", icon='MESH_DATA')
            op.physical_material = selected_material

            col.separator()

            # Collision mesh list below the create buttons
            if col_tri_meshes:
                list_box = col.box()
                list_box.label(text="Collision Meshes", icon='OUTLINER_OB_MESH')
                for col_mesh in col_tri_meshes:
                    row = list_box.row()
                    col_type = _get_collision_type(col_mesh.name) or "collision"
                    phys_mat = (col_mesh.data.materials[0].name
                                if col_mesh.data.materials else "—")
                    row.label(text=f"{col_mesh.name}  [{col_type}]  {phys_mat}")
            else:
                col.label(text="No collision meshes found", icon='INFO')

            active_collision_status = _get_collision_material_status(mesh_ob)
            if active_collision_status:
                status_box = col.box()
                is_tri = active_collision_status["type"] == "_tri"
                status_box.label(
                    text="Active Collision Trimesh Materials" if is_tri else "Active Collision Material",
                    icon='MATERIAL'
                )
                if is_tri:
                    status_box.label(text="Slot order matches physicalMaterialNames array in the file", icon='INFO')

                if not active_collision_status["entries"]:
                    row = status_box.row()
                    row.alert = True
                    row.label(text="No material slots on active collision mesh", icon='ERROR')
                else:
                    for entry in active_collision_status["entries"]:
                        row = status_box.row(align=True)
                        row.label(
                            text=f"[{entry['slot']}] {entry['name'] or '<empty>'}",
                            icon='CHECKMARK' if entry["valid"] else 'ERROR'
                        )
                        row.label(text="valid" if entry["valid"] else "not in collision material list")

                    summary = status_box.row()
                    summary.alert = active_collision_status["invalid_count"] > 0
                    summary.label(
                        text=(
                            f"Valid: {active_collision_status['valid_count']}  "
                            f"Invalid: {active_collision_status['invalid_count']}"
                        ),
                        icon='INFO' if active_collision_status["invalid_count"] == 0 else 'ERROR'
                    )


class WITCHER_OT_select_terrain_tiles(bpy.types.Operator):
    bl_idname = "witcher.select_terrain_tiles"
    bl_label = "Select Terrain Tiles"
    bl_description = "Select all terrain tile meshes under the active terrain root"

    def execute(self, context):
        root = _resolve_terrain_root(context)
        if root is None:
            self.report({'WARNING'}, "Select a terrain root or terrain tile first")
            return {'CANCELLED'}

        tiles = _get_terrain_tiles(root)
        if not tiles:
            self.report({'WARNING'}, "No terrain tiles found under the active terrain root")
            return {'CANCELLED'}

        for obj in list(context.selected_objects):
            obj.select_set(False)
        for obj in tiles:
            obj.select_set(True)
        context.view_layer.objects.active = tiles[0]
        self.report({'INFO'}, f"Selected {len(tiles)} terrain tiles")
        return {'FINISHED'}


class WITCHER_OT_apply_fullmap_multires(bpy.types.Operator):
    bl_idname = "witcher.apply_fullmap_multires"
    bl_label = "Apply Full-Map Multires"
    bl_description = "Adjust multires level on the selected full terrain map object"

    target_level: IntProperty(
        name="Target Level",
        description="Target multires subdivision level",
        default=5,
        min=0,
        max=10,
    )

    def execute(self, context):
        obj = _resolve_terrain_full_map(context)
        if obj is None:
            self.report({'WARNING'}, "Select a full-map terrain object first")
            return {'CANCELLED'}

        if context.mode != 'OBJECT':
            try:
                bpy.ops.object.mode_set(mode='OBJECT')
            except Exception:
                pass

        from .importers import import_w2w
        if not import_w2w.adjust_full_map_multires(obj, self.target_level):
            self.report({'ERROR'}, "Failed to adjust full-map multires")
            return {'CANCELLED'}

        self.report({'INFO'}, f"{obj.name}: multires set to {self.target_level}")
        return {'FINISHED'}


class WITCHER_OT_apply_terrain_material_values(bpy.types.Operator):
    bl_idname = "witcher.apply_terrain_material_values"
    bl_label = "Apply Terrain Material Values"
    bl_description = "Apply terrain roughness/specular to all imported terrain materials"

    def execute(self, context):
        scene_settings = getattr(context.scene, "witcher_file_browser", None)
        if scene_settings is None:
            self.report({'ERROR'}, "Terrain settings are not available")
            return {'CANCELLED'}

        roughness = max(0.0, min(1.0, float(getattr(scene_settings, "terrain_material_roughness", 0.82))))
        specular = max(0.0, min(1.0, float(getattr(scene_settings, "terrain_material_specular", 0.12))))

        from .importers import import_w2w
        updated = import_w2w.update_all_terrain_material_values(roughness, specular)
        if updated <= 0:
            self.report({'WARNING'}, "No imported terrain materials found to update")
        else:
            self.report({'INFO'}, f"Updated {updated} terrain materials")
        return {'FINISHED'}


class WITCH_PT_Terrain(WITCH_PT_Base, bpy.types.Panel):
    bl_label = "Terrain"
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return context is not None and context.scene is not None

    def draw_header(self, context):
        self.layout.label(text="", icon='WORLD_DATA')

    # Reorganized terrain UI into inspector-like sections with clearer selection stats and action groups.
    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        scene_settings = getattr(context.scene, "witcher_file_browser", None)
        target_level = int(getattr(scene_settings, "terrain_multires_level", 5))

        def section(section_id, label, icon, default_closed=False):
            container = layout.box()
            header, body = container.panel(section_id, default_closed=default_closed)
            header.label(text=label, icon=icon)
            return body

        if scene_settings and hasattr(scene_settings, "terrain_import_mode"):
            body = section("witcher_terrain_import_mode", "Import Mode", 'SETTINGS')
            if body:
                col = body.column(align=True)
                col.prop(scene_settings, "terrain_import_mode", text="")
                col.prop(scene_settings, "terrain_multires_level", text="Target Multires")

            if hasattr(scene_settings, "terrain_material_roughness") and hasattr(scene_settings, "terrain_material_specular"):
                body = section("witcher_terrain_material", "Material", 'MATERIAL', default_closed=True)
                if body:
                    col = body.column(align=True)
                    row = col.row(align=True)
                    row.prop(scene_settings, "terrain_material_roughness", text="Roughness")
                    row.prop(scene_settings, "terrain_material_specular", text="Specular")
                    col.operator(
                        "witcher.apply_terrain_material_values",
                        text="Apply Material To Loaded Terrain",
                        icon='SHADING_RENDERED',
                    )

        body = section("witcher_terrain_collection", "Layer Collection", 'OUTLINER_COLLECTION', default_closed=True)
        coll = context.collection
        if body:
            col = body.column(align=True)
            if coll:
                col.prop(coll, "name")
                group_type = str(coll.get("group_type", "")).strip()
                world_path = str(coll.get("world_path", "")).strip()
                level_path = str(coll.get("level_path", "")).strip()
                layer_build_tag = str(coll.get("layerBuildTag", "")).strip()

                if group_type:
                    col.label(text=f"group_type: {group_type}")
                if world_path:
                    col.label(text=f"world_path: {world_path}")
                if level_path:
                    col.label(text=f"level_path: {level_path}")
                if layer_build_tag:
                    col.label(text=f"layerBuildTag: {layer_build_tag}")

                has_level_button = bool(level_path)
                has_group_button = group_type == "LayerGroup"
                if has_level_button or has_group_button:
                    col.separator()
                    col_load = col.column(align=True)
                    if has_level_button:
                        col_load.operator("witcher.load_layer", text="Load This Level", icon='CUBE')
                    if has_group_button:
                        col_load.operator("witcher.load_layer_group", text="Load This LayerGroup", icon='OUTLINER_COLLECTION')
            else:
                col.label(text="No active collection", icon='INFO')

        full_map_obj = _resolve_terrain_full_map(context)
        if full_map_obj:
            body = section("witcher_terrain_full_map", "Full Map", 'NODETREE')
            if body:
                col = body.column(align=True)
                col.label(text=f"Object: {full_map_obj.name}")
                col.label(text=f"Hub: {str(full_map_obj.get('terrain_hub', '-'))}")
                col.label(text=f"Terrain Size: {float(full_map_obj.get('terrainSize', 0.0)):.2f}")
                col.label(text=f"Elevation: {float(full_map_obj.get('lowestElevation', 0.0)):.2f} .. {float(full_map_obj.get('highestElevation', 0.0)):.2f}")

                multires = None
                for mod in full_map_obj.modifiers:
                    if mod.type == 'MULTIRES':
                        multires = mod
                        break
                if multires is not None:
                    col.separator()
                    col.label(text=f"Multires Total: {int(getattr(multires, 'total_levels', 0))}")
                    col.label(text=f"Multires View: {int(getattr(multires, 'levels', 0))}")

                col.separator()
                row = col.row(align=True)
                op = row.operator("witcher.apply_fullmap_multires", text="Apply Full Map Multires", icon='MOD_MULTIRES')
                op.target_level = target_level
            return

        root = _resolve_terrain_root(context)
        if not root:
            body = section("witcher_terrain_none_selected", "Terrain Selection", 'INFO')
            if body:
                col = body.column(align=True)
                col.label(text="No terrain object selected", icon='INFO')
                col.operator("witcher.import_w2w", text="Import .w2w / .yml", icon='IMPORT')
            return

        tiles = _get_terrain_tiles(root)
        selected_tiles = [
            obj for obj in context.selected_objects
            if _is_terrain_tile(obj) and _terrain_root_from_object(obj) == root
        ]

        body = section("witcher_terrain_tile_info", "Tile Terrain", 'GRID')
        if body:
            col = body.column(align=True)
            col.label(text=f"Root: {root.name}")
            col.label(text=f"Grid: {int(root.get('x_tiles', 0))} x {int(root.get('y_tiles', 0))}")
            col.label(text=f"Loaded Tiles: {len(tiles)}")
            col.label(text=f"Terrain Size: {float(root.get('terrainSize', 0.0)):.2f}")
            col.label(text=f"Elevation: {float(root.get('lowestElevation', 0.0)):.2f} .. {float(root.get('highestElevation', 0.0)):.2f}")

        body = section("witcher_terrain_tile_controls", "Tile Controls", 'MOD_MULTIRES')
        if body:
            col = body.column(align=True)
            col.label(text=f"Selected Tiles: {len(selected_tiles)}")
            col.separator()

            # Tile actions stay stacked to remain readable in the narrow N-panel.
            col_tile = col.column(align=True)
            col_tile.operator("witcher.select_terrain_tiles", text="Select Root Tiles", icon='RESTRICT_SELECT_OFF')
            op = col_tile.operator("witcher.adjust_tile_multires", text="Apply Tile Multires", icon='MOD_MULTIRES')
            op.target_level = target_level


_IMPORT_ORIGIN_PROPS = {
    "origin": "witcher_import_origin",
    "source_game": "witcher_source_game",
    "entity_path": "witcher_entity_path",
    "item_category": "witcher_item_category",
    "item_name": "witcher_item_name",
    "equip_template": "witcher_equip_template",
    "item_appearance": "witcher_item_appearance",
    "owner_entity_path": "witcher_owner_entity_path",
}

_IMPORT_ORIGIN_LABELS = {
    "origin": "Origin",
    "source_game": "Source Game",
    "entity_path": "Entity Path",
    "item_category": "Category",
    "item_name": "Item",
    "equip_template": "Equip Template",
    "item_appearance": "Item Appearance",
    "owner_entity_path": "Owner Entity",
}

_ORIGIN_DISPLAY_NAMES = {
    "direct_entity": "Direct Entity",
    "equipment_slot": "Equipment Slot",
    "template_slot": "Template Slot",
}


def _read_import_origin_info(obj):
    """Read import origin metadata from an object and its parent (one level up)."""
    if obj is None or not hasattr(obj, "get"):
        return {}
    info = {}
    for source in (obj, getattr(obj, "parent", None)):
        if source is None or not hasattr(source, "get"):
            continue
        for key, prop_name in _IMPORT_ORIGIN_PROPS.items():
            if key in info:
                continue
            val = str(source.get(prop_name, "") or "").strip()
            if val:
                info[key] = val
    return info


def _draw_import_source_section(layout, context, obj):
    info = _read_import_origin_info(obj)
    col = layout.column(align=True)
    if not info:
        col.label(text="No import metadata found", icon='INFO')
        return
    for key in _IMPORT_ORIGIN_PROPS:
        value = info.get(key, "")
        if not value:
            continue
        label = _IMPORT_ORIGIN_LABELS.get(key, key)
        if key == "origin":
            value = _ORIGIN_DISPLAY_NAMES.get(value, value)
        col.label(text=f"{label}: {value}")


class WITCH_PT_Utils(WITCH_PT_Base, bpy.types.Panel):
    bl_label = "Utilities / Settings"
    bl_options = {'DEFAULT_CLOSED'}

    def draw_header(self, context):
        self.layout.label(text="", icon='TOOL_SETTINGS')

    # Reorganized utility panel into context, path, cache, and export sections for cleaner scanning.
    def draw(self, context):
        ob = context.object
        coll = context.collection
        layout = self.layout
        layout.use_property_decorate = False

        def section(section_id, label, icon, default_closed=False):
            container = layout.box()
            header, body = container.panel(section_id, default_closed=default_closed)
            header.label(text=label, icon=icon)
            return body

        if ob:
            entity_type = str(ob.get("entity_type", "")).strip()
            template = str(ob.get("template", "")).strip()
            if entity_type or template:
                box = layout.box()
                col = box.column(align=True)
                col.label(text=(entity_type if entity_type else ob.name), icon='OBJECT_DATA')
                col.prop(ob, "name")
                if template:
                    col.label(text=f"template: {template}")
                if entity_type:
                    col.label(text=f"entity_type: {entity_type}")

            body = section("witcher_utils_import_source", "Import Source", 'IMPORT')
            if body:
                _draw_import_source_section(body, context, ob)

        if coll:
            has_witcher_data = any(
                str(coll.get(k, "")).strip()
                for k in ("group_type", "world_path", "level_path", "layerBuildTag")
            )
            if has_witcher_data:
                box = layout.box()
                col = box.column(align=True)
                col.label(text=coll.name, icon='OUTLINER_COLLECTION')
                col.prop(coll, "name")

        body = section("witcher_utils_addon_settings", "Addon Settings", 'SETTINGS')
        if body:
            addon_prefs = get_all_addon_prefs(context)
            col = body.column(align=True)
            col.prop(addon_prefs, "use_fbx_repo")
            if hasattr(addon_prefs, "verbose_logging"):
                col.prop(addon_prefs, "verbose_logging")
                if addon_prefs.verbose_logging:
                    warn_row = col.row()
                    warn_row.alert = True
                    warn_row.label(
                        text="Debug Logging Active \u2014 detailed output in System Console",
                        icon='INFO',
                    )

            game_path_issue = get_witcher3_game_path_issue(context)
            if game_path_issue:
                warn = col.box()
                warn.alert = True
                warn.label(text="Witcher 3 path is not configured correctly", icon='ERROR')
                warn.label(text=game_path_issue)

            if hasattr(addon_prefs, "witcher_game_path"):
                col.separator()
                col.prop(addon_prefs, "witcher_game_path", text="Game Path")

        body = section("witcher_utils_display_controls", "Display Controls", 'HIDE_OFF', default_closed=True)
        if body:
            col = body.column(align=True)
            col.label(text="LOD Visibility", icon='MOD_DECIM')
            row_lod = col.row(align=True)
            row_lod.operator(WITCH_OT_ENTITY_lod_toggle.bl_idname, text="LOD0", icon='MESH_DATA').action = "_lod0"
            row_lod.operator(WITCH_OT_ENTITY_lod_toggle.bl_idname, text="LOD1", icon='MESH_DATA').action = "_lod1"
            row_lod.operator(WITCH_OT_ENTITY_lod_toggle.bl_idname, text="LOD2", icon='MESH_DATA').action = "_lod2"

            col.separator()
            col.label(text="Collision Visibility", icon='MOD_PHYSICS')
            row = col.row(align=True)
            row.operator(WITCH_OT_ENTITY_lod_toggle.bl_idname, text="Hide", icon='HIDE_ON').action = "_collisionHide"
            row.operator(WITCH_OT_ENTITY_lod_toggle.bl_idname, text="Show", icon='HIDE_OFF').action = "_collisionShow"

        body = section("witcher_utils_rot90", "Rig Orientation (Rot90)", 'ARMATURE_DATA', default_closed=True)
        if body:
            addon_prefs = get_all_addon_prefs(context)
            col = body.column(align=True)
            if hasattr(addon_prefs, "do_fix_tail"):
                col.prop(addon_prefs, "do_fix_tail", text="Default On Import")
            obj = context.active_object
            armature = None
            if obj and obj.type == 'ARMATURE':
                armature = obj
            elif obj and obj.parent and obj.parent.type == 'ARMATURE':
                armature = obj.parent
            if armature and hasattr(armature.data, 'witcherui_RigSettings'):
                rig_settings = armature.data.witcherui_RigSettings
                rot90_on = bool(getattr(rig_settings, "rot90_imported", False))
                col.label(text=f"Rig: {armature.name}  ({'Display Fix ON' if rot90_on else 'Display Fix OFF'})")
                col.operator(
                    "witcher.toggle_rot90",
                    text="Remove Display Fix" if rot90_on else "Apply Display Fix",
                    icon='BONE_DATA'
                )
            else:
                col.label(text="Select an armature to toggle Rot90.", icon='INFO')

        body = section("witcher_utils_external_paths", "External Paths", 'FILE_FOLDER', default_closed=True)
        if body:
            addon_prefs = get_all_addon_prefs(context)
            _draw_external_path_sections(body, addon_prefs, section_prefix="witcher_utils_extpaths")

        body = section("witcher_utils_cache", "Cache Management", 'FILE_FOLDER')
        if body:
            _draw_cache_management_table(body)

        body = section("witcher_utils_cache_export", "Export Counts / Bulk Export", 'EXPORT', default_closed=True)
        if body:
            # File export counts + bulk-export controls
            ui_cache_export.draw_cache_export_ui(body, context)

        body = section("witcher_utils_about", "About", 'INFO', default_closed=True)
        if body:
            col = body.column(align=True)
            version_text = ".".join(str(part) for part in bl_info.get("version", ()))
            col.label(text=f"Witcher 3 Tools v{version_text}")
            col.label(text=f"Author: {bl_info.get('author', 'Unknown')}")
            doc_url = bl_info.get("doc_url", "") or "https://github.com/dingdio/Witcher3_Blender_Tools"
            row = col.row(align=True)
            row.operator("wm.url_open", text="GitHub", icon='URL').url = doc_url
            col.label(text="Settings also live in Add-on Preferences", icon='PREFERENCES')

from .ui.ui_custom_icons import custom_icons

WITCHER_TOOLS_TABS = [
    ('TOOLS',    'Tools',    'Rigging, animation, and helper tools'),
    ('SETTINGS', 'Settings', 'Settings, paths, cache, and addon configuration'),
]
class WITCH_PT_Main(WITCH_PT_Base, bpy.types.Panel):
    bl_idname = "WITCH_PT_Main"
    bl_label = "Witcher 3 Tools"

    def draw_header(self, context):
        layout = self.layout
        if custom_icons:
            layout.template_icon(icon_value=custom_icons["main"]["witcher_icon"].icon_id)
        else:
            layout.label(text="", icon='BONE_DATA')

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = False
        layout.use_property_decorate = False
        scene = context.scene

        def section(section_id, label, icon, default_closed=False):
            container = layout.box()
            header, body = container.panel(section_id, default_closed=default_closed)
            header.label(text=label, icon=icon)
            return body

        # Game path warning — always visible at top if misconfigured
        game_path_issue = get_witcher3_game_path_issue(context)
        if game_path_issue:
            warn_box = layout.box()
            warn_box.alert = True
            warn_col = warn_box.column(align=True)
            warn_row = warn_col.row(align=True)
            warn_row.label(text="SET WITCHER 3 PATH", icon='ERROR')
            warn_row.operator("witcher.open_addon_preferences", text="Open Preferences", icon='PREFERENCES')
            warn_col.label(text=f"Need folder containing {WITCHER3_EXE_REL}")
            warn_col.label(text=game_path_issue)
            warn_col.operator("witcher.autofind_w3_path", text="Auto Find Witcher 3 Path", icon='VIEWZOOM')
            layout.separator()

        # ── Asset Browser (always visible) ────────────────────────
        ext_dep_alert = get_external_import_dependency_alert()
        if ext_dep_alert:
            ext_box = layout.box()
            ext_box.alert = True
            ext_col = ext_box.column(align=True)
            ext_head = ext_col.row(align=True)
            ext_head.label(text="SET EXTERNAL ADDONS", icon='ERROR')
            ext_head.operator("witcher.open_addon_preferences", text="Open Preferences", icon='PREFERENCES')
            ext_head.operator("witcher.dismiss_external_import_alert", text="", icon='PANEL_CLOSE')

            alert_kind = ext_dep_alert.get("kind", "")
            alert_status = ext_dep_alert.get("status", "")
            if alert_kind == "redcloth":
                ext_col.label(text="Tried to import Redcloth, but external APX support is not ready.")
                ext_col.label(text="Enable io_mesh_apx. APB->APX conversion also needs apex_sdk_cli.")
            elif alert_kind == "speedtree":
                ext_col.label(text="Tried to import SpeedTree (.srt), but io_mesh_srt is not enabled.")
                ext_col.label(text="Enable the io_mesh_srt add-on to import SpeedTree files.")
            else:
                ext_col.label(text="A required external import add-on is missing or not configured.")

            if alert_status == "apx_sdk_missing":
                ext_col.label(text="APX SDK CLI is missing/invalid in io_mesh_apx settings.")
            elif alert_status == "apx_addon_disabled":
                ext_col.label(text="io_mesh_apx is missing or disabled.")
            elif alert_status == "srt_addon_disabled":
                ext_col.label(text="io_mesh_srt is missing or disabled.")

            source_name = (ext_dep_alert.get("source_name") or "").strip()
            if source_name:
                ext_col.label(text=f"File: {source_name}")

            alert_reason = (ext_dep_alert.get("reason") or "").strip()
            if alert_reason and alert_reason not in {"io_mesh_apx addon is not enabled.", "io_mesh_srt addon is not enabled."}:
                ext_col.label(text=alert_reason)
            layout.separator()

        from .ui.ui_file_browser import WITCHER_PT_AssetBrowser
        WITCHER_PT_AssetBrowser.draw(self, context)

        layout.separator(factor=0.5)

        # ── 2-tab nav ─────────────────────────────────────────────
        nav_row = layout.row(align=True)
        nav_row.scale_y = 1.8
        nav_row.prop_enum(scene, "witcher_tools_tab", 'TOOLS')
        nav_row.prop_enum(scene, "witcher_tools_tab", 'SETTINGS')
        layout.separator(factor=0.3)

        tab = getattr(scene, "witcher_tools_tab", "TOOLS")

        # ══ TOOLS TAB ═════════════════════════════════════════════
        if tab == "TOOLS":
            ob = context.object
            coll = context.collection

            if ob:
                entity_type = str(ob.get("entity_type", "")).strip()
                template = str(ob.get("template", "")).strip()
                if entity_type or template:
                    box = layout.box()
                    col = box.column(align=True)
                    col.label(text=(entity_type if entity_type else ob.name), icon='OBJECT_DATA')
                    col.prop(ob, "name")
                    if template:
                        col.label(text=f"template: {template}")
                    if entity_type:
                        col.label(text=f"entity_type: {entity_type}")

            if coll:
                has_witcher_data = any(
                    str(coll.get(k, "")).strip()
                    for k in ("group_type", "world_path", "level_path", "layerBuildTag")
                )
                if has_witcher_data:
                    box = layout.box()
                    col = box.column(align=True)
                    col.label(text=coll.name, icon='OUTLINER_COLLECTION')
                    col.prop(coll, "name")

            body = section("witcher_tools_display", "Display Controls", 'HIDE_OFF')
            if body:
                col = body.column(align=True)
                col.label(text="LOD Visibility", icon='MOD_DECIM')
                row_lod = col.row(align=True)
                row_lod.operator(WITCH_OT_ENTITY_lod_toggle.bl_idname, text="LOD0", icon='MESH_DATA').action = "_lod0"
                row_lod.operator(WITCH_OT_ENTITY_lod_toggle.bl_idname, text="LOD1", icon='MESH_DATA').action = "_lod1"
                row_lod.operator(WITCH_OT_ENTITY_lod_toggle.bl_idname, text="LOD2", icon='MESH_DATA').action = "_lod2"
                col.separator()
                col.label(text="Collision Visibility", icon='MOD_PHYSICS')
                col_row = col.row(align=True)
                col_row.operator(WITCH_OT_ENTITY_lod_toggle.bl_idname, text="Hide", icon='HIDE_ON').action = "_collisionHide"
                col_row.operator(WITCH_OT_ENTITY_lod_toggle.bl_idname, text="Show", icon='HIDE_OFF').action = "_collisionShow"
                col.separator()
                col.label(text="Cloth Simulation", icon='PHYSICS')
                cloth_row = col.row(align=True)
                cloth_row.operator("witcher.toggle_cloth_simulation", text="Hide", icon='HIDE_ON').show = False
                cloth_row.operator("witcher.toggle_cloth_simulation", text="Show", icon='HIDE_OFF').show = True

            body = section("witcher_tools_rig", "Rig Tools", 'ARMATURE_DATA')
            if body:
                col = body.column(align=True)
                col.label(text="Constraints", icon='CONSTRAINT')
                col.operator(WITCH_OT_AddConstraints.bl_idname, text="Add Constraints", icon='CONSTRAINT').action = "add_const"
                col.operator(WITCH_OT_AddConstraints.bl_idname, text="Add Constraints IK", icon='CONSTRAINT').action = "add_const_ik"
                col.operator(WITCH_OT_AddConstraints.bl_idname, text="Attach to r_weapon", icon='CONSTRAINT').action = "attach_r_weapon"
                col.operator(WITCH_OT_AddConstraints.bl_idname, text="Attach to l_weapon", icon='CONSTRAINT').action = "attach_l_weapon"
                col.separator()
                col.label(text="Rig Orientation (Rot90)", icon='BONE_DATA')
                addon_prefs = get_all_addon_prefs(context)
                if hasattr(addon_prefs, "do_fix_tail"):
                    col.prop(addon_prefs, "do_fix_tail", text="Default On Import")
                obj = context.active_object
                armature = None
                if obj and obj.type == 'ARMATURE':
                    armature = obj
                elif obj and obj.parent and obj.parent.type == 'ARMATURE':
                    armature = obj.parent
                if armature and hasattr(armature.data, 'witcherui_RigSettings'):
                    rig_settings = armature.data.witcherui_RigSettings
                    rot90_on = bool(getattr(rig_settings, "rot90_imported", False))
                    col.label(text=f"Rig: {armature.name}  ({'Display Fix ON' if rot90_on else 'Display Fix OFF'})")
                    col.operator(
                        "witcher.toggle_rot90",
                        text="Remove Display Fix" if rot90_on else "Apply Display Fix",
                        icon='BONE_DATA'
                    )
                else:
                    col.label(text="Select an armature to toggle Rot90.", icon='INFO')

                col.separator()
                merge_box = col.box()
                merge_col = merge_box.column(align=True)
                merge_col.label(text="Hierarchy Merge", icon='ARMATURE_DATA')
                merge_col.label(text="Select all armatures/empties first.", icon='RESTRICT_SELECT_OFF')
                merge_col.operator(
                    WITCH_OT_merge_armature_hierarchy.bl_idname,
                    text="Merge Armature Hierarchy",
                    icon='ARMATURE_DATA'
                )

        # ══ SETTINGS TAB ══════════════════════════════════════════
        elif tab == "SETTINGS":
            body = section("witcher_settings_cache", "Cache Management", 'FILE_FOLDER')
            if body:
                _draw_cache_management_table(body)

            body = section("witcher_settings_cache_export", "Export Counts / Bulk Export", 'EXPORT', default_closed=True)
            if body:
                ui_cache_export.draw_export_stats_ui(body, context)

            body = section("witcher_settings_import_opts", "Import Options", 'IMPORT', default_closed=True)
            if body:
                ui_cache_export.draw_import_options_ui(body, context)

            body = section("witcher_settings_ext_paths", "External Paths/Addons", 'FILE_FOLDER', default_closed=True)
            if body:
                addon_prefs = get_all_addon_prefs(context)
                _draw_external_path_sections(body, addon_prefs, section_prefix="witcher_settings_extpaths")
                ui_cache_export.draw_addon_status_ui(body, context)

            body = section("witcher_settings_about", "About", 'INFO', default_closed=True)
            if body:
                col = body.column(align=True)
                version_text = ".".join(str(part) for part in bl_info.get("version", ()))
                col.label(text=f"Witcher 3 Tools v{version_text}")
                col.label(text=f"Author: {bl_info.get('author', 'Unknown')}")
                doc_url = bl_info.get("doc_url", "") or "https://github.com/dingdio/Witcher3_Blender_Tools"
                row = col.row(align=True)
                row.operator("wm.url_open", text="GitHub", icon='URL').url = doc_url
                col.label(text="Settings also live in Add-on Preferences", icon='PREFERENCES')


class WITCH_PT_ExternalPaths(WITCH_PT_Base, bpy.types.Panel):
    bl_label = "External Paths"
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        # Moved into Utilities / Settings.
        return False

    def draw_header(self, context):
        self.layout.label(text="", icon='FILE_FOLDER')

    # Reorganized path shortcuts into collapsible inspector sections so common paths stay on top.
    def draw(self, context):
        layout = self.layout
        addon_prefs = context.preferences.addons[ADDON_NAME].preferences
        _draw_external_path_sections(layout, addon_prefs, section_prefix="witcher_extpaths_legacy")

class WITCH_PT_Quick(WITCH_PT_Base, bpy.types.Panel):
    bl_label = "Quick Animation Import (Legacy)"
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        # Quick animation UI is now nested under Character Appearances > Animations.
        return False

    def draw(self, context):
        pass


# Cache management operators
class WITCHER_OT_cache_info(bpy.types.Operator):
    bl_idname = "witcher.cache_info"
    bl_label = "Cache Item Info"
    bl_options = {'INTERNAL'}

    cache_name: bpy.props.StringProperty()

    @classmethod
    def description(cls, context, properties):
        cache_name = getattr(properties, "cache_name", "")
        return f"{_get_cache_group_label(cache_name)}: {_get_cache_description(cache_name)}"

    def execute(self, context):
        label = _get_cache_label(self.cache_name)
        group = _get_cache_group_label(self.cache_name)
        detail = _get_cache_description(self.cache_name)
        self.report({'INFO'}, f"{group} | {label}: {detail}")
        return {'FINISHED'}


class WITCHER_OT_check_cache(bpy.types.Operator):
    bl_idname = "witcher.check_cache"
    bl_label = "Check Cache"
    cache_name: bpy.props.StringProperty()

    @classmethod
    def description(cls, context, properties):
        cache_name = getattr(properties, "cache_name", "")
        return f"{_get_cache_group_label(cache_name)} - Check {_get_cache_label(cache_name)}: {_get_cache_description(cache_name)}"

    def execute(self, context):
        status, detail = _check_cache_status(self.cache_name)
        CACHE_STATUS[self.cache_name] = {
            "status": status,
            "detail": detail,
            "checked_at": int(time.time())
        }
        self.report({'INFO'}, f"{_get_cache_label(self.cache_name)}: {detail}")
        return {'FINISHED'}


class WITCHER_OT_refresh_cache_checked(bpy.types.Operator):
    bl_idname = "witcher.refresh_cache_checked"
    bl_label = "Refresh Cache (Smart)"
    cache_name: bpy.props.StringProperty()
    _status: str = ""

    @classmethod
    def description(cls, context, properties):
        cache_name = getattr(properties, "cache_name", "")
        return f"{_get_cache_group_label(cache_name)} - Refresh {_get_cache_label(cache_name)} if needed. {_get_cache_description(cache_name)}"

    def invoke(self, context, event):
        status, detail = _check_cache_status(self.cache_name)
        self._status = status
        CACHE_STATUS[self.cache_name] = {
            "status": status,
            "detail": detail,
            "checked_at": int(time.time())
        }
        if status in {"stale", "unknown"}:
            return context.window_manager.invoke_confirm(self, event)
        if status == "ok":
            self.report({'INFO'}, f"{_get_cache_label(self.cache_name)}: {detail}")
            return {'FINISHED'}
        # missing or other: rebuild directly
        return self.execute(context)

    def execute(self, context):
        label = _get_cache_label(self.cache_name)
        status, detail = _check_cache_status(self.cache_name)
        if self._status:
            status = self._status
        if status == "ok":
            self.report({'INFO'}, f"{label}: {detail}")
            return {'FINISHED'}

        if _refresh_cache_by_name(self.cache_name):
            CACHE_STATUS[self.cache_name] = {
                "status": "ok",
                "detail": "Refreshed",
                "checked_at": int(time.time())
            }
            self.report({'INFO'}, f"Refreshed {label}")
            return {'FINISHED'}

        self.report({'WARNING'}, f"No refresh action for {label}")
        return {'CANCELLED'}


class WITCHER_OT_delete_cache(bpy.types.Operator):
    bl_idname = "witcher.delete_cache"
    bl_label = "Delete Cache"
    cache_name: bpy.props.StringProperty()

    @classmethod
    def description(cls, context, properties):
        cache_name = getattr(properties, "cache_name", "")
        return f"{_get_cache_group_label(cache_name)} - Delete {_get_cache_label(cache_name)}. {_get_cache_description(cache_name)}"

    def execute(self, context):
        label = _get_cache_label(self.cache_name)
        if _delete_cache_by_name(self.cache_name):
            CACHE_STATUS[self.cache_name] = {
                "status": "missing",
                "detail": "Deleted",
                "checked_at": int(time.time())
            }
            self.report({'INFO'}, f"Deleted {label}")
        else:
            self.report({'WARNING'}, f"{label} does not exist")
        return {'FINISHED'}

from .CR2W.witcher_cache.CollisionCache import CollisionManager
from .CR2W.witcher_cache.Bundles import BundleManager
from .CR2W.witcher_cache.Speech import SpeechManager
from .CR2W.witcher_cache.TextureCache import TextureManager
from .CR2W.witcher_cache.W3Strings import W3StringManager

# Operator to refresh a cache file
class WITCHER_OT_refresh_cache(bpy.types.Operator):
    bl_idname = "witcher.refresh_cache"
    bl_label = "Refresh Cache"
    cache_name: bpy.props.StringProperty()

    @classmethod
    def description(cls, context, properties):
        cache_name = getattr(properties, "cache_name", "")
        return f"{_get_cache_group_label(cache_name)} - Refresh {_get_cache_label(cache_name)}. {_get_cache_description(cache_name)}"

    def execute(self, context):
        label = _get_cache_label(self.cache_name)
        if _refresh_cache_by_name(self.cache_name):
            CACHE_STATUS[self.cache_name] = {
                "status": "ok",
                "detail": "Refreshed",
                "checked_at": int(time.time())
            }
            self.report({'INFO'}, f"Refreshed {label}")
            return {'FINISHED'}
        self.report({'WARNING'}, f"No refresh action for {label}")
        return {'CANCELLED'}


from bpy.utils import (register_class, unregister_class)

_classes = [
    #ent_import
    WITCH_OT_morphs,
    WITCH_OT_w2L,
    WITCH_OT_w2w,
    # WITCH_OT_w2mi,
    # WITCH_OT_w2mg,
    #WITCH_OT_w2ent,
    WITCH_OT_radish_w2L,
    WITCH_OT_export_textures,
    #anims
    WITCH_OT_AddConstraints,
    #WITCH_OT_ImportW2Rig,
    # WITCH_OT_ExportW2RigJson,
    # WITCH_OT_ExportW2AnimJson,
    WITCH_OT_ViewportNormals,
    WITCH_OT_ToggleClothSimulation,
    WITCH_OT_load_layer,
    WITCH_OT_load_layer_group,
    WITCH_OT_load_texarray,
    WITCHER_OT_open_external_path,
    WITCHER_OT_open_addon_preferences,
    WITCHER_OT_dismiss_external_import_alert,
    WITCHER_OT_select_terrain_tiles,
    WITCHER_OT_apply_fullmap_multires,
    WITCHER_OT_apply_terrain_material_values,
    WITCH_OT_toggle_rot90,
    WITCH_OT_merge_armature_hierarchy,

    #panels
    WITCH_PT_Main,
    WITCH_PT_CMesh,
    WITCH_PT_Terrain,
    WITCH_PT_ExternalPaths,
    #WITCH_PT_Utils,
]

def register():
    bpy.utils.register_class(PathItem)
    bpy.utils.register_class(WITCHER_UL_path_list)
    bpy.utils.register_class(AddPathOperator)
    bpy.utils.register_class(RemovePathOperator)
    bpy.utils.register_class(AddRedkitProjectOperator)
    bpy.utils.register_class(RemoveRedkitProjectOperator)
    bpy.utils.register_class(WITCHER_OT_reset_browser_popup_width)
    bpy.utils.register_class(WITCHER_OT_autofind_w3_path)
    bpy.utils.register_class(WITCHER_OT_autofind_w2_path)
    bpy.utils.register_class(WITCHER_OT_open_pref_path)
    bpy.utils.register_class(WITCHER_OT_pref_help_popup)

    bpy.utils.register_class(Witcher3AddonPrefs)
    prefs = bpy.context.preferences.addons[ADDON_NAME].preferences
    _apply_dev_pref_overrides(prefs)
    # Apply logging levels after programmatic dev overrides because property
    # update callbacks do not fire when set via setattr().
    try:
        _update_verbose_logging(prefs, bpy.context)
    except Exception:
        pass
    _auto_initialize_game_and_audio_paths(prefs, bpy.context)
    bpy.types.Scene.witcher_tools_tab = EnumProperty(
        name="Witcher Tools Tab",
        items=WITCHER_TOOLS_TABS,
        default='TOOLS'
    )
    armature_context.register()
    for cls in _classes:
        register_class(cls)
    ui_custom_icons.register()
    ui_entity.register()
    ui_material.register()
    ui_morphs.register()
    ui_texture_export.register()
    ui_import_menu.register()
    #ui_map.register()
    ui_anims.register()
    ui_speech.register()
    ui_scene.register()
    bpy.utils.register_class(WITCHER_OT_cache_info)
    bpy.utils.register_class(WITCHER_OT_check_cache)
    bpy.utils.register_class(WITCHER_OT_refresh_cache_checked)
    bpy.utils.register_class(WITCHER_OT_delete_cache)
    bpy.utils.register_class(WITCHER_OT_refresh_cache)
    ui_cache_export.register()
    register_class(WITCH_PT_Quick)
    ui_voice.register()
    ui_mimics.register()
    ui_re_anims.register()
    ui_anims_list.register()
    w3_material_nodes.register()
    w3_material_nodes_custom.register()
    w3_asset_browser.register()
    
    # Register new unified panel system
    unified_lists.register()
    unified_panels.register()
    
    # Register dev features only when the dev folder exists and dev_mode_enabled is true.
    try:
        from . import dev
        dev.register()
    except ImportError:
        pass  # Dev folder not present (production build)


def unregister():
    # Safe no-op when dev features were never registered.
    try:
        from . import dev
        dev.unregister()
    except ImportError:
        pass
    
    # Unregister new unified panel system
    unified_panels.unregister()
    unified_lists.unregister()
    
    #PATH LIST
    bpy.utils.unregister_class(RemoveRedkitProjectOperator)
    bpy.utils.unregister_class(AddRedkitProjectOperator)
    bpy.utils.unregister_class(RemovePathOperator)
    bpy.utils.unregister_class(AddPathOperator)
    bpy.utils.unregister_class(WITCHER_OT_autofind_w2_path)
    bpy.utils.unregister_class(WITCHER_OT_autofind_w3_path)
    bpy.utils.unregister_class(WITCHER_OT_reset_browser_popup_width)
    bpy.utils.unregister_class(WITCHER_OT_pref_help_popup)
    bpy.utils.unregister_class(WITCHER_OT_open_pref_path)
    bpy.utils.unregister_class(WITCHER_UL_path_list)
    bpy.utils.unregister_class(PathItem)

    w3_asset_browser.unregister()
    w3_material_nodes_custom.unregister()
    unregister_class(WITCH_PT_Quick)
    ui_cache_export.unregister()
    bpy.utils.unregister_class(WITCHER_OT_refresh_cache)
    bpy.utils.unregister_class(WITCHER_OT_delete_cache)
    bpy.utils.unregister_class(WITCHER_OT_refresh_cache_checked)
    bpy.utils.unregister_class(WITCHER_OT_check_cache)
    bpy.utils.unregister_class(WITCHER_OT_cache_info)
    bpy.utils.unregister_class(Witcher3AddonPrefs)
    del bpy.types.Scene.witcher_tools_tab
    armature_context.unregister()
    for cls in _classes:
        unregister_class(cls)
    ui_import_menu.unregister()
    ui_texture_export.unregister()
    #ui_map.unregister()
    ui_scene.unregister()
    ui_speech.unregister()
    ui_anims.unregister()
    ui_material.unregister()
    ui_entity.unregister()
    ui_morphs.unregister()
    ui_voice.unregister()
    ui_mimics.unregister()
    ui_re_anims.unregister()
    ui_anims_list.unregister()
    w3_material_nodes.unregister()
    ui_custom_icons.unregister()

