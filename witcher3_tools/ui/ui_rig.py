import json
import logging
import time

import bpy
from bpy.app.handlers import persistent
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, FloatVectorProperty, IntProperty, StringProperty
from bpy.types import PropertyGroup, UIList

from .. import pose_key_tools
from ..lipsync import redkit_project
from .armature_context import get_main_armature

log = logging.getLogger(__name__)
_REDKIT_PROJECT_ENUM_CACHE = []
_RIG_ROTATION_UPDATE_LOCK = False
_RIG_HAND_TRACK_UPDATE_LOCK = False
_RIG_POSEKEY_LIST_UPDATE_LOCK = False
_RIG_POSE_SETTINGS_UPDATE_LOCK = False
_RIG_POSEKEY_REFRESH_PENDING = False
_RIG_POSE_SYNC_TIMER_RUNNING = False
_RIG_AUTO_CAPTURE_LOCK = False
_RIG_AUTO_CAPTURE_MODAL_RUNNING = False
_RIG_AUTO_CAPTURE_IDLE_DELAY = 0.22


def _find_character_armature(context):
    return get_main_armature(
        context,
        prefer_active=True,
        remember=True,
        fallback=True,
        allow_auxiliary_active=True,
    )


class WITCH_PG_RigBoneItem(PropertyGroup):
    name: StringProperty(default="")
    label: StringProperty(default="")
    parent_name: StringProperty(default="")
    group: StringProperty(default="FK")
    enabled: BoolProperty(default=False)
    depth: IntProperty(default=0)


class WITCH_PG_RigPresetItem(PropertyGroup):
    name: StringProperty(default="")
    label: StringProperty(default="")
    source_path: StringProperty(default="", subtype='FILE_PATH')
    data_json: StringProperty(default="")
    version: IntProperty(default=0)
    bone_count: IntProperty(default=0)
    hand_count: IntProperty(default=0)
    ik_count: IntProperty(default=0)
    track_count: IntProperty(default=0)


class WITCH_PG_RigPoseKeyItem(PropertyGroup):
    name: StringProperty(default="")
    label: StringProperty(default="")
    track_name: StringProperty(default="")
    event_name: StringProperty(default="")
    actor: StringProperty(default="")
    start_frame: bpy.props.FloatProperty(default=0.0)
    duration_frames: bpy.props.FloatProperty(default=0.0)
    bone_count: IntProperty(default=0)


