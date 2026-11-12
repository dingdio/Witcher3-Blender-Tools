"""Sample and generate Trajectory curves without changing body animation."""
import math

import bpy
from mathutils import Matrix, Quaternion, Vector

from . import motion_extraction as mx
from .action_compat import assign_action, iter_action_fcurves, remove_action_fcurve

TRAJECTORY = "Trajectory"
ROOT = "Root"
DEFAULT_SOURCE = "pelvis"
ROTATION_PROPS = ("rotation_quaternion", "rotation_euler", "rotation_axis_angle")


def find_bone(armature, name):
    bones = armature.pose.bones
    if name in bones:
        return bones[name]
    lower = name.lower()
    return next((b for b in bones if b.name.lower() == lower), None)


def action_frames(action):
    start, end = (int(v) for v in action.frame_range)  # as the set exporter truncates
    return list(range(start, end + 1))


def clip_duration(scene, frames):
    fps = scene.render.fps / (scene.render.fps_base or 1.0)
    return (len(frames) - 1) / fps if fps > 0 else 0.0


def _delta(pose_bone):
    """Return the armature-space pose relative to rest."""
    return pose_bone.matrix @ pose_bone.bone.matrix_local.inverted()


def _root_delta(armature):
    root = find_bone(armature, ROOT)
    return _delta(root) if root is not None else Matrix.Identity(4)


def sample_bone(context, armature, bone_name, frames):
    """Sample (position, quaternion) relative to Root, whose track the engine ignores."""
    scene = context.scene
    current, subframe = scene.frame_current, scene.frame_subframe
    pb = find_bone(armature, bone_name)
    if pb is None:
        raise ValueError(f"Armature has no '{bone_name}' bone")
    out = []
    try:
        for f in frames:
            scene.frame_set(f)
            root_inv = _root_delta(armature).inverted()
            # Head travel; the delta matrix's own translation swings when a bone with an offset head tilts.
            position = (root_inv @ pb.matrix).translation - pb.bone.matrix_local.translation
            out.append((position, (root_inv @ _delta(pb)).to_quaternion().normalized()))
    finally:
        scene.frame_set(current, subframe=subframe)
    return out


def motion_from_trajectory(context, armature, frames):
    """Return uncompressed (x, y, z, yaw) motion frames."""
    samples = sample_bone(context, armature, TRAJECTORY, frames)
    return mx.extract([tuple(p) for p, _ in samples], [tuple(q) for _, q in samples])


def build_motion_extraction(context, armature, action, eps=mx.DEFAULT_EPS, flags=None, frames=None):
    """Return (compressed motion or None, raw frames, duration)."""
    frames = frames or action_frames(action)
    with using_action(armature, action):
        raw = motion_from_trajectory(context, armature, frames)
    duration = clip_duration(context.scene, frames)
    return mx.compress(raw, duration, eps=eps, flags=flags), raw, duration


def _unwrap(values):
    out = []
    for v in values:
        if out:
            while v - out[-1] > math.pi:
                v -= 2 * math.pi
            while v - out[-1] < -math.pi:
                v += 2 * math.pi
        out.append(v)
    return out


def _smooth(values, radius):
    if radius <= 0 or len(values) < 3:
        return list(values)
    out = []
    for i in range(len(values)):
        lo, hi = max(0, i - radius), min(len(values), i + radius + 1)
        out.append(sum(values[lo:hi]) / (hi - lo))
    # Preserve loop endpoints and travel distance.
    out[0], out[-1] = values[0], values[-1]
    return out


def plan_trajectory(samples, path="LINEAR", rotation="KEEP", smooth=6, height=False):
    """Return (location, yaw) frames from body samples."""
    n = len(samples)
    if not n:
        return []
    pos = [Vector((p.x, p.y, p.z if height else 0.0)) for p, _ in samples]
    initial = samples[0][1].inverted()
    heading = _unwrap([mx.twist_z(tuple(q @ initial)) for _, q in samples])
    span = max(n - 1, 1)

    if path == "LINEAR":
        locs = [pos[0].lerp(pos[-1], i / span) for i in range(n)]
    elif path == "SMOOTH":
        radius = int(smooth)
        locs = [Vector(v) for v in zip(*(_smooth([p[a] for p in pos], radius) for a in range(3)))]
    else:
        locs = pos
    if not height:
        for v in locs:
            v.z = 0.0

    if rotation == "LINEAR":
        yaws = [heading[-1] * i / span for i in range(n)]
    elif rotation == "SMOOTH":
        yaws = _smooth(heading, int(smooth))
    elif rotation == "NONE":
        yaws = [0.0] * n
    else:
        yaws = [None] * n
    return list(zip(locs, yaws))


