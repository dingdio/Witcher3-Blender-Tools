"""Build the EEVEE node graph for texarray terrain blending."""

from __future__ import annotations

import hashlib
import os

import bpy

from .terrain_detail import (
    DETAIL_VERSION,
    OVERLAY_UV_SCALE,
    PARAMS_SHARPNESS_SCALE,
    TRIPLANAR_TIGHTEN,
    UV_SCALE_LUT,
)

NODE_VERSION = 1
_EPS = 1e-3
_UP = (0.0, 0.0, 1.0)


def _sig(parts) -> str:
    return hashlib.sha1(repr(parts).encode("utf-8", errors="replace")).hexdigest()


def _stamp(path: str) -> str:
    try:
        return f"{path}:{os.path.getmtime(path)}"
    except OSError:
        return str(path)


def _image_for(path: str, colorspace: str, alpha_mode: str = "CHANNEL_PACKED"):
    if not path or not os.path.isfile(path):
        return None
    key = os.path.normcase(os.path.normpath(path))
    image = None
    for img in bpy.data.images:
        try:
            existing = os.path.normcase(os.path.normpath(bpy.path.abspath(img.filepath)))
        except Exception:
            continue
        if existing == key:
            image = img
            break
    if image is None:
        image = bpy.data.images.load(path, check_existing=True)
    try:
        stamp = str(os.path.getmtime(path))
    except OSError:
        stamp = ""
    if image.get("w3_detail_stamp") != stamp:
        try:
            image.reload()
        except Exception:
            pass
        image["w3_detail_stamp"] = stamp
    image.colorspace_settings.name = colorspace
    image.alpha_mode = alpha_mode
    return image


class _G:

    def __init__(self, tree):
        self.t = tree
        self.x = 0
        self.y = 0

    def node(self, bl_idname, x=None, **props):
        n = self.t.nodes.new(bl_idname)
        n.location = (self.x if x is None else x, self.y)
        self.y -= 160
        if self.y < -4000:
            self.y = 0
            self.x += 260
        for k, v in props.items():
            setattr(n, k, v)
        return n

    def link(self, out_sock, in_sock):
        self.t.links.new(out_sock, in_sock)

    def _wire(self, node, index, value):
        if value is None:
            return
        if hasattr(value, "is_linked"):  # a socket
            self.link(value, node.inputs[index])
        else:
            node.inputs[index].default_value = value

    def math(self, op, a=None, b=None, clamp=False):
        n = self.node("ShaderNodeMath", operation=op, use_clamp=clamp)
        self._wire(n, 0, a)
        self._wire(n, 1, b)
        return n.outputs[0]

    def vmath(self, op, a=None, b=None, c=None, scale=None):
        n = self.node("ShaderNodeVectorMath", operation=op)
        self._wire(n, 0, a)
        self._wire(n, 1, b)
        self._wire(n, 2, c)
        if scale is not None:
            self._wire(n, 3, scale)
        return n.outputs[0]

    def mix_f(self, fac, a, b):
        n = self.node("ShaderNodeMix", data_type="FLOAT")
        self._wire(n, 0, fac)
        self._wire(n, 2, a)
        self._wire(n, 3, b)
        return n.outputs[0]

    def mix_v(self, fac, a, b):
        n = self.node("ShaderNodeMix", data_type="VECTOR")
        self._wire(n, 0, fac)
        self._wire(n, 4, a)
        self._wire(n, 5, b)
        return n.outputs[1]

    def sep(self, vec):
        n = self.node("ShaderNodeSeparateXYZ")
        self.link(vec, n.inputs[0])
        return n.outputs[0], n.outputs[1], n.outputs[2]

    def comb(self, x=None, y=None, z=None):
        n = self.node("ShaderNodeCombineXYZ")
        self._wire(n, 0, x)
        self._wire(n, 1, y)
        self._wire(n, 2, z)
        return n.outputs[0]

    def sep_color(self, col):
        n = self.node("ShaderNodeSeparateColor")
        self.link(col, n.inputs[0])
        return n.outputs[0], n.outputs[1], n.outputs[2]

    def image(self, img, uv, interpolation="Linear", extension="REPEAT"):
        n = self.node("ShaderNodeTexImage", interpolation=interpolation, extension=extension)
        n.image = img
        self.link(uv, n.inputs[0])
        return n.outputs[0], n.outputs[1]

    def group(self, tree):
        n = self.node("ShaderNodeGroup")
        n.node_tree = tree
        return n


