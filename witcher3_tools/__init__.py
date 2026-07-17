import sys as _sys
import json
import os
import re
import shutil
from pathlib import Path

from .extension_paths import (
    get_audio_root,
    get_cache_root,
    get_extension_user_dir,
    get_redkit_working_root,
    get_temp_root,
    get_texture_root,
    get_uncook_root,
    get_w2_uncook_root,
)
from .lod_utils import lod_level_from_name

LEGACY_ADDON_NAME = "io_import_w2l"
ADDON_NAME = __package__ or __name__
WOLVENKIT_CLI_DOWNLOAD_URL = "https://github.com/WolvenKit/WolvenKit-7-nightly/releases/"
WOLVENKIT_CLI_RELEASES_API = "https://api.github.com/repos/WolvenKit/WolvenKit-7-nightly/releases"
RADISH_LIPSYNC_REDKIT_URL = "https://www.nexusmods.com/witcher3/mods/9914"
WWISE_DOWNLOAD_URL = "https://www.audiokinetic.com/en/download/"
WWISE_INSTALL_DOC_URL = "https://www.audiokinetic.com/en/library/wwise_launcher/?id=install_wwise_through_launcher&source=InstallGuide"

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
        return {}, [], []

    if not getattr(dev_config, "DEV_MODE_ENABLED", False):
        return {}, [], []

    defaults = getattr(dev_config, "ADDON_PREFS_DEFAULTS", {})
    if not isinstance(defaults, dict):
        defaults = {}

    redkit_projects = getattr(dev_config, "ADDON_PREFS_REDKIT_PROJECTS", [])
    if not isinstance(redkit_projects, list):
        redkit_projects = []

    unreal_projects = getattr(dev_config, "ADDON_PREFS_UNREAL_PROJECTS", [])
    if not isinstance(unreal_projects, list):
        unreal_projects = []

    return defaults, redkit_projects, unreal_projects

def _apply_dev_pref_overrides(prefs):
    """Apply dev-only defaults without overwriting existing user preferences."""
    defaults, redkit_projects, unreal_projects = _load_dev_pref_overrides()

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

    if unreal_projects and hasattr(prefs, "unreal_projects") and len(prefs.unreal_projects) == 0:
        for path in unreal_projects:
            if not path:
                continue
            item = prefs.unreal_projects.add()
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


def _get_mesh_export_material_entries(mesh_ob):
    """Return used material slot preview rows for the given mesh object."""
    mesh = getattr(mesh_ob, "data", None)
    polygons = getattr(mesh, "polygons", None)
    materials = getattr(mesh, "materials", None)
    if mesh is None or polygons is None or materials is None:
        return []

    desired_entries = []
    for mat_idx in sorted({poly.material_index for poly in polygons}):
        mat = materials[mat_idx] if 0 <= mat_idx < len(materials) else None
        desired_entries.append((mat_idx, mat.name if mat else "(empty)"))
    return desired_entries


def _get_mesh_group_export_material_entries(meshes):
    combined_entries = set()
    for mesh_ob in meshes or []:
        combined_entries.update(_get_mesh_export_material_entries(mesh_ob))
    return sorted(combined_entries, key=lambda entry: (entry[0], (entry[1] or "").lower()))


def _draw_mesh_used_material_entries(layout, entries):
    if not entries:
        layout.label(text="-")
        return

    for slot_index, material_name in entries:
        row = layout.row(align=True)
        split = row.split(factor=0.12, align=True)
        split.label(text=str(slot_index))
        split.label(text=material_name or "(empty)", icon='MATERIAL')


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

def get_radish_tools_path(context) -> str:
    addon_prefs = context.preferences.addons[ADDON_NAME].preferences
    return addon_prefs.radish_tools_path

def get_wwise_console_path(context) -> str:
    addon_prefs = context.preferences.addons[ADDON_NAME].preferences
    return getattr(addon_prefs, "wwise_console_path", "")

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


def _get_registered_addon_prefs(context=None):
    ctx = context or bpy.context
    addons = getattr(getattr(ctx, "preferences", None), "addons", None)
    if addons is None:
        return None
    for name in (ADDON_NAME, LEGACY_ADDON_NAME):
        try:
            addon = addons.get(name)
        except Exception:
            addon = None
        if addon is not None:
            return addon.preferences
    return None

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

def get_unify_character_armature(context) -> bool:
    try:
        addon_prefs = context.preferences.addons[ADDON_NAME].preferences
        return bool(getattr(addon_prefs, "premerge_character_armature", False))
    except Exception:
        return False

def get_import_physics_enabled(context) -> bool:
    try:
        addon_prefs = context.preferences.addons[ADDON_NAME].preferences
        return bool(getattr(addon_prefs, "import_physics_enabled", True))
    except Exception:
        return True

def get_rig_rot90_enabled(rig_settings, default=False):
    """Return whether the rig currently has rot90 applied."""
    if rig_settings is None:
        return bool(default)
    rot90_state = str(getattr(rig_settings, "rot90_state", "") or "").strip().upper()
    if rot90_state in {"ON", "TRUE", "1", "ENABLED"}:
        return True
    if rot90_state in {"OFF", "FALSE", "0", "DISABLED"}:
        return False
    if hasattr(rig_settings, "rot90_imported"):
        return bool(getattr(rig_settings, "rot90_imported", default))
    if hasattr(rig_settings, "rot90_compensate"):
        return bool(getattr(rig_settings, "rot90_compensate", default))
    return bool(default)

def set_rig_rot90_enabled(rig_settings, enabled: bool):
    """Set rot90 state on rig settings."""
    if rig_settings is None:
        return
    rig_settings.rot90_imported = bool(enabled)
    rig_settings.rot90_compensate = bool(enabled)
    if hasattr(rig_settings, "rot90_state"):
        rig_settings.rot90_state = "ON" if enabled else "OFF"


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
from .rigging import constraints as constrain_util
from . import file_helpers


#ui
from .ui import ui_custom_icons
from .ui import ui_map
from .ui.ui_map import (WITCH_OT_w2L,
                                     WITCH_OT_w2w,
                                     WITCH_OT_import_world_tile,
                                     WITCH_OT_w2l_collection_details,
                                     WITCH_OT_load_layer,
                                     WITCH_OT_load_layer_group,
                                     WITCH_OT_cancel_layer_stream_job,
                                     WITCH_OT_load_layers_around_camera,
                                     WITCH_OT_rebuild_layer_scan_cache,
                                     WITCH_OT_scan_layers_nearby,
                                     WITCH_OT_radish_w2L,
                                     WITCH_OT_export_textures,
                                     WITCH_OT_cancel_foliage_job,
                                     WITCH_OT_load_foliage_around_camera,
                                     WITCH_OT_toggle_foliage_visibility,
                                     WITCH_OT_unload_foliage,
                                     WITCH_OT_hydrate_foliage_sources,
                                     WITCH_OT_open_foliage_browser,
                                     WITCH_OT_check_foliage_world)
from .ui import ui_anims
from .ui import ui_speech
from .ui import ui_file_browser
from .ui import ui_entity
from .ui import ui_equipment
from .ui import ui_morphs
from .ui import ui_material
from .ui.ui_morphs import (WITCH_OT_morphs)

