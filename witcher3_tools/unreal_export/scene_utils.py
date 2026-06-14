"""Low-level Blender object/selection helpers shared by the Unreal export
modules. Kept dependency-free (only ``bpy`` at call time) so both
``bundle`` and ``export_armature`` can use them without a circular import."""

from __future__ import annotations

from typing import Any


def _active_object(context):
    try:
        return context.view_layer.objects.active
    except Exception:
        return getattr(context, "object", None)


def _set_active_object(context, obj) -> None:
    try:
        context.view_layer.objects.active = obj
    except Exception:
        pass


def _object_mode(context) -> str:
    return str(getattr(getattr(context, "object", None), "mode", "") or "OBJECT")


def _deselect_all(context) -> None:
    try:
        import bpy
        bpy.ops.object.select_all(action="DESELECT")
        return
    except Exception:
        pass
    for selected in list(getattr(context, "selected_objects", []) or []):
        try:
            selected.select_set(False)
        except Exception:
            pass


def _snapshot_object_state(context) -> tuple[Any, list[Any], str]:
    return (
        _active_object(context),
        list(getattr(context, "selected_objects", []) or []),
        _object_mode(context),
    )


def _select_only(context, obj) -> None:
    _deselect_all(context)
    try:
        obj.select_set(True)
    except Exception:
        pass
    _set_active_object(context, obj)


def _restore_object_state(context, state, *, restore_mode: bool = True) -> None:
    saved_active, saved_selection, saved_mode = state
    _deselect_all(context)
    for obj in saved_selection:
        if not _object_still_exists(obj):
            continue
        try:
            obj.select_set(True)
        except Exception:
            pass
    if saved_active is not None and _object_still_exists(saved_active):
        _set_active_object(context, saved_active)
    if restore_mode and saved_mode != "OBJECT" and _active_object(context):
        try:
            import bpy
            bpy.ops.object.mode_set(mode=saved_mode)
        except Exception:
            pass


def _object_still_exists(obj) -> bool:
    if obj is None:
        return False
    try:
        import bpy
        return _bpy_objects_contains_name(bpy.data.objects, getattr(obj, "name", ""))
    except Exception:
        return True


def _remove_object(obj) -> None:
    import bpy

    try:
        obj_type = getattr(obj, "type", "")
        mesh = obj.data
        bpy.data.objects.remove(obj, do_unlink=True)
        if mesh and mesh.users == 0:
            if obj_type == "MESH":
                bpy.data.meshes.remove(mesh)
            elif obj_type == "ARMATURE":
                bpy.data.armatures.remove(mesh)
    except Exception:
        pass


def _iter_bpy_objects():
    import bpy

    objects = getattr(getattr(bpy, "data", None), "objects", None)
    if objects is None:
        return []
    try:
        return [obj for obj in objects if hasattr(obj, "name")]
    except Exception:
        return []


def _bpy_objects_contains_name(objects, name: str) -> bool:
    try:
        if name in objects:
            return True
    except Exception:
        pass
    try:
        return any(getattr(obj, "name", None) == name for obj in objects)
    except Exception:
        return False


def _unique_temp_object_name(prefix: str, objects) -> str:
    existing = {str(getattr(obj, "name", "") or "") for obj in objects}
    candidate = prefix
    counter = 2
    while candidate in existing:
        candidate = f"{prefix}_{counter}"
        counter += 1
    return candidate