def _ensure_group(name: str, content_sig: str, builder):
    existing = bpy.data.node_groups.get(name)
    if existing is not None and existing.get("w3_detail_sig") == content_sig:
        return existing
    if existing is not None:
        existing.name = existing.name + ".old"
    tree = bpy.data.node_groups.new(name, "ShaderNodeTree")
    tree["w3_detail_sig"] = content_sig
    builder(tree)
    return tree


def _purge_unused_detail_groups():
    prefixes = (".W3AtlasUV ", ".W3TerrainTap ", ".W3TerrainDetail ")
    removed = True
    while removed:
        removed = False
        for tree in list(bpy.data.node_groups):
            if tree.users == 0 and tree.name.startswith(prefixes):
                bpy.data.node_groups.remove(tree)
                removed = True


def _atlas_uv_builder(layout):
    def build(tree):
        tree.interface.new_socket("Slice", in_out="INPUT", socket_type="NodeSocketFloat")
        tree.interface.new_socket("U", in_out="INPUT", socket_type="NodeSocketFloat")
        tree.interface.new_socket("V", in_out="INPUT", socket_type="NodeSocketFloat")
        tree.interface.new_socket("UV", in_out="OUTPUT", socket_type="NodeSocketVector")
        g = _G(tree)
        n_in = g.node("NodeGroupInput", x=-460)
        g.x = 0

        cols = float(layout["cols"])
        cell_u = layout["cell_px"] / layout["atlas_w"]
        cell_v = layout["cell_px"] / layout["atlas_h"]
        inner_u = layout["slice_px"] / layout["atlas_w"]
        inner_v = layout["slice_px"] / layout["atlas_h"]
        g_u = layout["gutter_px"] / layout["atlas_w"]
        g_v = layout["gutter_px"] / layout["atlas_h"]

        slice_s = n_in.outputs["Slice"]
        col = g.math("MODULO", slice_s, cols)
        row = g.math("FLOOR", g.math("DIVIDE", slice_s, cols))
        fu = g.math("FRACT", n_in.outputs["U"])
        fv = g.math("FRACT", n_in.outputs["V"])

        u = g.math("ADD", g.math("MULTIPLY", col, cell_u),
                   g.math("ADD", g.math("MULTIPLY", fu, inner_u), g_u))
        # Atlas rows are stored top-down; shader V is bottom-up.
        v = g.math("ADD", g.math("SUBTRACT", 1.0 - g_v - inner_v,
                                 g.math("MULTIPLY", row, cell_v)),
                   g.math("MULTIPLY", fv, inner_v))

        uv = g.comb(x=u, y=v, z=0.0)
        n_out = g.node("NodeGroupOutput", x=g.x + 320)
        g.link(uv, n_out.inputs["UV"])
    return build


