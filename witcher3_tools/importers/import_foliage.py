"""Foliage loading for ``.flyr`` resources."""
import os
import json
import math
import logging
from dataclasses import dataclass
from typing import Iterable, Sequence

import bpy

from ..foliage_core import (
    Bounds2D,
    CELL_SIZE,
    decode_foliage_instance_transform,
    foliage_cells_for_bounds,
    point_in_bounds,
    terrain_tile_bounds,
)

log = logging.getLogger(__name__)

# Per-root cache: owner -> depot path -> transforms.
_type_transforms: dict = {}

_OWNER_KEYS_PROP = "_foliage_owner_keys"
_LOADED_OWNERS_PROP = "_loaded_foliage_owners"
_OWNER_ATTRIBUTE = "foliage_owner"
_ROTATION_ATTRIBUTE = "rot"
_SCALE_ATTRIBUTE = "scale"
_LEGACY_OWNER = "__legacy__"

FOLIAGE_SOURCE_MODE_FULL = "FULL"
FOLIAGE_SOURCE_MODE_PROXY = "PROXY"

_SOURCE_MODE_PROP = "_foliage_source_mode"
_SOURCE_CONTEXT_PROP = "_foliage_source_context_path"
_SOURCE_KIND_PROP = "_foliage_source_kind"
_SOURCE_READY_PROP = "_foliage_source_ready"
_PROXY_SOURCE_MARKER = "_foliage_shared_proxy_source"
_PROXY_KIND_PROP = "_foliage_proxy_kind"
_FALLBACK_HIDDEN_PROP = "_foliage_fallback_hidden"
_SOURCE_NODE_NAME = "Foliage Source"
_VIEWPORT_POSITION_NODE_NAME = "W3 Viewport Position"
_VIEWPORT_DISTANCE_NODE_NAME = "W3 Viewport Distance"
_VIEWPORT_CULL_ENABLED_NODE_NAME = "W3 Viewport Cull Enabled"
_VIEWPORT_DENSITY_NODE_NAME = "W3 Ground Cover Density"
_VIEWPORT_DENSITY_ENABLED_NODE_NAME = "W3 Ground Cover Density Enabled"
_VIEWPORT_SOURCE_NODE_NAME = "W3 Fast Viewport Source"
_VIEWPORT_FAST_MATERIAL_ENABLED_NODE_NAME = "W3 Fast Viewport Material Enabled"
_VIEWPORT_MATERIAL_PROP = "_w3_foliage_viewport_material"
_VIEWPORT_MATERIAL_SOURCE_PROP = "_w3_foliage_viewport_material_source"
_VIEWPORT_SOURCE_PROP = "_w3_foliage_viewport_source"
_LEGACY_VIEWPORT_DRIVER_PROP = "_w3_foliage_viewport_driver"
_FOLIAGE_GN_VERSION = 8

_PROXY_KIND_GRASS = "grass"
_PROXY_KIND_FLOWER = "flower"
_PROXY_KIND_REED = "reed"
_PROXY_KIND_SHRUB = "shrub"
_PROXY_KIND_CONIFER = "conifer"
_PROXY_KIND_TREE = "tree"

# Viewer mode hydrates only dominant source types.
FOLIAGE_VIEWER_GROUND_SOURCE_LIMIT = 8
FOLIAGE_VIEWER_TREE_SOURCE_LIMIT = 6

_DEFAULT_VIEWPORT_DISTANCE = 75.0
_DEFAULT_VIEWPORT_GROUND_DENSITY = 0.25


def _normalise_source_mode(source_mode: str) -> str:
    mode = str(source_mode or FOLIAGE_SOURCE_MODE_FULL).strip().upper()
    if mode not in {FOLIAGE_SOURCE_MODE_FULL, FOLIAGE_SOURCE_MODE_PROXY}:
        raise ValueError("Foliage source_mode must be 'FULL' or 'PROXY'")
    return mode


@dataclass(frozen=True)
class FoliageLoadResult:
    """Result of a tile/cell-scoped foliage load."""

    requested_cells: tuple[str, ...]
    loaded_cells: tuple[str, ...]
    skipped_cells: tuple[str, ...]
    failed_cells: tuple[str, ...]
    instance_count: int
    affected_types: tuple[str, ...]
    hydrated_types: tuple[str, ...] = ()
    hidden_fallback_types: tuple[str, ...] = ()

    @property
    def success(self) -> bool:
        return bool(self.requested_cells) and not self.failed_cells


@dataclass(frozen=True)
class FoliageHydrationResult:
    """Result of progressively replacing proxy instances with real sources."""

    requested_types: tuple[str, ...]
    hydrated_types: tuple[str, ...]
    failed_types: tuple[str, ...]
    remaining_types: tuple[str, ...]

    @property
    def success(self) -> bool:
        return not self.failed_types and not self.remaining_types


def _json_string_list(owner, prop_name: str) -> list[str]:
    try:
        raw = json.loads(owner.get(prop_name, "[]"))
    except Exception:
        return []
    if not isinstance(raw, list):
        return []
    return [str(value) for value in raw]


def _set_json_string_list(owner, prop_name: str, values: Iterable[str]) -> None:
    owner[prop_name] = json.dumps([str(value) for value in values], separators=(",", ":"))


def _owner_keys(foliage_root) -> list[str]:
    return _json_string_list(foliage_root, _OWNER_KEYS_PROP)


def _ensure_owner_index(foliage_root, owner_key: str) -> int:
    return _ensure_owner_indices(foliage_root, (owner_key,))[str(owner_key)]


def _ensure_owner_indices(foliage_root, owner_keys: Iterable[str]) -> dict[str, int]:
    """Resolve persistent owner ids with at most one collection property write."""

    keys = _owner_keys(foliage_root)
    indices = {key: index for index, key in enumerate(keys)}
    changed = False
    for owner_key in owner_keys:
        owner_key = str(owner_key)
        if owner_key in indices:
            continue
        indices[owner_key] = len(keys)
        keys.append(owner_key)
        changed = True
    if changed:
        _set_json_string_list(foliage_root, _OWNER_KEYS_PROP, keys)
    return indices


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
        bucket = _restore_root_transform_bucket(foliage_root)
        _type_transforms[root_key] = bucket
    return bucket


# ---------------------------------------------------------------------------
# Path / grid helpers
# ---------------------------------------------------------------------------

def _path_is_under_root(path: str, root: str) -> bool:
    if not path or not root:
        return False
    try:
        path_key = os.path.normcase(os.path.abspath(os.path.normpath(path)))
        root_key = os.path.normcase(os.path.abspath(os.path.normpath(root)))
        return os.path.commonpath((path_key, root_key)) == root_key
    except (OSError, ValueError):
        return False


def _configured_depot_roots(context=None, *, world_path: str = "") -> list[str]:
    """Return roots for this source, preserving REDkit depot-first priority."""

    context = context or getattr(bpy, "context", None)
    normal_uncook = ""
    try:
        from .. import get_uncook_path
        normal_uncook = str(get_uncook_path(context) or "").strip()
    except Exception:
        pass

    try:
        from .. import ADDON_NAME
        prefs = context.preferences.addons[ADDON_NAME].preferences
    except Exception:
        prefs = None
    redkit_depot = str(getattr(prefs, "redkit_depot_path", "") or "").strip() if prefs else ""
    redkit_uncooked = str(getattr(prefs, "redkit_uncooked_path", "") or "").strip() if prefs else ""
    redkit_source = bool(
        world_path
        and (
            _path_is_under_root(world_path, redkit_depot)
            or _path_is_under_root(world_path, redkit_uncooked)
        )
    )
    roots = [redkit_depot, redkit_uncooked, normal_uncook] if redkit_source else [normal_uncook]

    unique = []
    seen = set()
    for root in roots:
        norm = os.path.normcase(os.path.normpath(root))
        if norm not in seen:
            seen.add(norm)
            unique.append(root)
    return unique


def _to_game_rel_path(abs_path: str, context=None) -> str:
    norm = abs_path.replace("/", "\\")
    for root in _configured_depot_roots(context, world_path=abs_path):
        root = root.replace("/", "\\").rstrip("\\")
        prefix = root + "\\"
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


