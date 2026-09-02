"""Bake Blender cutscene timelines into engine-ready actor actions."""
from __future__ import annotations

import hashlib
import json
import os

import bpy
from mathutils import Matrix

CUTSCENE_TRACK_NAME = "cutscene_anim"
BAKE_TRACK_NAME = "cutscene_anim_baked"
BAKE_BACKUP_SUFFIX = "_prebake"
BAKED_ACTION_TAG = "cutscene_bake_output"
BAKED_SOURCE_CLIP_IDS_PROP = "cutscene_bake_source_clip_ids"
BAKED_SOURCE_CLIP_STARTS_PROP = "cutscene_bake_source_clip_starts"
CUTSCENE_SOURCE_INDEX_PROP = "witcher_cutscene_source_index"
PREBAKE_STATE_PROP = "cutscene_prebake_state"
BAKE_FINGERPRINT_PROP = "witcher_cutscene_bake_fp"
SHOTS_FINGERPRINT_PROP = "witcher_cutscene_shots_fp"
_TRANSFORM_PATHS = ("location", "rotation_euler", "rotation_quaternion", "rotation_axis_angle", "scale",
                    "delta_location", "delta_rotation_euler", "delta_rotation_quaternion", "delta_scale")
_CAMERA_PATHS = ("lens", "lens_unit", "sensor_fit", "sensor_width", "sensor_height", "shift_x", "shift_y",
                 "dof.use_dof", "dof.focus_distance", "dof.aperture_fstop")
_CONSTRAINT_UI_PROPS = {"rna_type", "name", "active", "show_expanded", "is_valid", "is_override_data",
                        "error_location", "error_rotation"}
SCALE_TOL = 1e-4

SCAFFOLD_ACTORS = {"trajectories"}
IMPORT_TRACK_NAMES = {"anim_import", "mimic_import"}
_ACTIVE_BAKE_TRANSACTIONS = {}


def iter_cutscene_actor_armatures(scene):
    for obj in scene.objects:
        if getattr(obj, "type", None) == 'ARMATURE' and str(obj.get("cutscene_actor_name", "") or "").strip():
            yield obj


def _is_backup_track(track):
    return BAKE_BACKUP_SUFFIX in track.name


def _active_cutscene_strips(armature):
    ad = armature.animation_data
    tracks = [
        t for t in (ad.nla_tracks if ad else [])
        if t.name.startswith(CUTSCENE_TRACK_NAME) and not _is_backup_track(t) and not t.mute
    ]
    return [s for t in tracks for s in t.strips if not s.mute]


def bake_inputs(scene):
    strips, lanes, foreign = 0, set(), []
    for armature in iter_cutscene_actor_armatures(scene):
        if armature.get(PROP_RIG_TAG):
            continue
        label = str(armature.get("cutscene_actor_name", "") or armature.name)
        ad = armature.animation_data
        for track in (ad.nla_tracks if ad else []):
            if track.mute or _is_backup_track(track) or track.name in IMPORT_TRACK_NAMES:
                continue
            live = [s for s in track.strips if not s.mute and s.action is not None and not s.action.get(BAKED_ACTION_TAG)]
            if not live:
                continue
            if track.name.startswith(CUTSCENE_TRACK_NAME):
                strips += len(live)
                lanes.add(track.name)
            else:
                foreign.append((label, track.name, armature.name))
    return {"strips": strips, "lanes": sorted(lanes), "foreign": foreign}


def _active_cutscene_source_starts(armature):
    source_starts = {}
    anim_data = getattr(armature, "animation_data", None)
    for track in (anim_data.nla_tracks if anim_data else []):
        if not track.name.startswith(CUTSCENE_TRACK_NAME) or track.mute:
            continue
        for strip in track.strips:
            action = getattr(strip, "action", None)
            if strip.mute or action is None or action.get(BAKED_ACTION_TAG):
                continue
            try:
                source_index = int(action.get(CUTSCENE_SOURCE_INDEX_PROP, -1))
            except (TypeError, ValueError):
                source_index = -1
            if source_index >= 0:
                strip_start = float(getattr(strip, "frame_start", 0.0) or 0.0)
                source_starts[source_index] = min(source_starts.get(source_index, strip_start), strip_start)
    return source_starts


def _active_cutscene_source_ids(armature):
    return list(_active_cutscene_source_starts(armature))


def effective_frame_range(scene):
    """Include active cutscene strips in the scene range."""
    start, end = int(scene.frame_start), int(scene.frame_end)
    for armature in iter_cutscene_actor_armatures(scene):
        ad = armature.animation_data
        for track in (ad.nla_tracks if ad else []):
            if track.name.startswith(CUTSCENE_TRACK_NAME) and not track.mute:
                for strip in track.strips:
                    if not strip.mute:
                        start = min(start, int(strip.frame_start))
                        end = max(end, int(strip.frame_end))
    return start, end


def _object_chain(obj):
    chain = [obj]
    while chain[-1].parent is not None:
        chain.append(chain[-1].parent)
    return chain


def _is_identity(matrix, tol=1e-4):
    return all(abs(a - b) <= tol for row, ref in zip(matrix, Matrix.Identity(4)) for a, b in zip(row, ref))


def _non_unit_scale(matrix):
    return any(abs(s - 1.0) > SCALE_TOL for s in matrix.to_scale())


def _has_animation(obj):
    ad = obj.animation_data
    return bool(ad and (ad.action or ad.nla_tracks or ad.drivers))


def _iter_actions(obj):
    ad = obj.animation_data
    if not ad:
        return
    if ad.action:
        yield ad.action
    for track in ad.nla_tracks:
        if track.mute:
            continue
        for strip in track.strips:
            if strip.action and not strip.mute:
                yield strip.action


def _action_fcurves(action):
    if hasattr(action, "fcurves"):
        return list(action.fcurves)
    return [fc for layer in action.layers for strip in layer.strips
            for bag in strip.channelbags for fc in bag.fcurves]


