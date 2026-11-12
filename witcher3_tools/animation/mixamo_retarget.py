"""Retarget humanoids onto W3 rigs using world-space rest deltas, from telltale_tools."""
import math
import re

import bpy
from mathutils import Matrix, Quaternion, Vector

from .action_compat import assign_action, new_action_fcurve, resolve_action_slot
from .pose_keys import GAME_IK_BONE_MAP
from ..rigging.ik import CONTROL_PREFIX

PREVIEW = "W3 Retarget"
SIDE_ROLES = {"shoulder": "shoulder", "clavicle": "shoulder", "arm": "arm", "upperarm": "arm", "forearm": "forearm",
              "lowerarm": "forearm", "hand": "hand", "upleg": "upleg", "thigh": "upleg", "leg": "leg", "calf": "leg",
              "shin": "leg", "foot": "foot", "toebase": "toe", "ball": "toe", "toe": "toe"}
W3_SIDE = {"shoulder": "shoulder", "arm": "bicep", "forearm": "forearm", "hand": "hand", "upleg": "thigh", "leg": "shin",
           "foot": "foot", "toe": "toe"}
FINGERS = ("thumb", "index", "middle", "ring", "pinky")
CLAVICLES = {"l_shoulder", "r_shoulder"}
FEET = {"l_foot", "r_foot"}
TRAJECTORY = "Trajectory"


def source_role(name: str):
    """Return (role, side, number) for Mixamo/UE bone names, or None."""
    n = name.rsplit(":", 1)[-1].lower().replace(" ", "")
    side = ""
    for prefix, s in (("left", "L"), ("right", "R")):
        if n.startswith(prefix):
            side, n = s, n[len(prefix):]
    m = re.match(r"(.+?)[._]([lr])$", n)
    if not side and m:
        n, side = m.group(1), m.group(2).upper()
    n = n.strip("_.")
    m = re.match(r"(?:hand)?(thumb|index|middle|ring|pinky)_?(?:0*(\d)|metacarpal)$", n)
    if m:
        return (m.group(1), side, int(m.group(2) or 0)) if side else None
    m = re.match(r"(spine|neck)_?0*(\d*)$", n)
    if m and not side:
        return m.group(1), "", int(m.group(2) or 0)
    if not side and n in ("hips", "pelvis", "head"):
        return ("head" if n == "head" else "hips"), "", 0
    role = SIDE_ROLES.get(n)
    return (role, side, 0) if role and side else None


def _depth(bone) -> int:
    return len(bone.parent_recursive)


def _spread(sources, targets):
    """Map chains by relative position, blending adjacent source bones."""
    rows = []
    for i, t in enumerate(targets):
        f = i * (len(sources) - 1) / max(len(targets) - 1, 1)
        a = min(int(f), len(sources) - 1)
        mix = f - a
        rows.append((t, sources[a], sources[a + 1], round(mix, 4)) if mix > 1e-4 else (t, sources[a], "", 0.0))
    return rows


def auto_map(source, target, fingers: bool = True):
    """Return (target, source, blend source, mix) rows from bone names."""
    roles = {}
    for bone in sorted(source.data.bones, key=_depth):
        role = source_role(bone.name)
        if role is not None:
            roles.setdefault(role, bone.name)
    names = {b.name.lower(): b.name for b in target.data.bones}
    present = lambda *wanted: [names[w] for w in wanted if w in names]
    rows = []
    if ("hips", "", 0) in roles and "pelvis" in names:
        rows.append((names["pelvis"], roles[("hips", "", 0)], "", 0.0))
    spine = [roles[r] for r in sorted(r for r in roles if r[0] == "spine")]
    neck = [roles[r] for r in sorted(r for r in roles if r[0] == "neck")] + ([roles[("head", "", 0)]] if ("head", "", 0) in roles else [])
    for sources, targets in ((spine, present("torso", "torso2", "torso3")), (neck, present("neck", "head"))):
        if sources and targets:
            rows += _spread(sources, targets)
    for (role, side, number), name in roles.items():
        if not side:
            continue
        if role in W3_SIDE:
            wanted = f"{side.lower()}_{W3_SIDE[role]}"
        elif fingers and role in FINGERS:
            wanted = f"{side.lower()}_{role}{number}"
        else:
            continue
        if wanted in names:
            rows.append((names[wanted], name, "", 0.0))
    depth = {b.name: _depth(b) for b in target.data.bones}
    return sorted(rows, key=lambda r: depth[r[0]])


