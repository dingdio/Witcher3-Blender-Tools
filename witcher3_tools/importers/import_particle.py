"""Bake deterministic Blender previews for supported Witcher 3 particles."""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
import hashlib
import json
import logging
from math import ceil, cos, pi, sin, sqrt
import os
import random
import time
from collections.abc import Mapping, Sequence

import bpy
from bpy.app.handlers import persistent
from mathutils import Quaternion


log = logging.getLogger(__name__)

PARTICLE_PREVIEW_VERSION = 12
PARTICLE_OBJECT_PROP = "witcher_particle_preview"
PARTICLE_MATERIAL_PROP = "witcher_particle_preview_material"
PARTICLE_MESH_PROP = "witcher_particle_preview_mesh"
PARTICLE_BILLBOARD_BASIS_PROP = "witcher_particle_billboard_basis"
PARTICLE_GENERATIONS = 4
_RESPAWN_GAP_FRAMES = 0.1
_ADDITIVE_PREVIEW_GAIN = 6.0
_BILLBOARD_UPDATE_INTERVAL = 1.0 / 30.0
_BILLBOARD_CONSTRAINT_NAME = "Witcher Particle Camera Facing"
_BILLBOARD_MODE_PROP = "witcher_particle_billboard_mode"
_BILLBOARD_TARGET_PROP = "witcher_particle_billboard_target"
_BILLBOARD_ROOT_PROP = "witcher_particle_view_root"
_BILLBOARD_ROOT_NAME = "Witcher Particle View Root"

_SPLASH_TEXTURE = r"fx\textures\water\splash_with_normal.xbm"
_WATER_SPLASH_MATERIAL = r"fx\shaders\water_splash_additive.w2mg"

_MATERIAL_DEFAULTS = {
    r"fx\shaders\fire_sparks.w2mg": {
        "texture": r"fx\textures\fire\fire_spark_04.xbm",
        "atlas_width": 2,
        "atlas_height": 2,
        "color": (253.0 / 255.0, 130.0 / 255.0, 91.0 / 255.0, 1.0),
        "strength": 22.0,
    },
    r"fx\shaders\subuv_alpha_blend.w2mg": {
        "texture": r"fx\textures\smoke\puffy_smoke_8x8.xbm",
        "atlas_width": 8,
        "atlas_height": 8,
    },
}


def _get(value, name, default=None):
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _property(module, *names, default=None):
    getter = getattr(module, "property", None)
    for name in names:
        if callable(getter):
            value = getter(name, None)
            if value is not None:
                return value
        properties = _get(module, "properties", {}) or {}
        if isinstance(properties, Mapping) and name in properties:
            return properties[name]
    return default


def _module(emitter, type_name):
    getter = getattr(emitter, "module", None)
    if callable(getter):
        module = getter(type_name)
        if module is not None and bool(_get(module, "enabled", _get(module, "isEnabled", True))):
            return module
    return next(
        (
            module
            for module in (_get(emitter, "modules", ()) or ())
            if str(_get(module, "type_name", _get(module, "type", ""))) == type_name
            and bool(_get(module, "enabled", _get(module, "isEnabled", True)))
        ),
        None,
    )


def _evaluator(module, *names):
    if module is None:
        return None
    getter = getattr(module, "evaluator", None)
    for name in names:
        if callable(getter):
            value = getter(name)
            if value is not None:
                return value
        evaluators = _get(module, "evaluators", {}) or {}
        if isinstance(evaluators, Mapping) and name in evaluators:
            return evaluators[name]
    return None


def _material_parameter(material, name, default=None):
    if material is None:
        return default
    getter = getattr(material, "parameter", None)
    if callable(getter):
        value = getter(name, None)
        if value is not None:
            return value
    parameters = _get(material, "parameters", {}) or {}
    if isinstance(parameters, Mapping):
        if name in parameters:
            return parameters[name]
        lowered = name.lower()
        return next(
            (value for key, value in parameters.items() if str(key).lower() == lowered),
            default,
        )
    for parameter in parameters:
        if str(_get(parameter, "name", "")).lower() == name.lower():
            return _get(parameter, "value", parameter)
    return default


def _vector(value, default=(0.0, 0.0, 0.0, 0.0)):
    if value is None:
        return tuple(float(item) for item in default)
    if isinstance(value, Mapping):
        return tuple(float(value.get(axis, default[index]) or 0.0) for index, axis in enumerate("XYZW"))
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        items = list(value)
        return tuple(float(items[index] if index < len(items) else default[index]) for index in range(4))
    components = []
    for index, axis in enumerate("XYZW"):
        item = getattr(value, axis, getattr(value, axis.lower(), default[index]))
        components.append(float(item or 0.0))
    return tuple(components)


def _free_axis_count(evaluator):
    value = _get(evaluator, "free_axes", _get(evaluator, "freeAxes", 3))
    text = str(value)
    names = {"FVA_One": 1, "FVA_Two": 2, "FVA_Three": 3, "FVA_Four": 4}
    if text in names:
        return names[text]
    try:
        return max(1, min(4, int(value)))
    except (TypeError, ValueError):
        return 3


def _evaluator_vector(evaluator, value, default=(0.0, 0.0, 0.0, 0.0)):
    if isinstance(value, (int, float)):
        vector = (float(value), float(default[1]), float(default[2]), float(default[3]))
    else:
        vector = _vector(value, default)
    free_axes = _free_axis_count(evaluator)
    if bool(_get(evaluator, "spill", True)) and free_axes < 4:
        vector = vector[:free_axes] + (vector[free_axes - 1],) * (4 - free_axes)
    return vector


def _curve_keys(curve):
    keys = _get(curve, "keys", _get(curve, "points", None))
    if keys is None and isinstance(curve, Sequence) and not isinstance(curve, (str, bytes)):
        keys = curve
    return sorted(keys or (), key=lambda key: float(_get(key, "time", 0.0) or 0.0))


def _curve_type(key, side):
    short_side = "l" if side == "left" else "r"
    names = (
        f"curve_type_{side}",
        f"curve_type_{short_side}",
        f"type_{side}",
        f"curveType{side[:1].upper()}{side[1:]}",
    )
    value = next((_get(key, name, None) for name in names if _get(key, name, None) is not None), 1)
    text = str(value).lower()
    if "constant" in text or text == "0":
        return "constant"
    if "interpolate" in text or text == "1":
        return "linear"
    return "bezier"


def _tangent(key, side):
    value = _get(key, f"tangent_{side}", _get(key, f"control_{side}", None))
    if value is None:
        return (-0.1, 0.0) if side == "left" else (0.1, 0.0)
    vector = _vector(value)
    return vector[0], vector[1]


def _evaluate_curve(curve, time):
    evaluate = getattr(curve, "evaluate", None)
    if callable(evaluate):
        return float(evaluate(time))
    keys = _curve_keys(curve)
    if not keys:
        return 1.0
    times = [float(_get(key, "time", 0.0) or 0.0) for key in keys]
    if time <= times[0]:
        return float(_get(keys[0], "value", 0.0) or 0.0)
    if time >= times[-1]:
        return float(_get(keys[-1], "value", 0.0) or 0.0)
    right_index = bisect_left(times, time)
    if times[right_index] == time:
        return float(_get(keys[right_index], "value", 0.0) or 0.0)
    left, right = keys[right_index - 1], keys[right_index]
    start, end = times[right_index - 1], times[right_index]
    left_value = float(_get(left, "value", 0.0) or 0.0)
    right_value = float(_get(right, "value", 0.0) or 0.0)
    duration = end - start
    if duration <= 0.0:
        return right_value
    if _curve_type(left, "right") == "constant":
        return left_value
    factor = (time - start) / duration
    if _curve_type(left, "right") == "linear" and _curve_type(right, "left") == "linear":
        return left_value + (right_value - left_value) * factor
    outgoing = _tangent(left, "right")
    incoming = _tangent(right, "left")
    slope0 = duration * outgoing[1] / outgoing[0] if abs(outgoing[0]) > 1e-8 else 0.0
    slope1 = duration * incoming[1] / incoming[0] if abs(incoming[0]) > 1e-8 else 0.0
    cubic = 2.0 * left_value - 2.0 * right_value + slope0 + slope1
    quadratic = -3.0 * left_value + 3.0 * right_value - 2.0 * slope0 - slope1
    return ((cubic * factor + quadratic) * factor + slope0) * factor + left_value


