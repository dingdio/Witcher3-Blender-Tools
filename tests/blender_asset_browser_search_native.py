"""Asset browser search: ranking, filter before the cap, Home search off the draw path, tags, batch sort.

Run: blender --background --factory-startup --python tests/blender_asset_browser_search_native.py
"""

import sys
import traceback
from pathlib import Path
from types import SimpleNamespace

import bpy


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import witcher3_tools as addon
from witcher3_tools.ui import ui_file_browser as fb


class FakeManager:
    def __init__(self, keys):
        self.Items = {key: [] for key in keys}


class FakeLayout:
    def __init__(self, texts):
        self.texts = texts

    def _child(self, *args, **kwargs):
        return FakeLayout(self.texts)

    row = column = split = box = grid_flow = _child

    def label(self, text="", **kwargs):
        self.texts.append(text)

    def operator(self, idname, **kwargs):
        self.texts.append(f"op:{idname}")
        return SimpleNamespace()

    def prop(self, data, name, **kwargs):
        assert hasattr(data, name), name

    def separator(self, **kwargs):
        pass

    def template_icon(self, **kwargs):
        pass

    template_icon_view = template_icon


class DrawHost:
    pass


for _name, _value in vars(fb.SimpleFileBrowser).items():
    if callable(_value) and not _name.startswith("__"):
        setattr(DrawHost, _name, _value)


def draw_texts():
    host = DrawHost()
    host.layout = FakeLayout([])
    host.draw(bpy.context)
    return host.layout.texts


def populate(paths):
    fb.folder_structure.clear()
    for path in paths:
        fb.folder_structure.add_path(path)
    fb.clear_search_cache()


addon.register()
original_persist, original_save, original_loaders = (
    fb._persist_current_browser_state,
    fb.save_browser_state,
    fb._global_search_cache_loaders,
)
fb._persist_current_browser_state = lambda context: None
fb.save_browser_state = lambda *args, **kwargs: None
failed = False
try:
    browser = bpy.context.scene.witcher_file_browser
    world = r"levels\novigrad\novigrad.w2w"
    texarray = r"levels\novigrad\novigrad.texarray"
    occlusion = [rf"levels\novigrad\occlusion_tiles\novigrad.w2w_0x{i}.w3occlusion" for i in range(40)]
    populate(occlusion + [world, texarray, "root_file.w2ent"])
    browser.active_cache_type = "Bundle"
    browser.current_folder = "levels"
    browser.sort_by = 'NAME'
    browser.sort_ascending = True

    browser.search_query = "  novigrad  "
    assert browser.search_query == "novigrad", browser.search_query
    state = fb.get_cached_search_results(browser, compute=False)
    assert state is not None and state["total"] == 42, state
    assert fb._sorted_search_results(state, browser)[:2] == [texarray, world]
    browser.sort_ascending = False
    assert fb._sorted_search_results(state, browser)[:2] == [world, texarray]
    browser.sort_ascending = True

    assert bpy.ops.witcher.browser_page(action="next") == {'FINISHED'}
    assert browser.search_page_index == 1, browser.search_page_index
    assert bpy.ops.witcher.copy_all_search_paths() == {'FINISHED'}

    browser.extension_filter = ".texarray"
    state = fb.get_cached_search_results(browser, compute=False)
    assert state["total"] == 1 and fb._sorted_search_results(state, browser) == [texarray], state
    browser.extension_filter = ""

    browser.search_query = "   "
    assert browser.search_query == ""

    fb.BROWSER_SEARCH_RESULT_LIMIT = 5
    try:
        browser.search_query = "novigrad.w2w"
        texts = draw_texts()
    finally:
        fb.BROWSER_SEARCH_RESULT_LIMIT = 1000
    assert "Showing the best 5 of 41 matches" in texts, texts
    assert next(text for text in texts if text.startswith("levels\\")).startswith(world), texts

    fb._global_search_cache_loaders = lambda scope, loadmods=False: [
        ("Bundle", lambda: FakeManager([world, r"characters\geralt\body.w2mesh.1.buffer"])),
        (fb.WITCHER2_BUNDLE_CACHE_TYPE, lambda: FakeManager([world])),
    ]
    browser.search_query = ""
    browser.active_cache_type = ""
    browser.current_folder = ""
    fb._set_browser_state_save_suspended(True)
    browser.search_query = "novigrad.w2w"
    fb._set_browser_state_save_suspended(False)
    assert fb.get_cached_search_results(browser, compute=False) is None, "restored Home query ran on restore"
    texts = draw_texts()
    assert "Search not run yet" in texts and "op:witcher.run_browser_search" in texts, texts
    assert fb.get_cached_search_results(browser, compute=False) is None, "draw ran the Home search"
    assert bpy.ops.witcher.run_browser_search() == {'FINISHED'}
    state = fb.get_cached_search_results(browser, compute=False)
    results = fb._sorted_search_results(state, browser)
    assert sorted(results) == sorted([("Bundle", world), (fb.WITCHER2_BUNDLE_CACHE_TYPE, world)]), results
    texts = draw_texts()
    assert "[Bun]" in texts and "[W2 Bun]" in texts, texts
    browser.search_query = "body"
    assert fb.get_cached_search_results(browser, compute=False)["total"] == 0, "buffer file was searchable"
    browser.search_query = ""

    populate([rf"folder\file_{i:03d}.w2ent" for i in range(60)])
    browser.active_cache_type = "Bundle"
    browser.current_folder = "folder"
    browser.sort_ascending = False
    assert fb.get_visible_batch_file_paths(bpy.context)[0] == r"folder\file_059.w2ent"
    browser.sort_ascending = True

    request = fb.FileActionOperatorImportToScene._build_import_state(
        SimpleNamespace(file_path="root_file.w2ent", cache_type="Bundle"),
        bpy.context,
    )
    assert request["full_path"] == "root_file.w2ent", request

    print("W3TB_ASSET_BROWSER_SEARCH_NATIVE_OK", flush=True)
except Exception:
    failed = True
    traceback.print_exc()
    print("W3TB_ASSET_BROWSER_SEARCH_NATIVE_FAIL", flush=True)
finally:
    fb._persist_current_browser_state = original_persist
    fb.save_browser_state = original_save
    fb._global_search_cache_loaders = original_loaders
    fb.folder_structure.clear()
    fb.clear_search_cache()
    addon.unregister()

if failed:
    sys.exit(1)
