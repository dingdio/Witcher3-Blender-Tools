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

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

if "witcher3_tools" not in sys.modules:
    _pkg = types.ModuleType("witcher3_tools")
    _pkg.__path__ = [str(REPO_ROOT / "witcher3_tools")]
    _pkg.__package__ = "witcher3_tools"
    sys.modules["witcher3_tools"] = _pkg
for _name, _sub in (("witcher3_tools.importers", "importers"),
                    ("witcher3_tools.CR2W", "CR2W"),
                    ("witcher3_tools.CR2W.witcher_cache", "CR2W/witcher_cache")):
    if _name not in sys.modules:
        _pkg = types.ModuleType(_name)
        _pkg.__path__ = [str(REPO_ROOT / "witcher3_tools" / _sub)]
        _pkg.__package__ = _name
        sys.modules[_name] = _pkg

from witcher3_tools.importers import terrain_detail as td

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

    def test_dx_normal_to_opengl_reconstructs_z(self):
        img = np.zeros((4, 4, 4), dtype=np.uint8)
        img[:, :, 0] = 128  # x ~ 0
        img[:, :, 1] = 128  # dx green
        img[:, :, 3] = 77
        out = td._dx_normal_to_opengl(img)
        self.assertTrue((out[:, :, 2] >= 254).all())   # z ~ 1
        self.assertTrue((out[:, :, 3] == 77).all())    # alpha (roughness) kept
        nx = out[:, :, 0] / 255.0 * 2 - 1
        ny = out[:, :, 1] / 255.0 * 2 - 1
        self.assertLess(np.abs(nx).max(), 0.01)
        self.assertLess(np.abs(ny).max(), 0.01)


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
                    uv_t = ((c[0] + ox) / res, (c[1] + oy) / res)
                    texel = _gl_closest(ctrl_gl, uv_t)
                    ov = min(max(int(round(texel[0])) - 1, 0), self.n_layers - 1)
                    bg = min(max(int(round(texel[1])) - 1, 0), self.n_layers - 1)
                    node_ov += w * self.slice_colors[ov]
                    node_bg += w * self.slice_colors[bg]
                np.testing.assert_allclose(node_ov, ref_ov, atol=1e-9)
                np.testing.assert_allclose(node_bg, ref_bg, atol=1e-9)

                uv_p = (u + 0.5 / res, v + 0.5 / res)
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
    def test_heightmap_normal_texel_alignment(self):
        res = 16
        for j in (0, 1, 7, 15):
            u = j / (res - 1)
            uv = u * (res - 1) / res + 0.5 / res
            q = uv * res - 0.5
            self.assertAlmostEqual(q, j, places=9)
            self.assertAlmostEqual(q - np.floor(q), 0.0, places=9)


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
            self.assertTrue((ctrl[:, :, 0] == (c & 0x1F)).all())
            self.assertTrue((ctrl[:, :, 1] == ((c >> 5) & 0x1F)).all())
            self.assertTrue((ctrl[:, :, 2] == ((c >> 13) & 0x7)).all())
            self.assertTrue((ctrl[:, :, 3] == np.where(c == 0, 0, 255)).all())
            normal = read_png(maps["normal"]).astype(np.float64) / 65535.0 * 2 - 1
            lengths = np.linalg.norm(normal, axis=-1)
            self.assertLess(np.abs(lengths - 1.0).max(), 0.01)
            self.assertGreater(normal[..., 2].min(), 0.0)


if __name__ == "__main__":
    unittest.main()
