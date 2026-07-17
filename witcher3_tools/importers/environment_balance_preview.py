"""Managed balance-map preview for Blender's realtime compositor."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import os
from typing import Any

import bpy


log = logging.getLogger(__name__)

_TREE_NAME = "W3 Environment Balance Preview"
_OWNER_PROP = "witcher_environment_balance_preview"
_PREVIOUS_TREE_PROP = "witcher_environment_balance_previous_tree"
_VIEW_SETTINGS_PROP = "witcher_environment_balance_view_settings"
_VIEW_PROPERTIES = (
    "view_transform",
    "look",
    "exposure",
    "gamma",
    "use_curve_mapping",
    "use_white_balance",
)


@dataclass
class _PreviewState:
    scene: Any
    previous_tree: Any
    view_settings: dict[str, Any]
    spaces: dict[int, tuple[Any, str]] = field(default_factory=dict)
    tree: Any = None
    image: Any = None


_STATES: dict[int, _PreviewState] = {}


def _scene_key(scene) -> int:
    return int(scene.as_pointer())


def _snapshot_view_settings(scene) -> dict[str, Any]:
    settings = scene.view_settings
    return {
        name: getattr(settings, name)
        for name in _VIEW_PROPERTIES
        if hasattr(settings, name)
    }


def _set_view_property(settings, name: str, value: Any) -> None:
    if hasattr(settings, name):
        setattr(settings, name, value)


def _use_managed_view_settings(scene) -> None:
    settings = scene.view_settings
    _set_view_property(settings, "view_transform", "Standard")
    _set_view_property(settings, "look", "None")
    _set_view_property(settings, "exposure", 0.0)
    _set_view_property(settings, "gamma", 1.0)
    _set_view_property(settings, "use_curve_mapping", False)
    _set_view_property(settings, "use_white_balance", False)


def _restore_view_settings(scene, values: dict[str, Any]) -> None:
    settings = scene.view_settings
    # View changes can alter which looks are valid, so restore it first.
    if "view_transform" in values:
        _set_view_property(settings, "view_transform", values["view_transform"])
    for name in _VIEW_PROPERTIES:
        if name != "view_transform" and name in values:
            _set_view_property(settings, name, values[name])


def _store_restore_state(scene, previous_tree, view_settings: dict[str, Any]) -> None:
    scene[_PREVIOUS_TREE_PROP] = previous_tree.name if previous_tree is not None else ""
    scene[_VIEW_SETTINGS_PROP] = dict(view_settings)


def _pop_restore_state(scene) -> tuple[Any, dict[str, Any]]:
    previous_name = str(scene.get(_PREVIOUS_TREE_PROP, "") or "")
    try:
        view_settings = dict(scene.get(_VIEW_SETTINGS_PROP, {}))
    except (TypeError, ValueError):
        view_settings = {}
    for name in (_PREVIOUS_TREE_PROP, _VIEW_SETTINGS_PROP):
        if name in scene:
            del scene[name]
    return bpy.data.node_groups.get(previous_name), view_settings


def _view3d_spaces(context, scene):
    window_manager = getattr(context, "window_manager", None) or bpy.context.window_manager
    for window in getattr(window_manager, "windows", ()):
        if getattr(window, "scene", None) != scene:
            continue
        for area in getattr(window.screen, "areas", ()):
            if area.type == "VIEW_3D":
                yield area.spaces.active


def _enable_viewport_compositor(context, state: _PreviewState) -> None:
    for space in _view3d_spaces(context, state.scene):
        pointer = int(space.as_pointer())
        if pointer not in state.spaces:
            state.spaces[pointer] = (space, str(space.shading.use_compositor))
        space.shading.use_compositor = "ALWAYS"


def _restore_viewport_compositor(state: _PreviewState) -> None:
    for space, previous in state.spaces.values():
        try:
            space.shading.use_compositor = previous
        except (ReferenceError, RuntimeError):
            pass
    state.spaces.clear()


def _resolved_image_path(path: str) -> str:
    text = str(path or "").strip()
    if not text:
        return ""
    source = os.path.normpath(bpy.path.abspath(text))

    root, extension = os.path.splitext(source)
    extension = extension.lower()
    if extension == ".dds":
        return source if os.path.isfile(source) else ""

    sibling_dds = root + ".dds"
    xbm_path = source if extension == ".xbm" else root + ".xbm"
    if not os.path.isfile(xbm_path):
        return sibling_dds if os.path.isfile(sibling_dds) else ""

    from ..materials.material import _convert_xbm_to_writable_dds

    converted = _convert_xbm_to_writable_dds(xbm_path, sibling_dds)
    return os.path.normpath(converted) if converted and os.path.isfile(converted) else ""


def _load_balance_image(path: str):
    resolved = _resolved_image_path(path)
    if not resolved:
        raise FileNotFoundError(f"Balance map was not found: {path}")

    image = bpy.data.images.load(resolved, check_existing=False)
    try:
        if tuple(image.size) != (512, 512):
            raise ValueError(
                f"Balance map must be 512x512, got {image.size[0]}x{image.size[1]}"
            )
        image.colorspace_settings.name = "Non-Color"
        image[_OWNER_PROP] = True
        image["witcher_environment_resolved_path"] = resolved
        return image
    except Exception:
        bpy.data.images.remove(image)
        raise


def _set_or_link(tree, socket, value) -> None:
    if hasattr(value, "is_output"):
        tree.links.new(value, socket)
    else:
        socket.default_value = value


def _math(tree, operation: str, first, second=None, *, name: str = ""):
    node = tree.nodes.new("ShaderNodeMath")
    node.operation = operation
    if name:
        node.name = name
        node.label = name
    _set_or_link(tree, node.inputs[0], first)
    if second is not None:
        _set_or_link(tree, node.inputs[1], second)
    return node.outputs[0]


def _vector_math(tree, operation: str, first, second=None, *, scale=None, name: str = ""):
    node = tree.nodes.new("ShaderNodeVectorMath")
    node.operation = operation
    if name:
        node.name = name
        node.label = name
    _set_or_link(tree, node.inputs[0], first)
    if second is not None:
        _set_or_link(tree, node.inputs[1], second)
    if scale is not None:
        _set_or_link(tree, node.inputs[3], scale)
    return node.outputs[0]


def _sample_balance_slice(tree, image_socket, red, green, blue_slice, suffix: str):
    def atlas_axis(channel):
        value = _math(tree, "ADD", channel, 0.5 / 64.0)
        value = _math(tree, "MULTIPLY", value, 255.0 / 256.0)
        value = _math(tree, "MAXIMUM", value, 1.0 / 64.0)
        value = _math(tree, "MINIMUM", value, 63.0 / 64.0)
        return _math(tree, "DIVIDE", value, 8.0)

    base_x = atlas_axis(red)
    base_y = atlas_axis(green)
    blue_slice = _math(tree, "MAXIMUM", blue_slice, 0.0)
    blue_slice = _math(tree, "MINIMUM", blue_slice, 0.99999)
    blue_row = _math(tree, "FLOOR", _math(tree, "MULTIPLY", blue_slice, 8.0))
    tile_y = _math(tree, "DIVIDE", blue_row, 8.0)
    blue_column = _math(tree, "MULTIPLY", blue_slice, 64.0)
    blue_column = _math(
        tree,
        "SUBTRACT",
        blue_column,
        _math(tree, "MULTIPLY", blue_row, 8.0),
    )
    tile_x = _math(tree, "DIVIDE", _math(tree, "FLOOR", blue_column), 8.0)

    x = _math(tree, "ADD", base_x, tile_x)
    # DDS row zero is at the top; Blender compositor UV zero is at the bottom.
    y = _math(tree, "SUBTRACT", 1.0, _math(tree, "ADD", base_y, tile_y))
    coordinates = tree.nodes.new("ShaderNodeCombineXYZ")
    coordinates.name = f"Balance Atlas UV {suffix}"
    tree.links.new(x, coordinates.inputs["X"])
    tree.links.new(y, coordinates.inputs["Y"])
    coordinates.inputs["Z"].default_value = 1.0

    sample = tree.nodes.new("CompositorNodeMapUV")
    sample.name = f"Balance Atlas Sample {suffix}"
    sample.inputs["Interpolation"].default_value = "Bilinear"
    sample.inputs["Extension X"].default_value = "Clip"
    sample.inputs["Extension Y"].default_value = "Clip"
    tree.links.new(image_socket, sample.inputs["Image"])
    tree.links.new(coordinates.outputs["Vector"], sample.inputs["UV"])
    return sample.outputs["Image"]


def _hable_tone(tree, image_socket, curve, post_scale: float):
    try:
        shoulder, linear, angle, toe, numerator, denominator = map(float, curve)
    except (TypeError, ValueError):
        shoulder, linear, angle, toe, numerator, denominator = (
            0.22,
            0.30,
            0.10,
            0.20,
            0.01,
            0.30,
        )
    denominator = max(denominator, 1.0e-6)
    x = _vector_math(tree, "MAXIMUM", image_socket, (0.0, 0.0, 0.0))
    ax = _vector_math(tree, "SCALE", x, scale=shoulder)
    top = _vector_math(
        tree,
        "ADD",
        _vector_math(
            tree,
            "MULTIPLY",
            x,
            _vector_math(tree, "ADD", ax, (angle * linear,) * 3),
        ),
        (toe * numerator,) * 3,
    )
    bottom = _vector_math(
        tree,
        "ADD",
        _vector_math(
            tree,
            "MULTIPLY",
            x,
            _vector_math(tree, "ADD", ax, (linear,) * 3),
        ),
        (toe * denominator,) * 3,
    )
    mapped = _vector_math(
        tree,
        "SUBTRACT",
        _vector_math(tree, "DIVIDE", top, bottom),
        (numerator / denominator,) * 3,
    )
    mapped = _vector_math(tree, "MAXIMUM", mapped, (0.0, 0.0, 0.0))

    def scalar_curve(value: float) -> float:
        return max(
            0.0,
            (
                (value * (shoulder * value + angle * linear) + toe * numerator)
                / (value * (shoulder * value + linear) + toe * denominator)
            )
            - numerator / denominator,
        )

    return _vector_math(
        tree,
        "SCALE",
        mapped,
        scale=max(0.0, float(post_scale)) / max(scalar_curve(11.2), 1.0e-6),
        name="W3 Hable Tone Curve",
    )


def _layout_nodes(tree) -> None:
    depths = {node: 0 for node in tree.nodes}
    for _ in tree.nodes:
        changed = False
        for link in tree.links:
            depth = depths[link.from_node] + 1
            if depth > depths[link.to_node]:
                depths[link.to_node] = depth
                changed = True
        if not changed:
            break
    columns = {}
    for node, depth in depths.items():
        columns.setdefault(depth, []).append(node)
    for depth, nodes in columns.items():
        for row, node in enumerate(sorted(nodes, key=lambda item: item.name)):
            node.location = (depth * 240.0, -row * 180.0)


def _build_tree(
    image,
    view: dict[str, Any],
    amount: float,
    brightness: float,
    exposure_ev: float,
    tone_curve,
    tone_post_scale: float,
):
    tree = bpy.data.node_groups.new(_TREE_NAME, "CompositorNodeTree")
    tree[_OWNER_PROP] = True
    try:
        tree.interface.new_socket(
            name="Image",
            in_out="OUTPUT",
            socket_type="NodeSocketColor",
        )

        render_layers = tree.nodes.new("CompositorNodeRLayers")
        exposure = tree.nodes.new("CompositorNodeExposure")
        exposure.name = "W3 Balance Exposure"
        exposure.inputs["Exposure"].default_value = float(view.get("exposure", 0.0)) + float(
            exposure_ev
        )
        tree.links.new(render_layers.outputs["Image"], exposure.inputs["Image"])

        tone_linear = _hable_tone(
            tree,
            exposure.outputs["Image"],
            tone_curve,
            tone_post_scale,
        )
        control = _vector_math(
            tree,
            "POWER",
            _vector_math(tree, "ABSOLUTE", tone_linear),
            (1.0 / 2.2, 1.0 / 2.2, 1.0 / 2.2),
        )
        channels = tree.nodes.new("ShaderNodeSeparateXYZ")
        channels.name = "W3 Balance Control Channels"
        tree.links.new(control, channels.inputs["Vector"])
        red = channels.outputs["X"]
        green = channels.outputs["Y"]
        blue = channels.outputs["Z"]

        control_blue = _math(tree, "MULTIPLY", blue, 255.0 / 256.0)
        blue_index = _math(tree, "MULTIPLY", control_blue, 64.0)
        lower_slice = _math(tree, "DIVIDE", _math(tree, "FLOOR", blue_index), 64.0)
        upper_slice = _math(tree, "ADD", control_blue, 1.0 / 64.0)
        slice_factor = _math(tree, "FRACT", blue_index)

        image_node = tree.nodes.new("CompositorNodeImage")
        image_node.name = "W3 Balance Map"
        image_node.image = image
        lower_sample = _sample_balance_slice(
            tree,
            image_node.outputs["Image"],
            red,
            green,
            lower_slice,
            "Lower",
        )
        upper_sample = _sample_balance_slice(
            tree,
            image_node.outputs["Image"],
            red,
            green,
            upper_slice,
            "Upper",
        )
        sample_delta = _vector_math(tree, "SUBTRACT", upper_sample, lower_sample)
        sample = _vector_math(
            tree,
            "ADD",
            lower_sample,
            _vector_math(tree, "SCALE", sample_delta, scale=slice_factor),
        )

        original_linear = _vector_math(tree, "ABSOLUTE", tone_linear)
        sampled_linear = _vector_math(
            tree,
            "POWER",
            _vector_math(tree, "ABSOLUTE", sample),
            (2.2, 2.2, 2.2),
        )
        sampled_linear = _vector_math(
            tree,
            "SCALE",
            sampled_linear,
            scale=float(brightness),
        )
        balance_delta = _vector_math(tree, "SUBTRACT", sampled_linear, original_linear)
        balanced = _vector_math(
            tree,
            "ADD",
            original_linear,
            _vector_math(tree, "SCALE", balance_delta, scale=float(amount)),
        )
        balanced = _vector_math(tree, "MAXIMUM", balanced, (0.0, 0.0, 0.0))

        result_channels = tree.nodes.new("ShaderNodeSeparateXYZ")
        tree.links.new(balanced, result_channels.inputs["Vector"])
        source_channels = tree.nodes.new("CompositorNodeSeparateColor")
        source_channels.mode = "RGB"
        tree.links.new(render_layers.outputs["Image"], source_channels.inputs["Image"])
        combine = tree.nodes.new("CompositorNodeCombineColor")
        combine.mode = "RGB"
        tree.links.new(result_channels.outputs["X"], combine.inputs["Red"])
        tree.links.new(result_channels.outputs["Y"], combine.inputs["Green"])
        tree.links.new(result_channels.outputs["Z"], combine.inputs["Blue"])
        tree.links.new(source_channels.outputs["Alpha"], combine.inputs["Alpha"])

        output = tree.nodes.new("NodeGroupOutput")
        output.name = "W3 Balance Output"
        output.is_active_output = True
        tree.links.new(combine.outputs["Image"], output.inputs["Image"])
        _layout_nodes(tree)
        return tree
    except Exception:
        bpy.data.node_groups.remove(tree)
        raise


def _remove_resources(tree, image) -> None:
    if tree is not None:
        try:
            bpy.data.node_groups.remove(tree)
        except (ReferenceError, RuntimeError):
            pass
    if image is not None:
        try:
            if image.users == 0:
                bpy.data.images.remove(image)
        except (ReferenceError, RuntimeError):
            pass


def apply_balance_preview(
    context,
    balance_map_path: str,
    amount: float,
    brightness: float,
    exposure_ev: float,
    tone_curve=(0.22, 0.30, 0.10, 0.20, 0.01, 0.30),
    tone_post_scale: float = 1.0,
) -> bool:
    """Apply one 512x512 flattened 64-cube LUT to rendered viewports."""

    scene = getattr(context, "scene", None)
    if (
        scene is None
        or not hasattr(scene, "compositing_node_group")
        or not balance_map_path
    ):
        return False

    key = _scene_key(scene)
    state = _STATES.get(key)
    view = state.view_settings if state is not None else _snapshot_view_settings(scene)
    image = tree = None
    try:
        image = _load_balance_image(balance_map_path)
        tree = _build_tree(
            image,
            view,
            amount,
            brightness,
            exposure_ev,
            tone_curve,
            tone_post_scale,
        )
    except Exception:
        log.exception("Could not build balance-map preview for '%s'", balance_map_path)
        _remove_resources(tree, image)
        return False

    if state is None:
        state = _PreviewState(
            scene=scene,
            previous_tree=scene.compositing_node_group,
            view_settings=view,
        )
        _STATES[key] = state
        _store_restore_state(scene, state.previous_tree, view)
    elif scene.compositing_node_group is not state.tree:
        # Preserve a compositor the user selected while the preview was active.
        state.previous_tree = scene.compositing_node_group
        scene[_PREVIOUS_TREE_PROP] = (
            state.previous_tree.name if state.previous_tree is not None else ""
        )

    previous_managed_tree = state.tree
    previous_image = state.image
    try:
        scene.compositing_node_group = tree
        state.tree = tree
        state.image = image
        _use_managed_view_settings(scene)
        _enable_viewport_compositor(context, state)
    except Exception:
        log.exception("Could not activate balance-map preview")
        scene.compositing_node_group = state.previous_tree
        _restore_view_settings(scene, state.view_settings)
        _restore_viewport_compositor(state)
        state.tree = previous_managed_tree
        state.image = previous_image
        if previous_managed_tree is not None:
            scene.compositing_node_group = previous_managed_tree
            _use_managed_view_settings(scene)
            _enable_viewport_compositor(context, state)
        else:
            _STATES.pop(key, None)
            _pop_restore_state(scene)
        _remove_resources(tree, image)
        return False

    _remove_resources(previous_managed_tree, previous_image)
    return True


def clear_balance_preview(context) -> bool:
    """Remove the managed LUT compositor and restore the user's scene state."""

    scene = getattr(context, "scene", None)
    if scene is None or not hasattr(scene, "compositing_node_group"):
        return False
    key = _scene_key(scene)
    state = _STATES.pop(key, None)
    if state is None:
        # A script/add-on reload clears _STATES but Blender keeps the assigned
        # node group. Detach that orphan so the realtime compositor does not
        # remain enabled forever with no preview state left to restore.
        tree = scene.compositing_node_group
        managed_tree = tree if tree is not None and bool(tree.get(_OWNER_PROP, False)) else None
        has_restore_state = _PREVIOUS_TREE_PROP in scene or _VIEW_SETTINGS_PROP in scene
        if managed_tree is None and not has_restore_state:
            return False
        previous_tree, view_settings = _pop_restore_state(scene)
        images = []
        if managed_tree is not None:
            for node in managed_tree.nodes:
                image = getattr(node, "image", None)
                if (
                    image is not None
                    and bool(image.get(_OWNER_PROP, False))
                    and image not in images
                ):
                    images.append(image)
            scene.compositing_node_group = previous_tree
        if view_settings:
            _restore_view_settings(scene, view_settings)
        elif scene.view_settings.view_transform == "Standard":
            # Older saved previews predate the persistent snapshot. Standard
            # was forced by the managed LUT; return those files to Blender's
            # neutral realtime fallback instead of leaving them overexposed.
            scene.view_settings.view_transform = "AgX"
        for space in _view3d_spaces(context, scene):
            if space.shading.use_compositor == "ALWAYS":
                space.shading.use_compositor = "DISABLED"
        _remove_resources(managed_tree, None)
        for image in images:
            _remove_resources(None, image)
        return True

    if scene.compositing_node_group is state.tree:
        scene.compositing_node_group = state.previous_tree
    _restore_view_settings(scene, state.view_settings)
    _restore_viewport_compositor(state)
    _remove_resources(state.tree, state.image)
    _pop_restore_state(scene)
    return True


__all__ = ("apply_balance_preview", "clear_balance_preview")
