"""Native Blender previews for supported cooked entity effects."""

from __future__ import annotations

import json
import logging
from math import exp, pi, radians, sin

import bpy


log = logging.getLogger(__name__)

FIRE_ENABLED_PROP = "witcher_fire_enabled"
FIRE_EFFECT_PROP = "witcher_fire_effect"
FIRE_PARTICLE_PROP = "witcher_fire_particle"
FIRE_PREVIEW_VERSION = 5
FIRE_SPRITE_IMAGE = "Witcher Candle Flame Sprite"
FIRE_SPRITE_MATERIAL = "Witcher Candle Flame Sprite"
FIRE_SPRITE_IMAGE_PROP = "witcher_fire_preview_image"
FIRE_SPRITE_MATERIAL_PROP = "witcher_fire_preview_material"


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
    """Return the cooked flame effects that have a usable entity slot."""
    slots = _slot_lookup(entity)
    specs = []
    for effect in _entry_get(entity, "cookedEffects", []) or []:
        effect_name = str(_entry_get(effect, "name", "") or "").strip()
        is_looped = bool(_entry_get(effect, "is_looped", False))
        if not is_looped:
            continue
        particles = list(_entry_get(effect, "particle_systems", []) or [])
        flame_particle = next(
            (particle for particle in particles if _is_flame_particle(_entry_get(particle, "path", ""))),
            None,
        )
        if flame_particle is None:
            continue
        slot_name = str(
            _entry_get(flame_particle, "slot", "")
            or effect_name
            or "fire"
        ).strip()
        slot = slots.get(slot_name.lower())
        if slot is None:
            log.debug("Skipping flame effect '%s': slot '%s' was not found", effect_name, slot_name)
            continue
        specs.append({
            "name": effect_name or "fire",
            "particle_path": str(_entry_get(flame_particle, "path", "") or ""),
            "particle_paths": [
                str(_entry_get(particle, "path", "") or "")
                for particle in particles
                if _entry_get(particle, "path", "")
            ],
            "slot_name": slot_name,
            "transform": _entry_get(slot, "transform", None),
            "is_looped": is_looped,
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


def _smoothstep(edge0, edge1, value):
    if edge0 == edge1:
        return 0.0
    value = max(0.0, min(1.0, (value - edge0) / (edge1 - edge0)))
    return value * value * (3.0 - 2.0 * value)


def _get_fire_sprite_image():
    image = next(
        (item for item in bpy.data.images if bool(item.get(FIRE_SPRITE_IMAGE_PROP, False))),
        None,
    )
    if image is not None and image.get("witcher_fire_preview_version") == FIRE_PREVIEW_VERSION:
        return image
    if image is not None and image.users == 0:
        bpy.data.images.remove(image)
    elif image is not None:
        image[FIRE_SPRITE_IMAGE_PROP] = False

    width, height = 96, 192
    image = bpy.data.images.new(FIRE_SPRITE_IMAGE, width=width, height=height, alpha=True)
    image[FIRE_SPRITE_IMAGE_PROP] = True
    image["witcher_fire_preview_version"] = FIRE_PREVIEW_VERSION
    image.alpha_mode = 'STRAIGHT'
    image.colorspace_settings.name = 'Non-Color'
    pixels = []
    for row in range(height):
        y = (row + 0.5) / height
        vertical = max(0.0, sin(pi * y))
        half_width = 0.015 + 0.49 * (vertical ** 0.78) * (1.0 - 0.34 * y)
        center = 0.055 * sin(5.7 * y + 0.35) * (0.25 + 0.75 * y)
        smoke = _smoothstep(0.72, 0.99, y)
        base = 1.0 - _smoothstep(0.03, 0.18, y)
        for column in range(width):
            x = 2.0 * (column + 0.5) / width - 1.0
            distance = abs(x - center) / max(half_width, 1.0e-5)
            alpha = 1.0 - _smoothstep(0.76, 1.02, distance)

            # Leave a small forked foot around the wick.
            notch = exp(-((x - center) / 0.105) ** 2) * base
            alpha *= 1.0 - 0.62 * notch
            alpha *= 0.96 - 0.58 * smoke

            core_width = max(0.035, half_width * (0.43 - 0.10 * y))
            core_distance = abs(x - center * 0.42 + 0.018) / core_width
            core = exp(-(core_distance ** 3.4)) * (1.0 - _smoothstep(0.54, 0.78, y))
            core *= _smoothstep(0.10, 0.25, y)
            heat = max(0.0, 1.0 - distance)

            red = 1.0
            green = 0.08 + 0.85 * heat + 0.45 * core
            blue = 0.002 + 0.035 * heat + 0.22 * core
            smoke_dim = 1.0 - 0.88 * smoke
            red *= smoke_dim
            green *= smoke_dim
            blue *= smoke_dim
            pixels.extend((red, green, blue, max(0.0, min(1.0, alpha))))

    image.pixels.foreach_set(pixels)
    image.update()
    try:
        image.pack()
    except RuntimeError:
        pass
    return image


def _get_fire_sprite_material():
    material = next(
        (item for item in bpy.data.materials if bool(item.get(FIRE_SPRITE_MATERIAL_PROP, False))),
        None,
    )
    if material is not None and material.get("witcher_fire_preview_version") == FIRE_PREVIEW_VERSION:
        return material
    if material is None:
        material = bpy.data.materials.new(name=FIRE_SPRITE_MATERIAL)
    material[FIRE_SPRITE_MATERIAL_PROP] = True
    material["witcher_fire_preview_version"] = FIRE_PREVIEW_VERSION
    material.diffuse_color = (1.0, 0.22, 0.01, 1.0)
    material.use_nodes = True
    nodes = material.node_tree.nodes
    nodes.clear()
    output = nodes.new("ShaderNodeOutputMaterial")
    texture = nodes.new("ShaderNodeTexImage")
    texture.image = _get_fire_sprite_image()
    texture.interpolation = 'Linear'
    texture.extension = 'CLIP'
    emission = nodes.new("ShaderNodeEmission")
    emission.inputs["Strength"].default_value = 2.4
    transparent = nodes.new("ShaderNodeBsdfTransparent")
    mix = nodes.new("ShaderNodeMixShader")
    material.node_tree.links.new(texture.outputs["Color"], emission.inputs["Color"])
    material.node_tree.links.new(texture.outputs["Alpha"], mix.inputs[0])
    material.node_tree.links.new(transparent.outputs[0], mix.inputs[1])
    material.node_tree.links.new(emission.outputs[0], mix.inputs[2])
    material.node_tree.links.new(mix.outputs[0], output.inputs["Surface"])
    if hasattr(material, "surface_render_method"):
        material.surface_render_method = 'DITHERED'
    elif hasattr(material, "blend_method"):
        material.blend_method = 'BLEND'
    if hasattr(material, "use_transparency_overlap"):
        material.use_transparency_overlap = False
    return material


def _create_flame_card(name, width, height, target_collection, material, rotation_z=0.0):
    vertices = (
        (-width * 0.5, 0.0, 0.0),
        (width * 0.5, 0.0, 0.0),
        (width * 0.5, 0.0, height),
        (-width * 0.5, 0.0, height),
    )
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(vertices, [], ((0, 1, 2, 3),))
    uv_layer = mesh.uv_layers.new(name="UVMap")
    uv_by_vertex = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
    for loop in mesh.loops:
        uv_layer.data[loop.index].uv = uv_by_vertex[loop.vertex_index]
    mesh.materials.append(material)
    obj = bpy.data.objects.new(name, mesh)
    _link_object(obj, target_collection)
    obj["witcher_type"] = "CFXFlamePreview"
    obj["witcher_fire_sprite_card"] = True
    obj.rotation_euler.z = rotation_z
    return obj


def _remove_preview_tree(anchor):
    for child in list(getattr(anchor, "children_recursive", ()) or ()):
        if child.get("witcher_type") == "CFXFlamePreview":
            bpy.data.objects.remove(child, do_unlink=True)
    bpy.data.objects.remove(anchor, do_unlink=True)


def _add_enabled_driver(target, data_path, index, owner, expression):
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
    variable.targets[0].data_path = f'["{FIRE_ENABLED_PROP}"]'
    driver.expression = expression
    return fcurve


def _configure_flame_animation(anchor, owner):
    base_scale = tuple(float(value) for value in anchor.scale)
    base_rotation = tuple(float(value) for value in anchor.rotation_euler)
    scale_expressions = (
        f"enabled*{base_scale[0]:.9g}*(1+0.055*sin(frame*0.47)+0.025*sin(frame*1.31))",
        f"enabled*{base_scale[1]:.9g}*(1+0.045*sin(frame*0.61+1.7)+0.02*sin(frame*1.17))",
        f"enabled*{base_scale[2]:.9g}*(1+0.075*sin(frame*0.39+0.8)+0.035*sin(frame*1.43))",
    )
    for axis, expression in enumerate(scale_expressions):
        _add_enabled_driver(anchor, "scale", axis, owner, expression)
    _add_enabled_driver(
        anchor,
        "rotation_euler",
        0,
        owner,
        f"{base_rotation[0]:.9g}+enabled*(0.026*sin(frame*0.29)+0.012*sin(frame*0.97))",
    )
    _add_enabled_driver(
        anchor,
        "rotation_euler",
        1,
        owner,
        f"{base_rotation[1]:.9g}+enabled*(0.032*sin(frame*0.33+1.2)+0.011*sin(frame*1.21))",
    )


def _set_owner_property(owner, name, value, description):
    owner[name] = value
    try:
        owner.id_properties_ui(name).update(description=description)
    except Exception:
        pass


def _create_flame_preview(owner, spec, target_collection):
    effect_key = f"{spec['name'].lower()}|{spec['slot_name'].lower()}|{spec['particle_path'].lower()}"
    for child in getattr(owner, "children_recursive", ()) or ():
        if child.get("witcher_effect_preview_key") == effect_key:
            if child.get("witcher_fire_preview_version") == FIRE_PREVIEW_VERSION:
                return child
            _remove_preview_tree(child)
            break

    anchor = bpy.data.objects.new(f"{spec['name']}_FX", None)
    _link_object(anchor, target_collection)
    anchor.parent = owner
    anchor.empty_display_type = 'SPHERE'
    anchor.empty_display_size = 0.012
    _apply_local_transform(anchor, spec.get("transform"))
    anchor["witcher_type"] = "CFXPreview"
    anchor["witcher_effect_preview_key"] = effect_key
    anchor["witcher_effect_name"] = spec["name"]
    anchor["witcher_effect_slot"] = spec["slot_name"]
    anchor["witcher_particle_systems"] = json.dumps(spec["particle_paths"], separators=(",", ":"))
    anchor["witcher_effect_looped"] = bool(spec["is_looped"])
    anchor["witcher_effect_length"] = float(spec["length"])
    anchor["witcher_fire_preview_version"] = FIRE_PREVIEW_VERSION

    material = _get_fire_sprite_material()
    cards = (
        _create_flame_card(
            f"{spec['name']}_flame_sprite_a",
            width=0.055,
            height=0.126,
            target_collection=target_collection,
            material=material,
        ),
        _create_flame_card(
            f"{spec['name']}_flame_sprite_b",
            width=0.055,
            height=0.126,
            target_collection=target_collection,
            material=material,
            rotation_z=pi * 0.5,
        ),
    )
    for obj in cards:
        obj.parent = anchor
        obj["witcher_effect_name"] = spec["name"]
        obj["witcher_particle_system"] = spec["particle_path"]
    _configure_flame_animation(anchor, owner)
    return anchor


def _configure_light_flicker(light_obj, owner, phase):
    if light_obj is None or getattr(light_obj, "type", "") != 'LIGHT':
        return
    light_data = getattr(light_obj, "data", None)
    if light_data is None or not hasattr(light_data, "energy"):
        return
    base_energy = float(light_obj.get("witcher_base_energy", light_data.energy) or 0.0)
    flicker_strength = float(light_obj.get("witcher_flicker_strength", 0.25) or 0.0)
    position_offset = float(light_obj.get("witcher_flicker_position_offset", 0.0) or 0.0)
    light_obj["witcher_base_energy"] = base_energy
    light_obj["witcher_fire_light"] = True
    expression = (
        f"enabled*{base_energy:.9g}*(1+{flicker_strength:.9g}*("
        f"0.42*sin(frame*0.37+{phase:.9g})+"
        f"0.24*sin(frame*0.83+{phase + 1.3:.9g})+"
        f"0.13*sin(frame*1.73+{phase + 2.1:.9g})))"
    )
    _add_enabled_driver(light_data, "energy", -1, owner, expression)
    if position_offset > 0.0:
        _add_enabled_driver(
            light_obj,
            "delta_location",
            0,
            owner,
            f"enabled*{position_offset:.9g}*(0.55*sin(frame*0.41+{phase:.9g})+0.2*sin(frame*1.19))",
        )
        _add_enabled_driver(
            light_obj,
            "delta_location",
            1,
            owner,
            f"enabled*{position_offset:.9g}*(0.48*sin(frame*0.53+{phase + 0.7:.9g})+0.18*sin(frame*1.31))",
        )


def import_entity_effect_previews(entity, owner, imported_objects=(), target_collection=None):
    """Create supported effect previews and return their anchor objects."""
    if entity is None or owner is None:
        return []
    specs = fire_preview_specs(entity)
    if not specs:
        return []

    source_enabled = _entry_get(entity, "isLightOn", None)
    if FIRE_ENABLED_PROP not in owner:
        _set_owner_property(
            owner,
            FIRE_ENABLED_PROP,
            True if source_enabled is None else bool(source_enabled),
            "Enable the imported Witcher fire preview and its lights.",
        )
    primary = specs[0]
    _set_owner_property(owner, FIRE_EFFECT_PROP, primary["name"], "Cooked Witcher effect name.")
    _set_owner_property(owner, FIRE_PARTICLE_PROP, primary["particle_path"], "Source Witcher particle-system path.")

    anchors = [_create_flame_preview(owner, spec, target_collection) for spec in specs]
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
    try:
        owner.update_tag()
        bpy.context.view_layer.update()
    except Exception:
        pass
    return [anchor for anchor in anchors if anchor is not None]
