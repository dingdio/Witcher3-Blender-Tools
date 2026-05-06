import logging
log = logging.getLogger(__name__)
import os
import json
import numpy as np
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
from mathutils import Euler
from math import radians
import math
from mathutils import Matrix
from ..ui.ui_voice import load_voice_and_lipsync
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


def _as_float(value, default=0.0):
    try:
        return float(value)
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

def create_camera_drivers(camera_obj, camera, name):
    camera_data:bpy.types.Camera = camera.data
    camera_data.lens_unit = 'FOV' #convert witcher FOV angle to mm, angle cannot be driven it uses mm lens prop
    camera_data.sensor_fit = 'VERTICAL'
    camera_data.sensor_height = 43.266615300557

    driver_curve = camera_data.driver_add("lens")
    driver = driver_curve.driver
    channel = name
    driver.expression = f'43.266615300557 / ( 2 * tan( pi * {channel} / 360.0 ) )' #channel
    var = driver.variables.get(channel)
    if var is None:
        var = driver.variables.new()
    var.type = "SINGLE_PROP"
    var.name = channel
    camera_obj["%s" % channel] = 35
    target = var.targets[0]
    target.id_type = "OBJECT"
    target.data_path = '["%s"]' % channel #'["%s"]' % channel
    target.id = camera_obj
    camera_obj.update_tag()

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

    def load_section(self, section):
        self.scene_element_dict = {}
        self._section_scene_event_elements = list(getattr(section, "sceneEventElements", []) or [])
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

    def execute(self):
        s = time.time()
        _CStoryScene = self._CStoryScene
        context = bpy.context
        placeCube = bpy.data.objects.get('SCENE_POINT')
        if not placeCube:
            bpy.ops.object.empty_add(type="PLAIN_AXES", radius=0.1)
            placeCube = bpy.context.object
            placeCube.name = "SCENE_POINT"


        scene_camera_entity_path = "gameplay\\camera\\scene_camera.w2ent"

        scene_cam_obj = check_if_actor_already_in_scene(scene_camera_entity_path)
        if not scene_cam_obj:
            scene_cam_obj = import_entity.import_ent_template(str(Path(get_uncook_path(context)) / scene_camera_entity_path))

        cams_in_scene = {}
        for camera_def in _CStoryScene.cameraDefinitions.More: #<array:2,0,StorySceneCameraDefinition>
            camera_class = w3_types.StorySceneCameraDefinition(camera_def)
            cam_in_scene = check_if_camera_already_in_scene(camera_class.cameraName)
            if not cam_in_scene:
                # cam_in_scene = import_entity.import_ent_template(str(scene_camera_entity))
                # cam_in_scene.name = camera_class.cameraName
                # camera_node = cam_in_scene.pose.bones['Camera_Node']
                # bpy.ops.object.posemode_toggle()
                # set_blender_pose_bone_transform(camera_node, camera_class.cameraTransform.EngineTransform)
                # bpy.ops.object.posemode_toggle()
                camera_data = bpy.data.cameras.new(name=camera_class.cameraName)
                cam_in_scene = bpy.data.objects.new(camera_class.cameraName, camera_data)
                bpy.context.collection.objects.link(cam_in_scene)
                create_camera_drivers(cam_in_scene, cam_in_scene, 'hctFOV')

            else:
                reset_transforms(cam_in_scene)
            set_blender_object_transform(cam_in_scene, camera_class.cameraTransform.EngineTransform, from_this_object = placeCube)
            cam_in_scene.rotation_euler[0] += np.pi/2
            if camera_class.cameraFov != None:
                #cam_in_scene.data.lens = camera_class.cameraFov
                cam_in_scene['hctFOV'] = camera_class.cameraFov
                #cam_in_scene.data.sensor_width = camera_class.cameraFov
            else:
                cam_in_scene['hctFOV'] = 50.0

            #StorySceneCameraDefinition
            #gameplay\camera\scene_camera.w2ent
            cams_in_scene[camera_class.cameraName] = cam_in_scene
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
            actors_dict[actor.id] = (actor_obj, actor)

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

        #shot = scene_element_dict[8]
        self.__frame_current = 0 #? this controls the duration of each strip regardless of Interpolation events
        __fps = 30

        def get_event_camera_name(event):
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

        def get_event_camera_pose(event):
            camera_definition = getattr(event, "cameraDefinition", None)
            if camera_definition:
                try:
                    camera_definition = w3_types.StorySceneCameraDefinition(camera_definition)
                    transform = getattr(getattr(camera_definition, "cameraTransform", None), "EngineTransform", None)
                    if transform:
                        fov = camera_definition.cameraFov if camera_definition.cameraFov is not None else getattr(event, "cameraFov", None)
                        camera_matrix = _engine_transform_matrix(transform, from_this_object=placeCube)
                        camera_matrix = camera_matrix @ Matrix.Rotation(math.radians(90.0), 4, 'X')
                        return camera_matrix, fov or 50.0, camera_definition.cameraName
                except Exception:
                    log.debug("Could not resolve embedded camera pose from %s", event.__class__.__name__, exc_info=True)

            camera_name = get_event_camera_name(event)
            cam_in_scene = cams_in_scene.get(camera_name)
            if not cam_in_scene:
                return None, None, camera_name
            return cam_in_scene.matrix_world.copy(), cam_in_scene.get('hctFOV', 50.0), camera_name

        def get_event_guid_string(event):
            guid = getattr(event, "GUID", None)
            guid = getattr(guid, "GUID", guid)
            return getattr(guid, "GuidString", None)

        def get_event_start_frame(shot, event):
            scene_element = getattr(event, "sceneElement", None)
            scene_element_id = getattr(scene_element, "Value", None)
            element_start = self._section_element_start_seconds.get(
                scene_element_id,
                getattr(shot.get('dialogscript'), "_w3_scene_start_seconds", 0.0),
            )
            element_duration = self._section_element_duration_seconds.get(
                scene_element_id,
                _dialog_script_duration(shot.get('dialogscript')),
            )
            start_position = _as_float(getattr(event, "startPosition", None), 0.0)
            return (element_start + (element_duration * start_position)) * __fps

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
        for section_event in getattr(self, "_section_scene_event_elements", []) or []:
            if section_event.__class__.__name__ in {"CStorySceneEventCustomCamera", "CStorySceneEventCustomCameraInstance"}:
                guid_string = get_event_guid_string(section_event)
                if guid_string:
                    CustomCameraInstances[guid_string] = section_event

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
            remove_strips_from_track(scene_cam_obj, "CustomCameraInstance")
            remove_strips_from_track(scene_cam_obj, "PAUSE")
            remove_strips_from_track(scene_cam_obj, "dialogLine")
        reset_scene(scene_cam_obj)

        class _Dummy: pass
        dummy_keyframe_points = iter(lambda: _Dummy, None)
        
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
                    load_voice_and_lipsync(shot['dialogscript'].dialogLine.String.val, curr_actor, context=context, at_frame = self.__frame_current)
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
                if event.__class__.__name__ == "CStorySceneEventAnimation":
                    #event: w3_types.CStorySceneEventAnimation
                    actor_name = getattr(event, "actor", None) or getattr(event, "actorName", None)
                    event_actor = set_cur_actor_by_str(actor_name, actors_dict) if actor_name else curr_actor
                    if event_actor is None:
                        continue
                    guid_suffix = (get_event_guid_string(event) or "")[:8]
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
                        _safe_nla_track_name("cutscene_import_pose", event.actor, anim_name, (get_event_guid_string(event) or "")[:8]),
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
                        _safe_nla_track_name("cutscene_import_mimic", event.actor, event.animationName, (get_event_guid_string(event) or "")[:8]),
                        get_event_start_frame(shot, event),
                        face_target_mode="owner",
                        show_all=True,
                    )
                elif event.__class__.__name__ == "CStorySceneEventOverridePlacement":
                    event: w3_types.CStorySceneEventOverridePlacement
                    actor_obj = set_cur_actor_by_str(event.actorName, actors_dict)
                    if actor_obj is None:
                        continue
                    set_blender_object_transform(actor_obj, event.placement.EngineTransform if event.placement else EngineTransform(), from_this_object = placeCube)
                elif  event.__class__.__name__ ==  "CStorySceneEventCameraInterpolation":
                    InterpolationAction = bpy.data.actions.new(name="CameraInterpolation")
                    bl_bone = scene_cam_obj.pose.bones['Camera_ManipulationNode']
                    rotation_fix_matrix = Matrix.Rotation(math.radians(-90.0), 4, 'X') #! maybe just set cam for w2scenes?
                    pos_curves = [dummy_keyframe_points] * 3
                    rot_curves = [dummy_keyframe_points] * 4

                    prop_rot_map = {'QUATERNION':'rotation_quaternion', 'AXIS_ANGLE':'rotation_axis_angle'}
                    data_path_rot = prop_rot_map.get(bl_bone.rotation_mode, 'rotation_quaternion')
                    bone_rotation = getattr(bl_bone, data_path_rot)
                    data_path = 'pose.bones["%s"].location'%bl_bone.name
                    for axis_i in range(3):
                        pos_curves[axis_i] = new_action_fcurve(InterpolationAction, scene_cam_obj, data_path=data_path, index=axis_i, group_name=bl_bone.name)
                    data_path = 'pose.bones["%s"].%s'%(bl_bone.name, data_path_rot)
                    for axis_i in range(len(bone_rotation)):
                        rot_curves[axis_i] = new_action_fcurve(InterpolationAction, scene_cam_obj, data_path=data_path, index=axis_i, group_name=bl_bone.name)

                    track_curves = [new_action_fcurve(InterpolationAction, scene_cam_obj, data_path="pose.bones[\"Camera_Node\"][\"hctFOV\"]")] 
                    
                    keyGuidsObjs = []
                    for guid in getattr(getattr(event, "keyGuids", None), "More", []) or []:
                        cam_event = CustomCameraInstances.get(guid.GuidString)
                        if not cam_event:
                            log.warning("Skipping camera interpolation key %s with no matching custom camera event", guid.GuidString)
                            continue
                        camera_matrix, camera_fov, camera_name = get_event_camera_pose(cam_event)
                        if camera_matrix is None:
                            log.warning("Skipping camera interpolation key %s with unresolved camera %s", guid.GuidString, camera_name)
                            continue
                        keyGuidsObjs.append((get_event_start_frame(shot, cam_event), camera_matrix, camera_fov, cam_event))

                    if not keyGuidsObjs:
                        log.warning("Skipping camera interpolation event with no resolved camera keys")
                        continue
                    strip_start_frame = min(frame for frame, _matrix, _fov, _event in keyGuidsObjs)
                    for key_frame, camera_matrix, camera_fov, event in keyGuidsObjs:
                        bone_matrix = camera_matrix @ rotation_fix_matrix
                        bl_bone.matrix = scene_cam_obj.matrix_world.inverted() @ bone_matrix
                        scene_cam_obj.pose.bones["Camera_Node"]["hctFOV"] = camera_fov
                        interFrame = key_frame - strip_start_frame

                        for i in range(3):
                            pos_curves[i].keyframe_points.add(1)
                            pos_curves[i].keyframe_points[-1].co = (interFrame, bl_bone.location[i])
                            pos_curves[i].keyframe_points[-1].interpolation = 'LINEAR'
                        for i in range(4):
                            rot_curves[i].keyframe_points.add(1)
                            rot_curves[i].keyframe_points[-1].co = (interFrame, bl_bone.rotation_quaternion[i])
                            rot_curves[i].keyframe_points[-1].interpolation = 'LINEAR'

                        track_curves[0].keyframe_points.insert(interFrame, camera_fov)
                        track_curves[0].keyframe_points[-1].interpolation = 'LINEAR' # CONSTANT
                        #bone.keyframe_insert(data_path='location', frame=frame)
                        #bone.keyframe_insert(data_path='rotation_quaternion', frame=frame)
                        
                        # location_curve.keyframe_points[-1].co = (frame, bone.location[0])
                        # location_curve.keyframe_points[-1].handle_left_type = 'VECTOR'
                        # location_curve.keyframe_points[-1].handle_right_type = 'VECTOR'
                        
                    self.__assign_action(scene_cam_obj, InterpolationAction, track_name = "CameraInterpolation", at_frame=strip_start_frame)
                
                elif  event.__class__.__name__ ==  "CStorySceneEventCustomCamera":
                    guid_string = get_event_guid_string(event)
                    if guid_string:
                        CustomCameraInstances[guid_string] = event
                    cam_event = event
                    #TODO create camera and add to scene
                
                elif  event.__class__.__name__ ==  "CStorySceneEventCustomCameraInstance":
                    guid_string = get_event_guid_string(event)
                    if guid_string:
                        CustomCameraInstances[guid_string] = event
                    cam_event = event
                    
                    keyGuidsObjs = []
                    camera_matrix, camera_fov, camera_name = get_event_camera_pose(cam_event)
                    if camera_matrix is None:
                        log.warning("Skipping custom camera instance with unresolved camera %s", camera_name)
                        continue
                    keyGuidsObjs.append((camera_matrix, camera_fov, cam_event))
                    marker_frame = get_event_start_frame(shot, event)
                    context = bpy.context
                    scene = context.scene

                    marker = scene.timeline_markers.new(camera_name or "CustomCameraInstance", frame=int(marker_frame))
                    ##todo check if this cam has an interpolation even and use the scene cam instead
                    #marker.camera = scene_cam_obj #cams_in_scene[cam_event.customCameraName]
                    
                    InterpolationAction = bpy.data.actions.new(name="CustomCameraInstance")
                    bl_bone = scene_cam_obj.pose.bones['Camera_ManipulationNode']
                    rotation_fix_matrix = Matrix.Rotation(math.radians(-90.0), 4, 'X') #! maybe just set cam for w2scenes?
                    pos_curves = [dummy_keyframe_points] * 3
                    rot_curves = [dummy_keyframe_points] * 4

                    prop_rot_map = {'QUATERNION':'rotation_quaternion', 'AXIS_ANGLE':'rotation_axis_angle'}
                    data_path_rot = prop_rot_map.get(bl_bone.rotation_mode, 'rotation_quaternion')
                    bone_rotation = getattr(bl_bone, data_path_rot)
                    data_path = 'pose.bones["%s"].location'%bl_bone.name
                    for axis_i in range(3):
                        pos_curves[axis_i] = new_action_fcurve(InterpolationAction, scene_cam_obj, data_path=data_path, index=axis_i, group_name=bl_bone.name)
                    data_path = 'pose.bones["%s"].%s'%(bl_bone.name, data_path_rot)
                    for axis_i in range(len(bone_rotation)):
                        rot_curves[axis_i] = new_action_fcurve(InterpolationAction, scene_cam_obj, data_path=data_path, index=axis_i, group_name=bl_bone.name)

                    track_curves = [new_action_fcurve(InterpolationAction, scene_cam_obj, data_path="pose.bones[\"Camera_Node\"][\"hctFOV\"]")] 
                    
                    for camera_matrix, camera_fov, event in keyGuidsObjs:
                        bone_matrix = camera_matrix @ rotation_fix_matrix
                        bl_bone.matrix = scene_cam_obj.matrix_world.inverted() @ bone_matrix
                        scene_cam_obj.pose.bones["Camera_Node"]["hctFOV"] = camera_fov
                        interFrame = 0.0

                        for i in range(3):
                            pos_curves[i].keyframe_points.add(1)
                            pos_curves[i].keyframe_points[-1].co = (interFrame, bl_bone.location[i])
                            pos_curves[i].keyframe_points[-1].interpolation = 'LINEAR'
                        for i in range(4):
                            rot_curves[i].keyframe_points.add(1)
                            rot_curves[i].keyframe_points[-1].co = (interFrame, bl_bone.rotation_quaternion[i])
                            rot_curves[i].keyframe_points[-1].interpolation = 'LINEAR'
                        track_curves[0].keyframe_points.insert(interFrame, camera_fov)
                        track_curves[0].keyframe_points[-1].interpolation = 'LINEAR' # CONSTANT
                            
                    tmp_frame = self.__frame_current
                    self.__assign_action(scene_cam_obj, InterpolationAction, track_name = "CustomCameraInstance", at_frame = marker_frame)
                    self.__frame_current = tmp_frame
                else:
                    log.debug("Unhandled event type: %s", event.__class__.__name__)
            
            ###################
            #  SHOT ENDING    #
            ###################
            self.__frame_current = dialogframe #! THE FRAME THIS SHOT ENDS ON

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
