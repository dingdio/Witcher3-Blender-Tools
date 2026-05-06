import logging
import os
import math
from pathlib import Path
from mathutils import Quaternion as MQuaternion, Vector as MVector, Euler as MEuler, Matrix as MMatrix
from ..CR2W.common_blender import repo_file
from ..external_addon_tools import get_re_addon_status
log = logging.getLogger(__name__)
from .. import fbx_util, file_helpers
from .. import get_uncook_path
from .. import get_W3_VOICE_PATH
from .. import get_W3_OGG_PATH
from .. import get_rig_rot90_enabled
from .. import get_all_addon_prefs
from ..importers import import_anims, import_cutscene, import_entity, import_rig
from ..exporters import export_anims, export_cutscene
from ..action_compat import (
    assign_action,
    bind_strip_action_slot,
    iter_action_fcurves,
    new_action_fcurve,
    remove_action_fcurve,
    resolve_action_slot,
)
# from io_import_w2l.importers import import_cutscene
# from io_import_w2l.importers import import_scene
from ..ui.ui_utils import WITCH_PT_Base
from ..ui.ui_anims_list import load_anim_into_scene, resolve_animation_load_context
from ..ui.armature_context import (
    get_main_armature,
    set_main_armature,
)
from ..camera_tracks import (
    CAMERA_DOF_TRACK_NAMES,
    CAMERA_CONTROL_BONE,
    CAMERA_EDIT_BONE,
    CAMERA_SENSOR_HEIGHT,
    CAMERA_TRACK_NAMES,
    ensure_camera_track_properties,
    find_camera_preview_object,
    fov_to_lens,
    set_camera_dof_from_blender_camera,
    set_camera_dof_from_distance,
    setup_camera_preview_drivers,
)


import bpy


SCRATCH_CAMERA_DEFAULT_REPO_PATH = "gameplay\\camera\\scene_camera.w2ent"
SCRATCH_CUTSCENE_TRACK_NAME = "cutscene_anim"
SCRATCH_CUTSCENE_FACE_TRACK_NAME = "cutscene_anim_face"
SCRATCH_CUTSCENE_ROOT_COMPONENT = "Root"


def _find_character_armature(context):
    return get_main_armature(
        context,
        prefer_active=True,
        remember=True,
        fallback=True,
        allow_auxiliary_active=True,
    )


def _format_action_source_label(source):
    labels = {
        "NLA_PLAYING": "NLA (Playing)",
        "NLA_LAST": "NLA (Last Strip)",
        "ACTION_SLOT": "Action Slot",
        "NLA_FALLBACK": "NLA (Fallback)",
        "NLA_LAST_FALLBACK": "NLA (Last Strip, Fallback)",
        "ACTION_FALLBACK": "Action Slot (Fallback)",
        "NONE": "None",
    }
    return labels.get(source, source or "Unknown")


def _short_panel_header_text(text, max_len=28):
    value = str(text or "").strip()
    if not value:
        return ""
    return value if len(value) <= max_len else (value[: max_len - 1] + "…")


def _get_animation_panel_header_status(context):
    arm_obj = _find_character_armature(context)
    if not arm_obj:
        return "No target"

    try:
        scene = getattr(context, "scene", None)
        frame = getattr(scene, "frame_current", 0) if scene else 0
        nla_now, _ = export_anims.get_nla_action_at_frame(arm_obj, frame=frame)
        if nla_now:
            return _short_panel_header_text(getattr(nla_now, "name", "NLA"))
        nla_last, _ = export_anims.get_nla_last_action(arm_obj, prefer_tracks=("anim_import",))
        if nla_last:
            return _short_panel_header_text(getattr(nla_last, "name", "NLA"))
        action_slot = export_anims.get_action_slot(arm_obj)
        if action_slot:
            return _short_panel_header_text(getattr(action_slot, "name", "Action"))
    except Exception:
        pass

    return _short_panel_header_text(getattr(arm_obj, "name", "Animation"))


def _animset_compare_key(path_value):
    """Normalize animset paths for UI matching (handles .w2anims vs .w2anims.json)."""
    raw = str(path_value or "").strip()
    if not raw:
        return ""
    normalized = os.path.normpath(raw.replace("/", os.sep).replace("\\", os.sep))
    if normalized.lower().endswith(".json"):
        normalized = normalized[:-5]
    return os.path.normcase(normalized)


def _animset_repo_compare_key(context, repo_rel_path):
    repo_rel = str(repo_rel_path or "").strip()
    if not repo_rel or ":" in repo_rel:
        return ""
    abs_path = os.path.join(get_uncook_path(context), repo_rel.replace("/", os.sep).replace("\\", os.sep))
    return _animset_compare_key(abs_path)


def _resolve_root_orientation_action(armature_obj):
    if not armature_obj or armature_obj.type != 'ARMATURE' or not armature_obj.animation_data:
        return None

    action = None
    if armature_obj.animation_data.nla_tracks:
        anim_import_track = armature_obj.animation_data.nla_tracks.get('anim_import')
        if anim_import_track and anim_import_track.strips:
            for strip in reversed(anim_import_track.strips):
                if strip.action:
                    action = strip.action
                    break

    if action is None:
        action = armature_obj.animation_data.action

    if action is None and armature_obj.animation_data.nla_tracks:
        for track in armature_obj.animation_data.nla_tracks:
            for strip in track.strips:
                if strip.action:
                    action = strip.action
                    break
            if action:
                break

    return action


def _get_loaded_animset_ui_state(context):
    scene = getattr(context, "scene", None)
    if scene is None:
        return {
            "loaded_path": "",
            "loaded_key": "",
            "source_tag": "",
            "source_badge": "",
            "display_name": "",
            "display_path": "",
            "clip_count": 0,
            "has_loaded_set": False,
        }

    loaded_path = str(getattr(scene, "witcher_loaded_w2anims_path", "") or "").strip()
    loaded_key = _animset_compare_key(loaded_path)
    source_tag = str(getattr(scene, "witcher_loaded_w2anims_source_tag", "") or "").strip().upper()
    loaded_path_no_json = loaded_path[:-5] if loaded_path.lower().endswith(".json") else loaded_path

    display_name = ""
    display_path = ""
    if loaded_path_no_json:
        display_name = os.path.basename(loaded_path_no_json.replace("\\", "/"))
        try:
            uncook_root = os.path.normpath(get_uncook_path(context))
            rel_path = os.path.relpath(os.path.normpath(loaded_path_no_json), uncook_root)
            if not rel_path.startswith(".."):
                display_path = rel_path.replace("\\", "/")
        except Exception:
            pass

    clip_count = len(getattr(scene, "witcher_w2anims_list", []))
    has_loaded_set = bool(loaded_key or clip_count)
    if not display_name and has_loaded_set:
        display_name = "In-memory animation set"

    source_badge = {
        "W2": "W2",
        "W3": "W3",
        "JSON": "JSON",
        "FILE": "FILE",
        "MEMORY": "MEM",
    }.get(source_tag, source_tag or "")

    return {
        "loaded_path": loaded_path,
        "loaded_key": loaded_key,
        "source_tag": source_tag,
        "source_badge": source_badge,
        "display_name": display_name,
        "display_path": display_path,
        "clip_count": clip_count,
        "has_loaded_set": has_loaded_set,
    }


def _get_selected_collection_item(owner, collection_name, index_name):
    collection = getattr(owner, collection_name, None)
    if collection is None:
        return None, -1
    try:
        item_count = len(collection)
    except Exception:
        return None, -1
    if item_count <= 0:
        return None, -1

    current_index = int(getattr(owner, index_name, -1))
    safe_index = max(0, min(current_index, item_count - 1))
    return collection[safe_index], safe_index


_NLA_MODE_MAP = {'REPLACE': 'replace', 'APPEND': 'append', 'APPEND_AT_CURSOR': 'append_at_cursor'}

def _scene_nla_mode(scene):
    return _NLA_MODE_MAP.get(getattr(scene, 'witcher_anim_nla_mode', 'REPLACE'), 'replace')


def on_anim_list_index_changed(self, context):
    """Callback when animation list selection changes. Auto-loads if enabled."""
    if not getattr(context.scene, 'witcher_load_anim_on_select', False):
        return

    scene = context.scene
    item, _safe_index = _get_selected_collection_item(
        scene,
        "witcher_w2anims_list",
        "witcher_w2anims_list_index",
    )
    if item is None:
        return

    main_arm_obj = _find_character_armature(context)

    if not main_arm_obj:
        return

    anim_name = item.name
    fdir_abs = context.scene.witcher_loaded_w2anims_path

    if not fdir_abs:
        return

    try:
        _nla_mode = _scene_nla_mode(context.scene)
        load_anim_into_scene(context, anim_name, fdir_abs, main_arm_obj, nla_mode=_nla_mode)
        # Apply root orientation if enabled
        auto_orient = getattr(context.scene, 'witcher_auto_orient_root', False)
        log.info(f"[on_select] Auto orient root setting: {auto_orient}")
        if auto_orient:
            apply_root_orientation(main_arm_obj)
    except FileNotFoundError as e:
        log.error(f"Auto-load animation failed: {e}")
        def _draw_error(self_op, ctx):
            self_op.layout.label(text=str(e))
        context.window_manager.popup_menu(_draw_error, title="Missing Buffer File", icon='ERROR')
    except Exception as e:
        log.error(f"Auto-load animation failed: {e}")
from bpy.types import Panel, Operator, UIList, PropertyGroup
from bpy.props import IntProperty, StringProperty, CollectionProperty, FloatProperty, FloatVectorProperty, BoolProperty, EnumProperty
from bpy_extras.io_utils import (
        ImportHelper,
        ExportHelper
        )


class ListItem(PropertyGroup):
    """Group of properties representing an item in the list."""

    name: StringProperty(
           name="Name",
           description="Name of the animation",
           default="Untitled")
    framesPerSecond: FloatProperty(
           name="Frames Per Second",
           description="",
           default=0)
    numFrames: IntProperty(
           name="Num Frames",
           description="",
           default=0)
    duration: FloatProperty(
           name="Duration",
           description="",
           default=0)
    SkeletalAnimationType: StringProperty(
           name="SkeletalAnimationType",
           description="",
           default="SAT_Normal")
    AdditiveType: StringProperty(
           name="AdditiveType",
           description="",
           default="")
    RootMotion: BoolProperty(
        name="Root Motion",
        default=False,
        options=set(),
        description="",
    )

    # jsonData: StringProperty(
    #        name="Animation in Json",
    #        description="",
    #        default="")

class W3AnimExportEntry(PropertyGroup):
    """One entry in the multi-animation export set."""
    action_name: StringProperty(
        name="Action",
        description="Name of the Blender action to export",
        default="",
    )
    enabled: BoolProperty(
        name="Enabled",
        description="Include this entry in the next multi-export",
        default=True,
    )
    skeletal_anim_type: EnumProperty(
        name="Animation Type",
        items=[
            ('SAT_Normal', "Normal", "Standard skeletal animation"),
            ('SAT_Additive', "Additive", "Additive skeletal animation"),
            ('SAT_MS', "MS", "Motion-sampled animation"),
        ],
        default='SAT_Normal',
    )
    additive_type: EnumProperty(
        name="Additive Type",
        items=[
            ('NONE', "None", "No additive type"),
            ('AT_Local', "Local", "Local additive"),
            ('AT_Ref', "Ref", "Reference additive"),
            ('AT_TPose', "T-Pose", "T-Pose additive"),
            ('AT_Animation', "Animation", "Animation additive"),
        ],
        default='NONE',
    )
    include_motion_extraction: BoolProperty(
        name="Include Motion Extraction",
        description="Generate motion extraction from Trajectory bone",
        default=False,
    )


class WITCH_UL_AnimExportSet(UIList):
    bl_idname = "WITCH_UL_AnimExportSet"

    def draw_item(self, context, layout, data, item, icon,
                  active_data, active_propname, index, flt_flag):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            row.prop(item, "enabled", text="")
            action_exists = item.action_name in bpy.data.actions
            icon_id = 'ACTION' if action_exists else 'ERROR'
            row.label(text=item.action_name or "<empty>", icon=icon_id)
            type_badges = {'SAT_Additive': 'Add', 'SAT_MS': 'MS'}
            badge = type_badges.get(item.skeletal_anim_type, '')
            if badge:
                row.label(text=badge)
        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text="", icon='ACTION')


class WITCH_OT_AnimExportSetAdd(bpy.types.Operator):
    """Add the currently resolved action to the export set"""
    bl_idname = "witcher.anim_export_set_add"
    bl_label = "Add Current"

    @classmethod
    def poll(cls, context):
        return export_anims.get_selected_armature(context) is not None

    def execute(self, context):
        armature = export_anims.get_selected_armature(context)
        source_mode = getattr(context.scene, "witcher_w3_anim_source", "NLA")
        action, _ = export_anims.resolve_action(armature, context=context, source_mode=source_mode)
        if action is None:
            self.report({'WARNING'}, "No action resolved; nothing added.")
            return {'CANCELLED'}

        for entry in context.scene.witcher_anim_export_set:
            if entry.action_name == action.name:
                self.report({'INFO'}, f"'{action.name}' is already in the export set.")
                return {'CANCELLED'}

        entry = context.scene.witcher_anim_export_set.add()
        entry.action_name = action.name
        entry.enabled = True
        context.scene.witcher_anim_export_set_index = len(context.scene.witcher_anim_export_set) - 1
        return {'FINISHED'}


class WITCH_OT_AnimExportSetRemove(bpy.types.Operator):
    """Remove the selected entry from the export set"""
    bl_idname = "witcher.anim_export_set_remove"
    bl_label = "Remove"

    @classmethod
    def poll(cls, context):
        return (context.scene
                and getattr(context.scene, "witcher_anim_export_set", None)
                and len(context.scene.witcher_anim_export_set) > 0)

    def execute(self, context):
        lst = context.scene.witcher_anim_export_set
        idx = context.scene.witcher_anim_export_set_index
        lst.remove(idx)
        context.scene.witcher_anim_export_set_index = max(0, min(idx, len(lst) - 1))
        return {'FINISHED'}


def _camera_bone_world_position(armature_obj, bone_name=CAMERA_CONTROL_BONE):
    if armature_obj is None or getattr(armature_obj, "type", None) != 'ARMATURE':
        return None
    pose_bones = getattr(getattr(armature_obj, "pose", None), "bones", None)
    pose_bone = pose_bones.get(bone_name) if pose_bones else None
    if pose_bone is None:
        return None
    return (armature_obj.matrix_world @ pose_bone.matrix).translation


def _selected_camera_dof_target_position(context, armature_obj):
    active_pose_bone = getattr(context, "active_pose_bone", None)
    active_obj = getattr(context, "object", None)
    if active_pose_bone is not None and active_obj is not None:
        return (active_obj.matrix_world @ active_pose_bone.matrix).translation

    selected = list(getattr(context, "selected_objects", []) or [])
    active_selected = getattr(context, "active_object", None)
    ordered = []
    if active_selected in selected:
        ordered.append(active_selected)
    ordered.extend(obj for obj in selected if obj is not active_selected)
    for obj in ordered:
        if obj is None or obj is armature_obj or getattr(obj, "type", None) == 'CAMERA':
            continue
        return obj.matrix_world.translation
    return None


def _is_cutscene_nla_track_name(track_name):
    track_name = str(track_name or "")
    return track_name == "cutscene_anim" or track_name.startswith("cutscene_anim")


CAMERA_CUT_MARKER_PREFIX = "W3 Cam "


def _is_camera_armature(obj):
    return (
        obj is not None
        and getattr(obj, "type", None) == 'ARMATURE'
        and getattr(getattr(obj, "pose", None), "bones", {}).get(CAMERA_CONTROL_BONE) is not None
    )


def _find_camera_armature(context):
    active_obj = getattr(context, "active_object", None)
    if _is_camera_armature(active_obj):
        return active_obj
    parent_obj = getattr(active_obj, "parent", None)
    if _is_camera_armature(parent_obj):
        return parent_obj

    display_armature = _find_character_armature(context)
    if _is_camera_armature(display_armature):
        return display_armature

    for obj in getattr(context, "selected_objects", []) or []:
        if _is_camera_armature(obj):
            return obj
        parent_obj = getattr(obj, "parent", None)
        if _is_camera_armature(parent_obj):
            return parent_obj

    scene = getattr(context, "scene", None)
    for obj in getattr(scene, "objects", []) or []:
        if not _is_camera_armature(obj):
            continue
        actor_type = str(obj.get("cutscene_actor_type", "") or "")
        actor_name = str(obj.get("cutscene_actor_name", "") or "").lower()
        if "CAT_Camera" in actor_type or actor_name == "camera":
            return obj
    return None


def _iter_camera_cut_strips(armature_obj):
    anim_data = getattr(armature_obj, "animation_data", None)
    if anim_data is None:
        return []

    cuts = []
    for track in getattr(anim_data, "nla_tracks", []) or []:
        if getattr(track, "mute", False):
            continue
        track_name = str(getattr(track, "name", "") or "")
        if not _is_cutscene_nla_track_name(track_name):
            continue
        for strip in getattr(track, "strips", []) or []:
            if getattr(strip, "mute", False):
                continue
            cuts.append((track, strip))
    cuts.sort(key=lambda item: (float(item[1].frame_start), float(item[1].frame_end), item[1].name))
    return cuts


def _current_camera_cut_index(context, armature_obj, cuts=None):
    cuts = cuts if cuts is not None else _iter_camera_cut_strips(armature_obj)
    if not cuts:
        return -1

    frame = float(getattr(getattr(context, "scene", None), "frame_current", 0.0))
    for idx, (_track, strip) in enumerate(cuts):
        if float(strip.frame_start) <= frame <= float(strip.frame_end):
            return idx

    previous = -1
    for idx, (_track, strip) in enumerate(cuts):
        if float(strip.frame_start) <= frame:
            previous = idx
        else:
            break
    return previous if previous >= 0 else 0


def _select_nla_strip(track, strip):
    try:
        for other in track.strips:
            other.select = False
        strip.select = True
    except Exception:
        pass


def _camera_cut_marker_name(index):
    return f"{CAMERA_CUT_MARKER_PREFIX}{int(index) + 1:02d}"


def _is_camera_cut_marker(marker):
    return str(getattr(marker, "name", "") or "").startswith(CAMERA_CUT_MARKER_PREFIX)


def _sync_camera_cut_markers(scene, armature_obj):
    if scene is None or armature_obj is None:
        return 0

    markers = getattr(scene, "timeline_markers", None)
    if markers is None:
        return 0

    for marker in list(markers):
        if _is_camera_cut_marker(marker):
            markers.remove(marker)

    camera_obj = find_camera_preview_object(armature_obj)
    cuts = _iter_camera_cut_strips(armature_obj)
    for idx, (_track, strip) in enumerate(cuts):
        marker = markers.new(_camera_cut_marker_name(idx), frame=int(round(float(strip.frame_start))))
        try:
            marker.camera = camera_obj
        except Exception:
            pass
    return len(cuts)


def _apply_camera_cut_markers_to_strips(scene, armature_obj):
    if scene is None or armature_obj is None:
        return 0

    cuts = _iter_camera_cut_strips(armature_obj)
    markers = sorted(
        [marker for marker in getattr(scene, "timeline_markers", []) if _is_camera_cut_marker(marker)],
        key=lambda marker: int(getattr(marker, "frame", 0)),
    )
    if not cuts or len(markers) != len(cuts):
        return 0

    for idx, ((track, strip), marker) in enumerate(zip(cuts, markers)):
        old_start = float(strip.frame_start)
        old_end = float(strip.frame_end)
        old_length = max(1.0, old_end - old_start)
        new_start = float(getattr(marker, "frame", old_start))
        if idx + 1 < len(markers):
            new_end = max(new_start + 1.0, float(getattr(markers[idx + 1], "frame", new_start + old_length)))
        else:
            new_end = new_start + old_length
        strip.frame_start = new_start
        strip.frame_end = new_end
        _select_nla_strip(track, strip)
    return len(cuts)


def _iter_scene_cutscene_strips(scene):
    for obj in getattr(scene, "objects", []) or []:
        anim_data = getattr(obj, "animation_data", None)
        if anim_data is None:
            continue
        for track in getattr(anim_data, "nla_tracks", []) or []:
            if getattr(track, "mute", False):
                continue
            if not _is_cutscene_nla_track_name(getattr(track, "name", "")):
                continue
            for strip in getattr(track, "strips", []) or []:
                if not getattr(strip, "mute", False):
                    yield obj, track, strip


def _move_cutscene_strips_after(scene, boundary, delta, skip_strip=None):
    if abs(float(delta)) <= 1e-6:
        return
    boundary = float(boundary)
    for _obj, _track, strip in _iter_scene_cutscene_strips(scene):
        if strip == skip_strip:
            continue
        if float(strip.frame_start) >= boundary - 1e-6:
            strip.frame_start = float(strip.frame_start) + float(delta)
            strip.frame_end = float(strip.frame_end) + float(delta)


def _linearize_action_keys_near_frame(action, frame, radius=2.0, target=None):
    if action is None:
        return 0
    frame = float(frame)
    radius = max(0.0, float(radius))
    changed = 0
    for fcurve in iter_action_fcurves(action, target=target):
        for key in getattr(fcurve, "keyframe_points", []) or []:
            try:
                if abs(float(key.co.x) - frame) <= radius:
                    key.interpolation = 'LINEAR'
                    changed += 1
            except Exception:
                continue
    return changed


def _pose_bone_world_matrix(armature_obj, bone_name):
    pose_bone = getattr(getattr(armature_obj, "pose", None), "bones", {}).get(bone_name)
    if pose_bone is None:
        return None
    return armature_obj.matrix_world @ pose_bone.matrix


def _set_pose_bone_world_matrix(armature_obj, bone_name, world_matrix):
    pose_bone = getattr(getattr(armature_obj, "pose", None), "bones", {}).get(bone_name)
    if pose_bone is None:
        return None
    pose_bone.matrix = armature_obj.matrix_world.inverted() @ world_matrix
    return pose_bone


def _resolve_native_camera_target(context, camera_armature):
    active_obj = getattr(context, "active_object", None)
    if getattr(active_obj, "type", None) == 'CAMERA':
        return active_obj
    scene_camera = getattr(getattr(context, "scene", None), "camera", None)
    if getattr(scene_camera, "type", None) == 'CAMERA':
        return scene_camera
    return find_camera_preview_object(camera_armature)


def _fcurve_insert_direct(action, armature_obj, data_path, frame, values):
    """Insert keyframe points directly into an action's fcurves (works in Blender 5.0+)."""
    for i, v in enumerate(values):
        fc = None
        for existing in iter_action_fcurves(action, target=armature_obj):
            if existing.data_path == data_path and existing.array_index == i:
                fc = existing
                break
        if fc is None:
            try:
                fc = new_action_fcurve(action, armature_obj, data_path, index=i)
            except Exception:
                continue
        if fc is not None:
            try:
                fc.keyframe_points.insert(float(frame), float(v), options={'FAST'})
            except Exception:
                pass


def _insert_rig_keyframe_to_action(action, armature_obj, edit_bone, camera_bone, frame):
    """Write one keyframe of rig pose data directly into an action without switching anim_data.action."""
    if edit_bone is not None:
        bp = f'pose.bones["{edit_bone.name}"]'
        loc = edit_bone.location
        _fcurve_insert_direct(action, armature_obj, f"{bp}.location", frame, [loc.x, loc.y, loc.z])
        if edit_bone.rotation_mode == 'QUATERNION':
            q = edit_bone.rotation_quaternion
            _fcurve_insert_direct(action, armature_obj, f"{bp}.rotation_quaternion", frame, [q.w, q.x, q.y, q.z])
        elif edit_bone.rotation_mode == 'AXIS_ANGLE':
            _fcurve_insert_direct(action, armature_obj, f"{bp}.rotation_axis_angle", frame, list(edit_bone.rotation_axis_angle))
        else:
            e = edit_bone.rotation_euler
            _fcurve_insert_direct(action, armature_obj, f"{bp}.rotation_euler", frame, [e.x, e.y, e.z])
        sc = edit_bone.scale
        _fcurve_insert_direct(action, armature_obj, f"{bp}.scale", frame, [sc.x, sc.y, sc.z])
    if camera_bone is not None:
        cp = f'pose.bones["{camera_bone.name}"]'
        if "hctFOV" in camera_bone:
            _fcurve_insert_direct(action, armature_obj, f'{cp}["hctFOV"]', frame, [float(camera_bone["hctFOV"])])
        for track_name in CAMERA_DOF_TRACK_NAMES:
            if track_name in camera_bone:
                _fcurve_insert_direct(action, armature_obj, f'{cp}["{track_name}"]', frame, [float(camera_bone[track_name])])


def _key_rig_from_camera(context, camera_armature, camera_obj, insert_key=True, key_frame=None, target_action=None):
    if camera_armature is None or camera_obj is None:
        return False

    preview_camera = find_camera_preview_object(camera_armature)
    edit_world = _pose_bone_world_matrix(camera_armature, CAMERA_EDIT_BONE)
    if preview_camera is not None and edit_world is not None:
        offset = edit_world.inverted() @ preview_camera.matrix_world
        desired_edit_world = camera_obj.matrix_world @ offset.inverted()
        edit_bone = _set_pose_bone_world_matrix(camera_armature, CAMERA_EDIT_BONE, desired_edit_world)
    else:
        edit_bone = _set_pose_bone_world_matrix(camera_armature, CAMERA_EDIT_BONE, camera_obj.matrix_world)

    camera_bone = ensure_camera_track_properties(camera_armature, track_names=CAMERA_TRACK_NAMES)
    if camera_bone is not None:
        try:
            camera_bone["hctFOV"] = math.degrees(float(camera_obj.data.angle_y))
        except Exception:
            pass
        set_camera_dof_from_blender_camera(camera_bone, camera_obj)

    if insert_key:
        frame = key_frame if key_frame is not None else getattr(getattr(context, "scene", None), "frame_current", None)
        if frame is not None:
            if target_action is not None:
                # Blender 5.0-safe path: write directly into the action's fcurves
                _insert_rig_keyframe_to_action(target_action, camera_armature, edit_bone, camera_bone, frame)
            else:
                # Standard path: keyframe_insert uses whatever action is currently active
                if edit_bone is not None:
                    edit_bone.keyframe_insert(data_path="location", frame=frame)
                    if edit_bone.rotation_mode == 'QUATERNION':
                        edit_bone.keyframe_insert(data_path="rotation_quaternion", frame=frame)
                    elif edit_bone.rotation_mode == 'AXIS_ANGLE':
                        edit_bone.keyframe_insert(data_path="rotation_axis_angle", frame=frame)
                    else:
                        edit_bone.keyframe_insert(data_path="rotation_euler", frame=frame)
                    edit_bone.keyframe_insert(data_path="scale", frame=frame)
                if camera_bone is not None:
                    camera_bone.keyframe_insert(data_path='["hctFOV"]', frame=frame)
                    for track_name in CAMERA_DOF_TRACK_NAMES:
                        camera_bone.keyframe_insert(data_path=f'["{track_name}"]', frame=frame)
    return True


def _strip_scene_frame_to_action_frame(strip, scene_frame):
    if strip is None:
        return scene_frame
    action_start = float(getattr(strip, "action_frame_start", 0.0) or 0.0)
    return action_start + float(scene_frame) - float(getattr(strip, "frame_start", 0.0) or 0.0)


def _copy_strip_settings(source_strip, target_strip):
    for attr, value in _strip_settings_snapshot(source_strip).items():
        if not hasattr(target_strip, attr):
            continue
        try:
            setattr(target_strip, attr, value)
        except Exception:
            pass


def _strip_settings_snapshot(strip):
    settings = {}
    for attr in (
        "blend_type",
        "extrapolation",
        "use_auto_blend",
        "blend_in",
        "blend_out",
        "mute",
    ):
        if not hasattr(strip, attr):
            continue
        try:
            settings[attr] = getattr(strip, attr)
        except Exception:
            pass
    return settings


def _apply_strip_settings_snapshot(settings, target_strip):
    for attr, value in dict(settings or {}).items():
        if not hasattr(target_strip, attr):
            continue
        try:
            setattr(target_strip, attr, value)
        except Exception:
            pass


def _set_strip_action_range(strip, action_start=None, action_end=None):
    if strip is None:
        return
    if action_start is not None and hasattr(strip, "action_frame_start"):
        try:
            strip.action_frame_start = float(action_start)
        except Exception:
            pass
    if action_end is not None and hasattr(strip, "action_frame_end"):
        try:
            strip.action_frame_end = float(action_end)
        except Exception:
            pass


def _selected_camera_cut_strips(camera_armature):
    selected = []
    for track, strip in _iter_camera_cut_strips(camera_armature):
        if bool(getattr(strip, "select", False)):
            selected.append((track, strip))
    return selected