def _tap_builder(layout, atlas_d, atlas_n, atlas_uv_tree):
    n_slices = max(1, int(layout["n_slices"]))
    has_normals = atlas_n is not None

    def build(tree):
        for name in ("CtrlR", "CtrlG", "CtrlB"):
            tree.interface.new_socket(name, in_out="INPUT", socket_type="NodeSocketFloat")
        tree.interface.new_socket("TexPos", in_out="INPUT", socket_type="NodeSocketVector")
        tree.interface.new_socket("TriW", in_out="INPUT", socket_type="NodeSocketVector")
        for name, stype in (
            ("OvDiff", "NodeSocketVector"), ("BgDiff", "NodeSocketVector"),
            ("OvNormal", "NodeSocketVector"), ("BgNormal", "NodeSocketVector"),
            ("OvRough", "NodeSocketFloat"), ("BgRough", "NodeSocketFloat"),
        ):
            tree.interface.new_socket(name, in_out="OUTPUT", socket_type=stype)

        g = _G(tree)
        n_in = g.node("NodeGroupInput", x=-520)
        g.x = 0

        def idx_from(chan):
            raw = g.math("ROUND", g.math("MULTIPLY", chan, 255.0))
            return g.math("MINIMUM", g.math("MAXIMUM", g.math("SUBTRACT", raw, 1.0), 0.0),
                          float(n_slices - 1))

        ov_slice = idx_from(n_in.outputs["CtrlR"])
        bg_slice = idx_from(n_in.outputs["CtrlG"])
        k = g.math("ROUND", g.math("MULTIPLY", n_in.outputs["CtrlB"], 255.0))

        b0 = g.math("MODULO", k, 2.0)
        k2 = g.math("FLOOR", g.math("DIVIDE", k, 2.0))
        b1 = g.math("MODULO", k2, 2.0)
        b2 = g.math("MINIMUM", g.math("FLOOR", g.math("DIVIDE", k2, 2.0)), 1.0)
        L = UV_SCALE_LUT
        s01 = g.mix_f(b0, L[0], L[1])
        s23 = g.mix_f(b0, L[2], L[3])
        s45 = g.mix_f(b0, L[4], L[5])
        s67 = g.mix_f(b0, L[6], L[7])
        scale = g.mix_f(b2, g.mix_f(b1, s01, s23), g.mix_f(b1, s45, s67))

        px, py, pz = g.sep(n_in.outputs["TexPos"])
        ny_neg = g.math("MULTIPLY", py, -1.0)
        nz_neg = g.math("MULTIPLY", pz, -1.0)

        def atlas_uv(slice_s, u, v):
            n = g.group(atlas_uv_tree)
            g.link(slice_s, n.inputs["Slice"])
            g.link(u, n.inputs["U"])
            g.link(v, n.inputs["V"])
            return n.outputs["UV"]

        def decode_normal(col, orientation):
            n = g.vmath("MULTIPLY_ADD", col, (2.0, 2.0, 2.0), (-1.0, -1.0, -1.0))
            nx, ny, nz = g.sep(n)
            if orientation == "top":
                return g.comb(x=nx, y=g.math("MULTIPLY", ny, -1.0), z=nz)
            if orientation == "side_y":
                return g.comb(x=nx, y=g.math("MAXIMUM", nz, _EPS),
                              z=g.math("MULTIPLY", ny, -1.0))
            return g.comb(x=g.math("MAXIMUM", nz, _EPS), y=nx,
                          z=g.math("MULTIPLY", ny, -1.0))

        ov_uv = atlas_uv(ov_slice,
                         g.math("MULTIPLY", px, OVERLAY_UV_SCALE),
                         g.math("MULTIPLY", ny_neg, OVERLAY_UV_SCALE))
        ov_diff, _ = g.image(atlas_d, ov_uv)

        twx, twy, twz = g.sep(n_in.outputs["TriW"])
        uv_top = atlas_uv(bg_slice, g.math("MULTIPLY", px, scale),
                          g.math("MULTIPLY", ny_neg, scale))
        uv_sy = atlas_uv(bg_slice, g.math("MULTIPLY", px, scale),
                         g.math("MULTIPLY", nz_neg, scale))
        uv_sx = atlas_uv(bg_slice, g.math("MULTIPLY", py, scale),
                         g.math("MULTIPLY", nz_neg, scale))
        d_top, _ = g.image(atlas_d, uv_top)
        d_sy, _ = g.image(atlas_d, uv_sy)
        d_sx, _ = g.image(atlas_d, uv_sx)
        bg_diff = g.vmath("ADD",
                          g.vmath("ADD",
                                  g.vmath("SCALE", d_top, scale=twz),
                                  g.vmath("SCALE", d_sy, scale=twy)),
                          g.vmath("SCALE", d_sx, scale=twx))

        n_out = g.node("NodeGroupOutput", x=g.x + 900)
        g.link(ov_diff, n_out.inputs["OvDiff"])
        g.link(bg_diff, n_out.inputs["BgDiff"])

        if has_normals:
            ovn_col, ovn_a = g.image(atlas_n, ov_uv)
            ov_normal = decode_normal(ovn_col, "top")
            n_top, a_top = g.image(atlas_n, uv_top)
            n_sy, a_sy = g.image(atlas_n, uv_sy)
            n_sx, a_sx = g.image(atlas_n, uv_sx)
            bg_normal = g.vmath("ADD",
                                g.vmath("ADD",
                                        g.vmath("SCALE", decode_normal(n_top, "top"), scale=twz),
                                        g.vmath("SCALE", decode_normal(n_sy, "side_y"), scale=twy)),
                                g.vmath("SCALE", decode_normal(n_sx, "side_x"), scale=twx))
            bg_rough = g.math("ADD",
                              g.math("ADD",
                                     g.math("MULTIPLY", a_top, twz),
                                     g.math("MULTIPLY", a_sy, twy)),
                              g.math("MULTIPLY", a_sx, twx))
            g.link(ov_normal, n_out.inputs["OvNormal"])
            g.link(bg_normal, n_out.inputs["BgNormal"])
            g.link(ovn_a, n_out.inputs["OvRough"])
            g.link(bg_rough, n_out.inputs["BgRough"])
        else:
            n_out.inputs["OvNormal"].default_value = _UP
            n_out.inputs["BgNormal"].default_value = _UP
            n_out.inputs["OvRough"].default_value = 0.9
            n_out.inputs["BgRough"].default_value = 0.9
    return build


