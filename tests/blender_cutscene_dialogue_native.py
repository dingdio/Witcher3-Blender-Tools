import inspect
import subprocess
import sys
import tempfile
import traceback
import wave
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import bpy

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import witcher3_tools as addon


def assert_preview_line_baseline(scene, ui_cutscene):
    scene.render.fps = 30
    lines = [
        {"actor": "GERALT", "line_id": "1001", "line_index": 1001, "line_text": "First line."},
        {"actor": "CIRI", "line_id": "1002", "line_index": 1002, "line_text": "Second line."},
    ]
    events = [
        {"frame": 12, "duration_frames": 30},
        {"frame": 60, "duration_frames": 0},
    ]
    ui_cutscene._populate_cutscene_dialog_items(scene, lines, events)
    assert len(scene.witcher_cutscene_dialog_items) == 2
    first = scene.witcher_cutscene_dialog_items[0]
    second = scene.witcher_cutscene_dialog_items[1]
    assert (first.actor, first.line_id, first.start_frame, first.end_frame) == ("GERALT", "1001", 12, 42)
    assert second.actor == "CIRI" and second.start_frame == 60 and second.end_frame > second.start_frame
    ui_cutscene._populate_cutscene_dialog_items(scene, [], [])
    assert len(scene.witcher_cutscene_dialog_items) == 0


def assert_authored_line_model(scene, ui_cutscene):
    scene.witcher_cutscene_actor_items.clear()
    scene.witcher_cutscene_dialog_lines.clear()
    scene.witcher_cutscene_dialog_items.clear()
    scene.render.fps = 30
    scene.render.fps_base = 1.0
    scene.frame_current = 10

    for name in ("GERALT", "CIRI"):
        actor = scene.witcher_cutscene_actor_items.add()
        actor.actor_name = actor.voice_tag = name
        actor.actor_type = "CAT_Actor"
    scene.witcher_cutscene_loaded_actor_index = 0
    first_index = ui_cutscene.add_cutscene_dialog_line(scene, text="Authored exact text")
    first = scene.witcher_cutscene_dialog_lines[first_index]
    assert first.speaker == "GERALT"
    assert (first.start_frame, first.end_frame) == (
        10,
        10 + ui_cutscene._dialog_default_duration_frames(first.text, 30.0),
    )
    assert first.tier == 'SUBTITLE'
    for field in ("game_line_id", "game_voice_file_name", "wav_path", "allocated_line_id", "lipsync_ref"):
        assert hasattr(first, field), field
    first.allocated_line_id = "200001"

    scene.witcher_cutscene_loaded_actor_index = 1
    scene.frame_current = 80
    second_index = ui_cutscene.add_cutscene_dialog_line(scene, text="Second authored line")
    second = scene.witcher_cutscene_dialog_lines[second_index]
    assert second.speaker == "CIRI"
    second.tier = 'GAME'
    second.game_line_id = "1002"
    assert ui_cutscene._cutscene_dialog_speaker_candidates(scene) == ["GERALT", "CIRI"]
    second.speaker = "CUSTOM_TAG"
    assert second.speaker == "CUSTOM_TAG"
    second.speaker = "CIRI"

    assert ui_cutscene.move_cutscene_dialog_line(scene, -1) == 0
    assert [line.text for line in scene.witcher_cutscene_dialog_lines] == [
        "Second authored line", "Authored exact text",
    ]
    moved = scene.witcher_cutscene_dialog_lines[0]
    duration = moved.end_frame - moved.start_frame
    scene.frame_current = 200
    assert ui_cutscene.set_cutscene_dialog_line_from_playhead(scene)
    moved = scene.witcher_cutscene_dialog_lines[0]
    assert (moved.start_frame, moved.end_frame) == (200, 200 + duration)

    assert ui_cutscene.sync_authored_cutscene_dialog_items(scene) == 2
    assert [item.line_text for item in scene.witcher_cutscene_dialog_items] == [
        "Second authored line", "Authored exact text",
    ]
    assert ui_cutscene._cutscene_get_active_subtitle(scene, 200) == "Second authored line"
    assert ui_cutscene.refresh_cutscene_dialog_language(bpy.context) == 2
    assert scene.witcher_cutscene_dialog_items[0].line_text == "Second authored line"
    assert ui_cutscene.remove_cutscene_dialog_line(scene)
    assert len(scene.witcher_cutscene_dialog_lines) == len(scene.witcher_cutscene_dialog_items) == 1

    scene.witcher_cutscene_dialog_lines.clear()
    scene.witcher_cutscene_dialog_items.clear()
    for text, start, end in (("First", 10, 20), ("Second", 20, 30)):
        item = scene.witcher_cutscene_dialog_items.add()
        item.line_text, item.start_frame, item.end_frame = text, start, end
    assert ui_cutscene._cutscene_get_active_subtitle(scene, 19) == "First"
    assert ui_cutscene._cutscene_get_active_subtitle(scene, 20) == "Second"
    assert ui_cutscene._cutscene_get_active_subtitle(scene, 30) is None

    scene.witcher_cutscene_dialog_lines.clear()
    scene.witcher_cutscene_dialog_items.clear()
    for actor, text, line_id, start, end in (
        ("GERALT", "Imported first", "3001", 12, 42),
        ("CIRI", "Imported second", "3002", 60, 95),
    ):
        item = scene.witcher_cutscene_dialog_items.add()
        item.actor, item.line_text, item.line_id = actor, text, line_id
        item.start_frame, item.end_frame = start, end
    assert ui_cutscene.copy_cutscene_preview_to_authored(scene) == 2
    assert [line.speaker for line in scene.witcher_cutscene_dialog_lines] == ["GERALT", "CIRI"]
    assert [line.text for line in scene.witcher_cutscene_dialog_lines] == ["Imported first", "Imported second"]
    assert [line.tier for line in scene.witcher_cutscene_dialog_lines] == ['GAME', 'GAME']
    assert [line.game_line_id for line in scene.witcher_cutscene_dialog_lines] == ["3001", "3002"]
    assert [item.line_text for item in scene.witcher_cutscene_dialog_items] == ["Imported first", "Imported second"]

    scene.witcher_cutscene_dialog_lines.clear()
    scene.witcher_cutscene_dialog_items.clear()
    scene.witcher_cutscene_actor_items.clear()
    scene.frame_current = 25
    assert bpy.ops.witcher.cutscene_dialog_add_line() == {'FINISHED'}
    assert len(scene.witcher_cutscene_dialog_lines) == 1
    assert bpy.ops.witcher.cutscene_dialog_remove_line() == {'FINISHED'}
    assert len(scene.witcher_cutscene_dialog_lines) == 0


