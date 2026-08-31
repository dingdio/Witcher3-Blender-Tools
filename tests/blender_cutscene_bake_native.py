import sys
import traceback
from pathlib import Path

import bpy

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

BLEND = REPO_ROOT / "WORKING_TEMP" / "e2e_dlc" / "cs_ciri_drowner.blend"
OUT = REPO_ROOT / "WORKING_TEMP" / "e2e_dlc" / "dlc" / "modw3tools_e2e" / "data" / "cutscenes" / "cs_ciri_drowner.w2cutscene"

assert BLEND.is_file(), f"Missing integration fixture: {BLEND}. Save cs_ciri_drowner.blend there before running this test."
bpy.ops.wm.open_mainfile(filepath=str(BLEND))

import witcher3_tools as addon

addon.register()
try:
    from witcher3_tools.animation import cutscene_bake, cutscene_validate
    from witcher3_tools.exporters import export_cutscene
    from witcher3_tools.CR2W.CR2W_file import read_CR2W

    ctx = bpy.context
    fixture_scene = ctx.scene
    transaction_scene = bpy.data.scenes.new("W3TB_transaction_regressions")
    ctx.window.scene = transaction_scene
    transaction_scene.frame_start = 1
    transaction_scene.frame_end = 2

    def make_armature(name, bone_name=None):
        armature = bpy.data.armatures.new(f"{name}_data")
        obj = bpy.data.objects.new(name, armature)
        transaction_scene.collection.objects.link(obj)
        if bone_name:
            for candidate in ctx.view_layer.objects:
                candidate.select_set(False)
            obj.select_set(True)
            ctx.view_layer.objects.active = obj
            bpy.ops.object.mode_set(mode='EDIT')
            bone = armature.edit_bones.new(bone_name)
            bone.tail = (0.0, 0.0, 1.0)
            bpy.ops.object.mode_set(mode='OBJECT')
        return obj

    empty_parent = make_armature("W3TB_empty_parent")
    camera = make_armature("W3TB_camera", "Camera_Node")
    camera.parent = empty_parent
    camera["cutscene_actor_name"] = "Camera"
    camera["cutscene_actor_type"] = "CAT_Camera"
    camera["cutscene_actor_template"] = "gameplay\\camera\\scene_camera.w2ent"
    camera.animation_data_create()
    camera_action = bpy.data.actions.new("W3TB_camera_active_action")
    camera.animation_data.action = camera_action
    camera_bone = camera.pose.bones["Camera_Node"]
    camera_bone.location = (0.0, 0.0, 0.0)
    camera_bone.keyframe_insert("location", frame=1)
    camera_bone.location = (1.0, 0.0, 0.0)
    camera_bone.keyframe_insert("location", frame=2)

    camera_message = "Camera has no cutscene NLA strips."
    _lines, setup_errors, setup_warnings = cutscene_validate.validate_cutscene_setup(ctx)
    assert camera_message not in setup_errors and camera_message in setup_warnings
    _lines, full_errors, _warnings = cutscene_validate.validate_cutscene(ctx)
    assert any("no active cutscene_anim strips" in issue for issue in full_errors), full_errors
    transaction = cutscene_bake.begin_bake_transaction(transaction_scene)
    try:
        cutscene_bake.bake_cutscene_actors(ctx, frame_start=1, frame_end=2)
        assert not cutscene_validate.validate_cutscene(ctx)[1]
    finally:
        transaction.rollback()
    assert camera.animation_data.action is camera_action
    assert not camera.animation_data.nla_tracks
    assert cutscene_bake.PREBAKE_STATE_PROP not in empty_parent

    def keyed_camera_action(name, values):
        action = bpy.data.actions.new(name)
        camera.animation_data.action = action
        for frame, value in enumerate(values, 1):
            camera_bone.location = (*value, 0.0)
            camera_bone.keyframe_insert("location", index=0, frame=frame)
            camera_bone.keyframe_insert("location", index=1, frame=frame)
        camera.animation_data.action = None
        return action

    import_action = keyed_camera_action("W3TB_import_source", ((2.0, 7.0), (4.0, 9.0)))
    cutscene_action = import_action.copy()
    cutscene_action.name = "W3TB_cutscene_copy"
    for curve in cutscene_bake._action_fcurves(import_action):
        for point in curve.keyframe_points:
            point.co[1] += 100.0
        curve.update()
    camera_bone.location = (0.0, 0.0, 0.0)
    cutscene_track = camera.animation_data.nla_tracks.new()
    cutscene_track.name = cutscene_bake.CUTSCENE_TRACK_NAME
    cutscene_strip = cutscene_track.strips.new(cutscene_action.name, 1, cutscene_action)
    cutscene_strip.blend_type = 'COMBINE'
    import_track = camera.animation_data.nla_tracks.new()
    import_track.name = "anim_import"
    import_strip = import_track.strips.new(import_action.name, 1, import_action)
    import_strip.blend_type = 'COMBINE'
    import_track.is_solo = True
    import_state = (bool(import_track.mute), bool(import_track.is_solo))
    transaction_scene.frame_set(1)
    ctx.view_layer.update()

    path = 'pose.bones["Camera_Node"].location'

    def assert_isolated_bake(result):
        assert len(result) == 1 and result[0][0] is camera, result
        curves = {
            (curve.data_path, curve.array_index): curve
            for curve in cutscene_bake._action_fcurves(result[0][1])
        }
        for frame, expected in enumerate(((2.0, 7.0), (4.0, 9.0)), 1):
            actual = (curves[(path, 0)].evaluate(frame), curves[(path, 1)].evaluate(frame))
            assert all(abs(value - wanted) < 1e-5 for value, wanted in zip(actual, expected)), (frame, actual)
        assert import_track.mute and not import_track.is_solo

    assert_isolated_bake(cutscene_bake.bake_cutscene_actors(ctx, frame_start=1, frame_end=2))
    assert_isolated_bake(cutscene_bake.bake_cutscene_actors(ctx, frame_start=1, frame_end=2))
    for holder in cutscene_bake._object_chain(camera):
        cutscene_bake._unstash_holder(holder)
    assert (bool(import_track.mute), bool(import_track.is_solo)) == import_state
    for track in list(camera.animation_data.nla_tracks):
        camera.animation_data.nla_tracks.remove(track)
    for action in (cutscene_action, import_action):
        if action.users == 0:
            bpy.data.actions.remove(action)
    camera.animation_data.action = camera_action

    prop = make_armature("W3TB_stale_prop", "root")
    prop[cutscene_bake.PROP_RIG_TAG] = True
    prop["cutscene_actor_name"] = "props"
    prop["cutscene_actor_type"] = "CAT_Prop"
    extra_collection = bpy.data.collections.new("W3TB_stale_prop_extra")
    transaction_scene.collection.children.link(extra_collection)
    extra_collection.objects.link(prop)
    prop_action = bpy.data.actions.new("W3TB_stale_prop_bake")
    prop_action[cutscene_bake.BAKED_ACTION_TAG] = True
    prop.animation_data_create()
    prop_track = prop.animation_data.nla_tracks.new()
    prop_track.name = cutscene_bake.CUTSCENE_TRACK_NAME
    prop_track.strips.new(prop_action.name, 1, prop_action)
    for candidate in ctx.view_layer.objects:
        candidate.select_set(False)
    prop.select_set(True)
    ctx.view_layer.objects.active = prop

    def prop_snapshot():
        return (
            tuple(sorted(collection.as_pointer() for collection in prop.users_collection)),
            tuple(
                (
                    owner_scene.name,
                    view_layer.name,
                    view_layer.objects.active.as_pointer() if view_layer.objects.active else 0,
                    prop.select_get(view_layer=view_layer),
                )
                for owner_scene in bpy.data.scenes
                for view_layer in owner_scene.view_layers
                if prop.name in view_layer.objects
            ),
            tuple(
                (
                    track.as_pointer(), track.name, bool(track.mute),
                    tuple((strip.as_pointer(), strip.action.as_pointer(), bool(strip.mute)) for strip in track.strips),
                )
                for track in prop.animation_data.nla_tracks
            ),
        )

    prop_before = prop_snapshot()
    transaction = cutscene_bake.begin_bake_transaction(transaction_scene)
    assert cutscene_bake.bake_prop_actor(ctx, 1, 2) is None
    assert not prop.users_collection
    assert cutscene_bake.find_prop_actor(transaction_scene) is None
    assert prop_track.mute
    transaction.rollback()
    assert prop_snapshot() == prop_before
    assert cutscene_bake.find_prop_actor(transaction_scene) is prop

    prop_name = prop.name
    prop_data_name = prop.data.name
    prop_action_name = prop_action.name
    transaction = cutscene_bake.begin_bake_transaction(transaction_scene)
    assert cutscene_bake.bake_prop_actor(ctx, 1, 2) is None
    transaction.commit()
    assert bpy.data.objects.get(prop_name) is None
    assert bpy.data.armatures.get(prop_data_name) is None
    assert bpy.data.actions.get(prop_action_name) is None

    ctx.window.scene = fixture_scene
    actors = list(cutscene_bake.iter_cutscene_actor_armatures(ctx.scene))
    active_before = {
        arm.name: [(strip.name, strip.action.name, float(strip.frame_start), float(strip.frame_end))
                   for strip in cutscene_bake._active_cutscene_strips(arm)]
        for arm in actors
    }
    source_ids_before = {
        arm.name: cutscene_bake._active_cutscene_source_ids(arm)
        for arm in actors
    }
    range_before = cutscene_bake.effective_frame_range(ctx.scene)
    export_before = [
        (entry["actor_name"], entry["component"], entry["action_name"],
         entry["strip_frame_start"], entry["strip_frame_end"], entry["source_index"],
         tuple(entry["source_clip_ids"]))
        for entry in export_cutscene._collect_cutscene_nla_entries(ctx)
    ]
    issues_before = cutscene_bake.validate_cutscene_for_export(ctx)
    print("ISSUES BEFORE BAKE:")
    for issue in issues_before:
        print("  -", issue)
    assert issues_before, "expected validator to flag the un-baked scene"

    probe_sources = [
        (arm, strip) for arm in actors
        for strip in cutscene_bake._active_cutscene_strips(arm)
        if strip.action is not None
    ]
    assert probe_sources, "expected an authored cutscene strip for the mute probe"
    probe_actor, source_strip = probe_sources[0]
    probe_source_id = 1_999_999
    probe_action = source_strip.action.copy()
    probe_action.name = "W3TB_muted_far_future"
    probe_action[cutscene_bake.CUTSCENE_SOURCE_INDEX_PROP] = probe_source_id
    probe_track = probe_actor.animation_data.nla_tracks.new()
    probe_track.name = "cutscene_anim_muted_probe"
    probe_strip = probe_track.strips.new(probe_action.name, range_before[1] + 1000, probe_action)
    probe_strip.mute = True
    assert probe_strip.frame_end > range_before[1]

    assert {
        arm.name: [(strip.name, strip.action.name, float(strip.frame_start), float(strip.frame_end))
                   for strip in cutscene_bake._active_cutscene_strips(arm)]
        for arm in actors
    } == active_before
    assert {
        arm.name: cutscene_bake._active_cutscene_source_ids(arm)
        for arm in actors
    } == source_ids_before
    assert cutscene_bake.effective_frame_range(ctx.scene) == range_before
    assert cutscene_bake.validate_cutscene_for_export(ctx) == issues_before
    assert [
        (entry["actor_name"], entry["component"], entry["action_name"],
         entry["strip_frame_start"], entry["strip_frame_end"], entry["source_index"],
         tuple(entry["source_clip_ids"]))
        for entry in export_cutscene._collect_cutscene_nla_entries(ctx)
    ] == export_before

    fallback_actor = export_cutscene._collect_cutscene_actor_roots(ctx.scene)[0]
    fallback_anim_data = fallback_actor.animation_data or fallback_actor.animation_data_create()
    fallback_action = fallback_anim_data.action
    fallback_slot = getattr(fallback_anim_data, "action_slot", None)
    mute_states = []
    try:
        for actor_root in export_cutscene._collect_cutscene_actor_roots(ctx.scene):
            for armature in export_cutscene._iter_cutscene_related_armatures(actor_root, ctx.scene):
                anim_data = getattr(armature, "animation_data", None)
                for track in getattr(anim_data, "nla_tracks", []) or []:
                    if not export_cutscene._is_cutscene_track_name(track.name):
                        continue
                    for strip in track.strips:
                        mute_states.append((strip, bool(strip.mute)))
                        strip.mute = True
        fallback_anim_data.action = source_strip.action
        assert export_cutscene._has_cutscene_nla_strips(ctx)
        assert not export_cutscene._collect_cutscene_nla_entries(ctx)
        assert export_cutscene._collect_cutscene_active_entries(ctx)
        fallback_state = export_cutscene._build_cutscene_export_state(ctx)
        assert fallback_state["source_mode"] == "nla", fallback_state["source_mode"]
        assert not fallback_state["entries"], fallback_state["entries"]
    finally:
        fallback_anim_data.action = fallback_action
        if fallback_action is not None and fallback_slot is not None and hasattr(fallback_anim_data, "action_slot"):
            fallback_anim_data.action_slot = fallback_slot
        for strip, muted in mute_states:
            strip.mute = muted

    actor_world = probe_actor.matrix_world.copy()
    old_parent = probe_actor.parent
    old_parent_type = probe_actor.parent_type
    old_parent_bone = probe_actor.parent_bone
    old_parent_inverse = probe_actor.matrix_parent_inverse.copy()
    muted_holder = bpy.data.objects.new("W3TB_muted_nla_holder", None)
    ctx.scene.collection.objects.link(muted_holder)
    probe_actor.parent = muted_holder
    probe_actor.parent_type = 'OBJECT'
    probe_actor.parent_bone = ""
    probe_actor.matrix_world = actor_world
    muted_holder.animation_data_create()
    holder_action = bpy.data.actions.new("W3TB_muted_holder_action")
    muted_holder.animation_data.action = holder_action
    muted_holder.location = (0.0, 0.0, 0.0)
    muted_holder.keyframe_insert("location", frame=1)
    muted_holder.location = (1.0, 0.0, 0.0)
    muted_holder.keyframe_insert("location", frame=11)
    muted_holder.animation_data.action = None
    muted_holder.location = (0.0, 0.0, 0.0)
    holder_track = muted_holder.animation_data.nla_tracks.new()
    holder_track.name = "W3TB_muted_holder_track"
    holder_strip = holder_track.strips.new(holder_action.name, 1, holder_action)
    holder_strip.mute = True
    ctx.view_layer.update()
    holder_issues = cutscene_bake.validate_cutscene_for_export(ctx)
    assert not any(
        muted_holder.name in issue and "object-level animation" in issue
        for issue in holder_issues
    ), holder_issues
    probe_actor.parent = old_parent
    probe_actor.parent_type = old_parent_type
    probe_actor.parent_bone = old_parent_bone
    probe_actor.matrix_parent_inverse = old_parent_inverse
    probe_actor.matrix_world = actor_world
    bpy.data.objects.remove(muted_holder, do_unlink=True)
    if holder_action.users == 0:
        bpy.data.actions.remove(holder_action)

    baked = cutscene_bake.bake_cutscene_actors(ctx)
    print(f"BAKED {len(baked)} actors: {[a.get('cutscene_actor_name') for a, _ in baked]}")
    assert len(baked) == 3, [a.name for a, _ in baked]
    for arm, action in baked:
        baked_ids = list(action.get(cutscene_bake.BAKED_SOURCE_CLIP_IDS_PROP, []) or [])
        assert baked_ids == source_ids_before.get(arm.name, []), (arm.name, baked_ids)
        assert probe_source_id not in baked_ids
        assert tuple(int(round(frame)) for frame in action.frame_range) == range_before

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
        baked_ids = list(_action.get(cutscene_bake.BAKED_SOURCE_CLIP_IDS_PROP, []) or [])
        assert baked_ids == source_ids_before.get(arm.name, []), (arm.name, baked_ids)
        assert probe_source_id not in baked_ids
        assert tuple(int(round(frame)) for frame in _action.frame_range) == range_before
    assert not cutscene_bake.validate_cutscene_for_export(ctx)
    print("RE-BAKE OK: tracks/actions stable")

    prop_probe = bpy.data.objects.new("W3TB_generated_prop_probe", None)
    ctx.scene.collection.objects.link(prop_probe)
    prop_probe[cutscene_bake.TRAJECTORY_SLOT_PROP] = "Trajectory01"
    prop_armature, _prop_action = cutscene_bake.bake_prop_actor(
        ctx, range_before[0], range_before[1], "w3tb_generated_prop_probe",
    )
    prop_template = "generated\\w3tb_missing_props.w2ent"
    prop_armature["cutscene_actor_template"] = prop_template
    missing_prop_message = "Props entity (.w2ent) not written yet"
    assert any(missing_prop_message in issue for issue in cutscene_bake.validate_cutscene_for_export(ctx))
    assert not any(
        missing_prop_message in issue
        for issue in cutscene_bake.validate_cutscene_for_export(
            ctx,
            allowed_missing_prop_templates={prop_template},
        )
    )
    cutscene_bake.remove_prop_actor(ctx.scene)
    bpy.data.objects.remove(prop_probe, do_unlink=True)
    assert not cutscene_bake.validate_cutscene_for_export(ctx)

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
    bake_start, bake_end = cutscene_bake.effective_frame_range(bpy.context.scene)
    assert (bake_start, bake_end) == range_before
    expected = round((bake_end - bake_start) / 30.0, 1)
    assert abs(list(durations)[0] - expected) < 0.2, (durations, expected)
    print("W3TB_CUTSCENE_BAKE_NATIVE_OK")
except Exception:
    traceback.print_exc()
    print("W3TB_CUTSCENE_BAKE_NATIVE_FAIL")
    sys.exit(1)
finally:
    addon.unregister()
