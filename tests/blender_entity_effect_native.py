"""Blender-native smoke test for cooked entity flame and fountain previews.

Run with:
  blender --background --factory-startup --python tests/blender_entity_effect_native.py
"""

from __future__ import annotations

import json
from math import radians
from pathlib import Path
import sys
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import bpy  # noqa: E402
import witcher3_tools  # noqa: E402


class LayoutRecorder:
    def __init__(self):
        self.properties = []

    def box(self):
        return self

    def label(self, **_kwargs):
        pass

    def prop(self, _owner, name, **_kwargs):
        self.properties.append(name)


def main() -> None:
    witcher3_tools.register()
    try:
        from witcher3_tools.importers import entity_effects, import_particle
        from witcher3_tools.ui.ui_entity import WITCH_PT_ENTITY_EFFECTS_Panel

        scene = bpy.context.scene
        collection = scene.collection
        preview_calls = []

        def fake_particle_preview(source, *, parent, target_collection, **_kwargs):
            preview_calls.append(source)
            basename = source.replace("/", "\\").rsplit("\\", 1)[-1]
            emitter_counts = {
                "candle_flame_fx2.w2p": (("flame", 2), ("flame burst", 2)),
                "brazier_fire.w2p": (("smoke_vertex", 2), ("fire_anim", 1), ("embers", 2)),
                "p_splash_circle.w2p": (("splash", 10), ("rings", 16)),
                "p_fountain_tip.w2p": (("splash_tip", 15),),
            }[basename]
            created = []
            for emitter, count in emitter_counts:
                mesh = bpy.data.meshes.new(f"{emitter} Test Quad")
                mesh.from_pydata(
                    [(-1.0, -1.0, 0.0), (1.0, -1.0, 0.0), (1.0, 1.0, 0.0), (-1.0, 1.0, 0.0)],
                    [],
                    [(0, 1, 2, 3)],
                )
                for index in range(count):
                    obj = bpy.data.objects.new(f"{emitter}_{index + 1:02d}", mesh)
                    target_collection.objects.link(obj)
                    obj.parent = parent
                    obj["witcher_particle_preview"] = True
                    obj["witcher_particle_emitter"] = emitter
                    obj["witcher_particle_system"] = source
                    created.append(obj)
            return created

        entity_effects._import_particle_preview = fake_particle_preview
        owner = bpy.data.objects.new("Candle Entity", None)
        collection.objects.link(owner)
        slots_parent = bpy.data.objects.new("Candle Entity Slots", None)
        collection.objects.link(slots_parent)
        slots_parent.parent = owner
        slots_parent["witcher_slots_parent"] = True
        fire_slot = bpy.data.objects.new("Candle Fire Slot", None)
        collection.objects.link(fire_slot)
        fire_slot.parent = slots_parent
        fire_slot["witcher_slot_name"] = "fire"
        fire_slot.location.z = 0.132597685

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
                "loop_start": 0.368729,
                "loop_end": 0.642023,
                "is_looped": True,
                "particle_systems": [
                    {
                        "path": r"fx\light_sources\candles\candle_sparks_fx1.w2p",
                        "slot": "fire",
                        "time_begin": 0.742660,
                        "duration": 1.383556,
                    },
                    {
                        "path": r"fx\light_sources\candles\candle_smoke_fx4.w2p",
                        "slot": "fire",
                        "time_begin": 0.693868,
                        "duration": 7.192057,
                    },
                    {
                        "path": r"fx\light_sources\candles\candle_flame_fx2.w2p",
                        "slot": "fire",
                        "time_begin": 0.0,
                        "duration": 0.953704,
                    },
                ],
            }],
        )

        specs = entity_effects.fire_preview_specs(entity)
        assert len(specs) == 1
        assert specs[0]["slot_name"] == "fire"
        assert specs[0]["particle_path"].endswith("candle_flame_fx2.w2p")
        untimed_effect = dict(entity.cookedEffects[0])
        untimed_effect["particle_systems"] = [
            {"path": item["path"], "slot": item["slot"]}
            for item in entity.cookedEffects[0]["particle_systems"]
        ]
        assert len(entity_effects.fire_preview_specs(SimpleNamespace(
            slots=entity.slots,
            cookedEffects=[untimed_effect],
        ))) == 3

        anchors = entity_effects.import_entity_effect_previews(
            entity,
            owner,
            imported_objects=[light_obj],
            target_collection=collection,
        )
        assert len(anchors) == 1
        anchor = anchors[0]
        assert anchor.parent == fire_slot
        assert max(abs(float(value)) for value in anchor.location) < 1e-7
        bpy.context.view_layer.update()
        assert abs(anchor.matrix_world.translation.z - 0.132597685) < 1e-7
        assert owner[entity_effects.FIRE_ENABLED_PROP] is True
        assert owner[entity_effects.FIRE_EFFECT_PROP] == "fire"
        assert owner[entity_effects.FIRE_PARTICLE_PROP].endswith("candle_flame_fx2.w2p")
        flame_particles = [child for child in anchor.children if child.type == 'MESH']
        assert len(flame_particles) == 4
        assert all(child.get("witcher_particle_preview") for child in flame_particles)
        assert all(child.get("witcher_effect_preview_child") for child in flame_particles)
        assert anchor["witcher_particle_emitters"] == '["flame","flame burst"]'
        assert json.loads(anchor["witcher_particle_systems"]) == [
            r"fx\light_sources\candles\candle_flame_fx2.w2p"
        ]
        assert preview_calls == [r"fx\light_sources\candles\candle_flame_fx2.w2p"]
        assert len(anchor.animation_data.drivers) == 3
        assert len(light_data.animation_data.drivers) == 1
        assert len(light_obj.animation_data.drivers) == 3

        bpy.context.view_layer.objects.active = owner
        owner.select_set(True)
        assert WITCH_PT_ENTITY_EFFECTS_Panel.poll(bpy.context)

        scene.frame_set(1)
        enabled_energy = float(light_data.energy)
        scene.frame_set(9)
        assert abs(float(light_data.energy) - enabled_energy) > 1e-4

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

        reimported = entity_effects.import_entity_effect_previews(
            entity,
            owner,
            imported_objects=[light_obj],
            target_collection=collection,
        )
        assert reimported == [anchor]
        assert preview_calls == [r"fx\light_sources\candles\candle_flame_fx2.w2p"]

        brazier_owner = bpy.data.objects.new("Beauclair Brazier", None)
        collection.objects.link(brazier_owner)
        brazier_entity = SimpleNamespace(
            isLightOn=True,
            slots=[
                SimpleNamespace(
                    name="fire_fx1",
                    transform={
                        "X": 0.014223,
                        "Y": -0.030086,
                        "Z": 1.114114,
                        "Yaw": 12.0,
                        "Pitch": 23.0,
                        "Roll": 34.0,
                    },
                ),
                SimpleNamespace(
                    name="light_point",
                    transform={"X": 0.0, "Y": 0.0, "Z": 1.415564},
                ),
            ],
            cookedEffects=[{
                "name": "fire",
                "length": 6.986434,
                "loop_start": 0.581628,
                "loop_end": 0.800918,
                "is_looped": True,
                "particle_systems": [
                    {
                        "path": r"dlc\bob\data\fx\gameplay\light_sources\fire_interactive_fx2.w2p",
                        "slot": "light_point",
                        "time_begin": 0.805168,
                        "duration": 5.499282,
                    },
                    {
                        "path": r"dlc\bob\data\fx\gameplay\light_sources\brazier_fire.w2p",
                        "slot": "fire_fx1",
                        "time_begin": 0.027042,
                        "duration": 5.858314,
                    },
                ],
            }],
        )

        brazier_specs = entity_effects.fire_preview_specs(brazier_entity)
        assert len(brazier_specs) == 1
        assert brazier_specs[0]["slot_name"] == "fire_fx1"
        assert brazier_specs[0]["particle_path"].endswith("brazier_fire.w2p")
        brazier_anchors = entity_effects.import_entity_effect_previews(
            brazier_entity,
            brazier_owner,
            target_collection=collection,
        )
        assert len(brazier_anchors) == 1
        brazier_anchor = brazier_anchors[0]
        assert brazier_anchor["witcher_effect_slot"] == "fire_fx1"
        assert abs(brazier_anchor.location.z - 1.114114) < 1e-7
        assert brazier_anchor.rotation_mode == 'YXZ'
        assert all(
            abs(float(actual) - radians(expected)) < 1e-7
            for actual, expected in zip(brazier_anchor.rotation_euler, (12.0, 23.0, 34.0))
        )
        assert brazier_anchor["witcher_particle_emitters"] == '["smoke_vertex","fire_anim","embers"]'
        assert preview_calls[-1].endswith("brazier_fire.w2p")
        assert not any(path.endswith("fire_interactive_fx2.w2p") for path in preview_calls)

        fountain_owner = bpy.data.objects.new("Wyzima Castle Fountain", None)
        collection.objects.link(fountain_owner)
        fountain_entity = SimpleNamespace(
            slots=[
                SimpleNamespace(
                    name="down_splash",
                    transform={"X": 0.0, "Y": 0.0, "Z": 0.45},
                ),
                SimpleNamespace(
                    name="fountain_tip",
                    transform={"X": 0.0, "Y": 0.0, "Z": 1.767488956},
                ),
            ],
            cookedEffects=[{
                "name": "fountain_water",
                "length": 0.0,
                "is_looped": True,
                "particle_systems": [
                    {
                        "path": r"fx\water\water_fountain\p_splash_circle.w2p",
                        "slot": "down_splash",
                    },
                    {
                        "path": r"fx\water\water_fountain\p_fountain_tip.w2p",
                        "slot": "fountain_tip",
                    },
                ],
            }],
        )

        fountain_specs = entity_effects.fountain_preview_specs(fountain_entity)
        assert len(fountain_specs) == 2
        assert {(spec["kind"], spec["slot_name"]) for spec in fountain_specs} == {
            ("impact", "down_splash"),
            ("tip", "fountain_tip"),
        }

        preview_calls.clear()
        fountain_anchors = entity_effects.import_entity_effect_previews(
            fountain_entity,
            fountain_owner,
            target_collection=collection,
        )
        assert len(fountain_anchors) == 2
        by_kind = {item["witcher_water_preview_kind"]: item for item in fountain_anchors}
        impact_anchor = by_kind["impact"]
        tip_anchor = by_kind["tip"]
        assert impact_anchor.parent == fountain_owner
        assert tip_anchor.parent == fountain_owner
        assert abs(impact_anchor.location.z - 0.45) < 1e-7
        assert abs(tip_anchor.location.z - 1.767488956) < 1e-7
        assert fountain_owner[entity_effects.WATER_ENABLED_PROP] is True
        assert fountain_owner[entity_effects.WATER_EFFECT_PROP] == "fountain_water"
        assert "p_splash_circle.w2p" in fountain_owner[entity_effects.WATER_PARTICLES_PROP]
        assert "p_fountain_tip.w2p" in fountain_owner[entity_effects.WATER_PARTICLES_PROP]
        assert entity_effects.FIRE_ENABLED_PROP not in fountain_owner
        bpy.context.view_layer.objects.active = fountain_owner
        fountain_owner.select_set(True)
        assert WITCH_PT_ENTITY_EFFECTS_Panel.poll(bpy.context)
        panel_layout = LayoutRecorder()
        WITCH_PT_ENTITY_EFFECTS_Panel.draw(SimpleNamespace(layout=panel_layout), bpy.context)
        assert '["witcher_water_fx_enabled"]' in panel_layout.properties
        assert '["witcher_water_fx_effect"]' in panel_layout.properties
        assert '["witcher_water_fx_particles"]' in panel_layout.properties

        impact_children = [child for child in impact_anchor.children if child.type == 'MESH']
        tip_children = [child for child in tip_anchor.children if child.type == 'MESH']
        impact_rings = [child for child in impact_children if child["witcher_particle_emitter"] == "rings"]
        assert len(impact_children) == 26
        assert len(impact_rings) == 16
        assert len(tip_children) == 15
        assert all(child.get("witcher_effect_preview_child") for child in impact_children + tip_children)
        assert impact_anchor["witcher_particle_emitters"] == '["splash","rings"]'
        assert tip_anchor["witcher_particle_emitters"] == '["splash_tip"]'
        assert all(
            anchor["witcher_particle_preview_version"] == import_particle.PARTICLE_PREVIEW_VERSION
            for anchor in fountain_anchors
        )
        assert preview_calls == [
            r"fx\water\water_fountain\p_splash_circle.w2p",
            r"fx\water\water_fountain\p_fountain_tip.w2p",
        ]

        fountain_owner[entity_effects.WATER_ENABLED_PROP] = False
        fountain_owner.update_tag()
        scene.frame_set(21)
        bpy.context.view_layer.update()
        assert all(max(abs(float(value)) for value in anchor.scale) < 1e-6 for anchor in fountain_anchors)

        fountain_owner[entity_effects.WATER_ENABLED_PROP] = True
        fountain_owner.update_tag()
        scene.frame_set(22)
        bpy.context.view_layer.update()
        assert all(max(float(value) for value in anchor.scale) > 0.0 for anchor in fountain_anchors)

        reimported = entity_effects.import_entity_effect_previews(
            fountain_entity,
            fountain_owner,
            target_collection=collection,
        )
        assert set(reimported) == set(fountain_anchors)

        stale_anchor = bpy.data.objects.new("Stale Fountain Preview", None)
        collection.objects.link(stale_anchor)
        stale_anchor.parent = fountain_owner
        stale_anchor["witcher_type"] = "CFXFountainPreview"
        stale_anchor["witcher_water_preview_kind"] = "tip"
        stale_anchor["witcher_effect_preview_key"] = "fountain|stale|tip|stale.w2p"
        stale_anchor["witcher_water_preview_version"] = entity_effects.WATER_PREVIEW_VERSION - 1
        stale_child = bpy.data.objects.new("Stale Fountain Preview Child", None)
        collection.objects.link(stale_child)
        stale_child.parent = stale_anchor
        stale_child["witcher_effect_preview_child"] = True
        stale_basis = bpy.data.objects.new("Stale Fountain Billboard Basis", None)
        collection.objects.link(stale_basis)
        stale_basis.parent = stale_anchor
        stale_basis["witcher_particle_billboard_basis"] = True
        stale_anchor_name = stale_anchor.name
        stale_child_name = stale_child.name
        stale_basis_name = stale_basis.name
        reimported = entity_effects.import_entity_effect_previews(
            fountain_entity,
            fountain_owner,
            target_collection=collection,
        )
        assert set(reimported) == set(fountain_anchors)
        assert stale_anchor_name not in bpy.data.objects
        assert stale_child_name not in bpy.data.objects
        assert stale_basis_name not in bpy.data.objects
        print("ENTITY_EFFECT_BLENDER_SMOKE_OK")
    finally:
        witcher3_tools.unregister()


if __name__ == "__main__":
    main()
