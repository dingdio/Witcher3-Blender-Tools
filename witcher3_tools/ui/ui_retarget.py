"""Humanoid retargeting UI for W3 rigs."""
import math

import bpy
from bpy.props import BoolProperty, CollectionProperty, FloatProperty, IntProperty, PointerProperty, StringProperty

from ..animation import mixamo_retarget as retarget


def settings(context):
    return context.scene.witcher_mixamo_retarget


def _rows(s):
    return [(b.target, b.source, b.source2, b.mix) for b in s.bones]


def _armature_poll(self, obj):
    return obj.type == 'ARMATURE'


def _clear_other_previews(self, context):
    """A preview left on the old target would have no button to remove it."""
    for obj in bpy.data.objects:
        if obj is not self.target and retarget.preview_active(obj):
            retarget.clear_preview(obj)


class WitcherRetargetBone(bpy.types.PropertyGroup):
    target: StringProperty(name="Target", description="Bone of the Witcher armature")
    source: StringProperty(name="Source", description="Bone of the source armature whose rotation from rest the target bone takes")
    source2: StringProperty(name="Blend", description="Optional second source bone; the target takes a blend of both rotations")
    mix: FloatProperty(name="Mix", description="Share of the second source bone", default=0.0, min=0.0, max=1.0)


class WitcherRetargetSettings(bpy.types.PropertyGroup):
    source: PointerProperty(name="Source", type=bpy.types.Object, poll=_armature_poll,
                            description="Armature with the animation, e.g. an imported Mixamo FBX")
    target: PointerProperty(name="Target", type=bpy.types.Object, poll=_armature_poll, update=_clear_other_previews,
                            description="Witcher armature that receives the animation")
    bones: CollectionProperty(type=WitcherRetargetBone)
    bone_index: IntProperty(default=0)
    fingers: BoolProperty(name="Fingers", default=True, description="Map the finger bones too")
    scale: FloatProperty(name="Scale", default=1.0, min=0.001, soft_max=100.0,
                         description="Hips translation scale: target hips height over source hips height (Auto Map fills it)")
    match_rest: BoolProperty(name="Match Rest Pose", default=True,
                             description="Turn each target bone's rest direction onto the source's first (a T-pose source on the "
                                         "A-pose Witcher rig); off transfers the rotations from rest as they are")
    root_motion: BoolProperty(name="Root Motion to Trajectory", default=True,
                              description="Also move the Trajectory bone along the hips' straight-line travel, so the clip "
                                          "exports with motion extraction (the body keeps its travel, as in game clips). "
                                          "Use Motion Extraction > Generate Trajectory for curved paths or turns")
    ik: BoolProperty(name="IK Bones", default=True,
                     description="Key the IK_* bones on the bones they mark, as the game's clips do")
    name: StringProperty(name="Name", description="Name of the baked action and of its exported clip; empty uses <target>_retarget")


class WITCH_UL_retarget_bones(bpy.types.UIList):
    use_filter_show = False

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        ok = data.source is not None and data.target is not None and item.target in data.target.data.bones \
            and item.source in data.source.data.bones
        source = item.source.rsplit(":", 1)[-1] + (" + " + item.source2.rsplit(":", 1)[-1] if item.source2 else "")
        layout.label(text=f"{item.target}  ←  {source}", icon='BONE_DATA' if ok else 'ERROR')

    def draw_filter(self, context, layout):
        pass


class _Ready:
    @classmethod
    def poll(cls, context):
        s = settings(context)
        return s.source is not None and s.target is not None and s.source != s.target


class WITCH_OT_retarget_auto_map(_Ready, bpy.types.Operator):
    bl_idname = "witcher.retarget_auto_map"
    bl_label = "Auto Map"
    bl_description = ("Fill the bone map from the bone names (Mixamo, UE5 and similar rigs onto the W3 human skeleton), the scale "
                      "from the hips heights, and turn the source to face the same way as the target")
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        s = settings(context)
        rows = retarget.auto_map(s.source, s.target, s.fingers)
        if not rows:
            self.report({'ERROR'}, "No bone names matched")
            return {'CANCELLED'}
        s.bones.clear()
        for t, src, src2, mix in rows:
            item = s.bones.add()
            item.target, item.source, item.source2, item.mix = t, src, src2, mix
        s.bone_index = 0
        s.scale = retarget.height_ratio(s.source, s.target, rows)
        turn = retarget.facing_turn(s.source, s.target, rows)
        text = f"{len(rows)} bones mapped, scale {s.scale:.3f}"
        if abs(math.degrees(turn)) > 1.0:
            retarget.turn_source(s.source, turn)
            text += f", source turned {math.degrees(turn):.0f}° to face the target"
        self.report({'INFO'}, text)
        return {'FINISHED'}


class WITCH_OT_retarget_bone_add(bpy.types.Operator):
    bl_idname = "witcher.retarget_bone_add"
    bl_label = "Add Bone Pair"
    bl_description = "Add a row to the bone map"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        s = settings(context)
        s.bones.add()
        s.bone_index = len(s.bones) - 1
        return {'FINISHED'}


