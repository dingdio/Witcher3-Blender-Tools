"""Blender runtime glue for Dyng armatures."""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Set, Tuple

import bpy
from bpy.app.handlers import persistent
from mathutils import Matrix, Vector

from . import presets as physics_presets
from . import runtime as physics_runtime
from .dyng import (
    DYNG_COLLISION_COUNT_PROP,
    DYNG_DATA_PROP,
    DYNG_LINK_COUNT_PROP,
    DYNG_NODE_COUNT_PROP,
    DYNG_PARSE_STATUS_PROP,
    DYNG_TRIANGLE_COUNT_PROP,
    DyngResourceData,
    DyngSimulator,
    attach_dyng_resource_to_object,
    matrix_translation,
    resource_from_object,
    transform_from_axes,
)

log = logging.getLogger(__name__)

DYNG_ENABLED_PROP = "witcher_dyng_sim_enabled"
DYNG_RUNTIME_OPT_IN_PROP = "witcher_dyng_runtime_opt_in"
DYNG_LAST_FRAME_PROP = "witcher_dyng_last_frame"
DYNG_LAST_STEP_PROP = "witcher_dyng_last_step"
DYNG_SIM_STATUS_PROP = "witcher_dyng_sim_status"
DYNG_GRAVITY_PROP = "witcher_dyng_gravity"
DYNG_DAMPENING_PROP = "witcher_dyng_dampening"
DYNG_SPEED_PROP = "witcher_dyng_speed"
DYNG_LINK_ITERATIONS_PROP = "witcher_dyng_link_iterations"
DYNG_USE_OFFSETS_PROP = "witcher_dyng_use_offsets"
DYNG_PLANE_COLLISION_PROP = "witcher_dyng_plane_collision"
DYNG_BODY_COLLISION_PROP = "witcher_dyng_body_collision"
DYNG_BODY_COLLISION_RADIUS_PROP = "witcher_dyng_body_collision_radius"
DYNG_BODY_COLLISION_STRENGTH_PROP = "witcher_dyng_body_collision_strength"
DYNG_SHAKE_PROP = "witcher_dyng_shake"
DYNG_WIND_PROP = "witcher_dyng_wind"
DYNG_BLEND_PROP = "witcher_dyng_blend"
DYNG_ACCESSORY_PREVIEW_PROP = "witcher_dyng_accessory_preview"
DYNG_CACHE_STATUS_PROP = "witcher_dyng_cache_status"
DYNG_PRESET_PROP = "witcher_dyng_preset"

SCENE_WIND_ENABLED_ATTR = "witcher_dyng_wind_enabled"
SCENE_WIND_OBJECT_ATTR = "witcher_dyng_wind_object"
SCENE_WIND_DIRECTION_ATTR = "witcher_dyng_wind_direction"
SCENE_WIND_SPEED_ATTR = "witcher_dyng_wind_speed"
SCENE_LIVE_PREVIEW_ATTR = "witcher_physics_live_preview_enabled"
_BLENDER_WIND_SPEED_SCALE = 0.1


@dataclass
class _ObjectState:
    resource: DyngResourceData
    simulator: DyngSimulator
    rot90: bool
    frame: Optional[int] = None


@dataclass(frozen=True)
class DyngUserPreset:
    name: str
    gravity: float
    dampening: float
    speed: float
    link_iterations: int
    use_offsets: bool
    plane_collision: bool
    body_collision: bool
    body_collision_radius: float
    body_collision_strength: float
    shake: float
    wind: float
    blend: float


@dataclass
class _FrameCache:
    frame_start: int
    frame_end: int
    settings: Tuple
    matrices: Dict[int, Dict[str, Matrix]]


_STATES: Dict[str, _ObjectState] = {}
_FRAME_CACHES: Dict[str, _FrameCache] = {}
_RESOURCE_CACHES: Dict[str, Tuple[str, DyngResourceData]] = {}
_RUNTIME_OBJECT_NAMES: Set[str] = set()
_LAST_FRAMES: Dict[str, int] = {}
_CONSTRAINT_MUTES: Dict[Tuple[str, str, str], bool] = {}
_SUPPRESS_FRAME_HANDLER = False
_DYNG_RESET_FRAME_GAP = 1
_DYNG_MAX_ADVANCE_FRAMES = 1
_ROT90_TO_GAME = Matrix.Rotation(math.radians(90.0), 4, 'Z')
_ROT90_FROM_GAME = Matrix.Rotation(math.radians(-90.0), 4, 'Z')

DYNG_USER_PRESETS: Tuple[DyngUserPreset, ...] = (
    DyngUserPreset("Ciri_Hair_Reactive", 1.0, 0.88, 1.35, 12, False, False, False, 0.0, 1.0, 0.0, 1.0, 1.0),
    DyngUserPreset("Weighted_Accessory", 1.25, 0.82, 1.4, 18, True, True, False, 0.0, 1.0, 0.0, 0.0, 1.0),
)

def _state_key(obj: bpy.types.Object) -> str:
    return physics_runtime.state_key(obj)


def live_preview_enabled(scene: Optional[bpy.types.Scene] = None) -> bool:
    if scene is None:
        scene = getattr(bpy.context, "scene", None)
    if scene is None:
        return True
    value = getattr(scene, SCENE_LIVE_PREVIEW_ATTR, None)
    if value is not None:
        return bool(value)
    getter = getattr(scene, "get", None)
    if callable(getter):
        return bool(getter(SCENE_LIVE_PREVIEW_ATTR, True))
    return True


def set_live_preview_enabled(scene: Optional[bpy.types.Scene] = None, enabled: bool = True) -> None:
    if scene is None:
        scene = getattr(bpy.context, "scene", None)
    if scene is None:
        return
    try:
        setattr(scene, SCENE_LIVE_PREVIEW_ATTR, bool(enabled))
        return
    except Exception:
        pass
    try:
        scene[SCENE_LIVE_PREVIEW_ATTR] = bool(enabled)
    except Exception:
        pass


def _is_dyng_path(path: str) -> bool:
    return str(path or "").lower().endswith((".w3dyng", ".dyng"))


def is_dyng_armature(obj: Optional[bpy.types.Object]) -> bool:
    if obj is None or obj.type != "ARMATURE":
        return False
    if obj.get(DYNG_DATA_PROP):
        return True
    if str(obj.get("witcher_type", "")) == "CAnimDangleConstraint_Dyng":
        return True
    return _is_dyng_path(str(obj.get("witcher_path", "")))


def _default_use_offsets(resource: Optional[DyngResourceData]) -> bool:
    return False


def _default_plane_collision(resource: Optional[DyngResourceData]) -> bool:
    return False


def _default_body_collision(obj: Optional[bpy.types.Object], resource: Optional[DyngResourceData]) -> bool:
    return False


def _default_body_collision_radius(obj: Optional[bpy.types.Object], resource: Optional[DyngResourceData]) -> float:
    return 0.0


