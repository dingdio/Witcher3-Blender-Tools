from __future__ import annotations

import json
from math import pi, radians

import bpy
from bpy.app.handlers import persistent
from mathutils import Matrix

from ..CR2W.prop_utils import prop_to_string, read_enum_prop


ENV_COLOR_GROUPS = (
    "ECG_Default",
    "ECG_LightsDefault",
    "ECG_LightsDawn",
    "ECG_LightsNoon",
    "ECG_LightsEvening",
    "ECG_LightsNight",
    "ECG_FX_Default",
    "ECG_FX_Fire",
    "ECG_FX_FireFlares",
    "ECG_FX_FireLight",
    "ECG_FX_Smoke",
    "ECG_FX_SmokeExplosion",
    "ECG_FX_Sky",
    "ECG_FX_SkyNight",
    "ECG_FX_SkyDawn",
    "ECG_FX_SkyNoon",
    "ECG_FX_SkySunset",
    "ECG_FX_SkyRain",
    "ECG_FX_MainCloudsMiddle",
    "ECG_FX_MainCloudsFront",
    "ECG_FX_MainCloudsBack",
    "ECG_FX_MainCloudsRim",
    "ECG_FX_BackgroundCloudsFront",
    "ECG_FX_BackgroundCloudsBack",
    "ECG_FX_BackgroundHazeFront",
    "ECG_FX_BackgroundHazeBack",
    "ECG_FX_Blood",
    "ECG_FX_Water",
    "ECG_FX_Fog",
    "ECG_FX_LightShaft",
    "ECG_FX_LightShaftSun",
    "ECG_FX_LightShaftInteriorDawn",
    "ECG_FX_LightShaftSpotlightDawn",
    "ECG_FX_LightShaftReflectionLightDawn",
    "ECG_FX_LightShaftInteriorNoon",
    "ECG_FX_LightShaftSpotlightNoon",
    "ECG_FX_LightShaftReflectionLightNoon",
    "ECG_FX_LightShaftInteriorEvening",
    "ECG_FX_LightShaftSpotlightEvening",
    "ECG_FX_LightShaftReflectionLightEvening",
    "ECG_FX_LightShaftInteriorNight",
    "ECG_FX_LightShaftSpotlightNight",
    "ECG_FX_LightShaftReflectionLightNight",
    "ECG_FX_Trails",
    "ECG_FX_ScreenParticles",
    "ECG_Custom0",
    "ECG_Custom1",
    "ECG_Custom2",
)

_SCENE_COLOR_GROUPS_PROP = "witcher_environment_light_color_groups"


def _driver_double_cross(frame, phase, offset, axis, fps):
    cycle = (float(frame) * 0.25 / max(0.001, float(fps)) + float(phase)) % 1.0
    t = 2.0 * cycle
    if t > 1.0:
        t -= 1.0
        sign = 1.0
        radius = float(offset)
    else:
        sign = -1.0
        radius = 2.0 * float(offset)
    xz = radius * (-sign * 3.0 * t * t + sign * 3.0 * t)
    if int(axis) == 1:
        return radius * (6.0 * t * t * t - 9.0 * t * t + 3.0 * t)
    return 0.5 * xz if int(axis) == 2 else xz


def _install_driver_namespace():
    bpy.app.driver_namespace["witcher_light_double_cross"] = _driver_double_cross


@persistent
def _restore_driver_namespace_on_load(_filepath=""):
    _install_driver_namespace()


def _driver_namespace_handlers():
    return [
        handler
        for handler in bpy.app.handlers.load_post
        if (
            getattr(handler, "__module__", "") == __name__
            and getattr(handler, "__name__", "") == "_restore_driver_namespace_on_load"
        )
    ]


def register_driver_namespace():
    for handler in _driver_namespace_handlers():
        bpy.app.handlers.load_post.remove(handler)
    bpy.app.handlers.load_post.append(_restore_driver_namespace_on_load)
    _install_driver_namespace()


def unregister_driver_namespace():
    for handler in _driver_namespace_handlers():
        bpy.app.handlers.load_post.remove(handler)
    function = bpy.app.driver_namespace.get("witcher_light_double_cross")
    if (
        function is _driver_double_cross
        or (
            getattr(function, "__module__", "") == __name__
            and getattr(function, "__name__", "") == "_driver_double_cross"
        )
    ):
        bpy.app.driver_namespace.pop("witcher_light_double_cross", None)


