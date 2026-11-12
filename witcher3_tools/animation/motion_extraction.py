"""CDPR motion extraction: REAAnimImporter and CLineMotionExtraction2. Quaternions use (w, x, y, z)."""
import math

import numpy as np

FLAG_X, FLAG_Y, FLAG_Z, FLAG_YAW = 1, 2, 4, 8
AXES = ((FLAG_X, "X"), (FLAG_Y, "Y"), (FLAG_Z, "Z"), (FLAG_YAW, "Yaw"))
TRANSLATION_THRESHOLD = 0.01   # metres
YAW_THRESHOLD = 0.33           # radians (~19 degrees)
DEFAULT_EPS = 0.03
DEFAULT_MIN_KNOTS = 2
MAX_KNOT_DISTANCE = 255        # deltaTimes are Uint8


def _q_mul(a, b):
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return (aw * bw - ax * bx - ay * by - az * bz,
            aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw)


def _q_inv(q):
    w, x, y, z = q
    n = w * w + x * x + y * y + z * z
    return (w / n, -x / n, -y / n, -z / n)


def _q_rotate(q, v):
    w, x, y, z = _q_mul(_q_mul(q, (0.0, v[0], v[1], v[2])), _q_inv(q))
    return (x, y, z)


def twist_z(q):
    """RedQuaternion::DecomposeRestAxis about +Z, in [-pi, pi]."""
    w, _, _, z = q
    if abs(w) < 1e-12 and abs(z) < 1e-12:
        return 0.0
    angle = 2.0 * math.atan2(z, w)
    if angle > math.pi:
        angle -= 2.0 * math.pi
    elif angle < -math.pi:
        angle += 2.0 * math.pi
    return angle


def extract(positions, rotations):
    """Return (x, y, z, yaw) frames relative to the first Trajectory transform."""
    if not positions:
        return []
    p0, inv_r0 = positions[0], _q_inv(rotations[0])
    frames, prev = [], 0.0
    for k, (p, r) in enumerate(zip(positions, rotations)):
        t = _q_rotate(inv_r0, (p[0] - p0[0], p[1] - p0[1], p[2] - p0[2]))
        yaw = twist_z(_q_mul(inv_r0, r))
        if k:
            while yaw - prev > math.pi:
                yaw -= 2.0 * math.pi
            while yaw - prev < -math.pi:
                yaw += 2.0 * math.pi
        frames.append((t[0], t[1], t[2], yaw))
        prev = yaw
    return frames


def axis_flags(frames, translation_threshold=TRANSLATION_THRESHOLD, yaw_threshold=YAW_THRESHOLD):
    flags = 0
    for f in frames:
        for i, (flag, _) in enumerate(AXES):
            if abs(f[i]) > (yaw_threshold if flag == FLAG_YAW else translation_threshold):
                flags |= flag
    return flags


def _costs(pts, prev, cur, nxt):
    """CalcDistPointToLine squared distances, preserving the engine's float32 order."""
    f32 = np.float32
    dims = pts.shape[1]
    len_sq = np.zeros(len(cur), f32)
    dot = np.zeros(len(cur), f32)
    for i in range(dims):
        d = pts[nxt, i] - pts[prev, i]
        len_sq = len_sq + d * d
        dot = dot + (pts[cur, i] - pts[prev, i]) * d
    dot = dot / len_sq
    ret = np.zeros(len(cur), f32)
    for i in range(dims):
        val = (pts[cur, i] - pts[prev, i]) - (pts[nxt, i] - pts[prev, i]) * dot
        ret = ret + val * val
    return ret


def _keep_points(points, eps, min_knots, max_knot_distance):
    pts = np.asarray(points, dtype=np.float32)
    used = np.ones(len(pts), bool)
    eps = np.float32(eps)
    while True:
        idx = np.flatnonzero(used)
        if len(idx) < 3:
            break
        prev, cur, nxt = idx[:-2], idx[1:-1], idx[2:]
        costs = _costs(pts, prev, cur, nxt)
        if max_knot_distance > 0:
            costs[(nxt - prev) > max_knot_distance] = np.inf
        best = int(np.argmin(costs))
        if not np.isfinite(costs[best]) or np.sqrt(costs[best]) > eps:
            break
        if len(idx) == min_knots or len(idx) <= 2:
            break
        used[cur[best]] = False
    return used.tolist()


def compress(frames, duration, eps=DEFAULT_EPS, min_knots=DEFAULT_MIN_KNOTS, max_knot_distance=MAX_KNOT_DISTANCE,
             flags=None):
    """Build CLineMotionExtraction2 from evenly spaced frames; flags=None detects axes."""
    if len(frames) < 2 or duration <= 0.0:
        return None
    flags = axis_flags(frames) if flags is None else flags
    active = [i for i, (flag, _) in enumerate(AXES) if flags & flag]
    if not active:
        return None
    step = np.float32(duration) / np.float32(len(frames) - 1)
    points = [(step * np.float32(i),) + tuple(f[a] for a in active) for i, f in enumerate(frames)]
    used = _keep_points(points, eps, min_knots, max_knot_distance)
    kept = [i for i, u in enumerate(used) if u]
    return {
        "duration": float(duration),
        "frames": [frames[i][a] for i in kept for a in active],
        "delta_times": [b - a for a, b in zip(kept, kept[1:])],
        "flags": flags,
        "uncompressed_frames": [tuple(f) for f in frames],
    }


def keys(me):
    """Return (time, (x, y, z, yaw)) keys."""
    active = [i for i, (flag, _) in enumerate(AXES) if me["flags"] & flag]
    deltas = me.get("delta_times", me.get("deltaTimes", []))
    total = sum(deltas)
    slice_ = me["duration"] / total if total and me["duration"] > 0 else 1.0 / 30.0
    out, frame = [], 0
    for k in range(len(deltas) + 1):
        value = [0.0, 0.0, 0.0, 0.0]
        for j, a in enumerate(active):
            value[a] = me["frames"][k * len(active) + j]
        out.append((frame * slice_, tuple(value)))
        if k < len(deltas):
            frame += deltas[k]
    return out


def sample(me, t):
    """Sample (x, y, z, yaw) with engine linear interpolation."""
    ks = keys(me)
    if t <= ks[0][0]:
        return ks[0][1]
    if t >= ks[-1][0]:
        return ks[-1][1]
    for (ta, a), (tb, b) in zip(ks, ks[1:]):
        if t <= tb:
            w = (t - ta) / (tb - ta) if tb > ta else 0.0
            return tuple(av + (bv - av) * w for av, bv in zip(a, b))
    return ks[-1][1]


def max_error(me, frames, duration):
    """Return the largest per-axis compression error."""
    if len(frames) < 2:
        return 0.0
    step = duration / (len(frames) - 1)
    return max(max(abs(a - b) for a, b in zip(sample(me, i * step), f)) for i, f in enumerate(frames))


def summary(frames, duration):
    """Summarize travel, speed and turn."""
    if not frames:
        return {"distance": 0.0, "speed": 0.0, "turn_deg": 0.0, "height": 0.0, "end": (0.0, 0.0, 0.0, 0.0)}
    end = frames[-1]
    distance = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(frames, frames[1:]))
    return {
        "distance": distance,
        "speed": distance / duration if duration > 0 else 0.0,
        "turn_deg": math.degrees(end[3]),
        "height": end[2],
        "end": tuple(end),
        "path_length": distance,
    }


def flag_names(flags):
    return [name for flag, name in AXES if flags & flag]