def _camera_cut_selection_for_combine(context, camera_armature):
    cuts = _iter_camera_cut_strips(camera_armature)
    selected = _selected_camera_cut_strips(camera_armature)
    if len(selected) >= 2:
        return sorted(selected, key=lambda item: (float(item[1].frame_start), float(item[1].frame_end)))

    cut_index = _current_camera_cut_index(context, camera_armature, cuts)
    if cut_index < 0 or cut_index + 1 >= len(cuts):
        return []
    return [cuts[cut_index], cuts[cut_index + 1]]


def _validate_adjacent_camera_cuts(cuts):
    if len(cuts) < 2:
        return False
    previous_end = float(cuts[0][1].frame_end)
    for _track, strip in cuts[1:]:
        strip_start = float(strip.frame_start)
        if abs(strip_start - previous_end) > 1e-4:
            return False
        previous_end = float(strip.frame_end)
    return True


def _new_camera_edit_track(camera_armature):
    anim_data = camera_armature.animation_data_create()
    track = anim_data.nla_tracks.new()
    base_name = "cutscene_anim_camera_edits"
    track.name = base_name
    return track


def _create_camera_cut_strip(camera_armature, preferred_track, name, scene_start, scene_end, action,
                             action_start=None, action_end=None, settings=None):
    scene_start = float(scene_start)
    scene_end = max(scene_start + 1.0, float(scene_end))

    def _create_on_track(track):
        new_strip = track.strips.new(name, int(round(scene_start)), action)
        _apply_strip_settings_snapshot(settings or {}, new_strip)
        _set_strip_action_range(new_strip, action_start=action_start, action_end=action_end)
        new_strip.frame_start = scene_start
        new_strip.frame_end = scene_end
        bind_strip_action_slot(new_strip, resolve_action_slot(action, target=camera_armature, ensure=True))
        return track, new_strip

    try:
        return _create_on_track(preferred_track)
    except RuntimeError:
        # Blender creates new NLA strips at full action length before we can trim the
        # action range. If that overlaps neighboring cuts, create the strip on a fresh
        # camera edit track instead.
        return _create_on_track(_new_camera_edit_track(camera_armature))


def _clear_camera_preview_drivers(camera_obj):
    if camera_obj is None or getattr(camera_obj, "type", None) != 'CAMERA':
        return 0
    camera_data = getattr(camera_obj, "data", None)
    if camera_data is None:
        return 0
    removed = 0
    for data_path in ("lens", "dof.focus_distance", "dof.aperture_fstop"):
        try:
            camera_data.driver_remove(data_path)
            removed += 1
        except (TypeError, RuntimeError, ValueError):
            pass
    return removed


def _scene_edit_range(scene):
    if scene is None:
        return 0, 1
    if bool(getattr(scene, "use_preview_range", False)):
        start = int(getattr(scene, "frame_preview_start", getattr(scene, "frame_start", 0)))
        end = int(getattr(scene, "frame_preview_end", getattr(scene, "frame_end", start + 1)))
    else:
        start = int(getattr(scene, "frame_start", 0))
        end = int(getattr(scene, "frame_end", start + 1))
    if end <= start:
        end = start + 1
    return start, end


def _selected_blender_camera(context):
    active_obj = getattr(context, "active_object", None)
    if getattr(active_obj, "type", None) == 'CAMERA':
        return active_obj
    for obj in getattr(context, "selected_objects", []) or []:
        if getattr(obj, "type", None) == 'CAMERA':
            return obj
    scene_camera = getattr(getattr(context, "scene", None), "camera", None)
    if getattr(scene_camera, "type", None) == 'CAMERA':
        return scene_camera
    return None


def _handoff_preview_camera_animation_to_rig(camera_armature, camera_obj):
    if camera_armature is None or camera_obj is None:
        return
    if camera_obj != find_camera_preview_object(camera_armature):
        return
    for datablock in (camera_obj, getattr(camera_obj, "data", None)):
        anim_data = getattr(datablock, "animation_data", None)
        if anim_data is None:
            continue
        try:
            anim_data.action = None
        except Exception:
            pass
        for track in getattr(anim_data, "nla_tracks", []) or []:
            try:
                track.mute = True
            except Exception:
                pass
    setup_camera_preview_drivers(camera_armature, camera_obj)


def _normalize_scratch_repo_path(path):
    return export_anims._normalize_repo_path(path)


def _iter_object_descendants(root_obj):
    pending = list(getattr(root_obj, "children", []) or [])
    while pending:
        child = pending.pop(0)
        pending.extend(getattr(child, "children", []) or [])
        yield child


def _resolve_active_armature(context):
    active_obj = getattr(context, "active_object", None)
    armature_obj = _resolve_actor_armature_from_object(context, active_obj)
    if armature_obj is not None:
        return armature_obj
    for obj in getattr(context, "selected_objects", []) or []:
        armature_obj = _resolve_actor_armature_from_object(context, obj)
        if armature_obj is not None:
            return armature_obj
    return _find_character_armature(context)


def _object_parent_chain(obj):
    current = getattr(obj, "parent", None)
    while current is not None:
        yield current
        current = getattr(current, "parent", None)


def _armature_from_object_reference(obj):
    if obj is None:
        return None
    if getattr(obj, "type", None) == 'ARMATURE':
        return obj
    for parent in _object_parent_chain(obj):
        if getattr(parent, "type", None) == 'ARMATURE':
            return parent
    for modifier in getattr(obj, "modifiers", []) or []:
        if getattr(modifier, "type", None) != 'ARMATURE':
            continue
        target = getattr(modifier, "object", None)
        if getattr(target, "type", None) == 'ARMATURE':
            return target
    for child in _iter_object_descendants(obj):
        if getattr(child, "type", None) == 'ARMATURE':
            return child
    return None


def _actor_entity_key(obj):
    if obj is None:
        return ""
    rig_settings = getattr(getattr(obj, "data", None), "witcherui_RigSettings", None)
    for candidate in (
        getattr(rig_settings, "repo_path", "") if rig_settings else "",
        obj.get("witcher_entity_path", ""),
        obj.get("cutscene_actor_template", ""),
    ):
        candidate = _normalize_scratch_repo_path(candidate)
        if candidate:
            return candidate.lower()
    return ""


def _actor_entity_name_hint(obj):
    if obj is None:
        return ""
    rig_settings = getattr(getattr(obj, "data", None), "witcherui_RigSettings", None)
    for candidate in (
        getattr(rig_settings, "entity_name", "") if rig_settings else "",
        obj.get("witcher_entity_name", ""),
        obj.get("cutscene_actor_name", ""),
    ):
        candidate = str(candidate or "").strip()
        if candidate:
            return candidate
    obj_name = str(getattr(obj, "name", "") or "").strip()
    if ":" in obj_name:
        prefix = obj_name.split(":", 1)[0].strip()
        if prefix:
            return prefix
    return ""


def _actor_armature_score(candidate, selected_armature, selected_key, selected_entity_name):
    if candidate is None or getattr(candidate, "type", None) != 'ARMATURE':
        return -1
    score = 0
    if candidate is selected_armature:
        score += 100
    witcher_type = str(candidate.get("witcher_type", "") or "")
    candidate_name = str(getattr(candidate, "name", "") or "")
    if witcher_type == "CMovingPhysicalAgentComponent" or "CMovingPhysicalAgentComponent" in candidate_name:
        score += 80
    if getattr(getattr(candidate, "pose", None), "bones", {}).get("Trajectory") is not None:
        score += 40
    rig_settings = getattr(getattr(candidate, "data", None), "witcherui_RigSettings", None)
    if rig_settings is not None:
        if str(getattr(rig_settings, "main_entity_skeleton", "") or "").strip():
            score += 25
        if str(getattr(rig_settings, "repo_path", "") or "").strip():
            score += 20
    candidate_key = _actor_entity_key(candidate)
    if selected_key and candidate_key == selected_key:
        score += 30
    candidate_entity_name = _actor_entity_name_hint(candidate)
    if selected_entity_name and candidate_entity_name == selected_entity_name:
        score += 20
    if str(candidate.get("_w3_entity_context_role", "") or "").strip().lower() == "primary":
        score += 10
    if str(candidate.get("mimicFaceFile", "") or "").strip() and candidate is not selected_armature:
        score -= 25
    return score


def _prefer_actor_root_armature(context, selected_armature):
    if selected_armature is None:
        return None
    scene = getattr(context, "scene", None)
    if scene is None:
        return selected_armature

    for obj in getattr(scene, "objects", []) or []:
        if getattr(obj, "type", None) == 'ARMATURE' and obj.get("mimicFace") == selected_armature.name:
            return obj

    selected_key = _actor_entity_key(selected_armature)
    selected_entity_name = _actor_entity_name_hint(selected_armature)
    candidates = [selected_armature]
    for obj in getattr(scene, "objects", []) or []:
        if getattr(obj, "type", None) != 'ARMATURE' or obj is selected_armature:
            continue
        if selected_key and _actor_entity_key(obj) == selected_key:
            candidates.append(obj)
            continue
        if selected_entity_name and _actor_entity_name_hint(obj) == selected_entity_name:
            candidates.append(obj)

    return max(
        candidates,
        key=lambda obj: _actor_armature_score(obj, selected_armature, selected_key, selected_entity_name),
    )


def _resolve_actor_armature_from_object(context, obj):
    armature_obj = _armature_from_object_reference(obj)
    if armature_obj is None:
        return None
    return _prefer_actor_root_armature(context, armature_obj)


def _find_armature_in_hierarchy(root_obj, predicate=None):
    candidates = []
    if getattr(root_obj, "type", None) == 'ARMATURE':
        candidates.append(root_obj)
    candidates.extend(
        child for child in _iter_object_descendants(root_obj)
        if getattr(child, "type", None) == 'ARMATURE'
    )
    if predicate is not None:
        for candidate in candidates:
            if predicate(candidate):
                return candidate
    return candidates[0] if candidates else None


def _find_new_imported_armature(before_names, predicate=None):
    before_names = set(before_names or [])
    for obj in bpy.data.objects:
        if obj.name in before_names or getattr(obj, "type", None) != 'ARMATURE':
            continue
        if predicate is None or predicate(obj):
            return obj
    for obj in bpy.data.objects:
        if obj.name in before_names or getattr(obj, "type", None) != 'ARMATURE':
            continue
        return obj
    return None


def _resolve_actor_template_path(armature_obj, explicit_path=""):
    explicit_path = _normalize_scratch_repo_path(explicit_path)
    if explicit_path:
        return explicit_path
    if armature_obj is None:
        return ""
    for prop_name in ("cutscene_actor_template", "witcher_entity_path", "repo_path"):
        candidate = _normalize_scratch_repo_path(armature_obj.get(prop_name, ""))
        if candidate:
            return candidate
    rig_settings = getattr(getattr(armature_obj, "data", None), "witcherui_RigSettings", None)
    if rig_settings is not None:
        candidate = _normalize_scratch_repo_path(getattr(rig_settings, "repo_path", ""))
        if candidate:
            return candidate
    return ""


def _resolve_actor_display_name(armature_obj, explicit_name=""):
    explicit_name = str(explicit_name or "").strip()
    if explicit_name:
        return explicit_name
    if armature_obj is None:
        return ""
    entity_hint = _actor_entity_name_hint(armature_obj)
    if entity_hint:
        return entity_hint
    actor_name = str(armature_obj.get("cutscene_actor_name", "") or "").strip()
    if actor_name:
        return actor_name
    rig_settings = getattr(getattr(armature_obj, "data", None), "witcherui_RigSettings", None)
    if rig_settings is not None:
        entity_name = str(getattr(rig_settings, "entity_name", "") or "").strip()
        if entity_name:
            return entity_name
    obj_name = str(getattr(armature_obj, "name", "") or "").strip()
    if ":" in obj_name:
        prefix = obj_name.split(":", 1)[0].strip()
        if prefix:
            return prefix
    return obj_name


def _resolve_actor_appearance(armature_obj, explicit_appearance=""):
    explicit_appearance = str(explicit_appearance or "").strip()
    if explicit_appearance:
        return explicit_appearance
    if armature_obj is None:
        return ""
    existing = str(armature_obj.get("cutscene_actor_appearance", "") or "").strip()
    if existing:
        return existing
    rig_settings = getattr(getattr(armature_obj, "data", None), "witcherui_RigSettings", None)
    app_list = getattr(rig_settings, "app_list", None) if rig_settings is not None else None
    try:
        app_idx = int(getattr(rig_settings, "app_list_index", -1))
        if app_list is not None and 0 <= app_idx < len(app_list):
            return str(getattr(app_list[app_idx], "name", "") or "").strip()
    except Exception:
        pass
    return ""


def _actor_has_mimic_setup(armature_obj):
    if armature_obj is None:
        return False
    mimic_name = str(armature_obj.get("mimicFace", "") or "").strip()
    mimic_file = str(armature_obj.get("mimicFaceFile", "") or "").strip()
    if mimic_name and mimic_file:
        return True
    rig_settings = getattr(getattr(armature_obj, "data", None), "witcherui_RigSettings", None)
    return bool(str(getattr(rig_settings, "main_face_skeleton", "") or "").strip()) if rig_settings else False


def _tag_scratch_cutscene_actor(armature_obj, actor_name="", template_path="", actor_type="CAT_Actor",
                                appearance="", use_mimic=False, imported_new=False):
    if armature_obj is None or getattr(armature_obj, "type", None) != 'ARMATURE':
        return False
    actor_name = _resolve_actor_display_name(armature_obj, actor_name)
    template_path = _resolve_actor_template_path(armature_obj, template_path)
    appearance = _resolve_actor_appearance(armature_obj, appearance)
    actor_type = str(actor_type or "CAT_Actor").strip() or "CAT_Actor"

    armature_obj["cutscene_actor_name"] = actor_name
    armature_obj["cutscene_actor_template"] = template_path
    armature_obj["cutscene_actor_type"] = actor_type
    armature_obj["cutscene_component"] = SCRATCH_CUTSCENE_ROOT_COMPONENT
    armature_obj["cutscene_actor_appearance"] = appearance
    armature_obj["cutscene_actor_use_mimic"] = bool(use_mimic)
    armature_obj[import_cutscene.CUTSCENE_ACTOR_IMPORTED_PROP] = bool(imported_new)
    # Initialize extended actor props if not already set
    for _k, _v in (
        ("cutscene_actor_tag", ""),
        ("cutscene_actor_voice_tag", ""),
        ("cutscene_actor_final_position", ""),
        ("cutscene_actor_kill_me", False),
        ("cutscene_actor_anim_final_pos", ""),
    ):
        if _k not in armature_obj:
            armature_obj[_k] = _v
    return True


def _resolve_component_armature(actor_obj, component):
    component_text = str(component or "").strip().lower()
    if component_text in {"face", "mimic"}:
        mimic_name = str(actor_obj.get("mimicFace", "") or "").strip() if actor_obj else ""
        mimic_obj = bpy.data.objects.get(mimic_name) if mimic_name else None
        if getattr(mimic_obj, "type", None) == 'ARMATURE':
            return mimic_obj
    return actor_obj


def _resolve_scratch_component(context):
    scene = getattr(context, "scene", None)
    component = str(getattr(scene, "witcher_cutscene_scratch_component", "") or "").strip() if scene else ""
    if not component:
        component = SCRATCH_CUTSCENE_ROOT_COMPONENT
    if component.lower() == "mimic":
        component = "face"
    return component


def _resolve_scratch_action(context, armature_obj):
    scene = getattr(context, "scene", None)
    action_name = str(getattr(scene, "witcher_cutscene_scratch_action_name", "") or "").strip() if scene else ""
    if action_name:
        return bpy.data.actions.get(action_name)
    source_mode = getattr(scene, "witcher_w3_anim_source", "NLA") if scene else "NLA"
    action, _info = export_anims.resolve_action(
        armature_obj,
        context=context,
        source_mode=source_mode,
        prefer_tracks=("anim_import",),
    )
    return action


def _scratch_action_group_name(action, armature_obj, component):
    """Return the group name for a new cutscene strip.

    If the target armature already has strips on the cutscene_anim track,
    reuse their group name so the new strip joins the same multipart animation.
    Otherwise, use the action name.
    """
    track_name = _scratch_track_name_for_component(component or SCRATCH_CUTSCENE_ROOT_COMPONENT)
    anim_data = getattr(armature_obj, "animation_data", None)
    if anim_data is not None:
        for track in getattr(anim_data, "nla_tracks", []) or []:
            if str(getattr(track, "name", "") or "") != track_name:
                continue
            for strip in getattr(track, "strips", []) or []:
                strip_action = getattr(strip, "action", None)
                if strip_action is None:
                    continue
                stored = str(strip_action.get(export_cutscene.CUTSCENE_ANIMATION_NAME_PROP, "") or "").strip()
                if stored and stored.count(":") >= 2:
                    # format is actor:component:group_name — extract the last part
                    return stored.split(":", 2)[2]
    return str(getattr(action, "name", "") or "").strip() or "cutscene"


def _import_strip_group_name(action, strip):
    stored = str(action.get(export_cutscene.CUTSCENE_ANIMATION_NAME_PROP, "") or "").strip() if action is not None else ""
    if stored and stored.count(":") >= 2:
        return stored.split(":", 2)[2]
    strip_name = str(getattr(strip, "name", "") or "").strip()
    if strip_name:
        return strip_name
    return str(getattr(action, "name", "") or "").strip() or "cutscene"


def _ensure_cutscene_animation_list_entry(scene, actor_name, component, group_name, action, fps=None):
    if scene is None:
        return None
    anims = list(getattr(scene, "witcher_cutscene_animation_items", []))
    if not any(int(getattr(a, "source_index", -2)) == -1 for a in anims):
        sentinel = scene.witcher_cutscene_animation_items.add()
        sentinel.source_index = -1
        sentinel.full_name = "Cutscene"
        sentinel.display_name = "Cutscene"
        anims = list(getattr(scene, "witcher_cutscene_animation_items", []))

    full_name = export_anims._compose_cutscene_animation_name(actor_name, component, group_name)
    existing = next(
        (a for a in anims
         if str(getattr(a, "full_name", "") or "") == full_name
         and int(getattr(a, "source_index", -2)) >= 0),
        None,
    )
    if existing is not None:
        existing.is_loaded = True
        return existing

    new_src_idx = max(
        (int(getattr(a, "source_index", -1)) for a in anims if int(getattr(a, "source_index", -1)) >= 0),
        default=0,
    ) + 1
    item = scene.witcher_cutscene_animation_items.add()
    item.source_index = new_src_idx
    item.full_name = full_name or getattr(action, "name", "")
    item.display_name = group_name or getattr(action, "name", "")
    item.actor_name = actor_name
    item.component_name = component
    item.is_loaded = True
    try:
        fr_start, fr_end = action.frame_range
        item.num_frames = max(1, int(round(fr_end - fr_start)))
    except Exception:
        item.num_frames = 0
    if fps is None:
        render = getattr(scene, "render", None)
        fps = float(getattr(render, "fps", 30.0) or 30.0)
    item.frames_per_second = float(fps or 30.0)
    item.duration = item.num_frames / item.frames_per_second if item.frames_per_second else 0.0
    return item


def _action_frame_range(action):
    try:
        start, end = action.frame_range
        start = float(start)
        end = float(end)
    except Exception:
        start, end = 0.0, 1.0
    if end < start:
        end = start + 1.0
    return start, end


def _copy_action_for_cutscene(action, actor_name, component, group_name):
    if action is None:
        return None
    try:
        cutscene_action = action.copy()
    except Exception:
        return action
    name_parts = [
        str(actor_name or "").strip(),
        str(component or "").strip(),
        str(group_name or getattr(action, "name", "") or "cutscene").strip(),
        "cutscene",
    ]
    cutscene_action.name = "_".join(part.replace(":", "_").replace("\\", "_").replace("/", "_") for part in name_parts if part)
    return cutscene_action


def _scratch_track_name_for_component(component):
    return SCRATCH_CUTSCENE_FACE_TRACK_NAME if str(component or "").strip().lower() == "face" else SCRATCH_CUTSCENE_TRACK_NAME


def _find_or_create_cutscene_track(armature_obj, track_name):
    anim_data = armature_obj.animation_data_create()
    track = anim_data.nla_tracks.get(track_name)
    if track is None:
        track = anim_data.nla_tracks.new()
        track.name = track_name
    return track


def _new_cutscene_edit_track(armature_obj, track_name):
    anim_data = armature_obj.animation_data_create()
    track = anim_data.nla_tracks.new()
    track.name = f"{track_name}_edits"
    return track


def _resolve_scratch_strip_start(context, armature_obj, track_name):
    scene = getattr(context, "scene", None)
    start = float(getattr(scene, "frame_current", 0.0) if scene else 0.0)
    if not bool(getattr(scene, "witcher_cutscene_scratch_add_after_last", False)):
        return start

    anim_data = getattr(armature_obj, "animation_data", None)
    if anim_data is None:
        return start
    last_end = None
    for track in getattr(anim_data, "nla_tracks", []) or []:
        if str(getattr(track, "name", "") or "") != track_name:
            continue
        for strip in getattr(track, "strips", []) or []:
            strip_end = float(getattr(strip, "frame_end", start) or start)
            last_end = strip_end if last_end is None else max(last_end, strip_end)
    return max(start, last_end) if last_end is not None else start


def _tag_action_for_cutscene(action, actor_name, component, group_name):
    if action is None:
        return ""
    anim_name = export_anims._compose_cutscene_animation_name(actor_name, component, group_name)
    action[export_cutscene.CUTSCENE_ANIMATION_NAME_PROP] = anim_name
    action[export_cutscene.CUTSCENE_SOURCE_PATH_PROP] = ""
    action[export_cutscene.CUTSCENE_SOURCE_INDEX_PROP] = -1
    return anim_name


def _create_cutscene_action_strip(context, armature_obj, action, actor_name, component,
                                  start_frame=None, strip_length=None, group_name=""):
    if armature_obj is None or action is None:
        return None, None
    component = component or SCRATCH_CUTSCENE_ROOT_COMPONENT
    group_name = group_name or str(getattr(action, "name", "") or "cutscene")
    action = _copy_action_for_cutscene(action, actor_name, component, group_name)
    _tag_action_for_cutscene(action, actor_name, component, group_name)

    track_name = _scratch_track_name_for_component(component)
    action_start, action_end = _action_frame_range(action)
    default_length = max(1.0, action_end - action_start)
    length = max(1.0, float(strip_length or default_length))
    scene_start = float(start_frame) if start_frame is not None else _resolve_scratch_strip_start(context, armature_obj, track_name)
    scene_end = scene_start + length
    strip_name = group_name if group_name else action.name

    def _create_on_track(track):
        strip = track.strips.new(strip_name, int(round(scene_start)), action)
        strip.frame_start = scene_start
        strip.frame_end = scene_end
        strip.blend_type = 'COMBINE'
        if hasattr(strip, "extrapolation"):
            try:
                strip.extrapolation = 'NOTHING'
            except Exception:
                pass
        _set_strip_action_range(strip, action_start=action_start, action_end=action_start + length)
        bind_strip_action_slot(strip, resolve_action_slot(action, target=armature_obj, ensure=True))
        return track, strip

    preferred_track = _find_or_create_cutscene_track(armature_obj, track_name)
    try:
        return _create_on_track(preferred_track)
    except RuntimeError:
        return _create_on_track(_new_cutscene_edit_track(armature_obj, track_name))


def _action_has_fcurves(action, target=None):
    return bool(tuple(iter_action_fcurves(action, target=target))) if action is not None else False


def _cutscene_actor_roots(scene):
    try:
        return list(export_cutscene._collect_cutscene_actor_roots(scene))
    except Exception:
        return []


def _cutscene_nla_entries(context):
    try:
        return list(export_cutscene._collect_cutscene_nla_entries(context))
    except Exception:
        return []


def _scratch_validation_lines(context):
    scene = getattr(context, "scene", None)
    actors = _cutscene_actor_roots(scene)
    entries = _cutscene_nla_entries(context)
    errors = []
    warnings = []

    if not actors:
        errors.append("No cutscene actors are assigned.")

    cameras = [
        actor for actor in actors
        if str(actor.get("cutscene_actor_type", "") or "") == "CAT_Camera"
        or str(actor.get("cutscene_actor_name", "") or "").strip().lower() == "camera"
        or _is_camera_armature(actor)
    ]
    camera = cameras[0] if cameras else None
    if camera is None:
        errors.append("No cutscene camera actor is assigned.")
    else:
        if not _is_camera_armature(camera):
            errors.append(f"Camera actor '{camera.name}' is missing Camera_Node.")
        camera_cuts = _iter_camera_cut_strips(camera)
        if not camera_cuts:
            errors.append("Camera has no cutscene NLA strips.")
        else:
            for _track, strip in camera_cuts:
                action = getattr(strip, "action", None)
                if action is None:
                    errors.append(f"Camera strip '{strip.name}' has no action.")
                    continue
                if not _action_has_fcurves(action, target=camera):
                    errors.append(f"Camera action '{action.name}' has no keyed channels.")
            markers = [marker for marker in getattr(scene, "timeline_markers", []) if _is_camera_cut_marker(marker)]
            if markers and len(markers) != len(camera_cuts):
                warnings.append(f"Camera marker count ({len(markers)}) does not match camera cuts ({len(camera_cuts)}).")

    entries_by_actor = {}
    for entry in entries:
        entries_by_actor.setdefault(str(entry.get("actor_name", "") or ""), []).append(entry)

    for actor in actors:
        actor_name = str(actor.get("cutscene_actor_name", "") or "").strip()
        actor_type = str(actor.get("cutscene_actor_type", "") or "").strip()
        if not actor_name:
            errors.append(f"Actor object '{actor.name}' has no cutscene actor name.")
        if not _resolve_actor_template_path(actor):
            warnings.append(f"Actor '{actor_name or actor.name}' has no entity template path.")
        if actor_type not in {"CAT_None", "CAT_Actor", "CAT_Prop", "CAT_Camera"}:
            errors.append(f"Actor '{actor_name or actor.name}' has invalid actor type '{actor_type}'.")
        if actor_type != "CAT_Camera":
            skeleton_path = export_cutscene._resolve_cutscene_skeleton_path(actor, SCRATCH_CUTSCENE_ROOT_COMPONENT, scene=scene)
            if not skeleton_path:
                errors.append(f"Actor '{actor_name or actor.name}' has no body skeleton path.")
            if not entries_by_actor.get(actor_name):
                warnings.append(f"Actor '{actor_name or actor.name}' has no cutscene animation strips.")
            if actor_type == "CAT_Actor" and not getattr(getattr(actor, "pose", None), "bones", {}).get("Trajectory"):
                warnings.append(f"Actor '{actor_name or actor.name}' has no Trajectory bone.")
        if bool(actor.get("cutscene_actor_use_mimic", False)):
            mimic_name = str(actor.get("mimicFace", "") or "").strip()
            mimic_obj = bpy.data.objects.get(mimic_name) if mimic_name else None
            if getattr(mimic_obj, "type", None) != 'ARMATURE':
                warnings.append(f"Actor '{actor_name or actor.name}' is marked mimic but has no face armature.")

    for entry in entries:
        action = entry.get("action")
        strip_name = str(entry.get("strip_name", "") or "")
        if action is None:
            errors.append(f"Cutscene strip '{strip_name}' has no action.")
        elif not _action_has_fcurves(action, target=entry.get("armature_obj")):
            warnings.append(f"Action '{action.name}' has no keyed channels.")
        if float(entry.get("strip_frame_end", 0.0) or 0.0) <= float(entry.get("strip_frame_start", 0.0) or 0.0):
            errors.append(f"Cutscene strip '{strip_name}' has zero or negative duration.")

    summary = [
        f"Actors: {len(actors)}",
        f"Animation strips: {len(entries)}",
        f"Camera cuts: {len(_iter_camera_cut_strips(camera)) if camera is not None else 0}",
    ]
    lines = ["OK " + ", ".join(summary)] if not errors else ["ERROR " + ", ".join(summary)]
    lines.extend(f"ERROR {line}" for line in errors)
    lines.extend(f"WARN {line}" for line in warnings)
    return lines, errors, warnings


def _key_pose_bone_transform(pose_bone, frame):
    pose_bone.keyframe_insert(data_path="location", frame=frame)
    if pose_bone.rotation_mode == 'QUATERNION':
        pose_bone.keyframe_insert(data_path="rotation_quaternion", frame=frame)
    elif pose_bone.rotation_mode == 'AXIS_ANGLE':
        pose_bone.keyframe_insert(data_path="rotation_axis_angle", frame=frame)
    else:
        pose_bone.keyframe_insert(data_path="rotation_euler", frame=frame)
    pose_bone.keyframe_insert(data_path="scale", frame=frame)