class WITCH_OT_retarget_bone_remove(bpy.types.Operator):
    bl_idname = "witcher.retarget_bone_remove"
    bl_label = "Remove Bone Pair"
    bl_description = "Remove the selected row from the bone map"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        s = settings(context)
        return 0 <= s.bone_index < len(s.bones)

    def execute(self, context):
        s = settings(context)
        s.bones.remove(s.bone_index)
        s.bone_index = min(s.bone_index, len(s.bones) - 1)
        return {'FINISHED'}


class WITCH_OT_retarget_preview(_Ready, bpy.types.Operator):
    bl_idname = "witcher.retarget_preview"
    bl_label = "Preview"
    bl_description = "Preview the retarget; Clear Preview restores the target's action and pose"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        s = settings(context)
        try:
            count = retarget.preview(context, s.source, s.target, _rows(s), s.scale, s.match_rest, s.ik)
        except ValueError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, f"Preview on {count} bones")
        return {'FINISHED'}


class WITCH_OT_retarget_clear_preview(bpy.types.Operator):
    bl_idname = "witcher.retarget_clear_preview"
    bl_label = "Clear Preview"
    bl_description = "Restore the target's action and pose, and remove preview helpers"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return retarget.preview_active(settings(context).target)

    def execute(self, context):
        retarget.clear_preview(settings(context).target)
        return {'FINISHED'}


class WITCH_OT_retarget_bake(_Ready, bpy.types.Operator):
    bl_idname = "witcher.retarget_bake"
    bl_label = "Bake"
    bl_description = ("Bake the source's animation into a new action on the target, one key per source frame with every bone "
                      "keyed; the scene's range becomes the bake's (clears the preview). Export it like any clip (W3 clips are 30 fps)")
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        s = settings(context)
        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        name = s.name or f"{s.target.name}_retarget"
        try:
            action, stats = retarget.bake(context, s.source, s.target, _rows(s), s.scale, name, s.root_motion, s.match_rest, s.ik)
        except ValueError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        context.scene.frame_start, context.scene.frame_end = (int(f) for f in action.frame_range)
        # Motion and export tools must act on the bake, not on an NLA strip left on the rig.
        context.scene.witcher_w3_anim_source = 'ACTION'
        text = f"{action.name}: {stats['bones']} bones, {stats['frames']} frames at {stats['fps']:g} fps"
        if abs(stats["fps"] - 30.0) > 1e-3:
            self.report({'WARNING'}, text + "; W3 clips play at 30 fps, set the scene to 30 and bake again")
        else:
            self.report({'INFO'}, text)
        return {'FINISHED'}


def draw(layout, context):
    s = settings(context)
    col = layout.column(align=True)
    col.prop(s, "source", icon='ARMATURE_DATA')
    col.prop(s, "target", icon='OUTLINER_OB_ARMATURE')
    if s.source is None or s.target is None:
        return

    row = layout.row(align=True)
    row.label(text="Bone Map", icon='GROUP_BONE')
    row.prop(s, "fingers", text="", icon='VIEW_PAN')
    row.operator(WITCH_OT_retarget_auto_map.bl_idname, text="Auto Map", icon='AUTO')
    row = layout.row()
    row.template_list("WITCH_UL_retarget_bones", "", s, "bones", s, "bone_index", rows=5)
    col = row.column(align=True)
    col.operator(WITCH_OT_retarget_bone_add.bl_idname, text="", icon='ADD')
    col.operator(WITCH_OT_retarget_bone_remove.bl_idname, text="", icon='REMOVE')
    if 0 <= s.bone_index < len(s.bones):
        item = s.bones[s.bone_index]
        col = layout.column(align=True)
        col.prop_search(item, "target", s.target.data, "bones", text="Target")
        col.prop_search(item, "source", s.source.data, "bones", text="Source")
        col.prop_search(item, "source2", s.source.data, "bones", text="Blend")
        sub = col.row()
        sub.enabled = bool(item.source2)
        sub.prop(item, "mix", slider=True)

    col = layout.column(align=True)
    col.prop(s, "scale")
    col.prop(s, "match_rest")
    col.prop(s, "root_motion")
    col.prop(s, "ik")
    col.prop(context.scene.render, "fps")

    row = layout.row(align=True)
    row.operator(WITCH_OT_retarget_preview.bl_idname, icon='HIDE_OFF')
    sub = row.row(align=True)
    sub.enabled = retarget.preview_active(s.target)
    sub.operator(WITCH_OT_retarget_clear_preview.bl_idname, text="", icon='X')
    layout.prop(s, "name")
    layout.operator(WITCH_OT_retarget_bake.bl_idname, icon='REC')


_classes = (WitcherRetargetBone, WitcherRetargetSettings, WITCH_UL_retarget_bones, WITCH_OT_retarget_auto_map,
            WITCH_OT_retarget_bone_add, WITCH_OT_retarget_bone_remove, WITCH_OT_retarget_preview,
            WITCH_OT_retarget_clear_preview, WITCH_OT_retarget_bake)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.witcher_mixamo_retarget = PointerProperty(type=WitcherRetargetSettings)


def unregister():
    del bpy.types.Scene.witcher_mixamo_retarget
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
