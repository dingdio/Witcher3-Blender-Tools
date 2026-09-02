import sys
import traceback
from pathlib import Path
from types import SimpleNamespace

import bpy


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import witcher3_tools as addon

def expected_cancel(call, message):
    try:
        return call()
    except RuntimeError as exc:
        assert message in str(exc), exc
        return {'CANCELLED'}


addon.register()
try:
    from witcher3_tools.extension_paths import get_dev_override
    from witcher3_tools.repo_paths import casting
    from witcher3_tools.ui import ui_cutscene
    from witcher3_tools import w3_casting

    details_rna = bpy.ops.witcher.cutscene_exact_value_details.get_rna_type().properties
    for prop_name in ("field_label", "value"):
        assert prop_name in details_rna and details_rna[prop_name].is_skip_save, prop_name
    assert not details_rna["value"].is_readonly
    exact_probe = r"C:\WP7\exact value\probe.w2cutscene"
    clipboard_before = bpy.context.window_manager.clipboard
    copy_reports = []
    copy_context = SimpleNamespace(window_manager=SimpleNamespace(clipboard="before"))
    copy_operator = SimpleNamespace(
        field_label="Loaded File", value=exact_probe,
        report=lambda levels, message: copy_reports.append((levels, message)),
    )
    result = ui_cutscene.WITCH_OT_CutsceneExactValueDetails.execute(copy_operator, copy_context)
    assert result == {'FINISHED'} and copy_context.window_manager.clipboard == exact_probe, (
        result, copy_context.window_manager.clipboard,
    )
    assert copy_reports == [({'INFO'}, "Loaded File copied to clipboard")], copy_reports
    assert bpy.context.window_manager.clipboard == clipboard_before

    prefs = addon._get_prefs(bpy.context)
    uncook_path = str(getattr(prefs, "uncook_path", "") or "")
    ciri_rel = r"quests\main_npcs\cirilla.w2ent"
    if not (Path(uncook_path) / Path(ciri_rel)).is_file():
        uncook_path = str(get_dev_override("fallback_uncook_path_w3", "") or "")
    prefs.uncook_path = uncook_path
    assert (Path(prefs.uncook_path) / Path(ciri_rel)).is_file(), "Configure uncook_path before running this test"

    assert not hasattr(bpy.types.Scene, "witcher_casting_query")
    operator_rna = bpy.ops.witcher.cast_actor.get_rna_type().properties
    assert "candidate" not in operator_rna
    for name in (
        "query", "candidates", "candidate_index", "template_path", "appearance_choice", "appearance",
    ):
        assert name in operator_rna, name
        assert operator_rna[name].is_skip_save, name

    props = bpy.context.window_manager.operator_properties_last("WITCHER_OT_cast_actor")
    ui_cutscene.reset_cast_actor_dialog(props)
    assert (
        props.query,
        props.candidate_index,
        props.template_path,
        props.selected_category,
        props.selected_rigs,
        props.selected_indexed,
        props.match_status,
        props.appearance_choice,
        props.appearance,
    ) == ("", -1, "", "", "", False, "", "__DEFAULT__", "")
    assert len(props.candidates) == 0

    props.query = "geralt_player"
    geralt_path = r"characters\player_entities\geralt\geralt_player.w2ent"
    geralt_row = next(row for row in props.candidates if row.template_path == geralt_path)
    assert geralt_row.label == f"{geralt_path} [geralt]", geralt_row.label
    assert "163367" not in geralt_row.label

    props.query = "Dandelion"
    dandelion_rows = [row for row in props.candidates if row.label.endswith(" [Dandelion]")]
    assert len(dandelion_rows) == 3, [(row.template_path, row.label) for row in props.candidates]
    assert len({row.label for row in dandelion_rows}) == 3
    assert all(row.label == f"{row.template_path} [Dandelion]" for row in dandelion_rows)

    props.query = "ciri"
    ciri_paths = [row.template_path for row in props.candidates]
    assert ciri_rel in ciri_paths and props.candidate_index == -1, ciri_paths[:5]
    assert not props.template_path, "search change must not retain or auto-select a candidate"
    props.candidate_index = ciri_paths.index(ciri_rel)
    assert props.template_path == ciri_rel and props.selected_category == "character"
    assert "woman_base.w2rig" in props.selected_rigs
    appearance_ids = [item[0] for item in ui_cutscene._cast_appearance_items(props, bpy.context)]
    assert appearance_ids[:5] == ["__DEFAULT__", "ciri", "__q305_dudu", "__q205_naked", "__q505_hooded"], appearance_ids[:8]
    assert len(appearance_ids) == len(set(appearance_ids)), appearance_ids
    appearance_items = ui_cutscene._CAST_APPEARANCE_ITEMS
    appearance_history_size = len(ui_cutscene._CAST_APPEARANCE_ITEM_HISTORY)
    ui_cutscene._set_cast_appearance_items(casting.casting_record(ciri_rel))
    assert ui_cutscene._CAST_APPEARANCE_ITEMS is appearance_items
    assert len(ui_cutscene._CAST_APPEARANCE_ITEM_HISTORY) == appearance_history_size
    props.appearance_choice = "__q305_dudu"
    assert props.appearance == "__q305_dudu"

    props.query = "drowner"
    drowner_rel = r"characters\npc_entities\monsters\drowner_lvl1.w2ent"
    drowner_paths = [row.template_path for row in props.candidates]
    assert drowner_rel in drowner_paths and props.candidate_index == -1, drowner_paths[:5]
    assert not props.template_path and not props.appearance, "query B must invalidate query A selection"
    props.candidate_index = drowner_paths.index(drowner_rel)
    assert props.template_path == drowner_rel
    assert [item[0] for item in ui_cutscene._cast_appearance_items(props, bpy.context)] == [
        "__DEFAULT__", "drowner_01",
    ]

    props.query = ciri_rel
    assert len(props.candidates) == 1 and props.candidates[0].indexed
    props.candidate_index = 0
    assert props.template_path == ciri_rel and props.selected_indexed

    raw_path = r"characters\npc_entities\test\popup_probe.w2ent"
    props.query = raw_path
    assert len(props.candidates) == 1 and not props.candidates[0].indexed
    props.candidate_index = 0
    assert props.template_path == raw_path and not props.selected_indexed

    real_resolve_cast = casting.resolve_cast

    def capped_hits(_query, category=None, limit=8):
        assert limit == 101 and category is None
        return [
            {"path": f"characters\\probe\\actor_{index:03d}.w2ent", "alias": f"actor {index}", "record": None}
            for index in range(101)
        ]

    casting.resolve_cast = capped_hits
    try:
        props.query = "popup cap probe"
    finally:
        casting.resolve_cast = real_resolve_cast
    assert len(props.candidates) == 100
    assert props.match_status == "100+ matches — refine the search", props.match_status

    props.query = "ciri"
    props.candidate_index = [row.template_path for row in props.candidates].index(ciri_rel)

    props.appearance_choice = "ciri"
    props.query = "stale state"
    ui_cutscene.reset_cast_actor_dialog(props)
    assert not props.query and len(props.candidates) == 0 and props.candidate_index == -1
    assert not props.template_path and not props.appearance and props.appearance_choice == "__DEFAULT__"
    props.query = "ciri"
    props.candidate_index = [row.template_path for row in props.candidates].index(ciri_rel)
    ui_cutscene.reset_cast_actor_dialog(props)
    assert not props.query and len(props.candidates) == 0, "every reopen must start blank"

    result = bpy.ops.witcher.cast_actor(
        'EXEC_DEFAULT',
        template_path=ciri_rel,
        appearance="ciri",
    )
    assert result == {'FINISHED'}, result
    scene = bpy.context.scene
    active_index = scene.witcher_cutscene_loaded_actor_index
    assert 0 <= active_index < len(scene.witcher_cutscene_actor_items)
    active_row = scene.witcher_cutscene_actor_items[active_index]
    assert active_row.actor_name == "cirilla" and active_row.template_path == ciri_rel, (
        active_row.actor_name, active_row.template_path,
    )
    assert active_row.appearance_name == "ciri" and active_row.is_loaded
    ciri_object_name = str(active_row.object_name)

    existing_pointers = {obj.as_pointer() for obj in bpy.data.objects}
    existing_armatures = {data.as_pointer() for data in bpy.data.armatures}
    real_cast_actor = w3_casting.cast_actor

    def leaking_failure(*_args, **_kwargs):
        obj = bpy.data.objects.new("cast_failure_probe", bpy.data.armatures.new("cast_failure_probe_data"))
        bpy.context.scene.collection.objects.link(obj)
        raise RuntimeError("intentional popup failure")

    w3_casting.cast_actor = leaking_failure
    try:
        failed = expected_cancel(
            lambda: bpy.ops.witcher.cast_actor(
                'EXEC_DEFAULT',
                template_path=r"characters\probe\failure.w2ent",
            ),
            "intentional popup failure",
        )
    finally:
        w3_casting.cast_actor = real_cast_actor
    assert failed == {'CANCELLED'}, failed
    assert {obj.as_pointer() for obj in bpy.data.objects} == existing_pointers
    assert {data.as_pointer() for data in bpy.data.armatures} == existing_armatures
    assert bpy.data.objects.get("cast_failure_probe") is None
    assert bpy.data.armatures.get("cast_failure_probe_data") is None

    real_sync = ui_cutscene._sync_actor_items_with_scene

    def post_import_failure(*_args, **_kwargs):
        obj = bpy.data.objects.new("cast_sync_failure_probe", bpy.data.armatures.new("cast_sync_failure_probe_data"))
        bpy.context.scene.collection.objects.link(obj)
        return obj, {
            "label": "cast_sync_failure_probe",
            "template": r"characters\probe\sync_failure.w2ent",
            "appearance": "",
        }

    def failing_sync(_scene):
        raise RuntimeError("intentional post-import sync failure")

    w3_casting.cast_actor = post_import_failure
    ui_cutscene._sync_actor_items_with_scene = failing_sync
    try:
        failed = expected_cancel(
            lambda: bpy.ops.witcher.cast_actor(
                'EXEC_DEFAULT',
                template_path=r"characters\probe\sync_failure.w2ent",
            ),
            "intentional post-import sync failure",
        )
    finally:
        w3_casting.cast_actor = real_cast_actor
        ui_cutscene._sync_actor_items_with_scene = real_sync
    assert failed == {'CANCELLED'}, failed
    assert {obj.as_pointer() for obj in bpy.data.objects} == existing_pointers
    assert {data.as_pointer() for data in bpy.data.armatures} == existing_armatures
    assert bpy.data.objects.get("cast_sync_failure_probe") is None
    assert bpy.data.armatures.get("cast_sync_failure_probe_data") is None

    from witcher3_tools.filtered_list.animations_manager import (
        CModSbUiAnimationList,
        CModStoryBoardAnimationListsManager,
    )
    from witcher3_tools.filtered_list.filtered_list import SModUiCategorizedListItem
    from witcher3_tools.repo_paths import repo_file_for_source
    from witcher3_tools.ui import ui_anims, ui_anims_list

    def scene_state(scene):
        tracks = []
        for obj in bpy.data.objects:
            anim_data = getattr(obj, "animation_data", None)
            for track in (anim_data.nla_tracks if anim_data else ()):
                tracks.append((
                    obj.as_pointer(), track.as_pointer(), track.name, track.mute,
                    tuple(
                        (
                            strip.as_pointer(), strip.name, float(strip.frame_start), float(strip.frame_end),
                            strip.mute, strip.action.as_pointer() if strip.action else 0,
                        )
                        for strip in track.strips
                    ),
                ))
        return (
            tuple(sorted((obj.as_pointer(), obj.name, obj.select_get()) for obj in bpy.data.objects)),
            bpy.context.view_layer.objects.active.as_pointer() if bpy.context.view_layer.objects.active else 0,
            tuple(sorted((action.as_pointer(), action.name) for action in bpy.data.actions)),
            tuple(sorted(tracks)),
            tuple(
                (
                    int(row.source_index), bool(row.file_backed), row.full_name, row.display_name,
                    row.actor_name, row.component_name, bool(row.is_loaded), bool(row.muted),
                )
                for row in scene.witcher_cutscene_animation_items
            ),
            tuple(
                (row.event_scope, int(row.source_index), row.event_name)
                for row in scene.witcher_cutscene_event_items
            ),
            int(scene.witcher_cutscene_loaded_anim_index),
            int(scene.get(ui_cutscene.AUTHORED_CLIP_SEQUENCE_PROP, 0)),
            int(scene.frame_current), float(scene.frame_subframe), int(scene.frame_start), int(scene.frame_end),
            int(scene.render.fps), float(scene.render.fps_base),
        )

    def assert_no_browse_debris():
        for obj in bpy.data.objects:
            anim_data = getattr(obj, "animation_data", None)
            for track in (anim_data.nla_tracks if anim_data else ()):
                name = str(track.name)
                assert not name.startswith(ui_anims.CUTSCENE_BROWSE_TEMP_TRACK_PREFIX), name
                assert not name.lower().startswith("anim_import"), name
        assert not [
            action.name for action in bpy.data.actions
            if action.name.startswith(ui_anims.CUTSCENE_BROWSE_TEMP_TRACK_PREFIX)
        ]

    scene = bpy.context.scene
    ciri_actor = bpy.data.objects.get(ciri_object_name)
    assert ciri_actor is not None and ciri_actor.type == 'ARMATURE', ciri_object_name
    ciri_row_index = next(
        index for index, row in enumerate(scene.witcher_cutscene_actor_items)
        if str(row.object_name) == ciri_object_name
    )
    scene.witcher_cutscene_loaded_actor_index = ciri_row_index

    old_manager_globals = (
        CModStoryBoardAnimationListsManager.active,
        CModStoryBoardAnimationListsManager.active_list,
    )
    active_sentinel, list_sentinel = object(), object()
    CModStoryBoardAnimationListsManager.active = active_sentinel
    CModStoryBoardAnimationListsManager.active_list = list_sentinel
    try:
        catalog = ui_anims_list.build_animation_catalog_records(
            bpy.context, ciri_actor, source_game="AUTO", compatible_only=True,
        )
        assert CModStoryBoardAnimationListsManager.active is active_sentinel
        assert CModStoryBoardAnimationListsManager.active_list is list_sentinel
    finally:
        CModStoryBoardAnimationListsManager.active, CModStoryBoardAnimationListsManager.active_list = old_manager_globals
    assert catalog, "Ciri must expose a compatible animation catalog"

    real_records = []
    for record in catalog:
        if record["component"] != "BODY" or not record["animation_id"] or int(record["frames"]) <= 0:
            continue
        full_path = repo_file_for_source(record["repo_path"], record["source_game"])
        if Path(full_path).is_file() and str(full_path).lower().endswith(".w2anims"):
            real_records.append((int(record["frames"]), record, full_path))
    assert real_records, "Ciri needs a real uncooked compatible .w2anims file for Browse & Add"
    _frames, add_record, add_source = min(real_records, key=lambda item: item[0])

    browse = bpy.context.window_manager.witcher_cutscene_browse_animation
    for name in ("selected_catalog_id", "selected_category", "refresh_token"):
        assert name not in browse.bl_rna.properties, name
    assert "catalog_id" not in bpy.ops.witcher.cutscene_browse_add_animation.get_rna_type().properties
    invoke_calls = []
    fake_wm = SimpleNamespace(
        witcher_cutscene_browse_animation=browse,
        invoke_props_dialog=lambda _operator, **kwargs: invoke_calls.append(kwargs) or {'RUNNING_MODAL'},
    )
    invoke_context = SimpleNamespace(scene=scene, window_manager=fake_wm)
    invoke_operator = SimpleNamespace(actor_object_name="", report=lambda *_args: None)
    browse.refreshing = True
    try:
        browse.actor_object_name = "stale actor"
        browse.query = "stale query"
        browse.source = "W2"
        browse.compatibility = "ALL"
        browse.component = "FACE"
        browse.placement = "AFTER_LAST"
        stale = browse.results.add()
        stale.animation_id = "stale"
        browse.result_index = 0
    finally:
        browse.refreshing = False
    invoked = ui_cutscene.WITCH_OT_CutsceneBrowseAddAnimation.invoke(invoke_operator, invoke_context, None)
    assert invoked == {'RUNNING_MODAL'} and invoke_calls, invoked
    assert invoke_operator.actor_object_name == ciri_object_name
    assert (browse.actor_object_name, browse.query, browse.source, browse.compatibility) == (
        ciri_object_name, "", "AUTO", "COMPATIBLE",
    )
    assert (browse.category, browse.component, browse.placement, browse.result_index) == ("ALL", "BODY", "CURRENT", -1)

    expected = ui_anims_list.filter_animation_catalog_records(catalog)[:100]
    actual = [
        (
            row.animation_id, row.caption, row.category, row.repo_path,
            int(row.frames), round(float(row.duration), 5), row.component, row.source_game, bool(row.compatible),
        )
        for row in browse.results
    ]
    expected_rows = [
        (
            row["animation_id"], row["caption"], row["category"], row["repo_path"],
            int(row["frames"]), round(int(row["frames"]) / 30.0, 5), row["component"],
            row["source_game"], bool(row["compatible"]),
        )
        for row in expected
    ]
    if actual != expected_rows:
        mismatch = next(
            ((index, got, wanted) for index, (got, wanted) in enumerate(zip(actual, expected_rows)) if got != wanted),
            (min(len(actual), len(expected_rows)), None, None),
        )
        raise AssertionError(("Browse results must match the shared compatible catalog", len(actual), len(expected_rows), mismatch))

    category_items = ui_cutscene._CUTSCENE_BROWSE_CATEGORY_ITEMS
    category_history_size = len(ui_cutscene._CUTSCENE_BROWSE_CATEGORY_ITEM_HISTORY)
    ui_cutscene.refresh_cutscene_browse_animation_results(browse, bpy.context)
    assert ui_cutscene._CUTSCENE_BROWSE_CATEGORY_ITEMS is category_items
    assert len(ui_cutscene._CUTSCENE_BROWSE_CATEGORY_ITEM_HISTORY) == category_history_size

    synthetic = [
        dict(add_record, animation_id="cname_only_probe", caption="plain label", component="BODY"),
        dict(add_record, animation_id="other_id", caption="caption only probe", component="FACE"),
    ]
    normal_filter = CModSbUiAnimationList()
    normal_filter._items = [
        SModUiCategorizedListItem("cname_only_probe", "cname only probe"),
        SModUiCategorizedListItem("other_id", "caption only probe"),
    ]
    real_builder = ui_anims_list.build_animation_catalog_records
    ui_anims_list.build_animation_catalog_records = lambda *_args, **_kwargs: synthetic
    try:
        for query, wanted, component in (
            ("  CNAME__ONLY  ", "cname_only_probe", "BODY"),
            ("CAPTION___ONLY", "other_id", "FACE"),
        ):
            normal_filter.setWildcardFilter(query)
            assert [item.id for item in normal_filter._items if not item.isWildcardMiss] == [wanted], query
            browse.refreshing = True
            try:
                browse.query = query
                browse.category = "ALL"
            finally:
                browse.refreshing = False
            ui_cutscene.refresh_cutscene_browse_animation_results(browse, bpy.context)
            assert [row.animation_id for row in browse.results] == [wanted], (query, browse.match_status)
            browse.component = "FACE" if component == "BODY" else "BODY"
            before_hint_selection = scene_state(scene)
            assert ui_cutscene.select_cutscene_browse_animation_result(browse, 0)
            assert (browse.component, browse.selected_component) == (component, component)
            assert scene_state(scene) == before_hint_selection, "component hint selection must not mutate scene data"
    finally:
        ui_anims_list.build_animation_catalog_records = real_builder

    ui_cutscene.reset_cutscene_browse_animation_dialog(
        SimpleNamespace(actor_object_name=ciri_object_name), bpy.context,
    )

    before_selection = scene_state(scene)
    assert ui_cutscene.select_cutscene_browse_animation_result(browse, 0)
    assert scene_state(scene) == before_selection, "catalog selection must not mutate scene/NLA/actions"

    assert_no_browse_debris()
    scene.frame_start, scene.frame_end = 7, 333
    scene.render.fps, scene.render.fps_base = 24, 1.001
    scene.frame_set(30)
    timing_before = (scene.frame_current, scene.frame_subframe, scene.frame_start, scene.frame_end,
                     scene.render.fps, scene.render.fps_base)
    authored_before = {
        int(row.source_index) for row in scene.witcher_cutscene_animation_items
        if not row.file_backed and int(row.source_index) >= ui_cutscene.AUTHORED_CLIP_ID_BASE
    }
    action_ptrs_before = {action.as_pointer() for action in bpy.data.actions}
    result = bpy.ops.witcher.cutscene_browse_add_animation(
        'EXEC_DEFAULT', actor_object_name=ciri_object_name,
        animation_id=add_record["animation_id"], source_path=add_record["repo_path"],
        source_game=add_record["source_game"], component="BODY", placement="CURRENT",
    )
    assert result == {'FINISHED'}, result
    authored_after = {
        int(row.source_index) for row in scene.witcher_cutscene_animation_items
        if not row.file_backed and int(row.source_index) >= ui_cutscene.AUTHORED_CLIP_ID_BASE
    }
    new_ids = authored_after - authored_before
    assert len(new_ids) == 1, (authored_before, authored_after)
    current_id = next(iter(new_ids))
    current_rows = [
        (index, bool(row.file_backed)) for index, row in enumerate(scene.witcher_cutscene_animation_items)
        if int(row.source_index) == current_id
    ]
    assert current_rows == [(scene.witcher_cutscene_loaded_anim_index, False)], current_rows
    current_row = scene.witcher_cutscene_animation_items[current_rows[0][0]]
    effective_fps = float(scene.render.fps) / float(scene.render.fps_base)
    assert abs(ui_cutscene._scene_fps(scene) - effective_fps) < 1e-9
    assert abs(float(current_row.frames_per_second) - effective_fps) < 1e-5, (
        current_row.frames_per_second, effective_fps,
    )
    assert abs(float(current_row.duration) - (int(current_row.num_frames) / effective_fps)) < 1e-5
    current_group = ui_cutscene._clip_groups(scene)[current_id]
    assert min(float(strip.frame_start) for _track, strip in current_group["strips"]) == 30.0
    current_actions = {strip.action.as_pointer() for _track, strip in current_group["strips"]}
    assert {action.as_pointer() for action in bpy.data.actions} - action_ptrs_before == current_actions
    assert (scene.frame_current, scene.frame_subframe, scene.frame_start, scene.frame_end,
            scene.render.fps, scene.render.fps_base) == timing_before
    assert_no_browse_debris()

    first_track, first_strip = current_group["strips"][0]
    base_signature = ui_cutscene._clip_signature(scene)
    frame_start = float(first_strip.frame_start)
    first_strip.frame_start = frame_start + 1.0
    assert ui_cutscene._clip_signature(scene) != base_signature
    first_strip.frame_start = frame_start
    frame_end = float(first_strip.frame_end)
    first_strip.frame_end = frame_end + 1.0
    assert ui_cutscene._clip_signature(scene) != base_signature
    first_strip.frame_end = frame_end
    strip_name = str(first_strip.name)
    first_strip.name = f"{strip_name}_signature_probe"
    assert ui_cutscene._clip_signature(scene) != base_signature
    first_strip.name = strip_name
    source_prop = ui_anims.export_cutscene.CUTSCENE_SOURCE_PATH_PROP
    source_path = str(first_strip.action.get(source_prop, "") or "")
    first_strip.action[source_prop] = f"{source_path}.signature_probe"
    assert ui_cutscene._clip_signature(scene) != base_signature
    first_strip.action[source_prop] = source_path
    source_action = first_strip.action
    probe_action = source_action.copy()
    try:
        first_strip.action = probe_action
        assert ui_cutscene._clip_signature(scene) != base_signature
    finally:
        first_strip.action = source_action
        bpy.data.actions.remove(probe_action)
    assert ui_cutscene._clip_signature(scene) == base_signature

    event_items = ui_cutscene._event_target_items(None, bpy.context)
    event_history_size = len(ui_cutscene._EVENT_TARGET_ITEM_HISTORY)
    assert ui_cutscene._event_target_items(None, bpy.context) is event_items
    assert len(ui_cutscene._EVENT_TARGET_ITEM_HISTORY) == event_history_size

    target = next(
        obj for obj in bpy.data.objects
        if any(track.as_pointer() == first_track.as_pointer()
               for track in (getattr(getattr(obj, "animation_data", None), "nla_tracks", ()) or ()))
    )
    existing_end = max(
        float(strip.frame_end)
        for obj in bpy.data.objects
        for track in (getattr(getattr(obj, "animation_data", None), "nla_tracks", ()) or ())
        if track.name in (first_track.name, f"{first_track.name}_2")
        for strip in track.strips
    )
    edit_end = existing_end + 137.0
    edits = target.animation_data.nla_tracks.new()
    edits.name = f"{first_track.name}_2"
    edit_strip = edits.strips.new("after_last_extent_probe", int(edit_end - 10.0), first_strip.action)
    edit_strip.frame_start = edit_end - 10.0
    edit_strip.frame_end = edit_end
    actual_end = edit_end + 137.0
    file_track = target.animation_data.nla_tracks.new()
    file_track.name = f"{first_track.name}_file_42"
    file_strip = file_track.strips.new("file_backed_extent_probe", int(actual_end - 10.0), first_strip.action)
    file_strip.frame_start = actual_end - 10.0
    file_strip.frame_end = actual_end

    authored_before = authored_after
    action_ptrs_before = {action.as_pointer() for action in bpy.data.actions}
    result = bpy.ops.witcher.cutscene_browse_add_animation(
        'EXEC_DEFAULT', actor_object_name=ciri_object_name,
        animation_id=add_record["animation_id"], source_path=add_record["repo_path"],
        source_game=add_record["source_game"], component="BODY", placement="AFTER_LAST",
    )
    assert result == {'FINISHED'}, result
    authored_after = {
        int(row.source_index) for row in scene.witcher_cutscene_animation_items
        if not row.file_backed and int(row.source_index) >= ui_cutscene.AUTHORED_CLIP_ID_BASE
    }
    new_ids = authored_after - authored_before
    assert len(new_ids) == 1, (authored_before, authored_after)
    after_last_id = next(iter(new_ids))
    after_rows = [
        (index, bool(row.file_backed)) for index, row in enumerate(scene.witcher_cutscene_animation_items)
        if int(row.source_index) == after_last_id
    ]
    assert after_rows == [(scene.witcher_cutscene_loaded_anim_index, False)], after_rows
    after_group = ui_cutscene._clip_groups(scene)[after_last_id]
    assert min(float(strip.frame_start) for _track, strip in after_group["strips"]) == actual_end
    after_actions = {strip.action.as_pointer() for _track, strip in after_group["strips"]}
    assert {action.as_pointer() for action in bpy.data.actions} - action_ptrs_before == after_actions
    assert (scene.frame_current, scene.frame_subframe, scene.frame_start, scene.frame_end,
            scene.render.fps, scene.render.fps_base) == timing_before
    assert_no_browse_debris()

    multipart_source_action = after_group["strips"][0][1].action

    def multipart_loader(_context, _animation_name, _path, _actor_obj, **kwargs):
        temp_track = target.animation_data.nla_tracks.new()
        temp_track.name = kwargs["NLA_track"]
        for part, start in enumerate((0.0, 12.0), 1):
            temp_action = multipart_source_action.copy()
            temp_action.name = f"{ui_anims.CUTSCENE_BROWSE_TEMP_TRACK_PREFIX}_action_{part}"
            temp_strip = temp_track.strips.new(f"multipart_{part}", int(start), temp_action)
            temp_strip.frame_start = start
            temp_strip.frame_end = start + 8.0
        return [target]

    real_loader = ui_anims.load_anim_into_scene
    ui_anims.load_anim_into_scene = multipart_loader
    try:
        authored_before = authored_after
        action_ptrs_before = {action.as_pointer() for action in bpy.data.actions}
        result = bpy.ops.witcher.cutscene_browse_add_animation(
            'EXEC_DEFAULT', actor_object_name=ciri_object_name,
            animation_id="multipart_probe", source_path=add_record["repo_path"],
            source_game=add_record["source_game"], component="BODY", placement="CURRENT",
        )
    finally:
        ui_anims.load_anim_into_scene = real_loader
    assert result == {'FINISHED'}, result
    authored_after = {
        int(row.source_index) for row in scene.witcher_cutscene_animation_items
        if not row.file_backed and int(row.source_index) >= ui_cutscene.AUTHORED_CLIP_ID_BASE
    }
    multipart_ids = authored_after - authored_before
    assert len(multipart_ids) == 1, multipart_ids
    multipart_id = next(iter(multipart_ids))
    multipart_rows = [
        (index, bool(row.file_backed)) for index, row in enumerate(scene.witcher_cutscene_animation_items)
        if int(row.source_index) == multipart_id
    ]
    assert multipart_rows == [(scene.witcher_cutscene_loaded_anim_index, False)], multipart_rows
    multipart_group = ui_cutscene._clip_groups(scene)[multipart_id]
    assert len(multipart_group["strips"]) == 2, multipart_group["strips"]
    assert {
        int(strip.action.get(ui_anims.export_cutscene.CUTSCENE_SOURCE_INDEX_PROP, -1))
        for _track, strip in multipart_group["strips"]
    } == {multipart_id}
    multipart_actions = {strip.action.as_pointer() for _track, strip in multipart_group["strips"]}
    assert {action.as_pointer() for action in bpy.data.actions} - action_ptrs_before == multipart_actions
    assert_no_browse_debris()

    partial_before = scene_state(scene)
    partial_sequence = partial_before[7]
    real_create_strip = ui_anims._create_cutscene_action_strip
    promoted = {}

    def fail_second_promotion(*args, **kwargs):
        if promoted:
            source_index = promoted["source_index"]
            row = scene.witcher_cutscene_animation_items.add()
            row.source_index = source_index
            row.file_backed = False
            row.full_name = "cirilla:Root:partial_promotion_probe"
            row.actor_name = "cirilla"
            row.component_name = "Root"
            event = scene.witcher_cutscene_event_items.add()
            event.event_scope = "ENTRY"
            event.source_index = source_index
            event.event_name = "partial_promotion_event_probe"
            raise RuntimeError("intentional partial promotion failure")
        track, strip = real_create_strip(*args, **kwargs)
        promoted["source_index"] = int(
            strip.action.get(ui_anims.export_cutscene.CUTSCENE_SOURCE_INDEX_PROP, -1)
        )
        return track, strip

    ui_anims.load_anim_into_scene = multipart_loader
    ui_anims._create_cutscene_action_strip = fail_second_promotion
    try:
        failed = expected_cancel(
            lambda: bpy.ops.witcher.cutscene_browse_add_animation(
                'EXEC_DEFAULT', actor_object_name=ciri_object_name,
                animation_id="partial_promotion_probe", source_path=add_record["repo_path"],
                source_game=add_record["source_game"], component="BODY", placement="CURRENT",
            ),
            "intentional partial promotion failure",
        )
    finally:
        ui_anims.load_anim_into_scene = real_loader
        ui_anims._create_cutscene_action_strip = real_create_strip
    assert failed == {'CANCELLED'} and promoted, (failed, promoted)
    partial_after = scene_state(scene)
    assert partial_after[:7] + partial_after[8:] == partial_before[:7] + partial_before[8:]
    assert partial_after[7] == partial_sequence + 1, (partial_sequence, partial_after[7])
    assert not [row for row in scene.witcher_cutscene_animation_items if int(row.source_index) == promoted["source_index"]]
    assert not [row for row in scene.witcher_cutscene_event_items if int(row.source_index) == promoted["source_index"]]
    assert_no_browse_debris()

    atomic_before = scene_state(scene)
    failed = expected_cancel(
        lambda: bpy.ops.witcher.cutscene_browse_add_animation(
            'EXEC_DEFAULT', actor_object_name=ciri_object_name,
            animation_id="missing_materialization_probe",
            source_path=r"animations\__missing_wp4_probe__.w2anims",
            source_game="w3", component="BODY", placement="CURRENT",
        ),
        "__missing_wp4_probe__.w2anims",
    )
    assert failed == {'CANCELLED'}, failed
    assert scene_state(scene) == atomic_before, "failed materialization must be atomic"
    assert_no_browse_debris()

    print("W3TB_CUTSCENE_POPUPS_NATIVE_OK")
except Exception:
    traceback.print_exc()
    print("W3TB_CUTSCENE_POPUPS_NATIVE_FAIL")
    sys.exit(1)
finally:
    addon.unregister()
