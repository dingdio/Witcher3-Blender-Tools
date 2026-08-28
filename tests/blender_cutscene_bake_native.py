import sys
import traceback
from pathlib import Path

import bpy

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

BLEND = REPO_ROOT / "WORKING_TEMP" / "e2e_dlc" / "cs_ciri_drowner.blend"
OUT = REPO_ROOT / "WORKING_TEMP" / "e2e_dlc" / "dlc" / "modw3tools_e2e" / "data" / "cutscenes" / "cs_ciri_drowner.w2cutscene"

bpy.ops.wm.open_mainfile(filepath=str(BLEND))

import witcher3_tools as addon

addon.register()
try:
    from witcher3_tools.animation import cutscene_bake
    from witcher3_tools.exporters import export_cutscene
    from witcher3_tools.CR2W.CR2W_file import read_CR2W

    ctx = bpy.context
    issues_before = cutscene_bake.validate_cutscene_for_export(ctx)
    print("ISSUES BEFORE BAKE:")
    for issue in issues_before:
        print("  -", issue)
    assert issues_before, "expected validator to flag the un-baked scene"

    baked = cutscene_bake.bake_cutscene_actors(ctx)
    print(f"BAKED {len(baked)} actors: {[a.get('cutscene_actor_name') for a, _ in baked]}")
    assert len(baked) == 3, [a.name for a, _ in baked]

    issues_after = cutscene_bake.validate_cutscene_for_export(ctx)
    print("ISSUES AFTER BAKE:")
    for issue in issues_after:
        print("  -", issue)
    assert not issues_after, issues_after

    # Re-baking must be idempotent.
    n_actions = len(bpy.data.actions)
    baked = cutscene_bake.bake_cutscene_actors(ctx)
    assert len(baked) == 3, [a.name for a, _ in baked]
    assert len(bpy.data.actions) == n_actions, (n_actions, len(bpy.data.actions))
    for arm, _action in baked:
        names = [t.name for t in arm.animation_data.nla_tracks]
        assert names.count("cutscene_anim") == 1 and not any(".00" in n for n in names), names
        assert sum(1 for t in arm.animation_data.nla_tracks if not t.mute) == 1, names
    assert not cutscene_bake.validate_cutscene_for_export(ctx)
    print("RE-BAKE OK: tracks/actions stable")

    ret = export_cutscene.export_w3_cutscene(ctx, str(OUT))
    print("EXPORT:", ret, OUT.stat().st_size, "bytes")

    f = read_CR2W(str(OUT))
    anims = []
    for ch in f.CHUNKS.CHUNKS:
        if ch.Type == "CSkeletalAnimation":
            dur = fps = None
            for p in ch.PROPS or []:
                if p.theName == "duration":
                    dur = p.Value
                if p.theName == "framesPerSecond":
                    fps = p.Value
            anims.append((round(dur or 0, 3), round(fps or 0, 2)))
    print("EXPORTED ANIMS (duration, fps):", anims)
    assert len(anims) == 3, anims
    durations = {a[0] for a in anims}
    assert len(durations) == 1, f"animations must all span the full cutscene: {anims}"
    expected = round((bpy.context.scene.frame_end - bpy.context.scene.frame_start) / 30.0, 1)
    assert abs(list(durations)[0] - expected) < 0.2, (durations, expected)
    print("W3TB_CUTSCENE_BAKE_NATIVE_OK")
except Exception:
    traceback.print_exc()
    print("W3TB_CUTSCENE_BAKE_NATIVE_FAIL")
    sys.exit(1)
finally:
    addon.unregister()