def find_all_flyr_keys_in_bundles(
    foliage_prefix: str,
    context=None,
    *,
    world_path: str = "",
) -> dict:
    """Return {cell_key: game_rel_path} for every .flyr found in bundles + disk."""
    prefix_lower = foliage_prefix.lower().rstrip("\\") + "\\"
    result = {}

    # Prefer project and uncooked files over bundles.
    disk_dirs = []
    if os.path.isabs(foliage_prefix) or os.path.splitdrive(foliage_prefix)[0]:
        disk_dirs.append(foliage_prefix)
    else:
        for root in _configured_depot_roots(context, world_path=world_path):
            disk_dirs.append(os.path.join(root, foliage_prefix))
    for disk_dir in disk_dirs:
        if not os.path.isdir(disk_dir):
            continue
        try:
            filenames = os.listdir(disk_dir)
        except OSError:
            continue
        for fname in filenames:
            if not fname.lower().endswith(".flyr"):
                continue
            base = os.path.splitext(fname)[0]
            if not base.lower().startswith("foliage_"):
                continue
            key = base[len("foliage_"):]
            # Absolute paths preserve which configured depot won.
            result.setdefault(key, os.path.join(disk_dir, fname))

    bm = _get_bundle_manager()
    if bm is not None:
        for key in bm.Items:
            kl = key.lower().replace("/", "\\")
            if kl.startswith(prefix_lower) and kl.endswith(".flyr"):
                base = os.path.splitext(os.path.basename(key))[0]
                if base.lower().startswith("foliage_"):
                    result.setdefault(base[len("foliage_"):], key.replace("/", "\\"))
    return result


def count_all_foliage_cells(foliage_prefix: str, context=None, *, world_path: str = "") -> int:
    return len(find_all_flyr_keys_in_bundles(foliage_prefix, context, world_path=world_path))


def resolve_flyr_abs_path(game_rel_path: str, context=None, world_path: str = "") -> str:
    """Resolve one known cell directly, probing disk before bundle extraction."""

    raw = str(game_rel_path or "").strip().strip('"')
    if not raw:
        return None

    candidates = []
    if os.path.isabs(raw) or os.path.splitdrive(raw)[0]:
        candidates.append(raw)
    else:
        rel = raw.replace("/", os.sep).replace("\\", os.sep).lstrip("\\/")
        if world_path and (os.path.isabs(world_path) or os.path.splitdrive(world_path)[0]):
            candidates.append(
                os.path.join(os.path.dirname(world_path), "source_foliage", os.path.basename(rel))
            )
        for root in _configured_depot_roots(context, world_path=world_path):
            candidates.append(os.path.join(root, rel))

    for candidate in candidates:
        if os.path.isfile(candidate):
            return os.path.normpath(candidate)

    from ..CR2W.common_blender import redkit_repo_context, repo_file
    try:
        with redkit_repo_context(world_path or None):
            p = repo_file(raw)
        if p and os.path.isfile(p):
            return p
    except Exception:
        log.exception("Failed to resolve flyr: %s", raw)
    return None


def resolve_foliage_cells_for_bounds(
    foliage_prefix: str,
    bounds: Bounds2D | Sequence[float],
    context=None,
    *,
    world_path: str = "",
) -> list[tuple[str, str]]:
    """Resolve only cells intersecting ``bounds`` without global enumeration."""

    resolved = []
    for cx, cy in foliage_cells_for_bounds(bounds, CELL_SIZE):
        key = cell_key(cx, cy)
        rel_path = game_rel_flyr_path(foliage_prefix, cx, cy)
        abs_path = resolve_flyr_abs_path(rel_path, context, world_path=world_path)
        if abs_path:
            resolved.append((key, abs_path))
    return resolved


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
    coll[_OWNER_KEYS_PROP] = "[]"
    coll[_LOADED_OWNERS_PROP] = "[]"
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


def get_loaded_owners(foliage_root) -> set[str]:
    return set(_json_string_list(foliage_root, _LOADED_OWNERS_PROP))


def count_instances(foliage_root) -> int:
    total = 0
    for obj in foliage_root.objects:
        if obj.get("_is_foliage_instancer") and obj.type == 'MESH':
            total += len(obj.data.vertices)
    return total


def count_loaded_cells(foliage_root) -> int:
    cells = get_loaded_cells(foliage_root)
    for owner_key in get_loaded_owners(foliage_root):
        cells.add(owner_key.rsplit("|", 1)[-1])
    return len(cells)


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


def _is_real_foliage_source(obj) -> bool:
    """Return whether ``obj`` is usable as a hydrated foliage source."""

    if obj is None or obj.get(_SOURCE_KIND_PROP) == FOLIAGE_SOURCE_MODE_PROXY:
        return False
    explicit = obj.get(_SOURCE_READY_PROP)
    if explicit is not None:
        return bool(explicit)
    marker = str(obj.get("_depot_path", "") or "")
    return bool(
        marker.startswith("_src_")
        and obj.type == 'MESH'
        and obj.data is not None
        and len(obj.data.vertices) > 0
    )


def _find_real_source_mesh(depot_path: str, foliage_root):
    marker = "_src_" + depot_path
    for obj in foliage_root.objects:
        if obj.get("_depot_path") == marker and _is_real_foliage_source(obj):
            return obj
    return None


def _remove_source_object(obj) -> None:
    data = obj.data if getattr(obj, "type", None) == 'MESH' else None
    bpy.data.objects.remove(obj, do_unlink=True)
    if data is not None and data.users == 0:
        bpy.data.meshes.remove(data)


def _foliage_proxy_kind(depot_path: str) -> str:
    """Classify an SRT path into one of a few cheap visual preview shapes."""

    path = str(depot_path or "").lower().replace("/", "\\")
    name = os.path.splitext(os.path.basename(path))[0]
    searchable = f"{path} {name.replace('_', ' ')}"

    if any(word in searchable for word in (
        "flower", "blossom", "dandelion", "poppy", "buttercup",
    )):
        return _PROXY_KIND_FLOWER
    if any(word in searchable for word in (
        "reed", "cattail", "bulrush", "water_rush", "water rush",
    )):
        return _PROXY_KIND_REED
    if any(word in searchable for word in (
        "grass", "fern", "herb", "weed", "nettle", "clover", "heather",
        "moss", "ivy", "groundplant", "ground_plant", "small_plant",
    )):
        return _PROXY_KIND_GRASS
    if any(word in searchable for word in (
        "bush", "shrub", "bramble", "thicket", "hedge", "scrub",
    )):
        return _PROXY_KIND_SHRUB
    if any(word in searchable for word in (
        "pine", "spruce", "fir_", "fir ", "conifer", "larch",
    )):
        return _PROXY_KIND_CONIFER
    if (
        "\\trees\\" in path
        or "\\tree\\" in path
        or any(word in searchable for word in (
            "tree", "birch", "willow", "oak", "alder", "beech", "elm",
            "maple", "rowan", "poplar", "sycamore", "ash_tree",
        ))
    ):
        return _PROXY_KIND_TREE
    # Unknown types default to low vegetation.
    return _PROXY_KIND_SHRUB


def _is_ground_cover(depot_path: str) -> bool:
    return _foliage_proxy_kind(depot_path) not in {_PROXY_KIND_TREE, _PROXY_KIND_CONIFER}


def _foliage_viewport_settings(scene=None):
    scene = scene or getattr(bpy.context, "scene", None)
    settings = getattr(scene, "witcher_file_browser", None)
    return {
        "cull_enabled": bool(getattr(settings, "foliage_viewport_distance_culling", True)),
        "distance": max(
            1.0,
            float(getattr(settings, "foliage_viewport_distance", _DEFAULT_VIEWPORT_DISTANCE)),
        ),
        "ground_density": min(
            1.0,
            max(
                0.01,
                float(
                    getattr(
                        settings,
                        "foliage_viewport_ground_density",
                        _DEFAULT_VIEWPORT_GROUND_DENSITY,
                    )
                ),
            ),
        ),
        "fast_materials": bool(getattr(settings, "foliage_viewport_fast_materials", True)),
    }