def _evaluate(evaluator, time, default=0.0):
    if evaluator is None:
        return default
    curves = _get(evaluator, "curves", ()) or ()
    if curves:
        values = [_evaluate_curve(curve, time) for curve in curves]
        free_axes = _free_axis_count(evaluator)
        values = values[:free_axes] or [float(default)]
        if bool(_get(evaluator, "spill", True)):
            values += [values[-1]] * (4 - len(values))
        else:
            values += list(_vector(default))[len(values):]
        return tuple(values[:4])
    type_name = str(_get(evaluator, "type_name", _get(evaluator, "type", "")) or "")
    value = _get(evaluator, "value", None)
    if "Const" in type_name and value is not None:
        if isinstance(value, (Mapping, Sequence)) and not isinstance(value, (str, bytes)):
            return _evaluator_vector(evaluator, value)
        return float(value)
    start = _get(evaluator, "start", None)
    end = _get(evaluator, "end", None)
    if start is not None or end is not None:
        start_vector = _evaluator_vector(evaluator, start)
        end_vector = _evaluator_vector(evaluator, end)
        clamped = min(1.0, max(0.0, float(time)))
        result = tuple(a + (b - a) * clamped for a, b in zip(start_vector, end_vector))
        if all(not isinstance(value, (Mapping, Sequence)) for value in (start, end)):
            return result[0]
        return result
    if value is not None:
        if isinstance(value, (Mapping, Sequence)) and not isinstance(value, (str, bytes)):
            return _evaluator_vector(evaluator, value)
        return float(value)
    minimum = _get(evaluator, "minimum", _get(evaluator, "min", None))
    maximum = _get(evaluator, "maximum", _get(evaluator, "max", None))
    if minimum is not None or maximum is not None:
        return minimum if minimum is not None else maximum
    return default


def _random_scalar(evaluator, rng, axis=0, default=0.0):
    if evaluator is None:
        return float(default)
    minimum = _get(evaluator, "minimum", _get(evaluator, "min", None))
    maximum = _get(evaluator, "maximum", _get(evaluator, "max", None))
    if minimum is None and maximum is None:
        value = _evaluate(evaluator, 0.0, default)
        if isinstance(value, Sequence):
            return float(value[axis])
        return float(value)
    minimum = maximum if minimum is None else minimum
    maximum = minimum if maximum is None else maximum
    if axis or isinstance(minimum, (Mapping, Sequence)) or not isinstance(minimum, (int, float)):
        minimum = _evaluator_vector(evaluator, minimum)[axis]
    if axis or isinstance(maximum, (Mapping, Sequence)) or not isinstance(maximum, (int, float)):
        maximum = _evaluator_vector(evaluator, maximum)[axis]
    return rng.uniform(float(minimum), float(maximum))


def _scalar(evaluator, time=0.0, default=0.0):
    value = _evaluate(evaluator, time, default)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return float(value[0])
    return float(value)


def _stable_seed(source_path, emitter_name, index, seed):
    text = f"{seed if seed is not None else ''}|{source_path.lower()}|{emitter_name.lower()}|{index}"
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "little")


@dataclass(frozen=True)
class _EmitterSettings:
    name: str
    drawer: str
    lifetime: float
    birth_rate: float
    burst_count: int
    burst_interval: float
    emitter_loops: int
    max_particles: int
    lod_enabled: bool
    spawn_radius: float
    spawn_position: tuple[float, float, float]
    spawn_box_extents: tuple[float, float, float]
    spawn_sphere_inner: float
    spawn_sphere_outer: float
    spawn_sphere_surface: bool
    spawn_sphere_axes: tuple[bool, bool, bool, bool, bool, bool]
    spawn_sphere_velocity: bool
    spawn_sphere_force: float
    velocity_min: tuple[float, float, float]
    velocity_max: tuple[float, float, float]
    acceleration: tuple[float, float, float]
    velocity_evaluator: object
    velocity_modulate: bool
    base_size_min: tuple[float, float]
    base_size_max: tuple[float, float]
    size_axes: int
    size_spill: bool
    size_evaluator: object
    size_modulate: bool
    alpha_evaluator: object
    initial_rotation: object
    rotation_rate: object
    initial_frame: object
    animation_speed: float
    animation_mode: str
    atlas_width: int
    atlas_height: int
    material_path: str
    texture_path: str
    material_parameters: object

    @property
    def active_count(self):
        if not self.lod_enabled:
            return 0
        if self.burst_count > 0:
            repeats = 1 if self.burst_interval <= 0.0 else int(ceil(self.lifetime / self.burst_interval))
            if self.emitter_loops > 0:
                repeats = min(repeats, self.emitter_loops)
            count = self.burst_count * max(1, repeats)
        else:
            count = int(round(self.birth_rate * self.lifetime))
        return max(0, min(self.max_particles, count))


def _range_vectors(evaluator, default):
    minimum = _get(evaluator, "minimum", _get(evaluator, "min", None))
    maximum = _get(evaluator, "maximum", _get(evaluator, "max", None))
    if minimum is None and maximum is None:
        value = _evaluate(evaluator, 0.0, default)
        vector = _evaluator_vector(evaluator, value, default)
        return vector, vector
    minimum = maximum if minimum is None else minimum
    maximum = minimum if maximum is None else maximum
    return (
        _evaluator_vector(evaluator, minimum, default),
        _evaluator_vector(evaluator, maximum, default),
    )


def _representative_scalar(evaluator, default):
    minimum = _get(evaluator, "minimum", _get(evaluator, "min", None))
    maximum = _get(evaluator, "maximum", _get(evaluator, "max", None))
    if minimum is not None or maximum is not None:
        minimum = maximum if minimum is None else minimum
        maximum = minimum if maximum is None else maximum
        return 0.5 * (float(minimum) + float(maximum))
    return _scalar(evaluator, 0.0, default)