def valid_rows(rows, source, target):
    sb, tb = source.data.bones, target.data.bones
    return [(t, s, s2 if s2 in sb else "", mix if s2 in sb else 0.0) for t, s, s2, mix in rows if t in tb and s in sb]


def facing_turn(source, target, rows) -> float:
    """Return world-Z alignment in radians, or zero without both thighs."""
    thighs = {t: s for t, s, *_ in valid_rows(rows, source, target) if t in ("l_thigh", "r_thigh")}
    if len(thighs) < 2:
        return 0.0

    def facing(obj, left, right):
        m = obj.matrix_world
        v = (m @ obj.data.bones[left].head_local - m @ obj.data.bones[right].head_local).cross(Vector((0.0, 0.0, 1.0)))
        return math.atan2(v.y, v.x)

    turn = facing(target, "l_thigh", "r_thigh") - facing(source, thighs["l_thigh"], thighs["r_thigh"])
    return math.atan2(math.sin(turn), math.cos(turn))


def turn_source(source, angle: float):
    """Rotate about world Z through the source origin."""
    pivot = source.matrix_world.translation.copy()
    source.matrix_world = Matrix.Translation(pivot) @ Matrix.Rotation(angle, 4, 'Z') @ Matrix.Translation(-pivot) @ source.matrix_world


def _height(obj, name) -> float:
    m = obj.matrix_world
    return (m @ obj.data.bones[name].head_local).z - min((m @ b.head_local).z for b in obj.data.bones)


def hips_row(rows, target):
    bones = target.data.bones
    return min(rows, key=lambda r: _depth(bones[r[0]]))


def height_ratio(source, target, rows) -> float:
    """Hips height ratio, measured above each rig's lowest bone."""
    rows = valid_rows(rows, source, target)
    if not rows:
        return 1.0
    t, s = hips_row(rows, target)[:2]
    h = _height(source, s)
    return _height(target, t) / h if h > 1e-9 else 1.0


def _rest(obj):
    m = obj.matrix_world
    return {b.name: ((m @ b.matrix_local).to_3x3().normalized().to_quaternion(), m @ b.head_local) for b in obj.data.bones}


def _alignments(rows, source_rest, target_rest, target, match_rest: bool):
    """Align rest directions; clavicles inherit alignment and feet use yaw only."""
    by_target = {r[0]: r for r in rows}
    out = {}
    if not match_rest:
        return {t: Quaternion() for t in by_target}

    def source_head(row):
        head = source_rest[row[1]][1]
        return head.lerp(source_rest[row[2]][1], row[3]) if row[2] else head

    def mapped_children(bone):
        found = []
        for child in bone.children:
            found += [child] if child.name in by_target else mapped_children(child)
        return found

    for bone in sorted((target.data.bones[t] for t in by_target), key=_depth):
        row, align = by_target[bone.name], None
        children = mapped_children(bone)
        if len(children) == 1 and bone.name not in CLAVICLES:
            d_source = source_head(by_target[children[0].name]) - source_head(row)
            d_target = target_rest[children[0].name][1] - target_rest[bone.name][1]
            if bone.name in FEET:
                d_source.z = d_target.z = 0.0
            if d_source.length > 1e-6 and d_target.length > 1e-6:
                align = d_target.rotation_difference(d_source)
        if align is None:
            parent = next((p for p in bone.parent_recursive if p.name in out), None)
            align = out[parent.name].copy() if parent else Quaternion()
        out[bone.name] = align
    return out