def _bake_camera_rig_action_from_scene(context, camera_armature, scene_start, scene_end, action_name):
    scene = getattr(context, "scene", None)
    if scene is None or camera_armature is None:
        return None
    pose_bones = getattr(getattr(camera_armature, "pose", None), "bones", None)
    if not pose_bones:
        return None

    scene_start = int(round(float(scene_start)))
    scene_end = int(round(float(scene_end)))
    if scene_end <= scene_start:
        return None

    previous_frame = scene.frame_current
    bone_names = [bone.name for bone in pose_bones]
    samples = []
    try:
        for scene_frame in range(scene_start, scene_end + 1):
            scene.frame_set(scene_frame)
            context.view_layer.update()
            camera_bone = pose_bones.get(CAMERA_CONTROL_BONE)
            track_values = {}
            if camera_bone is not None:
                for track_name in CAMERA_TRACK_NAMES:
                    try:
                        track_values[track_name] = float(camera_bone.get(track_name, 0.0))
                    except Exception:
                        track_values[track_name] = 0.0
            samples.append({
                "frame": scene_frame,
                "bone_matrices": {
                    bone_name: pose_bone.matrix.copy()
                    for bone_name in bone_names
                    for pose_bone in (pose_bones.get(bone_name),)
                    if pose_bone is not None
                },
                "tracks": track_values,
            })
    finally:
        scene.frame_set(previous_frame)
        context.view_layer.update()

    action = bpy.data.actions.new(action_name)
    anim_data = camera_armature.animation_data_create()
    previous_action = getattr(anim_data, "action", None)
    try:
        assign_action(camera_armature, action)
        for sample in samples:
            local_frame = int(sample["frame"] - scene_start)
            for bone_name, matrix in sample["bone_matrices"].items():
                pose_bone = pose_bones.get(bone_name)
                if pose_bone is None:
                    continue
                pose_bone.matrix = matrix
                _key_pose_bone_transform(pose_bone, local_frame)
            camera_bone = pose_bones.get(CAMERA_CONTROL_BONE)
            if camera_bone is not None:
                for track_name, value in sample["tracks"].items():
                    camera_bone[track_name] = value
                    camera_bone.keyframe_insert(data_path=f'["{track_name}"]', frame=local_frame)
    finally:
        if previous_action is not None:
            assign_action(camera_armature, previous_action)
        else:
            anim_data.action = None
    return action


def _bake_camera_rig_action_from_camera(context, camera_armature, camera_obj, scene_start, scene_end, action_name):
    scene = getattr(context, "scene", None)
    if scene is None or camera_armature is None or getattr(camera_obj, "type", None) != 'CAMERA':
        return None
    pose_bones = getattr(getattr(camera_armature, "pose", None), "bones", None)
    if not pose_bones:
        return None

    scene_start = int(round(float(scene_start)))
    scene_end = int(round(float(scene_end)))
    if scene_end <= scene_start:
        return None

    previous_frame = scene.frame_current
    samples = []
    camera_bone = ensure_camera_track_properties(camera_armature, track_names=CAMERA_TRACK_NAMES)
    preview_camera = find_camera_preview_object(camera_armature)
    camera_offset = None
    try:
        scene.frame_set(scene_start)
        context.view_layer.update()
        _clear_camera_preview_drivers(camera_obj)
        edit_world = _pose_bone_world_matrix(camera_armature, CAMERA_EDIT_BONE)
        if edit_world is not None and preview_camera is not None:
            try:
                camera_offset = edit_world.inverted() @ preview_camera.matrix_world
            except Exception:
                camera_offset = None
        for scene_frame in range(scene_start, scene_end + 1):
            scene.frame_set(scene_frame)
            context.view_layer.update()
            track_values = {}
            if camera_bone is not None:
                try:
                    camera_bone["hctFOV"] = math.degrees(float(camera_obj.data.angle_y))
                except Exception:
                    pass
                set_camera_dof_from_blender_camera(camera_bone, camera_obj)
                for track_name in CAMERA_TRACK_NAMES:
                    try:
                        track_values[track_name] = float(camera_bone.get(track_name, 0.0))
                    except Exception:
                        track_values[track_name] = 0.0
            samples.append({
                "frame": scene_frame,
                "matrix_world": camera_obj.matrix_world.copy(),
                "tracks": track_values,
            })
    finally:
        scene.frame_set(previous_frame)
        context.view_layer.update()

    action = bpy.data.actions.new(action_name)
    anim_data = camera_armature.animation_data_create()
    previous_action = getattr(anim_data, "action", None)
    try:
        assign_action(camera_armature, action)
        edit_bone = pose_bones.get(CAMERA_EDIT_BONE) or pose_bones.get(CAMERA_CONTROL_BONE)
        camera_bone = ensure_camera_track_properties(camera_armature, track_names=CAMERA_TRACK_NAMES)
        for sample in samples:
            local_frame = int(sample["frame"] - scene_start)
            if edit_bone is not None:
                desired_world = sample["matrix_world"]
                if camera_offset is not None:
                    try:
                        desired_world = desired_world @ camera_offset.inverted()
                    except Exception:
                        desired_world = sample["matrix_world"]
                keyed_bone = _set_pose_bone_world_matrix(camera_armature, edit_bone.name, desired_world)
                _key_pose_bone_transform(keyed_bone or edit_bone, local_frame)
            if camera_bone is not None:
                for track_name, value in sample["tracks"].items():
                    camera_bone[track_name] = value
                    camera_bone.keyframe_insert(data_path=f'["{track_name}"]', frame=local_frame)
    finally:
        if previous_action is not None:
            assign_action(camera_armature, previous_action)
        else:
            anim_data.action = None
    return action


class WITCH_OT_CameraSetupPreview(bpy.types.Operator):
    """Set up a Blender camera lens driver from the Witcher hctFOV track"""
    bl_idname = "witcher.camera_setup_preview"
    bl_label = "Setup Preview Camera"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        armature_obj = _find_camera_armature(context)
        return armature_obj is not None and getattr(armature_obj, "type", None) == 'ARMATURE'

    def execute(self, context):
        armature_obj = _find_camera_armature(context)
        if armature_obj is None:
            self.report({'WARNING'}, "No camera armature selected.")
            return {'CANCELLED'}

        camera_obj = find_camera_preview_object(armature_obj)
        active_obj = getattr(context, "active_object", None)
        if camera_obj is None and getattr(active_obj, "type", None) == 'CAMERA':
            camera_obj = active_obj
        if camera_obj is None:
            self.report({'WARNING'}, "No Blender camera found under the camera rig.")
            return {'CANCELLED'}

        if not setup_camera_preview_drivers(armature_obj, camera_obj):
            self.report({'WARNING'}, "Could not set up the camera preview drivers.")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Linked {camera_obj.name} lens and DOF to Camera_Node tracks.")
        return {'FINISHED'}


class WITCH_OT_CameraSetSceneCamera(bpy.types.Operator):
    """Use the imported Witcher camera object as the Blender scene camera"""
    bl_idname = "witcher.camera_set_scene_camera"
    bl_label = "Set Scene Camera"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _find_camera_armature(context) is not None

    def execute(self, context):
        camera_armature = _find_camera_armature(context)
        camera_obj = find_camera_preview_object(camera_armature)
        if camera_obj is None:
            self.report({'WARNING'}, "No Blender camera found under the camera rig.")
            return {'CANCELLED'}
        context.scene.camera = camera_obj
        setup_camera_preview_drivers(camera_armature, camera_obj)
        return {'FINISHED'}


class WITCH_OT_CameraKeyRigFromSceneCamera(bpy.types.Operator):
    """Key the Witcher camera rig from the active Blender camera"""
    bl_idname = "witcher.camera_key_rig_from_scene_camera"
    bl_label = "Key Rig From Scene Camera"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _find_camera_armature(context) is not None

    def execute(self, context):
        camera_armature = _find_camera_armature(context)
        camera_obj = _resolve_native_camera_target(context, camera_armature)
        if camera_obj is None or getattr(camera_obj, "type", None) != 'CAMERA':
            self.report({'WARNING'}, "No Blender camera available.")
            return {'CANCELLED'}
        if camera_obj == find_camera_preview_object(camera_armature):
            self.report({'WARNING'}, "Use a separate Blender camera when keying the rig from camera view.")
            return {'CANCELLED'}

        cuts = _iter_camera_cut_strips(camera_armature)
        cut_index = _current_camera_cut_index(context, camera_armature, cuts)
        target_action = None
        key_frame = None
        if cut_index >= 0:
            _track, strip = cuts[cut_index]
            target_action = getattr(strip, "action", None)
            key_frame = _strip_scene_frame_to_action_frame(strip, context.scene.frame_current)

        if not _key_rig_from_camera(
            context,
            camera_armature,
            camera_obj,
            insert_key=True,
            key_frame=key_frame,
            target_action=target_action,
        ):
            self.report({'WARNING'}, "Could not key the camera rig.")
            return {'CANCELLED'}
        return {'FINISHED'}


class WITCH_OT_CameraBakeCutFromSceneCamera(bpy.types.Operator):
    """Bake the current camera cut from the active Blender camera to the Witcher camera rig"""
    bl_idname = "witcher.camera_bake_cut_from_scene_camera"
    bl_label = "Bake Cut From Scene Camera"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _find_camera_armature(context) is not None

    def execute(self, context):
        scene = context.scene
        camera_armature = _find_camera_armature(context)
        camera_obj = _resolve_native_camera_target(context, camera_armature)
        if camera_obj is None or getattr(camera_obj, "type", None) != 'CAMERA':
            self.report({'WARNING'}, "No Blender camera available.")
            return {'CANCELLED'}
        if camera_obj == find_camera_preview_object(camera_armature):
            self.report({'WARNING'}, "Use a separate Blender camera when baking the rig from camera view.")
            return {'CANCELLED'}

        cuts = _iter_camera_cut_strips(camera_armature)
        cut_index = _current_camera_cut_index(context, camera_armature, cuts)
        if cut_index < 0:
            self.report({'WARNING'}, "No camera cut strip found.")
            return {'CANCELLED'}

        _track, strip = cuts[cut_index]
        target_action = getattr(strip, "action", None)
        start = int(round(float(strip.frame_start)))
        end = int(round(float(strip.frame_end)))
        previous_frame = scene.frame_current
        try:
            for frame in range(start, end + 1):
                scene.frame_set(frame)
                context.view_layer.update()
                _key_rig_from_camera(
                    context,
                    camera_armature,
                    camera_obj,
                    insert_key=True,
                    key_frame=_strip_scene_frame_to_action_frame(strip, frame),
                    target_action=target_action,
                )
        finally:
            scene.frame_set(previous_frame)
        return {'FINISHED'}


class WITCH_OT_CameraCutJump(bpy.types.Operator):
    """Jump between imported camera cut strips"""
    bl_idname = "witcher.camera_cut_jump"
    bl_label = "Jump Cut"
    bl_options = {'REGISTER', 'UNDO'}

    direction: EnumProperty(
        name="Direction",
        items=[
            ('PREV', "Previous", ""),
            ('CURRENT', "Current", ""),
            ('NEXT', "Next", ""),
        ],
        default='CURRENT',
    )

    @classmethod
    def poll(cls, context):
        return _find_camera_armature(context) is not None

    def execute(self, context):
        camera_armature = _find_camera_armature(context)
        cuts = _iter_camera_cut_strips(camera_armature)
        if not cuts:
            self.report({'WARNING'}, "No camera cut strips found.")
            return {'CANCELLED'}

        idx = _current_camera_cut_index(context, camera_armature, cuts)
        if self.direction == 'PREV':
            idx = max(0, idx - 1)
        elif self.direction == 'NEXT':
            idx = min(len(cuts) - 1, idx + 1)
        idx = max(0, min(idx, len(cuts) - 1))

        track, strip = cuts[idx]
        _select_nla_strip(track, strip)
        context.scene.frame_set(int(round(float(strip.frame_start))))
        return {'FINISHED'}


class WITCH_OT_CameraCutResize(bpy.types.Operator):
    """Resize the current camera cut strip and ripple later cutscene strips"""
    bl_idname = "witcher.camera_cut_resize"
    bl_label = "Resize Cut"
    bl_options = {'REGISTER', 'UNDO'}

    delta: IntProperty(
        name="Frames",
        default=1,
    )
    ripple: BoolProperty(
        name="Ripple",
        default=True,
    )

    @classmethod
    def poll(cls, context):
        return _find_camera_armature(context) is not None

    def execute(self, context):
        scene = context.scene
        camera_armature = _find_camera_armature(context)
        cuts = _iter_camera_cut_strips(camera_armature)
        cut_index = _current_camera_cut_index(context, camera_armature, cuts)
        if cut_index < 0:
            self.report({'WARNING'}, "No camera cut strip found.")
            return {'CANCELLED'}

        track, strip = cuts[cut_index]
        old_end = float(strip.frame_end)
        min_end = float(strip.frame_start) + 1.0
        new_end = max(min_end, old_end + int(self.delta))
        actual_delta = new_end - old_end
        if abs(actual_delta) <= 1e-6:
            return {'CANCELLED'}

        strip.frame_end = new_end
        if self.ripple:
            _move_cutscene_strips_after(scene, old_end, actual_delta, skip_strip=strip)
        _select_nla_strip(track, strip)
        scene.frame_set(int(round(float(strip.frame_start))))
        _sync_camera_cut_markers(scene, camera_armature)
        return {'FINISHED'}


class WITCH_OT_CameraCutSplit(bpy.types.Operator):
    """Split the current camera cut strip at the playhead"""
    bl_idname = "witcher.camera_cut_split"
    bl_label = "Cut"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _find_camera_armature(context) is not None

    def execute(self, context):
        scene = context.scene
        camera_armature = _find_camera_armature(context)
        cuts = _iter_camera_cut_strips(camera_armature)
        cut_index = _current_camera_cut_index(context, camera_armature, cuts)
        if cut_index < 0:
            self.report({'WARNING'}, "No camera cut strip found.")
            return {'CANCELLED'}

        track, strip = cuts[cut_index]
        action = getattr(strip, "action", None)
        if action is None:
            self.report({'WARNING'}, "Current camera cut has no action.")
            return {'CANCELLED'}

        split_frame = float(scene.frame_current)
        old_start = float(strip.frame_start)
        old_end = float(strip.frame_end)
        if split_frame <= old_start + 1e-4 or split_frame >= old_end - 1e-4:
            self.report({'WARNING'}, "Move the playhead inside the cut, not on its boundary.")
            return {'CANCELLED'}

        split_action_frame = _strip_scene_frame_to_action_frame(strip, split_frame)
        old_action_end = float(getattr(strip, "action_frame_end", split_action_frame) or split_action_frame)
        strip_settings = _strip_settings_snapshot(strip)

        strip.frame_end = split_frame
        _set_strip_action_range(strip, action_end=split_action_frame)
        new_track, new_strip = _create_camera_cut_strip(
            camera_armature,
            track,
            f"{strip.name}_cut",
            split_frame,
            old_end,
            action,
            action_start=split_action_frame,
            action_end=old_action_end,
            settings=strip_settings,
        )

        _linearize_action_keys_near_frame(action, split_action_frame, radius=2.0, target=camera_armature)
        _select_nla_strip(new_track, new_strip)
        _sync_camera_cut_markers(scene, camera_armature)
        return {'FINISHED'}


class WITCH_OT_CameraCutCombine(bpy.types.Operator):
    """Combine adjacent camera cut strips into one baked camera action"""
    bl_idname = "witcher.camera_cut_combine"
    bl_label = "Combine"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _find_camera_armature(context) is not None

    def execute(self, context):
        scene = context.scene
        camera_armature = _find_camera_armature(context)
        selected_cuts = _camera_cut_selection_for_combine(context, camera_armature)
        if len(selected_cuts) < 2:
            self.report({'WARNING'}, "Select adjacent camera cuts or place the playhead before the next cut.")
            return {'CANCELLED'}
        if not _validate_adjacent_camera_cuts(selected_cuts):
            self.report({'WARNING'}, "Camera cuts must be adjacent.")
            return {'CANCELLED'}

        first_track, first_strip = selected_cuts[0]
        scene_start = float(first_strip.frame_start)
        scene_end = float(selected_cuts[-1][1].frame_end)
        strip_settings = _strip_settings_snapshot(first_strip)
        action_name = f"{camera_armature.name}_camera_combined_{int(round(scene_start))}_{int(round(scene_end))}"
        action = _bake_camera_rig_action_from_scene(
            context,
            camera_armature,
            scene_start,
            scene_end,
            action_name,
        )
        if action is None:
            self.report({'WARNING'}, "Could not bake combined camera cut action.")
            return {'CANCELLED'}

        for track, strip in reversed(selected_cuts):
            track.strips.remove(strip)

        new_track, new_strip = _create_camera_cut_strip(
            camera_armature,
            first_track,
            action.name,
            scene_start,
            scene_end,
            action,
            action_start=0.0,
            action_end=max(1.0, scene_end - scene_start),
            settings=strip_settings,
        )
        _select_nla_strip(new_track, new_strip)
        scene.frame_set(int(round(scene_start)))
        _sync_camera_cut_markers(scene, camera_armature)
        return {'FINISHED'}


class WITCH_OT_CameraCutSyncMarkers(bpy.types.Operator):
    """Create timeline markers from the current camera cut NLA strips"""
    bl_idname = "witcher.camera_cut_sync_markers"
    bl_label = "Sync Markers"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _find_camera_armature(context) is not None

    def execute(self, context):
        camera_armature = _find_camera_armature(context)
        count = _sync_camera_cut_markers(context.scene, camera_armature)
        self.report({'INFO'}, f"Synced {count} camera cut markers.")
        return {'FINISHED'}


class WITCH_OT_CameraCutApplyMarkers(bpy.types.Operator):
    """Apply moved timeline markers back to camera cut NLA strips"""
    bl_idname = "witcher.camera_cut_apply_markers"
    bl_label = "Apply Markers"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _find_camera_armature(context) is not None

    def execute(self, context):
        camera_armature = _find_camera_armature(context)
        count = _apply_camera_cut_markers_to_strips(context.scene, camera_armature)
        if not count:
            self.report({'WARNING'}, "Camera marker count does not match camera cut count.")
            return {'CANCELLED'}
        _sync_camera_cut_markers(context.scene, camera_armature)
        self.report({'INFO'}, f"Applied {count} camera cut markers.")
        return {'FINISHED'}


class WITCH_OT_CameraSetDofFromSelected(bpy.types.Operator):
    """Set Witcher DOF tracks from the distance to the active selected object or bone"""
    bl_idname = "witcher.camera_set_dof_from_selected"
    bl_label = "DOF From Selected"
    bl_options = {'REGISTER', 'UNDO'}

    keyframe: BoolProperty(
        name="Keyframe",
        default=True,
        description="Insert keyframes on the DOF tracks at the current frame",
    )
    far_distance_factor: FloatProperty(
        name="Far Blur",
        default=5.0,
        min=0.0,
    )
    near_distance_factor: FloatProperty(
        name="Near Blur",
        default=0.5,
        min=0.0,
    )
    far_focus_factor: FloatProperty(
        name="Far Focus",
        default=1.0,
        min=0.0,
    )
    near_focus_factor: FloatProperty(
        name="Near Focus",
        default=0.5,
        min=0.0,
    )
    intensity: FloatProperty(
        name="Intensity",
        default=1.0,
        min=0.0,
    )

    @classmethod
    def poll(cls, context):
        armature_obj = _find_camera_armature(context)
        return armature_obj is not None and getattr(armature_obj, "type", None) == 'ARMATURE'

    def execute(self, context):
        armature_obj = _find_camera_armature(context)
        camera_bone = ensure_camera_track_properties(armature_obj, track_names=CAMERA_TRACK_NAMES)
        if camera_bone is None:
            self.report({'WARNING'}, "No Camera_Node bone found on the selected armature.")
            return {'CANCELLED'}

        camera_pos = _camera_bone_world_position(armature_obj)
        target_pos = _selected_camera_dof_target_position(context, armature_obj)
        if camera_pos is None or target_pos is None:
            self.report({'WARNING'}, "Select a target object or pose bone to set camera DOF.")
            return {'CANCELLED'}

        distance = (target_pos - camera_pos).length
        set_camera_dof_from_distance(
            camera_bone,
            distance,
            far_distance_factor=self.far_distance_factor,
            near_distance_factor=self.near_distance_factor,
            far_focus_factor=self.far_focus_factor,
            near_focus_factor=self.near_focus_factor,
            override=1.0,
            intensity=self.intensity,
        )
        if self.keyframe:
            frame = getattr(getattr(context, "scene", None), "frame_current", None)
            if frame is not None:
                target_action = None
                cuts = _iter_camera_cut_strips(armature_obj)
                cut_index = _current_camera_cut_index(context, armature_obj, cuts)
                if cut_index >= 0:
                    _track, strip = cuts[cut_index]
                    target_action = getattr(strip, "action", None)
                    frame = _strip_scene_frame_to_action_frame(strip, frame)

                anim_data = armature_obj.animation_data_create()
                previous_action = getattr(anim_data, "action", None)
                if target_action is not None:
                    anim_data.action = target_action
                try:
                    for track_name in CAMERA_DOF_TRACK_NAMES:
                        camera_bone.keyframe_insert(data_path=f'["{track_name}"]', frame=frame)
                finally:
                    if target_action is not None:
                        anim_data.action = previous_action
        self.report({'INFO'}, f"Set camera DOF from distance {distance:.3f}.")
        return {'FINISHED'}


class WITCH_OT_CameraConvertCutsToBlenderCameras(bpy.types.Operator):
    """Create a Blender camera for each NLA cut strip by baking the rig's animation onto it.
    Tag each camera with the strip name so it can be applied back with 'Blender Cams → Rig'."""
    bl_idname = "witcher.camera_convert_cuts_to_blender_cameras"
    bl_label = "Cuts → Blender Cameras"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _find_camera_armature(context) is not None

    def execute(self, context):
        scene = context.scene
        camera_armature = _find_camera_armature(context)
        if camera_armature is None:
            self.report({'WARNING'}, "No Witcher camera rig found.")
            return {'CANCELLED'}

        preview_camera = find_camera_preview_object(camera_armature)
        cuts = _iter_camera_cut_strips(camera_armature)
        if not cuts:
            self.report({'WARNING'}, "No camera cut NLA strips found.")
            return {'CANCELLED'}

        camera_bone = ensure_camera_track_properties(camera_armature, track_names=CAMERA_TRACK_NAMES)
        previous_frame = scene.frame_current
        created = []
        try:
            for _track, strip in cuts:
                strip_name = str(getattr(strip, "name", "") or "")
                cam_name = f"BlenderCam_{strip_name}" if strip_name else f"BlenderCam_{len(created) + 1:02d}"

                cam_data = bpy.data.cameras.new(name=cam_name)
                cam_data.sensor_fit = 'VERTICAL'
                cam_data.sensor_height = CAMERA_SENSOR_HEIGHT
                cam_data.lens_unit = 'FOV'

                cam_obj = bpy.data.objects.new(name=cam_name, object_data=cam_data)
                cam_obj["witcher_cut_strip_name"] = strip_name
                scene.collection.objects.link(cam_obj)
                created.append(cam_obj)

                start = int(round(float(strip.frame_start)))
                end = int(round(float(strip.frame_end)))

                for frame in range(start, end + 1):
                    scene.frame_set(frame)
                    context.view_layer.update()

                    world_mat = None
                    if preview_camera is not None:
                        world_mat = preview_camera.matrix_world.copy()
                    else:
                        world_mat = _pose_bone_world_matrix(camera_armature, CAMERA_EDIT_BONE)

                    if world_mat is not None:
                        cam_obj.matrix_world = world_mat

                    if camera_bone is not None and "hctFOV" in camera_bone:
                        cam_data.lens = fov_to_lens(float(camera_bone["hctFOV"]))

                    cam_obj.keyframe_insert("location", frame=frame)
                    cam_obj.keyframe_insert("rotation_euler", frame=frame)
                    cam_data.keyframe_insert("lens", frame=frame)
        finally:
            scene.frame_set(previous_frame)

        self.report({'INFO'}, f"Created {len(created)} Blender camera(s) from cuts.")
        return {'FINISHED'}


