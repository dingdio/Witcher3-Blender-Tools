"""Smoke-test repeated add-on registration in Blender 4.5+."""

import sys
from pathlib import Path

import bpy
try:
    from _bpy_restrict_state import RestrictBlend
except ImportError:  # Blender 4.5
    from bpy_restrict_state import RestrictBlend


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import witcher3_tools as addon
from witcher3_tools.ui import ui_equipment, ui_anim_events


registered = False
try:
    for _ in range(2):
        with RestrictBlend():
            addon.register()
        registered = True
        for name in ('quick_anim_category_bulk', 'open_strings_browser', 'image_browser',
                     'character_image_browser', 'location_image_browser'):
            assert getattr(bpy.ops.witcher, name).get_rna_type(), name
        assert bpy.app.timers.is_registered(ui_anim_events.restore_track_layout)
        if _ == 0:
            bpy.app.timers.unregister(ui_anim_events.restore_track_layout)
            ui_anim_events.restore_track_layout()
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
        registered = {
            cls
            for mod_name, mod in list(sys.modules.items()) if mod_name.startswith("witcher3_tools")
            for cls in vars(mod).values()
            if isinstance(cls, type) and "bl_rna" in vars(cls) and cls.__module__.startswith("witcher3_tools")
        }
        for cls in registered:
            # Registering a subclass of a registered class detaches the parent's Python class (issue #13).
            assert not registered.intersection(cls.__mro__[1:]), f"{cls.__name__} subclasses a registered class"

        addon.unregister()
        assert not bpy.app.timers.is_registered(ui_anim_events.restore_track_layout)
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
