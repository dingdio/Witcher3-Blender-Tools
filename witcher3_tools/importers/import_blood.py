"""Transient world-space blood ribbon, sampled from a moving weapon helper."""
import math

import bpy
from mathutils import Vector

from . import import_particle as particles
from . import import_trail
from .import_trail import _curve_node

BLOOD_PREVIEW_VERSION = 1
_MODULES = {'CParticleInitializerSize', 'CParticleInitializerLifeTime',
            'CParticleModificatorAlphaOverLife', 'CParticleModificatorAcceleration'}


def _material(emitter, settings, start, rate, *, live=False, track=None, distances=(), times=()):
    if emitter.material.base_material.lower() == import_trail.RED_BLOOD_MATERIAL:
        duration = times[-1] + settings.lifetime
        return import_trail._material(emitter, settings, import_trail.material_parameters(emitter.material),
                                      start, duration, rate, distances, times,
                                      track or {'duration': duration}, live=live)
    parameters = emitter.material.parameters
    image = particles._load_particle_image(parameters['normal_and_splash'])
    if image is None:
        raise ValueError('The blood trail texture could not be loaded')
    mat = bpy.data.materials.new('Witcher Blood Trail')
    mat.use_nodes = True
    mat.surface_render_method = 'BLENDED'
    mat.use_transparency_overlap = True
    mat.use_transparent_shadow = False
    mat['witcher_blood_version'] = BLOOD_PREVIEW_VERSION
    mat['witcher_source_material'] = emitter.material.base_material
    mat['witcher_source_texture'] = parameters['normal_and_splash']
    # shortcut: Principled approximates water_splash lighting; no game cubemap/depth pass.
    mat['witcher_blood_limits'] = 'Blender lighting; no game cubemap reflection or depth fade'
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    nodes.clear()

    def math_node(op, a, b):
        node = nodes.new('ShaderNodeMath')
        node.operation = op
        for i, value in enumerate((a, b)):
            if isinstance(value, (float, int)):
                node.inputs[i].default_value = value
            else:
                links.new(value, node.inputs[i])
        return node.outputs[0]

    clock = nodes.new('ShaderNodeValue')
    clock.name = 'Blood Time'
    if not live:
        clock.outputs[0].driver_add('default_value').driver.expression = f'(frame-({start!r}))/{rate!r}'
    born = nodes.new('ShaderNodeAttribute')
    born.attribute_name = 'w3_trail_birth'
    age = math_node('SUBTRACT', clock.outputs[0], born.outputs['Fac'])
    alive = math_node('MULTIPLY', math_node('GREATER_THAN', age, 0),
                      math_node('LESS_THAN', age, settings.lifetime))
    normalized = math_node('DIVIDE', age, settings.lifetime)
    fade = _curve_node(nodes, links, 'Blood Age Fade', normalized,
                       [(i / 64, particles._scalar(settings.alpha_evaluator, i / 64, 1)) for i in range(65)])
    texture = nodes.new('ShaderNodeTexImage')
    texture.image = image
    alpha = math_node('MULTIPLY', texture.outputs['Alpha'], float(parameters.get('alpha_multiplier', 1)))
    alpha = math_node('MINIMUM', math_node('MULTIPLY', alpha, math_node('MULTIPLY', alive, fade)), 1)
    normal = nodes.new('ShaderNodeNormalMap')
    links.new(texture.outputs['Color'], normal.inputs['Color'])
    normal.inputs['Strength'].default_value = float(parameters.get('normal_multiplier', {}).get('W', 1))
    surface = nodes.new('ShaderNodeBsdfPrincipled')
    color = parameters.get('color', {})
    tint = tuple(float(color.get(k, 0)) / 255 for k in ('Red', 'Green', 'Blue'))
    surface.inputs['Base Color'].default_value = (*tint, 1)
    surface.inputs['Roughness'].default_value = math.sqrt(2 / (float(parameters.get('reflection_power_exponent', 10)) + 2))
    highlight = parameters.get('color_highlight', {})
    surface.inputs['Specular Tint'].default_value = tuple(float(highlight.get(k, 255)) / 255 for k in ('Red', 'Green', 'Blue')) + (1,)
    links.new(normal.outputs['Normal'], surface.inputs['Normal'])
    transparent = nodes.new('ShaderNodeBsdfTransparent')
    mix = nodes.new('ShaderNodeMixShader')
    links.new(alpha, mix.inputs[0])
    links.new(transparent.outputs[0], mix.inputs[1])
    links.new(surface.outputs[0], mix.inputs[2])
    output = nodes.new('ShaderNodeOutputMaterial')
    links.new(mix.outputs[0], output.inputs['Surface'])
    mat.diffuse_color = (*tint, 0.6)
    return mat