class WITCH_OT_CameraApplyBlenderCamerasToRig(bpy.types.Operator):
    """Bake Blender shot cameras onto the Witcher camera rig.

    Primary path: reads cameras bound to timeline markers via 'New Shot'.
    Auto-imports the Witcher camera rig if not yet in the scene.
    Legacy path: cameras tagged with 'witcher_cut_strip_name' (from Cuts → Blender Cams)."""
    bl_idname = "witcher.camera_apply_blender_cameras_to_rig"
    bl_label = "Shots → Rig"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        scene = getattr(context, "scene", None)
        if scene is None:
            return False
        has_shots = any(
            obj.get("witcher_shot_index") is not None
            for obj in scene.objects
            if getattr(obj, "type", None) == 'CAMERA'
        )
        return has_shots or _find_camera_armature(context) is not None

    def execute(self, context):
        scene = context.scene
        shots = _iter_shot_markers(scene)
        if shots:
            return self._apply_shots(context, shots)
        return self._apply_tagged_cameras(context)

    def _apply_shots(self, context, shots):
        scene = context.scene
        camera_armature = _find_camera_armature(context)
        if camera_armature is None:
            camera_armature = _import_cutscene_camera_rig(context)
            if camera_armature is None:
                self.report({'ERROR'}, "Could not find or import the Witcher camera rig. Set the entity path in the Camera tab.")
                return {'CANCELLED'}

        if not str(camera_armature.get("cutscene_actor_name", "") or "").strip():
            _tag_scratch_cutscene_actor(
                camera_armature,
                actor_name="Camera",
                template_path=getattr(scene, "witcher_cutscene_scratch_camera_repo_path", "") or SCRATCH_CAMERA_DEFAULT_REPO_PATH,
                actor_type="CAT_Camera",
                appearance="",
                use_mimic=False,
                imported_new=False,
            )

        scene_end = int(getattr(scene, "frame_end", 0))
        shot_ranges = []
        for i, (shot_idx, cam_obj, frame) in enumerate(shots):
            next_frame = shots[i + 1][2] if i + 1 < len(shots) else scene_end + 1
            shot_ranges.append((shot_idx, cam_obj, frame, next_frame - 1))

        actor_name = str(camera_armature.get("cutscene_actor_name", "") or "Camera")
        component = SCRATCH_CUTSCENE_ROOT_COMPONENT
        camera_animation_name = "camera"
        applied = 0
        baked_shots = []  # (shot_idx, action, range_start, range_end)
        for shot_idx, cam_obj, range_start, range_end in shot_ranges:
            if range_end <= range_start:
                continue
            action_name = _shot_camera_action_name(shot_idx)
            old_action = bpy.data.actions.get(action_name)
            if old_action is not None:
                _remove_shot_nla_strip(camera_armature, shot_idx)
                bpy.data.actions.remove(old_action)
            action = _bake_camera_rig_action_from_camera(
                context, camera_armature, cam_obj, range_start, range_end, action_name,
            )
            if action is None:
                continue
            action["witcher_shot_index"] = shot_idx
            _tag_action_for_cutscene(action, actor_name, component, camera_animation_name)

            # Passthrough bones (Camera_OrbitNode / Camera_LookAtNode) are part of the
            # rig skeleton but rarely animated. If they have no fcurves in the action the
            # exporter skips them and the game uses stale bone state from a previous
            # animation, causing the camera to appear offset / on its side. Insert
            # identity (rest-pose) keyframes on every frame so the exporter includes them.
            length_local = max(1, range_end - range_start)
            _PASSTHROUGH_BONES = ("Camera_OrbitNode", "Camera_LookAtNode")
            for pb_name in _PASSTHROUGH_BONES:
                pb = camera_armature.pose.bones.get(pb_name)
                if pb is None:
                    continue
                existing_dps = {
                    fc.data_path
                    for fc in iter_action_fcurves(action, target=camera_armature)
                    if f'pose.bones["{pb_name}"]' in fc.data_path
                }
                if existing_dps:
                    continue
                rot_mode = getattr(pb, "rotation_mode", "QUATERNION")
                if rot_mode == "QUATERNION":
                    dp_rot = f'pose.bones["{pb_name}"].rotation_quaternion'
                    rot_vals = [1.0, 0.0, 0.0, 0.0]
                elif rot_mode == "AXIS_ANGLE":
                    dp_rot = f'pose.bones["{pb_name}"].rotation_axis_angle'
                    rot_vals = [0.0, 0.0, 1.0, 0.0]
                else:
                    dp_rot = f'pose.bones["{pb_name}"].rotation_euler'
                    rot_vals = [0.0, 0.0, 0.0]
                dp_loc = f'pose.bones["{pb_name}"].location'
                for af in range(0, length_local + 1):
                    _fcurve_insert_direct(action, camera_armature, dp_loc, af, [0.0, 0.0, 0.0])
                    _fcurve_insert_direct(action, camera_armature, dp_rot, af, rot_vals)

            track = _find_or_create_cutscene_track(camera_armature, SCRATCH_CUTSCENE_TRACK_NAME)
            _create_camera_cut_strip(
                camera_armature, track, action_name,
                float(range_start), float(range_end), action,
                action_start=0.0,
                action_end=float(length_local),
                settings={"blend_type": "COMBINE", "extrapolation": "NOTHING"},
            )
            baked_shots.append((shot_idx, action, range_start, range_end))
            applied += 1

        camera_obj = find_camera_preview_object(camera_armature)
        if camera_obj is not None:
            setup_camera_preview_drivers(camera_armature, camera_obj)
            scene.camera = camera_obj
        _sync_camera_cut_markers(scene, camera_armature)

        # Register camera shots as one cutscene animation. Each shot remains a separate
        # NLA strip/action for editing, but they share the same cutscene animation name
        # so export writes them as multipart parts of Camera:Root:camera.
        fps = float(getattr(getattr(scene, "render", None), "fps", None) or 30)
        anims = scene.witcher_cutscene_animation_items
        # Ensure cutscene sentinel (source_index -1) exists
        if not any(int(getattr(a, "source_index", -2)) == -1 for a in anims):
            sentinel = anims.add()
            sentinel.source_index = -1
            sentinel.full_name = "Cutscene"
            sentinel.display_name = "Cutscene"
        # Remove old camera entries (actor_name == actor_name, source_index >= 0)
        to_remove = [
            i for i, a in enumerate(anims)
            if str(getattr(a, "actor_name", "") or "").lower() == actor_name.lower()
            and int(getattr(a, "source_index", -2)) >= 0
        ]
        for i in reversed(to_remove):
            anims.remove(i)
        next_idx = max((int(getattr(a, "source_index", -1)) for a in anims if int(getattr(a, "source_index", -1)) >= 0), default=0) + 1
        if baked_shots:
            first_start = min(int(range_start) for _shot_idx, _action, range_start, _range_end in baked_shots)
            last_end = max(int(range_end) for _shot_idx, _action, _range_start, range_end in baked_shots)
            full_name = export_anims._compose_cutscene_animation_name(actor_name, component, camera_animation_name)
            item = anims.add()
            item.source_index = next_idx
            item.full_name = full_name
            item.display_name = camera_animation_name
            item.actor_name = actor_name
            item.component_name = component
            item.is_loaded = True
            item.num_frames = max(1, last_end - first_start + 1)
            item.frames_per_second = fps
            item.duration = item.num_frames / fps if fps else 0.0

        if applied == 0:
            self.report({'WARNING'}, "No shots baked. Check that shots have markers with cameras bound.")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Baked {applied} shot(s) onto the rig.")
        return {'FINISHED'}

    def _apply_tagged_cameras(self, context):
        scene = context.scene
        camera_armature = _find_camera_armature(context)
        if camera_armature is None:
            self.report({'WARNING'}, "No Witcher camera rig found.")
            return {'CANCELLED'}

        cuts = _iter_camera_cut_strips(camera_armature)
        if not cuts:
            self.report({'WARNING'}, "No camera cut NLA strips found.")
            return {'CANCELLED'}

        tagged_cameras = {}
        for obj in scene.objects:
            if getattr(obj, "type", None) == 'CAMERA':
                strip_name = str(obj.get("witcher_cut_strip_name", "") or "")
                if strip_name:
                    tagged_cameras[strip_name] = obj

        if not tagged_cameras:
            self.report({'WARNING'}, "No shots or tagged cameras found. Use 'New Shot' or 'Cuts → Blender Cams' first.")
            return {'CANCELLED'}

        previous_frame = scene.frame_current
        applied = 0
        try:
            for _track, strip in cuts:
                strip_name = str(getattr(strip, "name", "") or "")
                blender_cam = tagged_cameras.get(strip_name)
                if blender_cam is None:
                    continue
                target_action = getattr(strip, "action", None)
                if target_action is None:
                    continue

                start = int(round(float(strip.frame_start)))
                end = int(round(float(strip.frame_end)))

                for frame in range(start, end + 1):
                    scene.frame_set(frame)
                    context.view_layer.update()
                    action_frame = _strip_scene_frame_to_action_frame(strip, frame)
                    _key_rig_from_camera(
                        context,
                        camera_armature,
                        blender_cam,
                        insert_key=True,
                        key_frame=action_frame,
                        target_action=target_action,
                    )

                _PASSTHROUGH_BONES = ("Camera_OrbitNode", "Camera_LookAtNode")
                for pb_name in _PASSTHROUGH_BONES:
                    pb = camera_armature.pose.bones.get(pb_name)
                    if pb is None:
                        continue
                    existing_dps = {
                        fc.data_path
                        for fc in iter_action_fcurves(target_action, target=camera_armature)
                        if f'pose.bones["{pb_name}"]' in fc.data_path
                    }
                    if existing_dps:
                        continue
                    rot_mode = getattr(pb, "rotation_mode", "QUATERNION")
                    if rot_mode == "QUATERNION":
                        dp_rot = f'pose.bones["{pb_name}"].rotation_quaternion'
                        rot_vals = [1.0, 0.0, 0.0, 0.0]
                    elif rot_mode == "AXIS_ANGLE":
                        dp_rot = f'pose.bones["{pb_name}"].rotation_axis_angle'
                        rot_vals = [0.0, 0.0, 1.0, 0.0]
                    else:
                        dp_rot = f'pose.bones["{pb_name}"].rotation_euler'
                        rot_vals = [0.0, 0.0, 0.0]
                    dp_loc = f'pose.bones["{pb_name}"].location'
                    for frame in range(start, end + 1):
                        af = _strip_scene_frame_to_action_frame(strip, frame)
                        _fcurve_insert_direct(target_action, camera_armature, dp_loc, af, [0.0, 0.0, 0.0])
                        _fcurve_insert_direct(target_action, camera_armature, dp_rot, af, rot_vals)

                applied += 1
        finally:
            scene.frame_set(previous_frame)

        if applied == 0:
            self.report({'WARNING'}, "No matching strips found for tagged cameras.")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Baked {applied} camera(s) onto the rig.")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Pelvis / Root bone animation offset editor
# ---------------------------------------------------------------------------

def _get_pelvis_anim_action(armature_obj, context):
    """Return the action that should be edited for the pelvis bone (NLA-aware)."""
    anim_data = getattr(armature_obj, "animation_data", None)
    if anim_data is None:
        return None
    if getattr(anim_data, "use_nla", False):
        scene = getattr(context, "scene", None)
        frame = getattr(scene, "frame_current", 0)
        action, _ = export_anims.get_nla_action_at_frame(armature_obj, frame=frame)
        if action is not None:
            return action
    return getattr(anim_data, "action", None)


def _apply_loc_delta_to_action_fcurves(action, armature_obj, bone_name, delta_loc):
    """Add a location delta to every location keyframe of a bone."""
    dp = f'pose.bones["{bone_name}"].location'
    modified = 0
    for fc in iter_action_fcurves(action, target=armature_obj):
        if fc.data_path != dp:
            continue
        idx = fc.array_index
        if 0 <= idx < 3:
            for kp in fc.keyframe_points:
                kp.co[1] += delta_loc[idx]
            fc.update()
            modified += 1
    return modified


def _apply_euler_delta_to_action_fcurves(action, armature_obj, bone_name, delta_rad):
    """Add per-axis radian offset to rotation_euler keyframes of a bone."""
    dp = f'pose.bones["{bone_name}"].rotation_euler'
    modified = 0
    for fc in iter_action_fcurves(action, target=armature_obj):
        if fc.data_path != dp:
            continue
        idx = fc.array_index
        if 0 <= idx < 3:
            for kp in fc.keyframe_points:
                kp.co[1] += delta_rad[idx]
            fc.update()
            modified += 1
    return modified


def _apply_quat_delta_to_action_fcurves(action, armature_obj, bone_name, delta_q):
    """Apply a quaternion delta to all rotation_quaternion keyframes of a bone."""
    dp = f'pose.bones["{bone_name}"].rotation_quaternion'
    fcs = [None] * 4
    for fc in iter_action_fcurves(action, target=armature_obj):
        if fc.data_path == dp and 0 <= fc.array_index < 4:
            fcs[fc.array_index] = fc
    if not any(fcs):
        return 0
    frames = set()
    for fc in fcs:
        if fc:
            for kp in fc.keyframe_points:
                frames.add(kp.co[0])
    for frame_co in sorted(frames):
        vals = [1.0, 0.0, 0.0, 0.0]
        kp_refs = [None] * 4
        for i, fc in enumerate(fcs):
            if fc is None:
                continue
            for kp in fc.keyframe_points:
                if abs(kp.co[0] - frame_co) < 0.5:
                    vals[i] = kp.co[1]
                    kp_refs[i] = kp
                    break
        old_q = MQuaternion(vals)
        new_q = delta_q @ old_q
        new_vals = [new_q.w, new_q.x, new_q.y, new_q.z]
        for i, kp in enumerate(kp_refs):
            if kp is not None:
                kp.co[1] = new_vals[i]
    for fc in fcs:
        if fc:
            fc.update()
    return 1


class WITCH_OT_PelvisSetReference(bpy.types.Operator):
    """Store the current pose of the selected bone as the reference for delta baking"""
    bl_idname = "witcher.pelvis_set_reference"
    bl_label = "Set Reference Pose"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        ao = context.active_object
        return ao is not None and ao.type == 'ARMATURE'

    def execute(self, context):
        scene = context.scene
        ao = context.active_object
        bone_name = str(getattr(scene, "witcher_pelvis_bone_name", "Pelvis") or "Pelvis")
        pb = ao.pose.bones.get(bone_name)
        if pb is None:
            self.report({'WARNING'}, f"Bone '{bone_name}' not found in armature.")
            return {'CANCELLED'}
        loc = pb.location
        scene.witcher_pelvis_ref_loc = (loc.x, loc.y, loc.z)
        rot = pb.rotation_quaternion if pb.rotation_mode == 'QUATERNION' else pb.rotation_euler.to_quaternion()
        scene.witcher_pelvis_ref_rot = (rot.w, rot.x, rot.y, rot.z)
        scene.witcher_pelvis_has_ref = True
        self.report({'INFO'}, f"Reference pose stored for '{bone_name}'.")
        return {'FINISHED'}


class WITCH_OT_PelvisBakePoseDelta(bpy.types.Operator):
    """Bake the delta between the reference pose and the current bone pose onto all keyframes"""
    bl_idname = "witcher.pelvis_bake_pose_delta"
    bl_label = "Bake Pose Delta"
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=400)

    def draw(self, context):
        layout = self.layout
        layout.label(text="WARNING: This permanently modifies animation keyframe data.", icon='ERROR')
        layout.label(text="Save your .blend file before applying!", icon='FILE_BLEND')
        layout.separator()
        layout.label(text="The delta between the stored reference pose and the")
        layout.label(text="current bone pose will be baked into every keyframe.")

    @classmethod
    def poll(cls, context):
        ao = context.active_object
        if ao is None or ao.type != 'ARMATURE':
            return False
        scene = context.scene
        return bool(getattr(scene, "witcher_pelvis_has_ref", False))

    def execute(self, context):
        scene = context.scene
        ao = context.active_object
        bone_name = str(getattr(scene, "witcher_pelvis_bone_name", "Pelvis") or "Pelvis")
        pb = ao.pose.bones.get(bone_name)
        if pb is None:
            self.report({'WARNING'}, f"Bone '{bone_name}' not found in armature.")
            return {'CANCELLED'}
        action = _get_pelvis_anim_action(ao, context)
        if action is None:
            self.report({'WARNING'}, "No active action found for the armature.")
            return {'CANCELLED'}

        ref_loc = list(getattr(scene, "witcher_pelvis_ref_loc", [0.0, 0.0, 0.0]))
        ref_rot_vals = list(getattr(scene, "witcher_pelvis_ref_rot", [1.0, 0.0, 0.0, 0.0]))
        ref_q = MQuaternion(ref_rot_vals)

        cur_loc = pb.location
        delta_loc = (cur_loc.x - ref_loc[0], cur_loc.y - ref_loc[1], cur_loc.z - ref_loc[2])

        cur_q = pb.rotation_quaternion if pb.rotation_mode == 'QUATERNION' else pb.rotation_euler.to_quaternion()
        delta_q = cur_q @ ref_q.conjugated()

        _apply_loc_delta_to_action_fcurves(action, ao, bone_name, delta_loc)
        if pb.rotation_mode == 'QUATERNION':
            _apply_quat_delta_to_action_fcurves(action, ao, bone_name, delta_q)
        else:
            delta_euler_vals = delta_q.to_euler(pb.rotation_mode)
            _apply_euler_delta_to_action_fcurves(action, ao, bone_name, [delta_euler_vals.x, delta_euler_vals.y, delta_euler_vals.z])

        scene.witcher_pelvis_has_ref = False
        self.report({'INFO'}, f"Pose delta baked into action '{action.name}' for bone '{bone_name}'.")
        return {'FINISHED'}


class WITCH_OT_PelvisApplyNumericOffset(bpy.types.Operator):
    """Apply a numeric location/rotation offset to all keyframes of the selected bone"""
    bl_idname = "witcher.pelvis_apply_numeric_offset"
    bl_label = "Apply Numeric Offset"
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=400)

    def draw(self, context):
        layout = self.layout
        layout.label(text="WARNING: This permanently modifies animation keyframe data.", icon='ERROR')
        layout.label(text="Save your .blend file before applying!", icon='FILE_BLEND')
        layout.separator()
        scene = context.scene
        layout.label(text="Location offset (Blender units):")
        layout.prop(scene, "witcher_pelvis_offset_loc", text="")
        layout.label(text="Rotation offset (degrees, XYZ Euler):")
        layout.prop(scene, "witcher_pelvis_offset_rot", text="")

    @classmethod
    def poll(cls, context):
        ao = context.active_object
        return ao is not None and ao.type == 'ARMATURE'

    def execute(self, context):
        import math
        scene = context.scene
        ao = context.active_object
        bone_name = str(getattr(scene, "witcher_pelvis_bone_name", "Pelvis") or "Pelvis")
        action = _get_pelvis_anim_action(ao, context)
        if action is None:
            self.report({'WARNING'}, "No active action found for the armature.")
            return {'CANCELLED'}

        offset_loc = list(getattr(scene, "witcher_pelvis_offset_loc", [0.0, 0.0, 0.0]))
        offset_rot_deg = list(getattr(scene, "witcher_pelvis_offset_rot", [0.0, 0.0, 0.0]))

        _apply_loc_delta_to_action_fcurves(action, ao, bone_name, offset_loc)

        rot_rad = [math.radians(d) for d in offset_rot_deg]
        pb = ao.pose.bones.get(bone_name)
        rotation_mode = getattr(pb, "rotation_mode", "QUATERNION") if pb else "QUATERNION"
        if rotation_mode == "QUATERNION":
            delta_euler = MEuler(rot_rad, 'XYZ')
            delta_q = delta_euler.to_quaternion()
            _apply_quat_delta_to_action_fcurves(action, ao, bone_name, delta_q)
        else:
            _apply_euler_delta_to_action_fcurves(action, ao, bone_name, rot_rad)

        self.report({'INFO'}, f"Offset applied to action '{action.name}' for bone '{bone_name}'.")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Shot-based camera workflow helpers
# ---------------------------------------------------------------------------

def _shot_camera_action_name(shot_index):
    return f"witcher_camera_shot_{int(shot_index):02d}"


def _next_shot_index(scene):
    existing_indices = {
        int(obj.get("witcher_shot_index", -1))
        for obj in getattr(scene, "objects", []) or []
        if getattr(obj, "type", None) == 'CAMERA' and obj.get("witcher_shot_index") is not None
    }
    return max(existing_indices, default=-1) + 1


def _iter_shot_markers(scene):
    """Return sorted [(shot_index, camera_obj, marker_frame)] for W3 shot cameras."""
    shots = []
    for marker in getattr(scene, "timeline_markers", []) or []:
        cam = getattr(marker, "camera", None)
        if cam is None or getattr(cam, "type", None) != 'CAMERA':
            continue
        shot_idx = cam.get("witcher_shot_index")
        if shot_idx is None:
            continue
        shots.append((int(shot_idx), cam, int(marker.frame)))
    shots.sort(key=lambda x: x[2])
    return shots


def _remove_shot_nla_strip(camera_armature, shot_index):
    """Remove the NLA strip whose action name matches the given shot index."""
    action_name = _shot_camera_action_name(shot_index)
    anim_data = getattr(camera_armature, "animation_data", None)
    if anim_data is None:
        return
    for track in getattr(anim_data, "nla_tracks", []) or []:
        for strip in list(getattr(track, "strips", []) or []):
            strip_action = getattr(strip, "action", None)
            if strip_action is not None and str(getattr(strip_action, "name", "") or "") == action_name:
                try:
                    track.strips.remove(strip)
                except Exception:
                    pass
                return


def _import_cutscene_camera_rig(context):
    """Import the cutscene camera rig if not already in scene. Returns armature or None."""
    existing = _find_camera_armature(context)
    if existing is not None:
        return existing
    scene = context.scene
    repo_path = _normalize_scratch_repo_path(
        getattr(scene, "witcher_cutscene_scratch_camera_repo_path", "") or SCRATCH_CAMERA_DEFAULT_REPO_PATH
    )
    if not repo_path:
        return None
    before_names = {obj.name for obj in bpy.data.objects}
    try:
        imported_obj = import_entity.import_ent_template(repo_file(repo_path), load_face_poses=False, import_apperance=0)
    except Exception:
        log.exception("Failed to auto-import cutscene camera '%s'.", repo_path)
        return None
    camera_armature = _find_armature_in_hierarchy(imported_obj, predicate=_is_camera_armature)
    if camera_armature is None:
        camera_armature = _find_new_imported_armature(before_names, predicate=_is_camera_armature)
    if camera_armature is None:
        return None
    _tag_scratch_cutscene_actor(
        camera_armature,
        actor_name="Camera",
        template_path=repo_path,
        actor_type="CAT_Camera",
        appearance="",
        use_mimic=False,
        imported_new=True,
    )
    ensure_camera_track_properties(camera_armature, track_names=CAMERA_TRACK_NAMES)
    camera_obj = find_camera_preview_object(camera_armature)
    if camera_obj is not None:
        setup_camera_preview_drivers(camera_armature, camera_obj)
    set_main_armature(scene, camera_armature)
    return camera_armature


class WITCH_OT_CutsceneNewShot(bpy.types.Operator):
    """Create a new shot: a Blender camera and a timeline marker at the current frame.
    Shots are baked onto the Witcher rig with 'Shots → Rig'."""
    bl_idname = "witcher.cutscene_new_shot"
    bl_label = "New Shot"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene

        shot_idx = _next_shot_index(scene)
        shot_number = shot_idx + 1
        cam_name = f"Shot {shot_number:02d}"

        cam_data = bpy.data.cameras.new(name=cam_name)
        cam_obj = bpy.data.objects.new(cam_name, cam_data)

        current_cam = getattr(scene, "camera", None)
        if current_cam is not None and getattr(current_cam, "type", None) == 'CAMERA':
            cam_data.lens = getattr(getattr(current_cam, "data", None), "lens", 50.0)
            cam_obj.matrix_world = current_cam.matrix_world.copy()

        scene.collection.objects.link(cam_obj)
        cam_obj["witcher_shot_index"] = shot_idx

        frame = scene.frame_current
        marker = scene.timeline_markers.new(cam_name, frame=frame)
        marker.camera = cam_obj

        scene.camera = cam_obj

        self.report({'INFO'}, f"Created {cam_name} at frame {frame}.")
        return {'FINISHED'}


class WITCH_OT_CutsceneUseSelectedCameraAsShot(bpy.types.Operator):
    """Use the selected Blender camera as a cutscene shot"""
    bl_idname = "witcher.cutscene_use_selected_camera_as_shot"
    bl_label = "Use Selected Camera"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        scene = getattr(context, "scene", None)
        active_obj = getattr(context, "active_object", None)
        scene_cam = getattr(scene, "camera", None) if scene is not None else None
        return (
            scene is not None
            and (
                getattr(active_obj, "type", None) == 'CAMERA'
                or getattr(scene_cam, "type", None) == 'CAMERA'
            )
        )

    def execute(self, context):
        scene = context.scene
        active_obj = getattr(context, "active_object", None)
        cam_obj = active_obj if getattr(active_obj, "type", None) == 'CAMERA' else getattr(scene, "camera", None)
        if cam_obj is None or getattr(cam_obj, "type", None) != 'CAMERA':
            self.report({'WARNING'}, "Select a camera or set the scene camera first.")
            return {'CANCELLED'}

        shot_idx = cam_obj.get("witcher_shot_index")
        if shot_idx is None:
            shot_idx = _next_shot_index(scene)
            cam_obj["witcher_shot_index"] = int(shot_idx)
        else:
            shot_idx = int(shot_idx)

        frame = int(getattr(scene, "frame_current", 0))
        marker = next(
            (
                m for m in getattr(scene, "timeline_markers", []) or []
                if getattr(m, "camera", None) == cam_obj
                and cam_obj.get("witcher_shot_index") is not None
            ),
            None,
        )
        marker_name = f"Shot {shot_idx + 1:02d}"
        if marker is None:
            marker = scene.timeline_markers.new(marker_name, frame=frame)
        else:
            marker.frame = frame
            marker.name = marker_name
        marker.camera = cam_obj
        scene.camera = cam_obj

        self.report({'INFO'}, f"Using '{cam_obj.name}' as {marker_name} at frame {frame}.")
        return {'FINISHED'}


class WITCH_OT_CutsceneScratchImportCamera(bpy.types.Operator):
    """Import and tag the standard cutscene camera entity"""
    bl_idname = "witcher.cutscene_scratch_import_camera"
    bl_label = "Import Cutscene Camera"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        repo_path = _normalize_scratch_repo_path(
            getattr(scene, "witcher_cutscene_scratch_camera_repo_path", "") or SCRATCH_CAMERA_DEFAULT_REPO_PATH
        )
        if not repo_path:
            self.report({'WARNING'}, "Camera entity path is empty.")
            return {'CANCELLED'}

        before_names = {obj.name for obj in bpy.data.objects}
        try:
            imported_obj = import_entity.import_ent_template(repo_file(repo_path), load_face_poses=False, import_apperance=0)
        except Exception as exc:
            log.exception("Failed to import cutscene camera '%s'.", repo_path)
            self.report({'ERROR'}, f"Could not import camera entity: {exc}")
            return {'CANCELLED'}

        camera_armature = _find_armature_in_hierarchy(imported_obj, predicate=_is_camera_armature)
        if camera_armature is None:
            camera_armature = _find_new_imported_armature(before_names, predicate=_is_camera_armature)
        if camera_armature is None:
            self.report({'WARNING'}, "Imported entity did not contain a Camera_Node rig.")
            return {'CANCELLED'}

        _tag_scratch_cutscene_actor(
            camera_armature,
            actor_name="Camera",
            template_path=repo_path,
            actor_type="CAT_Camera",
            appearance="",
            use_mimic=False,
            imported_new=True,
        )
        ensure_camera_track_properties(camera_armature, track_names=CAMERA_TRACK_NAMES)
        camera_obj = find_camera_preview_object(camera_armature)
        if camera_obj is not None:
            setup_camera_preview_drivers(camera_armature, camera_obj)
            scene.camera = camera_obj
        set_main_armature(scene, camera_armature)
        self.report({'INFO'}, f"Imported cutscene camera '{camera_armature.name}'.")
        return {'FINISHED'}


class WITCH_OT_CutsceneScratchAssignActor(bpy.types.Operator):
    """Tag the selected entity armature as a cutscene actor"""
    bl_idname = "witcher.cutscene_scratch_assign_actor"
    bl_label = "Assign Cutscene Actor"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _resolve_active_armature(context) is not None

    def execute(self, context):
        scene = context.scene
        armature_obj = _resolve_active_armature(context)
        if armature_obj is None:
            self.report({'WARNING'}, "Select an entity armature first.")
            return {'CANCELLED'}

        actor_name = _resolve_actor_display_name(armature_obj, "")
        if not actor_name:
            actor_name = _resolve_actor_display_name(
                armature_obj,
                getattr(scene, "witcher_cutscene_scratch_actor_name", ""),
            )
        actor_type = str(getattr(scene, "witcher_cutscene_scratch_actor_type", "CAT_Actor") or "CAT_Actor")
        template_path = _resolve_actor_template_path(armature_obj, "")
        if not template_path:
            template_path = _resolve_actor_template_path(
                armature_obj,
                getattr(scene, "witcher_cutscene_scratch_actor_template", ""),
            )
        appearance = _resolve_actor_appearance(armature_obj, "")
        if not appearance:
            appearance = _resolve_actor_appearance(
                armature_obj,
                getattr(scene, "witcher_cutscene_scratch_actor_appearance", ""),
            )
        use_mimic = bool(getattr(scene, "witcher_cutscene_scratch_use_mimic", True)) and _actor_has_mimic_setup(armature_obj)

        _tag_scratch_cutscene_actor(
            armature_obj,
            actor_name=actor_name,
            template_path=template_path,
            actor_type=actor_type,
            appearance=appearance,
            use_mimic=use_mimic,
            imported_new=False,
        )
        if use_mimic:
            import_cutscene._ensure_cutscene_face_setup(armature_obj)
        scene.witcher_cutscene_scratch_actor_name = actor_name
        scene.witcher_cutscene_scratch_actor_template = template_path
        scene.witcher_cutscene_scratch_actor_appearance = appearance
        self.report({'INFO'}, f"Assigned cutscene actor '{actor_name}'.")
        return {'FINISHED'}


class WITCH_OT_CutsceneScratchAddAction(bpy.types.Operator):
    """Add the selected action to the actor's cutscene NLA track"""
    bl_idname = "witcher.cutscene_scratch_add_action"
    bl_label = "Add Action To Cutscene"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _resolve_active_armature(context) is not None

    def execute(self, context):
        scene = context.scene
        actor_obj = _resolve_active_armature(context)
        if actor_obj is None:
            self.report({'WARNING'}, "Select an actor armature first.")
            return {'CANCELLED'}
        actor_name = _resolve_actor_display_name(actor_obj, "")
        if not actor_name:
            actor_name = _resolve_actor_display_name(actor_obj, getattr(scene, "witcher_cutscene_scratch_actor_name", ""))
        if not actor_name:
            self.report({'WARNING'}, "Actor name is empty.")
            return {'CANCELLED'}
        if not str(actor_obj.get("cutscene_actor_name", "") or "").strip():
            _tag_scratch_cutscene_actor(
                actor_obj,
                actor_name=actor_name,
                template_path=_resolve_actor_template_path(actor_obj, ""),
                actor_type=str(getattr(scene, "witcher_cutscene_scratch_actor_type", "CAT_Actor") or "CAT_Actor"),
                appearance=_resolve_actor_appearance(actor_obj, ""),
                use_mimic=bool(getattr(scene, "witcher_cutscene_scratch_use_mimic", True)) and _actor_has_mimic_setup(actor_obj),
                imported_new=False,
            )

        component = _resolve_scratch_component(context)
        target_armature = _resolve_component_armature(actor_obj, component)
        if target_armature is None:
            self.report({'WARNING'}, f"No armature found for component '{component}'.")
            return {'CANCELLED'}

        action = _resolve_scratch_action(context, target_armature)
        if action is None:
            self.report({'WARNING'}, "Choose an action or put one in the active action slot.")
            return {'CANCELLED'}
        # Auto-detect group name from existing strips so adding more strips joins the same multipart
        group_name = _scratch_action_group_name(action, target_armature, component)
        strip_length = int(getattr(scene, "witcher_cutscene_scratch_strip_length", 0) or 0)
        track, strip = _create_cutscene_action_strip(
            context,
            target_armature,
            action,
            actor_name,
            component,
            strip_length=strip_length if strip_length > 0 else None,
            group_name=group_name,
        )
        if strip is None:
            self.report({'WARNING'}, "Could not create cutscene NLA strip.")
            return {'CANCELLED'}
        _select_nla_strip(track, strip)

        _ensure_cutscene_animation_list_entry(scene, actor_name, component, group_name, action)

        self.report({'INFO'}, f"Added '{action.name}' as {actor_name}:{component}:{group_name}.")
        return {'FINISHED'}


