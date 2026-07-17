"""World environment loading and preview controls for the Witcher N-panel."""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
from typing import Any, Mapping

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)

from ..environment_catalog import (
    ENVIRONMENT_OFF_IDENTIFIER,
    EnvironmentDefinitionItem,
    scan_environment_definitions,
)
from .ui_cr2w_fields import _draw_imported_class_sections, _schema_from_field_items
from .ui_utils import WITCH_PT_Base


# ColorScaled maps to dimensionless SUN strength in the fixed-luminance preview.
_BLENDER_KEY_LIGHT_ENERGY_SCALE = 1.0
# Preserve the sky brightness before fog is applied.
_BLENDER_SKY_BRIGHTNESS_SCALE = 1.0
# Four shadowless SUNs approximate the ambient-probe hemisphere.
_BLENDER_AMBIENT_FILL_ENERGY_SCALE = 0.60
# Standalone .env files lack the probe atlas; use a rough sky-add fallback.
_BLENDER_REFLECTION_FILL_ENERGY_SCALE = 1.20
# Convert HDR point-light radiance to useful Blender preview watts.
_BLENDER_CAMERA_LIGHT_ENERGY_SCALE = 20.0


_SCENE_PROP = "witcher_environment"
_RUNTIME: dict[int, dict[str, Any]] = {}
_ENVIRONMENT_SELECTOR_CACHE_KEY: tuple[str, ...] | None = None
_ENVIRONMENT_SELECTOR_CATALOG: tuple[EnvironmentDefinitionItem, ...] = ()
_ENVIRONMENT_SELECTOR_LOOKUP: dict[str, EnvironmentDefinitionItem] = {}
_ENVIRONMENT_SELECTOR_ENUM_ITEMS = (
    (
        ENVIRONMENT_OFF_IDENTIFIER,
        "ENV - OFF",
        "Disable the Entity Editor preview environment",
        "WORLD_DATA",
        0,
    ),
)


@dataclass(frozen=True)
class EnvironmentUIResult:
    ok: bool
    message: str
    data: Any = None
    resolved_path: str = ""


def _scene_key(scene) -> int:
    try:
        return int(scene.as_pointer())
    except Exception:
        return id(scene)


def environment_runtime(scene, *, create: bool = True) -> dict[str, Any] | None:
    """Return the non-RNA parsed environment state for *scene*.

    Parsed CR2W objects can be large and are intentionally kept out of the blend
    file.  Paths and UI choices remain persistent in the Scene PropertyGroup.
    """

    key = _scene_key(scene)
    if create:
        return _RUNTIME.setdefault(key, {})
    return _RUNTIME.get(key)


def clear_environment_runtime(scene=None) -> None:
    if scene is None:
        _RUNTIME.clear()
    else:
        _RUNTIME.pop(_scene_key(scene), None)


def _settings(scene):
    return getattr(scene, _SCENE_PROP, None)


def _short_text(value, limit: int = 46) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: max(0, limit - 3)] + "..."


def _status_icon(settings) -> str:
    level = str(getattr(settings, "status_level", "INFO") or "INFO")
    return {"OK": "CHECKMARK", "WARNING": "ERROR", "ERROR": "CANCEL"}.get(level, "INFO")


def _set_status(settings, text: str, *, details: str = "", level: str = "INFO") -> None:
    settings.status_text = _short_text(text)
    settings.status_details = str(details or text or "")
    settings.status_level = level if level in {"INFO", "OK", "WARNING", "ERROR"} else "INFO"


def _source_path(settings, kind: str) -> str:
    return {
        "WORLD": settings.world_path,
        "ENVIRONMENT": settings.environment_path,
        "SCENES_ENVIRONMENT": settings.scenes_environment_path,
        "WEATHER": settings.weather_path,
    }.get(kind, "")


def _absolute_source(path: str, settings=None) -> str:
    text = str(path or "").strip()
    if not text:
        return ""
    expanded = bpy.path.abspath(text)
    if Path(expanded).is_file():
        return str(Path(expanded))

    try:
        from ..importers import import_environment

        world_path = str(getattr(settings, "world_path", "") or "")
        resolved = import_environment.resolve_environment_asset(text, source_path=bpy.path.abspath(world_path))
        return str(resolved or expanded)
    except Exception:
        return expanded


def _core_module():
    from ..CR2W import dc_environment

    return dc_environment


def _preview_module():
    from ..importers import import_environment

    return import_environment


def _as_xyz(value, default=(0.0, 0.0, 1.0)) -> tuple[float, float, float]:
    if value is None:
        return default
    if all(hasattr(value, name) for name in ("x", "y", "z")):
        values = (value.x, value.y, value.z)
    else:
        try:
            values = tuple(value)[:3]
        except (TypeError, ValueError):
            return default
    try:
        return tuple(float(component) for component in values)
    except (TypeError, ValueError):
        return default


def _as_vec4(value, default=(1.0, 1.0, 1.0, 1.0)) -> tuple[float, float, float, float]:
    if value is None:
        return default
    if all(hasattr(value, name) for name in ("x", "y", "z")):
        values = (value.x, value.y, value.z, getattr(value, "w", 1.0))
    else:
        try:
            values = tuple(value)
        except (TypeError, ValueError):
            return default
    values = values[:4] + (1.0,) * max(0, 4 - len(values))
    try:
        return tuple(float(component) for component in values)
    except (TypeError, ValueError):
        return default


def _curve(environment, *suffixes):
    curves = getattr(environment, "curves", {}) or {}
    wanted = sorted(
        (str(value).replace("_", "").lower() for value in suffixes),
        key=len,
        reverse=True,
    )
    normalized_curves = [
        (str(path).replace("_", "").lower(), curve)
        for path, curve in curves.items()
    ]
    for suffix in wanted:
        for normalized, curve in normalized_curves:
            if normalized.endswith(suffix):
                return curve
    return None


def _curve_value(
    environment,
    seconds: float,
    suffixes,
    default,
    *,
    zero_scalar_is_unset: bool = False,
    zero_color_is_unset: bool = False,
):
    curve = _curve(environment, *suffixes)
    if curve is None:
        return default
    is_scalar = bool(getattr(curve, "is_scalar", False))
    points = tuple(getattr(curve, "points", ()) or ())
    unset_gate = zero_scalar_is_unset if is_scalar else zero_color_is_unset
    if unset_gate and points and _core_module().curve_is_placeholder(curve):
        # Empty graph fields may be stored as zeroed curves.
        return default
    try:
        return curve.evaluate((float(seconds) % 86400.0) / 86400.0)
    except Exception:
        return default


def _parameter_key(value: Any) -> str:
    text = str(value or "").lower()
    if text.startswith("m_"):
        text = text[2:]
    return text.replace("_", "")


def _parameter_value(environment, *path, default=None):
    current = getattr(environment, "params", {}) or {}
    missing = object()
    for component in path:
        if not isinstance(current, Mapping):
            return default
        wanted = _parameter_key(component)
        match = next(
            (value for name, value in current.items() if _parameter_key(name) == wanted),
            missing,
        )
        if match is missing:
            return default
        current = match
    return current


def _camera_light_values(environment, seconds: float, energy_scale: float):
    """Evaluate camera-following lights for character previews."""

    if environment is None or not bool(
        _parameter_value(environment, "cameraLightsSetup", "activated", default=False)
    ):
        return ()

    lights = []
    for index in range(2):
        light_name = f"gameplayLight{index}"
        if not bool(
            _parameter_value(
                environment,
                "cameraLightsSetup",
                light_name,
                "activated",
                default=False,
            )
        ):
            continue
        prefix = f"cameralightssetup.{light_name.lower()}"
        color_value = _curve_value(
            environment,
            seconds,
            (f"{prefix}.color",),
            (255.0, 255.0, 255.0, 1.0),
        )
        radiance = _curve_color(color_value, (255.0, 255.0, 255.0, 1.0))
        peak = max(1.0e-8, max(radiance))

        def scalar(field: str, default: float) -> float:
            value = _curve_value(
                environment,
                seconds,
                (f"{prefix}.{field.lower()}",),
                (0.0, 0.0, 0.0, default),
            )
            return float(_as_vec4(value, (0.0, 0.0, 0.0, default))[3])

        lights.append(
            {
                "name": f"Gameplay {index + 1}",
                "color": tuple(channel / peak for channel in radiance),
                "energy": peak * max(0.0, float(energy_scale)) * _BLENDER_CAMERA_LIGHT_ENERGY_SCALE,
                "radius": max(0.001, scalar("radius", 10.0)),
                "attenuation": max(0.0, min(1.0, scalar("attenuation", 0.5))),
                "offset_front": scalar("offsetFront", 0.0),
                "offset_right": scalar("offsetRight", 0.0),
                "offset_up": scalar("offsetUp", 0.0),
            }
        )
    return tuple(lights)


def _weather_blend(settings) -> float:
    index = int(getattr(settings, "weather_index", -1))
    if index < 0 or index >= len(settings.weather_presets):
        return 0.0
    return max(0.0, min(1.0, float(settings.weather_presets[index].environment_blend)))


def _blend_curve_value(
    base_environment,
    overlay_environment,
    blend,
    seconds,
    suffixes,
    default,
    *,
    zero_scalar_is_unset: bool = False,
    zero_color_is_unset: bool = False,
):
    base_value = _as_vec4(
        _curve_value(
            base_environment,
            seconds,
            suffixes,
            default,
            zero_scalar_is_unset=zero_scalar_is_unset,
            zero_color_is_unset=zero_color_is_unset,
        ),
        _as_vec4(default),
    )
    if overlay_environment is None or blend <= 0.0:
        return base_value
    overlay_value = _as_vec4(
        _curve_value(
            overlay_environment,
            seconds,
            suffixes,
            base_value,
            zero_scalar_is_unset=zero_scalar_is_unset,
            zero_color_is_unset=zero_color_is_unset,
        ),
        base_value,
    )
    return tuple(base + (overlay - base) * blend for base, overlay in zip(base_value, overlay_value))


def _curve_color(value, default) -> tuple[float, float, float]:
    rgba = _as_vec4(value, _as_vec4(default))
    scale = 255.0 if max(rgba[:3]) > 1.0 else 1.0
    intensity = max(0.0, rgba[3])
    return tuple((max(0.0, component / scale) ** 2.2) * intensity for component in rgba[:3])


def _linear_color(value, default) -> tuple[float, float, float]:
    """Convert a packed color to linear RGB while preserving intensity."""

    rgba = _as_vec4(value, _as_vec4(default))
    scale = 255.0 if max(rgba[:3]) > 1.0 else 1.0
    intensity = max(0.0, rgba[3])
    return tuple(max(0.0, component / scale) * intensity for component in rgba[:3])