def _scale_issues(context, armature, label):
    """Scale never reaches the engine (export writes 1,1,1), so it must be fixed, not silently dropped."""
    issues = []
    ev = armature.evaluated_get(context.evaluated_depsgraph_get())
    scaled = [pb.name for pb in ev.pose.bones
              if _non_unit_scale((pb.parent.matrix.inverted() @ pb.matrix) if pb.parent else pb.matrix)]
    if scaled:
        issues.append(
            f"{label}: {len(scaled)} bone(s) carry non-unit scale at frame {context.scene.frame_current} "
            f"({', '.join(scaled[:4])}) — scale is not exported; clear bone scale and scale constraints"
        )
    for holder in _object_chain(armature):
        if _non_unit_scale(holder.matrix_basis):
            issues.append(
                f"{label}: object '{holder.name}' has non-unit scale — the engine ignores scale; "
                f"apply or reset it before baking"
            )
        for action in _iter_actions(holder):
            for fc in _action_fcurves(action):
                path = fc.data_path
                if (path == "scale" or path.endswith(".scale")) and any(
                        abs(k.co[1] - 1.0) > SCALE_TOL for k in fc.keyframe_points):
                    issues.append(
                        f"{label}: '{path}' on '{holder.name}' is keyed away from 1.0 — "
                        f"scale is not exported; remove the scale keys"
                    )
                    break
    return issues


def _needs_bake(armature):
    chain = _object_chain(armature)
    return (
        any(_has_animation(o) or any(c.enabled for c in o.constraints) for o in chain)
        or not _is_identity(armature.matrix_world)
        or any(c.enabled for pb in armature.pose.bones for c in pb.constraints)
    )


def _bone_custom_props(armature):
    """(bone, prop) pairs for numeric custom props: camera tracks (Camera_Node) and face pose weights."""
    out = []
    for pb in armature.pose.bones:
        for key in pb.keys():
            value = pb[key]
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                out.append((pb.name, key))
    return out


def _sample_actor(context, armature, frames):
    """Per-frame basis matrices per bone (world motion folded into roots) plus bone custom-prop values."""
    deps = context.evaluated_depsgraph_get()
    scene = context.scene

    pose_bones = list(armature.pose.bones)
    names = [pb.name for pb in pose_bones]
    parents = {pb.name: (pb.parent.name if pb.parent else None) for pb in pose_bones}
    rest_local = {pb.name: pb.bone.matrix_local.copy() for pb in pose_bones}
    rest_rel = {}
    for pb in pose_bones:
        if pb.parent is not None:
            rest_rel[pb.name] = rest_local[pb.parent.name].inverted() @ rest_local[pb.name]
    custom_keys = _bone_custom_props(armature)

    samples = {name: [] for name in names}
    custom = {key: [] for key in custom_keys}
    scaled = set()
    for frame in frames:
        scene.frame_set(frame)
        deps.update()
        ev = armature.evaluated_get(deps)
        obj_world = ev.matrix_world.copy()
        pose_arm = {pb.name: pb.matrix.copy() for pb in ev.pose.bones}
        for name in names:
            parent = parents[name]
            if parent is None:
                # fold world motion in: armature-space pose becomes world pose
                basis = rest_local[name].inverted() @ (obj_world @ pose_arm[name])
            else:
                pose_rel = pose_arm[parent].inverted() @ pose_arm[name]
                basis = rest_rel[name].inverted() @ pose_rel
            samples[name].append(basis)
            if _non_unit_scale(basis):
                scaled.add(name)
        for bone, prop in custom_keys:
            custom[(bone, prop)].append(float(ev.pose.bones[bone].get(prop, 0.0)))
    if scaled:
        label = str(armature.get("cutscene_actor_name", "") or armature.name)
        raise RuntimeError(
            f"Cannot bake {label}: non-unit bone scale sampled on {', '.join(sorted(scaled)[:6])} — "
            f"scale is not exported; clear bone scale and scale constraints"
        )
    return samples, custom


def _isolate_import_tracks(armatures):
    states = []
    for armature in armatures:
        anim_data = getattr(armature, "animation_data", None)
        has_cutscene_source = any(
            not track.mute
            and track.name.startswith(CUTSCENE_TRACK_NAME)
            and any(
                not strip.mute
                and strip.action is not None
                and not strip.action.get(BAKED_ACTION_TAG)
                for strip in track.strips
            )
            for track in (anim_data.nla_tracks if anim_data else [])
        )
        if not has_cutscene_source:
            continue
        isolated = False
        for track in getattr(anim_data, "nla_tracks", []) or []:
            if str(getattr(track, "name", "") or "") not in IMPORT_TRACK_NAMES:
                continue
            states.append((track, _rna_state(track, ("mute", "is_solo"))))
            track.mute = True
            if hasattr(track, "is_solo"):
                track.is_solo = False
            isolated = True
        if isolated:
            armature.update_tag(refresh={'TIME'})
    return states


def _restore_import_tracks(states):
    for track, state in states:
        _restore_rna_state(track, state)


def bake_actor_flat_action(context, armature, frame_start, frame_end, action_name=""):
    """Bake pose and object motion, folding world motion into root bones."""
    frames = list(range(int(frame_start), int(frame_end) + 1))
    import_states = _isolate_import_tracks((armature,))
    try:
        samples, custom = _sample_actor(context, armature, frames)
    finally:
        _restore_import_tracks(import_states)
    label = str(armature.get("cutscene_actor_name", "") or armature.name).strip()
    return _write_flat_action(armature, frames, samples, action_name or f"{label}:Root:cs_baked", custom)


def _write_flat_action(armature, frames, samples, action_name, custom=None):
    action = bpy.data.actions.new(action_name)
    action[BAKED_ACTION_TAG] = True
    fcurves = {}

    def fc(path, index):
        key = (path, index)
        if key not in fcurves:
            fcurves[key] = action.fcurves.new(path, index=index) if hasattr(action, "fcurves") else None
        return fcurves[key]

    use_legacy = hasattr(action, "fcurves")
    if not use_legacy:
        # Blender 4.4+ slotted actions
        slot = action.slots.new('OBJECT', armature.name)
        layer = action.layers.new("Layer")
        strip = layer.strips.new(type='KEYFRAME')
        bag = strip.channelbags.new(slot)

        def fc(path, index):
            key = (path, index)
            if key not in fcurves:
                fcurves[key] = bag.fcurves.new(path, index=index)
            return fcurves[key]

    n = len(frames)

    def write(curve, values):
        data = [0.0] * (2 * n)
        for fi, (frame, value) in enumerate(zip(frames, values)):
            data[2 * fi] = frame
            data[2 * fi + 1] = value
        curve.keyframe_points.add(n)
        curve.keyframe_points.foreach_set("co", data)
        curve.update()

    for name in samples:
        base = f'pose.bones["{name}"]'
        pb = armature.pose.bones.get(name)
        if pb is not None:
            pb.rotation_mode = 'QUATERNION'
        decomposed = [basis.decompose() for basis in samples[name]]
        for ci in range(3):
            write(fc(base + ".location", ci), [loc[ci] for loc, _rot, _scale in decomposed])
        for ci in range(4):
            write(fc(base + ".rotation_quaternion", ci), [rot[ci] for _loc, rot, _scale in decomposed])

    for (bone, prop), values in (custom or {}).items():
        write(fc(f'pose.bones["{bone}"]["{prop}"]', 0), values)

    return action