def _settings(emitter):
    life_module = _module(emitter, "CParticleInitializerLifeTime")
    size_module = _module(emitter, "CParticleInitializerSize")
    size_over_life = _module(emitter, "CParticleModificatorSizeOverLife")
    alpha_over_life = _module(emitter, "CParticleModificatorAlphaOverLife")
    spawn_circle = _module(emitter, "CParticleInitializerSpawnCircle")
    spawn_box = _module(emitter, "CParticleInitializerSpawnBox")
    spawn_sphere = _module(emitter, "CParticleInitializerSpawnSphere")
    position = _module(emitter, "CParticleInitializerPosition")
    velocity = _module(emitter, "CParticleInitializerVelocity")
    acceleration = _module(emitter, "CParticleModificatorAcceleration")
    velocity_over_life = _module(emitter, "CParticleModificatorVelocityOverLife")
    rotation = _module(emitter, "CParticleInitializerRotation")
    rotation_rate = _module(emitter, "CParticleInitializerRotationRate")
    texture_animation = _module(emitter, "CParticleModificatorTextureAnimation")

    lods = list(_get(emitter, "lods", ()) or ())
    lod = lods[0] if lods else {}
    birth_rate = _get(lod, "birth_rate", _get(lod, "birthRate", 0.0))
    if not isinstance(birth_rate, (int, float)):
        birth_rate = _scalar(birth_rate, 0.0, 0.0)

    bursts = tuple(_get(lod, "bursts", ()) or ())
    burst_count = sum(max(0, int(_get(burst, "spawn_count", 1) or 0)) for burst in bursts)
    positive_repeats = [float(_get(burst, "repeat_time", 0.0) or 0.0) for burst in bursts]
    positive_repeats = [value for value in positive_repeats if value > 0.0]
    burst_interval = min(positive_repeats) if positive_repeats else float(_get(lod, "duration", 0.0) or 0.0)

    size_evaluator = _evaluator(size_module, "size")
    size_min, size_max = _range_vectors(size_evaluator, (0.1, 0.1, 0.1, 0.1))

    direction = _evaluate(_evaluator(acceleration, "direction"), 0.0, (0.0, 0.0, -1.0, 0.0))
    direction = _vector(direction, (0.0, 0.0, -1.0, 0.0))
    acceleration_scale = _scalar(_evaluator(acceleration, "scale"), 0.0, 0.0) if acceleration else 0.0
    velocity_eval = _evaluator(velocity, "velocity")
    velocity_min, velocity_max = _range_vectors(velocity_eval, (0.0, 0.0, 0.0, 0.0))

    position_value = _evaluate(_evaluator(position, "position"), 0.0, (0.0, 0.0, 0.0, 0.0))
    box_value = _evaluate(_evaluator(spawn_box, "extents"), 0.0, (0.0, 0.0, 0.0, 0.0))
    material = _get(emitter, "material", None)
    material_path = str(_get(material, "base_material", _get(material, "baseMaterial", "")) or "")
    normalized_material_path = material_path.replace("/", "\\").lower()
    defaults = _MATERIAL_DEFAULTS.get(normalized_material_path, {})
    texture_path = next(
        (
            str(value)
            for name in ("normal_and_splash", "diffuse", "tex", "subuv_texture")
            if (value := _material_parameter(material, name, ""))
        ),
        str(defaults.get("texture", "") or ""),
    )
    if not texture_path and material_path.replace("/", "\\").lower() == _WATER_SPLASH_MATERIAL.lower():
        texture_path = _SPLASH_TEXTURE
    atlas_width_value = _material_parameter(material, "subuvwidth", None)
    if atlas_width_value is None:
        # CDPR's fire_glow graph ships this parameter with an extra `u`.
        atlas_width_value = _material_parameter(
            material,
            "subuUVwidth",
            defaults.get("atlas_width", 1),
        )
    atlas_width = max(1, int(round(float(atlas_width_value or 1))))
    atlas_height = max(1, int(round(float(
        _material_parameter(material, "subuvheight", defaults.get("atlas_height", 1)) or 1
    ))))
    initial_frame = _evaluator(texture_animation, "initialFrame", "initial_frame")
    animation_speed = _scalar(_evaluator(texture_animation, "animationSpeed", "animation_speed"), 0.0, 30.0) if texture_animation else 0.0
    animation_mode = str(_property(texture_animation, "animationMode", "animation_mode", default="TAM_Speed") or "TAM_Speed")
    sphere_axes = tuple(
        bool(_property(spawn_sphere, name, default=True))
        for name in (
            "spawnPositiveX", "spawnNegativeX", "spawnPositiveY",
            "spawnNegativeY", "spawnPositiveZ", "spawnNegativeZ",
        )
    )

    return _EmitterSettings(
        name=str(_get(emitter, "name", "particle") or "particle"),
        drawer=str(_get(emitter, "drawer_type", _get(emitter, "drawer", "")) or ""),
        lifetime=max(0.001, _representative_scalar(_evaluator(life_module, "lifeTime", "life_time"), 1.0)),
        birth_rate=max(0.0, float(birth_rate or 0.0)),
        burst_count=burst_count,
        burst_interval=max(0.0, burst_interval),
        emitter_loops=max(0, int(_get(emitter, "loops", _get(emitter, "emitterLoops", 0)) or 0)),
        max_particles=max(0, int(_get(emitter, "max_particles", _get(emitter, "maxParticles", 55)) or 55)),
        lod_enabled=bool(_get(lod, "enabled", _get(lod, "isEnabled", True))),
        spawn_radius=max(0.0, _scalar(_evaluator(spawn_circle, "outerRadius", "outer_radius"), 0.0, 0.0)),
        spawn_position=tuple(_vector(position_value)[:3]),
        spawn_box_extents=tuple(abs(value) for value in _vector(box_value)[:3]),
        spawn_sphere_inner=max(0.0, _scalar(_evaluator(spawn_sphere, "innerRadius", "inner_radius"), 0.0, 0.0)),
        spawn_sphere_outer=max(0.0, _scalar(_evaluator(spawn_sphere, "outerRadius", "outer_radius"), 0.0, 0.0)),
        spawn_sphere_surface=bool(_property(spawn_sphere, "surfaceOnly", "surface_only", default=False)),
        spawn_sphere_axes=sphere_axes,
        spawn_sphere_velocity=bool(_property(spawn_sphere, "velocity", default=False)),
        spawn_sphere_force=_scalar(_evaluator(spawn_sphere, "forceScale", "force_scale"), 0.0, 1.0),
        velocity_min=tuple(velocity_min[:3]),
        velocity_max=tuple(velocity_max[:3]),
        acceleration=tuple(value * acceleration_scale for value in direction[:3]),
        velocity_evaluator=_evaluator(velocity_over_life, "velocity"),
        velocity_modulate=bool(_property(velocity_over_life, "modulate", default=True)),
        base_size_min=tuple(size_min[:2]),
        base_size_max=tuple(size_max[:2]),
        size_axes=_free_axis_count(size_evaluator),
        size_spill=bool(_get(size_evaluator, "spill", True)),
        size_evaluator=_evaluator(size_over_life, "size"),
        size_modulate=bool(_property(size_over_life, "modulate", default=True)),
        alpha_evaluator=_evaluator(alpha_over_life, "alpha"),
        initial_rotation=_evaluator(rotation, "rotation"),
        rotation_rate=_evaluator(rotation_rate, "rotationRate", "rotation_rate"),
        initial_frame=initial_frame,
        animation_speed=animation_speed,
        animation_mode=animation_mode,
        atlas_width=atlas_width,
        atlas_height=atlas_height,
        material_path=material_path,
        texture_path=texture_path,
        material_parameters=_get(material, "parameters", {}) or {},
    )


def _load_particle_image(texture_path):
    if not texture_path:
        return None
    try:
        from ..CR2W.common_blender import repo_file, win_path_isfile
        from ..materials import material as material_module

        source_path = repo_file(texture_path)
        if not source_path or not win_path_isfile(source_path):
            return None
        dds_path = material_module._convert_xbm_to_writable_dds(source_path)
        if not dds_path or not win_path_isfile(dds_path):
            return None
        image_path = material_module._convert_dds_to_blender_image_cache(dds_path) or dds_path
        if not win_path_isfile(image_path):
            return None
        image = bpy.data.images.load(os.path.abspath(image_path), check_existing=True)
        image.colorspace_settings.name = "Non-Color"
        image.alpha_mode = "CHANNEL_PACKED"
        image["witcher_source_texture"] = texture_path
        return image
    except Exception:
        log.debug("Could not load particle texture %s", texture_path, exc_info=True)
        return None


def _separate_color_node(nodes):
    try:
        node = nodes.new("ShaderNodeSeparateColor")
        node.mode = "RGB"
        return node, node.outputs["Red"]
    except Exception:
        node = nodes.new("ShaderNodeSeparateRGB")
        return node, node.outputs["R"]


def _material_key(settings, has_image):
    parameters = settings.material_parameters
    if isinstance(parameters, Mapping):
        parameters = sorted((str(key), str(value)) for key, value in parameters.items())
    text = json.dumps(
        [PARTICLE_PREVIEW_VERSION, settings.material_path, settings.texture_path,
         settings.atlas_width, settings.atlas_height, parameters, bool(has_image)],
        sort_keys=True,
        default=str,
    )
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _material_float(settings, name, default):
    parameters = settings.material_parameters
    value = parameters.get(name, default) if isinstance(parameters, Mapping) else default
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _normal_strength(settings):
    parameters = settings.material_parameters
    # water_splash_additive.w2mg defaults normal_multiplier.W to 2.0.
    value = parameters.get("normal_multiplier", 2.0) if isinstance(parameters, Mapping) else 2.0
    if isinstance(value, Mapping):
        value = value.get("W", value.get("w", max(_vector(value, (1.0,) * 4)[:3])))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        values = list(value)
        value = values[3] if len(values) > 3 else max(values, default=1.0)
    try:
        return max(0.0, min(2.0, float(value)))
    except (TypeError, ValueError):
        return 2.0


def _particle_texture_node(settings, nodes, links, object_info, image):
    texture = nodes.new("ShaderNodeTexImage")
    texture.name = "W3 Particle Texture"
    texture.image = image
    texture.extension = "CLIP"
    texture.interpolation = "Linear"
    if settings.atlas_width * settings.atlas_height <= 1:
        return texture

    texcoord = nodes.new("ShaderNodeTexCoord")
    separate_uv = nodes.new("ShaderNodeSeparateXYZ")
    scale_u = nodes.new("ShaderNodeMath")
    scale_u.operation = "MULTIPLY"
    scale_u.inputs[1].default_value = 1.0 / settings.atlas_width
    scale_v = nodes.new("ShaderNodeMath")
    scale_v.operation = "MULTIPLY"
    scale_v.inputs[1].default_value = 1.0 / settings.atlas_height
    separate_color, frame_output = _separate_color_node(nodes)
    frame_floor = nodes.new("ShaderNodeMath")
    frame_floor.operation = "FLOOR"
    frame_column = nodes.new("ShaderNodeMath")
    frame_column.operation = "MODULO"
    frame_column.inputs[1].default_value = float(settings.atlas_width)
    frame_row = nodes.new("ShaderNodeMath")
    frame_row.operation = "DIVIDE"
    frame_row.inputs[1].default_value = float(settings.atlas_width)
    frame_row_floor = nodes.new("ShaderNodeMath")
    frame_row_floor.operation = "FLOOR"
    frame_row_mod = nodes.new("ShaderNodeMath")
    frame_row_mod.operation = "MODULO"
    frame_row_mod.inputs[1].default_value = float(settings.atlas_height)
    column_scale = nodes.new("ShaderNodeMath")
    column_scale.operation = "MULTIPLY"
    column_scale.inputs[1].default_value = 1.0 / settings.atlas_width
    row_scale = nodes.new("ShaderNodeMath")
    row_scale.operation = "MULTIPLY"
    row_scale.inputs[1].default_value = 1.0 / settings.atlas_height
    offset_u = nodes.new("ShaderNodeMath")
    offset_u.operation = "ADD"
    offset_v = nodes.new("ShaderNodeMath")
    offset_v.operation = "ADD"
    combine = nodes.new("ShaderNodeCombineXYZ")

    links.new(texcoord.outputs["UV"], separate_uv.inputs[0])
    links.new(separate_uv.outputs["X"], scale_u.inputs[0])
    links.new(separate_uv.outputs["Y"], scale_v.inputs[0])
    links.new(object_info.outputs["Color"], separate_color.inputs[0])
    links.new(frame_output, frame_floor.inputs[0])
    links.new(frame_floor.outputs[0], frame_column.inputs[0])
    links.new(frame_floor.outputs[0], frame_row.inputs[0])
    links.new(frame_row.outputs[0], frame_row_floor.inputs[0])
    links.new(frame_row_floor.outputs[0], frame_row_mod.inputs[0])
    links.new(frame_column.outputs[0], column_scale.inputs[0])
    links.new(frame_row_mod.outputs[0], row_scale.inputs[0])
    links.new(scale_u.outputs[0], offset_u.inputs[0])
    links.new(column_scale.outputs[0], offset_u.inputs[1])
    links.new(scale_v.outputs[0], offset_v.inputs[0])
    links.new(row_scale.outputs[0], offset_v.inputs[1])
    links.new(offset_u.outputs[0], combine.inputs["X"])
    links.new(offset_v.outputs[0], combine.inputs["Y"])
    links.new(combine.outputs[0], texture.inputs["Vector"])
    return texture


