"""Blender-native check for Witcher 3 skin subsurface wiring."""

from __future__ import annotations

import sys
from pathlib import Path

import bpy


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def main() -> None:
    from witcher3_tools.materials import material as material_module

    skin_material = bpy.data.materials.new('W3 Skin SSS Test')
    skin_material.use_nodes = True
    group_node = material_module.init_material_nodes(skin_material, 'pbr_skin')
    group_node.inputs['SubsurfaceScale'].default_value = 0.5
    skin_tree = group_node.node_tree

    principled = next(node for node in skin_tree.nodes if node.type == 'BSDF_PRINCIPLED')
    weight = principled.inputs.get('Subsurface Weight') or principled.inputs.get('Subsurface')
    weight_scale = weight.links[0].from_node
    assert weight_scale.name == 'W3 Skin Subsurface Weight'
    assert weight_scale.operation == 'MULTIPLY'
    assert weight_scale.use_clamp
    assert abs(weight_scale.inputs[1].default_value - 0.4) < 1e-6
    assert weight_scale.inputs[0].links[0].from_socket.name in {'SubsurfaceScale', 'Subsurface Scale'}
    assert getattr(principled, 'subsurface_method', '') in {'RANDOM_WALK_SKIN', 'RANDOM_WALK'}

    radius = principled.inputs['Subsurface Radius']
    scale = principled.inputs.get('Subsurface Scale')
    if scale is not None:
        assert tuple(round(value, 4) for value in radius.default_value) == (1.0, 0.35, 0.2)
        assert abs(scale.default_value - 0.01) < 1e-6
    else:
        assert tuple(round(value, 4) for value in radius.default_value) == (0.01, 0.0035, 0.002)

    material_module.ensure_node_group('Witcher3_Skin')
    assert len([node for node in skin_tree.nodes if node.name == 'W3 Skin Subsurface Weight']) == 1

    main_tree = material_module.ensure_node_group('Witcher3_Main')
    assert main_tree.nodes.get('W3 Skin Subsurface Weight') is None
    print('MATERIAL_SKIN_BLENDER_SMOKE_OK')


if __name__ == '__main__':
    main()
