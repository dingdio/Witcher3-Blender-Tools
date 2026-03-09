"""RE Animations Integration for Witcher 3 Blender Tools.

Bridges the RE Animations Plugin's .re (HDF5) format with the Witcher 3
addon's w3_face_poses control bone system.

Import: Reads shape key data from .re files -> keyframes on w3_face_poses.
Export: Samples w3_face_poses per frame -> temporary mesh + armature -> RE export.
"""

import os
import bpy
import logging
from ..action_compat import bind_strip_action_slot, new_action_fcurve, resolve_action_slot
from bpy.props import BoolProperty, StringProperty, FloatProperty
from bpy_extras.io_utils import ImportHelper, ExportHelper

log = logging.getLogger(__name__)

CONTROL_BONE = "w3_face_poses"
_RE_PLUGIN_PATCHED = False


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

def _is_re_plugin_available():
    try:
        from blender_re_animations_plugin import hdf_manager  # noqa: F401
        return True
    except Exception:
        return False


def _is_main_armature(obj):
    """Safe check: obj must be a bpy.types.Object of type ARMATURE with w3_face_poses."""
    try:
        if not obj or not hasattr(obj, 'type') or obj.type != 'ARMATURE':
            return False
        if not obj.pose:
            return False
        return CONTROL_BONE in obj.pose.bones
    except Exception:
        return False


def _get_morph_names(pose_bone):
    """Return custom-property names from the pose bone (skip internals)."""
    return [k for k in pose_bone.keys() if not k.startswith("_")]


def _has_view_3d_context(context):
    area = getattr(context, 'area', None)
    return area is not None and area.type == 'VIEW_3D'


def _find_3d_override():
    """Find a VIEW_3D area + WINDOW region for temp_override context."""
    wm = bpy.context.window_manager
    for window in wm.windows:
        screen = window.screen
        for area in screen.areas:
            if area.type == 'VIEW_3D':
                for region in area.regions:
                    if region.type == 'WINDOW':
                        return {
                            'window': window,
                            'screen': screen,
                            'area': area,
                            'region': region,
                        }
    return None


def _ensure_object_mode(context):
    """Switch to OBJECT mode if currently in another mode, ignoring errors."""
    try:
        active = context.active_object
        if active and active.mode != 'OBJECT':
            if _has_view_3d_context(context):
                bpy.ops.object.mode_set(mode='OBJECT')
            else:
                ov = _find_3d_override()
                if ov:
                    with bpy.context.temp_override(**ov):
                        bpy.ops.object.mode_set(mode='OBJECT')
    except Exception:
        pass


def _resolve_filepath(filepath):
    if not filepath:
        return ""
    path = bpy.path.abspath(filepath)
    if path.startswith("//"):
        path = os.path.abspath(path.replace("//", ""))
    return os.path.normpath(path)


def _patch_re_plugin_selected_ids():
    """Patch RE plugin selection helper to ignore non-Object datablocks."""
    global _RE_PLUGIN_PATCHED
    if _RE_PLUGIN_PATCHED:
        return
    try:
        from blender_re_animations_plugin.common_data import AnimHelper as ReAnimHelper
    except Exception:
        return

    if getattr(ReAnimHelper, "_w3_safe_patch", False):
        _RE_PLUGIN_PATCHED = True
        return

    orig = ReAnimHelper.get_selected_obj_list

    def _safe_selected_obj_list(context):
        try:
            objs = orig(context)
        except Exception:
            objs = []
        return [o for o in objs if hasattr(o, "type")]

    ReAnimHelper.get_selected_obj_list = staticmethod(_safe_selected_obj_list)
    ReAnimHelper._w3_safe_patch = True
    _RE_PLUGIN_PATCHED = True


def _patch_re_phoneme_headers(morph_names):
    """Force RE plugin to export all provided morph names."""
    try:
        from blender_re_animations_plugin import phoneme_extractor as _pe
    except Exception:
        return None

    orig_read = _pe.PhonemeExtractor.read_phoneme_weights

    def _read(self):
        orig_read(self)
        try:
            self.mimic_header_list = list(morph_names)
        except Exception:
            pass

    _pe.PhonemeExtractor.read_phoneme_weights = _read
    return orig_read


def _restore_re_phoneme_headers(orig_read):
    if not orig_read:
        return
    try:
        from blender_re_animations_plugin import phoneme_extractor as _pe
    except Exception:
        return
    _pe.PhonemeExtractor.read_phoneme_weights = orig_read


