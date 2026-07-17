"""Blender preview helpers for Witcher world environments.

This module deliberately accepts plain values instead of UI or parser types.  The
environment reader can therefore pass evaluated sun/moon/light directions here
without making the Blender preview part of CR2W decoding.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import logging
import math
import os
from pathlib import Path
import shutil
from typing import Any, Iterable, Mapping, Sequence

import bpy
from mathutils import Matrix, Vector

from ..CR2W.common_blender import (
    _get_redkit_depot_roots,
    redkit_repo_context,
    repo_file,
    win_path_isfile,
)


log = logging.getLogger(__name__)

ENVIRONMENT_COLLECTION_NAME = "Witcher Environment Preview"
ENVIRONMENT_ANCHOR_NAME = "W3 Environment Anchor"
ENVIRONMENT_SUN_NAME = "W3 Environment Sun"
ENVIRONMENT_MOON_NAME = "W3 Environment Moon"
ENVIRONMENT_CLOUD_NAME = "W3 Environment Cloud Layer"
ENVIRONMENT_FOG_NAME = "W3 Environment Fog Volume"
ENVIRONMENT_LIGHT_NAME = "W3 Environment Key Light"
ENVIRONMENT_AMBIENT_LIGHT_NAME = "W3 Environment Ambient Fill"
ENVIRONMENT_CAMERA_LIGHT_NAME = "W3 Environment Camera Light"
ENVIRONMENT_CAMERA_CONSTRAINT_NAME = "W3 Environment Camera Anchor"
ENVIRONMENT_WORLD_NAME = "Witcher Environment Sky"
ENVIRONMENT_SUN_MATERIAL_NAME = "W3 Environment Sun Preview"
ENVIRONMENT_MOON_MATERIAL_NAME = "W3 Environment Moon Preview"
ENVIRONMENT_CLOUD_MATERIAL_NAME = "W3 Environment Cloud Preview"
ENVIRONMENT_FOG_MATERIAL_NAME = "W3 Environment Fog Preview"

_MANAGED_PROP = "witcher_environment_preview"
_ROLE_PROP = "witcher_environment_role"
_DEPOT_PATH_PROP = "witcher_environment_depot_path"
_RESOLVED_PATH_PROP = "witcher_environment_resolved_path"
_ASSET_MODE_PROP = "witcher_environment_asset_mode"
_SOURCE_PATH_PROP = "witcher_environment_source_path"
_TIME_PROP = "witcher_environment_time_seconds"
_WORLD_MANAGED_PROP = "witcher_environment_world"
_SCENE_PREVIOUS_WORLD_PROP = "witcher_environment_previous_world"
_SCENE_WORLD_ACTIVE_PROP = "witcher_environment_world_active"
_SCENE_PREVIOUS_VOLUMETRIC_END_PROP = "witcher_environment_previous_volumetric_end"
_SCENE_VOLUMETRIC_END_ACTIVE_PROP = "witcher_environment_volumetric_end_active"
_SCENE_PREVIOUS_VIEW_EXPOSURE_PROP = "witcher_environment_previous_view_exposure"
_SCENE_VIEW_EXPOSURE_ACTIVE_PROP = "witcher_environment_view_exposure_active"
_MATERIAL_ROLE_PROP = "witcher_environment_material_role"
_MATERIAL_OWNER_PROP = "witcher_environment_material_owner"
_PREVIEW_OWNER_PROP = "witcher_environment_owner"
_MOON_TEXTURE_PROP = "witcher_environment_moon_texture_path"
_MOON_TEXTURE_SOURCE_PROP = "witcher_environment_moon_texture_source"
_STARS_SOURCE_PROP = "witcher_environment_stars_source"
_PREVIEW_IMAGE_PROP = "witcher_environment_preview_image"
_CAMERA_LIGHT_INDEX_PROP = "witcher_environment_camera_light_index"
_CAMERA_LIGHT_FRONT_PROP = "witcher_environment_camera_light_front"
_CAMERA_LIGHT_RIGHT_PROP = "witcher_environment_camera_light_right"
_CAMERA_LIGHT_UP_PROP = "witcher_environment_camera_light_up"
_CAMERA_LIGHT_ATTENUATION_PROP = "witcher_environment_camera_light_attenuation"

_ROLE_ANCHOR = "anchor"
_ROLE_SUN_ROOT = "sun_root"
_ROLE_MOON_ROOT = "moon_root"
_ROLE_SUN_GEOMETRY = "sun_geometry"
_ROLE_MOON_GEOMETRY = "moon_geometry"
_ROLE_CLOUD_ROOT = "cloud_root"
_ROLE_CLOUD_GEOMETRY = "cloud_geometry"
_ROLE_FOG_VOLUME = "fog_volume"
_ROLE_KEY_LIGHT = "key_light"
_ROLE_AMBIENT_LIGHT = "ambient_light"
_ROLE_CAMERA_LIGHT = "camera_light"

_SKY_NODE_ZENITH = "W3 Sky Zenith"
_SKY_NODE_HORIZON = "W3 Sky Horizon"
_SKY_NODE_STARS = "W3 Sky Stars"
_SKY_NODE_STAR_ROTATION = "W3 Sky Star Rotation"
_SKY_NODE_DAY_FACTOR = "W3 Sky Day Factor"
_SKY_NODE_BRIGHTNESS = "W3 Sky Brightness"
_SKY_NODE_GLOBAL_BRIGHTNESS = "W3 Sky Global Brightness"
_SKY_NODE_SUN_DIRECTION = "W3 Sky Sun Direction"
_SKY_NODE_SUN_HORIZON_FRONT = "W3 Sky Sun Horizon Front"
_SKY_NODE_SUN_HORIZON_BACK = "W3 Sky Sun Horizon Back"
_SKY_NODE_SUN_HORIZON_MIX = "W3 Sky Sun Horizon Direction"
_SKY_NODE_HORIZON_CAMERA_HEIGHT = "W3 Sky Camera Height Offset"
_SKY_NODE_HORIZON_ATTENUATION = "W3 Sky Horizon Attenuation"
_SKY_NODE_HORIZON_POWER = "W3 Sky Horizon Power"
_SKY_NODE_HALO_COLOR = "W3 Sky Halo Color"
_SKY_NODE_MOON_DIRECTION = "W3 Sky Moon Direction"
_SKY_NODE_MOON_HALO_COLOR = "W3 Sky Moon Halo Color"
_SKY_NODE_MOON_HALO_ADD = "W3 Sky Moon Halo Add"
_SKY_NODE_CLOUD_COLOR = "W3 Sky Cloud Color"
_SKY_NODE_CLOUD_RAMP = "W3 Sky Cloud Coverage"
_SKY_NODE_CLOUD_MIX = "W3 Sky Cloud Mix"
_SKY_NODE_CLOUD_OPACITY = "W3 Sky Cloud Opacity"
_SKY_NODE_FOG_COLOR = "W3 Sky Fog Color"
_SKY_NODE_FOG_COLOR_FRONT = "W3 Sky Fog Color Front"
_SKY_NODE_FOG_COLOR_BACK = "W3 Sky Fog Color Back"
_SKY_NODE_FOG_DIRECTION = "W3 Sky Fog Direction"
_SKY_NODE_FOG_DIRECTION_WEIGHT = "W3 Sky Fog Direction Weight"
_SKY_NODE_FOG_BASE_OPACITY = "W3 Sky Fog Base Opacity"
_SKY_NODE_FOG_MIX = "W3 Sky Fog Mix"
_SKY_NODE_AERIAL_COLOR = "W3 Sky Aerial Color"
_SKY_NODE_AERIAL_COLOR_FRONT = "W3 Sky Aerial Color Front"
_SKY_NODE_AERIAL_COLOR_BACK = "W3 Sky Aerial Color Back"
_SKY_NODE_AERIAL_BASE_OPACITY = "W3 Sky Aerial Base Opacity"
_SKY_NODE_AERIAL_MIX = "W3 Sky Aerial Mix"

# Legacy callers provide only a sky-density multiplier, so keep their restrained
# surface conversion.
_BLENDER_SKY_FOG_DENSITY_SCALE = 0.37
# Bound world-unit fog so background rays do not cross an infinite volume.
# The box follows the camera; height shaping remains in world space.
_BLENDER_FOG_HALF_EXTENT = 4096.0
_BLENDER_FOG_HALF_HEIGHT = 512.0
_BLENDER_FOG_MAX_DENSITY = 0.02
# Clear a 2 km world's corners without drawing to the cloud mesh's 6.4 km radius.
_BLENDER_CLOUD_MAX_REACH = 2048.0
# Eevee's default 1 mm SUN shadow texel exhausts the virtual shadow pool on a
# whole imported world. Two centimetres retains useful authored shadows.
_BLENDER_KEY_SHADOW_RESOLUTION = 0.02
# These upper-sky colors are not a full environment probe; keep proxies overhead.
_AMBIENT_FILL_DIRECTIONS = (
    (1.0, 1.0, 1.0),
    (1.0, -1.0, 1.0),
    (-1.0, 1.0, 1.0),
    (-1.0, -1.0, 1.0),
)

# The vanilla night sky; used when a world's sky material does not declare one.
DEFAULT_STARS_CUBE = "fx\\textures\\cloud\\stars_cubemap\\stars2.w2cube"

# Synodic cycle used for the preview phase.
LUNAR_MONTH_DAYS = 29.53

# Keep celestial discs 100 m from the camera so size controls remain practical.
CELESTIAL_DISTANCE = 100.0

_MATERIAL_RESOURCE_CACHE: dict[tuple[str, str], dict[str, tuple[str, str]]] = {}
_CLOUD_EFFECT_CACHE: dict[str, Any] = {}
_VIEW_CLIP_END_STATES: dict[tuple[int, int], tuple[Any, float, float]] = {}
_CAMERA_CLIP_END_STATES: dict[tuple[int, int], tuple[Any, float, float]] = {}

# Compress additive HDR moon values enough to preserve surface detail.
_BLENDER_MOON_EMISSION_SCALE = 0.36


@dataclass(frozen=True)
class PreviewResult:
    collection_name: str = ""
    sun_object_names: tuple[str, ...] = ()
    moon_object_names: tuple[str, ...] = ()
    cloud_object_names: tuple[str, ...] = ()
    key_light_name: str = ""
    camera_light_names: tuple[str, ...] = ()
    sun_resolved_path: str = ""
    moon_resolved_path: str = ""
    cloud_resolved_path: str = ""
    sun_mode: str = ""
    moon_mode: str = ""
    cloud_mode: str = ""
    sky_world_name: str = ""
    stars_resolved_path: str = ""
    stars_mode: str = ""
    warnings: tuple[str, ...] = ()

    @property
    def success(self) -> bool:
        return bool(self.collection_name and self.key_light_name)

    @property
    def used_fallback(self) -> bool:
        return self.sun_mode == "FALLBACK" or self.moon_mode == "FALLBACK"


@dataclass(frozen=True)
class CloudLayerAsset:
    effect_path: str = ""
    effect_resolved_path: str = ""
    mesh_path: str = ""
    material_path: str = ""
    scale: float = 1.0
    strength: float = 1.0


def _normalise_depot_path(path: Any) -> str:
    return str(path or "").strip().replace("/", "\\")


def _source_key(path: Any) -> str:
    text = str(path or "").strip()
    return os.path.normcase(os.path.normpath(os.path.abspath(text))) if text else ""


def resolve_environment_asset(
    depot_path: str,
    source_path: str = "",
    *,
    version: int = 999,
) -> str:
    """Resolve one world-provided asset path without inventing alternatives.

    ``source_path`` should be the absolute ``.w2w``/``.env`` path. Configured
    project roots are searched before the normal uncook depot.
    """

    depot_path = _normalise_depot_path(depot_path)
    if not depot_path:
        return ""
    try:
        redkit_roots = _get_redkit_depot_roots()
        with redkit_repo_context(source_path or None, roots=redkit_roots or None):
            resolved = repo_file(depot_path, version=version)
    except Exception:
        log.warning("Could not resolve environment asset: %s", depot_path, exc_info=True)
        return ""
    return os.path.normpath(resolved) if win_path_isfile(resolved) else ""


def _material_resources(material_path: str, source_path: str = "") -> dict[str, tuple[str, str]]:
    material_path = _normalise_depot_path(material_path)
    if not material_path:
        return {}
    cache_key = (str(source_path or "").lower(), material_path.lower())
    cached = _MATERIAL_RESOURCE_CACHE.get(cache_key)
    if cached is not None:
        return cached

    try:
        from ..materials import reader as material_reader

        with redkit_repo_context(source_path or None):
            params = material_reader.read_material_params_from_path(material_path) or {}
    except Exception:
        log.warning("Could not inspect environment material resources: %s", material_path, exc_info=True)
        params = {}
    result = {
        str(name).lower(): (str(value[0]), _normalise_depot_path(value[1]))
        for name, value in params.items()
        if isinstance(value, (tuple, list)) and len(value) >= 2 and value[1]
    }
    _MATERIAL_RESOURCE_CACHE[cache_key] = result
    return result


def resolve_sky_stars_cube(skybox_material_path: str, source_path: str = "") -> str:
    """Return the stars cubemap declared by the world's sky material graph."""

    params = _material_resources(skybox_material_path, source_path)
    preferred = params.get("stars_cube")
    if preferred and preferred[0] == "handle:CCubeTexture":
        return preferred[1]
    for name, (param_type, depot_path) in params.items():
        if "star" in name and param_type == "handle:CCubeTexture":
            return depot_path
    return ""


def resolve_moon_detail_texture(moon_material_path: str, source_path: str = "") -> str:
    """Return the lunar surface texture declared by the moon material graph."""

    params = _material_resources(moon_material_path, source_path)
    preferred = params.get("normal")
    if preferred and preferred[0] == "handle:ITexture":
        return preferred[1]
    for name, (param_type, depot_path) in params.items():
        if "normal" in name and param_type == "handle:ITexture":
            return depot_path
    return ""


def resolve_cloud_textures(
    cloud_material_path: str,
    source_path: str = "",
) -> tuple[str, str]:
    """Return the detail/normal and coverage maps declared by a cloud layer."""

    params = _material_resources(cloud_material_path, source_path)
    clouds = params.get("clouds", ("", ""))[1]
    coverage = params.get("coverage", ("", ""))[1]
    return _normalise_depot_path(clouds), _normalise_depot_path(coverage)


def _effect_value(effect: Any, name: str, default: Any = None) -> Any:
    if isinstance(effect, Mapping):
        return effect.get(name, default)
    return getattr(effect, name, default)


def resolve_weather_cloud_layer(
    weather_effects: Sequence[Any],
    source_path: str = "",
) -> CloudLayerAsset | None:
    """Decode the first mesh-backed CLOUDS emitter from selected weather.

    The weather table points at a ``.w2p`` rather than directly at its dome.
    Read that particle so the preview uses the authored mesh, material and
    initializer scale instead of guessing alternate asset paths.
    """

    for effect in weather_effects or ():
        effect_type = str(_effect_value(effect, "effect_type", "CLOUDS") or "CLOUDS").upper()
        effect_path = _normalise_depot_path(_effect_value(effect, "path", ""))
        strength = max(0.0, float(_effect_value(effect, "strength", 1.0) or 0.0))
        if effect_type != "CLOUDS" or not effect_path or strength <= 0.0:
            continue
        resolved = resolve_environment_asset(effect_path, source_path)
        if not resolved:
            continue

        cache_key = _source_key(resolved)
        cached = _CLOUD_EFFECT_CACHE.get(cache_key)
        if cached is None:
            cached = ()
            try:
                from ..CR2W import dc_environment
                from ..CR2W.CR2W_file import read_CR2W

                cr2w = read_CR2W(resolved)
                chunks = getattr(getattr(cr2w, "CHUNKS", None), "CHUNKS", ()) or ()
                prop_cache: dict[int, dict[str, Any]] = {}

                def props(index: int) -> dict[str, Any]:
                    if index not in prop_cache:
                        chunk = chunks[index]
                        prop_cache[index] = {
                            str(getattr(prop, "theName", "") or ""): dc_environment._decode_value(
                                prop,
                                {},
                                "",
                            )
                            for prop in (getattr(chunk, "PROPS", ()) or ())
                        }
                    return prop_cache[index]

                def chunk_index(value: Any, *, pointer: bool = False) -> int | None:
                    if isinstance(value, Mapping) and "chunk_index" in value:
                        try:
                            return int(value["chunk_index"])
                        except (TypeError, ValueError):
                            return None
                    try:
                        index = int(value)
                    except (TypeError, ValueError):
                        return None
                    return index - 1 if pointer else index

                for emitter_index, emitter in enumerate(chunks):
                    if str(getattr(emitter, "Type", "")) != "CParticleEmitter":
                        continue
                    emitter_props = props(emitter_index)
                    drawer_index = chunk_index(emitter_props.get("particleDrawer"), pointer=True)
                    if drawer_index is None or not (0 <= drawer_index < len(chunks)):
                        continue
                    if str(getattr(chunks[drawer_index], "Type", "")) != "CParticleDrawerMesh":
                        continue
                    mesh_value = props(drawer_index).get("meshes", "")
                    if isinstance(mesh_value, Sequence) and not isinstance(mesh_value, str):
                        mesh_value = next((value for value in mesh_value if value), "")
                    mesh_path = _normalise_depot_path(mesh_value)
                    if not mesh_path:
                        continue

                    material_index = chunk_index(emitter_props.get("material"))
                    material_path = ""
                    if material_index is not None and 0 <= material_index < len(chunks):
                        material_path = _normalise_depot_path(
                            props(material_index).get("baseMaterial", "")
                        )

                    scale = 1.0
                    upper = min(
                        value
                        for value in (drawer_index, material_index, len(chunks))
                        if value is not None and value > emitter_index
                    )
                    for module_index in range(emitter_index + 1, upper):
                        if str(getattr(chunks[module_index], "Type", "")) != "CParticleInitializerSize":
                            continue
                        evaluator_index = chunk_index(props(module_index).get("size"), pointer=True)
                        if evaluator_index is None or not (0 <= evaluator_index < len(chunks)):
                            continue
                        vector = props(evaluator_index).get("value", {})
                        if isinstance(vector, Mapping):
                            components = [
                                float(vector.get(axis, 0.0) or 0.0)
                                for axis in ("X", "Y", "Z")
                            ]
                            scale = max(components) if max(components, default=0.0) > 0.0 else 1.0
                        break
                    cached = (mesh_path, material_path, scale)
                    break
            except Exception:
                log.exception("Could not inspect weather cloud effect '%s'", resolved)
            _CLOUD_EFFECT_CACHE[cache_key] = cached

        if cached:
            mesh_path, material_path, scale = cached
            return CloudLayerAsset(
                effect_path=effect_path,
                effect_resolved_path=resolved,
                mesh_path=mesh_path,
                material_path=material_path,
                scale=max(0.001, float(scale)),
                strength=strength,
            )
    return None


def _cached_cubemap_source(source_path: str) -> str:
    source = Path(source_path)
    stat = source.stat()
    identity = f"{source.resolve()}|{stat.st_mtime_ns}|{stat.st_size}"
    digest = hashlib.sha1(identity.encode("utf-8", errors="ignore")).hexdigest()
    from ..extension_paths import get_cache_root

    cache_dir = Path(get_cache_root(create=True)) / "environment_preview" / "cubemaps" / digest[:16]
    cache_dir.mkdir(parents=True, exist_ok=True)
    # Hash the stem because Blender identifies equirect images by it.
    cached = cache_dir / f"{source.stem}_{digest[:16]}{source.suffix}"
    if not cached.is_file() or cached.stat().st_size != stat.st_size:
        shutil.copy2(source, cached)
    return str(cached)


