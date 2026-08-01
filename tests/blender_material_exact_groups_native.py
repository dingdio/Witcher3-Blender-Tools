from __future__ import annotations

import sys
from pathlib import Path

import bpy


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def main() -> None:
    from witcher3_tools.materials import material as material_module
    from witcher3_tools.materials.nodes.domain import refresh_witcher_include_state

    def reaches(tree, source, target):
        pending, seen = [source], set()
        while pending:
            node = pending.pop()
            if node == target:
                return True
            if node in seen:
                continue
            seen.add(node)
            pending.extend(link.to_node for output in node.outputs for link in output.links)
        return False

    for name in ("pbr_hair_moving", "pbr_hair_simple"):
        tree = material_module.ensure_node_group(name)
        mix = tree.nodes["Mix"]
        color_result = next(output for output in mix.outputs if output.identifier == "Result_Color")
        vibrance_color = tree.nodes["Group.001"].inputs["Color"]
        assert any(link.from_socket == color_result for link in vibrance_color.links)

    assert material_module._is_srgb_texture_param("p_diffuse", "p_diffuse")
    assert material_module._is_srgb_texture_param("specular", "specular")

    pattern_tree = material_module.ensure_node_group("pbr_pattern_normal_spec")
    assert [
        item.name
        for item in pattern_tree.interface.items_tree
        if getattr(item, "item_type", "") == "SOCKET" and item.in_out == "OUTPUT"
    ] == ["Cycles", "Eevee"]
    assert pattern_tree.nodes["__W3_RK_Overlay"].blend_type == "OVERLAY"
    assert pattern_tree.nodes["__W3_RK_Overlay"].inputs[0].default_value == 1.0
    for name, gamma in (
        ("__W3_RK_DiffuseGamma", 1 / 2.2),
        ("__W3_RK_PatternGamma", 1 / 2.2),
        ("__W3_RK_OverlayLinear", 2.2),
    ):
        assert abs(pattern_tree.nodes[name].inputs["Gamma"].default_value - gamma) < 1e-5
    assert any(
        link.from_socket.name == "p_normal_alpha"
        and link.to_node == pattern_tree.nodes["__W3_RK_RoughF"]
        for link in pattern_tree.links
    )
    assert any(
        link.from_socket.name == "Roughness"
        and link.to_node == pattern_tree.nodes["__W3_RK_RoughPow"]
        for link in pattern_tree.links
    )
    group_output = next(node for node in pattern_tree.nodes if node.type == "GROUP_OUTPUT")
    assert group_output.inputs["Cycles"].links[0].from_node.name == "__W3_RK_CyclesBSDF"
    assert group_output.inputs["Eevee"].links[0].from_node.name == "__W3_RK_BSDF"

    mat = bpy.data.materials.new("W3 Pattern UV Test")
    mat.use_nodes = True
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    nodes.clear()
    graph = nodes.new("ShaderNodeGroup")
    graph.node_tree = pattern_tree
    output = nodes.new("ShaderNodeOutputMaterial")
    links.new(graph.outputs["Cycles"], output.inputs["Surface"])

    textures = []
    for pin in ("p_diffuse", "p_normal", "specular"):
        texture = nodes.new("ShaderNodeTexImage")
        texture.name = pin
        texture.image = bpy.data.images.new(f"{pin}_test", 1, 1)
        links.new(texture.outputs["Color"], graph.inputs[pin])
        textures.append(texture)

    tile = nodes.new("ShaderNodeCombineXYZ")
    rotation = nodes.new("ShaderNodeValue")
    links.new(tile.outputs["Vector"], graph.inputs["p_tile"])
    material_module.reconcile_w3_pattern_uv_links(mat, graph)

    transform = nodes["__W3_PatternUVTransform"]
    assert transform.node_tree.name == "W3 Pattern UV Transform"
    assert transform.inputs["Tile"].links[0].from_node == tile
    links.new(rotation.outputs["Value"], graph.inputs["p_rotation"])
    refresh_witcher_include_state(mat)
    assert transform.inputs["Rotation"].links[0].from_node == rotation
    assert all(texture.inputs["Vector"].links[0].from_node == transform for texture in textures)

    vert_tree = material_module.ensure_node_group("pbr_vert_blend_colorize")
    vert_inputs = {
        item.name
        for item in vert_tree.interface.items_tree
        if getattr(item, "item_type", "") == "SOCKET" and item.in_out == "INPUT"
    }
    assert {"DiffuseArray", "NormalArray", "PatternTexture_alpha"} <= vert_inputs
    assert vert_tree.nodes["Color Attribute"].layer_name == "W3RawColor"
    assert vert_tree.nodes["__W3_RK_VertBlendLayerScale"].inputs[1].default_value == 16.0
    assert vert_tree.nodes["Reroute.013"].inputs[0].links[0].from_node.name == "__W3_RK_VertBlendPatternMix"

    vert_mat = bpy.data.materials.new("W3 Vertex Blend UV Test")
    vert_mat.use_nodes = True
    vert_nodes, vert_links = vert_mat.node_tree.nodes, vert_mat.node_tree.links
    vert_nodes.clear()
    vert_graph = vert_nodes.new("ShaderNodeGroup")
    vert_graph.node_tree = vert_tree
    for pin in ("PatternTexture", "PatternMask"):
        texture = vert_nodes.new("ShaderNodeTexImage")
        texture.name = pin
        texture.image = bpy.data.images.new(f"{pin}_test", 1, 1)
        vert_links.new(texture.outputs["Color"], vert_graph.inputs[pin])
    scale = vert_nodes.new("ShaderNodeValue")
    offset = vert_nodes.new("ShaderNodeCombineXYZ")
    vert_links.new(scale.outputs["Value"], vert_graph.inputs["PatternUVScale"])
    vert_links.new(offset.outputs["Vector"], vert_graph.inputs["PatternUVOffset"])
    material_module.reconcile_w3_pattern_uv_links(vert_mat, vert_graph)
    assert vert_nodes["__W3_VertPatternUV"].uv_map == "SecondUV"
    assert vert_nodes["PatternTexture"].inputs["Vector"].links[0].from_node.name == "__W3_VertPatternOffset"
    assert vert_nodes["__W3_VertPatternMaskUV"].uv_map == "DiffuseUV"

    wear_tree = material_module.ensure_node_group("pbr_std_wear_paint")
    assert wear_tree.nodes["W3 Wear Raw Vertex Color"].layer_name == "W3RawColor"
    assert wear_tree.nodes["Group"].inputs["Diffuse"].links[0].from_node.name == "W3 Wear Apply AO"
    assert wear_tree.nodes["W3 Wear Apply Paint"].inputs[0].is_linked
    assert wear_tree.nodes["W3 Wear Apply Wear"].inputs[0].is_linked

    fountain_tree = material_module.ensure_node_group("m_fountain_cascade")
    fountain_inputs = {
        item.name
        for item in fountain_tree.interface.items_tree
        if getattr(item, "item_type", "") == "SOCKET" and item.in_out == "INPUT"
    }
    assert {
        "normal_and_splash", "normal_and_splash_alpha", "cubemap",
        "texture_speed", "texture_coord", "fountain_uv",
    } <= fountain_inputs
    assert [
        (item.name, item.socket_type)
        for item in fountain_tree.interface.items_tree
        if getattr(item, "item_type", "") == "SOCKET" and item.in_out == "INPUT"
    ] == [
        ("normal_and_splash", "NodeSocketColor"),
        ("normal_and_splash_alpha", "NodeSocketFloat"),
        ("cubemap", "NodeSocketColor"),
        ("texture_speed", "NodeSocketVector"),
        ("texture_coord", "NodeSocketVector"),
        ("normal_multiplier", "NodeSocketVector"),
        ("alpha_ramp_a", "NodeSocketFloat"),
        ("alpha_ramp_b", "NodeSocketFloat"),
        ("alpha_multiplier", "NodeSocketFloat"),
        ("reflection_power_exponent", "NodeSocketFloat"),
        ("reflection_multiplier", "NodeSocketFloat"),
        ("soft_alpha", "NodeSocketFloat"),
        ("refraction_multiplier", "NodeSocketFloat"),
        ("fountain_uv", "NodeSocketVector"),
    ]
    assert fountain_tree["witcher_material_graph_version"] == 6
    assert fountain_tree.nodes["Texture Alpha Sharpen"].operation == "POWER"
    assert fountain_tree.nodes["Texture Alpha Sharpen"].inputs[1].default_value == 8.0
    assert abs(fountain_tree.nodes["Sparse Alpha Gain"].inputs[1].default_value - 0.035) < 1e-6
    assert abs(fountain_tree.nodes["Veil Coverage"].inputs[1].default_value - 0.00002) < 1e-9
    assert fountain_tree.nodes["Additive Water"].inputs["Strength"].default_value == 1200.0
    assert fountain_tree.nodes["Fountain Output"].inputs["Shader"].links[0].from_node.name == "Cascade Visibility"
    assert fountain_tree.nodes["Cascade Visibility"].inputs[2].links[0].from_node.name == "Additive Water"
    refraction_fresnel = fountain_tree.nodes["Refraction Fresnel"]
    refraction_fresnel_weight = fountain_tree.nodes["Refraction Fresnel Weight"]
    refraction_tint = fountain_tree.nodes["Refraction Tint"]
    combined_color = fountain_tree.nodes["Water + Refraction Tint"]
    assert refraction_fresnel.inputs["Normal"].links[0].from_node.name == "Waterfall Normal"
    assert refraction_fresnel_weight.operation == "MULTIPLY"
    assert {socket.links[0].from_node.name for socket in refraction_fresnel_weight.inputs[:2]} == {
        "Refraction Fresnel", "Refraction Weight",
    }
    assert refraction_tint.operation == "SCALE"
    assert refraction_tint.inputs["Scale"].links[0].from_node == refraction_fresnel_weight
    assert combined_color.operation == "ADD"
    assert {socket.links[0].from_node.name for socket in combined_color.inputs[:2]} == {
        "Water Tint", "Refraction Tint",
    }
    assert fountain_tree.nodes["Additive Water"].inputs["Color"].links[0].from_node == combined_color
    assert reaches(fountain_tree, fountain_tree.nodes["Additive Water"], fountain_tree.nodes["Fountain Output"])
    assert reaches(fountain_tree, fountain_tree.nodes["Waterfall Normal"], fountain_tree.nodes["Fountain Output"])
    assert reaches(fountain_tree, fountain_tree.nodes["Refraction Weight"], fountain_tree.nodes["Fountain Output"])
    fountain_mat = bpy.data.materials.new("W3 Fountain Cascade Test")
    fountain_mat.use_nodes = True
    fountain_nodes = fountain_mat.node_tree.nodes
    fountain_links = fountain_mat.node_tree.links
    fountain_nodes.clear()
    fountain_graph = fountain_nodes.new("ShaderNodeGroup")
    fountain_graph.node_tree = fountain_tree
    fountain_output = fountain_nodes.new("ShaderNodeOutputMaterial")
    fountain_links.new(fountain_graph.outputs["Shader"], fountain_output.inputs["Surface"])
    splash = fountain_nodes.new("ShaderNodeTexImage")
    splash.image = bpy.data.images.new("fountain_splash_test", 1, 1)
    cube = fountain_nodes.new("ShaderNodeTexEnvironment")
    cube.image = bpy.data.images.new("fountain_cube_test", 2, 1)
    speed = fountain_nodes.new("ShaderNodeCombineXYZ")
    speed.inputs["Y"].default_value = -0.2
    scale = fountain_nodes.new("ShaderNodeCombineXYZ")
    scale.inputs["X"].default_value = 3.0
    scale.inputs["Y"].default_value = 0.25
    fountain_links.new(splash.outputs["Color"], fountain_graph.inputs["normal_and_splash"])
    fountain_links.new(splash.outputs["Alpha"], fountain_graph.inputs["normal_and_splash_alpha"])
    fountain_links.new(cube.outputs["Color"], fountain_graph.inputs["cubemap"])
    fountain_links.new(speed.outputs[0], fountain_graph.inputs["texture_speed"])
    fountain_links.new(scale.outputs[0], fountain_graph.inputs["texture_coord"])
    material_module.setup_fountain_cascade_nodes(fountain_mat, fountain_graph)
    assert fountain_mat["witcher_fountain_shader_version"] == 2
    assert not material_module._exact_water_material_upgrade_required(fountain_mat, "m_fountain_cascade")
    fountain_mat["witcher_fountain_shader_version"] = 1
    assert material_module._exact_water_material_upgrade_required(fountain_mat, "m_fountain_cascade")
    fountain_mat["witcher_fountain_shader_version"] = 2
    assert splash.inputs["Vector"].links[0].from_node.name == "W3 Fountain Animated UV"
    direction = fountain_nodes["W3 Fountain Direction"]
    assert direction.inputs[0].links[0].from_node == speed
    assert tuple(direction.inputs[1].default_value) == (1.0, -1.0, 1.0)
    assert fountain_nodes["W3 Fountain Flow"].inputs[0].links[0].from_node == direction
    assert fountain_graph.inputs["fountain_uv"].links[0].from_node.name == "W3 Fountain UV"
    assert cube.inputs["Vector"].links[0].from_node.name == "W3 Fountain Reflection"
    time_output = fountain_nodes["W3 Fountain Time"].outputs[0]
    time_path = time_output.path_from_id("default_value")
    time_driver = next(
        curve.driver for curve in fountain_mat.node_tree.animation_data.drivers
        if curve.data_path == time_path
    )
    assert set(time_driver.variables.keys()) == {"fps", "fps_base"}
    fountain_mesh = bpy.data.meshes.new("W3 Fountain Cascade Test")
    fountain_mesh.from_pydata([(0.0, 0.0, 0.0)], [], [])
    fountain_object = bpy.data.objects.new("W3 Fountain Cascade Test", fountain_mesh)
    bpy.context.scene.collection.objects.link(fountain_object)
    fountain_mesh.materials.append(fountain_mat)
    bpy.context.scene.render.fps = 30
    bpy.context.scene.render.fps_base = 1.0
    bpy.context.scene.frame_set(1)
    start_time = time_output.default_value
    bpy.context.scene.frame_set(31)
    assert abs((time_output.default_value - start_time) - 1.0) < 1e-6
    material_module.mat_apply_settings(fountain_mat, "m_fountain_cascade")
    assert fountain_mat.surface_render_method == "BLENDED"
    assert not fountain_mat.use_transparency_overlap
    assert not fountain_mat.use_transparent_shadow
    fountain_nodes.remove(fountain_nodes["W3 Fountain UV Scale"])
    assert material_module._exact_water_material_upgrade_required(fountain_mat, "m_fountain_cascade")

    water_tree = material_module.ensure_node_group("transparent_reflective")
    water_inputs = {
        item.name
        for item in water_tree.interface.items_tree
        if getattr(item, "item_type", "") == "SOCKET" and item.in_out == "INPUT"
    }
    assert {
        "Normal", "NormalBig", "WindSpeed", "SmallWavesTile", "BigWavesTile",
        "DeepColor", "Transparency", "NormalIntensity",
    } <= water_inputs
    water_interface = {
        item.name: item
        for item in water_tree.interface.items_tree
        if getattr(item, "item_type", "") == "SOCKET" and item.in_out == "INPUT"
    }
    assert [(name, item.socket_type) for name, item in water_interface.items()] == [
        ("Diffuse", "NodeSocketColor"),
        ("Normal", "NodeSocketColor"),
        ("NormalBig", "NodeSocketColor"),
        ("Normal_is_sRGB", "NodeSocketFloat"),
        ("Alpha", "NodeSocketFloat"),
        ("Opacity", "NodeSocketFloat"),
        ("Roughness", "NodeSocketFloat"),
        ("IOR", "NodeSocketFloat"),
        ("SpecularColor", "NodeSocketColor"),
        ("RSpecBase", "NodeSocketFloat"),
        ("RSpecScale", "NodeSocketFloat"),
        ("DeepColor", "NodeSocketColor"),
        ("Transparency", "NodeSocketFloat"),
        ("WindSpeed", "NodeSocketVector"),
        ("SmallWavesTile", "NodeSocketVector"),
        ("BigWavesTile", "NodeSocketVector"),
        ("NormalIntensity", "NodeSocketFloat"),
    ]
    assert water_tree["witcher_material_graph_version"] == 3
    assert all(
        abs(actual - expected) < 1e-6
        for actual, expected in zip(water_interface["Diffuse"].default_value, (0.14, 0.28, 0.34, 1.0))
    )
    assert abs(water_interface["Roughness"].default_value - 0.16) < 1e-6
    assert abs(water_interface["IOR"].default_value - 1.333) < 1e-6
    water_surface = water_tree.nodes["Water Surface"]
    assert water_surface.bl_idname == "ShaderNodeBsdfPrincipled"
    assert abs(water_surface.inputs["Transmission Weight"].default_value - 0.60) < 1e-6
    assert abs(water_surface.inputs["Coat Weight"].default_value - 0.10) < 1e-6
    assert abs(water_surface.inputs["Coat Roughness"].default_value - 0.08) < 1e-6
    water_inputs_node = water_tree.nodes["Water Inputs"]
    water_fresnel = water_tree.nodes["Water Fresnel"]
    water_tint = water_tree.nodes["Shallow Deep Tint"]
    assert water_tint.inputs[0].links[0].from_node == water_fresnel
    assert water_tint.inputs[1].links[0].from_socket == water_inputs_node.outputs["DeepColor"]
    assert water_tint.inputs[2].links[0].from_socket == water_inputs_node.outputs["Diffuse"]
    assert water_surface.inputs["Base Color"].links[0].from_node == water_tint
    specular_tint = water_tree.nodes["Specular Tint"]
    assert specular_tint.operation == "NORMALIZE"
    assert specular_tint.inputs[0].links[0].from_socket == water_inputs_node.outputs["SpecularColor"]
    assert water_surface.inputs["Specular Tint"].links[0].from_node == specular_tint
    rspec_scale = water_tree.nodes["RSpec Scale"]
    rspec_value = water_tree.nodes["RSpec Value"]
    rspec_roughness = water_tree.nodes["RSpec Roughness"]
    assert rspec_scale.operation == "MULTIPLY"
    assert rspec_scale.inputs[0].links[0].from_socket == water_inputs_node.outputs["Roughness"]
    assert rspec_scale.inputs[1].links[0].from_socket == water_inputs_node.outputs["RSpecScale"]
    assert rspec_value.operation == "ADD"
    assert rspec_value.inputs[0].links[0].from_socket == water_inputs_node.outputs["RSpecBase"]
    assert rspec_value.inputs[1].links[0].from_node == rspec_scale
    assert rspec_roughness.operation == "POWER"
    assert rspec_roughness.inputs[0].default_value == 2.0
    assert rspec_roughness.inputs[1].links[0].from_node == rspec_value
    assert water_surface.inputs["Roughness"].links[0].from_node == rspec_roughness
    alpha_compensation = water_tree.nodes["Alpha Blend Compensation"]
    assert alpha_compensation.operation == "POWER"
    assert abs(alpha_compensation.inputs[1].default_value - 0.65) < 1e-6
    assert not any(node.type in {"BSDF_GLASS", "BSDF_ANISOTROPIC"} for node in water_tree.nodes)
    assert water_tree.nodes["Water Surface Visibility"].inputs[2].links[0].from_node == water_surface
    water_mat = bpy.data.materials.new("W3 Transparent Reflective Test")
    water_mat.use_nodes = True
    water_nodes = water_mat.node_tree.nodes
    water_links = water_mat.node_tree.links
    water_nodes.clear()
    water_graph = water_nodes.new("ShaderNodeGroup")
    water_graph.node_tree = water_tree
    water_output = water_nodes.new("ShaderNodeOutputMaterial")
    water_links.new(water_graph.outputs["Shader"], water_output.inputs["Surface"])
    water_normal = water_nodes.new("ShaderNodeTexImage")
    water_normal.image = bpy.data.images.new("water_normal_test", 1, 1)
    wind = water_nodes.new("ShaderNodeCombineXYZ")
    wind.inputs["X"].default_value = 0.025
    wind.inputs["Y"].default_value = 0.015
    small_tile = water_nodes.new("ShaderNodeCombineXYZ")
    small_tile.inputs["X"].default_value = 18.0
    small_tile.inputs["Y"].default_value = 18.0
    big_tile = water_nodes.new("ShaderNodeCombineXYZ")
    big_tile.inputs["X"].default_value = 12.0
    big_tile.inputs["Y"].default_value = 12.0
    water_links.new(water_normal.outputs["Color"], water_graph.inputs["Normal"])
    water_links.new(wind.outputs["Vector"], water_graph.inputs["WindSpeed"])
    water_links.new(small_tile.outputs["Vector"], water_graph.inputs["SmallWavesTile"])
    water_links.new(big_tile.outputs["Vector"], water_graph.inputs["BigWavesTile"])
    material_module.setup_transparent_reflective_nodes(water_mat, water_graph)
    assert water_mat["witcher_transparent_reflective_shader_version"] == 1
    assert not material_module._exact_water_material_upgrade_required(water_mat, "transparent_reflective")
    water_mat["witcher_transparent_reflective_shader_version"] = 0
    assert material_module._exact_water_material_upgrade_required(water_mat, "transparent_reflective")
    water_mat["witcher_transparent_reflective_shader_version"] = 1
    water_normal_big = water_nodes["W3 Water Normal Big"]
    assert water_normal_big.image == water_normal.image
    assert water_graph.inputs["Normal"].links[0].from_node == water_normal
    assert water_graph.inputs["NormalBig"].links[0].from_node == water_normal_big
    assert water_normal.inputs["Vector"].links[0].from_node.name == "W3 Water Small UV"
    assert water_normal_big.inputs["Vector"].links[0].from_node.name == "W3 Water Big UV"
    assert water_nodes["W3 Water Small UV"].operation == "ADD"
    assert water_nodes["W3 Water Big UV"].operation == "SUBTRACT"
    assert water_nodes["W3 Water Small Scale"].inputs[1].links[0].from_node == small_tile
    assert water_nodes["W3 Water Big Scale"].inputs[1].links[0].from_node == big_tile
    assert water_nodes["W3 Water Flow"].inputs[0].links[0].from_node == wind
    water_time_output = water_nodes["W3 Water Time"].outputs[0]
    water_time_path = water_time_output.path_from_id("default_value")
    water_time_driver = next(
        curve.driver for curve in water_mat.node_tree.animation_data.drivers
        if curve.data_path == water_time_path
    )
    assert set(water_time_driver.variables.keys()) == {"fps", "fps_base"}
    fountain_mesh.materials.append(water_mat)
    bpy.context.scene.frame_set(1)
    water_start_time = water_time_output.default_value
    bpy.context.scene.frame_set(31)
    assert abs((water_time_output.default_value - water_start_time) - 1.0) < 1e-6
    material_module.mat_apply_settings(water_mat, "transparent_reflective")
    assert water_mat.surface_render_method == "BLENDED"
    assert not water_mat.use_transparency_overlap
    assert not water_mat.use_transparent_shadow
    water_nodes.remove(water_nodes["W3 Water Flow"])
    assert material_module._exact_water_material_upgrade_required(water_mat, "transparent_reflective")

    old_water_tree_pointer = water_tree.as_pointer()
    water_tree["witcher_material_graph_version"] = 0
    water_tree = material_module.ensure_node_group("transparent_reflective")
    assert water_tree.as_pointer() != old_water_tree_pointer
    assert water_tree["witcher_material_graph_version"] == 3
    assert water_graph.node_tree == water_tree
    assert water_graph.inputs["Normal"].links[0].from_node == water_normal
    assert water_graph.inputs["NormalBig"].links[0].from_node == water_normal_big

    color_mat = bpy.data.materials.new("W3 Color Space Test")
    color_mat.use_nodes = True
    color_node = material_module.create_node_color(
        color_mat,
        {"value": "241; 20; 20; 255"},
        None,
    )
    color_value = color_node.outputs[0].default_value
    assert abs(color_value[0] - 0.8796224) < 1e-6
    assert abs(color_value[1] - 0.00699541) < 1e-6
    assert color_value[3] == 1.0

    mesh = bpy.data.meshes.new("W3 Hair Color Test")
    mesh.from_pydata([(0.0, 0.0, 0.0)], [], [])
    obj = bpy.data.objects.new("W3 Hair Color Test", mesh)
    bpy.context.scene.collection.objects.link(obj)
    mesh.materials.append(mat)
    material_module.mat_apply_settings(mat, "pbr_hair_simple")
    assert tuple(mesh.color_attributes["Color"].data[0].color) == (1.0, 1.0, 1.0, 1.0)
    assert mat.surface_render_method == "DITHERED"

    array_mesh = bpy.data.meshes.new("W3 Texture Array Test")
    array_mesh.from_pydata([(0.0, 0.0, 0.0)], [], [])
    array_obj = bpy.data.objects.new("W3 Texture Array Test", array_mesh)
    bpy.context.scene.collection.objects.link(array_obj)
    bpy.context.view_layer.objects.active = array_obj
    color = array_mesh.color_attributes.new(name="Color", domain="POINT", type="BYTE_COLOR")
    color.data[0].color_srgb = (0.0, 64 / 255, 0.0, 1.0)
    material_module._ensure_raw_vertex_color(array_obj)
    bpy.context.view_layer.objects.active = None
    array_tree = material_module.create_texarray(ARRAY_SIZE=3)
    assert array_tree.nodes["W3 Texture Array Vertex Color"].layer_name == "W3RawColor"
    assert abs(array_mesh.color_attributes["W3RawColor"].data[0].color[1] - 64 / 255) < 1e-5
    assert array_tree.nodes["W3 Texture Array Layer"].inputs[1].default_value == 16.0
    thresholds = sorted(
        node.inputs[1].default_value
        for node in array_tree.nodes
        if node.type == "MATH" and node.operation == "SUBTRACT"
    )
    assert thresholds == [0.0, 1.0]

    print("MATERIAL_EXACT_GROUPS_BLENDER_SMOKE_OK")


if __name__ == "__main__":
    main()