def _water_particle_material(settings, image, key):

    material = bpy.data.materials.new(f"Witcher Particle {settings.name}")
    material[PARTICLE_MATERIAL_PROP] = key
    material["witcher_particle_preview_version"] = PARTICLE_PREVIEW_VERSION
    material["witcher_source_material"] = settings.material_path
    material["witcher_source_texture"] = settings.texture_path
    material["witcher_particle_texture_loaded"] = bool(image)
    material["witcher_particle_atlas_width"] = settings.atlas_width
    material["witcher_particle_atlas_height"] = settings.atlas_height
    alpha_multiplier = max(0.0, _material_float(settings, "alpha_multiplier", 1.0))
    reflection_multiplier = max(0.0, _material_float(settings, "reflection_multiplier", 8.0))
    reflection_exponent = max(0.01, _material_float(settings, "reflection_power_exponent", 5.0))
    refraction_multiplier = max(0.0, _material_float(settings, "refraction_multiplier", 0.01))
    soft_alpha = max(0.0, _material_float(settings, "soft_alpha", 4.0))
    normal_strength = _normal_strength(settings)
    reflection_gain = min(0.75, reflection_multiplier / 32.0)
    reflection_power = min(1.5, max(0.25, reflection_exponent / 8.0))
    refraction_gain = min(0.20, refraction_multiplier * 4.0)
    # soft_alpha controls scene-depth fading, not opacity.
    preview_gain = _ADDITIVE_PREVIEW_GAIN
    material["witcher_particle_alpha_multiplier"] = alpha_multiplier
    material["witcher_particle_reflection_multiplier"] = reflection_multiplier
    material["witcher_particle_reflection_power_exponent"] = reflection_exponent
    material["witcher_particle_refraction_multiplier"] = refraction_multiplier
    material["witcher_particle_soft_alpha"] = soft_alpha
    material["witcher_particle_normal_strength"] = normal_strength
    material["witcher_particle_reflection_gain"] = reflection_gain
    material["witcher_particle_reflection_power"] = reflection_power
    material["witcher_particle_refraction_gain"] = refraction_gain
    material["witcher_particle_strength_cap"] = 0.8
    material["witcher_particle_preview_gain"] = preview_gain
    material.use_nodes = True
    material.diffuse_color = (0.65, 0.86, 1.0, 0.35)
    material.use_backface_culling = False

    nodes = material.node_tree.nodes
    links = material.node_tree.links
    nodes.clear()
    output = nodes.new("ShaderNodeOutputMaterial")
    transparent = nodes.new("ShaderNodeBsdfTransparent")
    emission = nodes.new("ShaderNodeEmission")
    emission.name = "W3 Particle Additive Emission"
    emission.inputs["Color"].default_value = (0.82, 0.92, 1.0, 1.0)
    add = nodes.new("ShaderNodeAddShader")
    add.name = "W3 Particle Additive Approximation"
    object_info = nodes.new("ShaderNodeObjectInfo")
    object_info.name = "W3 Particle Object Info"
    normal_map = nodes.new("ShaderNodeNormalMap")
    normal_map.name = "W3 Particle Normal Map"
    normal_map.inputs["Strength"].default_value = normal_strength
    normal_map.inputs["Color"].default_value = (0.5, 0.5, 1.0, 1.0)
    fresnel = nodes.new("ShaderNodeFresnel")
    fresnel.name = "W3 Particle Fresnel"
    fresnel.inputs["IOR"].default_value = 1.333
    reflection_power_node = nodes.new("ShaderNodeMath")
    reflection_power_node.name = "W3 Particle Reflection Power"
    reflection_power_node.operation = "POWER"
    reflection_power_node.inputs[1].default_value = reflection_power
    reflection_gain_node = nodes.new("ShaderNodeMath")
    reflection_gain_node.name = "W3 Particle Reflection Gain"
    reflection_gain_node.operation = "MULTIPLY"
    reflection_gain_node.inputs[1].default_value = reflection_gain
    refraction_gain_node = nodes.new("ShaderNodeMath")
    refraction_gain_node.name = "W3 Particle Refraction Gain"
    refraction_gain_node.operation = "ADD"
    refraction_gain_node.inputs[1].default_value = refraction_gain
    detail_base = nodes.new("ShaderNodeMath")
    detail_base.name = "W3 Particle Detail Base"
    detail_base.operation = "ADD"
    detail_base.inputs[1].default_value = 0.15
    detail_limit = nodes.new("ShaderNodeMath")
    detail_limit.name = "W3 Particle Detail Limit"
    detail_limit.operation = "MINIMUM"
    detail_limit.inputs[1].default_value = 0.8
    links.new(normal_map.outputs["Normal"], fresnel.inputs["Normal"])
    links.new(fresnel.outputs["Fac"], reflection_power_node.inputs[0])
    links.new(reflection_power_node.outputs[0], reflection_gain_node.inputs[0])
    links.new(reflection_gain_node.outputs[0], refraction_gain_node.inputs[0])
    links.new(refraction_gain_node.outputs[0], detail_base.inputs[0])
    links.new(detail_base.outputs[0], detail_limit.inputs[0])

    alpha_source = object_info.outputs["Alpha"]
    if image is not None:
        texture = _particle_texture_node(settings, nodes, links, object_info, image)
        links.new(texture.outputs["Color"], normal_map.inputs["Color"])
        texture_alpha = nodes.new("ShaderNodeMath")
        texture_alpha.name = "W3 Particle Texture Alpha"
        texture_alpha.operation = "MULTIPLY"
        texture_alpha.use_clamp = True
        links.new(texture.outputs["Alpha"], texture_alpha.inputs[0])
        links.new(object_info.outputs["Alpha"], texture_alpha.inputs[1])
        alpha_source = texture_alpha.outputs[0]

    alpha_gain = nodes.new("ShaderNodeMath")
    alpha_gain.name = "W3 Particle Alpha Multiplier"
    alpha_gain.operation = "MULTIPLY"
    alpha_gain.use_clamp = True
    alpha_gain.inputs[1].default_value = alpha_multiplier
    strength = nodes.new("ShaderNodeMath")
    strength.name = "W3 Particle Additive Strength"
    strength.operation = "MULTIPLY"
    preview_gain_node = nodes.new("ShaderNodeMath")
    preview_gain_node.name = "W3 Particle Preview Gain"
    preview_gain_node.operation = "MULTIPLY"
    preview_gain_node.inputs[1].default_value = preview_gain
    links.new(alpha_source, alpha_gain.inputs[0])
    links.new(alpha_gain.outputs[0], strength.inputs[0])
    links.new(detail_limit.outputs[0], strength.inputs[1])
    links.new(strength.outputs[0], preview_gain_node.inputs[0])
    links.new(preview_gain_node.outputs[0], emission.inputs["Strength"])
    links.new(transparent.outputs[0], add.inputs[0])
    links.new(emission.outputs[0], add.inputs[1])
    links.new(add.outputs[0], output.inputs["Surface"])

    if hasattr(material, "surface_render_method"):
        material.surface_render_method = "BLENDED"
    elif hasattr(material, "blend_method"):
        material.blend_method = "BLEND"
    if hasattr(material, "use_transparency_overlap"):
        # Avoid transparent-card depth occlusion in Eevee.
        material.use_transparency_overlap = True
    if hasattr(material, "use_transparent_shadow"):
        material.use_transparent_shadow = False
    return material


def _settings_parameter(settings, *names, default=None):
    parameters = settings.material_parameters
    if not isinstance(parameters, Mapping):
        return default
    lowered = {str(key).lower(): value for key, value in parameters.items()}
    return next((lowered[name.lower()] for name in names if name.lower() in lowered), default)


