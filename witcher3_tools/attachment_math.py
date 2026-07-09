"""RED Engine attachment flag and transform helpers."""

from __future__ import annotations

import re
from typing import Any, Iterable


HAF_FREE_POSITION_AXIS_X = 1 << 0
HAF_FREE_POSITION_AXIS_Y = 1 << 1
HAF_FREE_POSITION_AXIS_Z = 1 << 2
HAF_FREE_ROTATION = 1 << 3

_FLAG_NAMES = {
    "haffreepositionaxisx": HAF_FREE_POSITION_AXIS_X,
    "freepositionaxisx": HAF_FREE_POSITION_AXIS_X,
    "x": HAF_FREE_POSITION_AXIS_X,
    "haffreepositionaxisy": HAF_FREE_POSITION_AXIS_Y,
    "freepositionaxisy": HAF_FREE_POSITION_AXIS_Y,
    "y": HAF_FREE_POSITION_AXIS_Y,
    "haffreepositionaxisz": HAF_FREE_POSITION_AXIS_Z,
    "freepositionaxisz": HAF_FREE_POSITION_AXIS_Z,
    "z": HAF_FREE_POSITION_AXIS_Z,
    "haffreerotation": HAF_FREE_ROTATION,
    "freerotation": HAF_FREE_ROTATION,
}

_FLAG_VALUES_TO_NAMES = (
    (HAF_FREE_POSITION_AXIS_X, "HAF_FreePositionAxisX"),
    (HAF_FREE_POSITION_AXIS_Y, "HAF_FreePositionAxisY"),
    (HAF_FREE_POSITION_AXIS_Z, "HAF_FreePositionAxisZ"),
    (HAF_FREE_ROTATION, "HAF_FreeRotation"),
)

ENGINE_TRANSFORM_DEFAULTS = {
    "X": 0.0,
    "Y": 0.0,
    "Z": 0.0,
    "Pitch": 0.0,
    "Yaw": 0.0,
    "Roll": 0.0,
    "Scale_x": 1.0,
    "Scale_y": 1.0,
    "Scale_z": 1.0,
}


def _attr_or_key(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _coerce_real(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    try:
        text = str(value).strip()
        if not text:
            return default
        return float(text)
    except Exception:
        return default


def coerce_attachment_flags(value: Any) -> int:
    """Normalize RED EHardAttachmentFlags values from parsed CR2W forms."""

    if value is None:
        return 0
    for attr_name in ("Value", "_value", "val", "strings"):
        nested = getattr(value, attr_name, None)
        if nested is not None and nested is not value:
            return coerce_attachment_flags(nested)
    if isinstance(value, dict):
        for nested_name in ("Value", "_value", "val", "strings"):
            if nested_name in value:
                return coerce_attachment_flags(value[nested_name])
        flags = 0
        for key, item_value in value.items():
            if bool(item_value):
                flags |= coerce_attachment_flags(key)
        return flags
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, (list, tuple, set, frozenset)):
        flags = 0
        for item in value:
            flags |= coerce_attachment_flags(item)
        return flags

    text = str(value or "").strip()
    if not text:
        return 0
    try:
        return int(text, 0)
    except ValueError:
        pass

    flags = 0
    for token in re.split(r"[^A-Za-z0-9_]+", text):
        normalized = token.replace("_", "").lower()
        flags |= _FLAG_NAMES.get(normalized, 0)
    return flags


def attachment_flag_names(value: Any) -> list[str]:
    """Return WolvenKit/RTTI names for a parsed attachment bitfield."""

    flags = coerce_attachment_flags(value)
    return [name for bit, name in _FLAG_VALUES_TO_NAMES if flags & bit]


def attachment_flags_text(value: Any) -> str:
    """Return the pipe-separated form used by WolvenKit JSON enum scalars."""

    return "|".join(attachment_flag_names(value))


def normalize_engine_transform(transform: Any) -> dict[str, float]:
    """Normalize parsed/dict EngineTransform values into plain numeric fields."""

    return {
        name: _coerce_real(_attr_or_key(transform, name, default), default)
        for name, default in ENGINE_TRANSFORM_DEFAULTS.items()
    }


def engine_transform_is_identity(transform: Any, tolerance: float = 1e-8) -> bool:
    values = normalize_engine_transform(transform)
    return all(
        abs(values[name] - default) <= tolerance
        for name, default in ENGINE_TRANSFORM_DEFAULTS.items()
    )


def bone_name_from_slot_index(bone_names: Iterable[str], bone_index: Any, default: str = "") -> str:
    try:
        index = int(bone_index)
    except Exception:
        return default
    if index < 0:
        return default
    names = list(bone_names or [])
    if index >= len(names):
        return default
    return str(names[index] or "")