def _load_stars_image(cube_path: str, source_path: str, warnings: list[str]):
    depot_path = _normalise_depot_path(cube_path)
    resolved = resolve_environment_asset(depot_path, source_path) if depot_path else ""
    if not resolved:
        if depot_path:
            warnings.append(f"Stars cubemap was not found: {depot_path}")
        return None, ""
    try:
        from ..ui.blender_fun import load_w2cube_blick_equirect_image

        cached_cube = _cached_cubemap_source(resolved)
        known_images = {item.as_pointer() for item in bpy.data.images}
        image, _dds_path = load_w2cube_blick_equirect_image(
            cached_cube,
            check_existing=True,
            colorspace="sRGB",
        )
        if image is None:
            raise RuntimeError("cubemap conversion did not produce a Blender image")
        if int(image.size[0]) > 2048:
            image.scale(2048, max(1, int(image.size[1]) * 2048 // int(image.size[0])))
        image["witcher_environment_source_path"] = depot_path
        image["witcher_environment_resolved_path"] = resolved
        if image.as_pointer() not in known_images:
            image[_PREVIEW_IMAGE_PROP] = True
        return image, resolved
    except Exception as exc:
        log.exception("Could not load environment stars cubemap '%s'", resolved)
        warnings.append(f"Stars cubemap preview failed: {exc}")
        return None, resolved


def _load_moon_detail_image(texture_path: str, source_path: str, warnings: list[str]):
    depot_path = _normalise_depot_path(texture_path)
    resolved = resolve_environment_asset(depot_path, source_path) if depot_path else ""
    if not resolved:
        if depot_path:
            warnings.append(f"Moon detail texture was not found: {depot_path}")
        return None
    try:
        from ..CR2W.common_blender import bpy_image_load_safe
        from ..materials.material import _convert_xbm_to_writable_dds

        dds_path = _convert_xbm_to_writable_dds(resolved)
        known_images = {item.as_pointer() for item in bpy.data.images}
        image = bpy_image_load_safe(dds_path, check_existing=True) if dds_path else None
        if image is None:
            raise RuntimeError("texture conversion did not produce a Blender image")
        try:
            image.colorspace_settings.name = "Non-Color"
        except Exception:
            pass
        image["witcher_environment_source_path"] = depot_path
        image["witcher_environment_resolved_path"] = resolved
        if image.as_pointer() not in known_images:
            image[_PREVIEW_IMAGE_PROP] = True
        return image
    except Exception as exc:
        log.exception("Could not load moon detail texture '%s'", resolved)
        warnings.append(f"Moon detail preview failed: {exc}")
        return None


def _load_cloud_image(texture_path: str, source_path: str, warnings: list[str]):
    depot_path = _normalise_depot_path(texture_path)
    resolved = resolve_environment_asset(depot_path, source_path) if depot_path else ""
    if not resolved:
        if depot_path:
            warnings.append(f"Cloud texture was not found: {depot_path}")
        return None
    try:
        from ..CR2W.common_blender import bpy_image_load_safe
        from ..materials.material import _convert_xbm_to_writable_dds

        dds_path = _convert_xbm_to_writable_dds(resolved)
        known_images = {item.as_pointer() for item in bpy.data.images}
        image = bpy_image_load_safe(dds_path, check_existing=True) if dds_path else None
        if image is None:
            raise RuntimeError("texture conversion did not produce a Blender image")
        try:
            image.colorspace_settings.name = "Non-Color"
        except Exception:
            pass
        image["witcher_environment_source_path"] = depot_path
        image["witcher_environment_resolved_path"] = resolved
        if image.as_pointer() not in known_images:
            image[_PREVIEW_IMAGE_PROP] = True
        return image
    except Exception as exc:
        log.exception("Could not load environment cloud texture '%s'", resolved)
        warnings.append(f"Cloud texture preview failed: {exc}")
        return None


def _managed_world(scene):
    world = getattr(scene, "world", None)
    try:
        return world if world is not None and bool(world.get(_WORLD_MANAGED_PROP, False)) else None
    except Exception:
        return None


def _ensure_environment_world(scene):
    world = _managed_world(scene)
    if world is not None:
        return world

    previous = getattr(scene, "world", None)
    scene[_SCENE_PREVIOUS_WORLD_PROP] = previous.name if previous is not None else ""
    scene[_SCENE_WORLD_ACTIVE_PROP] = True

    preferred = bpy.data.worlds.get(ENVIRONMENT_WORLD_NAME)
    if preferred is not None and (
        not bool(preferred.get(_WORLD_MANAGED_PROP, False)) or preferred.users > 0
    ):
        preferred = None
    world = preferred or bpy.data.worlds.new(ENVIRONMENT_WORLD_NAME)
    world[_WORLD_MANAGED_PROP] = True
    world.use_nodes = True
    scene.world = world
    return world


def _restore_environment_world(scene) -> bool:
    current = _managed_world(scene)
    active = bool(scene.get(_SCENE_WORLD_ACTIVE_PROP, False))
    if not active and current is None:
        return False

    previous_name = str(scene.get(_SCENE_PREVIOUS_WORLD_PROP, "") or "")
    if current is not None:
        scene.world = bpy.data.worlds.get(previous_name) if previous_name else None
    for key in (_SCENE_PREVIOUS_WORLD_PROP, _SCENE_WORLD_ACTIVE_PROP):
        try:
            del scene[key]
        except Exception:
            pass
    if current is not None and current.users == 0:
        try:
            bpy.data.worlds.remove(current)
        except Exception:
            pass
    return current is not None


def _remove_unused_preview_images() -> None:
    for image in list(bpy.data.images):
        try:
            tagged = bool(image.get(_PREVIEW_IMAGE_PROP, False))
        except Exception:
            tagged = False
        if tagged and image.users == 0:
            try:
                bpy.data.images.remove(image)
            except Exception:
                pass


def _moon_phase_angle(day_number: float, time_seconds: float) -> float:
    day_fraction = (float(time_seconds) % 86400.0) / 86400.0
    return 2.0 * math.pi * ((float(day_number) + day_fraction) / LUNAR_MONTH_DAYS)


def _moon_phase_light(moon_direction, phase_angle: float) -> Vector:
    """World-space phase light dotted with the moon's sphere normals.

    Day 0 lights the viewer-facing hemisphere (full moon); the light then swings
    horizontally around the disc so a vertical terminator carves the crescent,
    reaching the far side at half a synodic month (new moon)."""

    view = -_vector3(moon_direction, (0.0, 0.0, -1.0))
    # view x up makes the waning crescent open to the left.
    right = view.cross(Vector((0.0, 0.0, 1.0)))
    if right.length_squared < 1.0e-9:
        right = Vector((1.0, 0.0, 0.0))
    right.normalize()
    angle = float(phase_angle)
    return view * math.cos(angle) + right * math.sin(angle)


def _star_rotation(moon_direction) -> tuple[Vector, float]:
    """Rotate the star lookup into an orthonormal moon-direction frame."""

    moon = _vector3(moon_direction, (0.0, 0.0, -1.0))
    up = Vector((0.0, 0.0, 1.0))
    axis_x = moon.cross(up)
    if axis_x.length_squared < 1.0e-9:
        axis_x = Vector((1.0, 0.0, 0.0))
    axis_x.normalize()
    axis_y = moon.cross(axis_x)
    quaternion = Matrix((axis_x, axis_y, moon)).to_quaternion()
    return quaternion.axis, quaternion.angle


def _build_environment_world_nodes(world, stars_image=None) -> None:
    nodes = world.node_tree.nodes
    links = world.node_tree.links
    nodes.clear()

    texcoord = nodes.new("ShaderNodeTexCoord")
    texcoord.name = "W3 Sky Direction"
    texcoord.location = (-1900, 160)

    separate = nodes.new("ShaderNodeSeparateXYZ")
    separate.name = "W3 Sky Direction Z"
    separate.location = (-1700, 460)

    zenith_color = nodes.new("ShaderNodeRGB")
    zenith_color.name = _SKY_NODE_ZENITH
    zenith_color.label = "Sky Color"
    zenith_color.location = (-690, 790)

    horizon_color = nodes.new("ShaderNodeRGB")
    horizon_color.name = _SKY_NODE_HORIZON
    horizon_color.label = "Sky Color Horizon"
    horizon_color.location = (-1120, 660)

    sun_horizon_front = nodes.new("ShaderNodeRGB")
    sun_horizon_front.name = _SKY_NODE_SUN_HORIZON_FRONT
    sun_horizon_front.label = "Sun Color Horizon"
    sun_horizon_front.location = (-1120, 530)

    sun_horizon_back = nodes.new("ShaderNodeRGB")
    sun_horizon_back.name = _SKY_NODE_SUN_HORIZON_BACK
    sun_horizon_back.label = "Sun Back Horizon Color"
    sun_horizon_back.location = (-1120, 400)

    # Directional color influence follows a clamped dot-product power curve.
    sun_direction = nodes.new("ShaderNodeCombineXYZ")
    sun_direction.name = _SKY_NODE_SUN_DIRECTION
    sun_direction.label = "Sun Direction"
    sun_direction.location = (-1700, 40)
    sun_direction.inputs[2].default_value = 1.0

    # Blend back/front horizon colors from the normalized XY sun/view dot.
    view_xy = nodes.new("ShaderNodeVectorMath")
    view_xy.name = "W3 Sky View XY"
    view_xy.operation = "MULTIPLY"
    view_xy.location = (-1510, 660)
    view_xy.inputs[1].default_value = (1.0, 1.0, 0.0)

    view_xy_normalize = nodes.new("ShaderNodeVectorMath")
    view_xy_normalize.name = "W3 Sky View XY Normalize"
    view_xy_normalize.operation = "NORMALIZE"
    view_xy_normalize.location = (-1330, 660)

    sun_xy = nodes.new("ShaderNodeVectorMath")
    sun_xy.name = "W3 Sky Sun XY"
    sun_xy.operation = "MULTIPLY"
    sun_xy.location = (-1510, 520)
    sun_xy.inputs[1].default_value = (1.0, 1.0, 0.0)

    sun_xy_normalize = nodes.new("ShaderNodeVectorMath")
    sun_xy_normalize.name = "W3 Sky Sun XY Normalize"
    sun_xy_normalize.operation = "NORMALIZE"
    sun_xy_normalize.location = (-1330, 520)

    sun_xy_dot = nodes.new("ShaderNodeVectorMath")
    sun_xy_dot.name = "W3 Sky Sun Horizon Dot XY"
    sun_xy_dot.operation = "DOT_PRODUCT"
    sun_xy_dot.location = (-1120, 810)

    sun_facing = nodes.new("ShaderNodeMath")
    sun_facing.name = "W3 Sky Sun Horizon Facing"
    sun_facing.operation = "MULTIPLY_ADD"
    sun_facing.use_clamp = True
    sun_facing.location = (-930, 810)
    sun_facing.inputs[1].default_value = 0.5
    sun_facing.inputs[2].default_value = 0.5

    sun_horizon_direction = nodes.new("ShaderNodeMixRGB")
    sun_horizon_direction.name = _SKY_NODE_SUN_HORIZON_MIX
    sun_horizon_direction.label = "Sun Back to Front Horizon"
    sun_horizon_direction.blend_type = "MIX"
    sun_horizon_direction.location = (-690, 600)

    z_square = nodes.new("ShaderNodeMath")
    z_square.name = "W3 Sky Horizon Z Squared"
    z_square.operation = "MULTIPLY"
    z_square.location = (-1510, 850)

    horizon_weight = nodes.new("ShaderNodeMath")
    horizon_weight.name = "W3 Sky Horizon Weight"
    horizon_weight.operation = "SUBTRACT"
    horizon_weight.use_clamp = True
    horizon_weight.location = (-1330, 850)
    horizon_weight.inputs[0].default_value = 1.0

    sun_horizon_weight = nodes.new("ShaderNodeMath")
    sun_horizon_weight.name = "W3 Sky Sun Horizon Influence"
    sun_horizon_weight.operation = "MULTIPLY"
    sun_horizon_weight.use_clamp = True
    sun_horizon_weight.location = (-1120, 940)
    sun_horizon_weight.inputs[1].default_value = 1.0

    directional_horizon = nodes.new("ShaderNodeMixRGB")
    directional_horizon.name = "W3 Sky Directional Horizon"
    directional_horizon.blend_type = "MIX"
    directional_horizon.location = (-450, 650)

    # Evaluate vertical falloff on a camera-centered 1000-unit shell.
    camera_height = nodes.new("ShaderNodeValue")
    camera_height.name = _SKY_NODE_HORIZON_CAMERA_HEIGHT
    camera_height.label = "(Camera Z + 710) / 1000"
    camera_height.location = (-1510, 1120)
    camera_height.outputs[0].default_value = 0.71

    horizon_z = nodes.new("ShaderNodeMath")
    horizon_z.name = "W3 Sky Horizon Shell Z"
    horizon_z.operation = "ADD"
    horizon_z.location = (-1330, 1120)

    horizon_attenuation = nodes.new("ShaderNodeMath")
    horizon_attenuation.name = _SKY_NODE_HORIZON_ATTENUATION
    horizon_attenuation.operation = "MULTIPLY"
    horizon_attenuation.location = (-1120, 1120)
    horizon_attenuation.inputs[1].default_value = 1.8

    horizon_bias = nodes.new("ShaderNodeMath")
    horizon_bias.name = "W3 Sky Horizon Bias"
    horizon_bias.operation = "ADD"
    horizon_bias.location = (-930, 1120)
    horizon_bias.inputs[1].default_value = 0.1

    horizon_min = nodes.new("ShaderNodeMath")
    horizon_min.name = "W3 Sky Horizon Denominator"
    horizon_min.operation = "MAXIMUM"
    horizon_min.location = (-750, 1120)
    horizon_min.inputs[1].default_value = 0.0001

    horizon_reciprocal = nodes.new("ShaderNodeMath")
    horizon_reciprocal.name = "W3 Sky Horizon Reciprocal"
    horizon_reciprocal.operation = "DIVIDE"
    horizon_reciprocal.use_clamp = True
    horizon_reciprocal.location = (-570, 1120)
    horizon_reciprocal.inputs[0].default_value = 1.0

    horizon_power = nodes.new("ShaderNodeMath")
    horizon_power.name = _SKY_NODE_HORIZON_POWER
    horizon_power.operation = "POWER"
    horizon_power.location = (-390, 1120)
    horizon_power.inputs[1].default_value = 2.8

    horizon_factor = nodes.new("ShaderNodeMath")
    horizon_factor.name = "W3 Sky Horizon Factor"
    horizon_factor.operation = "SUBTRACT"
    horizon_factor.use_clamp = True
    horizon_factor.location = (-210, 1120)
    horizon_factor.inputs[0].default_value = 1.0

    gradient = nodes.new("ShaderNodeMixRGB")
    gradient.name = "W3 Sky Gradient"
    gradient.label = "Directional Horizon"
    gradient.blend_type = "MIX"
    gradient.location = (-30, 700)

    halo_dot = nodes.new("ShaderNodeVectorMath")
    halo_dot.name = "W3 Sky Halo Dot"
    halo_dot.operation = "DOT_PRODUCT"
    halo_dot.location = (-950, 40)

    halo_clamp = nodes.new("ShaderNodeMath")
    halo_clamp.name = "W3 Sky Halo Clamp"
    halo_clamp.operation = "MAXIMUM"
    halo_clamp.location = (-780, 40)
    halo_clamp.inputs[1].default_value = 0.0

    halo_core = nodes.new("ShaderNodeMath")
    halo_core.name = "W3 Sky Halo Core"
    halo_core.operation = "POWER"
    halo_core.location = (-610, 120)
    halo_core.inputs[1].default_value = 0.33

    halo_core_scale = nodes.new("ShaderNodeMath")
    halo_core_scale.name = "W3 Sky Halo Core Scale"
    halo_core_scale.operation = "MULTIPLY"
    halo_core_scale.location = (-440, 120)
    halo_core_scale.inputs[1].default_value = 1.0

    halo_veil = nodes.new("ShaderNodeMath")
    halo_veil.name = "W3 Sky Halo Veil"
    halo_veil.operation = "POWER"
    halo_veil.location = (-610, -40)
    halo_veil.inputs[1].default_value = 7.0

    halo_veil_scale = nodes.new("ShaderNodeMath")
    halo_veil_scale.name = "W3 Sky Halo Veil Scale"
    halo_veil_scale.operation = "MULTIPLY"
    halo_veil_scale.location = (-440, -40)
    halo_veil_scale.inputs[1].default_value = 0.22

    halo_sum = nodes.new("ShaderNodeMath")
    halo_sum.name = "W3 Sky Halo Sum"
    halo_sum.operation = "ADD"
    halo_sum.location = (-280, 40)

    halo_color = nodes.new("ShaderNodeRGB")
    halo_color.name = _SKY_NODE_HALO_COLOR
    halo_color.label = "Sun Halo Color"
    halo_color.location = (-280, -120)
    halo_color.outputs[0].default_value = (1.0, 0.8, 0.5, 1.0)

    halo_scale = nodes.new("ShaderNodeVectorMath")
    halo_scale.name = "W3 Sky Halo"
    halo_scale.operation = "SCALE"
    halo_scale.location = (-110, 40)

    add_halo = nodes.new("ShaderNodeMixRGB")
    add_halo.name = "W3 Sky Halo Add"
    add_halo.location = (60, 260)
    add_halo.blend_type = "MIX"
    add_halo.inputs[0].default_value = 0.0

    # Apply moon influence before sun influence.
    moon_direction = nodes.new("ShaderNodeCombineXYZ")
    moon_direction.name = _SKY_NODE_MOON_DIRECTION
    moon_direction.label = "Moon Direction"
    moon_direction.location = (-1150, -120)
    moon_direction.inputs[2].default_value = 1.0

    moon_dot = nodes.new("ShaderNodeVectorMath")
    moon_dot.name = "W3 Sky Moon Halo Dot"
    moon_dot.operation = "DOT_PRODUCT"
    moon_dot.location = (-950, -120)

    moon_clamp = nodes.new("ShaderNodeMath")
    moon_clamp.name = "W3 Sky Moon Halo Clamp"
    moon_clamp.operation = "MAXIMUM"
    moon_clamp.inputs[1].default_value = 0.0
    moon_clamp.location = (-780, -120)

    moon_core = nodes.new("ShaderNodeMath")
    moon_core.name = "W3 Sky Moon Halo Core"
    moon_core.operation = "POWER"
    moon_core.location = (-610, -100)
    moon_core.inputs[1].default_value = 0.33

    moon_core_scale = nodes.new("ShaderNodeMath")
    moon_core_scale.name = "W3 Sky Moon Halo Core Scale"
    moon_core_scale.operation = "MULTIPLY"
    moon_core_scale.location = (-440, -100)
    moon_core_scale.inputs[1].default_value = 0.0

    moon_veil = nodes.new("ShaderNodeMath")
    moon_veil.name = "W3 Sky Moon Halo Veil"
    moon_veil.operation = "POWER"
    moon_veil.location = (-610, -240)
    moon_veil.inputs[1].default_value = 22.0

    moon_veil_scale = nodes.new("ShaderNodeMath")
    moon_veil_scale.name = "W3 Sky Moon Halo Veil Scale"
    moon_veil_scale.operation = "MULTIPLY"
    moon_veil_scale.location = (-440, -240)
    moon_veil_scale.inputs[1].default_value = 0.012

    moon_sum = nodes.new("ShaderNodeMath")
    moon_sum.name = "W3 Sky Moon Halo Sum"
    moon_sum.operation = "ADD"
    moon_sum.location = (-280, -160)

    moon_halo_color = nodes.new("ShaderNodeRGB")
    moon_halo_color.name = _SKY_NODE_MOON_HALO_COLOR
    moon_halo_color.label = "Moon Halo Color"
    moon_halo_color.location = (-280, -340)
    moon_halo_color.outputs[0].default_value = (0.43, 0.76, 1.0, 1.0)

    moon_halo_scale = nodes.new("ShaderNodeVectorMath")
    moon_halo_scale.name = "W3 Sky Moon Halo"
    moon_halo_scale.operation = "SCALE"
    moon_halo_scale.location = (-110, -160)

    add_moon_halo = nodes.new("ShaderNodeMixRGB")
    add_moon_halo.name = _SKY_NODE_MOON_HALO_ADD
    add_moon_halo.location = (230, 220)
    add_moon_halo.blend_type = "MIX"
    add_moon_halo.inputs[0].default_value = 0.0

    global_brightness = nodes.new("ShaderNodeVectorMath")
    global_brightness.name = _SKY_NODE_GLOBAL_BRIGHTNESS
    global_brightness.label = "Global Sky Brightness"
    global_brightness.operation = "SCALE"
    global_brightness.location = (80, 260)
    global_brightness.inputs["Scale"].default_value = 1.0

    star_rotation = nodes.new("ShaderNodeVectorRotate")
    star_rotation.name = _SKY_NODE_STAR_ROTATION
    star_rotation.label = "Moon Trajectory Frame"
    star_rotation.location = (-700, -320)
    star_rotation.rotation_type = "AXIS_ANGLE"

    stars = nodes.new("ShaderNodeTexEnvironment")
    stars.name = _SKY_NODE_STARS
    stars.label = "Imported Stars Cubemap"
    stars.location = (-500, -320)
    stars.projection = "EQUIRECTANGULAR"
    stars.image = stars_image

    star_gamma = nodes.new("ShaderNodeGamma")
    star_gamma.name = "W3 Sky Star Shaping"
    star_gamma.location = (-280, -320)
    # Higher gamma keeps only the brighter stars, so the cubemap reads as a
    # sparse sharp field instead of a dense wash of equirectangular blobs.
    star_gamma.inputs[1].default_value = 4.6

    star_brightness = nodes.new("ShaderNodeMixRGB")
    star_brightness.name = "W3 Sky Star Brightness"
    star_brightness.location = (-70, -320)
    star_brightness.blend_type = "MULTIPLY"
    star_brightness.inputs[0].default_value = 1.0
    star_brightness.inputs[2].default_value = (1.0, 1.0, 1.0, 1.0)

    add_stars = nodes.new("ShaderNodeMixRGB")
    add_stars.name = _SKY_NODE_DAY_FACTOR
    add_stars.label = "Night Stars Factor"
    add_stars.location = (260, 160)
    add_stars.blend_type = "ADD"
    add_stars.inputs[0].default_value = 0.0

    cloud_abs = nodes.new("ShaderNodeMath")
    cloud_abs.name = "W3 Sky Cloud Abs Z"
    cloud_abs.operation = "ABSOLUTE"
    cloud_abs.location = (-1150, -520)

    cloud_denom = nodes.new("ShaderNodeMath")
    cloud_denom.name = "W3 Sky Cloud Denom"
    cloud_denom.operation = "ADD"
    cloud_denom.location = (-980, -520)
    cloud_denom.inputs[1].default_value = 0.3

    cloud_px = nodes.new("ShaderNodeMath")
    cloud_px.name = "W3 Sky Cloud X"
    cloud_px.operation = "DIVIDE"
    cloud_px.location = (-810, -450)

    cloud_py = nodes.new("ShaderNodeMath")
    cloud_py.name = "W3 Sky Cloud Y"
    cloud_py.operation = "DIVIDE"
    cloud_py.location = (-810, -600)

    cloud_coords = nodes.new("ShaderNodeCombineXYZ")
    cloud_coords.name = "W3 Sky Cloud Coords"
    cloud_coords.location = (-640, -520)

    cloud_noise = nodes.new("ShaderNodeTexNoise")
    cloud_noise.name = "W3 Sky Cloud Noise"
    cloud_noise.location = (-470, -520)
    cloud_noise.inputs["Scale"].default_value = 1.3
    cloud_noise.inputs["Detail"].default_value = 8.0
    roughness = cloud_noise.inputs.get("Roughness")
    if roughness is not None:
        roughness.default_value = 0.62

    cloud_ramp = nodes.new("ShaderNodeValToRGB")
    cloud_ramp.name = _SKY_NODE_CLOUD_RAMP
    cloud_ramp.location = (-280, -520)
    cloud_ramp.color_ramp.interpolation = "EASE"
    cloud_ramp.color_ramp.elements[0].position = 0.62
    cloud_ramp.color_ramp.elements[0].color = (0.0, 0.0, 0.0, 1.0)
    cloud_ramp.color_ramp.elements[1].position = 0.78
    cloud_ramp.color_ramp.elements[1].color = (1.0, 1.0, 1.0, 1.0)

    cloud_fade = nodes.new("ShaderNodeMapRange")
    cloud_fade.name = "W3 Sky Cloud Horizon Fade"
    cloud_fade.location = (-280, -700)
    cloud_fade.clamp = True
    cloud_fade.inputs[1].default_value = 0.015
    cloud_fade.inputs[2].default_value = 0.18
    cloud_fade.inputs[3].default_value = 0.0
    cloud_fade.inputs[4].default_value = 1.0

    cloud_mask = nodes.new("ShaderNodeMath")
    cloud_mask.name = "W3 Sky Cloud Mask"
    cloud_mask.operation = "MULTIPLY"
    cloud_mask.location = (-40, -560)

    cloud_opacity = nodes.new("ShaderNodeMath")
    cloud_opacity.name = _SKY_NODE_CLOUD_OPACITY
    cloud_opacity.label = "Cloud Amount"
    cloud_opacity.operation = "MULTIPLY"
    cloud_opacity.location = (120, -560)
    cloud_opacity.inputs[1].default_value = 1.0

    cloud_color = nodes.new("ShaderNodeRGB")
    cloud_color.name = _SKY_NODE_CLOUD_COLOR
    cloud_color.label = "Cloud Color"
    cloud_color.location = (120, -720)
    cloud_color.outputs[0].default_value = (0.8, 0.82, 0.87, 1.0)

    cloud_mix = nodes.new("ShaderNodeMixRGB")
    cloud_mix.name = _SKY_NODE_CLOUD_MIX
    cloud_mix.label = "Clouds Over Sky"
    cloud_mix.location = (460, 160)
    # The real weather particle adds a thin illuminated cloud layer.  Additive
    # noise preserves the authored sky/halo instead of replacing most of the
    # night sky with a flat grey procedural color.
    cloud_mix.blend_type = "ADD"
    cloud_mix.inputs[0].default_value = 0.0

    fog_color = nodes.new("ShaderNodeRGB")
    fog_color.name = _SKY_NODE_FOG_COLOR
    fog_color.label = "Global Fog Middle"
    fog_color.location = (300, -80)
    fog_color.outputs[0].default_value = (0.08, 0.30, 0.42, 1.0)

    fog_color_front = nodes.new("ShaderNodeRGB")
    fog_color_front.name = _SKY_NODE_FOG_COLOR_FRONT
    fog_color_front.label = "Global Fog Front"
    fog_color_front.location = (300, -210)
    fog_color_front.outputs[0].default_value = (0.08, 0.30, 0.42, 1.0)

    fog_color_back = nodes.new("ShaderNodeRGB")
    fog_color_back.name = _SKY_NODE_FOG_COLOR_BACK
    fog_color_back.label = "Global Fog Back"
    fog_color_back.location = (300, -340)
    fog_color_back.outputs[0].default_value = (0.08, 0.30, 0.42, 1.0)

    fog_direction = nodes.new("ShaderNodeCombineXYZ")
    fog_direction.name = _SKY_NODE_FOG_DIRECTION
    fog_direction.label = "Global Light Direction"
    fog_direction.location = (-120, -140)
    fog_direction.inputs[2].default_value = 1.0

    fog_dot = nodes.new("ShaderNodeVectorMath")
    fog_dot.name = "W3 Sky Fog Direction Dot"
    fog_dot.operation = "DOT_PRODUCT"
    fog_dot.location = (60, -140)

    fog_side = nodes.new("ShaderNodeMath")
    fog_side.name = "W3 Sky Fog Front Side"
    fog_side.operation = "GREATER_THAN"
    fog_side.location = (230, -470)
    fog_side.inputs[1].default_value = 0.0

    fog_side_color = nodes.new("ShaderNodeMixRGB")
    fog_side_color.name = "W3 Sky Fog Front Back"
    fog_side_color.blend_type = "MIX"
    fog_side_color.location = (480, -280)

    # Blend middle/front/back fog by squared directional alignment.
    fog_direction_square = nodes.new("ShaderNodeMath")
    fog_direction_square.name = "W3 Sky Fog Direction Squared"
    fog_direction_square.operation = "MULTIPLY"
    fog_direction_square.use_clamp = True
    fog_direction_square.location = (230, -590)

    fog_direction_weight = nodes.new("ShaderNodeMath")
    fog_direction_weight.name = _SKY_NODE_FOG_DIRECTION_WEIGHT
    fog_direction_weight.operation = "MULTIPLY"
    fog_direction_weight.use_clamp = True
    fog_direction_weight.location = (420, -590)
    fog_direction_weight.inputs[1].default_value = 1.0

    fog_directional_color = nodes.new("ShaderNodeMixRGB")
    fog_directional_color.name = "W3 Sky Fog Directional Color"
    fog_directional_color.blend_type = "MIX"
    fog_directional_color.location = (660, -180)

    # Compact vertical-density fit for sky rays:
    # opacity multiplier = 1 - 0.30 * smoothstep(0.30, 0.80, direction.z).
    fog_height_opacity = nodes.new("ShaderNodeMapRange")
    fog_height_opacity.name = "W3 Sky Fog Height Opacity"
    fog_height_opacity.interpolation_type = "SMOOTHSTEP"
    fog_height_opacity.clamp = True
    fog_height_opacity.location = (300, -740)
    fog_height_opacity.inputs[1].default_value = 0.30
    fog_height_opacity.inputs[2].default_value = 0.80
    fog_height_opacity.inputs[3].default_value = 1.0
    fog_height_opacity.inputs[4].default_value = 0.70

    fog_base_opacity = nodes.new("ShaderNodeValue")
    fog_base_opacity.name = _SKY_NODE_FOG_BASE_OPACITY
    fog_base_opacity.label = "Sky Fog Optical Depth"
    fog_base_opacity.location = (480, -820)

    fog_opacity = nodes.new("ShaderNodeMath")
    fog_opacity.name = "W3 Sky Fog Opacity"
    fog_opacity.operation = "MULTIPLY"
    fog_opacity.use_clamp = True
    fog_opacity.location = (660, -700)

    aerial_color = nodes.new("ShaderNodeRGB")
    aerial_color.name = _SKY_NODE_AERIAL_COLOR
    aerial_color.label = "Aerial Middle"
    aerial_color.location = (300, -960)
    aerial_color.outputs[0].default_value = (1.0, 1.0, 1.0, 1.0)

    aerial_color_front = nodes.new("ShaderNodeRGB")
    aerial_color_front.name = _SKY_NODE_AERIAL_COLOR_FRONT
    aerial_color_front.label = "Aerial Front"
    aerial_color_front.location = (300, -1090)
    aerial_color_front.outputs[0].default_value = (1.0, 1.0, 1.0, 1.0)

    aerial_color_back = nodes.new("ShaderNodeRGB")
    aerial_color_back.name = _SKY_NODE_AERIAL_COLOR_BACK
    aerial_color_back.label = "Aerial Back"
    aerial_color_back.location = (300, -1220)
    aerial_color_back.outputs[0].default_value = (1.0, 1.0, 1.0, 1.0)

    aerial_side_color = nodes.new("ShaderNodeMixRGB")
    aerial_side_color.name = "W3 Sky Aerial Front Back"
    aerial_side_color.blend_type = "MIX"
    aerial_side_color.location = (480, -1080)

    aerial_directional_color = nodes.new("ShaderNodeMixRGB")
    aerial_directional_color.name = "W3 Sky Aerial Directional Color"
    aerial_directional_color.blend_type = "MIX"
    aerial_directional_color.location = (660, -980)

    aerial_luminance = nodes.new("ShaderNodeRGBToBW")
    aerial_luminance.name = "W3 Sky Aerial Luminance"
    aerial_luminance.location = (660, -850)

    aerial_scale = nodes.new("ShaderNodeVectorMath")
    aerial_scale.name = "W3 Sky Aerial Scattering"
    aerial_scale.operation = "SCALE"
    aerial_scale.location = (850, -900)

    aerial_base_opacity = nodes.new("ShaderNodeValue")
    aerial_base_opacity.name = _SKY_NODE_AERIAL_BASE_OPACITY
    aerial_base_opacity.label = "Aerial Optical Depth"
    aerial_base_opacity.location = (480, -1360)

    aerial_opacity = nodes.new("ShaderNodeMath")
    aerial_opacity.name = "W3 Sky Aerial Opacity"
    aerial_opacity.operation = "MULTIPLY"
    aerial_opacity.use_clamp = True
    aerial_opacity.location = (660, -1280)

    aerial_mix = nodes.new("ShaderNodeMixRGB")
    aerial_mix.name = _SKY_NODE_AERIAL_MIX
    aerial_mix.label = "Aerial Perspective"
    aerial_mix.blend_type = "MIX"
    aerial_mix.location = (860, 160)

    fog_mix = nodes.new("ShaderNodeMixRGB")
    fog_mix.name = _SKY_NODE_FOG_MIX
    fog_mix.label = "Sky Fog"
    fog_mix.location = (1060, 160)
    fog_mix.blend_type = "MIX"
    fog_mix.inputs[0].default_value = 0.0

    background = nodes.new("ShaderNodeBackground")
    background.name = _SKY_NODE_BRIGHTNESS
    background.location = (1260, 160)

    output = nodes.new("ShaderNodeOutputWorld")
    output.name = "W3 Sky Output"
    output.location = (1460, 160)

    # In a Blender World, Normal is the camera-to-sky ray.
    direction_output = texcoord.outputs["Normal"]
    links.new(direction_output, separate.inputs[0])
    links.new(direction_output, view_xy.inputs[0])
    links.new(view_xy.outputs[0], view_xy_normalize.inputs[0])
    links.new(sun_direction.outputs[0], sun_xy.inputs[0])
    links.new(sun_xy.outputs[0], sun_xy_normalize.inputs[0])
    links.new(view_xy_normalize.outputs[0], sun_xy_dot.inputs[0])
    links.new(sun_xy_normalize.outputs[0], sun_xy_dot.inputs[1])
    links.new(sun_xy_dot.outputs["Value"], sun_facing.inputs[0])
    links.new(sun_facing.outputs[0], sun_horizon_direction.inputs[0])
    links.new(sun_horizon_back.outputs[0], sun_horizon_direction.inputs[1])
    links.new(sun_horizon_front.outputs[0], sun_horizon_direction.inputs[2])

    links.new(separate.outputs[2], z_square.inputs[0])
    links.new(separate.outputs[2], z_square.inputs[1])
    links.new(z_square.outputs[0], horizon_weight.inputs[1])
    links.new(horizon_weight.outputs[0], sun_horizon_weight.inputs[0])
    links.new(sun_horizon_weight.outputs[0], directional_horizon.inputs[0])
    links.new(horizon_color.outputs[0], directional_horizon.inputs[1])
    links.new(sun_horizon_direction.outputs[0], directional_horizon.inputs[2])

    links.new(separate.outputs[2], horizon_z.inputs[0])
    links.new(camera_height.outputs[0], horizon_z.inputs[1])
    links.new(horizon_z.outputs[0], horizon_attenuation.inputs[0])
    links.new(horizon_attenuation.outputs[0], horizon_bias.inputs[0])
    links.new(horizon_bias.outputs[0], horizon_min.inputs[0])
    links.new(horizon_min.outputs[0], horizon_reciprocal.inputs[1])
    links.new(horizon_reciprocal.outputs[0], horizon_power.inputs[0])
    links.new(horizon_power.outputs[0], horizon_factor.inputs[1])
    links.new(horizon_factor.outputs[0], gradient.inputs[0])
    links.new(directional_horizon.outputs[0], gradient.inputs[1])
    links.new(zenith_color.outputs[0], gradient.inputs[2])

    links.new(direction_output, halo_dot.inputs[0])
    links.new(sun_direction.outputs[0], halo_dot.inputs[1])
    links.new(halo_dot.outputs["Value"], halo_clamp.inputs[0])
    links.new(halo_clamp.outputs[0], halo_core.inputs[0])
    links.new(halo_core.outputs[0], halo_core_scale.inputs[0])
    links.new(halo_clamp.outputs[0], halo_veil.inputs[0])
    links.new(halo_veil.outputs[0], halo_veil_scale.inputs[0])
    links.new(halo_core_scale.outputs[0], halo_sum.inputs[0])
    links.new(halo_veil_scale.outputs[0], halo_sum.inputs[1])
    links.new(halo_color.outputs[0], halo_scale.inputs[0])
    links.new(halo_sum.outputs[0], halo_scale.inputs["Scale"])

    links.new(direction_output, moon_dot.inputs[0])
    links.new(moon_direction.outputs[0], moon_dot.inputs[1])
    links.new(moon_dot.outputs["Value"], moon_clamp.inputs[0])
    links.new(moon_clamp.outputs[0], moon_core.inputs[0])
    links.new(moon_core.outputs[0], moon_core_scale.inputs[0])
    links.new(moon_clamp.outputs[0], moon_veil.inputs[0])
    links.new(moon_veil.outputs[0], moon_veil_scale.inputs[0])
    links.new(moon_core_scale.outputs[0], moon_sum.inputs[0])
    links.new(moon_veil_scale.outputs[0], moon_sum.inputs[1])
    links.new(moon_halo_color.outputs[0], moon_halo_scale.inputs[0])
    links.new(moon_sum.outputs[0], moon_halo_scale.inputs["Scale"])

    links.new(moon_core_scale.outputs[0], add_moon_halo.inputs[0])
    links.new(gradient.outputs[0], add_moon_halo.inputs[1])
    links.new(moon_halo_color.outputs[0], add_moon_halo.inputs[2])
    links.new(halo_core_scale.outputs[0], add_halo.inputs[0])
    links.new(add_moon_halo.outputs[0], add_halo.inputs[1])
    links.new(halo_color.outputs[0], add_halo.inputs[2])
    links.new(add_halo.outputs[0], global_brightness.inputs[0])

    links.new(direction_output, star_rotation.inputs["Vector"])
    links.new(star_rotation.outputs[0], stars.inputs[0])
    links.new(stars.outputs[0], star_gamma.inputs[0])
    links.new(star_gamma.outputs[0], star_brightness.inputs[1])
    links.new(global_brightness.outputs[0], add_stars.inputs[1])
    links.new(star_brightness.outputs[0], add_stars.inputs[2])

    links.new(separate.outputs[2], cloud_abs.inputs[0])
    links.new(cloud_abs.outputs[0], cloud_denom.inputs[0])
    links.new(separate.outputs[0], cloud_px.inputs[0])
    links.new(cloud_denom.outputs[0], cloud_px.inputs[1])
    links.new(separate.outputs[1], cloud_py.inputs[0])
    links.new(cloud_denom.outputs[0], cloud_py.inputs[1])
    links.new(cloud_px.outputs[0], cloud_coords.inputs[0])
    links.new(cloud_py.outputs[0], cloud_coords.inputs[1])
    links.new(cloud_coords.outputs[0], cloud_noise.inputs[0])
    links.new(cloud_noise.outputs["Fac"], cloud_ramp.inputs[0])
    links.new(separate.outputs[2], cloud_fade.inputs[0])
    links.new(cloud_ramp.outputs[0], cloud_mask.inputs[0])
    links.new(cloud_fade.outputs[0], cloud_mask.inputs[1])
    links.new(cloud_mask.outputs[0], cloud_opacity.inputs[0])
    links.new(add_stars.outputs[0], cloud_mix.inputs[1])
    links.new(cloud_color.outputs[0], cloud_mix.inputs[2])
    links.new(cloud_opacity.outputs[0], cloud_mix.inputs[0])

    links.new(direction_output, fog_dot.inputs[0])
    links.new(fog_direction.outputs[0], fog_dot.inputs[1])
    links.new(fog_dot.outputs["Value"], fog_side.inputs[0])
    links.new(fog_side.outputs[0], fog_side_color.inputs[0])
    links.new(fog_color_back.outputs[0], fog_side_color.inputs[1])
    links.new(fog_color_front.outputs[0], fog_side_color.inputs[2])
    links.new(fog_dot.outputs["Value"], fog_direction_square.inputs[0])
    links.new(fog_dot.outputs["Value"], fog_direction_square.inputs[1])
    links.new(fog_direction_square.outputs[0], fog_direction_weight.inputs[0])
    links.new(fog_direction_weight.outputs[0], fog_directional_color.inputs[0])
    links.new(fog_color.outputs[0], fog_directional_color.inputs[1])
    links.new(fog_side_color.outputs[0], fog_directional_color.inputs[2])
    links.new(separate.outputs[2], fog_height_opacity.inputs[0])
    links.new(fog_height_opacity.outputs[0], fog_opacity.inputs[0])
    links.new(fog_base_opacity.outputs[0], fog_opacity.inputs[1])
    links.new(fog_directional_color.outputs[0], fog_mix.inputs[2])
    links.new(fog_opacity.outputs[0], fog_mix.inputs[0])

    links.new(fog_side.outputs[0], aerial_side_color.inputs[0])
    links.new(aerial_color_back.outputs[0], aerial_side_color.inputs[1])
    links.new(aerial_color_front.outputs[0], aerial_side_color.inputs[2])
    links.new(fog_direction_weight.outputs[0], aerial_directional_color.inputs[0])
    links.new(aerial_color.outputs[0], aerial_directional_color.inputs[1])
    links.new(aerial_side_color.outputs[0], aerial_directional_color.inputs[2])
    links.new(cloud_mix.outputs[0], aerial_luminance.inputs[0])
    links.new(aerial_directional_color.outputs[0], aerial_scale.inputs[0])
    links.new(aerial_luminance.outputs[0], aerial_scale.inputs["Scale"])
    links.new(fog_height_opacity.outputs[0], aerial_opacity.inputs[0])
    links.new(aerial_base_opacity.outputs[0], aerial_opacity.inputs[1])
    links.new(cloud_mix.outputs[0], aerial_mix.inputs[1])
    links.new(aerial_scale.outputs[0], aerial_mix.inputs[2])
    links.new(aerial_opacity.outputs[0], aerial_mix.inputs[0])
    links.new(aerial_mix.outputs[0], fog_mix.inputs[1])
    links.new(fog_mix.outputs[0], background.inputs[0])
    links.new(background.outputs[0], output.inputs[0])


def _update_environment_world(
    scene,
    *,
    stars_image=None,
    replace_stars_image: bool = False,
    sky_zenith_color=(0.15, 0.35, 0.8),
    sky_horizon_color=(0.8, 0.5, 0.35),
    sun_horizon_color=(0.85, 0.9, 1.0),
    sun_back_horizon_color=(0.364, 0.307, 0.298),
    sun_direction=(0.0, 0.0, 1.0),
    moon_direction=(0.0, 0.0, -1.0),
    sun_color=(1.0, 0.75, 0.35),
    moon_color=(0.55, 0.65, 1.0),
    sun_sky_color=(0.55, 0.78, 1.0),
    sun_sky_brightness: float = 1.0,
    sun_area_sky_size: float = 0.33,
    sun_influence: float = 1.0,
    moon_sky_color=(0.55, 0.78, 1.0),
    moon_sky_brightness: float = 1.0,
    moon_area_sky_size: float = 0.33,
    moon_influence: float = 0.0,
    sky_brightness: float = 1.0,
    fog_color=(0.08, 0.30, 0.42),
    fog_color_front=None,
    fog_color_middle=None,
    fog_color_back=None,
    aerial_color_front=(1.0, 1.0, 1.0),
    aerial_color_middle=(1.0, 1.0, 1.0),
    aerial_color_back=(1.0, 1.0, 1.0),
    fog_direction=(0.0, 0.0, 1.0),
    fog_sky_density: float = 0.0,
    fog_density: float = 0.0,
    fog_dist_clamp: float = 0.0,
    fog_final_exp: float = 1.0,
    aerial_final_exp: float = 1.0,
    sky_day_factor: float = 1.0,
    horizon_attenuation: float = 1.8,
    stars_brightness: float = 1.1,
    cloud_amount: float = 0.45,
):
    world = _ensure_environment_world(scene)
    nodes = world.node_tree.nodes
    existing_stars_image = None
    existing_stars = nodes.get(_SKY_NODE_STARS)
    if existing_stars is not None and not replace_stars_image:
        existing_stars_image = getattr(existing_stars, "image", None)
    if (
        nodes.get(_SKY_NODE_CLOUD_MIX) is None
        or nodes.get(_SKY_NODE_STAR_ROTATION) is None
        or nodes.get(_SKY_NODE_MOON_HALO_ADD) is None
        or nodes.get(_SKY_NODE_GLOBAL_BRIGHTNESS) is None
        or nodes.get(_SKY_NODE_FOG_MIX) is None
        or nodes.get(_SKY_NODE_AERIAL_MIX) is None
        or nodes.get(_SKY_NODE_FOG_DIRECTION) is None
        or nodes.get(_SKY_NODE_HORIZON_POWER) is None
    ):
        _build_environment_world_nodes(
            world,
            stars_image=stars_image if replace_stars_image else existing_stars_image,
        )
        nodes = world.node_tree.nodes

    stars = nodes.get(_SKY_NODE_STARS)
    if stars is not None and (replace_stars_image or stars_image is not None):
        stars.image = stars_image

    day = max(0.0, min(1.0, float(sky_day_factor)))
    sun_vector = _vector3(sun_direction, (0.0, 0.0, 1.0))
    for node_name, color in (
        (_SKY_NODE_ZENITH, sky_zenith_color),
        (_SKY_NODE_HORIZON, sky_horizon_color),
        (_SKY_NODE_SUN_HORIZON_FRONT, sun_horizon_color),
        (_SKY_NODE_SUN_HORIZON_BACK, sun_back_horizon_color),
    ):
        color_node = nodes.get(node_name)
        if color_node is not None:
            color_node.outputs[0].default_value = _rgba(color)

    camera = getattr(scene, "camera", None)
    camera_z = 0.0
    if camera is not None:
        try:
            camera_z = float(camera.matrix_world.translation.z)
        except Exception:
            camera_z = float(getattr(camera.location, "z", 0.0))
    camera_height = nodes.get(_SKY_NODE_HORIZON_CAMERA_HEIGHT)
    if camera_height is not None:
        camera_height.outputs[0].default_value = (camera_z + 710.0) / 1000.0
    attenuation_node = nodes.get(_SKY_NODE_HORIZON_ATTENUATION)
    if attenuation_node is not None:
        attenuation_node.inputs[1].default_value = max(0.1, float(horizon_attenuation))

    sun_direction_node = nodes.get(_SKY_NODE_SUN_DIRECTION)
    if sun_direction_node is not None:
        for index in range(3):
            sun_direction_node.inputs[index].default_value = float(sun_vector[index])

    halo_color = nodes.get(_SKY_NODE_HALO_COLOR)
    if halo_color is not None:
        sky_rgb = tuple(
            channel * max(0.0, float(sun_sky_brightness))
            for channel in _rgb(sun_sky_color)
        )
        halo_color.outputs[0].default_value = (*sky_rgb, 1.0)
    sun_area = nodes.get("W3 Sky Halo Core")
    if sun_area is not None:
        sun_area.inputs[1].default_value = max(0.0001, float(sun_area_sky_size))
    sun_amount = nodes.get("W3 Sky Halo Core Scale")
    clamped_sun_influence = max(0.0, min(1.0, float(sun_influence)))
    if sun_amount is not None:
        sun_amount.inputs[1].default_value = clamped_sun_influence
    sun_horizon_amount = nodes.get("W3 Sky Sun Horizon Influence")
    if sun_horizon_amount is not None:
        sun_horizon_amount.inputs[1].default_value = clamped_sun_influence

    moon_vector = _vector3(moon_direction, (0.0, 0.0, -1.0))
    moon_direction_node = nodes.get(_SKY_NODE_MOON_DIRECTION)
    if moon_direction_node is not None:
        for index in range(3):
            moon_direction_node.inputs[index].default_value = float(moon_vector[index])

    moon_halo_color = nodes.get(_SKY_NODE_MOON_HALO_COLOR)
    if moon_halo_color is not None:
        sky_rgb = tuple(
            channel * max(0.0, float(moon_sky_brightness))
            for channel in _rgb(moon_sky_color)
        )
        moon_halo_color.outputs[0].default_value = (*sky_rgb, 1.0)
    moon_area = nodes.get("W3 Sky Moon Halo Core")
    if moon_area is not None:
        moon_area.inputs[1].default_value = max(0.0001, float(moon_area_sky_size))
    moon_amount = nodes.get("W3 Sky Moon Halo Core Scale")
    if moon_amount is not None:
        moon_amount.inputs[1].default_value = max(0.0, min(1.0, float(moon_influence)))

    global_brightness = nodes.get(_SKY_NODE_GLOBAL_BRIGHTNESS)
    if global_brightness is not None:
        global_brightness.inputs["Scale"].default_value = max(0.0, float(sky_brightness))

    star_scale = nodes.get("W3 Sky Star Brightness")
    if star_scale is not None:
        scale = max(0.0, float(stars_brightness))
        star_scale.inputs[2].default_value = (scale, scale, scale, 1.0)
    star_shaping = nodes.get("W3 Sky Star Shaping")
    if star_shaping is not None:
        star_shaping.inputs[1].default_value = 4.6

    star_rotation = nodes.get(_SKY_NODE_STAR_ROTATION)
    if star_rotation is not None:
        axis, angle = _star_rotation(moon_direction)
        star_rotation.inputs["Axis"].default_value = tuple(axis)
        star_rotation.inputs["Angle"].default_value = float(angle)

    add_stars = nodes.get(_SKY_NODE_DAY_FACTOR)
    if add_stars is not None:
        # A missing stars image must contribute nothing: an unassigned
        # Environment Texture outputs magenta, which used to wash the whole
        # night sky purple.
        has_stars = stars is not None and getattr(stars, "image", None) is not None
        add_stars.inputs[0].default_value = (1.0 - day) if has_stars else 0.0

    amount = max(0.0, min(1.0, float(cloud_amount)))
    cloud_ramp = nodes.get(_SKY_NODE_CLOUD_RAMP)
    if cloud_ramp is not None:
        start = 0.84 - 0.30 * amount
        cloud_ramp.color_ramp.elements[0].position = start
        cloud_ramp.color_ramp.elements[1].position = min(1.0, start + 0.18)

    cloud_opacity = nodes.get(_SKY_NODE_CLOUD_OPACITY)
    if cloud_opacity is not None:
        cloud_opacity.inputs[1].default_value = 0.0 if amount <= 0.01 else 0.70

    cloud_color = nodes.get(_SKY_NODE_CLOUD_COLOR)
    if cloud_color is not None:
        brightness = 0.04 + 0.24 * day
        cloud_rgb = tuple(
            channel * brightness
            for channel in (0.62, 0.87, 1.25)
        )
        cloud_color.outputs[0].default_value = (*cloud_rgb, 1.0)

    middle_color = fog_color if fog_color_middle is None else fog_color_middle
    for node_name, color in (
        (_SKY_NODE_FOG_COLOR, middle_color),
        (_SKY_NODE_FOG_COLOR_FRONT, middle_color if fog_color_front is None else fog_color_front),
        (_SKY_NODE_FOG_COLOR_BACK, middle_color if fog_color_back is None else fog_color_back),
    ):
        fog_color_node = nodes.get(node_name)
        if fog_color_node is not None:
            # Compress HDR fog uniformly so Blender preserves its hue.
            fog_color_node.outputs[0].default_value = _fog_volume_color(color)

    for node_name, color in (
        (_SKY_NODE_AERIAL_COLOR, aerial_color_middle),
        (_SKY_NODE_AERIAL_COLOR_FRONT, aerial_color_front),
        (_SKY_NODE_AERIAL_COLOR_BACK, aerial_color_back),
    ):
        color_node = nodes.get(node_name)
        if color_node is not None:
            # Aerial colors are HDR and are tone-mapped after the sky/fog pass.
            color_node.outputs[0].default_value = _rgba(color)

    fog_vector = _vector3(fog_direction, (0.0, 0.0, 1.0))
    fog_direction_node = nodes.get(_SKY_NODE_FOG_DIRECTION)
    if fog_direction_node is not None:
        for index in range(3):
            fog_direction_node.inputs[index].default_value = float(fog_vector[index])

    distance = max(0.0, float(fog_dist_clamp))
    direction_weight = nodes.get(_SKY_NODE_FOG_DIRECTION_WEIGHT)
    if direction_weight is not None:
        direction_weight.inputs[1].default_value = (
            max(0.0, min(1.0, (distance - 150.0) / 500.0))
            if distance > 0.0
            else 1.0
        )

    sky_density = max(0.0, float(fog_sky_density))
    if distance > 0.0:
        optical_depth = min(80.0, max(0.0, float(fog_density)) * sky_density * distance)
    else:
        optical_depth = sky_density * _BLENDER_SKY_FOG_DENSITY_SCALE
    raw_opacity = 1.0 - math.exp(-optical_depth)
    opacity = math.pow(raw_opacity, max(0.0, float(fog_final_exp)))
    aerial_opacity = math.pow(raw_opacity, max(0.0, float(aerial_final_exp)))
    fog_base_opacity = nodes.get(_SKY_NODE_FOG_BASE_OPACITY)
    if fog_base_opacity is not None:
        fog_base_opacity.outputs[0].default_value = opacity
    aerial_base_opacity = nodes.get(_SKY_NODE_AERIAL_BASE_OPACITY)
    if aerial_base_opacity is not None:
        aerial_base_opacity.outputs[0].default_value = aerial_opacity
    fog_mix = nodes.get(_SKY_NODE_FOG_MIX)
    if fog_mix is not None:
        # Retain a useful unlinked value for old files and UI diagnostics; the
        # managed graph drives this socket from the height-shaped opacity node.
        fog_mix.inputs[0].default_value = opacity

    background = nodes.get(_SKY_NODE_BRIGHTNESS)
    if background is not None:
        # Stars and layered cloud materials are composited after sky brightness.
        background.inputs[1].default_value = 1.0
    world.color = _rgb(sky_zenith_color)
    return world


def _collection_is_below(parent, target) -> bool:
    for child in getattr(parent, "children", ()):
        if child == target or _collection_is_below(child, target):
            return True
    return False


def _iter_scene_collections(parent):
    for child in getattr(parent, "children", ()):
        yield child
        yield from _iter_scene_collections(child)


def _is_managed(item) -> bool:
    try:
        return bool(item.get(_MANAGED_PROP, False))
    except Exception:
        return False


def _role(item) -> str:
    try:
        return str(item.get(_ROLE_PROP, "") or "")
    except Exception:
        return ""


def ensure_environment_collection(context) -> Any:
    scene = context.scene
    for collection in _iter_scene_collections(scene.collection):
        if _is_managed(collection):
            return collection

    preferred = bpy.data.collections.get(ENVIRONMENT_COLLECTION_NAME)
    if preferred is not None and (
        not _is_managed(preferred)
        or not _collection_is_below(scene.collection, preferred)
    ):
        # Do not reuse a same-named user collection or a preview owned by a
        # different scene. Blender will suffix the new collection as needed.
        preferred = None
    collection = preferred or bpy.data.collections.new(ENVIRONMENT_COLLECTION_NAME)
    collection[_MANAGED_PROP] = True
    if not str(collection.get(_PREVIEW_OWNER_PROP, "") or ""):
        collection[_PREVIEW_OWNER_PROP] = collection.name
    collection[_ROLE_PROP] = "collection"
    if not _collection_is_below(scene.collection, collection):
        scene.collection.children.link(collection)
    return collection


def _find_layer_collection(layer_collection, collection):
    if getattr(layer_collection, "collection", None) == collection:
        return layer_collection
    for child in getattr(layer_collection, "children", ()):
        found = _find_layer_collection(child, collection)
        if found is not None:
            return found
    return None


@contextmanager
def _active_collection(context, collection):
    view_layer = context.view_layer
    previous = getattr(view_layer, "active_layer_collection", None)
    target = _find_layer_collection(view_layer.layer_collection, collection)
    try:
        if target is not None:
            view_layer.active_layer_collection = target
        yield
    finally:
        if previous is not None:
            try:
                view_layer.active_layer_collection = previous
            except Exception:
                pass


@contextmanager
def _preserve_selection(context):
    selected = list(getattr(context, "selected_objects", ()) or ())
    active = getattr(getattr(context, "view_layer", None), "objects", None)
    active = getattr(active, "active", None)
    try:
        yield
    finally:
        try:
            bpy.ops.object.select_all(action="DESELECT")
        except Exception:
            pass
        for obj in selected:
            if getattr(obj, "name", "") in bpy.data.objects:
                try:
                    obj.select_set(True)
                except Exception:
                    pass
        if active is not None and getattr(active, "name", "") in bpy.data.objects:
            try:
                context.view_layer.objects.active = active
            except Exception:
                pass


def _find_role_objects(collection, role: str) -> list[Any]:
    return [obj for obj in collection.objects if _is_managed(obj) and _role(obj) == role]


def _tag(item, role: str) -> None:
    item[_MANAGED_PROP] = True
    item[_ROLE_PROP] = role


def _new_empty(collection, name: str, role: str):
    obj = bpy.data.objects.new(name, None)
    obj.empty_display_type = "PLAIN_AXES"
    obj.empty_display_size = 1.0
    _tag(obj, role)
    collection.objects.link(obj)
    return obj


def _ensure_role_empty(collection, name: str, role: str):
    found = _find_role_objects(collection, role)
    obj = found[0] if found else _new_empty(collection, name, role)
    for duplicate in found[1:]:
        _remove_object(duplicate)
    return obj


def _ensure_anchor(context, collection):
    anchor = _ensure_role_empty(collection, ENVIRONMENT_ANCHOR_NAME, _ROLE_ANCHOR)
    camera = getattr(context.scene, "camera", None)
    anchor.parent = None
    anchor.rotation_euler = (0.0, 0.0, 0.0)
    anchor.scale = (1.0, 1.0, 1.0)
    _remove_anchor_camera_constraint(anchor)
    if camera is not None:
        anchor.location = camera.matrix_world.translation
    return anchor


def _view_camera(window, space):
    if bool(getattr(space, "use_local_camera", False)):
        camera = getattr(space, "camera", None)
        if getattr(camera, "type", None) == "CAMERA":
            return camera
    scene = getattr(window, "scene", None)
    camera = getattr(scene, "camera", None)
    return camera if getattr(camera, "type", None) == "CAMERA" else None


def _remove_anchor_camera_constraint(anchor) -> None:
    constraint = anchor.constraints.get(ENVIRONMENT_CAMERA_CONSTRAINT_NAME)
    if constraint is not None:
        anchor.constraints.remove(constraint)


def _pointer(value) -> int:
    try:
        return int(value.as_pointer())
    except (AttributeError, ReferenceError, TypeError):
        return id(value)


def _ensure_preview_clip_end(scene, target, reach: float, states) -> bool:
    if target is None:
        return False
    current = float(getattr(target, "clip_end", 0.0))
    key = (_pointer(scene), _pointer(target))
    state = states.get(key)
    if state is not None and abs(current - state[2]) > 1.0e-4:
        # A user edit made while Preview is active becomes the new base value.
        states.pop(key, None)
        state = None
    reach = float(reach)
    if current >= reach:
        return False
    previous = state[1] if state is not None else current
    target.clip_end = reach
    states[key] = (target, previous, reach)
    return True


def _ensure_camera_clip(scene, camera, reach: float) -> bool:
    return _ensure_preview_clip_end(
        scene,
        getattr(camera, "data", None),
        reach,
        _CAMERA_CLIP_END_STATES,
    )


def _ensure_view_clip(scene, space, reach: float) -> bool:
    return _ensure_preview_clip_end(scene, space, reach, _VIEW_CLIP_END_STATES)


def _restore_preview_clip_ends(scene) -> int:
    scene_pointer = _pointer(scene)
    restored = 0
    for states in (_VIEW_CLIP_END_STATES, _CAMERA_CLIP_END_STATES):
        for key, (target, previous, active) in list(states.items()):
            if key[0] != scene_pointer:
                continue
            try:
                if abs(float(target.clip_end) - active) <= 1.0e-4:
                    target.clip_end = previous
                    restored += 1
            except (AttributeError, ReferenceError, TypeError):
                pass
            states.pop(key, None)
    return restored


def _preview_anchors():
    found = []
    for scene in bpy.data.scenes:
        for collection in _iter_scene_collections(scene.collection):
            if _is_managed(collection):
                anchors = _find_role_objects(collection, _ROLE_ANCHOR)
                if anchors:
                    found.append((scene, collection, anchors[0]))
    return found


def _main_view3d(window):
    best = None
    for area in getattr(window.screen, "areas", ()):
        if area.type == "VIEW_3D":
            if best is None or area.width * area.height > best.width * best.height:
                best = area
    if best is None:
        return None, None
    space = best.spaces.active
    return space, getattr(space, "region_3d", None)


def _celestial_reach(collection) -> float:
    reach = CELESTIAL_DISTANCE
    for role in (_ROLE_SUN_ROOT, _ROLE_MOON_ROOT):
        for root in _find_role_objects(collection, role):
            reach = max(reach, Vector(root.location).length + max(root.scale))
    for root in _find_role_objects(collection, _ROLE_CLOUD_ROOT):
        for obj in _find_role_objects(collection, _ROLE_CLOUD_GEOMETRY):
            if obj.parent != root or getattr(obj, "type", "") != "MESH":
                continue
            radius = max((Vector(corner).length for corner in obj.bound_box), default=0.0)
            reach = max(reach, radius * max(root.scale))
    return reach


def _camera_yaw_axes(view_matrix_world=None) -> tuple[Vector, Vector, Vector]:
    """Return a yaw-only camera forward/right/up basis in Blender space."""

    forward = Vector((0.0, -1.0, 0.0))
    if view_matrix_world is not None:
        try:
            candidate = view_matrix_world.to_quaternion() @ Vector((0.0, 0.0, -1.0))
            candidate.z = 0.0
            if candidate.length_squared > 1.0e-12:
                forward = candidate.normalized()
        except Exception:
            pass
    right = Vector((-forward.y, forward.x, 0.0))
    return forward, right, Vector((0.0, 0.0, 1.0))


def _update_camera_light_positions(collection, view_matrix_world=None) -> int:
    forward, right, up = _camera_yaw_axes(view_matrix_world)
    updated = 0
    for light_obj in _find_role_objects(collection, _ROLE_CAMERA_LIGHT):
        target = (
            forward * float(light_obj.get(_CAMERA_LIGHT_FRONT_PROP, 0.0))
            + right * float(light_obj.get(_CAMERA_LIGHT_RIGHT_PROP, 0.0))
            + up * float(light_obj.get(_CAMERA_LIGHT_UP_PROP, 0.0))
        )
        if (Vector(light_obj.location) - target).length_squared > 1.0e-8:
            light_obj.location = target
            updated += 1
    return updated


def _follow_viewports():
    """Keep the sky centered on each viewport until no preview remains."""

    try:
        anchors = _preview_anchors()
        if not anchors:
            return None
        by_scene = {scene.name: (collection, anchor) for scene, collection, anchor in anchors}
        for manager in bpy.data.window_managers:
            for window in manager.windows:
                entry = by_scene.get(getattr(window.scene, "name", ""))
                if entry is None:
                    continue
                collection, anchor = entry
                space, region = _main_view3d(window)
                if region is None:
                    continue
                if region.view_perspective == "CAMERA":
                    camera = _view_camera(window, space)
                    _remove_anchor_camera_constraint(anchor)
                    if camera is not None:
                        eye = camera.matrix_world.translation
                        if (Vector(anchor.location) - eye).length_squared > 1.0e-8:
                            anchor.location = eye
                        _ensure_camera_clip(window.scene, camera, _celestial_reach(collection) * 1.1)
                        _update_camera_light_positions(collection, camera.matrix_world)
                    continue
                _remove_anchor_camera_constraint(anchor)
                eye = region.view_matrix.inverted().translation
                if (Vector(anchor.location) - eye).length_squared > 1.0e-8:
                    anchor.location = eye
                _update_camera_light_positions(collection, region.view_matrix.inverted())
                reach = _celestial_reach(collection) * 1.1
                _ensure_view_clip(window.scene, space, reach)
    except Exception:
        log.exception("Environment viewport follow stopped")
        return None
    return 0.1


def _restore_render_anchor(scene=None, *_args):
    # Timeline markers can switch the camera after preview creation.
    updated = 0
    for preview_scene, collection, anchor in _preview_anchors():
        if scene is not None and preview_scene.name != scene.name:
            continue
        camera = getattr(preview_scene, "camera", None)
        _remove_anchor_camera_constraint(anchor)
        if camera is not None:
            # Scripted camera edits may not have updated the dependency graph yet.
            for view_layer in preview_scene.view_layers:
                view_layer.update()
            anchor.location = camera.matrix_world.translation
            _ensure_camera_clip(preview_scene, camera, _celestial_reach(collection) * 1.1)
            _update_camera_light_positions(collection, camera.matrix_world)
            updated += 1
    return updated


def _ensure_view_follow() -> None:
    # Render handlers work in background mode; viewport following needs a window.
    for handlers in (bpy.app.handlers.render_init, bpy.app.handlers.render_pre):
        if _restore_render_anchor not in handlers:
            handlers.append(_restore_render_anchor)
    if bpy.app.background:
        return
    if not bpy.app.timers.is_registered(_follow_viewports):
        bpy.app.timers.register(_follow_viewports, first_interval=0.2)


def stop_preview_runtime() -> None:
    if bpy.app.timers.is_registered(_follow_viewports):
        bpy.app.timers.unregister(_follow_viewports)
    for handlers in (bpy.app.handlers.render_init, bpy.app.handlers.render_pre):
        while _restore_render_anchor in handlers:
            handlers.remove(_restore_render_anchor)


def _ensure_celestial_root(collection, anchor, role: str):
    name = ENVIRONMENT_SUN_NAME if role == _ROLE_SUN_ROOT else ENVIRONMENT_MOON_NAME
    root = _ensure_role_empty(collection, name, role)
    if root.parent != anchor:
        root.parent = anchor
    return root


def _ensure_cloud_root(collection, anchor):
    root = _ensure_role_empty(collection, ENVIRONMENT_CLOUD_NAME, _ROLE_CLOUD_ROOT)
    if root.parent != anchor:
        root.parent = anchor
    root.matrix_parent_inverse.identity()
    root.location = (0.0, 0.0, 0.0)
    root.rotation_mode = "XYZ"
    root.rotation_euler = (0.0, 0.0, 0.0)
    return root


def _ensure_key_light(collection, anchor):
    found = _find_role_objects(collection, _ROLE_KEY_LIGHT)
    light_obj = found[0] if found else None
    for duplicate in found[1:]:
        _remove_object(duplicate)
    if light_obj is None or getattr(light_obj, "type", "") != "LIGHT":
        if light_obj is not None:
            _remove_object(light_obj)
        light_data = bpy.data.lights.new(ENVIRONMENT_LIGHT_NAME, type="SUN")
        light_obj = bpy.data.objects.new(ENVIRONMENT_LIGHT_NAME, light_data)
        _tag(light_obj, _ROLE_KEY_LIGHT)
        collection.objects.link(light_obj)
    elif light_obj.data.type != "SUN":
        light_obj.data.type = "SUN"
    light_obj.data.use_shadow = True
    if hasattr(light_obj.data, "shadow_maximum_resolution"):
        light_obj.data.shadow_maximum_resolution = max(
            float(light_obj.data.shadow_maximum_resolution),
            _BLENDER_KEY_SHADOW_RESOLUTION,
        )
    # SUN translation is irrelevant. Keeping it under the moving sky anchor
    # needlessly reevaluates the light and every lit object on camera movement.
    if light_obj.parent is not None:
        light_obj.parent = None
    light_obj.location = (0.0, 0.0, 0.0)
    return light_obj


def _ensure_ambient_lights(collection, anchor):
    found = _find_role_objects(collection, _ROLE_AMBIENT_LIGHT)
    if len(found) != len(_AMBIENT_FILL_DIRECTIONS) or any(
        getattr(obj, "type", "") != "LIGHT" for obj in found
    ):
        for obj in found:
            _remove_object(obj)
        found = []
    while len(found) < len(_AMBIENT_FILL_DIRECTIONS):
        index = len(found) + 1
        light_data = bpy.data.lights.new(
            f"{ENVIRONMENT_AMBIENT_LIGHT_NAME} {index}",
            type="SUN",
        )
        light_obj = bpy.data.objects.new(
            f"{ENVIRONMENT_AMBIENT_LIGHT_NAME} {index}",
            light_data,
        )
        _tag(light_obj, _ROLE_AMBIENT_LIGHT)
        collection.objects.link(light_obj)
        found.append(light_obj)
    for light_obj in found:
        if light_obj.data.type != "SUN":
            light_obj.data.type = "SUN"
        if light_obj.parent is not None:
            light_obj.parent = None
        light_obj.location = (0.0, 0.0, 0.0)
    return found


def _camera_light_field(spec: Any, name: str, default: Any) -> Any:
    if isinstance(spec, Mapping):
        return spec.get(name, default)
    return getattr(spec, name, default)


def _ensure_camera_lights(collection, anchor, specs: Sequence[Any]):
    specs = tuple(specs or ())
    found = sorted(
        _find_role_objects(collection, _ROLE_CAMERA_LIGHT),
        key=lambda obj: int(obj.get(_CAMERA_LIGHT_INDEX_PROP, 0)),
    )
    while len(found) > len(specs):
        _remove_object(found.pop())
    while len(found) < len(specs):
        index = len(found)
        name = f"{ENVIRONMENT_CAMERA_LIGHT_NAME} {index + 1}"
        light_data = bpy.data.lights.new(name, type="POINT")
        light_obj = bpy.data.objects.new(name, light_data)
        _tag(light_obj, _ROLE_CAMERA_LIGHT)
        collection.objects.link(light_obj)
        found.append(light_obj)

    for index, (light_obj, spec) in enumerate(zip(found, specs)):
        if getattr(light_obj, "type", "") != "LIGHT":
            _remove_object(light_obj)
            name = f"{ENVIRONMENT_CAMERA_LIGHT_NAME} {index + 1}"
            light_data = bpy.data.lights.new(name, type="POINT")
            light_obj = bpy.data.objects.new(name, light_data)
            _tag(light_obj, _ROLE_CAMERA_LIGHT)
            collection.objects.link(light_obj)
            found[index] = light_obj
        light_obj.data.type = "POINT"
        light_obj[_CAMERA_LIGHT_INDEX_PROP] = index
        light_obj[_CAMERA_LIGHT_FRONT_PROP] = float(
            _camera_light_field(spec, "offset_front", 0.0)
        )
        light_obj[_CAMERA_LIGHT_RIGHT_PROP] = float(
            _camera_light_field(spec, "offset_right", 0.0)
        )
        light_obj[_CAMERA_LIGHT_UP_PROP] = float(_camera_light_field(spec, "offset_up", 0.0))
        attenuation = max(
            0.0,
            min(1.0, float(_camera_light_field(spec, "attenuation", 0.5))),
        )
        radius = max(0.001, float(_camera_light_field(spec, "radius", 10.0)))
        light_obj[_CAMERA_LIGHT_ATTENUATION_PROP] = attenuation
        if light_obj.parent != anchor:
            light_obj.parent = anchor
        light_obj.matrix_parent_inverse.identity()
        light_obj.data.color = _rgb(_camera_light_field(spec, "color", (1.0, 1.0, 1.0)))
        light_obj.data.energy = max(0.0, float(_camera_light_field(spec, "energy", 0.0)))
        light_obj.data.use_shadow = False
        if hasattr(light_obj.data, "use_custom_distance"):
            light_obj.data.use_custom_distance = True
        if hasattr(light_obj.data, "cutoff_distance"):
            light_obj.data.cutoff_distance = radius
        if hasattr(light_obj.data, "shadow_soft_size"):
            light_obj.data.shadow_soft_size = 0.05
    return found


def _remove_object(obj) -> None:
    data = getattr(obj, "data", None)
    data_type = getattr(obj, "type", "")
    materials = list(getattr(data, "materials", ()) or ()) if data_type == "MESH" else []
    try:
        bpy.data.objects.remove(obj, do_unlink=True)
    except Exception:
        return
    if data is None or getattr(data, "users", 1):
        return
    try:
        if data_type == "MESH":
            bpy.data.meshes.remove(data)
        elif data_type == "LIGHT":
            bpy.data.lights.remove(data)
    except Exception:
        pass
    for material in materials:
        if material is not None and not getattr(material, "users", 1) and _is_managed(material):
            try:
                bpy.data.materials.remove(material)
            except Exception:
                pass


def _remove_role_objects(collection, role: str) -> None:
    for obj in list(_find_role_objects(collection, role)):
        _remove_object(obj)


def _move_to_collection(obj, collection) -> None:
    if obj.name not in collection.objects:
        collection.objects.link(obj)
    for owner in list(getattr(obj, "users_collection", ())):
        if owner != collection:
            try:
                owner.objects.unlink(obj)
            except Exception:
                pass


def _reset_geometry_local_transforms(root, geometry: Sequence[Any]) -> None:
    """Pin origin-centered celestial meshes to their positioning root."""

    managed = {id(obj) for obj in geometry}
    for obj in geometry:
        parent = obj.parent
        if parent is not None and id(parent) in managed:
            continue
        if parent is not root:
            obj.parent = root
        obj.matrix_parent_inverse.identity()
        obj.location = (0.0, 0.0, 0.0)
        obj.rotation_mode = "XYZ"
        obj.rotation_euler = (0.0, 0.0, 0.0)
        obj.scale = (1.0, 1.0, 1.0)


def _make_material_additive(material) -> None:
    """Use additive blending so black celestial pixels remain invisible."""

    for attr, value in (
        ("surface_render_method", "BLENDED"),
        ("blend_method", "BLEND"),
        ("shadow_method", "NONE"),
        # Cull the far hemisphere so additive blending does not turn a crescent
        # into a ring.
        ("use_backface_culling", True),
    ):
        try:
            setattr(material, attr, value)
        except Exception:
            pass


def _make_material_alpha_blended(material) -> None:
    for attr, value in (
        ("surface_render_method", "BLENDED"),
        ("blend_method", "BLEND"),
        ("shadow_method", "NONE"),
        ("use_backface_culling", False),
    ):
        try:
            setattr(material, attr, value)
        except Exception:
            pass


def _link_additive_output(nodes, links, emission, output, prefix: str) -> None:
    transparent = nodes.new("ShaderNodeBsdfTransparent")
    transparent.name = f"{prefix} Transparent"
    transparent.location = (emission.location[0], emission.location[1] - 160)
    add_shader = nodes.new("ShaderNodeAddShader")
    add_shader.name = f"{prefix} Additive"
    add_shader.location = (
        (emission.location[0] + output.location[0]) / 2.0,
        emission.location[1],
    )
    links.new(emission.outputs[0], add_shader.inputs[0])
    links.new(transparent.outputs[0], add_shader.inputs[1])
    links.new(add_shader.outputs[0], output.inputs["Surface"])


def _create_emission_material(role: str, color: Sequence[float]):
    name = f"W3 Environment {role.title()} Fallback"
    # Fallback bodies are per-preview data; sharing would couple colors between
    # scenes that happen to use the same role.
    material = bpy.data.materials.new(name)
    _tag(material, f"{role}_fallback_material")
    rgba = _rgba(color)
    material.diffuse_color = rgba
    material.use_nodes = True
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    nodes.clear()
    output = nodes.new("ShaderNodeOutputMaterial")
    emission = nodes.new("ShaderNodeEmission")
    emission.inputs["Color"].default_value = rgba
    emission.inputs["Strength"].default_value = 2.0
    _link_additive_output(nodes, links, emission, output, "W3 Fallback")
    _make_material_additive(material)
    return material


def _preview_owner(objects: Iterable[Any]) -> str:
    for obj in objects:
        for collection in getattr(obj, "users_collection", ()):
            if not _is_managed(collection):
                continue
            owner = str(collection.get(_PREVIEW_OWNER_PROP, "") or "")
            if not owner:
                owner = collection.name
                collection[_PREVIEW_OWNER_PROP] = owner
            return owner
    return ""


def _managed_preview_material(name: str, role: str, owner: str):
    material = _find_managed_material(role, owner)
    if material is not None:
        return material
    material = bpy.data.materials.get(name)
    if material is not None and (
        not _is_managed(material) or str(material.get(_MATERIAL_ROLE_PROP, "")) != role
        or str(material.get(_MATERIAL_OWNER_PROP, "") or "") != owner
    ):
        material = None
    material = material or bpy.data.materials.new(name)
    material[_MANAGED_PROP] = True
    material[_MATERIAL_ROLE_PROP] = role
    material[_MATERIAL_OWNER_PROP] = owner
    material.use_nodes = True
    material.use_backface_culling = False
    return material


def _find_managed_material(role: str, owner: str = ""):
    for material in bpy.data.materials:
        try:
            if bool(material.get(_MANAGED_PROP, False)) and str(
                material.get(_MATERIAL_ROLE_PROP, "")
            ) == role and (
                not owner or str(material.get(_MATERIAL_OWNER_PROP, "") or "") == owner
            ):
                return material
        except Exception:
            continue
    return None


def _assign_preview_material(objects: Iterable[Any], material) -> None:
    for obj in objects:
        if getattr(obj, "type", "") != "MESH":
            continue
        previous = list(obj.data.materials)
        obj.data.materials.clear()
        obj.data.materials.append(material)
        for old_material in previous:
            if (
                old_material is not None
                and old_material != material
                and old_material.users == 0
                and _is_managed(old_material)
            ):
                try:
                    bpy.data.materials.remove(old_material)
                except Exception:
                    pass


def _preview_tint(color: Sequence[float]) -> tuple[float, float, float, float]:
    """Keep chroma while letting explicit emission controls set exposure."""

    rgb = _rgb(color)
    peak = max(rgb)
    if peak > 1.0:
        rgb = tuple(channel / peak for channel in rgb)
    return (rgb[0], rgb[1], rgb[2], 1.0)


def _preview_emission_strength(color: Sequence[float], strength: float) -> float:
    """Preserve ColorScaled HDR intensity after normalizing emission chroma."""

    return max(0.0, float(strength)) * max(1.0, max(_rgb(color)))


def _ensure_flat_preview_material(
    objects: Sequence[Any],
    role: str,
    color: Sequence[float],
    strength: float = 1.0,
):
    owner = _preview_owner(objects)
    material = _managed_preview_material(
        f"W3 Environment {role.title()} Flat Preview",
        f"{role}_flat",
        owner,
    )
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    if nodes.get("W3 Flat Additive") is None:
        nodes.clear()
        emission = nodes.new("ShaderNodeEmission")
        emission.name = "W3 Flat Emission"
        output = nodes.new("ShaderNodeOutputMaterial")
        output.location = (400, 0)
        _link_additive_output(nodes, links, emission, output, "W3 Flat")
    _make_material_additive(material)
    tint = _preview_tint(color)
    nodes["W3 Flat Emission"].inputs[0].default_value = tint
    nodes["W3 Flat Emission"].inputs[1].default_value = _preview_emission_strength(color, strength)
    material.diffuse_color = tint
    _assign_preview_material(objects, material)
    return material


def _ensure_sun_preview_material(objects: Sequence[Any], color, strength: float = 5.0):
    owner = _preview_owner(objects)
    material = _managed_preview_material(ENVIRONMENT_SUN_MATERIAL_NAME, "sun", owner)
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    if nodes.get("W3 Sun Additive") is None:
        nodes.clear()
        layer = nodes.new("ShaderNodeLayerWeight")
        layer.name = "W3 Sun Facing"
        layer.location = (-520, 20)

        ramp = nodes.new("ShaderNodeValToRGB")
        ramp.name = "W3 Sun Limb"
        ramp.location = (-300, 20)
        ramp.color_ramp.interpolation = "EASE"
        ramp.color_ramp.elements[0].position = 0.0
        ramp.color_ramp.elements[0].color = (1.0, 1.0, 0.75, 1.0)
        ramp.color_ramp.elements[1].position = 0.72
        ramp.color_ramp.elements[1].color = (1.0, 0.08, 0.0, 1.0)

        tint = nodes.new("ShaderNodeMixRGB")
        tint.name = "W3 Sun Color"
        tint.location = (-70, 20)
        tint.blend_type = "MULTIPLY"
        tint.inputs[0].default_value = 1.0

        emission = nodes.new("ShaderNodeEmission")
        emission.name = "W3 Sun Emission"
        emission.location = (150, 20)

        output = nodes.new("ShaderNodeOutputMaterial")
        output.location = (560, 20)

        links.new(layer.outputs["Facing"], ramp.inputs[0])
        links.new(ramp.outputs[0], tint.inputs[1])
        links.new(tint.outputs[0], emission.inputs[0])
        _link_additive_output(nodes, links, emission, output, "W3 Sun")
    _make_material_additive(material)

    ramp = nodes.get("W3 Sun Limb")
    if ramp is not None:
        ramp.color_ramp.elements[0].color = (1.0, 1.0, 0.75, 1.0)
        ramp.color_ramp.elements[1].color = (1.0, 0.08, 0.0, 1.0)
    tint = nodes.get("W3 Sun Color")
    if tint is not None:
        tint.inputs[2].default_value = _preview_tint(color)
    emission = nodes.get("W3 Sun Emission")
    if emission is not None:
        emission.inputs[1].default_value = _preview_emission_strength(color, strength)
    material.diffuse_color = _preview_tint(color)
    _assign_preview_material(objects, material)
    return material


def _ensure_moon_preview_material(
    objects: Sequence[Any],
    color,
    detail_image=None,
    detail_path: str = "",
    detail_source_path: str = "",
    strength: float = 1.0,
    phase_light: Sequence[float] = (0.0, 0.0, 1.0),
    uv_scroll: float = 0.0,
):
    owner = _preview_owner(objects)
    material = _managed_preview_material(ENVIRONMENT_MOON_MATERIAL_NAME, "moon", owner)
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    if nodes.get("W3 Moon Normal Map") is None:
        nodes.clear()
        texcoord = nodes.new("ShaderNodeTexCoord")
        texcoord.location = (-1140, 80)

        # Drift the lunar face by one UV wrap per day.
        mapping = nodes.new("ShaderNodeMapping")
        mapping.name = "W3 Moon Mapping"
        mapping.label = "Day-Hour Scroll"
        mapping.location = (-950, 150)

        image_node = nodes.new("ShaderNodeTexImage")
        image_node.name = "W3 Moon Detail"
        image_node.label = "Moon Normal Map"
        image_node.location = (-760, 150)
        image_node.interpolation = "Linear"
        image_node.extension = "REPEAT"

        noise = nodes.new("ShaderNodeTexNoise")
        noise.name = "W3 Moon Detail Fallback"
        noise.location = (-760, -120)
        noise.inputs["Scale"].default_value = 8.0
        noise.inputs["Detail"].default_value = 6.0
        roughness = noise.inputs.get("Roughness")
        if roughness is not None:
            roughness.default_value = 0.7

        # A perturbed surface normal and rotating light vector form the crescent.
        normal_map = nodes.new("ShaderNodeNormalMap")
        normal_map.name = "W3 Moon Normal Map"
        normal_map.location = (-500, -120)

        phase_light_node = nodes.new("ShaderNodeCombineXYZ")
        phase_light_node.name = "W3 Moon Phase Light"
        phase_light_node.label = "Phase Light Direction"
        phase_light_node.location = (-500, -300)
        phase_light_node.inputs[2].default_value = 1.0

        phase_dot = nodes.new("ShaderNodeVectorMath")
        phase_dot.name = "W3 Moon Phase Dot"
        phase_dot.operation = "DOT_PRODUCT"
        phase_dot.location = (-310, -180)

        phase = nodes.new("ShaderNodeMath")
        phase.name = "W3 Moon Phase"
        phase.operation = "MAXIMUM"
        phase.location = (-130, -180)
        phase.inputs[1].default_value = 0.0

        ramp = nodes.new("ShaderNodeValToRGB")
        ramp.name = "W3 Moon Surface"
        ramp.location = (-310, 80)
        ramp.color_ramp.elements[0].position = 0.28
        ramp.color_ramp.elements[0].color = (0.01, 0.015, 0.025, 1.0)
        ramp.color_ramp.elements[1].position = 0.95
        ramp.color_ramp.elements[1].color = (1.0, 1.0, 1.0, 1.0)

        tint = nodes.new("ShaderNodeMixRGB")
        tint.name = "W3 Moon Color"
        tint.location = (-70, 80)
        tint.blend_type = "MULTIPLY"
        tint.inputs[0].default_value = 1.0

        phase_mix = nodes.new("ShaderNodeMixRGB")
        phase_mix.name = "W3 Moon Phase Mix"
        phase_mix.label = "Phase Shading"
        phase_mix.location = (100, 80)
        phase_mix.blend_type = "MULTIPLY"
        phase_mix.inputs[0].default_value = 1.0

        emission = nodes.new("ShaderNodeEmission")
        emission.name = "W3 Moon Emission"
        emission.location = (280, 80)

        output = nodes.new("ShaderNodeOutputMaterial")
        output.location = (690, 80)

        links.new(texcoord.outputs["UV"], mapping.inputs[0])
        links.new(mapping.outputs[0], image_node.inputs[0])
        links.new(texcoord.outputs["Generated"], noise.inputs[0])
        links.new(noise.outputs["Fac"], ramp.inputs[0])
        links.new(image_node.outputs["Color"], normal_map.inputs["Color"])
        links.new(normal_map.outputs["Normal"], phase_dot.inputs[0])
        links.new(phase_light_node.outputs[0], phase_dot.inputs[1])
        links.new(phase_dot.outputs["Value"], phase.inputs[0])
        links.new(ramp.outputs[0], tint.inputs[1])
        links.new(tint.outputs[0], phase_mix.inputs[1])
        links.new(phase.outputs[0], phase_mix.inputs[2])
        links.new(phase_mix.outputs[0], emission.inputs[0])
        _link_additive_output(nodes, links, emission, output, "W3 Moon")
    _make_material_additive(material)

    image_node = nodes.get("W3 Moon Detail")
    ramp = nodes.get("W3 Moon Surface")
    if image_node is not None and ramp is not None:
        image_node.image = detail_image
        ramp.color_ramp.elements[0].position = 0.28
        ramp.color_ramp.elements[0].color = (0.01, 0.015, 0.025, 1.0)
        ramp.color_ramp.elements[1].position = 0.95
        ramp.color_ramp.elements[1].color = (1.0, 1.0, 1.0, 1.0)
        for link in list(ramp.inputs[0].links):
            links.remove(link)
        source = image_node.outputs["Alpha"] if detail_image is not None else nodes["W3 Moon Detail Fallback"].outputs["Fac"]
        links.new(source, ramp.inputs[0])

    mapping = nodes.get("W3 Moon Mapping")
    if mapping is not None:
        mapping.inputs["Location"].default_value = (float(uv_scroll), 0.0, 0.0)
    phase_light_node = nodes.get("W3 Moon Phase Light")
    if phase_light_node is not None:
        light = _vector3(phase_light, (0.0, 0.0, 1.0))
        for index in range(3):
            phase_light_node.inputs[index].default_value = float(light[index])
    phase_mix = nodes.get("W3 Moon Phase Mix")
    if phase_mix is not None:
        # Without moon_n there is no normal data; skip the phase term instead
        # of dotting the light against a constant black texture.
        phase_mix.inputs[0].default_value = 1.0 if detail_image is not None else 0.0

    tint = nodes.get("W3 Moon Color")
    if tint is not None:
        tint.inputs[2].default_value = _preview_tint(color)
    emission = nodes.get("W3 Moon Emission")
    if emission is not None:
        emission.inputs[1].default_value = (
            _preview_emission_strength(color, strength) * _BLENDER_MOON_EMISSION_SCALE
        )
    material.diffuse_color = _preview_tint(color)
    material[_MOON_TEXTURE_PROP] = _normalise_depot_path(detail_path)
    material[_MOON_TEXTURE_SOURCE_PROP] = _source_key(detail_source_path)
    _assign_preview_material(objects, material)
    return material


def _ensure_cloud_preview_material(
    objects: Sequence[Any],
    *,
    detail_image=None,
    coverage_image=None,
    amount: float = 1.0,
    strength: float = 1.0,
    day_factor: float = 0.0,
    time_seconds: float = 0.0,
):
    owner = _preview_owner(objects)
    material = _managed_preview_material(ENVIRONMENT_CLOUD_MATERIAL_NAME, "cloud", owner)
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    # Rebuild the earlier approximation as well as creating a fresh graph.  The
    # native material uses coverage.r (not luminance) and combines it with the
    # detail texture additively before the alpha cutoff.
    if (
        nodes.get("W3 Cloud Output") is None
        or nodes.get("W3 Cloud Coverage Channels") is None
        or nodes.get("W3 Cloud Detail Plus Coverage") is None
    ):
        nodes.clear()
        texcoord = nodes.new("ShaderNodeTexCoord")
        texcoord.name = "W3 Cloud UV"
        texcoord.location = (-1050, 80)

        detail_mapping = nodes.new("ShaderNodeMapping")
        detail_mapping.name = "W3 Cloud Detail Mapping"
        detail_mapping.location = (-860, 180)
        detail_mapping.inputs["Scale"].default_value = (3.0, 3.0, 1.0)
        detail = nodes.new("ShaderNodeTexImage")
        detail.name = "W3 Cloud Detail"
        detail.label = "stratocumulus_cover_normal"
        detail.location = (-650, 180)
        detail.extension = "REPEAT"

        coverage_mapping = nodes.new("ShaderNodeMapping")
        coverage_mapping.name = "W3 Cloud Coverage Mapping"
        coverage_mapping.location = (-860, -120)
        coverage = nodes.new("ShaderNodeTexImage")
        coverage.name = "W3 Cloud Coverage"
        coverage.label = "cloud_cover_tiled"
        coverage.location = (-650, -120)
        coverage.extension = "REPEAT"
        coverage_channels = nodes.new("ShaderNodeSeparateColor")
        coverage_channels.name = "W3 Cloud Coverage Channels"
        coverage_channels.location = (-430, -100)
        coverage_ramp = nodes.new("ShaderNodeValToRGB")
        coverage_ramp.name = "W3 Cloud Coverage Shape"
        coverage_ramp.location = (-230, -100)
        # cloud_layer_thin.w2mg evaluates
        # saturate(lerp(coverage_A=-1, coverage_B=1, coverage.r)).
        coverage_ramp.color_ramp.interpolation = "LINEAR"
        coverage_ramp.color_ramp.elements[0].position = 0.5
        coverage_ramp.color_ramp.elements[1].position = 1.0

        detail_plus_coverage = nodes.new("ShaderNodeMath")
        detail_plus_coverage.name = "W3 Cloud Detail Plus Coverage"
        detail_plus_coverage.operation = "ADD"
        detail_plus_coverage.location = (-20, 120)
        mask = nodes.new("ShaderNodeMath")
        mask.name = "W3 Cloud Texture Mask"
        mask.operation = "SUBTRACT"
        mask.use_clamp = True
        mask.location = (160, 120)
        mask.inputs[1].default_value = 1.0
        opacity = nodes.new("ShaderNodeMath")
        opacity.name = "W3 Cloud Effect Amount"
        opacity.operation = "MULTIPLY"
        opacity.location = (340, 120)
        opacity.inputs[1].default_value = 1.0

        tone = nodes.new("ShaderNodeValToRGB")
        tone.name = "W3 Cloud Tone"
        tone.location = (-20, 330)
        emission = nodes.new("ShaderNodeEmission")
        emission.name = "W3 Cloud Emission"
        emission.location = (360, 330)
        emission.inputs[1].default_value = 1.0
        transparent = nodes.new("ShaderNodeBsdfTransparent")
        transparent.name = "W3 Cloud Transparent"
        transparent.location = (360, -50)
        mix = nodes.new("ShaderNodeMixShader")
        mix.name = "W3 Cloud Alpha"
        mix.location = (550, 180)
        output = nodes.new("ShaderNodeOutputMaterial")
        output.name = "W3 Cloud Output"
        output.location = (760, 180)

        links.new(texcoord.outputs["UV"], detail_mapping.inputs[0])
        links.new(detail_mapping.outputs[0], detail.inputs[0])
        links.new(texcoord.outputs["UV"], coverage_mapping.inputs[0])
        links.new(coverage_mapping.outputs[0], coverage.inputs[0])
        links.new(coverage.outputs["Color"], coverage_channels.inputs["Color"])
        links.new(coverage_channels.outputs["Red"], coverage_ramp.inputs[0])
        # cloud_layer_thin.w2mg:
        # alpha = saturate(sunFacingAlpha * (detail.a + cov - 1)).
        # The preview has no native per-pixel sun-facing term, so it uses 1.0.
        links.new(detail.outputs["Alpha"], detail_plus_coverage.inputs[0])
        links.new(coverage_ramp.outputs[0], detail_plus_coverage.inputs[1])
        links.new(detail_plus_coverage.outputs[0], mask.inputs[0])
        links.new(mask.outputs[0], opacity.inputs[0])
        links.new(detail.outputs["Alpha"], tone.inputs[0])
        links.new(tone.outputs[0], emission.inputs[0])
        links.new(opacity.outputs[0], mix.inputs[0])
        links.new(transparent.outputs[0], mix.inputs[1])
        links.new(emission.outputs[0], mix.inputs[2])
        links.new(mix.outputs[0], output.inputs["Surface"])

    _make_material_alpha_blended(material)
    nodes["W3 Cloud Detail"].image = detail_image
    nodes["W3 Cloud Coverage"].image = coverage_image
    # Refresh older managed materials too.  The previous approximation used a
    # much wider 0.34..0.78 ramp, making WT_Clear look overcast.
    coverage_shape = nodes["W3 Cloud Coverage Shape"].color_ramp
    coverage_shape.interpolation = "LINEAR"
    coverage_shape.elements[0].position = 0.5
    coverage_shape.elements[0].color = (0.0, 0.0, 0.0, 1.0)
    coverage_shape.elements[1].position = 1.0
    coverage_shape.elements[1].color = (1.0, 1.0, 1.0, 1.0)
    seconds = float(time_seconds) % 86400.0
    nodes["W3 Cloud Detail Mapping"].inputs["Location"].default_value = (
        (seconds * 0.001) % 1.0,
        0.0,
        0.0,
    )
    nodes["W3 Cloud Coverage Mapping"].inputs["Location"].default_value = (
        (seconds * -0.0005) % 1.0,
        0.0,
        0.0,
    )
    opacity = max(0.0, min(1.0, float(amount) * float(strength)))
    if detail_image is None or coverage_image is None:
        opacity = 0.0
    nodes["W3 Cloud Effect Amount"].inputs[1].default_value = opacity

    # Noon cloud_layer_thin ColorScaled inputs are warm HDR whites (FX_Sky and
    # Custom0), not the dark blue invented by the old preview.  Reinhard
    # compression keeps that authored chroma in Blender's emission range while
    # the detail alpha supplies a restrained tonal variation.
    fx_sky = (1.4604, 1.4208, 1.3182)
    custom0 = (1.5196, 1.4158, 1.2701)
    dark = tuple(channel / (1.0 + channel) for channel in fx_sky) + (1.0,)
    bright = tuple(channel / (1.0 + channel) for channel in custom0) + (1.0,)
    tone = nodes["W3 Cloud Tone"].color_ramp
    tone.elements[0].position = 0.18
    tone.elements[0].color = dark
    tone.elements[1].position = 0.90
    tone.elements[1].color = bright
    material.diffuse_color = bright
    _assign_preview_material(objects, material)
    return material


def _fog_volume_color(color: Sequence[float]) -> tuple[float, float, float, float]:
    """Compress HDR fog with one scale so its hue is preserved."""

    rgb = _rgb(color, (0.08, 0.30, 0.42))
    scale = 1.0 / (1.0 + max(rgb))
    compressed = tuple(channel * scale for channel in rgb)
    return (compressed[0], compressed[1], compressed[2], 1.0)


def _create_fog_box(collection, anchor):
    vertices = (
        (-1.0, -1.0, -1.0),
        (1.0, -1.0, -1.0),
        (1.0, 1.0, -1.0),
        (-1.0, 1.0, -1.0),
        (-1.0, -1.0, 1.0),
        (1.0, -1.0, 1.0),
        (1.0, 1.0, 1.0),
        (-1.0, 1.0, 1.0),
    )
    faces = (
        (0, 3, 2, 1),
        (4, 5, 6, 7),
        (0, 1, 5, 4),
        (1, 2, 6, 5),
        (2, 3, 7, 6),
        (3, 0, 4, 7),
    )
    mesh = bpy.data.meshes.new(ENVIRONMENT_FOG_NAME)
    mesh.from_pydata(vertices, (), faces)
    mesh.update()
    obj = bpy.data.objects.new(ENVIRONMENT_FOG_NAME, mesh)
    _tag(obj, _ROLE_FOG_VOLUME)
    collection.objects.link(obj)
    obj.parent = anchor
    obj.matrix_parent_inverse.identity()
    obj.location = (0.0, 0.0, 0.0)
    obj.rotation_mode = "XYZ"
    obj.rotation_euler = (0.0, 0.0, 0.0)
    obj.scale = (
        _BLENDER_FOG_HALF_EXTENT,
        _BLENDER_FOG_HALF_EXTENT,
        _BLENDER_FOG_HALF_HEIGHT,
    )
    obj.display_type = "BOUNDS"
    obj.hide_select = True
    _disable_shadow_casting(obj)
    return obj


def _ensure_fog_volumetric_end(scene) -> None:
    """Extend Eevee's froxel range while remembering the user's prior value."""

    eevee = getattr(scene, "eevee", None)
    if eevee is None or not hasattr(eevee, "volumetric_end"):
        return
    current = float(eevee.volumetric_end)
    active = scene.get(_SCENE_VOLUMETRIC_END_ACTIVE_PROP)
    if active is None:
        scene[_SCENE_PREVIOUS_VOLUMETRIC_END_PROP] = current
    elif abs(current - float(active)) > 1.0e-4:
        # The user changed the value while Preview was active. Preserve that as
        # the setting to restore, then re-extend it for the managed fog volume.
        scene[_SCENE_PREVIOUS_VOLUMETRIC_END_PROP] = current
    target = max(current, _BLENDER_FOG_HALF_EXTENT)
    eevee.volumetric_end = target
    scene[_SCENE_VOLUMETRIC_END_ACTIVE_PROP] = target


def _restore_fog_volumetric_end(scene) -> bool:
    if _SCENE_VOLUMETRIC_END_ACTIVE_PROP not in scene:
        return False
    eevee = getattr(scene, "eevee", None)
    active = float(scene.get(_SCENE_VOLUMETRIC_END_ACTIVE_PROP, 0.0))
    previous = float(scene.get(_SCENE_PREVIOUS_VOLUMETRIC_END_PROP, active))
    restored = False
    if eevee is not None and hasattr(eevee, "volumetric_end"):
        current = float(eevee.volumetric_end)
        # Do not overwrite a setting the user changed after the last refresh.
        if abs(current - active) <= 1.0e-4:
            eevee.volumetric_end = previous
            restored = True
    for prop_name in (
        _SCENE_PREVIOUS_VOLUMETRIC_END_PROP,
        _SCENE_VOLUMETRIC_END_ACTIVE_PROP,
    ):
        if prop_name in scene:
            del scene[prop_name]
    return restored


def _ensure_view_exposure(scene, exposure_ev: float) -> None:
    """Apply one additive tone offset without compounding refreshes.

    The previous value is kept separately from the last managed result. If the
    current value no longer matches that result, the user edited Exposure while
    Preview was active; treat that edit as the new base for subsequent updates.
    """

    view_settings = getattr(scene, "view_settings", None)
    if view_settings is None or not hasattr(view_settings, "exposure"):
        return
    offset = float(exposure_ev)
    if not math.isfinite(offset):
        offset = 0.0
    current = float(view_settings.exposure)
    active = scene.get(_SCENE_VIEW_EXPOSURE_ACTIVE_PROP)
    if active is None:
        previous = current
    elif abs(current - float(active)) > 1.0e-5:
        # Preserve a user edit made after the last preview refresh. The managed
        # The managed offset remains additive to the new scene exposure.
        previous = current
    else:
        previous = float(scene.get(_SCENE_PREVIOUS_VIEW_EXPOSURE_PROP, current))
    view_settings.exposure = previous + offset
    scene[_SCENE_PREVIOUS_VIEW_EXPOSURE_PROP] = previous
    scene[_SCENE_VIEW_EXPOSURE_ACTIVE_PROP] = float(view_settings.exposure)


def _restore_view_exposure(scene) -> bool:
    if _SCENE_VIEW_EXPOSURE_ACTIVE_PROP not in scene:
        return False
    view_settings = getattr(scene, "view_settings", None)
    active = float(scene.get(_SCENE_VIEW_EXPOSURE_ACTIVE_PROP, 0.0))
    previous = float(scene.get(_SCENE_PREVIOUS_VIEW_EXPOSURE_PROP, active))
    restored = False
    if view_settings is not None and hasattr(view_settings, "exposure"):
        current = float(view_settings.exposure)
        # A user edit after the last refresh wins over the managed restore.
        if abs(current - active) <= 1.0e-5:
            view_settings.exposure = previous
            restored = True
    for prop_name in (
        _SCENE_PREVIOUS_VIEW_EXPOSURE_PROP,
        _SCENE_VIEW_EXPOSURE_ACTIVE_PROP,
    ):
        if prop_name in scene:
            del scene[prop_name]
    return restored


def _update_balance_preview(
    context,
    *,
    source_path: str,
    balance_map_path: str,
    amount: float,
    brightness: float,
    exposure_ev: float,
    tone_curve_parameters,
    tone_post_scale: float,
    warnings: list[str],
) -> None:
    from .environment_balance_preview import apply_balance_preview, clear_balance_preview

    scene = context.scene
    # The LUT compositor owns exposure while active. Restore the user's base
    # value before its first snapshot so repeated refreshes never compound it.
    _restore_view_exposure(scene)
    requested = _normalise_depot_path(balance_map_path)
    resolved = ""
    if requested and float(amount) > 0.0:
        resolved = requested if win_path_isfile(requested) else resolve_environment_asset(
            requested,
            source_path,
        )
    if resolved and apply_balance_preview(
        context,
        resolved,
        max(0.0, min(1.0, float(amount))),
        max(0.0, float(brightness)),
        float(exposure_ev),
        tone_curve_parameters,
        max(0.0, float(tone_post_scale)),
    ):
        return

    restored_balance = clear_balance_preview(context)
    _ensure_view_exposure(scene, exposure_ev)
    if requested and float(amount) > 0.0:
        warnings.append(
            f"Balance-map preview failed: {resolved or requested}"
        )


def _ensure_fog_volume(
    scene,
    collection,
    anchor,
    *,
    color: Sequence[float],
    density: float,
    appear_distance: float,
    appear_range: float,
    final_exp: float,
    vert_offset: float,
    vert_density: float,
):
    """Create a bounded Eevee fog approximation.

    A World volume has infinite path length and therefore erases the sky.  The
    large managed box follows the camera while its material retains world-height
    and view-distance shaping.
    """

    density = max(0.0, min(_BLENDER_FOG_MAX_DENSITY, float(density)))
    engine = str(getattr(getattr(scene, "render", None), "engine", "") or "")
    if density <= 1.0e-8 or not engine.startswith("BLENDER_EEVEE"):
        _remove_role_objects(collection, _ROLE_FOG_VOLUME)
        _restore_fog_volumetric_end(scene)
        return None
    _ensure_fog_volumetric_end(scene)

    found = _find_role_objects(collection, _ROLE_FOG_VOLUME)
    obj = found[0] if found else None
    for duplicate in found[1:]:
        _remove_object(duplicate)
    if obj is None or getattr(obj, "type", "") != "MESH":
        if obj is not None:
            _remove_object(obj)
        obj = _create_fog_box(collection, anchor)
    if obj.parent != anchor:
        obj.parent = anchor
    obj.matrix_parent_inverse.identity()
    obj.location = (0.0, 0.0, 0.0)
    obj.rotation_mode = "XYZ"
    obj.rotation_euler = (0.0, 0.0, 0.0)
    obj.scale = (
        _BLENDER_FOG_HALF_EXTENT,
        _BLENDER_FOG_HALF_EXTENT,
        _BLENDER_FOG_HALF_HEIGHT,
    )

    owner = _preview_owner((obj,))
    material = _managed_preview_material(ENVIRONMENT_FOG_MATERIAL_NAME, "fog", owner)
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    if nodes.get("W3 Fog Output") is None or nodes.get("W3 Fog Vertical Density") is None:
        nodes.clear()
        output = nodes.new("ShaderNodeOutputMaterial")
        output.name = "W3 Fog Output"
        output.location = (700, 80)

        scatter = nodes.new("ShaderNodeVolumeScatter")
        scatter.name = "W3 Fog Scatter"
        scatter.location = (480, 80)

        camera = nodes.new("ShaderNodeCameraData")
        camera.name = "W3 Fog Camera Data"
        camera.location = (-900, 260)
        distance_after_start = nodes.new("ShaderNodeMath")
        distance_after_start.name = "W3 Fog Distance After Start"
        distance_after_start.operation = "SUBTRACT"
        distance_after_start.location = (-680, 260)
        distance_range = nodes.new("ShaderNodeMath")
        distance_range.name = "W3 Fog Appear Range"
        distance_range.operation = "DIVIDE"
        distance_range.use_clamp = True
        distance_range.location = (-470, 260)
        distance_power = nodes.new("ShaderNodeMath")
        distance_power.name = "W3 Fog Final Exponent"
        distance_power.operation = "POWER"
        distance_power.use_clamp = True
        distance_power.location = (-250, 260)

        geometry = nodes.new("ShaderNodeNewGeometry")
        geometry.name = "W3 Fog Geometry"
        geometry.location = (-900, -180)
        position = nodes.new("ShaderNodeSeparateXYZ")
        position.name = "W3 Fog World Position"
        position.location = (-680, -180)
        height = nodes.new("ShaderNodeMath")
        height.name = "W3 Fog Height Above Offset"
        height.operation = "SUBTRACT"
        height.location = (-470, -180)
        vertical_slope = nodes.new("ShaderNodeMath")
        vertical_slope.name = "W3 Fog Vertical Slope"
        vertical_slope.operation = "MULTIPLY"
        vertical_slope.location = (-250, -180)
        vertical_positive = nodes.new("ShaderNodeMath")
        vertical_positive.name = "W3 Fog Vertical Positive"
        vertical_positive.operation = "MAXIMUM"
        vertical_positive.location = (-30, -180)
        vertical_positive.inputs[1].default_value = 0.0
        vertical_denominator = nodes.new("ShaderNodeMath")
        vertical_denominator.name = "W3 Fog Vertical Denominator"
        vertical_denominator.operation = "ADD"
        vertical_denominator.location = (170, -180)
        vertical_denominator.inputs[1].default_value = 1.0

        base_density = nodes.new("ShaderNodeValue")
        base_density.name = "W3 Fog Base Density"
        base_density.location = (-30, -20)
        vertical_density = nodes.new("ShaderNodeMath")
        vertical_density.name = "W3 Fog Vertical Density"
        vertical_density.operation = "DIVIDE"
        vertical_density.location = (170, -20)
        final_density = nodes.new("ShaderNodeMath")
        final_density.name = "W3 Fog Final Density"
        final_density.operation = "MULTIPLY"
        final_density.location = (360, 80)

        links.new(camera.outputs["View Distance"], distance_after_start.inputs[0])
        links.new(distance_after_start.outputs[0], distance_range.inputs[0])
        links.new(distance_range.outputs[0], distance_power.inputs[0])
        links.new(geometry.outputs["Position"], position.inputs[0])
        links.new(position.outputs["Z"], height.inputs[0])
        links.new(height.outputs[0], vertical_slope.inputs[0])
        links.new(vertical_slope.outputs[0], vertical_positive.inputs[0])
        links.new(vertical_positive.outputs[0], vertical_denominator.inputs[0])
        links.new(base_density.outputs[0], vertical_density.inputs[0])
        links.new(vertical_denominator.outputs[0], vertical_density.inputs[1])
        links.new(vertical_density.outputs[0], final_density.inputs[0])
        links.new(distance_power.outputs[0], final_density.inputs[1])
        links.new(final_density.outputs[0], scatter.inputs["Density"])
        links.new(scatter.outputs[0], output.inputs["Volume"])

    appear_distance = max(0.0, float(appear_distance))
    appear_range = max(1.0e-3, float(appear_range))
    final_exp = max(0.05, min(4.0, float(final_exp)))
    vert_offset = float(vert_offset)
    vert_density = float(vert_density)
    fog_rgba = _fog_volume_color(color)
    nodes["W3 Fog Scatter"].inputs["Color"].default_value = fog_rgba
    nodes["W3 Fog Distance After Start"].inputs[1].default_value = appear_distance
    nodes["W3 Fog Appear Range"].inputs[1].default_value = appear_range
    nodes["W3 Fog Final Exponent"].inputs[1].default_value = final_exp
    nodes["W3 Fog Height Above Offset"].inputs[1].default_value = vert_offset
    nodes["W3 Fog Vertical Slope"].inputs[1].default_value = -vert_density
    nodes["W3 Fog Base Density"].outputs[0].default_value = density
    material.diffuse_color = fog_rgba
    _assign_preview_material((obj,), material)

    values = {
        "density": density,
        "appear_distance": appear_distance,
        "appear_range": appear_range,
        "final_exp": final_exp,
        "vert_offset": vert_offset,
        "vert_density": vert_density,
    }
    for name, value in values.items():
        obj[f"witcher_environment_fog_{name}"] = value
        material[f"witcher_environment_fog_{name}"] = value
    return obj


def _set_named_node_default(nodes, name: str, value: Any) -> bool:
    node = nodes.get(name)
    if node is None:
        return False
    color_output = node.outputs.get("Color")
    if color_output is not None:
        color_output.default_value = _rgba(value)
        return True
    value_output = node.outputs.get("Value")
    if value_output is not None:
        value_output.default_value = float(value)
        return True
    return False


def _update_world_water_materials(
    *,
    scene,
    color: Sequence[float],
    fresnel: float,
    ambient_scale: float,
    diffuse_scale: float,
    flow_intensity: float,
    foam_intensity: float,
) -> int:
    from .import_w2w import _world_water_materials

    values = {
        "W3 Water Tint": _rgb(color, (0.0, 0.0, 0.0)),
        "W3 Water Fresnel Gain": float(fresnel),
        "W3 Water Ambient Scale": float(ambient_scale),
        "W3 Water Diffuse Scale": float(diffuse_scale),
        "W3 Water Flow": float(flow_intensity),
        "W3 Water Foam": float(foam_intensity),
    }
    updated = 0
    for material in _world_water_materials(scene):
        node_tree = getattr(material, "node_tree", None)
        if not material.use_nodes or node_tree is None:
            continue
        changed = False
        for node_name, value in values.items():
            changed = _set_named_node_default(node_tree.nodes, node_name, value) or changed
        for node_name, value in values.items():
            prop_name = node_name.lower().replace(" ", "_")
            material[prop_name] = tuple(value) if isinstance(value, tuple) else value
        updated += int(changed)
    return updated


def _disable_shadow_casting(obj) -> None:
    # A celestial body sits exactly in the key light direction; an opaque
    # shadow caster there would eclipse the whole scene.
    try:
        obj.visible_shadow = False
    except Exception:
        pass


def _create_fallback_disc(collection, root, role: str, color: Sequence[float]):
    # Witcher applies a scale of 10 * evaluated celestial size. A 0.1 radius
    # disc keeps the default Blender fallback at a practical one-unit radius.
    segments = 32
    radius = 0.1
    vertices = [
        (
            radius * math.cos((2.0 * math.pi * i) / segments),
            radius * math.sin((2.0 * math.pi * i) / segments),
            0.0,
        )
        for i in range(segments)
    ]
    mesh = bpy.data.meshes.new(f"W3 Environment {role.title()} Disc")
    mesh.from_pydata(vertices, [], [tuple(range(segments))])
    mesh.update()
    obj = bpy.data.objects.new(f"W3 Environment {role.title()} Fallback", mesh)
    geometry_role = _ROLE_SUN_GEOMETRY if role == "sun" else _ROLE_MOON_GEOMETRY
    _tag(obj, geometry_role)
    obj[_ASSET_MODE_PROP] = "FALLBACK"
    collection.objects.link(obj)
    obj.parent = root
    obj.data.materials.append(_create_emission_material(role, color))
    _disable_shadow_casting(obj)
    return [obj]


def _extract_imported_objects(result: Any) -> list[Any]:
    if isinstance(result, tuple) and result:
        result = result[0]
    if result is None:
        return []
    if isinstance(result, (list, tuple, set)):
        return [obj for obj in result if hasattr(obj, "name")]
    return [result] if hasattr(result, "name") else []


def _import_asset_mesh(
    context,
    collection,
    root,
    resolved_path: str,
    depot_path: str,
    source_path: str,
    geometry_role: str,
    *,
    import_materials: bool,
) -> list[Any]:
    from . import import_mesh as import_mesh_module

    with _active_collection(context, collection), _preserve_selection(context):
        with redkit_repo_context(source_path or resolved_path):
            result = import_mesh_module.import_mesh(
                resolved_path,
                do_import_mats=bool(import_materials),
                do_import_armature=False,
                keep_lod_meshes=False,
                do_merge_normals=False,
                keep_empty_lods=False,
                keep_proxy_meshes=False,
                do_import_collision=False,
                build_material_nodes=bool(import_materials),
            )
    objects = _extract_imported_objects(result)
    for obj in objects:
        _move_to_collection(obj, collection)
        _tag(obj, geometry_role)
        obj[_ASSET_MODE_PROP] = "GAME"
        obj[_DEPOT_PATH_PROP] = depot_path
        obj[_RESOLVED_PATH_PROP] = resolved_path
        _disable_shadow_casting(obj)
    _reset_geometry_local_transforms(root, objects)
    return objects


def _ensure_material_slot(objects: Iterable[Any], role: str) -> None:
    for obj in objects:
        if getattr(obj, "type", "") != "MESH":
            continue
        if not obj.data.materials:
            obj.data.materials.append(
                bpy.data.materials.new(f"W3 Environment {role.title()} Material")
            )


def _apply_world_material(
    context,
    objects: Sequence[Any],
    material: Any,
    role: str,
    source_path: str,
    warnings: list[str],
) -> None:
    if material is None or not objects:
        return
    if isinstance(material, bpy.types.Material):
        for obj in objects:
            if getattr(obj, "type", "") != "MESH":
                continue
            if obj.data.materials:
                for index in range(len(obj.data.materials)):
                    obj.data.materials[index] = material
            else:
                obj.data.materials.append(material)
        return

    material_chunk = material
    try:
        if isinstance(material, (str, os.PathLike)):
            material_path = _normalise_depot_path(material)
            if not resolve_environment_asset(material_path, source_path):
                warnings.append(f"{role.title()} material was not found: {material_path}")
                return
            from ..materials import reader as material_reader

            with redkit_repo_context(source_path or None):
                material_chunk = material_reader._load_material_root_chunk(
                    material_path,
                    version=999,
                )
        if material_chunk is None:
            warnings.append(f"{role.title()} material could not be decoded")
            return

        from .. import get_texture_path
        from .import_mesh import load_w3_materials_CR2W_Mesh

        _ensure_material_slot(objects, role)
        with redkit_repo_context(source_path or None):
            load_w3_materials_CR2W_Mesh(
                list(objects),
                get_texture_path(context),
                [material_chunk],
                [role.title()],
                force_mat_update=True,
                mat_filename=f"environment_{role}",
                build_material_nodes=True,
            )
    except Exception as exc:
        log.exception("Could not apply the world %s material", role)
        warnings.append(f"{role.title()} material preview failed: {exc}")


def _asset_matches(root, depot_path: str, geometry: Sequence[Any], source_path: str = "") -> bool:
    if not geometry:
        return False
    try:
        current_path = str(root.get(_DEPOT_PATH_PROP, "") or "")
        current_source = str(root.get(_SOURCE_PATH_PROP, "") or "")
    except Exception:
        return False
    return current_path.lower() == depot_path.lower() and current_source == _source_key(source_path)


def _ensure_celestial_geometry(
    context,
    collection,
    root,
    *,
    role: str,
    depot_path: str,
    source_path: str,
    material: Any,
    color: Sequence[float],
    import_materials: bool,
    warnings: list[str],
) -> tuple[list[Any], str, str]:
    geometry_role = _ROLE_SUN_GEOMETRY if role == "sun" else _ROLE_MOON_GEOMETRY
    geometry = _find_role_objects(collection, geometry_role)
    depot_path = _normalise_depot_path(depot_path)
    mode = str(root.get(_ASSET_MODE_PROP, "") or "")
    resolved = str(root.get(_RESOLVED_PATH_PROP, "") or "")

    if _asset_matches(root, depot_path, geometry, source_path) and (mode == "GAME" or not depot_path):
        _reset_geometry_local_transforms(root, geometry)
        if import_materials:
            _apply_world_material(context, geometry, material, role, source_path, warnings)
        return geometry, mode or "FALLBACK", resolved

    _remove_role_objects(collection, geometry_role)
    geometry = []
    resolved = resolve_environment_asset(depot_path, source_path) if depot_path else ""
    if depot_path and not resolved:
        warnings.append(f"{role.title()} mesh was not found: {depot_path}")
    if resolved:
        try:
            geometry = _import_asset_mesh(
                context,
                collection,
                root,
                resolved,
                depot_path,
                source_path,
                geometry_role,
                import_materials=import_materials,
            )
        except Exception as exc:
            log.exception("Could not import environment %s mesh '%s'", role, resolved)
            warnings.append(f"{role.title()} mesh import failed: {exc}")

    if geometry:
        mode = "GAME"
        if import_materials:
            _apply_world_material(context, geometry, material, role, source_path, warnings)
    else:
        geometry = _create_fallback_disc(collection, root, role, color)
        mode = "FALLBACK"
        resolved = ""
        if import_materials:
            _apply_world_material(context, geometry, material, role, source_path, warnings)

    root[_DEPOT_PATH_PROP] = depot_path
    root[_RESOLVED_PATH_PROP] = resolved
    root[_ASSET_MODE_PROP] = mode
    root[_SOURCE_PATH_PROP] = _source_key(source_path)
    return geometry, mode, resolved


def _set_cloud_preview_scale(root, geometry: Sequence[Any], authored_scale: float) -> float:
    local_radius = max(
        (Vector(corner).length for obj in geometry for corner in obj.bound_box),
        default=0.0,
    )
    scale = max(0.001, float(authored_scale))
    if local_radius > 1.0e-8:
        scale = min(scale, _BLENDER_CLOUD_MAX_REACH / local_radius)
    root.scale = (scale,) * 3
    return scale


def _ensure_cloud_layer(
    context,
    collection,
    anchor,
    *,
    asset: CloudLayerAsset | None,
    source_path: str,
    amount: float,
    day_factor: float,
    time_seconds: float,
    warnings: list[str],
) -> tuple[list[Any], str, str]:
    if asset is None:
        _remove_role_objects(collection, _ROLE_CLOUD_GEOMETRY)
        _remove_role_objects(collection, _ROLE_CLOUD_ROOT)
        return [], "NONE", ""

    root = _ensure_cloud_root(collection, anchor)
    geometry = _find_role_objects(collection, _ROLE_CLOUD_GEOMETRY)
    mesh_path = _normalise_depot_path(asset.mesh_path)
    resolved = str(root.get(_RESOLVED_PATH_PROP, "") or "")
    if not _asset_matches(root, mesh_path, geometry, source_path):
        _remove_role_objects(collection, _ROLE_CLOUD_GEOMETRY)
        geometry = []
        resolved = resolve_environment_asset(mesh_path, source_path)
        if not resolved:
            warnings.append(f"Cloud mesh was not found: {mesh_path}")
        else:
            try:
                geometry = _import_asset_mesh(
                    context,
                    collection,
                    root,
                    resolved,
                    mesh_path,
                    source_path,
                    _ROLE_CLOUD_GEOMETRY,
                    import_materials=False,
                )
            except Exception as exc:
                log.exception("Could not import weather cloud mesh '%s'", resolved)
                warnings.append(f"Cloud mesh import failed: {exc}")
    else:
        _reset_geometry_local_transforms(root, geometry)

    if not geometry:
        _remove_role_objects(collection, _ROLE_CLOUD_ROOT)
        return [], "NONE", ""

    _set_cloud_preview_scale(root, geometry, asset.scale)
    root[_DEPOT_PATH_PROP] = mesh_path
    root[_RESOLVED_PATH_PROP] = resolved
    root[_ASSET_MODE_PROP] = "GAME"
    root[_SOURCE_PATH_PROP] = _source_key(source_path)
    root["witcher_environment_effect_path"] = asset.effect_path
    root["witcher_environment_effect_resolved_path"] = asset.effect_resolved_path

    detail_path, coverage_path = resolve_cloud_textures(asset.material_path, source_path)
    owner = _preview_owner(geometry)
    material = _find_managed_material("cloud", owner)
    detail_node = (
        material.node_tree.nodes.get("W3 Cloud Detail")
        if material is not None and material.use_nodes
        else None
    )
    coverage_node = (
        material.node_tree.nodes.get("W3 Cloud Coverage")
        if material is not None and material.use_nodes
        else None
    )

    def existing_image(node, depot_path):
        image = getattr(node, "image", None) if node is not None else None
        current = str(image.get("witcher_environment_source_path", "") or "") if image else ""
        return image if current.lower() == _normalise_depot_path(depot_path).lower() else None

    detail_image = existing_image(detail_node, detail_path)
    coverage_image = existing_image(coverage_node, coverage_path)
    if detail_image is None:
        detail_image = _load_cloud_image(detail_path, source_path, warnings)
    if coverage_image is None:
        coverage_image = _load_cloud_image(coverage_path, source_path, warnings)
    _ensure_cloud_preview_material(
        geometry,
        detail_image=detail_image,
        coverage_image=coverage_image,
        amount=amount,
        strength=asset.strength,
        day_factor=day_factor,
        time_seconds=time_seconds,
    )
    return geometry, "GAME", resolved


def _vector3(value: Any, fallback: Sequence[float]) -> Vector:
    try:
        vector = Vector(value)
        if len(vector) < 3 or vector.length_squared < 1.0e-12:
            raise ValueError
        vector.resize_3d()
        return vector.normalized()
    except Exception:
        return Vector(fallback).normalized()


def _rgb(value: Any, fallback=(1.0, 1.0, 1.0)) -> tuple[float, float, float]:
    try:
        values = tuple(float(channel) for channel in value)
        if len(values) < 3:
            raise ValueError
        return tuple(max(0.0, channel) for channel in values[:3])
    except Exception:
        return tuple(float(channel) for channel in fallback[:3])


def _rgba(value: Any) -> tuple[float, float, float, float]:
    rgb = _rgb(value)
    return (rgb[0], rgb[1], rgb[2], 1.0)


def _set_fallback_color(objects: Iterable[Any], color: Sequence[float]) -> None:
    rgba = _rgba(color)
    for obj in objects:
        try:
            obj.color = rgba
            if str(obj.get(_ASSET_MODE_PROP, "")) != "FALLBACK":
                continue
            for material in obj.data.materials:
                material.diffuse_color = rgba
                nodes = getattr(getattr(material, "node_tree", None), "nodes", None)
                emission = nodes.get("Emission") if nodes is not None else None
                if emission is not None and emission.inputs.get("Color") is not None:
                    emission.inputs["Color"].default_value = rgba
        except Exception:
            pass


def _set_celestial_transform(root, direction, size: float, distance: float) -> None:
    direction = _vector3(direction, (0.0, 0.0, 1.0))
    distance = max(0.001, float(distance))
    size = max(0.0, float(size))
    root.location = direction * distance
    if str(root.get(_ASSET_MODE_PROP, "") or "") == "GAME":
        # Keep spherical assets at identity rotation so their surface stays fixed.
        root.rotation_mode = "XYZ"
        root.rotation_euler = (0.0, 0.0, 0.0)
    else:
        # The procedural fallback is a flat disc and must face the anchor.
        root.rotation_mode = "QUATERNION"
        root.rotation_quaternion = (-direction).to_track_quat("Z", "Y")
    # Scale with distance to preserve angular size when scenery requires a more
    # distant anchor.
    root.scale = (10.0 * size * (distance / CELESTIAL_DISTANCE),) * 3


def _set_key_light(light_obj, direction, color, energy: float) -> None:
    direction = _vector3(direction, (0.0, 0.0, 1.0))
    # Blender SUN rays travel along local -Z. Align local +Z to the evaluated
    # sky direction so the illumination travels back toward the world.
    light_obj.rotation_mode = "QUATERNION"
    light_obj.rotation_quaternion = direction.to_track_quat("Z", "Y")
    light_obj.data.color = _rgb(color)
    light_obj.data.energy = max(0.0, float(energy))
    if hasattr(light_obj.data, "specular_factor"):
        # Limit direct specular so imported terrain does not look wet and bright
        # surfaces retain detail under the night key.
        light_obj.data.specular_factor = 0.25


def _set_ambient_lights(light_objects, color, energy: float) -> None:
    normalized_color = _rgb(color)
    energy = max(0.0, float(energy))
    for light_obj, direction in zip(light_objects, _AMBIENT_FILL_DIRECTIONS):
        vector = _vector3(direction, (0.0, 0.0, 1.0))
        light_obj.rotation_mode = "QUATERNION"
        light_obj.rotation_quaternion = vector.to_track_quat("Z", "Y")
        light_obj.data.color = normalized_color
        light_obj.data.energy = energy
        light_obj.data.use_shadow = False
        if hasattr(light_obj.data, "specular_factor"):
            light_obj.data.specular_factor = 0.0


def _result_from_collection(
    collection,
    warnings=(),
    *,
    sky_world=None,
    stars_resolved_path: str = "",
) -> PreviewResult:
    sun_objects = _find_role_objects(collection, _ROLE_SUN_GEOMETRY)
    moon_objects = _find_role_objects(collection, _ROLE_MOON_GEOMETRY)
    cloud_objects = _find_role_objects(collection, _ROLE_CLOUD_GEOMETRY)
    sun_roots = _find_role_objects(collection, _ROLE_SUN_ROOT)
    moon_roots = _find_role_objects(collection, _ROLE_MOON_ROOT)
    cloud_roots = _find_role_objects(collection, _ROLE_CLOUD_ROOT)
    lights = _find_role_objects(collection, _ROLE_KEY_LIGHT)
    camera_lights = _find_role_objects(collection, _ROLE_CAMERA_LIGHT)
    sun_root = sun_roots[0] if sun_roots else None
    moon_root = moon_roots[0] if moon_roots else None
    cloud_root = cloud_roots[0] if cloud_roots else None
    stars_node = None
    if sky_world is not None and getattr(sky_world, "node_tree", None) is not None:
        stars_node = sky_world.node_tree.nodes.get(_SKY_NODE_STARS)
    stars_loaded = stars_node is not None and getattr(stars_node, "image", None) is not None
    return PreviewResult(
        collection_name=collection.name,
        sun_object_names=tuple(obj.name for obj in sun_objects),
        moon_object_names=tuple(obj.name for obj in moon_objects),
        cloud_object_names=tuple(obj.name for obj in cloud_objects),
        key_light_name=lights[0].name if lights else "",
        camera_light_names=tuple(obj.name for obj in camera_lights),
        sun_resolved_path=str(sun_root.get(_RESOLVED_PATH_PROP, "") or "") if sun_root else "",
        moon_resolved_path=str(moon_root.get(_RESOLVED_PATH_PROP, "") or "") if moon_root else "",
        cloud_resolved_path=(
            str(cloud_root.get(_RESOLVED_PATH_PROP, "") or "") if cloud_root else ""
        ),
        sun_mode=str(sun_root.get(_ASSET_MODE_PROP, "") or "") if sun_root else "",
        moon_mode=str(moon_root.get(_ASSET_MODE_PROP, "") or "") if moon_root else "",
        cloud_mode=str(cloud_root.get(_ASSET_MODE_PROP, "") or "") if cloud_root else "NONE",
        sky_world_name=str(getattr(sky_world, "name", "") or ""),
        stars_resolved_path=str(stars_resolved_path or ""),
        stars_mode="GAME" if stars_loaded else "NONE",
        warnings=tuple(str(message) for message in warnings if message),
    )


def ensure_preview(
    context,
    *,
    source_path: str = "",
    sun_mesh_path: str = "",
    moon_mesh_path: str = "",
    sun_material: Any = None,
    moon_material: Any = None,
    skybox_material_path: str = "",
    moon_material_path: str = "",
    stars_cube_path: str = "",
    weather_effects: Sequence[Any] = (),
    sun_direction: Sequence[float] = (0.0, 0.0, 1.0),
    moon_direction: Sequence[float] = (0.0, 0.0, -1.0),
    key_direction: Sequence[float] | None = None,
    sun_size: float = 1.0,
    moon_size: float = 1.0,
    sun_color: Sequence[float] = (1.0, 0.75, 0.35),
    moon_color: Sequence[float] = (0.55, 0.65, 1.0),
    sun_sky_color: Sequence[float] = (0.55, 0.78, 1.0),
    sun_sky_brightness: float = 1.0,
    sun_area_sky_size: float = 0.33,
    sun_influence: float = 1.0,
    moon_sky_color: Sequence[float] = (0.55, 0.78, 1.0),
    moon_sky_brightness: float = 1.0,
    moon_area_sky_size: float = 0.33,
    moon_influence: float = 0.0,
    key_color: Sequence[float] = (1.0, 1.0, 1.0),
    key_energy: float = 3.0,
    ambient_color: Sequence[float] = (0.61, 0.75, 1.0),
    ambient_energy: float = 0.0,
    camera_lights: Sequence[Any] = (),
    tone_exposure_ev: float = 0.0,
    tone_curve_parameters: Sequence[float] = (0.22, 0.30, 0.10, 0.20, 0.01, 0.30),
    tone_post_scale: float = 1.0,
    balance_map_path: str = "",
    balance_map_amount: float = 0.0,
    balance_post_brightness: float = 1.0,
    sky_zenith_color: Sequence[float] = (0.15, 0.35, 0.8),
    sky_horizon_color: Sequence[float] = (0.8, 0.5, 0.35),
    sun_horizon_color: Sequence[float] = (0.85, 0.9, 1.0),
    sun_back_horizon_color: Sequence[float] = (0.364, 0.307, 0.298),
    sky_brightness: float = 1.0,
    fog_color: Sequence[float] = (0.08, 0.30, 0.42),
    fog_color_front: Sequence[float] | None = None,
    fog_color_middle: Sequence[float] | None = None,
    fog_color_back: Sequence[float] | None = None,
    aerial_color_front: Sequence[float] = (1.0, 1.0, 1.0),
    aerial_color_middle: Sequence[float] = (1.0, 1.0, 1.0),
    aerial_color_back: Sequence[float] = (1.0, 1.0, 1.0),
    fog_direction: Sequence[float] | None = None,
    fog_sky_density: float = 0.0,
    fog_density: float = 0.0,
    fog_dist_clamp: float = 0.0,
    fog_appear_distance: float = 0.0,
    fog_appear_range: float = 0.0,
    fog_final_exp: float = 1.0,
    aerial_final_exp: float = 1.0,
    fog_vert_offset: float = 0.0,
    fog_vert_density: float = 0.0,
    water_color: Sequence[float] = (0.0, 0.0, 0.0),
    water_fresnel: float = 1.0,
    water_ambient_scale: float = 0.1,
    water_diffuse_scale: float = 0.4,
    water_flow_intensity: float = 0.6,
    water_foam_intensity: float = 0.0,
    sky_day_factor: float = 1.0,
    horizon_attenuation: float = 1.8,
    stars_brightness: float = 1.1,
    cloud_amount: float = 0.45,
    sun_brightness: float = 5.0,
    moon_brightness: float = 1.0,
    sky_enabled: bool = True,
    anchor_distance: float = 100.0,
    time_seconds: float | None = None,
    day_number: float = 0.0,
    import_materials: bool = True,
) -> PreviewResult:
    """Create or refresh the managed environment preview."""

    collection = ensure_environment_collection(context)
    collection[_SOURCE_PATH_PROP] = str(source_path or "")
    if time_seconds is not None:
        collection[_TIME_PROP] = float(time_seconds) % 86400.0
    seconds = float(collection.get(_TIME_PROP, 43200.0))
    moon_phase = _moon_phase_angle(day_number, seconds)
    moon_scroll = -seconds / 86400.0

    anchor = _ensure_anchor(context, collection)
    sun_root = _ensure_celestial_root(collection, anchor, _ROLE_SUN_ROOT)
    moon_root = _ensure_celestial_root(collection, anchor, _ROLE_MOON_ROOT)
    light = _ensure_key_light(collection, anchor)
    ambient_lights = _ensure_ambient_lights(collection, anchor)
    _ensure_camera_lights(collection, anchor, camera_lights)
    warnings: list[str] = []
    cloud_asset = resolve_weather_cloud_layer(weather_effects, source_path)

    sun_objects, _sun_mode, _sun_resolved = _ensure_celestial_geometry(
        context,
        collection,
        sun_root,
        role="sun",
        depot_path=sun_mesh_path,
        source_path=source_path,
        material=sun_material,
        color=sun_color,
        import_materials=import_materials,
        warnings=warnings,
    )
    moon_objects, _moon_mode, _moon_resolved = _ensure_celestial_geometry(
        context,
        collection,
        moon_root,
        role="moon",
        depot_path=moon_mesh_path,
        source_path=source_path,
        material=moon_material,
        color=moon_color,
        import_materials=import_materials,
        warnings=warnings,
    )

    if import_materials:
        moon_texture_path = resolve_moon_detail_texture(moon_material_path, source_path)
        moon_detail = _load_moon_detail_image(moon_texture_path, source_path, warnings)
        _ensure_sun_preview_material(sun_objects, sun_color, strength=sun_brightness)
        _ensure_moon_preview_material(
            moon_objects,
            moon_color,
            detail_image=moon_detail,
            detail_path=moon_texture_path,
            detail_source_path=source_path,
            strength=moon_brightness,
            phase_light=_moon_phase_light(moon_direction, moon_phase),
            uv_scroll=moon_scroll,
        )
    else:
        _ensure_flat_preview_material(sun_objects, "sun", sun_color)
        _ensure_flat_preview_material(moon_objects, "moon", moon_color)

    cloud_objects, _cloud_mode, _cloud_resolved = _ensure_cloud_layer(
        context,
        collection,
        anchor,
        asset=cloud_asset,
        source_path=source_path,
        amount=cloud_amount,
        day_factor=sky_day_factor,
        time_seconds=seconds,
        warnings=warnings,
    )
    _ensure_fog_volume(
        context.scene,
        collection,
        anchor,
        color=fog_color,
        density=fog_density,
        appear_distance=fog_appear_distance,
        appear_range=fog_appear_range,
        final_exp=fog_final_exp,
        vert_offset=fog_vert_offset,
        vert_density=fog_vert_density,
    )
    _update_world_water_materials(
        scene=context.scene,
        color=water_color,
        fresnel=water_fresnel,
        ambient_scale=water_ambient_scale,
        diffuse_scale=water_diffuse_scale,
        flow_intensity=water_flow_intensity,
        foam_intensity=water_foam_intensity,
    )

    sky_world = None
    stars_resolved = ""
    if sky_enabled:
        declared_stars = (
            stars_cube_path
            or resolve_sky_stars_cube(skybox_material_path, source_path)
            or DEFAULT_STARS_CUBE
        )
        stars_image, stars_resolved = _load_stars_image(declared_stars, source_path, warnings)
        sky_world = _update_environment_world(
            context.scene,
            stars_image=stars_image,
            replace_stars_image=True,
            sky_zenith_color=sky_zenith_color,
            sky_horizon_color=sky_horizon_color,
            sun_horizon_color=sun_horizon_color,
            sun_back_horizon_color=sun_back_horizon_color,
            sun_direction=sun_direction,
            moon_direction=moon_direction,
            sun_color=sun_color,
            moon_color=moon_color,
            sun_sky_color=sun_sky_color,
            sun_sky_brightness=sun_sky_brightness,
            sun_area_sky_size=sun_area_sky_size,
            sun_influence=sun_influence,
            moon_sky_color=moon_sky_color,
            moon_sky_brightness=moon_sky_brightness,
            moon_area_sky_size=moon_area_sky_size,
            moon_influence=moon_influence,
            sky_brightness=sky_brightness,
            fog_color=fog_color,
            fog_color_front=fog_color_front,
            fog_color_middle=fog_color_middle,
            fog_color_back=fog_color_back,
            aerial_color_front=aerial_color_front,
            aerial_color_middle=aerial_color_middle,
            aerial_color_back=aerial_color_back,
            fog_direction=(
                key_direction if fog_direction is None and key_direction is not None else fog_direction
            ),
            fog_sky_density=fog_sky_density,
            fog_density=fog_density,
            fog_dist_clamp=fog_dist_clamp,
            fog_final_exp=fog_final_exp,
            aerial_final_exp=aerial_final_exp,
            sky_day_factor=sky_day_factor,
            horizon_attenuation=horizon_attenuation,
            stars_brightness=stars_brightness,
            # A selected mesh-backed weather particle replaces the old
            # procedural approximation; do not draw both layers together.
            cloud_amount=0.0 if cloud_objects else cloud_amount,
        )
        if sky_world is not None:
            sky_world["witcher_environment_stars_path"] = str(declared_stars or "")
            sky_world["witcher_environment_stars_resolved"] = str(
                stars_resolved if stars_image is not None else ""
            )
            sky_world[_STARS_SOURCE_PROP] = _source_key(source_path)
    else:
        _restore_environment_world(context.scene)

    _set_celestial_transform(sun_root, sun_direction, sun_size, anchor_distance)
    _set_celestial_transform(moon_root, moon_direction, moon_size, anchor_distance)
    _set_fallback_color(sun_objects, sun_color)
    _set_fallback_color(moon_objects, moon_color)
    _set_key_light(
        light,
        sun_direction if key_direction is None else key_direction,
        key_color,
        key_energy,
    )
    _set_ambient_lights(ambient_lights, ambient_color, ambient_energy)
    camera = getattr(context.scene, "camera", None)
    _update_camera_light_positions(
        collection,
        camera.matrix_world if camera is not None else None,
    )
    _update_balance_preview(
        context,
        source_path=source_path,
        balance_map_path=balance_map_path,
        amount=balance_map_amount,
        brightness=balance_post_brightness,
        exposure_ev=tone_exposure_ev,
        tone_curve_parameters=tone_curve_parameters,
        tone_post_scale=tone_post_scale,
        warnings=warnings,
    )
    _ensure_view_follow()
    return _result_from_collection(
        collection,
        warnings,
        sky_world=sky_world,
        stars_resolved_path=stars_resolved,
    )


def update_preview(
    context,
    *,
    source_path: str = "",
    sun_mesh_path: str = "",
    moon_mesh_path: str = "",
    sun_material: Any = None,
    moon_material: Any = None,
    skybox_material_path: str = "",
    moon_material_path: str = "",
    stars_cube_path: str = "",
    weather_effects: Sequence[Any] = (),
    sun_direction: Sequence[float] = (0.0, 0.0, 1.0),
    moon_direction: Sequence[float] = (0.0, 0.0, -1.0),
    key_direction: Sequence[float] | None = None,
    sun_size: float = 1.0,
    moon_size: float = 1.0,
    sun_color: Sequence[float] = (1.0, 0.75, 0.35),
    moon_color: Sequence[float] = (0.55, 0.65, 1.0),
    sun_sky_color: Sequence[float] = (0.55, 0.78, 1.0),
    sun_sky_brightness: float = 1.0,
    sun_area_sky_size: float = 0.33,
    sun_influence: float = 1.0,
    moon_sky_color: Sequence[float] = (0.55, 0.78, 1.0),
    moon_sky_brightness: float = 1.0,
    moon_area_sky_size: float = 0.33,
    moon_influence: float = 0.0,
    key_color: Sequence[float] = (1.0, 1.0, 1.0),
    key_energy: float = 3.0,
    ambient_color: Sequence[float] = (0.61, 0.75, 1.0),
    ambient_energy: float = 0.0,
    camera_lights: Sequence[Any] = (),
    tone_exposure_ev: float = 0.0,
    tone_curve_parameters: Sequence[float] = (0.22, 0.30, 0.10, 0.20, 0.01, 0.30),
    tone_post_scale: float = 1.0,
    balance_map_path: str = "",
    balance_map_amount: float = 0.0,
    balance_post_brightness: float = 1.0,
    sky_zenith_color: Sequence[float] = (0.15, 0.35, 0.8),
    sky_horizon_color: Sequence[float] = (0.8, 0.5, 0.35),
    sun_horizon_color: Sequence[float] = (0.85, 0.9, 1.0),
    sun_back_horizon_color: Sequence[float] = (0.364, 0.307, 0.298),
    sky_brightness: float = 1.0,
    fog_color: Sequence[float] = (0.08, 0.30, 0.42),
    fog_color_front: Sequence[float] | None = None,
    fog_color_middle: Sequence[float] | None = None,
    fog_color_back: Sequence[float] | None = None,
    aerial_color_front: Sequence[float] = (1.0, 1.0, 1.0),
    aerial_color_middle: Sequence[float] = (1.0, 1.0, 1.0),
    aerial_color_back: Sequence[float] = (1.0, 1.0, 1.0),
    fog_direction: Sequence[float] | None = None,
    fog_sky_density: float = 0.0,
    fog_density: float = 0.0,
    fog_dist_clamp: float = 0.0,
    fog_appear_distance: float = 0.0,
    fog_appear_range: float = 0.0,
    fog_final_exp: float = 1.0,
    aerial_final_exp: float = 1.0,
    fog_vert_offset: float = 0.0,
    fog_vert_density: float = 0.0,
    water_color: Sequence[float] = (0.0, 0.0, 0.0),
    water_fresnel: float = 1.0,
    water_ambient_scale: float = 0.1,
    water_diffuse_scale: float = 0.4,
    water_flow_intensity: float = 0.6,
    water_foam_intensity: float = 0.0,
    sky_day_factor: float = 1.0,
    horizon_attenuation: float = 1.8,
    stars_brightness: float = 1.1,
    cloud_amount: float = 0.45,
    sun_brightness: float = 5.0,
    moon_brightness: float = 1.0,
    sky_enabled: bool = True,
    anchor_distance: float = 100.0,
    time_seconds: float | None = None,
    day_number: float = 0.0,
    import_materials: bool = True,
) -> PreviewResult:
    """Apply one evaluated day-cycle state without re-importing assets."""

    collection = ensure_environment_collection(context)
    anchor = _ensure_anchor(context, collection)
    sun_root = _ensure_celestial_root(collection, anchor, _ROLE_SUN_ROOT)
    moon_root = _ensure_celestial_root(collection, anchor, _ROLE_MOON_ROOT)
    light = _ensure_key_light(collection, anchor)
    ambient_lights = _ensure_ambient_lights(collection, anchor)
    _ensure_camera_lights(collection, anchor, camera_lights)
    warnings: list[str] = []
    sun_geometry = _find_role_objects(collection, _ROLE_SUN_GEOMETRY)
    moon_geometry = _find_role_objects(collection, _ROLE_MOON_GEOMETRY)
    requested_sun = _normalise_depot_path(sun_mesh_path)
    requested_moon = _normalise_depot_path(moon_mesh_path)
    assets_changed = (
        (requested_sun and not _asset_matches(sun_root, requested_sun, sun_geometry, source_path))
        or (requested_moon and not _asset_matches(moon_root, requested_moon, moon_geometry, source_path))
    )
    if not sun_geometry or not moon_geometry or assets_changed:
        return ensure_preview(
            context,
            source_path=source_path,
            sun_mesh_path=sun_mesh_path,
            moon_mesh_path=moon_mesh_path,
            sun_material=sun_material,
            moon_material=moon_material,
            skybox_material_path=skybox_material_path,
            moon_material_path=moon_material_path,
            stars_cube_path=stars_cube_path,
            weather_effects=weather_effects,
            sun_direction=sun_direction,
            moon_direction=moon_direction,
            key_direction=key_direction,
            sun_size=sun_size,
            moon_size=moon_size,
            sun_color=sun_color,
            moon_color=moon_color,
            sun_sky_color=sun_sky_color,
            sun_sky_brightness=sun_sky_brightness,
            sun_area_sky_size=sun_area_sky_size,
            sun_influence=sun_influence,
            moon_sky_color=moon_sky_color,
            moon_sky_brightness=moon_sky_brightness,
            moon_area_sky_size=moon_area_sky_size,
            moon_influence=moon_influence,
            key_color=key_color,
            key_energy=key_energy,
            ambient_color=ambient_color,
            ambient_energy=ambient_energy,
            camera_lights=camera_lights,
            tone_exposure_ev=tone_exposure_ev,
            tone_curve_parameters=tone_curve_parameters,
            tone_post_scale=tone_post_scale,
            balance_map_path=balance_map_path,
            balance_map_amount=balance_map_amount,
            balance_post_brightness=balance_post_brightness,
            sky_zenith_color=sky_zenith_color,
            sky_horizon_color=sky_horizon_color,
            sun_horizon_color=sun_horizon_color,
            sun_back_horizon_color=sun_back_horizon_color,
            sky_brightness=sky_brightness,
            fog_color=fog_color,
            fog_color_front=fog_color_front,
            fog_color_middle=fog_color_middle,
            fog_color_back=fog_color_back,
            aerial_color_front=aerial_color_front,
            aerial_color_middle=aerial_color_middle,
            aerial_color_back=aerial_color_back,
            fog_direction=fog_direction,
            fog_sky_density=fog_sky_density,
            fog_density=fog_density,
            fog_dist_clamp=fog_dist_clamp,
            fog_appear_distance=fog_appear_distance,
            fog_appear_range=fog_appear_range,
            fog_final_exp=fog_final_exp,
            aerial_final_exp=aerial_final_exp,
            fog_vert_offset=fog_vert_offset,
            fog_vert_density=fog_vert_density,
            water_color=water_color,
            water_fresnel=water_fresnel,
            water_ambient_scale=water_ambient_scale,
            water_diffuse_scale=water_diffuse_scale,
            water_flow_intensity=water_flow_intensity,
            water_foam_intensity=water_foam_intensity,
            sky_day_factor=sky_day_factor,
            horizon_attenuation=horizon_attenuation,
            stars_brightness=stars_brightness,
            cloud_amount=cloud_amount,
            sun_brightness=sun_brightness,
            moon_brightness=moon_brightness,
            sky_enabled=sky_enabled,
            anchor_distance=anchor_distance,
            time_seconds=time_seconds,
            day_number=day_number,
            import_materials=import_materials,
        )
    if time_seconds is not None:
        collection[_TIME_PROP] = float(time_seconds) % 86400.0
    seconds = float(collection.get(_TIME_PROP, 43200.0))
    moon_phase = _moon_phase_angle(day_number, seconds)
    moon_scroll = -seconds / 86400.0

    _set_celestial_transform(sun_root, sun_direction, sun_size, anchor_distance)
    _set_celestial_transform(moon_root, moon_direction, moon_size, anchor_distance)
    sun_geometry = _find_role_objects(collection, _ROLE_SUN_GEOMETRY)
    moon_geometry = _find_role_objects(collection, _ROLE_MOON_GEOMETRY)
    _set_fallback_color(sun_geometry, sun_color)
    _set_fallback_color(moon_geometry, moon_color)
    if import_materials:
        moon_owner = _preview_owner(moon_geometry)
        moon_material_preview = _find_managed_material("moon", moon_owner)
        moon_detail_node = (
            moon_material_preview.node_tree.nodes.get("W3 Moon Detail")
            if moon_material_preview is not None and moon_material_preview.use_nodes
            else None
        )
        moon_texture_path = resolve_moon_detail_texture(moon_material_path, source_path)
        current_moon_texture = (
            str(moon_material_preview.get(_MOON_TEXTURE_PROP, "") or "")
            if moon_material_preview is not None
            else ""
        )
        current_moon_source = (
            str(moon_material_preview.get(_MOON_TEXTURE_SOURCE_PROP, "") or "")
            if moon_material_preview is not None
            else ""
        )
        same_moon_texture = (
            _normalise_depot_path(moon_texture_path).lower()
            == _normalise_depot_path(current_moon_texture).lower()
            and current_moon_source == _source_key(source_path)
        )
        moon_detail = getattr(moon_detail_node, "image", None)
        if not same_moon_texture or (moon_texture_path and moon_detail is None):
            moon_detail = _load_moon_detail_image(moon_texture_path, source_path, warnings)
        _ensure_sun_preview_material(sun_geometry, sun_color, strength=sun_brightness)
        _ensure_moon_preview_material(
            moon_geometry,
            moon_color,
            detail_image=moon_detail,
            detail_path=moon_texture_path,
            detail_source_path=source_path,
            strength=moon_brightness,
            phase_light=_moon_phase_light(moon_direction, moon_phase),
            uv_scroll=moon_scroll,
        )
    else:
        _ensure_flat_preview_material(sun_geometry, "sun", sun_color)
        _ensure_flat_preview_material(moon_geometry, "moon", moon_color)
    cloud_asset = resolve_weather_cloud_layer(weather_effects, source_path)
    cloud_objects, _cloud_mode, _cloud_resolved = _ensure_cloud_layer(
        context,
        collection,
        anchor,
        asset=cloud_asset,
        source_path=source_path,
        amount=cloud_amount,
        day_factor=sky_day_factor,
        time_seconds=seconds,
        warnings=warnings,
    )
    _ensure_fog_volume(
        context.scene,
        collection,
        anchor,
        color=fog_color,
        density=fog_density,
        appear_distance=fog_appear_distance,
        appear_range=fog_appear_range,
        final_exp=fog_final_exp,
        vert_offset=fog_vert_offset,
        vert_density=fog_vert_density,
    )
    _update_world_water_materials(
        scene=context.scene,
        color=water_color,
        fresnel=water_fresnel,
        ambient_scale=water_ambient_scale,
        diffuse_scale=water_diffuse_scale,
        flow_intensity=water_flow_intensity,
        foam_intensity=water_foam_intensity,
    )
    _set_key_light(
        light,
        sun_direction if key_direction is None else key_direction,
        key_color,
        key_energy,
    )
    _set_ambient_lights(ambient_lights, ambient_color, ambient_energy)
    camera = getattr(context.scene, "camera", None)
    _update_camera_light_positions(
        collection,
        camera.matrix_world if camera is not None else None,
    )
    sky_world = None
    stars_resolved = ""
    if sky_enabled:
        stars_image = None
        sky_world = _managed_world(context.scene)
        declared_stars = (
            stars_cube_path
            or resolve_sky_stars_cube(skybox_material_path, source_path)
            or DEFAULT_STARS_CUBE
        )
        current_stars = (
            str(sky_world.get("witcher_environment_stars_path", "") or "")
            if sky_world is not None
            else ""
        )
        current_stars_source = (
            str(sky_world.get(_STARS_SOURCE_PROP, "") or "")
            if sky_world is not None
            else ""
        )
        stars_node = (
            sky_world.node_tree.nodes.get(_SKY_NODE_STARS)
            if sky_world is not None and getattr(sky_world, "node_tree", None) is not None
            else None
        )
        same_stars = (
            _normalise_depot_path(declared_stars).lower()
            == _normalise_depot_path(current_stars).lower()
            and current_stars_source == _source_key(source_path)
        )
        needs_stars = bool(declared_stars) and (
            sky_world is None or not same_stars or getattr(stars_node, "image", None) is None
        )
        replace_stars_image = sky_world is None or not same_stars or not declared_stars or needs_stars
        if needs_stars:
            stars_image, stars_resolved = _load_stars_image(
                declared_stars,
                source_path,
                warnings,
            )
        else:
            stars_resolved = (
                str(sky_world.get("witcher_environment_stars_resolved", "") or "")
                if sky_world is not None
                else ""
            )
        sky_world = _update_environment_world(
            context.scene,
            stars_image=stars_image,
            replace_stars_image=replace_stars_image,
            sky_zenith_color=sky_zenith_color,
            sky_horizon_color=sky_horizon_color,
            sun_horizon_color=sun_horizon_color,
            sun_back_horizon_color=sun_back_horizon_color,
            sun_direction=sun_direction,
            moon_direction=moon_direction,
            sun_color=sun_color,
            moon_color=moon_color,
            sun_sky_color=sun_sky_color,
            sun_sky_brightness=sun_sky_brightness,
            sun_area_sky_size=sun_area_sky_size,
            sun_influence=sun_influence,
            moon_sky_color=moon_sky_color,
            moon_sky_brightness=moon_sky_brightness,
            moon_area_sky_size=moon_area_sky_size,
            moon_influence=moon_influence,
            sky_brightness=sky_brightness,
            fog_color=fog_color,
            fog_color_front=fog_color_front,
            fog_color_middle=fog_color_middle,
            fog_color_back=fog_color_back,
            aerial_color_front=aerial_color_front,
            aerial_color_middle=aerial_color_middle,
            aerial_color_back=aerial_color_back,
            fog_direction=(
                key_direction if fog_direction is None and key_direction is not None else fog_direction
            ),
            fog_sky_density=fog_sky_density,
            fog_density=fog_density,
            fog_dist_clamp=fog_dist_clamp,
            fog_final_exp=fog_final_exp,
            aerial_final_exp=aerial_final_exp,
            sky_day_factor=sky_day_factor,
            horizon_attenuation=horizon_attenuation,
            stars_brightness=stars_brightness,
            cloud_amount=0.0 if cloud_objects else cloud_amount,
        )
        sky_world["witcher_environment_stars_path"] = str(declared_stars or "")
        sky_world[_STARS_SOURCE_PROP] = _source_key(source_path)
        sky_world["witcher_environment_stars_resolved"] = str(
            stars_resolved if not replace_stars_image or stars_image is not None else ""
        )
    else:
        _restore_environment_world(context.scene)
    _update_balance_preview(
        context,
        source_path=source_path,
        balance_map_path=balance_map_path,
        amount=balance_map_amount,
        brightness=balance_post_brightness,
        exposure_ev=tone_exposure_ev,
        tone_curve_parameters=tone_curve_parameters,
        tone_post_scale=tone_post_scale,
        warnings=warnings,
    )
    _ensure_view_follow()
    return _result_from_collection(
        collection,
        warnings,
        sky_world=sky_world,
        stars_resolved_path=stars_resolved,
    )


def _evaluation_value(evaluation: Any, name: str, default: Any) -> Any:
    if isinstance(evaluation, Mapping):
        return evaluation.get(name, default)
    return getattr(evaluation, name, default)


def apply_environment_evaluation(context, evaluation: Any, **overrides) -> PreviewResult:
    """Adapt a parser evaluation object/dict to :func:`update_preview`.

    Expected field names are ``sun_direction``, ``moon_direction``,
    ``key_direction`` (or ``light_direction``), sizes/colors, ``key_energy`` and
    ``time_seconds``. Missing fields retain the preview defaults.
    """

    defaults = {
        "sun_direction": (0.0, 0.0, 1.0),
        "moon_direction": (0.0, 0.0, -1.0),
        "key_direction": None,
        "sun_size": 1.0,
        "moon_size": 1.0,
        "sun_color": (1.0, 0.75, 0.35),
        "moon_color": (0.55, 0.65, 1.0),
        "sun_sky_color": (0.55, 0.78, 1.0),
        "sun_sky_brightness": 1.0,
        "sun_area_sky_size": 0.33,
        "sun_influence": 1.0,
        "moon_sky_color": (0.55, 0.78, 1.0),
        "moon_sky_brightness": 1.0,
        "moon_area_sky_size": 0.33,
        "moon_influence": 0.0,
        "key_color": (1.0, 1.0, 1.0),
        "key_energy": 3.0,
        "ambient_color": (0.61, 0.75, 1.0),
        "ambient_energy": 0.0,
        "camera_lights": (),
        "tone_exposure_ev": 0.0,
        "tone_curve_parameters": (0.22, 0.30, 0.10, 0.20, 0.01, 0.30),
        "tone_post_scale": 1.0,
        "balance_map_path": "",
        "balance_map_amount": 0.0,
        "balance_post_brightness": 1.0,
        "sky_zenith_color": (0.15, 0.35, 0.8),
        "sky_horizon_color": (0.8, 0.5, 0.35),
        "sun_horizon_color": (0.85, 0.9, 1.0),
        "sun_back_horizon_color": (0.364, 0.307, 0.298),
        "sky_brightness": 1.0,
        "fog_color": (0.08, 0.30, 0.42),
        "fog_color_front": None,
        "fog_color_middle": None,
        "fog_color_back": None,
        "aerial_color_front": (1.0, 1.0, 1.0),
        "aerial_color_middle": (1.0, 1.0, 1.0),
        "aerial_color_back": (1.0, 1.0, 1.0),
        "fog_direction": None,
        "fog_sky_density": 0.0,
        "fog_density": 0.0,
        "fog_dist_clamp": 0.0,
        "fog_appear_distance": 0.0,
        "fog_appear_range": 0.0,
        "fog_final_exp": 1.0,
        "aerial_final_exp": 1.0,
        "fog_vert_offset": 0.0,
        "fog_vert_density": 0.0,
        "water_color": (0.0, 0.0, 0.0),
        "water_fresnel": 1.0,
        "water_ambient_scale": 0.1,
        "water_diffuse_scale": 0.4,
        "water_flow_intensity": 0.6,
        "water_foam_intensity": 0.0,
        "sky_day_factor": 1.0,
        "horizon_attenuation": 1.8,
        "stars_brightness": 1.1,
        "cloud_amount": 0.45,
        "weather_effects": (),
        "sun_brightness": 5.0,
        "moon_brightness": 1.0,
        "sky_enabled": True,
        "anchor_distance": 100.0,
        "time_seconds": None,
        "day_number": 0.0,
    }
    values = {
        name: _evaluation_value(evaluation, name, default)
        for name, default in defaults.items()
    }
    if values["key_direction"] is None:
        values["key_direction"] = _evaluation_value(
            evaluation,
            "light_direction",
            values["sun_direction"],
        )
    values.update(overrides)
    return update_preview(context, **values)


def clear_preview(context) -> int:
    from .environment_balance_preview import clear_balance_preview

    scene = context.scene
    restored_balance = clear_balance_preview(context)
    restored_volumetric_end = _restore_fog_volumetric_end(scene)
    restored_view_exposure = _restore_view_exposure(scene)
    restored_clip_ends = _restore_preview_clip_ends(scene)
    collection = next(
        (item for item in _iter_scene_collections(scene.collection) if _is_managed(item)),
        None,
    )
    restored_world = _restore_environment_world(scene)
    if collection is None:
        _remove_unused_preview_images()
        if not _preview_anchors():
            stop_preview_runtime()
        return (
            int(restored_world)
            + int(restored_volumetric_end)
            + int(restored_view_exposure)
            + int(restored_balance)
            + restored_clip_ends
        )

    managed = [obj for obj in list(collection.objects) if _is_managed(obj)]
    managed.sort(key=lambda obj: 0 if getattr(obj, "parent", None) is not None else 1)
    for obj in managed:
        _remove_object(obj)

    if not collection.objects and not collection.children:
        try:
            bpy.data.collections.remove(collection)
        except Exception:
            try:
                scene.collection.children.unlink(collection)
            except Exception:
                pass
    _remove_unused_preview_images()
    if not _preview_anchors():
        stop_preview_runtime()
    return (
        len(managed)
        + int(restored_world)
        + int(restored_volumetric_end)
        + int(restored_view_exposure)
        + int(restored_balance)
        + restored_clip_ends
    )


__all__ = (
    "ENVIRONMENT_COLLECTION_NAME",
    "PreviewResult",
    "apply_environment_evaluation",
    "clear_preview",
    "ensure_environment_collection",
    "ensure_preview",
    "resolve_environment_asset",
    "resolve_moon_detail_texture",
    "resolve_sky_stars_cube",
    "stop_preview_runtime",
    "update_preview",
)
