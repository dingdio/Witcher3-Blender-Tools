"""Compact UI and operators for physics simulation."""

from __future__ import annotations

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty, IntProperty, PointerProperty, StringProperty

from ..cloth.geometry_nodes import find_clothsimulation_modifier
from ..physics import breast_blender
from ..physics import dyng_blender
from ..physics.dyng import (
    DYNG_PARSE_STATUS_PROP,
)
from .ui_utils import WITCH_PT_Base


_PHYSICS_TAB_ATTR = "witcher_physics_tab"
_PHYSICS_SCOPE_ATTR = "witcher_physics_scope"
_LEGACY_PHYSICS_FILTER_ATTR = "witcher_physics_filter"
_PHYSICS_DYNG_TARGET_ATTR = "witcher_physics_dyng_target"
_PHYSICS_BREAST_TARGET_ATTR = "witcher_physics_breast_target"
_PHYSICS_DYNG_INDEX_ATTR = "witcher_physics_dyng_index"
_PHYSICS_BREAST_INDEX_ATTR = "witcher_physics_breast_index"
_PHYSICS_CLOTH_INDEX_ATTR = "witcher_physics_cloth_index"
_BREAST_ELLIPSE_PREVIEW_ATTR = "witcher_breast_ellipse_preview"
_BREAST_ELLIPSE_PREVIEW_OFFSET_ATTR = "witcher_breast_ellipse_preview_offset"
_BREAST_ELLIPSE_PREVIEW_HANDLE = None

_PHYSICS_TAB_ITEMS = (
    ("DYNG", "Dyng", "Show CDyngResource physics armatures"),
    ("BREAST", "Breast", "Show CAnimDangleConstraint_Breast armatures"),
    ("CLOTH", "Cloth", "Show imported Redcloth ClothSimulation items"),
)

_PHYSICS_SCOPE_ITEMS = (
    ("GLOBAL", "Global", "Show every matching item in the scene"),
    ("SELECTED", "Selected", "Show items owned by the selected character"),
)

_EMPTY_PRESET_ITEMS = (("__NONE__", "No Presets", "No saved presets are available"),)

_IDPROP_DESCRIPTIONS = {
    "witcher_name": "name. Component name stored on the imported dangle constraint",
    "witcher_path": "dyng. Game-relative or resolved path to the Dyng resource used by this armature",
    DYNG_PARSE_STATUS_PROP: "Dyng parse status. Last Dyng resource parse result",
    dyng_blender.DYNG_NODE_COUNT_PROP: "CDyngResource nodes. Number of simulated nodes parsed from the Dyng resource",
    dyng_blender.DYNG_LINK_COUNT_PROP: "CDyngResource links. Number of link constraints parsed from the Dyng resource",
    dyng_blender.DYNG_TRIANGLE_COUNT_PROP: "CDyngResource triangles. Number of triangle surfaces parsed from the Dyng resource",
    dyng_blender.DYNG_COLLISION_COUNT_PROP: "CDyngResource collisions. Number of collision entries parsed from the Dyng resource",
    dyng_blender.DYNG_GRAVITY_PROP: "gravity. Gravity multiplier used by the Blender-side Dyng solver",
    dyng_blender.DYNG_DAMPENING_PROP: "dampening. Velocity retention factor; lower values damp motion faster",
    dyng_blender.DYNG_SPEED_PROP: "speed. Simulation speed multiplier for preview, step, cache, and bake",
    dyng_blender.DYNG_LINK_ITERATIONS_PROP: "max_links_iterations. Maximum solver passes for link constraints",
    dyng_blender.DYNG_USE_OFFSETS_PROP: "useOffsets component flag. Center node tethers on authored offset matrices; useful for hair containment",
    dyng_blender.DYNG_PLANE_COLLISION_PROP: "planeCollision. Keep nodes on one side of each local bone plane; this is not mesh collision",
    dyng_blender.DYNG_BODY_COLLISION_PROP: "bodyCollision. Use Blender-side body collision when supported",
    dyng_blender.DYNG_BODY_COLLISION_RADIUS_PROP: "bodyCollisionRadius. Radius used by Dyng body collision",
    dyng_blender.DYNG_BODY_COLLISION_STRENGTH_PROP: "bodyCollisionStrength. Strength of Dyng body collision response",
    dyng_blender.DYNG_SHAKE_PROP: "shake. Adds small procedural jitter to nodes that support shake",
    dyng_blender.DYNG_WIND_PROP: "wind. Per-armature multiplier for the shared Dyng wind force",
    dyng_blender.DYNG_BLEND_PROP: "blend. Blend from original pose to simulated result, from 0 to 1",
    dyng_blender.DYNG_ACCESSORY_PREVIEW_PROP: "accessoryPreview. Let accessory Dyng resources respond to wind in preview",
    dyng_blender.DYNG_LAST_STEP_PROP: "lastStep. Duration in seconds of the last Dyng simulation step",
    dyng_blender.DYNG_CACHE_STATUS_PROP: "cacheStatus. Last Dyng cache status",
    dyng_blender.DYNG_BAKE_STATUS_PROP: "bakeStatus. Last managed Dyng bake operation status",
    breast_blender.BREAST_SIM_TIME_PROP: "simTime. Simulation time step from the Breast constraint",
    breast_blender.BREAST_ELLIPSE_PROP: "elA. Ellipse center and radius values used by the Breast solver",
    breast_blender.BREAST_VEL_DAMP_PROP: "velDamp. Velocity retention factor; lower values damp motion faster",
    breast_blender.BREAST_BOUNCE_DAMP_PROP: "bounceDamp. Energy retained after bouncing on the ellipse",
    breast_blender.BREAST_IN_ACC_PROP: "inAcc. Acceleration applied when moving back inside the ellipse",
    breast_blender.BREAST_INERTIA_SCALER_PROP: "inertiaScaler. Parent-motion inertia multiplier",
    breast_blender.BREAST_BLACK_HOLE_PROP: "blackHole. Pull strength toward the simulation ellipse center",
    breast_blender.BREAST_VEL_CLAMP_PROP: "velClamp. Maximum simulated point velocity before clamping",
    breast_blender.BREAST_GRAVITY_PROP: "gravity. Constant gravity applied by the Breast solver",
    breast_blender.BREAST_MOVEMENT_WEIGHT_PROP: "movementBoneWeight. Translation influence on bones",
    breast_blender.BREAST_ROTATION_WEIGHT_PROP: "rotationBoneWeight. Rotation influence on bones",
    breast_blender.BREAST_START_OFFSET_PROP: "startSimPointOffset. Initial point offset on reset",
    breast_blender.BREAST_BLEND_PROP: "blend. Blend from original pose to simulated result, from 0 to 1",
    breast_blender.BREAST_LAST_STEP_PROP: "lastStep. Duration in seconds of the last Breast simulation step",
}


def _breast_saved_user_preset_items(self, context):
    names = breast_blender.saved_user_preset_names()
    if not names:
        return _EMPTY_PRESET_ITEMS
    return tuple((name, name.replace("_", " "), f"Delete saved Breast preset {name}") for name in names)


def _dyng_user_preset_items(self, context):
    names = dyng_blender.user_preset_names()
    if not names:
        return _EMPTY_PRESET_ITEMS
    return tuple((name, name.replace("_", " "), f"Load Blender user Dyng preset {name}") for name in names)


def _dyng_saved_user_preset_items(self, context):
    names = dyng_blender.saved_user_preset_names()
    if not names:
        return _EMPTY_PRESET_ITEMS
    return tuple((name, name.replace("_", " "), f"Delete saved Dyng preset {name}") for name in names)


def _clean_physics_name(obj):
    name = getattr(obj, "name", str(obj or ""))
    return name.replace("CAnimDangleConstraint_", "")


def _scene_target_name(context, kind: str) -> str:
    attr = _PHYSICS_DYNG_TARGET_ATTR if kind == "DYNG" else _PHYSICS_BREAST_TARGET_ATTR
    return str(getattr(context.scene, attr, "") or "")


def _target_from_scene(context, kind: str, objects):
    target_name = _scene_target_name(context, kind)
    by_name = {obj.name: obj for obj in objects}
    if target_name in by_name:
        return by_name[target_name]
    finder = dyng_blender.find_dyng_armature if kind == "DYNG" else breast_blender.find_breast_armature
    selected = finder(context)
    if selected is not None and selected.name in by_name:
        return selected
    return objects[0] if objects else None


def _unique_sorted_objects(objects):
    by_name = {obj.name: obj for obj in objects if obj is not None}
    return [by_name[name] for name in sorted(by_name, key=str.lower)]


def _short_label(text, max_chars=42):
    text = str(text or "")
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 3)] + "..."


def _root_for_panel_object(obj):
    return dyng_blender.character_root_for_object(obj)


def _owner_label_from_name(name):
    head, sep, _tail = str(name or "").partition(":")
    if sep and head.strip():
        return _short_label(head.strip(), 38)
    return ""


def _scope_label_for_object(obj):
    root = _root_for_panel_object(obj)
    if root is not None:
        root_name = getattr(root, "name", "")
        return _owner_label_from_name(root_name) or _short_label(root_name, 38)
    return _owner_label_from_name(getattr(obj, "name", "")) or "Scene / Unparented"


def _scope_label_for_root(root):
    if isinstance(root, str):
        return root
    if root is None:
        return "Scene / Unparented"
    root_name = getattr(root, "name", "Character")
    return _owner_label_from_name(root_name) or _short_label(root_name, 38)


def _panel_dyng_objects(context):
    objects = list(dyng_blender.dyng_objects_for_context(context))
    objects.extend(obj for obj in bpy.data.objects if dyng_blender.is_dyng_armature(obj))
    return _unique_sorted_objects(objects)


def _panel_breast_objects(context):
    objects = list(breast_blender.breast_objects_for_context(context))
    objects.extend(obj for obj in bpy.data.objects if breast_blender.is_breast_armature(obj))
    return _unique_sorted_objects(objects)


def _panel_cloth_objects(context):
    scene_objects = getattr(getattr(context, "scene", None), "objects", []) or []
    return _unique_sorted_objects(obj for obj in scene_objects if find_clothsimulation_modifier(obj) is not None)


_KIND_INDEX_ATTRS = {
    "DYNG": _PHYSICS_DYNG_INDEX_ATTR,
    "BREAST": _PHYSICS_BREAST_INDEX_ATTR,
    "CLOTH": _PHYSICS_CLOTH_INDEX_ATTR,
}
_KIND_LABELS = {"DYNG": "Dyng", "BREAST": "Breast", "CLOTH": "Cloth"}
_KIND_ICONS = {"DYNG": "ARMATURE_DATA", "BREAST": "PHYSICS", "CLOTH": "MOD_CLOTH"}
_KIND_OBJECTS = {"DYNG": _panel_dyng_objects, "BREAST": _panel_breast_objects, "CLOTH": _panel_cloth_objects}
_KIND_PREDICATES = {
    "DYNG": dyng_blender.is_dyng_armature,
    "BREAST": breast_blender.is_breast_armature,
    "CLOTH": lambda obj: find_clothsimulation_modifier(obj) is not None,
}


def _kind_index_attr(kind: str) -> str:
    return _KIND_INDEX_ATTRS.get(kind, _PHYSICS_CLOTH_INDEX_ATTR)


def _kind_label(kind: str) -> str:
    return _KIND_LABELS.get(kind, str(kind or "Physics").title())


