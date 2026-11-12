"""Check Mixamo preview, bake, IK markers and native export.

  blender --background --factory-startup --python tests/blender_mixamo_retarget_native.py
"""
import math
import os
import sys
import traceback
from pathlib import Path

import bpy
from mathutils import Vector

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
import witcher3_tools as addon  # noqa: E402

FBX = REPO_ROOT / "DEV_STUFF" / "mixamo" / "Silly Dancing.fbx"
OUT = REPO_ROOT / "WORKING_TEMP" / "mixamo_retarget"
RENDER = "--render" in sys.argv

if not FBX.is_file():
    print(f"{FBX} missing, skipped W3TB_MIXAMO_RETARGET_OK")
    sys.exit(0)
OUT.mkdir(parents=True, exist_ok=True)
addon.register()
try:
    from witcher3_tools.animation import mixamo_retarget as retarget
    from witcher3_tools.controller.session import player_armature
    from witcher3_tools.CR2W.dc_anims import load_bin_anims_single
    from witcher3_tools.exporters.export_anims import W3AnimationExporter
    from witcher3_tools.extension_paths import get_dev_override

    context = bpy.context
    scene = context.scene
    prefs = addon._get_prefs(context)
    rel_player = os.path.join("gameplay", "templates", "characters", "player", "player.w2ent")
    if not (Path(prefs.uncook_path or "") / rel_player).is_file():
        prefs.uncook_path = str(get_dev_override("fallback_uncook_path_w3", "") or "")
    if "Cube" in bpy.data.objects:
        bpy.data.objects.remove(bpy.data.objects["Cube"])
    assert bpy.ops.witcher.import_geralt(inventory_preset_id="__none__") == {'FINISHED'}
    arm = player_armature(context)
    bpy.ops.import_scene.fbx(filepath=str(FBX))
    source = next(o for o in scene.objects if o.type == 'ARMATURE' and any(b.name.startswith("mixamorig:") for b in o.data.bones))
    assert scene.render.fps == 30

    s = scene.witcher_mixamo_retarget
    s.source, s.target = source, arm
    assert bpy.ops.witcher.retarget_auto_map() == {'FINISHED'}
    rows = {b.target: (b.source.rsplit(":", 1)[-1], b.source2.rsplit(":", 1)[-1], b.mix) for b in s.bones}
    print(f"{len(rows)} rows, scale {s.scale:.3f}:", {t: v for t, v in rows.items() if not any(f in t for f in retarget.FINGERS)})
    assert rows["pelvis"][0] == "Hips" and rows["torso"][0] == "Spine" and rows["torso3"][0] == "Spine2"
    assert rows["neck"][0] == "Neck" and rows["head"][0] == "Head"
    assert rows["l_bicep"][0] == "LeftArm" and rows["r_shin"][0] == "RightLeg" and rows["l_toe"][0] == "LeftToeBase"
    assert rows["l_index2"][0] == "LeftHandIndex2" and rows["r_thumb1"][0] == "RightHandThumb1"
    assert "Root" not in rows and "Trajectory" not in rows and not any("Roll" in t or "roll" in t for t in rows)
    assert len(rows) == 6 + 16 + 30, len(rows)
    assert 5.0 < s.scale < 15.0, s.scale
    s.name = "mixamo_silly_dancing"

    rows_full = [(b.target, b.source, b.source2, b.mix) for b in s.bones]
    assert abs(retarget.facing_turn(source, arm, rows_full)) < math.radians(1.0), "Auto Map turns the source to face the target"

    assert bpy.ops.witcher.retarget_preview() == {'FINISHED'}
    assert retarget.preview_active(arm)
    watch = ("pelvis", "head", "l_foot", "r_foot", "l_hand", "r_index3")
    frames = list(retarget.frame_range(source, scene))
    probe = (frames[0], frames[len(frames) // 3], frames[2 * len(frames) // 3], frames[-1])

    def world(obj, names):
        return {n: obj.matrix_world @ obj.pose.bones[n].head for n in names}

    previewed = {}
    for f in probe:
        scene.frame_set(f)
        previewed[f] = world(arm, watch)

    s.root_motion = False
    assert bpy.ops.witcher.retarget_bake() == {'FINISHED'}
    assert not retarget.preview_active(arm) and not [o for o in scene.objects if o.name.startswith(retarget.PREVIEW)]
    action = arm.animation_data.action
    assert action.name == "mixamo_silly_dancing" and tuple(action.frame_range) == (frames[0], frames[-1])
    for f, expected in previewed.items():
        scene.frame_set(f)
        error = max((world(arm, watch)[n] - p).length for n, p in expected.items())
        print(f"frame {f}: baked vs preview {error * 1000:.2f} mm")
        assert error < 2e-3, "the bake must reproduce the preview constraints"

    rolls = [pb for pb in arm.pose.bones if "oll" in pb.name and pb.name not in rows]
    worst = {"feet": 0.0, "head": 9.0, "hands": 9.0, "roll": 0.0, "ik": 0.0}
    for f in frames:
        scene.frame_set(f)
        p = world(arm, ("pelvis", "head", "l_foot", "r_foot", "l_hand", "r_hand", "l_bicep", "r_bicep"))
        feet = (p["l_foot"] + p["r_foot"]) / 2
        worst["feet"] = max(worst["feet"], (feet - p["pelvis"]).to_2d().length)
        assert p["l_foot"].z < p["pelvis"].z - 0.4 and p["r_foot"].z < p["pelvis"].z - 0.4, f"frame {f}: feet not below the hips"
        worst["head"] = min(worst["head"], p["head"].z - p["pelvis"].z)
        side = (p["l_bicep"] - p["r_bicep"]).normalized()
        worst["hands"] = min(worst["hands"], (p["l_hand"] - p["pelvis"]).dot(side), -(p["r_hand"] - p["pelvis"]).dot(side))
        worst["roll"] = max([worst["roll"]] + [math.degrees(pb.matrix_basis.to_quaternion().angle) for pb in rolls])
        for ik, bone in retarget.ik_markers(arm).items():
            worst["ik"] = max(worst["ik"], (arm.pose.bones[ik].matrix.translation - arm.pose.bones[bone].matrix.translation).length,
                              arm.pose.bones[ik].matrix.to_quaternion().rotation_difference(arm.pose.bones[bone].matrix.to_quaternion()).angle)
    print("worst over the clip:", {k: round(v, 4) for k, v in worst.items()})
    assert worst["feet"] < 0.45 and worst["head"] > 0.4 and worst["hands"] > -0.05
    assert worst["roll"] < 1e-4, "roll helpers stay at rest, as in game clips"
    assert worst["ik"] < 1e-4, "IK markers sit on their bones"
    left_side = (world(arm, ("l_bicep",))["l_bicep"] - world(arm, ("r_bicep",))["r_bicep"])
    assert left_side.x < 0.0, "Geralt's left side stays at -X: facing +Y like the rig"

    scene.frame_set(frames[-1])
    pelvis_end = arm.pose.bones["pelvis"].matrix.translation.copy()
    s.root_motion = True
    assert bpy.ops.witcher.retarget_bake() == {'FINISHED'}
    action = arm.animation_data.action
    assert action.name.startswith("mixamo_silly_dancing")
    traj = arm.pose.bones["Trajectory"]
    scene.frame_set(frames[0])
    t0 = traj.matrix.translation.copy()
    scene.frame_set(frames[-1])
    travel = traj.matrix.translation - t0
    split = (arm.pose.bones["pelvis"].matrix.translation - pelvis_end).length
    print(f"Trajectory travel {travel.length:.3f} m, pelvis vs bake without root motion {split * 1000:.2f} mm")
    assert abs(travel.z) < 1e-6 and split < 1e-4

    exporter = W3AnimationExporter()
    out = OUT / "mixamo_silly_dancing.w2anims"
    arm.select_set(True)
    context.view_layer.objects.active = arm
    exporter.export_native(armature=arm, filepath=str(out), use_frame_range=True, single_action=True,
                           skeletal_type="SAT_Normal", additive_type=None, include_motion_extraction=True, action=action)
    rig = os.path.join(prefs.uncook_path, "characters", "base_entities", "man_base", "man_base.w2rig")
    anim_set = load_bin_anims_single(str(out), action.name, rigPath=rig)
    entry = next(e for e in anim_set.animations if e.animation.name == action.name)
    buf = entry.animation.animBuffer
    names = [b.BoneName for b in buf.bones]
    print(f"exported {out.name}: {len(names)} bones, {buf.numFrames} frames, {entry.animation.duration:.2f} s")
    assert set(names) >= {"pelvis", "Trajectory", "l_index2", "IK_l_foot"} and buf.numFrames == len(frames)

    if RENDER:
        source.scale = source.scale * s.scale
        source.location.x += 1.0
        scene.render.engine = 'BLENDER_WORKBENCH'
        scene.render.resolution_x, scene.render.resolution_y = 900, 600
        bpy.ops.object.camera_add(location=(0.5, 6.0, 1.0))
        camera = context.object
        camera.rotation_euler = (Vector((0.5, 0.0, 0.9)) - camera.location).to_track_quat('-Z', 'Y').to_euler()
        camera.data.type, camera.data.ortho_scale = 'ORTHO', 3.8
        scene.camera = camera
        for f in probe:
            scene.frame_set(f)
            scene.render.filepath = str(OUT / f"front_{f:03d}.png")
            bpy.ops.render.render(write_still=True)
    print("W3TB_MIXAMO_RETARGET_OK")
except Exception:
    traceback.print_exc()
    print("W3TB_MIXAMO_RETARGET_FAIL")
    sys.exit(1)