from .ui import ui_voice
from .ui import ui_mimics
from .ui import ui_re_anims
from .ui import ui_anims_list
from .ui import ui_texture_export
from .ui import ui_import_menu
from .ui import ui_dialog_language
from .ui import ui_cutscene
from .ui import ui_animated_component
from .ui import ui_scene
from .ui import ui_physics
from .ui import ui_environment
from .ui import armature_context
from .ui import ui_cache_export
from . import lipsync
from . import livelink_face
from . import strings_browser
from .ui.ui_mesh import (WITCH_OT_w2mesh, WITCH_OT_apx, WITCH_OT_redcloth, WITCH_OT_redapex, WITCH_OT_w2mesh_export, WITCH_OT_nxs,
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

from .materials import nodes as material_nodes
from . import w3_asset_browser
from . import unreal_export

import bpy
from bpy.types import (Panel, Operator)
from bpy.props import (
    StringProperty, BoolProperty, CollectionProperty, IntProperty, EnumProperty,
    FloatProperty,
)
from mathutils import Vector
from bpy_extras.io_utils import ImportHelper, ExportHelper
import addon_utils

bl_info = {
    "name": "Witcher 3 Tools",
    "author": "Dingdio",
    "version": (1, 1, 0),
    "blender": (4, 5, 0),
    "location": "File > Import-Export > Witcher 3 Assets",
    "description": "Tools for Witcher 3 and Witcher 2",
    "warning": "",
    "doc_url": "https://github.com/dingdio/Witcher3_Blender_Tools",
    "category": "Import-Export"
}

def _get_addon_about_info():
    info = globals().get("bl_info")
    if isinstance(info, dict):
        version = info.get("version", ())
        version_text = ".".join(str(part) for part in version)
        return {
            "version": version_text,
            "author": info.get("author", "Unknown"),
            "doc_url": info.get("doc_url", "") or "https://github.com/dingdio/Witcher3_Blender_Tools",
        }

    manifest_path = os.path.join(os.path.dirname(__file__), "blender_manifest.toml")
    manifest_info = {
        "version": "",
        "author": "Unknown",
        "doc_url": "https://github.com/dingdio/Witcher3_Blender_Tools",
    }
    try:
        with open(manifest_path, "r", encoding="utf-8") as manifest_file:
            for line in manifest_file:
                key, separator, value = line.partition("=")
                if not separator:
                    continue
                key = key.strip()
                value = value.strip().strip('"')
                if key == "version":
                    manifest_info["version"] = value
                elif key == "maintainer":
                    manifest_info["author"] = value
                elif key == "website":
                    manifest_info["doc_url"] = value
    except OSError:
        pass

    return manifest_info

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


def _default_w2_uncook_path():
    return get_w2_uncook_root(create=True)


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
    if not getattr(prefs, "w2_unbundle_path", ""):
        prefs.w2_unbundle_path = _default_w2_uncook_path()
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


def ensure_witcher2_game_path_initialized(context) -> bool:
    prefs = get_all_addon_prefs(context)
    _auto_initialize_game_and_audio_paths(prefs, context)
    return not bool(get_witcher2_game_path_issue(context))


def _invalidate_dlc_mounter_settings(_self=None, _context=None):
    try:
        from .importers import dlc_mounters

        dlc_mounters.clear_dlc_mounter_cache()
    except Exception:
        pass


def _reset_dlc_mounter_sources(_self=None, context=None):
    try:
        prefs = get_all_addon_prefs(context or bpy.context)
        if hasattr(prefs, "dlc_mounter_sources"):
            prefs.dlc_mounter_sources.clear()
            prefs.dlc_mounter_sources_index = 0
            prefs.redkit_dlc_mounter_sources_index = 0
    except Exception:
        pass
    _invalidate_dlc_mounter_settings(_self, context)


class PathItem(bpy.types.PropertyGroup):
    path: StringProperty(
        name="Path",
        subtype='DIR_PATH',
        description="A directory path",
        update=_reset_dlc_mounter_sources,
    )


class UnrealProjectItem(bpy.types.PropertyGroup):
    path: StringProperty(
        name="Project",
        subtype='FILE_PATH',
        description="Unreal .uproject file",
    )


class DlcMounterSourceItem(bpy.types.PropertyGroup):
    enabled: BoolProperty(
        name="Enabled",
        default=True,
        description="Use this DLC definition when applying DLC mounters",
        update=_invalidate_dlc_mounter_settings,
    )
    key: StringProperty(default="", options={'HIDDEN'})
    source_id: StringProperty(default="", options={'HIDDEN'})
    source_kind: StringProperty(default="", options={'HIDDEN'})
    is_vanilla: BoolProperty(default=False, options={'HIDDEN'})
    source_label: StringProperty(name="Source", default="")
    dlc_id: StringProperty(name="DLC ID", default="")
    dlc_name: StringProperty(name="DLC", default="")
    dlc_description: StringProperty(name="Description", default="")
    dlc_name_key: StringProperty(name="Name Key", default="")
    dlc_description_key: StringProperty(name="Description Key", default="")
    dlc_folder_name: StringProperty(name="Folder Name", default="")
    mounter_types: StringProperty(name="Mounters", default="")
    root_path: StringProperty(name="Source Root", subtype='DIR_PATH', default="")
    dlc_dir: StringProperty(name="DLC Folder", subtype='DIR_PATH', default="")
    reddlc_path: StringProperty(name=".reddlc", subtype='FILE_PATH', default="")


def _dlc_mounter_item_is_redkit(item) -> bool:
    source_kind = str(getattr(item, "source_kind", "") or "").lower()
    source_id = str(getattr(item, "source_id", "") or "").lower()
    return source_kind == "redkit" or "redkit" in source_id


def _dlc_mounter_item_icon(item):
    if _dlc_mounter_item_is_redkit(item):
        return 'TOOL_SETTINGS'
    if bool(getattr(item, "is_vanilla", False)):
        return 'FILE'
    return 'MODIFIER'


def _dlc_mounter_folder_key(item):
    source_id = str(getattr(item, "source_id", "") or "").strip().lower()
    dlc_dir = str(getattr(item, "dlc_dir", "") or "").replace("/", "\\").strip()
    if not dlc_dir:
        dlc_dir = str(getattr(item, "dlc_folder_name", "") or "").strip()
    try:
        dlc_dir = os.path.normcase(os.path.normpath(dlc_dir)).lower()
    except Exception:
        dlc_dir = dlc_dir.lower()
    return source_id, dlc_dir


def _dlc_mounter_reddlc_filename(item) -> str:
    reddlc_path = str(getattr(item, "reddlc_path", "") or "").replace("/", "\\").strip()
    if not reddlc_path:
        return ""
    return reddlc_path.rsplit("\\", 1)[-1]


def _dlc_mounter_folder_has_multiple_reddlc(data, propname, item) -> bool:
    collection = getattr(data, propname, None)
    if collection is None or isinstance(collection, (str, bytes)):
        return False
    target_key = _dlc_mounter_folder_key(item)
    filenames = set()
    try:
        iterator = iter(collection)
    except TypeError:
        return False
    for other in iterator:
        if _dlc_mounter_folder_key(other) != target_key:
            continue
        filename = _dlc_mounter_reddlc_filename(other)
        if filename:
            filenames.add(filename.lower())
        if len(filenames) > 1:
            return True
    return False


def _dlc_mounter_item_label(item, data=None, propname="") -> str:
    folder_name = str(getattr(item, "dlc_folder_name", "") or "").strip()
    if not folder_name:
        folder_name = os.path.basename(os.path.normpath(str(getattr(item, "dlc_dir", "") or "")))
    dlc_name = str(getattr(item, "dlc_name", "") or "").strip()
    if not folder_name:
        return dlc_name or "DLC"
    parts = [folder_name]
    if data is not None and propname and _dlc_mounter_folder_has_multiple_reddlc(data, propname, item):
        reddlc_filename = _dlc_mounter_reddlc_filename(item)
        if reddlc_filename:
            parts.append(f"({reddlc_filename})")
    if dlc_name and dlc_name.lower() != folder_name.lower():
        parts.append(f"({dlc_name})")
    return " ".join(parts)


def _draw_dlc_mounter_source_item(layout, data, propname, item, index):
    row = layout.row(align=True)
    row.prop(item, "enabled", text="")
    details = row.operator("witcher.dlc_mounter_source_details", text="", icon='INFO')
    details.index = index
    row.label(text="", icon=_dlc_mounter_item_icon(item))
    row.label(text=_dlc_mounter_item_label(item, data, propname))


def _filter_dlc_mounter_source_items(ui_list, data, propname, show_redkit: bool):
    items = getattr(data, propname)
    flags = []
    for item in items:
        is_redkit = _dlc_mounter_item_is_redkit(item)
        flags.append(ui_list.bitflag_filter_item if is_redkit == show_redkit else 0)
    return flags, []


class WITCHER_UL_game_dlc_mounter_sources(bpy.types.UIList):
    def filter_items(self, context, data, propname):
        return _filter_dlc_mounter_source_items(self, data, propname, show_redkit=False)

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        _draw_dlc_mounter_source_item(layout, data, "dlc_mounter_sources", item, index)


class WITCHER_UL_redkit_dlc_mounter_sources(bpy.types.UIList):
    def filter_items(self, context, data, propname):
        return _filter_dlc_mounter_source_items(self, data, propname, show_redkit=True)

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        _draw_dlc_mounter_source_item(layout, data, "dlc_mounter_sources", item, index)


class WITCHER_OT_dlc_mounter_source_details(bpy.types.Operator):
    bl_idname = "witcher.dlc_mounter_source_details"
    bl_label = "DLC Details"
    bl_description = "Show details for the selected DLC definition"
    bl_options = {'INTERNAL'}

    index: IntProperty(default=-1, options={'HIDDEN', 'SKIP_SAVE'})
    dlc_id: StringProperty(name="DLC ID", options={'SKIP_SAVE'})
    dlc_name: StringProperty(name="Name", options={'SKIP_SAVE'})
    dlc_description: StringProperty(name="Description", options={'SKIP_SAVE'})
    dlc_name_key: StringProperty(name="Name Key", options={'SKIP_SAVE'})
    dlc_description_key: StringProperty(name="Description Key", options={'SKIP_SAVE'})
    dlc_folder_name: StringProperty(name="Folder", options={'SKIP_SAVE'})
    source_label: StringProperty(name="Source", options={'SKIP_SAVE'})
    mounter_types: StringProperty(name="Mounters", options={'SKIP_SAVE'})
    reddlc_path: StringProperty(name=".reddlc", subtype='FILE_PATH', options={'SKIP_SAVE'})
    dlc_dir: StringProperty(name="DLC Folder", subtype='DIR_PATH', options={'SKIP_SAVE'})
    root_path: StringProperty(name="Source Root", subtype='DIR_PATH', options={'SKIP_SAVE'})

    def _copy_from_item(self, item):
        self.dlc_id = str(getattr(item, "dlc_id", "") or "")
        self.dlc_name = str(getattr(item, "dlc_name", "") or "")
        self.dlc_description = str(getattr(item, "dlc_description", "") or "")
        self.dlc_name_key = str(getattr(item, "dlc_name_key", "") or "")
        self.dlc_description_key = str(getattr(item, "dlc_description_key", "") or "")
        self.dlc_folder_name = str(getattr(item, "dlc_folder_name", "") or "")
        if not self.dlc_folder_name:
            self.dlc_folder_name = os.path.basename(os.path.normpath(str(getattr(item, "dlc_dir", "") or "")))
        self.source_label = str(getattr(item, "source_label", "") or "")
        self.mounter_types = str(getattr(item, "mounter_types", "") or "")
        self.reddlc_path = str(getattr(item, "reddlc_path", "") or "")
        self.dlc_dir = str(getattr(item, "dlc_dir", "") or "")
        self.root_path = str(getattr(item, "root_path", "") or "")

    def invoke(self, context, event):
        prefs = get_all_addon_prefs(context)
        index = int(self.index if self.index >= 0 else getattr(prefs, "dlc_mounter_sources_index", 0) or 0)
        if index < 0 or index >= len(getattr(prefs, "dlc_mounter_sources", []) or []):
            self.report({'WARNING'}, "No DLC selected.")
            return {'CANCELLED'}
        self._copy_from_item(prefs.dlc_mounter_sources[index])
        return context.window_manager.invoke_props_dialog(self, width=620)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        col = layout.column(align=True)
        col.prop(self, "dlc_name")
        col.prop(self, "dlc_description")
        col.prop(self, "dlc_name_key")
        col.prop(self, "dlc_description_key")
        col.prop(self, "dlc_id")
        col.prop(self, "dlc_folder_name")
        col.prop(self, "source_label")
        col.prop(self, "mounter_types")
        col.prop(self, "reddlc_path")
        col.prop(self, "dlc_dir")
        col.prop(self, "root_path")

    def execute(self, context):
        return {'FINISHED'}


class WITCHER_OT_refresh_dlc_mounter_sources(bpy.types.Operator):
    bl_idname = "witcher.refresh_dlc_mounter_sources"
    bl_label = "Refresh DLC List"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        try:
            from .importers import dlc_mounters

            count = dlc_mounters.sync_dlc_mounter_sources(context)
        except Exception as exc:
            self.report({'ERROR'}, f"Failed to refresh DLC list: {exc}")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Found {count} DLC definition file(s).")
        return {'FINISHED'}


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


class AddUnrealProjectOperator(bpy.types.Operator):
    bl_idname = "witcher.add_unreal_project"
    bl_label = "Add Unreal Project"

    def execute(self, context):
        addon_prefs = get_all_addon_prefs(context)
        new_item = addon_prefs.unreal_projects.add()
        new_item.path = ""
        return {'FINISHED'}


class RemoveUnrealProjectOperator(bpy.types.Operator):
    bl_idname = "witcher.remove_unreal_project"
    bl_label = "Remove Unreal Project"

    def execute(self, context):
        addon_prefs = get_all_addon_prefs(context)
        index = addon_prefs.unreal_projects_index
        if 0 <= index < len(addon_prefs.unreal_projects):
            addon_prefs.unreal_projects.remove(index)
            if index >= len(addon_prefs.unreal_projects):
                addon_prefs.unreal_projects_index = len(addon_prefs.unreal_projects) - 1
        return {'FINISHED'}


class WITCHER_OT_reset_browser_popup_width(bpy.types.Operator):
    """Reset the asset browser popup width to the default (50% of window)"""
    bl_idname = "witcher.reset_browser_popup_width"
    bl_label = "Reset to Default"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        prefs = get_all_addon_prefs(context)
        prefs.browser_popup_width = 0
        prefs.browser_popup_width_percent = 50
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


def _safe_storage_name(value, fallback="download"):
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip()).strip("._-")
    return name or fallback


def _request_github_json(url):
    import json
    import urllib.request

    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "witcher3-blender-tools",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def _select_wolvenkit_nightly_asset(assets):
    candidates = []
    for asset in assets or []:
        name = str(asset.get("name") or "").strip()
        url = str(asset.get("browser_download_url") or "").strip()
        if not name.lower().endswith(".zip") or not url:
            continue
        lower = name.lower()
        score = 0
        if "wolvenkit" in lower:
            score -= 20
        if "nightly" in lower:
            score -= 10
        if "cli" in lower:
            score -= 2
        if any(token in lower for token in ("source", "symbols", "pdb", "debug")):
            score += 50
        candidates.append((score, name.casefold(), asset))
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][2] if candidates else None


def _find_latest_wolvenkit_nightly_release():
    releases = _request_github_json(WOLVENKIT_CLI_RELEASES_API)
    if not isinstance(releases, list):
        raise RuntimeError("GitHub releases response was not a list.")
    for release in releases:
        if release.get("draft"):
            continue
        asset = _select_wolvenkit_nightly_asset(release.get("assets") or [])
        if asset:
            return release, asset
    raise RuntimeError("No WolvenKit nightly zip asset was found in GitHub releases.")


def _download_file(url, destination, expected_size=0, progress_callback=None):
    import urllib.request

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination.with_name(destination.name + ".download")
    if temp_path.exists():
        temp_path.unlink()

    request = urllib.request.Request(
        url,
        headers={"User-Agent": "witcher3-blender-tools"},
    )
    downloaded = 0
    with urllib.request.urlopen(request, timeout=60) as response:
        total = int(response.headers.get("Content-Length") or expected_size or 0)
        with temp_path.open("wb") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                downloaded += len(chunk)
                if progress_callback:
                    progress_callback(downloaded, total)

    if expected_size and downloaded != int(expected_size):
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise RuntimeError(f"Downloaded size mismatch: expected {expected_size}, got {downloaded}.")

    temp_path.replace(destination)
    return destination


def _assert_path_inside(path, root):
    path = Path(path).resolve(strict=False)
    root = Path(root).resolve(strict=False)
    if path == root or root in path.parents:
        return path
    raise RuntimeError(f"Unsafe path outside extension storage: {path}")


def _remove_tree_inside(path, root):
    path = _assert_path_inside(path, root)
    root = Path(root).resolve(strict=False)
    if path == root:
        raise RuntimeError(f"Refusing to remove storage root: {root}")
    if path.exists():
        shutil.rmtree(path)


def _extract_zip_safe(zip_path, destination, progress_callback=None):
    import zipfile

    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    destination_resolved = destination.resolve(strict=False)
    with zipfile.ZipFile(zip_path, "r") as archive:
        members = archive.infolist()
        total = len(members)
        for index, member in enumerate(members, 1):
            member_path = destination / member.filename
            _assert_path_inside(member_path, destination_resolved)
            archive.extract(member, destination)
            if progress_callback:
                progress_callback(index, total)


def _find_wolvenkit_cli(root):
    candidates = sorted(
        (item for item in Path(root).rglob("*.exe") if item.name.casefold() == "wolvenkit.cli.exe"),
        key=lambda item: (len(item.parts), str(item).casefold()),
    )
    return candidates[0] if candidates else None


class WITCHER_OT_download_wolvenkit_cli_nightly(bpy.types.Operator):
    bl_idname = "witcher.download_wolvenkit_cli_nightly"
    bl_label = "Install WolvenKit Nightly"
    bl_description = "Download the latest WolvenKit-7 nightly zip, extract it into extension storage, and set WolvenKit.CLI.exe"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        addon_prefs = get_all_addon_prefs(context)
        wm = context.window_manager

        try:
            wm.progress_begin(0, 100)
        except Exception:
            pass

        try:
            release, asset = _find_latest_wolvenkit_nightly_release()
            tag = _safe_storage_name(
                release.get("tag_name") or release.get("name") or release.get("published_at"),
                fallback="latest",
            )
            asset_name = _safe_storage_name(asset.get("name"), fallback="WolvenKit-nightly.zip")
            storage_root = Path(get_extension_user_dir(create=True)) / "external_tools" / "wolvenkit_7_nightly"
            downloads_dir = storage_root / "downloads"
            install_dir = storage_root / tag
            staging_dir = storage_root / f".{tag}.extracting"
            zip_path = downloads_dir / asset_name

            def download_progress(downloaded, total):
                if not total:
                    return
                try:
                    wm.progress_update(max(1, min(70, int((downloaded / total) * 70))))
                except Exception:
                    pass

            _download_file(
                asset.get("browser_download_url"),
                zip_path,
                expected_size=int(asset.get("size") or 0),
                progress_callback=download_progress,
            )

            storage_root.mkdir(parents=True, exist_ok=True)
            _remove_tree_inside(staging_dir, storage_root)
            staging_dir.mkdir(parents=True, exist_ok=True)

            def extract_progress(done, total):
                if not total:
                    return
                try:
                    wm.progress_update(70 + max(1, min(25, int((done / total) * 25))))
                except Exception:
                    pass

            _extract_zip_safe(zip_path, staging_dir, progress_callback=extract_progress)
            cli_path = _find_wolvenkit_cli(staging_dir)
            if not cli_path:
                raise RuntimeError("Downloaded nightly did not contain WolvenKit.CLI.exe.")

            _remove_tree_inside(install_dir, storage_root)
            staging_dir.replace(install_dir)
            final_cli_path = install_dir / cli_path.relative_to(staging_dir)
            if not final_cli_path.is_file():
                raise RuntimeError("WolvenKit.CLI.exe was not found after install.")

            addon_prefs.wolvenkit = str(final_cli_path)
            try:
                wm.progress_update(100)
            except Exception:
                pass
            self.report({'INFO'}, f"WolvenKit CLI set: {final_cli_path}")
            return {'FINISHED'}
        except Exception as exc:
            self.report({'ERROR'}, str(exc).splitlines()[0][:240])
            return {'CANCELLED'}
        finally:
            try:
                wm.progress_end()
            except Exception:
                pass