def _viewport_density_threshold(density: float) -> float:
    density = min(1.0, max(0.01, float(density)))
    return density * 100.0 - 0.5


def _diffuse_image_from_material(material):
    node_tree = getattr(material, "node_tree", None)
    nodes = list(getattr(node_tree, "nodes", ()) or ())
    image_nodes = [node for node in nodes if getattr(node, "type", "") == 'TEX_IMAGE' and node.image]
    if not image_nodes:
        return None
    for node in image_nodes:
        label = f"{getattr(node, 'name', '')} {getattr(node, 'label', '')}".lower()
        if any(word in label for word in ("diffuse", "base color", "basecolor", "albedo")):
            return node.image
    return image_nodes[0].image


def _get_or_create_fast_viewport_material(source_material):
    source_key = str(getattr(source_material, "name_full", None) or source_material.name)
    for material in bpy.data.materials:
        if (
            material.get(_VIEWPORT_MATERIAL_PROP)
            and str(material.get(_VIEWPORT_MATERIAL_SOURCE_PROP, "")) == source_key
        ):
            return material

    material = bpy.data.materials.new(name=f"{source_material.name} [W3 Viewport]")
    material.use_nodes = True
    material[_VIEWPORT_MATERIAL_PROP] = True
    material[_VIEWPORT_MATERIAL_SOURCE_PROP] = source_key
    try:
        material.diffuse_color = source_material.diffuse_color
    except Exception:
        pass

    nodes = material.node_tree.nodes
    links = material.node_tree.links
    nodes.clear()
    output = nodes.new('ShaderNodeOutputMaterial')
    output.location = (320, 0)
    shader = nodes.new('ShaderNodeBsdfPrincipled')
    shader.location = (40, 0)
    shader.inputs['Roughness'].default_value = 0.8
    links.new(shader.outputs['BSDF'], output.inputs['Surface'])

    image = _diffuse_image_from_material(source_material)
    if image is not None:
        texture = nodes.new('ShaderNodeTexImage')
        texture.location = (-260, 0)
        texture.image = image
        links.new(texture.outputs['Color'], shader.inputs['Base Color'])
        links.new(texture.outputs['Alpha'], shader.inputs['Alpha'])
    else:
        try:
            shader.inputs['Base Color'].default_value = source_material.diffuse_color
        except Exception:
            pass

    try:
        material.surface_render_method = 'DITHERED'
    except Exception:
        try:
            material.blend_method = 'HASHED'
        except Exception:
            pass
    try:
        material.use_transparency_overlap = False
    except Exception:
        pass
    return material


def _hide_foliage_source(source) -> None:
    """Keep source datablocks usable by GN but out of the viewport and render."""

    source.hide_viewport = True
    source.hide_render = True
    try:
        source.hide_select = True
    except Exception:
        pass
    try:
        source.hide_set(True)
    except Exception:
        pass


def _get_or_create_fast_viewport_source(source_obj, foliage_root):
    data = getattr(source_obj, "data", None)
    if data is None:
        return None
    source_materials = [slot.material for slot in getattr(source_obj, "material_slots", ())]
    if not source_materials or not any(source_materials):
        return None

    source_key = str(getattr(source_obj, "name_full", None) or source_obj.name)
    fast_source = next(
        (
            obj for obj in foliage_root.objects
            if str(obj.get(_VIEWPORT_SOURCE_PROP, "")) == source_key
            and getattr(obj, "data", None) is data
        ),
        None,
    )
    if fast_source is None:
        fast_source = bpy.data.objects.new(f"{source_obj.name} [W3 Viewport]", data)
        fast_source[_VIEWPORT_SOURCE_PROP] = source_key
        foliage_root.objects.link(fast_source)

    for slot, source_material in zip(fast_source.material_slots, source_materials):
        slot.link = 'OBJECT'
        slot.material = (
            _get_or_create_fast_viewport_material(source_material)
            if source_material is not None
            else None
        )
    _hide_foliage_source(fast_source)
    return fast_source


def _get_or_create_proxy_source(foliage_root, depot_path: str = ""):
    """Return one invisible placeholder used until a real source is hydrated."""

    for obj in foliage_root.objects:
        if obj.get(_PROXY_SOURCE_MARKER):
            _hide_foliage_source(obj)
            return obj

    mesh = bpy.data.meshes.new("W3_foliage_placeholder")
    mesh.from_pydata(((0.0, 0.0, 0.0),), (), ())
    mesh.update()

    source = bpy.data.objects.new("W3_foliage_placeholder_source", mesh)
    source[_PROXY_SOURCE_MARKER] = True
    source[_PROXY_KIND_PROP] = "hidden"
    source["_is_foliage_source"] = True
    source[_SOURCE_KIND_PROP] = FOLIAGE_SOURCE_MODE_PROXY
    source[_SOURCE_READY_PROP] = False
    foliage_root.objects.link(source)
    _hide_foliage_source(source)
    return source


def _get_or_import_source_mesh(
    depot_path: str,
    foliage_root,
    source_context_path: str = "",
    context=None,
):
    """Return the single LOD0 source mesh for this depot path, importing if needed."""
    marker = "_src_" + depot_path
    existing = _find_real_source_mesh(depot_path, foliage_root)
    if existing is not None:
        _hide_foliage_source(existing)
        return existing

    # Discard failed legacy sources so hydration can retry.
    for obj in list(foliage_root.objects):
        if obj.get("_depot_path") == marker:
            _remove_source_object(obj)

    from ..importers.import_helpers import meshPath
    from ..importers.import_blender_fun import _import_foliage_mesh
    from .. import get_W3_FOLIAGE_PATH

    mp = meshPath(
        meshName=depot_path,
        fbx_uncook_path=get_W3_FOLIAGE_PATH(context or getattr(bpy, "context", None)),
    )
    mp.type = "mesh_foliage"

    before = {o.as_pointer() for o in bpy.data.objects}
    try:
        from ..CR2W.common_blender import redkit_repo_context
        with redkit_repo_context(source_context_path or None):
            _import_foliage_mesh(mp)
    except Exception:
        log.exception("Failed to import foliage type: %s", depot_path)

    new_objects = [o for o in bpy.data.objects if o.as_pointer() not in before]
    source = _pick_best_mesh(new_objects)

    if source is None or source.data is None or len(source.data.vertices) == 0:
        # Keep the instancer on the shared proxy and allow a later retry.
        if source is not None:
            _remove_source_object(source)
        return None

    source["_depot_path"] = marker
    source["_is_foliage_source"] = True
    source[_SOURCE_KIND_PROP] = FOLIAGE_SOURCE_MODE_FULL
    source[_SOURCE_READY_PROP] = True
    _hide_foliage_source(source)

    # Move to foliage root (remove from any scene collection it landed in)
    for c in list(source.users_collection):
        c.objects.unlink(source)
    foliage_root.objects.link(source)

    return source


# ---------------------------------------------------------------------------
# GN instancer: build tree + rebuild mesh
# ---------------------------------------------------------------------------

def _normalise_transform(transform) -> tuple[float, ...]:
    values = tuple(float(value) for value in transform)
    if len(values) == 6:
        return values + (1.0, 1.0, 1.0)
    if len(values) != 9:
        raise ValueError("Foliage transforms must have 6 or 9 components")
    return values


def _mesh_vector_attribute(mesh, name: str, count: int, default) -> list[float]:
    attr = mesh.attributes.get(name)
    if attr is None or len(attr.data) != count:
        return list(default) * count
    values = [0.0] * (count * 3)
    try:
        attr.data.foreach_get("vector", values)
        return values
    except Exception:
        result = []
        for item in attr.data:
            vector = getattr(item, "vector", default)
            result.extend((float(vector[0]), float(vector[1]), float(vector[2])))
        return result


def _mesh_owner_attribute(mesh, count: int) -> list[int]:
    attr = mesh.attributes.get(_OWNER_ATTRIBUTE)
    if attr is None or len(attr.data) != count:
        return [-1] * count
    values = [0] * count
    try:
        attr.data.foreach_get("value", values)
        return [int(value) for value in values]
    except Exception:
        return [int(getattr(item, "value", -1)) for item in attr.data]


