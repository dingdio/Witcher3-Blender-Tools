import logging
import bpy
from bpy.app.handlers import persistent


log = logging.getLogger(__name__)


_runtime_armature_name_by_scene = {}
_runtime_explicit_none_by_scene = {}


def _armature_poll(_self, obj):
    return bool(obj and obj.type == "ARMATURE")


def is_armature_object(obj):
    try:
        return bool(obj and obj.type == "ARMATURE")
    except ReferenceError:
        return False
    except Exception:
        return False


def is_armature_in_scene(scene, obj):
    if scene is None or not is_armature_object(obj):
        return False
    try:
        return scene.objects.get(obj.name) is obj
    except Exception:
        return False


def get_rig_settings(armature_obj):
    if not is_armature_object(armature_obj):
        return None
    return getattr(armature_obj.data, "witcherui_RigSettings", None)


def armature_has_character_data(armature_obj):
    rig_settings = get_rig_settings(armature_obj)
    if rig_settings is None:
        return False
    if getattr(rig_settings, "main_entity_skeleton", ""):
        return True
    if getattr(rig_settings, "main_face_skeleton", ""):
        return True
    if getattr(rig_settings, "entity_name", ""):
        return True
    if getattr(rig_settings, "jsonData", ""):
        return True
    try:
        if len(rig_settings.app_list) > 0:
            return True
    except Exception:
        pass
    try:
        if len(rig_settings.bone_order_list) > 0:
            return True
    except Exception:
        pass
    return False


def _scene_key(scene):
    if scene is None:
        return 0
    try:
        return int(scene.as_pointer())
    except Exception:
        return 0


def _set_runtime_target(scene, armature_obj, explicit_none=False):
    key = _scene_key(scene)
    if not key:
        return
    if is_armature_in_scene(scene, armature_obj):
        _runtime_armature_name_by_scene[key] = armature_obj.name
        _runtime_explicit_none_by_scene[key] = False
        return
    _runtime_armature_name_by_scene[key] = ""
    _runtime_explicit_none_by_scene[key] = bool(explicit_none)


def _get_runtime_target(scene):
    key = _scene_key(scene)
    if not key:
        return None
    obj_name = _runtime_armature_name_by_scene.get(key, "")
    if not obj_name:
        return None
    obj = bpy.data.objects.get(obj_name)
    if is_armature_in_scene(scene, obj):
        return obj
    _set_runtime_target(scene, None, explicit_none=False)
    return None


def _get_explicit_none(scene):
    if scene is None:
        return False
    try:
        return bool(getattr(scene, "witcher_main_armature_explicit_none", False))
    except Exception:
        key = _scene_key(scene)
        return bool(_runtime_explicit_none_by_scene.get(key, False))


def _get_scene_pointer(scene):
    if scene is None or not hasattr(scene, "witcher_main_armature"):
        return None
    try:
        armature = getattr(scene, "witcher_main_armature", None)
    except Exception:
        armature = None
    if is_armature_in_scene(scene, armature):
        _set_runtime_target(scene, armature, explicit_none=False)
        return armature
    return _get_runtime_target(scene)


def set_main_armature(scene, armature_obj, explicit_none=False):
    if scene is None or not hasattr(scene, "witcher_main_armature"):
        _set_runtime_target(scene, armature_obj, explicit_none=explicit_none)
        return None

    if is_armature_in_scene(scene, armature_obj):
        _set_runtime_target(scene, armature_obj, explicit_none=False)
        try:
            scene.witcher_main_armature = armature_obj
            scene.witcher_main_armature_explicit_none = False
        except Exception:
            pass
        return armature_obj

    _set_runtime_target(scene, None, explicit_none=explicit_none)
    try:
        scene.witcher_main_armature = None
        scene.witcher_main_armature_explicit_none = bool(explicit_none)
    except Exception:
        pass
    return None


def _pick_auto_candidate(scene):
    if scene is None:
        return None

    armatures = [obj for obj in scene.objects if is_armature_object(obj)]
    if not armatures:
        return None

    character_armatures = [obj for obj in armatures if armature_has_character_data(obj)]
    if len(character_armatures) == 1:
        return character_armatures[0]
    if len(character_armatures) > 1:
        return None

    if len(armatures) == 1:
        return armatures[0]
    return None


def get_main_armature(context, prefer_active=True, remember=True, fallback=True):
    if context is None:
        return None
    scene = getattr(context, "scene", None)
    if scene is None:
        return None

    stored = _get_scene_pointer(scene)

    active_obj = getattr(context, "object", None) or getattr(context, "active_object", None)
    if prefer_active and is_armature_in_scene(scene, active_obj):
        if armature_has_character_data(active_obj) or stored is None:
            if remember:
                set_main_armature(scene, active_obj)
            return active_obj

    if stored is not None:
        return stored

    if _get_explicit_none(scene):
        return None

    selected_objects = getattr(context, "selected_objects", []) or []
    for obj in selected_objects:
        if is_armature_in_scene(scene, obj):
            if remember:
                set_main_armature(scene, obj)
            return obj

    if not fallback:
        return None

    candidate = _pick_auto_candidate(scene)
    if candidate and remember:
        set_main_armature(scene, candidate)
    return candidate