def write_trajectory(context, armature, action, plan, frames):
    """Replace Trajectory keys with the planned location and yaw."""
    scene = context.scene
    pb = find_bone(armature, TRAJECTORY)
    if pb is None:
        raise ValueError("Armature has no Trajectory bone")
    rest = pb.bone.matrix_local
    prop = {'QUATERNION': "rotation_quaternion", 'AXIS_ANGLE': "rotation_axis_angle"}.get(pb.rotation_mode, "rotation_euler")
    path = pb.path_from_id() + "."
    write_rotation = any(yaw is not None for _, yaw in plan)
    paths = {path + "location"}
    if write_rotation:
        paths.update(path + p for p in ROTATION_PROPS)
    with using_action(armature, action):
        for curve in [c for c in iter_action_fcurves(action, target=armature) if c.data_path in paths]:
            remove_action_fcurve(action, curve, target=armature)
        current, subframe = scene.frame_current, scene.frame_subframe
        try:
            for f, (loc, yaw) in zip(frames, plan):
                scene.frame_set(f)
                rotation = (Quaternion((0, 0, 1), yaw).to_matrix().to_4x4() if yaw is not None
                            else (_root_delta(armature).inverted() @ _delta(pb)).to_quaternion().to_matrix().to_4x4())
                local = Matrix.Translation(loc) @ rotation
                pb.matrix = _root_delta(armature) @ local @ rest
                pb.keyframe_insert("location", frame=f, group=pb.name)
                if write_rotation:
                    pb.keyframe_insert(prop, frame=f, group=pb.name)
        finally:
            scene.frame_set(current, subframe=subframe)
    for curve in iter_action_fcurves(action, target=armature):
        if curve.data_path in paths:
            for k in curve.keyframe_points:
                k.interpolation = 'LINEAR'


class using_action:
    """Isolate an action, then restore animation settings and pose."""

    def __init__(self, armature, action):
        self.armature, self.action = armature, action

    def __enter__(self):
        data = self.armature.animation_data_create()
        if data.use_tweak_mode:
            raise ValueError("Exit NLA tweak mode before sampling an action")
        self.previous, self.slot = data.action, data.action_slot
        self.settings = {p: getattr(data, p) for p in ("use_nla", "action_blend_type", "action_influence")}
        self.pose = [(pb, pb.matrix_basis.copy()) for pb in self.armature.pose.bones]
        self.object_matrix = self.armature.matrix_basis.copy()
        self.scene = bpy.context.scene
        self.frame = self.scene.frame_current, self.scene.frame_subframe
        data.use_nla = False
        data.action_blend_type = 'REPLACE'
        data.action_influence = 1.0
        if self.action is None:
            data.action = None
        elif data.action != self.action:
            assign_action(self.armature, self.action)
        for pb, _ in self.pose:
            pb.matrix_basis = Matrix.Identity(4)
        return self

    def __exit__(self, *exc):
        data = self.armature.animation_data
        data.action = self.previous
        if self.previous is not None:
            data.action_slot = self.slot
        for prop, value in self.settings.items():
            setattr(data, prop, value)
        for pb, matrix in self.pose:
            pb.matrix_basis = matrix
        self.armature.matrix_basis = self.object_matrix
        self.scene.frame_set(self.frame[0], subframe=self.frame[1])


def generate_trajectory(context, armature, action, source=DEFAULT_SOURCE, path="LINEAR", rotation="KEEP",
                        smooth=6, height=False):
    """Replace Trajectory keys from source samples; return the plan."""
    frames = action_frames(action)
    pb = find_bone(armature, TRAJECTORY)
    if pb is None:
        raise ValueError("Armature has no Trajectory bone")
    rotation_paths = {pb.path_from_id(p) for p in ROTATION_PROPS}
    if rotation != "KEEP" and any(
            c.data_path in rotation_paths for c in iter_action_fcurves(action, target=armature)):
        raise ValueError("Trajectory already has rotation keys; keep them or edit them in the Graph Editor")
    with using_action(armature, action):
        samples = sample_bone(context, armature, source, frames)
    plan = plan_trajectory(samples, path=path, rotation=rotation, smooth=smooth, height=height)
    write_trajectory(context, armature, action, plan, frames)
    return plan