def _restore_root_transform_bucket(foliage_root) -> dict:
    """Rehydrate tile/cell ownership from persistent instancer attributes."""

    keys = _owner_keys(foliage_root)
    owners = {key: {} for key in get_loaded_owners(foliage_root)}
    owners.update({key: {} for key in get_loaded_cells(foliage_root)})
    legacy_index = None

    for obj in foliage_root.objects:
        if not obj.get("_is_foliage_instancer") or obj.type != 'MESH' or obj.data is None:
            continue
        marker = str(obj.get("_depot_path", "") or "")
        depot_path = marker[len("_inst_"):] if marker.startswith("_inst_") else marker
        if not depot_path:
            continue
        mesh = obj.data
        count = len(mesh.vertices)
        if count == 0:
            continue

        positions = [0.0] * (count * 3)
        try:
            mesh.vertices.foreach_get("co", positions)
        except Exception:
            positions = [component for vertex in mesh.vertices for component in vertex.co[:]]
        rotations = _mesh_vector_attribute(mesh, _ROTATION_ATTRIBUTE, count, (0.0, 0.0, 0.0))
        scales = _mesh_vector_attribute(mesh, _SCALE_ATTRIBUTE, count, (1.0, 1.0, 1.0))
        owner_indices = _mesh_owner_attribute(mesh, count)

        for index in range(count):
            owner_index = owner_indices[index]
            if not 0 <= owner_index < len(keys):
                if legacy_index is None:
                    legacy_index = _ensure_owner_index(foliage_root, _LEGACY_OWNER)
                    keys = _owner_keys(foliage_root)
                    owners.setdefault(_LEGACY_OWNER, {})
                owner_index = legacy_index
            owner_key = keys[owner_index]
            transform = tuple(
                positions[index * 3:index * 3 + 3]
                + rotations[index * 3:index * 3 + 3]
                + scales[index * 3:index * 3 + 3]
            )
            owners.setdefault(owner_key, {}).setdefault(depot_path, []).append(transform)
    return owners


def _transforms_for_depot(root_transforms: dict, depot_path: str, owner_indices_by_key: dict):
    combined = []
    owner_indices = []
    for owner_key, by_type in root_transforms.items():
        transforms = by_type.get(depot_path, ())
        if not transforms:
            continue
        owner_index = owner_indices_by_key[owner_key]
        for transform in transforms:
            combined.append(transform)
            owner_indices.append(owner_index)
    return combined, owner_indices


def _build_foliage_gn_tree(
    ng,
    source_obj,
    *,
    viewport_position=(0.0, 0.0, 0.0),
    viewport_distance: float = _DEFAULT_VIEWPORT_DISTANCE,
    viewport_cull_enabled: bool = True,
    viewport_ground_density: float = _DEFAULT_VIEWPORT_GROUND_DENSITY,
    is_ground_cover: bool = True,
    fast_viewport_source=None,
    fast_viewport_material_enabled: bool = True,
):
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
        na.inputs["Name"].default_value = _ROTATION_ATTRIBUTE
    except Exception:
        na.inputs[0].default_value = _ROTATION_ATTRIBUTE

    # Preserve packed uniform scale.
    scale_attr = nodes.new('GeometryNodeInputNamedAttribute')
    scale_attr.location = (-500, -275)
    for value in ('FLOAT_VECTOR', 'VECTOR'):
        try:
            scale_attr.data_type = value
            break
        except Exception:
            pass
    try:
        scale_attr.inputs["Name"].default_value = _SCALE_ATTRIBUTE
    except Exception:
        scale_attr.inputs[0].default_value = _SCALE_ATTRIBUTE

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
    oi.name = _SOURCE_NODE_NAME
    oi.label = _SOURCE_NODE_NAME
    try:
        oi["_is_foliage_source_node"] = True
    except Exception:
        pass
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

    # Cull only in the viewport; renders keep every authored point.
    is_viewport = nodes.new('GeometryNodeIsViewport')
    is_viewport.location = (-500, 500)
    position = nodes.new('GeometryNodeInputPosition')
    position.location = (-900, 300)
    view_position = nodes.new('ShaderNodeCombineXYZ')
    view_position.name = _VIEWPORT_POSITION_NODE_NAME
    view_position.label = _VIEWPORT_POSITION_NODE_NAME
    view_position.location = (-900, 450)
    for socket, value in zip(view_position.inputs, viewport_position):
        socket.default_value = float(value)
    distance = nodes.new('ShaderNodeVectorMath')
    distance.operation = 'DISTANCE'
    distance.location = (-700, 350)
    outside = nodes.new('ShaderNodeMath')
    outside.name = _VIEWPORT_DISTANCE_NODE_NAME
    outside.label = _VIEWPORT_DISTANCE_NODE_NAME
    outside.operation = 'GREATER_THAN'
    outside.inputs[1].default_value = float(viewport_distance)
    outside.location = (-500, 300)
    viewport_cull = nodes.new('FunctionNodeBooleanMath')
    viewport_cull.operation = 'AND'
    viewport_cull.location = (-300, 350)
    cull_enabled = nodes.new('FunctionNodeBooleanMath')
    cull_enabled.name = _VIEWPORT_CULL_ENABLED_NODE_NAME
    cull_enabled.label = _VIEWPORT_CULL_ENABLED_NODE_NAME
    cull_enabled.operation = 'AND'
    cull_enabled.inputs[1].default_value = bool(viewport_cull_enabled)
    cull_enabled.location = (-150, 300)
    index = nodes.new('GeometryNodeInputIndex')
    index.location = (-700, 700)
    modulo = nodes.new('ShaderNodeMath')
    modulo.operation = 'MODULO'
    modulo.inputs[1].default_value = 100.0
    modulo.location = (-500, 700)
    density = nodes.new('ShaderNodeMath')
    density.name = _VIEWPORT_DENSITY_NODE_NAME
    density.label = _VIEWPORT_DENSITY_NODE_NAME
    density.operation = 'GREATER_THAN'
    density.inputs[1].default_value = _viewport_density_threshold(viewport_ground_density)
    density.location = (-300, 700)
    viewport_density = nodes.new('FunctionNodeBooleanMath')
    viewport_density.operation = 'AND'
    viewport_density.location = (-100, 650)
    density_enabled = nodes.new('FunctionNodeBooleanMath')
    density_enabled.name = _VIEWPORT_DENSITY_ENABLED_NODE_NAME
    density_enabled.label = _VIEWPORT_DENSITY_ENABLED_NODE_NAME
    density_enabled.operation = 'AND'
    density_enabled.inputs[1].default_value = bool(is_ground_cover)
    density_enabled.location = (50, 550)
    viewport_remove = nodes.new('FunctionNodeBooleanMath')
    viewport_remove.operation = 'OR'
    viewport_remove.location = (100, 300)
    delete = nodes.new('GeometryNodeDeleteGeometry')
    delete.domain = 'POINT'
    delete.location = (250, 100)

    links.new(position.outputs['Position'], distance.inputs[0])
    links.new(view_position.outputs['Vector'], distance.inputs[1])
    links.new(distance.outputs['Value'], outside.inputs[0])
    links.new(outside.outputs[0], viewport_cull.inputs[0])
    links.new(is_viewport.outputs[0], viewport_cull.inputs[1])
    links.new(viewport_cull.outputs[0], cull_enabled.inputs[0])
    links.new(index.outputs['Index'], modulo.inputs[0])
    links.new(modulo.outputs[0], density.inputs[0])
    links.new(density.outputs[0], viewport_density.inputs[0])
    links.new(is_viewport.outputs[0], viewport_density.inputs[1])
    links.new(viewport_density.outputs[0], density_enabled.inputs[0])
    links.new(cull_enabled.outputs[0], viewport_remove.inputs[0])
    links.new(density_enabled.outputs[0], viewport_remove.inputs[1])
    links.new(gin.outputs['Geometry'], delete.inputs['Geometry'])
    links.new(viewport_remove.outputs[0], delete.inputs['Selection'])
    links.new(delete.outputs['Geometry'], iop.inputs['Points'])

    # Swap to cheap object-linked materials only in the viewport.
    fast_oi = nodes.new('GeometryNodeObjectInfo')
    fast_oi.name = _VIEWPORT_SOURCE_NODE_NAME
    fast_oi.label = _VIEWPORT_SOURCE_NODE_NAME
    fast_oi.location = (-100, -450)
    fast_oi.inputs['Object'].default_value = fast_viewport_source
    try:
        fast_oi.transform_space = 'ORIGINAL'
    except Exception:
        pass
    material_switch = nodes.new('GeometryNodeSwitch')
    material_switch.input_type = 'GEOMETRY'
    material_switch.location = (100, -300)
    fast_enabled = nodes.new('FunctionNodeBooleanMath')
    fast_enabled.name = _VIEWPORT_FAST_MATERIAL_ENABLED_NODE_NAME
    fast_enabled.label = _VIEWPORT_FAST_MATERIAL_ENABLED_NODE_NAME
    fast_enabled.operation = 'AND'
    fast_enabled.inputs[1].default_value = bool(
        fast_viewport_material_enabled and fast_viewport_source is not None
    )
    fast_enabled.location = (-100, -550)
    links.new(is_viewport.outputs[0], fast_enabled.inputs[0])
    links.new(fast_enabled.outputs[0], material_switch.inputs['Switch'])
    links.new(oi.outputs['Geometry'], material_switch.inputs['False'])
    links.new(fast_oi.outputs['Geometry'], material_switch.inputs['True'])
    links.new(material_switch.outputs['Output'], iop.inputs['Instance'])
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
    try:
        links.new(scale_attr.outputs[0], iop.inputs['Scale'])
    except Exception:
        pass
    links.new(iop.outputs['Instances'], gout.inputs['Geometry'])


