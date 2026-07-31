"""Run with Blender: --background --factory-startup --python tests/blender_entity_light_native.py"""

from __future__ import annotations

from math import isclose, pi, radians
import os
from pathlib import Path
import sys
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import bpy  # noqa: E402
from mathutils import Vector  # noqa: E402
import witcher3_tools  # noqa: E402


def _close(actual, expected, tolerance=1.0e-6):
    assert isclose(float(actual), float(expected), rel_tol=tolerance, abs_tol=tolerance), (actual, expected)


def main():
    witcher3_tools.register()
    try:
        from witcher3_tools.CR2W.fast_cache_scan import scan_dependency_file
        from witcher3_tools.importers import import_blender_fun, import_entity
        from witcher3_tools.importers import entity_light
        from witcher3_tools.importers.entity_light import apply_environment_light_groups
        from witcher3_tools.ui import ui_map

        assert ui_map._WORLD_LAYER_SCAN_CACHE_VERSION == 15
        assert import_blender_fun.CACHED_LAYER_TRANSFORM_MODE_VERSION == 11
        entity_light.unregister_driver_namespace()
        assert "witcher_light_double_cross" not in bpy.app.driver_namespace
        assert not entity_light._driver_namespace_handlers()
        entity_light.register_driver_namespace()
        assert len(entity_light._driver_namespace_handlers()) == 1
        bpy.app.driver_namespace.pop("witcher_light_double_cross", None)
        entity_light._restore_driver_namespace_on_load()
        assert callable(bpy.app.driver_namespace["witcher_light_double_cross"])

        point = import_entity._import_light_component({
            "type": "CPointLightComponent",
            "name": "Native Point",
            "radius": 1.5,
            "brightness": 10.0,
            "attenuation": 0.8,
            "color": {"Red": 255, "Green": 220, "Blue": 200},
            "envColorGroup": "ECG_FX_FireLight",
            "lightUsageMask": "LUM_IsExteriorOnly",
            "lightFlickering": {"flickerStrength": 0.4, "positionOffset": 0.005},
        })
        assert point.data.type == "POINT"
        assert not point.data.use_shadow
        assert point.data.use_custom_distance
        _close(point.data.cutoff_distance, 1.5)
        _close(point.data.shadow_soft_size, 1.0 / 0.8)
        _close(point["witcher_light_base_energy"], 4.0 * pi * 10.0 / (0.8 * 0.8))
        _close(point.data.energy, 4.0 * pi * 10.0 / (0.8 * 0.8) * 0.8)
        _close(point["witcher_flicker_period"], 0.2)
        assert point["witcher_light_eevee_attenuation_compat"]
        assert point["witcher_light_receiver_mask_unsupported"]
        assert len(point.data.animation_data.drivers) == 1
        assert len(point.animation_data.drivers) == 3

        base_energy = float(point["witcher_base_energy"])
        energy_samples = []
        position_samples = []
        for frame in range(0, 97):
            bpy.context.scene.frame_set(frame)
            energy_samples.append(float(point.data.energy))
            position_samples.append(tuple(float(value) for value in point.delta_location))
        assert min(energy_samples) >= base_energy * 0.6 - 1.0e-4
        assert max(energy_samples) <= base_energy + 1.0e-4
        assert max(energy_samples) - min(energy_samples) > base_energy * 0.1
        assert max(abs(value[0]) for value in position_samples) > 0.004
        assert all(abs(value[2] - 0.5 * value[0]) < 1.0e-7 for value in position_samples)
        _close(position_samples[0][0], position_samples[96][0])
        bpy.context.scene.render.fps = 30
        bpy.context.scene.frame_set(15)
        _close(point.data.energy, energy_samples[12])
        bpy.context.scene.frame_set(0)
        position_at_zero = tuple(float(value) for value in point.delta_location)
        bpy.context.scene.frame_set(120)
        position_after_four_seconds = tuple(float(value) for value in point.delta_location)
        assert all(
            abs(first - second) < 1.0e-7
            for first, second in zip(position_at_zero, position_after_four_seconds)
        )
        bpy.context.scene.render.fps = 24

        wide_spot = import_entity._import_light_component({
            "type": "CSpotLightComponent",
            "name": "Native Wide Spot",
            "brightness": 50.0,
            "attenuation": 0.7,
            "color": {"Red": 255, "Green": 250, "Blue": 245},
            "envColorGroup": "ECG_FX_FireLight",
            "innerAngle": 250.0,
            "outerAngle": 350.0,
            "lightFlickering": {
                "flickerStrength": 0.2,
                "flickerPeriod": 0.1,
                "positionOffset": 0.01,
            },
        })
        assert wide_spot.data.type == "POINT"
        assert wide_spot["witcher_light_wide_spot_approximation"]
        assert not wide_spot.data.use_shadow
        _close(wide_spot.data.cutoff_distance, 5.0)
        _close(wide_spot.data.shadow_soft_size, 1.0 / 0.7)
        assert len(wide_spot.data.animation_data.drivers) == 1
        assert len(wide_spot.animation_data.drivers) == 3

        spot = import_entity._import_light_component({
            "type": "CSpotLightComponent",
            "name": "Native Spot",
            "shadowCastingMode": "LSCM_Normal",
            "innerAngle": 60.0,
            "outerAngle": 90.0,
        })
        assert spot.data.type == "SPOT"
        assert spot.data.use_shadow
        _close(spot.data.spot_size, radians(90.0))
        _close(spot.data.spot_blend, 1.0 / 3.0)
        direction = spot.matrix_world.to_3x3() @ Vector((0.0, 0.0, -1.0))
        _close(direction.x, 0.0)
        _close(direction.y, 1.0)
        _close(direction.z, 0.0)

        disabled = import_entity._import_light_component({
            "type": "CPointLightComponent",
            "name": "Disabled Point",
            "isEnabled": False,
        })
        assert disabled.hide_render
        assert disabled.hide_viewport
        excluded = import_entity._import_light_component({
            "type": "CPointLightComponent",
            "name": "Excluded Point",
            "lightUsageMask": "LUM_ExcludeFromSceneRender",
        })
        assert excluded.hide_render
        assert not excluded.hide_viewport
        offset_without_flicker = import_entity._import_light_component({
            "type": "CPointLightComponent",
            "name": "Static Offset Point",
            "lightFlickering": {"flickerStrength": 0.0, "positionOffset": 0.1},
        })
        assert offset_without_flicker.animation_data is None

        fire_group = (1.497111322032124, 0.7104160798982903, 0.15649878900546316)
        apply_environment_light_groups(
            bpy.context.scene,
            {"ECG_FX_FireLight": fire_group},
        )
        _close(wide_spot["witcher_base_energy"], 4.0 * pi * 50.0 / (0.7 * 0.7) * fire_group[0])
        _close(wide_spot.data.color[0], 1.0)
        _close(wide_spot.data.color[1], 0.456098186, tolerance=1.0e-5)
        _close(wide_spot.data.color[2], 0.096495863, tolerance=1.0e-5)
        assert wide_spot.data.energy >= wide_spot["witcher_base_energy"] * 0.8 - 1.0e-4
        assert wide_spot.data.energy <= wide_spot["witcher_base_energy"] + 1.0e-4

        cached_spot = import_blender_fun._import_cached_plan_light_item(
            {
                "name": "Cached Wide Spot",
                "component_type": "CSpotLightComponent",
                "brightness": 50.0,
                "attenuation": 0.7,
                "color": {"Red": 255, "Green": 250, "Blue": 245},
                "envColorGroup": "ECG_FX_FireLight",
                "innerAngle": 250.0,
                "outerAngle": 350.0,
                "lightFlickering": {
                    "flickerStrength": 0.2,
                    "flickerPeriod": 0.1,
                    "positionOffset": 0.01,
                },
            },
            "component_spot_light",
            bpy.context.scene.collection,
            None,
            "",
            "cached_spot",
            "test",
        )
        assert cached_spot.data.type == "POINT"
        assert not cached_spot.data.use_shadow
        _close(cached_spot.data.cutoff_distance, 5.0)
        _close(cached_spot["witcher_base_energy"], 4.0 * pi * 50.0 / (0.7 * 0.7) * fire_group[0])
        assert len(cached_spot.data.animation_data.drivers) == 1
        assert len(cached_spot.animation_data.drivers) == 3

        parsed_props = {
            "name": SimpleNamespace(ToString=lambda: "Parsed Spot"),
            "radius": SimpleNamespace(theType="Float", Value=4.0),
            "brightness": SimpleNamespace(theType="Float", Value=25.0),
            "attenuation": SimpleNamespace(theType="Float", Value=0.5),
            "color": SimpleNamespace(
                theType="Color",
                More=[
                    SimpleNamespace(theName="Red", theType="Uint8", Value=255),
                    SimpleNamespace(theName="Green", theType="Uint8", Value=128),
                    SimpleNamespace(theName="Blue", theType="Uint8", Value=64),
                ],
            ),
            "shadowCastingMode": SimpleNamespace(strings=["LSCM_Normal"]),
            "isEnabled": SimpleNamespace(theType="Bool", Value=True),
            "innerAngle": SimpleNamespace(theType="Float", Value=30.0),
            "outerAngle": SimpleNamespace(theType="Float", Value=60.0),
            "softness": SimpleNamespace(theType="Float", Value=1.0),
            "transform": None,
        }
        parsed_component = SimpleNamespace(
            name="CSpotLightComponent",
            GetVariableByName=parsed_props.get,
        )
        full_plan = import_blender_fun._new_level_import_plan()
        import_blender_fun._resolve_component_import_plan(
            full_plan,
            parsed_component,
            "entity",
        )
        parsed_item = full_plan["items"][0]
        assert parsed_item["name"] == "Parsed Spot"
        assert parsed_item["component_type"] == "CSpotLightComponent"
        _close(parsed_item["brightness"], 25.0)
        _close(parsed_item["outerAngle"], 60.0)
        assert parsed_item["color"] == {"Red": 255, "Green": 128, "Blue": 64}

        parsed_spot = import_blender_fun.import_single_component(parsed_component, None)
        assert parsed_spot.data.type == "SPOT"
        assert parsed_spot.data.use_shadow
        _close(parsed_spot.data.cutoff_distance, 4.0)
        _close(parsed_spot["witcher_light_base_energy"], 4.0 * pi * 25.0)
        _close(parsed_spot.data.spot_size, radians(60.0))
        _close(parsed_spot.data.spot_blend, 0.5)

        mesh_data = bpy.data.meshes.new("Drawable Flags")
        mesh_obj = bpy.data.objects.new("Drawable Flags", mesh_data)
        bpy.context.scene.collection.objects.link(mesh_obj)
        import_entity._apply_drawable_shadow_flags(mesh_obj, None)
        assert not mesh_obj.visible_shadow
        import_entity._apply_drawable_shadow_flags(mesh_obj, 3)
        assert mesh_obj.visible_shadow
        import_entity._apply_drawable_shadow_flags(mesh_obj, 1025)
        assert mesh_obj.visible_shadow
        assert mesh_obj["witcher_drawableFlags_local_lights_only"]
        import_entity._apply_drawable_shadow_flags(
            mesh_obj,
            None,
            "CDestructionComponent",
        )
        assert mesh_obj.visible_shadow
        import_entity._apply_drawable_shadow_flags(
            mesh_obj,
            "DF_IsVisible|DF_CastShadows|DF_ClimbBlock",
        )
        assert mesh_obj.visible_shadow

        import_blender_fun._tag_object_tree_drawable_shadows(mesh_obj, None)
        assert not mesh_obj.visible_shadow
        import_blender_fun._tag_object_tree_drawable_shadows(
            mesh_obj,
            "DF_IsVisible|DF_CastShadowsFromLocalLightsOnly",
        )
        assert mesh_obj.visible_shadow
        assert mesh_obj["witcher_drawableFlags_local_lights_only"]
        import_blender_fun._tag_object_tree_drawable_shadows(
            mesh_obj,
            ["DF_IsVisible", "DF_CastShadows"],
        )
        assert mesh_obj.visible_shadow

        source_path = str(os.environ.get("WITCHER_TEST_BRAZIER_ENTITY", "") or "").strip()
        if source_path:
            scan = scan_dependency_file(source_path)
            assert scan and scan["complete"]
            entity = scan["entities"][0]
            components = {item["kind"]: item for item in entity["components"]}
            _close(components["component_spot_light"]["outerAngle"], 350.0)
            _close(components["component_spot_light"]["brightness"], 50.0)
            _close(components["component_point_light"]["radius"], 1.5)
            _close(components["component_point_light"]["brightness"], 10.0)
            meshes = {item["component_name"]: item for item in entity["stream_items"]}
            assert "DF_CastShadows" in meshes["CStaticMeshComponent0"]["drawable_flags"]
            assert meshes["brazier_fuel"]["drawable_flags"] is None
        print("ENTITY_LIGHT_NATIVE_BLENDER_OK")
    finally:
        witcher3_tools.unregister()


if __name__ == "__main__":
    main()
