"""
Foliage loading for .flyr (CFoliageResource) files.

Flat structure — no sub-collections:

    World_Foliage/
      fi_pine_01        GN instancer mesh (visible) — N vertices = N instances
      fi_pine_01_src    LOD0 source mesh  (hidden)  — referenced by GN Object Info
      fi_grass_01       ...

Using Object Info instead of Collection Info means exactly ONE mesh is
instanced per type regardless of how many LODs/proxies the import produced.
Blender object count = 2 × number of unique tree types (~40-100 objects total).
"""
import os
import json
import math
import logging
import bpy
from math import radians
from mathutils import Euler

log = logging.getLogger(__name__)

CELL_SIZE = 64.0

# Session-level accumulator scoped per foliage root collection:
#   root_key -> {depot_path -> [(x, y, z, ex, ey, ez), ...]}
# Euler values are pre-converted to XYZ order for the GN attribute.
_type_transforms: dict = {}


def _foliage_root_state_key(foliage_root) -> str:
    """Return a stable session key for one live foliage root collection."""
    try:
        return str(foliage_root.as_pointer())
    except Exception:
        return str(getattr(foliage_root, "name", "") or "")


def _get_root_transform_bucket(foliage_root, create: bool = False):
    root_key = _foliage_root_state_key(foliage_root)
    bucket = _type_transforms.get(root_key)
    if bucket is None and create:
        bucket = {}
        _type_transforms[root_key] = bucket
    return bucket


# ---------------------------------------------------------------------------
# Path / grid helpers
# ---------------------------------------------------------------------------

def _to_game_rel_path(abs_path: str, context=None) -> str:
    from .. import get_uncook_path
    uncook = (get_uncook_path(context or bpy.context) or "").replace("/", "\\").rstrip("\\")
    norm = abs_path.replace("/", "\\")
    if uncook:
        prefix = uncook + "\\"
        if norm.lower().startswith(prefix.lower()):
            return norm[len(prefix):]
    return norm


def get_game_rel_foliage_prefix(world_path: str, context=None) -> str:
    rel = _to_game_rel_path(world_path, context)
    return os.path.join(os.path.dirname(rel), "source_foliage").replace("/", "\\")


def cell_key(cx: float, cy: float) -> str:
    return f"{cx:.2f}_{cy:.2f}"


def cell_key_from_path(flyr_path: str) -> str:
    base = os.path.splitext(os.path.basename(flyr_path))[0]
    return base[len("foliage_"):] if base.startswith("foliage_") else base


def game_rel_flyr_path(foliage_prefix: str, cx: float, cy: float) -> str:
    return os.path.join(foliage_prefix, f"foliage_{cx:.2f}_{cy:.2f}.flyr").replace("/", "\\")


def _snap_to_cell(coord: float) -> float:
    return math.floor(coord / CELL_SIZE) * CELL_SIZE


def cells_in_radius(cam_x, cam_y, terrain_size, radius):
    half = terrain_size / 2.0
    x0 = _snap_to_cell(cam_x - radius)
    y0 = _snap_to_cell(cam_y - radius)
    x1 = _snap_to_cell(cam_x + radius)
    y1 = _snap_to_cell(cam_y + radius)
    x = x0
    while x <= x1:
        y = y0
        while y <= y1:
            cx_c = x + CELL_SIZE * 0.5
            cy_c = y + CELL_SIZE * 0.5
            if (cx_c - cam_x)**2 + (cy_c - cam_y)**2 <= radius**2 and abs(x) <= half and abs(y) <= half:
                yield x, y
            y += CELL_SIZE
        x += CELL_SIZE


# ---------------------------------------------------------------------------
# Bundle-aware cell discovery
# ---------------------------------------------------------------------------

def _get_bundle_manager():
    try:
        from ..CR2W.witcher_cache.Bundles import LoadBundleManager
        return LoadBundleManager()
    except Exception:
        return None


