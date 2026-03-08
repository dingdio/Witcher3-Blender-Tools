import logging
import math
import struct
from dataclasses import dataclass

try:
    from .w3_types import Quaternion, w2AnimsFrames
except Exception:
    try:
        # Supports direct module execution from CR2W folder in local debug scripts.
        from w3_types import Quaternion, w2AnimsFrames
    except Exception:
        # Minimal fallback for standalone parser validation without package context.
        class Quaternion:
            def __init__(self, x, y, z, w):
                self.X = x
                self.Y = y
                self.Z = z
                self.W = w

        class w2AnimsFrames:
            def __init__(
                self,
                id,
                BoneName,
                position_dt,
                position_numFrames,
                positionFrames,
                rotation_dt,
                rotation_numFrames,
                rotationFrames,
                scale_dt,
                scale_numFrames,
                scaleFrames,
                rotationFramesQuat,
            ):
                self.id = id
                self.BoneName = BoneName
                self.position_dt = position_dt
                self.position_numFrames = position_numFrames
                self.positionFrames = positionFrames
                self.rotation_dt = rotation_dt
                self.rotation_numFrames = rotation_numFrames
                self.rotationFrames = rotationFrames
                self.scale_dt = scale_dt
                self.scale_numFrames = scale_numFrames
                self.scaleFrames = scaleFrames
                self.rotationFramesQuat = rotationFramesQuat

log = logging.getLogger(__name__)


# Matches HavokLib enum ordering.
STT_DYNAMIC = 0
STT_STATIC = 1
STT_IDENTITY = 2

QT_8BIT = 0
QT_16BIT = 1
QT_32BIT = 2
QT_40BIT = 3
QT_48BIT = 4
QT_UNCOMPRESSED = 5

TT_POS_X = 0
TT_POS_Y = 1
TT_POS_Z = 2
TT_ROTATION = 3
TT_SCALE_X = 4
TT_SCALE_Y = 5
TT_SCALE_Z = 6


@dataclass
class TransformMask:
    quantization_types: int
    position_types: int
    rotation_types: int
    scale_types: int

    @classmethod
    def from_bytes(cls, data, offset):
        q, p, r, s = struct.unpack_from("<BBBB", data, offset)
        return cls(q, p, r, s)

    def get_pos_quantization_type(self):
        return self.quantization_types & 0x03

    def get_rot_quantization_type(self):
        return ((self.quantization_types >> 2) & 0x0F) + 2

    def get_scale_quantization_type(self):
        return (self.quantization_types >> 6) & 0x03

    def _flags_sub_track(self, flags, axis_index):
        is_static = (flags >> axis_index) & 0x01
        is_spline = (flags >> (axis_index + 4)) & 0x01
        if is_static:
            return STT_STATIC
        if is_spline:
            return STT_DYNAMIC
        return STT_IDENTITY

    def get_sub_track_type(self, track_type):
        if track_type == TT_POS_X:
            return self._flags_sub_track(self.position_types, 0)
        if track_type == TT_POS_Y:
            return self._flags_sub_track(self.position_types, 1)
        if track_type == TT_POS_Z:
            return self._flags_sub_track(self.position_types, 2)
        if track_type == TT_ROTATION:
            if self.rotation_types & 0xF0:
                return STT_DYNAMIC
            if self.rotation_types & 0x0F:
                return STT_STATIC
            return STT_IDENTITY
        if track_type == TT_SCALE_X:
            return self._flags_sub_track(self.scale_types, 0)
        if track_type == TT_SCALE_Y:
            return self._flags_sub_track(self.scale_types, 1)
        if track_type == TT_SCALE_Z:
            return self._flags_sub_track(self.scale_types, 2)
        return STT_IDENTITY


class SplineStaticTrack:
    def __init__(self, item):
        self.item = item

    def get_value(self, _local_frame):
        return self.item

    def is_static(self):
        return True


class SplineDynamicTrackVector:
    def __init__(self, tracks, knots, degree):
        self.tracks = tracks
        self.knots = knots
        self.degree = degree

    def get_value(self, local_frame):
        out = [0.0, 0.0, 0.0]
        knot_span = -1

        for axis in range(3):
            c_points = self.tracks[axis]
            if len(c_points) <= 1:
                out[axis] = c_points[0]
                continue
            if knot_span < 0:
                knot_span = _find_knot_span(
                    self.degree,
                    local_frame,
                    len(c_points),
                    self.knots,
                )
            out[axis] = _get_single_point(
                knot_span,
                self.degree,
                local_frame,
                self.knots,
                c_points,
            )

        return out

    def is_static(self):
        return not self.knots


