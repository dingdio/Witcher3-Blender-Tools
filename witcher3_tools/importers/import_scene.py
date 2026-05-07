import logging
log = logging.getLogger(__name__)
import os
import json
from pathlib import Path
from .. import get_uncook_path
from ..CR2W import read_json_w3
from ..CR2W import w3_types
from ..CR2W.CR2W_types import EngineTransform
from ..CR2W.common_blender import repo_file
from ..CR2W.dc_scene import load_bin_scene
from ..importers import import_entity
from ..importers.import_helpers import set_blender_object_transform#, set_blender_pose_bone_transform
from ..action_compat import assign_action, bind_strip_action_slot, new_action_fcurve, resolve_action_slot
from .import_cutscene import (
    check_if_actor_already_in_scene,
    _ensure_cutscene_actor_appearance,
    _ensure_cutscene_face_setup,
)
from ..camera_tracks import (
    CAMERA_CONTROL_BONE,
    CAMERA_EDIT_BONE,
    CAMERA_TRACK_DEFAULTS,
    CAMERA_TRACK_NAMES,
    ensure_camera_track_properties,
    find_camera_preview_object,
    setup_camera_preview_drivers,
)
from mathutils import Euler
from math import radians
import math
from mathutils import Matrix, Vector
from ..ui.ui_voice import _find_face_meshes, _get_sequence_editor_strips, load_voice_and_lipsync
from ..ui.ui_anims_list import SetupActor, GetAnimationInfoByName, load_anim_into_scene

def check_if_camera_already_in_scene(name):
    for o in bpy.context.scene.objects:
        if o.type != 'CAMERA':
            continue
        if len(o.name) > 4 and o.name[-4] != "." and o.name == name:
            return o
    return False


def loadSceneFile(fileName):
    dirpath, file = os.path.split(fileName)
    basename, ext = os.path.splitext(file)
    if fileName.endswith('.w2scene'):
        w3Data = load_bin_scene(fileName)
        return w3Data
    else:
        pass

import bpy
from .import_anims import NewW2ANIMSListItem

W2SCENE_AUDIO_STRIP_PROP = "witcher_w2scene_section_audio"
W2SCENE_AUDIO_SOURCE_PROP = "witcher_w2scene_source"
W2SCENE_AUDIO_SECTION_PROP = "witcher_w2scene_section"
W2SCENE_SECTION_NLA_TRACK_NAMES = {
    "SceneDialogsetIdle",
    "ScenePlacement",
    "voice_import",
    "voice_import_phoneme",
}
W2SCENE_SECTION_NLA_TRACK_PREFIXES = (
    "cutscene_import_body",
    "cutscene_import_pose",
    "cutscene_import_mimic",
)
W2SCENE_CAMERA_ENTITY_PATH = "gameplay\\camera\\scene_camera.w2ent"
W2SCENE_CAMERA_NLA_TRACK_NAMES = {
    "CameraInterpolation",
    "CameraShots",
    "CustomCamera",
    "CustomCameraInstance",
    "PAUSE",
    "dialogLine",
}


def _strip_get(strip, prop_name, default=None):
    try:
        return strip.get(prop_name, default)
    except Exception:
        return default


def _looks_like_voice_line_strip(strip):
    name = str(getattr(strip, "name", "") or "").split(".", 1)[0]
    return len(name) == 10 and name.isdigit()


def clear_w2scene_section_audio(scene):
    strips = _get_sequence_editor_strips(getattr(scene, "sequence_editor", None))
    if strips is None:
        return 0
    removed = 0
    for strip in list(strips):
        if getattr(strip, "type", None) != 'SOUND':
            continue
        if not bool(_strip_get(strip, W2SCENE_AUDIO_STRIP_PROP, False)) and not _looks_like_voice_line_strip(strip):
            continue
        try:
            strips.remove(strip)
            removed += 1
        except Exception:
            log.debug("Could not remove .w2scene audio strip %s", getattr(strip, "name", ""), exc_info=True)
    return removed


def _nla_track_name_matches(track_name, track_names=(), track_prefixes=()):
    current_name = str(track_name or "")
    if any(current_name == name or current_name.startswith(f"{name}.") for name in track_names):
        return True
    return any(current_name.startswith(prefix) for prefix in track_prefixes)


def _remove_orphan_action(action):
    if action is not None and getattr(action, "users", 0) == 0:
        try:
            bpy.data.actions.remove(action)
        except Exception:
            pass


def clear_nla_tracks(target_obj, track_names=(), track_prefixes=()):
    anim_data = getattr(target_obj, "animation_data", None) if target_obj is not None else None
    if anim_data is None:
        return 0
    removed = 0
    for track in list(anim_data.nla_tracks):
        if not _nla_track_name_matches(getattr(track, "name", ""), track_names=track_names, track_prefixes=track_prefixes):
            continue
        actions = []
        for strip in list(track.strips):
            action = getattr(strip, "action", None)
            if action is not None:
                actions.append(action)
            try:
                track.strips.remove(strip)
                removed += 1
            except Exception:
                log.debug("Could not remove strip %s from %s", getattr(strip, "name", ""), getattr(track, "name", ""), exc_info=True)
        try:
            anim_data.nla_tracks.remove(track)
        except Exception:
            pass
        for action in actions:
            _remove_orphan_action(action)
    return removed


def clear_nla_track(target_obj, track_name):
    return clear_nla_tracks(target_obj, track_names=(track_name,))


def clear_w2scene_actor_section_nla(context, actor_obj):
    removed = clear_nla_tracks(
        actor_obj,
        track_names=W2SCENE_SECTION_NLA_TRACK_NAMES,
        track_prefixes=W2SCENE_SECTION_NLA_TRACK_PREFIXES,
    )
    mimic_name = str(actor_obj.get("mimicFace", "") or "") if actor_obj is not None else ""
    mimic_armature = bpy.data.objects.get(mimic_name) if mimic_name else None
    if mimic_armature is not None and mimic_armature is not actor_obj:
        removed += clear_nla_tracks(
            mimic_armature,
            track_names=W2SCENE_SECTION_NLA_TRACK_NAMES,
            track_prefixes=W2SCENE_SECTION_NLA_TRACK_PREFIXES,
        )
    try:
        face_meshes = _find_face_meshes(context, actor_obj)
    except Exception:
        face_meshes = []
    for mesh_obj in face_meshes:
        shape_keys = getattr(getattr(mesh_obj, "data", None), "shape_keys", None)
        removed += clear_nla_tracks(
            shape_keys,
            track_names=W2SCENE_SECTION_NLA_TRACK_NAMES,
            track_prefixes=W2SCENE_SECTION_NLA_TRACK_PREFIXES,
        )
    return removed


def clear_w2scene_story_scene_actor_nla(context, story_scene, reset_actors=False):
    if story_scene is None:
        return 0
    removed = 0
    for actor_ref in getattr(getattr(story_scene, "sceneTemplates", None), "value", []) or []:
        try:
            actor_template = story_scene.chunksRef[actor_ref - 1]
            actor = w3_types.CStorySceneActor(actor_template)
            actor_obj = check_if_actor_already_in_scene(actor.entityTemplate)
        except Exception:
            actor_obj = None
        if actor_obj:
            removed += clear_w2scene_actor_section_nla(context, actor_obj)
            if reset_actors:
                try:
                    reset_transforms(actor_obj)
                except Exception:
                    log.debug("Could not reset .w2scene actor %s", getattr(actor_obj, "name", ""), exc_info=True)
    return removed


def clear_w2scene_camera_runtime(context, scene_cam_obj=None, clear_markers=True):
    scene = getattr(context, "scene", None)
    removed = 0
    if scene_cam_obj is None:
        scene_cam_obj = check_if_actor_already_in_scene(W2SCENE_CAMERA_ENTITY_PATH)
    if scene_cam_obj:
        removed += clear_nla_tracks(scene_cam_obj, track_names=W2SCENE_CAMERA_NLA_TRACK_NAMES)
    if clear_markers and scene is not None:
        for marker in list(scene.timeline_markers):
            try:
                scene.timeline_markers.remove(marker)
                removed += 1
            except Exception:
                pass
    return removed


def clear_w2scene_runtime_state(context, story_scene=None, reset_actors=False):
    scene = getattr(context, "scene", None)
    removed = {
        "audio": 0,
        "actor_nla": 0,
        "camera": 0,
    }
    if scene is not None:
        removed["audio"] = clear_w2scene_section_audio(scene)
    removed["actor_nla"] = clear_w2scene_story_scene_actor_nla(context, story_scene, reset_actors=reset_actors)
    removed["camera"] = clear_w2scene_camera_runtime(context)
    return removed