def _remove_bake_output(holder):
    ad = holder.animation_data
    if ad is None:
        return
    for track in list(ad.nla_tracks):
        actions = [s.action for s in track.strips if s.action is not None]
        if track.name.startswith(CUTSCENE_TRACK_NAME) and actions and all(a.get(BAKED_ACTION_TAG) for a in actions):
            transaction = _active_bake_transaction(holder)
            if transaction is not None and transaction.stage_track(holder, track, delete_orphan_actions=True):
                continue
            ad.nla_tracks.remove(track)
            for action in actions:
                if action.users == 0:
                    bpy.data.actions.remove(action)


def _add_bake_track(armature, action, frame_start):
    ad = armature.animation_data or armature.animation_data_create()
    track = ad.nla_tracks.new()
    track.name = BAKE_TRACK_NAME
    strip = track.strips.new(action.name, int(frame_start), action)
    strip.name = action.name
    ad.action = None


def _flat(matrix):
    return [v for row in matrix for v in row]


def _unflat(values):
    return Matrix([values[i * 4:i * 4 + 4] for i in range(4)])


def _stash_holder(holder, track_state_overrides=None):
    """Silence and zero a baked chain object; the recorded state lets a re-bake resample the sources."""
    if holder.get(PREBAKE_STATE_PROP):
        return
    pose_bones = getattr(getattr(holder, "pose", None), "bones", ())
    state = {
        "basis": _flat(holder.matrix_basis),
        "parent_inverse": _flat(holder.matrix_parent_inverse),
        "constraints": [c.name for c in holder.constraints if c.enabled],
        "bone_constraints": [[pb.name, c.name] for pb in pose_bones
                             for c in pb.constraints if c.enabled],
        "unmuted_tracks": [],
        "solo_tracks": [],
        "unmuted_drivers": [],
    }
    track_state_overrides = track_state_overrides or {}
    ad = holder.animation_data
    if ad is not None:
        for drv in ad.drivers:
            if not drv.mute:
                state["unmuted_drivers"].append([drv.data_path, drv.array_index])
            drv.mute = True  # the bake already folded driven motion in
        action = ad.action
        if action is not None:
            # Keep the authored action reachable (and re-bakeable) instead of orphaning it.
            track = ad.nla_tracks.new()
            track.name = CUTSCENE_TRACK_NAME + BAKE_BACKUP_SUFFIX
            try:
                strip = track.strips.new(action.name, int(round(action.frame_range[0])), action)
                slot = getattr(ad, "action_slot", None)
                if slot is not None and hasattr(strip, "action_slot"):
                    strip.action_slot = slot
            except Exception:
                ad.nla_tracks.remove(track)
                action.use_fake_user = True
            ad.action = None
        for track in ad.nla_tracks:
            if track.name.startswith(CUTSCENE_TRACK_NAME) and not _is_backup_track(track):
                track.name = track.name + BAKE_BACKUP_SUFFIX
            override = track_state_overrides.get(track.as_pointer(), {})
            if not override.get("mute", track.mute):
                state["unmuted_tracks"].append(track.name)
            if override.get("is_solo", getattr(track, "is_solo", False)):
                state["solo_tracks"].append(track.name)
            track.mute = True
            if hasattr(track, "is_solo"):
                track.is_solo = False
    for con in holder.constraints:
        con.enabled = False
    for pb in pose_bones:
        for con in pb.constraints:
            con.enabled = False
    holder.matrix_parent_inverse = Matrix.Identity(4)
    holder.matrix_basis = Matrix.Identity(4)
    holder[PREBAKE_STATE_PROP] = json.dumps(state)


def _unstash_holder(holder, defer_import_tracks=False):
    raw = holder.get(PREBAKE_STATE_PROP)
    if not raw:
        return []
    state = json.loads(raw)
    _remove_bake_output(holder)
    deferred = []
    ad = holder.animation_data
    if ad is not None:
        names = set(state.get("unmuted_tracks") or [])
        solo_names = set(state.get("solo_tracks") or []) if "solo_tracks" in state else None
        restore_solo = []
        for track in ad.nla_tracks:
            desired = {
                "mute": track.name not in names,
                "is_solo": track.name in solo_names if solo_names is not None else bool(getattr(track, "is_solo", False)),
            }
            if defer_import_tracks and track.name in IMPORT_TRACK_NAMES:
                deferred.append((track, desired))
                track.mute = True
                if hasattr(track, "is_solo"):
                    track.is_solo = False
                continue
            if hasattr(track, "is_solo"):
                track.is_solo = False
                restore_solo.append((track, desired["is_solo"]))
            track.mute = desired["mute"]
        for track, is_solo in restore_solo:
            track.is_solo = is_solo
        drivers = {tuple(k) for k in state.get("unmuted_drivers") or []}
        for drv in ad.drivers:
            if (drv.data_path, drv.array_index) in drivers:
                drv.mute = False
    enabled = set(state.get("constraints") or [])
    for con in holder.constraints:
        if con.name in enabled:
            con.enabled = True
    pose_bones = getattr(getattr(holder, "pose", None), "bones", None)
    if pose_bones is not None:
        for bone, con_name in state.get("bone_constraints") or []:
            pb = pose_bones.get(bone)
            con = pb.constraints.get(con_name) if pb is not None else None
            if con is not None:
                con.enabled = True
    holder.matrix_parent_inverse = _unflat(state["parent_inverse"])
    holder.matrix_basis = _unflat(state["basis"])
    del holder[PREBAKE_STATE_PROP]
    return deferred


TRAJECTORY_SLOT_PROP = "cutscene_prop_slot"
PROP_RIG_TAG = "cutscene_prop_rig"
PROP_ACTOR_DEFAULT_NAME = "props"
PROPS_ENTITY_FILE_PROP = "cutscene_props_entity_file"


def iter_prop_objects(scene):
    out = []
    for obj in scene.objects:
        slot = str(obj.get(TRAJECTORY_SLOT_PROP, "") or "").strip()
        if slot:
            out.append((obj, slot))
    out.sort(key=lambda item: item[1])
    return out


def find_prop_actor(scene):
    for obj in scene.objects:
        if getattr(obj, "type", None) == 'ARMATURE' and obj.get(PROP_RIG_TAG):
            return obj
    return None


