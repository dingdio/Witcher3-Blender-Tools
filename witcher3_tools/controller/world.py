"""Scene collision and optional reference terrain for the player."""
import logging
import math
import os
import time

import bpy
import numpy as np
from mathutils import Vector
from mathutils.bvhtree import BVHTree

from . import core

log = logging.getLogger(__name__)
WORLD_RADIUS = 60.0            # collision radius, metres
BODY_RADIUS = 0.3
WALL_HEIGHTS = (0.5, 1.3)      # ray heights above feet
WALL_STEEP = 0.6               # maximum wall normal |z|
MAX_DROP = 60.0


def _skip_object(obj) -> bool:
    from ..ui import ui_map
    if (obj.get('witcher_environment_preview') or obj.get('witcher_trail_version')
            or obj.get('witcher_particle_preview_version') or obj.get('witcher_player_effect')):
        return True
    if obj.display_type in {'WIRE', 'BOUNDS'}:
        return True
    if any(k.startswith(("witcher_water", "witcher_world_water")) for k in obj.keys()):
        return True
    return (ui_map._layer_visibility_is_foliage_object(obj) or ui_map._layer_visibility_is_proxy_mesh_object(obj)
            or ui_map._layer_visibility_is_engine_hidden_object(obj))


def _mesh_arrays(obj, with_uv=False):
    """(vertices, triangles, triangle material indices, triangle corner UVs or None) of the evaluated mesh."""
    try:
        mesh = obj.to_mesh()
    except RuntimeError:
        return None
    try:
        mesh.calc_loop_triangles()
        n, t = len(mesh.vertices), len(mesh.loop_triangles)
        if not n or not t:
            return None
        co = np.empty(n * 3)
        mesh.vertices.foreach_get("co", co)
        tri = np.empty(t * 3, np.int32)
        mesh.loop_triangles.foreach_get("vertices", tri)
        mat = np.empty(t, np.int32)
        mesh.loop_triangles.foreach_get("material_index", mat)
        uv = None
        if with_uv and mesh.uv_layers.active is not None:
            loops = np.empty(t * 3, np.int32)
            mesh.loop_triangles.foreach_get("loops", loops)
            per_loop = np.empty(len(mesh.loops) * 2)
            mesh.uv_layers.active.data.foreach_get("uv", per_loop)
            uv = per_loop.reshape(-1, 2)[loops].reshape(t, 3, 2)
        return co.reshape(n, 3), tri.reshape(t, 3), mat, uv
    finally:
        obj.to_mesh_clear()


def _barycentric(corners, p):
    a, b, c = corners
    v0, v1, v2 = b - a, c - a, p - a
    d00, d01, d11, d20, d21 = v0 @ v0, v0 @ v1, v1 @ v1, v2 @ v0, v2 @ v1
    den = d00 * d11 - d01 * d01
    if abs(den) < 1e-12:
        return np.array([1.0, 0.0, 0.0])
    v, w = (d11 * d20 - d01 * d21) / den, (d00 * d21 - d01 * d20) / den
    return np.array([1.0 - v - w, v, w])


