"""Blender-independent foliage grid and transform helpers."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Iterator, Sequence

from .terrain_core import (
    Bounds2D,
    coerce_bounds as _coerce_bounds,
    point_in_bounds,
    terrain_tile_bounds,
)


CELL_SIZE = 64.0


@dataclass(frozen=True)
class DecodedFoliageTransform:
    """One foliage transform in Blender's XYZ-Euler representation."""

    location: tuple[float, float, float]
    rotation_xyz: tuple[float, float, float]
    scale: tuple[float, float, float]
    packed: bool


def foliage_cells_for_bounds(
    bounds: Bounds2D | Sequence[float],
    cell_size: float = CELL_SIZE,
) -> Iterator[tuple[float, float]]:
    """Yield cells intersecting half-open ``bounds``."""

    bounds = _coerce_bounds(bounds)
    cell_size = float(cell_size)
    if not math.isfinite(cell_size) or cell_size <= 0.0:
        raise ValueError("Foliage cell size must be a positive finite number")
    if bounds.max_x <= bounds.min_x or bounds.max_y <= bounds.min_y:
        return

    first_x = math.floor(bounds.min_x / cell_size)
    first_y = math.floor(bounds.min_y / cell_size)
    # Preserve half-open maximum edges.
    last_x = math.ceil(bounds.max_x / cell_size) - 1
    last_y = math.ceil(bounds.max_y / cell_size) - 1
    for ix in range(first_x, last_x + 1):
        for iy in range(first_y, last_y + 1):
            yield ix * cell_size, iy * cell_size


def _value(source: Any, key: str, default: Any = None) -> Any:
    if isinstance(source, dict):
        return source.get(key, default)
    return getattr(source, key, default)


def _has_value(source: Any, key: str) -> bool:
    if isinstance(source, dict):
        return key in source
    return hasattr(source, key)


def _float_value(source: Any, key: str, default: float = 0.0) -> float:
    try:
        value = _value(source, key, default)
        return default if value is None else float(value)
    except (TypeError, ValueError):
        return float(default)


def is_packed_foliage_instance(instance: Any) -> bool:
    """Detect RED's compact position/scale/yaw foliage transform layout."""

    if _has_value(instance, "Quat_z") and _has_value(instance, "Quat_w"):
        return True
    if instance.__class__.__name__ == "SFoliageInstanceData":
        return True
    if _has_value(instance, "Scale_x"):
        return False

    # Cached values may lack the concrete packed type.
    uniform_scale = _float_value(instance, "Yaw", 1.0)
    qz = _float_value(instance, "Pitch", 0.0)
    qw = _float_value(instance, "Roll", 1.0)
    quat_length = math.hypot(qz, qw)
    return 0.05 <= uniform_scale <= 50.0 and abs(quat_length - 1.0) <= 0.05


def _matrix_yxz_to_xyz(x: float, y: float, z: float) -> tuple[float, float, float]:
    """Convert YXZ Euler angles to XYZ without Blender."""

    sx, cx = math.sin(x), math.cos(x)
    sy, cy = math.sin(y), math.cos(y)
    sz, cz = math.sin(z), math.cos(z)

    r00 = cz * cy - sz * sx * sy
    r10 = sz * cy + cz * sx * sy
    r11 = cz * cx
    r12 = sz * sy - cz * sx * cy
    r20 = -cx * sy
    r21 = sx
    r22 = cx * cy

    out_y = math.asin(max(-1.0, min(1.0, -r20)))
    cos_y = math.cos(out_y)
    if abs(cos_y) > 1.0e-8:
        out_x = math.atan2(r21, r22)
        out_z = math.atan2(r10, r00)
    else:
        # Choose a stable gimbal-lock solution.
        out_x = math.atan2(-r12, r11)
        out_z = 0.0
    return out_x, out_y, out_z


def decode_foliage_instance_transform(instance: Any) -> DecodedFoliageTransform:
    """Decode packed RED foliage or legacy transforms."""

    location = (
        _float_value(instance, "X", 0.0),
        _float_value(instance, "Y", 0.0),
        _float_value(instance, "Z", 0.0),
    )
    packed = is_packed_foliage_instance(instance)
    if packed:
        uniform_scale = _float_value(
            instance,
            "Scale",
            _float_value(instance, "Scale_x", _float_value(instance, "Yaw", 1.0)),
        )
        if abs(uniform_scale) <= 1.0e-8:
            uniform_scale = 1.0
        qz = _float_value(instance, "Quat_z", _float_value(instance, "Pitch", 0.0))
        qw = _float_value(instance, "Quat_w", _float_value(instance, "Roll", 1.0))
        quat_length = math.hypot(qz, qw)
        if quat_length > 1.0e-8:
            qz /= quat_length
            qw /= quat_length
        else:
            qz, qw = 0.0, 1.0
        yaw_z = 2.0 * math.atan2(qz, qw)
        return DecodedFoliageTransform(
            location=location,
            rotation_xyz=(0.0, 0.0, yaw_z),
            scale=(uniform_scale, uniform_scale, uniform_scale),
            packed=True,
        )

    rotation_yxz = (
        math.radians(_float_value(instance, "Yaw", 0.0)),
        math.radians(_float_value(instance, "Pitch", 0.0)),
        math.radians(_float_value(instance, "Roll", 0.0)),
    )
    return DecodedFoliageTransform(
        location=location,
        rotation_xyz=_matrix_yxz_to_xyz(*rotation_yxz),
        scale=(
            _float_value(instance, "Scale_x", 1.0),
            _float_value(instance, "Scale_y", 1.0),
            _float_value(instance, "Scale_z", 1.0),
        ),
        packed=False,
    )


__all__ = (
    "Bounds2D",
    "CELL_SIZE",
    "DecodedFoliageTransform",
    "decode_foliage_instance_transform",
    "foliage_cells_for_bounds",
    "is_packed_foliage_instance",
    "point_in_bounds",
    "terrain_tile_bounds",
)