def blood_settings(emitter):
    if emitter.drawer_type != 'CParticleDrawerTrail' or emitter.keep_simulation_local:
        raise ValueError('Blood preview requires a world-space CParticleDrawerTrail')
    red = emitter.material and emitter.material.base_material.lower() == import_trail.RED_BLOOD_MATERIAL
    modules = _MODULES | import_trail._MODULES if red else _MODULES
    unsupported = [m.type_name for m in emitter.modules if m.enabled and m.type_name not in modules]
    if unsupported:
        raise ValueError('Unsupported blood modules: ' + ', '.join(unsupported))
    settings = particles._settings(emitter)
    if not settings.lod_enabled:
        return None
    lod = emitter.lods[0]
    if (emitter.loops != 1 or lod.bursts or lod.use_duration_range or lod.use_delay_range
            or lod.birth_rate.type_name != 'CEvaluatorFloatConst' or lod.duration <= 0):
        raise ValueError('Blood preview requires one constant-rate emitter cycle')
    life = particles._evaluator(particles._module(emitter, 'CParticleInitializerLifeTime'), 'lifeTime')
    if life is None or life.type_name != 'CEvaluatorFloatConst':
        raise ValueError('Blood preview requires constant particle lifetime')
    acceleration = particles._module(emitter, 'CParticleModificatorAcceleration')
    if acceleration and (not particles._property(acceleration, 'worldSpace', default=True)
                         or any(e.type_name not in ('CEvaluatorFloatConst', 'CEvaluatorVectorConst')
                                for e in acceleration.evaluators.values())):
        raise ValueError('Blood preview requires constant world-space acceleration')
    if not red and (not emitter.material or emitter.material.base_material.lower() != r'fx\shaders\water_splash.w2mg'):
        raise ValueError('Unsupported blood trail material')
    count = math.ceil(lod.duration * settings.birth_rate - 1e-6)
    if count < 2 or count > min(emitter.max_particles, 2000):
        raise ValueError('Blood emission must produce 2–2000 ribbon points within the particle limit')
    size = particles._evaluator(particles._module(emitter, 'CParticleInitializerSize'), 'size')
    if size is not None and 'Random' in size.type_name:
        raise ValueError('Random blood ribbon width is not supported')
    return settings