def _default_wind_strength(resource: Optional[DyngResourceData]) -> float:
    if resource is None:
        return 0.0
    return 1.0 if resource.triangles else 0.0


def is_wind_field_object(obj: Optional[bpy.types.Object]) -> bool:
    field = getattr(obj, "field", None) if obj is not None else None
    return field is not None and str(getattr(field, "type", "") or "").upper() == "WIND"


def wind_object_from_scene(scene: Optional[bpy.types.Scene]) -> Optional[bpy.types.Object]:
    obj = getattr(scene, SCENE_WIND_OBJECT_ATTR, None) if scene is not None else None
    return obj if is_wind_field_object(obj) else None


def wind_direction_from_object(obj: Optional[bpy.types.Object]) -> Tuple[float, float, float]:
    if not is_wind_field_object(obj):
        return (0.0, 0.0, 0.0)
    direction = getattr(obj, "matrix_world", Matrix.Identity(4)).to_quaternion() @ Vector((0.0, 0.0, 1.0))
    if direction.length <= 1e-6:
        return (0.0, 0.0, 0.0)
    direction.normalize()
    return (float(direction.x), float(direction.y), float(direction.z))


def set_wind_object_direction(obj: Optional[bpy.types.Object], direction_value) -> bool:
    if not is_wind_field_object(obj):
        return False
    direction = Vector((float(direction_value[0]), float(direction_value[1]), float(direction_value[2])))
    if direction.length <= 1e-6:
        return False
    direction.normalize()
    obj.rotation_euler = direction.to_track_quat("Z", "Y").to_euler()
    return True


def _default_blend(resource: Optional[DyngResourceData]) -> float:
    return 1.0


def _is_accessory_resource(resource: Optional[DyngResourceData]) -> bool:
    return resource is not None and not bool(resource.triangles)


def _ensure_resource_default_props(obj: bpy.types.Object, resource: Optional[DyngResourceData]) -> None:
    defaults = {
        DYNG_USE_OFFSETS_PROP: _default_use_offsets(resource),
        DYNG_PLANE_COLLISION_PROP: _default_plane_collision(resource),
        DYNG_BODY_COLLISION_PROP: _default_body_collision(obj, resource),
        DYNG_BODY_COLLISION_RADIUS_PROP: _default_body_collision_radius(obj, resource),
        DYNG_BODY_COLLISION_STRENGTH_PROP: 1.0,
        DYNG_SHAKE_PROP: 0.0,
        DYNG_WIND_PROP: _default_wind_strength(resource),
        DYNG_BLEND_PROP: _default_blend(resource),
        DYNG_ACCESSORY_PREVIEW_PROP: True,
    }
    for key, value in defaults.items():
        if key not in obj:
            obj[key] = value


def _bool_prop(obj: bpy.types.Object, key: str, default: bool = False) -> bool:
    return bool(obj.get(key, default))


def _float_prop(obj: bpy.types.Object, key: str, default: float = 0.0) -> float:
    try:
        return float(obj.get(key, default))
    except (TypeError, ValueError):
        return float(default)


def _clamped_float_prop(obj: bpy.types.Object, key: str, default: float, minimum: float, maximum: float) -> float:
    value = _float_prop(obj, key, default)
    return max(minimum, min(maximum, value))


def find_dyng_armature(context: bpy.types.Context) -> Optional[bpy.types.Object]:
    return physics_runtime.find_armature(context, is_dyng_armature)


def ensure_default_props(obj: bpy.types.Object, *, enabled: Optional[bool] = None) -> None:
    defaults = {
        DYNG_ENABLED_PROP: False,
        DYNG_RUNTIME_OPT_IN_PROP: False,
        DYNG_PRESET_PROP: "",
        DYNG_GRAVITY_PROP: 1.0,
        DYNG_DAMPENING_PROP: 0.95,
        DYNG_SPEED_PROP: 1.0,
        DYNG_LINK_ITERATIONS_PROP: 10,
    }
    for key, value in defaults.items():
        if key not in obj:
            obj[key] = value
    if enabled is not None:
        obj[DYNG_ENABLED_PROP] = bool(enabled)
        obj[DYNG_RUNTIME_OPT_IN_PROP] = bool(enabled)


def _has_dyng_runtime_opt_in_props(obj: Optional[bpy.types.Object]) -> bool:
    if obj is None:
        return False
    return bool(obj.get(DYNG_ENABLED_PROP, False)) and bool(obj.get(DYNG_RUNTIME_OPT_IN_PROP, False))


def is_dyng_runtime_enabled(
    obj: Optional[bpy.types.Object],
    scene: Optional[bpy.types.Scene] = None,
) -> bool:
    if not live_preview_enabled(scene):
        return False
    return _has_dyng_runtime_opt_in_props(obj)


def configure_imported_dyng(obj: bpy.types.Object, *, enabled: bool = True) -> None:
    """Initialize a newly imported Dyng armature for runtime simulation."""

    clear_state(obj)
    clear_cache(obj)
    ensure_default_props(obj, enabled=enabled)
    _ensure_resource_default_props(obj, resource_from_object(obj))
    if enabled:
        obj[DYNG_BLEND_PROP] = 1.0
        obj[DYNG_ACCESSORY_PREVIEW_PROP] = True
        set_live_preview_enabled(enabled=True)
        _RUNTIME_OBJECT_NAMES.add(_state_key(obj))
        ensure_frame_handler()
        obj[DYNG_SIM_STATUS_PROP] = "Dyng runtime enabled on import"
    else:
        obj[DYNG_SIM_STATUS_PROP] = "Dyng runtime ready"


def _resolve_dyng_path(path: str) -> str:
    if not path:
        return ""
    if os.path.isabs(path) and os.path.exists(path):
        return path
    try:
        from ..CR2W.common_blender import repo_file

        resolved = repo_file(path)
        if resolved and os.path.exists(resolved):
            return resolved
    except Exception:
        log.debug("Failed to resolve Dyng repo path: %s", path, exc_info=True)
    return path


def load_resource_for_object(obj: bpy.types.Object) -> Optional[DyngResourceData]:
    key = _state_key(obj)
    payload = str(obj.get(DYNG_DATA_PROP, "") or "")
    cached = _RESOURCE_CACHES.get(key)
    if payload and cached is not None and cached[0] == payload:
        resource = cached[1]
        _ensure_resource_default_props(obj, resource)
        return resource

    resource = resource_from_object(obj)
    if resource is not None:
        _RESOURCE_CACHES[key] = (payload, resource)
        _ensure_resource_default_props(obj, resource)
        return resource
    _RESOURCE_CACHES.pop(key, None)

    path = _resolve_dyng_path(str(obj.get("witcher_path", "") or ""))
    if not path or not _is_dyng_path(path) or not os.path.exists(path):
        obj[DYNG_SIM_STATUS_PROP] = "No Dyng resource path"
        return None
    resource = attach_dyng_resource_to_object(obj, path)
    if resource is not None:
        payload = str(obj.get(DYNG_DATA_PROP, "") or "")
        if payload:
            _RESOURCE_CACHES[key] = (payload, resource)
        _ensure_resource_default_props(obj, resource)
    return resource