def reset_transforms(new_obj):
    x, y, z = (radians(0), radians(0), radians(0))
    mat = Euler((x, y, z)).to_matrix().to_4x4()
    new_obj.matrix_world = mat
    new_obj.matrix_local = mat
    new_obj.matrix_basis = mat

    new_obj.location[0] = 0
    new_obj.location[1] = 0
    new_obj.location[2] = 0
    new_obj.scale[0] = 1
    new_obj.scale[1] = 1
    new_obj.scale[2] = 1

class BlankEngineTransform:
    def __init__(self):
        self.X = 0.0
        self.Y = 0.0
        self.Z = 0.0
        self.Pitch = 0.0
        self.Yaw = 0.0
        self.Roll = 0.0
        self.Scale_x = 1.0
        self.Scale_y = 1.0
        self.Scale_z = 1.0
import time

def _cname_index_to_string(index_obj):
    if index_obj is None:
        return ""
    value = getattr(index_obj, "String", None)
    if value:
        return str(value)
    if hasattr(index_obj, "ToString"):
        try:
            return str(index_obj.ToString() or "")
        except Exception:
            return ""
    return ""


def _cname_array_values(prop):
    if prop is None:
        return []
    if isinstance(prop, (list, tuple)):
        return [str(value or "").strip() for value in prop if str(value or "").strip()]
    values = []
    for index_obj in getattr(prop, "Index", []) or []:
        value = _cname_index_to_string(index_obj).strip()
        if value:
            values.append(value)
    return values


def _scene_actor_preferred_appearance(actor):
    values = _cname_array_values(getattr(actor, "appearanceFilter", None))
    return values[0] if values else ""


def _transform_real(transform, name, default=0.0):
    if transform is None:
        return default
    value = transform.get(name, default) if isinstance(transform, dict) else getattr(transform, name, default)
    try:
        return float(value)
    except Exception:
        return default


def _engine_transform_matrix(engine_transform, from_this_object=None):
    base_matrix = from_this_object.matrix_world.copy() if from_this_object else Matrix.Identity(4)
    yaw = _transform_real(engine_transform, "Yaw", 0.0)
    pitch = _transform_real(engine_transform, "Pitch", 0.0)
    roll = _transform_real(engine_transform, "Roll", 0.0)
    loc_x = _transform_real(engine_transform, "X", 0.0)
    loc_y = _transform_real(engine_transform, "Y", 0.0)
    loc_z = _transform_real(engine_transform, "Z", 0.0)
    if yaw == 0.0 and pitch == 0.0 and roll == 0.0:
        local_matrix = Matrix.Identity(4)
    else:
        local_matrix = Euler((radians(yaw), radians(pitch), radians(roll)), 'YXZ').to_matrix().to_4x4()
    local_matrix.translation = (loc_x, loc_y, loc_z)
    return base_matrix @ local_matrix


def _pose_bone_world_matrix(armature_obj, bone_name):
    pose = getattr(armature_obj, "pose", None) if armature_obj is not None else None
    pose_bones = getattr(pose, "bones", None) if pose is not None else None
    pose_bone = pose_bones.get(bone_name) if pose_bones is not None else None
    if pose_bone is None:
        return None
    return armature_obj.matrix_world @ pose_bone.matrix


def _camera_preview_offset_matrix(camera_armature):
    edit_world = _pose_bone_world_matrix(camera_armature, CAMERA_EDIT_BONE)
    preview_camera = find_camera_preview_object(camera_armature)
    if edit_world is None or preview_camera is None:
        return Matrix.Identity(4)
    return edit_world.inverted() @ preview_camera.matrix_world


def _camera_matrix_to_edit_bone_matrix(camera_armature, camera_matrix, preview_offset):
    desired_edit_world = camera_matrix @ preview_offset.inverted()
    return camera_armature.matrix_world.inverted() @ desired_edit_world


_W2SCENE_CAMERA_TRACK_DEFAULTS = {
    "hctFOV": 50.0,
    "overrideFactor": 1.0,
    "dofFocusDistFar": 0.0,
    "dofBlurDistFar": 0.0,
    "dofIntensity": 0.0,
    "dofFocusDistNear": 0.0,
    "dofBlurDistNear": 0.0,
    "blenderDofFocusDistance": 0.0,
    "blenderDofFocusDistanceWeight": 0.0,
}


_W2SCENE_CAMERA_TRACK_ATTRS = {
    "hctFOV": "cameraFov",
    "dofFocusDistFar": "dofFocusDistFar",
    "dofBlurDistFar": "dofBlurDistFar",
    "dofIntensity": "dofIntensity",
    "dofFocusDistNear": "dofFocusDistNear",
    "dofBlurDistNear": "dofBlurDistNear",
}


_BLENDER_DOF_FOCUS_DISTANCE_TRACK = "blenderDofFocusDistance"
_BLENDER_DOF_FOCUS_DISTANCE_WEIGHT_TRACK = "blenderDofFocusDistanceWeight"
_W2SCENE_DOF_FOCUS_BONE_NAMES = ("head", "Head", "neck", "Neck")


_APERTURE_DOF_DEFAULT_APERTURE = 5
_APERTURE_DOF_CIRCLE_OF_CONFUSION = 0.00003


def _prop_children(prop):
    if prop is None:
        return []
    for attr in ("PROPS", "MoreProps", "More"):
        values = getattr(prop, attr, None)
        if values is not None:
            return list(values)
    return []


def _prop_child_value(prop, name, default=None):
    for child in _prop_children(prop):
        if getattr(child, "theName", None) != name:
            continue
        if hasattr(child, "Value"):
            return child.Value
        if hasattr(child, "value"):
            return child.value
        index_obj = getattr(child, "Index", None)
        if index_obj is not None:
            return getattr(index_obj, "String", None) or getattr(index_obj, "Path", None) or default
        return default
    return default


def _prop_child_value_any(prop, names, default=None):
    for name in names:
        value = _prop_child_value(prop, name, None)
        if value is not None:
            return value
    return default


def _prop_child_bool(prop, name, default=False):
    value = _prop_child_value(prop, name, default)
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes"}
    return bool(value)


def _aperture_enum_index(value):
    if value is None:
        return _APERTURE_DOF_DEFAULT_APERTURE
    if isinstance(value, str):
        aperture_names = {
            "APERTURE_1_0": 0,
            "APERTURE_1_4": 1,
            "APERTURE_2_0": 2,
            "APERTURE_2_8": 3,
            "APERTURE_4_0": 4,
            "APERTURE_5_6": 5,
            "APERTURE_8_0": 6,
            "APERTURE_11_0": 7,
            "APERTURE_16_0": 8,
            "APERTURE_22_0": 9,
            "APERTURE_32_0": 10,
        }
        if value in aperture_names:
            return aperture_names[value]
    return _as_int(value, _APERTURE_DOF_DEFAULT_APERTURE)


def _aperture_dof_to_engine_planes(dof_prop):
    if not (
        _prop_child_bool(dof_prop, "enabled", False)
        or _prop_child_bool(dof_prop, "m_enabled", False)
    ):
        return None
    focal_length = _as_float(
        _prop_child_value_any(dof_prop, ("focalLength", "m_focalLength"), 0.0),
        0.0,
    )
    focus_distance = _as_float(
        _prop_child_value_any(dof_prop, ("distance", "m_distance"), 0.0),
        0.0,
    )
    if focal_length <= 0.0 or focus_distance <= 0.0:
        return None

    focal_meters = focal_length * 0.001
    aperture = math.pow(
        1.4142,
        _aperture_enum_index(_prop_child_value_any(dof_prop, ("aperture", "m_aperture"), None)),
    )
    hyperfocal_distance = (focal_meters * focal_meters) / (aperture * _APERTURE_DOF_CIRCLE_OF_CONFUSION)
    focus_near = (hyperfocal_distance * focus_distance) / (hyperfocal_distance + focus_distance)
    if focus_distance < hyperfocal_distance:
        focus_far = (hyperfocal_distance * focus_distance) / (hyperfocal_distance - focus_distance)
    else:
        focus_far = 1000.0 * focal_meters

    return {
        "dofFocusDistNear": focus_near,
        "dofFocusDistFar": focus_far,
        "dofBlurDistNear": 0.0,
        "dofBlurDistFar": focus_far + focus_near,
        "dofIntensity": 1.0,
    }