def default_props_entity_path(scene, cutscene_name=""):
    repo = str(getattr(scene, "witcher_cutscene_export_repo_path", "") or "").replace("/", "\\")
    if repo:
        folder, _sep, name = repo.rpartition("\\")
        stem = name.rsplit(".", 1)[0] or cutscene_name or "cutscene"
        return (folder + "\\" if folder else "") + f"{stem}_props.w2ent"
    stem = cutscene_name or "cutscene"
    return f"animations\\cutscenes\\blender_tools\\{stem}_props.w2ent"


def assign_prop_slots(scene, objects):
    """Tag objects with the next free TrajectoryNN slots. Returns [(object, slot)]."""
    from ..CR2W import animated_component as ac

    used = {slot for _obj, slot in iter_prop_objects(scene)}
    free = [name for name in ac.trajectory_bone_names()[1:] if name not in used]
    plan = []
    for obj in objects:
        current = str(obj.get(TRAJECTORY_SLOT_PROP, "") or "").strip()
        if current:
            plan.append((obj, current, False))
        elif free:
            plan.append((obj, free.pop(0), True))
        else:
            raise RuntimeError("All 24 trajectory prop slots are in use")
    for obj, slot, tag in plan:
        if tag:
            obj[TRAJECTORY_SLOT_PROP] = slot
    return [(obj, slot) for obj, slot, _tag in plan]


def clear_prop_slot(obj):
    if TRAJECTORY_SLOT_PROP in obj.keys():
        del obj[TRAJECTORY_SLOT_PROP]


def remove_prop_actor(scene):
    arm = find_prop_actor(scene)
    if arm is not None:
        transaction = _ACTIVE_BAKE_TRANSACTIONS.get(scene.as_pointer())
        if transaction is not None and transaction.defer_prop_removal(arm):
            _remove_bake_output(arm)
            return
        _remove_bake_output(arm)
        data = arm.data
        bpy.data.objects.remove(arm, do_unlink=True)
        if data.users == 0:
            bpy.data.armatures.remove(data)


_TRACK_STATE_ATTRS = ("mute", "is_solo", "lock", "select")


def _rna_state(item, attrs):
    state = {}
    for attr in attrs:
        if hasattr(item, attr):
            try:
                state[attr] = getattr(item, attr)
            except Exception:
                pass
    return state


def _restore_rna_state(item, state):
    for attr, value in state.items():
        if hasattr(item, attr):
            try:
                setattr(item, attr, value)
            except Exception:
                pass


def _active_bake_transaction(holder):
    for transaction in tuple(_ACTIVE_BAKE_TRANSACTIONS.values()):
        if transaction.owns_holder(holder):
            return transaction
    return None