def _particle_color(settings, default, names=None):
    names = names or (
        "color_multiply",
        "color",
        "colour",
        "glow_color",
    )
    value = _settings_parameter(
        settings,
        *names,
        default=default,
    )
    if isinstance(value, Mapping):
        if any(key in value for key in ("Red", "Green", "Blue", "Alpha")):
            values = tuple(float(value.get(name, fallback)) for name, fallback in (
                ("Red", 255.0), ("Green", 255.0), ("Blue", 255.0), ("Alpha", 255.0),
            ))
        else:
            values = _vector(value, default)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        values = tuple(float(item) for item in value)
    else:
        values = tuple(float(item) for item in default)
    values = values + tuple(default[len(values):])
    if max(values[:4], default=1.0) > 1.0:
        values = tuple(item / 255.0 for item in values)
    return tuple(max(0.0, item) for item in values[:4])


def _generic_particle_material(settings, image, key):
    normalized_path = settings.material_path.replace("/", "\\").lower()
    defaults = _MATERIAL_DEFAULTS.get(normalized_path, {})
    packed_fire = normalized_path.endswith(r"\fire_glow.w2mg")
    style = "alpha" if "alpha_blend" in normalized_path else "additive"
    tint = _particle_color(settings, defaults.get("color", (1.0, 1.0, 1.0, 1.0)))
    strength_value = _settings_parameter(
        settings,
        "color_multiply_value",
        "color_multiplier",
        "glow_multiplier",
        default=defaults.get("strength", 1.0),
    )
    try:
        strength_value = max(0.0, float(strength_value))
    except (TypeError, ValueError):
        strength_value = float(defaults.get("strength", 1.0))
    alpha_value = max(0.0, _material_float(settings, "alpha_value", 1.0))
    alpha_multiplier = max(0.0, _material_float(settings, "alpha_multiplier", 1.0))

    material = bpy.data.materials.new(f"Witcher Particle {settings.name}")
    material[PARTICLE_MATERIAL_PROP] = key
    material["witcher_particle_preview_version"] = PARTICLE_PREVIEW_VERSION
    material["witcher_source_material"] = settings.material_path
    material["witcher_source_texture"] = settings.texture_path
    material["witcher_particle_texture_loaded"] = bool(image)
    material["witcher_particle_atlas_width"] = settings.atlas_width
    material["witcher_particle_atlas_height"] = settings.atlas_height
    material["witcher_particle_material_style"] = style
    material["witcher_particle_packed_channels"] = "R=color,G=glow" if packed_fire else ""
    material["witcher_particle_color_strength"] = strength_value
    material["witcher_particle_alpha_multiplier"] = alpha_value * alpha_multiplier
    material.use_nodes = True
    material.diffuse_color = tint
    material.use_backface_culling = False

    nodes = material.node_tree.nodes
    links = material.node_tree.links
    nodes.clear()
    output = nodes.new("ShaderNodeOutputMaterial")
    transparent = nodes.new("ShaderNodeBsdfTransparent")
    emission = nodes.new("ShaderNodeEmission")
    emission.name = "W3 Particle Emission"
    object_info = nodes.new("ShaderNodeObjectInfo")
    object_info.name = "W3 Particle Object Info"
    tint_node = nodes.new("ShaderNodeRGB")
    tint_node.name = "W3 Particle Tint"
    tint_node.outputs[0].default_value = tint

    alpha_source = object_info.outputs["Alpha"]
    color_source = tint_node.outputs[0]
    if image is not None:
        texture = _particle_texture_node(settings, nodes, links, object_info, image)
        if packed_fire:
            # fire_glow packs R=color and G=glow masks; alpha and blue are not display channels.
            separate, red = _separate_color_node(nodes)
            separate.name = "W3 Particle Fire Channels"
            green = separate.outputs.get("Green") or separate.outputs.get("G")
            links.new(texture.outputs["Color"], separate.inputs[0])

            def parameter_float(name, default):
                try:
                    return float(_settings_parameter(settings, name, default=default))
                except (TypeError, ValueError):
                    return float(default)

            color_multiplier = parameter_float("color_multiplier", 1.0)
            glow_multiplier = parameter_float("glow_multiplier", 1.0)
            glow_tint = _particle_color(
                settings,
                (1.0, 1.0, 1.0, 1.0),
                ("glow_color",),
            )
            tint_node.outputs[0].default_value = tuple(
                value * color_multiplier for value in tint
            )
            glow_tint_node = nodes.new("ShaderNodeRGB")
            glow_tint_node.name = "W3 Particle Fire Glow Tint"
            glow_tint_node.outputs[0].default_value = tuple(
                value * glow_multiplier for value in glow_tint
            )
            red_color = nodes.new("ShaderNodeMixRGB")
            red_color.name = "W3 Particle Fire Red"
            red_color.blend_type = "MULTIPLY"
            red_color.inputs[0].default_value = 1.0
            links.new(tint_node.outputs[0], red_color.inputs[1])
            links.new(red, red_color.inputs[2])
            green_color = nodes.new("ShaderNodeMixRGB")
            green_color.name = "W3 Particle Fire Green"
            green_color.blend_type = "MULTIPLY"
            green_color.inputs[0].default_value = 1.0
            links.new(glow_tint_node.outputs[0], green_color.inputs[1])
            links.new(green, green_color.inputs[2])
            add_color = nodes.new("ShaderNodeMixRGB")
            add_color.name = "W3 Particle Fire Color"
            add_color.blend_type = "ADD"
            add_color.inputs[0].default_value = 1.0
            links.new(red_color.outputs[0], add_color.inputs[1])
            links.new(green_color.outputs[0], add_color.inputs[2])
            channel_sum = nodes.new("ShaderNodeMath")
            channel_sum.name = "W3 Particle Fire Channel Mask"
            channel_sum.operation = "ADD"
            channel_sum.use_clamp = True
            links.new(red, channel_sum.inputs[0])
            links.new(green, channel_sum.inputs[1])
            vertex_sum = nodes.new("ShaderNodeMath")
            vertex_sum.name = "W3 Particle Fire Vertex Fade"
            vertex_sum.operation = "ADD"
            links.new(channel_sum.outputs[0], vertex_sum.inputs[0])
            links.new(object_info.outputs["Alpha"], vertex_sum.inputs[1])
            silhouette = nodes.new("ShaderNodeMath")
            silhouette.name = "W3 Particle Fire Silhouette"
            silhouette.operation = "SUBTRACT"
            silhouette.inputs[1].default_value = 1.0
            silhouette.use_clamp = True
            links.new(vertex_sum.outputs[0], silhouette.inputs[0])
            masked_color = nodes.new("ShaderNodeMixRGB")
            masked_color.name = "W3 Particle Fire Masked Color"
            masked_color.blend_type = "MULTIPLY"
            masked_color.inputs[0].default_value = 1.0
            links.new(add_color.outputs[0], masked_color.inputs[1])
            links.new(silhouette.outputs[0], masked_color.inputs[2])
            gamma = nodes.new("ShaderNodeGamma")
            gamma.name = "W3 Particle Fire Gamma To Linear"
            gamma.inputs["Gamma"].default_value = 2.2
            links.new(masked_color.outputs[0], gamma.inputs["Color"])
            color_source = gamma.outputs["Color"]
            full_strength = nodes.new("ShaderNodeValue")
            full_strength.name = "W3 Particle Fire Full Strength"
            full_strength.outputs[0].default_value = 1.0
            alpha_source = full_strength.outputs[0]
        else:
            multiply_color = nodes.new("ShaderNodeMixRGB")
            multiply_color.name = "W3 Particle Texture Tint"
            multiply_color.blend_type = "MULTIPLY"
            multiply_color.inputs[0].default_value = 1.0
            links.new(texture.outputs["Color"], multiply_color.inputs[1])
            links.new(tint_node.outputs[0], multiply_color.inputs[2])
            color_source = multiply_color.outputs[0]
            texture_alpha = nodes.new("ShaderNodeMath")
            texture_alpha.name = "W3 Particle Texture Alpha"
            texture_alpha.operation = "MULTIPLY"
            texture_alpha.use_clamp = True
            links.new(texture.outputs["Alpha"], texture_alpha.inputs[0])
            links.new(object_info.outputs["Alpha"], texture_alpha.inputs[1])
            alpha_source = texture_alpha.outputs[0]

    alpha_gain = nodes.new("ShaderNodeMath")
    alpha_gain.name = "W3 Particle Alpha Multiplier"
    alpha_gain.operation = "MULTIPLY"
    alpha_gain.use_clamp = True
    alpha_gain.inputs[1].default_value = (
        1.0 if packed_fire else alpha_value * alpha_multiplier
    )
    links.new(alpha_source, alpha_gain.inputs[0])
    links.new(color_source, emission.inputs["Color"])

    if style == "alpha":
        emission.inputs["Strength"].default_value = strength_value
        blend = nodes.new("ShaderNodeMixShader")
        blend.name = "W3 Particle Alpha Blend"
        links.new(alpha_gain.outputs[0], blend.inputs[0])
        links.new(transparent.outputs[0], blend.inputs[1])
        links.new(emission.outputs[0], blend.inputs[2])
        links.new(blend.outputs[0], output.inputs["Surface"])
    else:
        strength = nodes.new("ShaderNodeMath")
        strength.name = "W3 Particle Additive Strength"
        strength.operation = "MULTIPLY"
        strength.inputs[1].default_value = strength_value
        links.new(alpha_gain.outputs[0], strength.inputs[0])
        links.new(strength.outputs[0], emission.inputs["Strength"])
        add = nodes.new("ShaderNodeAddShader")
        add.name = "W3 Particle Additive Approximation"
        links.new(transparent.outputs[0], add.inputs[0])
        links.new(emission.outputs[0], add.inputs[1])
        links.new(add.outputs[0], output.inputs["Surface"])

    if hasattr(material, "surface_render_method"):
        material.surface_render_method = "BLENDED"
    elif hasattr(material, "blend_method"):
        material.blend_method = "BLEND"
    if hasattr(material, "use_transparency_overlap"):
        material.use_transparency_overlap = True
    if hasattr(material, "use_transparent_shadow"):
        material.use_transparent_shadow = False
    return material