def _object_ancestors(obj: Optional[bpy.types.Object]) -> List[bpy.types.Object]:
    ancestors: List[bpy.types.Object] = []
    while obj is not None:
        ancestors.append(obj)
        obj = getattr(obj, "parent", None)
    return ancestors


def character_root_for_object(obj: Optional[bpy.types.Object]) -> Optional[bpy.types.Object]:
    if obj is None:
        return None
    ancestors = _object_ancestors(obj)
    for candidate in ancestors:
        if str(candidate.get("witcher_app_name", "") or "").strip():
            return candidate
    if ancestors:
        return ancestors[-1]
    return None


def find_character_root(context: bpy.types.Context) -> Optional[bpy.types.Object]:
    obj = getattr(context, "active_object", None)
    root = character_root_for_object(obj)
    if root is not None:
        return root
    for obj in getattr(context, "selected_objects", []) or []:
        root = character_root_for_object(obj)
        if root is not None:
            return root
    return None


def _is_descendant_of(obj: bpy.types.Object, root: bpy.types.Object) -> bool:
    current: Optional[bpy.types.Object] = obj
    while current is not None:
        if current == root:
            return True
        current = getattr(current, "parent", None)
    return False


def dyng_objects_for_character(root: Optional[bpy.types.Object]) -> List[bpy.types.Object]:
    return physics_runtime.objects_for_character(
        root,
        bpy.data.objects,
        is_runtime_kind=is_dyng_armature,
        is_descendant_of=_is_descendant_of,
    )


def dyng_objects_for_context(context: bpy.types.Context) -> List[bpy.types.Object]:
    return physics_runtime.objects_for_context(
        context,
        find_root=find_character_root,
        find_armature_fn=find_dyng_armature,
        objects_for_character_fn=dyng_objects_for_character,
    )


def enable_dyng_object(obj: bpy.types.Object, enabled: bool) -> bool:
    resource = load_resource_for_object(obj)
    if resource is None:
        return False
    clear_state(obj)
    clear_cache(obj)
    if enabled and not live_preview_enabled(getattr(bpy.context, "scene", None)):
        ensure_default_props(obj, enabled=False)
        _RUNTIME_OBJECT_NAMES.discard(_state_key(obj))
        remove_frame_handler()
        obj[DYNG_SIM_STATUS_PROP] = "Live preview is off"
        return False
    ensure_default_props(obj, enabled=enabled)
    if enabled:
        _RUNTIME_OBJECT_NAMES.add(_state_key(obj))
        ensure_frame_handler()
        obj[DYNG_SIM_STATUS_PROP] = "Dyng runtime enabled"
    else:
        _RUNTIME_OBJECT_NAMES.discard(_state_key(obj))
        _restore_external_constraints(obj)
        clear_state(obj)
        obj[DYNG_SIM_STATUS_PROP] = "Dyng runtime disabled"
        if not _RUNTIME_OBJECT_NAMES and not enabled_dyng_objects(bpy.context.scene):
            remove_frame_handler()
    return True


def enable_dyng_objects(objects: Sequence[bpy.types.Object], enabled: bool) -> int:
    return physics_runtime.enable_objects(objects, enabled, enable_dyng_object)


def restore_import_default_runtime(scene: Optional[bpy.types.Scene] = None) -> int:
    """Re-enable physics armatures imported while runtime defaults were temporarily off."""

    restored = 0
    for obj in getattr(bpy.data, "objects", []) or []:
        if not is_dyng_armature(obj):
            continue
        status = str(obj.get(DYNG_SIM_STATUS_PROP, "") or "")
        missing_flags = DYNG_ENABLED_PROP not in obj or DYNG_RUNTIME_OPT_IN_PROP not in obj
        temporarily_ready = status in {"Dyng runtime ready", "Live preview is off"}
        if not missing_flags and not temporarily_ready:
            continue
        ensure_default_props(obj, enabled=True)
        _ensure_resource_default_props(obj, resource_from_object(obj))
        obj[DYNG_BLEND_PROP] = 1.0
        obj[DYNG_ACCESSORY_PREVIEW_PROP] = True
        obj[DYNG_SIM_STATUS_PROP] = "Dyng runtime enabled"
        _RUNTIME_OBJECT_NAMES.add(_state_key(obj))
        restored += 1
    if restored:
        set_live_preview_enabled(scene, True)
        ensure_frame_handler()
    return restored


def clear_state(obj: Optional[bpy.types.Object] = None) -> None:
    if obj is None:
        _STATES.clear()
        _LAST_FRAMES.clear()
        _CONSTRAINT_MUTES.clear()
    else:
        key = _state_key(obj)
        _STATES.pop(key, None)
        _LAST_FRAMES.pop(key, None)


def clear_cache(obj: Optional[bpy.types.Object] = None) -> None:
    if obj is None:
        _FRAME_CACHES.clear()
        return
    _FRAME_CACHES.pop(_state_key(obj), None)
    obj[DYNG_CACHE_STATUS_PROP] = "Cache cleared"


def redkit_preset_names() -> Tuple[str, ...]:
    return tuple(preset.name for preset in DYNG_USER_PRESETS)


def saved_user_preset_names() -> Tuple[str, ...]:
    return physics_presets.saved_preset_names("dyng")


def user_preset_names() -> Tuple[str, ...]:
    names = list(redkit_preset_names())
    for name in saved_user_preset_names():
        if name not in names:
            names.append(name)
    return tuple(names)


def _user_preset_by_name(name: str) -> Optional[DyngUserPreset]:
    lowered = str(name or "").lower()
    for preset in DYNG_USER_PRESETS:
        if preset.name.lower() == lowered:
            return preset
    return None


