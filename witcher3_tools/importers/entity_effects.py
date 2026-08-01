"""Native Blender previews for supported cooked entity effects."""

from __future__ import annotations

import json
import logging
from math import radians

import bpy

from .entity_light import configure_entity_light_flicker


log = logging.getLogger(__name__)

FIRE_ENABLED_PROP = "witcher_fire_enabled"
FIRE_EFFECT_PROP = "witcher_fire_effect"
FIRE_PARTICLE_PROP = "witcher_fire_particle"
FIRE_PREVIEW_VERSION = 10

WATER_ENABLED_PROP = "witcher_water_fx_enabled"
WATER_EFFECT_PROP = "witcher_water_fx_effect"
WATER_PARTICLES_PROP = "witcher_water_fx_particles"
WATER_PREVIEW_VERSION = 16

_FOUNTAIN_PARTICLE_KINDS = {
    "p_splash_circle.w2p": "impact",
    "p_fountain_tip.w2p": "tip",
}


def _entry_get(value, key, default=None):
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _slot_lookup(entity) -> dict[str, object]:
    result = {}
    for slot in _entry_get(entity, "slots", []) or []:
        name = str(_entry_get(slot, "name", "") or "").strip()
        if name:
            result.setdefault(name.lower(), slot)
    return result


def _is_flame_particle(path: str) -> bool:
    lowered = str(path or "").replace("/", "\\").lower()
    basename = lowered.rsplit("\\", 1)[-1]
    return basename.endswith(".w2p") and ("flame" in basename or "fire" in basename)


def fire_preview_specs(entity) -> list[dict]:
    slots = _slot_lookup(entity)
    specs = []
    for effect in _entry_get(entity, "cookedEffects", []) or []:
        effect_name = str(_entry_get(effect, "name", "") or "").strip()
        is_looped = bool(_entry_get(effect, "is_looped", False))
        if not is_looped:
            continue
        particles = list(_entry_get(effect, "particle_systems", []) or [])
        if effect_name.lower() != "fire" and not any(
            _is_flame_particle(_entry_get(particle, "path", ""))
            for particle in particles
        ):
            continue
        loop_start = float(_entry_get(effect, "loop_start", 0.0) or 0.0)
        loop_end = float(_entry_get(effect, "loop_end", 0.0) or 0.0)
        for particle in particles:
            particle_path = str(_entry_get(particle, "path", "") or "")
            if not particle_path.lower().endswith(".w2p"):
                continue
            time_begin = _entry_get(particle, "time_begin", None)
            duration = _entry_get(particle, "duration", None)
            if loop_end > loop_start and time_begin is not None and duration is not None:
                # Ignore start/stop tails outside the steady loop.
                time_begin = float(time_begin)
                if time_begin > loop_end or time_begin + max(0.0, float(duration)) < loop_start:
                    continue
            slot_name = str(
                _entry_get(particle, "slot", "")
                or effect_name
                or "fire"
            ).strip()
            slot = slots.get(slot_name.lower())
            if slot is None:
                log.debug("Skipping flame effect '%s': slot '%s' was not found", effect_name, slot_name)
                continue
            specs.append({
                "name": effect_name or "fire",
                "particle_path": particle_path,
                "slot_name": slot_name,
                "transform": _entry_get(slot, "transform", None),
                "is_looped": is_looped,
                "length": float(_entry_get(effect, "length", 0.0) or 0.0),
            })
    return specs


def fountain_preview_specs(entity) -> list[dict]:
    slots = _slot_lookup(entity)
    specs = []
    for effect in _entry_get(entity, "cookedEffects", []) or []:
        if not bool(_entry_get(effect, "is_looped", False)):
            continue
        effect_name = str(_entry_get(effect, "name", "") or "").strip()
        for particle in _entry_get(effect, "particle_systems", []) or []:
            particle_path = str(_entry_get(particle, "path", "") or "")
            basename = particle_path.replace("/", "\\").rsplit("\\", 1)[-1].lower()
            kind = _FOUNTAIN_PARTICLE_KINDS.get(basename)
            if kind is None:
                continue
            slot_name = str(_entry_get(particle, "slot", "") or "").strip()
            slot = slots.get(slot_name.lower())
            if slot is None:
                log.debug("Skipping fountain effect '%s': slot '%s' was not found", effect_name, slot_name)
                continue
            specs.append({
                "name": effect_name or "fountain_water",
                "kind": kind,
                "particle_path": particle_path,
                "slot_name": slot_name,
                "transform": _entry_get(slot, "transform", None),
                "is_looped": True,
                "length": float(_entry_get(effect, "length", 0.0) or 0.0),
            })
    return specs


