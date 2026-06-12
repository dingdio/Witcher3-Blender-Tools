import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

if "witcher3_tools" not in sys.modules:
    _pkg = types.ModuleType("witcher3_tools")
    _pkg.__path__ = [str(REPO_ROOT / "witcher3_tools")]
    _pkg.__package__ = "witcher3_tools"
    sys.modules["witcher3_tools"] = _pkg

from witcher3_tools.unreal_export import texture_export
from witcher3_tools.unreal_export.manifest import (
    SCHEMA,
    build_manifest,
    convert_witcher_param,
    depot_asset_rel,
    texture_compression_for_param,
    texture_srgb_for_param,
)
from witcher3_tools.unreal_export.material_chain import ChainBuilder
from witcher3_tools.unreal_export.plugin_install import (
    PLUGIN_NAME,
    install_or_update_plugin,
    plugin_target_dir,
)
from witcher3_tools.unreal_export.socket_client import encode_message, import_bundle_request

for _name in [n for n in list(sys.modules) if n == "witcher3_tools" or n.startswith("witcher3_tools.")]:
    sys.modules.pop(_name, None)


def _stub_register_texture(raw_value, param_name):
    return {"depot": depot_asset_rel(raw_value), "rough_depot": None}


class TestDepotPaths(unittest.TestCase):
    def test_mesh_depot_path_maps_to_asset_rel(self):
        self.assertEqual(
            depot_asset_rel(r"characters\models\geralt\body\model\t_01_mg__body_hires.w2mesh"),
            "characters/models/geralt/body/model/t_01_mg__body_hires",
        )

    def test_master_graph_path_keeps_engine_layout(self):
        self.assertEqual(
            depot_asset_rel(r"engine\materials\graphs\pbr_std.w2mg"),
            "engine/materials/graphs/pbr_std",
        )

    def test_segments_are_sanitized_and_slashes_normalized(self):
        self.assertEqual(
            depot_asset_rel("characters/models/bad name/3start.xbm"),
            "characters/models/bad_name/_3start",
        )

    def test_texture_color_space_heuristics(self):
        self.assertTrue(texture_srgb_for_param("Diffuse"))
        self.assertFalse(texture_srgb_for_param("Normal"))
        self.assertEqual(texture_compression_for_param("Normal"), "normalmap")
        self.assertEqual(texture_compression_for_param("DetailNormal"), "normalmap")
        self.assertEqual(texture_compression_for_param("TintMask"), "masks")
        self.assertEqual(texture_compression_for_param("SpecularTexture"), "default")

    def test_extracted_rough_params_are_masks_not_normalmaps(self):
        # "<Name>Rough" textures come from a normal map's alpha; the rough/mask
        # check must outrank the "normal" token or the master material gets a
        # Normal sampler on a TC_Masks texture and fails to compile.
        self.assertEqual(texture_compression_for_param("NormalRough"), "masks")
        self.assertEqual(texture_compression_for_param("DetailNormalRough"), "masks")
        self.assertEqual(texture_compression_for_param("RoughnessTexture"), "masks")
        self.assertFalse(texture_srgb_for_param("NormalRough"))


class TestParamConversion(unittest.TestCase):
    def test_texture_param_resolves_to_depot_reference(self):
        entries, warnings = convert_witcher_param(
            "Diffuse",
            "handle:ITexture",
            r"characters\models\geralt\body\model\t_01_mg__body_hires_d01.xbm",
            _stub_register_texture,
        )
        self.assertEqual(warnings, [])
        self.assertEqual(entries, [{
            "name": "Diffuse",
            "kind": "texture",
            "depot": "characters/models/geralt/body/model/t_01_mg__body_hires_d01",
        }])

    def test_cr2w_color_params_convert_from_255_range(self):
        entries, _ = convert_witcher_param("SpecularColor", "Color", "255; 128; 0; 255", None)
        self.assertEqual(entries[0]["kind"], "vector")
        self.assertAlmostEqual(entries[0]["value"][0], 1.0)
        self.assertAlmostEqual(entries[0]["value"][1], 128 / 255.0)
        self.assertAlmostEqual(entries[0]["value"][2], 0.0)

    def test_blender_rgb_params_stay_linear(self):
        entries, _ = convert_witcher_param("SpecularColor", "RGB", "0.25 ; 0.5 ; 0.75 ; 1.0", None)
        self.assertEqual(entries[0]["value"], [0.25, 0.5, 0.75, 1.0])

    def test_vector_params_are_not_rescaled(self):
        entries, _ = convert_witcher_param("UVTiling", "Vector", "4.0; 4.0; 0.0; 1.0", None)
        self.assertEqual(entries[0]["value"], [4.0, 4.0, 0.0, 1.0])

    def test_texture_arrays_defer_with_warning(self):
        entries, warnings = convert_witcher_param("Pattern", "handle:CTextureArray", "foo.texarray", None)
        self.assertEqual(entries, [])
        self.assertTrue(any("deferred" in warning for warning in warnings))

    def test_normal_with_rough_sibling_adds_roughness_param(self):
        def register(raw_value, param_name):
            return {"depot": depot_asset_rel(raw_value), "rough_depot": depot_asset_rel(raw_value) + "_rough"}

        entries, _ = convert_witcher_param("Normal", "handle:ITexture", r"chars\n01.xbm", register)
        self.assertEqual([e["name"] for e in entries], ["Normal", "NormalRough"])