def _particle_material(settings, image_loader):
    image = image_loader(settings.texture_path) if settings.texture_path else None
    key = _material_key(settings, image is not None)
    existing = next(
        (material for material in bpy.data.materials if material.get(PARTICLE_MATERIAL_PROP) == key),
        None,
    )
    if existing is not None:
        return existing
    if settings.material_path.replace("/", "\\").lower() == _WATER_SPLASH_MATERIAL.lower():
        return _water_particle_material(settings, image, key)
    return _generic_particle_material(settings, image, key)


def _particle_mesh(drawer, material):
    horizontal = drawer == "CParticleDrawerEmitterOrientation"
    transpose_uv = drawer in {"CParticleDrawerMotionBlur", "CParticleDrawerRain"}
    shape = "horizontal" if horizontal else "motion" if transpose_uv else "billboard"
    key = f"v{PARTICLE_PREVIEW_VERSION}|{shape}|{material.get(PARTICLE_MATERIAL_PROP, material.name)}"
    mesh = next((item for item in bpy.data.meshes if item.get(PARTICLE_MESH_PROP) == key), None)
    if mesh is not None:
        return mesh
    mesh = bpy.data.meshes.new(f"Witcher Particle {shape.title()} Quad")
    mesh[PARTICLE_MESH_PROP] = key
    vertices = ((-1.0, -1.0, 0.0), (1.0, -1.0, 0.0), (1.0, 1.0, 0.0), (-1.0, 1.0, 0.0))
    faces = ((0, 1, 2, 3),)
    mesh.from_pydata(vertices, [], faces)
    uv_layer = mesh.uv_layers.new(name="UVMap")
    coordinates = (
        # Convert REDengine's transposed DirectX UVs to Blender orientation.
        ((0.0, 1.0), (0.0, 0.0), (1.0, 0.0), (1.0, 1.0))
        if transpose_uv
        else ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
    )
    for polygon in mesh.polygons:
        for index, loop_index in enumerate(polygon.loop_indices):
            uv_layer.data[loop_index].uv = coordinates[index]
    mesh.materials.append(material)
    mesh.update()
    return mesh


def _link_object(obj, target_collection):
    collection = target_collection or getattr(bpy.context, "collection", None) or bpy.context.scene.collection
    collection.objects.link(obj)


def _active_view_rotation(scene=None):
    for window in getattr(bpy.context.window_manager, "windows", ()):
        if scene is not None and window.scene != scene:
            continue
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                space = area.spaces.active
                region_3d = getattr(space, "region_3d", None)
                if region_3d is not None:
                    if region_3d.view_perspective == "CAMERA":
                        camera = getattr(space, "camera", None) or window.scene.camera
                        if camera is not None:
                            return camera.matrix_world.to_quaternion().normalized()
                    return region_3d.view_rotation.copy().normalized()
    return None


def _set_view_root_orientation(root, world_orientation):
    world_orientation.normalize()
    # Avoid depsgraph updates for equivalent q/-q rotations.
    if abs(root.rotation_quaternion.dot(world_orientation)) > 1.0 - 1e-7:
        return False
    root.rotation_quaternion = world_orientation
    root.update_tag(refresh={"OBJECT"})
    return True


def _view_orientation(scene, *, render=False):
    if render and scene.camera is not None:
        return scene.camera.matrix_world.to_quaternion()
    orientation = _active_view_rotation(scene)
    if orientation is None and scene.camera is not None:
        orientation = scene.camera.matrix_world.to_quaternion()
    return orientation


def _update_view_root(root, scene, *, render=False):
    orientation = _view_orientation(scene, render=render)
    if orientation is None:
        orientation = Quaternion((1.0, 0.0, 0.0), pi / 2.0)
    _set_view_root_orientation(root, orientation)


def _scene_view_root(scene, *, create=False):
    root = bpy.data.objects.get(str(scene.get(_BILLBOARD_ROOT_PROP, "")))
    if root is not None and root.get(_BILLBOARD_ROOT_PROP):
        return root
    if not create:
        return None
    root = bpy.data.objects.new(_BILLBOARD_ROOT_NAME, None)
    scene.collection.objects.link(root)
    root.rotation_mode = "QUATERNION"
    root.empty_display_size = 0.001
    root.hide_render = True
    root.hide_select = True
    root[_BILLBOARD_ROOT_PROP] = True
    scene[_BILLBOARD_ROOT_PROP] = root.name
    _update_view_root(root, scene)
    return root


def _constrain_billboard(owner, target):
    constraint = owner.constraints.get(_BILLBOARD_CONSTRAINT_NAME)
    if constraint is None or constraint.type != "COPY_ROTATION":
        constraint = owner.constraints.new("COPY_ROTATION")
        constraint.name = _BILLBOARD_CONSTRAINT_NAME
    constraint.target = target
    return constraint


def _billboard_bases():
    objects = getattr(bpy.data, "objects", None)
    if objects is None:
        return []
    return [obj for obj in objects if obj.get(PARTICLE_BILLBOARD_BASIS_PROP)]


def _update_billboard_basis(basis, scene, *, render=False, view_rotation=None):
    constraint = basis.constraints.get(_BILLBOARD_CONSTRAINT_NAME)
    target = getattr(constraint, "target", None)
    if target is None or not target.get(_BILLBOARD_ROOT_PROP):
        return
    if view_rotation is not None and not render:
        _set_view_root_orientation(target, view_rotation)
    else:
        _update_view_root(target, scene, render=render)


_BILLBOARD_MIGRATED = False


def _migrate_legacy_billboards():
    legacy = [basis for basis in _billboard_bases()
              if basis.constraints.get(_BILLBOARD_CONSTRAINT_NAME) is None]
    if not legacy:
        return
    scene_names = [(scene, set(scene.objects.keys())) for scene in bpy.data.scenes]
    for basis in legacy:
        scene = next((s for s, names in scene_names if basis.name in names), None)
        if scene is None:
            continue
        target = None
        if str(basis.get(_BILLBOARD_MODE_PROP, "")) == "live_target":
            target = bpy.data.objects.get(str(basis.get(_BILLBOARD_TARGET_PROP, "")))
        if target is None:
            target = _scene_view_root(scene, create=True)
        _constrain_billboard(basis, target)


_BILLBOARD_MOVING_SECONDS = 0.2
_BILLBOARD_FOLLOW_STATE = {}


def _follow_particle_viewports():
    global _BILLBOARD_MIGRATED
    if not _BILLBOARD_MIGRATED:
        _BILLBOARD_MIGRATED = True
        _migrate_legacy_billboards()
    interval = None
    now = time.monotonic()
    for scene in bpy.data.scenes:
        root = _scene_view_root(scene)
        if root is None:
            continue
        interval = _BILLBOARD_UPDATE_INTERVAL
        orientation = _view_orientation(scene)
        if orientation is None:
            orientation = Quaternion((1.0, 0.0, 0.0), pi / 2.0)
        orientation.normalize()
        state = _BILLBOARD_FOLLOW_STATE.setdefault(scene.name, [None, 0.0])
        last_seen, last_push = state
        moving = last_seen is not None and abs(last_seen.dot(orientation)) < 1.0 - 1e-7
        state[0] = orientation
        # Throttle depsgraph updates while navigating.
        if moving and now - last_push < _BILLBOARD_MOVING_SECONDS:
            continue
        if _set_view_root_orientation(root, orientation):
            state[1] = now
    return interval


def _restore_particle_billboards(scene, *_args):
    root = _scene_view_root(scene)
    if root is not None:
        _update_view_root(root, scene, render=True)


def _ensure_particle_preview_runtime():
    if _restore_particle_billboards not in bpy.app.handlers.render_pre:
        bpy.app.handlers.render_pre.append(_restore_particle_billboards)
    if not bpy.app.background and not bpy.app.timers.is_registered(_follow_particle_viewports):
        bpy.app.timers.register(_follow_particle_viewports, first_interval=_BILLBOARD_UPDATE_INTERVAL)