def _euler_direction(yaw_degrees: float, pitch_degrees: float):
    """Return the forward direction for yaw and pitch angles."""

    yaw = math.radians(float(yaw_degrees))
    pitch = math.radians(float(pitch_degrees))
    cos_pitch = math.cos(pitch)
    return (
        -cos_pitch * math.sin(yaw),
        cos_pitch * math.cos(yaw),
        math.sin(pitch),
    )


def _normalised_lerp(first, second, factor: float):
    value = tuple(a + (b - a) * factor for a, b in zip(first, second))
    length = math.sqrt(sum(component * component for component in value))
    return tuple(component / length for component in value) if length > 1.0e-8 else second


def _world_material(world, role: str):
    skybox = getattr(world, "skybox", None)
    reference = getattr(skybox, f"{role}_material_ref", None)
    cr2w = getattr(world, "cr2w_file", None)
    chunks = getattr(cr2w, "CHUNKS", None)
    chunks = getattr(chunks, "CHUNKS", chunks)
    try:
        if reference is not None and chunks is not None:
            return chunks[int(reference)]
    except (IndexError, TypeError, ValueError):
        pass
    return str(getattr(skybox, f"{role}_material", "") or "") or None


def _preview_values(scene) -> dict[str, Any]:
    settings = _settings(scene)
    runtime = environment_runtime(scene)
    selector_active = str(
        getattr(settings, "preview_environment", "") or ENVIRONMENT_OFF_IDENTIFIER
    ) != ENVIRONMENT_OFF_IDENTIFIER
    direct_environment = selector_active or bool(runtime.get("direct_environment"))
    world = None if direct_environment else runtime.get("world")
    seconds = float(settings.fake_day_seconds)

    trajectory = getattr(world, "trajectory", None)
    if trajectory is not None:
        try:
            sun_direction = _as_xyz(trajectory.sun_direction(seconds))
            moon_direction = _as_xyz(trajectory.moon_direction(seconds), (0.0, 0.0, -1.0))
            key_direction = _as_xyz(trajectory.light_direction(seconds))
        except Exception:
            sun_direction = (0.0, 0.0, 1.0)
            moon_direction = (0.0, 0.0, -1.0)
            key_direction = sun_direction
        try:
            sky_day_factor = max(0.0, min(1.0, float(trajectory.sky_day_factor(seconds))))
        except Exception:
            sky_day_factor = 1.0
    else:
        sun_direction = (0.0, 0.0, 1.0)
        moon_direction = (0.0, 0.0, -1.0)
        key_direction = sun_direction
        sky_day_factor = 1.0

    base_environment = (
        runtime.get("selector_environment")
        if selector_active
        else runtime.get("environment") or runtime.get("scenes_environment")
    )
    overlay_environment = None if direct_environment else runtime.get("weather_environment")
    weather_blend = 0.0 if direct_environment else _weather_blend(settings)
    forced_direction_factor = max(
        0.0,
        min(
            1.0,
            float(
                _parameter_value(
                    base_environment,
                    "globalLight",
                    "activatedFactorLightDir",
                    default=0.0,
                )
                or 0.0
            ),
        ),
    )
    if forced_direction_factor > 0.0:
        def forced_direction(name: str):
            yaw = _curve_value(
                base_environment,
                seconds,
                (f"globallight.forced{name}diranglesyaw",),
                (0.0, 0.0, 0.0, 0.0),
            )[3]
            pitch = _curve_value(
                base_environment,
                seconds,
                (f"globallight.forced{name}diranglespitch",),
                (0.0, 0.0, 0.0, 0.0),
            )[3]
            return _euler_direction(yaw, pitch)

        key_direction = _normalised_lerp(
            key_direction,
            forced_direction("light"),
            forced_direction_factor,
        )
        sun_direction = _normalised_lerp(
            sun_direction,
            forced_direction("sun"),
            forced_direction_factor,
        )
        moon_direction = _normalised_lerp(
            moon_direction,
            forced_direction("moon"),
            forced_direction_factor,
        )
    sun_size = _blend_curve_value(
        base_environment,
        overlay_environment,
        weather_blend,
        seconds,
        ("sunandmoonparams.sunsize", "sunsize"),
        (1.0,) * 4,
        zero_scalar_is_unset=True,
    )[3]
    moon_size = _blend_curve_value(
        base_environment,
        overlay_environment,
        weather_blend,
        seconds,
        ("sunandmoonparams.moonsize", "moonsize"),
        (1.0,) * 4,
        zero_scalar_is_unset=True,
    )[3]
    light_color = _blend_curve_value(
        base_environment,
        overlay_environment,
        weather_blend,
        seconds,
        ("globallight.suncolor", "suncolor"),
        (255.0, 255.0, 255.0, 1.0),
        zero_color_is_unset=True,
    )
    sun_color_value = _blend_curve_value(
        base_environment,
        overlay_environment,
        weather_blend,
        seconds,
        ("sunandmoonparams.suncolor", "suncolor"),
        (255.0, 224.0, 170.0, 1.0),
        zero_color_is_unset=True,
    )
    moon_color_value = _blend_curve_value(
        base_environment,
        overlay_environment,
        weather_blend,
        seconds,
        ("sunandmoonparams.mooncolor", "mooncolor"),
        (170.0, 190.0, 255.0, 1.0),
        zero_color_is_unset=True,
    )
    sky_zenith_value = _blend_curve_value(
        base_environment,
        overlay_environment,
        weather_blend,
        seconds,
        ("sky.skycolor", "skycolor"),
        (108.0, 158.0, 230.0, 1.0),
        zero_color_is_unset=True,
    )
    sky_horizon_value = _blend_curve_value(
        base_environment,
        overlay_environment,
        weather_blend,
        seconds,
        ("sky.skycolorhorizon", "skycolorhorizon"),
        (230.0, 186.0, 158.0, 1.0),
        zero_color_is_unset=True,
    )
    sun_horizon_value = _blend_curve_value(
        base_environment,
        overlay_environment,
        weather_blend,
        seconds,
        ("sky.suncolorhorizon", "suncolorhorizon"),
        (235.0, 242.0, 255.0, 1.0),
        zero_color_is_unset=True,
    )
    sun_back_horizon_value = _blend_curve_value(
        base_environment,
        overlay_environment,
        weather_blend,
        seconds,
        ("sky.sunbackhorizoncolor", "sunbackhorizoncolor"),
        (161.0, 149.0, 147.0, 1.0),
        zero_color_is_unset=True,
    )
    sun_sky_value = _blend_curve_value(
        base_environment,
        overlay_environment,
        weather_blend,
        seconds,
        ("sky.suncolorsky", "suncolorsky"),
        (195.0, 227.0, 255.0, 1.0),
        zero_color_is_unset=True,
    )
    sun_sky_brightness = _blend_curve_value(
        base_environment,
        overlay_environment,
        weather_blend,
        seconds,
        ("sky.suncolorskybrightness", "suncolorskybrightness"),
        (0.0, 0.0, 0.0, 1.0),
        zero_scalar_is_unset=True,
    )[3]
    sun_area_sky_size = _blend_curve_value(
        base_environment,
        overlay_environment,
        weather_blend,
        seconds,
        ("sky.sunareaskysize", "sunareaskysize"),
        (0.0, 0.0, 0.0, 0.33),
        zero_scalar_is_unset=True,
    )[3]
    sun_influence = _blend_curve_value(
        base_environment,
        overlay_environment,
        weather_blend,
        seconds,
        ("sky.suninfluence", "suninfluence"),
        (0.0, 0.0, 0.0, 1.0),
        zero_scalar_is_unset=True,
    )[3]
    moon_sky_value = _blend_curve_value(
        base_environment,
        overlay_environment,
        weather_blend,
        seconds,
        ("sky.mooncolorsky", "mooncolorsky"),
        (195.0, 227.0, 255.0, 1.0),
        zero_color_is_unset=True,
    )
    moon_sky_brightness = _blend_curve_value(
        base_environment,
        overlay_environment,
        weather_blend,
        seconds,
        ("sky.mooncolorskybrightness", "mooncolorskybrightness"),
        (0.0, 0.0, 0.0, 1.0),
        zero_scalar_is_unset=True,
    )[3]
    moon_area_sky_size = _blend_curve_value(
        base_environment,
        overlay_environment,
        weather_blend,
        seconds,
        ("sky.moonareaskysize", "moonareaskysize"),
        (0.0, 0.0, 0.0, 0.33),
        zero_scalar_is_unset=True,
    )[3]
    moon_influence = _blend_curve_value(
        base_environment,
        overlay_environment,
        weather_blend,
        seconds,
        ("sky.mooninfluence", "mooninfluence"),
        (0.0, 0.0, 0.0, 0.0),
        zero_scalar_is_unset=True,
    )[3]
    fog_color_front_value = _blend_curve_value(
        base_environment,
        overlay_environment,
        weather_blend,
        seconds,
        ("globalfog.fogcolorfront", "fogcolorfront"),
        (48.0, 72.0, 88.0, 1.0),
        zero_color_is_unset=True,
    )
    fog_color_value = _blend_curve_value(
        base_environment,
        overlay_environment,
        weather_blend,
        seconds,
        ("globalfog.fogcolormiddle", "fogcolormiddle"),
        (48.0, 72.0, 88.0, 1.0),
        zero_color_is_unset=True,
    )
    fog_color_back_value = _blend_curve_value(
        base_environment,
        overlay_environment,
        weather_blend,
        seconds,
        ("globalfog.fogcolorback", "fogcolorback"),
        (48.0, 72.0, 88.0, 1.0),
        zero_color_is_unset=True,
    )
    aerial_color_front_value = _blend_curve_value(
        base_environment,
        overlay_environment,
        weather_blend,
        seconds,
        ("globalfog.aerialcolorfront", "aerialcolorfront"),
        (255.0, 255.0, 255.0, 1.0),
        zero_color_is_unset=True,
    )
    aerial_color_middle_value = _blend_curve_value(
        base_environment,
        overlay_environment,
        weather_blend,
        seconds,
        ("globalfog.aerialcolormiddle", "aerialcolormiddle"),
        (255.0, 255.0, 255.0, 1.0),
        zero_color_is_unset=True,
    )
    aerial_color_back_value = _blend_curve_value(
        base_environment,
        overlay_environment,
        weather_blend,
        seconds,
        ("globalfog.aerialcolorback", "aerialcolorback"),
        (255.0, 255.0, 255.0, 1.0),
        zero_color_is_unset=True,
    )
    fog_sky_density = _blend_curve_value(
        base_environment,
        overlay_environment,
        weather_blend,
        seconds,
        ("globalfog.fogskydensityscale", "fogskydensityscale"),
        (0.0, 0.0, 0.0, 0.0),
        zero_scalar_is_unset=True,
    )[3]
    fog_density = _blend_curve_value(
        base_environment,
        overlay_environment,
        weather_blend,
        seconds,
        ("globalfog.fogdensity", "fogdensity"),
        (0.0, 0.0, 0.0, 0.0),
        zero_scalar_is_unset=True,
    )[3]
    fog_dist_clamp = _blend_curve_value(
        base_environment,
        overlay_environment,
        weather_blend,
        seconds,
        ("globalfog.fogdistclamp", "fogdistclamp"),
        (0.0, 0.0, 0.0, 0.0),
        zero_scalar_is_unset=True,
    )[3]
    fog_appear_distance = _blend_curve_value(
        base_environment,
        overlay_environment,
        weather_blend,
        seconds,
        ("globalfog.fogappeardistance", "fogappeardistance"),
        (0.0, 0.0, 0.0, 0.0),
        zero_scalar_is_unset=True,
    )[3]
    fog_appear_range = _blend_curve_value(
        base_environment,
        overlay_environment,
        weather_blend,
        seconds,
        ("globalfog.fogappearrange", "fogappearrange"),
        (0.0, 0.0, 0.0, 0.0),
        zero_scalar_is_unset=True,
    )[3]
    fog_final_exp = _blend_curve_value(
        base_environment,
        overlay_environment,
        weather_blend,
        seconds,
        ("globalfog.fogfinalexp", "fogfinalexp"),
        (0.0, 0.0, 0.0, 1.0),
        zero_scalar_is_unset=True,
    )[3]
    aerial_final_exp = _blend_curve_value(
        base_environment,
        overlay_environment,
        weather_blend,
        seconds,
        ("globalfog.aerialfinalexp", "aerialfinalexp"),
        (0.0, 0.0, 0.0, 1.0),
        zero_scalar_is_unset=True,
    )[3]
    fog_vert_offset = _blend_curve_value(
        base_environment,
        overlay_environment,
        weather_blend,
        seconds,
        ("globalfog.fogvertoffset", "fogvertoffset"),
        (0.0, 0.0, 0.0, 0.0),
        zero_scalar_is_unset=True,
    )[3]
    fog_vert_density = _blend_curve_value(
        base_environment,
        overlay_environment,
        weather_blend,
        seconds,
        ("globalfog.fogvertdensity", "fogvertdensity"),
        (0.0, 0.0, 0.0, 0.0),
        zero_scalar_is_unset=True,
    )[3]
    water_color_value = _blend_curve_value(
        base_environment,
        overlay_environment,
        weather_blend,
        seconds,
        ("water.watercolor", "watercolor"),
        (0.0, 0.0, 0.0, 1.0),
    )
    water_fresnel = _blend_curve_value(
        base_environment,
        overlay_environment,
        weather_blend,
        seconds,
        ("water.waterfresnel", "waterfresnel"),
        (0.0, 0.0, 0.0, 1.0),
        zero_scalar_is_unset=True,
    )[3]
    water_ambient_scale = _blend_curve_value(
        base_environment,
        overlay_environment,
        weather_blend,
        seconds,
        ("water.waterambientscale", "waterambientscale"),
        (0.0, 0.0, 0.0, 0.1),
        zero_scalar_is_unset=True,
    )[3]
    water_diffuse_scale = _blend_curve_value(
        base_environment,
        overlay_environment,
        weather_blend,
        seconds,
        ("water.waterdiffusescale", "waterdiffusescale"),
        (0.0, 0.0, 0.0, 0.4),
        zero_scalar_is_unset=True,
    )[3]
    water_flow_intensity = _blend_curve_value(
        base_environment,
        overlay_environment,
        weather_blend,
        seconds,
        ("water.waterflowintensity", "waterflowintensity"),
        (0.0, 0.0, 0.0, 0.6),
        zero_scalar_is_unset=True,
    )[3]
    water_foam_intensity = _blend_curve_value(
        base_environment,
        overlay_environment,
        weather_blend,
        seconds,
        ("water.waterfoamintensity", "waterfoamintensity"),
        (0.0, 0.0, 0.0, 0.0),
        zero_scalar_is_unset=True,
    )[3]
    ambient_sky_top_value = _blend_curve_value(
        base_environment,
        overlay_environment,
        weather_blend,
        seconds,
        (
            "globallight.envprobebaselightingambient.colorskytop",
            "envprobebaselightingambient.colorskytop",
        ),
        (204.0, 224.0, 255.0, 1.0),
        zero_color_is_unset=True,
    )
    ambient_sky_horizon_value = _blend_curve_value(
        base_environment,
        overlay_environment,
        weather_blend,
        seconds,
        (
            "globallight.envprobebaselightingambient.colorskyhorizon",
            "envprobebaselightingambient.colorskyhorizon",
        ),
        (204.0, 224.0, 255.0, 1.0),
        zero_color_is_unset=True,
    )
    ambient_light_scale = _blend_curve_value(
        base_environment,
        overlay_environment,
        weather_blend,
        seconds,
        ("globallight.envprobeambientscalelight", "envprobeambientscalelight"),
        (0.0, 0.0, 0.0, 1.0),
        zero_scalar_is_unset=True,
    )[3]
    reflection_sky_add_value = _blend_curve_value(
        base_environment,
        overlay_environment,
        weather_blend,
        seconds,
        (
            "globallight.envprobebaselightingreflection.colorskyadd",
            "envprobebaselightingreflection.colorskyadd",
        ),
        (255.0, 255.0, 255.0, 0.0),
        zero_color_is_unset=True,
    )
    character_reflection_shadow = _blend_curve_value(
        base_environment,
        overlay_environment,
        weather_blend,
        seconds,
        (
            "globallight.characterslightingboostreflectionshadow",
            "characterslightingboostreflectionshadow",
        ),
        (0.0, 0.0, 0.0, 1.0),
        zero_scalar_is_unset=True,
    )[3]
    probe_reflection_shadow = _blend_curve_value(
        base_environment,
        overlay_environment,
        weather_blend,
        seconds,
        (
            "globallight.envprobereflectionscaleshadow",
            "envprobereflectionscaleshadow",
        ),
        (0.0, 0.0, 0.0, 1.0),
        zero_scalar_is_unset=True,
    )[3]
    tone_exposure_scale = _blend_curve_value(
        base_environment,
        overlay_environment,
        weather_blend,
        seconds,
        ("tonemapping.exposurescale", "exposurescale"),
        (0.0, 0.0, 0.0, 1.0),
        zero_scalar_is_unset=True,
    )[3]
    tone_luminance_limit_shape = _blend_curve_value(
        base_environment,
        overlay_environment,
        weather_blend,
        seconds,
        ("tonemapping.luminancelimitshape", "luminancelimitshape"),
        (0.0, 0.0, 0.0, 1.0),
        zero_scalar_is_unset=True,
    )[3]
    tone_luminance_limit_min = _blend_curve_value(
        base_environment,
        overlay_environment,
        weather_blend,
        seconds,
        ("tonemapping.luminancelimitmin", "luminancelimitmin"),
        (0.0, 0.0, 0.0, 1.0),
        zero_scalar_is_unset=True,
    )[3]
    tone_luminance_limit_max = _blend_curve_value(
        base_environment,
        overlay_environment,
        weather_blend,
        seconds,
        ("tonemapping.luminancelimitmax", "luminancelimitmax"),
        (0.0, 0.0, 0.0, 2.0),
        zero_scalar_is_unset=True,
    )[3]
    tone_curve_parameters = tuple(
        _blend_curve_value(
            base_environment,
            overlay_environment,
            weather_blend,
            seconds,
            (f"tonemapping.newtonemapcurveparameters.{name}", name),
            (0.0, 0.0, 0.0, default),
            zero_scalar_is_unset=True,
        )[3]
        for name, default in (
            ("shoulderstrength", 0.22),
            ("linearstrength", 0.30),
            ("linearangle", 0.10),
            ("toestrength", 0.20),
            ("toenumerator", 0.01),
            ("toedenominator", 0.30),
        )
    )
    tone_post_scale = _blend_curve_value(
        base_environment,
        overlay_environment,
        weather_blend,
        seconds,
        ("tonemapping.postscale", "postscale"),
        (0.0, 0.0, 0.0, 1.0),
        zero_scalar_is_unset=True,
    )[3]
    # CEdPreviewPanel runs the tone curve at a fixed 0.5 key; postScale comes after that S-curve and is not exposure.
    tone_scale = max(float(tone_exposure_scale), 1.0e-6)
    tone_shape_range = 11.2 * tone_scale
    tone_limit_min = float(tone_luminance_limit_min)
    tone_limit_max = float(tone_luminance_limit_max)
    tone_key = 0.5
    tone_key = min(max(tone_key, tone_limit_min), tone_limit_max)
    tone_shaped_key = (
        max(tone_key, 1.0e-4) / tone_shape_range
    ) ** float(tone_luminance_limit_shape) * tone_shape_range
    tone_multiplier = tone_scale / max(tone_shaped_key, 1.0e-6)
    tone_exposure_ev = math.log2(tone_multiplier) if tone_multiplier > 0.0 else 0.0
    balance_map_enabled = bool(
        _parameter_value(
            base_environment,
            "finalColorBalance",
            "activatedBalanceMap",
            default=False,
        )
    )
    balance_map_path = str(
        _parameter_value(
            base_environment,
            "finalColorBalance",
            "balanceMap0",
            default="",
        )
        or ""
    )
    balance_map_amount = _curve_value(
        base_environment,
        seconds,
        ("finalcolorbalance.balancemapamount",),
        (0.0, 0.0, 0.0, 1.0),
    )[3]
    balance_post_brightness = _curve_value(
        base_environment,
        seconds,
        ("finalcolorbalance.balancepostbrightness",),
        (0.0, 0.0, 0.0, 1.0),
    )[3]
    full_effects = bool(getattr(settings, "full_effects", False))
    sky_brightness = _blend_curve_value(
        base_environment,
        overlay_environment,
        weather_blend,
        seconds,
        ("sky.globalskybrightness", "globalskybrightness"),
        (0.0, 0.0, 0.0, 1.0),
        zero_scalar_is_unset=True,
    )[3]
    horizon_attenuation = _blend_curve_value(
        base_environment,
        overlay_environment,
        weather_blend,
        seconds,
        ("sky.horizonverticalattenuation", "horizonverticalattenuation"),
        (0.0, 0.0, 0.0, 1.8),
    )[3]
    color_scale = 255.0 if max(light_color[:3]) > 1.0 else 1.0
    key_color = tuple(max(0.0, component / color_scale) ** 2.2 for component in light_color[:3])
    key_energy = (
        max(0.0, float(light_color[3]))
        * float(settings.key_light_energy)
        * _BLENDER_KEY_LIGHT_ENERGY_SCALE
    )
    ambient_top = _curve_color(ambient_sky_top_value, (0.61, 0.75, 1.0, 1.0))
    ambient_horizon = _curve_color(ambient_sky_horizon_value, (0.61, 0.75, 1.0, 1.0))
    ambient_radiance = tuple(
        (top + horizon) * 0.5
        for top, horizon in zip(ambient_top, ambient_horizon)
    )
    ambient_peak = max(1.0e-8, max(ambient_radiance))
    ambient_color = tuple(channel / ambient_peak for channel in ambient_radiance)
    ambient_energy = (
        ambient_peak
        # The four unshadowed preview lights approximate diffuse sky fill.
        * max(0.0, float(ambient_light_scale))
        * float(getattr(settings, "ambient_light_energy", 1.0))
        * _BLENDER_AMBIENT_FILL_ENERGY_SCALE
    )
    if direct_environment and ambient_energy <= 1.0e-8:
        reflection_radiance = _curve_color(
            reflection_sky_add_value,
            (255.0, 255.0, 255.0, 0.0),
        )
        reflection_peak = max(reflection_radiance)
        if reflection_peak > 1.0e-8:
            ambient_color = tuple(channel / reflection_peak for channel in reflection_radiance)
            # a standalone .env has no captured probe atlas
            ambient_energy = (
                key_energy
                * reflection_peak
                * max(0.0, float(character_reflection_shadow))
                * max(0.0, float(probe_reflection_shadow))
                * float(getattr(settings, "ambient_light_energy", 1.0))
                * _BLENDER_REFLECTION_FILL_ENERGY_SCALE
            )
    # Direct environment previews omit camera-following lights.
    camera_lights = (
        _camera_light_values(
            base_environment,
            seconds,
            float(settings.key_light_energy),
        )
        if not direct_environment
        else ()
    )

    weather_effects = ()
    weather_table = runtime.get("weather")
    if not direct_environment and weather_table is not None and settings.active_weather_name:
        preset = weather_table.preset(settings.active_weather_name)
        weather_effects = tuple(
            {
                "path": str(getattr(effect, "path", "") or ""),
                "strength": float(getattr(effect, "strength", 0.0) or 0.0),
                "effect_type": str(getattr(effect, "effect_type", "CLOUDS") or "CLOUDS"),
            }
            for effect in (getattr(preset, "effects", ()) or ())
        )

    skybox = getattr(world, "skybox", None)
    preview_source_path = str(
        getattr(world, "source_path", "")
        or getattr(base_environment, "source_path", "")
        or settings.environment_path
        or settings.world_path
        or ""
    )
    return {
        "source_path": preview_source_path,
        "sun_mesh_path": str(getattr(skybox, "sun_mesh", "") or ""),
        "moon_mesh_path": str(getattr(skybox, "moon_mesh", "") or ""),
        "sun_material": _world_material(world, "sun"),
        "moon_material": _world_material(world, "moon"),
        "skybox_material_path": str(getattr(skybox, "skybox_material", "") or ""),
        "moon_material_path": str(getattr(skybox, "moon_material", "") or ""),
        "weather_effects": weather_effects,
        "sun_direction": sun_direction,
        "moon_direction": moon_direction,
        "key_direction": key_direction,
        "sun_size": max(0.001, sun_size),
        # A one-key size curve is still an authored value.
        "moon_size": max(0.001, moon_size * float(settings.moon_size_scale)),
        "sun_color": _curve_color(sun_color_value, (1.0, 0.75, 0.35, 1.0)),
        "moon_color": _curve_color(moon_color_value, (0.55, 0.65, 1.0, 1.0)),
        "key_color": key_color,
        "key_energy": key_energy,
        "ambient_color": ambient_color,
        "ambient_energy": ambient_energy,
        "camera_lights": camera_lights,
        "tone_exposure_ev": tone_exposure_ev,
        "tone_curve_parameters": tone_curve_parameters,
        "tone_post_scale": max(0.0, float(tone_post_scale)),
        "balance_map_path": balance_map_path if balance_map_enabled and full_effects else "",
        "balance_map_amount": max(0.0, min(1.0, float(balance_map_amount))),
        "balance_post_brightness": max(0.0, float(balance_post_brightness)),
        "sky_zenith_color": _curve_color(sky_zenith_value, (0.15, 0.35, 0.8, 1.0)),
        "sky_horizon_color": _curve_color(sky_horizon_value, (0.8, 0.5, 0.35, 1.0)),
        "sun_horizon_color": _curve_color(sun_horizon_value, (0.85, 0.9, 1.0, 1.0)),
        "sun_back_horizon_color": _curve_color(
            sun_back_horizon_value,
            (0.364, 0.307, 0.298, 1.0),
        ),
        "sun_sky_color": _curve_color(sun_sky_value, (0.55, 0.78, 1.0, 1.0)),
        "sun_sky_brightness": max(0.0, float(sun_sky_brightness)),
        "sun_area_sky_size": max(0.0001, float(sun_area_sky_size)),
        "sun_influence": max(0.0, min(1.0, float(sun_influence))),
        "moon_sky_color": _curve_color(moon_sky_value, (0.55, 0.78, 1.0, 1.0)),
        "moon_sky_brightness": max(0.0, float(moon_sky_brightness)),
        "moon_area_sky_size": max(0.0001, float(moon_area_sky_size)),
        "moon_influence": max(0.0, min(1.0, float(moon_influence))),
        "sky_brightness": (
            max(0.0, float(sky_brightness)) * _BLENDER_SKY_BRIGHTNESS_SCALE
        ),
        "fog_color": _curve_color(fog_color_value, (0.08, 0.30, 0.42, 1.0)),
        "fog_color_front": _curve_color(fog_color_front_value, (0.08, 0.30, 0.42, 1.0)),
        "fog_color_back": _curve_color(fog_color_back_value, (0.08, 0.30, 0.42, 1.0)),
        "aerial_color_front": _curve_color(
            aerial_color_front_value,
            (255.0, 255.0, 255.0, 1.0),
        ),
        "aerial_color_middle": _curve_color(
            aerial_color_middle_value,
            (255.0, 255.0, 255.0, 1.0),
        ),
        "aerial_color_back": _curve_color(
            aerial_color_back_value,
            (255.0, 255.0, 255.0, 1.0),
        ),
        "fog_sky_density": max(0.0, float(fog_sky_density)),
        "fog_density": max(0.0, float(fog_density)) if full_effects else 0.0,
        "fog_dist_clamp": max(0.0, float(fog_dist_clamp)),
        "fog_appear_distance": max(0.0, float(fog_appear_distance)),
        "fog_appear_range": max(0.0, float(fog_appear_range)),
        "fog_final_exp": max(0.05, float(fog_final_exp)),
        "aerial_final_exp": max(0.0, float(aerial_final_exp)),
        "fog_vert_offset": float(fog_vert_offset),
        "fog_vert_density": float(fog_vert_density),
        # Water color is stored linearly not gamma-decoded.
        "water_color": _linear_color(water_color_value, (0.0, 0.0, 0.0, 1.0)),
        "water_fresnel": max(0.0, float(water_fresnel)),
        "water_ambient_scale": max(0.001, float(water_ambient_scale)),
        "water_diffuse_scale": max(0.001, float(water_diffuse_scale)),
        "water_flow_intensity": max(0.0, float(water_flow_intensity)),
        # Preserve negative values for the material's offset and clamp.
        "water_foam_intensity": float(water_foam_intensity),
        "sky_day_factor": sky_day_factor,
        "horizon_attenuation": max(0.1, float(horizon_attenuation)),
        "stars_brightness": float(settings.stars_brightness),
        "cloud_amount": float(getattr(settings, "cloud_amount", 0.45)),
        "sun_brightness": float(settings.sun_brightness),
        "moon_brightness": float(settings.moon_brightness),
        "sky_enabled": bool(settings.sky_enabled),
        "anchor_distance": float(settings.anchor_distance),
        "time_seconds": seconds,
        "day_number": float(settings.fake_day_number),
        "import_materials": bool(settings.import_materials),
    }