def find_all_flyr_keys_in_bundles(foliage_prefix: str) -> dict:
    """Return {cell_key: game_rel_path} for every .flyr found in bundles + disk."""
    prefix_lower = foliage_prefix.lower().rstrip("\\") + "\\"
    result = {}

    bm = _get_bundle_manager()
    if bm is not None:
        for key in bm.Items:
            kl = key.lower().replace("/", "\\")
            if kl.startswith(prefix_lower) and kl.endswith(".flyr"):
                base = os.path.splitext(os.path.basename(key))[0]
                if base.startswith("foliage_"):
                    result[base[len("foliage_"):]] = key.replace("/", "\\")

    from .. import get_uncook_path
    uncook = (get_uncook_path(bpy.context) or "").rstrip("\\/")
    if uncook:
        disk_dir = os.path.join(uncook, foliage_prefix)
        if os.path.isdir(disk_dir):
            for fname in os.listdir(disk_dir):
                if fname.lower().endswith(".flyr"):
                    base = os.path.splitext(fname)[0]
                    if base.startswith("foliage_"):
                        ck = base[len("foliage_"):]
                        if ck not in result:
                            result[ck] = os.path.join(foliage_prefix, fname).replace("/", "\\")
    return result


def count_all_foliage_cells(foliage_prefix: str) -> int:
    return len(find_all_flyr_keys_in_bundles(foliage_prefix))


def resolve_flyr_abs_path(game_rel_path: str) -> str:
    from ..CR2W.common_blender import repo_file
    try:
        p = repo_file(game_rel_path)
        if p and os.path.exists(p):
            return p
    except Exception:
        log.exception("Failed to resolve flyr: %s", game_rel_path)
    return None


def get_terrain_size_for_world(world_root_collection) -> float:
    def _search(coll):
        if "terrainSize" in coll:
            return float(coll["terrainSize"])
        for obj in coll.objects:
            if "terrainSize" in obj:
                return float(obj["terrainSize"])
        for child in coll.children:
            r = _search(child)
            if r is not None:
                return r
        return None
    return _search(world_root_collection) or 2048.0


# ---------------------------------------------------------------------------
# Foliage root collection (flat — no sub-collections)
# ---------------------------------------------------------------------------

def get_foliage_root_collection(world_root_collection):
    for child in world_root_collection.children:
        if child.get("_is_foliage_root"):
            return child
    name = world_root_collection.name + "_Foliage"
    coll = bpy.data.collections.new(name)
    world_root_collection.children.link(coll)
    coll["_is_foliage_root"] = True
    coll["_loaded_cells"] = "[]"
    return coll


def get_loaded_cells(foliage_root) -> set:
    try:
        return set(json.loads(foliage_root.get("_loaded_cells", "[]")))
    except Exception:
        return set()


def mark_cell_loaded(foliage_root, key: str):
    cells = get_loaded_cells(foliage_root)
    cells.add(key)
    foliage_root["_loaded_cells"] = json.dumps(sorted(cells))


def count_instances(foliage_root) -> int:
    total = 0
    for obj in foliage_root.objects:
        if obj.get("_is_foliage_instancer") and obj.type == 'MESH':
            total += len(obj.data.vertices)
    return total


def count_loaded_cells(foliage_root) -> int:
    return len(get_loaded_cells(foliage_root))


# ---------------------------------------------------------------------------
# Source mesh: import, pick LOD0, discard the rest
# ---------------------------------------------------------------------------

def _pick_best_mesh(objects: list):
    """Return the mesh object with the most faces (= LOD0), remove all others."""
    meshes = [o for o in objects if o.type == 'MESH' and o.data]
    if not meshes:
        # Remove non-mesh objects too
        for o in objects:
            bpy.data.objects.remove(o, do_unlink=True)
        return None

    best = max(meshes, key=lambda o: len(o.data.polygons))

    for obj in list(objects):
        if obj is best:
            continue
        data = obj.data if obj.type == 'MESH' else None
        bpy.data.objects.remove(obj, do_unlink=True)
        if data and data.users == 0:
            bpy.data.meshes.remove(data)

    return best