# ---------------------------------------------------------------------------
#  Import .re -> w3_face_poses
# ---------------------------------------------------------------------------

class WITCH_OT_ImportREMimic(bpy.types.Operator, ImportHelper):
    """Import .re mimic animation onto w3_face_poses (Witcher 3 Tools)"""
    bl_idname = "witcher.import_re_mimic"
    bl_label = "W3 Tools: Import .re Mimic"
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = '.re'
    filter_glob: StringProperty(default='*.re', options={'HIDDEN'})

    recreate_phonemes: BoolProperty(
        name="Approximate Phonemes",
        description="After import, approximate phoneme values from the morph data",
        default=False,
    )
    nla_track_name: StringProperty(
        name="NLA Track", default="mimic_import",
    )

    @classmethod
    def poll(cls, context):
        try:
            return _is_re_plugin_available() and _is_main_armature(context.active_object)
        except Exception:
            return False

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        armature = context.active_object
        if not _is_main_armature(armature):
            self.report({'ERROR'}, "Active object must be the main armature with w3_face_poses")
            return {'CANCELLED'}

        pose_bone = armature.pose.bones[CONTROL_BONE]

        # --- Read .re via RE plugin's HdfManager ---
        try:
            from blender_re_animations_plugin.hdf_manager import HdfManager
            from blender_re_animations_plugin.asset_node import ReAssetNode
        except ImportError:
            self.report({'ERROR'}, "RE Animations Plugin not available")
            return {'CANCELLED'}

        resolved_path = _resolve_filepath(self.filepath)
        if not resolved_path or not os.path.isfile(resolved_path):
            self.report({'ERROR'}, f".re file not found: {resolved_path or self.filepath}")
            return {'CANCELLED'}

        hdf = HdfManager()
        try:
            hdf.read_hdf_file(resolved_path)
        except Exception as e:
            self.report({'ERROR'}, f"Failed to read .re file: {e} ({resolved_path})")
            return {'CANCELLED'}

        # Extract shape-key frames
        shape_keys_data = None
        for n in hdf.get_nodes():
            if isinstance(n, ReAssetNode):
                skc = getattr(n, 'shape_key_container', None)
                if skc:
                    shape_keys_data = skc.shape_keys

        if not shape_keys_data:
            self.report({'ERROR'}, "No mimic / shape-key data in .re file")
            return {'CANCELLED'}

        # Collect morph names and normalise frame dicts
        all_morphs = set()
        normalised = []  # list[dict[str,float]]
        for frame_data in shape_keys_data:
            merged = {}
            if isinstance(frame_data, dict):
                merged = frame_data
            elif isinstance(frame_data, (list, tuple)):
                for d in frame_data:
                    if isinstance(d, dict):
                        merged.update(d)
            all_morphs.update(merged.keys())
            normalised.append(merged)

        if not all_morphs:
            self.report({'WARNING'}, "Shape-key data is empty")
            return {'CANCELLED'}

        # Ensure custom props exist on w3_face_poses
        for name in all_morphs:
            if name not in pose_bone:
                pose_bone[name] = 0.0
                try:
                    pose_bone.id_properties_ui(name).update(
                        min=0.0, max=1.0, soft_min=0.0, soft_max=1.0)
                except Exception:
                    pass

        # Build action
        base = os.path.splitext(os.path.basename(resolved_path))[0]
        action = bpy.data.actions.new(name=f"{base}_mimic")
        fcs = {}
        for name in all_morphs:
            dp = f'pose.bones["{CONTROL_BONE}"]["{name}"]'
            fcs[name] = new_action_fcurve(action, armature, data_path=dp)

        for f, vals in enumerate(normalised):
            for name, value in vals.items():
                fc = fcs.get(name)
                if fc:
                    fc.keyframe_points.add(1)
                    kp = fc.keyframe_points[-1]
                    kp.co = (f, float(value))
                    kp.interpolation = 'LINEAR'

        for fc in fcs.values():
            fc.update()

        # Push to NLA
        if armature.animation_data is None:
            armature.animation_data_create()
        track = armature.animation_data.nla_tracks.get(self.nla_track_name)
        if track is None:
            track = armature.animation_data.nla_tracks.new()
            track.name = self.nla_track_name

        insert = 0
        for s in track.strips:
            insert = max(insert, int(s.frame_end) + 1)
        try:
            strip = track.strips.new(action.name, insert, action)
            bind_strip_action_slot(strip, resolve_action_slot(action, target=armature, ensure=True))
            strip.blend_type = 'COMBINE'
        except Exception:
            armature.animation_data.action = action
            if hasattr(armature.animation_data, "action_slot"):
                slot = resolve_action_slot(action, target=armature, ensure=True)
                if slot is not None:
                    armature.animation_data.action_slot = slot

        self.report({'INFO'},
                    f"Imported {len(all_morphs)} morphs x {len(normalised)} frames")

        # Phoneme approximation
        if self.recreate_phonemes:
            self._approximate_phonemes(context, armature, base)

        return {'FINISHED'}

    def _approximate_phonemes(self, context, armature, voice_id):
        try:
            from .ui_voice import _recreate_phonemes_from_lipsync
            ok = _recreate_phonemes_from_lipsync(
                context, armature, voice_id,
                track_name=self.nla_track_name)
            if ok:
                self.report({'INFO'}, "Phoneme approximation applied")
            else:
                self.report({'WARNING'}, "Phoneme approximation failed")
        except Exception as e:
            log.warning("Phoneme approximation error: %s", e)