def _refresh_preview(context, *, ensure: bool = False, quiet: bool = False):
    scene = context.scene
    settings = _settings(scene)
    if settings is None or not settings.preview_enabled:
        return None
    values = _preview_values(scene)
    preview = _preview_module()
    if ensure:
        result = preview.ensure_preview(context, **values)
    else:
        result = preview.update_preview(context, **values)
    if not quiet:
        warnings = tuple(getattr(result, "warnings", ()) or ())
        if warnings:
            details = [str(message) for message in warnings]
            stars_path = str(getattr(result, "stars_resolved_path", "") or "")
            if stars_path:
                details.append(f"Stars: {stars_path}")
            _set_status(
                settings,
                "Preview has warnings",
                details="\n".join(details),
                level="WARNING",
            )
        else:
            details = ["Environment preview updated."]
            for label, attribute in (
                ("Sun", "sun_resolved_path"),
                ("Moon", "moon_resolved_path"),
                ("Stars", "stars_resolved_path"),
                ("Cloud", "cloud_resolved_path"),
            ):
                resolved = str(getattr(result, attribute, "") or "")
                if resolved:
                    details.append(f"{label}: {resolved}")
            _set_status(settings, "Preview updated", details="\n".join(details), level="OK")
    return result


def _on_daycycle_changed(settings, context) -> None:
    if context is None or not settings.preview_enabled:
        return
    try:
        _ensure_loaded(context)
        _refresh_preview(context, quiet=True)
    except Exception as exc:
        _set_status(settings, "Preview update failed", details=str(exc), level="WARNING")


