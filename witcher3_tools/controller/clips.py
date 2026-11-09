"""Sample imported actions into in-place poses and Trajectory root motion.

Motion extraction is the fallback when Trajectory stands still.
"""
import math
import re

import bpy
import numpy as np
from mathutils import Matrix, Quaternion, Vector

from ..animation.action_compat import iter_action_fcurves
from ..importers import motion_tools
from .core import RATE, Clip

_PATH = re.compile(r'pose\.bones\["(.+)"\]\.(location|rotation_quaternion)$')


def _extracted_motion(arm, action, frames):
    source = motion_tools._auto_motion_source_for_action(action, arm)
    if source is None:
        return [Matrix() for _ in frames]
    start = float(action.frame_range[0])
    return [motion_tools._auto_motion_source_matrix(source, f - start) for f in frames]


def _flat(m: Matrix) -> Matrix:
    return Matrix.Translation((m.translation.x, m.translation.y, 0.0)) @ Matrix.Rotation(math.atan2(m[1][0], m[0][0]), 4, 'Z')


def _trajectory_motion(arm, rot, loc):
    """Separate Trajectory pose and travel; parent-space travel avoids double yaw."""
    bone = arm.data.bones.get("Trajectory")
    if bone is None:
        return None
    index = {b.name: i for i, b in enumerate(arm.data.bones)}
    chain = []
    while bone is not None:
        chain.append(bone)
        bone = bone.parent
    chain.reverse()
    locals_ = [(b.parent.matrix_local.inverted() @ b.matrix_local) if b.parent else b.matrix_local.copy() for b in chain]
    rest_inv = chain[-1].matrix_local.inverted()
    parent_rest = chain[-2].matrix_local if len(chain) > 1 else Matrix()
    pose, travel = [], []
    for f in range(len(rot)):
        m = parent = Matrix()
        for b, local in zip(chain, locals_):
            i = index[b.name]
            parent = m
            m = m @ local @ Matrix.LocRotScale(Vector(loc[f, i]), Quaternion(rot[f, i]), None)
        pose.append(_flat(m @ rest_inv))
        travel.append(_flat(parent_rest @ parent.inverted() @ m @ rest_inv))
    moves = any(m.translation.length > 0.01 or abs(math.atan2(m[1][0], m[0][0])) > 0.01 for m in pose + travel)
    return (pose, travel) if moves else None


def _basis(rot, loc, f, i) -> Matrix:
    return Matrix.LocRotScale(Vector(loc[f, i]), Quaternion(rot[f, i]), None)


def _store(rot, loc, f, i, basis: Matrix, prev):
    q = basis.to_quaternion()
    if prev is not None and q.dot(prev) < 0.0:
        q.negate()
    rot[f, i], loc[f, i] = q, basis.translation
    return q


def _make_in_place(bones, rot, loc, motion):
    """Move root correction to children so blended poses stay in place."""
    index = {pb.name: i for i, pb in enumerate(bones)}
    for root in (pb for pb in bones if pb.parent is None):
        r = index[root.name]
        root_rest = root.bone.matrix_local
        children = [(index[c.name], c.bone.matrix_local, c.bone.matrix_local.inverted()) for c in root.children]
        prev = [None] * (len(children) + 1)
        for f, m in enumerate(motion):
            m_inv = m.inverted()
            root_arm = root_rest @ _basis(rot, loc, f, r)
            for k, (c, rest, rest_inv) in enumerate(children):
                arm_space = root_arm @ root_rest.inverted() @ rest @ _basis(rot, loc, f, c)
                prev[k] = _store(rot, loc, f, c, rest_inv @ m_inv @ arm_space, prev[k])
            if children:
                rot[f, r], loc[f, r] = (1.0, 0.0, 0.0, 0.0), (0.0, 0.0, 0.0)
            else:
                prev[-1] = _store(rot, loc, f, r, root_rest.inverted() @ m_inv @ root_arm, prev[-1])


def _close_loop(rot, loc):
    """Spread a cycle's closing error across its samples."""
    count = len(rot)
    if count < 3:
        return
    loc += (loc[0] - loc[-1])[None] * (np.arange(count) / (count - 1))[:, None, None]
    for b in range(rot.shape[1]):
        first, last = Quaternion(rot[0, b]), Quaternion(rot[-1, b])
        if first.dot(last) < 0.0:
            first.negate()
        error = first @ last.inverted()
        for f in range(1, count):
            rot[f, b] = Quaternion().slerp(error, f / (count - 1)) @ Quaternion(rot[f, b])


