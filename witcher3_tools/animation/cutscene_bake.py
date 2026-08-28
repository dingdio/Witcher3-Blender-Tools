"""Bake Blender cutscene timelines into engine-ready actor actions."""
from __future__ import annotations

import json
import os

import bpy
from mathutils import Matrix

CUTSCENE_TRACK_NAME = BAKE_TRACK_NAME = "cutscene_anim"
BAKE_BACKUP_SUFFIX = "_prebake"
BAKED_ACTION_TAG = "cutscene_bake_output"
PREBAKE_STATE_PROP = "cutscene_prebake_state"
SCALE_TOL = 1e-4

SCAFFOLD_ACTORS = {"trajectories"}


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
    return [s for t in tracks for s in t.strips]


def effective_frame_range(scene):
    """Scene range extended to the last unmuted cutscene strip end across actors."""
    end = int(scene.frame_end)
    for armature in iter_cutscene_actor_armatures(scene):
        ad = armature.animation_data
        for track in (ad.nla_tracks if ad else []):
            if track.name.startswith(CUTSCENE_TRACK_NAME) and not track.mute:
                for strip in track.strips:
                    end = max(end, int(strip.frame_end))
    return int(scene.frame_start), end


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


def bake_actor_flat_action(context, armature, frame_start, frame_end, action_name=""):
    """Bake pose and object motion, folding world motion into root bones."""
    frames = list(range(int(frame_start), int(frame_end) + 1))
    samples, custom = _sample_actor(context, armature, frames)
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


def _stash_holder(holder):
    """Silence and zero a baked chain object; the recorded state lets a re-bake resample the sources."""
    if holder.get(PREBAKE_STATE_PROP):
        return
    is_arm = holder.type == 'ARMATURE'
    state = {
        "basis": _flat(holder.matrix_basis),
        "parent_inverse": _flat(holder.matrix_parent_inverse),
        "constraints": [c.name for c in holder.constraints if c.enabled],
        "bone_constraints": [[pb.name, c.name] for pb in holder.pose.bones
                             for c in pb.constraints if c.enabled] if is_arm else [],
        "unmuted_tracks": [],
        "unmuted_drivers": [],
    }
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
            if not track.mute:
                state["unmuted_tracks"].append(track.name)
            track.mute = True
    for con in holder.constraints:
        con.enabled = False
    if is_arm:
        for pb in holder.pose.bones:
            for con in pb.constraints:
                con.enabled = False
    holder.matrix_parent_inverse = Matrix.Identity(4)
    holder.matrix_basis = Matrix.Identity(4)
    holder[PREBAKE_STATE_PROP] = json.dumps(state)


def _unstash_holder(holder):
    raw = holder.get(PREBAKE_STATE_PROP)
    if not raw:
        return
    state = json.loads(raw)
    _remove_bake_output(holder)
    ad = holder.animation_data
    if ad is not None:
        names = set(state.get("unmuted_tracks") or [])
        for track in ad.nla_tracks:
            if track.name in names:
                track.mute = False
        drivers = {tuple(k) for k in state.get("unmuted_drivers") or []}
        for drv in ad.drivers:
            if (drv.data_path, drv.array_index) in drivers:
                drv.mute = False
    enabled = set(state.get("constraints") or [])
    for con in holder.constraints:
        if con.name in enabled:
            con.enabled = True
    if holder.type == 'ARMATURE':
        for bone, con_name in state.get("bone_constraints") or []:
            pb = holder.pose.bones.get(bone)
            con = pb.constraints.get(con_name) if pb is not None else None
            if con is not None:
                con.enabled = True
    holder.matrix_parent_inverse = _unflat(state["parent_inverse"])
    holder.matrix_basis = _unflat(state["basis"])
    del holder[PREBAKE_STATE_PROP]


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
        _remove_bake_output(arm)
        data = arm.data
        bpy.data.objects.remove(arm)
        bpy.data.armatures.remove(data)


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


