"""Parse Witcher 3 world environment data without Blender dependencies."""

from __future__ import annotations

import csv
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Sequence, TextIO


DAY_SECONDS = 24.0 * 60.0 * 60.0
Vec3 = tuple[float, float, float]
Vec4 = tuple[float, float, float, float]


_CURVE_ENTRY_NAME_ALIASES = {
    "me": "time",
    "time": "time",
    "ntrolpoint": "controlPoint",
    "controlpoint": "controlPoint",
    "lue": "value",
    "value": "value",
    "rvetypel": "curveTypeL",
    "curvetypel": "curveTypeL",
    "rvetype0": "curveTypeL",
    "curvetype0": "curveTypeL",
    "rvetyper": "curveTypeR",
    "curvetyper": "curveTypeR",
    "rvetype1": "curveTypeR",
    "curvetype1": "curveTypeR",
}


def normalize_curve_entry_name(name: str) -> str:
    """Normalize cooked and uncooked ``SCurveDataEntry`` field names.

    Some source files contain names missing their first two letters
    (``me``, ``ntrolPoint``, ``lue`` and ``rveTypeL/R``).  The CR2W parser
    correctly retains those names, so normalization belongs at this adapter
    boundary rather than in the generic reader.
    """

    text = str(name or "").strip()
    return _CURVE_ENTRY_NAME_ALIASES.get(text.lower(), text)


def normalize_depot_path(value: Any) -> str:
    text = str(value or "").strip().strip('"')
    if not text:
        return ""
    return text.replace("/", "\\").lstrip("\\")


def _children(value: Any) -> list[Any]:
    if value is None:
        return []
    for attr in ("PROPS", "MoreProps", "More"):
        children = getattr(value, attr, None)
        if children is not None:
            return list(children)
    return []


