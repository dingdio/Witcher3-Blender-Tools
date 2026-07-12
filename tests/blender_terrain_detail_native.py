"""Blender-native terrain material smoke test."""

from __future__ import annotations

import os
import importlib
import sys
import tempfile
from pathlib import Path

import bpy
import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from witcher3_tools.importers import terrain_detail_nodes
from witcher3_tools.importers.terrain_w2ter import write_png


def _rgba(path: str, pixels: np.ndarray) -> None:
    height, width = pixels.shape[:2]
    write_png(path, width, height, 6, 8, np.ascontiguousarray(pixels).tobytes())


def main() -> None:
    ui_map = importlib.import_module("witcher3_tools.ui.ui_map")
    operator_class = ui_map.WITCH_OT_w2w
    bpy.utils.register_class(operator_class)
    try:
        properties = bpy.ops.witcher.import_w2w.get_rna_type().properties
        assert properties["terrain_import_mode"].default == "FULL_MAP"
        assert properties["terrain_multires_level"].default == 8
        assert properties["terrain_build_layer_tree"].default is True
        assert properties["terrain_detail_material"].default is True
        assert properties["terrain_detail_texture_res"].default == "1024"
    finally:
        bpy.utils.unregister_class(operator_class)

    with tempfile.TemporaryDirectory() as tmp:
        atlas_d_path = os.path.join(tmp, "atlas_d.png")
        atlas_n_path = os.path.join(tmp, "atlas_n.png")
        control_path = os.path.join(tmp, "control.png")
        params_path = os.path.join(tmp, "params.png")
        params2_path = os.path.join(tmp, "params2.png")
        normal_path = os.path.join(tmp, "normal.png")
        tint_path = os.path.join(tmp, "tint.png")

        atlas_d = np.full((8, 8, 4), (105, 125, 70, 255), dtype=np.uint8)
        atlas_n = np.full((8, 8, 4), (128, 128, 255, 190), dtype=np.uint8)
        control = np.full((4, 4, 4), (1, 1, 0, 255), dtype=np.uint8)
        control[0, 0, 3] = 0
        params = np.full((4, 4, 4), (64, 13, 0, 128), dtype=np.uint8)
        params2 = np.full((4, 4, 4), (64, 64, 0, 255), dtype=np.uint8)
        macro_normal = np.full((4, 4, 4), (128, 128, 255, 255), dtype=np.uint8)
        tint = np.full((4, 4, 4), (128, 128, 128, 255), dtype=np.uint8)
        for path, pixels in (
            (atlas_d_path, atlas_d), (atlas_n_path, atlas_n),
            (control_path, control), (params_path, params),
            (params2_path, params2), (normal_path, macro_normal),
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
            "params2": params2_path, "normal": normal_path,
            "tint": tint_path, "res": 4, "has_holes": True,
        }

        bpy.ops.mesh.primitive_plane_add(size=2.0)
        obj = bpy.context.object
        material = terrain_detail_nodes.apply_tile_detail_material(
            obj, "TerrainDetailNativeSmoke", atlas, maps)
        assert material is not None
        assert material.get("witcher_terrain_detail") is True
        assert obj.data.materials[0] is material
        assert len(material.node_tree.nodes) == 3

        tint[:, :, 0] = 150
        _rgba(tint_path, tint)
        os.utime(tint_path, None)
        material = terrain_detail_nodes.apply_tile_detail_material(
            obj, "TerrainDetailNativeSmoke", atlas, maps)
        assert material is not None
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
