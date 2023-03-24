from io_import_w2l.setup_logging_bl import *
log = logging.getLogger(__name__)
import os
import json
import numpy as np
from pathlib import Path
from io_import_w2l import get_uncook_path
from io_import_w2l.CR2W import read_json_w3
from io_import_w2l.CR2W import w3_types
from io_import_w2l.CR2W.CR2W_types import EngineTransform
from io_import_w2l.CR2W.dc_scene import load_bin_scene
from io_import_w2l.importers import import_entity
from io_import_w2l.importers.import_blender_fun import set_blender_object_transform, set_blender_pose_bone_transform
from .import_cutscene import check_if_actor_already_in_scene
from mathutils import Euler
from math import radians
import math
from mathutils import Matrix
from io_import_w2l.ui.ui_voice import load_voice_and_lipsync
from io_import_w2l.ui.ui_anims_list import SetupActor, GetAnimationInfoByName, load_anim_into_scene

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
from .import_anims import NewListItem, set_global_set

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
        track_name == track_name if track_name else self.__NLA_track

        if not self.__use_NLA:
            target.animation_data.action = action
        else:
            #frame_current = bpy.context.scene.frame_current
            if track_name:
                target_track: bpy.types.NlaTrack = target.animation_data.nla_tracks.get(track_name)
                if target_track is None:
                    target_track: bpy.types.NlaTrack = target.animation_data.nla_tracks.new()
                    target_track.name = track_name #action.name
                if self.__frame_current !=0:
                    pass # adding multiple strips
                else:
                    for strip in target_track.strips:
                        target_track.strips.remove(strip)
            else:
                target_track: bpy.types.NlaTrack = target.animation_data.nla_tracks.new()
                target_track.name = action.name
            
            if at_frame:
                self.__frame_current = at_frame
            test_strips = []
            for st in target_track.strips:
                test_strips.append(st)
            if target_track.strips:
                last_strip = target_track.strips[-1]
                strip_start = last_strip.frame_end
            # else:
            try:
                target_strip = target_track.strips.new(action.name, self.__frame_current, action)
            except Exception as e:
                target_strip = target_track.strips.new(action.name, int(self.__frame_current + 1), action)
                target_strip.frame_start = self.__frame_current
                start_frame, end_frame = action.frame_range
                length = end_frame - start_frame
                target_strip.frame_end = self.__frame_current + length
            target_strip.blend_type = 'REPLACE'

    def loadSceneFile(self, filePath):
        self._CStoryScene:w3_types.CStoryScene = loadSceneFile(filePath)

    def load_section(self, section):
        self.scene_element_dict = {}

        for el in section.sceneElements.value:
            chunk = self._CStoryScene.chunksRef[el-1]
            sceneElement = w3_types.str_to_class(chunk.Type)(chunk)
            shot_dict = {}
            shot_dict['dialogscript'] = sceneElement
            shot_dict['CUE'] = []
            self.scene_element_dict[el] = shot_dict
            #each sceneelement contains dialoge and "CUE" shot that
            #contains all the events (sceneEventElements)
            #shot_1
            #shot_2
            #shot_3

        for sceneEventElement in section.sceneEventElements:
            el_type =  sceneEventElement.__class__.__name__
            if hasattr(sceneEventElement, 'theType'):
                raise Exception('Missing Event Class')
            else:
                self.scene_element_dict[sceneEventElement.sceneElement.Value]['CUE'].append(sceneEventElement)

    def load_sections(self):
        for el in self._CStoryScene.sections.value: #<array:2,0,ptr:CStorySceneSection>
            chunk = self._CStoryScene.chunksRef[el-1]
            section = w3_types.CStorySceneSection(chunk)
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


        scene_camera_entity_path =  "gameplay\camera\scene_camera.w2ent"

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

        for actor in _CStoryScene.sceneTemplates.value: #<array:2,0,ptr:CStorySceneActor>
            actor_template = _CStoryScene.chunksRef[actor-1]
            actor = w3_types.CStorySceneActor(actor_template)

            actor_obj = check_if_actor_already_in_scene(actor.entityTemplate)
            if not actor_obj:
                actor_obj = import_entity.import_ent_template(get_uncook_path(bpy.context)+'\\'+actor.entityTemplate, load_face_poses=actor.useMimic)
            actors_dict[actor.id] = (actor_obj, actor)

        for di in _CStoryScene.dialogsetInstances.value: #<array:2,0,ptr:CStorySceneActor>
            chunk = _CStoryScene.chunksRef[di-1]
            _di = w3_types.CStorySceneDialogsetInstance(chunk)
            placementTag = _di.placementTag[0] # find placement tag, use for relative transforms

            #place cube should not change if using actor as base. Need to create temp transform
            #placeCube = False
            for key, actor in actors_dict.items():
                if placementTag in actor[1].actorTags:
                    placeCube = actor[0]
                    break

            for dss in _di.slots.value: #<array:2,0,ptr:CStorySceneActor>
                chunk = _CStoryScene.chunksRef[dss-1]
                _dss = w3_types.CStorySceneDialogsetSlot(chunk)
                reset_transforms(actors_dict[_dss.actorName][0])
                if _dss.slotPlacement:
                    set_blender_object_transform(actors_dict[_dss.actorName][0], _dss.slotPlacement.EngineTransform, from_this_object = placeCube)
                else:
                    set_blender_object_transform(actors_dict[_dss.actorName][0], BlankEngineTransform(), from_this_object = placeCube)

        #shot = scene_element_dict[8]
        CustomCameraInstances = {}
        self.__frame_current = 0 #? this controls the duration of each strip regardless of Interpolation events
        __fps = 30
        
        
        
        ###################
        #   RESET SCENE   #
        ###################
        #TODO delete all markers in the scene
        
        def remove_strips_from_track(scene_cam_obj, trackname):
            if scene_cam_obj.animation_data is None:
                print("No animation data yet.")
                return
            if trackname in scene_cam_obj.animation_data.nla_tracks:
                track = scene_cam_obj.animation_data.nla_tracks[trackname]
                for strip in track.strips:
                    track.strips.remove(strip)
            else:
                print(f"Track '{trackname}' not found in the NLA Editor.")
        
        def reset_scene(scene_cam_obj):
            scene = bpy.context.scene
            for marker in scene.timeline_markers:
                scene.timeline_markers.remove(marker)
                
            remove_strips_from_track(scene_cam_obj, "CameraInterpolation")
            remove_strips_from_track(scene_cam_obj, "CustomCameraInstance")
            remove_strips_from_track(scene_cam_obj, "PAUSE")
        reset_scene(scene_cam_obj)
        
        for key, shot in self.scene_element_dict.items():
            # if key != 7:
            #     break
            # if key == 7:
            #     continue
            
            ###################
            #   SHOT SCRIPT   #
            ###################
            if shot['dialogscript'].__class__.__name__ == 'CStoryScenePauseElement':
                action = bpy.data.actions.new(name="PAUSE")
                class _Dummy: pass
                dummy_keyframe_points = iter(lambda: _Dummy, None)
                pos_curves = [dummy_keyframe_points] * 3
                dialogframe = self.__frame_current
                for axis_i in range(3):
                    pos_curves[axis_i] = action.fcurves.new(data_path='location', index=axis_i, action_group="PAUSE")
                #PAUSE BEGIN
                for i in range(3):
                    pos_curves[i].keyframe_points.add(1)
                    pos_curves[i].keyframe_points[-1].co = (dialogframe, scene_cam_obj.location[i])
                    pos_curves[i].keyframe_points[-1].interpolation = 'LINEAR'

                #PAUSE END
                dialogframe=self.__frame_current + shot['dialogscript'].duration *__fps
                for i in range(3):
                    pos_curves[i].keyframe_points.add(1)
                    pos_curves[i].keyframe_points[-1].co = (dialogframe, scene_cam_obj.location[i])
                    pos_curves[i].keyframe_points[-1].interpolation = 'LINEAR'

                self.__assign_action(scene_cam_obj, action, track_name = "PAUSE")
            elif shot['dialogscript'].__class__.__name__ == 'CStorySceneLine':
                curr_actor = set_cur_actor_by_str(shot['dialogscript'].voicetag, actors_dict)
                load_voice_and_lipsync(shot['dialogscript'].dialogLine.String.val, curr_actor, context=context, at_frame = self.__frame_current)
                action = bpy.data.actions.new(name="dialogLine")
                class _Dummy: pass
                dummy_keyframe_points = iter(lambda: _Dummy, None)
                pos_curves = [dummy_keyframe_points] * 3
                dialogframe = self.__frame_current
                for axis_i in range(3):
                    pos_curves[axis_i] = action.fcurves.new(data_path='location', index=axis_i, action_group="dialogLine")
                #PAUSE BEGIN
                for i in range(3):
                    pos_curves[i].keyframe_points.add(1)
                    pos_curves[i].keyframe_points[-1].co = (dialogframe, scene_cam_obj.location[i])
                    pos_curves[i].keyframe_points[-1].interpolation = 'LINEAR'

                #PAUSE END
                dialogframe=self.__frame_current + shot['dialogscript'].approvedDuration *__fps
                for i in range(3):
                    pos_curves[i].keyframe_points.add(1)
                    pos_curves[i].keyframe_points[-1].co = (dialogframe, scene_cam_obj.location[i])
                    pos_curves[i].keyframe_points[-1].interpolation = 'LINEAR'

                self.__assign_action(scene_cam_obj, action, track_name = "dialogLine")
            else:
                dialogframe = self.__frame_current
                print(shot['dialogscript'].__class__.__name__)
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
                    SetupActor(curr_actor)
                    (anim_name, fdir) = GetAnimationInfoByName(event.animationName)
                    load_anim_into_scene(bpy.context, anim_name, fdir, curr_actor, "EventAnimation", at_frame = self.__frame_current)
                elif  event.__class__.__name__ == "CStorySceneEventChangePose": #type(event) == w3_types.CStorySceneEventChangePose:
                    event: w3_types.CStorySceneEventChangePose
                    curr_actor = set_cur_actor_by_str(event.actor, actors_dict)
                    SetupActor(curr_actor)
                    if event.forceBodyIdleAnimation:
                        (anim_name, fdir) = GetAnimationInfoByName(event.forceBodyIdleAnimation)
                    elif event.transitionAnimation:
                        (anim_name, fdir) = GetAnimationInfoByName(event.transitionAnimation)
                    load_anim_into_scene(bpy.context, anim_name, fdir, curr_actor, "EventChangePose", self.__frame_current)
                elif event.__class__.__name__ == "CStorySceneEventOverridePlacement":
                    event: w3_types.CStorySceneEventOverridePlacement
                    set_blender_object_transform(actors_dict[event.actorName][0], event.placement.EngineTransform if event.placement else EngineTransform(), from_this_object = placeCube)
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
                        pos_curves[axis_i] = InterpolationAction.fcurves.new(data_path=data_path, index=axis_i, action_group=bl_bone.name)
                    data_path = 'pose.bones["%s"].%s'%(bl_bone.name, data_path_rot)
                    for axis_i in range(len(bone_rotation)):
                        rot_curves[axis_i] = InterpolationAction.fcurves.new(data_path=data_path, index=axis_i, action_group=bl_bone.name)

                    track_curves = [InterpolationAction.fcurves.new(data_path="pose.bones[\"Camera_Node\"][\"hctFOV\"]")] 
                    
                    keyGuidsObjs = []
                    for guid in event.keyGuids.More:
                        cam_event = CustomCameraInstances[guid.GuidString]
                        
                        keyGuidsObjs.append((cams_in_scene[cam_event.customCameraName], cam_event))

                    interFrame = 0
                    for cam1, event in keyGuidsObjs:
                        bone_matrix = cam1.matrix_basis @ rotation_fix_matrix
                        bl_bone.matrix = scene_cam_obj.matrix_world.inverted() @ bone_matrix
                        scene_cam_obj.pose.bones["Camera_Node"]["hctFOV"] = cam1['hctFOV']
                        interFrame = (shot['dialogscript'].duration * (event.startPosition if event.startPosition else 0.0)) * __fps

                        for i in range(3):
                            pos_curves[i].keyframe_points.add(1)
                            pos_curves[i].keyframe_points[-1].co = (interFrame, bl_bone.location[i])
                            pos_curves[i].keyframe_points[-1].interpolation = 'LINEAR'
                        for i in range(4):
                            rot_curves[i].keyframe_points.add(1)
                            rot_curves[i].keyframe_points[-1].co = (interFrame, bl_bone.rotation_quaternion[i])
                            rot_curves[i].keyframe_points[-1].interpolation = 'LINEAR'

                        track_curves[0].keyframe_points.insert(interFrame, cam1['hctFOV'])
                        track_curves[0].keyframe_points[-1].interpolation = 'LINEAR' # CONSTANT
                        #bone.keyframe_insert(data_path='location', frame=frame)
                        #bone.keyframe_insert(data_path='rotation_quaternion', frame=frame)
                        
                        # location_curve.keyframe_points[-1].co = (frame, bone.location[0])
                        # location_curve.keyframe_points[-1].handle_left_type = 'VECTOR'
                        # location_curve.keyframe_points[-1].handle_right_type = 'VECTOR'
                        
                    self.__assign_action(scene_cam_obj, InterpolationAction, track_name = "CameraInterpolation")
                
                elif  event.__class__.__name__ ==  "CStorySceneEventCustomCamera":
                    CustomCameraInstances[event.GUID.GUID.GuidString] = event
                    cam_event = event
                    #TODO create camera and add to scene
                
                elif  event.__class__.__name__ ==  "CStorySceneEventCustomCameraInstance":
                    CustomCameraInstances[event.GUID.GUID.GuidString] = event
                    cam_event = event
                    
                    keyGuidsObjs = []
                    keyGuidsObjs.append((cams_in_scene[cam_event.customCameraName], cam_event))
                    marker_frame = (shot['dialogscript'].approvedDuration * (event.startPosition if event.startPosition else 0.0)) * __fps
                    marker_frame += self.__frame_current
                    context = bpy.context
                    scene = context.scene

                    marker = scene.timeline_markers.new(cam_event.customCameraName, frame=int(marker_frame))
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
                        pos_curves[axis_i] = InterpolationAction.fcurves.new(data_path=data_path, index=axis_i, action_group=bl_bone.name)
                    data_path = 'pose.bones["%s"].%s'%(bl_bone.name, data_path_rot)
                    for axis_i in range(len(bone_rotation)):
                        rot_curves[axis_i] = InterpolationAction.fcurves.new(data_path=data_path, index=axis_i, action_group=bl_bone.name)

                    track_curves = [InterpolationAction.fcurves.new(data_path="pose.bones[\"Camera_Node\"][\"hctFOV\"]")] 
                    
                    interFrame = 0
                    for cam1, event in keyGuidsObjs:
                        bone_matrix = cam1.matrix_basis @ rotation_fix_matrix
                        bl_bone.matrix = scene_cam_obj.matrix_world.inverted() @ bone_matrix
                        scene_cam_obj.pose.bones["Camera_Node"]["hctFOV"] = cam1['hctFOV']
                        interFrame = (shot['dialogscript'].approvedDuration * (event.startPosition if event.startPosition else 0.0)) * __fps

                        for i in range(3):
                            pos_curves[i].keyframe_points.add(1)
                            pos_curves[i].keyframe_points[-1].co = (interFrame, bl_bone.location[i])
                            pos_curves[i].keyframe_points[-1].interpolation = 'LINEAR'
                        for i in range(4):
                            rot_curves[i].keyframe_points.add(1)
                            rot_curves[i].keyframe_points[-1].co = (interFrame, bl_bone.rotation_quaternion[i])
                            rot_curves[i].keyframe_points[-1].interpolation = 'LINEAR'
                        track_curves[0].keyframe_points.insert(interFrame, cam1['hctFOV'])
                        track_curves[0].keyframe_points[-1].interpolation = 'LINEAR' # CONSTANT
                            
                    tmp_frame = self.__frame_current
                    self.__assign_action(scene_cam_obj, InterpolationAction, track_name = "CustomCameraInstance", at_frame = marker_frame)
                    self.__frame_current = tmp_frame
                else:
                    print(event.__class__.__name__)
            
            ###################
            #  SHOT ENDING    #
            ###################
            self.__frame_current = dialogframe #! THE FRAME THIS SHOT ENDS ON

        log.info(f'Loaded scene in {time.time() - s} seconds.')

def import_w3_scene(filePath):
    sceneImporter = SceneImporter()
    sceneImporter.loadSceneFile(filePath)
    return sceneImporter

def set_cur_actor_by_str(actor_tag_str, actors_dict):
    curr_actor = actors_dict[actor_tag_str][0]
    bpy.ops.object.select_all(action='DESELECT')
    curr_actor.select_set(True)
    bpy.context.view_layer.objects.active = curr_actor
    return curr_actor
