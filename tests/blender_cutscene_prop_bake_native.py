import sys
import traceback
from pathlib import Path

import bpy

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

BLEND = REPO_ROOT / "WORKING_TEMP" / "e2e_dlc" / "cs_ciri_drowner.blend"
OUT = REPO_ROOT / "WORKING_TEMP" / "e2e_dlc" / "dlc" / "modw3tools_e2e" / "data" / "cutscenes" / "cs_ciri_drowner.w2cutscene"
PROPS_ENT = OUT.parent / "cs_ciri_drowner_props.w2ent"

bpy.ops.wm.open_mainfile(filepath=str(BLEND))

import witcher3_tools as addon

addon.register()
try:
    from witcher3_tools.animation import cutscene_bake
    from witcher3_tools.exporters import export_cutscene
    from witcher3_tools.CR2W.CR2W_file import read_CR2W

    ctx = bpy.context
    scene = ctx.scene

    def find_carried_sword():
        for obj in scene.objects:
            if getattr(obj, "type", None) != 'MESH':
                continue
            for con in obj.constraints:
                if con.type == 'CHILD_OF' and getattr(con, "subtarget", "") == "r_weapon":
                    return obj
        return None

    sword = find_carried_sword()
    assert sword is not None, "no mesh with CHILD_OF -> r_weapon found"
    print("SWORD:", sword.name, "| depot:", sword.get("witcher_path", ""))

    deps = ctx.evaluated_depsgraph_get()
    sample_frames = [0, 55, 100, 150, 199]
    expected_world = {}
    for frame in sample_frames:
        scene.frame_set(frame)
        deps.update()
        expected_world[frame] = sword.evaluated_get(deps).matrix_world.copy()

    assigned = cutscene_bake.assign_prop_slots(scene, [sword])
    assert assigned == [(sword, "Trajectory01")], assigned

    baked = cutscene_bake.bake_cutscene_actors(ctx)
    labels = [str(a.get("cutscene_actor_name", "")) for a, _ in baked]
    print("BAKED:", labels)
    assert len(baked) == 4, labels
    prop_arm = cutscene_bake.find_prop_actor(scene)
    assert prop_arm is not None and prop_arm in [a for a, _ in baked]

    out_props = cutscene_bake.generate_props_entity(ctx, out_path=str(PROPS_ENT))
    assert Path(out_props) == PROPS_ENT, out_props

    issues = cutscene_bake.validate_cutscene_for_export(ctx)
    print("ISSUES AFTER BAKE:")
    for issue in issues:
        print("  -", issue)
    assert not issues, issues

    for frame in sample_frames:
        scene.frame_set(frame)
        deps.update()
        got = prop_arm.evaluated_get(deps).pose.bones["Trajectory01"].matrix
        diff = max(
            abs(a - b)
            for row_a, row_b in zip(got, expected_world[frame])
            for a, b in zip(row_a, row_b)
        )
        assert diff < 1e-4, (frame, diff, got, expected_world[frame])
    print("SLOT-BONE POSE MATCHES SWORD WORLD AT", sample_frames)

    ret = export_cutscene.export_w3_cutscene(ctx, str(OUT))
    assert ret == {'FINISHED'}, ret
    print("EXPORT:", OUT.stat().st_size, "bytes")

    f = read_CR2W(str(OUT))
    anims = []
    names = []
    for ch in f.CHUNKS.CHUNKS:
        if ch.Type == "CSkeletalAnimation":
            dur = name = None
            for p in ch.PROPS or []:
                if p.theName == "duration":
                    dur = p.Value
                if p.theName == "name":
                    name = str(p.ToString())
            anims.append(round(dur or 0, 3))
            names.append(name)
    print("ANIMS:", list(zip(names, anims)))
    assert len(anims) == 4, names
    assert len(set(anims)) == 1, anims
    assert any(str(n or "").startswith("props:Root:") for n in names), names

    imports = [
        str(getattr(imp, "path", None) or getattr(imp, "depotPath", None) or "").lower()
        for imp in (getattr(f, "CR2WImport", []) or [])
    ]
    assert any(p.endswith("trajectories_24.w2rig") for p in imports), imports
    assert any(p.endswith("cs_ciri_drowner_props.w2ent") for p in imports), imports
    print("IMPORTS OK: trajectories_24.w2rig + props .w2ent template referenced")

    # REDkit requires usedInFiles to exactly match the wrapper depot path.
    SCENE_DEPOT = "dlc\\modw3tools_e2e\\data\\scenes\\cs_ciri_drowner.w2scene"
    used = []
    for ch in f.CHUNKS.CHUNKS:
        if ch.Type == "CCutsceneTemplate":
            for p in ch.PROPS or []:
                if p.theName == "usedInFiles":
                    used = [str(el.ToString()) for el in (p.elements or [])]
    print("USED IN FILES:", used)
    assert used == [SCENE_DEPOT], used

    from witcher3_tools.CR2W import scene_builder

    SCENE_OUT = OUT.parent.parent / "scenes" / "cs_ciri_drowner.w2scene"
    SCENE_OUT.parent.mkdir(parents=True, exist_ok=True)
    duration = (scene.frame_end - scene.frame_start) / float(scene.render.fps)
    scene_builder.save_cutscene_wrapper_scene(
        str(SCENE_OUT), "dlc\\modw3tools_e2e\\data\\cutscenes\\cs_ciri_drowner.w2cutscene",
        duration=duration, section_name="cs_ciri_drowner",
    )
    wrapper = read_CR2W(str(SCENE_OUT))
    w_types = [exp.name for exp in wrapper.CR2WExport]
    assert "CStorySceneCutsceneSection" in w_types, w_types
    print(f"WRAPPER OK: {SCENE_OUT} ({SCENE_OUT.stat().st_size} bytes, duration {duration:.3f}s)")

    attachments = cutscene_bake.collect_prop_attachments(scene)
    assert attachments and attachments[0]["slot"] == "Trajectory01", attachments
    ent = read_CR2W(str(PROPS_ENT))
    ent_types = [exp.name for exp in ent.CR2WExport]
    assert ent_types == [
        "CEntityTemplate", "CEntity", "CAnimatedComponent",
        "CHardAttachment", "CSkeletonBoneSlot", "CMeshComponent",
    ], ent_types
    ent_chunk = ent.CHUNKS.CHUNKS[1]
    assert getattr(ent_chunk, "Components", None) == [3, 6], getattr(ent_chunk, "Components", None)
    print(f"PROPS ENTITY OK (native): {PROPS_ENT} ({PROPS_ENT.stat().st_size} bytes), chunks={ent_types}")

    print("W3TB_CUTSCENE_PROP_BAKE_NATIVE_OK")
except Exception:
    traceback.print_exc()
    print("W3TB_CUTSCENE_PROP_BAKE_NATIVE_FAIL")
    sys.exit(1)
finally:
    addon.unregister()