class _BakeTransaction:
    def __init__(self, scene):
        self.scene = scene
        self.scene_pointer = scene.as_pointer()
        self._done = False
        self._deferred_prop_removal = False
        self._staged_tracks = {}
        self._initial_action_pointers = {action.as_pointer() for action in bpy.data.actions}
        self._action_fake_users = {}
        self._initial_prop = find_prop_actor(scene)
        self._initial_prop_pointer = self._initial_prop.as_pointer() if self._initial_prop is not None else None
        self._initial_prop_data = self._initial_prop.data if self._initial_prop is not None else None
        self._initial_prop_collections = tuple(self._initial_prop.users_collection) if self._initial_prop is not None else ()
        self._initial_prop_view_state = []
        if self._initial_prop is not None:
            for owner_scene in bpy.data.scenes:
                for view_layer in owner_scene.view_layers:
                    if self._initial_prop.name in view_layer.objects:
                        self._initial_prop_view_state.append((
                            view_layer,
                            view_layer.objects.active,
                            self._initial_prop.select_get(view_layer=view_layer),
                        ))
        self._scene_state = {
            "frame_start": int(scene.frame_start),
            "frame_end": int(scene.frame_end),
            "frame_current": int(scene.frame_current),
            "frame_subframe": float(getattr(scene, "frame_subframe", 0.0) or 0.0),
            "fingerprint_present": BAKE_FINGERPRINT_PROP in scene.keys(),
            "fingerprint": scene.get(BAKE_FINGERPRINT_PROP),
        }

        holders = []
        seen = set()
        armatures = list(iter_cutscene_actor_armatures(scene))
        if self._initial_prop is not None and self._initial_prop not in armatures:
            armatures.append(self._initial_prop)
        for armature in armatures:
            for holder in _object_chain(armature):
                pointer = holder.as_pointer()
                if pointer not in seen:
                    seen.add(pointer)
                    holders.append(holder)
        self._holder_pointers = seen
        self._holder_states = [self._snapshot_holder(holder) for holder in holders]

    def _remember_action(self, action):
        if action is None:
            return
        pointer = action.as_pointer()
        self._action_fake_users.setdefault(pointer, (action, bool(action.use_fake_user)))

    def _snapshot_holder(self, holder):
        anim_data = holder.animation_data
        pose_bones = getattr(getattr(holder, "pose", None), "bones", ())
        tracks = []
        if anim_data is not None:
            self._remember_action(anim_data.action)
            for track in anim_data.nla_tracks:
                for strip in track.strips:
                    self._remember_action(strip.action)
                tracks.append({
                    "pointer": track.as_pointer(),
                    "state": {"name": track.name, **_rna_state(track, _TRACK_STATE_ATTRS)},
                })

        return {
            "holder": holder,
            "pointer": holder.as_pointer(),
            "matrix_basis": holder.matrix_basis.copy(),
            "matrix_parent_inverse": holder.matrix_parent_inverse.copy(),
            "matrix_world": holder.matrix_world.copy(),
            "prebake_present": PREBAKE_STATE_PROP in holder.keys(),
            "prebake": holder.get(PREBAKE_STATE_PROP),
            "constraints": [(constraint, bool(constraint.enabled)) for constraint in holder.constraints],
            "bone_constraints": [
                (constraint, bool(constraint.enabled))
                for pose_bone in pose_bones
                for constraint in pose_bone.constraints
            ],
            "rotation_modes": {
                pose_bone.name: pose_bone.rotation_mode
                for pose_bone in pose_bones
            },
            "had_animation_data": anim_data is not None,
            "action": anim_data.action if anim_data is not None else None,
            "action_slot": getattr(anim_data, "action_slot", None) if anim_data is not None else None,
            "drivers": {
                (driver.data_path, driver.array_index): bool(driver.mute)
                for driver in (anim_data.drivers if anim_data is not None else [])
            },
            "tracks": tracks,
        }

    def owns_holder(self, holder):
        try:
            return holder.as_pointer() in self._holder_pointers
        except ReferenceError:
            return False

    def stage_track(self, holder, track, delete_orphan_actions=False):
        try:
            holder_pointer = holder.as_pointer()
            track_pointer = track.as_pointer()
        except ReferenceError:
            return False
        state = next((item for item in self._holder_states if item["pointer"] == holder_pointer), None)
        if state is None or track_pointer not in {item["pointer"] for item in state["tracks"]}:
            return False
        staged = self._staged_tracks.setdefault(track_pointer, {
            "holder": holder,
            "track": track,
            "delete_orphan_actions": False,
        })
        staged["delete_orphan_actions"] = bool(staged["delete_orphan_actions"] or delete_orphan_actions)
        track.mute = True
        return True

    def defer_prop_removal(self, armature):
        if self._initial_prop_pointer is None or armature.as_pointer() != self._initial_prop_pointer:
            return False
        if self._deferred_prop_removal:
            return True
        self._deferred_prop_removal = True
        for collection in tuple(armature.users_collection):
            collection.objects.unlink(armature)
        return True

    def _restore_holder(self, state):
        holder = state["holder"]
        try:
            if holder.as_pointer() != state["pointer"]:
                return
        except ReferenceError:
            return

        anim_data = holder.animation_data
        if anim_data is not None:
            initial_track_pointers = {item["pointer"] for item in state["tracks"]}
            for track in list(anim_data.nla_tracks):
                if track.as_pointer() not in initial_track_pointers:
                    anim_data.nla_tracks.remove(track)

            current_tracks = {track.as_pointer(): track for track in anim_data.nla_tracks}
            # Free every original name first so Blender does not suffix names while they are restored.
            for pointer, track in current_tracks.items():
                if pointer in initial_track_pointers:
                    track.name = f"__cutscene_export_rollback_{pointer}"
            for track_state in state["tracks"]:
                track = current_tracks.get(track_state["pointer"])
                if track is not None:
                    _restore_rna_state(track, track_state["state"])

        if not state["had_animation_data"]:
            if holder.animation_data is not None:
                holder.animation_data_clear()
        else:
            anim_data = holder.animation_data or holder.animation_data_create()
            try:
                anim_data.action = state["action"]
            except Exception:
                anim_data.action = None
                anim_data.action = state["action"]
            if state["action_slot"] is not None and hasattr(anim_data, "action_slot"):
                try:
                    anim_data.action_slot = state["action_slot"]
                except Exception:
                    pass
            for driver in anim_data.drivers:
                key = (driver.data_path, driver.array_index)
                if key in state["drivers"]:
                    driver.mute = state["drivers"][key]

        for constraint, enabled in state["constraints"]:
            try:
                constraint.enabled = enabled
            except ReferenceError:
                pass
        for constraint, enabled in state["bone_constraints"]:
            try:
                constraint.enabled = enabled
            except ReferenceError:
                pass
        pose_bones = getattr(getattr(holder, "pose", None), "bones", None)
        if pose_bones is not None:
            for bone_name, rotation_mode in state["rotation_modes"].items():
                pose_bone = pose_bones.get(bone_name)
                if pose_bone is not None:
                    pose_bone.rotation_mode = rotation_mode
        holder.matrix_parent_inverse = state["matrix_parent_inverse"]
        holder.matrix_basis = state["matrix_basis"]
        holder.matrix_world = state["matrix_world"]
        if state["prebake_present"]:
            holder[PREBAKE_STATE_PROP] = state["prebake"]
        elif PREBAKE_STATE_PROP in holder.keys():
            del holder[PREBAKE_STATE_PROP]

    def _finish(self):
        if _ACTIVE_BAKE_TRANSACTIONS.get(self.scene_pointer) is self:
            del _ACTIVE_BAKE_TRANSACTIONS[self.scene_pointer]
        self._done = True

    def rollback(self):
        if self._done:
            return
        scene = self.scene
        try:
            current_prop = find_prop_actor(scene)
            if current_prop is not None and (
                self._initial_prop_pointer is None
                or current_prop.as_pointer() != self._initial_prop_pointer
            ):
                remove_prop_actor(scene)
            for state in self._holder_states:
                self._restore_holder(state)

            for action, use_fake_user in self._action_fake_users.values():
                try:
                    action.use_fake_user = use_fake_user
                except ReferenceError:
                    pass
            for action in list(bpy.data.actions):
                if (
                    action.as_pointer() not in self._initial_action_pointers
                    and action.get(BAKED_ACTION_TAG)
                    and action.users == 0
                ):
                    bpy.data.actions.remove(action)

            scene.frame_start = self._scene_state["frame_start"]
            scene.frame_end = self._scene_state["frame_end"]
            if self._scene_state["fingerprint_present"]:
                scene[BAKE_FINGERPRINT_PROP] = self._scene_state["fingerprint"]
            elif BAKE_FINGERPRINT_PROP in scene.keys():
                del scene[BAKE_FINGERPRINT_PROP]
            if self._deferred_prop_removal:
                for collection in tuple(self._initial_prop.users_collection):
                    collection.objects.unlink(self._initial_prop)
                for collection in self._initial_prop_collections:
                    collection.objects.link(self._initial_prop)
                for view_layer, active, selected in self._initial_prop_view_state:
                    self._initial_prop.select_set(selected, view_layer=view_layer)
                    view_layer.objects.active = active
            scene.frame_set(
                self._scene_state["frame_current"],
                subframe=self._scene_state["frame_subframe"],
            )
            for view_layer in scene.view_layers:
                view_layer.update()
        finally:
            self._finish()

    def commit(self):
        if self._done:
            return
        orphan_candidates = {}
        try:
            for staged in self._staged_tracks.values():
                holder = staged["holder"]
                try:
                    anim_data = holder.animation_data
                except ReferenceError:
                    continue
                if anim_data is None:
                    continue
                pointer = staged["track"].as_pointer()
                track = next((item for item in anim_data.nla_tracks if item.as_pointer() == pointer), None)
                if track is None:
                    continue
                if staged["delete_orphan_actions"]:
                    for strip in track.strips:
                        if strip.action is not None:
                            orphan_candidates[strip.action.as_pointer()] = strip.action
                anim_data.nla_tracks.remove(track)

            if self._deferred_prop_removal:
                bpy.data.objects.remove(self._initial_prop, do_unlink=True)
                if self._initial_prop_data.users == 0:
                    bpy.data.armatures.remove(self._initial_prop_data)
            for action in orphan_candidates.values():
                if action.users == 0:
                    bpy.data.actions.remove(action)
            self._finish()
        finally:
            if not self._done:
                self._finish()