def _kind_icon(kind: str) -> str:
    return _KIND_ICONS.get(kind, "MOD_CLOTH")


def _objects_for_kind(context, kind: str):
    return _KIND_OBJECTS.get(kind, _panel_cloth_objects)(context)


def _is_physics_object_kind(obj, kind: str) -> bool:
    return _KIND_PREDICATES.get(kind, _KIND_PREDICATES["CLOTH"])(obj)


def _scope_mode(context) -> str:
    return str(getattr(getattr(context, "scene", None), _PHYSICS_SCOPE_ATTR, "GLOBAL") or "GLOBAL")


def _dyng_wind_object_poll(self, obj) -> bool:
    return dyng_blender.is_wind_field_object(obj)


def _tag_3d_viewports_redraw():
    window_manager = getattr(bpy.context, "window_manager", None)
    for window in getattr(window_manager, "windows", []) or []:
        screen = getattr(window, "screen", None)
        for area in getattr(screen, "areas", []) or []:
            if getattr(area, "type", "") == "VIEW_3D":
                area.tag_redraw()


def _breast_ellipse_preview_offset(context):
    scene = getattr(context, "scene", None)
    if scene is None:
        return 0.08
    try:
        return float(getattr(scene, _BREAST_ELLIPSE_PREVIEW_OFFSET_ATTR, 0.08) or 0.0)
    except (TypeError, ValueError):
        return 0.08