def _on_preview_setting_changed(settings, context) -> None:
    if context is None or not settings.preview_enabled:
        return
    try:
        _ensure_loaded(context)
        _refresh_preview(context, quiet=True)
    except Exception as exc:
        _set_status(settings, "Preview update failed", details=str(exc), level="WARNING")


def _get_fake_hour(settings) -> float:
    return float(settings.fake_day_seconds) / 3600.0


def _set_fake_hour(settings, value) -> None:
    settings.fake_day_seconds = (float(value) % 24.0) * 3600.0


def _on_preview_enabled(settings, context) -> None:
    if context is None:
        return
    try:
        if settings.preview_enabled:
            _ensure_loaded(context)
            _refresh_preview(context, ensure=True, quiet=True)
        else:
            _preview_module().clear_preview(context)
    except Exception as exc:
        _set_status(settings, "Preview unavailable", details=str(exc), level="WARNING")


def _preset_sequence(table):
    values = getattr(table, "presets", table)
    return list(values or [])


def _populate_weather_items(settings, table) -> None:
    runtime = environment_runtime(settings.id_data)
    runtime["populating_weather"] = True
    try:
        settings.weather_presets.clear()
        for preset in _preset_sequence(table):
            item = settings.weather_presets.add()
            item.name = str(getattr(preset, "name", "") or "Unnamed")
            item.environment_path = str(getattr(preset, "environment_path", "") or "")
            item.probability = float(getattr(preset, "probability", 0.0) or 0.0)
            item.wind_scale = float(getattr(preset, "wind_scale", 0.0) or 0.0)
            item.blend_time = float(getattr(preset, "blend_time", 0.0) or 0.0)
            item.environment_blend = float(getattr(preset, "environment_blend", 0.0) or 0.0)
            item.occurrence_time = float(getattr(preset, "occurrence_time", 0.0) or 0.0)
            item.skybox = str(getattr(preset, "skybox", "") or "")
            item.effect_count = len(getattr(preset, "effects", ()) or ())
        settings.weather_index = 0 if settings.weather_presets else -1
    finally:
        runtime["populating_weather"] = False
    if settings.weather_presets:
        _select_weather(settings.id_data, settings.weather_index, refresh=False)
    else:
        settings.active_weather_name = ""