def begin_bake_transaction(scene):
    """Journal bake-owned Blender state so an export can explicitly commit or roll back."""
    pointer = scene.as_pointer()
    if pointer in _ACTIVE_BAKE_TRANSACTIONS:
        raise RuntimeError("A cutscene bake transaction is already active for this scene")
    transaction = _BakeTransaction(scene)
    _ACTIVE_BAKE_TRANSACTIONS[pointer] = transaction
    return transaction


def ensure_prop_actor(context, actor_name=PROP_ACTOR_DEFAULT_NAME, entity_path=""):
    from ..CR2W import animated_component as ac

    scene = context.scene
    arm = find_prop_actor(scene)
    if arm is not None:
        return arm

    arm_data = bpy.data.armatures.new("cutscene_props_ARM")
    arm = bpy.data.objects.new("cutscene_props", arm_data)
    scene.collection.objects.link(arm)

    view_layer = context.view_layer
    prev_active = view_layer.objects.active
    view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode='EDIT')
    try:
        # Match the trajectories rig convention: coincident heads, 0.01 m +X tails.
        ebs = arm_data.edit_bones
        names = ac.trajectory_bone_names()
        root = ebs.new(names[0])
        root.head = (0.0, 0.0, 0.0)
        root.tail = (0.01, 0.0, 0.0)
        for name in names[1:]:
            bone = ebs.new(name)
            bone.head = (0.0, 0.0, 0.0)
            bone.tail = (0.01, 0.0, 0.0)
            bone.parent = root
            bone.use_connect = False
    finally:
        bpy.ops.object.mode_set(mode='OBJECT')
        view_layer.objects.active = prev_active

    arm["cutscene_actor_name"] = actor_name
    arm["cutscene_actor_type"] = "CAT_Prop"
    arm["cutscene_actor_template"] = entity_path or default_props_entity_path(scene)
    arm["witcher_path"] = ac.TRAJECTORY_RIG_PATH
    arm[PROP_RIG_TAG] = True
    return arm


def bake_prop_actor(context, frame_start=None, frame_end=None, cutscene_name=""):
    """Bake prop world transforms to slot bones, or return None.

    Must run before actor chains are zeroed: props sample the evaluated scene.
    """
    scene = context.scene
    props = iter_prop_objects(scene)
    if not props:
        # The prop actor is bake scaffolding; without props it would export stale tracks.
        remove_prop_actor(scene)
        return None
    default_start, default_end = effective_frame_range(scene)
    frame_start = default_start if frame_start is None else int(frame_start)
    frame_end = default_end if frame_end is None else int(frame_end)
    if not cutscene_name:
        cutscene_name = default_props_entity_path(scene).rsplit("\\", 1)[-1].rsplit("_props", 1)[0]

    armature = ensure_prop_actor(context)
    deps = context.evaluated_depsgraph_get()
    bones = armature.data.bones
    root_name = bones[0].name
    root_ml = bones[root_name].matrix_local.copy()
    slot_info = []
    for obj, slot in props:
        bone = bones.get(slot)
        if bone is not None:
            slot_info.append((obj, slot, bone.matrix_local.inverted() @ root_ml))

    frames = list(range(frame_start, frame_end + 1))
    root_basis = root_ml.inverted()
    samples = {root_name: [], **{slot: [] for _obj, slot, _pre in slot_info}}
    current = scene.frame_current
    for frame in frames:
        scene.frame_set(frame)
        deps.update()
        samples[root_name].append(root_basis)
        for obj, slot, pre in slot_info:
            world = obj.evaluated_get(deps).matrix_world
            samples[slot].append(pre @ world)
    scene.frame_set(current)

    label = str(armature.get("cutscene_actor_name", "") or PROP_ACTOR_DEFAULT_NAME).strip()
    action = _write_flat_action(armature, frames, samples, f"{label}:Root:{cutscene_name}_{label}")

    ad = armature.animation_data or armature.animation_data_create()
    # Prop tracks are bake output, so replace rather than back them up.
    for track in [t for t in ad.nla_tracks if t.name.startswith(CUTSCENE_TRACK_NAME)]:
        transaction = _active_bake_transaction(armature)
        if transaction is not None and transaction.stage_track(armature, track):
            continue
        ad.nla_tracks.remove(track)
    _add_bake_track(armature, action, frame_start)
    armature.matrix_world = Matrix.Identity(4)
    return armature, action


def collect_prop_attachments(scene):
    """Serializable CHardAttachment data for the props entity. Raises on missing paths."""
    from ..CR2W import animated_component as ac
    from ..ui.ui_animated_component import resolve_mesh_depot

    bone_names = ac.trajectory_bone_names()
    attachments = []
    for obj, slot in iter_prop_objects(scene):
        if slot not in bone_names:
            raise RuntimeError(f"'{obj.name}' has invalid trajectory slot '{slot}'")
        depot = resolve_mesh_depot(obj)
        if not depot:
            raise RuntimeError(f"No .w2mesh depot path found on prop '{obj.name}'")
        attachments.append({
            "mesh": depot,
            "slot": slot,
            "bone_index": bone_names.index(slot),
        })
    return attachments


def generate_props_entity(context, out_path=""):
    from ..CR2W import animated_component as ac
    from ..ui.ui_animated_component import _resolve_export_dir, _safe_repo_output_path

    scene = context.scene
    attachments = collect_prop_attachments(scene)
    if not attachments:
        raise RuntimeError("No prop objects assigned to trajectory slots")

    armature = ensure_prop_actor(context)
    entity_path = str(armature.get("cutscene_actor_template", "") or "").strip() \
        or default_props_entity_path(scene)
    if not out_path:
        export_dir = _resolve_export_dir(context)
        if not export_dir:
            raise RuntimeError("No REDkit project or uncook path configured for output")
        out_path = _safe_repo_output_path(export_dir, entity_path)
    entity_name = os.path.splitext(os.path.basename(entity_path.replace("\\", "/")))[0]
    ac.generate_entity(attachments, out_path, entity_name=entity_name)
    armature["cutscene_actor_template"] = entity_path
    armature[PROPS_ENTITY_FILE_PROP] = str(out_path)
    return out_path


