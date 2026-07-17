"""Blender-native terrain material smoke test."""

from __future__ import annotations

import os
import importlib
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import bpy
import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import witcher3_tools
from witcher3_tools.importers import terrain_detail_nodes
from witcher3_tools.importers.terrain_w2ter import write_png


def _rgba(path: str, pixels: np.ndarray) -> None:
    height, width = pixels.shape[:2]
    write_png(path, width, height, 6, 8, np.ascontiguousarray(pixels).tobytes())


def main() -> None:
    assert terrain_detail_nodes.NODE_VERSION == 13
    ui_map = importlib.import_module("witcher3_tools.ui.ui_map")
    operator_class = ui_map.WITCH_OT_w2w
    bpy.utils.register_class(operator_class)
    try:
        properties = bpy.ops.witcher.import_w2w.get_rna_type().properties
        assert properties["terrain_import_mode"].default == "TILES"
        assert properties["terrain_multires_level"].default == 6
        assert properties["terrain_build_layer_tree"].default is True
        assert properties["terrain_detail_material"].default is True
        assert properties["terrain_detail_texture_res"].default == "1024"
    finally:
        bpy.utils.unregister_class(operator_class)

    from witcher3_tools.importers import import_w2w
    group = SimpleNamespace(name="World Reimport Smoke", ChildrenGroups=(), ChildrenInfos=())
    world_path = r"C:\worlds\reimport_smoke.w2w"
    first = import_w2w.AddCLayerGroup(group, False, world_path)
    bpy.context.scene.collection.children.link(first)
    try:
        assert import_w2w.AddCLayerGroup(group, False, world_path.upper()) is first
    finally:
        bpy.data.collections.remove(first)

    with tempfile.TemporaryDirectory() as tmp:
        atlas_d_path = os.path.join(tmp, "atlas_d.png")
        atlas_n_path = os.path.join(tmp, "atlas_n.png")
        control_path = os.path.join(tmp, "control.png")
        params_path = os.path.join(tmp, "params.png")
        params2_path = os.path.join(tmp, "params2.png")
        params3_path = os.path.join(tmp, "params3.png")
        normal_path = os.path.join(tmp, "normal.png")
        tint_path = os.path.join(tmp, "tint.png")

        atlas_d = np.full((8, 8, 4), (105, 125, 70, 255), dtype=np.uint8)
        atlas_n = np.full((8, 8, 4), (128, 128, 255, 190), dtype=np.uint8)
        control = np.full((5, 5, 4), (1, 1, 0, 255), dtype=np.uint8)
        control[0, 0, 3] = 0
        params = np.full((5, 5, 4), (64, 13, 0, 128), dtype=np.uint8)
        params2 = np.full((5, 5, 4), (64, 64, 0, 255), dtype=np.uint8)
        params3 = np.full((5, 5, 4), (0, 0, 0, 0), dtype=np.uint8)
        macro_normal = np.full((5, 5, 4), (128, 128, 255, 255), dtype=np.uint8)
        tint = np.full((5, 5, 4), (128, 128, 128, 255), dtype=np.uint8)
        for path, pixels in (
            (atlas_d_path, atlas_d), (atlas_n_path, atlas_n),
            (control_path, control), (params_path, params),
            (params2_path, params2), (params3_path, params3),
            (normal_path, macro_normal),
            (tint_path, tint),
        ):
            _rgba(path, pixels)

        atlas = {
            "diffuse": atlas_d_path,
            "normal": atlas_n_path,
            "layout": {
                "version": 1, "n_slices": 1, "slice_px": 4,
                "gutter_px": 2, "cell_px": 8, "cols": 1, "rows": 1,
                "atlas_w": 8, "atlas_h": 8, "has_normals": True,
            },
        }
        maps = {
            "control": control_path, "params": params_path,
            "params2": params2_path, "params3": params3_path,
            "normal": normal_path,
            "tint": tint_path, "res": 4, "map_res": 5,
            "tint_res": 4, "tint_map_res": 5, "has_holes": True,
        }

        bpy.ops.mesh.primitive_plane_add(size=2.0)
        obj = bpy.context.object

        # The terrain-face inspector must resolve one selected face through the
        # same four control taps used by the detail shader and expose source names.
        control_buffer = os.path.join(tmp, "face_inspector.buffer")
        control_word = 19 | (17 << 5) | (7 << 10) | (3 << 13)
        np.array([control_word], dtype="<u2").tofile(control_buffer)
        obj["terrain_multires"] = 0
        obj["tile_x"] = 8
        obj["tile_y"] = 7
        obj["tile_res"] = 1
        obj["witcher_terrain_control_res"] = 1
        obj["witcher_terrain_texture_buffer"] = control_buffer
        obj["witcher_terrain_positive_x_texture_buffer"] = ""
        obj["witcher_terrain_positive_y_texture_buffer"] = ""
        obj["witcher_terrain_positive_xy_texture_buffer"] = ""
        obj["witcher_terrain_layer_metadata"] = json.dumps([
            {
                "id": 19, "atlas_index": 18, "name": "sand_to grass",
                "diffuse_source": r"environment\terrain\sand_to grass.xbm",
                "blend_sharpness": 0.13,
            },
            {
                "id": 17, "atlas_index": 16, "name": "ground_wet",
                "diffuse_source": r"environment\terrain\ground_wet.xbm",
                "slope_base_dampening": 0.25,
                "slope_normal_dampening": 0.5,
            },
        ])
        inspector_class = witcher3_tools.WITCHER_OT_inspect_terrain_face_materials
        bpy.utils.register_class(inspector_class)
        try:
            bpy.context.view_layer.objects.active = obj
            obj.select_set(True)
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='SELECT')
            class InspectorHarness:
                _layer_summary = staticmethod(inspector_class._layer_summary)
                _layer_paths = staticmethod(inspector_class._layer_paths)

                def _read_selection(self, context):
                    return inspector_class._read_selection(self, context)

            inspector = InspectorHarness()
            inspector_class._inspect(inspector, bpy.context)
            assert "19: sand_to grass" in inspector.horizontal_layers
            assert "17: ground_wet" in inspector.vertical_layers
            assert "threshold 0.98" in inspector.slope_parameters
            assert "H19/V17 S7=0.98 UV3=0.025" in inspector.corner_samples
            assert r"environment\terrain\sand_to grass.xbm" in inspector.horizontal_paths
        finally:
            if bpy.context.mode == 'EDIT_MESH':
                bpy.ops.object.mode_set(mode='OBJECT')
            bpy.utils.unregister_class(inspector_class)

        layer_metadata = json.loads(obj["witcher_terrain_layer_metadata"])
        material = terrain_detail_nodes.apply_tile_detail_material(
            obj, "TerrainDetailNativeSmoke", atlas, maps,
            fresnel_power=16.0,
            texture_pack_key="native_test_world",
            layer_metadata=layer_metadata)
        assert material is not None
        assert material.get("witcher_terrain_detail") is True
        assert obj.data.materials[0] is material
        assert len(material.node_tree.nodes) == 5

        material_group_node = next(
            node for node in material.node_tree.nodes
            if node.bl_idname == "ShaderNodeGroup"
        )
        material_group = material_group_node.node_tree
        assert material_group_node.name == terrain_detail_nodes.DETAIL_GROUP_NODE_NAME
        assert {
            "Normal Strength", "Tint Strength", "Fresnel Strength", "Slope Override",
        }.issubset(material_group_node.inputs.keys())
        assert {
            "Slope", "Specular", "IOR", "MacroNormalDebug", "FinalNormalDebug",
        }.issubset(material_group_node.outputs.keys())
        lut = terrain_detail_nodes.ensure_terrain_texture_pack_image(
            "native_test_world", layer_metadata)
        assert lut is not None
        assert any(
            node.bl_idname == "ShaderNodeTexImage" and node.image is lut
            for node in material_group.nodes
        )
        terrain_detail_nodes.update_terrain_texture_pack_layer(
            "native_test_world", 19, specularity=1.0, specularity_base=1.0)
        live_rows = terrain_detail_nodes.terrain_texture_pack_values(
            "native_test_world", layer_metadata)
        layer_19 = next(row for row in live_rows if row["id"] == 19)
        assert abs(layer_19["specularity"] - 1.0) < 1e-6
        assert abs(layer_19["specularity_base"] - 1.0) < 1e-6
        # Unchanged source keeps live edits; changed parameters replace them.
        assert terrain_detail_nodes.ensure_terrain_texture_pack_image(
            "native_test_world", layer_metadata) is lut
        retained = terrain_detail_nodes.terrain_texture_pack_values(
            "native_test_world", layer_metadata)
        assert abs(next(row for row in retained if row["id"] == 19)[
            "specularity"] - 1.0) < 1e-6
        updated_metadata = [dict(row) for row in layer_metadata]
        updated_layer_19 = next(row for row in updated_metadata if row["id"] == 19)
        updated_layer_19["specularity"] = 0.35
        updated_layer_19["specularity_base"] = 0.65
        assert terrain_detail_nodes.ensure_terrain_texture_pack_image(
            "native_test_world", updated_metadata) is lut
        refreshed = terrain_detail_nodes.terrain_texture_pack_values(
            "native_test_world", updated_metadata)
        refreshed_layer_19 = next(row for row in refreshed if row["id"] == 19)
        assert abs(refreshed_layer_19["specularity"] - 0.35) < 1e-6
        assert abs(refreshed_layer_19["specularity_base"] - 0.65) < 1e-6
        layer_metadata = updated_metadata

        terrain_detail_nodes.configure_material_controls(
            material,
            surface_mode="OVERRIDE",
            roughness=0.91,
            specular=0.07,
            normal_strength=0.25,
            tint_strength=0.5,
            fresnel_strength=0.0,
            slope_mode="VERTICAL",
            debug_view="SLOPE",
        )
        principled = material.node_tree.nodes["Principled BSDF"]
        assert not principled.inputs["Roughness"].is_linked
        assert abs(principled.inputs["Roughness"].default_value - 0.91) < 1e-6
        spec_input = principled.inputs.get("Specular IOR Level") or principled.inputs["Specular"]
        assert not spec_input.is_linked
        assert abs(spec_input.default_value - 0.5) < 1e-6
        assert not principled.inputs["IOR"].is_linked
        assert abs(
            principled.inputs["IOR"].default_value
            - float(terrain_detail_nodes.f0_to_ior(0.07))
        ) < 1e-6
        assert abs(material_group_node.inputs["Normal Strength"].default_value - 0.25) < 1e-6
        assert abs(material_group_node.inputs["Tint Strength"].default_value - 0.5) < 1e-6
        assert material_group_node.inputs["Fresnel Strength"].default_value == 0.0
        assert material_group_node.inputs["Slope Override"].default_value == 1.0
        debug_mix = material.node_tree.nodes[terrain_detail_nodes.DEBUG_MIX_NODE_NAME]
        debug_emission = material.node_tree.nodes[terrain_detail_nodes.DEBUG_EMISSION_NODE_NAME]
        assert debug_mix.inputs[0].default_value == 1.0
        assert debug_emission.inputs["Color"].links[0].from_socket.name == "Slope"

        terrain_detail_nodes.configure_material_controls(material, debug_view="FINAL")
        assert principled.inputs["Roughness"].links[0].from_socket.name == "Roughness"
        assert principled.inputs["IOR"].links[0].from_socket.name == "IOR"
        assert not spec_input.is_linked
        assert abs(spec_input.default_value - 0.5) < 1e-6
        assert debug_mix.inputs[0].default_value == 0.0
        unmatched_material_offsets = [
            node for node in material_group.nodes
            if node.bl_idname == "ShaderNodeVectorMath"
            and node.operation == "ADD"
            and tuple(round(v, 6) for v in node.inputs[1].default_value)
            == (-0.5, -0.5, 0.0)
        ]
        # Paired half-texel offsets cancel before the control gather.
        assert not unmatched_material_offsets
        assert not any(
            node.bl_idname == "ShaderNodeMath" and node.operation == "ARCCOSINE"
            for node in material_group.nodes
        )
        # Positive Z and geometric slope each require one square root.
        assert sum(
            node.bl_idname == "ShaderNodeMath" and node.operation == "SQRT"
            for node in material_group.nodes
        ) == 2
        assert sum(
            node.bl_idname == "ShaderNodeVectorMath" and node.operation == "CROSS_PRODUCT"
            for node in material_group.nodes
        ) == 1
        power_nodes = [
            node for node in material_group.nodes
            if node.bl_idname == "ShaderNodeMath" and node.operation == "POWER"
        ]
        # Specularity and the three final color channels each use one 2.2 power.
        assert len(power_nodes) == 6
        exponents = [node.inputs[1].default_value for node in power_nodes]
        assert sum(abs(value - 2.2) < 1e-6 for value in exponents) == 4
        assert sum(abs(value - 0.5) < 1e-6 for value in exponents) == 1
        assert sum(abs(value - 16.0) < 1e-6 for value in exponents) == 1

        tap_group = next(
            group for group in bpy.data.node_groups
            if group.name.startswith(".W3TerrainTap ")
        )
        tap_nodes = [
            node for node in material_group.nodes
            if node.bl_idname == "ShaderNodeGroup" and node.node_tree is tap_group
        ]
        assert len(tap_nodes) == 4
        assert all(node.inputs["MacroNormal"].is_linked for node in tap_nodes)
        projections = {
            node.label: node for node in tap_group.nodes
            if node.bl_idname == "ShaderNodeGroup" and node.label
        }
        assert set(projections) == {
            "XY horizontal", "XY triplanar",
            "-XZ triplanar", "-YZ triplanar",
        }
        tap_output = next(
            node for node in tap_group.nodes if node.bl_idname == "NodeGroupOutput"
        )
        vertical_diffuse_sample = tap_output.inputs["BgDiff"].links[0].from_node
        assert vertical_diffuse_sample.bl_idname == "ShaderNodeTexImage"
        assert (
            vertical_diffuse_sample.inputs["Vector"].links[0].from_node
            == projections["XY triplanar"]
        )
        for label in ("-XZ triplanar", "-YZ triplanar"):
            projection = projections[label]
            u_multiply = projection.inputs["U"].links[0].from_node
            u_negate = u_multiply.inputs[0].links[0].from_node
            assert u_multiply.operation == "MULTIPLY"
            assert u_negate.operation == "MULTIPLY"
            assert u_negate.inputs[1].default_value == -1.0
            v_multiply = projection.inputs["V"].links[0].from_node
            v_source = v_multiply.inputs[0].links[0].from_socket
            assert v_multiply.operation == "MULTIPLY"
            assert v_source.name == "Z"

        power_16_sig = material["witcher_terrain_detail_sig"]
        tint[:, :, 0] = 150
        _rgba(tint_path, tint)
        os.utime(tint_path, None)
        material = terrain_detail_nodes.apply_tile_detail_material(
            obj, "TerrainDetailNativeSmoke", atlas, maps,
            fresnel_power=8.0,
            texture_pack_key="native_test_world",
            layer_metadata=layer_metadata)
        assert material is not None
        assert material["witcher_terrain_detail_sig"] != power_16_sig
        material_group = next(
            node for node in material.node_tree.nodes
            if node.bl_idname == "ShaderNodeGroup"
        ).node_tree
        assert sum(
            node.bl_idname == "ShaderNodeMath"
            and node.operation == "POWER"
            and abs(node.inputs[1].default_value - 8.0) < 1e-6
            for node in material_group.nodes
        ) == 1
        detail_groups = [
            group for group in bpy.data.node_groups
            if group.name.startswith((".W3AtlasUV ", ".W3TerrainTap ",
                                      ".W3TerrainDetail "))
        ]
        assert len(detail_groups) == 3, [group.name for group in detail_groups]

        bpy.context.scene.render.engine = "BLENDER_EEVEE"
        bpy.context.scene.render.resolution_x = 16
        bpy.context.scene.render.resolution_y = 16
        bpy.context.scene.render.resolution_percentage = 100
        bpy.ops.object.camera_add(location=(0.0, 0.0, 3.0))
        camera = bpy.context.object
        camera.rotation_euler = (0.0, 0.0, 0.0)
        bpy.context.scene.camera = camera
        bpy.ops.render.render()

        print("TERRAIN_DETAIL_NATIVE_OK", len(bpy.data.node_groups))


if __name__ == "__main__":
    main()