def apply_user_preset(obj: bpy.types.Object, preset_name: str) -> bool:
    if not is_dyng_armature(obj):
        return False
    name = physics_presets.normalize_preset_name(preset_name)
    preset = _user_preset_by_name(name)
    saved = None if preset is not None else physics_presets.get_preset("dyng", name)
    if preset is None and saved is None:
        return False
    ensure_default_props(obj)
    resource = load_resource_for_object(obj)
    _ensure_resource_default_props(obj, resource)
    if preset is not None:
        values = {
            "preset": preset.name,
            "gravity": preset.gravity,
            "dampening": preset.dampening,
            "speed": preset.speed,
            "linkIterations": int(preset.link_iterations),
            "useOffsets": bool(preset.use_offsets),
            "planeCollision": bool(preset.plane_collision),
            "bodyCollision": bool(preset.body_collision),
            "bodyCollisionRadius": float(preset.body_collision_radius),
            "bodyCollisionStrength": float(preset.body_collision_strength),
            "shake": preset.shake,
            "wind": preset.wind,
            "blend": preset.blend,
            "accessoryPreview": True,
        }
    else:
        values = dict(saved or {})
        values["preset"] = name
    obj[DYNG_PRESET_PROP] = str(values.get("preset", name) or name)
    obj[DYNG_GRAVITY_PROP] = float(values.get("gravity", obj.get(DYNG_GRAVITY_PROP, 1.0)) or 1.0)
    obj[DYNG_DAMPENING_PROP] = float(values.get("dampening", obj.get(DYNG_DAMPENING_PROP, 0.95)) or 0.95)
    obj[DYNG_SPEED_PROP] = float(values.get("speed", obj.get(DYNG_SPEED_PROP, 1.0)) or 1.0)
    obj[DYNG_LINK_ITERATIONS_PROP] = int(values.get("linkIterations", obj.get(DYNG_LINK_ITERATIONS_PROP, 10)) or 10)
    obj[DYNG_USE_OFFSETS_PROP] = bool(values.get("useOffsets", obj.get(DYNG_USE_OFFSETS_PROP, _default_use_offsets(resource))))
    obj[DYNG_PLANE_COLLISION_PROP] = bool(values.get("planeCollision", obj.get(DYNG_PLANE_COLLISION_PROP, _default_plane_collision(resource))))
    obj[DYNG_BODY_COLLISION_PROP] = bool(values.get("bodyCollision", obj.get(DYNG_BODY_COLLISION_PROP, _default_body_collision(obj, resource))))
    obj[DYNG_BODY_COLLISION_RADIUS_PROP] = float(values.get("bodyCollisionRadius", obj.get(DYNG_BODY_COLLISION_RADIUS_PROP, _default_body_collision_radius(obj, resource))) or 0.0)
    obj[DYNG_BODY_COLLISION_STRENGTH_PROP] = float(values.get("bodyCollisionStrength", obj.get(DYNG_BODY_COLLISION_STRENGTH_PROP, 1.0)) or 0.0)
    obj[DYNG_SHAKE_PROP] = float(values.get("shake", obj.get(DYNG_SHAKE_PROP, 0.0)) or 0.0)
    obj[DYNG_WIND_PROP] = float(values.get("wind", obj.get(DYNG_WIND_PROP, _default_wind_strength(resource))) or 0.0)
    obj[DYNG_BLEND_PROP] = float(values.get("blend", obj.get(DYNG_BLEND_PROP, _default_blend(resource))) or 0.0)
    obj[DYNG_ACCESSORY_PREVIEW_PROP] = bool(values.get("accessoryPreview", obj.get(DYNG_ACCESSORY_PREVIEW_PROP, True)))
    clear_state(obj)
    clear_cache(obj)
    obj[DYNG_SIM_STATUS_PROP] = f"Loaded Dyng Blender preset {obj[DYNG_PRESET_PROP]}"
    return True


def save_user_preset(obj: bpy.types.Object, preset_name: str) -> str:
    if not is_dyng_armature(obj):
        raise ValueError("Object is not a Dyng armature")
    name = physics_presets.normalize_preset_name(preset_name)
    if not name:
        raise ValueError("Preset name is empty")
    if name in redkit_preset_names():
        raise ValueError("REDkit presets are read-only")
    ensure_default_props(obj)
    resource = load_resource_for_object(obj)
    _ensure_resource_default_props(obj, resource)
    values = {
        "preset": name,
        "gravity": float(obj.get(DYNG_GRAVITY_PROP, 1.0) or 1.0),
        "dampening": float(obj.get(DYNG_DAMPENING_PROP, 0.95) or 0.95),
        "speed": float(obj.get(DYNG_SPEED_PROP, 1.0) or 1.0),
        "linkIterations": int(obj.get(DYNG_LINK_ITERATIONS_PROP, 10) or 10),
        "useOffsets": bool(obj.get(DYNG_USE_OFFSETS_PROP, _default_use_offsets(resource))),
        "planeCollision": bool(obj.get(DYNG_PLANE_COLLISION_PROP, _default_plane_collision(resource))),
        "bodyCollision": bool(obj.get(DYNG_BODY_COLLISION_PROP, _default_body_collision(obj, resource))),
        "bodyCollisionRadius": float(obj.get(DYNG_BODY_COLLISION_RADIUS_PROP, _default_body_collision_radius(obj, resource)) or 0.0),
        "bodyCollisionStrength": float(obj.get(DYNG_BODY_COLLISION_STRENGTH_PROP, 1.0) or 0.0),
        "shake": float(obj.get(DYNG_SHAKE_PROP, 0.0) or 0.0),
        "wind": float(obj.get(DYNG_WIND_PROP, _default_wind_strength(resource)) or 0.0),
        "blend": float(obj.get(DYNG_BLEND_PROP, _default_blend(resource)) or 0.0),
        "accessoryPreview": bool(obj.get(DYNG_ACCESSORY_PREVIEW_PROP, False)),
    }
    saved_name = physics_presets.save_preset("dyng", name, values)
    obj[DYNG_PRESET_PROP] = saved_name
    obj[DYNG_SIM_STATUS_PROP] = f"Saved Dyng Blender preset {saved_name}"
    return saved_name


def delete_user_preset(preset_name: str) -> bool:
    return physics_presets.delete_preset("dyng", preset_name)


def _rig_uses_rot90(obj: Optional[bpy.types.Object]) -> bool:
    return physics_runtime.rig_uses_rot90(obj)


def _matrix_to_solver_space(obj: bpy.types.Object, matrix: Matrix) -> Matrix:
    if not _rig_uses_rot90(obj):
        return matrix.copy()
    return matrix @ _ROT90_TO_GAME


def _matrix_from_solver_space(obj: bpy.types.Object, matrix: Matrix) -> Matrix:
    if not _rig_uses_rot90(obj):
        return matrix.copy()
    return matrix @ _ROT90_FROM_GAME


def _transform_from_blender_matrix(matrix: Matrix, obj: Optional[bpy.types.Object] = None):
    if obj is not None:
        matrix = _matrix_to_solver_space(obj, matrix)
    x_axis = (float(matrix[0][0]), float(matrix[1][0]), float(matrix[2][0]))
    y_axis = (float(matrix[0][1]), float(matrix[1][1]), float(matrix[2][1]))
    z_axis = (float(matrix[0][2]), float(matrix[1][2]), float(matrix[2][2]))
    position = (float(matrix[0][3]), float(matrix[1][3]), float(matrix[2][3]))
    return transform_from_axes(x_axis, y_axis, z_axis, position)


def _blender_matrix_from_transform(transform, obj: Optional[bpy.types.Object] = None) -> Matrix:
    x_axis = transform[0]
    y_axis = transform[1]
    z_axis = transform[2]
    position = matrix_translation(transform)
    matrix = Matrix(
        (
            (x_axis[0], y_axis[0], z_axis[0], position[0]),
            (x_axis[1], y_axis[1], z_axis[1], position[1]),
            (x_axis[2], y_axis[2], z_axis[2], position[2]),
            (0.0, 0.0, 0.0, 1.0),
        )
    )
    if obj is not None:
        return _matrix_from_solver_space(obj, matrix)
    return matrix


