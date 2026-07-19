"""Build the EEVEE node graph for texarray terrain blending."""

from __future__ import annotations

import hashlib
import json
import os

import bpy

from .terrain_detail import (
    DETAIL_VERSION,
    OVERLAY_UV_SCALE,
    PARAMS_SHARPNESS_SCALE,
    TERRAIN_GAMMA,
    TRIPLANAR_TIGHTEN,
    UV_SCALE_LUT,
    f0_to_ior,
)

NODE_VERSION = 14
_EPS = 1e-3
_UP = (0.0, 0.0, 1.0)

TEXTURE_PACK_LUT_WIDTH = 32
TEXTURE_PACK_LUT_HEIGHT = 2
TEXTURE_PACK_IMAGE_PREFIX = ".W3TerrainTexturePack "
TEXTURE_PACK_KEY_PROP = "witcher_terrain_texture_pack_key"
TEXTURE_PACK_SOURCE_PROP = "witcher_terrain_texture_pack_source"

_TEXTURE_PACK_FIELDS = (
    "blend_sharpness",
    "slope_base_dampening",
    "slope_normal_dampening",
    "falloff",
    "specularity",
    "specularity_base",
    "specularity_scale",
)
_TEXTURE_PACK_DEFAULTS = {
    "blend_sharpness": 0.0,
    "slope_base_dampening": 0.0,
    "slope_normal_dampening": 0.5,
    "falloff": 0.0,
    "specularity": 0.0,
    "specularity_base": 0.0,
    "specularity_scale": 0.0,
}

DETAIL_GROUP_NODE_NAME = "W3 Terrain Detail"
DEBUG_EMISSION_NODE_NAME = "W3 Terrain Debug"
DEBUG_MIX_NODE_NAME = "W3 Terrain Debug Mix"


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
    image_count = len(bpy.data.images)
    image = bpy.data.images.load(path, check_existing=True)
    is_new = len(bpy.data.images) > image_count
    try:
        stamp = str(os.path.getmtime(path))
    except OSError:
        stamp = ""
    previous_stamp = str(image.get("w3_detail_stamp", "") or "")
    if not is_new and stamp and previous_stamp != stamp:
        try:
            image.reload()
        except Exception:
            pass
    if previous_stamp != stamp:
        image["w3_detail_stamp"] = stamp
    if image.colorspace_settings.name != colorspace:
        image.colorspace_settings.name = colorspace
    if image.alpha_mode != alpha_mode:
        image.alpha_mode = alpha_mode
    return image


def _texture_pack_image_name(texture_pack_key: str) -> str:
    digest = hashlib.sha1(str(texture_pack_key).encode(
        "utf-8", errors="replace")).hexdigest()[:12]
    return f"{TEXTURE_PACK_IMAGE_PREFIX}{digest}"


def _texture_pack_source(metadata) -> str:
    rows = []
    for index, source in enumerate(metadata or []):
        try:
            layer_id = int(source.get("id", index + 1))
        except (TypeError, ValueError, AttributeError):
            layer_id = index + 1
        row = {"id": layer_id}
        if isinstance(source, dict):
            for field in _TEXTURE_PACK_FIELDS:
                try:
                    row[field] = float(source.get(
                        field, _TEXTURE_PACK_DEFAULTS[field]))
                except (TypeError, ValueError):
                    row[field] = _TEXTURE_PACK_DEFAULTS[field]
        rows.append(row)
    return json.dumps(rows, sort_keys=True, separators=(",", ":"))


def _find_texture_pack_image(texture_pack_key: str):
    key = str(texture_pack_key or "")
    if not key:
        return None
    named = bpy.data.images.get(_texture_pack_image_name(key))
    if named is not None and str(named.get(TEXTURE_PACK_KEY_PROP, "")) == key:
        return named
    return next(
        (image for image in bpy.data.images
         if str(image.get(TEXTURE_PACK_KEY_PROP, "")) == key),
        None,
    )


def _clamp_unit(value, default=0.0) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = float(default)
    return max(0.0, min(1.0, value))


def _write_texture_pack_pixels(image, metadata) -> None:
    pixels = [0.0] * (
        TEXTURE_PACK_LUT_WIDTH * TEXTURE_PACK_LUT_HEIGHT * 4)
    for index, source in enumerate(metadata or []):
        if not isinstance(source, dict):
            continue
        try:
            layer_id = int(source.get("id", index + 1))
        except (TypeError, ValueError):
            layer_id = index + 1
        if not 1 <= layer_id <= TEXTURE_PACK_LUT_WIDTH:
            continue
        x = layer_id - 1
        row0 = x * 4
        row1 = (TEXTURE_PACK_LUT_WIDTH + x) * 4
        pixels[row0:row0 + 4] = (
            _clamp_unit(source.get("blend_sharpness", 0.0)),
            _clamp_unit(source.get("slope_base_dampening", 0.0)),
            _clamp_unit(source.get("slope_normal_dampening", 0.5), 0.5),
            _clamp_unit(source.get("falloff", 0.0)),
        )
        pixels[row1:row1 + 4] = (
            _clamp_unit(source.get("specularity", 0.0)),
            _clamp_unit(source.get("specularity_base", 0.0)),
            _clamp_unit(source.get("specularity_scale", 0.0)),
            0.0,
        )
    image.pixels[:] = pixels
    image.update()


def ensure_terrain_texture_pack_image(
    texture_pack_key: str,
    layer_metadata,
    *,
    reset=False,
):
    """Return the shared live parameter LUT for one terrain world."""
    key = str(texture_pack_key or "")
    if not key:
        return None
    image = _find_texture_pack_image(key)
    created = image is None
    if image is None:
        image = bpy.data.images.new(
            _texture_pack_image_name(key),
            width=TEXTURE_PACK_LUT_WIDTH,
            height=TEXTURE_PACK_LUT_HEIGHT,
            alpha=True,
            float_buffer=True,
        )
    elif tuple(image.size) != (TEXTURE_PACK_LUT_WIDTH, TEXTURE_PACK_LUT_HEIGHT):
        image.scale(TEXTURE_PACK_LUT_WIDTH, TEXTURE_PACK_LUT_HEIGHT)
        created = True
    source = _texture_pack_source(layer_metadata)
    previous_source = str(image.get(TEXTURE_PACK_SOURCE_PROP, "") or "")
    source_changed = not created and previous_source != source
    image[TEXTURE_PACK_KEY_PROP] = key
    image[TEXTURE_PACK_SOURCE_PROP] = source
    # Reassigning either image interpretation setting makes Blender discard the
    # in-memory generated pixels, even when the assigned value is unchanged.
    if image.colorspace_settings.name != "Non-Color":
        image.colorspace_settings.name = "Non-Color"
    if image.alpha_mode != "CHANNEL_PACKED":
        image.alpha_mode = "CHANNEL_PACKED"
    if not image.use_fake_user:
        image.use_fake_user = True
    if created or reset or source_changed:
        _write_texture_pack_pixels(image, layer_metadata)
    return image


