# -*- coding: utf-8 -*-

import bpy

def setupFrameRanges(use_NLA = False):
    obj = bpy.context.object
    s, e = 1, 1
    if use_NLA:
        if obj.animation_data.nla_tracks:
            for track in obj.animation_data.nla_tracks:
                for i in track.strips:
                    s = min(s, i.frame_start)
                    e = max(e, i.frame_end)
    else:
        if obj.animation_data.action:
            i = obj.animation_data.action
            ts, te = i.frame_range
            s = min(s, ts)
            e = max(e, te)
        else:
            for i in bpy.data.actions:
                ts, te = i.frame_range
                s = min(s, ts)
                e = max(e, te)
    bpy.context.scene.frame_start = int(s)
    bpy.context.scene.frame_end = int(e)
    if bpy.context.scene.rigidbody_world is not None:
        bpy.context.scene.rigidbody_world.point_cache.frame_start = int(s)
        bpy.context.scene.rigidbody_world.point_cache.frame_end = int(e)

def setupLighting():
    bpy.context.scene.world.light_settings.use_ambient_occlusion = True
    bpy.context.scene.world.light_settings.use_environment_light = True
    bpy.context.scene.world.light_settings.use_indirect_light = True

def setupFps():
    bpy.context.scene.render.fps = 30
    bpy.context.scene.render.fps_base = 1