def _array_children(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    elements = getattr(value, "elements", None)
    if elements is not None:
        return list(elements)
    chunks = getattr(value, "chunks", None)
    elements = getattr(chunks, "elements", None) if chunks is not None else None
    if elements is not None:
        return list(elements)
    return _children(value)


def _prop_name(value: Any) -> str:
    return str(
        getattr(value, "theName", "")
        or getattr(value, "elementName", "")
        or ""
    ).strip()


def _prop_type(value: Any) -> str:
    return str(getattr(value, "theType", "") or "").strip()


def _field_key(name: str) -> str:
    text = str(name or "").strip().lower()
    return text[2:] if text.startswith("m_") else text


def _get_field(value: Any, *names: str) -> Any:
    if value is None:
        return None

    getter = getattr(value, "GetVariableByName", None)
    if getter is not None:
        for name in names:
            for candidate in (name, name[2:] if name.startswith("m_") else f"m_{name}"):
                try:
                    result = getter(candidate)
                except Exception:
                    result = None
                if result is not None:
                    return result

    keys = {_field_key(name) for name in names}
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _field_key(key) in keys:
                return item
    for item in _children(value):
        if _field_key(_prop_name(item)) in keys:
            return item
    for name in names:
        for candidate in (name, name[2:] if name.startswith("m_") else f"m_{name}"):
            if hasattr(value, candidate):
                return getattr(value, candidate)
    return None


def _enum_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    index = getattr(value, "Index", None)
    if index is not None:
        text = getattr(index, "String", None)
        if text:
            return str(text)
        strings = getattr(index, "strings", None)
        if strings:
            return str(strings[0])
        to_string = getattr(index, "ToString", None)
        if to_string is not None:
            try:
                text = to_string()
            except Exception:
                text = ""
            if text:
                return str(text)
    strings = getattr(value, "strings", None)
    if strings:
        return str(strings[0])
    return default


def _number(value: Any, default: float = 0.0) -> float:
    if value is None:
        return float(default)
    if isinstance(value, (int, float)):
        return float(value)
    raw = getattr(value, "Value", None)
    if raw is None:
        raw = getattr(value, "value", None)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(default)


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(_number(value, float(default)))
    except (TypeError, ValueError, OverflowError):
        return int(default)


def _boolean(value: Any, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    raw = getattr(value, "Value", value)
    if isinstance(raw, str):
        text = raw.strip().lower()
        if text in {"true", "yes", "on", "1"}:
            return True
        if text in {"false", "no", "off", "0", ""}:
            return False
    try:
        return bool(int(raw))
    except (TypeError, ValueError):
        return bool(raw) if raw is not None else bool(default)


def _curve_array_scalars(value: Any) -> list[float]:
    """Read primitive arrays from supported PROPERTY layouts."""

    if value is None:
        return []
    for attr in ("value", "Value"):
        raw = getattr(value, attr, None)
        if raw is None:
            continue
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
            return [_number(item) for item in raw]
        return [_number(raw)]
    return [_number(item) for item in _array_children(value)]


def _curve_array_vectors(value: Any) -> list[Vec4]:
    """Read split ``Vector`` arrays, including flattened Count=1 data."""

    if value is None:
        return []
    count = int(getattr(value, "Count", 0) or 0)
    for attr in ("value", "Value"):
        raw = getattr(value, attr, None)
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or not raw:
            continue
        if isinstance(raw[0], Sequence) and not isinstance(raw[0], (str, bytes)):
            return [_vector4(item, (-0.1, 0.0, 0.1, 0.0)) for item in raw]
        if count > 0 and len(raw) >= count * 4:
            return [
                _vector4(
                    raw[index * 4 : index * 4 + 4],
                    (-0.1, 0.0, 0.1, 0.0),
                )
                for index in range(count)
            ]
        if len(raw) >= 4:
            return [_vector4(raw[:4], (-0.1, 0.0, 0.1, 0.0))]

    children = _array_children(value)
    field_names = {_field_key(_prop_name(item)) for item in children if _prop_name(item)}
    if children and field_names.intersection({"x", "y", "z", "w"}):
        return [_vector4(value, (-0.1, 0.0, 0.1, 0.0))]
    return [_vector4(item, (-0.1, 0.0, 0.1, 0.0)) for item in children]


def _string(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    string_obj = getattr(value, "String", None)
    if string_obj is not None:
        text = getattr(string_obj, "String", None)
        if text is not None:
            return str(text)
        if isinstance(string_obj, str):
            return string_obj
    text = _enum_text(value)
    if text:
        return text
    raw = getattr(value, "Value", None)
    return str(raw) if raw is not None else default


def resource_path(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, Path)):
        return normalize_depot_path(value)
    for attr in ("DepotPath", "depotPath", "Path", "path"):
        path = getattr(value, attr, None)
        if path:
            return normalize_depot_path(path)
    handles = list(getattr(value, "Handles", None) or [])
    for handle in handles:
        path = resource_path(handle)
        if path:
            return path
    index = getattr(value, "Index", None)
    if index is not None:
        for attr in ("DepotPath", "Path", "path"):
            path = getattr(index, attr, None)
            if path:
                return normalize_depot_path(path)
    text = _string(value)
    if "\\" in text or "/" in text:
        return normalize_depot_path(text)
    return ""


def embedded_resource_index(value: Any) -> int | None:
    for handle in list(getattr(value, "Handles", None) or []):
        reference = getattr(handle, "Reference", None)
        if isinstance(reference, int) and reference >= 0:
            return reference
        if getattr(handle, "ChunkHandle", False):
            raw = getattr(handle, "val", None)
            try:
                raw_int = int(raw)
            except (TypeError, ValueError):
                continue
            if raw_int > 0:
                return raw_int - 1
    return None


def _vector4(value: Any, default: Vec4 = (0.0, 0.0, 0.0, 0.0)) -> Vec4:
    if value is None:
        return default
    if isinstance(value, Mapping):
        return tuple(float(value.get(axis, value.get(axis.lower(), default[i]))) for i, axis in enumerate("XYZW"))  # type: ignore[return-value]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        values = list(value)[:4]
        values.extend(default[len(values):])
        return tuple(float(item) for item in values)  # type: ignore[return-value]
    values = []
    for i, axis in enumerate("XYZW"):
        prop = _get_field(value, axis)
        if prop is not None:
            values.append(_number(prop, default[i]))
        else:
            raw = getattr(value, axis, getattr(value, axis.lower(), None))
            values.append(float(raw) if raw is not None else default[i])
    return tuple(values)  # type: ignore[return-value]


@dataclass(frozen=True)
class CurvePoint:
    time: float
    control_point: Vec4 = (-0.1, 0.0, 0.1, 0.0)
    value: float = 0.0
    curve_type_l: int = 1
    curve_type_r: int = 1


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _smooth_curve_value(
    fraction: float,
    point0: tuple[float, float],
    point1: tuple[float, float],
    point2: tuple[float, float],
    point3: tuple[float, float],
) -> float:
    x0, y0 = point0
    x1, y1 = point1
    x2, y2 = point2
    x3, y3 = point3
    if not (x0 < x1 < x2 < x3):
        return y1
    span = x2 - x1
    tangent1 = span * (y2 - y0) / (x2 - x0)
    tangent2 = span * (y3 - y1) / (x3 - x1)
    return _hermite(min(1.0, max(0.0, fraction)), y1, tangent1, tangent2, y2)


def _hermite(t: float, value0: float, tangent0: float, tangent1: float, value1: float) -> float:
    t2 = t * t
    t3 = t2 * t
    return (
        (2.0 * t3 - 3.0 * t2 + 1.0) * value0
        + (t3 - 2.0 * t2 + t) * tangent0
        + (-2.0 * t3 + 3.0 * t2) * value1
        + (t3 - t2) * tangent1
    )


@dataclass(frozen=True)
class SimpleCurve:
    curve_type: str = "SCT_Float"
    scalar_edit_scale: float = 1.0
    scalar_edit_origin: float = 0.0
    base_type: str = "CT_Smooth"
    loop: bool = True
    points: tuple[CurvePoint, ...] = ()

    @property
    def is_scalar(self) -> bool:
        return self.curve_type in {"", "SCT_Float"}

    def _bounds(self, time: float) -> tuple[int, int, float, float, float]:
        count = len(self.points)
        if count == 1:
            return 0, 0, time, self.points[0].time, self.points[0].time
        adapted = time - math.floor(time) if self.loop else time
        if not self.loop:
            if adapted <= self.points[0].time:
                return 0, 0, adapted, self.points[0].time, self.points[0].time
            if adapted >= self.points[-1].time:
                index = count - 1
                return index, index, adapted, self.points[index].time, self.points[index].time

        upper = 0
        while upper < count and self.points[upper].time < adapted:
            upper += 1
        if upper < count and adapted == self.points[upper].time:
            return upper, upper, adapted, self.points[upper].time, self.points[upper].time
        lower = upper - 1 if upper > 0 else count - 1
        upper = upper if upper < count else 0
        time1 = self.points[lower].time
        time2 = self.points[upper].time
        if self.loop:
            while time2 <= time1:
                time2 += 1.0
            while adapted < time1:
                adapted += 1.0
        return lower, upper, adapted, time1, time2

    def _evaluate_channel(self, time: float, channel: int, scalar: bool) -> float:
        if not self.points:
            return 1.0 if scalar else 0.0
        lower, upper, adapted, time1, time2 = self._bounds(time)
        if lower == upper:
            point = self.points[lower]
            return point.value if scalar else point.control_point[channel]

        local_time = (adapted - time1) / (time2 - time1)
        point1 = self.points[lower]
        point2 = self.points[upper]
        value1 = point1.value if scalar else point1.control_point[channel]
        value2 = point2.value if scalar else point2.control_point[channel]

        if self.base_type == "CT_Linear":
            return _lerp(value1, value2, local_time)
        if self.base_type == "CT_Segmented" and scalar:
            if point1.curve_type_r == 0:  # CST_Constant
                return value1
            if point1.curve_type_r == 1 and point2.curve_type_l == 1:  # CST_Interpolate
                return _lerp(value1, value2, local_time)
            tangent0 = point1.control_point[3] / point1.control_point[2] if abs(point1.control_point[2]) > 1.0e-20 else 0.0
            tangent1 = point2.control_point[1] / point2.control_point[0] if abs(point2.control_point[0]) > 1.0e-20 else 0.0
            duration = time2 - time1
            return _hermite(local_time, value1, duration * tangent0, duration * tangent1, value2)

        count = len(self.points)
        index0 = (lower - 1) % count if self.loop else max(0, lower - 1)
        index3 = (upper + 1) % count if self.loop else min(count - 1, upper + 1)
        time0 = self.points[index0].time
        time3 = self.points[index3].time
        while time0 >= time1:
            time0 -= 1.0
        while time3 <= time2:
            time3 += 1.0
        value0 = self.points[index0].value if scalar else self.points[index0].control_point[channel]
        value3 = self.points[index3].value if scalar else self.points[index3].control_point[channel]
        return _smooth_curve_value(
            local_time,
            (time0, value0),
            (time1, value1),
            (time2, value2),
            (time3, value3),
        )

    def evaluate_scalar(self, time: float) -> float:
        if self.is_scalar:
            return self._evaluate_channel(time, 3, True)
        return self.evaluate(time)[3]

    def evaluate(self, time: float) -> Vec4:
        if self.is_scalar:
            return (0.0, 0.0, 0.0, self._evaluate_channel(time, 3, True))
        return tuple(self._evaluate_channel(time, channel, False) for channel in range(4))  # type: ignore[return-value]

    def evaluate_seconds(self, seconds: float) -> Vec4:
        return self.evaluate(float(seconds) / DAY_SECONDS)

    def evaluate_scalar_seconds(self, seconds: float) -> float:
        return self.evaluate_scalar(float(seconds) / DAY_SECONDS)


def parse_simple_curve(value: Any) -> SimpleCurve:
    """Decode supported ``SSimpleCurve`` PROPERTY layouts."""

    if isinstance(value, SimpleCurve):
        return value
    curve_type = _enum_text(_get_field(value, "CurveType"), "SCT_Float")
    scalar_scale = _number(_get_field(value, "ScalarEditScale"), 1.0)
    scalar_origin = _number(_get_field(value, "ScalarEditOrigin"), 0.0)
    base_type = _enum_text(_get_field(value, "dataBaseType"), "CT_Smooth")
    loop_prop = _get_field(value, "loop", "dataLoop")
    loop = _boolean(loop_prop, True) if loop_prop is not None else True
    data = _get_field(value, "dataCurveValues", "Curve Values")
    entries = _array_children(data)
    # The generic CR2W reader flattens a one-element struct array: ``More``
    # contains the SCurveDataEntry fields directly instead of one ELEMENT
    # wrapper.  Treat those fields as one entry; otherwise moonSize and other
        # single-key curves become five zero-valued placeholder points.
    if int(getattr(data, "Count", 0) or 0) == 1 and entries:
        field_names = {
            normalize_curve_entry_name(_prop_name(item))
            for item in entries
            if _prop_name(item)
        }
        if field_names.intersection({"time", "controlPoint", "value", "curveTypeL", "curveTypeR"}):
            entries = [entries]
    points: list[CurvePoint] = []
    for entry in entries:
        items = (
            list(entry)
            if isinstance(entry, Sequence) and not isinstance(entry, (str, bytes, Mapping))
            else _children(entry)
        )
        fields = {
            normalize_curve_entry_name(_prop_name(item)): item
            for item in items
            if _prop_name(item)
        }
        if not fields and isinstance(entry, Mapping):
            fields = {normalize_curve_entry_name(str(key)): item for key, item in entry.items()}
        point = CurvePoint(
            time=_number(fields.get("time"), 0.0),
            control_point=_vector4(fields.get("controlPoint"), (-0.1, 0.0, 0.1, 0.0)),
            value=_number(fields.get("value"), 0.0),
            curve_type_l=_integer(fields.get("curveTypeL"), 1),
            curve_type_r=_integer(fields.get("curveTypeR"), 1),
        )
        points.append(point)
    if not points:
        # Source files may store SSimpleCurve as parallel arrays rather than an
        # SCurveDataEntry struct array.
        # Color curves omit dataValues and keep RGBA in dataControlPoints.
        times = _curve_array_scalars(_get_field(value, "dataTimes"))
        values = _curve_array_scalars(_get_field(value, "dataValues"))
        curve_types_l = _curve_array_scalars(_get_field(value, "dataCurveType0"))
        curve_types_r = _curve_array_scalars(_get_field(value, "dataCurveType1"))
        control_points = _curve_array_vectors(_get_field(value, "dataControlPoints"))
        point_count = max(
            len(times),
            len(values),
            len(curve_types_l),
            len(curve_types_r),
            len(control_points),
        )
        for index in range(point_count):
            points.append(
                CurvePoint(
                    time=times[index] if index < len(times) else 0.0,
                    control_point=(
                        control_points[index]
                        if index < len(control_points)
                        else (-0.1, 0.0, 0.1, 0.0)
                    ),
                    value=values[index] if index < len(values) else 0.0,
                    curve_type_l=(
                        int(curve_types_l[index]) if index < len(curve_types_l) else 1
                    ),
                    curve_type_r=(
                        int(curve_types_r[index]) if index < len(curve_types_r) else 1
                    ),
                )
            )
    points.sort(key=lambda point: point.time)
    return SimpleCurve(
        curve_type=curve_type,
        scalar_edit_scale=scalar_scale,
        scalar_edit_origin=scalar_origin,
        base_type=base_type,
        loop=loop,
        points=tuple(points),
    )


def gamma_to_linear_color(value: Sequence[float], *, scaled: bool = True, normalized: bool = True) -> Vec4:
    """Convert stored curve color channels to linear RGB.

    Curve RGB channels use 0..255 values.  Color-scaled curves additionally
    multiply the linear RGB result by the non-negative W channel.
    """

    vector = list(value[:4])
    vector.extend((0.0,) * (4 - len(vector)))
    normalization = 1.0 / 255.0 if normalized else 1.0
    scale = max(0.0, float(vector[3])) if scaled else 1.0
    rgb = tuple(
        scale * math.pow(normalization * min(255.0, max(0.0, float(channel))), 2.2)
        for channel in vector[:3]
    )
    return (rgb[0], rgb[1], rgb[2], 1.0)


def curve_color_linear(curve: SimpleCurve, time: float) -> Vec4:
    return gamma_to_linear_color(
        curve.evaluate(time),
        scaled=curve.curve_type == "SCT_ColorScaled",
        normalized=True,
    )


def _default_float_curve(points: Iterable[tuple[float, float]]) -> SimpleCurve:
    return SimpleCurve(points=tuple(CurvePoint(time=time, value=value) for time, value in sorted(points)))


@dataclass(frozen=True)
class GlobalLightingTrajectory:
    yaw_degrees: float = 0.0
    yaw_degrees_sun_offset: float = 0.0
    yaw_degrees_moon_offset: float = 0.0
    sun_curve_shift_factor: float = 0.0
    moon_curve_shift_factor: float = 0.0
    sun_squeeze: float = 1.0
    moon_squeeze: float = 1.0
    sun_height: SimpleCurve = field(default_factory=lambda: _default_float_curve(((0.15, -0.08), (0.5, 0.4), (0.85, -0.08))))
    moon_height: SimpleCurve = field(default_factory=lambda: _default_float_curve(((0.15, 0.08), (0.5, -0.8), (0.85, 0.08))))
    light_height: SimpleCurve = field(default_factory=lambda: _default_float_curve(((0.15, 0.01), (0.5, 0.4), (0.85, 0.01))))
    light_dir_choice: SimpleCurve = field(default_factory=lambda: _default_float_curve(((0.5, 1.0),)))
    sky_day_amount: SimpleCurve = field(default_factory=lambda: _default_float_curve(((0.15, 0.0), (0.25, 1.0), (0.75, 1.0), (0.85, 0.0))))
    moon_shafts_begin_hour: float = 0.0
    moon_shafts_end_hour: float = 0.0

    @staticmethod
    def _squeezed_progress(progress: float, reference: float, squeeze: float) -> float:
        reference %= 1.0
        return (reference + (progress % 1.0 - reference) * squeeze) % 1.0

    @staticmethod
    def _direction(height: SimpleCurve, pitch_progress: float, yaw_progress: float) -> Vec3:
        pitch_value = min(1.0, max(-1.0, height.evaluate_scalar(pitch_progress)))
        pitch = 0.5 * math.pi * pitch_value
        yaw = 2.0 * math.pi * yaw_progress
        cos_pitch = math.cos(pitch)
        direction = (
            cos_pitch * math.sin(yaw),
            cos_pitch * math.cos(yaw),
            math.sin(pitch),
        )
        length = math.sqrt(sum(component * component for component in direction))
        return tuple(component / length for component in direction)  # type: ignore[return-value]

    def sun_direction(self, seconds: float) -> Vec3:
        day = float(seconds) / DAY_SECONDS
        shifted = day + self.sun_curve_shift_factor
        yaw = self._squeezed_progress(shifted, 0.5, self.sun_squeeze)
        yaw += (self.yaw_degrees + self.yaw_degrees_sun_offset) / 360.0
        return self._direction(self.sun_height, day, yaw)

    def moon_direction(self, seconds: float) -> Vec3:
        day = float(seconds) / DAY_SECONDS
        shifted = day + self.moon_curve_shift_factor
        yaw = self._squeezed_progress(shifted, 0.5, self.moon_squeeze)
        yaw += (self.yaw_degrees + self.yaw_degrees_moon_offset) / 360.0
        return self._direction(self.moon_height, day, yaw)

    def light_direction(self, seconds: float) -> Vec3:
        day = float(seconds) / DAY_SECONDS
        sun_yaw = self._squeezed_progress(day + self.sun_curve_shift_factor, 0.5, self.sun_squeeze)
        sun_yaw += (self.yaw_degrees + self.yaw_degrees_sun_offset) / 360.0
        moon_yaw = self._squeezed_progress(day + self.moon_curve_shift_factor, 0.5, self.moon_squeeze)
        moon_yaw += (self.yaw_degrees + self.yaw_degrees_moon_offset) / 360.0
        sun_yaw %= 1.0
        moon_yaw %= 1.0
        blend = min(1.0, max(0.0, self.light_dir_choice.evaluate_scalar(day)))
        shortest_turn = (sun_yaw - moon_yaw + 0.5) % 1.0 - 0.5
        light_yaw = (moon_yaw + shortest_turn * blend) % 1.0
        return self._direction(self.light_height, day, light_yaw)

    def sky_day_factor(self, seconds: float) -> float:
        return self.sky_day_amount.evaluate_scalar(float(seconds) / DAY_SECONDS)

    def moon_shafts_enabled(self, seconds: float) -> bool:
        hour = (float(seconds) % DAY_SECONDS) / 3600.0
        begin = self.moon_shafts_begin_hour
        end = self.moon_shafts_end_hour
        return (hour > begin or hour < end) if begin > end else (hour > begin and hour < end)


def parse_global_lighting_trajectory(value: Any) -> GlobalLightingTrajectory:
    if isinstance(value, GlobalLightingTrajectory):
        return value
    defaults = GlobalLightingTrajectory()

    def curve(name: str, default: SimpleCurve) -> SimpleCurve:
        prop = _get_field(value, name)
        return parse_simple_curve(prop) if prop is not None else default

    return GlobalLightingTrajectory(
        yaw_degrees=_number(_get_field(value, "yawDegrees"), defaults.yaw_degrees),
        yaw_degrees_sun_offset=_number(_get_field(value, "yawDegreesSunOffset"), defaults.yaw_degrees_sun_offset),
        yaw_degrees_moon_offset=_number(_get_field(value, "yawDegreesMoonOffset"), defaults.yaw_degrees_moon_offset),
        sun_curve_shift_factor=_number(_get_field(value, "sunCurveShiftFactor"), defaults.sun_curve_shift_factor),
        moon_curve_shift_factor=_number(_get_field(value, "moonCurveShiftFactor"), defaults.moon_curve_shift_factor),
        sun_squeeze=_number(_get_field(value, "sunSqueeze"), defaults.sun_squeeze),
        moon_squeeze=_number(_get_field(value, "moonSqueeze"), defaults.moon_squeeze),
        sun_height=curve("sunHeight", defaults.sun_height),
        moon_height=curve("moonHeight", defaults.moon_height),
        light_height=curve("lightHeight", defaults.light_height),
        light_dir_choice=curve("lightDirChoice", defaults.light_dir_choice),
        sky_day_amount=curve("skyDayAmount", defaults.sky_day_amount),
        moon_shafts_begin_hour=_number(_get_field(value, "moonShaftsBeginHour"), defaults.moon_shafts_begin_hour),
        moon_shafts_end_hour=_number(_get_field(value, "moonShaftsEndHour"), defaults.moon_shafts_end_hour),
    )


def _decode_value(value: Any, curves: Dict[str, SimpleCurve], path: str) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, SimpleCurve):
        curves[path] = value
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for name, item in value.items():
            child_path = f"{path}.{name}" if path else str(name)
            result[str(name)] = _decode_value(item, curves, child_path)
        return result
    the_type = _prop_type(value)
    if the_type == "SSimpleCurve":
        decoded = parse_simple_curve(value)
        curves[path] = decoded
        return decoded
    if "handle:" in the_type or the_type.startswith("soft:"):
        path_value = resource_path(value)
        return path_value if path_value else {"chunk_index": embedded_resource_index(value)}
    if the_type == "Bool":
        return _boolean(value)
    if the_type in {"Float", "CFloat"}:
        return _number(value)
    if the_type in {"Uint8", "Uint16", "Uint32", "Uint64", "Int8", "Int16", "Int32", "Int64"}:
        return _integer(value)
    if the_type in {"String", "StringAnsi", "CName", "NodeRef"}:
        return _string(value)
    enum = _enum_text(value)
    if enum:
        return enum
    if the_type.startswith("array:") or the_type.startswith("static:"):
        return [
            _decode_value(item, curves, f"{path}[{index}]")
            for index, item in enumerate(_array_children(value))
        ]
    children = _children(value)
    if children:
        result: dict[str, Any] = {}
        for item in children:
            name = _prop_name(item)
            if not name:
                continue
            child_path = f"{path}.{name}" if path else name
            result[name] = _decode_value(item, curves, child_path)
        return result
    raw = getattr(value, "Value", None)
    return raw


@dataclass(frozen=True)
class EnvironmentDefinition:
    source_path: str = ""
    params: Mapping[str, Any] = field(default_factory=dict)
    curves: Mapping[str, SimpleCurve] = field(default_factory=dict)
    cr2w_file: Any = field(default=None, repr=False, compare=False)

    def curve(self, path: str) -> SimpleCurve | None:
        key = str(path or "").strip().strip(".")
        if key.startswith("envParams."):
            key = key[len("envParams."):]
        return self.curves.get(key)

    def evaluate_curve(self, path: str, time: float) -> Vec4 | None:
        curve = self.curve(path)
        return curve.evaluate(time) if curve is not None else None

    def evaluate_curve_seconds(self, path: str, seconds: float) -> Vec4 | None:
        curve = self.curve(path)
        return curve.evaluate_seconds(seconds) if curve is not None else None


_PLACEHOLDER_CONTROL_POINT = (-0.1, 0.0, 0.1, 0.0)


def _point_is_placeholder(point: CurvePoint) -> bool:
    if abs(float(point.time)) > 1.0e-12 or abs(float(point.value)) > 1.0e-12:
        return False
    control = tuple(float(component) for component in (point.control_point or ()))[:4]
    if len(control) < 4:
        return True
    return all(abs(component) <= 1.0e-12 for component in control) or all(
        abs(a - b) <= 1.0e-9 for a, b in zip(control, _PLACEHOLDER_CONTROL_POINT)
    )


def curve_is_placeholder(curve: SimpleCurve) -> bool:
    """Return whether a curve contains only serialized placeholder values."""

    points = tuple(curve.points or ())
    if not points:
        return True
    if curve.is_scalar:
        return all(
            abs(float(point.time)) <= 1.0e-12 and abs(float(point.value)) <= 1.0e-12
            for point in points
        )
    return all(_point_is_placeholder(point) for point in points)


# Stable display order for known groups; unknown groups retain file order.
_ENV_PARAM_GROUP_ORDER = (
    "finalcolorbalance",
    "sharpen",
    "painteffect",
    "ssaonv",
    "ssaoms",
    "globallight",
    "interiorfallback",
    "speedtree",
    "tonemapping",
    "bloomnew",
    "globalfog",
    "sky",
    "depthoffield",
    "colormodtransparency",
    "shadows",
    "water",
    "colorgroups",
    "flarecolorgroups",
    "sunandmoonparams",
    "windparams",
    "gameplayeffects",
    "motionblur",
    "cameralightssetup",
    "dialoglightparams",
)

_ENV_FIELD_MAX_DEPTH = 3


@dataclass(frozen=True)
class EnvironmentFieldChild:
    parent_key: str
    item_key: str
    label: str
    type_text: str
    value_text: str
    depth: int
    has_children: bool


@dataclass(frozen=True)
class EnvironmentFieldRow:
    group: str
    field: str
    type_text: str
    value_text: str
    is_set: bool
    children: tuple[EnvironmentFieldChild, ...] = ()


def _env_type_text(value: Any) -> str:
    if isinstance(value, SimpleCurve):
        return f"SSimpleCurve ({'scalar' if value.is_scalar else 'color'})"
    if isinstance(value, bool):
        return "Bool"
    if isinstance(value, int):
        return "Int"
    if isinstance(value, float):
        return "Float"
    if isinstance(value, str):
        return "String"
    if isinstance(value, Mapping):
        return f"struct ({len(value)} fields)"
    if isinstance(value, Sequence):
        return f"array ({len(value)})"
    return type(value).__name__


def _env_value_summary(value: Any) -> str:
    if isinstance(value, SimpleCurve):
        points = tuple(value.points or ())
        if value.is_scalar:
            values = [float(point.value) for point in points]
            if len(set(values)) <= 1:
                return f"= {values[0] if values else 0.0:g}"
            return f"{len(points)} keys · {min(values):g} … {max(values):g}"
        return f"{len(points)} keys"
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value if value else '""'
    if isinstance(value, Mapping):
        return f"{len(value)} fields"
    if isinstance(value, Sequence):
        return f"{len(value)} items"
    if value is None:
        return '""'
    return str(value)


def _env_child_pairs(value: Any) -> list[tuple[str, Any]]:
    if isinstance(value, SimpleCurve):
        return []
    if isinstance(value, Mapping):
        return [(str(key), item) for key, item in value.items()]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [(f"[{index}]", item) for index, item in enumerate(value)]
    return []


def _env_fill_children(
    entries: list[EnvironmentFieldChild], value: Any, parent_key: str = "", depth: int = 0
) -> None:
    for index, (label, child) in enumerate(_env_child_pairs(value)):
        item_key = f"{parent_key}/{index}" if parent_key else str(index)
        has_children = bool(_env_child_pairs(child)) and depth < _ENV_FIELD_MAX_DEPTH
        entries.append(
            EnvironmentFieldChild(
                parent_key=parent_key,
                item_key=item_key,
                label=label,
                type_text=_env_type_text(child),
                value_text=_env_value_summary(child),
                depth=depth,
                has_children=has_children,
            )
        )
        if has_children:
            _env_fill_children(entries, child, item_key, depth + 1)


def _env_field_is_set(value: Any) -> bool:
    if value is None or value == "":
        return False
    if isinstance(value, SimpleCurve):
        return not curve_is_placeholder(value)
    return True


def _env_field_row(group: str, field_name: str, value: Any) -> EnvironmentFieldRow:
    is_set = _env_field_is_set(value)
    children: list[EnvironmentFieldChild] = []
    if is_set:
        _env_fill_children(children, value)
    return EnvironmentFieldRow(
        group=group,
        field=field_name,
        type_text=_env_type_text(value),
        value_text=_env_value_summary(value) if is_set else "<unset>",
        is_set=is_set,
        children=tuple(children),
    )


def describe_environment_fields(environment: Any) -> tuple[EnvironmentFieldRow, ...]:
    """Flatten ``EnvironmentDefinition.params`` into displayable field rows.

    Groups follow the CAreaEnvironmentParams member order; unknown groups keep
    file order.  Placeholder curves and empty values report ``is_set=False``.
    """

    params = getattr(environment, "params", None) or {}
    order = {name: index for index, name in enumerate(_ENV_PARAM_GROUP_ORDER)}
    groups = sorted(params.items(), key=lambda item: order.get(_field_key(item[0]), len(order)))
    rows: list[EnvironmentFieldRow] = []
    for group, value in groups:
        if isinstance(value, Mapping):
            rows.extend(_env_field_row(group, field_name, field_value) for field_name, field_value in value.items())
        else:
            rows.append(_env_field_row("envParams", group, value))
    return tuple(rows)


@dataclass(frozen=True)
class SkyboxResources:
    sun_mesh: str = ""
    sun_material: str = ""
    sun_material_ref: int | None = None
    moon_mesh: str = ""
    moon_material: str = ""
    moon_material_ref: int | None = None
    skybox_mesh: str = ""
    skybox_material: str = ""
    skybox_material_ref: int | None = None


@dataclass(frozen=True)
class WorldEnvironment:
    source_path: str = ""
    environment_definition: str = ""
    scenes_environment_definition: str = ""
    weather_template: str = ""
    trajectory: GlobalLightingTrajectory = field(default_factory=GlobalLightingTrajectory)
    skybox: SkyboxResources = field(default_factory=SkyboxResources)
    raw_fields: Mapping[str, Any] = field(default_factory=dict)
    cr2w_file: Any = field(default=None, repr=False, compare=False)


def _coerce_cr2w(source: Any) -> tuple[Any, str]:
    if isinstance(source, (str, os.PathLike)):
        path = os.fspath(source)
        from .CR2W_file import read_CR2W

        return read_CR2W(path), os.path.abspath(path)
    source_path = str(getattr(source, "fileName", "") or "")
    return source, source_path


def _chunks(cr2w: Any) -> list[Any]:
    chunks = getattr(cr2w, "CHUNKS", None)
    if chunks is not None:
        nested = getattr(chunks, "CHUNKS", None)
        return list(nested if nested is not None else chunks)
    return []


def _chunk_named(cr2w: Any, name: str) -> Any:
    if getattr(cr2w, "name", None) == name:
        return cr2w
    return next((chunk for chunk in _chunks(cr2w) if getattr(chunk, "name", None) == name), None)


def _embedded_base_resource(prop: Any, chunks: list[Any]) -> tuple[str, int | None]:
    index = embedded_resource_index(prop)
    if index is None or not (0 <= index < len(chunks)):
        return "", index
    base_material = _get_field(chunks[index], "baseMaterial")
    return resource_path(base_material), index


def load_world_environment(source: Any) -> WorldEnvironment:
    """Load the environment section from a path or parsed ``.w2w`` CR2W."""

    cr2w, source_path = _coerce_cr2w(source)
    world_chunk = _chunk_named(cr2w, "CGameWorld")
    if world_chunk is None:
        raise ValueError("CR2W does not contain a CGameWorld chunk")
    params = _get_field(world_chunk, "environmentParameters")
    if params is None:
        raise ValueError("CGameWorld has no environmentParameters")
    trajectory = parse_global_lighting_trajectory(_get_field(params, "globalLightingTrajectory"))
    sky = _get_field(params, "skybox")
    chunks = _chunks(cr2w)
    sun_material, sun_ref = _embedded_base_resource(_get_field(sky, "sunMaterial"), chunks)
    moon_material, moon_ref = _embedded_base_resource(_get_field(sky, "moonMaterial"), chunks)
    skybox_material, skybox_ref = _embedded_base_resource(_get_field(sky, "skyboxMaterial"), chunks)
    curves: Dict[str, SimpleCurve] = {}
    raw = _decode_value(params, curves, "")
    return WorldEnvironment(
        source_path=source_path,
        environment_definition=resource_path(_get_field(params, "environmentDefinition")),
        scenes_environment_definition=resource_path(_get_field(params, "scenesEnvironmentDefinition")),
        weather_template=resource_path(_get_field(params, "weatherTemplate")),
        trajectory=trajectory,
        skybox=SkyboxResources(
            sun_mesh=resource_path(_get_field(sky, "sunMesh")),
            sun_material=sun_material,
            sun_material_ref=sun_ref,
            moon_mesh=resource_path(_get_field(sky, "moonMesh")),
            moon_material=moon_material,
            moon_material_ref=moon_ref,
            skybox_mesh=resource_path(_get_field(sky, "skyboxMesh")),
            skybox_material=skybox_material,
            skybox_material_ref=skybox_ref,
        ),
        raw_fields=raw if isinstance(raw, Mapping) else {},
        cr2w_file=cr2w,
    )


def decode_world_environment(cr2w_file: Any) -> WorldEnvironment:
    if isinstance(cr2w_file, (str, os.PathLike)):
        raise TypeError("decode_world_environment expects an already-parsed CR2W object")
    return load_world_environment(cr2w_file)


def load_environment_source(path: str | os.PathLike[str]) -> EnvironmentDefinition:
    return load_environment_definition(path)


def load_environment_definition(source: Any) -> EnvironmentDefinition:
    """Load a ``CEnvironmentDefinition`` and index every simple curve."""

    cr2w, source_path = _coerce_cr2w(source)
    chunk = _chunk_named(cr2w, "CEnvironmentDefinition")
    if chunk is None:
        raise ValueError("CR2W does not contain a CEnvironmentDefinition chunk")
    params_prop = _get_field(chunk, "envParams")
    if params_prop is None:
        raise ValueError("CEnvironmentDefinition has no envParams")
    curves: Dict[str, SimpleCurve] = {}
    params = _decode_value(params_prop, curves, "")
    return EnvironmentDefinition(
        source_path=source_path,
        params=params if isinstance(params, Mapping) else {},
        curves=curves,
        cr2w_file=cr2w,
    )


@dataclass(frozen=True)
class WeatherEffect:
    path: str = ""
    priority: int = 0
    probability: float = 0.0
    strength: float = 0.0
    effect_type: str = "CLOUDS"


@dataclass(frozen=True)
class WeatherPreset:
    name: str
    probability: float = 0.0
    wind_scale: float = 0.1
    blend_time: float = 45.0
    skybox: float = -1.0
    fake_shadow: float = -1.0
    background_thunder: bool = False
    environment_path: str = ""
    environment_blend: float = 0.0
    occurrence_time: float = 600.0
    effects: tuple[WeatherEffect, ...] = ()

    @property
    def env_path(self) -> str:
        return self.environment_path

    @property
    def env_blend(self) -> float:
        return self.environment_blend


@dataclass(frozen=True)
class WeatherTable:
    source_path: str = ""
    presets: tuple[WeatherPreset, ...] = ()
    warnings: tuple[str, ...] = ()

    def preset(self, name: str) -> WeatherPreset | None:
        return next((preset for preset in self.presets if preset.name == name), None)


def _csv_float(row: Mapping[str, str], key: str, default: float, warnings: list[str], row_number: int) -> float:
    text = str(row.get(key, "") or "").strip()
    if not text:
        return default
    try:
        return float(text)
    except ValueError:
        warnings.append(f"Row {row_number}: invalid {key} value {text!r}")
        return default


def _csv_bool(row: Mapping[str, str], key: str, default: bool, warnings: list[str], row_number: int) -> bool:
    text = str(row.get(key, "") or "").strip().lower()
    if not text:
        return default
    if text in {"true", "1", "yes", "on"}:
        return True
    if text in {"false", "0", "no", "off"}:
        return False
    warnings.append(f"Row {row_number}: invalid {key} value {text!r}")
    return default


def load_weather_table(source: str | os.PathLike[str] | TextIO) -> WeatherTable:
    """Parse the native semicolon-delimited weather ``C2dArray`` CSV."""

    source_path = ""
    close_file = False
    if isinstance(source, (str, os.PathLike)):
        source_path = os.path.abspath(os.fspath(source))
        handle: TextIO = open(source_path, "r", encoding="utf-8-sig", newline="")
        close_file = True
    else:
        handle = source
        source_path = str(getattr(source, "name", "") or "")
    warnings: list[str] = []
    presets: list[WeatherPreset] = []
    try:
        reader = csv.DictReader(handle, delimiter=";")
        if reader.fieldnames is None:
            return WeatherTable(source_path=source_path, warnings=("Weather table has no header",))
        for row_number, row in enumerate(reader, start=2):
            name = str(row.get("name", "") or "").strip()
            if not name:
                warnings.append(f"Row {row_number}: weather preset has no name")
                continue
            effects: list[WeatherEffect] = []
            for index in range(1, 6):
                effect_path = normalize_depot_path(row.get(f"{index}_effect", ""))
                if not effect_path:
                    continue
                probability = _csv_float(row, f"{index}_prob", 0.0, warnings, row_number)
                strength = _csv_float(row, f"{index}_strength", 0.0, warnings, row_number)
                priority = int(_csv_float(row, f"{index}_priority", 0.0, warnings, row_number))
                effect_type = str(row.get(f"{index}_type", "") or "CLOUDS").strip().upper() or "CLOUDS"
                effects.append(
                    WeatherEffect(
                        path=effect_path,
                        priority=priority,
                        probability=probability,
                        strength=strength,
                        effect_type=effect_type,
                    )
                )
            environment_path = normalize_depot_path(row.get("envPath", ""))
            environment_blend = _csv_float(row, "envBlend", 0.0, warnings, row_number) if environment_path else 0.0
            presets.append(
                WeatherPreset(
                    name=name,
                    probability=_csv_float(row, "probability", 0.0, warnings, row_number),
                    wind_scale=_csv_float(row, "windScale", 0.1, warnings, row_number),
                    blend_time=_csv_float(row, "blendTime", 45.0, warnings, row_number),
                    skybox=_csv_float(row, "skybox", -1.0, warnings, row_number),
                    fake_shadow=_csv_float(row, "fakeShadow", -1.0, warnings, row_number),
                    background_thunder=_csv_bool(row, "backgroundThunder", False, warnings, row_number),
                    environment_path=environment_path,
                    environment_blend=min(1.0, max(0.0, environment_blend)),
                    occurrence_time=_csv_float(
                        row,
                        "occurenceTime" if "occurenceTime" in row else "occurrenceTime",
                        600.0,
                        warnings,
                        row_number,
                    ),
                    effects=tuple(effects),
                )
            )
    finally:
        if close_file:
            handle.close()
    return WeatherTable(source_path=source_path, presets=tuple(presets), warnings=tuple(warnings))


__all__ = [
    "DAY_SECONDS",
    "CurvePoint",
    "EnvironmentDefinition",
    "EnvironmentFieldChild",
    "EnvironmentFieldRow",
    "GlobalLightingTrajectory",
    "SimpleCurve",
    "SkyboxResources",
    "WeatherEffect",
    "WeatherPreset",
    "WeatherTable",
    "WorldEnvironment",
    "curve_color_linear",
    "curve_is_placeholder",
    "decode_world_environment",
    "describe_environment_fields",
    "embedded_resource_index",
    "gamma_to_linear_color",
    "load_environment_definition",
    "load_environment_source",
    "load_weather_table",
    "load_world_environment",
    "normalize_curve_entry_name",
    "normalize_depot_path",
    "parse_global_lighting_trajectory",
    "parse_simple_curve",
    "resource_path",
]