def terrain_texture_pack_values(texture_pack_key: str, layer_metadata=None):
    """Read live LUT values and merge them with layer names and source paths."""
    image = ensure_terrain_texture_pack_image(texture_pack_key, layer_metadata or [])
    if image is None:
        return []
    metadata_by_id = {}
    for index, source in enumerate(layer_metadata or []):
        if not isinstance(source, dict):
            continue
        try:
            layer_id = int(source.get("id", index + 1))
        except (TypeError, ValueError):
            layer_id = index + 1
        metadata_by_id[layer_id] = source
    pixels = list(image.pixels[:])
    rows = []
    layer_ids = sorted(metadata_by_id) or list(range(1, TEXTURE_PACK_LUT_WIDTH + 1))
    for layer_id in layer_ids:
        if not 1 <= layer_id <= TEXTURE_PACK_LUT_WIDTH:
            continue
        source = metadata_by_id.get(layer_id, {})
        x = layer_id - 1
        row0 = pixels[x * 4:x * 4 + 4]
        offset = (TEXTURE_PACK_LUT_WIDTH + x) * 4
        row1 = pixels[offset:offset + 4]
        rows.append({
            **source,
            "id": layer_id,
            "name": str(source.get("name") or f"Layer {layer_id}"),
            "blend_sharpness": row0[0],
            "slope_base_dampening": row0[1],
            "slope_normal_dampening": row0[2],
            "falloff": row0[3],
            "specularity": row1[0],
            "specularity_base": row1[1],
            "specularity_scale": row1[2],
        })
    return rows


def update_terrain_texture_pack_layer(texture_pack_key: str, layer_id: int, **values):
    """Update one live texture-pack row without rebuilding terrain materials."""
    image = _find_texture_pack_image(texture_pack_key)
    layer_id = int(layer_id)
    if image is None or not 1 <= layer_id <= TEXTURE_PACK_LUT_WIDTH:
        return False
    pixels = list(image.pixels[:])
    x = layer_id - 1
    row0 = x * 4
    row1 = (TEXTURE_PACK_LUT_WIDTH + x) * 4
    mapping = {
        "blend_sharpness": row0,
        "slope_base_dampening": row0 + 1,
        "slope_normal_dampening": row0 + 2,
        "falloff": row0 + 3,
        "specularity": row1,
        "specularity_base": row1 + 1,
        "specularity_scale": row1 + 2,
    }
    for field, value in values.items():
        offset = mapping.get(field)
        if offset is not None:
            pixels[offset] = _clamp_unit(value)
    image.pixels[:] = pixels
    image.update()
    image["witcher_terrain_texture_pack_revision"] = int(
        image.get("witcher_terrain_texture_pack_revision", 0)) + 1
    return True


def reset_terrain_texture_pack(texture_pack_key: str, layer_metadata=None):
    """Restore the live LUT to the parameters read from the terrain material."""
    image = _find_texture_pack_image(texture_pack_key)
    if layer_metadata is None and image is not None:
        try:
            layer_metadata = json.loads(str(image.get(TEXTURE_PACK_SOURCE_PROP, "[]")))
        except (TypeError, ValueError, json.JSONDecodeError):
            layer_metadata = []
    return ensure_terrain_texture_pack_image(
        texture_pack_key, layer_metadata or [], reset=True)


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
    prefixes = (".W3AtlasUV ", ".W3TerrainTap ", ".W3TerrainCompute ",
                ".W3TerrainDetail ")
    # batch_remove: per-datablock remove() re-syncs the depsgraph each call.
    while True:
        doomed = [
            tree for tree in bpy.data.node_groups
            if tree.users == 0 and tree.name.startswith(prefixes)
        ]
        if not doomed:
            return
        bpy.data.batch_remove(doomed)


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
        tree.interface.new_socket("MacroNormal", in_out="INPUT", socket_type="NodeSocketVector")
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
        nx_neg = g.math("MULTIPLY", px, -1.0)
        ny_neg = g.math("MULTIPLY", py, -1.0)

        def atlas_uv(slice_s, u, v, projection=""):
            n = g.group(atlas_uv_tree)
            n.label = projection
            g.link(slice_s, n.inputs["Slice"])
            g.link(u, n.inputs["U"])
            g.link(v, n.inputs["V"])
            return n.outputs["UV"]

        macro_x, macro_y, macro_z = g.sep(n_in.outputs["MacroNormal"])
        macro_y_negative = g.math("LESS_THAN", macro_y, 0.0)

        def decode_normal(col, orientation):
            n = g.vmath("MULTIPLY_ADD", col, (2.0, 2.0, 2.0), (-1.0, -1.0, -1.0))
            nx, ny, nz = g.sep(n)
            # Atlas conversion changes DirectX green to Blender/OpenGL green.
            stored_y = g.math("MULTIPLY", ny, -1.0)
            if orientation in ("top", "horizontal"):
                return g.comb(x=nx, y=stored_y, z=nz)
            if orientation == "side_y":
                return g.comb(
                    x=nx,
                    y=g.mix_f(macro_y_negative, stored_y, ny),
                    z=nz,
                )
            return g.comb(x=nx, y=ny, z=nz)

        ov_uv = atlas_uv(ov_slice,
                         g.math("MULTIPLY", px, OVERLAY_UV_SCALE),
                         g.math("MULTIPLY", ny_neg, OVERLAY_UV_SCALE),
                         "XY horizontal")
        ov_diff, _ = g.image(atlas_d, ov_uv)

        twx, twy, twz = g.sep(n_in.outputs["TriW"])
        uv_top = atlas_uv(bg_slice, g.math("MULTIPLY", px, scale),
                          g.math("MULTIPLY", ny_neg, scale),
                          "XY triplanar")
        # Vertical projections use negative horizontal axes; PNG row order
        # yields Blender coordinates (-X, +Z) and (-Y, +Z).
        uv_sy = atlas_uv(bg_slice, g.math("MULTIPLY", nx_neg, scale),
                         g.math("MULTIPLY", pz, scale),
                         "-XZ triplanar")
        uv_sx = atlas_uv(bg_slice, g.math("MULTIPLY", ny_neg, scale),
                         g.math("MULTIPLY", pz, scale),
                         "-YZ triplanar")
        d_top, _ = g.image(atlas_d, uv_top)
        # Keep slope color on XY so a slope change does not move the sample to
        # an unrelated region of a transition texture. Normals and roughness
        # remain triplanar.
        bg_diff = d_top

        n_out = g.node("NodeGroupOutput", x=g.x + 900)
        g.link(ov_diff, n_out.inputs["OvDiff"])
        g.link(bg_diff, n_out.inputs["BgDiff"])

        if has_normals:
            ovn_col, ovn_a = g.image(atlas_n, ov_uv)
            ov_normal = decode_normal(ovn_col, "horizontal")
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