def _story_scene_camera_track_values(camera_definition):
    values = {}
    for track_name in CAMERA_TRACK_NAMES:
        default = _W2SCENE_CAMERA_TRACK_DEFAULTS.get(track_name, CAMERA_TRACK_DEFAULTS.get(track_name, 0.0))
        attr_name = _W2SCENE_CAMERA_TRACK_ATTRS.get(track_name)
        if attr_name:
            values[track_name] = _as_float(getattr(camera_definition, attr_name, None), default)
        else:
            values[track_name] = default
    aperture_planes = _aperture_dof_to_engine_planes(getattr(camera_definition, "dof", None))
    if aperture_planes is not None:
        values.update(aperture_planes)
    return values


def _camera_actor_focus_distance(camera_matrix, focus_targets):
    if camera_matrix is None or not focus_targets:
        return None
    camera_loc = camera_matrix.translation
    all_candidates = []
    front_candidates = []
    try:
        camera_forward = camera_matrix.to_quaternion() @ Vector((0.0, 0.0, -1.0))
    except Exception:
        camera_forward = None

    for target_obj in focus_targets:
        if target_obj is None:
            continue
        try:
            target_loc = _focus_target_location(target_obj)
            if target_loc is None:
                continue
            direction = target_loc - camera_loc
            distance = float(direction.length)
        except Exception:
            continue
        if distance <= 0.001:
            continue
        all_candidates.append((distance, 0.0))
        if camera_forward is not None:
            try:
                dot = float(direction.normalized().dot(camera_forward))
                projected_distance = float(direction.dot(camera_forward))
            except Exception:
                dot = 0.0
                projected_distance = 0.0
            if dot > 0.0 and projected_distance > 0.001:
                front_candidates.append((projected_distance, dot, distance))

    if front_candidates:
        front_candidates.sort(key=lambda item: (-item[1], item[0]))
        return front_candidates[0][0]
    if all_candidates:
        return min(distance for distance, _dot in all_candidates)
    return None


def _focus_target_location(target_obj):
    candidate_objects = []
    try:
        mimic_name = str(target_obj.get("mimicFace", "") or "")
    except Exception:
        mimic_name = ""
    mimic_obj = bpy.data.objects.get(mimic_name) if mimic_name else None
    if mimic_obj is not None and mimic_obj is not target_obj:
        candidate_objects.append(mimic_obj)
    candidate_objects.append(target_obj)

    for candidate_obj in candidate_objects:
        pose_bones = getattr(getattr(candidate_obj, "pose", None), "bones", None)
        if pose_bones is None:
            continue
        focus_bone = None
        for bone_name in _W2SCENE_DOF_FOCUS_BONE_NAMES:
            focus_bone = pose_bones.get(bone_name)
            if focus_bone is not None:
                break
        if focus_bone is None:
            for pose_bone in pose_bones:
                if str(getattr(pose_bone, "name", "") or "").lower().endswith("head"):
                    focus_bone = pose_bone
                    break
        if focus_bone is not None:
            try:
                return (candidate_obj.matrix_world @ focus_bone.matrix).translation
            except Exception:
                pass

    bound_box = getattr(target_obj, "bound_box", None)
    if bound_box:
        try:
            points = [target_obj.matrix_world @ Vector(corner) for corner in bound_box]
            if points:
                center = Vector((0.0, 0.0, 0.0))
                for point in points:
                    center += point
                return center / len(points)
        except Exception:
            pass

    try:
        return target_obj.matrix_world.translation
    except Exception:
        return None


def _camera_tracks_with_blender_focus_distance(camera_tracks, camera_matrix, focus_targets):
    tracks = dict(camera_tracks or {})
    focus_distance = _camera_actor_focus_distance(camera_matrix, focus_targets)
    if focus_distance is not None:
        tracks[_BLENDER_DOF_FOCUS_DISTANCE_TRACK] = focus_distance
        tracks[_BLENDER_DOF_FOCUS_DISTANCE_WEIGHT_TRACK] = 1.0
    return tracks


def _existing_w2scene_actor_focus_targets(story_scene):
    focus_targets = []
    for actor_ref in getattr(getattr(story_scene, "sceneTemplates", None), "value", []) or []:
        try:
            actor_template = story_scene.chunksRef[actor_ref - 1]
            actor = w3_types.CStorySceneActor(actor_template)
            actor_obj = check_if_actor_already_in_scene(actor.entityTemplate)
        except Exception:
            actor_obj = None
        if actor_obj is not None:
            focus_targets.append(actor_obj)
    return focus_targets


def _custom_camera_event_transform(event):
    translation = getattr(event, "cameraTranslation", None)
    rotation = getattr(event, "cameraRotation", None)
    if translation is None or rotation is None:
        return None
    transform = BlankEngineTransform()
    transform.X = _transform_real(translation, "X", 0.0)
    transform.Y = _transform_real(translation, "Y", 0.0)
    transform.Z = _transform_real(translation, "Z", 0.0)
    transform.Pitch = _transform_real(rotation, "Pitch", 0.0)
    transform.Yaw = _transform_real(rotation, "Yaw", 0.0)
    transform.Roll = _transform_real(rotation, "Roll", 0.0)
    return transform


def _prepare_w2scene_camera_rig(context, camera_armature, repo_path):
    if camera_armature is None or getattr(camera_armature, "type", None) != 'ARMATURE':
        return None, None
    camera_armature["cutscene_actor_name"] = "Camera"
    camera_armature["cutscene_actor_template"] = str(repo_path or "")
    camera_armature["cutscene_actor_type"] = "CAT_Camera"
    camera_armature["witcher_w2scene_camera"] = True
    camera_bone = ensure_camera_track_properties(camera_armature, track_names=CAMERA_TRACK_NAMES)
    preview_camera = find_camera_preview_object(camera_armature)
    if preview_camera is not None:
        setup_camera_preview_drivers(camera_armature, preview_camera)
        if getattr(context, "scene", None) is not None:
            context.scene.camera = preview_camera
    return camera_bone, preview_camera


def _w2scene_guid_string(guid):
    guid = getattr(guid, "GUID", guid)
    return str(getattr(guid, "GuidString", None) or "")


def get_w2scene_event_guid_string(event):
    return _w2scene_guid_string(getattr(event, "GUID", None))


def get_w2scene_event_camera_name(event):
    camera_name = getattr(event, "customCameraName", None)
    if camera_name:
        return camera_name
    camera_definition = getattr(event, "cameraDefinition", None)
    if camera_definition:
        try:
            camera_definition = w3_types.StorySceneCameraDefinition(camera_definition)
            return camera_definition.cameraName
        except Exception:
            log.debug("Could not read cameraDefinition from %s", event.__class__.__name__, exc_info=True)
    return None


def _w2scene_camera_definition_matrix(camera_definition, place_object=None):
    transform = getattr(getattr(camera_definition, "cameraTransform", None), "EngineTransform", None)
    if transform is None:
        return None
    camera_matrix = _engine_transform_matrix(transform, from_this_object=place_object)
    return camera_matrix @ Matrix.Rotation(math.radians(90.0), 4, 'X')


def build_w2scene_camera_definitions(story_scene, place_object=None):
    camera_definitions = {}
    for camera_def in _iter_prop_values(getattr(story_scene, "cameraDefinitions", None)):
        try:
            camera_class = w3_types.StorySceneCameraDefinition(camera_def)
        except Exception:
            log.debug("Could not parse story scene camera definition", exc_info=True)
            continue
        camera_name = str(getattr(camera_class, "cameraName", "") or "")
        if not camera_name:
            continue
        camera_definitions[camera_name] = (
            _w2scene_camera_definition_matrix(camera_class, place_object=place_object),
            _story_scene_camera_track_values(camera_class),
            camera_class,
        )
    return camera_definitions