def _blend_matrix(target_matrix: Matrix, simulated_matrix: Matrix, weight: float) -> Matrix:
    weight = max(0.0, min(1.0, float(weight)))
    if weight >= 0.999:
        return simulated_matrix
    if weight <= 0.001:
        return target_matrix.copy()

    location = target_matrix.translation.lerp(simulated_matrix.translation, weight)
    try:
        rotation = target_matrix.to_quaternion().slerp(simulated_matrix.to_quaternion(), weight)
        scale = target_matrix.to_scale().lerp(simulated_matrix.to_scale(), weight)
        return Matrix.LocRotScale(location, rotation, scale)
    except Exception:
        blended = target_matrix.copy()
        blended.translation = location
        return blended

def _set_pose_bone_matrix_basis(
    armature: bpy.types.Object,
    pose_bone: bpy.types.PoseBone,
    desired_matrix: Matrix,
    parent_matrix: Optional[Matrix],
) -> None:
    bone = armature.data.bones.get(pose_bone.name)
    if bone is None:
        pose_bone.matrix = desired_matrix
        return
    if bone.parent is None:
        pose_bone.matrix_basis = bone.matrix_local.inverted_safe() @ desired_matrix
        return
    if parent_matrix is None:
        parent_matrix = pose_bone.parent.matrix.copy() if pose_bone.parent else Matrix.Identity(4)
    rest_local = bone.parent.matrix_local.inverted_safe() @ bone.matrix_local
    pose_bone.matrix_basis = rest_local.inverted_safe() @ parent_matrix.inverted_safe() @ desired_matrix


def _apply_desired_dynamic_matrices(
    obj: bpy.types.Object,
    resource: DyngResourceData,
    desired_matrices: Sequence[Optional[Matrix]],
    target_matrices: Sequence[Optional[Matrix]],
) -> int:
    name_to_index = {node.name: index for index, node in enumerate(resource.nodes)}
    applied_matrices: List[Optional[Matrix]] = [
        matrix.copy() if matrix is not None else None
        for matrix in target_matrices
    ]
    updated = 0
    for index in _dynamic_bone_indices(resource):
        if index >= len(desired_matrices):
            continue
        desired_matrix = desired_matrices[index]
        if desired_matrix is None:
            continue
        node = resource.nodes[index]
        pose_bone = obj.pose.bones.get(node.name)
        if pose_bone is None or not _can_simulate_pose_bone(pose_bone):
            continue
        _mute_external_copy_transforms(pose_bone)
        parent_index = name_to_index.get(node.parent)
        parent_matrix = None
        if parent_index is not None and parent_index < len(applied_matrices):
            parent_matrix = applied_matrices[parent_index]
        elif pose_bone.parent is not None:
            parent_matrix = pose_bone.parent.matrix.copy()
        _set_pose_bone_matrix_basis(obj, pose_bone, desired_matrix, parent_matrix)
        applied_matrices[index] = desired_matrix.copy()
        updated += 1
    if updated:
        obj.update_tag()
    return updated


def _scene_wind(scene: bpy.types.Scene) -> Tuple[float, Tuple[float, float, float]]:
    if not bool(getattr(scene, SCENE_WIND_ENABLED_ATTR, False)):
        return 0.0, (0.0, 0.0, 0.0)
    wind_object = wind_object_from_scene(scene)
    if wind_object is not None:
        field = getattr(wind_object, "field", None)
        raw_speed = float(getattr(field, "strength", 0.0) or 0.0)
        direction = Vector(wind_direction_from_object(wind_object))
        if raw_speed < 0.0:
            direction.negate()
        speed = abs(raw_speed)
    else:
        speed = max(0.0, float(getattr(scene, SCENE_WIND_SPEED_ATTR, 0.0) or 0.0))
        direction_value = getattr(scene, SCENE_WIND_DIRECTION_ATTR, (1.0, 0.0, 0.0))
        direction = Vector((float(direction_value[0]), float(direction_value[1]), float(direction_value[2])))
    if direction.length <= 1e-6 or speed <= 1e-6:
        return 0.0, (0.0, 0.0, 0.0)
    direction.normalize()
    return speed * _BLENDER_WIND_SPEED_SCALE, (float(direction.x), float(direction.y), float(direction.z))


def _bone_rest_local_matrix(armature: bpy.types.Object, bone_name: str) -> Matrix:
    bone = armature.data.bones.get(bone_name)
    if bone is None:
        return Matrix.Identity(4)
    matrix = bone.matrix_local.copy()
    if bone.parent is not None:
        return bone.parent.matrix_local.inverted_safe() @ matrix
    return matrix


def _pose_bone_is_externally_driven(pose_bone: bpy.types.PoseBone) -> bool:
    for constraint in pose_bone.constraints:
        target = getattr(constraint, "target", None)
        if target is not None and target != pose_bone.id_data:
            return True
    return False


def _external_copy_transform_constraints(
    pose_bone: bpy.types.PoseBone,
) -> List[bpy.types.Constraint]:
    constraints = []
    for constraint in pose_bone.constraints:
        target = getattr(constraint, "target", None)
        if constraint.type == "COPY_TRANSFORMS" and target is not None and target != pose_bone.id_data:
            constraints.append(constraint)
    return constraints


def _dangle_buffer_aligned_matrix(
    target: bpy.types.Object,
    bone_name: str,
    cache: Optional[Dict[str, Matrix]] = None,
) -> Optional[Matrix]:
    if str(target.get("witcher_type", "")) != "CAnimDangleBufferComponent":
        return None
    if not bone_name.startswith("dyng_"):
        return None
    pose_bone = target.pose.bones.get(bone_name)
    if pose_bone is None:
        return None

    if cache is None:
        cache = {}
    cached = cache.get(bone_name)
    if cached is not None:
        return cached.copy()

    local = _bone_rest_local_matrix(target, bone_name)
    if pose_bone.parent is None:
        bone = target.data.bones.get(bone_name)
        matrix = bone.matrix_local.copy() if bone is not None else pose_bone.matrix.copy()
    elif pose_bone.parent.name.startswith("dyng_"):
        parent_matrix = _dangle_buffer_aligned_matrix(target, pose_bone.parent.name, cache)
        if parent_matrix is None:
            parent_matrix = pose_bone.parent.matrix.copy()
        matrix = parent_matrix @ local
    else:
        matrix = pose_bone.parent.matrix.copy() @ local
    cache[bone_name] = matrix.copy()
    return matrix