def _compute_builder(
    tap_tree,
    param_lut_img,
    has_holes,
    *,
    has_normal=False,
    has_tint=False,
    fresnel_power=2.0,
):
    def build(tree):
        tree.interface.new_socket("Normal Strength", in_out="INPUT",
                                  socket_type="NodeSocketFloat").default_value = 1.0
        tree.interface.new_socket("Tint Strength", in_out="INPUT",
                                  socket_type="NodeSocketFloat").default_value = 1.0
        tree.interface.new_socket("Fresnel Strength", in_out="INPUT",
                                  socket_type="NodeSocketFloat").default_value = 1.0
        tree.interface.new_socket("Slope Override", in_out="INPUT",
                                  socket_type="NodeSocketFloat").default_value = -1.0
        for name, socket_type in (
            ("Tile Fraction", "NodeSocketVector"),
            ("Params", "NodeSocketColor"),
            ("Params Alpha", "NodeSocketFloat"),
            ("Params2", "NodeSocketColor"),
            ("Params2 Alpha", "NodeSocketFloat"),
            ("Params3", "NodeSocketColor"),
            ("Params3 Alpha", "NodeSocketFloat"),
            ("Macro Normal Sample", "NodeSocketColor"),
            ("Tint Sample", "NodeSocketColor"),
        ):
            tree.interface.new_socket(name, in_out="INPUT", socket_type=socket_type)
        for index in range(4):
            tree.interface.new_socket(
                f"Control {index}", in_out="INPUT", socket_type="NodeSocketColor")
            tree.interface.new_socket(
                f"Control {index} Alpha", in_out="INPUT", socket_type="NodeSocketFloat")
        for name, stype in (
            ("BaseColor", "NodeSocketColor"), ("Normal", "NodeSocketVector"),
            ("Roughness", "NodeSocketFloat"), ("Specular", "NodeSocketFloat"),
            ("IOR", "NodeSocketFloat"),
            ("Alpha", "NodeSocketFloat"), ("Slope", "NodeSocketFloat"),
            ("MacroNormalDebug", "NodeSocketColor"),
            ("FinalNormalDebug", "NodeSocketColor"),
        ):
            tree.interface.new_socket(name, in_out="OUTPUT", socket_type=stype)

        g = _G(tree)
        n_in = g.node("NodeGroupInput", x=-560)
        geo = g.node("ShaderNodeNewGeometry", x=-560)
        g.x = 0
        strength = n_in.outputs["Normal Strength"]
        tint_strength = n_in.outputs["Tint Strength"]
        fresnel_strength = n_in.outputs["Fresnel Strength"]
        slope_override = n_in.outputs["Slope Override"]

        fx, fy, _ = g.sep(n_in.outputs["Tile Fraction"])
        ix = g.math("SUBTRACT", 1.0, fx)
        iy = g.math("SUBTRACT", 1.0, fy)
        weights = (
            g.math("MULTIPLY", ix, iy),  # (0,0)
            g.math("MULTIPLY", fx, iy),  # (1,0)
            g.math("MULTIPLY", ix, fy),  # (0,1)
            g.math("MULTIPLY", fx, fy),  # (1,1)
        )
        if has_normal:
            n_col = n_in.outputs["Macro Normal Sample"]
            # Reconstruct positive Z from filtered signed XY. Interpolating a
            # stored Z channel would shift steep slope thresholds.
            macro_signed = g.vmath(
                "MULTIPLY_ADD", n_col, (2.0, 2.0, 0.0), (-1.0, -1.0, 0.0))
            macro_x, macro_y, _macro_stored_z = g.sep(macro_signed)
            macro_xy_sq = g.math(
                "ADD",
                g.math("MULTIPLY", macro_x, macro_x),
                g.math("MULTIPLY", macro_y, macro_y),
            )
            macro_z = g.math(
                "SQRT", g.math("MAXIMUM", g.math("SUBTRACT", 1.0, macro_xy_sq), 0.0))
            macro_n = g.vmath(
                "NORMALIZE", g.comb(x=macro_x, y=macro_y, z=macro_z))
        else:
            macro_n = geo.outputs["Normal"]

        tri_raw = g.vmath("MAXIMUM",
                          g.vmath("SUBTRACT", g.vmath("ABSOLUTE", macro_n),
                                  (TRIPLANAR_TIGHTEN,) * 3),
                          (0.0, 0.0, 0.0))
        tri_sum = g.math("MAXIMUM", g.vmath("DOT_PRODUCT", tri_raw, (1.0, 1.0, 1.0)), 1e-4)
        tri_w = g.vmath("DIVIDE", tri_raw, g.comb(x=tri_sum, y=tri_sum, z=tri_sum))

        p_col = n_in.outputs["Params"]
        p_alpha = n_in.outputs["Params Alpha"]
        thr, baked_sharp_n, baked_base_damp = g.sep_color(p_col)
        if param_lut_img is None:
            sharp = g.math("MULTIPLY", baked_sharp_n, PARAMS_SHARPNESS_SCALE)
            base_damp = baked_base_damp
            norm_damp = p_alpha
            p2_col = n_in.outputs["Params2"]
            base_bg = n_in.outputs["Params2 Alpha"]
            spec_ov, spec_bg, base_ov = g.sep_color(p2_col)
            p3_col = n_in.outputs["Params3"]
            scale_bg = n_in.outputs["Params3 Alpha"]
            falloff_ov, falloff_bg, scale_ov = g.sep_color(p3_col)
        else:
            sharp = base_damp = norm_damp = None
            spec_ov = spec_bg = base_ov = base_bg = None
            falloff_ov = falloff_bg = scale_ov = scale_bg = None

        acc = None
        param_acc = None
        for index, w in enumerate(weights):
            ctrl_col = n_in.outputs[f"Control {index}"]
            ctrl_a = n_in.outputs[f"Control {index} Alpha"]
            cr, cg, cb = g.sep_color(ctrl_col)
            if param_lut_img is not None:
                def lut_uv(channel, row):
                    raw = g.math("ROUND", g.math("MULTIPLY", channel, 255.0))
                    layer_index = g.math(
                        "MINIMUM",
                        g.math("MAXIMUM", g.math("SUBTRACT", raw, 1.0), 0.0),
                        float(TEXTURE_PACK_LUT_WIDTH - 1),
                    )
                    return g.comb(
                        x=g.math(
                            "DIVIDE", g.math("ADD", layer_index, 0.5),
                            float(TEXTURE_PACK_LUT_WIDTH)),
                        y=(float(row) + 0.5) / float(TEXTURE_PACK_LUT_HEIGHT),
                    )

                h0_col, h0_alpha = g.image(
                    param_lut_img, lut_uv(cr, 0), interpolation="Closest",
                    extension="EXTEND")
                h1_col, _h1_alpha = g.image(
                    param_lut_img, lut_uv(cr, 1), interpolation="Closest",
                    extension="EXTEND")
                v0_col, v0_alpha = g.image(
                    param_lut_img, lut_uv(cg, 0), interpolation="Closest",
                    extension="EXTEND")
                v1_col, _v1_alpha = g.image(
                    param_lut_img, lut_uv(cg, 1), interpolation="Closest",
                    extension="EXTEND")
                h0r, _h0g, _h0b = g.sep_color(h0_col)
                h1r, h1g, h1b = g.sep_color(h1_col)
                _v0r, v0g, v0b = g.sep_color(v0_col)
                v1r, v1g, v1b = g.sep_color(v1_col)
                param_part = {
                    "sharp": g.math("MULTIPLY", h0r, w),
                    "base_damp": g.math("MULTIPLY", v0g, w),
                    "norm_damp": g.math("MULTIPLY", v0b, w),
                    "spec_ov": g.math("MULTIPLY", h1r, w),
                    "spec_bg": g.math("MULTIPLY", v1r, w),
                    "base_ov": g.math("MULTIPLY", h1g, w),
                    "base_bg": g.math("MULTIPLY", v1g, w),
                    "falloff_ov": g.math("MULTIPLY", h0_alpha, w),
                    "falloff_bg": g.math("MULTIPLY", v0_alpha, w),
                    "scale_ov": g.math("MULTIPLY", h1b, w),
                    "scale_bg": g.math("MULTIPLY", v1b, w),
                }
                if param_acc is None:
                    param_acc = param_part
                else:
                    for key in param_acc:
                        param_acc[key] = g.math("ADD", param_acc[key], param_part[key])
            tap = g.group(tap_tree)
            g.link(cr, tap.inputs["CtrlR"])
            g.link(cg, tap.inputs["CtrlG"])
            g.link(cb, tap.inputs["CtrlB"])
            g.link(geo.outputs["Position"], tap.inputs["TexPos"])
            g.link(tri_w, tap.inputs["TriW"])
            g.link(macro_n, tap.inputs["MacroNormal"])
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

        if param_acc is not None:
            sharp = param_acc["sharp"]
            base_damp = param_acc["base_damp"]
            norm_damp = param_acc["norm_damp"]
            spec_ov = param_acc["spec_ov"]
            spec_bg = param_acc["spec_bg"]
            base_ov = param_acc["base_ov"]
            base_bg = param_acc["base_bg"]
            falloff_ov = param_acc["falloff_ov"]
            falloff_bg = param_acc["falloff_bg"]
            scale_ov = param_acc["scale_ov"]
            scale_bg = param_acc["scale_bg"]

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

        mx, my, mz = g.sep(macro_n)
        world_tangent = g.vmath(
            "NORMALIZE",
            g.vmath(
                "SUBTRACT",
                (1.0, 0.0, 0.0),
                g.vmath("SCALE", macro_n, scale=mx),
            ),
        )
        world_binormal = g.vmath("CROSS_PRODUCT", macro_n, world_tangent)

        def combine_tangent_normal(tangent_normal):
            tx, ty, tz = g.sep(tangent_normal)
            return g.vmath(
                "ADD",
                g.vmath(
                    "ADD",
                    g.vmath("SCALE", world_tangent, scale=tx),
                    g.vmath("SCALE", world_binormal, scale=ty),
                ),
                g.vmath("SCALE", macro_n, scale=tz),
            )

        combined_ov = combine_tangent_normal(ov_ns)
        combined_bg = combine_tangent_normal(bg_ns)
        flat_bg = g.mix_v(mz, combined_bg, _UP)
        biased = g.vmath("NORMALIZE", g.mix_v(base_damp, combined_bg, flat_bg))
        bx, by, bz = g.sep(biased)
        horizontal = g.math(
            "SQRT",
            g.math(
                "ADD",
                g.math("MULTIPLY", bx, bx),
                g.math("MULTIPLY", by, by),
            ),
        )
        slope = g.math(
            "MAXIMUM",
            g.math("MINIMUM", g.math("DIVIDE", horizontal, g.math("MAXIMUM", bz, _EPS)), 1.0),
            0.0,
        )
        high_threshold = g.math(
            "MAXIMUM", g.math("MINIMUM", g.math("ADD", thr, sharp), 1.0), 0.0)
        source_blend = g.math(
            "DIVIDE",
            g.math("SUBTRACT", slope, thr),
            g.math("MAXIMUM", g.math("SUBTRACT", high_threshold, thr), _EPS),
            clamp=True,
        )
        # A negative override keeps the imported slope blend. Zero and one
        # isolate the horizontal/vertical branches for live material debugging.
        use_slope_override = g.math("GREATER_THAN", slope_override, -0.5)
        clamped_slope_override = g.math(
            "MAXIMUM", g.math("MINIMUM", slope_override, 1.0), 0.0)
        blend = g.mix_f(use_slope_override, source_blend, clamped_slope_override)

        diff = g.mix_v(blend, ov_diff, bg_diff)
        rough = g.mix_f(blend, acc["ov_r"], acc["bg_r"])
        spec_gamma = g.mix_f(blend, spec_ov, spec_bg)
        spec_base = g.mix_f(blend, base_ov, base_bg)
        spec_scale = g.mix_f(blend, scale_ov, scale_bg)
        falloff = g.mix_f(blend, falloff_ov, falloff_bg)

        # Packed falloff controls the roughness-dependent direct F0. Principled
        # needs its IOR input to cover the full 0..1 F0 range.
        spec_factor = g.math(
            "ADD",
            g.math("MULTIPLY", g.math("SUBTRACT", 1.0, rough), falloff),
            g.math("MULTIPLY", g.math("SUBTRACT", spec_base, 0.5), 3.0),
        )
        f0 = g.math(
            "MAXIMUM",
            g.math(
                "MINIMUM",
                g.math("MULTIPLY", g.math("POWER", spec_gamma, TERRAIN_GAMMA), spec_factor),
                1.0,
            ),
            0.0,
        )
        sqrt_f0 = g.math("POWER", f0, 0.5)
        ior = g.math(
            "MINIMUM",
            g.math(
                "DIVIDE",
                g.math("ADD", 1.0, sqrt_f0),
                g.math(
                    "MAXIMUM", g.math("SUBTRACT", 1.0, sqrt_f0),
                    2.0 / 1001.0),
            ),
            1000.0,
        )

        ovx, ovy, ovz = g.sep(combined_ov)
        bgx, bgy, bgz = g.sep(combined_bg)
        horizontal_weight = norm_damp
        vertical_weight = g.math("SUBTRACT", 1.0, norm_damp)

        def signed_safe_denominator(value):
            """Keep the signed normal-Z derivative finite at zero."""
            negative = g.math("LESS_THAN", value, 0.0)
            positive_value = g.math("MAXIMUM", value, _EPS)
            negative_value = g.math("MINIMUM", value, -_EPS)
            return g.mix_f(negative, positive_value, negative_value)

        ovz_safe = signed_safe_denominator(ovz)
        bgz_safe = signed_safe_denominator(bgz)
        full_n = g.vmath("NORMALIZE", g.comb(
            x=g.math(
                "ADD",
                g.math("MULTIPLY", vertical_weight,
                       g.math("DIVIDE", bgx, bgz_safe)),
                g.math("MULTIPLY", horizontal_weight,
                       g.math("DIVIDE", ovx, ovz_safe)),
            ),
            y=g.math(
                "ADD",
                g.math("MULTIPLY", vertical_weight,
                       g.math("DIVIDE", bgy, bgz_safe)),
                g.math("MULTIPLY", horizontal_weight,
                       g.math("DIVIDE", ovy, ovz_safe)),
            ),
            z=1.0,
        ))
        final_n = g.vmath("NORMALIZE", g.mix_v(blend, full_n, combined_bg))

        if has_tint:
            t_col = n_in.outputs["Tint Sample"]
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
            authored_tint = g.vmath(
                "ADD", darken,
                g.vmath("MULTIPLY", g.vmath("SUBTRACT", screen, darken), mask))
            tinted = g.mix_v(tint_strength, diff, authored_tint)
        else:
            tinted = diff

        # Add the view-angle rim in stored gamma space using the imported power.
        view_dir = g.vmath(
            "NORMALIZE", g.vmath("SCALE", geo.outputs["Incoming"], scale=-1.0))
        ndotv = g.vmath("DOT_PRODUCT", final_n, view_dir)
        rim_base = g.math(
            "MAXIMUM", g.math("MINIMUM", g.math("SUBTRACT", 1.0, ndotv), 1.0), 0.0)
        rim = g.math(
            "MULTIPLY",
            g.math("MULTIPLY", g.math("POWER", rim_base, fresnel_power), spec_scale),
            fresnel_strength,
        )
        diffuse_gamma = g.vmath(
            "MAXIMUM",
            g.vmath(
                "MINIMUM",
                g.vmath("ADD", tinted, g.comb(x=rim, y=rim, z=rim)),
                (1.0, 1.0, 1.0),
            ),
            (0.0, 0.0, 0.0),
        )
        # Convert the completed terrain blend to linear color once before
        # feeding Principled Base Color.
        dr, dg, db = g.sep(diffuse_gamma)
        diffuse_linear = g.comb(
            x=g.math("POWER", dr, TERRAIN_GAMMA),
            y=g.math("POWER", dg, TERRAIN_GAMMA),
            z=g.math("POWER", db, TERRAIN_GAMMA),
        )
        n_out = g.node("NodeGroupOutput", x=g.x + 900)
        g.link(diffuse_linear, n_out.inputs["BaseColor"])
        g.link(final_n, n_out.inputs["Normal"])
        g.link(rough, n_out.inputs["Roughness"])
        g.link(f0, n_out.inputs["Specular"])
        g.link(ior, n_out.inputs["IOR"])
        g.link(blend, n_out.inputs["Slope"])
        g.link(
            g.vmath("MULTIPLY_ADD", macro_n, (0.5, 0.5, 0.5), (0.5, 0.5, 0.5)),
            n_out.inputs["MacroNormalDebug"],
        )
        g.link(
            g.vmath("MULTIPLY_ADD", final_n, (0.5, 0.5, 0.5), (0.5, 0.5, 0.5)),
            n_out.inputs["FinalNormalDebug"],
        )
        if has_holes:
            g.link(g.math("MINIMUM", acc["alpha"], 1.0), n_out.inputs["Alpha"])
        else:
            n_out.inputs["Alpha"].default_value = 1.0
    return build