def _preview_center_cross(points, scale=0.18):
    if len(points) < 8:
        return []
    loop = points[:-1] if points[0] == points[-1] else points
    if not loop:
        return []
    center = tuple(sum(point[index] for point in loop) / len(loop) for index in range(3))

    def marker_axis(point):
        vec = tuple(point[index] - center[index] for index in range(3))
        length = sum(value * value for value in vec) ** 0.5
        if length <= 1e-8:
            return None
        marker = max(0.015, length * float(scale))
        unit = tuple(value / length for value in vec)
        return (
            tuple(center[index] - unit[index] * marker for index in range(3)),
            tuple(center[index] + unit[index] * marker for index in range(3)),
        )

    quarter_index = max(1, len(loop) // 4)
    segments = []
    for pair in (marker_axis(loop[0]), marker_axis(loop[quarter_index])):
        if pair is not None:
            segments.extend(pair)
    return segments


def _preview_point_marker(point, size=0.025):
    try:
        x, y, z = (float(value) for value in point)
    except (TypeError, ValueError):
        return []
    return [
        (x - size, y, z),
        (x + size, y, z),
        (x, y - size, z),
        (x, y + size, z),
        (x, y, z - size),
        (x, y, z + size),
    ]


def _preview_fill_triangles(points):
    if len(points) < 4:
        return []
    loop = points[:-1] if points[0] == points[-1] else points
    if len(loop) < 3:
        return []
    center = tuple(sum(point[index] for point in loop) / len(loop) for index in range(3))
    triangles = []
    for index, point in enumerate(loop):
        triangles.extend((center, point, loop[(index + 1) % len(loop)]))
    return triangles


def _preview_line_shader(gpu, context):
    try:
        shader = gpu.shader.from_builtin("POLYLINE_UNIFORM_COLOR")
        region = getattr(context, "region", None)
        width = float(getattr(region, "width", 1) or 1)
        height = float(getattr(region, "height", 1) or 1)
        return shader, (width, height)
    except Exception:
        try:
            return gpu.shader.from_builtin("UNIFORM_COLOR"), None
        except Exception:
            return None, None


def _draw_preview_points(shader, batch_for_shader, points, primitive, color, *, viewport_size=None, line_width=2.0):
    if len(points) < 2:
        return
    shader.bind()
    if viewport_size is not None:
        try:
            shader.uniform_float("viewportSize", viewport_size)
            shader.uniform_float("lineWidth", float(line_width))
        except Exception:
            pass
    shader.uniform_float("color", color)
    batch = batch_for_shader(shader, primitive, {"pos": points})
    batch.draw(shader)


def _draw_breast_ellipse_preview():
    context = bpy.context
    scene = getattr(context, "scene", None)
    if scene is None or not bool(getattr(scene, _BREAST_ELLIPSE_PREVIEW_ATTR, False)):
        return
    if str(getattr(scene, _PHYSICS_TAB_ATTR, "") or "") != "BREAST":
        return
    obj = _active_list_object(context, "BREAST", _panel_breast_objects(context))
    if obj is None:
        return
    guides = breast_blender.ellipse_preview_guides(
        obj,
        display_offset=_breast_ellipse_preview_offset(context),
    )
    if not guides:
        return
    try:
        import gpu
        from gpu_extras.batch import batch_for_shader
    except Exception:
        return

    try:
        shader, viewport_size = _preview_line_shader(gpu, context)
        if shader is None:
            return
        try:
            fill_shader = gpu.shader.from_builtin("UNIFORM_COLOR")
        except Exception:
            fill_shader = None
        try:
            gpu.state.blend_set("ALPHA")
            gpu.state.depth_test_set("NONE")
            gpu.state.line_width_set(2.0)
        except Exception:
            pass
        for bone_name, data in guides.items():
            points = list(data.get("ellipse", []) or [])
            if not points:
                continue
            color = (1.0, 0.35, 0.55, 0.95) if bone_name.startswith("l_") else (0.35, 0.65, 1.0, 0.95)
            fill_color = color[:3] + (0.12,)
            if fill_shader is not None:
                _draw_preview_points(
                    fill_shader,
                    batch_for_shader,
                    _preview_fill_triangles(points),
                    "TRIS",
                    fill_color,
                )
            _draw_preview_points(
                shader,
                batch_for_shader,
                points,
                "LINE_STRIP",
                color,
                viewport_size=viewport_size,
                line_width=3.0,
            )
            _draw_preview_points(
                shader,
                batch_for_shader,
                _preview_center_cross(points),
                "LINES",
                (1.0, 1.0, 1.0, 0.95),
                viewport_size=viewport_size,
                line_width=2.0,
            )
            center = data.get("center")
            bone = data.get("bone")
            if center and bone:
                _draw_preview_points(
                    shader,
                    batch_for_shader,
                    [bone, center],
                    "LINES",
                    (1.0, 0.9, 0.2, 0.7),
                    viewport_size=viewport_size,
                    line_width=1.5,
                )
            start = data.get("start")
            if start:
                _draw_preview_points(
                    shader,
                    batch_for_shader,
                    _preview_point_marker(start),
                    "LINES",
                    (1.0, 0.75, 0.2, 0.95),
                    viewport_size=viewport_size,
                    line_width=2.0,
                )
    finally:
        try:
            gpu.state.line_width_set(1.0)
            gpu.state.depth_test_set("LESS_EQUAL")
            gpu.state.blend_set("NONE")
        except Exception:
            pass


def _remove_breast_ellipse_preview_handler():
    global _BREAST_ELLIPSE_PREVIEW_HANDLE
    if _BREAST_ELLIPSE_PREVIEW_HANDLE is None:
        return
    try:
        bpy.types.SpaceView3D.draw_handler_remove(_BREAST_ELLIPSE_PREVIEW_HANDLE, "WINDOW")
    except Exception:
        pass
    _BREAST_ELLIPSE_PREVIEW_HANDLE = None
    _tag_3d_viewports_redraw()


def _sync_breast_ellipse_preview_handler(scene=None):
    global _BREAST_ELLIPSE_PREVIEW_HANDLE
    scene = scene or getattr(bpy.context, "scene", None)
    enabled = bool(getattr(scene, _BREAST_ELLIPSE_PREVIEW_ATTR, False)) if scene is not None else False
    if enabled and _BREAST_ELLIPSE_PREVIEW_HANDLE is None:
        try:
            _BREAST_ELLIPSE_PREVIEW_HANDLE = bpy.types.SpaceView3D.draw_handler_add(
                _draw_breast_ellipse_preview,
                (),
                "WINDOW",
                "POST_VIEW",
            )
        except Exception:
            _BREAST_ELLIPSE_PREVIEW_HANDLE = None
    elif not enabled:
        _remove_breast_ellipse_preview_handler()
        return
    _tag_3d_viewports_redraw()


def _on_breast_ellipse_preview_changed(scene, context):
    _sync_breast_ellipse_preview_handler(scene)


def _on_physics_live_preview_changed(scene, context):
    # Rebuild handlers in both directions.  In particular, a handler removes
    # itself after a frame change while global preview is off.
    dyng_blender.ensure_frame_handler()
    breast_blender.ensure_frame_handler()


def _refresh_breast_ellipse_preview(context):
    scene = getattr(context, "scene", None)
    if scene is not None and bool(getattr(scene, _BREAST_ELLIPSE_PREVIEW_ATTR, False)):
        _sync_breast_ellipse_preview_handler(scene)
    _tag_3d_viewports_redraw()


def _object_matches_scope(context, obj) -> bool:
    if _scope_mode(context) != "SELECTED":
        return True
    root = dyng_blender.find_character_root(context)
    if root is None:
        return False
    return _scope_label_for_object(obj) == _scope_label_for_root(root)


def _object_matches_list_filter(obj, filter_text: str) -> bool:
    needle = str(filter_text or "").strip().lower()
    if not needle:
        return True
    haystack = " ".join(
        (
            str(getattr(obj, "name", "")),
            _clean_physics_name(obj),
            _scope_label_for_object(obj),
            str(obj.get("witcher_path", "") if hasattr(obj, "get") else ""),
        )
    ).lower()
    return needle in haystack


def _object_visible_for_kind(context, obj, kind: str) -> bool:
    return _is_physics_object_kind(obj, kind) and _object_matches_scope(context, obj)


def _filtered_objects_for_kind(context, kind: str, objects=None):
    if objects is None:
        objects = _objects_for_kind(context, kind)
    return [obj for obj in objects if _object_visible_for_kind(context, obj, kind)]


def _active_list_object(context, kind: str, objects=None):
    if objects is None:
        objects = _objects_for_kind(context, kind)
    visible_objects = _filtered_objects_for_kind(context, kind, objects)
    scene = getattr(context, "scene", None)
    if scene is None:
        return visible_objects[0] if visible_objects else None
    index = int(getattr(scene, _kind_index_attr(kind), -1))
    data_objects = getattr(bpy.data, "objects", [])
    if 0 <= index < len(data_objects):
        obj = data_objects[index]
        if obj in visible_objects:
            return obj
    if kind in {"DYNG", "BREAST"}:
        target_name = _scene_target_name(context, kind)
        for obj in visible_objects:
            if obj.name == target_name:
                return obj
    return visible_objects[0] if visible_objects else None


def _operator_dyng_object(context, object_name=""):
    if object_name:
        obj = bpy.data.objects.get(object_name)
        if obj is not None and dyng_blender.is_dyng_armature(obj):
            return obj
    return dyng_blender.find_dyng_armature(context)


def _operator_breast_object(context, object_name=""):
    if object_name:
        obj = bpy.data.objects.get(object_name)
        if obj is not None and breast_blender.is_breast_armature(obj):
            return obj
    return breast_blender.find_breast_armature(context)


def _select_object(context, obj, *, ensure_object_mode=True) -> None:
    if ensure_object_mode and bpy.ops.object.mode_set.poll():
        bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    context.view_layer.objects.active = obj


class WITCH_OT_PhysicsSetTarget(bpy.types.Operator):
    bl_idname = "witcher.physics_set_target"
    bl_label = "Set Physics Target"
    bl_description = "Set the armature targeted by the Physics panel"
    bl_options = {"REGISTER", "UNDO"}

    kind: EnumProperty(items=_PHYSICS_TAB_ITEMS)
    object_name: StringProperty(options={"HIDDEN"})
    select_object: BoolProperty(default=False)

    def execute(self, context):
        obj = bpy.data.objects.get(self.object_name)
        if self.kind == "DYNG":
            if obj is None or not dyng_blender.is_dyng_armature(obj):
                self.report({"WARNING"}, "Dyng target not found.")
                return {"CANCELLED"}
            setattr(context.scene, _PHYSICS_DYNG_TARGET_ATTR, obj.name)
            setattr(context.scene, _PHYSICS_TAB_ATTR, "DYNG")
        elif self.kind == "BREAST":
            if obj is None or not breast_blender.is_breast_armature(obj):
                self.report({"WARNING"}, "Breast target not found.")
                return {"CANCELLED"}
            setattr(context.scene, _PHYSICS_BREAST_TARGET_ATTR, obj.name)
            setattr(context.scene, _PHYSICS_TAB_ATTR, "BREAST")
        else:
            self.report({"WARNING"}, "Unsupported physics target.")
            return {"CANCELLED"}

        if self.select_object:
            _select_object(context, obj)
        return {"FINISHED"}


class WITCH_OT_DyngLoadData(bpy.types.Operator):
    bl_idname = "witcher.dyng_load_data"
    bl_label = "Load Dyng Resource"
    bl_description = "Parse the Dyng resource path and create editable runtime values for this armature"
    bl_options = {"REGISTER", "UNDO"}

    object_name: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        obj = _operator_dyng_object(context, self.object_name)
        if obj is None:
            self.report({"WARNING"}, "Select a Dyng armature.")
            return {"CANCELLED"}
        resource = dyng_blender.load_resource_for_object(obj)
        if resource is None:
            self.report({"WARNING"}, str(obj.get(dyng_blender.DYNG_SIM_STATUS_PROP, "Dyng data not loaded")))
            return {"CANCELLED"}
        dyng_blender.ensure_default_props(obj)
        self.report({"INFO"}, f"Loaded {len(resource.nodes)} Dyng nodes.")
        return {"FINISHED"}


class WITCH_OT_DyngToggleRuntime(bpy.types.Operator):
    bl_idname = "witcher.dyng_toggle_runtime"
    bl_label = "Toggle Dyng Preview"
    bl_description = "Start or pause live Dyng preview for this armature"
    bl_options = {"REGISTER", "UNDO"}

    enable: BoolProperty(default=True)
    object_name: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        obj = _operator_dyng_object(context, self.object_name)
        if obj is None:
            self.report({"WARNING"}, "Select a Dyng armature.")
            return {"CANCELLED"}
        if not dyng_blender.enable_dyng_object(obj, bool(self.enable)):
            self.report({"WARNING"}, "Dyng data is not available.")
            return {"CANCELLED"}
        action = "Enabled" if self.enable else "Disabled"
        self.report({"INFO"}, f"{action} Dyng runtime.")
        return {"FINISHED"}


class WITCH_OT_DyngToggleAllRuntime(bpy.types.Operator):
    bl_idname = "witcher.dyng_toggle_all_runtime"
    bl_label = "Toggle Dyng Preview Scope"
    bl_description = "Start or pause live Dyng preview for armatures matching the current list scope"
    bl_options = {"REGISTER", "UNDO"}

    enable: BoolProperty(default=True)

    def execute(self, context):
        objects = _filtered_objects_for_kind(context, "DYNG")
        if not objects:
            self.report({"WARNING"}, "No Dyng armatures match the current scope.")
            return {"CANCELLED"}
        count = dyng_blender.enable_dyng_objects(objects, bool(self.enable))
        if count <= 0:
            self.report({"WARNING"}, "No Dyng armatures could be loaded.")
            return {"CANCELLED"}
        action = "Enabled" if self.enable else "Disabled"
        self.report({"INFO"}, f"{action} {count} Dyng armature(s) in scope.")
        return {"FINISHED"}


class WITCH_OT_DyngSelect(bpy.types.Operator):
    bl_idname = "witcher.dyng_select"
    bl_label = "Select Dyng"
    bl_description = "Select this Dyng armature"
    bl_options = {"REGISTER", "UNDO"}

    object_name: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        obj = bpy.data.objects.get(self.object_name)
        if obj is None:
            self.report({"WARNING"}, "Dyng armature not found.")
            return {"CANCELLED"}
        _select_object(context, obj)
        return {"FINISHED"}


class WITCH_OT_DyngStep(bpy.types.Operator):
    bl_idname = "witcher.dyng_step"
    bl_label = "Step Dyng"
    bl_description = "Run one deterministic Dyng simulation step on the selected armature"
    bl_options = {"REGISTER", "UNDO"}

    reset: BoolProperty(default=False)
    object_name: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        obj = _operator_dyng_object(context, self.object_name)
        if obj is None:
            self.report({"WARNING"}, "Select a Dyng armature.")
            return {"CANCELLED"}
        scene = context.scene
        dt = float(scene.render.fps_base or 1.0) / float(scene.render.fps or 24)
        if not dyng_blender.step_object(obj, dt, reset=bool(self.reset)):
            self.report({"WARNING"}, "No Dyng bones were updated.")
            return {"CANCELLED"}
        context.view_layer.update()
        return {"FINISHED"}


class WITCH_OT_DyngReset(bpy.types.Operator):
    bl_idname = "witcher.dyng_reset"
    bl_label = "Reset Dyng"
    bl_description = "Reset Dyng simulation state and restore dynamic bones to their target pose"
    bl_options = {"REGISTER", "UNDO"}

    object_name: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        obj = _operator_dyng_object(context, self.object_name)
        if obj is None:
            self.report({"WARNING"}, "Select a Dyng armature.")
            return {"CANCELLED"}
        if not dyng_blender.reset_object(obj):
            self.report({"WARNING"}, "Dyng reset failed.")
            return {"CANCELLED"}
        context.view_layer.update()
        return {"FINISHED"}


class WITCH_OT_DyngBake(bpy.types.Operator):
    bl_idname = "witcher.dyng_bake"
    bl_label = "Bake Dyng"
    bl_description = "Bake Dyng into an owned Action while preserving non-Dyng source animation"
    bl_options = {"REGISTER", "UNDO"}

    object_name: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        obj = bpy.data.objects.get(self.object_name) if self.object_name else None
        if obj is None or not dyng_blender.is_dyng_armature(obj):
            self.report({"WARNING"}, "The specified Dyng armature no longer exists.")
            return {"CANCELLED"}
        scene = context.scene
        frame_start = int(scene.frame_start)
        frame_end = int(scene.frame_end)
        total_frames = abs(frame_end - frame_start) + 1
        window_manager = context.window_manager
        window_manager.progress_begin(0, total_frames)
        try:
            count = dyng_blender.bake_object(
                context,
                obj,
                frame_start,
                frame_end,
                progress_callback=lambda completed, _total: window_manager.progress_update(completed),
            )
        except Exception as exc:
            if not str(obj.get(dyng_blender.DYNG_BAKE_STATUS_PROP, "")).startswith("Bake failed"):
                obj[dyng_blender.DYNG_BAKE_STATUS_PROP] = "Bake failed; see report"
            self.report({"ERROR"}, f"Dyng bake failed: {exc}")
            return {"CANCELLED"}
        finally:
            window_manager.progress_end()
        if count <= 0:
            self.report({"WARNING"}, "No Dyng keys were baked.")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Baked {count} Dyng bone-frame samples.")
        return {"FINISHED"}


class WITCH_OT_DyngDeleteBake(bpy.types.Operator):
    bl_idname = "witcher.dyng_delete_bake"
    bl_label = "Delete Entire Generated Dyng Action"
    bl_description = "Delete the entire generated Action, including later edits, and restore the source Action"
    bl_options = {"REGISTER", "UNDO"}

    object_name: StringProperty(options={"HIDDEN"})

    @classmethod
    def poll(cls, context):
        return getattr(context, "scene", None) is not None

    def invoke(self, context, event):
        obj = bpy.data.objects.get(self.object_name)
        if obj is None or not dyng_blender.is_dyng_armature(obj):
            self.report({"WARNING"}, "The specified Dyng armature no longer exists.")
            return {"CANCELLED"}
        info = dyng_blender.bake_info(obj)
        if not info.managed:
            self.report({"WARNING"}, "This armature has no managed Dyng bake.")
            return {"CANCELLED"}
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        # Destructive operations never fall back to selection or another list row.
        obj = bpy.data.objects.get(self.object_name)
        if obj is None or not dyng_blender.is_dyng_armature(obj):
            self.report({"WARNING"}, "The specified Dyng armature no longer exists.")
            return {"CANCELLED"}
        info = dyng_blender.bake_info(obj)
        if not info.managed or not dyng_blender.delete_bake(obj):
            status = str(obj.get(dyng_blender.DYNG_BAKE_STATUS_PROP, "No owned Dyng bake was deleted."))
            self.report({"WARNING"}, status)
            return {"CANCELLED"}
        self.report({"INFO"}, f"Deleted Dyng bake from {obj.name}.")
        return {"FINISHED"}


class WITCH_OT_DyngDeleteLegacyBake(bpy.types.Operator):
    bl_idname = "witcher.dyng_delete_legacy_bake"
    bl_label = "Delete Possible Legacy Dyng Bake"
    bl_description = "Permanently delete an untagged Action that looks like an old Dyng bake; verify it is not user animation"
    bl_options = {"REGISTER", "UNDO"}

    object_name: StringProperty(options={"HIDDEN"})

    def invoke(self, context, event):
        obj = bpy.data.objects.get(self.object_name)
        info = dyng_blender.bake_info(obj) if obj is not None else None
        if obj is None or not dyng_blender.is_dyng_armature(obj) or info is None or not info.legacy:
            self.report({"WARNING"}, "No strictly recognized legacy Dyng bake was found.")
            return {"CANCELLED"}
        if int(getattr(info.action, "users", 0) or 0) != 1:
            self.report({"WARNING"}, "The legacy Action is shared and cannot be safely deleted.")
            return {"CANCELLED"}
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        obj = bpy.data.objects.get(self.object_name)
        if obj is None or not dyng_blender.is_dyng_armature(obj):
            self.report({"WARNING"}, "The specified Dyng armature no longer exists.")
            return {"CANCELLED"}
        if not dyng_blender.delete_legacy_bake(obj):
            self.report({"WARNING"}, "The legacy Action changed or is not safe to delete.")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Deleted legacy Dyng bake from {obj.name}.")
        return {"FINISHED"}


class WITCH_OT_DyngCache(bpy.types.Operator):
    bl_idname = "witcher.dyng_cache"
    bl_label = "Cache Dyng Range"
    bl_description = "Build an in-memory Dyng cache over the scene frame range"
    bl_options = {"REGISTER", "UNDO"}

    object_name: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        obj = _operator_dyng_object(context, self.object_name)
        if obj is None:
            self.report({"WARNING"}, "Select a Dyng armature.")
            return {"CANCELLED"}
        scene = context.scene
        count = dyng_blender.build_cache_for_object(context, obj, int(scene.frame_start), int(scene.frame_end))
        if count <= 0:
            self.report({"WARNING"}, "No Dyng frames were cached.")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Cached {count} Dyng frame(s).")
        return {"FINISHED"}


class WITCH_OT_DyngApplyUserPreset(bpy.types.Operator):
    bl_idname = "witcher.dyng_apply_user_preset"
    bl_label = "Load Dyng User Preset"
    bl_description = "Load a Blender user Dyng preset onto this armature"
    bl_options = {"REGISTER", "UNDO"}

    preset: EnumProperty(
        name="Preset",
        description="Blender user Dyng preset",
        items=_dyng_user_preset_items,
    )
    object_name: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        obj = _operator_dyng_object(context, self.object_name)
        if obj is None:
            self.report({"WARNING"}, "Select a Dyng armature.")
            return {"CANCELLED"}
        if self.preset == "__NONE__":
            self.report({"WARNING"}, "No Dyng preset selected.")
            return {"CANCELLED"}
        if not dyng_blender.apply_user_preset(obj, self.preset):
            self.report({"WARNING"}, "Dyng preset could not be loaded.")
            return {"CANCELLED"}
        context.view_layer.update()
        self.report({"INFO"}, f"Loaded Dyng preset {self.preset}.")
        return {"FINISHED"}


class WITCH_OT_DyngSaveUserPreset(bpy.types.Operator):
    bl_idname = "witcher.dyng_save_user_preset"
    bl_label = "Save Dyng User Preset"
    bl_description = "Save the current Dyng Blender runtime values as a user preset"
    bl_options = {"REGISTER", "UNDO"}

    preset_name: StringProperty(name="Preset Name", default="")
    object_name: StringProperty(options={"HIDDEN"})

    def invoke(self, context, event):
        obj = _operator_dyng_object(context, self.object_name)
        if obj is not None and not self.preset_name:
            current = str(obj.get(dyng_blender.DYNG_PRESET_PROP, "") or "").strip()
            self.preset_name = current if current and current not in dyng_blender.redkit_preset_names() else "Dyng Preset"
        return context.window_manager.invoke_props_dialog(self, width=360)

    def draw(self, context):
        self.layout.prop(self, "preset_name", text="Name")

    def execute(self, context):
        obj = _operator_dyng_object(context, self.object_name)
        if obj is None:
            self.report({"WARNING"}, "Select a Dyng armature.")
            return {"CANCELLED"}
        try:
            saved_name = dyng_blender.save_user_preset(obj, self.preset_name)
        except ValueError as exc:
            self.report({"WARNING"}, str(exc))
            return {"CANCELLED"}
        context.view_layer.update()
        self.report({"INFO"}, f"Saved Dyng preset {saved_name}.")
        return {"FINISHED"}


class WITCH_OT_DyngDeleteUserPreset(bpy.types.Operator):
    bl_idname = "witcher.dyng_delete_user_preset"
    bl_label = "Delete Dyng User Preset"
    bl_description = "Delete the current saved Dyng user preset"
    bl_options = {"REGISTER", "UNDO"}

    preset: EnumProperty(
        name="Preset",
        description="Saved Dyng user preset",
        items=_dyng_saved_user_preset_items,
    )

    def invoke(self, context, event):
        if self.preset == "__NONE__":
            return self.execute(context)
        return context.window_manager.invoke_props_dialog(self, width=320)

    def draw(self, context):
        label = str(self.preset or "").replace("_", " ")
        self.layout.label(text=f'Delete "{label}"?', icon="TRASH")

    def execute(self, context):
        if self.preset == "__NONE__":
            self.report({"WARNING"}, "No saved Dyng preset selected.")
            return {"CANCELLED"}
        if not dyng_blender.delete_user_preset(self.preset):
            self.report({"WARNING"}, "Saved Dyng preset not found.")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Deleted Dyng preset {self.preset}.")
        return {"FINISHED"}


class WITCH_OT_BreastToggleRuntime(bpy.types.Operator):
    bl_idname = "witcher.breast_toggle_runtime"
    bl_label = "Toggle Breast Preview"
    bl_description = "Start or pause live Breast physics preview for this armature"
    bl_options = {"REGISTER", "UNDO"}

    enable: BoolProperty(default=True)
    object_name: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        obj = _operator_breast_object(context, self.object_name)
        if obj is None:
            self.report({"WARNING"}, "Select a Breast physics armature.")
            return {"CANCELLED"}
        if not breast_blender.enable_breast_object(obj, bool(self.enable)):
            self.report({"WARNING"}, "Breast physics bones are not available.")
            return {"CANCELLED"}
        return {"FINISHED"}


class WITCH_OT_BreastToggleAllRuntime(bpy.types.Operator):
    bl_idname = "witcher.breast_toggle_all_runtime"
    bl_label = "Toggle Breast Preview Scope"
    bl_description = "Start or pause live Breast physics preview for armatures matching the current list scope"
    bl_options = {"REGISTER", "UNDO"}

    enable: BoolProperty(default=True)

    def execute(self, context):
        objects = _filtered_objects_for_kind(context, "BREAST")
        if not objects:
            self.report({"WARNING"}, "No Breast physics armatures match the current scope.")
            return {"CANCELLED"}
        count = breast_blender.enable_breast_objects(objects, bool(self.enable))
        if count <= 0:
            self.report({"WARNING"}, "No Breast physics armatures could be enabled.")
            return {"CANCELLED"}
        action = "Enabled" if self.enable else "Disabled"
        self.report({"INFO"}, f"{action} {count} Breast physics armature(s) in scope.")
        return {"FINISHED"}


class WITCH_OT_BreastSelect(bpy.types.Operator):
    bl_idname = "witcher.breast_select"
    bl_label = "Select Breast"
    bl_description = "Select this Breast physics armature"
    bl_options = {"REGISTER", "UNDO"}

    object_name: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        obj = bpy.data.objects.get(self.object_name)
        if obj is None:
            self.report({"WARNING"}, "Breast physics armature not found.")
            return {"CANCELLED"}
        _select_object(context, obj)
        return {"FINISHED"}


class WITCH_OT_BreastApplyPreset(bpy.types.Operator):
    bl_idname = "witcher.breast_apply_preset"
    bl_label = "Load Breast Preset"
    bl_description = "Load a Breast constraint preset onto this armature"
    bl_options = {"REGISTER", "UNDO"}

    preset: StringProperty(name="Preset", default="")
    object_name: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        obj = _operator_breast_object(context, self.object_name)
        if obj is None:
            self.report({"WARNING"}, "Select a Breast physics armature.")
            return {"CANCELLED"}
        if not self.preset:
            self.report({"WARNING"}, "No Breast preset selected.")
            return {"CANCELLED"}
        if not breast_blender.apply_preset(obj, self.preset):
            self.report({"WARNING"}, "Breast preset could not be loaded.")
            return {"CANCELLED"}
        context.view_layer.update()
        _refresh_breast_ellipse_preview(context)
        self.report({"INFO"}, f"Loaded Breast preset {self.preset}.")
        return {"FINISHED"}


class WITCH_MT_BreastPresetMenu(bpy.types.Menu):
    bl_idname = "WITCH_MT_breast_presets"
    bl_label = "Breast Presets"

    def draw(self, context):
        layout = self.layout
        objects = _panel_breast_objects(context)
        obj = _active_list_object(context, "BREAST", objects) or _target_from_scene(context, "BREAST", objects)
        object_name = obj.name if obj is not None else ""

        def add_preset(name, *, icon="PRESET"):
            label = name if name == breast_blender.CUSTOM_PRESET_NAME else name.replace("_", " ")
            op = layout.operator(WITCH_OT_BreastApplyPreset.bl_idname, text=label, icon=icon)
            op.preset = name
            op.object_name = object_name

        add_preset(breast_blender.CUSTOM_PRESET_NAME, icon="INFO")
        layout.separator()
        layout.label(text="REDkit Presets", icon="PRESET")
        for name in breast_blender.redkit_preset_names():
            add_preset(name)
        layout.separator()
        layout.label(text="Saved Presets", icon="FILE_TICK")
        saved_names = breast_blender.saved_user_preset_names()
        if saved_names:
            for name in saved_names:
                add_preset(name, icon="FILE_TICK")
        else:
            row = layout.row()
            row.enabled = False
            row.label(text="No saved presets", icon="INFO")


class WITCH_OT_BreastSaveUserPreset(bpy.types.Operator):
    bl_idname = "witcher.breast_save_user_preset"
    bl_label = "Save Breast User Preset"
    bl_description = "Save the current Breast values as a user preset"
    bl_options = {"REGISTER", "UNDO"}

    preset_name: StringProperty(name="Preset Name", default="")
    object_name: StringProperty(options={"HIDDEN"})

    def invoke(self, context, event):
        obj = _operator_breast_object(context, self.object_name)
        if obj is not None and not self.preset_name:
            current = str(obj.get(breast_blender.BREAST_PRESET_PROP, "") or "").strip()
            reserved = {name.lower() for name in breast_blender.redkit_preset_names()}
            reserved.add(breast_blender.CUSTOM_PRESET_NAME.lower())
            self.preset_name = current if current and current.lower() not in reserved else "Breast Preset"
        return context.window_manager.invoke_props_dialog(self, width=360)

    def draw(self, context):
        self.layout.prop(self, "preset_name", text="Name")

    def execute(self, context):
        obj = _operator_breast_object(context, self.object_name)
        if obj is None:
            self.report({"WARNING"}, "Select a Breast physics armature.")
            return {"CANCELLED"}
        try:
            saved_name = breast_blender.save_user_preset(obj, self.preset_name)
        except ValueError as exc:
            self.report({"WARNING"}, str(exc))
            return {"CANCELLED"}
        context.view_layer.update()
        _refresh_breast_ellipse_preview(context)
        self.report({"INFO"}, f"Saved Breast preset {saved_name}.")
        return {"FINISHED"}


class WITCH_OT_BreastDeleteUserPreset(bpy.types.Operator):
    bl_idname = "witcher.breast_delete_user_preset"
    bl_label = "Delete Breast User Preset"
    bl_description = "Delete the current saved Breast user preset"
    bl_options = {"REGISTER", "UNDO"}

    preset: EnumProperty(
        name="Preset",
        description="Saved Breast user preset",
        items=_breast_saved_user_preset_items,
    )

    def invoke(self, context, event):
        if self.preset == "__NONE__":
            return self.execute(context)
        return context.window_manager.invoke_props_dialog(self, width=320)

    def draw(self, context):
        label = str(self.preset or "").replace("_", " ")
        self.layout.label(text=f'Delete "{label}"?', icon="TRASH")

    def execute(self, context):
        if self.preset == "__NONE__":
            self.report({"WARNING"}, "No saved Breast preset selected.")
            return {"CANCELLED"}
        if not breast_blender.delete_user_preset(self.preset):
            self.report({"WARNING"}, "Saved Breast preset not found.")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Deleted Breast preset {self.preset}.")
        return {"FINISHED"}


class WITCH_OT_BreastStep(bpy.types.Operator):
    bl_idname = "witcher.breast_step"
    bl_label = "Step Breast"
    bl_description = "Run one deterministic Breast physics simulation step on the selected armature"
    bl_options = {"REGISTER", "UNDO"}

    reset: BoolProperty(default=False)
    object_name: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        obj = _operator_breast_object(context, self.object_name)
        if obj is None:
            self.report({"WARNING"}, "Select a Breast physics armature.")
            return {"CANCELLED"}
        scene = context.scene
        dt = float(scene.render.fps_base or 1.0) / float(scene.render.fps or 24)
        if not breast_blender.step_object(obj, dt, reset=bool(self.reset)):
            self.report({"WARNING"}, "No Breast physics bones were updated.")
            return {"CANCELLED"}
        context.view_layer.update()
        return {"FINISHED"}


class WITCH_OT_BreastReset(bpy.types.Operator):
    bl_idname = "witcher.breast_reset"
    bl_label = "Reset Breast"
    bl_description = "Reset Breast physics simulation state and restore target pose"
    bl_options = {"REGISTER", "UNDO"}

    object_name: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        obj = _operator_breast_object(context, self.object_name)
        if obj is None:
            self.report({"WARNING"}, "Select a Breast physics armature.")
            return {"CANCELLED"}
        if not breast_blender.reset_object(obj):
            self.report({"WARNING"}, "Breast physics reset failed.")
            return {"CANCELLED"}
        context.view_layer.update()
        return {"FINISHED"}


class WITCH_OT_BreastBake(bpy.types.Operator):
    bl_idname = "witcher.breast_bake"
    bl_label = "Bake Breast"
    bl_description = "Bake Breast physics simulation to pose-bone keyframes over the scene frame range"
    bl_options = {"REGISTER", "UNDO"}

    object_name: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        obj = _operator_breast_object(context, self.object_name)
        if obj is None:
            self.report({"WARNING"}, "Select a Breast physics armature.")
            return {"CANCELLED"}
        scene = context.scene
        count = breast_blender.bake_object(context, obj, int(scene.frame_start), int(scene.frame_end))
        if count <= 0:
            self.report({"WARNING"}, "No Breast physics keys were baked.")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Baked {count} Breast physics bone keys.")
        return {"FINISHED"}


class WITCH_OT_ClothSelect(bpy.types.Operator):
    bl_idname = "witcher.physics_cloth_select"
    bl_label = "Select Cloth"
    bl_description = "Select this Redcloth ClothSimulation mesh"
    bl_options = {"REGISTER", "UNDO"}

    object_name: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        obj = bpy.data.objects.get(self.object_name)
        if obj is None or find_clothsimulation_modifier(obj) is None:
            self.report({"WARNING"}, "Cloth item not found.")
            return {"CANCELLED"}
        _select_object(context, obj)
        return {"FINISHED"}


class WITCH_OT_ClothToggleSimulationObject(bpy.types.Operator):
    bl_idname = "witcher.physics_cloth_toggle_object"
    bl_label = "Toggle Cloth Simulation"
    bl_description = "Show or hide this object's ClothSimulation modifier"
    bl_options = {"REGISTER", "UNDO"}

    object_name: StringProperty(options={"HIDDEN"})
    show: BoolProperty(default=True)

    def execute(self, context):
        obj = bpy.data.objects.get(self.object_name)
        mod = find_clothsimulation_modifier(obj)
        if mod is None:
            self.report({"WARNING"}, "ClothSimulation modifier not found.")
            return {"CANCELLED"}
        mod.show_viewport = bool(self.show)
        mod.show_render = bool(self.show)
        self.report({"INFO"}, f"{'Showed' if self.show else 'Hid'} ClothSimulation on {obj.name}")
        return {"FINISHED"}


class WITCH_OT_ClothToggleScope(bpy.types.Operator):
    bl_idname = "witcher.physics_cloth_toggle_scope"
    bl_label = "Toggle Cloth Simulation Scope"
    bl_description = "Show or hide ClothSimulation modifiers matching the current list scope"
    bl_options = {"REGISTER", "UNDO"}

    show: BoolProperty(default=True)

    def execute(self, context):
        objects = _filtered_objects_for_kind(context, "CLOTH")
        if not objects:
            self.report({"WARNING"}, "No ClothSimulation items match the current scope.")
            return {"CANCELLED"}
        count = 0
        for obj in objects:
            mod = find_clothsimulation_modifier(obj)
            if mod is None:
                continue
            mod.show_viewport = bool(self.show)
            mod.show_render = bool(self.show)
            count += 1
        if count <= 0:
            self.report({"WARNING"}, "No ClothSimulation modifiers were changed.")
            return {"CANCELLED"}
        self.report({"INFO"}, f"{'Showed' if self.show else 'Hid'} {count} ClothSimulation item(s) in scope.")
        return {"FINISHED"}


class WITCH_OT_DyngCreateWindObject(bpy.types.Operator):
    bl_idname = "witcher.dyng_create_wind_object"
    bl_label = "Create In-Scene Wind Force"
    bl_description = "Create or reuse a Blender Wind force field used by the Dyng solver"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        obj = dyng_blender.wind_object_from_scene(scene)
        if obj is None:
            for candidate in getattr(scene, "objects", []) or []:
                if dyng_blender.is_wind_field_object(candidate) and str(getattr(candidate, "name", "")).startswith("Witcher Dyng Wind"):
                    obj = candidate
                    setattr(scene, dyng_blender.SCENE_WIND_OBJECT_ATTR, obj)
                    break
        if obj is None:
            cursor = getattr(scene, "cursor", None)
            location = getattr(cursor, "location", (0.0, 0.0, 0.0))
            if not bpy.ops.object.effector_add.poll():
                self.report({"WARNING"}, "Cannot create a Wind force field in this context.")
                return {"CANCELLED"}
            bpy.ops.object.effector_add(type="WIND", location=location)
            obj = getattr(context, "object", None) or getattr(bpy.context, "object", None)
            if obj is None or not dyng_blender.is_wind_field_object(obj):
                self.report({"WARNING"}, "Wind force field creation failed.")
                return {"CANCELLED"}
            obj.name = "Witcher Dyng Wind"
            obj.empty_display_size = 1.0
            field = getattr(obj, "field", None)
            if field is not None:
                field.strength = float(getattr(scene, dyng_blender.SCENE_WIND_SPEED_ATTR, 1.0) or 1.0)
            dyng_blender.set_wind_object_direction(
                obj,
                getattr(scene, dyng_blender.SCENE_WIND_DIRECTION_ATTR, (1.0, 0.0, 0.0)),
            )
            context.view_layer.update()
            setattr(scene, dyng_blender.SCENE_WIND_OBJECT_ATTR, obj)
        setattr(scene, dyng_blender.SCENE_WIND_ENABLED_ATTR, True)
        _select_object(context, obj, ensure_object_mode=False)
        self.report({"INFO"}, f"Using {obj.name} for Dyng wind.")
        return {"FINISHED"}


class WITCH_OT_DyngSelectWindObject(bpy.types.Operator):
    bl_idname = "witcher.dyng_select_wind_object"
    bl_label = "Select Dyng Wind Force"
    bl_description = "Select the Blender Wind force field used by the Dyng solver"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        obj = dyng_blender.wind_object_from_scene(context.scene)
        if obj is None:
            self.report({"WARNING"}, "No Dyng wind object is assigned.")
            return {"CANCELLED"}
        _select_object(context, obj)
        return {"FINISHED"}


def _ensure_idprop_description(obj, key):
    description = _IDPROP_DESCRIPTIONS.get(key)
    if not description or obj is None or key not in obj:
        return
    try:
        prop_ui = obj.id_properties_ui(key)
        current = prop_ui.as_dict().get("description", "")
        if current != description:
            prop_ui.update(description=description)
    except Exception:
        pass


def _draw_idprop(layout, obj, key, text):
    if key in obj:
        _ensure_idprop_description(obj, key)
        layout.prop(obj, f'["{key}"]', text=text)
        return True
    return False


def _breast_ellipse_preview_status(context, obj):
    scene = getattr(context, "scene", None)
    enabled = bool(getattr(scene, _BREAST_ELLIPSE_PREVIEW_ATTR, False)) if scene is not None else False
    if obj is None or breast_blender.BREAST_ELLIPSE_PROP not in obj:
        return enabled, False, "No elA data", "", "INFO"
    guides = breast_blender.ellipse_preview_guides(
        obj,
        segments=12,
        display_offset=_breast_ellipse_preview_offset(context),
    )
    if not guides:
        return enabled, False, "No preview geometry", "", "INFO"
    names = " / ".join(sorted(guides))
    distances = []
    for name in breast_blender.BREAST_BONE_NAMES:
        data = guides.get(name)
        if not data:
            continue
        distance = data.get("distance")
        try:
            short_name = "L" if name.startswith("l_") else "R" if name.startswith("r_") else name
            distances.append(f"{short_name} {float(distance):.3f}m")
        except (TypeError, ValueError):
            pass
    distance_text = f"Distance: {' / '.join(distances)}" if distances else ""
    return enabled, True, f"Viewport: {names}", distance_text, "HIDE_OFF"


def _draw_runtime_state(layout, kind: str, obj):
    if kind == "DYNG":
        enabled = dyng_blender.is_dyng_runtime_enabled(obj)
        legacy_enabled = bool(obj.get(dyng_blender.DYNG_ENABLED_PROP, False))
        opt_in = bool(obj.get(dyng_blender.DYNG_RUNTIME_OPT_IN_PROP, False))
    else:
        enabled = breast_blender.is_breast_runtime_enabled(obj)
        legacy_enabled = bool(obj.get(breast_blender.BREAST_ENABLED_PROP, False))
        opt_in = bool(obj.get(breast_blender.BREAST_RUNTIME_OPT_IN_PROP, False))
    layout.label(text="Preview: Running" if enabled else "Preview: Paused", icon="PLAY" if enabled else "PAUSE")
    if not dyng_blender.live_preview_enabled(getattr(bpy.context, "scene", None)):
        layout.label(text="Live Preview is off", icon="INFO")
    if legacy_enabled and not opt_in:
        layout.label(text="Legacy flag ignored", icon="INFO")


class _PhysicsTargetListBase(bpy.types.UIList):
    kind = ""
    use_filter_show = False
    use_filter_sort_alpha = False

    def filter_items(self, context, data, propname):
        items = getattr(data, propname)
        filter_text = getattr(self, "filter_name", "")
        flags = [
            self.bitflag_filter_item
            if _object_visible_for_kind(context, item, self.kind) and _object_matches_list_filter(item, filter_text)
            else 0
            for item in items
        ]
        return flags, []

    def draw_filter(self, context, layout):
        row = layout.row(align=True)
        row.prop(self, "filter_name", text="Filter", icon="VIEWZOOM")

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index, flt_flag):
        if self.layout_type not in {"DEFAULT", "COMPACT"}:
            layout.alignment = "CENTER"
            layout.label(text="")
            return
        if not _is_physics_object_kind(item, self.kind):
            return
        row = layout.row(align=True)
        if self.kind == "DYNG":
            enabled = dyng_blender.is_dyng_runtime_enabled(item)
            data_icon = "FILE_TICK" if bool(item.get("witcher_dyng_data")) else "FILE_REFRESH"
            toggle_slot = row.row(align=True)
            toggle_slot.enabled = dyng_blender.live_preview_enabled(getattr(context, "scene", None)) or enabled
            toggle_op = toggle_slot.operator(
                WITCH_OT_DyngToggleRuntime.bl_idname,
                text="",
                icon="PAUSE" if enabled else "PLAY",
            )
            toggle_op.object_name = item.name
            toggle_op.enable = not enabled
        elif self.kind == "BREAST":
            enabled = breast_blender.is_breast_runtime_enabled(item)
            data_icon = "PHYSICS"
            toggle_slot = row.row(align=True)
            toggle_slot.enabled = dyng_blender.live_preview_enabled(getattr(context, "scene", None)) or enabled
            toggle_op = toggle_slot.operator(
                WITCH_OT_BreastToggleRuntime.bl_idname,
                text="",
                icon="PAUSE" if enabled else "PLAY",
            )
            toggle_op.object_name = item.name
            toggle_op.enable = not enabled
        else:
            mod = find_clothsimulation_modifier(item)
            enabled = bool(getattr(mod, "show_viewport", False)) if mod is not None else False
            data_icon = "MOD_CLOTH"
            toggle_slot = row.row(align=True)
            toggle_slot.enabled = mod is not None
            toggle_op = toggle_slot.operator(
                WITCH_OT_ClothToggleSimulationObject.bl_idname,
                text="",
                icon="HIDE_OFF" if enabled else "HIDE_ON",
            )
            toggle_op.object_name = item.name
            toggle_op.show = not enabled
        row.label(text="", icon=data_icon)
        row.label(text=f"{_scope_label_for_object(item)}: {_clean_physics_name(item)}")