def assert_game_voice_picker(scene, ui_cutscene, ui_voice, import_cutscene):
    scene.witcher_cutscene_dialog_lines.clear()
    scene.witcher_cutscene_dialog_items.clear()
    line_index = ui_cutscene.add_cutscene_dialog_line(scene)
    line = scene.witcher_cutscene_dialog_lines[line_index]
    line.speaker = "GERALT"
    line.tier = 'GAME'
    line.start_frame = 42
    line.end_frame = 72
    line.game_line_id = "111"
    line.game_voice_file_name = "111"

    synthetic_nodes = [
        {
            "game": "W3", "voiceLineId": "222", "line_id": "222", "speaker": "GERALT",
            "text": "Find me in the cache", "duration": "1.5", "display_compact": "[GERALT] Find me",
            "search_blob": "222 geralt find me in the cache 1.5", "source_path": r"quests\picker.w2scene",
        },
        {
            "game": "W3", "voiceLineId": "333", "line_id": "333", "speaker": "CIRI",
            "text": "Wrong speaker", "duration": "2.0", "display_compact": "[CIRI] Wrong speaker",
            "search_blob": "333 ciri wrong speaker 2.0", "source_path": r"quests\other.w2scene",
        },
        {
            "game": "W2", "voiceLineId": "444", "line_id": "444", "speaker": "GERALT",
            "text": "Wrong game", "duration": "1.0", "display_compact": "[GERALT] Wrong game",
            "search_blob": "444 geralt wrong game 1.0", "source_path": r"quests\w2.w2scene",
        },
    ]
    real_ensure = ui_voice.ensure_voice_cache
    real_cache = ui_voice._voice_node_cache
    real_loader = ui_voice.load_voice_and_lipsync
    real_remove = import_cutscene.remove_cutscene_dialog_audio_strips
    load_calls = []
    remove_calls = []
    reports = []
    ui_voice.ensure_voice_cache = lambda _context=None: None
    ui_voice._voice_node_cache = synthetic_nodes
    def fake_load(*args, **kwargs):
        load_calls.append((args, kwargs))
        scene.frame_set(0)
        return SimpleNamespace()

    ui_voice.load_voice_and_lipsync = fake_load
    import_cutscene.remove_cutscene_dialog_audio_strips = (
        lambda *args, **kwargs: remove_calls.append((args, kwargs)) or 1
    )
    try:
        state = bpy.context.window_manager.witcher_cutscene_dialog_voice
        state.refreshing = True
        try:
            state.initialized = False
            state.line_index = -1
            state.query = "stale"
            state.speaker = "CIRI"
            stale = state.results.add()
            stale.voice_line_id = "stale"
            state.result_index = 0
        finally:
            state.refreshing = False

        voice_browser_state = (
            str(getattr(scene, "witcher_voice_search_text", "")),
            str(getattr(scene, "witcher_voice_speaker_filter", "")),
            int(getattr(scene, "witcher_voice_list_index", -1)),
            tuple(ui_voice._voice_filtered_indices),
        )
        invoke_calls = []
        fake_wm = SimpleNamespace(
            witcher_cutscene_dialog_voice=state,
            invoke_props_dialog=lambda _operator, **kwargs: invoke_calls.append(kwargs) or {'RUNNING_MODAL'},
        )
        fake_context = SimpleNamespace(scene=scene, window_manager=fake_wm)
        fake_operator = SimpleNamespace(line_index=-1, report=lambda *args: reports.append(args))
        result = ui_cutscene.WITCH_OT_CutsceneDialogPickGameVoice.invoke(fake_operator, fake_context, None)
        assert result == {'RUNNING_MODAL'} and invoke_calls == [{"width": 760, "confirm_text": "Use Voice Line"}]
        assert (state.line_index, state.query, state.speaker, state.result_index) == (line_index, "", "GERALT", -1)
        assert [row.voice_line_id for row in state.results] == ["222"]
        assert voice_browser_state == (
            str(getattr(scene, "witcher_voice_search_text", "")),
            str(getattr(scene, "witcher_voice_speaker_filter", "")),
            int(getattr(scene, "witcher_voice_list_index", -1)),
            tuple(ui_voice._voice_filtered_indices),
        )

        state.query = '"find me" -unused'
        assert [row.voice_line_id for row in state.results] == ["222"]
        before_selection = (line.game_line_id, line.game_voice_file_name, line.start_frame)
        state.result_index = 0
        assert (state.selected_line_id, state.selected_voice_line_id) == ("222", "222")
        line = scene.witcher_cutscene_dialog_lines[line_index]
        assert (line.game_line_id, line.game_voice_file_name, line.start_frame) == before_selection

        reopened = ui_cutscene.WITCH_OT_CutsceneDialogPickGameVoice.invoke(fake_operator, fake_context, None)
        assert reopened == {'RUNNING_MODAL'} and len(invoke_calls) == 2
        assert (state.query, state.speaker, state.result_index) == ('"find me" -unused', "GERALT", 0)
        assert (state.selected_line_id, state.selected_voice_line_id) == ("222", "222")

        state.cache_key = "stale-language-or-source"
        assert ui_cutscene.WITCH_OT_CutsceneDialogPickGameVoice.invoke(
            fake_operator, fake_context, None,
        ) == {'RUNNING_MODAL'}
        assert state.cache_key != "stale-language-or-source"
        assert (state.query, state.result_index, state.selected_voice_line_id) == (
            '"find me" -unused', 0, "222",
        )

        line.speaker = "CIRI"
        line.text = ""
        scene.frame_set(73)
        execute_operator = SimpleNamespace(line_index=-1, report=lambda *args: reports.append(args))
        assert ui_cutscene.WITCH_OT_CutsceneDialogPickGameVoice.execute(
            execute_operator, bpy.context,
        ) == {'FINISHED'}
        line = scene.witcher_cutscene_dialog_lines[line_index]
        assert (line.game_line_id, line.game_voice_file_name, line.speaker, line.text) == (
            "222", "222", "GERALT", "Find me in the cache",
        )
        assert (line.start_frame, line.end_frame) == (42, 87)
        assert scene.frame_current == 73
        assert [kwargs["line_id"] for _args, kwargs in remove_calls[:2]] == ["111", "222"]
        assert all(
            kwargs["source_path"] == ui_cutscene._AUTHORED_DIALOG_SOURCE_PATH
            for _args, kwargs in remove_calls[:2]
        )
        assert len(load_calls) == 1
        args, kwargs = load_calls[0]
        assert args == ("222",)
        assert kwargs["context"] is bpy.context and kwargs["at_frame"] == 42.0
        assert kwargs["nla_mode"] == "replace"
        assert kwargs["allow_context_actor"] is False
        assert kwargs["nla_track"] == ui_cutscene._authored_cutscene_dialog_nla_track("222")
        assert kwargs["strip_props"][import_cutscene.CUTSCENE_DIALOG_LINE_ID_PROP] == "222"
        assert kwargs["strip_props"][import_cutscene.CUTSCENE_DIALOG_TEXT_PROP] == "Find me in the cache"

        def failed_load(*_args, **_kwargs):
            raise RuntimeError("preview probe failed")

        ui_voice.load_voice_and_lipsync = failed_load
        state.selected_voice_line_id = state.selected_line_id = "333"
        state.selected_speaker = "CIRI"
        state.selected_text = "Selection survives preview failure"
        state.selected_duration = "2.0"
        assert ui_cutscene.WITCH_OT_CutsceneDialogPickGameVoice.execute(
            execute_operator, bpy.context,
        ) == {'FINISHED'}
        line = scene.witcher_cutscene_dialog_lines[line_index]
        # With no CIRI actor cast, use its registry voicetag.
        assert (line.game_line_id, line.speaker, line.text, line.end_frame) == (
            "333", "CIRILLA", "Selection survives preview failure", 102,
        )
        assert any("preview failed" in args[1].lower() for args in reports if len(args) > 1)

        remove_before = len(remove_calls)
        assert ui_cutscene.remove_cutscene_dialog_line(scene, context=bpy.context)
        assert len(remove_calls) == remove_before + 1
        assert remove_calls[-1][1]["line_id"] == "333"

        ui_cutscene.add_cutscene_dialog_line(scene, text="First replacement")
        second_index = ui_cutscene.add_cutscene_dialog_line(scene, text="Second replacement")
        second_before = tuple(
            getattr(scene.witcher_cutscene_dialog_lines[second_index], field)
            for field in ("speaker", "text", "tier", "game_line_id", "game_voice_file_name")
        )
        switch_operator = SimpleNamespace(line_index=second_index, report=lambda *args: reports.append(args))
        assert ui_cutscene.WITCH_OT_CutsceneDialogPickGameVoice.invoke(
            switch_operator, fake_context, None,
        ) == {'RUNNING_MODAL'}
        assert state.query == '"find me" -unused'
        assert state.result_index == -1 and not state.selected_voice_line_id
        assert ui_cutscene.WITCH_OT_CutsceneDialogPickGameVoice.execute(
            switch_operator, bpy.context,
        ) == {'CANCELLED'}
        second_after = tuple(
            getattr(scene.witcher_cutscene_dialog_lines[second_index], field)
            for field in ("speaker", "text", "tier", "game_line_id", "game_voice_file_name")
        )
        assert second_after == second_before
    finally:
        ui_voice.ensure_voice_cache = real_ensure
        ui_voice._voice_node_cache = real_cache
        ui_voice.load_voice_and_lipsync = real_loader
        import_cutscene.remove_cutscene_dialog_audio_strips = real_remove
        state.initialized = False
        scene.witcher_cutscene_dialog_lines.clear()
        scene.witcher_cutscene_dialog_items.clear()


def assert_dialogue_tooltips(ui_cutscene):
    properties = ui_cutscene.CutsceneAuthoredDialogLine.bl_rna.properties
    for name in (
        "speaker", "text", "start_frame", "end_frame", "tier", "game_line_id",
        "game_voice_file_name", "wav_path", "allocated_line_id", "lipsync_ref",
    ):
        assert properties[name].description.strip(), name
    assert "First frame after" in properties["end_frame"].description
    assert "current timeline frame" in ui_cutscene.WITCH_OT_CutsceneDialogFromPlayhead.bl_description
    for operator in (
        ui_cutscene.WITCH_OT_CutsceneDialogAddLine,
        ui_cutscene.WITCH_OT_CutsceneDialogRemoveLine,
        ui_cutscene.WITCH_OT_CutsceneDialogPickGameVoice,
        ui_cutscene.WITCH_OT_CutsceneDialogPreviewGameLine,
        ui_cutscene.WITCH_OT_CutsceneDialogGenerateWav,
        ui_cutscene.WITCH_OT_CutsceneDialogPrepareWav,
        ui_cutscene.WITCH_OT_CutsceneDialogMoveLine,
        ui_cutscene.WITCH_OT_CutsceneDialogCopyPreview,
    ):
        assert operator.bl_description.strip(), operator.__name__


def assert_dialogue_nla_preview_cleanup(scene, ui_cutscene, ui_voice):
    obj = bpy.data.objects.new("dialogue_nla_cleanup_probe", None)
    scene.collection.objects.link(obj)
    obj.animation_data_create()
    actions = []

    def add_track(name):
        action = bpy.data.actions.new(name + "_action")
        actions.append(action.name)
        track = obj.animation_data.nla_tracks.new()
        track.name = name
        track.strips.new(name, 1, action)
        return track

    target = ui_cutscene._authored_cutscene_dialog_nla_track("700")
    add_track(target)
    add_track(target + "_phoneme")
    add_track("voice_import_user_preview")
    try:
        assert ui_voice.remove_voice_lipsync_tracks(scene, target) == 2
        assert obj.animation_data.nla_tracks.get(target) is None
        assert obj.animation_data.nla_tracks.get(target + "_phoneme") is None
        assert obj.animation_data.nla_tracks.get("voice_import_user_preview") is not None
    finally:
        bpy.data.objects.remove(obj, do_unlink=True)
        for action_name in actions:
            action = bpy.data.actions.get(action_name)
            if action is not None and action.users == 0:
                bpy.data.actions.remove(action)


