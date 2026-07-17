"""Terrain detail material tests."""

import os
import shutil
import struct
import sys
import tempfile
import types
import unittest
import zlib
from pathlib import Path
from unittest import mock

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

if "witcher3_tools" not in sys.modules:
    _pkg = types.ModuleType("witcher3_tools")
    _pkg.__path__ = [str(REPO_ROOT / "witcher3_tools")]
    _pkg.__package__ = "witcher3_tools"
    sys.modules["witcher3_tools"] = _pkg
for _name, _sub in (("witcher3_tools.importers", "importers"),
                    ("witcher3_tools.unreal_export", "unreal_export"),
                    ("witcher3_tools.CR2W", "CR2W"),
                    ("witcher3_tools.CR2W.witcher_cache", "CR2W/witcher_cache")):
    if _name not in sys.modules:
        _pkg = types.ModuleType(_name)
        _pkg.__path__ = [str(REPO_ROOT / "witcher3_tools" / _sub)]
        _pkg.__package__ = _name
        sys.modules[_name] = _pkg

from witcher3_tools.importers import terrain_detail as td
from witcher3_tools.unreal_export import terrain_material as terrain_material

for _name in [n for n in list(sys.modules)
              if n == "witcher3_tools" or n.startswith("witcher3_tools.")]:
    sys.modules.pop(_name, None)

REAL_TILES_DIR = os.environ.get("WITCHER_TERRAIN_TEST_DIR", "")
REAL_TEX_BUF = os.path.join(REAL_TILES_DIR, "tile_22_x_21_res512.w2ter.2.buffer")
REAL_HM_BUF = os.path.join(REAL_TILES_DIR, "tile_22_x_21_res512.w2ter.1.buffer")