class WITCH_UL_PhysicsDyngTargets(_PhysicsTargetListBase):
    bl_idname = "WITCH_UL_PhysicsDyngTargets"
    kind = "DYNG"


class WITCH_UL_PhysicsBreastTargets(_PhysicsTargetListBase):
    bl_idname = "WITCH_UL_PhysicsBreastTargets"
    kind = "BREAST"


class WITCH_UL_PhysicsClothTargets(_PhysicsTargetListBase):
    bl_idname = "WITCH_UL_PhysicsClothTargets"
    kind = "CLOTH"


_LIST_IDS = {
    "DYNG": WITCH_UL_PhysicsDyngTargets.bl_idname,
    "BREAST": WITCH_UL_PhysicsBreastTargets.bl_idname,
    "CLOTH": WITCH_UL_PhysicsClothTargets.bl_idname,
}


def _list_id_for_kind(kind: str) -> str:
    return _LIST_IDS.get(kind, WITCH_UL_PhysicsClothTargets.bl_idname)


def _draw_dyng_wind_section(layout, context):
    scene = getattr(context, "scene", None)
    if scene is None or not hasattr(scene, dyng_blender.SCENE_WIND_ENABLED_ATTR):
        return

    wind_col = _draw_detail_panel(layout, "witcher_physics_dyng_wind", "Wind", "FORCE_WIND")
    if not wind_col:
        return
    wind_object = dyng_blender.wind_object_from_scene(scene)
    wind_action_row = wind_col.row(align=True)
    wind_action_text = "Create Wind Object" if wind_object is None else "Use Wind Object"
    wind_action_row.operator(WITCH_OT_DyngCreateWindObject.bl_idname, text=wind_action_text, icon="FORCE_WIND")
    select_slot = wind_action_row.row(align=True)
    select_slot.enabled = wind_object is not None
    select_slot.operator(WITCH_OT_DyngSelectWindObject.bl_idname, text="Select", icon="RESTRICT_SELECT_OFF")

    if hasattr(scene, dyng_blender.SCENE_WIND_OBJECT_ATTR):
        wind_col.prop(scene, dyng_blender.SCENE_WIND_OBJECT_ATTR, text="Object")

    wind_col.prop(scene, dyng_blender.SCENE_WIND_ENABLED_ATTR, text="Use Wind")
    settings_col = wind_col.column(align=True)
    settings_col.enabled = bool(getattr(scene, dyng_blender.SCENE_WIND_ENABLED_ATTR, False))
    if wind_object is not None:
        field = getattr(wind_object, "field", None)
        if field is not None:
            settings_col.prop(field, "strength", text="Speed")
        direction = dyng_blender.wind_direction_from_object(wind_object)
        direction_row = settings_col.row(align=True)
        direction_row.enabled = False
        direction_row.label(text=f"Direction  X {direction[0]:.2f}  Y {direction[1]:.2f}  Z {direction[2]:.2f}")
    else:
        wind = settings_col.split(factor=0.26, align=True)
        control_col = wind.column(align=True)
        direction_control = control_col.row(align=True)
        direction_control.scale_y = 1.8
        direction_control.prop(scene, dyng_blender.SCENE_WIND_DIRECTION_ATTR, text="")
        values_col = wind.column(align=True)
        values_col.prop(scene, dyng_blender.SCENE_WIND_SPEED_ATTR, text="Speed")
        values_col.prop(scene, dyng_blender.SCENE_WIND_DIRECTION_ATTR, index=0, text="X")
        values_col.prop(scene, dyng_blender.SCENE_WIND_DIRECTION_ATTR, index=1, text="Y")
        values_col.prop(scene, dyng_blender.SCENE_WIND_DIRECTION_ATTR, index=2, text="Z")