class SplineDynamicTrackQuat:
    def __init__(self, track, knots, degree):
        self.track = track
        self.knots = knots
        self.degree = degree

    def get_value(self, local_frame):
        knot_span = _find_knot_span(
            self.degree,
            local_frame,
            len(self.track),
            self.knots,
        )
        return _get_single_point(
            knot_span,
            self.degree,
            local_frame,
            self.knots,
            self.track,
        )

    def is_static(self):
        return False


def _align_relative(offset, alignment=4):
    result = offset & (alignment - 1)
    if result == 0:
        return offset
    return offset + (alignment - result)


def _read_u8(data, offset):
    return data[offset], offset + 1


def _read_u16(data, offset):
    return struct.unpack_from("<H", data, offset)[0], offset + 2


def _read_f32(data, offset):
    return struct.unpack_from("<f", data, offset)[0], offset + 4


def _normalize_quat(x, y, z, w):
    length = math.sqrt((x * x) + (y * y) + (z * z) + (w * w))
    if length <= 1e-8:
        return 0.0, 0.0, 0.0, 1.0
    inv = 1.0 / length
    return x * inv, y * inv, z * inv, w * inv


def _quat_compute_w(x, y, z):
    w2 = 1.0 - ((x * x) + (y * y) + (z * z))
    if w2 < 0.0:
        w2 = 0.0
    return math.sqrt(w2)


def _read_32bit_quat(data, offset):
    c_val = struct.unpack_from("<I", data, offset)[0]

    r = float((c_val >> 18) & ((1 << 10) - 1)) * (1.0 / 1023.0)
    r = 1.0 - (r * r)

    phi_theta = float(c_val & 0x3FFFF)
    phi = math.floor(math.sqrt(phi_theta))
    theta = 0.0

    if phi > 0.0:
        theta = (math.pi * 0.25) * (phi_theta - (phi * phi)) / phi
        phi = (math.pi * 0.5 / 511.0) * phi

    magnitude = math.sqrt(max(1.0 - (r * r), 0.0))
    s_phi = math.sin(phi)
    c_phi = math.cos(phi)
    s_theta = math.sin(theta)
    c_theta = math.cos(theta)

    x = s_phi * c_theta * magnitude
    y = s_phi * s_theta * magnitude
    z = c_phi * magnitude
    w = r

    if c_val & 0x10000000:
        x = -x
    if c_val & 0x20000000:
        y = -y
    if c_val & 0x40000000:
        z = -z
    if c_val & 0x80000000:
        w = -w

    return (x, y, z, w), offset + 4


def _read_40bit_quat(data, offset):
    c_val = int.from_bytes(data[offset : offset + 5], "little")

    v0 = (c_val >> 0) & 0xFFF
    v1 = (c_val >> 12) & 0xFFF
    v2 = (c_val >> 24) & 0xFFF

    fractal = 0.000345436
    x = (v0 - 2049) * fractal
    y = (v1 - 2049) * fractal
    z = (v2 - 2049) * fractal
    w = _quat_compute_w(x, y, z)

    if (c_val >> 38) & 0x1:
        w = -w

    result_shift = (c_val >> 36) & 0x3

    if result_shift == 0:
        out = (w, x, y, z)
    elif result_shift == 1:
        out = (x, w, y, z)
    elif result_shift == 2:
        out = (x, y, w, z)
    else:
        out = (x, y, z, w)

    return out, offset + 5


def _read_48bit_quat(data, offset):
    c_x, c_y, c_z = struct.unpack_from("<HHH", data, offset)

    result_shift = ((c_y >> 14) & 0x2) | ((c_x >> 15) & 0x1)
    r_sign = (c_z >> 15) != 0

    fractal = 0.000043161
    x = ((c_x & 0x7FFF) - 16383) * fractal
    y = ((c_y & 0x7FFF) - 16383) * fractal
    z = ((c_z & 0x7FFF) - 16383) * fractal
    w = _quat_compute_w(x, y, z)
    if r_sign:
        w = -w

    if result_shift == 0:
        out = (w, x, y, z)
    elif result_shift == 1:
        out = (x, w, y, z)
    elif result_shift == 2:
        out = (x, y, w, z)
    else:
        out = (x, y, z, w)

    return out, offset + 6


def read_quantized_quat(q_type, data, offset):
    if q_type == QT_32BIT:
        return _read_32bit_quat(data, offset)
    if q_type == QT_40BIT:
        return _read_40bit_quat(data, offset)
    if q_type == QT_48BIT:
        return _read_48bit_quat(data, offset)
    if q_type == QT_UNCOMPRESSED:
        q = struct.unpack_from("<4f", data, offset)
        return q, offset + 16
    return (0.0, 0.0, 0.0, 1.0), offset