def _external_copy_transform_matrix(pose_bone: bpy.types.PoseBone) -> Optional[Matrix]:
    owner = pose_bone.id_data
    buffer_cache: Dict[str, Matrix] = {}
    for constraint in _external_copy_transform_constraints(pose_bone):
        target = getattr(constraint, "target", None)
        subtarget = str(getattr(constraint, "subtarget", "") or "")
        if target is None or target.type != "ARMATURE" or not subtarget:
            continue
        target_bone = target.pose.bones.get(subtarget)
        if target_bone is None:
            continue
        target_matrix = _dangle_buffer_aligned_matrix(target, subtarget, buffer_cache)
        if target_matrix is None:
            target_matrix = target_bone.matrix
        return owner.matrix_world.inverted_safe() @ target.matrix_world @ target_matrix
    return None


def _constraint_mute_key(pose_bone: bpy.types.PoseBone, constraint: bpy.types.Constraint) -> Tuple[str, str, str]:
    return (_state_key(pose_bone.id_data), str(pose_bone.name), str(constraint.name))


def _mute_external_copy_transforms(pose_bone: bpy.types.PoseBone) -> None:
    for constraint in _external_copy_transform_constraints(pose_bone):
        key = _constraint_mute_key(pose_bone, constraint)
        if key not in _CONSTRAINT_MUTES:
            obj = pose_bone.id_data
            if is_dyng_runtime_enabled(obj) and bool(constraint.mute):
                _CONSTRAINT_MUTES[key] = False
            else:
                _CONSTRAINT_MUTES[key] = bool(constraint.mute)
        constraint.mute = True


def _restore_external_constraints(obj: bpy.types.Object) -> None:
    if obj.type != "ARMATURE":
        return
    for pose_bone in obj.pose.bones:
        for constraint in pose_bone.constraints:
            key = _constraint_mute_key(pose_bone, constraint)
            if key in _CONSTRAINT_MUTES:
                constraint.mute = bool(_CONSTRAINT_MUTES.pop(key))


def _can_simulate_pose_bone(pose_bone: bpy.types.PoseBone) -> bool:
    if not _pose_bone_is_externally_driven(pose_bone):
        return True
    return _external_copy_transform_matrix(pose_bone) is not None


def _target_blender_matrices(
    armature: bpy.types.Object,
    resource: DyngResourceData,
) -> Tuple[List[Optional[Matrix]], List]:
    name_to_index = {node.name: index for index, node in enumerate(resource.nodes)}
    matrices: List[Optional[Matrix]] = [None] * len(resource.nodes)
    transforms = [None] * len(resource.nodes)

    def build(index: int) -> Matrix:
        cached = matrices[index]
        if cached is not None:
            return cached

        node = resource.nodes[index]
        pose_bone = armature.pose.bones.get(node.name)
        if pose_bone is not None and _pose_bone_is_externally_driven(pose_bone):
            matrix = _external_copy_transform_matrix(pose_bone)
            if matrix is None:
                matrix = pose_bone.matrix.copy()
        else:
            local = _bone_rest_local_matrix(armature, node.name)
            parent_index = name_to_index.get(node.parent)
            if parent_index is not None and parent_index != index:
                matrix = build(parent_index) @ local
            elif pose_bone is not None:
                bone = armature.data.bones.get(node.name)
                matrix = bone.matrix_local.copy() if bone is not None else pose_bone.matrix.copy()
            else:
                matrix = Matrix.Identity(4)

        matrices[index] = matrix
        transforms[index] = _transform_from_blender_matrix(matrix, armature)
        return matrix

    for index in range(len(resource.nodes)):
        build(index)
    return matrices, transforms


def _dynamic_bone_indices(resource: DyngResourceData) -> List[int]:
    return [
        index
        for index, node in enumerate(resource.nodes)
        if node.distance > 0.0 or node.name.startswith("dyng_")
    ]


def _get_state(
    obj: bpy.types.Object,
    resource: DyngResourceData,
    target_transforms: Sequence,
    *,
    reset: bool = False,
) -> _ObjectState:
    key = _state_key(obj)
    state = _STATES.get(key)
    rot90 = _rig_uses_rot90(obj)
    if reset or state is None or state.resource != resource or state.rot90 != rot90:
        simulator = DyngSimulator(resource, target_transforms)
        state = _ObjectState(resource=resource, simulator=simulator, rot90=rot90)
        _STATES[key] = state
    return state


def _cache_settings(scene: bpy.types.Scene, obj: bpy.types.Object) -> Tuple:
    wind_direction = getattr(scene, SCENE_WIND_DIRECTION_ATTR, (1.0, 0.0, 0.0))
    wind_object = wind_object_from_scene(scene)
    if wind_object is not None:
        field = getattr(wind_object, "field", None)
        wind_object_key = (
            getattr(wind_object, "name", ""),
            tuple(round(float(value), 6) for row in wind_object.matrix_world for value in row),
            round(float(getattr(field, "strength", 0.0) or 0.0), 6),
        )
    else:
        wind_object_key = ("",)
    resource = load_resource_for_object(obj)
    default_use_offsets = _default_use_offsets(resource)
    default_plane_collision = _default_plane_collision(resource)
    default_body_collision = _default_body_collision(obj, resource)
    default_body_collision_radius = _default_body_collision_radius(obj, resource)
    default_wind = _default_wind_strength(resource)
    default_blend = _default_blend(resource)
    return (
        float(obj.get(DYNG_GRAVITY_PROP, 1.0) or 1.0),
        float(obj.get(DYNG_DAMPENING_PROP, 0.95) or 0.95),
        float(obj.get(DYNG_SPEED_PROP, 1.0) or 1.0),
        int(obj.get(DYNG_LINK_ITERATIONS_PROP, 10) or 10),
        bool(obj.get(DYNG_USE_OFFSETS_PROP, default_use_offsets)),
        bool(obj.get(DYNG_PLANE_COLLISION_PROP, default_plane_collision)),
        bool(obj.get(DYNG_BODY_COLLISION_PROP, default_body_collision)),
        round(_float_prop(obj, DYNG_BODY_COLLISION_RADIUS_PROP, default_body_collision_radius), 6),
        round(_float_prop(obj, DYNG_BODY_COLLISION_STRENGTH_PROP, 1.0), 6),
        round(_float_prop(obj, DYNG_SHAKE_PROP, 0.0), 6),
        round(_float_prop(obj, DYNG_WIND_PROP, default_wind), 6),
        round(_clamped_float_prop(obj, DYNG_BLEND_PROP, default_blend, 0.0, 1.0), 6),
        bool(obj.get(DYNG_ACCESSORY_PREVIEW_PROP, False)),
        bool(_rig_uses_rot90(obj)),
        bool(getattr(scene, SCENE_WIND_ENABLED_ATTR, False)),
        wind_object_key,
        tuple(round(float(value), 6) for value in wind_direction),
        round(float(getattr(scene, SCENE_WIND_SPEED_ATTR, 0.0) or 0.0), 6),
    )


def _capture_dynamic_matrices(
    obj: bpy.types.Object,
    resource: DyngResourceData,
) -> Dict[str, Matrix]:
    matrices: Dict[str, Matrix] = {}
    for index in _dynamic_bone_indices(resource):
        pose_bone = obj.pose.bones.get(resource.nodes[index].name)
        if pose_bone is None or not _can_simulate_pose_bone(pose_bone):
            continue
        matrices[pose_bone.name] = pose_bone.matrix.copy()
    return matrices