def _main_builder(
    res,
    map_res,
    compute_tree,
    images,
    *,
    tint_res=0,
    tint_map_res=0,
):
    control_img = images["control"]
    params_img = images["params"]
    params2_img = images["params2"]
    params3_img = images["params3"]
    param_lut_img = images.get("param_lut")
    normal_img = images.get("normal")
    tint_img = images.get("tint")

    def build(tree):
        tree.interface.new_socket("Normal Strength", in_out="INPUT",
                                  socket_type="NodeSocketFloat").default_value = 1.0
        tree.interface.new_socket("Tint Strength", in_out="INPUT",
                                  socket_type="NodeSocketFloat").default_value = 1.0
        tree.interface.new_socket("Fresnel Strength", in_out="INPUT",
                                  socket_type="NodeSocketFloat").default_value = 1.0
        tree.interface.new_socket("Slope Override", in_out="INPUT",
                                  socket_type="NodeSocketFloat").default_value = -1.0
        output_names = (
            ("BaseColor", "NodeSocketColor"), ("Normal", "NodeSocketVector"),
            ("Roughness", "NodeSocketFloat"), ("Specular", "NodeSocketFloat"),
            ("IOR", "NodeSocketFloat"), ("Alpha", "NodeSocketFloat"),
            ("Slope", "NodeSocketFloat"),
            ("MacroNormalDebug", "NodeSocketColor"),
            ("FinalNormalDebug", "NodeSocketColor"),
        )
        for name, socket_type in output_names:
            tree.interface.new_socket(name, in_out="OUTPUT", socket_type=socket_type)

        g = _G(tree)
        n_in = g.node("NodeGroupInput", x=-560)
        uv = g.node("ShaderNodeUVMap", x=-560, uv_map="UVMap").outputs["UV"]
        g.x = 0

        compute = g.group(compute_tree)
        for name in ("Normal Strength", "Tint Strength", "Fresnel Strength",
                     "Slope Override"):
            g.link(n_in.outputs[name], compute.inputs[name])

        grid_p = g.vmath("MULTIPLY", uv, (float(res), float(res), 1.0))
        # Paired half-texel offsets cancel, so tile UV maps directly to the
        # control lattice.
        c = g.vmath("FLOOR", grid_p)
        fraction = g.vmath("SUBTRACT", grid_p, c)
        g.link(fraction, compute.inputs["Tile Fraction"])

        inv_map_res = 1.0 / float(map_res)
        if normal_img is not None:
            uv_n = g.vmath(
                "MULTIPLY",
                g.vmath("ADD", grid_p, (0.5, 0.5, 0.0)),
                (inv_map_res, inv_map_res, 1.0),
            )
            normal_col, _ = g.image(normal_img, uv_n, extension="EXTEND")
            g.link(normal_col, compute.inputs["Macro Normal Sample"])

        uv_p = g.vmath(
            "MULTIPLY",
            g.vmath("ADD", grid_p, (0.5, 0.5, 0.0)),
            (inv_map_res, inv_map_res, 1.0),
        )
        params_col, params_alpha = g.image(params_img, uv_p, extension="EXTEND")
        g.link(params_col, compute.inputs["Params"])
        g.link(params_alpha, compute.inputs["Params Alpha"])
        if param_lut_img is None:
            params2_col, params2_alpha = g.image(params2_img, uv_p, extension="EXTEND")
            params3_col, params3_alpha = g.image(params3_img, uv_p, extension="EXTEND")
            g.link(params2_col, compute.inputs["Params2"])
            g.link(params2_alpha, compute.inputs["Params2 Alpha"])
            g.link(params3_col, compute.inputs["Params3"])
            g.link(params3_alpha, compute.inputs["Params3 Alpha"])

        offsets = ((0.5, 0.5), (1.5, 0.5), (0.5, 1.5), (1.5, 1.5))
        for index, (ox, oy) in enumerate(offsets):
            uv_t = g.vmath(
                "MULTIPLY",
                g.vmath("ADD", c, (ox, oy, 0.0)),
                (inv_map_res, inv_map_res, 1.0),
            )
            control_col, control_alpha = g.image(
                control_img, uv_t, interpolation="Closest", extension="EXTEND")
            g.link(control_col, compute.inputs[f"Control {index}"])
            g.link(control_alpha, compute.inputs[f"Control {index} Alpha"])

        if tint_img is not None:
            tint_uv = uv
            if tint_res > 0 and tint_map_res > 0:
                tint_uv = g.vmath(
                    "MULTIPLY",
                    g.vmath(
                        "ADD",
                        g.vmath(
                            "MULTIPLY", uv,
                            (float(tint_res), float(tint_res), 1.0)),
                        (0.5, 0.5, 0.0),
                    ),
                    (1.0 / float(tint_map_res),
                     1.0 / float(tint_map_res), 1.0),
                )
            tint_col, _ = g.image(tint_img, tint_uv, extension="EXTEND")
            g.link(tint_col, compute.inputs["Tint Sample"])

        n_out = g.node("NodeGroupOutput", x=g.x + 900)
        for name, _socket_type in output_names:
            g.link(compute.outputs[name], n_out.inputs[name])
    return build