def _load_environment(scene, path: str, *, slot: str):
    settings = _settings(scene)
    resolved = _absolute_source(path, settings)
    if not resolved or not Path(resolved).is_file():
        raise FileNotFoundError(f"Environment definition not found: {path}")
    environment = _core_module().load_environment_definition(resolved)
    environment_runtime(scene)[slot] = environment
    _sync_environment_fields(scene)
    return environment, resolved


def _environment_details(resolved: str) -> str:
    return (
        f"Environment: {resolved}\n\n"
        "Preview: sky and horizon color, sky brightness, stars, visible sun and moon, "
        "and the global key light. Other environment fields remain loaded but are not previewed."
    )


def _load_weather(scene, path: str):
    settings = _settings(scene)
    resolved = _absolute_source(path, settings)
    if not resolved or not Path(resolved).is_file():
        raise FileNotFoundError(f"Weather table not found: {path}")
    table = _core_module().load_weather_table(resolved)
    environment_runtime(scene)["weather"] = table
    _populate_weather_items(settings, table)
    return table, resolved


def _weather_details(table, resolved: str) -> str:
    details = (
        f"Weather: {resolved}\n\n"
        "Preview: environment overlay and mesh-backed cloud weather effects."
    )
    warnings = tuple(getattr(table, "warnings", ()) or ())
    if warnings:
        details += "\n\nWarnings:\n" + "\n".join(str(value) for value in warnings)
    return details


def _load_world(scene, path: str, *, load_references: bool = True):
    settings = _settings(scene)
    resolved = _absolute_source(path, settings)
    if not resolved or not Path(resolved).is_file():
        raise FileNotFoundError(f"World not found: {path}")
    world = _core_module().load_world_environment(resolved)
    warnings = _store_world(scene, world, resolved, load_references=load_references)
    return world, resolved, warnings


def _store_world(scene, world, source_path: str, *, load_references: bool = True):
    settings = _settings(scene)
    runtime = environment_runtime(scene)
    runtime.clear()
    runtime["populating_weather"] = True
    try:
        settings.weather_presets.clear()
        settings.weather_index = -1
        settings.active_weather_name = ""
    finally:
        runtime["populating_weather"] = False
    runtime["world"] = world
    settings.world_path = str(getattr(world, "source_path", "") or source_path)
    settings.environment_path = str(getattr(world, "environment_definition", "") or "")
    settings.scenes_environment_path = str(getattr(world, "scenes_environment_definition", "") or "")
    settings.weather_path = str(getattr(world, "weather_template", "") or "")

    warnings = []
    if load_references and settings.environment_path:
        try:
            _load_environment(scene, settings.environment_path, slot="environment")
        except Exception as exc:
            warnings.append(str(exc))
    if load_references and settings.scenes_environment_path:
        try:
            _load_environment(scene, settings.scenes_environment_path, slot="scenes_environment")
        except Exception as exc:
            warnings.append(str(exc))
    if load_references and settings.weather_path:
        try:
            _load_weather(scene, settings.weather_path)
        except Exception as exc:
            warnings.append(str(exc))
    _sync_environment_fields(scene)
    return warnings


def sync_world_import(context, world_file, file_path: str) -> EnvironmentUIResult:
    """Populate the panel from a normal ``.w2w`` import.

    ``WORLD.environment`` is preferred because the world importer may already
    have decoded it.  Older parsed WORLD objects fall back to the source path.
    """

    scene = context.scene
    settings = _settings(scene)
    try:
        world = getattr(world_file, "environment", None)
        if world is not None and hasattr(world, "environment_definition"):
            resolved = str(getattr(world, "source_path", "") or file_path or "")
            warnings = _store_world(scene, world, resolved, load_references=True)
        else:
            world, resolved, warnings = _load_world(scene, file_path, load_references=True)
        details = f"World: {resolved}"
        if warnings:
            details += "\n\nReferenced sources:\n" + "\n".join(warnings)
        _set_status(
            settings,
            "World environment ready" if not warnings else "Environment has warnings",
            details=details,
            level="OK" if not warnings else "WARNING",
        )
        return EnvironmentUIResult(True, settings.status_text, world, resolved)
    except Exception as exc:
        _set_status(settings, "World environment failed", details=str(exc), level="ERROR")
        return EnvironmentUIResult(False, str(exc), None, str(file_path or ""))


def load_environment_path(context, path: str) -> EnvironmentUIResult:
    """Load an Asset Browser ``.env`` path into the base definition slot."""

    settings = _settings(context.scene)
    settings.environment_path = str(path or "")
    try:
        environment, resolved = _load_environment(context.scene, path, slot="environment")
        environment_runtime(context.scene)["direct_environment"] = True
        _set_status(
            settings,
            "Base environment loaded",
            details=_environment_details(resolved),
            level="OK",
        )
        if settings.preview_enabled:
            _refresh_preview(context, ensure=True, quiet=True)
        return EnvironmentUIResult(True, settings.status_text, environment, resolved)
    except Exception as exc:
        _set_status(settings, "Environment load failed", details=str(exc), level="ERROR")
        return EnvironmentUIResult(False, str(exc), None, str(path or ""))


def load_weather_path(context, path: str) -> EnvironmentUIResult:
    """Load an Asset Browser weather ``.csv`` path and populate its presets."""

    settings = _settings(context.scene)
    settings.weather_path = str(path or "")
    try:
        table, resolved = _load_weather(context.scene, path)
        count = len(_preset_sequence(table))
        message = f"{count} weather presets"
        warnings = tuple(getattr(table, "warnings", ()) or ())
        _set_status(
            settings,
            message if not warnings else f"{count} presets with warnings",
            details=_weather_details(table, resolved),
            level="OK" if not warnings else "WARNING",
        )
        if settings.preview_enabled:
            _refresh_preview(context, ensure=True, quiet=True)
        return EnvironmentUIResult(True, message, table, resolved)
    except Exception as exc:
        _set_status(settings, "Weather load failed", details=str(exc), level="ERROR")
        return EnvironmentUIResult(False, str(exc), None, str(path or ""))


def _ensure_loaded(context) -> dict[str, Any]:
    scene = context.scene
    settings = _settings(scene)
    runtime = environment_runtime(scene)
    selector_identifier = str(
        getattr(settings, "preview_environment", "") or ENVIRONMENT_OFF_IDENTIFIER
    )
    if selector_identifier != ENVIRONMENT_OFF_IDENTIFIER:
        if runtime.get("selector_environment") is None:
            _rebuild_environment_selector_cache(context)
            item = _ENVIRONMENT_SELECTOR_LOOKUP.get(selector_identifier)
            if item is None:
                raise FileNotFoundError("Selected preview environment is no longer available")
            _load_environment(scene, item.depot_path, slot="selector_environment")
        return runtime
    if runtime.get("direct_environment"):
        if runtime.get("environment") is None and settings.environment_path:
            _load_environment(scene, settings.environment_path, slot="environment")
        return runtime
    if runtime.get("world") is None and settings.world_path:
        selected_weather = str(settings.active_weather_name or "")
        _load_world(scene, settings.world_path)
        if selected_weather:
            restored_index = next(
                (
                    index
                    for index, preset in enumerate(settings.weather_presets)
                    if preset.name == selected_weather
                ),
                -1,
            )
            if restored_index >= 0 and settings.weather_index != restored_index:
                settings.weather_index = restored_index
    elif runtime.get("environment") is None and settings.environment_path:
        _load_environment(scene, settings.environment_path, slot="environment")
    return runtime


def _select_weather(scene, index: int, *, refresh: bool = True) -> None:
    settings = _settings(scene)
    runtime = environment_runtime(scene)
    if runtime.get("populating_weather"):
        return
    if index < 0 or index >= len(settings.weather_presets):
        settings.active_weather_name = ""
        runtime.pop("weather_environment", None)
        _sync_environment_fields(scene)
        return

    item = settings.weather_presets[index]
    settings.active_weather_name = item.name
    runtime.pop("weather_environment", None)
    if item.environment_path:
        try:
            _load_environment(scene, item.environment_path, slot="weather_environment")
        except Exception as exc:
            _set_status(settings, "Weather overlay missing", details=str(exc), level="WARNING")
    else:
        _sync_environment_fields(scene)
    if refresh and settings.preview_enabled:
        try:
            _refresh_preview(bpy.context, quiet=True)
        except Exception as exc:
            _set_status(settings, "Weather preview failed", details=str(exc), level="WARNING")


def _on_weather_index_changed(settings, context) -> None:
    try:
        if context is not None:
            runtime = environment_runtime(context.scene)
            if runtime.get("world") is None and settings.world_path:
                resolved = _absolute_source(settings.world_path, settings)
                runtime["world"] = _core_module().load_world_environment(resolved)
            if runtime.get("environment") is None and settings.environment_path:
                _load_environment(context.scene, settings.environment_path, slot="environment")
        _select_weather(settings.id_data, int(settings.weather_index))
    except Exception as exc:
        _set_status(settings, "Weather selection failed", details=str(exc), level="WARNING")


_FIELDS_SLOTS = {
    "BASE": "environment",
    "SCENES": "scenes_environment",
    "WEATHER": "weather_environment",
}