class TestChainBuilder(unittest.TestCase):
    """Simulates a Geralt body chain: local -> body material .w2mi -> base .w2mi -> pbr_std.w2mg."""

    BODY_MI = r"characters\models\geralt\body\materials\t_01_mg__body_hires.w2mi"
    BASE_MI = r"characters\models\geralt\body\materials\geralt_body_base.w2mi"
    GRAPH = r"engine\materials\graphs\pbr_std.w2mg"

    def _chain_reader(self, material_path, version):
        self.assertEqual(depot_asset_rel(material_path), depot_asset_rel(self.BODY_MI))
        return {
            "resolved_graph": self.GRAPH,
            "errors": [],
            "chain": [
                {"path": self.BODY_MI, "chunk_type": "CMaterialInstance", "_material_bin": "body_bin"},
                {"path": self.BASE_MI, "chunk_type": "CMaterialInstance", "_material_bin": "base_bin"},
                {"path": self.GRAPH, "chunk_type": "CMaterialGraph", "_material_bin": "graph_bin"},
            ],
        }

    def _params_reader(self, material_bin):
        return {
            "body_bin": {"Diffuse": ("handle:ITexture", r"characters\models\geralt\body\model\t_01_mg__body_hires_d01.xbm")},
            "base_bin": {"SpecularColor": ("Color", "255; 255; 255; 255")},
            "graph_bin": {"Roughness": ("Float", "0.7")},
        }.get(material_bin, {})

    def _make_builder(self):
        return ChainBuilder(
            _stub_register_texture,
            chain_reader=self._chain_reader,
            params_reader=self._params_reader,
            enable_mask_reader=lambda material_bin: False,
        )

    def _geralt_local_info(self):
        return {
            "name": "t_01_mg__body_hires_Material0",
            "material_slot_index": 0,
            "witcher_props": {
                "local": True,
                "base_custom": self.BODY_MI,
                "enableMask": False,
                "input_props": [
                    {"name": "Diffuse", "type": "TEX_IMAGE",
                     "value": r"characters\models\geralt\body\model\t_01_mg__body_hires_d01.xbm"},
                ],
            },
        }

    def test_chain_levels_emit_parent_first_at_depot_paths(self):
        builder = self._make_builder()
        local_id = builder.add_slot_material(
            self._geralt_local_info(), "characters/models/geralt/body/model"
        )

        materials = builder.ordered_materials()
        self.assertEqual(
            [m["asset_path"] for m in materials],
            [
                "characters/models/geralt/body/materials/geralt_body_base",
                "characters/models/geralt/body/materials/t_01_mg__body_hires",
                "characters/models/geralt/body/model/t_01_mg__body_hires_Material0",
            ],
        )
        self.assertEqual(local_id, "characters/models/geralt/body/model/t_01_mg__body_hires_Material0")

        base_body, body_material, local = materials
        self.assertEqual(base_body["parent_master"], "engine/materials/graphs/pbr_std")
        self.assertEqual(body_material["parent_material"], base_body["id"])
        self.assertEqual(local["parent_material"], body_material["id"])
        self.assertEqual(local["params"][0]["depot"],
                         "characters/models/geralt/body/model/t_01_mg__body_hires_d01")

    def test_master_declares_graph_defaults_and_instance_params(self):
        builder = self._make_builder()
        builder.add_slot_material(self._geralt_local_info(), "characters/models/geralt/body/model")

        masters = builder.ordered_masters()
        self.assertEqual(len(masters), 1)
        params_by_name = {p["name"]: p for p in masters[0]["params"]}
        self.assertIn("Roughness", params_by_name)
        self.assertEqual(params_by_name["Roughness"]["value"], 0.7)
        self.assertIn("Diffuse", params_by_name)
        self.assertIn("SpecularColor", params_by_name)

    def test_external_material_uses_chain_head_without_local_instance(self):
        builder = self._make_builder()
        slot_id = builder.add_slot_material(
            {
                "name": "t_01_mg__body_hires",
                "witcher_props": {
                    "local": False,
                    "base_custom": self.BODY_MI,
                    "input_props": [],
                },
            },
            "characters/models/geralt/body/model",
        )
        self.assertEqual(slot_id, "characters/models/geralt/body/materials/t_01_mg__body_hires")
        self.assertEqual(len(builder.ordered_materials()), 2)

    def test_second_slot_reuses_chain_entries(self):
        builder = self._make_builder()
        builder.add_slot_material(self._geralt_local_info(), "characters/models/geralt/body/model")
        other = self._geralt_local_info()
        other["name"] = "t_01_mg__body_hires_Material1"
        builder.add_slot_material(other, "characters/models/geralt/body/model")

        asset_paths = [m["asset_path"] for m in builder.ordered_materials()]
        self.assertEqual(len(asset_paths), len(set(asset_paths)))
        self.assertEqual(len([p for p in asset_paths if p.endswith("geralt_body_base")]), 1)

    def test_w2mg_base_parents_directly_to_master(self):
        builder = ChainBuilder(
            _stub_register_texture,
            chain_reader=lambda path, version: self.fail("w2mg base should not walk a chain"),
            params_reader=lambda material_bin: {},
        )
        builder.add_slot_material(
            {
                "name": "sword_mat",
                "witcher_props": {
                    "local": True,
                    "base_custom": r"engine\materials\graphs\pbr_std.w2mg",
                    "input_props": [{"name": "Diffuse", "type": "TEX_IMAGE", "value": r"items\sword_d01.xbm"}],
                },
            },
            "items/weapons/sword",
        )
        materials = builder.ordered_materials()
        self.assertEqual(len(materials), 1)
        self.assertEqual(materials[0]["parent_master"], "engine/materials/graphs/pbr_std")