def _draw_select_active_menu_item(layout, kind: str, obj):
    row = layout.row()
    row.enabled = obj is not None
    if kind == "DYNG":
        op = row.operator(WITCH_OT_DyngSelect.bl_idname, text="Select Active", icon="RESTRICT_SELECT_OFF")
    elif kind == "BREAST":
        op = row.operator(WITCH_OT_BreastSelect.bl_idname, text="Select Active", icon="RESTRICT_SELECT_OFF")
    else:
        op = row.operator(WITCH_OT_ClothSelect.bl_idname, text="Select Active", icon="RESTRICT_SELECT_OFF")
    op.object_name = obj.name if obj is not None else ""


class WITCH_MT_PhysicsListActions(bpy.types.Menu):
    bl_idname = "WITCH_MT_physics_list_actions"
    bl_label = "Physics List Actions"

    def draw(self, context):
        layout = self.layout
        scene = getattr(context, "scene", None)
        if scene is None:
            layout.label(text="No scene context", icon="INFO")
            return

        kind = str(getattr(scene, _PHYSICS_TAB_ATTR, "DYNG") or "DYNG")
        if kind not in _KIND_LABELS:
            kind = "CLOTH"
        objects = _objects_for_kind(context, kind)
        visible = _filtered_objects_for_kind(context, kind, objects)
        obj = _active_list_object(context, kind, objects)

        _draw_select_active_menu_item(layout, kind, obj)

        if kind == "DYNG":
            if obj is not None and not bool(obj.get("witcher_dyng_data")):
                load_op = layout.operator(WITCH_OT_DyngLoadData.bl_idname, text="Load Dyng Resource", icon="FILE_REFRESH")
                load_op.object_name = obj.name
            layout.separator()
            self._draw_preview_scope_actions(layout, scene, WITCH_OT_DyngToggleAllRuntime.bl_idname, visible)
        elif kind == "BREAST":
            layout.separator()
            self._draw_preview_scope_actions(layout, scene, WITCH_OT_BreastToggleAllRuntime.bl_idname, visible)
        else:
            layout.separator()
            show_slot = layout.row()
            show_slot.enabled = bool(visible)
            show_slot.operator(WITCH_OT_ClothToggleScope.bl_idname, text="Show All", icon="HIDE_OFF").show = True
            hide_slot = layout.row()
            hide_slot.enabled = bool(visible)
            hide_slot.operator(WITCH_OT_ClothToggleScope.bl_idname, text="Hide All", icon="HIDE_ON").show = False

    @staticmethod
    def _draw_preview_scope_actions(layout, scene, operator_id: str, visible):
        live_enabled = dyng_blender.live_preview_enabled(scene)
        start_slot = layout.row()
        start_slot.enabled = live_enabled and bool(visible)
        start_slot.operator(operator_id, text="Start All", icon="PLAY").enable = True

        pause_slot = layout.row()
        pause_slot.enabled = bool(visible)
        pause_slot.operator(operator_id, text="Pause All", icon="PAUSE").enable = False