def resolve_w2scene_event_camera_pose(event, camera_definitions, place_object=None, focus_targets=None):
    camera_definition = getattr(event, "cameraDefinition", None)
    if camera_definition:
        try:
            camera_definition = w3_types.StorySceneCameraDefinition(camera_definition)
            camera_matrix = _w2scene_camera_definition_matrix(camera_definition, place_object=place_object)
            if camera_matrix is not None:
                camera_tracks = _camera_tracks_with_blender_focus_distance(
                    _story_scene_camera_track_values(camera_definition),
                    camera_matrix,
                    focus_targets,
                )
                return camera_matrix, camera_tracks, camera_definition.cameraName
        except Exception:
            log.debug("Could not resolve embedded camera pose from %s", event.__class__.__name__, exc_info=True)

    transform = _custom_camera_event_transform(event)
    if transform is not None:
        camera_matrix = _engine_transform_matrix(transform, from_this_object=place_object)
        camera_matrix = camera_matrix @ Matrix.Rotation(math.radians(90.0), 4, 'X')
        camera_tracks = _camera_tracks_with_blender_focus_distance(
            _story_scene_camera_track_values(event),
            camera_matrix,
            focus_targets,
        )
        return (
            camera_matrix,
            camera_tracks,
            get_w2scene_event_camera_name(event) or getattr(event, "eventName", None),
        )

    camera_name = get_w2scene_event_camera_name(event)
    camera_data = camera_definitions.get(camera_name)
    if not camera_data:
        return None, None, camera_name
    camera_matrix, camera_tracks, _camera_definition = camera_data
    if camera_matrix is None:
        return None, None, camera_name
    camera_matrix = camera_matrix.copy()
    camera_tracks = _camera_tracks_with_blender_focus_distance(camera_tracks, camera_matrix, focus_targets)
    return camera_matrix, camera_tracks, camera_name


def _as_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def _as_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def _dialog_script_duration(dialogscript):
    return _as_float(
        getattr(dialogscript, "_w3_scene_duration_seconds", None)
        or getattr(dialogscript, "approvedDuration", None)
        or getattr(dialogscript, "duration", None),
        0.0,
    )


def _iter_prop_values(prop):
    if prop is None:
        return []
    if isinstance(prop, (list, tuple)):
        return list(prop)
    for attr in ("value", "More", "elements"):
        values = getattr(prop, attr, None)
        if values is not None:
            return list(values)
    return []


def _safe_nla_track_name(prefix, *parts):
    raw = "_".join(str(part or "").strip() for part in parts if str(part or "").strip())
    safe = "".join(char if char.isalnum() or char in "._-" else "_" for char in raw)
    safe = "_".join(part for part in safe.split("_") if part)
    if not safe:
        return prefix
    return f"{prefix}_{safe[:80]}"

class HasAnimationData:
    animation_data: bpy.types.AnimData

