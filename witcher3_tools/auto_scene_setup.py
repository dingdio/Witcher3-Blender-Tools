# -*- coding: utf-8 -*-

import bpy

def setupFrameRanges(use_NLA = False, target_obj = None):
    obj = target_obj if target_obj is not None else bpy.context.object
    s, e = 1, 1
    if use_NLA:
        if obj and getattr(obj, "animation_data", None) and obj.animation_data.nla_tracks:
            for track in obj.animation_data.nla_tracks:
                for i in track.strips:
                    s = min(s, i.frame_start)
                    e = max(e, i.frame_end)
        else:
            for scene_obj in bpy.context.scene.objects:
                anim_data = getattr(scene_obj, "animation_data", None)
                if not anim_data or not anim_data.nla_tracks:
                    continue
                for track in anim_data.nla_tracks:
                    for i in track.strips:
                        s = min(s, i.frame_start)
                        e = max(e, i.frame_end)
    else:
        if obj and getattr(obj, "animation_data", None) and obj.animation_data.action:
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

def setupFps():
    bpy.context.scene.render.fps = 30
    bpy.context.scene.render.fps_base = 1
