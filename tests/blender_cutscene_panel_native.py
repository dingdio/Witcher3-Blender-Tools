import sys
import traceback
from pathlib import Path

import bpy

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import witcher3_tools as addon

_ICON_ITEMS = bpy.types.UILayout.bl_rna.functions["label"].parameters["icon"].enum_items
_ICONS = {item.identifier for item in _ICON_ITEMS}
_FILTER_FLAG = 1 << 30  # UIList.bitflag_filter_item


def _bind(cls, host, names):
    for attr in names:
        if attr in cls.__dict__:
            setattr(host, attr, cls.__dict__[attr].__get__(host))
    return host


class FakeOperator:
    def __init__(self, calls, idname, instance_id):
        object.__setattr__(self, "calls", calls)
        object.__setattr__(self, "idname", idname)
        object.__setattr__(self, "instance_id", instance_id)

    def __setattr__(self, name, value):
        self.calls.append(("op_prop", self.idname, name, value, self.instance_id))


class FakeLayout:
    def __init__(self, calls=None, parent=None, counter=None, expand_default_closed=None):
        self.calls = calls if calls is not None else []
        self.parent = parent
        self._counter = counter if counter is not None else [0]
        self._expand_default_closed = (
            parent._expand_default_closed if expand_default_closed is None and parent is not None
            else bool(expand_default_closed)
        )
        self.node_id = self._counter[0]
        self._counter[0] += 1
        self._enabled = self._active = True
        self.scale_x = self.scale_y = 1.0
        self.alert = self.use_property_split = self.use_property_decorate = False
        self.alignment = 'EXPAND'
        self.ui_units_x = self.ui_units_y = 0.0

    @property
    def enabled(self):
        return self._enabled and (self.parent.enabled if self.parent is not None else True)

    @enabled.setter
    def enabled(self, value):
        self._enabled = bool(value)

    @property
    def active(self):
        return self._active and (self.parent.active if self.parent is not None else True)

    @active.setter
    def active(self, value):
        self._active = bool(value)

    def _sub(self, *_a, **_k):
        return FakeLayout(self.calls, self, self._counter, self._expand_default_closed)

    def row(self, *_a, **kw):
        child = self._sub()
        self.calls.append(("layout", "row", child.node_id, self.node_id, kw.get("align", False)))
        return child

    column = box = split = grid_flow = column_flow = _sub

    def panel(self, idname, default_closed=False, **_k):
        self.calls.append(("panel", idname, bool(default_closed)))
        body = None if default_closed and not self._expand_default_closed else self._sub()
        return self._sub(), body

    def _state(self, kind, key="", text="", value=None, icon='NONE', instance_id=None):
        parent_id = self.parent.node_id if self.parent is not None else None
        self.calls.append((
            "state", kind, key, text, value, icon, bool(self.enabled), self.node_id, parent_id, instance_id,
        ))

    def separator(self, *_a, **_k):
        pass

    separator_spacer = separator

    def label(self, text="", icon='NONE', **_k):
        assert icon in _ICONS, f"unknown icon {icon!r}"
        self.calls.append(("label", text))
        self._state("label", text, text=text, icon=icon)

    def prop(self, data, path, **kw):
        value = data.path_resolve(path)
        assert kw.get("icon", 'NONE') in _ICONS
        self.calls.append(("prop", path))
        self._state("prop", path, text=kw.get("text", ""), value=value, icon=kw.get("icon", 'NONE'))

    def prop_enum(self, data, path, value, **kw):
        data.path_resolve(path)
        items = data.bl_rna.properties[path].enum_items
        assert not len(items) or value in items, f"{path}: unknown enum value {value!r}"
        self.calls.append(("prop_enum", f"{path}:{value}"))
        self._state("prop_enum", path, text=kw.get("text", ""), value=value, icon=kw.get("icon", 'NONE'))

    def prop_search(self, data, path, search_data, search_prop, **kw):
        value = data.path_resolve(path)
        resolver = getattr(search_data, "path_resolve", None)
        if resolver is not None:
            resolver(search_prop)
        else:
            getattr(search_data, search_prop)
        self.calls.append(("prop", path))
        self._state("prop", path, text=kw.get("text", ""), value=value, icon=kw.get("icon", 'NONE'))

    def menu(self, idname, **_k):
        assert hasattr(bpy.types, idname), f"unknown menu {idname}"
        self.calls.append(("menu", idname))

    def operator(self, idname, text="", icon='NONE', **_k):
        module, name = idname.split(".")
        assert hasattr(bpy.types, f"{module.upper()}_OT_{name}"), f"unknown operator {idname}"  # bpy.ops resolves lazily
        assert icon in _ICONS, f"unknown icon {icon!r}"
        self.calls.append(("op", idname))
        self.calls.append(("op_text", idname, text))
        instance_id = self._counter[0]
        self._counter[0] += 1
        self._state("op", idname, text=text, icon=icon, instance_id=instance_id)
        return FakeOperator(self.calls, idname, instance_id)

    def template_list(self, listtype_name, _list_id, dataptr, propname, active_dataptr, active_propname, **_k):
        cls = getattr(bpy.types, listtype_name, None)
        assert cls is not None, f"unknown UIList {listtype_name}"
        items = dataptr.path_resolve(propname)
        active_dataptr.path_resolve(active_propname)
        host = _bind(cls, type("Host", (), {"layout_type": 'DEFAULT', "bitflag_filter_item": _FILTER_FLAG})(),
                     ("draw_item", "filter_items"))
        flags = host.filter_items(bpy.context, dataptr, propname)[0] if hasattr(host, "filter_items") else None
        for i, item in enumerate(items):
            if flags is None or flags[i] & _FILTER_FLAG:
                host.draw_item(
                    bpy.context, FakeLayout(self.calls, self, self._counter),
                    dataptr, item, 0, active_dataptr, active_propname, i, 0,
                )
        self.calls.append(("list", listtype_name))
        self._state("list", listtype_name)


def draw_panel(cls):
    calls = []
    host = _bind(
        cls,
        type("Host", (), {"layout": FakeLayout(calls, expand_default_closed=True)})(),
        ("draw", "draw_header"),
    )
    host.draw(bpy.context)
    if hasattr(host, "draw_header"):
        host.draw_header(bpy.context)
    return calls


def draw_all_tabs(state, tab_ids, cls):
    scene = bpy.context.scene
    result = {}
    for tab in tab_ids:
        scene.witcher_cs_tab = tab
        try:
            result[tab] = draw_panel(cls)
        except Exception as exc:
            raise AssertionError(f"[{state}] tab {tab} failed to draw: {exc}") from exc
    return result


def draw_dialogue_editor_state(scene, cls):
    authored = getattr(scene, "witcher_cutscene_dialog_lines", None)
    if authored is None:
        return None
    prefs = addon._get_prefs(bpy.context)
    old_tts_command = prefs.tts_command
    prefs.tts_command = ""
    authored.clear()
    authored.add()
    line = authored[0]
    line.speaker = "GERALT"
    line.text = "Authored dialogue probe"
    line.start_frame = 20
    line.end_frame = 80
    line.tier = 'SUBTITLE'
    old_repo_path = scene.witcher_cutscene_export_repo_path
    scene.witcher_cutscene_export_repo_path = r"dlc\panel\data\cutscenes\dialogue_probe.w2cutscene"
    scene.witcher_cs_tab = 'DIALOGS'
    calls = draw_panel(cls)
    labels = [call[1] for call in calls if call[0] == "label"]
    assert "Dialogue (preview)" not in labels, labels
    assert ("list", "WITCH_UL_CutsceneAuthoredDialogList") in calls, calls
    for operator in (
        "witcher.cutscene_dialog_add_line",
        "witcher.cutscene_dialog_remove_line",
        "witcher.cutscene_dialog_move_line",
        "witcher.cutscene_dialog_from_playhead",
    ):
        assert ("op", operator) in calls, (operator, calls)
    for path in ("speaker", "text", "start_frame", "end_frame", "tier"):
        assert ("prop", path) in calls, (path, calls)
    companion_values = [
        call[3] for call in calls
        if call[:3] == ("op_prop", "witcher.cutscene_exact_value_details", "value")
    ]
    assert companion_values == [r"dlc\panel\data\scenes\dialogue_probe.w2scene"], companion_values
    line.tier = 'GAME'
    game_calls = draw_panel(cls)
    assert ("prop", "game_line_id") in game_calls
    assert ("prop", "game_voice_file_name") in game_calls
    assert ("op", "witcher.cutscene_dialog_pick_game_voice") in game_calls
    assert ("op", "witcher.cutscene_dialog_preview_game_line") in game_calls
    line.tier = 'WAV'
    wav_calls = draw_panel(cls)
    assert ("prop", "wav_path") in wav_calls
    assert ("prop", "allocated_line_id") in wav_calls
    assert ("op", "witcher.cutscene_dialog_prepare_wav") in wav_calls
    assert ("op", "witcher.cutscene_dialog_generate_wav") not in wav_calls
    prefs.tts_command = 'tts --text "{text}" --out "{out}"'
    tts_calls = draw_panel(cls)
    assert ("op", "witcher.cutscene_dialog_generate_wav") in tts_calls
    prefs.tts_command = old_tts_command
    scene.witcher_cutscene_export_repo_path = old_repo_path
    authored.clear()
    scene.witcher_cutscene_dialog_items.clear()
    return calls