def _set_instancer_viewport_source(
    instancer_obj,
    source_obj,
    foliage_root,
    enabled: bool,
) -> None:
    fast_source = _get_or_create_fast_viewport_source(source_obj, foliage_root)
    for modifier in instancer_obj.modifiers:
        if modifier.type != 'NODES' or modifier.node_group is None:
            continue
        nodes = modifier.node_group.nodes
        source_node = nodes.get(_VIEWPORT_SOURCE_NODE_NAME)
        fast_enabled = nodes.get(_VIEWPORT_FAST_MATERIAL_ENABLED_NODE_NAME)
        if source_node is not None:
            source_node.inputs['Object'].default_value = fast_source
        if fast_enabled is not None:
            fast_enabled.inputs[1].default_value = bool(enabled and fast_source is not None)
        modifier.node_group.update_tag()


def _set_instancer_viewport_settings(
    instancer_obj,
    settings=None,
    depot_path: str = "",
) -> None:
    settings = settings or _foliage_viewport_settings()
    depot_path = depot_path or _instancer_depot_path(instancer_obj)

    for modifier in instancer_obj.modifiers:
        if modifier.type != 'NODES' or modifier.node_group is None:
            continue
        nodes = modifier.node_group.nodes
        distance = nodes.get(_VIEWPORT_DISTANCE_NODE_NAME)
        cull_enabled = nodes.get(_VIEWPORT_CULL_ENABLED_NODE_NAME)
        density = nodes.get(_VIEWPORT_DENSITY_NODE_NAME)
        density_enabled = nodes.get(_VIEWPORT_DENSITY_ENABLED_NODE_NAME)
        if distance is not None:
            distance.inputs[1].default_value = float(settings["distance"])
        if cull_enabled is not None:
            cull_enabled.inputs[1].default_value = bool(settings["cull_enabled"])
        if density is not None:
            density.inputs[1].default_value = _viewport_density_threshold(
                settings["ground_density"]
            )
        if density_enabled is not None:
            density_enabled.inputs[1].default_value = _is_ground_cover(depot_path)
        modifier.node_group.update_tag()


def _instancer_source_nodes(instancer_obj) -> list:
    tagged = []
    fallback = []
    for modifier in instancer_obj.modifiers:
        if modifier.type != 'NODES' or modifier.node_group is None:
            continue
        for node in modifier.node_group.nodes:
            if getattr(node, "bl_idname", "") != 'GeometryNodeObjectInfo':
                continue
            fallback.append(node)
            try:
                is_source = bool(node.get("_is_foliage_source_node"))
            except Exception:
                is_source = False
            if is_source or node.name == _SOURCE_NODE_NAME:
                tagged.append(node)
    return tagged or fallback


def _get_instancer_source(instancer_obj):
    for node in _instancer_source_nodes(instancer_obj):
        socket = node.inputs.get('Object')
        if socket is not None:
            return socket.default_value
    return None


def _sync_instancer_source_visibility(instancer_obj, source_obj) -> bool:
    """Show hydrated vegetation and completely hide diagnostic fallback types."""

    hidden = not _is_real_foliage_source(source_obj)
    instancer_obj.hide_viewport = hidden
    instancer_obj.hide_render = hidden
    try:
        instancer_obj.hide_select = hidden
    except Exception:
        pass
    try:
        instancer_obj.hide_set(hidden)
    except Exception:
        pass
    instancer_obj[_FALLBACK_HIDDEN_PROP] = hidden
    return hidden


def _set_instancer_source(instancer_obj, source_obj) -> bool:
    """Swap Object Info in-place so point ownership and meshes stay intact."""

    changed = False
    for node in _instancer_source_nodes(instancer_obj):
        socket = node.inputs.get('Object')
        if socket is None:
            continue
        socket.default_value = source_obj
        changed = True
        try:
            node.id_data.update_tag()
        except Exception:
            pass
    if changed:
        mode = (
            FOLIAGE_SOURCE_MODE_FULL
            if _is_real_foliage_source(source_obj)
            else FOLIAGE_SOURCE_MODE_PROXY
        )
        instancer_obj[_SOURCE_MODE_PROP] = mode
        _sync_instancer_source_visibility(instancer_obj, source_obj)
        try:
            instancer_obj.update_tag()
        except Exception:
            pass
    return changed


def _rebuild_instancer_mesh(instancer_obj, transforms, owner_indices=None):
    """
    Rebuild the instancer mesh from all accumulated transforms.
    transforms: list of (x, y, z, ex, ey, ez, sx, sy, sz), radians + scale.
    """
    mesh = instancer_obj.data
    transforms = [_normalise_transform(transform) for transform in transforms]
    n = len(transforms)
    if owner_indices is None:
        owner_indices = [-1] * n
    elif len(owner_indices) != n:
        raise ValueError("Foliage owner id count does not match transform count")
    mesh.clear_geometry()

    if n == 0:
        mesh.update()
        return

    flat_pos = [value for transform in transforms for value in transform[0:3]]
    flat_rot = [value for transform in transforms for value in transform[3:6]]
    flat_scale = [value for transform in transforms for value in transform[6:9]]

    mesh.vertices.add(n)
    mesh.vertices.foreach_set("co", flat_pos)

    for name in (_ROTATION_ATTRIBUTE, _SCALE_ATTRIBUTE, _OWNER_ATTRIBUTE):
        existing = mesh.attributes.get(name)
        if existing is not None:
            mesh.attributes.remove(existing)
    rotation_attr = mesh.attributes.new(_ROTATION_ATTRIBUTE, 'FLOAT_VECTOR', 'POINT')
    rotation_attr.data.foreach_set("vector", flat_rot)
    scale_attr = mesh.attributes.new(_SCALE_ATTRIBUTE, 'FLOAT_VECTOR', 'POINT')
    scale_attr.data.foreach_set("vector", flat_scale)
    owner_attr = mesh.attributes.new(_OWNER_ATTRIBUTE, 'INT', 'POINT')
    owner_attr.data.foreach_set("value", owner_indices)
    mesh.update()