def apply_tile_detail_material(
    obj,
    mat_name,
    atlas,
    maps,
    *,
    normal_strength=1.0,
    fresnel_power=2.0,
    texture_pack_key="",
    layer_metadata=None,
    purge_unused=True,
):
    layout = atlas["layout"]
    res = int(maps.get("res") or 0)
    if res <= 0:
        return None
    map_res = int(maps.get("map_res") or res)
    tint_res = int(maps.get("tint_res") or 0)
    tint_map_res = int(maps.get("tint_map_res") or 0)
    if map_res < res:
        return None
    fresnel_power = float(fresnel_power)

    # Keep samples in stored gamma space until the completed diffuse is converted
    # to linear once.
    atlas_d = _image_for(atlas["diffuse"], "Non-Color", alpha_mode="STRAIGHT")
    atlas_n = _image_for(atlas.get("normal") or "", "Non-Color") if layout.get("has_normals") else None
    control = _image_for(maps["control"], "Non-Color")
    params = _image_for(maps["params"], "Non-Color")
    normal = _image_for(maps.get("normal") or "", "Non-Color")
    tint = _image_for(maps.get("tint") or "", "Non-Color", alpha_mode="STRAIGHT")
    param_lut = None
    if texture_pack_key and layer_metadata:
        param_lut = ensure_terrain_texture_pack_image(
            texture_pack_key, layer_metadata)
    params2 = _image_for(maps["params2"], "Non-Color") if param_lut is None else None
    params3 = _image_for(maps["params3"], "Non-Color") if param_lut is None else None
    if (atlas_d is None or control is None or params is None
            or (param_lut is None and (params2 is None or params3 is None))):
        return None

    has_holes = bool(maps.get("has_holes"))
    layout_sig = _sig((NODE_VERSION, DETAIL_VERSION, sorted(layout.items())))
    tap_sig = _sig((layout_sig, _stamp(atlas["diffuse"]), _stamp(atlas.get("normal") or "")))
    compute_sig = _sig((tap_sig, bool(param_lut), str(texture_pack_key or ""),
                        bool(normal), bool(tint), has_holes, fresnel_power))
    map_keys = ["control", "params", "normal", "tint"]
    if param_lut is None:
        map_keys.extend(("params2", "params3"))
    full_sig = _sig((tap_sig, res, map_res, tint_res, tint_map_res,
                      has_holes, normal_strength, fresnel_power,
                      str(texture_pack_key or ""),
                      [_stamp(maps.get(key) or "") for key in map_keys]))

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
        compute_tree = _ensure_group(
            f".W3TerrainCompute {compute_sig[:10]}",
            compute_sig,
            _compute_builder(
                tap_tree,
                param_lut,
                has_holes,
                has_normal=normal is not None,
                has_tint=tint is not None,
                fresnel_power=fresnel_power,
            ),
        )
        main_name = f".W3TerrainDetail {mat_name}"
        images = {"control": control, "params": params, "params2": params2,
                  "params3": params3, "param_lut": param_lut,
                  "normal": normal, "tint": tint}
        main_tree = _ensure_group(main_name, full_sig,
                                  _main_builder(
                                       res,
                                       map_res,
                                       compute_tree,
                                       images,
                                       tint_res=tint_res,
                                       tint_map_res=tint_map_res,
                                   ))

        mat.use_nodes = True
        nt = mat.node_tree
        nt.nodes.clear()
        group_node = nt.nodes.new("ShaderNodeGroup")
        group_node.name = DETAIL_GROUP_NODE_NAME
        group_node.label = "Witcher Terrain Detail"
        group_node["witcher_terrain_detail_node"] = True
        group_node.node_tree = main_tree
        group_node.location = (-320, 0)
        group_node.inputs["Normal Strength"].default_value = float(normal_strength)
        bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
        bsdf.location = (0, 0)
        debug_emission = nt.nodes.new("ShaderNodeEmission")
        debug_emission.name = DEBUG_EMISSION_NODE_NAME
        debug_emission.label = "Unlit Terrain Debug"
        debug_emission.location = (0, -260)
        debug_emission.inputs["Strength"].default_value = 1.0
        debug_mix = nt.nodes.new("ShaderNodeMixShader")
        debug_mix.name = DEBUG_MIX_NODE_NAME
        debug_mix.label = "Terrain Debug View"
        debug_mix.location = (340, 0)
        debug_mix.inputs[0].default_value = 0.0
        out = nt.nodes.new("ShaderNodeOutputMaterial")
        out.location = (620, 0)
        nt.links.new(group_node.outputs["BaseColor"], bsdf.inputs["Base Color"])
        nt.links.new(group_node.outputs["Normal"], bsdf.inputs["Normal"])
        nt.links.new(group_node.outputs["Roughness"], bsdf.inputs["Roughness"])
        if "IOR" in bsdf.inputs:
            nt.links.new(group_node.outputs["IOR"], bsdf.inputs["IOR"])
            if "Specular IOR Level" in bsdf.inputs:
                bsdf.inputs["Specular IOR Level"].default_value = 0.5
        elif "Specular" in bsdf.inputs:
            nt.links.new(group_node.outputs["Specular"], bsdf.inputs["Specular"])
        if has_holes:
            nt.links.new(group_node.outputs["Alpha"], bsdf.inputs["Alpha"])
        nt.links.new(group_node.outputs["BaseColor"], debug_emission.inputs["Color"])
        nt.links.new(bsdf.outputs["BSDF"], debug_mix.inputs[1])
        nt.links.new(debug_emission.outputs["Emission"], debug_mix.inputs[2])
        nt.links.new(debug_mix.outputs["Shader"], out.inputs["Surface"])

        mat["witcher_terrain_material"] = True
        mat["witcher_terrain_material_key"] = mat_name
        mat["witcher_terrain_detail"] = True
        mat["witcher_terrain_detail_sig"] = full_sig
        if purge_unused:
            _purge_unused_detail_groups()

    if texture_pack_key:
        mat[TEXTURE_PACK_KEY_PROP] = str(texture_pack_key)
    if layer_metadata:
        mat["witcher_terrain_layer_metadata"] = json.dumps(
            list(layer_metadata), separators=(",", ":"))

    if obj is not None and obj.data is not None:
        if not (len(obj.data.materials) == 1 and obj.data.materials[0] is mat):
            obj.data.materials.clear()
            obj.data.materials.append(mat)
    return mat