def _main_builder(res, tap_tree, images, has_holes):
    control_img = images["control"]
    params_img = images["params"]
    params2_img = images["params2"]
    normal_img = images.get("normal")
    tint_img = images.get("tint")

    def build(tree):
        tree.interface.new_socket("Normal Strength", in_out="INPUT",
                                  socket_type="NodeSocketFloat").default_value = 1.0
        for name, stype in (
            ("BaseColor", "NodeSocketColor"), ("Normal", "NodeSocketVector"),
            ("Roughness", "NodeSocketFloat"), ("Specular", "NodeSocketFloat"),
            ("Alpha", "NodeSocketFloat"),
        ):
            tree.interface.new_socket(name, in_out="OUTPUT", socket_type=stype)

        g = _G(tree)
        n_in = g.node("NodeGroupInput", x=-560)
        uv_node = g.node("ShaderNodeUVMap", x=-560, uv_map="UVMap")
        geo = g.node("ShaderNodeNewGeometry", x=-560)
        g.x = 0
        uv = uv_node.outputs["UV"]
        strength = n_in.outputs["Normal Strength"]

        inv_res = 1.0 / float(res)
        p = g.vmath("MULTIPLY", uv, (float(res), float(res), 1.0))
        c = g.vmath("FLOOR", p)
        f = g.vmath("SUBTRACT", p, c)
        fx, fy, _ = g.sep(f)
        ix = g.math("SUBTRACT", 1.0, fx)
        iy = g.math("SUBTRACT", 1.0, fy)
        weights = (
            g.math("MULTIPLY", ix, iy),  # (0,0)
            g.math("MULTIPLY", fx, iy),  # (1,0)
            g.math("MULTIPLY", ix, fy),  # (0,1)
            g.math("MULTIPLY", fx, fy),  # (1,1)
        )
        offsets = ((0.5, 0.5), (1.5, 0.5), (0.5, 1.5), (1.5, 1.5))

        if normal_img is not None:
            k = (res - 1.0) / res
            uv_n = g.vmath("ADD", g.vmath("MULTIPLY", uv, (k, k, 1.0)),
                           (0.5 * inv_res, 0.5 * inv_res, 0.0))
            n_col, _ = g.image(normal_img, uv_n, extension="EXTEND")
            macro_n = g.vmath("NORMALIZE",
                              g.vmath("MULTIPLY_ADD", n_col, (2.0, 2.0, 2.0),
                                      (-1.0, -1.0, -1.0)))
        else:
            macro_n = geo.outputs["Normal"]

        tri_raw = g.vmath("MAXIMUM",
                          g.vmath("SUBTRACT", g.vmath("ABSOLUTE", macro_n),
                                  (TRIPLANAR_TIGHTEN,) * 3),
                          (0.0, 0.0, 0.0))
        tri_sum = g.math("MAXIMUM", g.vmath("DOT_PRODUCT", tri_raw, (1.0, 1.0, 1.0)), 1e-4)
        tri_w = g.vmath("DIVIDE", tri_raw, g.comb(x=tri_sum, y=tri_sum, z=tri_sum))

        uv_p = g.vmath("ADD", uv, (0.5 * inv_res, 0.5 * inv_res, 0.0))
        p_col, p_alpha = g.image(params_img, uv_p, extension="EXTEND")
        thr, sharp_n, base_damp = g.sep_color(p_col)
        sharp = g.math("MULTIPLY", sharp_n, PARAMS_SHARPNESS_SCALE)
        norm_damp = p_alpha
        p2_col, _ = g.image(params2_img, uv_p, extension="EXTEND")
        spec_ov, spec_bg, _unused = g.sep_color(p2_col)

        acc = None
        for (ox, oy), w in zip(offsets, weights):
            uv_t = g.vmath("MULTIPLY", g.vmath("ADD", c, (ox, oy, 0.0)),
                           (inv_res, inv_res, 1.0))
            ctrl_col, ctrl_a = g.image(control_img, uv_t, interpolation="Closest",
                                       extension="EXTEND")
            cr, cg, cb = g.sep_color(ctrl_col)
            tap = g.group(tap_tree)
            g.link(cr, tap.inputs["CtrlR"])
            g.link(cg, tap.inputs["CtrlG"])
            g.link(cb, tap.inputs["CtrlB"])
            g.link(geo.outputs["Position"], tap.inputs["TexPos"])
            g.link(tri_w, tap.inputs["TriW"])
            part = {
                "ov_diff": g.vmath("SCALE", tap.outputs["OvDiff"], scale=w),
                "bg_diff": g.vmath("SCALE", tap.outputs["BgDiff"], scale=w),
                "ov_n": g.vmath("SCALE", tap.outputs["OvNormal"], scale=w),
                "bg_n": g.vmath("SCALE", tap.outputs["BgNormal"], scale=w),
                "ov_r": g.math("MULTIPLY", tap.outputs["OvRough"], w),
                "bg_r": g.math("MULTIPLY", tap.outputs["BgRough"], w),
                "alpha": g.math("MULTIPLY", ctrl_a, w),
            }
            if acc is None:
                acc = part
            else:
                for key in acc:
                    if key in ("ov_r", "bg_r", "alpha"):
                        acc[key] = g.math("ADD", acc[key], part[key])
                    else:
                        acc[key] = g.vmath("ADD", acc[key], part[key])

        ov_diff, bg_diff = acc["ov_diff"], acc["bg_diff"]
        ov_n = g.vmath("NORMALIZE", acc["ov_n"])
        bg_n = g.vmath("NORMALIZE", acc["bg_n"])

        def apply_strength(nrm):
            nx, ny, nz = g.sep(nrm)
            return g.vmath("NORMALIZE", g.comb(
                x=g.math("MULTIPLY", nx, strength),
                y=g.math("MULTIPLY", ny, strength),
                z=g.math("MAXIMUM", nz, _EPS)))

        ov_ns = apply_strength(ov_n)
        bg_ns = apply_strength(bg_n)
        bg_nd = g.vmath("NORMALIZE", g.mix_v(norm_damp, _UP, bg_ns))

        mx, my, mz = g.sep(macro_n)
        vertex_flat = g.math("MAXIMUM", g.math("MINIMUM", mz, 1.0), 0.0)
        flat_bg = g.mix_v(vertex_flat, bg_n, _UP)
        biased = g.vmath("NORMALIZE", g.mix_v(base_damp, bg_n, flat_bg))
        bx, by, bz = g.sep(biased)
        slope = g.math("MINIMUM",
                       g.math("DIVIDE",
                              g.math("ADD", g.math("ABSOLUTE", bx), g.math("ABSOLUTE", by)),
                              g.math("MAXIMUM", g.math("ABSOLUTE", bz), _EPS)),
                       1.0)
        blend = g.math("DIVIDE", g.math("SUBTRACT", slope, thr),
                       g.math("MAXIMUM", sharp, _EPS), clamp=True)

        diff = g.mix_v(blend, ov_diff, bg_diff)
        detail_n = g.vmath("NORMALIZE", g.mix_v(blend, ov_ns, bg_nd))
        rough = g.mix_f(blend, acc["ov_r"], acc["bg_r"])
        spec = g.mix_f(blend, spec_ov, spec_bg)

        dx, dy, dz = g.sep(detail_n)
        final_n = g.vmath("NORMALIZE", g.comb(
            x=g.math("ADD", mx, dx),
            y=g.math("ADD", my, dy),
            z=g.math("MULTIPLY", g.math("MAXIMUM", mz, _EPS),
                     g.math("MAXIMUM", dz, _EPS))))

        if tint_img is not None:
            t_col, _ = g.image(tint_img, uv, extension="EXTEND")
            darken = g.vmath("SCALE", g.vmath("MULTIPLY", t_col, diff), scale=2.0)
            one = (1.0, 1.0, 1.0)
            screen = g.vmath("SUBTRACT", one,
                             g.vmath("SCALE",
                                     g.vmath("MULTIPLY",
                                             g.vmath("SUBTRACT", one, t_col),
                                             g.vmath("SUBTRACT", one, diff)),
                                     scale=2.0))
            tr, tg, tb = g.sep(t_col)
            mask = g.comb(x=g.math("GREATER_THAN", tr, 0.5),
                          y=g.math("GREATER_THAN", tg, 0.5),
                          z=g.math("GREATER_THAN", tb, 0.5))
            tinted = g.vmath("ADD", darken,
                             g.vmath("MULTIPLY", g.vmath("SUBTRACT", screen, darken), mask))
        else:
            tinted = diff

        n_out = g.node("NodeGroupOutput", x=g.x + 900)
        g.link(tinted, n_out.inputs["BaseColor"])
        g.link(final_n, n_out.inputs["Normal"])
        g.link(rough, n_out.inputs["Roughness"])
        g.link(spec, n_out.inputs["Specular"])
        if has_holes:
            g.link(g.math("MINIMUM", acc["alpha"], 1.0), n_out.inputs["Alpha"])
        else:
            n_out.inputs["Alpha"].default_value = 1.0
    return build