class WITCH_OT_CutsceneUseImportNlaStrip(bpy.types.Operator):
    """Copy an existing import NLA strip into the cutscene workflow"""
    bl_idname = "witcher.cutscene_use_import_nla_strip"
    bl_label = "Use NLA Strip In Cutscene"
    bl_options = {'REGISTER', 'UNDO'}

    actor_object_name: StringProperty(default="")
    source_object_name: StringProperty(default="")
    track_name: StringProperty(default="")
    strip_name: StringProperty(default="")
    source_frame_start: FloatProperty(default=-1.0)
    component: StringProperty(default=SCRATCH_CUTSCENE_ROOT_COMPONENT)
    mute_source: BoolProperty(default=True)

    @classmethod
    def poll(cls, context):
        return getattr(context, "scene", None) is not None

    def execute(self, context):
        scene = context.scene
        actor_obj = bpy.data.objects.get(str(self.actor_object_name or ""))
        source_obj = bpy.data.objects.get(str(self.source_object_name or ""))
        if actor_obj is None or source_obj is None:
            self.report({'WARNING'}, "Could not resolve the actor or source armature.")
            return {'CANCELLED'}
        if getattr(source_obj, "type", None) != 'ARMATURE':
            self.report({'WARNING'}, "Source object is not an armature.")
            return {'CANCELLED'}

        anim_data = getattr(source_obj, "animation_data", None)
        track = getattr(anim_data, "nla_tracks", {}).get(self.track_name) if anim_data is not None else None
        if track is None:
            self.report({'WARNING'}, f"NLA track '{self.track_name}' was not found.")
            return {'CANCELLED'}
        source_strip = next(
            (strip for strip in getattr(track, "strips", []) or []
             if str(getattr(strip, "name", "") or "") == str(self.strip_name or "")
             and (self.source_frame_start < 0.0
                  or abs(float(getattr(strip, "frame_start", 0.0) or 0.0) - float(self.source_frame_start)) < 0.01)),
            None,
        )
        if source_strip is None:
            self.report({'WARNING'}, f"NLA strip '{self.strip_name}' was not found.")
            return {'CANCELLED'}
        source_action = getattr(source_strip, "action", None)
        if source_action is None:
            self.report({'WARNING'}, "The selected NLA strip has no action.")
            return {'CANCELLED'}

        actor_name = _resolve_actor_display_name(actor_obj, getattr(scene, "witcher_cutscene_scratch_actor_name", ""))
        if not actor_name:
            self.report({'WARNING'}, "Actor name is empty.")
            return {'CANCELLED'}
        if not str(actor_obj.get("cutscene_actor_name", "") or "").strip():
            _tag_scratch_cutscene_actor(
                actor_obj,
                actor_name=actor_name,
                template_path=_resolve_actor_template_path(actor_obj, ""),
                actor_type=str(getattr(scene, "witcher_cutscene_scratch_actor_type", "CAT_Actor") or "CAT_Actor"),
                appearance=_resolve_actor_appearance(actor_obj, ""),
                use_mimic=bool(getattr(scene, "witcher_cutscene_scratch_use_mimic", True)) and _actor_has_mimic_setup(actor_obj),
                imported_new=False,
            )

        component = str(self.component or SCRATCH_CUTSCENE_ROOT_COMPONENT)
        group_name = _import_strip_group_name(source_action, source_strip)
        strip_length = max(1.0, float(source_strip.frame_end) - float(source_strip.frame_start))
        cutscene_track, cutscene_strip = _create_cutscene_action_strip(
            context,
            source_obj,
            source_action,
            actor_name,
            component,
            start_frame=float(source_strip.frame_start),
            strip_length=strip_length,
            group_name=group_name,
        )
        if cutscene_strip is None:
            self.report({'WARNING'}, "Could not create cutscene NLA strip.")
            return {'CANCELLED'}

        try:
            _set_strip_action_range(
                cutscene_strip,
                action_start=float(getattr(source_strip, "action_frame_start", cutscene_strip.action_frame_start)),
                action_end=float(getattr(source_strip, "action_frame_end", cutscene_strip.action_frame_end)),
            )
        except Exception:
            pass
        _select_nla_strip(cutscene_track, cutscene_strip)
        if bool(self.mute_source):
            source_strip.mute = True

        _ensure_cutscene_animation_list_entry(scene, actor_name, component, group_name, source_action)
        self.report({'INFO'}, f"Using '{source_strip.name}' in cutscene as {actor_name}:{component}:{group_name}.")
        return {'FINISHED'}


class WITCH_OT_CutsceneScratchCreateCameraCut(bpy.types.Operator):
    """Create a camera cut action and NLA strip for scratch cutscene editing"""
    bl_idname = "witcher.cutscene_scratch_create_camera_cut"
    bl_label = "Create Camera Cut"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _find_camera_armature(context) is not None

    def execute(self, context):
        scene = context.scene
        camera_armature = _find_camera_armature(context)
        if camera_armature is None:
            self.report({'WARNING'}, "No camera rig selected.")
            return {'CANCELLED'}
        if not str(camera_armature.get("cutscene_actor_name", "") or "").strip():
            _tag_scratch_cutscene_actor(
                camera_armature,
                actor_name="Camera",
                template_path=getattr(scene, "witcher_cutscene_scratch_camera_repo_path", "") or SCRATCH_CAMERA_DEFAULT_REPO_PATH,
                actor_type="CAT_Camera",
                appearance="",
                use_mimic=False,
                imported_new=False,
            )

        scene_start = float(scene.frame_current)
        length = max(1, int(getattr(scene, "witcher_cutscene_scratch_strip_length", 0) or 0) or 60)
        action_name = f"{camera_armature.name}_camera_cut_{int(round(scene_start)):04d}"
        action = bpy.data.actions.new(action_name)
        actor_name = str(camera_armature.get("cutscene_actor_name", "") or "Camera")
        _tag_action_for_cutscene(action, actor_name, SCRATCH_CUTSCENE_ROOT_COMPONENT, action_name)

        track = _find_or_create_cutscene_track(camera_armature, SCRATCH_CUTSCENE_TRACK_NAME)
        _track, strip = _create_camera_cut_strip(
            camera_armature,
            track,
            action_name,
            scene_start,
            scene_start + float(length),
            action,
            action_start=0.0,
            action_end=float(length),
            settings={"blend_type": "COMBINE", "extrapolation": "NOTHING"},
        )

        camera_obj = _resolve_native_camera_target(context, camera_armature)
        if camera_obj is not None:
            _key_rig_from_camera(
                context,
                camera_armature,
                camera_obj,
                insert_key=True,
                key_frame=0.0,
                target_action=action,
            )
            _key_rig_from_camera(
                context,
                camera_armature,
                camera_obj,
                insert_key=True,
                key_frame=float(length),
                target_action=action,
            )
        _select_nla_strip(_track, strip)
        _sync_camera_cut_markers(scene, camera_armature)
        self.report({'INFO'}, f"Created camera cut {int(scene_start)}-{int(scene_start + length)}.")
        return {'FINISHED'}


class WITCH_OT_CutsceneScratchBakeSelectedCameraRange(bpy.types.Operator):
    """Bake the selected Blender camera over the timeline range into a cutscene camera NLA cut"""
    bl_idname = "witcher.cutscene_scratch_bake_selected_camera_range"
    bl_label = "Bake Selected Camera Range"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _find_camera_armature(context) is not None and _selected_blender_camera(context) is not None

    def execute(self, context):
        scene = context.scene
        camera_armature = _find_camera_armature(context)
        source_camera = _selected_blender_camera(context)
        if camera_armature is None:
            self.report({'WARNING'}, "No cutscene camera rig found.")
            return {'CANCELLED'}
        if source_camera is None:
            self.report({'WARNING'}, "Select a Blender camera to bake.")
            return {'CANCELLED'}

        if not str(camera_armature.get("cutscene_actor_name", "") or "").strip():
            _tag_scratch_cutscene_actor(
                camera_armature,
                actor_name="Camera",
                template_path=getattr(scene, "witcher_cutscene_scratch_camera_repo_path", "") or SCRATCH_CAMERA_DEFAULT_REPO_PATH,
                actor_type="CAT_Camera",
                appearance="",
                use_mimic=False,
                imported_new=False,
            )

        start_frame, end_frame = _scene_edit_range(scene)
        action_name = f"{camera_armature.name}_camera_range_{start_frame:04d}_{end_frame:04d}"
        action = _bake_camera_rig_action_from_camera(
            context,
            camera_armature,
            source_camera,
            start_frame,
            end_frame,
            action_name,
        )
        if action is None:
            self.report({'WARNING'}, "Could not bake the selected camera into the cutscene rig.")
            return {'CANCELLED'}

        actor_name = str(camera_armature.get("cutscene_actor_name", "") or "Camera")
        _tag_action_for_cutscene(action, actor_name, SCRATCH_CUTSCENE_ROOT_COMPONENT, action_name)
        track = _find_or_create_cutscene_track(camera_armature, SCRATCH_CUTSCENE_TRACK_NAME)
        _track, strip = _create_camera_cut_strip(
            camera_armature,
            track,
            action_name,
            float(start_frame),
            float(end_frame),
            action,
            action_start=0.0,
            action_end=float(max(1, end_frame - start_frame)),
            settings={"blend_type": "COMBINE", "extrapolation": "NOTHING"},
        )
        _select_nla_strip(_track, strip)
        _sync_camera_cut_markers(scene, camera_armature)
        _handoff_preview_camera_animation_to_rig(camera_armature, source_camera)
        self.report({'INFO'}, f"Baked '{source_camera.name}' to camera cut {start_frame}-{end_frame}.")
        return {'FINISHED'}


class WITCH_OT_CutsceneScratchValidate(bpy.types.Operator):
    """Validate the current scene for cutscene export"""
    bl_idname = "witcher.cutscene_scratch_validate"
    bl_label = "Validate Cutscene"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        lines, errors, warnings = _scratch_validation_lines(context)
        context.scene.witcher_cutscene_scratch_validation_report = "\n".join(lines)
        if errors:
            self.report({'ERROR'}, f"{len(errors)} cutscene validation error(s).")
            return {'CANCELLED'}
        if warnings:
            self.report({'WARNING'}, f"Cutscene valid with {len(warnings)} warning(s).")
        else:
            self.report({'INFO'}, "Cutscene setup looks exportable.")
        return {'FINISHED'}


def _anim_get_active_redkit_project(context):
    addon_prefs = get_all_addon_prefs(context)
    projects = getattr(addon_prefs, "redkit_projects", [])
    index = getattr(addon_prefs, "redkit_projects_index", 0)
    if projects and 0 <= index < len(projects):
        p = projects[index].path
        if p:
            return os.path.normpath(bpy.path.abspath(p))
    return None


def _anim_compute_full_export_path(workspace_root, repo_path):
    if not workspace_root or not repo_path:
        return None
    clean = repo_path.replace("/", os.sep).replace("\\", os.sep).lstrip(os.sep)
    return os.path.normpath(os.path.join(workspace_root, clean))


class WITCH_OT_AnimExportGotoProjectPath(bpy.types.Operator):
    """Create REDkit directory structure and navigate the file browser to the animation's game path"""
    bl_idname = "witcher.anim_export_goto_project_path"
    bl_label = "Go To Project Path"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        project_path = _anim_get_active_redkit_project(context)
        if not project_path:
            self.report({'WARNING'}, "No REDkit project configured. Set one in addon preferences.")
            return {'CANCELLED'}

        workspace_root = os.path.join(project_path, "workspace")
        repo_path = getattr(context.scene, "witcher_anim_export_repo_path", "")

        if repo_path:
            full_path = _anim_compute_full_export_path(workspace_root, repo_path)
        else:
            full_path = workspace_root

        if not full_path:
            self.report({'WARNING'}, "Could not compute project path.")
            return {'CANCELLED'}

        dir_path = os.path.dirname(full_path) if repo_path else full_path
        try:
            os.makedirs(dir_path, exist_ok=True)
        except Exception as e:
            self.report({'ERROR'}, f"Failed to create directories: {e}")
            return {'CANCELLED'}

        space = context.space_data
        if space and hasattr(space, 'params') and space.params:
            space.params.directory = dir_path.encode('utf-8')
            if repo_path:
                space.params.filename = os.path.basename(full_path)
            self.report({'INFO'}, f"Navigated to: {dir_path}")
        else:
            self.report({'INFO'}, f"Created path: {dir_path}")
        return {'FINISHED'}


class WITCH_OT_AnimSetRepoFromBrowser(bpy.types.Operator):
    """Set the animation repo path from the current file browser location"""
    bl_idname = "witcher.anim_set_repo_from_browser"
    bl_label = "Set Repo from Here"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        project_path = _anim_get_active_redkit_project(context)
        if not project_path:
            self.report({'ERROR'}, "No active REDkit project found.")
            return {'CANCELLED'}

        workspace_root = os.path.join(project_path, "workspace")
        space = context.space_data
        if not (space and hasattr(space, 'params') and space.params):
            self.report({'ERROR'}, "Must run from File Browser area.")
            return {'CANCELLED'}

        current_filename = space.params.filename
        try:
            current_dir = space.params.directory
            if isinstance(current_dir, bytes):
                current_dir = current_dir.decode('utf-8')
        except Exception as e:
            self.report({'ERROR'}, f"Failed to read browser path: {e}")
            return {'CANCELLED'}

        current_path_abs = os.path.abspath(current_dir)
        workspace_root_abs = os.path.abspath(workspace_root)

        if not current_path_abs.lower().startswith(workspace_root_abs.lower()):
            self.report({'WARNING'}, "Current folder is outside the active REDkit project workspace.")
            return {'CANCELLED'}

        try:
            rel_path = os.path.relpath(current_path_abs, workspace_root_abs)
        except ValueError:
            self.report({'ERROR'}, "Path is on a different drive.")
            return {'CANCELLED'}

        if rel_path == '.':
            rel_path = ""

        filename = current_filename
        if isinstance(filename, bytes):
            filename = filename.decode('utf-8')
        filename = filename or ""

        if rel_path and filename:
            full_repo_path = os.path.join(rel_path, filename)
        elif filename:
            full_repo_path = filename
        else:
            full_repo_path = rel_path

        full_repo_path = full_repo_path.replace('/', '\\')
        context.scene.witcher_anim_export_repo_path = full_repo_path
        self.report({'INFO'}, f"Repo path set to: {full_repo_path}")

        if current_filename:
            space.params.filename = current_filename
        for area in context.screen.areas:
            if area.type == 'FILE_BROWSER':
                area.tag_redraw()
        return {'FINISHED'}


class WITCH_OT_CutsceneExportGotoProjectPath(bpy.types.Operator):
    """Create REDkit directory structure and navigate the file browser to the cutscene's game path"""
    bl_idname = "witcher.cutscene_export_goto_project_path"
    bl_label = "Go To Project Path"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        project_path = _anim_get_active_redkit_project(context)
        if not project_path:
            self.report({'WARNING'}, "No REDkit project configured. Set one in addon preferences.")
            return {'CANCELLED'}

        workspace_root = os.path.join(project_path, "workspace")
        repo_path = getattr(context.scene, "witcher_cutscene_export_repo_path", "")

        if repo_path:
            full_path = _anim_compute_full_export_path(workspace_root, repo_path)
        else:
            full_path = workspace_root

        if not full_path:
            self.report({'WARNING'}, "Could not compute project path.")
            return {'CANCELLED'}

        dir_path = os.path.dirname(full_path) if repo_path else full_path
        try:
            os.makedirs(dir_path, exist_ok=True)
        except Exception as e:
            self.report({'ERROR'}, f"Failed to create directories: {e}")
            return {'CANCELLED'}

        space = context.space_data
        if space and hasattr(space, 'params') and space.params:
            space.params.directory = dir_path.encode('utf-8')
            if repo_path:
                space.params.filename = os.path.basename(full_path)
            self.report({'INFO'}, f"Navigated to: {dir_path}")
        else:
            self.report({'INFO'}, f"Created path: {dir_path}")
        return {'FINISHED'}


class WITCH_OT_CutsceneSetRepoFromBrowser(bpy.types.Operator):
    """Set the cutscene repo path from the current file browser location"""
    bl_idname = "witcher.cutscene_set_repo_from_browser"
    bl_label = "Set Repo from Here"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        project_path = _anim_get_active_redkit_project(context)
        if not project_path:
            self.report({'ERROR'}, "No active REDkit project found.")
            return {'CANCELLED'}

        workspace_root = os.path.join(project_path, "workspace")
        space = context.space_data
        if not (space and hasattr(space, 'params') and space.params):
            self.report({'ERROR'}, "Must run from File Browser area.")
            return {'CANCELLED'}

        current_filename = space.params.filename
        try:
            current_dir = space.params.directory
            if isinstance(current_dir, bytes):
                current_dir = current_dir.decode('utf-8')
        except Exception as e:
            self.report({'ERROR'}, f"Failed to read browser path: {e}")
            return {'CANCELLED'}

        current_path_abs = os.path.abspath(current_dir)
        workspace_root_abs = os.path.abspath(workspace_root)

        if not current_path_abs.lower().startswith(workspace_root_abs.lower()):
            self.report({'WARNING'}, "Current folder is outside the active REDkit project workspace.")
            return {'CANCELLED'}

        try:
            rel_path = os.path.relpath(current_path_abs, workspace_root_abs)
        except ValueError:
            self.report({'ERROR'}, "Path is on a different drive.")
            return {'CANCELLED'}

        if rel_path == '.':
            rel_path = ""

        filename = current_filename
        if isinstance(filename, bytes):
            filename = filename.decode('utf-8')
        filename = filename or ""

        if rel_path and filename:
            full_repo_path = os.path.join(rel_path, filename)
        elif filename:
            full_repo_path = filename
        else:
            full_repo_path = rel_path

        full_repo_path = full_repo_path.replace('/', '\\')
        context.scene.witcher_cutscene_export_repo_path = full_repo_path
        self.report({'INFO'}, f"Repo path set to: {full_repo_path}")

        if current_filename:
            space.params.filename = current_filename
        for area in context.screen.areas:
            if area.type == 'FILE_BROWSER':
                area.tag_redraw()
        return {'FINISHED'}


class TOOL_UL_List(UIList):
    """Demo UIList."""
    bl_idname = "TOOL_UL_List"
    layout_type = "DEFAULT" # could be "COMPACT" or "GRID"
    # list_id ToDo

    use_name_reverse: bpy.props.BoolProperty(
        name="Reverse Name",
        default=False,
        options=set(),
        description="Reverse name sort order",
    )

    use_order_name: bpy.props.BoolProperty(
        name="Name",
        default=False,
        options=set(),
        description="Sort groups by their name (case-insensitive)",
    )

    filter_string: bpy.props.StringProperty(
        name="filter_string",
        default = "",
        description="Filter string for name"
    )

    filter_invert: bpy.props.BoolProperty(
        name="Invert",
        default = False,
        options=set(),
        description="Invert Filter"
    )

    def filter_items(self, context,
                    data, # Data from which to take Collection property
                    property # Identifier of property in data, for the collection
        ):


        items = getattr(data, property)
        if not len(items):
            return [], []

        # https://docs.blender.org/api/current/bpy.types.UI_UL_list.html
        # helper functions for handling UIList objects.
        if self.filter_string:
            flt_flags = bpy.types.UI_UL_list.filter_items_by_name(
                    self.filter_string,
                    self.bitflag_filter_item,
                    items,
                    propname="name",
                    reverse=self.filter_invert)
        else:
            flt_flags = [self.bitflag_filter_item] * len(items)

        # https://docs.blender.org/api/current/bpy.types.UI_UL_list.html
        # helper functions for handling UIList objects.
        if self.use_order_name:
            flt_neworder = bpy.types.UI_UL_list.sort_items_by_name(items, "name")
            if self.use_name_reverse:
                flt_neworder.reverse()
        else:
            flt_neworder = []


        return flt_flags, flt_neworder

    def draw_filter(self, context,
                    layout # Layout to draw the item
        ):

        row = layout.row(align=True)
        row.prop(self, "filter_string", text="Filter", icon="VIEWZOOM")
        row.prop(self, "filter_invert", text="", icon="ARROW_LEFTRIGHT")


        row = layout.row(align=True)
        row.label(text="Order by:")
        row.prop(self, "use_order_name", toggle=True)

        icon = 'TRIA_UP' if self.use_name_reverse else 'TRIA_DOWN'
        row.prop(self, "use_name_reverse", text="", icon=icon)

    def draw_item(self, context,
                    layout, # Layout to draw the item
                    data, # Data from which to take Collection property
                    item, # Item of the collection property
                    icon, # Icon of the item in the collection
                    active_data, # Data from which to take property for the active element
                    active_propname, # Identifier of property in active_data, for the active element
                    index, # Index of the item in the collection - default 0
                    flt_flag # The filter-flag result for this item - default 0
            ):

        # Make sure your code supports all 3 layout types
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            layout.label(text=item.name)
        elif self.layout_type in {'GRID'}:
            layout.alignment = 'CENTER'
            layout.label(text="")

class TOOL_OT_List_LoadAnim(Operator):
    """ Add an Item to the UIList"""
    bl_idname = "witcher.list_loadanim"
    bl_label = "Load"
    bl_description = "Load the selected animation"

    action: StringProperty(default="default")
    @classmethod
    def poll(cls, context):
        return context.scene

    def execute(self, context):
        scene = context.scene
        action = self.action
        if "load" == action or "load_cutscene" == action:
            if "load_cutscene" == action:
                list_name = "witcher_w2cutscene_list"
                index_name = "witcher_w2cutscene_list_index"
                working_list_path = context.scene.witcher_loaded_w2cutscene_path
            else:
                list_name = "witcher_w2anims_list"
                index_name = "witcher_w2anims_list_index"
                working_list_path = context.scene.witcher_loaded_w2anims_path
            log.debug("load anim")
            main_arm_obj = _find_character_armature(context)
            item, _safe_index = _get_selected_collection_item(scene, list_name, index_name)
            if item is not None:
                anim_name = item.name 
                fdir_abs = working_list_path  #repo_file(fdir) #!REMOVE !TODO link witcher_loaded_w2anims_path to an object? or keep for cutscene?
                #!REMOVE TODO load anim on click or highlight instead of having to hit the load button

                if not main_arm_obj:
                    self.report({'ERROR'}, "No armature found. Select or import a rig first.")
                    return {'CANCELLED'}
                
                _dirpath, file = os.path.split(fdir_abs)
                _basename, ext = os.path.splitext(file)
                try:
                    if ext.lower() == '.json':
                        _resolved_main_arm_obj, target_armatures, rig_path, _face_animation = resolve_animation_load_context(
                            context,
                            anim_name,
                            fdir=fdir_abs,
                            main_arm_obj=main_arm_obj,
                        )
                        animset = import_anims.import_w3_animSet(fdir_abs, rig_path)
                        #import json by name
                        target_obj = target_armatures if len(target_armatures) > 1 else target_armatures[0]
                        import_anims.import_from_list_item(
                            context,
                            item,
                            animset,
                            target_obj=target_obj,
                        )
                    else:
                        load_anim_into_scene(
                            context,
                            anim_name,
                            fdir_abs,
                            main_arm_obj,
                            face_target_mode="owner" if action == "load_cutscene" else "auto",
                            nla_mode=_scene_nla_mode(context.scene),
                        )
                except FileNotFoundError as e:
                    self.report({'ERROR'}, str(e))
                    return {'CANCELLED'}

                # Apply root orientation if enabled
                auto_orient = getattr(context.scene, 'witcher_auto_orient_root', False)
                log.info(f"Auto orient root setting: {auto_orient}")
                if auto_orient and main_arm_obj:
                    apply_root_orientation(main_arm_obj)

                #import_anims.import_from_list_item(context, item)
            # context.scene.witcher_w2anims_list.add()
            else:
                self.report({'ERROR'}, "No animation selected.")
                return {'CANCELLED'}
        elif "clear" == action:
            log.debug("Debug Clear")
            bpy.context.scene.witcher_w2anims_list.clear()
            bpy.context.scene.witcher_w2anims_list_index = -1
        return {'FINISHED'}


class TOOL_OT_List_Add(Operator):
    """ Add an Item to the UIList"""
    bl_idname = "witcher.list_add"
    bl_label = "Add"
    bl_description = "add a new item to the list."

    @classmethod
    def poll(cls, context):
        """ We can only add items to the list of an active object
            but the list may be empty or doesn't yet exist so
            just this function can only check if there is an active object
        """
        return context.scene

    def execute(self, context):
        context.scene.witcher_w2anims_list.add()
        return {'FINISHED'}

class TOOL_OT_List_Remove(Operator):
    """ Add an Item to the UIList"""
    bl_idname = "witcher.list_remove"
    bl_label = "Add"
    bl_description = "Remove an new item from the list."

    @classmethod
    def poll(cls, context):
        """ We can only remove items from the list of an active object
            that has items in it, but the list may be empty or doesn't
            yet exist and there's no reason to remove an item from an empty
            list.
        """
        return (context.scene
                and context.scene.witcher_w2anims_list
                and len(context.scene.witcher_w2anims_list))

    def execute(self, context):
        alist = context.scene.witcher_w2anims_list
        index = context.scene.witcher_w2anims_list_index
        context.scene.witcher_w2anims_list.remove(index)
        context.scene.witcher_w2anims_list_index = min(max(0, index - 1), len(alist) - 1)
        return {'FINISHED'}

class TOOL_OT_List_Reorder(Operator):
    """ Add an Item to the UIList"""
    bl_idname = "witcher.list_reorder"
    bl_label = "Add"
    bl_description = "add a new item to the list."

    direction: bpy.props.EnumProperty(items=(('UP', 'Up', ""),
                                              ('DOWN', 'Down', ""),))

    @classmethod
    def poll(cls, context):
        """ No reason to try to reorder a list with fewer than
            two items in it.
        """
        return (context.scene
                and context.scene.witcher_w2anims_list
                and len(context.scene.witcher_w2anims_list) > 1)

    def move_index(self):
        """ Move index of an item while clamping it. """
        index = bpy.context.scene.witcher_w2anims_list_index
        list_length = len(bpy.context.scene.witcher_w2anims_list) - 1
        new_index = index + (-1 if self.direction == 'UP' else 1)

        bpy.context.scene.witcher_w2anims_list_index = max(0, min(new_index, list_length))

    def execute(self, context):
        alist = context.scene.witcher_w2anims_list
        index = context.scene.witcher_w2anims_list_index

        neighbor = index + (-1 if self.direction == 'UP' else 1)
        alist.move(neighbor, index)
        self.move_index()
        return {'FINISHED'}

class ButtonOperatorImportW2Anims(bpy.types.Operator, ImportHelper):
    """Import W2 Anims"""
    bl_idname = "witcher.import_w2_anims_json"
    bl_label = "W2 Anims"
    filename_ext = ".w2anims"
    def execute(self, context):
        fdir = self.filepath
        if Path(fdir).is_dir():
            self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
            return {'CANCELLED'}
        import_anims.start_import(context, fdir)
        return {'FINISHED'}
    def invoke(self, context, event):
        UNCOOK_PATH = os.path.join(get_uncook_path(context),"animations\\")
        if os.path.exists(UNCOOK_PATH):
            self.filepath = UNCOOK_PATH if self.filepath == '' else self.filepath
        return ImportHelper.invoke(self, context, event)

import mathutils
class ButtonOperatorToggloRootMotion(bpy.types.Operator):
    """Toggle Root Motion"""
    bl_idname = "witcher.toggle_motion"
    bl_label = "Toggle Root Motion"
    def execute(self, context):
        # Check if there is an active object and if it's an armature
        if context.active_object and context.active_object.type == 'ARMATURE':
            armature = context.active_object.data

            # Store the original mode
            original_mode = context.mode

            # Switch to Edit mode if not already in Edit mode
            if original_mode != 'EDIT':
                bpy.ops.object.mode_set(mode='EDIT')

            # Check if the bone already exists
            if "RootMotion" in armature.edit_bones:
                # Bone exists, remove it
                armature.edit_bones.remove(armature.edit_bones["RootMotion"])
                log.info("Bone 'RootMotion' removed from the armature.")
            else:
                root_bone = armature.edit_bones['Root']
                # Bone doesn't exist, create it as the first bone
                new_bone = armature.edit_bones.new("RootMotion")
                new_bone.head = root_bone.head.copy()
                new_bone.tail = root_bone.tail.copy()
                #rotation_matrix = mathutils.Matrix.Rotation(-90.0, 3, 'Y')
                #new_bone.transform(rotation_matrix)
                root_bone.parent = new_bone
                armature.edit_bones.active = new_bone


                log.info("Bone 'RootMotion' added to the armature.")

            # Update the scene
            context.view_layer.update()

            # Return to the original mode
            bpy.ops.object.mode_set(mode=original_mode)
        else:
            log.warning("No active armature selected.")

        return {'FINISHED'}


class WITCH_OT_ToggleRootMotionDrivers(bpy.types.Operator):
    """Toggle root motion using drivers (no extra bones) - switches between world movement and in-place playback"""
    bl_idname = "witcher.toggle_root_motion_drivers"
    bl_label = "Toggle In-Place (Drivers)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'ARMATURE'

    def execute(self, context):
        from ..importers.motion_tools import (
            setup_root_motion_drivers,
            has_root_motion_drivers,
            set_root_motion_mode,
            get_root_motion_mode
        )

        armature = context.active_object

        # Setup drivers if not present
        if not has_root_motion_drivers(armature):
            if not setup_root_motion_drivers(armature):
                self.report({'ERROR'}, "Could not setup drivers - missing Root or Trajectory bone")
                return {'CANCELLED'}

        # Toggle mode
        current = get_root_motion_mode(armature)
        new_mode = 'IN_PLACE' if current == 'ROOT_MOTION' else 'ROOT_MOTION'
        set_root_motion_mode(armature, new_mode)

        mode_text = 'ON (World Movement)' if new_mode == 'ROOT_MOTION' else 'OFF (In-Place)'
        self.report({'INFO'}, f"Root Motion: {mode_text}")
        return {'FINISHED'}


