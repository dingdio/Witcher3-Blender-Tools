"""Baked world-space ribbons for the native weapon-trail particle drawer."""
import math

import bpy
from mathutils import Vector

from . import import_particle as particles
from ..CR2W import dc_particle
from ..CR2W.CR2W_types import getCR2W
from ..CR2W.common_blender import repo_file

TRAIL_PREVIEW_VERSION = 1
_MODULES = {'CParticleInitializerAlpha', 'CParticleInitializerSize',
            'CParticleInitializerLifeTime', 'CParticleModificatorAlphaOverLife',
            'CParticleModificatorAlphaOverEffect'}
RED_BLOOD_MATERIAL = r'fx\weapons\trails\blood\red_blood_trail_mat.w2mg'


def material_parameters(material):
    """Resolve instance inheritance and the graph's authored parameter defaults."""
    parameters = dict(material.parameters)
    path = material.base_material
    seen = set()
    while path and path not in seen:
        seen.add(path)
        with open(repo_file(path), 'rb') as stream:
            resource = getCR2W(stream)
        if path.lower().endswith('.w2mg'):
            graph = path.replace('/', '\\').lower()
            if graph not in (r'fx\shaders\weapon_trails.w2mg', RED_BLOOD_MATERIAL):
                raise ValueError(f'Unsupported trail shader: {path}')
            for chunk in resource.CHUNKS.CHUNKS:
                name = dc_particle._value(chunk.GetVariableByName('parameterName'))
                if not name:
                    continue
                for field in ('scalar', 'color', 'vector', 'texture'):
                    prop = chunk.GetVariableByName(field)
                    if prop is not None:
                        parameters.setdefault(name, dc_particle._value(prop))
            if graph == RED_BLOOD_MATERIAL:
                # This graph uses unnamed constants for the same additive/distorted trail chain.
                parameters.update(distortion_texture=parameters['noise03'], distortion_scale=12,
                                  distortion_bias=0.1, distortion_multiply=0.4,
                                  color={'Red': 255, 'Green': 71, 'Blue': 31}, color_multiply=0.5)
            return parameters
        instance = next(c for c in resource.CHUNKS.CHUNKS if c.Type == 'CMaterialInstance')
        decoded = dc_particle._decode_material_chunk(instance)
        parameters = {**decoded.parameters, **parameters}
        path = decoded.base_material
    raise ValueError('Trail material has no supported base graph')


def _curve_node(nodes, links, name, value, points):
    node = nodes.new('ShaderNodeFloatCurve')
    node.name = name
    node.mapping.initialize()
    node.mapping.use_clip = False
    curve = node.mapping.curves[0]
    points = list(points)
    curve.points[0].location = points[0]
    curve.points[-1].location = points[-1]
    for x, y in points[1:-1]:
        curve.points.new(x, y)
    for point in curve.points:
        point.handle_type = 'VECTOR'
    node.mapping.update()
    links.new(value, node.inputs['Value'])
    return node.outputs['Value']