def bake_cutscene_actors(context, frame_start=None, frame_end=None, cutscene_name="", set_scene_range=False):
    """Bake all actors to full-length tracks, muting the editable source tracks.

    Every actor and prop is sampled from the live scene before any chain is
    zeroed (shared parents and prop parenting stay correct); a re-bake first
    restores the previous sources so edits are picked up.
    """
    scene = context.scene
    actors = [a for a in iter_cutscene_actor_armatures(scene) if not a.get(PROP_RIG_TAG)]
    deferred_import_states = []
    for armature in actors:
        for holder in _object_chain(armature):
            deferred_import_states.extend(_unstash_holder(holder, defer_import_tracks=True))
    source_starts_by_armature = {
        armature.as_pointer(): _active_cutscene_source_starts(armature)
        for armature in actors
    }

    default_start, default_end = effective_frame_range(scene)
    frame_start = default_start if frame_start is None else int(frame_start)
    frame_end = default_end if frame_end is None else int(frame_end)
    if set_scene_range:
        scene.frame_start, scene.frame_end = frame_start, frame_end
    if not cutscene_name:
        repo = str(getattr(scene, "witcher_cutscene_export_repo_path", "") or "")
        cutscene_name = repo.replace("/", "\\").rsplit("\\", 1)[-1].rsplit(".", 1)[0] or "cutscene"

    frames = list(range(frame_start, frame_end + 1))
    current = scene.frame_current
    import_states = _isolate_import_tracks(actors)
    intended_import_states = {
        track.as_pointer(): state
        for track, state in (*import_states, *deferred_import_states)
    }
    sampled_ok = False
    try:
        scale_issues = [issue for a in actors
                        for issue in _scale_issues(context, a, str(a.get("cutscene_actor_name", "") or a.name))]
        if scale_issues:
            raise RuntimeError("Cannot bake:\n" + "\n".join(scale_issues))

        # Unanimated actors get a placement track too, or the engine never spawns them.
        targets = [a for a in actors if _needs_bake(a)
                   or str(a.get("cutscene_actor_name", "") or "").strip().lower() not in SCAFFOLD_ACTORS]
        sampled = [(a, *_sample_actor(context, a, frames)) for a in targets]
        prop_result = bake_prop_actor(context, frame_start, frame_end, cutscene_name)
        sampled_ok = True
    finally:
        scene.frame_set(current)
        if not sampled_ok:
            _restore_import_tracks(import_states)
            _restore_import_tracks(deferred_import_states)

    baked = []
    for armature, samples, custom in sampled:
        label = str(armature.get("cutscene_actor_name", "") or "").strip()
        action = _write_flat_action(armature, frames, samples, f"{label}:Root:{cutscene_name}_{label}", custom)
        source_starts = source_starts_by_armature.get(armature.as_pointer(), {})
        source_ids = list(source_starts)
        if source_ids:
            action[BAKED_SOURCE_CLIP_IDS_PROP] = source_ids
            action[BAKED_SOURCE_CLIP_STARTS_PROP] = [source_starts[source_id] for source_id in source_ids]
        for holder in _object_chain(armature):
            _stash_holder(holder, intended_import_states)
        _add_bake_track(armature, action, frame_start)
        baked.append((armature, action))
    if prop_result:
        baked.append(prop_result)
    context.view_layer.update()  # zeroed chains must be reflected in matrix_world for validation
    scene[BAKE_FINGERPRINT_PROP] = bake_fingerprint(scene)
    return baked


def validate_cutscene_for_export(
        context, frame_start=None, frame_end=None, allowed_missing_prop_templates=(), details=None):
    """Return violations of the engine's one-full-length-track-per-actor contract."""
    scene = context.scene
    allowed_missing_prop_templates = {
        str(path or "").strip().replace("/", "\\").lower()
        for path in allowed_missing_prop_templates
        if str(path or "").strip()
    }
    default_start, default_end = effective_frame_range(scene)
    frame_start = default_start if frame_start is None else int(frame_start)
    frame_end = default_end if frame_end is None else int(frame_end)
    issues = []

    def add(message, obj=None, tab="TEMPLATE"):
        issues.append(message)
        if details is not None:
            details.append((message, getattr(obj, "name", "") if obj is not None else "", tab))

    actors = list(iter_cutscene_actor_armatures(scene))
    if not actors:
        add("No cutscene actors (armatures with cutscene_actor_name) in the scene", tab="ACTORS")
        return issues

    for armature in actors:
        label = str(armature.get("cutscene_actor_name", "") or armature.name)
        strips = _active_cutscene_strips(armature)
        if not strips and label.lower() not in SCAFFOLD_ACTORS:
            add(f"{label}: no active cutscene_anim strips — " + (
                "bake to flatten the active action" if _has_animation(armature)
                else "actor will not appear in engine; bake to create its placement track"), armature)
        if len(strips) > 1:
            add(
                f"{label}: {len(strips)} strips — engine plays ALL animations from t=0 "
                f"(no timeline); run Bake for Cutscene to flatten", armature,
            )
        for strip in strips:
            if int(strip.frame_start) > frame_start or int(strip.frame_end) < frame_end:
                add(
                    f"{label}: strip '{strip.name}' covers {int(strip.frame_start)}-{int(strip.frame_end)}, "
                    f"not the full range {frame_start}-{frame_end} — engine holds/misses poses", armature,
                )
        for message in _scale_issues(context, armature, label):
            add(message, armature)
        for holder in _object_chain(armature):
            if not _is_identity(holder.matrix_world):
                add(
                    f"{label}: object '{holder.name}' has a non-identity transform — the engine ignores "
                    f"object transforms; bake folds translation/rotation into bones", holder,
                )
            had = holder.animation_data
            if had and (
                had.action
                or (
                    holder is not armature
                    and any(
                        not track.mute
                        and any(strip.action is not None and not strip.mute for strip in track.strips)
                        for track in had.nla_tracks
                    )
                )
            ):
                add(f"{label}: object '{holder.name}' has object-level animation — not exported; bake it", holder)
            for con in holder.constraints:
                if con.enabled:
                    add(f"{label}: constraint '{con.name}' on '{holder.name}' will not export; bake", holder)
        bone_constraints = sum(1 for pb in armature.pose.bones for con in pb.constraints if con.enabled)
        if bone_constraints:
            add(f"{label}: {bone_constraints} enabled bone constraint(s) — export reads fcurves only; bake", armature)

    props = iter_prop_objects(scene)
    prop_arm = find_prop_actor(scene)
    if props:
        seen_slots = {}
        for obj, slot in props:
            if slot in seen_slots:
                add(f"Props '{seen_slots[slot]}' and '{obj.name}' share trajectory slot {slot}", obj, tab="ACTORS")
            seen_slots[slot] = obj.name
        if prop_arm is None or not _active_cutscene_strips(prop_arm):
            add(f"{len(props)} prop(s) assigned to trajectory slots but not baked — run Bake for Cutscene", prop_arm)
        else:
            from ..ui.ui_animated_component import _resolve_repo_file

            template = str(prop_arm.get("cutscene_actor_template", "") or "").strip()
            written = str(prop_arm.get(PROPS_ENTITY_FILE_PROP, "") or "")
            template_key = template.replace("/", "\\").lower()
            if (
                (not template or not ((written and os.path.isfile(written)) or _resolve_repo_file(context, template)))
                and template_key not in allowed_missing_prop_templates
            ):
                add("Props entity (.w2ent) not written yet — run Generate Props Entity", prop_arm)
    elif prop_arm is not None:
        add("Prop actor exists but no props are assigned to trajectory slots — re-bake or remove it", prop_arm, tab="ACTORS")
    return issues


