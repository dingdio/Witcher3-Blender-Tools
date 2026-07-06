"""Shared helpers for Blender physics runtime modules."""

from __future__ import annotations


def state_key(obj):
    return str(getattr(obj, "name_full", getattr(obj, "name", "")))


def rig_uses_rot90(obj):
    rig_settings = getattr(getattr(obj, "data", None), "witcherui_RigSettings", None)
    if rig_settings is None:
        return True
    rot90_state = str(getattr(rig_settings, "rot90_state", "") or "").upper()
    if rot90_state == "ON":
        return True
    if hasattr(rig_settings, "rot90_imported") or hasattr(rig_settings, "rot90_compensate"):
        return bool(
            getattr(rig_settings, "rot90_imported", True)
            or getattr(rig_settings, "rot90_compensate", False)
        )
    return True


def object_by_runtime_name(name, objects, *, key_fn=state_key):
    objects = objects or ()
    getter = getattr(objects, "get", None)
    if callable(getter):
        return getter(name)
    for obj in objects:
        if key_fn(obj) == name:
            return obj
    return None


def find_armature(context, is_runtime_kind):
    obj = getattr(context, "active_object", None)
    while obj is not None:
        if is_runtime_kind(obj):
            return obj
        obj = getattr(obj, "parent", None)
    for obj in getattr(context, "selected_objects", []) or []:
        if is_runtime_kind(obj):
            return obj
    return None


def objects_for_character(root, objects, *, is_runtime_kind, is_descendant_of):
    if root is None:
        return []
    result = [obj for obj in objects if is_runtime_kind(obj) and is_descendant_of(obj, root)]
    result.sort(key=lambda obj: obj.name.lower())
    return result


def objects_for_context(context, *, find_root, find_armature_fn, objects_for_character_fn):
    root = find_root(context)
    if root is not None:
        objects = objects_for_character_fn(root)
        if objects:
            return objects
    obj = find_armature_fn(context)
    return [obj] if obj is not None else []


def enable_objects(objects, enabled, enable_object):
    return sum(1 for obj in objects if enable_object(obj, enabled))


def refresh_runtime_object_names(
    scene,
    runtime_names,
    objects,
    *,
    live_preview_enabled,
    is_runtime_kind,
    has_runtime_opt_in,
    key_fn=state_key,
):
    if not live_preview_enabled(scene):
        return
    for obj in objects or []:
        if is_runtime_kind(obj) and has_runtime_opt_in(obj):
            runtime_names.add(key_fn(obj))
    for name in list(runtime_names):
        obj = object_by_runtime_name(name, objects, key_fn=key_fn)
        if obj is None or not is_runtime_kind(obj) or not has_runtime_opt_in(obj):
            runtime_names.discard(name)


def enabled_runtime_objects(scene, objects, *, live_preview_enabled, is_runtime_kind, is_runtime_enabled):
    if not live_preview_enabled(scene):
        return []
    return [obj for obj in objects if is_runtime_kind(obj) and is_runtime_enabled(obj, scene)]


def runtime_objects(
    scene,
    runtime_names,
    objects,
    *,
    live_preview_enabled,
    is_runtime_kind,
    has_runtime_opt_in,
    is_runtime_enabled,
    key_fn=state_key,
):
    refresh_runtime_object_names(
        scene,
        runtime_names,
        objects,
        live_preview_enabled=live_preview_enabled,
        is_runtime_kind=is_runtime_kind,
        has_runtime_opt_in=has_runtime_opt_in,
        key_fn=key_fn,
    )
    if not runtime_names or not live_preview_enabled(scene):
        return []

    result = []
    for name in list(runtime_names):
        obj = object_by_runtime_name(name, objects, key_fn=key_fn)
        if obj is None or not is_runtime_kind(obj) or not is_runtime_enabled(obj, scene):
            runtime_names.discard(name)
            continue
        result.append(obj)
    return result


def frame_dt(scene, obj, last_frames, *, reset_frame_gap, max_advance_frames, key_fn=state_key):
    fps = float(getattr(scene.render, "fps", 24) or 24)
    fps_base = float(getattr(scene.render, "fps_base", 1.0) or 1.0)
    current = int(scene.frame_current)
    key = key_fn(obj)
    previous = last_frames.get(key)
    last_frames[key] = current
    dt = fps_base / fps
    if previous is None or current <= int(previous):
        return dt, True
    frame_gap = current - int(previous)
    if frame_gap > reset_frame_gap:
        return dt, True
    frame_step = max(1, min(frame_gap, max_advance_frames))
    return (frame_step * fps_base) / fps, False


def scene_frame_dt(scene):
    return float(scene.render.fps_base or 1.0) / float(scene.render.fps or 24)


def normalized_frame_range(frame_start, frame_end):
    frame_start = int(frame_start)
    frame_end = int(frame_end)
    return (frame_end, frame_start) if frame_end < frame_start else (frame_start, frame_end)


def is_rest_frame(scene):
    current = int(scene.frame_current)
    return current <= int(scene.frame_start) or current <= 0


def remove_frame_handler(handlers, handler_name, module_suffix):
    for handler in list(handlers):
        if getattr(handler, "__name__", "") == handler_name and str(getattr(handler, "__module__", "")).endswith(module_suffix):
            handlers.remove(handler)


def ensure_frame_handler(
    handlers,
    handler,
    *,
    handler_name,
    module_suffix,
    runtime_names,
    live_preview_enabled,
    refresh_names,
    scene,
):
    remove_frame_handler(handlers, handler_name, module_suffix)
    if not live_preview_enabled(scene):
        return
    refresh_names(scene)
    if runtime_names:
        handlers.append(handler)


def keyframe_pose_bone_transform(pose_bone, frame):
    pose_bone.keyframe_insert(data_path="location", frame=frame)
    rotation_path = {
        "QUATERNION": "rotation_quaternion",
        "AXIS_ANGLE": "rotation_axis_angle",
    }.get(pose_bone.rotation_mode, "rotation_euler")
    pose_bone.keyframe_insert(data_path=rotation_path, frame=frame)
    pose_bone.keyframe_insert(data_path="scale", frame=frame)