def _draw_list_side_buttons(column, context, kind: str, obj, visible):
    column.menu(WITCH_MT_PhysicsListActions.bl_idname, text="", icon="DOWNARROW_HLT")


def _draw_physics_list(layout, context, kind: str, objects):
    col = _draw_detail_panel(
        layout,
        f"witcher_physics_{kind.lower()}_items",
        f"{_kind_label(kind)} Items",
        _kind_icon(kind),
    )
    if not col:
        return _active_list_object(context, kind, objects)
    scene = getattr(context, "scene", None)
    if scene is None:
        col.label(text="No scene context.", icon="INFO")
        return None

    visible = _filtered_objects_for_kind(context, kind, objects)
    obj = _active_list_object(context, kind, objects)
    scope_row = col.row(align=True)
    scope_row.prop(scene, _PHYSICS_SCOPE_ATTR, expand=True)
    if kind in {"DYNG", "BREAST"} and hasattr(scene, dyng_blender.SCENE_LIVE_PREVIEW_ATTR):
        scope_row.prop(scene, dyng_blender.SCENE_LIVE_PREVIEW_ATTR, text="Live Preview")
    if _scope_mode(context) == "SELECTED":
        root = dyng_blender.find_character_root(context)
        info = col.row()
        info.enabled = False
        info.label(
            text=_scope_label_for_root(root) if root is not None else "Selected: none",
            icon="ARMATURE_DATA" if root is not None else "INFO",
        )
    if not visible:
        col.label(text="No matching items.", icon="INFO")
    list_row = col.row(align=True)
    list_row.template_list(
        _list_id_for_kind(kind),
        "",
        bpy.data,
        "objects",
        scene,
        _kind_index_attr(kind),
        rows=6,
    )
    buttons = list_row.column(align=True)
    _draw_list_side_buttons(buttons, context, kind, obj, visible)
    return obj


def _draw_detail_panel(container, section_id: str, label: str, icon: str, *, default_closed=False):
    box = container.box()
    header, body = box.panel(section_id, default_closed=default_closed)
    row = header.row(align=True)
    row.label(text=label, icon=icon)
    return body


def _preset_display_name(name, fallback="Custom Values"):
    name = str(name or "").strip()
    if not name:
        return fallback
    return name.replace("_", " ")


def _current_dyng_preset(obj):
    return str(obj.get(dyng_blender.DYNG_PRESET_PROP, "") or "").strip()


def _current_breast_preset(obj):
    return str(obj.get(breast_blender.BREAST_PRESET_PROP, breast_blender.CUSTOM_PRESET_NAME) or breast_blender.CUSTOM_PRESET_NAME).strip()


def _draw_dyng_preset_controls(layout, obj):
    current_preset = _current_dyng_preset(obj)
    preset_label = _preset_display_name(current_preset)
    saved_names = set(dyng_blender.saved_user_preset_names())
    can_delete = current_preset in saved_names

    row = layout.row(align=True)
    load_slot = row.row(align=True)
    load_slot.enabled = bool(dyng_blender.user_preset_names())
    load_op = load_slot.operator_menu_enum(
        WITCH_OT_DyngApplyUserPreset.bl_idname,
        "preset",
        text=_short_label(preset_label, 28),
        icon="PRESET",
    )
    load_op.object_name = obj.name
    save_op = row.operator(WITCH_OT_DyngSaveUserPreset.bl_idname, text="", icon="FILE_TICK")
    save_op.object_name = obj.name
    delete_slot = row.row(align=True)
    delete_slot.enabled = can_delete
    delete_op = delete_slot.operator(WITCH_OT_DyngDeleteUserPreset.bl_idname, text="", icon="X")
    if can_delete:
        delete_op.preset = current_preset
    elif not saved_names:
        delete_op.preset = "__NONE__"