def header_lines(calls):
    labels = [c[1] for c in calls if c[0] == "label"]
    return labels[0], next(t for t in labels if t.startswith(("Next:", "Ready")))


def states(calls, kind, key=None):
    return [c for c in calls if c[0] == "state" and c[1] == kind and (key is None or c[2] == key)]


def assert_action_state(calls, idname, enabled, reason=None, text=None, check_reason=True):
    matches = [c for c in states(calls, "op", idname) if text is None or c[3] == text]
    assert len(matches) == 1 and matches[0][6] is enabled, (idname, enabled, matches)
    action = matches[0]
    outer_labels = [c for c in states(calls, "label") if c[7] == action[8]]
    if reason is not None:
        assert len(outer_labels) == 1, (idname, reason, outer_labels)
        assert outer_labels[0][2] == reason and outer_labels[0][6], (action, outer_labels)
    elif check_reason:
        assert not outer_labels, (idname, text, outer_labels)
    return action


def assert_disabled_prop(calls, path, value, text=None):
    matches = [
        c for c in states(calls, "prop", path)
        if c[4] == value and not c[6] and (text is None or c[3] == text)
    ]
    assert matches, (path, value, text, states(calls, "prop", path))
    return matches[0]


def assert_exact_value_details(calls, prop_state, field_label, value):
    idname = "witcher.cutscene_exact_value_details"
    matches = []
    for operator in states(calls, "op", idname):
        assigned = {
            call[2]: call[3] for call in calls
            if call[0] == "op_prop" and call[1] == idname and call[4] == operator[9]
        }
        if assigned == {"field_label": field_label, "value": value}:
            matches.append(operator)
    assert len(matches) == 1, (field_label, value, matches)
    details = matches[0]
    assert details[3] == "" and details[5] == 'INFO' and details[6], details
    assert details[7] == prop_state[8], (prop_state, details)
    return details


def assert_default_closed(calls, *idnames):
    panels = {call[1]: call[2] for call in calls if call[0] == "panel"}
    assert all(panels.get(idname) is True for idname in idnames), (idnames, panels)


def assert_operator_poll(idname, expected):
    module, name = idname.split(".")
    actual = bool(getattr(getattr(bpy.ops, module), name).poll())
    assert actual is expected, (idname, expected, actual)


def select_only(*objs):
    for o in bpy.data.objects:
        o.select_set(False)
    for o in objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = objs[0] if objs else None


def make_scratch_actor(scene):
    arm = bpy.data.objects.new("scratch_actor", bpy.data.armatures.new("scratch_rig"))
    scene.collection.objects.link(arm)
    select_only(arm)
    bpy.ops.object.mode_set(mode='EDIT')
    bone = arm.data.edit_bones.new("root")
    bone.head, bone.tail = (0.0, 0.0, 0.0), (0.0, 0.0, 1.0)
    bpy.ops.object.mode_set(mode='POSE')
    pb = arm.pose.bones["root"]
    for frame, x in ((1, 0.0), (24, 1.0)):
        pb.location = (x, 0.0, 0.0)
        pb.keyframe_insert("location", frame=frame)
    bpy.ops.object.mode_set(mode='OBJECT')
    action = arm.animation_data.action
    action.name = "scratch_idle"
    return arm, action


def make_technical_clip(scene, actor_name, source_action, *, prop=False):
    arm = bpy.data.objects.new(f"panel_{actor_name}_clip", bpy.data.armatures.new(f"panel_{actor_name}_rig"))
    scene.collection.objects.link(arm)
    arm["cutscene_actor_name"] = actor_name
    if prop:
        arm[cutscene_bake.PROP_RIG_TAG] = True
    action = source_action.copy()
    action.name = f"panel_{actor_name}_action"
    track = arm.animation_data_create().nla_tracks.new()
    track.name = "cutscene_anim"
    track.strips.new(f"panel_{actor_name}_strip", 1, action)
    return arm, action


def assert_shots_to_rig_rollback(scene, source_action, ui_anims, ui_cutscene):
    old_frame = (scene.frame_current, scene.frame_subframe, scene.frame_end)
    rig_data = bpy.data.armatures.new("panel_camera_transaction_rig_data")
    rig = bpy.data.objects.new("panel_camera_transaction_rig", rig_data)
    scene.collection.objects.link(rig)
    select_only(rig)
    bpy.ops.object.mode_set(mode='EDIT')
    bone = rig_data.edit_bones.new(CAMERA_CONTROL_BONE)
    bone.head, bone.tail = (0.0, 0.0, 0.0), (0.0, 0.0, 1.0)
    bpy.ops.object.mode_set(mode='OBJECT')
    rig["cutscene_actor_name"] = "camera_transaction_probe"
    rig["cutscene_actor_type"] = "CAT_Camera"
    camera_bone = rig.pose.bones[CAMERA_CONTROL_BONE]
    present_track, absent_track = CAMERA_TRACK_NAMES[0], CAMERA_TRACK_NAMES[-1]
    camera_bone[present_track] = 47.25

    cameras = []
    shots = []
    for shot_index, frame in enumerate((1, 35, 69)):
        data = bpy.data.cameras.new(f"panel_transaction_camera_data_{shot_index}")
        camera = bpy.data.objects.new(f"panel_transaction_camera_{shot_index}", data)
        scene.collection.objects.link(camera)
        cameras.append(camera)
        shots.append((shot_index, camera, frame))
    driver_curve = cameras[0].data.driver_add("lens")
    driver_curve.driver.type = 'SCRIPTED'
    driver_curve.driver.expression = "42.0"

    old_actions = []
    old_track = rig.animation_data_create().nla_tracks.new()
    old_track.name = "cutscene_anim"
    for shot_index, frame in enumerate((1, 35, 69)):
        action = source_action.copy()
        action.name = f"panel_old_camera_shot_{shot_index}"
        action["witcher_shot_index"] = shot_index
        ui_anims._tag_action_for_cutscene(
            action,
            "camera_transaction_probe",
            "Root",
            "camera",
            scene,
            source_index=ui_cutscene.AUTHORED_CLIP_ID_BASE + 800 + shot_index,
        )
        track, _strip = ui_anims._create_camera_cut_strip(
            rig,
            old_track,
            action.name,
            frame,
            min(frame + 30, 100),
            action,
            action_start=1.0,
            action_end=24.0,
            settings={"blend_type": "COMBINE", "extrapolation": "NOTHING"},
        )
        old_track = track
        old_actions.append(action)

    scene.frame_end = 100
    sequence_prop = ui_cutscene.AUTHORED_CLIP_SEQUENCE_PROP

    def state():
        tracks = tuple(sorted(
            (
                track.as_pointer(), track.name, bool(track.mute),
                tuple(
                    (
                        strip.as_pointer(), strip.name, strip.action.as_pointer(),
                        float(strip.frame_start), float(strip.frame_end),
                    )
                    for strip in track.strips
                ),
            )
            for track in rig.animation_data.nla_tracks
        ))
        actions = tuple(sorted((action.as_pointer(), action.name, action.users) for action in bpy.data.actions))
        props = tuple(
            (
                name,
                name in camera_bone,
                camera_bone.get(name),
                dict(camera_bone.id_properties_ui(name).as_dict()) if name in camera_bone else None,
            )
            for name in CAMERA_TRACK_NAMES
        )
        drivers = tuple(
            (curve.as_pointer(), curve.data_path, bool(curve.mute), curve.driver.expression)
            for curve in cameras[0].data.animation_data.drivers
        )
        return (
            tracks,
            actions,
            (sequence_prop in scene, scene.get(sequence_prop)),
            props,
            drivers,
            (scene.frame_current, scene.frame_subframe, scene.frame_end),
        )

    real_find = ui_anims._find_camera_armature
    real_bake = ui_anims._bake_camera_rig_action_from_camera
    real_create = ui_anims._create_camera_cut_strip
    reports = []
    operator = type("ShotsRollbackProbe", (), {
        "report": lambda _self, levels, message: reports.append((levels, message)),
    })()
    before = state()
    try:
        ui_anims._find_camera_armature = lambda _context: rig
        for phase, fail_at in (("bake", 1), ("bake", 2), ("strip", 1), ("strip", 2)):
            calls = {"bake": 0, "strip": 0}

            def fake_bake(_context, _rig, _camera, _start, _end, action_name, **_kwargs):
                calls["bake"] += 1
                camera_bone[present_track] = 900.0 + calls["bake"]
                camera_bone[absent_track] = 800.0 + calls["bake"]
                action = source_action.copy()
                action.name = action_name
                if phase == "bake" and calls["bake"] == fail_at:
                    raise RuntimeError(f"intentional {phase} failure {fail_at}")
                return action

            def fake_create(*args, **kwargs):
                calls["strip"] += 1
                result = real_create(*args, **kwargs)
                if phase == "strip" and calls["strip"] == fail_at:
                    raise RuntimeError(f"intentional {phase} failure {fail_at}")
                return result

            ui_anims._bake_camera_rig_action_from_camera = fake_bake
            ui_anims._create_camera_cut_strip = fake_create
            result = ui_anims.WITCH_OT_CameraApplyBlenderCamerasToRig._apply_shots(
                operator, bpy.context, shots,
            )
            assert result == {'CANCELLED'}, (phase, fail_at, result)
            assert state() == before, (phase, fail_at, state(), before)
    finally:
        ui_anims._find_camera_armature = real_find
        ui_anims._bake_camera_rig_action_from_camera = real_bake
        ui_anims._create_camera_cut_strip = real_create
        for camera in cameras:
            data = camera.data
            bpy.data.objects.remove(camera, do_unlink=True)
            bpy.data.cameras.remove(data)
        bpy.data.objects.remove(rig, do_unlink=True)
        bpy.data.armatures.remove(rig_data)
        for action in old_actions:
            if action.name in bpy.data.actions and action.users == 0:
                bpy.data.actions.remove(action)
        scene.frame_set(old_frame[0], subframe=old_frame[1])
        scene.frame_end = old_frame[2]
    assert len(reports) == 4, reports


