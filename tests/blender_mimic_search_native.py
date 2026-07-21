import sys
from pathlib import Path

import bpy


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import witcher3_tools
from witcher3_tools.ui import ui_mimics


witcher3_tools.register()
scene = bpy.context.scene
setattr(scene, ui_mimics.MIMIC_SEARCH_PROP, "head_left02")
getattr(scene, ui_mimics.MIMIC_LIST_PROP).clear()

result = bpy.ops.witcher.quick_mimic_debug(action="search")
items = getattr(scene, ui_mimics.MIMIC_LIST_PROP)

assert result == {'FINISHED'}
assert [item.name for item in items if not item.isCategory] == ["head_left02_head_accent_face"]
print("Mimic search native test passed")