class WITCHER_OT_autofind_wwise_console(bpy.types.Operator):
    bl_idname = "witcher.autofind_wwise_console"
    bl_label = "Auto Find Wwise Console"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        addon_prefs = get_all_addon_prefs(context)
        try:
            from .lipsync import radish_runner as _lipsync_radish_runner
            detected_path = _lipsync_radish_runner.auto_detect_wwise_console(
                tools_dir=getattr(addon_prefs, "radish_tools_path", ""),
            )
        except Exception as exc:
            self.report({'ERROR'}, f"Could not auto-find Wwise Console: {exc}")
            return {'CANCELLED'}

        if not detected_path:
            self.report({'WARNING'}, "Could not auto-find Wwise 2021.1.x.")
            return {'CANCELLED'}

        addon_prefs.wwise_console_path = str(detected_path)
        self.report({'INFO'}, f"Wwise Console set: {detected_path}")
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

        if topic == "radish_tools":
            return {
                "title": "Radish Lipsync 4 REDkit",
                "icon": 'TOOL_SETTINGS',
                "lines": [
                    "Set this to the radish-tools folder from Radish Lipsync 4 REDkit.",
                    "The normal Radish Modding Tools package is not enough for WAV lipsync.",
                    "It must contain w3speech phoneme, creator, converter, and repo.lipsync files.",
                    "Download: Nexus Mods mod 9914.",
                ],
                "warnings": [],
            }

        if topic == "wolvenkit_cli":
            return {
                "title": "WolvenKit 7 CLI",
                "icon": 'CONSOLE',
                "lines": [
                    "Set this to WolvenKit.CLI.exe.",
                    "The Install Nightly button downloads the newest WolvenKit-7 nightly zip.",
                    "It extracts into this extension's user storage and sets this path automatically.",
                    "Manual download: WolvenKit/WolvenKit-7-nightly releases on GitHub.",
                ],
                "warnings": [],
            }

        if topic == "wwise_console":
            return {
                "title": "Wwise Console",
                "icon": 'SOUND',
                "lines": [
                    "WEM generation requires Audiokinetic Wwise 2021.1.x.",
                    "Radish docs recommend v2021.1.14 for REDkit lipsync.",
                    "Set this to WwiseConsole.exe or its Authoring x64 Release bin folder.",
                    "If empty, the add-on checks Radish _settings_.bat, environment variables, PATH, and Program Files.",
                    "vgmstream can decode .wem files, but it cannot encode them.",
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
        if topic == "radish_tools":
            return "Where the Radish Lipsync 4 REDkit w3speech tools are installed."
        if topic == "wolvenkit_cli":
            return "Download or set WolvenKit.CLI.exe for CR2W conversion."
        if topic == "wwise_console":
            return "Where WwiseConsole.exe is installed for REDkit-compatible .wem generation."
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
        description="Path where you uncooked the game files.",
        update=_reset_dlc_mounter_sources,
    )
    wolvenkit: StringProperty(
        name="Wolvenkit 7 CLI exe",
        subtype='FILE_PATH',
        default="",
        description="Wolvenkit .exe."
    )
    radish_tools_path: StringProperty(
        name="Radish Lipsync 4 REDkit",
        subtype='DIR_PATH',
        default="",
        description="radish-tools folder from Radish Lipsync 4 REDkit, not the normal Radish Modding Tools package."
    )
    wwise_console_path: StringProperty(
        name="Wwise Console",
        subtype='FILE_PATH',
        default="",
        description="Optional path to WwiseConsole.exe or its bin folder. WEM generation requires Wwise 2021.1.x; v2021.1.14 is recommended by Radish."
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
        description="Main REDkit depot (read-only).",
        update=_reset_dlc_mounter_sources,
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
    redkit_projects_index: IntProperty(update=_reset_dlc_mounter_sources)

    unreal_projects: CollectionProperty(type=UnrealProjectItem)
    unreal_projects_index: IntProperty()

    dlc_mounter_sources: CollectionProperty(type=DlcMounterSourceItem)
    dlc_mounter_sources_index: IntProperty()
    redkit_dlc_mounter_sources_index: IntProperty()

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
        name="Witcher 2 Uncook Path",
        subtype='DIR_PATH',
        default=_default_w2_uncook_path(),
        description="Extension storage folder for extracted Witcher 2 DZIP resources."
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
    premerge_character_armature: bpy.props.BoolProperty(
        name="Pre-merge character armature on import",
        default=False,
        description="Experimental: import supported character templates as one precomputed armature instead of per-part armatures with constraints."
    )
    import_physics_enabled: bpy.props.BoolProperty(
        name="Physics Enabled",
        default=True,
        description="Enable imported Dyng and Breast physics by default."
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

    # Entity import behaviour
    import_idle_animation: bpy.props.BoolProperty(
        name="Import Idle Animation",
        default=False,
        description=(
            "Automatically load default idle animation on this entity.\n"
            "(resolved from the entity's behaviour graph (.w2beh)"
            "Has no effect if no w2beh or animation set is found."
        ),
    )
    prefer_bundles_for_linked_assets: bpy.props.BoolProperty(
        name="Load Linked Assets from Bundles",
        default=False,
        description=(
            "When a .w2scene is loaded from a REDkit depot, resolve all linked assets "
            "(entity templates, meshes, textures, etc.) from the vanilla bundle/uncook "
            "instead of preferring REDkit paths."
        ),
    )
    read_dlc_mounters: bpy.props.BoolProperty(
        name="Read DLC Mounters",
        default=True,
        description=(
            "Global enable/disable for DLC mounters. When disabled, all configured "
            ".reddlc mounters are ignored during imports."
        ),
        update=_invalidate_dlc_mounter_settings,
    )
    do_replace_appearances: bpy.props.BoolProperty(
        name="Replace Appearances",
        default=False,
        description=(
            "Make DLC appearance mounters behave like the game by replacing the "
            "target appearance instead of adding the DLC appearance as a separate option."
        ),
        update=_invalidate_dlc_mounter_settings,
    )
    mesh_import_do_import_mats: bpy.props.BoolProperty(
        name="Mesh Import: Apply Materials",
        default=True,
        description="If enabled, materials will be imported.",
        options={'HIDDEN'},
    )
    mesh_import_do_import_armature: bpy.props.BoolProperty(
        name="Mesh Import: Import Armature",
        default=True,
        description="If enabled, the armature will be imported",
        options={'HIDDEN'},
    )
    mesh_import_keep_lod_meshes: bpy.props.BoolProperty(
        name="Mesh Import: Keep LODs",
        default=False,
        description="If enabled, it will keep low quality meshes and materials",
        options={'HIDDEN'},
    )
    mesh_import_keep_empty_lods: bpy.props.BoolProperty(
        name="Mesh Import: Keep Empty LODs",
        default=False,
        description="If enabled, it will keep empty mesh LODs with zero polygons",
        options={'HIDDEN'},
    )
    mesh_import_rotate_180: bpy.props.BoolProperty(
        name="Mesh Import: Rotate 180",
        default=False,
        description="Rotate both the mesh and the armature on the Z-axis by 180 degrees. Default is False",
        options={'HIDDEN'},
    )
    mesh_import_hide_zero_weight_faces: bpy.props.BoolProperty(
        name="Mesh Import: Hide Zero-Weight Faces",
        default=True,
        description="Hides faces without bones on skinned meshes. The default game behaviour",
        options={'HIDDEN'},
    )
    mesh_import_do_import_collision: bpy.props.BoolProperty(
        name="Mesh Import: Import Collision",
        default=False,
        description="Import collision shapes. Uncooked meshes use the embedded collision; cooked meshes look up the .nxs file in the collision cache",
        options={'HIDDEN'},
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
        default=False,
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
        name="Asset Browser Width (px)",
        description="Optional fixed width of the asset browser popup in pixels. Set to 0 to use the percentage-based width",
        default=0,
        min=0,
        max=3000,
    )
    browser_popup_width_percent: bpy.props.IntProperty(
        name="Asset Browser Width (%)",
        description="Default width of the asset browser popup as a percentage of the current monitor width when no pixel override is set",
        default=50,
        min=20,
        max=100,
        subtype='PERCENTAGE',
    )
    browser_file_page_size: bpy.props.IntProperty(
        name="Asset Browser Files Per Page",
        description="Optional fixed number of files shown per page in list mode. Set to 0 to auto-size from about 80% of monitor height",
        default=0,
        min=0,
        max=500,
    )
    strings_browser_page_size: bpy.props.IntProperty(
        name="Strings Browser Rows",
        description="Rows shown per page in the Strings Browser popup",
        default=1000,
        min=50,
        soft_max=100000,
    )
    browser_grid_max_rows: bpy.props.IntProperty(
        name="Asset Browser Grid Rows Per Page",
        description="Optional fixed number of full grid rows shown before pagination is used. Set to 0 to auto-size from about 80% of monitor height",
        default=0,
        min=0,
        max=12,
    )
    browser_grid_columns: bpy.props.IntProperty(
        name="Asset Browser Grid Columns",
        description="Number of grid tiles per row. Set to 0 to choose automatically from the browser width",
        default=0,
        min=0,
        max=8,
    )
    browser_grid_size_mode: bpy.props.EnumProperty(
        name="Default Icon Size",
        description="Default grid tile size preset used by the asset browser",
        items=[
            ('SMALL', 'Small', 'Compact grid tiles with more columns'),
            ('MEDIUM', 'Medium', 'Balanced grid tiles'),
            ('LARGE', 'Large', 'Larger grid tiles with wider filename rows'),
        ],
        default='MEDIUM',
    )
    browser_grid_tile_width: bpy.props.IntProperty(
        name="Asset Browser Grid Tile Width (px)",
        description="Legacy setting retained for compatibility. Grid sizing now uses the Small/Large preset instead",
        default=150,
        min=120,
        max=320,
    )
    browser_folder_panel_width_percent: bpy.props.IntProperty(
        name="Asset Browser Folder Panel Width (%)",
        description="Default width of the folder list panel as a percentage of the current monitor width when no pixel override is set",
        default=15,
        min=8,
        max=40,
        subtype='PERCENTAGE',
    )
    browser_folder_panel_width: bpy.props.IntProperty(
        name="Asset Browser Folder Panel Width (px)",
        description="Optional fixed width of the folder list panel in pixels. Set to 0 to use the percentage-based width",
        default=0,
        min=0,
        max=640,
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
        common_col.prop(self, "import_idle_animation")
        common_col.prop(self, "prefer_bundles_for_linked_assets")
        dlc_box = common_box.box()
        dlc_header = dlc_box.row(align=True)
        dlc_header.label(text="DLC Mounters", icon='FILE')
        dlc_box.prop(self, "read_dlc_mounters")
        dlc_controls = dlc_box.column(align=True)
        dlc_controls.enabled = bool(self.read_dlc_mounters)
        dlc_controls.prop(self, "do_replace_appearances")
        dlc_header = dlc_controls.row(align=True)
        dlc_header.operator("witcher.refresh_dlc_mounter_sources", text="Refresh", icon='FILE_REFRESH')
        dlc_controls.label(text="Bundles / Assets DLC", icon='FILE')
        dlc_row = dlc_controls.row()
        dlc_row.template_list(
            "WITCHER_UL_game_dlc_mounter_sources",
            "",
            self,
            "dlc_mounter_sources",
            self,
            "dlc_mounter_sources_index",
            rows=4,
        )
        dlc_controls.label(text="REDkit DLC", icon='TOOL_SETTINGS')
        redkit_dlc_row = dlc_controls.row()
        redkit_dlc_row.template_list(
            "WITCHER_UL_redkit_dlc_mounter_sources",
            "",
            self,
            "dlc_mounter_sources",
            self,
            "redkit_dlc_mounter_sources_index",
            rows=3,
        )
        common_col.prop(self, "verbose_logging")

        # Asset Browser settings — dedicated section
        try:
            display_width = int(getattr(getattr(context, "window", None), "width", 0) or 0)
        except Exception:
            display_width = 0
        if os.name == "nt" and getattr(context, "window", None) is not None:
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

                window = context.window
                win_x = int(getattr(window, "x", 0) or 0)
                win_y = int(getattr(window, "y", 0) or 0)
                win_w = int(getattr(window, "width", 0) or 0)
                win_h = int(getattr(window, "height", 0) or 0)
                rect = _RECT(win_x, win_y, win_x + max(1, win_w), win_y + max(1, win_h))
                monitor = ctypes.windll.user32.MonitorFromRect(ctypes.byref(rect), 2)
                if monitor:
                    info = _MONITORINFO()
                    info.cbSize = ctypes.sizeof(_MONITORINFO)
                    if ctypes.windll.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                        monitor_width = int(info.rcMonitor.right - info.rcMonitor.left)
                        if monitor_width > 0:
                            display_width = monitor_width
            except Exception:
                pass

        browser_box = layout.box()
        browser_head = browser_box.row(align=True)
        browser_head.label(text="Asset Browser", icon='FILE_FOLDER')
        browser_head.label(text="", icon='IMGDISPLAY')

        # --- Popup window sizing ---
        popup_box = browser_box.box()
        popup_box.label(text="Popup Window", icon='WINDOW')
        popup_col = popup_box.column()
        popup_col.use_property_split = True
        popup_col.use_property_decorate = False
        popup_col.prop(self, "browser_popup_width_percent", text="Window Width (%)")
        if display_width > 0:
            fixed_width = int(getattr(self, "browser_popup_width", 0) or 0)
            info_row = popup_col.row()
            if fixed_width > 0:
                info_row.label(text=f"Fixed popup width: {fixed_width} px", icon='INFO')
            else:
                pct = max(20, min(100, int(getattr(self, "browser_popup_width_percent", 50) or 50)))
                try:
                    ui_scale = float(getattr(getattr(context, "preferences", None), "system", None).ui_scale or 1.0)
                except Exception:
                    ui_scale = 1.0
                safe_margin = int(round(48 * ui_scale))
                approx_width = min(display_width - safe_margin, int(display_width * pct / 100.0))
                info_row.label(text=f"About {pct}% of monitor (~{approx_width} px)", icon='INFO')
        width_row = popup_col.row(align=True)
        width_row.prop(self, "browser_popup_width", text="Override (px)")
        width_row.operator("witcher.reset_browser_popup_width", text="", icon='LOOP_BACK')

        # --- Folder panel ---
        folder_box = browser_box.box()
        folder_box.label(text="Folder Panel", icon='FILE_FOLDER')
        folder_col = folder_box.column()
        folder_col.use_property_split = True
        folder_col.use_property_decorate = False
        folder_col.prop(self, "browser_folder_panel_width_percent", text="Panel Width (%)")
        if display_width > 0:
            fixed_folder_width = int(getattr(self, "browser_folder_panel_width", 0) or 0)
            info_row = folder_col.row()
            if fixed_folder_width > 0:
                info_row.label(text=f"Fixed folder panel width: {fixed_folder_width} px", icon='INFO')
            else:
                folder_pct = max(8, min(40, int(getattr(self, "browser_folder_panel_width_percent", 15) or 15)))
                approx_folder_width = min(640, max(180, int(display_width * folder_pct / 100.0)))
                info_row.label(text=f"About {folder_pct}% of monitor (~{approx_folder_width} px)", icon='INFO')
        folder_col.prop(self, "browser_folder_panel_width", text="Override (px)")

        # --- Grid / List layout ---
        layout_box = browser_box.box()
        layout_box.label(text="Files Layout", icon='IMGDISPLAY')
        layout_col = layout_box.column()
        layout_col.use_property_split = True
        layout_col.use_property_decorate = False
        layout_col.prop(self, "browser_grid_size_mode", text="Default Icon Size")
        layout_col.prop(self, "browser_grid_columns", text="Grid Columns")
        layout_col.prop(self, "browser_grid_max_rows", text="Grid Rows Per Page")
        layout_col.prop(self, "browser_file_page_size", text="List Files Per Page")
        layout_col.prop(self, "strings_browser_page_size", text="Strings Rows Per Page")

        # External command-line tool paths
        _tools_box, tools_col = section("External Tools", 'TOOL_SETTINGS')
        wolvenkit_row = tools_col.row(align=True)
        wolvenkit_row.prop(self, "wolvenkit")
        wolvenkit_row.operator("witcher.download_wolvenkit_cli_nightly", text="Install Nightly", icon='IMPORT')
        wolvenkit_row.operator("wm.url_open", text="", icon='URL').url = WOLVENKIT_CLI_DOWNLOAD_URL
        help_op = wolvenkit_row.operator("witcher.pref_help_popup", text="", icon='QUESTION')
        help_op.topic = "wolvenkit_cli"
        help_op.path = getattr(self, "wolvenkit", "")
        help_op.is_file = True
        help_op.title_text = "WolvenKit 7 CLI"

        radish_path_row = tools_col.row(align=True)
        radish_path_row.prop(self, "radish_tools_path")
        radish_path_row.operator("wm.url_open", text="Website", icon='URL').url = RADISH_LIPSYNC_REDKIT_URL
        help_op = radish_path_row.operator("witcher.pref_help_popup", text="", icon='QUESTION')
        help_op.topic = "radish_tools"
        help_op.path = getattr(self, "radish_tools_path", "")
        help_op.is_file = False
        help_op.title_text = "Radish Lipsync 4 REDkit"

        wwise_path_row = tools_col.row(align=True)
        wwise_path_row.prop(self, "wwise_console_path")
        wwise_path_row.operator("witcher.autofind_wwise_console", text="Auto Find", icon='VIEWZOOM')
        help_op = wwise_path_row.operator("witcher.pref_help_popup", text="", icon='QUESTION')
        help_op.topic = "wwise_console"
        help_op.path = getattr(self, "wwise_console_path", "")
        help_op.is_file = True
        help_op.title_text = "Wwise Console"

        lipsync_box = _tools_box.box()
        lipsync_box.label(text="REDkit Lipsync", icon='SOUND')
        try:
            from .lipsync import radish_runner as _lipsync_radish_runner
        except Exception:
            _lipsync_radish_runner = None

        if _lipsync_radish_runner is not None:
            tools_dir, missing_tools = _lipsync_radish_runner.get_full_tool_status(
                getattr(self, "radish_tools_path", ""),
                include_converter=True,
            )
            radish_row = lipsync_box.row(align=True)
            radish_row.alert = not bool(tools_dir)
            if tools_dir:
                radish_row.label(text=f"Radish ready: {Path(tools_dir).name}", icon='CHECKMARK')
            else:
                radish_row.label(text="Radish Lipsync 4 REDkit missing", icon='ERROR')
                if missing_tools:
                    missing_text = ", ".join(missing_tools[:2])
                    if len(missing_tools) > 2:
                        missing_text += ", ..."
                    radish_row.label(text=missing_text)

            wwise_console, _missing_wwise = _lipsync_radish_runner.get_wwise_status(
                getattr(self, "wwise_console_path", ""),
                tools_dir=tools_dir or getattr(self, "radish_tools_path", ""),
            )
            wwise_row = lipsync_box.row(align=True)
            wwise_row.alert = not bool(wwise_console)
            if wwise_console:
                wwise_row.label(text=f"Wwise ready: {Path(wwise_console).parent.name}", icon='CHECKMARK')
            else:
                wwise_row.label(text="Wwise 2021.1.x missing", icon='ERROR')
        else:
            lipsync_box.label(text="Lipsync status unavailable", icon='ERROR')

        links_row = lipsync_box.row(align=True)
        links_row.operator("wm.url_open", text="Wwise 2021.1.x", icon='URL').url = WWISE_DOWNLOAD_URL
        links_row.operator("wm.url_open", text="Wwise Docs", icon='HELP').url = WWISE_INSTALL_DOC_URL

        # External importer add-on status (APX / SRT / RE)
        ext_addons_box, ext_addons_col = section("External Addons", 'PLUGIN')
        ext_info_row = ext_addons_col.row(align=True)
        ext_info_row.label(text="Used for Redcloth, SpeedTree, mimic, and cutscene .re export")
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

        re_status = ui_cache_export.get_re_addon_status()
        re_row = deps_box.row(align=True)
        re_icon = 'CHECKMARK' if re_status["enabled"] else 'ERROR'
        re_row.label(
            text=f"blender_re_animations_plugin: {'enabled' if re_status['enabled'] else 'not enabled'}",
            icon=re_icon,
        )
        if not re_status["exists"] and ui_cache_export.RE_ADDON_URL:
            re_row.operator("wm.url_open", text="Source", icon='URL').url = ui_cache_export.RE_ADDON_URL

        # Modding/work project paths
        mod_box, mod_col = section("Mod Paths", 'FILE_FOLDER')
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

        unreal_box, _unreal_col = section("Unreal Projects", 'FILE_FOLDER')
        unreal_row = unreal_box.row(align=True)
        unreal_row.template_list("WITCHER_UL_path_list", "", self, "unreal_projects", self, "unreal_projects_index", rows=3)
        unreal_buttons = unreal_row.column(align=True)
        unreal_buttons.operator("witcher.add_unreal_project", text="", icon="ADD")
        unreal_buttons.operator("witcher.remove_unreal_project", text="", icon="REMOVE")

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
        from .cloth.geometry_nodes import find_clothsimulation_modifier
        count = 0
        for obj in context.scene.objects:
            mod = find_clothsimulation_modifier(obj)
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
        "name": "sound_cache.pkl",
        "relative_path": os.path.join("SoundCache", "sound_cache.pkl"),
        "label": "sound_cache.pkl",
        "description": "Vanilla sound cache index from game archives.",
    },
    {
        "name": "sound_cache_mods.pkl",
        "relative_path": os.path.join("SoundCache", "sound_cache_mods.pkl"),
        "label": "sound_cache_mods.pkl",
        "description": "Mod/DLC sound cache index.",
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
        "name": "w2_dzip_cache.pkl",
        "relative_path": os.path.join("Witcher2Bundles", "w2_dzip_cache.pkl"),
        "label": "w2_dzip_cache.pkl",
        "description": "Witcher 2 DZIP archive index cache.",
        "group": "witcher2",
    },
    {
        "name": "w2_string_cache",
        "relative_path": os.path.join("W2Strings"),
        "label": "W2Strings",
        "description": "Witcher 2 localized string table cache.",
        "is_dir": True,
        "group": "witcher2",
    },
    {
        "name": "w2_speech_cache",
        "relative_path": os.path.join("W2Speech"),
        "label": "W2Speech",
        "description": "Witcher 2 speech archive lookup cache.",
        "is_dir": True,
        "group": "witcher2",
    },
    {
        "name": "dlc_definition_cache.pkl",
        "relative_path": os.path.join("DLC", "dlc_definition_cache.pkl"),
        "label": "dlc_definition_cache.pkl",
        "description": "DLC definition and mounter cache from configured .reddlc files.",
        "group": "other",
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
        "name": "journal_browser_locations.pkl",
        "relative_path": os.path.join("JournalBrowser", "journal_browser_locations.pkl"),
        "label": "journal_browser_locations.pkl",
        "description": "Curated locations browser entry cache.",
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
    "witcher2": "Witcher 2",
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
    item = _get_cache_item(cache_name)
    explicit_group = item.get("group")
    if explicit_group in CACHE_GROUP_LABELS:
        return explicit_group
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
    if cache_name == "sound_cache.pkl":
        return lambda: SoundManager.BuildSourceSignature()
    if cache_name == "sound_cache_mods.pkl":
        return lambda: SoundManager.BuildSourceSignature(loadmods=True)
    if cache_name == "speech_cache.pkl":
        return lambda: SpeechManager.BuildSourceSignature()
    if cache_name == "bundle_cache.pkl":
        return lambda: BundleManager.BuildSourceSignature(False)
    if cache_name == "bundle_cache_mods.pkl":
        return lambda: BundleManager.BuildSourceSignature(True)
    if cache_name == "w2_dzip_cache.pkl":
        return lambda: DzipManager.BuildSourceSignature(get_witcher2_game_path(bpy.context))
    if cache_name == "dlc_definition_cache.pkl":
        return _build_dlc_definition_cache_signature
    if cache_name == "journal_browser_bestiary.pkl":
        return lambda: w3_asset_browser._journal_browser_signature("BESTIARY")
    if cache_name == "journal_browser_characters.pkl":
        return lambda: w3_asset_browser._journal_browser_signature("CHARACTERS")
    if cache_name == "journal_browser_locations.pkl":
        return w3_asset_browser._location_browser_signature
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
    if key == "LOCATIONS":
        try:
            w3_asset_browser._load_location_entries_cached(key, force_refresh=True)
            return True
        except Exception:
            log.warning("Failed to refresh locations cache", exc_info=True)
            return False
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


def _build_dlc_definition_cache_signature():
    from .importers import dlc_mounters

    return dlc_mounters.build_dlc_mounter_cache_signature(bpy.context)


def _refresh_dlc_definition_cache():
    from .importers import dlc_mounters

    return dlc_mounters.refresh_dlc_mounter_cache(bpy.context, sync_sources=True)


def _clear_dlc_definition_cache_state():
    try:
        from .importers import dlc_mounters

        dlc_mounters.clear_dlc_mounter_cache(reset_manager=True)
    except Exception:
        pass


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
        "sound_cache.pkl": lambda: SoundManager.Get(do_reload=True),
        "sound_cache_mods.pkl": lambda: SoundManager.Get(do_reload=True, loadmods=True),
        "speech_cache.pkl": lambda: SpeechManager.Get(do_reload=True),
        "bundle_cache.pkl": lambda: BundleManager.Get(loadmods=False, reset_cache=True),
        "bundle_cache_mods.pkl": lambda: BundleManager.Get(loadmods=True, reset_cache=True),
        "w2_dzip_cache.pkl": lambda: DzipManager.Get(reset_cache=True),
        "w2_string_cache": lambda: W2StringManager.Get(do_reload=True),
        "w2_speech_cache": lambda: W2SpeechManager.Get(do_reload=True),
        "dlc_definition_cache.pkl": _refresh_dlc_definition_cache,
        "journal_browser_bestiary.pkl": lambda: _refresh_journal_cache("BESTIARY"),
        "journal_browser_characters.pkl": lambda: _refresh_journal_cache("CHARACTERS"),
        "journal_browser_locations.pkl": lambda: _refresh_journal_cache("LOCATIONS"),
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
        if cache_name == "journal_browser_locations.pkl":
            w3_asset_browser._clear_journal_browser_caches("LOCATIONS")
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
        if cache_name == "dlc_definition_cache.pkl":
            _clear_dlc_definition_cache_state()
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

    for group_key in ("main", "main_mods", "witcher2", "other"):
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

def _strip_blender_copy_suffix(name):
    return re.sub(r'\.\d{3}$', '', name or "")


def _strip_lod_suffix(name):
    return re.sub(r'_lod\d+$', '', _strip_blender_copy_suffix(name), flags=re.IGNORECASE)


def _is_lod_named_object(obj):
    return bool(re.search(r'_lod\d+$', _strip_blender_copy_suffix(getattr(obj, "name", "")), re.IGNORECASE))


def _sort_meshes_by_lod(meshes):
    return sorted(
        (mesh for mesh in meshes if mesh and getattr(mesh, "type", None) == 'MESH'),
        key=lambda mesh: (lod_level_from_name(mesh.name), _strip_blender_copy_suffix(mesh.name).lower()),
    )


def _find_related_meshes(base_name, scene=None):
    lod_meshes = []
    col_tri_meshes = []
    scene = scene or getattr(bpy.context, "scene", None)
    if scene is None:
        return lod_meshes, col_tri_meshes
    for obj in scene.objects:
        if obj.name.startswith(base_name) and obj.name[len(base_name):].startswith("_lod"):
            lod_meshes.append(obj)
        elif obj.name.startswith(base_name):
            if _get_collision_type(obj.name):
                col_tri_meshes.append(obj)
    return _sort_meshes_by_lod(lod_meshes), col_tri_meshes


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

def _resolve_cmesh_context(context):
    active_ob = getattr(context, "active_object", None)
    scene = getattr(context, "scene", None)
    if not active_ob or active_ob.type not in {'MESH', 'ARMATURE'}:
        return None

    armature_ob = None
    current_mesh = active_ob if active_ob.type == 'MESH' else None
    if active_ob.type == 'ARMATURE':
        armature_ob = active_ob
    elif current_mesh and getattr(current_mesh, "parent", None) and current_mesh.parent.type == 'ARMATURE':
        armature_ob = current_mesh.parent
    elif current_mesh:
        for modifier in getattr(current_mesh, "modifiers", []):
            armature_obj = getattr(modifier, "object", None)
            if modifier.type == 'ARMATURE' and armature_obj and getattr(armature_obj, "type", None) == 'ARMATURE':
                armature_ob = armature_obj
                break

    export_meshes = []
    col_tri_meshes = []
    base_name = ""

    if armature_ob is not None:
        armature_meshes = [child for child in armature_ob.children if child.type == 'MESH']
        lod_named_meshes = [mesh for mesh in armature_meshes if _is_lod_named_object(mesh)]
        export_meshes = _sort_meshes_by_lod(lod_named_meshes if lod_named_meshes else armature_meshes)
        if export_meshes:
            base_name = _strip_lod_suffix(export_meshes[0].name)
    elif active_ob.type == 'MESH':
        if _get_collision_type(active_ob.name):
            export_meshes = [active_ob]
        else:
            base_name = _strip_lod_suffix(active_ob.name)
            lod_meshes, col_tri_meshes = _find_related_meshes(base_name, scene=scene)
            export_meshes = list(lod_meshes)
            if active_ob not in export_meshes:
                export_meshes.append(active_ob)
            export_meshes = _sort_meshes_by_lod(export_meshes)

    if export_meshes and base_name and not col_tri_meshes:
        _, col_tri_meshes = _find_related_meshes(base_name, scene=scene)

    main_mesh = export_meshes[0] if export_meshes else current_mesh
    if main_mesh is None:
        return None

    return {
        "active_object": active_ob,
        "armature": armature_ob,
        "main_mesh": main_mesh,
        "current_mesh": current_mesh if current_mesh and current_mesh.type == 'MESH' else None,
        "export_meshes": export_meshes or [main_mesh],
        "collision_meshes": col_tri_meshes,
        "base_name": base_name or _strip_lod_suffix(main_mesh.name),
    }


def _resolve_cmesh_target(context):
    cmesh_context = _resolve_cmesh_context(context)
    return cmesh_context["main_mesh"] if cmesh_context else None


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
        add_row(col, "Radish Lipsync 4 REDkit", getattr(addon_prefs, "radish_tools_path", ""))
        add_row(col, "Wwise Console", getattr(addon_prefs, "wwise_console_path", ""))
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
        add_row(col, "Uncook", addon_prefs.w2_unbundle_path)

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
        cmesh_context = _resolve_cmesh_context(context)
        mesh_ob = cmesh_context["main_mesh"] if cmesh_context else None
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
        armature_ob = cmesh_context["armature"] if cmesh_context else None
        current_mesh = cmesh_context["current_mesh"] if cmesh_context else None
        export_meshes = list(cmesh_context["export_meshes"]) if cmesh_context else [mesh_ob]
        col_tri_meshes = list(cmesh_context["collision_meshes"]) if cmesh_context else []
        current_mesh_settings = getattr(current_mesh, "witcherui_MeshSettings", None) if current_mesh else None

        def section(section_id, label, icon, default_closed=False):
            container = layout.box()
            header, body = container.panel(section_id, default_closed=default_closed)
            header.label(text=label, icon=icon)
            return body

        def value_row(col, label, value, icon='NONE'):
            row = col.row(align=True)
            split = row.split(factor=0.38, align=True)
            split.label(text=label)
            if icon != 'NONE':
                split.label(text=value or "-", icon=icon)
            else:
                split.label(text=value or "-")

        # --- Mesh Info ---
        body = section("witcher_cmesh_info", "Mesh Info", 'OBJECT_DATA')
        if body:
            col = body.column(align=True)
            col.prop(mesh_settings, "item_repo_path")
            value_row(col, "Skeleton", armature_ob.name if armature_ob else "-", icon='ARMATURE_DATA' if armature_ob else 'NONE')
            value_row(col, "LOD Meshes", str(len(export_meshes)))
            col.label(text="Used Slots")
            _draw_mesh_used_material_entries(col, _get_mesh_group_export_material_entries(export_meshes))

        if current_mesh_settings is not None:
            body = section("witcher_cmesh_current_lod", "Current LOD", 'MESH_DATA')
            if body:
                col = body.column(align=True)
                value_row(col, "Object", current_mesh.name, icon='MESH_DATA')
                row = col.row()
                row.enabled = False
                row.prop(current_mesh_settings, "lod_level")
                col.prop(current_mesh_settings, "distance")
                row = col.row()
                row.enabled = False
                row.prop(current_mesh_settings, "mat_id", text="Imported Mat ID")
                col.label(text="Used Slots")
                _draw_mesh_used_material_entries(col, _get_mesh_export_material_entries(current_mesh))

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
            row = col.row()
            row.enabled = False
            row.prop(mesh_settings, "isStatic")
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
        lod_meshes = list(export_meshes)

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


_TERRAIN_TEXTURE_PACK_UI_LOADING = False


def _terrain_texture_pack_update(item, context):
    if _TERRAIN_TEXTURE_PACK_UI_LOADING:
        return
    key = str(getattr(item, "texture_pack_key", "") or "")
    layer_id = int(getattr(item, "layer_id", 0) or 0)
    if not key or layer_id <= 0:
        return
    from .importers import terrain_detail_nodes
    terrain_detail_nodes.update_terrain_texture_pack_layer(
        key,
        layer_id,
        blend_sharpness=item.blend_sharpness,
        slope_base_dampening=item.slope_base_dampening,
        # The stored range is [0, 1]; the editor exposes [-1, 1].
        slope_normal_dampening=item.slope_normal_dampening * 0.5 + 0.5,
        falloff=item.falloff,
        specularity=item.specularity,
        specularity_base=item.specularity_base,
        specularity_scale=item.specularity_scale,
    )


class WITCHER_PG_terrain_texture_layer(bpy.types.PropertyGroup):
    layer_id: IntProperty(name="Layer ID", min=1, max=32, options={'SKIP_SAVE'})
    texture_pack_key: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
    diffuse_path: StringProperty(
        name="Diffuse", description="Source diffuse texture", options={'SKIP_SAVE'})
    normal_path: StringProperty(
        name="Normal", description="Source normal/roughness texture", options={'SKIP_SAVE'})
    blend_sharpness: FloatProperty(
        name="Blend Sharpness", min=0.0, max=1.0,
        description="Texture blend sharpness stored in val.X",
        update=_terrain_texture_pack_update)
    slope_base_dampening: FloatProperty(
        name="Slope Base Damp", min=0.0, max=1.0,
        description="Slope base dampening stored in val.Y",
        update=_terrain_texture_pack_update)
    slope_normal_dampening: FloatProperty(
        name="Slope Normal Damp", min=-1.0, max=1.0,
        description="Unpacked slope normal dampening stored in val.Z",
        update=_terrain_texture_pack_update)
    falloff: FloatProperty(
        name="Falloff", min=0.0, max=1.0,
        description=(
            "Runtime falloff stored in val.W; some editors label the slot "
            "RSpec_Scale"),
        update=_terrain_texture_pack_update)
    specularity: FloatProperty(
        name="Specularity", min=0.0, max=1.0,
        description="Direct specularity input stored in val2.X",
        update=_terrain_texture_pack_update)
    specularity_base: FloatProperty(
        name="RSpec Base", min=0.0, max=1.0,
        description="Roughness-dependent specular base stored in val2.Y",
        update=_terrain_texture_pack_update)
    specularity_scale: FloatProperty(
        name="RSpec Scale", min=0.0, max=1.0,
        description=(
            "Runtime RSpec_Scale stored in val2.Z; some editors label the slot "
            "Fallof"),
        update=_terrain_texture_pack_update)


class WITCHER_UL_terrain_texture_layers(bpy.types.UIList):
    def draw_item(
        self, _context, layout, _data, item, _icon, _active_data,
        _active_propname, _index,
    ):
        row = layout.row(align=True)
        row.label(text="", icon='MATERIAL')
        row.label(text=f"{int(item.layer_id)}: {item.name}")


def _active_terrain_texture_pack(context):
    obj = getattr(context, "active_object", None)
    mat = getattr(obj, "active_material", None) if obj is not None else None
    if mat is None and obj is not None and getattr(obj, "data", None) is not None:
        materials = getattr(obj.data, "materials", None)
        if materials:
            mat = materials[0]
    if mat is None or not bool(mat.get("witcher_terrain_detail")):
        raise RuntimeError("Make an imported detail-terrain object active")
    key = str(mat.get("witcher_terrain_texture_pack_key", "") or "")
    if not key:
        raise RuntimeError("Reimport this terrain to enable live Texture Pack controls")
    metadata_text = str(mat.get("witcher_terrain_layer_metadata", "") or "")
    if not metadata_text and obj is not None:
        metadata_text = str(obj.get("witcher_terrain_layer_metadata", "") or "")
    try:
        metadata = json.loads(metadata_text) if metadata_text else []
    except (TypeError, ValueError, json.JSONDecodeError):
        metadata = []
    if not isinstance(metadata, list) or not metadata:
        raise RuntimeError("Terrain Texture Pack metadata is unavailable; reimport the terrain")
    return obj, mat, key, metadata


def _populate_terrain_texture_pack_ui(scene, key, metadata, source_name=""):
    from .importers import terrain_detail_nodes
    rows = terrain_detail_nodes.terrain_texture_pack_values(key, metadata)
    global _TERRAIN_TEXTURE_PACK_UI_LOADING
    _TERRAIN_TEXTURE_PACK_UI_LOADING = True
    try:
        scene.witcher_terrain_texture_layers.clear()
        for row in rows:
            item = scene.witcher_terrain_texture_layers.add()
            item.name = str(row.get("name") or f"Layer {row.get('id', 0)}")
            item.layer_id = int(row.get("id", 0))
            item.texture_pack_key = str(key)
            item.diffuse_path = str(
                row.get("diffuse_source") or row.get("diffuse_dds") or "")
            item.normal_path = str(
                row.get("normal_source") or row.get("normal_dds") or "")
            item.blend_sharpness = float(row.get("blend_sharpness", 0.0))
            item.slope_base_dampening = float(row.get("slope_base_dampening", 0.0))
            item.slope_normal_dampening = (
                float(row.get("slope_normal_dampening", 0.5)) * 2.0 - 1.0)
            item.falloff = float(row.get("falloff", 0.0))
            item.specularity = float(row.get("specularity", 0.0))
            item.specularity_base = float(row.get("specularity_base", 0.0))
            item.specularity_scale = float(row.get("specularity_scale", 0.0))
        scene.witcher_terrain_texture_pack_key = str(key)
        scene.witcher_terrain_texture_pack_source = str(source_name)
        scene.witcher_terrain_texture_pack_metadata = json.dumps(
            metadata, separators=(",", ":"))
        if rows:
            scene.witcher_terrain_texture_layer_index = min(
                max(0, int(scene.witcher_terrain_texture_layer_index)),
                len(rows) - 1,
            )
        else:
            scene.witcher_terrain_texture_layer_index = 0
    finally:
        _TERRAIN_TEXTURE_PACK_UI_LOADING = False
    return len(rows)


class WITCHER_OT_load_terrain_texture_pack(bpy.types.Operator):
    bl_idname = "witcher.load_terrain_texture_pack"
    bl_label = "Load Terrain Texture Pack"
    bl_description = "Load live per-layer parameters from the active terrain material"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        try:
            _obj, mat, key, metadata = _active_terrain_texture_pack(context)
            count = _populate_terrain_texture_pack_ui(
                context.scene, key, metadata, source_name=mat.name)
        except RuntimeError as exc:
            self.report({'WARNING'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, f"Loaded {count} terrain Texture Pack layers")
        return {'FINISHED'}


class WITCHER_OT_reset_terrain_texture_pack(bpy.types.Operator):
    bl_idname = "witcher.reset_terrain_texture_pack"
    bl_label = "Reset Texture Pack Parameters"
    bl_description = "Restore every live layer parameter to the imported terrain material values"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        scene = context.scene
        key = str(getattr(scene, "witcher_terrain_texture_pack_key", "") or "")
        try:
            metadata = json.loads(str(
                getattr(scene, "witcher_terrain_texture_pack_metadata", "") or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            metadata = []
        if not key or not metadata:
            self.report({'WARNING'}, "Load an active terrain Texture Pack first")
            return {'CANCELLED'}
        from .importers import terrain_detail_nodes
        terrain_detail_nodes.reset_terrain_texture_pack(key, metadata)
        count = _populate_terrain_texture_pack_ui(
            scene, key, metadata,
            source_name=getattr(scene, "witcher_terrain_texture_pack_source", ""))
        self.report({'INFO'}, f"Reset {count} terrain Texture Pack layers")
        return {'FINISHED'}


class WITCHER_OT_apply_terrain_material_values(bpy.types.Operator):
    bl_idname = "witcher.apply_terrain_material_values"
    bl_label = "Sync Terrain Material Controls"
    bl_description = "Sync the live terrain material controls to all loaded terrain materials"

    def execute(self, context):
        scene_settings = getattr(context.scene, "witcher_file_browser", None)
        if scene_settings is None:
            self.report({'ERROR'}, "Terrain settings are not available")
            return {'CANCELLED'}

        from .importers import import_w2w
        updated = import_w2w.update_all_terrain_material_controls(scene_settings)
        if updated <= 0:
            self.report({'WARNING'}, "No compatible loaded terrain materials found")
        else:
            self.report({'INFO'}, f"Synced {updated} terrain materials")
        return {'FINISHED'}


class WITCHER_OT_inspect_terrain_face_materials(bpy.types.Operator):
    bl_idname = "witcher.inspect_terrain_face_materials"
    bl_label = "Inspect Terrain Face Materials"
    bl_description = (
        "Show the horizontal and vertical atlas layers used at the center of "
        "the single selected terrain face"
    )
    bl_options = {'INTERNAL'}

    face_info: StringProperty(name="Selected Face", options={'SKIP_SAVE'})
    control_location: StringProperty(name="Control Location", options={'SKIP_SAVE'})
    horizontal_layers: StringProperty(name="Horizontal", options={'SKIP_SAVE'})
    vertical_layers: StringProperty(name="Vertical", options={'SKIP_SAVE'})
    slope_parameters: StringProperty(name="Slope Parameters", options={'SKIP_SAVE'})
    vertical_scales: StringProperty(name="Vertical Scale", options={'SKIP_SAVE'})
    corner_samples: StringProperty(name="Four Control Taps", options={'SKIP_SAVE'})
    horizontal_paths: StringProperty(name="Horizontal Paths", options={'SKIP_SAVE'})
    vertical_paths: StringProperty(name="Vertical Paths", options={'SKIP_SAVE'})
    control_buffer: StringProperty(
        name="Control Buffer", subtype='FILE_PATH', options={'SKIP_SAVE'})

    @staticmethod
    def _layer_summary(entries):
        parts = []
        for entry in entries:
            layer = entry["layer"]
            layer_id = int(layer["id"])
            if layer_id <= 0:
                parts.append(f"None / Hole ({float(entry['weight']) * 100.0:.1f}%)")
                continue
            parts.append(
                f"{layer_id}: {layer['name']} "
                f"[atlas {int(layer['atlas_index'])}] "
                f"({float(entry['weight']) * 100.0:.1f}%)")
        return " | ".join(parts) or "None"

    @staticmethod
    def _layer_paths(entries):
        parts = []
        for entry in entries:
            layer = entry["layer"]
            if int(layer["id"]) <= 0:
                continue
            path = str(layer.get("diffuse_source") or layer.get("diffuse_dds") or "")
            if path:
                parts.append(f"{int(layer['id'])}: {path}")
        return " | ".join(parts) or "No source path available"

    def _read_selection(self, context):
        obj = getattr(context, "active_object", None)
        if not _is_terrain_tile(obj):
            raise RuntimeError("Make a terrain tile the active object")
        if context.mode != 'EDIT_MESH':
            raise RuntimeError("Enter Edit Mode and select exactly one terrain face")

        import bmesh
        bm = bmesh.from_edit_mesh(obj.data)
        bm.faces.ensure_lookup_table()
        bm.faces.index_update()
        selected = [face for face in bm.faces if face.select]
        if len(selected) != 1:
            raise RuntimeError(
                f"Select exactly one terrain face (currently selected: {len(selected)})")
        face = selected[0]
        uv_layer = bm.loops.layers.uv.active
        if uv_layer is None:
            raise RuntimeError("The terrain tile has no active UV map")
        uvs = [loop[uv_layer].uv.copy() for loop in face.loops]
        if not uvs:
            raise RuntimeError("The selected terrain face has no UV coordinates")
        u = sum(float(uv.x) for uv in uvs) / len(uvs)
        v = sum(float(uv.y) for uv in uvs) / len(uvs)
        return obj, int(face.index), uvs, u, v

    def _inspect(self, context):
        from .importers import import_w2w, terrain_detail

        obj, face_index, uvs, u, v = self._read_selection(context)
        metadata = import_w2w.ensure_terrain_inspector_metadata(obj)
        lattice = terrain_detail.load_tile_control_lattice(
            metadata["texture_buffer"],
            metadata["res"],
            positive_x_texture_buffer=metadata["positive_x_texture_buffer"],
            positive_y_texture_buffer=metadata["positive_y_texture_buffer"],
            positive_xy_texture_buffer=metadata["positive_xy_texture_buffer"],
        )
        if lattice is None:
            raise RuntimeError(f"Could not read terrain control buffer: {metadata['texture_buffer']}")
        result = terrain_detail.inspect_terrain_control_lattice(
            lattice, u, v, metadata["layers"])

        res = int(result["resolution"])
        min_u = min(float(uv.x) for uv in uvs)
        max_u = max(float(uv.x) for uv in uvs)
        min_v = min(float(uv.y) for uv in uvs)
        max_v = max(float(uv.y) for uv in uvs)
        grid_x, grid_y = result["grid"]
        cell_x, cell_y = result["cell"]
        self.face_info = (
            f"{obj.name} | face {face_index} | "
            f"UV center ({u:.6f}, {v:.6f})")
        self.control_location = (
            f"cell ({cell_x}, {cell_y}) | lattice ({grid_x:.3f}, {grid_y:.3f}) | "
            f"face span X {min_u * res:.3f}..{max_u * res:.3f}, "
            f"Y {min_v * res:.3f}..{max_v * res:.3f}")
        self.horizontal_layers = self._layer_summary(result["horizontal_layers"])
        self.vertical_layers = self._layer_summary(result["vertical_layers"])

        effective = result["effective"]
        self.slope_parameters = (
            f"threshold {float(effective['slope_threshold']):.6g} | "
            f"H sharpness {float(effective['blend_sharpness']):.6g} | "
            f"V base damp {float(effective['slope_base_dampening']):.6g} | "
            f"V normal damp {float(effective['slope_normal_dampening']):.6g} | "
            f"hole {float(effective['hole_weight']) * 100.0:.1f}%")

        scales = {}
        corner_parts = []
        for tap in result["taps"]:
            weight = float(tap["weight"])
            if weight > 1e-8:
                key = (int(tap["scale_index"]), float(tap["vertical_uv_scale"]))
                scales[key] = scales.get(key, 0.0) + weight
            corner_parts.append(
                f"{tap['corner']} {weight * 100.0:.1f}%: "
                f"H{int(tap['horizontal_id'])}/V{int(tap['vertical_id'])} "
                f"S{int(tap['slope_index'])}={float(tap['slope_threshold']):.3g} "
                f"UV{int(tap['scale_index'])}={float(tap['vertical_uv_scale']):.4g} "
                f"0x{int(tap['control']):04X}")
        self.vertical_scales = " | ".join(
            f"index {index}: {scale:.6g} ({weight * 100.0:.1f}%)"
            for (index, scale), weight in sorted(scales.items())
        ) or "None"
        self.corner_samples = " | ".join(corner_parts)
        self.horizontal_paths = self._layer_paths(result["horizontal_layers"])
        self.vertical_paths = self._layer_paths(result["vertical_layers"])
        self.control_buffer = metadata["texture_buffer"]

    def invoke(self, context, event):
        try:
            self._inspect(context)
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self, width=780)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        selection = layout.box()
        selection.label(text="Selected Face", icon='FACESEL')
        selection.prop(self, "face_info")
        selection.prop(self, "control_location")

        layers = layout.box()
        layers.label(text="Atlas Layers at Face Center", icon='MATERIAL')
        layers.prop(self, "horizontal_layers")
        layers.prop(self, "vertical_layers")
        layers.prop(self, "horizontal_paths")
        layers.prop(self, "vertical_paths")

        controls = layout.box()
        controls.label(text="Control Blend", icon='NODE_MATERIAL')
        controls.prop(self, "slope_parameters")
        controls.prop(self, "vertical_scales")
        controls.prop(self, "corner_samples")
        controls.prop(self, "control_buffer")

    def execute(self, context):
        return {'FINISHED'}


def _collection_visible_on_start_get(collection):
    try:
        value = collection.get("witcher_visible_on_start", True)
    except Exception:
        return True
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", ""}
    return bool(value)


def _collection_visible_on_start_set(collection, value):
    try:
        collection["witcher_visible_on_start"] = bool(value)
    except Exception:
        return
    try:
        ui_map.invalidate_layer_visibility_cache()
    except Exception:
        pass
    try:
        context = bpy.context
        scene_settings = getattr(context.scene, "witcher_file_browser", None)
        hide = bool(getattr(scene_settings, "terrain_layer_hide_default_hidden", False))
        solo = bool(getattr(scene_settings, "terrain_layer_solo_default_hidden", False))
        if hide or solo:
            ui_map.apply_default_hidden_layer_groups(context, hide, solo)
    except Exception:
        pass


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
                col.prop(scene_settings, "terrain_multires_level", text="Preview Detail")
                if str(getattr(scene_settings, "terrain_import_mode", "")) == "SELECTED_TILE":
                    coords = col.row(align=True)
                    coords.prop(scene_settings, "terrain_tile_x", text="Tile X")
                    coords.prop(scene_settings, "terrain_tile_y", text="Tile Y")
                    col.prop(scene_settings, "terrain_include_foliage", text="Include Foliage")
                    foliage_detail = col.row()
                    foliage_detail.enabled = bool(scene_settings.terrain_include_foliage)
                    foliage_detail.prop(scene_settings, "terrain_foliage_mode", text="Foliage Detail")
                    actions = col.row(align=True)
                    import_op = actions.operator("witcher.import_world_tile", text="Import Tile", icon='IMPORT')
                    import_op.use_view = False
                    import_op.action = 'IMPORT'
                    view_op = actions.operator("witcher.import_world_tile", text="Tile Under View", icon='VIEW_CAMERA')
                    view_op.use_view = True
                    view_op.action = 'IMPORT'
                    unload_op = actions.operator("witcher.import_world_tile", text="", icon='TRASH')
                    unload_op.use_view = False
                    unload_op.action = 'UNLOAD'

            if hasattr(scene_settings, "terrain_material_surface_mode"):
                body = section("witcher_terrain_material", "Material", 'MATERIAL', default_closed=True)
                if body:
                    col = body.column(align=True)
                    col.prop(scene_settings, "terrain_material_surface_mode", text="Surface")
                    if scene_settings.terrain_material_surface_mode == 'OVERRIDE':
                        row = col.row(align=True)
                        row.prop(scene_settings, "terrain_material_roughness", text="Roughness")
                        row.prop(scene_settings, "terrain_material_specular", text="Specular")
                    col.prop(scene_settings, "terrain_material_normal_strength", text="Normal Strength")
                    debug_header = col.row(align=True)
                    debug_header.prop(
                        scene_settings,
                        "terrain_material_show_debug",
                        text="Debug",
                        icon=(
                            'TRIA_DOWN'
                            if scene_settings.terrain_material_show_debug
                            else 'TRIA_RIGHT'
                        ),
                        emboss=False,
                    )
                    if scene_settings.terrain_material_show_debug:
                        debug_box = col.box()
                        debug_box.prop(scene_settings, "terrain_material_debug_view", text="View")
                        debug_box.prop(scene_settings, "terrain_material_slope_mode", text="Slope")
                        debug_box.prop(scene_settings, "terrain_material_tint_strength", text="Tint")
                        debug_box.prop(scene_settings, "terrain_material_fresnel_strength", text="Fresnel")
                    col.operator(
                        "witcher.apply_terrain_material_values",
                        text="Sync Loaded Terrain",
                        icon='SHADING_RENDERED',
                    )

            if hasattr(scene_settings, "water_wind"):
                body = section("witcher_terrain_water", "Water", 'MOD_FLUIDSIM', default_closed=True)
                if body:
                    col = body.column(align=True)
                    has_water = any(
                        m.get("witcher_world_water_material") for m in bpy.data.materials
                    )
                    if not has_water:
                        col.label(text="No world water loaded", icon='INFO')
                    col.prop(scene_settings, "water_wind", text="Wind", slider=True)
                    col.prop(scene_settings, "water_wind_direction", text="Direction")
                    col.prop(scene_settings, "water_flow_speed", text="Flow Speed")
                    col.prop(scene_settings, "water_foam_intensity", text="Foam")
                    col.prop(scene_settings, "water_reflection", text="Reflection", slider=True)
                    col.prop(scene_settings, "water_clarity", text="Clarity", slider=True)
                    col.prop(scene_settings, "water_level", text="Level")

            if hasattr(context.scene, "witcher_terrain_texture_layers"):
                body = section(
                    "witcher_terrain_texture_pack",
                    "Texture Pack Parameters",
                    'TEXTURE',
                    default_closed=True,
                )
                if body:
                    col = body.column(align=True)
                    col.operator(
                        "witcher.load_terrain_texture_pack",
                        text="Load Active Terrain Pack",
                        icon='FILE_REFRESH',
                    )
                    layers = context.scene.witcher_terrain_texture_layers
                    if layers:
                        col.prop(
                            context.scene,
                            "witcher_terrain_texture_pack_source",
                            text="Material",
                        )
                        col.template_list(
                            "WITCHER_UL_terrain_texture_layers",
                            "terrain_texture_pack",
                            context.scene,
                            "witcher_terrain_texture_layers",
                            context.scene,
                            "witcher_terrain_texture_layer_index",
                            rows=5,
                        )
                        index = min(
                            max(0, int(context.scene.witcher_terrain_texture_layer_index)),
                            len(layers) - 1,
                        )
                        item = layers[index]
                        params = col.box()
                        params.label(
                            text=f"Layer {int(item.layer_id)}: {item.name}",
                            icon='MATERIAL',
                        )
                        spec_row = params.row(align=True)
                        spec_row.prop(item, "specularity", text="Specularity")
                        spec_row.prop(item, "specularity_base", text="RSpec Base")
                        params.prop(item, "blend_sharpness", text="Blend Sharpness")
                        params.prop(item, "slope_base_dampening", text="Slope Base Damp")
                        params.prop(item, "slope_normal_dampening", text="Slope Normal Damp")
                        params.prop(item, "falloff", text="Falloff")
                        params.prop(item, "specularity_scale", text="RSpec Scale")
                        params.label(
                            text="Some editors swap the last two labels",
                            icon='INFO',
                        )
                        paths = params.box()
                        paths.label(text="Texture Sources", icon='FILE_IMAGE')
                        paths.prop(item, "diffuse_path", text="Diffuse")
                        paths.prop(item, "normal_path", text="Normal")
                        col.operator(
                            "witcher.reset_terrain_texture_pack",
                            text="Reset Imported Values",
                            icon='LOOP_BACK',
                        )

        body = section("witcher_terrain_collection", "Layer Collection", 'OUTLINER_COLLECTION', default_closed=True)
        coll = context.collection
        if body:
            col = body.column(align=True)
            if coll:
                col.prop(coll, "name")
                group_type = str(coll.get("group_type", "")).strip()
                world_path = str(coll.get("world_path", "")).strip()
                level_path = ui_map.ensure_collection_w2layer_path(coll)
                layer_build_tag = str(coll.get("layerBuildTag", "")).strip()

                if group_type:
                    col.prop(coll, '["group_type"]', text="group_type")
                if group_type == "LayerGroup":
                    col.prop(coll, "witcher_visible_on_start", text="isVisibleOnStart", toggle=True)
                if world_path:
                    col.prop(coll, '["world_path"]', text="world_path")
                if level_path:
                    col.prop(coll, '["w2layer_path"]', text="w2layer_path")
                if layer_build_tag:
                    col.prop(coll, '["layerBuildTag"]', text="layerBuildTag")

                has_level_button = bool(level_path)
                has_group_button = group_type == "LayerGroup"
                if has_level_button or has_group_button or group_type:
                    col.separator()
                    col_load = col.column(align=True)
                    col_load.operator("witcher.w2l_collection_details", text="Details", icon='INFO')
                    if has_level_button:
                        col_load.operator("witcher.load_layer", text="Load This Layer", icon='CUBE')
                        col_load.operator(
                            "witcher.send_unreal_layer_collection",
                            text="Send Layer to Unreal", icon='URL',
                        ).action = "SEND"
                    if has_group_button:
                        col_load.operator("witcher.load_layer_group", text="Load This LayerGroup", icon='OUTLINER_COLLECTION')
                        col_load.operator(
                            "witcher.send_unreal_layer_group_collection",
                            text="Send LayerGroup to Unreal", icon='URL',
                        ).action = "SEND"
            else:
                col.label(text="No active collection", icon='INFO')

        if scene_settings and hasattr(scene_settings, "terrain_layer_load_radius"):
            body = section("witcher_terrain_layer_streaming", "Load Layers", 'VIEW_CAMERA', default_closed=True)
            if body:
                col = body.column(align=True)
                ui_map.draw_layer_stream_job_ui(col, context)
                controls = col.column(align=True)
                controls.enabled = not ui_map.layer_stream_job_running()
                range_box = controls.box()
                range_box.label(text="Range")
                range_box.prop(scene_settings, "terrain_layer_load_radius", text="Radius (World Units)")
                range_box.prop(scene_settings, "terrain_layer_max_load_count", text="Load Limit (0 = All)")
                range_box.prop(scene_settings, "terrain_layer_skip_loaded", text="Skip Complete Layers")

                filter_box = controls.box()
                filter_box.label(text="Resources to Import")
                filter_col = filter_box.column(align=True)
                filter_col.prop(scene_settings, "terrain_layer_do_import_mesh", text="Mesh")
                filter_col.prop(scene_settings, "terrain_layer_do_import_proxy_mesh", text="Proxy Mesh")
                filter_col.prop(scene_settings, "terrain_layer_do_import_entity", text="Entity")
                filter_col.prop(scene_settings, "terrain_layer_do_import_redcloth", text="Redcloth")
                filter_col.prop(scene_settings, "terrain_layer_do_import_redapex", text="Redapex")
                filter_col.prop(scene_settings, "terrain_layer_do_import_collision", text="Collision")
                filter_col.prop(scene_settings, "terrain_layer_do_import_rigidbody", text="Rigid Body")
                filter_col.prop(scene_settings, "terrain_layer_do_import_point_light", text="Point Lights")
                filter_col.prop(scene_settings, "terrain_layer_do_import_spot_light", text="Spot Lights")

                import_options_box = controls.box()
                import_options_box.label(text="Per-Type Import Options")

                mesh_box = import_options_box.box()
                mesh_box.label(text="W2Mesh")
                mesh_box.prop(scene_settings, "terrain_layer_instanced_sector", text="Instance Repeated Meshes")
                mesh_row = mesh_box.row(align=True)
                mesh_row.prop(scene_settings, "terrain_layer_keep_lod_meshes", text="Keep LODs")
                mesh_row.prop(scene_settings, "terrain_layer_keep_empty_lods", text="Keep Empty LODs")
                mesh_box.prop(scene_settings, "terrain_layer_keep_proxy_meshes", text="Keep Proxy Mesh LODs")

                redapex_box = import_options_box.box()
                redapex_box.label(text="Redapex")
                redapex_box.enabled = bool(getattr(scene_settings, "terrain_layer_do_import_redapex", True))
                redapex_row = redapex_box.row(align=True)
                redapex_row.enabled = bool(getattr(scene_settings, "terrain_layer_do_import_redapex", True))
                redapex_row.prop(scene_settings, "terrain_layer_redapex_import_chunks", text="Redapex Chunks")
                redapex_row.prop(scene_settings, "terrain_layer_redapex_import_floor", text="Redapex Floor")
                redapex_box.prop(scene_settings, "terrain_layer_redapex_collections_as_empties", text="Collections as Empties")

                filter_options_box = controls.box()
                filter_options_box.label(text="Filters")
                filter_options_box.prop(scene_settings, "terrain_layer_enable_name_filter", text="Enable Regex Filter")
                regex_row = filter_options_box.row(align=True)
                regex_row.enabled = bool(getattr(scene_settings, "terrain_layer_enable_name_filter", False))
                regex_row.prop(scene_settings, "terrain_layer_name_filter_regex", text="Regex")

                visibility_box = controls.box()
                visibility_box.label(text="Visibility After Load")
                if ui_map.location_viewer_visibility_active(context):
                    visibility_box.label(text="Location View: Proxies Hidden", icon='HIDE_ON')
                def visibility_row(label, hide_prop, solo_prop):
                    row = visibility_box.row(align=True)
                    hide_enabled = bool(getattr(scene_settings, hide_prop, False))
                    solo_enabled = bool(getattr(scene_settings, solo_prop, False))
                    row.prop(scene_settings, hide_prop, text=label, icon='HIDE_ON' if hide_enabled else 'HIDE_OFF', toggle=True)
                    row.prop(scene_settings, solo_prop, text="", icon='HIDE_OFF' if solo_enabled else 'HIDE_ON', toggle=True)

                visibility_row("Default-Hidden Groups", "terrain_layer_hide_default_hidden", "terrain_layer_solo_default_hidden")
                visibility_row("Engine Hidden", "terrain_layer_hide_engine_hidden_meshes", "terrain_layer_solo_engine_hidden_meshes")
                visibility_row("Proxy Meshes", "terrain_layer_hide_proxy_meshes", "terrain_layer_solo_proxy_meshes")
                visibility_row("Redapex", "terrain_layer_hide_redapex", "terrain_layer_solo_redapex")
                visibility_row("Collision", "terrain_layer_hide_collision", "terrain_layer_solo_collision")
                visibility_row("Volume Meshes", "terrain_layer_hide_volume_meshes", "terrain_layer_solo_volume_meshes")
                visibility_row("Shadow Meshes", "terrain_layer_hide_shadow_meshes", "terrain_layer_solo_shadow_meshes")

                debug_box = controls.box()
                debug_box.label(text="Debug")
                debug_box.prop(scene_settings, "terrain_layer_write_profile_log", text="Write Profile Log")
                controls.label(text=ui_map.get_camera_position_label(context))
                scan_row = controls.row(align=True)
                scan_row.label(text=ui_map.get_nearby_cache_summary_label(context))
                scan_row.operator("witcher.scan_layers_nearby", text="", icon='VIEWZOOM')
                row = controls.row(align=True)
                row.operator("witcher.load_layers_around_camera", text="Load Layers Around Camera", icon='VIEW_CAMERA')
                row.operator("witcher.rebuild_layer_scan_cache", text="", icon='FILE_REFRESH')
                controls.operator(
                    "witcher.send_unreal_layers_around_camera",
                    text="Send Nearby Layers to Unreal", icon='URL',
                ).action = "SEND"

        body = section("witcher_terrain_foliage", "Foliage", 'PARTICLE_DATA', default_closed=True)
        if body:
            col = body.column(align=True)
            col.label(text=ui_map.get_foliage_info_label(context))
            ui_map.draw_foliage_job_ui(col, context)
            controls = col.column(align=True)
            controls.enabled = not ui_map.foliage_busy()
            if scene_settings and hasattr(scene_settings, "foliage_load_radius"):
                controls.prop(scene_settings, "foliage_load_radius", text="Radius (World Units)")
            controls.operator("witcher.load_foliage_around_camera", text="Load Foliage Around Camera", icon='PARTICLE_DATA')
            controls.operator("witcher.hydrate_foliage_sources", text="Load Full Sources", icon='IMPORT')
            row = controls.row(align=True)
            row.operator("witcher.check_foliage_world", text="World Info", icon='INFO')
            row.operator("witcher.open_foliage_browser", text="Browse Folder", icon='FILE_FOLDER')
            controls.separator()
            row2 = controls.row(align=True)
            row2.operator("witcher.toggle_foliage_visibility", text="Toggle Visibility", icon='HIDE_OFF')
            row2.operator("witcher.unload_foliage", text="Unload", icon='TRASH')

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

        active_tile = context.active_object if _is_terrain_tile(context.active_object) else None
        body = section("witcher_terrain_face_materials", "Face Materials", 'MATERIAL')
        if body:
            col = body.column(align=True)
            if active_tile is None:
                col.label(text="Make a terrain tile active", icon='INFO')
            elif context.mode != 'EDIT_MESH':
                col.label(text="Edit Mode: select one face", icon='FACESEL')
            else:
                col.label(text="Uses the selected face center", icon='FACESEL')
            action = col.row()
            action.enabled = active_tile is not None
            action.operator(
                "witcher.inspect_terrain_face_materials",
                text="Inspect Selected Face",
                icon='VIEWZOOM',
            )

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
                for k in ("group_type", "world_path", "w2layer_path", "level_path", "layerBuildTag")
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
            if hasattr(addon_prefs, "premerge_character_armature"):
                col.prop(addon_prefs, "premerge_character_armature", text="Pre-merged Armature")
            if hasattr(addon_prefs, "import_physics_enabled"):
                col.prop(addon_prefs, "import_physics_enabled", text="Physics Enabled")
            obj = context.active_object
            armature = None
            if obj and obj.type == 'ARMATURE':
                armature = obj
            elif obj and obj.parent and obj.parent.type == 'ARMATURE':
                armature = obj.parent
            if armature and hasattr(armature.data, 'witcherui_RigSettings'):
                rig_settings = armature.data.witcherui_RigSettings
                rot90_on = get_rig_rot90_enabled(rig_settings, default=False)
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
            about_info = _get_addon_about_info()
            col.label(text=f"Witcher 3 Tools v{about_info['version']}")
            col.label(text=f"Author: {about_info['author']}")
            doc_url = about_info["doc_url"]
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
                    for k in ("group_type", "world_path", "w2layer_path", "level_path", "layerBuildTag")
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
                if hasattr(addon_prefs, "premerge_character_armature"):
                    col.prop(addon_prefs, "premerge_character_armature", text="Pre-merged Armature")
                if hasattr(addon_prefs, "import_physics_enabled"):
                    col.prop(addon_prefs, "import_physics_enabled", text="Physics Enabled")
                obj = context.active_object
                armature = None
                if obj and obj.type == 'ARMATURE':
                    armature = obj
                elif obj and obj.parent and obj.parent.type == 'ARMATURE':
                    armature = obj.parent
                if armature and hasattr(armature.data, 'witcherui_RigSettings'):
                    rig_settings = armature.data.witcherui_RigSettings
                    rot90_on = get_rig_rot90_enabled(rig_settings, default=False)
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
                about_info = _get_addon_about_info()
                col.label(text=f"Witcher 3 Tools v{about_info['version']}")
                col.label(text=f"Author: {about_info['author']}")
                doc_url = about_info["doc_url"]
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
from .CR2W.witcher_cache.Witcher2Bundles import DzipManager
from .CR2W.witcher_cache.SoundCache import SoundManager
from .CR2W.witcher_cache.Speech import SpeechManager
from .CR2W.witcher_cache.TextureCache import TextureManager
from .CR2W.witcher_cache.W2Speech import W2SpeechManager
from .CR2W.witcher_cache.W2Strings import W2StringManager
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
    WITCH_OT_import_world_tile,
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
    WITCH_OT_w2l_collection_details,
    WITCH_OT_load_layer,
    WITCH_OT_load_layer_group,
    WITCH_OT_cancel_layer_stream_job,
    WITCH_OT_load_layers_around_camera,
    WITCH_OT_rebuild_layer_scan_cache,
    WITCH_OT_scan_layers_nearby,
    WITCH_OT_cancel_foliage_job,
    WITCH_OT_load_foliage_around_camera,
    WITCH_OT_toggle_foliage_visibility,
    WITCH_OT_unload_foliage,
    WITCH_OT_hydrate_foliage_sources,
    WITCH_OT_open_foliage_browser,
    WITCH_OT_check_foliage_world,
    WITCH_OT_load_texarray,
    WITCHER_OT_open_external_path,
    WITCHER_OT_open_addon_preferences,
    WITCHER_OT_dismiss_external_import_alert,
    WITCHER_OT_select_terrain_tiles,
    WITCHER_OT_apply_fullmap_multires,
    WITCHER_PG_terrain_texture_layer,
    WITCHER_UL_terrain_texture_layers,
    WITCHER_OT_load_terrain_texture_pack,
    WITCHER_OT_reset_terrain_texture_pack,
    WITCHER_OT_apply_terrain_material_values,
    WITCHER_OT_inspect_terrain_face_materials,
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
    bpy.utils.register_class(UnrealProjectItem)
    bpy.utils.register_class(DlcMounterSourceItem)
    bpy.utils.register_class(WITCHER_UL_path_list)
    bpy.utils.register_class(WITCHER_UL_game_dlc_mounter_sources)
    bpy.utils.register_class(WITCHER_UL_redkit_dlc_mounter_sources)
    bpy.utils.register_class(WITCHER_OT_dlc_mounter_source_details)
    bpy.utils.register_class(AddPathOperator)
    bpy.utils.register_class(RemovePathOperator)
    bpy.utils.register_class(AddRedkitProjectOperator)
    bpy.utils.register_class(RemoveRedkitProjectOperator)
    bpy.utils.register_class(AddUnrealProjectOperator)
    bpy.utils.register_class(RemoveUnrealProjectOperator)
    bpy.utils.register_class(WITCHER_OT_refresh_dlc_mounter_sources)
    bpy.utils.register_class(WITCHER_OT_reset_browser_popup_width)
    bpy.utils.register_class(WITCHER_OT_autofind_w3_path)
    bpy.utils.register_class(WITCHER_OT_autofind_w2_path)
    bpy.utils.register_class(WITCHER_OT_download_wolvenkit_cli_nightly)
    bpy.utils.register_class(WITCHER_OT_autofind_wwise_console)
    bpy.utils.register_class(WITCHER_OT_open_pref_path)
    bpy.utils.register_class(WITCHER_OT_pref_help_popup)

    bpy.utils.register_class(Witcher3AddonPrefs)
    prefs = _get_registered_addon_prefs(bpy.context)
    if prefs is not None:
        _apply_dev_pref_overrides(prefs)
        # Apply logging levels after programmatic dev overrides because property
        # update callbacks do not fire when set via setattr().
        try:
            _update_verbose_logging(prefs, bpy.context)
        except Exception:
            pass
        _auto_initialize_game_and_audio_paths(prefs, bpy.context)
    try:
        from .importers import dlc_mounters

        dlc_sources = getattr(prefs, "dlc_mounter_sources", []) or []
        has_legacy_dlc_strings = any(
            not getattr(item, "dlc_name_key", "")
            and str(getattr(item, "dlc_name", "") or "").startswith("dlc_")
            for item in dlc_sources
        )
        if len(dlc_sources) == 0 or has_legacy_dlc_strings:
            dlc_mounters.sync_dlc_mounter_sources(bpy.context)
    except Exception:
        pass
    bpy.types.Scene.witcher_tools_tab = EnumProperty(
        name="Witcher Tools Tab",
        items=WITCHER_TOOLS_TABS,
        default='TOOLS'
    )
    bpy.types.Collection.witcher_visible_on_start = BoolProperty(
        name="isVisibleOnStart",
        description="Whether this RED layer group is visible when the world starts",
        default=True,
        get=_collection_visible_on_start_get,
        set=_collection_visible_on_start_set,
    )
    armature_context.register()
    for cls in _classes:
        register_class(cls)
    bpy.types.Scene.witcher_terrain_texture_layers = CollectionProperty(
        type=WITCHER_PG_terrain_texture_layer)
    bpy.types.Scene.witcher_terrain_texture_layer_index = IntProperty(
        name="Texture Pack Layer", default=0, min=0)
    bpy.types.Scene.witcher_terrain_texture_pack_key = StringProperty(
        name="Texture Pack Key", options={'HIDDEN'})
    bpy.types.Scene.witcher_terrain_texture_pack_source = StringProperty(
        name="Terrain Material", description="Loaded terrain material data-block")
    bpy.types.Scene.witcher_terrain_texture_pack_metadata = StringProperty(
        name="Texture Pack Source Data", options={'HIDDEN'})
    ui_custom_icons.register()
    ui_file_browser.register()
    ui_entity.register()
    ui_equipment.register()
    ui_material.register()
    ui_morphs.register()
    livelink_face.register()
    ui_texture_export.register()
    ui_import_menu.register()
    ui_dialog_language.register()
    ui_anims.register()
    ui_physics.register()
    ui_environment.register()
    ui_speech.register()
    ui_scene.register()
    ui_cutscene.register()
    ui_animated_component.register()
    bpy.utils.register_class(WITCHER_OT_cache_info)
    bpy.utils.register_class(WITCHER_OT_check_cache)
    bpy.utils.register_class(WITCHER_OT_refresh_cache_checked)
    bpy.utils.register_class(WITCHER_OT_delete_cache)
    bpy.utils.register_class(WITCHER_OT_refresh_cache)
    ui_cache_export.register()
    register_class(WITCH_PT_Quick)
    ui_voice.register()
    strings_browser.register()
    lipsync.register()
    ui_mimics.register()
    ui_re_anims.register()
    ui_anims_list.register()
    material_nodes.register()
    w3_asset_browser.register()
    unreal_export.register()
    
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
    
    #PATH LIST
    bpy.utils.unregister_class(WITCHER_OT_refresh_dlc_mounter_sources)
    bpy.utils.unregister_class(RemoveUnrealProjectOperator)
    bpy.utils.unregister_class(AddUnrealProjectOperator)
    bpy.utils.unregister_class(RemoveRedkitProjectOperator)
    bpy.utils.unregister_class(AddRedkitProjectOperator)
    bpy.utils.unregister_class(RemovePathOperator)
    bpy.utils.unregister_class(AddPathOperator)
    bpy.utils.unregister_class(WITCHER_OT_autofind_wwise_console)
    bpy.utils.unregister_class(WITCHER_OT_download_wolvenkit_cli_nightly)
    bpy.utils.unregister_class(WITCHER_OT_autofind_w2_path)
    bpy.utils.unregister_class(WITCHER_OT_autofind_w3_path)
    bpy.utils.unregister_class(WITCHER_OT_reset_browser_popup_width)
    bpy.utils.unregister_class(WITCHER_OT_pref_help_popup)
    bpy.utils.unregister_class(WITCHER_OT_open_pref_path)
    bpy.utils.unregister_class(WITCHER_OT_dlc_mounter_source_details)
    bpy.utils.unregister_class(WITCHER_UL_redkit_dlc_mounter_sources)
    bpy.utils.unregister_class(WITCHER_UL_game_dlc_mounter_sources)
    bpy.utils.unregister_class(WITCHER_UL_path_list)
    bpy.utils.unregister_class(DlcMounterSourceItem)
    bpy.utils.unregister_class(UnrealProjectItem)
    bpy.utils.unregister_class(PathItem)

    unreal_export.unregister()
    w3_asset_browser.unregister()
    unregister_class(WITCH_PT_Quick)
    ui_cache_export.unregister()
    bpy.utils.unregister_class(WITCHER_OT_refresh_cache)
    bpy.utils.unregister_class(WITCHER_OT_delete_cache)
    bpy.utils.unregister_class(WITCHER_OT_refresh_cache_checked)
    bpy.utils.unregister_class(WITCHER_OT_check_cache)
    bpy.utils.unregister_class(WITCHER_OT_cache_info)
    bpy.utils.unregister_class(Witcher3AddonPrefs)
    del bpy.types.Scene.witcher_tools_tab
    if hasattr(bpy.types.Collection, "witcher_visible_on_start"):
        del bpy.types.Collection.witcher_visible_on_start
    for prop_name in (
        "witcher_terrain_texture_layers",
        "witcher_terrain_texture_layer_index",
        "witcher_terrain_texture_pack_key",
        "witcher_terrain_texture_pack_source",
        "witcher_terrain_texture_pack_metadata",
    ):
        if hasattr(bpy.types.Scene, prop_name):
            delattr(bpy.types.Scene, prop_name)
    armature_context.unregister()
    for cls in _classes:
        unregister_class(cls)
    ui_import_menu.unregister()
    ui_texture_export.unregister()
    ui_cutscene.unregister()
    ui_animated_component.unregister()
    ui_scene.unregister()
    ui_speech.unregister()
    ui_environment.unregister()
    ui_physics.unregister()
    ui_anims.unregister()
    ui_dialog_language.unregister()
    ui_material.unregister()
    ui_equipment.unregister()
    ui_entity.unregister()
    ui_file_browser.unregister()
    livelink_face.unregister()
    ui_morphs.unregister()
    lipsync.unregister()
    strings_browser.unregister()
    ui_voice.unregister()
    ui_mimics.unregister()
    ui_re_anims.unregister()
    ui_anims_list.unregister()
    material_nodes.unregister()
    ui_custom_icons.unregister()

