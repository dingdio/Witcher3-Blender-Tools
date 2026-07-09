"""Tests for the W2 bespoke node group policy.

Pin lists mirror the bundled witcher3_materials.blend node groups; declared
parameter sets mirror the real Witcher 2 shader graphs (including the junk
names W2 graphs surface from their parameter buffers).
"""

import sys
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

if "witcher3_tools" not in sys.modules:
    _pkg = types.ModuleType("witcher3_tools")
    _pkg.__path__ = [str(REPO_ROOT / "witcher3_tools")]
    _pkg.__package__ = "witcher3_tools"
    _pkg.get_addon_name = lambda: "witcher3_tools"
    sys.modules["witcher3_tools"] = _pkg

from witcher3_tools.materials.w2_compat import (
    canonical_w2_pin_key,
    is_w2_srgb_texture_param,
    plan_w2_socket_renames,
    w2_bespoke_node_group_name,
)


WITCHER2_MAIN_PINS = [
    'Diffuse', 'Alpha', 'Normal', 'Roughness', 'Roughness_min', 'Roughness_max',
    'TintMask', 'SpecularTexture', 'SpecularColor', 'RSpecBase', 'RSpecScale',
    'Translucency', 'FresnelStrenght', 'FresnelPower', 'AOPower', 'AmbientPower',
    'Height', 'DetailPower', 'DetailTile', 'DetailTile_W', 'DetailNormal',
    'DetailNormal1', 'Detail2Normal', 'DetailNormal2', 'ColorShift_ BlendColors',
    'ColorShift_ KeepGray', 'ColorShift_Enabled', 'DetailAlbedoSpecPower',
    'DetailAlbedoPower', 'VarianceColor', 'VarianceOffset', 'SnowDiffuse',
    'SnowNormal', 'Pattern_Array',
]

WITCHER2_HAIR_PINS = [
    'tex_Diffuse', 'Diffuse_Multiplier', 'Alpha', 'AlphaCutoff', 'tex_Normalmap',
    'tex_Specular', 'Roughness', 'SpecularColor', 'RSpecBase', 'RSpecScale',
    'Anisotropy', 'SpecularShiftTexture', 'Translucency', 'TranslucencyRim',
    'TranslucencyRimScale',
]

WITCHER3_EYE_PINS = [
    'Diffuse', 'Alpha', 'Roughness', 'Specular', 'NormalBase', 'NormalBubble',
    'Specularity', 'SubsurfaceFactor', 'EyeParallaxPlane', 'EyeRadius', 'IrisSize',
]


class TestBespokeGroupName(unittest.TestCase):
    def test_name_from_base_path(self):
        self.assertEqual(
            w2_bespoke_node_group_name(r"characters\shaders\cloth.w2mg"),
            "Witcher2_characters_shaders_cloth",
        )

    def test_name_normalizes_case_and_slashes(self):
        self.assertEqual(
            w2_bespoke_node_group_name("Characters/Shaders/Cloth.w2mg"),
            "Witcher2_characters_shaders_cloth",
        )

    def test_long_path_fits_blender_name_limit_and_stays_unique(self):
        long_a = r"dlc\some\very\long\path\to\a\deeply\nested\shader\graph_variant_a.w2mg"
        long_b = r"dlc\some\very\long\path\to\a\deeply\nested\shader\graph_variant_b.w2mg"
        name_a = w2_bespoke_node_group_name(long_a)
        name_b = w2_bespoke_node_group_name(long_b)
        self.assertLessEqual(len(name_a), 63)
        self.assertLessEqual(len(name_b), 63)
        self.assertNotEqual(name_a, name_b)

    def test_empty_path(self):
        self.assertEqual(w2_bespoke_node_group_name(""), "")