class WITCH_OT_RemoveRootMotionDrivers(bpy.types.Operator):
    """Remove root motion drivers from Root bone"""
    bl_idname = "witcher.remove_root_motion_drivers"
    bl_label = "Remove Root Motion Drivers"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if not context.active_object or context.active_object.type != 'ARMATURE':
            return False
        from ..importers.motion_tools import has_root_motion_drivers
        return has_root_motion_drivers(context.active_object)

    def execute(self, context):
        from ..importers.motion_tools import remove_root_motion_drivers
        armature = context.active_object
        remove_root_motion_drivers(armature)
        self.report({'INFO'}, "Root motion drivers removed")
        return {'FINISHED'}


# =============================================================================
# CONTROLLER EMPTY APPROACH (Recommended)
# =============================================================================

class WITCH_OT_SetupRootMotionController(bpy.types.Operator):
    """Create a controller empty that follows Trajectory for root motion control"""
    bl_idname = "witcher.setup_root_motion_controller"
    bl_label = "Setup Root Motion Controller"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if not context.active_object or context.active_object.type != 'ARMATURE':
            return False
        from ..importers.motion_tools import has_root_motion_controller
        return not has_root_motion_controller(context.active_object)

    def execute(self, context):
        from ..importers.motion_tools import setup_root_motion_controller
        armature = context.active_object

        controller = setup_root_motion_controller(armature)
        if not controller:
            self.report({'ERROR'}, "Could not create controller - missing Trajectory bone")
            return {'CANCELLED'}

        self.report({'INFO'}, f"Created root motion controller: {controller.name}")
        return {'FINISHED'}


class WITCH_OT_RemoveRootMotionController(bpy.types.Operator):
    """Remove the root motion controller empty and unparent armature"""
    bl_idname = "witcher.remove_root_motion_controller"
    bl_label = "Remove Controller"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if not context.active_object or context.active_object.type != 'ARMATURE':
            return False
        from ..importers.motion_tools import has_root_motion_controller
        return has_root_motion_controller(context.active_object)

    def execute(self, context):
        from ..importers.motion_tools import remove_root_motion_controller
        armature = context.active_object

        if remove_root_motion_controller(armature):
            self.report({'INFO'}, "Root motion controller removed")
            return {'FINISHED'}
        else:
            self.report({'ERROR'}, "Could not remove controller")
            return {'CANCELLED'}


class WITCH_OT_ToggleRootMotionController(bpy.types.Operator):
    """Toggle between root motion (character moves) and in-place (character stays put) modes"""
    bl_idname = "witcher.toggle_root_motion_controller"
    bl_label = "Toggle Root Motion"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if not context.active_object or context.active_object.type != 'ARMATURE':
            return False
        from ..importers.motion_tools import has_root_motion_controller
        return has_root_motion_controller(context.active_object)

    def execute(self, context):
        from ..importers.motion_tools import toggle_controller_mode
        armature = context.active_object

        success, new_mode = toggle_controller_mode(armature)
        if success:
            mode_text = 'In-Place (Trajectory Counteracted)' if new_mode == 'IN_PLACE' else 'Root Motion (Natural Movement)'
            self.report({'INFO'}, f"Mode: {mode_text}")
            return {'FINISHED'}
        else:
            self.report({'ERROR'}, "Could not toggle mode")
            return {'CANCELLED'}


def apply_root_orientation(armature_obj):
    """
    Orient Root bone so the character faces the direction of its natural movement.
    """
    if not armature_obj or armature_obj.type != 'ARMATURE':
        return False

    action = _resolve_root_orientation_action(armature_obj)
    if action is None:
        return False

    pose_bones = armature_obj.pose.bones
    if "Root" not in pose_bones:
        log.warning("Auto Orient Root skipped: no 'Root' bone found in armature")
        return False

    # Check if already applied — don't apply twice
    if action.get("root_orientation_applied", False):
        log.info(f"Root orientation already applied to {action.name}")
        return True

    root_bone = pose_bones["Root"]

    # The swap copies the Trajectory bone's rotation onto Root keeping the world-space movement direction identical to the motion extraction.
    initial_quat = _read_root_first_frame_quat(action, armature_obj)

    # Step 2: Remove ALL fcurves for Root bone (rotation, location, scale)
    root_data_paths = [
        'pose.bones["Root"].rotation_quaternion',
        'pose.bones["Root"].rotation_euler',
        'pose.bones["Root"].location',
        'pose.bones["Root"].scale',
    ]

    fcurves_to_remove = []
    for fc in iter_action_fcurves(action, target=armature_obj):
        if fc.data_path in root_data_paths:
            fcurves_to_remove.append(fc)

    for fc in fcurves_to_remove:
        remove_action_fcurve(action, fc, target=armature_obj)

    log.info(f"Removed {len(fcurves_to_remove)} fcurves from Root bone")

    # ------------------------------------------------------------------
    # Step 3: Key Root with the preserved first-frame rotation (static)
    # ------------------------------------------------------------------
    root_bone.rotation_mode = 'QUATERNION'

    quat_path = 'pose.bones["Root"].rotation_quaternion'
    loc_path = 'pose.bones["Root"].location'

    quat_values = [initial_quat.w, initial_quat.x, initial_quat.y, initial_quat.z]
    for i, val in enumerate(quat_values):
        fc = new_action_fcurve(action, armature_obj, data_path=quat_path, index=i, group_name="Root")
        kp = fc.keyframe_points.insert(1, val)
        kp.interpolation = 'LINEAR'

    for i in range(3):
        fc = new_action_fcurve(action, armature_obj, data_path=loc_path, index=i, group_name="Root")
        kp = fc.keyframe_points.insert(1, 0.0)
        kp.interpolation = 'LINEAR'

    # Mark as applied
    action["root_orientation_applied"] = True

    log.info(f"Applied root orientation to {action.name}")
    log.info(f"  Root initial quaternion: {initial_quat}")

    return True


def _read_root_first_frame_quat(action, armature_obj=None):
    """Read Root bone's rotation at its first keyframe, returned as a Quaternion.

    Reads fcurve values directly (no frame_set overhead) and handles both
    quaternion and euler rotation modes.  Returns identity if no Root rotation
    fcurves exist.
    """
    quat_path = 'pose.bones["Root"].rotation_quaternion'
    euler_path = 'pose.bones["Root"].rotation_euler'

    first_frame = None
    quat_curves = {}   # array_index → fcurve
    euler_curves = {}  # array_index → fcurve
    euler_order = 'XYZ'

    fcurve_iter = (iter_action_fcurves(action, target=armature_obj)
                   if armature_obj is not None else action.fcurves)
    for fc in fcurve_iter:
        if fc.data_path == quat_path and fc.keyframe_points:
            f = fc.keyframe_points[0].co[0]
            if first_frame is None or f < first_frame:
                first_frame = f
            quat_curves[fc.array_index] = fc
        elif fc.data_path == euler_path and fc.keyframe_points:
            f = fc.keyframe_points[0].co[0]
            if first_frame is None or f < first_frame:
                first_frame = f
            euler_curves[fc.array_index] = fc

    if first_frame is None:
        log.info("_read_root_first_frame_quat: no Root rotation fcurves, returning identity")
        return mathutils.Quaternion()

    if quat_curves:
        w = quat_curves[0].evaluate(first_frame) if 0 in quat_curves else 1.0
        x = quat_curves[1].evaluate(first_frame) if 1 in quat_curves else 0.0
        y = quat_curves[2].evaluate(first_frame) if 2 in quat_curves else 0.0
        z = quat_curves[3].evaluate(first_frame) if 3 in quat_curves else 0.0
        return mathutils.Quaternion((w, x, y, z)).normalized()

    # Euler fallback
    ex = euler_curves[0].evaluate(first_frame) if 0 in euler_curves else 0.0
    ey = euler_curves[1].evaluate(first_frame) if 1 in euler_curves else 0.0
    ez = euler_curves[2].evaluate(first_frame) if 2 in euler_curves else 0.0
    return mathutils.Euler((ex, ey, ez), euler_order).to_quaternion()


class WITCH_OT_ApplyRootOrientation(bpy.types.Operator):
    """Apply orientation correction to Root bone animation (Z+ up, X+ towards Y-)"""
    bl_idname = "witcher.apply_root_orientation"
    bl_label = "Orient Root"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if not obj or obj.type != 'ARMATURE' or not obj.animation_data:
            return False
        pose_bones = getattr(getattr(obj, "pose", None), "bones", None)
        if pose_bones is None or "Root" not in pose_bones:
            return False
        # Allow if there's an active action OR NLA tracks with strips
        if obj.animation_data.action:
            return True
        if obj.animation_data.nla_tracks:
            for track in obj.animation_data.nla_tracks:
                if track.strips:
                    return True
        return False

    def execute(self, context):
        armature = context.active_object

        # Check if already applied first
        action = None
        if armature.animation_data:
            action = armature.animation_data.action
            if action is None and armature.animation_data.nla_tracks:
                for track in armature.animation_data.nla_tracks:
                    for strip in track.strips:
                        if strip.action:
                            action = strip.action
                            break
                    if action:
                        break

        if action and action.get("root_orientation_applied", False):
            self.report({'INFO'}, f"Root orientation already applied to '{action.name}'")
            return {'FINISHED'}

        if apply_root_orientation(armature):
            self.report({'INFO'}, "Root orientation applied to animation")
        else:
            self.report({'WARNING'}, "Could not apply root orientation - check console")
        return {'FINISHED'}


class WITCH_OT_ResampleAnimation(bpy.types.Operator):
    """Resample the active animation to a uniform frame rate using Blender's bake.
    This fixes shaking/jittering caused by bones having different sample rates.
    The original keyframes are replaced with baked values at each frame."""
    bl_idname = "witcher.resample_animation"
    bl_label = "Resample Animation"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (obj and obj.type == 'ARMATURE' and 
                obj.animation_data and obj.animation_data.action)
    
    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)
    
    def execute(self, context):
        obj = context.active_object
        action = obj.animation_data.action
        
        # Get frame range
        frame_start = int(action.frame_range[0])
        frame_end = int(action.frame_range[1])
        
        # Store original action name
        original_name = action.name
        
        # Use Blender's built-in bake operator
        try:
            bpy.ops.nla.bake(
                frame_start=frame_start,
                frame_end=frame_end,
                step=1,
                only_selected=False,
                visual_keying=False,
                clear_constraints=False,
                clear_parents=False,
                use_current_action=True,
                clean_curves=False,
                bake_types={'POSE'}
            )
            self.report({'INFO'}, f"Resampled animation '{original_name}' to frames {frame_start}-{frame_end}")
        except Exception as e:
            self.report({'ERROR'}, f"Bake failed: {str(e)}")
            return {'CANCELLED'}
        
        return {'FINISHED'}


class WITCH_OT_BakePelvisToTrajectory(bpy.types.Operator):
    """Bake Pelvis XY locomotion onto the Trajectory bone (Z=0, grounded).

    Use before export when your animation has root motion on the Pelvis
    instead of the Trajectory bone. The Pelvis world position is preserved —
    its XY+Yaw locomotion transfers to Trajectory so motion extraction works."""
    bl_idname = "witcher.bake_pelvis_to_trajectory"
    bl_label = "Bake Pelvis → Trajectory"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if not obj or obj.type != 'ARMATURE':
            return False
        if not obj.animation_data or not obj.animation_data.action:
            return False
        bones = getattr(getattr(obj, "pose", None), "bones", None)
        if bones is None:
            return False
        bone_names_lower = {b.name.lower() for b in bones}
        return 'pelvis' in bone_names_lower and 'trajectory' in bone_names_lower

    def _find_bone(self, armature, name_lower):
        for b in armature.pose.bones:
            if b.name.lower() == name_lower:
                return b
        return None

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        from mathutils import Matrix
        armature = context.active_object
        action = armature.animation_data.action
        frame_start = int(action.frame_range[0])
        frame_end = int(action.frame_range[1])
        original_frame = context.scene.frame_current

        pelvis = self._find_bone(armature, 'pelvis')
        traj = self._find_bone(armature, 'trajectory')
        if not pelvis or not traj:
            self.report({'ERROR'}, "Could not find Pelvis or Trajectory bone")
            return {'CANCELLED'}

        # First pass: record Pelvis armature-space matrices at every frame
        # (read before we modify any keyframes so we get the original motion)
        pelvis_matrices = {}
        for frame in range(frame_start, frame_end + 1):
            context.scene.frame_set(frame)
            pelvis_matrices[frame] = pelvis.matrix.copy()

        # Second pass: bake Trajectory from Pelvis XY+Yaw, then re-seat Pelvis
        for frame in range(frame_start, frame_end + 1):
            context.scene.frame_set(frame)
            m = pelvis_matrices[frame]

            x = m.translation.x
            y = m.translation.y
            # Z rotation = yaw; XYZ euler is reliable for typical locomotion
            yaw = m.to_euler('XYZ').z

            # Trajectory carries XY + yaw only; Z stays at 0 (ground plane)
            traj_mat = Matrix.Translation((x, y, 0.0)) @ Matrix.Rotation(yaw, 4, 'Z')
            traj.matrix = traj_mat

            traj.keyframe_insert(data_path='location', frame=frame)
            if traj.rotation_mode == 'QUATERNION':
                traj.keyframe_insert(data_path='rotation_quaternion', frame=frame)
            elif traj.rotation_mode == 'AXIS_ANGLE':
                traj.keyframe_insert(data_path='rotation_axis_angle', frame=frame)
            else:
                traj.keyframe_insert(data_path='rotation_euler', frame=frame)

            # Re-evaluate the dependency graph so the new Trajectory keyframes
            # propagate through the parent chain before we re-seat Pelvis
            context.view_layer.update()

            # Set Pelvis back to its original world position; Blender solves for
            # the new local transform relative to the updated Trajectory parent
            pelvis.matrix = m
            pelvis.keyframe_insert(data_path='location', frame=frame)
            if pelvis.rotation_mode == 'QUATERNION':
                pelvis.keyframe_insert(data_path='rotation_quaternion', frame=frame)
            elif pelvis.rotation_mode == 'AXIS_ANGLE':
                pelvis.keyframe_insert(data_path='rotation_axis_angle', frame=frame)
            else:
                pelvis.keyframe_insert(data_path='rotation_euler', frame=frame)

        context.scene.frame_set(original_frame)
        self.report(
            {'INFO'},
            f"Baked {frame_end - frame_start + 1} frames: Pelvis XY+Yaw → Trajectory",
        )
        return {'FINISHED'}


