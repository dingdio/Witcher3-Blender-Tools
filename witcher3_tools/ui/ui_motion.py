"""Trajectory generation and motion extraction preview UI."""
import json
import textwrap

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, PointerProperty, StringProperty

from ..animation import motion_extraction as mx
from ..animation import trajectory_tools as tt

REPORT_PROP = "w3_motion_report"
PATH_OBJECT = "W3_MotionPath"

HELP = (
    "Motion extraction is the curve that moves the character in game.",
    "The engine builds it from the Trajectory bone: how far it travels and",
    "turns from its first frame. The body keeps its own travel in the clip;",
    "in game the body is played relative to Trajectory while the entity",
    "follows the curve. So Trajectory must follow the body along the ground.",
    "Automatic axes omit travel under 1 cm and turns under 19 deg.",
    "Lower tolerance keeps more keys; the preview reports the measured error.",
    "Vanilla walk loops are a straight line: 2 keys.",
)


def settings(context):
    return context.scene.witcher_motion


class WitcherMotionSettings(bpy.types.PropertyGroup):
    source_bone: StringProperty(name="Follow Bone", default=tt.DEFAULT_SOURCE,
                                description="Bone whose ground path the Trajectory follows (the hips)")
    path: EnumProperty(name="Path", default='LINEAR', items=[
        ('LINEAR', "Straight", "Straight line from the first to the last frame at constant speed - walk and run "
                               "loops, as vanilla cycles are authored"),
        ('SMOOTH', "Smoothed", "Follow the hips' ground path with the side-to-side sway averaged out - curved paths"),
        ('EXACT', "Exact", "Follow the hips' ground path frame by frame, sway included"),
    ])
    rotation: EnumProperty(name="Turn", default='KEEP', items=[
        ('KEEP', "Keep Existing", "Preserve authored Trajectory rotation keys"),
        ('NONE', "None", "Set zero yaw when Trajectory has no rotation keys"),
        ('LINEAR', "Estimate Constant", "Estimate a constant turn from hips without authored Trajectory rotation; hip lean can bias it"),
        ('SMOOTH', "Estimate Smoothed", "Estimate turning from hips without authored Trajectory rotation; hip lean can bias it"),
    ])
    smooth: IntProperty(name="Smoothing", default=6, min=1, max=60,
                        description="Frames averaged on each side for Smoothed path and Follow Hips turn")
    height: BoolProperty(name="Keep Height", default=False,
                         description="Let Trajectory follow the hips up and down (stairs, jumps). Off keeps it on the "
                                     "ground, which is what locomotion uses")
    eps: FloatProperty(name="Tolerance", default=mx.DEFAULT_EPS, min=0.0001, max=1.0, precision=3,
                       description="Lower values retain more keys; CDPR uses 0.03. Motion Details shows the measured error")
    axes: EnumProperty(name="Axes", default='AUTO', items=[
        ('AUTO', "Automatic", "Store the axes that move more than 1 cm or turn more than 19 deg, as the engine does"),
        ('CUSTOM', "Custom", "Choose the stored axes yourself"),
    ])
    use_x: BoolProperty(name="X", default=False, description="Sideways travel")
    use_y: BoolProperty(name="Y", default=True, description="Forward travel")
    use_z: BoolProperty(name="Z", default=False, description="Vertical travel")
    use_yaw: BoolProperty(name="Yaw", default=False, description="Turning")


def flags_from_settings(s):
    if s.axes == 'AUTO':
        return None
    return (mx.FLAG_X * s.use_x) | (mx.FLAG_Y * s.use_y) | (mx.FLAG_Z * s.use_z) | (mx.FLAG_YAW * s.use_yaw)


def _target(context):
    from ..exporters import export_anims
    armature = export_anims.get_selected_armature(context)
    if armature is None:
        return None, None
    source_mode = getattr(context.scene, "witcher_w3_anim_source", "AUTO")
    action, _ = export_anims.resolve_action(armature, context=context, source_mode=source_mode)
    return armature, action


def export_frames(context, action):
    """Frames the exporter samples: the action's range in a set, the scene range for a lone action."""
    scene = context.scene
    if any(e.enabled for e in scene.witcher_anim_export_set):
        return tt.action_frames(action)
    return list(range(scene.frame_start, scene.frame_end + 1))


