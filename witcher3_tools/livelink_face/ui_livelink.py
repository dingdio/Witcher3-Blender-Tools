import logging
from pathlib import Path

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, StringProperty
from bpy_extras.io_utils import ImportHelper

from . import importer


log = logging.getLogger(__name__)
_STREAM_SESSION = None


def _head_unit_items():
    return [
        ("AUTO", "Auto", "Infer degrees or radians from the imported values"),
        ("DEGREES", "Degrees", "Treat HeadYaw, HeadPitch, and HeadRoll as degrees"),
        ("RADIANS", "Radians", "Treat HeadYaw, HeadPitch, and HeadRoll as radians"),
    ]


def _scene_head_units(scene):
    return getattr(scene, "witcher_livelink_head_units", "AUTO")


def _scene_head_scale(scene):
    return float(getattr(scene, "witcher_livelink_head_scale", 1.0))


def _scene_neck_share(scene):
    return float(getattr(scene, "witcher_livelink_neck_share", 0.35))


def _scene_mirror_view(scene):
    return bool(getattr(scene, "witcher_livelink_mirror_view", False))


def is_streaming():
    return _STREAM_SESSION is not None and _STREAM_SESSION.running


class WITCH_OT_import_livelinkface_csv(bpy.types.Operator, ImportHelper):
    """Import Live Link Face CSV onto Witcher FACS controls and head/neck bones."""
    bl_idname = "witcher.import_livelinkface_csv"
    bl_label = "Import Live Link Face CSV"
    bl_options = {'UNDO'}

    filename_ext = ".csv"
    filter_glob: StringProperty(default="*.csv", options={'HIDDEN'})

    apply_facs: BoolProperty(
        name="FACS",
        default=True,
        description="Key ARKit/FACS values on w3_face_poses",
    )
    apply_head: BoolProperty(
        name="Head / Neck",
        default=True,
        description="Key only the head and neck pose bones from HeadYaw, HeadPitch, and HeadRoll",
    )
    head_units: EnumProperty(
        name="Head Units",
        items=_head_unit_items(),
        default="AUTO",
    )
    head_rotation_scale: FloatProperty(
        name="Head Scale",
        default=1.0,
        min=-10.0,
        max=10.0,
        description="Scale imported HeadYaw, HeadPitch, and HeadRoll values",
    )
    neck_rotation_share: FloatProperty(
        name="Neck Share",
        default=0.35,
        min=0.0,
        max=1.0,
        subtype='FACTOR',
        description="Fraction of head rotation applied to the neck bone",
    )
    zero_head_from_first_frame: BoolProperty(
        name="Zero Head From First Frame",
        default=False,
        description="Subtract the first frame's head rotation from all imported head samples",
    )
    mirror_view: BoolProperty(
        name="Mirror View",
        default=False,
        description="Swap left/right FACS and invert head yaw/roll for mirror-style preview/import",
    )
    replace_existing: BoolProperty(
        name="Replace Existing",
        default=True,
        description="Replace the existing Live Link Face NLA track on this character",
    )

    def invoke(self, context, event):
        scene = context.scene
        self.head_units = _scene_head_units(scene)
        self.head_rotation_scale = _scene_head_scale(scene)
        self.neck_rotation_share = _scene_neck_share(scene)
        self.mirror_view = _scene_mirror_view(scene)
        return ImportHelper.invoke(self, context, event)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "apply_facs")
        layout.prop(self, "apply_head")
        col = layout.column()
        col.enabled = self.apply_head
        col.prop(self, "head_units")
        col.prop(self, "head_rotation_scale")
        col.prop(self, "neck_rotation_share")
        col.prop(self, "zero_head_from_first_frame")
        layout.prop(self, "mirror_view")
        layout.prop(self, "replace_existing")

    def execute(self, context):
        armature = importer.resolve_target_armature(context)
        if armature is None:
            self.report({'ERROR'}, "No character target armature found.")
            return {'CANCELLED'}

        try:
            capture = importer.read_livelinkface_csv(self.filepath)
            result = importer.apply_capture_to_armature(
                context,
                armature,
                capture,
                action_name=f"LiveLinkFace {Path(self.filepath).stem}",
                replace_existing=self.replace_existing,
                apply_facs=self.apply_facs,
                apply_head=self.apply_head,
                head_units=self.head_units,
                head_rotation_scale=self.head_rotation_scale,
                neck_rotation_share=self.neck_rotation_share,
                zero_head_from_first_frame=self.zero_head_from_first_frame,
                mirror_view=self.mirror_view,
            )
        except Exception as exc:
            log.exception("Live Link Face import failed")
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

        bones = ", ".join(result["head_bones"]) if result["head_bones"] else "none"
        self.report(
            {'INFO'},
            f"Imported {result['frame_count']} Live Link frame(s), {result['facs_count']} FACS channel(s), head bones: {bones}.",
        )
        return {'FINISHED'}