class TestTextureRegistry(unittest.TestCase):
    def _make_registry(self, temp_dir):
        registry = texture_export.TextureRegistry(temp_dir)
        self._converted = []

        def fake_resolve(value):
            return "C:\\fake\\" + Path(str(value)).name

        def fake_convert(source_path, textures_dir, base_name):
            output = Path(textures_dir) / f"{base_name}.png"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"png")
            self._converted.append(str(output))
            return str(output)

        self._original = (
            texture_export.resolve_texture_path,
            texture_export.convert_texture_for_unreal,
            texture_export.extract_alpha_to_grayscale_png,
        )
        texture_export.resolve_texture_path = fake_resolve
        texture_export.convert_texture_for_unreal = fake_convert
        texture_export.extract_alpha_to_grayscale_png = lambda src, dst: False
        return registry

    def tearDown(self):
        if hasattr(self, "_original"):
            (
                texture_export.resolve_texture_path,
                texture_export.convert_texture_for_unreal,
                texture_export.extract_alpha_to_grayscale_png,
            ) = self._original

    def test_textures_export_once_with_flat_bundle_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = self._make_registry(temp_dir)
            depot = r"characters\models\geralt\body\model\t_01_mg__body_hires_d01.xbm"

            first = registry.register(depot, "Diffuse")
            second = registry.register(depot.replace("\\", "/"), "Diffuse")

            self.assertEqual(first, second)
            self.assertEqual(len(self._converted), 1)
            entries = registry.manifest_entries()
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["depot_path"],
                             "characters/models/geralt/body/model/t_01_mg__body_hires_d01")
            # Bundle files stay flat (depot-mirrored folders broke Windows
            # MAX_PATH in Blender); Unreal placement uses depot_path instead.
            self.assertEqual(entries[0]["file"], "Textures/t_01_mg__body_hires_d01.png")
            self.assertTrue(entries[0]["srgb"])

    def test_same_stem_from_different_depots_gets_unique_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = self._make_registry(temp_dir)

            first = registry.register(r"items\sword_a\diffuse.xbm", "Diffuse")
            second = registry.register(r"items\sword_b\diffuse.xbm", "Diffuse")

            self.assertNotEqual(first["depot"], second["depot"])
            files = sorted(entry["file"] for entry in registry.manifest_entries())
            self.assertEqual(files, ["Textures/diffuse.png", "Textures/diffuse_2.png"])