def get_main_armature_and_rig_settings(context, prefer_active=True, remember=True, fallback=True):
    armature = get_main_armature(
        context,
        prefer_active=prefer_active,
        remember=remember,
        fallback=fallback,
    )
    return armature, get_rig_settings(armature)


def format_character_label(armature_obj):
    if not is_armature_object(armature_obj):
        return "None"

    rig_settings = get_rig_settings(armature_obj)
    entity_name = ""
    if rig_settings:
        entity_name = (getattr(rig_settings, "entity_name", "") or "").strip()

    if entity_name and entity_name != armature_obj.name:
        return f"{entity_name} ({armature_obj.name})"
    if entity_name:
        return entity_name
    return armature_obj.name


def draw_main_armature_selector(layout, context, label="Character", fallback=True):
    scene = getattr(context, "scene", None)
    if scene is None or not hasattr(scene, "witcher_main_armature"):
        return None

    row = layout.row(align=True)
    row.prop(scene, "witcher_main_armature", text=label, icon="ARMATURE_DATA")
    row.operator(WITCH_OT_ClearMainArmature.bl_idname, text="", icon="X")

    armature = get_main_armature(
        context,
        prefer_active=True,
        remember=False,
        fallback=fallback,
    )
    status = layout.row(align=True)
    if armature:
        status.label(text=f"Current: {format_character_label(armature)}", icon="ARMATURE_DATA")
    else:
        status.label(text="Current: None", icon="INFO")

    active_obj = getattr(context, "object", None)
    if active_obj and active_obj.type == "ARMATURE" and armature and active_obj != armature:
        layout.label(text=f"Active armature is not target: {active_obj.name}", icon="INFO")

    return armature


def _on_main_armature_changed(scene, _context):
    armature = getattr(scene, "witcher_main_armature", None)
    if is_armature_object(armature):
        _set_runtime_target(scene, armature, explicit_none=False)
        try:
            scene.witcher_main_armature_explicit_none = False
        except Exception:
            pass


_last_scene_ptr = 0
_last_active_ptr = 0


def _sync_main_armature_from_active(context=None):
    global _last_scene_ptr, _last_active_ptr

    ctx = context or bpy.context
    scene = getattr(ctx, "scene", None)
    view_layer = getattr(ctx, "view_layer", None)
    if scene is None or view_layer is None:
        return

    # Clear stale target when object was deleted or removed from this scene.
    try:
        raw_scene_target = getattr(scene, "witcher_main_armature", None)
    except Exception:
        raw_scene_target = None
    if raw_scene_target and not is_armature_in_scene(scene, raw_scene_target):
        set_main_armature(scene, None, explicit_none=False)

    active_obj = getattr(view_layer.objects, "active", None)
    try:
        scene_ptr = scene.as_pointer()
    except Exception:
        scene_ptr = 0
    try:
        active_ptr = active_obj.as_pointer() if active_obj else 0
    except Exception:
        active_ptr = 0

    if scene_ptr == _last_scene_ptr and active_ptr == _last_active_ptr:
        return
    _last_scene_ptr = scene_ptr
    _last_active_ptr = active_ptr

    if is_armature_in_scene(scene, active_obj) and armature_has_character_data(active_obj):
        set_main_armature(scene, active_obj)


@persistent
def _auto_sync_active_armature_handler(_scene, _depsgraph):
    try:
        _sync_main_armature_from_active()
    except Exception:
        pass


class WITCH_OT_ClearMainArmature(bpy.types.Operator):
    bl_idname = "witcher.clear_main_armature"
    bl_label = "Clear Character Target"
    bl_description = "Set the current character target to None"

    def execute(self, context):
        set_main_armature(context.scene, None, explicit_none=True)
        return {"FINISHED"}


classes = [
    WITCH_OT_ClearMainArmature,
]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    if not hasattr(bpy.types.Scene, "witcher_main_armature"):
        bpy.types.Scene.witcher_main_armature = bpy.props.PointerProperty(
            name="Main Armature",
            description="Character armature target for animation/entity/equipment tools",
            type=bpy.types.Object,
            poll=_armature_poll,
            update=_on_main_armature_changed,
        )

    if not hasattr(bpy.types.Scene, "witcher_main_armature_explicit_none"):
        bpy.types.Scene.witcher_main_armature_explicit_none = bpy.props.BoolProperty(
            name="Main Armature Explicit None",
            description="Internal flag to keep target armature cleared when user selects None",
            default=False,
            options={"HIDDEN"},
        )

    if _auto_sync_active_armature_handler not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_auto_sync_active_armature_handler)

    _sync_main_armature_from_active()


def unregister():
    if _auto_sync_active_armature_handler in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_auto_sync_active_armature_handler)

    try:
        from ..importers.import_entity import _unregister_entity_cache_handler
        _unregister_entity_cache_handler()
    except Exception:
        pass

    if hasattr(bpy.types.Scene, "witcher_main_armature_explicit_none"):
        del bpy.types.Scene.witcher_main_armature_explicit_none
    if hasattr(bpy.types.Scene, "witcher_main_armature"):
        del bpy.types.Scene.witcher_main_armature

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