class WITCH_UL_RigBoneList(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type not in {'DEFAULT', 'COMPACT'}:
            layout.label(text=item.label or item.name)
            return

        row = layout.row(align=True)
        row.prop(item, "enabled", text="")
        icon_name = 'BONE_DATA'
        if item.group == "IK":
            icon_name = 'CONSTRAINT'
        op = row.operator(WITCH_OT_RigSelectBone.bl_idname, text="", icon='RESTRICT_SELECT_OFF')
        op.bone_name = item.name
        row.label(text=item.label or item.name, icon=icon_name)


class WITCH_UL_RigPresetList(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type not in {'DEFAULT', 'COMPACT'}:
            layout.label(text=item.label or item.name)
            return

        row = layout.row(align=True)
        row.label(text="", icon='PRESET')
        row.label(text=item.label or item.name)


class WITCH_UL_RigPoseKeyList(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type not in {'DEFAULT', 'COMPACT'}:
            layout.label(text=item.label or item.name)
            return

        row = layout.row(align=True)
        row.label(text="", icon='NLA')
        row.label(text=item.label or item.name)


def _redkit_project_enum_items(self, context):
    global _REDKIT_PROJECT_ENUM_CACHE
    items = []
    for index, project_path in redkit_project.iter_project_paths(context):
        name = project_path.name or f"Project {index + 1}"
        items.append((str(index), name, str(project_path), 'FILE_FOLDER', index))
    if not items:
        items.append(("NONE", "No REDkit Project", "Add REDkit projects in Add-on Preferences", 'ERROR', 0))
    _REDKIT_PROJECT_ENUM_CACHE = items
    return _REDKIT_PROJECT_ENUM_CACHE


def _on_redkit_project_update(self, context):
    value = str(getattr(self, "witcher_rig_redkit_project", "") or "")
    if value == "NONE":
        return
    try:
        redkit_project.set_active_project_index(context, int(value))
    except Exception:
        log.debug("Could not set active REDkit project from rig selector", exc_info=True)


def _active_project_index(context):
    try:
        from .. import get_all_addon_prefs

        prefs = get_all_addon_prefs(context)
        return int(getattr(prefs, "redkit_projects_index", 0) or 0)
    except Exception:
        return 0


def _sync_project_selector_from_preferences(scene, context):
    projects = redkit_project.iter_project_paths(context)
    if not projects:
        try:
            if str(getattr(scene, "witcher_rig_redkit_project", "") or "") != "NONE":
                scene.witcher_rig_redkit_project = "NONE"
        except Exception:
            pass
        return
    valid_indices = {str(index) for index, _path in projects}
    current = str(_active_project_index(context))
    if current not in valid_indices:
        current = str(projects[0][0])
        redkit_project.set_active_project_index(context, current)
    try:
        if str(getattr(scene, "witcher_rig_redkit_project", "") or "") != current:
            scene.witcher_rig_redkit_project = current
    except Exception:
        pass


def _bone_depth(pose_bone):
    depth = 0
    parent = getattr(pose_bone, "parent", None)
    while parent is not None and depth < 32:
        depth += 1
        parent = getattr(parent, "parent", None)
    return depth


def _bone_group(name, mode):
    if mode == 'IK':
        return "IK" if pose_key_tools.is_game_ik_marker(name) else ""
    if mode == 'HAND':
        return "HAND" if pose_key_tools.is_hand_bone_name(name) else ""
    return "FK"


def refresh_rig_bone_list(context, mode="FK"):
    scene = context.scene
    armature = _find_character_armature(context)
    old_index = int(getattr(scene, "witcher_rig_bone_list_index", 0) or 0)
    old_items = getattr(scene, "witcher_rig_bone_list", []) or []
    preferred_bone_name = ""
    try:
        context_bone_name = str(_context_selected_bone_name(context) or "")
    except Exception:
        context_bone_name = ""
    if context_bone_name and _bone_group(context_bone_name, mode):
        preferred_bone_name = context_bone_name
    if 0 <= old_index < len(old_items):
        preferred_bone_name = preferred_bone_name or str(getattr(old_items[old_index], "name", "") or "")
    if not preferred_bone_name:
        preferred_bone_name = str(getattr(scene, "witcher_rig_active_bone_name", "") or "")
    if not preferred_bone_name:
        preferred_bone_name = str(getattr(scene, "witcher_rig_last_selected_bone", "") or "")

    scene.witcher_rig_bone_list.clear()
    scene.witcher_rig_bone_list_mode = mode
    if armature is None or getattr(armature, "pose", None) is None:
        scene.witcher_rig_bone_list_index = 0
        return 0

    active_entries = {
        str(entry.get("name", "")): entry
        for entry in pose_key_tools.pose_key_bone_entries(context, armature, mode)
    }
    active_only = bool(getattr(scene, "witcher_rig_show_active_only", False))
    count = 0
    selected_index = -1
    for bone_name in pose_key_tools.ordered_pose_bone_names(armature):
        group = _bone_group(bone_name, mode)
        if not group:
            continue
        is_active = bone_name in active_entries
        is_selected_override = bone_name == preferred_bone_name
        if active_only and not is_active and not is_selected_override:
            continue
        pose_bone = armature.pose.bones.get(bone_name)
        if pose_bone is None:
            continue
        depth = _bone_depth(pose_bone)
        item = scene.witcher_rig_bone_list.add()
        item.name = bone_name
        item.parent_name = pose_bone.parent.name if pose_bone.parent else ""
        item.group = group
        item.depth = depth
        indent = "  " * min(depth, 6)
        item.label = f"{indent}{bone_name}"
        item.enabled = is_active or group in {"HAND", "IK"}
        if bone_name == preferred_bone_name:
            selected_index = count
        count += 1
    if selected_index >= 0:
        scene.witcher_rig_bone_list_index = selected_index
        scene.witcher_rig_active_bone_name = preferred_bone_name
        scene.witcher_rig_last_selected_bone = preferred_bone_name
    else:
        scene.witcher_rig_bone_list_index = min(old_index, max(0, count - 1))
    return count


def _active_list_bone(context):
    scene = context.scene
    idx = int(getattr(scene, "witcher_rig_bone_list_index", 0) or 0)
    items = getattr(scene, "witcher_rig_bone_list", [])
    if 0 <= idx < len(items):
        return items[idx]
    return None


def _set_pose_bone_selected(pose_bone, selected):
    selected = bool(selected)
    for candidate in (pose_bone, getattr(pose_bone, "bone", None)):
        if candidate is None:
            continue
        select_set = getattr(candidate, "select_set", None)
        if callable(select_set):
            try:
                select_set(selected)
                return True
            except Exception:
                pass
        if hasattr(candidate, "select"):
            try:
                candidate.select = selected
                return True
            except Exception:
                pass
    return False


def _pose_bone_is_selected(context, pose_bone):
    if pose_bone is None:
        return False
    try:
        selected_pose_bones = getattr(context, "selected_pose_bones", None)
        if selected_pose_bones is not None and pose_bone in selected_pose_bones:
            return True
    except Exception:
        pass
    for candidate in (pose_bone, getattr(pose_bone, "bone", None)):
        if candidate is None:
            continue
        select_get = getattr(candidate, "select_get", None)
        if callable(select_get):
            try:
                return bool(select_get())
            except Exception:
                pass
        if hasattr(candidate, "select"):
            try:
                return bool(candidate.select)
            except Exception:
                pass
    return False


def _set_active_pose_bone(armature, pose_bone):
    data_bones = getattr(getattr(armature, "data", None), "bones", None)
    if data_bones is None:
        return False
    try:
        data_bones.active = pose_bone.bone
        return True
    except Exception:
        try:
            data_bones.active = data_bones.get(pose_bone.name)
            return True
        except Exception:
            pass
    return False


def _selected_pose_bone_entries(context, mode):
    armature = _find_character_armature(context)
    if armature is None or getattr(armature, "pose", None) is None:
        return []
    entries = []
    for bone in armature.pose.bones:
        if not _pose_bone_is_selected(context, bone):
            continue
        group = _bone_group(bone.name, mode)
        if group:
            entries.append({"name": bone.name, "group": group})
    return entries


def _enabled_list_bone_entries(context, mode):
    entries = []
    for item in getattr(context.scene, "witcher_rig_bone_list", []) or []:
        if not bool(getattr(item, "enabled", False)):
            continue
        group = str(getattr(item, "group", "") or _bone_group(item.name, mode) or mode)
        entries.append({"name": item.name, "group": group})
    return entries


def _rotation_values_are_zero(values, epsilon=1e-5):
    if values is None:
        return True
    try:
        values = tuple(values)[:3]
    except Exception:
        return True
    return all(abs(float(value)) <= epsilon for value in values)


def _pose_key_bone_is_active(context, armature, bone_name, mode):
    return any(
        str(entry.get("name", "") or "") == bone_name
        for entry in pose_key_tools.pose_key_bone_entries(context, armature, mode)
    )


def _maybe_auto_enable_bone_after_rotation(
    context,
    armature,
    bone_name,
    mode,
    previous_rotation,
    new_rotation,
    was_active,
):
    scene = getattr(context, "scene", None)
    if scene is None or mode != 'FK':
        return False
    if not bool(getattr(scene, "witcher_rig_auto_enable_pose_bone", True)):
        return False
    if was_active:
        return False
    if not _rotation_values_are_zero(previous_rotation):
        return False
    if _rotation_values_are_zero(new_rotation):
        return False

    scene.witcher_rig_active_bone_name = bone_name
    scene.witcher_rig_last_selected_bone = bone_name
    index = _bone_list_index_by_name(context, bone_name)
    if index < 0 or getattr(scene, "witcher_rig_bone_list_mode", "") != mode or bool(getattr(scene, "witcher_rig_show_active_only", False)):
        refresh_rig_bone_list(context, mode)
        index = _bone_list_index_by_name(context, bone_name)
    if index >= 0:
        scene.witcher_rig_bone_list_index = index
        try:
            scene.witcher_rig_bone_list[index].enabled = True
        except Exception:
            pass
    return True


def _last_selected_bone_entry(context, mode):
    bone_name = str(getattr(context.scene, "witcher_rig_last_selected_bone", "") or "").strip()
    if not bone_name:
        return []
    group = _bone_group(bone_name, mode)
    if not group:
        return []
    armature = _find_character_armature(context)
    pose_bones = getattr(getattr(armature, "pose", None), "bones", None) if armature is not None else None
    if pose_bones is None or pose_bones.get(bone_name) is None:
        return []
    return [{"name": bone_name, "group": group}]


def _scene_actor_value(context, armature):
    scene_value = str(getattr(context.scene, "witcher_rig_pose_actor", "") or "").strip()
    return scene_value or pose_key_tools.actor_name_for_armature(armature)


def _selected_preset_item(context):
    scene = context.scene
    idx = int(getattr(scene, "witcher_rig_preset_list_index", 0) or 0)
    items = getattr(scene, "witcher_rig_preset_list", [])
    if 0 <= idx < len(items):
        return items[idx]
    return None


def _selected_pose_key_item(context):
    scene = context.scene
    idx = int(getattr(scene, "witcher_rig_pose_key_list_index", 0) or 0)
    items = getattr(scene, "witcher_rig_pose_key_list", [])
    if 0 <= idx < len(items):
        return items[idx]
    return None


def _pose_key_list_signature(armature):
    parts = [f"ARMATURE|{str(getattr(armature, 'name', '') or '')}"]
    for item in pose_key_tools.iter_pose_key_strips(armature):
        strip = item.get("strip")
        metadata = item.get("metadata", {}) or {}
        parts.append("|".join((
            str(getattr(strip, "name", "") or ""),
            str(item.get("trackName", "") or ""),
            f"{float(item.get('frameStart', 0.0) or 0.0):.4f}",
            f"{float(item.get('durationFrames', 0.0) or 0.0):.4f}",
            str(metadata.get("eventName", "") or ""),
            str(len(metadata.get("bones", []) or [])),
        )))
    return "\n".join(parts)


def _sync_pose_key_settings_from_strip(context, strip):
    global _RIG_POSE_SETTINGS_UPDATE_LOCK
    if strip is None:
        return
    scene = context.scene
    metadata = pose_key_tools.pose_key_metadata_from_strip(strip)
    scene.witcher_rig_last_pose_key_strip = str(getattr(strip, "name", "") or "")
    scene.witcher_rig_last_pose_key_metadata = json.dumps(metadata, sort_keys=True)
    _RIG_POSE_SETTINGS_UPDATE_LOCK = True
    try:
        scene.witcher_rig_pose_start_frame = float(getattr(strip, "frame_start", 0.0) or 0.0)
        if metadata:
            scene.witcher_rig_pose_actor = str(metadata.get("actor", "") or getattr(scene, "witcher_rig_pose_actor", "") or "")
            scene.witcher_rig_pose_event_name = str(metadata.get("eventName", "") or getattr(scene, "witcher_rig_pose_event_name", "") or "Pose key")
            scene.witcher_rig_pose_duration = float(metadata.get("duration", scene.witcher_rig_pose_duration))
            scene.witcher_rig_pose_blend_in = float(metadata.get("blendIn", scene.witcher_rig_pose_blend_in))
            scene.witcher_rig_pose_blend_out = float(metadata.get("blendOut", scene.witcher_rig_pose_blend_out))
            scene.witcher_rig_pose_weight = max(0.0, min(1.0, float(metadata.get("weight", scene.witcher_rig_pose_weight))))
            scene.witcher_rig_pose_link_to_dialogset = bool(metadata.get("linkToDialogset", scene.witcher_rig_pose_link_to_dialogset))
            scene.witcher_rig_pose_preset_name = str(metadata.get("presetName", scene.witcher_rig_pose_preset_name) or "None")
            scene.witcher_rig_pose_preset_version = int(metadata.get("presetVersion", scene.witcher_rig_pose_preset_version) or 0)
    finally:
        _RIG_POSE_SETTINGS_UPDATE_LOCK = False


def _update_selected_pose_key_item_from_strip(context, armature, strip):
    if strip is None:
        return
    item = _selected_pose_key_item(context)
    if item is None or str(getattr(item, "name", "") or "") != str(getattr(strip, "name", "") or ""):
        return
    metadata = pose_key_tools.pose_key_metadata_from_strip(strip)
    item.track_name = ""
    try:
        for data in pose_key_tools.iter_pose_key_strips(armature):
            if data.get("strip") == strip:
                item.track_name = str(data.get("trackName", "") or "")
                break
    except Exception:
        pass
    item.event_name = str(metadata.get("eventName", "") or item.name or "Pose key")
    item.actor = str(metadata.get("actor", "") or "")
    item.start_frame = float(getattr(strip, "frame_start", 0.0) or 0.0)
    item.duration_frames = max(
        0.0,
        float(getattr(strip, "frame_end", 0.0) or 0.0) - float(getattr(strip, "frame_start", 0.0) or 0.0),
    )
    item.bone_count = len(metadata.get("bones", []) or [])
    item.label = f"{item.event_name}  F {item.start_frame:g}  D {item.duration_frames:g}"
    context.scene.witcher_rig_pose_key_list_signature = _pose_key_list_signature(armature)


def refresh_rig_pose_key_list(context, armature=None):
    global _RIG_POSEKEY_LIST_UPDATE_LOCK
    scene = context.scene
    armature = armature or _find_character_armature(context)
    current = str(getattr(scene, "witcher_rig_last_pose_key_strip", "") or "")
    if not current:
        item = _selected_pose_key_item(context)
        current = str(getattr(item, "name", "") or "") if item is not None else ""

    _RIG_POSEKEY_LIST_UPDATE_LOCK = True
    try:
        scene.witcher_rig_pose_key_list.clear()
        if armature is None:
            scene.witcher_rig_pose_key_list_index = 0
            scene.witcher_rig_pose_key_list_signature = ""
            return 0

        selected_index = -1
        pose_keys = pose_key_tools.iter_pose_key_strips(armature)
        for index, data in enumerate(pose_keys):
            strip = data.get("strip")
            metadata = data.get("metadata", {}) or {}
            item = scene.witcher_rig_pose_key_list.add()
            item.name = str(data.get("name", "") or "")
            item.track_name = str(data.get("trackName", "") or "")
            item.event_name = str(metadata.get("eventName", "") or item.name or "Pose key")
            item.actor = str(metadata.get("actor", "") or "")
            item.start_frame = float(data.get("frameStart", 0.0) or 0.0)
            item.duration_frames = float(data.get("durationFrames", 0.0) or 0.0)
            item.bone_count = len(metadata.get("bones", []) or [])
            item.label = f"{item.event_name}  F {item.start_frame:g}  D {item.duration_frames:g}"
            if item.name == current:
                selected_index = index

        if selected_index < 0 and pose_keys:
            selected_index = 0
        scene.witcher_rig_pose_key_list_index = max(0, selected_index)
        scene.witcher_rig_pose_key_list_signature = _pose_key_list_signature(armature)
    finally:
        _RIG_POSEKEY_LIST_UPDATE_LOCK = False

    item = _selected_pose_key_item(context)
    if item is not None:
        strip = pose_key_tools.set_active_pose_key_strip(context, armature, item.name)
        _sync_pose_key_settings_from_strip(context, strip)
        _sync_hand_tracks_from_pose_key(context, armature)
    return len(getattr(scene, "witcher_rig_pose_key_list", []) or [])


def _pose_key_list_is_stale(context, armature):
    if armature is None:
        return False
    signature = _pose_key_list_signature(armature)
    return signature != str(getattr(context.scene, "witcher_rig_pose_key_list_signature", "") or "")


def _schedule_pose_key_list_refresh(context, armature):
    global _RIG_POSEKEY_REFRESH_PENDING
    if _RIG_POSEKEY_REFRESH_PENDING or armature is None:
        return
    scene_name = str(getattr(getattr(context, "scene", None), "name", "") or "")
    armature_name = str(getattr(armature, "name", "") or "")
    if not scene_name or not armature_name:
        return
    _RIG_POSEKEY_REFRESH_PENDING = True

    def _run_refresh():
        global _RIG_POSEKEY_REFRESH_PENDING
        _RIG_POSEKEY_REFRESH_PENDING = False
        scene = bpy.data.scenes.get(scene_name)
        refresh_armature = bpy.data.objects.get(armature_name)
        if scene is None or refresh_armature is None:
            return None
        try:
            if bpy.context.scene != scene:
                return None
            refresh_rig_pose_key_list(bpy.context, refresh_armature)
        except Exception:
            log.debug("Deferred PoseKey list refresh failed", exc_info=True)
        return None

    try:
        bpy.app.timers.register(_run_refresh, first_interval=0.01)
    except Exception:
        _RIG_POSEKEY_REFRESH_PENDING = False


def _ensure_pose_key_list_current(context, armature):
    if armature is None:
        return 0
    if _pose_key_list_is_stale(context, armature):
        _schedule_pose_key_list_refresh(context, armature)
    return len(getattr(context.scene, "witcher_rig_pose_key_list", []) or [])


def _active_redkit_project_path(context):
    value = str(getattr(context.scene, "witcher_rig_redkit_project", "") or "")
    if value and value != "NONE":
        try:
            redkit_project.set_active_project_index(context, int(value))
        except Exception:
            pass
    return redkit_project.get_active_project_path(context)


def _context_selected_bone_name(context):
    active_pose_bone = getattr(context, "active_pose_bone", None)
    if active_pose_bone is not None and str(getattr(active_pose_bone, "name", "") or ""):
        return str(active_pose_bone.name)

    armature = getattr(context, "object", None)
    if getattr(armature, "type", None) != 'ARMATURE':
        armature = getattr(getattr(context, "view_layer", None), "objects", None)
        armature = getattr(armature, "active", None)
    if getattr(armature, "type", None) == 'ARMATURE' and str(getattr(armature, "mode", "") or "") == 'POSE':
        data_bones = getattr(getattr(armature, "data", None), "bones", None)
        active_data_bone = getattr(data_bones, "active", None) if data_bones is not None else None
        if active_data_bone is not None and str(getattr(active_data_bone, "name", "") or ""):
            return str(active_data_bone.name)

    try:
        selected_pose_bones = getattr(context, "selected_pose_bones", None) or []
    except Exception:
        selected_pose_bones = []
    for pose_bone in selected_pose_bones:
        name = str(getattr(pose_bone, "name", "") or "")
        if name:
            return name
    return ""


def _pose_rotation_signature(armature, bone_name):
    pose_bone = None
    try:
        pose_bone = armature.pose.bones.get(bone_name)
    except Exception:
        pose_bone = None
    if pose_bone is None:
        return ""

    mode = str(getattr(pose_bone, "rotation_mode", "QUATERNION") or "QUATERNION")
    try:
        matrix = getattr(pose_bone, "matrix_basis", None)
        if matrix is not None:
            values = tuple(float(matrix[row][col]) for row in range(4) for col in range(4))
        elif mode == 'QUATERNION':
            values = tuple(float(v) for v in pose_bone.rotation_quaternion)
        elif mode == 'AXIS_ANGLE':
            values = tuple(float(v) for v in pose_bone.rotation_axis_angle)
        else:
            values = tuple(float(v) for v in pose_bone.rotation_euler)
    except Exception:
        return ""
    rounded = ",".join(f"{value:.6f}" for value in values)
    return f"{getattr(armature, 'name', '')}|{bone_name}|{mode}|{rounded}"


def _pose_capture_signature(context, armature, bone_name):
    rotation_signature = _pose_rotation_signature(armature, bone_name)
    if not rotation_signature:
        return ""
    scene = getattr(context, "scene", None)
    strip_name = str(getattr(scene, "witcher_rig_last_pose_key_strip", "") or "") if scene is not None else ""
    mode = str(getattr(scene, "witcher_rig_tab", "FK") or "FK") if scene is not None else "FK"
    return f"{strip_name}|{mode}|{rotation_signature}"


def _set_auto_capture_baseline(context, armature=None, bone_name=None):
    scene = getattr(context, "scene", None)
    if scene is None:
        return
    if armature is None:
        armature = _find_character_armature(context)
    bone_name = str(bone_name or _selected_or_active_bone_name(context) or "").strip()
    signature = _pose_capture_signature(context, armature, bone_name) if armature is not None and bone_name else ""
    try:
        scene.witcher_rig_last_pose_capture_signature = signature
    except Exception:
        pass


def _selected_or_active_bone_name(context):
    selected_name = _context_selected_bone_name(context)
    if selected_name:
        return selected_name
    item = _active_list_bone(context)
    if item is not None and str(getattr(item, "name", "") or ""):
        return str(item.name)
    return str(getattr(context.scene, "witcher_rig_last_selected_bone", "") or "")


def _bone_list_index_by_name(context, bone_name):
    bone_name = str(bone_name or "")
    if not bone_name:
        return -1
    for index, item in enumerate(getattr(context.scene, "witcher_rig_bone_list", []) or []):
        if str(getattr(item, "name", "") or "") == bone_name:
            return index
    return -1


def _select_bone_in_panel(context, bone_name, mode):
    scene = context.scene
    bone_name = str(bone_name or "").strip()
    if not bone_name:
        return False
    if str(getattr(scene, "witcher_rig_active_bone_name", "") or "") != bone_name:
        scene.witcher_rig_active_bone_name = bone_name
    if str(getattr(scene, "witcher_rig_last_selected_bone", "") or "") != bone_name:
        scene.witcher_rig_last_selected_bone = bone_name
    if getattr(scene, "witcher_rig_bone_list_mode", "") != mode:
        refresh_rig_bone_list(context, mode)
    index = _bone_list_index_by_name(context, bone_name)
    if index < 0:
        refresh_rig_bone_list(context, mode)
        index = _bone_list_index_by_name(context, bone_name)
    if index < 0:
        return False
    if int(getattr(scene, "witcher_rig_bone_list_index", 0) or 0) != index:
        scene.witcher_rig_bone_list_index = index
    return True

def _active_pose_mode_armature_and_bone(context):
    armature = getattr(context, "object", None)
    if getattr(armature, "type", None) != 'ARMATURE':
        active_objects = getattr(getattr(context, "view_layer", None), "objects", None)
        armature = getattr(active_objects, "active", None)
    if getattr(armature, "type", None) != 'ARMATURE':
        armature = _find_character_armature(context)
    if getattr(armature, "type", None) != 'ARMATURE' or str(getattr(armature, "mode", "") or "") != 'POSE':
        return None, ""
    bone_name = _context_selected_bone_name(context)
    if not bone_name:
        data_bones = getattr(getattr(armature, "data", None), "bones", None)
        active_data_bone = getattr(data_bones, "active", None) if data_bones is not None else None
        bone_name = str(getattr(active_data_bone, "name", "") or "")
    if not bone_name:
        scene = getattr(context, "scene", None)
        candidate = str(getattr(scene, "witcher_rig_active_bone_name", "") or "") if scene is not None else ""
        if candidate in getattr(armature.pose, "bones", {}):
            bone_name = candidate
    if not bone_name:
        return None, ""
    return armature, bone_name


def _sync_rotation_from_bone(context, bone_name=None):
    global _RIG_ROTATION_UPDATE_LOCK
    armature = _find_character_armature(context)
    bone_name = str(bone_name or _selected_or_active_bone_name(context) or "").strip()
    if armature is None or not bone_name:
        return
    mode = getattr(context.scene, "witcher_rig_tab", "FK")
    values = pose_key_tools.pose_key_bone_rotation_euler(context, armature, bone_name, group=mode if mode in {'FK', 'HAND', 'IK'} else None)
    if values is None:
        return
    _RIG_ROTATION_UPDATE_LOCK = True
    try:
        context.scene.witcher_rig_active_bone_name = bone_name
        context.scene.witcher_rig_pose_rotation = values
    finally:
        _RIG_ROTATION_UPDATE_LOCK = False


def _sync_hand_tracks_from_pose_key(context, armature=None):
    global _RIG_HAND_TRACK_UPDATE_LOCK
    scene = getattr(context, "scene", None)
    if scene is None:
        return
    armature = armature or _find_character_armature(context)
    values = pose_key_tools.pose_key_hand_tracks(context, armature)
    _RIG_HAND_TRACK_UPDATE_LOCK = True
    try:
        scene.witcher_rig_hand_tracks = tuple(values)
    finally:
        _RIG_HAND_TRACK_UPDATE_LOCK = False


def _on_rig_bone_list_index_update(self, context):
    _sync_rotation_from_bone(context)
    _set_auto_capture_baseline(context)


def _on_rig_pose_key_list_index_update(self, context):
    if _RIG_POSEKEY_LIST_UPDATE_LOCK:
        return
    armature = _find_character_armature(context)
    item = _selected_pose_key_item(context)
    if armature is None or item is None:
        return
    strip = pose_key_tools.set_active_pose_key_strip(context, armature, item.name)
    _sync_pose_key_settings_from_strip(context, strip)
    mode = getattr(context.scene, "witcher_rig_tab", "FK")
    if mode in {'FK', 'HAND', 'IK'}:
        refresh_rig_bone_list(context, mode)
    _sync_rotation_from_bone(context)
    _sync_hand_tracks_from_pose_key(context, armature)
    _set_auto_capture_baseline(context, armature)


def _on_rig_pose_settings_update(self, context):
    if _RIG_POSE_SETTINGS_UPDATE_LOCK:
        return
    scene = context.scene
    armature = _find_character_armature(context)
    if armature is None:
        return
    strip = pose_key_tools.find_pose_key_strip(context, armature)
    if strip is None:
        return
    try:
        metadata = pose_key_tools.update_pose_key_strip_settings(
            context,
            armature,
            strip,
            actor=_scene_actor_value(context, armature),
            event_name=str(getattr(scene, "witcher_rig_pose_event_name", "Pose key") or "Pose key"),
            start_frame=float(getattr(scene, "witcher_rig_pose_start_frame", getattr(strip, "frame_start", 0.0)) or 0.0),
            duration=float(getattr(scene, "witcher_rig_pose_duration", 1.0)),
            blend_in=float(getattr(scene, "witcher_rig_pose_blend_in", 0.0) or 0.0),
            blend_out=float(getattr(scene, "witcher_rig_pose_blend_out", 0.0) or 0.0),
            weight=float(getattr(scene, "witcher_rig_pose_weight", 1.0) or 1.0),
            link_to_dialogset=bool(getattr(scene, "witcher_rig_pose_link_to_dialogset", True)),
            preset_name=str(getattr(scene, "witcher_rig_pose_preset_name", "None") or "None"),
            preset_version=int(getattr(scene, "witcher_rig_pose_preset_version", 0) or 0),
            nla_blend_type=str(getattr(scene, "witcher_rig_pose_nla_blend_type", "COMBINE") or "COMBINE"),
        )
        scene.witcher_rig_last_pose_key_strip = str(getattr(strip, "name", "") or "")
        scene.witcher_rig_last_pose_key_metadata = json.dumps(metadata, sort_keys=True)
        _update_selected_pose_key_item_from_strip(context, armature, strip)
    except Exception as exc:
        scene.witcher_rig_pose_status = str(exc)


def _on_rig_pose_rotation_update(self, context):
    global _RIG_AUTO_CAPTURE_LOCK
    if _RIG_ROTATION_UPDATE_LOCK or _RIG_AUTO_CAPTURE_LOCK:
        return
    scene = context.scene
    armature = _find_character_armature(context)
    bone_name = str(getattr(scene, "witcher_rig_active_bone_name", "") or _selected_or_active_bone_name(context) or "").strip()
    if armature is None or not bone_name:
        return
    mode = getattr(scene, "witcher_rig_tab", "FK")
    group = _bone_group(bone_name, mode) or "FK"
    previous_rotation = pose_key_tools.pose_key_bone_rotation_euler(context, armature, bone_name)
    new_rotation = tuple(getattr(scene, "witcher_rig_pose_rotation", (0.0, 0.0, 0.0)))
    was_active = _pose_key_bone_is_active(context, armature, bone_name, mode)
    try:
        _RIG_AUTO_CAPTURE_LOCK = True
        metadata = pose_key_tools.set_pose_key_bone_rotation_euler(
            context,
            armature,
            bone_name,
            new_rotation,
            group=group,
        )
        scene.witcher_rig_last_pose_key_metadata = json.dumps(metadata, sort_keys=True)
        strip = pose_key_tools.find_pose_key_strip(context, armature)
        _update_selected_pose_key_item_from_strip(context, armature, strip)
        enabled = _maybe_auto_enable_bone_after_rotation(
            context,
            armature,
            bone_name,
            mode,
            previous_rotation,
            new_rotation,
            was_active,
        )
        if not enabled and bool(getattr(scene, "witcher_rig_show_active_only", False)):
            refresh_rig_bone_list(context, mode)
        _set_auto_capture_baseline(context, armature, bone_name)
        scene.witcher_rig_pose_status = f"Updated {bone_name}" + (" and enabled" if enabled else "")
    except Exception as exc:
        scene.witcher_rig_pose_status = str(exc)
    finally:
        _RIG_AUTO_CAPTURE_LOCK = False


def _on_rig_hand_tracks_update(self, context):
    if _RIG_HAND_TRACK_UPDATE_LOCK:
        return
    scene = context.scene
    armature = _find_character_armature(context)
    if armature is None or getattr(armature, "pose", None) is None:
        return
    try:
        metadata = pose_key_tools.set_pose_key_hand_tracks(
            context,
            armature,
            tuple(getattr(scene, "witcher_rig_hand_tracks", ())),
        )
        scene.witcher_rig_last_pose_key_metadata = json.dumps(metadata, sort_keys=True)
        strip = pose_key_tools.find_pose_key_strip(context, armature)
        _update_selected_pose_key_item_from_strip(context, armature, strip)
        if bool(getattr(scene, "witcher_rig_show_active_only", False)):
            refresh_rig_bone_list(context, getattr(scene, "witcher_rig_tab", "HAND"))
        scene.witcher_rig_pose_status = "Updated hands"
    except Exception as exc:
        scene.witcher_rig_pose_status = str(exc)


def _on_rig_active_only_update(self, context):
    mode = getattr(context.scene, "witcher_rig_tab", "FK")
    if mode in {'FK', 'HAND', 'IK'}:
        refresh_rig_bone_list(context, mode)
        _sync_rotation_from_bone(context)
        _set_auto_capture_baseline(context)


def _on_rig_tab_update(self, context):
    mode = getattr(context.scene, "witcher_rig_tab", "FK")
    if mode in {'FK', 'HAND', 'IK'}:
        refresh_rig_bone_list(context, mode)
        _sync_rotation_from_bone(context)
        if mode == 'HAND':
            _sync_hand_tracks_from_pose_key(context)
        _set_auto_capture_baseline(context)


def _capture_pose_key_viewport_rotation(context, armature, bone_name, mode, *, status_prefix="Captured"):
    scene = context.scene
    group = _bone_group(bone_name, mode) or "FK"
    previous_rotation = pose_key_tools.pose_key_bone_rotation_euler(context, armature, bone_name)
    was_active = _pose_key_bone_is_active(context, armature, bone_name, mode)
    metadata = pose_key_tools.set_pose_key_bone_rotation_from_current_pose(
        context,
        armature,
        bone_name,
        group=group,
    )
    scene.witcher_rig_last_pose_key_metadata = json.dumps(metadata, sort_keys=True)
    _sync_rotation_from_bone(context, bone_name)
    _set_auto_capture_baseline(context, armature, bone_name)
    strip = pose_key_tools.find_pose_key_strip(context, armature)
    _update_selected_pose_key_item_from_strip(context, armature, strip)
    new_rotation = pose_key_tools.pose_key_bone_rotation_euler(context, armature, bone_name)
    enabled = _maybe_auto_enable_bone_after_rotation(
        context,
        armature,
        bone_name,
        mode,
        previous_rotation,
        new_rotation,
        was_active,
    )
    if not enabled and bool(getattr(scene, "witcher_rig_show_active_only", False)):
        refresh_rig_bone_list(context, mode)
    scene.witcher_rig_pose_status = f"{status_prefix} {bone_name}" + (" and enabled" if enabled else "")
    return metadata


def _sync_pose_mode_selection(context):
    scene = getattr(context, "scene", None)
    if scene is None:
        return
    mode = getattr(scene, "witcher_rig_tab", "FK")
    if mode not in {'FK', 'HAND', 'IK'}:
        return

    armature, bone_name = _active_pose_mode_armature_and_bone(context)
    if armature is None or not bone_name:
        return
    group = _bone_group(bone_name, mode)
    if not group:
        return

    if (
        str(getattr(scene, "witcher_rig_last_pose_mode_bone", "") or "") == bone_name
        and str(getattr(scene, "witcher_rig_active_bone_name", "") or "") == bone_name
        and str(getattr(scene, "witcher_rig_bone_list_mode", "") or "") == mode
    ):
        return
    _select_bone_in_panel(context, bone_name, mode)
    scene.witcher_rig_last_pose_mode_bone = bone_name
    _set_auto_capture_baseline(context, armature, bone_name)


def _rig_pose_sync_timer():
    if not _RIG_POSE_SYNC_TIMER_RUNNING:
        return None
    try:
        _sync_pose_mode_selection(bpy.context)
    except Exception:
        log.debug("Rig pose sync timer failed", exc_info=True)
    armature, _bone_name = _active_pose_mode_armature_and_bone(bpy.context)
    if armature is not None:
        return 0.08
    return 0.25


@persistent
def _rig_pose_sync_depsgraph_handler(scene, depsgraph):
    if not _RIG_POSE_SYNC_TIMER_RUNNING:
        return
    try:
        _sync_pose_mode_selection(bpy.context)
    except Exception:
        log.debug("Rig pose sync depsgraph handler failed", exc_info=True)


def start_rig_pose_sync_timer():
    global _RIG_POSE_SYNC_TIMER_RUNNING
    try:
        handlers = bpy.app.handlers.depsgraph_update_post
        if _rig_pose_sync_depsgraph_handler not in handlers:
            handlers.append(_rig_pose_sync_depsgraph_handler)
    except Exception:
        log.debug("Could not register Rig pose sync depsgraph handler", exc_info=True)
    if _RIG_POSE_SYNC_TIMER_RUNNING:
        return
    _RIG_POSE_SYNC_TIMER_RUNNING = True
    try:
        is_registered = getattr(bpy.app.timers, "is_registered", None)
        if callable(is_registered) and is_registered(_rig_pose_sync_timer):
            return
    except Exception:
        pass
    try:
        bpy.app.timers.register(_rig_pose_sync_timer, first_interval=0.15)
    except Exception:
        _RIG_POSE_SYNC_TIMER_RUNNING = False


def stop_rig_pose_sync_timer():
    global _RIG_POSE_SYNC_TIMER_RUNNING
    _RIG_POSE_SYNC_TIMER_RUNNING = False
    try:
        handlers = bpy.app.handlers.depsgraph_update_post
        if _rig_pose_sync_depsgraph_handler in handlers:
            handlers.remove(_rig_pose_sync_depsgraph_handler)
    except Exception:
        pass
    try:
        is_registered = getattr(bpy.app.timers, "is_registered", None)
        if callable(is_registered) and not is_registered(_rig_pose_sync_timer):
            return
        bpy.app.timers.unregister(_rig_pose_sync_timer)
    except Exception:
        pass


class WITCH_OT_RigRefreshBones(bpy.types.Operator):
    bl_idname = "witcher.rig_refresh_bones"
    bl_label = "Refresh Bones"
    bl_options = {'REGISTER', 'UNDO'}

    mode: EnumProperty(
        items=[
            ('FK', "FK", ""),
            ('HAND', "Hands", ""),
            ('IK', "IK", ""),
        ],
        default='FK',
    )

    def execute(self, context):
        count = refresh_rig_bone_list(context, self.mode)
        self.report({'INFO'}, f"Loaded {count} {self.mode} bones")
        return {'FINISHED'}


class WITCH_OT_RigSelectBone(bpy.types.Operator):
    bl_idname = "witcher.rig_select_bone"
    bl_label = "Select Bone"
    bl_options = {'REGISTER', 'UNDO'}

    bone_name: StringProperty(default="")

    def execute(self, context):
        armature = _find_character_armature(context)
        if armature is None or getattr(armature, "pose", None) is None:
            self.report({'ERROR'}, "Select a character armature")
            return {'CANCELLED'}
        pose_bone = armature.pose.bones.get(self.bone_name)
        if pose_bone is None:
            self.report({'ERROR'}, f"Bone not found: {self.bone_name}")
            return {'CANCELLED'}

        context.view_layer.objects.active = armature
        armature.select_set(True)
        try:
            bpy.ops.object.mode_set(mode='POSE')
        except Exception:
            pass
        for bone in armature.pose.bones:
            _set_pose_bone_selected(bone, False)
        selected = _set_pose_bone_selected(pose_bone, True)
        active = _set_active_pose_bone(armature, pose_bone)
        context.scene.witcher_rig_last_selected_bone = self.bone_name
        context.scene.witcher_rig_active_bone_name = self.bone_name
        try:
            context.view_layer.update()
        except Exception:
            pass
        _sync_rotation_from_bone(context, self.bone_name)
        _set_auto_capture_baseline(context, armature, self.bone_name)
        if not selected and not active:
            self.report({'WARNING'}, "Bone API did not expose selection state")
            return {'CANCELLED'}
        return {'FINISHED'}


class WITCH_OT_RigToggleBoneSelection(bpy.types.Operator):
    bl_idname = "witcher.rig_toggle_bone_selection"
    bl_label = "Toggle List"
    bl_options = {'REGISTER', 'UNDO'}

    enabled: BoolProperty(default=True)

    def execute(self, context):
        for item in getattr(context.scene, "witcher_rig_bone_list", []) or []:
            item.enabled = bool(self.enabled)
        return {'FINISHED'}


class WITCH_OT_RigResetHandTrack(bpy.types.Operator):
    bl_idname = "witcher.rig_reset_hand_track"
    bl_label = "Reset Hand Track"
    bl_description = "Reset one Hands slider, or reset all Hands sliders"
    bl_options = {'REGISTER', 'UNDO'}

    side: EnumProperty(
        items=[
            ('L', "Left", ""),
            ('R', "Right", ""),
            ('ALL', "All", ""),
        ],
        default='ALL',
    )
    part: EnumProperty(
        items=[
            ('all', "All Fingers", ""),
            ('A', "A Pinky", ""),
            ('B', "B Ring", ""),
            ('C', "C Middle", ""),
            ('D', "D Index", ""),
            ('E', "E Thumb", ""),
            ('twist', "Twist", ""),
            ('handX', "Hand X", ""),
            ('handY', "Hand Y", ""),
            ('handZ', "Hand Z", ""),
        ],
        default='all',
    )
    all_tracks: BoolProperty(default=False)

    def execute(self, context):
        scene = context.scene
        values = list(getattr(scene, "witcher_rig_hand_tracks", (0.0,) * pose_key_tools.HAND_TRACK_COUNT))
        while len(values) < pose_key_tools.HAND_TRACK_COUNT:
            values.append(0.0)

        if self.all_tracks:
            values = [0.0] * pose_key_tools.HAND_TRACK_COUNT
        elif self.side == 'ALL':
            for side in ('L', 'R'):
                values[pose_key_tools.hand_track_index(side, self.part)] = 0.0
        else:
            values[pose_key_tools.hand_track_index(self.side, self.part)] = 0.0

        scene.witcher_rig_hand_tracks = tuple(values)
        return {'FINISHED'}


class WITCH_OT_RigResetBoneRotation(bpy.types.Operator):
    bl_idname = "witcher.rig_reset_bone_rotation"
    bl_label = "Reset Bone Rotation"
    bl_description = "Set this bone's active PoseKey rotation to 0, 0, 0"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        armature = _find_character_armature(context)
        bone_name = str(getattr(scene, "witcher_rig_active_bone_name", "") or _selected_or_active_bone_name(context) or "").strip()
        if armature is None or getattr(armature, "pose", None) is None:
            self.report({'ERROR'}, "Select a character armature")
            return {'CANCELLED'}
        if not bone_name:
            self.report({'ERROR'}, "Select a bone")
            return {'CANCELLED'}

        group = _bone_group(bone_name, getattr(scene, "witcher_rig_tab", "FK")) or "FK"
        try:
            metadata = pose_key_tools.set_pose_key_bone_rotation_euler(
                context,
                armature,
                bone_name,
                (0.0, 0.0, 0.0),
                group=group,
            )
        except Exception as exc:
            scene.witcher_rig_pose_status = str(exc)
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

        scene.witcher_rig_last_pose_key_metadata = json.dumps(metadata, sort_keys=True)
        _sync_rotation_from_bone(context, bone_name)
        _set_auto_capture_baseline(context, armature, bone_name)
        strip = pose_key_tools.find_pose_key_strip(context, armature)
        _update_selected_pose_key_item_from_strip(context, armature, strip)
        if bool(getattr(scene, "witcher_rig_show_active_only", False)):
            refresh_rig_bone_list(context, getattr(scene, "witcher_rig_tab", "FK"))
        scene.witcher_rig_pose_status = f"Reset {bone_name}"
        self.report({'INFO'}, scene.witcher_rig_pose_status)
        return {'FINISHED'}


class WITCH_OT_RigCaptureBoneRotation(bpy.types.Operator):
    bl_idname = "witcher.rig_capture_bone_rotation"
    bl_label = "Capture Viewport Rotation"
    bl_description = "Apply the selected Blender pose bone rotation to the active PoseKey"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        armature = _find_character_armature(context)
        bone_name = _context_selected_bone_name(context) or str(getattr(scene, "witcher_rig_active_bone_name", "") or _selected_or_active_bone_name(context) or "")
        bone_name = str(bone_name or "").strip()
        if armature is None or getattr(armature, "pose", None) is None:
            self.report({'ERROR'}, "Select a character armature")
            return {'CANCELLED'}
        if not bone_name:
            self.report({'ERROR'}, "Select a bone")
            return {'CANCELLED'}

        mode = getattr(scene, "witcher_rig_tab", "FK")
        try:
            _capture_pose_key_viewport_rotation(
                context,
                armature,
                bone_name,
                mode,
                status_prefix="Captured",
            )
        except Exception as exc:
            scene.witcher_rig_pose_status = str(exc)
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

        self.report({'INFO'}, scene.witcher_rig_pose_status)
        return {'FINISHED'}


class WITCH_OT_RigAutoCaptureModal(bpy.types.Operator):
    bl_idname = "witcher.rig_auto_capture_modal"
    bl_label = "Viewport Pose Capture"
    bl_description = "When playback is paused in Pose Mode, capture the selected viewport pose bone into the active PoseKey after the edit stops"
    bl_options = {'REGISTER'}

    _timer = None
    _pending_signature = ""
    _pending_since = 0.0
    _mouse_down = False
    _last_status = ""

    def _set_status(self, scene, message):
        message = str(message or "")
        if message == self._last_status:
            return
        self._last_status = message
        try:
            scene.witcher_rig_pose_status = message
        except Exception:
            pass

    def invoke(self, context, event):
        global _RIG_AUTO_CAPTURE_MODAL_RUNNING
        scene = getattr(context, "scene", None)
        window = getattr(context, "window", None)
        if scene is None or window is None:
            self.report({'ERROR'}, "Auto Capture needs an active Blender window")
            return {'CANCELLED'}
        if _RIG_AUTO_CAPTURE_MODAL_RUNNING:
            scene.witcher_rig_auto_capture_rotation = False
            scene.witcher_rig_pose_status = "Viewport Capture off"
            return {'FINISHED'}

        armature, bone_name = _active_pose_mode_armature_and_bone(context)
        if armature is None:
            armature = _find_character_armature(context)
            bone_name = _selected_or_active_bone_name(context)
        if armature is None or not bone_name:
            self.report({'ERROR'}, "Select a pose bone")
            return {'CANCELLED'}

        scene.witcher_rig_auto_capture_rotation = True
        _set_auto_capture_baseline(context, armature, bone_name)
        self._pending_signature = ""
        self._pending_since = 0.0
        self._mouse_down = False
        self._last_status = ""
        self._timer = context.window_manager.event_timer_add(0.18, window=window)
        context.window_manager.modal_handler_add(self)
        _RIG_AUTO_CAPTURE_MODAL_RUNNING = True
        self._set_status(scene, f"Viewport watching {bone_name}")
        return {'RUNNING_MODAL'}

    def _finish(self, context):
        global _RIG_AUTO_CAPTURE_MODAL_RUNNING
        if self._timer is not None:
            try:
                context.window_manager.event_timer_remove(self._timer)
            except Exception:
                pass
            self._timer = None
        _RIG_AUTO_CAPTURE_MODAL_RUNNING = False
        scene = getattr(context, "scene", None)
        if scene is not None:
            scene.witcher_rig_auto_capture_rotation = False
            scene.witcher_rig_pose_status = "Viewport Capture off"
        return {'FINISHED'}

    def _tick(self, context):
        global _RIG_AUTO_CAPTURE_LOCK
        scene = getattr(context, "scene", None)
        if scene is None or not bool(getattr(scene, "witcher_rig_auto_capture_rotation", False)):
            return
        if _RIG_AUTO_CAPTURE_LOCK or _RIG_ROTATION_UPDATE_LOCK:
            self._set_status(scene, "Viewport waiting")
            return
        if bool(getattr(getattr(context, "screen", None), "is_animation_playing", False)):
            self._pending_signature = ""
            self._set_status(scene, "Viewport capture paused")
            return

        mode = getattr(scene, "witcher_rig_tab", "FK")
        if mode != 'FK':
            self._pending_signature = ""
            self._set_status(scene, "Use FK tab for capture")
            return

        armature, bone_name = _active_pose_mode_armature_and_bone(context)
        if armature is None or not bone_name or not _bone_group(bone_name, mode):
            self._pending_signature = ""
            self._set_status(scene, "Select a pose bone")
            return

        strip, action = pose_key_tools.active_pose_key_action(context, armature)
        if strip is None or action is None:
            self._pending_signature = ""
            self._set_status(scene, "Select a PoseKey")
            return

        signature = _pose_capture_signature(context, armature, bone_name)
        if not signature:
            self._pending_signature = ""
            self._set_status(scene, "No pose data")
            return

        last_signature = str(getattr(scene, "witcher_rig_last_pose_capture_signature", "") or "")
        if not last_signature:
            scene.witcher_rig_last_pose_capture_signature = signature
            self._pending_signature = ""
            self._set_status(scene, f"Viewport watching {bone_name}")
            return
        if signature == last_signature:
            self._pending_signature = ""
            self._set_status(scene, f"Viewport watching {bone_name}")
            return

        now = time.monotonic()
        if signature != self._pending_signature:
            self._pending_signature = signature
            self._pending_since = now
            self._set_status(scene, f"Viewport pending {bone_name}")
            return
        if self._mouse_down or now - self._pending_since < _RIG_AUTO_CAPTURE_IDLE_DELAY:
            return

        try:
            _RIG_AUTO_CAPTURE_LOCK = True
            _capture_pose_key_viewport_rotation(
                context,
                armature,
                bone_name,
                mode,
                status_prefix="Viewport captured",
            )
            self._last_status = str(getattr(scene, "witcher_rig_pose_status", "") or "")
        except Exception as exc:
            scene.witcher_rig_last_pose_capture_signature = signature
            self._set_status(scene, str(exc))
            log.debug("Rig auto capture failed", exc_info=True)
        finally:
            _RIG_AUTO_CAPTURE_LOCK = False
            self._pending_signature = ""
            self._pending_since = 0.0

    def modal(self, context, event):
        scene = getattr(context, "scene", None)
        if scene is None or not bool(getattr(scene, "witcher_rig_auto_capture_rotation", False)):
            return self._finish(context)

        if event.type in {'LEFTMOUSE', 'MIDDLEMOUSE', 'RIGHTMOUSE'}:
            if event.value == 'PRESS':
                self._mouse_down = True
            elif event.value == 'RELEASE':
                self._mouse_down = False
                self._pending_since = time.monotonic()

        if event.type == 'TIMER':
            self._tick(context)

        return {'PASS_THROUGH'}

    def cancel(self, context):
        self._finish(context)


class WITCH_OT_RigRefreshPoseKeys(bpy.types.Operator):
    bl_idname = "witcher.rig_refresh_pose_keys"
    bl_label = "Refresh PoseKeys"
    bl_options = {'REGISTER'}

    def execute(self, context):
        count = refresh_rig_pose_key_list(context)
        self.report({'INFO'}, f"Loaded {count} PoseKeys")
        return {'FINISHED'}


class WITCH_OT_RigAddPoseKey(bpy.types.Operator):
    bl_idname = "witcher.rig_add_pose_key"
    bl_label = "Add PoseKey"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        armature = _find_character_armature(context)
        if armature is None or getattr(armature, "pose", None) is None:
            self.report({'ERROR'}, "Select a character armature")
            return {'CANCELLED'}

        scene = context.scene
        try:
            strip, _action, metadata = pose_key_tools.create_empty_pose_key(
                context,
                armature,
                actor=_scene_actor_value(context, armature),
                duration=float(getattr(scene, "witcher_rig_pose_duration", 1.0)),
                blend_in=float(getattr(scene, "witcher_rig_pose_blend_in", 0.0) or 0.0),
                blend_out=float(getattr(scene, "witcher_rig_pose_blend_out", 0.0) or 0.0),
                weight=float(getattr(scene, "witcher_rig_pose_weight", 1.0) or 1.0),
                link_to_dialogset=bool(getattr(scene, "witcher_rig_pose_link_to_dialogset", True)),
                preset_name=str(getattr(scene, "witcher_rig_pose_preset_name", "None") or "None"),
                preset_version=int(getattr(scene, "witcher_rig_pose_preset_version", 0) or 0),
                event_name=str(getattr(scene, "witcher_rig_pose_event_name", "Pose key") or "Pose key"),
                nla_blend_type=str(getattr(scene, "witcher_rig_pose_nla_blend_type", "COMBINE") or "COMBINE"),
            )
        except Exception as exc:
            log.exception("Failed to add PoseKey")
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

        scene.witcher_rig_last_pose_key_strip = getattr(strip, "name", "")
        scene.witcher_rig_last_pose_key_metadata = json.dumps(metadata, sort_keys=True)
        refresh_rig_pose_key_list(context)
        _sync_hand_tracks_from_pose_key(context, armature)
        mode = getattr(scene, "witcher_rig_tab", "FK")
        if mode in {'FK', 'HAND', 'IK'}:
            refresh_rig_bone_list(context, mode)
        self.report({'INFO'}, "Added PoseKey")
        return {'FINISHED'}


class WITCH_OT_RigRemovePoseKey(bpy.types.Operator):
    bl_idname = "witcher.rig_remove_pose_key"
    bl_label = "Remove PoseKey"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        armature = _find_character_armature(context)
        item = _selected_pose_key_item(context)
        if armature is None or item is None:
            self.report({'ERROR'}, "Select a PoseKey")
            return {'CANCELLED'}
        strip_name = str(getattr(item, "name", "") or "")
        if not pose_key_tools.remove_pose_key_strip(context, armature, strip_name):
            self.report({'ERROR'}, "PoseKey not found")
            return {'CANCELLED'}
        refresh_rig_pose_key_list(context)
        _sync_hand_tracks_from_pose_key(context, armature)
        mode = getattr(context.scene, "witcher_rig_tab", "FK")
        if mode in {'FK', 'HAND', 'IK'}:
            refresh_rig_bone_list(context, mode)
        self.report({'INFO'}, "Removed PoseKey")
        return {'FINISHED'}


class WITCH_OT_RigSyncGameIkMarkers(bpy.types.Operator):
    bl_idname = "witcher.rig_sync_game_ik_markers"
    bl_label = "Sync Game IK Markers"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        armature = _find_character_armature(context)
        if armature is None:
            self.report({'ERROR'}, "Select a character armature")
            return {'CANCELLED'}
        count = pose_key_tools.sync_game_ik_marker_pose(armature)
        self.report({'INFO'}, f"Synced {count} IK marker bones")
        return {'FINISHED'}


class WITCH_OT_RigCreatePoseKey(bpy.types.Operator):
    bl_idname = "witcher.rig_create_pose_key"
    bl_label = "Create Pose Key"
    bl_options = {'REGISTER', 'UNDO'}

    mode: EnumProperty(
        items=[
            ('FK', "FK", ""),
            ('HAND', "Hands", ""),
            ('IK', "IK", ""),
        ],
        default='FK',
    )
    source: EnumProperty(
        items=[
            ('ENABLED', "Enabled", ""),
            ('SELECTED', "Selected", ""),
        ],
        default='ENABLED',
    )

    def execute(self, context):
        armature = _find_character_armature(context)
        if armature is None or getattr(armature, "pose", None) is None:
            self.report({'ERROR'}, "Select a character armature")
            return {'CANCELLED'}

        if self.mode == 'IK' and bool(getattr(context.scene, "witcher_rig_sync_ik_on_create", True)):
            pose_key_tools.sync_game_ik_marker_pose(armature)

        entries = (
            _selected_pose_bone_entries(context, self.mode)
            if self.source == 'SELECTED'
            else _enabled_list_bone_entries(context, self.mode)
        )
        if not entries:
            entries = _last_selected_bone_entry(context, self.mode)
        if not entries:
            item = _active_list_bone(context)
            if item is not None:
                entries = [{"name": item.name, "group": item.group or self.mode}]
        if not entries:
            self.report({'ERROR'}, "No bones selected for the PoseKey")
            return {'CANCELLED'}

        scene = context.scene
        try:
            strip, _action, metadata = pose_key_tools.create_pose_key_from_current_pose(
                context,
                armature,
                entries,
                actor=_scene_actor_value(context, armature),
                duration=float(getattr(scene, "witcher_rig_pose_duration", 1.0)),
                blend_in=float(getattr(scene, "witcher_rig_pose_blend_in", 0.0) or 0.0),
                blend_out=float(getattr(scene, "witcher_rig_pose_blend_out", 0.0) or 0.0),
                weight=float(getattr(scene, "witcher_rig_pose_weight", 1.0) or 1.0),
                link_to_dialogset=bool(getattr(scene, "witcher_rig_pose_link_to_dialogset", True)),
                preset_name=str(getattr(scene, "witcher_rig_pose_preset_name", "None") or "None"),
                preset_version=int(getattr(scene, "witcher_rig_pose_preset_version", 0) or 0),
                event_name=str(getattr(scene, "witcher_rig_pose_event_name", "Pose key") or "Pose key"),
                nla_blend_type=str(getattr(scene, "witcher_rig_pose_nla_blend_type", "COMBINE") or "COMBINE"),
            )
        except Exception as exc:
            log.exception("Failed to create PoseKey")
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

        scene.witcher_rig_last_pose_key_strip = getattr(strip, "name", "")
        scene.witcher_rig_last_pose_key_metadata = json.dumps(metadata, sort_keys=True)
        refresh_rig_pose_key_list(context)
        _sync_hand_tracks_from_pose_key(context, armature)
        mode = getattr(scene, "witcher_rig_tab", "FK")
        if mode in {'FK', 'HAND', 'IK'}:
            refresh_rig_bone_list(context, mode)
        self.report({'INFO'}, f"Created PoseKey with {len(entries)} bones")
        return {'FINISHED'}


class WITCH_OT_RigCopyPoseKeyXml(bpy.types.Operator):
    bl_idname = "witcher.rig_copy_pose_key_xml"
    bl_label = "Copy PoseKey XML"
    bl_options = {'REGISTER'}

    def execute(self, context):
        armature = _find_character_armature(context)
        strip = pose_key_tools.find_pose_key_strip(context, armature)
        metadata = {}
        if strip is not None:
            metadata = pose_key_tools.pose_key_metadata_from_target(strip)
            if not metadata and getattr(strip, "action", None) is not None:
                metadata = pose_key_tools.pose_key_metadata_from_target(strip.action)
        if not metadata:
            try:
                metadata = json.loads(str(getattr(context.scene, "witcher_rig_last_pose_key_metadata", "") or "{}"))
            except Exception:
                metadata = {}
        if not metadata:
            self.report({'ERROR'}, "No PoseKey strip found")
            return {'CANCELLED'}
        context.window_manager.clipboard = pose_key_tools.pose_key_xml_from_metadata(metadata)
        self.report({'INFO'}, "PoseKey XML copied")
        return {'FINISHED'}


class WITCH_OT_RigRefreshRedkitPresets(bpy.types.Operator):
    bl_idname = "witcher.rig_refresh_redkit_presets"
    bl_label = "Load Presets"
    bl_options = {'REGISTER'}

    def execute(self, context):
        scene = context.scene
        project_path = _active_redkit_project_path(context)
        preset_path = pose_key_tools.redkit_pose_presets_path(project_path) if project_path else ""
        if preset_path:
            scene.witcher_rig_redkit_preset_path = preset_path
        else:
            preset_path = str(getattr(scene, "witcher_rig_redkit_preset_path", "") or "").strip()

        scene.witcher_rig_preset_list.clear()
        if not preset_path:
            scene.witcher_rig_preset_status = "No REDkit project selected"
            self.report({'ERROR'}, scene.witcher_rig_preset_status)
            return {'CANCELLED'}

        try:
            presets = pose_key_tools.read_redkit_pose_presets(preset_path)
        except FileNotFoundError:
            scene.witcher_rig_preset_status = "No scene.redPresets in project"
            self.report({'WARNING'}, f"Preset file not found: {preset_path}")
            return {'CANCELLED'}
        except Exception as exc:
            log.exception("Failed to load REDkit pose presets")
            scene.witcher_rig_preset_status = "Could not load presets"
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

        for preset in presets:
            item = scene.witcher_rig_preset_list.add()
            item.name = str(preset.get("name", "") or "")
            item.version = int(preset.get("version", 0) or 0)
            item.bone_count = int(preset.get("boneCount", 0) or 0)
            item.hand_count = int(preset.get("handCount", 0) or 0)
            item.ik_count = int(preset.get("ikCount", 0) or 0)
            item.track_count = int(preset.get("trackCount", 0) or 0)
            item.source_path = preset_path
            item.label = f"{item.name}  v{item.version}"
            item.data_json = json.dumps(preset, sort_keys=True)

        scene.witcher_rig_preset_list_index = min(
            int(getattr(scene, "witcher_rig_preset_list_index", 0) or 0),
            max(0, len(scene.witcher_rig_preset_list) - 1),
        )
        scene.witcher_rig_preset_status = f"Loaded {len(presets)} presets"
        self.report({'INFO'}, scene.witcher_rig_preset_status)
        return {'FINISHED'}


class WITCH_OT_RigApplyRedkitPreset(bpy.types.Operator):
    bl_idname = "witcher.rig_apply_redkit_preset"
    bl_label = "Apply Preset"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        armature = _find_character_armature(context)
        if armature is None or getattr(armature, "pose", None) is None:
            self.report({'ERROR'}, "Select a character armature")
            return {'CANCELLED'}

        item = _selected_preset_item(context)
        if item is None:
            self.report({'ERROR'}, "Select a preset")
            return {'CANCELLED'}
        try:
            preset = json.loads(str(getattr(item, "data_json", "") or "{}"))
        except Exception as exc:
            self.report({'ERROR'}, f"Invalid preset data: {exc}")
            return {'CANCELLED'}

        scene = context.scene
        preset_name = str(preset.get("name", item.name) or item.name or "None")
        preset_version = int(preset.get("version", getattr(item, "version", 0)) or 0)
        scene.witcher_rig_pose_preset_name = preset_name
        scene.witcher_rig_pose_preset_version = preset_version
        if not str(getattr(scene, "witcher_rig_pose_event_name", "") or "").strip():
            scene.witcher_rig_pose_event_name = preset_name

        actor = _scene_actor_value(context, armature)
        duration = float(getattr(scene, "witcher_rig_pose_duration", 1.0))
        blend_in = float(getattr(scene, "witcher_rig_pose_blend_in", 0.0) or 0.0)
        blend_out = float(getattr(scene, "witcher_rig_pose_blend_out", 0.0) or 0.0)
        weight = float(getattr(scene, "witcher_rig_pose_weight", 1.0) or 1.0)
        link_to_dialogset = bool(getattr(scene, "witcher_rig_pose_link_to_dialogset", True))
        event_name = str(getattr(scene, "witcher_rig_pose_event_name", "") or preset_name)
        nla_blend_type = str(getattr(scene, "witcher_rig_pose_nla_blend_type", "COMBINE") or "COMBINE")

        try:
            active_strip = pose_key_tools.find_pose_key_strip(context, armature)
            if active_strip is not None:
                strip, _action, metadata = pose_key_tools.apply_pose_key_preset_to_strip(
                    context,
                    armature,
                    active_strip,
                    preset,
                    actor=actor,
                    duration=duration,
                    blend_in=blend_in,
                    blend_out=blend_out,
                    weight=weight,
                    link_to_dialogset=link_to_dialogset,
                    event_name=event_name,
                    nla_blend_type=nla_blend_type,
                )
            else:
                strip, _action, metadata = pose_key_tools.create_pose_key_from_preset(
                    context,
                    armature,
                    preset,
                    actor=actor,
                    duration=duration,
                    blend_in=blend_in,
                    blend_out=blend_out,
                    weight=weight,
                    link_to_dialogset=link_to_dialogset,
                    event_name=event_name,
                    nla_blend_type=nla_blend_type,
                )
        except Exception as exc:
            log.exception("Failed to apply REDkit pose preset")
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

        scene.witcher_rig_last_pose_key_strip = getattr(strip, "name", "")
        scene.witcher_rig_last_pose_key_metadata = json.dumps(metadata, sort_keys=True)
        scene.witcher_rig_pose_status = f"Applied preset {preset_name}"
        refresh_rig_pose_key_list(context)
        _sync_hand_tracks_from_pose_key(context, armature)
        if getattr(scene, "witcher_rig_tab", "FK") in {'FK', 'HAND', 'IK'}:
            refresh_rig_bone_list(context, getattr(scene, "witcher_rig_tab", "FK"))
        _sync_rotation_from_bone(context)
        _set_auto_capture_baseline(context, armature)
        self.report({'INFO'}, f"Applied preset {preset_name}")
        return {'FINISHED'}


def _draw_pose_settings(layout, context, armature):
    scene = context.scene
    box = layout.box()
    row = box.row(align=True)
    row.label(text="PoseKey", icon='ACTION')
    box.prop(scene, "witcher_rig_pose_actor", text="Actor")
    if armature and not str(getattr(scene, "witcher_rig_pose_actor", "") or "").strip():
        row = box.row()
        row.enabled = False
        row.label(text=pose_key_tools.actor_name_for_armature(armature), icon='ARMATURE_DATA')
    row = box.row(align=True)
    row.prop(scene, "witcher_rig_pose_start_frame", text="Start")
    row.prop(scene, "witcher_rig_pose_duration", text="Duration")
    row = box.row(align=True)
    row.prop(scene, "witcher_rig_pose_blend_in", text="Blend In")
    row.prop(scene, "witcher_rig_pose_blend_out", text="Blend Out")
    row = box.row(align=True)
    row.prop(scene, "witcher_rig_pose_weight", text="Weight", slider=True)
    row.prop(scene, "witcher_rig_pose_link_to_dialogset", text="Dialogset")
    row = box.row(align=True)
    row.prop(scene, "witcher_rig_pose_nla_blend_type", text="NLA")


def _draw_pose_key_selector(layout, context, armature):
    scene = context.scene
    box = layout.box()
    box.label(text="PoseKeys", icon='NLA')
    if armature is None:
        row = box.row()
        row.enabled = False
        row.label(text="Select a character armature.", icon='INFO')
        return

    stale = _pose_key_list_is_stale(context, armature)
    _ensure_pose_key_list_current(context, armature)
    row = box.row()
    row.template_list(
        "WITCH_UL_RigPoseKeyList",
        "WITCHER_RIG_POSE_KEYS",
        scene,
        "witcher_rig_pose_key_list",
        scene,
        "witcher_rig_pose_key_list_index",
        rows=3,
    )
    col = row.column(align=True)
    col.operator(WITCH_OT_RigAddPoseKey.bl_idname, text="", icon='ADD')
    col.operator(WITCH_OT_RigRemovePoseKey.bl_idname, text="", icon='REMOVE')
    col.separator()
    col.operator(WITCH_OT_RigRefreshPoseKeys.bl_idname, text="", icon='FILE_REFRESH')
    if stale:
        info = box.row()
        info.enabled = False
        info.label(text="Refreshing PoseKeys.", icon='FILE_REFRESH')


def _draw_bone_list(layout, context, mode):
    scene = context.scene
    box = layout.box()
    row = box.row(align=True)
    row.operator(WITCH_OT_RigRefreshBones.bl_idname, text="Refresh", icon='FILE_REFRESH').mode = mode
    row.prop(scene, "witcher_rig_show_active_only", text="Active")
    row.operator(WITCH_OT_RigToggleBoneSelection.bl_idname, text="", icon='CHECKBOX_HLT').enabled = True
    row.operator(WITCH_OT_RigToggleBoneSelection.bl_idname, text="", icon='CHECKBOX_DEHLT').enabled = False
    if getattr(scene, "witcher_rig_bone_list_mode", "") != mode:
        box.label(text="Refresh this list.", icon='INFO')
    box.template_list(
        "WITCH_UL_RigBoneList",
        f"WITCHER_RIG_{mode}",
        scene,
        "witcher_rig_bone_list",
        scene,
        "witcher_rig_bone_list_index",
        rows=8 if mode == 'FK' else 6,
    )


_HAND_FINGER_ROWS = (
    ("all", "Fingers"),
    ("A", "A Pinky"),
    ("B", "B Ring"),
    ("C", "C Middle"),
    ("D", "D Index"),
    ("E", "E Thumb"),
    ("twist", "Twist"),
)


def _draw_hand_track_row(layout, scene, side, part, label):
    row = layout.row(align=True)
    row.label(text=label)
    op = row.operator(WITCH_OT_RigResetHandTrack.bl_idname, text="", icon='LOOP_BACK')
    op.side = side
    op.part = part
    row.prop(
        scene,
        "witcher_rig_hand_tracks",
        index=pose_key_tools.hand_track_index(side, part),
        text="",
        slider=True,
    )


def _draw_hand_side_tracks(layout, scene, side, title):
    layout.label(text=title, icon='BONE_DATA')
    suffix = "R" if side == 'R' else "L"
    for part, label in _HAND_FINGER_ROWS:
        _draw_hand_track_row(layout, scene, side, part, f"{label} {suffix}")


def _draw_hand_axis_tracks(layout, scene, side, title):
    layout.label(text=title, icon='DRIVER_ROTATIONAL_DIFFERENCE')
    suffix = "R" if side == 'R' else "L"
    for part, axis in (("handX", "X"), ("handY", "Y"), ("handZ", "Z")):
        _draw_hand_track_row(layout, scene, side, part, f"Axis {axis} {suffix}")


def _draw_hands_tab(layout, context, armature):
    scene = context.scene
    strip, action = pose_key_tools.active_pose_key_action(context, armature)
    has_pose_key = strip is not None and action is not None

    box = layout.box()
    row = box.row(align=True)
    row.enabled = has_pose_key
    row.label(text="Hands", icon='BONE_DATA')
    op = row.operator(WITCH_OT_RigResetHandTrack.bl_idname, text="Reset", icon='LOOP_BACK')
    op.all_tracks = True
    if not has_pose_key:
        row = box.row()
        row.enabled = False
        row.label(text="Select or add a PoseKey.", icon='INFO')

    fingers = layout.box()
    fingers.enabled = has_pose_key
    fingers.label(text="Fingers", icon='BONE_DATA')
    _draw_hand_side_tracks(fingers, scene, 'R', "Right Fingers")
    fingers.separator()
    _draw_hand_side_tracks(fingers, scene, 'L', "Left Fingers")

    hands = layout.box()
    hands.enabled = has_pose_key
    hands.label(text="Hand Axes", icon='DRIVER_ROTATIONAL_DIFFERENCE')
    _draw_hand_axis_tracks(hands, scene, 'R', "Right Hand")
    hands.separator()
    _draw_hand_axis_tracks(hands, scene, 'L', "Left Hand")

    status = str(getattr(scene, "witcher_rig_pose_status", "") or "")
    if status:
        row = layout.row()
        row.enabled = False
        row.label(text=status, icon='INFO')


def _draw_pose_bone_editor(layout, context, mode):
    scene = context.scene
    box = layout.box()
    box.label(text="Active PoseKey Bone", icon='DRIVER_ROTATIONAL_DIFFERENCE')
    selected_name = str(_selected_or_active_bone_name(context) or "")
    bone_name = str(getattr(scene, "witcher_rig_active_bone_name", "") or selected_name or "")
    box.prop(scene, "witcher_rig_active_bone_name", text="Bone")
    if not bone_name:
        row = box.row()
        row.enabled = False
        row.label(text="Select a bone.", icon='INFO')
        return
    if mode != 'FK':
        row = box.row()
        row.enabled = False
        row.label(text="FK rotation editor.", icon='INFO')
        return
    row = box.row(align=True)
    auto_enabled = bool(getattr(scene, "witcher_rig_auto_capture_rotation", False))
    row.operator_context = 'INVOKE_DEFAULT'
    row.operator(
        WITCH_OT_RigAutoCaptureModal.bl_idname,
        text="Stop Capture" if auto_enabled else "Viewport Capture",
        icon='PAUSE' if auto_enabled else 'REC',
    )
    if bool(getattr(scene, "witcher_rig_auto_capture_rotation", False)) and bool(getattr(getattr(context, "screen", None), "is_animation_playing", False)):
        row = box.row()
        row.enabled = False
        row.label(text="Paused during playback.", icon='PAUSE')
    row = box.row(align=True)
    row.prop(scene, "witcher_rig_auto_enable_pose_bone", text="Auto Enable")
    row = box.row(align=True)
    row.prop(scene, "witcher_rig_pose_rotation", text="Rotation")
    row.operator(WITCH_OT_RigResetBoneRotation.bl_idname, text="", icon='LOOP_BACK')
    row.operator(WITCH_OT_RigCaptureBoneRotation.bl_idname, text="", icon='IMPORT')
    status = str(getattr(scene, "witcher_rig_pose_status", "") or "")
    if status:
        row = box.row()
        row.enabled = False
        row.label(text=status, icon='INFO')


def _draw_create_buttons(layout, mode):
    row = layout.row(align=True)
    op = row.operator(WITCH_OT_RigCreatePoseKey.bl_idname, text="Enabled", icon='KEY_HLT')
    op.mode = mode
    op.source = 'ENABLED'
    op = row.operator(WITCH_OT_RigCreatePoseKey.bl_idname, text="Selected", icon='RESTRICT_SELECT_OFF')
    op.mode = mode
    op.source = 'SELECTED'


def draw_rig_tab(layout, context, display_armature=None):
    scene = context.scene
    armature = display_armature or _find_character_armature(context)

    _draw_pose_key_selector(layout, context, armature)

    sub = layout.row(align=True)
    sub.scale_y = 1.25
    sub.prop_enum(scene, "witcher_rig_tab", 'FK')
    sub.prop_enum(scene, "witcher_rig_tab", 'HAND')
    sub.prop_enum(scene, "witcher_rig_tab", 'IK')
    sub.prop_enum(scene, "witcher_rig_tab", 'PRESETS')

    mode = getattr(scene, "witcher_rig_tab", "FK")
    if armature is None and mode != 'PRESETS':
        layout.label(text="Select a character armature.", icon='INFO')
        return

    if mode in {'FK', 'HAND', 'IK'}:
        _draw_pose_settings(layout, context, armature)
        if mode == 'HAND':
            _draw_hands_tab(layout, context, armature)
        elif mode == 'IK':
            ik_box = layout.box()
            row = ik_box.row(align=True)
            row.operator(WITCH_OT_RigSyncGameIkMarkers.bl_idname, text="Sync Markers", icon='CONSTRAINT')
            row.prop(scene, "witcher_rig_sync_ik_on_create", text="Auto")
            _draw_bone_list(layout, context, mode)
            _draw_create_buttons(layout, mode)
        else:
            _draw_bone_list(layout, context, mode)
            _draw_pose_bone_editor(layout, context, mode)
            _draw_create_buttons(layout, mode)
    else:
        if armature is not None:
            _draw_pose_settings(layout, context, armature)
        else:
            layout.label(text="Select a character armature to apply.", icon='INFO')

        _sync_project_selector_from_preferences(scene, context)
        source_box = layout.box()
        source_box.label(text="REDkit Project Presets", icon='FILE_FOLDER')
        source_box.prop(scene, "witcher_rig_redkit_project", text="Project")
        source_box.prop(scene, "witcher_rig_redkit_preset_path", text="Preset File")
        source_box.operator(WITCH_OT_RigRefreshRedkitPresets.bl_idname, text="Load Presets", icon='FILE_REFRESH')
        status = str(getattr(scene, "witcher_rig_preset_status", "") or "")
        if status:
            row = source_box.row()
            row.enabled = False
            row.label(text=status, icon='INFO')

        preset_box = layout.box()
        preset_box.label(text="Presets", icon='PRESET')
        preset_box.template_list(
            "WITCH_UL_RigPresetList",
            "WITCHER_RIG_PRESETS",
            scene,
            "witcher_rig_preset_list",
            scene,
            "witcher_rig_preset_list_index",
            rows=6,
        )
        item = _selected_preset_item(context)
        if item is not None:
            row = preset_box.row()
            row.enabled = False
            row.label(
                text=f"Body {item.bone_count}, Hands {item.hand_count}, IK {item.ik_count}",
                icon='BONE_DATA',
            )
        preset_box.operator(WITCH_OT_RigApplyRedkitPreset.bl_idname, text="Apply Preset", icon='KEY_HLT')

        box = layout.box()
        box.label(text="Preset Metadata", icon='PRESET')
        box.prop(scene, "witcher_rig_pose_event_name", text="Event")
        box.prop(scene, "witcher_rig_pose_preset_name", text="Preset")
        box.prop(scene, "witcher_rig_pose_preset_version", text="Version")
        box.operator(WITCH_OT_RigCopyPoseKeyXml.bl_idname, text="Copy PoseKey XML", icon='COPYDOWN')
        last = str(getattr(scene, "witcher_rig_last_pose_key_strip", "") or "")
        if last:
            info = box.row()
            info.enabled = False
            info.label(text=last, icon='NLA')


classes = [
    WITCH_PG_RigBoneItem,
    WITCH_PG_RigPresetItem,
    WITCH_PG_RigPoseKeyItem,
    WITCH_UL_RigBoneList,
    WITCH_UL_RigPresetList,
    WITCH_UL_RigPoseKeyList,
    WITCH_OT_RigRefreshBones,
    WITCH_OT_RigSelectBone,
    WITCH_OT_RigToggleBoneSelection,
    WITCH_OT_RigResetHandTrack,
    WITCH_OT_RigResetBoneRotation,
    WITCH_OT_RigCaptureBoneRotation,
    WITCH_OT_RigAutoCaptureModal,
    WITCH_OT_RigRefreshPoseKeys,
    WITCH_OT_RigAddPoseKey,
    WITCH_OT_RigRemovePoseKey,
    WITCH_OT_RigSyncGameIkMarkers,
    WITCH_OT_RigCreatePoseKey,
    WITCH_OT_RigCopyPoseKeyXml,
    WITCH_OT_RigRefreshRedkitPresets,
    WITCH_OT_RigApplyRedkitPreset,
]


def register_props():
    bpy.types.Scene.witcher_rig_tab = EnumProperty(
        name="Rig Tab",
        description="Active rig control section",
        items=[
            ('FK', "FK", "Pose individual animation bones"),
            ('HAND', "Hands", "Pose hand and finger bones"),
            ('IK', "IK", "Sync and pose game IK marker bones"),
            ('PRESETS', "Presets", "PoseKey preset metadata and clipboard"),
        ],
        default='FK',
        update=_on_rig_tab_update,
    )
    bpy.types.Scene.witcher_rig_bone_list = bpy.props.CollectionProperty(type=WITCH_PG_RigBoneItem)
    bpy.types.Scene.witcher_rig_bone_list_index = IntProperty(default=0, update=_on_rig_bone_list_index_update)
    bpy.types.Scene.witcher_rig_bone_list_mode = StringProperty(default="")
    bpy.types.Scene.witcher_rig_pose_key_list = CollectionProperty(type=WITCH_PG_RigPoseKeyItem)
    bpy.types.Scene.witcher_rig_pose_key_list_index = IntProperty(default=0, update=_on_rig_pose_key_list_index_update)
    bpy.types.Scene.witcher_rig_pose_key_list_signature = StringProperty(default="", options={'SKIP_SAVE'})
    bpy.types.Scene.witcher_rig_show_active_only = BoolProperty(
        name="Active Only",
        description="Show only bones present in the active PoseKey",
        default=False,
        update=_on_rig_active_only_update,
    )
    bpy.types.Scene.witcher_rig_auto_capture_rotation = BoolProperty(
        name="Viewport Capture",
        description="Capture the selected pose bone rotation after a Pose Mode edit goes idle",
        default=False,
        options={'SKIP_SAVE'},
    )
    bpy.types.Scene.witcher_rig_auto_enable_pose_bone = BoolProperty(
        name="Auto Enable",
        description="Enable an inactive zero-rotation FK bone when its PoseKey rotation is changed",
        default=True,
    )
    bpy.types.Scene.witcher_rig_active_bone_name = StringProperty(name="Bone", default="", options={'SKIP_SAVE'})
    bpy.types.Scene.witcher_rig_pose_rotation = FloatVectorProperty(
        name="Rotation",
        size=3,
        subtype='EULER',
        unit='ROTATION',
        default=(0.0, 0.0, 0.0),
        soft_min=-6.283185307179586,
        soft_max=6.283185307179586,
        update=_on_rig_pose_rotation_update,
    )
    bpy.types.Scene.witcher_rig_hand_tracks = FloatVectorProperty(
        name="Hands",
        description="Hand and finger tracks from -1 to 1",
        size=pose_key_tools.HAND_TRACK_COUNT,
        default=(0.0,) * pose_key_tools.HAND_TRACK_COUNT,
        min=-1.0,
        max=1.0,
        soft_min=-1.0,
        soft_max=1.0,
        update=_on_rig_hand_tracks_update,
    )
    bpy.types.Scene.witcher_rig_last_selected_bone = StringProperty(default="", options={'SKIP_SAVE'})
    bpy.types.Scene.witcher_rig_last_pose_mode_bone = StringProperty(default="", options={'SKIP_SAVE'})
    bpy.types.Scene.witcher_rig_last_pose_capture_signature = StringProperty(default="", options={'SKIP_SAVE'})
    bpy.types.Scene.witcher_rig_preset_list = CollectionProperty(type=WITCH_PG_RigPresetItem)
    bpy.types.Scene.witcher_rig_preset_list_index = IntProperty(default=0)
    bpy.types.Scene.witcher_rig_preset_status = StringProperty(name="Preset Status", default="", options={'SKIP_SAVE'})
    bpy.types.Scene.witcher_rig_redkit_project = EnumProperty(
        name="REDkit Project",
        description="Active REDkit project used for Control Rig pose presets",
        items=_redkit_project_enum_items,
        update=_on_redkit_project_update,
    )
    bpy.types.Scene.witcher_rig_redkit_preset_path = StringProperty(
        name="Preset File",
        default="",
        subtype='FILE_PATH',
        description="REDkit scene.redPresets file used by the Control Rig presets tab",
    )
    bpy.types.Scene.witcher_rig_pose_actor = StringProperty(name="Actor", default="", update=_on_rig_pose_settings_update)
    bpy.types.Scene.witcher_rig_pose_event_name = StringProperty(name="Event", default="Pose key", update=_on_rig_pose_settings_update)
    bpy.types.Scene.witcher_rig_pose_start_frame = bpy.props.FloatProperty(name="Start", default=0.0, update=_on_rig_pose_settings_update)
    bpy.types.Scene.witcher_rig_pose_duration = bpy.props.FloatProperty(name="Duration", default=1.0, min=0.0, subtype='TIME', update=_on_rig_pose_settings_update)
    bpy.types.Scene.witcher_rig_pose_blend_in = bpy.props.FloatProperty(name="Blend In", default=0.0, min=0.0, subtype='TIME', update=_on_rig_pose_settings_update)
    bpy.types.Scene.witcher_rig_pose_blend_out = bpy.props.FloatProperty(name="Blend Out", default=0.0, min=0.0, subtype='TIME', update=_on_rig_pose_settings_update)
    bpy.types.Scene.witcher_rig_pose_weight = bpy.props.FloatProperty(name="Weight", default=1.0, min=0.0, max=1.0, update=_on_rig_pose_settings_update)
    bpy.types.Scene.witcher_rig_pose_link_to_dialogset = BoolProperty(name="Link To Dialogset", default=True, update=_on_rig_pose_settings_update)
    bpy.types.Scene.witcher_rig_pose_preset_name = StringProperty(name="Preset", default="None", update=_on_rig_pose_settings_update)
    bpy.types.Scene.witcher_rig_pose_preset_version = IntProperty(name="Preset Version", default=0, min=0, update=_on_rig_pose_settings_update)
    bpy.types.Scene.witcher_rig_pose_nla_blend_type = EnumProperty(
        name="NLA Blend",
        items=[
            ('COMBINE', "Combine", "Layer this PoseKey over existing animation"),
            ('REPLACE', "Replace", "Replace lower NLA strips while active"),
            ('ADD', "Add", "Additive NLA blend"),
            ('SUBTRACT', "Subtract", "Subtract NLA blend"),
            ('MULTIPLY', "Multiply", "Multiply NLA blend"),
        ],
        default='COMBINE',
        update=_on_rig_pose_settings_update,
    )
    bpy.types.Scene.witcher_rig_sync_ik_on_create = BoolProperty(name="Sync IK On Create", default=True)
    bpy.types.Scene.witcher_rig_last_pose_key_strip = StringProperty(default="", options={'SKIP_SAVE'})
    bpy.types.Scene.witcher_rig_last_pose_key_metadata = StringProperty(default="", options={'SKIP_SAVE'})
    bpy.types.Scene.witcher_rig_pose_status = StringProperty(name="Pose Status", default="", options={'SKIP_SAVE'})
    start_rig_pose_sync_timer()


def unregister_props():
    stop_rig_pose_sync_timer()
    for prop_name in (
        "witcher_rig_tab",
        "witcher_rig_bone_list",
        "witcher_rig_bone_list_index",
        "witcher_rig_bone_list_mode",
        "witcher_rig_pose_key_list",
        "witcher_rig_pose_key_list_index",
        "witcher_rig_pose_key_list_signature",
        "witcher_rig_show_active_only",
        "witcher_rig_auto_capture_rotation",
        "witcher_rig_auto_enable_pose_bone",
        "witcher_rig_active_bone_name",
        "witcher_rig_pose_rotation",
        "witcher_rig_hand_tracks",
        "witcher_rig_last_selected_bone",
        "witcher_rig_last_pose_mode_bone",
        "witcher_rig_last_pose_capture_signature",
        "witcher_rig_preset_list",
        "witcher_rig_preset_list_index",
        "witcher_rig_preset_status",
        "witcher_rig_redkit_project",
        "witcher_rig_redkit_preset_path",
        "witcher_rig_pose_actor",
        "witcher_rig_pose_event_name",
        "witcher_rig_pose_start_frame",
        "witcher_rig_pose_duration",
        "witcher_rig_pose_blend_in",
        "witcher_rig_pose_blend_out",
        "witcher_rig_pose_weight",
        "witcher_rig_pose_link_to_dialogset",
        "witcher_rig_pose_preset_name",
        "witcher_rig_pose_preset_version",
        "witcher_rig_pose_nla_blend_type",
        "witcher_rig_sync_ik_on_create",
        "witcher_rig_last_pose_key_strip",
        "witcher_rig_last_pose_key_metadata",
        "witcher_rig_pose_status",
    ):
        if hasattr(bpy.types.Scene, prop_name):
            delattr(bpy.types.Scene, prop_name)