def apply_tile_detail_material(obj, mat_name, atlas, maps, *, normal_strength=1.0):
    layout = atlas["layout"]
    res = int(maps.get("res") or 0)
    if res <= 0:
        return None

    atlas_d = _image_for(atlas["diffuse"], "sRGB", alpha_mode="STRAIGHT")
    atlas_n = _image_for(atlas.get("normal") or "", "Non-Color") if layout.get("has_normals") else None
    control = _image_for(maps["control"], "Non-Color")
    params = _image_for(maps["params"], "Non-Color")
    params2 = _image_for(maps["params2"], "Non-Color")
    normal = _image_for(maps.get("normal") or "", "Non-Color")
    tint = _image_for(maps.get("tint") or "", "sRGB", alpha_mode="STRAIGHT")
    if atlas_d is None or control is None or params is None or params2 is None:
        return None

    has_holes = bool(maps.get("has_holes"))
    layout_sig = _sig((NODE_VERSION, DETAIL_VERSION, sorted(layout.items())))
    tap_sig = _sig((layout_sig, _stamp(atlas["diffuse"]), _stamp(atlas.get("normal") or "")))
    full_sig = _sig((tap_sig, res, has_holes, normal_strength,
                     [_stamp(maps.get(k) or "") for k in
                      ("control", "params", "params2", "normal", "tint")]))

    mat = bpy.data.materials.get(mat_name)
    if mat is not None and mat.get("witcher_terrain_material_key") != mat_name:
        mat = next((m for m in bpy.data.materials
                    if m.get("witcher_terrain_material_key") == mat_name), None)
    if mat is None:
        mat = bpy.data.materials.new(name=mat_name)

    if mat.get("witcher_terrain_detail_sig") != full_sig:
        atlas_uv_tree = _ensure_group(f".W3AtlasUV {layout_sig[:10]}", layout_sig,
                                      _atlas_uv_builder(layout))
        tap_tree = _ensure_group(f".W3TerrainTap {tap_sig[:10]}", tap_sig,
                                 _tap_builder(layout, atlas_d, atlas_n, atlas_uv_tree))
        main_name = f".W3TerrainDetail {mat_name}"
        images = {"control": control, "params": params, "params2": params2,
                  "normal": normal, "tint": tint}
        main_tree = _ensure_group(main_name, full_sig,
                                  _main_builder(res, tap_tree, images, has_holes))

        mat.use_nodes = True
        nt = mat.node_tree
        nt.nodes.clear()
        group_node = nt.nodes.new("ShaderNodeGroup")
        group_node.node_tree = main_tree
        group_node.location = (-320, 0)
        group_node.inputs["Normal Strength"].default_value = float(normal_strength)
        bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
        bsdf.location = (0, 0)
        out = nt.nodes.new("ShaderNodeOutputMaterial")
        out.location = (340, 0)
        nt.links.new(group_node.outputs["BaseColor"], bsdf.inputs["Base Color"])
        nt.links.new(group_node.outputs["Normal"], bsdf.inputs["Normal"])
        nt.links.new(group_node.outputs["Roughness"], bsdf.inputs["Roughness"])
        if "Specular IOR Level" in bsdf.inputs:
            nt.links.new(group_node.outputs["Specular"], bsdf.inputs["Specular IOR Level"])
        if has_holes:
            nt.links.new(group_node.outputs["Alpha"], bsdf.inputs["Alpha"])
        nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

        mat["witcher_terrain_material"] = True
        mat["witcher_terrain_material_key"] = mat_name
        mat["witcher_terrain_detail"] = True
        mat["witcher_terrain_detail_sig"] = full_sig
        _purge_unused_detail_groups()

    if obj is not None and obj.data is not None:
        if not (len(obj.data.materials) == 1 and obj.data.materials[0] is mat):
            obj.data.materials.clear()
            obj.data.materials.append(mat)
    return mat