def ik_markers(target):
    bones = target.data.bones
    return {ik: bone for ik, bone in GAME_IK_BONE_MAP.items() if ik in bones and bone in bones}


def frame_range(source, scene):
    """Return active action/NLA bounds, falling back to the scene range."""
    data = source.animation_data
    solo = [t for t in data.nla_tracks if t.is_solo] if data and data.use_nla else []
    # Blender does not evaluate the active action while a track is soloed.
    spans = [tuple(data.action.frame_range)] if data and data.action and not solo else []
    if data and data.use_nla:
        spans += [(s.frame_start, s.frame_end) for t in (solo or data.nla_tracks) if not t.mute for s in t.strips if not s.mute]
    start, end = (min(s[0] for s in spans), max(s[1] for s in spans)) if spans else (scene.frame_start, scene.frame_end)
    return range(int(round(start)), int(round(end)) + 1)


def _rest_pose(target):
    """Clear the action and reset bones before preview or bake."""
    data = target.animation_data_create()
    data.action = None
    data.use_nla = False
    data.action_blend_type = 'REPLACE'
    data.action_influence = 1.0
    for pb in target.pose.bones:
        pb.matrix_basis = Matrix()


def _solve(bones, rel, rotations, heads, follow):
    """Return per-bone (rotation, location) bases, including IK markers."""
    pose, out = {}, {}
    for bone in bones:
        base = pose[bone.parent.name] @ rel[bone.name] if bone.parent else rel[bone.name]
        q, head = rotations.get(bone.name), heads.get(bone.name)
        m = base if q is None and head is None else Matrix.LocRotScale(
            base.translation if head is None else head, base.to_quaternion() if q is None else q, None)
        pose[bone.name] = m
        basis = base.inverted_safe() @ m
        out[bone.name] = (basis.to_quaternion(), basis.to_translation())
    lookup = bones[0].id_data.bones
    for marker, bone in follow.items():
        b = lookup[marker]
        base = pose[b.parent.name] @ rel[marker] if b.parent else rel[marker]
        m = pose[bone] @ lookup[bone].matrix_local.inverted() @ b.matrix_local
        basis = base.inverted_safe() @ m
        out[marker] = (basis.to_quaternion(), basis.to_translation())
    return out


def _continuous(quats):
    out = []
    for q in quats:
        if out and out[-1].dot(q) < 0:
            q = -q
        out.append(q)
    return out


def _write_curve(action, target, path, index, group, frames, values):
    curve = new_action_fcurve(action, target, path, index=index, group_name=group)
    if max(values) - min(values) < 1e-7:
        frames, values = frames[:1], values[:1]
    n = len(frames)
    curve.keyframe_points.add(n)
    curve.keyframe_points.foreach_set("co", [v for pair in zip(frames, values) for v in pair])
    curve.keyframe_points.foreach_set("interpolation", [bpy.types.Keyframe.bl_rna.properties["interpolation"].enum_items["LINEAR"].value] * n)
    curve.update()