def _sync_environment_fields(scene) -> None:
    settings = _settings(scene)
    if settings is None:
        return
    settings.definition_fields.clear()
    runtime = environment_runtime(scene, create=False) or {}
    environment = runtime.get(_FIELDS_SLOTS.get(settings.fields_source, "environment"))
    settings.definition_fields_path = str(getattr(environment, "source_path", "") or "")
    if environment is None:
        return
    for row in _core_module().describe_environment_fields(environment):
        item = settings.definition_fields.add()
        item.class_name = row.group
        item.field_name = row.field
        item.type_text = row.type_text
        item.value_text = row.value_text
        item.is_set = row.is_set
        for child in row.children:
            entry = item.children.add()
            entry.parent_key = child.parent_key
            entry.item_key = child.item_key
            entry.label = child.label
            entry.type_text = child.type_text
            entry.value_text = child.value_text
            entry.depth = child.depth
            entry.has_children = child.has_children
        item.has_children = len(item.children) > 0


def _environment_selector_roots(context) -> tuple[str, ...]:
    try:
        from .. import get_all_addon_prefs

        prefs = get_all_addon_prefs(context)
    except Exception:
        prefs = None

    roots = []
    seen = set()
    for attr in ("redkit_depot_path", "redkit_uncooked_path", "uncook_path"):
        value = str(getattr(prefs, attr, "") or "").strip() if prefs is not None else ""
        if not value:
            continue
        expanded = bpy.path.abspath(value)
        key = os.path.normcase(os.path.normpath(expanded))
        if key in seen:
            continue
        seen.add(key)
        roots.append(os.path.normpath(expanded))
    return tuple(roots)


def _environment_enum_number(identifier: str, used: set[int]) -> int:
    try:
        number = int(identifier.removeprefix("ENV_")[:8], 16) & 0x7FFFFFFF
    except (TypeError, ValueError):
        number = 1
    number = number or 1
    while number in used:
        number = 1 if number == 0x7FFFFFFF else number + 1
    used.add(number)
    return number


def _rebuild_environment_selector_cache(context, *, force: bool = False):
    global _ENVIRONMENT_SELECTOR_CACHE_KEY
    global _ENVIRONMENT_SELECTOR_CATALOG
    global _ENVIRONMENT_SELECTOR_LOOKUP
    global _ENVIRONMENT_SELECTOR_ENUM_ITEMS

    roots = _environment_selector_roots(context)
    cache_key = tuple(os.path.normcase(os.path.normpath(root)) for root in roots)
    if not force and cache_key == _ENVIRONMENT_SELECTOR_CACHE_KEY:
        return _ENVIRONMENT_SELECTOR_ENUM_ITEMS

    catalog = scan_environment_definitions(roots)
    used_numbers = {0}
    enum_items = [
        (
            ENVIRONMENT_OFF_IDENTIFIER,
            "ENV - OFF",
            "Disable the Entity Editor preview environment",
            "WORLD_DATA",
            0,
        )
    ]
    for item in catalog:
        enum_items.append(
            (
                item.identifier,
                item.label,
                f"Load {item.depot_path}",
                "WORLD_DATA",
                _environment_enum_number(item.identifier, used_numbers),
            )
        )

    # Blender's dynamic EnumProperty keeps references to the strings returned by its callback.
    _ENVIRONMENT_SELECTOR_CACHE_KEY = cache_key
    _ENVIRONMENT_SELECTOR_CATALOG = catalog
    _ENVIRONMENT_SELECTOR_LOOKUP = {item.identifier: item for item in catalog}
    _ENVIRONMENT_SELECTOR_ENUM_ITEMS = tuple(enum_items)
    return _ENVIRONMENT_SELECTOR_ENUM_ITEMS


def _enum_preview_environment_items(_owner, context):
    return _rebuild_environment_selector_cache(context)


def _disable_preview_environment_selector(settings, context) -> None:
    runtime = environment_runtime(context.scene)
    runtime.pop("selector_environment", None)
    settings.preview_environment = ENVIRONMENT_OFF_IDENTIFIER
    _sync_environment_fields(context.scene)
    if settings.preview_enabled:
        settings.preview_enabled = False
    else:
        _preview_module().clear_preview(context)
    _set_status(
        settings,
        "Preview environment off",
        details="Entity Editor environment selector: ENV - OFF",
        level="OK",
    )


def _apply_preview_environment_identifier(settings, context, identifier: str) -> EnvironmentUIResult:
    if context is None or getattr(context, "scene", None) is None:
        return EnvironmentUIResult(False, "No active scene")
    identifier = str(identifier or ENVIRONMENT_OFF_IDENTIFIER)
    if not identifier or identifier == ENVIRONMENT_OFF_IDENTIFIER:
        _disable_preview_environment_selector(settings, context)
        return EnvironmentUIResult(True, "Preview environment off")

    _rebuild_environment_selector_cache(context)
    item = _ENVIRONMENT_SELECTOR_LOOKUP.get(identifier)
    if item is None:
        message = "Environment is no longer available"
        _set_status(
            settings,
            message,
            details="Refresh the Entity Editor environment list and choose another definition.",
            level="WARNING",
        )
        return EnvironmentUIResult(False, message)

    try:
        environment, resolved = _load_environment(
            context.scene,
            item.depot_path,
            slot="selector_environment",
        )
    except Exception as exc:
        _set_status(settings, "Environment load failed", details=str(exc), level="ERROR")
        return EnvironmentUIResult(False, str(exc), None, item.depot_path)

    settings.preview_environment = identifier
    _set_status(
        settings,
        "Preview environment loaded",
        details=_environment_details(resolved),
        level="OK",
    )
    if settings.preview_enabled:
        _refresh_preview(context, ensure=True, quiet=True)
    else:
        settings.preview_enabled = True
    return EnvironmentUIResult(True, settings.status_text, environment, resolved)


def _preview_environment_selector_label(settings, context) -> str:
    identifier = str(getattr(settings, "preview_environment", "") or ENVIRONMENT_OFF_IDENTIFIER)
    if identifier == ENVIRONMENT_OFF_IDENTIFIER:
        return "ENV - OFF"
    _rebuild_environment_selector_cache(context)
    item = _ENVIRONMENT_SELECTOR_LOOKUP.get(identifier)
    return item.label if item is not None else "ENV - OFF"


def _on_fields_source_changed(settings, context) -> None:
    if context is None:
        return
    try:
        _sync_environment_fields(settings.id_data)
    except Exception as exc:
        _set_status(settings, "Field display failed", details=str(exc), level="WARNING")


class WITCH_PG_EnvironmentFieldChild(bpy.types.PropertyGroup):
    parent_key: StringProperty(default="")
    item_key: StringProperty(default="")
    label: StringProperty(default="")
    type_text: StringProperty(default="")
    value_text: StringProperty(default="")
    depth: IntProperty(default=0)
    has_children: BoolProperty(default=False)
    show_children: BoolProperty(default=False)


class WITCH_PG_EnvironmentFieldItem(bpy.types.PropertyGroup):
    class_name: StringProperty(default="")
    field_name: StringProperty(default="")
    type_text: StringProperty(default="")
    value_text: StringProperty(default="")
    is_set: BoolProperty(default=False)
    show_unset: BoolProperty(name="Show Unset", default=False)
    has_children: BoolProperty(default=False)
    show_children: BoolProperty(default=False)
    children: CollectionProperty(type=WITCH_PG_EnvironmentFieldChild)


class WITCH_PG_EnvironmentWeatherPreset(bpy.types.PropertyGroup):
    name: StringProperty(name="Preset", default="")
    environment_path: StringProperty(name="Environment", default="", subtype="FILE_PATH")
    probability: FloatProperty(name="Probability", default=0.0)
    wind_scale: FloatProperty(name="Wind Scale", default=0.0)
    blend_time: FloatProperty(name="Blend Time", default=0.0, subtype="TIME")
    environment_blend: FloatProperty(name="Environment Blend", default=0.0)
    occurrence_time: FloatProperty(name="Occurrence Time", default=0.0, subtype="TIME")
    skybox: StringProperty(name="Skybox", default="")
    effect_count: IntProperty(name="Effects", default=0)