class WITCHER_PT_animset_panel(WITCH_PT_Base, Panel):
    # Promoted to top-level: no longer hidden inside Character Appearances.
    bl_idname = "WITCHER_PT_animset_panel"
    bl_label = "Animation"
    bl_description = "Animation sets, clips, speech, and playback controls"
    bl_options = set()  # Open by default — prominent, not collapsed

    def draw_header(self, context):
        self.layout.label(text="", icon='ACTION')

    def draw_header_preset(self, context):
        text = _get_animation_panel_header_status(context)
        ui_scale = context.preferences.system.ui_scale
        max_chars = max(8, int((context.region.width - 135 * ui_scale) / (7 * ui_scale)))
        if len(text) > max_chars:
            text = text[:max_chars - 3] + "..."
        self.layout.label(text=text)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = False  # Must be False so prop_enum() shows text ON buttons, not beside them
        layout.use_property_decorate = False
        scene = context.scene
        if scene is None:
            return

        def section(section_id, label, icon, default_closed=False):
            container = layout.box()
            header, body = container.panel(section_id, default_closed=default_closed)
            header.label(text=label, icon=icon)
            return body

        display_armature = _find_character_armature(context)
        rig_settings = None
        if display_armature and display_armature.type == 'ARMATURE' and hasattr(display_armature.data, "witcherui_RigSettings"):
            rig_settings = display_armature.data.witcherui_RigSettings

        # --- Active context banner (always visible at the top of this panel) ---
        ctx_box = layout.box()
        ctx_row = ctx_box.row(align=True)
        if display_armature:
            ctx_row.label(text=display_armature.name, icon='ARMATURE_DATA')
            frame = scene.frame_current
            nla_now, _ = export_anims.get_nla_action_at_frame(display_armature, frame=frame)
            if nla_now:
                ctx_row.label(text=nla_now.name, icon='ACTION')
            else:
                ctx_row.label(text="No action @ frame", icon='ACTION')
        else:
            ctx_row.alert = True
            ctx_row.label(text="No character selected", icon='INFO')

        # --- Section navigator: 3 big highlighted buttons ---
        anim_tab = getattr(scene, "witcher_anim_tab", "CLIPS")
        nav_row = layout.row(align=True)
        nav_row.scale_y = 1.6
        nav_row.prop_enum(scene, "witcher_anim_tab", 'SETS')
        nav_row.prop_enum(scene, "witcher_anim_tab", 'CLIPS')
        nav_row.prop_enum(scene, "witcher_anim_tab", 'SPEECH')
        layout.separator(factor=0.3)

        # ===================== SETS TAB =====================
        if anim_tab == "SETS":
            loaded_set = _get_loaded_animset_ui_state(context)

            # Current animset status + explicit import entry point (TW2/TW3 .w2anims)
            status_box = layout.box()
            head_row = status_box.row(align=True)
            head_row.label(text="Current Loaded Set", icon='CHECKMARK' if loaded_set["has_loaded_set"] else 'INFO')
            if loaded_set["has_loaded_set"] and loaded_set["source_badge"]:
                badge_row = head_row.row(align=True)
                badge_row.enabled = False
                badge_row.label(text=f"[{loaded_set['source_badge']}]")
            action_row = status_box.row(align=True)
            action_row.operator(ButtonOperatorImportW2Anims.bl_idname, text="Import Set (.w2anims)", icon='IMPORT')
            if loaded_set["has_loaded_set"]:
                action_row.prop_enum(scene, "witcher_anim_tab", 'CLIPS', text="Clips")

            if loaded_set["has_loaded_set"]:
                title_row = status_box.row(align=True)
                title_row.label(text=loaded_set["display_name"] or "Loaded animation set", icon='ACTION')
                title_row.label(text=f"{loaded_set['clip_count']} clips", icon='ANIM_DATA')
                status_box.label(text="Load clip entries in the Clips tab (next to Sets).", icon='INFO')
                if loaded_set["display_path"]:
                    path_row = status_box.row()
                    path_row.scale_y = 0.75
                    path_row.label(text=loaded_set["display_path"], icon='FILE')
            else:
                status_box.label(text="No .w2anims set loaded yet.", icon='INFO')
                status_box.label(text="Load a character-linked set below, or import a .w2anims file.", icon='IMPORT')

            if not rig_settings:
                layout.label(text="Select/import a character armature to browse character-linked sets.", icon='INFO')
            else:
                # Entity info header
                info_box = layout.box()
                info_box.label(text=f"{display_armature.name}", icon='ARMATURE_DATA')
                skeleton = getattr(rig_settings, 'main_entity_skeleton', '') or '-'
                info_row = info_box.row()
                info_row.scale_y = 0.75
                info_row.label(text=f"Skeleton: {skeleton}", icon='BONE_DATA')

                if not rig_settings.animset_list:
                    layout.label(text="No animation sets found. Import a character entity to populate.", icon='INFO')
                else:
                    search_box = layout.box()
                    search_row = search_box.row(align=True)
                    search_row.prop(scene, "witcher_animset_filter_text", text="", icon='VIEWZOOM')
                    filter_text = str(getattr(scene, "witcher_animset_filter_text", "") or "").strip().lower()

                    groups = []
                    current_group_name = "Sets"
                    current_group_items = []
                    for item in rig_settings.animset_list:
                        item_path = str(getattr(item, "path", "") or "")
                        if ":" in item_path:
                            if current_group_items:
                                groups.append((current_group_name, current_group_items))
                            current_group_name = item_path.rstrip(":") or "Sets"
                            current_group_items = []
                            continue
                        current_group_items.append(item)
                    if current_group_items:
                        groups.append((current_group_name, current_group_items))

                    total_set_count = 0
                    visible_set_count = 0
                    matched_group_count = 0

                    for group_name, group_items in groups:
                        total_set_count += len(group_items)

                        visible_items = []
                        for item in group_items:
                            item_path = str(getattr(item, "path", "") or "")
                            filename = item_path.replace("\\", "/").split("/")[-1]
                            haystack = f"{filename} {item_path} {group_name}".lower()
                            if filter_text and filter_text not in haystack:
                                continue
                            visible_items.append(item)

                        if not visible_items:
                            continue

                        visible_set_count += len(visible_items)
                        matched_group_count += 1

                        current_box = layout.box()
                        hdr = current_box.row()
                        hdr.enabled = False
                        hdr.label(text=group_name, icon='OUTLINER_OB_ARMATURE')

                        for item in visible_items:
                            item_path = str(getattr(item, "path", "") or "")
                            filename = item_path.replace("\\", "/").split("/")[-1]
                            is_loaded_set = bool(loaded_set["loaded_key"]) and (
                                _animset_repo_compare_key(context, item_path) == loaded_set["loaded_key"]
                            )
                            button_icon = 'CHECKMARK' if is_loaded_set else 'ACTION'
                            button_text = filename if not is_loaded_set else f"{filename}  [Loaded]"
                            file_row = current_box.row(align=True)
                            op = file_row.operator(
                                "witcher.list_loadapp",
                                text=button_text,
                                icon=button_icon,
                                depress=is_loaded_set,
                            )
                            op.action = "w2anims"
                            op.path = item_path
                            reveal_op = file_row.operator("witcher.reveal_anim_in_explorer", text="", icon='FILE_FOLDER')
                            reveal_op.path = item_path
                            info_op = file_row.operator("witcher.animset_path_info", text="", icon='QUESTION')
                            info_op.path = item_path

                    stats_row = search_box.row(align=True)
                    if filter_text:
                        stats_row.label(text=f"{visible_set_count}/{total_set_count} sets in {matched_group_count} groups", icon='FILTER')
                    else:
                        stats_row.label(text=f"{total_set_count} sets in {len(groups)} groups", icon='INFO')

                    if total_set_count > 0 and visible_set_count == 0:
                        no_match = layout.box()
                        no_match.label(text=f"No sets match '{filter_text}'", icon='INFO')
                        no_match.label(text="Try part of file name, path, or group (e.g. sword).")

        body = section("witcher_anim_imported_sets", "Imported Animation Clips", 'ACTION') if anim_tab == "CLIPS" else None
        if body:
            col_main = body.column(align=True)
            loaded_set = _get_loaded_animset_ui_state(context)

            if loaded_set["has_loaded_set"]:
                set_info = col_main.box()
                hdr = set_info.row(align=True)
                hdr.label(text="Current Set", icon='CHECKMARK')
                if loaded_set["source_badge"]:
                    badge = hdr.row(align=True)
                    badge.enabled = False
                    badge.label(text=f"[{loaded_set['source_badge']}]")
                hdr.label(text=f"{loaded_set['clip_count']} clips", icon='ANIM_DATA')
                set_info.label(text=loaded_set["display_name"] or "Loaded animation set", icon='ACTION')
                if loaded_set["display_path"]:
                    path_row = set_info.row()
                    path_row.scale_y = 0.75
                    path_row.label(text=loaded_set["display_path"], icon='FILE')
            else:
                hint = col_main.box()
                hint.label(text="No animation set loaded yet.", icon='INFO')
                hint.label(text="Import a .w2anims set to populate clip entries.", icon='IMPORT')

            col_main.operator(ButtonOperatorImportW2Anims.bl_idname, text="Import Set (.w2anims)", icon='IMPORT')

            box = col_main.box()
            list_row = box.row()
            col = list_row.column(align=True)
            col.template_list(
                "TOOL_UL_List",
                "The_List",
                scene,
                "witcher_w2anims_list",
                scene,
                "witcher_w2anims_list_index",
            )
            reorder_col = list_row.column()
            if len(scene.witcher_w2anims_list) > 1:
                reorder_col.operator("witcher.list_reorder", text="", icon="TRIA_UP").direction = "UP"
                reorder_col.operator("witcher.list_reorder", text="", icon="TRIA_DOWN").direction = "DOWN"

            row = box.row(align=True)
            row.operator("witcher.list_loadanim", text="Load Clip", icon='PLAY').action = "load"
            row.prop(scene, "witcher_load_anim_on_select", text="Load on Select")

            opts = box.box()
            opts.label(text="Import / Decode Options", icon='SETTINGS')
            opts.prop(scene, "witcher_prefer_uncompressed_anims", text="Prefer Uncompressed Data")
            opts.prop(scene, "witcher_bake_every_frame", text="Bake Every Frame")
            opts.prop(scene, "witcher_smooth_missing_frames", text="Smooth Missing Frames")
            opts.prop(scene, "witcher_scale_keys_to_duration", text="Scale Keys to Duration")

            motion_box = box.box()
            motion_box.label(text="Motion Extraction Debug", icon='ACTION')
            motion_box.label(text="Import motion extraction debug object when present.")
            motion_box.prop(scene, "witcher_motion_extraction_debug_compressed", text="Compressed")
            motion_box.prop(scene, "witcher_motion_extraction_debug_uncompressed", text="Uncompressed")

            orient_row = box.row(align=True)
            orient_row.prop(scene, "witcher_auto_orient_root", text="Auto Orient Root")
            orient_row.operator(WITCH_OT_ApplyRootOrientation.bl_idname, text="", icon='ORIENTATION_GLOBAL')
            box.prop(scene, "witcher_anim_nla_mode", text="NLA Load Mode")

            item, _safe_index = _get_selected_collection_item(
                scene,
                "witcher_w2anims_list",
                "witcher_w2anims_list_index",
            )
            if item is not None:
                info = col_main.box()
                info.label(text="Selected Clip", icon='INFO')
                info.label(text=f"Name: {item.name}")
                info.label(text=f"Frames: {item.numFrames}")
                info.label(text=f"FPS: {round(item.framesPerSecond, 2)}")
                info.label(text=f"Length: {round(item.duration, 2)} sec")
                info.label(text=f"Type: {item.SkeletalAnimationType}")
                if len(item.AdditiveType):
                    info.label(text=f"Additive: {item.AdditiveType}")
                info.label(text=f"Root Motion: {item.RootMotion}")

        body = section("witcher_anim_quick_browser", "Quick Animation Browser", 'PRESET') if anim_tab == "CLIPS" else None
        if body:
            from . import ui_anims_list as _ui_anims_list
            if hasattr(_ui_anims_list, "ensure_quick_anim_list_current"):
                _ui_anims_list.ensure_quick_anim_list_current(context)
            col = body.column(align=True)
            search_row = col.row(align=True)
            if hasattr(scene, "witcher_quick_anim_search"):
                search_row.prop(scene, "witcher_quick_anim_search", text="", icon='VIEWZOOM')
                clear_btn = search_row.row(align=True)
                clear_btn.enabled = bool(getattr(scene, "witcher_quick_anim_search", ""))
                clear_op = clear_btn.operator("witcher.myanimlist_debug", text="", icon='X')
                clear_op.action = "clear_search"
            if hasattr(scene, "witcher_quick_anim_load_on_select"):
                search_row.prop(scene, "witcher_quick_anim_load_on_select", text="Load on Select")

            if hasattr(scene, "witcher_auto_orient_root"):
                col.prop(scene, "witcher_auto_orient_root", text="Auto Orient Root")
            if hasattr(scene, "witcher_anim_nla_mode"):
                col.prop(scene, "witcher_anim_nla_mode", text="NLA Load Mode")
            if hasattr(scene, "witcher_quick_anim_auto_collapse_categories"):
                col.prop(scene, "witcher_quick_anim_auto_collapse_categories", text="Auto Collapse Categories")
            if hasattr(scene, "witcher_quick_anim_show_all"):
                col.prop(scene, "witcher_quick_anim_show_all", text="Show All Animations")

            bulk_row = col.row(align=True)
            bulk_row.operator("witcher.quick_anim_category_bulk", text="Expand All").action = "expand_all"
            bulk_row.operator("witcher.quick_anim_category_bulk", text="Collapse All").action = "collapse_all"

            list_box = col.box()
            list_box.template_list(
                listtype_name='MYANIMLISTITEM_UL_basic',
                list_id='W3_UI_ANIMATION_LIST',
                dataptr=scene,
                propname='witcher_quick_anim_list',
                active_dataptr=scene,
                active_propname='witcher_quick_anim_list_index',
                rows=7,
            )
            list_box.label(text=f"{len(getattr(scene, 'witcher_quick_anim_list', []))} visible entries", icon='INFO')
            act = list_box.row(align=True)
            act.operator("witcher.myanimlist_debug", text="Rebuild", icon='FILE_REFRESH').action = "reset3"
            act.operator("witcher.myanimlist_debug", text="Load", icon='PLAY').action = "load"

        body = section("witcher_pelvis_edit", "Bone Animation Offset Editor", 'BONE_DATA', default_closed=True) if anim_tab == "CLIPS" else None
        if body:
            col = body.column(align=True)
            col.prop(scene, "witcher_pelvis_bone_name", text="Bone")
            col.separator()

            move_box = col.box()
            move_box.label(text="Move & Bake Mode", icon='TRANSFORM_ORIGINS')
            move_box.label(text="1. Select bone in Pose Mode, move with G/R", icon='INFO')
            move_box.label(text="2. Set Reference, move bone to desired pose", icon='INFO')
            move_box.label(text="3. Bake Pose Delta to apply offset to all keyframes", icon='INFO')
            ref_row = move_box.row(align=True)
            ref_row.operator("witcher.pelvis_set_reference", text="Set Reference", icon='EYEDROPPER')
            has_ref = bool(getattr(scene, "witcher_pelvis_has_ref", False))
            if has_ref:
                ref_loc = list(getattr(scene, "witcher_pelvis_ref_loc", [0.0, 0.0, 0.0]))
                move_box.label(
                    text=f"Ref: ({ref_loc[0]:.3f}, {ref_loc[1]:.3f}, {ref_loc[2]:.3f})",
                    icon='CHECKMARK',
                )
            bake_row = move_box.row(align=True)
            bake_row.enabled = has_ref
            bake_row.operator("witcher.pelvis_bake_pose_delta", text="Bake Pose Delta", icon='NLA_PUSHDOWN')

            col.separator()

            num_box = col.box()
            num_box.label(text="Numeric Offset Mode", icon='DRIVER_TRANSFORM')
            num_box.prop(scene, "witcher_pelvis_offset_loc", text="Location")
            num_box.prop(scene, "witcher_pelvis_offset_rot", text="Rotation (deg)")
            num_box.operator("witcher.pelvis_apply_numeric_offset", text="Apply Offset", icon='CHECKMARK')

        if anim_tab == "SPEECH":
            try:
                from . import ui_voice as _ui_voice
            except Exception:
                _ui_voice = None
            try:
                from . import ui_mimics as _ui_mimics_dialog
            except Exception:
                _ui_mimics_dialog = None

        # --- Quick Mimic Import (own section, above Dialogue Browser) ---
        mimic_body = section("witcher_anim_quick_mimic", "Quick Mimic Import", 'SHAPEKEY_DATA') if anim_tab == "SPEECH" else None
        if mimic_body:
            mimic_col = mimic_body.column(align=True)
            mimic_props_ready_dialog = bool(
                _ui_mimics_dialog
                and hasattr(scene, _ui_mimics_dialog.MIMIC_LIST_PROP)
                and hasattr(scene, _ui_mimics_dialog.MIMIC_LIST_INDEX_PROP)
                and hasattr(scene, _ui_mimics_dialog.MIMIC_AUTO_LOAD_PROP)
            )
            if mimic_props_ready_dialog:
                if hasattr(_ui_mimics_dialog, "ensure_mimic_list_initialized"):
                    _ui_mimics_dialog.ensure_mimic_list_initialized(context)

                search_row = mimic_col.row(align=True)
                if hasattr(scene, _ui_mimics_dialog.MIMIC_SEARCH_PROP):
                    search_row.prop(scene, _ui_mimics_dialog.MIMIC_SEARCH_PROP, text="", icon='VIEWZOOM')
                    clear_btn = search_row.row(align=True)
                    clear_btn.enabled = bool(getattr(scene, _ui_mimics_dialog.MIMIC_SEARCH_PROP, ""))
                    clear_btn.operator("witcher.quick_mimic_debug", text="", icon='X').action = "clear_search"
                search_row.prop(scene, _ui_mimics_dialog.MIMIC_AUTO_LOAD_PROP, text="Load on Select")

                if hasattr(scene, _ui_mimics_dialog.MIMIC_AUTO_COLLAPSE_PROP):
                    mimic_col.prop(scene, _ui_mimics_dialog.MIMIC_AUTO_COLLAPSE_PROP, text="Auto Collapse Categories")
                if hasattr(scene, "witcher_anim_nla_mode"):
                    mimic_col.prop(scene, "witcher_anim_nla_mode", text="NLA Load Mode")

                bulk_row = mimic_col.row(align=True)
                bulk_row.operator("witcher.quick_mimic_category_bulk", text="Expand All").action = "expand_all"
                bulk_row.operator("witcher.quick_mimic_category_bulk", text="Collapse All").action = "collapse_all"

                mimic_list_box = mimic_col.box()
                mimic_list_box.template_list(
                    "MYMIMICLISTITEM_UL_basic",
                    "W3_UI_MIMIC_LIST_DIALOG",
                    scene,
                    _ui_mimics_dialog.MIMIC_LIST_PROP,
                    scene,
                    _ui_mimics_dialog.MIMIC_LIST_INDEX_PROP,
                    sort_lock=True,
                    rows=7,
                )
                mimic_list_box.label(
                    text=f"{len(getattr(scene, _ui_mimics_dialog.MIMIC_LIST_PROP, []))} visible entries",
                    icon='INFO',
                )
                mimic_actions = mimic_list_box.row(align=True)
                mimic_actions.operator("witcher.quick_mimic_debug", text="Rebuild", icon='FILE_REFRESH').action = "reset3"
                mimic_actions.operator("witcher.quick_mimic_debug", text="Load", icon='PLAY').action = "load"
            else:
                mimic_col.label(text="Mimic properties not registered.", icon='INFO')

        body = section("witcher_anim_dialogue_browser", "Dialogue Browser", 'TEXT', default_closed=False) if anim_tab == "SPEECH" else None
        if body:
            col = body.column(align=True)
            if _ui_voice and hasattr(_ui_voice, "ensure_voice_list_initialized"):
                _ui_voice.ensure_voice_list_initialized(context)

            # --- Options row (compact) ---
            option_row = col.row(align=True)
            if hasattr(scene, "witcher_voice_show_details"):
                option_row.prop(scene, "witcher_voice_show_details", text="IDs/dur")
            if hasattr(scene, "witcher_voice_replace_audio"):
                option_row.prop(scene, "witcher_voice_replace_audio", text="Replace")
            if hasattr(scene, "witcher_voice_recreate_phonemes"):
                option_row.prop(scene, "witcher_voice_recreate_phonemes", text="Phonemes")
            if getattr(scene, "witcher_voice_recreate_phonemes", False):
                if hasattr(scene, "witcher_voice_phoneme_accuracy"):
                    col.prop(scene, "witcher_voice_phoneme_accuracy", text="Accuracy", slider=True)
            if hasattr(scene, "witcher_anim_nla_mode"):
                col.prop(scene, "witcher_anim_nla_mode", text="NLA Load Mode")

            # --- Loaded lipsync status ---
            _arm = _find_character_armature(context)
            if _arm and _arm.animation_data:
                _voice_tracks = []
                for _trk in _arm.animation_data.nla_tracks:
                    if _trk.name in ("voice_import", "voice_import_phoneme"):
                        for _strip in _trk.strips:
                            _aname = _strip.action.name if _strip.action else "?"
                            _label = "Phonemes" if "phoneme" in _trk.name else "Morphs"
                            _voice_tracks.append((_label, _aname, _strip.frame_start, _strip.frame_end))
                if _voice_tracks:
                    status_box = col.box()
                    for _label, _aname, _fs, _fe in _voice_tracks:
                        status_box.label(text=f"{_label}: {_aname}  [{int(_fs)}-{int(_fe)}]", icon='NLA')
                    status_box.operator("witcher.clear_lipsync", text="Clear Lipsync", icon='TRASH')

            # --- Popular / pinned speaker quick-filters ---
            popular_speakers = []
            if _ui_voice and hasattr(_ui_voice, "_voice_popular_speakers_cache"):
                popular_speakers = list(getattr(_ui_voice, "_voice_popular_speakers_cache", []))
            if popular_speakers:
                col.label(text="Popular speakers", icon='COMMUNITY')
                popular_grid = col.grid_flow(columns=4, align=True)
                for speaker in popular_speakers:
                    op = popular_grid.operator(
                        "witcher.quick_voice_filter_speaker",
                        text=f"[{speaker}]",
                        icon='FILTER',
                    )
                    op.speaker = speaker
                    if _ui_voice and hasattr(_ui_voice, "_get_speaker_count"):
                        try:
                            op.count = int(_ui_voice._get_speaker_count(speaker))
                        except Exception:
                            pass

            if hasattr(scene, "witcher_voice_pinned_speakers") and scene.witcher_voice_pinned_speakers:
                col.label(text="Pinned", icon='BOOKMARKS')
                pinned_grid = col.grid_flow(columns=4, align=True)
                for pin in scene.witcher_voice_pinned_speakers:
                    pin_name = getattr(pin, "name", "")
                    if not pin_name:
                        continue
                    op = pinned_grid.operator(
                        "witcher.quick_voice_filter_speaker",
                        text=f"[{pin_name}]",
                        icon='FILTER',
                    )
                    op.speaker = pin_name
                    if _ui_voice and hasattr(_ui_voice, "_get_speaker_count"):
                        try:
                            op.count = int(_ui_voice._get_speaker_count(pin_name))
                        except Exception:
                            pass

            # --- Dialogue list ---
            if hasattr(scene, "witcher_voice_list") and hasattr(scene, "witcher_voice_list_index"):
                # --- Selected-line speaker filter (lives with the filter controls) ---
                if 0 <= scene.witcher_voice_list_index < len(scene.witcher_voice_list):
                    selected = scene.witcher_voice_list[scene.witcher_voice_list_index]
                    speaker = getattr(selected, "speaker", "")
                    if speaker:
                        effective_speaker = ""
                        speaker_is_pinned = False
                        if _ui_voice and hasattr(_ui_voice, "_get_effective_speaker"):
                            try:
                                effective_speaker = _ui_voice._get_effective_speaker(scene)
                            except Exception:
                                effective_speaker = ""
                        if _ui_voice and hasattr(_ui_voice, "_is_pinned"):
                            try:
                                speaker_is_pinned = bool(_ui_voice._is_pinned(scene, speaker))
                            except Exception:
                                speaker_is_pinned = False
                        chip_row = col.row(align=True)
                        chip_row.operator("witcher.quick_voice_filter_speaker", text=f"Only [{speaker}]", icon='FILTER').speaker = speaker
                        chip_row.operator("witcher.quick_voice_pin_speaker", text="", icon='BOOKMARKS').speaker = speaker
                        if speaker_is_pinned and hasattr(bpy.ops.witcher, "quick_voice_unpin_speaker"):
                            chip_row.operator("witcher.quick_voice_unpin_speaker", text="", icon='X').speaker = speaker
                        if effective_speaker:
                            chip_row.operator("witcher.quick_voice_clear_speaker", text="Clear", icon='PANEL_CLOSE')

                # --- Pager ---
                if _ui_voice and hasattr(_ui_voice, "get_voice_browser_stats"):
                    stats = _ui_voice.get_voice_browser_stats(scene)
                    if all(hasattr(scene, p) for p in ("witcher_voice_page_size", "witcher_voice_page_index")):
                        pager = col.row(align=True)
                        pager.prop(scene, "witcher_voice_page_size", text="Rows")
                        pager.operator("witcher.quick_voice_page", text="<<").action = "first"
                        pager.operator("witcher.quick_voice_page", text="<").action = "prev"
                        pager.label(text=f"{stats['page_index'] + 1}/{stats['total_pages']}")
                        pager.operator("witcher.quick_voice_page", text=">").action = "next"
                        pager.operator("witcher.quick_voice_page", text=">>").action = "last"
                    col.label(
                        text=(
                            f"Showing {stats['visible_start']}-{stats['visible_end']} "
                            f"of {stats['filtered']} filtered  ({stats['total']} total)"
                        ),
                        icon='INFO',
                    )
                else:
                    total_nodes = _ui_voice.get_voice_node_count() if _ui_voice else 0
                    col.label(text=f"Showing {len(scene.witcher_voice_list)} of {total_nodes} lines", icon='INFO')

                # --- The actual list ---
                col.template_list(
                    "MYVOICELISTITEM_UL_basic",
                    "",
                    scene,
                    "witcher_voice_list",
                    scene,
                    "witcher_voice_list_index",
                    sort_lock=True,
                    rows=7,
                )

                # --- Actions (first thing under the list) ---
                act_row = col.row(align=True)
                if hasattr(scene, "witcher_voice_load_on_select"):
                    act_row.prop(scene, "witcher_voice_load_on_select", text="Load on Select")
                act_row.operator("witcher.quick_voice_debug", text="Rebuild", icon='FILE_REFRESH').action = "reset3"
                act_row.operator("witcher.quick_voice_debug", text="Load", icon='PLAY').action = "load"

                # --- Search bar ---
                if hasattr(scene, "witcher_voice_search_text"):
                    search_row = col.row(align=True)
                    search_row.prop(scene, "witcher_voice_search_text", text="", icon='VIEWZOOM')
                    search_row.operator("witcher.quick_voice_clear_filter", text="", icon='X')

                    # Search status / syntax hint
                    raw_search = getattr(scene, "witcher_voice_search_text", "").strip()
                    eff_speaker = ""
                    if _ui_voice and hasattr(_ui_voice, "_get_effective_speaker"):
                        try:
                            eff_speaker = _ui_voice._get_effective_speaker(scene)
                        except Exception:
                            pass
                    if raw_search or eff_speaker:
                        hint_parts = []
                        if eff_speaker:
                            hint_parts.append(f"speaker={eff_speaker}")
                        if raw_search and _ui_voice and hasattr(_ui_voice, "_parse_search_tokens"):
                            try:
                                _toks, _sp = _ui_voice._parse_search_tokens(raw_search)
                                id_toks   = [t for t in _toks if t['type'] == 'id']
                                text_toks = [t for t in _toks if t['type'] != 'id']
                                if id_toks:
                                    hint_parts.append(f"id:{id_toks[0]['terms'][0]}")
                                if text_toks:
                                    parts = []
                                    c = {tt: sum(1 for t in text_toks if t['type'] == tt) for tt in ('and','phrase','not','or')}
                                    if c['and']:    parts.append(f"{c['and']} word(s)")
                                    if c['phrase']: parts.append(f"{c['phrase']} phrase(s)")
                                    if c['not']:    parts.append(f"{c['not']} excluded")
                                    if c['or']:     parts.append(f"{c['or']} OR-group(s)")
                                    if parts:
                                        hint_parts.append("text: " + ", ".join(parts))
                            except Exception:
                                pass
                        if hint_parts:
                            col.label(text="Filtering: " + " | ".join(hint_parts), icon='VIEWZOOM')
                    else:
                        col.label(
                            text='Tip: words  "phrase"  -exclude  id:NNN  @SPEAKER  w1|w2',
                            icon='INFO',
                        )

                # --- Utility row (visually separated) ---
                col.separator(factor=0.5)
                util_row = col.row(align=True)
                util_row.scale_y = 0.85
                util_row.operator("witcher.quick_voice_copy", text="Copy Selected", icon='COPYDOWN').scope = "selected"
                util_row.operator("witcher.quick_voice_copy", text="Copy All", icon='COPYDOWN').scope = "all"
            else:
                col.label(text="Dialogue browser properties are not registered yet.", icon='INFO')

            # --- Speech Cache Tools (collapsed sub-panel) ---
            cache_header, cache_body = col.panel("witcher_dialogue_cache_tools", default_closed=True)
            cache_header.label(text="Cache Tools", icon='FILE_FOLDER')
            if cache_body:
                # Paths are configured in Addon Preferences; just expose an open button here.
                cache_body.operator(
                    "witcher.open_voice_audio_path",
                    text="Open Audio Folder",
                    icon='FILE_FOLDER',
                )
                if all(hasattr(scene, p) for p in (
                    "witcher_speech_pair_total",
                    "witcher_speech_pair_extracted",
                    "witcher_speech_pair_cr2w",
                    "witcher_speech_pair_wem",
                )):
                    counts = cache_body.box()
                    counts.label(text=f"Bundle pairs: {scene.witcher_speech_pair_total}")
                    counts.label(text=f"Extracted pairs: {scene.witcher_speech_pair_extracted}")
                    counts.label(text=f".cr2w files: {scene.witcher_speech_pair_cr2w}")
                    counts.label(text=f".wem files: {scene.witcher_speech_pair_wem}")
                    if getattr(scene, "witcher_speech_pair_last_refresh", ""):
                        counts.label(text=f"Last refresh: {scene.witcher_speech_pair_last_refresh}")
                cache_body.operator(
                    "witcher.refresh_speech_counts",
                    text="Refresh Counts",
                    icon='FILE_REFRESH',
                )

        body = section("witcher_anim_playback", "Playback / Root Motion", 'CON_LOCLIKE', default_closed=False) if anim_tab == "CLIPS" else None
        if body:
            col_main = body.column(align=True)
            root_motion_box = col_main.box()
            root_motion_box.label(text="Root Motion", icon='CON_LOCLIKE')

            active_armature = display_armature if (display_armature and display_armature.type == 'ARMATURE') else context.active_object
            if active_armature and active_armature.type == 'ARMATURE':
                from ..importers.motion_tools import (
                    has_root_motion_controller, get_controller_mode,
                    has_root_motion_drivers, get_root_motion_mode
                )

                has_controller = has_root_motion_controller(active_armature)
                if has_controller:
                    current_mode = get_controller_mode(active_armature)
                    icon = 'PAUSE' if current_mode == 'IN_PLACE' else 'PLAY'
                    text_label = "In-Place (Locked)" if current_mode == 'IN_PLACE' else "Root Motion (Moving)"
                    row = root_motion_box.row(align=True)
                    row.operator(WITCH_OT_ToggleRootMotionController.bl_idname, text=text_label, icon=icon)
                    row.operator(WITCH_OT_RemoveRootMotionController.bl_idname, text="", icon='X')
                    root_motion_box.label(
                        text=("Trajectory counteracted" if current_mode == 'IN_PLACE' else "Natural animation"),
                        icon='INFO',
                    )
                else:
                    root_motion_box.operator(
                        WITCH_OT_SetupRootMotionController.bl_idname,
                        text="Setup Controller (Recommended)",
                        icon='EMPTY_ARROWS',
                    )

                alt_box = root_motion_box.box()
                alt_box.label(text="Alternatives", icon='DOWNARROW_HLT')
                has_drivers = has_root_motion_drivers(active_armature)
                if has_drivers:
                    driver_mode = get_root_motion_mode(active_armature)
                    row = alt_box.row(align=True)
                    row.operator(
                        WITCH_OT_ToggleRootMotionDrivers.bl_idname,
                        text=f"Drivers: {'ON' if driver_mode == 'ROOT_MOTION' else 'OFF'}",
                        icon='DRIVER',
                    )
                    row.operator(WITCH_OT_RemoveRootMotionDrivers.bl_idname, text="", icon='X')
                else:
                    alt_box.operator(WITCH_OT_ToggleRootMotionDrivers.bl_idname, text="Setup Drivers (Root bone)", icon='DRIVER')
                alt_box.operator(ButtonOperatorToggloRootMotion.bl_idname, text="Toggle RootMotion Bone", icon='BONE_DATA')
            else:
                root_motion_box.label(text="Select a character armature", icon='INFO')

            col_main.operator(WITCH_OT_ResampleAnimation.bl_idname, text="Resample Animation", icon='TIME')

            current_box = col_main.box()
            current_box.label(text="Current Animation", icon='ACTION')
            if display_armature:
                frame = scene.frame_current
                nla_action, nla_info = export_anims.get_nla_action_at_frame(display_armature, frame=frame)
                if nla_action:
                    track_name = (nla_info or {}).get("track", "")
                    strip_name = (nla_info or {}).get("strip", "")
                    extra = f" [{track_name}/{strip_name}]" if track_name or strip_name else ""
                    current_box.label(text=f"NLA (Playing @ {frame}): {nla_action.name}{extra}")
                else:
                    nla_last_action, nla_last_info = export_anims.get_nla_last_action(display_armature, prefer_tracks=("anim_import",))
                    if nla_last_action:
                        track_name = (nla_last_info or {}).get("track", "")
                        strip_name = (nla_last_info or {}).get("strip", "")
                        extra = f" [{track_name}/{strip_name}]" if track_name or strip_name else ""
                        current_box.label(text=f"NLA (Last Strip): {nla_last_action.name}{extra}")
                    else:
                        current_box.label(text="NLA: None")

                action_slot = export_anims.get_action_slot(display_armature)
                current_box.label(text=f"Action Slot: {action_slot.name}" if action_slot else "Action Slot: None")

                if hasattr(scene, "witcher_w3_anim_source"):
                    current_box.prop(scene, "witcher_w3_anim_source", text="Source", expand=True)

                resolved_action, resolved_info = export_anims.resolve_action(
                    display_armature,
                    context=context,
                    source_mode=getattr(scene, "witcher_w3_anim_source", "NLA"),
                )
                if resolved_action:
                    source_label = _format_action_source_label((resolved_info or {}).get("source"))
                    if source_label:
                        current_box.label(text=f"Using: {source_label}")
                    current_box.label(text=f"Current: {resolved_action.name}")
                else:
                    current_box.label(text="Current: None")
            else:
                current_box.label(text="No armature found.", icon='INFO')

            camera_bone = None
            if display_armature and getattr(display_armature, "pose", None):
                camera_bone = display_armature.pose.bones.get(CAMERA_CONTROL_BONE)
            track_items = []
            track_names = []
            if rig_settings and getattr(rig_settings, "witcher_tracks_list", None):
                track_items = [x for x in rig_settings.witcher_tracks_list if x.type == 0]
                for track in track_items:
                    track_name = str(getattr(track, "path", "") or getattr(track, "name", "") or "")
                    if track_name and track_name not in track_names:
                        track_names.append(track_name)
            if camera_bone:
                ensure_camera_track_properties(display_armature, track_names=track_names or CAMERA_TRACK_NAMES)
                if not track_names:
                    track_names = list(CAMERA_TRACK_NAMES)

            if rig_settings and track_names and not camera_bone:
                tracks_box = col_main.box()
                row = tracks_box.row(align=False)
                row.prop(
                    rig_settings,
                    "witcher_tracks_collapse",
                    icon="TRIA_DOWN" if not rig_settings.witcher_tracks_collapse else "TRIA_RIGHT",
                    icon_only=True,
                    emboss=False,
                )
                row.label(text=f"Tracks ({len(track_names)})", icon='ANIM')
                if not rig_settings.witcher_tracks_collapse:
                    for track_name in track_names:
                        if camera_bone and track_name in camera_bone:
                            tracks_box.prop(camera_bone, f'["{track_name}"]', text=track_name)

            action_info_box = col_main.box()
            action_info_box.label(text="Action Import Info", icon='INFO')
            action = None
            if display_armature:
                action, _ = export_anims.resolve_action(
                    display_armature,
                    context=context,
                    source_mode=getattr(scene, "witcher_w3_anim_source", "NLA"),
                )
            if action:
                source_file = action.get("w3_anim_source_file", "")
                buffer_source = action.get("w3_anim_buffer_source", "")
                buffer_detail = action.get("w3_anim_buffer_detail", "")
                if source_file:
                    action_info_box.label(text="File: " + os.path.basename(source_file))
                if buffer_source:
                    detail_text = f" ({buffer_detail})" if buffer_detail else ""
                    action_info_box.label(text="Buffer: " + buffer_source + detail_text)
                if not source_file and not buffer_source:
                    action_info_box.label(text="No import metadata found.")
            else:
                action_info_box.label(text="No action found.")

        export_body = section("witcher_anim_export_set", "Export Set", 'EXPORT', default_closed=True) if anim_tab == "CLIPS" else None
        if export_body:
            export_set = getattr(scene, "witcher_anim_export_set", None)

            row = export_body.row()
            row.template_list(
                "WITCH_UL_AnimExportSet", "",
                scene, "witcher_anim_export_set",
                scene, "witcher_anim_export_set_index",
                rows=3,
            )
            btn_col = row.column(align=True)
            btn_col.operator(WITCH_OT_AnimExportSetAdd.bl_idname, text="", icon='ADD')
            btn_col.operator(WITCH_OT_AnimExportSetRemove.bl_idname, text="", icon='REMOVE')

            if export_set and len(export_set) > 0:
                idx = scene.witcher_anim_export_set_index
                if 0 <= idx < len(export_set):
                    entry = export_set[idx]
                    settings_row = export_body.row(align=True)
                    settings_row.prop(entry, "skeletal_anim_type", text="")
                    if entry.skeletal_anim_type == 'SAT_Additive':
                        settings_row.prop(entry, "additive_type", text="")
                    settings_row.prop(entry, "include_motion_extraction", text="Motion Extraction")

            export_body.separator()
            bake_row = export_body.row(align=True)
            bake_row.operator(WITCH_OT_BakePelvisToTrajectory.bl_idname, text="Bake Pelvis → Trajectory", icon='BONE_DATA')
            export_body.prop(scene, "witcher_anim_export_repo_path", text="Repo Path")
            export_body.operator(WITCH_OT_ExportW2AnimJson.bl_idname, text="Export...", icon='EXPORT')

class WITCH_OT_import_w3_fbx(Operator, ImportHelper):
    """Same as normal FBX import but applies materials. Need seprate "FBX Import plugin for blender" enabled. Download from Nexus"""
    bl_idname = "witcher.import_witcher3_fbx"
    bl_label = "Import Witcher 3 FBX"
    bl_options = {'REGISTER', 'UNDO'}

    # Properties provided or used by ImportHelper mixin class.
    filename_ext = ".fbx"
    filter_glob: StringProperty(
        default="*.fbx",
        options={'HIDDEN'}
    )
    files: CollectionProperty(
        name="File Path",
        description="File path used for importing",
        type=bpy.types.OperatorFileListElement
    )
    directory: StringProperty()

    # Other properties
    recursive: BoolProperty(
        name = "Recursive",
        default = False,
        description = "Recursive import. Be careful, and have a console open"
    )
    keep_lod_meshes: BoolProperty(
        name="Keep LODs",
        default=False,
        description="If enabled, it will keep low quality meshes and materials"
    )
    remove_doubles: BoolProperty(
        name="Remove Doubles",
        default=True,
        description="Disable this if you get incorrectly merged verts."
    )
    quadrangulate: BoolProperty(
        name="Tris to Quads",
        default=True,
        description="Runs the Tris to Quads operator on imported meshes with UV seams enabled. Therefore it shouldn't break anything"
    )
    combined_armatures: BoolProperty(
        name="Combine Armatures",
        default=True,
        description="Merge all armatures into one"
    )
    force_update_mats: BoolProperty(
        name="Overwrite Materials",
        default=False,
        description="Re-create materials even if they were already imported before. Their old versions will be overwritten"
    )

    def execute(self, context):
        # if not bpy.data.is_saved:
        # 	self.report({'ERROR'}, 'Please save your file first. Textures will be written in a "textures" folder next to the .blend file.')
        # 	return {'CANCELLED'}

        filepath = self.filepath	# Provided by ImportHelper.

        uncook_path = get_uncook_path(context)
        recursive = self.recursive
        keep_lod_meshes = self.keep_lod_meshes
        remove_doubles = self.remove_doubles
        quadrangulate = self.quadrangulate
        combined_armatures = self.combined_armatures
        if recursive:
            combined_armatures = False

        paths = [os.path.join(self.directory, name.name)
            for name in self.files]

        if not uncook_path or not os.path.isdir(uncook_path):
            raise Exception("Please set a valid Uncook Path in Edit -> Preferences -> Add-ons -> Witcher 3 Tools.")

        #bpy.ops.outliner.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)

        fbx_util.importFbx(filepath
                            ,"name"
                            ,"name"
                            ,uncook_path = uncook_path
                            ,keep_lod_meshes = keep_lod_meshes
                        )

        return {'FINISHED'}