def _get_or_import_source_mesh(depot_path: str, foliage_root):
    """Return the single LOD0 source mesh for this depot path, importing if needed."""
    marker = "_src_" + depot_path
    for obj in foliage_root.objects:
        if obj.get("_depot_path") == marker:
            return obj

    from ..importers.import_helpers import meshPath
    from ..importers.import_blender_fun import _import_foliage_mesh

    mp = meshPath(meshName=depot_path)
    mp.type = "mesh_foliage"

    before = {o.as_pointer() for o in bpy.data.objects}
    try:
        _import_foliage_mesh(mp)
    except Exception:
        log.exception("Failed to import foliage type: %s", depot_path)

    new_objects = [o for o in bpy.data.objects if o.as_pointer() not in before]
    source = _pick_best_mesh(new_objects)

    if source is None:
        # Nothing imported — create a placeholder empty mesh so the instancer still works
        mesh = bpy.data.meshes.new("foliage_src_empty")
        source = bpy.data.objects.new("foliage_src_empty", mesh)

    source["_depot_path"] = marker
    source["_is_foliage_source"] = True
    source.hide_viewport = True
    source.hide_render = True

    # Move to foliage root (remove from any scene collection it landed in)
    for c in list(source.users_collection):
        c.objects.unlink(source)
    foliage_root.objects.link(source)

    return source


# ---------------------------------------------------------------------------
# GN instancer: build tree + rebuild mesh
# ---------------------------------------------------------------------------

def _build_foliage_gn_tree(ng, source_obj):
    """
    Named Attribute "rot" (FLOAT_VECTOR, XYZ euler)
      → Euler to Rotation
      → Instance on Points   ← Object Info (source_obj)
      → Output
    """
    nodes = ng.nodes
    links = ng.links
    nodes.clear()

    use_iface = hasattr(ng, "interface") and hasattr(ng.interface, "new_socket")

    def _sock(name, in_out, stype):
        if use_iface:
            ng.interface.new_socket(name=name, in_out=in_out, socket_type=stype)
        else:
            (ng.inputs if in_out == 'INPUT' else ng.outputs).new(stype, name)

    _sock("Geometry", "OUTPUT", "NodeSocketGeometry")
    _sock("Geometry", "INPUT", "NodeSocketGeometry")

    gin  = nodes.new('NodeGroupInput');  gin.location  = (-700, 0)
    gout = nodes.new('NodeGroupOutput'); gout.location = ( 500, 0)

    # Named attribute for rotation (FLOAT_VECTOR, XYZ euler)
    na = nodes.new('GeometryNodeInputNamedAttribute')
    na.location = (-500, -150)
    for v in ('FLOAT_VECTOR', 'VECTOR'):
        try:
            na.data_type = v
            break
        except Exception:
            pass
    try:
        na.inputs["Name"].default_value = "rot"
    except Exception:
        na.inputs[0].default_value = "rot"

    # Euler → Rotation
    e2r = None
    for bl_id in ('FunctionNodeEulerToRotation', 'FunctionNodeRotationFromEuler'):
        try:
            e2r = nodes.new(bl_id)
            e2r.location = (-250, -150)
            break
        except Exception:
            pass

    # Object Info — single LOD0 source mesh
    oi = nodes.new('GeometryNodeObjectInfo')
    oi.location = (-250, -300)
    try:
        oi.inputs['Object'].default_value = source_obj
    except Exception:
        pass
    try:
        oi.transform_space = 'ORIGINAL'
    except Exception:
        pass

    # Instance on Points
    iop = nodes.new('GeometryNodeInstanceOnPoints')
    iop.location = (150, 0)

    links.new(gin.outputs['Geometry'], iop.inputs['Points'])
    links.new(oi.outputs['Geometry'], iop.inputs['Instance'])
    if e2r is not None:
        links.new(na.outputs[0], e2r.inputs[0])
        try:
            links.new(e2r.outputs['Rotation'], iop.inputs['Rotation'])
        except Exception:
            links.new(e2r.outputs[0], iop.inputs['Rotation'])
    else:
        try:
            links.new(na.outputs[0], iop.inputs['Rotation'])
        except Exception:
            pass
    links.new(iop.outputs['Instances'], gout.inputs['Geometry'])


def _rebuild_instancer_mesh(instancer_obj, transforms):
    """
    Rebuild the instancer mesh from all accumulated transforms.
    transforms: list of (x, y, z, ex, ey, ez)  — XYZ euler, radians.
    """
    mesh = instancer_obj.data
    n = len(transforms)
    mesh.clear_geometry()

    if n == 0:
        return

    flat_pos = []
    flat_rot = []
    for (x, y, z, ex, ey, ez) in transforms:
        flat_pos += [x, y, z]
        flat_rot += [ex, ey, ez]

    mesh.vertices.add(n)
    mesh.vertices.foreach_set("co", flat_pos)

    existing = mesh.attributes.get("rot")
    if existing is not None:
        mesh.attributes.remove(existing)
    attr = mesh.attributes.new("rot", 'FLOAT_VECTOR', 'POINT')
    attr.data.foreach_set("vector", flat_rot)
    mesh.update()