def analyse(context, armature, action):
    s = settings(context)
    me, raw, duration = tt.build_motion_extraction(context, armature, action, eps=s.eps, flags=flags_from_settings(s),
                                                   frames=export_frames(context, action))
    info = mx.summary(raw, duration)
    report = {
        "frames": len(raw), "duration": duration, "distance": info["distance"], "speed": info["speed"],
        "turn_deg": info["turn_deg"], "height": info["height"],
        "flags": me["flags"] if me else 0,
        "keys": (len(me["delta_times"]) + 1) if me else 0,
        "error": mx.max_error(me, raw, duration) if me else 0.0,
        "eps": s.eps,
        "axis_mask": flags_from_settings(s),
    }
    action[REPORT_PROP] = json.dumps(report)
    return report, me, raw


def stored_report(action):
    try:
        return json.loads(action.get(REPORT_PROP, "")) if action else None
    except (TypeError, ValueError):
        return None


class WITCH_OT_motion_generate_trajectory(bpy.types.Operator):
    """Replace Trajectory keys with the hips path; leave the body unchanged"""
    bl_idname = "witcher.motion_generate_trajectory"
    bl_label = "Generate Trajectory"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        armature, action = _target(context)
        return action is not None and tt.find_bone(armature, tt.TRAJECTORY) is not None

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        s = settings(context)
        armature, action = _target(context)
        if tt.find_bone(armature, s.source_bone) is None:
            self.report({'ERROR'}, f"Armature has no '{s.source_bone}' bone to follow")
            return {'CANCELLED'}
        try:
            tt.generate_trajectory(context, armature, action, source=s.source_bone, path=s.path, rotation=s.rotation,
                                   smooth=s.smooth, height=s.height)
            report, _, _ = analyse(context, armature, action)
        except ValueError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, f"Trajectory: {report['distance']:.2f} m at {report['speed']:.2f} m/s, "
                              f"turn {report['turn_deg']:.0f} deg")
        return {'FINISHED'}


class WITCH_OT_motion_analyse(bpy.types.Operator):
    """Preview the clip's exported motion extraction"""
    bl_idname = "witcher.motion_analyse"
    bl_label = "Preview Motion Extraction"

    @classmethod
    def poll(cls, context):
        armature, action = _target(context)
        return action is not None and tt.find_bone(armature, tt.TRAJECTORY) is not None

    def execute(self, context):
        armature, action = _target(context)
        try:
            report, me, _ = analyse(context, armature, action)
        except ValueError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        if me is None:
            self.report({'WARNING'}, "No motion extraction generated with these settings")
        else:
            self.report({'INFO'}, f"{report['keys']} keys on {', '.join(mx.flag_names(report['flags']))}")
        _show_path(context, armature, action, me)
        return {'FINISHED'}


class WITCH_OT_motion_clear_path(bpy.types.Operator):
    """Remove the motion path preview"""
    bl_idname = "witcher.motion_clear_path"
    bl_label = "Clear Path"

    @classmethod
    def poll(cls, context):
        return _path_object() is not None

    def execute(self, context):
        _remove_path()
        return {'FINISHED'}


def _path_object():
    return next((obj for obj in bpy.data.objects if obj.get("w3_motion_preview")), None)


def _remove_path():
    obj = _path_object()
    if obj is not None:
        curve = obj.data
        bpy.data.objects.remove(obj, do_unlink=True)
        if curve is not None and curve.users == 0:
            bpy.data.curves.remove(curve)


def _show_path(context, armature, action, me):
    """Draw motion keys relative to the clip's starting transform."""
    obj = _path_object()
    if me is None:
        _remove_path()
        return
    frames = export_frames(context, action)
    with tt.using_action(armature, action):
        (p0, q0), = tt.sample_bone(context, armature, tt.TRAJECTORY, frames[:1])
        from mathutils import Matrix
        context.scene.frame_set(frames[0])
        start = armature.matrix_world @ tt._root_delta(armature) @ Matrix.Translation(p0) @ q0.to_matrix().to_4x4()
    curve = obj.data if obj is not None else bpy.data.curves.new(PATH_OBJECT, 'CURVE')
    curve.dimensions = '3D'
    curve.splines.clear()
    spline = curve.splines.new('POLY')
    pts = [v for _, v in mx.keys(me)]
    spline.points.add(len(pts) - 1)
    for point, (x, y, z, _) in zip(spline.points, pts):
        point.co = (x, y, z, 1.0)
    curve.bevel_depth = 0.01
    if obj is None:
        obj = bpy.data.objects.new(PATH_OBJECT, curve)
        obj["w3_motion_preview"] = True
        context.scene.collection.objects.link(obj)
    obj.matrix_world = start
    obj.hide_select = True