class WITCH_PG_EnvironmentSettings(bpy.types.PropertyGroup):
    preview_environment: StringProperty(
        name="Environment",
        description="Environment used by the scene preview",
        default=ENVIRONMENT_OFF_IDENTIFIER,
    )
    world_path: StringProperty(
        name="World",
        description="Absolute path to a cooked or uncooked Witcher world",
        default="",
        subtype="FILE_PATH",
    )
    environment_path: StringProperty(
        name="Base",
        description="Environment definition path; relative paths use the configured depots",
        default="",
        subtype="FILE_PATH",
    )
    scenes_environment_path: StringProperty(
        name="Scenes",
        description="World scene environment definition",
        default="",
        subtype="FILE_PATH",
    )
    weather_path: StringProperty(
        name="Weather",
        description="Weather table path; relative paths use the configured depots",
        default="",
        subtype="FILE_PATH",
    )
    fake_day_seconds: FloatProperty(
        name="Seconds",
        description="Preview time in seconds from midnight",
        default=43200.0,
        min=0.0,
        max=86399.999,
        precision=1,
        update=_on_daycycle_changed,
    )
    fake_day_hour: FloatProperty(
        name="Hour",
        description="Preview time as a decimal hour",
        min=0.0,
        max=24.0,
        precision=3,
        get=_get_fake_hour,
        set=_set_fake_hour,
    )
    fake_day_number: IntProperty(
        name="Day",
        description="Preview date driving the moon phase (29.53-day cycle; day 0 is full)",
        default=0,
        min=0,
        soft_max=30,
        update=_on_daycycle_changed,
    )
    fields_source: EnumProperty(
        name="Show",
        description="Which loaded environment definition to display",
        items=(
            ("BASE", "Base", "World environment definition"),
            ("SCENES", "Scenes", "Scenes environment definition"),
            ("WEATHER", "Weather", "Selected weather preset overlay environment"),
        ),
        default="BASE",
        update=_on_fields_source_changed,
    )
    definition_fields: CollectionProperty(type=WITCH_PG_EnvironmentFieldItem)
    definition_fields_path: StringProperty(name="Definition", default="")
    weather_presets: CollectionProperty(type=WITCH_PG_EnvironmentWeatherPreset)
    weather_index: IntProperty(
        name="Weather Index",
        default=-1,
        update=_on_weather_index_changed,
    )
    active_weather_name: StringProperty(name="Selected", default="")
    preview_enabled: BoolProperty(
        name="Preview",
        description="Create and update the managed sky, stars, sun, moon, and key light preview",
        default=False,
        update=_on_preview_enabled,
    )
    full_effects: BoolProperty(
        name="Full Effects",
        description="Enable the realtime balance LUT and volumetric fog; slower in large rendered viewports",
        default=False,
        update=_on_preview_setting_changed,
    )
    import_materials: BoolProperty(
        name="Imported Materials",
        description="Use the imported visible sun and textured moon materials",
        default=True,
        update=_on_preview_setting_changed,
    )
    sky_enabled: BoolProperty(
        name="Sky",
        description="Use a managed World with the evaluated sky gradient and stars cubemap",
        default=True,
        update=_on_preview_setting_changed,
    )
    stars_brightness: FloatProperty(
        name="Stars",
        description="Brightness multiplier for the stars cubemap at night",
        default=1.1,
        min=0.0,
        soft_max=20.0,
        update=_on_preview_setting_changed,
    )
    cloud_amount: FloatProperty(
        name="Clouds",
        description="Strength of the selected weather cloud layer (0 disables clouds)",
        default=0.45,
        min=0.0,
        max=1.0,
        update=_on_preview_setting_changed,
    )
    sun_brightness: FloatProperty(
        name="Sun",
        description="Emission multiplier for the authored visible sun color",
        default=5.0,
        min=0.0,
        soft_max=20.0,
        update=_on_preview_setting_changed,
    )
    moon_brightness: FloatProperty(
        name="Moon",
        description="Emission multiplier for the authored visible moon color",
        default=1.0,
        min=0.0,
        soft_max=10.0,
        update=_on_preview_setting_changed,
    )
    moon_size_scale: FloatProperty(
        name="Moon Scale",
        description="Preview size multiplier applied after the authored environment moon size",
        default=1.0,
        min=0.1,
        soft_max=3.0,
        precision=2,
        update=_on_preview_setting_changed,
    )
    anchor_distance: FloatProperty(
        name="Distance",
        description=(
            "Distance from the viewer to the sun and moon discs; they scale with it, "
            "keeping their angular size; larger values place them behind more scenery"
        ),
        default=900.0,
        min=1.0,
        soft_max=10000.0,
        subtype="DISTANCE",
        update=_on_preview_setting_changed,
    )
    key_light_energy: FloatProperty(
        name="Key Energy",
        description="Preview multiplier for the imported global key light",
        default=1.0,
        min=0.0,
        soft_max=20.0,
        update=_on_preview_setting_changed,
    )
    ambient_light_energy: FloatProperty(
        name="Ambient Energy",
        description="Preview multiplier for the environment-probe ambient fill",
        default=1.0,
        min=0.0,
        soft_max=4.0,
        update=_on_preview_setting_changed,
    )
    status_text: StringProperty(name="Status", default="Ready")
    status_details: StringProperty(name="Details", default="Load a world or an environment definition.")
    status_level: EnumProperty(
        name="Status Level",
        items=(
            ("INFO", "Info", "Informational status"),
            ("OK", "Ready", "Operation completed"),
            ("WARNING", "Warning", "Operation completed with a warning"),
            ("ERROR", "Error", "Operation failed"),
        ),
        default="INFO",
        options={"HIDDEN"},
    )


class WITCH_OT_EnvironmentSelectorSelect(bpy.types.Operator):
    bl_idname = "witcher.environment_selector_select"
    bl_label = "Select Preview Environment"
    bl_description = "Search for and load a native Entity Editor preview environment"
    bl_property = "environment"

    environment: EnumProperty(
        name="Environment",
        description="Environment definition to load",
        items=_enum_preview_environment_items,
    )

    def invoke(self, context, event):
        settings = _settings(context.scene)
        current = str(getattr(settings, "preview_environment", "") or ENVIRONMENT_OFF_IDENTIFIER)
        identifiers = {item[0] for item in _rebuild_environment_selector_cache(context)}
        self.environment = current if current in identifiers else ENVIRONMENT_OFF_IDENTIFIER
        context.window_manager.invoke_search_popup(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        settings = _settings(context.scene)
        identifier = str(self.environment or ENVIRONMENT_OFF_IDENTIFIER)
        result = _apply_preview_environment_identifier(settings, context, identifier)
        if not result.ok:
            self.report({"ERROR"}, result.message)
            return {"CANCELLED"}
        settings.preview_environment = identifier
        self.report({"INFO"}, result.message)
        return {"FINISHED"}


class WITCH_OT_EnvironmentSelectorRefresh(bpy.types.Operator):
    bl_idname = "witcher.environment_selector_refresh"
    bl_label = "Refresh Environments"
    bl_description = "Rescan environment\\definitions in the configured depots"

    def execute(self, context):
        settings = _settings(context.scene)
        previous = str(getattr(settings, "preview_environment", "") or ENVIRONMENT_OFF_IDENTIFIER)
        items = _rebuild_environment_selector_cache(context, force=True)
        identifiers = {item[0] for item in items}
        if previous not in identifiers:
            _apply_preview_environment_identifier(settings, context, ENVIRONMENT_OFF_IDENTIFIER)
            settings.preview_environment = ENVIRONMENT_OFF_IDENTIFIER
        count = max(0, len(items) - 1)
        self.report({"INFO"}, f"Found {count} environment definitions")
        return {"FINISHED"}


class WITCH_OT_EnvironmentLoad(bpy.types.Operator):
    bl_idname = "witcher.environment_load"
    bl_label = "Load Environment Source"
    bl_description = "Load the selected world environment source"
    bl_options = {"REGISTER"}

    kind: EnumProperty(
        items=(
            ("WORLD", "World", "Load world environment parameters"),
            ("ENVIRONMENT", "Base Environment", "Load the base environment definition"),
            ("SCENES_ENVIRONMENT", "Scenes Environment", "Load the scenes environment definition"),
            ("WEATHER", "Weather", "Load the weather preset table"),
        ),
        default="WORLD",
    )

    def execute(self, context):
        settings = _settings(context.scene)
        path = _source_path(settings, self.kind)
        if not path:
            self.report({"WARNING"}, "Choose a source path first")
            return {"CANCELLED"}
        try:
            if self.kind == "WORLD":
                world, resolved, warnings = _load_world(context.scene, path)
                details = f"World: {resolved}"
                if warnings:
                    details += "\n\nReferenced sources:\n" + "\n".join(warnings)
                _set_status(
                    settings,
                    "World loaded" if not warnings else "World loaded with warnings",
                    details=details,
                    level="OK" if not warnings else "WARNING",
                )
            elif self.kind == "WEATHER":
                table, resolved = _load_weather(context.scene, path)
                count = len(_preset_sequence(table))
                warnings = tuple(getattr(table, "warnings", ()) or ())
                _set_status(
                    settings,
                    f"{count} weather presets" if not warnings else f"{count} presets with warnings",
                    details=_weather_details(table, resolved),
                    level="OK" if not warnings else "WARNING",
                )
            else:
                slot = "environment" if self.kind == "ENVIRONMENT" else "scenes_environment"
                _environment, resolved = _load_environment(context.scene, path, slot=slot)
                label = "Base environment loaded" if slot == "environment" else "Scenes environment loaded"
                _set_status(settings, label, details=_environment_details(resolved), level="OK")
            if settings.preview_enabled:
                _refresh_preview(context, ensure=True, quiet=True)
        except Exception as exc:
            label = self.kind.replace("_", " ").title()
            _set_status(settings, f"{label} load failed", details=str(exc), level="ERROR")
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class WITCH_OT_EnvironmentSelectWeather(bpy.types.Operator):
    bl_idname = "witcher.environment_select_weather"
    bl_label = "Select Weather"
    bl_description = "Select this weather preset and apply its environment overlay"

    index: IntProperty(default=-1)

    def execute(self, context):
        settings = _settings(context.scene)
        if self.index < 0 or self.index >= len(settings.weather_presets):
            return {"CANCELLED"}
        settings.weather_index = self.index
        item = settings.weather_presets[self.index]
        _set_status(settings, "Weather selected", details=f"Preset: {item.name}", level="OK")
        return {"FINISHED"}


class WITCH_OT_EnvironmentPreview(bpy.types.Operator):
    bl_idname = "witcher.environment_preview"
    bl_label = "Environment Preview"
    bl_description = "Create, refresh, or clear the managed environment preview"
    bl_options = {"REGISTER", "UNDO"}

    action: EnumProperty(
        items=(
            ("CREATE", "Create", "Create or rebuild the preview"),
            ("REFRESH", "Refresh", "Update the current preview"),
            ("CLEAR", "Clear", "Remove the managed preview"),
        ),
        default="REFRESH",
    )

    def execute(self, context):
        settings = _settings(context.scene)
        try:
            if self.action == "CLEAR":
                removed = _preview_module().clear_preview(context)
                settings.preview_enabled = False
                _set_status(settings, "Preview cleared", details=f"Removed objects: {removed}", level="OK")
            else:
                _ensure_loaded(context)
                if settings.preview_enabled:
                    _refresh_preview(context, ensure=self.action == "CREATE")
                else:
                    settings.preview_enabled = True
        except Exception as exc:
            _set_status(settings, "Preview failed", details=str(exc), level="ERROR")
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class WITCH_OT_EnvironmentDetails(bpy.types.Operator):
    bl_idname = "witcher.environment_details"
    bl_label = "Environment Details"
    bl_description = "Show copyable environment paths and the full status message"

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=560)

    def draw(self, context):
        settings = _settings(context.scene)
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        layout.prop(settings, "world_path", text="World")
        layout.prop(settings, "environment_path", text="Base")
        layout.prop(settings, "scenes_environment_path", text="Scenes")
        layout.prop(settings, "weather_path", text="Weather")
        layout.prop(settings, "active_weather_name", text="Preset")
        layout.prop(settings, "status_details", text="Status")

    def execute(self, context):
        return {"FINISHED"}


class WITCH_OT_EnvironmentWeatherInfo(bpy.types.Operator):
    bl_idname = "witcher.environment_weather_info"
    bl_label = "Weather Preset Details"
    bl_description = "Show copyable values for this weather preset"

    index: IntProperty(default=-1)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=520)

    def draw(self, context):
        settings = _settings(context.scene)
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        if self.index < 0 or self.index >= len(settings.weather_presets):
            layout.label(text="Preset is no longer available.", icon="INFO")
            return
        item = settings.weather_presets[self.index]
        layout.prop(item, "name", text="Preset")
        layout.prop(item, "environment_path", text="Environment")
        layout.prop(item, "probability")
        layout.prop(item, "wind_scale")
        layout.prop(item, "blend_time")
        layout.prop(item, "environment_blend")
        layout.prop(item, "occurrence_time")
        layout.prop(item, "skybox")
        layout.prop(item, "effect_count")

    def execute(self, context):
        return {"FINISHED"}


