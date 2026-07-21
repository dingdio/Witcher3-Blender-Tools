from __future__ import annotations

import sys
from pathlib import Path

import bpy


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def main() -> None:
    from witcher3_tools.materials import material as material_module

    material = bpy.data.materials.new("W3 Eye Blick Test")
    material.use_nodes = True
    group = material_module.init_material_nodes(material, "pbr_eye")
    tree = material.node_tree
    env = tree.nodes.new("ShaderNodeTexEnvironment")
    env.name = "BlickCube"
    env.image = bpy.data.images.new("W3 Eye Blick Equirect Test", 1, 1)
    env.image["witcher_blick_equirect_dds"] = True
    env.image.colorspace_settings.name = "sRGB"
    tree.links.new(env.outputs["Color"], group.inputs["BlickCube"])
    normal_bubble = tree.nodes.new("ShaderNodeTexImage")
    normal_bubble.name = "NormalBubble"
    normal_bubble.image = bpy.data.images.new("W3 Eye NormalBubble Test", 1, 1)
    tree.links.new(normal_bubble.outputs["Color"], group.inputs["NormalBubble"])
    diffuse = tree.nodes.new("ShaderNodeTexImage")
    diffuse.name = "Diffuse"
    diffuse.image = bpy.data.images.new("W3 Eye Diffuse Test", 1, 1)
    tree.links.new(diffuse.outputs["Color"], group.inputs["Diffuse"])
    normal_base = tree.nodes.new("ShaderNodeTexImage")
    normal_base.name = "NormalBase"
    normal_base.image = bpy.data.images.new("W3 Eye NormalBase Test", 1, 1)
    tree.links.new(normal_base.outputs["Color"], group.inputs["NormalBase"])

    for name, value in {
        "EyeRadius": 0.015,
        "BlickScale": 1.0,
        "BlikScaleMeat": 0.3,
        "Specularity": 0.18,
        "SpecularityMeat": 0.15,
        "BubbleNormalTile": 1.2,
        "IrisSize": 0.83,
        "IrisCoordFactor": 0.159,
        "IrisCoordMargin": 0.02,
        "EggFullRadius": 1.0,
        "EggSubFactor": 0.2,
        "EggMarginFactor": 0.4,
        "EggMarginExponent": 1.0,
    }.items():
        node = tree.nodes.new("ShaderNodeValue")
        node.name = name
        node.outputs[0].default_value = value
        tree.links.new(node.outputs[0], group.inputs[name])

    for _ in range(2):
        material_module.setup_eye_reflection_nodes(material, group, tree.nodes, tree.links)

    assert len([node for node in tree.nodes if node.name == "W3 Eye Blick Lookup"]) == 1
    assert env.inputs["Vector"].links[0].from_node.name == "W3 Eye Blick Lookup"
    assert env.image.colorspace_settings.name == "Non-Color"
    reflection = tree.nodes["W3 Eye Reflection"]
    assert reflection.inputs[1].links[0].from_node.name == "W3 Eye Bubble World Normal"
    assert normal_bubble.inputs["Vector"].links[0].from_node.name == "W3 Eye Bubble UV Tiled"
    assert diffuse.inputs["Vector"].links[0].from_node.name == "W3 Eye Iris UV"
    assert normal_base.inputs["Vector"].links[0].from_node.name == "W3 Eye Iris UV"
    assert len([node for node in tree.nodes if node.name == "W3 Eye Bubble World Normal"]) == 1
    assert tuple(tree.nodes["W3 Eye Bubble Detail XY"].inputs[1].default_value)[:3] == (
        1.0,
        -1.0,
        0.0,
    )
    environment_blick = tree.nodes[material_module._EYE_BLICK_ENV_NODE]
    material_module.set_eye_blick_environment_color((1.913295, 1.913295, 1.913295))
    assert tuple(round(value, 6) for value in environment_blick.inputs[1].default_value) == (
        1.913295,
        1.913295,
        1.913295,
    )

    eye_tree = group.node_tree
    add_shader = eye_tree.nodes["W3 Eye Additive Blick"]
    emission = eye_tree.nodes["W3 Eye Blick Emission"]
    assert abs(emission.inputs["Strength"].default_value - 1.0) < 1e-6
    output = next(node for node in eye_tree.nodes if node.type == "GROUP_OUTPUT")
    assert all(
        output.inputs[name].links[0].from_node.name == add_shader.name
        for name in ("Cycles", "Eevee")
    )

    specular_factor = tree.nodes["W3 Eye Specular IOR Level"].inputs[1].default_value
    expected_factor = 1.0 / (2.0 * ((1.38 - 1.0) / (1.38 + 1.0)) ** 2)
    assert abs(specular_factor - expected_factor) < 1e-6
    assert abs((0.18 ** 2.2) * specular_factor - 0.450979) < 1e-5

    mesh = bpy.data.meshes.new("W3 Eye Iris Driver Mesh")
    mesh.from_pydata([(0.0, 0.0, 0.0)], [], [])
    obj = bpy.data.objects.new("W3 Eye Iris Driver Object", mesh)
    bpy.context.scene.collection.objects.link(obj)
    mesh.materials.append(material)
    obj.shape_key_add(name="Basis")
    obj.shape_key_add(name="iris_wide")
    obj.shape_key_add(name="iris_narrow")

    armature = bpy.data.armatures.new("W3 Eye Iris Driver Armature")
    rig = bpy.data.objects.new("W3 Eye Iris Driver Rig", armature)
    bpy.context.scene.collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    rig.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    edit_bone = armature.edit_bones.new("w3_face_poses")
    edit_bone.tail.z = 1.0
    bpy.ops.object.mode_set(mode="OBJECT")
    control_bone = rig.pose.bones["w3_face_poses"]
    control_bone["iris_wide"] = 0.0
    control_bone["iris_narrow"] = 0.0

    for _ in range(2):
        assert material_module.setup_eye_iris_morph_drivers([obj], rig) == 1
    control = tree.nodes[material_module._EYE_IRIS_MORPH_CONTROL_NODE]
    assert len([
        node for node in tree.nodes
        if node.name == material_module._EYE_IRIS_MORPH_CONTROL_NODE
    ]) == 1
    strength = tree.nodes[material_module._EYE_IRIS_MORPH_STRENGTH_NODE]
    assert abs(strength.outputs[0].default_value - 0.2) < 1e-6
    driver_curve = next(
        curve
        for curve in tree.animation_data.drivers
        if material_module._EYE_IRIS_MORPH_CONTROL_NODE in curve.data_path
    )
    assert driver_curve.driver.expression == "narrow - wide"
    assert {variable.name for variable in driver_curve.driver.variables} == {"wide", "narrow"}

    control_bone["iris_wide"] = 1.0
    control_bone["iris_narrow"] = 0.0
    rig.update_tag(refresh={"DATA"})
    bpy.context.scene.frame_set(bpy.context.scene.frame_current + 1)
    bpy.context.view_layer.update()
    assert abs(control.outputs[0].default_value + 1.0) < 1e-6
    control_bone["iris_wide"] = 0.0
    control_bone["iris_narrow"] = 1.0
    rig.update_tag(refresh={"DATA"})
    bpy.context.scene.frame_set(bpy.context.scene.frame_current + 1)
    bpy.context.view_layer.update()
    assert abs(control.outputs[0].default_value - 1.0) < 1e-6

    no_blick_material = bpy.data.materials.new("W3 Eye Iris Without Blick Test")
    no_blick_material.use_nodes = True
    no_blick_group = material_module.init_material_nodes(no_blick_material, "pbr_eye")
    no_blick_mesh = bpy.data.meshes.new("W3 Eye Iris Without Blick Mesh")
    no_blick_obj = bpy.data.objects.new("W3 Eye Iris Without Blick Object", no_blick_mesh)
    bpy.context.scene.collection.objects.link(no_blick_obj)
    no_blick_mesh.materials.append(no_blick_material)
    assert material_module.setup_eye_iris_morph_drivers([no_blick_obj], rig) == 1
    assert no_blick_material.node_tree.nodes.get(
        material_module._EYE_IRIS_MORPH_CONTROL_NODE
    ) is not None
    assert no_blick_group.inputs["BlickCube"].is_linked is False
    print("MATERIAL_EYE_BLENDER_SMOKE_OK")


if __name__ == "__main__":
    main()