class WITCH_OT_motion_details(bpy.types.Operator):
    bl_idname = "witcher.motion_details"
    bl_label = "Motion Extraction Details"
    bl_description = "Explain motion extraction and show copyable values from the last preview"

    action_name: StringProperty(name="Action")
    summary: StringProperty(name="Last Preview")

    def invoke(self, context, event):
        _, action = _target(context)
        self.action_name = action.name if action else ""
        info = stored_report(action)
        self.summary = (f"Travel {info['distance']:.3f} m; speed {info['speed']:.3f} m/s; turn {info['turn_deg']:.2f} deg; "
                       f"{info['frames']} frames in {info['duration']:.3f} s; {info['keys']} keys; "
                       f"axes {', '.join(mx.flag_names(info['flags'])) or 'none'}; max error {info['error']:.4f}"
                       if info else "")
        return context.window_manager.invoke_props_dialog(self, width=540)

    def draw(self, context):
        col = self.layout.column(align=True)
        for line in textwrap.wrap(" ".join(HELP), width=74):
            col.label(text=line)
        col.separator()
        col.label(text="Authored Trajectory rotation is kept by default.")
        col.label(text="Hip-based estimates are only for unkeyed Trajectory rotation.")
        if self.action_name:
            self.layout.prop(self, "action_name")
        if self.summary:
            self.layout.prop(self, "summary")

    def execute(self, context):
        return {'FINISHED'}


def draw(layout, context):
    s = settings(context)
    armature, action = _target(context)

    layout.operator(WITCH_OT_motion_details.bl_idname, text="Motion Details", icon='INFO')

    if action is None:
        layout.label(text="Select an armature and action", icon='INFO')
        return
    if tt.find_bone(armature, tt.TRAJECTORY) is None:
        layout.label(text="No Trajectory bone", icon='ERROR')
        return
    layout.label(text=action.name, icon='ACTION')

    box = layout.box()
    box.label(text="1. Trajectory", icon='TRACKING')
    col = box.column(align=True)
    col.prop_search(s, "source_bone", armature.pose, "bones", text="Follow")
    col.prop(s, "path")
    col.prop(s, "rotation")
    if s.rotation in {'LINEAR', 'SMOOTH'}:
        col.label(text="Estimate: hip lean affects turns", icon='INFO')
    sub = col.row(align=True)
    sub.enabled = s.path == 'SMOOTH' or s.rotation == 'SMOOTH'
    sub.prop(s, "smooth")
    col.prop(s, "height")
    box.operator(WITCH_OT_motion_generate_trajectory.bl_idname, icon='TRACKING_FORWARDS')

    box = layout.box()
    box.label(text="2. Motion Extraction", icon='CON_FOLLOWPATH')
    col = box.column(align=True)
    col.prop(s, "eps")
    col.row(align=True).prop(s, "axes", expand=True)
    if s.axes == 'CUSTOM':
        row = col.row(align=True)
        for name in ("use_x", "use_y", "use_z", "use_yaw"):
            row.prop(s, name, toggle=True)
    row = box.row(align=True)
    row.operator(WITCH_OT_motion_analyse.bl_idname, icon='VIEWZOOM')
    row.operator(WITCH_OT_motion_clear_path.bl_idname, text="", icon='X')

    report = stored_report(action)
    if report:
        col = box.column(align=True)
        col.label(text="Last Preview")
        if not report["flags"]:
            col.label(text="No motion stored", icon='INFO')
        else:
            col.label(text=f"Travel {report['distance']:.2f} m  ·  {report['speed']:.2f} m/s")
            col.label(text=f"Turn {report['turn_deg']:.0f}°  ·  {report['keys']} keys")
            col.label(text=f"Axes {', '.join(mx.flag_names(report['flags']))}")
            col.label(text=f"{report['frames']} frames, {report['duration']:.2f} s")
        if abs(report.get("eps", s.eps) - s.eps) > 1e-6 or report.get("axis_mask") != flags_from_settings(s):
            col.label(text="Settings changed; preview again", icon='FILE_REFRESH')

    entry = next((e for e in getattr(context.scene, "witcher_anim_export_set", []) if e.action_name == action.name), None)
    if entry is not None:
        row = layout.row()
        row.prop(entry, "include_motion_extraction", text="Export Motion Extraction")
        if report and report["flags"] and not entry.include_motion_extraction:
            layout.label(text="Motion export is off", icon='ERROR')
    else:
        layout.label(text="Add clip to Export Set", icon='INFO')


_classes = (
    WitcherMotionSettings,
    WITCH_OT_motion_details,
    WITCH_OT_motion_generate_trajectory,
    WITCH_OT_motion_analyse,
    WITCH_OT_motion_clear_path,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.witcher_motion = PointerProperty(type=WitcherMotionSettings)


def unregister():
    del bpy.types.Scene.witcher_motion
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
