"""Smoke-test repeated add-on registration in Blender 4.5+."""

import sys
from pathlib import Path

import bpy


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import witcher3_tools as addon
from witcher3_tools.ui import ui_equipment


registered = False
try:
    for _ in range(2):
        addon.register()
        registered = True
        assert hasattr(bpy.types.Scene, "witcher_file_browser")
        assert hasattr(bpy.types.Scene, "witcher_character_tab")
        assert hasattr(bpy.types.Scene, "witcher_cutscene_dialog_lines")
        assert hasattr(bpy.types.Scene, "witcher_cutscene_dialog_line_index")
        assert hasattr(bpy.types.Scene, "witcher_cutscene_dialog_id_space")
        assert hasattr(bpy.types.Scene, "witcher_cutscene_dialog_strings_path")
        assert hasattr(bpy.types.WindowManager, "witcherui_temp_data")
        assert hasattr(bpy.types.WindowManager, "witcher_cutscene_dialog_voice")
        assert addon.Witcher3AddonPrefs.bl_rna.properties["tts_command"].default == ""
        assert bpy.app.handlers.load_post.count(
            ui_equipment._repair_equipment_state_on_load
        ) == 1

        addon.unregister()
        registered = False
        assert not hasattr(bpy.types.Scene, "witcher_file_browser")
        assert not hasattr(bpy.types.Scene, "witcher_character_tab")
        assert not hasattr(bpy.types.Scene, "witcher_cutscene_dialog_lines")
        assert not hasattr(bpy.types.Scene, "witcher_cutscene_dialog_line_index")
        assert not hasattr(bpy.types.Scene, "witcher_cutscene_dialog_id_space")
        assert not hasattr(bpy.types.Scene, "witcher_cutscene_dialog_strings_path")
        assert not hasattr(bpy.types.WindowManager, "witcherui_temp_data")
        assert not hasattr(bpy.types.WindowManager, "witcher_cutscene_dialog_voice")
        assert ui_equipment._repair_equipment_state_on_load not in bpy.app.handlers.load_post

    print("W3TB_REGISTRATION_NATIVE_OK")
finally:
    if registered:
        addon.unregister()
