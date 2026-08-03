import bpy

from ..importers import import_blender_fun, import_environment, import_particle
from . import ui_equipment, ui_map, ui_rig


_TIMERS = (
    {
        "id": "particle_billboards",
        "label": "Particle Billboards",
        "module": import_particle,
        "func": "_follow_particle_viewports",
        "start": "_ensure_particle_preview_runtime",
    },
    {
        "id": "sky_follow",
        "label": "Sky Preview",
        "module": import_environment,
        "func": "_follow_viewports",
        "start": "_ensure_view_follow",
        "stop": "stop_preview_runtime",
    },
    {
        "id": "deferred_materials",
        "label": "Material Streaming",
        "module": import_blender_fun,
        "func": "_deferred_material_tick",
        "start": "ensure_deferred_material_timer",
    },
    {
        "id": "terrain_view_lod",
        "label": "Terrain / Foliage LOD",
        "module": ui_map,
        "func": "_terrain_view_lod_auto_tick",
        "start": "register_view_lod_timer",
        "stop": "unregister_view_lod_timer",
    },
    {
        "id": "equipment_icons",
        "label": "Equipment Icons",
        "module": ui_equipment,
        "func": "_equipment_item_icon_timer",
        "start": "_ensure_equipment_item_icon_timer",
        "flag": "_EQUIPMENT_ITEM_ICON_TIMER_RUNNING",
    },
    {
        "id": "rig_pose_sync",
        "label": "Rig Pose Sync",
        "module": ui_rig,
        "func": "_rig_pose_sync_timer",
        "start": "start_rig_pose_sync_timer",
        "stop": "stop_rig_pose_sync_timer",
    },
)


def _entry(timer_id):
    return next((entry for entry in _TIMERS if entry["id"] == timer_id), None)


def _timer_func(entry):
    return getattr(entry["module"], entry["func"], None)


def _is_running(entry):
    func = _timer_func(entry)
    try:
        return func is not None and bpy.app.timers.is_registered(func)
    except Exception:
        return False


class WITCHER_OT_timer_stop(bpy.types.Operator):
    bl_idname = "witcher.timer_stop"
    bl_label = "Stop Timer"
    bl_description = "Stop this background timer"

    timer_id: bpy.props.StringProperty()

    def execute(self, context):
        entry = _entry(self.timer_id)
        if entry is None:
            return {'CANCELLED'}
        stop = getattr(entry["module"], entry.get("stop", ""), None)
        if stop is not None:
            stop()
        else:
            func = _timer_func(entry)
            if func is not None and bpy.app.timers.is_registered(func):
                bpy.app.timers.unregister(func)
        if entry.get("flag"):
            setattr(entry["module"], entry["flag"], False)
        return {'FINISHED'}


class WITCHER_OT_timer_start(bpy.types.Operator):
    bl_idname = "witcher.timer_start"
    bl_label = "Start Timer"
    bl_description = "Start this background timer"

    timer_id: bpy.props.StringProperty()

    def execute(self, context):
        entry = _entry(self.timer_id)
        if entry is None:
            return {'CANCELLED'}
        start = getattr(entry["module"], entry.get("start", ""), None)
        if start is not None:
            start()
        else:
            func = _timer_func(entry)
            if func is not None and not bpy.app.timers.is_registered(func):
                bpy.app.timers.register(func, first_interval=0.0)
        return {'FINISHED'}


def draw_timer_controls(layout):
    for entry in _TIMERS:
        running = _is_running(entry)
        row = layout.row(align=True)
        row.label(text="", icon='PLAY' if running else 'PAUSE')
        row.label(text=entry["label"])
        if running:
            op = row.operator(WITCHER_OT_timer_stop.bl_idname, text="", icon='SNAP_FACE')
        else:
            op = row.operator(WITCHER_OT_timer_start.bl_idname, text="", icon='TRIA_RIGHT')
        op.timer_id = entry["id"]


_classes = (
    WITCHER_OT_timer_stop,
    WITCHER_OT_timer_start,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