@persistent
def _resume_particle_billboards(_unused):
    global _BILLBOARD_MIGRATED
    _BILLBOARD_MIGRATED = False
    _BILLBOARD_FOLLOW_STATE.clear()
    if _billboard_bases():
        _ensure_particle_preview_runtime()


def register_particle_preview_runtime():
    if _resume_particle_billboards not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_resume_particle_billboards)
    _ensure_particle_preview_runtime()


def unregister_particle_preview_runtime():
    if bpy.app.timers.is_registered(_follow_particle_viewports):
        bpy.app.timers.unregister(_follow_particle_viewports)
    for handlers, callback in (
        (bpy.app.handlers.load_post, _resume_particle_billboards),
        (bpy.app.handlers.render_pre, _restore_particle_billboards),
    ):
        while callback in handlers:
            handlers.remove(callback)


def _create_billboard_basis(parent, target_collection, billboard_target, scene):
    basis = bpy.data.objects.new("Witcher Particle View Basis", None)
    _link_object(basis, target_collection)
    basis.parent = parent
    basis.rotation_mode = "QUATERNION"
    basis.empty_display_size = 0.001
    basis.hide_render = True
    basis.hide_select = True
    basis[PARTICLE_BILLBOARD_BASIS_PROP] = True
    if billboard_target is not None:
        mode = "live_target"
        basis[_BILLBOARD_TARGET_PROP] = billboard_target.name
        _constrain_billboard(basis, billboard_target)
    else:
        if _active_view_rotation(scene) is not None:
            mode = "live_viewport"
        elif scene.camera is not None:
            mode = "live_camera"
        else:
            mode = "live_fallback"
        _constrain_billboard(basis, _scene_view_root(scene, create=True))
        _ensure_particle_preview_runtime()
    basis[_BILLBOARD_MODE_PROP] = mode
    return basis, mode


def _action_fcurves(action):
    legacy = getattr(action, "fcurves", None)
    if legacy is not None:
        return list(legacy)
    curves = []
    for layer in getattr(action, "layers", ()) or ():
        for strip in getattr(layer, "strips", ()) or ():
            for channelbag in getattr(strip, "channelbags", ()) or ():
                curves.extend(channelbag.fcurves)
    return curves


def _finish_action(obj, start_frame, end_frame):
    action = getattr(getattr(obj, "animation_data", None), "action", None)
    if action is None:
        return
    action.name = f"{obj.name} Lifecycle"
    if hasattr(action, "use_cyclic"):
        action.use_cyclic = True
    if hasattr(action, "use_frame_range"):
        action.use_frame_range = True
        action.frame_start = float(start_frame)
        action.frame_end = float(end_frame)
    for fcurve in _action_fcurves(action):
        points = fcurve.keyframe_points
        for index, keyframe in enumerate(points):
            keyframe.interpolation = "LINEAR"
            if (
                fcurve.data_path == "location"
                and index + 1 < len(points)
                and float(points[index + 1].co.x - keyframe.co.x) <= _RESPAWN_GAP_FRAMES + 1e-4
            ):
                # Do not interpolate across the respawn jump.
                keyframe.interpolation = "CONSTANT"
        if not any(modifier.type == "CYCLES" for modifier in fcurve.modifiers):
            fcurve.modifiers.new("CYCLES")


def _size_factor(settings, normalized_life):
    value = _evaluate(settings.size_evaluator, normalized_life, (1.0, 1.0, 1.0, 1.0))
    vector = _vector(value, (1.0, 1.0, 1.0, 1.0))
    return vector[0], vector[1]


def _sphere_spawn_offset(settings, rng):
    if settings.spawn_sphere_outer <= 0.0:
        return [0.0, 0.0, 0.0]
    radius = (
        settings.spawn_sphere_outer
        if settings.spawn_sphere_surface
        else rng.uniform(settings.spawn_sphere_inner, settings.spawn_sphere_outer)
    )
    vertical = rng.uniform(-1.0, 1.0)
    azimuth = rng.uniform(0.0, 2.0 * pi)
    horizontal = sqrt(max(0.0, 1.0 - vertical * vertical))
    delta = [
        radius * horizontal * cos(azimuth),
        radius * horizontal * sin(azimuth),
        radius * vertical,
    ]
    signs = {(False, False): 0.0, (True, False): 1.0, (False, True): -1.0}
    for axis, allowed in enumerate(zip(settings.spawn_sphere_axes[::2], settings.spawn_sphere_axes[1::2])):
        if allowed in signs:
            delta[axis] = signs[allowed] * abs(delta[axis])
    return delta


def _generation(settings, rng):
    angle = rng.uniform(0.0, 2.0 * pi)
    spawn = [
        settings.spawn_position[0] + cos(angle) * settings.spawn_radius,
        settings.spawn_position[1] + sin(angle) * settings.spawn_radius,
        settings.spawn_position[2],
    ]
    for axis, extent in enumerate(settings.spawn_box_extents):
        spawn[axis] += rng.uniform(-extent, extent)

    sphere_delta = _sphere_spawn_offset(settings, rng)
    spawn = [value + delta for value, delta in zip(spawn, sphere_delta)]

    velocity = [
        rng.uniform(settings.velocity_min[axis], settings.velocity_max[axis])
        for axis in range(3)
    ]
    if settings.spawn_sphere_velocity:
        velocity = [
            value + delta * settings.spawn_sphere_force
            for value, delta in zip(velocity, sphere_delta)
        ]

    base_size_x = rng.uniform(settings.base_size_min[0], settings.base_size_max[0])
    base_size_y = (
        base_size_x
        if settings.size_axes <= 1 and settings.size_spill
        else rng.uniform(settings.base_size_min[1], settings.base_size_max[1])
    )
    return {
        "angle": angle,
        "spawn_position": spawn,
        "base_size": [base_size_x, base_size_y],
        "velocity": velocity,
        # REDengine stores these values in turns.
        "initial_rotation": _random_scalar(settings.initial_rotation, rng, default=0.0) * 2.0 * pi,
        "rotation_rate": _random_scalar(settings.rotation_rate, rng, default=0.0) * 2.0 * pi,
        "initial_frame": _random_scalar(settings.initial_frame, rng, default=0.0),
    }


def _particle_position(settings, generation, normalized):
    age = normalized * settings.lifetime
    start = generation["spawn_position"]
    velocity = generation["velocity"]
    if age <= 0.0:
        return tuple(start)
    if settings.velocity_evaluator is None:
        return tuple(
            start[axis]
            + velocity[axis] * age
            + 0.5 * settings.acceleration[axis] * age * age
            for axis in range(3)
        )

    # Approximate REDengine's per-tick velocity modifier with midpoint integration.
    steps = max(2, min(32, int(ceil(age * 20.0))))
    delta_time = age / steps
    displacement = [0.0, 0.0, 0.0]
    for step in range(steps):
        sample_age = (step + 0.5) * delta_time
        sample_life = sample_age / settings.lifetime
        value = _evaluate(settings.velocity_evaluator, sample_life, (1.0, 1.0, 1.0, 1.0))
        factor = _vector(value, (1.0, 1.0, 1.0, 1.0))
        base_velocity = [
            velocity[axis] + settings.acceleration[axis] * sample_age
            for axis in range(3)
        ]
        current_velocity = (
            [base_velocity[axis] * factor[axis] for axis in range(3)]
            if settings.velocity_modulate
            else list(factor[:3])
        )
        for axis in range(3):
            displacement[axis] += current_velocity[axis] * delta_time
    return tuple(start[axis] + displacement[axis] for axis in range(3))


def _atlas_frame(settings, generation, normalized):
    total_frames = settings.atlas_width * settings.atlas_height
    if total_frames <= 1:
        return 0.0
    progress = normalized if "lifetime" in settings.animation_mode.lower() else normalized * settings.lifetime
    return generation["initial_frame"] + settings.animation_speed * progress


def _keyframe_particle_sample(obj, settings, generation, base_orientation, normalized, frame):
    age = normalized * settings.lifetime
    size_x, size_y = _size_factor(settings, normalized)
    if settings.size_modulate:
        size_x *= generation["base_size"][0]
        size_y *= generation["base_size"][1]
    obj.scale = (size_x, size_y, 1.0)
    obj.keyframe_insert(data_path="scale", frame=frame, group="Particle Lifecycle")

    obj.location = _particle_position(settings, generation, normalized)
    obj.keyframe_insert(data_path="location", frame=frame, group="Particle Lifecycle")

    alpha = max(0.0, _scalar(settings.alpha_evaluator, normalized, 1.0))
    obj.color[3] = alpha
    obj.keyframe_insert(data_path="color", index=3, frame=frame, group="Particle Lifecycle")
    if settings.atlas_width * settings.atlas_height > 1:
        obj.color[0] = float(_atlas_frame(settings, generation, normalized))
        obj.keyframe_insert(data_path="color", index=0, frame=frame, group="Particle Lifecycle")

    roll = generation["initial_rotation"] + generation["rotation_rate"] * age
    if settings.drawer in {
        "CParticleDrawerBillboard",
        "CParticleDrawerSphereAligned",
        "CParticleDrawerVerticalFixed",
    }:
        roll = pi / 2.0 - roll
    obj.rotation_quaternion = (
        base_orientation @ Quaternion((0.0, 0.0, 1.0), roll)
    ).normalized()
    obj.keyframe_insert(data_path="rotation_quaternion", frame=frame, group="Particle Lifecycle")