class TestPlanRenames(unittest.TestCase):
    def test_cloth_on_witcher2_main(self):
        declared = {
            'diffusemap', 'normalmap', 'specularmap', 'Glossiness',
            'SpecularColorSwitch', 'Fresnel falloff', 'rim brightness',
            'specularity_shift', 'sortGroup',
        }
        renames = plan_w2_socket_renames(declared, WITCHER2_MAIN_PINS)
        self.assertEqual(renames, {
            'Diffuse': 'diffusemap',
            'Normal': 'normalmap',
            'SpecularTexture': 'specularmap',
        })

    def test_leather_uses_specular_not_specularmap(self):
        declared = {'diffusemap', 'normalmap', 'specular', 'cubemap', 'Glossiness'}
        renames = plan_w2_socket_renames(declared, WITCHER2_MAIN_PINS)
        self.assertEqual(renames['SpecularTexture'], 'specular')

    def test_hair_pins_already_match(self):
        declared = {
            'tex_Diffuse', 'tex_Normalmap', 'tex_Specular', 'cubemap',
            'glossiness', 'LOD Bias', 'Specular Power',
        }
        self.assertEqual(plan_w2_socket_renames(declared, WITCHER2_HAIR_PINS), {})

    def test_glass_declares_canonical_names(self):
        declared = {'Diffuse', 'Normal', 'Opacity', 'Refraction', 'ColorMod'}
        self.assertEqual(plan_w2_socket_renames(declared, WITCHER2_MAIN_PINS), {})

    def test_eye_witcher_on_witcher3_eye(self):
        declared = {'diffusemap', 'normalmap', 'specular', 'cubemap', 'eye_glow', 'glossiness'}
        renames = plan_w2_socket_renames(declared, WITCHER3_EYE_PINS)
        self.assertEqual(renames, {
            'Diffuse': 'diffusemap',
            'NormalBase': 'normalmap',
            'Specular': 'specular',  # case-only fix so exports match the graph
        })

    def test_stdmat_camelcase_diffusemap(self):
        declared = {'DiffuseMap', 'DiffuseColor'}
        renames = plan_w2_socket_renames(declared, WITCHER2_MAIN_PINS)
        self.assertEqual(renames, {'Diffuse': 'DiffuseMap'})

    def test_junk_buffer_names_are_inert(self):
        declared = {'ERenderingSortGroup', 'sortGroup', 'Uint', 'CMaterialGraph', 'paramBlockSize'}
        self.assertEqual(plan_w2_socket_renames(declared, WITCHER2_MAIN_PINS), {})

    def test_pin_keeps_name_when_graph_declares_it_exactly(self):
        # The exact pin name wins over an alias also present in the graph.
        declared = {'Diffuse', 'diffusemap'}
        renames = plan_w2_socket_renames(declared, WITCHER2_MAIN_PINS)
        self.assertNotIn('Diffuse', renames)

    def test_alias_does_not_steal_an_existing_socket_name(self):
        # A socket named like the alias already exists -> no duplicate names.
        pins = ['Diffuse', 'diffusemap']
        renames = plan_w2_socket_renames({'diffusemap'}, pins)
        self.assertEqual(renames, {})


class TestSrgbClassification(unittest.TestCase):
    def test_diffuse_family_is_srgb(self):
        for name in ('diffusemap', 'DiffuseMap', 'diffuse', 'diff', 'tex_Diffuse', 'Diffuse'):
            self.assertTrue(is_w2_srgb_texture_param(name), name)

    def test_specular_family_is_srgb(self):
        for name in ('specularmap', 'specular', 'tex_Specular', 'SpecularTexture'):
            self.assertTrue(is_w2_srgb_texture_param(name), name)

    def test_normals_and_unknowns_are_not_srgb(self):
        for name in ('normalmap', 'Normal', 'tex_Normalmap', 'Glossiness', 'cubemap', ''):
            self.assertFalse(is_w2_srgb_texture_param(name), name)

    def test_canonical_key_lookup(self):
        self.assertEqual(canonical_w2_pin_key('Diffusemap'), 'diffuse')
        self.assertEqual(canonical_w2_pin_key('Ambientmap'), 'tintmask')
        self.assertEqual(canonical_w2_pin_key('nonsense'), '')


if __name__ == "__main__":
    unittest.main()
