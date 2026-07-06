"""Breast dangle settings and solver."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Dict, Mapping, MutableMapping, Sequence, Tuple

from .dyng import IDENTITY_MATRIX, MatrixRows, Vector3, matrix_mul, matrix_translation, transform_point

BREAST_BONE_NAMES = ("l_boob", "r_boob")


@dataclass(frozen=True)
class BreastPreset:
    name: str
    ellipse: Tuple[float, float, float, float]
    vel_damp: float
    bounce_damp: float
    in_acc: float
    inertia_scaler: float
    black_hole: float
    vel_clamp: float
    gravity: float
    movement_bone_weight: float
    rotation_bone_weight: float
    sim_time: float
    start_sim_point_offset: float


REDKIT_BREAST_PRESETS: Tuple[BreastPreset, ...] = (
    BreastPreset("Default_Naked", (0.0, 0.05, 0.4, 0.15), 0.97, 0.98, 1.0, 1.0, 0.002, 200.0, -0.001, 0.05, 1.0, 0.01, 0.0),
    BreastPreset("Natural_Normal", (0.0, 0.03, 0.21, 0.14), 0.88, 0.96, 1.0, 1.5, 0.002, 300.0, -0.001, 0.1, 1.0, 0.01, 0.0),
    BreastPreset("Unnatural", (0.0, -0.02, 0.3, 0.2), 0.95, 0.99, 1.0, 2.0, 0.0008, 150.0, -0.001, 0.13, 1.0, 0.01, 0.0),
    BreastPreset("Clothed", (0.0, 0.03, 0.21, 0.14), 0.8, 0.9, 1.0, 2.0, 0.05, 300.0, -0.001, 0.05, 0.05, 0.01, 0.0),
)
BREAST_PRESETS: Tuple[BreastPreset, ...] = REDKIT_BREAST_PRESETS
CUSTOM_PRESET_NAME = "CUSTOM_PRESET"


def breast_preset_index(value: Any) -> int:
    if value is None:
        return -1
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return -1
    try:
        return int(float(text))
    except ValueError:
        pass
    lowered = text.lower()
    for index, preset in enumerate(BREAST_PRESETS):
        if preset.name.lower() == lowered:
            return index
    if lowered == CUSTOM_PRESET_NAME.lower():
        return len(BREAST_PRESETS)
    return -1


def breast_preset_name(value: Any) -> str:
    index = breast_preset_index(value)
    if 0 <= index < len(BREAST_PRESETS):
        return BREAST_PRESETS[index].name
    if index == len(BREAST_PRESETS):
        return CUSTOM_PRESET_NAME
    return str(value or "")


@dataclass(frozen=True)
class BreastSettings:
    preset: str = CUSTOM_PRESET_NAME
    sim_time: float = 0.01
    ellipse: Tuple[float, float, float, float] = (0.0, 0.0, 0.15, 0.2)
    vel_damp: float = 0.62
    bounce_damp: float = 0.9
    in_acc: float = 0.8
    inertia_scaler: float = 1.1
    black_hole: float = 0.002
    vel_clamp: float = 160.0
    gravity: float = -0.006
    movement_bone_weight: float = 0.15
    rotation_bone_weight: float = 0.3
    start_sim_point_offset: float = 0.08
    blend: float = 1.0

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "BreastSettings":
        preset_value = _first_value(data, "preset", "m_preset")
        preset_index = breast_preset_index(preset_value)
        base = cls()
        if 0 <= preset_index < len(BREAST_PRESETS):
            base = cls.from_preset(BREAST_PRESETS[preset_index])
        elif preset_index == len(BREAST_PRESETS):
            base = cls(preset=CUSTOM_PRESET_NAME)
        elif preset_value not in (None, ""):
            base = cls(preset=str(preset_value))

        values: Dict[str, Any] = {
            "preset": breast_preset_name(preset_value) if preset_value not in (None, "") else base.preset,
            "sim_time": _float_from(data, base.sim_time, "simTime", "m_simTime", "sim_time"),
            "ellipse": _vector4_from(data, base.ellipse, "ellipse", "m_elA", "elA"),
            "vel_damp": _float_from(data, base.vel_damp, "velDamp", "m_velDamp", "vel_damp"),
            "bounce_damp": _float_from(data, base.bounce_damp, "bounceDamp", "m_bounceDamp", "bounce_damp"),
            "in_acc": _float_from(data, base.in_acc, "inAcc", "m_inAcc", "in_acc"),
            "inertia_scaler": _float_from(data, base.inertia_scaler, "inertiaScaler", "m_inertiaScaler", "inertia_scaler"),
            "black_hole": _float_from(data, base.black_hole, "blackHole", "m_blackHole", "black_hole"),
            "vel_clamp": _float_from(data, base.vel_clamp, "velClamp", "m_velClamp", "vel_clamp"),
            "gravity": _float_from(data, base.gravity, "gravity", "m_gravity"),
            "movement_bone_weight": _float_from(data, base.movement_bone_weight, "movementBoneWeight", "m_movementBoneWeight", "movement_bone_weight"),
            "rotation_bone_weight": _float_from(data, base.rotation_bone_weight, "rotationBoneWeight", "m_rotationBoneWeight", "rotation_bone_weight"),
            "start_sim_point_offset": _float_from(data, base.start_sim_point_offset, "startSimPointOffset", "m_startSimPointOffset", "start_sim_point_offset"),
            "blend": _clamp(_float_from(data, base.blend, "blend", "m_blend"), 0.0, 1.0),
        }
        return cls(**values)

    @classmethod
    def from_preset(cls, preset: BreastPreset) -> "BreastSettings":
        return cls(
            preset=preset.name,
            sim_time=preset.sim_time,
            ellipse=preset.ellipse,
            vel_damp=preset.vel_damp,
            bounce_damp=preset.bounce_damp,
            in_acc=preset.in_acc,
            inertia_scaler=preset.inertia_scaler,
            black_hole=preset.black_hole,
            vel_clamp=preset.vel_clamp,
            gravity=preset.gravity,
            movement_bone_weight=preset.movement_bone_weight,
            rotation_bone_weight=preset.rotation_bone_weight,
            start_sim_point_offset=preset.start_sim_point_offset,
        )

    def as_tuple(self) -> Tuple[Any, ...]:
        return (
            self.preset,
            round(self.sim_time, 8),
            tuple(round(v, 8) for v in self.ellipse),
            round(self.vel_damp, 8),
            round(self.bounce_damp, 8),
            round(self.in_acc, 8),
            round(self.inertia_scaler, 8),
            round(self.black_hole, 8),
            round(self.vel_clamp, 8),
            round(self.gravity, 8),
            round(self.movement_bone_weight, 8),
            round(self.rotation_bone_weight, 8),
            round(self.start_sim_point_offset, 8),
            round(self.blend, 8),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "preset": self.preset,
            "simTime": self.sim_time,
            "ellipse": self.ellipse,
            "velDamp": self.vel_damp,
            "bounceDamp": self.bounce_damp,
            "inAcc": self.in_acc,
            "inertiaScaler": self.inertia_scaler,
            "blackHole": self.black_hole,
            "velClamp": self.vel_clamp,
            "gravity": self.gravity,
            "movementBoneWeight": self.movement_bone_weight,
            "rotationBoneWeight": self.rotation_bone_weight,
            "startSimPointOffset": self.start_sim_point_offset,
            "blend": self.blend,
        }


def _first_value(data: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


def _float_from(data: Mapping[str, Any], default: float, *keys: str) -> float:
    value = _first_value(data, *keys)
    if value is None:
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _vector4_from(data: Mapping[str, Any], default: Sequence[float], *keys: str) -> Tuple[float, float, float, float]:
    value = _first_value(data, *keys)
    if value is None:
        return (float(default[0]), float(default[1]), float(default[2]), float(default[3]))
    if isinstance(value, str):
        parts = [part.strip() for part in value.replace(";", ",").split(",")]
        if len(parts) >= 4:
            value = parts
    try:
        values = list(value)
        if len(values) >= 4:
            return (float(values[0]), float(values[1]), float(values[2]), float(values[3]))
    except (TypeError, ValueError):
        pass
    return (float(default[0]), float(default[1]), float(default[2]), float(default[3]))


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, float(value)))


# Keep the inertial signal as a frame impulse, not per-second acceleration:
# imported values are authored at frame scale and over-amplify at preview rates.

_EPS = 1e-8
_PROBE_POINT: Vector3 = (1.0, 0.0, 0.0)
_REST_PULL_GAIN = 12.0
_ROTATION_GAIN = 1.0
_RELAXED_PASSES = 30


def _delta3(a: Vector3, b: Vector3) -> Vector3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _scale3(a: Vector3, s: float) -> Vector3:
    return (a[0] * s, a[1] * s, a[2] * s)


def _add3(a: Vector3, b: Vector3) -> Vector3:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _dot3(a: Vector3, b: Sequence[float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _axis(matrix: Sequence[Sequence[float]], row: int) -> Vector3:
    return (float(matrix[row][0]), float(matrix[row][1]), float(matrix[row][2]))


def _cross3(a: Vector3, b: Vector3) -> Vector3:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _length3(a: Vector3) -> float:
    return math.sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2])


def _rodrigues(v: Vector3, k: Vector3, cos_t: float, sin_t: float) -> Vector3:
    """Rotate v about unit axis k (Rodrigues' rotation)."""
    kv = k[0] * v[0] + k[1] * v[1] + k[2] * v[2]
    cross = (
        k[1] * v[2] - k[2] * v[1],
        k[2] * v[0] - k[0] * v[2],
        k[0] * v[1] - k[1] * v[0],
    )
    one_minus = 1.0 - cos_t
    return (
        v[0] * cos_t + cross[0] * sin_t + k[0] * kv * one_minus,
        v[1] * cos_t + cross[1] * sin_t + k[1] * kv * one_minus,
        v[2] * cos_t + cross[2] * sin_t + k[2] * kv * one_minus,
    )


class _EllipseLagPoint:
    """A velocity-capped 2D lag point confined to the authored ellipse."""

    def __init__(self, settings: BreastSettings):
        self.lat = 0.0
        self.lift = 0.0
        self.vel_lat = 0.0
        self.vel_lift = 0.0
        self.configure(settings)
        self.reset()

    def configure(self, settings: BreastSettings) -> None:
        cx, cy, rx, ry = settings.ellipse
        self.center = (float(cx), float(cy))
        self.radius = (abs(float(rx)), abs(float(ry)))
        self.vel_damp = _clamp(settings.vel_damp, 0.0, 1.0)
        self.bounce_damp = _clamp(settings.bounce_damp, 0.0, 1.0)
        # Intentional role swap vs the field names: inertiaScaler drives the
        # parent-acceleration impulse (see _BreastBone.solve); in_acc sets how
        # hard the ellipse boundary pulls the point back inside. REDkit field
        # names are kept for round-trip; the roles are this model's own.
        self.boundary_strength = _clamp(settings.in_acc, 0.0, 1.0)
        self.inertia = float(settings.inertia_scaler)
        self.rest_pull = max(0.0, float(settings.black_hole)) * _REST_PULL_GAIN
        self.vel_clamp = max(0.0, float(settings.vel_clamp))
        self.gravity = float(settings.gravity)
        self.move_weight = float(settings.movement_bone_weight)
        self.rot_weight = float(settings.rotation_bone_weight)
        self.rest_offset = float(settings.start_sim_point_offset)
        self.sim_time = max(0.0, float(settings.sim_time))

    def reset(self) -> None:
        self.lat = 0.0
        self.lift = -self.rest_offset
        self.vel_lat = 0.0
        self.vel_lift = 0.0001

    def advance(self, dt: float, impulse_lat: float, impulse_lift: float, *, relaxed: bool) -> None:
        dt = max(float(dt) * self.sim_time, _EPS)
        passes = _RELAXED_PASSES if relaxed else 1
        share = 1.0 / float(passes)
        step_dt = dt * share
        for _ in range(passes):
            self._integrate(step_dt, impulse_lat * share, impulse_lift * share)

    def _integrate(self, dt: float, impulse_lat: float, impulse_lift: float) -> None:
        rest_lift = -self.rest_offset
        impulse_lat += -self.lat * self.rest_pull
        impulse_lift += (rest_lift - self.lift) * self.rest_pull

        self.vel_lat = (self.vel_lat + impulse_lat / dt) * self.vel_damp
        self.vel_lift = (self.vel_lift + impulse_lift / dt) * self.vel_damp

        speed = math.hypot(self.vel_lat, self.vel_lift)
        if self.vel_clamp > 0.0 and speed > self.vel_clamp:
            scale = self.vel_clamp / speed
            self.vel_lat *= scale
            self.vel_lift *= scale

        self.lat += self.vel_lat * dt
        self.lift += self.vel_lift * dt
        self._project_inside()

    def _project_inside(self) -> None:
        rx, ry = self.radius
        if rx <= _EPS or ry <= _EPS or self.boundary_strength <= 0.0:
            return
        ox = self.lat - self.center[0]
        oy = self.lift - self.center[1]
        reach = (ox / rx) ** 2 + (oy / ry) ** 2
        if reach <= 1.0:
            return

        shrink = 1.0 / math.sqrt(reach)
        target_lat = self.center[0] + ox * shrink
        target_lift = self.center[1] + oy * shrink
        self.lat += (target_lat - self.lat) * self.boundary_strength
        self.lift += (target_lift - self.lift) * self.boundary_strength

        nx = ox / (rx * rx)
        ny = oy / (ry * ry)
        length = math.hypot(nx, ny)
        if length <= _EPS:
            return
        nx /= length
        ny /= length
        outward = self.vel_lat * nx + self.vel_lift * ny
        if outward > 0.0:
            take = (1.0 + self.bounce_damp) * outward
            self.vel_lat -= take * nx
            self.vel_lift -= take * ny


class _BreastBone:
    """Drives one breast bone from local frame motion and a 2D lag point."""

    def __init__(self, local_transform: Sequence[Sequence[float]], settings: BreastSettings):
        self.local = tuple(tuple(float(v) for v in row) for row in local_transform)  # type: ignore[assignment]
        self.point = _EllipseLagPoint(settings)
        self.probe_position: Vector3 = transform_point(self.local, _PROBE_POINT)
        self.frame_delta: Vector3 = (0.0, 0.0, 0.0)

    def configure(self, settings: BreastSettings) -> None:
        self.point.configure(settings)

    def reset(self, seeded_model: Sequence[Sequence[float]]) -> None:
        self.probe_position = transform_point(seeded_model, _PROBE_POINT)
        self.frame_delta = (0.0, 0.0, 0.0)
        self.point.reset()

    def solve(self, parent: Sequence[Sequence[float]], dt: float, *, reset: bool, relaxed: bool) -> MatrixRows:
        model = matrix_mul(self.local, parent)
        if dt <= 0.0:
            return tuple(tuple(float(v) for v in row) for row in model)  # type: ignore[return-value]

        forward = _axis(model, 0)
        down = _axis(model, 1)
        side = _axis(model, 2)
        lift_axis = _scale3(down, -1.0)

        probe = transform_point(model, _PROBE_POINT)
        frame_delta = _delta3(probe, self.probe_position)
        frame_impulse = _delta3(frame_delta, self.frame_delta)
        self.probe_position = probe
        self.frame_delta = frame_delta

        if reset:
            self.point.reset()
            self.frame_delta = (0.0, 0.0, 0.0)
            impulse_lat = 0.0
            impulse_lift = 0.0
        else:
            impulse_lat = -self.point.inertia * _dot3(frame_impulse, side)
            impulse_lift = -self.point.inertia * _dot3(frame_impulse, lift_axis)
        impulse_lat += self.point.gravity * side[2]
        impulse_lift += self.point.gravity * lift_axis[2]

        self.point.advance(dt, impulse_lat, impulse_lift, relaxed=relaxed)

        return self._compose(model, forward, side, lift_axis)

    def _compose(
        self,
        model: Sequence[Sequence[float]],
        forward: Vector3,
        side: Vector3,
        lift_axis: Vector3,
    ) -> MatrixRows:
        point = self.point
        origin = matrix_translation(model)
        sway = _add3(
            _scale3(side, point.lat),
            _scale3(lift_axis, point.lift + point.rest_offset),
        )
        out = [list(row) for row in model]

        move = point.move_weight
        out[3][0] = origin[0] + sway[0] * move
        out[3][1] = origin[1] + sway[1] * move
        out[3][2] = origin[2] + sway[2] * move
        out[3][3] = 1.0

        reach = _length3(sway)
        if reach > _EPS and point.rot_weight != 0.0:
            axis = _cross3(forward, sway)
            axis_len = _length3(axis)
            if axis_len > _EPS:
                axis = _scale3(axis, 1.0 / axis_len)
                angle = point.rot_weight * reach * _ROTATION_GAIN
                cos_t = math.cos(angle)
                sin_t = math.sin(angle)
                out[0] = list(_rodrigues(_axis(model, 0), axis, cos_t, sin_t)) + [0.0]
                out[1] = list(_rodrigues(_axis(model, 1), axis, cos_t, sin_t)) + [0.0]
                out[2] = list(_rodrigues(_axis(model, 2), axis, cos_t, sin_t)) + [0.0]

        return tuple(tuple(float(v) for v in row) for row in out)  # type: ignore[return-value]


class BreastSimulator:
    """Two-bone breast physics simulator."""

    def __init__(
        self,
        local_transforms: Mapping[str, Sequence[Sequence[float]]],
        settings: BreastSettings | Mapping[str, Any] | None = None,
    ):
        self.settings = settings if isinstance(settings, BreastSettings) else BreastSettings.from_mapping(settings or {})
        self.bones: MutableMapping[str, _BreastBone] = {}
        for name in BREAST_BONE_NAMES:
            local = local_transforms.get(name) or IDENTITY_MATRIX
            self.bones[name] = _BreastBone(local, self.settings)

    def set_settings(self, settings: BreastSettings | Mapping[str, Any]) -> None:
        self.settings = settings if isinstance(settings, BreastSettings) else BreastSettings.from_mapping(settings)
        for bone in self.bones.values():
            bone.configure(self.settings)

    def reset(self, parent_transforms: Mapping[str, Sequence[Sequence[float]]]) -> None:
        for name, bone in self.bones.items():
            parent = parent_transforms.get(name) or IDENTITY_MATRIX
            bone.reset(matrix_mul(bone.local, parent))

    def step(
        self,
        parent_transforms: Mapping[str, Sequence[Sequence[float]]],
        dt: float,
        *,
        reset: bool = False,
        relaxed: bool = False,
    ) -> Dict[str, MatrixRows]:
        dt = max(float(dt), 1e-6)
        outputs: Dict[str, MatrixRows] = {}
        for name, bone in self.bones.items():
            parent = parent_transforms.get(name) or IDENTITY_MATRIX
            outputs[name] = bone.solve(parent, dt, reset=reset, relaxed=relaxed)
        return outputs
