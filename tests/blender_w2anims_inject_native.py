"""Check clip injection and replacement against a vanilla bear set.

  blender --background --factory-startup --python tests/blender_w2anims_inject_native.py
"""
import hashlib
import os
import sys
import tempfile
import traceback
from pathlib import Path

import bpy

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import witcher3_tools as addon  # noqa: E402

SET = "animations/animals/bear/bear_animation.w2anims"
RIG = "characters/base_entities/bear_berserker_base/bear_berserker_base.w2rig"
CLIP = "bear_sleeping_end"


def _root():
    for root in (os.environ.get("W3_UNCOOK", ""), addon.get_uncook_path(bpy.context) or "",
                 r"C:\Users\david\AppData\Roaming\Blender Foundation\Blender\5.0\extensions\.user\vscode_development\witcher3_tools\witcher_uncook",
                 r"E:\w3.modding\modkit\r4data"):
        if root and (Path(root) / SET).is_file() and (Path(root) / RIG).is_file():
            return Path(root)
    raise RuntimeError("vanilla bear set/rig not found")


def _sha(paths):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def _load(arm, name, path):
    from witcher3_tools.ui import ui_anims_list
    ui_anims_list.load_anim_into_scene(bpy.context, name, str(path), arm, use_NLA=False)
    action = arm.animation_data.action
    assert action is not None, f"{name} did not load"
    return action


def _pose(arm, action, frames):
    arm.animation_data.action = action
    out = []
    for f in frames:
        bpy.context.scene.frame_set(f)
        out.append({b.name: b.matrix_basis.copy() for b in arm.pose.bones})
    return out


addon.register()
try:
    from witcher3_tools.importers import import_rig

    root = _root()
    src = root / SET
    src_files = sorted(src.parent.glob("bear_animation.w2anims*"))
    before = _sha(src_files)

    arm = import_rig.import_w3_rig(str(root / RIG), ns="inject")
    for obj in bpy.context.view_layer.objects:
        obj.select_set(obj is arm)
    bpy.context.view_layer.objects.active = arm

    original = _load(arm, CLIP, src)
    scene = bpy.context.scene
    entry = scene.witcher_anim_export_set.add()
    entry.action_name = original.name
    entry.enabled = True

    scene.witcher_inject.target = str(src)
    assert len(scene.witcher_inject.clips) == 78 and not scene.witcher_inject.error
    tmp = Path(tempfile.mkdtemp())
    out = tmp / "added" / "bear_animation.w2anims"
    if original.name.lower() == CLIP:
        try:
            bpy.ops.witcher.inject_save_copy('EXEC_DEFAULT', filepath=str(out))
            raise AssertionError("duplicate clip name was not refused")
        except RuntimeError as exc:
            assert "already exists" in str(exc) and not out.exists(), exc

    original.name = "bear_sleeping_end_injected"
    entry.action_name = original.name
    assert bpy.ops.witcher.inject_save_copy('EXEC_DEFAULT', filepath=str(out)) == {'FINISHED'}
    assert _sha(src_files) == before, "source files changed"
    assert out.is_file() and Path(f"{out}.78.buffer").is_file()

    replaced = tmp / "replaced" / "bear_animation.w2anims"
    entry.replace_clip = "bear_sleeping_loop"
    assert bpy.ops.witcher.inject_save_copy('EXEC_DEFAULT', filepath=str(replaced)) == {'FINISHED'}
    assert _sha(src_files) == before, "source files changed"
    from witcher3_tools.CR2W import w2anims_inject as wi
    names = [c["name"] for c in wi.describe_set(wi.RawCR2W(replaced.read_bytes()))]
    assert len(names) == 78 and "bear_sleeping_end_injected" not in names

    frame_start, frame_end = (int(v) for v in original.frame_range)
    frames = sorted({frame_start, (frame_start + frame_end) // 2, frame_end})
    expected = _pose(arm, original, frames)
    reloaded = _load(arm, original.name, out)
    assert reloaded is not original
    actual = _pose(arm, reloaded, frames)
    swapped = _load(arm, "bear_sleeping_loop", replaced)
    worst_swap = max(abs(v) for e, a in zip(expected, _pose(arm, swapped, frames)) for n in e for row in (e[n] - a[n]) for v in row)
    assert worst_swap < 5e-3, f"replaced clip differs by {worst_swap}"

    worst = max(abs(v) for e, a in zip(expected, actual) for name in e for row in (e[name] - a[name]) for v in row)
    assert worst < 5e-3, f"re-imported pose differs by {worst}"
    vanilla_again = _load(arm, CLIP, out)
    print(f"frames {frames}, worst matrix delta {worst:.2e}, vanilla clip still loads: {vanilla_again.name}")
    print("W3TB_W2ANIMS_INJECT_OK")
except Exception:
    traceback.print_exc()
    print("W3TB_W2ANIMS_INJECT_FAIL")
    sys.exit(1)