def _draw_breast_preset_controls(layout, obj):
    current_preset = _current_breast_preset(obj)
    preset_label = _preset_display_name(current_preset)
    saved_names = set(breast_blender.saved_user_preset_names())
    can_delete = current_preset in saved_names

    row = layout.row(align=True)
    row.menu(WITCH_MT_BreastPresetMenu.bl_idname, text=_short_label(preset_label, 28), icon="PRESET")
    save_op = row.operator(WITCH_OT_BreastSaveUserPreset.bl_idname, text="", icon="FILE_TICK")
    save_op.object_name = obj.name
    delete_slot = row.row(align=True)
    delete_slot.enabled = can_delete
    delete_op = delete_slot.operator(WITCH_OT_BreastDeleteUserPreset.bl_idname, text="", icon="X")
    if can_delete:
        delete_op.preset = current_preset
    elif not saved_names:
        delete_op.preset = "__NONE__"


def _draw_dyng_bake_controls(layout, context, obj):
    bake = _draw_detail_panel(
        layout,
        "witcher_physics_dyng_bake",
        "Cache & Bake",
        "ACTION",
        default_closed=False,
    )
    if not bake:
        return

    scene = context.scene
    target_row = bake.row()
    target_row.prop(obj, "name", text="Target")

    range_row = bake.row(align=True)
    range_row.prop(scene, "frame_start", text="Start")
    range_row.prop(scene, "frame_end", text="End")
    bake.label(text="Bake starts from a reset pose", icon="INFO")

    cache_row = bake.row(align=True)
    cache_op = cache_row.operator(WITCH_OT_DyngCache.bl_idname, text="Cache Range", icon="FILE_REFRESH")
    cache_op.object_name = obj.name
    cache_row.label(text=_short_label(dyng_blender.cache_summary(obj), 28), icon="TIME")

    info = dyng_blender.bake_info(obj)
    bake.separator(factor=0.4)
    if info.managed:
        if info.restore_missing:
            bake.label(text="Status: Source Action/slot missing", icon="ERROR")
            bake.label(text="Delete and re-bake are blocked", icon="INFO")
        elif not info.verified:
            bake.label(text="Status: Owned bake; Dyng data unavailable", icon="ERROR")
        elif not info.valid:
            bake.label(text="Status: Bake modified or incomplete", icon="ERROR")
        elif info.overridden:
            bake.label(text="Status: Live preview overrides bake", icon="ERROR")
        elif info.active:
            bake.label(text="Status: Baked and active", icon="CHECKMARK")
        elif info.nla:
            bake.label(text="Status: Baked in NLA", icon="CHECKMARK")
        else:
            bake.label(text="Status: Baked, not active", icon="ERROR")
        bake.prop(info.action, "name", text="Action")
        if info.frame_start is not None and info.frame_end is not None:
            bake.label(text=f"Baked Range: {info.frame_start} to {info.frame_end}", icon="PREVIEW_RANGE")
            frame_count = info.frame_end - info.frame_start + 1
            bake.label(text=f"Frames: {frame_count}    Bones: {info.bone_count}", icon="BONE_DATA")
        bake.label(text=f"Dyng Curve Keys: {info.key_count}", icon="KEY_HLT")
    elif info.legacy:
        bake.label(text="Status: Possible unmanaged legacy bake", icon="ERROR")
        bake.prop(info.action, "name", text="Action")
        if info.frame_start is not None and info.frame_end is not None:
            bake.label(text=f"Detected Range: {info.frame_start} to {info.frame_end}", icon="PREVIEW_RANGE")
        bake.label(text="Full-frame curve signature detected.", icon="INFO")
    elif info.missing:
        bake.label(text="Status: Managed bake Action is missing", icon="ERROR")
        if info.restore_missing:
            bake.label(text="Source Action/slot is also missing", icon="ERROR")
        else:
            bake.label(text="Rebake to replace stale tracking", icon="INFO")
    else:
        status = str(obj.get(dyng_blender.DYNG_BAKE_STATUS_PROP, "Not baked") or "Not baked")
        bake.label(text=f"Last Operation: {_short_label(status, 32)}", icon="INFO")

    action_row = bake.row(align=True)
    bake_slot = action_row.row(align=True)
    bake_slot.enabled = not info.restore_missing
    bake_op = bake_slot.operator(WITCH_OT_DyngBake.bl_idname, text="Bake Range", icon="KEY_HLT")
    bake_op.object_name = obj.name
    if info.legacy:
        legacy_slot = action_row.row(align=True)
        legacy_slot.enabled = int(getattr(info.action, "users", 0) or 0) == 1
        legacy_op = legacy_slot.operator(
            WITCH_OT_DyngDeleteLegacyBake.bl_idname,
            text="Delete Possible Legacy...",
            icon="TRASH",
        )
        legacy_op.object_name = obj.name
    else:
        delete_slot = action_row.row(align=True)
        delete_slot.enabled = info.managed and not info.restore_missing
        delete_op = delete_slot.operator(WITCH_OT_DyngDeleteBake.bl_idname, text="Delete Bake...", icon="TRASH")
        delete_op.object_name = obj.name


def _draw_dyng_details(layout, context, obj):
    resource = _draw_detail_panel(layout, "witcher_physics_dyng_resource", "Resource Data", "PROPERTIES", default_closed=False)
    if resource:
        resource.label(text="CAnimDangleConstraint_Dyng", icon="INFO")
        _draw_idprop(resource, obj, "witcher_name", "Name")
        if obj.get("witcher_path"):
            _draw_idprop(resource, obj, "witcher_path", "Path")
        for key, label in (
            (dyng_blender.DYNG_NODE_COUNT_PROP, "Nodes"),
            (dyng_blender.DYNG_LINK_COUNT_PROP, "Links"),
            (dyng_blender.DYNG_TRIANGLE_COUNT_PROP, "Triangles"),
            (dyng_blender.DYNG_COLLISION_COUNT_PROP, "Collisions"),
        ):
            _draw_idprop(resource, obj, key, label)
        status = str(obj.get(dyng_blender.DYNG_SIM_STATUS_PROP, obj.get(DYNG_PARSE_STATUS_PROP, "")) or "")
        if status:
            resource.label(text=_short_label(status, 48), icon="INFO")
        summary = dyng_blender.summarize_object(obj)
        if summary:
            resource.label(text=_short_label(summary, 48), icon="INFO")

    runtime = _draw_detail_panel(layout, "witcher_physics_dyng_runtime", "Runtime Values", "PHYSICS", default_closed=False)
    if runtime:
        _draw_runtime_state(runtime, "DYNG", obj)
        _draw_dyng_preset_controls(runtime, obj)
        runtime_keys = (
            (dyng_blender.DYNG_GRAVITY_PROP, "Gravity"),
            (dyng_blender.DYNG_DAMPENING_PROP, "Damping"),
            (dyng_blender.DYNG_SPEED_PROP, "Speed"),
            (dyng_blender.DYNG_LINK_ITERATIONS_PROP, "Link Iterations"),
            (dyng_blender.DYNG_BLEND_PROP, "Blend"),
            (dyng_blender.DYNG_USE_OFFSETS_PROP, "Use Authored Offsets"),
            (dyng_blender.DYNG_PLANE_COLLISION_PROP, "Local Plane Limit"),
            (dyng_blender.DYNG_SHAKE_PROP, "Shake"),
            (dyng_blender.DYNG_WIND_PROP, "Wind Influence"),
            (dyng_blender.DYNG_ACCESSORY_PREVIEW_PROP, "Accessory Preview"),
        )
        if all(key in obj for key, _label in runtime_keys):
            runtime.separator(factor=0.4)
            for key, label in runtime_keys:
                _draw_idprop(runtime, obj, key, label)
            if (
                dyng_blender.has_authored_offsets(obj)
                and not bool(obj.get(dyng_blender.DYNG_USE_OFFSETS_PROP, False))
            ):
                runtime.label(text="Offsets available; enable for containment", icon="INFO")
        else:
            runtime.label(text="Load Dyng data first.", icon="INFO")
        runtime.separator(factor=0.4)
        row = runtime.row(align=True)
        step_op = row.operator(WITCH_OT_DyngStep.bl_idname, text="Step", icon="TIME")
        step_op.object_name = obj.name
        reset_op = row.operator(WITCH_OT_DyngReset.bl_idname, text="Reset", icon="LOOP_BACK")
        reset_op.object_name = obj.name

    _draw_dyng_bake_controls(layout, context, obj)


def _draw_breast_details(layout, context, obj):
    imported = _draw_detail_panel(layout, "witcher_physics_breast_imported", "Resource Data", "PROPERTIES", default_closed=False)
    if imported:
        imported.label(text="CAnimDangleConstraint_Breast", icon="INFO")
        _draw_idprop(imported, obj, "witcher_name", "Name")
        _draw_breast_preset_controls(imported, obj)
        if hasattr(context.scene, _BREAST_ELLIPSE_PREVIEW_ATTR):
            preview_enabled, can_preview, preview_text, distance_text, preview_icon = _breast_ellipse_preview_status(context, obj)
            preview_row = imported.row()
            preview_row.enabled = can_preview or preview_enabled
            preview_row.prop(context.scene, _BREAST_ELLIPSE_PREVIEW_ATTR, text="Show Ellipse")
            if preview_enabled or not can_preview:
                imported.label(text=_short_label(preview_text, 48), icon=preview_icon)
            if preview_enabled and hasattr(context.scene, _BREAST_ELLIPSE_PREVIEW_OFFSET_ATTR):
                offset_row = imported.row()
                offset_row.prop(context.scene, _BREAST_ELLIPSE_PREVIEW_OFFSET_ATTR, text="Preview Offset")
                if distance_text:
                    imported.label(text=_short_label(distance_text, 48), icon="INFO")
        breast_fields = (
            (breast_blender.BREAST_SIM_TIME_PROP, "Sim Time"),
            (breast_blender.BREAST_ELLIPSE_PROP, "Ellipse"),
            (breast_blender.BREAST_VEL_DAMP_PROP, "Velocity Damping"),
            (breast_blender.BREAST_BOUNCE_DAMP_PROP, "Bounce Damping"),
            (breast_blender.BREAST_IN_ACC_PROP, "Input Accel"),
            (breast_blender.BREAST_INERTIA_SCALER_PROP, "Inertia Scale"),
            (breast_blender.BREAST_BLACK_HOLE_PROP, "Black Hole"),
            (breast_blender.BREAST_VEL_CLAMP_PROP, "Velocity Clamp"),
            (breast_blender.BREAST_GRAVITY_PROP, "Gravity"),
            (breast_blender.BREAST_MOVEMENT_WEIGHT_PROP, "Movement Weight"),
            (breast_blender.BREAST_ROTATION_WEIGHT_PROP, "Rotation Weight"),
            (breast_blender.BREAST_START_OFFSET_PROP, "Start Offset"),
        )
        if all(key in obj for key, _label in breast_fields):
            for key, label in breast_fields:
                _draw_idprop(imported, obj, key, label)
        else:
            imported.label(text="Run or step Breast first.", icon="INFO")

    runtime = _draw_detail_panel(layout, "witcher_physics_breast_runtime", "Runtime Values", "PHYSICS", default_closed=False)
    if runtime:
        _draw_runtime_state(runtime, "BREAST", obj)
        runtime.label(text=_short_label(breast_blender.summarize_object(obj), 48), icon="INFO")
        _draw_idprop(runtime, obj, breast_blender.BREAST_BLEND_PROP, "Blend")
        _draw_idprop(runtime, obj, breast_blender.BREAST_LAST_STEP_PROP, "Last Step")
        status = str(obj.get(breast_blender.BREAST_SIM_STATUS_PROP, "") or "")
        if status:
            runtime.label(text=_short_label(status, 48), icon="INFO")
        runtime.separator(factor=0.4)
        row = runtime.row(align=True)
        step_op = row.operator(WITCH_OT_BreastStep.bl_idname, text="Step", icon="TIME")
        step_op.object_name = obj.name
        reset_op = row.operator(WITCH_OT_BreastReset.bl_idname, text="Reset", icon="LOOP_BACK")
        reset_op.object_name = obj.name
        bake_op = runtime.operator(WITCH_OT_BreastBake.bl_idname, text="Bake Range", icon="KEY_HLT")
        bake_op.object_name = obj.name