def _find_knot_span(degree, value, c_points_size, knots):
    if c_points_size <= 1:
        return 0

    if value >= knots[c_points_size]:
        return c_points_size - 1

    low = degree
    high = c_points_size
    mid = (low + high) // 2

    while value < knots[mid] or value >= knots[mid + 1]:
        if value < knots[mid]:
            high = mid
        else:
            low = mid
        mid = (low + high) // 2

    return mid


def _point_scale(point, scalar):
    if isinstance(point, tuple):
        return tuple(c * scalar for c in point)
    return point * scalar


def _point_add(a, b):
    if isinstance(a, tuple):
        return tuple(x + y for x, y in zip(a, b))
    return a + b


def _get_single_point(knot_span_index, degree, frame, knots, c_points):
    n = [0.0] * max(5, degree + 1)
    n[0] = 1.0

    for i in range(1, degree + 1):
        for j in range(i - 1, -1, -1):
            denominator = knots[knot_span_index + i - j] - knots[knot_span_index - j]
            a = 0.0 if denominator == 0 else (frame - knots[knot_span_index - j]) / denominator
            tmp = n[j] * a
            n[j + 1] += n[j] - tmp
            n[j] = tmp

    if isinstance(c_points[0], tuple):
        ret = tuple(0.0 for _ in range(len(c_points[0])))
    else:
        ret = 0.0

    for i in range(0, degree + 1):
        ret = _point_add(ret, _point_scale(c_points[knot_span_index - i], n[i]))

    return ret


class TransformSplineBlock:
    def __init__(self, data, block_offset, num_tracks, num_float_tracks):
        self._tracks = []
        self._assign(data, block_offset, num_tracks, num_float_tracks)

    def _make_vector_track(self, data, rel, mask, q_type, default_value, axis_types):
        sub_types = [mask.get_sub_track_type(axis_types[0]),
                     mask.get_sub_track_type(axis_types[1]),
                     mask.get_sub_track_type(axis_types[2])]
        use_spline = STT_DYNAMIC in sub_types

        if use_spline:
            num_items, rel = _read_u16(data, rel)
            degree, rel = _read_u8(data, rel)

            knot_size = num_items + degree + 2
            knots = list(data[rel : rel + knot_size])
            rel += knot_size
            rel = _align_relative(rel, 4)

            extremes = [(0.0, 0.0)] * 3
            tracks = [[], [], []]

            for axis in range(3):
                t_type = sub_types[axis]
                if t_type == STT_DYNAMIC:
                    min_v = struct.unpack_from("<f", data, rel)[0]
                    max_v = struct.unpack_from("<f", data, rel + 4)[0]
                    rel += 8
                    extremes[axis] = (min_v, max_v)
                    tracks[axis] = [0.0] * (num_items + 1)
                elif t_type == STT_STATIC:
                    value, rel = _read_f32(data, rel)
                    tracks[axis] = [value]
                else:
                    tracks[axis] = [default_value]

            for i in range(num_items + 1):
                for axis in range(3):
                    if sub_types[axis] != STT_DYNAMIC:
                        continue
                    if q_type == QT_8BIT:
                        q_val, rel = _read_u8(data, rel)
                        frac = float(q_val) * (1.0 / 255.0)
                    else:
                        q_val, rel = _read_u16(data, rel)
                        frac = float(q_val) * (1.0 / 65535.0)

                    min_v, max_v = extremes[axis]
                    tracks[axis][i] = min_v + (max_v - min_v) * frac

            rel = _align_relative(rel, 4)
            return SplineDynamicTrackVector(tracks, knots, degree), rel

        out = [default_value, default_value, default_value]
        for axis in range(3):
            if sub_types[axis] == STT_STATIC:
                value, rel = _read_f32(data, rel)
                out[axis] = value
        return SplineStaticTrack(out), rel

    def _assign(self, data, block_offset, num_tracks, num_float_tracks):
        masks = []
        rel = block_offset
        for i in range(num_tracks):
            masks.append(TransformMask.from_bytes(data, rel + i * 4))

        rel += (num_tracks * 4) + num_float_tracks
        rel = _align_relative(rel, 4)

        for mask in masks:
            track = {}

            track["pos"], rel = self._make_vector_track(
                data,
                rel,
                mask,
                mask.get_pos_quantization_type(),
                0.0,
                (TT_POS_X, TT_POS_Y, TT_POS_Z),
            )

            rot_type = mask.get_sub_track_type(TT_ROTATION)
            if rot_type == STT_DYNAMIC:
                num_items, rel = _read_u16(data, rel)
                degree, rel = _read_u8(data, rel)
                knot_size = num_items + degree + 2
                knots = list(data[rel : rel + knot_size])
                rel += knot_size

                q_type = mask.get_rot_quantization_type()
                if q_type == QT_48BIT:
                    rel = _align_relative(rel, 2)
                elif q_type == QT_32BIT:
                    rel = _align_relative(rel, 4)

                values = []
                for _ in range(num_items + 1):
                    quat, rel = read_quantized_quat(q_type, data, rel)
                    values.append(quat)
                track["rot"] = SplineDynamicTrackQuat(values, knots, degree)
            elif rot_type == STT_STATIC:
                quat, rel = read_quantized_quat(mask.get_rot_quantization_type(), data, rel)
                track["rot"] = SplineStaticTrack(quat)
            else:
                track["rot"] = SplineStaticTrack((0.0, 0.0, 0.0, 1.0))

            rel = _align_relative(rel, 4)

            track["scale"], rel = self._make_vector_track(
                data,
                rel,
                mask,
                mask.get_scale_quantization_type(),
                1.0,
                (TT_SCALE_X, TT_SCALE_Y, TT_SCALE_Z),
            )

            self._tracks.append(track)

    def get_value(self, track_id, local_frame):
        t = self._tracks[track_id]
        return (
            t["pos"].get_value(local_frame),
            t["rot"].get_value(local_frame),
            t["scale"].get_value(local_frame),
        )