def assert_w3_picker_cache_isolated(scene, ui_cutscene, ui_voice):
    old_game = scene.witcher_voice_game
    real_ensure = ui_voice.ensure_voice_cache
    cache_state = (
        ui_voice._voice_node_cache,
        ui_voice._voice_cache_loaded,
        ui_voice._voice_filtered_indices,
        ui_voice._voice_cache_identity_loaded,
        ui_voice._voice_cache_source_revision_loaded,
        ui_voice._voice_speaker_counts,
        ui_voice._voice_popular_speakers_cache,
        ui_voice._voice_game_override,
    )
    try:
        scene.witcher_voice_game = 'W2'
        scene.witcher_voice_search_text = "browser search stays"
        scene.witcher_voice_speaker_filter = "TRISS"
        scene.witcher_voice_list_index = 7
        browser_state = (
            scene.witcher_voice_game,
            scene.witcher_voice_search_text,
            scene.witcher_voice_speaker_filter,
            scene.witcher_voice_list_index,
        )
        w2_nodes = [{"game": "W2", "voiceLineId": "w2-sentinel"}]
        w2_filtered = [0]
        w2_counts = {"TRISS": 1}
        w2_popular = ["TRISS"]
        w2_identity = ("W2", "en", "en")
        ui_voice._voice_node_cache = w2_nodes
        ui_voice._voice_cache_loaded = True
        ui_voice._voice_filtered_indices = w2_filtered
        ui_voice._voice_cache_identity_loaded = w2_identity
        ui_voice._voice_cache_source_revision_loaded = 91
        ui_voice._voice_speaker_counts = w2_counts
        ui_voice._voice_popular_speakers_cache = w2_popular
        requested_games = []

        def fake_ensure(context=None):
            requested_games.append(ui_voice.get_active_voice_game(context))
            ui_voice._voice_node_cache = [{
                "game": "W3",
                "voiceLineId": "555",
                "line_id": "555",
                "speaker": "GERALT",
                "text": "W3 survives a W2 browser",
                "duration": "1.0",
                "display_compact": "[GERALT] W3 survives a W2 browser",
                "search_blob": "555 geralt w3 survives a w2 browser",
                "source_path": "quests\\w3.w2scene",
            }]
            ui_voice._voice_cache_loaded = True
            ui_voice._voice_filtered_indices = [0]
            ui_voice._voice_cache_identity_loaded = ("W3", "en", "en")
            ui_voice._voice_cache_source_revision_loaded = 92
            ui_voice._voice_speaker_counts = {"GERALT": 1}
            ui_voice._voice_popular_speakers_cache = ["GERALT"]

        ui_voice.ensure_voice_cache = fake_ensure
        state = bpy.context.window_manager.witcher_cutscene_dialog_voice
        state.refreshing = True
        try:
            state.query = "survives"
            state.speaker = "GERALT"
        finally:
            state.refreshing = False
        assert ui_cutscene.refresh_cutscene_dialog_voice_results(state, bpy.context) == 1
        assert [row.voice_line_id for row in state.results] == ["555"]
        assert requested_games == ["W3"]
        assert browser_state == (
            scene.witcher_voice_game,
            scene.witcher_voice_search_text,
            scene.witcher_voice_speaker_filter,
            scene.witcher_voice_list_index,
        )
        assert ui_voice._voice_node_cache is w2_nodes
        assert ui_voice._voice_filtered_indices is w2_filtered
        assert ui_voice._voice_cache_identity_loaded == w2_identity
        assert ui_voice._voice_cache_source_revision_loaded == 91
        assert ui_voice._voice_speaker_counts is w2_counts
        assert ui_voice._voice_popular_speakers_cache is w2_popular
        assert ui_voice._voice_game_override is None
    finally:
        ui_voice.ensure_voice_cache = real_ensure
        scene.witcher_voice_game = old_game
        (
            ui_voice._voice_node_cache,
            ui_voice._voice_cache_loaded,
            ui_voice._voice_filtered_indices,
            ui_voice._voice_cache_identity_loaded,
            ui_voice._voice_cache_source_revision_loaded,
            ui_voice._voice_speaker_counts,
            ui_voice._voice_popular_speakers_cache,
            ui_voice._voice_game_override,
        ) = cache_state


def assert_linked_scene_dialog_fallback(import_cutscene, dc_scene):
    real_load = import_cutscene.loadCutsceneFile
    real_resolve = import_cutscene._resolve_cutscene_linked_scene_file
    real_context = import_cutscene.redkit_repo_context
    real_get_lines = dc_scene.get_cutscene_dialog_lines
    calls = []
    import_cutscene.loadCutsceneFile = lambda _path: SimpleNamespace(
        usedInFiles=["missing.w2scene", "authored.w2scene", "later.w2scene"]
    )
    import_cutscene._resolve_cutscene_linked_scene_file = lambda depot_path, _cutscene: depot_path
    import_cutscene.redkit_repo_context = lambda _path: nullcontext()

    def fake_get_lines(scene_path, _cutscene_path):
        calls.append(scene_path)
        if scene_path == "missing.w2scene":
            return []
        return [{
            "actor": "GERALT",
            "voice_file": "123",
            "sound_event": "",
            "line_id": "123",
            "line_index": 123,
            "line_text": "Authored wins",
            "approved_duration": 1.0,
        }]

    dc_scene.get_cutscene_dialog_lines = fake_get_lines
    try:
        lines = import_cutscene.load_cutscene_dialog_items("cutscene.w2cutscene")
    finally:
        import_cutscene.loadCutsceneFile = real_load
        import_cutscene._resolve_cutscene_linked_scene_file = real_resolve
        import_cutscene.redkit_repo_context = real_context
        dc_scene.get_cutscene_dialog_lines = real_get_lines
    assert calls == ["missing.w2scene", "authored.w2scene"], calls
    assert len(lines) == 1 and lines[0]["line_text"] == "Authored wins"
    assert lines[0]["scene_path"] == "authored.w2scene"


def assert_project_id_copy_back(scene, ui_cutscene, redkit_project):
    scene_path = r"dlc\modtest\data\scenes\copy_back.w2scene"
    scene.witcher_cutscene_dialog_lines.clear()
    scene.witcher_cutscene_dialog_items.clear()
    for line_id, text in (("12001", "Project subtitle"), ("12002", "Project WAV")):
        item = scene.witcher_cutscene_dialog_items.add()
        item.actor, item.line_text, item.line_id = "GERALT", text, line_id
        item.scene_path, item.start_frame, item.end_frame = scene_path, 10, 40

    project_lines = [
        SimpleNamespace(
            line_id="12001",
            resource=f'CStoryScene "{scene_path}"',
            assets=SimpleNamespace(has_wav=False, has_wem=False, has_re=False, wav_path=None),
        ),
        SimpleNamespace(
            line_id="12002",
            resource=f'CStoryScene "{scene_path}"',
            assets=SimpleNamespace(
                has_wav=True, has_wem=False, has_re=True, wav_path=Path("project_voice.wav"),
            ),
        ),
    ]
    real_project_path = redkit_project.get_active_project_path
    real_read_lines = redkit_project.read_project_voice_lines
    redkit_project.get_active_project_path = lambda _context: Path("project")
    redkit_project.read_project_voice_lines = lambda *_args, **_kwargs: project_lines
    try:
        assert ui_cutscene.copy_cutscene_preview_to_authored(scene) == 2
    finally:
        redkit_project.get_active_project_path = real_project_path
        redkit_project.read_project_voice_lines = real_read_lines
    subtitle, wav = scene.witcher_cutscene_dialog_lines
    assert (subtitle.tier, subtitle.allocated_line_id) == ('SUBTITLE', "12001")
    assert (wav.tier, wav.allocated_line_id, wav.lipsync_ref) == ('WAV', "12002", "12002")
    assert Path(wav.wav_path).name == "project_voice.wav"
    scene.witcher_cutscene_dialog_lines.clear()
    scene.witcher_cutscene_dialog_items.clear()