class WITCH_OT_ImportW2Rig(bpy.types.Operator, ImportHelper):
    """Load Witcher 3 .w2rig file or .w2rig.json"""
    bl_idname = "witcher.import_w2_rig"
    bl_label = "Import .w2rig"
    filename_ext = ".w2rig, .w2rig.json; w3dyny"
    bl_options = {'REGISTER', 'UNDO'}

    filter_glob: StringProperty(default='*.w2rig;*.w2rig.json;*.w3dyng;*.w3dyng.json', options={'HIDDEN'})

    def execute(self, context):
        log.debug("importing rig")
        fdir = self.filepath
        if os.path.isdir(fdir):
            self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
            return {'CANCELLED'}
        ext = file_helpers.getFilenameType(fdir)
        if ext == ".w2rig" or ext == ".json" or ext == ".w3dyng":
            rig_name = os.path.splitext(os.path.basename(fdir))[0]
            # Strip double extensions like .w2rig.json
            if rig_name.endswith('.w2rig') or rig_name.endswith('.w3dyng'):
                rig_name = os.path.splitext(rig_name)[0]
            armature_obj = import_rig.start_rig_import(fdir, rig_name, None, context=context)
            set_main_armature(context.scene, armature_obj)
        elif ext ==".w3fac":
            faceData = import_rig.loadFaceFile(fdir)
            armature_obj = import_rig.create_armature(faceData.mimicSkeleton, "yes", context=context)
            set_main_armature(context.scene, armature_obj)
        return {'FINISHED'}
    def invoke(self, context, event):
        UNCOOK_PATH = os.path.join(get_uncook_path(context),"characters\\base_entities\\")
        if os.path.exists(UNCOOK_PATH):
            self.filepath = UNCOOK_PATH if self.filepath == '' else self.filepath
        return ImportHelper.invoke(self, context, event)

class WITCH_OT_ExportW2RigJson(bpy.types.Operator, ExportHelper):
    """export W2 rig Json"""
    bl_idname = "witcher.export_w2_rig"
    bl_label = "Export"
    filename_ext = ".json"
    filename = ".w2rig"
    def execute(self, context):
        obj = context.object
        fdir = self.filepath
        ext = file_helpers.getFilenameType(fdir)
        import_rig.export_w3_rig(context, fdir)
        return {'FINISHED'}


from ..importers.motion_tools import generate_motion_extraction, MotionExtraction

def _normalize_w2anims_export_path(path, use_native_writer):
    p = Path(path)
    if use_native_writer:
        if p.suffix.lower() == ".json":
            p = p.with_suffix("")
        if p.suffix.lower() != ".w2anims":
            p = p.with_suffix(".w2anims")
        return str(p)

    if p.suffix.lower() == ".json":
        return str(p)
    if p.suffix.lower() == ".w2anims":
        return str(p) + ".json"
    return str(p.with_suffix(".json"))


def _normalize_w2cutscene_export_path(path):
    p = Path(path)
    if p.suffix.lower() != ".w2cutscene":
        p = p.with_suffix(".w2cutscene")
    return str(p)


class WITCH_OT_ExportW2AnimJson(bpy.types.Operator, ExportHelper):
    """Export Witcher 3 animation set (.w2anims)"""
    bl_idname = "witcher.export_w2_anim"
    bl_label = "Export Animations"
    filename_ext = ".w2anims"

    @classmethod
    def poll(cls, context):
        return export_anims.get_selected_armature(context) is not None

    use_json_legacy: BoolProperty(
        name="Use JSON (Legacy)",
        description="Export as .w2anims.json for WolvenKit processing instead of writing .w2anims directly",
        default=False,
    )

    # Fallback settings used only when the export set is empty
    skeletal_anim_type: EnumProperty(
        name="Animation Type",
        items=[
            ('SAT_Normal', "Normal", "Standard skeletal animation"),
            ('SAT_Additive', "Additive", "Additive skeletal animation"),
            ('SAT_MS', "MS", "Motion-sampled animation"),
        ],
        default='SAT_Normal',
    )
    additive_type: EnumProperty(
        name="Additive Type",
        items=[
            ('NONE', "None", "No additive type"),
            ('AT_Local', "Local", "Local additive"),
            ('AT_Ref', "Ref", "Reference additive"),
            ('AT_TPose', "T-Pose", "T-Pose additive"),
            ('AT_Animation', "Animation", "Animation additive"),
        ],
        default='NONE',
    )
    include_motion_extraction: BoolProperty(
        name="Include Motion Extraction",
        description="Generate motion extraction from Trajectory bone",
        default=False,
    )

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        armature = export_anims.get_selected_armature(context)
        source_mode = getattr(scene, "witcher_w3_anim_source", "NLA")
        export_set = getattr(scene, "witcher_anim_export_set", None)
        entries = [e for e in (export_set or []) if e.enabled]

        # --- Export Set ---
        set_box = layout.box()
        header_row = set_box.row(align=True)
        header_row.label(text=f"Export Set ({len(entries)} enabled)", icon='ACTION')
        header_row.operator(WITCH_OT_AnimExportSetAdd.bl_idname, text="Add Current", icon='ADD')

        list_row = set_box.row()
        list_row.template_list(
            "WITCH_UL_AnimExportSet", "",
            scene, "witcher_anim_export_set",
            scene, "witcher_anim_export_set_index",
            rows=3,
        )
        btn_col = list_row.column(align=True)
        btn_col.operator(WITCH_OT_AnimExportSetRemove.bl_idname, text="", icon='REMOVE')

        if export_set and len(export_set) > 0:
            idx = scene.witcher_anim_export_set_index
            if 0 <= idx < len(export_set):
                entry = export_set[idx]
                settings_row = set_box.row(align=True)
                settings_row.prop(entry, "skeletal_anim_type", text="")
                if entry.skeletal_anim_type == 'SAT_Additive':
                    settings_row.prop(entry, "additive_type", text="")
                settings_row.prop(entry, "include_motion_extraction", text="Motion Extraction")
        else:
            # No entries: show current action info and fallback settings
            if armature:
                action, info = export_anims.resolve_action(armature, context=context, source_mode=source_mode)
                if action:
                    set_box.label(text=f"Current: {action.name}", icon='PLAY')
                    source_label = _format_action_source_label((info or {}).get("source"))
                    if source_label:
                        set_box.label(text=f"Source: {source_label}")
                else:
                    set_box.label(text="No action found", icon='ERROR')
            set_box.label(text="Empty set — exports current action", icon='INFO')
            set_box.prop(self, "skeletal_anim_type")
            if self.skeletal_anim_type == 'SAT_Additive':
                set_box.prop(self, "additive_type")
            set_box.prop(self, "include_motion_extraction")

        # --- Format ---
        layout.prop(self, "use_json_legacy")

        # --- Game Path ---
        path_box = layout.box()
        path_box.label(text="Game Path (REDkit)", icon='FILE_FOLDER')
        path_box.prop(scene, "witcher_anim_export_repo_path", text="Repo Path")

        project_path = _anim_get_active_redkit_project(context)
        if project_path:
            repo_path = getattr(scene, "witcher_anim_export_repo_path", "")
            if repo_path:
                full_path = _anim_compute_full_export_path(
                    os.path.join(project_path, "workspace"), repo_path,
                )
                if full_path:
                    col = path_box.column(align=True)
                    col.scale_y = 0.75
                    col.label(text=os.path.dirname(full_path))
                    col.label(text=os.path.basename(full_path))
            row = path_box.row(align=True)
            row.operator(WITCH_OT_AnimExportGotoProjectPath.bl_idname, text="Go To Project Path", icon='FILEBROWSER')
            row.operator(WITCH_OT_AnimSetRepoFromBrowser.bl_idname, text="Set from Folder", icon='FILE_FOLDER')
        else:
            path_box.label(text="No REDkit project configured", icon='INFO')
            path_box.label(text="Set one in Preferences → Add-ons → Witcher 3 Tools")

    def execute(self, context):
        use_native = not self.use_json_legacy
        savepath = _normalize_w2anims_export_path(self.filepath, use_native)

        export_set = getattr(context.scene, "witcher_anim_export_set", [])
        entries = [e for e in export_set if e.enabled]

        if entries:
            result = export_anims.export_w3_anim_set(
                context, savepath, entries=entries, use_native_writer=use_native,
            )
            if result == {'FINISHED'}:
                self.report({'INFO'}, f"Exported {len(entries)} animation(s) to {savepath}")
            return result or {'FINISHED'}
        else:
            additive = self.additive_type if self.additive_type != 'NONE' else None
            result = export_anims.export_w3_anim(
                context, savepath,
                use_native_writer=use_native,
                skeletal_type=self.skeletal_anim_type,
                additive_type=additive,
                include_motion_extraction=self.include_motion_extraction,
            )
            return result or {'FINISHED'}

    def invoke(self, context, event):
        scene = context.scene
        # If repo path + project are set, pre-fill filepath from them
        repo_path = getattr(scene, "witcher_anim_export_repo_path", "")
        project_path = _anim_get_active_redkit_project(context)
        if project_path and repo_path:
            workspace_root = os.path.join(project_path, "workspace")
            full_path = _anim_compute_full_export_path(workspace_root, repo_path)
            if full_path:
                use_native = not self.use_json_legacy
                self.filepath = _normalize_w2anims_export_path(full_path, use_native)
                return ExportHelper.invoke(self, context, event)

        # Fall back to action name
        export_set = getattr(scene, "witcher_anim_export_set", [])
        first = next((e for e in export_set if e.enabled), None)
        action = None
        if first:
            action = bpy.data.actions.get(first.action_name)
        if action is None:
            source_mode = getattr(scene, "witcher_w3_anim_source", "NLA")
            armature = export_anims.get_selected_armature(context)
            action, _ = export_anims.resolve_action(armature, context=context, source_mode=source_mode)
        if action:
            current = Path(self.filepath) if self.filepath else None
            if current and str(current.parent) not in (".", ""):
                self.filepath = str(current.parent / f"{action.name}{self.filename_ext}")
            else:
                self.filepath = f"{action.name}{self.filename_ext}"
        return ExportHelper.invoke(self, context, event)


class WITCH_OT_ExportW2Cutscene(bpy.types.Operator, ExportHelper):
    """Export W2 Cutscene (native .w2cutscene)"""
    bl_idname = "witcher.export_w2_cutscene"
    bl_label = "Export Cutscene"
    filename_ext = ".w2cutscene"
    filter_glob: StringProperty(default="*.w2cutscene", options={'HIDDEN'})

    export_redkit_re_files: BoolProperty(
        name="Export Redkit .re Files",
        description="Export each cutscene entry as a Redkit-friendly .re file next to the .w2cutscene",
        default=False,
    )

    export_redkit_csv: BoolProperty(
        name="Export Redkit CSV",
        description="Write an animation;component CSV manifest next to the .w2cutscene. This also exports Redkit .re files.",
        default=False,
    )

    @classmethod
    def poll(cls, context):
        scene = getattr(context, "scene", None)
        if scene is None:
            return False
        return any(
            getattr(obj, "type", None) == 'ARMATURE'
            and str(obj.get("cutscene_actor_name", "") or "").strip()
            for obj in scene.objects
        )

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        re_status = get_re_addon_status()

        layout.prop(self, "export_redkit_re_files")
        layout.prop(self, "export_redkit_csv")
        if self.export_redkit_csv and not self.export_redkit_re_files:
            layout.label(text="CSV export also writes Redkit .re files.", icon='INFO')
        if self.export_redkit_re_files or self.export_redkit_csv:
            icon = 'CHECKMARK' if re_status["enabled"] else 'ERROR'
            status = "enabled" if re_status["enabled"] else "not enabled"
            layout.label(text=f"RE addon: {status}", icon=icon)
            if not re_status["enabled"]:
                warning_row = layout.row()
                warning_row.alert = True
                warning_row.label(text="Enable blender_re_animations_plugin before exporting Redkit .re files.", icon='ERROR')
            layout.label(text="Files write to <cutscene>_redkit/<actor>/*.re", icon='FILE_FOLDER')

        path_box = layout.box()
        path_box.label(text="Game Path (REDkit)", icon='FILE_FOLDER')
        path_box.prop(scene, "witcher_cutscene_export_repo_path", text="Repo Path")

        project_path = _anim_get_active_redkit_project(context)
        if project_path:
            repo_path = getattr(scene, "witcher_cutscene_export_repo_path", "")
            if repo_path:
                full_path = _anim_compute_full_export_path(
                    os.path.join(project_path, "workspace"), repo_path,
                )
                if full_path:
                    col = path_box.column(align=True)
                    col.scale_y = 0.75
                    col.label(text=os.path.dirname(full_path))
                    col.label(text=os.path.basename(full_path))
            row = path_box.row(align=True)
            row.operator(WITCH_OT_CutsceneExportGotoProjectPath.bl_idname, text="Go To Project Path", icon='FILEBROWSER')
            row.operator(WITCH_OT_CutsceneSetRepoFromBrowser.bl_idname, text="Set from Folder", icon='FILE_FOLDER')
        else:
            path_box.label(text="No REDkit project configured", icon='INFO')
            path_box.label(text="Set one in Preferences -> Add-ons -> Witcher 3 Tools")

    def execute(self, context):
        if self.export_redkit_re_files or self.export_redkit_csv:
            re_status = get_re_addon_status()
            if not re_status["enabled"]:
                self.report({'ERROR'}, "Enable blender_re_animations_plugin to export Redkit .re files")
                return {'CANCELLED'}

        savepath = _normalize_w2cutscene_export_path(self.filepath)
        return export_cutscene.export_w3_cutscene(
            context,
            savepath,
            export_redkit_re_files=self.export_redkit_re_files,
            export_redkit_csv=self.export_redkit_csv,
        )

    def invoke(self, context, event):
        scene = context.scene
        repo_path = getattr(scene, "witcher_cutscene_export_repo_path", "")
        project_path = _anim_get_active_redkit_project(context)
        if project_path and repo_path:
            workspace_root = os.path.join(project_path, "workspace")
            full_path = _anim_compute_full_export_path(workspace_root, repo_path)
            if full_path:
                self.filepath = _normalize_w2cutscene_export_path(full_path)
                return ExportHelper.invoke(self, context, event)

        loaded_path = getattr(scene, "witcher_loaded_w2cutscene_path", "")
        if loaded_path:
            current = Path(self.filepath) if self.filepath else None
            filename = Path(loaded_path).name
            if current and str(current.parent) not in (".", ""):
                self.filepath = str(current.parent / filename)
            else:
                self.filepath = filename
        return ExportHelper.invoke(self, context, event)


#-----------------------------------------------------------------------------
#
classes = [
    W3AnimExportEntry,
    ButtonOperatorImportW2Anims,
    ButtonOperatorToggloRootMotion,
    WITCH_OT_ToggleRootMotionDrivers,
    WITCH_OT_RemoveRootMotionDrivers,
    WITCH_OT_SetupRootMotionController,
    WITCH_OT_RemoveRootMotionController,
    WITCH_OT_ToggleRootMotionController,
    WITCH_OT_ApplyRootOrientation,
    WITCH_OT_ResampleAnimation,
    WITCH_OT_BakePelvisToTrajectory,
    ListItem,
    TOOL_UL_List,
    TOOL_OT_List_Add,
    TOOL_OT_List_Remove,
    TOOL_OT_List_Reorder,
    WITCHER_PT_animset_panel,
    TOOL_OT_List_LoadAnim,
    WITCH_OT_ImportW2Rig,
    WITCH_OT_ExportW2RigJson,
    WITCH_UL_AnimExportSet,
    WITCH_OT_AnimExportSetAdd,
    WITCH_OT_AnimExportSetRemove,
    WITCH_OT_CameraSetupPreview,
    WITCH_OT_CameraSetSceneCamera,
    WITCH_OT_CameraKeyRigFromSceneCamera,
    WITCH_OT_CameraBakeCutFromSceneCamera,
    WITCH_OT_CameraCutJump,
    WITCH_OT_CameraCutResize,
    WITCH_OT_CameraCutSplit,
    WITCH_OT_CameraCutCombine,
    WITCH_OT_CameraCutSyncMarkers,
    WITCH_OT_CameraCutApplyMarkers,
    WITCH_OT_CameraSetDofFromSelected,
    WITCH_OT_CameraConvertCutsToBlenderCameras,
    WITCH_OT_CameraApplyBlenderCamerasToRig,
    WITCH_OT_PelvisSetReference,
    WITCH_OT_PelvisBakePoseDelta,
    WITCH_OT_PelvisApplyNumericOffset,
    WITCH_OT_CutsceneNewShot,
    WITCH_OT_CutsceneUseSelectedCameraAsShot,
    WITCH_OT_CutsceneScratchImportCamera,
    WITCH_OT_CutsceneScratchAssignActor,
    WITCH_OT_CutsceneScratchAddAction,
    WITCH_OT_CutsceneUseImportNlaStrip,
    WITCH_OT_CutsceneScratchCreateCameraCut,
    WITCH_OT_CutsceneScratchBakeSelectedCameraRange,
    WITCH_OT_CutsceneScratchValidate,
    WITCH_OT_AnimExportGotoProjectPath,
    WITCH_OT_AnimSetRepoFromBrowser,
    WITCH_OT_CutsceneExportGotoProjectPath,
    WITCH_OT_CutsceneSetRepoFromBrowser,
    WITCH_OT_ExportW2AnimJson,
    WITCH_OT_ExportW2Cutscene,
]



def register():
    #bpy.types.TOPBAR_MT_file_import.append(menu_func_import)
    for c in classes:
        bpy.utils.register_class(c)

    # bpy.types.Scene.anim_export_name = StringProperty(
    #        name="Anim Export Name",
    #        description="Name of the animation",
    #        default="My_New_Anim")
    bpy.types.Scene.witcher_anim_tab = EnumProperty(
        name="Animation Tab",
        description="Active sub-section of the Animation panel",
        items=[
            ('SETS',   "Sets",   "Character-linked animation sets (idle, locomotion, facial)"),
            ('CLIPS',  "Clips",  "Import and browse individual animation clips"),
            ('SPEECH', "Speech", "Voiceline import and dialogue browser"),
        ],
        default='CLIPS',
    )
    bpy.types.Scene.witcher_loaded_w2anims_path = StringProperty(default='')
    bpy.types.Scene.witcher_loaded_w2anims_source_tag = StringProperty(
        name="Loaded Animation Set Source",
        description="Compact source tag for the currently loaded .w2anims set (W2/W3/JSON/etc.)",
        default='',
        options={'SKIP_SAVE'},
    )
    bpy.types.Scene.witcher_animset_filter_text = StringProperty(
        name="Set Filter",
        description="Filter character-linked animation sets by filename, path, or category",
        default='',
    )
    bpy.types.Scene.witcher_w2anims_list = CollectionProperty(type = ListItem)
    bpy.types.Scene.witcher_w2anims_list_index = IntProperty(name = "Index for witcher_w2anims_list",
                                             default = 0,
                                             update = on_anim_list_index_changed)

    bpy.types.Scene.witcher_load_anim_on_select = BoolProperty(
        name="Load on Select",
        description="Automatically load animation when selecting it in the list",
        default=False
    )

    bpy.types.Scene.witcher_anim_nla_mode = EnumProperty(
        name="NLA Load Mode",
        description="How the clip is placed on the anim_import / mimic_import / voice_import NLA track",
        items=[
            ('REPLACE',          "Replace",  "Clear the track and replace it with this clip"),
            ('APPEND',           "Append",   "Add the clip after the last existing strip on the track"),
            ('APPEND_AT_CURSOR', "At Frame", "Insert the clip at or after the current frame without deleting existing strips"),
        ],
        default='REPLACE',
    )

    bpy.types.Scene.witcher_w3_anim_source = EnumProperty(
        name="Animation Source",
        description="Choose which animation source is treated as current",
        items=[
            ('NLA', "NLA", "Use NLA strip at current frame (or last strip)"),
            ('ACTION', "Action Slot", "Use the legacy action slot"),
        ],
        default='NLA'
    )

    bpy.types.Scene.witcher_anim_export_set = CollectionProperty(type=W3AnimExportEntry)
    bpy.types.Scene.witcher_anim_export_set_index = IntProperty(
        name="Index for witcher_anim_export_set",
        default=0,
    )
    bpy.types.Scene.witcher_anim_export_repo_path = StringProperty(
        name="Animation Repo Path",
        description="Game-relative path for this .w2anims file (e.g. dlc\\bob\\data\\animations\\my_anim.w2anims)",
        default="",
    )
    bpy.types.Scene.witcher_cutscene_export_repo_path = StringProperty(
        name="Cutscene Repo Path",
        description="Game-relative path for this .w2cutscene file (e.g. dlc\\bob\\data\\cutscenes\\my_scene.w2cutscene)",
        default="",
    )
    bpy.types.Scene.witcher_cutscene_scratch_camera_repo_path = StringProperty(
        name="Cutscene Camera Entity",
        description="Game-relative camera entity path to import for scratch cutscenes",
        default=SCRATCH_CAMERA_DEFAULT_REPO_PATH,
    )
    bpy.types.Scene.witcher_cutscene_scratch_actor_name = StringProperty(
        name="Actor Name",
        description="Cutscene actor id written to the .w2cutscene actor definition",
        default="",
    )
    bpy.types.Scene.witcher_cutscene_scratch_actor_template = StringProperty(
        name="Actor Template",
        description="Game-relative entity template path for the selected cutscene actor",
        default="",
    )
    bpy.types.Scene.witcher_cutscene_scratch_actor_appearance = StringProperty(
        name="Appearance",
        description="Cutscene actor appearance name",
        default="",
    )
    bpy.types.Scene.witcher_cutscene_scratch_actor_type = EnumProperty(
        name="Actor Type",
        description="Cutscene actor type written to the .w2cutscene actor definition",
        items=[
            ("CAT_Actor", "Actor", "Animated actor"),
            ("CAT_Prop", "Prop", "Animated prop"),
            ("CAT_Camera", "Camera", "Cutscene camera actor"),
            ("CAT_None", "None", "No specific cutscene actor type"),
        ],
        default="CAT_Actor",
    )
    bpy.types.Scene.witcher_cutscene_scratch_use_mimic = BoolProperty(
        name="Use Mimic",
        description="Mark the selected actor as using hi-res mimic/face data",
        default=True,
    )
    bpy.types.Scene.witcher_cutscene_scratch_action_name = StringProperty(
        name="Action",
        description="Optional Blender action to add; empty uses the active action slot",
        default="",
    )
    bpy.types.Scene.witcher_cutscene_scratch_component = EnumProperty(
        name="Component",
        description="Cutscene animation component, usually Root or face",
        items=[
            (SCRATCH_CUTSCENE_ROOT_COMPONENT, "Root", "Body/root cutscene animation"),
            ("face", "Face", "Mimic/face cutscene animation"),
        ],
        default=SCRATCH_CUTSCENE_ROOT_COMPONENT,
    )
    bpy.types.Scene.witcher_cutscene_scratch_multipart_name = StringProperty(
        name="Multipart Name",
        description="Shared cutscene animation name for grouping multiple NLA strips into one multipart animation",
        default="",
    )
    bpy.types.Scene.witcher_cutscene_scratch_strip_length = IntProperty(
        name="Strip Length",
        description="Optional strip length in frames; 0 uses the action range",
        default=0,
        min=0,
    )
    bpy.types.Scene.witcher_cutscene_scratch_add_after_last = BoolProperty(
        name="Add After Last",
        description="Place the new strip after the last strip on the same cutscene track",
        default=False,
    )
    bpy.types.Scene.witcher_cutscene_scratch_validation_report = StringProperty(
        name="Cutscene Validation Report",
        default="",
        options={'SKIP_SAVE'},
    )

    bpy.types.Scene.witcher_auto_orient_root = BoolProperty(
        name="Auto Orient Root",
        description="Automatically orient Root bone after import (Z+ up, X+ towards Y-). Experimental.",
        default=True
    )

    bpy.types.Scene.witcher_motion_extraction_debug_compressed = BoolProperty(
        name="Import Compressed Motion Extraction",
        description="If enabled, imports compressed motion extraction as a debug object when present",
        default=False
    )

    bpy.types.Scene.witcher_motion_extraction_debug_uncompressed = BoolProperty(
        name="Import Uncompressed Motion Extraction",
        description="If enabled, attempts to import uncompressed motion extraction as a debug object when present",
        default=False
    )
    
    bpy.types.Scene.witcher_loaded_w2cutscene_path = StringProperty(default='')
    bpy.types.Scene.witcher_w2cutscene_list = CollectionProperty(type = ListItem)
    bpy.types.Scene.witcher_w2cutscene_list_index = IntProperty(name = "Index for witcher_w2cutscene_list",
                                             default = 0)

    bpy.types.Scene.witcher_loaded_w2scene_path = StringProperty(default='')
    bpy.types.Scene.witcher_w2scene_list = CollectionProperty(type = ListItem)
    bpy.types.Scene.witcher_w2scene_list_index = IntProperty(name = "Index for witcher_w2scene_list",
                                             default = 0)
    
    bpy.types.Scene.witcher_prefer_uncompressed_anims = BoolProperty(
        name="Prefer Uncompressed Animation Data",
        description="For uncooked .w2anims files, use embedded uncompressed keyframe data instead of compressed buffers (experimental)",
        default=False
    )
    bpy.types.Scene.witcher_bake_every_frame = BoolProperty(
        name="Bake Every Frame",
        description="Insert keyframes on every frame after resampling (more accurate, less smooth)",
        default=True
    )
    bpy.types.Scene.witcher_smooth_missing_frames = BoolProperty(
        name="Smooth Missing Frames",
        description="Apply light smoothing to resampled missing frames (may reduce pops, less accurate)",
        default=False
    )
    bpy.types.Scene.witcher_scale_keys_to_duration = BoolProperty(
        name="Scale Keys to Duration",
        description="When not baking every frame, scale key times to fit the animation duration using animation dt",
        default=False
    )

    # Pelvis / root bone editor properties
    bpy.types.Scene.witcher_pelvis_bone_name = StringProperty(
        name="Bone",
        description="Name of the bone to apply location/rotation offsets to",
        default="Pelvis",
    )
    bpy.types.Scene.witcher_pelvis_offset_loc = FloatVectorProperty(
        name="Location Offset",
        description="Location offset to add to every keyframe of the bone",
        size=3,
        default=(0.0, 0.0, 0.0),
        subtype='XYZ',
    )
    bpy.types.Scene.witcher_pelvis_offset_rot = FloatVectorProperty(
        name="Rotation Offset (deg)",
        description="XYZ Euler rotation offset in degrees to add to every keyframe of the bone",
        size=3,
        default=(0.0, 0.0, 0.0),
        subtype='EULER',
    )
    bpy.types.Scene.witcher_pelvis_ref_loc = FloatVectorProperty(
        name="Ref Loc",
        size=3,
        default=(0.0, 0.0, 0.0),
        options={'SKIP_SAVE'},
    )
    bpy.types.Scene.witcher_pelvis_ref_rot = FloatVectorProperty(
        name="Ref Rot",
        size=4,
        default=(1.0, 0.0, 0.0, 0.0),
        options={'SKIP_SAVE'},
    )
    bpy.types.Scene.witcher_pelvis_has_ref = BoolProperty(
        name="Has Reference Pose",
        default=False,
        options={'SKIP_SAVE'},
    )


def unregister():
    for prop_name in (
        "witcher_anim_tab",
        "witcher_w2anims_list",
        "witcher_w2anims_list_index",
        "witcher_loaded_w2anims_path",
        "witcher_loaded_w2anims_source_tag",
        "witcher_animset_filter_text",
        "witcher_load_anim_on_select",
        "witcher_anim_nla_mode",
        "witcher_w3_anim_source",
        "witcher_auto_orient_root",
        "witcher_motion_extraction_debug_compressed",
        "witcher_motion_extraction_debug_uncompressed",
        "witcher_w2cutscene_list",
        "witcher_w2cutscene_list_index",
        "witcher_loaded_w2cutscene_path",
        "witcher_w2scene_list",
        "witcher_w2scene_list_index",
        "witcher_loaded_w2scene_path",
        "witcher_prefer_uncompressed_anims",
        "witcher_bake_every_frame",
        "witcher_smooth_missing_frames",
        "witcher_scale_keys_to_duration",
        "witcher_anim_export_set",
        "witcher_anim_export_set_index",
        "witcher_anim_export_repo_path",
        "witcher_cutscene_export_repo_path",
        "witcher_cutscene_scratch_camera_repo_path",
        "witcher_cutscene_scratch_actor_name",
        "witcher_cutscene_scratch_actor_template",
        "witcher_cutscene_scratch_actor_appearance",
        "witcher_cutscene_scratch_actor_type",
        "witcher_cutscene_scratch_use_mimic",
        "witcher_cutscene_scratch_action_name",
        "witcher_cutscene_scratch_component",
        "witcher_cutscene_scratch_multipart_name",
        "witcher_cutscene_scratch_strip_length",
        "witcher_cutscene_scratch_add_after_last",
        "witcher_cutscene_scratch_validation_report",
        "witcher_pelvis_bone_name",
        "witcher_pelvis_offset_loc",
        "witcher_pelvis_offset_rot",
        "witcher_pelvis_ref_loc",
        "witcher_pelvis_ref_rot",
        "witcher_pelvis_has_ref",
    ):
        if hasattr(bpy.types.Scene, prop_name):
            delattr(bpy.types.Scene, prop_name)
    #bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    #del bpy.types.Scene.anim_export_name
    for c in reversed(classes):
        bpy.utils.unregister_class(c)

if __name__ == '__main__':
    register()