def _get_or_create_instancer(
    depot_path: str,
    source_obj,
    foliage_root,
    source_context_path: str = "",
):
    """Return (or create) the GN instancer object for this depot path."""
    viewport_settings = _foliage_viewport_settings()
    fast_source = _get_or_create_fast_viewport_source(source_obj, foliage_root)
    marker = "_inst_" + depot_path
    for obj in foliage_root.objects:
        if obj.get("_depot_path") == marker:
            if int(obj.get("_foliage_gn_version", 0) or 0) < _FOLIAGE_GN_VERSION:
                modifier = next((mod for mod in obj.modifiers if mod.type == 'NODES'), None)
                old_group = modifier.node_group if modifier is not None else None
                node_group = bpy.data.node_groups.new(
                    obj.name + f"_GN_v{_FOLIAGE_GN_VERSION}",
                    'GeometryNodeTree',
                )
                if modifier is None:
                    modifier = obj.modifiers.new("FoliageInstancer", 'NODES')
                modifier.node_group = node_group
                _build_foliage_gn_tree(
                    node_group,
                    source_obj,
                    viewport_distance=viewport_settings["distance"],
                    viewport_cull_enabled=viewport_settings["cull_enabled"],
                    viewport_ground_density=viewport_settings["ground_density"],
                    is_ground_cover=_is_ground_cover(depot_path),
                    fast_viewport_source=fast_source,
                    fast_viewport_material_enabled=viewport_settings["fast_materials"],
                )
                if old_group is not None and old_group.users == 0:
                    bpy.data.node_groups.remove(old_group)
                obj["_foliage_gn_version"] = _FOLIAGE_GN_VERSION
            _set_instancer_source(obj, source_obj)
            _set_instancer_viewport_source(
                obj,
                source_obj,
                foliage_root,
                viewport_settings["fast_materials"],
            )
            _set_instancer_viewport_settings(
                obj,
                settings=viewport_settings,
                depot_path=depot_path,
            )
            if source_context_path:
                obj[_SOURCE_CONTEXT_PROP] = str(source_context_path)
            return obj

    safe = "fi_" + depot_path.replace("\\", "_").replace("/", "_").replace(":", "_")[-55:]
    mesh = bpy.data.meshes.new(safe)
    obj  = bpy.data.objects.new(safe, mesh)
    obj["_depot_path"] = marker
    obj["_is_foliage_instancer"] = True
    obj["_foliage_gn_version"] = _FOLIAGE_GN_VERSION
    foliage_root.objects.link(obj)

    ng  = bpy.data.node_groups.new(safe + "_GN", 'GeometryNodeTree')
    mod = obj.modifiers.new("FoliageInstancer", 'NODES')
    mod.node_group = ng
    _build_foliage_gn_tree(
        ng,
        source_obj,
        viewport_distance=viewport_settings["distance"],
        viewport_cull_enabled=viewport_settings["cull_enabled"],
        viewport_ground_density=viewport_settings["ground_density"],
        is_ground_cover=_is_ground_cover(depot_path),
        fast_viewport_source=fast_source,
        fast_viewport_material_enabled=viewport_settings["fast_materials"],
    )
    _set_instancer_source(obj, source_obj)
    if source_context_path:
        obj[_SOURCE_CONTEXT_PROP] = str(source_context_path)

    return obj


def apply_foliage_viewport_settings(scene=None, position=None) -> int:
    settings = _foliage_viewport_settings(scene)
    updated = 0
    for foliage_root in bpy.data.collections:
        if not foliage_root.get("_is_foliage_root"):
            continue
        for obj in list(foliage_root.objects):
            if obj.get(_LEGACY_VIEWPORT_DRIVER_PROP):
                bpy.data.objects.remove(obj, do_unlink=True)
        for instancer in list(foliage_root.objects):
            if not instancer.get("_is_foliage_instancer"):
                continue
            depot_path = _instancer_depot_path(instancer)
            source = _get_instancer_source(instancer)
            if not depot_path or source is None:
                continue
            instancer = _get_or_create_instancer(
                depot_path,
                source,
                foliage_root,
                str(instancer.get(_SOURCE_CONTEXT_PROP, "") or ""),
            )
            _set_instancer_viewport_source(
                instancer,
                source,
                foliage_root,
                settings["fast_materials"],
            )
            _set_instancer_viewport_settings(
                instancer,
                settings,
                depot_path,
            )
            updated += 1
    if position is not None:
        update_foliage_viewport_position(position, scene, min_distance=0.0)
    return updated


def update_foliage_viewport_position(position, scene=None, *, min_distance: float = 1.0) -> int:
    if position is None:
        return 0
    settings = _foliage_viewport_settings(scene)
    if not settings["cull_enabled"]:
        return 0
    updated = 0
    min_distance_sq = float(min_distance) ** 2
    for foliage_root in bpy.data.collections:
        if not foliage_root.get("_is_foliage_root"):
            continue
        for instancer in foliage_root.objects:
            if not instancer.get("_is_foliage_instancer"):
                continue
            if instancer.hide_viewport or instancer.get(_FALLBACK_HIDDEN_PROP, False):
                continue
            origin = getattr(
                getattr(instancer, "matrix_world", None),
                "translation",
                (0.0, 0.0, 0.0),
            )
            target = tuple(float(position[index]) - float(origin[index]) for index in range(3))
            for modifier in instancer.modifiers:
                if modifier.type != 'NODES' or modifier.node_group is None:
                    continue
                node = modifier.node_group.nodes.get(_VIEWPORT_POSITION_NODE_NAME)
                if node is None:
                    continue
                previous = tuple(float(socket.default_value) for socket in node.inputs[:3])
                if sum((a - b) ** 2 for a, b in zip(previous, target)) < min_distance_sq:
                    continue
                for socket, value in zip(node.inputs, target):
                    socket.default_value = value
                updated += 1
    return updated


def _find_instancer(depot_path: str, foliage_root):
    marker = "_inst_" + depot_path
    for obj in foliage_root.objects:
        if obj.get("_depot_path") == marker:
            return obj
    return None


def _instancer_depot_path(instancer_obj) -> str:
    marker = str(instancer_obj.get("_depot_path", "") or "")
    return marker[len("_inst_"):] if marker.startswith("_inst_") else ""


def _foliage_type_instance_counts(root_transforms: dict) -> dict[str, int]:
    counts = {}
    for by_type in root_transforms.values():
        for depot_path, transforms in by_type.items():
            count = len(transforms)
            if count:
                counts[depot_path] = counts.get(depot_path, 0) + count
    return counts


def _viewer_source_priority(
    root_transforms: dict,
    *,
    ground_limit: int = FOLIAGE_VIEWER_GROUND_SOURCE_LIMIT,
    tree_limit: int = FOLIAGE_VIEWER_TREE_SOURCE_LIMIT,
) -> tuple[str, ...]:
    """Choose a bounded set of dominant real sources for a fast polished view."""

    counts = _foliage_type_instance_counts(root_transforms)
    tree_kinds = {_PROXY_KIND_TREE, _PROXY_KIND_CONIFER}
    trees = []
    ground = []
    for depot_path, count in counts.items():
        item = (-count, depot_path.lower(), depot_path)
        if _foliage_proxy_kind(depot_path) in tree_kinds:
            trees.append(item)
        else:
            ground.append(item)
    trees.sort()
    ground.sort()
    selected = ground[:max(0, int(ground_limit))] + trees[:max(0, int(tree_limit))]
    # Prioritize the most common selected types.
    selected.sort()
    return tuple(item[2] for item in selected)


def _sync_fallback_instancer_visibility(foliage_root) -> tuple[str, ...]:
    hidden = []
    for obj in foliage_root.objects:
        if not obj.get("_is_foliage_instancer"):
            continue
        depot_path = _instancer_depot_path(obj)
        source = _get_instancer_source(obj)
        if _sync_instancer_source_visibility(obj, source):
            if obj.type == 'MESH' and obj.data is not None and len(obj.data.vertices):
                hidden.append(depot_path)
    return tuple(sorted(path for path in hidden if path))


