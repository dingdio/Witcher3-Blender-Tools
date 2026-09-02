import math
import sys
import traceback
from pathlib import Path

import bpy

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

OUT_DIR = REPO_ROOT / "WORKING_TEMP" / "e2e_dlc" / "dlc" / "modw3tools_e2e" / "data"
CS_REPO = "dlc\\modw3tools_e2e\\data\\cutscenes\\cs_e2e_dryrun.w2cutscene"
SCENE_REPO = "dlc\\modw3tools_e2e\\data\\scenes\\cs_e2e_dryrun.w2scene"
LEGACY_SCENE_REPO = "quests\\legacy_dialogue.w2scene"
CS_PATH = OUT_DIR / "cutscenes" / "cs_e2e_dryrun.w2cutscene"
SCENE_PATH = OUT_DIR / "scenes" / "cs_e2e_dryrun.w2scene"

FPS = 30
NUM_FRAMES = 150  # 5 s

import witcher3_tools as addon

addon.register()
try:
    from witcher3_tools.extension_paths import get_dev_override

    prefs = addon._get_prefs(bpy.context)
    uncook_path = str(getattr(prefs, "uncook_path", "") or "")
    if not (Path(uncook_path) / "quests" / "main_npcs" / "cirilla.w2ent").is_file():
        uncook_path = str(get_dev_override("fallback_uncook_path_w3", "") or "")
    prefs.uncook_path = uncook_path
    ciri_path = Path(prefs.uncook_path) / "quests" / "main_npcs" / "cirilla.w2ent"
    assert ciri_path.is_file(), "Configure uncook_path before running this test"

    from witcher3_tools.w3_casting import cast_actor
    from witcher3_tools.exporters import export_cutscene
    from witcher3_tools.CR2W.CR2W_file import read_CR2W
    from witcher3_tools.CR2W.dc_scene import get_cutscene_dialog_lines
    from witcher3_tools import dialog_language
    from witcher3_tools.importers import import_cutscene
    from witcher3_tools.lipsync import redkit_project
    from witcher3_tools.ui import ui_animated_component, ui_cutscene

    scene = bpy.context.scene
    scene.render.fps = FPS
    scene.frame_start = 0
    scene.frame_end = NUM_FRAMES - 1

    ciri, ciri_info = cast_actor("ciri", at=(0.0, 0.0, 0.0))
    drowner, drw_info = cast_actor("drowner", at=(0.0, 2.5, 0.0))
    print(f"E2E cast: {ciri_info['label']} + {drw_info['label']}")

    def find_armature(obj):
        if getattr(obj, "type", None) == 'ARMATURE':
            return obj
        stack = list(getattr(obj, "children", []) or [])
        while stack:
            child = stack.pop()
            if getattr(child, "type", None) == 'ARMATURE':
                return child
            stack.extend(getattr(child, "children", []) or [])
        return None

    def animate(armature, action_name, sway_bone_names):
        armature.animation_data_create()
        action = bpy.data.actions.new(action_name)
        armature.animation_data.action = action
        posed = []
        for name in sway_bone_names:
            bone = armature.pose.bones.get(name)
            if bone is not None:
                posed.append(bone)
        assert posed, f"none of {sway_bone_names} found on {armature.name}"
        for frame in range(0, NUM_FRAMES, 10):
            angle = 0.12 * math.sin(2.0 * math.pi * frame / float(NUM_FRAMES))
            for bone in posed:
                bone.rotation_mode = 'QUATERNION'
                bone.rotation_quaternion = (math.cos(angle / 2), 0.0, 0.0, math.sin(angle / 2))
                bone.keyframe_insert("rotation_quaternion", frame=frame)
        return action

    ciri_arm = find_armature(ciri)
    drw_arm = find_armature(drowner)
    assert ciri_arm is not None and drw_arm is not None, (ciri, drowner)
    ciri_source = animate(ciri_arm, "ciri_sway", ["torso", "torso2", "pelvis"])
    animate(drw_arm, "drowner_sway", ["torso", "torso2", "pelvis", "spine1"])

    scene.witcher_cutscene_export_repo_path = CS_REPO
    scene.witcher_cutscene_used_in_files = (
        LEGACY_SCENE_REPO + "; DLC/MODW3TOOLS_E2E/DATA/SCENES/CS_E2E_DRYRUN.W2SCENE"
    )
    scene.witcher_cutscene_dialog_id_space = 9999
    scene.witcher_cutscene_dialog_lines.clear()
    game_line_id = "337201"
    game_line_text = dialog_language.resolve_localized_text(game_line_id)
    assert game_line_text, f"Could not resolve vanilla GAME line {game_line_id}"
    for speaker, text, tier, line_id, start, end in (
        ("CIRI", "The hunt starts now.", 'SUBTITLE', "", 60, 90),
        ("DROWNER", game_line_text, 'GAME', game_line_id, 12, 75),
    ):
        index = ui_cutscene.add_cutscene_dialog_line(scene, text=text)
        line = scene.witcher_cutscene_dialog_lines[index]
        line.speaker, line.tier = speaker, tier
        line.start_frame, line.end_frame = start, end
        if tier == 'GAME':
            line.game_line_id = line_id
            line.game_voice_file_name = line_id

    CS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SCENE_PATH.parent.mkdir(parents=True, exist_ok=True)

    result = export_cutscene.export_w3_cutscene(bpy.context, str(CS_PATH))
    assert CS_PATH.is_file(), f"cutscene export produced no file ({result})"
    print(f"E2E cutscene written: {CS_PATH} ({CS_PATH.stat().st_size} bytes)")

    real_resolve_export_dir = ui_animated_component._resolve_export_dir
    real_project_path = redkit_project.get_active_project_path
    ui_animated_component._resolve_export_dir = lambda _context: str(REPO_ROOT / "WORKING_TEMP" / "e2e_dlc")
    redkit_project.get_active_project_path = lambda _context: None
    try:
        assert bpy.ops.witcher.cutscene_export_scene_wrapper() == {'FINISHED'}
    finally:
        ui_animated_component._resolve_export_dir = real_resolve_export_dir
        redkit_project.get_active_project_path = real_project_path
    assert SCENE_PATH.is_file()
    strings_path = SCENE_PATH.with_suffix(".strings.csv")
    assert strings_path.is_file() and Path(scene.witcher_cutscene_dialog_strings_path) == strings_path
    assert scene.witcher_cutscene_dialog_lines[0].allocated_line_id == "2119999000"
    assert "2119999000|||The hunt starts now." in strings_path.read_text(encoding="utf-8")
    print(f"E2E wrapper written: {SCENE_PATH} ({SCENE_PATH.stat().st_size} bytes)")

    cs = read_CR2W(str(CS_PATH))
    cs_types = [exp.name for exp in cs.CR2WExport]
    assert "CCutsceneTemplate" in cs_types, cs_types
    anim_entries = [t for t in cs_types if t == "CSkeletalAnimationSetEntry"]
    assert len(anim_entries) >= 2, f"expected >=2 animation entries, got {cs_types}"

    wrapper = read_CR2W(str(SCENE_PATH))
    w_types = [exp.name for exp in wrapper.CR2WExport]
    assert "CStorySceneCutsceneSection" in w_types, w_types
    imports = [
        (getattr(imp, "path", None) or getattr(imp, "depotPath", None))
        for imp in (getattr(wrapper, "CR2WImport", []) or [])
    ]
    assert any(str(p or "").lower().endswith("cs_e2e_dryrun.w2cutscene") for p in imports), imports
    wrapper_lines = get_cutscene_dialog_lines(str(SCENE_PATH), CS_REPO)
    assert [line["actor"] for line in wrapper_lines] == ["CIRI", "DROWNER"]
    assert [line["line_index"] for line in wrapper_lines] == [2119999000, int(game_line_id)]
    assert [round(line["approved_duration"], 3) for line in wrapper_lines] == [1.0, 2.1]

    cutscene_template = import_cutscene.loadCutsceneFile(str(CS_PATH))
    assert list(cutscene_template.usedInFiles or []) == [SCENE_REPO, LEGACY_SCENE_REPO]
    imported_lines = import_cutscene.load_cutscene_dialog_items(str(CS_PATH))
    imported_events = import_cutscene.collect_cutscene_dialog_event_frames(str(CS_PATH))
    assert [line["actor"] for line in imported_lines] == ["CIRI", "DROWNER"]
    assert [line["line_index"] for line in imported_lines] == [2119999000, int(game_line_id)]
    assert imported_lines[0]["line_text"] == "The hunt starts now."
    assert [round(line["approved_duration"], 3) for line in imported_lines] == [1.0, 2.1]
    scene.witcher_cutscene_dialog_lines.clear()
    ui_cutscene._populate_cutscene_dialog_items(scene, imported_lines, imported_events)
    assert [
        (item.actor, item.start_frame, item.end_frame)
        for item in scene.witcher_cutscene_dialog_items
    ] == [("CIRI", 60, 90), ("DROWNER", 12, 75)]
    assert ui_cutscene.copy_cutscene_preview_to_authored(scene) == 2
    subtitle, game = scene.witcher_cutscene_dialog_lines
    assert (subtitle.tier, subtitle.allocated_line_id, subtitle.text) == (
        'SUBTITLE', "2119999000", "The hunt starts now.",
    )
    assert (game.tier, game.game_line_id, game.game_voice_file_name, game.text) == (
        'GAME', game_line_id, game_line_id, game_line_text,
    )
    assert [(line.start_frame, line.end_frame) for line in (subtitle, game)] == [(60, 90), (12, 75)]

    dialog_roots = [
        event for event in (import_cutscene.loadCutsceneFile(str(CS_PATH)).animevents or [])
        if str(getattr(event, "type_name", "")) == export_cutscene.CUTSCENE_DIALOG_EVENT_TYPE
    ]
    assert [round(float(event.start_time), 3) for event in dialog_roots] == [2.0, 0.4]
    assert all(event.animation_name == export_cutscene.CUTSCENE_ROOT_ANIMATION_NAME for event in dialog_roots)

    print(f"E2E parse-back OK: cutscene chunks={len(cs_types)} "
          f"(anim entries={len(anim_entries)}), wrapper chunks={len(w_types)}")
    from witcher3_tools.animation import cutscene_bake
    from witcher3_tools.animation.action_compat import iter_action_fcurves
    OP_PATH = OUT_DIR / "cutscenes" / "cs_e2e_dryrun_op.w2cutscene"
    OP_REBAKE_PATH = OUT_DIR / "cutscenes" / "cs_e2e_dryrun_rebake.w2cutscene"
    OP_NO_BAKE_PATH = OUT_DIR / "cutscenes" / "cs_e2e_dryrun_no_bake.w2cutscene"
    OP_DIALOGUE_INVALID_PATH = OUT_DIR / "cutscenes" / "cs_e2e_dryrun_dialogue_invalid.w2cutscene"
    OP_VALIDATE_FAIL_PATH = OUT_DIR / "cutscenes" / "cs_e2e_dryrun_validate_fail.w2cutscene"
    OP_WRITE_FAIL_PATH = OUT_DIR / "cutscenes" / "cs_e2e_dryrun_write_fail.w2cutscene"
    OP_CANCELLED_PATH = OUT_DIR / "cutscenes" / "cs_e2e_dryrun_cancelled.w2cutscene"
    OP_PLAN_FAIL_PATH = OUT_DIR / "cutscenes" / "cs_e2e_dryrun_plan_fail.w2cutscene"
    fade = scene.witcher_cutscene_event_items.add()
    fade.event_type, fade.event_name, fade.event_scope, fade.source_index = "CExtAnimCutsceneFadeEvent", "e2e_fade", "ROOT", -1
    fade.start_time, fade.duration = 1.25, 0.5
    assert not cutscene_bake.bake_state(scene)["baked"]
    assert bpy.ops.witcher.export_w2_cutscene(filepath=str(OP_PATH), bake_before_export=True) == {'FINISHED'}, \
        scene.witcher_cutscene_validation_report
    assert cutscene_bake.bake_state(scene)["baked"] and not cutscene_bake.bake_state(scene)["stale"]
    op_types = [exp.name for exp in read_CR2W(str(OP_PATH)).CR2WExport]
    assert op_types.count("CSkeletalAnimationSetEntry") == 2, op_types
    roots = list(getattr(import_cutscene.loadCutsceneFile(str(OP_PATH)), "animevents", None) or [])
    assert any(str(getattr(e, "type_name", "")) == "CExtAnimCutsceneFadeEvent"
               and abs(float(getattr(e, "start_time", 0.0) or 0.0) - 1.25) < 1e-4 for e in roots), \
        [(getattr(e, "type_name", None), getattr(e, "start_time", None)) for e in roots]
    assert not any(l.startswith("ERROR") for l in scene.witcher_cutscene_validation_report.splitlines())

    def baked_curve_value(armature, data_path, array_index, frame):
        action = next(
            strip.action
            for track in armature.animation_data.nla_tracks
            for strip in track.strips
            if strip.action is not None and strip.action.get(cutscene_bake.BAKED_ACTION_TAG)
        )
        curve = next(
            fc for fc in iter_action_fcurves(action, target=armature)
            if fc.data_path == data_path and fc.array_index == array_index
        )
        return float(curve.evaluate(frame))

    def native_animation_signature(path, actor_name):
        template = import_cutscene.loadCutsceneFile(str(path))
        node = next(
            item for item in template.animations
            if str(item.animation.name).split(":", 1)[0].lower() == actor_name.lower()
        )
        parts = list(getattr(node.animation.animBuffer, "parts", None) or [node.animation.animBuffer])
        return tuple(
            (
                int(part.numFrames),
                tuple(
                    (
                        int(bone.id),
                        tuple(tuple(round(float(value), 6) for value in frame) for frame in bone.positionFrames),
                        tuple(
                            tuple(
                                round(float(getattr(rotation, name, getattr(rotation, name.lower(), 0.0))), 6)
                                for name in ("X", "Y", "Z", "W")
                            )
                            for rotation in (bone.rotationFramesQuat or bone.rotationFrames)
                        ),
                    )
                    for bone in part.bones
                ),
            )
            for part in parts
        )

    def bake_scene_signature():
        def matrix_values(matrix):
            return tuple(round(float(value), 8) for row in matrix for value in row)

        holders = {}
        for actor in cutscene_bake.iter_cutscene_actor_armatures(scene):
            for holder in cutscene_bake._object_chain(actor):
                holders[holder.as_pointer()] = holder
        object_rows = []
        action_targets = {}
        for holder in sorted(holders.values(), key=lambda item: item.name):
            anim_data = holder.animation_data
            if anim_data and anim_data.action:
                action_targets.setdefault(anim_data.action.as_pointer(), holder)
            tracks = tuple(
                (
                    track.name,
                    bool(track.mute),
                    tuple(
                        (
                            strip.name,
                            bool(strip.mute),
                            strip.action.as_pointer() if strip.action else 0,
                            round(float(strip.frame_start), 6),
                            round(float(strip.frame_end), 6),
                        )
                        for strip in track.strips
                    ),
                )
                for track in (anim_data.nla_tracks if anim_data else [])
            )
            if anim_data:
                for track in anim_data.nla_tracks:
                    for strip in track.strips:
                        if strip.action:
                            action_targets.setdefault(strip.action.as_pointer(), holder)
            object_rows.append((
                holder.name,
                holder.animation_data.action.as_pointer() if anim_data and anim_data.action else 0,
                tracks,
                str(holder.get(cutscene_bake.PREBAKE_STATE_PROP, "")),
                matrix_values(holder.matrix_basis),
                matrix_values(holder.matrix_parent_inverse),
                tuple(bool(constraint.enabled) for constraint in holder.constraints),
                tuple(
                    (bone.name, tuple(bool(constraint.enabled) for constraint in bone.constraints))
                    for bone in (holder.pose.bones if holder.type == 'ARMATURE' else [])
                ),
                tuple(bool(driver.mute) for driver in (anim_data.drivers if anim_data else [])),
            ))
        def action_curve_signature(action):
            target = action_targets.get(action.as_pointer())
            return tuple(sorted(
                (
                    curve.data_path,
                    int(curve.array_index),
                    str(curve.extrapolation),
                    tuple(
                        (
                            tuple(round(float(value), 8) for value in point.co),
                            tuple(round(float(value), 8) for value in point.handle_left),
                            tuple(round(float(value), 8) for value in point.handle_right),
                            str(point.interpolation),
                        )
                        for point in curve.keyframe_points
                    ),
                )
                for curve in iter_action_fcurves(action, target=target)
            ))

        baked_actions = tuple(sorted(
            (action.as_pointer(), action.name, bool(action.use_fake_user), action_curve_signature(action))
            for action in bpy.data.actions
            if action.get(cutscene_bake.BAKED_ACTION_TAG)
        ))
        return (
            int(scene.frame_start),
            int(scene.frame_end),
            int(scene.frame_current),
            round(float(scene.frame_subframe), 6),
            str(scene.get(cutscene_bake.BAKE_FINGERPRINT_PROP, "")),
            tuple(object_rows),
            baked_actions,
        )

    def assert_export_cancelled(expected_error, **kwargs):
        output_path = Path(kwargs["filepath"])
        output_before = output_path.read_bytes() if output_path.exists() else None
        try:
            result = bpy.ops.witcher.export_w2_cutscene(**kwargs)
        except RuntimeError as exc:
            assert expected_error in str(exc), exc
        else:
            assert result == {'CANCELLED'}, result
        output_after = output_path.read_bytes() if output_path.exists() else None
        assert output_after == output_before, output_path

    source_curves = list(iter_action_fcurves(ciri_source, target=ciri_arm))
    source_curve = next(
        fc for fc in source_curves
        if fc.data_path.endswith(".rotation_quaternion") and fc.array_index == 3 and len(fc.keyframe_points) > 2
    )
    source_key = source_curve.keyframe_points[len(source_curve.keyframe_points) // 3]
    source_frame = float(source_key.co[0])
    fingerprint = cutscene_bake.bake_fingerprint(scene)
    baked_before = baked_curve_value(ciri_arm, source_curve.data_path, source_curve.array_index, source_frame)
    native_before = native_animation_signature(OP_PATH, "ciri")
    bytes_before = OP_PATH.read_bytes()

    source_key.co[1] = float(source_key.co[1]) + 0.35
    source_curve.update()
    ciri_source.update_tag()
    bpy.context.view_layer.update()
    scene.frame_start = 7
    export_range = (int(scene.frame_start), int(scene.frame_end))
    assert cutscene_bake.bake_fingerprint(scene) == fingerprint
    assert cutscene_bake.bake_state(scene)["baked"] and not cutscene_bake.bake_state(scene)["stale"]

    bake_calls = [0]
    real_bake = cutscene_bake.bake_cutscene_actors

    def counted_bake(*args, **kwargs):
        bake_calls[0] += 1
        return real_bake(*args, **kwargs)

    cutscene_bake.bake_cutscene_actors = counted_bake
    try:
        assert bpy.ops.witcher.export_w2_cutscene(
            filepath=str(OP_REBAKE_PATH), bake_before_export=True,
        ) == {'FINISHED'}, scene.witcher_cutscene_validation_report
        assert bake_calls[0] == 1, bake_calls
        assert (int(scene.frame_start), int(scene.frame_end)) == export_range
        baked_after = baked_curve_value(ciri_arm, source_curve.data_path, source_curve.array_index, source_frame)
        native_after = native_animation_signature(OP_REBAKE_PATH, "ciri")
        bytes_after = OP_REBAKE_PATH.read_bytes()
        assert not math.isclose(baked_after, baked_before, abs_tol=1e-4), (baked_before, baked_after)
        assert native_after != native_before and bytes_after != bytes_before

        source_key.co[1] = float(source_key.co[1]) + 0.35
        source_curve.update()
        ciri_source.update_tag()
        bpy.context.view_layer.update()
        assert cutscene_bake.bake_fingerprint(scene) == fingerprint
        assert bpy.ops.witcher.export_w2_cutscene(
            filepath=str(OP_NO_BAKE_PATH), bake_before_export=False,
        ) == {'FINISHED'}, scene.witcher_cutscene_validation_report
        assert bake_calls[0] == 1, bake_calls
        assert math.isclose(
            baked_curve_value(ciri_arm, source_curve.data_path, source_curve.array_index, source_frame),
            baked_after,
            abs_tol=1e-6,
        )
        assert native_animation_signature(OP_NO_BAKE_PATH, "ciri") == native_after
        assert "WARN Bake before export is off; current baked output may be stale." in \
            scene.witcher_cutscene_validation_report.splitlines()

        actor_type = str(ciri_arm.get("cutscene_actor_type", ""))
        ciri_arm["cutscene_actor_type"] = "BROKEN"
        invalid_signature = bake_scene_signature()
        assert_export_cancelled(
            "invalid actor type",
            filepath=str(OP_NO_BAKE_PATH), bake_before_export=False,
        )
        assert bake_scene_signature() == invalid_signature
        assert "invalid actor type" in scene.witcher_cutscene_validation_report
        ciri_arm["cutscene_actor_type"] = actor_type

        original_game_line_id = game.game_line_id
        game.game_line_id = "not-a-number"
        dialogue_invalid_signature = bake_scene_signature()
        OP_DIALOGUE_INVALID_PATH.write_bytes(b"dialogue-invalid-sentinel")
        try:
            assert_export_cancelled(
                "valid numeric Game Line ID",
                filepath=str(OP_DIALOGUE_INVALID_PATH), bake_before_export=True,
            )
            assert bake_scene_signature() == dialogue_invalid_signature
            assert "valid numeric Game Line ID" in scene.witcher_cutscene_validation_report
        finally:
            game.game_line_id = original_game_line_id

        from witcher3_tools.animation import cutscene_validate
        scene.frame_start = 7
        scene.frame_end = NUM_FRAMES - 1
        scene.frame_set(23)
        cancelled_signature = bake_scene_signature()
        real_collect = cutscene_validate.collect_issues

        def forced_validation_failure(context, **kwargs):
            return real_collect(context, **kwargs) + [
                cutscene_validate.issue("ERROR", "forced post-bake validation failure"),
            ]

        cutscene_validate.collect_issues = forced_validation_failure
        try:
            assert_export_cancelled(
                "forced post-bake validation failure",
                filepath=str(OP_VALIDATE_FAIL_PATH), bake_before_export=True,
            )
        finally:
            cutscene_validate.collect_issues = real_collect
        assert any(i.severity == "ERROR" and i.message == "forced post-bake validation failure"
                   for i in scene.witcher_cutscene_validation_issues)
        assert bake_scene_signature() == cancelled_signature

        real_writer = export_cutscene.export_w3_cutscene

        def forced_write_failure(*_args, **_kwargs):
            raise RuntimeError("forced cutscene write failure")

        export_cutscene.export_w3_cutscene = forced_write_failure
        try:
            assert_export_cancelled(
                "forced cutscene write failure",
                filepath=str(OP_WRITE_FAIL_PATH), bake_before_export=True,
            )
        finally:
            export_cutscene.export_w3_cutscene = real_writer
        assert bake_scene_signature() == cancelled_signature

        from witcher3_tools.ui import ui_anims as ui_anims_module
        real_generated_plan = ui_anims_module._cutscene_generated_actor_template_entries
        planning_calls = [0]

        def forced_post_bake_plan_failure(context):
            planning_calls[0] += 1
            if planning_calls[0] == 2:
                raise RuntimeError("forced generated actor planning failure")
            return real_generated_plan(context)

        ui_anims_module._cutscene_generated_actor_template_entries = forced_post_bake_plan_failure
        try:
            assert_export_cancelled(
                "forced generated actor planning failure",
                filepath=str(OP_PLAN_FAIL_PATH), bake_before_export=True,
            )
        finally:
            ui_anims_module._cutscene_generated_actor_template_entries = real_generated_plan
        assert planning_calls[0] == 2, planning_calls
        assert bake_scene_signature() == cancelled_signature

        def forced_write_cancelled(*_args, **_kwargs):
            return {'CANCELLED'}

        export_cutscene.export_w3_cutscene = forced_write_cancelled
        try:
            assert_export_cancelled(
                "Cutscene export cancelled",
                filepath=str(OP_CANCELLED_PATH), bake_before_export=True,
            )
        finally:
            export_cutscene.export_w3_cutscene = real_writer
        assert bake_scene_signature() == cancelled_signature
    finally:
        cutscene_bake.bake_cutscene_actors = real_bake

    print("W3TB_CUTSCENE_E2E_DRYRUN_OK")
except Exception:
    traceback.print_exc()
    print("W3TB_CUTSCENE_E2E_DRYRUN_FAIL")
    sys.exit(1)
finally:
    addon.unregister()
