import math
import sys
import traceback
import shutil
from pathlib import Path
from types import SimpleNamespace

import bpy

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

OUT = REPO_ROOT / "WORKING_TEMP" / "cutscene_clip_identity" / "clip_identity.w2cutscene"
OUT_OTHER = REPO_ROOT / "WORKING_TEMP" / "cutscene_clip_identity" / "clip_identity_other.w2cutscene"
OUT_CHANGED = REPO_ROOT / "WORKING_TEMP" / "cutscene_clip_identity" / "clip_identity_changed.w2cutscene"
OUT_BAKED = REPO_ROOT / "WORKING_TEMP" / "cutscene_clip_identity" / "clip_identity_baked.w2cutscene"
MISSING = REPO_ROOT / "WORKING_TEMP" / "cutscene_clip_identity" / "intentionally_missing.w2cutscene"

import witcher3_tools as addon


def select_only(obj):
    for candidate in bpy.context.view_layer.objects:
        candidate.select_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


def make_actor(scene, name, actor_name):
    armature = bpy.data.armatures.new(f"{name}_rig")
    obj = bpy.data.objects.new(name, armature)
    scene.collection.objects.link(obj)
    select_only(obj)
    bpy.ops.object.mode_set(mode='EDIT')
    bone = armature.edit_bones.new("root")
    bone.head = (0.0, 0.0, 0.0)
    bone.tail = (0.0, 0.0, 1.0)
    bpy.ops.object.mode_set(mode='OBJECT')
    obj["cutscene_actor_name"] = actor_name
    obj["cutscene_actor_template"] = "characters\\npc_entities\\test\\identity_actor.w2ent"
    obj["cutscene_actor_type"] = "CAT_Actor"
    return obj


def make_action(actor, name, distance=1.0):
    actor.animation_data_create()
    action = bpy.data.actions.new(name)
    actor.animation_data.action = action
    bone = actor.pose.bones["root"]
    bone.rotation_mode = 'QUATERNION'
    for frame, x in ((1, 0.0), (11, distance)):
        bone.location = (x, 0.0, 0.0)
        bone.keyframe_insert("location", frame=frame)
        bone.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
        bone.keyframe_insert("rotation_quaternion", frame=frame)
    actor.animation_data.action = None
    return action


def add_entry_event(scene, source_index, name):
    event = scene.witcher_cutscene_event_items.add()
    event.event_type = "CExtAnimSoundEvent"
    event.event_name = name
    event.event_scope = "ENTRY"
    event.source_index = int(source_index)
    event.start_time = 0.1


def event_ids(scene):
    return {
        str(item.event_name): int(item.source_index)
        for item in scene.witcher_cutscene_event_items
        if str(item.event_scope).upper() == "ENTRY"
    }