# ---------------------------------------------------------------------------
#  Export w3_face_poses -> .re
# ---------------------------------------------------------------------------

class WITCH_OT_ExportREMimic(bpy.types.Operator, ExportHelper):
    """Export w3_face_poses morph animation as .re file (Witcher 3 Tools)"""
    bl_idname = "witcher.export_re_mimic"
    bl_label = "W3 Tools: Export .re Mimic"
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = '.re'
    filter_glob: StringProperty(default='*.re', options={'HIDDEN'})

    anim_length: FloatProperty(
        name='Animation Length (s)',
        description='0 = auto from scene frames',
        min=0.0, default=0.0,
    )

    @classmethod
    def poll(cls, context):
        try:
            return _is_re_plugin_available() and _is_main_armature(context.active_object)
        except Exception:
            return False

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    # ---- helpers ----

    def _create_temp_mesh(self, morph_names, morph_samples, frame_start):
        """Create a throwaway single-vertex mesh with animated shape keys.

        IMPORTANT: The RE plugin's fill_shape_key_mimic_data() always reads
        frames starting at 0 (it does ``f -= scene.frame_start``), so we
        must keyframe starting at frame 0 regardless of scene.frame_start.

        Uses keyframe_insert() instead of manual FCurve creation to ensure
        proper action-slot binding in Blender 4.x.
        """
        name = "_w3_re_tmp_mesh"
        md = bpy.data.meshes.new(name)
        md.from_pydata([(0, 0, 0)], [], [])
        md.update()
        obj = bpy.data.objects.new(name, md)
        bpy.context.scene.collection.objects.link(obj)

        obj.shape_key_add(name="Basis")
        for mn in morph_names:
            obj.shape_key_add(name=mn).value = 0.0

        # Use keyframe_insert to let Blender handle action/slot creation.
        # This is critical in Blender 4.x where manual FCurve creation
        # does not automatically bind the action slot, leaving shape key
        # values at 0 when scene.frame_set() is called.
        num_frames = len(next(iter(morph_samples.values())))
        key_blocks = obj.data.shape_keys.key_blocks
        for f_idx in range(num_frames):
            for mn in morph_names:
                kb = key_blocks[mn]
                kb.value = morph_samples[mn][f_idx]
                kb.keyframe_insert(data_path='value', frame=f_idx)

        # Verify a few values were written
        nonzero = sum(1 for mn in morph_names for v in morph_samples[mn] if abs(v) > 1e-6)
        log.info("Temp mesh: %d morphs, %d frames, %d non-zero samples",
                 len(morph_names), num_frames, nonzero)
        return obj

    def _create_temp_armature(self, src_armature):
        """Create a 'head_anim' armature with head/neck bones."""
        name = "head_anim"
        ad = bpy.data.armatures.new(name)
        obj = bpy.data.objects.new(name, ad)
        bpy.context.scene.collection.objects.link(obj)

        # Deselect everything, select our new armature
        for o in bpy.context.view_layer.objects.selected:
            o.select_set(False)
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj

        def _add_bones():
            root = ad.edit_bones.new("Root")
            root.head = (0, 0, 0)
            root.tail = (0, 0.01, 0)

            src_bones = src_armature.data.bones

            def _find_src(names):
                for name in names:
                    bone = src_bones.get(name)
                    if bone:
                        return bone
                return None

            head_src = _find_src(("head", "Head", "head_g", "Head_g"))
            neck_src = _find_src(("neck", "Neck", "neck_g", "Neck_g"))

            if not head_src:
                head_src = src_bones.get(CONTROL_BONE)
            if not neck_src and head_src and head_src.parent:
                neck_src = head_src.parent

            neck_bone = None
            if neck_src:
                neck_bone = ad.edit_bones.new(neck_src.name)
                neck_bone.head = neck_src.head_local.copy()
                neck_bone.tail = neck_src.tail_local.copy()
                neck_bone.parent = root

            if head_src:
                head_bone = ad.edit_bones.new(head_src.name)
                head_bone.head = head_src.head_local.copy()
                head_bone.tail = head_src.tail_local.copy()
                head_bone.parent = neck_bone or root

            if not head_src and not neck_src:
                for bn in ("torso3", "l_shoulder"):
                    eb = ad.edit_bones.new(bn)
                    eb.head = (0, 0, 0)
                    eb.tail = (0, 0.01, 0)
                    eb.parent = root

        ov = _find_3d_override()
        if ov:
            with bpy.context.temp_override(**ov):
                try:
                    bpy.ops.object.mode_set(mode='EDIT')
                    _add_bones()
                finally:
                    try:
                        bpy.ops.object.mode_set(mode='OBJECT')
                    except Exception:
                        pass
        else:
            try:
                bpy.ops.object.mode_set(mode='EDIT')
                _add_bones()
            finally:
                try:
                    bpy.ops.object.mode_set(mode='OBJECT')
                except Exception:
                    pass

        return obj

    def _cleanup(self, context, temp_mesh, temp_arm, created_arm, armature):
        """Remove temporary objects, restore selection. Always safe to call."""
        _ensure_object_mode(context)

        try:
            for o in bpy.context.view_layer.objects.selected:
                o.select_set(False)
        except Exception:
            pass

        # Remove temp mesh
        if temp_mesh:
            try:
                sk = getattr(temp_mesh.data, 'shape_keys', None)
                if sk and sk.animation_data and sk.animation_data.action:
                    act = sk.animation_data.action
                    sk.animation_data.action = None
                    if act.users == 0:
                        bpy.data.actions.remove(act)
                md = temp_mesh.data
                bpy.data.objects.remove(temp_mesh, do_unlink=True)
                if md and md.users == 0:
                    bpy.data.meshes.remove(md)
            except Exception as e:
                log.warning("Cleanup temp mesh error: %s", e)

        # Remove temp armature (only if we created it)
        if created_arm and temp_arm:
            try:
                ad = temp_arm.data
                bpy.data.objects.remove(temp_arm, do_unlink=True)
                if ad and ad.users == 0:
                    bpy.data.armatures.remove(ad)
            except Exception as e:
                log.warning("Cleanup temp armature error: %s", e)

        # Restore original armature as active
        if armature:
            try:
                armature.select_set(True)
                bpy.context.view_layer.objects.active = armature
            except Exception:
                pass

    # ---- main ----

    def execute(self, context):
        armature = context.active_object
        if not _is_main_armature(armature):
            self.report({'ERROR'}, "Active object must be the main armature")
            return {'CANCELLED'}

        pose_bone = armature.pose.bones[CONTROL_BONE]
        scene = context.scene

        morph_names = _get_morph_names(pose_bone)
        if not morph_names:
            self.report({'ERROR'}, "No morphs on w3_face_poses")
            return {'CANCELLED'}

        fs, fe = scene.frame_start, scene.frame_end
        if fe < fs:
            self.report({'ERROR'}, "Invalid frame range")
            return {'CANCELLED'}

        # Make sure we're in object mode before we start
        _ensure_object_mode(context)

        # Sample morph values per frame.
        # Use the same simple approach as ui_voice._sample_morph_values:
        # scene.frame_set() evaluates all animation (actions, NLA, drivers)
        # and custom properties on pose bones are updated in-place.
        prev = scene.frame_current
        samples = {m: [] for m in morph_names}
        for f in range(fs, fe + 1):
            scene.frame_set(f)
            for m in morph_names:
                val = float(pose_bone[m])
                samples[m].append(val)
        scene.frame_set(prev)

        # Diagnostic: check if we actually sampled non-zero values
        nonzero = sum(1 for m in morph_names for v in samples[m] if abs(v) > 1e-6)
        log.info("Sampled %d morphs x %d frames: %d non-zero values",
                 len(morph_names), fe - fs + 1, nonzero)
        if nonzero == 0:
            log.warning("All sampled morph values are zero! Check that "
                        "w3_face_poses has animated custom properties in "
                        "the current frame range (%d-%d).", fs, fe)

        existing_arm = bpy.data.objects.get("head_anim")
        created_arm = existing_arm is None
        temp_mesh = None
        temp_arm = None
        orig_read = None

        try:
            resolved_path = _resolve_filepath(self.filepath)
            if not resolved_path:
                self.report({'ERROR'}, "Invalid export file path")
                return {'CANCELLED'}
            root, ext = os.path.splitext(resolved_path)
            if ext.lower() != '.re':
                resolved_path = root + '.re'

            temp_mesh = self._create_temp_mesh(morph_names, samples, fs)

            if created_arm:
                temp_arm = self._create_temp_armature(armature)
            else:
                temp_arm = existing_arm

            # Select the temp mesh for the RE plugin
            for o in bpy.context.view_layer.objects.selected:
                o.select_set(False)
            temp_mesh.select_set(True)
            bpy.context.view_layer.objects.active = temp_mesh

            al = self.anim_length
            if al <= 0.0:
                al = (fe - fs + 1) / (scene.render.fps or 30)

            # Call RE plugin's export with mimic context override
            orig_read = _patch_re_phoneme_headers(morph_names)
            override = {'is_exporting_mimic': ''}
            if not _has_view_3d_context(context):
                ov = _find_3d_override()
                if not ov:
                    self.report({'ERROR'}, "No 3D Viewport available for export")
                    return {'CANCELLED'}
                override.update(ov)
            with bpy.context.temp_override(**override):
                bpy.ops.export_animset.re(
                    'EXEC_DEFAULT',
                    filepath=resolved_path,
                    context_obj_name="head_anim",
                    orig_obj_name=temp_mesh.name,
                    rotate_imported_object=False,
                    anim_length=al,
                    create_root_bone=False,
                )

            self.report({'INFO'},
                        f"Exported {len(morph_names)} morphs to "
                        f"{os.path.basename(resolved_path)}")
        except Exception as e:
            log.error("RE mimic export failed: %s", e, exc_info=True)
            self.report({'ERROR'}, f"Export failed: {e}")
            return {'CANCELLED'}
        finally:
            _restore_re_phoneme_headers(orig_read)
            self._cleanup(context, temp_mesh, temp_arm, created_arm, armature)

        return {'FINISHED'}


