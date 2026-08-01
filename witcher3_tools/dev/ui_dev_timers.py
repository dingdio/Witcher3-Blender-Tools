import bpy

from ..importers import import_blender_fun, import_environment, import_particle
from ..ui import ui_equipment, ui_map, ui_rig

_TIMERS = (
    {
        "id": "particle_billboards",
        "label": "Particle billboard follow (30 Hz)",
        "module": import_particle,
        "func": "_follow_particle_viewports",
        "start": "_ensure_particle_preview_runtime",
    },
    {
        "id": "sky_follow",
        "label": "Sky preview viewport follow (5 Hz)",
        "module": import_environment,
        "func": "_follow_viewports",
        "start": "_ensure_view_follow",
        "stop": "stop_preview_runtime",
    },
    {
        "id": "deferred_materials",
        "label": "Deferred material streaming",
        "module": import_blender_fun,
        "func": "_deferred_material_tick",
        "start": "ensure_deferred_material_timer",
    },
    {
        "id": "terrain_view_lod",
        "label": "Terrain view-LOD / foliage auto tick",
        "module": ui_map,
        "func": "_terrain_view_lod_auto_tick",
        "start": "register_view_lod_timer",
        "stop": "unregister_view_lod_timer",
    },
    {
        "id": "equipment_icons",
        "label": "Equipment icon resolver",
        "module": ui_equipment,
        "func": "_equipment_item_icon_timer",
        "start": "_ensure_equipment_item_icon_timer",
        "flag": "_EQUIPMENT_ITEM_ICON_TIMER_RUNNING",
    },
    {
        "id": "rig_pose_sync",
        "label": "Rig pose-mode sync",
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


class WITCHER_DEV_OT_timer_stop(bpy.types.Operator):
    bl_idname = "witcher_dev.timer_stop"
    bl_label = "Stop Timer"
    bl_description = "Unregister this recurring timer"

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


class WITCHER_DEV_OT_timer_start(bpy.types.Operator):
    bl_idname = "witcher_dev.timer_start"
    bl_label = "Start Timer"
    bl_description = "Start this timer through its normal ensure/start path"

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


class VIEW3D_PT_witcher_dev_timers(bpy.types.Panel):
    bl_label = "Timers"
    bl_idname = "VIEW3D_PT_witcher_dev_timers"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'W3 Dev'
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)
        for entry in _TIMERS:
            running = _is_running(entry)
            row = col.row(align=True)
            row.label(text=entry["label"], icon='PLAY' if running else 'PAUSE')
            if running:
                op = row.operator("witcher_dev.timer_stop", text="", icon='SNAP_FACE')
            else:
                op = row.operator("witcher_dev.timer_start", text="", icon='TRIA_RIGHT')
            op.timer_id = entry["id"]
        col.separator()
        col.label(text="Instance timers (not controllable here):", icon='INFO')
        col.label(text="  Unreal bridge job drain, LiveLink stream")


_classes = (
    WITCHER_DEV_OT_timer_stop,
    WITCHER_DEV_OT_timer_start,
    VIEW3D_PT_witcher_dev_timers,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
