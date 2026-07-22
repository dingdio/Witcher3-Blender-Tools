from __future__ import annotations

import sys
from pathlib import Path

import bpy


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def main() -> None:
    from witcher3_tools.materials import material as material_module
    from witcher3_tools.materials.nodes.domain import refresh_witcher_include_state

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