def read_png(path):
    with open(path, "rb") as handle:
        data = handle.read()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, ihdr = 8, b"", None
    while pos < len(data):
        ln = struct.unpack(">I", data[pos:pos + 4])[0]
        tag = data[pos + 4:pos + 8]
        payload = data[pos + 8:pos + 8 + ln]
        if tag == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", payload)
        elif tag == b"IDAT":
            idat += payload
        pos += 12 + ln
    w, h, depth, ctype = ihdr[0], ihdr[1], ihdr[2], ihdr[3]
    raw = zlib.decompress(idat)
    ch = {0: 1, 2: 3, 6: 4}[ctype]
    stride = w * ch * (depth // 8) + 1
    rows = np.frombuffer(raw, np.uint8).reshape(h, stride)
    assert (rows[:, 0] == 0).all()
    px = rows[:, 1:]
    if depth == 16:
        pairs = px.reshape(h, w, ch, 2).astype(np.uint16)
        return pairs[..., 0] * 256 + pairs[..., 1]
    return px.reshape(h, w, ch)


def _bc4_reference(block):
    a0, a1 = int(block[0]), int(block[1])
    bits = int.from_bytes(bytes(block[2:8]), "little")
    pal = [float(a0), float(a1)]
    if a0 > a1:
        pal += [((7 - i) * a0 + i * a1) / 7.0 for i in range(1, 7)]
    else:
        pal += [((5 - i) * a0 + i * a1) / 5.0 for i in range(1, 5)] + [0.0, 255.0]
    out = []
    for k in range(16):
        code = (bits >> (3 * k)) & 7
        out.append(int(min(255, max(0, pal[code] + 0.5))))
    return out


class TestBCDecoding(unittest.TestCase):
    def test_bc4_matches_reference_both_modes(self):
        rng = np.random.default_rng(7)
        blocks = rng.integers(0, 256, size=(64, 8), dtype=np.uint8)
        blocks[:32, 0] = 200  # a0 > a1 mode
        blocks[:32, 1] = 10
        blocks[32:, 0] = 10   # 6-value mode
        blocks[32:, 1] = 200
        got = td._decode_bc4_plane(blocks)
        for i in range(blocks.shape[0]):
            self.assertEqual(list(got[i]), _bc4_reference(blocks[i]), f"block {i}")

    def test_bc3_shape_and_alpha_plane(self):
        alpha = bytes([255, 0] + [0] * 6)
        white = struct.pack("<HH", 0xFFFF, 0xFFFF) + b"\x00" * 4
        rgba = td._decode_bc3_to_rgba(alpha + white, 4, 4)
        self.assertEqual(rgba.shape, (4, 4, 4))
        self.assertTrue((rgba[:, :, 3] == 255).all())
        self.assertTrue((rgba[:, :, :3] == 255).all())

    def test_bc3_color_uses_four_color_mode(self):
        alpha = bytes([255, 0] + [0] * 6)
        color = struct.pack("<HHI", 0x0000, 0xFFFF, 0xAAAAAAAA)
        rgba = td._decode_bc3_to_rgba(alpha + color, 4, 4)
        self.assertTrue((rgba[:, :, :3] == 85).all())
        self.assertTrue((rgba[:, :, 3] == 255).all())

    def test_dx_normal_to_opengl_preserves_red_blue_and_alpha(self):
        img = np.zeros((4, 4, 4), dtype=np.uint8)
        img[:, :, 0] = 64
        img[:, :, 1] = 192
        img[:, :, 2] = 17
        img[:, :, 3] = 77
        out = td._dx_normal_to_opengl(img)
        self.assertTrue((out[:, :, 0] == 64).all())
        self.assertTrue((out[:, :, 1] == 63).all())
        self.assertTrue((out[:, :, 2] == 17).all())
        self.assertTrue((out[:, :, 3] == 77).all())


class TestTerrainMath(unittest.TestCase):
    def test_tangent_normal_uses_vertex_orthonormal_basis(self):
        root_half = np.sqrt(0.5)
        vertex = np.array([root_half, 0.0, root_half])
        np.testing.assert_allclose(
            td.combine_tangent_normal(vertex, (0.0, 0.0, 1.0)),
            vertex,
            atol=1e-12,
        )
        np.testing.assert_allclose(
            td.combine_tangent_normal(vertex, (1.0, 0.0, 0.0)),
            (root_half, 0.0, -root_half),
            atol=1e-12,
        )

    def test_slope_blend_uses_bounded_angle_approximation(self):
        angle = 0.5
        normal = np.array([np.sin(angle), 0.0, np.cos(angle)])
        slope = np.clip(np.tan(angle), 0.0, 1.0)
        self.assertAlmostEqual(
            float(td.compute_slope_blend(normal, 0.2, 0.4)),
            np.clip((slope - 0.2) / 0.4, 0.0, 1.0),
        )


        self.assertAlmostEqual(
            float(td.compute_slope_blend(normal, 0.9, 0.4)),
            np.clip((slope - 0.9) / 0.1, 0.0, 1.0),
        )

    def test_specularity_uses_packed_falloff_and_roughness(self):
        specularity = 0.5
        roughness = 0.25
        base = 0.5
        falloff = 0.4
        expected_f0 = specularity ** 2.2 * ((1.0 - roughness) * falloff)
        self.assertAlmostEqual(
            float(td.legacy_specular_ior_level(specularity, roughness, base, falloff)),
            expected_f0 / 0.08,
        )
        self.assertEqual(
            float(td.legacy_specular_ior_level(0.7, 0.2, 0.47, 0.0)), 0.0)

    def test_f0_maps_to_principled_ior_instead_of_clipping_at_eight_percent(self):
        expected_f0 = 0.5 ** 2.2 * ((1.0 - 0.25) * 0.4)
        self.assertAlmostEqual(
            float(td.terrain_specular_f0(0.5, 0.25, 0.5, 0.4)),
            expected_f0,
        )
        ior = float(td.f0_to_ior(expected_f0))
        reconstructed_f0 = ((ior - 1.0) / (ior + 1.0)) ** 2
        self.assertAlmostEqual(reconstructed_f0, expected_f0)

        # Direct F0 must retain the white reflection limit instead of the legacy
        # 0.08 multiplier cap.
        self.assertEqual(float(td.terrain_specular_f0(1.0, 1.0, 1.0, 1.0)), 1.0)
        self.assertEqual(float(td.f0_to_ior(1.0)), 1000.0)

    def test_diffuse_overlay_stays_gamma_until_final_decode(self):
        diffuse = np.array([0.5, 0.25, 0.8])
        tint = np.array([0.4, 0.75, 0.5])
        diffuse_gamma = td.overlay_gamma(diffuse, tint)
        np.testing.assert_allclose(diffuse_gamma, (0.4, 0.625, 0.8), atol=1e-12)
        np.testing.assert_allclose(
            td.gamma_to_linear(diffuse_gamma),
            np.power((0.4, 0.625, 0.8), 2.2),
            atol=1e-12,
        )

    def test_unset_layer_rows_match_engine_zero_constants(self):
        rows = td.normalize_layer_params([], 31)
        self.assertEqual(rows[30]["blend_sharpness"], 0.0)
        self.assertEqual(rows[30]["specularity"], 0.0)
        self.assertEqual(rows[30]["specularity_base"], 0.0)
        self.assertEqual(rows[30]["specularity_scale"], 0.0)

    def test_baked_parameter_maps_keep_both_layer_rspec_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            tex_buf = os.path.join(tmp, "tile.buffer")
            control = np.full((2, 2), 1 | (2 << 5), dtype="<u2")
            control.tofile(tex_buf)
            rows = [
                {**td.DEFAULT_LAYER_PARAMS, "specularity": 0.1,
                 "specularity_base": 0.2, "falloff": 0.3,
                 "specularity_scale": 0.4},
                {**td.DEFAULT_LAYER_PARAMS, "specularity": 0.5,
                 "specularity_base": 0.6, "falloff": 0.7,
                 "specularity_scale": 0.8},
            ]
            maps = td.build_tile_detail_maps(
                tex_buf, "", 2, 1.0, 1.0, rows,
                layer_count=2, skip_existing=False)
            params2 = np.flipud(read_png(maps["params2"]))[0, 0] / 255.0
            params3 = np.flipud(read_png(maps["params3"]))[0, 0] / 255.0
            np.testing.assert_allclose(params2, (0.1, 0.5, 0.2, 0.6), atol=0.5 / 255)
            np.testing.assert_allclose(params3, (0.3, 0.7, 0.4, 0.8), atol=0.5 / 255)


class TestTerrainFaceInspection(unittest.TestCase):
    @staticmethod
    def _control(horizontal, vertical, slope=0, scale=0):
        return int(horizontal) | (int(vertical) << 5) | (int(slope) << 10) | (int(scale) << 13)

    def test_layer_metadata_keeps_depot_name_and_atlas_index(self):
        layers = [
            types.SimpleNamespace(
                diffuse_source=r"environment\textures_tileable\soil\ground_wet.xbm",
                normal_source=r"environment\textures_tileable\soil\ground_wet_n.xbm",
                diffuse_dds=r"C:\cache\ground_wet.dds",
                normal_dds=r"C:\cache\ground_wet_n.dds",
                blend_sharpness=0.13,
            )
        ]

        metadata = td.build_terrain_layer_metadata(layers)

        self.assertEqual(metadata[0]["id"], 1)
        self.assertEqual(metadata[0]["atlas_index"], 0)
        self.assertEqual(metadata[0]["name"], "ground_wet")
        self.assertEqual(metadata[0]["diffuse_source"], layers[0].diffuse_source)
        self.assertAlmostEqual(metadata[0]["blend_sharpness"], 0.13)

    def test_control_word_decodes_all_packed_fields(self):
        decoded = td.decode_terrain_control(self._control(19, 17, slope=7, scale=3))

        self.assertEqual(decoded["horizontal_id"], 19)
        self.assertEqual(decoded["vertical_id"], 17)
        self.assertEqual(decoded["slope_index"], 7)
        self.assertEqual(decoded["slope_threshold"], 0.98)
        self.assertEqual(decoded["scale_index"], 3)
        self.assertEqual(decoded["vertical_uv_scale"], 0.025)
        self.assertFalse(decoded["hole"])

    def test_face_center_reports_four_weighted_shader_taps(self):
        lattice = np.zeros((3, 3), dtype=np.uint16)
        lattice[0, 0] = self._control(1, 2, slope=0, scale=0)
        lattice[0, 1] = self._control(3, 2, slope=2, scale=1)
        lattice[1, 0] = self._control(1, 4, slope=4, scale=2)
        lattice[1, 1] = self._control(3, 4, slope=6, scale=3)
        metadata = []
        for layer_id in range(1, 5):
            metadata.append({
                "id": layer_id,
                "atlas_index": layer_id - 1,
                "name": f"material_{layer_id}",
                "blend_sharpness": layer_id / 10.0,
                "slope_base_dampening": layer_id / 10.0,
                "slope_normal_dampening": 0.5,
            })

        # res=2 and UV=.25 means p=(.5,.5), exactly the center of these taps.
        result = td.inspect_terrain_control_lattice(lattice, 0.25, 0.25, metadata)

        self.assertEqual(result["cell"], (0, 0))
        self.assertEqual([tap["weight"] for tap in result["taps"]], [0.25] * 4)
        self.assertEqual(
            [(entry["layer"]["id"], entry["weight"])
             for entry in result["horizontal_layers"]],
            [(1, 0.5), (3, 0.5)],
        )
        self.assertEqual(
            [(entry["layer"]["id"], entry["weight"])
             for entry in result["vertical_layers"]],
            [(2, 0.5), (4, 0.5)],
        )
        self.assertAlmostEqual(result["effective"]["blend_sharpness"], 0.2)
        self.assertAlmostEqual(result["effective"]["slope_base_dampening"], 0.3)
        self.assertAlmostEqual(
            result["effective"]["slope_threshold"],
            sum((0.0, 0.25, 0.5, 0.75)) / 4.0,
        )

    def test_control_lattice_uses_positive_neighbor_edges(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = [os.path.join(tmp, f"control_{name}.buffer")
                     for name in ("current", "right", "up", "diagonal")]
            arrays = (
                np.array([[1, 2], [3, 4]], dtype="<u2"),
                np.array([[5, 6], [7, 8]], dtype="<u2"),
                np.array([[9, 10], [11, 12]], dtype="<u2"),
                np.array([[13, 14], [15, 16]], dtype="<u2"),
            )
            for path, values in zip(paths, arrays):
                values.tofile(path)

            lattice = td.load_tile_control_lattice(
                paths[0],
                2,
                positive_x_texture_buffer=paths[1],
                positive_y_texture_buffer=paths[2],
                positive_xy_texture_buffer=paths[3],
            )

        np.testing.assert_array_equal(
            lattice,
            np.array([[1, 2, 5], [3, 4, 7], [9, 10, 13]], dtype=np.uint16),
        )


class TestTerrainMaterialGraph(unittest.TestCase):
    def test_reads_authored_fresnel_power(self):
        fresnel = types.SimpleNamespace(Type="CMaterialBlockMathFresnel")
        fresnel.GetVariableByName = lambda name: (
            types.SimpleNamespace(Value=16.0) if name == "power" else None
        )
        cr2w = types.SimpleNamespace(
            CHUNKS=types.SimpleNamespace(CHUNKS=[fresnel]))

        diffuse, normal, power = terrain_material._terrain_material_properties(cr2w)

        self.assertEqual(diffuse, "")
        self.assertEqual(normal, "")
        self.assertEqual(power, 16.0)

    def test_missing_fresnel_block_keeps_engine_default(self):
        cr2w = types.SimpleNamespace(
            CHUNKS=types.SimpleNamespace(CHUNKS=[]))

        self.assertEqual(
            terrain_material._terrain_material_properties(cr2w)[2], 2.0)
        self.assertEqual(terrain_material.TerrainMaterialSet().fresnel_power, 2.0)

    def test_source_xbm_bitmap_fallback_is_converted_to_dds(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_xbm = Path(tmp) / "grass.xbm"
            converted_dds = Path(tmp) / "grass.dds"
            existing_dds = Path(tmp) / "rock.dds"
            source_xbm.write_bytes(b"CR2W")
            existing_dds.write_bytes(b"DDS ")

            def convert(path):
                self.assertEqual(Path(path), source_xbm)
                converted_dds.write_bytes(b"DDS ")
                return str(converted_dds)

            root_package = types.ModuleType("witcher3_tools")
            root_package.__path__ = []
            cr2w_package = types.ModuleType("witcher3_tools.CR2W")
            cr2w_package.__path__ = []
            common_blender = types.ModuleType("witcher3_tools.CR2W.common_blender")
            common_blender.repo_file = lambda path: path
            common_blender.win_safe_path = lambda path: path
            texture_converters = types.ModuleType(
                "witcher3_tools.CR2W.texture_converters")
            texture_converters.convert_xbm_to_dds = mock.Mock(side_effect=convert)
            cr2w_package.texture_converters = texture_converters

            with mock.patch.dict(
                sys.modules,
                {
                    "witcher3_tools": root_package,
                    "witcher3_tools.CR2W": cr2w_package,
                    "witcher3_tools.CR2W.common_blender": common_blender,
                    "witcher3_tools.CR2W.texture_converters": texture_converters,
                },
            ):
                resolved = terrain_material._resolved_existing_repo_files(
                    [str(source_xbm), str(existing_dds)])

            self.assertEqual(resolved, [str(converted_dds), str(existing_dds)])
            texture_converters.convert_xbm_to_dds.assert_called_once_with(str(source_xbm))


class TestAtlas(unittest.TestCase):
    def _fake_layers(self, tmp, n, px=16):
        rng = np.random.default_rng(3)
        layers = []
        for i in range(n):
            arr = rng.integers(0, 256, size=(px, px, 4), dtype=np.uint8)
            path = os.path.join(tmp, f"slice_{i}.dds")
            header = bytearray(128)
            header[:4] = b"DDS "
            struct.pack_into("<II", header, 12, px, px)
            header[84:88] = b"\x00" * 4
            struct.pack_into("<I", header, 88, 32)  # RGBA8
            with open(path, "wb") as f:
                f.write(bytes(header))
                f.write(arr.tobytes())
            layer = types.SimpleNamespace(diffuse_dds=path, normal_dds="")
            layer.pixels = arr
            layers.append(layer)
        return layers

    def test_pack_layout_and_gutter_wrap(self):
        with tempfile.TemporaryDirectory() as tmp:
            layers = self._fake_layers(tmp, 5, px=16)
            atlas = td.pack_world_detail_atlases("t", layers, tmp, slice_px=16,
                                                 skip_existing=False)
            lay = atlas["layout"]
            self.assertEqual((lay["cols"], lay["rows"]), (3, 2))
            img = read_png(atlas["diffuse"])
            g, sp, cp = lay["gutter_px"], lay["slice_px"], lay["cell_px"]
            for s, layer in enumerate(layers):
                row, col = divmod(s, lay["cols"])
                cell = img[row * cp + g:row * cp + g + sp, col * cp + g:col * cp + g + sp]
                src = layer.pixels.copy()
                src[:, :, 3] = 255
                self.assertTrue((cell == src).all(), f"slice {s} interior")
                left = img[row * cp + g:row * cp + g + sp, col * cp:col * cp + g]
                self.assertTrue((left == src[:, -g:]).all(), f"slice {s} gutter wrap")

    def test_pack_cache_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            layers = self._fake_layers(tmp, 3, px=16)
            a1 = td.pack_world_detail_atlases("t", layers, tmp, slice_px=16)
            stamp = os.path.getmtime(a1["diffuse"])
            a2 = td.pack_world_detail_atlases("t", layers, tmp, slice_px=16)
            self.assertEqual(os.path.getmtime(a2["diffuse"]), stamp)

    def test_cache_freshness_only_tracks_source_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source.bin")
            Path(source).touch()
            os.utime(source, (1_000_000_000, 1_000_000_000))
            self.assertEqual(td._max_mtime([source]), 1_000_000_000)


def _gl_closest(img, uv):
    n = img.shape[0]
    x = min(n - 1, max(0, int(np.floor(uv[0] * n))))
    y = min(n - 1, max(0, int(np.floor(uv[1] * n))))
    return img[y, x]  # row y counted from GL bottom


def _gl_bilinear(img, uv):
    n = img.shape[0]
    q = np.asarray(uv, np.float64) * n - 0.5
    c = np.floor(q).astype(int)
    f = q - c
    def tex(cx, cy):
        return img[min(n - 1, max(0, cy)), min(n - 1, max(0, cx))].astype(np.float64)
    return (tex(c[0], c[1]) * (1 - f[0]) * (1 - f[1])
            + tex(c[0] + 1, c[1]) * f[0] * (1 - f[1])
            + tex(c[0], c[1] + 1) * (1 - f[0]) * f[1]
            + tex(c[0] + 1, c[1] + 1) * f[0] * f[1])


class TestEngineEquivalence(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(11)
        self.res = 8
        self.n_layers = 6
        control = rng.integers(0, 2 ** 16, size=(self.res, self.res), dtype=np.uint16)
        control |= 0x21  # keep overlay/bkgrnd indices non-zero (no holes)
        control &= np.uint16(0x1F | (0x1F << 5) | (0x3F << 10))
        ov = np.clip(control & 0x1F, 1, self.n_layers).astype(np.uint16)
        bg = np.clip((control >> 5) & 0x1F, 1, self.n_layers).astype(np.uint16)
        self.control = (ov | (bg << 5) | (control & np.uint16(0x3F << 10))).astype(np.uint16)
        self.rows = [{k: float(v) for k, v in zip(
            ("blend_sharpness", "slope_base_dampening", "slope_normal_dampening",
             "falloff", "specularity", "specularity_base", "specularity_scale"),
            rng.uniform(0.0, 1.0, 7))} for _ in range(self.n_layers)]
        self.slice_colors = rng.uniform(0.05, 0.95, size=(self.n_layers, 3))

    def _engine_reference(self, u, v):
        res = self.res
        p = np.array([u * res, v * res])
        c = np.floor(p).astype(int)
        f = p - c
        wts = [(1 - f[0]) * (1 - f[1]), f[0] * (1 - f[1]),
               (1 - f[0]) * f[1], f[0] * f[1]]
        offs = [(0, 0), (1, 0), (0, 1), (1, 1)]
        ov_diff = np.zeros(3)
        bg_diff = np.zeros(3)
        thr = sharp = base_d = norm_d = 0.0
        for (ox, oy), w in zip(offs, wts):
            cx = min(res - 1, max(0, c[0] + ox))
            cy = min(res - 1, max(0, c[1] + oy))
            cc = int(self.control[cy, cx])  # row cy = data row (v axis)
            ov = min(max((cc & 0x1F) - 1, 0), self.n_layers - 1)
            bg = min(max(((cc >> 5) & 0x1F) - 1, 0), self.n_layers - 1)
            sl = (cc >> 10) & 7
            ov_diff += w * self.slice_colors[ov]
            bg_diff += w * self.slice_colors[bg]
            thr += w * td.SLOPE_THRESHOLD_LUT[sl]
            sharp += w * self.rows[ov]["blend_sharpness"]
            base_d += w * self.rows[bg]["slope_base_dampening"]
            norm_d += w * self.rows[bg]["slope_normal_dampening"]
        return ov_diff, bg_diff, thr, sharp, base_d, norm_d

    def test_baked_images_match_engine_taps(self):
        with tempfile.TemporaryDirectory() as tmp:
            tex_buf = os.path.join(tmp, "tile_0_x_0_res8.w2ter.2.buffer")
            self.control.astype("<u2").tofile(tex_buf)
            maps = td.build_tile_detail_maps(tex_buf, "", self.res, 16.0, 10.0,
                                             self.rows, layer_count=self.n_layers,
                                             skip_existing=False)
            ctrl_gl = np.flipud(read_png(maps["control"]))
            par_gl = np.flipud(read_png(maps["params"])).astype(np.float64) / 255.0

            rng = np.random.default_rng(5)
            res = self.res
            map_res = res + 1
            for _ in range(200):
                u, v = rng.uniform(0.0, 1.0, 2)
                ref_ov, ref_bg, ref_thr, ref_sharp, ref_bd, ref_nd = self._engine_reference(u, v)

                p = np.array([u * res, v * res])
                c = np.floor(p)
                f = p - c
                wts = [(1 - f[0]) * (1 - f[1]), f[0] * (1 - f[1]),
                       (1 - f[0]) * f[1], f[0] * f[1]]
                node_ov = np.zeros(3)
                node_bg = np.zeros(3)
                for (ox, oy), w in zip(((0.5, 0.5), (1.5, 0.5), (0.5, 1.5), (1.5, 1.5)), wts):
                    uv_t = ((c[0] + ox) / map_res, (c[1] + oy) / map_res)
                    texel = _gl_closest(ctrl_gl, uv_t)
                    ov = min(max(int(round(texel[0])) - 1, 0), self.n_layers - 1)
                    bg = min(max(int(round(texel[1])) - 1, 0), self.n_layers - 1)
                    node_ov += w * self.slice_colors[ov]
                    node_bg += w * self.slice_colors[bg]
                np.testing.assert_allclose(node_ov, ref_ov, atol=1e-9)
                np.testing.assert_allclose(node_bg, ref_bg, atol=1e-9)

                uv_p = ((u * res + 0.5) / map_res, (v * res + 0.5) / map_res)
                got = _gl_bilinear(par_gl, uv_p)
                self.assertAlmostEqual(got[0], ref_thr, delta=1.5 / 255)
                self.assertAlmostEqual(got[1] * td.PARAMS_SHARPNESS_SCALE, ref_sharp,
                                       delta=1.5 / 255 * td.PARAMS_SHARPNESS_SCALE)
                self.assertAlmostEqual(got[2], ref_bd, delta=1.5 / 255)
                self.assertAlmostEqual(got[3], ref_nd, delta=1.5 / 255)

    def test_scale_mux_matches_lut(self):
        for k in range(8):
            b0, b1, b2 = k & 1, (k >> 1) & 1, min((k >> 2), 1)
            s01 = td.UV_SCALE_LUT[0] * (1 - b0) + td.UV_SCALE_LUT[1] * b0
            s23 = td.UV_SCALE_LUT[2] * (1 - b0) + td.UV_SCALE_LUT[3] * b0
            s45 = td.UV_SCALE_LUT[4] * (1 - b0) + td.UV_SCALE_LUT[5] * b0
            s67 = td.UV_SCALE_LUT[6] * (1 - b0) + td.UV_SCALE_LUT[7] * b0
            lo = s01 * (1 - b1) + s23 * b1
            hi = s45 * (1 - b1) + s67 * b1
            self.assertAlmostEqual(lo * (1 - b2) + hi * b2, td.UV_SCALE_LUT[k])


class TestNormalMapAlignment(unittest.TestCase):
    def test_vertex_and_material_half_texel_offsets_cancel(self):
        res = 16
        map_res = res + 1
        for j in (0, 1, 7, 15):
            geometric_u = j / res
            region_u = geometric_u + 0.5 / res
            control_p = region_u * res - 0.5
            self.assertAlmostEqual(control_p, j, places=9)
            uv = (control_p + 0.5) / map_res
            q = uv * map_res - 0.5
            self.assertAlmostEqual(q, j, places=9)

    def test_heightmap_normal_texel_alignment(self):
        res = 16
        map_res = res + 1
        for j in (0, 1, 7, 16):
            u = j / res
            uv = (u * res + 0.5) / map_res
            q = uv * map_res - 0.5
            self.assertAlmostEqual(q, j, places=9)
            self.assertAlmostEqual(q - np.floor(q), 0.0, places=9)


class TestTileDetailStitching(unittest.TestCase):
    @staticmethod
    def _control(values):
        values = np.asarray(values, dtype=np.uint16)
        return (values | (values << 5) | (values << 10)).astype("<u2")

    @staticmethod
    def _rgba(seed, res):
        out = np.empty((res, res, 4), dtype=np.uint8)
        yy, xx = np.indices((res, res))
        out[..., 0] = seed + yy * 11 + xx
        out[..., 1] = seed + yy * 3 + xx * 7
        out[..., 2] = seed + yy + xx * 5
        out[..., 3] = 255
        return out

    def _write_tile(self, tmp, name, control, height, tint):
        tex = os.path.join(tmp, name + ".2.buffer")
        hm = os.path.join(tmp, name + ".1.buffer")
        color = os.path.join(tmp, name + ".3.buffer")
        np.asarray(control, dtype="<u2").tofile(tex)
        np.asarray(height, dtype="<u2").tofile(hm)
        np.asarray(tint, dtype=np.uint8).tofile(color)
        return tex, hm, color

    def test_positive_neighbor_maps_form_one_shared_lattice(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = 4
            rows = [dict(td.DEFAULT_LAYER_PARAMS) for _ in range(8)]
            tiles = {}
            for y in range(2):
                for x in range(3):
                    base = 1 + x * 2 + y
                    control = self._control(np.full((res, res), base, np.uint16))
                    height = (
                        np.arange(res * res, dtype=np.uint16).reshape(res, res)
                        + x * 100 + y * 1000
                    )
                    tint = self._rgba(10 + x * 30 + y * 70, res)
                    tiles[(x, y)] = self._write_tile(
                        tmp, f"tile_{y}_{x}", control, height, tint)

            def build(x, y):
                tex, hm, tint = tiles[(x, y)]
                right = tiles.get((x + 1, y), ("", "", ""))
                up = tiles.get((x, y + 1), ("", "", ""))
                diagonal = tiles.get((x + 1, y + 1), ("", "", ""))
                return td.build_tile_detail_maps(
                    tex, hm, res, 16.0, 100.0, rows,
                    layer_count=len(rows), tint_buffer=tint, skip_existing=False,
                    positive_x_texture_buffer=right[0],
                    positive_y_texture_buffer=up[0],
                    positive_xy_texture_buffer=diagonal[0],
                    positive_x_heightmap_buffer=right[1],
                    positive_y_heightmap_buffer=up[1],
                    positive_xy_heightmap_buffer=diagonal[1],
                    positive_x_tint_buffer=right[2],
                    positive_y_tint_buffer=up[2],
                    positive_xy_tint_buffer=diagonal[2],
                )

            left = build(0, 0)
            right = build(1, 0)
            self.assertEqual(left["res"], res)
            self.assertEqual(left["map_res"], res + 1)
            self.assertEqual(left["tint_map_res"], res + 1)

            for key in ("control", "params", "params2", "params3", "normal", "tint"):
                left_img = np.flipud(read_png(left[key]))
                right_img = np.flipud(read_png(right[key]))
                self.assertEqual(left_img.shape[:2], (res + 1, res + 1))
                np.testing.assert_array_equal(
                    left_img[:, -1], right_img[:, 0], err_msg=key)

            control = np.flipud(read_png(left["control"]))
            up_control = np.fromfile(tiles[(0, 1)][0], "<u2").reshape(res, res)
            diagonal_control = np.fromfile(tiles[(1, 1)][0], "<u2").reshape(res, res)
            np.testing.assert_array_equal(control[-1, :-1, 0], up_control[0] & 0x1F)
            self.assertEqual(int(control[-1, -1, 0]), int(diagonal_control[0, 0] & 0x1F))

    def test_missing_neighbors_repeat_outer_positive_edges(self):
        current = np.array([[1, 2], [3, 4]], dtype=np.uint16)
        stitched = td._stitch_positive_edges(current)
        np.testing.assert_array_equal(
            stitched,
            np.array([[1, 2, 2], [3, 4, 4], [3, 4, 4]], dtype=np.uint16),
        )


class TestFullMapDetailMaps(unittest.TestCase):
    def test_combines_tiles_with_capped_resolution_and_preserves_holes(self):
        with tempfile.TemporaryDirectory() as tmp:
            buffers = {}
            rows = []
            for index in range(4):
                rows.append({
                    **td.DEFAULT_LAYER_PARAMS,
                    "blend_sharpness": 0.2 + index * 0.1,
                    "specularity": 0.1 + index * 0.1,
                })
            for y in range(2):
                for x in range(2):
                    overlay = 1 + x + y * 2
                    background = 4 - x - y * 2
                    slope = x + y * 2
                    scale = 7 - slope
                    value = overlay | (background << 5) | (slope << 10) | (scale << 13)
                    control = np.full((4, 4), value, dtype="<u2")
                    if (x, y) == (0, 0):
                        control[1, 1] = 0
                    path = os.path.join(tmp, f"tile_{y}_x_{x}.buffer")
                    control.tofile(path)
                    buffers[(x, y)] = path

            maps = td.build_fullmap_detail_maps(
                buffers, 4, 2, 2, rows, tmp, "world",
                layer_count=4, target_res=4, skip_existing=False)

            self.assertEqual(maps["res"], 4)
            self.assertTrue(maps["has_holes"])
            control = np.flipud(read_png(maps["control"]))
            params = np.flipud(read_png(maps["params"]))

            self.assertTrue((control[0:2, 0:2, 0] == 1).all())
            self.assertTrue((control[0:2, 2:4, 0] == 2).all())
            self.assertTrue((control[2:4, 0:2, 0] == 3).all())
            self.assertTrue((control[2:4, 2:4, 0] == 4).all())
            self.assertEqual(int(control[0, 0, 3]), 0)
            self.assertEqual(int(control[0, 1, 3]), 255)
            self.assertEqual(int(control[3, 3, 2]), 4)
            self.assertAlmostEqual(
                params[3, 3, 0] / 255.0,
                td.SLOPE_THRESHOLD_LUT[3],
                delta=1.0 / 255.0,
            )


@unittest.skipUnless(os.path.isfile(REAL_TEX_BUF), "real Novigrad tile not available")
class TestRealTile(unittest.TestCase):
    def test_real_tile_maps_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            tex_buf = os.path.join(tmp, os.path.basename(REAL_TEX_BUF))
            hm_buf = os.path.join(tmp, os.path.basename(REAL_HM_BUF))
            shutil.copyfile(REAL_TEX_BUF, tex_buf)
            shutil.copyfile(REAL_HM_BUF, hm_buf)
            rows = [dict(td.DEFAULT_LAYER_PARAMS) for _ in range(31)]
            maps = td.build_tile_detail_maps(
                tex_buf, hm_buf, 512, 187.5, 360.0,
                rows, layer_count=31, skip_existing=False)
            self.assertEqual(maps["hole_count"], 83)
            ctrl = np.flipud(read_png(maps["control"]))
            c = np.fromfile(tex_buf, dtype="<u2").reshape(512, 512)
            self.assertTrue((ctrl[:512, :512, 0] == (c & 0x1F)).all())
            self.assertTrue((ctrl[:512, :512, 1] == ((c >> 5) & 0x1F)).all())
            self.assertTrue((ctrl[:512, :512, 2] == ((c >> 13) & 0x7)).all())
            self.assertTrue((ctrl[:512, :512, 3] == np.where(c == 0, 0, 255)).all())
            normal = read_png(maps["normal"]).astype(np.float64) / 65535.0 * 2 - 1
            lengths = np.linalg.norm(normal, axis=-1)
            self.assertLess(np.abs(lengths - 1.0).max(), 0.01)
            self.assertGreater(normal[..., 2].min(), 0.0)


if __name__ == "__main__":
    unittest.main()