def apply_cached_frame(obj: bpy.types.Object, scene: bpy.types.Scene, frame: int, *, update_status: bool = True) -> bool:
    cache = _FRAME_CACHES.get(_state_key(obj))
    if cache is None or cache.settings != _cache_settings(scene, obj):
        return False
    matrices = cache.matrices.get(int(frame))
    if matrices is None:
        return False
    resource = load_resource_for_object(obj)
    if resource is None:
        return False
    target_mats, _target_transforms = _target_blender_matrices(obj, resource)
    desired_mats: List[Optional[Matrix]] = [
        matrices.get(node.name)
        for node in resource.nodes
    ]
    updated = _apply_desired_dynamic_matrices(obj, resource, desired_mats, target_mats)
    if updated:
        if update_status:
            obj[DYNG_SIM_STATUS_PROP] = f"Cache frame {frame}"
        return True
    return False


def step_object(
    obj: bpy.types.Object,
    dt: float,
    *,
    reset: bool = False,
    relaxed: bool = False,
    update_status: bool = True,
) -> bool:
    if not is_dyng_armature(obj):
        return False
    ensure_default_props(obj)
    resource = load_resource_for_object(obj)
    if resource is None or not resource.nodes:
        return False
    _ensure_resource_default_props(obj, resource)

    target_mats, target_transforms = _target_blender_matrices(obj, resource)
    state = _get_state(obj, resource, target_transforms, reset=reset)
    scene = bpy.context.scene
    wind_speed, wind_vector = _scene_wind(scene)
    wind_strength = wind_speed * _clamped_float_prop(
        obj,
        DYNG_WIND_PROP,
        _default_wind_strength(resource),
        0.0,
        10.0,
    )
    accessory_preview = bool(obj.get(DYNG_ACCESSORY_PREVIEW_PROP, False))
    if _is_accessory_resource(resource) and not accessory_preview:
        wind_strength = 0.0
        blend = 0.0
    else:
        blend = _clamped_float_prop(obj, DYNG_BLEND_PROP, _default_blend(resource), 0.0, 1.0)
    transforms = state.simulator.step(
        target_transforms,
        dt,
        speed=float(obj.get(DYNG_SPEED_PROP, 1.0) or 1.0),
        dampening=float(obj.get(DYNG_DAMPENING_PROP, 0.95) or 0.95),
        gravity=float(obj.get(DYNG_GRAVITY_PROP, 1.0) or 1.0),
        wind=wind_strength,
        wind_vector=wind_vector,
        use_offsets=_bool_prop(obj, DYNG_USE_OFFSETS_PROP, _default_use_offsets(resource)),
        plane_collision=_bool_prop(obj, DYNG_PLANE_COLLISION_PROP, _default_plane_collision(resource)),
        body_collision=_bool_prop(obj, DYNG_BODY_COLLISION_PROP, _default_body_collision(obj, resource)),
        body_collision_radius=_float_prop(
            obj,
            DYNG_BODY_COLLISION_RADIUS_PROP,
            _default_body_collision_radius(obj, resource),
        ),
        body_collision_strength=_float_prop(obj, DYNG_BODY_COLLISION_STRENGTH_PROP, 1.0),
        shake=_float_prop(obj, DYNG_SHAKE_PROP, 0.0),
        max_link_iterations=int(obj.get(DYNG_LINK_ITERATIONS_PROP, 10) or 10),
        force_reset=reset,
        relaxed=relaxed,
    )

    desired_mats: List[Optional[Matrix]] = [None] * len(resource.nodes)
    for index in _dynamic_bone_indices(resource):
        node = resource.nodes[index]
        pose_bone = obj.pose.bones.get(node.name)
        if pose_bone is None or not _can_simulate_pose_bone(pose_bone):
            continue
        target_matrix = target_mats[index]
        if target_matrix is None:
            target_matrix = _blender_matrix_from_transform(target_transforms[index], obj)
        simulated_matrix = _blender_matrix_from_transform(transforms[index], obj)
        desired_mats[index] = _blend_matrix(target_matrix, simulated_matrix, blend)

    updated = _apply_desired_dynamic_matrices(obj, resource, desired_mats, target_mats)
    if update_status:
        obj[DYNG_LAST_STEP_PROP] = float(dt)
        obj[DYNG_SIM_STATUS_PROP] = f"Updated {updated} Dyng bones"
    return updated > 0


def reset_object(obj: bpy.types.Object, *, update_status: bool = True) -> bool:
    if not is_dyng_armature(obj):
        return False
    resource = load_resource_for_object(obj)
    if resource is None:
        return False
    _ensure_resource_default_props(obj, resource)
    target_mats, target_transforms = _target_blender_matrices(obj, resource)
    clear_state(obj)
    state = _get_state(obj, resource, target_transforms, reset=True)
    state.simulator.force_reset(target_transforms)
    desired_mats: List[Optional[Matrix]] = [None] * len(resource.nodes)
    for index in _dynamic_bone_indices(resource):
        node = resource.nodes[index]
        pose_bone = obj.pose.bones.get(node.name)
        if pose_bone is None or not _can_simulate_pose_bone(pose_bone):
            continue
        target_matrix = target_mats[index]
        if target_matrix is None:
            target_matrix = _blender_matrix_from_transform(target_transforms[index], obj)
        desired_mats[index] = target_matrix.copy()
    _apply_desired_dynamic_matrices(obj, resource, desired_mats, target_mats)
    if update_status:
        obj[DYNG_SIM_STATUS_PROP] = "Dyng reset"
    return True


def enabled_dyng_objects(scene: bpy.types.Scene) -> List[bpy.types.Object]:
    return physics_runtime.enabled_runtime_objects(
        scene,
        getattr(bpy.data, "objects", []),
        live_preview_enabled=live_preview_enabled,
        is_runtime_kind=is_dyng_armature,
        is_runtime_enabled=is_dyng_runtime_enabled,
    )


def _refresh_runtime_object_names(scene: Optional[bpy.types.Scene] = None) -> None:
    physics_runtime.refresh_runtime_object_names(
        scene,
        _RUNTIME_OBJECT_NAMES,
        getattr(bpy.data, "objects", []),
        live_preview_enabled=live_preview_enabled,
        is_runtime_kind=is_dyng_armature,
        has_runtime_opt_in=_has_dyng_runtime_opt_in_props,
        key_fn=_state_key,
    )


def _object_by_runtime_name(name: str) -> Optional[bpy.types.Object]:
    return physics_runtime.object_by_runtime_name(
        name,
        getattr(bpy.data, "objects", []),
        key_fn=_state_key,
    )