_DEBUG_OUTPUTS = {
    "BASE_COLOR": "BaseColor",
    "SLOPE": "Slope",
    "ROUGHNESS": "Roughness",
    "SPECULAR": "Specular",
    "MACRO_NORMAL": "MacroNormalDebug",
    "FINAL_NORMAL": "FinalNormalDebug",
}


def _find_detail_group_node(mat):
    if mat is None or not mat.use_nodes or mat.node_tree is None:
        return None
    tagged = mat.node_tree.nodes.get(DETAIL_GROUP_NODE_NAME)
    if tagged is not None and tagged.bl_idname == "ShaderNodeGroup":
        return tagged
    for node in mat.node_tree.nodes:
        if node.bl_idname != "ShaderNodeGroup":
            continue
        if node.get("witcher_terrain_detail_node"):
            return node
        if node.node_tree is not None and node.node_tree.name.startswith(".W3TerrainDetail "):
            return node
    return None


def _find_principled(mat):
    if mat is None or not mat.use_nodes or mat.node_tree is None:
        return None
    node = mat.node_tree.nodes.get("Principled BSDF")
    if node is not None:
        return node
    return next(
        (item for item in mat.node_tree.nodes
         if item.bl_idname == "ShaderNodeBsdfPrincipled"),
        None,
    )


