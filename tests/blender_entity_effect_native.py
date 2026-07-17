"""Blender-native smoke test for cooked entity flame previews.

Run with:
  blender --background --factory-startup --python tests/blender_entity_effect_native.py
"""

from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import bpy  # noqa: E402
import witcher3_tools  # noqa: E402


def main() -> None:
    witcher3_tools.register()
    try:
        from witcher3_tools.importers import entity_effects
        from witcher3_tools.ui.ui_entity import WITCH_PT_ENTITY_EFFECTS_Panel

        scene = bpy.context.scene
        collection = scene.collection
        user_material = bpy.data.materials.new(entity_effects.FIRE_SPRITE_MATERIAL)
        user_material.use_nodes = True
        user_node = user_material.node_tree.nodes.new("ShaderNodeValue")
        user_image = bpy.data.images.new(entity_effects.FIRE_SPRITE_IMAGE, 4, 4)
        owner = bpy.data.objects.new("Candle Entity", None)
        collection.objects.link(owner)

        light_data = bpy.data.lights.new("Candle Light", type='POINT')
        light_data.energy = 100.0
        light_obj = bpy.data.objects.new("Candle Light", light_data)
        collection.objects.link(light_obj)
        light_obj.parent = owner
        light_obj["witcher_base_energy"] = 100.0
        light_obj["witcher_flicker_strength"] = 0.4
        light_obj["witcher_flicker_position_offset"] = 0.005

        entity = SimpleNamespace(
            isLightOn=True,
            slots=[SimpleNamespace(
                name="fire",
                transform={"X": 0.0, "Y": 0.0, "Z": 0.132597685},
            )],
            cookedEffects=[{
                "name": "fire",
                "length": 7.992495,
                "is_looped": True,
                "particle_systems": [
                    {"path": r"fx\light_sources\candles\candle_smoke_fx4.w2p", "slot": "fire"},
                    {"path": r"fx\light_sources\candles\candle_flame_fx2.w2p", "slot": "fire"},
                ],
            }],
        )

        specs = entity_effects.fire_preview_specs(entity)
        assert len(specs) == 1
        assert specs[0]["slot_name"] == "fire"
        assert specs[0]["particle_path"].endswith("candle_flame_fx2.w2p")

        anchors = entity_effects.import_entity_effect_previews(
            entity,
            owner,
            imported_objects=[light_obj],
            target_collection=collection,
        )
        assert len(anchors) == 1
        anchor = anchors[0]
        assert anchor.parent == owner
        assert abs(anchor.location.z - 0.132597685) < 1e-7
        assert owner[entity_effects.FIRE_ENABLED_PROP] is True
        assert owner[entity_effects.FIRE_EFFECT_PROP] == "fire"
        assert owner[entity_effects.FIRE_PARTICLE_PROP].endswith("candle_flame_fx2.w2p")
        flame_cards = [child for child in anchor.children if child.type == 'MESH']
        assert len(flame_cards) == 2
        assert all(child.get("witcher_fire_sprite_card") for child in flame_cards)
        assert all(len(child.data.polygons) == 1 for child in flame_cards)
        assert all(child.data.uv_layers.active is not None for child in flame_cards)
        assert abs(max(vertex.co.z for vertex in flame_cards[0].data.vertices) - 0.126) < 1e-7
        sprite_material = flame_cards[0].data.materials[0]
        assert sprite_material is not user_material
        assert user_material.node_tree.nodes.get(user_node.name) is user_node
        image_nodes = [node for node in sprite_material.node_tree.nodes if node.type == 'TEX_IMAGE']
        assert len(image_nodes) == 1
        assert image_nodes[0].image is not user_image
        assert tuple(image_nodes[0].image.size) == (96, 192)
        assert image_nodes[0].image.packed_file is not None
        assert image_nodes[0].image.colorspace_settings.name == 'Non-Color'
        assert len(anchor.animation_data.drivers) == 5
        assert len(light_data.animation_data.drivers) == 1
        assert len(light_obj.animation_data.drivers) == 2

        bpy.context.view_layer.objects.active = owner
        owner.select_set(True)
        assert WITCH_PT_ENTITY_EFFECTS_Panel.poll(bpy.context)

        scene.frame_set(1)
        enabled_energy = float(light_data.energy)
        enabled_scale = tuple(anchor.scale)
        scene.frame_set(9)
        assert abs(float(light_data.energy) - enabled_energy) > 1e-4
        assert any(abs(float(anchor.scale[i]) - enabled_scale[i]) > 1e-5 for i in range(3))

        owner[entity_effects.FIRE_ENABLED_PROP] = False
        owner.update_tag()
        scene.frame_set(10)
        bpy.context.view_layer.update()
        assert abs(float(light_data.energy)) < 1e-6
        assert max(abs(float(value)) for value in anchor.scale) < 1e-6

        owner[entity_effects.FIRE_ENABLED_PROP] = True
        owner.update_tag()
        scene.frame_set(11)
        bpy.context.view_layer.update()
        assert float(light_data.energy) > 0.0
        assert max(float(value) for value in anchor.scale) > 0.0
        print("ENTITY_EFFECT_BLENDER_SMOKE_OK")
    finally:
        witcher3_tools.unregister()


if __name__ == "__main__":
    main()