def bake_blood(scene, collection, emitter, matrix_at, start, track=None):
    """Play one authored emitter cycle at a simulated hit, then let particles die."""
    settings = blood_settings(emitter)
    if settings is None:
        return None
    lod = emitter.lods[0]
    count = math.ceil(lod.duration * settings.birth_rate - 1e-6)
    rate = scene.render.fps / scene.render.fps_base
    start += lod.delay * rate
    times = [i / settings.birth_rate for i in range(count)]
    duration = times[-1] + settings.lifetime
    size = particles._evaluator(particles._module(emitter, 'CParticleInitializerSize'), 'size')
    centers, axes, widths, distances = [], [], [], []
    distance = 0.0
    for time in times:
        matrix = matrix_at(start + time * rate)
        center = matrix.translation.copy()
        if centers:
            distance += (center - centers[-1]).length
        centers.append(center)
        axes.append((matrix.to_3x3() @ Vector((0, 0, 1))).normalized())
        widths.append(particles._vector(particles._evaluate(size, time / lod.duration, (1, 1, 1, 1)))[0])
        distances.append(distance)
    if distance < 1e-7:
        raise ValueError('The held weapon does not move during this blood effect')

    def vertices(time):
        result = []
        for birth, center, axis, width in zip(times, centers, axes, widths):
            age = max(0, min(settings.lifetime, time - birth))
            position = center + Vector(settings.acceleration) * (0.5 * age * age)
            result.extend((position + axis * width / 2, position - axis * width / 2))
        return result

    obj = mesh = material = None
    try:
        material = _material(emitter, settings, start, rate, track=track, distances=distances, times=times)
        mesh = bpy.data.meshes.new('Witcher Blood Trail')
        mesh.from_pydata(vertices(0), [], [(2*i, 2*i+1, 2*i+3, 2*i+2) for i in range(count-1)])
        mesh.materials.append(material)
        uv = mesh.uv_layers.new(name='Trail UV')
        for polygon in mesh.polygons:
            polygon.use_smooth = True
            for loop in polygon.loop_indices:
                vertex = mesh.loops[loop].vertex_index
                uv.data[loop].uv = (distances[vertex // 2] * float(emitter.drawer_properties.get('texturesPerUnit', 0.5)), vertex % 2)
        mesh.attributes.new('w3_trail_birth', 'FLOAT', 'POINT').data.foreach_set('value', [t for t in times for _ in range(2)])
        mesh.attributes.new('w3_trail_distance', 'FLOAT', 'POINT').data.foreach_set('value', [d for d in distances for _ in range(2)])
        obj = bpy.data.objects.new('Witcher Blood Trail', mesh)
        collection.objects.link(obj)
        obj.shape_key_add(name='Basis')
        steps = max(2, math.ceil(duration * max(60, rate * 2)))
        delta = duration * rate / steps
        for i in range(1, steps+1):
            key = obj.shape_key_add(name=f'Blood {i}')
            key.data.foreach_set('co', [v for point in vertices(duration * i / steps) for v in point])
            for frame, value in ((start+(i-1)*delta, 0), (start+i*delta, 1), (start+(i+1)*delta, 0)):
                key.value = value
                key.keyframe_insert('value', frame=frame)
        for curve in particles._action_fcurves(mesh.shape_keys.animation_data.action):
            for key in curve.keyframe_points:
                key.interpolation = 'LINEAR'
        end = start + duration * rate
        obj['witcher_trail_version'] = 1
        obj['witcher_blood_version'] = BLOOD_PREVIEW_VERSION
        obj['witcher_trail_kind'] = 'BLOOD'
        obj['witcher_trail_frame_start'] = start
        obj['witcher_trail_frame_end'] = end
        obj['witcher_trail_lifetime'] = settings.lifetime
        obj['witcher_trail_samples'] = count
        obj['witcher_blood_emission_seconds'] = lod.duration
        obj['witcher_blood_acceleration'] = settings.acceleration
        for frame, hidden in ((start-1, True), (start, False), (end, True)):
            obj.hide_render = obj.hide_viewport = hidden
            obj.keyframe_insert('hide_render', frame=frame)
            obj.keyframe_insert('hide_viewport', frame=frame)
        for curve in particles._action_fcurves(obj.animation_data.action):
            # Blender may put object and shape-key animation in slots of one Action.
            if curve.data_path in ('hide_render', 'hide_viewport'):
                for key in curve.keyframe_points:
                    key.interpolation = 'CONSTANT'
        return obj
    except Exception:
        from ..animation.event_effects import _remove
        if obj is not None:
            _remove([obj])
        else:
            if mesh is not None:
                bpy.data.meshes.remove(mesh)
            if material is not None:
                bpy.data.materials.remove(material)
        raise