def _fallback_dt(duration, num_frames):
    if num_frames > 1 and duration > 0.0:
        return duration / float(num_frames - 1)
    return 0.0


def decompress_spline_animation(
    data,
    num_tracks,
    num_float_tracks,
    num_frames,
    duration,
    frame_duration,
    block_duration,
    block_inverse_duration,
    block_offsets,
    bone_names=None,
):
    if not data or num_tracks <= 0:
        return []

    if not block_offsets:
        block_offsets = [0]

    blocks = []
    for boff in block_offsets:
        try:
            blocks.append(TransformSplineBlock(data, int(boff), num_tracks, num_float_tracks))
        except Exception as exc:
            log.warning("Failed to parse spline block at offset %s: %s", boff, exc)
            continue

    if not blocks:
        return []

    num_frames = max(1, int(num_frames))
    if frame_duration <= 0.0:
        frame_duration = _fallback_dt(duration, num_frames)
    if frame_duration <= 0.0:
        frame_duration = 1.0 / 30.0

    frame_rate = (1.0 / frame_duration) if frame_duration > 0.0 else 30.0
    if block_inverse_duration <= 0.0 and block_duration > 0.0:
        block_inverse_duration = 1.0 / block_duration

    bones = []
    for track_id in range(num_tracks):
        pos_frames = []
        rot_frames = []
        scale_frames = []

        for frame_idx in range(num_frames):
            sample_time = frame_idx * frame_duration

            if block_inverse_duration > 0.0:
                block_id = int(sample_time * block_inverse_duration)
            else:
                block_id = 0

            if block_id < 0:
                block_id = 0
            elif block_id >= len(blocks):
                block_id = len(blocks) - 1

            local_time = sample_time - (float(block_id) * block_duration)
            if local_time < 0.0:
                local_time = 0.0

            local_frame = local_time * frame_rate
            pos, rot, scale = blocks[block_id].get_value(track_id, local_frame)
            rot_n = _normalize_quat(rot[0], rot[1], rot[2], rot[3])

            pos_frames.append([float(pos[0]), float(pos[1]), float(pos[2])])
            rot_frames.append(Quaternion(rot_n[0], rot_n[1], rot_n[2], rot_n[3]))
            scale_frames.append([float(scale[0]), float(scale[1]), float(scale[2])])

        bone_name = track_id
        if bone_names and track_id < len(bone_names):
            bone_name = bone_names[track_id]

        bones.append(
            w2AnimsFrames(
                track_id,
                BoneName=bone_name,
                position_dt=frame_duration,
                position_numFrames=num_frames,
                positionFrames=pos_frames,
                rotation_dt=frame_duration,
                rotation_numFrames=num_frames,
                rotationFrames=rot_frames,
                scale_dt=frame_duration,
                scale_numFrames=num_frames,
                scaleFrames=scale_frames,
                rotationFramesQuat=rot_frames,
            )
        )

    return bones