def _rebuild_depot_types(
    foliage_root,
    root_transforms: dict,
    depot_paths: Iterable[str],
    source_context_by_type: dict | None = None,
    source_mode: str = FOLIAGE_SOURCE_MODE_FULL,
    context=None,
) -> None:
    """Upload every affected type once after all requested cells were parsed."""

    source_mode = _normalise_source_mode(source_mode)
    source_context_by_type = source_context_by_type or {}
    owner_indices_by_key = _ensure_owner_indices(foliage_root, root_transforms)
    for depot_path in sorted(set(depot_paths)):
        transforms, owner_indices = _transforms_for_depot(
            root_transforms,
            depot_path,
            owner_indices_by_key,
        )
        if not transforms:
            instancer = _find_instancer(depot_path, foliage_root)
            if instancer is not None:
                _rebuild_instancer_mesh(instancer, (), ())
            continue
        source_context_path = source_context_by_type.get(depot_path, "")
        source = _find_real_source_mesh(depot_path, foliage_root)
        if source is None and source_mode == FOLIAGE_SOURCE_MODE_FULL:
            source = _get_or_import_source_mesh(
                depot_path,
                foliage_root,
                source_context_path,
                context,
            )
        if source is None:
            source = _get_or_create_proxy_source(foliage_root, depot_path)
        instancer = _get_or_create_instancer(
            depot_path,
            source,
            foliage_root,
            source_context_path,
        )
        _rebuild_instancer_mesh(instancer, transforms, owner_indices)


def list_missing_foliage_sources(foliage_root) -> tuple[str, ...]:
    """List placed foliage types whose instancers still use a proxy source."""

    missing = set()
    for obj in foliage_root.objects:
        if not obj.get("_is_foliage_instancer"):
            continue
        depot_path = _instancer_depot_path(obj)
        if not depot_path:
            continue
        if obj.type == 'MESH' and obj.data is not None and len(obj.data.vertices) == 0:
            continue
        if not _is_real_foliage_source(_get_instancer_source(obj)):
            missing.add(depot_path)
    return tuple(sorted(missing))


def hydrate_missing_foliage_sources(
    foliage_root,
    context=None,
    *,
    depot_paths: Iterable[str] | None = None,
    max_sources: int | None = None,
) -> FoliageHydrationResult:
    """Replace selected proxy sources without rebuilding points."""

    missing = list_missing_foliage_sources(foliage_root)
    if depot_paths is None:
        requested = list(missing)
    else:
        if isinstance(depot_paths, str):
            selected = [depot_paths]
        else:
            selected = list(dict.fromkeys(str(value) for value in depot_paths))
        missing_set = set(missing)
        requested = [depot_path for depot_path in selected if depot_path in missing_set]

    if max_sources is not None:
        max_sources = int(max_sources)
        if max_sources < 0:
            raise ValueError("max_sources cannot be negative")
        requested = requested[:max_sources]

    hydrated = []
    failed = []
    for depot_path in requested:
        instancer = _find_instancer(depot_path, foliage_root)
        if instancer is None:
            failed.append(depot_path)
            continue
        source_context_path = str(instancer.get(_SOURCE_CONTEXT_PROP, "") or "")
        try:
            source = _get_or_import_source_mesh(
                depot_path,
                foliage_root,
                source_context_path,
                context,
            )
            if source is None:
                failed.append(depot_path)
                continue
            _get_or_create_instancer(
                depot_path,
                source,
                foliage_root,
                source_context_path,
            )
            hydrated.append(depot_path)
        except Exception:
            log.exception("Failed to hydrate foliage type: %s", depot_path)
            failed.append(depot_path)

    return FoliageHydrationResult(
        requested_types=tuple(requested),
        hydrated_types=tuple(hydrated),
        failed_types=tuple(failed),
        remaining_types=list_missing_foliage_sources(foliage_root),
    )


def apply_viewer_source_budget(foliage_root, context=None) -> FoliageHydrationResult:
    """Keep only the root-wide dominant foliage types on real source meshes."""

    root_transforms = _get_root_transform_bucket(foliage_root, create=True)
    priority = tuple(_viewer_source_priority(root_transforms))
    priority_set = set(priority)
    placeholder = None
    for obj in foliage_root.objects:
        if not obj.get("_is_foliage_instancer"):
            continue
        depot_path = _instancer_depot_path(obj)
        if not depot_path or depot_path in priority_set:
            continue
        if placeholder is None:
            placeholder = _get_or_create_proxy_source(foliage_root)
        _set_instancer_source(obj, placeholder)

    return hydrate_missing_foliage_sources(
        foliage_root,
        context,
        depot_paths=priority,
    )


def _rebuild_depot_types_transactionally(
    foliage_root,
    previous_transforms: dict,
    candidate_transforms: dict,
    affected_types,
    source_context_by_type=None,
    *,
    source_mode: str,
    context=None,
) -> None:
    """Restore previous point geometry if any affected-type rebuild fails."""

    owner_keys_before = str(foliage_root.get(_OWNER_KEYS_PROP, "[]") or "[]")
    try:
        _rebuild_depot_types(
            foliage_root,
            candidate_transforms,
            affected_types,
            source_context_by_type,
            source_mode=source_mode,
            context=context,
        )
    except Exception:
        foliage_root[_OWNER_KEYS_PROP] = owner_keys_before
        try:
            _rebuild_depot_types(
                foliage_root,
                previous_transforms,
                affected_types,
                source_mode=FOLIAGE_SOURCE_MODE_PROXY,
                context=context,
            )
        except Exception:
            log.exception("Failed to restore foliage geometry after rebuild error")
        finally:
            foliage_root[_OWNER_KEYS_PROP] = owner_keys_before
        raise


def _normalise_cell_entries(cell_paths) -> list[tuple[str, str]]:
    if isinstance(cell_paths, dict):
        iterable = cell_paths.items()
    elif isinstance(cell_paths, (str, os.PathLike)):
        iterable = (cell_paths,)
    else:
        iterable = cell_paths or ()

    result = []
    for entry in iterable:
        if isinstance(entry, (str, os.PathLike)):
            path = os.fspath(entry)
            key = cell_key_from_path(path)
        else:
            try:
                key, path = entry
            except (TypeError, ValueError):
                raise ValueError("Foliage cells must be paths or (cell_key, path) pairs") from None
            key = str(key)
            path = os.fspath(path)
        result.append((str(key), str(path)))
    return result


def _scoped_owner_key(owner_scope: str, key: str) -> str:
    owner_scope = str(owner_scope or "").strip()
    return f"{owner_scope}|{key}" if owner_scope else str(key)


def _collect_cell_transforms(foliage_chunk, bounds=None) -> dict[str, list[tuple[float, ...]]]:
    all_tree_data = []
    if hasattr(foliage_chunk, "Trees"):
        all_tree_data += list(foliage_chunk.Trees.elements)
    if hasattr(foliage_chunk, "Grasses"):
        all_tree_data += list(foliage_chunk.Grasses.elements)

    by_type: dict[str, list[tuple[float, ...]]] = {}
    for tree_data in all_tree_data:
        try:
            depot_path = str(tree_data.TreeType.DepotPath or "").strip()
        except Exception:
            continue
        if not depot_path:
            continue
        collection = getattr(tree_data, "TreeCollection", None)
        instances = list(getattr(collection, "elements", ()) or ())
        for instance in instances:
            try:
                decoded = decode_foliage_instance_transform(instance)
                x, y, z = decoded.location
                if bounds is not None and not point_in_bounds(x, y, bounds):
                    continue
                transform = (
                    x,
                    y,
                    z,
                    *decoded.rotation_xyz,
                    *decoded.scale,
                )
                if not all(math.isfinite(value) for value in transform):
                    continue
                by_type.setdefault(depot_path, []).append(transform)
            except Exception:
                continue
    return by_type


