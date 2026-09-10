"""Run with a Blender UI (auto-exits): blender --factory-startup --python <this file>.

Checks every preset page, stale texture-index repair, and thumbnail reuse.
"""
import sys
import tempfile
import pickle
import time
import traceback
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import witcher3_tools as addon
from witcher3_tools.ui import ui_equipment as eq, equipment_item_picker as picker

addon.register()
root = Path(addon._get_prefs(bpy.context).witcher_game_path)
print('GAME ROOT:', root, 'BUNDLES:', len(list(root.glob('content/content*/bundles/*.bundle'))), flush=True)
sandbox = Path(tempfile.mkdtemp(prefix='w3tb_icons_'))
from witcher3_tools.CR2W.witcher_cache.TextureCache import texture_manager as tm
from witcher3_tools.CR2W.witcher_cache import cache_meta
# Reproduce the saved empty index from before the v7 parser fix, with a valid
# game-file signature (so ordinary timestamp invalidation would accept it).
tm.get_cache_root = lambda create=True: str(sandbox)
old_index = tm.TextureManager()
del old_index.cache_version
old_index.base_path = str(root)
index_path = sandbox / 'TextureCache' / 'texture_cache.pkl'
index_path.parent.mkdir()
index_path.write_bytes(pickle.dumps(old_index))
signature, source = tm.TextureManager.BuildSourceSignature(False)
cache_meta.save_meta(str(index_path) + '.meta.json', cache_meta.make_meta(
    index_path.name, str(index_path), signature, source))
tm.TextureManager.InstanceManager = None
from witcher3_tools.ui import ui_file_browser
original_preview_path = ui_file_browser._make_browser_preview_temp_path
ui_file_browser._make_browser_preview_temp_path = lambda *args, **kwargs: str(
    sandbox / Path(original_preview_path(*args, **kwargs)).name
)
eq._EQUIPMENT_ICON_PATH_CACHE_FILE = sandbox / 'paths.json'
eq._EQUIPMENT_ICON_PERSIST_DIR = sandbox / 'icons'
eq._EQUIPMENT_ICON_PATH_DISK = {}
eq._clear_equipment_item_icon_cache()
# File loads remove non-persistent timers but leave Python module flags intact.
eq._EQUIPMENT_ITEM_ICON_TIMER_RUNNING = True
temp = bpy.context.window_manager.witcherui_temp_data
temp.preset_picker_view = 'LIST'
picker.prepare_inventory_preset_picker(bpy.context, 'GERALT_W3')
temp.preset_picker_page = 0
# A failed lookup saved by an older version must be retried, too.
first = eq._get_inventory_preset(temp.preset_picker_rows[0].identifier, source_game='w3')
entry = picker._inventory_preset_main_entry(first)
cache_key, *_ = eq._get_equipment_item_icon_cache_key(
    bpy.context, picker._inventory_preset_entry_item(entry),
    picker._inventory_preset_entry_attrs(entry, source_game='w3'),
    source_game='w3',
    fallback_template=picker._inventory_preset_entry_entity_path(entry) or picker._inventory_preset_entry_template(entry),
)
eq._EQUIPMENT_ICON_PATH_DISK[eq._equipment_icon_stable_key(cache_key)] = ''
# Queue before a simulated file load; the next draw must restart this request.
picker._inventory_preset_entry_icon_id(bpy.context, entry)
assert bpy.app.timers.is_registered(eq._equipment_item_icon_timer)
bpy.app.timers.unregister(eq._equipment_item_icon_timer)
observed = {}
resolve_times = []
original_resolver = eq._resolve_equipment_item_preview_path
def timed_resolve(*args, **kwargs):
    start = time.perf_counter()
    try:
        return original_resolver(*args, **kwargs)
    finally:
        resolve_times.append(time.perf_counter() - start)
eq._resolve_equipment_item_preview_path = timed_resolve


class ThumbnailPopup(bpy.types.Operator):
    bl_idname = 'wm.test_equipment_thumbnails'
    bl_label = 'Equipment thumbnail regression check'

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=860)

    def execute(self, context):
        return {'FINISHED'}

    def draw(self, context):
        picker.draw_inventory_preset_picker(context, self.layout)
        size = picker._inventory_preset_picker_page_size(temp)
        start = temp.preset_picker_page * size
        rows = temp.preset_picker_rows[start:start + size]
        icons = []
        for row in rows:
            preset = eq._get_inventory_preset(row.identifier, source_game='w3')
            for entry in picker._inventory_preset_preview_entries(picker._inventory_preset_entries(preset)):
                if entry:
                    icons.append(picker._inventory_preset_entry_icon_id(context, entry))
        observed[temp.preset_picker_page] = icons


bpy.utils.register_class(ThumbnailPopup)


def open_popup():
    bpy.ops.wm.test_equipment_thumbnails('INVOKE_DEFAULT')
    return None


attempts = 0
def check():
    global attempts
    attempts += 1
    try:
        page = temp.preset_picker_page
        icons = observed.get(page, [])
        error = eq._get_equipment_error_icon_id()
        loaded = icons and all(icon and icon != error for icon in icons)
        if not loaded and attempts < 45:
            return 1.0
        assert loaded, (page, icons, 'popup did not redraw with real thumbnails')
        print('THUMBNAIL PAGE OK:', page, len(icons), flush=True)
        size = picker._inventory_preset_picker_page_size(temp)
        if (page + 1) * size < len(temp.preset_picker_rows):
            temp.preset_picker_page += 1
            eq._tag_equipment_item_icon_redraw()
            attempts = 0
            return 1.0
        assert tm.TextureManager.InstanceManager.Items, 'empty texture index was not repaired'
        # Returning after selecting a character must reuse the loaded images.
        rig = bpy.data.objects.new('Thumbnail test rig', bpy.data.armatures.new('Thumbnail test rig'))
        bpy.context.collection.objects.link(rig)
        rig['_w3_entity_source_path'] = str(sandbox / 'player.w2ent')
        bpy.context.view_layer.objects.active = rig
        for row in temp.preset_picker_rows:
            preset = eq._get_inventory_preset(row.identifier, source_game='w3')
            for entry in picker._inventory_preset_preview_entries(picker._inventory_preset_entries(preset)):
                if entry:
                    assert picker._inventory_preset_entry_icon_id(bpy.context, entry) not in (0, error)
        assert not eq._EQUIPMENT_ITEM_ICON_REQUESTS, 'cached icons were queued again'
        print('TEXTURE INDEX:', len(tm.TextureManager.InstanceManager.Items), flush=True)
        print('RESOLVE TIMING:', len(resolve_times), 'total', sum(resolve_times), 'max', max(resolve_times), flush=True)
        print('W3TB_EQUIPMENT_THUMBNAIL_NATIVE_OK', flush=True)
    except Exception:
        traceback.print_exc()
        from witcher3_tools.ui import ui_file_browser
        print('SCALEFORM ROOTS:', ui_file_browser._browser_scaleform_root_cache, flush=True)
        print('W3TB_EQUIPMENT_THUMBNAIL_NATIVE_FAIL', flush=True)
    bpy.ops.wm.quit_blender()
    return None


bpy.app.timers.register(open_popup, first_interval=1.0)
bpy.app.timers.register(check, first_interval=2.0)