def assert_export_payload_and_validation(scene, export_cutscene, cutscene_validate, redkit_project):
    scene.witcher_cutscene_dialog_lines.clear()
    scene.witcher_cutscene_event_items.clear()
    scene.render.fps = 30
    scene.render.fps_base = 1.0
    scene.frame_start = 1
    scene.frame_end = 100

    for speaker, text, tier, line_id, start, end in (
        ("GERALT", "First export line", 'SUBTITLE', "200001", 12, 42),
        ("CIRI", "Second export line", 'GAME', "1002", 60, 90),
    ):
        index = len(scene.witcher_cutscene_dialog_lines)
        scene.witcher_cutscene_dialog_lines.add()
        line = scene.witcher_cutscene_dialog_lines[index]
        line.speaker, line.text, line.tier = speaker, text, tier
        line.start_frame, line.end_frame = start, end
        if tier == 'GAME':
            line.game_line_id = line_id
            line.game_voice_file_name = line_id
        else:
            line.allocated_line_id = line_id

    stale = scene.witcher_cutscene_event_items.add()
    stale.event_type, stale.event_scope = export_cutscene.CUTSCENE_DIALOG_EVENT_TYPE, "ROOT"
    stale.start_time, stale.animation_name = 9.0, "stale"
    stale_entry = scene.witcher_cutscene_event_items.add()
    stale_entry.event_type, stale_entry.event_scope = export_cutscene.CUTSCENE_DIALOG_EVENT_TYPE, "ENTRY"
    stale_entry.start_time, stale_entry.animation_name = 8.0, "stale_entry"
    fade = scene.witcher_cutscene_event_items.add()
    fade.event_type, fade.event_scope = "CExtAnimCutsceneFadeEvent", "ROOT"
    fade.start_time = 0.25

    wrapper_lines, dialog_events = export_cutscene._collect_authored_cutscene_dialogue(scene)
    assert [line["voicetag"] for line in wrapper_lines] == ["GERALT", "CIRI"]
    assert [line["string_id"] for line in wrapper_lines] == [200001, 1002]
    assert [line["approved_duration"] for line in wrapper_lines] == [1.0, 1.0]
    assert [line["voice_file_name"] for line in wrapper_lines] == ["", "1002"]
    assert [event["start_time"] for event in dialog_events] == [0.4, 2.0]
    assert all(event["event_type"] == export_cutscene.CUTSCENE_DIALOG_EVENT_TYPE for event in dialog_events)
    assert all(event["animation_name"] == export_cutscene.CUTSCENE_ROOT_ANIMATION_NAME for event in dialog_events)

    root_events, entry_events = export_cutscene._collect_scene_cutscene_events(scene)
    assert not entry_events
    assert [event["event_type"] for event in root_events] == [
        "CExtAnimCutsceneFadeEvent",
        export_cutscene.CUTSCENE_DIALOG_EVENT_TYPE,
        export_cutscene.CUTSCENE_DIALOG_EVENT_TYPE,
    ]
    assert [event["start_time"] for event in root_events[1:]] == [0.4, 2.0]

    scene.witcher_cutscene_dialog_lines.clear()
    cases = (
        ("", "", 'SUBTITLE', "200001", 10, 15),
        ("CIRI", "Outside", 'SUBTITLE', "", 0, 101),
        ("GERALT", "Overlap one", 'SUBTITLE', "200002", 20, 50),
        ("geralt", "Overlap two", 'GAME', "1002", 40, 60),
        ("CIRI", "Broken game line", 'GAME', "not-a-number", 70, 70),
    )
    for speaker, text, tier, line_id, start, end in cases:
        index = len(scene.witcher_cutscene_dialog_lines)
        scene.witcher_cutscene_dialog_lines.add()
        line = scene.witcher_cutscene_dialog_lines[index]
        line.speaker, line.text, line.tier = speaker, text, tier
        line.start_frame, line.end_frame = start, end
        if tier == 'GAME':
            line.game_line_id = line_id
        else:
            line.allocated_line_id = line_id

    real_project_path = redkit_project.get_active_project_path
    original_id_space = scene.witcher_cutscene_dialog_id_space
    scene.witcher_cutscene_dialog_id_space = -1
    redkit_project.get_active_project_path = lambda _context: None
    try:
        dialogue_issues = cutscene_validate._validate_authored_cutscene_dialogue(bpy.context)
        errors = [i["message"] for i in dialogue_issues if i["severity"] == "ERROR"]
        warnings = [i["message"] for i in dialogue_issues if i["severity"] != "ERROR"]
        assert all(i["tab"] == 'DIALOGS' for i in dialogue_issues), dialogue_issues
        assert {i["line"] for i in dialogue_issues if i["message"].startswith("Dialogue line 5 ")} == {4}, dialogue_issues
        _lines, full_errors, full_warnings = cutscene_validate.validate_cutscene(bpy.context)
    finally:
        redkit_project.get_active_project_path = real_project_path
        scene.witcher_cutscene_dialog_id_space = original_id_space
    assert "Dialogue line 1 has no speaker." in errors
    assert "Dialogue line 1 has no text." in errors
    assert "Dialogue line 5 must end after it starts." in errors
    assert "Dialogue line 5 has no valid numeric Game Line ID." in errors
    assert "Dialogue line 2 frames 0-101 are outside the cutscene range 1-100." in warnings
    assert "Dialogue lines 3 and 4 overlap for speaker 'geralt'." in warnings
    assert (
        "Dialogue line 2 has no allocated ID and no REDkit project or dialogue ID space is configured."
        in warnings
    )
    assert set(errors) <= set(full_errors)
    assert set(warnings) <= set(full_warnings)
    scene.witcher_cutscene_dialog_lines.clear()
    scene.witcher_cutscene_event_items.clear()


def assert_wrapper_dialogue_preflight(
    scene, ui_cutscene, ui_animated_component, export_cutscene, scene_builder,
):
    old_repo_path = scene.witcher_cutscene_export_repo_path
    real_resolve_dir = ui_animated_component._resolve_export_dir
    real_safe_path = ui_animated_component._safe_repo_output_path
    real_prepare = export_cutscene.prepare_authored_cutscene_dialogue_strings
    real_save = scene_builder.save_cutscene_wrapper_scene
    calls = []
    reports = []
    scene.witcher_cutscene_dialog_lines.clear()
    line = scene.witcher_cutscene_dialog_lines.add()
    line.speaker = "GERALT"
    line.text = "Invalid wrapper line"
    line.tier = 'GAME'
    line.game_line_id = "not-a-number"
    line.start_frame = line.end_frame = 20
    scene.witcher_cutscene_export_repo_path = (
        "dlc\\modtest\\data\\cutscenes\\invalid_wrapper.w2cutscene"
    )
    try:
        work_root = REPO_ROOT / "WORKING_TEMP"
        work_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="cutscene_preflight_", dir=str(work_root)) as temp_dir:
            out_path = Path(temp_dir) / "invalid_wrapper.w2scene"
            ui_animated_component._resolve_export_dir = lambda _context: temp_dir
            ui_animated_component._safe_repo_output_path = lambda *_args, **_kwargs: str(out_path)
            export_cutscene.prepare_authored_cutscene_dialogue_strings = (
                lambda *_args, **_kwargs: calls.append("prepare")
            )
            scene_builder.save_cutscene_wrapper_scene = (
                lambda *_args, **_kwargs: calls.append("write")
            )
            operator = SimpleNamespace(report=lambda level, message: reports.append((level, message)))
            execute = ui_cutscene.WITCH_OT_CutsceneExportSceneWrapper.execute.__get__(operator)
            assert execute(bpy.context) == {'CANCELLED'}
            assert not calls and not out_path.exists()
            assert reports and reports[-1][0] == {'ERROR'}
            assert "Dialogue invalid" in reports[-1][1]
            assert "Game Line ID" in reports[-1][1]
    finally:
        ui_animated_component._resolve_export_dir = real_resolve_dir
        ui_animated_component._safe_repo_output_path = real_safe_path
        export_cutscene.prepare_authored_cutscene_dialogue_strings = real_prepare
        scene_builder.save_cutscene_wrapper_scene = real_save
        scene.witcher_cutscene_export_repo_path = old_repo_path
        scene.witcher_cutscene_dialog_lines.clear()