def _draw_cloth_details(layout, obj):
    return


def _draw_physics_tab(layout, context, kind: str, objects, details_fn, fallback_obj=None):
    obj = _draw_physics_list(layout, context, kind, objects)
    if kind == "DYNG":
        _draw_dyng_wind_section(layout, context)
    if obj is None and fallback_obj is not None and _object_visible_for_kind(context, fallback_obj, kind):
        obj = fallback_obj
    if obj is not None:
        details_fn(layout, context, obj)


class WITCH_PT_Physics(WITCH_PT_Base, bpy.types.Panel):
    bl_idname = "WITCH_PT_Physics"
    bl_label = "Physics"

    def draw_header(self, context):
        self.layout.label(text="", icon="PHYSICS")

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = False
        layout.use_property_decorate = False
        scene = getattr(context, "scene", None)
        if scene is None:
            layout.label(text="No scene context.", icon="INFO")
            return
        dyng_objects = _panel_dyng_objects(context)
        breast_objects = _panel_breast_objects(context)
        cloth_objects = _panel_cloth_objects(context)
        dyng_obj = _target_from_scene(context, "DYNG", dyng_objects)
        breast_obj = _target_from_scene(context, "BREAST", breast_objects)

        tab_row = layout.row(align=True)
        tab_row.scale_y = 1.6
        tab_row.prop_enum(scene, _PHYSICS_TAB_ATTR, "DYNG")
        tab_row.prop_enum(scene, _PHYSICS_TAB_ATTR, "BREAST")
        tab_row.prop_enum(scene, _PHYSICS_TAB_ATTR, "CLOTH")
        layout.separator(factor=0.3)

        tab = str(getattr(scene, _PHYSICS_TAB_ATTR, "DYNG") or "DYNG")
        if tab == "DYNG":
            _draw_physics_tab(layout, context, "DYNG", dyng_objects, _draw_dyng_details, dyng_obj)
        elif tab == "BREAST":
            _draw_physics_tab(layout, context, "BREAST", breast_objects, _draw_breast_details, breast_obj)
        else:
            _draw_physics_tab(
                layout,
                context,
                "CLOTH",
                cloth_objects,
                lambda layout, _context, obj: _draw_cloth_details(layout, obj),
            )


classes = [
    WITCH_OT_PhysicsSetTarget,
    WITCH_OT_DyngLoadData,
    WITCH_OT_DyngToggleRuntime,
    WITCH_OT_DyngToggleAllRuntime,
    WITCH_OT_DyngSelect,
    WITCH_OT_DyngStep,
    WITCH_OT_DyngReset,
    WITCH_OT_DyngBake,
    WITCH_OT_DyngDeleteBake,
    WITCH_OT_DyngDeleteLegacyBake,
    WITCH_OT_DyngCache,
    WITCH_OT_DyngApplyUserPreset,
    WITCH_OT_DyngSaveUserPreset,
    WITCH_OT_DyngDeleteUserPreset,
    WITCH_OT_BreastToggleRuntime,
    WITCH_OT_BreastToggleAllRuntime,
    WITCH_OT_BreastSelect,
    WITCH_OT_BreastApplyPreset,
    WITCH_MT_BreastPresetMenu,
    WITCH_OT_BreastSaveUserPreset,
    WITCH_OT_BreastDeleteUserPreset,
    WITCH_OT_BreastStep,
    WITCH_OT_BreastReset,
    WITCH_OT_BreastBake,
    WITCH_OT_ClothSelect,
    WITCH_OT_ClothToggleSimulationObject,
    WITCH_OT_ClothToggleScope,
    WITCH_OT_DyngCreateWindObject,
    WITCH_OT_DyngSelectWindObject,
    WITCH_MT_PhysicsListActions,
    WITCH_UL_PhysicsDyngTargets,
    WITCH_UL_PhysicsBreastTargets,
    WITCH_UL_PhysicsClothTargets,
    WITCH_PT_Physics,
]


def _register_scene_property(attr: str, prop_factory, *, replace=False) -> None:
    if replace and hasattr(bpy.types.Scene, attr):
        delattr(bpy.types.Scene, attr)
    if not hasattr(bpy.types.Scene, attr):
        setattr(bpy.types.Scene, attr, prop_factory())


def register():
    _register_scene_property(
        _PHYSICS_TAB_ATTR,
        lambda: EnumProperty(
            name="Physics Tab",
            description="Active physics panel section",
            items=_PHYSICS_TAB_ITEMS,
            default="DYNG",
        ),
        replace=True,
    )
    _register_scene_property(
        _PHYSICS_SCOPE_ATTR,
        lambda: EnumProperty(
            name="Scope",
            description="Whether Physics lists show every scene item or only the selected character",
            items=_PHYSICS_SCOPE_ITEMS,
            default="GLOBAL",
        ),
    )
    for attr, label in (
        (_PHYSICS_DYNG_INDEX_ATTR, "Dyng List Index"),
        (_PHYSICS_BREAST_INDEX_ATTR, "Breast List Index"),
        (_PHYSICS_CLOTH_INDEX_ATTR, "Cloth List Index"),
    ):
        _register_scene_property(
            attr,
            lambda label=label: IntProperty(
                name=label,
                description="Active row in the Physics panel list",
                default=0,
                options={"SKIP_SAVE"},
            ),
        )
    _register_scene_property(
        _PHYSICS_DYNG_TARGET_ATTR,
        lambda: StringProperty(
            name="Dyng Target",
            description="Dyng armature targeted by the Physics panel",
            default="",
        ),
    )
    _register_scene_property(
        _PHYSICS_BREAST_TARGET_ATTR,
        lambda: StringProperty(
            name="Breast Target",
            description="Breast physics armature targeted by the Physics panel",
            default="",
        ),
    )
    _register_scene_property(
        dyng_blender.SCENE_WIND_ENABLED_ATTR,
        lambda: BoolProperty(
            name="Dyng Wind",
            description="Apply shared wind to enabled Dyng armatures",
            default=False,
        ),
    )
    _register_scene_property(
        dyng_blender.SCENE_LIVE_PREVIEW_ATTR,
        lambda: BoolProperty(
            name="Physics Live Preview",
            description="Allow enabled physics armatures to update on frame changes",
            default=True,
            update=_on_physics_live_preview_changed,
        ),
        replace=True,
    )
    _register_scene_property(
        _BREAST_ELLIPSE_PREVIEW_ATTR,
        lambda: BoolProperty(
            name="Breast Ellipse Preview",
            description="Draw colored viewport guide ellipses for the active Breast object's l_boob and r_boob elA values",
            default=False,
            update=_on_breast_ellipse_preview_changed,
        ),
    )
    _register_scene_property(
        _BREAST_ELLIPSE_PREVIEW_OFFSET_ATTR,
        lambda: FloatProperty(
            name="Breast Ellipse Offset",
            description="Viewport-only distance from the Breast bones to the elA guide plane",
            default=0.08,
            soft_min=-0.25,
            soft_max=0.5,
            subtype="DISTANCE",
            unit="LENGTH",
            options={"SKIP_SAVE"},
            update=_on_breast_ellipse_preview_changed,
        ),
    )
    _register_scene_property(
        dyng_blender.SCENE_WIND_OBJECT_ATTR,
        lambda: PointerProperty(
            name="Wind Object",
            description="Optional Blender Wind force field used by the Dyng solver",
            type=bpy.types.Object,
            poll=_dyng_wind_object_poll,
        ),
    )
    _register_scene_property(
        dyng_blender.SCENE_WIND_DIRECTION_ATTR,
        lambda: FloatVectorProperty(
            name="Wind Direction",
            description="Direction used by the Blender-side Dyng solver",
            size=3,
            subtype="DIRECTION",
            default=(1.0, 0.0, 0.0),
            min=-1.0,
            max=1.0,
            soft_min=-1.0,
            soft_max=1.0,
        ),
    )
    _register_scene_property(
        dyng_blender.SCENE_WIND_SPEED_ATTR,
        lambda: FloatProperty(
            name="Wind Speed",
            description="Strength of the shared Blender-side Dyng wind",
            default=0.0,
            min=0.0,
            soft_max=5.0,
        ),
    )
    for cls in classes:
        bpy.utils.register_class(cls)
    scene = getattr(bpy.context, "scene", None)
    dyng_blender.remove_frame_handler()
    breast_blender.remove_frame_handler()
    try:
        from .. import get_import_physics_enabled
        restore_import_runtime = get_import_physics_enabled(bpy.context)
    except Exception:
        restore_import_runtime = True
    if scene is not None and restore_import_runtime:
        dyng_blender.restore_import_default_runtime(scene)
        breast_blender.restore_import_default_runtime(scene)
    if scene is not None and dyng_blender.enabled_dyng_objects(scene):
        dyng_blender.ensure_frame_handler()
    if scene is not None and breast_blender.enabled_breast_objects(scene):
        breast_blender.ensure_frame_handler()
    _sync_breast_ellipse_preview_handler(scene)


def unregister():
    _remove_breast_ellipse_preview_handler()
    dyng_blender.remove_frame_handler()
    breast_blender.remove_frame_handler()
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    for attr in (
        dyng_blender.SCENE_LIVE_PREVIEW_ATTR,
        dyng_blender.SCENE_WIND_SPEED_ATTR,
        dyng_blender.SCENE_WIND_DIRECTION_ATTR,
        dyng_blender.SCENE_WIND_OBJECT_ATTR,
        dyng_blender.SCENE_WIND_ENABLED_ATTR,
        _BREAST_ELLIPSE_PREVIEW_OFFSET_ATTR,
        _BREAST_ELLIPSE_PREVIEW_ATTR,
        _PHYSICS_BREAST_TARGET_ATTR,
        _PHYSICS_DYNG_TARGET_ATTR,
        _PHYSICS_CLOTH_INDEX_ATTR,
        _PHYSICS_BREAST_INDEX_ATTR,
        _PHYSICS_DYNG_INDEX_ATTR,
        _LEGACY_PHYSICS_FILTER_ATTR,
        _PHYSICS_SCOPE_ATTR,
        _PHYSICS_TAB_ATTR,
    ):
        if hasattr(bpy.types.Scene, attr):
            delattr(bpy.types.Scene, attr)
