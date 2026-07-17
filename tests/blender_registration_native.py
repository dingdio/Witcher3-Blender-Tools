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
        assert hasattr(bpy.types.WindowManager, "witcherui_temp_data")
        assert bpy.app.handlers.load_post.count(
            ui_equipment._repair_equipment_state_on_load
        ) == 1

        addon.unregister()
        registered = False
        assert not hasattr(bpy.types.Scene, "witcher_file_browser")
        assert not hasattr(bpy.types.Scene, "witcher_character_tab")
        assert not hasattr(bpy.types.WindowManager, "witcherui_temp_data")
        assert ui_equipment._repair_equipment_state_on_load not in bpy.app.handlers.load_post

    print("W3TB_REGISTRATION_NATIVE_OK")
finally:
    if registered:
        addon.unregister()