addon.register()
try:
    from witcher3_tools.exporters import export_cutscene
    from witcher3_tools.importers import import_cutscene
    from witcher3_tools.ui import ui_anims, ui_cutscene

    def clip_snapshot(source_index):
        group = ui_cutscene._clip_groups(bpy.context.scene).get(int(source_index), {})
        return (
            tuple(sorted(
                (
                    track.as_pointer(), track.name, bool(track.mute),
                    strip.as_pointer(), strip.name, bool(strip.mute),
                    strip.action.as_pointer() if strip.action else 0,
                )
                for track, strip in group.get("strips", ())
            )),
            tuple(
                (
                    int(row.source_index), bool(row.file_backed), row.full_name,
                    bool(row.is_loaded), bool(row.muted), bool(row.track_muted), bool(row.has_prebake),
                )
                for row in bpy.context.scene.witcher_cutscene_animation_items
                if int(row.source_index) == int(source_index)
            ),
            tuple(
                (row.event_name, row.event_scope, int(row.source_index))
                for row in bpy.context.scene.witcher_cutscene_event_items
                if int(row.source_index) == int(source_index)
            ),
        )

    def row_muted(source_index):
        values = [
            bool(row.muted) for row in bpy.context.scene.witcher_cutscene_animation_items
            if int(row.source_index) == int(source_index)
        ]
        assert len(values) == 1, (source_index, values)
        return values[0]

    def exact_scene_snapshot():
        scene = bpy.context.scene
        return (
            str(scene.witcher_loaded_w2cutscene_path),
            int(scene.witcher_cutscene_loaded_actor_index),
            int(scene.witcher_cutscene_loaded_anim_index),
            int(scene.witcher_cutscene_event_index),
            tuple(sorted(
                (
                    repr(key),
                    tuple(sorted(
                        (
                            track.as_pointer(), track.name, bool(track.mute),
                            strip.as_pointer(), strip.name, bool(strip.mute),
                            strip.action.as_pointer() if strip.action else 0,
                        )
                        for track, strip in group.get("strips", ())
                    )),
                )
                for key, group in ui_cutscene._clip_groups(scene).items()
            )),
            tuple(
                tuple(getattr(row, field) for field in ui_cutscene._ANIMATION_ROW_FIELDS)
                for row in scene.witcher_cutscene_animation_items
            ),
            tuple(
                tuple(getattr(row, field) for field in ui_cutscene._EVENT_ROW_FIELDS)
                for row in scene.witcher_cutscene_event_items
            ),
            tuple(
                (
                    int(row.source_index), row.actor_name, row.object_name,
                    bool(row.is_loaded), bool(row.imported_by_cutscene),
                )
                for row in scene.witcher_cutscene_actor_items
            ),
        )

    def assert_cancelled(call):
        result = call()
        assert result == {'CANCELLED'}, result

    scene = bpy.context.scene
    scene.render.fps = 30
    scene.frame_start = 0
    scene.frame_end = 60
    actor = make_actor(scene, "identity_actor", "identity")
    import_cutscene.ensure_actor_custom_props(actor)
    source_action = make_action(actor, "same_source")

    def add_authored(start_frame):
        track, strip = ui_anims._create_cutscene_action_strip(
            bpy.context,
            actor,
            source_action,
            "identity",
            "Root",
            start_frame=start_frame,
            group_name="same_clip",
        )
        assert track is not None and strip is not None
        ui_anims._ensure_cutscene_animation_list_entry(
            scene, "identity", "Root", "same_clip", strip.action,
        )
        ui_cutscene.sync_animation_items_from_scene(scene)
        return int(strip.action[export_cutscene.CUTSCENE_SOURCE_INDEX_PROP])

    first_id = add_authored(0)
    second_id = add_authored(20)
    assert first_id >= ui_cutscene.AUTHORED_CLIP_ID_BASE
    assert second_id > first_id
    assert str(ui_cutscene._clip_groups(scene)[first_id]["full_name"]) == "identity:Root:same_clip"
    assert str(ui_cutscene._clip_groups(scene)[second_id]["full_name"]) == "identity:Root:same_clip"
    assert all(
        not bool(item.file_backed)
        for item in scene.witcher_cutscene_animation_items
        if int(item.source_index) in {first_id, second_id}
    )
    assert all(
        not str(strip.action.get(export_cutscene.CUTSCENE_SOURCE_PATH_PROP, "") or "")
        for source_index in (first_id, second_id)
        for _track, strip in ui_cutscene._clip_groups(scene)[source_index]["strips"]
    )

    groups = ui_cutscene._clip_groups(scene)
    first_track, first_strip = groups[first_id]["strips"][0]
    second_track, second_strip = groups[second_id]["strips"][0]
    assert first_track.as_pointer() == second_track.as_pointer()
    assert bpy.ops.witcher.cutscene_set_clip_muted(source_index=first_id, mute=True) == {'FINISHED'}
    groups = ui_cutscene._clip_groups(scene)
    assert all(strip.mute for _track, strip in groups[first_id]["strips"])
    assert all(not strip.mute for _track, strip in groups[second_id]["strips"])
    first_track = groups[first_id]["strips"][0][0]
    first_track.mute = True
    groups[second_id]["strips"][0][1].mute = True
    ui_cutscene.sync_animation_items_from_scene(scene)
    track_pointer = first_track.as_pointer()
    sibling_pointer = ui_cutscene._clip_groups(scene)[second_id]["strips"][0][1].as_pointer()
    assert bpy.ops.witcher.cutscene_set_clip_muted(source_index=first_id, mute=False) == {'FINISHED'}
    groups = ui_cutscene._clip_groups(scene)
    assert groups[first_id]["strips"][0][0].as_pointer() == track_pointer
    assert groups[first_id]["strips"][0][0].mute, "unmute must not clear the containing track"
    assert not groups[first_id]["strips"][0][1].mute
    assert groups[second_id]["strips"][0][1].as_pointer() == sibling_pointer
    assert groups[second_id]["strips"][0][1].mute, "unmute must not touch a sibling strip"
    assert not row_muted(first_id) and row_muted(second_id), "rows reflect strip mute, not track mute"
    groups[first_id]["strips"][0][0].mute = False
    groups[second_id]["strips"][0][1].mute = False
    ui_cutscene.sync_animation_items_from_scene(scene)

    add_entry_event(scene, first_id, "identity_first")
    add_entry_event(scene, second_id, "identity_second")

    first_action = ui_cutscene._clip_groups(scene)[first_id]["strips"][0][1].action
    mixed_prebake_track = actor.animation_data.nla_tracks.new()
    mixed_prebake_track.name = export_cutscene.CUTSCENE_TRACK_NAME + "_mixed_prebake"
    mixed_prebake_track.strips.new("mixed_prebake", 1, first_action)
    ui_cutscene.sync_animation_items_from_scene(scene)
    prebake_before = clip_snapshot(first_id)
    assert_cancelled(lambda: bpy.ops.witcher.cutscene_set_clip_muted(source_index=first_id, mute=True))
    assert clip_snapshot(first_id) == prebake_before
    assert_cancelled(lambda: bpy.ops.witcher.cutscene_remove_animation(source_index=first_id))
    assert clip_snapshot(first_id) == prebake_before
    actor.animation_data.nla_tracks.remove(mixed_prebake_track)
    ui_cutscene.sync_animation_items_from_scene(scene)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    result = export_cutscene.export_w3_cutscene(bpy.context, str(OUT))
    assert result == {'FINISHED'} and OUT.is_file(), result
    parsed = import_cutscene.loadCutsceneFile(str(OUT))
    assert parsed is not None
    _cutscene, _actors, parsed_anims, parsed_events = import_cutscene.collect_cutscene_preview(
        str(OUT), cutscene_template=parsed,
    )
    same_name_rows = [item for item in parsed_anims if item["full_name"] == "identity:Root:same_clip"]
    assert len(same_name_rows) == 2, parsed_anims
    parsed_identity_events = [
        item for item in parsed_events
        if item["event_name"] in {"identity_first", "identity_second"}
    ]
    assert len(parsed_identity_events) == 2, parsed_events
    parsed_event_targets = {item["event_name"]: int(item["source_index"]) for item in parsed_identity_events}
    assert parsed_event_targets["identity_first"] != parsed_event_targets["identity_second"], parsed_event_targets

    from witcher3_tools.ui import ui_anims_list
    captured_nodes = []
    real_load_anim_into_scene = ui_anims_list.load_anim_into_scene

    def capture_selected_node(_context, _anim_name, _fdir, main_arm_obj, NLA_track="cutscene_anim", **kwargs):
        captured_nodes.append(kwargs.get("cutscene_entry"))
        assert kwargs.get("cutscene_source_index") == 1
        assert kwargs.get("source_game") == "w3"
        assert main_arm_obj.get("witcher_source_game") == "w2"
        anim_data = main_arm_obj.animation_data_create()
        track = anim_data.nla_tracks.get(NLA_track) or anim_data.nla_tracks.new()
        track.name = NLA_track
        action = bpy.data.actions.new("captured_file_index")
        track.strips.new("captured_file_index", 1, action)
        return [main_arm_obj]

    actor["witcher_source_game"] = "w2"
    ui_anims_list.load_anim_into_scene = capture_selected_node
    try:
        applied = import_cutscene._apply_cutscene_animation_sequence_template(
            parsed,
            str(OUT),
            {1},
            actor,
            actor_name="identity",
            track_name=import_cutscene.CUTSCENE_FILE_TRACK_NAME,
        )
    finally:
        ui_anims_list.load_anim_into_scene = real_load_anim_into_scene
        del actor["witcher_source_game"]
    assert applied == {1}
    assert captured_nodes == [parsed.animations[1]], captured_nodes
    import_cutscene.clear_cutscene_actor_animation_tracks(actor, source_path=str(OUT))

    from witcher3_tools.CR2W import dc_cutscene_w2
    w2_entries = [
        SimpleNamespace(animation=SimpleNamespace(
            name="duplicate", animBuffer=SimpleNamespace(parts=[object(), object()]),
        )),
        SimpleNamespace(animation=SimpleNamespace(
            name="duplicate", animBuffer=SimpleNamespace(parts=[object()]),
        )),
    ]
    decoded_blob_indices = []
    originals = {
        "open_cr2w_read_stream": dc_cutscene_w2.open_cr2w_read_stream,
        "getCR2W": dc_cutscene_w2.getCR2W,
        "create_CCutscene_w2": dc_cutscene_w2.create_CCutscene_w2,
        "get_fallback_w2_skeleton_names": dc_cutscene_w2.get_fallback_w2_skeleton_names,
        "HavokPackfile": dc_cutscene_w2.HavokPackfile,
        "_apply_decoded_w2_cutscene_parts": dc_cutscene_w2._apply_decoded_w2_cutscene_parts,
    }
    dc_cutscene_w2.open_cr2w_read_stream = lambda _path: SimpleNamespace(cr2w_buf=b"")
    dc_cutscene_w2.getCR2W = lambda _stream: object()
    dc_cutscene_w2.create_CCutscene_w2 = lambda _file, _data: SimpleNamespace(animations=w2_entries)
    dc_cutscene_w2.get_fallback_w2_skeleton_names = lambda *_args, **_kwargs: ([], [])
    dc_cutscene_w2.HavokPackfile = SimpleNamespace(
        decode_animation_blob_at_index=lambda _data, blob_index, **_kwargs: (
            decoded_blob_indices.append(blob_index) or SimpleNamespace(buffer=object())
        ),
    )
    dc_cutscene_w2._apply_decoded_w2_cutscene_parts = lambda *_args, **_kwargs: None
    try:
        w2_selected = dc_cutscene_w2.load_w2_cutscene_anim(
            "duplicate.w2cutscene", anim_name="duplicate", anim_index=1,
        )
    finally:
        for attr, value in originals.items():
            setattr(dc_cutscene_w2, attr, value)
    assert w2_selected.animations == [w2_entries[1]]
    assert decoded_blob_indices == [2], decoded_blob_indices

    assert bpy.ops.witcher.cutscene_remove_animation(source_index=first_id) == {'FINISHED'}
    assert first_id not in ui_cutscene._clip_groups(scene)
    assert "identity_first" not in event_ids(scene)
    assert event_ids(scene)["identity_second"] == second_id
    third_id = add_authored(40)
    assert third_id > second_id, (first_id, second_id, third_id)

    second_group = ui_cutscene._clip_groups(scene)[second_id]
    for track, strip in list(second_group["strips"]):
        track.strips.remove(strip)
    ui_cutscene.sync_animation_items_from_scene(scene)
    assert second_id not in {
        int(item.source_index) for item in scene.witcher_cutscene_animation_items
    }
    assert "identity_second" not in event_ids(scene)

    add_entry_event(scene, third_id, "authored_keep")
    assert bpy.ops.witcher.cutscene_set_clip_muted(source_index=third_id, mute=True) == {'FINISHED'}
    authored_pointer = ui_cutscene._clip_groups(scene)[third_id]["strips"][0][1].as_pointer()
    authored_before_rebuild = clip_snapshot(third_id)

    ui_cutscene.sync_animation_items_from_scene(scene)
    imported = import_cutscene.import_w3_cutscene(
        str(OUT),
        selected_actor_indices=set(),
        selected_animation_indices=set(),
        auto_apply_selected_animations=False,
        import_burned_audio=False,
    )
    assert imported is not None
    ui_cutscene._sync_loaded_cutscene_state(scene, str(OUT), cutscene_data=imported)
    file_rows = [item for item in scene.witcher_cutscene_animation_items if item.file_backed]
    assert {int(item.source_index) for item in file_rows} == {0, 1}, [
        (item.source_index, item.file_backed, item.full_name) for item in scene.witcher_cutscene_animation_items
    ]
    authored_row = next(item for item in scene.witcher_cutscene_animation_items if int(item.source_index) == third_id)
    assert not authored_row.file_backed and authored_row.muted
    assert event_ids(scene)["authored_keep"] == third_id
    assert ui_cutscene._clip_groups(scene)[third_id]["strips"][0][1].as_pointer() == authored_pointer
    assert clip_snapshot(third_id) == authored_before_rebuild

    actor_row = next(item for item in scene.witcher_cutscene_actor_items if item.actor_name == "identity")
    actor_row.object_name = actor.name
    actor_row.is_loaded = True
    file_zero_name = next(item.full_name for item in file_rows if int(item.source_index) == 0)
    assert bpy.ops.witcher.set_cutscene_animation_loaded(source_index=0, load=True) == {'FINISHED'}
    ui_cutscene.sync_animation_items_from_scene(scene)
    assert import_cutscene.is_cutscene_animation_loaded(actor, file_zero_name, str(OUT), 0)
    assert not import_cutscene.is_cutscene_animation_loaded(actor, file_zero_name, str(OUT), 99)
    assert not import_cutscene.is_cutscene_animation_loaded(actor, file_zero_name, str(OUT) + ".other", 0)
    assert any(
        track.name.startswith(import_cutscene.CUTSCENE_FILE_TRACK_NAME + "_")
        for track in actor.animation_data.nla_tracks
    )
    assert ui_cutscene._clip_groups(scene)[third_id]["strips"][0][1].as_pointer() == authored_pointer
    assert ui_cutscene._clip_groups(scene)[third_id]["strips"][0][1].mute

    assert bpy.ops.witcher.cutscene_set_clip_muted(source_index=0, mute=True) == {'FINISHED'}
    assert row_muted(0)
    assert all(strip.mute for _track, strip in ui_cutscene._clip_groups(scene)[0]["strips"])
    actor_row = next(item for item in scene.witcher_cutscene_actor_items if item.actor_name == "identity")
    applied, errors = ui_cutscene._rebuild_cutscene_actor_animations(scene, actor_row)
    assert 0 in applied and not errors, (applied, errors)
    ui_cutscene.sync_animation_items_from_scene(scene)
    assert row_muted(0)
    assert all(strip.mute for _track, strip in ui_cutscene._clip_groups(scene)[0]["strips"])
    assert clip_snapshot(third_id) == authored_before_rebuild

    assert not ui_cutscene._clip_groups(scene)[0]["has_prebake"]
    other_ids = {
        key for key in ui_cutscene._clip_groups(scene)
        if isinstance(key, int) and key != 0
    }
    other_before = {key: clip_snapshot(key) for key in other_ids}
    file_one_before = clip_snapshot(1)
    assert bpy.ops.witcher.set_cutscene_animation_loaded(source_index=0, load=False) == {'FINISHED'}
    assert 0 not in ui_cutscene._clip_groups(scene)
    assert {
        key for key in ui_cutscene._clip_groups(scene)
        if isinstance(key, int) and key != 0
    } == other_ids
    assert {key: clip_snapshot(key) for key in other_ids} == other_before
    assert clip_snapshot(1) == file_one_before
    unloaded_zero = [
        item for item in scene.witcher_cutscene_animation_items
        if item.file_backed and int(item.source_index) == 0
    ]
    assert len(unloaded_zero) == 1
    assert unloaded_zero[0].full_name == file_zero_name
    assert not unloaded_zero[0].is_loaded and unloaded_zero[0].muted
    assert bpy.ops.witcher.set_cutscene_animation_loaded(source_index=0, load=True) == {'FINISHED'}
    reloaded_zero = next(
        item for item in scene.witcher_cutscene_animation_items
        if item.file_backed and int(item.source_index) == 0
    )
    assert reloaded_zero.is_loaded and reloaded_zero.muted
    assert not ui_cutscene._clip_groups(scene)[0]["has_prebake"]
    assert all(strip.mute for _track, strip in ui_cutscene._clip_groups(scene)[0]["strips"])
    assert {key: clip_snapshot(key) for key in other_ids} == other_before
    assert clip_snapshot(1) == file_one_before
    assert clip_snapshot(third_id) == authored_before_rebuild

    live_file_action = ui_cutscene._clip_groups(scene)[0]["strips"][0][1].action
    mute_probe_action = live_file_action.copy()
    mute_probe_action_name = mute_probe_action.name
    mute_probe_track_name = export_cutscene.CUTSCENE_TRACK_NAME + "_mute_probe_prebake"
    mute_probe_track = actor.animation_data.nla_tracks.new()
    mute_probe_track.name = mute_probe_track_name
    mute_probe_track.strips.new("mute_snapshot_probe", 1, mute_probe_action)
    ui_cutscene.sync_animation_items_from_scene(scene)
    assert ui_cutscene._clip_groups(scene)[0]["has_prebake"]
    assert ui_cutscene._file_clip_mute_state(scene, str(OUT), actor_name="identity")[0]
    mixed_row = next(
        item for item in scene.witcher_cutscene_animation_items
        if item.file_backed and int(item.source_index) == 0
    )
    assert mixed_row.has_prebake
    mixed_before = exact_scene_snapshot()
    assert_cancelled(lambda: bpy.ops.witcher.set_cutscene_animation_loaded(source_index=0, load=False))
    assert exact_scene_snapshot() == mixed_before
    actor_row = next(item for item in scene.witcher_cutscene_actor_items if item.actor_name == "identity")
    actor_key = (
        int(actor_row.source_index), str(actor_row.object_name), str(actor_row.actor_name),
    )
    assert 0 in {
        int(item.source_index)
        for item in ui_cutscene._actor_animation_entries_for_layer(scene, actor_row, "ALL")
    }
    assert_cancelled(lambda: bpy.ops.witcher.set_cutscene_actor_animation_layer(
        source_index=actor_key[0], object_name=actor_key[1], actor_name=actor_key[2],
        layer='ALL', load=False,
    ))
    assert exact_scene_snapshot() == mixed_before

    ui_cutscene.sync_animation_items_from_scene(scene)
    imported_again = import_cutscene.import_w3_cutscene(
        str(OUT),
        selected_actor_indices=set(),
        selected_animation_indices=set(),
        auto_apply_selected_animations=False,
        import_burned_audio=False,
    )
    ui_cutscene._sync_loaded_cutscene_state(scene, str(OUT), cutscene_data=imported_again)
    assert clip_snapshot(third_id) == authored_before_rebuild
    assert event_ids(scene)["authored_keep"] == third_id
    same_file_zero = next(
        item for item in scene.witcher_cutscene_animation_items
        if item.file_backed and int(item.source_index) == 0
    )
    assert same_file_zero.is_loaded and same_file_zero.muted and same_file_zero.has_prebake
    group_zero = ui_cutscene._clip_groups(scene)[0]
    live_file_strips = [
        strip for track, strip in group_zero["strips"] if "_prebake" not in track.name
    ]
    assert live_file_strips and all(strip.mute for strip in live_file_strips)
    assert row_muted(0)
    probe_track = actor.animation_data.nla_tracks.get(mute_probe_track_name)
    assert probe_track is not None and len(probe_track.strips) == 1
    assert not probe_track.strips[0].mute
    assert ui_cutscene._file_clip_mute_state(scene, str(OUT), actor_name="identity")[0]

    actor_row = next(item for item in scene.witcher_cutscene_actor_items if item.actor_name == "identity")
    applied, errors = ui_cutscene._rebuild_cutscene_actor_animations(scene, actor_row)
    assert 0 in applied and not errors, (applied, errors)
    ui_cutscene.sync_animation_items_from_scene(scene)
    group_zero = ui_cutscene._clip_groups(scene)[0]
    live_file_strips = [
        strip for track, strip in group_zero["strips"] if "_prebake" not in track.name
    ]
    assert live_file_strips and all(strip.mute for strip in live_file_strips)
    probe_track = actor.animation_data.nla_tracks.get(mute_probe_track_name)
    assert probe_track is not None and len(probe_track.strips) == 1
    assert not probe_track.strips[0].mute
    assert ui_cutscene._file_clip_mute_state(scene, str(OUT), actor_name="identity")[0]
    assert clip_snapshot(third_id) == authored_before_rebuild
    actor.animation_data.nla_tracks.remove(probe_track)
    probe_action = bpy.data.actions.get(mute_probe_action_name)
    if probe_action is not None:
        bpy.data.actions.remove(probe_action)
    ui_cutscene.sync_animation_items_from_scene(scene)
    assert row_muted(0)
    assert all(strip.mute for _track, strip in ui_cutscene._clip_groups(scene)[0]["strips"])

    file_action = ui_cutscene._clip_groups(scene)[0]["strips"][0][1].action
    backup_action = file_action.copy()
    backup_track = actor.animation_data.nla_tracks.new()
    backup_track.name = export_cutscene.CUTSCENE_TRACK_NAME + "_prebake"
    backup_track.strips.new("old_file_backup", 1, backup_action)
    shutil.copyfile(OUT, OUT_OTHER)
    ui_cutscene.sync_animation_items_from_scene(scene)
    imported_other = import_cutscene.import_w3_cutscene(
        str(OUT_OTHER),
        selected_actor_indices=set(),
        selected_animation_indices=set(),
        auto_apply_selected_animations=False,
        import_burned_audio=False,
    )
    ui_cutscene._sync_loaded_cutscene_state(scene, str(OUT_OTHER), cutscene_data=imported_other)
    assert 0 not in ui_cutscene._clip_groups(scene), ui_cutscene._clip_groups(scene)
    assert backup_track.name in actor.animation_data.nla_tracks
    assert len(backup_track.strips) == 1
    assert not next(
        item for item in scene.witcher_cutscene_animation_items
        if item.file_backed and int(item.source_index) == 0
    ).is_loaded
    assert not row_muted(0), "a different file must not inherit the prior file's low-index mute"
    assert clip_snapshot(third_id) == authored_before_rebuild
    actor_row = next(item for item in scene.witcher_cutscene_actor_items if item.actor_name == "identity")
    actor_row.object_name = actor.name
    actor_row.is_loaded = True
    assert bpy.ops.witcher.set_cutscene_animation_loaded(source_index=0, load=True) == {'FINISHED'}
    ui_cutscene.sync_animation_items_from_scene(scene)
    assert not row_muted(0)
    assert all(not strip.mute for _track, strip in ui_cutscene._clip_groups(scene)[0]["strips"])
    assert clip_snapshot(third_id) == authored_before_rebuild

    actor_row = next(item for item in scene.witcher_cutscene_actor_items if item.actor_name == "identity")
    actor_row.object_name = actor.name
    actor_row.is_loaded = True
    assert bpy.ops.witcher.set_cutscene_animation_loaded(source_index=0, load=True) == {'FINISHED'}
    same_path_file_action = ui_cutscene._clip_groups(scene)[0]["strips"][0][1].action
    same_path_backup_action = same_path_file_action.copy()
    same_path_backup_track = actor.animation_data.nla_tracks.new()
    same_path_backup_track.name = export_cutscene.CUTSCENE_TRACK_NAME + "_file_prebake"
    same_path_backup_track.strips.new("same_path_backup", 1, same_path_backup_action)

    changed_scene = bpy.data.scenes.new("clip_identity_changed")
    bpy.context.window.scene = changed_scene
    changed_scene.render.fps = 30
    changed_actor = make_actor(changed_scene, "changed_actor", "identity")
    import_cutscene.ensure_actor_custom_props(changed_actor)
    changed_source = make_action(changed_actor, "changed_source", distance=3.0)
    changed_track, changed_strip = ui_anims._create_cutscene_action_strip(
        bpy.context,
        changed_actor,
        changed_source,
        "identity",
        "Root",
        start_frame=0,
        group_name="changed_clip",
    )
    assert changed_track is not None and changed_strip is not None
    ui_anims._ensure_cutscene_animation_list_entry(
        changed_scene, "identity", "Root", "changed_clip", changed_strip.action,
    )
    ui_cutscene.sync_animation_items_from_scene(changed_scene)
    assert export_cutscene.export_w3_cutscene(bpy.context, str(OUT_CHANGED)) == {'FINISHED'}
    bpy.context.window.scene = scene
    shutil.copyfile(OUT_CHANGED, OUT_OTHER)

    changed_import = import_cutscene.import_w3_cutscene(
        str(OUT_OTHER),
        selected_actor_indices=set(),
        selected_animation_indices=set(),
        auto_apply_selected_animations=False,
        import_burned_audio=False,
    )
    scene.witcher_cs_event_target = "0"
    assert scene.witcher_cs_event_target == "0"
    ui_cutscene._sync_loaded_cutscene_state(scene, str(OUT_OTHER), cutscene_data=changed_import)
    assert scene.witcher_cs_event_target == "ROOT", "new-file row 0 silently inherited the old row 0 selection"
    changed_file_row = next(
        item for item in scene.witcher_cutscene_animation_items
        if item.file_backed and int(item.source_index) == 0
    )
    assert changed_file_row.full_name == "identity:Root:changed_clip"
    assert not changed_file_row.is_loaded
    assert 0 not in ui_cutscene._clip_groups(scene)
    assert same_path_backup_track.name in actor.animation_data.nla_tracks
    assert len(same_path_backup_track.strips) == 1
    assert ui_cutscene._clip_groups(scene)[third_id]["strips"][0][1].as_pointer() == authored_pointer

    legacy_scene = bpy.data.scenes.new("clip_identity_legacy")
    bpy.context.window.scene = legacy_scene
    legacy_actor = make_actor(legacy_scene, "legacy_actor", "legacy")
    legacy_action = make_action(legacy_actor, "legacy_action")
    legacy_action[export_cutscene.CUTSCENE_ANIMATION_NAME_PROP] = "legacy:Root:legacy_action"
    legacy_action[export_cutscene.CUTSCENE_SOURCE_PATH_PROP] = ""
    legacy_action[export_cutscene.CUTSCENE_SOURCE_INDEX_PROP] = 7
    legacy_track = legacy_actor.animation_data.nla_tracks.new()
    legacy_track.name = export_cutscene.CUTSCENE_TRACK_NAME
    legacy_track.strips.new("legacy_action", 1, legacy_action)
    legacy_row = legacy_scene.witcher_cutscene_animation_items.add()
    legacy_row.source_index = 7
    legacy_row.full_name = "legacy:Root:legacy_action"
    legacy_row.actor_name = "legacy"
    legacy_row.component_name = "Root"
    add_entry_event(legacy_scene, 7, "legacy_keep")
    rowless_action = legacy_action.copy()
    rowless_action.name = "legacy_rowless"
    rowless_action[export_cutscene.CUTSCENE_ANIMATION_NAME_PROP] = "legacy:Root:rowless"
    rowless_action[export_cutscene.CUTSCENE_SOURCE_INDEX_PROP] = 6
    legacy_track.strips.new("legacy_rowless", 20, rowless_action)
    add_entry_event(legacy_scene, 6, "legacy_rowless")
    orphan = legacy_scene.witcher_cutscene_animation_items.add()
    orphan.source_index = 8
    orphan.full_name = "legacy:Root:orphan"
    add_entry_event(legacy_scene, 8, "legacy_orphan")
    ghost = legacy_scene.witcher_cutscene_animation_items.add()
    ghost.source_index = 9
    ghost.file_backed = True
    ghost.full_name = "legacy:Root:ghost"
    add_entry_event(legacy_scene, 9, "legacy_ghost")

    file_actor = make_actor(legacy_scene, "legacy_file_actor", "identity")
    file_action = make_action(file_actor, "legacy_file_action")
    file_action[export_cutscene.CUTSCENE_ANIMATION_NAME_PROP] = "identity:Root:same_clip"
    file_action[export_cutscene.CUTSCENE_SOURCE_PATH_PROP] = ""
    file_action[export_cutscene.CUTSCENE_SOURCE_INDEX_PROP] = 0
    file_track = file_actor.animation_data.nla_tracks.new()
    file_track.name = export_cutscene.CUTSCENE_TRACK_NAME
    file_track.strips.new("legacy_file_action", 1, file_action)
    file_row = legacy_scene.witcher_cutscene_animation_items.add()
    file_row.source_index = 0
    file_row.full_name = "identity:Root:same_clip"
    file_row.actor_name = "identity"
    file_row.component_name = "Root"
    legacy_scene.witcher_loaded_w2cutscene_path = str(OUT)
    legacy_scene.witcher_cs_event_target = "7"

    ui_cutscene.sync_animation_items_from_scene(legacy_scene)
    migrated_rows = [
        int(item.source_index) for item in legacy_scene.witcher_cutscene_animation_items
        if not item.file_backed
    ]
    assert len(migrated_rows) == 2 and min(migrated_rows) >= ui_cutscene.AUTHORED_CLIP_ID_BASE, migrated_rows
    migrated_id = int(legacy_action[export_cutscene.CUTSCENE_SOURCE_INDEX_PROP])
    rowless_id = int(rowless_action[export_cutscene.CUTSCENE_SOURCE_INDEX_PROP])
    assert event_ids(legacy_scene) == {
        "legacy_keep": migrated_id,
        "legacy_rowless": rowless_id,
    }, event_ids(legacy_scene)
    assert legacy_scene.witcher_cs_event_target == str(migrated_id)
    assert migrated_id != rowless_id
    assert str(file_action[export_cutscene.CUTSCENE_SOURCE_PATH_PROP]) == str(OUT)
    assert int(file_action[export_cutscene.CUTSCENE_SOURCE_INDEX_PROP]) == 0
    assert next(
        item for item in legacy_scene.witcher_cutscene_animation_items
        if int(item.source_index) == 0
    ).file_backed
    assert legacy_scene.get(ui_cutscene.CLIP_IDENTITY_SCHEMA_PROP) == ui_cutscene.CLIP_IDENTITY_SCHEMA_VERSION
    snapshot = (
        tuple((int(item.source_index), bool(item.file_backed)) for item in legacy_scene.witcher_cutscene_animation_items),
        tuple(sorted(event_ids(legacy_scene).items())),
        int(legacy_scene.get(ui_cutscene.AUTHORED_CLIP_SEQUENCE_PROP, 0)),
    )
    assert ui_cutscene.migrate_cutscene_clip_identity(legacy_scene) is False
    ui_cutscene.sync_animation_items_from_scene(legacy_scene)
    assert snapshot == (
        tuple((int(item.source_index), bool(item.file_backed)) for item in legacy_scene.witcher_cutscene_animation_items),
        tuple(sorted(event_ids(legacy_scene).items())),
        int(legacy_scene.get(ui_cutscene.AUTHORED_CLIP_SEQUENCE_PROP, 0)),
    )

    assert not MISSING.exists(), MISSING
    missing_scene = bpy.data.scenes.new("clip_identity_missing_file")
    bpy.context.window.scene = missing_scene
    missing_actor = make_actor(missing_scene, "missing_file_actor", "missing")
    missing_scene.witcher_loaded_w2cutscene_path = str(MISSING)
    missing_action = make_action(missing_actor, "missing_file_action")
    missing_action[export_cutscene.CUTSCENE_ANIMATION_NAME_PROP] = "missing:Root:file_clip"
    missing_action[export_cutscene.CUTSCENE_SOURCE_PATH_PROP] = str(MISSING)
    missing_action[export_cutscene.CUTSCENE_SOURCE_INDEX_PROP] = 2
    missing_track = missing_actor.animation_data.nla_tracks.new()
    missing_track.name = export_cutscene.CUTSCENE_TRACK_NAME
    missing_track.strips.new("missing_file_action", 1, missing_action)
    missing_file_row = missing_scene.witcher_cutscene_animation_items.add()
    missing_file_row.source_index = 2
    missing_file_row.full_name = "missing:Root:file_clip"

    missing_authored_action = make_action(missing_actor, "missing_authored_action")
    missing_authored_action[export_cutscene.CUTSCENE_ANIMATION_NAME_PROP] = "missing:Root:authored_clip"
    missing_authored_action[export_cutscene.CUTSCENE_SOURCE_PATH_PROP] = ""
    missing_authored_action[export_cutscene.CUTSCENE_SOURCE_INDEX_PROP] = 3
    missing_track.strips.new("missing_authored_action", 20, missing_authored_action)
    missing_authored_row = missing_scene.witcher_cutscene_animation_items.add()
    missing_authored_row.source_index = 3
    missing_authored_row.full_name = "missing:Root:authored_clip"
    add_entry_event(missing_scene, 3, "missing_authored_keep")

    missing_ghost = missing_scene.witcher_cutscene_animation_items.add()
    missing_ghost.source_index = 4
    missing_ghost.file_backed = True
    missing_ghost.full_name = "missing:Root:ghost"
    add_entry_event(missing_scene, 4, "missing_ghost_drop")
    assert ui_cutscene.migrate_cutscene_clip_identity(missing_scene) is True
    migrated_missing_id = int(missing_authored_action[export_cutscene.CUTSCENE_SOURCE_INDEX_PROP])
    assert migrated_missing_id >= ui_cutscene.AUTHORED_CLIP_ID_BASE
    assert int(missing_action[export_cutscene.CUTSCENE_SOURCE_INDEX_PROP]) == 2
    assert next(
        item for item in missing_scene.witcher_cutscene_animation_items
        if int(item.source_index) == 2
    ).file_backed
    assert event_ids(missing_scene) == {"missing_authored_keep": migrated_missing_id}
    assert {int(item.source_index) for item in missing_scene.witcher_cutscene_animation_items} == {
        2, migrated_missing_id,
    }
    missing_snapshot = (
        tuple((int(item.source_index), bool(item.file_backed)) for item in missing_scene.witcher_cutscene_animation_items),
        tuple(sorted(event_ids(missing_scene).items())),
        int(missing_scene.get(ui_cutscene.AUTHORED_CLIP_SEQUENCE_PROP, 0)),
    )
    assert ui_cutscene.migrate_cutscene_clip_identity(missing_scene) is False
    assert missing_snapshot == (
        tuple((int(item.source_index), bool(item.file_backed)) for item in missing_scene.witcher_cutscene_animation_items),
        tuple(sorted(event_ids(missing_scene).items())),
        int(missing_scene.get(ui_cutscene.AUTHORED_CLIP_SEQUENCE_PROP, 0)),
    )

    from witcher3_tools.animation import cutscene_bake
    baked_scene = bpy.data.scenes.new("clip_identity_baked")
    bpy.context.window.scene = baked_scene
    baked_scene.render.fps = 30
    baked_scene.frame_start = 0
    baked_scene.frame_end = 40
    baked_actor = make_actor(baked_scene, "baked_identity_actor", "baked_identity")
    import_cutscene.ensure_actor_custom_props(baked_actor)
    baked_source = make_action(baked_actor, "baked_same_source", distance=2.0)
    baked_ids = []
    for start_frame in (10, 30):
        _track, baked_strip = ui_anims._create_cutscene_action_strip(
            bpy.context,
            baked_actor,
            baked_source,
            "baked_identity",
            "Root",
            start_frame=start_frame,
            group_name="baked_same_clip",
        )
        ui_anims._ensure_cutscene_animation_list_entry(
            baked_scene, "baked_identity", "Root", "baked_same_clip", baked_strip.action,
        )
        baked_ids.append(int(baked_strip.action[export_cutscene.CUTSCENE_SOURCE_INDEX_PROP]))
    assert len(set(baked_ids)) == 2
    add_entry_event(baked_scene, baked_ids[0], "baked_identity_first")
    add_entry_event(baked_scene, baked_ids[1], "baked_identity_second")
    for event in baked_scene.witcher_cutscene_event_items:
        event.animation_name = "baked_identity:Root:baked_same_clip"
    baked_outputs = cutscene_bake.bake_cutscene_actors(bpy.context, set_scene_range=True)
    baked_action = next(action for armature, action in baked_outputs if armature == baked_actor)
    assert set(baked_action[cutscene_bake.BAKED_SOURCE_CLIP_IDS_PROP]) == set(baked_ids)
    assert list(baked_action[cutscene_bake.BAKED_SOURCE_CLIP_STARTS_PROP]) == [10.0, 30.0]
    baked_outputs = cutscene_bake.bake_cutscene_actors(bpy.context, set_scene_range=True)
    baked_action = next(action for armature, action in baked_outputs if armature == baked_actor)
    assert set(baked_action[cutscene_bake.BAKED_SOURCE_CLIP_IDS_PROP]) == set(baked_ids)
    assert list(baked_action[cutscene_bake.BAKED_SOURCE_CLIP_STARTS_PROP]) == [10.0, 30.0]
    assert export_cutscene.export_w3_cutscene(bpy.context, str(OUT_BAKED)) == {'FINISHED'}
    parsed_baked = import_cutscene.loadCutsceneFile(str(OUT_BAKED))
    _cutscene, _actors, _animations, baked_events = import_cutscene.collect_cutscene_preview(
        str(OUT_BAKED), cutscene_template=parsed_baked,
    )
    parsed_baked_events = {
        item["event_name"]: item
        for item in baked_events
        if item["event_name"] in {"baked_identity_first", "baked_identity_second"}
    }
    assert set(parsed_baked_events) == {"baked_identity_first", "baked_identity_second"}, baked_events
    baked_row = next(item for item in _animations if item["actor_name"] == "baked_identity")
    assert {
        int(item["source_index"]) for item in parsed_baked_events.values()
    } == {int(baked_row["source_index"])}, parsed_baked_events
    assert {
        item["animation_name"] for item in parsed_baked_events.values()
    } == {baked_row["full_name"]}, parsed_baked_events
    assert math.isclose(
        parsed_baked_events["baked_identity_first"]["start_time"],
        0.1 + (10.0 / 30.0),
        abs_tol=1e-6,
    )
    assert math.isclose(
        parsed_baked_events["baked_identity_second"]["start_time"],
        0.1 + (30.0 / 30.0),
        abs_tol=1e-6,
    )

    print("W3TB_CUTSCENE_CLIP_IDENTITY_NATIVE_OK")
except Exception:
    traceback.print_exc()
    print("W3TB_CUTSCENE_CLIP_IDENTITY_NATIVE_FAIL")
    sys.exit(1)
finally:
    addon.unregister()
