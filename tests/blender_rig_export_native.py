"""Rig export round-trip: import vanilla man_base.w2rig, export native, compare engine-space rest pose.

Run with Blender 4.5+:
  blender --background --factory-startup --python tests/blender_rig_export_native.py
"""
import os
import sys
import tempfile
import traceback
from pathlib import Path

import bpy

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import witcher3_tools as addon  # noqa: E402

VANILLA_RIG = "characters/base_entities/man_base/man_base.w2rig"


def _find_rig():
    roots = [
        os.environ.get("W3_UNCOOK", ""),
        addon.get_uncook_path(bpy.context) or "",
        r"C:\Users\david\AppData\Roaming\Blender Foundation\Blender\5.0\extensions\.user\vscode_development\witcher3_tools\witcher_uncook",
        r"E:\w3.modding\modkit\r4data",
    ]
    for root in roots:
        if root and (Path(root) / VANILLA_RIG).is_file():
            return str(Path(root) / VANILLA_RIG)
    raise RuntimeError("vanilla man_base.w2rig not found")


def _parse(path):
    from witcher3_tools.CR2W import dc_skeleton
    cr2w = dc_skeleton.getCR2W(dc_skeleton.open_cr2w_read_stream(path))
    return cr2w, dc_skeleton.create_Skeleton(cr2w)


def _check(rot90, rig_path, src, tmp):
    from witcher3_tools.exporters import export_rig
    from witcher3_tools.importers import import_rig

    arm = import_rig.import_w3_rig(rig_path, ns=f"rot{int(rot90)}", do_fix_tail=rot90)
    for obj in bpy.context.view_layer.objects:
        obj.select_set(obj is arm)
    bpy.context.view_layer.objects.active = arm

    out = str(tmp / f"rot{int(rot90)}.w2rig")
    assert export_rig.export_w3_rig(bpy.context, out) == {'FINISHED'}
    cr2w, exp = _parse(out)
    assert exp.names == src.names, "bone names/order changed"
    assert exp.parentIdx == src.parentIdx, "parent indices changed"
    assert len(cr2w.CR2WExport) == 5, "control rig / teleport chunks not carried over"

    worst_pos = max(abs(getattr(a, c) - getattr(b, c)) for a, b in zip(src.positions, exp.positions) for c in "xyz")
    worst_rot = min(abs(a.x * b.x + a.y * b.y + a.z * b.z + a.w * b.w) for a, b in zip(src.rotations, exp.rotations))
    assert worst_pos < 1e-5, f"position error {worst_pos}"
    assert worst_rot > 1 - 1e-6, f"rotation error {1 - worst_rot}"

    again = import_rig.import_w3_rig(out, ns=f"again{int(rot90)}", do_fix_tail=rot90)
    worst_mat = max(
        abs(v) for bone in arm.data.bones
        for row in (bone.matrix_local - again.data.bones[bone.name].matrix_local) for v in row
    )
    assert worst_mat < 1e-4, f"re-import matrix_local error {worst_mat}"
    print(f"rot90={rot90}: pos {worst_pos:.2e}, rot {1 - worst_rot:.2e}, matrix {worst_mat:.2e}")


addon.register()
try:
    rig_path = _find_rig()
    _, src = _parse(rig_path)
    tmp = Path(tempfile.mkdtemp())
    for rot90 in (True, False):
        _check(rot90, rig_path, src, tmp)
    print("W3TB_RIG_EXPORT_OK")
except Exception:
    traceback.print_exc()
    print("W3TB_RIG_EXPORT_FAIL")
    sys.exit(1)