def load_foliage_cells(
    cell_paths,
    foliage_root,
    context=None,
    *,
    bounds: Bounds2D | Sequence[float] | None = None,
    owner_scope: str = "",
    replace: bool = False,
    source_mode: str = FOLIAGE_SOURCE_MODE_FULL,
    hydrate_viewer_sources: bool = True,
) -> FoliageLoadResult:
    """Parse cells and upload each affected foliage type once."""

    from ..CR2W.CR2W_reader import load_foliage
    from ..CR2W.common_blender import redkit_repo_context

    source_mode = _normalise_source_mode(source_mode)
    entries = _normalise_cell_entries(cell_paths)
    if bounds is not None and not isinstance(bounds, Bounds2D):
        bounds = Bounds2D(*(float(value) for value in bounds))
    requested = tuple(key for key, _path in entries)
    root_transforms = _get_root_transform_bucket(foliage_root, create=True)
    candidate_transforms = dict(root_transforms)
    loaded_cell_keys = get_loaded_cells(foliage_root)
    loaded_owner_keys = get_loaded_owners(foliage_root)

    loaded = []
    skipped = []
    failed = []
    instance_count = 0
    affected_types = set()
    hydrate_types = set()
    source_context_by_type = {}
    new_owner_keys = set()

    for key, path in entries:
        owner_key = _scoped_owner_key(owner_scope, key)
        if not replace and (
            owner_key in candidate_transforms
            or (bounds is None and not owner_scope and key in loaded_cell_keys)
        ):
            if source_mode == FOLIAGE_SOURCE_MODE_FULL:
                hydrate_types.update(candidate_transforms.get(owner_key, {}))
            else:
                # Reapply viewer source policy to cached cells.
                affected_types.update(candidate_transforms.get(owner_key, {}))
            skipped.append(key)
            continue

        abs_path = resolve_flyr_abs_path(path, context)
        if not abs_path:
            log.warning("Could not resolve flyr path: %s", path)
            failed.append(key)
            continue
        try:
            with redkit_repo_context(abs_path):
                level = load_foliage(abs_path)
        except Exception:
            log.exception("Failed to load: %s", abs_path)
            failed.append(key)
            continue

        foliage_chunk = getattr(level, "Foliage", None)
        if foliage_chunk is None:
            log.warning("Foliage file has no Foliage chunk: %s", abs_path)
            failed.append(key)
            continue

        by_type = _collect_cell_transforms(foliage_chunk, bounds=bounds)
        previous = candidate_transforms.get(owner_key, {})
        affected_types.update(previous)
        affected_types.update(by_type)
        candidate_transforms[owner_key] = by_type
        new_owner_keys.add(owner_key)

        for depot_path, transforms in by_type.items():
            instance_count += len(transforms)
            source_context_by_type.setdefault(depot_path, abs_path)
        loaded.append(key)
        if bounds is None and not owner_scope:
            loaded_cell_keys.add(key)
        else:
            loaded_owner_keys.add(owner_key)

    if affected_types:
        _rebuild_depot_types_transactionally(
            foliage_root,
            root_transforms,
            candidate_transforms,
            affected_types,
            source_context_by_type,
            source_mode=source_mode,
            context=context,
        )
    elif new_owner_keys:
        _ensure_owner_indices(foliage_root, new_owner_keys)

    if new_owner_keys:
        root_transforms.clear()
        root_transforms.update(candidate_transforms)
        _set_json_string_list(foliage_root, "_loaded_cells", sorted(loaded_cell_keys))
        _set_json_string_list(foliage_root, _LOADED_OWNERS_PROP, sorted(loaded_owner_keys))
    foliage_root[_SOURCE_MODE_PROP] = source_mode

    hydrated_types = ()
    if source_mode == FOLIAGE_SOURCE_MODE_PROXY and hydrate_viewer_sources:
        hydration = apply_viewer_source_budget(foliage_root, context)
        hydrated_types = hydration.hydrated_types
    elif source_mode == FOLIAGE_SOURCE_MODE_FULL and hydrate_types:
        hydration = hydrate_missing_foliage_sources(
            foliage_root,
            context,
            depot_paths=hydrate_types,
        )
        hydrated_types = hydration.hydrated_types

    hidden_fallback_types = _sync_fallback_instancer_visibility(foliage_root)

    return FoliageLoadResult(
        requested_cells=requested,
        loaded_cells=tuple(loaded),
        skipped_cells=tuple(skipped),
        failed_cells=tuple(failed),
        instance_count=instance_count,
        affected_types=tuple(sorted(affected_types)),
        hydrated_types=tuple(hydrated_types),
        hidden_fallback_types=hidden_fallback_types,
    )


# ---------------------------------------------------------------------------
# Load one .flyr cell
# ---------------------------------------------------------------------------

def load_foliage_cell(
    game_rel_path: str,
    foliage_root,
    context,
    *,
    source_mode: str = FOLIAGE_SOURCE_MODE_FULL,
):
    """Compatibility wrapper for the original single-cell operator API."""

    result = load_foliage_cells(
        (game_rel_path,),
        foliage_root,
        context,
        source_mode=source_mode,
    )
    succeeded = bool(result.loaded_cells or result.skipped_cells) and not result.failed_cells
    return succeeded, result.instance_count


def load_foliage_for_bounds(
    world_path: str,
    world_root_collection,
    context,
    bounds: Bounds2D | Sequence[float],
    *,
    owner_scope: str,
    replace: bool = False,
    source_mode: str = FOLIAGE_SOURCE_MODE_FULL,
) -> FoliageLoadResult:
    """Directly resolve and batch-load the foliage intersecting world bounds."""

    foliage_prefix = get_game_rel_foliage_prefix(world_path, context)
    cells = resolve_foliage_cells_for_bounds(
        foliage_prefix,
        bounds,
        context,
        world_path=world_path,
    )
    foliage_root = get_foliage_root_collection(world_root_collection)
    return load_foliage_cells(
        cells,
        foliage_root,
        context,
        bounds=bounds,
        owner_scope=owner_scope,
        replace=replace,
        source_mode=source_mode,
    )


def load_foliage_for_tile(
    world_path: str,
    world_root_collection,
    context,
    tile_x: int,
    tile_y: int,
    tiles_x: int,
    tiles_y: int,
    terrain_size: float,
    *,
    invert_y: bool = False,
    replace: bool = False,
    source_mode: str = FOLIAGE_SOURCE_MODE_FULL,
) -> FoliageLoadResult:
    """Import only one terrain tile's foliage using deterministic cell paths."""

    bounds = terrain_tile_bounds(
        tile_x,
        tile_y,
        tiles_x,
        tiles_y,
        terrain_size,
        invert_y=invert_y,
    )
    return load_foliage_for_bounds(
        world_path,
        world_root_collection,
        context,
        bounds,
        owner_scope=f"tile:{int(tile_x)}:{int(tile_y)}",
        replace=replace,
        source_mode=source_mode,
    )


def unload_foliage_owners(foliage_root, owner_keys: Iterable[str]) -> int:
    """Remove tile/cell-owned points and rebuild each affected type once."""

    root_transforms = _get_root_transform_bucket(foliage_root, create=True)
    candidate_transforms = dict(root_transforms)
    affected_types = set()
    removed_instances = 0
    loaded_cells = get_loaded_cells(foliage_root)
    loaded_owners = get_loaded_owners(foliage_root)
    for owner_key in {str(value) for value in owner_keys}:
        by_type = candidate_transforms.pop(owner_key, None)
        if by_type is None:
            continue
        affected_types.update(by_type)
        removed_instances += sum(len(transforms) for transforms in by_type.values())
        loaded_owners.discard(owner_key)
        if "|" not in owner_key:
            loaded_cells.discard(owner_key)
    if affected_types:
        # Keep unloading free of source imports.
        _rebuild_depot_types_transactionally(
            foliage_root,
            root_transforms,
            candidate_transforms,
            affected_types,
            source_mode=FOLIAGE_SOURCE_MODE_PROXY,
        )
    root_transforms.clear()
    root_transforms.update(candidate_transforms)
    _set_json_string_list(foliage_root, "_loaded_cells", sorted(loaded_cells))
    _set_json_string_list(foliage_root, _LOADED_OWNERS_PROP, sorted(loaded_owners))
    return removed_instances


def unload_foliage_tile(foliage_root, tile_x: int, tile_y: int) -> int:
    """Unload every cell contribution owned by one terrain tile."""

    prefix = f"tile:{int(tile_x)}:{int(tile_y)}|"
    root_transforms = _get_root_transform_bucket(foliage_root, create=True)
    return unload_foliage_owners(
        foliage_root,
        (owner_key for owner_key in tuple(root_transforms) if owner_key.startswith(prefix)),
    )


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