def _get_or_create_instancer(depot_path: str, source_obj, foliage_root):
    """Return (or create) the GN instancer object for this depot path."""
    marker = "_inst_" + depot_path
    for obj in foliage_root.objects:
        if obj.get("_depot_path") == marker:
            return obj

    safe = "fi_" + depot_path.replace("\\", "_").replace("/", "_").replace(":", "_")[-55:]
    mesh = bpy.data.meshes.new(safe)
    obj  = bpy.data.objects.new(safe, mesh)
    obj["_depot_path"] = marker
    obj["_is_foliage_instancer"] = True
    foliage_root.objects.link(obj)

    ng  = bpy.data.node_groups.new(safe + "_GN", 'GeometryNodeTree')
    mod = obj.modifiers.new("FoliageInstancer", 'NODES')
    mod.node_group = ng
    _build_foliage_gn_tree(ng, source_obj)

    return obj


# ---------------------------------------------------------------------------
# Load one .flyr cell
# ---------------------------------------------------------------------------

def load_foliage_cell(game_rel_path: str, foliage_root, context):
    from ..CR2W.CR2W_reader import load_foliage

    abs_path = resolve_flyr_abs_path(game_rel_path)
    if not abs_path:
        log.warning("Could not resolve flyr path: %s", game_rel_path)
        return False, 0

    try:
        level = load_foliage(abs_path)
    except Exception:
        log.exception("Failed to load: %s", abs_path)
        return False, 0

    foliage_chunk = getattr(level, "Foliage", None)
    if foliage_chunk is None:
        log.warning("Foliage file has no Foliage chunk: %s", abs_path)
        return False, 0

    all_tree_data = []
    if hasattr(foliage_chunk, "Trees"):
        all_tree_data += list(foliage_chunk.Trees.elements)
    if hasattr(foliage_chunk, "Grasses"):
        all_tree_data += list(foliage_chunk.Grasses.elements)

    # Batch transforms by depot_path
    new_by_type: dict = {}
    for tree_data in all_tree_data:
        try:
            depot_path = tree_data.TreeType.DepotPath
        except Exception:
            continue
        if not depot_path:
            continue

        instances = []
        if hasattr(tree_data, "TreeCollection") and hasattr(tree_data.TreeCollection, "elements"):
            instances = tree_data.TreeCollection.elements

        for inst in instances:
            try:
                q = Euler((radians(float(inst.Yaw)),
                           radians(float(inst.Pitch)),
                           radians(float(inst.Roll))), 'YXZ').to_quaternion()
                e = q.to_euler('XYZ')
                new_by_type.setdefault(depot_path, []).append(
                    (float(inst.X), float(inst.Y), float(inst.Z), e.x, e.y, e.z)
                )
            except Exception:
                continue

    instance_count = sum(len(v) for v in new_by_type.values())

    root_transforms = _get_root_transform_bucket(foliage_root, create=True)

    for depot_path, new_transforms in new_by_type.items():
        source = _get_or_import_source_mesh(depot_path, foliage_root)
        instancer = _get_or_create_instancer(depot_path, source, foliage_root)

        combined = root_transforms.get(depot_path, []) + new_transforms
        root_transforms[depot_path] = combined
        _rebuild_instancer_mesh(instancer, combined)

    return True, instance_count


# ---------------------------------------------------------------------------
# Visibility / unload
# ---------------------------------------------------------------------------

def toggle_foliage_visibility(foliage_root):
    foliage_root.hide_viewport = not foliage_root.hide_viewport


def unload_foliage(foliage_root):
    root_key = _foliage_root_state_key(foliage_root)
    _type_transforms.pop(root_key, None)

    for obj in list(foliage_root.objects):
        mesh = obj.data if obj.type == 'MESH' else None
        bpy.data.objects.remove(obj, do_unlink=True)
        if mesh and mesh.users == 0:
            bpy.data.meshes.remove(mesh)
    bpy.data.collections.remove(foliage_root)