def environment_group_curve_suffix(group_name: str) -> str:
    name = str(group_name or "ECG_Default").removeprefix("ECG_")
    if name == "Default":
        name = "DefaultGroup"
    elif name.startswith("Custom"):
        name = "CustomGroup" + name[len("Custom") :]
    return "colorgroups." + name.replace("_", "").lower()


def _value(source, *names, default=None):
    for name in names:
        if isinstance(source, dict) and name in source:
            return source[name]
        try:
            value = source.get(name, None)
        except Exception:
            value = getattr(source, name, None)
        if value is not None:
            return value
    return default


def _number(value, default=0.0):
    if value is None:
        return float(default)
    value = getattr(value, "Value", value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _boolean(value, default=True):
    if value is None:
        return bool(default)
    value = getattr(value, "Value", value)
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "off", "no"}
    return bool(value)


def _text(value, default=""):
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip() or default
    if isinstance(value, (list, tuple, set)):
        text = "|".join(str(item).strip() for item in value if str(item).strip())
        return text or default
    text = read_enum_prop(value) or prop_to_string(value, default="")
    return str(text or default).strip()


def _struct_values(value):
    if isinstance(value, dict):
        return value
    result = {}
    for item in (
        getattr(value, "MoreProps", None)
        or getattr(value, "More", None)
        or getattr(value, "PROPS", None)
        or ()
    ):
        name = str(getattr(item, "theName", "") or "")
        if name:
            result[name] = getattr(item, "Value", None)
    return result


def _color_channels(value):
    values = _struct_values(value)
    channels = []
    for name in ("Red", "Green", "Blue"):
        channel = values.get(name, values.get(name.lower(), getattr(value, name, 255.0)))
        channels.append(max(0.0, min(255.0, _number(channel, 255.0))))
    return tuple(channels)