# ---------------------------------------------------------------------------
#  Context Menus
# ---------------------------------------------------------------------------

def _draw_viewport_menu(self, context):
    """3D viewport right-click -- only for main armature."""
    if not _is_main_armature(context.active_object):
        return
    if not _is_re_plugin_available():
        return
    layout = self.layout
    prev_ctx = layout.operator_context
    layout.operator_context = 'INVOKE_DEFAULT'
    layout.separator()
    layout.operator(WITCH_OT_ImportREMimic.bl_idname,
                    text="W3 Tools: Import .re Mimic", icon='IMPORT')
    layout.operator(WITCH_OT_ExportREMimic.bl_idname,
                    text="W3 Tools: Export .re Mimic", icon='EXPORT')
    layout.operator_context = prev_ctx


def _draw_outliner_menu(self, context):
    """Outliner right-click -- show RE mimic import/export when available."""
    if not _is_re_plugin_available():
        return
    layout = self.layout
    prev_ctx = layout.operator_context
    layout.operator_context = 'INVOKE_DEFAULT'
    layout.separator()
    layout.operator(WITCH_OT_ImportREMimic.bl_idname,
                    text="W3 Tools: Import .re Mimic", icon='IMPORT')
    layout.operator(WITCH_OT_ExportREMimic.bl_idname,
                    text="W3 Tools: Export .re Mimic", icon='EXPORT')
    layout.operator_context = prev_ctx


# ---------------------------------------------------------------------------
#  Registration
# ---------------------------------------------------------------------------

_classes = [
    WITCH_OT_ImportREMimic,
    WITCH_OT_ExportREMimic,
]


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    _patch_re_plugin_selected_ids()
    bpy.types.VIEW3D_MT_object_context_menu.append(_draw_viewport_menu)
    bpy.types.OUTLINER_MT_object.append(_draw_outliner_menu)


def unregister():
    bpy.types.OUTLINER_MT_object.remove(_draw_outliner_menu)
    bpy.types.VIEW3D_MT_object_context_menu.remove(_draw_viewport_menu)
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