class WITCH_UL_EnvironmentWeatherPresets(bpy.types.UIList):
    """Compact weather picker: actions/status first, one primary text label."""

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index=0):
        if self.layout_type in {"DEFAULT", "COMPACT"}:
            selected = index == int(getattr(data, "weather_index", -1))
            select = layout.operator(
                WITCH_OT_EnvironmentSelectWeather.bl_idname,
                text="",
                icon="RADIOBUT_ON" if selected else "RADIOBUT_OFF",
                emboss=False,
            )
            select.index = index
            info = layout.operator(
                WITCH_OT_EnvironmentWeatherInfo.bl_idname,
                text="",
                icon="INFO",
                emboss=False,
            )
            info.index = index
            layout.label(text=item.name or "Unnamed")
        else:
            layout.alignment = "CENTER"
            layout.label(text="", icon="WORLD_DATA")


def _section(layout, panel_id: str, title: str, icon: str, *, default_closed: bool = False):
    try:
        header, body = layout.panel(panel_id, default_closed=default_closed)
        header.label(text=title, icon=icon)
        return body
    except Exception:
        box = layout.box()
        box.label(text=title, icon=icon)
        return box


def _stack_name(environment, path: str) -> str:
    source = str(getattr(environment, "source_path", "") or path or "")
    return Path(source).name if source else "-"


def _draw_active_stack(layout, scene, settings) -> None:
    runtime = environment_runtime(scene, create=False) or {}
    base = runtime.get("environment")
    scenes = runtime.get("scenes_environment")
    overlay = runtime.get("weather_environment")
    blend = _weather_blend(settings)

    box = layout.box()
    box.label(text="Active", icon="RENDERLAYERS")
    col = box.column(align=True)
    rows = (
        ("Base", base, settings.environment_path, base is not None, ""),
        ("Scenes", scenes, settings.scenes_environment_path, base is None and scenes is not None, ""),
        (
            "Weather",
            overlay,
            "",
            overlay is not None and blend > 0.0,
            f"{settings.active_weather_name} {int(round(blend * 100.0))}%" if overlay is not None else "",
        ),
    )
    for label, environment, path, sampled, suffix in rows:
        row = col.row(align=True)
        row.label(text="", icon="RADIOBUT_ON" if sampled else "BLANK1")
        text = f"{label}: {_stack_name(environment, path)}"
        if suffix:
            text = f"{text} ({suffix.strip()})"
        row.label(
            text=_short_text(text),
            icon="CHECKMARK" if environment is not None else "BLANK1",
        )


def _load_button(layout, kind: str, text: str = "Load") -> None:
    op = layout.operator(WITCH_OT_EnvironmentLoad.bl_idname, text=text, icon="FILE_REFRESH")
    op.kind = kind


def draw_entity_environment_selector(layout, context) -> None:
    """Draw the native Entity Editor-style preview environment choice."""

    settings = _settings(context.scene)
    box = layout.box()
    box.label(text="Preview Environment", icon="WORLD_DATA")
    if settings is None:
        box.label(text="Environment settings unavailable.", icon="INFO")
        return

    items = _rebuild_environment_selector_cache(context)
    row = box.row(align=True)
    row.operator(
        WITCH_OT_EnvironmentSelectorSelect.bl_idname,
        text=_preview_environment_selector_label(settings, context),
        icon="VIEWZOOM",
    )
    row.operator(WITCH_OT_EnvironmentSelectorRefresh.bl_idname, text="", icon="FILE_REFRESH")
    if len(items) <= 1:
        box.label(text="No .env definitions found in configured depots.", icon="INFO")
    status = box.row(align=True)
    status.label(text=_short_text(settings.status_text), icon=_status_icon(settings))
    status.operator(WITCH_OT_EnvironmentDetails.bl_idname, text="", icon="INFO")


class WITCH_PT_Environment(WITCH_PT_Base, bpy.types.Panel):
    bl_idname = "WITCH_PT_Environment"
    bl_label = "Environment"

    def draw_header(self, context):
        self.layout.label(text="", icon="WORLD_DATA")

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = False
        layout.use_property_decorate = False
        settings = _settings(context.scene)
        if settings is None:
            layout.label(text="Environment settings unavailable.", icon="INFO")
            return

        source = _section(layout, "witcher_environment_source", "Source", "WORLD_DATA")
        if source is not None:
            source.prop(settings, "world_path", text="World")
            row = source.row(align=True)
            _load_button(row, "WORLD", "Load World")
            row.operator(WITCH_OT_EnvironmentDetails.bl_idname, text="", icon="INFO")
            status = source.row(align=True)
            status.label(text=_short_text(settings.status_text), icon=_status_icon(settings))
            _draw_active_stack(source, context.scene, settings)

        day = _section(layout, "witcher_environment_day_cycle", "Day Cycle", "TIME")
        if day is not None:
            time_col = day.column(align=True)
            time_col.prop(settings, "fake_day_hour", slider=True)
            time_col.prop(settings, "fake_day_seconds", slider=True)
            time_col.prop(settings, "fake_day_number")

        definition = _section(
            layout,
            "witcher_environment_definition",
            "Environment Definition",
            "LIGHT_SUN",
        )
        if definition is not None:
            definition.prop(settings, "environment_path", text="Base")
            _load_button(definition, "ENVIRONMENT")
            definition.prop(settings, "scenes_environment_path", text="Scenes")
            _load_button(definition, "SCENES_ENVIRONMENT")

        weather = _section(layout, "witcher_environment_weather", "Weather", "MOD_FLUID")
        if weather is not None:
            weather.prop(settings, "weather_path", text="Table")
            _load_button(weather, "WEATHER", "Load Presets")
            if settings.weather_presets:
                weather.template_list(
                    WITCH_UL_EnvironmentWeatherPresets.__name__,
                    "",
                    settings,
                    "weather_presets",
                    settings,
                    "weather_index",
                    rows=min(7, max(3, len(settings.weather_presets))),
                )
                weather.prop(settings, "active_weather_name", text="Selected")
            else:
                weather.label(text="No weather presets loaded.", icon="INFO")

        fields = _section(
            layout,
            "witcher_environment_fields",
            "Definition Fields",
            "PROPERTIES",
            default_closed=True,
        )
        if fields is not None:
            fields.row(align=True).prop(settings, "fields_source", expand=True)
            if settings.definition_fields_path:
                fields.label(text=_short_text(settings.definition_fields_path, 64), icon="FILE")
            items = list(settings.definition_fields)
            _draw_imported_class_sections(
                fields,
                items,
                _schema_from_field_items(items),
                False,
                "No definition loaded. Load a world or environment first.",
                per_class_show_unset=True,
            )

        preview = _section(layout, "witcher_environment_preview", "Preview", "SHADING_RENDERED")
        if preview is not None:
            preview.prop(settings, "preview_enabled", text="Enable Preview")
            preview.prop(settings, "full_effects", text="Full Effects")

            sky = preview.box()
            sky.label(text="Sky", icon="WORLD")
            sky.prop(settings, "sky_enabled", text="Show Sky")
            stars = sky.column(align=True)
            stars.enabled = settings.sky_enabled
            stars.prop(settings, "stars_brightness", text="Stars")
            stars.prop(settings, "cloud_amount", text="Clouds")

            bodies = preview.box()
            bodies.label(text="Sun & Moon", icon="LIGHT_SUN")
            bodies.prop(settings, "import_materials", text="Imported Look")
            bodies.prop(settings, "sun_brightness", text="Sun")
            bodies.prop(settings, "moon_brightness", text="Moon")
            bodies.prop(settings, "moon_size_scale", text="Moon Scale")

            placement = preview.box()
            placement.label(text="Placement & Light", icon="LIGHT_DATA")
            placement.prop(settings, "anchor_distance", text="Distance")
            placement.prop(settings, "key_light_energy", text="Key Energy")
            placement.prop(settings, "ambient_light_energy", text="Ambient")
            row = preview.row(align=True)
            create = row.operator(WITCH_OT_EnvironmentPreview.bl_idname, text="Create", icon="ADD")
            create.action = "CREATE"
            refresh = row.operator(WITCH_OT_EnvironmentPreview.bl_idname, text="Refresh", icon="FILE_REFRESH")
            refresh.action = "REFRESH"
            clear = row.operator(WITCH_OT_EnvironmentPreview.bl_idname, text="", icon="TRASH")
            clear.action = "CLEAR"


classes = (
    WITCH_PG_EnvironmentFieldChild,
    WITCH_PG_EnvironmentFieldItem,
    WITCH_PG_EnvironmentWeatherPreset,
    WITCH_PG_EnvironmentSettings,
    WITCH_OT_EnvironmentSelectorSelect,
    WITCH_OT_EnvironmentSelectorRefresh,
    WITCH_OT_EnvironmentLoad,
    WITCH_OT_EnvironmentSelectWeather,
    WITCH_OT_EnvironmentPreview,
    WITCH_OT_EnvironmentDetails,
    WITCH_OT_EnvironmentWeatherInfo,
    WITCH_UL_EnvironmentWeatherPresets,
    WITCH_PT_Environment,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    if hasattr(bpy.types.Scene, _SCENE_PROP):
        delattr(bpy.types.Scene, _SCENE_PROP)
    setattr(
        bpy.types.Scene,
        _SCENE_PROP,
        PointerProperty(type=WITCH_PG_EnvironmentSettings),
    )


def unregister():
    global _ENVIRONMENT_SELECTOR_CACHE_KEY
    global _ENVIRONMENT_SELECTOR_CATALOG
    global _ENVIRONMENT_SELECTOR_LOOKUP
    global _ENVIRONMENT_SELECTOR_ENUM_ITEMS

    try:
        preview = _preview_module()
        if getattr(bpy.context, "scene", None) is not None:
            preview.clear_preview(bpy.context)
        preview.stop_preview_runtime()
    except Exception:
        pass
    clear_environment_runtime()
    _ENVIRONMENT_SELECTOR_CACHE_KEY = None
    _ENVIRONMENT_SELECTOR_CATALOG = ()
    _ENVIRONMENT_SELECTOR_LOOKUP = {}
    _ENVIRONMENT_SELECTOR_ENUM_ITEMS = (
        (
            ENVIRONMENT_OFF_IDENTIFIER,
            "ENV - OFF",
            "Disable the Entity Editor preview environment",
            "WORLD_DATA",
            0,
        ),
    )
    if hasattr(bpy.types.Scene, _SCENE_PROP):
        delattr(bpy.types.Scene, _SCENE_PROP)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


__all__ = (
    "EnvironmentUIResult",
    "WITCH_PT_Environment",
    "clear_environment_runtime",
    "draw_entity_environment_selector",
    "environment_runtime",
    "load_environment_path",
    "load_weather_path",
    "register",
    "sync_world_import",
    "unregister",
)
