import math
import sys
import traceback
from pathlib import Path

import bpy
from mathutils import Vector

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

OUT_PATH = REPO_ROOT / "WORKING_TEMP" / "e2e_dlc" / "dlc" / "modw3tools_e2e" / "data" / "cutscenes" / "cs_multipart.w2cutscene"
FPS = 30
NUM_FRAMES = 150
CUTS = [0, 50, 110]
SAMPLE_FRAMES = [0, 49, 50, 110, 149]

import witcher3_tools as addon

addon.register()
try:
    from witcher3_tools.extension_paths import get_dev_override

    prefs = addon._get_prefs(bpy.context)
    uncook_path = str(getattr(prefs, "uncook_path", "") or "")
    if not (Path(uncook_path) / "quests" / "main_npcs" / "cirilla.w2ent").is_file():
        uncook_path = str(get_dev_override("fallback_uncook_path_w3", "") or "")
    prefs.uncook_path = uncook_path
    assert (Path(uncook_path) / "gameplay" / "camera" / "scene_camera.w2ent").is_file(), \
        "Configure uncook_path before running this test"

    from witcher3_tools.animation import cutscene_bake
    from witcher3_tools.importers import import_cutscene
    from witcher3_tools.w3_casting import cast_actor

    scene = bpy.context.scene
    scene.render.fps = FPS
    scene.frame_start, scene.frame_end = 0, NUM_FRAMES - 1
    scene.witcher_cutscene_export_repo_path = r"dlc\modw3tools_e2e\data\cutscenes\cs_multipart.w2cutscene"

    # Bake must preserve actor placement and yaw in Root.
    ciri, _info = cast_actor("ciri", at=(1.0, 2.0, 0.0))
    ciri.rotation_euler.z = math.radians(180.0)
    ciri_arm = next(obj for obj in [ciri, *ciri.children_recursive] if obj.type == 'ARMATURE')
    ciri_arm.animation_data_create()
    ciri_arm.animation_data.action = bpy.data.actions.new("ciri_sway")
    posed = [ciri_arm.pose.bones[name] for name in ("torso", "torso2", "pelvis") if name in ciri_arm.pose.bones]
    assert posed
    for frame in range(0, NUM_FRAMES, 10):
        angle = 0.12 * math.sin(2.0 * math.pi * frame / float(NUM_FRAMES))
        for bone in posed:
            bone.rotation_mode = 'QUATERNION'
            bone.rotation_quaternion = (math.cos(angle / 2), 0.0, 0.0, math.sin(angle / 2))
            bone.keyframe_insert("rotation_quaternion", frame=frame)

    for frame in CUTS:
        scene.frame_set(frame)
        assert bpy.ops.witcher.cutscene_new_shot() == {'FINISHED'}
    assert [r[2:] for r in cutscene_bake.shot_ranges(scene)] == [(0, 49), (50, 109), (110, 149)]

    def world_pose(armature):
        rows = {}
        for frame in SAMPLE_FRAMES:
            bpy.context.scene.frame_set(frame)
            bpy.context.view_layer.update()
            pelvis = armature.matrix_world @ armature.pose.bones["pelvis"].matrix
            trajectory = armature.matrix_world @ armature.pose.bones["Trajectory"].matrix
            rows[frame] = [*pelvis.to_translation(), *(trajectory.to_3x3() @ Vector((0.0, 1.0, 0.0)))]
        return rows

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    assert bpy.ops.witcher.export_w2_cutscene(filepath=str(OUT_PATH), bake_before_export=True) == {'FINISHED'}, \
        scene.witcher_cutscene_validation_report
    assert not cutscene_bake.shots_stale(scene)
    source_pose = world_pose(ciri_arm)

    # Adjacent parts share their boundary frame.
    expected_first = CUTS
    expected_parts = [CUTS[1] - CUTS[0] + 1, CUTS[2] - CUTS[1] + 1, NUM_FRAMES - CUTS[2]]
    template = import_cutscene.loadCutsceneFile(str(OUT_PATH))
    layouts = {}
    for node in template.animations:
        buffer = node.animation.animBuffer
        parts = list(getattr(buffer, "parts", None) or [])
        layouts[str(node.animation.name)] = (
            [int(frame) for frame in (buffer.firstFrames if parts else [])],
            [int(part.numFrames) for part in parts],
            int(buffer.numFrames),
        )
    assert {name.split(":", 1)[0].lower() for name in layouts} >= {"camera", "ciri"}, layouts
    for name, layout in layouts.items():
        assert layout == (expected_first, expected_parts, NUM_FRAMES), (name, layout)

    def key(frames, index):
        return frames[min(index, len(frames) - 1)]  # constant channels keep a single key

    def pose_at(part, index):
        rows = []
        for bone in part.bones:
            rotation = key(bone.rotationFramesQuat or bone.rotationFrames, index)
            rows.append((
                int(bone.id),
                [float(value) for value in key(bone.positionFrames, index)],
                [float(getattr(rotation, axis, getattr(rotation, axis.lower(), 0.0))) for axis in ("X", "Y", "Z", "W")],
            ))
        return rows

    for name in layouts:
        node = next(item for item in template.animations if str(item.animation.name) == name)
        parts = node.animation.animBuffer.parts
        for prev, nxt in zip(parts, parts[1:]):
            for (prev_id, prev_pos, prev_rot), (next_id, next_pos, next_rot) in zip(
                    pose_at(prev, int(prev.numFrames) - 1), pose_at(nxt, 0)):
                assert prev_id == next_id, name
                assert all(abs(a - b) < 2e-3 for a, b in zip(prev_pos + prev_rot, next_pos + next_rot)), \
                    (name, prev_id, prev_pos, next_pos, prev_rot, next_rot)
    print(f"Multipart layouts: {layouts}")

    # Static camera parts must survive the round-trip too.
    bpy.ops.wm.read_homefile(use_empty=True)
    bpy.context.scene.render.fps = FPS
    assert import_cutscene.import_w3_cutscene(
        str(OUT_PATH), auto_apply_selected_animations=True, import_burned_audio=False,
    ) is not None
    actors = {
        str(obj.get("cutscene_actor_name", "")).lower(): obj
        for obj in bpy.context.scene.objects
        if obj.type == 'ARMATURE' and obj.get("cutscene_actor_name")
    }
    assert set(actors) >= {"camera", "ciri"}, actors
    expected_strips = [(CUTS[0], CUTS[1]), (CUTS[1], CUTS[2]), (CUTS[2], NUM_FRAMES - 1)]
    for name, armature in actors.items():
        strips = sorted(
            (int(strip.frame_start), int(strip.frame_end))
            for track in armature.animation_data.nla_tracks for strip in track.strips
        )
        assert strips == expected_strips, (name, strips)
    imported_pose = world_pose(actors["ciri"])
    for frame in SAMPLE_FRAMES:
        assert all(abs(a - b) < 0.02 for a, b in zip(source_pose[frame], imported_pose[frame])), \
            (frame, source_pose[frame], imported_pose[frame])

    print("W3TB_CUTSCENE_MULTIPART_OK")
except Exception:
    traceback.print_exc()
    print("W3TB_CUTSCENE_MULTIPART_FAIL")
    sys.exit(1)
finally:
    addon.unregister()