addon.register()
try:
    from witcher3_tools.ui import ui_anims, ui_cutscene
    from witcher3_tools.repo_paths.animation_index import find_anims
    from witcher3_tools.extension_paths import get_dev_override
    from witcher3_tools.importers.import_cutscene import ACTOR_CUSTOM_PROP_DEFAULTS
    from witcher3_tools.animation import cutscene_bake, cutscene_validate
    from witcher3_tools.animation.camera_tracks import CAMERA_CONTROL_BONE, CAMERA_TRACK_NAMES

    collapsed_layout = FakeLayout()
    _collapsed_header, collapsed_body = collapsed_layout.panel("collapsed_probe", default_closed=True)
    assert collapsed_body is None
    expanded_layout = FakeLayout(expand_default_closed=True)
    _expanded_header, expanded_body = expanded_layout.panel("expanded_probe", default_closed=True)
    assert expanded_body is not None
    rna_probe_calls = []
    FakeLayout(rna_probe_calls).prop_search(
        bpy.context.scene,
        "render.fps",
        bpy.context.scene,
        "render.engine",
    )
    assert ("prop", "render.fps") in rna_probe_calls

    def assert_actor_keys(*objs):
        for obj in objs:
            missing = [k for k in ACTOR_CUSTOM_PROP_DEFAULTS if obj is not None and k not in obj]
            assert not missing, (obj.name, missing)

    assert callable(find_anims)
    panel_cls = ui_cutscene.WITCHER_PT_cutscene_panel
    scene = bpy.context.scene
    tab_items = list(bpy.types.Scene.bl_rna.properties["witcher_cs_tab"].enum_items)
    tab_ids = [item.identifier for item in tab_items]
    expected_tab_calls = [
        ('ACTORS', 'Actors', 'ARMATURE_DATA'),
        ('ANIMS', 'Clips', 'NLA'),
        ('CAMERA', 'Camera', 'CAMERA_DATA'),
        ('EVENTS', 'Events', 'SEQUENCE'),
        ('DIALOGS', 'Dialogue', 'OUTLINER_OB_SPEAKER'),
        ('TEMPLATE', 'Export', 'EXPORT'),
    ]
    expected_tab_values = [1, 2, 3, 4, 5, 0]
    assert [
        (item.identifier, item.name, item.value, item.icon) for item in tab_items
    ] == [
        (identifier, name, value, icon)
        for (identifier, name, icon), value in zip(expected_tab_calls, expected_tab_values)
    ], [(item.identifier, item.name, item.value, item.icon) for item in tab_items]

    prefs = addon._get_prefs(bpy.context)
    camera_rel = Path("gameplay") / "camera" / "scene_camera.w2ent"
    uncook = str(getattr(prefs, "uncook_path", "") or "")
    if not (Path(uncook) / camera_rel).is_file():
        uncook = str(get_dev_override("fallback_uncook_path_w3", "") or "")
    have_camera_entity = bool(uncook) and (Path(uncook) / camera_rel).is_file()
    if have_camera_entity:
        prefs.uncook_path = uncook

    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    calls = draw_all_tabs("empty", tab_ids, panel_cls)
    flat = [c for tab_calls in calls.values() for c in tab_calls]
    assert_default_closed(
        flat,
        "witcher_cs_actors_tag_form",
        "witcher_cutscene_loaded_strip_fallback",
        "witcher_cs_events_schema",
        "witcher_cs_export_metadata",
        "witcher_cs_export_burned",
        "witcher_cs_export_template_fields",
    )
    tab_calls = states(calls['ACTORS'], "prop_enum", "witcher_cs_tab")
    assert [(c[4], c[3], c[5]) for c in tab_calls] == expected_tab_calls, tab_calls
    assert len({c[7] for c in tab_calls}) == 2, tab_calls
    assert len({c[7] for c in tab_calls[:3]}) == len({c[7] for c in tab_calls[3:]}) == 1, tab_calls
    assert tab_calls[0][7] != tab_calls[3][7], tab_calls
    tab_row_ids = {tab_calls[0][7], tab_calls[3][7]}
    tab_rows = [c for c in calls['ACTORS'] if c[:2] == ("layout", "row") and c[2] in tab_row_ids]
    assert len(tab_rows) == 2 and all(c[4] is True for c in tab_rows), tab_rows
    assert ("prop", "witcher_cutscene_export_repo_path") in flat
    assert ("op", "witcher.cutscene_create_new") in flat and ("op", "witcher.export_w2_cutscene") in flat
    assert any("No actors" in c[1] for c in calls['ACTORS'] if c[0] == "label"), calls['ACTORS']
    cast_controls = [c for c in calls['ACTORS'] if c[0] == "op_text" and c[1] == "witcher.cast_actor"]
    assert cast_controls == [("op_text", "witcher.cast_actor", "Cast Actor…")], cast_controls
    assert ("prop", "witcher_casting_query") not in calls['ACTORS']
    assert not hasattr(bpy.types.Scene, "witcher_casting_query")
    assert_action_state(calls['ACTORS'], "witcher.cutscene_scratch_assign_actor", False,
                        "Select an armature in the viewport")
    assert_action_state(calls['ACTORS'], "witcher.cutscene_add_props", False,
                        "Select a mesh in the viewport")
    assert_action_state(calls['ACTORS'], "witcher.cutscene_grip_prop", False, check_reason=False)
    assert_operator_poll("witcher.cutscene_add_props", False)
    assert_operator_poll("witcher.cutscene_grip_prop", False)
    assert_action_state(calls['ANIMS'], "witcher.cutscene_browse_add_animation", False,
                        "Select an actor in the Actors tab")
    assert_action_state(calls['ANIMS'], "witcher.cutscene_scratch_add_action", False, check_reason=False)
    bake_action = assert_action_state(calls['TEMPLATE'], "witcher.cutscene_bake", False,
                                      "No actors or props to bake")
    set_range_action = assert_action_state(
        calls['TEMPLATE'], "witcher.cutscene_set_scene_range", True, check_reason=False,
    )
    assert set_range_action[7] == bake_action[8], (bake_action, set_range_action)
    assert_action_state(calls['TEMPLATE'], "witcher.cutscene_generate_props_entity", False,
                        "No props assigned (Actors tab › Props)")
    step_calls = [c for c in states(calls['TEMPLATE'], "op") if c[3][:1].isdigit()]
    assert [c[3] for c in step_calls] == [
        "1  Bake actors", "2  Validate", "3  Export .w2cutscene",
        "4  Write props .w2ent", "5  Write REDkit .w2scene",
    ], step_calls
    assert ("op", "witcher.cutscene_scratch_import_camera") in calls['CAMERA'], "no-rig state offers Load Rig"
    dialog_labels = [c[1] for c in calls['DIALOGS'] if c[0] == "label"]
    assert "Dialogue" in dialog_labels and "Dialogue (preview)" not in dialog_labels, dialog_labels
    assert ("list", "WITCH_UL_CutsceneAuthoredDialogList") in calls['DIALOGS'], calls['DIALOGS']
    assert ("prop", "witcher_lipsync_redkit_project") in calls['DIALOGS'], calls['DIALOGS']
    assert ("op", "witcher.cutscene_dialog_add_from_speech") in calls['DIALOGS'], calls['DIALOGS']
    assert any(
        t.startswith(("idSpace ", "Radish IDs ", "No project")) or ": no idSpace" in t for t in dialog_labels
    ), dialog_labels

    scene.witcher_loaded_w2cutscene_path = r"C:\panel\preview.w2cutscene"
    scene.witcher_cutscene_used_in_files = r"dlc\panel\scenes\preview.w2scene"
    event = scene.witcher_cutscene_event_items.add()
    event.event_type = "CExtAnimCutsceneDialogEvent"
    line = scene.witcher_cutscene_dialog_items.add()
    line.actor, line.line_text, line.line_id = "GERALT", "probe line", "1"
    scene.witcher_cs_tab = 'DIALOGS'
    dcalls = draw_panel(panel_cls)
    preview_labels = [c[1] for c in dcalls if c[0] == "label"]
    assert "Dialogue (preview)" in preview_labels and any(t.startswith("Read-only") for t in preview_labels), preview_labels
    assert ("list", "WITCH_UL_CutsceneDialogList") in dcalls and ("prop", "line_text") in dcalls, dcalls
    assert ("op", "witcher.cutscene_dialog_copy_preview") in dcalls, dcalls
    scene.witcher_cutscene_dialog_items.clear()
    scene.witcher_cutscene_event_items.clear()
    empty_preview_calls = draw_panel(panel_cls)
    empty_preview_labels = [c[1] for c in empty_preview_calls if c[0] == "label"]
    assert "Dialogue" in empty_preview_labels and "Dialogue (preview)" not in empty_preview_labels
    assert ("op", "witcher.cutscene_dialog_add_line") in empty_preview_calls, empty_preview_calls
    scene.witcher_loaded_w2cutscene_path = ""
    scene.witcher_cutscene_used_in_files = ""
    editor_calls = draw_dialogue_editor_state(scene, panel_cls)
    assert editor_calls is not None
    print("STATE dialogue editor OK")
    status, hint = header_lines(calls['ACTORS'])
    assert status == "0 actors · 0 clips · not baked" and hint == "Next: press New or Import", (status, hint)
    assert len(ui_cutscene._cutscene_cast_actors(scene)) == len(ui_cutscene._clip_groups(scene)) == 0

    loaded_path = r"C:\panel\copyable_loaded.w2cutscene"
    burned_event = "panel_burned_event_exact"
    burned_item = r"soundspack\cutscenes\panel_burned_item.wem"
    scene.witcher_loaded_w2cutscene_path = loaded_path
    scene.witcher_loaded_cutscene_name = "copyable_loaded"
    scene.witcher_cutscene_last_import_seconds = 1.25
    scene.witcher_cutscene_burned_audio_event = burned_event
    scene.witcher_cutscene_burned_audio_item_path = burned_item
    actor_row = scene.witcher_cutscene_actor_items.add()
    actor_row.source_index = 0
    actor_row.label = actor_row.actor_name = "preview_actor"
    actor_row.actor_type = "CAT_Actor"
    anim_row = scene.witcher_cutscene_animation_items.add()
    anim_row.source_index = 0
    anim_row.file_backed = True
    anim_row.full_name = "preview_actor:Root:preview_clip"
    anim_row.display_name = "preview_clip"
    anim_row.actor_name = "preview_actor"
    anim_row.component_name = "Root"
    file_counts = (len(scene.witcher_cutscene_actor_items), len(scene.witcher_cutscene_animation_items))
    file_calls = draw_all_tabs("file-only", tab_ids, panel_cls)
    assert file_counts == (
        len(scene.witcher_cutscene_actor_items), len(scene.witcher_cutscene_animation_items),
    ), "draw must not rebuild file rows"
    status, hint = header_lines(file_calls['ACTORS'])
    assert status == "0 actors · 0 clips · not baked", status
    assert hint == "Next: load an actor entity (Actors tab)", hint
    export_calls = file_calls['TEMPLATE']
    loaded_state = assert_disabled_prop(
        export_calls, "witcher_loaded_w2cutscene_path", loaded_path, "Loaded File",
    )
    burned_event_state = assert_disabled_prop(
        export_calls, "witcher_cutscene_burned_audio_event", burned_event, "Event",
    )
    burned_item_state = assert_disabled_prop(
        export_calls, "witcher_cutscene_burned_audio_item_path", burned_item, "Item",
    )
    assert_exact_value_details(export_calls, loaded_state, "Loaded File", loaded_path)
    assert_exact_value_details(export_calls, burned_event_state, "Event", burned_event)
    assert_exact_value_details(export_calls, burned_item_state, "Item", burned_item)
    timing = ("label", "Imported in 1.25s")
    assert timing in export_calls and export_calls.index(timing) > export_calls.index(loaded_state)
    ui_cutscene._clear_loaded_cutscene_state(scene)

    props_only_mesh = bpy.data.meshes.new("panel_props_only_mesh")
    props_only_obj = bpy.data.objects.new("panel_props_only_object", props_only_mesh)
    scene.collection.objects.link(props_only_obj)
    props_only_obj[cutscene_bake.TRAJECTORY_SLOT_PROP] = "Trajectory08"
    select_only(props_only_obj)
    assert_operator_poll("witcher.cutscene_add_props", True)
    assert_operator_poll("witcher.cutscene_grip_prop", True)
    assert not ui_cutscene._cutscene_cast_actors(scene)
    scene.witcher_cs_tab = 'TEMPLATE'
    props_only_calls = draw_panel(panel_cls)
    assert_action_state(props_only_calls, "witcher.cutscene_bake", True)
    assert_action_state(props_only_calls, "witcher.cutscene_generate_props_entity", True)
    assert ("label", "No actors or props to bake") not in props_only_calls
    assert ("label", "No props assigned (Actors tab › Props)") not in props_only_calls
    bpy.data.objects.remove(props_only_obj, do_unlink=True)
    bpy.data.meshes.remove(props_only_mesh)
    select_only()
    assert_operator_poll("witcher.cutscene_add_props", False)
    assert_operator_poll("witcher.cutscene_grip_prop", False)

    assert bpy.ops.witcher.cutscene_create_new(cutscene_name="probe scene", length=120, fps=25) == {'FINISHED'}
    assert scene.witcher_cutscene_export_repo_path.endswith("\\probe_scene.w2cutscene"), scene.witcher_cutscene_export_repo_path
    assert (scene.frame_start, scene.frame_end, scene.frame_current, scene.render.fps) == (0, 119, 0, 25)
    assert bpy.ops.witcher.cutscene_create_new() == {'FINISHED'}
    assert scene.witcher_cutscene_export_repo_path.endswith("new_cutscene_01.w2cutscene")
    assert (scene.frame_start, scene.frame_end, scene.render.fps, scene.render.fps_base) == (0, 299, 30, 1.0)
    trajectories = ui_cutscene._find_cutscene_actor_armature(scene, "trajectories")
    assert trajectories is not None
    camera = ui_cutscene._find_cutscene_actor_armature(scene, "camera")
    assert camera is not None or not have_camera_entity, "camera actor missing although the entity exists"
    actor_names = [a.actor_name for a in scene.witcher_cutscene_actor_items]
    assert "trajectories" in actor_names, actor_names
    assert_actor_keys(trajectories, camera)
    technical_calls = draw_all_tabs("new/technical", tab_ids, panel_cls)
    technical_labels = [c[1] for c in technical_calls['ACTORS'] if c[0] == "label"]
    assert "trajectories" in technical_labels, technical_labels
    if camera is not None:
        assert "camera" in technical_labels, technical_labels
        assert_default_closed(technical_calls['CAMERA'], "witcher_cs_camera_rig_tools")
    else:
        camera_probe_index = len(scene.witcher_cutscene_actor_items)
        camera_probe = scene.witcher_cutscene_actor_items.add()
        camera_probe.label = camera_probe.actor_name = "camera_probe"
        camera_probe.actor_type = "CAT_Camera"
        scene.witcher_cs_tab = 'ACTORS'
        probe_labels = [c[1] for c in draw_panel(panel_cls) if c[0] == "label"]
        assert "camera_probe" in probe_labels, probe_labels
        scene.witcher_cutscene_actor_items.remove(camera_probe_index)
    if camera is not None:
        bone = camera.pose.bones.get(CAMERA_CONTROL_BONE)
        probe = CAMERA_TRACK_NAMES[0]
        if bone is not None and probe in bone:
            saved = bone[probe]
            del bone[probe]
            select_only(camera)
            scene.witcher_cs_tab = 'CAMERA'
            draw_panel(panel_cls)
            assert probe not in bone, "draw must not write camera track properties"
            bone[probe] = saved

    file_actor_index = len(scene.witcher_cutscene_actor_items)
    file_actor = scene.witcher_cutscene_actor_items.add()
    file_actor.source_index = 77
    file_actor.label = file_actor.actor_name = "technical_file_actor"
    file_actor.actor_type = "CAT_Actor"
    scene.witcher_loaded_w2cutscene_path = r"C:\panel\technical_file.w2cutscene"
    scene.witcher_loaded_cutscene_name = "technical_file"
    scene.witcher_cs_tab = 'ACTORS'
    technical_status, technical_hint = header_lines(draw_panel(panel_cls))
    assert not ui_cutscene._cutscene_cast_actors(scene), technical_status
    assert technical_hint == "Next: load an actor entity (Actors tab)", technical_hint
    scene.witcher_cutscene_actor_items.remove(file_actor_index)
    scene.witcher_loaded_w2cutscene_path = ""
    scene.witcher_loaded_cutscene_name = ""

    camera_copy_rig = camera
    synthetic_camera_rig = camera_copy_rig is None
    if synthetic_camera_rig:
        camera_copy_data = bpy.data.armatures.new("panel_camera_copy_rig_data")
        camera_copy_rig = bpy.data.objects.new("panel_camera_copy_rig", camera_copy_data)
        scene.collection.objects.link(camera_copy_rig)
        camera_copy_rig["cutscene_actor_type"] = "CAT_Camera"
    original_camera_name = camera_copy_rig.name
    camera_copy_rig.name = "panel_camera_rig_exact"
    shot_data = bpy.data.cameras.new("panel_shot_camera_data")
    shot_camera = bpy.data.objects.new("panel_shot_camera_exact", shot_data)
    scene.collection.objects.link(shot_camera)
    shot_camera["witcher_shot_index"] = 7
    shot_marker = scene.timeline_markers.new("panel_shot_marker", frame=12)
    shot_marker.camera = shot_camera
    scene.witcher_cs_tab = 'CAMERA'
    camera_calls = draw_panel(panel_cls)
    rig_state = assert_disabled_prop(camera_calls, "name", "panel_camera_rig_exact")
    shot_state = assert_disabled_prop(camera_calls, "name", "panel_shot_camera_exact")
    assert_exact_value_details(camera_calls, rig_state, "Rig", "panel_camera_rig_exact")
    assert_exact_value_details(camera_calls, shot_state, "Shot", "panel_shot_camera_exact")
    scene.timeline_markers.remove(shot_marker)
    bpy.data.objects.remove(shot_camera, do_unlink=True)
    bpy.data.cameras.remove(shot_data)
    if synthetic_camera_rig:
        bpy.data.objects.remove(camera_copy_rig, do_unlink=True)
        bpy.data.armatures.remove(camera_copy_data)
    else:
        camera_copy_rig.name = original_camera_name
    for idx in range(len(scene.witcher_cutscene_actor_items)):
        scene.witcher_cutscene_loaded_actor_index = idx
        calls = draw_all_tabs(f"new/actor{idx}", tab_ids, panel_cls)
        assert ("list", "WITCH_UL_LoadedActorList") in calls['ACTORS']
        ops = [c[1] for c in calls['ACTORS'] if c[0] == "op"]
        for op in ("witcher.cast_actor", "witcher.cutscene_scratch_assign_actor", "witcher.cutscene_remove_actor_full",
                   "witcher.cutscene_replace_actor", "witcher.cutscene_add_props", "witcher.cutscene_grip_prop"):
            assert op in ops, (op, ops)
        assert "witcher.set_cutscene_actor_loaded" not in ops, ops  # New actors are not restorable from a file
        assert ("prop", "witcher_cutscene_retarget_male_template") not in calls['ACTORS']  # no W2 actor -> no retarget section
        assert ("prop", '["cutscene_actor_tag"]') in calls['ACTORS']
    cam_ops = [c[1] for c in calls['CAMERA'] if c[0] == "op"]
    for op in ("witcher.cutscene_new_shot", "witcher.camera_apply_blender_cameras_to_rig",
               "witcher.camera_convert_cuts_to_blender_cameras", "witcher.camera_cut_split"):
        assert op in cam_ops, (op, cam_ops)
    assert ("witcher.cutscene_scratch_import_camera" in cam_ops) == (camera is None), cam_ops
    assert any(c[1].endswith("0 shots") for c in calls['CAMERA'] if c[0] == "label"), [c for c in calls['CAMERA'] if c[0] == "label"]

    shot_frame_state = (scene.frame_current, scene.frame_end)
    scene.frame_end = 100
    scene.frame_set(10)
    assert bpy.ops.witcher.cutscene_new_shot() == {'FINISHED'}
    scene.frame_set(40)
    assert bpy.ops.witcher.cutscene_new_shot() == {'FINISHED'}
    shots = cutscene_bake.iter_shot_markers(scene)
    assert [s[2] for s in shots] == [10, 40] and all(c.get("witcher_shot_generated") for _i, c, _f in shots), shots
    assert [r[2:] for r in cutscene_bake.shot_ranges(scene)] == [(10, 39), (40, 100)]
    assert cutscene_bake.shots_stale(scene) and cutscene_bake.bake_state(scene)["shots_stale"]
    scene[cutscene_bake.SHOTS_FINGERPRINT_PROP] = cutscene_bake.shots_fingerprint(scene)
    assert not cutscene_bake.shots_stale(scene)
    shots[1][1].location.x += 1.0
    assert cutscene_bake.shots_stale(scene)
    pivot = bpy.data.objects.new("panel_shot_pivot", None)
    aim = bpy.data.objects.new("panel_shot_aim", None)
    for helper in (pivot, aim):
        scene.collection.objects.link(helper)
    shots[0][1].parent = pivot
    track = shots[1][1].constraints.new('TRACK_TO')
    track.target = aim
    scene[cutscene_bake.SHOTS_FINGERPRINT_PROP] = cutscene_bake.shots_fingerprint(scene)
    assert not cutscene_bake.shots_stale(scene)
    pivot.keyframe_insert("location", frame=10)
    assert cutscene_bake.shots_stale(scene), "animated parent must invalidate shots"
    scene[cutscene_bake.SHOTS_FINGERPRINT_PROP] = cutscene_bake.shots_fingerprint(scene)
    aim.location.z += 1.0
    assert cutscene_bake.shots_stale(scene), "constraint target motion must invalidate shots"
    pivot.location.x = 3.0
    pivot.keyframe_insert("location", frame=30)
    scene[cutscene_bake.SHOTS_FINGERPRINT_PROP] = cutscene_bake.shots_fingerprint(scene)
    scene.frame_set(20)
    assert not cutscene_bake.shots_stale(scene), "scrubbing an animated parent is not an edit"
    track.track_axis = 'TRACK_X'
    assert cutscene_bake.shots_stale(scene), "constraint settings must invalidate shots"
    scene.frame_set(40)
    shots[1][1].constraints.remove(track)
    shots[0][1].parent = None
    pivot_action = pivot.animation_data.action
    for helper in (pivot, aim):
        bpy.data.objects.remove(helper, do_unlink=True)
    bpy.data.actions.remove(pivot_action)
    assert bpy.ops.witcher.cutscene_new_shot() == {'FINISHED'}
    scene.witcher_cs_tab = 'CAMERA'
    shot_calls = draw_panel(panel_cls)
    shot_ops = [c[1] for c in shot_calls if c[0] == "op"]
    assert shot_ops.count("witcher.cutscene_jump_to_shot") == 3 and shot_ops.count("witcher.cutscene_remove_shot") == 3, shot_ops
    shot_labels = [c[1] for c in shot_calls if c[0] == "label"]
    assert "10–39" in shot_labels and "40 skipped" in shot_labels, shot_labels
    assert any("3 shots" in l and "Shots → Rig" in l for l in shot_labels), shot_labels
    shot_issues = cutscene_validate.collect_issues(bpy.context)
    assert any(i["tab"] == 'CAMERA' and "skipped" in i["message"] and i["frame"] == 40 for i in shot_issues), shot_issues
    assert any("not on the camera rig" in i["message"] for i in shot_issues), shot_issues
    skipped_index = next(idx for idx, _cam, start, end in cutscene_bake.shot_ranges(scene) if end <= start)
    skipped_name = next(cam.name for idx, cam, _f in cutscene_bake.iter_shot_markers(scene) if idx == skipped_index)
    assert bpy.ops.witcher.cutscene_jump_to_shot(shot_index=shots[0][0]) == {'FINISHED'}
    assert scene.frame_current == 10 and scene.camera == shots[0][1], (scene.frame_current, scene.camera)
    assert bpy.ops.witcher.cutscene_remove_shot(shot_index=skipped_index) == {'FINISHED'}
    assert skipped_name not in bpy.data.objects
    assert [r[2:] for r in cutscene_bake.shot_ranges(scene)] == [(10, 39), (40, 100)]

    rig_data = bpy.data.armatures.new("panel_shot_rig_data")
    rig = bpy.data.objects.new("panel_shot_rig", rig_data)
    scene.collection.objects.link(rig)
    select_only(rig)
    bpy.ops.object.mode_set(mode='EDIT')
    rig_bone = rig_data.edit_bones.new(CAMERA_CONTROL_BONE)
    rig_bone.head, rig_bone.tail = (0.0, 0.0, 0.0), (0.0, 0.0, 1.0)
    bpy.ops.object.mode_set(mode='OBJECT')
    rig["cutscene_actor_name"] = "camera_shot_probe"
    rig["cutscene_actor_type"] = "CAT_Camera"
    actions_before = {a.as_pointer() for a in bpy.data.actions}
    assert bpy.ops.witcher.camera_apply_blender_cameras_to_rig() == {'FINISHED'}
    assert not cutscene_bake.shots_stale(scene)
    assert not any(m.name.startswith("W3 Cam") for m in scene.timeline_markers), [m.name for m in scene.timeline_markers]
    rig_cuts = ui_anims._iter_camera_cut_strips(rig)
    assert [(t.name, int(s.frame_start), int(s.frame_end)) for t, s in rig_cuts] == [
        ("cutscene_anim_camera", 10, 39), ("cutscene_anim_camera", 40, 100),
    ], [(t.name, s.frame_start, s.frame_end) for t, s in rig_cuts]
    shown_arm = ui_cutscene._cs_find_camera_armature(bpy.context)
    rig_labels = [c[1] for c in draw_panel(panel_cls) if c[0] == "label"]
    shown_cuts = len(ui_anims._iter_camera_cut_strips(shown_arm))
    assert any(l.startswith(f"{shown_cuts} cut") and l.endswith("2 shots") for l in rig_labels), rig_labels
    old_shot_cams = [cam for _i, cam, _f in cutscene_bake.iter_shot_markers(scene)]
    select_only(rig)
    assert bpy.ops.witcher.camera_convert_cuts_to_blender_cameras() == {'FINISHED'}
    converted = cutscene_bake.iter_shot_markers(scene)
    assert [s[2] for s in converted] == [10, 40], converted
    assert all(cam not in old_shot_cams and cam.get("witcher_shot_generated") for _i, cam, _f in converted), converted
    assert all(cam.name in bpy.data.objects for cam in old_shot_cams)
    assert cutscene_bake.shots_stale(scene)
    for idx, _cam, _f in list(cutscene_bake.iter_shot_markers(scene)):
        assert bpy.ops.witcher.cutscene_remove_shot(shot_index=idx) == {'FINISHED'}
    for cam in old_shot_cams:
        data = cam.data
        bpy.data.objects.remove(cam, do_unlink=True)
        bpy.data.cameras.remove(data)
    assert not cutscene_bake.iter_shot_markers(scene) and not cutscene_bake.shots_stale(scene)
    bpy.data.objects.remove(rig, do_unlink=True)
    bpy.data.armatures.remove(rig_data)
    for action in [a for a in bpy.data.actions if a.as_pointer() not in actions_before and a.users == 0]:
        bpy.data.actions.remove(action)
    del scene[cutscene_bake.SHOTS_FINGERPRINT_PROP]
    ui_cutscene._sync_actor_items_with_scene(scene)
    ui_cutscene.sync_animation_items_from_scene(scene)
    scene.frame_end = shot_frame_state[1]
    scene.frame_set(shot_frame_state[0])

    status, hint = header_lines(calls['ACTORS'])
    assert status == "0 actors · 0 clips · not baked", status
    assert len(ui_cutscene._cutscene_cast_actors(scene)) == len(ui_cutscene._clip_groups(scene)) == 0
    assert hint == "Next: add an actor (Actors tab)", hint

    arm, action = make_scratch_actor(scene)
    assert_shots_to_rig_rollback(scene, action, ui_anims, ui_cutscene)
    select_only(arm)
    scene.witcher_cutscene_scratch_actor_name = "scratch"
    scene.witcher_cutscene_scratch_actor_template = "characters\\npc_entities\\test\\scratch.w2ent"
    scene.witcher_cutscene_scratch_use_mimic = False
    select_only(arm)
    assert_operator_poll("witcher.cutscene_add_props", False)
    assert_operator_poll("witcher.cutscene_grip_prop", False)
    scene.witcher_cs_tab = 'ACTORS'
    assert_action_state(draw_panel(panel_cls), "witcher.cutscene_scratch_assign_actor", True)
    assert bpy.ops.witcher.cutscene_scratch_assign_actor() == {'FINISHED'}
    scratch_name = str(arm.get("cutscene_actor_name", ""))
    assert scratch_name, "assign did not tag the armature"
    assert_actor_keys(arm)
    scene.witcher_cs_tab = 'ACTORS'
    assert header_lines(draw_panel(panel_cls))[1] == "Next: add a clip (Clips tab)"

    technical_clips = [
        make_technical_clip(scene, "camera", action),
        make_technical_clip(scene, "trajectories", action),
        make_technical_clip(scene, "props_probe", action, prop=True),
    ]
    assert ui_cutscene._clip_groups(scene), "technical strips should exercise the clip-group path"
    assert not ui_cutscene._cutscene_clip_strips(scene)
    scene.witcher_cs_tab = 'TEMPLATE'
    technical_status, technical_hint = header_lines(draw_panel(panel_cls))
    assert technical_status == "1 actor · 0 clips · not baked", technical_status
    assert technical_hint == "Next: add a clip (Clips tab)", technical_hint
    for technical_arm, technical_action in technical_clips:
        technical_data = technical_arm.data
        bpy.data.objects.remove(technical_arm, do_unlink=True)
        bpy.data.armatures.remove(technical_data)
        bpy.data.actions.remove(technical_action)

    ui_cutscene._sync_actor_items_with_scene(scene)
    actor_names = [a.actor_name for a in scene.witcher_cutscene_actor_items]
    assert scratch_name in actor_names, actor_names
    scene.witcher_cutscene_loaded_actor_index = actor_names.index(scratch_name)
    scene.witcher_cs_tab = 'ACTORS'
    assert_default_closed(draw_panel(panel_cls), "witcher_cs_actor_properties")

    scene.witcher_cutscene_scratch_action_name = action.name
    scene.witcher_cutscene_scratch_component = "Root"
    arm.animation_data.action = None
    import_track = arm.animation_data.nla_tracks.new()
    import_track.name = "anim_import"
    import_strip = import_track.strips.new(action.name, 1, action)
    import_track.is_solo = True
    arm.animation_data.action = action
    select_only(arm)
    scene.witcher_cs_tab = 'ANIMS'
    ready_add_calls = draw_panel(panel_cls)
    assert_action_state(ready_add_calls, "witcher.cutscene_browse_add_animation", True)
    assert_action_state(ready_add_calls, "witcher.cutscene_scratch_add_action", True)
    assert bpy.ops.witcher.cutscene_scratch_add_action() == {'FINISHED'}
    tracks = {t.name: t for t in arm.animation_data.nla_tracks}
    assert "cutscene_anim" in tracks and len(tracks["cutscene_anim"].strips) == 1, list(tracks)
    assert arm.animation_data.action is None
    assert import_track.mute and import_strip.mute and not import_track.is_solo
    assert tracks["cutscene_anim"].strips[0].action != action

    fallback_action = action.copy()
    fallback_action.name = "scratch_fallback"
    fallback_source = import_track.strips.new(fallback_action.name, 30, fallback_action)
    candidates = ui_cutscene._collect_cutscene_import_nla_candidates(scene)
    assert any(candidate["strip_name"] == fallback_source.name for candidate in candidates)
    assert bpy.ops.witcher.cutscene_use_import_nla_strip(
        actor_object_name=arm.name,
        source_object_name=arm.name,
        track_name=import_track.name,
        strip_name=fallback_source.name,
        source_frame_start=fallback_source.frame_start,
        component="Root",
    ) == {'FINISHED'}
    promoted = [strip for strip in tracks["cutscene_anim"].strips if strip.action != tracks["cutscene_anim"].strips[0].action]
    assert len(promoted) == 1 and promoted[0].action != fallback_action
    assert import_track.mute and fallback_source.mute
    promoted_action = promoted[0].action
    promoted_id = int(promoted_action["witcher_cutscene_source_index"])
    tracks["cutscene_anim"].strips.remove(promoted[0])
    import_track.strips.remove(fallback_source)
    for index in reversed(range(len(scene.witcher_cutscene_animation_items))):
        if int(scene.witcher_cutscene_animation_items[index].source_index) == promoted_id:
            scene.witcher_cutscene_animation_items.remove(index)
    bpy.data.actions.remove(promoted_action)
    bpy.data.actions.remove(fallback_action)

    anims = list(scene.witcher_cutscene_animation_items)
    clip_rows = [i for i, a in enumerate(anims) if a.source_index != -1 and a.actor_name.lower() == scratch_name.lower()]
    assert clip_rows, [(a.actor_name, a.source_index) for a in anims]

    ui_cutscene.sync_animation_items_from_scene(scene)
    anims = list(scene.witcher_cutscene_animation_items)
    clip_rows = [i for i, a in enumerate(anims) if a.actor_name.lower() == scratch_name.lower()]
    clip_id = int(anims[clip_rows[0]].source_index)
    assert clip_id >= ui_cutscene.AUTHORED_CLIP_ID_BASE, clip_id
    assert not anims[clip_rows[0]].file_backed
    assert scene.witcher_cutscene_animation_items[clip_rows[0]].is_loaded
    assert not scene.witcher_cutscene_animation_items[clip_rows[0]].muted

    clip_group = ui_cutscene._clip_groups(scene)[clip_id]
    base_strip = clip_group["strips"][0][1]
    multipart_start = int(base_strip.frame_end) + 10
    multipart = tracks["cutscene_anim"].strips.new("multipart_same_id", multipart_start, base_strip.action)
    assert multipart.frame_start >= base_strip.frame_end, (base_strip.frame_end, multipart.frame_start)
    ui_cutscene.sync_animation_items_from_scene(scene)
    multipart_groups = ui_cutscene._clip_groups(scene)
    assert len(multipart_groups) == 1 and len(multipart_groups[clip_id]["strips"]) == 2, multipart_groups
    scene.witcher_cs_tab = 'ANIMS'
    multipart_status, _multipart_hint = header_lines(draw_panel(panel_cls))
    assert "1 clip" in multipart_status and "2 clips" not in multipart_status, multipart_status
    tracks["cutscene_anim"].strips.remove(multipart)
    ui_cutscene.sync_animation_items_from_scene(scene)
    restored_groups = ui_cutscene._clip_groups(scene)
    assert len(restored_groups) == 1 and len(restored_groups[clip_id]["strips"]) == 1, restored_groups

    mixed_technical = [
        make_technical_clip(scene, "camera", action),
        make_technical_clip(scene, "trajectories", action),
        make_technical_clip(scene, "props_probe", action, prop=True),
    ]
    mixed_status, mixed_hint = header_lines(draw_panel(panel_cls))
    assert mixed_status == "1 actor · 1 clip · not baked", mixed_status
    assert mixed_hint == "Next: Bake actors (Export tab)", mixed_hint
    for technical_arm, technical_action in mixed_technical:
        technical_data = technical_arm.data
        bpy.data.objects.remove(technical_arm, do_unlink=True)
        bpy.data.armatures.remove(technical_data)
        bpy.data.actions.remove(technical_action)

    assert bpy.ops.witcher.cutscene_set_clip_muted(source_index=clip_id, mute=True) == {'FINISHED'}
    assert tracks["cutscene_anim"].strips[0].mute and scene.witcher_cutscene_animation_items[clip_rows[0]].muted
    assert bpy.ops.witcher.cutscene_set_clip_muted(source_index=clip_id, mute=False) == {'FINISHED'}
    assert not tracks["cutscene_anim"].strips[0].mute
    manual = tracks["cutscene_anim"].strips.new("manual_clip", 60, action)  # untagged action -> composed key
    ui_cutscene.sync_animation_items_from_scene(scene)
    names = [a.full_name for a in scene.witcher_cutscene_animation_items]
    assert f"{scratch_name}:Root:manual_clip" in names, names
    tracks["cutscene_anim"].strips.remove(manual)
    ui_cutscene.sync_animation_items_from_scene(scene)
    assert f"{scratch_name}:Root:manual_clip" not in [a.full_name for a in scene.witcher_cutscene_animation_items]
    assert not hasattr(bpy.types, "WITCHER_OT_cutscene_find_anims") and not hasattr(bpy.types.Scene, "witcher_cs_anim_query")
    assert not hasattr(bpy.types, "WITCHER_OT_cutscene_open_quick_browser")
    assert hasattr(bpy.types, "WITCHER_OT_cutscene_browse_add_animation")
    assert hasattr(bpy.types.WindowManager, "witcher_cutscene_browse_animation")
    select_only(arm)
    browse_rna = bpy.ops.witcher.cutscene_browse_add_animation.get_rna_type().properties
    for prop_name in ("actor_object_name", "animation_id", "source_path", "source_game", "component", "placement"):
        assert prop_name in browse_rna and browse_rna[prop_name].is_skip_save, prop_name

    bpy.ops.witcher.cutscene_add_event('EXEC_DEFAULT', event_scope="ROOT", source_index=-1,
                                        event_name="root_ev", start_time=0.5)
    bpy.ops.witcher.cutscene_add_event('EXEC_DEFAULT', event_scope="ENTRY",
                                        source_index=clip_id, event_name="entry_ev",
                                        start_time=0.25, duration=0.5)
    scene.witcher_cutscene_event_index = 0
    scene.witcher_cs_entry_event_idx = 1

    assert not [a for a in scene.witcher_cutscene_animation_items if a.source_index == -1], "no Cutscene sentinel row"
    scene.witcher_cs_event_target = "ROOT"
    scene.witcher_cs_tab = 'EVENTS'
    calls = draw_panel(panel_cls)
    assert ("list", "WITCH_UL_RootEventList") in calls and ("prop", "start_time") in calls, calls
    assert ("op", "witcher.cutscene_event_partial_info") in calls, calls  # FadeEvent: in/color are not stored
    assert_action_state(
        calls, "witcher.cutscene_add_event", True, text="Cutscene", check_reason=False,
    )
    assert_action_state(
        calls, "witcher.cutscene_add_event", False,
        "Pick an animation above to add animation events", text="Animation",
    )
    scene.witcher_cs_event_target = str(clip_id)
    calls = draw_panel(panel_cls)
    assert ("list", "WITCH_UL_EntryEventList") in calls and ("prop", "start_time") in calls, calls
    assert_action_state(
        calls, "witcher.cutscene_add_event", True, text="Cutscene", check_reason=False,
    )
    assert_action_state(calls, "witcher.cutscene_add_event", True, text="Animation")
    assert not hasattr(bpy.types.Scene, "witcher_cs_show_event_schema")

    retained_target_items = ui_cutscene._event_target_items(None, bpy.context)
    probe_ids = [clip_id + offset for offset in range(101, 108)]
    for probe_id in probe_ids:
        probe_row = scene.witcher_cutscene_animation_items.add()
        probe_row.source_index = probe_id
        probe_row.full_name = f"probe:Root:{probe_id}"
        probe_row.display_name = f"probe_{probe_id}"
        probe_row.actor_name = "probe"
        probe_row.component_name = "Root"
    scene.witcher_cs_event_target = str(clip_id)
    scene.witcher_cutscene_animation_items.move(len(scene.witcher_cutscene_animation_items) - 1, 0)
    assert scene.witcher_cs_event_target == str(clip_id), scene.witcher_cs_event_target
    expanded_target_items = ui_cutscene._event_target_items(None, bpy.context)
    target_values = {item[0]: item[4] for item in expanded_target_items}
    assert target_values["ROOT"] == 0 and target_values[str(clip_id)] == clip_id + 1, target_values
    assert any(items is retained_target_items for items in ui_cutscene._EVENT_TARGET_ITEM_HISTORY)
    history_size = len(ui_cutscene._EVENT_TARGET_ITEM_HISTORY)
    assert ui_cutscene._event_target_items(None, bpy.context) is expanded_target_items
    assert len(ui_cutscene._EVENT_TARGET_ITEM_HISTORY) == history_size

    original_clip_groups = ui_cutscene._clip_groups
    clip_group_calls = [0]

    def counted_clip_groups(target_scene):
        clip_group_calls[0] += 1
        return original_clip_groups(target_scene)

    ui_cutscene._clip_groups = counted_clip_groups
    try:
        scene.witcher_cs_tab = 'ANIMS'
        draw_panel(panel_cls)
    finally:
        ui_cutscene._clip_groups = original_clip_groups
    assert clip_group_calls == [1], clip_group_calls

    for probe_id in probe_ids:
        ui_cutscene._remove_cutscene_animation_entry(scene, probe_id, remove_strips=False)
        assert scene.witcher_cs_event_target == str(clip_id), scene.witcher_cs_event_target
    removal_probe = clip_id + 999
    probe_row = scene.witcher_cutscene_animation_items.add()
    probe_row.source_index = removal_probe
    probe_row.full_name = f"probe:Root:{removal_probe}"
    scene.witcher_cs_event_target = str(removal_probe)
    ui_cutscene._remove_cutscene_animation_entry(scene, removal_probe, remove_strips=False)
    assert scene.witcher_cs_event_target == "ROOT", scene.witcher_cs_event_target

    clear_scene = bpy.data.scenes.new("panel_event_target_clear")
    bpy.context.window.scene = clear_scene
    clear_row = clear_scene.witcher_cutscene_animation_items.add()
    clear_row.source_index = 7
    clear_row.full_name = "old:Root:clip"
    clear_scene.witcher_cs_event_target = "7"
    assert clear_scene.witcher_cs_event_target == "7"
    ui_cutscene._clear_loaded_cutscene_state(clear_scene)
    assert clear_scene.witcher_cs_event_target == "ROOT"
    replacement_row = clear_scene.witcher_cutscene_animation_items.add()
    replacement_row.source_index = 7
    replacement_row.full_name = "new:Root:unrelated"
    assert clear_scene.witcher_cs_event_target == "ROOT", "cleared numeric value rebound to the replacement row"
    bpy.context.window.scene = scene
    bpy.data.scenes.remove(clear_scene)

    scene.witcher_cs_event_target = "ROOT"
    clip_row = next(i for i, item in enumerate(scene.witcher_cutscene_animation_items)
                    if int(item.source_index) == clip_id)
    scene.witcher_cutscene_loaded_anim_index = clip_row
    calls = draw_all_tabs("scratch/clip", tab_ids, panel_cls)
    assert ("list", "WITCH_UL_LoadedAnimList") in calls['ANIMS']
    assert ("op", "witcher.cutscene_scratch_add_action") in calls['ANIMS']
    assert ("op", "witcher.cutscene_browse_add_animation") in calls['ANIMS']
    assert ("op", "witcher.cutscene_open_quick_browser") not in calls['ANIMS']
    assert_action_state(calls['ANIMS'], "witcher.cutscene_browse_add_animation", True)
    assert_action_state(calls['ANIMS'], "witcher.cutscene_scratch_add_action", True)
    assert_action_state(calls['TEMPLATE'], "witcher.cutscene_bake", True)
    clip_actor_state = assert_disabled_prop(calls['ANIMS'], "actor_name", scratch_name, "Actor")
    clip_component_state = assert_disabled_prop(calls['ANIMS'], "component_name", "Root", "Component")
    assert_exact_value_details(calls['ANIMS'], clip_actor_state, "Actor", scratch_name)
    assert_exact_value_details(calls['ANIMS'], clip_component_state, "Component", "Root")
    labels = [c[1] for c in calls['ANIMS'] if c[0] == "label"]
    assert "Use loaded strips (fallback)" in labels, labels
    assert not any("Loaded clips sit on anim_import" in text for text in labels), labels
    status, hint = header_lines(calls['ANIMS'])
    assert status == "1 actor · 1 clip · not baked" and hint == "Next: Bake actors (Export tab)", (status, hint)
    assert len(ui_cutscene._cutscene_cast_actors(scene)) == len(ui_cutscene._clip_groups(scene)) == 1
    for idx in range(len(scene.witcher_cutscene_actor_items)):
        scene.witcher_cutscene_loaded_actor_index = idx
        draw_all_tabs(f"scratch/actor{idx}", tab_ids, panel_cls)

    scratch_actor_index = next(
        i for i, item in enumerate(scene.witcher_cutscene_actor_items)
        if item.actor_name == scratch_name
    )
    scene.witcher_cutscene_loaded_actor_index = scratch_actor_index
    prop_mesh = bpy.data.meshes.new("panel_prop_mesh")
    prop_obj = bpy.data.objects.new("panel_prop_object_exact", prop_mesh)
    scene.collection.objects.link(prop_obj)
    prop_obj[cutscene_bake.TRAJECTORY_SLOT_PROP] = "Trajectory07"
    select_only(prop_obj)
    assert_operator_poll("witcher.cutscene_add_props", True)
    assert_operator_poll("witcher.cutscene_grip_prop", True)
    scene.witcher_cs_tab = 'ACTORS'
    prop_calls = draw_panel(panel_cls)
    assert_action_state(prop_calls, "witcher.cutscene_add_props", True)
    assert_action_state(prop_calls, "witcher.cutscene_grip_prop", True)
    prop_slot = assert_disabled_prop(
        prop_calls, f'["{cutscene_bake.TRAJECTORY_SLOT_PROP}"]', "Trajectory07",
    )
    prop_object = assert_disabled_prop(prop_calls, "name", "panel_prop_object_exact")
    prop_remove = assert_action_state(
        prop_calls, "witcher.cutscene_remove_prop", True, check_reason=False,
    )
    assert prop_slot[8] == prop_object[8] == prop_remove[7], (prop_slot, prop_object, prop_remove)
    assert_exact_value_details(prop_calls, prop_slot, "Slot", "Trajectory07")
    assert_exact_value_details(prop_calls, prop_object, "Object", "panel_prop_object_exact")
    scene.witcher_cs_tab = 'TEMPLATE'
    props_export_calls = draw_panel(panel_cls)
    assert_action_state(props_export_calls, "witcher.cutscene_generate_props_entity", True)
    assert ("label", "No props assigned (Actors tab › Props)") not in props_export_calls
    bpy.data.objects.remove(prop_obj, do_unlink=True)
    bpy.data.meshes.remove(prop_mesh)
    select_only(arm)

    baked = cutscene_bake.bake_cutscene_actors(bpy.context)
    assert baked and cutscene_bake.bake_state(scene)["baked"], [a.name for a, _ in baked]
    stored_fingerprint = scene[cutscene_bake.BAKE_FINGERPRINT_PROP]
    del scene[cutscene_bake.BAKE_FINGERPRINT_PROP]
    legacy_state = cutscene_bake.bake_state(scene)
    assert legacy_state["baked"] and legacy_state["stale"], legacy_state
    scene[cutscene_bake.BAKE_FINGERPRINT_PROP] = stored_fingerprint
    ui_cutscene.sync_animation_items_from_scene(scene)
    baked_row = next(a for a in scene.witcher_cutscene_animation_items if int(a.source_index) == clip_id)
    assert baked_row.is_loaded and not baked_row.muted, "sources stashed by bake still count as loaded clips"
    assert baked_row.has_prebake and baked_row.track_muted
    calls = draw_all_tabs("baked", tab_ids, panel_cls)
    anim_labels = [c[1] for c in calls['ANIMS'] if c[0] == "label"]
    assert "Pre-bake source · read-only" in anim_labels, anim_labels
    assert "Track muted" in anim_labels, anim_labels
    mutation_ops = {"witcher.cutscene_set_clip_muted", "witcher.cutscene_remove_animation"}
    assert not mutation_ops.intersection(c[1] for c in calls['ANIMS'] if c[0] == "op"), calls['ANIMS']
    status, hint = header_lines(calls['TEMPLATE'])
    assert status == "1 actor · 1 clip · baked", status
    assert hint == "Ready to export (Export tab)" or hint.startswith("Next: fix "), hint
    bake_description = bpy.ops.witcher.export_w2_cutscene.get_rna_type().properties["bake_before_export"].description
    assert "every export" in bake_description and "current baked output" in bake_description, bake_description
    export_ops = [c[1] for c in calls['TEMPLATE'] if c[0] == "op"]
    for op in ("witcher.cutscene_bake", "witcher.cutscene_validate", "witcher.export_w2_cutscene",
               "witcher.cutscene_export_scene_wrapper"):
        assert op in export_ops, export_ops
    assert ("prop", "witcher_cutscene_export_repo_path") in calls['TEMPLATE']
    assert ("prop", "witcher_cutscene_dialog_id_space") in calls['TEMPLATE']

    src_track = next(t for t in arm.animation_data.nla_tracks if t.name.startswith("cutscene_anim")
                     and not any(s.action is not None and s.action.get(cutscene_bake.BAKED_ACTION_TAG) for s in t.strips))
    src_track.strips[0].frame_end += 1.0
    assert cutscene_bake.bake_state(scene)["stale"]
    scene.witcher_cs_tab = 'TEMPLATE'
    status, hint = header_lines(draw_panel(panel_cls))
    assert status == "1 actor · 1 clip · stale" and hint == "Next: Export re-bakes when enabled", (status, hint)
    assert not hasattr(bpy.types, "WITCHER_OT_cutscene_scratch_validate")
    assert not hasattr(bpy.types.Scene, "witcher_cutscene_scratch_validation_report")
    try:
        bpy.ops.witcher.cutscene_validate()
    except RuntimeError as exc:
        assert "validation error(s)" in str(exc), exc
    report = scene.witcher_cutscene_validation_report
    assert report and all(l.startswith(("ERROR", "WARN", "OK")) for l in report.splitlines()), report
    assert any("partial fields" in l for l in report.splitlines()), report
    issues = scene.witcher_cutscene_validation_issues
    assert [f"{i.severity} {i.message}" for i in issues] == [l for l in report.splitlines() if not l.startswith("OK")], report
    calls = draw_panel(panel_cls)
    ops = [c[1] for c in calls if c[0] == "op"]
    assert "witcher.cutscene_validation_report" in ops and "witcher.cutscene_set_scene_range" in ops, ops
    assert ops.count("witcher.cutscene_validate") == 1, ops
    assert ("list", "WITCH_UL_CutsceneValidationIssues") in calls, calls
    assert ops.count("witcher.cutscene_validation_goto") == len(issues) <= ops.count("witcher.cutscene_exact_value_details"), ops
    target = next((i for i in issues if i.object_name), None) or next(i for i in issues if i.tab)
    scene.witcher_cs_tab = 'ACTORS' if target.tab != 'ACTORS' else 'TEMPLATE'
    assert bpy.ops.witcher.cutscene_validation_goto(
        tab=target.tab, object_name=target.object_name, frame=target.frame, line=target.line,
    ) == {'FINISHED'}
    assert scene.witcher_cs_tab == target.tab, (scene.witcher_cs_tab, target.tab)
    if target.object_name:
        assert bpy.context.view_layer.objects.active.name == target.object_name, bpy.context.view_layer.objects.active
    scene.witcher_cs_tab = 'TEMPLATE'

    bpy.data.objects.remove(arm, do_unlink=True)
    assert ui_cutscene._scene_needs_actor_sync(scene)
    ui_cutscene._sync_actor_items_with_scene(scene)
    assert scratch_name not in [a.actor_name for a in scene.witcher_cutscene_actor_items]
    assert not ui_cutscene._scene_needs_actor_sync(scene)
    ui_cutscene.sync_animation_items_from_scene(scene)
    assert clip_id not in [int(a.source_index) for a in scene.witcher_cutscene_animation_items]
    draw_all_tabs("actor-removed", tab_ids, panel_cls)

    print("W3TB_CUTSCENE_PANEL_NATIVE_OK")
except Exception:
    traceback.print_exc()
    print("W3TB_CUTSCENE_PANEL_NATIVE_FAIL")
    sys.exit(1)
finally:
    addon.unregister()