def _material(emitter, settings, parameters, start, duration, rate, distances, times, track, *, live=False):
    images = {}
    for key in ('diffuse', 'distortion_texture'):
        path = parameters[key]
        images[path] = particles._load_particle_image(path)
        if images[path] is None:
            raise ValueError(f'Trail texture could not be loaded: {path}')
    material = bpy.data.materials.new('Witcher Weapon Trail')
    material.use_nodes = True
    material['witcher_trail_version'] = TRAIL_PREVIEW_VERSION
    material['witcher_source_material'] = emitter.material.base_material
    material['witcher_source_texture'] = parameters.get('diffuse', '')
    # shortcut: scene-depth refraction and REDengine day/shadow lighting need a renderer-specific pass.
    material['witcher_trail_limits'] = 'Additive preview; no scene-depth refraction or game lighting'
    material.use_backface_culling = False
    material.surface_render_method = 'BLENDED'
    material.use_transparency_overlap = True
    material.use_transparent_shadow = False
    nodes, links = material.node_tree.nodes, material.node_tree.links
    nodes.clear()

    def math_node(operation, a, b=0.0):
        node = nodes.new('ShaderNodeMath')
        node.operation = operation
        for index, value in enumerate((a, b)):
            if isinstance(value, (int, float)):
                node.inputs[index].default_value = value
            else:
                links.new(value, node.inputs[index])
        return node.outputs[0]

    def attribute(name):
        node = nodes.new('ShaderNodeAttribute')
        node.attribute_name = name
        return node.outputs['Fac']

    def clamp(value):
        return math_node('MINIMUM', math_node('MAXIMUM', value, 0), 1)

    clock = nodes.new('ShaderNodeValue')
    clock.name = 'Trail Time'
    if not live:
        clock.outputs[0].driver_add('default_value').driver.expression = f'(frame-({start!r}))/{rate!r}'
    time = clock.outputs[0]
    age = math_node('SUBTRACT', time, attribute('w3_trail_birth'))
    normalized_age = clamp(math_node('DIVIDE', age, settings.lifetime))
    alive = math_node('MULTIPLY', math_node('GREATER_THAN', age, 0),
                      math_node('LESS_THAN', age, settings.lifetime))
    alive = math_node('MULTIPLY', alive, math_node('LESS_THAN', time, duration))
    fade = _curve_node(nodes, links, 'Particle Age Fade', normalized_age,
                       [(i / 64, particles._scalar(settings.alpha_evaluator, i / 64, 1)) for i in range(65)])
    alpha = math_node('MULTIPLY', alive, fade)
    initial_alpha = particles._scalar(particles._evaluator(emitter.module('CParticleInitializerAlpha'), 'alpha'), 0, 1)
    alpha = math_node('MULTIPLY', alpha, initial_alpha)
    if emitter.module('CParticleModificatorAlphaOverEffect'):
        curves = track.get('curves', [])
        effect_curve = curves[1] if len(curves) > 1 else None
        effect_age = clamp(math_node('DIVIDE', time, track['duration']))
        effect_fade = _curve_node(nodes, links, 'Effect Fade', effect_age,
                                  [(i / 64, particles._evaluate_curve(effect_curve, i / 64)) for i in range(65)])
        alpha = math_node('MULTIPLY', alpha, effect_fade)

    uv = nodes.new('ShaderNodeTexCoord')
    separate = nodes.new('ShaderNodeSeparateXYZ')
    links.new(uv.outputs['UV'], separate.inputs[0])
    u = separate.outputs['X']
    if emitter.drawer_properties.get('dynamicTexCoords', False):
        points = [(t / duration, d) for t, d in zip(times, distances)]
        current = _curve_node(nodes, links, 'Head Distance', clamp(math_node('DIVIDE', time, duration)), points)
        oldest = _curve_node(nodes, links, 'Tail Distance',
                             clamp(math_node('DIVIDE', math_node('SUBTRACT', time, settings.lifetime), duration)), points)
        if live:
            for name in ('Head Distance', 'Tail Distance'):
                nodes.remove(nodes[name])
            head, tail = nodes.new('ShaderNodeValue'), nodes.new('ShaderNodeValue')
            head.name, tail.name = 'Head Distance', 'Tail Distance'
            current, oldest = head.outputs[0], tail.outputs[0]
        span = math_node('MAXIMUM', math_node('SUBTRACT', current, oldest), 0.000001)
        u = math_node('DIVIDE', math_node('SUBTRACT', attribute('w3_trail_distance'), oldest), span)
        u = math_node('MULTIPLY', u, float(emitter.drawer_properties.get('texturesPerUnit', 0.5)))
    coords = nodes.new('ShaderNodeCombineXYZ')
    links.new(u, coords.inputs['X'])
    links.new(separate.outputs['Y'], coords.inputs['Y'])

    def vector_node(operation, a, b):
        node = nodes.new('ShaderNodeVectorMath')
        node.operation = operation
        for index, value in enumerate((a, b)):
            if isinstance(value, tuple):
                node.inputs[index].default_value = value
            else:
                links.new(value, node.inputs[index])
        return node.outputs['Vector']

    def texture(path, coordinates):
        node = nodes.new('ShaderNodeTexImage')
        node.image = images[path]
        links.new(coordinates, node.inputs['Vector'])
        return node

    # Authored graph: noise(UV * scale + time * speed) perturbs the diffuse lookup.
    scale = float(parameters.get('distortion_scale', 6))
    speed = particles._vector(parameters.get('distortion_speed'), (1, 1, 0, 0))[:3]
    motion = nodes.new('ShaderNodeCombineXYZ')
    for index in range(3):
        links.new(math_node('MULTIPLY', time, speed[index]), motion.inputs[index])
    noise_uv = vector_node('ADD', vector_node('MULTIPLY', coords.outputs[0], (scale,) * 3), motion.outputs[0])
    noise = texture(parameters['distortion_texture'], noise_uv)
    offset = vector_node('SUBTRACT', vector_node('MULTIPLY', noise.outputs['Color'], (0.5,) * 3),
                         (float(parameters.get('distortion_bias', 0.2)),) * 3)
    amount = float(parameters.get('distortion_multiply', 0.15))
    warped = vector_node('ADD', coords.outputs[0], vector_node('MULTIPLY', offset, (amount,) * 3))
    diffuse = texture(parameters['diffuse'], warped)
    color = parameters.get('color', {})
    tint = tuple(float(color.get(k, 255)) / 255 for k in ('Red', 'Green', 'Blue'))
    tint = tuple(x * float(parameters.get('color_multiply', 0.5)) * 2.2 for x in tint)
    rgb = vector_node('MULTIPLY', diffuse.outputs['Color'], tint)
    # The native graph requests gamma input; convert its output to Blender's linear light.
    rgb = vector_node('MULTIPLY', rgb, rgb)
    emission = nodes.new('ShaderNodeEmission')
    links.new(rgb, emission.inputs['Color'])
    links.new(math_node('MULTIPLY', alpha, alpha), emission.inputs['Strength'])
    transparent = nodes.new('ShaderNodeBsdfTransparent')
    add = nodes.new('ShaderNodeAddShader')
    links.new(transparent.outputs[0], add.inputs[0])
    links.new(emission.outputs[0], add.inputs[1])
    output = nodes.new('ShaderNodeOutputMaterial')
    links.new(add.outputs[0], output.inputs['Surface'])
    material.diffuse_color = (*tint, 0.3)
    return material


