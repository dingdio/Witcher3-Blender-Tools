"""Live weapon ribbons driven by the controller clock and evaluated sword motion."""
import logging
import math

import bpy
import numpy as np
from mathutils import Vector

from ..animation import event_effects
from ..CR2W.common_blender import redkit_repo_context
from ..importers import import_particle as particles, import_trail, import_blood

log = logging.getLogger(__name__)


class Ribbon:
    def __init__(self, scene, weapon, local, emitter, track, duration, blood):
        self.weapon, self.local, self.blood = weapon, local, blood
        self.settings = settings = (import_blood.blood_settings(emitter) if blood
                                    else import_trail.trail_settings(emitter, track))
        if settings is None:
            raise ValueError('Disabled trail emitter')
        self.offset = emitter.lods[0].delay if blood else track['time_begin']
        self.duration = duration
        if blood:
            count = math.ceil(emitter.lods[0].duration * settings.birth_rate - 1e-6)
            self.times = [i / settings.birth_rate for i in range(count)]
            self.duration = self.times[-1] + settings.lifetime
            self.size = particles._evaluator(particles._module(emitter, 'CParticleInitializerSize'), 'size')
            self.emission = emitter.lods[0].duration
            material = import_blood._material(emitter, settings, 0, 1, live=True, track=track,
                                              times=self.times, distances=[0] * len(self.times))
        else:
            count = max(2, math.ceil(duration * max(settings.birth_rate, 120)) + 1)
            if count > 2000:
                raise ValueError('Live trail is too long')
            self.times = [duration * i / (count - 1) for i in range(count)]
            material = import_trail._material(emitter, settings, import_trail.material_parameters(emitter.material),
                                              0, duration, 1, [0, 1], [0, duration], track, live=True)
        mesh = bpy.data.meshes.new('Player Blood' if blood else 'Player Sword Trail')
        mesh.from_pydata([(0, 0, 0)] * (2 * count), [], [(2*i, 2*i+1, 2*i+3, 2*i+2) for i in range(count-1)])
        mesh.materials.append(material)
        mesh.uv_layers.new(name='Trail UV')
        mesh.attributes.new('w3_trail_birth', 'FLOAT', 'POINT').data.foreach_set('value', [t for t in self.times for _ in range(2)])
        mesh.attributes.new('w3_trail_distance', 'FLOAT', 'POINT')
        self.obj = bpy.data.objects.new(mesh.name, mesh)
        scene.collection.objects.link(self.obj)
        self.obj['witcher_player_effect'] = True
        self.tiling = float(emitter.drawer_properties.get('texturesPerUnit', 0.5))
        self.nodes = material.node_tree.nodes
        self.clock = (self.nodes.get('Blood Time') or self.nodes['Trail Time']).outputs[0]
        self.previous_time, self.previous_matrix = 0.0, None
        self.stop()

    def stop(self):
        self.start = None
        self.obj.hide_render = self.obj.hide_viewport = True

    def trigger(self, time, duration):
        self.start = time + self.offset
        self.end = self.start + self.duration
        if duration is not None and not self.blood:
            self.end = min(self.end, time + duration)
        self.points, self.axes, self.widths, self.distances = [], [], [], []

    def update(self, time, depsgraph):
        matrix = self.weapon.evaluated_get(depsgraph).matrix_world @ self.local
        previous = self.previous_matrix if self.previous_matrix is not None else matrix
        span = time - self.previous_time
        old_time = self.previous_time
        self.previous_time, self.previous_matrix = time, matrix.copy()
        if self.start is None or time < self.start:
            return
        if time >= self.end:
            self.stop()
            return
        age = time - self.start
        while len(self.points) < len(self.times) and self.times[len(self.points)] <= age:
            birth = self.times[len(self.points)]
            factor = min(1, max(0, (self.start + birth - old_time) / span)) if span > 0 else 1
            # shortcut: interpolate between presented poses; exact substeps need per-tick rig evaluation.
            sample = previous.lerp(matrix, factor)
            point = sample.translation.copy()
            distance = self.distances[-1] + (point-self.points[-1]).length if self.points else 0
            self.points.append(point)
            self.axes.append((sample.to_3x3() @ Vector((0, 0, 1))).normalized())
            width = (particles._vector(particles._evaluate(self.size, birth / self.emission, (1, 1, 1, 1)))[0]
                     if self.blood else self.settings.base_size_min[0])
            self.widths.append(width)
            self.distances.append(distance)
        vertices = []
        for i, birth in enumerate(self.times):
            if i >= len(self.points):
                vertices.extend((matrix.translation, matrix.translation))
                continue
            point = self.points[i].copy()
            if self.blood:
                particle_age = min(self.settings.lifetime, age-birth)
                point += Vector(self.settings.acceleration) * (0.5 * particle_age * particle_age)
            width = self.axes[i] * self.widths[i] / 2
            vertices.extend((point + width, point - width))
        mesh = self.obj.data
        mesh.vertices.foreach_set('co', [v for point in vertices for v in point])
        distance = self.distances + [self.distances[-1] if self.distances else 0] * (len(self.times)-len(self.points))
        mesh.attributes['w3_trail_distance'].data.foreach_set('value', [d for d in distance for _ in range(2)])
        uv = mesh.uv_layers.active.data
        uv.foreach_set('uv', [v for loop in mesh.loops for v in (distance[loop.vertex_index//2]*self.tiling, loop.vertex_index % 2)])
        self.clock.default_value = age
        if 'Head Distance' in self.nodes:
            self.nodes['Head Distance'].outputs[0].default_value = float(np.interp(age, self.times, distance))
            self.nodes['Tail Distance'].outputs[0].default_value = float(np.interp(age-self.settings.lifetime, self.times, distance))
        mesh.update()
        self.obj.hide_render = self.obj.hide_viewport = False


class Effects:
    def __init__(self, scene, owner, weapons, clips):
        self.ribbons = {}
        # Ribbons from a session that was saved or undone mid-play have no owner left.
        event_effects._remove([o for o in bpy.data.objects if o.get('witcher_player_effect')])
        self.saved = [(o, o.hide_viewport, o.hide_render) for o in scene.objects
                      if o.get('witcher_trail_version') and o.get('witcher_trail_owner') == owner]
        requests = {(e[1], e[2], e[5]) for clip in clips for e in clip.effects if not e[3]}
        for category, weapon in weapons.items():
            try:
                _, mesh, inverse, entity, path = event_effects._item_from_slot(scene, weapon['slot'])
            except (ValueError, OSError):
                log.exception('player: cannot resolve effects for %s', category)
                continue
            for name, slot, blood in requests:
                if slot != (weapon['slot'].hold_slot or 'r_weapon'):
                    continue
                effect = next((e for e in entity.cookedEffects if e['name'] == name), None)
                if effect is None or effect['is_looped']:
                    continue
                ribbons = self.ribbons.setdefault((category, name, blood), [])
                for track in effect['particle_systems']:
                    duration = min(track['duration'], effect['length'] - track['time_begin'])
                    if track['duration'] <= 0 or (not blood and duration <= 0):
                        continue
                    try:
                        local = inverse @ event_effects._effect_matrix(entity, track['slot'])
                        with redkit_repo_context(path):
                            system, _, source_path = particles._resolve_source(track['path'])
                        with redkit_repo_context(source_path):
                            for emitter in system.emitters:
                                if emitter.lods and not emitter.lods[0].enabled:
                                    continue
                                ribbon = Ribbon(scene, mesh, local, emitter, track, duration, blood)
                                ribbon.obj['witcher_player_effect_owner'] = owner
                                ribbon.obj['witcher_player_effect_name'] = name
                                ribbons.append(ribbon)
                    except (ValueError, OSError):
                        log.exception('player: unsupported weapon effect %s', name)

    def trigger(self, category, time, event, blood_preview):
        _, name, _, stop, duration, blood = event
        if blood and not blood_preview:
            return
        for ribbon in self.ribbons.get((category, name, blood), []):
            if stop:
                ribbon.stop()
            else:
                ribbon.trigger(time, duration)

    def update(self, time):
        if not self.ribbons:
            return
        depsgraph = bpy.context.evaluated_depsgraph_get()
        for ribbons in self.ribbons.values():
            for ribbon in ribbons:
                ribbon.update(time, depsgraph)

    def stop(self):
        for ribbons in self.ribbons.values():
            for ribbon in ribbons:
                ribbon.stop()

    def hide_bakes(self):
        for obj, _, _ in self.saved:
            try:
                obj.hide_viewport = obj.hide_render = True
            except ReferenceError:
                pass

    def close(self):
        event_effects._remove([r.obj for rs in self.ribbons.values() for r in rs])
        self.ribbons.clear()
        for obj, viewport, render in self.saved:
            try:
                obj.hide_viewport, obj.hide_render = viewport, render
            except ReferenceError:
                pass