def _runtime_dyng_objects(scene: bpy.types.Scene) -> List[bpy.types.Object]:
    return physics_runtime.runtime_objects(
        scene,
        _RUNTIME_OBJECT_NAMES,
        getattr(bpy.data, "objects", []),
        live_preview_enabled=live_preview_enabled,
        is_runtime_kind=is_dyng_armature,
        has_runtime_opt_in=_has_dyng_runtime_opt_in_props,
        is_runtime_enabled=is_dyng_runtime_enabled,
        key_fn=_state_key,
    )


def frame_dt(scene: bpy.types.Scene, obj: bpy.types.Object) -> Tuple[float, bool]:
    return physics_runtime.frame_dt(
        scene,
        obj,
        _LAST_FRAMES,
        reset_frame_gap=_DYNG_RESET_FRAME_GAP,
        max_advance_frames=_DYNG_MAX_ADVANCE_FRAMES,
        key_fn=_state_key,
    )


def _is_rest_frame(scene: bpy.types.Scene) -> bool:
    return physics_runtime.is_rest_frame(scene)


@persistent
def dyng_frame_change_post(scene: bpy.types.Scene) -> None:
    if _SUPPRESS_FRAME_HANDLER:
        return
    objects = _runtime_dyng_objects(scene)
    if not objects:
        remove_frame_handler()
        return
    for obj in objects:
        try:
            current = int(scene.frame_current)
            if _is_rest_frame(scene):
                reset_object(obj, update_status=False)
                _LAST_FRAMES[_state_key(obj)] = current
                continue
            if apply_cached_frame(obj, scene, current, update_status=False):
                _LAST_FRAMES[_state_key(obj)] = current
                continue
            dt, reset = frame_dt(scene, obj)
            step_object(obj, dt, reset=reset, update_status=False)
        except Exception:
            obj[DYNG_SIM_STATUS_PROP] = "Dyng update failed"
            log.warning("Dyng frame update failed for %s", obj.name, exc_info=True)


def ensure_frame_handler() -> None:
    physics_runtime.ensure_frame_handler(
        bpy.app.handlers.frame_change_post,
        dyng_frame_change_post,
        handler_name="dyng_frame_change_post",
        module_suffix="dyng_blender",
        runtime_names=_RUNTIME_OBJECT_NAMES,
        live_preview_enabled=live_preview_enabled,
        refresh_names=_refresh_runtime_object_names,
        scene=getattr(bpy.context, "scene", None),
    )


def remove_frame_handler() -> None:
    physics_runtime.remove_frame_handler(
        bpy.app.handlers.frame_change_post,
        "dyng_frame_change_post",
        "dyng_blender",
    )


def bake_object(
    context: bpy.types.Context,
    obj: bpy.types.Object,
    frame_start: int,
    frame_end: int,
) -> int:
    frame_start, frame_end = physics_runtime.normalized_frame_range(frame_start, frame_end)
    resource = load_resource_for_object(obj)
    if resource is None:
        return 0
    clear_state(obj)
    baked = 0
    scene = context.scene
    original_frame = int(scene.frame_current)
    dt = physics_runtime.scene_frame_dt(scene)
    global _SUPPRESS_FRAME_HANDLER
    was_suppressed = _SUPPRESS_FRAME_HANDLER
    _SUPPRESS_FRAME_HANDLER = True
    try:
        for frame in range(frame_start, frame_end + 1):
            scene.frame_set(frame)
            if frame == frame_start or frame <= 0:
                reset_object(obj, update_status=False)
            else:
                step_object(obj, dt, reset=False, update_status=False)
            for index in _dynamic_bone_indices(resource):
                pose_bone = obj.pose.bones.get(resource.nodes[index].name)
                if pose_bone is None or not _can_simulate_pose_bone(pose_bone):
                    continue
                physics_runtime.keyframe_pose_bone_transform(pose_bone, frame)
                baked += 1
    finally:
        try:
            scene.frame_set(original_frame)
        finally:
            _SUPPRESS_FRAME_HANDLER = was_suppressed
    obj[DYNG_SIM_STATUS_PROP] = f"Baked {frame_start}-{frame_end}"
    return baked


def build_cache_for_object(
    context: bpy.types.Context,
    obj: bpy.types.Object,
    frame_start: int,
    frame_end: int,
) -> int:
    frame_start, frame_end = physics_runtime.normalized_frame_range(frame_start, frame_end)
    resource = load_resource_for_object(obj)
    if resource is None:
        return 0
    clear_state(obj)
    clear_cache(obj)

    scene = context.scene
    original_frame = int(scene.frame_current)
    dt = physics_runtime.scene_frame_dt(scene)
    cache = _FrameCache(
        frame_start=int(frame_start),
        frame_end=int(frame_end),
        settings=_cache_settings(scene, obj),
        matrices={},
    )

    global _SUPPRESS_FRAME_HANDLER
    was_suppressed = _SUPPRESS_FRAME_HANDLER
    _SUPPRESS_FRAME_HANDLER = True
    try:
        for frame in range(frame_start, frame_end + 1):
            scene.frame_set(frame)
            if frame == frame_start or frame <= 0:
                reset_object(obj, update_status=False)
            else:
                step_object(obj, dt, reset=False, update_status=False)
            cache.matrices[int(frame)] = _capture_dynamic_matrices(obj, resource)
    finally:
        try:
            scene.frame_set(original_frame)
        finally:
            _SUPPRESS_FRAME_HANDLER = was_suppressed

    _FRAME_CACHES[_state_key(obj)] = cache
    cached_frames = len(cache.matrices)
    obj[DYNG_CACHE_STATUS_PROP] = f"Cached {frame_start}-{frame_end}"
    obj[DYNG_SIM_STATUS_PROP] = f"Cached {cached_frames} Dyng frames"
    if frame_start <= original_frame <= frame_end:
        apply_cached_frame(obj, scene, original_frame, update_status=False)
    return cached_frames


def build_cache_for_objects(
    context: bpy.types.Context,
    objects: Sequence[bpy.types.Object],
    frame_start: int,
    frame_end: int,
) -> int:
    cached = 0
    for obj in objects:
        cached += build_cache_for_object(context, obj, frame_start, frame_end)
    return cached


def cache_summary(obj: bpy.types.Object) -> str:
    cache = _FRAME_CACHES.get(_state_key(obj))
    if cache is not None:
        return f"Cached {cache.frame_start}-{cache.frame_end}"
    return str(obj.get(DYNG_CACHE_STATUS_PROP, "No cache") or "No cache")


def summarize_object(obj: bpy.types.Object) -> str:
    nodes = int(obj.get(DYNG_NODE_COUNT_PROP, 0) or 0)
    links = int(obj.get(DYNG_LINK_COUNT_PROP, 0) or 0)
    triangles = int(obj.get(DYNG_TRIANGLE_COUNT_PROP, 0) or 0)
    collisions = int(obj.get(DYNG_COLLISION_COUNT_PROP, 0) or 0)
    if nodes:
        return f"{nodes} nodes, {links} links, {triangles} triangles, {collisions} collisions"
    return str(obj.get(DYNG_PARSE_STATUS_PROP, "Dyng data not loaded"))