def bake(context, source, target, rows, scale: float, name: str, root_motion: bool = True, match_rest: bool = True,
         ik: bool = True, frames=None):
    """Bake all target bones; return (action, stats)."""
    rows = valid_rows(rows, source, target)
    if not rows:
        raise ValueError("no bone pair names a bone of both armatures")
    if target.animation_data and target.animation_data.use_tweak_mode:
        raise ValueError("Exit NLA tweak mode before retargeting")
    scene = context.scene
    frames = list(frames or frame_range(source, scene))
    clear_preview(target)
    current = scene.frame_current
    scene.frame_set(frames[0])
    source_rest, target_rest = _rest(source), _rest(target)
    align = _alignments(rows, source_rest, target_rest, target, match_rest)
    hips, hips_source = hips_row(rows, target)[:2]
    bones = target.data.bones
    q_target_inv = target.matrix_world.to_3x3().normalized().to_quaternion().inverted()
    rel = {b.name: (b.parent.matrix_local.inverted() @ b.matrix_local) if b.parent else b.matrix_local.copy() for b in bones}
    ordered = sorted(bones, key=_depth)
    follow = ik_markers(target) if ik else {}
    trajectory = TRAJECTORY if root_motion and TRAJECTORY in bones else None
    used = {r[1] for r in rows} | {r[2] for r in rows if r[2]}
    offsets = {r[0]: align[r[0]] @ target_rest[r[0]][0] for r in rows}
    rest_inv = {n: source_rest[n][0].inverted() for n in used}

    samples = []
    try:
        for f in frames:
            scene.frame_set(f)
            m = source.matrix_world
            pose = source.pose.bones
            samples.append(({n: (m @ pose[n].matrix).to_3x3().normalized().to_quaternion() @ rest_inv[n] for n in used},
                            (m @ pose[hips_source].matrix).translation))
    finally:
        scene.frame_set(current)

    travel = (samples[-1][1] - samples[0][1]) * scale if trajectory else Vector()
    travel.z = 0.0
    travel = q_target_inv @ travel
    span = max(len(frames) - 1, 1)
    h_rest = source_rest[hips_source][1]
    traj_rest = bones[trajectory].head_local if trajectory else None
    solved = []
    for i, (deltas, head) in enumerate(samples):
        rotations = {}
        for t, s, s2, mix in rows:
            d = deltas[s].slerp(deltas[s2], mix) if s2 else deltas[s]
            rotations[t] = q_target_inv @ d @ offsets[t]
        step = travel * (i / span)
        # Keep body travel: the engine plays it relative to Trajectory.
        heads = {hips: bones[hips].head_local + q_target_inv @ ((head - h_rest) * scale)}
        if trajectory:
            heads[trajectory] = traj_rest + step
        solved.append(_solve(ordered, rel, rotations, heads, follow))

    fps = scene.render.fps / scene.render.fps_base
    action = bpy.data.actions.new(name)
    action.use_fake_user = True
    action["w3_retarget_source"] = source.name
    resolve_action_slot(action, target=target, ensure=True)
    keys = [float(f) for f in frames]
    for b in ordered:
        n = b.name
        # Keyed IK controls would mark the bake as unbaked IK and block export.
        if n.startswith(CONTROL_PREFIX):
            continue
        path = f'pose.bones["{n}"]'
        quats = _continuous([frame[n][0] for frame in solved])
        # Trajectory yaw stays unkeyed until authored or generated explicitly.
        for k in range(0 if n == trajectory else 4):
            _write_curve(action, target, path + ".rotation_quaternion", k, n, keys, [q[k] for q in quats])
        for k in range(3):
            _write_curve(action, target, path + ".location", k, n, keys, [frame[n][1][k] for frame in solved])
    action.frame_range = (keys[0], keys[-1])
    previous = target.animation_data.action if target.animation_data else None
    if previous is not None:
        previous.use_fake_user = True
    for pb in target.pose.bones:
        pb.rotation_mode = 'QUATERNION'
    _rest_pose(target)
    assign_action(target, action)
    scene.frame_set(current)
    return action, {"bones": len(rows), "frames": len(frames), "fps": fps, "travel": travel.length}


def preview_active(target) -> bool:
    return target is not None and target.type == 'ARMATURE' and (any(
        c.name.startswith(PREVIEW) for pb in target.pose.bones for c in pb.constraints)
        or bool(_preview_collections(target)))


def _preview_collections(target):
    return [c for c in bpy.data.collections if c.get("w3_retarget_target") in (target, target.name)]