def sample_action(arm, action, length: float, name: str, cyclic: bool = False) -> Clip:
    bones = arm.pose.bones
    index = {pb.name: i for i, pb in enumerate(bones)}
    count = int(math.ceil(length * RATE - 1e-6)) + 1
    times = np.minimum(np.arange(count) / RATE, length)
    start, end = (float(v) for v in action.frame_range)
    frames = start + times * ((end - start) / length if length > 0 else 0.0)
    rot = np.zeros((count, len(bones), 4))
    rot[..., 0] = 1.0
    loc = np.zeros((count, len(bones), 3))
    for fc in iter_action_fcurves(action, target=arm):
        m = _PATH.match(fc.data_path)
        if m is None or m.group(1) not in index:
            continue
        target = loc if m.group(2) == "location" else rot
        target[:, index[m.group(1)], fc.array_index] = [fc.evaluate(f) for f in frames]
    motion, travel = _trajectory_motion(arm, rot, loc) or (_extracted_motion(arm, action, frames),) * 2
    _make_in_place(bones, rot, loc, motion)
    if cyclic:
        _close_loop(rot, loc)
    rot /= np.maximum(np.linalg.norm(rot, axis=2, keepdims=True), 1e-8)
    root_pos = np.array([m.translation for m in travel])
    root_yaw = np.unwrap([math.atan2(m[1][0], m[0][0]) for m in travel])
    return Clip(name, length, root_pos, root_yaw, poses={"rot": rot.astype(np.float32), "loc": loc.astype(np.float32)})


def load_clip(arm, animset_path: str, name: str, rig_path=None, cyclic: bool = False) -> Clip:
    """Sample `name` and restore the armature's current action."""
    from ..CR2W.dc_anims import load_bin_anims_single
    from ..importers.import_anims import AnimImporter
    anim_set = load_bin_anims_single(animset_path, name, rigPath=rig_path)
    entries = getattr(anim_set, "animations", None) or []
    entry = next((e for e in entries if e.animation.name == name), None)
    if entry is None:
        raise KeyError(name)
    data = arm.animation_data
    before = data.action if data is not None else None
    AnimImporter(animset_path, entry).assign(arm)
    action = arm.animation_data.action
    try:
        if action is None or action == before:
            raise KeyError(name)
        clip = sample_action(arm, action, float(entry.animation.duration), name, cyclic)
    finally:
        if action is not None and action != before:
            arm.animation_data.action = before
            bpy.data.actions.remove(action)
    events = entry.entries or []
    named = [(e.event_name, float(e.start_time), float(getattr(e, "duration", 0.0) or 0.0))
             for e in events if e.event_name and not getattr(e, "sound_event_name", None)]
    clip.windows = sorted((n, t, t + d) for n, t, d in named if d > 0.0)
    clip.events = sorted((t, n) for n, t, d in named if d <= 0.0)
    clip.sounds = sorted(_sound(e) for e in events if getattr(e, "sound_event_name", None))
    return clip


def _sound(event):
    """Read sound metadata, including class defaults omitted from cooked files."""
    footstep = event.type_name == "CExtAnimFootstepEvent"
    filtered, cooldown = getattr(event, "filter", None), getattr(event, "filterCooldown", None)
    filtered = footstep if filtered is None else bool(filtered)
    cooldown = (0.2 if footstep else 0.0) if cooldown is None else float(cooldown)
    return float(event.start_time), str(event.sound_event_name), str(getattr(event, "bone", "") or ""), footstep, cooldown if filtered else 0.0


def sample(clip: Clip, t: float):
    rot, loc = clip.poses["rot"], clip.poses["loc"]
    if len(rot) < 2:
        return rot[0], loc[0]
    t = min(max(t, 0.0), clip.length)
    i = min(int(t * RATE), len(rot) - 2)
    span = min((i + 1) / RATE, clip.length) - i / RATE
    a = min((t - i / RATE) / span, 1.0) if span > 1e-9 else 1.0
    if a <= 0.0:
        return rot[i], loc[i]
    return rot[i] * (1 - a) + rot[i + 1] * a, loc[i] * (1 - a) + loc[i + 1] * a


def blend(mix):
    """(rot (B, 4), loc (B, 3)) of [(Clip, time, weight)] with the weights summing to 1."""
    rot = loc = ref = None
    for clip, t, w in mix:
        r, l = sample(clip, t)
        if rot is None:
            rot, loc, ref = r * w, l * w, r
            continue
        sign = np.sign((r * ref).sum(axis=1, keepdims=True))
        sign[sign == 0] = 1.0
        rot = rot + r * sign * w
        loc = loc + l * w
    rot = rot / np.maximum(np.linalg.norm(rot, axis=1, keepdims=True), 1e-8)
    return rot, loc


def overlay(rot, loc, top_rot, top_loc, weights):
    """Blend `top` over a pose by per-bone weights (B,)."""
    k = weights[:, None]
    sign = np.sign((rot * top_rot).sum(axis=1, keepdims=True))
    sign[sign == 0] = 1.0
    rot = rot * (1.0 - k) + top_rot * sign * k
    return rot / np.maximum(np.linalg.norm(rot, axis=1, keepdims=True), 1e-8), loc * (1.0 - k) + top_loc * k


def apply(arm, rot, loc):
    bones = arm.pose.bones
    try:
        bones.foreach_set("rotation_quaternion", np.ascontiguousarray(rot, dtype=np.float32).ravel())
        bones.foreach_set("location", np.ascontiguousarray(loc, dtype=np.float32).ravel())
    except (TypeError, RuntimeError):
        for pb, r, l in zip(bones, rot, loc):
            pb.rotation_quaternion = r
            pb.location = l
    bones[0].location = bones[0].location     # tag pose after foreach_set