def bake_cutscene_actors(context, frame_start=None, frame_end=None, cutscene_name=""):
    """Bake all actors to full-length tracks, muting the editable source tracks.

    Every actor and prop is sampled from the live scene before any chain is
    zeroed (shared parents and prop parenting stay correct); a re-bake first
    restores the previous sources so edits are picked up.
    """
    scene = context.scene
    actors = [a for a in iter_cutscene_actor_armatures(scene) if not a.get(PROP_RIG_TAG)]
    for armature in actors:
        for holder in _object_chain(armature):
            _unstash_holder(holder)

    default_start, default_end = effective_frame_range(scene)
    frame_start = default_start if frame_start is None else int(frame_start)
    frame_end = default_end if frame_end is None else int(frame_end)
    scene.frame_start, scene.frame_end = frame_start, frame_end
    if not cutscene_name:
        repo = str(getattr(scene, "witcher_cutscene_export_repo_path", "") or "")
        cutscene_name = repo.replace("/", "\\").rsplit("\\", 1)[-1].rsplit(".", 1)[0] or "cutscene"

    scale_issues = [issue for a in actors
                    for issue in _scale_issues(context, a, str(a.get("cutscene_actor_name", "") or a.name))]
    if scale_issues:
        raise RuntimeError("Cannot bake:\n" + "\n".join(scale_issues))

    frames = list(range(frame_start, frame_end + 1))
    current = scene.frame_current
    try:
        # Unanimated actors get a placement track too, or the engine never spawns them.
        targets = [a for a in actors if _needs_bake(a)
                   or str(a.get("cutscene_actor_name", "") or "").strip().lower() not in SCAFFOLD_ACTORS]
        sampled = [(a, *_sample_actor(context, a, frames)) for a in targets]
        prop_result = bake_prop_actor(context, frame_start, frame_end, cutscene_name)
    finally:
        scene.frame_set(current)

    baked = []
    for armature, samples, custom in sampled:
        label = str(armature.get("cutscene_actor_name", "") or "").strip()
        action = _write_flat_action(armature, frames, samples, f"{label}:Root:{cutscene_name}_{label}", custom)
        for holder in _object_chain(armature):
            _stash_holder(holder)
        _add_bake_track(armature, action, frame_start)
        baked.append((armature, action))
    if prop_result:
        baked.append(prop_result)
    context.view_layer.update()  # zeroed chains must be reflected in matrix_world for validation
    return baked


def validate_cutscene_for_export(context, frame_start=None, frame_end=None):
    """Return violations of the engine's one-full-length-track-per-actor contract."""
    scene = context.scene
    default_start, default_end = effective_frame_range(scene)
    frame_start = default_start if frame_start is None else int(frame_start)
    frame_end = default_end if frame_end is None else int(frame_end)
    issues = []
    actors = list(iter_cutscene_actor_armatures(scene))
    if not actors:
        return ["No cutscene actors (armatures with cutscene_actor_name) in the scene"]

    for armature in actors:
        label = str(armature.get("cutscene_actor_name", "") or armature.name)
        strips = _active_cutscene_strips(armature)
        if not strips and label.lower() not in SCAFFOLD_ACTORS:
            issues.append(f"{label}: no active cutscene_anim strips — " + (
                "bake to flatten the active action" if _has_animation(armature)
                else "actor will not appear in engine; bake to create its placement track"))
        if len(strips) > 1:
            issues.append(
                f"{label}: {len(strips)} strips — engine plays ALL animations from t=0 "
                f"(no timeline); run Bake for Cutscene to flatten"
            )
        for strip in strips:
            if int(strip.frame_start) > frame_start or int(strip.frame_end) < frame_end:
                issues.append(
                    f"{label}: strip '{strip.name}' covers {int(strip.frame_start)}-{int(strip.frame_end)}, "
                    f"not the full range {frame_start}-{frame_end} — engine holds/misses poses"
                )
        issues.extend(_scale_issues(context, armature, label))
        for holder in _object_chain(armature):
            if not _is_identity(holder.matrix_world):
                issues.append(
                    f"{label}: object '{holder.name}' has a non-identity transform — the engine ignores "
                    f"object transforms; bake folds translation/rotation into bones"
                )
            had = holder.animation_data
            if had and (had.action or (holder is not armature and any(not t.mute for t in had.nla_tracks))):
                issues.append(
                    f"{label}: object '{holder.name}' has object-level animation — not exported; bake it"
                )
            for con in holder.constraints:
                if con.enabled:
                    issues.append(f"{label}: constraint '{con.name}' on '{holder.name}' will not export; bake")
        bone_constraints = sum(1 for pb in armature.pose.bones for con in pb.constraints if con.enabled)
        if bone_constraints:
            issues.append(f"{label}: {bone_constraints} enabled bone constraint(s) — export reads fcurves only; bake")

    props = iter_prop_objects(scene)
    prop_arm = find_prop_actor(scene)
    if props:
        seen_slots = {}
        for obj, slot in props:
            if slot in seen_slots:
                issues.append(f"Props '{seen_slots[slot]}' and '{obj.name}' share trajectory slot {slot}")
            seen_slots[slot] = obj.name
        if prop_arm is None or not _active_cutscene_strips(prop_arm):
            issues.append(
                f"{len(props)} prop(s) assigned to trajectory slots but not baked — run Bake for Cutscene"
            )
        else:
            from ..ui.ui_animated_component import _resolve_repo_file

            template = str(prop_arm.get("cutscene_actor_template", "") or "").strip()
            written = str(prop_arm.get(PROPS_ENTITY_FILE_PROP, "") or "")
            if not template or not ((written and os.path.isfile(written)) or _resolve_repo_file(context, template)):
                issues.append("Props entity (.w2ent) not written yet — run Generate Props Entity")
    elif prop_arm is not None:
        issues.append("Prop actor exists but no props are assigned to trajectory slots — re-bake or remove it")
    return issues