def _scene_color_groups(scene):
    if scene is None:
        return {}
    try:
        value = json.loads(str(scene.get(_SCENE_COLOR_GROUPS_PROP, "") or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def apply_light_environment(light_obj, color_groups=None):
    if light_obj is None or getattr(light_obj, "type", "") != "LIGHT":
        return
    base_color = tuple(float(value) for value in light_obj.get("witcher_light_base_color", (1.0, 1.0, 1.0)))
    base_energy = float(light_obj.get("witcher_light_base_energy", 0.0) or 0.0)
    group_name = str(light_obj.get("witcher_light_env_color_group", "ECG_Default") or "ECG_Default")
    groups = color_groups if isinstance(color_groups, dict) else {}
    group_color = groups.get(group_name, (1.0, 1.0, 1.0))
    try:
        group_color = tuple(max(0.0, float(value)) for value in group_color[:3])
    except (TypeError, ValueError):
        group_color = (1.0, 1.0, 1.0)
    final_color = tuple(base * group for base, group in zip(base_color, group_color))
    peak = max(final_color, default=0.0)
    light_obj.data.color = tuple(value / peak for value in final_color) if peak > 1.0e-8 else (0.0, 0.0, 0.0)
    light_obj["witcher_light_environment_color"] = list(group_color)
    light_obj["witcher_base_energy"] = base_energy * peak
    strength = max(0.0, min(1.0, float(light_obj.get("witcher_flicker_strength", 0.0) or 0.0)))
    light_obj.data.energy = base_energy * peak * (1.0 - 0.5 * strength)


def apply_environment_light_groups(scene, color_groups, *, store=True):
    groups = {
        str(name): [float(value) for value in values[:3]]
        for name, values in (color_groups or {}).items()
    }
    if store:
        scene[_SCENE_COLOR_GROUPS_PROP] = json.dumps(groups, separators=(",", ":"))
    for obj in scene.objects:
        if obj.type == "LIGHT" and "witcher_light_base_energy" in obj:
            apply_light_environment(obj, groups)


def _replace_driver(target, data_path, index=-1):
    try:
        target.driver_remove(data_path, index)
    except (TypeError, ValueError):
        pass
    fcurve = target.driver_add(data_path, index)
    fcurve.driver.type = "SCRIPTED"
    return fcurve


def _add_driver_property(fcurve, name, owner, data_path):
    variable = fcurve.driver.variables.new()
    variable.name = name
    variable.type = "SINGLE_PROP"
    target = variable.targets[0]
    if isinstance(owner, bpy.types.Scene):
        target.id_type = "SCENE"
    target.id = owner
    target.data_path = data_path


def configure_entity_light_flicker(
    light_obj,
    *,
    scene=None,
    phase=0.0,
):
    if light_obj is None or getattr(light_obj, "type", "") != "LIGHT":
        return
    light_data = getattr(light_obj, "data", None)
    if light_data is None or not hasattr(light_data, "energy"):
        return

    base_energy = float(light_obj.get("witcher_base_energy", light_data.energy) or 0.0)
    strength = max(
        0.0,
        min(1.0, float(light_obj.get("witcher_flicker_strength", 0.0) or 0.0)),
    )
    period = max(0.001, float(light_obj.get("witcher_flicker_period", 0.2) or 0.2))
    position_offset = max(
        0.0,
        float(light_obj.get("witcher_flicker_position_offset", 0.0) or 0.0),
    )
    light_obj["witcher_base_energy"] = base_energy
    light_obj["witcher_flicker_strength"] = strength
    light_obj["witcher_flicker_period"] = period
    light_obj["witcher_flicker_position_offset"] = position_offset

    render = getattr(scene, "render", None)
    fps = float(getattr(render, "fps", 24.0) or 24.0) / max(
        0.001,
        float(getattr(render, "fps_base", 1.0) or 1.0),
    )
    if strength > 0.0:
        if scene is not None:
            frequency = f"({2.0 * pi:.9g}*b/max(1,p*f))"
        else:
            frequency = f"{2.0 * pi / max(1.0, period * fps):.9g}"
        expression = (
            f"base*(1-{0.5 * strength:.9g}+{0.5 * strength:.9g}*("
            f"0.5*sin(frame*{frequency}+{phase:.9g})+"
            f"0.3*sin(frame*0.53*{frequency}+{phase + 1.3:.9g})+"
            f"0.2*sin(frame*1.79*{frequency}+{phase + 2.1:.9g})))"
        )
        fcurve = _replace_driver(light_data, "energy")
        _add_driver_property(fcurve, "base", light_obj, '["witcher_base_energy"]')
        _add_driver_property(fcurve, "p", light_obj, '["witcher_flicker_period"]')
        if scene is not None:
            _add_driver_property(fcurve, "f", scene, "render.fps")
            _add_driver_property(fcurve, "b", scene, "render.fps_base")
        fcurve.driver.expression = expression

    if strength > 0.0 and position_offset > 0.0:
        # Native RED uses a four-second DoubleCrossBezier path in XYZ.
        _install_driver_namespace()
        light_obj["witcher_flicker_position_phase"] = float(phase) % 1.0
        position_fps = "f/max(b,.001)" if scene is not None else f"{fps:.9g}"
        for axis in range(3):
            fcurve = _replace_driver(light_obj, "delta_location", axis)
            _add_driver_property(
                fcurve,
                "offset",
                light_obj,
                '["witcher_flicker_position_offset"]',
            )
            _add_driver_property(
                fcurve,
                "phase",
                light_obj,
                '["witcher_flicker_position_phase"]',
            )
            if scene is not None:
                _add_driver_property(fcurve, "f", scene, "render.fps")
                _add_driver_property(fcurve, "b", scene, "render.fps_base")
            fcurve.driver.expression = (
                "witcher_light_double_cross("
                f"frame,phase,offset,{axis},{position_fps})"
            )

    light_obj["witcher_light_flicker_driver"] = strength > 0.0


def configure_entity_light(light_obj, component, component_type=None, *, scene=None):
    component_type = str(component_type or _value(component, "type", "component_type", default="") or "")
    is_spot = component_type == "CSpotLightComponent"
    radius = max(0.01, _number(_value(component, "radius"), 5.0))
    brightness = max(0.0, _number(_value(component, "brightness"), 1.0))
    attenuation = max(0.0, min(1.0, _number(_value(component, "attenuation"), 1.0)))
    inner_angle = max(0.01, _number(_value(component, "innerAngle", "inner_angle"), 30.0))
    outer_angle = max(0.01, _number(_value(component, "outerAngle", "outer_angle"), 45.0))
    softness = max(0.0, _number(_value(component, "softness"), 2.0))
    shadow_mode = _text(_value(component, "shadowCastingMode", "shadow_casting_mode"), "LSCM_None")
    env_group = _text(_value(component, "envColorGroup", "env_color_group"), "ECG_Default")
    usage_mask = _text(_value(component, "lightUsageMask", "light_usage_mask"), "")
    enabled = _boolean(_value(component, "isEnabled", "is_enabled"), True)
    flicker = _struct_values(_value(component, "lightFlickering", "light_flickering", default={}))
    flicker_strength = max(0.0, min(1.0, _number(flicker.get("flickerStrength"), 0.0)))
    flicker_period = max(0.001, _number(flicker.get("flickerPeriod"), 0.2))
    position_offset = max(0.0, _number(flicker.get("positionOffset"), 0.0))

    # RED spots may approach 360 degrees; Blender spots stop at 180.
    wide_spot = is_spot and outer_angle > 180.0
    light_obj.data.type = "POINT" if wide_spot or not is_spot else "SPOT"
    if hasattr(light_obj.data, "normalize"):
        light_obj.data.normalize = True
    if hasattr(light_obj.data, "use_custom_distance"):
        light_obj.data.use_custom_distance = True
    if hasattr(light_obj.data, "cutoff_distance"):
        light_obj.data.cutoff_distance = radius
    light_obj.data.use_shadow = shadow_mode != "LSCM_None"
    # Eevee's custom cutoff supplies RED's quartic radius fade. For a
    # shadowless light, source radius plus compensated power closely matches
    # RED's 1/(1 + (attenuation*distance)^2) diffuse falloff.
    render_engine = str(getattr(getattr(scene, "render", None), "engine", "") or "")
    eevee_attenuation_compat = (
        render_engine.startswith("BLENDER_EEVEE")
        and not light_obj.data.use_shadow
        and attenuation > 1.0e-6
    )
    light_obj.data.shadow_soft_size = 1.0 / attenuation if eevee_attenuation_compat else 0.0
    if light_obj.data.type == "SPOT":
        light_obj.data.spot_size = radians(min(180.0, outer_angle))
        light_obj.data.spot_blend = max(0.0, min(1.0, 1.0 - inner_angle / outer_angle))

    color = _color_channels(_value(component, "color", default={}))
    base_color = tuple((channel / 255.0) ** 2 for channel in color)
    light_obj["witcher_light_base_color"] = list(base_color)
    light_obj["witcher_light_base_energy"] = (
        4.0 * pi * brightness / (attenuation * attenuation)
        if eevee_attenuation_compat
        else 4.0 * pi * brightness
    )
    light_obj["witcher_light_radius"] = radius
    light_obj["witcher_light_attenuation"] = attenuation
    light_obj["witcher_light_eevee_attenuation_compat"] = eevee_attenuation_compat
    light_obj["witcher_light_red_type"] = component_type
    light_obj["witcher_light_inner_angle"] = inner_angle
    light_obj["witcher_light_outer_angle"] = outer_angle
    light_obj["witcher_light_softness"] = softness
    light_obj["witcher_light_wide_spot_approximation"] = wide_spot
    light_obj["witcher_light_shadow_casting_mode"] = shadow_mode
    light_obj["witcher_light_env_color_group"] = env_group
    light_obj["witcher_light_usage_mask"] = usage_mask
    light_obj["witcher_light_receiver_mask_unsupported"] = any(
        flag in usage_mask
        for flag in ("LUM_IsExteriorOnly", "LUM_IsInteriorOnly")
    )
    light_obj["witcher_flicker_strength"] = flicker_strength
    light_obj["witcher_flicker_period"] = flicker_period
    light_obj["witcher_flicker_position_offset"] = position_offset
    light_obj.hide_render = not enabled or "LUM_ExcludeFromSceneRender" in usage_mask
    light_obj.hide_viewport = not enabled
    light_obj["witcher_light_enabled"] = enabled
    apply_light_environment(light_obj, _scene_color_groups(scene))
    configure_entity_light_flicker(light_obj, scene=scene)
    return light_obj.data.type


def orient_red_spot(light_obj):
    light_obj.matrix_world = light_obj.matrix_world @ Matrix.Rotation(radians(90.0), 4, "X")