def clear_preview(target):
    for pb in target.pose.bones:
        for c in [c for c in pb.constraints if c.name.startswith(PREVIEW)]:
            pb.constraints.remove(c)
    for coll in _preview_collections(target):
        if "previous_pose" in coll:
            data = target.animation_data_create()
            data.action = coll.get("previous_action")
            if data.action is not None:
                data.action_slot = next((s for s in data.action.slots if s.handle == coll["previous_slot"]), None)
            for prop, value in coll["previous_settings"].items():
                setattr(data, prop, value)
            for name, state in coll["previous_pose"].items():
                pb = target.pose.bones.get(name)
                if pb is not None:
                    pb.rotation_mode = state["mode"]
                    values = state["matrix"]
                    pb.matrix_basis = Matrix([values[i:i + 4] for i in range(0, 16, 4)])
        for obj in list(coll.objects):
            bpy.data.objects.remove(obj)
        bpy.data.collections.remove(coll)
    scene = bpy.context.scene
    scene.frame_set(scene.frame_current, subframe=scene.frame_subframe)


def preview(context, source, target, rows, scale: float, match_rest: bool = True, ik: bool = True) -> int:
    """Create live constraints; return the mapped bone count."""
    rows = valid_rows(rows, source, target)
    if not rows:
        raise ValueError("no bone pair names a bone of both armatures")
    if target.animation_data and target.animation_data.use_tweak_mode:
        raise ValueError("Exit NLA tweak mode before retargeting")
    clear_preview(target)
    scene = context.scene
    current = scene.frame_current
    scene.frame_set(frame_range(source, scene)[0])
    source_rest, target_rest = _rest(source), _rest(target)
    scene.frame_set(current)
    align = _alignments(rows, source_rest, target_rest, target, match_rest)
    hips = hips_row(rows, target)[0]
    coll = bpy.data.collections.new(PREVIEW)
    coll["w3_retarget_target"] = target
    data = target.animation_data_create()
    if data.action is not None:
        coll["previous_action"] = data.action
    coll["previous_slot"] = data.action_slot_handle
    coll["previous_settings"] = {p: getattr(data, p) for p in ("use_nla", "action_blend_type", "action_influence")}
    coll["previous_pose"] = {pb.name: {"mode": pb.rotation_mode,
                                     "matrix": [v for row in pb.matrix_basis for v in row]} for pb in target.pose.bones}
    scene.collection.children.link(coll)
    _rest_pose(target)
    size = max(target.dimensions) * 0.02
    for t, s, s2, mix in rows:
        pb = target.pose.bones[t]
        pb.rotation_mode = 'QUATERNION'
        if t == hips:
            c = pb.constraints.new('TRANSFORM')
            c.name = PREVIEW + " Location"
            c.target, c.subtarget = source, s
            c.target_space = c.owner_space = 'WORLD'
            c.use_motion_extrapolate = True
            c.map_from = c.map_to = 'LOCATION'
            c.mix_mode = 'REPLACE'
            for axis, h, r in zip("xyz", source_rest[s][1], target_rest[t][1]):
                setattr(c, f"from_min_{axis}", h - 1.0)
                setattr(c, f"from_max_{axis}", h + 1.0)
                setattr(c, f"to_min_{axis}", r - scale)
                setattr(c, f"to_max_{axis}", r + scale)
        for bone, influence in ((s, 1.0), (s2, mix)) if s2 else ((s, 1.0),):
            empty = bpy.data.objects.new(f"{PREVIEW} {t} {bone}", None)
            coll.objects.link(empty)
            empty.empty_display_size = size
            empty.parent, empty.parent_type, empty.parent_bone = source, 'BONE', bone
            empty.matrix_parent_inverse = Matrix()
            empty.rotation_mode = 'QUATERNION'
            empty.rotation_quaternion = source_rest[bone][0].inverted() @ align[t] @ target_rest[t][0]
            c = pb.constraints.new('COPY_ROTATION')
            c.name = PREVIEW
            c.target = empty
            c.target_space = c.owner_space = 'WORLD'
            c.mix_mode = 'REPLACE'
            c.influence = influence
    if ik:
        for marker, bone in ik_markers(target).items():
            c = target.pose.bones[marker].constraints.new('COPY_TRANSFORMS')
            c.name = PREVIEW + " IK"
            c.target, c.subtarget = target, bone
            c.target_space = c.owner_space = 'WORLD'
    for obj in coll.objects:
        obj.hide_set(True)
    return len(rows)