from typing import Union
class SceneImporter():
    def __init__(self):
        self._CStoryScene = None
        self.__use_NLA = True
        self.__NLA_track = 'CAMERA_BLEND'
        self.__frame_margin = 0
        self.__frame_current = 0
        self.scene_element_dict = []
        self.scene_sections = []
        self._section_scene_event_elements = []
        self._section_event_variant_by_guid = {}
        self._section_active_variant_id = None
        self._scene_filepath = ""
        self._section_name = ""

    def __assign_action(self, target: Union[bpy.types.ID, HasAnimationData], action: bpy.types.Action, track_name:str = None, at_frame = False):
        if target.animation_data is None:
            target.animation_data_create()
        track_name = track_name if track_name else self.__NLA_track

        if not self.__use_NLA:
            assign_action(target, action)
        else:
            #frame_current = bpy.context.scene.frame_current
            if track_name:
                target_track: bpy.types.NlaTrack = target.animation_data.nla_tracks.get(track_name)
                if target_track is None:
                    target_track: bpy.types.NlaTrack = target.animation_data.nla_tracks.new()
                    target_track.name = track_name #action.name
                strip_frame = at_frame if at_frame is not False else self.__frame_current
                if strip_frame !=0:
                    pass # adding multiple strips
                else:
                    for strip in target_track.strips:
                        target_track.strips.remove(strip)
            else:
                target_track: bpy.types.NlaTrack = target.animation_data.nla_tracks.new()
                target_track.name = action.name
            
            strip_frame = at_frame if at_frame is not False else self.__frame_current
            test_strips = []
            for st in target_track.strips:
                test_strips.append(st)
            if target_track.strips:
                last_strip = target_track.strips[-1]
                strip_start = last_strip.frame_end
            # else:
            try:
                target_strip = target_track.strips.new(action.name, strip_frame, action)
            except Exception as e:
                target_strip = target_track.strips.new(action.name, int(strip_frame + 1), action)
                target_strip.frame_start = strip_frame
                start_frame, end_frame = action.frame_range
                length = end_frame - start_frame
                target_strip.frame_end = strip_frame + length
            bind_strip_action_slot(target_strip, resolve_action_slot(action, target=target, ensure=True))
            target_strip.blend_type = 'REPLACE'

    def loadSceneFile(self, filePath):
        self._scene_filepath = str(filePath or "")
        self._CStoryScene:w3_types.CStoryScene = loadSceneFile(filePath)

    def _section_variant_duration_overrides(self, section):
        duration_by_element_id = {}
        for variant_ptr in _iter_prop_values(getattr(section, "variants", None)):
            try:
                variant_chunk = self._CStoryScene.chunksRef[variant_ptr - 1]
                variant = w3_types.CStorySceneSectionVariant(variant_chunk)
            except Exception:
                log.debug("Could not parse story scene section variant", exc_info=True)
                continue

            for element_info_prop in _iter_prop_values(getattr(variant, "elementInfo", None)):
                try:
                    element_info = w3_types.CStorySceneSectionVariantElementInfo(element_info_prop)
                except Exception:
                    log.debug("Could not parse section variant element info", exc_info=True)
                    continue
                element_id = str(getattr(element_info, "elementId", "") or "")
                approved_duration = getattr(element_info, "approvedDuration", None)
                if element_id and approved_duration is not None:
                    duration_by_element_id[element_id] = _as_float(approved_duration)

            if duration_by_element_id:
                break
        return duration_by_element_id

    def _section_default_variant_id(self, section):
        default_variant_id = getattr(section, "defaultVariantId", None)
        if default_variant_id is not None:
            return _as_int(default_variant_id, 0)
        for variant_ptr in _iter_prop_values(getattr(section, "variants", None)):
            try:
                variant_chunk = self._CStoryScene.chunksRef[variant_ptr - 1]
                variant = w3_types.CStorySceneSectionVariant(variant_chunk)
                return _as_int(getattr(variant, "id", 0), 0)
            except Exception:
                log.debug("Could not parse story scene section variant id", exc_info=True)
        return None

    def _section_event_variant_map(self, section):
        event_variant_by_guid = {}
        for event_info_ptr in _iter_prop_values(getattr(section, "eventsInfo", None)):
            try:
                event_info_chunk = self._CStoryScene.chunksRef[event_info_ptr - 1]
                event_info = w3_types.CStorySceneEventInfo(event_info_chunk)
            except Exception:
                log.debug("Could not parse story scene event info", exc_info=True)
                continue
            guid_string = _w2scene_guid_string(getattr(event_info, "eventGuid", None))
            if guid_string:
                event_variant_by_guid[guid_string] = _as_int(getattr(event_info, "sectionVariantId", 0), 0)
        return event_variant_by_guid

    def _section_event_is_active(self, event, include_muted=False):
        if not include_muted and bool(getattr(event, "isMuted", False) or False):
            return False
        guid_string = get_w2scene_event_guid_string(event)
        active_variant_id = getattr(self, "_section_active_variant_id", None)
        event_variant_by_guid = getattr(self, "_section_event_variant_by_guid", {}) or {}
        if active_variant_id is not None and guid_string in event_variant_by_guid:
            return event_variant_by_guid[guid_string] == active_variant_id
        return True

    def _event_start_frame(self, event, fps=30.0, fallback_dialogscript=None):
        scene_element = getattr(event, "sceneElement", None)
        scene_element_id = getattr(scene_element, "Value", None)
        element_start = self._section_element_start_seconds.get(
            scene_element_id,
            getattr(fallback_dialogscript, "_w3_scene_start_seconds", 0.0),
        )
        element_duration = self._section_element_duration_seconds.get(
            scene_element_id,
            _dialog_script_duration(fallback_dialogscript),
        )
        start_position = _as_float(getattr(event, "startPosition", None), 0.0)
        return (element_start + (element_duration * start_position)) * fps

    def load_section(self, section):
        self.scene_element_dict = {}
        self._section_name = str(getattr(section, "sectionName", "") or "")
        self._section_scene_event_elements = list(getattr(section, "sceneEventElements", []) or [])
        self._section_active_variant_id = self._section_default_variant_id(section)
        self._section_event_variant_by_guid = self._section_event_variant_map(section)
        self._section_element_start_seconds = {}
        self._section_element_duration_seconds = {}
        self._section_duration_seconds = 0.0
        duration_overrides = self._section_variant_duration_overrides(section)

        section_time_seconds = 0.0
        for el in getattr(getattr(section, "sceneElements", None), "value", []) or []:
            chunk = self._CStoryScene.chunksRef[el-1]
            sceneElement = w3_types.str_to_class(chunk.Type)(chunk)
            element_id = str(getattr(sceneElement, "elementID", "") or "")
            duration_seconds = duration_overrides.get(element_id, _dialog_script_duration(sceneElement))
            setattr(sceneElement, "_w3_scene_duration_seconds", duration_seconds)
            setattr(sceneElement, "_w3_scene_start_seconds", section_time_seconds)
            self._section_element_start_seconds[el] = section_time_seconds
            self._section_element_duration_seconds[el] = duration_seconds

            shot_dict = {}
            shot_dict['dialogscript'] = sceneElement
            shot_dict['CUE'] = []
            shot_dict['start_seconds'] = section_time_seconds
            shot_dict['duration_seconds'] = duration_seconds
            self.scene_element_dict[el] = shot_dict
            section_time_seconds += duration_seconds
            #each sceneelement contains dialoge and "CUE" shot that
            #contains all the events (sceneEventElements)
            #shot_1
            #shot_2
            #shot_3
        self._section_duration_seconds = section_time_seconds

        for sceneEventElement in self._section_scene_event_elements:
            el_type =  sceneEventElement.__class__.__name__
            if hasattr(sceneEventElement, 'theType'):
                raise Exception('Missing Event Class')
            else:
                scene_element = getattr(sceneEventElement, "sceneElement", None)
                scene_element_id = getattr(scene_element, "Value", None)
                if scene_element_id in self.scene_element_dict:
                    self.scene_element_dict[scene_element_id]['CUE'].append(sceneEventElement)

    def load_sections(self):
        for el in self._CStoryScene.sections.value: #<array:2,0,ptr:CStorySceneSection>
            chunk = self._CStoryScene.chunksRef[el-1]
            section_cls = w3_types.str_to_class(chunk.Type)
            section = section_cls(chunk)
            self.scene_sections.append(section)

    def preview_camera_event(self, context, event, fps=30.0):
        if event is None:
            raise ValueError("No story scene event selected")
        placeCube = bpy.data.objects.get('SCENE_POINT')
        if not placeCube:
            bpy.ops.object.empty_add(type="PLAIN_AXES", radius=0.1)
            placeCube = bpy.context.object
            placeCube.name = "SCENE_POINT"

        scene_camera_entity_path = W2SCENE_CAMERA_ENTITY_PATH
        scene_cam_obj = check_if_actor_already_in_scene(scene_camera_entity_path)
        if not scene_cam_obj:
            scene_cam_obj = import_entity.import_ent_template(str(Path(get_uncook_path(context)) / scene_camera_entity_path))
        if scene_cam_obj is None:
            raise RuntimeError("Could not load gameplay\\camera\\scene_camera.w2ent")

        event_frame = self._event_start_frame(event, fps=fps)
        if getattr(context, "scene", None) is not None:
            context.scene.frame_set(max(0, int(round(event_frame))))
        context.view_layer.update()

        camera_bone, preview_camera = _prepare_w2scene_camera_rig(context, scene_cam_obj, scene_camera_entity_path)
        if camera_bone is None:
            raise RuntimeError(f"Scene camera rig is missing {CAMERA_CONTROL_BONE}")
        edit_bone = scene_cam_obj.pose.bones.get(CAMERA_EDIT_BONE)
        if edit_bone is None:
            raise RuntimeError(f"Scene camera rig is missing {CAMERA_EDIT_BONE}")

        camera_definitions = build_w2scene_camera_definitions(self._CStoryScene, place_object=placeCube)
        camera_focus_targets = _existing_w2scene_actor_focus_targets(self._CStoryScene)
        camera_matrix, camera_tracks, camera_name = resolve_w2scene_event_camera_pose(
            event,
            camera_definitions,
            place_object=placeCube,
            focus_targets=camera_focus_targets,
        )
        if camera_matrix is None:
            raise RuntimeError(f"Could not resolve camera pose for {event.__class__.__name__}")

        preview_offset = _camera_preview_offset_matrix(scene_cam_obj)
        edit_bone.matrix = _camera_matrix_to_edit_bone_matrix(scene_cam_obj, camera_matrix, preview_offset)
        for track_name in CAMERA_TRACK_NAMES:
            camera_bone[track_name] = _as_float(
                (camera_tracks or {}).get(track_name),
                _W2SCENE_CAMERA_TRACK_DEFAULTS.get(track_name, CAMERA_TRACK_DEFAULTS.get(track_name, 0.0)),
            )
        if preview_camera is not None:
            context.scene.camera = preview_camera
        context.view_layer.update()
        return camera_name or event.__class__.__name__, event_frame

    def execute(self):
        s = time.time()
        _CStoryScene = self._CStoryScene
        context = bpy.context
        placeCube = bpy.data.objects.get('SCENE_POINT')
        if not placeCube:
            bpy.ops.object.empty_add(type="PLAIN_AXES", radius=0.1)
            placeCube = bpy.context.object
            placeCube.name = "SCENE_POINT"

        removed_audio = clear_w2scene_section_audio(context.scene)
        if removed_audio:
            log.info("Removed %d previous .w2scene audio strip(s).", removed_audio)

        scene_camera_entity_path = W2SCENE_CAMERA_ENTITY_PATH

        scene_cam_obj = check_if_actor_already_in_scene(scene_camera_entity_path)
        if not scene_cam_obj:
            scene_cam_obj = import_entity.import_ent_template(str(Path(get_uncook_path(context)) / scene_camera_entity_path))
        context.view_layer.update()
        _scene_camera_bone, scene_camera_preview_obj = _prepare_w2scene_camera_rig(context, scene_cam_obj, scene_camera_entity_path)
        scene_camera_preview_offset = _camera_preview_offset_matrix(scene_cam_obj)

        camera_definitions = {}
        actors_dict= {}
        dialogset_idle_animations = []

        for actor in getattr(getattr(_CStoryScene, "sceneTemplates", None), "value", []) or []: #<array:2,0,ptr:CStorySceneActor>
            actor_template = _CStoryScene.chunksRef[actor-1]
            actor = w3_types.CStorySceneActor(actor_template)
            preferred_appearance = _scene_actor_preferred_appearance(actor)

            actor_obj = check_if_actor_already_in_scene(actor.entityTemplate)
            if not actor_obj:
                actor_obj = import_entity.import_ent_template(
                    repo_file(actor.entityTemplate),
                    load_face_poses=True,
                    import_apperance=1,
                    selected_appearance_name=preferred_appearance,
                )
            else:
                _ensure_cutscene_actor_appearance(actor_obj, preferred_appearance)
            if actor_obj is None:
                log.warning("Skipping scene actor %s; failed to import %s", actor.id, actor.entityTemplate)
                continue
            _ensure_cutscene_face_setup(actor_obj)
            removed_tracks = clear_w2scene_actor_section_nla(context, actor_obj)
            if removed_tracks:
                log.debug("Removed %d stale .w2scene NLA strip(s) from %s", removed_tracks, actor_obj.name)
            actors_dict[actor.id] = (actor_obj, actor)

        for actor_obj, _actor in actors_dict.values():
            reset_transforms(actor_obj)
        context.view_layer.update()

        for di in getattr(getattr(_CStoryScene, "dialogsetInstances", None), "value", []) or []: #<array:2,0,ptr:CStorySceneActor>
            chunk = _CStoryScene.chunksRef[di-1]
            _di = w3_types.CStorySceneDialogsetInstance(chunk)
            placementTag = _di.placementTag[0] if _di.placementTag else None # find placement tag, use for relative transforms

            #place cube should not change if using actor as base. Need to create temp transform
            #placeCube = False
            if placementTag:
                for key, actor in actors_dict.items():
                    if placementTag in (actor[1].actorTags or []):
                        placeCube = actor[0]
                        break

            for dss in getattr(getattr(_di, "slots", None), "value", []) or []: #<array:2,0,ptr:CStorySceneActor>
                chunk = _CStoryScene.chunksRef[dss-1]
                _dss = w3_types.CStorySceneDialogsetSlot(chunk)
                actor_entry = actor_entry_by_str(_dss.actorName, actors_dict)
                if not _dss.actorName or not actor_entry:
                    log.debug("Skipping dialogset slot %s with actor %s", _dss.slotName, _dss.actorName)
                    continue
                actor_obj = actor_entry[0]
                reset_transforms(actor_obj)
                if _dss.slotPlacement:
                    set_blender_object_transform(actor_obj, _dss.slotPlacement.EngineTransform, from_this_object = placeCube)
                else:
                    set_blender_object_transform(actor_obj, BlankEngineTransform(), from_this_object = placeCube)
                if getattr(_dss, "forceBodyIdleAnimation", None):
                    dialogset_idle_animations.append((actor_obj, _dss.forceBodyIdleAnimation, _dss.actorName, _dss.slotName))

        camera_focus_targets = [actor_entry[0] for actor_entry in actors_dict.values() if actor_entry and actor_entry[0]]
        camera_definitions = build_w2scene_camera_definitions(_CStoryScene, place_object=placeCube)

        #shot = scene_element_dict[8]
        self.__frame_current = 0 #? this controls the duration of each strip regardless of Interpolation events
        __fps = 30

        def get_event_camera_pose(event):
            return resolve_w2scene_event_camera_pose(
                event,
                camera_definitions,
                place_object=placeCube,
                focus_targets=camera_focus_targets,
            )

        def get_event_start_frame(shot, event):
            return self._event_start_frame(event, fps=__fps, fallback_dialogscript=shot.get('dialogscript'))

        def extend_nla_track_to_frame(target_obj, track_name, end_frame):
            if target_obj is None or target_obj.animation_data is None:
                return
            target_track = target_obj.animation_data.nla_tracks.get(track_name)
            if target_track is None or not target_track.strips:
                return
            target_strip = max(target_track.strips, key=lambda strip: float(getattr(strip, "frame_start", 0.0) or 0.0))
            strip_start = float(getattr(target_strip, "frame_start", 0.0) or 0.0)
            if end_frame <= strip_start:
                return
            action = getattr(target_strip, "action", None)
            action_length = 0.0
            if action is not None:
                try:
                    action_start, action_end = action.frame_range
                    action_length = float(action_end) - float(action_start)
                except Exception:
                    action_length = 0.0
            if action_length > 0.0 and hasattr(target_strip, "repeat"):
                try:
                    target_strip.repeat = max(float(getattr(target_strip, "repeat", 1.0) or 1.0), math.ceil((end_frame - strip_start) / action_length))
                except Exception:
                    pass
            try:
                target_strip.frame_end = end_frame
            except Exception:
                pass

        def load_scene_animation_by_name(anim_name, actor_obj, track_name, at_frame, face_target_mode="auto", show_all=False, extend_to_frame=None):
            if actor_obj is None or not anim_name:
                return False
            try:
                SetupActor(actor_obj, show_all=show_all)
                resolved_anim_name, fdir = GetAnimationInfoByName(anim_name)
                if not resolved_anim_name or not fdir:
                    log.warning("Skipping scene animation '%s'; animation was not found", anim_name)
                    return False
                target_armatures = load_anim_into_scene(
                    bpy.context,
                    resolved_anim_name,
                    fdir,
                    actor_obj,
                    track_name,
                    at_frame=at_frame,
                    face_target_mode=face_target_mode,
                )
                if extend_to_frame is not None:
                    for target_armature in target_armatures or [actor_obj]:
                        extend_nla_track_to_frame(target_armature, track_name, extend_to_frame)
                return True
            except Exception:
                log.warning(
                    "Failed to load scene animation '%s' on '%s'",
                    anim_name,
                    getattr(actor_obj, "name", "<unknown>"),
                    exc_info=True,
                )
            return False

        CustomCameraInstances = {}
        camera_interpolation_key_guids = set()
        for section_event in getattr(self, "_section_scene_event_elements", []) or []:
            if not self._section_event_is_active(section_event, include_muted=True):
                continue
            if section_event.__class__.__name__ in {"CStorySceneEventCustomCamera", "CStorySceneEventCustomCameraInstance"}:
                guid_string = get_w2scene_event_guid_string(section_event)
                if guid_string:
                    CustomCameraInstances[guid_string] = section_event
            if section_event.__class__.__name__ == "CStorySceneEventCameraInterpolation" and self._section_event_is_active(section_event):
                for guid in _iter_prop_values(getattr(section_event, "keyGuids", None)):
                    guid_string = getattr(guid, "GuidString", None)
                    if guid_string:
                        camera_interpolation_key_guids.add(guid_string)

        section_end_frame = self._section_duration_seconds * __fps
        for actor_obj, idle_name, actor_name, slot_name in dialogset_idle_animations:
            load_scene_animation_by_name(
                idle_name,
                actor_obj,
                "SceneDialogsetIdle",
                0.0,
                extend_to_frame=section_end_frame,
            )
        
        
        
        ###################
        #   RESET SCENE   #
        ###################
        #TODO delete all markers in the scene
        
        def remove_strips_from_track(scene_cam_obj, trackname):
            if scene_cam_obj.animation_data is None:
                log.debug("No animation data yet.")
                return
            if trackname in scene_cam_obj.animation_data.nla_tracks:
                track = scene_cam_obj.animation_data.nla_tracks[trackname]
                for strip in track.strips:
                    track.strips.remove(strip)
            else:
                log.debug("Track '%s' not found in the NLA Editor.", trackname)
        
        def reset_scene(scene_cam_obj):
            scene = bpy.context.scene
            for marker in scene.timeline_markers:
                scene.timeline_markers.remove(marker)
                
            remove_strips_from_track(scene_cam_obj, "CameraInterpolation")
            remove_strips_from_track(scene_cam_obj, "CustomCamera")
            remove_strips_from_track(scene_cam_obj, "CustomCameraInstance")
            remove_strips_from_track(scene_cam_obj, "CameraShots")
            remove_strips_from_track(scene_cam_obj, "PAUSE")
            remove_strips_from_track(scene_cam_obj, "dialogLine")
        reset_scene(scene_cam_obj)

        class _Dummy: pass
        dummy_keyframe_points = iter(lambda: _Dummy, None)

        def create_scene_camera_action(action_name, key_data, strip_start_frame, interpolation='LINEAR'):
            if not key_data:
                return None
            bl_bone = scene_cam_obj.pose.bones.get(CAMERA_EDIT_BONE)
            camera_bone = ensure_camera_track_properties(scene_cam_obj, track_names=CAMERA_TRACK_NAMES)
            if bl_bone is None or camera_bone is None:
                log.warning("Skipping %s; scene camera rig is missing %s or %s", action_name, CAMERA_EDIT_BONE, CAMERA_CONTROL_BONE)
                return None

            action = bpy.data.actions.new(name=action_name)
            pos_curves = [dummy_keyframe_points] * 3
            rot_curves = [dummy_keyframe_points] * 4

            prop_rot_map = {'QUATERNION':'rotation_quaternion', 'AXIS_ANGLE':'rotation_axis_angle'}
            data_path_rot = prop_rot_map.get(bl_bone.rotation_mode, 'rotation_quaternion')
            bone_rotation = getattr(bl_bone, data_path_rot)
            data_path = 'pose.bones["%s"].location'%bl_bone.name
            for axis_i in range(3):
                pos_curves[axis_i] = new_action_fcurve(action, scene_cam_obj, data_path=data_path, index=axis_i, group_name=bl_bone.name)
            data_path = 'pose.bones["%s"].%s'%(bl_bone.name, data_path_rot)
            for axis_i in range(len(bone_rotation)):
                rot_curves[axis_i] = new_action_fcurve(action, scene_cam_obj, data_path=data_path, index=axis_i, group_name=bl_bone.name)

            track_curves = {
                track_name: new_action_fcurve(
                    action,
                    scene_cam_obj,
                    data_path=f"pose.bones[\"{CAMERA_CONTROL_BONE}\"][\"{track_name}\"]",
                )
                for track_name in CAMERA_TRACK_NAMES
            }

            for key_frame, camera_matrix, camera_tracks, _event in key_data:
                bl_bone.matrix = _camera_matrix_to_edit_bone_matrix(scene_cam_obj, camera_matrix, scene_camera_preview_offset)
                for track_name in CAMERA_TRACK_NAMES:
                    camera_bone[track_name] = _as_float(
                        (camera_tracks or {}).get(track_name),
                        _W2SCENE_CAMERA_TRACK_DEFAULTS.get(track_name, CAMERA_TRACK_DEFAULTS.get(track_name, 0.0)),
                    )
                interFrame = key_frame - strip_start_frame

                for i in range(3):
                    pos_curves[i].keyframe_points.add(1)
                    pos_curves[i].keyframe_points[-1].co = (interFrame, bl_bone.location[i])
                    pos_curves[i].keyframe_points[-1].interpolation = interpolation
                rotation_values = getattr(bl_bone, data_path_rot)
                for i in range(len(rotation_values)):
                    rot_curves[i].keyframe_points.add(1)
                    rot_curves[i].keyframe_points[-1].co = (interFrame, rotation_values[i])
                    rot_curves[i].keyframe_points[-1].interpolation = interpolation
                for track_name, track_curve in track_curves.items():
                    point = track_curve.keyframe_points.insert(interFrame, float(camera_bone[track_name]))
                    point.interpolation = interpolation
            return action

        def object_transform_values(obj):
            prop_rot_map = {'QUATERNION':'rotation_quaternion', 'AXIS_ANGLE':'rotation_axis_angle'}
            data_path_rot = prop_rot_map.get(obj.rotation_mode, 'rotation_euler')
            return (
                tuple(float(value) for value in obj.location),
                tuple(float(value) for value in getattr(obj, data_path_rot)),
                tuple(float(value) for value in obj.scale),
                data_path_rot,
            )

        def engine_transform_to_object_values(obj, engine_transform, from_object):
            saved_matrix_world = obj.matrix_world.copy()
            saved_location = obj.location.copy()
            saved_rotation_euler = obj.rotation_euler.copy()
            saved_rotation_quaternion = obj.rotation_quaternion.copy()
            saved_rotation_axis_angle = tuple(float(value) for value in obj.rotation_axis_angle)
            saved_scale = obj.scale.copy()
            try:
                set_blender_object_transform(obj, engine_transform, from_this_object=from_object)
                return object_transform_values(obj)
            finally:
                obj.matrix_world = saved_matrix_world
                obj.location = saved_location
                obj.rotation_euler = saved_rotation_euler
                obj.rotation_quaternion = saved_rotation_quaternion
                for axis_i, value in enumerate(saved_rotation_axis_angle):
                    obj.rotation_axis_angle[axis_i] = value
                obj.scale = saved_scale

        def create_object_transform_action(action_name, target_obj, key_data, interpolation='CONSTANT'):
            if target_obj is None or not key_data:
                return None
            data_path_rot = key_data[0][1][3]
            rotation_values = getattr(target_obj, data_path_rot)
            action = bpy.data.actions.new(name=action_name)
            loc_curves = [
                new_action_fcurve(action, target_obj, data_path='location', index=axis_i, group_name="Placement")
                for axis_i in range(3)
            ]
            rot_curves = [
                new_action_fcurve(action, target_obj, data_path=data_path_rot, index=axis_i, group_name="Placement")
                for axis_i in range(len(rotation_values))
            ]
            scale_curves = [
                new_action_fcurve(action, target_obj, data_path='scale', index=axis_i, group_name="Placement")
                for axis_i in range(3)
            ]

            for frame, values, _event in key_data:
                location_values, rotation_values, scale_values, _rot_path = values
                for axis_i, curve in enumerate(loc_curves):
                    point = curve.keyframe_points.insert(frame, float(location_values[axis_i]))
                    point.interpolation = interpolation
                for axis_i, curve in enumerate(rot_curves):
                    point = curve.keyframe_points.insert(frame, float(rotation_values[axis_i]))
                    point.interpolation = interpolation
                for axis_i, curve in enumerate(scale_curves):
                    point = curve.keyframe_points.insert(frame, float(scale_values[axis_i]))
                    point.interpolation = interpolation
            return action

        camera_shot_key_data = []
        placement_key_data_by_actor = {}
        
        for key, shot in self.scene_element_dict.items():
            # if key != 7:
            #     break
            # if key == 7:
            #     continue
            curr_actor = None
            
            ###################
            #   SHOT SCRIPT   #
            ###################
            if shot['dialogscript'].__class__.__name__ == 'CStoryScenePauseElement':
                action = bpy.data.actions.new(name="PAUSE")
                pos_curves = [dummy_keyframe_points] * 3
                dialogframe = self.__frame_current
                duration_frames = _dialog_script_duration(shot['dialogscript']) * __fps
                for axis_i in range(3):
                    pos_curves[axis_i] = new_action_fcurve(action, scene_cam_obj, data_path='location', index=axis_i, group_name="PAUSE")
                #PAUSE BEGIN
                for i in range(3):
                    pos_curves[i].keyframe_points.add(1)
                    pos_curves[i].keyframe_points[-1].co = (0.0, scene_cam_obj.location[i])
                    pos_curves[i].keyframe_points[-1].interpolation = 'LINEAR'

                #PAUSE END
                dialogframe=self.__frame_current + duration_frames
                for i in range(3):
                    pos_curves[i].keyframe_points.add(1)
                    pos_curves[i].keyframe_points[-1].co = (duration_frames, scene_cam_obj.location[i])
                    pos_curves[i].keyframe_points[-1].interpolation = 'LINEAR'

                self.__assign_action(scene_cam_obj, action, track_name = "PAUSE")
            elif shot['dialogscript'].__class__.__name__ == 'CStorySceneLine':
                curr_actor = set_cur_actor_by_str(shot['dialogscript'].voicetag, actors_dict)
                if curr_actor is not None:
                    _ensure_cutscene_face_setup(curr_actor)
                    load_voice_and_lipsync(
                        shot['dialogscript'].dialogLine.String.val,
                        curr_actor,
                        context=context,
                        at_frame=self.__frame_current,
                        nla_mode='replace',
                        strip_props={
                            W2SCENE_AUDIO_STRIP_PROP: True,
                            W2SCENE_AUDIO_SOURCE_PROP: self._scene_filepath,
                            W2SCENE_AUDIO_SECTION_PROP: self._section_name,
                        },
                    )
                action = bpy.data.actions.new(name="dialogLine")
                pos_curves = [dummy_keyframe_points] * 3
                dialogframe = self.__frame_current
                duration_frames = _dialog_script_duration(shot['dialogscript']) * __fps
                for axis_i in range(3):
                    pos_curves[axis_i] = new_action_fcurve(action, scene_cam_obj, data_path='location', index=axis_i, group_name="dialogLine")
                #PAUSE BEGIN
                for i in range(3):
                    pos_curves[i].keyframe_points.add(1)
                    pos_curves[i].keyframe_points[-1].co = (0.0, scene_cam_obj.location[i])
                    pos_curves[i].keyframe_points[-1].interpolation = 'LINEAR'

                #PAUSE END
                dialogframe=self.__frame_current + duration_frames
                for i in range(3):
                    pos_curves[i].keyframe_points.add(1)
                    pos_curves[i].keyframe_points[-1].co = (duration_frames, scene_cam_obj.location[i])
                    pos_curves[i].keyframe_points[-1].interpolation = 'LINEAR'

                self.__assign_action(scene_cam_obj, action, track_name = "dialogLine")
            else:
                dialogframe = self.__frame_current
                log.debug("Unhandled dialog script type: %s", shot['dialogscript'].__class__.__name__)
                # "CStorySceneDialogset",
                # "CStorySceneDialogsetInstance",
                # "CStorySceneDialogsetSlot",
            
            ###################
            #  SHOT ELEMENTS  #
            ###################

            #shot['CUE'] = []
            for event in shot['CUE']:
                if not self._section_event_is_active(event):
                    log.debug("Skipping muted or inactive scene event: %s", event.__class__.__name__)
                    continue
                if event.__class__.__name__ == "CStorySceneEventAnimation":
                    #event: w3_types.CStorySceneEventAnimation
                    actor_name = getattr(event, "actor", None) or getattr(event, "actorName", None)
                    event_actor = set_cur_actor_by_str(actor_name, actors_dict) if actor_name else curr_actor
                    if event_actor is None:
                        continue
                    guid_suffix = (get_w2scene_event_guid_string(event) or "")[:8]
                    load_scene_animation_by_name(
                        event.animationName,
                        event_actor,
                        _safe_nla_track_name("cutscene_import_body", actor_name, event.animationName, guid_suffix),
                        get_event_start_frame(shot, event),
                    )
                elif  event.__class__.__name__ == "CStorySceneEventChangePose": #type(event) == w3_types.CStorySceneEventChangePose:
                    event: w3_types.CStorySceneEventChangePose
                    curr_actor = set_cur_actor_by_str(event.actor, actors_dict)
                    if curr_actor is None:
                        continue
                    if event.forceBodyIdleAnimation:
                        anim_name = event.forceBodyIdleAnimation
                    elif event.transitionAnimation:
                        anim_name = event.transitionAnimation
                    else:
                        anim_name = None
                    load_scene_animation_by_name(
                        anim_name,
                        curr_actor,
                        _safe_nla_track_name("cutscene_import_pose", event.actor, anim_name, (get_w2scene_event_guid_string(event) or "")[:8]),
                        get_event_start_frame(shot, event),
                    )
                elif event.__class__.__name__ == "CStorySceneEventMimicsAnim":
                    event: w3_types.CStorySceneEventMimicsAnim
                    actor_obj = set_cur_actor_by_str(event.actor, actors_dict)
                    if actor_obj is None:
                        continue
                    _ensure_cutscene_face_setup(actor_obj)
                    load_scene_animation_by_name(
                        event.animationName,
                        actor_obj,
                        _safe_nla_track_name("cutscene_import_mimic", event.actor, event.animationName, (get_w2scene_event_guid_string(event) or "")[:8]),
                        get_event_start_frame(shot, event),
                        face_target_mode="owner",
                        show_all=True,
                    )
                elif event.__class__.__name__ == "CStorySceneEventOverridePlacement":
                    event: w3_types.CStorySceneEventOverridePlacement
                    actor_obj = set_cur_actor_by_str(event.actorName, actors_dict)
                    if actor_obj is None:
                        continue
                    engine_transform = event.placement.EngineTransform if event.placement else EngineTransform()
                    placement_frame = get_event_start_frame(shot, event)
                    placement_values = engine_transform_to_object_values(actor_obj, engine_transform, placeCube)
                    actor_key = getattr(actor_obj, "name", str(id(actor_obj)))
                    placement_entry = placement_key_data_by_actor.setdefault(
                        actor_key,
                        {"object": actor_obj, "keys": []},
                    )
                    placement_entry["keys"].append((placement_frame, placement_values, event))
                elif  event.__class__.__name__ ==  "CStorySceneEventCameraInterpolation":
                    keyGuidsObjs = []
                    for guid in _iter_prop_values(getattr(event, "keyGuids", None)):
                        guid_string = getattr(guid, "GuidString", None)
                        cam_event = CustomCameraInstances.get(guid_string)
                        if not cam_event:
                            log.warning("Skipping camera interpolation key %s with no matching custom camera event", guid_string)
                            continue
                        camera_matrix, camera_tracks, camera_name = get_event_camera_pose(cam_event)
                        if camera_matrix is None:
                            log.warning("Skipping camera interpolation key %s with unresolved camera %s", guid_string, camera_name)
                            continue
                        keyGuidsObjs.append((get_event_start_frame(shot, cam_event), camera_matrix, camera_tracks, cam_event))

                    if not keyGuidsObjs:
                        log.warning("Skipping camera interpolation event with no resolved camera keys")
                        continue
                    strip_start_frame = min(frame for frame, _matrix, _tracks, _event in keyGuidsObjs)
                    camera_action = create_scene_camera_action("CameraInterpolation", keyGuidsObjs, strip_start_frame)
                    if camera_action is not None:
                        self.__assign_action(scene_cam_obj, camera_action, track_name = "CameraInterpolation", at_frame=strip_start_frame)
                
                elif  event.__class__.__name__ ==  "CStorySceneEventCustomCamera":
                    guid_string = get_w2scene_event_guid_string(event)
                    if guid_string:
                        CustomCameraInstances[guid_string] = event
                    if guid_string and guid_string in camera_interpolation_key_guids:
                        continue
                    camera_matrix, camera_tracks, camera_name = get_event_camera_pose(event)
                    if camera_matrix is None:
                        log.warning("Skipping custom camera event with unresolved camera %s", camera_name)
                        continue
                    marker_frame = get_event_start_frame(shot, event)
                    marker = bpy.context.scene.timeline_markers.new(camera_name or "CustomCamera", frame=int(marker_frame))
                    if scene_camera_preview_obj is not None:
                        marker.camera = scene_camera_preview_obj
                    camera_shot_key_data.append((marker_frame, camera_matrix, camera_tracks, event))
                
                elif  event.__class__.__name__ ==  "CStorySceneEventCustomCameraInstance":
                    guid_string = get_w2scene_event_guid_string(event)
                    if guid_string:
                        CustomCameraInstances[guid_string] = event
                    camera_matrix, camera_tracks, camera_name = get_event_camera_pose(event)
                    if camera_matrix is None:
                        log.warning("Skipping custom camera instance with unresolved camera %s", camera_name)
                        continue
                    marker_frame = get_event_start_frame(shot, event)
                    marker = bpy.context.scene.timeline_markers.new(camera_name or "CustomCameraInstance", frame=int(marker_frame))
                    if scene_camera_preview_obj is not None:
                        marker.camera = scene_camera_preview_obj
                    camera_shot_key_data.append((marker_frame, camera_matrix, camera_tracks, event))
                else:
                    log.debug("Unhandled event type: %s", event.__class__.__name__)
            
            ###################
            #  SHOT ENDING    #
            ###################
            self.__frame_current = dialogframe #! THE FRAME THIS SHOT ENDS ON

        if camera_shot_key_data:
            camera_shot_key_data.sort(key=lambda item: item[0])
            last_frame, last_matrix, last_tracks, last_event = camera_shot_key_data[-1]
            if section_end_frame > last_frame:
                camera_shot_key_data.append((section_end_frame, last_matrix.copy(), dict(last_tracks or {}), last_event))
            camera_action = create_scene_camera_action("CameraShots", camera_shot_key_data, 0.0, interpolation='CONSTANT')
            if camera_action is not None:
                self.__assign_action(scene_cam_obj, camera_action, track_name="CustomCamera", at_frame=0.0)

        for placement_entry in placement_key_data_by_actor.values():
            actor_obj = placement_entry["object"]
            placement_keys = list(placement_entry["keys"])
            if actor_obj is None or not placement_keys:
                continue
            placement_keys.sort(key=lambda item: item[0])
            action_keys = []
            if placement_keys[0][0] > 0.0:
                action_keys.append((0.0, object_transform_values(actor_obj), None))
            action_keys.extend(placement_keys)
            last_frame, last_values, last_event = action_keys[-1]
            if section_end_frame > last_frame:
                action_keys.append((section_end_frame, last_values, last_event))
            action_name = _safe_nla_track_name("ScenePlacement", getattr(actor_obj, "name", "Actor"))
            placement_action = create_object_transform_action(action_name, actor_obj, action_keys, interpolation='CONSTANT')
            if placement_action is not None:
                self.__assign_action(actor_obj, placement_action, track_name="ScenePlacement", at_frame=0.0)
                extend_nla_track_to_frame(actor_obj, "ScenePlacement", section_end_frame)

        log.info(f'Loaded scene in {time.time() - s} seconds.')

def import_w3_scene(filePath):
    sceneImporter = SceneImporter()
    sceneImporter.loadSceneFile(filePath)
    return sceneImporter

def actor_entry_by_str(actor_tag_str, actors_dict):
    actor_entry = actors_dict.get(actor_tag_str)
    if actor_entry is None:
        needle = str(actor_tag_str or "").strip().lower()
        for _actor_id, candidate in actors_dict.items():
            actor = candidate[1]
            actor_tags = [str(tag or "").strip().lower() for tag in (getattr(actor, "actorTags", None) or [])]
            alias = str(getattr(actor, "alias", "") or "").strip().lower()
            if needle and (needle == alias or needle in actor_tags):
                actor_entry = candidate
                break
    return actor_entry

def set_cur_actor_by_str(actor_tag_str, actors_dict):
    actor_entry = actor_entry_by_str(actor_tag_str, actors_dict)
    if actor_entry is None or actor_entry[0] is None:
        log.warning("Scene actor '%s' is not loaded; skipping actor-bound event", actor_tag_str)
        return None

    curr_actor = actor_entry[0]
    bpy.ops.object.select_all(action='DESELECT')
    curr_actor.select_set(True)
    bpy.context.view_layer.objects.active = curr_actor
    return curr_actor