def bake_fingerprint(scene):
    """Hash of every authored cutscene strip (bake outputs excluded) and active action, per actor."""
    items = []
    for arm in iter_cutscene_actor_armatures(scene):
        ad = arm.animation_data
        if not ad:
            continue
        if ad.action is not None:
            items.append((arm.name, "", "", ad.action.name, 0.0, 0.0, False))
        for track in ad.nla_tracks:
            if not track.name.startswith(CUTSCENE_TRACK_NAME):
                continue
            for s in track.strips:
                if s.action is None or s.action.get(BAKED_ACTION_TAG):
                    continue
                items.append((arm.name, track.name, s.name, s.action.name,
                              round(float(s.frame_start), 3), round(float(s.frame_end), 3), bool(track.mute or s.mute)))
    return hashlib.sha1(repr(sorted(items)).encode("utf-8")).hexdigest()


def iter_shot_markers(scene):
    shots = []
    for marker in getattr(scene, "timeline_markers", []) or []:
        cam = getattr(marker, "camera", None)
        if getattr(cam, "type", None) != 'CAMERA' or cam.get("witcher_shot_index") is None:
            continue
        shots.append((int(cam["witcher_shot_index"]), cam, int(marker.frame)))
    shots.sort(key=lambda shot: shot[2])
    return shots


def shot_ranges(scene, shots=None):
    shots = iter_shot_markers(scene) if shots is None else list(shots)
    scene_end = int(getattr(scene, "frame_end", 0))
    return [
        (idx, cam, frame, (shots[i + 1][2] if i + 1 < len(shots) else scene_end + 1) - 1)
        for i, (idx, cam, frame) in enumerate(shots)
    ]


def shots_fingerprint(scene):
    """Hash shot markers, cameras, and their animation."""
    from .action_compat import iter_action_fcurves

    def keys(datablock):
        action = getattr(getattr(datablock, "animation_data", None), "action", None)
        if action is None:
            return ()
        return tuple(
            (fc.data_path, fc.array_index, tuple((round(k.co[0], 3), round(k.co[1], 5)) for k in fc.keyframe_points))
            for fc in iter_action_fcurves(action, target=datablock)
        )

    def nla(obj):
        ad = getattr(obj, "animation_data", None)
        return tuple(
            (t.name, s.name, getattr(s.action, "name", ""), round(float(s.frame_start), 3), round(float(s.frame_end), 3),
             bool(t.mute or s.mute))
            for t in (getattr(ad, "nla_tracks", None) or ()) for s in t.strips
        )

    def flat(value):
        if isinstance(value, (str, bytes)):
            return value
        if isinstance(value, (set, frozenset)):
            return tuple(sorted(value))
        try:
            return tuple(flat(v) for v in value)
        except TypeError:
            return round(value, 4) if isinstance(value, float) else value

    def animated_paths(datablock):
        ad = getattr(datablock, "animation_data", None)
        if ad is None:
            return set()
        paths = {d.data_path for d in ad.drivers}
        for action in [ad.action] + [s.action for t in ad.nla_tracks for s in t.strips]:
            if action is not None:
                paths.update(fc.data_path for fc in iter_action_fcurves(action, target=datablock))
        return paths

    def props(datablock, paths):
        """Static values only: animated or driven channels move on scrub, and their keys are hashed instead."""
        animated = animated_paths(datablock)
        return tuple(flat(datablock.path_resolve(p)) for p in paths if p not in animated)

    def constraint(c):
        return tuple(
            (p.identifier, getattr(getattr(c, p.identifier), "name", "") if p.type == 'POINTER' else flat(getattr(c, p.identifier)))
            for p in c.bl_rna.properties
            if p.type != 'COLLECTION' and p.identifier not in _CONSTRAINT_UI_PROPS
        )

    def upstream(obj):
        """The camera plus everything its world matrix depends on: parents and constraint targets, transitively."""
        out, seen, stack = [], set(), [obj]
        while stack:
            o = stack.pop()
            if o is None or o.name in seen:
                continue
            seen.add(o.name)
            out.append((
                o.name, o.rotation_mode, o.parent_type, o.parent_bone, flat(o.matrix_parent_inverse),
                props(o, _TRANSFORM_PATHS), tuple(constraint(c) for c in o.constraints), keys(o), nla(o),
            ))
            stack.append(o.parent)
            stack.extend(getattr(c, "target", None) for c in o.constraints)
        return tuple(out)

    items = [int(getattr(scene, "frame_end", 0))]
    for idx, cam, frame in iter_shot_markers(scene):
        data = cam.data
        items.append((
            idx, frame, upstream(cam), props(data, _CAMERA_PATHS), keys(data), nla(data),
            upstream(data.dof.focus_object) if data.dof.focus_object is not None else (),
        ))
    return hashlib.sha1(repr(items).encode("utf-8")).hexdigest()


def shots_stale(scene):
    if not iter_shot_markers(scene):
        return False
    stored = scene.get(SHOTS_FINGERPRINT_PROP)
    return stored is None or stored != shots_fingerprint(scene)


def bake_state(scene):
    """Lightweight hint: baked output exists; stale means its fingerprint is missing or changed."""
    actors = [a for a in iter_cutscene_actor_armatures(scene) if not a.get(PROP_RIG_TAG)]
    targets = [a for a in actors if _needs_bake(a)
               or str(a.get("cutscene_actor_name", "") or "").strip().lower() not in SCAFFOLD_ACTORS]
    baked = [a for a in targets
             if any(s.action is not None and s.action.get(BAKED_ACTION_TAG) for s in _active_cutscene_strips(a))]
    is_baked = bool(targets) and len(baked) == len(targets)
    stored = scene.get(BAKE_FINGERPRINT_PROP)
    return {
        "baked": is_baked,
        "baked_count": len(baked),
        "target_count": len(targets),
        "stale": bool(is_baked and (stored is None or stored != bake_fingerprint(scene))),
        "shots_stale": shots_stale(scene),
        "range": effective_frame_range(scene),
    }