def _replace_input_link(tree, from_socket, to_socket):
    if from_socket is None and not to_socket.is_linked:
        return
    if (
        from_socket is not None
        and len(to_socket.links) == 1
        and to_socket.links[0].from_socket == from_socket
    ):
        return
    for link in list(to_socket.links):
        tree.links.remove(link)
    if from_socket is not None:
        tree.links.new(from_socket, to_socket)


def _ensure_debug_root(mat, group_node, principled):
    tree = mat.node_tree
    emission = tree.nodes.get(DEBUG_EMISSION_NODE_NAME)
    if emission is None or emission.bl_idname != "ShaderNodeEmission":
        emission = tree.nodes.new("ShaderNodeEmission")
        emission.name = DEBUG_EMISSION_NODE_NAME
        emission.label = "Unlit Terrain Debug"
        emission.location = (0, -260)
        emission.inputs["Strength"].default_value = 1.0

    mix = tree.nodes.get(DEBUG_MIX_NODE_NAME)
    if mix is None or mix.bl_idname != "ShaderNodeMixShader":
        mix = tree.nodes.new("ShaderNodeMixShader")
        mix.name = DEBUG_MIX_NODE_NAME
        mix.label = "Terrain Debug View"
        mix.location = (340, 0)

    output = next(
        (node for node in tree.nodes if node.bl_idname == "ShaderNodeOutputMaterial"
         and getattr(node, "is_active_output", True)),
        None,
    )
    if output is None:
        output = tree.nodes.new("ShaderNodeOutputMaterial")
        output.location = (620, 0)

    if not emission.inputs["Color"].is_linked and "BaseColor" in group_node.outputs:
        tree.links.new(group_node.outputs["BaseColor"], emission.inputs["Color"])
    if not any(link.from_node is principled for link in mix.inputs[1].links):
        _replace_input_link(tree, principled.outputs["BSDF"], mix.inputs[1])
    if not any(link.from_node is emission for link in mix.inputs[2].links):
        _replace_input_link(tree, emission.outputs["Emission"], mix.inputs[2])
    if not any(link.from_node is mix for link in output.inputs["Surface"].links):
        _replace_input_link(tree, mix.outputs["Shader"], output.inputs["Surface"])
    return emission, mix