class World:
    """BVH of nearby visible geometry, rebuilt after moving half its radius."""

    def __init__(self, player):
        self.exclude = {player.name} | {o.name for o in player.children_recursive}
        self.bvh = None
        self.centre = None
        self.triangles = 0
        self._skip = {}
        self.sources = []              # [(object, material indices, UVs)]
        self._owner = self._local = self._flat = None

    def ensure(self, x: float, y: float):
        if self.centre is None or math.hypot(x - self.centre[0], y - self.centre[1]) > WORLD_RADIUS * 0.5:
            self.build(x, y)

    def build(self, x: float, y: float):
        started = time.perf_counter()
        depsgraph = bpy.context.evaluated_depsgraph_get()
        cache, verts, owners, local = {}, [], [], []
        self.sources = []
        centre = np.array([x, y])
        for inst in depsgraph.object_instances:
            obj = inst.object
            if obj.type != 'MESH':
                continue
            orig = obj.original
            name = orig.name
            if name in self.exclude:
                continue
            if name not in self._skip:
                self._skip[name] = _skip_object(orig)
            if self._skip[name]:
                continue
            m = np.array(inst.matrix_world)
            corners = np.array(obj.bound_box) @ m[:3, :3].T + m[:3, 3]
            lo, hi = corners[:, :2].min(axis=0), corners[:, :2].max(axis=0)
            if np.linalg.norm(np.clip(centre, lo, hi) - centre) > WORLD_RADIUS:
                continue
            key = name if inst.is_instance else (name, obj.data.name)
            if key not in cache:
                cache[key] = _mesh_arrays(obj, with_uv=bool(orig.get("witcher_terrain_tile")))
            arrays = cache[key]
            if arrays is None:
                continue
            co, tri, mat, uv = arrays
            points = (co @ m[:3, :3].T + m[:3, 3])[tri]
            lo, hi = points[:, :, :2].min(axis=1), points[:, :, :2].max(axis=1)
            near = np.linalg.norm(np.clip(centre, lo, hi) - centre, axis=1) <= WORLD_RADIUS
            if near.any():
                verts.append(points[near].reshape(-1, 3))
                index = np.flatnonzero(near)
                owners.append(np.full(len(index), len(self.sources), np.int32))
                local.append(index)
                self.sources.append((orig, mat, uv))
        if verts:
            flat = np.concatenate(verts)
            self.triangles = len(flat) // 3
            self.bvh = BVHTree.FromPolygons(flat.tolist(), np.arange(len(flat)).reshape(-1, 3).tolist(), all_triangles=True)
            self._owner, self._local, self._flat = np.concatenate(owners), np.concatenate(local), flat
        else:
            self.bvh, self.triangles, self._owner, self._local, self._flat = None, 0, None, None, None
        self.centre = (x, y)
        log.info("player world: %d triangles in %.2fs", self.triangles, time.perf_counter() - started)

    def floor(self, x: float, y: float, z: float):
        if self.bvh is None:
            return None
        hit = self.bvh.ray_cast(Vector((x, y, z + core.STEP_UP)), Vector((0.0, 0.0, -1.0)), core.STEP_UP + MAX_DROP)
        return hit[0].z if hit[0] is not None else None

    def slide(self, x0: float, y0: float, z: float, x1: float, y1: float):
        if self.bvh is None:
            return x1, y1
        move = Vector((x1 - x0, y1 - y0, 0.0))
        for _ in range(2):
            length = move.length
            if length < 1e-6:
                return x0 + move.x, y0 + move.y
            wall = self._wall(x0, y0, z, move / length, length + BODY_RADIUS)
            if wall is None:
                return x0 + move.x, y0 + move.y
            move -= wall * move.dot(wall)
        return x0, y0

    def _wall(self, x: float, y: float, z: float, direction: Vector, distance: float):
        for h in WALL_HEIGHTS:
            hit = self.bvh.ray_cast(Vector((x, y, z + h)), direction, distance)
            if hit[0] is not None and abs(hit[1].z) < WALL_STEEP:
                normal = Vector((hit[1].x, hit[1].y, 0.0))
                if normal.length > 1e-6:
                    return normal.normalized()
        return None

    def ray(self, origin: Vector, direction: Vector, distance: float):
        return self.bvh.ray_cast(origin, direction, distance)[3] if self.bvh is not None else None

    def ground_hit(self, x: float, y: float, z: float):
        """(object, material index, UV or None) of the triangle under (x, y, z), or None."""
        if self.bvh is None:
            return None
        location, _, face, _ = self.bvh.ray_cast(Vector((x, y, z + core.STEP_UP)), Vector((0.0, 0.0, -1.0)), core.STEP_UP + 2.0)
        if location is None:
            return None
        obj, mat, uv = self.sources[self._owner[face]]
        tri = int(self._local[face])
        if uv is None:
            return obj, int(mat[tri]), None
        return obj, int(mat[tri]), _barycentric(self._flat[face * 3:face * 3 + 3], np.array(location)) @ uv[tri]


REFERENCE_LEVEL = r"engine\reference_level\world\reference_level.w2w"


def _ground_below(arm):
    world = World(arm)
    x, y, z = arm.matrix_world.translation
    world.build(x, y)
    if world.bvh is None:
        return None
    hit = world.bvh.ray_cast(Vector((x, y, z + 1000.0)), Vector((0.0, 0.0, -1.0)), 2000.0)[0]
    return hit.z if hit is not None else None


def import_reference_ground(context):
    """Import REDkit reference terrain as one object, with its environment."""
    from ..CR2W import CR2W_reader
    from ..CR2W.common_blender import mod_loading_context, repo_file
    from ..importers import import_w2w
    path = str(repo_file(REFERENCE_LEVEL) or "")
    if not os.path.isfile(path):
        raise FileNotFoundError(REFERENCE_LEVEL)
    browser = context.scene.witcher_file_browser
    mode, active = browser.terrain_import_mode, context.view_layer.active_layer_collection
    browser.terrain_import_mode = 'FULL_MAP'
    try:
        world = CR2W_reader.load_w2w(path, include_groups=False)
        with mod_loading_context(context):
            import_w2w.btn_import_w2w(world, path)
    finally:
        browser.terrain_import_mode = mode
        context.view_layer.active_layer_collection = active
    env = getattr(context.scene, "witcher_environment", None)
    if env is not None and not env.preview_enabled:
        env.preview_enabled = True          # the import loaded the level's environment; this shows it
    context.view_layer.update()


def ensure_ground(context, arm) -> bool:
    """Add reference terrain if needed; return whether it was imported."""
    if _ground_below(arm) is not None:
        return False
    layer = context.view_layer
    active, selected = layer.objects.active, [o for o in layer.objects if o.select_get()]
    import_reference_ground(context)
    for o in layer.objects:
        o.select_set(o in selected)
    layer.objects.active = active
    z = _ground_below(arm)
    m = arm.matrix_world.copy()
    if z is None:                      # outside reference terrain
        m.translation.xy = (0.0, 0.0)
        arm.matrix_world = m
        context.view_layer.update()
        z = _ground_below(arm)
    if z is not None:
        m.translation.z = z
        arm.matrix_world = m
    return True