class WITCH_OT_start_livelinkface_stream(bpy.types.Operator):
    """Start Live Link Face UDP preview stream."""
    bl_idname = "witcher.start_livelinkface_stream"
    bl_label = "Start Live Link Face Stream"

    def execute(self, context):
        global _STREAM_SESSION

        armature = importer.resolve_target_armature(context)
        if armature is None:
            self.report({'ERROR'}, "No character target armature found.")
            return {'CANCELLED'}

        try:
            importer.ensure_livelink_face_setup(context, armature)
            if _STREAM_SESSION is not None:
                _STREAM_SESSION.stop()
            scene = context.scene
            _STREAM_SESSION = importer.LiveLinkStreamSession(
                armature_name=armature.name,
                udp_port=getattr(scene, "witcher_livelink_udp_port", 11111),
                apply_facs=getattr(scene, "witcher_livelink_stream_facs", True),
                apply_head=getattr(scene, "witcher_livelink_stream_head", True),
                head_units=_scene_head_units(scene),
                head_rotation_scale=_scene_head_scale(scene),
                neck_rotation_share=_scene_neck_share(scene),
                mirror_view=_scene_mirror_view(scene),
            )
            _STREAM_SESSION.start()
        except Exception as exc:
            log.exception("Live Link Face stream failed to start")
            if _STREAM_SESSION is not None:
                _STREAM_SESSION.stop()
                _STREAM_SESSION = None
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

        self.report({'INFO'}, f"Live Link Face stream listening on UDP {context.scene.witcher_livelink_udp_port}.")
        return {'FINISHED'}


class WITCH_OT_stop_livelinkface_stream(bpy.types.Operator):
    """Stop Live Link Face UDP preview stream."""
    bl_idname = "witcher.stop_livelinkface_stream"
    bl_label = "Stop Live Link Face Stream"

    def execute(self, context):
        global _STREAM_SESSION
        if _STREAM_SESSION is not None:
            _STREAM_SESSION.stop()
            _STREAM_SESSION = None
        self.report({'INFO'}, "Live Link Face stream stopped.")
        return {'FINISHED'}


def draw_morph_panel(layout, context):
    scene = context.scene
    box = layout.box()
    header = box.row(align=True)
    header.label(text="Live Link Face", icon='SHAPEKEY_DATA')

    row = box.row(align=True)
    row.operator(WITCH_OT_import_livelinkface_csv.bl_idname, text="Import CSV", icon='IMPORT')
    if is_streaming():
        row.operator(WITCH_OT_stop_livelinkface_stream.bl_idname, text="Stop Stream", icon='PAUSE')
    else:
        row.operator(WITCH_OT_start_livelinkface_stream.bl_idname, text="Start Stream", icon='PLAY')

    stream = box.row(align=True)
    stream.prop(scene, "witcher_livelink_udp_port", text="Port")
    stream.prop(scene, "witcher_livelink_stream_facs", text="FACS", toggle=True)
    stream.prop(scene, "witcher_livelink_stream_head", text="Head", toggle=True)
    stream.prop(scene, "witcher_livelink_mirror_view", text="Mirror", toggle=True)

    head = box.row(align=True)
    head.prop(scene, "witcher_livelink_head_units", text="")
    head.prop(scene, "witcher_livelink_head_scale", text="Scale")
    head.prop(scene, "witcher_livelink_neck_share", text="Neck")


_CLASSES = (
    WITCH_OT_import_livelinkface_csv,
    WITCH_OT_start_livelinkface_stream,
    WITCH_OT_stop_livelinkface_stream,
)


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)

    bpy.types.Scene.witcher_livelink_udp_port = IntProperty(
        name="UDP Port",
        default=11111,
        min=1,
        max=65535,
    )
    bpy.types.Scene.witcher_livelink_stream_facs = BoolProperty(
        name="FACS",
        default=True,
    )
    bpy.types.Scene.witcher_livelink_stream_head = BoolProperty(
        name="Head",
        default=True,
    )
    bpy.types.Scene.witcher_livelink_mirror_view = BoolProperty(
        name="Mirror View",
        default=False,
        description="Swap left/right FACS and invert head yaw/roll while streaming",
    )
    bpy.types.Scene.witcher_livelink_head_units = EnumProperty(
        name="Head Units",
        items=_head_unit_items(),
        default="AUTO",
    )
    bpy.types.Scene.witcher_livelink_head_scale = FloatProperty(
        name="Head Scale",
        default=1.0,
        min=-10.0,
        max=10.0,
    )
    bpy.types.Scene.witcher_livelink_neck_share = FloatProperty(
        name="Neck Share",
        default=0.35,
        min=0.0,
        max=1.0,
        subtype='FACTOR',
    )


def unregister():
    global _STREAM_SESSION
    if _STREAM_SESSION is not None:
        _STREAM_SESSION.stop()
        _STREAM_SESSION = None

    for prop_name in (
        "witcher_livelink_neck_share",
        "witcher_livelink_head_scale",
        "witcher_livelink_head_units",
        "witcher_livelink_mirror_view",
        "witcher_livelink_stream_head",
        "witcher_livelink_stream_facs",
        "witcher_livelink_udp_port",
    ):
        if hasattr(bpy.types.Scene, prop_name):
            delattr(bpy.types.Scene, prop_name)

    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