def configure_material_controls(
    mat,
    *,
    surface_mode="SOURCE",
    roughness=0.82,
    specular=0.12,
    normal_strength=1.0,
    tint_strength=1.0,
    fresnel_strength=1.0,
    slope_mode="SOURCE",
    debug_view="FINAL",
):
    """Apply scene terrain controls to one material without rebuilding its graph."""
    principled = _find_principled(mat)
    if principled is None:
        return False
    group_node = _find_detail_group_node(mat)
    if group_node is None:
        if "Roughness" in principled.inputs:
            principled.inputs["Roughness"].default_value = float(roughness)
        spec_input = principled.inputs.get("Specular IOR Level") or principled.inputs.get("Specular")
        ior_input = principled.inputs.get("IOR")
        if ior_input is not None:
            ior_input.default_value = float(f0_to_ior(specular))
            if spec_input is not None:
                spec_input.default_value = 0.5
        elif spec_input is not None:
            spec_input.default_value = float(specular)
        if "Metallic" in principled.inputs:
            principled.inputs["Metallic"].default_value = 0.0
        return True

    for name, value in (
        ("Normal Strength", normal_strength),
        ("Tint Strength", tint_strength),
        ("Fresnel Strength", fresnel_strength),
        ("Slope Override", {"HORIZONTAL": 0.0, "VERTICAL": 1.0}.get(
            str(slope_mode).upper(), -1.0)),
    ):
        socket = group_node.inputs.get(name)
        if socket is not None:
            socket.default_value = float(value)

    tree = mat.node_tree
    rough_input = principled.inputs.get("Roughness")
    spec_input = principled.inputs.get("Specular IOR Level") or principled.inputs.get("Specular")
    ior_input = principled.inputs.get("IOR")
    use_source = str(surface_mode).upper() != "OVERRIDE"
    if rough_input is not None:
        if use_source and "Roughness" in group_node.outputs:
            _replace_input_link(tree, group_node.outputs["Roughness"], rough_input)
        else:
            _replace_input_link(tree, None, rough_input)
            rough_input.default_value = float(roughness)
    if ior_input is not None:
        if use_source and "IOR" in group_node.outputs:
            _replace_input_link(tree, group_node.outputs["IOR"], ior_input)
        else:
            _replace_input_link(tree, None, ior_input)
            ior_input.default_value = float(f0_to_ior(specular))
        if spec_input is not None:
            _replace_input_link(tree, None, spec_input)
            spec_input.default_value = 0.5
    elif spec_input is not None:
        if use_source and "Specular" in group_node.outputs:
            _replace_input_link(tree, group_node.outputs["Specular"], spec_input)
        else:
            _replace_input_link(tree, None, spec_input)
            spec_input.default_value = float(specular)
    if "Metallic" in principled.inputs:
        principled.inputs["Metallic"].default_value = 0.0

    emission, debug_mix = _ensure_debug_root(mat, group_node, principled)
    view = str(debug_view).upper()
    debug_mix.inputs[0].default_value = 0.0 if view == "FINAL" else 1.0
    color_input = emission.inputs["Color"]
    debug_socket = None
    if view == "ROUGHNESS" and not use_source:
        value = float(roughness)
        color_input.default_value = (value, value, value, 1.0)
    elif view == "SPECULAR" and not use_source:
        value = float(specular)
        color_input.default_value = (value, value, value, 1.0)
    else:
        output_name = _DEBUG_OUTPUTS.get(view, "BaseColor")
        debug_socket = (
            group_node.outputs.get(output_name)
            or group_node.outputs.get("BaseColor")
        )
    _replace_input_link(tree, debug_socket, color_input)

    mat["witcher_terrain_material"] = True
    return True