def assert_string_id_preparation(scene, export_cutscene, redkit_project):
    assert export_cutscene._cutscene_dialog_id_space_bounds(0) == (0, 2110000000, 2110000999)
    temp_root = REPO_ROOT / "WORKING_TEMP"
    temp_root.mkdir(parents=True, exist_ok=True)
    real_project_path = redkit_project.get_active_project_path
    scene.witcher_cutscene_dialog_lines.clear()
    scene.witcher_cutscene_dialog_id_space = 9999
    try:
        with tempfile.TemporaryDirectory(prefix="cutscene_strings_", dir=str(temp_root)) as temp_dir:
            temp_dir = Path(temp_dir)
            wrapper_path = temp_dir / "cs_strings.w2scene"
            for speaker, text, tier in (
                ("GERALT", "Fallback first.", 'SUBTITLE'),
                ("CIRI", "Fallback second.", 'WAV'),
                ("DANDELION", "Game string stays external.", 'GAME'),
            ):
                index = len(scene.witcher_cutscene_dialog_lines)
                scene.witcher_cutscene_dialog_lines.add()
                line = scene.witcher_cutscene_dialog_lines[index]
                line.speaker, line.text, line.tier = speaker, text, tier
                line.start_frame, line.end_frame = 10 + index * 20, 25 + index * 20
                if tier == 'GAME':
                    line.game_line_id = "1002"

            redkit_project.get_active_project_path = lambda _context: None
            result = export_cutscene.prepare_authored_cutscene_dialogue_strings(
                bpy.context,
                wrapper_path,
                scene_repo="dlc\\modtest\\data\\scenes\\cs_strings.w2scene",
            )
            assert result["mode"] == "csv" and result["allocated_count"] == 2, result
            assert result["id_space"] == 9999
            assert [line.allocated_line_id for line in scene.witcher_cutscene_dialog_lines] == [
                "2119999000", "2119999001", "",
            ]
            csv_path = Path(result["path"])
            assert csv_path == wrapper_path.with_suffix(".strings.csv") and csv_path.is_file()
            first_bytes = csv_path.read_bytes()
            assert not first_bytes.startswith(b"\xef\xbb\xbf")
            assert first_bytes == (
                b";meta[language=en]\n"
                b"; id      |key(hex)|key(str)| text\n"
                b";\n"
                b"2119999000|||Fallback first.\n"
                b"2119999001|||Fallback second.\n"
            )

            repeated = export_cutscene.prepare_authored_cutscene_dialogue_strings(
                bpy.context,
                wrapper_path,
                scene_repo="dlc\\modtest\\data\\scenes\\cs_strings.w2scene",
            )
            assert repeated["allocated_count"] == 0
            assert csv_path.read_bytes() == first_bytes

            w3strings = Path(r"E:\w3.modding\radish-tools\w3strings.exe")
            if w3strings.is_file():
                encoded = subprocess.run(
                    [str(w3strings), "--encode", str(csv_path), "--id-space", "9999"],
                    cwd=str(temp_dir),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
                assert encoded.returncode == 0, (encoded.stdout, encoded.stderr)

            scene.witcher_cutscene_dialog_lines[0].text = "Radish | delimiter"
            try:
                export_cutscene.prepare_authored_cutscene_dialogue_strings(
                    bpy.context,
                    wrapper_path,
                    scene_repo="dlc\\modtest\\data\\scenes\\cs_strings.w2scene",
                )
                raise AssertionError("Radish delimiter must be rejected")
            except ValueError as exc:
                assert "cannot encode" in str(exc), exc

        scene.witcher_cutscene_dialog_lines.clear()
        with tempfile.TemporaryDirectory(prefix="cutscene_redkit_", dir=str(temp_root)) as temp_dir:
            project_path = Path(temp_dir)
            project_base = 2_115_555_000
            (project_path / "test.w3edit").write_text(
                '{"idSpace": 2115555000}', encoding="utf-8",
            )
            project_csv = project_path / redkit_project.PROJECT_STRINGS_CSV
            project_csv.write_text(
                ";".join(redkit_project.PROJECT_STRING_COLUMNS) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            line_index = len(scene.witcher_cutscene_dialog_lines)
            scene.witcher_cutscene_dialog_lines.add()
            line = scene.witcher_cutscene_dialog_lines[line_index]
            line.speaker = "GERALT"
            line.text = "REDkit first."
            line.tier = 'SUBTITLE'
            redkit_project.get_active_project_path = lambda _context: project_path
            scene_repo = "dlc\\modtest\\data\\scenes\\cs_redkit.w2scene"
            result = export_cutscene.prepare_authored_cutscene_dialogue_strings(
                bpy.context, project_path / "cs_redkit.w2scene", scene_repo=scene_repo,
            )
            assert result["mode"] == "redkit" and result["allocated_count"] == 1, result
            assert line.allocated_line_id == str(project_base + 1)
            project_line = redkit_project.find_project_voice_line(
                project_path, line.allocated_line_id, language="en", include_unvoiced=True,
            )
            assert project_line is not None
            assert project_line.text == "REDkit first." and project_line.speaker == "GERALT"
            assert project_line.resource == f'CStoryScene "{scene_repo}"'
            first_bytes = project_csv.read_bytes()
            repeated = export_cutscene.prepare_authored_cutscene_dialogue_strings(
                bpy.context, project_path / "cs_redkit.w2scene", scene_repo=scene_repo,
            )
            assert repeated["allocated_count"] == 0 and project_csv.read_bytes() == first_bytes

            line = scene.witcher_cutscene_dialog_lines[line_index]
            line.speaker = "CIRI"
            line.text = "REDkit edited."
            export_cutscene.prepare_authored_cutscene_dialogue_strings(
                bpy.context, project_path / "cs_redkit.w2scene", scene_repo=scene_repo,
            )
            project_line = redkit_project.find_project_voice_line(
                project_path, line.allocated_line_id, language="en", include_unvoiced=True,
            )
            assert project_line.text == "REDkit edited." and project_line.speaker == "CIRI"

            foreign_id = str(project_base + 99)
            redkit_project.add_project_line(
                project_path,
                foreign_id,
                "Foreign owner.",
                "GERALT",
                language="en",
                resource='CStoryScene "quests\\foreign.w2scene"',
                property_name="Line text",
                key=foreign_id,
            )
            conflict_index = len(scene.witcher_cutscene_dialog_lines)
            conflict = scene.witcher_cutscene_dialog_lines.add()
            conflict.speaker = "GERALT"
            conflict.text = "Must fail before the first row changes."
            conflict.tier = 'SUBTITLE'
            conflict.allocated_line_id = foreign_id
            line.text = "This update must not reach the project CSV."
            before_conflict = project_csv.read_bytes()
            try:
                export_cutscene.prepare_authored_cutscene_dialogue_strings(
                    bpy.context, project_path / "cs_redkit.w2scene", scene_repo=scene_repo,
                )
                raise AssertionError("Foreign REDkit ID ownership was accepted")
            except ValueError as exc:
                assert "already belongs" in str(exc), exc
            assert project_csv.read_bytes() == before_conflict
            unchanged = redkit_project.find_project_voice_line(
                project_path, line.allocated_line_id, language="en", include_unvoiced=True,
            )
            assert unchanged.text == "REDkit edited."
            scene.witcher_cutscene_dialog_lines.remove(conflict_index)
    finally:
        redkit_project.get_active_project_path = real_project_path
        scene.witcher_cutscene_dialog_lines.clear()


def assert_line_id_status(scene, export_cutscene, cutscene_validate, redkit_project):
    temp_root = REPO_ROOT / "WORKING_TEMP"
    temp_root.mkdir(parents=True, exist_ok=True)
    real_project_path = redkit_project.get_active_project_path
    original_id_space = scene.witcher_cutscene_dialog_id_space
    original_repo = scene.witcher_cutscene_export_repo_path
    scene.witcher_cutscene_dialog_lines.clear()

    def add_line(raw_id):
        index = len(scene.witcher_cutscene_dialog_lines)
        scene.witcher_cutscene_dialog_lines.add()
        line = scene.witcher_cutscene_dialog_lines[index]
        line.speaker, line.text, line.tier = "GERALT", f"Line {index + 1}", 'SUBTITLE'
        line.start_frame, line.end_frame = 1 + index * 10, 5 + index * 10
        line.allocated_line_id = raw_id
        return index

    def status(index):
        return export_cutscene.authored_dialog_line_id_status(bpy.context, index)

    try:
        with tempfile.TemporaryDirectory(prefix="cutscene_idstatus_", dir=str(temp_root)) as temp_dir:
            project_path = Path(temp_dir)
            name = project_path.name
            (project_path / "test.w3edit").write_text('{"idSpace": 10000000}', encoding="utf-8")
            csv_path = project_path / redkit_project.PROJECT_STRINGS_CSV
            csv_path.write_text(
                ";".join(redkit_project.PROJECT_STRING_COLUMNS) + "\n", encoding="utf-8", newline="\n",
            )
            scene.witcher_cutscene_export_repo_path = "dlc\\modtest\\data\\cutscenes\\cs_status.w2cutscene"
            own_resource = export_cutscene._companion_scene_resource(scene)
            assert own_resource == 'CStoryScene "dlc\\modtest\\data\\scenes\\cs_status.w2scene"', own_resource
            redkit_project.add_project_line(
                project_path, "10000005", "Foreign.", "GERALT", resource='CStoryScene "quests\\foreign.w2scene"',
            )
            redkit_project.add_project_line(project_path, "10000006", "Mine.", "CIRI", resource=own_resource)
            redkit_project.get_active_project_path = lambda _context: project_path

            info = redkit_project.next_project_line_id(project_path)
            assert (info.id_space, info.used_count, info.next_line_id) == (10000000, 2, 10000007), info
            assert info.metadata_path.name == "test.w3edit" and info.csv_mtime > 0, info
            owners = redkit_project.read_project_string_owners(project_path)
            assert owners == {10000005: 'CStoryScene "quests\\foreign.w2scene"', 10000006: own_resource}, owners
            assert redkit_project.read_project_string_owners(project_path) is owners, "unchanged CSV must hit the cache"
            redkit_project.add_project_line(project_path, "10000007", "Later.", "CIRI", resource=own_resource)
            assert 10000007 in redkit_project.read_project_string_owners(project_path), "CSV write must refresh the cache"
            assert redkit_project.next_project_line_id(project_path).next_line_id == 10000008

            expected = (
                ("", 'INFO', "allocated on export (next 10000008)"),
                ("9999999", 'ERROR', f"outside {name} idSpace (from 10000000)"),
                ("10000005", 'ERROR', 'taken by CStoryScene "quests\\foreign.w2scene"'),
                ("10000006", 'OK', f"already in {name}"),
                ("10000050", 'OK', f"free in {name}"),
                ("abc", 'ERROR', "must be numeric"),
            )
            for raw_id, _state, _text in expected:
                add_line(raw_id)
            for index, (_raw_id, state, text) in enumerate(expected):
                assert status(index) == (state, text), (index, status(index))
            dup = add_line("10000050")
            assert status(4) == ('ERROR', "also used by line 7") and status(dup) == ('ERROR', "also used by line 5")

            issues = cutscene_validate._validate_authored_cutscene_dialogue(bpy.context)
            errors = [i["message"] for i in issues if i["severity"] == "ERROR"]
            assert f"Dialogue line 2 ID 9999999 outside {name} idSpace (from 10000000)." in errors, errors
            assert 'Dialogue line 3 ID 10000005 taken by CStoryScene "quests\\foreign.w2scene".' in errors, errors
            assert "Dialogue line 5 ID 10000050 also used by line 7." in errors, errors
            assert "Dialogue line 6 ID abc must be numeric." in errors, errors
            assert not any(m.startswith(("Dialogue line 1 ID", "Dialogue line 4 ID")) for m in errors), errors

        scene.witcher_cutscene_dialog_lines.clear()
        redkit_project.get_active_project_path = lambda _context: None
        scene.witcher_cutscene_dialog_id_space = 9999
        for raw_id in ("", "2119999500", "2110000000"):
            add_line(raw_id)
        assert status(0) == ('INFO', "allocated on export (from 2119999000)"), status(0)
        assert status(1) == ('OK', "in fallback space 9999"), status(1)
        assert status(2) == ('ERROR', "outside fallback space 9999 (2119999000-2119999999)"), status(2)
        scene.witcher_cutscene_dialog_id_space = -1
        assert status(1) == ('INFO', "no REDkit project or fallback ID space to check"), status(1)
        assert status(0) == ('INFO', "no REDkit project or fallback ID space"), status(0)
    finally:
        redkit_project.get_active_project_path = real_project_path
        scene.witcher_cutscene_dialog_id_space = original_id_space
        scene.witcher_cutscene_export_repo_path = original_repo
        scene.witcher_cutscene_dialog_lines.clear()


def assert_wav_lipsync_handoff(scene, ui_cutscene, ui_lipsync, redkit_project):
    temp_root = REPO_ROOT / "WORKING_TEMP"
    temp_root.mkdir(parents=True, exist_ok=True)
    real_project_path = redkit_project.get_active_project_path
    old_replace_audio = scene.witcher_lipsync_replace_audio
    scene.witcher_cutscene_dialog_lines.clear()
    scene.witcher_cutscene_dialog_items.clear()
    scene.witcher_lipsync_lines.clear()
    scene.witcher_lipsync_line_index = -1
    scene.witcher_cutscene_dialog_id_space = 9999
    redkit_project.get_active_project_path = lambda _context: None
    try:
        with tempfile.TemporaryDirectory(prefix="cutscene_wav_", dir=str(temp_root)) as temp_dir:
            wav_path = Path(temp_dir) / "custom_line.wav"
            with wave.open(str(wav_path), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(8000)
                wav_file.writeframes(b"\0\0" * 800)

            index = ui_cutscene.add_cutscene_dialog_line(scene, text="Custom voiced line.")
            line = scene.witcher_cutscene_dialog_lines[index]
            line.speaker = "GERALT"
            line.tier = 'WAV'
            line.start_frame = 37
            line.end_frame = 67
            line.wav_path = str(wav_path)
            editor_line, soundstrip = ui_cutscene.prepare_cutscene_dialog_wav_line(
                bpy.context, line_index=index,
            )
            line = scene.witcher_cutscene_dialog_lines[index]
            assert line.allocated_line_id == line.lipsync_ref == "2119999000"
            assert len(scene.witcher_lipsync_lines) == 1
            assert (editor_line.line_id, editor_line.text, editor_line.speaker) == (
                "2119999000", "Custom voiced line.", "GERALT",
            )
            assert Path(editor_line.wav_path) == wav_path
            assert soundstrip is not None and soundstrip.frame_start == 37
            assert soundstrip["witcher_cutscene_dialog_audio"]
            assert soundstrip["witcher_cutscene_dialog_line_id"] == "2119999000"
            assert soundstrip["witcher_cutscene_dialog_source_path"] == "witcher_cutscene_authored_dialogue"
            assert ui_lipsync._resolve_start_frame(scene) == 37.0

            line.text = "Custom voiced line edited."
            assert bpy.ops.witcher.cutscene_dialog_prepare_wav(line_index=index) == {'FINISHED'}
            editor_line = ui_lipsync._find_editor_line_by_id(scene, "2119999000")
            assert len(scene.witcher_lipsync_lines) == 1
            assert editor_line.text == "Custom voiced line edited."
            tagged = [
                strip for strip in list(ui_lipsync._get_sequence_editor_strips(scene) or [])
                if bool(strip.get("witcher_cutscene_dialog_audio", False))
                and strip.get("witcher_cutscene_dialog_source_path", "") == "witcher_cutscene_authored_dialogue"
            ]
            assert len(tagged) == 1, tagged
            repeated_strip = tagged[0]

            result_id = "2119999007"
            job = SimpleNamespace(
                line_id=result_id,
                text=editor_line.text,
                speaker=editor_line.speaker,
                language=editor_line.language,
            )
            ui_lipsync._update_editor_line_from_result(
                bpy.context,
                editor_line,
                job,
                str(wav_path),
                repeated_strip,
                audio_source="wav_lipsync",
            )
            line = scene.witcher_cutscene_dialog_lines[index]
            assert line.allocated_line_id == line.lipsync_ref == result_id
            assert line.wav_path == str(wav_path)
            assert repeated_strip["witcher_cutscene_dialog_line_id"] == result_id
            assert repeated_strip.frame_start == 37

            scene.witcher_cutscene_dialog_line_index = index
            assert ui_cutscene.remove_cutscene_dialog_line(scene, context=bpy.context)
            remaining = [
                strip for strip in list(ui_lipsync._get_sequence_editor_strips(scene) or [])
                if bool(strip.get("witcher_cutscene_dialog_audio", False))
                and strip.get("witcher_cutscene_dialog_source_path", "") == "witcher_cutscene_authored_dialogue"
            ]
            assert not remaining, remaining
    finally:
        redkit_project.get_active_project_path = real_project_path
        scene.witcher_lipsync_replace_audio = old_replace_audio
        scene.witcher_cutscene_dialog_lines.clear()
        scene.witcher_cutscene_dialog_items.clear()
        scene.witcher_lipsync_lines.clear()
        scene.witcher_lipsync_line_index = -1


def assert_speech_connect(scene, ui_cutscene, ui_lipsync, export_cutscene, redkit_project):
    temp_root = REPO_ROOT / "WORKING_TEMP"
    temp_root.mkdir(parents=True, exist_ok=True)
    real_project_path = redkit_project.get_active_project_path
    original_id_space = scene.witcher_cutscene_dialog_id_space
    original_repo = scene.witcher_cutscene_export_repo_path
    old_replace_audio = scene.witcher_lipsync_replace_audio
    scene.witcher_cutscene_dialog_lines.clear()
    scene.witcher_cutscene_dialog_items.clear()
    scene.witcher_lipsync_lines.clear()
    scene.witcher_lipsync_line_index = -1
    scene.witcher_cutscene_actor_items.clear()
    actor = scene.witcher_cutscene_actor_items.add()
    actor.actor_name = actor.voice_tag = "CIRILLA"
    actor.actor_type = "CAT_Actor"
    scene.render.fps = 30
    scene.render.fps_base = 1.0

    def tagged_strips(line_id):
        return [
            strip for strip in list(ui_lipsync._get_sequence_editor_strips(scene) or [])
            if str(strip.get("witcher_cutscene_dialog_line_id", "") or "") == line_id
            and strip.get("witcher_cutscene_dialog_source_path", "") == "witcher_cutscene_authored_dialogue"
        ]

    try:
        with tempfile.TemporaryDirectory(prefix="cutscene_speech_", dir=str(temp_root)) as temp_dir:
            project_path = Path(temp_dir)
            (project_path / "test.w3edit").write_text('{"idSpace": 10000000}', encoding="utf-8")
            csv_path = project_path / redkit_project.PROJECT_STRINGS_CSV
            csv_path.write_text(
                ";".join(redkit_project.PROJECT_STRING_COLUMNS) + "\n", encoding="utf-8", newline="\n",
            )
            scene.witcher_cutscene_export_repo_path = "dlc\\modtest\\data\\cutscenes\\cs_speech.w2cutscene"
            own_resource = export_cutscene._companion_scene_resource(scene)
            foreign_resource = 'CStoryScene "quests\\foreign.w2scene"'
            redkit_project.add_project_line(project_path, "10000010", "Foreign voiced line.", "CIRI", resource=foreign_resource)
            redkit_project.get_active_project_path = lambda _context: project_path

            _project_lines, added = ui_lipsync.load_project_lines_into_editor(bpy.context, clear_existing=False)
            assert added == 1 and scene.witcher_lipsync_lines[0].line_id == "10000010", added
            assert ui_lipsync.load_project_lines_into_editor(bpy.context, clear_existing=False)[1] == 0, "append must dedupe"
            wav_path = project_path / "speech_line.wav"
            with wave.open(str(wav_path), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(8000)
                wav_file.writeframes(b"\0\0" * 4000)
            ui_lipsync._add_editor_line(
                bpy.context, line_id="10000020", text="Speech first.", speaker="CIRI", wav_path=str(wav_path),
            )
            assert ui_lipsync._find_editor_line_by_id(scene, "10000020").duration == "0.5"

            scene.frame_current = 100
            for line in scene.witcher_lipsync_lines:
                line.selected = True
            assert bpy.ops.witcher.add_lipsync_lines_to_cutscene() == {'FINISHED'}
            lines = scene.witcher_cutscene_dialog_lines
            assert len(lines) == 2, len(lines)
            first, second = lines[0], lines[1]
            assert (first.tier, first.lipsync_ref, first.allocated_line_id, first.text) == (
                'WAV', "10000010", "10000010", "Foreign voiced line.",
            )
            assert first.speaker == "CIRILLA", first.speaker
            first_frames = ui_cutscene._dialog_default_duration_frames("Foreign voiced line.", 30.0)
            assert (first.start_frame, first.end_frame) == (100, 100 + first_frames), (first.start_frame, first.end_frame)
            assert (second.lipsync_ref, second.start_frame, second.end_frame) == ("10000020", first.end_frame, first.end_frame + 15)
            assert Path(second.wav_path) == wav_path, second.wav_path
            assert not any(line.selected for line in scene.witcher_lipsync_lines)
            strips = tagged_strips("10000020")
            assert len(strips) == 1 and strips[0].frame_start == second.start_frame, strips
            assert ui_cutscene._cutscene_dialog_lipsync_refs(scene) == {"10000010", "10000020"}

            scene.witcher_cutscene_dialog_lines[1].text = "Edited in cutscene."
            assert ui_lipsync._find_editor_line_by_id(scene, "10000020").text == "Edited in cutscene."
            assert ui_lipsync._find_editor_line_by_id(scene, "10000020").speaker == "CIRI", "alias speaker must not be overwritten"
            ui_lipsync._set_active_editor_line(scene, ui_lipsync._find_editor_line_by_id(scene, "10000020"))
            scene.witcher_lipsync_text = "Edited in speech."
            assert scene.witcher_cutscene_dialog_lines[1].text == "Edited in speech."

            # Linked foreign-owned lines are references, not project edits.
            assert export_cutscene.authored_dialog_line_id_status(bpy.context, 0) == ('OK', f"references {foreign_resource}")
            result = export_cutscene.prepare_authored_cutscene_dialogue_strings(
                bpy.context, project_path / "cs_speech.w2scene", scene_repo="dlc\\modtest\\data\\scenes\\cs_speech.w2scene",
            )
            assert result["mode"] == "redkit" and result["allocated_count"] == 0, result
            foreign = redkit_project.find_project_voice_line(project_path, "10000010", include_unvoiced=True)
            assert (foreign.text, foreign.speaker, foreign.resource) == ("Foreign voiced line.", "CIRI", foreign_resource)
            mine = redkit_project.find_project_voice_line(project_path, "10000020", include_unvoiced=True)
            assert (mine.text, mine.resource) == ("Edited in speech.", own_resource), (mine.text, mine.resource)

            assert bpy.ops.witcher.cutscene_dialog_unlink_speech(line_index=0) == {'FINISHED'}
            assert scene.witcher_cutscene_dialog_lines[0].lipsync_ref == ""
            state, text = export_cutscene.authored_dialog_line_id_status(bpy.context, 0)
            assert state == 'ERROR' and text.startswith("taken by"), (state, text)

            ui_lipsync._set_active_editor_line(scene, ui_lipsync._find_editor_line_by_id(scene, "10000010"))
            assert bpy.ops.witcher.cutscene_dialog_add_from_speech() == {'FINISHED'}
            assert len(scene.witcher_cutscene_dialog_lines) == 3
            assert scene.witcher_cutscene_dialog_lines[2].lipsync_ref == "10000010"

            index = ui_cutscene.add_cutscene_dialog_line(scene, text="Made in cutscene.")
            scene.witcher_cutscene_dialog_lines[index].speaker = "CIRILLA"
            assert bpy.ops.witcher.cutscene_dialog_send_to_speech(line_index=index) == {'FINISHED'}
            made = scene.witcher_cutscene_dialog_lines[index]
            assert made.tier == 'WAV' and made.lipsync_ref == made.allocated_line_id == "10000021", (made.lipsync_ref, made.allocated_line_id)
            assert ui_lipsync._find_editor_line_by_id(scene, "10000021").text == "Made in cutscene."
            assert bpy.ops.witcher.cutscene_dialog_open_speech(line_index=index) == {'FINISHED'}
            assert ui_lipsync._active_editor_line(scene).line_id == "10000021"
            if hasattr(scene, "witcher_anim_tab"):
                assert scene.witcher_anim_tab == 'SPEECH'
    finally:
        redkit_project.get_active_project_path = real_project_path
        scene.witcher_cutscene_dialog_id_space = original_id_space
        scene.witcher_cutscene_export_repo_path = original_repo
        scene.witcher_lipsync_replace_audio = old_replace_audio
        for line_id in ("10000020", "10000021"):
            ui_cutscene._remove_authored_cutscene_dialog_preview(scene, line_id)
        scene.witcher_cutscene_dialog_lines.clear()
        scene.witcher_cutscene_dialog_items.clear()
        scene.witcher_lipsync_lines.clear()
        scene.witcher_lipsync_line_index = -1
        scene.witcher_cutscene_actor_items.clear()


def assert_optional_tts_hook(scene, ui_cutscene, redkit_project):
    prefs = addon._get_prefs(bpy.context)
    old_command = prefs.tts_command
    real_project_path = redkit_project.get_active_project_path
    real_generate = ui_cutscene.generate_cutscene_dialog_wav
    scene.witcher_cutscene_dialog_lines.clear()
    scene.witcher_cutscene_dialog_items.clear()
    scene.witcher_lipsync_lines.clear()
    scene.witcher_lipsync_line_index = -1
    scene.witcher_cutscene_dialog_id_space = 9998
    redkit_project.get_active_project_path = lambda _context: None
    try:
        with tempfile.TemporaryDirectory(prefix="cutscene_tts_", dir=str(REPO_ROOT / "WORKING_TEMP")) as temp_dir:
            output_path = Path(temp_dir) / "generated line.wav"
            index = ui_cutscene.add_cutscene_dialog_line(scene, text='Run & hide, "now".')
            line = scene.witcher_cutscene_dialog_lines[index]
            line.speaker = "CIRI"
            line.tier = 'WAV'
            line.start_frame = 52
            line.end_frame = 82
            line.wav_path = str(output_path)
            prefs.tts_command = r'"C:\Program Files\External TTS\tts.exe" --text "{text}" --out "{out}"'
            calls = []

            def fake_run(argv, **kwargs):
                calls.append((argv, kwargs))
                assert argv[0] == r"C:\Program Files\External TTS\tts.exe", argv
                assert argv[1:4] == ["--text", 'Run & hide, "now".', "--out"], argv
                assert Path(argv[4]) == output_path
                with wave.open(str(output_path), "wb") as wav_file:
                    wav_file.setnchannels(1)
                    wav_file.setsampwidth(2)
                    wav_file.setframerate(8000)
                    wav_file.writeframes(b"\0\0" * 800)
                return SimpleNamespace(returncode=0, stdout="generated", stderr="")

            generated_path, editor_line, soundstrip, completed = ui_cutscene.generate_cutscene_dialog_wav(
                bpy.context,
                line_index=index,
                run_command=fake_run,
            )
            assert generated_path == str(output_path)
            assert completed.returncode == 0 and len(calls) == 1
            assert calls[0][1]["cwd"] == str(output_path.parent)
            assert calls[0][1]["check"] is False and calls[0][1]["timeout"] == 300
            line = scene.witcher_cutscene_dialog_lines[index]
            assert line.wav_path == str(output_path)
            assert line.lipsync_ref == editor_line.line_id == "2119998000"
            assert soundstrip is not None and soundstrip.frame_start == 52

            try:
                ui_cutscene._cutscene_dialog_tts_argv("tts --out {out}", line.text, str(output_path))
            except RuntimeError as exc:
                assert "{text}" in str(exc)
            else:
                raise AssertionError("TTS template without {text} was accepted")

            operator_calls = []
            ui_cutscene.generate_cutscene_dialog_wav = lambda context, line_index=None: (
                operator_calls.append((context, line_index)) or (
                    str(output_path), editor_line, soundstrip, SimpleNamespace(returncode=0)
                )
            )
            reports = []
            operator = SimpleNamespace(
                line_index=index,
                report=lambda level, message: reports.append((level, message)),
            )
            execute = ui_cutscene.WITCH_OT_CutsceneDialogGenerateWav.execute.__get__(operator)
            assert execute(bpy.context) == {'FINISHED'}
            assert operator_calls == [(bpy.context, index)]
            assert reports and reports[-1][0] == {'INFO'}
    finally:
        ui_cutscene.generate_cutscene_dialog_wav = real_generate
        redkit_project.get_active_project_path = real_project_path
        prefs.tts_command = old_command
        scene.witcher_cutscene_dialog_lines.clear()
        scene.witcher_cutscene_dialog_items.clear()
        scene.witcher_lipsync_lines.clear()
        scene.witcher_lipsync_line_index = -1


def assert_zero_line_wrapper_baseline(scene_builder, cr2w_writer, read_CR2W, get_cutscene_dialog_lines):
    depot_path = "dlc\\modw3tools_tests\\data\\cutscenes\\cs_dialogue_native.w2cutscene"
    work_root = REPO_ROOT / "WORKING_TEMP"
    work_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cutscene_dialogue_", dir=work_root) as temp_dir:
        temp_dir = Path(temp_dir)
        omitted_path = temp_dir / "omitted.w2scene"
        empty_path = temp_dir / "empty.w2scene"
        scene_builder.save_cutscene_wrapper_scene(str(omitted_path), depot_path, duration=4.0)

        kwargs = {"lines": []} if "lines" in inspect.signature(
            scene_builder.build_cutscene_wrapper_scene
        ).parameters else {}
        empty_wrapper = scene_builder.build_cutscene_wrapper_scene(depot_path, duration=4.0, **kwargs)
        cr2w_writer.write_w2scene(empty_wrapper, str(empty_path))
        assert omitted_path.read_bytes() == empty_path.read_bytes()

        parsed = read_CR2W(str(omitted_path))
        chunk_types = [chunk.name for chunk in parsed.CHUNKS.CHUNKS]
        assert "CStorySceneCutsceneSection" in chunk_types
        assert "CStorySceneLine" not in chunk_types
        assert get_cutscene_dialog_lines(str(omitted_path), depot_path) == []


def assert_line_wrapper_round_trip(scene_builder, cr2w_writer, read_CR2W, get_cutscene_dialog_lines):
    depot_path = "dlc\\modw3tools_tests\\data\\cutscenes\\cs_dialogue_native.w2cutscene"
    lines = [
        {
            "voicetag": "GERALT",
            "speaking_to": "CIRI",
            "string_id": 123456,
            "approved_duration": 1.25,
            "voice_file_name": "123456",
            "sound_event": "vo_geralt_123456",
            "is_background": False,
        },
        {
            "voicetag": "CIRI",
            "speaking_to": "GERALT",
            "string_id": 123457,
            "approved_duration": 2.5,
        },
    ]
    work_root = REPO_ROOT / "WORKING_TEMP"
    work_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cutscene_dialogue_lines_", dir=work_root) as temp_dir:
        temp_dir = Path(temp_dir)
        scene_path = temp_dir / "lines.w2scene"
        wrapper = scene_builder.build_cutscene_wrapper_scene(depot_path, duration=5.0, lines=lines)
        cr2w_writer.write_w2scene(wrapper, str(scene_path))

        parsed = read_CR2W(str(scene_path))
        chunks = parsed.CHUNKS.CHUNKS
        root = next(chunk for chunk in chunks if chunk.name == "CStoryScene")
        assert root.GetVariableByName("elementIDCounter").Value == 4
        section = next(chunk for chunk in chunks if chunk.name == "CStorySceneCutsceneSection")
        element_chunks = [chunks[index - 1] for index in section.GetVariableByName("sceneElements").value]
        assert [chunk.name for chunk in element_chunks] == [
            "CStorySceneCutscenePlayer", "CStorySceneLine", "CStorySceneLine",
        ]
        assert [chunk.GetVariableByName("elementID").ToString() for chunk in element_chunks] == [
            "CutscenePlayer_2", "Line_3", "Line_4",
        ]
        parsed_lines = element_chunks[1:]
        assert [chunk.GetVariableByName("speakingTo").ToString() for chunk in parsed_lines] == ["CIRI", "GERALT"]
        assert all(chunk.GetVariableByName("isBackgroundLine").Value for chunk in parsed_lines)
        assert parsed_lines[0].GetVariableByName("voiceFileName").ToString() == "123456"
        assert parsed_lines[0].GetVariableByName("soundEventName").ToString() == "vo_geralt_123456"

        reverse = get_cutscene_dialog_lines(str(scene_path), depot_path)
        assert [line["actor"] for line in reverse] == ["GERALT", "CIRI"]
        assert [line["line_index"] for line in reverse] == [123456, 123457]
        assert [round(line["approved_duration"], 3) for line in reverse] == [1.25, 2.5]

        wolvenkit = Path(
            r"G:\sourcetree\WolvenKit-7\WolvenKit.CLI\bin\Release\net481\WolvenKit.CLI.exe"
        )
        if wolvenkit.is_file():
            json_path = temp_dir / "lines.json"
            rebuilt_path = temp_dir / "lines_roundtrip.w2scene"
            subprocess.run(
                [str(wolvenkit), "--input", str(scene_path), "--output", str(json_path), "--cr2w2json"],
                check=True,
            )
            subprocess.run(
                [str(wolvenkit), "--input", str(json_path), "--output", str(rebuilt_path), "--json2cr2w"],
                check=True,
            )
            assert "CStorySceneLine" in [chunk.name for chunk in read_CR2W(str(rebuilt_path)).CHUNKS.CHUNKS]


def assert_voicetag_alias_matching(scene, ui_cutscene, ui_voice):
    assert {"CIRILLA", "CIRI"} <= ui_voice.voice_speaker_aliases("CIRILLA")
    assert {"GERALT", "GRLT"} <= ui_voice.voice_speaker_aliases("geralt_player")
    assert ui_voice.voice_speaker_voicetag("CIRI") == "CIRILLA"
    assert ui_voice.voice_speaker_aliases("CUSTOM_NPC_TAG") == {"CUSTOM_NPC_TAG"}

    scene.witcher_cutscene_actor_items.clear()
    scene.witcher_cutscene_dialog_lines.clear()
    scene.witcher_cutscene_dialog_items.clear()
    for actor_name, voice_tag in (("cirilla", "CIRILLA"), ("geralt_player", "")):
        actor = scene.witcher_cutscene_actor_items.add()
        actor.actor_name = actor_name
        actor.voice_tag = voice_tag
        actor.actor_type = "CAT_Actor"
    scene.witcher_cutscene_loaded_actor_index = 0
    ciri_index = ui_cutscene.add_cutscene_dialog_line(scene, text="alias probe")
    scene.witcher_cutscene_loaded_actor_index = 1
    geralt_index = ui_cutscene.add_cutscene_dialog_line(scene, text="second speaker")
    assert scene.witcher_cutscene_dialog_lines[ciri_index].speaker == "CIRILLA"
    assert scene.witcher_cutscene_dialog_lines[geralt_index].speaker == "geralt_player"

    real_get_object = ui_cutscene._get_loaded_cutscene_actor_object
    ui_cutscene._get_loaded_cutscene_actor_object = lambda item: item.actor_name
    try:
        assert ui_cutscene._find_actor_obj_by_voicetag(scene, "CIRI") == "cirilla"
        assert ui_cutscene._find_actor_obj_by_voicetag(scene, "GERALT") == "geralt_player"
        assert ui_cutscene._find_actor_obj_by_voicetag(scene, "YENNEFER") is None
    finally:
        ui_cutscene._get_loaded_cutscene_actor_object = real_get_object

    nodes = [
        {
            "game": "W3", "voiceLineId": "555", "line_id": "555", "speaker": "CIRI",
            "speaker_candidates": ["CIRI"], "text": "Ciri speaks", "duration": "1.0",
            "display_compact": "[CIRI] Ciri speaks", "search_blob": "555 ciri cirilla ciri speaks 1.0",
        },
        {
            "game": "W3", "voiceLineId": "666", "line_id": "666", "speaker": "GERALT",
            "speaker_candidates": ["GERALT"], "text": "Geralt speaks", "duration": "1.0",
            "display_compact": "[GERALT] Geralt speaks", "search_blob": "666 geralt geralt speaks 1.0",
        },
    ]
    real_ensure = ui_voice.ensure_voice_cache
    real_cache = ui_voice._voice_node_cache
    ui_voice.ensure_voice_cache = lambda _context=None: None
    ui_voice._voice_node_cache = nodes
    state = bpy.context.window_manager.witcher_cutscene_dialog_voice
    try:
        for speaker, expected in (("CIRILLA", "555"), ("geralt_player", "666"), ("", "555")):
            state.refreshing = True
            try:
                state.query = ""
                state.speaker = speaker
            finally:
                state.refreshing = False
            ui_cutscene.refresh_cutscene_dialog_voice_results(state, bpy.context)
            assert state.results[0].voice_line_id == expected, (speaker, [r.voice_line_id for r in state.results])
        assert len(state.results) == 2

        fake_wm = SimpleNamespace(
            witcher_cutscene_dialog_voice=state,
            invoke_props_dialog=lambda _operator, **_kwargs: {'RUNNING_MODAL'},
        )
        fake_context = SimpleNamespace(scene=scene, window_manager=fake_wm)
        state.initialized = False
        for index, expected_speaker in ((ciri_index, "CIRILLA"), (geralt_index, "GERALT_PLAYER")):
            operator = SimpleNamespace(line_index=index, report=lambda *_args: None)
            assert ui_cutscene.WITCH_OT_CutsceneDialogPickGameVoice.invoke(operator, fake_context, None) == {'RUNNING_MODAL'}
            assert state.speaker == expected_speaker
        assert [row.voice_line_id for row in state.results] == ["666"]

        state.line_index = ciri_index
        state.refreshing = True
        try:
            state.speaker = "CIRILLA"
        finally:
            state.refreshing = False
        ui_cutscene.refresh_cutscene_dialog_voice_results(state, bpy.context)
        state.result_index = 0
        line = ui_cutscene.apply_cutscene_dialog_voice_result(bpy.context, state)
        assert (line.speaker, line.game_line_id) == ("CIRILLA", "555")

        state.refreshing = True
        try:
            state.speaker = "GERALT"
        finally:
            state.refreshing = False
        ui_cutscene.refresh_cutscene_dialog_voice_results(state, bpy.context)
        state.result_index = 0
        line = ui_cutscene.apply_cutscene_dialog_voice_result(bpy.context, state)
        assert (line.speaker, line.game_line_id) == ("geralt_player", "666")

        scene.witcher_cutscene_actor_items.clear()
        line.speaker = "TRISS"
        line = ui_cutscene.apply_cutscene_dialog_voice_result(bpy.context, state)
        assert line.speaker == "GERALT"
    finally:
        ui_voice.ensure_voice_cache = real_ensure
        ui_voice._voice_node_cache = real_cache
        state.initialized = False
        scene.witcher_cutscene_actor_items.clear()
        scene.witcher_cutscene_dialog_lines.clear()
        scene.witcher_cutscene_dialog_items.clear()


exit_code = 0
registered = False
try:
    addon.register()
    registered = True

    from witcher3_tools.CR2W import cr2w_writer, dc_scene, scene_builder
    from witcher3_tools.CR2W.CR2W_file import read_CR2W
    from witcher3_tools.CR2W.dc_scene import get_cutscene_dialog_lines
    from witcher3_tools.animation import cutscene_validate
    from witcher3_tools.exporters import export_cutscene
    from witcher3_tools.lipsync import redkit_project
    from witcher3_tools.lipsync import ui_lipsync
    from witcher3_tools.importers import import_cutscene
    from witcher3_tools.ui import ui_animated_component, ui_cutscene, ui_voice

    assert_preview_line_baseline(bpy.context.scene, ui_cutscene)
    assert_authored_line_model(bpy.context.scene, ui_cutscene)
    assert_game_voice_picker(bpy.context.scene, ui_cutscene, ui_voice, import_cutscene)
    assert_voicetag_alias_matching(bpy.context.scene, ui_cutscene, ui_voice)
    assert_dialogue_nla_preview_cleanup(bpy.context.scene, ui_cutscene, ui_voice)
    assert_w3_picker_cache_isolated(bpy.context.scene, ui_cutscene, ui_voice)
    assert_dialogue_tooltips(ui_cutscene)
    assert_linked_scene_dialog_fallback(import_cutscene, dc_scene)
    assert_project_id_copy_back(bpy.context.scene, ui_cutscene, redkit_project)
    assert_export_payload_and_validation(
        bpy.context.scene, export_cutscene, cutscene_validate, redkit_project,
    )
    assert_wrapper_dialogue_preflight(
        bpy.context.scene, ui_cutscene, ui_animated_component, export_cutscene, scene_builder,
    )
    assert_string_id_preparation(bpy.context.scene, export_cutscene, redkit_project)
    assert_line_id_status(bpy.context.scene, export_cutscene, cutscene_validate, redkit_project)
    assert_wav_lipsync_handoff(
        bpy.context.scene, ui_cutscene, ui_lipsync, redkit_project,
    )
    assert_speech_connect(bpy.context.scene, ui_cutscene, ui_lipsync, export_cutscene, redkit_project)
    assert_optional_tts_hook(bpy.context.scene, ui_cutscene, redkit_project)
    assert_zero_line_wrapper_baseline(scene_builder, cr2w_writer, read_CR2W, get_cutscene_dialog_lines)
    assert_line_wrapper_round_trip(scene_builder, cr2w_writer, read_CR2W, get_cutscene_dialog_lines)
except Exception:
    traceback.print_exc()
    print("W3TB_CUTSCENE_DIALOGUE_FAIL")
    exit_code = 1
finally:
    if registered:
        try:
            addon.unregister()
        except Exception:
            traceback.print_exc()
            if not exit_code:
                print("W3TB_CUTSCENE_DIALOGUE_FAIL")
            exit_code = 1

if exit_code:
    sys.exit(exit_code)
print("W3TB_CUTSCENE_DIALOGUE_OK")