def trail_settings(emitter, track):
    if emitter.drawer_type != 'CParticleDrawerTrail' or emitter.keep_simulation_local:
        raise ValueError('Weapon trails require a world-space CParticleDrawerTrail')
    unsupported = [m.type_name for m in emitter.modules if m.enabled and m.type_name not in _MODULES]
    if unsupported:
        raise ValueError('Unsupported trail modules: ' + ', '.join(unsupported))
    settings = particles._settings(emitter)
    if not settings.lod_enabled or settings.birth_rate <= 0 or settings.max_particles <= 0:
        return None
    if settings.base_size_min != settings.base_size_max:
        raise ValueError('Random trail width is not supported')
    curves = track.get('curves', [])
    for index in (0, 2):
        if len(curves) > index and any(abs(particles._evaluate_curve(curves[index], i / 32) - 1) > 1e-4 for i in range(33)):
            raise ValueError('Animated trail emission/size needs a simulated preview')
    return settings


def bake_trail(scene, collection, emitter, matrix_at, start, end, track):
    """Sample a moving emitter; the saved material reveals its world-space history."""
    settings = trail_settings(emitter, track)
    if settings is None:
        return None
    rate = scene.render.fps / scene.render.fps_base
    duration = (end - start) / rate
    if duration <= 0:
        return None
    # Oversample the authored motion for smooth ribbon edges and subframe scrubbing.
    count = max(2, math.ceil(duration * max(settings.birth_rate, rate * 4)) + 1)
    if count > 20000:
        raise ValueError('Trail bake is too long; shorten the selected clip')
    times = [duration * i / (count - 1) for i in range(count)]
    width = settings.base_size_min[0]
    vertices, distances = [], []
    previous, distance = None, 0.0
    for time in times:
        matrix = matrix_at(start + time * rate)
        position = matrix.translation
        axis = matrix.to_3x3() @ Vector((0, 0, 1))
        axis.normalize()
        vertices.extend((position + axis * width / 2, position - axis * width / 2))
        if previous is not None:
            distance += (position - previous).length
        distances.append(distance)
        previous = position.copy()
    if distance < 1e-7:
        raise ValueError('The held weapon does not move during this effect')
    parameters = material_parameters(emitter.material)
    material = None
    mesh = None
    try:
        material = _material(emitter, settings, parameters, start, duration, rate, distances, times, track)
        mesh = bpy.data.meshes.new('Witcher Weapon Trail')
        mesh.from_pydata(vertices, [], [(2*i, 2*i+1, 2*i+3, 2*i+2) for i in range(count-1)])
        mesh.materials.append(material)
        uv = mesh.uv_layers.new(name='Trail UV')
        for polygon in mesh.polygons:
            for loop in polygon.loop_indices:
                vertex = mesh.loops[loop].vertex_index
                uv.data[loop].uv = (distances[vertex // 2] * float(emitter.drawer_properties.get('texturesPerUnit', 0.5)), vertex % 2)
        for name, values in (('w3_trail_birth', times), ('w3_trail_distance', distances)):
            attribute = mesh.attributes.new(name, 'FLOAT', 'POINT')
            attribute.data.foreach_set('value', [value for value in values for _ in range(2)])
        obj = bpy.data.objects.new('Witcher Weapon Trail', mesh)
        collection.objects.link(obj)
        obj['witcher_trail_version'] = TRAIL_PREVIEW_VERSION
        obj['witcher_trail_frame_start'] = start
        obj['witcher_trail_frame_end'] = end
        obj['witcher_trail_lifetime'] = settings.lifetime
        obj['witcher_trail_width'] = width
        obj['witcher_trail_samples'] = count
        for frame, hidden in ((start-1, True), (start, False), (end, True)):
            obj.hide_render = obj.hide_viewport = hidden
            obj.keyframe_insert('hide_render', frame=frame)
            obj.keyframe_insert('hide_viewport', frame=frame)
        for curve in particles._action_fcurves(obj.animation_data.action):
            for key in curve.keyframe_points:
                key.interpolation = 'CONSTANT'
        return obj
    except Exception:
        if mesh is not None and mesh.users == 0:
            bpy.data.meshes.remove(mesh)
        if material is not None and material.users == 0:
            bpy.data.materials.remove(material)
        raise