def _link_object(obj, target_collection):
    collection = target_collection or getattr(bpy.context, "collection", None)
    if collection is None:
        collection = getattr(getattr(bpy.context, "scene", None), "collection", None)
    if collection is not None and obj.name not in collection.objects:
        collection.objects.link(obj)


def _transform_value(transform, key, default=0.0):
    value = _entry_get(transform, key, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _apply_local_transform(obj, transform):
    if transform is None:
        return
    obj.location = (
        _transform_value(transform, "X"),
        _transform_value(transform, "Y"),
        _transform_value(transform, "Z"),
    )
    obj.rotation_mode = 'YXZ'
    obj.rotation_euler = (
        radians(_transform_value(transform, "Yaw")),
        radians(_transform_value(transform, "Pitch")),
        radians(_transform_value(transform, "Roll")),
    )
    obj.scale = (
        _transform_value(transform, "Scale_x", 1.0),
        _transform_value(transform, "Scale_y", 1.0),
        _transform_value(transform, "Scale_z", 1.0),
    )


def _find_entity_slot(owner, slot_name):
    key = str(slot_name or "").strip().lower()
    return next(
        (
            child
            for child in getattr(owner, "children_recursive", ()) or ()
            if str(child.get("witcher_slot_name", "") or "").strip().lower() == key
        ),
        None,
    ) if key else None


def _remove_preview_tree(anchor):
    for child in list(getattr(anchor, "children_recursive", ()) or ()):
        if (
            child.get("witcher_effect_preview_child")
            or child.get("witcher_particle_preview")
            or child.get("witcher_particle_billboard_basis")
            or child.get("witcher_type") in {"CFXFlamePreview", "CFXFountainPreview"}
        ):
            bpy.data.objects.remove(child, do_unlink=True)
    bpy.data.objects.remove(anchor, do_unlink=True)


def _add_enabled_driver(target, data_path, index, owner, expression, enabled_prop=FIRE_ENABLED_PROP):
    try:
        target.driver_remove(data_path, index)
    except (TypeError, ValueError):
        pass
    fcurve = target.driver_add(data_path, index)
    driver = fcurve.driver
    driver.type = 'SCRIPTED'
    variable = driver.variables.new()
    variable.name = "enabled"
    variable.type = 'SINGLE_PROP'
    variable.targets[0].id = owner
    variable.targets[0].data_path = f'["{enabled_prop}"]'
    driver.expression = expression
    return fcurve


def _set_owner_property(owner, name, value, description):
    owner[name] = value
    try:
        owner.id_properties_ui(name).update(description=description)
    except Exception:
        pass


def _fountain_effect_key(spec):
    particle_key = spec["particle_path"].replace("/", "\\").lower()
    return f"fountain|{spec['name'].lower()}|{spec['slot_name'].lower()}|{particle_key}"


def _import_particle_preview(source, **kwargs):
    from .import_particle import import_particle_system

    return import_particle_system(source, **kwargs)


def _create_w2p_preview(owner, spec, target_collection, *, family):
    from .import_particle import PARTICLE_PREVIEW_VERSION

    is_water = family == "fountain"
    effect_key = (
        _fountain_effect_key(spec)
        if is_water
        else f"{spec['name'].lower()}|{spec['slot_name'].lower()}|{spec['particle_path'].lower()}"
    )
    version_prop = "witcher_water_preview_version" if is_water else "witcher_fire_preview_version"
    version = WATER_PREVIEW_VERSION if is_water else FIRE_PREVIEW_VERSION
    for child in getattr(owner, "children_recursive", ()) or ():
        if child.get("witcher_effect_preview_key") == effect_key:
            if (
                child.get(version_prop) == version
                and child.get("witcher_particle_preview_version") == PARTICLE_PREVIEW_VERSION
            ):
                return child
            _remove_preview_tree(child)
            break

    kind = spec.get("kind", "")
    suffix = f"_{kind}" if kind else ""
    anchor = bpy.data.objects.new(f"{spec['name']}{suffix}_FX", None)
    _link_object(anchor, target_collection)
    slot = _find_entity_slot(owner, spec["slot_name"])
    anchor.parent = slot or owner
    anchor.empty_display_type = 'CIRCLE' if is_water else 'SPHERE'
    anchor.empty_display_size = (0.10 if kind == "impact" else 0.035) if is_water else 0.012
    if slot is None:
        _apply_local_transform(anchor, spec.get("transform"))
    anchor["witcher_type"] = "CFXFountainPreview" if is_water else "CFXPreview"
    anchor["witcher_effect_preview_key"] = effect_key
    anchor["witcher_effect_name"] = spec["name"]
    anchor["witcher_effect_slot"] = spec["slot_name"]
    anchor["witcher_particle_system"] = spec["particle_path"]
    anchor["witcher_particle_systems"] = json.dumps([spec["particle_path"]], separators=(",", ":"))
    anchor["witcher_effect_looped"] = bool(spec["is_looped"])
    anchor["witcher_effect_length"] = float(spec["length"])
    anchor[version_prop] = version
    anchor["witcher_particle_preview_version"] = PARTICLE_PREVIEW_VERSION
    if is_water:
        anchor["witcher_water_preview_kind"] = kind
    try:
        particles = _import_particle_preview(
            spec["particle_path"],
            parent=anchor,
            target_collection=target_collection,
        )
    except Exception:
        log.warning(
            "Failed to import %s particle system '%s'.",
            family,
            spec["particle_path"],
            exc_info=True,
        )
        _remove_preview_tree(anchor)
        return None

    emitters = []
    for particle in particles:
        particle["witcher_effect_preview_child"] = True
        emitter = str(particle.get("witcher_particle_emitter", "") or "")
        if emitter and emitter not in emitters:
            emitters.append(emitter)
    anchor["witcher_particle_emitters"] = json.dumps(emitters, separators=(",", ":"))
    base_scale = tuple(float(value) for value in anchor.scale)
    for axis, scale in enumerate(base_scale):
        _add_enabled_driver(
            anchor,
            "scale",
            axis,
            owner,
            f"enabled*{scale:.9g}",
            enabled_prop=WATER_ENABLED_PROP if is_water else FIRE_ENABLED_PROP,
        )
    return anchor


def _configure_light_flicker(light_obj, owner, phase):
    if light_obj is None or getattr(light_obj, "type", "") != 'LIGHT':
        return
    light_obj["witcher_fire_light"] = True
    configure_entity_light_flicker(
        light_obj,
        scene=bpy.context.scene,
        owner=owner,
        enabled_prop=FIRE_ENABLED_PROP,
        phase=phase,
    )


def import_entity_effect_previews(entity, owner, imported_objects=(), target_collection=None):
    """Create supported effect previews and return their anchor objects."""
    if entity is None or owner is None:
        return []
    fire_specs = fire_preview_specs(entity)
    water_specs = fountain_preview_specs(entity)
    if not fire_specs and not water_specs:
        return []

    anchors = []
    if fire_specs:
        source_enabled = _entry_get(entity, "isLightOn", None)
        if FIRE_ENABLED_PROP not in owner:
            _set_owner_property(
                owner,
                FIRE_ENABLED_PROP,
                True if source_enabled is None else bool(source_enabled),
                "Enable the imported Witcher fire preview and its lights.",
            )
        primary = next(
            (spec for spec in fire_specs if _is_flame_particle(spec["particle_path"])),
            fire_specs[0],
        )
        _set_owner_property(owner, FIRE_EFFECT_PROP, primary["name"], "Cooked Witcher effect name.")
        _set_owner_property(owner, FIRE_PARTICLE_PROP, primary["particle_path"], "Source Witcher particle-system path.")
        anchors.extend(
            _create_w2p_preview(owner, spec, target_collection, family="fire")
            for spec in fire_specs
        )

        seen_lights = set()
        light_index = 0
        for obj in imported_objects or ():
            if obj is None or getattr(obj, "type", "") != 'LIGHT':
                continue
            identity = id(obj)
            if identity in seen_lights:
                continue
            seen_lights.add(identity)
            _configure_light_flicker(obj, owner, phase=light_index * 1.71)
            light_index += 1

    if water_specs:
        active_keys = {_fountain_effect_key(spec) for spec in water_specs}
        stale_anchors = [
            child
            for child in getattr(owner, "children_recursive", ()) or ()
            if (
                child.get("witcher_type") == "CFXFountainPreview"
                and child.get("witcher_water_preview_kind") in {"impact", "tip"}
                and (
                    child.get("witcher_effect_preview_key") not in active_keys
                    or child.get("witcher_water_preview_version") != WATER_PREVIEW_VERSION
                )
            )
        ]
        for child in stale_anchors:
            _remove_preview_tree(child)
        if WATER_ENABLED_PROP not in owner:
            _set_owner_property(
                owner,
                WATER_ENABLED_PROP,
                True,
                "Enable the imported Witcher fountain splash previews.",
            )
        _set_owner_property(
            owner,
            WATER_EFFECT_PROP,
            water_specs[0]["name"],
            "Cooked Witcher fountain effect name.",
        )
        _set_owner_property(
            owner,
            WATER_PARTICLES_PROP,
            json.dumps([spec["particle_path"] for spec in water_specs], separators=(",", ":")),
            "Source Witcher fountain particle-system paths.",
        )
        anchors.extend(
            _create_w2p_preview(owner, spec, target_collection, family="fountain")
            for spec in water_specs
        )
    try:
        owner.update_tag()
        bpy.context.view_layer.update()
    except Exception:
        pass
    return [anchor for anchor in anchors if anchor is not None]
