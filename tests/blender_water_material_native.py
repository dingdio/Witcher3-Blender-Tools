"""Blender-native smoke test for the managed world-water material."""

from __future__ import annotations

import sys
from pathlib import Path

import bpy


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def main() -> None:
    from witcher3_tools.importers import import_w2w

    user_material = bpy.data.materials.new("water_simple_m")
    user_material.use_nodes = True
    user_node = user_material.node_tree.nodes.new("ShaderNodeValue")
    user_node.name = "User Water Node"
    material = import_w2w._ensure_simple_water_material()
    nodes = material.node_tree.nodes

    assert material is not user_material
    assert user_material.node_tree.nodes.get("User Water Node") is user_node
    assert material.get("witcher_world_water_material") is True
    assert material.get("witcher_world_water_version") == 9
    surface = nodes["W3 Water Surface"]
    assert surface.bl_idname == "ShaderNodeBsdfPrincipled"
    assert surface.inputs["Base Color"].links[0].from_node.name == "W3 Water Foam Color"
    assert surface.inputs["Alpha"].links[0].from_node.name == "W3 Water Shore Cut"
    shore_cut = nodes["W3 Water Shore Cut"]
    assert shore_cut.inputs[0].links[0].from_node.name == "W3 Water Final Opacity"
    assert shore_cut.inputs[1].links[0].from_node.name == "W3 Water Shore Mask"
    tint = nodes["W3 Water Shallow Tint"]
    assert tint.inputs[0].links[0].from_node.name == "W3 Water Depth Invert"
    assert nodes["W3 Water Foam Color"].inputs[1].links[0].from_node == tint
    assert abs(nodes["W3 Water Extinction Preview"].outputs[0].default_value - 0.10) < 1e-6
    assert abs(material.get("witcher_water_extinction_preview") - 0.10) < 1e-6
    assert abs(material.get("witcher_water_representative_depth_m") - 0.65) < 1e-6
    assert material.surface_render_method == "BLENDED"
    assert not material.use_transparent_shadow
    assert not material.use_transparency_overlap
    assert surface.inputs["Normal"].links[0].from_node.name == "W3 Water Small Normal"
    assert not nodes["W3 Water Facing"].inputs["Normal"].is_linked
    assert nodes["W3 Water Shallow Mask"].inputs["Fac"].links[0].from_node.name == "W3 Water Depth Mask"
    assert nodes["W3 Water Depth Opacity Mix"].inputs[0].links[0].from_node.name == "W3 Water Depth Distance Fade"
    assert nodes["W3 Water Depth Opacity"].inputs[0].links[0].from_node.name == "W3 Water Opacity"
    assert nodes["W3 Water Final Opacity"].inputs[0].links[0].from_node.name == "W3 Water Depth Opacity"
    assert nodes["W3 Water Foam Gain"].inputs[0].links[0].from_node.name == "W3 Water Foam"
    assert nodes["W3 Water Foam Pattern"].inputs["Value"].links[0].from_node.name == "W3 Water Small Waves"
    assert nodes["W3 Water Shore Foam"].inputs[0].links[0].from_node.name == "W3 Water Shallow Mask"
    combined = nodes["W3 Water Foam Combined"]
    assert combined.inputs[0].links[0].from_node.name == "W3 Water Foam Strength"
    assert not combined.inputs[1].is_linked  # bare build: no foam texture
    assert nodes["W3 Water Foam Color"].inputs[0].links[0].from_node == combined
    assert nodes["W3 Water Wind Wave Gain"].inputs[0].links[0].from_node.name == "W3 Water Wind"
    for label, distance in (("Large", 2000.0), ("Medium", 120.0), ("Small", 50.0)):
        fade = nodes[f"W3 Water {label} Distance Fade"]
        assert abs(fade.inputs["From Max"].default_value - distance) < 1e-6
        band_gain = nodes[f"W3 Water {label} Wind Strength"]
        assert band_gain.inputs[0].links[0].from_node == fade
        assert band_gain.inputs[1].links[0].from_node.name == "W3 Water Wind Wave Gain"
        assert nodes[f"W3 Water {label} Normal"].inputs["Strength"].links[0].from_node == band_gain

    animated_coordinates = nodes["W3 Water Animated Coordinates"]
    for label in ("Large", "Medium", "Small"):
        assert nodes[f"W3 Water {label} Waves"].inputs["Vector"].links[0].from_node == animated_coordinates

    # Rebuild with foam patches and an authored medium normal map.
    foam_img = bpy.data.images.new("test_foam", 8, 8)
    med_img = bpy.data.images.new("test_med_n", 8, 8)
    material = import_w2w._ensure_simple_water_material(
        foam_image=foam_img, medium_normal_image=med_img)
    nodes = material.node_tree.nodes
    surface = nodes["W3 Water Surface"]
    assert material.get("witcher_water_foam_texture") is True
    assert material.get("witcher_water_medium_normal_texture") is True
    combined = nodes["W3 Water Foam Combined"]
    assert combined.inputs[1].links[0].from_node.name == "W3 Water Foam Patches"
    gain = nodes["W3 Water Foam Patch Gain"]
    assert gain.inputs[0].links[0].from_node.name == "W3 Water Foam Patch Shape"
    assert gain.inputs[1].links[0].from_node.name == "W3 Water Foam Wind"
    assert nodes["W3 Water Foam Patch Large"].image == foam_img
    assert nodes["W3 Water Foam Patch Small"].image == foam_img
    product = nodes["W3 Water Foam Patch Product"]
    assert product.inputs[0].links[0].from_node.name == "W3 Water Foam Patch Large"
    assert product.inputs[1].links[0].from_node.name == "W3 Water Foam Patch Small"
    med_nm = nodes["W3 Water Medium Normal Map"]
    assert med_nm.inputs["Color"].links[0].from_node.name == "W3 Water Medium Wave Texture"
    assert med_nm.inputs["Strength"].links[0].from_node.name == "W3 Water Medium Wind Strength"
    assert nodes["W3 Water Medium Wave Texture"].image == med_img
    assert "W3 Water Medium Waves" not in nodes
    assert nodes["W3 Water Large Normal"].inputs["Normal"].links[0].from_node == med_nm
    assert nodes["W3 Water Small Normal"].inputs["Normal"].links[0].from_node.name == "W3 Water Large Normal"
    flow_offset = nodes["W3 Water Flow Offset"]
    assert flow_offset.inputs["Scale"].links[0].from_node.name == "W3 Water Flow"
    time_output = nodes["W3 Water Time"].outputs[0]
    time_path = time_output.path_from_id("default_value")
    drivers = {
        (fcurve.data_path, fcurve.array_index): fcurve.driver
        for fcurve in material.node_tree.animation_data.drivers
    }
    driver = drivers[(time_path, 0)]
    assert set(driver.variables.keys()) == {"fps", "fps_base"}

    scene = bpy.context.scene
    bpy.ops.mesh.primitive_plane_add()
    bpy.context.object.data.materials.append(material)
    bpy.context.object[import_w2w.WORLD_WATER_OBJECT_PROP] = True
    scene.frame_set(1)
    start_time = time_output.default_value
    scene.frame_set(25)
    end_time = time_output.default_value
    assert abs((end_time - start_time) - 1.0) < 1e-6

    from types import SimpleNamespace
    import math

    settings = SimpleNamespace(
        water_wind=0.8,
        water_wind_direction=90.0,
        water_flow_speed=1.5,
        water_foam_intensity=0.9,
        water_reflection=0.5,
        water_clarity=0.3,
        water_level=1.25,
    )
    assert import_w2w.update_world_water_controls(settings) == 1
    assert abs(nodes["W3 Water Wind"].outputs[0].default_value - 0.8) < 1e-6
    assert abs(nodes["W3 Water Flow"].outputs[0].default_value - 1.5) < 1e-6
    assert abs(nodes["W3 Water Foam"].outputs[0].default_value - 0.9) < 1e-6
    direction = nodes["W3 Water Flow Direction"]
    assert abs(direction.inputs["X"].default_value) < 1e-6
    assert abs(direction.inputs["Y"].default_value - 0.002236) < 1e-6
    assert abs(nodes["W3 Water Depth Opacity Scale"].inputs["To Min"].default_value - 0.7) < 1e-6
    assert abs(surface.inputs["Specular IOR Level"].default_value - 0.5) < 1e-6
    assert abs(bpy.context.object.location.z - 1.25) < 1e-6

    print("WATER_MATERIAL_BLENDER_SMOKE_OK")


if __name__ == "__main__":
    main()