class TestManifest(unittest.TestCase):
    def test_manifest_schema_and_content_root(self):
        manifest = build_manifest(
            asset_name="geralt body",
            bundle_root=r"F:\exports\geralt",
            content_root="ImportedFbx/",
            meshes=[{"name": "t_01_mg__body_hires", "fbx": "Meshes/t_01_mg__body_hires.fbx",
                     "asset_path": "characters/models/geralt/body/model/t_01_mg__body_hires",
                     "kind": "skeletal", "slots": []}],
        )

        self.assertEqual(manifest["schema"], SCHEMA)
        self.assertEqual(manifest["content_root"], "/Game/ImportedFbx")
        self.assertEqual(manifest["asset_name"], "geralt_body")
        self.assertNotIn("rig", manifest)
        self.assertNotIn("blueprint", manifest)

    def test_manifest_includes_rig_and_blueprint_when_present(self):
        manifest = build_manifest(
            asset_name="geralt",
            bundle_root=r"F:\exports\geralt",
            rig={"name": "geralt", "fbx": "Meshes/geralt.fbx",
                 "asset_path": "characters/base_entities/geralt/geralt"},
            blueprint={"name": "geralt", "asset_path": "characters/npc_entities/main_npc/geralt",
                       "mesh_asset_paths": ["a", "b"]},
        )
        self.assertEqual(manifest["rig"]["asset_path"], "characters/base_entities/geralt/geralt")
        self.assertEqual(manifest["blueprint"]["name"], "geralt")


class TestSocketClient(unittest.TestCase):
    def test_socket_message_uses_little_endian_length_prefix(self):
        payload = import_bundle_request(r"F:\bundle\witcher_unreal_export.json")
        encoded = encode_message(payload)
        size = int.from_bytes(encoded[:4], byteorder="little", signed=False)
        body = encoded[4:]

        self.assertEqual(size, len(body))
        decoded = json.loads(body.decode("utf-8"))
        self.assertEqual(decoded["command"], "import_bundle")
        self.assertEqual(decoded["schema"], SCHEMA)


class TestPluginInstall(unittest.TestCase):
    def test_plugin_target_resolves_project_plugins_dir(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project_file = Path(temp_dir) / "WitcherTest.uproject"
            project_file.write_text("{}", encoding="utf-8")

            target = Path(plugin_target_dir(project_file))

            self.assertEqual(target, Path(temp_dir) / "Plugins" / PLUGIN_NAME)

    def test_install_plugin_copies_source_and_ignores_build_artifacts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project_file = root / "WitcherTest.uproject"
            project_file.write_text("{}", encoding="utf-8")

            source_dir = root / "SourcePlugin"
            (source_dir / "Source").mkdir(parents=True)
            (source_dir / "Binaries").mkdir()
            (source_dir / f"{PLUGIN_NAME}.uplugin").write_text("{}", encoding="utf-8")
            (source_dir / "Source" / "Importer.cpp").write_text("// source", encoding="utf-8")
            (source_dir / "Binaries" / "skip.dll").write_text("binary", encoding="utf-8")

            result = install_or_update_plugin(project_file, source_dir)
            target = Path(result["target_dir"])

            self.assertTrue(result["updated"])
            self.assertTrue((target / f"{PLUGIN_NAME}.uplugin").exists())
            self.assertTrue((target / "Source" / "Importer.cpp").exists())
            self.assertFalse((target / "Binaries").exists())

    def test_install_plugin_refuses_unexpected_existing_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project_file = root / "WitcherTest.uproject"
            project_file.write_text("{}", encoding="utf-8")

            source_dir = root / "SourcePlugin"
            source_dir.mkdir()
            (source_dir / f"{PLUGIN_NAME}.uplugin").write_text("{}", encoding="utf-8")

            target = root / "Plugins" / PLUGIN_NAME
            target.mkdir(parents=True)
            (target / "keep.txt").write_text("not our plugin", encoding="utf-8")

            with self.assertRaises(ValueError):
                install_or_update_plugin(project_file, source_dir)


if __name__ == "__main__":
    unittest.main()