def _keyframe_generation(obj, settings, generation, base_orientation, birth_frame, duration_frames, steps):
    obj.color = (
        float(_atlas_frame(settings, generation, 0.0)),
        1.0,
        1.0,
        1.0,
    )
    # Keep respawn keys separate in Blender.
    end_epsilon = min(_RESPAWN_GAP_FRAMES, duration_frames * 0.01)
    for step in range(steps + 1):
        normalized = step / steps
        frame = birth_frame + normalized * (duration_frames - end_epsilon)
        _keyframe_particle_sample(obj, settings, generation, base_orientation, normalized, frame)


def _animate_particle(obj, settings, rng, index, frame_start, fps, base_orientation):
    count = settings.active_count
    phase = (index + 0.5) / count
    duration_frames = settings.lifetime * fps
    first_frame = float(frame_start) - phase * duration_frames
    steps = max(2, min(15, int(ceil(duration_frames))))
    generations = [_generation(settings, rng) for _ in range(PARTICLE_GENERATIONS)]
    obj.rotation_mode = "QUATERNION"
    for generation_index, generation in enumerate(generations):
        _keyframe_generation(
            obj,
            settings,
            generation,
            base_orientation,
            first_frame + generation_index * duration_frames,
            duration_frames,
            steps,
        )

    cycle_end = first_frame + PARTICLE_GENERATIONS * duration_frames
    first_generation = generations[0]
    obj.color = (
        float(_atlas_frame(settings, first_generation, 0.0)),
        1.0,
        1.0,
        1.0,
    )
    _keyframe_particle_sample(obj, settings, first_generation, base_orientation, 0.0, cycle_end)

    _finish_action(obj, first_frame, cycle_end)
    obj["witcher_particle_phase"] = phase
    obj["witcher_particle_spawn_angle"] = first_generation["angle"]
    obj["witcher_particle_base_size"] = first_generation["base_size"][0]
    obj["witcher_particle_base_size_xy"] = first_generation["base_size"]
    obj["witcher_particle_velocity"] = first_generation["velocity"]
    obj["witcher_particle_velocity_z"] = first_generation["velocity"][2]
    obj["witcher_particle_initial_frame"] = first_generation["initial_frame"]
    obj["witcher_particle_generation_count"] = PARTICLE_GENERATIONS
    obj["witcher_particle_action_period_frames"] = PARTICLE_GENERATIONS * duration_frames
    obj["witcher_particle_generations"] = json.dumps(generations, sort_keys=True)


def _resolve_source(source):
    if not isinstance(source, (str, os.PathLike)):
        source_path = str(_get(source, "source_path", "") or "")
        return source, source_path, source_path
    path = os.fspath(source)
    resolved = path
    if not os.path.isfile(resolved):
        try:
            from ..CR2W.common_blender import (
                _get_redkit_depot_roots,
                redkit_repo_context,
                repo_file,
                win_path_isfile,
            )

            resolved = repo_file(path) or path
            if not win_path_isfile(resolved):
                # Let configured REDkit depots fill gaps without overriding the normal uncook.
                roots = _get_redkit_depot_roots()
                if roots:
                    with redkit_repo_context(roots=roots):
                        redkit_path = repo_file(path)
                    if redkit_path and win_path_isfile(redkit_path):
                        resolved = redkit_path
        except Exception:
            pass
    from ..CR2W.dc_particle import load_bin_particle

    system = load_bin_particle(resolved)
    try:
        from ..CR2W.common_blender import _get_redkit_depot_roots, get_repo_override_state, win_path_isfile

        override_roots, _read_only = get_repo_override_state()
        roots = list(dict.fromkeys([*_get_redkit_depot_roots(), *override_roots]))
        module_count = sum(len(emitter.modules) for emitter in system.emitters)
        if not os.path.isabs(path):
            resolved_key = os.path.normcase(resolved)
            best_rank = next(
                (
                    rank for rank, root in enumerate(roots)
                    if os.path.normcase(os.path.join(root, path)) == resolved_key
                ),
                len(roots),
            )
            for rank, root in enumerate(roots):
                redkit_path = os.path.join(root, path)
                if not win_path_isfile(redkit_path) or os.path.normcase(redkit_path) == resolved_key:
                    continue
                redkit_system = load_bin_particle(redkit_path)
                redkit_module_count = sum(len(emitter.modules) for emitter in redkit_system.emitters)
                if redkit_module_count > module_count or (
                    redkit_module_count == module_count and rank < best_rank
                ):
                    system, resolved = redkit_system, redkit_path
                    module_count, best_rank = redkit_module_count, rank
    except Exception:
        log.debug("Could not resolve a richer REDkit W2P source for %s", path, exc_info=True)
    return system, path, resolved


def import_particle_system(
    source,
    *,
    parent=None,
    target_collection=None,
    frame_start=1.0,
    fps=None,
    seed=None,
    image_loader=None,
    billboard_target=None,
):
    """Create a deterministic preview and return its particle mesh objects."""
    system, source_path, context_path = _resolve_source(source)
    scene = bpy.context.scene
    if fps is None:
        fps = float(scene.render.fps) / float(scene.render.fps_base or 1.0)
    fps = max(1.0, float(fps))
    image_loader = image_loader or _load_particle_image
    if context_path and os.path.isabs(context_path):
        from ..CR2W.common_blender import redkit_repo_context

        source_image_loader = image_loader

        def image_loader(texture_path):
            with redkit_repo_context(context_path):
                return source_image_loader(texture_path)
    created = []

    for emitter in (_get(system, "emitters", ()) or ()):
        settings = _settings(emitter)
        if settings.active_count <= 0:
            continue
        material = _particle_material(settings, image_loader)
        horizontal = settings.drawer == "CParticleDrawerEmitterOrientation"
        billboard_basis, billboard_mode = (
            (None, "horizontal")
            if horizontal
            else _create_billboard_basis(parent, target_collection, billboard_target, scene)
        )
        mesh = _particle_mesh(settings.drawer, material)
        for index in range(settings.active_count):
            particle_seed = _stable_seed(source_path, settings.name, index, seed)
            rng = random.Random(particle_seed)
            obj = bpy.data.objects.new(f"{settings.name}_{index + 1:02d}", mesh)
            _link_object(obj, target_collection)
            obj.parent = parent
            obj[PARTICLE_OBJECT_PROP] = True
            obj["witcher_particle_preview_version"] = PARTICLE_PREVIEW_VERSION
            obj["witcher_particle_system"] = source_path
            obj["witcher_particle_emitter"] = settings.name
            obj["witcher_particle_drawer"] = settings.drawer
            obj["witcher_particle_material"] = settings.material_path
            obj["witcher_particle_texture"] = settings.texture_path
            obj["witcher_particle_lifetime"] = settings.lifetime
            obj["witcher_particle_birth_rate"] = settings.birth_rate
            obj["witcher_particle_burst_count"] = settings.burst_count
            obj["witcher_particle_burst_interval"] = settings.burst_interval
            obj["witcher_particle_active_count"] = settings.active_count
            obj["witcher_particle_max_count"] = settings.max_particles
            obj["witcher_particle_spawn_radius"] = settings.spawn_radius
            obj["witcher_particle_acceleration"] = settings.acceleration
            obj["witcher_particle_acceleration_z"] = settings.acceleration[2]
            obj["witcher_particle_atlas_width"] = settings.atlas_width
            obj["witcher_particle_atlas_height"] = settings.atlas_height
            obj["witcher_particle_seed"] = str(particle_seed)
            obj["witcher_particle_billboard_mode"] = billboard_mode
            _animate_particle(obj, settings, rng, index, float(frame_start), fps, Quaternion())
            if billboard_basis is not None:
                constraint = obj.constraints.new("COPY_ROTATION")
                constraint.name = _BILLBOARD_CONSTRAINT_NAME
                constraint.target = billboard_basis
                constraint.owner_space = "LOCAL" if parent is not None else "WORLD"
                constraint.target_space = "LOCAL" if parent is not None else "WORLD"
                constraint.mix_mode = "BEFORE"
            created.append(obj)
    return created


__all__ = [
    "import_particle_system",
    "register_particle_preview_runtime",
    "unregister_particle_preview_runtime",
]
